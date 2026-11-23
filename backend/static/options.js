/* ==========================================================================
   OPT — what the options market says about a share.

   /m/OPT          every holding with an option chain: the last reading of each
   /m/OPT/NVDA     one underlying: the surface, the skew, the term structure,
                   the range the prices imply, priced against delivered, and
                   the chain itself

   One request carries the whole analysis; only the chain table asks again,
   per expiry. The surface is surface3d.js; the line charts are kit.js.
   ========================================================================== */

const LAST = decodeURIComponent(location.pathname.replace(/\/+$/, "").split("/").pop() || "");
const SYM = /^opt$/i.test(LAST) ? null : LAST.toUpperCase();
let A = null, STRAT_OV = null;
const ST = { view: "delta", width: 0.3, sel: null, smile: null, horizon: 92, level: null,
  chainExp: null, chainAll: false, surf: null, osort: { k: "weight", dir: -1 },
  pick: null, side: "bought" };
const BOOK = "Your book is everything you hold: every open position together.";

const vol = (v, d = 0) => v == null ? "–" : (v * 100).toFixed(d) + "%";
const px = v => v == null ? "–" : nf(v, Math.abs(v) >= 1000 ? 0 : 2);
const pts = v => { if (v == null) return "–"; const r = Math.round(v); return (r > 0 ? "+" : "") + r; };
const sd = d => { const t = new Date(d + "T00:00:00Z");
  return t.toLocaleString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }) + (t.getUTCFullYear() !== new Date().getUTCFullYear() ? " " + String(t.getUTCFullYear()).slice(2) : ""); };
const big = n => n == null ? "–" : n >= 1e6 ? (n / 1e6).toFixed(1) + "M" : n >= 1e4 ? Math.round(n / 1e3) + "K" : n.toLocaleString("nl-NL");
const ago = ts => { const s = Date.now() / 1000 - ts;
  return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago" : s < 129600 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago"; };
const dLabel = d => Math.abs(d - 0.5) < 1e-6 ? "at the money" : d > 0.5 ? `${Math.round((1 - d) * 100)}Δ put` : `${Math.round(d * 100)}Δ call`;
const MODE = {
  quotes: ["live quotes", "Solved from the bid and ask of each out-of-the-money option. Yahoo's option quotes are about fifteen minutes behind."],
  trades: ["last trades", "New York is closed and Yahoo shows no bids or asks, so each contract's last trade is paired with the share price at the minute it traded. Strikes that did not trade are missing."],
};

$("#shell").innerHTML = termBar({ here: SYM ? `OPT · ${SYM}` : "OPT", right: `<button class="tbtn" id="reloadBtn">Reload</button>` });
wireShell();
$("#reloadBtn").onclick = () => SYM ? loadSym(true) : loadOverview();

const goBox = () => `<span class="go"><input id="goSym" placeholder="ticker" aria-label="Open another share's options"><button class="seg" id="goBtn">Open</button></span>`;
function wireGo() {
  const i = $("#goSym"), go = () => { const s = i.value.trim().toUpperCase(); if (s) location.assign("/m/OPT/" + encodeURIComponent(s)); };
  if (!i) return;
  i.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  $("#goBtn").onclick = go;
}

/* ==========================================================================
   the book
   ========================================================================== */
async function loadOverview() {
  let O;
  try { O = await (await fetch("/api/pf/options")).json(); } catch (e) { O = { rows: [], say: [], error: String(e.message || e) }; }
  renderOverview(O);
  if ((O.collector || {}).state === "running") setTimeout(loadOverview, 3000);
}

function renderOverview(O) {
  const J = O.collector || {}, rows = O.rows || [], read = rows.filter(r => r.iv30 != null);
  const k = ST.osort.k, dir = ST.osort.dir;
  const sorted = [...rows].sort((a, b) => {
    const va = k === "sym" ? a.sym : a[k], vb = k === "sym" ? b.sym : b[k];
    if (va == null) return 1; if (vb == null) return -1;
    return (va > vb ? 1 : va < vb ? -1 : 0) * (k === "sym" ? 1 : dir);
  });
  const cols = [["sym", "Share", "l"], ["weight", "Weight", "", "The holding's value as a share of your book. " + BOOK], ["spot", "Price"], ["iv30", "Priced, a year", "", "Thirty-day implied volatility: the move the options are priced for, as a yearly figure"],
    ["week", "A usual week", "", "One standard deviation over five trading days, from the same figure"], ["rv21", "Delivered", "", "What the share actually did over the past month, as a yearly figure"],
    ["gap", "Priced − delivered", "", "Points of volatility. Above zero, options price more movement than the share has been making"],
    ["skew", "Puts over calls", "", "25-delta, one month: how many points more a put costs than a call the same distance out. The price of protection"],
    ["slope", "Far − near", "", "Six-month minus one-month implied volatility. Below zero, something close by is being priced"],
    ["pc_vol", "Puts per call", "", "Volume in the session read"], ["rank", "Rank", "", `Where today's figure stands among the sessions stored; needs ${O.min_history} of them`], ["d", "Read", "l"]];
  $("#app").innerHTML = `
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>OPTIONS</h1><span class="sub">what the options market prices for the shares you hold</span><span class="sp"></span>${goBox()}
      <button class="seg" id="collectBtn" ${J.state === "running" ? "disabled" : ""}>${J.state === "running" ? `Reading ${J.done} of ${J.total}…` : "Read all now"}</button></div>
    <div class="prose">An option's price is a bet on how far a share can move by a date. Read together, a share's options say how much movement the market expects, how much more it fears a fall than it hopes for a rise, and whether the worry is close by or far off. Click a share for its surface. <b>Weight</b> is the holding's share of your book: everything you hold, at today's prices.</div>
  </div></div>
  ${O.error ? `<div class="panel"><div class="body"><div class="note"><b>Could not read</b>${esc(O.error)}</div></div></div>` : ""}
  ${(O.say || []).length ? `<div class="panel"><header><h2>What it says</h2><span class="hint">${read.length} of ${rows.length} read</span></header>
    <div class="body"><ul class="say">${O.say.map(s => `<li>${esc(s)}</li>`).join("")}</ul></div></div>` : ""}
  <div class="panel"><header><h2>Your shares</h2><span class="hint">click a heading to sort</span></header><div class="body">
    <div class="tscroll"><table class="tbl"><thead><tr>${cols.map(([key, lab, cl, tt]) =>
      `<th class="sort ${cl || ""}" data-k="${key}" ${tt ? `title="${esc(tt)}"` : ""}>${lab}${k === key && key !== "sym" ? (dir < 0 ? " ▾" : " ▴") : ""}</th>`).join("")}</tr></thead><tbody>
    ${sorted.map(r => `<tr class="pick" data-s="${esc(r.sym)}">
      <td><a href="/m/OPT/${encodeURIComponent(r.sym)}"><b>${esc(r.sym)}</b></a> <span class="dim">${esc((r.name || "").slice(0, 26))}</span>${r.how === "turbo" ? ` <span class="bdg warn">your turbo</span>` : r.how === "viewed" ? ` <span class="bdg info">looked at</span>` : ""}</td>
      <td>${r.weight == null ? "–" : r.weight.toFixed(1) + "%"}</td>
      ${r.iv30 == null ? `<td colspan="9" class="l dim">${r.d ? "no thirty-day figure in the last reading" : "not read yet"}</td>` : `
      <td>${px(r.spot)}</td><td><b>${vol(r.iv30)}</b></td><td>±${r.week.toFixed(1)}%</td><td>${vol(r.rv21)}</td>
      <td class="${r.gap >= 5 ? "warnc" : ""}">${pts(r.gap)}</td><td class="${r.skew >= 6 ? "warnc" : ""}">${pts(r.skew)}</td>
      <td class="${r.slope <= -3 ? "warnc" : ""}">${pts(r.slope)}</td><td>${r.pc_vol == null ? "–" : r.pc_vol.toFixed(2)}</td>
      <td>${r.rank == null ? `<span class="dim">${r.n}/${O.min_history}</span>` : ord(r.rank)}</td>`}
      <td class="l">${r.d ? `${dm(r.d)} <span class="mode ${r.mode}">${MODE[r.mode] ? MODE[r.mode][0] : r.mode}</span>` : ""}</td></tr>`).join("")}
    </tbody></table></div>
    <div class="src">One reading per share per New York session, taken while the market is open when the terminal is running (again in the last hour, so the stored figure is near the close), and from the session's last trades otherwise. Amsterdam and Frankfurt lines have no options on Yahoo and are left out; the shares your turbos track are included. Amber marks a figure worth a look: options pricing 5 points more than delivered, puts 6 points over calls, or near-dated options 3 points dearer than far-dated ones.
      ${(J.errors || []).length ? `<br>Not read: ${J.errors.map(esc).join(" · ")}` : ""}</div>
  </div></div>`;
  wireGo(); wrapTables();
  // The screener, Cboe's strategy histories and why sellers earn: built once,
  // moved back in under the table after every redraw.
  if (!STRAT_OV) { STRAT_OV = document.createElement("div"); OptStrat.overview(STRAT_OV); }
  $("#app").appendChild(STRAT_OV);
  $("#collectBtn").onclick = async () => { await fetch("/api/pf/options/collect", { method: "POST" }); setTimeout(loadOverview, 800); };
  $$("th.sort").forEach(th => th.onclick = () => { ST.osort = { k: th.dataset.k, dir: ST.osort.k === th.dataset.k ? -ST.osort.dir : -1 }; renderOverview(O); });
  $$("tr.pick").forEach(tr => tr.onclick = e => { if (!e.target.closest("a")) location.assign("/m/OPT/" + encodeURIComponent(tr.dataset.s)); });
}

