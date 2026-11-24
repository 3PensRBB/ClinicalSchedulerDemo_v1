"""Fast-change tools for the scheduler: call-out replacement, swaps, late PTO,
staff changes. Each works on a Schedule in place; call on ``sched.copy()``
for a what-if preview."""

from __future__ import annotations

from datetime import date, timedelta

from .pto import _bench_cap_conflicts, check_request
from .solver import repair_window
from .world import FORBIDDEN_TURNS, SHIFT_PREF_CODE, Schedule, World, daterange, period_days

SHIFT_NAME = {"D": "Day", "E": "Evening", "N": "Night"}


def _run_length(sched: Schedule, tid: str, d: date) -> int:
    """Consecutive working days through d if tid also worked d."""
    n, x = 1, d - timedelta(days=1)
    while (tid, x) in sched.assign:
        n, x = n + 1, x - timedelta(days=1)
    x = d + timedelta(days=1)
    while (tid, x) in sched.assign:
        n, x = n + 1, x + timedelta(days=1)
    return n


def find_replacement(world: World, sched: Schedule, tid: str, d: date, limit: int = 10,
                     slot: tuple | None = None) -> list[dict]:
    """Rank who can cover tid's shift on d. Hard rules filter; soft rules score."""
    s, b = slot or sched.assign[(tid, d)]
    out = []
    for c in world.techs:
        cid = c["id"]
        if cid == tid or not world.qualified(c, b) or not world.active(c, d) or sched.on_pto(cid, d) \
                or (cid, d) in sched.assign:
            continue
        prev, nxt = sched.assign.get((cid, d - timedelta(days=1))), sched.assign.get((cid, d + timedelta(days=1)))
        if (prev and (prev[0], s) in FORBIDDEN_TURNS) or (nxt and (s, nxt[0]) in FORBIDDEN_TURNS):
            continue
        if _run_length(sched, cid, d) > c["max_consecutive_shifts"]:
            continue
        pdays = period_days(world, d)
        worked = sum(1 for x in pdays if (cid, x) in sched.assign)
        cap = round(c["shifts_per_week"] * len(pdays) / 7)
        score, why = 100, []
        if worked + 1 > cap:
            if c["employment"] == "PRN":
                continue
            score -= 40
            why.append(f"overtime ({worked + 1}/{cap} shifts this pay period)")
        else:
            why.append(f"straight time ({worked + 1}/{cap})")
        if c["employment"] == "PRN":
            score -= 10
            why.append("PRN float")
        pref = SHIFT_PREF_CODE.get(c["shift_pref"])
        if pref and pref != s:
            score -= 15
            why.append(f"prefers {c['shift_pref'].lower()}")
        if d.weekday() >= 5 and d not in world.holidays and not world.weekend_on(c, d) and c["employment"] != "PRN":
            score -= 20
            why.append("off-group weekend")
        if not world.holiday_on(c, d):
            major = world.holidays[d]["major"]
            score -= 35 if major else 25
            why.append(f"holiday group {c['holiday_group']} has {world.holidays[d]['name']} off")
        pc = world.personal_conflicts(c, d, s)
        if pc:
            score -= 30
            why.append("conflicts: " + "; ".join(pc))
        if b != c["home_dept"] and c["employment"] != "PRN":
            score -= 5
            why.append(f"cross-covers from {c['home_dept']}")
        pickups = sum(1 for e in sched.log if e.get("action") == "callout" and e.get("replacement") == cid)
        if pickups:
            score -= 3 * pickups
            why.append(f"already picked up {pickups} call-out shift(s)")
        out.append({"tech": cid, "name": c["name"], "home": c["home_dept"], "employment": c["employment"],
                    "score": score, "why": why})
    return sorted(out, key=lambda r: (-r["score"], world.by_id[r["tech"]]["seniority_rank"]))[:limit]


def apply_callout(world: World, sched: Schedule, tid: str, d: date, replacement: str | None) -> dict:
    s, b = sched.assign.pop((tid, d))
    sched.source.pop((tid, d), None)
    entry = {"action": "callout", "date": d.isoformat(), "tech": tid, "shift": s, "bench": b,
             "replacement": replacement}
    if replacement:
        sched.assign[(replacement, d)] = (s, b)
        sched.source[(replacement, d)] = "callout"
    sched.log.append(entry)
    return entry


def swap(world: World, sched: Schedule, a: str, da: date, b: str, db: date) -> list[dict]:
    """Trade a's shift on da with b's shift on db (b may be off on da, a off on db).
    Applies in place and returns hard-rule violations involving a or b."""
    from .validate import violations
    sa, sb = sched.assign.pop((a, da), None), sched.assign.pop((b, db), None)
    if sa:
        sched.assign[(b, da)] = sa
        sched.source[(b, da)] = "swap"
    if sb:
        sched.assign[(a, db)] = sb
        sched.source[(a, db)] = "swap"
    lo, hi = min(da, db) - timedelta(days=14), max(da, db) + timedelta(days=14)
    v = violations(world, sched, lo, hi, techs=[a, b])
    v += [x for x in violations(world, sched, min(da, db), max(da, db)) if x["rule"] == "coverage"]
    sched.log.append({"action": "swap", "a": a, "da": da.isoformat(), "b": b, "db": db.isoformat(),
                      "violations": len(v)})
    return v


