/* ==========================================================================
   BOND — the bond market, the second page of MKT.

   Nine parts load in parallel from /api/pf/bonds/{part} and each renders the
   moment it lands; a slow one (the portfolio what-if downloads five years of
   prices the first time) never holds up the rest. Long history for the
   charts comes separately from /api/pf/bonds/history, thinned on the server.

   The formatting, the controls and the chart come from kit.js.
   ========================================================================== */

const PARTS = ["us", "europe", "inflation", "global", "credit", "junk", "auctions", "news", "stocks", "portfolio", "path"];
const D = {};
const SECTIONS = [
  ["s-sum", "Summary"], ["s-curve", "Curve"], ["s-yields", "Yields"], ["s-infl", "Inflation"],
  ["s-cb", "Central banks"], ["s-path", "Rate path"], ["s-stocks", "Stocks & rates"], ["s-book", "Your portfolio"],
  ["s-eu", "Europe"], ["s-world", "World"], ["s-credit", "Credit"], ["s-junk", "Junk"], ["s-auct", "Auctions"],
  ["s-news", "News"], ["s-src", "Sources"],
];
const SEC_OF = { us: "s-curve", fed: "s-cb", path: "s-path", inflation: "s-infl", stocks: "s-stocks",
  portfolio: "s-book", europe: "s-eu", global: "s-world", credit: "s-credit", junk: "s-junk", auctions: "s-auct" };
const SEC_NAME = { us: "Treasuries", fed: "The Fed", path: "Rate path", inflation: "Inflation", stocks: "Stocks",
  portfolio: "Your book", europe: "Europe", global: "World", credit: "Credit", junk: "Junk", auctions: "Auctions" };

/* ---------- this page's loading states ------------------------------------ */
const failed = p => D[p] && D[p].error ?
  `<div class="note"><b>Could not load this part</b>${esc(D[p].error)}</div>` :
  `<div class="dim ld">Loading<span class="blink">_</span></div>`;
const ok = p => D[p] && !D[p].error;

/* history cache: one fetch per key set and start */
const HC = {};
async function hist(keys, start) {
  const k = keys.join(",") + "|" + start;
  if (!HC[k]) HC[k] = fetch(`/api/pf/bonds/history?keys=${encodeURIComponent(keys.join(","))}&start=${start}`)
    .then(r => r.json()).catch(e => ({ series: {}, error: String(e) }));
  return HC[k];
}

/* ==========================================================================
   scaffold
   ========================================================================== */
$("#shell").innerHTML = termBar({
  here: "MKT · BONDS",
  right: `<button class="tbtn" id="reloadBtn">Reload</button>`,
});
wireShell();

function scaffold() {
  const P = (id, title, hint, body) => `<div class="panel" id="${id}"><header><h2>${title}</h2>
      <span class="hint">${hint}</span></header><div class="body">${body}</div></div>`;
  $("#app").innerHTML = `
  <nav class="mtabs"><a href="/m/MKT">Overview</a><a class="on" href="/m/MKT/bonds">Bonds &amp; rates</a><a href="/m/MKT/oil">Oil</a></nav>
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>BONDS &amp; RATES</h1><span class="sub" id="topSub">US Treasuries in depth · Europe and the world in overview</span></div>
    <div class="tiles" id="tiles">${failed("us")}</div>
  </div></div>
  <nav class="secnav" id="secnav">${SECTIONS.map(([id, t]) => `<a href="#${id}" data-id="${id}">${t}</a>`).join("")}</nav>
  ${P("s-sum", "What matters now", "read from every panel below — click one to jump there",
      `<div class="two"><div><div class="insl" id="insBox"><div class="dim ld">Reading the panels<span class="blink">_</span></div></div></div>
       <div><div class="grp" style="margin-top:0">Coming up</div><div id="coming"></div></div></div>`)}
  ${P("s-curve", "The Treasury curve", "every maturity from one month to thirty years", `<div id="b-curve">${failed("us")}</div>`)}
  ${P("s-yields", "2, 10, 20 and 30-year yields", "levels, changes in basis points, and where they sit in history", `<div id="b-yields">${failed("us")}</div>`)}
  ${P("s-infl", "Inflation", "what bonds are protecting against — and what they price", `<div id="b-infl">${failed("inflation")}</div>`)}
  ${P("s-cb", "Central banks", "the short end is set here", `<div id="b-cb">${failed("us")}</div>`)}
  ${P("s-path", "The rate path", "what futures price for every meeting ahead", `<div id="b-path">${failed("path")}</div>`)}
  ${P("s-stocks", "Stocks &amp; rates", "what yields do to equities — and to your holdings", `<div id="b-stocks">${failed("stocks")}</div>`)}
  ${P("s-book", "Bonds in your portfolio", "a what-if with funds a euro investor can buy — history, not advice", `<div id="b-book">${failed("portfolio")}</div>`)}
  ${P("s-eu", "Europe", "the euro curve, Bunds and the spreads between member states", `<div id="b-eu">${failed("europe")}</div>`)}
  ${P("s-world", "The world", "ten-year yields and policy rates in eleven markets", `<div id="b-world">${failed("global")}</div>`)}
  ${P("s-credit", "Credit &amp; mortgages", "what companies and home buyers pay over Treasuries", `<div id="b-credit">${failed("credit")}</div>`)}
  ${P("s-junk", "Junk bonds", "high yield by rating: what it pays, what that says about defaults, and how it behaves when stocks fall", `<div id="b-junk">${failed("junk")}</div>`)}
  ${P("s-auct", "Treasury auctions", "what the US is selling, and how well it sold", `<div id="b-auct">${failed("auctions")}</div>`)}
  ${P("s-news", "Bond news", "headlines about rates, central banks and inflation", `<div id="b-news">${failed("news")}</div>`)}
  ${P("s-src", "Sources", "every number on this page, and where it came from", `<div id="b-src"><div class="dim ld">…</div></div>`)}`;

  const bar = document.querySelector(".term-bar");
  const setTop = () => document.documentElement.style.setProperty("--barh", (bar ? bar.offsetHeight : 36) + "px");
  setTop(); window.addEventListener("resize", setTop);
  const links = $$("#secnav a");
  const io = new IntersectionObserver(es => {
    for (const e of es) if (e.isIntersecting) links.forEach(a => a.classList.toggle("on", a.dataset.id === e.target.id));
  }, { rootMargin: "-40% 0px -55% 0px" });
  SECTIONS.forEach(([id]) => { const el = document.getElementById(id); el && io.observe(el); });
}

async function fetchPart(p) {
  try {
    const r = await fetch(`/api/pf/bonds/${p}`);
    D[p] = r.ok ? await r.json() : { error: `HTTP ${r.status}` };
  } catch (e) { D[p] = { error: String(e.message || e) }; }
  try { (RENDER[p] || (() => {}))(); } catch (e) { console.error(p, e); }
  wrapTables();
  renderSummary();
  if (PARTS.every(x => D[x])) renderSources();
}

function load() {
  for (const k of Object.keys(HC)) delete HC[k];
  PARTS.forEach(p => delete D[p]);
  scaffold();
  PARTS.forEach(fetchPart);
}
$("#reloadBtn").onclick = load;

/* ==========================================================================
   header tiles
   ========================================================================== */
function renderTop() {
  if (!ok("us")) { if (D.us) $("#tiles").innerHTML = failed("us"); return; }
  const U = D.us, K = Object.fromEntries(U.keys.map(k => [k.k, k])), L = U.live || {};
  const liveOf = { DGS10: "^TNX", DGS30: "^TYX", DGS5: "^FVX" };
  const t = [];
  for (const [sid, lab] of [["DGS2", "US 2-year"], ["DGS10", "US 10-year"], ["DGS20", "US 20-year"], ["DGS30", "US 30-year"]]) {
    const k = K[sid]; if (!k) continue;
    const q = L[liveOf[sid]];
    const live = q && q.last != null && q.date >= k.asof;
    t.push(tile(lab, yv(live ? q.last : k.last, live ? 3 : 2), bpc(live ? q.chg_bp : k.d1),
      live ? `<i class="live-dot"></i>Cboe, ${dm(q.date)} · close ${yv(k.last)}` : `close ${dm(k.asof)} · ${pctl(k.pct_2000)} since 2000`,
      sid === "DGS10", live ? "Intraday from Cboe's yield index; the close is the Treasury's own curve" : "The Treasury's daily par curve"));
  }
  const s = (U.spreads || []).find(x => x.k === "2s10s");
  if (s) t.push(tile("2s10s curve", `${s.last_bp > 0 ? "+" : ""}${s.last_bp}bp`, bpc(s.d1), s.inverted ? "inverted" : `${bp(s.m1)} in a month`));
  const A = U.anatomy || {};
  if (A.real != null) t.push(tile("10y real yield", yv(A.real), null, `TIPS · ${bp(A.real_y1)} in a year`));
  if (A.breakeven != null) t.push(tile("10y breakeven", yv(A.breakeven), null, "inflation priced for 10 years"));
  if (U.move) t.push(tile("MOVE", U.move.last.toFixed(0), null, `bond volatility · ${pctl(U.move.pct_1y)} of year`));
  if (ok("path") && D.path.fed && D.path.fed.meetings.length) {
    const ms = D.path.fed.meetings, e = ms[ms.length - 1];
    t.push(tile("Fed, priced", yv(e.after), bpc(e.cum_bp), `after ${mon(e.date)} · next ${dm(ms[0].date)}: ${ms[0].hike >= 50 ? ms[0].hike.toFixed(0) + "% hike" : ms[0].cut >= 50 ? ms[0].cut.toFixed(0) + "% cut" : ms[0].hold.toFixed(0) + "% hold"}`,
      false, "What fed funds futures price for the last meeting they cover — the Rate path panel has every meeting"));
  }
  if (ok("europe")) {
    const b = (D.europe.bunds || []).find(x => x.k === "BUND10");
    if (b) t.push(tile("Bund 10-year", yv(b.last), bpc(b.d1), `Bundesbank, ${dm(b.asof)}`));
    const g = D.europe.us_bund;
    if (g) t.push(tile("US − Bund 10y", `${g.last_bp}bp`, null, `${bp(g.y1)} in a year`));
  }
  $("#tiles").innerHTML = t.join("");
  $("#topSub").innerHTML = `Treasury close ${dmy(U.asof)} · curve ${esc(U.shape)} · intraday yields from Cboe where newer`;
}

/* ==========================================================================
   the curve
   ========================================================================== */
const CURVE_COL = { "Today": COL.ink, "1 week ago": COL.s4, "1 month ago": COL.s1, "3 months ago": COL.s3, "1 year ago": COL.s2, "3 years ago": COL.mid };
let CURVE_ON = ["Today", "1 month ago", "1 year ago"];
let CURVE_VS = "1 month ago";

function renderCurve() {
  if (!ok("us")) { $("#b-curve").innerHTML = failed("us"); return; }
  const U = D.us;
  const regs = Object.entries(U.regime || {});
  const cls = k => k.startsWith("bear") ? "bear" : k.startsWith("bull") ? "bull" : k.startsWith("twist") ? "twist" : "";
  $("#b-curve").innerHTML = `
    <div class="two">
      <div>
        <div class="fbar"><span class="lbl">Show</span>${chips("curveChips", U.curves.map(c => ({ k: c.label, name: `${c.label}${c.label !== "Today" ? "" : " " + dm(c.date)}`, color: CURVE_COL[c.label] })), CURVE_ON)}</div>
        <div class="lc" id="curveChart"></div>
        <div class="fbar" style="margin-top:.6rem"><span class="lbl">Change since</span>
          <span class="grp" id="curveVs">${U.curves.filter(c => c.label !== "Today").map(c => `<button class="seg ${c.label === CURVE_VS ? "on" : ""}" data-v="${c.label}">${c.label.replace(" ago", "")}</button>`).join("")}</span></div>
        <div class="lc" id="curveChg"></div>
      </div>
      <div>
        <div class="cards" style="grid-template-columns:1fr">
          <div class="card"><div class="k">Shape today</div><div class="v">${esc(U.shape)}</div>
            <div class="t">${shapeText(U)}</div></div>
        </div>
        <div class="regs" style="grid-template-columns:1fr">${regs.map(([h, r]) => `<div class="reg"><div class="k">Last ${esc(h)} · 2y ${bp(r.d2)}, 10y ${bp(r.d10)}</div>
          <div class="n ${cls(r.k)}">${esc(r.k)}</div><div class="t">${esc(r.text)}</div></div>`).join("")}</div>
      </div>
    </div>
    <div class="grp">Curve spreads</div>
    <div class="tscroll"><table class="tbl"><thead><tr><th>Spread</th><th>Now</th><th>1D</th><th>1M</th><th>3M</th><th>1Y</th><th>vs since 1990</th><th class="l">What it tells you</th></tr></thead>
    <tbody>${(U.spreads || []).map(s => `<tr><td>${esc(s.k)} <span class="dim">${esc(s.label)}</span></td>
      <td><b class="${s.last_bp < 0 ? "neg" : ""}">${s.last_bp > 0 ? "+" : ""}${s.last_bp}bp</b></td><td>${bpc(s.d1)}</td><td>${bpc(s.m1)}</td><td>${bpc(s.m3)}</td><td>${bpc(s.y1)}</td>
      <td>${ord(s.pct_1990)}${pbar(s.pct_1990)}</td><td class="w">${esc(s.why)}${inversions(s)}</td></tr>`).join("")}</tbody></table></div>
    <div class="src">Par yields from the U.S. Treasury's daily yield curve (same day), history from FRED since 1990. Bear = yields up (bond prices down), bull = yields down; steepener = long minus short widening.</div>`;
  wireChips("curveChips", CURVE_ON, drawCurve);
  $$("#curveVs .seg").forEach(b => b.onclick = () => {
    CURVE_VS = b.dataset.v; $$("#curveVs .seg").forEach(x => x.classList.toggle("on", x === b)); drawCurveChg();
  });
  drawCurve(); drawCurveChg();
}

