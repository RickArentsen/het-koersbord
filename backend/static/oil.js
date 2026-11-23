/* ==========================================================================
   OIL — the oil market, MKT's third page.

   Twelve parts load in parallel from /api/pf/oil/{part} and each renders the
   moment it lands; a slow one (UN Comtrade, the first PortWatch read) never
   holds up the rest. The map (oilmap.js) has three feeds of its own: its
   layers, the offshore platforms (minutes the first time, then a month in the
   cache), and live tankers. While the page is in view the tankers refresh
   every minute, the price tiles every five and the map's layers every thirty;
   ⤢ Full tab gives the map the whole tab.

   The formatting, the controls and the line chart come from kit.js.
   ========================================================================== */

const PARTS = ["prices", "curve", "cracks", "flows", "supply", "opec", "stocks", "trade", "pump", "book", "news", "calendar"];
const D = {};
const SECTIONS = [
  ["s-sum", "Summary"], ["s-map", "Map"], ["s-prices", "Prices"], ["s-curve", "Curve"], ["s-refine", "Refining"],
  ["s-flows", "Flows"], ["s-supply", "Supply"], ["s-opec", "OPEC+"], ["s-stocks", "Stocks"],
  ["s-trade", "Who trades"], ["s-pump", "Pump prices"], ["s-book", "Your portfolio"], ["s-note", "Claude"],
  ["s-news", "News"], ["s-src", "Sources"],
];
const SEC_OF = { prices: "s-prices", curve: "s-curve", refine: "s-refine", flows: "s-flows", supply: "s-supply",
  opec: "s-opec", stocks: "s-stocks", trade: "s-trade", pump: "s-pump", book: "s-book" };
const SEC_NAME = { prices: "Prices", curve: "Curve", refine: "Refining", flows: "Flows", supply: "Supply",
  opec: "OPEC+", stocks: "Stocks", trade: "Who trades", pump: "Pump prices", book: "Your book" };

/* ---------- formatting ---------------------------------------------------- */
const fx = (v, d = 0) => v == null || isNaN(v) ? "–" :
  Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
const sg = (v, d = 0) => v == null || isNaN(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + fx(Math.abs(v), d);
const sgc = (v, d = 0, suf = "") => v == null ? `<span class="dim">–</span>` : `<span class="${sgn(v)}">${sg(v, d)}${suf}</span>`;
const usd = (v, d = 2) => v == null ? "–" : "$" + fx(v, d);
const eurl = v => v == null ? "–" : "€" + fx(v, 3);
const ymd = d => d ? dmy(d) : "–";
const monl = ym => ym ? mon(ym.length === 7 ? ym + "-01" : ym) : "–";
const days = d => d ? Math.round((Date.parse(d + "T00:00:00Z") - Date.now()) / 864e5) : null;

const failed = p => D[p] && D[p].error ?
  `<div class="note"><b>Could not load this part</b>${esc(D[p].error)}</div>` :
  `<div class="dim ld">Loading<span class="blink">_</span></div>`;
const ok = p => D[p] && !D[p].error;
const api = async (u, o) => { const r = await fetch(u, o); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status); return r.json(); };

/* a sparkline: [[x, y], ...] */
function spk(pts, o = {}) {
  const w = o.w || 92, h = o.h || 22, ys = (pts || []).map(p => p[1]).filter(v => v != null);
  if (ys.length < 2) return `<span class="dim">–</span>`;
  let lo = Math.min(...ys), hi = Math.max(...ys);
  if (o.zero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0); }
  if (hi === lo) { hi += 1; lo -= 1; }
  const n = pts.length, X = i => 1 + i / (n - 1) * (w - 2), Y = v => h - 2 - (v - lo) / (hi - lo) * (h - 4);
  let d = "";
  pts.forEach((p, i) => { if (p[1] != null) d += (d ? "L" : "M") + X(i).toFixed(1) + " " + Y(p[1]).toFixed(1); });
  const last = pts[n - 1][1];
  const col = o.color || (o.dir ? (last >= pts[0][1] ? "var(--gain)" : "var(--loss)") : "var(--accent)");
  return `<span class="spk"><svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">${o.zero && lo < 0 ? `<line x1="0" x2="${w}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--rule-strong)" stroke-width="1"/>` : ""}
    <path d="${d}" fill="none" stroke="${col}" stroke-width="1.3"/><circle cx="${X(n - 1)}" cy="${Y(last)}" r="1.8" fill="${col}"/></svg></span>`;
}

function stClass(s) {
  return { "shut": "shut", "severely reduced": "severe", "reduced": "reduced", "normal": "normal",
    "busier than a year ago": "busy" }[s] || "";
}