/* ==========================================================================
   one share
   ========================================================================== */
async function loadSym(fresh) {
  $("#app").innerHTML = `<div class="center">Reading the chain<span class="blink">_</span></div>`;
  try { A = await (await fetch(`/api/pf/options/${encodeURIComponent(SYM)}${fresh ? "?fresh=true" : ""}`)).json(); }
  catch (e) { A = { available: false, why: String(e.message || e) }; }
  if (!A.available) {
    $("#app").innerHTML = `<div class="panel"><div class="body">
      <div class="hdr"><h1>${esc(SYM)}</h1><span class="sp"></span>${goBox()}</div>
      <div class="note"><b>No options to read</b>${esc(A.why || "no answer")}${A.alt ? ` <a href="/m/OPT/${encodeURIComponent(A.alt)}">Open ${esc(A.alt)} ›</a>` : ""}</div>
      <div class="hd-links"><a href="/m/OPT">‹ every share</a><a href="/t/${encodeURIComponent(SYM)}">${esc(SYM)} research ›</a></div></div></div>`;
    wireGo();
    return;
  }
  ST.sel = null; ST.smile = null; ST.chainExp = null; ST.level = null; ST.pick = null; CT = null;
  scaffold();
}

function cmDay() { return A.cm["30"]; }

function tiles() {
  const R = A.realised || {}, c30 = cmDay(), c180 = A.cm["180"], t = [], F = A.flow, P = A.position;
  t.push(tile("Priced move, a year", vol(c30), null, c30 ? `±${(c30 / Math.sqrt(252) * 100).toFixed(1)}% a day over the next month` : "", true,
    "Thirty-day implied volatility at the money"));
  t.push(tile("Delivered, past month", vol(R.rv21), null, R.pct != null ? `${pctl(R.pct)} of the past two years` : "", false, "Realised volatility of daily closes, 21 sessions"));
  if (c30 && R.rv21) { const g = (c30 - R.rv21) * 100;
    t.push(tile("Priced − delivered", pts(g) + " pts", null, Math.abs(g) < 3 ? "about what it has been doing" : g > 0 ? "options price more than it has moved" : "options price less than it has moved")); }
  const sk = A.skew30 || {};
  t.push(tile("Puts over calls", sk.gap == null ? "–" : pts(sk.gap) + " pts", null, sk.put ? `put ${vol(sk.put)} · call ${vol(sk.call)} · 25-delta, a month` : "", false,
    "How much more a put costs than a call the same distance from the price, in points of volatility"));
  t.push(tile("Near / far", `${vol(c30)} / ${vol(c180)}`, null, "one month / six months"));
  t.push(tile("Puts per call", F.pc_vol == null ? "–" : F.pc_vol.toFixed(2), null, `${big(F.call_vol)} calls · ${big(F.put_vol)} puts traded`));
  if (A.earnings) {
    const days = Math.round((MS(A.earnings) - Date.now()) / 864e5), e = A.expiries.find(r => r.exp >= A.earnings);
    t.push(`<a class="tile" href="/m/ERN/${encodeURIComponent(SYM)}"><div class="k">Results</div><div class="v">${dm(A.earnings)}</div>
      <div class="s">${days >= 0 ? `in ${days} days` : `${-days} days ago`}${e && days >= 0 ? ` · ±${e.move.toFixed(0)}% priced by ${dm(e.exp)}` : ""}</div></a>`);
  }
  if (P && P.value_eur) t.push(tile("Your position", eur(P.value_eur), null, c30 ? `${(P.weight || 0).toFixed(1)}% of your book · a usual month ±${eur(P.value_eur * c30 * Math.sqrt(30 / 365))}` : "", false,
    (P.as !== SYM ? `Held as ${P.as}. ` : "") + BOOK));
  const n = (A.history || []).filter(h => h.iv30).length;
  t.push(tile("History", `${n} session${n === 1 ? "" : "s"}`, null, n >= A.min_history ? "enough for a rank" : `a rank needs ${A.min_history}`, false,
    "Yahoo keeps no history of implied volatility; one reading is stored per session from the day this page was built"));
  return t.join("");
}

