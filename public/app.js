/* Clinical Lab Scheduler front end (no build step).
 *
 * The published schedule loads from /data/bundle.json. Edits are kept as a
 * "delta" in this browser (localStorage) and sent with every API call; the
 * stateless API (app.py) rebuilds base + delta, runs the action and returns a
 * patch that is merged in when the user commits.
 */
"use strict";

const STORE_KEY = "lab-scheduler-delta-v1";
const SHIFT = { D: "Day", E: "Evening", N: "Night" };
const ABBR = { Hematology: "H", Chemistry: "C", Microbiology: "M", "Blood Bank": "BB" };
const PREF_CODE = { Days: "D", Evenings: "E", Nights: "N" };
const PREF_ORDER = { Days: 0, Evenings: 1, Nights: 2, Rotating: 3 };

let B = null;                       // bundle
let delta = { assign: {}, pto: {}, end_dates: {} };
let log = [];
let byId = {}, dayIdx = {}, holidays = {}, staffIdx = new Map();
let summaryState = { loading: false, data: null, violations: [], count: 0 };
const ui = { tab: "dashboard", month: null, benches: null, ptoFilter: "all", ptoSearch: "",
             callout: { date: "2027-01-01", tech: null }, preview: null };

/* ---------------------------------------------------------------- utils -- */
const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtDay = (iso) => { const d = new Date(iso + "T12:00:00"); return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "2-digit" }); };
const weekday = (iso) => new Date(iso + "T12:00:00").getDay(); // 0 Sun .. 6 Sat
const addDays = (iso, n) => { const d = new Date(iso + "T12:00:00"); d.setDate(d.getDate() + n); return d.toISOString().slice(0, 10); };
const EPOCH = Date.UTC(2026, 0, 5);
const weekParity = (iso) => Math.floor((Date.parse(iso + "T00:00:00Z") - EPOCH) / 86400000 / 7) % 2;

function saveDelta() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify({ delta, log, base: B.start + B.end })); } catch (e) { /* private mode */ }
}
function loadDelta() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORE_KEY) || "null");
    if (raw && raw.base === B.start + B.end) { delta = raw.delta; log = raw.log || []; }
  } catch (e) { /* ignore */ }
}
const hasChanges = () => Object.keys(delta.assign).length || Object.keys(delta.pto).length || Object.keys(delta.end_dates).length;

async function api(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" },
                                  body: JSON.stringify({ delta, ...body }) });
  let data = null;
  try { data = await res.json(); } catch (e) { /* non-JSON error */ }
  if (!res.ok) {
    const msg = data && data.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : `${res.status} ${res.statusText}`;
    throw new Error(msg);
  }
  return data;
}

/* ---------------------------------------------------------------- model -- */
function baseCode(tid, iso) {
  const i = dayIdx[iso];
  const c = i === undefined ? "" : B.grid[tid][i];
  return c ? `${c[0]}|${B.benches[+c.slice(1)]}` : null;
}
function getA(tid, iso) {   // -> [shift, bench] | null
  const k = `${tid}|${iso}`;
  const v = k in delta.assign ? delta.assign[k] : baseCode(tid, iso);
  return v ? v.split("|") : null;
}
const onPto = (tid, iso) => (B.ptoSet[tid] && B.ptoSet[tid].has(iso)) || (delta.pto[tid] || []).includes(iso);
const endDate = (tid) => delta.end_dates[tid] || byId[tid].end_date || null;
const isActive = (tid, iso) => { const e = endDate(tid); return !e || iso <= e; };
const dayType = (iso) => holidays[iso] ? "holiday" : (weekday(iso) % 6 === 0 ? "weekend" : "weekday");
const need = (iso, s, b) => B.minimums[dayType(iso)][s][b];
const qualified = (t, b) => ["primary", "competent"].includes(t.competencies[b]);
function describe(tid, iso) {
  const a = getA(tid, iso);
  return `${byId[tid].name} ${fmtDay(iso)}: ${a ? `${SHIFT[a[0]]} / ${a[1]}` : onPto(tid, iso) ? "PTO" : "off"}`;
}

function rebuildIndex() {
  staffIdx = new Map();
  for (const t of B.techs) for (const iso of B.days) {
    const a = getA(t.id, iso);
    if (!a) continue;
    const k = `${iso}|${a[0]}|${a[1]}`;
    if (!staffIdx.has(k)) staffIdx.set(k, []);
    staffIdx.get(k).push(t.id);
  }
}
const have = (iso, s, b) => (staffIdx.get(`${iso}|${s}|${b}`) || []).length;

function mergePatch(p) {
  for (const [k, v] of Object.entries(p.assign || {})) {
    const [tid, iso] = k.split("|");
    if (v === baseCode(tid, iso)) delete delta.assign[k]; else delta.assign[k] = v;
  }
  for (const [tid, days] of Object.entries(p.pto || {})) {
    const s = new Set(delta.pto[tid] || []);
    days.forEach((d) => s.add(d));
    delta.pto[tid] = [...s].sort();
  }
  Object.assign(delta.end_dates, p.end_dates || {});
}

