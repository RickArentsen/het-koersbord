/* ==========================================================================
   IDX — the S&P 500 and the Nasdaq 100, from the inside. One index at a
   time (the switch at the top), every section read from /api/pf/idx/{ix}
   (pf/idx.py, rebuilt each evening after New York closes), your book from
   /api/pf/idx/{ix}/book, the two together from /api/pf/idx/compare.
   ========================================================================== */

const QS = new URLSearchParams(location.search);
let IX = (QS.get("i") || (() => { try { return localStorage.getItem("idx_ix"); } catch { return null; } })() || "spx").toLowerCase();
if (!["spx", "ndx"].includes(IX)) IX = "spx";
let V = null, BOOK = null, CMP = null, POLL = null;
let PER = "ytd", MSORT = "w", MDIR = -1, MN = 40, TREND_R = 1260, MAPP = "1d";
const NAME = { spx: "S&P 500", ndx: "Nasdaq 100" };
const VOLN = { spx: "VIX", ndx: "VXN" };
const PLAB = { "1d": "1D", "1w": "1W", "1m": "1M", "3m": "3M", ytd: "YTD", "1y": "1Y" };

$("#shell").innerHTML = termBar({ here: "IDX · THE INDICES", right:
  `<a class="tbtn" href="/m/MKT" title="Market snapshot">MKT</a><a class="tbtn" href="/m/RRG" title="Sector rotation">RRG</a>
   <button class="tbtn" id="rebBtn" title="Read every member again (two to three minutes)">Rebuild</button>` });
wireShell();
$("#rebBtn").onclick = () => load(true);

const p1 = (v, d = 1) => v == null || !isFinite(v) ? "–" : nf(v, d) + "%";
const sp = (v, d = 1) => v == null || !isFinite(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d) + "%";
const spp = (v, d = 1) => v == null || !isFinite(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d) + "pp";
const csp = (v, d = 1) => `<span class="${sgn(v)}">${sp(v, d)}</span>`;
const ordr = n => n == null ? "–" : ord(Math.round(n));   // kit.js ord, on a rounded percentile
const ICON = { good: "▲", warn: "!", bad: "▼", info: "·" };

async function load(rebuild = false) {
  clearTimeout(POLL);
  try { localStorage.setItem("idx_ix", IX); } catch { }
  history.replaceState(null, "", `/m/IDX?i=${IX}${location.hash}`);
  try {
    const r = await fetch(`/api/pf/idx/${IX}${rebuild ? "?rebuild=1" : ""}`);
    V = await r.json();
    if (!r.ok) throw new Error(V.detail || `HTTP ${r.status}`);
  } catch (e) {
    $("#app").innerHTML = `<div class="note"><b>Could not load ${NAME[IX]}</b>${esc(e.message)}</div>`;
    return;
  }
  if (V.building) { renderBuilding(); POLL = setTimeout(() => load(), 4000); return; }
  render();
  if ((V.job || {}).state === "running") POLL = setTimeout(() => load(), 8000);
  fetch(`/api/pf/idx/${IX}/book`).then(r => r.json()).then(j => { BOOK = j; renderBook(); }).catch(() => { BOOK = { available: false, why: "could not read" }; renderBook(); });
  fetch(`/api/pf/idx/compare`).then(r => r.json()).then(j => { CMP = j; renderCompare(); }).catch(() => { });
}

function sw() {
  return `<span class="ixsw">${["spx", "ndx"].map(k => `<button data-ix="${k}" class="${k === IX ? "on" : ""}">${NAME[k]}</button>`).join("")}</span>`;
}
function wireSw() {
  $$(".ixsw button").forEach(b => b.onclick = () => { if (b.dataset.ix !== IX) { IX = b.dataset.ix; V = BOOK = null; $("#app").innerHTML = `<div class="center">Loading<span class="blink">_</span></div>`; load(); } });
}

function renderBuilding() {
  const j = V.job || {};
  $("#app").innerHTML = `<div class="panel"><div class="body">${sw()}
    <div class="prog" style="margin-top:.8rem"><b>Building the ${NAME[IX]} for the first time.</b> Every member's prices for three years, the estimates the
      watchlist scan has not read, the reporting season, the option prices and the futures positioning: two to three minutes, after which it is
      rebuilt each evening after New York closes.<br><span class="dim">${esc(j.phase || "starting")}${j.error ? " — " + esc(j.error) : ""}</span></div></div></div>`;
  wireSw();
}

const SECTIONS = [["s-top", "Overview"], ["s-trend", "Trend"], ["s-breadth", "Breadth"], ["s-conc", "Concentration"], ["s-members", "Members"],
  ["s-sectors", "Sectors"], ["s-val", "Valuation"], ["s-earn", "Earnings"], ["s-vol", "Volatility"], ["s-pos", "Positioning"],
  ["s-hist", "History"], ["s-book", "Your book"], ["s-cmp", "The two"]];

