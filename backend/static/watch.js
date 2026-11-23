/* ==========================================================================
   WCH — the watchlist.

   Every US and European company above $5bn, by sector, with the ones worth
   a look pulled out: well below the 52-week high, while the business still
   checks out and the analysts are not cutting their forecasts. The server
   scans once a weekday evening (pf/watch.py); this page sorts what the scan
   found by the preset and the pullback you choose, without asking again.

   Since build 27 it also reads the market's mood (a dip of its own in a calm
   market needs a higher score) and lists the momentum leaders: the strongest
   twelve-month performers, the one factor of Nexus's that held up.

   A starting point, not advice.
   ========================================================================== */

let V = null;                     // /api/pf/wl
let PRE = "balanced", THR = 20;   // saved on the server: the alerts and the brief use them too
const F = { region: "all", cap: 5e9, trend: "any", q: "", close: true };
let POLL = 0, NOTE_T = 0, OPEN_SYM = null, MREG = "US";

/* ---------- formatting ---------------------------------------------------- */
const fx = (v, d = 0) => v == null || isNaN(v) ? "–" :
  Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
const sg = (v, d = 0) => {
  if (v == null || isNaN(v)) return "–";
  const a = Number(Math.abs(v).toFixed(d));       // −0.3 at no decimals is 0, not "−0"
  return (a === 0 ? "" : v > 0 ? "+" : "−") + fx(a, d);
};
const sgc = (v, d = 0, suf = "") => v == null ? `<span class="dim">–</span>` : `<span class="${sgn(v)}">${sg(v, d)}${suf}</span>`;
const usdbn = v => v == null ? "–" : v >= 1e12 ? "$" + fx(v / 1e12, 2) + "tn" : "$" + fx(v / 1e9, 0) + "bn";
const api = async (u, o) => { const r = await fetch(u, o); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status); return r.json(); };
const post = (u, b) => api(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
const pxf = (v, ccy) => v == null ? "–" : fx(v, v >= 1000 ? 0 : 2) + (ccy && ccy !== "USD" ? ` <span class="dim">${esc(ccy)}</span>` : "");
const when = ts => ts ? dmyTime(new Date(ts * 1000).toISOString()) : "–";
const shortSym = s => s;
const TREND = { "falling": ["falling", "tr-falling", "still making new lows: the last one within a week"],
  "basing": ["basing", "tr-basing", "no new low for a week or more"],
  "recovering": ["recovering", "tr-recovering", "above its 50-day average and 8% or more off the low"],
  "at the high": ["at the high", "tr-at", "within 3% of its 52-week high"] };
const STATUS = { opp: "opportunity", close: "close", reason: "down for a reason", weak: "not strong enough",
  near: "near its high", nodata: "no data yet" };
const GCOL = v => v == null ? "#2a3840" : v >= 70 ? "#3fb380" : v >= 45 ? "#d9a441" : "#d0604c";
const GLET = { quality: "Q", growth: "G", estimates: "E", value: "V", income: "I" };

function status(r) {
  const dd = r.pr ? r.pr.dd52 : r.dd52;
  if (dd == null) return "nodata";
  if (dd > -THR) return "near";
  if (!r.ck) return "nodata";
  if (r.broken) return "reason";
  const j = (r.ps || {})[PRE] || {};
  if (j.ok) return "opp";
  if (j.close) return "close";
  return "weak";
}
/* why a stock is where it is, in a few words */
function verdict(r) {
  const j = (r.ps || {})[PRE] || {}, P = V.presets[PRE];
  if (r.broken) return r.broken;
  if (j.ok) return "fails nothing this preset counts";
  const lab = k => V.checks[k].label.toLowerCase();
  if ((j.must || []).length) return "fails a must-pass check: " + j.must.map(lab).join(", ");
  if ((j.fails || []).length) return "fails " + j.fails.map(lab).join(", ");
  const ny = (r.ck || []).find(c => c[0] === "e_ny90");
  if (!ny || ny[1] === "na") return "forecasts not judged: fewer than three analysts";
  if (j.bar > P.min && (j.score || 0) >= P.min) return `scores ${fx(j.score, 0)}: a fall of its own in a calm market needs ${j.bar}`;
  return `scores ${fx(j.score, 0)}, under the ${j.bar || P.min} an opportunity needs`;
}
const score = r => ((r.ps || {})[PRE] || {}).score;
const rank = r => 0.7 * (score(r) || 0) + 0.3 * (r.pb || 0);
const pinned = s => (V.pins || []).some(p => p.sym === s);
const country = r => {
  const c = (r.m && r.m.country) || r.country || "";
  return c === "United States" || c === "US" ? "" : c;      // said only when it is not the US
};
const ctag = r => country(r) ? `<span class="cc">${esc(country(r))}</span>` : "";

function passes(r) {
  if (F.region !== "all" && r.region !== F.region) return false;
  if (F.cap > 5e9 && (r.mcap_usd || 0) < F.cap) return false;
  if (F.trend === "notfalling" && r.pr && r.pr.trend === "falling") return false;
  if (F.q) {
    const q = F.q.toUpperCase();
    if (!(r.sym.toUpperCase().includes(q) || (r.name || "").toUpperCase().includes(q) ||
          (r.also || []).some(a => a.toUpperCase().includes(q)))) return false;
  }
  return true;
}

/* a line of weekly closes over the year */
function spark(pts, w = 96, h = 22, col) {
  const ys = (pts || []).filter(v => v != null);
  if (ys.length < 3) return `<span class="dim">–</span>`;
  const lo = Math.min(...ys), hi = Math.max(...ys) || 1, n = ys.length;
  const X = i => 1 + i / (n - 1) * (w - 2), Y = v => h - 2 - (v - lo) / ((hi - lo) || 1) * (h - 4);
  const d = ys.map((v, i) => (i ? "L" : "M") + X(i).toFixed(1) + " " + Y(v).toFixed(1)).join("");
  const c = col || (ys[n - 1] >= ys[0] ? "var(--gain)" : "var(--loss)");
  return `<span class="spk"><svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}"><path d="${d}" fill="none" stroke="${c}" stroke-width="1.2"/>
    <circle cx="${X(n - 1)}" cy="${Y(ys[n - 1])}" r="1.8" fill="${c}"/></svg></span>`;
}
function scorebar(v) {
  if (v == null) return `<span class="dim">–</span>`;
  return `<span class="sbar" title="${fx(v, 0)} of 100"><i style="width:${Math.max(0, Math.min(100, v))}%;background:${GCOL(v)}"></i></span>${fx(v, 0)}`;
}
function dots(gs) {
  const order = PRE === "value" ? ["quality", "growth", "estimates", "value", "income"] : ["quality", "growth", "estimates", "value"];
  return `<span class="dots">${order.map(g => `<i style="background:${GCOL((gs || {})[g])}" title="${esc(V.groups[g])}: ${(gs || {})[g] ?? "no data"}">${GLET[g]}</i>`).join("")}</span>`;
}
function splitbar(f) {
  if (!f) return `<span class="dim">–</span>`;
  const parts = [["company", f.company, f.company < 0 ? "#d0604c" : "#3fb380"], ["sector", f.sector, "#d9a441"], ["market", f.market, "#4fa8ce"]];
  const tot = parts.reduce((a, p) => a + Math.abs(p[1] || 0), 0) || 1;
  const t = `Since its high: ${sg(f.stock, 1)}%. Over the same days the market (${f.bench || ""}) ${sg(f.market, 1)}%, its sector ${f.sector != null ? sg(f.sector, 1) + "% against the market" : "unknown"}, the company ${sg(f.company, 1)}% against ${f.sector != null ? "its sector" : "the market"}`;
  return `<span class="split" title="${esc(t)}">${parts.map(p => `<i style="width:${Math.abs(p[1] || 0) / tot * 100}%;background:${p[2]}"></i>`).join("")}</span> <span class="${sgn(f.company)}">${sg(f.company, 0)}</span>`;
}
const FTAG = { own: ["own", "ft-own", "the market and its sector explain less than a quarter of this fall: it is the company's own"],
  sector: ["sector", "ft-sector", "its sector's fall explains a quarter or more of this one"],
  market: ["market", "ft-market", "the market's fall explains a quarter or more of this one"] };
function ftag(r) {
  const t = r.tag;
  if (!t || !FTAG[t.tag]) return "";
  const x = FTAG[t.tag];
  return `<span class="ftag ${x[1]}" title="${esc(x[2])} (market ${t.market_share}%, sector ${t.sector_share}%)${r.strict ? " — in a calm market it needs " + V.rules.strict_min : ""}">${x[0]}</span>`;
}
const MOOD = { calm: ["calm", "md-calm"], correction: ["correction", "md-corr"], "sell-off": ["sell-off", "md-sell"] };
const REGN = { US: "US", EU: "Europe" };

function fitcell(r) {
  const f = r.fit || {};
  if (f.held) return `<span class="held" title="you hold it: ${fx(f.w, 1)}% of the book">HELD ${fx(f.w, 1)}%</span>`;
  // the sector's share of your book is in the panel's header; the row says how this stock moves with the book
  if (f.corr == null) return `<span class="dim">–</span>`;
  return `<span class="${f.corr >= 0.7 ? "warnc" : f.corr <= 0.3 ? "pos" : ""}" title="correlation of its weekly moves with your book's over two years: ${f.corr >= 0.7 ? "moves like what you own" : f.corr <= 0.3 ? "a diversifier" : "partly like what you own"}">ρ ${fx(f.corr, 2)}</span>`;
}
function pecell(r) {
  const p = r.fpe || (r.m || {}).fpe_ny, med = (r.ctx || {}).pe_med;
  if (p == null) return `<span class="dim">–</span>`;
  const rel = med ? p / med : null;
  return `<span title="${med ? `${esc((r.ctx || {}).pe_label || "")}: ${fx(med, 1)}×` : ""}" class="${rel == null ? "" : rel <= 0.85 ? "pos" : rel > 1.15 ? "neg" : ""}">${fx(p, 1)}×</span>`;
}

/* ==========================================================================
   scaffold
   ========================================================================== */
$("#shell").innerHTML = termBar({ here: "WCH · WATCHLIST", right: `<button class="tbtn" id="scanBtn" title="Scan again now">Rescan</button>` });
wireShell();

function scaffold() {
  $("#app").innerHTML = `
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>WCH</h1><span class="sub" id="topSub">the watchlist: strong businesses well below their high</span></div>
    <div class="prog" id="prog"></div>
    <div class="tiles" id="tiles" style="margin-top:.5rem"></div>
    <div class="moods" id="moods"></div>
    <div class="disc">A starting point, not advice. The screen reads Yahoo's numbers and keeps what passes its rules; every stock on it still needs
      reading before money goes anywhere near it. <a href="#s-how">How it works</a>.</div>
  </div></div>
  <div id="ctl"></div>
  <div class="panel" id="s-sec"><header><h2>By sector</h2><span class="hint">opportunities in each, and how much of your book is there already — click one</span></header>
    <div class="body"><div class="secs" id="secs"></div></div></div>
  <div id="sectors"></div>
  <div class="panel" id="s-reason"><header><h2>Down for a reason</h2><span class="hint">fell as far, but the analysts are cutting their forecasts or the business is losing money — the value traps, so you can skip them</span></header>
    <div class="body" id="reason"></div></div>
  <div class="panel" id="s-mom"><header><h2>Momentum leaders</h2><span class="hint">the strongest twelve-month performers among each region's 500 largest companies, skipping the last month — the one factor that held up when Nexus was tested on honest data</span></header>
    <div class="body" id="mom"></div></div>
  <div class="panel" id="s-pins"><header><h2>Your pins and holdings</h2><span class="hint">what you pinned, with a buy price if you set one, and how every holding scores today</span></header>
    <div class="body" id="pins"></div></div>
  <div class="panel" id="s-track"><header><h2>Track record</h2><span class="hint">every stock the screen flagged, at that day's close, against its market since — so you can judge the screen</span></header>
    <div class="body" id="track"></div></div>
  <div class="panel" id="s-how"><header><h2>How it works</h2><span class="hint">the funnel, every check and its rule, the presets</span></header>
    <div class="body" id="how"></div></div>`;
}

/* ==========================================================================
   loading, and the scan in progress
   ========================================================================== */
async function load() {
  try { V = await api("/api/pf/wl"); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load the watchlist</b>${esc(e.message)}</div>`; return; }
  PRE = V.prefs.preset; THR = V.prefs.thr;
  if (!$("#top")) scaffold();
  renderCtl(); renderAll();
  watchJob();
  const m = location.hash.match(/^#s=([^&]+)/);
  if (m) openStock(decodeURIComponent(m[1]));
}

function watchJob() {
  clearTimeout(POLL);
  const j = V.job || {};
  renderProg();
  if (j.state === "running") POLL = setTimeout(async () => {
    try { V.job = await api("/api/pf/wl/job"); } catch { }
    if (V.job.state === "done" || V.job.state === "error") return load();
    watchJob();
  }, 3000);
}

function renderProg() {
  const j = V.job || {}, el = $("#prog");
  if (!el) return;
  if (j.state === "running") {
    const pct = j.n ? Math.round(j.i / j.n * 100) : 0;
    el.innerHTML = `<span class="warnc">Scanning</span> <span>${esc(j.phase)}${j.n ? ` · ${fx(j.i)} of ${fx(j.n)}` : ""}</span>
      ${j.n ? `<span class="bar"><i style="width:${pct}%"></i></span>` : ""}
      <span class="dim">${V.scan ? "the page shows the last scan until this one is done" : "the first scan takes ten to fifteen minutes; the page fills in when it is done"}</span>`;
  } else if (j.state === "error") el.innerHTML = `<span class="warnc">The last scan failed: ${esc(j.error || "")}</span>`;
  else el.innerHTML = j.warn ? `<span class="dim">${esc(j.warn)}</span>` : "";
}

$("#scanBtn").onclick = async () => {
  try { V.job = await post("/api/pf/wl/scan"); watchJob(); flash("Scanning: a few minutes."); } catch (e) { flash(esc(e.message)); }
};

/* ==========================================================================
   the controls
   ========================================================================== */
let SAVE_T = 0;
function savePrefs() {
  clearTimeout(SAVE_T);
  SAVE_T = setTimeout(() => post("/api/pf/wl/prefs", { preset: PRE, thr: THR }).catch(() => { }), 600);
}

function renderCtl() {
  const P = V.presets;
  const seg = (id, opts, cur) => `<span class="grp2" id="${id}">${opts.map(([v, l]) => `<button class="seg ${v === cur ? "on" : ""}" data-v="${v}">${l}</button>`).join("")}</span>`;
  $("#ctl").innerHTML = `
    <span><span class="lbl">Preset</span>${seg("cPre", Object.keys(P).map(k => [k, P[k].label]), PRE)}</span>
    <span><span class="lbl">Pullback</span>at least <span class="thr" id="cThrV">${fx(THR)}%</span> below the 52-week high
      <input type="range" id="cThr" min="${V.deep_dd}" max="50" step="1" value="${THR}"></span>
    <span><span class="lbl">Listed</span>${seg("cReg", [["all", "All"], ["US", "US"], ["EU", "Europe"]], F.region)}</span>
    <span><span class="lbl">Size</span>${seg("cCap", [["5000000000", "$5bn+"], ["20000000000", "$20bn+"], ["100000000000", "$100bn+"]], String(F.cap))}</span>
    <span><span class="lbl">Trend</span>${seg("cTr", [["any", "Any"], ["notfalling", "Not still falling"]], F.trend)}</span>
    <span><input type="search" id="cQ" placeholder="find a name" value="${esc(F.q)}"></span>
    <p class="about" id="cAbout">${esc(P[PRE].about)} An opportunity fails none of the checks this preset counts, has its forecasts judged and
      scores ${P[PRE].min} or more — ${V.rules ? V.rules.strict_min : 85} for a fall of its own while the market is calm; close is one check short, and never on ${P[PRE].must.map(k => esc(V.checks[k].label.toLowerCase())).join(", ")}.</p>`;
  const wireSeg = (id, fn) => $$(`#${id} .seg`).forEach(b => b.onclick = () => { $$(`#${id} .seg`).forEach(x => x.classList.toggle("on", x === b)); fn(b.dataset.v); });
  wireSeg("cPre", v => { PRE = v; savePrefs(); renderCtl(); renderAll(); });
  wireSeg("cReg", v => { F.region = v; renderAll(); });
  wireSeg("cCap", v => { F.cap = +v; renderAll(); });
  wireSeg("cTr", v => { F.trend = v; renderAll(); });
  $("#cThr").oninput = e => { THR = +e.target.value; $("#cThrV").textContent = fx(THR) + "%"; clearTimeout(renderCtl._t); renderCtl._t = setTimeout(renderAll, 120); savePrefs(); };
  $("#cQ").oninput = e => { F.q = e.target.value.trim(); clearTimeout(renderCtl._q); renderCtl._q = setTimeout(renderAll, 150); };
}

/* ==========================================================================
   the lists
   ========================================================================== */
function renderAll() {
  const S = V.scan;
  if (!S) {
    $("#topSub").textContent = "no scan yet";
    $("#tiles").innerHTML = "";
    ["#secs", "#sectors", "#reason", "#pins", "#track", "#mom", "#moods"].forEach(s => { const el = $(s); if (el) el.innerHTML = ""; });
    $("#sectors").innerHTML = `<div class="panel"><div class="body"><div class="empty">The first scan is running. It reads Yahoo's screener for every
      US and European company above $5bn, five years of their prices, and the fundamentals and estimates of every one in a pullback — ten to fifteen
      minutes the first time. This page fills in when it is done.</div></div></div>`;
    renderHow();
    return;
  }
  const rows = S.rows.filter(passes);
  const near = (S.near || []).filter(passes);
  const st = {};
  for (const r of rows) (st[status(r)] = st[status(r)] || []).push(r);
  const nearN = near.length + (st.near || []).length;
  $("#topSub").innerHTML = `${fx(S.n_universe)} companies in the US and Europe above $5bn · scanned ${when(S.scanned_at)} in ${fx(S.took_s / 60, 0)} min
    · next scan ${dmy(V.next_slot)} 22:45${S.n_errors ? ` · <span class="warnc" title="${esc(Object.entries(S.errors || {}).slice(0, 8).map(([k, v]) => k + ": " + v).join("\n"))}">${fx(S.n_errors)} could not be read</span>` : ""}
    ${(S.ver || 1) < V.rules.scan_ver ? ` · <span class="warnc">scanning again to add the market's mood and the momentum leaders</span>` : ""}`;
  const T = (k, v, sub, sel) => `<div class="tile" data-go="${sel}"><div class="k">${k}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
  $("#tiles").innerHTML =
    T("Opportunities", `<span class="pos">${fx((st.opp || []).length)}</span>`, `pass ${V.presets[PRE].label}, ${fx(THR)}%+ below the high`, "#sectors") +
    T("Close", `<span class="warnc">${fx((st.close || []).length)}</span>`, "one check short, or too few analysts to judge", "#sectors") +
    T("Down for a reason", `<span class="neg">${fx((st.reason || []).length)}</span>`, "forecasts being cut, or losing money", "#s-reason") +
    T("Not strong enough", fx((st.weak || []).length), "in a pullback, weak fundamentals", "#s-how") +
    T("Near their high", fx(nearN), `less than ${fx(THR)}% below it`, "#s-sec") +
    T("New since the last scan", fx((S.new || []).length), (S.new || []).slice(0, 4).join(", ") || "none", "#sectors") +
    momTile(S);
  $$("#tiles .tile").forEach(t => t.onclick = () => document.querySelector(t.dataset.go)?.scrollIntoView({ behavior: "smooth" }));
  renderMoods(S);
  renderSectors(rows, near, st);
  renderReason(st.reason || []);
  renderMomentum();
  renderPins();
  renderTrack();
  renderHow();
}

function renderSectors(rows, near, st) {
  const S = V.scan;
  const by = {};
  for (const r of rows) (by[r.sector || "Other"] = by[r.sector || "Other"] || []).push(r);
  const nearBy = {};
  for (const r of near) nearBy[r.sector] = (nearBy[r.sector] || 0) + 1;
  for (const r of st.near || []) nearBy[r.sector] = (nearBy[r.sector] || 0) + 1;
  const secs = V.sectors.map(sec => {
    const list = by[sec] || [], meta = (S.sectors || []).find(x => x.sector === sec) || {};
    const opp = list.filter(r => status(r) === "opp"), close = list.filter(r => status(r) === "close");
    return { sec, opp, close, reason: list.filter(r => status(r) === "reason"), meta, near: nearBy[sec] || 0 };
  });
  $("#secs").innerHTML = secs.map(x => `<a href="#sec-${slug(x.sec)}"><b>${esc(x.sec)}</b>
    <span class="n ${x.opp.length ? "" : "z"}">${fx(x.opp.length)}</span> <span class="s">opportunit${x.opp.length === 1 ? "y" : "ies"}</span>
    <div class="s">${fx(x.close.length)} close · ${fx(x.reason.length)} down for a reason · ${fx(x.near)} near the high</div>
    <div class="s">your book: ${x.meta.book != null ? `<span class="${x.meta.book >= 25 ? "warnc" : ""}">${fx(x.meta.book, 0)}%</span>` : "none"}
      · P/E ${x.meta.pe_us ? fx(x.meta.pe_us, 1) + "× US" : ""}${x.meta.pe_eu ? " " + fx(x.meta.pe_eu, 1) + "× EU" : ""}</div></a>`).join("");
  const head = `<thead><tr><th></th><th>Company</th><th>Price</th><th title="below the highest close of the last 52 weeks; the small number: how many of the last five years' days were less deep">Below high</th>
    <th title="below the highest monthly close on record">All-time</th><th>Trend</th><th title="the preset's score, 0–100; the letters: quality, growth, estimates, valuation${PRE === "value" ? ", income" : ""}">Score</th>
    <th title="forward P/E; green when 15% under its sector's median, red when 15% over">Fwd P/E</th><th title="change in next year's EPS forecast over 90 days">Forecast</th>
    <th title="twelve-month return skipping the last month: the momentum the test used">12-1</th>
    <th title="the fall since the high in context: the company against its sector (red or green), the sector against the market (amber), the market (blue); the number is the company against its sector; the tag says who the fall belongs to">Why down</th>
    <th title="held already; ρ: how its weekly moves track your book's; % your book already in the sector">Fit</th><th>1 year</th></tr></thead>`;
  $("#sectors").innerHTML = secs.filter(x => x.opp.length || (F.close && x.close.length)).map(x => {
    const opp = x.opp.sort((a, b) => rank(b) - rank(a)), close = x.close.sort((a, b) => rank(b) - rank(a));
    return `<div class="panel" id="sec-${slug(x.sec)}"><header><h2>${esc(x.sec)}</h2>
      <span class="hint">${fx(opp.length)} opportunit${opp.length === 1 ? "y" : "ies"} · ${fx(close.length)} close · your book ${x.meta.book != null ? fx(x.meta.book, 0) + "%" : "0%"} in it</span></header>
      <div class="body"><div class="tscroll"><table class="wt">${head}<tbody>
      ${opp.map(trow).join("") || `<tr><td colspan="13" class="empty">Nothing in ${esc(x.sec.toLowerCase())} passes ${esc(V.presets[PRE].label)} at ${fx(THR)}%.</td></tr>`}
      ${close.length ? `<tr class="sep"><td colspan="13">Close — one check short (or, in a calm market, a fall of its own under ${V.rules.strict_min}); hover the score for which</td></tr>${close.map(trow).join("")}` : ""}
      </tbody></table></div></div></div>`;
  }).join("") || `<div class="panel"><div class="body"><div class="empty">Nothing passes with these settings. Lower the pullback, switch the preset or widen the filters.</div></div></div>`;
  wireRows($("#sectors"));
}

function trow(r) {
  const pr = r.pr || {}, m = r.m || {}, j = (r.ps || {})[PRE] || {};
  const tr = TREND[pr.trend] || ["–", "", ""];
  return `<tr class="r" data-s="${esc(r.sym)}">
    <td><button class="star ${pinned(r.sym) ? "on" : ""}" data-pin="${esc(r.sym)}" title="${pinned(r.sym) ? "Unpin" : "Pin to your watchlist"}">★</button></td>
    <td><span class="sy">${esc(r.sym)}</span><span class="nm" title="${esc(r.name)}${r.m && r.m.industry ? " — " + esc(r.m.industry) : ""}">${esc(r.name)}</span>${ctag(r)}${r.fit && r.fit.held ? `<span class="held">HELD</span>` : ""}</td>
    <td>${pxf(r.px, r.ccy)}</td>
    <td>${sgc(pr.dd52, 0, "%")} ${pr.pctile != null ? `<span class="dim" title="deeper than ${pr.pctile}% of the days in the last five years">${pr.pctile}</span>` : ""}</td>
    <td>${pr.dd_ath != null ? `<span class="dim" title="${pr.ath_5y ? `the highest close in five years, ${fx(pr.ath, 2)} in ${mon(pr.ath_d)}; the full history is read at the next scan` : `all-time high ${fx(pr.ath, 2)} in ${mon(pr.ath_d)}`}">${sg(pr.dd_ath, 0)}%${pr.ath_5y ? "*" : ""}</span>` : "–"}</td>
    <td><span class="${tr[1]}" title="${esc(tr[2])}${pr.lo_d ? ` · low ${dmy(pr.lo_d)}, ${sg(pr.off_low, 0)}% off it` : ""}">${tr[0]}</span></td>
    <td title="${esc(verdict(r))}">${scorebar(score(r))}${dots(r.gs)}</td>
    <td>${pecell(r)}</td>
    <td>${sgc(m.est_ny_90, 0, "%")}</td>
    <td>${sgc(pr.mom, 0, "%")}</td>
    <td>${splitbar(r.fall)}${ftag(r)}</td>
    <td>${fitcell(r)}</td>
    <td>${spark(pr.spark)}</td></tr>`;
}

function wireRows(host) {
  host.querySelectorAll("tr.r").forEach(tr => tr.onclick = e => {
    if (e.target.closest("[data-pin]") || e.target.closest("a")) return;
    openStock(tr.dataset.s);
  });
  host.querySelectorAll("[data-pin]").forEach(b => b.onclick = async e => {
    e.stopPropagation();
    const s = b.dataset.pin;
    try {
      if (pinned(s)) { const r = await api("/api/pf/wl/pin/" + encodeURIComponent(s), { method: "DELETE" }); V.pins = r.pins; }
      else { const r = await post("/api/pf/wl/pin", { sym: s }); V.pins = r.pins; }
      $$(`[data-pin="${CSS.escape(s)}"]`).forEach(x => x.classList.toggle("on", pinned(s)));
      renderPins();
    } catch (err) { flash(esc(err.message)); }
  });
}

function renderReason(list) {
  list.sort((a, b) => (a.pr.dd52 || 0) - (b.pr.dd52 || 0));
  $("#reason").innerHTML = list.length ? `<details class="more"${list.length <= 12 ? " open" : ""}><summary>${fx(list.length)} stocks — show them</summary>
    <div class="tscroll"><table class="wt"><thead><tr><th></th><th>Company</th><th>Sector</th><th>Below high</th><th>Forecast, 90 days</th><th>Revenue, latest quarter</th><th>Why it is here</th></tr></thead><tbody>
    ${list.map(r => `<tr class="r" data-s="${esc(r.sym)}"><td><button class="star ${pinned(r.sym) ? "on" : ""}" data-pin="${esc(r.sym)}">★</button></td>
      <td><span class="sy">${esc(r.sym)}</span><span class="nm">${esc(r.name)}</span>${ctag(r)}${r.fit && r.fit.held ? `<span class="held">HELD</span>` : ""}</td>
      <td style="text-align:left">${esc(r.sector)}</td><td>${sgc(r.pr.dd52, 0, "%")}</td><td>${sgc((r.m || {}).est_ny_90, 0, "%")}</td>
      <td>${sgc((r.m || {}).rev_g_q, 0, "%")}</td><td style="text-align:left;white-space:normal;color:var(--ink-mid)">${esc(r.broken)}</td></tr>`).join("")}
    </tbody></table></div></details>` : `<div class="empty">None at these settings.</div>`;
  wireRows($("#reason"));
}

/* ---------- pins and holdings ---------------------------------------------- */
function renderPins() {
  const S = V.scan || { rows: [], near: [] };
  const find = s => S.rows.find(r => r.sym === s || (r.also || []).includes(s)) || (S.near || []).find(r => r.sym === s);
  const pins = V.pins || [];
  const held = S.rows.filter(r => r.fit && r.fit.held && !pins.some(p => p.sym === r.sym));
  const line = (r, p) => {
    const s = r ? status(r) : "nodata";
    const px = r ? r.px : null, buy = p && p.buy;
    const gap = buy && px ? (px / buy - 1) * 100 : null;
    return `<tr class="r" data-s="${esc(r ? r.sym : p.sym)}">
      <td>${p ? `<button class="star on" data-pin="${esc(p.sym)}" title="Unpin">★</button>` : ""}</td>
      <td><span class="sy">${esc(r ? r.sym : p.sym)}</span><span class="nm">${esc(r ? r.name : "not in the last scan")}</span>${r && r.fit && r.fit.held ? `<span class="held">HELD</span>` : ""}</td>
      <td style="text-align:left"><span class="chipst st-${s}">${STATUS[s]}</span></td>
      <td>${r ? pxf(px, r.ccy) : "–"}</td><td>${r ? sgc(r.pr ? r.pr.dd52 : r.dd52, 0, "%") : "–"}</td>
      <td>${r && r.ps ? scorebar(score(r)) : "–"}</td>
      <td>${buy ? `${fx(buy, 2)} <span class="${gap <= 0 ? "pos" : "dim"}">${gap <= 0 ? "reached" : sg(gap, 1) + "% above"}</span>` : `<span class="dim">–</span>`}</td>
      <td style="text-align:left;white-space:normal;color:var(--ink-mid)">${esc((p && p.note) || (r && r.broken) || "")}</td></tr>`;
  };
  $("#pins").innerHTML = `<div class="pinf" style="margin-bottom:.5rem"><input id="pSym" placeholder="ticker, e.g. ADBE or ASML.AS" style="width:22ch">
      <input id="pBuy" class="px" placeholder="buy at" type="number" step="any"><input id="pNote" class="nt" placeholder="note (optional)">
      <button class="tbtn" id="pAdd">Pin</button><span class="dim">Pinned stocks are checked in full at every scan, whatever their size or pullback, and show up in ERN's watchlist.</span></div>
    <div class="tscroll"><table class="wt"><thead><tr><th></th><th>Company</th><th style="text-align:left">Status</th><th>Price</th><th>Below high</th><th>Score</th><th>Buy price</th><th style="text-align:left">Note</th></tr></thead><tbody>
    ${pins.map(p => line(find(p.sym), p)).join("") || `<tr><td colspan="8" class="empty">Nothing pinned yet: the ★ on any row pins it.</td></tr>`}
    ${held.length ? `<tr class="sep"><td colspan="8">Your holdings — how each scores today</td></tr>${held.sort((a, b) => (b.fit.w || 0) - (a.fit.w || 0)).map(r => line(r, null)).join("")}` : ""}
    </tbody></table></div><div id="noted"></div>`;
  api("/api/pf/wl/notes").then(x => {
    const el = $("#noted");
    if (el && (x.noted || []).length) el.innerHTML = `<div class="src">Claude's second opinions you asked for: ${x.noted.map(n =>
      `<a href="#s=${encodeURIComponent(n.sym)}">${esc(n.sym)}</a> <span class="dim">${dmy(n.created)}</span>`).join(" · ")}</div>`;
  }).catch(() => { });
  $("#pAdd").onclick = async () => {
    const sym = $("#pSym").value.trim().toUpperCase(), buy = parseFloat($("#pBuy").value);
    if (!sym) { flash("A ticker, please."); return; }
    try {
      const r = await post("/api/pf/wl/pin", { sym, buy: isNaN(buy) ? null : buy, note: $("#pNote").value.trim() || null });
      V.pins = r.pins; renderPins(); renderAll();
      if (!find(sym)) flash(`${esc(sym)} is pinned; it is checked in full at the next scan (or press Rescan).`);
    } catch (e) { flash(esc(e.message)); }
  };
  wireRows($("#pins"));
}

/* ---------- track record ----------------------------------------------------- */
async function renderTrack() {
  let T, M = null;
  try { T = await api("/api/pf/wl/track?preset=" + PRE); } catch (e) { $("#track").innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
  try { M = await api("/api/pf/wl/track?preset=" + V.rules.mom_preset); } catch (e) { M = null; }
  const s = T.summary;
  $("#track").innerHTML = !s.n ? `<div class="empty">Nothing yet: from the first scan on, every stock that becomes an opportunity under ${esc(V.presets[PRE].label)}
      is written down with that day's close and its market's, and never rewritten. Come back in a few weeks.</div>` :
    `<div class="para" style="margin-bottom:.5rem">${fx(s.n)} stocks flagged under ${esc(V.presets[PRE].label)} since ${dmy(s.since)}, ${fx(s.active)} still on the list.
      On average ${sg(s.avg_ret, 1)}% since flagged, ${sg(s.avg_excess, 1)} points against its market; ${fx(s.beat)} of ${fx(s.of)} beat it.
      Early days say little: a few weeks of a screen is noise.</div>
    <div class="tscroll"><table class="wt"><thead><tr><th>Flagged</th><th>Company</th><th>Then</th><th>Now</th><th>Since</th><th>Market</th><th>Difference</th><th>Status</th></tr></thead><tbody>
    ${T.rows.slice(0, 120).map(f => `<tr class="r" data-s="${esc(f.sym)}"><td style="text-align:left">${dmy(f.first_d)}</td>
      <td><span class="sy">${esc(f.sym)}</span><span class="nm">${esc(f.name || "")}</span></td>
      <td>${fx(f.first_px, 2)}</td><td>${fx(f.now_px, 2)}</td><td>${sgc(f.ret, 1, "%")}</td><td>${sgc(f.bench_ret, 1, "%")}</td><td>${sgc(f.excess, 1)}</td>
      <td>${f.active ? `<span class="chipst st-opp">on the list</span>` : `<span class="dim">left ${dmy(f.end_d)}</span>`}</td></tr>`).join("")}
    </tbody></table></div>`;
  $("#track").insertAdjacentHTML("beforeend", momTrack(M));
  wireRows($("#track"));
}

/* the momentum top ten, held a quarter at a time, as the test held it */
function momTrack(M) {
  const head = `<div class="sec2">Momentum top 10, held a quarter at a time</div>`;
  if (!M || !M.summary.n) return head + `<div class="empty">Nothing yet: at the first scan of each quarter the top ten of each region are written down
    at that day's close and their market's, and closed at the first scan of the next quarter — the way the test held them.</div>`;
  const s = M.summary;
  return head + `<div class="para" style="margin-bottom:.5rem">${fx(s.n)} holdings since ${dmy(s.since)}, ${fx(s.active)} this quarter. On average
      ${sg(s.avg_ret, 1)}% while held, ${sg(s.avg_excess, 1)} points against the market; ${fx(s.beat)} of ${fx(s.of)} beat it.</div>
    <div class="tscroll"><table class="wt"><thead><tr><th>From</th><th>Company</th><th>Rank</th><th>Then</th><th>Now</th><th>Held</th><th>Market</th><th>Difference</th><th>Status</th></tr></thead><tbody>
    ${M.rows.slice(0, 80).map(f => `<tr class="r" data-s="${esc(f.sym)}"><td style="text-align:left">${dmy(f.first_d)}</td>
      <td><span class="sy">${esc(f.sym)}</span><span class="nm">${esc(f.name || "")}</span> <span class="dim">${esc(REGN[f.bench] || f.bench)}</span></td>
      <td>${f.score != null ? "#" + fx(f.score) : "–"}</td><td>${fx(f.first_px, 2)}</td><td>${fx(f.now_px, 2)}</td><td>${sgc(f.ret, 1, "%")}</td><td>${sgc(f.bench_ret, 1, "%")}</td><td>${sgc(f.excess, 1)}</td>
      <td>${f.active ? `<span class="chipst st-opp">this quarter</span>` : `<span class="dim">to ${dmy(f.end_d)}</span>`}</td></tr>`).join("")}
    </tbody></table></div>`;
}

/* ---------- the market's mood ------------------------------------------------ */
function renderMoods(S) {
  const md = S.mood || {};
  const say = {
    calm: "dips now are mostly companies' own — historically the weakest time to buy them (they ranked 43 of 100 a year later, and one in five fell another 30%). A dip of its own needs " + V.rules.strict_min + ".",
    correction: "dips have held up about average (rank 52 of 100 a year later). The usual bar applies.",
    "sell-off": "dips bought in broad sell-offs did best (rank 55 of 100, a median +25% a year later, one in twenty fell another 30%). The usual bar applies."
  };
  $("#moods").innerHTML = Object.entries(md).filter(([, m]) => m).map(([reg, m]) => {
    const x = MOOD[m.state] || [m.state, ""];
    return `<div class="mood"><span class="lbl">${esc(REGN[reg] || reg)}</span><span class="mchip ${x[1]}">${esc(x[0])}</span>
      <span>the typical one of the ${fx(V.rules.test_n)} largest companies is ${fx(-m.median_dd, 0)}% below its 52-week high; ${fx(m.share20)}% are 20%+ down — ${say[m.state] || ""}</span></div>`;
  }).join("");
}

/* ---------- momentum leaders ------------------------------------------------- */
function momTile(S) {
  const L = S.leaders;
  if (!L) return "";
  const rows = Object.values(L).flatMap(l => l.slice(0, V.rules.lead_top)).map(x => (S.rows || []).find(r => r.sym === x)).filter(Boolean);
  const weak = rows.filter(r => r.lead && r.lead.weak.length).length;
  return `<div class="tile" data-go="#s-mom"><div class="k">Momentum top 10s</div><div class="v"><span class="${weak ? "warnc" : "pos"}">${fx(rows.length - weak)}</span> <small class="dim">of ${fx(rows.length)}</small></div>
    <div class="s">holding up, US and Europe; ${fx(weak)} with a weakening business</div></div>`;
}

function renderMomentum() {
  const S = V.scan || {}, L = S.leaders;
  if (!L) { $("#mom").innerHTML = `<div class="empty">The next scan adds the momentum leaders.</div>`; return; }
  const list = (L[MREG] || []).map(x => (S.rows || []).find(r => r.sym === x)).filter(Boolean);
  const biz = r => {
    const l = r.lead || {};
    if (!l.checked) return `<span class="dim" title="${esc(r.err || "not read this scan")}">not read</span>`;
    if (l.weak.length) return `<span class="neg" title="${esc(l.weak.join("; "))}">weakening</span> <span class="dim">${esc(l.weak[0].split(":")[0])}</span>`;
    return `<span class="pos" title="no forecast cuts, no failing estimates check, debt in hand">holding up</span>`;
  };
  const line = r => {
    const pr = r.pr || {}, top = r.lead && r.lead.top;
    return `<tr class="r ${top ? "top10" : ""}" data-s="${esc(r.sym)}">
      <td><button class="star ${pinned(r.sym) ? "on" : ""}" data-pin="${esc(r.sym)}" title="${pinned(r.sym) ? "Unpin" : "Pin to your watchlist"}">★</button></td>
      <td><span class="sy">${esc(r.sym)}</span><span class="nm" title="${esc(r.name)}">${esc(r.name)}</span>${ctag(r)}${r.fit && r.fit.held ? `<span class="held">HELD</span>` : ""}</td>
      <td>${r.lead ? "#" + r.lead.rank : ""}</td><td style="text-align:left">${esc(r.sector || "")}</td>
      <td>${pxf(r.px, r.ccy)}</td><td><b>${sgc(pr.mom, 0, "%")}</b></td><td>${sgc(pr.r12, 0, "%")}</td><td>${sgc(pr.dd52, 0, "%")}</td>
      <td title="${esc(verdict(r))}">${r.ps ? scorebar(score(r)) + dots(r.gs) : "–"}</td><td>${biz(r)}</td><td>${fitcell(r)}</td><td>${spark(pr.spark)}</td></tr>`;
  };
  const top = list.filter(r => r.lead && r.lead.top), rest = list.filter(r => !(r.lead && r.lead.top));
  const secs = {};
  for (const r of top) secs[r.sector || "Other"] = (secs[r.sector || "Other"] || 0) + 1;
  const crowd = Object.entries(secs).sort((a, b) => b[1] - a[1])[0];
  $("#mom").innerHTML = `<div class="mbar"><span class="grp2" id="mReg">${["US", "EU"].map(k => `<button class="seg ${k === MREG ? "on" : ""}" data-v="${k}">${REGN[k]}</button>`).join("")}</span>
      <span class="dim">${crowd && crowd[1] >= 4 ? `${crowd[1]} of the top ten are ${esc(crowd[0].toLowerCase())}: momentum crowds into one theme, and leaves it together.` : ""}</span></div>
    <div class="tscroll"><table class="wt"><thead><tr><th></th><th>Company</th><th>Rank</th><th style="text-align:left">Sector</th><th>Price</th>
      <th title="the close a month ago against the close a year ago: what the list is ranked on">12-1</th><th title="the plain twelve-month return">12 months</th>
      <th>Below high</th><th title="the preset's score, as for the dips">Score</th><th title="forecast cuts, a failing estimates check or too much debt mark a leader as weakening">Business</th><th>Fit</th><th>1 year</th></tr></thead><tbody>
      ${top.map(line).join("")}${rest.length ? `<tr class="sep"><td colspan="12">Ranks ${top.length + 1}–${list.length}</td></tr>${rest.map(line).join("")}` : ""}
    </tbody></table></div>
    <div class="disc">Ranked on the twelve-month return skipping the last month. In the test — the S&P 500 as it was at each date, 2020 to May 2026, the
      top ten bought each quarter — this made 27% a year against 11% for the average member, beat 300 random ten-stock picks every time and lost
      money in one year (2022, −9%). One period with strong trends; momentum falls hard when a trend turns, and it says nothing about price. A list to
      read, not to buy.</div>`;
  $$("#mReg .seg").forEach(b => b.onclick = () => { MREG = b.dataset.v; renderMomentum(); });
  wireRows($("#mom"));
}

/* ---------- how it works ----------------------------------------------------- */
function renderHow() {
  const C = V.checks, P = V.presets;
  const byG = {};
  for (const [k, c] of Object.entries(C)) (byG[c.g] = byG[c.g] || []).push([k, c]);
  $("#how").innerHTML = `<div class="para">
    <p><b>The funnel.</b> Every weekday at 22:45, after New York closes, the server asks Yahoo's screener for every company on the main US and
    European exchanges worth more than $5bn, sector by sector — about ${fx((V.scan || {}).n_universe || 1800)} once a company listed in several places is
    counted once (the listing that trades in the currency it reports in, Amsterdam first; your own listing when you hold it). It reads five years
    of daily prices for all of them. Every stock at least ${fx(V.deep_dd)}% below its 52-week high, every pin and every holding then gets the full
    check: four years and five quarters of statements, the analysts' estimates and how they moved over 90 days, and the key statistics.</p>
    <p><b>The rule that matters most.</b> A stock that fell while the analysts cut next year's earnings forecast by 15% or more is
    <i>down for a reason</i>, whatever else it scores: the price is following the business down. Those are listed apart, so you can skip them.
    So is a company losing money with no profit expected next year.</p>
    <p><b>The pullback.</b> How far below the highest close of the last 52 weeks; the small number is how unusual that is for this stock — the
    share of the last five years' days on which it was less far down. The trend says whether it is still making new lows. <i>Why down</i>
    puts the fall since the high in context: what the market did over the same days, how the sector did against the market (every member
    of the sector in the same region, equally weighted), and how the company did against its sector.</p>
    <p><b>Who fell with it, and the market's mood.</b> Tested on the S&P 500 as it was at each month-end from mid-2019 (so the
    companies that later dropped out are in it), a stock 20% or more below its high did, a year later, about as well as the average
    member — but not in every market. While the typical member was less than 10% below its own high, those dips ranked 43 of 100 and one in
    five fell another 30%; in broad sell-offs (2020, 2022, March–April 2025) they ranked 55 and one in twenty did. In a calm market a dip is
    almost always the company's own. So the page shows each region's mood — calm, correction (the typical company 10–15% below its high) or
    sell-off (15% or more) — and tags each fall: <i>market</i> or <i>sector</i> when their own fall over the same days explains a quarter
    or more of it, <i>own</i> otherwise; falls of their own lost another 30% in 11% of cases, against 3–5% for the others. In a calm market
    a fall of its own needs a score of ${V.rules ? V.rules.strict_min : 85} instead of 75.</p>
    <p>The mood is read on each region's ${V.rules ? V.rules.test_n : 500} largest companies, the size class the test covered: the whole universe down
    to $5bn always reads deeper.</p>
    <p><b>Momentum leaders.</b> The thirty strongest twelve-month performers among each region's ${V.rules ? V.rules.test_n : 500} largest companies,
    skipping the most recent month, checked like
    the dips, with forecast cuts, a failing estimates check or too much debt marked as a weakening business. In the same honest test the
    top ten, bought quarterly, made 27% a year against 11% for the average member. The track record holds each quarter's top ten from the
    first scan of the quarter to the first of the next.</p>
    <p><b>The score, and what qualifies.</b> Each check passes (1), warns (½) or fails (0); a group's score is the average of its checks, and
    the preset weighs the groups. An <i>opportunity</i> is below its high by at least the pullback you set, is not down for a reason, has had
    its forecasts judged by at least three analysts, <b>fails none of the checks the preset counts</b>, and scores at least 75. <i>Close</i> is
    one counted check short (never a must-pass one) with a score of 65 or more, or would qualify but for too few analysts. On the first
    full scan this left about eighty opportunities under Balanced, of 955 stocks in a pullback. The order within a sector is 70% score,
    30% how much on sale it is.</p></div>
    <div class="tscroll"><table class="wt" style="margin-top:.4rem"><thead><tr><th></th><th>Check</th><th style="text-align:left">Rule</th>
      ${Object.keys(P).map(k => `<th>${esc(P[k].label)}</th>`).join("")}</tr></thead><tbody>
    ${Object.keys(V.groups).map(g => `<tr class="sep"><td colspan="${3 + Object.keys(P).length}">${esc(V.groups[g])} —
      weight ${Object.keys(P).map(k => `${esc(P[k].label)} ${fx((P[k].w[g] || 0) * 100)}%`).join(", ")}</td></tr>
      ${(byG[g] || []).map(([k, c]) => `<tr><td></td><td>${esc(c.label)}</td><td style="text-align:left;white-space:normal;color:var(--ink-mid)">${esc(c.how)}</td>
        ${Object.keys(P).map(p => `<td>${P[p].must.includes(k) ? `<span class="warnc">must pass</span>` : k === "v_target" ? `<span class="dim">shown</span>` : (P[p].w[g] ? "scored" : `<span class="dim">–</span>`)}</td>`).join("")}</tr>`).join("")}`).join("")}
    </tbody></table></div>
    <div class="src">Sources: Yahoo Finance (screener, prices, statements, estimates, statistics) through yfinance. Statements are Yahoo's own
      four years and five quarters; the security page (click through) has ten years from the SEC for US companies. Sector P/E medians come
      from the whole universe, the US and Europe separately where each has ten names or more. Banks and insurers are judged on return on
      equity instead of cash flow and debt, which mean something else for them. Nothing here is advice.</div>`;
}

const slug = s => String(s || "").toLowerCase().replace(/[^a-z]+/g, "-");

/* ==========================================================================
   one stock: every check, why it fell, the fit, Claude's second opinion
   ========================================================================== */
function findRow(sym) {
  const S = V.scan || {};
  const s = sym.toUpperCase();
  const full = (S.rows || []).find(r => r.sym === s || (r.also || []).includes(s));
  if (full) return full;
  const n = (S.near || []).find(r => r.sym === s);
  return n ? { ...n, near_only: true } : null;
}

function openStock(sym) {
  const dr = $("#drawer");
  const r = findRow(sym);
  OPEN_SYM = r ? r.sym : sym.toUpperCase();
  dr.classList.add("on"); dr.setAttribute("aria-hidden", "false");
  if (location.hash !== "#s=" + OPEN_SYM) history.replaceState(null, "", "#s=" + encodeURIComponent(OPEN_SYM));
  if (!r) {
    dr.innerHTML = `<div class="dh"><h2>${esc(sym)}</h2><span class="sp"></span><button class="tbtn" id="drX">Close</button></div>
      <div class="db"><div class="note"><b>Not in the last scan</b>${esc(sym)} is not one of the US or European companies above $5bn the screen reads.
      Pin it and it is checked in full at the next scan. <a href="/t/${encodeURIComponent(sym)}">Its security page</a> has everything else.</div></div>`;
    $("#drX").onclick = closeStock;
    return;
  }
  const s = status(r), pr = r.pr || {}, m = r.m || {}, j = (r.ps || {})[PRE] || {}, f = r.fall;
  const pin = (V.pins || []).find(p => p.sym === r.sym);
  const enc = encodeURIComponent(r.sym);
  let h = `<div class="dh"><h2>${esc(r.name)}</h2><span class="dim">${esc(r.sym)} · ${esc(r.exch || "")} · ${esc(r.ccy || "")}${(r.also || []).length ? ` · also ${esc(r.also.slice(0, 4).join(", "))}` : ""}</span>
      <span class="sp"></span><span class="chipst st-${s}">${STATUS[s]}</span>
      <button class="star ${pin ? "on" : ""}" data-pin="${esc(r.sym)}" title="${pin ? "Unpin" : "Pin"}" style="font-size:1.1rem">★</button><button class="tbtn" id="drX">Close</button></div><div class="db">`;
  if (r.near_only) {
    h += `<div class="note plain">${esc(r.sym)} is ${sg(r.dd52, 1)}% from its 52-week high, so the scan did not check it in full. Pin it to have it checked at every scan.</div>
      <div class="links"><a href="/t/${enc}">Security page →</a></div></div>`;
    dr.innerHTML = h; $("#drX").onclick = closeStock; wireRows(dr); return;
  }
  h += `<div class="cards">
      <div class="card"><div class="k">Price</div><div class="v">${pxf(r.px, r.ccy)}</div><div class="t">${dmy(pr.d)} close · ${usdbn(r.mcap_usd)}</div></div>
      <div class="card"><div class="k">Below its 52-week high</div><div class="v">${sgc(pr.dd52, 1, "%")}</div><div class="t">high ${fx(pr.hi_px, 2)} on ${dmy(pr.hi_d)}${pr.pctile != null ? ` · deeper than ${pr.pctile}% of five years' days` : ""}</div></div>
      <div class="card"><div class="k">${pr.ath_5y ? "Five-year high" : "All-time high"}</div><div class="v">${pr.dd_ath != null ? sgc(pr.dd_ath, 0, "%") : "–"}</div><div class="t">${pr.ath ? `${fx(pr.ath, 2)} in ${mon(pr.ath_d)}` : ""}${pr.ath_5y ? " · the full history is read at the next scan" : ""}</div></div>
      <div class="card"><div class="k">${esc(V.presets[PRE].label)} score</div><div class="v">${scorebar(j.score)}</div><div class="t">${esc(verdict(r))}</div></div>
    </div>`;
  if (r.broken) h += `<div class="note"><b>Down for a reason</b>${esc(r.broken[0].toUpperCase() + r.broken.slice(1))}.</div>`;
  if (r.lead) h += `<div class="note plain"><b>Momentum leader, #${r.lead.rank} in ${esc(REGN[r.lead.region] || r.lead.region)}</b>${sg(pr.mom, 0)}% over twelve months skipping the last
    (${sg(pr.r12, 0)}% over the full year).${r.lead.weak.length ? ` Weakening: ${esc(r.lead.weak.join("; "))}.` : r.lead.checked ? " The business is holding up: no forecast cuts, no failing estimates check, debt in hand." : ""}</div>`;
  h += `<div class="links"><a href="/t/${enc}">Security page: ten years of filings, valuation history →</a><a href="/m/ERN/${enc}">Earnings →</a><a href="/m/OPT/${enc}">Options →</a><a href="/m/SPLC/${enc}">Supply chain →</a></div>`;
  if (r.about) h += `<p class="para">${esc(r.about)}${r.about.length >= 399 ? "…" : ""} <span class="dim">${esc(m.industry || "")}${m.country ? " · " + esc(m.country) : ""}</span></p>`;

  // the fall
  h += `<div class="sec"><span>The fall</span><span>${esc(TREND[pr.trend] ? TREND[pr.trend][0] : "")}</span></div>`;
  if (f) {
    const parts = [["the company", f.company, f.company < 0 ? "#d0604c" : "#3fb380"], ["its sector", f.sector, "#d9a441"], [f.bench || "the market", f.market, "#4fa8ce"]];
    const tot = parts.reduce((a, p) => a + Math.abs(p[1] || 0), 0) || 1;
    const own = Math.abs(f.company) >= Math.abs(f.sector || 0) + Math.abs(f.market);
    h += `<div class="para">Since its high on ${dmy(pr.hi_d)}: <b>${sg(f.stock, 1)}%</b>. Over the same days ${esc(f.bench || "the market")} moved ${sg(f.market, 1)}%,
      ${f.sector != null ? `its sector did ${sg(f.sector, 1)}% against the market, and the company ${sg(f.company, 1)}% against its sector.` : `and the company did ${sg(f.company, 1)}% against it.`}
      ${own ? "Mostly the company's own." : f.company > -10 ? "Mostly its sector and the market: the company has kept pace with them." : "Partly the company, partly its sector and the market."}
      Low ${fx(pr.lo_px, 2)} on ${dmy(pr.lo_d)}, ${sg(pr.off_low, 1)}% off it now; ${fx(pr.since_low)} sessions since. ${pr.ma200_gap != null ? `${sg(pr.ma200_gap, 0)}% against its 200-day average.` : ""}
      ${r.tag ? `The market's own fall over these days explains ${r.tag.market_share}% of this one, the sector's ${r.tag.sector_share}%: ${r.tag.tag === "own" ? "<b>it is the company's own</b>" : r.tag.tag === "sector" ? "<b>its sector took it down</b>" : "<b>the market took it down</b>"}.` : ""}
      ${(() => { const md = ((V.scan || {}).mood || {})[r.region || "US"]; return md ? `The ${esc(REGN[r.region] || "US")} market is ${esc({ calm: "calm", correction: "in a correction", "sell-off": "in a sell-off" }[md.state] || md.state)} (the typical large company ${fx(-md.median_dd, 0)}% below its high)${r.strict ? `, so this one needs a score of ${V.rules.strict_min}, not ${V.presets[PRE].min}` : ""}.` : ""; })()}</div>
      <div class="bigsplit">${parts.map(p => `<i style="width:${Math.abs(p[1] || 0) / tot * 100}%;background:${p[2]}" title="${esc(p[0])}: ${sg(p[1], 1)}%">${Math.abs(p[1] || 0) / tot > 0.14 ? esc(p[0]) : ""}</i>`).join("")}</div>`;
  }
  h += `<div style="margin:.3rem 0">${spark(pr.spark, 520, 70, s === "reason" ? "var(--loss)" : "var(--accent)")}</div>`;
  if ((pr.drops || []).length) h += `<div class="para">Its worst days since the high: ${pr.drops.map(d => `${dmy(d[0])} ${sg(d[1], 1)}%${d[2] != null ? ` (sector ${sg(d[2], 1)}%)` : ""}`).join("; ")}.</div>`;
  h += `<div id="drWhy"><div class="dim ld">Looking for what happened on those days<span class="blink">_</span></div></div>`;

  // the checks
  h += `<div class="sec"><span>Every check</span><span>${esc(V.presets[PRE].label)}: ${Object.entries(V.presets[PRE].w).map(([g, w]) => `${esc(V.groups[g])} ${fx(w * 100)}%`).join(" · ")}</span></div>`;
  const byG = {};
  for (const c of r.ck || []) (byG[V.checks[c[0]].g] = byG[V.checks[c[0]].g] || []).push(c);
  const icon = { pass: "✓", warn: "!", fail: "✗", na: "·", info: "i" };
  for (const g of Object.keys(V.groups)) {
    if (!byG[g]) continue;
    h += `<div class="gh"><span>${esc(V.groups[g])}</span>${scorebar((r.gs || {})[g])}${V.presets[PRE].w[g] ? "" : `<span class="dim">not counted in ${esc(V.presets[PRE].label)}</span>`}</div><div class="ck">`;
    for (const [k, st, txt] of byG[g]) {
      const must = V.presets[PRE].must.includes(k);
      h += `<span class="i ${st}">${icon[st] || "·"}</span><span class="l">${esc(V.checks[k].label)}${must ? ` <span class="warnc" title="must not fail in ${esc(V.presets[PRE].label)}">*</span>` : ""}</span><span class="v">${esc(txt)}</span>
        <span class="h">${esc(V.checks[k].how)}</span>`;
    }
    h += `</div>`;
  }
  if (r.err) h += `<div class="note plain">Part of the data could not be read: ${esc(r.err)}</div>`;
  h += `<div class="src">Statements to ${dmy(r.fin_end)} (Yahoo); estimates and prices from the scan of ${when(V.scan.scanned_at)}.</div>`;

  // fit
  const ft = r.fit || {};
  h += `<div class="sec"><span>Fit with your portfolio</span></div><div class="para">
    ${ft.held ? `You hold it: ${fx(ft.w, 1)}% of the book.` : "You do not hold it."}
    ${ft.sector_w != null ? `${esc(r.sector)} is ${fx(ft.sector_w, 1)}% of your book${ft.sector_w >= 25 ? " — adding here adds to a concentration you already have" : ft.sector_w < 3 ? " — this would be new ground" : ""}.` : `You hold nothing in ${esc(r.sector || "its sector")}.`}
    ${ft.corr != null ? `Over two years its weekly moves had a correlation of ${fx(ft.corr, 2)} with your book's: ${ft.corr >= 0.7 ? "it moves much like what you already own" : ft.corr <= 0.3 ? "it moves largely on its own — a diversifier" : "partly like what you own"}.` : ""}</div>`;

  // pin
  h += `<div class="sec"><span>Your pin</span></div><div class="pinf"><input id="dBuy" class="px" type="number" step="any" placeholder="buy at" value="${pin && pin.buy ? pin.buy : ""}">
    <input id="dNote" class="nt" placeholder="note" value="${esc(pin && pin.note || "")}"><button class="tbtn" id="dPin">${pin ? "Save" : "Pin"}</button>
    <span class="dim">A buy price raises an alert on the home screen and a line in the morning brief once the scan's close is at or below it.</span></div>`;

  // Claude
  h += `<div class="sec"><span>Claude's second opinion</span></div><div id="drNote"><div class="dim ld">…</div></div></div>`;
  dr.innerHTML = h;
  $("#drX").onclick = closeStock;
  wireRows(dr);
  $("#dPin").onclick = async () => {
    const buy = parseFloat($("#dBuy").value);
    try {
      const x = await post("/api/pf/wl/pin", { sym: r.sym, buy: isNaN(buy) ? null : buy, note: $("#dNote").value.trim() || null });
      V.pins = x.pins; renderPins(); flash("Saved."); openStock(r.sym);
    } catch (e) { flash(esc(e.message)); }
  };
  loadWhy(r);
  renderNote(r.sym);
}

