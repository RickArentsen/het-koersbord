/* ==========================================================================
   EUR — the euro: what it is worth, what moves it, and what it does to a
   book that is mostly in dollars.

   Two fetches: /api/pf/euro (everything from the ECB, FRED, the Bundesbank,
   the CFTC, the World Bank and The Economist, built on the server and kept
   half an hour) and /api/pf/euro/opt (FXE's options, which only read while
   New York trades). Each part renders on its own and says when it failed.
   ========================================================================== */

let V = null, O = null, RANGE = 3652;
const SECTIONS = [["s-price", "EUR/USD"], ["s-cross", "Every currency"], ["s-drive", "What moves it"],
  ["s-fair", "Fair value"], ["s-pos", "Who is betting"], ["s-opt", "The options"], ["s-book", "Your dollars"],
  ["s-hedge", "Hedging"], ["s-cal", "Coming up"], ["s-how", "How it works"]];

/* ---------- formatting ---------------------------------------------------- */
const fx = (v, d = 2) => v == null || isNaN(v) ? "–" :
  Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
const r4 = v => v == null ? "–" : Number(v).toFixed(4);
const sg = (v, d = 1, u = "%") => {
  if (v == null) return "–";
  const r = Number(Math.abs(v).toFixed(d));
  return (r === 0 ? "" : v > 0 ? "+" : "−") + fx(r, d) + u;
};
const sgc = (v, d = 1, u = "%") => `<span class="${sgn(v)}">${sg(v, d, u)}</span>`;
const e0 = v => v == null ? "–" : (v < 0 ? "−" : "") + "€" + fx(Math.abs(v), 0);
const es0 = v => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + "€" + fx(Math.abs(v), 0);
const api = async u => { const r = await fetch(u); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); };
const err = k => V && V[k + "_error"] ? `<div class="err">This part could not load: ${esc(V[k + "_error"])}</div>` : "";
const wait = t => `<div class="dim ld">${t}<span class="blink">_</span></div>`;

$("#shell").innerHTML = termBar({ here: "EUR · THE EURO", right:
  `<a class="tbtn" href="/m/MKT/bonds" title="Bonds &amp; rates">BOND</a><a class="tbtn" href="/m/MVO" title="Markowitz">MVO</a>
   <button class="tbtn" id="reloadBtn">Reload</button>` });
wireShell();
$("#reloadBtn").onclick = () => load(true);

function scaffold() {
  const P = (id, title, hint) => `<div class="panel" id="${id}"><header><h2>${title}</h2><span class="hint">${hint}</span></header>
    <div class="body" id="${id}-b">${wait("reading")}</div></div>`;
  $("#app").innerHTML = `
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>EUR</h1><span class="sub">the euro: what it is worth, what moves it, and what it does to your dollars</span></div>
    <div class="tiles" id="tiles">${wait("asking the ECB, FRED, the Bundesbank and the CFTC")}</div>
    <div class="disc" id="say"></div>
  </div></div>
  <nav class="secnav" id="secnav">${SECTIONS.map(([id, t]) => `<a href="#${id}" data-id="${id}">${t}</a>`).join("")}</nav>
  ${P("s-price", "EUR/USD since 1999", "the ECB's daily reference rate, and Yahoo's quote for today")}
  ${P("s-cross", "The euro against every currency", "the ECB's thirty reference rates; green where the euro gained")}
  ${P("s-drive", "What moves it", "the rate gap, what futures expect of the Fed and the ECB, real rates")}
  ${P("s-fair", "Fair value", "what purchasing power says, and the euro against forty trading partners")}
  ${P("s-pos", "Who is betting on it", "the CFTC's weekly count of CME euro futures")}
  ${P("s-opt", "What the options say", "the range FXE's options imply for EUR/USD")}
  ${P("s-book", "Your dollars", "what the exchange rate does to your book")}
  ${P("s-hedge", "Hedging the dollar", "what it costs now, and what it would have done since 1999")}
  ${P("s-cal", "Coming up", "the next decisions, and this week's releases that move the pair")}
  ${P("s-how", "How it works", "sources, and what each number leaves out")}`;
  const bar = document.querySelector(".term-bar");
  const setTop = () => document.documentElement.style.setProperty("--barh", (bar ? bar.offsetHeight : 36) + "px");
  setTop(); window.addEventListener("resize", setTop);
  const links = $$("#secnav a");
  const io = new IntersectionObserver(es => {
    for (const e of es) if (e.isIntersecting) links.forEach(a => a.classList.toggle("on", a.dataset.id === e.target.id));
  }, { rootMargin: "-40% 0px -55% 0px" });
  SECTIONS.forEach(([id]) => { const el = document.getElementById(id); el && io.observe(el); });
}

