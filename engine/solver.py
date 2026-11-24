"""CP-SAT scheduling model.

One function, ``solve_window``, serves three uses:
  * full generation (rolling 4-week chunks across the horizon),
  * repair mode (re-solve a date window, minimising changes to what's published),
  * PTO feasibility checks (hard rules only, minimise coverage shortage).
Assignments outside the window are treated as fixed context, so rest,
consecutive-day and weekly-hour rules hold across window boundaries.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from ortools.sat.python import cp_model

from .world import (BENCHES, FORBIDDEN_TURNS, SHIFT_CODES, SHIFT_PREF_CODE, Schedule, World, daterange,
                    period_days, period_start, week_start)

# Soft-constraint weights (penalty per occurrence). Shortage is effectively hard.
W = {
    "shortage": 10_000,
    "weekend_off_group": 70,  # works an off-group weekend (outranks hours)
    "under_hours": 60,        # FT/PT scheduled below FTE target for the week
    "holiday_off_group": 45,  # works a holiday their group has off (x2 for major)
    "personal": 35,           # violates a personal availability limit
    "shift_pref": 25,         # fixed-shift tech placed on another shift
    "prn": 20,                # uses a PRN float
    "stability_keep": 15,     # repair mode: reward keeping a published assignment
    "stability_new": 5,       # repair mode: adding a shift that wasn't published
    "cross_bench": 3,         # works outside home bench
    "extra_offshift": 2,      # staffing above minimum on evening/night
}


@dataclass
class SolveResult:
    assign: dict          # (tid, date) -> (shift, bench), for window days only
    shortages: list       # [(date, shift, bench, missing)]
    status: str
    objective: float
    seconds: float


def solve_window(world: World, d0: date, d1: date, sched: Schedule, *, mode: str = "full",
                 previous: dict | None = None, time_limit: float = 8.0, workers: int = 8) -> SolveResult:
    """mode: 'full' (all soft rules) | 'feasibility' (hard rules, holiday groups enforced)."""
    t0 = time.time()
    m = cp_model.CpModel()
    days = list(daterange(d0, d1))
    in_win = set(days)
    feas = mode == "feasibility"
    obj = []

    def fixed(tid, d):
        """Assignment outside the window (context), else None."""
        return None if d in in_win else sched.assign.get((tid, d))

    x = {}  # (tid, d, s, b) -> var
    for t in world.techs:
        tid = t["id"]
        for d in days:
            if sched.on_pto(tid, d) or not world.active(t, d):
                continue
            if feas and not world.holiday_on(t, d):
                continue  # holiday rotation is binding when deciding PTO
            for s in SHIFT_CODES:
                for b in BENCHES:
                    if world.qualified(t, b):
                        x[(tid, d, s, b)] = m.NewBoolVar(f"x_{tid}_{d}_{s}_{b}")

    by_tds = defaultdict(list)
    by_td = defaultdict(list)
    by_dsb = defaultdict(list)
    for (tid, d, s, b), v in x.items():
        by_tds[(tid, d, s)].append(v)
        by_td[(tid, d)].append(v)
        by_dsb[(d, s, b)].append((tid, v))

    def y(tid, d, s):  # works shift s on day d (var expr or 0/1 constant)
        if d in in_win:
            vs = by_tds.get((tid, d, s))
            return sum(vs) if vs else 0
        a = sched.assign.get((tid, d))
        return 1 if a and a[0] == s else 0

    def w(tid, d):
        if d in in_win:
            vs = by_td.get((tid, d))
            return sum(vs) if vs else 0
        return 1 if sched.assign.get((tid, d)) else 0

    # --- coverage (shortage slack is heavily penalised) ---------------------
    short = {}
    for d in days:
        for s in SHIFT_CODES:
            for b in BENCHES:
                need = world.demand(d, s, b)
                vs = [v for _, v in by_dsb.get((d, s, b), [])]
                cap = need + (0 if feas else world.max_extra[s])
                if need == 0 and not vs:
                    continue
                u = m.NewIntVar(0, need, f"short_{d}_{s}_{b}")
                short[(d, s, b)] = u
                m.Add(sum(vs) + u >= need)
                m.Add(sum(vs) <= cap)
                obj.append((W["shortage"], u))
                if not feas and s in ("E", "N") and vs:
                    extra = m.NewIntVar(0, cap, f"extra_{d}_{s}_{b}")
                    m.Add(extra >= sum(vs) - need)
                    obj.append((W["extra_offshift"], extra))

    for t in world.techs:
        tid, maxc, spw = t["id"], t["max_consecutive_shifts"], t["shifts_per_week"]
        # one shift per day
        for d in days:
            if by_td.get((tid, d)):
                m.Add(sum(by_td[(tid, d)]) <= 1)
        # rest between consecutive days (window edges use fixed context)
        for d in list(daterange(d0 - timedelta(days=1), d1)):
            nd = d + timedelta(days=1)
            if d not in in_win and nd not in in_win:
                continue
            for a, b in FORBIDDEN_TURNS:
                ya, yb = y(tid, d, a), y(tid, nd, b)
                if isinstance(ya, int) and isinstance(yb, int):
                    continue
                m.Add(ya + yb <= 1)
        # max consecutive working days
        span = list(daterange(d0 - timedelta(days=maxc), d1 + timedelta(days=maxc)))
        for i in range(len(span) - maxc):
            win = span[i:i + maxc + 1]
            if not any(d in in_win for d in win):
                continue
            terms = [w(tid, d) for d in win]
            if all(isinstance(z, int) for z in terms):
                continue
            m.Add(sum(terms) <= maxc)
        # Hours are counted per 2-week pay period (hospital 8/80 rule): hard cap at
        # FTE, FT/PT under target penalised. A weekly sanity cap of FTE+1 remains.
        for ws in sorted({week_start(d) for d in days}):
            worked = sum(w(tid, d) for d in daterange(ws, ws + timedelta(days=6)))
            if not isinstance(worked, int):
                m.Add(worked <= spw + 1)
        for ps in sorted({period_start(d) for d in days}):
            pdays = period_days(world, ps)
            worked = sum(w(tid, d) for d in pdays)
            if isinstance(worked, int):
                continue
            # Overtime is on hours WORKED, so the hard cap ignores PTO; PTO hours
            # only lower the target that FT/PT staff should be scheduled to.
            cap = round(spw * len(pdays) / 7)
            m.Add(worked <= cap)
            if not feas and t["employment"] != "PRN":
                pto_days = sum(1 for d in pdays if sched.on_pto(tid, d))
                inactive = sum(1 for d in pdays if not world.active(t, d))
                target = max(0, cap - pto_days - round(inactive * spw / 7))
                if target:
                    under = m.NewIntVar(0, target, f"under_{tid}_{ps}")
                    m.Add(under >= target - worked)
                    obj.append((W["under_hours"], under))

    # --- soft preferences --------------------------------------------------
    if not feas:
        for (tid, d, s, b), v in x.items():
            t = world.by_id[tid]
            cost = 0
            pref = SHIFT_PREF_CODE.get(t["shift_pref"])
            if pref and s != pref:
                cost += W["shift_pref"]
            if t["employment"] == "PRN":
                cost += W["prn"]
            elif b != t["home_dept"]:
                cost += W["cross_bench"]
            if d.weekday() >= 5 and d not in world.holidays and not world.weekend_on(t, d) \
                    and t["employment"] != "PRN":
                cost += W["weekend_off_group"]
            if not world.holiday_on(t, d):
                cost += W["holiday_off_group"] * (2 if world.holidays[d]["major"] else 1)
            cost += W["personal"] * len(world.personal_conflicts(t, d, s))
            if previous is not None:
                if previous.get((tid, d)) == (s, b):
                    cost -= W["stability_keep"]
                elif (tid, d) not in previous:
                    cost += W["stability_new"]
            if cost:
                obj.append((cost, v))

    m.Minimize(sum(c * v for c, v in obj))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit
    solver.parameters.num_workers = workers
    solver.parameters.random_seed = 7
    st = solver.Solve(m)
    status = solver.StatusName(st)
    if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return SolveResult({}, [], status, float("nan"), time.time() - t0)

    assign = {(tid, d): (s, b) for (tid, d, s, b), v in x.items() if solver.Value(v)}
    shortages = [(d, s, b, solver.Value(u)) for (d, s, b), u in short.items() if solver.Value(u)]
    return SolveResult(assign, shortages, status, solver.ObjectiveValue(), time.time() - t0)


def generate_schedule(world: World, sched: Schedule, *, chunk_weeks: int = 4, lookahead_weeks: int = 2,
                      time_limit: float = 8.0, progress=None) -> list[SolveResult]:
    """Fill the horizon in rolling chunks. Each chunk is solved with a look-ahead
    (committed only for its own weeks) so it doesn't starve the next chunk;
    any slot still short afterwards is fixed by a local repair pass."""
    results = []
    d = world.start
    while d <= world.end:
        e = min(world.end, d + timedelta(weeks=chunk_weeks) - timedelta(days=1))
        la = min(world.end, e + timedelta(weeks=lookahead_weeks))
        for k in [k for k in sched.assign if d <= k[1] <= la]:
            del sched.assign[k]
        r = solve_window(world, d, la, sched, time_limit=time_limit)
        commit = {k: v for k, v in r.assign.items() if k[1] <= e}
        sched.assign.update(commit)
        for k in commit:
            sched.source[k] = "solver"
        r.shortages = [x for x in r.shortages if x[0] <= e]
        results.append(r)
        if progress:
            progress(d, e, r)
        d = e + timedelta(days=1)
    # Safety net: re-solve +/- 2 weeks around any remaining shortage.
    from .validate import violations
    for v in [v for v in violations(world, sched) if v["rule"] == "coverage"]:
        repair_window(world, sched, v["date"] - timedelta(days=14), v["date"] + timedelta(days=14),
                      reason=f"auto-repair gap {v['date']} {v['detail']}")
    return results


def repair_window(world: World, sched: Schedule, d0: date, d1: date, *, time_limit: float = 5.0,
                  reason: str = "") -> tuple[SolveResult, list[dict]]:
    """Re-solve [d0, d1] keeping as much of the published schedule as possible. Returns diff."""
    d0, d1 = max(d0, world.start), min(d1, world.end)
    previous = {k: v for k, v in sched.assign.items() if d0 <= k[1] <= d1}
    r = solve_window(world, d0, d1, sched, previous=previous, time_limit=time_limit)
    if not r.assign and r.status not in ("OPTIMAL", "FEASIBLE"):
        return r, []
    diff = []
    for k in set(previous) | set(r.assign):
        a, b = previous.get(k), r.assign.get(k)
        if a != b:
            diff.append({"tech": k[0], "date": k[1].isoformat(),
                         "before": "-".join(a) if a else "off", "after": "-".join(b) if b else "off"})
    for k in previous:
        sched.assign.pop(k, None)
    sched.assign.update(r.assign)
    for k in r.assign:
        if previous.get(k) != r.assign[k]:
            sched.source[k] = "repair"
    sched.log.append({"action": "repair", "window": [d0.isoformat(), d1.isoformat()],
                      "reason": reason, "changes": len(diff)})
    return r, sorted(diff, key=lambda x: (x["date"], x["tech"]))