function scaffold() {
  const chg = A.prev_close ? (A.spot / A.prev_close - 1) * 100 : null, m = MODE[A.mode] || [A.mode, ""];
  $("#app").innerHTML = `
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>${esc(SYM)}</h1><span class="sub">${esc(A.name || "")} · <b style="color:var(--ink)">${px(A.spot)}</b> ${esc(A.currency)}${chg == null ? "" : ` <span class="${sgn(chg)}">${pct(chg)}</span>`}</span>
      <span class="sp"></span>${goBox()}</div>
    <div class="tiles">${tiles()}</div>
    <div class="hd-links"><span><span class="mode ${A.mode}" title="${esc(m[1])}">${m[0]}</span> <span class="dim">session of ${dmy(A.session)} · read ${ago(A.asof)}</span></span>
      <a href="/m/OPT">‹ every share</a><a href="/t/${encodeURIComponent(SYM)}">${esc(SYM)} research ›</a><a href="/m/ERN/${encodeURIComponent(SYM)}">earnings ›</a><a href="/m/SPLC/${encodeURIComponent(SYM)}">supply chain ›</a></div>
  </div></div>
  <div class="og">
    <div class="panel" id="s-surface"><header><h2>The volatility surface</h2><span class="hint">drag to turn · double-click to reset · click an expiry to follow it below</span></header>
      <div class="body">
        <div class="fbar"><span class="grp" id="vView"><button class="seg on" data-v="delta">By delta</button><button class="seg" data-v="money">By strike</button></span>
          <span class="lbl" style="margin-left:.5rem;visibility:hidden" id="wLbl">Strikes</span><span class="grp" id="vWidth" style="visibility:hidden"><button class="seg" data-v="0.2">±20%</button><button class="seg on" data-v="0.3">±30%</button><button class="seg" data-v="1">all</button></span>
          <span class="sp"></span><span class="grp" id="vCam"><button class="seg" data-v="home">Corner</button><button class="seg" data-v="front">Front</button><button class="seg" data-v="side">Side</button><button class="seg" data-v="top">Above</button></span></div>
        <div id="surf"></div>
        <div class="s3-key"><span>lower</span><i></i><span>higher</span><span id="surfAxes" style="margin-left:.8rem"></span><span class="sp"></span><span id="surfSel"></span></div>
        <div class="src">${esc(m[1])} Expiries are spaced by the square root of time, so the first months are not squeezed into a sliver; those inside a week are left off the sheet and shown under Skew. Where no strike priced, there is no surface.</div>
      </div></div>
    <div class="stack">
      <div class="panel"><header><h2>What it says</h2></header><div class="body"><ul class="say">${(A.say || []).map(s => `<li>${esc(s)}</li>`).join("")}</ul></div></div>
      <div class="panel"><header><h2>How to read it</h2></header><div class="body">${howSurface()}</div></div>
    </div>
  </div>
  <div class="cols c-2">
    <div class="panel" id="s-skew"><header><h2>Skew</h2><span class="hint">one expiry at a time, across strikes</span></header>
      <div class="body"><div class="fbar" id="smileBar"></div><div class="lc" id="smileChart"></div>
      <div class="src" id="smileSrc"></div></div></div>
    <div class="panel" id="s-term"><header><h2>Term structure</h2><span class="hint">at the money, across expiries</span></header>
      <div class="body"><div class="lc" id="termChart"></div>
      <div class="src">Implied volatility at the money for each expiry, one step per expiry, with the 25-delta put and call either side. ${A.earnings ? `The amber point is the first expiry after the next results, ${dmy(A.earnings)}: from there on, every expiry carries that day's move.` : ""}</div></div></div>
  </div>
  <div class="panel" id="s-range"><header><h2>The range the prices imply</h2><span class="hint">the odds built into option prices: a yardstick, not a forecast</span></header>
    <div class="body"><div class="fbar"><span class="lbl">Horizon</span><span class="grp" id="hz">${[[31, "1M"], [92, "3M"], [183, "6M"], [366, "1Y"], [9999, "All"]].map(([d, l]) => `<button class="seg ${d === ST.horizon ? "on" : ""}" data-v="${d}">${l}</button>`).join("")}</span></div>
      <div id="cone"></div>
      <div class="odds"><span class="lbl">Odds of</span><input id="lvl" inputmode="decimal" aria-label="A price level"><span class="dim">${esc(A.currency)}</span>
        <span class="grp" id="lvlPre"></span></div>
      <div class="tscroll" id="oddsBox"></div>
      <div class="src">The dark band holds two outcomes in three, the light band nineteen in twenty, read off the options expiring on each date. These odds lean pessimistic, because protection against a fall costs extra. "Touched on the way" is a rule of thumb: about twice the chance of finishing beyond the level.</div>
    </div></div>
  <div class="cols c-2">
    <div class="panel" id="s-hist"><header><h2>Priced against delivered</h2><span class="hint">implied volatility next to what the share did</span></header>
      <div class="body"><div class="lc" id="histChart"></div><div class="tscroll" id="histBox" style="margin-top:.5rem"></div>
      <div class="src">Delivered is the yearly-rate swing of daily closes over 21 sessions. Priced is stored once per session, starting the day this page was built, because nobody publishes it after the fact: ${(A.history || []).length} session${(A.history || []).length === 1 ? "" : "s"} so far.</div></div></div>
    <div class="panel" id="s-flow"><header><h2>Where the trading is</h2><span class="hint">${A.mode === "trades" ? "the last session" : "today so far"}</span></header>
      <div class="body" id="flowBox"></div></div>
  </div>
  <div class="panel" id="s-chain"><header><h2>The chain</h2><span class="hint">calls on the left, puts on the right · click a call or a put to work it out</span></header>
    <div class="body"><div class="fbar"><span class="lbl">Expiry</span><select class="seg" id="chainExp">${A.flow.expiries.map(e => `<option value="${e.exp}">${sd(e.exp)}</option>`).join("")}</select>
      <button class="seg" id="chainAll">All strikes</button><span class="sp"></span><span class="dim" id="chainTot" style="font-size:.68rem"></span></div>
      <div class="chaincap"><span>Calls</span><span>Puts</span></div>
      <div class="chainbox" id="chainBox"><div class="dim ld">Loading<span class="blink">_</span></div></div>
      <div class="grp" style="margin-top:.7rem">How to read it</div>
      ${howChain()}</div></div>
  <div class="panel" id="s-contract" style="display:none"></div>
  <div class="panel" id="s-strat"></div>`;
  wireGo();
  ST.surf = new Surface3D($("#surf"), { label: `${SYM} implied volatility surface`,
    tip: (n, j, v) => { const i = surfRows()[n].i, r = A.expiries[i];
      if (ST.view === "money") { const mny = money()[j]; return `<b>${v.toFixed(1)}%</b> a year<br>${sd(r.exp)} · ${Math.round(r.days)} days<br>strike ${px(mny * r.fwd)} · ${(mny * 100).toFixed(1)}% of forward`; }
      const mm = A.m_delta[i][j]; return `<b>${v.toFixed(1)}%</b> a year<br>${sd(r.exp)} · ${Math.round(r.days)} days<br>${dLabel(A.deltas[j])}${mm ? ` · strike ${px(mm * r.fwd)}` : ""}`; },
    onPick: h => { const i = surfRows()[h.i].i; ST.sel = ST.sel === i ? null : i; ST.smile = null; drawSurface(); drawSmile(); } });
  const seg = (id, fn) => $$(`#${id} .seg`).forEach(b => b.onclick = () => { $$(`#${id} .seg`).forEach(x => x.classList.toggle("on", x === b)); fn(b.dataset.v); });
  seg("vView", v => { ST.view = v; ST.smile = null; $("#vWidth").style.visibility = $("#wLbl").style.visibility = v === "money" ? "" : "hidden"; drawSurface(); drawSmile(); });
  seg("vWidth", v => { ST.width = +v; drawSurface(); drawSmile(); });
  $$("#vCam .seg").forEach(b => b.onclick = () => { const c = { home: [-0.62, 0.52], front: [0, 0.3], side: [-Math.PI / 2, 0.3], top: [-0.3, 1.45] }[b.dataset.v]; ST.surf.view(c[0], c[1]); });
  seg("hz", v => { ST.horizon = +v; drawCone(); drawOdds(); });
  drawSurface(); drawSmile(); drawTerm(); drawCone(); wireOdds(); drawHist(); drawFlow();
  OptStrat.builder($("#s-strat"), SYM);
  $("#chainExp").onchange = e => { ST.chainExp = e.target.value; loadChain(); };
  $("#chainAll").onclick = () => { ST.chainAll = !ST.chainAll; $("#chainAll").classList.toggle("on", ST.chainAll); renderChain(); };
  const first = A.expiries.find(r => r.days >= 6) || A.expiries[0];
  ST.chainExp = first ? first.exp : A.flow.expiries[0].exp;
  $("#chainExp").value = ST.chainExp;
  loadChain();
  wrapTables();
  if (location.hash) { const el = document.getElementById(location.hash.slice(1)); if (el) el.scrollIntoView(); }
}

/* ---------- the surface ----------------------------------------------------------- */
/* Expiries inside a week are all gamma: their far strikes price at two and
   three times everything else and flatten the rest of the sheet. They stay in
   the skew chart and the chain. */
function surfRows() {
  const E = A.expiries.map((r, i) => ({ r, i })), keep = E.filter(o => o.r.days >= 6);
  return keep.length >= 4 ? keep : E;
}

function cols() {
  // the columns of the strike grid inside the chosen width
  const out = [];
  A.money.forEach((m, j) => { if (Math.abs(m - 1) <= ST.width + 1e-9) out.push(j); });
  return out;
}
function money() { return cols().map(j => A.money[j]); }

function drawSurface() {
  const E = surfRows();
  let data;
  if (ST.view === "money") {
    const cs = cols();
    data = { x: cs.map(j => Math.round(A.money[j] * 1000) / 10),
      rows: E.map(o => ({ pos: Math.sqrt(o.r.days), label: sd(o.r.exp), z: cs.map(j => A.z_money[o.i][j] == null ? null : A.z_money[o.i][j] * 100) })),
      xFmt: v => v + "%" };
  } else {
    data = { x: A.deltas.map((d, j) => j),
      rows: E.map(o => ({ pos: Math.sqrt(o.r.days), label: sd(o.r.exp), z: A.z_delta[o.i].map(v => v == null ? null : v * 100) })),
      xFmt: j => dLabel(A.deltas[j]).replace("at the money", "ATM"), xTicks: [0, 3, 8, 13, 16] };
  }
  data.zFmt = v => v + "%"; data.zTitle = "implied volatility, a year";
  $("#surfAxes").textContent = ST.view === "money" ? "across: strike, as a share of the forward price · back: expiry" : "across: puts, at the money, calls · back: expiry";
  const at = E.findIndex(o => o.i === ST.sel);
  data.sel = at >= 0 ? at : null;
  ST.surf.set(data);
  $("#surfSel").textContent = ST.sel == null ? "" : `followed: ${sd(A.expiries[ST.sel].exp)}`;
}

/* ---------- skew -------------------------------------------------------------------- */
function defaultSmile() {
  const E = A.expiries, pick = new Set();
  for (const d of [7, 30, 90, 180, 365]) {
    let best = null;
    E.forEach((r, i) => { if (best == null || Math.abs(r.days - d) < Math.abs(E[best].days - d)) best = i; });
    if (best != null) pick.add(E[best].exp);
  }
  if (ST.sel != null) pick.add(E[ST.sel].exp);
  return [...pick];
}