async function load(fresh) {
  scaffold();
  api("/api/pf/euro/opt").then(o => { O = o; if (V) renderOpt(); }).catch(e => { O = { available: false, why: e.message }; if (V) renderOpt(); });
  try { V = await api("/api/pf/euro" + (fresh ? "?fresh=1" : "")); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load the euro</b>${esc(e.message)}</div>`; return; }
  if (!V.available) { $("#app").innerHTML = `<div class="note"><b>No euro data</b>${esc(V.why || "")}</div>`; return; }
  const parts = [tiles, renderPrice, renderCross, renderDrive, renderFair, renderPos, renderBook, renderHedge, renderCal, how];
  for (const f of parts) { try { f(); } catch (e) { console.error(f.name, e); } }
  if (O) renderOpt();
  wrapTables();
}

/* ==========================================================================
   tiles
   ========================================================================== */
function tiles() {
  const p = V.price, r = V.rates || {}, pa = V.path || {}, b = V.book || {}, h = (V.hedge || {}).now || {};
  const g = r.gap2 || {};
  $("#tiles").innerHTML = [
    tile("EUR/USD", r4(p.now), p.day != null ? sgc(p.day, 2) : null, `${esc(p.now_src)} · ECB fixing ${r4(p.ecb)} on ${dmy(p.ecb_asof).slice(0, 5)}`, true),
    tile("On a year", sg(p.y1), null, `month ${sg(p.m1)} · year to date ${sg(p.ytd)}`),
    tile("Since 1999", `${p.pctile}th`, null, `percentile of days · average ${r4(p.avg)} · range ${r4(p.lo.v)}–${r4(p.hi.v)}`),
    tile("Two-year gap", g.v != null ? fx(g.v) + " pts" : "–", g.m1_bp != null ? sgc(g.m1_bp, 0, "bp") : null,
      g.v != null ? `US ${fx(g.us)}% − Bund ${fx(g.de)}% · change on a month` : "no rates"),
    tile("In a year, priced", pa.fed && pa.ecb ? `${sg(pa.fed.bp, 0, "")} / ${sg(pa.ecb.bp, 0, "")}` : "–", null,
      pa.fed && pa.ecb ? `basis points, the Fed / the ECB · the gap ${pa.gap_bp > 0 ? "widens" : pa.gap_bp < 0 ? "narrows" : "holds"}` : "no futures"),
    tile("Your dollars", b.available ? `${fx(b.usd_pct, 0)}%` : "–", null,
      b.available ? `${e0(b.usd)} · a cent on EUR/USD is ${e0(Math.abs(b.cent))}` : ""),
    tile("Hedging costs", h.cost != null ? `${fx(h.cost)}%` : "–", null, h.cost != null ? `a year: US 3-month ${fx(h.us3)}% − Euribor ${fx(h.eur3)}%` : ""),
  ].join("");
  $("#say").innerHTML = (V.say || []).map(s => esc(s)).join(" ") + ` <a href="#s-how">How it works</a>.`;
}

/* ==========================================================================
   the price
   ========================================================================== */
function renderPrice() {
  const p = V.price;
  const opts = [["1Y", 365], ["3Y", 1096], ["5Y", 1826], ["10Y", 3652], ["Since 1999", "max"]];
  $("#s-price-b").innerHTML = `<div class="ctl">${rangeBar("pxR", RANGE, opts)}<span class="dim">dashed: the average since 1999 · shaded: the moments below</span></div>
    <div class="lc" id="pxChart"></div>
    <div class="evs">${p.events.map(e => `<span>${dmy(e.d)} <b>${esc(e.label)}</b> ${r4(e.v)}</span>`).join("")}</div>
    <div class="txt"><p>Today ${r4(p.now)}: higher than on ${p.pctile}% of the days since the euro began, against an average of
      ${r4(p.avg)}. The last year ran from ${r4(p.y_lo)} to ${r4(p.y_hi)}. Over five years the euro is ${sg(p.y5)} against the dollar.</p></div>`;
  wireRange("pxR", r => { RANGE = r; drawPx(); });
  drawPx();
}

function drawPx() {
  const p = V.price, from = startOf(RANGE, "1999-01-01");
  const pts = p.series.filter(x => x[0] >= from);
  lchart($("#pxChart"), {
    series: [{ name: "EUR/USD", color: "var(--ink)", pts, w: 1.6 }],
    refs: [{ y: p.avg, label: "average since 1999" }],
    bands: p.events.filter(e => e.d >= from).map(e => [e.d, new Date(Date.parse(e.d) + 9 * 864e5).toISOString().slice(0, 10)]),
    yfmt: v => v.toFixed(2), tfmt: v => v.toFixed(4), label: "EUR/USD",
  });
}

/* ==========================================================================
   every currency
   ========================================================================== */
function spark(vals) {
  if (!vals || vals.length < 3) return "";
  const lo = Math.min(...vals), hi = Math.max(...vals), w = 70, h = 16;
  const d = vals.map((v, i) => `${i ? "L" : "M"}${(i / (vals.length - 1) * w).toFixed(1)} ${(h - (hi > lo ? (v - lo) / (hi - lo) : .5) * h).toFixed(1)}`).join("");
  return `<svg class="spark" width="${w}" height="${h}"><path d="${d}" fill="none" stroke="var(--ink-mid)" stroke-width="1.1"/></svg>`;
}

function renderCross() {
  const C = (V.crosses || {}).rows || [], gone = (V.crosses || {}).gone || [];
  if (!C.length) { $("#s-cross-b").innerHTML = err("crosses") || `<div class="empty">No reference rates.</div>`; return; }
  const groups = ["Majors", "Europe", "Rest of the world"];
  $("#s-cross-b").innerHTML = `<table class="tbl"><thead><tr><th class="l">Currency</th><th>One euro buys</th><th>Week</th><th>Month</th>
      <th>Year to date</th><th>Year</th><th>3 years</th><th>In its 52-week range</th><th>Year</th></tr></thead><tbody>
    ${groups.map(g => `<tr class="sec"><td colspan="9">${g}</td></tr>` + C.filter(c => c.group === g).map(c => `<tr>
      <td class="l"><b>${esc(c.c)}</b> <span class="dim">${esc(c.name)}</span></td>
      <td>${fx(c.v, c.v >= 100 ? 2 : 4)}</td><td>${sgc(c.w1)}</td><td>${sgc(c.m1)}</td><td>${sgc(c.ytd)}</td><td>${sgc(c.y1)}</td><td>${sgc(c.y3)}</td>
      <td>${c.pos != null ? `<span class="pos52" title="${c.pos}% of the way from the year's low to its high"><i style="left:calc(${c.pos}% - 1px)"></i></span>` : ""}</td>
      <td>${spark(c.spark)}</td></tr>`).join("")).join("")}
    </tbody></table>
    <div class="disc">A rise is the euro buying more of that currency. The ECB fixes these once a day at 14:15 Frankfurt time,
      latest ${esc(dmy(C[0].asof))}. The Danish krone is held close to the euro by its central bank.
      ${gone.map(g => `The ${esc(g.name)} was last fixed on ${dmy(g.last)} at ${fx(g.v, 4)}${g.joined ? `: its country adopted the euro on ${dmy(g.joined)}` : ""}.`).join(" ")}</div>`;
}

/* ==========================================================================
   what moves it
   ========================================================================== */
function renderDrive() {
  const r = V.rates || {}, pa = V.path || {}, f = r.fit, g = r.gap2 || {}, re = r.real;
  let h = err("rates");
  if (f) {
    const side = f.gap_pct > 0 ? "above" : "below";
    h += `<div class="sec">The rate gap</div>
    <div class="two"><div><div class="lc" id="gapChart"></div>
      <div class="disc">The two-year US Treasury yield less the two-year Bund's, since 2005. Above zero the dollar pays more.</div></div>
      <div><div id="gapScatter"></div>
      <div class="disc">Each dot is a Friday in the last three years: the gap across, EUR/USD up. The line is the fit.</div></div></div>
    <div class="txt"><p>Money goes where it is paid more, so when American two-year bonds pay more over German ones the dollar
      tends to rise. Today the gap is <b>${fx(f.gap)} points</b> (${g.m1_bp != null ? sg(g.m1_bp, 0, "bp") + " on a month, " : ""}${g.y1_bp != null ? sg(g.y1_bp, 0, "bp") + " on a year" : ""}).
      Over the last ${f.n} weeks each point of gap went with ${fx(Math.abs(f.per_point), 1)}% on EUR/USD (R² ${fx(f.r2)}: the gap explains
      about ${fx(f.r2 * 100, 0)}% of the euro's swings, the rest is everything else). On the gap alone EUR/USD would be <b>${r4(f.fair)}</b>;
      it is ${r4(f.fx)}, ${fx(Math.abs(f.gap_pct), 1)}% ${side} that${Math.abs(f.gap_pct) > 2 * f.sd / f.fx * 100 ? ", further than usual" : ", within its usual distance"}.
      ${r.gap10 ? ` The ten-year gap is ${fx(r.gap10.v)} points (US ${fx(r.gap10.us)}%, Bund ${fx(r.gap10.de)}%).` : ""}</p></div>`;
  }
  if (pa.fed || pa.ecb) {
    const row = (name, x) => x ? `<tr><td class="l">${name}</td><td>${fx(x.now)}%</td><td>${fx(x.then)}%</td><td>${sgc(x.bp, 0, "bp")}</td>
      <td class="l dim">by ${dmy(x.by)}</td><td class="l">${x.next ? dmy(x.next.date) : "–"}${x.next && x.next.change_bp != null ? ` <span class="dim">priced ${sg(x.next.change_bp, 0, "bp")}</span>` : x.next && x.next.days != null ? ` <span class="dim">in ${x.next.days} days</span>` : ""}</td></tr>` : "";
    h += `<div class="sec">What futures expect over the next year</div>
      <table class="tbl"><thead><tr><th class="l">Central bank</th><th>Now</th><th>In a year</th><th>Change</th><th class="l"></th><th class="l">Next decision</th></tr></thead>
      <tbody>${row("The Fed (fed funds)", pa.fed)}${row("The ECB (deposit rate)", pa.ecb)}</tbody></table>
      <div class="txt"><p>${pa.gap_bp != null ? `Together that ${pa.gap_bp > 10 ? `widens the policy gap by about ${pa.gap_bp}bp, which on its own leans toward the dollar`
        : pa.gap_bp < -10 ? `narrows the policy gap by about ${-pa.gap_bp}bp, which on its own leans toward the euro` : "leaves the policy gap about where it is"}.
        What the futures already price is in today's rate; it is a change in these expectations that moves it.` : ""}
        The whole path, meeting by meeting, is on <a href="/m/MKT/bonds#s-path">BOND</a>.</p></div>`;
  }
  if (re) h += `<div class="sec">Real rates</div><div class="cards">
      <div class="card"><div class="k">The Fed, real</div><div class="v">${sg(re.us, 2)}</div><div class="t">fed funds ${fx(re.us_rate)}% less core inflation ${fx(re.us_core)}% (${mon(re.us_month)})</div></div>
      <div class="card"><div class="k">The ECB, real</div><div class="v">${sg(re.ea, 2)}</div><div class="t">deposit rate ${fx(re.ea_rate)}% less core HICP ${fx(re.ea_core)}% (${mon(re.ea_month)})</div></div>
      <div class="card"><div class="k">The gap</div><div class="v">${sg(re.gap, 2, " pts")}</div><div class="t">${re.gap > 0 ? "in the dollar's favour: money in dollars earns more after inflation" : "in the euro's favour"}</div></div></div>`;
  $("#s-drive-b").innerHTML = h || `<div class="empty">No rates.</div>`;
  if (g.series) lchart($("#gapChart"), { series: [{ name: "two-year gap", color: "var(--s1)", pts: g.series, w: 1.5 }], zero: true,
    yfmt: v => v.toFixed(1), tfmt: v => v.toFixed(2) + " pts", height: 230, label: "the two-year rate gap" });
  if (f) scatter($("#gapScatter"), f.pts.map(([a, b]) => [a, b]), { height: 230, xl: "the gap, points", yl: "EUR/USD", tight: true,
    yfmt: t => t.toFixed(2), xfmt: t => t.toFixed(1) });
}

/* ==========================================================================
   fair value
   ========================================================================== */
function renderFair() {
  const pp = V.ppp || {}, ee = V.eer || {}, p = V.price;
  const cards = [];
  for (const x of pp.ppp || []) cards.push(`<div class="card"><div class="k">PPP, ${esc(x.name)} (${x.year})</div><div class="v">${r4(x.eurusd)}</div>
    <div class="t">${x.gap_pct < 0 ? `the euro is ${fx(-x.gap_pct, 0)}% below what prices there say` : `the euro is ${fx(x.gap_pct, 0)}% above what prices there say`}</div></div>`);
  const bm = pp.bigmac;
  if (bm) cards.push(`<div class="card"><div class="k">Big Mac index (${mon(bm.d)})</div><div class="v">${r4(bm.implied)}</div>
    <div class="t">€${fx(bm.price)} in the euro area, $${fx(bm.usd_price)} at ${r4(bm.fx_then)}: the euro ${bm.raw_pct >= 0 ? `${fx(bm.raw_pct, 0)}% over` : `${fx(-bm.raw_pct, 0)}% under`}valued.
      Allowing for income: ${r4(bm.implied_adj)}</div></div>`);
  if (ee.real) cards.push(`<div class="card"><div class="k">Real effective rate (${mon(ee.real.asof)})</div><div class="v">${fx(ee.real.v, 1)}</div>
    <div class="t">against 40 partners, after inflation: ${sg(ee.real.vs_avg)} from its average since 1999 (${fx(ee.real.avg, 1)})</div></div>`);
  if (ee.nominal) cards.push(`<div class="card"><div class="k">Nominal effective rate</div><div class="v">${fx(ee.nominal.v, 1)}</div>
    <div class="t">${sg(ee.nominal.y1)} on a year · higher than on ${ee.nominal.pctile}% of days since 1999</div></div>`);
  $("#s-fair-b").innerHTML = (err("ppp") + err("eer")) + `<div class="cards">${cards.join("")}</div>
    <div class="two"><div><div class="lc" id="pppChart"></div><div class="disc">EUR/USD against what purchasing power says, year by year (World Bank).</div></div>
      <div><div class="lc" id="reerChart"></div><div class="disc">The euro against forty trading partners after inflation (ECB, EER-40), monthly; dashed, its average.</div></div></div>
    <div class="txt"><p>${fairWords(pp, ee, p)}</p></div>`;
  const ser = [{ name: "EUR/USD", color: "var(--ink)", pts: p.series.filter((x, i) => i % 5 === 0), w: 1.4 }];
  (pp.ppp || []).forEach((x, i) => ser.push({ name: `PPP, ${x.name}`, color: i ? "var(--s3)" : "var(--s1)", pts: x.series, w: 1.6, dots: true }));
  lchart($("#pppChart"), { series: ser, yfmt: v => v.toFixed(2), tfmt: v => v.toFixed(4), height: 240, label: "EUR/USD and PPP" });
  if (ee.real) lchart($("#reerChart"), { series: [{ name: "real effective rate", color: "var(--s4)", pts: ee.real.series, w: 1.5 }],
    refs: [{ y: ee.real.avg, label: "average" }], yfmt: v => v.toFixed(0), tfmt: v => v.toFixed(1), height: 240, label: "the real effective euro" });
}

function fairWords(pp, ee, p) {
  const w = [], P = pp.ppp || [], bm = pp.bigmac;
  w.push(`<b>Purchasing power</b> is the rate at which the same basket of goods and services costs the same in both places.`);
  const cheap = P.length && P.every(x => x.gap_pct < 0), dear = P.length && P.every(x => x.gap_pct > 0);
  if (cheap) w.push(`By the World Bank's figures the euro is cheap: a euro buys more in ${P.map(x => x.name).join(" or ")} than its ${r4(p.now)} dollars buy in America (PPP ${P.map(x => r4(x.eurusd)).join(" and ")}).`);
  else if (dear) w.push(`By the World Bank's figures the euro is dear: its ${r4(p.now)} dollars buy more in America than a euro does in ${P.map(x => x.name).join(" or ")}.`);
  if (bm && bm.raw_pct != null) {
    const bmDear = bm.raw_pct > 0;
    w.push(`${(cheap && bmDear) || (dear && !bmDear) ? "The Big Mac index says the opposite" : "The Big Mac index agrees"}: a Big Mac costs ${bmDear ? "more" : "less"} in the euro area than in America at the rate of the day,
      so by burgers the euro is ${fx(Math.abs(bm.raw_pct), 0)}% ${bmDear ? "dear" : "cheap"} (${fx(Math.abs(bm.adj_pct), 0)}% ${bm.adj_pct > 0 ? "dear" : "cheap"} once the gap in incomes is allowed for).
      One product, made with local wages and rents, against a whole economy's prices: when the two disagree, the wider measure is the one to lean on.`);
  }
  if (ee.real) w.push(`Against forty trading partners after inflation the euro is ${sg(ee.real.vs_avg)} from its own average since 1999.`);
  w.push(`Currencies sit 20% from purchasing power for a decade at a time, so all of this is an anchor for the very long run, not a timing signal.`);
  return w.join(" ");
}

/* ==========================================================================
   who is betting
   ========================================================================== */
function renderPos() {
  const c = V.cot;
  if (!c) { $("#s-pos-b").innerHTML = err("cot") || `<div class="empty">No positioning.</div>`; return; }
  const bn = n => "€" + fx(Math.abs(n) * c.contract_eur / 1e9, 1) + "bn";
  const word = n => n > 0 ? "long the euro" : "short the euro";
  $("#s-pos-b").innerHTML = `<div class="cards">
      <div class="card"><div class="k">Asset managers</div><div class="v">${sg(c.am_pct, 1)} <small>of open interest</small></div>
        <div class="t">${fx(Math.abs(c.am), 0)} contracts ${word(c.am)} (${bn(c.am)}), ${c.am_w != null ? `${sg(c.am_w, 0, "")} on the week; ` : ""}higher than in ${c.am_rank}% of the last three years' weeks</div></div>
      <div class="card"><div class="k">Leveraged funds</div><div class="v">${sg(c.lf_pct, 1)} <small>of open interest</small></div>
        <div class="t">${fx(Math.abs(c.lf), 0)} contracts ${word(c.lf)} (${bn(c.lf)}), ${c.lf_w != null ? `${sg(c.lf_w, 0, "")} on the week; ` : ""}higher than in ${c.lf_rank}% of the last three years' weeks</div></div>
      <div class="card"><div class="k">Dealers</div><div class="v">${fx(c.dealer, 0)}</div><div class="t">the banks on the other side, net contracts</div></div></div>
    <div class="lc" id="cotChart"></div>
    <div class="txt"><p>The CFTC counts every CME euro future (€125,000 each) by who holds it, as of each Tuesday, published on Friday;
      this is the week to ${dmy(c.d)}. Asset managers are pension and mutual funds, slow money that leans one way for months;
      leveraged funds are hedge funds, fast money. A crowd far on one side is where the sharpest reversals come from:
      a reading in the top or bottom tenth of three years is worth noticing, not a signal on its own.</p></div>`;
  lchart($("#cotChart"), { series: [{ name: "asset managers", color: "var(--s1)", pts: c.series.map(x => [x[0], x[1]]), w: 1.4 },
    { name: "leveraged funds", color: "var(--s2)", pts: c.series.map(x => [x[0], x[2]]), w: 1.4 }], zero: true,
    yfmt: v => v.toFixed(0) + "%", tfmt: v => v.toFixed(1) + "%", height: 230, label: "net positions as a share of open interest" });
}

/* ==========================================================================
   the options
   ========================================================================== */
function renderOpt() {
  const el = $("#s-opt-b");
  if (!el) return;
  if (!O || !O.available) { el.innerHTML = `<div class="empty">${esc((O && O.why) || "No options reading.")}</div>`; return; }
  const src = O.source === "implied"
    ? (O.stale ? `the last reading of FXE's options, from the session of ${dmy(O.session)}; ${esc(O.why || "the market is shut")}` : `FXE's options, read from ${O.mode === "quotes" ? "live quotes" : "the day's trades"}`)
    : `EUR/USD's own swings over the last ${O.days} days (${fx(O.vol, 1)}% a year), as there is no options reading yet
       (Yahoo: ${esc(O.why || "no chain")}). The page reads FXE once a session while New York is open`;
  const lab = d => d < 45 ? "a month" : d < 120 ? "three months" : d < 250 ? "six months" : "a year";
  el.innerHTML = `<table class="tbl"><thead><tr><th class="l">Horizon</th><th>Volatility</th><th>2 in 3 chance between</th><th>19 in 20 between</th><th>Middle</th></tr></thead><tbody>
    ${(O.rows || []).map(r => `<tr><td class="l">${lab(r.days)} <span class="dim">${r.exp ? dmy(r.exp) : ""}</span></td><td>${fx(r.iv, 1)}%</td>
      <td>${r.q ? `${r4(r.q[1])} – ${r4(r.q[3])}` : "–"}</td><td>${r.q ? `${r4(r.q[0])} – ${r4(r.q[4])}` : "–"}</td><td>${r.q ? r4(r.q[2]) : "–"}</td></tr>`).join("")}
    </tbody></table>
    ${O.source === "implied" && O.skew && O.skew.gap != null ? `<div class="txt"><p>A month out, insuring against a <b>${O.skew.gap > 0 ? "fall" : "rise"}</b> of the euro costs more:
      the 25-delta put trades at ${fx(O.skew.put, 1)}% volatility, the call at ${fx(O.skew.call, 1)}%. ${O.skew.gap > 0 ? "The market is paying up for protection against a weaker euro." : "The market is paying up for exposure to a stronger euro."}</p></div>` : ""}
    <div class="disc">From ${src}. FXE is a trust that holds euros in New York; its price is EUR/USD less its costs, so its ranges are scaled
      to today's ${r4(O.fx_now)}. ${O.source === "implied" ? "Option prices lean toward the side people insure, so these are a yardstick, not a forecast."
      : "A range from past swings assumes the next months swing like the last three; options would price what traders fear instead."}
      ${O.source === "implied" ? `<a href="/m/OPT/FXE">OPT FXE</a> has the whole surface.` : ""}</div>`;
}

/* ==========================================================================
   your dollars, and hedging them
   ========================================================================== */
function renderBook() {
  const b = V.book;
  if (!b || !b.available) { $("#s-book-b").innerHTML = err("book") || `<div class="empty">No book.</div>`; return; }
  const cc = { USD: "var(--s1)", EUR: "var(--ink-mid)" };
  $("#s-book-b").innerHTML = `
    <div class="split">${b.ccy.map(c => `<i style="width:${c.pct}%;background:${cc[c.c] || "var(--s4)"}" title="${c.c} ${c.pct}%"></i>`).join("")}</div>
    <div class="split-key">${b.ccy.map(c => `<span><i style="background:${cc[c.c] || "var(--s4)"}"></i>${c.c} ${fx(c.pct, 1)}% · ${e0(c.v)}</span>`).join("")}</div>
    <div class="two"><div>
      <div class="sec">If EUR/USD moves</div>
      <table class="tbl"><thead><tr><th class="l">EUR/USD</th><th>To</th><th>Your dollar holdings, in euros</th></tr></thead><tbody>
      ${b.sens.map(s => `<tr><td class="l">${sg(s.move, 0)}</td><td>${r4(V.price.now * (1 + s.move / 100))}</td><td>${sgc(s.eur, 0, "")} €</td></tr>`).join("")}
      </tbody></table>
      <div class="disc">The share prices held still: only the currency moves. A stronger euro makes your dollars worth fewer euros.</div></div>
      <div><div class="sec">What the dollar has done so far</div>
      <div class="cards">
        <div class="card"><div class="k">Open positions</div><div class="v">${es0(b.unreal_fx)}</div><div class="t">of their gain or loss is the currency's</div></div>
        <div class="card"><div class="k">Sold positions</div><div class="v">${es0(b.realised_fx)}</div><div class="t">realised from the currency</div></div>
        <div class="card"><div class="k">Today</div><div class="v">${es0(b.day_fx)}</div><div class="t">from the currency alone</div></div></div>
      <div class="txt"><p>Helped most by the dollar: ${b.best.filter(x => x.fx > 0).map(x => `<a href="/t/${encodeURIComponent(x.t)}">${esc(x.t)}</a> ${es0(x.fx)}`).join(", ") || "none"}.
        Hurt most: ${b.worst.filter(x => x.fx < 0).map(x => `<a href="/t/${encodeURIComponent(x.t)}">${esc(x.t)}</a> ${es0(x.fx)}`).join(", ") || "none"}.</p>
        <p>Where a company is listed is not where it earns: Microsoft sells in euros too, Shell in dollars. This is the currency your
          shares are priced in, which is what moves your euro value day to day.</p></div></div></div>
    <div class="sec">An alert on EUR/USD</div>
    <div class="rule"><span>Tell me when EUR/USD goes</span>
      <select id="rK"><option value="eur_usd_below">below</option><option value="eur_usd_above">above</option></select>
      <input id="rV" type="number" step="0.0050" value="${(Math.round(V.price.now * 100) / 100 - 0.02).toFixed(2)}">
      <button class="tbtn on" id="rAdd">Set alert</button><span id="rOut" class="dim"></span></div>
    <div class="disc">It shows on the home screen's alerts, tested against today's rate. Big daily moves (±1%) and a fast-moving rate gap are flagged anyway.</div>`;
  $("#rAdd").onclick = async () => {
    const v = parseFloat($("#rV").value);
    if (isNaN(v)) return;
    try {
      const r = await fetch("/api/pf/alerts/rules", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker: "EURUSD", kind: $("#rK").value, value: v, note: "set on EUR" }) });
      if (!r.ok) throw new Error((await r.json()).detail || r.status);
      $("#rOut").textContent = `set: ${$("#rK").selectedOptions[0].text} ${v.toFixed(4)}`;
    } catch (e) { $("#rOut").textContent = "not set: " + e.message; }
  };
}

