# PRD: Clinical Lab Staff Scheduler (Demo v1)

| | |
|---|---|
| **Status** | v1.1. Decisions locked and demo built (M0–M4 done) |
| **Date** | 2026-11-24 |
| **Planning horizon in demo** | Mon 2026-11-30 → Sun 2028-01-02 (13 months, whole weeks) |
| **Run it** | `python3 scripts/run_demo.py all` builds everything; `streamlit run app/app.py` opens the UI |
| **Synthetic data** | `sim/generate_roster.py` → `data/` (all people fictional) |

---

## 1. Problem

One clinical lab tech also acts as the department scheduler. They build schedules for **28 lab technologists** across four separate benches — **Hematology, Chemistry, Microbiology and Blood Bank** — on **three 8-hour shifts (Day, Evening, Night)**, many months in advance. They must also guarantee coverage on every US federal holiday (Thanksgiving, Christmas, New Year's Day, July 4th, and the rest).

Today this is done by hand. The scheduler has to keep all of this in mind at once:

- Minimum coverage for each bench and shift.
- Who is signed off on which bench.
- Employment terms and rest rules.
- Fairness across weekends and holidays.
- Vacation requests that pile up in the same weeks.
- Personal availability limits.
- Changes after publishing: call-outs, swaps and resignations.

A single change, such as approving one vacation, sets off a series of manual checks. There's no quick way to answer "if I approve this, is Christmas night in Blood Bank still covered?"

## 2. Goals and non-goals

**Goals**
1. Cut the time to build a multi-month schedule from days of work to minutes of review.
2. Guarantee that no published schedule falls below minimum coverage or puts someone on a bench they aren't signed off for.
3. Make changes fast: a call-out, swap or PTO decision in under 2 minutes, with its effect shown before it's committed.
4. Make fairness visible and defensible (holiday rotation, weekend load, seniority), with a one-line reason for every decision.
5. Respect individual profiles (preferences, limits, wish lists) as far as coverage allows.

**Non-goals for the demo:** payroll or HRIS integration, union contract engines, workload-based demand forecasting, and a self-service mobile app for staff.

## 3. Personas

| Persona | Needs |
|---|---|
| **Scheduler (primary)**: a senior MLS who schedules in addition to bench work | Build months ahead quickly, fix gaps fast, defend decisions |
| **Lab manager / supervisor** | Approve PTO and overrides, see coverage risk and overtime, check fairness |
| **Bench tech** | Know their schedule early, see that holidays and vacation are decided fairly |

## 4. Decisions (confirmed 2026-11-24)

| Topic | Decision |
|---|---|
| Shift model | **Three 8-hour shifts**: Day 07:00–15:30, Evening 15:00–23:30, Night 23:00–07:30 (30-minute handoff overlap) |
| Minimum staffing | The assumed minimums in §5.3, kept in `data/coverage_rules.json` so they can be changed later |
| Benches | Hematology, Chemistry, **Microbiology and Blood Bank are separate** benches |
| Holiday policy | **Alternating groups.** Groups 1 and 2 alternate holiday by holiday, and each holiday flips every year |
| Vacation priority | **Seniority** (rank 1 = longest service). Can be revisited later |

## 5. Domain model and rules

### 5.1 Entities
- **Tech profile**: id, name, credential, home bench, competency per bench (`primary` / `competent` / `training`), employment (FT 1.0 = 5 shifts/week, PT 0.6 = 3, PRN ≤ 2), shift preference (Days / Evenings / Nights / Rotating), weekend group (A/B), holiday group (1/2), hire date and seniority rank, PTO balance, holiday wish list, holidays worked last year, max consecutive shifts, and personal limits.
- **Coverage rule**: minimum headcount per bench × shift × day type, plus how many extra staff are allowed above it.
- **Holiday**: the **actual date** (the date the lab staffs), the observed date (for payroll), whether it's a major holiday, and the **working group**.
- **PTO request**, **assignment** (tech × date × shift × bench, with its source), **audit log**.

### 5.2 Hard rules (no published schedule may break these)
1. Coverage meets the minimums on every bench, shift and day.
2. Techs work only on benches where they are `primary` or `competent`.
3. At most one shift per day, and at least 8 hours' rest. That rules out Evening→Day, Night→Day and Night→Evening on the next day.
4. No more than `max_consecutive_shifts` days in a row (5–6).
5. Scheduled shifts stay within FTE **per 2-week, 80-hour pay period** (the hospital 8/80 rule). Anything over is overtime and gets flagged. A weekly sanity cap of FTE+1 also applies.
6. Approved PTO is never scheduled over.

### 5.3 Minimum staffing

| Day type | Shift | Heme | Chem | Micro | BB | Total |
|---|---|---|---|---|---|---|
| Weekday | Day | 2 | 2 | 2 | 2 | 8 |
| Weekday | Evening | 1 | 1 | 1 | 1 | 4 |
| Weekday | Night | 1 | 1 | 0* | 1 | 3 |
| Weekend / holiday | Day | 1 | 1 | 1 | 1 | 4 |
| Weekend / holiday | Evening | 1 | 1 | 0 | 1 | 3 |
| Weekend / holiday | Night | 1 | 1 | 0* | 1 | 3 |

\* Micro has no night bench. Core Lab techs load blood cultures overnight, and Micro has an on-call tech.

Extra staff allowed above the minimum: +2 on Day, +1 on Evening or Night. **Demand is 95 shifts per week and FT/PT supply is 122 (22% slack)**, plus PRN. The slack is what absorbs PTO and sick calls and gives full-timers their hours.

### 5.4 Soft rules (weighted penalties in the solver)

| Weight | Rule |
|---|---|
| 70 | Working an off-group weekend (A works weekends in even weeks, B in odd weeks) |
| 60 | FT/PT scheduled below their FTE for the pay period (PTO counts toward hours) |
| 45 (×2 for major) | Working a holiday your group has off |
| 35 | Breaking a personal limit (class day, childcare, custody week, second job…) |
| 25 | Fixed-shift tech placed on another shift |
| 20 | Using a PRN float |
| 15 / 5 | Repair mode: keeping a published shift (reward) / adding a new shift |
| 3 | Working outside your home bench |
| 2 | Staffing above minimum on Evening or Night (extra hands go to Days first) |

### 5.5 PTO policy (seniority)
1. Pending requests are decided in **seniority order**.
2. The tech must have enough PTO balance.
3. **Bench PTO limit:** at most **2** people from the same home bench off on any day. During the **blackout window** (±3 days around Thanksgiving, Christmas, New Year's Day and July 4th) the limit is **1**.
4. **Holiday rotation outranks vacation:** on a holiday the tech's group works, only that group (plus PRN) can cover it.
5. Approving the request must not create a coverage gap. This is checked with the solver in feasibility mode over the affected weeks.
6. Days that fail a rule are dropped and the rest is approved (**partial**). Every decision comes with a one-line reason naming the rule and the more senior person involved.

## 6. Simulated scenario

### 6.1 How the 28 profiles are made
`python3 sim/generate_roster.py --seed 42 --start 2026-12-01 --months 13` produces the same output every time for a given seed.

| Attribute | How it's simulated |
|---|---|
| Home bench | Heme 7, Chem 7, Micro 5, BB 7, plus 2 PRN floats competent on every bench |
| Shift crews | Fixed **night crew** of 2 each in Heme, Chem and BB. Fixed **evening crew** of 2 each in Heme, Chem and BB, plus 1 in Micro. 3 Rotating techs. Everyone else works Days |
| Employment | 22 FT (5 × 8 h/week), 4 PT (3 × 8 h/week, all day-shift), 2 PRN (≤ 2 shifts/week) |
| Cross-training | Probability by home bench (Heme↔Chem high, Micro low), with a small chance of `training` status |
| Weekend / holiday groups | Alternate **within each bench-shift crew**, so every crew has someone on every weekend and every holiday |
| Seniority, PTO | Years of service drawn from a triangular distribution (0.3–30) and ranked. PTO is 40–120 h, plus 40 h at 5 years and another 40 h at 10 years |
| Personal limits | 0–2 per tech from a realistic set: no Tuesday days (class), no Sunday nights (childcare), every other Wednesday off (custody), no Thursday evenings (second job), and so on |
| PTO requests | 113 in total: a summer week (80% of techs), blocks around holidays (Christmas 35%, Thanksgiving 22%, July 4th 18%, New Year's 15%) and 1–4 personal days each |

### 6.2 Holidays and working groups (seed 42)

| Date (staffed) | Holiday | Group working |
|---|---|---|
| Fri 2026-12-25 | **Christmas Day** | 1 (group 2 worked Christmas 2025) |
| Fri 2027-01-01 | **New Year's Day** | 2 |
| Mon 2027-01-18 | MLK Jr. Day | 1 |
| Mon 2027-02-15 | Presidents Day | 2 |
| Mon 2027-05-31 | Memorial Day | 1 |
| Sat 2027-06-19 | Juneteenth (observed Fri 6/18) | 2 |
| Sun 2027-07-04 | **Independence Day** (observed Mon 7/5) | 1 |
| Mon 2027-09-06 | Labor Day | 2 |
| Mon 2027-10-11 | Columbus / Indigenous Peoples' Day | 1 |
| Thu 2027-11-11 | Veterans Day | 2 |
| Thu 2027-11-25 | **Thanksgiving** | 1 |
| Sat 2027-12-25 | **Christmas Day** (observed Fri 12/24) | **2**, flipped from 2026 |
| Sat 2028-01-01 | **New Year's Day** | **1**, flipped from 2027 |

## 7. The solution: three layers

1. **Rotation backbone.** A/B weekend groups and 1/2 holiday groups give everyone a predictable pattern. Fixed shift crews (Days, Evenings, Nights) match how 8-hour labs actually run.
2. **Optimizer (OR-Tools CP-SAT).** It encodes §5.2 as hard constraints and §5.4 as weighted penalties.
   - **Generate:** solves the horizon in 4-week chunks, each with a 2-week look-ahead that isn't committed, so one chunk doesn't use up the next chunk's hours. A local repair pass then fixes any slot still left short.
   - **Repair:** re-solves only a date window. Everything outside it is fixed, and a stability reward keeps published shifts in place.
   - **Feasibility:** answers "does approving this PTO create a gap?" for seniority-ordered decisions.
3. **Interactive copilot (Streamlit).** Every change is previewed on a sandbox copy, shows its diff and key numbers, and is only committed when the scheduler clicks.
   - **Replacement finder:** hard filters first (competency, PTO, rest, consecutive days, PRN cap), then a score with a stated reason for each candidate.
   - **Swap validator** and **date suggester** for blocked vacation requests.

## 8. Functional requirements: status

| # | Requirement | Status |
|---|---|---|
| F1 | Generate or load roster, holidays, PTO and coverage rules | ✅ `sim/generate_roster.py` |
| F2 | View tech profiles | ✅ Profiles tab (editing → Phase 2) |
| F3 | Auto-generate any horizon with hard rules guaranteed | ✅ `engine/solver.py` |
| F4 | Coverage health per bench, shift and day | ✅ Dashboard and coverage strip |
| F5 | Validate every change against all hard rules | ✅ `engine/validate.py` |
| F6 | PTO inbox: seniority batch, preview impact, approve or override, suggest dates | ✅ `engine/pto.py`, PTO tab |
| F7 | Holiday planner: groups, last year, this year | ✅ Holidays tab |
| F8 | Call-out replacement finder | ✅ Call-out tab |
| F9 | Repair mode with minimal changes | ✅ `repair_window` |
| F10 | Fairness dashboard: shift mix, weekends, holidays, preferences | ✅ Fairness tab (charts and table) |
| F11 | Export and audit log | ✅ `data/schedule.json` and Audit log tab (CSV/PDF export → Phase 2) |
| F12 | What-if sandbox | ✅ Every action previews on a copy first |
| F13 | Swap validator | ✅ Swap tab |
| F14 | Natural-language command bar (LLM → tool calls → validator) | ⏳ Phase 2 |
| F15 | Staff self-service portal and notifications | ⏳ Phase 3 |

## 9. Demo results (seed 42; full output in `docs/demo_results.md`)

| Scenario | Result |
|---|---|
| **S1: build 13 months** | 113 PTO requests decided in 36 s (99 approved, 10 partly approved, 4 denied). **6,709 shifts solved in about 20 s** (laptop-class CPU, every chunk optimal). **0 coverage gaps and 0 hard-rule violations**. **97.3%** of soft preferences met and **100%** of FT hours met. Only 10 PRN shifts used |
| **S2: Christmas 2026 crunch** | 11 holiday-week requests. For example, Chemistry's Caleb Okafor (#14) was approved, so Priya Foster (#25) was denied with the reason *"Chemistry is at its holiday-week PTO limit on Dec 23–26: more senior Caleb Okafor (#14) already approved."* Owen Thompson (#18) and Grace Ivanova (#7) were partly approved. Christmas Day was worked **only by group 1** |
| **S3: New Year's night call-out** (Blood Bank) | Ranked list in under 1 ms. PRN floats come first; FT staff from the off group are scored down with stated reasons. Applied with 0 violations |
| **S4: late summer vacation** | Aug 2–6 was **blocked** by the bench limit on Aug 6. The tool offered 3 open windows in 0.7 s. Jul 26–30 was approved, and the repair took 0.3 s with **1** backfill change |
| **S5: resignation** (Chem Days FT, effective 3/1/27) | Remaining 43 weeks re-solved in **9 s**. 136 changes for other staff, **0 gaps**. PRN use rose from 10 to 60, which is the signal to backfill the position |
| **S6: swaps** | A valid Saturday-for-weekday trade was approved. An invalid one was blocked with three reasons: not signed off for Blood Bank, overtime, and Night→Day rest |

## 10. Success metrics

Baselines are assumptions to confirm with the real scheduler.

| Metric | Baseline (assumed) | Demo result |
|---|---|---|
| Time to build a 13-month schedule | ~8–15 h per month by hand | about 1 minute of compute, then review |
| Time to resolve a call-out | 15–45 min of calls and checking | ranked, qualified list instantly |
| Coverage gaps in a published schedule | occasional | 0 |
| PTO decisions with a stated reason | rare | 100% |
| Soft preferences met | unknown | 97.3% |
| Off-group weekend shifts | unknown | close to 0 (fixed by pay-period accounting) |

## 11. Architecture (as built)

```
sim/generate_roster.py ──► data/*.json ──► engine/world.py (model, calendar, pay periods)
                                              │
     engine/pto.py (seniority decisions) ◄────┤
     engine/solver.py (CP-SAT: generate / repair / feasibility)
     engine/validate.py (hard rules, metrics)   engine/tools.py (call-out, swap, PTO, resign)
                                              │
            scripts/run_demo.py (S1–S6 → docs/demo_results.md)    app/app.py (Streamlit UI)
```
Python 3.11+, `ortools`, `streamlit`, `pandas` and `altair`. Tests: `pytest tests/` (8 tests: holiday dates, determinism, no violations, PTO caps, holiday rotation, call-out, swap, repair).

## 12. Milestones
- **M0 ✅** PRD and synthetic data generator
- **M1 ✅** CP-SAT solver with hard rules
- **M2 ✅** Soft rules, 13-month rolling horizon, holiday rotation, seniority PTO
- **M3 ✅** UI: dashboard, schedule grid, holiday planner, fairness, profiles, audit log
- **M4 ✅** PTO preview and date suggestions, replacement finder, repair mode, staff change, swap validator
- **M5 (next)** Natural-language command bar, profile editing in the UI, CSV/PDF export by bench and by person, staff self-service

## 13. Open questions (for later tuning)
1. Validate the minimums in §5.3 with the real lab. In particular: Micro weekend evening coverage, and whether Blood Bank needs 2 on weekend days.
2. Should the blackout window and bench PTO limit be different per bench?
3. Do Christmas Eve and New Year's Eve count as holidays for rotation?
4. Is seniority strictly first, or seniority only within a submission window (for example, requests due 60 days out)?
5. Should PRN staff enter their own availability?
6. Are there union or contract rules, such as a guaranteed weekend off per month or shift differentials?
