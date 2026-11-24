"""Seniority-ordered PTO approval with coverage feasibility checks.

Policy (demo defaults, see PRD §4.3):
  1. Pending requests are decided in seniority order (rank 1 = most senior).
  2. A request needs enough PTO balance.
  3. Approving it must not create a coverage gap that would not exist otherwise,
     checked with the CP-SAT model in feasibility mode over the affected weeks.
  4. Bench PTO limit: at most N techs from one home bench off per day (1 in
     the blackout window around a major holiday).
  5. Holiday rotation outranks vacation: on a holiday the tech's group works,
     only that group (plus PRN) can cover. Days that fail a rule are dropped
     and the rest is approved ("partial"), with the reason spelled out.
"""

from __future__ import annotations

from datetime import date, timedelta

from .solver import solve_window
from .world import Schedule, World, daterange, week_start


def _window(world: World, d0: date, d1: date):
    return max(world.start, week_start(d0)), min(world.end, week_start(d1) + timedelta(days=6))


def _shortage(world, sched, w0, w1):
    r = solve_window(world, w0, w1, sched, mode="feasibility", time_limit=3.0)
    return sum(s[3] for s in r.shortages), r.shortages


def check_request(world: World, sched: Schedule, tid: str, d0: date, d1: date, cache: dict | None = None):
    """Return (ok_dates, blocked_shortages, baseline) for adding PTO [d0, d1] for tid."""
    w0, w1 = _window(world, d0, d1)
    cache = {} if cache is None else cache
    key = (w0, frozenset((k, frozenset(v)) for k, v in sched.pto.items() if v))
    if key not in cache:
        cache[key] = _shortage(world, sched, w0, w1)[0]
    base = cache[key]
    trial = sched.copy()
    trial.pto[tid] |= set(daterange(d0, d1))
    short, detail = _shortage(world, trial, w0, w1)
    return short <= base, detail, base


def _bench_cap_conflicts(world: World, sched: Schedule, t: dict, days: set) -> dict:
    """Days where the tech's home bench is already at its PTO limit -> who is off."""
    out = {}
    peers = [p for p in world.techs if p["home_dept"] == t["home_dept"] and p["id"] != t["id"]]
    for d in days:
        off = [p for p in peers if sched.on_pto(p["id"], d)]
        if len(off) >= world.pto_cap(d):
            out[d] = off
    return out


def _fmt_days(ds) -> str:
    ds = sorted(ds)
    return f"{ds[0]:%b %d}" if len(ds) == 1 else f"{ds[0]:%b %d}–{ds[-1]:%b %d}"


def decide_requests(world: World, sched: Schedule, progress=None) -> list[dict]:
    pending = [r for r in world.requests if r["status"] == "pending"]
    pending.sort(key=lambda r: (world.by_id[r["tech_id"]]["seniority_rank"], r["submitted"], r["id"]))
    used: dict[str, int] = {}
    cache: dict = {}
    decisions = []
    for i, r in enumerate(pending):
        t = world.by_id[r["tech_id"]]
        tid, d0, d1 = t["id"], date.fromisoformat(r["start"]), date.fromisoformat(r["end"])
        rank, first = t["seniority_rank"], t["name"].split()[0]
        have = t["pto_balance_hours"] - used.get(tid, 0)
        dec = {"request": r["id"], "tech": tid, "name": t["name"], "bench": t["home_dept"],
               "start": r["start"], "end": r["end"], "type": r["type"], "seniority_rank": rank,
               "hours": r["hours"]}
        granted = set(daterange(d0, d1))
        notes = []
        if r["hours"] > have:
            granted = set()
            notes.append(f"Insufficient PTO balance: needs {r['hours']} h, {have} h left.")

        # 1) Bench PTO limit (tighter in holiday blackout windows).
        if granted:
            conflicts = _bench_cap_conflicts(world, sched, t, granted)
            if conflicts:
                who = sorted({p["id"]: p for v in conflicts.values() for p in v}.values(),
                             key=lambda p: p["seniority_rank"])
                whos = ", ".join(f"{p['name']} (#{p['seniority_rank']})" for p in who)
                hol = {world.blackout[d] for d in conflicts if d in world.blackout}
                lim = f"{t['home_dept']} is at its {'holiday-week ' if hol else ''}PTO limit on " \
                      f"{_fmt_days(conflicts)}: more senior {whos} already approved"
                granted -= set(conflicts)
                notes.append(lim + ".")

        # 2) Holiday rotation: the working group covers its holiday.
        if granted:
            hol = {d for d in granted if d in world.holidays and world.holiday_on(t, d)
                   and t["employment"] != "PRN"}
            for d in sorted(hol):
                ok, _, _ = check_request(world, sched, tid, d, d, cache)
                if not ok:
                    granted.discard(d)
                    notes.append(f"{world.holidays[d]['name']} {d:%b %d}: holiday group {t['holiday_group']} "
                                 f"works it this year and can't cover without {first}.")

        # 3) Coverage feasibility for what's left (CP-SAT, affected weeks).
        if granted:
            for a, b in _runs(sorted(granted)):
                ok, detail, _ = check_request(world, sched, tid, a, b, cache)
                if not ok:
                    granted -= set(daterange(a, b))
                    gaps = "; ".join(f"{s[0]:%b %d} {s[1]} {s[2]}" for s in detail[:2])
                    notes.append(f"{_fmt_days([a, b])} would leave a coverage gap ({gaps}).")

        full = len(granted) == (d1 - d0).days + 1
        if full:
            dec.update(status="approved", reason=f"Approved in seniority order (#{rank}); coverage holds.")
        elif granted:
            dec.update(status="partial", reason=f"Approved {_fmt_days(granted) if len(_runs(sorted(granted))) == 1 else f'{len(granted)} of {(d1 - d0).days + 1} days'}. " + " ".join(notes))
        else:
            dec.update(status="denied", reason=" ".join(notes))
        if granted:
            sched.pto[tid] |= granted
            used[tid] = used.get(tid, 0) + _hours(t, granted)
        dec["granted_days"] = len(granted)
        decisions.append(dec)
        if progress:
            progress(i + 1, len(pending), dec)
    sched.decisions = decisions
    return decisions


def _off_group(world, t, d):
    return not world.holiday_on(t, d)


def _hours(t, days):
    return round(8 * sum(1 for d in days if d.weekday() < 5) * t["shifts_per_week"] / 5)


def _runs(ds):
    out, a, prev = [], ds[0], ds[0]
    for d in ds[1:]:
        if (d - prev).days > 1:
            out.append((a, prev))
            a = d
        prev = d
    out.append((a, prev))
    return out