function shapeText(U) {
  const c = (U.curves[0] || {}).points || [], v = Object.fromEntries(c.map(p => [p.t, p.v]));
  const parts = [];
  if (v["3M"] != null && v["10Y"] != null) parts.push(`3-month ${yv(v["3M"])}, 10-year ${yv(v["10Y"])}, 30-year ${yv(v["30Y"])}.`);
  const lo = c.reduce((a, p) => (a == null || p.v < a.v ? p : a), null), hi = c.reduce((a, p) => (a == null || p.v > a.v ? p : a), null);
  if (lo && hi) parts.push(`Lowest point: ${lo.t} (${yv(lo.v)}); highest: ${hi.t} (${yv(hi.v)}).`);
  return esc(parts.join(" "));
}

function inversions(s) {
  const e = s.episodes || [];
  if (!e.length) return "";
  return `<br><span class="dim">Inverted: ${e.map(x => `${mon(x.from)}–${x.to ? mon(x.to) : "now"} (low ${x.low}bp)`).join(" · ")}</span>`;
}

function drawCurve() {
  const U = D.us, cats = U.tenors.map(t => t.t);
  lchart($("#curveChart"), {
    xType: "cat", cats, height: 250, label: "Treasury yield curve", noLegend: true,
    yfmt: v => v.toFixed(2) + "%",
    series: U.curves.filter(c => CURVE_ON.includes(c.label)).map(c => ({
      name: c.label === "Today" ? `Today ${dm(c.date)}` : `${c.label} (${dm(c.date)})`,
      color: CURVE_COL[c.label], w: c.label === "Today" ? 2.4 : 1.4,
      pts: c.points.map(p => [cats.indexOf(p.t), p.v]).filter(p => p[0] >= 0).sort((a, b) => a[0] - b[0]),
    })),
  });
}

function drawCurveChg() {
  const U = D.us, cats = U.tenors.map(t => t.t);
  const now = Object.fromEntries((U.curves[0].points || []).map(p => [p.t, p.v]));
  const then = Object.fromEntries(((U.curves.find(c => c.label === CURVE_VS) || {}).points || []).map(p => [p.t, p.v]));
  const vals = cats.map(t => now[t] != null && then[t] != null ? Math.round((now[t] - then[t]) * 100) : null);
  const mx = Math.max(10, ...vals.filter(v => v != null).map(Math.abs));
  $("#curveChg").innerHTML = `<div style="display:grid;grid-template-columns:repeat(${cats.length},1fr);gap:3px;height:96px">
    ${vals.map((v, i) => {
      const h = v == null ? 0 : Math.max(1, Math.abs(v) / mx * 34);
      const lab = v == null ? "" : (v > 0 ? "+" : "") + v;
      return `<div style="position:relative;height:96px" title="${cats[i]}: ${bp(v)}">
        <div style="position:absolute;left:0;right:0;top:48px;height:1px;background:var(--rule-strong)"></div>
        <div style="position:absolute;left:14%;right:14%;${v >= 0 ? `bottom:48px;height:${h}px;background:var(--gain)` : `top:49px;height:${h}px;background:var(--loss)`}"></div>
        <div class="${sgn(v)}" style="position:absolute;left:0;right:0;text-align:center;font-size:.6rem;${v >= 0 ? `bottom:${50 + h}px` : `top:${51 + h}px`}">${lab}</div>
      </div>`;
    }).join("")}</div>
    <div style="display:grid;grid-template-columns:repeat(${cats.length},1fr);gap:3px;font-size:.58rem;color:var(--ink-soft);text-align:center">${cats.map(c => `<span>${c}</span>`).join("")}</div>
    <div class="src" style="margin-top:.2rem">Change in basis points, per maturity, since ${esc(CURVE_VS)}.</div>`;
}

/* ==========================================================================
   yields over time
   ========================================================================== */
const YTABS = {
  yields: { name: "Yields", unit: "%", keys: [["DGS3MO", "3-month", COL.s4], ["DGS2", "2-year", COL.s1], ["DGS5", "5-year", COL.mid, "4 3"], ["DGS10", "10-year", COL.ink], ["DGS20", "20-year", COL.s3], ["DGS30", "30-year", COL.s2]], on: ["DGS2", "DGS10", "DGS20", "DGS30"], range: 1826 },
  spreads: { name: "Curve spreads", unit: "bp", keys: [["S2s10s", "10y − 2y", COL.ink], ["S3m10y", "10y − 3m", COL.s1], ["S5s30s", "30y − 5y", COL.s2], ["S10s30s", "30y − 10y", COL.s3]], on: ["S2s10s", "S3m10y"], range: 7305, zero: true },
  real: { name: "Real yield & inflation", unit: "%", keys: [["DGS10", "10y nominal", COL.ink], ["DFII10", "10y real (TIPS)", COL.s1], ["T10YIE", "10y breakeven", COL.s2], ["T5YIFR", "5y5y forward", COL.s3]], on: ["DGS10", "DFII10", "T10YIE"], range: 7305 },
  tp: { name: "Term premium", unit: "%", keys: [["DGS10", "10-year", COL.ink], ["THREEFYTP10", "term premium (Kim-Wright)", COL.s2]], on: ["DGS10", "THREEFYTP10"], range: 7305, zero: true },
  move: { name: "Bond volatility", unit: "", keys: [["MOVE", "MOVE index", COL.ink]], on: ["MOVE"], range: 730 },
};
let YTAB = "yields";
const YSTATE = Object.fromEntries(Object.entries(YTABS).map(([k, v]) => [k, { on: [...v.on], range: v.range }]));

function renderYields() {
  if (!ok("us")) { $("#b-yields").innerHTML = failed("us"); return; }
  const U = D.us, A = U.anatomy || {};
  const rows = U.keys.map(k => `<tr><td><b>${esc(k.label)}</b></td><td><b>${yv(k.last)}</b></td>
    <td>${bpc(k.d1)}</td><td>${bpc(k.w1)}</td><td>${bpc(k.m1)}</td><td>${bpc(k.m3)}</td><td>${bpc(k.ytd)}</td><td>${bpc(k.y1)}</td><td>${bpc(k.y3)}</td>
    <td>${rbar(k)}</td><td>${ord(k.pct_2000)}${pbar(k.pct_2000)}</td>
    <td class="l">${k.high_since && k.range_pos >= 80 ? `highest since ${mon(k.high_since)}` : k.low_since && k.range_pos <= 20 ? `lowest since ${mon(k.low_since)}` : k.range_pos >= 80 && !k.high_since ? "highest on record" : ""}</td></tr>`).join("");
  const stack = (parts) => { const tot = parts.reduce((a, p) => a + Math.max(0, p[1] || 0), 0) || 1;
    return `<div class="stackbar">${parts.filter(p => p[1] != null).map(([n, v, c]) =>
    `<div style="width:${Math.max(0, v) / tot * 100}%;background:${c}" title="${esc(n)} ${yv(v)}">${v / tot > .18 ? yv(v) : ""}</div>`).join("")}</div>
    <div class="stackleg">${parts.filter(p => p[1] != null).map(([n, v, c]) => `<span><i class="sw" style="display:inline-block;width:8px;height:8px;background:${c};margin-right:.25rem"></i>${esc(n)} ${yv(v)}</span>`).join("")}</div>`; };
  const mv = U.move;
  $("#b-yields").innerHTML = `
    <div class="tabs" id="ytabs">${Object.entries(YTABS).map(([k, v]) => `<button class="tab ${k === YTAB ? "on" : ""}" data-k="${k}">${v.name}</button>`).join("")}</div>
    <div class="fbar" id="ybar"></div>
    <div class="lc" id="yChart"></div>
    <div class="src">Shaded: US recessions (NBER). History from FRED since 1990 (the 20-year was not issued 1987–93; the 30-year paused 2002–06). Long ranges are thinned to weekly.</div>
    <div class="tscroll" style="margin-top:.7rem"><table class="tbl"><thead><tr><th>Maturity</th><th>Yield</th><th>1D</th><th>1W</th><th>1M</th><th>3M</th><th>YTD</th><th>1Y</th><th>3Y</th><th>52 weeks</th><th>vs since 2000</th><th class="l"></th></tr></thead><tbody>${rows}</tbody></table></div>
    <div class="two even" style="margin-top:.8rem">
      <div><div class="grp" style="margin-top:0">What the 10-year is made of</div>
        <div class="prose">Split by <b>inflation</b>: the TIPS real yield plus the breakeven, the inflation the market prices.</div>
        ${stack([["real yield", A.real, "var(--s1)"], ["inflation compensation", A.breakeven, "var(--s2)"]])}
        ${A.term_premium != null ? `<div class="prose" style="margin-top:.6rem">Split by <b>expectations</b>: the average short rate expected over ten years, plus the term premium for locking money up (Kim-Wright model, ${dmy(A.tp_asof)}).</div>
        ${stack([["expected short rates", A.expected_path, "var(--s3)"], ["term premium", A.term_premium, "var(--s4)"]])}` : ""}
        <div class="src">Real yield ${dmy(A.real_asof)}; breakeven ${dmy(A.be_asof)}. 5y5y forward inflation: ${yv(A.fwd5y5y)}. Term premium ${pctl(A.tp_pct_1990)} since 1990, ${bp(A.tp_y1)} in a year.</div>
      </div>
      <div><div class="grp" style="margin-top:0">Bond volatility</div>
        ${mv ? `<div class="cards" style="grid-template-columns:1fr 1fr"><div class="card"><div class="k">MOVE index</div><div class="v">${mv.last.toFixed(1)}</div><div class="t">${pctl(mv.pct_1y)} of the past year; 1-year average ${mv.avg_1y}; ${mv.m1 >= 0 ? "+" : ""}${mv.m1} in a month</div></div>
          <div class="card"><div class="k">VIX, for scale</div><div class="v">${mv.vix ?? "–"}</div><div class="t">The MOVE is the VIX of Treasuries: the implied daily swing in yields, in basis points a month. Around 100 means roughly 6bp a day.</div></div></div>` : `<div class="dim">MOVE not available.</div>`}
      </div>
    </div>`;
  $$("#ytabs .tab").forEach(b => b.onclick = () => { YTAB = b.dataset.k; $$("#ytabs .tab").forEach(x => x.classList.toggle("on", x === b)); drawYields(); });
  drawYields();
}

async function drawYields() {
  const T = YTABS[YTAB], st = YSTATE[YTAB];
  $("#ybar").innerHTML = `${rangeBar("yRange", st.range)}<span class="sp"></span>${chips("yChips", T.keys.map(([k, n, c]) => ({ k, name: n, color: c })), st.on)}`;
  wireRange("yRange", r => { st.range = r; drawYields(); });
  wireChips("yChips", st.on, drawYields);
  const host = $("#yChart");
  if (!host.innerHTML) host.innerHTML = `<div class="lc-empty">Loading<span class="blink">_</span></div>`;
  const tab = YTAB;
  const H = await hist(T.keys.map(k => k[0]), startOf(st.range));
  if (tab !== YTAB) return;
  const unit = T.unit;
  const fmt = unit === "bp" ? (v => (v > 0 ? "+" : "") + Math.round(v) + "bp") : unit === "%" ? (v => v.toFixed(2) + "%") : (v => v.toFixed(0));
  lchart(host, {
    label: T.name, noLegend: true, endLabels: true, zero: T.zero, bands: (D.us || {}).recessions,
    yfmt: unit === "%" ? (v => v.toFixed(1) + "%") : fmt, tfmt: fmt, efmt: fmt,
    series: T.keys.filter(([k]) => st.on.includes(k)).map(([k, n, c, dash]) => ({ name: n, color: c, dash, w: c === COL.ink ? 2 : 1.4, pts: (H.series || {})[k] || [] })),
    empty: H.error || "No data for this range",
  });
}

/* ==========================================================================
   inflation
   ========================================================================== */
