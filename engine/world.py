"""Domain model: roster, rules, calendar helpers and the Schedule container."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BENCHES = ["Hematology", "Chemistry", "Microbiology", "Blood Bank"]
SHIFT_CODES = ["D", "E", "N"]
SHIFT_PREF_CODE = {"Days": "D", "Evenings": "E", "Nights": "N"}
# (previous-day shift, next-day shift) pairs that leave < 8 h rest.
FORBIDDEN_TURNS = {("E", "D"), ("N", "D"), ("N", "E")}
# Fixed Monday epoch for alternating weekend / custody weeks (avoids ISO week-53 quirks).
EPOCH = date(2026, 1, 5)
QUALIFIED = ("primary", "competent")


def daterange(d0: date, d1: date):
    for i in range((d1 - d0).days + 1):
        yield d0 + timedelta(days=i)


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def week_parity(d: date) -> int:
    return ((d - EPOCH).days // 7) % 2


def period_start(d: date) -> date:
    """Start (Monday) of the 2-week pay period containing d (8/80 rule)."""
    ws = week_start(d)
    return ws - timedelta(weeks=week_parity(d))


def period_days(world: "World", d: date) -> list:
    """Days of d's pay period that fall inside the horizon."""
    ps = period_start(d)
    return [x for x in daterange(ps, ps + timedelta(days=13)) if world.start <= x <= world.end]


@dataclass
class World:
    techs: list[dict]
    holidays: dict[date, dict]
    requests: list[dict]
    shifts: dict
    minimums: dict
    max_extra: dict
    pto_policy: dict
    start: date
    end: date

    def __post_init__(self):
        self.by_id = {t["id"]: t for t in self.techs}
        k = self.pto_policy["blackout_days"]
        self.blackout = {}  # date -> major holiday name
        for d, h in self.holidays.items():
            if h["major"]:
                for i in range(-k, k + 1):
                    self.blackout.setdefault(d + timedelta(days=i), h["name"])

    def pto_cap(self, d: date) -> int:
        p = self.pto_policy
        return p["max_off_per_bench_blackout"] if d in self.blackout else p["max_off_per_bench"]

    # -- calendar ---------------------------------------------------------
    def day_type(self, d: date) -> str:
        if d in self.holidays:
            return "holiday"
        return "weekend" if d.weekday() >= 5 else "weekday"

    def demand(self, d: date, s: str, b: str) -> int:
        return self.minimums[self.day_type(d)][s][b]

    def weekend_on(self, t: dict, d: date) -> bool:
        """Is this a weekend the tech's A/B group is scheduled to work?"""
        return "AB"[week_parity(d)] == t["weekend_group"]

    def holiday_on(self, t: dict, d: date) -> bool:
        h = self.holidays.get(d)
        return h is None or t["employment"] == "PRN" or h["working_group"] == t["holiday_group"]

    # -- people -----------------------------------------------------------
    def qualified(self, t: dict, b: str) -> bool:
        return t["competencies"].get(b) in QUALIFIED

    def active(self, t: dict, d: date) -> bool:
        s, e = t.get("start_date"), t.get("end_date")
        return (s is None or d >= date.fromisoformat(s)) and (e is None or d <= date.fromisoformat(e))

    def personal_conflicts(self, t: dict, d: date, s: str) -> list[str]:
        out = []
        for c in t["constraints"]:
            if c["weekday"] == d.weekday() and s in c["shifts"] and \
                    (c["week_parity"] is None or c["week_parity"] == week_parity(d)):
                out.append(c["label"])
        return out

    @classmethod
    def load(cls, data_dir: Path = DATA_DIR) -> "World":
        rules = json.loads((data_dir / "coverage_rules.json").read_text())
        hol = json.loads((data_dir / "holidays.json").read_text())
        return cls(
            techs=json.loads((data_dir / "techs.json").read_text()),
            holidays={date.fromisoformat(h["date"]): h for h in hol},
            requests=json.loads((data_dir / "pto_requests.json").read_text()),
            shifts=rules["shifts"], minimums=rules["minimums"], max_extra=rules["max_extra"],
            pto_policy=rules["pto_policy"],
            start=date.fromisoformat(rules["horizon"]["start"]),
            end=date.fromisoformat(rules["horizon"]["end"]),
        )


@dataclass
class Schedule:
    """assign[(tech_id, date)] = (shift, bench). pto[tech_id] = set of dates."""
    assign: dict = field(default_factory=dict)
    pto: dict = field(default_factory=lambda: defaultdict(set))
    decisions: list = field(default_factory=list)  # PTO decisions with reasons
    log: list = field(default_factory=list)  # change log / audit trail
    source: dict = field(default_factory=dict)  # (tech_id, date) -> solver|manual|callout|swap

    def copy(self) -> "Schedule":
        pto = defaultdict(set, {k: set(v) for k, v in self.pto.items()})
        return Schedule(dict(self.assign), pto, list(self.decisions), list(self.log), dict(self.source))

    def on_pto(self, tid: str, d: date) -> bool:
        return d in self.pto.get(tid, ())

    def staff(self, d: date, s: str, b: str) -> list[str]:
        return [tid for (tid, dd), (ss, bb) in self.assign.items() if dd == d and ss == s and bb == b]

    def by_day(self):
        idx = defaultdict(list)
        for (tid, d), (s, b) in self.assign.items():
            idx[(d, s, b)].append(tid)
        return idx

    # -- persistence ------------------------------------------------------
    def to_json(self) -> dict:
        return {
            "assignments": [{"tech": t, "date": d.isoformat(), "shift": s, "bench": b,
                             "source": self.source.get((t, d), "solver")}
                            for (t, d), (s, b) in sorted(self.assign.items(), key=lambda kv: (kv[0][1], kv[0][0]))],
            "pto": {t: sorted(x.isoformat() for x in ds) for t, ds in self.pto.items() if ds},
            "decisions": self.decisions,
            "log": self.log,
        }

    @classmethod
    def from_json(cls, j: dict) -> "Schedule":
        sch = cls()
        for a in j["assignments"]:
            k = (a["tech"], date.fromisoformat(a["date"]))
            sch.assign[k] = (a["shift"], a["bench"])
            sch.source[k] = a.get("source", "solver")
        for t, ds in j["pto"].items():
            sch.pto[t] = {date.fromisoformat(x) for x in ds}
        sch.decisions, sch.log = j.get("decisions", []), j.get("log", [])
        return sch

    def save(self, path: Path):
        path.write_text(json.dumps(self.to_json(), indent=1))

    @classmethod
    def load(cls, path: Path) -> "Schedule":
        return cls.from_json(json.loads(path.read_text()))
