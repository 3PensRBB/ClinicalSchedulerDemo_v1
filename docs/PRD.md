# PRD: Clinical Lab Staff Scheduler (Demo v1)

| | |
|---|---|
| **Status** | Draft for review |
| **Date** | 2026-11-24 |
| **Planning horizon in demo** | 2026-12-01 → 2027-12-31 (13 months) |
| **Synthetic data** | `sim/generate_roster.py` → `data/` (all people fictional) |

---

## 1. Problem

One clinical lab tech also acts as the department scheduler. They build schedules for **28 lab technologists** across four benches — **Hematology and Chemistry (Core Lab), Microbiology, and Blood Bank** — on **day and night shifts**, many months in advance, and must guarantee coverage on every US federal holiday (Thanksgiving, Christmas, New Year's Day, July 4th, and the rest).

Today this is done by hand, usually in a spreadsheet. The work is slow and easy to get wrong because the scheduler has to keep all of this in mind at once:

- **Minimum coverage** for each bench on each shift, which differs on weekdays, weekends and holidays.
- **Competencies**: who is actually signed off to work Blood Bank or Micro, not just who is available.
- **Employment terms**: FTE, part-time and PRN hours, and rules about maximum consecutive shifts and turning from nights to days.
- **Fairness** across weekends, nights and holidays, along with each person's history ("I worked Christmas last year").
- **Vacation requests** that pile up around the same weeks, plus personal limits such as classes, childcare and custody days.
- **Changes after publishing**: call-outs, swaps, a resignation, a new hire still in training.

A single change, such as approving one vacation, can set off a series of manual checks. There's no quick way to answer "if I approve this, is Christmas night in Blood Bank still covered?"

## 2. Goals and non-goals

**Goals**
1. Cut the time to build a multi-month schedule from days of work to minutes of review.
2. Guarantee that no published schedule falls below minimum coverage or puts someone on a bench they aren't signed off for.
3. Make changes fast: answer a call-out, swap or PTO decision in under 2 minutes, showing its effect on coverage before it's applied.
4. Make fairness visible and defensible (holiday rotation, weekend and night load), so decisions are easy to explain to staff.
5. Respect individual profiles (preferences, constraints, wish lists) as far as coverage allows, and explain any request that can't be met.

**Non-goals for the demo**
- Payroll, timekeeping or HRIS integration (Kronos/UKG, Workday). We only stub the export.
- Union contract engines and state labor-law compliance beyond a small set of configurable rules.
- Instrument or workload-based demand forecasting. Demand is fixed minimums per bench and shift.
- A self-service mobile app for staff. Staff-facing views are a later phase.

## 3. Personas

| Persona | Needs |
|---|---|
| **Scheduler (primary)**: a senior MLS who schedules in addition to bench work | Build months ahead quickly, fix gaps fast, defend decisions, stop carrying the rules in their head |
| **Lab manager / supervisor** | Approve PTO, see coverage risk and overtime, check fairness |
| **Bench tech** | Know their schedule early, request time off and swaps, see that holidays rotate fairly |

## 4. Domain model and rules

### 4.1 Entities
- **Tech profile**: id, name, credential (MLS/MLT/SBB), home department, **competency per bench** (`primary` / `competent` / `training`), employment (FT/PT/PRN) and FTE, shift preference (Days/Nights/Rotating), **weekend group** (A/B), **holiday group** (1/2), hire date and seniority rank, PTO balance, holiday wish list, holidays worked last year, max consecutive shifts, on-call eligibility, and personal constraints.
- **Shift**: Day 07:00–19:30 and Night 19:00–07:30, each 12 h paid (including handoff overlap). *An 8-hour, 3-shift model is a config option. See open questions.*
- **Coverage rule**: minimum headcount per bench × shift × day type (weekday / weekend / holiday).
- **Holiday**: name, **actual date** (the date the lab staffs), observed date (for payroll), and whether it's a major holiday.
- **PTO request**: tech, date range, type, submitted date, status.
- **Assignment**: tech × date × shift × bench, plus its source (`template`, `solver`, `manual`, `swap`).

### 4.2 Hard constraints (the schedule must never break these)
1. Coverage on every bench, shift and day meets the minimums in `data/coverage_rules.json`.
2. A tech is assigned only to benches where they are `primary` or `competent`. `training` counts only as an extra person on top of the minimum.
3. No more than one shift per tech per 24 h, no day shift right after a night shift, and at least 2 days off after a run of nights.
4. No more than the tech's `max_consecutive_shifts` in a row.
5. Approved PTO is never scheduled over.
6. Scheduled hours stay at or below FTE hours for each pay period. Going over counts as overtime and needs a manager flag.

### 4.3 Soft constraints (weighted; the solver keeps penalties low)
- Shift preference (days/nights) and personal constraints.
- Weekend rotation by A/B group, and evenly spread weekend counts.
- **Holiday rotation**: group 1 and group 2 alternate the major holidays each year (for example, whoever worked Christmas last year has Christmas off this year), with ties broken by wish list and then seniority.
- Even spread of nights for Rotating staff.
- Pending PTO requests granted in **seniority order within the same submission window**, or first-come-first-served (configurable).
- Fewer changes to an already-published schedule ("stability").

### 4.4 Demo coverage numbers

| Day type | Shift | Heme | Chem | Micro | BB | Total |
|---|---|---|---|---|---|---|
| Weekday | Day | 2 | 2 | 2 | 1 | 7 |
| Weekday | Night | 1 | 1 | 0* | 1 | 3 |
| Weekend / holiday | Day | 1 | 1 | 1 | 1 | 4 |
| Weekend / holiday | Night | 1 | 1 | 0* | 1 | 3 |

\* Micro has no night bench. Core Lab techs load blood cultures overnight, and Micro has an on-call tech.

**Capacity check from the generator:** demand is 64 shifts per week and supply is about 74 shifts per week, so slack is **about 10 shifts (13.5%)**. That slack is what has to absorb vacation, sick calls and training, which is why holiday weeks are tight.

## 5. Simulated scenario

### 5.1 How the 28 profiles are made
`python3 sim/generate_roster.py --seed 42 --start 2026-12-01 --months 13`

The generator is deterministic for a given seed, so the demo is reproducible. Change `--seed` to get a new cohort.

| Attribute | How it's simulated |
|---|---|
| Home dept | Fixed headcount: Heme 7, Chem 8, Micro 6, BB 5, plus 2 PRN floats competent on every bench |
| Cross-training | Probability by home bench (Heme↔Chem high, Micro low), with a small chance of `training` status |
| Employment | About 85% FT (0.9 FTE = 3×12 h/week) and 15% PT (0.6). The two floats are PRN |
| Shift preference | A dedicated night crew of 3 each in Heme, Chem and BB (9 total). Everyone else is about 75% Days and 25% Rotating |
| Seniority | Years of service drawn from a triangular distribution (0.3–30, mode 6), then ranked |
| PTO balance | 40–120 h, plus 40 h at 5+ years and another 40 h at 10+ years |
| Holiday group | Alternates 1/2, with last year's worked holidays filled in to match |
| Wish list | 2 of the 4 major holidays |
| Constraints | 0–2 drawn from a realistic set (class days, childcare, custody, carpool, 3-on/4-off) |
| PTO requests | ~80% ask for a summer week, ~18% per major holiday ask for a block around it (**this produces the conflicts**), and 1–4 single personal days each. Seed 42 gives 110 requests |
| Holidays | Computed from the federal rules (nth-weekday, plus OPM's observed-date rule) for every year in the horizon |

**Output files:** `data/techs.json`, `data/techs.csv`, `data/holidays.json`, `data/pto_requests.json`, `data/coverage_rules.json`.

### 5.2 Holidays in the horizon (seed run)
| Date (staffed) | Day | Holiday |
|---|---|---|
| 2026-12-25 | Fri | **Christmas Day** |
| 2027-01-01 | Fri | **New Year's Day** |
| 2027-01-18 | Mon | MLK Jr. Day |
| 2027-02-15 | Mon | Presidents Day |
| 2027-05-31 | Mon | Memorial Day |
| 2027-06-19 | Sat | Juneteenth (observed Fri 6/18) |
| 2027-07-04 | Sun | **Independence Day** (observed Mon 7/5) |
| 2027-09-06 | Mon | Labor Day |
| 2027-10-11 | Mon | Columbus / Indigenous Peoples' Day |
| 2027-11-11 | Thu | Veterans Day |
| 2027-11-25 | Thu | **Thanksgiving** |
| 2027-12-25 | Sat | **Christmas Day** (observed Fri 12/24) |

### 5.3 Demo scenarios (scripted, all from seed 42)

**S1: Build the 13-month schedule.** Generate December 2026 through December 2027 in one action. Show that there are zero coverage gaps, how preferences were honored, and a fairness dashboard of weekends, nights and holidays per tech.

**S2: The Christmas 2026 crunch.** Seven people asked for time off around Christmas, and **four of them are in Chemistry** (T08 Elena Clarke, T09 Owen Kowalski, T11 Caleb Okafor, T12 Owen Kim). Chemistry needs 1 Day and 1 Night person on 12/25.
- T08 and T12 are in holiday group 2, worked Christmas 2025, and **get it off** under the rotation rule.
- T09 and T11 are in group 1. One Day Chem slot remains. T11 is less senior (rank 20 against T09's rank 18), so T11 is proposed to work and offered first pick of New Year's off.
- The system explains each decision in one line and shows which requests are denied, partly approved, or approved with a swap suggestion.

**S3: A call-out on New Year's night.** A Blood Bank night tech calls in sick on 2027-01-01. The **replacement finder** ranks candidates who are qualified for BB, not on PTO, rested (no day shift that day, inside consecutive limits), under their FTE hours, and who score well on fairness (fewest holidays worked). PRN floats rank above paying overtime. Fixed in one click, with a log entry.

**S4: Approve a summer vacation.** The manager approves a pending July week. The scheduler sees a **coverage preview** first (green/amber/red per day), then the solver repairs only the affected week, keeping changes to the published schedule to a minimum.

**S5: Staff change.** A Chemistry FT resigns effective March 1. Re-solve March through December, show the new slack (about 7 shifts/week), the weeks that become fragile, and an "open shifts" list to give to PRN staff or to justify a backfill requisition.

**S6: Swap request.** Two techs ask to trade a Saturday. The validator checks competency, rest and hours, and approves or explains which rule the swap breaks.

## 6. Ways to speed up the scheduler

### Option A: Rotation template ("master pattern")
Give each position a repeating cycle, for example a 2-week Pitman (2-2-3) or a 6-week pattern for 12-hour shifts, along with A/B weekend groups and a 1/2 holiday-group rotation. Rolling the pattern forward produces months of schedule instantly. The scheduler then handles only the exceptions: PTO and holidays.
- **Pros:** Simple, predictable, and staff can work out their own schedule. Building a horizon takes seconds. Easy to trust.
- **Cons:** Rigid. PTO and cross-bench gaps still have to be fixed by hand, and it ignores individual preferences beyond choosing a pattern.

### Option B: Constraint optimizer (OR-Tools CP-SAT)
Model the hard and soft rules in §4. The solver produces the full horizon, then works in **repair mode** for changes: it pins everything outside the affected window and minimizes changes inside it.
- **Pros:** Finds coverage-safe answers that a person would miss, balances fairness mathematically, and handles cross-training well. Every result can be explained in terms of the rules.
- **Cons:** Needs careful tuning of weights. Generating the whole horizon can feel like a "black box" unless results are explained and pinned assignments are honored.

### Option C: Interactive "copilot" layer (works with A or B)
A grid editor (techs × days) with **live validation**: a coverage heatmap, red flags on broken rules, and fairness counters that update on every drag. On top of that:
- **Find replacement** (ranked candidates, as in S3).
- **What-if sandbox**: try approving PTO or a resignation, see the impact, then commit or throw it away.
- **Swap validator** and request inbox.
- *(Optional)* **Natural-language commands** through an LLM that calls the same functions, for example "Give Priya Foster Dec 20–27 off and fix Chemistry coverage" or "Who can cover BB night on 1/1?". The LLM never edits the schedule directly. It proposes an action, and the rules engine checks it.

### Recommendation
**A + B + C, layered.** A template is the backbone, so schedules stay predictable and staff-readable. The CP-SAT solver fills gaps and repairs changes. The interactive layer is where the scheduler spends their time. For the demo, build B and C first (they make the strongest impression), and treat the template as the solver's starting point or a soft "stick to the pattern" preference.

## 7. Functional requirements

| # | Requirement | Priority |
|---|---|---|
| F1 | Load or generate roster, holidays, PTO and coverage rules (synthetic generator; CSV import) | MVP |
| F2 | Edit tech profiles: competencies, FTE, preferences, constraints, groups | MVP |
| F3 | Auto-generate a schedule for any horizon (1–13 months) with hard constraints guaranteed | MVP |
| F4 | Coverage heatmap per bench, shift and day, with gap and over-coverage flags | MVP |
| F5 | Live rule validation for manual edits (drag and drop) | MVP |
| F6 | PTO request inbox with a coverage preview and approve/deny; batch approval by seniority | MVP |
| F7 | Holiday planner view: all holidays in the horizon, who works and who's off, group rotation, last-year history | MVP |
| F8 | Replacement finder for call-outs, with ranked and explained candidates | MVP |
| F9 | Repair mode: re-solve a date window while keeping changes to a minimum | MVP |
| F10 | Fairness dashboard: weekends, nights, holidays and preference hits per tech | MVP |
| F11 | Export to CSV/Excel/PDF (by bench, by person) and an audit log of changes | MVP |
| F12 | What-if scenarios (sandbox copy) | Phase 2 |
| F13 | Swap requests and validator | Phase 2 |
| F14 | Natural-language command bar (LLM → tool calls → validator) | Phase 2 |
| F15 | Staff self-service portal and notifications | Phase 3 |

**Non-functional:** a full 13-month solve in under 60 s and a repair in under 5 s on a laptop. Fully deterministic given seed and inputs. No real PHI or PII in the demo.

## 8. UX sketch
1. **Dashboard:** horizon summary, coverage health by month, open gaps, pending requests, next holiday risk.
2. **Schedule grid:** rows are techs grouped by bench, columns are days. Cells show D/N/PTO/H with bench color. Filters by bench, shift and group. A coverage strip sits along the bottom.
3. **Holiday planner:** a matrix of holidays × techs showing worked last year, wish list and this year's assignment.
4. **Requests:** PTO and swap inbox, each with an impact preview.
5. **Profiles:** roster table with an editable profile drawer.
6. **Fairness:** per-tech bar charts against the team average.

## 9. Demo walkthrough (~10 minutes)
1. *(1 min)* The problem: 28 techs, 4 benches, 2 shifts, 12 holidays, 110 requests. Show the raw generated roster.
2. *(2 min)* **S1:** one click builds 13 months. Show the coverage heatmap is all green and the fairness dashboard.
3. *(2 min)* **S2:** the Christmas crunch, with explained decisions.
4. *(2 min)* **S3:** the New Year's night call-out, fixed in about 30 seconds.
5. *(2 min)* **S4/S5:** approve a summer vacation, then simulate a resignation and show the repair diff.
6. *(1 min)* Close on time saved, fairness and auditability.

## 10. Success metrics

Baselines are assumptions to confirm by interviewing the real scheduler.

| Metric | Baseline (assumed) | Demo target |
|---|---|---|
| Time to build a monthly schedule | ~8–15 h/month | < 30 min of review |
| Time to resolve a call-out | 15–45 min of phone calls and checking | < 2 min to a qualified candidate |
| Coverage gaps in a published schedule | occasional | 0 (hard constraint) |
| Holiday rotation complaints | — | 100% of decisions come with a stated reason |
| Preference satisfaction | unknown | ≥ 85% of soft preferences met |
| Spread of weekend counts | unknown | max − min ≤ 2 per quarter (FT) |

## 11. Demo architecture (proposed)
- **Python** backend: the data generator (done), plus a scheduling engine using **OR-Tools CP-SAT** and a rules validator.
- **UI:** **Streamlit** for the quickest demo, or **FastAPI with a React grid** if the demo needs to feel like a real product.
- **Storage:** JSON/SQLite. No external services.
- **Optional LLM command bar:** the Claude API with tool use, where the tools are `find_replacement`, `preview_pto`, `apply_change` and `resolve_window`.

```
data/ (generated) ──► engine/model.py (CP-SAT) ──► engine/validate.py ──► ui/ (grid, heatmap, inbox)
                          ▲                            ▲
                     repair window                manual edits / LLM proposals
```

## 12. Milestones
1. **M0 (done):** synthetic data generator and this PRD.
2. **M1:** solver for one month, hard constraints only, plus CSV export.
3. **M2:** soft constraints (preferences, weekends, holiday rotation) and the 13-month horizon.
4. **M3:** UI with grid, heatmap and holiday planner.
5. **M4:** PTO inbox with preview, replacement finder and repair mode, covering scenarios S2–S5.
6. **M5 (stretch):** what-if sandbox, swap validator and natural-language command bar.

## 13. Open questions and assumptions
1. **Shift model:** 12-hour day/night (assumed) or 8-hour day/evening/night? Many hospital labs run 3×8.
2. Real **minimum staffing** for each bench and shift, and whether Micro has night or weekend coverage.
3. Is **Blood Bank** separate from Microbiology? This draft treats them as separate benches.
4. Holiday policy: alternating groups (assumed) or a points/bidding system? Do Christmas Eve and New Year's Eve count as holidays?
5. PTO priority: seniority, first-come-first-served, or a blackout window around holidays?
6. Pay-period length, and overtime and holiday pay rules.
7. Should PRN availability be entered by the PRN staff themselves?
8. Is there a union contract or other rules (for example, a guaranteed weekend off per month)?