const ICH = [["CPI", COL.s2], ["Core PCE", COL.s1], ["Fed funds", COL.ink], ["10-year", COL.s3], ["Euro HICP", COL.s4], ["ECB deposit", COL.mid]];
const ION = ["CPI", "Core PCE", "Fed funds", "10-year"];
let IRANGE = "max";

function renderInflation() {
  if (!ok("inflation")) { $("#b-infl").innerHTML = failed("inflation"); return; }
  const I = D.inflation;
  const tbl = (rows, head) => `<table class="tbl"><thead><tr><th>${head}</th><th>Latest</th><th>Prior</th><th>A year ago</th><th class="l">Note</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${esc(r.name)} <span class="dim">${mon(r.asof)}</span></td><td><b class="${r.target != null && r.v > r.target + .5 ? "warnc" : ""}">${yv(r.v, 1)}</b></td><td>${yv(r.prev, 1)}</td><td>${yv(r.y1, 1)}</td>
      <td class="w">${esc(r.note)}${r.gap != null ? ` · ${pp(r.gap, 1)} vs target` : ""}</td></tr>`).join("")}</tbody></table>`;
  $("#b-infl").innerHTML = `
    <div class="two even">
      <div><div class="grp" style="margin-top:0">United States</div><div class="tscroll">${tbl(I.us, "Measure")}</div>
        <div class="grp">Euro area</div><div class="tscroll">${tbl(I.ea, "Measure")}</div></div>
      <div><div class="grp" style="margin-top:0">What the bond market prices</div>
        <div class="tscroll"><table class="tbl"><thead><tr><th>Market measure</th><th>Now</th><th>1M</th><th>1Y</th><th>vs since 2003</th></tr></thead><tbody>
        ${I.market.map(m => `<tr><td>${esc(m.name)}<br><span class="dim" style="font-size:.62rem;white-space:normal">${esc(m.note)}</span></td><td><b>${yv(m.last)}</b></td><td>${bpc(m.m1)}</td><td>${bpc(m.y1)}</td><td>${ord(m.pct_2003)}${pbar(m.pct_2003)}</td></tr>`).join("")}
        </tbody></table></div>
        <div class="grp">Real policy rates</div>
        <div class="cards">${I.real_policy.map(r => `<div class="card"><div class="k">${esc(r.name)}</div><div class="v ${r.v > 1 ? "warnc" : ""}">${r.v > 0 ? "+" : ""}${r.v.toFixed(2)}%</div>
          <div class="t">${yv(r.rate)} policy rate against ${yv(r.infl, 1)} core inflation (${mon(r.asof)}). ${r.v > 1 ? "Restrictive: squeezing demand." : r.v > -0.5 ? "Around neutral." : "Negative: still stimulating."}</div></div>`).join("")}</div>
      </div>
    </div>
    <div class="grp">Inflation, policy and the 10-year since 2000</div>
    <div class="fbar">${rangeBar("iRange", IRANGE, [["5Y", 1826], ["10Y", 3652], ["Since 2000", "max"]])}<span class="sp"></span>${chips("iChips", ICH.map(([k, c]) => ({ k, name: k, color: c })), ION)}</div>
    <div class="lc" id="iChart"></div>
    <div class="src">Monthly. Year-on-year CPI and PCE from the BLS and BEA, euro HICP from Eurostat (via FRED); rates are month-end. The dashed line is the 2% target both central banks aim at.</div>`;
  wireRange("iRange", r => { IRANGE = r; drawInfl(); });
  wireChips("iChips", ION, drawInfl);
  drawInfl();
}

function drawInfl() {
  const I = D.inflation, st = startOf(IRANGE, "2000-01-01");
  lchart($("#iChart"), {
    label: "Inflation and rates", noLegend: true, endLabels: true, zero: true, bands: I.recessions, refs: [{ y: 2, label: "2% target" }],
    yfmt: v => v.toFixed(0) + "%", tfmt: v => v.toFixed(2) + "%", efmt: v => v.toFixed(1) + "%",
    series: ICH.filter(([k]) => ION.includes(k)).map(([k, c]) => ({ name: k, color: c, w: c === COL.ink ? 2 : 1.4, pts: (I.chart[k] || []).filter(p => p[0] >= st) })),
  });
}

/* ==========================================================================
   central banks
   ========================================================================== */
let CBRANGE = 9131;
function renderCB() {
  if (!ok("us")) { $("#b-cb").innerHTML = failed("us"); return; }
  const F = D.us.fed || {}, E = ok("europe") ? D.europe.ecb || {} : null;
  const G = ok("global") ? Object.fromEntries(D.global.rows.map(r => [r.c, r])) : {};
  const fw = F.fedwatch;
  const nm = m => m ? `${dmy(m.date)} · in ${m.days} days` : "–";
  const read = (g, who) => g == null ? "" : g <= -20 ? `The 2-year sits ${Math.abs(g)}bp below the policy rate: the market expects ${who} to cut.`
    : g >= 20 ? `The 2-year sits ${g}bp above the policy rate: the market leans towards ${who} hiking.` : `The 2-year is within ${Math.abs(g)}bp of the policy rate: no big move priced.`;
  $("#b-cb").innerHTML = `
    <div class="cards">
      <div class="card"><div class="k">Federal Reserve</div><div class="v">${F.lower != null ? `${F.lower.toFixed(2)}–${F.upper.toFixed(2)}%` : "–"}</div>
        <div class="t">Effective ${yv(F.effr)} (${dm(F.effr_asof)}) · SOFR ${yv(F.sofr)}<br>Next decision ${nm(F.next_meeting)}${fw ? `<br>Futures price: cut ${fw.cut}% · hold ${fw.hold}% · hike ${fw.hike}%${fw.age_h != null && fw.age_h >= 2 ? ` <span class="dim">(read ${fw.age_h}h ago)</span>` : ""} · <a href="#s-path">every meeting ›</a>` : ""}<br>${esc(read(F.two_minus_effr_bp, "the Fed"))}</div></div>
      <div class="card"><div class="k">European Central Bank</div><div class="v">${E && E.dfr != null ? yv(E.dfr) + " deposit" : "–"}</div>
        <div class="t">${E ? `€STR ${yv(E.estr, 3)} (${dm(E.estr_asof)})${E.last_change ? `<br>Last change ${bp(E.last_change.bp)} on ${dmy(E.last_change.d)}` : ""}<br>Next decision ${nm(E.next_meeting)}<br>${esc(read(E.two_minus_dfr_bp, "the ECB"))}` : failed("europe")}</div></div>
      <div class="card"><div class="k">Bank of England</div><div class="v">${G.GB && G.GB.policy != null ? yv(G.GB.policy) : "–"}</div>
        <div class="t">Bank Rate${G.GB ? ` (${dm(G.GB.policy_asof)}) · 10-year gilt ${yv(G.GB.ten)}` : ""}</div></div>
      <div class="card"><div class="k">Bank of Japan</div><div class="v">${G.JP && G.JP.policy != null ? yv(G.JP.policy) : "–"}</div>
        <div class="t">Overnight call rate${G.JP ? ` (${mon(G.JP.policy_asof)}) · 10-year JGB ${yv(G.JP.ten)}` : ""}</div></div>
    </div>
    <div class="fbar" style="margin-top:.7rem">${rangeBar("cbRange", CBRANGE, [["5Y", 1826], ["10Y", 3652], ["25Y", 9131]])}</div>
    <div class="lc" id="cbChart"></div>
    <div class="src">Fed: target range and effective rate (FRED). ECB: deposit facility rate and €STR (ECB Data Portal). Bank Rate: Bank of England. The futures odds here are for the next meeting; the panel below chains every contract into the whole path.</div>`;
  wireRange("cbRange", r => { CBRANGE = r; drawCB(); });
  drawCB();
}

async function drawCB() {
  const H = await hist(["DFF", "ECB_DFR", "BOE_RATE", "DGS2"], startOf(CBRANGE));
  lchart($("#cbChart"), {
    label: "Policy rates", endLabels: true, zero: true, bands: (D.us || {}).recessions,
    yfmt: v => v.toFixed(1) + "%", tfmt: v => v.toFixed(2) + "%",
    series: [["DFF", "Fed funds", COL.ink], ["ECB_DFR", "ECB deposit", COL.s1], ["BOE_RATE", "BoE Bank Rate", COL.s2], ["DGS2", "US 2-year", COL.s3, "4 3"]]
      .map(([k, n, c, d]) => ({ name: n, color: c, dash: d, w: c === COL.ink ? 2 : 1.4, pts: (H.series || {})[k] || [] })),
  });
}

/* ==========================================================================
   the rate path
   ========================================================================== */
const PATH_COL = { "Today": COL.ink, "1 week ago": COL.s4, "1 month ago": COL.s1, "3 months ago": COL.s3, "Fed's own projection": COL.s2 };
let PATH_ON = ["Today", "1 month ago", "Fed's own projection"];
const longDay = d => new Date(d + "T00:00:00Z").toLocaleString("en-GB", { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
const monYear = d => new Date(d + "T00:00:00Z").toLocaleString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
const moves = bpv => { const n = Math.abs(bpv) / 25; return `${n.toFixed(1)} quarter-point ${bpv > 0 ? "hike" : "cut"}${n.toFixed(1) === "1.0" ? "" : "s"}`; };

function oddsCell(m) {
  const lead = m.hike >= m.cut ? ["hike", m.hike, "warn"] : ["cut", m.cut, "good"];
  if (lead[1] < 5) return `<span class="dim">hold</span>`;
  return `<span class="pbar ${lead[1] >= 50 ? "hi" : ""}" style="margin:0 .35rem 0 0"><i style="width:${Math.min(100, lead[1])}%"></i></span>${lead[1].toFixed(0)}% ${lead[0]}${m.second ? ` <span class="dim">+${m.second.toFixed(0)}% a second</span>` : ""}`;
}

function renderPath() {
  if (!ok("path")) { $("#b-path").innerHTML = failed("path"); return; }
  const P = D.path, F = P.fed, E = P.ecb;
  let fed = `<div class="note"><b>No fed funds futures</b>${esc(P.fed_error || "Yahoo returned none")}</div>`;
  if (F && F.meetings.length) {
    const end = F.meetings[F.meetings.length - 1], first = F.meetings.find(m => Math.abs(m.cum_bp) >= 25);
    const series = [{ k: "Today" }, ...F.past.map(p => ({ k: p.label })), ...(F.sep.length ? [{ k: "Fed's own projection" }] : [])]
      .map(s => ({ ...s, name: s.k, color: PATH_COL[s.k] }));
    fed = `
      <p class="prose" style="margin:0 0 .5rem">The Fed's rate is <b>${yv(F.now.rate)}</b>${F.now.lower != null ? ` (target ${F.now.lower.toFixed(2)}–${F.now.upper.toFixed(2)}%)` : ""}.
        Futures price <b>${yv(end.after)}</b> after the ${esc(longDay(end.date))} meeting, <b>${bp(end.cum_bp)}</b> from today${Math.abs(end.cum_bp) >= 12 ? `: about ${moves(end.cum_bp)}` : ""}.
        ${first ? `The first full quarter point is priced by ${esc(longDay(first.date))}.` : "No full quarter point is priced at any meeting."}</p>
      <div class="fbar">${chips("pathChips", series, PATH_ON)}</div>
      <div class="lc" id="pathChart"></div>
      <table class="tbl" style="margin-top:.6rem"><thead><tr><th>Meeting</th><th>In</th><th>Priced after</th><th>At this meeting</th>
        <th class="l">Odds</th><th>From today</th><th>Moves of 25bp</th><th class="l">Read from</th></tr></thead><tbody>
        ${F.meetings.map(m => `<tr><td>${dmy(m.date)}</td><td class="dim">${m.days}d</td><td><b>${yv(m.after)}</b></td><td>${bpc(Math.round(m.change_bp))}</td>
          <td class="l">${oddsCell(m)}</td><td>${bpc(Math.round(m.cum_bp))}</td><td>${m.moves > 0 ? "+" : ""}${m.moves.toFixed(1)}</td>
          <td class="l dim" title="${m.how === "next" ? "the month after the meeting has no meeting, so its contract is the rate after it" : "backed out of the meeting month's own contract"}">${esc(m.contract.replace(".CBT", ""))}${m.how === "thin" ? " · few days left" : ""}</td></tr>`).join("")}
      </tbody></table>
      <div class="src">30-day fed funds futures, one per month (Yahoo, ${dmy(F.asof)}). Each settles at the month's average effective rate, so a meeting month prices two rates weighted by the days either side of the decision; where the month after has no meeting, that month's contract is read instead. Today's rate: ${esc(F.now.src)}.
        Odds are one step at a time: 9bp priced is a 36% chance of 25bp. The Fed's own projection is the median of its members' year-end forecasts (FRED).${F.unpriced.length ? ` Not priced: ${F.unpriced.map(dmy).join(", ")}.` : ""} Meeting dates checked ${esc(F.meetings_as_of)}.</div>`;
  }
  let ecb = `<div class="note"><b>No euro futures</b>${esc(P.ecb_error || "Yahoo returned none")}</div>`;
  if (E && E.quarters.length) {
    ecb = `
      <p class="prose" style="margin:0 0 .5rem">The ECB's deposit rate is <b>${yv(E.dfr)}</b>. Its futures cover three months each, so this is by quarter, with the meetings that fall inside.</p>
      <table class="tbl"><thead><tr><th class="l">Quarter</th><th class="l">Meetings inside</th><th>Priced rate</th><th>From today</th><th>Moves of 25bp</th></tr></thead><tbody>
        ${E.quarters.map(q => `<tr><td class="l">${q.running ? "now" : dm(q.from)} – ${dmy(q.to)}</td>
          <td class="l dim">${q.meetings.length ? q.meetings.map(dm).join(", ") : "–"}</td><td><b>${yv(q.rate)}</b></td><td>${bpc(Math.round(q.cum_bp))}</td>
          <td title="${esc(q.contract)}, last price ${dmy(q.price_date)}">${q.moves > 0 ? "+" : ""}${q.moves.toFixed(1)}</td></tr>`).join("")}
      </tbody></table>
      <div class="src">Three-month euro short-term rate futures (CME, via Yahoo), each settling on the overnight rate between two third Wednesdays. The deposit rate is taken as that rate plus today's gap to it (${Math.round((E.dfr - E.estr) * 100)}bp). Yahoo keeps only each contract's last price; the oldest here is from ${dmy(E.oldest_price)}.</div>`;
  }
  $("#b-path").innerHTML = `<div class="two"><div><div class="grp" style="margin-top:0">Federal Reserve · meeting by meeting</div>${fed}</div>
    <div><div class="grp" style="margin-top:0">European Central Bank · by quarter</div>${ecb}</div></div>`;
  if (F && F.meetings.length) { wireChips("pathChips", PATH_ON, drawPath); drawPath(); }
}

function drawPath() {
  const F = D.path.fed, today = new Date().toISOString().slice(0, 10);
  const day = (d, n) => new Date(MS(d) + n * 864e5).toISOString().slice(0, 10);
  const last = F.meetings[F.meetings.length - 1].date;
  const steps = (r0, pts) => {
    const out = [[today, r0]];
    let cur = r0;
    for (const p of pts) { out.push([p.date, cur], [day(p.date, 1), p.after]); cur = p.after; }
    out.push([day(last, 30), cur]);
    return out;
  };
  const S = [{ name: "Today", color: COL.ink, w: 2.2, pts: steps(F.now.rate, F.meetings) },
    ...F.past.map(p => ({ name: p.label, color: PATH_COL[p.label], w: 1.3, pts: steps(p.rate, p.points) }))];
  const sep = F.sep.map(s => [`${s.year}-12-31`, s.rate]).filter(p => p[0] <= day(last, 30));
  if (sep.length) S.push({ name: "Fed's own projection", color: COL.s2, w: 1.3, dash: "2 4",
    pts: [[today, F.now.rate], ...sep] });
  lchart($("#pathChart"), {
    label: "The fed funds rate priced after each meeting", endLabels: true, noLegend: true, height: 250,
    yfmt: v => v.toFixed(2) + "%", tfmt: v => v.toFixed(2) + "%",
    series: S.map(s => ({ ...s, hidden: !PATH_ON.includes(s.name) })),
  });
}

/* ==========================================================================
   stocks and rates
   ========================================================================== */
let SENS_MODE = "ex";
let HSORT = "ex";
function renderStocks() {
  if (!ok("stocks")) { $("#b-stocks").innerHTML = failed("stocks"); return; }
  const S = D.stocks, E = S.erp || {};
  const eras = Object.entries(S.corr_eras || {});
  $("#b-stocks").innerHTML = `
    <div class="two">
      <div><div class="grp" style="margin-top:0">Do bonds still hedge stocks?</div>
        <div class="lc" id="corrChart"></div>
        <div class="src">One-year rolling correlation of daily S&amp;P 500 returns with daily Treasury returns (minus the change in the 10-year yield). Below zero, bonds rise when stocks fall — the hedge that made 60/40 work. Above zero, they fall together.</div></div>
      <div><div class="cards" style="grid-template-columns:1fr">
        <div class="card"><div class="k">Stock–bond correlation, past year</div><div class="v ${S.corr_now > .15 ? "warnc" : S.corr_now < -.15 ? "pos" : ""}">${S.corr_now != null ? (S.corr_now > 0 ? "+" : "") + S.corr_now.toFixed(2) : "–"}</div>
          <div class="t">${eras.map(([k, v]) => `${esc(k)}: <b>${v > 0 ? "+" : ""}${v}</b>`).join(" · ")}<br>
          ${S.corr_now > .15 ? "Positive: bonds have fallen WITH stocks. This happens when inflation — not recession — is what the market fears, as in 2022." : S.corr_now < -.15 ? "Negative: bonds have been a hedge." : "Near zero: diversifying, not hedging."}</div></div>
        ${(E.rows || []).map(r => `<div class="card"><div class="k">${esc(r.name)} earnings yield vs the 10-year</div>
          <div class="v ${r.gap_nominal < 1 ? "warnc" : ""}">${pp(r.gap_nominal)}</div>
          <div class="t">P/E ${r.pe ?? "–"}${r.fpe ? ` (forward ${r.fpe})` : ""} → earnings yield ${yv(r.ey_fwd ?? r.ey)} against a ${yv(E.ten)} 10-year; ${pp(r.gap_real)} against the ${yv(E.real)} real yield. The thinner this gap, the less stocks pay for their extra risk.</div></div>`).join("")}
      </div></div>
    </div>
    <div class="two even" style="margin-top:.8rem">
      <div><div class="grp" style="margin-top:0">Every day of the past year</div>
        <div class="lc" id="scatChart"></div>
        <div class="src">Each dot is one session: the change in the 10-year (bp, across) against the S&amp;P 500 (%, up). The line is the average relationship.</div></div>
      <div><div class="grp" style="margin-top:0">When the 10-year moves, stocks…</div>
        <table class="tbl"><thead><tr><th>Two years of sessions</th><th>Days</th><th>S&amp;P 500</th><th>Your book</th></tr></thead><tbody>
        ${(S.conditional || []).map(c => `<tr><td>${esc(c.label)}</td><td>${c.n}</td><td>${pcc(c.spx, 2)}</td><td>${pcc(c.you, 2)}</td></tr>`).join("")}</tbody></table>
        <div class="src">Average same-day return. Your book = today's holdings, in euros.</div></div>
    </div>
    <div class="grp">Who is rate-sensitive</div>
    <div class="fbar"><span class="grp" id="sensMode"><button class="seg ${SENS_MODE === "ex" ? "on" : ""}" data-m="ex">Beyond the market</button><button class="seg ${SENS_MODE === "raw" ? "on" : ""}" data-m="raw">Raw</button></span>
      <span class="dim" style="font-size:.64rem">% move for every +10bp in the US 10-year, daily, past year. "Beyond the market" removes what the S&amp;P's own move explains.</span></div>
    <div class="two even"><div id="sensBars"></div><div id="holdTbl"></div></div>
    <div class="grp">If the 10-year moved tomorrow</div>
    <div id="rateSim"></div>
    <div class="grp">Rate-shock days of the past year</div>
    <div class="tscroll"><table class="tbl"><thead><tr><th>Day</th><th>10-year</th><th>2-year</th><th>S&amp;P 500</th><th>Your book</th><th class="l"></th></tr></thead><tbody>
      ${(S.shocks || []).map(x => `<tr><td>${dmy(x.d)}</td><td>${bpc(x.d10)}</td><td>${bpc(x.d2)}</td><td>${pcc(x.spx, 2)}</td><td>${pcc(x.you, 2)}</td><td class="l">${x.fomc ? `<span class="bdg info">FOMC</span>` : ""}</td></tr>`).join("")}</tbody></table></div>
    ${S.fit_error ? `<div class="note">Your book's side of this panel failed: ${esc(S.fit_error)}</div>` : ""}`;
  lchart($("#corrChart"), {
    label: "Rolling stock-bond correlation", zero: true, height: 230, bands: (D.us || {}).recessions,
    yfmt: v => v.toFixed(1), tfmt: v => (v > 0 ? "+" : "") + v.toFixed(2),
    series: [{ name: "1-year correlation", color: COL.ink, w: 1.6, pts: S.rolling_corr || [] }],
  });
  scatter($("#scatChart"), S.scatter || [], { xl: "10-year change, bp", yl: "S&P 500, %", xu: "", yu: "%", height: 230 });
  $$("#sensMode .seg").forEach(b => b.onclick = () => { SENS_MODE = b.dataset.m; $$("#sensMode .seg").forEach(x => x.classList.toggle("on", x === b)); drawSens(); });
  drawSens();
  drawRateSim();
}

/* One slider, every sensitivity on the page applied to it: your book (its
   measured beta to the 10-year, in euros), the S&P 500, and each bond fund
   (minus duration times the move). A same-day shock, not a forecast. */
let SIM_BP = 25;
function drawRateSim() {
  const box = $("#rateSim");
  if (!box || !ok("stocks")) return;
  const S = D.stocks, you = S.you, spy = (S.sectors || []).find(x => x.sym === "SPY");
  const val = ok("portfolio") && D.portfolio.available ? D.portfolio.book.value : null;
  const funds = ok("portfolio") && D.portfolio.available ? D.portfolio.candidates.filter(c => c.available && c.dur) : [];
  const k = SIM_BP / 10;
  const yb = you ? you.per10bp * k : null;
  const hold = (S.holdings || []).map(h => ({ ...h, eur: val != null ? val * h.w / 100 * h.per10bp * k / 100 : null }))
    .sort((a, b) => (a.eur ?? 0) - (b.eur ?? 0));
  // The slider is drawn once; only the figures under it are redrawn, so a
  // drag is never interrupted by its own element being replaced.
  if (!$("#simSlider")) {
    box.innerHTML = `<div class="wi"><span>10-year yield</span><input type="range" id="simSlider" min="-100" max="100" step="5" value="${SIM_BP}"><b id="simVal"></b>
      <span class="dim" style="font-size:.64rem">in one session</span></div><div id="simOut"></div>`;
    const sl = $("#simSlider");
    sl.oninput = () => { SIM_BP = +sl.value; drawRateSim(); };
  }
  $("#simVal").className = sgn(SIM_BP); $("#simVal").textContent = bp(SIM_BP);
  $("#simOut").innerHTML = `
    <div class="cards">
      <div class="card"><div class="k">Your book</div><div class="v ${sgn(yb)}">${yb != null ? pc(yb, 2) : "–"}</div>
        <div class="t">${yb != null && val != null ? `≈ ${eur(val * yb / 100)} on ${eur(val)}. ` : ""}From its average reaction over the past year (R² ${you ? (you.r2 * 100).toFixed(0) : "–"}% — rates explain only part of any day).</div></div>
      <div class="card"><div class="k">S&amp;P 500</div><div class="v ${sgn(spy ? spy.per10bp * k : null)}">${spy ? pc(spy.per10bp * k, 2) : "–"}</div>
        <div class="t">Same measure, same year, for the index.</div></div>
      <div class="card"><div class="k">Largest moves in your book</div><div class="t">${hold.slice(0, SIM_BP >= 0 ? 4 : 0).concat(SIM_BP < 0 ? hold.slice(-4).reverse() : []).map(h =>
        `${esc(h.t)} <span class="${sgn(h.eur)}">${h.eur != null ? eur(h.eur) : pc(h.per10bp * k, 2)}</span>`).join(" · ") || "–"}</div></div>
    </div>
    ${funds.length ? `<div class="tscroll" style="margin-top:.4rem"><table class="tbl"><thead><tr><th>Bond fund, if its own yields moved as much</th><th>Duration</th><th>Price move</th><th class="l">…and a year of its yield</th></tr></thead><tbody>
      ${funds.map(c => { const mv = -c.dur * SIM_BP / 100; return `<tr><td>${esc(c.name)} <span class="dim">${esc(c.sym)}</span></td><td>${c.dur}</td><td class="${sgn(mv)}">${pc(mv, 2)}</td>
        <td class="l dim">${c.yield != null ? `${yv(c.yield)} a year of carry` : ""}</td></tr>`; }).join("")}</tbody></table></div>` : ""}
    <div class="src">Stocks: the average same-day response over the past year, which is noisy — and big falls in yields often come on risk-off days when stocks fall too (see the table above). Bond funds: minus duration × the move — the arithmetic of a bond price, far more reliable. Unhedged dollar funds would also move with EUR/USD.</div>`;
}

function drawSens() {
  const S = D.stocks;
  const val = x => SENS_MODE === "ex" ? (x.per10bp_ex_mkt ?? x.per10bp) : x.per10bp;
  const rows = (S.sectors || []).filter(x => SENS_MODE === "raw" || x.per10bp_ex_mkt != null);
  if (S.you) rows.push({ sym: "YOU", name: "Your book", ...S.you, you: true });
  rows.sort((a, b) => val(a) - val(b));
  const mx = Math.max(0.5, ...rows.map(r => Math.abs(val(r))));
  const bar = (v) => { const w = Math.min(50, Math.abs(v) / mx * 50);
    return `<div class="tr"><div class="mid"></div><i class="${v < 0 ? "neg" : "pos"}" style="${v < 0 ? `right:50%;width:${w}%` : `left:50%;width:${w}%`}"></i></div>`; };
  $("#sensBars").innerHTML = rows.map(r => `<div class="hb ${r.you ? "you" : ""}" title="raw ${pc(r.per10bp, 2)} · beyond market ${pc(r.per10bp_ex_mkt, 2)} · R² ${(r.r2 * 100).toFixed(0)}%">
      <span class="l"><b>${esc(r.sym)}</b> ${esc(r.name)}</span>${bar(val(r))}<span class="v ${sgn(val(r))}">${pc(val(r), 2)}</span></div>`).join("");
  const H = [...(S.holdings || [])];
  const hv = x => HSORT === "w" ? -x.w : HSORT === "raw" ? x.per10bp : (x.per10bp_ex_mkt ?? x.per10bp);
  H.sort((a, b) => hv(a) - hv(b));
  $("#holdTbl").innerHTML = H.length ? `<div class="tscroll" style="max-height:430px;overflow-y:auto"><table class="tbl"><thead><tr>
      <th>Your holding</th><th class="sort" data-s="w">Weight</th><th class="sort" data-s="raw">Raw</th><th class="sort" data-s="ex">Beyond mkt</th><th>Mkt β</th><th>R²</th></tr></thead><tbody>
      ${H.map(h => `<tr><td><a href="/t/${encodeURIComponent(h.t)}">${esc(h.t)}</a> <span class="dim">${esc((h.sector || "").slice(0, 16))}</span></td><td>${h.w.toFixed(1)}%</td>
        <td class="${sgn(h.per10bp)}">${pc(h.per10bp, 2)}</td><td class="${sgn(h.per10bp_ex_mkt)}"><b>${pc(h.per10bp_ex_mkt, 2)}</b></td><td>${h.beta_mkt ?? "–"}</td><td>${(h.r2 * 100).toFixed(0)}%</td></tr>`).join("")}
      </tbody></table></div><div class="src">In each holding's own currency, so this is the rate effect and not the dollar. Click a header to sort.</div>`
    : `<div class="dim">No holdings with a year of history.</div>`;
  $$("#holdTbl th.sort").forEach(th => th.onclick = () => { HSORT = th.dataset.s; drawSens(); });
}