async function commit(patch, text) {
  mergePatch(patch);
  log.push({ at: new Date().toISOString(), text, changes: Object.keys(patch.assign || {}).length });
  ui.preview = null;
  saveDelta();
  rebuildIndex();
  render();
  toast(text);
  await refreshSummary();
}

async function refreshSummary() {
  if (!hasChanges()) { summaryState = { loading: false, data: B.summary, violations: [], count: 0 }; renderChrome(); if (ui.tab === "dashboard") render(); return; }
  summaryState.loading = true; renderChrome();
  try {
    const r = await api("/api/summary", {});
    summaryState = { loading: false, data: r.summary, violations: r.violations, count: r.violation_count };
  } catch (e) {
    summaryState = { loading: false, data: null, violations: [], count: 0, error: e.message };
  }
  renderChrome();
  if (ui.tab === "dashboard") render();
}

/* ------------------------------------------------------------ rendering -- */
function toast(text) {
  const el = document.createElement("div");
  el.className = "note good";
  el.style.cssText = "position:fixed;right:16px;bottom:16px;z-index:50;max-width:min(420px,calc(100vw - 32px));box-shadow:0 4px 18px rgba(0,0,0,.15)";
  el.textContent = text;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 3500);
}

function renderChrome() {
  $("#horizon").textContent = `${B.start} → ${B.end}`;
  const n = Object.keys(delta.assign).length + Object.values(delta.pto).flat().length + Object.keys(delta.end_dates).length;
  const pill = $("#changes-pill");
  pill.hidden = !hasChanges();
  pill.className = "pill warn";
  pill.textContent = `${log.length} unpublished edit${log.length === 1 ? "" : "s"} · ${n} cells`;
  $("#reset").hidden = !hasChanges();
  document.querySelectorAll(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === ui.tab)));
}

function table(cols, rows, { tall = false, cls = "" } = {}) {
  const head = cols.map((c) => `<th class="${c.num ? "num" : ""}">${esc(c.label)}</th>`).join("");
  const body = rows.map((r) => `<tr>${cols.map((c) => `<td class="${c.num ? "num" : ""}">${c.html ? c.html(r) : esc(c.get ? c.get(r) : r[c.key])}</td>`).join("")}</tr>`).join("");
  return `<div class="tablewrap ${tall ? "tall" : ""}"><table class="${cls}"><thead><tr>${head}</tr></thead><tbody>${body || `<tr><td colspan="${cols.length}" class="muted">Nothing to show.</td></tr>`}</tbody></table></div>`;
}
const techOptions = (sel, filter = () => true) => B.techs.filter(filter).map((t) =>
  `<option value="${t.id}" ${t.id === sel ? "selected" : ""}>${esc(t.name)} (${esc(t.home_dept)}, ${esc(t.shift_pref)})</option>`).join("");
const errNote = (e) => `<div class="note crit">${esc(e.message || e)}</div>`;
const changesTable = (rows) => table([
  { label: "Date", key: "date" }, { label: "Tech", get: (r) => byId[r.tech] ? byId[r.tech].name : r.tech },
  { label: "Before", key: "before" }, { label: "After", key: "after" }], rows);
function patchRows(p) {
  return Object.entries(p.assign || {}).map(([k, v]) => {
    const [tech, date] = k.split("|");
    const b = getA(tech, date);
    return { tech, date, before: b ? b.join(" · ") : "off", after: v ? v.replace("|", " · ") : (onPto(tech, date) || (p.pto[tech] || []).includes(date) ? "PTO" : "off") };
  }).sort((a, b) => a.date.localeCompare(b.date));
}

function render() {
  renderChrome();
  const main = $("#main");
  const views = { dashboard, schedule, holidaysView, pto, callout, swapView, staff, fairness, profiles, logView };
  const name = { holidays: "holidaysView", swap: "swapView", log: "logView" }[ui.tab] || ui.tab;
  main.innerHTML = (views[name] || dashboard)();
  const after = { schedule: wireSchedule, pto: wirePto, callout: wireCallout, swap: wireSwap, staff: wireStaff, dashboard: () => {}, log: wireLog };
  (after[ui.tab] || (() => {}))();
}

/* ------------------------------------------------------------ dashboard -- */
function kpiTiles(s) {
  if (!s) return `<div class="note">${summaryState.loading ? '<span class="spinner"></span>Re-checking every rule against your edits…' : esc(summaryState.error || "Summary unavailable.")}</div>`;
  const tiles = [
    ["Coverage gaps", s.coverage_gaps, s.coverage_gaps > 0], ["Hard-rule violations", s.hard_violations, s.hard_violations > 0],
    ["Preferences met", `${s.preference_satisfaction_pct}%`], ["FT hours met", `${s.ft_hours_met_pct}%`],
    ["PRN shifts", s.prn_shifts], ["Shifts scheduled", s.assignments.toLocaleString()]];
  return `<div class="kpis">${tiles.map(([l, v, bad]) => `<div class="kpi ${bad ? "bad" : ""}"><div class="label">${l}</div><div class="value">${v}</div></div>`).join("")}</div>`;
}

