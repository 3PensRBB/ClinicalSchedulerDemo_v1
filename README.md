# Clinical Lab Scheduler Demo

A demo scheduler for 28 clinical lab technologists across Hematology, Chemistry, Microbiology and Blood Bank, covering day and night shifts with US federal holiday coverage.

- **PRD and demo scenarios:** [`docs/PRD.md`](docs/PRD.md)
- **Synthetic data generator** (standard library only, deterministic):

```bash
python3 sim/generate_roster.py --seed 42 --start 2026-12-01 --months 13
```

The generator writes `data/techs.json`, `data/techs.csv`, `data/holidays.json`, `data/pto_requests.json` and `data/coverage_rules.json`. All people are fictional.
