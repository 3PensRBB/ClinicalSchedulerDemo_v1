"""Clinical Lab Scheduler demo UI.

    streamlit run app/app.py

Loads data/schedule.json (run `python3 scripts/run_demo.py build` first, or use
the Rebuild button). Every change can be previewed in a sandbox copy before it
is committed and saved.
"""

from __future__ import annotations

import copy
import json
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.pto import decide_requests  # noqa: E402
from engine.solver import generate_schedule  # noqa: E402
from engine.tools import (SHIFT_NAME, apply_callout, describe, find_replacement, request_pto, resign,  # noqa: E402
                          suggest_dates, swap)
from engine.validate import coverage, summary, tech_metrics, violations  # noqa: E402
from engine.world import BENCHES, DATA_DIR, SHIFT_CODES, Schedule, World, daterange  # noqa: E402
from sim.generate_roster import generate  # noqa: E402

SCHEDULE = DATA_DIR / "schedule.json"
# Categorical slots 1-3 (validated all-pairs) for the three shifts; status palette for coverage.
SHIFT_COLOR = {"D": "#2a78d6", "E": "#eb6834", "N": "#1baf7a"}
SHIFT_TINT = {"D": "#cde2fb", "E": "#fbd9c9", "N": "#c4ecdc"}
STATUS = {"good": "#0ca30c", "warning": "#fab219", "critical": "#d03b3b"}
BENCH_ABBR = {"Hematology": "H", "Chemistry": "C", "Microbiology": "M", "Blood Bank": "BB"}

st.set_page_config(page_title="Clinical Lab Scheduler", page_icon="🧪", layout="wide")


# ----------------------------------------------------------------- state ---
def rebuild():
    with st.status("Building schedule…", expanded=True) as box:
        generate(seed=st.session_state.get("seed", 42))
        world, sched = World.load(), Schedule()
        bar = st.progress(0.0, text="Deciding PTO requests in seniority order")
        decide_requests(world, sched, progress=lambda i, n, d: bar.progress(i / n, text=f"PTO {i}/{n}: {d['name']}"))
        bar2 = st.progress(0.0, text="Solving schedule")
        total = (world.end - world.start).days + 1
        generate_schedule(world, sched, progress=lambda a, b, r: bar2.progress(
            min(1.0, ((b - world.start).days + 1) / total), text=f"Solved {a} → {b} ({r.status}, {r.seconds:.1f}s)"))
        sched.log.append({"action": "generate", "horizon": [world.start.isoformat(), world.end.isoformat()]})
        sched.save(SCHEDULE)
        box.update(label="Schedule built", state="complete")
    load(force=True)


def load(force=False):
    if force or "world" not in st.session_state:
        if not SCHEDULE.exists():
            return False
        st.session_state.world = World.load()
        st.session_state.sched = Schedule.load(SCHEDULE)
        st.session_state.pop("preview", None)
    return True


def commit(world: World, sched: Schedule, msg: str):
    st.session_state.world, st.session_state.sched = world, sched
    sched.save(SCHEDULE)
    (DATA_DIR / "techs.json").write_text(json.dumps(world.techs, indent=2))
    st.session_state.pop("preview", None)
    st.toast(msg, icon="✅")


# ---------------------------------------------------------------- sidebar ---
with st.sidebar:
    st.title("🧪 Lab Scheduler")
    st.caption("Hematology · Chemistry · Microbiology · Blood Bank — 3 × 8 h shifts")
    st.number_input("Synthetic data seed", value=42, step=1, key="seed")
    if st.button("Rebuild data + schedule", type="primary", use_container_width=True):
        rebuild()

if not load():
    st.info("No schedule yet. Click **Rebuild data + schedule** in the sidebar (about 1 minute).")
    st.stop()

world: World = st.session_state.world
sched: Schedule = st.session_state.sched
months = pd.period_range(world.start, world.end, freq="M")

with st.sidebar:
    month = st.selectbox("Month", months, index=1 if len(months) > 1 else 0, format_func=lambda p: p.strftime("%B %Y"))
    benches = st.multiselect("Benches", BENCHES, default=BENCHES)
    st.divider()
    st.caption(f"Horizon {world.start} → {world.end}")
    st.caption(f"{len(world.techs)} techs · {len(world.holidays)} holidays · {len(sched.log)} log entries")