/* ==========================================================================
   your portfolio: the what-if
   ========================================================================== */
let PICK = "IEGA.AS", WEIGHT = 20;

function renderBook() {
  if (!ok("portfolio")) { $("#b-book").innerHTML = failed("portfolio"); return; }
  const P = D.portfolio;
  if (!P.available) { $("#b-book").innerHTML = `<div class="note plain">${esc(P.why || "Not available.")}</div>`; return; }
  const B = P.book, bs = B.stats || {};
  if (!P.candidates.find(c => c.sym === PICK && c.available)) PICK = (P.candidates.find(c => c.available && c.sym !== "XEON.DE") || {}).sym;
  const at = (c, w) => (c.blends || []).find(b => Math.abs(b.w - w) < 1e-9) || {};
  let lastGroup = "";
  const rows = P.candidates.map(c => {
    const head = c.group !== lastGroup ? `<tr class="sec"><td colspan="14">${esc(c.group)}</td></tr>` : "";
    lastGroup = c.group;
    if (!c.available) return head + `<tr><td>${esc(c.name)} <span class="dim">${esc(c.sym)}</span></td><td colspan="13" class="l dim">${esc(c.why || "no data")}</td></tr>`;
    const b20 = at(c, 0.2);
    return head + `<tr class="pick ${c.sym === PICK ? "sel" : ""}" data-sym="${esc(c.sym)}" title="${esc(c.note || "")}">
      <td>${esc(c.name)} <span class="dim">${esc(c.sym)}</span></td><td>${c.ter.toFixed(2)}%</td><td><b>${yv(c.yield)}</b></td><td>${c.dur ? c.dur.toFixed(1) : "0"}</td>
      <td>${c.cushion_bp != null && c.dur ? c.cushion_bp + "bp" : "–"}</td>
      <td class="${c.corr > .3 ? "warnc" : ""}">${c.corr != null ? c.corr.toFixed(2) : "–"}</td><td>${c.corr_1y != null ? c.corr_1y.toFixed(2) : "–"}</td>
      <td class="${sgn(c.on_worst)}">${pc(c.on_worst, 2)}</td><td class="${sgn(c.y2022)}">${pc(c.y2022, 1)}</td>
      <td>${c.fund.vol != null ? c.fund.vol.toFixed(1) + "%" : "–"}</td><td class="neg">${c.fund.maxdd != null ? c.fund.maxdd.toFixed(1) + "%" : "–"}</td>
      <td class="${b20.vol < c.book.vol ? "pos" : "neg"}">${b20.vol != null ? pp(b20.vol - c.book.vol, 1) : "–"}</td>
      <td class="${b20.maxdd > c.book.maxdd ? "pos" : "neg"}">${b20.maxdd != null ? pp(b20.maxdd - c.book.maxdd, 1) : "–"}</td>
      <td class="${sgn(b20.ann - c.book.ann)}">${b20.ann != null ? pp(b20.ann - c.book.ann, 1) : "–"}</td></tr>`;
  }).join("");
  const hc = P.hedge;
  $("#b-book").innerHTML = `
    <div class="prose">Your book today: <b>${B.n} holdings</b>, ${eur(B.value)}. Held at today's weights since ${mon(B.from)}, it returned <b>${pc(bs.ann)}</b> a year with <b>${bs.vol}%</b> volatility and a worst drawdown of <b>${bs.maxdd}%</b>. Below: what adding a bond fund would have done over the same days, and what each fund yields now.</div>
    <div class="note plain" style="font-size:.7rem">History, not a forecast and not advice. Today's holdings are used for the whole period, which flatters the past (you picked them knowing how they did), and the funds' yields today differ from what they paid in these years. Yields are estimates from the curve each fund lives on, after costs — check the factsheet (KID) before buying anything.</div>
    <div class="tscroll"><table class="tbl" id="candTbl"><thead><tr><th>Fund</th><th title="Ongoing charges">TER</th><th title="Estimated yield to maturity after costs">Yield ≈</th><th title="Typical duration of the index: % price change per 1pp move in yields">Duration</th>
      <th title="How far yields could rise over a year before the yield is used up">Cushion</th><th title="Correlation of five-day returns with your book, whole period. Five days, because your shares close in New York and these funds in Frankfurt">Corr</th><th>Corr 1Y</th>
      <th title="Average return on your book's worst 5% of days">On your worst days</th><th>2022</th><th>Vol</th><th>Max DD</th><th title="Change in your volatility with 20% in this fund">+20%: vol</th><th title="Change in your worst drawdown with 20% in this fund (positive = shallower)">+20%: DD</th><th title="Change in your annual return with 20% in this fund">+20%: return</th></tr></thead>
      <tbody>${rows}</tbody></table></div>
    <div class="src">Click a fund to see it blended with your book. Returns are total returns (dividends reinvested) in euros; funds quoted in dollars are converted. "Hedged" funds strip out the dollar; unhedged ones carry EUR/USD as well as the bonds.</div>
    <div id="pickBox"></div>
    <div class="two even" style="margin-top:.8rem">
      <div><div class="grp" style="margin-top:0">What a euro investor earns on government bonds</div>
        <table class="tbl"><thead><tr><th>Maturity</th><th>US Treasury</th><th>…hedged to €</th><th>German Bund</th><th>Euro area avg</th><th>Hedged pickup</th></tr></thead><tbody>
        ${(P.curves || []).map(r => `<tr><td>${r.yrs}-year</td><td>${yv(r.ust)}</td><td><b>${yv(r.ust_hedged)}</b></td><td>${yv(r.bund)}</td><td>${yv(r.ea_all)}</td><td>${bpc(r.pickup_hedged_bp)}</td></tr>`).join("")}</tbody></table>
        <div class="src">${hc ? `Hedging dollars to euros costs about the gap between the two 3-month rates: ${yv(hc.us3m)} − ${yv(hc.ea3m)} = <b>${hc.cost.toFixed(2)}pp</b> a year (covered interest parity; the small cross-currency basis is ignored). ` : ""}Unhedged, a Treasury pays its full yield but moves with EUR/USD — a 10% fall in the dollar wipes out two years of coupons.</div></div>
      <div><div class="grp" style="margin-top:0">Cash, for comparison</div>
        <div class="cards"><div class="card"><div class="k">€STR (overnight)</div><div class="v">${yv(P.cash.estr, 2)}</div><div class="t">What a money-market fund like XEON earns, before costs, with no duration risk (${dm(P.cash.estr_asof)}).</div></div>
          <div class="card"><div class="k">ECB deposit rate</div><div class="v">${yv(P.cash.dfr)}</div><div class="t">The floor under euro short rates. When the ECB cuts, cash yields fall at once; bond yields you locked in do not.</div></div></div>
        <div class="prose" style="margin-top:.5rem">The trade-off in one line: <b>cash</b> pays the overnight rate and cannot fall in price; <b>bonds</b> lock in today's yield for years and gain when rates fall — and lose when they rise, as in 2022.</div></div>
    </div>`;
  $$("#candTbl tr.pick").forEach(tr => tr.onclick = () => { PICK = tr.dataset.sym; $$("#candTbl tr.pick").forEach(x => x.classList.toggle("sel", x === tr)); drawPick(); });
  drawPick();
}