function drawSmile() {
  const E = A.expiries;
  if (!ST.smile) ST.smile = defaultSmile();
  const palette = [COL.s1, COL.s2, COL.s3, COL.s4, COL.mid, "var(--warn)", COL.s1, COL.s2];
  const shown = E.map((r, i) => ({ r, i })).filter(o => ST.smile.includes(o.r.exp) || o.i === ST.sel);
  const offer = new Set(ST.smile);
  for (const d of [2, 7, 14, 30, 45, 60, 90, 135, 180, 270, 365, 540, 730]) {
    let best = 0;
    E.forEach((r, i) => { if (Math.abs(r.days - d) < Math.abs(E[best].days - d)) best = i; });
    offer.add(E[best].exp);
  }
  const list = E.map((r, i) => ({ r, i })).filter(o => offer.has(o.r.exp) || o.i === ST.sel)
    .map(o => ({ k: o.r.exp, name: sd(o.r.exp), color: o.i === ST.sel ? COL.ink : palette[Math.max(0, shown.findIndex(s => s.i === o.i)) % palette.length] }));
  $("#smileBar").innerHTML = `<span class="lbl">Expiries</span>${chips("smileChips", list, ST.smile)}`;
  wireChips("smileChips", ST.smile, drawSmile);
  let cats, series;
  if (ST.view === "money") {
    const cs = cols();
    // a label every five percent; the points in between say theirs on hover
    cats = cs.map(j => Math.round(A.money[j] * 1000) % 50 ? "" : Math.round(A.money[j] * 100) + "%");
    series = shown.map((o, n) => ({ name: sd(o.r.exp), color: o.i === ST.sel ? COL.ink : palette[n % palette.length], w: o.i === ST.sel ? 2.2 : 1.5,
      pts: cs.map((j, x) => [x, A.z_money[o.i][j] == null ? null : A.z_money[o.i][j] * 100, `${(A.money[j] * 100).toFixed(1)}% · strike ${px(A.money[j] * o.r.fwd)}`]) }));
  } else {
    cats = A.deltas.map(d => dLabel(d).replace("at the money", "ATM"));
    series = shown.map((o, n) => ({ name: sd(o.r.exp), color: o.i === ST.sel ? COL.ink : palette[n % palette.length], w: o.i === ST.sel ? 2.2 : 1.5,
      pts: A.deltas.map((d, x) => [x, A.z_delta[o.i][x] == null ? null : A.z_delta[o.i][x] * 100, A.m_delta[o.i][x] ? `strike ${px(A.m_delta[o.i][x] * o.r.fwd)}` : ""]) }));
  }
  lchart($("#smileChart"), { xType: "cat", cats, height: 260, label: "Implied volatility by strike", noLegend: true,
    yfmt: v => v.toFixed(0) + "%", tfmt: v => v.toFixed(1) + "%", series, empty: "No expiry chosen" });
  const ref = shown.find(o => o.r.m_p25 && o.r.m_c25 && o.r.days >= 20) || shown.find(o => o.r.m_p25 && o.r.m_c25);
  $("#smileSrc").innerHTML = ref ? `For ${sd(ref.r.exp)}: the 25-delta put sits at ${px(ref.r.m_p25 * ref.r.fwd)} (${((ref.r.m_p25 - 1) * 100).toFixed(0)}%) and is priced at ${vol(ref.r.p25, 1)}; the 25-delta call at ${px(ref.r.m_c25 * ref.r.fwd)} (+${((ref.r.m_c25 - 1) * 100).toFixed(0)}%) at ${vol(ref.r.c25, 1)}. A curve that is higher on the left than on the right is the usual shape for a share: falls are fast and insured against.`
    : "Each line is one expiry: implied volatility at every strike that priced.";
}

/* ---------- term structure ------------------------------------------------------------ */
function drawTerm() {
  const E = A.expiries, mark = A.earnings ? E.findIndex(r => r.exp >= A.earnings) : -1;
  lchart($("#termChart"), { xType: "cat", cats: E.map(r => sd(r.exp)), label: "Implied volatility by expiry", height: 260, endLabels: true,
    yfmt: v => v.toFixed(0) + "%", tfmt: v => v.toFixed(1) + "%",
    series: [{ name: "At the money", short: "ATM", color: COL.ink, w: 2.1, pts: E.map((r, i) => [i, r.atm * 100, `${Math.round(r.days)} days`]) },
      { name: "25-delta put", color: COL.s2, w: 1.3, pts: E.map((r, i) => [i, r.p25 ? r.p25 * 100 : null]) },
      { name: "25-delta call", color: COL.s1, w: 1.3, pts: E.map((r, i) => [i, r.c25 ? r.c25 * 100 : null]) },
      ...(mark >= 0 ? [{ name: "First expiry after results", color: "var(--warn)", w: 3, noEnd: true, pts: [[mark, E[mark].atm * 100]] }] : [])] });
}

/* ---------- the range ------------------------------------------------------------------- */
function horizonRows() {
  const E = A.expiries.filter(r => r.q);
  const rows = E.filter(r => r.days <= ST.horizon);
  return rows.length >= 2 ? rows : E.slice(0, Math.min(3, E.length));
}

function drawCone() {
  const host = $("#cone"); if (!host) return;
  if (!horizonRows().length) { host.innerHTML = `<div class="lc-empty">No expiry priced enough strikes to draw a range.</div>`; return; }
  const rows = horizonRows(), R = A.realised || {}, hist = (R.closes || []).slice(-Math.max(60, Math.min(250, Math.round(rows[rows.length - 1].days * 0.9))));
  const W = Math.max(300, Math.round(host.clientWidth)), H = W < 620 ? 260 : 330, M = { l: 8, r: 58, t: 12, b: 22 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b, today = new Date().toISOString().slice(0, 10);
  const t0 = hist.length ? MS(hist[0][0]) : Date.now() - 60 * 864e5, tN = MS(today), t1 = MS(rows[rows.length - 1].exp);
  let lo = Math.min(A.spot, ...hist.map(h => h[1]), ...rows.map(r => r.q[0])), hi = Math.max(A.spot, ...hist.map(h => h[1]), ...rows.map(r => r.q[4]));
  const lv = [...(A.levels || [])];
  if (ST.level && !lv.some(l => Math.abs(l.v - ST.level) < 0.005)) lv.push({ k: "typed", label: "level", v: ST.level });
  for (const l of lv) if (l.v > lo * 0.6 && l.v < hi * 1.5) { lo = Math.min(lo, l.v); hi = Math.max(hi, l.v); }
  const yt = nice(lo, hi, 6), X = t => M.l + (t - t0) / (t1 - t0) * pw, Y = v => M.t + (yt.hi - v) / (yt.hi - yt.lo) * ph;
  let g = "";
  for (const t of yt.ticks) g += `<line class="gl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(t).toFixed(1)}" y2="${Y(t).toFixed(1)}"/><text class="ax" x="${M.l + pw + 6}" y="${(Y(t) + 3).toFixed(1)}">${px(t)}</text>`;
  for (const [t, lab] of timeTicks(t0, t1, Math.max(3, Math.floor(pw / 80)))) g += `<text class="ax" x="${X(t).toFixed(1)}" y="${H - 6}" text-anchor="middle">${esc(lab)}</text>`;
  const band = (a, b, op) => { const up = [[tN, A.spot], ...rows.map(r => [MS(r.exp), r.q[b]])], dn = [[tN, A.spot], ...rows.map(r => [MS(r.exp), r.q[a]])].reverse();
    return `<polygon points="${[...up, ...dn].map(p => X(p[0]).toFixed(1) + "," + Y(p[1]).toFixed(1)).join(" ")}" fill="var(--accent)" opacity="${op}"/>`; };
  g += band(0, 4, 0.13) + band(1, 3, 0.2);
  g += `<path d="${[[tN, A.spot], ...rows.map(r => [MS(r.exp), r.q[2]])].map((p, i) => (i ? "L" : "M") + X(p[0]).toFixed(1) + " " + Y(p[1]).toFixed(1)).join("")}" fill="none" stroke="var(--accent)" stroke-width="1.2" stroke-dasharray="4 3"/>`;
  for (const r of rows) g += `<line x1="${X(MS(r.exp)).toFixed(1)}" x2="${X(MS(r.exp)).toFixed(1)}" y1="${Y(r.q[4]).toFixed(1)}" y2="${Y(r.q[0]).toFixed(1)}" stroke="var(--accent)" stroke-width="1" opacity=".35"/>`;
  if (hist.length) g += `<path d="${hist.map((h, i) => (i ? "L" : "M") + X(MS(h[0])).toFixed(1) + " " + Y(h[1]).toFixed(1)).join("")}" fill="none" stroke="var(--ink)" stroke-width="1.6" stroke-linejoin="round"/>`;
  g += `<line x1="${X(tN).toFixed(1)}" x2="${X(tN).toFixed(1)}" y1="${M.t}" y2="${M.t + ph}" stroke="var(--rule-strong)" stroke-width="1"/>
    <circle cx="${X(tN).toFixed(1)}" cy="${Y(A.spot).toFixed(1)}" r="3.4" fill="var(--ink)" stroke="var(--surface)" stroke-width="1.5"/>`;
  if (A.earnings && MS(A.earnings) > tN && MS(A.earnings) < t1) g += `<line x1="${X(MS(A.earnings)).toFixed(1)}" x2="${X(MS(A.earnings)).toFixed(1)}" y1="${M.t}" y2="${M.t + ph}" stroke="var(--warn)" stroke-width="1" stroke-dasharray="2 4"/>
    <text class="lv" x="${(X(MS(A.earnings)) + 4).toFixed(1)}" y="${M.t + 10}" fill="var(--warn)">results</text>`;
  const lcol = { cost: "var(--ink-mid)", target: "var(--warn)", typed: "var(--gain)" };
  for (const l of lv) { if (l.v < yt.lo || l.v > yt.hi) continue;
    g += `<line x1="${M.l}" x2="${M.l + pw}" y1="${Y(l.v).toFixed(1)}" y2="${Y(l.v).toFixed(1)}" stroke="${lcol[l.k]}" stroke-width="1" stroke-dasharray="5 4" opacity=".85"/>
      <text class="lv" x="${M.l + 4}" y="${(Y(l.v) - 4).toFixed(1)}" fill="${lcol[l.k]}">${esc(l.label)} ${px(l.v)}</text>`; }
  g += `<rect class="hov" x="${X(tN).toFixed(1)}" y="${M.t}" width="${(M.l + pw - X(tN)).toFixed(1)}" height="${ph}" fill="transparent"/>`;
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(SYM)}: price history and the range option prices imply">${g}</svg><div class="lc-tip" style="display:none"></div>`;
  const svg = host.querySelector("svg"), tip = host.querySelector(".lc-tip");
  svg.querySelector(".hov").addEventListener("mousemove", ev => {
    const b = svg.getBoundingClientRect(), t = t0 + ((ev.clientX - b.left) * (W / b.width) - M.l) / pw * (t1 - t0);
    const r = rows.reduce((a, c) => Math.abs(MS(c.exp) - t) < Math.abs(MS(a.exp) - t) ? c : a, rows[0]);
    const f = v => `${px(v)} <span class="dim">(${pc((v / A.spot - 1) * 100, 0)})</span>`;
    tip.innerHTML = `<div class="h">by ${dmy(r.exp)} · ${Math.round(r.days)} days</div>
      <div class="r"><span>two in three</span><b>${f(r.q[1])} – ${f(r.q[3])}</b></div>
      <div class="r"><span>nineteen in twenty</span><b>${f(r.q[0])} – ${f(r.q[4])}</b></div>
      <div class="r"><span>middle</span><b>${f(r.q[2])}</b></div>`;
    tip.style.display = "";
    const sx = X(MS(r.exp)) / W * b.width, tw = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(host.clientWidth - tw, sx - tw - 12)) + "px"; tip.style.top = "6px";
  });
  svg.querySelector(".hov").addEventListener("mouseleave", () => { tip.style.display = "none"; });
}