async function loadWhy(r) {
  let W;
  try { W = await api("/api/pf/wl/why/" + encodeURIComponent(r.sym)); } catch (e) { W = { error: e.message }; }
  if (OPEN_SYM !== r.sym) return;
  const el = $("#drWhy");
  if (!el) return;
  if (W.error) { el.innerHTML = `<div class="dim">The days and their catalysts could not be read: ${esc(W.error)}</div>`; return; }
  const days = W.days || [];
  let h = days.length ? `<table class="wt" style="margin-top:.4rem"><thead><tr><th>Day</th><th>Move</th><th>Company part</th><th style="text-align:left">What it was</th></tr></thead><tbody>
    ${days.map(d => {
      // the explanation names its headline and its likely cause; say each once
      const why = (d.why || "").replace(/\s*In the news: .*$/, "");
      const extra = (d.catalysts || []).filter(c => c.type !== "dividend" && !why.includes(c.text)).slice(0, 2);
      return `<tr><td style="text-align:left">${dmy(d.d)}</td><td>${sgc(d.r, 1, "%")}</td><td>${sgc(d.idio, 1)}</td>
      <td style="text-align:left;white-space:normal">${esc(why)}${d.headline ? ` <a href="${esc(d.headline.url || "#")}" target="_blank" rel="noopener">${esc(d.headline.text)}</a> <span class="dim">${esc(d.headline.source || "")}</span>` : ""}
      ${extra.map(c => `<div class="dim">${esc(c.text)}</div>`).join("")}</td></tr>`;
    }).join("")}</tbody></table>` : "";
  const er = (W.earnings || []).slice(-3);
  if (er.length) h += `<div class="para" style="margin-top:.4rem">Earnings since the high: ${er.map(e => `${dmy(e.date)} EPS ${e.eps_act ?? "–"} against ${e.eps_est ?? "–"} expected${e.surprise_pct != null ? ` (${sg(e.surprise_pct, 1)}%)` : ""}${e.reaction && e.reaction.r != null ? `, the stock ${sg(e.reaction.r, 1)}% next session` : ""}`).join("; ")}.</div>`;
  const ac = (W.actions || []).filter(a => a.action === "down" || a.action === "up" || a.pt_action).slice(0, 6);
  if (ac.length) h += `<div class="para">Analysts since the high: ${ac.map(a => `${dmy(a.date)} ${esc(a.firm || "")} ${esc(a.action === "down" ? "downgraded" : a.action === "up" ? "upgraded" : "moved the target")}${a.pt ? ` (target ${fx(a.pt, 0)}${a.pt_prior ? ` from ${fx(a.pt_prior, 0)}` : ""})` : ""}`).join("; ")}.</div>`;
  el.innerHTML = h || `<div class="dim">No standout company-specific days found.</div>`;
  if (W.archive) el.insertAdjacentHTML("beforeend", `<div class="src">From the security page's analysis of every session against ${esc((W.benchmarks || {}).market || "the market")} and ${esc((W.benchmarks || {}).sector || "its sector")}; headlines: ${esc(W.archive)}.</div>`);
}