function dashboard() {
  const months = {};
  for (const iso of B.days) for (const s of "DEN") for (const b of B.benches) {
    const n = need(iso, s, b), h = have(iso, s, b);
    if (!n && !h) continue;
    const m = iso.slice(0, 7);
    months[m] = months[m] || { month: m, short: 0, at: 0, above: 0 };
    months[m][h < n ? "short" : h === n ? "at" : "above"]++;
  }
  const dec = {}; B.decisions.forEach((d) => { dec[d.status] = (dec[d.status] || 0) + 1; });
  const v = summaryState.violations;
  return `
  <section><h2>Dashboard</h2><p class="lead">The published schedule${hasChanges() ? " plus your unpublished edits" : ""}, checked against every hard rule.</p></section>
  ${kpiTiles(summaryState.data)}
  <div class="grid2">
    <div class="panel"><h3>Coverage by month</h3>
      <p class="muted" style="margin:0 0 8px">Every bench × shift × day slot, counted by staffing against its minimum.</p>
      ${table([{ label: "Month", key: "month" }, { label: "⛔ Short", key: "short", num: true }, { label: "At minimum", key: "at", num: true }, { label: "✓ Above", key: "above", num: true }], Object.values(months))}
    </div>
    <div class="panel"><h3>Rule check</h3>
      ${summaryState.data && summaryState.data.hard_violations === 0 ? '<div class="note good">✓ Publishable: no hard-rule violations across the horizon.</div>' :
        v.length ? table([{ label: "Date", key: "date" }, { label: "Rule", key: "rule" }, { label: "Detail", key: "detail" }], v) : '<div class="note">Checking…</div>'}
      <h3 style="margin-top:16px">PTO decisions (seniority order)</h3>
      <div class="chips">${Object.entries(dec).map(([k, n]) => `<span class="pill ${k === "approved" ? "good" : k === "partial" ? "warn" : "crit"}">${n} ${k}</span>`).join("")}</div>
      <h3 style="margin-top:16px">Holidays</h3>
      ${table([{ label: "Date", key: "date" }, { label: "Holiday", get: (h) => h.name + (h.major ? " ★" : "") },
               { label: "Group working", key: "working_group", num: true },
               { label: "Staffed", num: true, get: (h) => "DEN".split("").reduce((a, s) => a + B.benches.reduce((x, b) => x + have(h.date, s, b), 0), 0) }], B.holidays)}
    </div>
  </div>`;
}

/* ------------------------------------------------------------- schedule -- */
function months() {
  const out = []; let m = B.start.slice(0, 7);
  while (m <= B.end.slice(0, 7)) { out.push(m); const [y, mm] = m.split("-").map(Number); m = mm === 12 ? `${y + 1}-01` : `${y}-${String(mm + 1).padStart(2, "0")}`; }
  return out;
}
function schedule() {
  const ms = months();
  ui.month = ui.month || ms[1] || ms[0];
  ui.benches = ui.benches || new Set(B.benches);
  const days = B.days.filter((d) => d.startsWith(ui.month));
  const groups = [...B.benches, "Float"].filter((g) => g === "Float" || ui.benches.has(g));
  let rows = "";
  for (const g of groups) {
    const ts = B.techs.filter((t) => t.home_dept === g).sort((a, b) => PREF_ORDER[a.shift_pref] - PREF_ORDER[b.shift_pref] || a.id.localeCompare(b.id));
    rows += `<tr class="bench"><td class="who">${esc(g === "Float" ? "PRN floats" : g)}</td>${days.map(() => "<td></td>").join("")}</tr>`;
    for (const t of ts) {
      rows += `<tr><td class="who">${esc(t.name)} <span class="muted">· ${t.shift_pref[0]}${t.employment !== "FT" ? " · " + t.employment : ""}${endDate(t.id) ? " · leaves " + endDate(t.id) : ""}</span></td>`;
      for (const iso of days) {
        const a = getA(t.id, iso), chg = `${t.id}|${iso}` in delta.assign;
        if (a) rows += `<td class="cell ${a[0]}${chg ? " chg" : ""}" data-t="${t.id}" data-d="${iso}" title="${esc(describe(t.id, iso))}">${a[0]}·${ABBR[a[1]]}</td>`;
        else if (onPto(t.id, iso)) rows += `<td class="cell P" title="PTO">PTO</td>`;
        else rows += `<td class="${chg ? "cell chg" : ""}"></td>`;
      }
      rows += "</tr>";
    }
  }
  const cov = "DEN".split("").map((s) => `<tr><td class="who">${SHIFT[s]} staffed / min</td>${days.map((iso) => {
    let h = 0, n = 0, short = false;
    for (const b of ui.benches) { const hh = have(iso, s, b), nn = need(iso, s, b); h += hh; n += nn; if (hh < nn) short = true; }
    return `<td class="cov ${short ? "short" : "ok"}">${short ? "⛔" : ""}${h}/${n}</td>`;
  }).join("")}</tr>`).join("");
  const head = days.map((iso) => {
    const wd = weekday(iso), hol = holidays[iso];
    return `<th class="${hol ? "hol" : wd % 6 === 0 ? "wkd" : ""}" title="${hol ? esc(hol.name) : ""}">${fmtDay(iso).slice(0, 3)}<br>${iso.slice(8)}${hol ? " ★" : ""}</th>`;
  }).join("");
  return `
  <section><h2>Schedule</h2><p class="lead">Each cell is shift · bench. Click a scheduled cell to handle a call-out for it. Dashed outlines mark your unpublished edits.</p></section>
  <div class="panel">
    <div class="form" style="margin-bottom:12px">
      <div class="field"><label for="month">Month</label><select id="month">${ms.map((m) => `<option ${m === ui.month ? "selected" : ""} value="${m}">${new Date(m + "-15T12:00:00").toLocaleDateString("en-US", { month: "long", year: "numeric" })}</option>`).join("")}</select></div>
      <div class="field"><label>Benches</label><div class="chips" id="bench-chips">${B.benches.map((b) => `<button class="chip" aria-pressed="${ui.benches.has(b)}" data-b="${esc(b)}">${esc(b)}</button>`).join("")}</div></div>
    </div>
    <div class="legend" style="margin-bottom:10px"><span><i class="sw D"></i>D Day 07:00–15:30</span><span><i class="sw E"></i>E Evening 15:00–23:30</span><span><i class="sw N"></i>N Night 23:00–07:30</span><span><i class="sw P"></i>PTO</span><span>H Heme · C Chem · M Micro · BB Blood Bank · ★ federal holiday</span></div>
    <div class="tablewrap tall"><table class="grid"><thead><tr><th class="who">Tech</th>${head}</tr></thead><tbody>${rows}</tbody><tfoot>${cov}</tfoot></table></div>
  </div>`;
}
function wireSchedule() {
  $("#month").onchange = (e) => { ui.month = e.target.value; render(); };
  $("#bench-chips").onclick = (e) => {
    const b = e.target.dataset.b; if (!b) return;
    ui.benches.has(b) ? ui.benches.delete(b) : ui.benches.add(b);
    if (!ui.benches.size) ui.benches.add(b);
    render();
  };
  $("#main").querySelectorAll("td.cell[data-t]").forEach((td) => td.onclick = () => {
    ui.callout = { date: td.dataset.d, tech: td.dataset.t }; go("callout");
  });
}