/* the Markdown Claude writes (the same small renderer as ALRT and ERN) */
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
  for (let i = 0; i < lines.length; i++) {
    const l = lines[i];
    if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|[\s:\-|]+\|\s*$/.test(lines[i + 1])) {
      flush(); endList();
      const cells = r => r.trim().replace(/^\||\|$/g, "").split("|").map(c => c.trim());
      out += `<div class="tscroll"><table><thead><tr>${cells(l).map(c => `<th>${mdInline(c)}</th>`).join("")}</tr></thead><tbody>`;
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { out += `<tr>${cells(lines[i]).map(c => `<td>${mdInline(c)}</td>`).join("")}</tr>`; i++; }
      out += `</tbody></table></div>`; i--; continue;
    }
    let m;
    if ((m = l.match(/^(#{1,3})\s+(.*)$/))) { flush(); endList(); out += `<h${m[1].length}>${mdInline(m[2])}</h${m[1].length}>`; continue; }
    if ((m = l.match(/^\s*[-*]\s+(.*)$/))) { flush(); if (list !== "ul") { endList(); out += "<ul>"; list = "ul"; } out += `<li>${mdInline(m[1])}</li>`; continue; }
    if ((m = l.match(/^\s*\d+[.)]\s+(.*)$/))) { flush(); if (list !== "ol") { endList(); out += "<ol>"; list = "ol"; } out += `<li>${mdInline(m[1])}</li>`; continue; }
    if ((m = l.match(/^&gt;\s?(.*)$/))) { flush(); endList(); out += `<blockquote>${mdInline(m[1])}</blockquote>`; continue; }
    if (!l.trim()) { flush(); endList(); continue; }
    if (list) endList();
    para.push(l.trim());
  }
  flush(); endList();
  return out;
}

/* ==========================================================================
   scaffold
   ========================================================================== */
$("#shell").innerHTML = termBar({ here: "MKT · OIL", right: `<button class="tbtn" id="reloadBtn">Reload</button>` });
wireShell();

const HOW = {
  curve: `<p>Each line is the price of oil for delivery in a later month, as traded today (or a week, a month, three months, a year ago).
    <b>Downward-sloping</b> (backwardation) means oil now is worth more than oil later: buyers pay up for barrels they can have
    at once, which happens when stocks are short. <b>Upward-sloping</b> (contango) is the opposite: there is oil to spare and it
    pays to store it. The spread between the first month and the month a year out is the cleanest single number for how tight
    the market is.</p><p>Month labels are today's; an older line is plotted by months ahead, so its own delivery months are in the tooltip.</p>`,
  refine: `<p>A crack spread is what a refinery earns for turning crude into products, before its own costs: the product price
    (dollars a gallon, times 42 to a barrel) less the crude price. Diesel over Brent is the crack Europe feels most; the 3-2-1
    is the US benchmark. These come from the continuous front-month futures, so a day when one contract rolls and the other has
    not can jump; read the trend, not one day.</p>`,
  flows: `<p><b>Chokepoints:</b> IMF PortWatch counts the ships that pass each strait or canal every day, from satellite AIS, and
    says what kind each is. The comparison is with the same week a year earlier, so the seasons cancel out.</p>
    <p><b>Loadings:</b> for every port, PortWatch estimates the cargo loaded and discharged from how deep each ship sits in the
    water before and after its call. "Tanker" is every tanker — crude, products, chemicals, LPG and LNG (Qatar's figure is mostly
    LNG) — and barrels are converted at 7.33 a tonne, the factor for crude, so they are a guide, not a count.</p>
    <p><b>The blind spot:</b> a ship that switches its transponder off is not counted. Around a war zone, and in the shadow fleet,
    that is common: compare a Gulf producer's loadings here with its official monthly exports under Supply before reading much
    into either.</p>`,
  supply: `<p>One number per country, with its source: the <b>EIA</b>'s crude figures for OPEC+ members and the US (consistent across
    the group and the only ones for Iran, Iraq and Kuwait's peers that report nothing), <b>JODI</b> (the governments' own monthly
    figures) for everyone else who reports, and the EIA's <b>all liquids</b> (crude plus condensate, gas liquids and biofuels)
    where neither has crude — so the UAE, Qatar and Brazil read high against the rest. The EIA's outlook runs past today: the
    dashed part of the world chart is its forecast.</p>`,
  opec: `<p>OPEC+ is OPEC's members plus ten other producers (Russia, Kazakhstan, Oman, Mexico and others) under the Declaration of
    Cooperation. A smaller group of them took extra, voluntary cuts in 2023 and meets monthly to decide how much of those to return;
    that group's required production is the target column. The EIA's spare capacity is oil a member could add within 30 days and
    hold for 90; oil that cannot reach the sea does not count.</p>`,
  stocks: `<p>The EIA's weekly survey of US stocks, published Wednesdays at 16:30 Amsterdam time. Each line is set against the range
    of the previous five years for the same week of the year, which is how the market reads it: a draw in a week when stocks
    usually build is the bullish surprise, not the draw itself.</p>`,
  trade: `<p><b>Positioning:</b> every Tuesday the CFTC (and ICE, for Brent and gasoil) count the futures held by each kind of trader.
    <b>Managed money</b> is hedge funds and trend-followers: their net position swings with the price and stretches at turns.
    <b>Producers and merchants</b> hedge real oil (producers sell forward, refiners buy). <b>Swap dealers</b> are banks on the
    other side of clients' swaps. Published Fridays at 21:30.</p>
    <p><b>Trade:</b> UN Comtrade's customs data, as the importers report it (the big exporters are slow to report or silent). A year
    behind and annual, so it shows the shape of the trade, not this month's.</p>`,
  pump: `<p>The European Commission's Weekly Oil Bulletin: what a litre of Euro 95 and of diesel costs at the pump in every member
    state each Monday, with all taxes, and without them. The tax share is the gap. The UK and the US are converted to euros at
    the day's exchange rate.</p>`,
  book: `<p>How each holding has moved with Brent, from two years of overlapping five-day returns rather than daily ones: shares close in
    New York or Amsterdam and Dated Brent is assessed in London, and one day's returns of the two barely overlap. A beta of 0.3
    means 0.3% for each 1% in Brent, on average; R² says how much of the holding's movement that explains. History, not a
    forecast, and not advice.</p>`,
};

function scaffold() {
  const P = (id, title, hint, body) => `<div class="panel" id="${id}"><header><h2>${title}</h2>
      <span class="hint">${hint}</span></header><div class="body">${body}</div></div>`;
  const how = (k, label = "How to read it") => `<details class="howto"><summary>${label}</summary>${HOW[k]}</details>`;
  $("#app").innerHTML = `
  <nav class="mtabs"><a href="/m/MKT">Overview</a><a href="/m/MKT/bonds">Bonds &amp; rates</a><a class="on" href="/m/MKT/oil">Oil</a></nav>
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>OIL</h1><span class="sub" id="topSub">prices · where oil comes from and where it goes · OPEC+ · stocks · who trades it · what it means for you</span></div>
    <div class="tiles" id="tiles">${failed("prices")}</div>
  </div></div>
  <nav class="secnav" id="secnav">${SECTIONS.map(([id, t]) => `<a href="#${id}" data-id="${id}">${t}</a>`).join("")}</nav>
  ${P("s-sum", "What matters now", "read from every panel below — click one to jump there",
      `<div class="two"><div><div class="insl" id="insBox"><div class="dim ld">Reading the panels<span class="blink">_</span></div></div></div>
       <div><div class="grp" style="margin-top:0">Coming up</div><div id="coming"></div>
            <div class="grp">Your oil alerts</div><div id="alerts"><div class="dim">…</div></div></div></div>`)}
  <div class="panel" id="s-map"><header><h2>Where the oil is</h2>
      <span class="hint">chokepoints, ports, pipelines, platforms and live tankers — drag, scroll to zoom, ◐ for the globe</span>
      <span id="mapQuote"></span>
      <button class="tbtn" id="mapFull" title="Fill the tab with the map and its panel; Esc to leave. /m/MKT/oil#globe opens the page like this.">⤢ Full tab</button></header>
    <div class="body"><div class="ctl" id="mapCtl"></div>
      <div class="og"><div class="mapcol"><div class="mapwrap flat" id="mapWrap"><div id="mapbox"></div><div class="map-leg" id="mapLeg"></div></div>
        <div class="map-status" id="mapStatus">Loading the map…</div></div>
      <div class="rail" id="mapRail"></div></div></div></div>
  ${P("s-prices", "Prices", "Brent, WTI, Dubai and the OPEC basket since the 1980s", `<div id="b-prices">${failed("prices")}</div>`)}
  ${P("s-curve", "The futures curve", "what oil for later delivery costs today", `<div id="b-curve">${failed("curve")}</div>${how("curve")}`)}
  ${P("s-refine", "Refining margins", "what a refinery earns: the products over crude", `<div id="b-refine">${failed("cracks")}</div>${how("refine")}`)}
  ${P("s-flows", "How the oil is moving", "tankers through the chokepoints, and what each country loads (IMF PortWatch, about a week behind)",
      `<div id="b-flows">${failed("flows")}</div>${how("flows", "How these are measured, and the blind spot")}`)}
  ${P("s-supply", "Where it comes from", "production by country, monthly — click a country", `<div id="b-supply">${failed("supply")}</div>${how("supply", "Where each number comes from")}`)}
  ${P("s-opec", "OPEC+", "what the group decided, what each member produces against its target", `<div id="b-opec">${failed("opec")}</div>${how("opec", "OPEC, OPEC+ and the voluntary group")}`)}
  ${P("s-stocks", "Stocks", "the EIA's weekly US survey, against the last five years", `<div id="b-stocks">${failed("stocks")}</div>${how("stocks")}`)}
  ${P("s-trade", "Who trades oil", "futures positioning, who buys whose crude, and who moves the barrels", `<div id="b-trade">${failed("trade")}</div>${how("trade")}`)}
  ${P("s-pump", "Pump prices", "Euro 95 and diesel in every EU country, the UK and the US — and Brent in other currencies", `<div id="b-pump">${failed("pump")}</div>${how("pump")}`)}
  ${P("s-book", "Oil and your portfolio", "how your holdings move with Brent, and the oil shares", `<div id="b-book">${failed("book")}</div>${how("book", "How this is measured")}`)}
  ${P("s-note", "Claude's note", "a one-page read of everything above, when you ask for it", `<div id="b-note"><div class="dim ld">…</div></div>`)}
  ${P("s-news", "Oil news", "OilPrice.com, the EIA, and your own feed filtered for oil", `<div id="b-news">${failed("news")}</div>`)}
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
    const r = await fetch(`/api/pf/oil/${p}`);
    D[p] = r.ok ? await r.json() : { error: `HTTP ${r.status}` };
  } catch (e) { D[p] = { error: String(e.message || e) }; }
  try { (RENDER[p] || (() => { }))(); } catch (e) { console.error(p, e); }
  wrapTables();
  renderSummary();
  if (PARTS.every(x => D[x])) renderSources();
}

let MAP_ON = false;
function load() {
  PARTS.forEach(p => delete D[p]);
  document.body.classList.remove("mapfull");
  scaffold();
  PARTS.forEach(fetchPart);
  LAST.prices = Date.now();
  initMap();
  renderAlerts();
  renderNote();
  const m = location.hash.match(/^#c=([A-Za-z]{2})$/);
  if (m) openCountry(m[1].toUpperCase());
}
$("#reloadBtn").onclick = load;

/* ==========================================================================
   header tiles
   ========================================================================== */
const UNIT = { "$/bbl": v => usd(v), "$/gal": v => usd(v, 3), "€/bbl": v => "€" + fx(v, 2), "€/MWh": v => "€" + fx(v, 2), "": v => fx(v, 1) };

function renderTop() {
  if (!ok("prices")) { if (D.prices) $("#tiles").innerHTML = failed("prices"); return; }
  const P = D.prices;
  $("#tiles").innerHTML = P.tiles.map(t => {
    const f = UNIT[t.unit] || (v => fx(v, 2));
    const chg = t.chg == null ? null : `<span class="${sgn(t.chg)}">${sg(t.chg, t.unit === "$/gal" ? 3 : 2)}${t.pct != null ? ` · ${pc(t.pct, 1)}` : ""}</span>`;
    const sp = (P.spark || {})[t.k];
    return tile(t.name, f(t.last), chg,
      `${esc(t.sub || "")}${t.asof ? ` · ${dm(t.asof)}` : ""}${sp && sp.length > 5 ? `<div style="margin-top:.2rem">${spk(sp, { w: 120, h: 18, dir: true })}</div>` : ""}`,
      !!t.lead, `${t.name}: ${t.unit}`);
  }).join("");
  $("#topSub").textContent = `prices about ten minutes late (Yahoo) · Dated Brent and spot a day or two late (EIA) · fetched ${P.live_age_s < 90 ? "just now" : Math.round(P.live_age_s / 60) + " min ago"}, again every five minutes`;
  mapQuote();
}

/* ==========================================================================
   summary, the calendar, alerts
   ========================================================================== */
function renderSummary() {
  const order = ["flows", "prices", "curve", "cracks", "supply", "opec", "stocks", "pump", "trade", "book"];
  const ic = { warn: "!", good: "▲", bad: "▼", info: "●" };
  const all = [];
  for (const p of order) if (ok(p)) for (const i of D[p].insights || []) all.push(i);
  const loading = order.filter(p => !D[p]).length;
  $("#insBox").innerHTML = all.map(i => `<a class="in ${esc(i.tone)}" href="#${SEC_OF[i.sec] || "s-prices"}">
    <span class="it"><i>${ic[i.tone] || "●"}</i>${esc(SEC_NAME[i.sec] || i.sec)}</span><span class="ix">${esc(i.text)}</span></a>`).join("")
    + (loading ? `<div class="dim" style="font-size:.66rem;padding:.4rem 0">${loading} panel(s) still loading<span class="blink">_</span></div>` : "")
    || `<div class="dim">Nothing yet.</div>`;
  renderComing();
}

function renderComing() {
  const ev = ok("calendar") ? D.calendar.events || [] : [];
  $("#coming").innerHTML = ev.map(e => `<div class="cm"><span class="d">${dmy(e.d)}${e.time ? " " + esc(e.time) : ""}</span>
    <span><span class="dim">${esc(e.k)}</span> ${esc(e.t)}${days(e.d) != null && days(e.d) >= 0 ? ` <span class="dim">· in ${days(e.d)} d</span>` : ""}</span></div>`).join("")
    || `<div class="dim" style="font-size:.7rem">${D.calendar ? "Nothing scheduled that we know of." : "Loading…"}</div>`;
}

const PSEUDO = { brent: "BRENT", wti: "WTI", hormuz: "HORMUZ", nl_diesel: "NL-DIESEL" };
async function renderAlerts() {
  let A;
  try { A = await api("/api/pf/oil/alerts"); } catch (e) { $("#alerts").innerHTML = `<div class="dim">${esc(e.message)}</div>`; return; }
  const kinds = A.kinds || {}, vals = A.values || {};
  const KEY = { oil_brent_above: "brent", oil_brent_below: "brent", oil_wti_above: "wti", oil_wti_below: "wti",
    oil_hormuz_above: "hormuz", oil_hormuz_below: "hormuz", oil_nldiesel_above: "nl_diesel", oil_nldiesel_below: "nl_diesel" };
  const now = k => { const v = vals[KEY[k]]; if (!v) return "no data yet"; const u = kinds[k].unit; return (u ? u : "") + fx(v.v, u === "€" ? 3 : u ? 2 : 1); };
  const fired = r => { const v = vals[KEY[r.kind]]; if (!v) return false; return kinds[r.kind].dir === "above" ? v.v > r.value : v.v < r.value; };
  $("#alerts").innerHTML = `<div class="rows">${(A.rules || []).map(r => `<div class="row">
      <span class="nm">${esc(kinds[r.kind]?.label || r.kind)} <b>${esc(kinds[r.kind]?.dir || "")} ${fx(r.value, 2)}</b>${r.note ? ` <span class="dim">${esc(r.note)}</span>` : ""}</span>
      <span class="px ${fired(r) ? "warnc" : "dim"}">${fired(r) ? "firing" : "quiet"} · now ${now(r.kind)}</span>
      <span class="ch"><a href="#" data-del="${r.id}" style="color:var(--loss)">delete</a></span></div>`).join("")
    || `<div class="dim" style="font-size:.7rem">None yet. They show on the home screen with your other alerts.</div>`}</div>
    <div class="alrt" style="margin-top:.45rem">
      <select id="aKind">${Object.entries(kinds).map(([k, v]) => `<option value="${k}">${esc(v.label)} ${v.dir}</option>`).join("")}</select>
      <input id="aVal" type="number" step="any" placeholder="value">
      <input id="aNote" class="nt" placeholder="note (optional)">
      <button class="tbtn" id="aAdd">Add</button><span class="dim" id="aNow"></span></div>`;
  const showNow = () => { const k = $("#aKind").value; $("#aNow").textContent = k ? "now " + now(k) : ""; };
  $("#aKind").onchange = showNow; showNow();
  $("#aAdd").onclick = async () => {
    const kind = $("#aKind").value, value = parseFloat($("#aVal").value);
    if (isNaN(value)) { flash("An alert needs a number."); return; }
    try {
      await api("/api/pf/alerts/rules", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker: PSEUDO[KEY[kind]], kind, value, note: $("#aNote").value.trim() }) });
      renderAlerts();
    } catch (e) { flash(esc(e.message)); }
  };
  $$("#alerts [data-del]").forEach(a => a.onclick = async e => {
    e.preventDefault();
    await fetch("/api/pf/alerts/rules/" + a.dataset.del, { method: "DELETE" });
    renderAlerts();
  });
}

/* ==========================================================================
   the map
   ========================================================================== */
const MAPL = [["shade", "Production", "#2b6f8f"], ["choke", "Chokepoints", "#3fb380"], ["ports", "Ports", "#d9a441"],
  ["pipes", "Pipelines", "#e0644f"], ["plat", "Platforms", "#c79a52"], ["tank", "Live tankers", "#e6eef1"]];
const MST = { on: MAPL.map(l => l[0]), shade: "kbd" };
const MDATA = { map: null, plat: null, tank: null };

/* What refreshes by itself while the page is open and in view: the live
   tankers every minute, the price tiles every five minutes, the map's layers
   every half hour (the server re-reads PortWatch when its copy is eight hours
   old). A tab that comes back into view catches up at once. */
const EVERY = { tank: 60e3, prices: 5 * 60e3, map: 30 * 60e3 };
const LAST = { tank: 0, prices: 0, map: 0 };
let TICK = 0;
function tick() {
  if (document.hidden || !TICK) return;
  const now = Date.now();
  if (now - LAST.tank >= EVERY.tank) loadTankers();
  if (now - LAST.prices >= EVERY.prices) refreshPrices();
  if (now - LAST.map >= EVERY.map) loadMapData();
  mapStatus();
}
document.addEventListener("visibilitychange", tick);

async function refreshPrices() {
  LAST.prices = Date.now();
  try {
    const r = await api("/api/pf/oil/prices");
    if (r && !r.error) { D.prices = r; renderTop(); renderSummary(); }
  } catch { }
}

async function initMap() {
  if (!MAP_ON) { MAP_ON = true; }
  $("#mapCtl").innerHTML = `${chips("mLayers", MAPL.map(([k, n, c]) => ({ k, name: n, color: c })), MST.on)}
    <span class="grp" id="mShade"><button class="seg ${MST.shade === "kbd" ? "on" : ""}" data-v="kbd">output</button><button class="seg ${MST.shade === "d12" ? "on" : ""}" data-v="d12">change on a year</button></span>`;
  wireChips("mLayers", MST.on, () => OilMap.layers(Object.fromEntries(MAPL.map(([k]) => [k, MST.on.includes(k)]))));
  $$("#mShade .seg").forEach(b => b.onclick = () => { $$("#mShade .seg").forEach(x => x.classList.toggle("on", x === b)); MST.shade = b.dataset.v; OilMap.shade(MST.shade); });
  $("#mapFull").onclick = () => mapFull();
  railHome();
  await OilMap.init($("#mapbox"), { onSelect: mapSelect, legend: $("#mapLeg"),
    onMode: m => { const w = $("#mapWrap"); if (w) { w.classList.toggle("globe", m === "globe"); w.classList.toggle("flat", m !== "globe"); } } });
  loadMapData(); loadPlatforms(); loadTankers();
  clearInterval(TICK);
  TICK = setInterval(tick, 30e3);
  mapHash();
}

/* the map on the whole tab: the map or globe, its legend and the panel beside it */
function mapFull(on) {
  const P = $("#s-map");
  if (!P) return;
  on = on == null ? !P.classList.contains("full") : !!on;
  P.classList.toggle("full", on);
  document.body.classList.toggle("mapfull", on);
  $("#mapFull").textContent = on ? "✕ Close" : "⤢ Full tab";
  OilMap.fill(on);
  mapQuote();
  if (!on) P.scrollIntoView({ block: "start" });
}

/* /m/MKT/oil#map opens the page with the map on the whole tab, #globe with the globe */
function mapHash() {
  const h = location.hash;
  if (h !== "#map" && h !== "#globe") return;
  mapFull(true);
  if (h === "#globe" && OilMap.state.mode !== "globe") OilMap.mode("globe");
}

/* the prices across the top of the full-tab view */
function mapQuote() {
  const Q = $("#mapQuote");
  if (!Q || !ok("prices")) return;
  const P = D.prices;
  const ts = ["BRENT", "WTI", "DATED"].map(k => (P.tiles || []).find(t => t.k === k)).filter(Boolean);
  const hz = ((MDATA.map && MDATA.map.chokepoints) || []).find(c => c.name === "Strait of Hormuz");
  Q.innerHTML = ts.map(t => `${esc(t.name)} <b>${(UNIT[t.unit] || (v => fx(v, 2)))(t.last)}</b> ${t.pct != null ? `<span class="${sgn(t.pct)}">${pc(t.pct, 1)}</span>` : ""}`).join(" · ")
    + (hz ? ` · Hormuz <b>${fx(hz.w7, 1)}</b> tankers a day <span class="dim">(${fx(hz.y7, 1)} a year ago)</span>` : "")
    + (P.live_age_s != null ? ` <span class="dim">· prices ${P.live_age_s < 90 ? "just fetched" : "fetched " + Math.round(P.live_age_s / 60) + " min ago"}, about ten minutes late</span>` : "");
}

async function loadMapData() {
  LAST.map = Date.now();
  let m;
  try { m = await api("/api/pf/oil/map"); } catch (e) { m = { error: e.message }; }
  if (m.error && MDATA.map && !MDATA.map.error) { MDATA.map.refresh_error = m.error; mapStatus(); return; }   // keep what is drawn
  MDATA.map = m;
  MDATA.map.checked = Date.now();
  OilMap.set("map", MDATA.map);
  if (!RAIL.sel) railHome();
  mapStatus();
  mapQuote();
}

async function loadPlatforms() {
  let p;
  try { p = await api("/api/pf/oil/platforms"); } catch (e) { p = { error: e.message }; }
  MDATA.plat = p;
  if (p.p) OilMap.set("platforms", p.p);
  if (p.loading || p.refreshing) setTimeout(loadPlatforms, p.p ? 300000 : 25000);
  mapStatus();
}

async function loadTankers() {
  LAST.tank = Date.now();
  let t;
  try { t = await api("/api/pf/oil/tankers"); } catch (e) { t = { error: e.message }; }
  t.checked = Date.now();
  MDATA.tank = t;
  const rows = [];
  for (const x of (t.baltic && t.baltic.tankers) || []) { x.src = "Digitraffic (Baltic)"; rows.push(x); }
  const seen = new Set(rows.map(x => x[8]));
  for (const x of (t.world && t.world.tankers) || []) { if (seen.has(x[8])) continue; x.src = "aisstream.io"; rows.push(x); }
  OilMap.set("tankers", rows);
  mapStatus();
}

/* how old each layer is, in words */
const agoS = s => s == null ? "–" : s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 2 * 86400 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago";
const hm = ms => new Date(ms).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
const behind = d => d ? Math.max(0, Math.round((Date.now() - Date.parse(d + "T00:00:00Z")) / 864e5)) : null;

function mapStatus() {
  const parts = [];
  const m = MDATA.map, p = MDATA.plat, t = MDATA.tank;
  if (m && !m.error) {
    const b = behind(m.flows_latest);
    parts.push(`Chokepoints and ports: IMF PortWatch, counted to ${ymd(m.flows_latest)}${b != null ? ` (${b} days behind)` : ""}, checked ${hm(m.checked)}`);
    const pm = Object.values(m.production || {}).map(r => r.month).filter(Boolean).sort().pop();
    if (pm) parts.push(`production shading: latest month ${monl(pm)}`);
  }
  if (m && m.error) parts.push(`<span class="warnc">the map's layers did not load: ${esc(m.error)}</span>`);
  if (m && m.refresh_error) parts.push(`<span class="warnc">last refresh failed (${esc(m.refresh_error)}); showing ${hm(m.checked)}'s</span>`);
  if (m && m.errors && Object.keys(m.errors).length) parts.push(`<span class="warnc">${esc(Object.keys(m.errors).join(", "))} did not load</span>`);
  if (p) {
    if (p.p) parts.push(`${fx(p.p.length)} platforms: ${fx(p.n_osm)} from OpenStreetMap (© contributors, ODbL), ${fx(p.n_sodir)} from Norway's Sodir, fetched ${p.fetched_at ? dmy(new Date(p.fetched_at * 1000).toISOString().slice(0, 10)) : agoS(p._age_s)}`
      + (p.refreshing ? " · refreshing" : ""));
    else if (p.loading) parts.push(`Platforms: fetching from OpenStreetMap — this takes a few minutes the first time${p.error ? ` <span class="warnc">(${esc(p.error)})</span>` : ""}`);
  }
  if (t) {
    const b = t.baltic || {}, w = t.world || {};
    parts.push(`Live tankers, updated ${t.checked ? hm(t.checked) : "–"} and every minute: ${fx((b.tankers || []).length)} in the Baltic (Finland's Digitraffic)${b.error ? ` <span class="warnc">${esc(b.error)}</span>` : ""}`);
    if (!w.has_key) parts.push(`worldwide is off — create a free key at <a href="https://aisstream.io" target="_blank" rel="noopener">aisstream.io</a> and paste it in Settings on the <a href="/">home screen</a>`);
    else parts.push(`worldwide (aisstream.io): <b>${esc(w.state)}</b>${w.state === "streaming" ? ` · ${fx((w.tankers || []).length)} tankers placed, ${fx(w.known_tankers)} identified, ${fx(w.rate, 1)} messages a second` : ""}${w.error ? ` <span class="warnc">${esc(w.error)}</span>` : ""}${w.state !== "streaming" && w.detail ? ` · ${esc(w.detail)}` : ""}`);
  }
  const el = $("#mapStatus");
  if (el) el.innerHTML = parts.join(" · ") + ` · Map: ${esc(OilMap.landSource() || "Natural Earth")}`;
}

/* The panel beside the map: with nothing clicked, the oil chokepoints now;
   a click shows that one thing, with a way back. */
const RAIL = { sel: null };
const shortCp = n => n.replace(/^Strait of /, "").replace(/ Strait$/, "");

function railHome() {
  RAIL.sel = null;
  const R = $("#mapRail");
  if (!R) return;
  const m = MDATA.map;
  const cps = ((m && m.chokepoints) || []).filter(c => c.oil).sort((a, b) => (b.y7 || 0) - (a.y7 || 0));
  R.innerHTML = `<div class="dim" style="font-size:.72rem;line-height:1.55">Click a chokepoint, a port, a pipeline, a tanker or a
      country. Countries are shaded by how much crude they produce; hover for the number.</div>
    ${cps.length ? `<div class="grp" style="margin-top:.7rem">Oil chokepoints: tankers a day, 7 days to ${ymd(m.flows_latest)}</div>
      <table class="tbl"><thead><tr><th>Where</th><th>Now</th><th>A year ago</th><th>Change</th></tr></thead><tbody>
      ${cps.map(c => `<tr class="click" data-rcp="${esc(c.id)}" title="${esc(c.status || "")}"><td>${esc(shortCp(c.name))}</td><td><b>${fx(c.w7, 1)}</b></td><td>${fx(c.y7, 1)}</td>
        <td><span class="st ${stClass(c.status)}" style="font-size:inherit;letter-spacing:0">${sg(c.vs_y7, 0)}%</span></td></tr>`).join("")}</tbody></table>
      <div class="src">The change is coloured like the map: <span class="st normal">normal</span> <span class="st busy">busier</span> <span class="st reduced">reduced</span>
        <span class="st severe">severely reduced</span> <span class="st shut">shut</span>. Click one to find it on the map. All tankers, crude and products, from satellite AIS.</div>` : ""}`;
  R.querySelectorAll("[data-rcp]").forEach(tr => tr.onclick = () => {
    const c = cps.find(x => x.id === tr.dataset.rcp);
    if (!c) return;
    OilMap.focus(c.lon, c.lat, 5);
    OilMap.select("choke:" + c.id);
    mapSelect("choke", c);
  });
  wrapTables();
}

function mapSelect(kind, it) {
  const R = $("#mapRail");
  const kv = rows => `<div class="kv">${rows.map(([k, v]) => `<span>${esc(k)}</span><span>${v}</span>`).join("")}</div>`;
  if (kind === "country") { openCountry(it.iso); return; }
  RAIL.sel = kind;
  const BACK = `<a href="#" class="back" data-back>← the oil chokepoints</a>`;
  if (kind === "choke") {
    R.innerHTML = BACK + `<h3>${esc(it.name)}</h3>${kv([["Tankers a day", `${fx(it.w7, 1)} <span class="dim">7 days to ${ymd(it.end)}</span>`],
      ["Same week last year", fx(it.y7, 1)], ["Change", sgc(it.vs_y7, 0, "%")], ["Status", `<span class="st ${stClass(it.status)}">${esc(it.status)}</span>`]])}
      ${it.note ? `<p class="howto">${esc(it.note)}</p>` : ""}
      <div class="hd-links"><a href="#s-flows" data-cp="${esc(it.id)}">its traffic, every week →</a>${it.geo ? `<a href="/m/GEO">on GEO →</a>` : ""}</div>`;
    return;
  }
  if (kind === "port") {
    R.innerHTML = BACK + `<h3>${esc(it.name)}</h3>${kv([["Country", esc(it.country || it.iso3)],
      ["Loaded", `${fx(it.exp / 1000, 1)} kt a day <span class="dim">≈ ${fx(it.exp * 7.33 / 1000)} kb/d as crude</span>`],
      ["Discharged", `${fx(it.imp / 1000, 1)} kt a day`], ["Tanker calls", `${it.calls} in ${MDATA.map?.ports_window?.days || 30} days`]])}
      <p class="howto">All tanker cargo, estimated by IMF PortWatch from AIS, ${ymd(MDATA.map?.ports_window?.since)} to ${ymd(MDATA.map?.ports_window?.latest)}.</p>
      ${it.iso ? `<div class="hd-links"><a href="#" data-iso="${esc(it.iso)}">${esc(it.country)} in depth →</a></div>` : ""}`;
    const a = R.querySelector("[data-iso]"); if (a) a.onclick = e => { e.preventDefault(); openCountry(a.dataset.iso); };
    return;
  }
  if (kind === "pipe") {
    R.innerHTML = BACK + `<h3>${esc(it.name)}</h3>${kv([["From", esc(it.from)], ["To", esc(it.to)], ["Capacity", esc(it.cap || "–")]])}
      ${it.bypass ? `<p class="howto"><b>Avoids ${esc(it.bypass)}.</b> Oil that goes this way does not need the strait.</p>` : ""}
      <p class="howto">The route is schematic: a few points along the real line, not a survey.</p>`;
    return;
  }
  if (kind === "tank") {
    const t = it;
    R.innerHTML = BACK + `<h3>${esc(t[4] || "Tanker")}</h3>${kv([["Destination", esc(t[5] || "–")], ["Speed", t[2] != null ? fx(t[2], 1) + " knots" : "–"],
      ["Course", t[3] != null ? fx(t[3]) + "°" : "–"], ["Draught", t[6] ? fx(t[6], 1) + " m" : "–"], ["AIS type", t[7] || "–"],
      ["MMSI", t[8]], ["Seen", t[9] < 120 ? t[9] + " seconds ago" : Math.round(t[9] / 60) + " minutes ago"], ["Source", esc(t.src || "")]])}
      <p class="howto">The destination is what the crew typed into the transponder; "FOR ORDER" means the cargo has not been sold on yet.</p>`;
    return;
  }
  if (kind === "plat") {
    R.innerHTML = BACK + `<h3>${esc(it[4] || "Offshore platform")}</h3>${kv([["Operator", esc(it[5] || "–")], ["Kind", esc(it[6] || it[3] || "–")],
      ["Where", `${fx(it[0], 3)}, ${fx(it[1], 3)}`], ["Source", it[2] === "sodir" ? "Sodir (Norway's official list)" : "OpenStreetMap"]])}`;
  }
}

document.addEventListener("click", e => {
  if (e.target.closest("[data-back]")) { e.preventDefault(); OilMap.select(null); railHome(); return; }
  const a = e.target.closest("[data-cp]");
  if (!a) return;
  if (document.getElementById("s-map").classList.contains("full")) mapFull(false);
  setTimeout(() => {
    const row = document.querySelector(`#cpTable tr[data-id="${a.dataset.cp}"]`);
    if (row) { row.classList.add("you"); row.scrollIntoView({ block: "center" }); setTimeout(() => row.classList.remove("you"), 3000); }
  }, 50);
});

/* ==========================================================================
   prices
   ========================================================================== */
const PSER = [["BRENT", "Brent (Dated)", COL.ink], ["WTI", "WTI", COL.s1], ["ORB", "OPEC basket", COL.s2], ["DUBAI", "Dubai (monthly)", COL.s3]];
const PST = { on: ["BRENT", "WTI"], range: 1826, ccy: "USD", real: false };

function renderPrices() {
  if (!ok("prices")) { if (D.prices) $("#b-prices").innerHTML = failed("prices"); return; }
  $("#b-prices").innerHTML = `<div class="ctl">${chips("pChips", PSER.map(([k, n, c]) => ({ k, name: n, color: c })), PST.on)}
      ${rangeBar("pRange", PST.range)}
      <span class="grp" id="pCcy"><button class="seg ${PST.ccy === "USD" ? "on" : ""}" data-v="USD">dollars</button><button class="seg ${PST.ccy === "EUR" ? "on" : ""}" data-v="EUR">euros</button></span>
      <span class="grp" id="pReal"><button class="seg ${PST.real ? "" : "on"}" data-v="0">as paid</button><button class="seg ${PST.real ? "on" : ""}" data-v="1">in today's money</button></span></div>
    <div class="lc" id="pChart"></div>
    <div class="src">Dated Brent and WTI at Cushing are the EIA's daily spot assessments (via FRED); the OPEC basket is OPEC's daily average of its members'
      crudes; Dubai is the IMF's monthly average. "In today's money" scales by US consumer prices (CPI-U) to the latest month published.</div>`;
  wireChips("pChips", PST.on, drawPrices);
  wireRange("pRange", r => { PST.range = r; drawPrices(); });
  $$("#pCcy .seg").forEach(b => b.onclick = () => { $$("#pCcy .seg").forEach(x => x.classList.toggle("on", x === b)); PST.ccy = b.dataset.v; drawPrices(); });
  $$("#pReal .seg").forEach(b => b.onclick = () => { $$("#pReal .seg").forEach(x => x.classList.toggle("on", x === b)); PST.real = b.dataset.v === "1"; drawPrices(); });
  drawPrices();
}

async function drawPrices() {
  const start = startOf(PST.range, "1986-01-01");
  let h;
  try { h = await api(`/api/pf/oil/history?keys=${PST.on.join(",")}&start=${start}&ccy=${PST.ccy}&real=${PST.real ? 1 : 0}`); }
  catch (e) { $("#pChart").innerHTML = `<div class="lc-empty">${esc(e.message)}</div>`; return; }
  const cur = PST.ccy === "EUR" ? "€" : "$";
  lchart($("#pChart"), {
    series: PSER.filter(([k]) => PST.on.includes(k)).map(([k, n, c]) => ({ name: n, color: c, pts: (h.series || {})[k] || [], w: k === "BRENT" ? 2 : 1.4 })),
    yfmt: v => cur + v.toFixed(0), tfmt: v => cur + v.toFixed(2), endLabels: true,
    label: "Oil prices", empty: "No prices for this range",
  });
}

/* ==========================================================================
   the curve
   ========================================================================== */
const CURVE_COL = { "Today": COL.ink, "1 week ago": COL.s4, "1 month ago": COL.s1, "3 months ago": COL.s3, "1 year ago": COL.s2 };
const CST = { root: "BZ" };

function renderCurve() {
  if (!ok("curve")) { if (D.curve) $("#b-curve").innerHTML = failed("curve"); return; }
  const R = D.curve.roots || {};
  const row = (lab, k) => `<tr><td>${lab}</td>${["BZ", "CL"].map(r => {
    const x = R[r]; if (!x) return "<td>–</td>";
    const v = x[k];
    return `<td>${v == null ? "–" : `<span class="${v > 0 ? "warnc" : "pos"}">${sg(v, 2)}</span>`}</td>`;
  }).join("")}</tr>`;
  $("#b-curve").innerHTML = `<div class="og"><div>
      <div class="ctl"><span class="grp" id="cRoot">${Object.entries(R).map(([k, v]) => `<button class="seg ${k === CST.root ? "on" : ""}" data-r="${k}">${esc(v.name)}</button>`).join("")}</span>
        <span class="mini" id="cAsof"></span></div>
      <div class="lc" id="cChart"></div></div>
    <div><table class="tbl"><thead><tr><th>Front month less…</th><th>Brent</th><th>WTI</th></tr></thead><tbody>
      ${row("the next month", "m1m2")}${row("six months later", "m1m7")}${row("a year later", "m1m13")}
      <tr><td class="dim">a year later, % of the front</td>${["BZ", "CL"].map(r => `<td class="dim">${R[r]?.m1m13_pct != null ? fx(R[r].m1m13_pct, 1) + "%" : "–"}</td>`).join("")}</tr>
      </tbody></table>
      <div class="mini" style="margin:.35rem 0 .2rem">positive = backwardation (oil now dearer than later), dollars a barrel</div>
      <div class="grp">The front month less a year later, every day</div><div class="lc" id="cSpread"></div>
      <div class="src" id="cKept"></div></div></div>`;
  $$("#cRoot .seg").forEach(b => b.onclick = () => { $$("#cRoot .seg").forEach(x => x.classList.toggle("on", x === b)); CST.root = b.dataset.r; drawCurve(); });
  drawCurve();
}

function drawCurve() {
  const R = (D.curve.roots || {})[CST.root];
  if (!R) return;
  const today = R.curves[0];
  // Points sit at their months ahead of each curve's own front month, so a
  // month Yahoo has no quote for leaves a gap instead of squeezing the axis.
  const mi = ym => { const [y, m] = ym.split("-").map(Number); return y * 12 + m - 1; };
  const lab = i => new Date(Date.UTC(Math.floor(i / 12), i % 12, 1)).toLocaleString("en-GB", { month: "short", timeZone: "UTC" }) + " " + String(Math.floor(i / 12)).slice(2);
  const t0 = today ? mi(today.pts[0].ym) : 0;
  const span = today ? mi(today.pts[today.pts.length - 1].ym) - t0 : 0;
  const cats = Array.from({ length: span + 1 }, (_, i) => lab(t0 + i));
  lchart($("#cChart"), {
    xType: "cat", cats, height: 290,
    series: R.curves.map(c => ({ name: `${c.label} (${dm(c.day)})`, short: c.label, color: CURVE_COL[c.label] || COL.mid,
      w: c.label === "Today" ? 2.4 : 1.3, dash: c.label === "1 year ago" ? "4 3" : "",
      pts: c.pts.map(p => [mi(p.ym) - mi(c.pts[0].ym), p.px, p.lab]).filter(p => p[0] <= span) })),
    yfmt: v => "$" + v.toFixed(0), tfmt: v => "$" + v.toFixed(2), label: "Futures curve",
  });
  $("#cAsof").innerHTML = `${esc(R.name)} settlements, ${dmy(R.asof)}${R.front ? ` · front month ${esc(R.front.lab)} expires ${dmy(R.front.expires)}` : ""}`;
  lchart($("#cSpread"), {
    height: 170, zero: true,
    series: [{ name: "front less a year later", color: COL.ink, pts: R.hist.m1m13 || [], w: 1.6 },
      { name: "front less next month", color: COL.s2, pts: R.hist.m1m2 || [], w: 1.1 }],
    yfmt: v => "$" + v.toFixed(0), tfmt: v => sg(v, 2), empty: "Kept from the first day this page ran",
  });
  $("#cKept").innerHTML = R.kept_from ? `The page keeps each day's curve, because Yahoo drops an expired month's history within weeks. Kept since ${dmy(R.kept_from)}.` : "";
}

/* ==========================================================================
   refining
   ========================================================================== */
const KSER = [["diesel", "Diesel over Brent", COL.s2], ["gasoline", "Gasoline over Brent", COL.s1], ["c321", "3-2-1 crack (WTI)", COL.ink]];
const KST = { on: ["diesel", "gasoline", "c321"], range: 730 };

function renderCracks() {
  if (!ok("cracks")) { if (D.cracks) $("#b-refine").innerHTML = failed("cracks"); return; }
  const K = D.cracks;
  $("#b-refine").innerHTML = `<table class="tbl"><thead><tr><th>Spread</th><th>Now</th><th>1 week</th><th>1 month</th><th>1 year</th>
      <th>5-year average</th><th>Percentile, 5 years</th></tr></thead><tbody>
      ${K.rows.map(r => `<tr><td>${esc(r.name)}<div class="mini">${esc(r.how)}</div></td><td><b>${usd(r.last)}</b></td>
        <td>${sgc(r.w1, 2)}</td><td>${sgc(r.m1, 2)}</td><td>${sgc(r.y1, 2)}</td><td>${usd(r.avg5)}</td>
        <td>${pbar(r.pctile)} ${r.pctile != null ? ord(r.pctile) : "–"}</td></tr>`).join("")}</tbody></table>
    <div class="ctl" style="margin-top:.6rem">${chips("kChips", KSER.map(([k, n, c]) => ({ k, name: n, color: c })), KST.on)}${rangeBar("kRange", KST.range, RANGES.slice(0, 7))}</div>
    <div class="lc" id="kChart"></div>
    <div class="src">Dollars a barrel, from Yahoo's continuous front-month futures: NY Harbor ULSD (diesel), RBOB gasoline, Brent and WTI.</div>`;
  wireChips("kChips", KST.on, drawCracks);
  wireRange("kRange", r => { KST.range = r; drawCracks(); });
  drawCracks();
}

function drawCracks() {
  const start = startOf(KST.range, "2019-01-01");
  lchart($("#kChart"), {
    series: KSER.filter(([k]) => KST.on.includes(k)).map(([k, n, c]) => ({ name: n, color: c,
      pts: (D.cracks.series[k] || []).filter(p => p[0] >= start), w: 1.5 })),
    yfmt: v => "$" + v.toFixed(0), tfmt: v => "$" + v.toFixed(2), endLabels: true, zero: true, label: "Crack spreads",
  });
}

/* ==========================================================================
   flows
   ========================================================================== */
const FST = { all: false };

function renderFlows() {
  if (!ok("flows")) { if (D.flows) $("#b-flows").innerHTML = failed("flows"); return; }
  const F = D.flows;
  const cps = F.chokepoints.filter(c => FST.all || c.oil);
  $("#b-flows").innerHTML = `<div class="grp" style="margin-top:0">Chokepoints <span class="dim">· tankers a day, the 7 days to ${ymd(F.latest)}</span>
      <a href="#" id="fAll" style="float:right;font-size:.62rem">${FST.all ? "only the oil routes" : `all ${F.chokepoints.length}`}</a></div>
    <table class="tbl" id="cpTable"><thead><tr><th>Chokepoint</th><th>Tankers a day</th><th>Same week last year</th><th>Change</th><th>Status</th>
      <th>Tanker capacity a day</th><th>Weekly, two years</th></tr></thead><tbody>
      ${cps.map(c => `<tr data-id="${esc(c.id)}" title="${esc(c.note || "")}"><td>${esc(c.name)}</td><td><b>${fx(c.w7, 1)}</b></td><td>${fx(c.y7, 1)}</td>
        <td>${sgc(c.vs_y7, 0, "%")}</td><td><span class="st ${stClass(c.status)}">${esc(c.status)}</span></td>
        <td>${c.dwt7 != null ? fx(c.dwt7 / 1e6, 2) + " Mt dwt" : "–"}</td><td class="spkc">${spk(c.spark, { w: 140, h: 22 })}</td></tr>`).join("")}</tbody></table>
    <div class="grp">What each country loads onto tankers <span class="dim">· click a country · the 30 days to ${ymd(F.pw_latest)}</span></div>
    <table class="tbl" id="ctTable"><thead><tr><th>Country</th><th>Loaded, kt a day</th><th>≈ kb/d as crude</th><th>vs the 30 days before</th>
      <th>vs a year ago</th><th>Discharged, kt a day</th><th>Loaded, weekly</th></tr></thead><tbody>
      ${F.countries.map(c => `<tr class="click" data-iso="${esc(c.iso || "")}"><td>${esc(c.name)}</td><td><b>${fx((c.exp.w30 || 0) / 1000, 1)}</b></td>
        <td>${fx(c.exp_kbd30)}</td><td>${sgc(c.exp.vs_p30, 0, "%")}</td><td>${sgc(c.exp.vs_y30, 0, "%")}</td>
        <td>${fx((c.imp.w30 || 0) / 1000, 1)}</td><td class="spkc">${spk(c.spark, { w: 140, h: 22 })}</td></tr>`).join("")}</tbody></table>`;
  $("#fAll").onclick = e => { e.preventDefault(); FST.all = !FST.all; renderFlows(); wrapTables(); };
  $$("#ctTable tr.click").forEach(tr => tr.onclick = () => tr.dataset.iso && openCountry(tr.dataset.iso));
}

/* ==========================================================================
   supply
   ========================================================================== */
const SST = { all: false };

function renderSupply() {
  if (!ok("supply")) { if (D.supply) $("#b-supply").innerHTML = failed("supply"); return; }
  const S = D.supply;
  const rows = SST.all ? S.rows : S.rows.slice(0, 25);
  $("#b-supply").innerHTML = `<div class="og"><div>
      <div class="grp" style="margin-top:0">World crude production, million barrels a day <span class="dim">· EIA, ${esc(S.edition || "")} outlook; dashed is its forecast</span></div>
      <div class="lc" id="sWorld"></div></div>
    <div><div class="grp" style="margin-top:0">Supply and demand, all liquids <span class="dim">· mb/d</span></div><div class="lc" id="sBal"></div>
      <div class="src">A positive stock draw means the world used more than it produced, and stocks paid the difference.</div></div></div>
    <div class="grp">Producers <span class="dim">· kb/d · click a country for its exports, ports and buyers</span>
      <a href="#" id="sAll" style="float:right;font-size:.62rem">${SST.all ? "top 25" : `all ${S.rows.length}`}</a></div>
    <table class="tbl"><thead><tr><th>Country</th><th></th><th>Output</th><th>Month</th><th>1 month</th><th>6 months</th><th>12 months</th>
      <th>Share of world</th><th>Exports</th><th>Source</th><th>Two years</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="click" data-iso="${esc(r.iso)}"><td>${esc(r.name)}</td><td class="mini">${esc(r.group || "")}</td>
        <td><b>${fx(r.kbd)}</b></td><td class="dim">${monl(r.month)}</td><td>${sgc(r.d1)}</td><td>${sgc(r.d6)}</td><td>${sgc(r.d12)}</td>
        <td>${r.share != null ? fx(r.share, 1) + "%" : "–"}</td><td>${r.exports ? fx(r.exports.kbd) : "–"}</td>
        <td class="mini">${esc(r.src)} ${esc(r.kind)}</td><td class="spkc">${spk(r.spark, { w: 110, h: 20, dir: true })}</td></tr>`).join("")}</tbody></table>`;
  $("#sAll").onclick = e => { e.preventDefault(); SST.all = !SST.all; renderSupply(); wrapTables(); };
  $$("#b-supply tr.click").forEach(tr => tr.onclick = () => openCountry(tr.dataset.iso));
  const W = S.world || {};
  const split = (pts, name, color, w = 1.5) => [
    { name, color, w, pts: (pts || []).filter(p => !p[2]).map(p => [p[0] + "-01", p[1]]) },
    { name: name + " (forecast)", short: name, color, w, dash: "4 3", noEnd: true,
      pts: (pts || []).filter((p, i, a) => p[2] || (a[i + 1] && a[i + 1][2])).map(p => [p[0] + "-01", p[1]]) }];
  lchart($("#sWorld"), {
    series: [...split(W.crude, "World", COL.ink, 2), ...split(W.opecplus, "OPEC+", COL.s2), ...split(W.us, "United States", COL.s1),
      ...split(W.others, "Others outside OPEC+", COL.s3)],
    yfmt: v => v.toFixed(0), tfmt: v => v.toFixed(2) + " mb/d", height: 250, noLegend: true, endLabels: true,
  });
  lchart($("#sBal"), {
    series: [...split(W.liquids, "Supply", COL.s1), ...split(W.demand, "Demand", COL.s2)],
    yfmt: v => v.toFixed(0), tfmt: v => v.toFixed(2) + " mb/d", height: 250, noLegend: true, endLabels: true,
  });
}

/* ==========================================================================
   OPEC+
   ========================================================================== */
function renderOpec() {
  if (!ok("opec")) { if (D.opec) $("#b-opec").innerHTML = failed("opec"); return; }
  const O = D.opec, T = O.targets || {};
  const nd = O.next_meeting ? days(O.next_meeting) : null;
  const kindLab = { voluntary: "the voluntary group", JMMC: "monitoring committee (JMMC)", ministerial: "OPEC+ ministers", other: "OPEC" };
  $("#b-opec").innerHTML = `<div class="cards">
      <div class="card"><div class="k">Next meeting</div><div class="v">${O.next_meeting ? dmy(O.next_meeting) : "–"}</div>
        <div class="t">${nd != null ? `in ${nd} day${nd === 1 ? "" : "s"} · ` : ""}the ${esc(O.group?.size || "")} countries with voluntary cuts, online</div></div>
      <div class="card"><div class="k">The group</div><div class="v" style="font-size:.8rem">${esc((O.group?.members || []).join(", ") || "–")}</div>
        <div class="t">as named in OPEC's release of ${ymd(O.group?.date)}</div></div>
      <div class="card"><div class="k">Spare capacity (EIA)</div><div class="v">${(() => { const s = (O.capacity?.spare || []).filter(x => !x[2]); return s.length ? fx(s[s.length - 1][1], 2) + " mb/d" : "–"; })()}</div>
        <div class="t">OPEC total, ${monl(O.last_hist)}: oil a member could add within 30 days</div></div>
      <div class="card"><div class="k">OPEC basket</div><div class="v">${usd(O.orb?.last)}</div><div class="t">${ymd(O.orb?.asof)} · OPEC's daily price</div></div></div>
    ${T.stale ? `<div class="note"><b>A newer OPEC table exists</b>The targets below are for ${monl(T.month)}, from the release of ${ymd(T.decided)};
      OPEC published another on ${ymd(T.newer?.date)} — <a href="${esc(T.newer?.url)}" target="_blank" rel="noopener">read it</a>. The page cannot read the table
      itself: OPEC publishes it as an image.</div>` : ""}
    <div class="og" style="margin-top:.7rem"><div>
      <div class="grp" style="margin-top:0">Members <span class="dim">· crude, kb/d, EIA · target: ${esc(T.group || "")}, ${monl(T.month)}</span></div>
      <table class="tbl"><thead><tr><th>Member</th><th></th><th>Output</th><th>Target</th><th>Against target</th><th>On a year</th><th>Two years</th></tr></thead><tbody>
      ${(O.members || []).map(m => `<tr class="click" data-iso="${esc(m.iso)}"><td>${esc(m.name)}</td><td class="mini">${esc(m.group)}</td>
        <td><b>${fx(m.kbd)}</b> <span class="dim mini">${monl(m.month)}</span></td><td>${m.target ? fx(m.target) : "–"}</td>
        <td>${m.gap != null ? `<span class="${m.gap < -50 ? "neg" : m.gap > 50 ? "warnc" : ""}">${sg(m.gap)}</span>` : "–"}</td>
        <td>${sgc(m.y1)}</td><td class="spkc">${spk(m.spark, { w: 110, h: 20, dir: true })}</td></tr>`).join("")}</tbody></table>
      <div class="src">Targets typed in from <a href="${esc(T.url || "#")}" target="_blank" rel="noopener">OPEC's release of ${ymd(T.decided)}</a> (published as an image).
        Output is the EIA's estimate, which can differ from OPEC's own secondary sources.</div></div>
    <div><div class="grp" style="margin-top:0">OPEC capacity, output and spare <span class="dim">· mb/d, EIA; dashed is forecast</span></div><div class="lc" id="oCap"></div>
      <div class="grp">What OPEC decided</div><div id="oRel"></div></div></div>`;
  $$("#b-opec tr.click").forEach(tr => tr.onclick = () => openCountry(tr.dataset.iso));
  $("#oRel").innerHTML = (O.releases || []).filter(r => r.kind !== "other" || /production|output|supply|market/i.test((r.decision || []).join(" ") + " " + (r.lead || ""))).slice(0, 8).map(r => `<div class="cm">
      <span class="d">${ymd(r.date)}</span><span><span class="dim">${esc(kindLab[r.kind] || r.kind)}</span>
      ${esc((r.decision && r.decision[0]) || r.lead || "")}${r.next ? ` <span class="dim">Next: ${dmy(r.next)}.</span>` : ""}
      <a href="${esc(r.url)}" target="_blank" rel="noopener">release →</a></span></div>`).join("") || `<div class="dim">No releases read.</div>`;
  const C = O.capacity || {};
  const split = (pts, name, color, w = 1.5) => [
    { name, color, w, pts: (pts || []).filter(p => !p[2]).map(p => [p[0] + "-01", p[1]]) },
    { name: name + " (forecast)", short: name, color, w, dash: "4 3", noEnd: true, pts: (pts || []).filter((p, i, a) => p[2] || (a[i + 1] && a[i + 1][2])).map(p => [p[0] + "-01", p[1]]) }];
  lchart($("#oCap"), {
    series: [...split(C.opec, "Capacity", COL.mid), ...split(C.production, "Output", COL.ink, 2), ...split(C.spare, "Spare", COL.s3), ...split(C.outages, "Unplanned outages", COL.s2)],
    yfmt: v => v.toFixed(0), tfmt: v => v.toFixed(2) + " mb/d", height: 240, noLegend: true, endLabels: true, zero: true,
  });
}

/* ==========================================================================
   stocks
   ========================================================================== */
const STK = { k: "crude" };

function renderStocks() {
  if (!ok("stocks")) { if (D.stocks) $("#b-stocks").innerHTML = failed("stocks"); return; }
  const St = D.stocks;
  // A change in a percentage is in points, not percent.
  const unitf = (r, v, sign) => v == null ? "–" : (sign ? sg(v, r.unit === "kb/d" ? 0 : 1) : fx(v, r.unit === "kb/d" ? 0 : 1))
    + (r.unit === "mb" ? " mb" : r.unit === "%" ? (sign ? " pts" : "%") : " kb/d");
  $("#b-stocks").innerHTML = `<div class="og"><div>
      <div class="ctl"><span class="grp" id="stSel">${Object.keys(St.seasonal || {}).map(k => {
        const r = St.rows.find(x => x.k === k); return `<button class="seg ${k === STK.k ? "on" : ""}" data-k="${k}">${esc(r ? r.label.replace(/ \(.*\)/, "") : k)}</button>`; }).join("")}</span></div>
      <div class="band" id="stBand"></div></div>
    <div><table class="tbl"><thead><tr><th>US, weekly</th><th>Latest</th><th>Week</th><th>Year</th><th>vs 5-yr avg</th></tr></thead><tbody>
      ${St.rows.map(r => `<tr><td>${esc(r.label)}<div class="mini">week to ${ymd(r.asof)}</div></td><td><b>${unitf(r, r.last)}</b></td>
        <td>${r.w1 == null ? "–" : `<span class="${sgn(r.w1)}">${unitf(r, r.w1, true)}</span>`}</td>
        <td>${r.y1 == null ? "–" : `<span class="${sgn(r.y1)}">${unitf(r, r.y1, true)}</span>`}</td>
        <td>${sgc(r.vs5_pct, 0, "%")}</td></tr>`).join("")}</tbody></table>
      <div class="src">Next report: ${dmy(St.next)} at 16:30 Amsterdam time (a day later in a US holiday week).</div></div></div>`;
  $$("#stSel .seg").forEach(b => b.onclick = () => { $$("#stSel .seg").forEach(x => x.classList.toggle("on", x === b)); STK.k = b.dataset.k; drawBand(); });
  drawBand();
}

/* this year and last against the five-year range, week by week */
function drawBand() {
  const host = $("#stBand"), z = (D.stocks.seasonal || {})[STK.k];
  if (!host || !z) return;
  const r = D.stocks.rows.find(x => x.k === STK.k) || {};
  const W = Math.max(280, host.clientWidth || 600), H = 260, M = { l: 46, r: 12, t: 10, b: 22 };
  const all = [...z.band.flatMap(b => [b[1], b[3]]), ...z.this.map(p => p[1]), ...z.last.map(p => p[1])];
  if (!all.length) { host.innerHTML = `<div class="lc-empty">No history</div>`; return; }
  const yt = nice(Math.min(...all), Math.max(...all), 5);
  const X = w => M.l + (w - 1) / 51 * (W - M.l - M.r), Y = v => M.t + (yt.hi - v) / (yt.hi - yt.lo) * (H - M.t - M.b);
  let g = "";
  for (const t of yt.ticks) g += `<line class="gl" x1="${M.l}" x2="${W - M.r}" y1="${Y(t)}" y2="${Y(t)}"/><text class="ax" x="${M.l - 5}" y="${Y(t) + 3}" text-anchor="end">${fx(t, yt.step < 1 ? 1 : 0)}</text>`;
  ["Jan", "Mar", "May", "Jul", "Sep", "Nov"].forEach((m, i) => { const w = 1 + i * 8.7; g += `<text class="ax" x="${X(w)}" y="${H - 5}" text-anchor="middle">${m}</text>`; });
  const B = z.band;
  if (B.length) {
    g += `<path d="M${B.map(b => `${X(b[0]).toFixed(1)} ${Y(b[3]).toFixed(1)}`).join("L")}L${B.slice().reverse().map(b => `${X(b[0]).toFixed(1)} ${Y(b[1]).toFixed(1)}`).join("L")}Z" fill="rgba(131,149,158,.16)"/>`;
    g += `<path d="M${B.map(b => `${X(b[0]).toFixed(1)} ${Y(b[2]).toFixed(1)}`).join("L")}" fill="none" stroke="var(--ink-mid)" stroke-dasharray="4 3" stroke-width="1.1"/>`;
  }
  const line = (pts, col, w) => pts.length ? `<path d="M${pts.map(p => `${X(p[0]).toFixed(1)} ${Y(p[1]).toFixed(1)}`).join("L")}" fill="none" stroke="${col}" stroke-width="${w}"/>` : "";
  g += line(z.last, COL.s1, 1.4) + line(z.this, "var(--ink)", 2.4);
  if (z.this.length) { const p = z.this[z.this.length - 1]; g += `<circle cx="${X(p[0])}" cy="${Y(p[1])}" r="3" fill="var(--ink)"/>`; }
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}">${g}</svg>
    <div class="legend"><span class="chip on" style="cursor:default"><i class="sw" style="background:var(--ink)"></i>${z.year}</span>
      <span class="chip on" style="cursor:default"><i class="sw" style="background:${COL.s1}"></i>${z.year - 1}</span>
      <span class="chip on" style="cursor:default"><i class="sw" style="background:rgba(131,149,158,.5)"></i>range ${esc(z.years)}, dashed: average</span>
      <span class="mini">${esc(r.label || "")}, ${esc(r.unit || "")}</span></div>`;
}