function closeStock() {
  const dr = $("#drawer");
  dr.classList.remove("on"); dr.setAttribute("aria-hidden", "true");
  OPEN_SYM = null;
  clearTimeout(NOTE_T);
  if (location.hash.startsWith("#s=")) history.replaceState(null, "", location.pathname);
}
document.addEventListener("keydown", e => { if (e.key === "Escape" && $("#drawer").classList.contains("on")) closeStock(); });
window.addEventListener("hashchange", () => {
  const m = location.hash.match(/^#s=([^&]+)/);
  if (m && V) openStock(decodeURIComponent(m[1]));
});

/* ---------- Claude's second opinion ---------------------------------------- */
function mdInline(s) {
  return s.replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((\/[^\s)]*)\)/g, '<a href="$2">$1</a>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function md(src) {
  const lines = esc(src || "").split("\n");
  let out = "", list = null, para = [];
  const flush = () => { if (para.length) { out += `<p>${mdInline(para.join(" "))}</p>`; para = []; } };
  const endList = () => { if (list) { out += `</${list}>`; list = null; } };
  for (const l of lines) {
    let m;
    if ((m = l.match(/^(#{1,3})\s+(.*)$/))) { flush(); endList(); out += `<h${m[1].length}>${mdInline(m[2])}</h${m[1].length}>`; continue; }
    if ((m = l.match(/^\s*[-*]\s+(.*)$/))) { flush(); if (list !== "ul") { endList(); out += "<ul>"; list = "ul"; } out += `<li>${mdInline(m[1])}</li>`; continue; }
    if ((m = l.match(/^\s*\d+[.)]\s+(.*)$/))) { flush(); if (list !== "ol") { endList(); out += "<ol>"; list = "ol"; } out += `<li>${mdInline(m[1])}</li>`; continue; }
    if (!l.trim()) { flush(); endList(); continue; }
    if (list) endList();
    para.push(l.trim());
  }
  flush(); endList();
  return out;
}

async function renderNote(sym) {
  clearTimeout(NOTE_T);
  let N;
  try { N = await api("/api/pf/wl/note/" + encodeURIComponent(sym)); } catch (e) { const el = $("#drNote"); if (el) el.innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
  if (OPEN_SYM !== sym) return;
  const el = $("#drNote");
  if (!el) return;
  const job = N.job, writer = N.writer === "claude-code" ? `Claude Code (${esc(N.cc_model_label)}) on your plan` : `the API (${esc(N.model_label)}) on your key`;
  let h = `<div class="para">Claude reads everything above — the checks, the fall and its days, the analysts, the headlines, the fit — and argues both
    sides: is the price ahead of a business that is intact, or is the market seeing a problem the numbers have not shown yet? Written by ${writer};
    change that in Settings. <b>Only when you press the button</b>: each one counts toward your plan's usage, or bills your key.</div>
    <div class="pinf" style="margin:.4rem 0"><button class="tbtn" id="nGo"${job && job.state === "running" ? " disabled" : ""}>Write a second opinion</button>
    <label class="dim"><input type="checkbox" id="nWeb"> with web search (for a fall the material cannot explain)</label></div>`;
  if (job && job.state === "running") h += `<div class="md writing">${md(job.text || "")}<div class="dim">${esc(job.phase)} · ${fx(job.elapsed_s)} s</div></div>`;
  if (job && job.state === "error") h += `<div class="note"><b>Could not write it</b>${esc(job.error)}</div>`;
  for (const n of N.notes || []) h += `<div class="md" style="margin-top:.6rem">${md(n.body)}</div><div class="src">${esc(n.model || "")} · ${dmyTime(n.created)}${n.cost_usd ? ` · $${fx(n.cost_usd, 3)}` : ""} · <a href="#" data-del="${n.id}">delete</a></div>`;
  for (const a of N.answers || []) h += `<div class="md" style="margin-top:.5rem"><p><b>You asked:</b> ${esc(a.question)}</p>${md(a.body)}</div>`;
  if ((N.notes || []).length) h += `<div class="qbox"><input id="nQ" placeholder="Ask about the note or the stock" maxlength="2000"><button class="tbtn" id="nAsk">Ask</button></div>`;
  el.innerHTML = h;
  const start = async q => {
    try { await post("/api/pf/wl/note/" + encodeURIComponent(sym), { question: q || null, web: !q && $("#nWeb") && $("#nWeb").checked }); renderNote(sym); }
    catch (e) { flash(esc(e.message)); }
  };
  $("#nGo").onclick = () => start(null);
  if ($("#nAsk")) $("#nAsk").onclick = () => { const q = $("#nQ").value.trim(); if (q) start(q); };
  el.querySelectorAll("[data-del]").forEach(a => a.onclick = async e => {
    e.preventDefault(); await fetch("/api/pf/wl/note/" + a.dataset.del, { method: "DELETE" }); renderNote(sym);
  });
  if (job && job.state === "running") NOTE_T = setTimeout(() => renderNote(sym), 2500);
}

load();