function below(r, level) {
  // chance of finishing below `level`, from the stored points of the distribution
  const c = r.cdf; if (!c || !c.length) return null;
  if (level <= c[0][0]) return c[0][1] * level / c[0][0];
  if (level >= c[c.length - 1][0]) return c[c.length - 1][1] + (1 - c[c.length - 1][1]) * (1 - c[c.length - 1][0] / level);
  for (let i = 1; i < c.length; i++) if (level <= c[i][0]) return c[i - 1][1] + (c[i][1] - c[i - 1][1]) * (level - c[i - 1][0]) / (c[i][0] - c[i - 1][0]);
  return null;
}

function wireOdds() {
  const pre = [["−20%", A.spot * 0.8], ["−10%", A.spot * 0.9], ["+10%", A.spot * 1.1], ["+20%", A.spot * 1.2], ...(A.levels || []).map(l => [l.label.replace("your ", ""), l.v])];
  $("#lvlPre").innerHTML = pre.map(([l, v]) => `<button class="seg" data-v="${v}">${esc(l)}</button>`).join("");
  const inp = $("#lvl"), set = v => { ST.level = v > 0 ? v : null; if (ST.level) inp.value = String(Math.round(ST.level * 100) / 100); drawCone(); drawOdds(); };
  $$("#lvlPre .seg").forEach(b => b.onclick = () => set(+b.dataset.v));
  inp.addEventListener("input", () => { const v = parseFloat(inp.value.replace(",", ".")); ST.level = v > 0 ? v : null; drawCone(); drawOdds(); });
  set(((A.levels || []).find(l => l.k === "cost") || {}).v || A.spot * 0.9);
}

function drawOdds() {
  const rows = horizonRows(), L = ST.level;
  if (!L) { $("#oddsBox").innerHTML = `<div class="dim" style="font-size:.7rem">Type a price to see the odds of it.</div>`; return; }
  const down = L < A.spot, pc0 = v => v == null ? "–" : v < 0.005 ? "under 1%" : v > 0.995 ? "over 99%" : (v * 100).toFixed(0) + "%";
  const step = Math.max(1, Math.ceil(rows.length / 9));
  const pick = rows.filter((r, i) => i % step === 0 || i === rows.length - 1);
  $("#oddsBox").innerHTML = `<table class="tbl"><thead><tr><th>By</th><th>Days</th><th>Finishing below ${px(L)}</th><th>Finishing above</th>
    <th title="About twice the chance of finishing beyond the level">Touched on the way</th><th class="l">In words</th></tr></thead><tbody>
    ${pick.map(r => { const b = below(r, L), tail = b == null ? null : down ? b : 1 - b, touch = tail == null ? null : Math.min(1, 2 * tail);
      return `<tr><td>${dmy(r.exp)}</td><td class="dim">${Math.round(r.days)}</td><td>${down ? `<b>${pc0(b)}</b>` : pc0(b)}</td><td>${down ? pc0(b == null ? null : 1 - b) : `<b>${pc0(b == null ? null : 1 - b)}</b>`}</td>
        <td>${pc0(touch)}</td><td class="w">${tail == null ? "" : tail < 0.02 ? "priced as very unlikely" : tail < 0.16 ? `about one chance in ${Math.round(1 / tail)}` : tail < 0.4 ? "a real possibility" : tail < 0.6 ? "about even" : "more likely than not"}</td></tr>`; }).join("")}
    </tbody></table>`;
  wrapTables();
}

/* ---------- priced against delivered ---------------------------------------------------- */
function drawHist() {
  const R = A.realised || {}, Hh = A.history || [];
  lchart($("#histChart"), { label: "Implied and realised volatility", height: 240, endLabels: true,
    yfmt: v => v.toFixed(0) + "%", tfmt: v => v.toFixed(1) + "%", empty: R.error || "No price history",
    series: [{ name: "Delivered, 1 month", short: "did", color: COL.mid, w: 1.4, pts: (R.series || []).map(p => [p[0], p[1] * 100]) },
      { name: "Priced, 1 month", color: COL.ink, w: 2, dots: true, pts: Hh.filter(h => h.iv30).map(h => [h.d, h.iv30 * 100]) },
      { name: "Priced, 3 months", color: COL.s1, w: 1.4, dots: true, pts: Hh.filter(h => h.iv90).map(h => [h.d, h.iv90 * 100]) }] });
  $("#histBox").innerHTML = `<table class="tbl"><thead><tr><th class="l"></th><th>2 weeks</th><th>1 month</th><th>3 months</th><th>6 months</th><th>1 year</th></tr></thead><tbody>
    <tr><td>Delivered</td><td>${vol(R.rv10)}</td><td>${vol(R.rv21)}</td><td>${vol(R.rv63)}</td><td class="dim">–</td><td>${vol(R.rv252)}</td></tr>
    <tr><td>Priced</td><td class="dim">–</td><td><b>${vol(A.cm["30"])}</b></td><td>${vol(A.cm["90"])}</td><td>${vol(A.cm["180"])}</td><td>${vol(A.cm["365"])}</td></tr></tbody></table>`;
}

/* ---------- where the trading is ---------------------------------------------------------- */
function drawFlow() {
  const F = A.flow, name = a => `${sd(a.exp)} ${px(a.strike)} ${a.type === "C" ? "call" : "put"}`;
  $("#flowBox").innerHTML = `
    <div class="cards" style="grid-template-columns:repeat(2,1fr)">
      <div class="card"><div class="k">Traded</div><div class="v">${big(F.call_vol)} calls · ${big(F.put_vol)} puts</div><div class="t">${F.pc_vol == null ? "" : F.pc_vol.toFixed(2) + " puts per call"}</div></div>
      <div class="card"><div class="k">Open</div><div class="v">${big(F.call_oi)} calls · ${big(F.put_oi)} puts</div><div class="t">${F.pc_oi == null ? "" : F.pc_oi.toFixed(2) + " puts per call"}</div></div>
    </div>
    <div class="grp">Most traded contracts</div>
    <table class="tbl"><thead><tr><th>Contract</th><th>Last</th><th>Traded</th><th>Open</th><th title="Volume divided by open interest. Above 1, more changed hands than existed yesterday: new positions">Traded / open</th></tr></thead><tbody>
    ${(F.active || []).slice(0, 10).map(a => `<tr class="pk" data-exp="${a.exp}" data-k="${a.strike}" data-t="${a.type}" title="Work this contract out"><td>${esc(name(a))}</td><td>${px(a.last)}</td><td><b>${big(a.vol)}</b></td><td>${big(a.oi)}</td><td class="${a.ratio >= 1 ? "warnc" : ""}">${a.ratio == null ? "–" : a.ratio.toFixed(1) + "×"}</td></tr>`).join("") || `<tr><td colspan="5" class="dim">Nothing traded.</td></tr>`}
    </tbody></table>
    <div class="grp">By expiry</div>
    <table class="tbl"><thead><tr><th>Expiry</th><th>Calls traded</th><th>Puts traded</th><th>Calls open</th><th>Puts open</th><th title="The strike above the price with the most calls outstanding, and below it with the most puts">Heaviest strikes</th></tr></thead><tbody>
    ${F.expiries.slice(0, 9).map(e => `<tr><td>${sd(e.exp)}</td><td>${big(e.call_vol)}</td><td>${big(e.put_vol)}</td><td>${big(e.call_oi)}</td><td>${big(e.put_oi)}</td>
      <td>${e.put_wall ? px(e.put_wall) + " P" : "–"} · ${e.call_wall ? px(e.call_wall) + " C" : "–"}</td></tr>`).join("")}
    </tbody></table>
    <div class="src">Volume says how much traded, not who bought: a large call trade can be someone selling. Where traded exceeds open, the positions are new. The heaviest strikes are where the most contracts sit; near expiry a share often drifts toward them. Click a contract to work it out.</div>`;
  $$("#flowBox tr.pk").forEach(tr => tr.onclick = () => openContract(tr.dataset.exp, +tr.dataset.k, tr.dataset.t));
}