/* ==========================================================================
   who trades
   ========================================================================== */
const TST = { mkt: null };

function renderTrade() {
  if (!ok("trade")) { if (D.trade) $("#b-trade").innerHTML = failed("trade"); return; }
  const T = D.trade, P = T.positioning || [];
  TST.mkt = TST.mkt || (P[0] && P[0].name);
  $("#b-trade").innerHTML = `<div class="grp" style="margin-top:0">Futures positioning <span class="dim">· contracts, as of each Tuesday · click a market</span></div>
    <table class="tbl" id="posT"><thead><tr><th>Market</th><th>Hedge funds, net</th><th>Week</th><th>3-year percentile</th><th>% of open interest</th>
      <th>Producers &amp; merchants, net</th><th>Swap dealers, net</th><th>As of</th></tr></thead><tbody>
      ${P.map(r => `<tr class="click ${r.name === TST.mkt ? "you" : ""}" data-m="${esc(r.name)}"><td>${esc(r.name)}</td><td><b>${sg(r.mm_net)}</b></td><td>${sgc(r.mm_w1)}</td>
        <td>${pbar(r.mm_pctile)} ${r.mm_pctile != null ? ord(r.mm_pctile) : "–"}</td><td>${r.mm_share != null ? fx(r.mm_share, 1) + "%" : "–"}</td>
        <td>${sg(r.pm_net)}</td><td>${sg(r.sw_net)}</td><td class="dim">${ymd(r.asof)}</td></tr>`).join("")}</tbody></table>
    <div class="lc" id="posC"></div>
    <div class="src">CFTC disaggregated futures-only reports for the NYMEX contracts; ICE's own reports for Brent and gasoil. Next: ${dmy(T.next_cot)}, 21:30.</div>
    <div class="og" style="margin-top:.8rem"><div>
      <div class="grp" style="margin-top:0">Who buys whose crude <span class="dim">· kb/d, from the importers' customs (UN Comtrade), the latest year each has published</span></div>
      <div id="sankey"></div></div>
    <div><div class="grp" style="margin-top:0">The importers</div>
      <table class="tbl"><thead><tr><th>Importer</th><th>Crude, kb/d</th><th>Year</th><th>Biggest suppliers</th></tr></thead><tbody>
      ${((T.matrix && T.matrix.importers) || []).map(i => {
        const sup = T.matrix.flows.filter(f => f.to === i.iso).sort((a, b) => b.kbd - a.kbd).slice(0, 3);
        return `<tr><td>${esc(i.name)}</td><td><b>${fx(i.kbd)}</b></td><td class="dim">${i.year}</td>
          <td class="mini">${sup.map(s => `${esc(isoName(s.from))} ${fx(s.kbd)}`).join(" · ")}</td></tr>`; }).join("")}</tbody></table></div></div>
    <div class="grp">Who moves the physical oil</div>
    <div class="who">${(T.traders || []).map(t => `<div><b>${esc(t.name)}</b><div class="w">${esc(t.who)}</div><p>${esc(t.what)}</p></div>`).join("")}</div>`;
  $$("#posT tr.click").forEach(tr => tr.onclick = () => { TST.mkt = tr.dataset.m; $$("#posT tr").forEach(x => x.classList.toggle("you", x === tr)); drawPos(); });
  drawPos();
  if (T.matrix) sankey($("#sankey"), T.matrix);
}