function renderHedge() {
  const h = V.hedge;
  if (!h || !h.series) { $("#s-hedge-b").innerHTML = err("hedge") || `<div class="empty">No hedging history.</div>`; return; }
  const n = h.now || {}, U = h.unhedged, Hd = h.hedged;
  $("#s-hedge-b").innerHTML = `
    <div class="cards">
      <div class="card"><div class="k">Hedging the dollar now</div><div class="v">${n.cost != null ? sg(-n.cost, 2) : "–"} <small>a year</small></div>
        <div class="t">selling dollars forward earns the euro's three-month rate (${fx(n.eur3)}%) instead of the dollar's (${fx(n.us3)}%)</div></div>
      <div class="card"><div class="k">S&amp;P 500, unhedged</div><div class="v">${fx(U.cagr)}% <small>a year</small></div>
        <div class="t">swings ${fx(U.vol, 1)}% a year · worst fall ${fx(U.worst, 1)}%</div></div>
      <div class="card"><div class="k">S&amp;P 500, hedged</div><div class="v">${fx(Hd.cagr)}% <small>a year</small></div>
        <div class="t">swings ${fx(Hd.vol, 1)}% a year · worst fall ${fx(Hd.worst, 1)}%</div></div></div>
    <div class="two"><div><div class="lc" id="hedgeChart"></div>
      <div class="disc">€1 in the S&amp;P 500 with dividends from ${mon(h.from + "-01")}, month by month; the gap between the lines is the currency.</div></div>
      <div><table class="tbl"><thead><tr><th class="l">Year</th><th>Unhedged</th><th>Hedged</th><th>The difference</th></tr></thead><tbody>
      ${h.years.slice(-12).reverse().map(y => `<tr><td class="l">${y.y}</td><td>${sgc(y.un)}</td><td>${sgc(y.hd)}</td><td>${sgc(y.un - y.hd)}</td></tr>`).join("")}
      </tbody></table></div></div>
    <div class="txt"><p>${hedgeWords(h)}</p></div>`;
  lchart($("#hedgeChart"), { series: [{ name: "unhedged", color: "var(--s1)", pts: h.series.map(x => [x[0], x[1]]), w: 1.5 },
    { name: "hedged", color: "var(--s3)", pts: h.series.map(x => [x[0], x[2]]), w: 1.5 }],
    yfmt: v => "€" + v.toFixed(1), tfmt: v => "€" + v.toFixed(2), height: 250, label: "the S&P 500 hedged and unhedged" });
}