/* ---------- the chain --------------------------------------------------------------------- */
let CH = null;
async function loadChain() {
  $("#chainBox").innerHTML = `<div class="dim ld">Loading<span class="blink">_</span></div>`;
  try { const r = await fetch(`/api/pf/options/${encodeURIComponent(SYM)}/chain?exp=${ST.chainExp}`); CH = r.ok ? await r.json() : { rows: [], error: `HTTP ${r.status}` }; }
  catch (e) { CH = { rows: [], error: String(e.message || e) }; }
  renderChain();
}

function renderChain() {
  if (!CH) return;
  if (CH.error || !CH.rows.length) { $("#chainBox").innerHTML = `<div class="note"><b>No chain</b>${esc(CH.error || "nothing listed for this expiry")}</div>`; return; }
  const S = CH.spot, all = CH.rows, rows = ST.chainAll ? all : all.filter(r => Math.abs(r.k / S - 1) <= 0.3);
  const atm = rows.reduce((a, r) => Math.abs(r.k - S) < Math.abs(a.k - S) ? r : a, rows[0]).k;
  const mo = Math.max(1, ...rows.map(r => Math.max(r.c ? r.c[5] : 0, r.p ? r.p[5] : 0)));
  const sel = (k, t) => ST.pick && ST.pick.exp === CH.exp && ST.pick.k === k && ST.pick.t === t ? "sel" : "";
  const side = (x, cls, itm, k) => { if (!x) return `<td colspan="5" class="dim"></td>`;
    const t = cls === "c" ? "C" : "P", a = `data-k="${k}" data-t="${t}"`, c = `pk ${cls} ${itm} ${sel(k, t)}`;
    return cls === "c"
      ? `<td class="oi ${c}" ${a}><i style="width:${(x[5] / mo * 100).toFixed(0)}%"></i><span>${big(x[5])}</span></td><td class="${c}" ${a}>${big(x[4])}</td><td class="${c}" ${a}>${x[0] ? px(x[0]) : "–"}</td><td class="${c}" ${a}>${x[1] ? px(x[1]) : "–"}</td><td class="${c}" ${a}>${x[2] ? px(x[2]) : "–"}</td>`
      : `<td class="${c}" ${a}>${x[2] ? px(x[2]) : "–"}</td><td class="${c}" ${a}>${x[0] ? px(x[0]) : "–"}</td><td class="${c}" ${a}>${x[1] ? px(x[1]) : "–"}</td><td class="${c}" ${a}>${big(x[4])}</td><td class="oi ${c}" ${a}><i style="width:${(x[5] / mo * 100).toFixed(0)}%"></i><span>${big(x[5])}</span></td>`; };
  $("#chainBox").innerHTML = `<table class="tbl chain"><thead>
    <tr><th>Open</th><th>Traded</th><th>Bid</th><th>Ask</th><th>Last</th><th class="k">Strike</th><th>Volatility</th><th>Last</th><th>Bid</th><th>Ask</th><th>Traded</th><th>Open</th></tr></thead><tbody>
    ${rows.map(r => `<tr class="${r.k === atm ? "atm" : ""}">${side(r.c, "c", r.k < S ? "itm" : "", r.k)}<td class="k">${px(r.k)}</td><td>${r.iv == null ? `<span class="dim">–</span>` : vol(r.iv, 1)}</td>${side(r.p, "p", r.k > S ? "itm" : "", r.k)}</tr>`).join("")}
    </tbody></table>`;
  // one call or one put is five cells: light them together, open them together
  const box = $("#chainBox"), same = td => $$(`#chainBox td.pk[data-k="${td.dataset.k}"][data-t="${td.dataset.t}"]`);
  box.onmouseover = ev => { const td = ev.target.closest("td.pk"); $$("#chainBox td.hot").forEach(x => x.classList.remove("hot")); if (td) same(td).forEach(x => x.classList.add("hot")); };
  box.onmouseleave = () => $$("#chainBox td.hot").forEach(x => x.classList.remove("hot"));
  box.onclick = ev => { const td = ev.target.closest("td.pk"); if (td) openContract(CH.exp, +td.dataset.k, td.dataset.t); };
  const e = A.flow.expiries.find(x => x.exp === CH.exp);
  $("#chainTot").textContent = e ? `${big(e.call_vol)} calls and ${big(e.put_vol)} puts traded · ${rows.length} of ${all.length} strikes shown · price ${px(S)}` : "";
  const row = $("#chainBox tr.atm"); if (row) $("#chainBox").scrollTop = Math.max(0, row.offsetTop - 220);
}

/* ---------- reading guides ---------------------------------------------------------------- */
function howSurface() {
  const c30 = cmDay(), sk = A.skew30 || {}, c180 = A.cm["180"];
  const eg = c30 ? `${vol(c30)} means ${esc(SYM)}'s options are priced as if it moves about ${(c30 / Math.sqrt(252) * 100).toFixed(1)}% on an ordinary day (the yearly figure divided by sixteen)` : "40% means about 2.5% on an ordinary day (the yearly figure divided by sixteen)";
  const here = [];
  if (sk.gap != null) here.push(sk.gap >= 2 ? `the left side stands ${Math.round(sk.gap)} points above the right` : sk.gap <= -2 ? `the right side stands ${Math.round(-sk.gap)} points above the left, which is unusual` : "the two sides are about level");
  if (c30 && c180) here.push((c30 - c180) * 100 >= 3 ? `the front is ${Math.round((c30 - c180) * 100)} points above the back` : (c180 - c30) * 100 >= 3 ? `the back is ${Math.round((c180 - c30) * 100)} points above the front` : "front and back are about level");
  return `<dl class="how">
    <dt>What it is</dt><dd>Every option has a price, and each price can be turned into one number: the yearly swing in the share that would make that price fair. That is implied volatility. ${eg}. The sheet is that number for every strike and every expiry at once, so it shows where options are dear and where they are cheap.</dd>
    <dt>Height and colour</dt><dd>Both say the same thing. Higher and warmer means more movement priced, so a dearer option.</dd>
    <dt>Left to right: the skew</dt><dd>From puts far below the price, through at the money in the middle, to calls far above it. Higher on the left than on the right is the usual shape for a share: falls are fast, so insurance against one costs more than a bet on a rise. Both ends turned up, a smile, means a large move either way is being paid for. Higher on the right is rare and means buyers are chasing a rise.</dd>
    <dt>Front to back: the term structure</dt><dd>From the nearest expiry to the furthest. Usually it lies flat or rises gently. A front edge standing above the back means something with a date is priced: results, a ruling, a vote. A ridge across one expiry is that event: every expiry after it carries the move, none before it does.</dd>
    ${here.length ? `<dt>On this sheet</dt><dd>For ${esc(SYM)} now, ${here.join(", and ")}. "What it says" above puts numbers on both.</dd>` : ""}
    <dt>What moves the whole sheet</dt><dd>It rises when the share falls hard or the market turns nervous, and sinks in quiet weeks and on the morning after results. Whoever buys an option needs the share to move more than the sheet says; whoever sells one needs it to move less.</dd>
    <dt>What not to read</dt><dd>One spike or dent on its own is a thin quote, not information. Where no strike priced there is no sheet, rather than a guess.</dd>
    <dt>The two views</dt><dd><b>By delta</b> puts every expiry on the same footing: a 25Δ put is the strike with about one chance in four of paying off, wherever that strike is, so the sheet has no ragged edge. <b>By strike</b> shows the same thing against the price itself. Drag to turn it; click an expiry to follow it in Skew below.</dd>
  </dl>`;
}

function howChain() {
  return `<dl class="how cols">
    <div><dt>One row, one strike</dt><dd>The strike is the price at which the option lets its holder deal: a <b>call</b> is the right to buy the share there, a <b>put</b> the right to sell it there. Calls are on the left and puts on the right, for the same strike and the expiry chosen above.</dd></div>
    <div><dt>Bid, ask, last</dt><dd>Prices per share, in ${esc(A.currency)}. A contract is 100 shares, so 9.95 costs 995. Bid is what a buyer will pay now, ask what a seller wants, last the most recent trade, which can be hours old. The gap between bid and ask is what dealing costs.</dd></div>
    <div><dt>Shaded cells</dt><dd>In the money: calls with a strike below the share price, puts with one above it. Part of their price is value they already have. Unshaded options are worth nothing if exercised today; all of their price is paid for time.</dd></div>
    <div><dt>The line across</dt><dd>The strike nearest the share price, at the money. Options there trade the most and lose the most to each passing day.</dd></div>
    <div><dt>Traded and open</dt><dd><b>Traded</b> is contracts that changed hands this session. <b>Open</b> is contracts in existence, counted once a day. The bar behind Open compares strikes within this expiry: the long bars are where the positions sit.</dd></div>
    <div><dt>Volatility</dt><dd>The implied volatility at that strike, read from the out-of-the-money option: the put below the price, the call above it. It is the row's price expressed as a yearly swing, which makes strikes comparable.</dd></div>
    <div><dt>Click a call or a put</dt><dd>To see that one contract worked out: what it costs, what it pays at expiry, where it breaks even, the odds the chain puts on that, and how its price moves with the share, with time and with volatility.</dd></div>
  </dl>`;
}

