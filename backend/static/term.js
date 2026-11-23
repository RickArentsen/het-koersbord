/* ==========================================================================
   Shared terminal shell: the module registry, the command line, formatting.
   Every page includes this, so the codes and the navigation stay in one place.
   ========================================================================== */

const MODULES = [
  { code: "PORT", title: "Portfolio",        href: "/portfolio",   state: "live" },
  { code: "MKT",  title: "Market snapshot",  href: "/m/MKT",       state: "live" },
  { code: "BOND", title: "Bonds & rates",    href: "/m/MKT/bonds", state: "live" },
  { code: "OIL",  title: "Oil",              href: "/m/MKT/oil",   state: "live" },
  { code: "RRG",  title: "Rotation",         href: "/m/RRG",       state: "live" },
  { code: "OPT",  title: "Options",          href: "/m/OPT",       state: "live" },
  { code: "SPLC", title: "Supply chain",     href: "/m/SPLC",      state: "live" },
  { code: "NEWS", title: "News",             href: "/m/NEWS",      state: "live" },
  { code: "ERN",  title: "Earnings",         href: "/m/ERN",       state: "live" },
  { code: "GEO",  title: "Geopolitics",      href: "/m/GEO",       state: "live" },
  { code: "ALRT", title: "Morning brief",    href: "/m/ALRT",      state: "live" },
  { code: "WCH",  title: "Watchlist",        href: "/m/WCH",       state: "live" },
  { code: "MVO",  title: "Markowitz & forecast", href: "/m/MVO",   state: "live" },
  { code: "EUR",  title: "The euro",         href: "/m/EUR",       state: "live" },
  { code: "MAC",  title: "Macro calendar",   href: "/m/MAC",       state: "live" },
  { code: "FCF",  title: "FCF model",        href: "/m/FCF",       state: "live" },
  { code: "IDX",  title: "S&P 500 & Nasdaq 100", href: "/m/IDX",   state: "live" },
  { code: "READ", title: "Articles",          href: "/m/READ",      state: "live" },
  { code: "KAI",  title: "Koers AI",          href: "/m/KAI",       state: "live" },
  { code: "POL",  title: "Politicians' trades", href: "/m/POL",     state: "live" },
  { code: "FLOW", title: "Institutional flow", href: "/m/FLOW",     state: "live" },
  { code: "WEEK", title: "The week in review", href: "/m/WEEK",     state: "live" },
  { code: "SYS",  title: "The terminal's health", href: "/m/SYS",  state: "live" },
];

/* Codes that take a ticker, either way round: "OPT NVDA", "NVDA OPT". */
const WITH_TICKER = [
  { re: "ERN|EARN|EE",                 to: s => "/m/ERN/" + s,  help: ["ERN MSFT", "that company's report: what is expected, the numbers, the call"] },
  { re: "OPT|OMON|OVDV|SKEW|IVOL",     to: s => "/m/OPT/" + s,  help: ["OPT NVDA", "its options: the volatility surface, the range the prices imply, the chain"] },
  { re: "SPLC|CHAIN|SUPPLY",           to: s => "/m/SPLC/" + s, help: ["SPLC NVDA", "its supply chain: who sells to it, who it sells to, in the filings' words"] },
  { re: "WCH|WATCH",                   to: s => "/m/WCH#s=" + s, help: ["WCH NKE", "that stock on the watchlist: every check, and why it fell"] },
  { re: "FCF|DCF",                     to: s => "/t/" + s + "#s-fcf", help: ["FCF META", "its free cash flow model: growth, multiple, the return you want, the price for it"] },
];

const MOD = Object.fromEntries(MODULES.map(m => [m.code, m]));

/* ---------- formatting ---------------------------------------------------- */
const $  = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const nf = (n, d = 2) => n == null ? "–" :
  new Intl.NumberFormat("nl-NL", { minimumFractionDigits: d, maximumFractionDigits: d }).format(n);
/* Round BEFORE formatting, and normalise the sign of zero. -0.4 euros at no
   decimals is zero, and Intl prints it as "€ -0", which reads as a loss. */
