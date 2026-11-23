/* ==========================================================================
   FLOW — what institutions are doing. /api/pf/flow (pf/flow.py) is the page:
   up and down volume, hedge funds' futures, their leverage, dark pools and
   options, your holdings' short volume and their largest holders.
   /flow/refresh fetches every source now; /flow/job is its progress.
   ========================================================================== */

let V = null, POLL = null, IX = "spx", BR_N = 252, POS_N = 520, POS_K = "13874A", DK_N = 252, OPEN = null;
const ICON = { good: "▲", warn: "!", bad: "▼", info: "·" };
const KLAB = { "90down": "90% down", "90up": "90% up", "80down": "80% down", "80up": "80% up" };
const KCLS = { "90down": "d90", "90up": "u90", "80down": "d80", "80up": "u80" };
const SECTIONS = [["s-top", "Overview"], ["s-breadth", "Breadth"], ["s-pos", "Hedge funds"], ["s-lev", "Leverage"],
  ["s-dark", "Dark pools & options"], ["s-mine", "Your holdings"], ["s-src", "Sources"]];

$("#shell").innerHTML = termBar({ here: "FLOW · INSTITUTIONAL FLOW", right:
  `<a class="tbtn" href="/m/IDX" title="The indices from the inside">IDX</a><a class="tbtn" href="/m/NEWS" title="News">NEWS</a>
   <button class="tbtn" id="refBtn" title="Fetch every source now, ahead of the evening's schedule">Refresh now</button>` });
wireShell();

