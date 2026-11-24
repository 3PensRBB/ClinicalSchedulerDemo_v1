"""Export the published schedule as one compact JSON bundle for the web front end.

    python3 scripts/export_static.py     # writes public/data/bundle.json

Per tech the schedule is a list with one code per day: "" = off, else shift
letter + bench index (e.g. "N3" = Night, Blood Bank).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.validate import summary  # noqa: E402
from engine.world import BENCHES, DATA_DIR, Schedule, World, daterange  # noqa: E402

OUT = ROOT / "public" / "data" / "bundle.json"


def export(world: World | None = None, sched: Schedule | None = None) -> Path:
    world = world or World.load()
    sched = sched or Schedule.load(DATA_DIR / "schedule.json")
    days = list(daterange(world.start, world.end))
    grid = {}
    for t in world.techs:
        row = []
        for d in days:
            a = sched.assign.get((t["id"], d))
            row.append(f"{a[0]}{BENCHES.index(a[1])}" if a else "")
        grid[t["id"]] = row
    bundle = {
        "start": world.start.isoformat(), "end": world.end.isoformat(), "benches": BENCHES,
        "shifts": world.shifts, "minimums": world.minimums, "max_extra": world.max_extra,
        "pto_policy": world.pto_policy, "techs": world.techs,
        "holidays": [world.holidays[d] for d in sorted(world.holidays)],
        "grid": grid,
        "pto": {t: sorted(d.isoformat() for d in ds) for t, ds in sched.pto.items() if ds},
        "decisions": sched.decisions, "summary": summary(world, sched),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(bundle, separators=(",", ":")))
    return OUT


if __name__ == "__main__":
    p = export()
    print(f"wrote {p} ({p.stat().st_size // 1024} KB)")