m0 = max(world.start, month.start_time.date())
m1 = min(world.end, month.end_time.date())
name = {t["id"]: t["name"] for t in world.techs}
label = {t["id"]: f"{t['name']} ({t['home_dept']}, {t['shift_pref']})" for t in world.techs}


def kpis(w: World, s: Schedule):
    sm = summary(w, s)
    c = st.columns(5)
    c[0].metric("Coverage gaps", sm["coverage_gaps"], help="Slots below minimum staffing (hard rule)")
    c[1].metric("Hard-rule violations", sm["hard_violations"])
    c[2].metric("Preferences met", f"{sm['preference_satisfaction_pct']}%")
    c[3].metric("FT hours met", f"{sm['ft_hours_met_pct']}%")
    c[4].metric("PRN shifts", sm["prn_shifts"])


def diff_table(changes: list[dict]):
    if not changes:
        st.write("No other assignments change.")
        return
    df = pd.DataFrame(changes)
    df.insert(1, "name", df["tech"].map(name))
    st.dataframe(df.drop(columns=["tech"]), hide_index=True, use_container_width=True)


tabs = st.tabs(["Dashboard", "Schedule", "Holidays", "PTO requests", "Call-out", "Swap", "Staff change",
                "Fairness", "Profiles", "Audit log"])

# ------------------------------------------------------------- dashboard ---
with tabs[0]:
    st.header("Dashboard")
    kpis(world, sched)
    cov = pd.DataFrame(coverage(world, sched))
    cov["status"] = (cov["have"] - cov["need"]).map(lambda x: "short" if x < 0 else "at minimum" if x == 0 else "above")
    cov["month"] = pd.to_datetime(cov["date"]).dt.to_period("M").astype(str)
    agg = cov.groupby(["month", "status"]).size().unstack(fill_value=0).reindex(
        columns=["short", "at minimum", "above"], fill_value=0)
    st.subheader("Coverage health by month (slots)")
    st.caption("Every bench × shift × day slot. ✅ above minimum · ➖ exactly at minimum · ⛔ short")
    st.dataframe(agg.rename(columns={"short": "⛔ short", "at minimum": "➖ at minimum", "above": "✅ above"}),
                 use_container_width=True)
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Upcoming holidays")
        rows = []
        for d, h in sorted(world.holidays.items()):
            on = [a for (t, dd), a in sched.assign.items() if dd == d]
            rows.append({"date": d, "holiday": h["name"] + (" ★" if h["major"] else ""),
                         "working group": h["working_group"], "shifts staffed": len(on),
                         "PTO that day": sum(1 for t in world.techs if sched.on_pto(t["id"], d))})
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    with c2:
        st.subheader("PTO decisions (seniority order)")
        dec = Counter(d["status"] for d in sched.decisions)
        st.dataframe(pd.DataFrame([{"status": k, "requests": v} for k, v in dec.items()]), hide_index=True)
        v = violations(world, sched)
        if v:
            st.error(f"{len(v)} hard-rule violations")
            st.dataframe(pd.DataFrame(v), hide_index=True)
        else:
            st.success("Publishable: no hard-rule violations across the whole horizon.")

# -------------------------------------------------------------- schedule ---
with tabs[1]:
    st.header(f"Schedule — {month.strftime('%B %Y')}")
    st.caption("Cell = shift · bench (D Day 07–15:30, E Evening 15–23:30, N Night 23–07:30). "
               "H Heme, C Chem, M Micro, BB Blood Bank. PTO = approved leave. ★ federal holiday.")
    days = list(daterange(m0, m1))
    cols = [f"{d:%a %d}" + (" ★" if d in world.holidays else "") for d in days]
    rows, idx = [], []
    for t in sorted(world.techs, key=lambda t: (BENCHES.index(t["home_dept"]) if t["home_dept"] in BENCHES else 9,
                                                 t["shift_pref"], t["id"])):
        if t["home_dept"] not in benches and t["home_dept"] != "Float":
            continue
        r = []
        for d in days:
            a = sched.assign.get((t["id"], d))
            r.append(f"{a[0]}·{BENCH_ABBR[a[1]]}" if a else ("PTO" if sched.on_pto(t["id"], d) else ""))
        rows.append(r)
        idx.append(f"{t['name']} · {t['home_dept'][:5]} · {t['shift_pref'][0]}")
    grid = pd.DataFrame(rows, index=idx, columns=cols)

    def color(v):
        if v == "PTO":
            return "background-color:#e4e2dc;color:#52514e"
        if v:
            return f"background-color:{SHIFT_TINT[v[0]]};color:#0b0b0b;border-left:3px solid {SHIFT_COLOR[v[0]]}"
        return ""

    st.dataframe(grid.style.map(color), use_container_width=True, height=min(36 * len(rows) + 40, 1100))
    st.subheader("Coverage strip")
    cv = pd.DataFrame(coverage(world, sched, m0, m1))
    cv = cv[cv["bench"].isin(benches)]
    strip = cv.groupby(["date", "shift"]).apply(
        lambda g: "⛔" if (g["have"] < g["need"]).any() else f"{g['have'].sum()}/{g['need'].sum()}",
        include_groups=False).unstack()
    strip.index = [f"{d:%a %d}" for d in strip.index]
    st.dataframe(strip.T.rename(index=SHIFT_NAME), use_container_width=True)