const eur = (n, d = 0) => {
  if (n == null) return "–";
  let v = Math.round(n * 10 ** d) / 10 ** d;
  if (Object.is(v, -0) || v === 0) v = 0;
  return new Intl.NumberFormat("nl-NL", { style: "currency", currency: "EUR",
    minimumFractionDigits: d, maximumFractionDigits: d }).format(v);
};
const pct = (n, d = 2) => n == null ? "–" : (n >= 0 ? "+" : "") + n.toFixed(d) + "%";
const sgn = n => n == null ? "" : (n > 0 ? "pos" : (n < 0 ? "neg" : ""));

/* The day change and its session (pf/sessions.py). A holding's day starts again
   at its market's close; a move since then outside the regular session is
   tagged "pre" or "after", and the session that just finished is kept as
   "last session". */
const TAG_TITLE = { pre: "includes New York's pre-market: the move since the last close, before the open",
                    after: "includes after-hours trading: the move since the close" };
const dayTag = tags => (tags || []).map(t => ` <span class="dtag ${t}" title="${TAG_TITLE[t] || ""}">${t}</span>`).join("");
const dayName = d => { const t = new Date(d + "T12:00:00Z");
  return ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][t.getUTCDay()] + " " + d.slice(8, 10) + "/" + d.slice(5, 7); };
const lastLine = T => T && T.last_eur != null
  ? `last session${(T.last_dates || []).length === 1 ? " " + dayName(T.last_dates[0]) : ""} <span class="${sgn(T.last_eur)}">${eur(T.last_eur)} ${pct(T.last_pct)}</span>` : "";
// The day's low and high of the book's move, as the live feed recorded them (pf/dayrange.py).
const tsHM = ts => { if (!ts) return ""; const t = new Date(ts * 1000);
  return String(t.getHours()).padStart(2, "0") + ":" + String(t.getMinutes()).padStart(2, "0"); };
const amsToday = () => new Date().toLocaleDateString("sv-SE", { timeZone: "Europe/Amsterdam" });
const rangeLine = R => R && R.lo != null && R.d === amsToday()
  ? `today's low <span class="${sgn(R.lo)}" title="at ${tsHM(R.lo_ts)}">${eur(R.lo)}</span> · high <span class="${sgn(R.hi)}" title="at ${tsHM(R.hi_ts)}">${eur(R.hi)}</span>` : "";
const rangesTitle = L => (L || []).map(r => `${dayName(r.d)}: low ${eur(r.lo)} at ${tsHM(r.lo_ts)}, high ${eur(r.hi)} at ${tsHM(r.hi_ts)}, last ${eur(r.last)}`).join("\n");
// What a row should show: the move now, or, when its market has not traded since
// its close, the session that just finished, labelled "last".
const shownMove = r => (r.session && r.session !== "open" && Math.abs(r.day_pct || 0) < 0.01 && r.last_pct != null)
  ? { eur: r.last_eur, pct: r.last_pct, lab: "last" } : { eur: r.day_eur, pct: r.day_pct, lab: r.day_tag || null };