function drawPos() {
  const r = (D.trade.positioning || []).find(x => x.name === TST.mkt);
  if (!r) return;
  lchart($("#posC"), {
    height: 210, zero: true,
    series: [{ name: "Hedge funds (managed money)", color: COL.ink, w: 2, pts: r.hist.map(h => [h[0], h[1]]) },
      { name: "Producers & merchants", color: COL.s2, w: 1.3, pts: r.hist.map(h => [h[0], h[2]]) },
      { name: "Swap dealers", color: COL.s1, w: 1.3, pts: r.hist.map(h => [h[0], h[3]]) }],
    yfmt: v => fx(v / 1000) + "k", tfmt: v => sg(v), label: "Positioning",
  });
}

const ISO_NAMES = {};
function isoName(iso) { return ISO_NAMES[iso] || iso; }

/* exporters on the left, importers on the right, ribbons as wide as the flow */
function sankey(host, M) {
  const W = Math.max(320, host.clientWidth || 600);
  const imps = M.importers.slice(0, 12).map(i => i.iso);
  const flows = M.flows.filter(f => imps.includes(f.to));
  const tot = {};
  for (const f of flows) tot[f.from] = (tot[f.from] || 0) + f.kbd;
  const exps = Object.entries(tot).sort((a, b) => b[1] - a[1]).slice(0, 13).map(e => e[0]);
  const agg = {};
  for (const f of flows) {
    const from = exps.includes(f.from) ? f.from : "Other";
    const k = from + ">" + f.to;
    agg[k] = (agg[k] || 0) + f.kbd;
  }
  const L = [...exps, "Other"], R = imps;
  const lt = Object.fromEntries(L.map(x => [x, 0])), rt = Object.fromEntries(R.map(x => [x, 0]));
  for (const [k, v] of Object.entries(agg)) { const [a, b] = k.split(">"); lt[a] += v; rt[b] += v; }
  const total = Object.values(lt).reduce((a, b) => a + b, 0);
  const gap = 5, H = Math.max(360, 26 * Math.max(L.length, R.length)), lw = 118, rw = 118;
  const sc = (H - gap * (Math.max(L.length, R.length) - 1)) / total;
  const ly = {}, ry = {};
  let y = 0; for (const x of L) { ly[x] = y; y += lt[x] * sc + gap; }
  y = 0; for (const x of R) { ry[x] = y; y += rt[x] * sc + gap; }
  const x0 = lw, x1 = W - rw;
  const lo = { ...ly }, ro = { ...ry };
  const pal = [COL.s2, COL.s1, COL.s3, COL.s4, "#c79a52", "#4fa8ce", "#e0644f", "#3fb380", "#a879e0", "#d95926", "#83959e", "#199e70", "#3987e5", "#5a6c76"];
  let g = "";
  const order = Object.entries(agg).sort((a, b) => L.indexOf(a[0].split(">")[0]) - L.indexOf(b[0].split(">")[0]) || R.indexOf(a[0].split(">")[1]) - R.indexOf(b[0].split(">")[1]));
  for (const [k, v] of order) {
    const [a, b] = k.split(">"), h = v * sc;
    if (h < 0.4) { lo[a] += h; ro[b] += h; continue; }
    const ya = lo[a], yb = ro[b]; lo[a] += h; ro[b] += h;
    const c = pal[L.indexOf(a) % pal.length], mx = (x0 + x1) / 2;
    g += `<path class="rb" data-a="${a}" data-b="${b}" fill="${c}" d="M${x0} ${ya}C${mx} ${ya} ${mx} ${yb} ${x1} ${yb}L${x1} ${yb + h}C${mx} ${yb + h} ${mx} ${ya + h} ${x0} ${ya + h}Z">
      <title>${esc(isoName(a))} → ${esc(isoName(b))}: ${fx(v)} kb/d</title></path>`;
  }
  for (const x of L) g += `<rect x="${x0 - 6}" y="${ly[x]}" width="6" height="${Math.max(1, lt[x] * sc)}" fill="${pal[L.indexOf(x) % pal.length]}"/>
    <text class="nd" x="${x0 - 10}" y="${ly[x] + lt[x] * sc / 2 + 3}" text-anchor="end">${esc(x === "Other" ? "Others" : isoName(x))} ${fx(lt[x])}</text>`;
  for (const x of R) g += `<rect x="${x1}" y="${ry[x]}" width="6" height="${Math.max(1, rt[x] * sc)}" fill="var(--ink-mid)"/>
    <text class="nd" x="${x1 + 10}" y="${ry[x] + rt[x] * sc / 2 + 3}">${esc(isoName(x))} ${fx(rt[x])}</text>`;
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H + 2}" height="${H + 2}">${g}</svg>`;
}