# -------------------------------------------------------------- holidays ---
with tabs[2]:
    st.header("Holiday planner")
    st.caption("Holiday groups alternate each holiday, so whoever worked Christmas last year has it off this year. "
               "Groups also alternate within each bench's shift crew, so every holiday stays covered.")
    hd = sorted(world.holidays)
    rows = []
    for t in world.techs:
        r = {"tech": t["name"], "bench": t["home_dept"], "group": t["holiday_group"],
             "worked last year": ", ".join(t["worked_last_year"])}
        for d in hd:
            a = sched.assign.get((t["id"], d))
            r[f"{world.holidays[d]['name'].split(' (')[0][:14]} {d:%m/%d/%y}"] = \
                f"{a[0]}·{BENCH_ABBR[a[1]]}" if a else ("PTO" if sched.on_pto(t["id"], d) else "off")
        rows.append(r)
    hdf = pd.DataFrame(rows).sort_values(["group", "bench"])
    st.dataframe(hdf.style.map(lambda v: color(v) if isinstance(v, str) and (v == "PTO" or v[:2] in
                                                                                ("D·", "E·", "N·")) else ""),
                 hide_index=True, use_container_width=True, height=1050)
    st.caption("Working group per holiday: " + ", ".join(f"{world.holidays[d]['name'].split(' (')[0]} {d:%m/%d} → "
                                                         f"G{world.holidays[d]['working_group']}" for d in hd))

# ------------------------------------------------------------------- PTO ---
with tabs[3]:
    st.header("PTO requests")
    st.caption("Batch policy: decided in seniority order; at most 2 per home bench off per day (1 within ±3 days "
               "of Thanksgiving, Christmas, New Year's, July 4th); the holiday's working group must still cover it.")
    dec = pd.DataFrame(sched.decisions)
    if not dec.empty:
        f = st.segmented_control("Show", ["all", "approved", "partial", "denied"], default="all")
        view = dec if f in (None, "all") else dec[dec["status"] == f]
        st.dataframe(view[["seniority_rank", "name", "bench", "start", "end", "type", "hours", "status", "reason"]]
                     .sort_values("start"), hide_index=True, use_container_width=True, height=420)
    st.subheader("New request against the published schedule")
    c = st.columns([2, 1, 1, 1])
    tid = c[0].selectbox("Tech", [t["id"] for t in world.techs], format_func=label.get, key="pto_t")
    d0 = c[1].date_input("From", date(2027, 8, 2), min_value=world.start, max_value=world.end, key="pto_d0")
    d1 = c[2].date_input("To", date(2027, 8, 6), min_value=world.start, max_value=world.end, key="pto_d1")
    if c[3].button("Preview impact", use_container_width=True):
        trial = sched.copy()
        res = request_pto(world, trial, tid, d0, d1)
        alts = suggest_dates(world, sched, tid, d0, d1) if res["status"] == "blocked" else []
        st.session_state.preview = {"kind": "pto", "res": res, "trial": trial, "alts": alts}
    p = st.session_state.get("preview")
    if p and p["kind"] == "pto":
        res = p["res"]
        if res["status"] == "blocked":
            why = []
            if res["bench_limit_days"]:
                why.append(f"bench PTO limit reached on {', '.join(res['bench_limit_days'])}")
            if not res["coverage_ok"]:
                why.append(f"coverage gap: {'; '.join(res['coverage_gaps'])}")
            st.warning("⚠️ Blocked by policy: " + " and ".join(why))
            if p["alts"]:
                st.info("Nearest open windows: " + " · ".join(f"**{a['start']} → {a['end']}**" for a in p["alts"]))
            if st.button("Approve anyway (manager override)"):
                trial = sched.copy()
                r2 = request_pto(world, trial, tid, d0, d1, force=True)
                commit(world, trial, f"PTO approved by override; {len(r2.get('changes', []))} changes")
                st.rerun()
        else:
            st.success(f"✅ Can approve. Repair: {res['solve_status']} in {res['solve_seconds']} s; "
                       f"{len(res['removed'])} shifts become PTO, {res['backfilled']} other changes.")
            diff_table(res["changes"])
            if st.button("Commit approval", type="primary"):
                commit(world, p["trial"], "PTO approved and schedule repaired")
                st.rerun()