/* Dates are day/month/year everywhere. ISO only ever exists inside the data. */
const dmy = d => {
  const m = String(d ?? "").match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[3]}/${m[2]}/${m[1]}` : (d ? String(d) : "–");
};
const dmyTime = iso => {
  const t = new Date(iso);
  if (isNaN(t)) return String(iso ?? "");
  const p = n => String(n).padStart(2, "0");
  return `${p(t.getDate())}/${p(t.getMonth() + 1)}/${t.getFullYear()} ${p(t.getHours())}:${p(t.getMinutes())}`;
};

/* The build these pages expect. Static HTML is re-read from disk on every
   request, but routes live in the running Python — so an un-restarted server
   serves brand new pages whose own links then 404. Check, and say so. */
const REQUIRED_BUILD = 57;

async function checkBackend(){
  let h = null;
  try{ h = await (await fetch("/api/pf/health")).json(); }catch{ return; }
  const warn = html => {
    const bar = document.createElement("div");
    bar.className = "stale-bar";
    bar.innerHTML = html;
    document.body.insertBefore(bar, document.body.firstChild.nextSibling);
  };
  /* Windows refusing a library is invisible otherwise: every panel just says
     "no prices". Name the library and what it carries. */
  const blk = h.blocked || [];
  if(blk.length) warn(`<b>Windows is blocking part of Python.</b> Smart App Control would not load
    ${blk.map(b => esc(b.what)).join("; ")}. Those parts show cached data or nothing until it loads —
    <code>SETUP.md</code>, “Smart App Control”, says what to do.`);
  if((h.build ?? 0) >= REQUIRED_BUILD) return;
  warn(`<b>Restart the backend.</b> These pages are build ${REQUIRED_BUILD};
    the server answering is build ${h.build ?? "older than 6"}. Pages are read from disk,
    routes are not — so links like <code>/portfolio</code> will 404 until it restarts.`);
}

/* ---------- the shell ----------------------------------------------------- */
function termBar({ here = "", right = "" } = {}) {
  return `
  <div class="term-bar">
    <a class="term-brand" href="/" title="Home">HET <b>KOERSBORD</b></a>
    <span class="term-meta" id="termClock">—</span>
    <span class="term-hours" id="termHours" title="Exchange hours"></span>
    ${here ? `<span class="term-meta" style="color:var(--accent)">${esc(here)}</span>` : ""}
    <span class="sp"></span>
    <button class="tbtn tblur ${Blur.on ? "on" : ""}" id="blurBtn" title="Blur your euro amounts (Shift+B); percentages stay">€</button>
    <label class="cmd" title="Type a module code and press Enter. HELP lists them.">
      <span>&gt;</span>
      <input id="cmdInput" autocomplete="off" spellcheck="false" placeholder="command or ticker — PORT, HELP, MSFT">
    </label>
    ${right}
  </div>`;
}

function wireShell() {
  const clock = () => {
    const t = new Date(), p = n => String(n).padStart(2, "0");
    const el = $("#termClock");
    if (el) el.textContent =
      `${p(t.getDate())}/${p(t.getMonth() + 1)}/${t.getFullYear()} ${p(t.getHours())}:${p(t.getMinutes())}:${p(t.getSeconds())}`;
  };
  clock(); setInterval(clock, 1000);
  checkBackend();
  Hours.start();
  const bb = $("#blurBtn");
  if (bb) bb.onclick = () => Blur.toggle();

  const input = $("#cmdInput");
  if (input) {
    input.addEventListener("keydown", e => {
      if (e.key !== "Enter") return;
      const raw = input.value.trim().toUpperCase();
      if (!raw) return;
      input.value = "";
      run(raw);
    });
    // "/" focuses the command line from anywhere, the way a terminal would
    document.addEventListener("keydown", e => {
      if (e.key === "/" && document.activeElement !== input &&
          !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
        e.preventDefault(); input.focus();
      }
    });
  }
}

function run(cmd) {
  if (cmd === "HELP" || cmd === "?") return showHelp();
  if (cmd === "HOME" || cmd === "H") return location.assign("/");
  // Words people type for the bond page. BOND the ETF is still "T BOND".
  if (["BONDS", "RATES", "YIELDS", "UST", "CURVE", "GC"].includes(cmd)) cmd = "BOND";
  // Earnings: EARNINGS, EARN, EVTS; "ERN MSFT" (or "MSFT ERN", "MSFT EE") opens
  // that company's report. EE alone stays a ticker.
  if (["EARNINGS", "EARN", "EVTS"].includes(cmd)) cmd = "ERN";
  // The morning brief: ALRT, or the words people reach for.
  if (["BRIEF", "MORNING", "AM", "BRF", "DAYBREAK"].includes(cmd)) cmd = "ALRT";
  // Options, the supply chain and rotation, by the words people reach for.
  if (["OPTIONS", "OMON", "OVDV", "SKEW", "IVOL", "VOLSURFACE"].includes(cmd)) cmd = "OPT";
  if (["SUPPLY", "CHAIN", "SUPPLYCHAIN", "SUPPLIERS", "CUSTOMERS"].includes(cmd)) cmd = "SPLC";
  if (["ROTATION", "ROTATE", "RELATIVE"].includes(cmd)) cmd = "RRG";
  // Oil: the page, by the words people reach for. BRENT the ETF is "T BNO".
  if (["CRUDE", "CRUD", "BRENT", "WTI", "OPEC", "OPEC+", "PETROL", "OLIE", "TANKERS", "HORMUZ"].includes(cmd)) cmd = "OIL";
  // The watchlist and its screen. W alone is Wayfair, so it is not one of them.
  if (["WATCH", "WATCHLIST", "EQS", "SCREEN", "SCREENER", "OPPS", "DIPS", "PULLBACKS"].includes(cmd)) cmd = "WCH";
  // The forecast: what the book might be worth in 1 to 50 years. PLAN and
  // FUTURE are left alone: both are tickers somewhere.
  // FCST lives under MVO since build 32.
  if (["FCST", "FORECAST", "PROJECTION", "PROJECT", "COMPOUND", "LONGRUN", "HORIZON"].includes(cmd)) return location.assign("/m/MVO/forecast");
  // Markowitz. MVO the oil trust is "T MVO".
  if (["MARKOWITZ", "FRONTIER", "EFFICIENT", "OPTIMISE", "OPTIMIZE", "MPT", "PORTOPT", "BLACKLITTERMAN"].includes(cmd)) cmd = "MVO";
  // The free-cash-flow model: what the cash flow says a share is worth.
  if (["DCF", "FCFM", "INTRINSIC", "FAIRVALUE", "QUALTRIM"].includes(cmd)) cmd = "FCF";
  // Articles written for you, and the day's live coverage.
  if (["ARTICLES", "ARTICLE", "STORIES", "DESK", "READER", "TOP"].includes(cmd)) cmd = "READ";
  // Koers AI, the paper-trading contest. Kadant the company stays "T KAI".
  if (["KOERSAI", "KOERS AI", "RIVAL", "CONTEST"].includes(cmd)) cmd = "KAI";
  // Congress's own trades, from the periodic transaction reports.
  if (["CONGRESS", "CONG", "POLITICIANS", "POLITICIAN", "SENATE", "STOCKACT", "PTR"].includes(cmd)) cmd = "POL";
  // What institutions are doing: up and down volume, hedge funds' futures and leverage, dark pools.
  if (["FLOWS", "INSTITUTIONAL", "INSTITUTIONS", "HEDGEFUNDS", "HEDGE FUNDS", "POSITIONING", "COT", "UVOL", "DVOL", "UPDOWN",
       "DEGROSS", "DEGROSSING", "LEVERAGE", "DARKPOOL", "DARKPOOLS", "DIX", "GEX", "PUTCALL", "PCR", "SHORTVOL"].includes(cmd)) cmd = "FLOW";
  // The week in review, and the terminal's own health.
  if (["WEEKLY", "REVIEW", "WRAP", "WEEKEND", "RECAP"].includes(cmd)) cmd = "WEEK";
  if (["HEALTH", "STATUS", "JOBS", "SYSTEM", "DIAG", "UPTIME"].includes(cmd)) cmd = "SYS";
  // The two indices from the inside. ^GSPC, ^NDX, SPY and QQQ stay security pages.
  if (["SPX", "SP500", "S&P500", "S&P", "SPX INDEX", "INDEX", "INDICES", "BREADTH", "MEMB"].includes(cmd)) return location.assign("/m/IDX?i=spx");
  if (["NDX", "NASDAQ100", "NASDAQ", "NDX INDEX", "QQQ IDX"].includes(cmd)) return location.assign("/m/IDX?i=ndx");
  // The euro. EURO and FXE the funds stay "T EURO", "T FXE".
  if (["EURO", "EURUSD", "EUR/USD", "FX", "FXC", "CURRENCY", "DOLLAR", "WCRS"].includes(cmd)) cmd = "EUR";
  // The macro calendar: ECO is Bloomberg's own code for it.
  if (["ECO", "ECOW", "MACRO", "CALENDAR", "RELEASES", "ECONOMY"].includes(cmd)) cmd = "MAC";
  // Two panels that live on other pages: the priced rate path, and risk by holding.
  if (["WIRP", "FEDWATCH", "RATEPATH", "FED"].includes(cmd)) return location.assign("/m/MKT/bonds#s-path");
  if (["RISK", "RISKMAP"].includes(cmd)) return location.assign("/portfolio#riskmap");
  // A code with a ticker, either way round. EE, OPT and the rest alone stay what they were.
  for (const w of WITH_TICKER) {
    const m = cmd.match(new RegExp(`^(?:${w.re})\\s+(\\S+)$`)) || cmd.match(new RegExp(`^(\\S+)\\s+(?:${w.re})$`));
    if (m) return location.assign(w.to(encodeURIComponent(m[1])));
  }
  const m = MOD[cmd];
  if (m) return location.assign(m.href);
  // Anything shaped like a symbol opens that security's page: MSFT, BRK-B,
  // ASML.AS, ^GSPC. "MSFT EQUITY" and "T MSFT" work too, for Bloomberg hands.
  // A typed word ("MARKET", "EARNINGS") still finds its module first; short
  // strings are tickers, since "MA" is Mastercard before it is Market.
  const guess = cmd.length >= 5 && MODULES.find(x => x.title.toUpperCase().startsWith(cmd));
  if (guess) return location.assign(guess.href);
  const sym = cmd.replace(/\s+(EQUITY|US|GO|<GO>)$/g, "").replace(/^T\s+/, "");
  if (/^\^?[A-Z0-9]{1,6}([.\-][A-Z]{1,3})?$/.test(sym) && !/^\d+$/.test(sym))
    return location.assign("/t/" + encodeURIComponent(sym));
  flash(`Unknown command "${esc(cmd)}" — a module code (HELP lists them) or a ticker.`);
}

/* ---------- exchange hours in the bar ---------------------------------------
   Amsterdam, Frankfurt, New York: open, shut, or New York's pre-market and
   after hours, with the time to the next bell. The server knows the
   holidays (the brief's calendar); the page counts down by itself and asks
   again when a bell has rung. */
const Hours = (() => {
  let D = null, t = null;
  const left = s => { s = Math.max(0, s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
    return h >= 24 ? `${Math.floor(h / 24)}d ${h % 24}h` : h ? `${h}h${String(m).padStart(2, "0")}` : `${m}m`; };
  const WORD = { open: "open", shut: "shut", pre: "pre-market", post: "after hours" };
  function draw() {
    const el = $("#termHours");
    if (!el || !D) return;
    const now = Date.now() / 1000;
    if (D.exchanges.some(x => x.at <= now)) { load(); return; }
    el.innerHTML = D.exchanges.map(x => `<span class="hx ${x.state}" title="${esc(x.n)}: ${WORD[x.state]}${x.holiday ? ` (${esc(x.holiday)})` : ""}, ${x.next} in ${left(x.at - now)}${x.early ? `; closes early at ${x.early}` : ""}">
      <i></i>${x.k === "NYC" ? "NY" : x.k}<small>${x.next} ${left(x.at - now)}</small></span>`).join("");
  }
  async function load() {
    try { const r = await fetch("/api/pf/hours"); if (r.ok) D = await r.json(); } catch {}
    draw();
  }
  return { start() { if (t) return; load(); t = setInterval(draw, 20000); setInterval(load, 600000); } };
})();

/* ---------- blurring your euros ------------------------------------------------
   One switch, remembered on every page: every euro amount on screen is
   blurred, percentages and dollar prices are not. Text is found as pages draw
   it (a MutationObserver), and each amount is wrapped in a span the stylesheet
   blurs; in a chart the whole label is. Inputs that hold euros carry
   data-eur. Anything inside data-noblur is left alone. */
const Blur = (() => {
  const RE = /(?:[−+\-]\s?)?€[\s ]?[−+\-]?\d[\d.,]*(?:[\s ]?(?:k|m|bn)\b)?|[−+\-]?\d[\d.,]*(?:[\s ]?(?:k|m))?[\s ]?(?:€|EUR\b(?!\/))|\bEUR[\s ]?[−+\-]?\d[\d.,]*/g;
  const SVG = "http://www.w3.org/2000/svg";
  let on = false, obs = null;
  try { on = localStorage.getItem("pf_blur") === "1"; } catch {}
  const skip = el => !el || el.closest("script,style,textarea,.eur-amt,[data-noblur]");
  function scan(root) {
    if (!root || !(root.nodeType === 1 || root.nodeType === 3)) return;
    if (root.nodeType === 3) { wrap(root); return; }
    if (skip(root)) return;
    const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const found = [];
    while (w.nextNode()) { RE.lastIndex = 0; if (RE.test(w.currentNode.nodeValue)) found.push(w.currentNode); }
    found.forEach(wrap);
  }
  function wrap(n) {
    const p = n.parentElement;
    if (!p || skip(p)) return;
    const s = n.nodeValue;
    RE.lastIndex = 0;
    if (!RE.test(s)) return;
    if (p.namespaceURI === SVG) { p.classList.add("eur-amt"); return; }
    const f = document.createDocumentFragment();
    let last = 0;
    RE.lastIndex = 0;
    s.replace(RE, (m, off) => {
      f.append(s.slice(last, off));
      const sp = document.createElement("span");
      sp.className = "eur-amt"; sp.textContent = m; f.append(sp);
      last = off + m.length;
      return m;
    });
    f.append(s.slice(last));
    n.replaceWith(f);
  }
  function watch() {
    if (obs || !document.body) return;
    obs = new MutationObserver(ms => {
      for (const m of ms) {
        if (m.type === "characterData") wrap(m.target);
        else m.addedNodes.forEach(scan);
      }
    });
    obs.observe(document.body, { childList: true, subtree: true, characterData: true });
    scan(document.body);
  }
  function apply() {
    document.documentElement.classList.toggle("blur-eur", on);
    const b = $("#blurBtn"); if (b) b.classList.toggle("on", on);
    if (on) { if (document.body) watch(); else document.addEventListener("DOMContentLoaded", watch, { once: true }); }
  }
  function toggle() { on = !on; try { localStorage.setItem("pf_blur", on ? "1" : "0"); } catch {} apply(); }
  document.addEventListener("keydown", e => {
    if (e.key === "B" && e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey &&
        !/^(INPUT|TEXTAREA|SELECT)$/.test((document.activeElement || {}).tagName)) { e.preventDefault(); toggle(); }
  });
  apply();
  return { get on() { return on; }, toggle, scan, RE };
})();

function flash(html) {
  let el = $("#termFlash");
  if (!el) {
    el = document.createElement("div");
    el.id = "termFlash";
    el.style.cssText = "position:fixed;left:50%;transform:translateX(-50%);bottom:1rem;z-index:200;" +
      "background:var(--surface);border:1px solid var(--warn);color:var(--warn);" +
      "padding:.4rem .7rem;font-size:.75rem;max-width:90vw;";
    document.body.appendChild(el);
  }
  el.innerHTML = html;
  clearTimeout(flash._t);
  flash._t = setTimeout(() => el.remove(), 3200);
}

function showHelp() {
  const rows = MODULES.map(m =>
    `<div class="row"><span class="nm"><b style="color:var(--accent)">${m.code}</b> — ${esc(m.title)}</span>
       <span class="dim"></span>
       <span class="ch ${m.state === "live" ? "pos" : (m.state === "next" ? "warnc" : "dim")}">${m.state}</span></div>`).join("");
  let el = $("#termHelp");
  if (el) { el.remove(); return; }
  el = document.createElement("div");
  el.id = "termHelp";
  el.style.cssText = "position:fixed;inset:0;z-index:150;background:rgba(3,6,8,.86);" +
    "display:flex;align-items:flex-start;justify-content:center;padding:8vh 1rem;overflow:auto;";
  el.innerHTML = `<div class="panel" style="width:min(760px,100%)">
      <header><h2>Commands</h2><span class="hint">Esc or HELP again to close · &ldquo;/&rdquo; focuses the command line</span></header>
      <div class="body"><div class="rows">${rows}
        <div class="row"><span class="nm"><b style="color:var(--accent)">HOME</b> — this screen</span><span class="dim"></span><span class="ch dim">live</span></div>
        ${WITH_TICKER.map(w => `<div class="row"><span class="nm"><b style="color:var(--accent)">${w.help[0]}</b> — ${esc(w.help[1])}</span><span class="dim"></span><span class="ch dim">with a ticker</span></div>`).join("")}
        <div class="row"><span class="nm"><b style="color:var(--accent)">MSFT</b> — any ticker opens that security's page</span><span class="dim"></span><span class="ch dim">ticker</span></div>
        <div class="row"><span class="nm"><b style="color:var(--accent)">WIRP</b> — the rate path the futures price · <b style="color:var(--accent)">RISK</b> — risk by holding</span><span class="dim"></span><span class="ch dim">panel</span></div>
        <div class="row"><span class="nm"><b>the book</b> — everything you hold: every open position together, at today's prices. A holding's weight is its share of the book</span><span class="dim"></span><span class="ch dim">a word</span></div>
      </div></div></div>`;
  el.onclick = e => { if (e.target === el) el.remove(); };
  document.addEventListener("keydown", function esckey(e) {
    if (e.key === "Escape") { el.remove(); document.removeEventListener("keydown", esckey); }
  });
  document.body.appendChild(el);
}