function hedgeWords(h) {
  const n = h.now || {}, U = h.unhedged, Hd = h.hedged;
  const down = h.years.filter(y => y.hd < 0 && +y.y < new Date().getFullYear());
  const cush = down.filter(y => y.un > y.hd), deep = down.filter(y => y.un < y.hd);
  const ex = a => a.slice(-2).map(y => `${y.y}: ${sg(y.un)} unhedged, ${sg(y.hd)} hedged`).join("; ");
  const w = [`Hedging swaps the dollar's moves for the gap between the two interest rates. While dollar rates are higher it costs that gap
    every year (${n.cost != null ? fx(n.cost) + "% now" : "–"}).`];
  if (down.length) w.push(`In the ${down.length} calendar years the S&P 500 itself fell, the dollar cushioned the fall in ${cush.length}${cush.length ? ` (${ex(cush)})` : ""}
    and deepened it in ${deep.length}${deep.length ? ` (${ex(deep)})` : ""}.`);
  const span = s => s.worst_from ? ` (${mon(s.worst_from + "-01")} to ${mon(s.worst_to + "-01")})` : "";
  w.push(`The worst fall was ${sg(U.worst)} unhedged${span(U)} against ${sg(Hd.worst)} hedged${span(Hd)}.
    Over these ${Math.round(h.months / 12)} years the two ended ${fx(Math.abs(U.cagr - Hd.cagr), 1)} points a year apart, in the
    ${U.cagr > Hd.cagr ? "unhedged" : "hedged"} version's favour, with ${fx(Math.abs(U.vol - Hd.vol), 1)} points a year ${U.vol > Hd.vol ? "more" : "less"} swing unhedged.
    Euro-hedged share classes of the big index funds do this inside the fund, for a little more in yearly costs.`);
  return w.join(" ");
}