/* ------------------------------------------------------------- holidays -- */
function holidaysView() {
  const cols = [{ label: "Tech", key: "name" }, { label: "Bench", key: "home_dept" }, { label: "Group", key: "holiday_group", num: true },
    ...B.holidays.map((h) => ({ label: `${h.name.split(" (")[0].replace(" National Independence Day", "")} ${h.date.slice(5)}/${h.date.slice(2, 4)}`,
      html: (t) => { const a = getA(t.id, h.date); return a ? `<span class="cell ${a[0]}" style="padding:1px 5px;border-radius:3px">${a[0]}·${ABBR[a[1]]}</span>` : onPto(t.id, h.date) ? '<span class="cell P" style="padding:1px 5px;border-radius:3px">PTO</span>' : '<span class="muted">off</span>'; } }))];
  const rows = [...B.techs].sort((a, b) => a.holiday_group.localeCompare(b.holiday_group) || a.home_dept.localeCompare(b.home_dept));
  return `
  <section><h2>Holiday planner</h2><p class="lead">Holiday groups alternate holiday by holiday, and each holiday flips every year: group 1 works Christmas 2026, group 2 works Christmas 2027. Groups also alternate within each bench's shift crew, so every holiday stays covered.</p></section>
  <div class="panel">${table([{ label: "Holiday", get: (h) => h.name + (h.major ? " ★" : "") }, { label: "Date staffed", key: "date" }, { label: "Observed (payroll)", key: "observed_date" }, { label: "Group working", key: "working_group", num: true }], B.holidays)}</div>
  <div class="panel"><h3>Who works each holiday</h3>${table(cols, rows, { tall: true })}</div>`;
}