def request_pto(world: World, sched: Schedule, tid: str, d0: date, d1: date, *, force: bool = False) -> dict:
    """Late PTO request against a published schedule: check policy + coverage, then
    remove the shifts and repair the affected weeks with minimal changes."""
    t = world.by_id[tid]
    days = set(daterange(d0, d1))
    caps = _bench_cap_conflicts(world, sched, t, days)
    ok, detail, _ = check_request(world, sched, tid, d0, d1)
    res = {"tech": tid, "start": d0.isoformat(), "end": d1.isoformat(),
           "bench_limit_days": sorted(x.isoformat() for x in caps), "coverage_ok": ok,
           "coverage_gaps": [f"{s[0]} {s[1]} {s[2]}" for s in detail[:5]]}
    if (caps or not ok) and not force:
        res["status"] = "blocked"
        return res
    sched.pto[tid] |= days
    removed = []
    for d in sorted(days):
        a = sched.assign.pop((tid, d), None)
        if a:
            removed.append({"tech": tid, "date": d.isoformat(), "before": "-".join(a), "after": "PTO"})
    r, diff = repair_window(world, sched, d0 - timedelta(days=d0.weekday()),
                            d1 + timedelta(days=6 - d1.weekday() + 7), reason=f"PTO {tid} {d0}–{d1}")
    res.update(status="approved", removed=removed, changes=sorted(removed + diff, key=lambda x: x["date"]),
               backfilled=len(diff), solve_status=r.status, solve_seconds=round(r.seconds, 2))
    return res


def suggest_dates(world: World, sched: Schedule, tid: str, d0: date, d1: date, weeks: int = 8) -> list[dict]:
    """Nearest alternative windows of the same length that pass policy + coverage."""
    t, n, out = world.by_id[tid], (d1 - d0).days, []
    for k in range(1, weeks + 1):
        for sign in (1, -1):
            a = d0 + timedelta(weeks=k * sign)
            b = a + timedelta(days=n)
            if a < world.start or b > world.end or any(sched.on_pto(tid, x) for x in daterange(a, b)):
                continue
            if _bench_cap_conflicts(world, sched, t, set(daterange(a, b))):
                continue
            ok, _, _ = check_request(world, sched, tid, a, b)
            if ok:
                out.append({"start": a.isoformat(), "end": b.isoformat()})
        if len(out) >= 3:
            break
    return out[:3]


def resign(world: World, sched: Schedule, tid: str, effective: date, *, chunk_weeks: int = 4,
           time_limit: float = 5.0) -> dict:
    """Last day worked is effective - 1. Re-solve the rest of the horizon in chunks,
    keeping everyone else's published shifts wherever possible."""
    t = world.by_id[tid]
    t["end_date"] = (effective - timedelta(days=1)).isoformat()
    removed = [k for k in sched.assign if k[0] == tid and k[1] >= effective]
    for k in removed:
        del sched.assign[k]
    sched.pto[tid] = {d for d in sched.pto.get(tid, set()) if d < effective}
    diff, secs, d = [], 0.0, effective
    while d <= world.end:
        e = min(world.end, d + timedelta(weeks=chunk_weeks) - timedelta(days=1))
        r, part = repair_window(world, sched, d, e, time_limit=time_limit,
                                reason=f"{tid} resigned effective {effective}")
        diff += part
        secs += r.seconds
        d = e + timedelta(days=1)
    # Safety net (as in generate_schedule): re-solve +/- 2 weeks around any gap left.
    from .validate import violations
    for v in [v for v in violations(world, sched, effective) if v["rule"] == "coverage"]:
        r, part = repair_window(world, sched, v["date"] - timedelta(days=14), v["date"] + timedelta(days=14),
                                time_limit=max(time_limit, 5.0), reason=f"auto-repair gap {v['date']}")
        diff += part
        secs += r.seconds
    others = [x for x in diff if x["tech"] != tid]
    return {"tech": tid, "effective": effective.isoformat(), "shifts_removed": len(removed),
            "changes_to_others": len(others), "changes": others, "solve_seconds": round(secs, 1)}


def describe(world: World, sched: Schedule, tid: str, d: date) -> str:
    a = sched.assign.get((tid, d))
    return f"{world.by_id[tid]['name']} {d:%a %b %d}: " + (f"{SHIFT_NAME[a[0]]} / {a[1]}" if a else "off")
