"""Synthetic data generator for the Clinical Lab scheduling demo.

Generates, deterministically from a seed:
  * 28 lab technologist profiles (data/techs.json, data/techs.csv)
  * US federal holidays across the planning horizon, with the alternating
    holiday group that works each one (data/holidays.json)
  * Simulated vacation / PTO requests (data/pto_requests.json)
  * Shift definitions and minimum coverage per bench x shift (data/coverage_rules.json)
and prints a supply-vs-demand capacity check.

Shift model: three 8-hour shifts (Day / Evening / Night).
All names and people are fictional. Standard library only:
    python3 sim/generate_roster.py --seed 42 --start 2026-12-01 --months 13
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

DEPARTMENTS = ["Hematology", "Chemistry", "Microbiology", "Blood Bank"]
SHIFTS = {
    "D": {"name": "Day", "start": "07:00", "end": "15:30", "paid_hours": 8},
    "E": {"name": "Evening", "start": "15:00", "end": "23:30", "paid_hours": 8},
    "N": {"name": "Night", "start": "23:00", "end": "07:30", "paid_hours": 8},
}
SHIFT_PREF_CODE = {"Days": "D", "Evenings": "E", "Nights": "N"}

# Minimum staff per bench per shift. Micro has no night bench (Core Lab loads
# overnight blood cultures; Micro keeps an on-call tech) and no weekend evening.
# Federal holidays are staffed like weekends.
_WEEKEND = {"D": {"Hematology": 1, "Chemistry": 1, "Microbiology": 1, "Blood Bank": 1},
            "E": {"Hematology": 1, "Chemistry": 1, "Microbiology": 0, "Blood Bank": 1},
            "N": {"Hematology": 1, "Chemistry": 1, "Microbiology": 0, "Blood Bank": 1}}
COVERAGE = {
    "weekday": {"D": {"Hematology": 2, "Chemistry": 2, "Microbiology": 2, "Blood Bank": 2},
                "E": {"Hematology": 1, "Chemistry": 1, "Microbiology": 1, "Blood Bank": 1},
                "N": {"Hematology": 1, "Chemistry": 1, "Microbiology": 0, "Blood Bank": 1}},
    "weekend": _WEEKEND,
    "holiday": _WEEKEND,
}
# Extra staff allowed above the minimum (full-timers still need their hours).
MAX_EXTRA = {"D": 2, "E": 1, "N": 1}

# PTO policy: max techs from the same home bench off on one day; tighter
# during the blackout window around each major holiday (date -3 .. +3).
PTO_POLICY = {"max_off_per_bench": 2, "max_off_per_bench_blackout": 1, "blackout_days": 3}

# Home-bench headcount (26) + 2 PRN floats = 28.
HEADCOUNT = {"Hematology": 7, "Chemistry": 7, "Microbiology": 5, "Blood Bank": 7}
FLOATS = 2
# Fixed off-shift crews per home bench: (evenings, nights).
CREWS = {"Hematology": (2, 2), "Chemistry": (2, 2), "Microbiology": (1, 0), "Blood Bank": (2, 2)}
PART_TIMERS = 4

CROSS_TRAIN_ODDS = {
    "Hematology": {"Chemistry": 0.6, "Blood Bank": 0.3, "Microbiology": 0.05},
    "Chemistry": {"Hematology": 0.6, "Blood Bank": 0.25, "Microbiology": 0.05},
    "Microbiology": {"Hematology": 0.15, "Chemistry": 0.15, "Blood Bank": 0.1},
    "Blood Bank": {"Hematology": 0.4, "Chemistry": 0.3, "Microbiology": 0.05},
}

FIRST = ["Avery", "Jordan", "Priya", "Marcus", "Elena", "Tomas", "Grace", "Devon", "Aisha", "Kenji",
         "Rosa", "Liam", "Fatima", "Noah", "Hannah", "Mateo", "Chloe", "Samuel", "Imani", "Lucas",
         "Mei", "Andre", "Sofia", "Caleb", "Nadia", "Owen", "Leah", "Rafael", "Zoe", "Ibrahim"]
LAST = ["Nguyen", "Patel", "Okafor", "Garcia", "Kowalski", "Haddad", "Johnson", "Silva", "Kim",
        "Thompson", "Reyes", "Andersen", "Mbeki", "Larsen", "Cohen", "Ortiz", "Walsh", "Tanaka",
        "Dubois", "Rahman", "Fischer", "Moreno", "Clarke", "Ivanova", "Brooks", "Shah", "Romero",
        "Becker", "Yilmaz", "Foster"]

MAJOR_HOLIDAYS = ["Thanksgiving Day", "Christmas Day", "New Year's Day", "Independence Day"]

# Personal availability limits (soft, high weight). weekday: Mon=0..Sun=6.
# week_parity: None = every week, 0/1 = only even/odd ISO weeks.
CONSTRAINT_POOL = [
    {"label": "No Tuesday day shifts (class schedule)", "weekday": 1, "shifts": ["D"], "week_parity": None},
    {"label": "No Sunday nights (childcare Monday AM)", "weekday": 6, "shifts": ["N"], "week_parity": None},
    {"label": "Every other Wednesday off (custody)", "weekday": 2, "shifts": ["D", "E", "N"], "week_parity": 0},
    {"label": "No Thursday evenings (second job)", "weekday": 3, "shifts": ["E"], "week_parity": None},
    {"label": "No Friday evenings/nights (observance)", "weekday": 4, "shifts": ["E", "N"], "week_parity": None},
    {"label": "No Monday day shifts (dialysis appt.)", "weekday": 0, "shifts": ["D"], "week_parity": 1},
]


@dataclass
class Tech:
    id: str
    name: str
    credential: str
    home_dept: str
    competencies: dict  # bench -> "primary" | "competent" | "training"
    fte: float
    employment: str  # FT | PT | PRN
    shift_pref: str  # Days | Evenings | Nights | Rotating
    weekend_group: str = "A"  # A works even-ISO-week weekends, B odd
    holiday_group: str = "1"  # alternates which holidays are worked
    hire_date: str = ""
    seniority_rank: int = 0  # 1 = most senior
    pto_balance_hours: int = 0
    holiday_off_wishlist: list = field(default_factory=list)
    worked_last_year: list = field(default_factory=list)
    max_consecutive_shifts: int = 5
    shifts_per_week: int = 5  # FTE target (PRN = availability cap)
    on_call_eligible: bool = False
    constraints: list = field(default_factory=list)


# ---------------------------------------------------------------- holidays ---
def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (Mon=0) of a month; n=-1 means last."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def observed(d: date) -> date:
    """OPM rule: Saturday -> Friday, Sunday -> Monday."""
    return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d


def federal_holidays(year: int) -> list[dict]:
    actual = [
        ("New Year's Day", date(year, 1, 1)),
        ("Martin Luther King Jr. Day", nth_weekday(year, 1, 0, 3)),
        ("Washington's Birthday (Presidents Day)", nth_weekday(year, 2, 0, 3)),
        ("Memorial Day", nth_weekday(year, 5, 0, -1)),
        ("Juneteenth National Independence Day", date(year, 6, 19)),
        ("Independence Day", date(year, 7, 4)),
        ("Labor Day", nth_weekday(year, 9, 0, 1)),
        ("Columbus Day / Indigenous Peoples' Day", nth_weekday(year, 10, 0, 2)),
        ("Veterans Day", date(year, 11, 11)),
        ("Thanksgiving Day", nth_weekday(year, 11, 3, 4)),
        ("Christmas Day", date(year, 12, 25)),
    ]
    return [{
        "name": name,
        # A 24/7 lab staffs the ACTUAL date; payroll may use the observed date.
        "date": d.isoformat(),
        "observed_date": observed(d).isoformat(),
        "weekday": d.strftime("%A"),
        "major": name in MAJOR_HOLIDAYS,
    } for name, d in actual]


def assign_holiday_groups(holidays: list[dict]) -> None:
    """Alternate the working group holiday by holiday, chronologically.

    Seeded so that Christmas flips each year: group 2 worked Christmas 2025, so
    group 1 works the first holiday in the horizon (Christmas 2026).
    """
    for i, h in enumerate(sorted(holidays, key=lambda h: h["date"])):
        h["working_group"] = "1" if i % 2 == 0 else "2"


# ------------------------------------------------------------------ roster ---
def make_techs(rng: random.Random, today: date) -> list[Tech]:
    names = rng.sample([(f, l) for f in FIRST for l in LAST], 28)
    homes = [d for d, n in HEADCOUNT.items() for _ in range(n)] + ["Float"] * FLOATS
    techs: list[Tech] = []
    for i, home in enumerate(homes):
        first, last = names[i]
        years = round(rng.triangular(0.3, 30, 6), 1)
        hire = today - timedelta(days=int(years * 365.25))
        if home == "Float":
            comps = {d: "competent" for d in DEPARTMENTS}
            comps[rng.choice(DEPARTMENTS)] = "primary"
            employment, fte, spw = "PRN", 0.0, 2
        else:
            comps = {home: "primary"}
            for other, p in CROSS_TRAIN_ODDS[home].items():
                r = rng.random()
                if r < p:
                    comps[other] = "competent"
                elif r < p + 0.08:
                    comps[other] = "training"
            employment, fte, spw = "FT", 1.0, 5
        bench_cred = ["MLS(ASCP)", "MLS(ASCP)SBB"] if home == "Blood Bank" else ["MLS(ASCP)", "MLT(ASCP)"]
        techs.append(Tech(
            id=f"T{i + 1:02d}", name=f"{first} {last}",
            credential=rng.choices(bench_cred, weights=[70, 30])[0],
            home_dept=home, competencies=comps, fte=fte, employment=employment,
            shift_pref="Days", shifts_per_week=spw, hire_date=hire.isoformat(),
            pto_balance_hours=rng.randint(40, 120) + (40 if years >= 5 else 0) + (40 if years >= 10 else 0),
            holiday_off_wishlist=rng.sample(MAJOR_HOLIDAYS, 2),
            max_consecutive_shifts=rng.choice([5, 5, 6]),
            on_call_eligible=home in ("Microbiology", "Blood Bank") and rng.random() < 0.6,
            constraints=rng.sample(CONSTRAINT_POOL, rng.choices([0, 1, 2], weights=[55, 35, 10])[0]),
        ))

    # Fixed evening / night crews (full-time), a few Rotating, the rest Days.
    for dept, (n_eve, n_night) in CREWS.items():
        pool = [t for t in techs if t.home_dept == dept]
        rng.shuffle(pool)
        for t in pool[:n_night]:
            t.shift_pref = "Nights"
        for t in pool[n_night:n_night + n_eve]:
            t.shift_pref = "Evenings"
    day_staff = [t for t in techs if t.shift_pref == "Days" and t.employment == "FT"]
    for t in rng.sample(day_staff, 3):
        t.shift_pref = "Rotating"
    for t in rng.sample([t for t in techs if t.shift_pref == "Days" and t.employment == "FT"], PART_TIMERS):
        t.employment, t.fte, t.shifts_per_week = "PT", 0.6, 3

    # Weekend (A/B) and holiday (1/2) groups alternate WITHIN each crew so every
    # bench/shift has someone available on every weekend and every holiday.
    crews = defaultdict(list)
    for t in techs:
        crews[(t.home_dept, t.shift_pref)].append(t)
    k = 0
    for key in sorted(crews):
        for j, t in enumerate(sorted(crews[key], key=lambda t: t.id)):
            t.weekend_group = "AB"[(j + k) % 2]
            t.holiday_group = "12"[(j + k // 2) % 2]
        k += 1
    for t in techs:
        t.worked_last_year = (["Thanksgiving Day 2025", "New Year's Day 2026"] if t.holiday_group == "1"
                              else ["Christmas Day 2025", "Independence Day 2026"])

    for rank, t in enumerate(sorted(techs, key=lambda t: (t.hire_date, t.id)), start=1):
        t.seniority_rank = rank
    return techs


def pto_hours(t: Tech, d0: date, d1: date) -> int:
    weekdays = sum(1 for i in range((d1 - d0).days + 1) if (d0 + timedelta(days=i)).weekday() < 5)
    return round(8 * weekdays * (t.shifts_per_week / 5))


def make_pto_requests(rng: random.Random, techs: list[Tech], start: date, end: date,
                      holidays: list[dict]) -> list[dict]:
    reqs, n = [], 0
    # Christmas is the most requested time off, then Thanksgiving.
    odds = {"Christmas Day": 0.35, "Thanksgiving Day": 0.22, "New Year's Day": 0.15, "Independence Day": 0.18}
    hol_dates = [(date.fromisoformat(h["date"]), odds[h["name"]]) for h in holidays if h["major"]]

    def add(t: Tech, d0: date, days: int, kind: str):
        nonlocal n
        d1 = d0 + timedelta(days=days - 1)
        if d0 < start or d1 > end:
            return
        n += 1
        reqs.append({
            "id": f"R{n:03d}", "tech_id": t.id, "start": d0.isoformat(), "end": d1.isoformat(),
            "type": kind, "status": "pending", "hours": pto_hours(t, d0, d1),
            "submitted": (d0 - timedelta(days=rng.randint(30, 150))).isoformat(),
        })

    for t in techs:
        if t.employment == "PRN":
            continue
        if rng.random() < 0.8:  # a summer week
            y = start.year + 1 if start.month > 8 else start.year
            add(t, date(y, rng.randint(6, 8), rng.randint(1, 24)), 7, "vacation")
        for h, p in hol_dates:  # holiday-adjacent blocks: the conflict generator
            if rng.random() < p:
                add(t, h - timedelta(days=rng.randint(0, 3)), rng.randint(3, 6), "holiday-adjacent")
        for _ in range(rng.randint(1, 4)):  # scattered single days
            add(t, start + timedelta(days=rng.randint(0, (end - start).days)), 1, "personal")
    return sorted(reqs, key=lambda r: (r["start"], r["id"]))


# ---------------------------------------------------------------- capacity ---
def capacity_check(techs: list[Tech]) -> dict:
    demand = sum(sum(COVERAGE[dt][s].values()) * n
                 for dt, n in (("weekday", 5), ("weekend", 2)) for s in SHIFTS)
    supply = sum(t.shifts_per_week for t in techs if t.employment != "PRN")
    return {"weekly_shift_demand": demand, "weekly_shift_supply_ft_pt": supply,
            "prn_available": sum(t.shifts_per_week for t in techs if t.employment == "PRN"),
            "slack_shifts": supply - demand, "slack_pct": round(100 * (supply - demand) / supply, 1)}


def generate(seed: int = 42, start: str = "2026-12-01", months: int = 13, out_dir: Path = DATA_DIR) -> dict:
    rng = random.Random(seed)
    s = date.fromisoformat(start)
    y, m = divmod(s.month - 1 + months, 12)
    month_end = date(s.year + y, m + 1, 1) - timedelta(days=1)
    s -= timedelta(days=s.weekday())  # snap to Monday (schedules run Mon-Sun weeks)
    e = month_end + timedelta(days=6 - month_end.weekday())  # snap to Sunday

    holidays = [h for yr in range(s.year, e.year + 1) for h in federal_holidays(yr)
                if s.isoformat() <= h["date"] <= e.isoformat()]
    assign_holiday_groups(holidays)
    techs = make_techs(rng, s)
    reqs = make_pto_requests(rng, techs, s, e, holidays)

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "techs.json").write_text(json.dumps([asdict(t) for t in techs], indent=2))
    (out_dir / "holidays.json").write_text(json.dumps(holidays, indent=2))
    (out_dir / "pto_requests.json").write_text(json.dumps(reqs, indent=2))
    (out_dir / "coverage_rules.json").write_text(json.dumps({
        "horizon": {"start": s.isoformat(), "end": e.isoformat()},
        "shifts": SHIFTS, "minimums": COVERAGE, "max_extra": MAX_EXTRA, "pto_policy": PTO_POLICY}, indent=2))
    with open(out_dir / "techs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "credential", "home_dept", "competencies", "employment", "fte",
                    "shifts_per_week", "shift_pref", "weekend_group", "holiday_group", "seniority_rank",
                    "hire_date", "pto_balance_hours", "holiday_off_wishlist", "constraints"])
        for t in techs:
            w.writerow([t.id, t.name, t.credential, t.home_dept,
                        "; ".join(f"{d}:{lvl}" for d, lvl in t.competencies.items()),
                        t.employment, t.fte, t.shifts_per_week, t.shift_pref, t.weekend_group,
                        t.holiday_group, t.seniority_rank, t.hire_date, t.pto_balance_hours,
                        "; ".join(t.holiday_off_wishlist), "; ".join(c["label"] for c in t.constraints)])
    return {"start": s, "end": e, "techs": techs, "holidays": holidays, "requests": reqs,
            "capacity": capacity_check(techs)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--start", default="2026-12-01")
    ap.add_argument("--months", type=int, default=13)
    args = ap.parse_args()
    g = generate(args.seed, args.start, args.months)
    print(f"Horizon {g['start']} -> {g['end']}: {len(g['techs'])} techs, "
          f"{len(g['holidays'])} holidays, {len(g['requests'])} PTO requests")
    for h in g["holidays"]:
        print(f"  {h['date']} {h['weekday']:<9} group {h['working_group']} works  {h['name']}"
              f"{'  *major' if h['major'] else ''}")
    print("Capacity:", g["capacity"])


if __name__ == "__main__":
    main()