function render() {
  const L = V.level || {}, R = L.r || {};
  const cur = (V.vol && V.vol.curve || []).find(x => x.sym === "^" + VOLN[IX]) || {};
  const b = V.breadth || {}, c = V.conc || {}, val = V.valuation || {}, ssn = V.season || {}, rv = V.revisions || {};
  const built = V.built_at ? new Date(V.built_at * 1000) : null;
  $("#app").innerHTML = `
  <div class="panel" id="s-top"><div class="body">
    <div class="ixhead">${sw()}<span class="lvl">${nf(L.last, 2)}</span><span class="chg">${csp(R["1d"], 2)}</span>
      <span class="dim">${dmy(L.d || V.asof)} · ${V.n_members} members</span><span class="sp"></span>
      <span class="dim">${built ? "built " + built.toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : ""}${V.stale ? " · being rebuilt" : ""}</span></div>
    <nav class="secnav" id="secnav">${SECTIONS.map(([id, t]) => `<a href="#${id}" data-id="${id}">${t}</a>`).join("")}</nav>
    <div class="two even">
      <div><div class="ctitle">What matters now</div><div class="ins">${(V.insights || []).map(x => `<div class="fd ${x.tone}"><i>${ICON[x.tone] || "·"}</i><span>${esc(x.text)}</span></div>`).join("")}</div></div>
      <div><div class="tiles">
        ${tile("From the high", sp(L.dd), null, `high ${nf(L.ath, 0)} on ${dmy(L.ath_d)}`)}
        ${tile("vs 200-day", sp(L.dist200), null, `50-day ${sp(L.dist50)}`)}
        ${tile("YTD", csp(R.ytd), null, L.tr_ytd != null ? `with dividends ${sp(L.tr_ytd)}` : "")}
        ${tile("1 year", csp(R["1y"]), null, L.tr_1y != null ? `with dividends ${sp(L.tr_1y)}` : "")}
        ${tile("3 years", sp(R["3y_pa"]) + "<small> a year</small>", null, `${sp(R["3y"], 0)} in all`)}
        ${tile("10 years", sp(R["10y_pa"]) + "<small> a year</small>", null, `${sp(R["10y"], 0)} in all`)}
        ${tile("Above 200-day", p1((b.p200 || []).slice(-1)[0], 0), null, `of ${b.n || "–"} members · 50-day ${p1((b.p50 || []).slice(-1)[0], 0)}`)}
        ${tile("Top 10 weight", p1(c.top10_now), null, `effective number ${nf(c.eff_now, 0)}`)}
        ${tile("Forward P/E", val.fwd_pe ? nf(val.fwd_pe, 1) : "–", null, val.trail_pe ? `trailing ${nf(val.trail_pe, 1)}` : "")}
        ${tile(VOLN[IX], cur.last != null ? nf(cur.last, 1) : "–", null, cur.pct10y != null ? `${ordr(cur.pct10y)} percentile of 10 years` : "")}
        ${tile("Season so far", ssn.reported ? p1(ssn.beat_pct, 0) + "<small> beat</small>" : "–", null, ssn.reported ? `${ssn.reported} reported, ${p1(ssn.reported_w, 0)} of the index` : "no reports yet")}
        ${tile("EPS estimates", rv.available ? csp(rv.eps_chg30, 2) : "–", null, rv.available ? `30 days · ${rv.up} raised, ${rv.down} cut` : "")}
      </div></div>
    </div>
    ${Object.keys(V.errors || {}).length ? `<div class="src">Parts that could not be read this time: ${esc(Object.entries(V.errors).map(([k, e]) => k + " (" + e + ")").join("; "))}</div>` : ""}
  </div></div>

  <div class="panel" id="s-trend"><header><h2>Price and trend</h2><span class="hint">ten years daily · the 50- and 200-day averages · equal weight beside it</span></header><div class="body">
    <div class="fbar">${rangeBar("trR", TREND_R, [["1Y", 252], ["2Y", 504], ["5Y", 1260], ["10Y", 2520]])}</div>
    <div class="lc" id="chTrend"></div><div class="ctitle">Below its high</div><div class="lc" id="chDD"></div>
    <div class="expl">The index against its own <b>50-day</b> and <b>200-day</b> averages: above the 200-day line is the usual definition of an uptrend. The dotted line is the
      <b>equal-weight</b> index (${IX === "spx" ? "every member 0.2%" : "every member 1%"}), rebased to the same start: when it trails, the largest shares are doing the work.
      The lower chart is the fall from the highest close so far.</div>
  </div></div>

  <div class="panel" id="s-breadth"><header><h2>Breadth — how many members are carrying it</h2><span class="hint">two years, rebuilt from the members' closes</span></header><div class="body">
    <div class="two even"><div><div class="ctitle">Share of members above their 200- and 50-day averages</div><div class="lc" id="chBr"></div></div>
      <div><div class="ctitle">New 52-week highs against new lows, each day</div><div class="lc" id="chHL"></div></div></div>
    <div class="two even"><div><div class="ctitle">Advance-decline line (rising members less falling ones, added up)</div><div class="lc" id="chAD"></div></div>
      <div><div class="ctitle">Equal weight against cap weight (100 = three years ago)</div><div class="lc" id="chEW"></div></div></div>
    <div class="two even"><div><div class="ctitle">Up volume: the members' shares traded in rising members, % of up and down volume</div><div class="lc" id="chUV"></div></div>
      <div><div class="ctitle">The lopsided days: 90% and 80% of the volume one way</div><div id="uvDays"></div></div></div>
    <div class="expl">An index can rise on a handful of shares. <b>${p1((b.p200 || []).slice(-1)[0], 0)}</b> of members are above their 200-day average and
      <b>${p1((b.near || []).slice(-1)[0], 0)}</b> within 5% of their 52-week high. When the index makes highs while this share falls, fewer shares are doing the
      lifting: a narrow market, which has tended to end either with the rest catching up or the leaders coming down. Below 25% has marked washed-out markets;
      above 85%, stretched ones. <b>Up volume</b> is Lowry's measure on the members: a day with 90% of the volume (and of the points) in falling shares is
      panic selling, and the low has tended to come once a 90% up day, or two 80% up days, answers it. <a href="/m/FLOW#s-breadth">FLOW</a> reads those signals,
      the NYSE's own figures and what followed them since 1965.</div>
  </div></div>

  <div class="panel" id="s-conc"><header><h2>Concentration</h2><span class="hint">${esc((V.members_src || {}).source || "")}</span></header><div class="body">
    <div class="two even"><div><div class="ctitle">The ten largest</div>${topTable()}</div>
      <div><div class="ctitle">Their share of the index: the ten largest, and the seven</div><div class="lc" id="chTop"></div>
        <div class="ctitle">The Magnificent Seven against the rest (100 = two years ago)</div><div class="lc" id="chMag"></div></div></div>
    <div class="expl">The <b>effective number</b> is how many equal weights would make the index this concentrated: ${nf(c.eff_now, 0)} for ${V.n_members} members.
      The weights back in time are today's weights moved with each share's price, holding share counts constant: right in shape, not to the decimal.
      The seven: ${esc((c.mag7_syms || []).join(", "))}, ${p1(c.mag7_now)} of the index.</div>
  </div></div>

  <div class="panel" id="s-members"><header><h2>Members</h2><span class="hint">sized by weight, grouped by sector, coloured by return</span></header><div class="body">
    <div class="fbar"><span class="dim">map</span>${["1d", "1w", "1m", "3m", "ytd", "1y"].map(k => `<button class="seg ${k === MAPP ? "on" : ""}" data-mp="${k}">${PLAB[k]}</button>`).join("")}</div>
    <div class="map" id="map"></div><div class="scale" id="mapScale"></div>
    <div class="ctitle" style="margin-top:.8rem">Every member</div>
    <div class="fbar"><span class="dim">returns and contribution over</span>${["1d", "1w", "1m", "3m", "ytd", "1y"].map(k => `<button class="seg ${k === PER ? "on" : ""}" data-per="${k}">${PLAB[k]}</button>`).join("")}
      <input class="srch" id="mq" placeholder="find a member" style="margin-left:auto;background:var(--ground);border:1px solid var(--rule-strong);color:var(--ink);font-family:var(--f-mono);font-size:.7rem;padding:.12rem .4rem"></div>
    <div id="mtbl"></div>
    <div class="ctitle">Who moved the index, ${PLAB[PER]}</div><div class="two even" id="contrib"></div>
  </div></div>

  <div class="panel" id="s-sectors"><header><h2>Sectors</h2><span class="hint">cap-weighted within each sector</span></header><div class="body" id="sectors"></div></div>

  <div class="panel" id="s-val"><header><h2>Valuation</h2><span class="hint">built from the members' own multiples</span></header><div class="body" id="val"></div></div>
  <div class="panel" id="s-earn"><header><h2>Earnings season and revisions</h2><span class="hint">${esc((V.season || {}).source || "")} · Yahoo's estimates</span></header><div class="body" id="earn"></div></div>
  <div class="panel" id="s-vol"><header><h2>Volatility and options</h2><span class="hint">percentiles over ten years · ${esc((V.options || {}).etf || "")} options</span></header><div class="body" id="vol"></div></div>
  <div class="panel" id="s-pos"><header><h2>Positioning in the future</h2><span class="hint">${esc((V.cot || {}).source || "CFTC")}</span></header><div class="body" id="pos"></div></div>
  <div class="panel" id="s-hist"><header><h2>What history says</h2><span class="hint">${V.history && V.history.from ? "since " + V.history.from.slice(0, 4) : ""}</span></header><div class="body" id="hist"></div></div>
  <div class="panel" id="s-book"><header><h2>Your book against the ${NAME[IX]}</h2><span class="hint">shares, trackers looked through, turbos at their exposure</span></header><div class="body" id="book">${spinner()}</div></div>
  <div class="panel" id="s-cmp"><header><h2>The S&amp;P 500 and the Nasdaq 100 together</h2></header><div class="body" id="cmp">${spinner()}</div></div>`;
  wireSw();
  wireNav();
  drawTrend();
  drawBreadth();
  drawConc();
  drawMap();
  renderMembers();
  renderSectors();
  renderVal();
  renderEarn();
  renderVol();
  renderPos();
  renderHist();
  if (BOOK) renderBook();
  if (CMP) renderCompare();
  wireRange("trR", r => { TREND_R = r; drawTrend(); });
  $$("[data-mp]").forEach(x => x.onclick = () => { MAPP = x.dataset.mp; $$("[data-mp]").forEach(y => y.classList.toggle("on", y === x)); drawMap(); });
  $$("[data-per]").forEach(x => x.onclick = () => { PER = x.dataset.per; $$("[data-per]").forEach(y => y.classList.toggle("on", y === x)); renderMembers(); });
  $("#mq").oninput = () => { MN = 40; renderMembers(); };
  wrapTables();
}
const spinner = () => `<div class="dim" style="padding:1rem 0">loading<span class="blink">_</span></div>`;

