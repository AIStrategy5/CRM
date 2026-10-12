"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const label = (s) => String(s ?? "").replace(/_/g, " ");
const fmt = (v, d = 2) => (v === null || v === undefined ? "–" : Number(v).toFixed(d));
const fmtP = (p) => (p === null || p === undefined ? "=–" : p < 0.001 ? "<0.001" : "=" + Number(p).toFixed(3));

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(typeof err.detail === "string" ? err.detail : JSON.stringify(err.detail || res.statusText));
  }
  return res.status === 204 ? null : res.json();
}
const formData = (form) => Object.fromEntries([...new FormData(form)].filter(([, v]) => v !== ""));

let META = {}, KOLS = [];
const charts = {};
function chart(id, cfg) {
  charts[id]?.destroy();
  const css = getComputedStyle(document.documentElement);
  Chart.defaults.color = css.getPropertyValue("--muted").trim();
  Chart.defaults.borderColor = css.getPropertyValue("--border").trim();
  charts[id] = new Chart($("#" + id), cfg);
}
const ACCENT = () => getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
const SEG_COLORS = { strategic_partner: "#1f8a5b", priority_gap: "#c0392b", engaged_advocate: "#2f6fdb", monitor: "#98a2b3" };

// ------------------------------------------------------------------ navigation
$$("#nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
function show(view) {
  $$("#nav button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + view));
  ({ dashboard: loadDashboard, kols: loadKols, interactions: loadInteractions, ai: loadInsights,
     actions: loadActions, analytics: loadAnalytics })[view]?.();
}

// ------------------------------------------------------------------ bootstrap
async function init() {
  META = await api("/api/meta");
  $("#engine").textContent = META.ai_engine === "claude" ? `AI: Claude (${META.model})` : "AI: rule-based (no API key)";
  const opt = (arr) => arr.map((x) => `<option value="${esc(x)}">${esc(label(x))}</option>`).join("");
  $("#interactionForm [name=type]").innerHTML = opt(META.interaction_types);
  $("#interactionForm [name=channel]").innerHTML = opt(META.channels);
  $("#insightForm [name=category]").innerHTML = opt(META.insight_categories);
  $("#actionForm [name=owner_function]").innerHTML = opt(META.owner_functions);
  $("#interactionForm [name=date]").valueAsDate = new Date();
  await refreshKolOptions();
  loadDashboard();
}
async function refreshKolOptions() {
  KOLS = await api("/api/kols");
  const kolOpts = KOLS.map((k) => `<option value="${k.id}">${esc(k.name)} · ${esc(k.therapeutic_area)}</option>`).join("");
  $("#interactionForm [name=kol_id]").innerHTML = kolOpts;
  $("#insightForm [name=kol_id]").innerHTML = `<option value="">(no KOL)</option>` + kolOpts;
  $("#aiKol").innerHTML = `<option value="">All</option>` + kolOpts;
  const tas = [...new Set(KOLS.map((k) => k.therapeutic_area))].sort();
  const taOpts = tas.map((t) => `<option>${esc(t)}</option>`).join("");
  $("#kolTA").innerHTML = `<option value="">All therapeutic areas</option>` + taOpts;
  $("#aiTA").innerHTML = `<option value="">All</option>` + taOpts;
  $("#taList").innerHTML = taOpts;
}

// ------------------------------------------------------------------ dashboard
async function loadDashboard() {
  const r = await api("/api/analytics");
  const o = r.overview;
  const kpi = (v, l) => `<div class="kpi"><div class="v">${v}</div><div class="l">${l}</div></div>`;
  $("#kpis").innerHTML = [
    kpi(o.kols, "KOLs"),
    kpi(o.interactions_in_window, `Interactions (last ${o.window_days}d)`),
    kpi(o.kols_engaged_in_window, `KOLs engaged (last ${o.window_days}d)`),
    kpi(o.tier1_coverage_pct === null ? "–" : o.tier1_coverage_pct + "%", "Tier 1 coverage"),
    kpi(fmt(o.sentiment.mean), `Mean sentiment (95% CI ${fmt(o.sentiment.ci_low)}–${fmt(o.sentiment.ci_high)})`),
    kpi(o.insights, "Insights captured"),
    kpi(`${o.open_actions}<span class="muted" style="font-size:14px"> / ${o.overdue_actions} overdue</span>`, "Open actions"),
  ].join("");

  const m = r.monthly.months;
  chart("chMonthly", { type: "bar", data: { labels: m.map((x) => x.month), datasets: [
    { label: "Interactions", data: m.map((x) => x.interactions), backgroundColor: ACCENT() },
    { label: "Mean sentiment", data: m.map((x) => x.avg_sentiment), type: "line", yAxisID: "y1", borderColor: "#b7791f", backgroundColor: "#b7791f" },
  ] }, options: { scales: { y: { beginAtZero: true }, y1: { position: "right", min: -1, max: 1, grid: { drawOnChartArea: false } } } } });

  const segs = Object.keys(SEG_COLORS);
  chart("chScatter", { type: "scatter", data: { datasets: segs.map((s) => ({
    label: label(s), backgroundColor: SEG_COLORS[s],
    data: r.engagement.filter((e) => e.segment === s).map((e) => ({ x: e.influence, y: e.engagement, name: e.name })),
  })) }, options: { plugins: { tooltip: { callbacks: { label: (c) => `${c.raw.name}: influence ${c.raw.x}, engagement ${c.raw.y}` } } },
    scales: { x: { title: { display: true, text: "Influence" }, min: 0, max: 100 }, y: { title: { display: true, text: "Engagement" }, min: 0, max: 100 } } } });

  const gaps = r.engagement.filter((e) => e.segment === "priority_gap").sort((a, b) => b.influence - a.influence).slice(0, 8);
  $("#gapList").innerHTML = gaps.length ? `<table><tr><th>KOL</th><th>Tier</th><th>Influence</th><th>Engagement</th><th>Last contact</th></tr>` +
    gaps.map((g) => `<tr><td>${esc(g.name)}</td><td>${g.tier}</td><td>${g.influence}</td><td>${g.engagement}</td><td>${g.last_interaction ?? "never"}</td></tr>`).join("") + "</table>"
    : `<p class="hint">No priority gaps detected.</p>`;

  const cats = Object.entries(o.by_category);
  chart("chCats", { type: "bar", data: { labels: cats.map(([c]) => label(c)), datasets: [{ label: "Insights", data: cats.map(([, n]) => n), backgroundColor: ACCENT() }] },
    options: { indexAxis: "y", plugins: { legend: { display: false } } } });
}

// ------------------------------------------------------------------ KOLs
let kolSort = { key: "engagement", dir: -1 };
async function loadKols() {
  const q = new URLSearchParams({ q: $("#kolSearch").value, therapeutic_area: $("#kolTA").value, tier: $("#kolTier").value });
  [...q.keys()].forEach((k) => !q.get(k) && q.delete(k));
  const list = await api("/api/kols?" + q);
  list.sort((a, b) => ((a[kolSort.key] ?? -1e9) > (b[kolSort.key] ?? -1e9) ? 1 : -1) * kolSort.dir);
  const cols = [["name", "Name"], ["specialty", "Specialty"], ["therapeutic_area", "TA"], ["tier", "Tier"], ["institution", "Institution"],
    ["interactions", "#Int."], ["engagement", "Engagement"], ["influence", "Influence"], ["avg_sentiment", "Sentiment"], ["segment", "Segment"]];
  $("#kolTable").innerHTML = `<tr>${cols.map(([k, l]) => `<th data-k="${k}">${l}${kolSort.key === k ? (kolSort.dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr>` +
    list.map((k) => `<tr class="click" data-id="${k.id}">${cols.map(([c]) => c === "segment"
      ? `<td><span class="tag ${esc(k.segment)}">${esc(label(k.segment))}</span></td>` : `<td>${esc(k[c] ?? "–")}</td>`).join("")}</tr>`).join("");
  $$("#kolTable th").forEach((th) => th.onclick = () => { kolSort = { key: th.dataset.k, dir: kolSort.key === th.dataset.k ? -kolSort.dir : -1 }; loadKols(); });
  $$("#kolTable tr.click").forEach((tr) => tr.onclick = () => showKol(tr.dataset.id));
}
["input", "change"].forEach((ev) => { $("#kolSearch").addEventListener(ev, loadKols); });
$("#kolTA").onchange = loadKols; $("#kolTier").onchange = loadKols;

async function showKol(id) {
  const k = await api("/api/kols/" + id);
  $("#kolDetail").innerHTML = `<div class="card"><h3>${esc(k.name)} <span class="muted">${esc(k.specialty)} · ${esc(k.institution)} · ${esc(k.region)} · Tier ${k.tier}</span></h3>
    <p>Influence ${k.influence} · ${k.publications} publications · ${k.trials_involved} trials · ${esc(k.email)}</p>
    <div class="row" style="flex:0"><button class="small" id="editKol">Edit</button><button class="small" id="synthKol">AI synthesis for this KOL</button>
      <button class="small" id="delKol">Delete</button></div>
    <h3>Interactions (${k.interactions.length})</h3>
    <div class="list">${k.interactions.map(renderInteraction).join("") || '<p class="hint">None yet.</p>'}</div>
    <h3>Insights (${k.insights.length})</h3>
    <div class="list">${k.insights.map((s) => `<div><span class="tag">${esc(label(s.category))}</span> ${esc(s.text)}</div>`).join("") || '<p class="hint">None yet.</p>'}</div></div>`;
  $("#editKol").onclick = () => openKolDialog(k);
  $("#synthKol").onclick = () => { show("ai"); $("#aiKol").value = k.id; runSynthesis(); };
  $("#delKol").onclick = async () => { if (confirm(`Delete ${k.name} and all their interactions?`)) { await api("/api/kols/" + id, { method: "DELETE" }); $("#kolDetail").innerHTML = ""; await refreshKolOptions(); loadKols(); } };
  $("#kolDetail").scrollIntoView({ behavior: "smooth" });
}
function openKolDialog(k = null) {
  const f = $("#kolForm"); f.reset();
  $("#kolDialogTitle").textContent = k ? "Edit KOL" : "New KOL";
  if (k) Object.entries(k).forEach(([n, v]) => { if (f.elements[n] && typeof v !== "object") f.elements[n].value = v ?? ""; });
  else f.elements.id.value = "";
  $("#kolDialog").showModal();
}
$("#btnNewKol").onclick = () => openKolDialog();
$("#kolForm").addEventListener("submit", async (e) => {
  if (e.submitter?.value !== "save") return;
  e.preventDefault();
  const d = formData(e.target); const id = d.id; delete d.id;
  ["tier", "publications", "trials_involved"].forEach((n) => d[n] = Number(d[n] || 0));
  try {
    await api(id ? "/api/kols/" + id : "/api/kols", { method: id ? "PUT" : "POST", body: d });
    $("#kolDialog").close(); await refreshKolOptions(); loadKols(); if (id) showKol(id);
  } catch (err) { alert(err.message); }
});

// ------------------------------------------------------------------ interactions
const renderInteraction = (i) => `<div><b>${esc(i.date)}</b> · <span class="tag">${esc(label(i.type))}</span>
  ${i.kol_name ? esc(i.kol_name) + " · " : ""}<span class="muted">${esc(i.msl_owner ?? "")} · sentiment ${fmt(i.sentiment, 1)}</span>
  <div>${esc(i.notes)}</div></div>`;
async function loadInteractions() {
  const list = await api("/api/interactions?limit=40");
  $("#interactionList").innerHTML = list.map(renderInteraction).join("");
}
$("#interactionForm [name=sentiment]").oninput = (e) => ($("#sentOut").textContent = e.target.value);
$("#interactionForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData(e.target);
  d.kol_id = Number(d.kol_id); d.sentiment = Number(d.sentiment);
  if (d.duration_min) d.duration_min = Number(d.duration_min);
  try {
    const r = await api("/api/interactions", { method: "POST", body: d });
    const flags = r.compliance_flags.map((f) => `<div class="flag"><b>${esc(label(f.flag_type))}</b>: ${esc(f.recommended_step)}</div>`).join("");
    $("#interactionFeedback").innerHTML = `<div class="alert">Saved. Suggested insight categories:
      ${r.suggested_categories.map((c) => `<span class="tag">${esc(label(c))}</span>`).join("") || "none detected"}</div>${flags}`;
    e.target.elements.notes.value = ""; e.target.elements.topics.value = "";
    loadInteractions();
  } catch (err) { alert(err.message); }
});

// ------------------------------------------------------------------ AI synthesis + insights
let lastSynthesis = null;
$("#btnSynth").onclick = runSynthesis;
async function runSynthesis() {
  const body = { therapeutic_area: $("#aiTA").value || null, kol_id: Number($("#aiKol").value) || null,
    since: $("#aiSince").value || null, max_interactions: Number($("#aiMax").value) || 60, use_ai: $("#aiUse").checked };
  $("#aiResult").innerHTML = `<div class="card">Synthesizing…</div>`;
  try {
    const r = await api("/api/ai/synthesize", { method: "POST", body });
    lastSynthesis = r.result;
    const s = r.result;
    const ids = (a) => a.length ? `<span class="hint">evidence: interactions ${a.join(", ")}</span>` : "";
    $("#aiResult").innerHTML = `<div class="card">
      <h3>Result <span class="pill">${esc(r.engine)}${r.model ? " · " + esc(r.model) : ""}</span> <span class="hint">${r.scope.interactions} interactions in scope</span></h3>
      ${r.warning ? `<div class="alert">${esc(r.warning)}</div>` : ""}
      <p>${esc(s.executive_summary)}</p>
      ${s.compliance_flags.length ? `<h3>⚠ Compliance flags (${s.compliance_flags.length})</h3>` + s.compliance_flags.map((f) =>
        `<div class="flag"><b>${esc(label(f.flag_type))}</b> · interaction ${f.interaction_id}<br><i>${esc(f.excerpt)}</i><br>${esc(f.recommended_step)}</div>`).join("") : ""}
      <h3>Themes</h3><div class="list">${s.themes.map((t) => `<div><b>${esc(t.title)}</b> <span class="tag">${esc(label(t.category))}</span>
        <span class="tag">${t.kol_count} KOLs</span><div>${esc(t.summary)}</div>${ids(t.supporting_interaction_ids)}</div>`).join("") || '<p class="hint">None.</p>'}</div>
      <h3>Actionable outcomes</h3><div class="list">${s.actionable_outcomes.map((o, i) => `<div><label class="check"><input type="checkbox" class="pickOutcome" data-i="${i}" checked>
        <b>${esc(o.title)}</b></label> <span class="tag ${esc(o.priority)}">${esc(o.priority)}</span> <span class="tag">${esc(o.owner_function)}</span>
        <div>${esc(o.rationale)}</div>${ids(o.supporting_interaction_ids)}</div>`).join("") || '<p class="hint">None.</p>'}</div>
      ${s.knowledge_gaps.length ? `<h3>Knowledge gaps</h3><ul>${s.knowledge_gaps.map((g) => `<li>${esc(g)}</li>`).join("")}</ul>` : ""}
      <button class="primary" id="btnAccept">Save themes as insights &amp; selected outcomes as actions</button></div>`;
    $("#btnAccept").onclick = async () => {
      const picked = $$(".pickOutcome").filter((c) => c.checked).map((c) => lastSynthesis.actionable_outcomes[c.dataset.i]);
      const res = await api("/api/ai/accept", { method: "POST", body: { themes: lastSynthesis.themes, actionable_outcomes: picked } });
      alert(`Saved ${res.insights.length} insights and ${res.actions.length} actions.`);
      loadInsights();
    };
  } catch (err) { $("#aiResult").innerHTML = `<div class="card alert">${esc(err.message)}</div>`; }
}
async function loadInsights() {
  const list = await api("/api/insights");
  $("#insightList").innerHTML = list.slice(0, 100).map((s) => `<div><span class="tag">${esc(label(s.category))}</span>
    <span class="tag">${esc(s.source)}</span> ${esc(s.text)} <span class="hint">${esc(s.kol_name ?? "")} · ${esc(s.therapeutic_area ?? "")}</span></div>`).join("");
}
$("#insightForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const d = formData(e.target); if (d.kol_id) d.kol_id = Number(d.kol_id);
  try { await api("/api/insights", { method: "POST", body: d }); e.target.elements.text.value = ""; loadInsights(); }
  catch (err) { alert(err.message); }
});

// ------------------------------------------------------------------ actions
async function loadActions() {
  const list = await api("/api/actions");
  const today = new Date().toISOString().slice(0, 10);
  ["open", "in_progress", "done"].forEach((st) => {
    $("#col-" + st).innerHTML = list.filter((a) => a.status === st).map((a) => {
      const overdue = a.status !== "done" && a.due_date && a.due_date < today;
      return `<div class="task ${overdue ? "overdue" : ""}"><b>${esc(a.title)}</b>
        <div><span class="tag ${esc(a.priority)}">${esc(a.priority)}</span><span class="tag">${esc(a.owner_function)}</span>${a.source === "ai" ? '<span class="tag">AI</span>' : ""}</div>
        ${a.rationale ? `<div class="hint">${esc(a.rationale)}</div>` : ""}
        <div class="hint">Due ${esc(a.due_date ?? "–")}${overdue ? ' · <b style="color:var(--bad)">overdue</b>' : ""}</div>
        <select data-id="${a.id}" class="small statusSel">${["open", "in_progress", "done"].map((s) => `<option value="${s}" ${s === a.status ? "selected" : ""}>${label(s)}</option>`).join("")}</select>
        <button class="small delAct" data-id="${a.id}">Delete</button></div>`;
    }).join("");
  });
  $$(".statusSel").forEach((s) => s.onchange = async () => { await api("/api/actions/" + s.dataset.id, { method: "PATCH", body: { status: s.value } }); loadActions(); });
  $$(".delAct").forEach((b) => b.onclick = async () => { if (confirm("Delete action?")) { await api("/api/actions/" + b.dataset.id, { method: "DELETE" }); loadActions(); } });
}
$("#actionForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try { await api("/api/actions", { method: "POST", body: formData(e.target) }); e.target.reset(); loadActions(); }
  catch (err) { alert(err.message); }
});

// ------------------------------------------------------------------ analytics
const testHtml = (t) => !t ? '<p class="hint">Not enough data for this test.</p>' :
  `<p><span class="${t.significant ? "sig" : "nsig"}">${t.significant ? "Significant" : "Not significant"}</span> · ${esc(t.test)} ·
   ${t.n ? `n=${t.n} · ` : ""}p${fmtP(t.p_value)}<br>${esc(t.interpretation ?? "")}${t.warning ? `<br><span class="hint">⚠ ${esc(t.warning)}</span>` : ""}</p>`;

function ciChart(id, groups, labeller) {
  const keys = Object.keys(groups);
  chart(id, { type: "bar", data: { labels: keys.map(labeller), datasets: [
    { label: "Mean sentiment", data: keys.map((k) => groups[k].mean), backgroundColor: ACCENT() },
    { label: "95% CI", type: "line", showLine: false, pointStyle: "line", pointRadius: 12, borderColor: "#b7791f",
      data: keys.map((k) => groups[k].ci_low) },
    { label: "", type: "line", showLine: false, pointStyle: "line", pointRadius: 12, borderColor: "#b7791f",
      data: keys.map((k) => groups[k].ci_high) },
  ] }, options: { plugins: { legend: { labels: { filter: (l) => l.text } },
    tooltip: { callbacks: { afterBody: (c) => `n=${groups[keys[c[0].dataIndex]].n}` } } }, scales: { y: { min: -1, max: 1 } } } });
}
async function loadAnalytics() {
  const r = await api("/api/analytics");
  ciChart("chTier", r.sentiment_by_tier.groups, (k) => "Tier " + k);
  $("#tierTest").innerHTML = testHtml(r.sentiment_by_tier.test);
  ciChart("chType", r.sentiment_by_type.groups, label);
  $("#typeTest").innerHTML = testHtml(r.sentiment_by_type.test);
  const tr = r.monthly.trend;
  $("#tests").innerHTML = `<h4>Engagement trend</h4>${tr ? `<p><span class="${tr.significant ? "sig" : "nsig"}">${tr.significant ? "Significant" : "Not significant"}</span> ·
    ${esc(tr.test)} · ${tr.n_months} months · slope ${tr.slope_per_month}/month · R²=${tr.r_squared} · p${fmtP(tr.p_value)}<br>${esc(tr.interpretation)}</p>` : '<p class="hint">Need ≥3 months.</p>'}
    <h4>Influence vs engagement alignment</h4>${testHtml(r.influence_vs_engagement && { ...r.influence_vs_engagement })}
    ${r.influence_vs_engagement ? `<p class="hint">rho=${r.influence_vs_engagement.rho}</p>` : ""}
    <h4>Insight category independence from therapeutic area</h4>${testHtml(r.insight_association)}
    ${r.insight_association ? `<p class="hint">χ²=${r.insight_association.chi2}, dof=${r.insight_association.dof}, Cramér's V=${r.insight_association.cramers_v}</p>` : ""}
    <h4>Segments</h4><p>${Object.entries(r.segments).map(([s, n]) => `<span class="tag ${esc(s)}">${esc(label(s))}: ${n}</span>`).join(" ")}</p>`;
  const x = r.insight_association;
  $("#xtab").innerHTML = x ? `<table><tr><th></th>${x.cols.map((c) => `<th>${esc(label(c))}</th>`).join("")}</tr>` +
    x.rows.map((rw, i) => `<tr><th>${esc(rw)}</th>${x.table[i].map((n) => `<td>${n}</td>`).join("")}</tr>`).join("") + "</table>" : '<p class="hint">Not enough data.</p>';
  $("#engTable").innerHTML = `<tr><th>KOL</th><th>Tier</th><th>TA</th><th>#Int.</th><th>Days since last</th><th>Engagement</th><th>Influence</th><th>Sentiment</th><th>Insights</th><th>Segment</th></tr>` +
    r.engagement.map((e) => `<tr><td>${esc(e.name)}</td><td>${e.tier}</td><td>${esc(e.therapeutic_area)}</td><td>${e.interactions}</td><td>${e.days_since_last ?? "–"}</td>
      <td>${e.engagement}</td><td>${e.influence}</td><td>${fmt(e.avg_sentiment)}</td><td>${e.insights}</td><td><span class="tag ${esc(e.segment)}">${esc(label(e.segment))}</span></td></tr>`).join("");
  $("#notes").innerHTML = r.notes.map((n) => `• ${esc(n)}`).join("<br>");
}

init().catch((e) => { document.body.insertAdjacentHTML("afterbegin", `<div class="alert">Failed to load: ${esc(e.message)}</div>`); });
