"""Stateless service layer for the web API.

The published (base) schedule ships with the app in data/schedule.json. Each
browser keeps its own edits as a *delta* and sends it with every request:

    {"assign":    {"T01|2027-01-01": "N|Blood Bank" | null},   # set / clear a shift
     "pto":       {"T14": ["2027-07-26", ...]},                 # extra approved PTO days
     "end_dates": {"T09": "2027-02-28"}}                        # last day employed

The server rebuilds the current state as base + delta, runs the requested
action on it, and answers with a *patch* in the same shape. The browser merges
the patch into its delta when the user commits. No server-side storage needed.
"""

from __future__ import annotations

import copy
import functools
from datetime import date

from .world import DATA_DIR, Schedule, World

SCHEDULE = DATA_DIR / "schedule.json"


@functools.lru_cache(maxsize=1)
def _base() -> tuple[World, Schedule]:
    return World.load(), Schedule.load(SCHEDULE)


def state(delta: dict | None) -> tuple[World, Schedule]:
    """Fresh, mutable copies of base + delta."""
    w0, s0 = _base()
    world = copy.deepcopy(w0)
    world.__post_init__()
    sched = s0.copy()
    delta = delta or {}
    for tid, end in (delta.get("end_dates") or {}).items():
        if tid in world.by_id:
            world.by_id[tid]["end_date"] = end
    for tid, days in (delta.get("pto") or {}).items():
        sched.pto[tid] |= {date.fromisoformat(d) for d in days}
    for key, val in (delta.get("assign") or {}).items():
        tid, d = key.split("|")
        k = (tid, date.fromisoformat(d))
        if val:
            s, b = val.split("|")
            sched.assign[k] = (s, b)
        else:
            sched.assign.pop(k, None)
    return world, sched


def patch(before_w: World, before: Schedule, after_w: World, after: Schedule) -> dict:
    """Difference between two states, in delta format."""
    out = {"assign": {}, "pto": {}, "end_dates": {}}
    for k in set(before.assign) | set(after.assign):
        a, b = before.assign.get(k), after.assign.get(k)
        if a != b:
            out["assign"][f"{k[0]}|{k[1].isoformat()}"] = "|".join(b) if b else None
    for tid in set(before.pto) | set(after.pto):
        added = after.pto.get(tid, set()) - before.pto.get(tid, set())
        if added:
            out["pto"][tid] = sorted(d.isoformat() for d in added)
    for t in after_w.techs:
        e = t.get("end_date")
        if e and e != before_w.by_id[t["id"]].get("end_date"):
            out["end_dates"][t["id"]] = e
    return out


def snapshot(world: World, sched: Schedule) -> tuple[World, Schedule]:
    w = copy.deepcopy(world)
    w.__post_init__()
    return w, sched.copy()
