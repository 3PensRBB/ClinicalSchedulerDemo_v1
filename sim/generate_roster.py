"""Synthetic data generator for the Clinical Lab scheduling demo.

Generates, deterministically from a seed:
  * 28 lab technologist profiles (data/techs.json, data/techs.csv)
  * US federal holidays across the planning horizon (data/holidays.json)
  * Simulated vacation / PTO requests (data/pto_requests.json)
  * Coverage requirements per department x shift (data/coverage_rules.json)
and prints a supply-vs-demand capacity check.

All names and people are fictional. Standard library only:
    python3 sim/generate_roster.py --seed 42 --start 2026-12-01 --months 13
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"

DEPARTMENTS = ["Hematology", "Chemistry", "Microbiology", "Blood Bank"]
SHIFTS = {
    "D": {"name": "Day", "start": "07:00", "end": "19:30", "paid_hours": 12},
    "N": {"name": "Night", "start": "19:00", "end": "07:30", "paid_hours": 12},
}

# Minimum staff per department per shift. Micro runs days only (night blood
# cultures are loaded by Core Lab techs); weekends run a reduced day crew.
COVERAGE = {
    "weekday": {"D": {"Hematology": 2, "Chemistry": 2, "Microbiology": 2, "Blood Bank": 1},
                "N": {"Hematology": 1, "Chemistry": 1, "Microbiology": 0, "Blood Bank": 1}},
    "weekend": {"D": {"Hematology": 1, "Chemistry": 1, "Microbiology": 1, "Blood Bank": 1},
                "N": {"Hematology": 1, "Chemistry": 1, "Microbiology": 0, "Blood Bank": 1}},
    # Federal holidays are staffed like weekends.
    "holiday": {"D": {"Hematology": 1, "Chemistry": 1, "Microbiology": 1, "Blood Bank": 1},
                "N": {"Hematology": 1, "Chemistry": 1, "Microbiology": 0, "Blood Bank": 1}},
}

# Home-department headcount (sums to 26) + 2 float/PRN = 28.
HEADCOUNT = {"Hematology": 7, "Chemistry": 8, "Microbiology": 6, "Blood Bank": 5}
FLOATS = 2

# Plausible cross-training: which other benches a home dept commonly covers.
CROSS_TRAIN_ODDS = {
    "Hematology": {"Chemistry": 0.6, "Blood Bank": 0.3, "Microbiology": 0.05},
    "Chemistry": {"Hematology": 0.6, "Blood Bank": 0.25, "Microbiology": 0.05},
    "Microbiology": {"Hematology": 0.1, "Chemistry": 0.1, "Blood Bank": 0.1},
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

CONSTRAINT_POOL = [
    "No Tuesday days (class schedule)",
    "No nights before Monday (childcare)",
    "Prefers Fri/Sat/Sun weekend block",
    "Max 2 consecutive nights",
    "Carpools: pair with same-shift tech",
    "Every other Wednesday off (custody)",
    "Prefers 3-on / 4-off pattern",
]


@dataclass
class Tech:
    id: str
    name: str
    credential: str
    home_dept: str
    competencies: dict  # dept -> "primary" | "competent" | "training"
    fte: float
    employment: str  # FT | PT | PRN
    shift_pref: str  # Days | Nights | Rotating
    weekend_group: str  # A | B (works alternate weekends)
    holiday_group: str  # 1 | 2 (alternates Thanksgiving+NY vs Christmas+July 4)
    hire_date: str
    seniority_rank: int = 0
    pto_balance_hours: int = 0
    holiday_off_wishlist: list = field(default_factory=list)
    worked_last_year: list = field(default_factory=list)
    max_consecutive_shifts: int = 4
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
    out = []
    for name, d in actual:
        out.append({
            "name": name,
            # A 24/7 lab staffs the ACTUAL date; payroll may use the observed date.
            "date": d.isoformat(),
            "observed_date": observed(d).isoformat(),
            "weekday": d.strftime("%A"),
            "major": name in MAJOR_HOLIDAYS,
        })
    return out


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
            employment, fte = "PRN", 0.0
        else:
            comps = {home: "primary"}
            for other, p in CROSS_TRAIN_ODDS[home].items():
                r = rng.random()
                if r < p:
                    comps[other] = "competent"
                elif r < p + 0.08:
                    comps[other] = "training"
            employment, fte = rng.choices([("FT", 0.9), ("PT", 0.6)], weights=[85, 15])[0]

        # Holiday-group alternation seeded from last year's history.
        hgroup = "1" if i % 2 == 0 else "2"
        worked_last = (["Thanksgiving Day", "New Year's Day"] if hgroup == "1"
                       else ["Christmas Day", "Independence Day"])

        techs.append(Tech(
            id=f"T{i + 1:02d}",
            name=f"{first} {last}",
            credential=rng.choices(["MLS(ASCP)", "MLT(ASCP)", "MLS(ASCP)SBB"], weights=[70, 25, 5])[0]
            if home != "Blood Bank" else rng.choices(["MLS(ASCP)", "MLS(ASCP)SBB"], weights=[70, 30])[0],
            home_dept=home,
            competencies=comps,
            fte=fte,
            employment=employment,
            shift_pref="Rotating",  # assigned below to hit a night-crew target
            weekend_group=rng.choice("AB"),
            holiday_group=hgroup,
            hire_date=hire.isoformat(),
            pto_balance_hours=rng.randint(40, 120) + (40 if years >= 5 else 0) + (40 if years >= 10 else 0),
            holiday_off_wishlist=rng.sample(MAJOR_HOLIDAYS, 2),
            worked_last_year=worked_last,
            max_consecutive_shifts=rng.choice([3, 4, 4, 5]),
            on_call_eligible=home in ("Microbiology", "Blood Bank") and rng.random() < 0.6,
            constraints=rng.sample(CONSTRAINT_POOL, rng.choices([0, 1, 2], weights=[50, 35, 15])[0]),
        ))

    # Dedicated night crew: ~3 per night-staffed dept (Heme, Chem, BB) => 9 night techs.
    for dept in ("Hematology", "Chemistry", "Blood Bank"):
        pool = [t for t in techs if t.home_dept == dept and t.employment != "PRN"]
        for t in rng.sample(pool, 3):
            t.shift_pref = "Nights"
    for t in techs:
        if t.shift_pref != "Nights":
            t.shift_pref = rng.choices(["Days", "Rotating"], weights=[75, 25])[0]

    for rank, t in enumerate(sorted(techs, key=lambda t: t.hire_date), start=1):
        t.seniority_rank = rank
    return techs


def make_pto_requests(rng: random.Random, techs: list[Tech], start: date, end: date,
                      holidays: list[dict]) -> list[dict]:
    reqs, n = [], 0
    hol_dates = [date.fromisoformat(h["date"]) for h in holidays if h["major"]]

    def add(t: Tech, d0: date, days: int, kind: str):
        nonlocal n
        d1 = d0 + timedelta(days=days - 1)
        if d0 < start or d1 > end:
            return
        n += 1
        reqs.append({
            "id": f"R{n:03d}", "tech_id": t.id, "start": d0.isoformat(), "end": d1.isoformat(),
            "type": kind, "status": "pending",
            "submitted": (d0 - timedelta(days=rng.randint(30, 150))).isoformat(),
        })

    for t in techs:
        if t.employment == "PRN":
            continue
        # One summer week (Jun-Aug).
        if rng.random() < 0.8:
            y = start.year + 1 if start.month > 8 else start.year
            add(t, date(y, rng.randint(6, 8), rng.randint(1, 24)), 7, "vacation")
        # Holiday-adjacent block for wish-listed holidays (the conflict generator).
        for h in hol_dates:
            if rng.random() < 0.18:
                add(t, h - timedelta(days=rng.randint(0, 3)), rng.randint(3, 6), "holiday-adjacent")
        # Scattered single days.
        for _ in range(rng.randint(1, 4)):
            add(t, start + timedelta(days=rng.randint(0, (end - start).days)), 1, "personal")
    return sorted(reqs, key=lambda r: r["start"])


# ---------------------------------------------------------------- capacity ---
def capacity_check(techs: list[Tech]) -> dict:
    demand = 0
    for day_type, n_days in (("weekday", 5), ("weekend", 2)):
        for shift in SHIFTS:
            demand += sum(COVERAGE[day_type][shift].values()) * n_days
    # 0.9 FTE of 12h shifts on an 80h/2wk basis ~ 3 shifts/wk; PT ~ 2; PRN ~ 1 if called.
    supply = sum(3 if t.employment == "FT" else 2 if t.employment == "PT" else 1 for t in techs)
    return {"weekly_shift_demand": demand, "weekly_shift_supply": supply,
            "slack_shifts": supply - demand, "slack_pct": round(100 * (supply - demand) / supply, 1)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--start", default="2026-12-01")
    ap.add_argument("--months", type=int, default=13)
    args = ap.parse_args()

    rng = random.Random(args.seed)
    start = date.fromisoformat(args.start)
    y, m = divmod(start.month - 1 + args.months, 12)
    end = date(start.year + y, m + 1, 1) - timedelta(days=1)

    holidays = [h for yr in range(start.year, end.year + 1) for h in federal_holidays(yr)
                if start.isoformat() <= h["date"] <= end.isoformat()]
    techs = make_techs(rng, start)
    reqs = make_pto_requests(rng, techs, start, end, holidays)

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "techs.json").write_text(json.dumps([asdict(t) for t in techs], indent=2))
    (DATA_DIR / "holidays.json").write_text(json.dumps(holidays, indent=2))
    (DATA_DIR / "pto_requests.json").write_text(json.dumps(reqs, indent=2))
    (DATA_DIR / "coverage_rules.json").write_text(json.dumps({"shifts": SHIFTS, "minimums": COVERAGE}, indent=2))
    with open(DATA_DIR / "techs.csv", "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "name", "credential", "home_dept", "competencies", "employment", "fte",
                    "shift_pref", "weekend_group", "holiday_group", "seniority_rank", "hire_date",
                    "pto_balance_hours", "holiday_off_wishlist", "constraints"])
        for t in techs:
            w.writerow([t.id, t.name, t.credential, t.home_dept,
                        "; ".join(f"{d}:{lvl}" for d, lvl in t.competencies.items()),
                        t.employment, t.fte, t.shift_pref, t.weekend_group, t.holiday_group,
                        t.seniority_rank, t.hire_date, t.pto_balance_hours,
                        "; ".join(t.holiday_off_wishlist), "; ".join(t.constraints)])

    print(f"Horizon {start} -> {end}: {len(techs)} techs, {len(holidays)} holidays, {len(reqs)} PTO requests")
    for h in holidays:
        print(f"  {h['date']} {h['weekday']:<9} {h['name']}{'  *major' if h['major'] else ''}")
    print("Capacity:", capacity_check(techs))


if __name__ == "__main__":
    main()