function drawPick() {
  const P = D.portfolio, c = P.candidates.find(x => x.sym === PICK);
  const box = $("#pickBox");
  if (!c || !c.available) { box.innerHTML = ""; return; }
  box.innerHTML = `<div class="grp">${esc(c.name)} <span class="dim">${esc(c.sym)}</span> blended with your book</div>
    <div class="prose">${esc(c.note || "")}${c.note ? " · " : ""}${c.n} shared trading days since ${dmy(c.from)}.</div>
    <div class="two">
      <div><div class="wi"><span>Weight in the fund</span><input type="range" id="wSlider" min="0" max="60" step="5" value="${WEIGHT}"><b id="wVal">${WEIGHT}%</b></div>
        <div class="lc" id="pickChart"></div><div class="src" id="pickStats"></div></div>
      <div><table class="tbl"><thead><tr><th>In the fund</th><th>Return /yr</th><th>Volatility</th><th>Worst drawdown</th></tr></thead><tbody>
        <tr><td>0% (your book)</td><td>${pc(c.book.ann)}</td><td>${c.book.vol}%</td><td class="neg">${c.book.maxdd}%</td></tr>
        ${c.blends.map(b => `<tr><td>${Math.round(b.w * 100)}%</td><td>${pc(b.ann)}</td><td>${b.vol}%</td><td class="neg">${b.maxdd}%</td></tr>`).join("")}
        <tr><td>100% (fund alone)</td><td>${pc(c.fund.ann)}</td><td>${c.fund.vol}%</td><td class="neg">${c.fund.maxdd}%</td></tr></tbody></table>
        <div class="src">Daily, rebalanced to the weight. Fund in 2022: ${pc(c.y2022)}.</div>
        ${c.scen.length ? `<div class="grp">The next 12 months if yields move once and stay</div>
        <div class="scen">${c.scen.map(s => `<div><span>${s.shift > 0 ? "+" : ""}${s.shift}bp</span><b class="${sgn(s.ret)}">${pc(s.ret, 1)}</b></div>`).join("")}</div>
        <div class="src">Yield ≈ ${yv(c.yield)} minus duration ${c.dur} × the move, with a convexity term — approximate. The fund keeps its yield only if yields rise less than ~${c.cushion_bp}bp.</div>` : ""}
      </div></div>`;
  wrapTables();
  const s = $("#wSlider");
  s.oninput = () => { WEIGHT = +s.value; $("#wVal").textContent = WEIGHT + "%"; drawBlend(c); };
  drawBlend(c);
}