/* ==========================================================================
   pump prices
   ========================================================================== */
const PU = { prod: "diesel", sort: "diesel", on: ["NL", "DE", "BE", "FR", "EU", "US"] };
const PU_COL = { NL: COL.ink, DE: COL.s1, BE: COL.s2, FR: COL.s3, EU: COL.s4, US: COL.mid };

function renderPump() {
  if (!ok("pump")) { if (D.pump) $("#b-pump").innerHTML = failed("pump"); return; }
  const U = D.pump;
  const rows = [...U.eu].sort((a, b) => (b[PU.sort] || 0) - (a[PU.sort] || 0));
  $("#b-pump").innerHTML = `<div class="og"><div>
      <div class="ctl"><span class="grp" id="puProd"><button class="seg ${PU.prod === "diesel" ? "on" : ""}" data-v="diesel">diesel</button><button class="seg ${PU.prod === "euro95" ? "on" : ""}" data-v="euro95">Euro 95</button></span>
        ${chips("puC", Object.keys(PU_COL).map(k => ({ k, name: k === "EU" ? "EU average" : k, color: PU_COL[k] })), PU.on)}</div>
      <div class="lc" id="puChart"></div>
      <div class="grp">Brent in other currencies <span class="dim">· the same barrel, where the money is not dollars</span></div>
      <table class="tbl"><thead><tr><th>Currency</th><th>Brent</th><th>On a year</th><th>This year</th></tr></thead><tbody>
        ${(U.currencies || []).map(c => `<tr class="${c.ccy === "EUR" ? "you" : ""}"><td>${esc(c.ccy)}</td><td>${fx(c.brent, c.brent > 1000 ? 0 : 2)}</td>
          <td>${sgc(c.y1, 1, "%")}</td><td>${sgc(c.ytd, 1, "%")}</td></tr>`).join("")}</tbody></table>
      <div class="src">Dated Brent (EIA) in each currency at Yahoo's exchange rate for the day.</div></div>
    <div><table class="tbl"><thead><tr><th>Country</th><th class="sort" data-s="euro95">Euro 95</th><th class="sort" data-s="diesel">Diesel</th>
      <th>Tax share</th><th>4 weeks</th><th>1 year</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="${r.c === "NL" ? "you" : ""}"><td>${esc(r.name)}</td><td>${eurl(r.petrol)}</td><td><b>${eurl(r.diesel)}</b></td>
        <td class="mini">${r.petrol_tax != null ? fx(r.petrol_tax) + "% / " + fx(r.diesel_tax) + "%" : "–"}</td>
        <td>${sgc(PU.sort === "diesel" ? r.diesel_m1 : r.petrol_m1, 1, "%")}</td><td>${sgc(PU.sort === "diesel" ? r.diesel_y1 : r.petrol_y1, 1, "%")}</td></tr>`).join("")}
      ${(U.other || []).map(r => `<tr><td>${esc(r.name)} <span class="mini">${esc(r.kind)}</span></td>
        <td>${r.kind === "petrol" ? eurl(r.eur_l) : ""}</td><td>${r.kind === "diesel" ? `<b>${eurl(r.eur_l)}</b>` : ""}</td>
        <td class="mini">${fx(r.local, r.local_unit === "p/l" ? 1 : 3)} ${esc(r.local_unit)}</td><td></td><td>${sgc(r.y1, 1, "%")}</td></tr>`).join("")}
      </tbody></table>
      <div class="src">€ a litre with all taxes, week of ${ymd(rows[0]?.asof)} (EU Weekly Oil Bulletin); tax share is petrol / diesel. The 4-week and 1-year
        changes are for ${PU.sort === "diesel" ? "diesel" : "Euro 95"}; click a column to sort by it.</div></div></div>`;
  $$("#b-pump th.sort").forEach(th => th.onclick = () => { PU.sort = th.dataset.s; renderPump(); wrapTables(); });
  $$("#puProd .seg").forEach(b => b.onclick = () => { $$("#puProd .seg").forEach(x => x.classList.toggle("on", x === b)); PU.prod = b.dataset.v; drawPump(); });
  wireChips("puC", PU.on, drawPump);
  drawPump();
}

function drawPump() {
  const H = D.pump.hist || {};
  lchart($("#puChart"), {
    series: PU.on.map(c => ({ name: c === "EU" ? "EU average" : c, color: PU_COL[c], w: c === "NL" ? 2.2 : 1.3,
      pts: H[`${c}:${PU.prod}`] || [] })),
    yfmt: v => "€" + v.toFixed(2), tfmt: v => "€" + v.toFixed(3), endLabels: true, height: 260, label: "Pump prices",
  });
}

/* ==========================================================================
   your portfolio
   ========================================================================== */
const BST = { all: false };

function renderBook() {
  if (!ok("book")) { if (D.book) $("#b-book").innerHTML = failed("book"); return; }
  const B = D.book, Y = B.you;
  const hs = B.holdings || [];
  const show = BST.all ? hs : [...new Map([...hs.slice(0, 8), ...hs.filter(h => h.oil_name), ...hs.slice(-5)].map(h => [h.t, h])).values()];
  const groups = [...new Set((B.board || []).map(b => b.group))];
  $("#b-book").innerHTML = `<div class="cards">
      <div class="card"><div class="k">Your portfolio's beta to Brent</div><div class="v">${Y ? sg(Y.beta, 2) : "–"}</div>
        <div class="t">${Y ? `R² ${fx(Y.r2, 2)}: oil explains ${fx(Y.r2 * 100)}% of your weekly moves` : esc(B.book_error || "not enough history")}</div></div>
      <div class="card"><div class="k">A $${fx(B.move)} rise in Brent</div><div class="v">${Y && Y.per10 != null ? eur(Y.per10) : "–"}</div>
        <div class="t">${fx(B.pct10, 1)}% on Brent at ${usd(B.brent)} (${ymd(B.brent_asof)}), on today's holdings</div></div>
      <div class="card"><div class="k">Brent this year</div><div class="v">${B.brent_ytd != null ? pc(B.brent_ytd, 1) : "–"}</div>
        <div class="t">Dated Brent since 31 December</div></div></div>
    <div class="og" style="margin-top:.7rem"><div>
      <div class="grp" style="margin-top:0">Your holdings against Brent <span class="dim">· two years of five-day returns</span>
        <a href="#" id="bAll" style="float:right;font-size:.62rem">${BST.all ? "the most and least exposed" : `all ${hs.length}`}</a></div>
      <table class="tbl"><thead><tr><th>Holding</th><th>Weight</th><th>Beta to Brent</th><th>± error</th><th>R²</th><th>€ per +$${fx(B.move)}</th></tr></thead><tbody>
      ${show.map(h => `<tr class="${h.oil_name ? "you" : ""}"><td><a href="/t/${encodeURIComponent(h.t)}">${esc(h.t)}</a> <span class="mini">${esc((h.name || "").slice(0, 26))}</span></td>
        <td>${fx(h.w, 1)}%</td><td><b class="${sgn(h.beta)}">${sg(h.beta, 2)}</b></td><td class="dim">${h.se != null ? "±" + fx(h.se * 2, 2) : "–"}</td>
        <td>${fx(h.r2, 2)}</td><td>${h.per10 != null ? eur(h.per10) : "–"}</td></tr>`).join("")}</tbody></table>
      <div class="src">Highlighted: oil and energy shares. The error band is two standard errors, widened for overlapping windows.</div></div>
    <div><div class="grp" style="margin-top:0">Oil shares <span class="dim">· held ones highlighted · beta from one year of five-day returns</span></div>
      <table class="tbl"><thead><tr><th>Share</th><th>Last</th><th>Day</th><th>This year</th><th>1 year</th><th>Beta</th></tr></thead><tbody>
      ${groups.map(g => `<tr class="sec"><td colspan="6">${esc(g)}</td></tr>` + B.board.filter(b => b.group === g).map(b => `<tr class="${b.held ? "you" : ""}">
        <td><a href="/t/${encodeURIComponent(b.sym)}">${esc(b.name)}</a> <span class="mini">${esc(b.sym)}</span></td><td>${fx(b.last, 2)}</td>
        <td>${sgc(b.d1, 1, "%")}</td><td>${sgc(b.ytd, 1, "%")}</td><td>${sgc(b.y1, 1, "%")}</td><td>${b.beta != null ? fx(b.beta, 2) : "–"}</td></tr>`).join("")).join("")}
      </tbody></table></div></div>`;
  $("#bAll").onclick = e => { e.preventDefault(); BST.all = !BST.all; renderBook(); wrapTables(); };
}

/* ==========================================================================
   Claude's note
   ========================================================================== */
let NOTE_T = null;
async function renderNote() {
  let N;
  try { N = await api("/api/pf/oil/note"); } catch (e) { $("#b-note").innerHTML = `<div class="note"><b>Could not load</b>${esc(e.message)}</div>`; return; }
  const job = N.job, latest = (N.notes || [])[0];
  const writer = N.writer === "claude-code" ? `Claude Code on your Claude plan (${esc(N.cc_model_label)})` : `the API with your key (${esc(N.model_label)})`;
  let h = `<div class="prose">Claude reads this page — every panel's numbers, the findings above, the calendar, the headlines and your holdings'
    sensitivity — and writes a one-page note. Written by ${writer}; change that in Settings. <b>Only when you press the button</b>: each note counts toward
    your plan's usage, or bills your API key.</div>
    <div class="ctl" style="margin-top:.5rem"><button class="tbtn on" id="nGo"${job && job.state === "running" ? " disabled" : ""}>Write today's note</button>
      <label class="mini"><input type="checkbox" id="nWeb"> with web search (to explain moves the page cannot)</label></div>`;
  if (job && job.state === "running") h += `<div class="mini">${esc(job.phase || "")} · ${job.elapsed_s}s<span class="blink">_</span></div>${job.text ? `<div class="writing md">${md(job.text)}</div>` : ""}`;
  if (job && job.state === "error") h += `<div class="note"><b>The note failed</b>${esc(job.error || "")}</div>`;
  if (latest) {
    h += `<div class="mini" style="margin-top:.7rem">${esc(latest.model || "")} · ${esc((latest.created || "").replace("T", " ").slice(0, 16))} UTC
      · <a href="#" data-delnote="${latest.id}" style="color:var(--loss)">delete</a></div><div class="md" style="margin-top:.3rem">${md(latest.body)}</div>
      ${(N.answers || []).slice(0, 5).map(a => `<div style="margin-top:.6rem"><div class="dim" style="font-size:.68rem">Q: ${esc(a.question)}</div><div class="md">${md(a.body)}</div></div>`).join("")}
      <div class="qbox"><input id="nQ" placeholder="Ask about the note or the page" maxlength="2000"><button class="tbtn" id="nAsk">Ask</button></div>
      ${(N.notes || []).length > 1 ? `<details class="howto"><summary>Earlier notes</summary>${N.notes.slice(1).map(n => `<div style="margin:.5rem 0"><div class="mini">${esc(n.d)} · ${esc(n.model || "")}</div><div class="md">${md(n.body)}</div></div>`).join("")}</details>` : ""}`;
  }
  $("#b-note").innerHTML = h;
  const start = async q => {
    try { await api("/api/pf/oil/note", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ question: q || null, web: !!($("#nWeb") && $("#nWeb").checked) }) }); }
    catch (e) { flash(esc(e.message)); return; }
    renderNote();
  };
  $("#nGo").onclick = () => start(null);
  if ($("#nAsk")) $("#nAsk").onclick = () => { const q = $("#nQ").value.trim(); if (q) start(q); };
  $$("#b-note [data-delnote]").forEach(a => a.onclick = async e => { e.preventDefault(); await fetch("/api/pf/oil/note/" + a.dataset.delnote, { method: "DELETE" }); renderNote(); });
  clearTimeout(NOTE_T);
  if (job && job.state === "running") NOTE_T = setTimeout(renderNote, 2500);
}

/* ==========================================================================
   news and sources
   ========================================================================== */
function renderNews() {
  if (!ok("news")) { if (D.news) $("#b-news").innerHTML = failed("news"); return; }
  $("#b-news").innerHTML = `<div class="nlist">${(D.news.items || []).slice(0, 30).map(x => `<a class="nws" href="${esc(x.url)}" target="_blank" rel="noopener">
      <span class="hl">${esc(x.headline)}</span><span class="mt">${esc(x.source)}${x.ts ? " · " + dmyTime(new Date(x.ts * 1000).toISOString()) : ""}</span></a>`).join("")}</div>`;
}

async function renderSources() {
  let S = {};
  try { S = await api("/api/pf/oil/sources"); } catch { }
  const rows = (S.sources || []).map(s => `<div class="row" style="align-items:flex-start"><span class="nm"><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.label)}</a></span>
    <span class="px ${s.ok ? "pos" : "warnc"}">${s.ok ? "ok" : "not yet"}</span>
    <span class="ch dim" style="min-width:0;text-align:right;white-space:normal;max-width:60ch">${s.age_s != null ? `fetched ${s.age_s < 120 ? s.age_s + "s" : s.age_s < 7200 ? Math.round(s.age_s / 60) + "m" : Math.round(s.age_s / 3600) + "h"} ago` : ""}${s.stale ? " · " + esc(s.stale) : ""}</span></div>`).join("");
  const errs = PARTS.filter(p => D[p] && D[p].error).map(p => `<div class="row"><span class="nm">${esc(p)}</span><span class="px warnc">error</span><span class="ch dim" style="min-width:0;white-space:normal">${esc(D[p].error)}</span></div>`).join("");
  $("#b-src").innerHTML = `<div class="rows">${rows}${errs}</div>
    <div class="src">All keyless except aisstream.io (a free key you create). Map: Natural Earth (public domain). Platforms © OpenStreetMap contributors (ODbL) and
      Sodir (NLOD). Chokepoints and ports: IMF PortWatch, from UN Global Platform AIS data. Pipelines and OPEC+ targets are typed in (oil_seed.json) and
      say when they were entered.</div>`;
}

/* ==========================================================================
   one country in depth
   ========================================================================== */
async function openCountry(iso) {
  const dr = $("#drawer");
  dr.classList.add("on"); dr.setAttribute("aria-hidden", "false");
  dr.innerHTML = `<div class="dh"><h2>${esc(isoName(iso))}</h2><span class="sp"></span><button class="tbtn" id="drX">Close</button></div>
    <div class="db"><div class="dim ld">Loading<span class="blink">_</span></div></div>`;
  $("#drX").onclick = closeCountry;
  if (location.hash !== "#c=" + iso) history.replaceState(null, "", "#c=" + iso);
  let C;
  try { C = await api(`/api/pf/oil/country/${iso}`); } catch (e) { dr.querySelector(".db").innerHTML = `<div class="note"><b>Could not load</b>${esc(e.message)}</div>`; return; }
  if (C.error) { dr.querySelector(".db").innerHTML = `<div class="note"><b>Could not load</b>${esc(C.error)}</div>`; return; }
  ISO_NAMES[iso] = C.name;
  dr.querySelector("h2").textContent = C.name;
  const pr = C.production || {}, last = (pr.series || []).slice(-1)[0];
  const tk = C.tankers;
  let h = `<div class="cards">
    <div class="card"><div class="k">Production</div><div class="v">${last ? fx(last[1]) + " kb/d" : "–"}</div><div class="t">${last ? monl(last[0]) : ""} · ${esc(pr.src || "")} ${esc(pr.kind || "")}${C.group ? " · " + esc(C.group) : ""}</div></div>
    ${C.target ? `<div class="card"><div class="k">OPEC+ target</div><div class="v">${fx(C.target.kbd)} kb/d</div><div class="t">${monl(C.target.month)} · ${last ? sg(last[1] - C.target.kbd) + " kb/d against it" : ""}</div></div>` : ""}
    ${tk ? `<div class="card"><div class="k">Loaded onto tankers</div><div class="v">${fx((tk.exp.w30 || 0) / 1000, 1)} kt/d</div><div class="t">30 days to ${ymd(tk.exp.end)} · ${sg(tk.exp.vs_p30, 0)}% on the 30 days before · ${sg(tk.exp.vs_y30, 0)}% on a year ago</div></div>` : ""}
    </div>
    <div class="sec">Production, kb/d (${esc(pr.src || "")} ${esc(pr.kind || "")})</div><div class="lc" id="drProd"></div>`;
  const J = C.jodi || {};
  if ((J.exp || []).length || (J.runs || []).length)
    h += `<div class="sec">The government's own monthly figures (JODI), kb/d</div><div class="lc" id="drJodi"></div>`;
  if (tk) h += `<div class="sec">Loaded onto and discharged from tankers, 7-day average, kt a day (IMF PortWatch)</div><div class="lc" id="drTank"></div>
    <div class="src">All tanker cargo, estimated from AIS. Ships that switch their transponders off are not counted.</div>`;
  if ((C.ports || []).length) {
    const ms = [...new Set(C.ports.flatMap(p => p.months.map(m => m[0])))].sort().slice(-13);
    h += `<div class="sec">Ports: tanker cargo loaded, kt a month (PortWatch)</div><table class="tbl"><thead><tr><th>Port</th>${ms.slice(-6).map(m => `<th>${monl(m).replace(" 20", " ")}</th>`).join("")}<th>13 months</th></tr></thead><tbody>
      ${C.ports.map(p => { const M = Object.fromEntries(p.months.map(m => [m[0], m])); return `<tr><td>${esc(p.name)}${p.lat != null ? ` <a href="#" class="mini" data-fly="${p.lon},${p.lat}">map</a>` : ""}</td>
        ${ms.slice(-6).map(m => `<td>${M[m] ? fx(M[m][1] / 1000) : "–"}</td>`).join("")}<td class="spkc">${spk(ms.map(m => [m, M[m] ? M[m][1] : 0]), { w: 100, h: 18 })}</td></tr>`; }).join("")}</tbody></table>`;
  }
  if ((C.buyers || []).length) h += `<div class="sec">Who buys its crude (importers' customs, UN Comtrade)</div><table class="tbl"><thead><tr><th>Importer</th><th>kb/d</th><th>$ bn</th><th>Year</th></tr></thead><tbody>
    ${C.buyers.slice(0, 12).map(b => `<tr><td>${esc(b.name)}</td><td><b>${fx(b.kbd)}</b></td><td>${fx(b.usd_bn, 1)}</td><td class="dim">${b.year}</td></tr>`).join("")}</tbody></table>
    <div class="src">Only the importers this page reads (the fifteen largest); a country's sales elsewhere are not here.</div>`;
  if (C.ports_error) h += `<div class="note plain">Ports: ${esc(C.ports_error)}</div>`;
  dr.querySelector(".db").innerHTML = h;
  wrapTables();
  lchart($("#drProd"), { series: [{ name: "Production", color: COL.ink, w: 2, pts: (pr.series || []).map(p => [p[0] + "-01", p[1]]) }],
    refs: C.target ? [{ y: C.target.kbd, label: `OPEC+ target ${fx(C.target.kbd)}` }] : [], yfmt: v => fx(v), tfmt: v => fx(v) + " kb/d", height: 200 });
  if ($("#drJodi")) lchart($("#drJodi"), { height: 200, endLabels: true, series: [["prod", "Production", COL.ink], ["exp", "Exports", COL.s2], ["imp", "Imports", COL.s1], ["runs", "Refinery runs", COL.s3]]
      .filter(([k]) => (J[k] || []).length).map(([k, n, c]) => ({ name: n, color: c, w: 1.5, pts: J[k].map(p => [p[0] + "-01", p[1]]) })), yfmt: v => fx(v), tfmt: v => fx(v) + " kb/d" });
  if ($("#drTank")) lchart($("#drTank"), { height: 200, endLabels: true, series: [{ name: "Loaded", color: COL.s2, w: 1.6, pts: tk.spark.map(p => [p[0], p[1] / 1000]) },
      { name: "Discharged", color: COL.s1, w: 1.3, pts: tk.spark_imp.map(p => [p[0], p[1] / 1000]) }], yfmt: v => fx(v), tfmt: v => fx(v, 1) + " kt/d" });
  dr.querySelectorAll("[data-fly]").forEach(a => a.onclick = e => {
    e.preventDefault(); const [lon, lat] = a.dataset.fly.split(",").map(Number);
    closeCountry(); document.getElementById("s-map").scrollIntoView(); OilMap.focus(lon, lat, 8);
  });
}

