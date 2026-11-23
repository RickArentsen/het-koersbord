/* ==========================================================================
   MAC — the macro calendar: the week's releases, what came out, and what it
   did to EUR/USD and to the S&P 500 future in the half hour after.

   One fetch, /api/pf/mac (pf/macro.py). The week comes from the feed the
   brief reads; actuals from FRED and the ECB; reactions from Yahoo's
   five-minute bars, recorded from build 31 on.
   ========================================================================== */

let V = null;
let F = { ccy: ["USD", "EUR"], rank: 2 };
try { const s = JSON.parse(localStorage.getItem("mac_filter") || "null"); if (s) F = { ...F, ...s }; } catch {}

const fx2 = (v, d = 2) => v == null || isNaN(v) ? "–" : Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
const sgp = (v, d = 2) => { if (v == null) return "–"; const r = Number(Math.abs(v).toFixed(d)); return `<span class="${sgn(v)}">${r === 0 ? "" : v > 0 ? "+" : "−"}${fx2(r, d)}%</span>`; };
const imp = r => `<span class="ipx r${r || 0}" title="${["holiday", "low", "medium", "high"][r || 0]} impact"><i></i><i></i><i></i></span>`;
const DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const todayIso = () => new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Amsterdam" });

$("#shell").innerHTML = termBar({ here: "MAC · MACRO CALENDAR", right:
  `<a class="tbtn" href="/m/EUR" title="The euro">EUR</a><a class="tbtn" href="/m/MKT/bonds#s-path" title="The rate path">BOND</a>
   <button class="tbtn" id="refBtn">Read actuals now</button>` });
wireShell();
$("#refBtn").onclick = async () => {
  $("#refBtn").disabled = true; $("#refBtn").textContent = "Reading…";
  try { const r = await (await fetch("/api/pf/mac/refresh", { method: "POST" })).json(); if (r.available) { V = r; render(); } }
  catch (e) { flash("Could not refresh: " + esc(e.message)); }
  $("#refBtn").disabled = false; $("#refBtn").textContent = "Read actuals now";
};

