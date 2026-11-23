/* ==========================================================================
   ERN — earnings.

   The page is a week of reports; any report opens in a drawer on the right,
   with its own address (/m/ERN/MSFT?d=2026-10-28) so it can be bookmarked,
   and seven tabs that each load on their own:

     Overview   what is expected and what usually happens (expect + playbook)
     Live       the watch on EDGAR, the wire and the price, on the day
     Results    the numbers against the consensus as it stood before
     Report     the free write-up, and Claude's when you ask for it
     Call       the transcript read: tone, topics, every question
     Sources    8-K, exhibits, slides, 10-Q, transcript, and what you add
     History    every past report, the move, the drift, the market

   Live watches are polled every ten seconds from anywhere on the page; the
   open event's watch every three. A browser notification and a tone mark the
   moment the numbers are found, if you allow them.
   ========================================================================== */

const S = {
  week: null, scope: localGet("ern.scope") || "market", wk: null, expo: null, up: null, season: null,
  live: [], seenFound: new Set(), ev: null, tab: null, poll: {}, notify: false,
};

function localGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localSet(k, v) { try { localStorage.setItem(k, v); } catch { } }

/* ---------- formatting ---------------------------------------------------- */
const pc = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d) + "%";
const pcc = (v, d = 1) => `<span class="${sgn(v)}">${pc(v, d)}</span>`;
const big = (v, c = "$") => {
  if (v == null) return "–";
  const a = Math.abs(v), s = v < 0 ? "−" : "";
  if (a >= 1e12) return `${s}${c}${(a / 1e12).toFixed(2)}tn`;
  if (a >= 1e9) return `${s}${c}${(a / 1e9).toFixed(a >= 1e11 ? 1 : 2)}bn`;
  if (a >= 1e6) return `${s}${c}${(a / 1e6).toFixed(1)}m`;
  return `${s}${c}${a.toLocaleString("en-GB", { maximumFractionDigits: 0 })}`;
};
const CUR = { USD: "$", EUR: "€", GBP: "£", JPY: "¥", TWD: "NT$", CHF: "CHF " };
const cs = c => CUR[c] ?? "$";
const px = (v, c = "$") => v == null ? "–" : (v < 0 ? "−" : "") + c + Math.abs(v).toFixed(2);
const eurS = v => v == null ? "–" : (v > 0 ? "+" : "") + eur(v);
const DAYN = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const dObj = iso => new Date(iso.slice(0, 10) + "T12:00:00Z");
const dshort = iso => { const t = dObj(iso); return `${DAYN[t.getUTCDay()]} ${String(t.getUTCDate()).padStart(2, "0")}/${String(t.getUTCMonth() + 1).padStart(2, "0")}`; };
const dm = iso => { const m = String(iso || "").match(/^\d{4}-(\d{2})-(\d{2})/); return m ? `${m[2]}/${m[1]}` : "–"; };
const mon = iso => dObj(iso).toLocaleString("en-GB", { month: "short", year: "2-digit", timeZone: "UTC" });
const addDays = (iso, n) => { const t = dObj(iso); t.setUTCDate(t.getUTCDate() + n); return t.toISOString().slice(0, 10); };
const WHEN = { bmo: "Before the open", amc: "After the close", dmh: "During the session" };
const ACTION = { main: "maintains", reit: "reiterates", init: "starts at", up: "upgrade", down: "downgrade" };
const WHEN_S = { bmo: "pre-open", amc: "post-close", dmh: "in session" };
const ago = s => s == null ? "" : s < 60 ? `${Math.round(s)}s ago` : s < 3600 ? `${Math.round(s / 60)}m ago` : `${Math.round(s / 3600)}h ago`;
const loading = `<div class="dim ld">Loading<span class="blink">_</span></div>`;
const fail = e => `<div class="note"><b>Could not load</b>${esc(e?.message || e?.error || e?.why || e)}</div>`;