const p1 = (v, d = 1) => v == null || !isFinite(v) ? "–" : nf(v, d) + "%";
const sp = (v, d = 1) => v == null || !isFinite(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d) + "%";
const csp = (v, d = 1) => `<span class="${sgn(v)}">${sp(v, d)}</span>`;
const ordr = n => n == null ? "–" : ord(Math.round(n));
// billions; a trillion and more as "tn", since 3.688bn in Dutch notation reads as three billion
const bn = (v, d = 0) => v == null ? "–" : (v < 0 ? "−" : "") + "$" + (Math.abs(v) >= 1000 ? nf(Math.abs(v) / 1000, 2) + "tn" : nf(Math.abs(v), d) + "bn");
// a percentile at the edge of the record said in words
const rk = (r, from) => r == null ? "–" : r <= 0 ? `the lowest since ${String(from || "").slice(0, 4)}` : r >= 99.5 ? `the highest since ${String(from || "").slice(0, 4)}` : `${ordr(r)} percentile`;
const xx = (v, d = 1) => v == null ? "–" : nf(v, d) + "×";
const kind = k => k ? `<span class="kind ${KCLS[k]}">${KLAB[k]}</span>` : "";
const big = v => v == null ? "–" : Math.abs(v) >= 1e9 ? nf(v / 1e9, 2) + "bn" : Math.abs(v) >= 1e6 ? nf(v / 1e6, 0) + "M" : nf(v, 0);
const agoS = ts => { if (!ts) return "never"; const s = Date.now() / 1000 - ts; return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 172800 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago"; };
const pts = (d, ys, n) => { const o = Math.max(0, d.length - n); return d.slice(o).map((x, i) => [x, ys[o + i]]); };
const pairs = (arr, n = 1e9) => arr.slice(-n).map(p => [p[0], p[1]]);

async function api(path, opt) {
  const r = await fetch("/api/pf" + path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}

async function load() {
  clearTimeout(POLL);
  try { V = await api("/flow"); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load FLOW</b>${esc(e.message)}</div>`; return; }
  render();
  if ((V.job || {}).state === "running") watchJob();
}

async function refresh() {
  try { await api("/flow/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" }); }
  catch (e) { alert("Could not start: " + e.message); return; }
  watchJob();
}

async function watchJob() {
  clearTimeout(POLL);
  let j = {};
  try { j = await api("/flow/job"); } catch { }
  const box = $("#jobBox");
  if (box) box.innerHTML = j.state === "running" ? `<div class="job"><b>Collecting</b> · ${esc(j.phase || "starting")}
    <span class="dim"> · one source at a time; Cboe's and FINRA's back files take a few minutes the first time</span></div>` : "";
  if (j.state === "running") { POLL = setTimeout(watchJob, 3000); return; }
  load();
}

/* ---------- the page ---------- */
function render() {
  const b = (V.breadth || {}).spx || {}, eq = (V.positioning || {}).equity || {}, lv = V.leverage || {}, dk = V.dark || {}, pc = V.putcall || {};
  const dy = ((V.breadth || {}).diary || {}).NYSE || {}, ny = (dy.sessions || [])[0];
  const ep = b.episode || {};
  $("#app").innerHTML = `
  <div class="panel" id="s-top"><div class="body">
    <div class="fhead"><h1>What institutions are doing</h1><span class="dim">up and down volume · hedge funds' futures and leverage · dark pools · options · your shares</span>
      <span class="sp"></span><span class="dim">read ${new Date(V.now * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}</span></div>
    <nav class="secnav" id="secnav">${SECTIONS.map(([id, t]) => `<a href="#${id}" data-id="${id}">${t}</a>`).join("")}</nav>
    <div id="jobBox"></div>
    <div class="two even">
      <div><div class="ctitle">What matters now</div><div class="ins">${(V.insights || []).map(x => `<div class="fd ${x.tone}"><i>${ICON[x.tone] || "·"}</i><span>${esc(x.text)}</span></div>`).join("")
        || `<div class="dim">Nothing collected yet: press Refresh now, or wait for the evening's run.</div>`}</div></div>
      <div><div class="tiles">
        ${tile("S&P 500 up volume", b.available ? p1(b.share.slice(-1)[0], 0) : "–", null, b.available ? `${dmy(b.d.slice(-1)[0])}${b.partial ? " so far" : ""} · ${b.adv.slice(-1)[0]} up, ${b.dec.slice(-1)[0]} down ${kind(b.kind.slice(-1)[0])}` : "after IDX's next build")}
        ${tile("NYSE up volume", ny ? p1(ny.share, 0) : "–", null, ny ? `${dmy(ny.d)}${ny.final ? "" : " · live"} · ${nf(ny.ratio, 2)} to 1` : "recorded from this build on")}
        ${tile("Follow-through", ep.start ? (ep.open ? "waiting" : ep.answered ? "answered" : "none") : "–", null,
          ep.start ? (ep.open ? `${ep.sessions} session${ep.sessions === 1 ? "" : "s"} since the selling of ${dmy(ep.last)}` : ep.answered ? `${dmy(ep.answered)}, ${esc(ep.how)}` : `the selling of ${dmy(ep.start)} went unanswered`) : "no 90% down day in three years")}
        ${tile("Zweig average", b.zweig_now != null ? p1(b.zweig_now, 0) : "–", null, "thrust: from under 40% to over 61.5% in ten sessions")}
        ${tile("HF equity futures", eq.gross_now != null ? bn(eq.gross_now) : "–", null, eq.gross_now != null ? `gross · ${sp(eq.chg4, 0)} in four weeks` : "")}
        ${tile("From its peak", eq.peak52 != null ? sp(eq.peak52, 0) : "–", null, eq.peak52_d ? `the 52-week high, ${dmy(eq.peak52_d)}` : "")}
        ${tile("Form PF leverage", lv.agg ? xx(lv.agg.last) : "–", null, lv.agg ? `gross ÷ capital · ${dmy(lv.agg.d)}` : "")}
        ${tile("Top ten funds", lv.lev10 ? xx(lv.lev10.last) : "–", null, lv.lev10 ? `${xx(lv.lev10.prev)} a quarter before` : "")}
        ${tile("DIX", dk.available ? p1(dk.dix, 1) : "–", null, dk.available ? `${ordr(dk.dix_rank)} percentile of five years` : "")}
        ${tile("GEX", dk.available ? bn(dk.gex, 1) : "–", null, dk.available ? `dealers' gamma · ${ordr(dk.gex_rank)} percentile` : "")}
        ${tile("Put/call, 5 days", pc.available ? nf(pc.avg5.tot, 2) : "–", null, pc.available ? `${ordr(pc.rank5.tot)} percentile of its year` : "collecting")}
        ${tile("Single stocks", pc.available ? nf(pc.avg5.eq, 2) : "–", null, pc.available ? `equity put/call · ${ordr(pc.rank5.eq)} percentile` : "")}
      </div></div>
    </div>
  </div></div>
  ${breadthPanel()}
  ${posPanel()}
  ${levPanel()}
  ${darkPanel()}
  ${minePanel()}
  ${srcPanel()}`;
  $("#refBtn").onclick = refresh;
  drawBreadth(); drawPos(); drawLev(); drawDark();
  wireBreadth(); wirePos(); wireMine();
  wireNav(); wrapTables();
}

function wireNav() {
  const bar = document.querySelector(".term-bar");
  document.documentElement.style.setProperty("--barh", (bar ? bar.offsetHeight : 36) + "px");
  const links = $$("#secnav a");
  const io = new IntersectionObserver(es => { for (const e of es) if (e.isIntersecting) links.forEach(a => a.classList.toggle("on", a.dataset.id === e.target.id)); },
    { rootMargin: "-40% 0px -55% 0px" });
  SECTIONS.forEach(([id]) => { const el = document.getElementById(id); el && io.observe(el); });
  if (location.hash.length > 1) setTimeout(() => { const el = document.getElementById(location.hash.slice(1)); el && el.scrollIntoView(); }, 60);
}

/* ---------- 1. breadth ---------- */
function statRow(label, g) {
  const c = h => { const x = (g || {})[h]; return x ? `${csp(x.median, 1)} <span class="dim">${nf(x.pos, 0)}%</span>` : "–"; };
  return `<tr><td class="l">${label}</td><td>${(g || {})["252"] ? (g["252"].n) : "–"}</td>${["5", "21", "63", "126", "252"].map(h => `<td>${c(h)}</td>`).join("")}</tr>`;
}
function breadthPanel() {
  const B = V.breadth || {}, b = B[IX] || {}, N = B.nyse || {}, D = B.diary || {};
  const ev = (b.events || []).slice(0, 14), eps = b.episodes || [];
  const dRow = s => `<tr><td>${dmy(s.d)}${s.final ? "" : ` <span class="live">live ${esc((s.asof || "").slice(11))}</span>`}</td><td>${p1(s.share, 1)}</td><td>${nf(s.ratio, 2)}</td>
    <td>${s.adv ?? "–"} : ${s.dec ?? "–"}</td><td>${s.nh ?? "–"} / ${s.nl ?? "–"}</td><td>${kind(s.kind)}</td></tr>`;
  const S = N.stats || {};
  return `<div class="panel" id="s-breadth"><header><h2>Breadth: up and down volume</h2><span class="hint">Lowry's 90% days, the follow-through, Zweig's thrust</span></header><div class="body">
    <div class="fbar"><span class="ixsw">${["spx", "ndx"].map(k => `<button data-ix="${k}" class="${k === IX ? "on" : ""}">${k === "spx" ? "S&P 500" : "Nasdaq 100"} members</button>`).join("")}</span>
      ${rangeBar("brR", BR_N, [["6M", 126], ["1Y", 252], ["2Y", 504], ["3Y", 756]])}</div>
    ${b.available ? `<div class="two even">
      <div><div class="ctitle">Volume in rising members, % of up and down volume, each session</div><div class="lc" id="chUV"></div></div>
      <div><div class="ctitle">Zweig: advancers' share, ten-day average</div><div class="lc" id="chZw"></div></div></div>
    <div class="two even">
      <div><div class="ctitle">The lopsided days</div><table class="tbl"><thead><tr><th>Day</th><th>Kind</th><th>Up volume</th><th>Points up</th><th>Up : down</th><th>That day</th><th>Since</th></tr></thead><tbody>
        ${ev.map(e => `<tr><td>${dmy(e.d)}</td><td>${kind(e.kind)}</td><td>${p1(e.share, 1)}</td><td>${p1(e.pts, 0)}</td><td>${e.adv} : ${e.dec}</td><td>${csp(e.day, 1)}</td><td>${csp(e.since, 1)}</td></tr>`).join("")
          || `<tr><td colspan="7" class="dim">None in three years.</td></tr>`}</tbody></table></div>
      <div><div class="ctitle">Panics and their answers</div><table class="tbl"><thead><tr><th>Began</th><th>90% days</th><th>Answered</th><th>By</th><th>Sessions</th></tr></thead><tbody>
        ${eps.map(e => `<tr><td>${dmy(e.start)}</td><td>${e.downs}</td><td>${e.answered ? dmy(e.answered) : e.open ? `<span class="warnc">waiting</span>` : `<span class="dim">never</span>`}</td>
          <td class="l">${esc(e.how || "")}</td><td>${e.sessions ?? "–"}</td></tr>`).join("") || `<tr><td colspan="5" class="dim">No 90% down day in three years.</td></tr>`}</tbody></table>
        ${(b.zweig || []).length ? `<div class="ctitle">Breadth thrusts</div><table class="tbl"><thead><tr><th>Day</th><th>From</th><th>To</th></tr></thead><tbody>
          ${b.zweig.map(z => `<tr><td>${dmy(z.d)}</td><td>${p1(z.low, 0)} on ${dmy(z.low_d)}</td><td>${p1(z.ema, 0)}</td></tr>`).join("")}</tbody></table>` : `<div class="dim" style="margin-top:.5rem;font-size:.7rem">No Zweig thrust among the members in three years.</div>`}
      </div></div>` : `<div class="note"><b>No member volume yet</b>${esc(b.why || "")}</div>`}
    <div class="expl"><b>How to read it.</b> Paul Desmond of Lowry Research found that major lows were made by <b>panic selling</b>: a day when 90% or more of the
      volume, and of the points lost and gained, is in falling shares. One such day says little; what has marked a low is the <b>answer</b>, a 90% upside day, or two
      80% upside days close together, usually within a few weeks. These are the S&P 500's (or the Nasdaq 100's) own members, read from the IDX build's
      download${b.partial ? `; the last session is still trading, so no signal is read from it` : ""}. Zweig's <b>breadth thrust</b> is the other classic: the ten-day
      average of advancers going from under 40% to over 61.5% within ten sessions, a market turning from oversold to broad buying almost at once.</div>
    <div class="two even">
      <div><div class="ctitle">The NYSE and the Nasdaq composites (WSJ's market diary)</div>
        ${["NYSE", "NASDAQ"].map(ex => { const x = D[ex] || {}; return `<div class="dim" style="font-size:.66rem;margin:.3rem 0 .15rem">${ex === "NYSE" ? "NYSE" : "Nasdaq"}: ${x.n || 0} session${x.n === 1 ? "" : "s"} recorded${x.since ? " since " + dmy(x.since) : ""}</div>
          <table class="tbl"><thead><tr><th>Session</th><th>Up volume</th><th>Up : down</th><th>Advancing : declining</th><th>New highs / lows</th><th></th></tr></thead><tbody>
          ${(x.sessions || []).slice(0, 8).map(dRow).join("") || `<tr><td colspan="6" class="dim">Nothing recorded yet.</td></tr>`}</tbody></table>`; }).join("")}
        <div class="expl">The WSJ's diary is live during the session and keeps no history, so the terminal saves its final count each evening. The record starts with this build:
          the NYSE's own 90% days and thrusts appear here as it grows. The members' figures above go back three years from the first day.</div></div>
      <div><div class="ctitle">What followed on the NYSE, ${N.available ? N.from.slice(0, 4) + " to " + N.to.slice(0, 7) : "1965 to 2020"}: the S&P 500's median return, and how often it rose</div>
        ${N.available ? `<table class="tbl"><thead><tr><th>After</th><th>n</th><th>1 week</th><th>1 month</th><th>3 months</th><th>6 months</th><th>1 year</th></tr></thead><tbody>
          ${statRow("the first 90% down day of a panic", S.down)}${statRow("the follow-through that answered it", S.answered)}
          ${statRow("a 90% up day", S.up)}${S.zweig ? statRow("a Zweig breadth thrust", S.zweig) : ""}${statRow("any day (every month)", S.all)}</tbody></table>
          <div class="expl">${N.episodes} panics, ${p1(N.answered_share, 0)} of them answered within three months, after a median of ${nf(N.median_wait, 0)} sessions.
            Volume only: the archive (unicorn.us.com) has no points gained and lost, and it stops in February 2020, before the Covid crash. A panic counts once, from its first
            down day. The grey figure is the share of cases in which the index was higher.</div>
          ${(N.thrusts || []).length ? `<div class="ctitle">The NYSE's breadth thrusts</div><table class="tbl"><thead><tr><th>Day</th><th>From</th><th>To</th><th>A year later</th></tr></thead><tbody>
            ${N.thrusts.map(z => `<tr><td>${dmy(z.d)}</td><td>${p1(z.low, 0)}</td><td>${p1(z.ema, 0)}</td><td>${csp(z.f252, 1)}</td></tr>`).join("")}</tbody></table>` : ""}`
          : `<div class="note"><b>Not read yet</b>${esc(N.why || "the NYSE archive is read on the first run")}</div>`}</div></div>
  </div></div>`;
}
function wireBreadth() {
  $$("#s-breadth .ixsw button").forEach(x => x.onclick = () => { IX = x.dataset.ix; const el = $("#s-breadth"); el.outerHTML = breadthPanel(); drawBreadth(); wireBreadth(); wrapTables(); });
  wireRange("brR", n => { BR_N = n; drawBreadth(); });
}
function drawBreadth() {
  const b = (V.breadth || {})[IX] || {};
  if (!b.available || !$("#chUV")) return;
  const ten = b.share.map((x, i) => { const w = b.share.slice(Math.max(0, i - 9), i + 1).filter(v => v != null); return w.length ? Math.round(w.reduce((a, c) => a + c, 0) / w.length * 10) / 10 : null; });
  lchart($("#chUV"), { height: 230, yfmt: v => nf(v, 0) + "%", refs: [{ y: 10, label: "90% down" }, { y: 90, label: "90% up" }], series: [
    { name: "up volume", color: "var(--ink-soft)", w: 0.8, pts: pts(b.d, b.share, BR_N) },
    { name: "ten-day average", color: "var(--accent)", w: 1.6, pts: pts(b.d, ten, BR_N) },
    { name: "90% days", color: "var(--warn)", w: 0.01, dots: true, pts: pts(b.d, b.share.map((s, i) => (b.kind[i] || "").startsWith("90") ? s : null), BR_N) }] });
  lchart($("#chZw"), { height: 230, yfmt: v => nf(v, 0) + "%", refs: [{ y: 40, label: "40%" }, { y: 61.5, label: "61.5%" }], series: [
    { name: "advancers, ten-day average", color: "var(--accent)", w: 1.6, pts: pts(b.d, b.ema10, BR_N) }] });
}

/* ---------- 2. hedge funds' futures ---------- */
function posPanel() {
  const P = V.positioning || {}, eq = P.equity || {}, rows = P.rows || [];
  const grp = { eq: "Equity index futures", vol: "Volatility", ust: "Treasury futures" };
  let last = null;
  const body = rows.map(r => {
    const h = r.grp !== last ? `<tr><td colspan="8" class="l dim" style="padding-top:.5rem;font-size:.6rem;letter-spacing:.12em;text-transform:uppercase">${grp[r.grp]}</td></tr>` : "";
    last = r.grp;
    return h + `<tr class="pick ${r.code === POS_K ? "on" : ""}" data-k="${r.code}"><td class="l"><b>${esc(r.short)}</b> <span class="dim">${esc(r.name)}</span></td>
      <td>${sp(r.lf_net_pct, 1)}</td><td>${ordr(r.lf_net_rank)}</td><td>${r.lf_net_4w != null ? (r.lf_net_4w > 0 ? "+" : "") + nf(r.lf_net_4w, 1) + "pp" : "–"}</td>
      <td>${csp(r.lf_gross_4w, 1)}</td><td>${csp(r.lf_gross_peak, 0)}</td><td>${sp(r.am_net_pct, 1)}</td><td>${ordr(r.am_rank)}</td></tr>`;
  }).join("");
  const pr = P.press || [];
  return `<div class="panel" id="s-pos"><header><h2>Hedge funds' futures</h2><span class="hint">CFTC Traders in Financial Futures, weekly, Tuesday's positions published on Friday${P.asof ? " · to " + dmy(P.asof) : ""}</span></header><div class="body">
    ${rows.length ? `<div class="tiles" style="margin-bottom:.5rem">
      ${tile("Gross, equity futures", bn(eq.gross_now), null, `long plus short, at today's index levels`)}
      ${tile("Four weeks", csp(eq.chg4, 1), null, eq.chg4_rank != null ? `${ordr(eq.chg4_rank)} percentile of four-week changes` : "")}
      ${tile("From the 52-week peak", csp(eq.peak52, 0), null, eq.peak52_d ? dmy(eq.peak52_d) : "")}
      ${tile("Net", bn(eq.net_now), null, "long less short")}
      ${tile("De-grossing", eq.degrossing ? `<span class="neg">yes</span>` : "no", null, `a cut of 10%+ in four weeks, among the sharpest 5%`)}
    </div>
    <div class="fbar">${rangeBar("posR", POS_N, [["1Y", 52], ["2Y", 104], ["5Y", 260], ["Max", 520]])}</div>
    <div class="two even"><div><div class="ctitle">Leveraged funds in the equity index futures, $bn at today's levels</div><div class="lc" id="chEq"></div></div>
      <div><div class="ctitle" id="posTitle">Net, % of open interest</div><div class="lc" id="chK"></div></div></div>
    <table class="tbl" id="posTbl"><thead><tr><th class="l">Contract</th><th>Hedge funds net, % of OI</th><th>Rank, 5 years</th><th>4 weeks</th><th>Gross, 4 weeks</th>
      <th>Gross vs 52-week peak</th><th>Asset managers net</th><th>Rank</th></tr></thead><tbody>${body}</tbody></table>
    <div class="expl"><b>Leveraged funds</b> are hedge funds, CTAs and other money managers trading for themselves; <b>asset managers</b> are pension funds, mutual funds
      and insurers. <b>Gross</b> is their longs plus their shorts (spreads left out): when it falls on both sides at once, they are <b>de-grossing</b>, cutting risk rather
      than changing their view. The equity total is in dollars at today's index levels, so it moves only when positions do. Futures are one part of their books: the
      cash shares and the swaps are not in the CFTC's count. Click a row for its ten years.</div>`
      : `<div class="note"><b>Not read yet</b>The CFTC's weekly file is read on the first run.</div>`}
    <div class="ctitle">In the press: prime-book and positioning headlines, the last ten days</div>
    <div class="press">${pr.map(n => `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.headline)}<span class="dim">${esc(n.source || "")}${n.ts ? " · " + dmyTime(n.ts * 1000) : ""}</span></a>`).join("")
      || `<div class="dim" style="font-size:.72rem">No headline on hedge funds' positioning in the news log.</div>`}</div>
    <div class="expl">The figures quoted in the press, such as "the fastest de-grossing since 2020" or gross leverage of about 300%, come from Goldman Sachs's and Morgan
      Stanley's prime brokerage desks, which lend to hedge funds and see their books. They are notes to clients and are not published, so their headlines are collected
      here from the news log (NEWS tags them "flow"). The CFTC above and the OFR below are the public record of the same thing.</div>
  </div></div>`;
}
function wirePos() {
  wireRange("posR", n => { POS_N = n; drawPos(); });
  $$("#posTbl tr.pick").forEach(tr => tr.onclick = () => { POS_K = tr.dataset.k; $$("#posTbl tr.pick").forEach(x => x.classList.toggle("on", x === tr)); drawPos(); });
}
function drawPos() {
  const P = V.positioning || {}, eq = P.equity;
  if (eq && $("#chEq")) lchart($("#chEq"), { height: 230, zero: true, yfmt: v => "$" + nf(v, 0) + "bn", series: [
    { name: "gross", color: "var(--warn)", w: 1.7, pts: pts(eq.d, eq.gross, POS_N) },
    { name: "net", color: "var(--accent)", w: 1.3, pts: pts(eq.d, eq.net, POS_N) }] });
  const r = (P.rows || []).find(x => x.code === POS_K) || (P.rows || [])[0];
  if (r && $("#chK")) {
    $("#posTitle").textContent = `${r.name}: net, % of open interest`;
    lchart($("#chK"), { height: 230, zero: true, yfmt: v => nf(v, 0) + "%", series: [
      { name: "hedge funds", color: "var(--warn)", w: 1.6, pts: pts(r.series.d, r.series.lf_net_pct, POS_N) },
      { name: "asset managers", color: "var(--accent)", w: 1.3, pts: pts(r.series.d, r.series.am_net_pct, POS_N) }] });
  }
}

/* ---------- 3. leverage ---------- */
function card(k, c, f, sub) {
  return tile(k, c ? f(c.last) : "–", null, c ? (sub ? sub(c) : `${f(c.prev)} a quarter before · ${f(c.yago)} a year before`) : "");
}
function levPanel() {
  const L = V.leverage || {};
  if (!L.agg) return `<div class="panel" id="s-lev"><header><h2>Leverage</h2></header><div class="body"><div class="note"><b>Not read yet</b>The OFR's Hedge Fund Monitor is read on the first run.</div></div></div>`;
  const sl = L.scoos || {}, s1 = (sl.scoos_lev || []).slice(-1)[0], s2 = (sl.scoos_terms || []).slice(-1)[0];
  return `<div class="panel" id="s-lev"><header><h2>Leverage</h2><span class="hint">the OFR's Hedge Fund Monitor: SEC Form PF, the Fed's dealer survey, sponsored repo · Form PF to ${dmy(L.agg.d)}</span></header><div class="body">
    <div class="tiles">
      ${card("Gross ÷ capital", L.agg, v => xx(v, 1))}
      ${card("Ten largest funds", L.lev10, v => xx(v, 1))}
      ${card("The next forty", L.lev11, v => xx(v, 1))}
      ${card("All the rest", L.lev51, v => xx(v, 1))}
      ${card("Equity gross ÷ capital", L.eq_gross, v => xx(v, 2), c => rk(c.rank, c.from))}
      ${card("Equity net ÷ gross", L.eq_ratio, v => p1(v, 0), c => `${rk(c.rank, c.from)} · ${p1(c.max, 0)} at most, ${dmy(c.max_d)}`)}
      ${card("Prime brokers lend", (L.borrow || {}).pb, v => bn(v), c => `${bn(c.yago)} a year before`)}
      ${card("Repo borrowing", (L.borrow || {}).repo, v => bn(v), c => `${bn(c.yago)} a year before`)}
      ${card("Sponsored repo", L.repo, v => bn(v), c => `${dmy(c.d)} · peak ${bn(c.max)} on ${dmy(c.max_d)}`)}
      ${card("HF Treasury futures short", L.tff_card, v => bn(v), c => `ten-year equivalents · ${dmy(c.d)}`)}
      ${tile("Dealers: leverage used", s1 ? sp(s1[1], 0) : "–", null, s1 ? `net share of dealers seeing more · ${dmy(s1[0])}` : "")}
      ${tile("Dealers: terms offered", s2 ? sp(s2[1], 0) : "–", null, s2 ? `net share easing price and terms for hedge funds` : "")}
    </div>
    <div class="grid3">
      <div><div class="ctitle">Gross leverage by fund size (gross assets ÷ capital, average)</div><div class="lc" id="chLev"></div></div>
      <div><div class="ctitle">Equity exposure ÷ capital: gross and net</div><div class="lc" id="chEqx"></div></div>
      <div><div class="ctitle">Leveraged funds' Treasury futures, $bn of ten-year equivalents</div><div class="lc" id="chTff"></div></div></div>
    <div class="two even">
      <div><div class="ctitle">By strategy (gross assets ÷ capital, weighted by size)</div><table class="tbl"><thead><tr><th class="l">Strategy</th><th>Latest</th><th>Quarter before</th><th>Year before</th><th>Highest</th><th>Rank</th></tr></thead><tbody>
        ${(L.strategies || []).map(s => `<tr><td class="l">${esc(s.name)}</td><td><b>${xx(s.last, 1)}</b></td><td>${xx(s.prev, 1)}</td><td>${xx(s.yago, 1)}</td><td>${xx(s.max, 1)} <span class="dim">${dmy(s.max_d).slice(3)}</span></td><td>${ordr(s.rank)}</td></tr>`).join("")}</tbody></table></div>
      <div><div class="ctitle">Sponsored repo (FICC), $bn: cash lent through a member bank, much of it to hedge funds' basis trades</div><div class="lc" id="chRepo"></div></div></div>
    <div class="expl"><b>Form PF</b> is the confidential report every large hedge fund files with the SEC; the Office of Financial Research publishes it added up, quarterly
      and about four months late. <b>Gross ÷ capital</b> is every long and short, on and off the balance sheet, against the investors' money. The ten largest
      funds run far more leverage than the rest, mostly in relative-value trades in Treasuries. Equity net ÷ gross is how much of the equity book is a bet on the market
      rather than a hedge: low means long and short books of similar size, the shape that is cut on both sides in a de-grossing. The Treasury futures short is the
      futures leg of the <b>basis trade</b> (long cash bonds on repo, short the future); a fast fall in it, with sponsored repo, is how a rates unwind would show.
      The dealer survey (SCOOS) asks the banks' credit officers each quarter.</div>
  </div></div>`;
}
function drawLev() {
  const L = V.leverage || {};
  if (!L.agg || !$("#chLev")) return;
  const s = L.lev_s || {};
  lchart($("#chLev"), { height: 220, yfmt: v => nf(v, 0) + "×", series: [
    { name: "ten largest", color: "var(--warn)", w: 1.7, pts: pairs(s.lev10 || []) },
    { name: "the next forty", color: "var(--accent)", w: 1.3, pts: pairs(s.lev11 || []) },
    { name: "the rest", color: "var(--ink-mid)", w: 1.1, pts: pairs(s.lev51 || []) }] });
  const e = L.eq_s || {};
  if (e.d) lchart($("#chEqx"), { height: 220, yfmt: v => nf(v, 1) + "×", series: [
    { name: "gross", color: "var(--warn)", w: 1.7, pts: e.d.map((x, i) => [x, e.gross[i]]) },
    { name: "net", color: "var(--accent)", w: 1.4, pts: e.d.map((x, i) => [x, e.net[i]]) }] });
  const t = L.tff || [];
  if (t.length) lchart($("#chTff"), { height: 220, yfmt: v => "$" + nf(v, 0), series: [
    { name: "short", color: "var(--loss)", w: 1.6, pts: t.map(x => [x[0], x[2]]) },
    { name: "long", color: "var(--gain)", w: 1.3, pts: t.map(x => [x[0], x[1]]) }] });
  if ((L.repo_s || []).length) lchart($("#chRepo"), { height: 220, yfmt: v => "$" + nf(v, 0), series: [
    { name: "sponsored repo", color: "var(--accent)", w: 1.6, pts: pairs(L.repo_s) }] });
}

/* ---------- 4. dark pools and options ---------- */
function darkPanel() {
  const dk = V.dark || {}, pc = V.putcall || {};
  const prod = [["tot", "All products"], ["eq", "Single stocks"], ["ix", "Index options"], ["spx", "SPX"], ["etp", "ETFs"], ["vix", "VIX options"]];
  return `<div class="panel" id="s-dark"><header><h2>Dark pools and options</h2><span class="hint">SqueezeMetrics' DIX and GEX, daily since 2011 · Cboe's put/call, a file per session</span></header><div class="body">
    <div class="fbar">${rangeBar("dkR", DK_N, [["3M", 63], ["6M", 126], ["1Y", 252], ["2Y", 504]])}</div>
    <div class="grid3">
      <div><div class="ctitle">DIX, %${dk.available ? ` · ${p1(dk.dix, 1)}, ${ordr(dk.dix_rank)} percentile of five years` : ""}</div><div class="lc" id="chDix"></div></div>
      <div><div class="ctitle">GEX, $bn${dk.available ? ` · ${bn(dk.gex, 1)}, ${ordr(dk.gex_rank)} percentile` : ""}</div><div class="lc" id="chGex"></div></div>
      <div><div class="ctitle">Put/call ratio, five-day average${pc.available ? ` · ${pc.n} sessions read` : ""}</div><div class="lc" id="chPc"></div></div></div>
    ${pc.available ? `<table class="tbl"><thead><tr><th class="l">Put/call</th><th>${dmy(pc.d)}</th><th>Five-day average</th><th>Rank in its year</th></tr></thead><tbody>
      ${prod.map(([k, n]) => `<tr><td class="l">${n}</td><td>${nf(pc.last[k], 2)}</td><td>${nf(pc.avg5[k], 2)}</td><td>${ordr(pc.rank5[k])}</td></tr>`).join("")}</tbody></table>`
      : `<div class="note"><b>Collecting</b>Cboe publishes one file per session; a year of them is read in the background, about sixty a run.</div>`}
    <div class="expl"><b>DIX</b> is the dollar-weighted share of dark-pool volume (trades away from the exchanges, where institutions deal in size) that is short sales.
      Market makers fill buy orders by selling short, so a high DIX has meant institutions buying; SqueezeMetrics' own study found high readings were followed by better
      returns. <b>GEX</b> is the dealers' gamma from the options they have sold: high, they hedge against the market and damp moves; low or negative, they hedge with
      it and add to them. The <b>put/call ratio</b> is puts traded for each call: a high five-day average is fear, which has tended to come near lows (it is a
      contrarian sign). The single-stock ratio is the one to watch for private investors; index options are mostly institutions' hedges.</div>
  </div></div>`;
}
function drawDark() {
  const dk = V.dark || {}, pc = V.putcall || {};
  wireRange("dkR", n => { DK_N = n; drawDark(); });
  if (dk.available && $("#chDix")) {
    const c = dk.chart;
    lchart($("#chDix"), { height: 210, yfmt: v => nf(v, 0), refs: [{ y: 45, label: "45" }], series: [
      { name: "DIX", color: "var(--accent)", w: 1.4, pts: pts(c.d, c.dix, DK_N) }] });
    lchart($("#chGex"), { height: 210, zero: true, yfmt: v => nf(v, 1), series: [
      { name: "GEX", color: "var(--warn)", w: 1.4, pts: pts(c.d, c.gex, DK_N) }] });
  }
  if (pc.available && $("#chPc")) {
    const c = pc.chart;
    lchart($("#chPc"), { height: 210, yfmt: v => nf(v, 2), series: [
      { name: "all products", color: "var(--warn)", w: 1.6, pts: pts(c.d, c.tot5, DK_N) },
      { name: "single stocks", color: "var(--accent)", w: 1.3, pts: pts(c.d, c.eq5, DK_N) }] });
  }
}

/* ---------- 5. your holdings ---------- */
function minePanel() {
  const M = V.mine || {}, rows = M.rows || [];
  const zc = z => z == null ? "–" : `<span class="${z >= 2 ? "neg" : z <= -2 ? "pos" : ""}">${z > 0 ? "+" : ""}${nf(z, 1)}</span>`;
  const inst = i => !i ? `<span class="dim">not read yet</span>` : !i.n ? `<span class="dim">none listed</span>`
    : `<span class="pos">${i.added}</span> added · <span class="neg">${i.cut}</span> cut <span class="dim">of ${i.n} · ${p1(i.held, 0)} held · ${dmy(i.asof)}</span>`;
  return `<div class="panel" id="s-mine"><header><h2>Your holdings</h2><span class="hint">FINRA's daily short-sale volume · the largest institutions, from their 13F filings (Yahoo)</span></header><div class="body">
    ${rows.length ? `<table class="tbl" id="mineTbl"><thead><tr><th class="l">Share</th><th>Held as</th><th>Short, % of volume</th><th>Its 20-day average</th><th>Standard deviations</th>
      <th>Market</th><th class="l">Ten largest holders, last quarter</th></tr></thead><tbody>
      ${rows.map(r => { const s = r.short || {}; return `<tr class="pick" data-s="${esc(r.sym)}"><td class="l"><a class="tkr" href="/t/${encodeURIComponent(r.held || r.sym)}"><b>${esc(r.sym)}</b></a>
        <span class="dim">${esc((r.name || "").slice(0, 28))}</span></td><td>${esc(r.how || "")}</td><td>${p1(s.ratio, 1)}</td><td>${p1(s.avg20, 1)}</td><td>${zc(s.z)}</td>
        <td>${p1(s.mkt, 1)}</td><td class="l">${inst(r.inst)}</td></tr>
        ${OPEN === r.sym ? `<tr><td colspan="7" class="l"><div class="holders">${(r.inst && r.inst.top || []).map(h => `${esc(h.holder)} <b>${p1(h.pct, 2)}</b>
          <span class="${sgn(h.chg)}">${sp(h.chg, 1)}</span>`).join(" · ") || "No holders read yet."}</div></td></tr>` : ""}`; }).join("")}</tbody></table>
      <div class="expl">${M.market ? `The whole market's short share was ${p1(M.market.ratio, 1)} on ${dmy(M.market.d)}, over ${M.sessions} sessions recorded. ` : ""}FINRA's
        <b>short volume</b> is every trade reported as a short sale, and most of it is market makers selling to buyers and covering within the day, so about half
        of all volume is "short" on a normal day. On its own it is not a bet against the share; what matters is a share's figure against its own normal, the standard
        deviations column (two or more is unusual). The holders are each institution's last 13F, filed 45 days after the quarter: who added and who cut, read once a week,
        one share at a time. Click a row for the names.</div>`
      : `<div class="note"><b>No US shares</b>Your book has no share or turbo with a US listing.</div>`}
  </div></div>`;
}
function wireMine() {
  $$("#mineTbl tr.pick").forEach(tr => tr.onclick = e => {
    if (e.target.closest("a")) return;
    OPEN = OPEN === tr.dataset.s ? null : tr.dataset.s;
    $("#s-mine").outerHTML = minePanel(); wireMine(); wrapTables();
  });
}

/* ---------- sources ---------- */
function srcPanel() {
  const st = V.status || {}, lab = V.labels || {};
  const what = { cftc: "weekly, after Friday 21:30", ofr: "daily check; Form PF quarterly", dix: "each evening", nyse: "an archive, read every four months",
    diary: "after each close", pc: "a file per session, after 23:30", short: "a file per session, after 00:30", own: "a holding a run, weekly each" };
  return `<div class="panel" id="s-src"><header><h2>Sources</h2><span class="hint">read by a background run every ten minutes, each source only when it is due</span></header><div class="body">
    <table class="tbl"><thead><tr><th class="l">Source</th><th class="l">When</th><th>Last read</th><th class="l">Result</th></tr></thead><tbody>
    ${Object.keys(lab).map(k => { const s = st[k] || {}; return `<tr class="srcrow"><td class="l">${esc(lab[k])}</td><td class="l dim">${what[k] || ""}</td>
      <td>${agoS(s.last_ok)}</td><td class="l ${s.ok === false ? "neg" : ""}">${esc(s.ok === false ? s.error || "failed" : s.detail || "")}</td></tr>`; }).join("")}</tbody></table>
    <div class="expl">Times are Amsterdam's. Every source is free and public, and asked one request at a time: the CFTC, the Office of Financial Research, SqueezeMetrics,
      unicorn.us.com, the Wall Street Journal's market diary, Cboe and FINRA; Yahoo only for the holders, and never while the watchlist's scan or the indices' build is reading.
      Not used: FINRA's margin statistics (its site refuses scripts) and NAAIM's exposure index (its public table stopped in August).</div>
  </div></div>`;
}

load();