/* ---------- one contract ------------------------------------------------------------------ */
let CT = null;
async function openContract(exp, k, t) {
  ST.pick = { exp, k, t };
  const host = $("#s-contract"); if (!host) return;
  host.style.display = "";
  host.innerHTML = `<div class="body"><div class="dim ld">Working it out<span class="blink">_</span></div></div>`;
  if (ST.chainExp !== exp) { ST.chainExp = exp; $("#chainExp").value = exp; loadChain(); } else renderChain();
  try { const r = await fetch(`/api/pf/options/${encodeURIComponent(SYM)}/contract?exp=${exp}&k=${k}&type=${t}`);
    CT = r.ok ? await r.json() : { error: (await r.json().catch(() => ({}))).detail || `HTTP ${r.status}` }; }
  catch (e) { CT = { error: String(e.message || e) }; }
  renderContract();
  host.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderContract() {
  const host = $("#s-contract"), C = CT; if (!host || !C) return;
  const shut = `<button class="seg" id="cClose">Close</button>`;
  if (C.error) { host.innerHTML = `<header><h2>One contract</h2><span class="sp"></span>${shut}</header><div class="body"><div class="note"><b>Could not work it out</b>${esc(C.error)}</div></div>`;
    $("#cClose").onclick = closeContract; return; }
  const sold = ST.side === "sold", sg = sold ? -1 : 1, n = C.shares, W = C.words[ST.side], G = C.greeks;
  const cur = C.currency === "USD" ? "$" : C.currency + " ";
  const m = v => v == null ? "–" : cur + (Math.abs(v) >= 1000 ? nf(v, 0) : nf(v, 2));          // money, unsigned
  const pl = v => v == null ? "–" : `<span class="${sgn(v)}">${v < 0 ? "−" : v > 0 ? "+" : ""}${cur}${Math.abs(v) >= 1000 ? nf(Math.abs(v), 0) : nf(Math.abs(v), 2)}</span>`;
  const pc0 = v => v == null ? "–" : v < 0.005 ? "under 1%" : v > 0.995 ? "over 99%" : Math.round(v * 100) + "%";
  const title = `${esc(C.sym)} ${sd(C.exp)} ${px(C.strike)} ${C.kind}`;
  const from = C.price_from === "mid" ? `middle of ${px(C.bid)} bid and ${px(C.ask)} ask` : `last trade${C.last_ts ? ", " + ago(C.last_ts) : ""}`;
  const noLimit = `<span style="font-size:.9rem">no limit</span>`;
  const t = [];
  t.push(tile(sold ? "Premium received" : "Premium", m(C.premium), null, `${px(C.price)} a share × ${n}${C.premium_eur ? ` · about ${eur(C.premium_eur)}` : ""} · ${from}`, true,
    "What one contract costs to buy, or pays to sell: the price per share times a hundred shares"));
  t.push(tile("Break-even at expiry", px(C.breakeven), null, `${pc(C.breakeven_pct)} from ${px(C.spot)}`, false,
    C.type === "C" ? "Strike plus premium: above this the buyer is in profit at expiry" : "Strike minus premium: below this the buyer is in profit at expiry"));
  t.push(tile(sold ? "Most you can make" : "Most you can lose", m(C.max_loss), null, sold ? "the premium, if it expires worthless" : "the premium, and never more", false, ""));
  t.push(tile(sold ? "Most you can lose" : "Most you can make", C.max_gain == null ? noLimit : m(C.max_gain), null,
    C.max_gain == null ? (sold ? "a share can rise without limit" : "it rises with the share") : "with the share at zero", false, ""));
  if (C.p_itm != null) {
    t.push(tile(sold ? "Expires worthless" : "Finishes in the money", pc0(sold ? 1 - C.p_itm : C.p_itm), null, C.odds_from === "chain" ? "the odds in the chain's own prices" : "at this volatility", false,
      "The chance, as priced, that the share ends beyond the strike. Read off every option of this expiry together, so it carries the skew"));
    t.push(tile(sold ? "You keep a profit" : "Makes money", pc0(sold ? 1 - C.p_profit : C.p_profit), null, "at expiry, as priced", false,
      "The chance, as priced, that the share ends beyond break-even"));
  }
  t.push(tile("Price is made of", C.stale ? "–" : `${px(C.intrinsic)} + ${px(C.time_value)}`, null, C.stale ? "the price is older than the share's move" : "value it has now + paid for time, a share", false,
    "Intrinsic value is what it would be worth exercised today. Time value is the rest, and is gone by expiry"));
  if (G) t.push(tile("Moves with the share", (G.delta * sg > 0 ? "+" : "−") + Math.abs(G.delta).toFixed(2), null, `like ${G.delta * sg > 0 ? "owning" : "being short"} ${Math.round(Math.abs(G.delta) * n)} shares${G.omega ? ` · ${Math.abs(G.omega).toFixed(1)}% per 1%` : ""}`, false, "Delta"));
  if (G) t.push(tile(sold ? "A day earns" : "A day costs", pl(G.theta * n * sg), null, sold ? "earned each day nothing happens" : "lost each day nothing happens", false, "Theta: the change over one calendar day with the share and volatility unchanged"));
  if (C.iv_used) t.push(tile("Implied volatility", vol(C.iv_used, 1), null, `${C.iv_atm ? `at the money ${vol(C.iv_atm, 1)}` : ""}${(A.realised || {}).rv21 ? ` · delivered ${vol(A.realised.rv21)}` : ""}`, false,
    "The yearly swing this option's own price implies"));
  t.push(tile("Time left", `${Math.round(C.days)} days`, null, `expires ${dmy(C.exp)}`, false, ""));

  const S = C.scenarios || [];
  const table = S.length ? `<table class="tbl"><thead><tr><th>${esc(C.sym)} at</th><th>Move</th><th title="Per share, on the last day">Worth at expiry</th>
      <th>Profit at expiry</th><th title="${sold ? "As a share of the premium received" : "As a share of the premium paid"}">On the premium</th>
      <th title="The same share price reached today, with implied volatility unchanged">If it were there today</th>
      <th title="The same share price reached halfway to expiry, with implied volatility unchanged">Halfway${C.half_date ? ", " + sd(C.half_date) : ""}</th></tr></thead><tbody>
    ${S.map(r => { const e = (r.end - C.price) * n * sg, now = r.now == null ? null : (r.now - C.price) * n * sg, half = r.half == null ? null : (r.half - C.price) * n * sg;
      return `<tr class="${r.mark ? "mk" : ""}"><td>${px(r.s)}${r.mark ? ` <span class="bdg info">${r.mark}</span>` : Math.abs(r.move_pct) < 0.05 ? ` <span class="bdg info">now</span>` : ""}</td>
        <td class="dim">${pc(r.move_pct, 0)}</td><td>${px(r.end)}</td><td>${pl(e)}</td><td class="${sgn(e)}">${pc(e / C.premium * 100, 0)}</td><td>${pl(now)}</td><td>${pl(half)}</td></tr>`; }).join("")}
    </tbody></table>` : `<div class="dim" style="font-size:.7rem">No price to work a payoff from.</div>`;

  host.innerHTML = `<header><h2>${title}</h2><span class="hint">one contract is ${n} shares · read ${ago(C.asof)}</span></header>
    <div class="body">
      <div class="fbar"><span class="grp" id="cSide"><button class="seg ${sold ? "" : "on"}" data-v="bought">If you buy it</button><button class="seg ${sold ? "on" : ""}" data-v="sold">If you sell it</button></span>
        <span class="sp"></span>${shut}</div>
      <div class="prose">${esc(W.what)}</div>
      ${C.stale ? `<div class="note"><b>This price is stale</b>${esc((W.lines.find(l => l[0] === "A stale price") || [])[1] || "")}</div>` : ""}
      <div class="tiles">${t.join("")}</div>
      <div class="two">
        <div><div class="grp">Profit and loss, per contract</div><div id="payoff"></div>
          <div class="src">The solid line is the day it expires: that is all the option is then. The dashed lines are the same share prices reached sooner, when the option still has time left in it${sold ? " (and so costs more to buy back)" : ""}. The shaded band is where the chain's prices put two outcomes in three for ${esc(C.sym)} on that day.</div></div>
        <div><div class="grp">In words</div><div class="clines">${W.lines.map(([k, v]) => `<div class="cl"><b>${esc(k)}</b><span>${esc(v)}</span></div>`).join("")}</div></div>
      </div>
      <div class="grp" style="margin-top:.7rem">If ${esc(C.sym)} is at …</div>
      ${table}
      <div class="src">Worked out with Black's formula from this option's own price, against the forward the chain implies for ${dmy(C.exp)} (${px(C.forward)}) and a ${(C.rate * 100).toFixed(1)}% interest rate. It treats the option as exercisable only at expiry; a US share option can be exercised any day, which matters little except deep in the money or just before a dividend. "If it were there today" and "halfway" hold implied volatility where it is, which the market does not: it usually falls after results and rises in a sell-off. Commission and tax are left out. The odds are the ones built into prices, not a forecast. ${MODE[C.mode] ? esc(MODE[C.mode][1]) : ""}</div>
    </div>`;
  $("#cClose").onclick = closeContract;
  $$("#cSide .seg").forEach(b => b.onclick = () => { ST.side = b.dataset.v; renderContract(); });
  drawPayoff();
  wrapTables();
}

function closeContract() {
  ST.pick = null; CT = null;
  const host = $("#s-contract"); if (host) { host.style.display = "none"; host.innerHTML = ""; }
  renderChain();
}

function drawPayoff() {
  const host = $("#payoff"), C = CT; if (!host || !C || !C.curve) return;
  const sg = ST.side === "sold" ? -1 : 1, n = C.shares, X0 = C.curve.x, cur = C.currency === "USD" ? "$" : "";
  const ser = k => C.curve[k] ? C.curve[k].map(v => v * n * sg) : null;
  const end = ser("end"), now = ser("now"), half = ser("half");
  const W = Math.max(300, Math.round(host.clientWidth)), H = W < 520 ? 250 : 310, M = { l: 54, r: 14, t: 16, b: 34 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b, x0 = X0[0], x1 = X0[X0.length - 1];
  const all = [...end, ...(now || []), ...(half || [])], yt = nice(Math.min(...all), Math.max(...all), 6);
  const X = v => M.l + (v - x0) / (x1 - x0) * pw, Y = v => M.t + (yt.hi - v) / (yt.hi - yt.lo) * ph;
  const money = v => (v < 0 ? "−" : "") + cur + nf(Math.abs(v), Math.abs(v) >= 100 ? 0 : 2);
  let g = "";
  for (const t of yt.ticks) g += `<line class="gl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(t).toFixed(1)}" y2="${Y(t).toFixed(1)}"/><text class="ax" x="${M.l - 6}" y="${(Y(t) + 3).toFixed(1)}" text-anchor="end">${money(t)}</text>`;
  const xt = nice(x0, x1, Math.max(4, Math.floor(pw / 90)));
  for (const t of xt.ticks) if (t >= x0 && t <= x1) g += `<text class="ax" x="${X(t).toFixed(1)}" y="${M.t + ph + 13}" text-anchor="middle">${px(t)}</text>`;
  g += `<text class="ax" x="${M.l + pw}" y="${H - 4}" text-anchor="end">${esc(C.sym)} share price on ${dmy(C.exp)}</text>`;
  if (C.range) { const a = Math.max(x0, C.range[1]), b = Math.min(x1, C.range[3]);
    if (b > a) g += `<rect x="${X(a).toFixed(1)}" y="${M.t}" width="${(X(b) - X(a)).toFixed(1)}" height="${ph}" fill="var(--accent)" opacity=".07"/>
      <text class="lv" x="${((X(a) + X(b)) / 2).toFixed(1)}" y="${M.t + ph - 5}" text-anchor="middle" fill="var(--ink-soft)">two in three</text>`; }
  // profit above the zero line in green, loss below it in red, under the expiry line
  const zero = Y(0), path = ys => X0.map((x, i) => (i ? "L" : "M") + X(x).toFixed(1) + " " + Y(ys[i]).toFixed(1)).join("");
  const area = `${path(end)}L${X(x1).toFixed(1)} ${zero.toFixed(1)}L${X(x0).toFixed(1)} ${zero.toFixed(1)}Z`;
  g += `<clipPath id="pfUp"><rect x="${M.l}" y="${M.t}" width="${pw}" height="${Math.max(0, zero - M.t).toFixed(1)}"/></clipPath>
    <clipPath id="pfDn"><rect x="${M.l}" y="${zero.toFixed(1)}" width="${pw}" height="${Math.max(0, M.t + ph - zero).toFixed(1)}"/></clipPath>
    <path d="${area}" fill="var(--gain)" opacity=".13" clip-path="url(#pfUp)"/><path d="${area}" fill="var(--loss)" opacity=".13" clip-path="url(#pfDn)"/>
    <line x1="${M.l}" x2="${M.l + pw}" y1="${zero.toFixed(1)}" y2="${zero.toFixed(1)}" stroke="var(--rule-strong)" stroke-width="1"/>`;
  // three upright lines, labelled on three different heights so close ones do not write over each other
  [[C.spot, "now", "var(--ink-mid)", "2 3"], [C.strike, "strike", "var(--ink-soft)", "5 4"], [C.breakeven, "break-even", "var(--warn)", ""]]
    .filter(k => k[0] >= x0 && k[0] <= x1).sort((a, b) => a[0] - b[0]).forEach(([v, label, col, dash], i) => {
      const right = X(v) > M.l + pw - 130;
      g += `<line x1="${X(v).toFixed(1)}" x2="${X(v).toFixed(1)}" y1="${M.t}" y2="${M.t + ph}" stroke="${col}" stroke-width="1" ${dash ? `stroke-dasharray="${dash}"` : ""}/>
        <text class="lv" x="${(X(v) + (right ? -4 : 4)).toFixed(1)}" y="${M.t + 9 + 12 * i}" text-anchor="${right ? "end" : "start"}" fill="${col}">${label} ${px(v)}</text>`; });
  g += `${half ? `<path d="${path(half)}" fill="none" stroke="var(--s2)" stroke-width="1.2" stroke-dasharray="2 3"/>` : ""}
    ${now ? `<path d="${path(now)}" fill="none" stroke="var(--s1)" stroke-width="1.5" stroke-dasharray="6 4"/>` : ""}
    <path d="${path(end)}" fill="none" stroke="var(--ink)" stroke-width="2.1" stroke-linejoin="round"/>
    <line class="xh" x1="0" x2="0" y1="${M.t}" y2="${M.t + ph}" stroke="var(--ink-soft)" stroke-width="1" stroke-dasharray="2 3" style="display:none"/>
    <rect class="hov" x="${M.l}" y="${M.t}" width="${pw}" height="${ph}" fill="transparent" style="cursor:crosshair"/>`;
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Profit and loss of the ${esc(C.sym)} ${px(C.strike)} ${C.kind} against the share price">${g}</svg>
    <div class="legend"><span class="chip on" style="cursor:default"><i class="sw" style="background:var(--ink)"></i>at expiry</span>
      ${half ? `<span class="chip on" style="cursor:default"><i class="sw" style="background:var(--s2)"></i>halfway, ${sd(C.half_date)}</span>` : ""}
      ${now ? `<span class="chip on" style="cursor:default"><i class="sw" style="background:var(--s1)"></i>today</span>` : ""}</div>
    <div class="lc-tip" style="display:none"></div>`;
  const svg = host.querySelector("svg"), tip = host.querySelector(".lc-tip"), xh = svg.querySelector(".xh"), hov = svg.querySelector(".hov");
  hov.addEventListener("mousemove", ev => {
    const b = svg.getBoundingClientRect(), v = x0 + ((ev.clientX - b.left) * (W / b.width) - M.l) / pw * (x1 - x0);
    const i = Math.max(0, Math.min(X0.length - 1, Math.round((v - x0) / (x1 - x0) * (X0.length - 1))));
    xh.setAttribute("x1", X(X0[i]).toFixed(1)); xh.setAttribute("x2", X(X0[i]).toFixed(1)); xh.style.display = "";
    const row = (l, y, c) => `<div class="r"><span><i class="sw" style="background:${c}"></i>${l}</span><b class="${sgn(y)}">${money(y)}</b></div>`;
    tip.innerHTML = `<div class="h">${esc(C.sym)} at ${px(X0[i])} <span class="dim">(${pc((X0[i] / C.spot - 1) * 100, 0)})</span></div>
      ${row("at expiry", end[i], "var(--ink)")}${half ? row("halfway", half[i], "var(--s2)") : ""}${now ? row("today", now[i], "var(--s1)") : ""}`;
    tip.style.display = "";
    const sx = X(X0[i]) / W * b.width, tw = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(host.clientWidth - tw, sx + (sx > host.clientWidth / 2 ? -tw - 12 : 12))) + "px"; tip.style.top = "6px";
  });
  hov.addEventListener("mouseleave", () => { tip.style.display = "none"; xh.style.display = "none"; });
}

let _oz = null;
window.addEventListener("resize", () => { clearTimeout(_oz); _oz = setTimeout(() => { if (A && A.available && $("#cone")) drawCone(); if ($("#payoff") && CT && !CT.error) drawPayoff(); }, 160); });

SYM ? loadSym(false) : loadOverview();