function wireNav() {
  const bar = document.querySelector(".term-bar");
  document.documentElement.style.setProperty("--barh", (bar ? bar.offsetHeight : 36) + "px");
  const links = $$("#secnav a");
  const io = new IntersectionObserver(es => { for (const e of es) if (e.isIntersecting) links.forEach(a => a.classList.toggle("on", a.dataset.id === e.target.id)); },
    { rootMargin: "-40% 0px -55% 0px" });
  SECTIONS.forEach(([id]) => { const el = document.getElementById(id); el && io.observe(el); });
  if (location.hash.length > 1) setTimeout(() => { const el = document.getElementById(location.hash.slice(1)); el && el.scrollIntoView(); }, 60);
}

/* ---------- trend ---------- */
function sma(arr, k) {
  const out = new Array(arr.length).fill(null); let s = 0;
  for (let i = 0; i < arr.length; i++) { s += arr[i]; if (i >= k) s -= arr[i - k]; if (i >= k - 1) out[i] = s / k; }
  return out;
}
function drawTrend() {
  const ch = V.chart; if (!ch) { $("#chTrend").innerHTML = `<div class="dim">No price history.</div>`; return; }
  const n = ch.c.length, a = Math.max(0, n - TREND_R);
  const m50 = sma(ch.c, 50), m200 = sma(ch.c, 200);
  const d = ch.d.slice(a), c = ch.c.slice(a);
  const ew = (ch.ew || []).slice(a); const e0 = ew.find(v => v != null), i0 = ew.findIndex(v => v != null);
  const ewR = e0 ? ew.map(v => v == null ? null : v / e0 * c[i0]) : null;
  lchart($("#chTrend"), { height: 290, endLabels: true, yfmt: v => nf(v, 0), label: "index", series: [
    { name: NAME[IX], color: "var(--ink)", w: 1.8, pts: d.map((x, i) => [x, c[i]]) },
    { name: "50-day", color: "var(--s1, #3987e5)", w: 1.1, pts: d.map((x, i) => [x, m50[a + i] != null ? Math.round(m50[a + i]) : null]) },
    { name: "200-day", color: "var(--warn)", w: 1.3, pts: d.map((x, i) => [x, m200[a + i] != null ? Math.round(m200[a + i]) : null]) },
    ...(ewR ? [{ name: "equal weight (rebased)", color: "var(--accent)", w: 1.1, dash: "3 3", pts: d.map((x, i) => [x, ewR[i] != null ? Math.round(ewR[i]) : null]) }] : [])] });
  let pk = 0; const dd = ch.c.map(v => { pk = Math.max(pk, v); return (v / pk - 1) * 100; }).slice(a);
  lchart($("#chDD"), { height: 150, zero: true, yfmt: v => nf(v, 0) + "%", tfmt: v => sp(v, 1), series: [{ name: "below the high", color: "var(--loss)", w: 1.3, pts: d.map((x, i) => [x, Math.round(dd[i] * 10) / 10]) }] });
}

/* ---------- breadth ---------- */
function drawBreadth() {
  const b = V.breadth; if (!b || !b.d) { $("#chBr").innerHTML = `<div class="dim">No breadth.</div>`; return; }
  lchart($("#chBr"), { height: 230, yfmt: v => nf(v, 0) + "%", refs: [{ y: 25, label: "washed out" }, { y: 85, label: "stretched" }], series: [
    { name: "above 200-day", color: "var(--warn)", w: 1.8, pts: b.d.map((x, i) => [x, b.p200[i]]) },
    { name: "above 50-day", color: "var(--accent)", w: 1.1, pts: b.d.map((x, i) => [x, b.p50[i]]) }] });
  lchart($("#chHL"), { height: 230, zero: true, yfmt: v => nf(v, 0), series: [
    { name: "new highs less new lows (10-day average)", color: "var(--gain)", w: 1.6, pts: b.d.map((x, i) => [x, avgN(b.nh, i, 10) - avgN(b.nl, i, 10)]) },
    { name: "new highs", color: "var(--ink-soft)", w: 0.8, pts: b.d.map((x, i) => [x, b.nh[i]]) },
    { name: "new lows", color: "var(--loss)", w: 0.8, pts: b.d.map((x, i) => [x, -b.nl[i]]) }] });
  lchart($("#chAD"), { height: 200, yfmt: v => nf(v, 0), series: [
    { name: "advance-decline line", color: "var(--ink)", w: 1.6, pts: b.d.map((x, i) => [x, b.ad[i]]) }] });
  const ew = V.ew;
  if (ew) lchart($("#chEW"), { height: 200, yfmt: v => nf(v, 0), refs: [{ y: 100 }], series: [{ name: "equal weight ÷ cap weight", color: "var(--accent)", w: 1.6, pts: ew.d.map((x, i) => [x, ew.v[i]]) }] });
  else $("#chEW").innerHTML = `<div class="dim">No equal-weight index.</div>`;
  drawUpVol();
}

/* Lowry's test on the members (pf/flow.py day_kind): 90% of the volume AND of the points one way, or 80% of the volume. */
function uvKind(u, i) {
  const t = u.upv[i] + u.dnv[i], p = u.ptu[i] + u.ptd[i];
  if (!t) return null;
  const s = u.upv[i] / t * 100, q = p ? u.ptu[i] / p * 100 : null;
  if (s <= 10 && (q == null || q <= 10)) return "90down";
  if (s >= 90 && (q == null || q >= 90)) return "90up";
  return s <= 20 ? "80down" : s >= 80 ? "80up" : null;
}
function drawUpVol() {
  const u = V.updown, host = $("#chUV");
  if (!host) return;
  if (!u || !u.d) { host.innerHTML = `<div class="dim">The next build adds the members' volume (it runs each evening after New York closes).</div>`; $("#uvDays").innerHTML = ""; return; }
  const n = Math.min(504, u.d.length), o = u.d.length - n;
  const sh = u.d.map((x, i) => { const t = u.upv[i] + u.dnv[i]; return t ? Math.round(u.upv[i] / t * 1000) / 10 : null; });
  const k = u.d.map((x, i) => uvKind(u, i));
  const ten = sh.map((x, i) => { const w = sh.slice(Math.max(0, i - 9), i + 1).filter(v => v != null); return w.length ? Math.round(w.reduce((a, b) => a + b, 0) / w.length * 10) / 10 : null; });
  lchart(host, { height: 210, yfmt: v => nf(v, 0) + "%", refs: [{ y: 10, label: "90% down" }, { y: 90, label: "90% up" }], series: [
    { name: "up volume, each day", color: "var(--ink-soft)", w: 0.8, pts: u.d.slice(o).map((x, i) => [x, sh[o + i]]) },
    { name: "ten-day average", color: "var(--accent)", w: 1.6, pts: u.d.slice(o).map((x, i) => [x, ten[o + i]]) },
    { name: "90% days", color: "var(--warn)", w: 0.01, dots: true, pts: u.d.slice(o).map((x, i) => [x, k[o + i] && k[o + i].startsWith("90") ? sh[o + i] : null]) }] });
  const days = u.d.map((x, i) => ({ d: x, k: k[i], s: sh[i], a: u.adv[i], b: u.dec[i] })).filter(x => x.k).reverse().slice(0, 12);
  const lab = { "90down": "90% down", "90up": "90% up", "80down": "80% down", "80up": "80% up" };
  $("#uvDays").innerHTML = days.length ? `<table class="tbl"><thead><tr><th>Day</th><th>Kind</th><th>Up volume</th><th>Up : down members</th></tr></thead><tbody>
    ${days.map(x => `<tr><td>${dmy(x.d)}</td><td class="${x.k.endsWith("down") ? "neg" : "pos"}">${lab[x.k]}</td><td>${p1(x.s, 1)}</td><td>${x.a} : ${x.b}</td></tr>`).join("")}</tbody></table>`
    : `<div class="dim">No day in three years had 80% of the volume one way.</div>`;
}
const avgN = (a, i, k) => { let s = 0, n = 0; for (let j = Math.max(0, i - k + 1); j <= i; j++) { s += a[j]; n++; } return Math.round(s / n * 10) / 10; };