/* ==========================================================================
   coming up, and how it works
   ========================================================================== */
function renderCal() {
  const c = V.calendar || {}, pa = V.path || {};
  const nf = pa.fed && pa.fed.next;
  $("#s-cal-b").innerHTML = `<div class="cards">
      <div class="card"><div class="k">The ECB</div><div class="v">${c.ecb ? dmy(c.ecb.date) : "–"}</div>
        <div class="t">${c.ecb ? `in ${c.ecb.days} days` : ""}${pa.ecb ? ` · the deposit rate is ${fx(pa.ecb.now)}%` : ""}</div></div>
      <div class="card"><div class="k">The Fed</div><div class="v">${c.fed ? dmy(c.fed.date) : "–"}</div>
        <div class="t">${c.fed ? `in ${c.fed.days} days` : ""}${nf ? ` · priced ${sg(nf.change_bp, 0, "bp")}: ${fx(nf.hike, 0)}% a hike, ${fx(nf.cut, 0)}% a cut` : ""}</div></div></div>
    ${(c.week || []).length ? `<table class="tbl" style="margin-top:.5rem"><thead><tr><th class="l">When (Amsterdam)</th><th class="l">Release</th><th>Forecast</th><th>Previous</th></tr></thead><tbody>
      ${c.week.map(e => `<tr><td class="l">${dmy(e.date).slice(0, 5)} ${esc(e.time || "")}</td><td class="l"><b>${esc(e.currency)}</b> ${esc(e.title)}</td>
        <td>${esc(e.forecast || "–")}</td><td>${esc(e.previous || "–")}</td></tr>`).join("")}</tbody></table>`
      : `<div class="empty">No high-impact euro or dollar release left this week in the feed.</div>`}
    <div class="disc">High-impact releases for the euro and the dollar from the macro calendar; <a href="/m/MAC">MAC</a> has the whole week,
      the results and how EUR/USD reacted. Meeting dates as published (${esc(c.as_of || "")}).</div>`;
}

