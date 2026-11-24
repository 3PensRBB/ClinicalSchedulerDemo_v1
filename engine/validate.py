"""Rule checker and metrics. Independent of the solver, so it can audit any
schedule: solver output, manual edits, swaps and call-out fixes."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta

from .world import (BENCHES, FORBIDDEN_TURNS, SHIFT_CODES, SHIFT_PREF_CODE, Schedule, World, daterange,
                    period_days, period_start)


def coverage(world: World, sched: Schedule, d0: date | None = None, d1: date | None = None):
    """Rows of {date, shift, bench, need, have} for every slot."""
    idx = sched.by_day()
    rows = []
    for d in daterange(d0 or world.start, d1 or world.end):
        for s in SHIFT_CODES:
            for b in BENCHES:
                need = world.demand(d, s, b)
                have = len(idx.get((d, s, b), []))
                if need or have:
                    rows.append({"date": d, "shift": s, "bench": b, "need": need, "have": have})
    return rows


def violations(world: World, sched: Schedule, d0: date | None = None, d1: date | None = None,
               techs: list[str] | None = None) -> list[dict]:
    """Hard-rule violations. Empty list == publishable."""
    d0, d1 = d0 or world.start, d1 or world.end
    out = []
    if techs is None:
        for r in coverage(world, sched, d0, d1):
            if r["have"] < r["need"]:
                out.append({"rule": "coverage", "date": r["date"], "detail":
                            f"{r['bench']} {r['shift']}: {r['have']}/{r['need']} staffed"})
    tids = techs or [t["id"] for t in world.techs]
    for tid in tids:
        t = world.by_id[tid]
        mine = {d: v for (x, d), v in sched.assign.items() if x == tid}
        for d, (s, b) in mine.items():
            if not (d0 <= d <= d1):
                continue
            if not world.qualified(t, b):
                out.append({"rule": "competency", "date": d, "tech": tid,
                            "detail": f"{t['name']} not signed off for {b}"})
            if sched.on_pto(tid, d):
                out.append({"rule": "pto", "date": d, "tech": tid, "detail": f"{t['name']} is on approved PTO"})
            if not world.active(t, d):
                out.append({"rule": "inactive", "date": d, "tech": tid, "detail": f"{t['name']} not employed"})
            nxt = mine.get(d + timedelta(days=1))
            if nxt and (s, nxt[0]) in FORBIDDEN_TURNS:
                out.append({"rule": "rest", "date": d, "tech": tid,
                            "detail": f"{t['name']} {s}→{nxt[0]} next day (<8 h rest)"})
        # consecutive days
        run, prev = 0, None
        for d in sorted(mine):
            run = run + 1 if prev and (d - prev).days == 1 else 1
            prev = d
            if run > t["max_consecutive_shifts"] and d0 <= d <= d1:
                out.append({"rule": "consecutive", "date": d, "tech": tid,
                            "detail": f"{t['name']} {run} days in a row (max {t['max_consecutive_shifts']})"})
        periods = Counter(period_start(d) for d in mine)
        for ps, n in periods.items():
            cap = round(t["shifts_per_week"] * len(period_days(world, ps)) / 7)
            if n > cap and d0 <= ps + timedelta(days=13) and ps <= d1:
                out.append({"rule": "hours", "date": ps, "tech": tid,
                            "detail": f"{t['name']} {n} shifts in pay period {ps} (FTE max {cap}) → overtime"})
    return sorted(out, key=lambda v: (v["date"], v["rule"]))


def tech_metrics(world: World, sched: Schedule, d0: date | None = None, d1: date | None = None) -> list[dict]:
    """Fairness and preference metrics per tech."""
    d0, d1 = d0 or world.start, d1 or world.end
    agg = defaultdict(Counter)
    for (tid, d), (s, b) in sched.assign.items():
        if not (d0 <= d <= d1):
            continue
        t, c = world.by_id[tid], agg[tid]
        c["shifts"] += 1
        c[s] += 1
        if d.weekday() >= 5:
            c["weekend_shifts"] += 1
        if d in world.holidays:
            c["holidays"] += 1
            if world.holidays[d]["major"]:
                c["major_holidays"] += 1
        pref = SHIFT_PREF_CODE.get(t["shift_pref"])
        if pref and s != pref:
            c["off_pref_shift"] += 1
        if b != t["home_dept"] and t["employment"] != "PRN":
            c["cross_bench"] += 1
        if world.personal_conflicts(t, d, s):
            c["personal_conflicts"] += 1
        if d.weekday() >= 5 and d not in world.holidays and not world.weekend_on(t, d) \
                and t["employment"] != "PRN":
            c["off_group_weekend"] += 1
        if not world.holiday_on(t, d):
            c["off_group_holiday"] += 1
    weeks = (d1 - d0).days / 7
    rows = []
    for t in world.techs:
        c = agg[t["id"]]
        pto_days = sum(1 for d in sched.pto.get(t["id"], ()) if d0 <= d <= d1)
        target = 0 if t["employment"] == "PRN" else round(t["shifts_per_week"] * weeks) - pto_days
        rows.append({"id": t["id"], "name": t["name"], "bench": t["home_dept"], "employment": t["employment"],
                     "shift_pref": t["shift_pref"], "shifts": c["shifts"], "target": max(target, 0),
                     "D": c["D"], "E": c["E"], "N": c["N"], "weekend_shifts": c["weekend_shifts"],
                     "holidays": c["holidays"], "major_holidays": c["major_holidays"], "pto_days": pto_days,
                     "off_pref_shift": c["off_pref_shift"], "cross_bench": c["cross_bench"],
                     "personal_conflicts": c["personal_conflicts"], "off_group_weekend": c["off_group_weekend"],
                     "off_group_holiday": c["off_group_holiday"]})
    return rows


def summary(world: World, sched: Schedule) -> dict:
    v = violations(world, sched)
    m = tech_metrics(world, sched)
    total = sum(r["shifts"] for r in m)
    soft = sum(r["off_pref_shift"] + r["personal_conflicts"] + r["off_group_weekend"] + r["off_group_holiday"]
               for r in m)
    ft = [r for r in m if r["employment"] == "FT"]
    wk = [r["weekend_shifts"] for r in ft]
    return {"assignments": total, "hard_violations": len(v),
            "coverage_gaps": sum(1 for x in v if x["rule"] == "coverage"),
            "preference_satisfaction_pct": round(100 * (1 - soft / max(total, 1)), 1),
            "ft_weekend_spread": (min(wk), max(wk)) if wk else (0, 0),
            "prn_shifts": sum(r["shifts"] for r in m if r["employment"] == "PRN"),
            "ft_hours_met_pct": round(100 * sum(min(r["shifts"], r["target"]) for r in ft) /
                                      max(1, sum(r["target"] for r in ft)), 1)}