/* ---------- concentration ---------- */
function topTable() {
  const c = V.conc || {};
  return `<table class="tbl"><thead><tr><th>Member</th><th>Weight</th><th>YTD</th></tr></thead><tbody>
    ${(c.top || []).map(r => `<tr><td><a class="tkr" href="/t/${encodeURIComponent(r.sym)}"><b>${esc(r.sym)}</b></a><span class="nm">${esc(r.name || "")}</span></td><td>${p1(r.w, 2)}</td><td>${csp(r.ytd)}</td></tr>`).join("")}
    <tr><td class="dim">together</td><td><b>${p1(c.top10_now, 1)}</b></td><td></td></tr></tbody></table>`;
}
function drawConc() {
  const c = V.conc; if (!c || !c.d) return;
  lchart($("#chTop"), { height: 190, yfmt: v => nf(v, 1) + "%", series: [{ name: "ten largest, % of the index", color: "var(--warn)", w: 1.8, pts: c.d.map((x, i) => [x, c.top10[i]]) },
    ...(c.mag7 ? [{ name: "the seven", color: "var(--accent)", w: 1.2, pts: c.d.map((x, i) => [x, c.mag7[i]]) }] : [])] });
  if (c.mag7_line) lchart($("#chMag"), { height: 190, yfmt: v => nf(v, 0), refs: [{ y: 100 }], series: [
    { name: "the seven", color: "var(--accent)", w: 1.8, pts: c.d.map((x, i) => [x, c.mag7_line[i]]) },
    { name: "the rest", color: "var(--ink-mid)", w: 1.6, pts: c.d.map((x, i) => [x, c.rest_line[i]]) }] });
}

/* ---------- the map: squarified, sector by sector ---------- */
function squarify(items, x, y, w, h) {
  const out = [], total = items.reduce((a, b) => a + b.v, 0) || 1;
  let rest = items.slice().sort((a, b) => b.v - a.v).map(it => ({ ...it, a: it.v / total * w * h }));
  while (rest.length) {
    const wide = w >= h, side = wide ? h : w;
    let row = [], best = Infinity;
    for (const it of rest) {
      const cand = row.concat([it]), s = cand.reduce((a, b) => a + b.a, 0);
      const mx = Math.max(...cand.map(z => z.a)), mn = Math.min(...cand.map(z => z.a));
      const worst = Math.max(side * side * mx / (s * s), (s * s) / (side * side * mn));
      if (worst > best && row.length) break;
      row = cand; best = worst;
    }
    const s = row.reduce((a, b) => a + b.a, 0), thick = s / side;
    let off = 0;
    for (const it of row) {
      const len = it.a / thick;
      out.push({ ...it, x: wide ? x : x + off, y: wide ? y + off : y, w: wide ? thick : len, h: wide ? len : thick });
      off += len;
    }
    if (wide) { x += thick; w -= thick; } else { y += thick; h -= thick; }
    rest = rest.slice(row.length);
  }
  return out;
}
const CAP = { "1d": 3, "1w": 6, "1m": 10, "3m": 20, ytd: 40, "1y": 50 };
function colour(v, cap) {
  if (v == null) return "#2a3238";
  const t = Math.max(-1, Math.min(1, v / cap)), a = Math.abs(t);
  return t >= 0 ? `rgb(${Math.round(30 - 10 * a)},${Math.round(60 + 110 * a)},${Math.round(50 + 40 * a)})`
    : `rgb(${Math.round(70 + 150 * a)},${Math.round(45 + 15 * a)},${Math.round(45 + 10 * a)})`;
}
function drawMap() {
  const host = $("#map"); if (!host) return;
  const W = host.clientWidth, H = host.clientHeight, cap = CAP[MAPP];
  const groups = {};
  for (const m of V.members || []) { if (!m.w) continue; (groups[m.sector] = groups[m.sector] || []).push(m); }
  const secs = squarify(Object.entries(groups).map(([k, ms]) => ({ k, v: ms.reduce((a, b) => a + b.w, 0), ms })), 0, 0, W, H);
  let html = "";
  for (const s of secs) {
    html += `<div class="sec" style="left:${s.x}px;top:${s.y}px;width:${s.w}px;height:${s.h}px"><span>${esc(s.w > 70 && s.h > 30 ? s.k : "")}</span></div>`;
    const pad = s.h > 40 ? 12 : 0;
    for (const t of squarify(s.ms.map(m => ({ ...m, v: m.w })), s.x, s.y + pad, s.w, s.h - pad)) {
      const r = (t.r || {})[MAPP], big = t.w > 46 && t.h > 30, fs = Math.max(9, Math.min(18, Math.sqrt(t.w * t.h) / 5));
      html += `<div class="m" data-s="${esc(t.sym)}" style="left:${t.x}px;top:${t.y}px;width:${t.w}px;height:${t.h}px;background:${colour(r, cap)};font-size:${fs}px">
        ${big ? `<b>${esc(t.sym)}</b><span style="font-size:${Math.max(8, fs * .72)}px">${sp(r, 1)}</span>` : ""}</div>`;
    }
  }
  host.innerHTML = html;
  $("#mapScale").innerHTML = `${sp(-cap, 0)} ${[-1, -.5, 0, .5, 1].map(t => `<i style="background:${colour(t * cap, cap)}"></i>`).join("")} ${sp(cap, 0)} <span style="margin-left:.6rem">${PLAB[MAPP]} · click a member for its page</span>`;
  const tip = $("#mtip"), by = Object.fromEntries((V.members || []).map(m => [m.sym, m]));
  $$("#map .m").forEach(el => {
    el.onmousemove = e => { const m = by[el.dataset.s]; if (!m) return;
      tip.innerHTML = `<b>${esc(m.sym)}</b> ${esc(m.name || "")}<br><span class="dim">${esc(m.sector)} · ${p1(m.w, 2)} of the index</span><br>
        ${["1d", "1w", "1m", "ytd", "1y"].map(k => `${PLAB[k]} ${sp((m.r || {})[k])}`).join(" · ")}${m.fpe ? `<br>forward P/E ${nf(m.fpe, 1)}` : ""}`;
      tip.style.display = "block"; tip.style.left = Math.min(window.innerWidth - 290, e.clientX + 14) + "px"; tip.style.top = (e.clientY + 14) + "px"; };
    el.onmouseleave = () => tip.style.display = "none";
    el.onclick = () => location.href = "/t/" + encodeURIComponent(el.dataset.s);
  });
}

