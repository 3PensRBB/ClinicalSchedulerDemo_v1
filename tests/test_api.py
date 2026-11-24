"""Web API tests against the published schedule in data/schedule.json."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402

client = TestClient(app)


def test_health_and_static_frontend():
    assert client.get("/api/health").json()["techs"] == 28
    assert client.get("/").status_code == 200
    assert client.get("/data/bundle.json").json()["start"] == "2026-11-30"


def test_published_schedule_is_clean():
    j = client.post("/api/summary", json={}).json()
    assert j["violation_count"] == 0 and j["summary"]["coverage_gaps"] == 0


def test_callout_round_trip_through_delta():
    c = client.post("/api/callout/candidates", json={"tech": "T25", "date": "2027-01-01"}).json()
    assert c["candidates"]
    rep = c["candidates"][0]["tech"]
    a = client.post("/api/callout/apply", json={"tech": "T25", "date": "2027-01-01", "replacement": rep}).json()
    assert a["violations"] == []
    patch = a["patch"]
    assert patch["assign"]["T25|2027-01-01"] is None and patch["assign"][f"{rep}|2027-01-01"]
    # the patch, sent back as the delta, is a valid state
    j = client.post("/api/summary", json={"delta": patch}).json()
    assert j["violation_count"] == 0


def test_blocked_pto_offers_alternatives():
    j = client.post("/api/pto/preview", json={"tech": "T14", "start": "2027-08-02", "end": "2027-08-06"}).json()
    assert j["result"]["status"] == "blocked" and j["alternatives"]


def test_invalid_swap_reports_violations():
    # Blood Bank night shift given to a tech not signed off for Blood Bank
    j = client.post("/api/swap", json={"a": "T25", "da": "2027-01-04", "b": "T16", "db": "2027-01-04"}).json()
    assert j["violations"]


def test_bad_input_is_a_clear_error():
    r = client.post("/api/pto/preview", json={"tech": "T99", "start": "2027-08-02", "end": "2027-08-06"})
    assert r.status_code == 404