/* ------------------------------------------------------------------ PTO -- */
function pto() {
  const q = ui.ptoSearch.toLowerCase();
  const rows = B.decisions.filter((d) => (ui.ptoFilter === "all" || d.status === ui.ptoFilter) && (!q || d.name.toLowerCase().includes(q) || d.bench.toLowerCase().includes(q)))
    .sort((a, b) => a.start.localeCompare(b.start));
  const p = ui.preview && ui.preview.kind === "pto" ? ui.preview : null;
  const f = ui.ptoForm || { tech: "T14", start: "2027-08-02", end: "2027-08-06" };
  return `
  <section><h2>PTO requests</h2><p class="lead">Decided in seniority order. At most ${B.pto_policy.max_off_per_bench} per home bench can be off on a day, and ${B.pto_policy.max_off_per_bench_blackout} within ±${B.pto_policy.blackout_days} days of Thanksgiving, Christmas, New Year's and July 4th. The holiday's working group must still cover it.</p></section>
  <div class="panel">
    <h3>New request against the published schedule</h3>
    <div class="form">
      <div class="field"><label for="pto-tech">Tech</label><select id="pto-tech">${techOptions(f.tech, (t) => t.employment !== "PRN")}</select></div>
      <div class="field"><label for="pto-start">From</label><input type="date" id="pto-start" value="${f.start}" min="${B.start}" max="${B.end}"></div>
      <div class="field"><label for="pto-end">To</label><input type="date" id="pto-end" value="${f.end}" min="${B.start}" max="${B.end}"></div>
      <button class="btn" id="pto-preview">Preview impact</button>
    </div>
    <div id="pto-out" style="margin-top:12px">${p ? ptoResult(p) : ""}</div>
  </div>
  <div class="panel">
    <div class="form" style="margin-bottom:10px">
      <div class="chips" id="pto-filter">${["all", "approved", "partial", "denied"].map((s) => `<button class="chip" aria-pressed="${ui.ptoFilter === s}" data-s="${s}">${s}</button>`).join("")}</div>
      <div class="field"><input type="search" id="pto-search" placeholder="Filter by name or bench" value="${esc(ui.ptoSearch)}" aria-label="Filter requests"></div>
    </div>
    ${table([{ label: "Seniority", get: (d) => "#" + d.seniority_rank, num: true }, { label: "Name", key: "name" }, { label: "Bench", key: "bench" },
             { label: "From", key: "start" }, { label: "To", key: "end" }, { label: "Type", key: "type" },
             { label: "Status", html: (d) => `<span class="pill ${d.status === "approved" ? "good" : d.status === "partial" ? "warn" : "crit"}">${d.status}</span>` },
             { label: "Reason", key: "reason" }], rows, { tall: true })}
  </div>`;
}
function ptoResult(p) {
  if (p.loading) return '<div class="note"><span class="spinner"></span>Checking bench limits and coverage, then repairing the affected weeks…</div>';
  if (p.error) return errNote(p.error);
  const r = p.data.result;
  if (r.status === "blocked") {
    const why = [];
    if (r.bench_limit_days.length) why.push(`the bench PTO limit is already reached on ${r.bench_limit_days.join(", ")}`);
    if (!r.coverage_ok) why.push(`it would leave a coverage gap (${r.coverage_gaps.join("; ")})`);
    const alts = p.data.alternatives || [];
    return `<div class="note warn">Blocked: ${esc(why.join(" and "))}.</div>
      ${alts.length ? `<p>Nearest open windows: ${alts.map((a) => `<button class="chip alt" data-s="${a.start}" data-e="${a.end}">${a.start} → ${a.end}</button>`).join(" ")}</p>` : ""}
      <button class="btn ghost" id="pto-force">Approve anyway (manager override)</button>`;
  }
  return `<div class="note good">✓ Can approve. ${r.removed.length} shift(s) become PTO; repair re-solved the affected weeks in ${r.solve_seconds} s and made ${r.backfilled} other change(s).</div>
    <div style="margin:10px 0">${changesTable(patchRows(p.data.patch))}</div>
    <button class="btn" id="pto-commit">Commit approval</button>`;
}
function wirePto() {
  const form = () => ({ tech: $("#pto-tech").value, start: $("#pto-start").value, end: $("#pto-end").value });
  const run = async (force = false) => {
    ui.ptoForm = form();
    ui.preview = { kind: "pto", loading: true }; render();
    try { ui.preview = { kind: "pto", data: await api("/api/pto/preview", { ...ui.ptoForm, force }), force }; }
    catch (e) { ui.preview = { kind: "pto", error: e }; }
    render();
  };
  $("#pto-preview").onclick = () => run(false);
  $("#pto-filter").onclick = (e) => { if (e.target.dataset.s) { ui.ptoFilter = e.target.dataset.s; render(); } };
  $("#pto-search").oninput = (e) => { ui.ptoSearch = e.target.value; const pos = e.target.selectionStart; render(); const el = $("#pto-search"); el.focus(); el.setSelectionRange(pos, pos); };
  const force = $("#pto-force"); if (force) force.onclick = () => run(true);
  document.querySelectorAll(".alt").forEach((b) => b.onclick = () => { $("#pto-start").value = b.dataset.s; $("#pto-end").value = b.dataset.e; run(false); });
  const c = $("#pto-commit"); if (c) c.onclick = () => {
    const f = ui.ptoForm; commit(ui.preview.data.patch, `PTO approved${ui.preview.force ? " by override" : ""}: ${byId[f.tech].name} ${f.start} → ${f.end}`);
  };
}