/* ---------- members ---------- */
const MCOLS = [["sym", "Member", r => r.sym], ["sector", "Sector", r => r.sector], ["w", "Weight", r => r.w], ["r", "Return", r => (r.r || {})[PER]],
  ["c", "Contribution", r => (r.c || {})[PER]], ["fpe", "Fwd P/E", r => r.fpe], ["dd52", "From 52w high", r => r.dd52],
  ["a200", "200-day", r => r.a200 ? 1 : 0], ["rev30", "EPS est. 30d", r => r.rev30]];
function renderMembers() {
  const q = ($("#mq") && $("#mq").value || "").trim().toUpperCase();
  const held = new Set((BOOK || {}).held || []);
  let rows = (V.members || []).filter(r => !q || r.sym.includes(q) || (r.name || "").toUpperCase().includes(q));
  const key = (MCOLS.find(c => c[0] === MSORT) || MCOLS[2])[2];
  rows.sort((a, b) => { const x = key(a), y = key(b); if (x == null) return 1; if (y == null) return -1; return typeof x === "string" ? MDIR * x.localeCompare(y) : MDIR * (x - y); });
  $("#mtbl").innerHTML = `<table class="tbl"><thead><tr>${MCOLS.map(([k, lab]) => `<th class="sort ${k === MSORT ? "on" : ""}" data-ms="${k}">${lab}${k === "r" || k === "c" ? " " + PLAB[PER] : ""}</th>`).join("")}</tr></thead><tbody>
    ${rows.slice(0, MN).map(r => `<tr class="${held.has(r.sym) ? "mine" : ""}"><td><a class="tkr" href="/t/${encodeURIComponent(r.sym)}"><b>${esc(r.sym)}</b></a><span class="nm">${esc((r.name || "").slice(0, 28))}</span></td>
      <td class="l dim">${esc(r.sector || "")}</td><td>${p1(r.w, 2)}</td><td>${csp((r.r || {})[PER])}</td>
      <td class="${sgn((r.c || {})[PER])}">${(r.c || {})[PER] != null ? spp((r.c || {})[PER], 2) : "–"}</td><td>${r.fpe ? nf(r.fpe, 1) : "–"}</td>
      <td class="${sgn(r.dd52)}">${sp(r.dd52)}</td><td>${r.a200 ? `<span class="pos">above</span>` : `<span class="neg">below</span>`}</td><td>${csp(r.rev30, 1)}</td></tr>`).join("")}</tbody></table>
    ${rows.length > MN ? `<button class="tbtn" id="mMore" style="margin-top:.4rem">Show all ${rows.length}</button>` : ""}
    <div class="src">Contribution: each member's share of the index's move, in percentage points: its weight at the start of the period times its return.
      Together they come to ${spp((V.contrib_sum || {})[PER] ?? (V.members || []).reduce((t, r) => t + ((r.c || {})[PER] || 0), 0), 2)} against the index's ${sp(((V.level || {}).r || {})[PER], 2)}: the members' returns include their dividends,
      and a member added during the period counts from its start. A gold edge: you hold it yourself, as shares or a turbo.</div>`;
  $$("[data-ms]").forEach(th => th.onclick = () => { if (MSORT === th.dataset.ms) MDIR = -MDIR; else { MSORT = th.dataset.ms; MDIR = ["sym", "sector"].includes(MSORT) ? 1 : -1; } renderMembers(); });
  const mm = $("#mMore"); if (mm) mm.onclick = () => { MN = 9999; renderMembers(); };
  const all = (V.members || []).filter(r => (r.c || {})[PER] != null).sort((a, b) => b.c[PER] - a.c[PER]);
  const mx = Math.max(0.01, ...all.map(r => Math.abs(r.c[PER])));
  const bars = list => list.map(r => `<div class="hb"><span class="l"><a class="tkr" href="/t/${encodeURIComponent(r.sym)}">${esc(r.sym)}</a> <span class="dim">${sp(r.r[PER])}</span></span>
    <span class="t"><span class="z" style="left:50%"></span><i style="${r.c[PER] >= 0 ? "left:50%" : "right:50%"};width:${Math.abs(r.c[PER]) / mx * 50}%;background:${r.c[PER] >= 0 ? "var(--gain)" : "var(--loss)"}"></i></span>
    <span class="v ${sgn(r.c[PER])}">${spp(r.c[PER], 2)}</span></div>`).join("");
  $("#contrib").innerHTML = `<div><div class="ctitle">Added the most</div>${bars(all.slice(0, 10))}</div><div><div class="ctitle">Took away the most</div>${bars(all.slice(-10).reverse())}</div>`;
  wrapTables();
}

/* ---------- sectors ---------- */
function renderSectors() {
  const S = V.sectors || [];
  const mx = Math.max(1, ...S.map(s => Math.abs((s.r || {}).ytd || 0)));
  $("#sectors").innerHTML = `<table class="tbl"><thead><tr><th>Sector</th><th>Weight</th><th>Members</th>${["1d", "1w", "1m", "3m", "ytd", "1y"].map(k => `<th>${PLAB[k]}</th>`).join("")}<th>Contribution YTD</th><th class="l">YTD</th></tr></thead><tbody>
    ${S.map(s => `<tr><td>${esc(s.sector)}</td><td>${p1(s.w)}</td><td class="dim">${s.n}</td>${["1d", "1w", "1m", "3m", "ytd", "1y"].map(k => `<td>${csp((s.r || {})[k])}</td>`).join("")}
      <td class="${sgn((s.c || {}).ytd)}">${spp((s.c || {}).ytd, 2)}</td>
      <td class="l"><span class="hb" style="display:inline-grid;grid-template-columns:1fr;width:140px"><span class="t"><span class="z" style="left:50%"></span><i style="${((s.r || {}).ytd || 0) >= 0 ? "left:50%" : "right:50%"};width:${Math.abs((s.r || {}).ytd || 0) / mx * 50}%;background:${((s.r || {}).ytd || 0) >= 0 ? "var(--gain)" : "var(--loss)"}"></i></span></span></td></tr>`).join("")}
  </tbody></table><div class="expl">Each sector's return is its members' returns weighted by their weight at the start of the period; its contribution is what it added to the index.
  A sector can lose ground and still carry the index if it is large enough, and a small one can double without moving it.</div>`;
}

