# Clinical Lab Scheduler Demo

A demo scheduler for 28 clinical lab technologists across four separate benches: Hematology, Chemistry, Microbiology and Blood Bank. It covers three 8-hour shifts and US federal holidays, with alternating holiday groups and vacation decided by seniority.

- **PRD:** [`docs/PRD.md`](docs/PRD.md)
- **Scenario results:** [`docs/demo_results.md`](docs/demo_results.md)

## Quick start

```bash
pip install -r requirements.txt
python3 scripts/run_demo.py all      # data -> PTO decisions -> 13-month schedule -> scenarios S1-S6 (~1 min)
streamlit run app/app.py             # interactive UI at http://localhost:8501
pytest tests/                        # 8 engine tests
```

## Layout

| Path | What it does |
|---|---|
| `sim/generate_roster.py` | Synthetic data: 28 profiles, federal holidays with working groups, PTO requests, coverage rules |
| `engine/world.py` | Domain model, calendar, 2-week pay periods, `Schedule` container |
| `engine/solver.py` | OR-Tools CP-SAT model: generate (rolling 4-week chunks with look-ahead), repair, feasibility |
| `engine/pto.py` | Seniority-ordered PTO decisions: bench limits, holiday blackout, holiday rotation, coverage check |
| `engine/validate.py` | Hard-rule checker, coverage table, fairness metrics |
| `engine/tools.py` | Call-out replacement finder, swap validator, late PTO and date suggestions, resignation re-plan |
| `scripts/run_demo.py` | Builds everything and runs PRD scenarios S1–S6 |
| `app/app.py` | Streamlit UI: dashboard, schedule grid, holidays, PTO, call-out, swap, staff change, fairness, profiles, audit |

The staffing minimums, extra-staff caps and PTO policy are in `data/coverage_rules.json`, generated from the constants at the top of `sim/generate_roster.py`. All people in the data are fictional.