function closeCountry() {
  const dr = $("#drawer");
  dr.classList.remove("on"); dr.setAttribute("aria-hidden", "true");
  if (location.hash.startsWith("#c=")) history.replaceState(null, "", location.pathname);
}
// Esc closes the country first, then the full-tab map.
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if ($("#drawer").classList.contains("on")) closeCountry();
  else if ($("#s-map") && $("#s-map").classList.contains("full")) mapFull(false);
});
// A link to #c=SA on this page (or typed into the address bar) opens that country; #map and #globe the full-tab map.
window.addEventListener("hashchange", () => {
  const m = location.hash.match(/^#c=([A-Za-z]{2})$/);
  if (m) openCountry(m[1].toUpperCase());
  mapHash();
});

/* ==========================================================================
   wiring
   ========================================================================== */
const RENDER = {
  prices: () => { renderTop(); renderPrices(); },
  curve: renderCurve,
  cracks: renderCracks,
  flows: renderFlows,
  supply: () => { for (const r of D.supply.rows || []) ISO_NAMES[r.iso] = r.name; renderSupply(); },
  opec: renderOpec,
  stocks: renderStocks,
  trade: () => { Object.assign(ISO_NAMES, (D.trade.matrix && D.trade.matrix.names) || {}); renderTrade(); },
  pump: renderPump,
  book: renderBook,
  news: renderNews,
  calendar: renderComing,
};

window.addEventListener("resize", () => { clearTimeout(window._oilR); window._oilR = setTimeout(() => { if (ok("stocks")) drawBand(); if (ok("trade") && D.trade.matrix) sankey($("#sankey"), D.trade.matrix); }, 200); });

load();