/* ---------- valuation ---------- */
function renderVal() {
  const v = V.valuation || {}, rec = V.record || {};
  const cape = v.cape;
  $("#val").innerHTML = `<div class="two even"><div>
      ${kvv("Forward P/E (next twelve months' estimates)", v.fwd_pe ? nf(v.fwd_pe, 1) : "–", "big")}
      ${kvv("Trailing P/E (the last twelve months' reported)", v.trail_pe ? nf(v.trail_pe, 1) : "–")}
      ${kvv("The ten largest, forward", v.top10_fwd_pe ? nf(v.top10_fwd_pe, 1) : "–")}
      ${kvv("The rest, forward", v.rest_fwd_pe ? nf(v.rest_fwd_pe, 1) : "–")}
      ${kvv("Equal weight, forward", v.equal_fwd_pe ? nf(v.equal_fwd_pe, 1) : "–")}
      ${kvv("Earnings yield (1 ÷ forward P/E)", p1(v.earn_yield, 2))}
      ${kvv("against the ten-year Treasury", v.us10y != null ? `${p1(v.us10y, 2)} · gap <span class="${sgn(v.erp)}">${spp(v.erp, 2)}</span>` : "–")}
      ${cape ? kvv("CAPE (Shiller, ten-year average real earnings)", `<b>${nf(cape.value, 1)}</b> <span class="dim">${esc(cape.asof || "")}</span>`) : ""}
      ${cape && cape.pred10 != null ? kvv("ten-year real return it has implied", `${sp(cape.pred10 * 100, 1)} a year <span class="dim">(average ${sp(cape.avg10 * 100, 1)})</span>`) : ""}
      <div class="src">${esc(v.note || "")}; ${p1(v.coverage_w, 0)} of the index's weight has a forward P/E. The index's multiple is total price over total earnings
        (a weighted harmonic mean), not an average of P/Es.</div></div>
    <div><div class="ctitle">Forward P/E, recorded here each evening</div><div class="lc" id="chPE"></div>
      <div class="expl">The <b>earnings yield</b> is what a year of the index's forecast profit is worth against its price; when it is below the ten-year yield,
        the bond pays more today and the shares are priced for growth. ${cape ? `<b>CAPE</b> ${nf(cape.value, 0)}: of the ${(cape.above30 || {}).n || "?"} months
        since 1871 above 30, the median real return over the next ten years was ${sp(((cape.above30 || {}).median || 0) * 100, 1)} a year.` : ""}
        The gap between the ten largest and the rest says whether the index's multiple is the giants' or everyone's.</div></div></div>`;
  const pts = (rec.d || []).map((d, i) => [d, rec.fwd_pe[i]]).filter(p => p[1] != null);
  if (pts.length >= 2) lchart($("#chPE"), { height: 200, yfmt: v => nf(v, 1), series: [{ name: "forward P/E", color: "var(--accent)", w: 1.8, pts }] });
  else $("#chPE").innerHTML = `<div class="dim" style="font-family:var(--f-ui);font-size:.78rem;padding:.6rem 0">The record starts today: a point each evening from here on.</div>`;
}
const kvv = (k, v, cls = "") => `<div class="kv"><span>${k}</span><span class="${cls}">${v}</span></div>`;