function drawBlend(c) {
  const Wk = D.portfolio.weekly[c.sym];
  if (!Wk) return;
  const w = WEIGHT / 100, bk = Wk.book, fd = Wk.fund, out = [[bk[0][0], 1]], rets = [];
  let v = 1;
  for (let i = 1; i < bk.length; i++) {
    const r = (1 - w) * (bk[i][1] / bk[i - 1][1] - 1) + w * (fd[i][1] / fd[i - 1][1] - 1);
    v *= 1 + r; rets.push(r); out.push([bk[i][0], v]);
  }
  const toPct = a => a.map(p => [p[0], (p[1] / a[0][1] - 1) * 100]);
  lchart($("#pickChart"), {
    label: "Growth", endLabels: true, zero: true, height: 250,
    yfmt: v => (v > 0 ? "+" : "") + v.toFixed(0) + "%", tfmt: v => (v > 0 ? "+" : "") + v.toFixed(1) + "%",
    series: [{ name: "Your book", color: COL.ink, w: 2, pts: toPct(bk) },
      { name: `Book + ${WEIGHT}% fund`, color: COL.s1, w: 1.8, pts: toPct(out) },
      { name: "Fund alone", color: COL.s3, w: 1.2, dash: "4 3", pts: toPct(fd) }],
  });
  let peak = -1e9, dd = 0; for (const p of out) { peak = Math.max(peak, p[1]); dd = Math.min(dd, p[1] / peak - 1); }
  const m = rets.reduce((a, b) => a + b, 0) / rets.length;
  const sd = Math.sqrt(rets.reduce((a, b) => a + (b - m) ** 2, 0) / rets.length) * Math.sqrt(52);
  const yrs = (MS(out[out.length - 1][0]) - MS(out[0][0])) / (365.25 * 864e5);
  $("#pickStats").innerHTML = `At ${WEIGHT}%: ${pc((v ** (1 / yrs) - 1) * 100)} a year, volatility ${(sd * 100).toFixed(1)}%, worst drawdown ${(dd * 100).toFixed(1)}% (weekly figures).`;
}

/* ==========================================================================
   Europe
   ========================================================================== */
let EU_ON = ["AAA today", "AAA 1 year ago", "All issuers today"];
let ETAB = "bunds", ERANGE = 3652;
function renderEurope() {
  if (!ok("europe")) { $("#b-eu").innerHTML = failed("europe"); return; }
  const E = D.europe;
  $("#b-eu").innerHTML = `
    <div class="two">
      <div><div class="grp" style="margin-top:0">The euro curve</div>
        <div class="fbar">${chips("euChips", [["AAA today", COL.ink], ["AAA 1 month ago", COL.s1], ["AAA 1 year ago", COL.s2], ["All issuers today", COL.s3]].map(([k, c]) => ({ k, name: k, color: c })), EU_ON)}</div>
        <div class="lc" id="euCurve"></div>
        <div class="src">ECB spot curves: AAA-rated governments (in practice Germany and the Netherlands) and all euro-area governments, ${dmy(E.asof)}.</div></div>
      <div><div class="grp" style="margin-top:0">Bunds</div>
        <table class="tbl"><thead><tr><th>German yield</th><th>Now</th><th>1D</th><th>1M</th><th>1Y</th><th>vs since 2000</th></tr></thead><tbody>
        ${E.bunds.map(b => `<tr><td>${esc(b.label)}</td><td><b>${yv(b.last)}</b></td><td>${bpc(b.d1)}</td><td>${bpc(b.m1)}</td><td>${bpc(b.y1)}</td><td>${ord(b.pct_2000)}${pbar(b.pct_2000)}</td></tr>`).join("")}
        </tbody></table>
        <div class="src">Bundesbank, daily, ${dmy((E.bunds[0] || {}).asof)}.${E.us_bund ? ` US 10-year minus Bund: <b>${E.us_bund.last_bp}bp</b> (${bp(E.us_bund.m1)} in a month, ${pctl(E.us_bund.pct_2000)} since 2000).` : ""}</div></div>
    </div>
    <div class="tabs" id="etabs">${[["bunds", "Bund yields"], ["usde", "US − Bund"], ["spreads", "Spreads over Germany"]].map(([k, n]) => `<button class="tab ${k === ETAB ? "on" : ""}" data-k="${k}">${n}</button>`).join("")}</div>
    <div class="fbar">${rangeBar("eRange", ERANGE, [["1Y", 365], ["5Y", 1826], ["10Y", 3652], ["Since 2000", "max"]])}</div>
    <div class="lc" id="euHist"></div>
    <div class="grp">Ten-year yields across the euro area</div>
    <div class="tscroll"><table class="tbl"><thead><tr><th>Country</th><th>10-year</th><th>1Y</th><th>Spread over Germany</th><th>Spread 1Y</th><th>Widest since 2008</th></tr></thead><tbody>
      ${E.countries.map(c => `<tr><td>${esc(c.name)}</td><td><b>${yv(c.v)}</b></td><td>${bpc(c.y1)}</td><td>${c.c === "DE" ? "–" : `<b>${c.spread}bp</b>`}</td><td>${c.c === "DE" ? "" : bpc(c.spread_y1)}</td>
        <td class="l">${c.spread_max != null ? `${c.spread_max}bp <span class="dim">${mon(c.spread_max_d)}</span>` : ""}</td></tr>`).join("")}</tbody></table></div>
    <div class="src">Monthly averages (ECB convergence yields), latest ${mon((E.countries[0] || {}).asof)}. The spread is what a country pays over Germany: the market's price for its fiscal and political risk. Italy's peaked above 500bp in the 2011 debt crisis.</div>`;
  wireChips("euChips", EU_ON, drawEuCurve);
  $$("#etabs .tab").forEach(b => b.onclick = () => { ETAB = b.dataset.k; $$("#etabs .tab").forEach(x => x.classList.toggle("on", x === b)); drawEuHist(); });
  wireRange("eRange", r => { ERANGE = r; drawEuHist(); });
  drawEuCurve(); drawEuHist();
}

function drawEuCurve() {
  const E = D.europe, cats = ["3M", "6M", "1Y", "2Y", "3Y", "5Y", "7Y", "10Y", "15Y", "20Y", "30Y"];
  const get = (lab, key) => ((E.curves.find(c => c.label === lab) || {})[key] || []).map(p => [cats.indexOf(p.t), p.v]).filter(p => p[0] >= 0);
  const defs = { "AAA today": ["Today", "aaa", COL.ink, 2.4], "AAA 1 month ago": ["1 month ago", "aaa", COL.s1, 1.4], "AAA 1 year ago": ["1 year ago", "aaa", COL.s2, 1.4], "All issuers today": ["Today", "all", COL.s3, 1.6] };
  lchart($("#euCurve"), {
    xType: "cat", cats, height: 240, label: "Euro curve", noLegend: true, yfmt: v => v.toFixed(2) + "%",
    series: EU_ON.map(k => ({ name: k, color: defs[k][2], w: defs[k][3], pts: get(defs[k][0], defs[k][1]) })),
  });
}

async function drawEuHist() {
  const st = startOf(ERANGE, "2000-01-01"), host = $("#euHist");
  if (ETAB === "bunds") {
    const H = await hist(["BUND2", "BUND10", "BUND30", "ECB_DFR"], st);
    lchart(host, { label: "Bund yields", endLabels: true, zero: true, yfmt: v => v.toFixed(1) + "%", tfmt: v => v.toFixed(2) + "%",
      series: [["BUND2", "2-year", COL.s1], ["BUND10", "10-year", COL.ink], ["BUND30", "30-year", COL.s2], ["ECB_DFR", "ECB deposit", COL.mid, "4 3"]]
        .map(([k, n, c, d]) => ({ name: n, color: c, dash: d, w: c === COL.ink ? 2 : 1.4, pts: (H.series || {})[k] || [] })) });
  } else if (ETAB === "usde") {
    const H = await hist(["SUS_DE10", "SUS_DE2"], st);
    lchart(host, { label: "US minus Bund", endLabels: true, zero: true, yfmt: v => Math.round(v) + "bp", tfmt: v => Math.round(v) + "bp",
      series: [["SUS_DE10", "10-year gap", COL.ink], ["SUS_DE2", "2-year gap", COL.s1]].map(([k, n, c]) => ({ name: n, color: c, w: c === COL.ink ? 2 : 1.4, pts: (H.series || {})[k] || [] })) });
  } else {
    const cs = [["IT", "Italy", COL.s2], ["FR", "France", COL.s1], ["ES", "Spain", COL.s3], ["GR", "Greece", COL.s4], ["NL", "Netherlands", COL.mid]];
    const H = await hist(["IRS_DE", ...cs.map(c => "IRS_" + c[0])], st < "2008-01-01" ? st : st);
    const de = Object.fromEntries(((H.series || {}).IRS_DE || []));
    lchart(host, { label: "Spreads over Germany", endLabels: true, zero: true, yfmt: v => Math.round(v) + "bp", tfmt: v => Math.round(v) + "bp",
      series: cs.map(([c, n, col]) => ({ name: n, color: col, w: 1.5, pts: ((H.series || {})["IRS_" + c] || []).filter(p => de[p[0]] != null).map(p => [p[0], (p[1] - de[p[0]]) * 100]) })) });
  }
}

/* ==========================================================================
   the world
   ========================================================================== */
let WRANGE = 3652;
function renderGlobal() {
  if (!ok("global")) { $("#b-world").innerHTML = failed("global"); return; }
  const G = D.global;
  $("#b-world").innerHTML = `
    <div class="tscroll"><table class="tbl"><thead><tr><th>Market</th><th>10-year</th><th>1M</th><th>1Y</th><th>vs since 2000</th><th>2-year</th><th>2s10s</th><th>Policy rate</th><th class="l">Source</th></tr></thead><tbody>
    ${G.rows.map(r => `<tr><td><b>${esc(r.name)}</b></td><td><b>${yv(r.ten)}</b> <span class="dim">${r.freq === "monthly" ? mon(r.asof) : dm(r.asof)}</span></td><td>${bpc(r.m1)}</td><td>${bpc(r.y1)}</td>
      <td>${ord(r.pct_2000)}${pbar(r.pct_2000)}</td><td>${yv(r.two)}</td><td>${r.slope_bp != null ? bpc(r.slope_bp) : "–"}</td>
      <td>${r.policy != null ? `${yv(r.policy)} <span class="dim">${esc(r.policy_name)}</span>` : "–"}</td><td class="l dim">${esc(r.src)}${r.freq === "monthly" ? " · monthly avg" : ""}</td></tr>`).join("")}</tbody></table></div>
    <div class="two" style="margin-top:.7rem">
      <div><div class="fbar">${rangeBar("wRange", WRANGE, [["1Y", 365], ["5Y", 1826], ["10Y", 3652], ["Since 2000", "max"]])}</div><div class="lc" id="wChart"></div>
        <div class="src">Daily 10-year yields: US Treasury, German Bund (Bundesbank), UK gilt (Bank of England, zero-coupon), Japanese JGB (Ministry of Finance).</div></div>
      <div><div class="grp" style="margin-top:0">Japan's curve, now and a year ago</div><div class="lc" id="jgbChart"></div>
        <div class="src">JGB yields, 2 to 40 years. For two decades Japan anchored global yields near zero; watch whether that anchor is still there.</div></div>
    </div>`;
  wireRange("wRange", r => { WRANGE = r; drawWorld(); });
  drawWorld();
  const cats = (G.jgb.now || []).map(p => p.t);
  lchart($("#jgbChart"), { xType: "cat", cats, height: 230, label: "JGB curve", yfmt: v => v.toFixed(2) + "%",
    series: [{ name: "Now", color: COL.ink, w: 2.2, pts: (G.jgb.now || []).map((p, i) => [i, p.v]) },
      { name: "A year ago", color: COL.s2, w: 1.4, pts: (G.jgb.y1 || []).map(p => [cats.indexOf(p.t), p.v]).filter(p => p[0] >= 0) }] });
}

async function drawWorld() {
  const H = await hist(["DGS10", "BUND10", "GB10", "JP10"], startOf(WRANGE, "2000-01-01"));
  lchart($("#wChart"), { label: "10-year yields", endLabels: true, zero: true, yfmt: v => v.toFixed(1) + "%", tfmt: v => v.toFixed(2) + "%",
    series: [["DGS10", "US", COL.ink], ["BUND10", "Germany", COL.s1], ["GB10", "UK", COL.s2], ["JP10", "Japan", COL.s3]]
      .map(([k, n, c]) => ({ name: n, color: c, w: c === COL.ink ? 2 : 1.4, pts: (H.series || {})[k] || [] })) });
}

/* ==========================================================================
   credit
   ========================================================================== */
