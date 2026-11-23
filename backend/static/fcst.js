/* ==========================================================================
   FCST — what the portfolio might be worth, 1 to 50 years out.

   Not a prediction: the range of outcomes 150 years of 16 stock markets
   would have given a book with your risk, your deposits and Box 3. The
   server draws ten thousand paths (pf/fcst.py); this page asks again when a
   setting changes and draws the fan, the table and the comparisons.

   Not advice either. A way to see what deposits, risk, costs and tax do to
   the long run.
   ========================================================================== */

let V = null;                 // /api/pf/fcst
let R = null;                 // the run on screen
let S = {};                   // the settings
let VIEW = "real", HZN = 30, LOG = false, SEQ = 0, TMR = 0;
const HZNS = [1, 2, 5, 10, 20, 30, 50];
const THIS_YEAR = new Date().getFullYear();

/* ---------- formatting ---------------------------------------------------- */
const fx = (v, d = 0) => v == null || isNaN(v) ? "–" :
  Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
const ec = v => {                                  // compact euros: €9,870 · €12.5k · €340k · €1.20m
  if (v == null || isNaN(v)) return "–";
  const a = Math.abs(v), s = v < 0 ? "−" : "";
  if (a < 10000) return s + "€" + fx(a, 0);
  if (a < 100000) return s + "€" + fx(a / 1e3, 1) + "k";
  if (a < 1e6) return s + "€" + fx(a / 1e3, 0) + "k";
  if (a < 1e9) return s + "€" + fx(a / 1e6, a < 1e7 ? 2 : 1) + "m";
  return s + "€" + fx(a / 1e9, 2) + "bn";
};
const pc0 = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + fx(Math.abs(v), d) + "%";
const api = async (u, o) => { const r = await fetch(u, o); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status); return r.json(); };
const post = (u, b) => api(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b || {}) });
const yrs = k => k === 1 ? "1 year" : `${k} years`;
const Q = { p5: 0, p10: 1, p25: 2, p50: 3, p75: 4, p90: 5, p95: 6 };
const HIST = { world: "all 16 markets together", country: "one market at a time" };
const B3 = { bill: "the 2028 bill", now: "today's system", none: "no Box 3" };

/* one horizon's numbers in the unit on screen */
function at(row, k) {
  const f = R.fan[k];
  if (VIEW === "real") return { p5: row.real.p5, p25: row.real.p25, p50: row.real.p50, p75: row.real.p75, p95: row.real.p95, put: row.put_in };
  return { p5: row.nom.p5, p50: row.nom.p50, p95: row.nom.p95, put: f.drawer_nom };
}
const rowOf = k => R.rows.find(r => r.y === k);

/* ==========================================================================
   scaffold
   ========================================================================== */
$("#shell").innerHTML = termBar({ here: "MVO · FORECAST", right: `<a class="tbtn" href="/portfolio" title="The portfolio">PORT</a>` });
wireShell();

function scaffold() {
  $("#app").innerHTML = `
  <nav class="mtabs"><a href="/m/MVO">Markowitz</a><a class="on" href="/m/MVO/forecast">Forecast</a></nav>
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>FORECAST</h1><span class="sub">what your portfolio might be worth, from 1 to 50 years — a range, not a prediction</span></div>
    <div class="tiles" id="tiles"></div>
    <div class="disc">Not a prediction and not advice. It is what 150 years of 16 stock markets would have done to a book with your risk,
      your deposits, your costs and Box 3: ten thousand paths, and the range they span. Half of them end inside the darker band.
      <a href="#s-how">How it works</a>.</div>
  </div></div>
  <div id="ctl"></div>
  <div class="panel" id="s-fan"><header><h2>The range</h2><span class="hint" id="fanHint"></span></header>
    <div class="body"><div class="fbar" id="fanBar" style="margin-bottom:.4rem"></div><div class="fan" id="fan"></div>
      <div class="fkey" id="fkey"></div><p class="read" id="read"></p></div></div>
  <div class="panel" id="s-tab"><header><h2>By horizon</h2><span class="hint">the time frames you asked about; the yearly return and the falls are without deposits, so they show the book itself</span></header>
    <div class="body" id="tab"></div></div>
  <div class="panel" id="s-cmp"><header><h2>Your book, an index fund, a savings account</h2><span class="hint">the same years of history, the same deposits, the same tax — only what the money is in differs</span></header>
    <div class="body" id="cmp"></div></div>
  <div class="panel" id="s-b3"><header><h2>Box 3</h2><span class="hint">what each regime takes from the typical outcome, and the facts used</span></header>
    <div class="body" id="b3"></div></div>
  <div class="panel" id="s-money"><header><h2>Does more money make stocks grow?</h2><span class="hint">your reason to invest, tested on 16 countries, decade by decade</span></header>
    <div class="body" id="money"></div></div>
  <div class="panel" id="s-hist"><header><h2>The history it draws from</h2><span class="hint">16 markets, 1872–2020, after inflation</span></header>
    <div class="body" id="hist"></div></div>
  <div class="panel" id="s-track"><header><h2>Track record</h2><span class="hint">does the book land inside the range it was given?</span></header>
    <div class="body" id="track"></div></div>
  <div class="panel" id="s-how"><header><h2>How it works</h2><span class="hint">and what it leaves out</span></header>
    <div class="body" id="how"></div></div>`;
}

