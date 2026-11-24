import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.pto import decide_requests  # noqa: E402
from engine.solver import generate_schedule  # noqa: E402
from engine.tools import apply_callout, find_replacement, request_pto, swap  # noqa: E402
from engine.validate import summary, violations  # noqa: E402
from engine.world import Schedule, World, daterange  # noqa: E402
from sim.generate_roster import federal_holidays, generate  # noqa: E402


def test_federal_holidays_2027():
    h = {x["name"]: (x["date"], x["observed_date"]) for x in federal_holidays(2027)}
    assert h["Martin Luther King Jr. Day"][0] == "2027-01-18"
    assert h["Memorial Day"][0] == "2027-05-31"
    assert h["Independence Day"] == ("2027-07-04", "2027-07-05")
    assert h["Juneteenth National Independence Day"] == ("2027-06-19", "2027-06-18")
    assert h["Labor Day"][0] == "2027-09-06"
    assert h["Thanksgiving Day"][0] == "2027-11-25"
    assert h["Christmas Day"] == ("2027-12-25", "2027-12-24")


def test_generator_shape_and_determinism(tmp_path):
    a = generate(seed=7, months=3, out_dir=tmp_path / "a")
    b = generate(seed=7, months=3, out_dir=tmp_path / "b")
    assert len(a["techs"]) == 28
    assert (tmp_path / "a" / "techs.json").read_text() == (tmp_path / "b" / "techs.json").read_text()
    # every night-staffed bench has night crew in both holiday groups
    for bench in ("Hematology", "Chemistry", "Blood Bank"):
        groups = {t.holiday_group for t in a["techs"] if t.home_dept == bench and t.shift_pref == "Nights"}
        assert groups == {"1", "2"}


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    generate(seed=42, start="2026-12-01", months=2, out_dir=d)
    world, sched = World.load(d), Schedule()
    decide_requests(world, sched)
    generate_schedule(world, sched, time_limit=5)
    return world, sched


def test_schedule_has_no_hard_violations(built):
    world, sched = built
    assert violations(world, sched) == []
    assert summary(world, sched)["coverage_gaps"] == 0


def test_pto_respected_and_bench_caps(built):
    world, sched = built
    for (tid, d) in sched.assign:
        assert not sched.on_pto(tid, d)
    for d in daterange(world.start, world.end):
        for bench in {t["home_dept"] for t in world.techs}:
            off = [t for t in world.techs if t["home_dept"] == bench and sched.on_pto(t["id"], d)]
            assert len(off) <= world.pto_cap(d), (d, bench)


def test_holiday_group_rotation(built):
    world, sched = built
    xmas = date(2026, 12, 25)
    working = world.holidays[xmas]["working_group"]
    for (tid, d) in sched.assign:
        t = world.by_id[tid]
        if d == xmas and t["employment"] != "PRN":
            assert t["holiday_group"] == working


def test_callout_replacement_is_valid(built):
    world, base = built
    sched = base.copy()
    d = date(2027, 1, 1)
    tid = next(t for (t, dd) in sorted(sched.assign) if dd == d)
    cands = find_replacement(world, sched, tid, d)
    assert cands, "expected at least one replacement"
    apply_callout(world, sched, tid, d, cands[0]["tech"])
    assert violations(world, sched, d - timedelta(days=7), d + timedelta(days=7)) == []


def test_invalid_swap_is_flagged(built):
    world, base = built
    sched = base.copy()
    (a, d), (s, b) = next(((k, v) for k, v in sorted(sched.assign.items()) if v[1] == "Blood Bank"))
    unq = next(t for t in world.techs if not world.qualified(t, "Blood Bank") and (t["id"], d) not in sched.assign)
    v = swap(world, sched, a, d, unq["id"], d)
    assert any(x["rule"] == "competency" for x in v)


def test_late_pto_repair_keeps_coverage(built):
    world, base = built
    sched = base.copy()
    t = next(t for t in world.techs if t["employment"] == "FT" and t["home_dept"] == "Microbiology")
    d0 = date(2027, 1, 11)
    res = request_pto(world, sched, t["id"], d0, d0 + timedelta(days=2), force=True)
    assert res["status"] == "approved"
    assert [v for v in violations(world, sched) if v["rule"] == "coverage"] == []