/* -------------------------------------------------------------- call-out -- */
function callout() {
  const iso = ui.callout.date;
  const working = B.techs.filter((t) => getA(t.id, iso)).sort((a, b) => getA(a.id, iso).join().localeCompare(getA(b.id, iso).join()));
  if (!working.find((t) => t.id === ui.callout.tech)) ui.callout.tech = working[0] ? working[0].id : null;
  const p = ui.preview && ui.preview.kind === "callout" ? ui.preview : null;
  return `
  <section><h2>Call-out</h2><p class="lead">Pick who called out. Candidates are already filtered for competency, PTO, rest between shifts, consecutive days and PRN limits, then ranked by cost and fairness.</p></section>
  <div class="panel">
    <div class="form">
      <div class="field"><label for="co-date">Date</label><input type="date" id="co-date" value="${iso}" min="${B.start}" max="${B.end}"></div>
      <div class="field" style="flex:1 1 320px"><label for="co-who">Who called out?</label><select id="co-who">${working.map((t) => { const a = getA(t.id, iso); return `<option value="${t.id}" ${t.id === ui.callout.tech ? "selected" : ""}>${esc(t.name)} — ${SHIFT[a[0]]} / ${esc(a[1])}</option>`; }).join("")}</select></div>
      <button class="btn" id="co-find">Find replacement</button>
    </div>
    <div id="co-out" style="margin-top:12px">${p ? calloutResult(p) : ""}</div>
  </div>`;
}
function calloutResult(p) {
  if (p.loading) return '<div class="note"><span class="spinner"></span>Ranking replacements…</div>';
  if (p.error) return errNote(p.error);
  const d = p.data;
  if (!d.candidates.length) return '<div class="note crit">No qualified, rested, available tech. Consider overtime from an off-group tech or agency staff.</div>';
  return `<p class="muted">${esc(d.who)} · ranked in ${d.ms} ms</p>` + table([
    { label: "Name", key: "name" }, { label: "Home", key: "home" }, { label: "Type", key: "employment" }, { label: "Score", key: "score", num: true },
    { label: "Why", get: (c) => c.why.join("; ") },
    { label: "", html: (c) => `<button class="btn ghost assign" data-r="${c.tech}">Assign</button>` }], d.candidates);
}
function wireCallout() {
  const find = async () => {
    ui.callout = { date: $("#co-date").value, tech: $("#co-who").value };
    if (!ui.callout.tech) return;
    ui.preview = { kind: "callout", loading: true }; render();
    try { ui.preview = { kind: "callout", data: await api("/api/callout/candidates", ui.callout) }; }
    catch (e) { ui.preview = { kind: "callout", error: e }; }
    render();
  };
  $("#co-date").onchange = (e) => { ui.callout = { date: e.target.value, tech: null }; ui.preview = null; render(); };
  $("#co-who").onchange = () => { ui.preview = null; find(); };
  $("#co-find").onclick = find;
  document.querySelectorAll(".assign").forEach((b) => b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await api("/api/callout/apply", { ...ui.callout, replacement: b.dataset.r });
      await commit(r.patch, `Call-out: ${byId[b.dataset.r].name} covers ${byId[ui.callout.tech].name} on ${ui.callout.date}`);
    } catch (e) { $("#co-out").innerHTML = errNote(e); }
  });
}

/* ----------------------------------------------------------------- swap -- */
function swapView() {
  const f = ui.swapForm || { a: "T01", da: "2027-02-13", b: "T03", db: "2027-02-09" };
  const p = ui.preview && ui.preview.kind === "swap" ? ui.preview : null;
  return `
  <section><h2>Swap request</h2><p class="lead">A's shift on date A goes to B, and B's shift on date B goes to A. The swap is checked against every hard rule before you can commit it.</p></section>
  <div class="panel">
    <div class="form">
      <div class="field"><label for="sw-a">Tech A</label><select id="sw-a">${techOptions(f.a)}</select></div>
      <div class="field"><label for="sw-da">Date A</label><input type="date" id="sw-da" value="${f.da}" min="${B.start}" max="${B.end}"></div>
      <div class="field"><label for="sw-b">Tech B</label><select id="sw-b">${techOptions(f.b)}</select></div>
      <div class="field"><label for="sw-db">Date B</label><input type="date" id="sw-db" value="${f.db}" min="${B.start}" max="${B.end}"></div>
      <button class="btn" id="sw-check">Validate swap</button>
    </div>
    <p class="muted" id="sw-now">Now: ${esc(describe(f.a, f.da))} ↔ ${esc(describe(f.b, f.db))}</p>
    <div>${p ? swapResult(p) : ""}</div>
  </div>`;
}
function swapResult(p) {
  if (p.loading) return '<div class="note"><span class="spinner"></span>Validating…</div>';
  if (p.error) return errNote(p.error);
  const d = p.data;
  if (d.violations.length) return `<div class="note crit">⛔ This swap breaks ${d.violations.length} hard rule(s).</div>` +
    table([{ label: "Date", key: "date" }, { label: "Rule", key: "rule" }, { label: "Detail", key: "detail" }], d.violations);
  return `<div class="note good">✓ Valid. After: ${esc(d.after.join(" ↔ "))}</div><p><button class="btn" id="sw-commit">Commit swap</button></p>`;
}
function wireSwap() {
  const form = () => ({ a: $("#sw-a").value, da: $("#sw-da").value, b: $("#sw-b").value, db: $("#sw-db").value });
  ["#sw-a", "#sw-da", "#sw-b", "#sw-db"].forEach((s) => $(s).onchange = () => { ui.swapForm = form(); ui.preview = null; render(); });
  $("#sw-check").onclick = async () => {
    ui.swapForm = form(); ui.preview = { kind: "swap", loading: true }; render();
    try { ui.preview = { kind: "swap", data: await api("/api/swap", ui.swapForm) }; } catch (e) { ui.preview = { kind: "swap", error: e }; }
    render();
  };
  const c = $("#sw-commit"); if (c) c.onclick = () => { const f = ui.swapForm; commit(ui.preview.data.patch, `Swap: ${byId[f.a].name} ${f.da} ↔ ${byId[f.b].name} ${f.db}`); };
}

