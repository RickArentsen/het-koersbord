/* ==========================================================================
   POL — politicians' trades: what members of Congress buy and sell, from
   their own periodic transaction reports. The data is the user's tracker
   (backend/ptt, pf/pol.py): /api/pf/pol is the page, /pol/member/{id},
   /pol/committee/{id} and /pol/ticker/{sym} the views behind its links.
   Every alpha is from the first open after the trade was FILED, against SPY.
   ========================================================================== */

let O = null, TAB = "latest", POLL = null;
// the filings tab: one calendar year at a time, this year's with the page and any other fetched when picked
const FY = {}, PAGE = 500;
let YEAR = null, SHOW = PAGE;
const F = { chamber: "all", side: "all", remit: false, min: 0, q: "" };
const MINS = [[0, "any size"], [15001, "$15k+"], [50001, "$50k+"], [250001, "$250k+"], [1000001, "$1M+"]];

$("#shell").innerHTML = termBar({ here: "POL · POLITICIANS' TRADES", right:
  `<button class="tbtn" id="runBtn" title="Fetch new filings and rescore now">Refresh now</button>` });
wireShell();

async function api(path, opt) {
  const r = await fetch("/api/pf" + path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}
const post = path => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
const usd = v => v == null ? "–" : v >= 1e9 ? `$${nf(v / 1e9, 1)}bn` : v >= 1e6 ? `$${nf(v / 1e6, 1)}M` : v >= 1e3 ? `$${nf(v / 1e3, 0)}k` : `$${nf(v, 0)}`;
const amt = s => esc(String(s || "–").replace(/\.00\b/g, "").replace(/\s*-\s*/, "–"));
const al = v => v == null ? `<span class="dim">–</span>` : pcc(v * 100, 1);
const side = t => t === "purchase" ? `<span class="side buy">buy</span>` : (t || "").startsWith("sale")
  ? `<span class="side sell">${t === "sale_partial" ? "sell part" : "sell"}</span>` : esc(t || "–");
const who = t => `<span class="party ${esc(t.party)}">${esc(t.party || "?")}</span><a href="#m=${esc(t.bg || "")}">${esc(t.member || "?")}</a>${t.chamber === "senate" ? `<span class="ch">sen</span>` : ""}`;
const remit = r => (r || []).length ? `<span class="remit" title="inside their committees' remit when traded">${esc(r.join(", "))}</span>` : "";
const grade = g => g ? `<span class="grade ${esc(g)}" title="how deliberate the trade looks (A-E): a description, not a forecast">${esc(g)}</span>` : "";

/* ---------- loading and routing ---------- */
async function load() {
  try { O = await api("/pol"); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load POL</b>${esc(e.message)}</div>`; return; }
  for (const k of Object.keys(FY)) delete FY[k];          // a run may have added filings: every year is read again
  if (O.filings) { FY[O.filings.year] = O.filings.trades || []; YEAR = YEAR || O.filings.year; }
  route();
  watchRun();
}
window.addEventListener("hashchange", route);

function route() {
  const h = location.hash.slice(1), [k, v] = [h.split("=")[0], decodeURIComponent(h.split("=").slice(1).join("="))];
  if (!O) return;
  if (!O.built) return renderBuild();
  if (k === "m" && v) return renderMember(v);
  if (k === "c" && v) return renderCommittee(v);
  if (k === "t" && v) return renderTicker(v);
  if (k === "tab" && v) TAB = v;
  render();
}

/* ---------- the run ---------- */
function runBox(r) {
  if (!r) return "";
  if (r.state === "running" || r.lock) {
    const step = r.step ? `<b>${esc(r.step)}</b> (step ${r.i} of ${r.n})${r.detail ? ` · ${esc(r.detail)}` : ""}` : "<b>starting</b>";
    return `<div class="job">The tracker is running (${esc((r.lock && r.lock.kind) || r.kind || "")}): ${step}.</div>`;
  }
  if (r.last && r.last.ok === false) return `<div class="job err">The last run stopped: ${esc(r.last.error || "")}</div>`;
  return "";
}
function watchRun() {
  clearTimeout(POLL);
  const r = O && O.run;
  if (!r || !(r.state === "running" || r.lock)) return;
  POLL = setTimeout(async () => {
    let j;
    try { j = await api("/pol/job"); } catch { j = null; }
    if (!j) return watchRun();
    O.run = { ...O.run, ...j };
    const box = $("#runBox");
    if (box) box.innerHTML = runBox(O.run);
    if (!(j.state === "running" || j.lock)) return load();
    watchRun();
  }, 10000);
}
$("#runBtn").onclick = async () => {
  try { O.run = { ...O.run, ...(await post("/pol/build")) }; route(); watchRun(); } catch (e) { alert(e.message); }
};

/* ---------- before the first build ---------- */
function renderBuild() {
  $("#app").innerHTML = `<div class="panel"><div class="body">
    <div class="hdr"><h1>POLITICIANS' TRADES</h1><span class="sub">what members of Congress buy and sell, from their own filings</span></div>
    <p class="intro">The data is your own tracker, run inside the terminal: every House <b>periodic transaction report</b> since 2020 from the
      Clerk, parsed from its PDF; every Senate report from the EFD portal; scanned paper filings read by Windows OCR and kept apart, unreviewed;
      committee seats point in time; prices from Yahoo. Every purchase is scored from <b>the first open after it was filed</b> against the
      S&P 500, because that is the earliest anyone outside the member's household could act on it.</p>
    <p class="intro">The first build reads everything since 2020: about an hour of filings, then the scanned ones, then the prices, a few
      hours in all. After that it runs every night at 05:30.</p>
    <div id="runBox">${runBox(O.run)}</div>
    ${O.run && (O.run.state === "running" || O.run.lock) ? "" : `<p><button class="tbtn" id="buildBtn">Build the data</button></p>`}
  </div></div>`;
  const b = $("#buildBtn");
  if (b) b.onclick = async () => { try { O.run = { ...O.run, ...(await post("/pol/build")) }; renderBuild(); watchRun(); } catch (e) { alert(e.message); } };
}

/* ---------- the overview ---------- */
function render() {
  const fr = O.freshness || {}, c = O.counts || {}, M = O.mine || {};
  const tabs = [["latest", "Filings", (O.years || {})[String(YEAR)] ?? (FY[YEAR] || []).length], ["mine", "Your holdings", (M.by || []).length],
    ["leaders", "Leaderboard", (O.leaders || []).length], ["strategy", "Strategy", null],
    ["committees", "Committees", (O.committees || []).length], ["scans", "Scanned filings", (O.ocr || []).length]];
  $("#app").innerHTML = `
    <div class="panel"><div class="body">
      <div class="status">
        <span><b>${nf(c.trades, 0)}</b> disclosed trades · <b>${nf(c.members, 0)}</b> members · newest filing
          <b>${fr.newest_filed ? dmy(fr.newest_filed) : "–"}</b>${fr.days_since != null ? ` (${fr.days_since} d ago)` : ""}</span>
        <span class="sp"></span>
        <span>checked ${fr.last_checked ? esc(fr.last_checked.slice(0, 16).replace("T", " ")) : "–"} · ${esc(O.next)}</span>
      </div>
      <div id="runBox">${runBox(O.run)}</div>
      ${c.senate === 0 ? `<div class="note2"><b>No Senate filings.</b> efdsearch.senate.gov refuses this network (HTTP 403 from its edge;
        a browser here is refused too), so the Senate cannot be read from this laptop. The House is complete on its own, and every
        night's run tries the Senate again.</div>` : ""}
      <div class="tabs" id="tabs">${tabs.map(([k, t, n]) => `<button data-k="${k}" class="${TAB === k ? "on" : ""}">${t}${n != null ? `<span class="n">${nf(n, 0)}</span>` : ""}</button>`).join("")}</div>
      <div id="tab"></div>
    </div></div>
    <p class="foot">From the members' own periodic transaction reports (STOCK Act): the House's from the Clerk, the Senate's from the EFD
      portal. Amounts are the ranges filed; sizes use their midpoint. A member has up to 45 days to file, so every alpha here runs from the
      <b>first open after the filing</b> against SPY, the earliest anyone outside the household could act. "In remit" means the company's
      industry falls under a committee the member sat on when they traded: context, not an accusation. Not advice.</p>`;
  $$("#tabs button").forEach(b => b.onclick = () => { TAB = b.dataset.k; history.replaceState(null, "", `#tab=${TAB}`); render(); });
  ({ latest: tabLatest, mine: tabMine, leaders: tabLeaders, strategy: tabStrategy, committees: tabCommittees, scans: tabScans })[TAB]();
  wrapTables();
}

function tradesTable(rows, opts = {}) {
  if (!rows.length) return `<p class="intro">${opts.empty || "Nothing here."}</p>`;
  const held = new Set(Object.keys((O.mine || {}).tickers || {}));
  return `<table class="tbl"><thead><tr><th>Filed</th>${opts.noWho ? "" : `<th class="l">Member</th>`}<th class="l">Trade</th>
    <th>Amount</th><th>Traded</th><th title="days from the trade to the filing">Lag</th><th class="l">Remit</th><th>Grade</th>
    <th title="from the first open after the filing, against SPY">12m alpha</th><th></th></tr></thead><tbody>
    ${rows.map(t => `<tr><td>${dmy(t.filed)}</td>${opts.noWho ? "" : `<td class="l">${who(t)}</td>`}
      <td class="l">${side(t.type)} ${t.ticker ? `<a href="#t=${esc(t.ticker)}">${esc(t.ticker)}</a>` : ""}${t.option ? ` <span class="dim">option</span>` : ""}
        ${held.has(t.ticker) ? `<span class="yours">you hold</span>` : ""} <span class="dim" title="${esc(t.asset)}">${esc((t.asset || "").slice(0, 38))}</span>
        ${t.owner && t.owner !== "self" ? `<span class="dim">· ${esc(t.owner)}</span>` : ""}</td>
      <td>${amt(t.amount)}</td><td>${dmy(t.traded)}</td><td>${t.lag != null ? t.lag + " d" : "–"}</td><td class="l">${remit(t.remit)}</td>
      <td>${grade(t.grade)}</td><td>${t.type === "purchase" ? al(t.alpha["12m"]) : ""}</td>
      <td>${t.pdf ? `<a href="${esc(t.pdf)}" target="_blank" rel="noopener" title="the filing itself">PDF</a>` : ""}</td></tr>`).join("")}
  </tbody></table>`;
}

async function pickYear(y) {
  YEAR = +y;
  SHOW = PAGE;
  if (!FY[YEAR]) {
    $("#tab").innerHTML = `<p class="intro">Reading the filings of ${YEAR}…</p>`;
    try { FY[YEAR] = (await api(`/pol/filings?year=${YEAR}`)).trades || []; }
    catch (e) { $("#tab").innerHTML = `<div class="note"><b>Could not load ${YEAR}</b>${esc(e.message)}</div>`; return; }
  }
  render();
}

function tabLatest() {
  if (!FY[YEAR]) return pickYear(YEAR);
  const all = FY[YEAR], now = O.filings ? O.filings.year : new Date().getFullYear();
  const q = F.q.trim().toUpperCase();
  const rows = all.filter(t => (F.chamber === "all" || t.chamber === F.chamber)
    && (F.side === "all" || (F.side === "buy" ? t.type === "purchase" : (t.type || "").startsWith("sale")))
    && (!F.remit || t.remit.length) && (t.mid || 0) >= F.min
    && (!q || (t.ticker || "").toUpperCase().includes(q) || (t.member || "").toUpperCase().includes(q)));
  const seg = (k, v, t) => `<button class="seg ${F[k] === v ? "on" : ""}" data-f="${k}" data-v="${v}">${t}</button>`;
  const years = Object.keys(O.years || {}).sort().reverse();
  const more = rows.length - SHOW;
  $("#tab").innerHTML = `<div class="filters">
      <span class="lbl">Year</span>${years.map(y => `<button class="seg yr ${+y === YEAR ? "on" : ""}" data-y="${y}"
        title="${nf(O.years[y], 0)} trades filed in ${y}">${y}</button>`).join("")}
    </div><div class="filters">
      <span class="lbl">Chamber</span>${seg("chamber", "all", "Both")}${seg("chamber", "house", "House")}${seg("chamber", "senate", "Senate")}
      <span class="lbl">Side</span>${seg("side", "all", "All")}${seg("side", "buy", "Buys")}${seg("side", "sell", "Sells")}
      <span class="lbl">Size</span><select id="fMin">${MINS.map(([v, t]) => `<option value="${v}" ${F.min === v ? "selected" : ""}>${t}</option>`).join("")}</select>
      <label><input type="checkbox" id="fRemit" ${F.remit ? "checked" : ""}> in remit only</label>
      <input type="search" id="fQ" placeholder="member or ticker" value="${esc(F.q)}">
      <span class="dim">${nf(rows.length, 0)} of ${nf(all.length, 0)} trades filed in ${YEAR}${YEAR === now ? " so far" : ""}</span></div>
    ${unreadNote()}
    ${tradesTable(rows.slice(0, SHOW), { empty: `No filing in ${YEAR} matches.` })}
    ${more > 0 ? `<p class="moreRow"><button class="tbtn" id="moreBtn">Show ${nf(Math.min(PAGE, more), 0)} more</button>
      <span class="dim">${nf(Math.min(SHOW, rows.length), 0)} shown, ${nf(more, 0)} to go</span></p>` : ""}`;
  const again = () => { SHOW = PAGE; tabLatest(); wrapTables(); };
  $$("#tab .seg[data-f]").forEach(b => b.onclick = () => { F[b.dataset.f] = b.dataset.v; again(); });
  $$("#tab .seg[data-y]").forEach(b => b.onclick = () => pickYear(b.dataset.y));
  $("#fMin").onchange = e => { F.min = +e.target.value; again(); };
  $("#fRemit").onchange = e => { F.remit = e.target.checked; again(); };
  $("#fQ").oninput = e => { F.q = e.target.value; clearTimeout(F.t); F.t = setTimeout(() => { again(); const i = $("#fQ"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }, 250); };
  const mb = $("#moreBtn");
  if (mb) mb.onclick = () => { SHOW += PAGE; tabLatest(); wrapTables(); };
}

/* Scanned filings neither the text reader nor the OCR could read: their trades are not in the list above. */
function unreadNote() {
  const U = O.unread || {}, f = U.filings || [];
  if (!f.length || U.year !== YEAR) return "";
  return `<details class="note2"><summary><b>${f.length} scanned filing${f.length === 1 ? "" : "s"} from ${U.year} could not be read</b>
      (paper forms the OCR could not make out): their trades are not in this list. Open them to read by hand.</summary>
    <div style="columns:2 22rem;margin-top:.4rem">${f.map(x => `<div><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.member || "?")}</a>
      <span class="dim">filed ${dmy(x.filed)}</span></div>`).join("")}</div></details>`;
}

function tabMine() {
  const M = O.mine || {};
  if (!Object.keys(M.tickers || {}).length) { $("#tab").innerHTML = `<p class="intro">No holdings to compare yet.</p>`; return; }
  $("#tab").innerHTML = `<p class="note2">Every trade Congress disclosed in the last year in a company you hold: your shares, their US listings
      (ASML for ASML.AS), and your turbos' underlyings.</p>
    ${(M.by || []).length ? `<table class="tbl"><thead><tr><th class="l">Company</th><th>Buys</th><th>Bought</th><th>Sells</th><th>Sold</th>
      <th class="l">Who</th><th>Last filed</th></tr></thead><tbody>
      ${M.by.map(b => `<tr><td class="l"><a href="#t=${esc(b.ticker)}">${esc(b.ticker)}</a> <span class="dim">you hold ${esc(b.held)}${b.how === "turbo" ? " (a turbo)" : ""}</span></td>
        <td class="pos">${b.buys || "–"}</td><td>${b.buys ? usd(b.buy_usd) : "–"}</td><td class="neg">${b.sells || "–"}</td><td>${b.sells ? usd(b.sell_usd) : "–"}</td>
        <td class="l"><span class="dim">${esc(b.members.slice(0, 4).join(", "))}${b.members.length > 4 ? ` +${b.members.length - 4}` : ""}</span></td>
        <td>${dmy(b.last)}</td></tr>`).join("")}</tbody></table>
      <div class="sec" style="margin-top:1rem">Every trade</div>${tradesTable(M.trades || [])}` :
      `<p class="intro">No member of Congress disclosed a trade in what you hold in the last year.</p>`}`;
}

function tabLeaders() {
  const rows = (O.leaders || []).filter(m => (m.n_scored_12m || 0) >= (F.minN ?? 10));
  const col = F.sort || "avg_alpha_12m";
  rows.sort((a, b) => (b[col] ?? -1e9) - (a[col] ?? -1e9));
  const th = (k, t, title) => `<th class="sort ${col === k ? "on" : ""}" data-k="${k}" title="${esc(title || "")}">${t}</th>`;
  $("#tab").innerHTML = `<p class="note2">Sitting members ranked by the average twelve-month alpha of their purchases from the <b>first open after
      the filing</b>: what copying them would have earned. Below about |t| = 2 a rank is noise. <i>Own entry</i> is the same trades from the
      member's own trade date; the gap is what the filing lag costs.</p>
    <div class="filters"><span class="lbl">At least</span>${[5, 10, 25, 50].map(n => `<button class="seg ${(F.minN ?? 10) === n ? "on" : ""}" data-n="${n}">${n} scored</button>`).join("")}
      <span class="dim">${rows.length} members</span></div>
    <table class="tbl"><thead><tr><th class="l">Member</th>${th("n_scored_12m", "Scored", "purchases with twelve months of prices")}
      ${th("avg_alpha_12m", "12m alpha", "equal weight, from the filing")}${th("wt_alpha_12m", "$-weighted", "weighted by the size filed")}
      ${th("hit_rate_12m", "Hit rate", "share of buys that beat SPY over 12 months")}${th("alpha_12m_t", "t", "mean / (sd / sqrt n)")}
      ${th("avg_lag_days", "Lag", "average days from trade to filing")}${th("avg_member_alpha_3m", "Own entry 3m", "from the member's own trade date")}
      ${th("lag_cost_3m", "Lost to lag", "3m: own entry minus the filing's")}${th("alpha_in_remit_12m", "In remit", "12m alpha inside their committees' remit")}
      ${th("alpha_out_remit_12m", "Out", "12m alpha outside it")}</tr></thead><tbody>
      ${rows.map(m => `<tr><td class="l"><span class="party ${esc(m.party)}">${esc(m.party || "?")}</span><a href="#m=${esc(m.bg)}">${esc(m.name)}</a>
          ${m.chamber === "senate" ? `<span class="ch">sen</span>` : ""}${m.tracked ? `<span class="remit" title="in the in-remit strategy">tracked</span>` : ""}</td>
        <td>${nf(m.n_scored_12m, 0)}</td><td>${al(m.avg_alpha_12m)}</td><td>${al(m.wt_alpha_12m)}</td>
        <td>${m.hit_rate_12m != null ? nf(m.hit_rate_12m * 100, 0) + "%" : "–"}</td>
        <td class="${Math.abs(m.alpha_12m_t || 0) >= 2 ? "" : "dim"}">${m.alpha_12m_t != null ? nf(m.alpha_12m_t, 1) : "–"}</td>
        <td>${m.avg_lag_days != null ? nf(m.avg_lag_days, 0) + " d" : "–"}</td><td>${al(m.avg_member_alpha_3m)}</td><td>${al(m.lag_cost_3m)}</td>
        <td>${al(m.alpha_in_remit_12m)}</td><td>${al(m.alpha_out_remit_12m)}</td></tr>`).join("")}</tbody></table>`;
  $$("#tab .seg").forEach(b => b.onclick = () => { F.minN = +b.dataset.n; tabLeaders(); wrapTables(); });
  $$("#tab th.sort").forEach(h => h.onclick = () => { F.sort = h.dataset.k; tabLeaders(); wrapTables(); });
}

function tabStrategy() {
  const S = O.strategy;
  if (!S || !S.equal || !S.equal.d) { $("#tab").innerHTML = `<p class="intro">The strategy has not been built yet.</p>`; return; }
  const W = F.wt === "size" ? S.size : S.equal;
  const x = v => v == null ? "–" : nf(v, 2) + "x";
  $("#tab").innerHTML = `<p class="note2">Every purchase inside the buyer's own committee remit, bought at the first open after the filing and held
      ${S.hold_days ? Math.round(S.hold_days / 30) + " months" : "a year"}, against the S&P 500 (SPY) over the same days. The rule selects trades,
      not people. Committee seats are read as they were on the day of each trade.</p>
    <div class="filters"><span class="lbl">Weight</span><button class="seg ${F.wt !== "size" ? "on" : ""}" data-w="equal">Equal (the headline)</button>
      <button class="seg ${F.wt === "size" ? "on" : ""}" data-w="size">By size filed</button></div>
    <div class="tiles">
      ${tile("Strategy", x(W.final_strategy), null, `since ${dmy(W.start)} · ${nf(W.n_trades, 0)} trades`, true)}
      ${tile("S&P 500", x(W.final_benchmark), null, "SPY over the same days")}
      ${tile("A year", W.cagr != null ? pc(W.cagr * 100, 1) : "–", null, `the S&P 500 ${W.bench_cagr != null ? pc(W.bench_cagr * 100, 1) : "–"}`)}
      ${tile("Worst fall", W.max_drawdown != null ? pc(-Math.abs(W.max_drawdown) * 100, 1) : "–", null, `the S&P 500 ${W.bench_max_drawdown != null ? pc(-Math.abs(W.bench_max_drawdown) * 100, 1) : "–"}`)}
      ${tile("Invested", W.exposure != null ? nf(W.exposure * 100, 0) + "%" : "–", null, "of the days")}
      ${tile("Members", nf(S.n_members, 0), null, "with an in-remit buy")}
    </div>
    <div class="lc" id="sChart" style="margin-top:.6rem"></div>
    <div class="grid2" style="margin-top:.8rem">
      <div><div class="sec">What a reader starting today would hold</div>
        <table class="tbl"><thead><tr><th class="l">Ticker</th><th>Weight</th><th>Since</th><th class="l">Bought by</th></tr></thead><tbody>
        ${(S.basket || []).map(b => `<tr><td class="l"><a href="#t=${esc(b.ticker)}">${esc(b.ticker)}</a></td><td>${nf((F.wt === "size" ? b.size_weight : b.equal_weight) * 100, 1)}%</td>
          <td>${dmy(b.first_entry)}</td><td class="l"><span class="dim">${esc((b.members || []).join(", "))}</span></td></tr>`).join("")}</tbody></table></div>
      <div><div class="sec">This week</div>${changesHtml(S.changes)}</div>
    </div>`;
  $$("#tab .seg").forEach(b => b.onclick = () => { F.wt = b.dataset.w; tabStrategy(); wrapTables(); });
  lchart($("#sChart"), { series: [{ name: "In-remit strategy", color: "var(--accent)", w: 2, pts: W.d.map((d, i) => [d, W.s[i]]) },
    { name: "S&P 500", color: "var(--s2)", pts: W.d.map((d, i) => [d, W.b[i]]) }], yfmt: v => v.toFixed(2) + "x", endLabels: true,
    refs: [{ y: 1 }], height: 260, label: "The in-remit strategy against the S&P 500" });
}

function changesHtml(C) {
  const list = (rows, verb) => (rows || []).length ? rows.slice(0, 12).map(r => `<div class="seat"><b>${esc(r.ticker)}</b> ${verb} ${dmy(r.date)}
    <span class="t">${esc((r.members || []).join(", "))}</span></div>`).join("") : `<div class="seat dim">none</div>`;
  if (!C) return `<p class="intro">–</p>`;
  return `<div class="lbl" style="font-size:.58rem;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-soft)">opened</div>${list(C.entries || C.opened, "opened")}
    <div class="lbl" style="font-size:.58rem;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-soft);margin-top:.5rem">timed out</div>${list(C.exits || C.closed, "closed")}`;
}

function tabCommittees() {
  const rows = O.committees || [];
  $("#tab").innerHTML = `<p class="note2">Every full committee, ranked by what its members' purchases <b>inside its own jurisdiction</b> returned over
      twelve months from the filing. A member sits on several committees, so one trade counts under each. The access weight is the tracker's
      judgement of how much market-moving information a seat exposes; it does not predict returns, and the tracker measures that too.</p>
    <table class="tbl"><thead><tr><th class="l">Committee</th><th>Access</th><th>Seats</th><th>Decisions</th><th>12m alpha</th><th>Hit rate</th>
      <th class="l">Most bought</th></tr></thead><tbody>
      ${rows.map(c => `<tr><td class="l"><a href="#c=${esc(c.committee_id)}">${esc(c.short_name || c.name)}</a><span class="ch">${esc(c.chamber || "")}</span></td>
        <td>${c.sensitivity != null ? nf(c.sensitivity, 2) : "–"}</td><td>${nf(c.n_seats, 0)}</td><td>${nf(c.n_decisions, 0)}</td>
        <td>${al(c.alpha_12m)}</td><td>${c.hit_rate != null ? nf(c.hit_rate * 100, 0) + "%" : "–"}</td>
        <td class="l"><span class="dim">${esc((c.top_tickers || []).join(", "))}</span></td></tr>`).join("")}</tbody></table>
    ${validationHtml(O.validation)}`;
}

function validationHtml(v) {
  if (!v) return "";
  return `<p class="note2">The A-E grade describes how deliberate a trade looks (inside the remit, alone in its filing, an obscure name, a new
    position, the member's own account). It is tested against returns on every run: A and B against D and E,
    ${v.top_minus_bottom != null ? `<b>${pc(v.top_minus_bottom * 100, 2)}</b> at t = ${nf(v.t_stat, 2)}` : "–"}: ${v.predictive ? "it predicts" : "it does <b>not</b> predict returns"}.</p>`;
}

function tabScans() {
  const rows = O.ocr || [];
  $("#tab").innerHTML = `<p class="note2">Scanned paper filings have no text, so the tracker reads them with Windows OCR. Those rows are kept
      apart: <b>unreviewed</b>, never scored, never in the leaderboard or the strategy. Check them against the filing.</p>
    ${rows.length ? `<table class="tbl"><thead><tr><th>Filed</th><th class="l">Member</th><th class="l">Asset, as read</th><th>Type</th><th>Traded</th>
      <th>Amount</th><th>Read</th><th></th></tr></thead><tbody>
      ${rows.map(r => `<tr><td>${dmy(r.filed)}</td><td class="l">${esc(r.member || "")}</td><td class="l"><span class="dim">${esc((r.asset_name || "").slice(0, 60))}</span></td>
        <td>${side(r.tx_type)}</td><td>${esc(r.traded || "–")}</td><td>${amt(r.amount_raw)}</td><td>${r.confident ? "sure" : `<span class="warnc">unsure</span>`}</td>
        <td>${r.pdf_url ? `<a href="${esc(r.pdf_url)}" target="_blank" rel="noopener">PDF</a>` : ""}</td></tr>`).join("")}</tbody></table>` :
      `<p class="intro">No scanned filing read yet.</p>`}`;
}

/* ---------- one member ---------- */
async function renderMember(bg) {
  $("#app").innerHTML = `<div class="panel"><div class="body"><div class="center">Loading<span class="blink">_</span></div></div></div>`;
  let m;
  try { m = await api(`/pol/member/${encodeURIComponent(bg)}`); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>No such member</b>${esc(e.message)} · <a href="#tab=${TAB}">back</a></div>`; return; }
  const seats = (m.committees || []).filter(s => !s.parent_id), subs = (m.committees || []).filter(s => s.parent_id);
  $("#app").innerHTML = `<div class="panel"><div class="body">
    <a class="back" href="#tab=${TAB}">‹ all politicians</a>
    <div class="hdr2"><h1>${esc(m.name)}</h1><span class="party ${esc(m.party)}">${esc(m.party || "?")}</span>
      <span class="sub">${esc(m.chamber === "senate" ? "Senate" : "House")} · ${esc(m.seat || m.state || "")}${m.former ? " · former member" : ""}
        ${m.tracked ? " · in the in-remit strategy" : ""}</span></div>
    <div class="tiles">
      ${tile("12m alpha", al(m.avg_alpha_12m), null, `from the filing · ${nf(m.n_scored_12m || 0, 0)} buys scored`, true)}
      ${tile("$-weighted", al(m.wt_alpha_12m), null, "by the size filed")}
      ${tile("Hit rate", m.hit_rate_12m != null ? nf(m.hit_rate_12m * 100, 0) + "%" : "–", null, "beat SPY over 12 months")}
      ${tile("t", m.alpha_12m_t != null ? nf(m.alpha_12m_t, 1) : "–", null, "below about 2, noise")}
      ${tile("Lag", m.avg_lag_days != null ? nf(m.avg_lag_days, 0) + " d" : "–", null, `lost to it (3m): ${m.lag_cost_3m != null ? pc(m.lag_cost_3m * 100, 1) : "–"}`)}
      ${tile("In remit", al(m.alpha_in_remit_12m), null, `outside it ${m.alpha_out_remit_12m != null ? pc(m.alpha_out_remit_12m * 100, 1) : "–"}`)}
      ${tile("Access", m.score != null ? nf(m.score, 2) : "–", null, "the strongest seat, 0-1")}
    </div>
    ${(m.held || []).length ? `<p class="note2">Traded what you hold: ${m.held.map(t => `<a href="#t=${esc(t)}">${esc(t)}</a>`).join(", ")}.</p>` : ""}
    <div class="grid2" style="margin-top:.6rem">
      <div><div class="sec">Committees now</div>
        ${seats.map(s => `<div class="seat"><a href="#c=${esc(s.committee_id)}">${esc(s.name)}</a>${s.title ? `<span class="t">${esc(s.title)}</span>` : ""}</div>`).join("") || `<div class="seat dim">none</div>`}
        ${subs.length ? `<div class="note2">and ${subs.length} subcommittee seat${subs.length === 1 ? "" : "s"}</div>` : ""}
        <div class="sec">Their remit</div><div class="chips">${(m.remit || []).map(s => `<span class="chip2 on">${esc(s)}</span>`).join("") || `<span class="dim">none</span>`}</div></div>
      <div><div class="sec">What they buy</div>
        <table class="tbl"><thead><tr><th class="l">Sector</th><th>Buys</th><th>Size</th><th></th></tr></thead><tbody>
        ${(m.sectors || []).slice(0, 12).map(s => `<tr><td class="l">${esc(s.sector)}</td><td>${s.n}</td><td>${usd(s.usd)}</td><td>${s.in_remit ? `<span class="remit">in remit</span>` : ""}</td></tr>`).join("")}</tbody></table></div>
    </div>
    <div class="sec">Every disclosed trade</div>${tradesTable(m.trades || [], { noWho: true })}
  </div></div>`;
  wrapTables();
}

/* ---------- one committee ---------- */
async function renderCommittee(cid) {
  let c;
  try { c = await api(`/pol/committee/${encodeURIComponent(cid)}`); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>No such committee</b>${esc(e.message)} · <a href="#tab=committees">back</a></div>`; return; }
  $("#app").innerHTML = `<div class="panel"><div class="body">
    <a class="back" href="#tab=committees">‹ all committees</a>
    <div class="hdr2"><h1>${esc(c.name)}</h1><span class="sub">${esc(c.chamber || "")} · access weight ${c.sensitivity != null ? nf(c.sensitivity, 2) : "–"}</span></div>
    <div class="chips">${String(c.sectors || "").split(",").filter(Boolean).map(s => `<span class="chip2 on">${esc(s)}</span>`).join("")}</div>
    <div class="tiles">
      ${tile("12m alpha", al(c.alpha_12m), null, `inside its jurisdiction · ${nf(c.n_settled || 0, 0)} settled`, true)}
      ${tile("Hit rate", c.hit_rate != null ? nf(c.hit_rate * 100, 0) + "%" : "–", null, "beat SPY")}
      ${tile("Seats", nf(c.n_seats, 0), null, `${nf(c.n_traders || 0, 0)} of them trade`)}
      ${tile("Decisions", nf(c.n_decisions, 0), null, "in-remit buys")}
    </div>
    <div class="grid2" style="margin-top:.6rem">
      <div><div class="sec">Seated now, of those who file trades</div>
        <table class="tbl"><thead><tr><th class="l">Member</th><th>12m alpha</th><th>Scored</th></tr></thead><tbody>
        ${(c.members || []).map(m => `<tr><td class="l"><span class="party ${esc(m.party)}">${esc(m.party || "?")}</span><a href="#m=${esc(m.bg)}">${esc(m.name)}</a>
          ${m.title ? `<span class="ch">${esc(m.title)}</span>` : ""}</td><td>${al(m.avg_alpha_12m)}</td><td>${nf(m.n_scored_12m || 0, 0)}</td></tr>`).join("")}</tbody></table></div>
      <div><div class="sec">Their latest in-remit trades</div>${tradesTable((c.trades || []).slice(0, 60))}</div>
    </div></div></div>`;
  wrapTables();
}

/* ---------- one company ---------- */
async function renderTicker(sym) {
  let t;
  try { t = await api(`/pol/ticker/${encodeURIComponent(sym)}`); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load</b>${esc(e.message)}</div>`; return; }
  const held = (O.mine || {}).tickers || {};
  const mine = t.tickers.map(x => held[x]).find(Boolean);
  $("#app").innerHTML = `<div class="panel"><div class="body">
    <a class="back" href="#tab=${TAB}">‹ back</a>
    <div class="hdr2"><h1>${esc(t.tickers.join(" · "))}</h1>
      <span class="sub">${mine ? `you hold ${esc(mine.held)}${mine.how === "turbo" ? " (a turbo)" : ""} · ` : ""}in the last year:
        ${t.year.buys} buys, ${t.year.sells} sells by ${t.year.members} member${t.year.members === 1 ? "" : "s"}</span>
      <a class="code" href="/t/${encodeURIComponent(sym)}">stock page ›</a></div>
    ${tradesTable(t.trades || [], { empty: "No member of Congress has disclosed a trade in it since 2020." })}</div></div>`;
  wrapTables();
}

load();