function renderCredit() {
  if (!ok("credit")) { $("#b-credit").innerHTML = failed("credit"); return; }
  const C = D.credit, M = C.mortgage;
  const oas = C.rows.filter(r => r.kind === "oas"), ylds = C.rows.filter(r => r.kind === "yield");
  $("#b-credit").innerHTML = `
    <div class="two">
      <div><div class="lc" id="crChart"></div>
        <div class="src">ICE BofA option-adjusted spreads over Treasuries, in basis points (FRED publishes only the last three years of these).</div></div>
      <div><table class="tbl"><thead><tr><th>Spread over govts</th><th>Now</th><th>1M</th><th>1Y</th><th>Percentile</th></tr></thead><tbody>
        ${oas.map(r => `<tr><td>${esc(r.name)}</td><td><b>${Math.round(r.last * 100)}bp</b></td><td>${bpc(r.m1)}</td><td>${bpc(r.y1)}</td><td>${ord(r.pct)} <span class="dim">since ${r.since.slice(0, 4)}</span></td></tr>`).join("")}
        </tbody></table>
        <table class="tbl" style="margin-top:.5rem"><thead><tr><th>All-in yield</th><th>Now</th><th>1M</th><th>1Y</th></tr></thead><tbody>
        ${ylds.map(r => `<tr><td>${esc(r.name)}</td><td><b>${yv(r.last)}</b></td><td>${bpc(r.m1)}</td><td>${bpc(r.y1)}</td></tr>`).join("")}</tbody></table>
        ${M ? `<div class="cards" style="margin-top:.6rem;grid-template-columns:1fr"><div class="card"><div class="k">30-year US mortgage (Freddie Mac, ${dmy(M.asof)})</div><div class="v">${yv(M.last)}</div>
          <div class="t">${M.spread_bp}bp over the 10-year Treasury; ${bp(M.y1)} in a year; ${pctl(M.pct_2000)} since 2000${M.high_since ? `, highest since ${mon(M.high_since)}` : ""}. Where the 10-year goes, housing follows.</div></div></div>` : ""}
      </div>
    </div>`;
  hist(["BAMLH0A0HYM2", "BAMLHE00EHYIOAS", "BAMLC0A4CBBB", "BAMLC0A0CM"], "2000-01-01").then(H => {
    lchart($("#crChart"), { label: "Credit spreads", endLabels: true, yfmt: v => Math.round(v * 100) + "bp", tfmt: v => Math.round(v * 100) + "bp",
      series: [["BAMLH0A0HYM2", "US high yield", COL.s2], ["BAMLHE00EHYIOAS", "Euro high yield", COL.s4], ["BAMLC0A4CBBB", "US BBB", COL.s1], ["BAMLC0A0CM", "US inv. grade", COL.ink]]
        .map(([k, n, c]) => ({ name: n, color: c, w: 1.5, pts: (H.series || {})[k] || [] })) });
  });
}

/* ==========================================================================
   junk bonds
   ========================================================================== */