function how() {
  $("#s-how-b").innerHTML = `<div class="txt">
    <div class="sec">Sources</div>
    <ul>
      <li><b>Exchange rates</b>: the ECB's reference rates, daily since 4 January 1999, and its effective rates (EER-40, nominal daily, real by consumer prices monthly). Today's EUR/USD from Yahoo.</li>
      <li><b>Yields</b>: the US two- and ten-year from FRED and the Treasury, the Bunds from the Bundesbank, the same series BOND reads; the three-month Euribor from the ECB.</li>
      <li><b>The path</b>: fed funds and three-month €STR futures from Yahoo, chained meeting by meeting on BOND.</li>
      <li><b>Fair value</b>: the World Bank's PPP conversion factor (LCU per international dollar, CC BY 4.0); The Economist's Big Mac index (CC BY 4.0).</li>
      <li><b>Positioning</b>: the CFTC's Traders in Financial Futures report, futures only, CME Euro FX (contract 099741).</li>
      <li><b>Options</b>: FXE's chain from Yahoo through OPT, scaled from the trust's price to EUR/USD.</li>
      <li><b>Your book</b>: the dashboard: each position's currency, its gain split into price and currency, and the sold ones' currency gain.</li>
      <li><b>Hedging</b>: the S&amp;P 500 total return from Yahoo, monthly; unhedged in euros at the month's last ECB fixing, hedged as the dollar return plus the gap between Euribor and the US three-month bill (covered interest parity), each month's rate at its start.</li>
    </ul>
    <div class="sec">What each leaves out</div>
    <ul>
      <li>The rate-gap fit is three years of levels: a relationship that held, not a law. Its R² says how much it explains.</li>
      <li>Futures prices include a premium for risk, so "priced" is not quite "expected".</li>
      <li>PPP compares whole economies' prices; it explains exchange rates over decades, barely over years.</li>
      <li>The CFTC sees only CME futures, a slice of a market that trades trillions a day elsewhere.</li>
      <li>Hedging is modelled without costs or the fund's own fee; a real hedged fund rolls monthly forwards and pays for them.</li>
      <li>Nothing here is a forecast or advice. Currencies are among the hardest prices there are to predict.</li>
    </ul></div>`;
}

load(false);