/* ==========================================================================
   loading
   ========================================================================== */
async function load() {
  scaffold();
  try { V = await api("/api/pf/fcst"); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load the forecast</b>${esc(e.message)}</div>`; return; }
  R = V.run;
  const p = R.params;
  S = { start: p.start, monthly: p.monthly, growth: p.growth, dep_years: p.dep_years, hist: p.hist, risk: p.risk,
        box3: p.box3, partner: p.partner, savings: p.savings, cost: p.cost, valuation: p.valuation };
  controls();
  renderRun();
  money(); history(); track(); how();
}

function schedule() {
  clearTimeout(TMR);
  $("#app").classList.add("busy");
  TMR = setTimeout(rerun, 260);
}

async function rerun() {
  const seq = ++SEQ;
  try {
    const r = await post("/api/pf/fcst/run", S);
    if (seq !== SEQ) return;
    R = r;
    renderRun();
  } catch (e) {
    flash(`The forecast could not run: ${esc(e.message)}`);
  } finally {
    if (seq === SEQ) $("#app").classList.remove("busy");
  }
}

/* ==========================================================================
   the controls
   ========================================================================== */
function seg(key, opts) {
  return `<span class="grp2">${opts.map(([v, l, t]) =>
    `<button class="seg ${String(S[key]) === String(v) ? "on" : ""}" data-k="${key}" data-v="${v}" title="${esc(t || "")}">${l}</button>`).join("")}</span>`;
}

function controls() {
  const b = V.base, dep = b.deposits || {}, rk = b.risk || {};
  $("#ctl").innerHTML = `
    <span><span class="lbl">Deposit</span>€ <input type="number" id="cMonthly" min="0" step="25" value="${S.monthly}"> a month,
      growing <input type="number" class="w5" id="cGrowth" step="0.5" value="${S.growth}">% a year above inflation,
      for <input type="number" class="w5" id="cYears" min="0" max="50" value="${S.dep_years}"> years</span>
    <span><span class="lbl">History</span>${seg("hist", [["world", "World", "all 16 markets together, weighted by economy: a spread-out investor"],
      ["country", "One market at a time", "as if your market could turn out to be any of the 16 — harsher"]])}</span>
    <span><span class="lbl">Risk</span>${seg("risk", [["book", "Your book", rk.available ? `beta ${fx(rk.beta, 2)} to the world, plus ${fx(rk.s * 100, 1)}% a year of its own` : "not measurable now"],
      ["index", "World index fund", "beta 1, nothing of its own, 0.20% a year in costs"]])}</span>
    <span><span class="lbl">Box 3</span>${seg("box3", [["bill", "2028 bill", "36% of the actual return every year, the first €1,800 free"],
      ["now", "Today's system", "36% of a deemed 6% on what is above €60,098, or of the actual return if lower"],
      ["none", "None", "no tax, to see what Box 3 costs"]])}
      <label class="ck" title="Each partner has his or her own tax-free amount"><input type="checkbox" id="cPartner" ${S.partner ? "checked" : ""}>fiscal partner</label>
      <span title="Savings use up part of the tax-free amount">savings elsewhere € <input type="number" id="cSavings" data-eur min="0" step="1000" value="${S.savings}"></span></span>
    <span><span class="lbl">Costs</span><input type="number" class="w5" id="cCost" min="0" max="10" step="0.05" value="${(+S.cost).toFixed(2)}">% a year
      <span class="why">${b.cost_pct != null ? `you paid ${fx(b.cost_pct, 2)}% over the last ${fx((b.costs || {}).years, 1)} years` : ""}</span></span>
    <span><span class="lbl">Start</span>€ <input type="number" class="w9" id="cStart" data-eur min="0" step="500" value="${Math.round(S.start)}">
      <span class="why">${b.value ? `your holdings: ${ec(b.value)}` : ""}</span></span>
    <label class="ck" title="Shiller's CAPE: price over ten years of average earnings. High starting prices have meant lower returns for the next ten years.">
      <input type="checkbox" id="cVal" ${S.valuation ? "checked" : ""}>today's high US prices (CAPE ${fx((b.cape || {}).value, 0)})</label>
    <span><span class="lbl">Show</span><span class="grp2">
      <button class="seg ${VIEW === "real" ? "on" : ""}" data-view="real" title="what the money buys today">Today's euros</button>
      <button class="seg ${VIEW === "nom" ? "on" : ""}" data-view="nom" title="the number on the statement then">Euros of the day</button></span></span>
    ${dep.n ? `<span class="about" style="flex-basis:100%;font-family:var(--f-ui);color:var(--ink-soft);font-size:.64rem">
      So far you put in ${ec(dep.per_month)} a month on average since ${dmy(dep.since)}, ${ec(dep.last12_per_month)} a month over the last twelve months.</span>` : ""}`;
  $$("#ctl .seg[data-k]").forEach(x => x.onclick = () => {
    S[x.dataset.k] = x.dataset.v;
    if (x.dataset.k === "risk") S.cost = x.dataset.v === "index" ? 0.2 : (V.base.cost_pct ?? 0.2);
    controls(); schedule();
  });
  $$("#ctl .seg[data-view]").forEach(x => x.onclick = () => { VIEW = x.dataset.view; controls(); renderRun(); });
  const num = (id, k, int) => { const el = $(id); el.oninput = () => { const v = parseFloat(el.value); if (!isNaN(v)) { S[k] = int ? Math.round(v) : v; schedule(); } }; };
  num("#cMonthly", "monthly"); num("#cGrowth", "growth"); num("#cYears", "dep_years", true);
  num("#cSavings", "savings"); num("#cCost", "cost"); num("#cStart", "start");
  $("#cPartner").onchange = e => { S.partner = e.target.checked; schedule(); };
  $("#cVal").onchange = e => { S.valuation = e.target.checked; schedule(); };
}

/* ==========================================================================
   the run: tiles, fan, table, comparisons, Box 3
   ========================================================================== */
function renderRun() {
  tiles(); fanBar(); fan(); table(); compare(); box3();
}

function tiles() {
  const p = R.params, unit = VIEW === "real" ? "today's euros" : "euros of the day";
  let h = tile("Today", ec(p.start), null, S.start === V.base.value ? "your holdings, without cash" : "as entered", true);
  for (const k of [1, 10, 30, 50]) {
    const a = at(rowOf(k), k);
    h += tile(`In ${yrs(k)}`, ec(a.p50), null, `${ec(a.p5)} – ${ec(a.p95)} · ${ec(a.put)} put in`, false,
      `typical outcome, ${unit}; 1 in 20 ends below the first figure, 1 in 20 above the second`);
  }
  const r10 = rowOf(10), r30 = rowOf(30);
  h += tile("Less than put in, 10 yrs", fx(r10.below_put_in, 0) + "%", null, "chance, in today's euros", false,
    "the share of paths worth less after ten years than everything you put in, measured in what it buys");
  if (p.box3 !== "none")
    h += tile("Box 3 takes, 30 yrs", fx(r30.box3_cost_pct, 0) + "%", null, `of the typical outcome, ${B3[p.box3]}`, false,
      "how much smaller the typical outcome is than with no Box 3, the same paths otherwise");
  $("#tiles").innerHTML = h;
}

function fanBar() {
  $("#fanBar").innerHTML = `<span class="grp">${HZNS.map(k => `<button class="seg ${k === HZN ? "on" : ""}" data-h="${k}">${k}Y</button>`).join("")}</span>
    <span class="grp" style="margin-left:.6rem">${[["lin", "Linear"], ["log", "Log"]].map(([v, l]) =>
      `<button class="seg ${(v === "log") === LOG ? "on" : ""}" data-s="${v}" title="${v === "log" ? "equal steps are equal growth: a doubling is the same height anywhere" : ""}">${l}</button>`).join("")}</span>`;
  $$("#fanBar .seg[data-h]").forEach(b => b.onclick = () => { HZN = +b.dataset.h; fanBar(); fan(); });
  $$("#fanBar .seg[data-s]").forEach(b => b.onclick = () => { LOG = b.dataset.s === "log"; fanBar(); fan(); });
  const p = R.params;
  $("#fanHint").textContent = `${VIEW === "real" ? "today's euros" : "euros of the day"} · ${HIST[p.hist]} · ${p.risk === "book" ? "your book's risk" : "a world index fund"} · ${B3[p.box3]}`;
}

/* The fan: bands for 5–95% and 25–75%, the typical path, and lines for what was put in and the alternatives. */
function fan() {
  const host = $("#fan");
  host.__draw = fan; CHARTS.add(host);
  const W = Math.max(280, Math.round(host.clientWidth || 700)); host.__w = W;
  const H = W < 560 ? 250 : 330;
  const real = VIEW === "real";
  const F = R.fan.slice(0, HZN + 1).map(f => {
    const a = real ? f.real : f.nom;
    return { y: f.y, b: a, put: real ? f.put_in : f.drawer_nom, idx: real ? f.index : f.index_nom,
             cash: real ? f.cash : f.cash_nom, drawer: real ? f.drawer : null };
  });
  const lines = [
    { k: "put", name: "Put in", col: "var(--ink-mid)", dash: "4 3", w: 1.2 },
    ...(R.params.risk === "book" ? [{ k: "idx", name: "Index fund, typical", col: "var(--s4)", dash: "6 3", w: 1.4 }] : []),
    { k: "cash", name: "Savings account, typical", col: "var(--s3)", dash: "2 3", w: 1.4 },
    ...(real ? [{ k: "drawer", name: "Cash in a drawer", col: "var(--ink-soft)", dash: "", w: 1 }] : []),
  ];
  let hi = 0, lo = Infinity;
  for (const f of F) {
    hi = Math.max(hi, f.b[6], f.put, f.idx, f.cash);
    lo = Math.min(lo, f.b[0], f.put, f.cash, f.drawer ?? Infinity);
  }
  const M = { l: 58, r: 64, t: 10, b: 22 }, pw = W - M.l - M.r, ph = H - M.t - M.b;
  const X = y => M.l + (y / Math.max(1, HZN)) * pw;
  let Y, ticks;
  if (LOG) {
    const a = Math.log10(Math.max(1, lo * 0.9)), b = Math.log10(hi * 1.05);
    Y = v => M.t + (b - Math.log10(Math.max(v, 1))) / (b - a) * ph;
    ticks = [];
    for (let e = Math.floor(a); e <= Math.ceil(b); e++) for (const m of [1, 2, 5]) { const v = m * 10 ** e; if (Math.log10(v) >= a && Math.log10(v) <= b) ticks.push(v); }
  } else {
    const t = nice(0, hi * 1.04, H < 260 ? 4 : 5);
    Y = v => M.t + (t.hi - v) / t.hi * ph;
    ticks = t.ticks;
  }
  let g = "";
  for (const v of ticks) g += `<line class="gl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}"/>
    <text class="ax" x="${M.l - 6}" y="${(Y(v) + 3).toFixed(1)}" text-anchor="end">${esc(ec(v))}</text>`;
  const step = HZN <= 2 ? 1 : HZN <= 5 ? 1 : HZN <= 10 ? 2 : HZN <= 20 ? 5 : 10;
  for (let y = 0; y <= HZN; y += step) g += `<text class="ax" x="${X(y).toFixed(1)}" y="${H - 5}" text-anchor="middle">${y ? THIS_YEAR + y : "now"}</text>`;
  const band = (i, j, op) => {
    const top = F.map(f => `${X(f.y).toFixed(1)} ${Y(f.b[j]).toFixed(1)}`), bot = F.map(f => `${X(f.y).toFixed(1)} ${Y(f.b[i]).toFixed(1)}`).reverse();
    return `<path d="M${top.join(" L")} L${bot.join(" L")} Z" fill="var(--accent)" opacity="${op}"/>`;
  };
  g += band(0, 6, 0.13) + band(2, 4, 0.22);
  const path = key => "M" + F.map(f => `${X(f.y).toFixed(1)} ${Y(key(f)).toFixed(1)}`).join(" L");
  for (const l of lines) if (F.every(f => f[l.k] != null))
    g += `<path d="${path(f => f[l.k])}" fill="none" stroke="${l.col}" stroke-width="${l.w}" ${l.dash ? `stroke-dasharray="${l.dash}"` : ""}/>`;
  g += `<path d="${path(f => f.b[3])}" fill="none" stroke="var(--accent)" stroke-width="2.2"/>`;
  const last = F[F.length - 1];
  const ends = [[last.b[6], "var(--accent)", "95%"], [last.b[3], "var(--ink)", "typical"], [last.b[0], "var(--accent)", "5%"]]
    .map(([v, c, n]) => ({ y: Y(v), v, c, n })).sort((a, b) => a.y - b.y);
  for (let i = 1; i < ends.length; i++) if (ends[i].y - ends[i - 1].y < 12) ends[i].y = ends[i - 1].y + 12;
  for (const e of ends) g += `<text class="el" x="${M.l + pw + 5}" y="${(e.y + 3).toFixed(1)}" fill="${e.c}">${esc(ec(e.v))}</text>`;
  g += `<line class="xh" x1="0" x2="0" y1="${M.t}" y2="${M.t + ph}" style="display:none"/>`;
  g += `<rect class="hov" x="${M.l}" y="${M.t}" width="${pw}" height="${ph}"/>`;
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="the range of outcomes by year">${g}</svg><div class="lc-tip" style="display:none"></div>`;
  $("#fkey").innerHTML = `<span><i class="box" style="background:var(--accent);opacity:.22"></i>half of the outcomes</span>
    <span><i class="box" style="background:var(--accent);opacity:.13"></i>nine in ten</span>
    <span><i style="border-color:var(--accent)"></i>typical (median)</span>` +
    lines.map(l => `<span><i style="border-color:${l.col};border-top-style:${l.dash ? "dashed" : "solid"}"></i>${esc(l.name)}</span>`).join("");
  const svg = host.querySelector("svg"), tip = host.querySelector(".lc-tip"), xh = svg.querySelector(".xh");
  svg.querySelector(".hov").addEventListener("mousemove", ev => {
    const r = svg.getBoundingClientRect(), px = (ev.clientX - r.left) * (W / r.width);
    const y = Math.max(0, Math.min(HZN, Math.round((px - M.l) / pw * HZN))), f = F[y];
    xh.setAttribute("x1", X(y)); xh.setAttribute("x2", X(y)); xh.style.display = "";
    const row = (n, v, c) => `<div class="r"><span>${c ? `<i class="sw" style="background:${c}"></i>` : ""}${n}</span><b>${ec(v)}</b></div>`;
    tip.innerHTML = `<div class="h">${y ? `${yrs(y)} · ${THIS_YEAR + y}` : "today"}</div>` +
      row("1 in 20 ends above", f.b[6]) + row("1 in 4 ends above", f.b[4]) + row("typical", f.b[3], "var(--accent)") +
      row("1 in 4 ends below", f.b[2]) + row("1 in 20 ends below", f.b[0]) +
      lines.map(l => f[l.k] != null ? row(l.name, f[l.k], l.col) : "").join("");
    tip.style.display = "";
    const hw = host.clientWidth, tw = tip.offsetWidth, sx = X(y) / W * r.width;
    tip.style.left = Math.max(0, Math.min(hw - tw, sx + (sx > hw / 2 ? -tw - 12 : 12))) + "px"; tip.style.top = "6px";
  });
  svg.querySelector(".hov").addEventListener("mouseleave", () => { tip.style.display = "none"; xh.style.display = "none"; });
  readout();
}

function readout() {
  const k = HZN, row = rowOf(k);
  if (!row) { $("#read").innerHTML = ""; return; }
  const a = at(row, k), unit = VIEW === "real" ? "today's euros" : "euros of the day";
  let s = `In ${yrs(k)} (${THIS_YEAR + k}), the typical outcome is <b>${ec(a.p50)}</b> in ${unit}, from ${ec(a.put)} put in.`;
  if (VIEW === "real") s += ` Half of the paths end between <b>${ec(a.p25)}</b> and <b>${ec(a.p75)}</b>; one in twenty below ${ec(a.p5)}, one in twenty above ${ec(a.p95)}.`;
  else s += ` One in twenty ends below ${ec(a.p5)}, one in twenty above ${ec(a.p95)}. In euros of the day the figures include inflation: a euro then buys less.`;
  s += ` ${fx(row.below_put_in, 0)}% of the paths are worth less than what went in, measured in what it buys.`;
  if (row.yearly) s += ` The book itself, without deposits, returns ${pc0(row.yearly.p50)} a year after inflation in the typical path (${pc0(row.yearly.p5)} to ${pc0(row.yearly.p95)}).`;
  $("#read").innerHTML = s;
}

function table() {
  const real = VIEW === "real";
  const head = real
    ? `<th>Horizon</th><th>Put in</th><th>1 in 20<small>ends below</small></th><th>1 in 4<small>ends below</small></th><th>Typical</th>
       <th>1 in 4<small>ends above</small></th><th>1 in 20<small>ends above</small></th>`
    : `<th>Horizon</th><th>Put in<small>euros of the day</small></th><th>1 in 20<small>ends below</small></th><th>Typical</th><th>1 in 20<small>ends above</small></th>`;
  let h = `<table class="tbl ht"><thead><tr>${head}<th>Below put in<small>chance, real</small></th>
    <th>A year, typical<small>after inflation, 5–95%</small></th><th>Worst fall on the way<small>typical · 1 in 10</small></th>
    <th>Box 3 paid<small>typical, today's €</small></th></tr></thead><tbody>`;
  for (const row of R.rows) {
    const k = row.y, a = at(row, k);
    const vals = real ? [a.p5, a.p25, a.p50, a.p75, a.p95] : [a.p5, a.p50, a.p95];
    const mid = real ? 2 : 1;
    h += `<tr><td>${yrs(k)} <span class="dim">${THIS_YEAR + k}</span></td><td class="put">${ec(a.put)}</td>
      ${vals.map((v, i) => `<td class="${i === mid ? "mid" : ""}">${ec(v)}</td>`).join("")}
      <td class="${row.below_put_in >= 25 ? "neg" : ""}">${fx(row.below_put_in, 0)}%</td>
      <td>${row.yearly ? `${pc0(row.yearly.p50)} <span class="rng">${pc0(row.yearly.p5, 0)} … ${pc0(row.yearly.p95, 0)}</span>` : "–"}</td>
      <td>${row.fall ? `${pc0(row.fall.p50, 0)} · <span class="neg">${pc0(row.fall.p10, 0)}</span>` : "–"}</td>
      <td>${row.tax_real ? ec(row.tax_real) : "–"}</td></tr>`;
  }
  h += `</tbody></table>
    <p class="read">The falls are measured from one year end to the next, so a crash that recovered within the year does not show: the real
    ones were deeper. "Below put in" compares what the money buys with what you paid in, both in today's euros.</p>`;
  $("#tab").innerHTML = h;
  wrapTables();
}

function compare() {
  const b = V.base, rk = b.risk || {}, p = R.params;
  const r10 = rowOf(10), r30 = rowOf(30), r50 = rowOf(50);
  const cards = rk.available ? `<div class="cards">
      <div class="card"><div class="k">Beta to the world</div><div class="v">${fx(rk.beta, 2)}</div>
        <div class="t">when world shares move 1%, your book moved ${fx(rk.beta, 2)}% (weekly, in euros, ${rk.weeks} weeks, today's holdings)</div></div>
      <div class="card"><div class="k">A swing of its own</div><div class="v">${fx(rk.s * 100, 1)}%</div>
        <div class="t">a year, on top of the market: what the ${rk.priced} holdings did that the world did not</div></div>
      <div class="card"><div class="k">Volatility</div><div class="v">${fx(rk.vol * 100, 1)}%</div>
        <div class="t">a year over the two years, against ${fx(rk.mvol * 100, 1)}% for the world index in euros; R² ${fx(rk.r2, 2)}</div></div>
      <div class="card"><div class="k">Costs</div><div class="v">${fx(p.cost, 2)}%</div>
        <div class="t">a year in this run${b.cost_pct != null ? `; you paid ${fx(b.cost_pct, 2)}% a year so far, which falls as the account grows` : ""}</div></div>
    </div>` : `<div class="note plain">${esc(rk.why || "The book's risk could not be measured.")} The run uses an index fund's risk.</div>`;
  const rows = [["Your book", "", r => r.real.p50], ["World index fund", "beta 1, 0.20% costs", r => r.index],
    ["Savings account", "the cash rate of the same years", r => r.cash], ["Cash in a drawer", "inflation eats it", r => r.drawer],
    ["Put in", "", r => r.put_in]];
  const t = `<table class="tbl" style="margin-top:.7rem"><thead><tr><th>Typical outcome, today's euros</th><th></th><th>10 years</th><th>30 years</th><th>50 years</th></tr></thead><tbody>
    ${rows.map(([n, w, f]) => `<tr><td>${n}</td><td class="l dim">${w}</td>${[r10, r30, r50].map(r => `<td>${ec(f(r))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  const d30 = r30.real.p50 - r30.index;
  const txt = p.risk === "book" && rk.available ? `<div class="txt"><p>Your book carries more market than an index fund (beta ${fx(rk.beta, 2)}), which
    adds return on average, and a swing of its own, which adds none. Swings cost compounding: lose 50% and you need +100% to get back.
    That is <b>volatility drag</b>, and it is why a riskier book can end below a calmer one with the same average.
    After 30 years the typical path of your book ends ${d30 >= 0 ? `<b>${ec(d30)} above</b>` : `<b>${ec(-d30)} below</b>`} the index fund's.
    The range is ${(r30.real.p95 - r30.real.p5) > (r30.index_p95 - r30.index_p5) ? "wider" : "narrower"}: after 30 years one in twenty of your book's paths ends below ${ec(r30.real.p5)} and one in twenty above
    ${ec(r30.real.p95)}; for the fund those are ${ec(r30.index_p5)} and ${ec(r30.index_p95)}.</p></div>` : "";
  $("#cmp").innerHTML = cards + t + txt;
}

/* what a regime takes, as a share of the untaxed outcome; nothing yet reads as such, not as "−0%" */
const lost = (v, none) => { const x = (1 - v / none) * 100; return x < 0.05 ? "nothing yet" : x < 0.5 ? "under 1%" : `−${Math.round(x)}%`; };

function box3() {
  const b3 = V.box3, p = R.params, rs = [10, 30, 50].map(rowOf);
  const card = (rg, title, sub) => `<div class="card ${p.box3 === rg ? "on" : ""}"><div class="k">${title}</div>
    <div class="v">${ec(rs[1].box3_by[rg])} <small class="dim" style="font-size:.62rem">in 30 years</small></div>
    <div class="t">${rs.map(r => `${r.y} yrs ${ec(r.box3_by[rg])}${rg !== "none" ? ` (${lost(r.box3_by[rg], r.box3_by.none)})` : ""}`).join(" · ")}<br>${sub}</div></div>`;
  $("#b3").innerHTML = `<div class="cards b3c">
      ${card("bill", "The 2028 bill", "36% of the actual return, every year")}
      ${card("now", "Today's system", "36% of a deemed 6%, above the tax-free amount")}
      ${card("none", "No Box 3", "for comparison")}
    </div>
    <div class="two" style="margin-top:.7rem"><div class="txt">
      <div class="sec">The 2028 bill</div><p>${esc(b3.bill.what)}</p>
      <p><b>Status.</b> ${esc(b3.bill.status)}</p>
      <p><b>Not used.</b> ${esc(b3.bill.considered)}</p>
      <p><b>Assumed.</b> ${esc(b3.bill.assumed)}</p>
      <p>The first year ahead (${THIS_YEAR + 1}) is still taxed under today's system: the bill starts in ${b3.bill.start}.</p>
    </div><div class="txt">
      <div class="sec">Today's system</div><p>${esc(b3.now.what)}</p>
      <div class="sec">Why the bill costs so much more here</div>
      <p>It taxes the whole return, inflation included, every year, on gains you have not sold. On a book that grows
      faster than the deemed 6%, that is more than today's system takes — and the €1,800 does not grow, so over decades
      it covers less and less. A loss is carried forward, which softens a crash year, but never back.</p>
      <div class="sec">Sources, as of ${dmy(b3.asof)}</div>
      <div class="src">${b3.sources.map(([n, u]) => `<div><a href="${esc(u)}" target="_blank" rel="noopener">${esc(n)}</a></div>`).join("")}</div>
    </div></div>`;
}

/* ==========================================================================
   the static panels: the money test, the history, the track record, how
   ========================================================================== */
function money() {
  const T = V.thesis;
  if (!T) { $("#money").innerHTML = `<div class="empty">Not in this build's data.</div>`; return; }
  const c = T.corr;
  $("#money").innerHTML = `<div class="two"><div>
      <div class="sec">Each dot is one country in one decade: money growth against the stock return after inflation</div>
      <div class="lc" id="mscat"></div>
    </div><div>
      <div class="cards">
        <div class="card"><div class="k">Money → inflation</div><div class="v">${fx(c.money_inf, 2)}</div><div class="t">correlation: faster money growth came with faster price rises, almost one for one</div></div>
        <div class="card"><div class="k">Money → stocks, real</div><div class="v neg">${fx(c.money_real, 2)}</div><div class="t">faster money growth came with <i>lower</i> stock returns after inflation</div></div>
        <div class="card"><div class="k">Money → stocks, nominal</div><div class="v">${fx(c.money_nom, 2)}</div><div class="t">in euros of the day, a weak link: more money did lift the number a little</div></div>
        <div class="card"><div class="k">Inflation → stocks, real</div><div class="v neg">${fx(c.inf_real, 2)}</div><div class="t">the reason: the decades with high inflation were the bad ones for shares</div></div>
      </div>
      <table class="tbl" style="margin-top:.6rem"><thead><tr><th>Money growth a year</th><th>Decades</th><th>Inflation</th><th>Stocks, real</th><th>Lost to inflation</th></tr></thead>
      <tbody>${T.buckets.map(x => `<tr><td>${esc(x.label)}</td><td>${x.n}</td><td>${fx(x.inf, 1)}%</td><td>${pc0(x.real)}</td><td>${x.loss_pct}% of decades</td></tr>`).join("")}</tbody></table>
    </div></div>
    <div class="txt" style="margin-top:.5rem"><p><b>What holds.</b> Banks create money when they lend, the money supply grows, and over decades
    prices rise with it (+${fx(c.money_inf, 2)}). Shares are claims on companies whose sales and profits are in money, so they keep up with
    inflation in the long run and add real growth on top: about ${fx(V.summary.world.geo * 100, 1)}% a year after inflation for the 16 markets together.</p>
    <p><b>What does not.</b> The extra money does not flow into stocks as extra return. Decades with money growth of 10% a year or more had
    the <i>worst</i> real stock returns, and lost to inflation more than a third of the time: high inflation pushes rates up and valuations down.
    Stocks grow because companies earn and reinvest, not because the money supply grows. So the forecast works in real terms, and money
    supply is not one of its inputs.</p></div>`;
  scatter($("#mscat"), T.points.map(p => [p.money, p.real]), { xl: "money growth, % a year", yl: "real stock return, % a year", xu: "%", yu: "%", height: 260 });
}

function history() {
  const sm = V.summary, w = sm.world, ab = V.about || {}, cp = V.base.cape || {};
  const name = { AUS: "Australia", BEL: "Belgium", CHE: "Switzerland", DEU: "Germany", DNK: "Denmark", ESP: "Spain", FIN: "Finland",
    FRA: "France", GBR: "United Kingdom", ITA: "Italy", JPN: "Japan", NLD: "Netherlands", NOR: "Norway", PRT: "Portugal", SWE: "Sweden", USA: "United States" };
  const cs = Object.entries(sm.countries).sort((a, b) => b[1].geo - a[1].geo);
  $("#hist").innerHTML = `<div class="two"><div>
      <div class="cards">
        <div class="card"><div class="k">World, a year after inflation</div><div class="v">${pc0(w.geo * 100)}</div><div class="t">compounded, ${w.from}–${w.to}; the average year ${pc0(w.mean * 100)}, swinging ${fx(w.sd * 100, 0)}%</div></div>
        <div class="card"><div class="k">Worst and best year</div><div class="v"><span class="neg">${pc0(w.worst[0] * 100, 0)}</span> / <span class="pos">${pc0(w.best[0] * 100, 0)}</span></div><div class="t">${w.worst[1]} and ${w.best[1]}</div></div>
        <div class="card"><div class="k">Dutch inflation, cash</div><div class="v">${fx(w.inf_geo * 100, 1)}% · ${pc0(w.bill_geo * 100)}</div><div class="t">a year: prices, and the cash rate after inflation</div></div>
      </div>
      <div class="txt" style="margin-top:.5rem"><p>The <b>world</b> view weights each market by the size of its economy the year before; the
      <b>one market at a time</b> view lets a path land in any of them, Japan after 1989 and Germany in two wars included. Years of
      hyperinflation or currency reform are left out (${sm.dropped.length}: ${esc(sm.dropped.slice(0, 8).join(", "))}…).
      Currency moves are left out too: over decades they roughly even out, and the dataset's yearly-average exchange rates
      invent jumps that never happened.</p>
      <p><b>Today's prices.</b> Shiller's US CAPE is ${fx(cp.value, 1)} (${esc(cp.asof || "")}). Since 1881 a starting CAPE has explained about
      ${fx((cp.r2 || 0) * 100, 0)}% of the next ten years' return; at today's level the fit says ${pc0((cp.pred10 || 0) * 100)} a year after
      inflation, against ${pc0((cp.avg10 || 0) * 100)} on average. Only two periods started this high (1929 and 1997–2001), so the
      checkbox applies it as a scenario, not as the forecast.</p>
      <p class="dim" style="font-size:.66rem">${esc(ab.cite || "")} ${esc(ab.license || "")}</p></div>
    </div><div>
      <table class="tbl"><thead><tr><th>Market</th><th>Years</th><th>A year, real</th><th>Swing</th><th>Worst year</th></tr></thead>
      <tbody>${cs.map(([c, x]) => `<tr><td>${esc(name[c] || c)}</td><td>${x.from}–${x.to}</td><td>${pc0(x.geo * 100)}</td><td>${fx(x.sd * 100, 0)}%</td>
        <td><span class="neg">${pc0(x.worst[0] * 100, 0)}</span> <span class="dim">${x.worst[1]}</span></td></tr>`).join("")}</tbody></table>
    </div></div>`;
  wrapTables();
}

function track() {
  const T = V.track || {};
  if (!T.n) {
    $("#track").innerHTML = `<div class="empty">No range kept yet. Each week the page keeps the range it gives for your book's next twelve
      months; a year later it is set against what the book actually did.${T.error ? ` <span class="neg">${esc(T.error)}</span>` : ""}</div>`;
    return;
  }
  const L = T.latest;
  let h = `<div class="txt"><p>${T.n} range${T.n === 1 ? "" : "s"} kept since ${dmy(T.first)}. The latest, ${dmy(L.d)}: your book's next twelve
    months between <b>${pc0(L.q.p5)}</b> and <b>${pc0(L.q.p95)}</b> (nine in ten), typically ${pc0(L.q.p50)} — before deposits and tax,
    after costs, in euros of the day.${T.next_due ? ` The first comparison is due on ${dmy(T.next_due)}.` : ""}</p></div>`;
  if (T.rows && T.rows.length) {
    h += `<table class="tbl"><thead><tr><th>Given on</th><th>Year to</th><th>Range, 5–95%</th><th>Typical</th><th>Actual</th><th>Fell in</th></tr></thead><tbody>
      ${T.rows.map(r => `<tr><td>${dmy(r.d)}</td><td>${dmy(r.to)}</td><td>${pc0(r.range.p5)} … ${pc0(r.range.p95)}</td><td>${pc0(r.range.p50)}</td>
        <td>${pc0(r.actual)}</td><td class="${/below|above/.test(r.band) ? "neg" : ""}">${esc(r.band)}</td></tr>`).join("")}</tbody></table>
      <p class="read">${T.inside_pct}% landed inside the 5–95% range. If the ranges are honest, that should be about nine in ten over many years.</p>`;
  }
  $("#track").innerHTML = h;
}

function how() {
  const s = V.summary, R0 = R;
  $("#how").innerHTML = `<div class="two"><div class="txt">
      <div class="sec">The method</div>
      <ul>
        <li><b>History, not a formula.</b> ${fx(R0.paths, 0)} paths of 50 years are drawn from ${s.world.from}–${s.world.to}, in runs of about
          ${R0.block} consecutive years at a time, so a bad decade stays a bad decade and each year's inflation comes with its returns.</li>
        <li><b>Your book is the market, scaled.</b> Its weekly return in euros over the last two years, on today's holdings, is split into
          what moved with world shares (beta) and what it did on its own. A path gives the book the cash rate plus beta times the market's
          excess, and a random swing of its own that adds risk and no return.</li>
        <li><b>Deposits</b> go in through each year and grow by the rate you set, in today's euros. <b>Costs</b> come off every year.</li>
        <li><b>Box 3</b> is paid out of the portfolio at each year end, in euros of the day, as the regime you pick describes.</li>
        <li><b>Today's euros</b> divide by each path's own inflation (Dutch inflation in the world view, the market's own in the other).</li>
        <li>The same draws are used for every setting, so moving a slider changes the setting, not the luck.</li>
      </ul></div><div class="txt">
      <div class="sec">What it leaves out</div>
      <ul>
        <li>Markets that went to zero — Russia in 1917, China in 1949 — are not in the data. Even the harsher view is kind to the past.</li>
        <li>Years of hyperinflation are dropped, because the data for them is unreliable — Germany in the early 1920s, Italy 1943–47, Japan 1945 and 1948. Those were among the worst years for anyone holding money, so this is kind too.</li>
        <li>The data is yearly and ends in 2020. A crash that recovered within the year does not show.</li>
        <li>Currency: the book's dollar exposure is in its measured risk, but the history itself is in each market's own money.</li>
        <li>The book's risk is two years of today's holdings. A book changes over fifty years; set Risk to the index fund to see a calmer one.</li>
        <li>Turbos and other leverage are not modelled: they can lose everything well before fifty years, which no average captures.</li>
        <li>No withdrawals: you have no exit plan yet, so the money stays in.</li>
        <li>Box 3 follows the bill as passed; the Eerste Kamer may still change it.</li>
      </ul></div></div>`;
}

load();