async function load() {
  try { V = await (await fetch("/api/pf/mac")).json(); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load the calendar</b>${esc(e.message)}</div>`; return; }
  if (!V.available) { $("#app").innerHTML = `<div class="note"><b>No calendar</b>${esc(V.why || "")}</div>`; return; }
  render();
}

function render() {
  $("#app").innerHTML = `
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>MAC</h1><span class="sub">the week's releases, what came out, and what it did to the euro and to American shares · Amsterdam time</span></div>
    <div class="tiles" id="tiles">${tiles()}</div>
  </div></div>
  <div class="panel" id="s-week"><header><h2>This week</h2><span class="hint">week of ${dmy(V.week_of)} · grey rows are behind us</span></header>
    <div class="body"><div class="fbar" id="filt"></div><div id="weekT"></div></div></div>
  <div class="panel" id="s-past"><header><h2>The last four weeks</h2><span class="hint">the euro's and the dollar's releases, with what came out and what moved</span></header>
    <div class="body" id="pastT"></div></div>
  <div class="panel" id="s-react"><header><h2>What moves the market</h2><span class="hint">by release, everything on record</span></header>
    <div class="body" id="reactT"></div></div>
  <div class="panel" id="s-how"><header><h2>How it works</h2><span class="hint">sources, timing, and what the record can and cannot say yet</span></header>
    <div class="body" id="how"></div></div>`;
  filters(); week(); past(); reactions(); how();
  wrapTables();
}

function tiles() {
  const n = V.next, b = V.banks || {}, fp = b.fed_priced;
  const last = (V.past || []).find(r => r.actual && r.forecast);
  const rec = (V.reactions || []).reduce((s, r) => s + r.n, 0);
  const until = n && n.ts ? Math.max(0, n.ts - Date.now() / 1000) : null;
  const left = until == null ? "" : until < 3600 ? `in ${Math.round(until / 60)} min` : until < 86400 ? `in ${Math.round(until / 3600)} h` : `in ${Math.round(until / 86400)} days`;
  return [
    tile("Next big release", n ? `${esc(n.t || "")} ${dmy(n.d).slice(0, 5)}` : "–", null, n ? `${esc(n.ccy)} ${esc(n.title)} · ${left}${n.forecast ? ` · expected ${esc(n.forecast)}` : ""}` : "nothing high-impact left", true),
    tile("This week", String(V.week.filter(r => (r.rank || 0) >= 3 && ["USD", "EUR"].includes(r.ccy)).length), null, "high-impact releases for the dollar and the euro"),
    tile("Last surprise", last ? esc(last.actual) : "–", null, last ? `${esc(last.ccy)} ${esc(last.title)} · expected ${esc(last.forecast)}` : "no actual on record yet"),
    tile("The Fed", b.fed ? dmy(b.fed.date) : "–", null, b.fed ? `in ${b.fed.days} days${fp ? ` · priced ${fp.change_bp > 0 ? "+" : ""}${fx2(fp.change_bp, 0)}bp` : ""}` : ""),
    tile("The ECB", b.ecb ? dmy(b.ecb.date) : "–", null, b.ecb ? `in ${b.ecb.days} days${b.ecb_year_bp != null ? ` · ${b.ecb_year_bp > 0 ? "+" : ""}${b.ecb_year_bp}bp priced in a year` : ""}` : ""),
    tile("Reactions on record", String(rec), null, `releases with EUR/USD and S&P 500 future moves, half an hour each`),
  ].join("");
}

function filters() {
  const all = [...new Set([...(V.currencies || []), "USD", "EUR"])].sort((a, b) => (a === "USD" ? -2 : a === "EUR" ? -1 : 0) - (b === "USD" ? -2 : b === "EUR" ? -1 : 0) || a.localeCompare(b));
  $("#filt").innerHTML = `<span><span class="lbl">Currency</span>${all.map(c => `<button class="chip ${F.ccy.includes(c) ? "on" : ""}" data-c="${c}">${c}</button>`).join(" ")}</span>
    <span><span class="lbl">Impact</span><span class="grp">${[[3, "High"], [2, "Medium up"], [1, "All"]].map(([r, l]) => `<button class="seg ${F.rank === r ? "on" : ""}" data-r="${r}">${l}</button>`).join("")}</span></span>`;
  $$("#filt .chip").forEach(b => b.onclick = () => {
    const c = b.dataset.c, i = F.ccy.indexOf(c);
    if (i >= 0) { if (F.ccy.length > 1) F.ccy.splice(i, 1); } else F.ccy.push(c);
    save(); filters(); week();
  });
  $$("#filt .seg").forEach(b => b.onclick = () => { F.rank = +b.dataset.r; save(); filters(); week(); });
}
function save() { try { localStorage.setItem("mac_filter", JSON.stringify(F)); } catch {} }

function actualCell(r) {
  if (!r.actual) return r.sourced && (r.ts || 0) < Date.now() / 1000 ? `<span class="dim" title="read from ${esc((r.actual_src || "FRED or the ECB").split(" ")[0])} once it is published">waiting</span>` : "–";
  const s = r.surprise;
  const dec = r.unit === "K" ? 0 : r.unit === "M" ? 2 : r.unit === "%" && /\.\d\d%$/.test(r.actual) ? 2 : 1;
  return `<b>${esc(r.actual)}</b>${s != null && s !== 0 ? ` <span class="${s > 0 ? "pos" : "neg"}" title="against the forecast">${s > 0 ? "+" : "−"}${fx2(Math.abs(s), dec)}${esc(r.unit || "")}</span>` : ""}
    <span class="src2" title="${esc(r.actual_src || "")}">${esc((r.actual_src || "").split(":")[0])}</span>`;
}

function rowHtml(r, nextK) {
  const done = (r.ts || 0) <= Date.now() / 1000;
  return `<tr class="${done ? "done" : ""} ${r.k === nextK ? "next" : ""} ${r.family ? "has-det" : ""}" data-k="${esc(r.k)}"
      ${r.family ? `title="Click for what moved inside it"` : ""}>
    <td class="l">${esc(r.t || "all day")}</td><td class="l">${imp(r.rank)}<span class="ccy">${esc(r.ccy)}</span> ${esc(r.title)}</td>
    <td>${esc(r.forecast || "–")}</td><td>${esc(r.previous || "–")}</td><td class="act">${actualCell(r)}</td>
    <td>${r.rx_fx != null ? sgp(r.rx_fx, 2) : "–"}</td><td>${r.rx_spx != null ? sgp(r.rx_spx, 2) : "–"}</td></tr>`;
}

function week() {
  const rows = V.week.filter(r => F.ccy.includes(r.ccy) && (r.rank || 0) >= F.rank);
  const nextK = V.next ? V.next.k : null, today = todayIso();
  const byDay = {};
  for (const r of rows) (byDay[r.d] = byDay[r.d] || []).push(r);
  const days = Object.keys(byDay).sort();
  $("#weekT").innerHTML = days.length ? `<table class="tbl"><thead><tr><th class="l">Time</th><th class="l">Release</th><th>Forecast</th><th>Previous</th><th>Actual</th>
      <th title="EUR/USD over the half hour after">EUR/USD</th><th title="The S&P 500 future over the half hour after">S&amp;P fut.</th></tr></thead><tbody>
    ${days.map(d => `<tr class="day ${d === today ? "today" : ""}"><td colspan="7">${DAYS[(new Date(d + "T12:00").getDay() + 6) % 7]} ${dmy(d).slice(0, 5)}${d === today ? " · today" : ""}</td></tr>`
      + byDay[d].map(r => rowHtml(r, nextK)).join("")).join("")}</tbody></table>`
    : `<div class="empty">Nothing this week for ${F.ccy.join(", ")} at this impact.</div>`;
  wireDetails("#weekT", V.week);
}

function past() {
  const rows = V.past || [];
  $("#pastT").innerHTML = rows.length ? `<table class="tbl"><thead><tr><th class="l">When</th><th class="l">Release</th><th>Forecast</th><th>Previous</th><th>Actual</th><th>EUR/USD</th><th>S&amp;P fut.</th></tr></thead><tbody>
    ${rows.map(r => `<tr class="${r.family ? "has-det" : ""}" data-k="${esc(r.k)}" ${r.family ? `title="Click for what moved inside it"` : ""}><td class="l">${dmy(r.d).slice(0, 5)} ${esc(r.t || "")}</td><td class="l">${imp(r.rank)}<span class="ccy">${esc(r.ccy)}</span> ${esc(r.title)}</td>
      <td>${esc(r.forecast || "–")}</td><td>${esc(r.previous || "–")}</td><td class="act">${actualCell(r)}</td>
      <td>${r.rx_fx != null ? sgp(r.rx_fx, 2) : "–"}</td><td>${r.rx_spx != null ? sgp(r.rx_spx, 2) : "–"}</td></tr>`).join("")}</tbody></table>
    <div class="disc">Releases at the same minute share one reaction: the half hour after 14:30 on a payrolls Friday is the jobs report, the unemployment rate and wages together.
      Click a release with ▸ for what moved inside it.</div>`
    : `<div class="empty">Nothing on record yet.</div>`;
  wireDetails("#pastT", V.past || []);
}

/* ---------- what moved inside a release (pf/macparts.py) ---------------------
   CPI by food, energy, goods and services; payrolls by sector; retail sales by
   store; GDP by contribution; JOLTS by industry; the euro area's HICP by
   division. Bars for the release's own period, and two years of the parts. */
const DET = {};            // by family and date: one fetch each
const DSTATE = {};         // by row: the measure shown and the series switched on
function wireDetails(sel, list) {
  const by = Object.fromEntries(list.map(r => [r.k, r]));
  $$(`${sel} tr.has-det`).forEach(tr => tr.onclick = e => {
    if (e.target.closest("a,button")) return;
    const r = by[tr.dataset.k];
    if (!r) return;
    const nx = tr.nextElementSibling;
    if (nx && nx.classList.contains("det")) { nx.remove(); tr.classList.remove("open"); return; }
    tr.classList.add("open");
    const det = document.createElement("tr");
    det.className = "det";
    det.innerHTML = `<td colspan="${tr.children.length}"><div class="mdet"><div class="dim">Reading the parts from FRED<span class="blink">_</span></div></div></td>`;
    tr.after(det);
    openDetail(r, det.querySelector(".mdet"));
  });
}

async function openDetail(r, box) {
  const key = `${r.ccy}|${r.title}|${r.d}`;
  if (!DET[key]) DET[key] = fetch(`/api/pf/mac/detail?${new URLSearchParams({ ccy: r.ccy, title: r.title, d: r.d })}`)
    .then(x => x.json()).catch(e => ({ available: false, why: String(e.message || e) }));
  const D = await DET[key];
  if (!D.available) { box.innerHTML = `<div class="empty">${esc(D.why || "No breakdown.")}</div>`; return; }
  const st = DSTATE[key] || (DSTATE[key] = { m: D.default, on: new Set((D.over.series || []).slice(0, 4).map(s => s.id)) });
  drawDetail(D, box, st);
}

function fmtM(v, unit) {
  if (v == null) return "–";
  // the sign of what shows: −0.004 is "0.00", not "−0.00"
  v = Number(v.toFixed(unit === "K" && Math.abs(v) >= 10 ? 0 : 2));
  if (unit === "K") return `${v > 0 ? "+" : v < 0 ? "−" : ""}${fx2(Math.abs(v), Math.abs(v) < 10 ? 1 : 0)}K`;
  if (unit === "M") return `${fx2(v, 2)}M`;
  if (unit === "pp") return `${v > 0 ? "+" : v < 0 ? "−" : ""}${fx2(Math.abs(v), 2)}pp`;
  return `${v > 0 ? "+" : v < 0 ? "−" : ""}${fx2(Math.abs(v), Math.abs(v) >= 10 ? 1 : 2)}%`;
}

function drawDetail(D, box, st) {
  const M = D.measures.find(m => m.k === st.m) || D.measures[0];
  const lev = M.k.startsWith("level") && M.unit !== "pp";      // a level has no sign to colour
  // the residual is only a share: it has no change of its own to show
  const rows = D.rows.filter(r => r.has && (r.id !== "_rest" || r[M.k] != null));
  const vals = rows.map(r => r[M.k]).filter(v => v != null);
  const hi = Math.max(1e-9, ...vals.map(Math.abs)), neg = vals.some(v => v < 0) && !lev;
  let firstSide = true;
  const bar = r => {
    const v = r[M.k];
    const side = r.side && firstSide ? (firstSide = false, " first") : "";
    const w = v == null ? 0 : Math.abs(v) / hi * (neg ? 50 : 100);
    const left = neg ? (v < 0 ? 50 - w : 50) : 0;
    return `<div class="mbar d${Math.min(4, r.depth)}${r.side ? " side" + side : ""}" title="${esc(r.label)}${r.w != null ? ` · weight ${fx2(r.w, 1)}%` : ""}">
      <span class="l">${esc(r.label)}</span>
      <span class="t">${neg ? `<span class="z" style="left:50%"></span>` : ""}${v != null ? `<i class="${v < 0 && !lev ? "n" : ""}" style="left:${left.toFixed(2)}%;width:${Math.max(.4, w).toFixed(2)}%"></i>` : ""}</span>
      <span class="v ${lev ? "" : sgn(v)}">${lev && M.unit !== "M" ? (v == null ? "–" : fx2(v, M.unit === "K" ? 0 : 2) + (M.unit === "K" ? "K" : "")) : fmtM(v, M.unit)}${r.w != null && M.k.startsWith("c_") ? `<small>${fx2(r.w, 1)}%</small>` : ""}</span></div>`;
  };
  const asked = D.asked && !D.exact ? `<span class="dim">the figures for the release's own period are not on FRED yet: this is the latest there is</span>` : "";
  const cards = (D.cards || []).length ? `<div class="mcards">${D.cards.map(c => `<div><div class="k">${esc(c.k)}</div><div class="v">${fx2(c.v, c.dec ?? 2)}${c.unit === "×" ? "×" : c.unit === "h" ? "h" : "%"}</div>
      <div class="t">${c.prev != null ? `${fx2(c.prev, c.dec ?? 2)} the month before` : ""}</div></div>`).join("")}</div>` : "";
  box.innerHTML = `<div class="mhd"><b>${esc(D.name)}</b><span>${esc(D.period)}</span>${asked}</div>
    ${D.say ? `<div class="msay">${esc(D.say)}</div>` : ""}${cards}
    <div class="two">
      <div><div class="fbar"><span class="grp">${D.measures.map(m => `<button class="seg ${m.k === M.k ? "on" : ""}" data-m="${m.k}">${esc(m.label)}</button>`).join("")}</span></div>
        <div class="mbars">${rows.map(bar).join("")}</div></div>
      <div><div class="fbar"><span class="lbl">Over time · ${esc(D.over.label.toLowerCase())}${D.over.unit === "%" ? ", %" : D.over.unit === "pp" ? ", points of growth" : D.over.unit === "K" ? ", thousands" : D.over.unit === "M" ? ", millions" : ""}</span></div>
        <div class="chips">${D.over.series.map(s => `<button class="chip ${st.on.has(s.id) ? "on" : ""}" data-s="${esc(s.id)}">${esc(s.label)}</button>`).join("")}</div>
        <div class="lc"></div></div>
    </div>
    <div class="disc">${[
      D.freq === "m" && D.sa ? "Seasonally adjusted month on month; the year from the same month a year before." : "",
      D.weights ? `Shares are weight × change, the weights from ${esc(D.weights)}: approximate, since the agencies reweight as prices drift, so the parts add up within a few hundredths.` : "",
      D.family === "us_retail" ? "Shares are each store type's part of last month's sales × its change." : "",
      D.family === "us_pce" ? "Goods and services are weighted by their part of last month's spending; the rows below them overlap." : "",
      D.family === "us_gdp" ? "Each part is BEA's own contribution to the annualised growth rate, in percentage points: they add up to the headline." : "",
      D.family === "us_jobs" ? "Jobs added in thousands, seasonally adjusted; the sectors add up to the total, the rest is utilities and other services." : "",
      D.note ? esc(D.note) : "",
      `From FRED${D.family === "ea_hicp" ? " (Eurostat's figures)" : ""}${(D.missing || []).length ? `; not answered: ${esc(D.missing.join(", "))}` : ""}.`].filter(Boolean).join(" ")}</div>`;
  box.querySelectorAll(".seg[data-m]").forEach(b => b.onclick = e => { e.stopPropagation(); st.m = b.dataset.m; drawDetail(D, box, st); });
  box.querySelectorAll(".chip[data-s]").forEach(b => b.onclick = e => {
    e.stopPropagation();
    st.on.has(b.dataset.s) ? st.on.delete(b.dataset.s) : st.on.add(b.dataset.s);
    drawDetail(D, box, st);
  });
  const palette = [COL.ink, COL.s1, COL.s2, COL.s3, COL.s4, COL.mid];
  const ou = D.over.unit;
  lchart(box.querySelector(".lc"), { label: D.name, endLabels: true, height: 230, zero: ou !== "M",
    yfmt: v => ou === "K" ? fx2(v, 0) + "K" : ou === "M" ? fx2(v, 1) + "M" : fx2(v, 1) + (ou === "pp" ? "" : "%"),
    tfmt: v => ou === "K" ? fx2(v, 0) + "K" : ou === "M" ? fx2(v, 2) + "M" : fx2(v, 2) + (ou === "pp" ? "pp" : "%"),
    series: D.over.series.map((s, i) => ({ name: s.label, color: palette[i % palette.length], w: i === 0 ? 2 : 1.4,
      hidden: !st.on.has(s.id), pts: s.pts })) });
}

function reactions() {
  const R = (V.reactions || []).filter(r => r.n >= 1);
  $("#reactT").innerHTML = R.length ? `<table class="tbl"><thead><tr><th class="l">Release</th><th>On record</th><th>EUR/USD, typical move</th><th>S&amp;P future, typical move</th>
      <th>EUR/USD per unit of surprise</th><th>S&amp;P per unit of surprise</th></tr></thead><tbody>
    ${R.slice(0, 30).map(r => `<tr><td class="l"><span class="ccy">${esc(r.ccy)}</span> ${esc(r.title)}</td><td>${r.n}</td>
      <td>${r.fx_abs != null ? "±" + fx2(r.fx_abs, 2) + "%" : "–"}</td><td>${r.spx_abs != null ? "±" + fx2(r.spx_abs, 2) + "%" : "–"}</td>
      <td>${r.fx_sens ? `${sgp(r.fx_sens.per_unit, 3)} per ${esc(r.unit === "K" ? "1K" : "point")} <span class="dim">(${r.fx_sens.n})</span>` : `<span class="dim">needs 4 surprises</span>`}</td>
      <td>${r.spx_sens ? `${sgp(r.spx_sens.per_unit, 3)} per ${esc(r.unit === "K" ? "1K" : "point")} <span class="dim">(${r.spx_sens.n})</span>` : `<span class="dim">needs 4 surprises</span>`}</td></tr>`).join("")}</tbody></table>
    <div class="disc">The typical move is the average size of the half-hour move, either way. Per unit of surprise is the slope of the move on actual-minus-forecast through zero,
      once four surprises are on record: a thin record says little, and this one starts in October 2026.</div>`
    : `<div class="empty">No reactions recorded yet. They are read 35 minutes after each release.</div>`;
}

function how() {
  $("#how").innerHTML = `<div class="txt">
    <div class="sec">Where each number comes from</div>
    <ul>
      <li><b>The calendar</b>: ForexFactory's weekly feed, the one the morning brief reads: time, impact, forecast and the previous figure. Amsterdam time. Each release is kept here once seen.</li>
      <li><b>The actual</b>, for the releases that move a euro investor in American shares: read from where the number is published, FRED for the American ones and FRED's copy of Eurostat's figures for the euro area's HICP (the ECB's and Eurostat's own HICP series stop at December 2025, when Bulgaria joined), and the ECB for its refinancing rate. Each is matched to the period the release is about: last month for CPI, for JOLTS the month it covers, 30 to 40 days before, the quarter just ended for GDP, the week to Saturday for claims. A euro-area flash estimate's actual arrives with the final figure about two weeks later. "ok" means the period before matches the feed's previous figure; "revised" that it has been revised since.</li>
      <li><b>The reaction</b>: Yahoo's five-minute bars for EUR/USD and the S&amp;P 500 future (ES=F, which trades at 14:30 Amsterdam time, before New York opens), from the close of the bar before the release to the close half an hour after. Read 35 minutes after each release; Yahoo keeps about two months of five-minute bars.</li>
      <li><b>What moved inside a release</b> (click a row marked ▸): its parts from FRED for the same period, CPI by food, energy, goods and services, payrolls by sector, retail sales by store, PCE and PPI by goods and services, GDP by BEA's contributions, JOLTS by industry, claims, and the euro area's HICP by its twelve divisions. A part's share is its weight × its change (BLS's December 2025 weights for CPI, Eurostat's 2025 weights for the HICP, last month's spending for retail and PCE), so the parts add up to the headline within a few hundredths. When a release's own period is not on FRED yet, the panel says so and shows the latest there is.</li>
      <li><b>Central banks</b>: the published meeting dates (${esc((V.banks || {}).as_of || "")}) and what futures price, from <a href="/m/EUR">EUR</a> and <a href="/m/MKT/bonds#s-path">BOND</a>.</li>
    </ul>
    <div class="sec">Read with care</div>
    <ul>
      <li>Half an hour holds whatever else happened in it. Releases at the same minute cannot be told apart.</li>
      <li>"Better" and "worse" depend on the release: a high CPI is bad news for bonds and often for shares, a high payrolls figure good for the economy and sometimes bad for rates. The surprise is shown only as above or below the forecast.</li>
      <li>The forecast is the feed's consensus, not an official one.</li>
      <li>With actuals for: ${(V.sources || []).map(esc).join(", ")}.</li>
    </ul>
    <div class="disc">Last read ${V.last_refresh ? dmyTime(new Date(V.last_refresh * 1000).toISOString()) : "not yet"}; it reads again every quarter of an hour.</div></div>`;
}

load();