# -------------------------------------------------------------- call-out ---
with tabs[4]:
    st.header("Call-out: find a replacement")
    c = st.columns([1, 2])
    cd = c[0].date_input("Date", date(2027, 1, 1), min_value=world.start, max_value=world.end, key="co_d")
    working = sorted([t for (t, d) in sched.assign if d == cd], key=lambda t: sched.assign[(t, cd)])
    if working:
        who = c[1].selectbox("Who called out?", working,
                             format_func=lambda t: f"{name[t]}: {SHIFT_NAME[sched.assign[(t, cd)][0]]} / "
                                                   f"{sched.assign[(t, cd)][1]}")
        cands = find_replacement(world, sched, who, cd)
        s_, b_ = sched.assign[(who, cd)]
        st.caption(f"Need: {b_} {SHIFT_NAME[s_]} on {cd:%a %b %d}. Hard filters already applied: competency, "
                   "PTO, rest between shifts, max consecutive days, PRN availability.")
        if cands:
            cdf = pd.DataFrame([{**x, "why": "; ".join(x["why"])} for x in cands])
            st.dataframe(cdf[["name", "home", "employment", "score", "why"]], hide_index=True, use_container_width=True)
            pick = st.selectbox("Assign", [x["tech"] for x in cands], format_func=name.get)
            if st.button("Assign replacement", type="primary"):
                new = sched.copy()
                apply_callout(world, new, who, cd, pick)
                commit(world, new, f"{name[pick]} covers {name[who]} on {cd}")
                st.rerun()
        else:
            st.error("No qualified, rested, available tech. Consider overtime from an off-group or agency staff.")

# ------------------------------------------------------------------ swap ---
with tabs[5]:
    st.header("Swap request")
    st.caption("A's shift on date A goes to B; B's shift on date B (if any) goes to A. Checked against all hard rules.")
    c = st.columns(4)
    a = c[0].selectbox("Tech A", [t["id"] for t in world.techs], format_func=label.get, key="sw_a")
    da = c[1].date_input("Date A", date(2027, 2, 13), min_value=world.start, max_value=world.end, key="sw_da")
    b = c[2].selectbox("Tech B", [t["id"] for t in world.techs], index=1, format_func=label.get, key="sw_b")
    db = c[3].date_input("Date B", date(2027, 2, 12), min_value=world.start, max_value=world.end, key="sw_db")
    st.write(f"Before: {describe(world, sched, a, da)}  ↔  {describe(world, sched, b, db)}")
    if st.button("Validate swap"):
        trial = sched.copy()
        v = swap(world, trial, a, da, b, db)
        st.session_state.preview = {"kind": "swap", "v": v, "trial": trial}
    p = st.session_state.get("preview")
    if p and p["kind"] == "swap":
        if p["v"]:
            st.error("⛔ Swap breaks hard rules")
            st.dataframe(pd.DataFrame(p["v"]), hide_index=True)
        else:
            st.success("✅ Swap is valid")
            if st.button("Commit swap", type="primary"):
                commit(world, p["trial"], "Swap committed")
                st.rerun()

