"""Clinical Lab Scheduler: web API + static front end.

Vercel loads the top-level ``app`` (FastAPI preset). Locally:

    uvicorn app:app --reload        # http://localhost:8000

The front end lives in public/ (served from Vercel's CDN). The API is
stateless: every request carries the browser's ``delta`` of edits on top of
the published schedule in data/schedule.json (see engine/service.py).
"""

from __future__ import annotations

import time
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine import service
from engine.tools import apply_callout, describe, find_replacement, request_pto, resign, suggest_dates, swap
from engine.validate import summary, violations

ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Clinical Lab Scheduler API", version="1.0")


class Delta(BaseModel):
    assign: dict[str, str | None] = Field(default_factory=dict)
    pto: dict[str, list[str]] = Field(default_factory=dict)
    end_dates: dict[str, str] = Field(default_factory=dict)


class Req(BaseModel):
    delta: Delta = Field(default_factory=Delta)


class CalloutReq(Req):
    tech: str
    date: date
    replacement: str | None = None


class SwapReq(Req):
    a: str
    da: date
    b: str
    db: date


class PtoReq(Req):
    tech: str
    start: date
    end: date
    force: bool = False


class ResignReq(Req):
    tech: str
    effective: date


def _load(r: Req):
    return service.state(r.delta.model_dump())


def _check_tech(world, *ids):
    for t in ids:
        if t not in world.by_id:
            raise HTTPException(404, f"Unknown tech {t}")


def _vjson(v: list[dict]) -> list[dict]:
    return [{**x, "date": x["date"].isoformat()} for x in v]


@app.get("/api/health")
def health():
    world, sched = service._base()
    return {"ok": True, "techs": len(world.techs), "assignments": len(sched.assign),
            "horizon": [world.start.isoformat(), world.end.isoformat()]}


@app.post("/api/summary")
def api_summary(r: Req):
    world, sched = _load(r)
    v = violations(world, sched)
    return {"summary": summary(world, sched), "violations": _vjson(v[:100]), "violation_count": len(v)}


@app.post("/api/callout/candidates")
def callout_candidates(r: CalloutReq):
    world, sched = _load(r)
    _check_tech(world, r.tech)
    if (r.tech, r.date) not in sched.assign:
        raise HTTPException(400, f"{world.by_id[r.tech]['name']} is not scheduled on {r.date}")
    t0 = time.time()
    cands = find_replacement(world, sched, r.tech, r.date)
    s, b = sched.assign[(r.tech, r.date)]
    return {"slot": {"shift": s, "bench": b}, "who": describe(world, sched, r.tech, r.date),
            "candidates": cands, "ms": round((time.time() - t0) * 1000, 1)}


@app.post("/api/callout/apply")
def callout_apply(r: CalloutReq):
    world, sched = _load(r)
    _check_tech(world, r.tech)
    if (r.tech, r.date) not in sched.assign:
        raise HTTPException(400, "That tech is not scheduled on that date")
    bw, bs = service.snapshot(world, sched)
    apply_callout(world, sched, r.tech, r.date, r.replacement)
    v = violations(world, sched, r.date - timedelta(days=7), r.date + timedelta(days=7))
    return {"patch": service.patch(bw, bs, world, sched), "violations": _vjson(v)}


@app.post("/api/swap")
def api_swap(r: SwapReq):
    world, sched = _load(r)
    _check_tech(world, r.a, r.b)
    before = [describe(world, sched, r.a, r.da), describe(world, sched, r.b, r.db)]
    bw, bs = service.snapshot(world, sched)
    v = swap(world, sched, r.a, r.da, r.b, r.db)
    return {"before": before, "after": [describe(world, sched, r.b, r.da), describe(world, sched, r.a, r.db)],
            "violations": _vjson(v), "patch": service.patch(bw, bs, world, sched)}


@app.post("/api/pto/preview")
def pto_preview(r: PtoReq):
    world, sched = _load(r)
    _check_tech(world, r.tech)
    if r.end < r.start:
        raise HTTPException(400, "End date is before start date")
    if r.start < world.start or r.end > world.end:
        raise HTTPException(400, f"Dates must fall within {world.start} – {world.end}")
    bw, bs = service.snapshot(world, sched)
    res = request_pto(world, sched, r.tech, r.start, r.end, force=r.force)
    out = {"result": res}
    if res["status"] == "blocked":
        out["alternatives"] = suggest_dates(world, bs, r.tech, r.start, r.end)
    else:
        out["patch"] = service.patch(bw, bs, world, sched)
        out["summary"] = summary(world, sched)
    return out


@app.post("/api/resign")
def api_resign(r: ResignReq):
    world, sched = _load(r)
    _check_tech(world, r.tech)
    if not (world.start < r.effective <= world.end):
        raise HTTPException(400, f"Effective date must fall within {world.start} – {world.end}")
    bw, bs = service.snapshot(world, sched)
    res = resign(world, sched, r.tech, r.effective, time_limit=2.0)  # keeps a 43-week re-plan under ~30 s
    return {"result": {k: v for k, v in res.items() if k != "changes"}, "changes": res["changes"][:400],
            "patch": service.patch(bw, bs, world, sched), "summary": summary(world, sched)}


# Static front end (declared after the API routes so /api/* always reaches the function).
app.mount("/", StaticFiles(directory=ROOT / "public", html=True), name="site")
