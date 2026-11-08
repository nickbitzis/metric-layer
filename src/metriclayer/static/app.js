// Metric Layer UI: catalog, query builder, chart (single y-axis), compiled SQL, and the ask box.
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6"];
const EXAMPLES = [
  "net revenue by country in 2026",
  "monthly active buyers trend",
  "conversion rate by traffic source",
  "average order value by device",
  "revenue by country",
  "refund rate by seller region",
];

const state = { metrics: [], byName: {}, dims: [], values: {}, picked: new Set(["gross_revenue"]), group: new Set(), filters: [], last: null };

async function api(path, body) {
  const r = await fetch(path, body ? { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) } : {});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error?.message || data.detail || r.statusText);
  return data;
}

function fmt(v, format) {
  if (v === null || v === undefined) return "";
  if (typeof v !== "number") return String(v);
  if (format === "percent") return (v * 100).toFixed(2) + "%";
  if (format === "currency") return "$" + v.toLocaleString(undefined, { maximumFractionDigits: Math.abs(v) < 1000 ? 2 : 0 });
  if (format === "decimal") return v.toFixed(2);
  return v.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

function short(v, format) {
  if (format === "percent") return (v * 100).toFixed(1) + "%";
  if (v === 0) return format === "currency" ? "$0" : "0";
  const a = Math.abs(v);
  const s = a >= 1e6 ? (v / 1e6).toFixed(1) + "M" : a >= 1e3 ? (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "k" : a >= 10 ? v.toFixed(0) : v.toFixed(2);
  return format === "currency" ? "$" + s : s;
}

// ---------- catalog ----------
function renderCatalog() {
  const q = $("filter").value.toLowerCase();
  const rows = state.metrics.filter((m) => !q || (m.name + m.label + m.description + m.synonyms.join(" ")).toLowerCase().includes(q));
  $("catalog").innerHTML = rows.map((m) => `
    <div class="metric">
      <button class="name" data-m="${esc(m.name)}">${esc(m.label)}</button>
      <span class="chip acc">${esc(m.type)}</span><span class="chip">${esc(m.owner)}</span>
      <div class="desc">${esc(m.description)}</div>
      <div class="lin">${esc(m.name)}: ${m.lineage.measures.map((x) => `${esc(x.agg)}(${esc(x.expr)})`).join(", ") || "derived from " + esc(m.lineage.depends_on.join(", "))}</div>
      <div class="lin">tables: ${esc(m.lineage.tables.join(", "))}</div>
    </div>`).join("") || `<p class="muted">No metric matches.</p>`;
  $("catalog").querySelectorAll("button.name").forEach((b) => (b.onclick = () => {
    state.picked = new Set([b.dataset.m]);
    state.group.clear();
    renderBuilder();
    run();
  }));
}

// ---------- builder ----------
function reachable() {
  const sets = [...state.picked].map((n) => new Set(state.byName[n]?.dimensions || []));
  return new Set(state.dims.map((d) => d.name).filter((d) => sets.every((s) => s.has(d))));
}

function renderBuilder() {
  $("metric-picks").innerHTML = state.metrics.map((m) => `
    <label class="pick ${state.picked.has(m.name) ? "on" : ""}" title="${esc(m.description)}">
      <input type="checkbox" data-m="${esc(m.name)}" ${state.picked.has(m.name) ? "checked" : ""}>${esc(m.label)}</label>`).join("");
  const ok = reachable();
  for (const g of [...state.group]) if (!ok.has(g)) state.group.delete(g);
  $("dim-picks").innerHTML = state.dims.map((d) => {
    const on = state.group.has(d.name), dis = !ok.has(d.name);
    return `<label class="pick ${on ? "on" : ""} ${dis ? "off" : ""}" title="${esc(d.description)}">
      <input type="checkbox" data-d="${esc(d.name)}" ${on ? "checked" : ""} ${dis ? "disabled" : ""}>${esc(d.name)}</label>`;
  }).join("");
  $("metric-picks").querySelectorAll("input").forEach((i) => (i.onchange = () => {
    i.checked ? state.picked.add(i.dataset.m) : state.picked.delete(i.dataset.m);
    renderBuilder();
  }));
  $("dim-picks").querySelectorAll("input").forEach((i) => (i.onchange = () => {
    i.checked ? state.group.add(i.dataset.d) : state.group.delete(i.dataset.d);
    renderBuilder();
  }));
  $("f-dim").innerHTML = state.dims.filter((d) => ok.has(d.name)).map((d) => `<option>${esc(d.name)}</option>`).join("");
  fillValues();
  renderFilters();
}

function fillValues() {
  const vals = state.values[$("f-dim").value] || [];
  $("f-val").innerHTML = vals.map((v) => `<option>${esc(v)}</option>`).join("");
}

function renderFilters() {
  $("filters").innerHTML = state.filters.map((f, i) => `<span class="chip acc">${esc(f.dimension)} ${esc(f.op)} ${esc(Array.isArray(f.value) ? f.value.join(", ") : f.value)}<button data-i="${i}" aria-label="Remove filter">x</button></span>`).join("");
  $("filters").querySelectorAll("button").forEach((b) => (b.onclick = () => { state.filters.splice(+b.dataset.i, 1); renderFilters(); }));
}

function currentQuery() {
  const q = { metrics: [...state.picked], group_by: [...state.group], filters: state.filters };
  if ($("grain").value) q.grain = $("grain").value;
  if ($("start").value && $("end").value) q.time_range = { start: $("start").value, end: $("end").value };
  return q;
}

function loadQuery(q) {
  state.picked = new Set(q.metrics);
  state.group = new Set(q.group_by.filter((d) => d !== "metric_time"));
  state.filters = q.filters;
  $("grain").value = q.grain || "";
  $("start").value = q.time_range?.start || "";
  $("end").value = q.time_range?.end || "";
  renderBuilder();
}

// ---------- results ----------
function showError(msg) { $("error").innerHTML = msg ? `<div class="err">${esc(msg)}</div>` : ""; }

function renderResult(res) {
  state.last = res;
  const metrics = res.query.metrics;
  const sel = $("chart-metric");
  const keep = metrics.includes(sel.value) ? sel.value : metrics[0];
  sel.innerHTML = metrics.map((m) => `<option value="${esc(m)}">${esc(state.byName[m]?.label || m)}</option>`).join("");
  sel.value = keep;
  drawChart();
  const fmtOf = (c) => state.byName[c]?.format;
  $("table").innerHTML = `<table><thead><tr>${res.columns.map((c) => `<th class="${fmtOf(c) ? "num" : ""}">${esc(c)}</th>`).join("")}</tr></thead>
    <tbody>${res.rows.slice(0, 200).map((r) => `<tr>${r.map((v, i) => `<td class="${fmtOf(res.columns[i]) ? "num" : ""}">${esc(fmt(v, fmtOf(res.columns[i])))}</td>`).join("")}</tr>`).join("")}</tbody></table>
    ${res.rows.length > 200 ? `<p class="muted">Showing 200 of ${res.rows.length} rows.</p>` : ""}`;
  $("sql").textContent = res.sql;
  $("sql").classList.remove("muted");
  $("status").textContent = `${res.row_count} rows in ${res.elapsed_ms} ms, cache ${res.cache_hit ? "hit" : "miss"}${res.preaggregations_used.length ? ", pre-aggregation " + res.preaggregations_used.join(", ") : ""}`;
  explain(res.query, false);
}

function drawChart() {
  const res = state.last;
  if (!res) return;
  const metric = $("chart-metric").value;
  const format = state.byName[metric]?.format;
  const mi = res.columns.indexOf(metric);
  const timeCol = res.columns.findIndex((c) => c.startsWith("metric_time__"));
  const dimCols = res.columns.map((c, i) => i).filter((i) => i !== timeCol && !res.query.metrics.includes(res.columns[i]));
  const label = (r) => dimCols.map((i) => r[i] ?? "(none)").join(" / ") || metric;
  const color = (i) => `var(${SERIES[i % SERIES.length]})`;
  if (!res.rows.length) { $("chart").innerHTML = `<p class="muted">No rows.</p>`; return; }
  if (timeCol < 0 && !dimCols.length) {
    $("chart").innerHTML = `<div class="big">${esc(fmt(res.rows[0][mi], format))}<span>${esc(state.byName[metric]?.label || metric)}</span></div>`;
    return;
  }
  const W = 760, H = 300, L = 56, R = 12, T = 12, B = 34;
  const vals = res.rows.map((r) => r[mi]).filter((v) => typeof v === "number");
  const max = Math.max(0, ...vals), min = Math.min(0, ...vals);
  const y = (v) => T + (H - T - B) * (1 - (v - min) / ((max - min) || 1));
  let svg = "";
  for (let k = 0; k <= 4; k++) {
    const v = min + ((max - min) * k) / 4;
    svg += `<line class="gridline" x1="${L}" x2="${W - R}" y1="${y(v)}" y2="${y(v)}"/><text x="${L - 6}" y="${y(v) + 4}" text-anchor="end">${esc(short(v, format))}</text>`;
  }
  let legend = [];
  if (timeCol >= 0) {
    const xs = [...new Set(res.rows.map((r) => r[timeCol]))].sort();
    const x = (t) => L + ((W - L - R) * xs.indexOf(t)) / Math.max(1, xs.length - 1);
    const groups = new Map();
    for (const r of res.rows) { const k = label(r); if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); }
    const ranked = [...groups.entries()].sort((a, b) => b[1].reduce((s, r) => s + (r[mi] || 0), 0) - a[1].reduce((s, r) => s + (r[mi] || 0), 0)).slice(0, 6);
    ranked.forEach(([k, rows], i) => {
      const pts = rows.filter((r) => typeof r[mi] === "number").sort((a, b) => (a[timeCol] < b[timeCol] ? -1 : 1)).map((r) => `${x(r[timeCol]).toFixed(1)},${y(r[mi]).toFixed(1)}`);
      svg += `<polyline fill="none" stroke="${color(i)}" stroke-width="2" points="${pts.join(" ")}"/>`;
      legend.push(`<span><i style="background:${color(i)}"></i>${esc(k)}</span>`);
    });
    const step = Math.ceil(xs.length / 8);
    xs.forEach((t, i) => { if (i % step === 0) svg += `<text x="${x(t)}" y="${H - 12}" text-anchor="middle">${esc(String(t).slice(0, 10))}</text>`; });
    if (groups.size > 6) legend.push(`<span>top 6 of ${groups.size} series shown</span>`);
  } else {
    const rows = [...res.rows].filter((r) => typeof r[mi] === "number").sort((a, b) => b[mi] - a[mi]).slice(0, 16);
    const bw = (W - L - R) / rows.length;
    rows.forEach((r, i) => {
      const x0 = L + i * bw + bw * 0.15, top = y(Math.max(0, r[mi])), bot = y(Math.min(0, r[mi]));
      svg += `<rect x="${x0}" y="${top}" width="${bw * 0.7}" height="${Math.max(1, bot - top)}" fill="${color(0)}" rx="2"><title>${esc(label(r))}: ${esc(fmt(r[mi], format))}</title></rect>`;
      svg += `<text x="${x0 + bw * 0.35}" y="${H - 12}" text-anchor="middle">${esc(String(label(r)).slice(0, 12))}</text>`;
    });
    if (res.rows.length > 16) legend.push(`<span>top 16 of ${res.rows.length} shown</span>`);
  }
  $("chart").innerHTML = `<div class="legend">${legend.join("")}</div><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(metric)} chart">${svg}</svg>`;
}

async function run() {
  showError("");
  $("status").textContent = "Running...";
  try { renderResult(await api("/api/query", currentQuery())); } catch (e) { $("status").textContent = ""; showError(e.message); }
}

async function explain(q, showSql = true) {
  try {
    const ex = await api("/api/explain", q || currentQuery());
    $("plan").innerHTML = `<div class="plan">${ex.ctes.map((c) => `<div class="cte"><code>${esc(c.name)}</code>
      <span class="chip ${c.kind === "cumulative" ? "warn" : "acc"}">${esc(c.kind)}</span>
      reads <code>${esc(c.source)}</code>, measures ${esc(c.measures.join(", "))}
      ${c.joins.length ? "<br>joins: " + c.joins.map((j) => `${esc(j.from)} to ${esc(j.to)} (${esc(j.cardinality)}, ${esc(j.on)})`).join("; ") : c.source.startsWith("preagg_") ? "<br>served from the pre-aggregation table" : "<br>no joins"}</div>`).join("")}</div>`;
    if (showSql) { $("sql").textContent = ex.sql; $("sql").classList.remove("muted"); showError(""); }
  } catch (e) { showError(e.message); }
}

// ---------- ask ----------
function describe(q) {
  const parts = [`<span class="k">metrics</span>${q.metrics.map((m) => `<span class="chip acc">${esc(m)}</span>`).join("")}`];
  const dims = q.group_by.filter((d) => d !== "metric_time");
  if (q.grain) parts.push(`<span class="k">grain</span><span class="chip">${esc(q.grain)}</span>`);
  if (dims.length) parts.push(`<span class="k">by</span>${dims.map((d) => `<span class="chip">${esc(d)}</span>`).join("")}`);
  if (q.filters.length) parts.push(`<span class="k">where</span>${q.filters.map((f) => `<span class="chip">${esc(f.dimension)} ${esc(f.op)} ${esc(Array.isArray(f.value) ? f.value.join(", ") : f.value)}</span>`).join("")}`);
  if (q.time_range) parts.push(`<span class="k">from</span><span class="chip">${esc(q.time_range.start)} to ${esc(q.time_range.end)}</span>`);
  return parts.join(" ");
}

async function ask(question) {
  $("question").value = question;
  $("interp").innerHTML = `<div class="interp muted">Interpreting...</div>`;
  try {
    const out = await api("/api/ask", { question });
    const it = out.interpretation;
    const notes = it.notes.length ? `<ul>${it.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : "";
    if (it.status === "clarify") {
      $("interp").innerHTML = `<div class="interp clarify"><strong>I need clarification.</strong> ${esc(it.clarification)}${notes}</div>`;
      return;
    }
    $("interp").innerHTML = `<div class="interp"><strong>Interpreted as</strong> ${describe(it.query)} <span class="muted">(${esc(it.provider)})</span>${notes}</div>`;
    loadQuery(it.query);
    renderResult(out.result);
  } catch (e) { $("interp").innerHTML = `<div class="err">${esc(e.message)}</div>`; }
}

async function init() {
  const [m, d, info] = await Promise.all([api("/api/metrics"), api("/api/dimensions"), api("/api/info")]);
  state.metrics = m.metrics;
  state.byName = Object.fromEntries(m.metrics.map((x) => [x.name, x]));
  state.dims = d.dimensions;
  state.values = d.values;
  $("info").textContent = `data version ${info.data_version}, today is ${info.anchor_date}`;
  $("examples").innerHTML = "Try: " + EXAMPLES.map((e) => `<a data-q="${esc(e)}">${esc(e)}</a>`).join("");
  $("examples").querySelectorAll("a").forEach((a) => (a.onclick = () => ask(a.dataset.q)));
  $("filter").oninput = renderCatalog;
  $("f-dim").onchange = fillValues;
  $("f-add").onclick = () => {
    const vals = [...$("f-val").selectedOptions].map((o) => o.value);
    if (!vals.length) return;
    const op = $("f-op").value;
    state.filters.push({ dimension: $("f-dim").value, op, value: op === "in" || op === "not_in" ? vals : vals[0] });
    renderFilters();
  };
  $("run").onclick = run;
  $("explain").onclick = () => explain();
  $("chart-metric").onchange = drawChart;
  $("ask").onsubmit = (e) => { e.preventDefault(); if ($("question").value.trim()) ask($("question").value.trim()); };
  renderCatalog();
  renderBuilder();
  const params = new URLSearchParams(location.search);
  if (params.get("q")) ask(params.get("q"));
  else { $("grain").value = "month"; state.group = new Set(["customer__country"]); renderBuilder(); run(); }
}

init();