async function api(path, opts) {
  const r = await fetch("/api/pf" + path, opts);
  let j = null;
  try { j = await r.json(); } catch { }
  if (!r.ok) throw new Error((j && j.detail) || `HTTP ${r.status}`);
  return j;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const del = path => api(path, { method: "DELETE" });

/* ==========================================================================
   the page
   ========================================================================== */
$("#shell").innerHTML = termBar({ here: "ERN", right: `<button class="tbtn" id="helpBtn">Help</button><a class="tbtn" href="/">Home</a>` });
wireShell();
$("#helpBtn").onclick = showHelp;

$("#app").innerHTML = `
  <div class="hdr"><h1>ERN · EARNINGS</h1>
    <span class="sub">the calendar · what is expected · the numbers the moment they land · what they mean</span>
    <span class="sp"></span>
    <a class="seg" href="/api/pf/earnings/calendar.ics" title="Your holdings' and watched names' next reports, for Outlook, Apple or Google Calendar">Add to your calendar (.ics)</a>
  </div>
  <div class="tiles" id="tiles">${loading}</div>
  <div class="livebar" id="livebar"></div>
  <nav class="secnav"><a href="#s-cal">Calendar</a><a href="#s-mine">Coming up</a><a href="#s-season">This season</a><a href="#s-how">How it works</a></nav>
  <section class="panel" id="s-cal"><header><h2>Calendar</h2><span class="hint" id="calHint">click any report for everything about it</span></header>
    <div class="body"><div class="fbar" id="calBar"></div><div id="cal">${loading}</div><div class="src" id="calSrc"></div></div></section>
  <div class="two" style="margin-top:.8rem">
    <section class="panel" id="s-mine"><header><h2>Coming up — yours</h2><span class="hint">holdings and watched names, next 100 days</span></header>
      <div class="body" id="up">${loading}</div></section>
    <section class="panel" id="s-season"><header><h2>This season</h2><span class="hint" id="seasonHint"></span></header>
      <div class="body" id="season">${loading}</div></section>
  </div>
  <section class="panel" id="s-how" style="margin-top:.8rem"><header><h2>How it works</h2></header><div class="body prose" id="how"></div></section>`;

$("#how").innerHTML = `
  <p><b>The calendar</b> merges Nasdaq (every US company, the consensus EPS, and after the fact the reported EPS),
  Finnhub with your key (before-open or after-close, and the consensus revenue) and Yahoo (exact times for your
  holdings, and the only source for Amsterdam and Frankfurt lines). <b>Yours</b> shows your holdings and watched
  names, <b>Large caps</b> adds every company above $20bn, <b>All</b> everything. A report with a blue edge is one
  you hold; times are Amsterdam time, with a ~ when the exact time is not announced.</p>
  <p><b>Expectations are frozen before the report.</b> The moment a company reports, every free source moves its
  consensus to the next quarter. So the consensus for each of your holdings is snapshotted in the three days before
  its report (and whenever you open one), and results are always judged against that snapshot.</p>
  <p><b>Live.</b> From fifteen minutes before any of your holdings or watched names reports, a watcher asks EDGAR
  every 10 seconds, the newswire every 30 and the price every 15. When the 8-K lands, the press release is read and
  compared with the consensus at once — usually within a minute or two of the release. Press <b>Watch live</b> on
  any other report to do the same. Allow notifications to be told even when this tab is in the background.</p>
  <p><b>The report.</b> Each report gets a free write-up assembled by rules from the figures. <b>Claude</b> can
  read the documents themselves — the 8-K and its exhibits, the slides (a PDF with its charts), the 10-Q's MD&A,
  the call transcript, anything you add, and your own thesis — and write what it means. It runs through Claude Code on
  your Claude subscription (a one-time sign-in, offered on the Report tab): no charge per report, each counts toward your
  plan's usage. An Anthropic API key is the alternative, under Settings. Transcripts come from the Motley
  Fool, usually within a day; slides are often only on the company's IR site — add the link under Sources, or let
  Claude look for them.</p>
  <p class="dim">Nothing here is advice. Parsed numbers link to the document they came from: check the ones you act on.</p>`;

/* ---------- tiles --------------------------------------------------------- */
function renderTiles() {
  const w = S.wk, e = S.expo, u = S.up, s = S.season;
  const t = [];
  if (w && !w.error) {
    const sm = w.summary;
    t.push(`<div class="tile lead"><div class="k">This week · yours</div>
      <div class="v">${sm.held} report${sm.held === 1 ? "" : "s"}</div>
      <div class="s">${sm.held ? `${eur(sm.held_value_eur)} · ${sm.held_weight.toFixed(1)}% of the book` : "none of your holdings"} · ${sm.events} companies in all</div></div>`);
  }
  if (e && !e.error) {
    t.push(`<div class="tile"><div class="k">Options price for them</div>
      <div class="v">${e.priced ? "±" + eur(e.eur) : "–"}</div>
      <div class="s">${e.priced ? `the sum of each report's implied move on your position (${e.priced} of ${e.n} priced)` : e.n ? "no option prices for this week's reports" : "nothing of yours this week"}</div></div>`);
  }
  const nx = (u?.events || []).find(x => x.held && x.days >= 0);
  if (nx) t.push(`<div class="tile click" data-open="${esc(nx.sym)}" data-d="${nx.date}"><div class="k">Next of yours</div>
      <div class="v">${esc(nx.sym)}<small>${nx.days === 0 ? "today" : `in ${nx.days} day${nx.days === 1 ? "" : "s"}`}</small></div>
      <div class="s">${dshort(nx.date)} ${nx.time_local ? "· ~" + nx.time_local : ""} · ${nx.held.weight}% of the book</div></div>`);
  const act = S.live.filter(x => x.active);
  t.push(`<div class="tile${act.length ? " lead" : ""}"><div class="k">Live watches</div>
      <div class="v">${act.length ? `${act.length} <small>${act.filter(x => x.found).length} in</small>` : "none"}</div>
      <div class="s">${act.length ? "EDGAR every 10s, the wire every 30s, the price every 15s" : "start 15 minutes before a holding reports, or press Watch live on any report"}</div></div>`);
  if (s && !s.error) {
    const m = s.mine_stats;
    t.push(`<div class="tile"><div class="k">Your season so far</div>
      <div class="v">${m.n ? `${m.beat}/${m.n} beat` : "–"}</div>
      <div class="s">${m.n ? `average move ${pc(m.avg_move)} · ${eurS(m.eur)} on report days · ${m.remaining} still to report` : "none of yours reported yet"}</div></div>`);
    const k = s.market;
    t.push(`<div class="tile"><div class="k">Market season</div>
      <div class="v">${k.beat_pct != null ? k.beat_pct.toFixed(0) + "% beat" : "–"}</div>
      <div class="s">${k.n ? `${k.n} companies above $10bn since ${dm(s.start)} · median EPS surprise ${pc(k.median_surprise)}` : ""}</div></div>`);
  }
  $("#tiles").innerHTML = t.join("") || loading;
  $$("#tiles [data-open]").forEach(el => el.onclick = () => openEvent(el.dataset.open, el.dataset.d));
}

/* ---------- the week ------------------------------------------------------ */
function calBar() {
  const w = S.wk;
  const range = w ? `${dm(w.week_of)} – ${dm(addDays(w.week_of, 6))}` : "";
  $("#calBar").innerHTML = `
    <span class="grp"><button class="seg" id="wkPrev" title="Previous week">‹</button><button class="seg" id="wkNow">This week</button><button class="seg" id="wkNext" title="Next week">›</button></span>
    <span class="wk">${range}</span>
    <span class="grp">${[["mine", "Yours"], ["market", "+ Large caps"], ["all", "All"]].map(([k, l]) =>
      `<button class="seg ${S.scope === k ? "on" : ""}" data-scope="${k}">${l}</button>`).join("")}</span>
    <span class="sp"></span>
    <input type="text" id="openSym" placeholder="open any company: NVDA" spellcheck="false" autocomplete="off">`;
  $("#wkPrev").onclick = () => loadWeek(addDays(S.week || S.wk.week_of, -7));
  $("#wkNext").onclick = () => loadWeek(addDays(S.week || S.wk.week_of, 7));
  $("#wkNow").onclick = () => loadWeek(null);
  $$("#calBar [data-scope]").forEach(b => b.onclick = () => { S.scope = b.dataset.scope; localSet("ern.scope", S.scope); loadWeek(S.week); });
  $("#openSym").addEventListener("keydown", ev => {
    if (ev.key !== "Enter") return;
    const s = ev.target.value.trim().toUpperCase();
    if (s) openEvent(s, null);
    ev.target.value = "";
  });
}

async function loadWeek(week) {
  S.week = week;
  $("#cal").innerHTML = loading;
  try {
    S.wk = await api(`/earnings/week?scope=${S.scope}` + (week ? `&week=${week}` : ""));
  } catch (e) { $("#cal").innerHTML = fail(e); return; }
  if (S.wk.error) { $("#cal").innerHTML = fail(S.wk.error); return; }
  calBar(); renderWeek(); renderTiles();
  api(`/earnings/exposure` + (week ? `?week=${week}` : "")).then(x => { S.expo = x; renderTiles(); }).catch(() => { });
}

/* Read-through, from the supply-chain map (SPLC): the holdings a reporter
   sells to and buys from. */
const andList = xs => xs.length < 2 ? (xs[0] || "") : xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1];
const tieWords = c => [c.sells_to.length ? "sells to " + andList(c.sells_to) : "", c.buys_from.length ? "buys from " + andList(c.buys_from) : ""]
  .filter(Boolean).join("; ") + ` — ${c.weight.toFixed(1)}% of your book`;

function evChip(e) {
  const cls = ["ev", e.held ? "held" : "", e.watch && !e.held ? "watch" : "",
    S.ev && S.ev.sym === e.sym && S.ev.d === e.date ? "sel" : ""].join(" ");
  let right = "";
  if (e.live && !e.live.found) right = `<span class="pulse" title="watching live"></span>`;
  else if (e.status === "reported" && e.surprise != null)
    right = `<span class="${sgn(e.surprise)}">${pc(e.surprise)}</span>`;
  else if (e.status === "reported") right = `<span class="dim">in</span>`;
  else if (e.time_local) right = `<span class="dim">${e.time_exact ? "" : "~"}${e.time_local}</span>`;
  const meta = [];
  if (e.held) meta.push(`<span class="w" title="your position">${e.held.weight}% · ${eur(e.held.value_eur)}</span>`);
  else if (e.watch) meta.push(`<span title="watched">☆ watched</span>`);
  if (e.status === "reported" && e.eps_act != null) meta.push(`<span>EPS ${px(e.eps_act)} vs ${px(e.eps_est)}</span>`);
  else if (e.eps_est != null) meta.push(`<span title="consensus EPS">EPS ${px(e.eps_est)}</span>`);
  if (e.rev_est != null && e.status !== "reported") meta.push(`<span title="consensus revenue">rev ${big(e.rev_est)}</span>`);
  if (!e.held && e.mcap) meta.push(`<span title="market value">${big(e.mcap)}</span>`);
  if (e.chain && e.chain.strong && !e.held) meta.push(`<span class="tie" title="${esc(tieWords(e.chain))}">⇄ ${e.chain.n} of yours</span>`);
  return `<button class="${cls}" data-sym="${esc(e.sym)}" data-d="${e.date}" title="${esc(e.name || e.sym)}">
    <span class="t">${esc(e.sym)}</span><span class="n">${esc(e.name || "")}</span><span class="r">${right}</span>
    <span class="m">${meta.join("")}</span></button>`;
}

function renderWeek() {
  const w = S.wk;
  const days = w.days.filter((d, i) => i < 5 || d.events.length);
  const html = days.map(d => {
    const groups = [["bmo", "Before the open"], ["amc", "After the close"], ["x", "Time not announced"]];
    const by = { bmo: [], amc: [], x: [] };
    for (const e of d.events) (by[e.when === "bmo" || e.when === "amc" ? e.when : "x"]).push(e);
    const body = groups.filter(([k]) => by[k].length).map(([k, l]) =>
      `<div class="slot"><div class="sl">${l}</div>${by[k].map(evChip).join("")}</div>`).join("");
    const cls = ["day", d.date === w.today ? "today" : "", d.date < w.today ? "past" : ""].join(" ");
    return `<div class="${cls}"><div class="dh"><b>${dshort(d.date)}</b><span>${d.total} report${d.total === 1 ? "s" : ""}</span></div>
      ${body || `<div class="empty">${d.total ? "none of yours" : "no reports"}</div>`}
      ${d.hidden ? `<div class="dayfoot">+${d.hidden} more — ${S.scope === "all" ? "smaller companies" : `<a href="#" data-scope-all>show all</a>`}</div>` : ""}</div>`;
  }).join("");
  $("#cal").innerHTML = `<div class="grid" style="--cols:${days.length}">${html}</div>`;
  $$("#cal .ev").forEach(b => b.onclick = () => openEvent(b.dataset.sym, b.dataset.d));
  $$("#cal [data-scope-all]").forEach(a => a.onclick = ev => { ev.preventDefault(); S.scope = "all"; localSet("ern.scope", "all"); loadWeek(S.week); });
  const src = w.sources;
  $("#calSrc").innerHTML = `Nasdaq ${src.nasdaq} companies · Finnhub ${src.finnhub_key ? src.finnhub + " rows" : "— add a Finnhub key in Settings for before/after-close times and revenue consensus"} · Yahoo for your holdings' exact times. Times in Amsterdam time.`;
}

/* ---------- coming up, the season ---------------------------------------- */
async function loadUpcoming() {
  try { S.up = await api("/earnings/upcoming?days=100"); } catch (e) { $("#up").innerHTML = fail(e); return; }
  const ev = S.up.events;
  if (!ev.length) { $("#up").innerHTML = `<div class="dim">No reports known for your holdings in the next 100 days.</div>`; return; }
  $("#up").innerHTML = `<div class="tscroll"><table class="tbl"><thead><tr><th>Date</th><th>When</th><th class="l">Company</th><th>Weight</th><th>Value</th><th>EPS est.</th><th>Days</th></tr></thead><tbody>
    ${ev.map(e => `<tr class="pick" data-sym="${esc(e.sym)}" data-d="${e.date}"><td>${dshort(e.date)}</td>
      <td class="dim">${e.time_local ? (e.time_exact ? "" : "~") + e.time_local : "–"} ${e.when ? WHEN_S[e.when] : ""}</td>
      <td class="l"><b>${esc(e.sym)}</b> <span class="dim sans">${esc((e.name || "").slice(0, 28))}</span>${e.watch && !e.held ? ' <span class="bdg info">watched</span>' : ""}</td>
      <td>${e.held ? e.held.weight.toFixed(2) + "%" : "–"}</td><td>${e.held ? eur(e.held.value_eur) : "–"}</td>
      <td>${px(e.eps_est)}</td><td>${e.days}</td></tr>`).join("")}</tbody></table></div>`;
  $$("#up tr.pick").forEach(r => r.onclick = () => openEvent(r.dataset.sym, r.dataset.d));
  renderTiles();
}

async function loadSeason() {
  try { S.season = await api("/earnings/season"); } catch (e) { $("#season").innerHTML = fail(e); return; }
  const s = S.season;
  if (s.error) { $("#season").innerHTML = fail(s.error); return; }
  $("#seasonHint").textContent = `since ${dm(s.start)}`;
  const m = s.mine_stats, k = s.market;
  $("#season").innerHTML = `
    <div class="kv" style="margin-bottom:.6rem">
      <div><div class="k">Yours reported</div><div class="v">${m.n}</div><div class="t">${m.beat} beat the EPS consensus · ${m.remaining} to come</div></div>
      <div><div class="k">Their move on the day</div><div class="v">${pc(m.avg_move)}</div><div class="t">average · ${eurS(m.eur)} on your positions</div></div>
      <div><div class="k">Market beat rate</div><div class="v">${k.beat_pct != null ? k.beat_pct.toFixed(0) + "%" : "–"}</div><div class="t">${k.n} companies above $10bn · ${k.miss_pct ?? "–"}% missed</div></div>
    </div>
    ${s.mine.length ? `<div class="tscroll" style="max-height:420px;overflow-y:auto"><table class="tbl"><thead><tr><th>Date</th><th class="l">Company</th><th>EPS</th><th>Surprise</th><th>Move</th><th>Your €</th></tr></thead><tbody>
    ${s.mine.map(r => `<tr class="pick" data-sym="${esc(r.sym)}" data-d="${r.date}"><td>${dm(r.date)}</td>
      <td class="l"><b>${esc(r.as || r.sym)}</b> <span class="dim sans">${esc((r.name || "").slice(0, 24))}</span></td>
      <td>${px(r.eps_act)}</td><td>${pcc(r.surprise)}</td><td>${pcc(r.reaction?.move)}</td>
      <td class="${sgn(r.eur)}">${r.eur != null ? eurS(r.eur) : "–"}</td></tr>`).join("")}</tbody></table></div>` : `<div class="dim">None of your holdings has reported yet this season.</div>`}
    <div class="src">EPS surprise against the Zacks consensus (Nasdaq); the move is the first session after the report, close to close.</div>`;
  $$("#season tr.pick").forEach(r => r.onclick = () => openEvent(r.dataset.sym, r.dataset.d));
  renderTiles();
}

/* ---------- live, everywhere --------------------------------------------- */
async function pollLive() {
  let r;
  try { r = await api("/earnings/live"); } catch { return; }
  S.live = r.watches || [];
  for (const w of S.live) {
    const k = `${w.symbol}:${w.date}`;
    if (w.found && !S.seenFound.has(k)) {
      S.seenFound.add(k);
      if (S.bootDone) announce(w);
    }
  }
  S.bootDone = true;
  renderLiveBar(); renderTiles();
  if (S.ev && S.tab === "live") renderLive();
}

function renderLiveBar() {
  const act = S.live.filter(x => x.active);
  $("#livebar").innerHTML = act.map(w => {
    const n = w.numbers || {};
    const txt = w.found
      ? `<b>${esc(w.symbol)}</b> in · ${n.eps != null ? `EPS ${px(n.eps)} ${n.eps_surprise != null ? pcc(n.eps_surprise) : ""}` : ""}
         ${n.revenue != null ? ` · rev ${big(n.revenue)} ${n.rev_surprise != null ? pcc(n.rev_surprise) : ""}` : ""}
         ${w.price?.move != null ? ` · stock ${pcc(w.price.move)}` : ""}`
      : `<b>${esc(w.symbol)}</b> waiting · ${w.checks} checks · ${w.wire ? "on the wire" : "EDGAR " + ago(w.age_s)}`;
    return `<div class="livecard ${w.found ? "ok" : ""}" data-sym="${esc(w.symbol)}" data-d="${w.date}"><span class="pulse ${w.found ? "ok" : ""}"></span>${txt}</div>`;
  }).join("");
  $$("#livebar .livecard").forEach(c => c.onclick = () => openEvent(c.dataset.sym, c.dataset.d, true, "live"));
}

function beep() {
  try {
    const a = new (window.AudioContext || window.webkitAudioContext)();
    [880, 1320].forEach((f, i) => {
      const o = a.createOscillator(), g = a.createGain();
      o.frequency.value = f; o.connect(g); g.connect(a.destination);
      g.gain.setValueAtTime(0.0001, a.currentTime + i * 0.18);
      g.gain.exponentialRampToValueAtTime(0.2, a.currentTime + i * 0.18 + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, a.currentTime + i * 0.18 + 0.16);
      o.start(a.currentTime + i * 0.18); o.stop(a.currentTime + i * 0.18 + 0.17);
    });
  } catch { }
}

function announce(w) {
  const n = w.numbers || {};
  const body = [n.eps != null ? `EPS ${px(n.eps)}${n.eps_surprise != null ? ` (${pc(n.eps_surprise)})` : ""}` : null,
    n.revenue != null ? `revenue ${big(n.revenue)}${n.rev_surprise != null ? ` (${pc(n.rev_surprise)})` : ""}` : null]
    .filter(Boolean).join(" · ") || "the filing is on EDGAR";
  beep();
  flash(`<b>${esc(w.symbol)}</b> results are in — ${esc(body)}`);
  try {
    if (window.Notification && Notification.permission === "granted") {
      const nt = new Notification(`${w.symbol} results are in`, { body, tag: `ern-${w.symbol}-${w.date}` });
      nt.onclick = () => { window.focus(); openEvent(w.symbol, w.date, true, "live"); };
    }
  } catch { }
}

/* ==========================================================================
   the event drawer
   ========================================================================== */
const TABS = [["overview", "Overview"], ["live", "Live"], ["results", "Results"], ["report", "Report"],
  ["call", "Call"], ["sources", "Sources"], ["history", "History"]];

function part(name) {
  const ev = S.ev;
  if (!ev) return Promise.reject("no event");
  if (!ev.parts[name]) {
    ev.parts[name] = api(`/earnings/event/${encodeURIComponent(ev.sym)}/${name}?d=${ev.d}`)
      .catch(e => { delete ev.parts[name]; throw e; });
  }
  return ev.parts[name];
}
function refresh(name) { if (S.ev) delete S.ev.parts[name]; return part(name); }

function closeEvent(push = true) {
  S.ev = null;
  for (const k in S.poll) { clearInterval(S.poll[k]); delete S.poll[k]; }
  $("#drawer").classList.remove("open"); $("#scrim").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  if (push) history.pushState({}, "", "/m/ERN");
  if (S.wk) renderWeek();
}

async function openEvent(sym, d, push = true, tab = null) {
  sym = String(sym).toUpperCase();
  for (const k in S.poll) { clearInterval(S.poll[k]); delete S.poll[k]; }
  S.ev = { sym, d, parts: {}, meta: null };
  const dr = $("#drawer");
  dr.innerHTML = `<div class="dw-h"><div class="dw-t"><h2>${esc(sym)}</h2><span class="sp"></span><button class="x" id="dwX" title="Close (Esc)">✕</button></div></div><div class="dw-b">${loading}</div>`;
  dr.classList.add("open"); $("#scrim").classList.add("open"); dr.setAttribute("aria-hidden", "false");
  $("#dwX").onclick = () => closeEvent();
  let ev;
  try { ev = await api(`/earnings/event/${encodeURIComponent(sym)}` + (d ? `?d=${d}` : "")); }
  catch (e) { $(".dw-b").innerHTML = fail(e); return; }
  if (S.ev?.sym !== sym) return;
  if (!ev.ok) { $(".dw-b").innerHTML = `<div class="note"><b>${esc(sym)}</b>${esc(ev.why || "no earnings dates known")}</div>`; return; }
  S.ev.sym = ev.symbol; S.ev.d = ev.date; S.ev.meta = ev;
  const url = `/m/ERN/${encodeURIComponent(ev.symbol)}?d=${ev.date}`;
  if (push && location.pathname + location.search !== url) history.pushState({ sym: ev.symbol, d: ev.date }, "", url);
  document.title = `${ev.symbol} earnings · ERN`;
  const liveOn = ev.live && (ev.live.active || ev.live.found);
  const def = tab || (liveOn && Math.abs(ev.days) <= 1 ? "live" : ev.reported ? "results" : "overview");
  renderHead();
  selectTab(def);
  if (S.wk) renderWeek();
}

function renderHead() {
  const ev = S.ev.meta;
  const q = ev.quote || {}, pos = ev.position;
  const status = ev.reported ? `<span class="bdg good">reported</span>` :
    ev.days === 0 ? `<span class="bdg warn">today</span>` : `<span class="bdg acc">in ${ev.days} day${ev.days === 1 ? "" : "s"}</span>`;
  const time = ev.time_local ? `${ev.time_exact ? "" : "~"}${ev.time_local} Amsterdam${ev.time_et ? ` (${ev.time_et} New York)` : ""}` : "time not announced";
  const nav = (lab, d) => d ? `<button class="seg" data-go="${d}" title="${d}">${lab}</button>` : `<button class="seg" disabled>${lab}</button>`;
  $("#drawer").innerHTML = `
    <div class="dw-h">
      <div class="dw-t"><h2>${esc(ev.symbol)}</h2><span class="nm">${esc(ev.name)}</span>${status}
        ${ev.estimated ? `<span class="bdg warn" title="The company has not confirmed this date; it is estimated from past reports">date estimated</span>` : ""}
        <span class="sp"></span>
        <span class="grp" style="display:inline-flex">${nav("‹ previous report", ev.prev)}${nav("next ›", ev.next)}</span>
        <button class="x" id="dwX" title="Close (Esc)">✕</button></div>
      <div class="dw-m"><span><b>${dshort(ev.date)} ${ev.date.slice(0, 4)}</b> · ${esc(ev.when_label)} · ${esc(time)}</span>
        ${q.price != null ? `<span>${esc(ev.currency || "")} <b>${q.price.toFixed(2)}</b> ${pcc(q.change_pct, 2)}</span>` : ""}
        ${ev.market_cap ? `<span>${big(ev.market_cap)}</span>` : ""}
        ${pos ? `<span class="w" style="color:var(--accent)">you hold ${pos.weight.toFixed(2)}% · ${eur(pos.value_eur)}${pos.as !== ev.symbol ? ` (as ${esc(pos.as)})` : ""}</span>` : ""}
        <span class="sp"></span>
        <button class="seg" id="pin">${ev.watch ? "★ watched" : "☆ watch"}</button>
        <a class="seg" href="/t/${encodeURIComponent(ev.symbol)}">security page →</a></div>
      <div class="tabs" id="dwTabs">${TABS.map(([k, l]) => `<button class="tab" data-tab="${k}">${l}${k === "live" && ev.live?.active ? '<span class="dot"></span>' : ""}</button>`).join("")}</div>
    </div>
    <div class="dw-b" id="dwBody"></div>`;
  $("#dwX").onclick = () => closeEvent();
  $$("#drawer [data-go]").forEach(b => b.onclick = () => openEvent(ev.symbol, b.dataset.go));
  $$("#dwTabs .tab").forEach(b => b.onclick = () => selectTab(b.dataset.tab));
  $("#pin").onclick = async () => {
    try { await post(`/earnings/watch/${encodeURIComponent(ev.symbol)}`, { on: !ev.watch }); ev.watch = !ev.watch; renderHead(); selectTab(S.tab); loadWeek(S.week); loadUpcoming(); } catch (e) { flash(esc(e.message)); }
  };
}

function selectTab(t) {
  S.tab = t;
  for (const k of ["live", "report"]) if (S.poll[k]) { clearInterval(S.poll[k]); delete S.poll[k]; }
  $$("#dwTabs .tab").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
  $("#dwBody").innerHTML = loading;
  ({ overview: renderOverview, live: renderLive, results: renderResults, report: renderReport,
     call: renderCall, sources: renderSources, history: renderHistory })[t]();
}
const body = () => $("#dwBody");
const still = t => S.ev && S.tab === t;

/* ---------- Overview: expectations and the playbook ---------------------- */
/* The two halves load separately: the playbook is usually cached and quick,
   the expectations read last quarter's release and call and can take a few
   seconds the first time. Neither waits for the other. */
async function renderOverview() {
  body().innerHTML = `<div id="ovExp">${loading}</div><div id="ovPb">${loading}</div>`;
  const ev0 = S.ev;
  part("playbook").then(v => ({ status: "fulfilled", value: v }), e => ({ status: "rejected", reason: e }))
    .then(pb => { if (still("overview") && S.ev === ev0) renderPlaybook(pb); });
  const ex = await part("expect").then(v => ({ status: "fulfilled", value: v }), e => ({ status: "rejected", reason: e }));
  if (!still("overview") || S.ev !== ev0) return;
  const ev = S.ev.meta, c = cs(ev.currency);
  let h = chainBlock(ev);
  if (ex.status === "fulfilled" && !ex.value.error) {
    const x = ex.value, k = x.consensus || {}, q = x.quarter || {};
    const tr = q.trend || {}, rv = q.revisions || {};
    h += `<div class="blk"><h3>What is expected${x.reported ? " (as it stood before the report)" : ""}<span class="hint">${esc(k.src || "")}</span></h3>
      <div class="kv">
        <div><div class="k">EPS consensus</div><div class="v">${px(k.eps, c)}</div>
          <div class="t">${k.eps_low != null ? `range ${px(k.eps_low, c)} – ${px(k.eps_high, c)}` : ""}${k.n_eps ? ` · ${k.n_eps} analysts` : ""}${x.dispersion != null ? ` · spread ${x.dispersion}%` : ""}</div></div>
        <div><div class="k">Revenue consensus</div><div class="v">${big(k.rev, c)}</div>
          <div class="t">${k.rev_low != null ? `range ${big(k.rev_low, c)} – ${big(k.rev_high, c)}` : k.rev == null ? "not known for this report" : ""}${k.n_rev ? ` · ${k.n_rev} analysts` : ""}</div></div>
        ${q.eps ? `<div><div class="k">Growth expected</div><div class="v">${pc(q.eps.growth)} <small class="dim">EPS</small></div>
          <div class="t">revenue ${pc(q.rev?.growth)} on a year ago (EPS ${px(q.eps.year_ago, c)})</div></div>` : ""}
        ${tr.current != null ? `<div><div class="k">How the estimate moved</div><div class="v">${pc(tr.chg_90d)} <small class="dim">90 days</small></div>
          <div class="t">${pc(tr.chg_30d)} in 30 days · revisions up ${rv.up30 ?? "–"} / down ${rv.down30 ?? "–"} (30d)</div></div>` : ""}
        ${k.implied?.move ? `<div><div class="k">Options price</div><div class="v">±${k.implied.move.toFixed(1)}%</div>
          <div class="t">the move priced for the report (expiry ${dm(k.implied.expiry)}; the full straddle ±${(k.implied.straddle_move || 0).toFixed(1)}%)</div></div>` : ""}
      </div>`;
    if ((x.guidance_vs || []).length) {
      h += `<div class="tscroll" style="margin-top:.6rem"><table class="tbl"><thead><tr><th>Guided last quarter</th><th>Low</th><th>High</th><th>Midpoint</th><th>Consensus</th><th>Street vs guide</th></tr></thead><tbody>
        ${x.guidance_vs.map(g => `<tr><td>${g.metric === "eps" ? "EPS" : "Revenue"}</td>
          <td>${g.metric === "eps" ? px(g.low, c) : big(g.low, c)}</td><td>${g.metric === "eps" ? px(g.high, c) : big(g.high, c)}</td>
          <td>${g.metric === "eps" ? px(g.mid, c) : big(g.mid, c)}</td><td>${g.metric === "eps" ? px(g.consensus, c) : big(g.consensus, c)}</td>
          <td>${pcc(g.gap_pct)}</td></tr>`).join("")}</tbody></table></div>
        <div class="src">The street above the company's own midpoint is a bar the company has to clear by more than its guide.</div>`;
    }
    if ((x.guidance_prev || []).length || (x.call_guidance_prev || []).length) {
      h += `<details style="margin-top:.5rem"><summary class="dim" style="cursor:pointer;font-size:.68rem">What they said last quarter about the outlook (${dm(x.prev_date)})</summary>
        <ul class="say">${[...(x.guidance_prev || []).map(g => g.text), ...(x.call_guidance_prev || []).map(g => `${g.speaker}: ${g.text}`)].slice(0, 12).map(t => `<li>${esc(t)}</li>`).join("")}</ul>
        <div class="src">${x.prev_sources?.release ? `<a href="${esc(x.prev_sources.release)}" target="_blank" rel="noopener">last release</a>` : ""} ${x.prev_sources?.transcript ? ` · <a href="${esc(x.prev_sources.transcript)}" target="_blank" rel="noopener">last call</a>` : ""}</div></details>`;
    }
    h += `</div>`;
    if ((x.watch || []).length || (x.segments || []).length) {
      h += `<div class="two"><div class="blk"><h3>What to watch<span class="hint">last quarter's call, in short</span></h3>
        ${(x.watch || []).length ? `<ul class="say">${x.watch.map(t => `<li>${esc(t).replace(/^([^—–-]{3,60}?)\s+[-–—]{1,2}\s+/, "<b>$1</b> — ")}</li>`).join("")}</ul>` : `<div class="dim">No transcript of last quarter's call found.</div>`}</div>
        <div class="blk"><h3>Where the money comes from<span class="hint">last filed quarter</span></h3>
        ${(x.segments || []).length ? `<table class="tbl"><thead><tr><th>Segment</th><th>Revenue</th><th>Share</th><th>Y/Y</th></tr></thead><tbody>
          ${x.segments.map(s => `<tr><td class="l">${esc(s.label)}</td><td>${big(s.q, c)}</td><td>${s.share != null ? s.share + "%" : "–"}</td><td>${pcc(s.yoy)}</td></tr>`).join("")}</tbody></table>
          <div class="src">From the XBRL of the ${esc(x.segments[0].end || "")} filing. The segment growing fastest is usually the one the stock trades on.</div>` : `<div class="dim">No segment data (non-US filer, or one segment).</div>`}</div></div>`;
    }
  } else h += fail(ex.reason || ex.value?.error);
  $("#ovExp").innerHTML = h;
}

/* The holdings on either side of the reporter: what its numbers say about
   positions other than itself. */
function chainBlock(ev) {
  const c = ev.chain;
  if (!c || !(c.suppliers.length + c.customers.length + c.competitors.length)) return "";
  const row = r => `<a href="/m/SPLC/${encodeURIComponent(r.sym)}"><b>${esc(r.label)}</b></a> <span class="dim">${r.weight.toFixed(1)}%</span>${r.share ? ` <span class="warnc">${esc(r.share)}</span>` : ""}`;
  const col = (title, rows, hint) => rows.length ? `<div><div class="k">${title}</div><div class="t" style="line-height:1.6">${rows.map(row).join("<br>")}</div><div class="t dim">${hint}</div></div>` : "";
  return `<div class="blk"><h3>Read-through<span class="hint">your holdings on either side of ${esc(ev.short || ev.symbol)} · <a href="/m/SPLC/${encodeURIComponent(ev.symbol)}">supply chain ›</a></span></h3>
    <div class="kv">
      ${col("Buy from it", c.customers, "its sales are their spending: strong orders here are demand from them")}
      ${col("Sell to it", c.suppliers, "its spending is their revenue: listen for what it says it will buy")}
      ${col("Compete with it", c.competitors, "share won here is share lost there")}
    </div>
    <div class="src">From the companies' own filings (SPLC). ${c.weight ? `${c.weight.toFixed(1)}% of your book trades with it.` : ""}</div></div>`;
}

function renderPlaybook(pb) {
  let h = "";
  if (pb.status === "fulfilled" && !pb.value.error) {
    const p = pb.value, st = p.stats || {}, e = p.eur;
    h += `<div class="blk"><h3>What usually happens<span class="hint">${st.n ? `the last ${st.n} reports` : ""}</span></h3>`;
    if (st.n) {
      h += `<div class="kv">
        <div><div class="k">Beat rate</div><div class="v">${st.beat_rate}%</div><div class="t">average EPS surprise ${pc(st.avg_surprise)}</div></div>
        <div><div class="k">Average move</div><div class="v">±${st.avg_abs_move.toFixed(1)}%</div><div class="t">largest ${pc(st.max_up)} / ${pc(st.max_down)}</div></div>
        ${p.implied?.move ? `<div><div class="k">Options price now</div><div class="v">±${p.implied.move.toFixed(1)}%</div><div class="t">${(p.implied.move / st.avg_abs_move).toFixed(2)}× the average actual move</div></div>` : ""}
        ${e ? `<div><div class="k">On your position</div><div class="v">${e.implied != null ? "±" + eur(e.implied) : "±" + eur(e.avg)}</div>
          <div class="t">${e.implied != null ? "at the implied move" : "at the average move"} · worst ${eurS(e.worst)}</div></div>` : ""}
      </div>
      <ul class="say" style="margin-top:.6rem">${(p.say || []).map(s => `<li>${esc(s)}</li>`).join("")}</ul>
      <div class="tscroll" style="margin-top:.6rem"><table class="tbl"><thead><tr><th>If it…</th><th>Times</th><th>Median move</th><th>Average</th><th>Up</th><th>5 days on</th>${e ? "<th>On your position</th>" : ""}</tr></thead><tbody>
        ${[["beats by 5%+", st.big_beat], ["beats by less", st.small_beat], ["misses", st.miss], [`ran up ${st.hot_threshold}%+ into it`, st.hot]].map(([l, g]) =>
          `<tr><td>${l}</td><td>${g?.n || 0}</td><td>${g?.n ? pcc(g.median) : "–"}</td><td>${g?.n ? pcc(g.avg) : "–"}</td><td>${g?.n ? `${g.up}/${g.n}` : "–"}</td>
          <td>${g?.d5 != null ? pcc(g.d5) : "–"}</td>${e ? `<td class="${sgn(g?.median)}">${g?.n ? eurS(Math.round(e.value * g.median / 100)) : "–"}</td>` : ""}</tr>`).join("")}
      </tbody></table></div>
      <div id="pbChart" class="lc" style="margin-top:.7rem"></div>
      <div class="legend"><span><span class="sw" style="background:var(--gain)"></span>up on the day</span><span><span class="sw" style="background:var(--loss)"></span>down</span>${p.implied?.move ? `<span><span class="ln"></span>± options-implied move now</span>` : ""}<span>▲ beat · ▼ miss (EPS)</span></div>
      <div class="src">Moves are the first session after the report, close to close (dividend-adjusted). History from Yahoo's record of estimates and reported EPS.</div>`;
    } else h += `<div class="dim">No reaction history for this symbol.</div>`;
    h += `</div>`;
    $("#ovPb").innerHTML = h;
    if (st.n) barChart($("#pbChart"), p.history.filter(r => r.move != null), p.implied?.move);
  } else { $("#ovPb").innerHTML = fail(pb.reason || pb.value?.error); }
}

/* ---------- Live --------------------------------------------------------- */
async function renderLive() {
  const ev = S.ev.meta;
  let w = null;
  try { w = (await api(`/earnings/live/${encodeURIComponent(ev.symbol)}?d=${ev.date}`)).watch; } catch { }
  if (!still("live")) return;
  const perm = window.Notification ? Notification.permission : "unsupported";
  const notif = perm === "granted" ? `<span class="bdg good">notifications on</span>` :
    perm === "denied" ? `<span class="bdg warn" title="Allow notifications for this site in the browser's settings">notifications blocked</span>` :
    perm === "unsupported" ? "" : `<button class="seg" id="notif">Notify me when numbers land</button>`;
  let h = "";
  if (!w) {
    const soon = Math.abs(ev.days) <= 1;
    h = `<div class="blk"><h3>The earnings moment</h3>
      <p class="prose">${ev.reported ? "This report is out; the numbers are under Results." :
        `${esc(ev.short || ev.symbol)} reports ${ev.days === 0 ? "today" : `on ${dshort(ev.date)}`} ${esc(ev.when_label)}${ev.time_local ? `, around ${ev.time_local} your time` : ""}.
        ${ev.position || ev.watch ? " Because you hold or watch it, a watch starts on its own fifteen minutes before." : " Start a watch to have the numbers read the moment they are filed."}`}</p>
      <div class="fbar">${!ev.reported || soon ? `<button class="seg go" id="liveGo">Watch live now</button>` : ""} ${notif}</div>
      <div class="src">A watch asks EDGAR every 10 seconds, the newswire every 30 and the price every 15, for four hours.</div></div>`;
  } else {
    const n = w.numbers || {}, c = cs(n.currency || "USD");
    const edgar = w.found ? `${esc(w.found.form)} accepted ${esc((w.found.accepted || "").slice(11, 19))} UTC` : `waiting — ${w.checks} checks, last ${ago(w.age_s)}`;
    h = `<div class="blk"><h3>${w.found ? "The numbers are in" : "Watching"} <span class="pulse ${w.found ? "ok" : ""}"></span>
        <span class="hint">started ${new Date(w.started * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}${w.manual ? " by you" : ""}</span></h3>
      <div class="lv">
        <div><div class="k">EDGAR</div><div class="v ${w.found ? "pos" : ""}">${w.found ? "Filed" : "Not yet"}</div><div class="t">${edgar}${w.found?.latency_s != null && w.found.latency_s < 3600 ? ` · seen ${w.found.latency_s}s after the SEC accepted it` : ""}</div></div>
        <div><div class="k">The wire</div><div class="v">${w.wire ? "Out" : "Not yet"}</div><div class="t">${w.wire ? `<a href="${esc(w.wire.url || "#")}" target="_blank" rel="noopener">${esc(w.wire.title)}</a> · ${esc(w.wire.provider || "")}` : "Yahoo's news for the symbol, every 30s"}</div></div>
        <div><div class="k">The stock</div><div class="v">${w.price?.move != null ? pcc(w.price.move, 2) : "–"}</div><div class="t">${w.price ? `${w.price.price} against the ${w.price.ref ?? "–"} close · ${new Date(w.price.ts * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}` : "pre- and after-market, every 15s"}</div></div>
      </div></div>`;
    if (w.found) {
      h += `<div class="blk"><h3>Against the consensus<span class="hint">read from the press release in ${n.read_s ?? "–"}s</span></h3>
        <table class="tbl"><thead><tr><th></th><th>Reported</th><th>Consensus</th><th>Surprise</th></tr></thead><tbody>
        <tr><td class="l">Revenue</td><td>${big(n.revenue, c)}${n.revenue_yoy != null ? ` <span class="dim">${pc(n.revenue_yoy)} y/y</span>` : ""}</td><td>${big(n.rev_est)}</td><td>${pcc(n.rev_surprise)}</td></tr>
        <tr><td class="l">EPS <span class="dim">${esc(n.eps_basis || "")}</span></td><td>${px(n.eps, c)}</td><td>${px(n.eps_est)}</td><td>${pcc(n.eps_surprise)}</td></tr>
        ${n.eps_gaap != null && n.eps_adj != null ? `<tr><td class="l dim">GAAP / adjusted</td><td>${px(n.eps_gaap, c)} / ${px(n.eps_adj, c)}</td><td></td><td></td></tr>` : ""}
        </tbody></table>
        ${(n.notes || []).map(x => `<div class="src">${esc(x)}</div>`).join("")}
        ${(n.highlights || []).length ? `<ul class="say" style="margin-top:.5rem">${n.highlights.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}
        ${(n.guidance || []).length ? `<h3 style="margin-top:.7rem">Guidance in the release</h3><ul class="say">${n.guidance.map(g => `<li>${esc(g.text)}</li>`).join("")}</ul>` : ""}
        <div class="src"><a href="${esc(n.release_url || "#")}" target="_blank" rel="noopener">the press release</a> · <a href="${esc(n.index || "#")}" target="_blank" rel="noopener">the filing</a> · parsed by fixed rules: check before acting</div></div>`;
    } else if (w.error) h += `<div class="note">${esc(w.error)}</div>`;
    h += `<div class="fbar"><button class="seg" id="liveStop">Stop watching</button> ${notif}</div>
      <div class="blk"><h3>Log</h3><div class="logl">${(w.log || []).slice().reverse().map(l => `<div>${new Date(l.t * 1000).toLocaleTimeString("en-GB")} · ${esc(l.msg)}</div>`).join("")}</div></div>`;
  }
  body().innerHTML = h;
  $("#liveGo") && ($("#liveGo").onclick = async () => {
    try { await post(`/earnings/live/${encodeURIComponent(ev.symbol)}?d=${ev.date}`); pollLive(); renderLive(); } catch (e) { flash(esc(e.message)); }
  });
  $("#liveStop") && ($("#liveStop").onclick = async () => { await del(`/earnings/live/${encodeURIComponent(ev.symbol)}?d=${ev.date}`); pollLive(); renderLive(); });
  $("#notif") && ($("#notif").onclick = async () => { try { await Notification.requestPermission(); } catch { } renderLive(); });
  if (w && w.active && !S.poll.live) S.poll.live = setInterval(() => { if (still("live")) renderLive(); }, 3000);
  if ((!w || !w.active) && S.poll.live) { clearInterval(S.poll.live); delete S.poll.live; }
  if (w && w.found && !ev.reported) { ev.reported = true; }
}

/* ---------- Results ------------------------------------------------------- */
async function renderResults() {
  let r;
  try { r = await refresh("results"); } catch (e) { body().innerHTML = fail(e); return; }
  if (!still("results")) return;
  if (r.error) { body().innerHTML = fail(r.error); return; }
  if (!r.reported) { body().innerHTML = `<div class="note plain"><b>Not reported yet</b>${esc(r.why)} The Live tab reads the numbers the moment they are filed.</div>`; return; }
  const ev = S.ev.meta, c = cs(r.release?.currency || ev.currency), k = r.consensus || {};
  const row = (lab, x, fmt, src, extra = "") => {
    if (!x) return `<tr><td class="l">${lab}</td><td>–</td><td>–</td><td>–</td><td></td><td class="dim l">${src || "not found"}</td></tr>`;
    const rng = x.range ? (() => {
      const [lo, hi] = x.range, span = hi - lo || 1, pos = v => Math.max(0, Math.min(100, (v - lo) / span * 100));
      return `<span class="rng" title="analysts' range ${fmt(lo)} – ${fmt(hi)}"><b style="left:${pos(x.est)}%"></b><i style="left:${pos(x.actual)}%"></i></span>${x.outside ? ` <span class="bdg ${x.outside === "above" ? "good" : "bad"}">${x.outside} range</span>` : ""}`;
    })() : "";
    return `<tr><td class="l">${lab}</td><td><b>${fmt(x.actual)}</b>${extra}</td><td>${fmt(x.est)}</td><td>${pcc(x.surprise)}</td><td class="l">${rng}</td><td class="dim l">${esc(src || "")}</td></tr>`;
  };
  const rx = r.reaction || {};
  let h = `<div class="blk"><h3>Against the consensus<span class="hint">consensus: ${esc(k.src || "")}</span></h3>
    <div class="tscroll"><table class="tbl"><thead><tr><th></th><th>Reported</th><th>Consensus</th><th>Surprise</th><th class="l">In the range</th><th class="l">Source</th></tr></thead><tbody>
      ${row("Revenue", r.revenue, v => big(v, c), r.revenue_source, r.revenue_yoy != null ? ` <span class="dim">${pc(r.revenue_yoy)} y/y</span>` : "")}
      ${row("EPS", r.eps, v => px(v, c), r.eps_source)}
      ${r.eps_release?.eps_gaap || r.eps_release?.eps_adj ? `<tr><td class="l dim">in the release</td><td colspan="5" class="l dim">GAAP ${px(r.eps_release.eps_gaap?.value, c)} · adjusted ${px(r.eps_release.eps_adj?.value, c)}</td></tr>` : ""}
      ${r.release?.op_income ? `<tr><td class="l">Operating income</td><td>${big(r.release.op_income.value, c)}</td><td>–</td><td>–</td><td></td><td class="dim l">press release${r.release.op_income.year_ago ? ` · ${pc((r.release.op_income.value / r.release.op_income.year_ago - 1) * 100)} y/y` : ""}</td></tr>` : ""}
      ${r.release?.net_income ? `<tr><td class="l">Net income</td><td>${big(r.release.net_income.value, c)}</td><td>–</td><td>–</td><td></td><td class="dim l">press release${r.release.net_income.year_ago ? ` · ${pc((r.release.net_income.value / r.release.net_income.year_ago - 1) * 100)} y/y` : ""}</td></tr>` : ""}
    </tbody></table></div>
    <div class="legend"><span><span class="ln" style="border-top:2px solid var(--warn)"></span>consensus</span><span><span class="sw" style="background:var(--ink);width:3px"></span>reported, within the analysts' range</span></div>
    ${(r.release?.notes || []).map(x => `<div class="src">${esc(x)}</div>`).join("")}</div>`;
  h += `<div class="two"><div class="blk"><h3>The reaction</h3>
    <div class="kv"><div><div class="k">${esc(rx.phase || "–")}${rx.day ? " · " + dm(rx.day) : ""}</div><div class="v">${pcc(rx.move, 2)}</div>
      <div class="t">${rx.gap != null ? `opened ${pc(rx.gap)}, ${pc(rx.intraday)} open to close` : ""}${rx.mkt != null ? ` · S&P 500 ${pc(rx.mkt)}` : ""}</div></div>
      ${rx.vs_implied != null ? `<div><div class="k">Against options</div><div class="v">${rx.vs_implied.toFixed(2)}×</div><div class="t">the implied ±${(k.implied?.move || 0).toFixed(1)}%</div></div>` : ""}
      ${rx.eur != null ? `<div><div class="k">Your position</div><div class="v ${sgn(rx.eur)}">${eurS(rx.eur)}</div><div class="t">on the day</div></div>` : ""}
      ${rx.d5 != null ? `<div><div class="k">Five sessions on</div><div class="v">${pcc(rx.d5)}</div><div class="t">since then ${pc(rx.since)}</div></div>` : ""}
    </div></div>
    <div class="blk"><h3>Analysts since<span class="hint">${(r.analysts_after || []).length} actions</span></h3>
    ${(r.analysts_after || []).length ? `<table class="tbl"><tbody>${r.analysts_after.slice(0, 12).map(a => `<tr><td>${dm(a.date)}</td><td class="l">${esc(a.firm || "")}</td>
      <td class="l">${a.action === "up" ? '<span class="pos">upgrade</span>' : a.action === "down" ? '<span class="neg">downgrade</span>' : esc(ACTION[a.action] || a.action || "")} ${esc(a.to || "")}</td>
      <td>${a.pt != null ? (a.pt_prior != null ? `${a.pt_prior} → ` : "") + `<span class="${a.pt_prior != null ? sgn(a.pt - a.pt_prior) : ""}">${a.pt}</span>` : ""}</td></tr>`).join("")}</tbody></table>` : `<div class="dim">None recorded yet.</div>`}</div></div>`;
  if ((r.release?.highlights || []).length || (r.guidance || []).length) {
    h += `<div class="two"><div class="blk"><h3>The release, in its own words</h3><ul class="say">${(r.release.highlights || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul>
      <div class="src">${r.release?.doc ? `<a href="${esc(r.release.doc.url)}" target="_blank" rel="noopener">press release</a>` : ""}${r.release?.filing ? ` · <a href="${esc(r.release.filing.index)}" target="_blank" rel="noopener">${esc(r.release.filing.form)} filed ${esc((r.release.filing.accepted || "").replace("T", " ").slice(0, 16))} UTC</a>` : ""}</div></div>
      <div class="blk"><h3>Guidance</h3>${(r.guidance || []).length ? `<ul class="say">${r.guidance.slice(0, 12).map(g => `<li>${esc(g.text)}</li>`).join("")}</ul>` : `<div class="dim">No forward-looking figures found in the release.</div>`}</div></div>`;
  } else if (!r.release?.ok) h += `<div class="note plain"><b>No press release read</b>${esc(r.release?.why || "")}</div>`;
  if (r.filed) {
    const f = r.filed;
    h += `<div class="blk"><h3>The filed quarter<span class="hint">XBRL, quarter to ${esc(f.end)}${f.filed ? `, filed ${esc(f.filed)}` : ""}</span></h3>
      <div class="kv"><div><div class="k">Revenue</div><div class="v">${big(f.revenue, c)}</div><div class="t">${pc(f.revenue_yoy)} y/y</div></div>
      <div><div class="k">Gross margin</div><div class="v">${f.gross_margin != null ? f.gross_margin.toFixed(1) + "%" : "–"}</div><div class="t"></div></div>
      <div><div class="k">Operating margin</div><div class="v">${f.op_margin != null ? f.op_margin.toFixed(1) + "%" : "–"}</div><div class="t">operating income ${big(f.op_income, c)}</div></div>
      <div><div class="k">EPS (GAAP)</div><div class="v">${px(f.eps, c)}</div><div class="t">${pc(f.eps_yoy)} y/y</div></div>
      ${f.fcf != null ? `<div><div class="k">Free cash flow</div><div class="v">${big(f.fcf, c)}</div><div class="t">capex ${big(f.capex, c)}</div></div>` : ""}
      ${f.rpo != null ? `<div><div class="k">Backlog (RPO)</div><div class="v">${big(f.rpo, c)}</div><div class="t">remaining performance obligations</div></div>` : ""}</div></div>`;
  }
  body().innerHTML = h;
}

/* ---------- Report -------------------------------------------------------- */
function mdInline(s) {
  return s.replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\[(PR|Slides|10-Q|10-K|Call|Data|Thesis|Other|6-K|8-K)\]/g, '<span class="tag">$1</span>');
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

/* Claude's section and the free write-up load separately: the write-up is
   built from every other part and can take a while the first time, and the
   Claude section re-draws every two seconds while a report is being written. */
async function renderReport() {
  const ev = S.ev.meta, ev0 = S.ev;
  if (!$("#rpClaude")) {
    body().innerHTML = `<div id="rpClaude">${loading}</div><div id="rpFree">${loading}</div>`;
    part("analysis").then(v => ({ status: "fulfilled", value: v }), e => ({ status: "rejected", reason: e }))
      .then(an => { if (still("report") && S.ev === ev0 && $("#rpFree")) renderFree(an); });
  }
  const rp = await refresh("report").then(v => ({ status: "fulfilled", value: v }), e => ({ status: "rejected", reason: e }));
  if (!still("report") || S.ev !== ev0) return;
  let h = "";
  const r = rp.status === "fulfilled" ? rp.value : null;
  if (!r) h += fail(rp.reason);
  // Claude first when there is one; the free write-up either way.
  if (r) {
    const job = r.job, latest = (r.reports || []).slice(-1)[0];
    const cc = r.writer === "claude-code", st = r.cc || {};
    const hint = cc ? `Claude Code · ${esc(r.cc_model_label)} · on your Claude plan, no charge per report`
                    : `${esc(r.model_label)} · $${r.price.input}/$${r.price.output} per million tokens in/out`;
    h += `<div class="blk"><h3>Claude's report<span class="hint">${hint}</span></h3>`;
    if (cc && !st.found) {
      h += `<div class="note plain"><b>Claude Code was not found</b>The Claude desktop app installs it; with the app installed
        it is picked up on its own. Or switch the writer to an API key under Settings on the <a href="/">home screen</a>.</div>`;
    } else if (cc && !st.logged_in) {
      h += `<div class="note plain"><b>One-time sign-in</b>Claude Code writes these reports on your Claude subscription — no charge per
        report; each counts toward your plan's usage like a long chat. It has to be signed in once: press “Sign in”, finish in the
        browser page that opens (claude.ai), then press “Check again”.
        <div class="fbar" style="margin-top:.5rem"><button class="seg go" id="ccLogin">Sign in</button><button class="seg" id="ccCheck">Check again</button>
        <span class="dim" id="ccMsg" style="font-size:.64rem"></span></div>
        <div class="src">Or in a terminal: <code>"${esc(st.path || "claude")}" auth login --claudeai</code></div></div>`;
    } else if (!cc && !r.has_key) {
      h += `<div class="note plain"><b>Needs an Anthropic API key</b>Add one under Settings on the <a href="/">home screen</a> (create it at console.anthropic.com),
        or switch the writer back to Claude Code, which uses your Claude subscription instead of billing each report.</div>`;
    } else if (!ev.reported) {
      h += `<div class="dim">Available once ${esc(ev.short || ev.symbol)} has reported${ev.days >= 0 ? ` (${dshort(ev.date)})` : ""}. Best after the call, when the transcript is in.</div>`;
    } else {
      const running = job && job.state === "running";
      const missingDefault = !latest;
      h += `<div class="fbar">
        <button class="seg go" id="rpGo" ${running ? "disabled" : ""}>${latest ? "Write it again" : "Write the report"}</button>
        <label class="dim" style="font-size:.66rem"><input type="checkbox" id="rpWeb" ${missingDefault ? "checked" : ""}> let Claude look on the company's site for missing slides or transcript</label>
        </div>`;
      if (job && (running || job.state === "error")) {
        h += job.state === "error" ? `<div class="note"><b>The report was not written</b>${esc(job.error)}</div>` :
          `<div class="dim" style="font-size:.68rem">${esc(job.question ? "Answering" : job.phase)} · ${job.elapsed_s}s${job.have ? ` · reading ${esc(job.have.join(", "))}` : ""}</div>
           ${job.text ? `<div class="writing md">${md(job.text)}</div>` : ""}`;
      }
      if (latest) {
        const u = latest.usage || {}, sr = latest.sources || {};
        h += `<div class="md">${md(latest.body)}</div>
          <div class="src">${esc(latest.model)} · ${esc(latest.created.replace("T", " ").slice(0, 16))} UTC · ${((u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0)).toLocaleString()} tokens read, ${(u.output_tokens || 0).toLocaleString()} written
          ${latest.cost_usd != null ? ` · <b>$${latest.cost_usd.toFixed(2)}</b>` : sr.writer === "claude-code" ? ` · <b>no charge</b>, on your Claude plan${u.api_equivalent_usd != null ? ` (≈$${u.api_equivalent_usd.toFixed(2)} at API prices)` : ""}` : ""} · had: ${esc((sr.have || []).join(", ") || "–")}${(sr.missing || []).length ? ` · missing: ${esc(sr.missing.join(", "))}` : ""}${sr.web ? " · web search on" : ""}
          ${(r.reports || []).length > 1 ? ` · ${(r.reports.length - 1)} earlier version${r.reports.length > 2 ? "s" : ""}` : ""} · <a href="#" data-del="${latest.id}">delete</a></div>`;
        h += `<div class="blk" style="margin-top:.8rem"><h3>Ask about it</h3>
          <textarea class="in" id="askQ" rows="2" placeholder="e.g. How much of the margin beat was the depreciation change? What did they say about pricing in Europe?"></textarea>
          <div class="fbar" style="margin-top:.3rem"><button class="seg go" id="askGo" ${running ? "disabled" : ""}>Ask</button><span class="dim" style="font-size:.64rem">${(latest.sources || {}).writer === "claude-code" ? "continues the report's own conversation, so the documents are not sent again" : "re-uses the same documents from the prompt cache within five minutes — a few cents"}</span></div>
          ${(r.answers || []).slice().reverse().map(a => `<div class="qa"><div class="q">Q · ${esc(a.question)}</div><div class="md">${md(a.body)}</div>
            <div class="src">${esc(a.created.replace("T", " ").slice(0, 16))} UTC${a.cost_usd != null ? ` · $${a.cost_usd.toFixed(3)}` : (a.sources || {}).writer === "claude-code" ? " · no charge" : ""} · <a href="#" data-del="${a.id}">delete</a></div></div>`).join("")}</div>`;
      }
      if (running && !S.poll.report) S.poll.report = setInterval(() => { if (still("report")) renderReport(); else { clearInterval(S.poll.report); delete S.poll.report; } }, 2000);
      if (!running && S.poll.report) { clearInterval(S.poll.report); delete S.poll.report; }
    }
    h += `</div>`;
  }
  $("#rpClaude").innerHTML = h;
  wireReport(ev);
}

function renderFree(an) {
  let h = "";
  if (an.status === "fulfilled" && an.value.ok) {
    const a = an.value;
    let sec = null;
    h += `<div class="blk"><h3>The free write-up<span class="hint">rules applied to the figures on this page</span></h3>`;
    for (const l of a.lines) {
      if (l.section !== sec) { sec = l.section; h += `<div class="alh">${esc(sec)}</div>`; }
      h += `<div class="al ${l.tone}"><i>${l.tone === "good" ? "▲" : l.tone === "bad" ? "▼" : l.tone === "warn" ? "!" : "·"}</i><span>${esc(l.text)}</span></div>`;
    }
    h += `<div class="src">${esc(a.note)}</div></div>`;
  } else if (an.status === "fulfilled") {
    h += `<div class="blk"><h3>The free write-up</h3><div class="dim">${esc(an.value.why || an.value.error || "")}</div></div>`;
  } else h += fail(an.reason);
  $("#rpFree").innerHTML = h;
}

function wireReport(ev) {
  $("#ccLogin") && ($("#ccLogin").onclick = async () => {
    try { await post("/earnings/claude-code/login"); $("#ccMsg").textContent = "a sign-in window opened — finish it in the browser, then press Check again"; }
    catch (e) { $("#ccMsg").textContent = e.message; }
  });
  $("#ccCheck") && ($("#ccCheck").onclick = async () => {
    $("#ccMsg").textContent = "checking…";
    try { const st = await api("/earnings/claude-code"); if (st.logged_in) renderReport(); else $("#ccMsg").textContent = "not signed in yet"; }
    catch (e) { $("#ccMsg").textContent = e.message; }
  });
  $("#rpGo") && ($("#rpGo").onclick = async () => {
    try { await post(`/earnings/report/${encodeURIComponent(ev.symbol)}?d=${ev.date}`, { web: $("#rpWeb").checked }); renderReport(); }
    catch (e) { flash(esc(e.message)); }
  });
  $("#askGo") && ($("#askGo").onclick = async () => {
    const q = $("#askQ").value.trim();
    if (!q) return;
    try { await post(`/earnings/report/${encodeURIComponent(ev.symbol)}?d=${ev.date}`, { question: q }); renderReport(); }
    catch (e) { flash(esc(e.message)); }
  });
  $$("#rpClaude [data-del]").forEach(a => a.onclick = async e => {
    e.preventDefault();
    if (!confirm("Delete this? It cannot be undone, and writing it again takes another run.")) return;
    await del(`/earnings/report/${a.dataset.del}`); renderReport();
  });
}

/* ---------- Call ---------------------------------------------------------- */
async function renderCall() {
  let r;
  try { r = await part("call"); } catch (e) { body().innerHTML = fail(e); return; }
  if (!still("call")) return;
  if (!r.ok) { body().innerHTML = `<div class="note plain"><b>No transcript yet</b>${esc(r.why || r.error || "")} <a href="#" id="toSrc">Add one under Sources</a>.</div>`;
    $("#toSrc") && ($("#toSrc").onclick = e => { e.preventDefault(); selectTab("sources"); }); return; }
  const a = r.analytics, vp = a.vs_prev;
  const toneRow = (lab, t) => {
    if (!t || t.net == null) return "";
    const w = Math.abs(t.net) * 50;
    return `<div class="tone"><span>${lab}</span><span class="tr"><span class="mid"></span><i class="${t.net >= 0 ? "pos" : "neg"}" style="${t.net >= 0 ? "left:50%" : `left:${50 - w}%`};width:${w}%"></i></span><span>${t.net >= 0 ? "+" : ""}${t.net.toFixed(2)}</span></div>`;
  };
  let h = `<div class="two"><div class="blk"><h3>Tone<span class="hint">positive minus negative words, over both</span></h3>
      ${toneRow("Script, now", a.tone.prepared)}${toneRow("Answers, now", a.tone.answers)}
      ${vp ? toneRow("Script, last qtr", vp.tone_prepared) + toneRow("Answers, last qtr", vp.tone_answers) : ""}
      <div class="src">Uncertainty words per 1,000: script ${a.tone.prepared.unc}${vp ? ` (last quarter ${vp.tone_prepared.unc})` : ""}, answers ${a.tone.answers.unc}.
      ${a.evasive ? `<span class="warnc">${a.evasive} answer${a.evasive > 1 ? "s" : ""} declined the detail asked for.</span>` : ""}</div></div>
    <div class="blk"><h3>What it was about</h3><div class="chips">${a.topics.slice(0, 16).map(([p, n]) => `<span class="chip" title="${n} mentions">${esc(p)}</span>`).join("")}</div>
      ${vp ? `<div class="alh">New this quarter</div><div class="chips">${vp.new_topics.map(p => `<span class="chip new">${esc(p)}</span>`).join("") || '<span class="dim">–</span>'}</div>
      <div class="alh">Gone since last quarter</div><div class="chips">${vp.dropped_topics.map(p => `<span class="chip gone">${esc(p)}</span>`).join("") || '<span class="dim">–</span>'}</div>` : ""}</div></div>`;
  if ((r.takeaways || []).length) h += `<div class="blk"><h3>Takeaways<span class="hint">${esc(r.source || "")}</span></h3><ul class="say">${r.takeaways.map(t => `<li>${esc(t).replace(/^([^—–-]{3,60}?)\s+[-–—]{1,2}\s+/, "<b>$1</b> — ")}</li>`).join("")}</ul></div>`;
  if ((r.risks || []).length) h += `<div class="blk"><h3>Risks named</h3><ul class="say">${r.risks.map(t => `<li>${esc(t)}</li>`).join("")}</ul></div>`;
  h += `<div class="blk"><h3>What analysts asked<span class="hint">${a.questions.length} questions from ${a.analysts} analysts</span></h3>
    ${a.questions.map(q => `<div class="qq"><div class="who"><b>${esc(q.analyst)}</b>${q.firm ? `<span>${esc(q.firm)}</span>` : ""}
      <span>answered by ${esc(q.answered_by.join(", ") || "–")} · ${q.answer_words} words</span>${q.evasive ? '<span class="bdg warn">evasive</span>' : ""}</div>
      <div class="qt">${esc(q.question)}</div></div>`).join("") || `<div class="dim">No Q&A found in the transcript.</div>`}</div>`;
  if ((a.guidance || []).length) h += `<div class="blk"><h3>Outlook, as said on the call</h3><ul class="say">${a.guidance.map(g => `<li><b>${esc(g.speaker)}:</b> ${esc(g.text)}</li>`).join("")}</ul></div>`;
  if ((a.quotes || []).length) h += `<div class="blk"><h3>Lines worth reading</h3><ul class="say">${a.quotes.map(g => `<li><b>${esc(g.speaker)}:</b> “${esc(g.text)}”</li>`).join("")}</ul></div>`;
  const speakers = [...new Set(r.turns.map(t => t.speaker))];
  h += `<div class="blk"><h3>The transcript<span class="hint"><a href="${esc(r.url || "#")}" target="_blank" rel="noopener">${esc(r.source || "")}</a>${r.published ? " · " + esc(r.published.slice(0, 10)) : ""}</span></h3>
    <div class="fbar"><span class="lbl">Show</span><select class="seg" id="trWho"><option value="">everyone</option><option value="!qa">prepared remarks</option><option value="qa">Q&A</option>${speakers.map(s => `<option>${esc(s)}</option>`).join("")}</select>
      <input type="text" id="trFind" placeholder="find in the call" style="width:200px"></div>
    <div id="trBody"></div></div>`;
  body().innerHTML = h;
  const draw = () => {
    const who = $("#trWho").value, f = $("#trFind").value.trim().toLowerCase();
    const ts = r.turns.filter(t => (!who || (who === "qa" ? t.section === "qa" : who === "!qa" ? t.section !== "qa" : t.speaker === who)) &&
      (!f || t.text.toLowerCase().includes(f)));
    $("#trBody").innerHTML = ts.slice(0, 400).map(t => `<div class="turn ${t.role}"><div class="sp">${esc(t.speaker)}${t.firm ? " · " + esc(t.firm) : t.title ? " · " + esc(t.title) : ""}</div>
      <div class="tx">${f ? esc(t.text).replace(new RegExp(f.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), m => `<mark>${m}</mark>`) : esc(t.text)}</div></div>`).join("") || `<div class="dim">Nothing matches.</div>`;
  };
  $("#trWho").onchange = draw; $("#trFind").oninput = draw; draw();
}

/* ---------- Sources ------------------------------------------------------- */
async function renderSources() {
  let r;
  try { r = await refresh("docs"); } catch (e) { body().innerHTML = fail(e); return; }
  if (!still("sources")) return;
  const ev = S.ev.meta;
  const st = { ok: "✓", waiting: "…", missing: "–" };
  let h = `<div class="blk"><h3>Documents<span class="hint">what the free write-up and Claude's report are built from</span></h3><div class="srcs">
    ${r.items.map(i => `<div class="si ${i.status}"><span class="k">${st[i.status] || ""} ${esc(i.kind)}</span>
      <span>${i.url ? `<a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a>` : esc(i.title)}
        ${i.when ? `<span class="dim"> · ${esc(String(i.when).replace("T", " ").slice(0, 16))}${String(i.when).includes("T") ? " UTC" : ""}</span>` : ""}
        ${i.note ? `<div class="nt">${esc(i.note)}${i.ir ? ` · <a href="${esc(i.ir)}" target="_blank" rel="noopener">company website</a>` : ""}</div>` : ""}</span>
      <span>${i.id ? `<a href="#" data-rm="${i.id}">remove</a>` : ""}</span></div>`).join("")}</div></div>
    <div class="blk"><h3>Add a source<span class="hint">a link to slides or a transcript, or paste the text</span></h3>
      <div class="frm">
        <label for="asKind">What</label><select class="in" id="asKind"><option value="slides">Slides / presentation</option><option value="transcript">Call transcript</option><option value="release">Press release / letter</option><option value="other">Other</option></select>
        <label for="asUrl">Link</label><input class="in" id="asUrl" placeholder="https://… (a PDF from the IR site is read, charts and all, by Claude)">
        <label for="asBody">or paste</label><textarea class="in" id="asBody" rows="4" placeholder="Paste the text. For a transcript, lines like 'Name: what they said' are read as turns."></textarea>
        <label for="asTitle">Title</label><input class="in" id="asTitle" placeholder="optional">
        <span></span><div><button class="seg go" id="asGo">Add</button> <span class="dim" id="asMsg" style="font-size:.66rem"></span></div>
      </div></div>`;
  body().innerHTML = h;
  $("#asGo").onclick = async () => {
    const b = { kind: $("#asKind").value, url: $("#asUrl").value.trim() || null, body: $("#asBody").value.trim() || null, title: $("#asTitle").value.trim() || null };
    $("#asMsg").textContent = b.url && !b.body ? "reading the link…" : "saving…";
    try {
      const x = await post(`/earnings/sources/${encodeURIComponent(ev.symbol)}?d=${ev.date}`, b);
      $("#asMsg").textContent = `added — ${x.words.toLocaleString()} words read`;
      for (const k of ["docs", "call", "analysis", "results"]) delete S.ev.parts[k];
      setTimeout(renderSources, 600);
    } catch (e) { $("#asMsg").textContent = e.message; }
  };
  $$("#dwBody [data-rm]").forEach(a => a.onclick = async e => {
    e.preventDefault();
    await del(`/earnings/sources/${a.dataset.rm}?sym=${encodeURIComponent(ev.symbol)}&d=${ev.date}`);
    for (const k of ["docs", "call", "analysis", "results"]) delete S.ev.parts[k];
    renderSources();
  });
}

/* ---------- History ------------------------------------------------------- */
async function renderHistory() {
  let p;
  try { p = await part("playbook"); } catch (e) { body().innerHTML = fail(e); return; }
  if (!still("history")) return;
  const rows = (p.history || []).slice().reverse();
  const c = cs(S.ev.meta.currency);
  body().innerHTML = `<div class="blk"><h3>Every report since ${rows.length ? mon(rows[rows.length - 1].date) : "–"}<span class="hint">click a row to open that report</span></h3>
    <div class="tscroll"><table class="tbl"><thead><tr><th>Date</th><th>When</th><th>EPS est.</th><th>Reported</th><th>Surprise</th><th>Month before</th><th>Gap</th><th>Day</th><th>Open→close</th><th>vs S&P</th><th>5 days</th></tr></thead><tbody>
    ${rows.map(r => `<tr class="pick" data-d="${r.date}"><td>${dm(r.date)} ${r.date.slice(2, 4)}</td><td class="dim">${esc((r.when || "").replace("after close", "post").replace("before open", "pre"))}</td>
      <td>${px(r.eps_est, c)}</td><td>${px(r.eps_act, c)}</td><td>${pcc(r.surprise)}</td><td>${pcc(r.runup)}</td><td>${pcc(r.gap)}</td>
      <td><b>${pcc(r.move)}</b></td><td>${pcc(r.intraday)}</td><td>${pcc(r.excess)}</td><td>${pcc(r.d5)}</td></tr>`).join("")}</tbody></table></div>
    <div class="src">"vs S&P" is the day's move minus the S&P 500's; "month before" is the twenty sessions into the report.</div></div>
    <div class="blk"><h3>Does the size of the beat matter?<span class="hint">EPS surprise against the move on the day</span></h3><div id="scat" class="lc"></div>
    <div class="src">${p.stats?.corr_surprise_move != null ? `Correlation ${p.stats.corr_surprise_move >= 0 ? "+" : ""}${p.stats.corr_surprise_move.toFixed(2)} over ${p.stats.n} reports. ` : ""}Surprises beyond ±60% are drawn at the edge.</div></div>`;
  $$("#dwBody tr.pick").forEach(tr => tr.onclick = () => openEvent(S.ev.sym, tr.dataset.d));
  scatter($("#scat"), (p.history || []).filter(r => r.move != null && r.surprise != null));
}

/* ==========================================================================
   charts
   ========================================================================== */
function nice(lo, hi, n = 5) {
  if (!(hi > lo)) { hi = lo + 1; lo = lo - 1; }
  const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
  const step = (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * mag;
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, t = [];
  for (let v = a; v <= b + step / 2; v += step) t.push(+v.toFixed(10));
  return { lo: a, hi: b, ticks: t };
}

function tipper(host) {
  let tip = host.querySelector(".lc-tip");
  if (!tip) { tip = document.createElement("div"); tip.className = "lc-tip"; tip.style.display = "none"; host.appendChild(tip); }
  return {
    show(html, x, y) {
      tip.innerHTML = html; tip.style.display = "block";
      const w = host.clientWidth, tw = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(w - tw, x + 12)) + "px"; tip.style.top = Math.max(0, y - 10) + "px";
    },
    hide() { tip.style.display = "none"; },
  };
}

/* The move on the day after each report: one bar per report, up green and
   down red, the options-implied band now as two dashed lines. */
function barChart(host, rows, implied) {
  if (!host) return;
  const draw = () => {
    const W = Math.max(280, host.clientWidth || 600), H = W < 560 ? 190 : 230;
    const ys = rows.map(r => r.move);
    let lo = Math.min(0, ...ys), hi = Math.max(0, ...ys);
    if (implied) { lo = Math.min(lo, -implied); hi = Math.max(hi, implied); }
    const yt = nice(lo * 1.08, hi * 1.08, 4);
    const M = { l: 38, r: implied ? 64 : 10, t: 12, b: 22 }, pw = W - M.l - M.r, ph = H - M.t - M.b;
    const Y = v => M.t + (yt.hi - v) / (yt.hi - yt.lo) * ph;
    const bw = pw / rows.length, gap = Math.max(2, Math.min(8, bw * 0.25)), w = Math.max(3, bw - gap);
    let g = "";
    for (const t of yt.ticks) g += `<line class="gl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(t)}" y2="${Y(t)}"/><text class="ax" x="${M.l - 6}" y="${Y(t) + 3}" text-anchor="end">${t > 0 ? "+" : ""}${t}%</text>`;
    g += `<line class="zl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(0)}" y2="${Y(0)}"/>`;
    if (implied) for (const v of [implied, -implied])
      g += `<line class="ref" x1="${M.l}" x2="${M.l + pw}" y1="${Y(v)}" y2="${Y(v)}"/><text class="ax" x="${M.l + pw + 4}" y="${Y(v) + 3}" style="fill:var(--warn)">${v > 0 ? "+" : "−"}${Math.abs(v).toFixed(1)}%</text>`;
    const step = Math.ceil(rows.length / Math.floor(pw / 46));
    rows.forEach((r, i) => {
      const x = M.l + i * bw + gap / 2, y0 = Y(0), y1 = Y(r.move), top = Math.min(y0, y1), hgt = Math.max(1, Math.abs(y1 - y0));
      const rad = Math.min(4, w / 2, hgt);
      // Rounded at the data end, square at the baseline.
      const up = r.move >= 0;
      const path = up
        ? `M${x},${y0} V${top + rad} Q${x},${top} ${x + rad},${top} H${x + w - rad} Q${x + w},${top} ${x + w},${top + rad} V${y0} Z`
        : `M${x},${y0} V${top + hgt - rad} Q${x},${top + hgt} ${x + rad},${top + hgt} H${x + w - rad} Q${x + w},${top + hgt} ${x + w},${top + hgt - rad} V${y0} Z`;
      g += `<path class="bar" d="${path}" fill="${up ? "var(--gain)" : "var(--loss)"}"/>`;
      if (r.surprise != null) g += `<text class="ax" x="${x + w / 2}" y="${up ? top - 3 : top + hgt + 10}" text-anchor="middle" style="font-size:8px;fill:var(--ink-mid)">${r.surprise >= 0 ? "▲" : "▼"}</text>`;
      if (i % step === 0) g += `<text class="ax" x="${x + w / 2}" y="${H - 6}" text-anchor="middle">${mon(r.date)}</text>`;
      g += `<rect class="hit" data-i="${i}" x="${M.l + i * bw}" y="${M.t}" width="${bw}" height="${ph}"/>`;
    });
    host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Move on the day after each report">${g}</svg>`;
    const tp = tipper(host);
    host.querySelectorAll(".hit").forEach(el => {
      el.onmousemove = ev => {
        const r = rows[+el.dataset.i], b = host.getBoundingClientRect();
        tp.show(`<div class="h">${dm(r.date)} ${r.date.slice(0, 4)} · ${esc(r.when || "")}</div>
          EPS ${px(r.eps_act)} vs ${px(r.eps_est)} · ${pc(r.surprise)}<br>day ${pc(r.move)} · gap ${pc(r.gap)}${r.d5 != null ? ` · 5 days ${pc(r.d5)}` : ""}${r.excess != null ? `<br>vs S&P ${pc(r.excess)}` : ""}`,
          ev.clientX - b.left, ev.clientY - b.top);
      };
      el.onmouseleave = () => tp.hide();
      el.onclick = () => openEvent(S.ev.sym, rows[+el.dataset.i].date);
    });
  };
  draw();
  host.__draw = draw;
}

function scatter(host, rows) {
  if (!host) return;
  if (rows.length < 3) { host.innerHTML = `<div class="dim ld">Too few reports.</div>`; return; }
  const draw = () => {
    const W = Math.max(280, host.clientWidth || 600), H = W < 560 ? 220 : 260;
    const clip = v => Math.max(-60, Math.min(60, v));
    const xs = rows.map(r => clip(r.surprise)), ys = rows.map(r => r.move);
    const xt = nice(Math.min(0, ...xs), Math.max(0, ...xs), 5), yt = nice(Math.min(0, ...ys), Math.max(0, ...ys), 4);
    const M = { l: 38, r: 12, t: 10, b: 30 }, pw = W - M.l - M.r, ph = H - M.t - M.b;
    const X = v => M.l + (v - xt.lo) / (xt.hi - xt.lo) * pw, Y = v => M.t + (yt.hi - v) / (yt.hi - yt.lo) * ph;
    let g = "";
    for (const t of yt.ticks) g += `<line class="gl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(t)}" y2="${Y(t)}"/><text class="ax" x="${M.l - 6}" y="${Y(t) + 3}" text-anchor="end">${t > 0 ? "+" : ""}${t}%</text>`;
    for (const t of xt.ticks) g += `<text class="ax" x="${X(t)}" y="${M.t + ph + 13}" text-anchor="middle">${t > 0 ? "+" : ""}${t}%</text>`;
    g += `<line class="zl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(0)}" y2="${Y(0)}"/><line class="zl" x1="${X(0)}" x2="${X(0)}" y1="${M.t}" y2="${M.t + ph}"/>`;
    g += `<text class="ax" x="${M.l + pw}" y="${H - 2}" text-anchor="end">EPS surprise →</text><text class="ax" x="${M.l + 2}" y="${M.t + 8}">move on the day</text>`;
    rows.forEach((r, i) => {
      const cl = Math.abs(r.surprise) > 60;
      g += `<circle cx="${X(clip(r.surprise))}" cy="${Y(r.move)}" r="4.5" fill="var(--s1)" stroke="var(--surface)" stroke-width="2" ${cl ? 'stroke-dasharray="2 2"' : ""}/>`;
      g += `<circle class="hit" data-i="${i}" cx="${X(clip(r.surprise))}" cy="${Y(r.move)}" r="10"/>`;
    });
    host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="EPS surprise against the move on the day">${g}</svg>`;
    const tp = tipper(host);
    host.querySelectorAll(".hit").forEach(el => {
      el.onmousemove = ev => { const r = rows[+el.dataset.i], b = host.getBoundingClientRect();
        tp.show(`<div class="h">${dm(r.date)} ${r.date.slice(0, 4)}</div>surprise ${pc(r.surprise)} · day ${pc(r.move)}`, ev.clientX - b.left, ev.clientY - b.top); };
      el.onmouseleave = () => tp.hide();
    });
  };
  draw();
  host.__draw = draw;
}

let rsT;
window.addEventListener("resize", () => { clearTimeout(rsT); rsT = setTimeout(() => $$(".lc").forEach(h => h.__draw && h.__draw()), 150); });

/* ==========================================================================
   boot
   ========================================================================== */
document.addEventListener("keydown", e => { if (e.key === "Escape" && S.ev && !$("#termHelp")) closeEvent(); });
$("#scrim").onclick = () => closeEvent();
window.addEventListener("popstate", () => routeFromUrl(false));

function routeFromUrl(push) {
  const m = location.pathname.match(/^\/m\/ERN\/([^/?#]+)/i);
  const d = new URLSearchParams(location.search).get("d");
  if (m) openEvent(decodeURIComponent(m[1]), d, push);
  else if (S.ev) closeEvent(false);
}

calBar();
loadWeek(null);
loadUpcoming();
loadSeason();
pollLive();
setInterval(pollLive, 10000);
routeFromUrl(false);
