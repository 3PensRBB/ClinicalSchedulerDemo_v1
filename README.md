# Clinical Lab Scheduler Demo

A demo scheduler for 28 clinical lab technologists across four separate benches: Hematology, Chemistry, Microbiology and Blood Bank. It covers three 8-hour shifts and US federal holidays, with alternating holiday groups and vacation decided by seniority.

- **PRD:** [`docs/PRD.md`](docs/PRD.md)
- **Scenario results:** [`docs/demo_results.md`](docs/demo_results.md)

## Deploy on Vercel

The repo deploys as-is with Vercel's FastAPI preset:

- **`app.py`** (repo root) exports the FastAPI `app`. That's the entry point Vercel looks for. It serves the API under `/api/*`.
- **`public/`** is the front end: plain HTML, CSS and JS with no build step. Vercel serves it from its CDN.
- **`requirements.txt`** lists only the runtime dependencies (`fastapi`, `ortools`), about 235 MB installed, within the 500 MB function limit.
- **`vercel.json`** sets `maxDuration` to 120 s and keeps tests, docs and the Streamlit UI out of the function bundle.

Import the repo in Vercel (or redeploy). No environment variables are needed. Optionally set `SOLVER_WORKERS` (default 8).

**How state works:** the published 13-month schedule ships in `data/schedule.json` and `public/data/bundle.json`. Each browser keeps its own edits (call-outs, swaps, approved PTO, staff changes) as a small *delta* in localStorage and sends it with each API call. The server is stateless: it rebuilds published + delta, runs the action, and returns a patch. "Discard my changes" returns to the published schedule.

## Run locally

```bash
pip install -r requirements-dev.txt
uvicorn app:app --reload             # web app at http://localhost:8000
pytest tests/                        # 14 tests (engine + API)
```

To rebuild the data and the 13-month schedule, and re-run scenarios S1–S6 (about 1 minute). This also refreshes `public/data/bundle.json`:

```bash
python3 scripts/run_demo.py all
```

The original Streamlit UI is still available for local use:

```bash
streamlit run streamlit_ui/app.py
```

## API

All endpoints take a JSON body with an optional `delta` (see `engine/service.py`).

| Endpoint | What it does | Typical time (1 vCPU) |
|---|---|---|
| `GET /api/health` | Status and horizon | < 0.1 s |
| `POST /api/summary` | Rule check and metrics for published + delta | < 0.1 s |
| `POST /api/callout/candidates` | Ranked replacements for `{tech, date}` | < 0.1 s |
| `POST /api/callout/apply` | Patch that assigns `replacement` | < 0.1 s |
| `POST /api/swap` | Validates `{a, da, b, db}`; returns violations and a patch | < 0.1 s |
| `POST /api/pto/preview` | Policy and coverage check, repair, or alternative dates for `{tech, start, end, force}` | about 1 s |
| `POST /api/resign` | Re-plans the rest of the horizon for `{tech, effective}` | about 20 s |

## Layout

| Path | What it does |
|---|---|
| `app.py` | FastAPI app (Vercel entry point) |
| `public/` | Web front end: `index.html`, `app.js`, `styles.css`, `data/bundle.json` |
| `engine/world.py` | Domain model, calendar, 2-week pay periods, `Schedule` container |
| `engine/solver.py` | OR-Tools CP-SAT model: generate (rolling chunks with look-ahead), repair (hinted), feasibility |
| `engine/pto.py` | Seniority-ordered PTO decisions: bench limits, holiday blackout, holiday rotation, coverage check |
| `engine/validate.py` | Hard-rule checker, coverage table, fairness metrics |
| `engine/tools.py` | Call-out finder, swap validator, late PTO and date suggestions, resignation re-plan |
| `engine/service.py` | Stateless delta/patch layer used by the API |
| `sim/generate_roster.py` | Synthetic data: 28 profiles, holidays with working groups, PTO requests, coverage rules |
| `scripts/run_demo.py` | Builds everything and runs PRD scenarios S1–S6 |
| `scripts/export_static.py` | Writes `public/data/bundle.json` from the published schedule |
| `streamlit_ui/app.py` | Optional local Streamlit UI |

The staffing minimums, extra-staff caps and PTO policy are in `data/coverage_rules.json`, generated from the constants at the top of `sim/generate_roster.py`. All people in the data are fictional.