/* --------------------------------------------------------- staff change -- */
function staff() {
  const f = ui.staffForm || { tech: "T09", effective: "2027-03-01" };
  const p = ui.preview && ui.preview.kind === "staff" ? ui.preview : null;
  return `
  <section><h2>Staff change</h2><p class="lead">Simulate a resignation or long leave. The optimizer re-plans everything from the effective date to the end of the horizon, keeping other people's published shifts wherever it can.</p></section>
  <div class="panel">
    <div class="form">
      <div class="field"><label for="st-tech">Tech</label><select id="st-tech">${techOptions(f.tech, (t) => !endDate(t.id))}</select></div>
      <div class="field"><label for="st-eff">Effective (first day gone)</label><input type="date" id="st-eff" value="${f.effective}" min="${addDays(B.start, 1)}" max="${B.end}"></div>
      <button class="btn" id="st-run" ${p && p.loading ? "disabled" : ""}>Preview re-plan</button>
    </div>
    <div style="margin-top:12px">${p ? staffResult(p) : ""}</div>
  </div>`;
}
function staffResult(p) {
  if (p.loading) return '<div class="note"><span class="spinner"></span>Re-planning the rest of the horizon. This takes about 30 seconds.</div>';
  if (p.error) return errNote(p.error);
  const d = p.data, r = d.result;
  return `<div class="note ${d.summary.coverage_gaps ? "crit" : "good"}">${r.shifts_removed} shifts removed; ${r.changes_to_others} changes for other staff; solved in ${r.solve_seconds} s.</div>
    <div style="margin:12px 0">${kpiTiles(d.summary)}</div>
    <p class="muted">First ${d.changes.length} changes:</p>${changesTable(d.changes)}
    <p><button class="btn" id="st-commit">Commit staff change</button></p>`;
}
function wireStaff() {
  $("#st-run").onclick = async () => {
    ui.staffForm = { tech: $("#st-tech").value, effective: $("#st-eff").value };
    ui.preview = { kind: "staff", loading: true }; render();
    try { ui.preview = { kind: "staff", data: await api("/api/resign", ui.staffForm) }; } catch (e) { ui.preview = { kind: "staff", error: e }; }
    render();
  };
  const c = $("#st-commit"); if (c) c.onclick = () => { const f = ui.staffForm; commit(ui.preview.data.patch, `Staff change: ${byId[f.tech].name} leaves effective ${f.effective}`); };
}

/* ------------------------------------------------------------- fairness -- */
function metrics() {
  return B.techs.map((t) => {
    const m = { t, D: 0, E: 0, N: 0, weekend: 0, holidays: 0, offPref: 0, offWeekend: 0, pto: 0 };
    for (const iso of B.days) {
      if (onPto(t.id, iso)) m.pto++;
      const a = getA(t.id, iso); if (!a) continue;
      m[a[0]]++;
      const wk = weekday(iso) % 6 === 0;
      if (wk) m.weekend++;
      if (holidays[iso]) m.holidays++;
      if (PREF_CODE[t.shift_pref] && a[0] !== PREF_CODE[t.shift_pref]) m.offPref++;
      if (wk && !holidays[iso] && t.employment !== "PRN" && "AB"[weekParity(iso)] !== t.weekend_group) m.offWeekend++;
    }
    m.total = m.D + m.E + m.N;
    return m;
  });
}
function fairness() {
  const ms = metrics().filter((m) => m.t.home_dept === "Float" || true);
  const max = Math.max(...ms.map((m) => m.total)), maxW = Math.max(...ms.map((m) => m.weekend));
  const mix = [...ms].sort((a, b) => a.t.name.localeCompare(b.t.name)).map((m) => `<div class="bar-row"><span class="name" title="${esc(m.t.name)}">${esc(m.t.name)}</span>
    <span class="bar" title="Day ${m.D} · Evening ${m.E} · Night ${m.N}">${"DEN".split("").map((s) => m[s] ? `<span class="${s}" style="width:${(m[s] / max) * 100}%"></span>` : "").join("")}</span><span class="num">${m.total}</span></div>`).join("");
  const wk = [...ms].sort((a, b) => b.weekend - a.weekend).map((m) => `<div class="bar-row"><span class="name">${esc(m.t.name)}</span>
    <span class="bar" title="${m.weekend} weekend shifts, ${m.offWeekend} on off-group weekends"><span class="W" style="width:${(m.weekend / maxW) * 100}%"></span></span><span class="num">${m.weekend}</span></div>`).join("");
  return `
  <section><h2>Fairness and preferences</h2><p class="lead">Shift mix, weekend load and holidays per tech across the whole horizon. Weekend counts differ mostly by crew size: a crew of four shares one weekend slot that a crew of two would split.</p></section>
  <div class="grid2">
    <div class="panel"><h3>Shift mix per tech</h3><div class="legend" style="margin-bottom:8px"><span><i class="sw D" style="background:var(--shift-d)"></i>Day</span><span><i class="sw E" style="background:var(--shift-e)"></i>Evening</span><span><i class="sw N" style="background:var(--shift-n)"></i>Night</span></div><div class="bars">${mix}</div></div>
    <div class="panel"><h3>Weekend shifts per tech</h3><div class="bars">${wk}</div></div>
  </div>
  <div class="panel"><h3>Table view</h3>${table([{ label: "Tech", get: (m) => m.t.name }, { label: "Bench", get: (m) => m.t.home_dept }, { label: "Pref", get: (m) => m.t.shift_pref },
    { label: "Shifts", key: "total", num: true }, { label: "Day", key: "D", num: true }, { label: "Eve", key: "E", num: true }, { label: "Night", key: "N", num: true },
    { label: "Weekend", key: "weekend", num: true }, { label: "Off-group wknd", key: "offWeekend", num: true }, { label: "Holidays", key: "holidays", num: true },
    { label: "Off-pref shift", key: "offPref", num: true }, { label: "PTO days", key: "pto", num: true }], ms, { tall: true })}</div>`;
}