/* ---------- earnings and revisions ---------- */
function renderEarn() {
  const s = V.season || {}, r = V.revisions || {};
  const W = (s.next || []).reduce((a, x) => { (a[x.d] = a[x.d] || []).push(x); return a; }, {});
  $("#earn").innerHTML = `<div class="two even"><div>
    <div class="ctitle">This season (quarter to ${dmy(s.quarter_end)}), since ${dmy(s.since)}</div>
    ${s.reported ? `<div class="tiles">${tile("Reported", String(s.reported), null, `${p1(s.reported_w, 0)} of the index`)}
      ${tile("Beat", p1(s.beat_pct, 0), null, `${s.beat} beat · ${s.inline} in line · ${s.miss} missed`)}
      ${tile("Surprise", sp(s.surprise_w), null, "weighted, against the estimate")}
      ${tile("Growth", s.growth_w != null ? sp(s.growth_w) : "–", null, s.growth_n ? `on a year, ${s.growth_n} members` : "from the next reports on")}</div>
      <div class="two even" style="margin-top:.5rem"><div><div class="ctitle">Best surprises</div>${(s.best || []).map(x => `<div class="kv"><span><a class="tkr" href="/t/${x.sym}">${esc(x.sym)}</a> <span class="dim">${dmy(x.d)}</span></span><span class="pos">${sp(x.s)}</span></div>`).join("")}</div>
        <div><div class="ctitle">Worst</div>${(s.worst || []).map(x => `<div class="kv"><span><a class="tkr" href="/t/${x.sym}">${esc(x.sym)}</a> <span class="dim">${dmy(x.d)}</span></span><span class="${sgn(x.s)}">${sp(x.s)}</span></div>`).join("")}</div></div>`
      : `<div class="dim">No member has reported yet this season.</div>`}
    <div class="ctitle">Reporting in the coming week · ${p1(s.next_w, 0)} of the index</div>
    ${Object.entries(W).map(([d, xs]) => `<div class="kv"><span style="white-space:nowrap">${new Date(d + "T12:00:00").toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" })} <span class="dim">${p1(xs.reduce((t, x) => t + x.w, 0), 0)}</span></span><span>${xs.slice(0, 12).map(x => `<a class="tkr" href="/m/ERN/${x.sym}" title="${p1(x.w, 2)} of the index${x.est != null ? ", estimate " + x.est : ""}">${esc(x.sym)}</a>${x.when ? `<span class="dim">${x.when === "bmo" ? "·am" : x.when === "amc" ? "·pm" : ""}</span>` : ""}`).join(" ")}${xs.length > 12 ? ` <span class="dim">+${xs.length - 12}</span>` : ""}</span></div>`).join("") || `<div class="dim">none</div>`}
  </div><div>
    <div class="ctitle">Next year's EPS: analysts in the last thirty days</div>
    ${r.available ? `<div class="tiles">${tile("Raised", String(r.up), null, `${p1(r.up_w, 0)} of the weight`)}${tile("Cut", String(r.down), null, `${p1(r.down_w, 0)} of the weight`)}
      ${tile("Index EPS, 30 days", csp(r.eps_chg30, 2), null, `90 days ${sp(r.eps_chg90, 1)}`)}${tile("Revisions", r.net_ratio != null ? (r.net_ratio > 0 ? "+" : "") + nf(r.net_ratio, 2) : "–", null, `${r.analysts_up} up · ${r.analysts_down} down`)}</div>
      ${barPair("members raised", r.up, "members cut", r.down)}
      <div class="expl">Prices follow earnings, and earnings follow what analysts expect. When far more members are being raised than cut, the index's forward
        earnings are rising under it, and a flat price means it is getting cheaper. Revisions: (raises − cuts) ÷ all changes by individual analysts, from −1 to +1.
        Of ${r.n} members with estimates.</div>` : `<div class="dim">${esc(r.why || "no estimates")}</div>`}
  </div></div>`;
}
function barPair(la, a, lb, b) {
  const t = (a + b) || 1;
  return `<div style="display:flex;height:12px;margin:.6rem 0 .2rem;border:1px solid var(--rule)"><i style="width:${a / t * 100}%;background:var(--gain)"></i><i style="width:${b / t * 100}%;background:var(--loss)"></i></div>
    <div class="dim" style="font-size:.64rem;display:flex;justify-content:space-between"><span>${a} ${la}</span><span>${b} ${lb}</span></div>`;
}

/* ---------- volatility ---------- */
function renderVol() {
  const v = V.vol || {}, o = V.options || {};
  const curve = v.curve || [];
  $("#vol").innerHTML = `<div class="two even"><div>
      <div class="ctitle">${VOLN[IX]} against what the index actually moved (21 days, annualised) · two years</div><div class="lc" id="chVol"></div>
      <div class="expl">${VOLN[IX]} is the volatility options price for the next thirty days. The gap over what the index then moves (<b>${spp(v.premium, 1)}</b> now) is
        what buyers of protection pay; it is usually positive, and negative only in a market moving faster than anyone priced.</div></div>
    <div><div class="ctitle">${IX === "spx" ? "The curve: protection for nine days, one, three and six months" : "The Nasdaq's volatility index beside the S&amp;P's"}</div><div class="lc" id="chCurve"></div>
      <table class="tbl"><thead><tr><th>Index</th><th>Now</th><th>A month ago</th><th>10-year percentile</th><th>Median</th><th>Range</th></tr></thead><tbody>
      ${curve.map(x => `<tr><td>${esc(x.sym.replace("^", ""))}</td><td><b>${nf(x.last, 1)}</b></td><td>${nf(x.m1, 1)}</td><td>${x.pct10y != null ? ordr(x.pct10y) : "–"}</td><td class="dim">${nf(x.median, 1)}</td><td class="dim">${nf(x.lo, 0)}–${nf(x.hi, 0)}</td></tr>`).join("")}</tbody></table>
      <div class="expl">${v.slope != null ? `VIX over VIX3M <b>${nf(v.slope, 2)}</b>: ${v.inverted ? "<b>inverted</b>, the next month dearer than three — the market is pricing stress now." : "below 1, the usual upward slope of a calm market."} ` : ""}
        ${v.vxn_vix != null ? `VXN over VIX ${nf(v.vxn_vix, 2)}: the Nasdaq's options against the S&P's. ` : ""}SKEW measures how dear crash protection is against ordinary
        protection; VVIX is the volatility of the VIX itself.</div></div></div>
    <div class="ctitle">What ${esc(o.etf || "")}'s options price</div>
    ${o.available ? `<table class="tbl"><thead><tr><th>Horizon</th><th>Expiry</th><th>Move, either way</th><th>A day</th><th>Implied volatility</th><th>Straddle</th></tr></thead><tbody>
      ${o.moves.map(m => `<tr><td>${esc(m.label)}</td><td>${dmy(m.expiry)} <span class="dim">${m.days}d</span></td><td><b>±${nf(m.move_pct, 1)}%</b></td><td>±${nf(m.daily, 2)}%</td><td>${p1(m.iv)}</td><td class="dim">${nf(m.straddle, 2)} on ${nf(m.strike, 0)}</td></tr>`).join("")}</tbody></table>
      ${o.events && o.events.length ? `<div class="src">US releases this week that options price in: ${o.events.map(e => `${esc(e.title)} (${dmy(e.d)})`).join(", ")}.</div>` : ""}
      <div class="src">An at-the-money straddle costs about what the market expects the ETF to move, either way, by its expiry.</div>` : `<div class="dim">${esc(o.why || "no options")}</div>`}`;
  const s = v.series || {};
  if (s.d) lchart($("#chVol"), { height: 220, yfmt: v => nf(v, 0), series: [
    { name: VOLN[IX], color: "var(--warn)", w: 1.6, pts: s.d.map((x, i) => [x, s.v[i]]) },
    { name: "realised", color: "var(--ink-mid)", w: 1.2, pts: s.d.map((x, i) => [x, s.rv ? s.rv[i] : null]) }] });
  const order = ["^VIX9D", "^VIX", "^VIX3M", "^VIX6M"], lab = { "^VIX9D": "9 days", "^VIX": "1 month", "^VIX3M": "3 months", "^VIX6M": "6 months" };
  const cc = order.map(k => curve.find(x => x.sym === k)).filter(Boolean);
  if (cc.length >= 2) lchart($("#chCurve"), { height: 170, xType: "cat", cats: cc.map(x => lab[x.sym]), yfmt: v => nf(v, 1), series: [
    { name: "now", color: "var(--warn)", w: 2, pts: cc.map((x, i) => [i, x.last]) },
    { name: "a month ago", color: "var(--ink-soft)", w: 1.4, dash: "4 3", pts: cc.map((x, i) => [i, x.m1]) }] });
  else $("#chCurve").innerHTML = "";
}

/* ---------- positioning ---------- */
function renderPos() {
  const c = V.cot || {};
  if (!c.available) { $("#pos").innerHTML = `<div class="dim">${esc(c.why || "not available")}</div>`; return; }
  $("#pos").innerHTML = `<div class="two even"><div><div class="lc" id="chCot"></div></div><div>
    ${kvv("Asset managers (pension funds, mutual funds), net", `<b>${sp(c.am_pct)}</b> of open interest · ${ordr(c.am_rank)} percentile of five years · ${spp(c.am_4w)} in four weeks`)}
    ${kvv("Leveraged funds (hedge funds, CTAs), net", `<b>${sp(c.lf_pct)}</b> · ${ordr(c.lf_rank)} percentile · ${spp(c.lf_4w)} in four weeks`)}
    ${kvv("As of", dmy(c.asof) + " (released the Friday after)")}
    <div class="expl">The ${esc(c.future)} future is where the big money adjusts its exposure fastest. <b>Asset managers</b> hold long index exposure as a matter
      of course; how much is the question. <b>Leveraged funds</b> trade it both ways, and are often short against long positions elsewhere: a deep short is
      fuel for a squeeze, a big long a crowded trade. Extremes (above the 95th or below the 5th percentile) matter more than the level.</div></div></div>`;
  const s = c.series;
  lchart($("#chCot"), { height: 240, zero: true, yfmt: v => nf(v, 0) + "%", series: [
    { name: "asset managers", color: "var(--accent)", w: 1.7, pts: s.d.map((x, i) => [x, s.am[i]]) },
    { name: "leveraged funds", color: "var(--warn)", w: 1.5, pts: s.d.map((x, i) => [x, s.lf[i]]) }] });
}

/* ---------- history ---------- */
function renderHist() {
  const h = V.history || {};
  if (!h.available) { $("#hist").innerHTML = `<div class="dim">No long history.</div>`; return; }
  const H = [["21", "1 month"], ["63", "3 months"], ["126", "6 months"], ["252", "12 months"]];
  const cell = (x, k) => x ? `<td>${csp(x.median)}</td><td>${p1(x.pos, 0)}</td><td class="dim">${sp(x.p10, 0)} to ${sp(x.p90, 0)}</td>` : `<td colspan="3" class="dim">too few</td>`;
  const mo = new Date().getMonth() + 1, MN_ = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const mx = Math.max(0.5, ...(h.months || []).map(m => Math.abs(m.mean)));
  $("#hist").innerHTML = `<div class="expl">Today the ${NAME[IX]} is <b>${sp(h.dist200)}</b> against its 200-day average and <b>${esc(h.bucket)}</b>.
      ${h.n_similar} months since ${h.from.slice(0, 4)} stood within ${h.band} points of that distance and in the same band below the high (one day a month counted,
      so a long rally is not twenty pieces of evidence). What followed, against every month:</div>
    <table class="tbl"><thead><tr><th>After</th><th>Like today: median</th><th>up</th><th>10th to 90th</th><th>Every month: median</th><th>up</th><th>10th to 90th</th></tr></thead><tbody>
      ${H.map(([k, lab]) => `<tr><td>${lab}</td>${cell((h.similar || {})[k])}${cell((h.all || {})[k])}</tr>`).join("")}</tbody></table>
    <div class="two even" style="margin-top:.8rem"><div><div class="ctitle">The months of the year: average return, share up</div>
      ${(h.months || []).map(m => `<div class="hb"><span class="l" style="${m.m === mo ? "color:var(--accent)" : ""}">${MN_[m.m - 1]}${m.m === mo ? " (now)" : ""} <span class="dim">${p1(m.pos, 0)} up</span></span>
        <span class="t"><span class="z" style="left:50%"></span><i style="${m.mean >= 0 ? "left:50%" : "right:50%"};width:${Math.abs(m.mean) / mx * 50}%;background:${m.mean >= 0 ? "var(--gain)" : "var(--loss)"}"></i></span>
        <span class="v ${sgn(m.mean)}">${sp(m.mean, 2)}</span></div>`).join("")}
      <div class="src">${(h.months || [])[0] ? (h.months[0].n) : ""} years of each month. Seasonality is weak evidence: the spread inside each month is many times the difference between them.</div></div>
    <div><div class="ctitle">The deepest falls</div><table class="tbl"><thead><tr><th>Peak</th><th>Fall</th><th>To the low</th><th>Back to the peak</th></tr></thead><tbody>
      ${(h.falls || []).map(f => `<tr><td>${dmy(f.peak)}</td><td class="neg">${sp(f.depth)}</td><td class="dim">${mths(f.down_days)}</td><td>${f.recovered ? dmy(f.recovered) + ` <span class="dim">${Math.round(f.up_days / 252 * 10) / 10} years</span>` : `<span class="warnc">not yet</span>`}</td></tr>`).join("")}</tbody></table>
      <div class="ctitle">The last times it stood like this</div>
      ${(h.recent_examples || []).map(x => `<div class="kv"><span>${dmy(x.d)} <span class="dim">${sp(x.dist)} vs 200-day, ${sp(x.dd)} from high</span></span><span>${x.f63 == null ? `<span class="dim">too recent to know</span>` : `3 months ${csp(x.f63)} · 12 ${x.f252 != null ? csp(x.f252) : `<span class="dim">not yet</span>`}`}</span></div>`).join("")}</div></div>`;
}

const mths = days => { const m = Math.max(1, Math.round(days / 21)); return m + (m === 1 ? " month" : " months"); };

/* ---------- your book ---------- */
function renderBook() {
  const host = $("#book"); if (!host) return;
  const b = BOOK;
  if (!b) { host.innerHTML = spinner(); return; }
  if (!b.available) { host.innerHTML = `<div class="dim">${esc(b.why || "not available")}</div>`; return; }
  host.innerHTML = `<div class="tiles">
      ${tile("Overlap", p1(b.overlap), null, "of your equity exposure is the index itself, at its weights")}
      ${tile("Active share", p1(b.active_share), null, "the rest: different shares or different weights")}
      ${tile("In its members", p1(b.in_index), null, `${b.names_in_book} of its ${b.members} members`)}
      ${tile("Outside it", p1(b.outside), null, "shares, funds and turbos it does not hold")}
      ${tile("Beta", b.beta != null ? nf(b.beta, 2) : "–", null, b.weeks ? `${b.weeks} weeks, in euros` : "")}
      ${tile("Correlation", b.corr != null ? nf(b.corr, 2) : "–", null, b.te != null ? `tracking error ${p1(b.te)} a year` : "")}</div>
    <div class="two even" style="margin-top:.6rem"><div><div class="ctitle">Where you hold more than the index</div>
      <table class="tbl"><thead><tr><th>Share</th><th>Your book</th><th>Index</th><th>More by</th><th class="l">Through</th></tr></thead><tbody>
      ${(b.over || []).map(x => `<tr><td><a class="tkr" href="/t/${x.sym}"><b>${esc(x.sym)}</b></a></td><td>${p1(x.book, 2)}</td><td class="dim">${p1(x.index, 2)}</td><td class="pos">${spp(x.diff, 2)}</td><td class="l dim">${esc((x.via || []).join(", "))}</td></tr>`).join("")}</tbody></table></div>
    <div><div class="ctitle">Where you hold less</div>
      <table class="tbl"><thead><tr><th>Share</th><th>Your book</th><th>Index</th><th>Less by</th></tr></thead><tbody>
      ${(b.under || []).map(x => `<tr><td><a class="tkr" href="/t/${x.sym}"><b>${esc(x.sym)}</b></a></td><td>${p1(x.book, 2)}</td><td class="dim">${p1(x.index, 2)}</td><td class="neg">${spp(x.diff, 2)}</td></tr>`).join("")}</tbody></table></div></div>
    <div class="expl">Your equity exposure of €${nf(b.exposure_eur, 0)} counts shares at their value, an S&amp;P 500 or Nasdaq 100 tracker as its members at their weights,
      and a turbo as the share it tracks at its exposure (value times leverage) — so a turbo on NVIDIA adds to your NVIDIA. <b>Overlap</b> is how much of it you would keep
      if you swapped the book for the index; <b>active share</b> is the part that is your own choice. Beta and tracking error compare the book's weekly returns with the
      index's, both in euros.</div>`;
  wrapTables();
  if (V) renderMembers();
}

/* ---------- the two together ---------- */
function renderCompare() {
  const host = $("#cmp"); if (!host || !CMP || !CMP.d) return;
  const s = CMP.spx || {}, n = CMP.ndx || {};
  host.innerHTML = `<div class="two even"><div><div class="ctitle">The Nasdaq 100 against the S&amp;P 500 (100 = five years ago)</div><div class="lc" id="chRat"></div></div>
    <div><div class="ctitle">How closely they move: 63-day correlation and the Nasdaq's beta to the S&amp;P</div><div class="lc" id="chCor"></div></div></div>
    <table class="tbl" style="margin-top:.5rem"><thead><tr><th></th><th>S&amp;P 500</th><th>Nasdaq 100</th></tr></thead><tbody>
      <tr><td>YTD</td><td>${csp(s.ytd)}</td><td>${csp(n.ytd)}</td></tr><tr><td>1 year</td><td>${csp(s.r1y)}</td><td>${csp(n.r1y)}</td></tr>
      <tr><td>From the high</td><td>${sp(s.dd)}</td><td>${sp(n.dd)}</td></tr><tr><td>Forward P/E</td><td>${nf(s.fwd_pe, 1)}</td><td>${nf(n.fwd_pe, 1)}</td></tr>
      <tr><td>Ten largest</td><td>${p1(s.top10)}</td><td>${p1(n.top10)}</td></tr><tr><td>Above 200-day</td><td>${p1(s.p200, 0)}</td><td>${p1(n.p200, 0)}</td></tr></tbody></table>
    <div class="expl">${CMP.shared != null ? `<b>${CMP.shared}</b> companies are in both; the Nasdaq 100's members are <b>${p1(CMP.ndx_in_spx)}</b> of the S&amp;P 500's weight.
      ` : ""}The Nasdaq 100 is the S&amp;P's growth end with the financials left out: when the line rises, growth and technology lead; its beta above 1 is how much
      more it swings.</div>`;
  lchart($("#chRat"), { height: 210, yfmt: v => nf(v, 0), refs: [{ y: 100 }], series: [{ name: "Nasdaq 100 ÷ S&P 500", color: "var(--accent)", w: 1.7, pts: CMP.d.map((x, i) => [x, CMP.ratio[i]]) }] });
  lchart($("#chCor"), { height: 210, yfmt: v => nf(v, 2), series: [
    { name: "correlation", color: "var(--ink)", w: 1.5, pts: CMP.corr_d.map((x, i) => [x, CMP.corr63[i]]) },
    { name: "beta", color: "var(--warn)", w: 1.5, pts: CMP.corr_d.map((x, i) => [x, CMP.beta63[i]]) }] });
  wrapTables();
}

window.addEventListener("resize", () => { clearTimeout(window._mrz); window._mrz = setTimeout(() => V && !V.building && drawMap(), 200); });
load();