let JREC = null;
function renderJunk() {
  if (!ok("junk")) { $("#b-junk").innerHTML = failed("junk"); return; }
  const J = D.junk, L = J.long && !J.long.error ? J.long : null, V = J.vs || {}, E = J.earn, DS = J.dispersion;
  if (JREC == null) JREC = J.recovery;
  const by = Object.fromEntries(J.rows.map(r => [r.k, r]));
  const us = by.US, eu = by.EU;
  const bpv = v => v == null ? "–" : Math.round(v * 100) + "bp";
  const maxEp = Math.max(...J.episodes.map(e => e.oas), us ? us.oas : 0);
  const bar = (lab, v, cls, note, dg = 2) => `<div class="jbar ${cls || ""}"><span class="l">${lab}</span><span class="b"><i style="width:${Math.max(.6, v / maxEp * 100).toFixed(1)}%"></i></span><span class="v">${v.toFixed(dg)}%${note ? ` <span class="dim">${note}</span>` : ""}</span></div>`;

  $("#b-junk").innerHTML = `
    <div class="prose" style="max-width:96ch">A junk bond is a loan to a company the rating agencies rate below BBB: more likely to default, so it has to pay more.
      The <b>spread</b> is that extra pay over a Treasury of the same length. It is the cleanest gauge there is of how worried lenders are, and it moves with stocks:
      wide when they fear a recession, tight when they do not.</div>
    <div class="tscroll" style="margin-top:.6rem"><table class="tbl" id="jTbl"><thead><tr><th class="l">Rating</th><th>Spread</th><th>1M</th><th>1Y</th>
      <th title="Where today sits in the three years FRED shows">3-year percentile</th><th class="l">3-year range</th><th>All-in yield</th>
      <th title="The yearly default rate the spread pays for if it paid for nothing else">Pays for defaults of</th>
      <th title="S&P, global companies, 1981-2024: the share defaulting in a year, averaged">Average year</th>
      <th title="The spread less what an average year of defaults costs">Left after an average year</th></tr></thead><tbody></tbody></table></div>
    <div class="fbar" style="margin-top:.4rem"><span class="lbl">If a defaulted bond pays back</span>
      <input type="range" id="jRec" min="0" max="70" step="5" value="${Math.round(JREC * 100)}" style="width:160px;accent-color:var(--accent)">
      <b id="jRecV">${Math.round(JREC * 100)}%</b><span class="dim" style="font-size:.66rem">about 40% on average over decades; much less in a bad recession</span></div>
    <div class="src">ICE BofA indexes via FRED, option-adjusted spreads over Treasuries (FRED publishes only the last three years of these, so the percentiles are three-year ones).
      <b>Pays for defaults of</b>: a lender who gets back ${Math.round(JREC * 100)}% of a defaulted bond loses the rest, so a spread s covers s ÷ (1 − recovery) of the index defaulting each year.
      That is an upper bound on what the market expects, because the spread also pays for the risk of a bad year. <b>Average year</b>: S&P's one-year default rates by rating, 1981–2024.
      S&P's CCC/C includes companies already on the brink, so the CCC comparison is rough. There is no euro split by rating on FRED.</div>

    <div class="two" style="margin-top:.8rem">
      <div><div class="grp" style="margin-top:0">Spreads by rating</div><div class="lc" id="jChart"></div>
        <div class="src">BB, B and the whole indexes, three years. CCC is on its own scale, right.</div></div>
      <div><div class="grp" style="margin-top:0">CCC and lower, and its gap over BB</div><div class="lc" id="jCcc"></div>
        <div class="src">${DS ? `The gap is <b>${bpv(DS.gap)}</b> now, the ${ord(DS.pct)} percentile of three years, ${bp(DS.y1)} in a year. When it widens while BB stays calm, the stress is in the companies closest to default, not in junk as a whole.` : ""}</div></div>
    </div>

    <div class="two" style="margin-top:.8rem">
      <div><div class="grp" style="margin-top:0">Where the US spread stands against its history</div>
        ${us ? bar("Today", us.oas, "now", dm(us.asof)) : ""}
        ${bar("Long-run average, since 1997", J.long_avg, "avg", "about", 0)}
        ${J.episodes.map(e => bar(`${e.d.slice(0, 4)} · ${esc(e.what)}`, e.oas, "", e.approx ? "about" : "", e.approx ? 1 : 2)).join("")}
        <div class="src">FRED keeps only three years of ICE BofA data; the older turning points are from ICE BofA's published history, rounded where sources differ.
          ${us ? `Today's ${bpv(us.oas)} is ${us.oas < J.long_avg ? "below" : "above"} the long-run average${us.oas < 3 ? ", in the tight zone where junk has paid little for its risk" : ""}.
          Spreads can stay tight for years, as they did from 2004 to 2007: tight says the pay is thin, not when that will change.` : ""}</div></div>
      <div><div class="grp" style="margin-top:0">What it earned against what it promised</div>
        ${(J.realised || []).map(x => `<div class="cards" style="grid-template-columns:1fr;margin-bottom:.4rem"><div class="card"><div class="k">${esc(x.name)} · ${dmy(x.from)} to ${dmy(x.to)}</div>
          <div class="v">${yv(x.ret)} <small>a year</small></div>
          <div class="t">It yielded ${yv(x.yield0)} at the start${x.oas0 != null ? `; the spread went from ${bpv(x.oas0)} to ${bpv(x.oas1)}, which ${x.oas1 < x.oas0 ? "added to the return" : "took from it"}` : ""}.
            ${x.ret < x.yield0 ? `It earned ${(x.yield0 - x.ret).toFixed(1)} points a year less than it promised: that gap is defaults, downgrades and${x.oas1 < x.oas0 ? " the yield falling as it was reinvested" : " prices falling"}.` : "It earned more than its starting yield, because spreads narrowed."}</div></div></div>`).join("")}
        <div class="src">Total-return indexes (ICE BofA via FRED), in each index's own currency. Over a full cycle junk usually earns less than its starting yield, because defaults come out of it; a calm stretch flatters it.</div>
        ${E && E.gap != null ? `<div class="cards" style="grid-template-columns:1fr"><div class="card"><div class="k">Junk against stocks' earnings yield</div>
          <div class="v">${yv(E.hy)} <small>vs</small> ${yv(E.earn)}</div>
          <div class="t">US junk yields ${E.gap.toFixed(1)} points more than the S&P 500's ${E.fwd ? "forward" : "trailing"} earnings yield. Junk's pay is fixed and capped; stocks' grows with earnings. A wide gap is junk competing hard with stocks for money.</div></div></div>` : ""}
      </div>
    </div>

    <div class="grp">Against stocks</div>
    ${V.corr != null ? `<div class="prose" style="max-width:96ch">Over the last three years the US spread and the S&P 500 moved against each other day to day (correlation <b>${V.corr.toFixed(2)}</b>, ${V.n} days): when stocks fall, spreads widen.
      Today the S&P is ${V.spx_dd != null ? `${Math.abs(V.spx_dd).toFixed(1)}% below its high of the past year` : "–"} and the spread is at its ${ord(V.hy_pct)} three-year percentile:
      ${V.mood === "credit" ? "<b>credit is more worried than stocks</b>." : V.mood === "stocks" ? "<b>stocks are more worried than credit</b>; the bond market sees less trouble." : "the two tell the same story."}</div>` : ""}
    ${L ? `<div class="two" style="margin-top:.5rem">
      <div><div class="lc" id="jDd"></div>
        <div class="src">How far each was below its own high, weekly, ${mon(L.from)} to now: HYG (the largest US junk fund) against SPY (the S&P 500), both with income reinvested, in dollars.</div></div>
      <div><div class="cards" style="grid-template-columns:1fr 1fr">
          <div class="card"><div class="k">Junk, a year</div><div class="v">${yv(L.hy.ann, 1)}</div><div class="t">swinging ${yv(L.hy.vol, 1)} a year; worst fall ${yv(L.hy.maxdd, 1)}</div></div>
          <div class="card"><div class="k">Stocks, a year</div><div class="v">${yv(L.eq.ann, 1)}</div><div class="t">swinging ${yv(L.eq.vol, 1)} a year; worst fall ${yv(L.eq.maxdd, 1)}</div></div></div>
        ${L.worst_months ? `<div class="prose" style="margin-top:.5rem">In the S&P 500's ${L.worst_months.n} worst months since ${L.from.slice(0, 4)}, stocks lost ${Math.abs(L.worst_months.eq).toFixed(1)}% on average and junk ${Math.abs(L.worst_months.hy).toFixed(1)}%;
          junk fell in ${L.worst_months.hy_down} of the ${L.worst_months.n}. It took about <b>${L.capture}%</b> of the stock market's hit, for ${Math.round(L.hy.ann / L.eq.ann * 100)}% of its return. Monthly correlation ${L.corr_m.toFixed(2)}.</div>` : ""}
        <table class="tbl" style="margin-top:.5rem"><thead><tr><th class="l">Sell-off</th><th>Junk</th><th>Stocks</th></tr></thead><tbody>
          ${L.windows.map(w => `<tr><td class="l">${w.k} · ${esc(w.what)}</td><td class="neg">${yv(w.hy, 1)}</td><td class="neg">${yv(w.eq, 1)}</td></tr>`).join("")}</tbody></table>
        <div class="src">The worst fall inside each window. Junk is not a hedge for a stock portfolio: it falls when stocks fall, a little less. Treasuries and euro government bonds rose in most of these, though not in 2022.</div></div>
    </div>` : J.long && J.long.error ? `<div class="note"><b>No long history</b>${esc(J.long.error)}</div>` : ""}

    <div class="grp">In your portfolio</div>
    <div class="tscroll"><table class="tbl"><thead><tr><th class="l">Fund you can buy</th><th>Yield now, before defaults</th><th>Fee</th><th>Duration</th><th class="l"></th></tr></thead><tbody>
      ${(J.funds || []).map(f => `<tr><td class="l"><b>${esc(f.name)}</b> <span class="dim">${esc(f.sym)} · ${esc(f.ccy)}</span></td><td>${yv(f.yield)}</td><td>${f.ter.toFixed(2)}%</td><td>${f.dur.toFixed(1)}y</td><td class="l dim">${esc(f.note || "")}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="src">Both are UCITS funds on DEGIRO-reachable exchanges. Their yields are the index's less the fee, before defaults${J.ea3 != null && eu ? `; euro junk's ${yv(eu.yield)} compares with ${yv(J.ea3)} on three-year euro governments` : ""}.
      They are in the <a href="#s-book">what-if above</a> under High yield, against your own book: how much each would have cut your swings, and what it did on your worst days.</div>`;

  junkTable();
  $("#jRec").oninput = e => { JREC = +e.target.value / 100; $("#jRecV").textContent = e.target.value + "%"; junkTable(); };
  hist(["BAMLH0A1HYBB", "BAMLH0A2HYB", "BAMLH0A0HYM2", "BAMLHE00EHYIOAS", "BAMLH0A3HYC"], "2000-01-01").then(H => {
    const S = H.series || {};
    lchart($("#jChart"), { label: "Junk spreads", endLabels: true, yfmt: v => Math.round(v * 100) + "bp", tfmt: v => Math.round(v * 100) + "bp",
      series: [["BAMLH0A1HYBB", "BB", COL.s1], ["BAMLH0A2HYB", "B", COL.s2], ["BAMLH0A0HYM2", "US HY", COL.ink], ["BAMLHE00EHYIOAS", "Euro HY", COL.s4]]
        .map(([k, n, c]) => ({ name: n, color: c, w: c === COL.ink ? 2 : 1.4, pts: S[k] || [] })) });
    const bb = new Map((S.BAMLH0A1HYBB || []).map(p => [p[0], p[1]]));
    const gap = (S.BAMLH0A3HYC || []).filter(p => bb.has(p[0])).map(p => [p[0], +(p[1] - bb.get(p[0])).toFixed(3)]);
    lchart($("#jCcc"), { label: "CCC and lower", endLabels: true, yfmt: v => Math.round(v * 100) + "bp", tfmt: v => Math.round(v * 100) + "bp",
      series: [{ name: "CCC & lower", color: COL.s2, w: 2, pts: S.BAMLH0A3HYC || [] }, { name: "CCC over BB", color: COL.mid, w: 1.2, dash: "4 3", pts: gap }] });
  });
  if (L) lchart($("#jDd"), { label: "Below the high", endLabels: true, yfmt: v => v.toFixed(0) + "%", tfmt: v => v.toFixed(1) + "%",
    series: [{ name: "Junk (HYG)", color: COL.s2, w: 1.6, pts: L.dd.map(r => [r[0], r[1]]) }, { name: "Stocks (SPY)", color: COL.ink, w: 1.4, pts: L.dd.map(r => [r[0], r[2]]) }] });
}

function junkTable() {
  const J = D.junk, rec = JREC;
  const bpv = v => v == null ? "–" : Math.round(v * 100) + "bp";
  $("#jTbl tbody").innerHTML = J.rows.map(r => {
    const be = r.oas / (1 - rec), loss = r.lr_default != null ? r.lr_default * (1 - rec) : null, left = loss != null ? r.oas - loss : null;
    return `<tr><td class="l"><b>${esc(r.name)}</b> <span class="dim">${esc(r.note)}</span></td>
      <td><b>${bpv(r.oas)}</b></td><td>${bpc(r.m1)}</td><td>${bpc(r.y1)}</td><td>${ord(r.pct)}${pbar(r.pct)}</td>
      <td class="l dim">${bpv(r.lo)} <small>${mon(r.lo_d)}</small> – ${bpv(r.hi)} <small>${mon(r.hi_d)}</small></td>
      <td>${yv(r.yield)}</td><td>${be.toFixed(1)}% <span class="dim">a year</span></td>
      <td>${r.lr_default != null ? r.lr_default.toFixed(r.lr_default < 1 ? 2 : 1) + "%" : `<span class="dim">–</span>`}</td>
      <td>${left != null ? `<span class="${left >= 0 ? "pos" : "neg"}">${left >= 0 ? "+" : ""}${left.toFixed(2)}pp</span> <span class="dim">${(r.oas / loss).toFixed(1)}× cover</span>` : `<span class="dim">–</span>`}</td></tr>`;
  }).join("");
}

/* ==========================================================================
   auctions
   ========================================================================== */
function renderAuctions() {
  if (!ok("auctions")) { $("#b-auct").innerHTML = failed("auctions"); return; }
  const A = D.auctions;
  const gcls = { strong: "good", average: "info", weak: "warn" };
  $("#b-auct").innerHTML = `
    <div class="two">
      <div><div class="grp" style="margin-top:0">Recent results</div>
        <div class="tscroll"><table class="tbl"><thead><tr><th>Auction</th><th class="l">Security</th><th>Size</th><th>Yield</th><th title="Bids received per dollar sold">Bid/cover</th><th>vs last 6</th><th title="Foreign and fund demand, share of competitive">Indirect</th><th title="Share the primary dealers had to take — high means weak demand">Dealers</th><th></th></tr></thead><tbody>
        ${A.recent.map(a => `<tr><td>${dmy(a.d)}</td><td class="l">${esc(a.term)}${a.reopen ? ` <span class="dim">reopen</span>` : ""}</td><td>$${a.size_bn}bn</td><td>${a.yield != null ? a.yield.toFixed(3) + "%" : "–"}</td>
          <td>${a.btc != null ? a.btc.toFixed(2) : "–"}</td><td class="dim">${a.btc_avg != null ? a.btc_avg.toFixed(2) : ""}</td><td>${a.indirect != null ? a.indirect + "%" : "–"}</td>
          <td>${a.dealer != null ? a.dealer + "%" : "–"}${a.dealer_avg != null ? ` <span class="dim">(${a.dealer_avg})</span>` : ""}</td><td>${a.grade ? `<span class="bdg ${gcls[a.grade]}">${a.grade}</span>` : ""}</td></tr>`).join("")}
        </tbody></table></div>
        <div class="src">Notes, bonds, TIPS and FRNs from TreasuryDirect, last 75 days. Graded against the previous six auctions of the same maturity: more bids per dollar and less left to dealers is strong demand. $${A.coupon_30d_bn}bn of coupons sold in the last 30 days.</div></div>
      <div><div class="grp" style="margin-top:0">Coming up</div>
        <table class="tbl"><thead><tr><th>Date</th><th class="l">Security</th><th>Size</th></tr></thead><tbody>
        ${A.upcoming.map(u => `<tr><td>${dmy(u.d)}</td><td class="l">${esc(u.term)}${u.reopen ? ` <span class="dim">reopen</span>` : ""}</td><td>${u.size_bn ? "$" + u.size_bn + "bn" : `<span class="dim">tba</span>`}</td></tr>`).join("") || `<tr><td colspan="3" class="dim">No coupon auctions announced yet.</td></tr>`}
        </tbody></table>
        <div class="src">Plus ${A.bills.n} bill auctions announced, $${A.bills.size_bn}bn. Sizes appear when the Treasury announces each auction, usually a few days before.</div></div>
    </div>`;
}

/* ==========================================================================
   news
   ========================================================================== */
let NTOPIC = "";
function renderNews() {
  if (!ok("news")) { $("#b-news").innerHTML = failed("news"); return; }
  const N = D.news, topics = [...new Set(N.items.flatMap(i => i.topics || []))];
  const ago = ts => { if (!ts) return ""; const h = (Date.now() / 1000 - ts) / 3600; return h < 1 ? `${Math.max(1, Math.round(h * 60))}m ago` : h < 48 ? `${Math.round(h)}h ago` : dmyTime(ts * 1000).slice(0, 10); };
  const list = N.items.filter(i => !NTOPIC || (i.topics || []).includes(NTOPIC));
  $("#b-news").innerHTML = `
    <div class="fbar"><button class="seg ${!NTOPIC ? "on" : ""}" data-t="">All ${N.items.length}</button>${topics.map(t => `<button class="seg ${NTOPIC === t ? "on" : ""}" data-t="${esc(t)}">${esc(t)}</button>`).join("")}</div>
    <div class="nlist">${list.map(i => `<a class="nws" href="${esc(i.url)}" target="_blank" rel="noopener"><div class="hl">${esc(i.headline)}</div>
      <div class="mt"><span>${esc(i.source)}</span><span>${ago(i.ts)}</span>${(i.topics || []).map(t => `<span class="tp">${esc(t)}</span>`).join("")}</div></a>`).join("") || `<div class="dim ld">No bond headlines right now.</div>`}</div>
    <div class="src">Yahoo's Treasury feeds (^TNX, TLT, ^TYX, 10-year futures)${N.news_age_s != null ? " plus the home screen's news, filtered for rates" : ""}.</div>`;
  $$("#b-news .fbar .seg").forEach(b => b.onclick = () => { NTOPIC = b.dataset.t; renderNews(); });
}

/* ==========================================================================
   summary and coming up
   ========================================================================== */
function renderSummary() {
  const order = ["us", "path", "inflation", "stocks", "portfolio", "europe", "global", "credit", "junk", "auctions"];
  const ic = { warn: "!", good: "▲", bad: "▼", info: "●" };
  const all = [];
  for (const p of order) if (ok(p)) for (const i of D[p].insights || []) all.push(i);
  const loading = order.filter(p => !D[p]).length;
  $("#insBox").innerHTML = all.map(i => `<a class="in ${esc(i.tone)}" href="#${SEC_OF[i.sec] || "s-curve"}">
    <span class="it"><i>${ic[i.tone] || "●"}</i>${esc(SEC_NAME[i.sec] || i.sec)}</span><span class="ix">${esc(i.text)}</span></a>`).join("")
    + (loading ? `<div class="dim" style="font-size:.66rem;padding:.4rem 0">${loading} panel(s) still loading<span class="blink">_</span></div>` : "")
    || `<div class="dim">Nothing yet.</div>`;
  renderComing();
}

function renderComing() {
  const ev = [];
  const today = new Date().toISOString().slice(0, 10);
  if (ok("news")) {
    for (const m of D.news.meetings || []) ev.push({ d: m.d, t: m.name, k: m.country });
    for (const e of D.news.events || []) ev.push({ d: e.d, t: `${e.name}${e.forecast ? ` · exp. ${e.forecast}` : ""}${e.prev ? ` · prev. ${e.prev}` : ""}`, k: e.country, time: e.time });
  }
  if (ok("auctions")) for (const u of (D.auctions.upcoming || []).slice(0, 8)) ev.push({ d: u.d, t: `${u.term} auction${u.size_bn ? ` · $${u.size_bn}bn` : ""}`, k: "UST" });
  const seen = new Set();
  const list = ev.filter(e => e.d >= today).sort((a, b) => a.d.localeCompare(b.d) || (a.time || "").localeCompare(b.time || ""))
    .filter(e => { const k = e.d + e.t; if (seen.has(k)) return false; seen.add(k); return true; }).slice(0, 16);
  $("#coming").innerHTML = list.map(e => `<div class="cm"><span class="d">${dmy(e.d)}${e.time ? " " + esc(e.time) : ""}</span><span>${e.k ? `<span class="dim">${esc(e.k)}</span> ` : ""}${esc(e.t)}</span></div>`).join("")
    || `<div class="dim" style="font-size:.7rem">${ok("news") || ok("auctions") ? "Nothing scheduled that we know of." : "Loading…"}</div>`;
}

async function renderSources() {
  let S = {};
  try { S = await (await fetch("/api/pf/bonds/sources")).json(); } catch { }
  const rows = Object.values(S).map(s => `<div class="row" style="align-items:flex-start"><span class="nm">${esc(s.label)}</span>
    <span class="px ${s.ok ? "pos" : "warnc"}">${s.ok ? "ok" : "not yet"}</span>
    <span class="ch dim" style="min-width:0;text-align:right;white-space:normal;max-width:60ch">${s.age_s != null ? `fetched ${s.age_s < 120 ? s.age_s + "s" : Math.round(s.age_s / 60) + "m"} ago` : ""}${
      s.missing && s.missing.length ? ` · missing: ${esc(s.missing.join(", "))}` : ""}${
      s.carried && s.carried.length ? ` · kept from the last fetch: ${esc(s.carried.join(", "))}` : ""}${
      s.status ? " · " + Object.entries(s.status).map(([k, v]) => `${esc(k)} ${v.ok ? "ok" : "failed" + (v.error ? ": " + esc(v.error.slice(0, 80)) : "")}`).join(" · ") : ""}</span></div>`).join("");
  const errs = PARTS.filter(p => D[p] && D[p].error).map(p => `<div class="row"><span class="nm">${esc(p)}</span><span class="px warnc">error</span><span class="ch dim" style="min-width:0;white-space:normal">${esc(D[p].error)}</span></div>`).join("");
  $("#b-src").innerHTML = `<div class="rows">${rows}${errs}</div>
    <div class="src">All keyless. Treasury yields are the Treasury's own par curve; the intraday tiles use Cboe's yield indices (^IRX, ^FVX, ^TNX, ^TYX) and are about 15 minutes delayed.
    Bond funds are priced from Yahoo's adjusted closes, so distributions count as return.</div>`;
}

const RENDER = {
  us: () => { renderTop(); renderCurve(); renderYields(); renderCB(); },
  europe: () => { renderTop(); renderEurope(); if (ok("us")) renderCB(); },
  global: () => { renderGlobal(); if (ok("us")) renderCB(); },
  inflation: renderInflation,
  stocks: renderStocks,
  portfolio: () => { renderBook(); drawRateSim(); },
  credit: renderCredit,
  junk: renderJunk,
  auctions: renderAuctions,
  news: renderNews,
  path: () => { renderTop(); renderPath(); },
};

load();