# ---------------------------------------------------------- staff change ---
with tabs[6]:
    st.header("Staff change: resignation or leave")
    c = st.columns([2, 1, 1])
    leaver = c[0].selectbox("Tech", [t["id"] for t in world.techs if not t.get("end_date")], format_func=label.get)
    eff = c[1].date_input("Effective (first day gone)", date(2027, 3, 1), min_value=world.start, max_value=world.end)
    if c[2].button("Preview re-plan"):
        w2, s2 = copy.deepcopy(world), sched.copy()
        w2.__post_init__()
        with st.spinner("Re-solving the remaining horizon…"):
            res = resign(w2, s2, leaver, eff)
        st.session_state.preview = {"kind": "resign", "res": res, "world": w2, "trial": s2}
    p = st.session_state.get("preview")
    if p and p["kind"] == "resign":
        res = p["res"]
        st.info(f"{res['shifts_removed']} shifts removed. {res['changes_to_others']} changes for others, "
                f"re-solved in {res['solve_seconds']} s.")
        kpis(p["world"], p["trial"])
        diff_table(res["changes"][:300])
        if st.button("Commit staff change", type="primary"):
            commit(p["world"], p["trial"], "Staff change committed")
            st.rerun()

# -------------------------------------------------------------- fairness ---
with tabs[7]:
    st.header("Fairness and preferences")
    met = pd.DataFrame(tech_metrics(world, sched))
    met = met[met["bench"].isin(benches + ["Float"])]
    long = met.melt(id_vars=["name", "bench"], value_vars=["D", "E", "N"], var_name="shift", value_name="count")
    long["shift"] = long["shift"].map(SHIFT_NAME)
    sel = alt.selection_point(fields=["shift"], bind="legend")
    chart = alt.Chart(long).mark_bar(cornerRadiusEnd=4, stroke="white", strokeWidth=1).encode(
        y=alt.Y("name:N", sort=alt.SortField("name"), title=None),
        x=alt.X("count:Q", title="Shifts in horizon"),
        color=alt.Color("shift:N", scale=alt.Scale(domain=["Day", "Evening", "Night"],
                                                    range=[SHIFT_COLOR["D"], SHIFT_COLOR["E"], SHIFT_COLOR["N"]]),
                        legend=alt.Legend(orient="top", title=None)),
        opacity=alt.condition(sel, alt.value(1), alt.value(0.25)),
        tooltip=["name", "bench", "shift", "count"]).add_params(sel).properties(height=28 * len(met))
    c1, c2 = st.columns(2)
    c1.subheader("Shift mix per tech")
    c1.altair_chart(chart, use_container_width=True)
    wk = alt.Chart(met).mark_bar(cornerRadiusEnd=4, color=SHIFT_COLOR["D"]).encode(
        y=alt.Y("name:N", sort="-x", title=None), x=alt.X("weekend_shifts:Q", title="Weekend shifts"),
        tooltip=["name", "bench", "shift_pref", "weekend_shifts", "off_group_weekend"]).properties(height=28 * len(met))
    c2.subheader("Weekend shifts per tech")
    c2.altair_chart(wk, use_container_width=True)
    st.subheader("Table view")
    st.dataframe(met, hide_index=True, use_container_width=True)

# -------------------------------------------------------------- profiles ---
with tabs[8]:
    st.header("Tech profiles (synthetic)")
    prof = pd.DataFrame([{
        "id": t["id"], "name": t["name"], "credential": t["credential"], "home": t["home_dept"],
        "competencies": ", ".join(f"{BENCH_ABBR[k]}:{v}" for k, v in t["competencies"].items()),
        "employment": t["employment"], "shifts/wk": t["shifts_per_week"], "shift pref": t["shift_pref"],
        "weekend grp": t["weekend_group"], "holiday grp": t["holiday_group"], "seniority": t["seniority_rank"],
        "hired": t["hire_date"], "PTO h": t["pto_balance_hours"], "max consec.": t["max_consecutive_shifts"],
        "wish list": ", ".join(t["holiday_off_wishlist"]), "limits": "; ".join(c["label"] for c in t["constraints"]),
        "end date": t.get("end_date", "")} for t in world.techs])
    st.dataframe(prof, hide_index=True, use_container_width=True, height=1050)

# ------------------------------------------------------------- audit log ---
with tabs[9]:
    st.header("Audit log")
    st.dataframe(pd.DataFrame(sched.log[::-1]), hide_index=True, use_container_width=True)