/* ------------------------------------------------------------- profiles -- */
function profiles() {
  return `<section><h2>Tech profiles</h2><p class="lead">28 synthetic, fictional technologists. Rank 1 is the most senior.</p></section>
  <div class="panel">${table([
    { label: "ID", key: "id" }, { label: "Name", key: "name" }, { label: "Credential", key: "credential" }, { label: "Home", key: "home_dept" },
    { label: "Signed off on", get: (t) => Object.entries(t.competencies).map(([b, l]) => `${ABBR[b]}:${l}`).join(", ") },
    { label: "Type", key: "employment" }, { label: "Shifts/wk", key: "shifts_per_week", num: true }, { label: "Shift pref", key: "shift_pref" },
    { label: "Wknd grp", key: "weekend_group" }, { label: "Hol grp", key: "holiday_group" }, { label: "Seniority", key: "seniority_rank", num: true },
    { label: "Hired", key: "hire_date" }, { label: "PTO h", key: "pto_balance_hours", num: true }, { label: "Max consec.", key: "max_consecutive_shifts", num: true },
    { label: "Limits", get: (t) => t.constraints.map((c) => c.label).join("; ") }, { label: "Leaves", get: (t) => endDate(t.id) || "" }], B.techs, { tall: true })}</div>`;
}

/* ------------------------------------------------------------------ log -- */
function logView() {
  return `<section><h2>Change log</h2><p class="lead">Edits you've made in this browser on top of the published schedule. They are not shared with anyone else.</p></section>
  <div class="panel">${table([{ label: "When", get: (e) => new Date(e.at).toLocaleString() }, { label: "Change", key: "text" }, { label: "Cells", key: "changes", num: true }], [...log].reverse())}
  ${hasChanges() ? '<p><button class="btn ghost" id="log-reset">Discard all my changes</button></p>' : ""}</div>`;
}
function wireLog() { const b = $("#log-reset"); if (b) b.onclick = reset; }

function reset() {
  delta = { assign: {}, pto: {}, end_dates: {} }; log = []; ui.preview = null;
  saveDelta(); rebuildIndex(); refreshSummary(); render(); toast("Back to the published schedule.");
}

/* ----------------------------------------------------------------- boot -- */
function go(tab) {
  ui.tab = tab; ui.preview = null;
  if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
  render(); window.scrollTo({ top: 0 });
}

async function boot() {
  try {
    const res = await fetch("/data/bundle.json");
    if (!res.ok) throw new Error(`Couldn't load the schedule (${res.status}).`);
    B = await res.json();
  } catch (e) { $("#main").innerHTML = errNote(e); return; }
  B.days = []; for (let d = B.start; d <= B.end; d = addDays(d, 1)) B.days.push(d);
  B.days.forEach((d, i) => { dayIdx[d] = i; });
  B.techs.forEach((t) => { byId[t.id] = t; });
  B.holidays.forEach((h) => { holidays[h.date] = h; });
  B.ptoSet = Object.fromEntries(Object.entries(B.pto).map(([k, v]) => [k, new Set(v)]));
  loadDelta();
  rebuildIndex();
  document.querySelector(".tabs").onclick = (e) => { if (e.target.dataset.tab) go(e.target.dataset.tab); };
  $("#reset").onclick = reset;
  const fromHash = location.hash.slice(1);
  ui.tab = document.querySelector(`.tabs [data-tab="${fromHash}"]`) ? fromHash : "dashboard";
  window.addEventListener("hashchange", () => { const t = location.hash.slice(1); if (t && t !== ui.tab && document.querySelector(`.tabs [data-tab="${t}"]`)) go(t); });
  summaryState.data = hasChanges() ? null : B.summary;
  render();
  if (hasChanges()) refreshSummary();
}
boot();
