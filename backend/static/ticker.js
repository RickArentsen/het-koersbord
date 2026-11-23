/* ==========================================================================
   One page per security, at /t/SYMBOL — any symbol, held or not.

   Eleven panels, each fed by its own request so a slow source (the SEC's
   segment data takes seconds the first time) never holds up the quote. Each
   panel says where its numbers came from; a panel that cannot be built says
   why instead of quietly showing nothing.

   Reading order is the order of the questions:
     what is it and what is it doing now      header, chart, what matters now
     why did it move                          daily moves, news, filings
     is the business any good                 financials, where the money comes
                                              from, are the investments working
     what does everyone expect                street, valuation, peers
     who owns it, and what do I think         ownership, your position, thesis
   ========================================================================== */

let SYM = decodeURIComponent(location.pathname.split("/t/")[1] || "").toUpperCase().replace(/\/+$/, "");
const S = {};                 // every payload, by part
const ERR = {};               // why a part failed
let CH = null;                // the chart
let DAY = null;               // the pinned day

$("#shell").innerHTML = termBar({
  here: SYM || "TICKER",
  right: `<a class="tbtn" href="/portfolio">PORT</a>
          <button class="tbtn" id="refreshBtn" title="Fetch everything again, ignoring what is cached for this page">Reload</button>`
});
wireShell();

/* ---------- formatting ---------------------------------------------------- */
const CCY = { USD: "$", EUR: "€", GBP: "£", JPY: "¥", CAD: "C$", AUD: "A$", HKD: "HK$", CHF: "CHF ", DKK: "kr ", SEK: "kr " };
const cs = c => { c = c || (S.overview || {}).currency; return c ? (CCY[c] ?? c + " ") : ""; };
const big = (v, c) => {
  if (v == null || !isFinite(v)) return "–";
  const a = Math.abs(v), s = v < 0 ? "−" : "";
  const u = a >= 1e12 ? (a / 1e12).toFixed(2) + "T" : a >= 1e9 ? (a / 1e9).toFixed(a >= 1e11 ? 0 : 1) + "B"
    : a >= 1e6 ? (a / 1e6).toFixed(a >= 1e8 ? 0 : 1) + "M" : a >= 1e3 ? (a / 1e3).toFixed(0) + "K" : nf(a, 0);
  return s + (c === false ? "" : cs(c)) + u;
};
const num = (v, d = 0) => v == null || !isFinite(v) ? "–" : (v >= 1e9 ? (v / 1e9).toFixed(2) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : nf(v, d));
const px = (v, d = 2) => v == null ? "–" : cs() + nf(v, d);
const mx = v => v == null || !isFinite(v) ? "–" : nf(v, 1) + "×";
const pl = (v, d = 1) => v == null || !isFinite(v) ? "–" : nf(v, d) + "%";
const pc = (v, d = 1) => v == null || !isFinite(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d) + "%";
const pp = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d) + "pp";
const cls = v => v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
const ago = ts => {
  if (!ts) return "";
  const s = Date.now() / 1000 - ts;
  if (s < 3600) return Math.max(1, Math.round(s / 60)) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  if (s < 86400 * 14) return Math.round(s / 86400) + "d ago";
  return dmy(new Date(ts * 1000).toISOString().slice(0, 10));
};
const daysTo = d => Math.round((new Date(d + "T12:00:00") - new Date()) / 864e5);
const kv = (a, b, c = "", title = "") => `<div class="kv"${title ? ` title="${esc(title)}"` : ""}><span>${a}</span><span class="${c}">${b}</span></div>`;
const src = t => t ? `<div class="src">${esc(t)}</div>` : "";
const toneIcon = t => ({ good: "▲", warn: "!", bad: "▼", info: "·" }[t] || "·");
const spin = `<div class="dim ld">loading<span class="blink">_</span></div>`;
function failed(part, what) {
  const e = ERR[part];
  if (!e) return spin;          // still loading, not broken
  return `<div class="note"><b>${esc(what || part)} unavailable</b>${esc(e || "no data")}</div>`;
}
const setHTML = (id, html) => { const el = document.getElementById(id); if (el) el.innerHTML = html; };

/* A tiny SVG sparkline; one series, one colour. */
function spark(vals, w = 90, h = 22, color = "var(--ink-mid)") {
  const v = vals.filter(x => x != null);
  if (v.length < 2) return "";
  const lo = Math.min(...v), hi = Math.max(...v), r = hi - lo || 1;
  const pts = vals.map((x, i) => x == null ? null : `${(i / (vals.length - 1) * (w - 2) + 1).toFixed(1)},${(h - 2 - (x - lo) / r * (h - 4)).toFixed(1)}`).filter(Boolean).join(" ");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="${color}" stroke-width="1.5" stroke-linejoin="round"/></svg>`;
}
/* A share bar: how big a part is of the whole. */
const bar = (p, color = "var(--accent)") => `<span class="bar"><i style="width:${Math.max(0, Math.min(100, p || 0))}%;background:${color}"></i></span>`;

/* ---------- loading ------------------------------------------------------- */
const PARTS = ["overview", "chart", "moves", "news", "street", "fundamentals", "catalysts",
  "valuation", "business", "peers", "ownership", "consensus"];

async function fetchPart(part) {
  try {
    const r = await fetch(`/api/pf/research/${encodeURIComponent(SYM)}/${part}`);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    if (j.error) throw new Error(j.error);
    S[part] = j; delete ERR[part];
  } catch (e) {
    ERR[part] = String(e.message || e);
  }
  RENDER[part] && RENDER[part]();
  renderGlance();
  if (["street", "moves", "news", "thesis", "position"].includes(part)) chartEvents();
}

async function loadPosition() {
  try {
    const r = await fetch("/api/pf/position/" + encodeURIComponent(SYM));
    S.position = r.ok ? await r.json() : null;
  } catch { S.position = null; }
  renderPosition(); renderHeader(); renderGlance(); chartEvents(); chartLines();
}

async function loadThesis() {
  try { S.thesis = await (await fetch(`/api/pf/research/${encodeURIComponent(SYM)}/thesis`)).json(); }
  catch { S.thesis = null; }
  renderThesis(); renderGlance(); chartEvents(); chartLines();
  if (S.catalysts) renderCatalysts();
}

function load() {
  if (!SYM) {
    $("#app").innerHTML = `<div class="panel"><div class="body"><div class="note"><b>No symbol in the address</b>
      Type a ticker in the command line above — MSFT, ASML, INGA.AS — or open one from PORT.</div></div></div>`;
    return;
  }
  scaffold();
  PARTS.forEach(fetchPart);
  // Deep links: ?day=2026-09-28 opens that session's card (the morning brief
  // links movers this way), #s-thesis and friends scroll once the sections exist.
  const qday = new URLSearchParams(location.search).get("day");
  if (qday && /^\d{4}-\d{2}-\d{2}$/.test(qday)) pinDay(qday);
  if (location.hash.length > 1) {
    const go = () => { const el = document.getElementById(location.hash.slice(1)); if (el) el.scrollIntoView({ block: "start" }); };
    setTimeout(go, 80); setTimeout(() => { if (window.scrollY > 0) go(); }, 1500);
  }
  loadPosition();
  loadThesis();
  loadFcf();
  loadPol();
}

/* Congress: members' own trades in this company, from POL (pf/pol.py), under its US tickers whatever line this is. */
async function loadPol() {
  const host = document.getElementById("pol");
  if (!host) return;
  try {
    const r = await fetch(`/api/pf/pol/ticker/${encodeURIComponent(SYM)}`);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    if (!j.built) { host.innerHTML = `<div class="dim">POL has not been built yet: open <a href="/m/POL">POL</a> to start it.</div>`; return; }
    const rows = (j.trades || []).slice(0, 20);
    $("#polHint").textContent = `${j.tickers.join(", ")} · in the last year ${j.year.buys} buys, ${j.year.sells} sells by ${j.year.members} member${j.year.members === 1 ? "" : "s"}`;
    host.innerHTML = rows.length ? `<div class="tscroll"><table class="tbl"><thead><tr><th>Filed</th><th class="l">Member</th><th class="l">Trade</th>
      <th>Amount</th><th>Traded</th><th title="from the first open after the filing, against SPY">12m alpha</th></tr></thead><tbody>
      ${rows.map(t => `<tr><td>${dmy(t.filed)}</td><td class="l"><a href="/m/POL#m=${encodeURIComponent(t.bg || "")}">${esc(t.member || "?")}</a>
          <span class="dim">${esc(t.party || "")}${t.chamber === "senate" ? " · senate" : ""}</span></td>
        <td class="l">${t.type === "purchase" ? `<span class="pos">buy</span>` : (t.type || "").startsWith("sale") ? `<span class="neg">sell</span>` : esc(t.type || "")}
          ${t.remit && t.remit.length ? `<span class="dim" title="inside their committees' remit">· in remit</span>` : ""}</td>
        <td>${esc(String(t.amount || "–").replace(/\s*-\s*/, "–"))}</td><td>${dmy(t.traded)}</td>
        <td>${t.type === "purchase" && t.alpha["12m"] != null ? `<span class="${sgn(t.alpha["12m"])}">${pct(t.alpha["12m"] * 100, 1)}</span>` : ""}</td></tr>`).join("")}</tbody></table></div>
      ${(j.trades || []).length > rows.length ? `<div class="dim" style="margin-top:.3rem"><a href="/m/POL#t=${encodeURIComponent(j.tickers[0])}">all ${j.trades.length} on POL ›</a></div>` : ""}`
      : `<div class="dim">No member of Congress has disclosed a trade in ${esc(j.tickers.join(", "))} since 2020.</div>`;
  } catch (e) {
    host.innerHTML = `<div class="note"><b>Congress unavailable</b>${esc(e.message || String(e))}</div>`;
  }
}

/* The FCF model: its own request, after the parts it reads from are on their way. */
async function loadFcf() {
  const host = document.getElementById("fcfm");
  if (!host) return;
  try {
    const r = await fetch(`/api/pf/fcf/${encodeURIComponent(SYM)}`);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    FCFK.card(host, j);
  } catch (e) {
    host.innerHTML = `<div class="note"><b>FCF model unavailable</b>${esc(e.message || String(e))}</div>`;
  }
}

$("#refreshBtn").onclick = () => { PARTS.forEach(p => { delete S[p]; }); load(); };

/* ---------- the page skeleton --------------------------------------------- */
const SECTIONS = [["top", "Overview"], ["s-chart", "Chart"], ["s-moves", "Moves"], ["s-news", "News"],
  ["s-fin", "Financials"], ["s-biz", "Business"], ["s-street", "Street"], ["s-val", "Valuation"], ["s-fcf", "FCF"],
  ["s-peers", "Peers"], ["s-own", "Ownership"], ["s-pol", "Congress"], ["s-thesis", "Thesis"]];

function scaffold() {
  document.title = `${SYM} · Het Koersbord`;
  $("#app").innerHTML = `
  <div class="panel" id="top"><div class="body" id="hdr">${spin}</div></div>
  <nav class="secnav" id="secnav">${SECTIONS.map(([id, t]) => `<a href="#${id}" data-id="${id}">${t}</a>`).join("")}</nav>

  <div class="cols c-3" id="s-chart">
    <div class="panel"><header><h2>Price</h2><span class="hint" id="chartHint"></span></header>
      <div class="body">
        <div class="pc-bar" id="pcBar"></div>
        <div id="pcHost"></div>
        <div class="pc-legend" id="pcLegend"></div>
        <div class="pc-stats" id="pcStats"></div>
        <div id="dayCard"></div>
      </div></div>
    <div class="stack">
      <div class="panel"><header><h2>What matters now</h2><span class="hint">built from every panel below</span></header>
        <div class="body" id="glance">${spin}</div></div>
      <div class="panel"><header><h2>Coming up</h2></header><div class="body" id="coming">${spin}</div></div>
    </div>
  </div>

  <div class="panel" id="s-moves"><header><h2>Daily moves — and what caused them</h2><span class="hint" id="movesHint"></span></header>
    <div class="body" id="moves">${spin}</div></div>

  <div class="cols c-news" id="s-news">
    <div class="panel"><header><h2>News</h2><span class="hint" id="newsHint"></span></header><div class="body" id="news">${spin}</div></div>
    <div class="panel"><header><h2>Deals, big numbers &amp; filings</h2></header><div class="body" id="deals">${spin}</div></div>
  </div>

  <div class="panel" id="s-fin"><header><h2>Financials</h2><span class="hint" id="finHint"></span></header>
    <div class="body" id="fin">${spin}</div></div>

  <div class="cols c-2" id="s-biz">
    <div class="panel"><header><h2>Where the money comes from</h2><span class="hint" id="bizHint"></span></header>
      <div class="body" id="biz">${spin}</div></div>
    <div class="panel"><header><h2>Are the investments working?</h2><span class="hint">capital spent against the return it earns</span></header>
      <div class="body" id="invest">${spin}</div></div>
  </div>

  <div class="panel" id="s-street"><header><h2>Street — estimates, revisions, targets</h2><span class="hint" id="streetHint"></span></header>
    <div class="body" id="street">${spin}</div></div>

  <div class="panel" id="s-val"><header><h2>Valuation in context</h2><span class="hint" id="valHint"></span></header>
    <div class="body" id="val">${spin}</div></div>

  <div class="panel" id="s-fcf"><header><h2>FCF model</h2><span class="hint">what the free cash flow says a share is worth · the company's own figures, yours to change</span>
    <a class="code" href="/m/FCF">FCF &rsaquo;</a></header>
    <div class="body" id="fcfm">${spin}</div></div>

  <div class="panel" id="s-peers"><header><h2>Peers</h2><span class="hint" id="peersHint"></span></header>
    <div class="body" id="peers">${spin}</div></div>

  <div class="cols c-2" id="s-own">
    <div class="panel"><header><h2>Ownership &amp; insiders</h2><span class="hint">Yahoo Finance</span></header>
      <div class="body" id="own">${spin}</div></div>
    <div class="stack">
      <div class="panel"><header><h2>Company</h2></header><div class="body" id="profile">${spin}</div></div>
      <div class="panel" id="posPanel" hidden><header><h2>Your position</h2><a class="code" href="/portfolio">PORT &rsaquo;</a></header>
        <div class="body" id="pos"></div></div>
    </div>
  </div>

  <div class="panel" id="s-pol"><header><h2>Congress</h2><span class="hint" id="polHint">members' own trades in it, from their filings</span>
    <a class="code" href="/m/POL">POL &rsaquo;</a></header><div class="body" id="pol">${spin}</div></div>

  <div class="panel" id="s-thesis"><header><h2>Your thesis</h2><span class="hint">saved locally · every save is a new version, nothing is overwritten</span></header>
    <div class="body" id="thesis">${spin}</div></div>`;

  // The section bar sits under the terminal bar, whose height depends on wrapping.
  const bar = document.querySelector(".term-bar");
  const setTop = () => document.documentElement.style.setProperty("--barh", (bar ? bar.offsetHeight : 36) + "px");
  setTop(); window.addEventListener("resize", setTop);
  const links = $$("#secnav a");
  const io = new IntersectionObserver(es => {
    for (const e of es) if (e.isIntersecting) links.forEach(a => a.classList.toggle("on", a.dataset.id === e.target.id));
  }, { rootMargin: "-40% 0px -55% 0px" });
  SECTIONS.forEach(([id]) => { const el = document.getElementById(id); el && io.observe(el); });

  CH = new PriceChart($("#pcHost"), {
    sym: SYM,
    onPick: d => pinDay(d),
    dayInfo: d => {
      const x = ((S.moves || {}).explained || {})[d];
      return x ? { driver: x[1], why: x[2] } : null;
    },
  });
  chartToolbar();
}

/* ---------- header -------------------------------------------------------- */
function renderHeader() {
  const o = S.overview;
  if (!o) {
    if (ERR.overview) {
      setHTML("hdr", `<div class="note"><b>${esc(SYM)} not found</b>${esc(ERR.overview)}<br>
        Check the symbol: US listings are plain (MSFT), others carry the exchange (ASML.AS, SAP.DE, SHEL.L).</div>`);
      // One clear message, not ten panels each saying "unavailable".
      $$("#app > :not(#top)").forEach(e => e.hidden = true);
    }
    return;
  }
  if (o.symbol && o.symbol !== SYM) {         // an ISIN resolved to its ticker
    SYM = o.symbol; history.replaceState(null, "", "/t/" + encodeURIComponent(SYM));
  }
  document.title = `${o.symbol} ${o.quote.price != null ? nf(o.quote.price, 2) : ""} · ${o.short || o.name}`;
  // A fund or an index has no income statement, segments, analysts or
  // insiders: those panels are hidden rather than shown as "unavailable".
  const nonCo = ["ETF", "INDEX", "MUTUALFUND", "CRYPTOCURRENCY", "CURRENCY", "FUTURE"].includes(o.quote_type);
  document.body.classList.toggle("nonco", nonCo);
  const q = o.quote, st = o.stats, P = S.position && (S.position.position || null);
  const ext = q.post_price && q.market_state && q.market_state !== "REGULAR"
    ? `<span class="ext">after hours ${nf(q.post_price, 2)} <b class="${cls(q.post_change_pct)}">${pc(q.post_change_pct, 2)}</b></span>`
    : q.pre_price && q.market_state === "PRE" ? `<span class="ext">pre-market ${nf(q.pre_price, 2)} <b class="${cls(q.pre_change_pct)}">${pc(q.pre_change_pct, 2)}</b></span>` : "";
  const rng = (q.high_52w && q.low_52w && q.price != null) ? Math.max(0, Math.min(100, (q.price - q.low_52w) / (q.high_52w - q.low_52w) * 100)) : null;
  const e = (S.catalysts || {}).earnings;
  const fig = (k, v, s = "", c = "", t = "") => `<div class="fig"${t ? ` title="${esc(t)}"` : ""}><span class="k">${k}</span><span class="v ${c}">${v}</span><span class="s">${s}</span></div>`;
  setHTML("hdr", `
    <div class="tkhead">
      <span class="sym">${esc(o.symbol)}</span>
      <span class="nm">${esc(o.name)}</span>
      ${o.exchange ? `<span class="chip">${esc(o.exchange)}</span>` : ""}
      ${o.sector ? `<span class="chip">${esc(o.sector)}</span>` : ""}
      ${o.industry ? `<span class="chip">${esc(o.industry)}</span>` : ""}
      ${S.position && S.position.open ? `<span class="chip hold" title="in your portfolio">held · ${pl(P && P.weight)} of book</span>`
        : S.position && S.position.closed ? `<span class="chip">traded before</span>` : ""}
      ${nonCo ? "" : `<a class="chip" href="/m/OPT/${encodeURIComponent(o.symbol)}" title="What its options price: the volatility surface, the range, the chain">options ›</a>
      <a class="chip" href="/m/SPLC/${encodeURIComponent(o.symbol)}" title="Who sells to it and who it sells to, in the filings' own words">supply chain ›</a>`}
      <span class="px">${cs()}${q.price != null ? nf(q.price, 2) : "–"}
        <span class="${cls(q.change_pct)}">${q.change != null ? (q.change >= 0 ? "+" : "") + nf(q.change, 2) : ""} ${pc(q.change_pct, 2)}</span></span>
    </div>
    <div class="tksub">${ext}<span class="dim">${esc(o.currency || "")} · ${q.market_state ? esc(q.market_state.toLowerCase()) : ""}
      ${q.time ? "· last " + dmyTime(q.time * 1000) : ""}</span></div>
    <div class="kstats">${nonCo ? fundFigs(o, fig, rng) : `
      ${fig("Market cap", big(st.market_cap), st.ev ? "EV " + big(st.ev) : "")}
      ${fig("P/E", st.pe_ttm ? nf(st.pe_ttm, 1) : "–", st.pe_fwd ? "fwd " + nf(st.pe_fwd, 1) : "", "", "price over trailing earnings; forward uses next-twelve-month consensus")}
      ${fig("EV/EBITDA", st.ev_ebitda ? nf(st.ev_ebitda, 1) : "–", st.ps ? "P/S " + nf(st.ps, 1) : "")}
      ${fig("FCF yield", pl(((S.valuation || {}).current || {}).fcf_yield ?? st.fcf_yield, 2), st.div_yield ? "div " + pl(st.div_yield, 2) : "no dividend", "",
        "free cash flow over market value; from the company's filings when available")}
      ${fig("52-week range", rng == null ? "–" : `<span class="rbar"><i style="left:${rng}%"></i></span>`,
        `${nf(q.low_52w, 0)} – ${nf(q.high_52w, 0)} · ${pc((q.price / q.high_52w - 1) * 100, 1)} off high`)}
      ${fig(st.beta_basis ? "Beta 1Y" : "Beta", st.beta ? nf(st.beta, 2) : "–", st.short_pct_float != null ? `short ${pl(st.short_pct_float, 1)} of float` : "", "",
            st.beta_basis ? `Yahoo has no five-year beta for it: measured here over ${st.beta_basis}` : "Yahoo's: five years of monthly returns against the S&P 500")}
      ${fig("Volume", num(q.volume), q.avg_volume ? `avg ${num(q.avg_volume)} · ${nf(q.volume / q.avg_volume, 1)}×` : "", q.volume && q.avg_volume && q.volume / q.avg_volume > 1.5 ? "warnc" : "")}
      ${fig("Next earnings", e ? dmy(e.date) : o.dates.earnings && o.dates.earnings >= new Date().toISOString().slice(0, 10) ? dmy(o.dates.earnings) : "–",
        e ? `${e.days >= 0 ? "in " + e.days + " days" : ""}${e.estimated ? " · estimated" : ""}` : "", e && e.days <= 7 && e.days >= 0 ? "warnc" : "")}
      ${fig("Street", o.analyst.rating ? esc(String(o.analyst.rating).replace(/^[\d.]+\s*-\s*/, "")) : "–",
        o.analyst.target_mean ? `target ${nf(o.analyst.target_mean, 0)} · ${pc((o.analyst.target_mean / q.price - 1) * 100, 0)}` : "")}`}
    </div>`);
}
/* The strip for a fund or an index: size, cost and yield instead of P/E. */
function fundFigs(o, fig, rng) {
  const q = o.quote, f = o.fund || {}, mv = S.moves, w = k => ((mv && mv.windows) || []).find(x => x.k === k);
  return `
    ${f.assets ? fig("Assets", big(f.assets), esc(f.family || "")) : fig("Type", esc(o.quote_type || ""), esc(o.exchange || ""))}
    ${fig("Expense ratio", f.expense != null ? pl(f.expense, 2) : "–", f.category ? esc(f.category) : "")}
    ${fig("Yield", f.yield != null ? pl(f.yield, 2) : "–", f.nav ? "NAV " + nf(f.nav, 2) : "")}
    ${fig("52-week range", rng == null ? "–" : `<span class="rbar"><i style="left:${rng}%"></i></span>`,
      `${nf(q.low_52w, 0)} – ${nf(q.high_52w, 0)} · ${pc((q.price / q.high_52w - 1) * 100, 1)} off high`)}
    ${fig("YTD", pc((w("YTD") || {}).stock, 1), "", cls((w("YTD") || {}).stock))}
    ${fig("1 year", pc((w("1Y") || {}).stock, 1), "", cls((w("1Y") || {}).stock))}
    ${fig("3y a year", f.ret_3y != null ? pc(f.ret_3y, 1) : "–", f.ret_5y != null ? "5y " + pc(f.ret_5y, 1) + " a year" : "")}
    ${fig("Volume", num(q.volume), q.avg_volume ? `avg ${num(q.avg_volume)}` : "")}`;
}

/* ---------- chart ---------------------------------------------------------- */
function chartToolbar() {
  const st = CH.st;
  const btn = (grp, v, lab, on, title = "") => `<button class="seg${on ? " on" : ""}" data-g="${grp}" data-v="${v}"${title ? ` title="${esc(title)}"` : ""}>${lab}</button>`;
  const bm = (S.chart || {}).benchmarks || {};
  const peers = ((S.peers || {}).rows || []).filter(r => !r.self).map(r => r.symbol);
  $("#pcBar").innerHTML = `
    <span class="grp">${PC_RANGES.map(r => btn("range", r, r, st.range === r)).join("")}</span>
    <span class="grp">${btn("type", "auto", "Auto", st.type === "auto", "candles up to 6 months, a line beyond")}${btn("type", "candle", "Candles", st.type === "candle")}${btn("type", "line", "Line", st.type === "line")}</span>
    <span class="grp">${btn("ma50", 1, "50d", st.ma50, "50-day moving average")}${btn("ma200", 1, "200d", st.ma200, "200-day moving average")}${btn("vol", 1, "Vol", st.vol)}${btn("log", 1, "Log", st.log, "logarithmic price scale")}</span>
    <span class="grp cmp"><span class="lbl">vs</span>
      ${bm.market ? btn("cmp", "market", `<i class="sw sw1"></i>${esc(bm.market.label)}`, st.cmp.market) : ""}
      ${bm.sector ? btn("cmp", "sector", `<i class="sw sw2"></i>${esc(bm.sector.label)}`, st.cmp.sector) : ""}
      ${peers.length ? `<select id="peerSel" class="seg" title="compare with a peer"><option value="">peer…</option>${peers.map(p => `<option${CH.peer && CH.peer.sym === p ? " selected" : ""}>${esc(p)}</option>`).join("")}</select>` : ""}
    </span>`;
  $$("#pcBar button.seg").forEach(b => b.onclick = () => {
    const g = b.dataset.g, v = b.dataset.v;
    if (g === "range") { CH.set("range", v); if ((v === "1D" || v === "5D") && !CH.intra[v]) loadIntra(v); }
    else if (g === "type") CH.set("type", v);
    else if (g === "cmp") { CH.st.cmp[v] = !CH.st.cmp[v]; CH.draw(); }
    else CH.set(g, !CH.st[g]);
    chartToolbar(); chartStats();
  });
  const ps = $("#peerSel");
  if (ps) ps.onchange = () => loadPeerLine(ps.value);
  const ev = CH.st.ev;
  const leg = (k, glyph, lab) => `<button class="lg${ev[k] ? " on" : ""}" data-k="${k}" title="show or hide">${glyph}${lab}</button>`;
  $("#pcLegend").innerHTML = `
    ${leg("earn", `<i class="g gE">E</i>`, "earnings (green beat · red miss)")}
    ${leg("analyst", `<i class="g gA">▲▼</i>`, "up/downgrades")}
    ${leg("filing", `<i class="g gF">F</i>`, "8-K filings")}
    ${leg("div", `<i class="g gD">D</i>`, "ex-dividend")}
    ${leg("move", `<i class="g gM">○</i>`, "company-specific move")}
    ${S.position && S.position.trades && S.position.trades.length ? leg("trade", `<i class="g gT">▲▼</i>`, "your trades") : ""}
    ${S.thesis && S.thesis.versions && S.thesis.versions.length ? leg("thesis", `<i class="g gH">T</i>`, "your thesis") : ""}`;
  $$("#pcLegend .lg").forEach(b => b.onclick = () => { ev[b.dataset.k] = !ev[b.dataset.k]; CH.draw(); chartToolbar(); });
}

async function loadIntra(r) {
  try {
    const j = await (await fetch(`/api/pf/research/${encodeURIComponent(SYM)}/intraday?range=${r}`)).json();
    CH.setIntra(r, j.bars ? j : { bars: [] });
  } catch { CH.setIntra(r, { bars: [] }); }
  chartStats();
}

async function loadPeerLine(sym) {
  if (!sym) { CH.setPeer(null); CH.st.cmp.peer = false; chartToolbar(); return; }
  try {
    const j = await (await fetch(`/api/pf/research/${encodeURIComponent(sym)}/chart`)).json();
    const m = {}; (j.bars || []).forEach(b => m[b[0]] = b[6]);
    CH.st.cmp.peer = true; CH.setPeer(sym, m);
  } catch { flash("could not load " + esc(sym)); }
  chartToolbar();
}

function chartStats() {
  const V = CH && CH.V;
  if (!V || !V.pts || V.pts.length < 2) { setHTML("pcStats", ""); return; }
  const p = V.pts, a = p[0], z = p[p.length - 1];
  const base = V.prevClose ?? a.c;
  const ch = (z.c / base - 1) * 100;
  const hi = p.reduce((m, x) => x.h > m.h ? x : m, p[0]), lo = p.reduce((m, x) => x.l < m.l ? x : m, p[0]);
  const rel = k => { const b = p.find(x => x[k] != null); return b && z[k] != null ? (z.c / a.c) / (z[k] / b[k]) * 100 - 100 : null; };
  const bm = (S.chart || {}).benchmarks || {};
  setHTML("pcStats", `
    <span><b class="${cls(ch)}">${pc(ch, 2)}</b> over ${esc(CH.st.range)}</span>
    <span>high ${nf(hi.h, 2)} <span class="dim">${V.intraday ? "" : dmy(hi.d)}</span></span>
    <span>low ${nf(lo.l, 2)} <span class="dim">${V.intraday ? "" : dmy(lo.d)}</span></span>
    ${!V.intraday && bm.market && rel("market") != null ? `<span>vs ${esc(bm.market.label)} <b class="${cls(rel("market"))}">${pp(rel("market"))}</b></span>` : ""}
    ${!V.intraday && bm.sector && rel("sector") != null ? `<span>vs ${esc(bm.sector.label)} <b class="${cls(rel("sector"))}">${pp(rel("sector"))}</b></span>` : ""}
    ${V.weekly ? `<span class="dim">weekly bars</span>` : ""}`);
}

/* Everything that happened on a day, keyed by session, for the chart. */
function chartEvents() {
  if (!CH) return;
  const ev = {};
  const add = (d, e) => { if (d) (ev[d] = ev[d] || []).push(e); };
  const st = S.street || {};
  for (const s of st.surprises || []) {
    const d = (s.reaction || {}).day || s.date, beat = (s.surprise_pct || 0) >= 0;
    add(d, { k: "earn", c: beat ? "gain" : "loss", tag: "earnings", text: `EPS ${s.eps_act} vs ${s.eps_est} est (${pc(s.surprise_pct, 1)}), stock ${pc((s.reaction || {}).move, 1)}` });
  }
  for (const a of st.actions || []) {
    if (a.action !== "up" && a.action !== "down") continue;
    const d = sessionOf(a.date);
    add(d, { k: "analyst", c: a.action === "up" ? "gain" : "loss", dir: a.action === "up" ? 1 : -1, tag: a.action === "up" ? "upgrade" : "downgrade",
      text: `${a.firm}: ${a.from || "?"} → ${a.to || "?"}${a.pt ? ", target " + nf(a.pt, 0) : ""}` });
  }
  for (const f of (S.news || {}).filings || []) {
    if (!["deal", "risk", "management", "capital"].includes(f.kind)) continue;
    add(sessionOf(f.date), { k: "filing", c: "accent", tag: f.form, text: f.what });
  }
  for (const dv of ((S.chart || {}).divs || [])) add(dv[0], { k: "div", c: "soft", tag: "ex-div", text: `dividend ${nf(dv[1], 2)}` });
  const mv = S.moves || {};
  for (const [d, x] of Object.entries(mv.explained || {})) {
    if (x[1] === "company" && Math.abs(x[3] || 0) >= 2.5) add(d, { k: "move", c: x[0] >= 0 ? "gain" : "loss", tag: "move", text: `${pc(x[0], 1)}, ${nf(x[3], 1)}σ company-specific` });
  }
  for (const t of (S.position || {}).trades || []) add(sessionOf(t.d), { k: "trade", c: t.kind === "BUY" ? "gain" : "loss", dir: t.kind === "BUY" ? 1 : -1, tag: "you " + t.kind.toLowerCase(), text: `${t.qty} @ ${nf(t.price, 2)}` });
  for (const v of (S.thesis || {}).versions || []) add(sessionOf(v.created.slice(0, 10)), { k: "thesis", c: "warn", tag: "thesis", text: `${v.stance || "thesis"} written at ${nf(v.price_at, 2)}` });
  CH.setEvents(ev);
  chartToolbar();
}
function sessionOf(day) {
  const bars = (S.chart || {}).bars;
  if (!bars || !day) return day;
  let lo = 0, hi = bars.length - 1;
  if (day > bars[hi][0]) return null;
  while (lo < hi) { const m = (lo + hi) >> 1; if (bars[m][0] < day) lo = m + 1; else hi = m; }
  return bars[lo][0];
}
function chartLines() {
  if (!CH) return;
  const L = [];
  const P = S.position && S.position.position;
  if (P && P.avg_price) L.push({ v: P.avg_price, label: "your avg", color: "var(--accent)" });
  const T = S.thesis && S.thesis.current;
  if (T && T.target) L.push({ v: T.target, label: "your target", color: "var(--warn)" });
  const a = (S.overview || {}).analyst;
  if (a && a.target_mean) L.push({ v: a.target_mean, label: "street target", color: "var(--ink-soft)" });
  // canvas cannot read var(); resolve now
  const cssv = v => v.startsWith("var(") ? getComputedStyle(document.documentElement).getPropertyValue(v.slice(4, -1)).trim() : v;
  CH.setLines(L.map(l => ({ ...l, color: cssv(l.color) })));
}

/* A day, in full, under the chart. */
async function pinDay(d) {
  DAY = d;
  setHTML("dayCard", `<div class="daycard">${spin}</div>`);
  let j = null;
  try { j = await (await fetch(`/api/pf/research/${encodeURIComponent(SYM)}/day/${d}`)).json(); } catch { }
  if (DAY !== d) return;
  if (!j || j.error) { setHTML("dayCard", `<div class="daycard"><div class="dim">${esc((j && j.error) || "could not load this day")}</div></div>`); return; }
  const m = j.move || {};
  const parts = m.mkt != null ? [["market", m.mkt, "var(--s1)"], ["sector", m.sec, "var(--s2)"], ["company", m.idio, "var(--ink)"]] : [];
  const maxAbs = Math.max(1, ...parts.map(p => Math.abs(p[1] || 0)), Math.abs(m.r || 0));
  const decomp = parts.length ? `<div class="decomp">${parts.map(([k, v, c]) => `
      <div class="dc"><span class="dk">${k}</span>
        <span class="dtrack"><span class="dmid"></span><i style="${v >= 0 ? "left:50%" : "right:50%"};width:${Math.abs(v || 0) / maxAbs * 50}%;background:${c}"></i></span>
        <span class="dv ${cls(v)}">${pp(v, 2)}</span></div>`).join("")}
      <div class="dc tot"><span class="dk">total</span><span></span><span class="dv ${cls(m.r)}"><b>${pc(m.r, 2)}</b></span></div></div>` : "";
  const cats = (m.catalysts || []).map(c => `<div class="cat"><b class="evk ${esc(c.type)}">${esc(c.type)}</b> ${c.url ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${esc(c.text)}</a>` : esc(c.text)}</div>`).join("");
  const nw = (j.news || []).slice(0, 8).map(n => `<a class="news" href="${esc(n.url)}" target="_blank" rel="noopener">
      <span class="hl">${esc(n.headline)}</span><span class="meta"><span class="chip">${esc(n.cat)}</span><span class="dim">${esc(n.source || "")} · ${n.ts ? dmyTime(n.ts * 1000) : ""}</span>${n.rel === "related" ? `<span class="chip">related</span>` : ""}</span></a>`).join("");
  setHTML("dayCard", `<div class="daycard">
    <div class="dhead"><b>${dmy(d)}</b> <span class="dim">${new Date(d + "T12:00:00").toLocaleDateString("en-GB", { weekday: "long" })}</span>
      <span>close ${nf(m.c, 2)}</span> <b class="${cls(m.r)}">${pc(m.r, 2)}</b>
      ${m.gap != null ? `<span class="dim">gap ${pc(m.gap, 2)}</span>` : ""}${m.range != null ? `<span class="dim">range ${pl(m.range, 2)}</span>` : ""}
      ${m.rvol ? `<span class="${m.rvol > 1.5 ? "warnc" : "dim"}">volume ${nf(m.rvol, 1)}× normal</span>` : ""}
      <button class="x" onclick="DAY=null;setHTML('dayCard','')" title="close">×</button></div>
    ${m.driver ? `<div class="dwhy"><b class="drv ${esc(m.driver)}">${esc(m.driver)}</b> ${esc(m.why || "")}</div>` : ""}
    <div class="dgrid"><div>${decomp}${cats ? `<div class="grp">Catalysts</div>${cats}` : ""}
      ${m.headline && !nw ? `<div class="grp">Headline</div><a href="${esc(m.headline.url)}" target="_blank" rel="noopener">${esc(m.headline.text)}</a>` : ""}</div>
      <div><div class="grp">News that session</div>${nw || `<div class="dim">${j.archive ? "No headlines archived for this session." : "No news archive — a Finnhub key under Settings on the home screen adds one."}</div>`}</div></div>
  </div>`);
}

/* ---------- what matters now --------------------------------------------- */
function insights() {
  const out = [];
  const o = S.overview, q = o && o.quote, st = o && o.stats;
  const add = (tone, tag, text, href) => out.push({ tone, tag, text, href });
  if (q && q.price && q.high_52w) {
    const off = (q.price / q.high_52w - 1) * 100;
    const vs200 = q.ma200 ? (q.price / q.ma200 - 1) * 100 : null;
    add(off > -5 ? "good" : off < -20 ? "warn" : "info", "price",
      `${pc(off, 1)} from the 52-week high of ${nf(q.high_52w, 2)}${vs200 != null ? `; ${pc(vs200, 1)} against the 200-day average — ${vs200 >= 0 ? "above" : "below"} the long-term trend` : ""}.`, "#s-chart");
  }
  const mv = S.moves;
  if (mv && mv.windows) {
    const y = mv.windows.find(w => w.k === "1Y"), m3 = mv.windows.find(w => w.k === "3M");
    if (y && y.stock != null && y.market != null)
      add(y.stock >= y.market ? "good" : "warn", "trend", `${pc(y.stock, 1)} over a year against ${pc(y.market, 1)} for ${mv.benchmarks.market}${y.sector != null ? ` and ${pc(y.sector, 1)} for ${mv.benchmarks.sector}` : ""}; three months ${pc(m3 && m3.stock, 1)}.`, "#s-moves");
    if (mv.model && mv.model.share_company != null)
      add("info", "drivers", `${nf(mv.model.share_company, 0)}% of its daily variance is company-specific; beta ${nf(mv.model.beta_market, 2)} to the market.`, "#s-moves");
  }
  const c = S.catalysts && S.catalysts.earnings;
  if (c && c.days != null && c.days >= 0) {
    const opt = S.catalysts.options && S.catalysts.options.earnings;
    const im = opt && (opt.event_move_pct || opt.move_pct);
    add(c.days <= 14 ? "warn" : "info", "earnings", `Earnings ${c.days === 0 ? "today" : "in " + c.days + " days"} (${dmy(c.date)}). Consensus EPS ${c.eps_est != null ? nf(c.eps_est, 2) : "–"} (${pc(c.eps_growth, 0)} y/y)${im ? `; options price a ±${nf(im, 1)}% move` : ""}${c.hist_avg_move ? ` against ±${nf(c.hist_avg_move, 1)}% on average` : ""}${c.beat_rate != null ? `, beat ${nf(c.beat_rate, 0)}% of the last ${c.n_hist}` : ""}.`, "#s-street");
  }
  const s = S.street;
  if (s && s.periods) {
    const fy = s.periods.find(p => p.period === "0y"), ny = s.periods.find(p => p.period === "+1y");
    const t = fy && fy.trend, r = fy && fy.revisions;
    if (t && t.chg_90d != null)
      add(t.chg_90d > 0.5 ? "good" : t.chg_90d < -0.5 ? "bad" : "info", "revisions",
        `This year's EPS consensus ${pc(t.chg_90d, 1)} in 90 days${r ? `; ${r.up30} analysts raised, ${r.down30} cut in the last month` : ""}${ny && ny.trend && ny.trend.chg_90d != null ? `. Next year ${pc(ny.trend.chg_90d, 1)}` : ""}.`, "#s-street");
  }
  const b = S.business;
  if (b && b.engine) add("good", "growth", `Fastest-growing line: ${b.engine.fastest.label} ${pc(b.engine.fastest.growth, 0)}; slowest: ${b.engine.slowest.label} ${pc(b.engine.slowest.growth, 0)}.`, "#s-biz");
  const inv = S.fundamentals && S.fundamentals.investments;
  if (inv) for (const f of (inv.findings || []).filter(f => ["capex", "roiic", "rpo"].includes(f.k))) add(f.tone, f.k === "rpo" ? "backlog" : "capital", f.text, "#s-biz");
  const v = S.valuation;
  if (v && v.stats && v.stats.pe && v.stats.pe["5y"]) {
    const d = v.stats.pe["5y"];
    add(d.pct_rank <= 30 ? "good" : d.pct_rank >= 75 ? "warn" : "info", "valuation",
      `P/E ${nf(d.current, 1)} — ${d.pct_rank <= 50 ? "cheaper" : "dearer"} than ${nf(d.pct_rank <= 50 ? 100 - d.pct_rank : d.pct_rank, 0)}% of the last five years (median ${nf(d.median, 1)})${v.current.pe_next_fy ? `; ${nf(v.current.pe_next_fy, 1)}× next year's earnings` : ""}${v.current.implied_growth != null ? `. The price implies ${nf(v.current.implied_growth, 1)}% a year of free-cash-flow growth for a decade` : ""}.`, "#s-val");
  }
  if (o && o.analyst && o.analyst.target_mean && q.price)
    add("info", "street", `${o.analyst.n || "The"} analysts: ${String(o.analyst.rating || "").replace(/^[\d.]+\s*-\s*/, "") || "–"}; mean target ${nf(o.analyst.target_mean, 0)} (${pc((o.analyst.target_mean / q.price - 1) * 100, 0)}), range ${nf(o.analyst.target_low, 0)}–${nf(o.analyst.target_high, 0)}.`, "#s-street");
  const ow = S.ownership && S.ownership.insider_summary;
  if (ow && (ow.buys || ow.sells))
    add(ow.buys > 0 ? "good" : "info", "insiders", `Insiders, six months: ${ow.buys} open-market buy${ow.buys === 1 ? "" : "s"}${ow.buy_value ? " (" + big(ow.buy_value) + ")" : ""}, ${ow.sells} sale${ow.sells === 1 ? "" : "s"} by ${ow.sellers} people${ow.sell_value ? " (" + big(ow.sell_value) + ")" : ""}.`, "#s-own");
  const n = S.news;
  if (n && n.impactful && n.impactful.length) {
    const top = n.impactful[0];
    add(top.sent > 0.2 ? "good" : top.sent < -0.2 ? "bad" : "info", "news", `${top.headline} — ${top.source}, ${ago(top.ts)}.`, "#s-news");
  }
  const P = S.position && S.position.position;
  if (S.position && S.position.open && P)
    add((P.unreal_eur || 0) >= 0 ? "good" : "warn", "you", `You hold ${P.qty} at ${nf(P.avg_price, 2)} average: ${eur(P.unreal_eur, 0)} unrealised, ${pl(P.weight)} of the book.`, "#posPanel");
  const T = S.thesis && S.thesis.current;
  if (T && T.review_date && daysTo(T.review_date) <= 7)
    add("warn", "thesis", `Your thesis review is ${daysTo(T.review_date) < 0 ? "overdue since" : "due"} ${dmy(T.review_date)}.`, "#s-thesis");
  return out;
}

/* Six dimensions, each scored 0–100 by a stated rule — a quick read, not a verdict. */
function scorecard() {
  const rows = [];
  const clamp = v => Math.max(0, Math.min(100, v));
  const f = S.fundamentals, inv = f && f.investments, ttm = inv && inv.ttm;
  if (ttm && ttm.rev_yoy != null)
    rows.push(["Growth", clamp(50 + ttm.rev_yoy * 2), `revenue ${pc(ttm.rev_yoy, 0)} over the last four quarters`, "50 + 2 × revenue growth"]);
  const qs = f && f.quarters, lq = qs && qs[qs.length - 1];
  const roic = inv && inv.annual && inv.annual.length ? inv.annual[inv.annual.length - 1].roic : null;
  if (lq && lq.op_margin != null)
    rows.push(["Profitability", clamp(lq.op_margin * 1.6 + (roic || 0) * 0.6), `operating margin ${pl(lq.op_margin, 0)}${roic != null ? ", ROIC " + pl(roic, 0) : ""}`, "1.6 × operating margin + 0.6 × ROIC"]);
  const v = S.valuation && S.valuation.current;
  if (v && v.net_debt != null && lq) {
    const ebit = f.ttm && f.ttm.op_income;
    const lev = ebit ? v.net_debt / ebit : null;
    rows.push(["Balance sheet", v.net_debt <= 0 ? 90 : clamp(85 - (lev || 0) * 18), v.net_debt <= 0 ? `net cash ${big(-v.net_debt)}` : `net debt ${nf(lev, 1)}× operating profit`, "net cash scores 90; net debt loses 18 points per turn of EBIT"]);
  }
  const pe = S.valuation && S.valuation.stats && S.valuation.stats.pe && S.valuation.stats.pe["5y"];
  if (pe) rows.push(["Cheapness", clamp(100 - pe.pct_rank), `P/E at the ${nf(pe.pct_rank, 0)}th percentile of five years`, "100 − the P/E's percentile against its own five-year history"]);
  const mv = S.moves, w = mv && mv.windows;
  if (w) {
    const y = w.find(x => x.k === "1Y"), m6 = w.find(x => x.k === "6M");
    if (y && y.stock != null && y.market != null)
      rows.push(["Momentum", clamp(50 + (y.stock - y.market) * 1.2 + ((m6 && m6.stock) || 0) * 0.4), `1Y ${pc(y.stock, 0)} vs market ${pc(y.market, 0)}`, "50 + 1.2 × one-year excess return + 0.4 × six-month return"]);
  }
  const s = S.street, fy = s && s.periods && s.periods.find(p => p.period === "0y");
  if (fy && fy.revisions) {
    const r = fy.revisions, tot = (r.up30 || 0) + (r.down30 || 0);
    const br = tot ? (r.up30 - r.down30) / tot : 0;
    rows.push(["Street", clamp(50 + br * 40 + ((fy.trend && fy.trend.chg_90d) || 0) * 3), `${r.up30} up / ${r.down30} down revisions, 30 days`, "50 + 40 × revision breadth + 3 × 90-day EPS change"]);
  }
  return rows;
}

function renderGlance() {
  if (!document.getElementById("glance")) return;
  const ins = insights(), sc = scorecard();
  if (!ins.length && !sc.length) return;
  setHTML("glance", `
    ${sc.length ? `<div class="score">${sc.map(([k, v, why, rule]) => `
      <div class="sc" title="${esc(rule)}"><span class="sk">${k}</span>
        <span class="strack"><i style="width:${v}%"></i></span><span class="sv">${nf(v, 0)}</span>
        <span class="sw2t">${esc(why)}</span></div>`).join("")}</div>` : ""}
    <div class="insl">${ins.map(i => `<a class="in ${i.tone}" href="${i.href || "#"}">
      <span class="it"><i>${toneIcon(i.tone)}</i>${esc(i.tag)}</span><span class="ix">${esc(i.text)}</span></a>`).join("")}</div>`);
}

/* ---------- coming up ------------------------------------------------------ */
function renderCatalysts() {
  const c = S.catalysts;
  if (!c) { setHTML("coming", failed("catalysts", "Calendar")); return; }
  const e = c.earnings, o = c.options || {}, d = c.dividend, T = S.thesis && S.thesis.current;
  const rows = [];
  if (e) {
    const im = o.earnings;
    rows.push(`<div class="cm"><div class="cmh"><b>Earnings</b> ${dmy(e.date)} <span class="${e.days <= 7 ? "warnc" : "dim"}">${e.days >= 0 ? "in " + e.days + " days" : ""}${e.estimated ? " · date estimated" : ""}</span>
        <a href="/m/ERN/${encodeURIComponent(SYM)}?d=${esc(e.date)}" style="margin-left:.4rem" title="What is expected, what usually happens, the numbers live, the report">ERN ›</a></div>
      ${kv("EPS consensus", e.eps_est != null ? `${nf(e.eps_est, 2)} <span class="dim">(${pc(e.eps_growth, 0)} y/y)</span>` : "–")}
      ${kv("Revenue consensus", e.rev_est != null ? `${big(e.rev_est)} <span class="dim">(${pc(e.rev_growth, 0)} y/y)</span>` : "–")}
      ${im ? kv("Options-implied move", `±${nf(im.event_move_pct || im.move_pct, 1)}%`, "", im.event_move_pct
        ? `straddle to ${im.expiry} prices ±${im.move_pct}%; the week before it ±${im.pre_move_pct}% — the difference, scaled for time, is the event`
        : `at-the-money straddle to ${im.expiry}`) : ""}
      ${e.hist_avg_move != null ? kv("Average reaction", `±${nf(e.hist_avg_move, 1)}% <span class="dim">(max ${nf(e.hist_max_move, 1)}%, last ${e.n_hist})</span>`) : ""}
      ${e.beat_rate != null ? kv("Beat rate", pl(e.beat_rate, 0), e.beat_rate >= 75 ? "pos" : "") : ""}</div>`);
  }
  if (o.front) rows.push(`<div class="cm">${kv("Expected move to " + dmy(o.front.expiry), `±${nf(o.front.move_pct, 1)}%`, "", "at-the-money straddle for the nearest expiry")}${o.front.iv ? kv("Implied volatility", pl(o.front.iv, 0)) : ""}</div>`);
  if (d && (d.ex || d.pay)) rows.push(`<div class="cm">${kv("Ex-dividend", d.ex ? dmy(d.ex) + (daysTo(d.ex) >= 0 ? ` <span class="dim">in ${daysTo(d.ex)}d</span>` : " <span class='dim'>passed</span>") : "–")}${kv("Paid", d.pay ? dmy(d.pay) : "–")}${kv("Annual rate", d.rate ? `${nf(d.rate, 2)} · ${pl(d.yield, 2)}` : "–")}</div>`);
  if (T && T.review_date) rows.push(`<div class="cm">${kv("Your thesis review", dmy(T.review_date), daysTo(T.review_date) < 0 ? "neg" : daysTo(T.review_date) <= 7 ? "warnc" : "")}</div>`);
  setHTML("coming", rows.join("") || `<div class="dim">Nothing scheduled that the feeds know of.</div>`);
}

/* ---------- moves ------------------------------------------------------------ */
let MV_TAB = "recent", MV_FILTER = "notable", MV_N = 40;
function renderMoves() {
  const m = S.moves;
  if (!m) { setHTML("moves", failed("moves", "Move analysis")); return; }
  setHTML("movesHint", `two-factor model on ${m.model.n} sessions · ${esc(m.archive)}`);
  const w = m.windows.filter(x => x.stock != null);
  const st = m.stats, md = m.model;
  const win = `<div class="tscroll"><table class="tbl win"><thead><tr><th></th>${w.map(x => `<th>${x.k}</th>`).join("")}</tr></thead><tbody>
    <tr><td>${esc(SYM)}</td>${w.map(x => `<td class="${cls(x.stock)}"><b>${pc(x.stock, 1)}</b></td>`).join("")}</tr>
    <tr><td><i class="sw sw1"></i>${esc(m.benchmarks.market)}</td>${w.map(x => `<td class="dim">${pc(x.market, 1)}</td>`).join("")}</tr>
    ${m.benchmarks.sector ? `<tr><td><i class="sw sw2"></i>${esc(m.benchmarks.sector)}</td>${w.map(x => `<td class="dim">${pc(x.sector, 1)}</td>`).join("")}</tr>` : ""}
    <tr><td>vs market</td>${w.map(x => { const r = x.market == null ? null : ((1 + x.stock / 100) / (1 + x.market / 100) - 1) * 100; return `<td class="${cls(r)}">${pp(r, 1)}</td>`; }).join("")}</tr>
  </tbody></table></div>`;
  const chips = `<div class="mchips">
    <span title="sensitivity to the market index">β market <b>${nf(md.beta_market, 2)}</b></span>
    ${md.beta_sector != null ? `<span title="sensitivity to the sector's move beyond the market">β sector <b>${nf(md.beta_sector, 2)}</b></span>` : ""}
    <span title="share of daily variance neither market nor sector explains">company-specific <b>${nf(md.share_company, 0)}%</b></span>
    <span>up days (1Y) <b>${nf(st.up_pct, 0)}%</b></span>
    <span>avg up <b class="pos">${pc(st.avg_up, 2)}</b> · down <b class="neg">${pc(st.avg_down, 2)}</b></span>
    <span>best <b class="pos">${pc(st.best && st.best.r, 1)}</b> ${st.best ? dmy(st.best.d) : ""}</span>
    <span>worst <b class="neg">${pc(st.worst && st.worst.r, 1)}</b> ${st.worst ? dmy(st.worst.d) : ""}</span>
    <span>streak <b class="${cls(st.streak)}">${st.streak > 0 ? st.streak + " up" : st.streak < 0 ? -st.streak + " down" : "–"}</b></span>
    <span>volatility 30d <b>${pl(st.vol30, 0)}</b> · 1Y <b>${pl(st.vol1y, 0)}</b></span>
    <span>max drawdown 1Y <b class="neg">${pl(st.max_dd_1y, 1)}</b></span></div>`;
  const tabs = [["recent", "Recent sessions"], ["biggest", "Biggest moves, 2 years"], ["calendar", "Calendar"], ["months", "Monthly returns"]];
  let body = "";
  if (MV_TAB === "recent" || MV_TAB === "biggest") {
    let rows = MV_TAB === "recent" ? m.days : m.biggest;
    if (MV_TAB === "recent" && MV_FILTER === "notable") rows = rows.filter(x => x.driver !== "quiet" && x.driver !== "n/a");
    if (MV_TAB === "recent" && MV_FILTER === "company") rows = rows.filter(x => x.driver === "company" || x.driver === "mixed");
    const shown = rows.slice(0, MV_N);
    body = `${MV_TAB === "recent" ? `<div class="fbar">${[["all", "All"], ["notable", "Not quiet"], ["company", "Company-specific"]].map(([k, l]) => `<button class="seg${MV_FILTER === k ? " on" : ""}" data-mf="${k}">${l}</button>`).join("")}</div>` : ""}
      <div class="tscroll"><table class="tbl moves"><thead><tr><th>Date</th><th>Close</th><th>Move</th>
        <th title="beta × the market's move">market</th><th title="the sector's move beyond the market">sector</th>
        <th title="what is left: the company's own move, and how unusual it was">company</th><th title="volume against its 50-day average">vol</th><th>Why</th></tr></thead><tbody>
      ${shown.map(x => `<tr data-d="${x.d}" class="${Math.abs(x.z || 0) >= 2 ? "hot" : ""}">
        <td>${dmy(x.d)}</td><td>${nf(x.c, 2)}</td><td class="${cls(x.r)}"><b>${pc(x.r, 2)}</b></td>
        <td class="${cls(x.mkt)}">${pp(x.mkt, 1)}</td><td class="${cls(x.sec)}">${pp(x.sec, 1)}</td>
        <td class="${cls(x.idio)}">${pp(x.idio, 1)} <span class="dim">${x.z != null ? nf(x.z, 1) + "σ" : ""}</span></td>
        <td class="${x.rvol > 1.5 ? "warnc" : "dim"}">${x.rvol ? nf(x.rvol, 1) + "×" : ""}</td>
        <td class="why"><b class="drv ${esc(x.driver)}">${esc(x.driver)}</b> ${esc(x.why)}
          ${x.headline ? ` <a href="${esc(x.headline.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">↗</a>` : ""}</td></tr>`).join("")}
      </tbody></table></div>
      ${rows.length > MV_N ? `<button class="tbtn more" id="mvMore">Show ${Math.min(40, rows.length - MV_N)} more</button>` : ""}`;
  } else if (MV_TAB === "calendar") {
    body = calendarHeat(m.calendar);
  } else {
    body = monthsHeat(m.months);
  }
  setHTML("moves", `${win}${chips}
    <div class="tabs">${tabs.map(([k, l]) => `<button class="tab${MV_TAB === k ? " on" : ""}" data-mt="${k}">${l}</button>`).join("")}
      <span class="dim tabnote">click a row or a day to see its full story on the chart</span></div>${body}`);
  $$("#moves [data-mt]").forEach(b => b.onclick = () => { MV_TAB = b.dataset.mt; MV_N = 40; renderMoves(); });
  $$("#moves [data-mf]").forEach(b => b.onclick = () => { MV_FILTER = b.dataset.mf; MV_N = 40; renderMoves(); });
  const more = $("#mvMore"); if (more) more.onclick = () => { MV_N += 40; renderMoves(); };
  $$("#moves [data-d]").forEach(r => r.onclick = () => showDay(r.dataset.d));
}
function showDay(d) {
  // pick a range that contains the day, then pin it
  const age = (Date.now() - new Date(d)) / 864e5;
  const r = age < 28 ? "1M" : age < 88 ? "3M" : age < 178 ? "6M" : age < 360 ? "1Y" : "3Y";
  const order = PC_RANGES.indexOf(CH.st.range);
  if (order < PC_RANGES.indexOf(r) || CH.st.range === "1D" || CH.st.range === "5D") CH.set("range", r);
  chartToolbar(); chartStats();
  pinDay(d);
  document.getElementById("s-chart").scrollIntoView({ behavior: "smooth", block: "start" });
}
function heatColor(r) {
  if (r == null) return "var(--sunk)";
  const a = Math.min(1, Math.abs(r) / 4);
  return r >= 0 ? `rgba(63,179,128,${0.12 + a * 0.78})` : `rgba(224,100,79,${0.12 + a * 0.78})`;
}
function calendarHeat(cal) {
  if (!cal || !cal.length) return `<div class="dim">No sessions.</div>`;
  const first = new Date(cal[0][0] + "T12:00:00");
  const start = new Date(first); start.setDate(first.getDate() - ((first.getDay() + 6) % 7));
  const by = Object.fromEntries(cal.map(x => [x[0], x]));
  const weeks = [];
  for (let d = new Date(start); d <= new Date(); d.setDate(d.getDate() + 7)) weeks.push(new Date(d));
  const M = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  let lastM = -1;
  const heads = weeks.map(w => { const m = w.getMonth(); const s = m !== lastM ? M[m] : ""; lastM = m; return `<span>${s}</span>`; }).join("");
  const rows = [0, 1, 2, 3, 4].map(dow => `<div class="hrow"><span class="hl2">${["Mon", "Tue", "Wed", "Thu", "Fri"][dow]}</span>${weeks.map(w => {
    const d = new Date(w); d.setDate(w.getDate() + dow);
    const k = d.toISOString().slice(0, 10), x = by[k];
    return x ? `<span class="hc${Math.abs(x[2] || 0) >= 2 ? " z" : ""}" data-d="${k}" style="background:${heatColor(x[1])}" title="${dmy(k)}: ${pc(x[1], 2)}${x[2] != null ? " · " + nf(x[2], 1) + "σ company" : ""}"></span>` : `<span class="hc e"></span>`;
  }).join("")}</div>`).join("");
  return `<div class="heat"><div class="hrow hh"><span class="hl2"></span>${heads}</div>${rows}</div>
    <div class="legend hleg"><span>down</span>${[-4, -2, -1, 0, 1, 2, 4].map(r => `<i class="hc" style="background:${heatColor(r)}"></i>`).join("")}<span>up</span>
    <span class="dim">outlined: a company-specific move beyond 2σ</span></div>`;
}
function monthsHeat(ms) {
  if (!ms || !ms.length) return `<div class="dim">No history.</div>`;
  const years = [...new Set(ms.map(x => x.m.slice(0, 4)))].sort().reverse();
  const by = Object.fromEntries(ms.map(x => [x.m, x.r]));
  const M = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const avg = M.map((_, i) => { const v = years.map(y => by[`${y}-${String(i + 1).padStart(2, "0")}`]).filter(x => x != null); return v.length ? v.reduce((a, b) => a + b, 0) / v.length : null; });
  const up = M.map((_, i) => { const v = years.map(y => by[`${y}-${String(i + 1).padStart(2, "0")}`]).filter(x => x != null); return v.length ? v.filter(x => x > 0).length / v.length * 100 : null; });
  const yr = y => { let p = 1, any = false; for (let i = 1; i <= 12; i++) { const r = by[`${y}-${String(i).padStart(2, "0")}`]; if (r != null) { p *= 1 + r / 100; any = true; } } return any ? (p - 1) * 100 : null; };
  return `<div class="tscroll"><table class="tbl mheat"><thead><tr><th></th>${M.map(m => `<th>${m}</th>`).join("")}<th>Year</th></tr></thead><tbody>
    ${years.map(y => `<tr><td>${y}</td>${M.map((_, i) => { const r = by[`${y}-${String(i + 1).padStart(2, "0")}`]; return `<td style="background:${r == null ? "transparent" : heatColor(r / 2.5)}">${r == null ? "" : pc(r, 1)}</td>`; }).join("")}<td class="${cls(yr(y))}"><b>${pc(yr(y), 1)}</b></td></tr>`).join("")}
    <tr class="avg"><td>average</td>${avg.map(a => `<td class="${cls(a)}">${pc(a, 1)}</td>`).join("")}<td></td></tr>
    <tr class="avg"><td>% up</td>${up.map(a => `<td>${a == null ? "" : nf(a, 0) + "%"}</td>`).join("")}<td></td></tr>
  </tbody></table></div><div class="src">month-end adjusted closes; seasonality over ${years.length} years is a curiosity, not a signal</div>`;
}

/* ---------- news ------------------------------------------------------------- */
let NW_CAT = "all", NW_REL = false, NW_Q = "", NW_N = 30;
const CAT_LABEL = { earnings: "Earnings", deal: "Deals", analyst: "Analysts", product: "Product", legal: "Legal & regulation", management: "Management", capital: "Capital", market: "Market", opinion: "Opinion", other: "Other" };
function renderNews() {
  const n = S.news;
  if (!n) { setHTML("news", failed("news", "News")); setHTML("deals", failed("news", "Deals")); return; }
  setHTML("newsHint", `${n.n_mine} about ${esc((S.overview || {}).short || SYM)} · ${(n.providers || []).join(" + ") || "no provider"}${n.tone_7d != null ? ` · 7-day tone ${n.tone_7d > 0 ? "+" : ""}${nf(n.tone_7d, 2)}` : ""}`);
  let items = n.items.filter(i => NW_REL || i.rel !== "related");
  const counts = {};
  items.forEach(i => counts[i.cat] = (counts[i.cat] || 0) + 1);
  if (NW_CAT !== "all") items = items.filter(i => i.cat === NW_CAT);
  if (NW_Q) { const rx = new RegExp(NW_Q.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "i"); items = items.filter(i => rx.test(i.headline + " " + i.summary)); }
  const cats = Object.keys(CAT_LABEL).filter(k => counts[k]);
  const hl = s => esc(s).replace(/((?:US)?\$|€|£)\s?\d[\d,.]*\s?(?:trillion|billion|million|bn|mn|tn|[bmk])?\b/gi, "<mark>$&</mark>");
  setHTML("news", `
    <div class="fbar"><button class="seg${NW_CAT === "all" ? " on" : ""}" data-nc="all">All ${Object.values(counts).reduce((a, b) => a + b, 0)}</button>
      ${cats.map(k => `<button class="seg${NW_CAT === k ? " on" : ""}" data-nc="${k}">${CAT_LABEL[k]} ${counts[k]}</button>`).join("")}
      <label class="chk"><input type="checkbox" id="nwRel"${NW_REL ? " checked" : ""}> related (${n.n_related})</label>
      <input class="srch" id="nwQ" placeholder="filter headlines" value="${esc(NW_Q)}"></div>
    <div class="nlist">${items.slice(0, NW_N).map(i => `<a class="news nitem" href="${esc(i.url)}" target="_blank" rel="noopener">
      <span class="meta"><span class="dim">${ago(i.ts)}</span><span class="chip c-${i.cat}">${esc(CAT_LABEL[i.cat] || i.cat)}</span>
        ${i.rel !== "direct" ? `<span class="chip">${i.rel}</span>` : ""}<span class="dim">${esc(i.source)}</span>
        ${i.sent ? `<span class="sent ${i.sent > 0 ? "pos" : "neg"}" title="headline tone, word-count based">${i.sent > 0 ? "+" : "−"}${nf(Math.abs(i.sent), 1)}</span>` : ""}</span>
      <span class="hl">${hl(i.headline)}</span>
      ${i.summary ? `<span class="sum">${esc(i.summary)}</span>` : ""}</a>`).join("") || `<div class="dim">Nothing matches.</div>`}
    ${items.length > NW_N ? `<button class="tbtn more" id="nwMore">Show more (${items.length - NW_N})</button>` : ""}</div>
    ${(n.errors || []).length ? `<div class="src">${esc(n.errors.join(" · "))}</div>` : ""}
    ${!n.has_key ? `<div class="note" style="margin-top:.5rem"><b>Thin coverage</b>Without a Finnhub key only Yahoo's last few headlines are available, and the move analysis has no news archive. Add a free key under Settings on the home screen.</div>` : ""}`);
  $$("#news [data-nc]").forEach(b => b.onclick = () => { NW_CAT = b.dataset.nc; NW_N = 30; renderNews(); });
  $("#nwRel").onchange = e => { NW_REL = e.target.checked; renderNews(); };
  const q = $("#nwQ"); q.oninput = () => { NW_Q = q.value; clearTimeout(q._t); q._t = setTimeout(() => { renderNews(); const q2 = $("#nwQ"); q2.focus(); q2.setSelectionRange(q2.value.length, q2.value.length); }, 250); };
  const more = $("#nwMore"); if (more) more.onclick = () => { const y = $(".nlist").scrollTop; NW_N += 30; renderNews(); $(".nlist").scrollTop = y; };

  // Deals, numbers, filings
  const imp = n.impactful || [];
  const seenU = new Set(imp.map(i => i.url));
  const deals = (n.deals || []).filter(d => !seenU.has(d.url));
  const fil = (n.filings || []).filter(f => f.kind !== "report").slice(0, 14);
  const KIND = { deal: "deal", risk: "risk", earnings: "results", management: "people", capital: "capital", governance: "governance", other: "other", report: "report" };
  setHTML("deals", `
    <div class="grp">Most impactful, last two weeks</div>
    ${imp.map(i => `<a class="news" href="${esc(i.url)}" target="_blank" rel="noopener"><span class="hl">${hl(i.headline)}</span>
      <span class="meta"><span class="chip c-${i.cat}">${esc(CAT_LABEL[i.cat] || i.cat)}</span><span class="dim">${esc(i.source)} · ${ago(i.ts)}</span>
      ${(i.money || []).slice(0, 3).map(m => `<span class="num">${esc(m)}</span>`).join("")}</span></a>`).join("") || `<div class="dim">Nothing stands out.</div>`}
    <div class="grp">Deals, partnerships &amp; contracts</div>
    ${deals.slice(0, 8).map(i => `<a class="news" href="${esc(i.url)}" target="_blank" rel="noopener"><span class="hl">${hl(i.headline)}</span>
      <span class="meta"><span class="dim">${esc(i.source || "")} · ${ago(i.ts)}</span>${(i.money || []).slice(0, 2).map(m => `<span class="num">${esc(m)}</span>`).join("")}</span></a>`).join("") || `<div class="dim">No deal news in the last month.</div>`}
    <div class="grp">SEC filings — material events</div>
    ${fil.length ? `<div class="rows">${fil.map(f => `<div class="row"><span class="nm"><span class="chip k-${f.kind}">${KIND[f.kind] || f.kind}</span>
      <a href="${esc(f.url)}" target="_blank" rel="noopener">${esc(f.what)}</a></span><span class="dim">${esc(f.form)}</span><span class="ch dim">${dmy(f.date)}</span></div>`).join("")}</div>`
      : `<div class="dim">${S.fundamentals && S.fundamentals.cik ? "No 8-Ks on file recently." : "Not an SEC filer — material events are published on the company's own exchange."}</div>`}`);
}

/* ---------- financials --------------------------------------------------------- */
let FIN_F = "q", FIN_K = null;
const FIN_ROWS = [
  ["Income", null],
  ["Revenue", "revenue", "big"], ["  growth y/y", "revenue_yoy", "pct"], ["  q/q", "revenue_qoq", "pct", "q"],
  ["  growth speeding up by", "rev_accel", "pp", "q"], ["Gross margin", "gross_margin", "pl"],
  ["Operating income", "op_income", "big"], ["  operating margin", "op_margin", "pl"], ["Net income", "net_income", "big"],
  ["EPS, reported", "eps", "eps"], ["  growth y/y", "eps_yoy", "pct"],
  ["EPS, as analysts count it", "eps_street", "eps"], ["  growth y/y", "eps_street_yoy", "pct"], ["R&D, % of revenue", "rnd_pct", "pl"],
  ["Cash flow", null],
  ["Operating cash flow", "ocf", "big"], ["Capital spending", "capex", "big"], ["  % of revenue", "capex_pct", "pl"],
  ["Free cash flow", "fcf", "big"], ["  FCF margin", "fcf_margin", "pl"], ["Stock-based pay", "sbc", "big"],
  ["FCF after stock-based pay", "fcf_sbc", "big"],
  ["Buybacks", "buybacks", "big"], ["Dividends paid", "dividends", "big"], ["Acquisitions", "acquisitions", "big"],
  ["Balance sheet", null],
  ["Cash & short-term inv.", "cashinv", "big"], ["Debt", "debtall", "big"], ["Net cash", "netcash", "big"],
  ["  net debt / EBITDA", "nd_ebitda", "x"], ["  interest cover", "int_cover", "x"],
  ["Inventory days", "inv_days", "days"], ["Receivable days", "rec_days", "days"],
  ["Customer prepayments", "deferred_rev", "big"], ["Backlog (RPO)", "rpo", "big"],
  ["Equity", "equity", "big"], ["Diluted shares", "shares", "num"],
];
// What a row's chart shows besides the figure: its growth, and for a flow the last four quarters' sum.
const FLOWS = new Set(["revenue", "op_income", "net_income", "ocf", "capex", "fcf", "sbc", "fcf_sbc", "buybacks", "dividends", "acquisitions", "eps", "eps_street"]);
const finRow = r => ({ ...r, cashinv: (r.cash || 0) + (r.st_inv || 0) || null, debtall: (r.debt || 0) + (r.debt_st || 0) || null,
  netcash: r.cash != null ? (r.cash || 0) + (r.st_inv || 0) - (r.debt || 0) - (r.debt_st || 0) : null });
function renderFin() {
  const f = S.fundamentals;
  if (!f) { setHTML("fin", failed("fundamentals", "Financials")); setHTML("invest", failed("fundamentals", "Investment analysis")); return; }
  setHTML("finHint", `${esc(f.source)}${f.currency && f.currency !== (S.overview || {}).currency ? " · reported in " + esc(f.currency) : ""}`);
  const all = (FIN_F === "q" ? f.quarters : f.annual).map(finRow);
  const rows = all.slice(FIN_F === "q" ? -10 : -8);
  const ests = ((f.estimates || {})[FIN_F] || []);
  const hasProv = FIN_F === "q" && rows.some(r => r.provisional);
  const fc = f.currency;
  const cell = (r, k, t, x = "") => {
    const v = r[k];
    if (v == null) return `<td class="dim ${x}">${r.estimate ? "" : "–"}</td>`;
    let mark = "";
    if (k === "eps" && r.eps_gap != null && Math.abs(r.eps_gap) > 10) mark = ` <span class="gapm" title="reported ${nf(r.eps, 2)}, ${nf(r.eps_street, 2)} as analysts count it (${pc(r.eps_gap, 0)}): a one-off gain or charge">≠</span>`;
    if (t === "big") return `<td class="${v < 0 ? "neg" : ""} ${x}">${big(v, fc)}${mark}</td>`;
    if (t === "pct") return `<td class="${cls(v)} ${x}">${pc(v, 1)}</td>`;
    if (t === "pp") return `<td class="${cls(v)} ${x}">${pp(v, 1)}</td>`;
    if (t === "pl") return `<td class="${x}">${pl(v, 1)}</td>`;
    if (t === "x") return `<td class="${x}">${nf(v, 1)}×</td>`;
    if (t === "days") return `<td class="${x}">${nf(v, 0)}</td>`;
    if (t === "eps") return `<td class="${x}">${nf(v, 2)}${mark}</td>`;
    return `<td class="${x}">${num(v)}</td>`;
  };
  const tt = f.ttm;
  const ttmRow = tt ? { ...finRow(tt), revenue_yoy: null, gross_margin: tt.gross_profit && tt.revenue ? tt.gross_profit / tt.revenue * 100 : null,
    op_margin: tt.op_income && tt.revenue ? tt.op_income / tt.revenue * 100 : null, fcf_margin: tt.fcf != null && tt.revenue ? tt.fcf / tt.revenue * 100 : null,
    capex_pct: tt.capex && tt.revenue ? tt.capex / tt.revenue * 100 : null, rnd_pct: tt.rnd && tt.revenue ? tt.rnd / tt.revenue * 100 : null,
    fcf_sbc: tt.fcf != null && tt.sbc != null ? tt.fcf - tt.sbc : null, eps_street: null } : null;
  const inv = f.investments || {};
  const ttmG = inv.ttm || {};
  const last = rows[rows.length - 1] || {};
  if (ttmRow) { ttmRow.revenue_yoy = ttmG.rev_yoy; ttmRow.eps_yoy = null;
    ["nd_ebitda", "int_cover", "inv_days", "rec_days", "deferred_rev"].forEach(k => ttmRow[k] = last[k]); }
  const showTtm = FIN_F === "q" && ttmRow;
  const flags = f.flags || [];
  setHTML("fin", `
    <div class="fbar"><button class="seg${FIN_F === "q" ? " on" : ""}" data-ff="q">Quarterly</button><button class="seg${FIN_F === "a" ? " on" : ""}" data-ff="a">Annual</button>
      <span class="dim">* derived by subtraction (Q4 = year − nine months) · figures in ${esc(fc || "")}${hasProv ? " · ᵖ provisional: revenue, net income and EPS (as analysts count it) from the earnings release, until the statements arrive" : ""}${ests.length ? " · E: consensus forecast" : ""} · ≠ reported EPS differs from the analysts' by more than a tenth${f.note ? " · " + esc(f.note) : ""}</span></div>
    ${flags.length ? `<div class="finds">${flags.map(x => `<div class="fd ${x.tone}"><i>${toneIcon(x.tone)}</i><span>${esc(x.text)}</span></div>`).join("")}</div>` : ""}
    <div class="finch">${finCharts(FIN_F === "q" ? f.quarters.slice(-12) : f.annual.slice(-8), fc)}</div>
    <div class="tscroll"><table class="tbl fin"><thead><tr><th></th>${rows.map(r => `<th${r.provisional ? ` class="prov" title="reported ${dmy(r.filed)}; the full statements are not out yet"` : r.kept ? ` title="kept from an earlier read: Yahoo no longer shows it"` : ""}>${esc(r.label)}${r.derived ? "*" : ""}${r.provisional ? "ᵖ" : ""}<div class="dim">${dmy(r.end).slice(3)}</div></th>`).join("")}${showTtm ? `<th class="ttm">TTM</th>` : ""}${ests.map(e => `<th class="est" title="${esc(e.source || "")}${e.n ? ", " + e.n + " analysts" : ""}">${esc(e.label)}<div class="dim">${dmy(e.end).slice(3)}</div></th>`).join("")}</tr></thead>
    <tbody>${FIN_ROWS.map(([lab, k, t, only]) => k === null ? `<tr class="sec"><td colspan="${rows.length + 2 + ests.length}">${lab}</td></tr>`
      : (only && only !== FIN_F) || (rows.every(r => r[k] == null) && ests.every(e => e[k] == null)) ? ""
      : `<tr data-fk="${k}" class="${lab.startsWith("  ") ? "sub" : ""}${FIN_K === k ? " on" : ""}" title="click for the chart"><td>${esc(lab.trim())}</td>${rows.map(r => cell(r, k, t)).join("")}${showTtm ? cell(ttmRow, k, t, "ttm") : ""}${ests.map(e => cell(e, k, t, "est")).join("")}</tr>`).join("")}
    </tbody></table></div>
    <div id="finDet"></div>`);
  $$("#fin [data-ff]").forEach(b => b.onclick = () => { FIN_F = b.dataset.ff; renderFin(); });
  $$("#fin tr[data-fk]").forEach(tr => tr.onclick = () => { FIN_K = FIN_K === tr.dataset.fk ? null : tr.dataset.fk; renderFin(); });
  if (FIN_K) finDetail(FIN_K, all, ests, fc);
  renderInvest();
}

/* One row as a chart: every period available, the forecast after it, its growth, and for a flow the last four quarters' sum. */
function finDetail(k, all, ests, fc) {
  const def = FIN_ROWS.find(r => r[1] === k) || [k, k, "big"];
  const t = def[2];
  const pts = all.map(r => ({ l: r.label, end: r.end, v: r[k], yoy: r[k === "revenue" ? "revenue_yoy" : k + "_yoy"], prov: r.provisional }))
    .concat(ests.filter(e => e[k] != null).map(e => ({ l: e.label, end: e.end, v: e[k], yoy: e[k + "_yoy"], est: true })))
    .filter(p => p.v != null);
  if (pts.length < 2) { setHTML("finDet", `<div class="dim" style="padding:.6rem 0">Too few periods to draw ${esc(def[0].trim())}.</div>`); return; }
  // the last four quarters, summed, for a flow in the quarterly view
  const ttm = FIN_F === "q" && FLOWS.has(k) ? pts.map((p, i) => i >= 3 && !p.est && pts.slice(i - 3, i + 1).every(x => !x.est && x.v != null)
    && (new Date(pts[i].end) - new Date(pts[i - 3].end)) / 864e5 < 300 ? pts.slice(i - 3, i + 1).reduce((a, x) => a + x.v, 0) : null) : [];
  const W = 900, H = 230, P = { l: 56, r: 56, t: 14, b: 26 };
  const vals = pts.map(p => p.v).concat(ttm.filter(v => v != null).map(v => v / 4));
  const mx = Math.max(0, ...vals), mn = Math.min(0, ...vals);
  const bw = (W - P.l - P.r) / pts.length;
  const Y = v => P.t + (1 - (v - mn) / ((mx - mn) || 1)) * (H - P.t - P.b);
  const gs = pts.map(p => p.yoy).filter(v => v != null);
  const gmx = Math.max(5, ...gs.map(Math.abs));
  const G = v => P.t + (1 - (v + gmx) / (2 * gmx)) * (H - P.t - P.b);
  const fmt = v => t === "big" ? big(v, fc) : t === "pl" ? pl(v, 1) : t === "pct" ? pc(v, 1) : t === "x" ? nf(v, 1) + "×" : nf(v, t === "eps" ? 2 : 0);
  const bars = t === "big" || t === "eps" || t === "num" || t === "days";
  const X = i => P.l + i * bw + bw / 2;
  const line = arr => arr.map((v, i) => v == null ? null : `${X(i).toFixed(1)},${v.toFixed(1)}`).filter(Boolean).join(" ");
  setHTML("finDet", `<figure class="mini wide fdet"><figcaption><b>${esc(def[0].trim())}</b>
      ${bars ? `<span class="lgd"><i class="swatch" style="background:var(--accent)"></i>each ${FIN_F === "q" ? "quarter" : "year"}</span>` : ""}
      ${ests.some(e => e[k] != null) ? `<span class="lgd"><i class="swatch" style="background:var(--accent-dim)"></i>forecast</span>` : ""}
      ${gs.length ? `<span class="lgd"><i class="swatch" style="background:var(--warn)"></i>growth y/y, right axis</span>` : ""}
      ${ttm.some(v => v != null) ? `<span class="lgd"><i class="swatch" style="background:var(--ink)"></i>last four quarters ÷ 4</span>` : ""}
      <button class="x" id="finDetX" title="close">×</button></figcaption>
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(def[0].trim())} over time">
      <line x1="${P.l}" x2="${W - P.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--rule-strong)"/>
      ${[mn, (mn + mx) / 2, mx].map(v => `<text x="${P.l - 4}" y="${Y(v) + 3}" text-anchor="end" class="ax">${fmt(v)}</text>`).join("")}
      ${gs.length ? [-gmx, 0, gmx].map(v => `<text x="${W - P.r + 4}" y="${G(v) + 3}" class="ax">${pc(v, 0)}</text>`).join("") : ""}
      ${bars ? pts.map((p, i) => { const y = Y(Math.max(p.v, 0)), h = Math.abs(Y(p.v) - Y(0));
        return `<rect x="${P.l + i * bw + 3}" y="${y}" width="${Math.max(1, bw - 6)}" height="${Math.max(1, h)}" rx="1.5" fill="${p.v < 0 ? "var(--loss)" : p.est ? "var(--accent-dim)" : "var(--accent)"}"${p.prov ? ` stroke="var(--warn)"` : ""}><title>${esc(p.l)}: ${fmt(p.v)}${p.yoy != null ? " · " + pc(p.yoy, 1) + " y/y" : ""}${p.est ? " · forecast" : ""}</title></rect>`; }).join("")
        : `<polyline points="${line(pts.map(p => Y(p.v)))}" fill="none" stroke="var(--accent)" stroke-width="2"/>${pts.map((p, i) => `<circle cx="${X(i)}" cy="${Y(p.v)}" r="4" fill="${p.est ? "var(--accent-dim)" : "var(--accent)"}"><title>${esc(p.l)}: ${fmt(p.v)}</title></circle>`).join("")}`}
      ${ttm.some(v => v != null) ? `<polyline points="${line(ttm.map(v => v == null ? null : Y(v / 4)))}" fill="none" stroke="var(--ink)" stroke-width="1.6" stroke-dasharray="5 3"/>` : ""}
      ${gs.length ? `<polyline points="${line(pts.map(p => p.yoy == null ? null : G(p.yoy)))}" fill="none" stroke="var(--warn)" stroke-width="1.6"/>` : ""}
      ${pts.map((p, i) => (pts.length - 1 - i) % Math.ceil(pts.length / 12) === 0 ? `<text x="${X(i)}" y="${H - 6}" text-anchor="middle" class="ax">${esc(p.l.replace(" FY", "·"))}</text>` : "").join("")}
    </svg></figure>`);
  const x = $("#finDetX"); if (x) x.onclick = () => { FIN_K = null; renderFin(); };
}

/* Three small charts, one measure each: no chart here has two y-axes. */
function finCharts(rows, fc) {
  const W = 300, H = 120, P = { l: 4, r: 4, t: 14, b: 18 };
  const n = rows.length; if (n < 2) return "";
  const bw = (W - P.l - P.r) / n;
  const barsSvg = (title, key, color, fmt, gkey) => {
    const vals = rows.map(r => r[key]);
    const mx0 = Math.max(0, ...vals.filter(v => v != null)), mn0 = Math.min(0, ...vals.filter(v => v != null));
    const Y = v => P.t + (1 - (v - mn0) / ((mx0 - mn0) || 1)) * (H - P.t - P.b);
    return `<figure class="mini"><figcaption>${title}</figcaption><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(title)}">
      <line x1="${P.l}" x2="${W - P.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--rule-strong)"/>
      ${rows.map((r, i) => { const v = r[key]; if (v == null) return ""; const y = Y(Math.max(v, 0)), h = Math.abs(Y(v) - Y(0));
        return `<rect x="${P.l + i * bw + 2}" y="${y}" width="${Math.max(1, bw - 4)}" height="${Math.max(1, h)}" rx="1.5" fill="${v < 0 ? "var(--loss)" : color}"><title>${esc(r.label)}: ${fmt(v)}${gkey && r[gkey] != null ? " · " + pc(r[gkey], 1) + " y/y" : ""}</title></rect>`; }).join("")}
      ${rows.map((r, i) => (n - 1 - i) % Math.ceil(n / 6) === 0 ? `<text x="${P.l + i * bw + bw / 2}" y="${H - 5}" text-anchor="middle" class="ax">${esc(r.label.replace(" FY", "·"))}</text>` : "").join("")}
      <text x="${W - P.r}" y="10" text-anchor="end" class="ax">${fmt(vals[n - 1])}${gkey && rows[n - 1][gkey] != null ? " · " + pc(rows[n - 1][gkey], 0) : ""}</text>
    </svg></figure>`;
  };
  const lines = (title, keys) => {
    const all = keys.flatMap(([k]) => rows.map(r => r[k])).filter(v => v != null);
    if (!all.length) return "";
    const mx0 = Math.max(...all, 0), mn0 = Math.min(...all, 0);
    const Y = v => P.t + (1 - (v - mn0) / ((mx0 - mn0) || 1)) * (H - P.t - P.b);
    const X = i => P.l + i * bw + bw / 2;
    return `<figure class="mini"><figcaption>${title} ${keys.map(([k, lab, c]) => `<span class="lgd"><i class="swatch" style="background:${c}"></i>${lab}</span>`).join("")}</figcaption>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="${esc(title)}">
      <line x1="${P.l}" x2="${W - P.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--rule-strong)"/>
      ${keys.map(([k, lab, c]) => { const pts = rows.map((r, i) => r[k] == null ? null : `${X(i).toFixed(1)},${Y(r[k]).toFixed(1)}`).filter(Boolean).join(" ");
        return `<polyline points="${pts}" fill="none" stroke="${c}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
          ${rows.map((r, i) => r[k] == null ? "" : `<circle cx="${X(i)}" cy="${Y(r[k])}" r="6" fill="transparent"><title>${esc(r.label)} ${lab}: ${pl(r[k], 1)}</title></circle>`).join("")}`; }).join("")}
      ${rows.map((r, i) => (n - 1 - i) % Math.ceil(n / 6) === 0 ? `<text x="${X(i)}" y="${H - 5}" text-anchor="middle" class="ax">${esc(r.label.replace(" FY", "·"))}</text>` : "").join("")}
    </svg></figure>`;
  };
  return barsSvg("Revenue", "revenue", "var(--accent)", v => big(v, fc), "revenue_yoy")
    + lines("Margins, %", [["gross_margin", "gross", "var(--s1)"], ["op_margin", "operating", "var(--s2)"], ["fcf_margin", "FCF", "var(--s3)"]])
    + barsSvg("Free cash flow", "fcf", "var(--s3)", v => big(v, fc), "fcf_yoy")
    + barsSvg("Capital spending", "capex", "var(--s2)", v => big(v, fc), "capex_yoy");
}

function renderInvest() {
  const f = S.fundamentals, inv = f && f.investments;
  if (!inv) return;
  const rows = (inv.annual || []).slice(-6);
  const i3 = inv.incremental;
  setHTML("invest", `
    <div class="finds">${(inv.findings || []).map(x => `<div class="fd ${x.tone}"><i>${toneIcon(x.tone)}</i><span>${esc(x.text)}</span></div>`).join("") || `<div class="dim">Not enough history to judge.</div>`}</div>
    ${i3 ? `<div class="i3">
      <div><span class="k">Spent, 3 years</span><b>${big(i3.spent_3y, f.currency)}</b><span class="dim">capex + acquisitions</span></div>
      <div><span class="k">Added operating profit</span><b class="${cls(i3.d_nopat)}">${big(i3.d_nopat, f.currency)}</b><span class="dim">after tax, a year, ${esc(i3.from)}→${esc(i3.to)}</span></div>
      <div><span class="k">Return on the increment</span><b class="${i3.roiic >= 15 ? "pos" : i3.roiic < 8 ? "neg" : "warnc"}">${pl(i3.roiic, 0)}</b><span class="dim">Δ profit ÷ Δ invested capital</span></div>
      <div><span class="k">Revenue vs capex, 3y</span><b>${pc(i3.rev_growth_3y, 0)} / ${pc(i3.capex_growth_3y, 0)}</b><span class="dim">growth of each</span></div></div>` : ""}
    ${rows.length ? `<div class="tscroll"><table class="tbl inv"><thead><tr><th></th>${rows.map(r => `<th>${esc(r.label)}</th>`).join("")}</tr></thead><tbody>
      <tr><td>Revenue growth</td>${rows.map(r => `<td class="${cls(r.rev_yoy)}">${pc(r.rev_yoy, 0)}</td>`).join("")}</tr>
      <tr><td>Capex</td>${rows.map(r => `<td>${big(r.capex, f.currency)}</td>`).join("")}</tr>
      <tr><td title="capital spending as a share of revenue">Capex / revenue</td>${rows.map(r => `<td>${pl(r.capex_pct, 0)}</td>`).join("")}</tr>
      <tr><td title="above 1 means building, not maintaining">Capex / depreciation</td>${rows.map(r => `<td>${r.capex_dna ? nf(r.capex_dna, 1) + "×" : "–"}</td>`).join("")}</tr>
      <tr><td>R&amp;D / revenue</td>${rows.map(r => `<td>${pl(r.rnd_pct, 0)}</td>`).join("")}</tr>
      <tr><td title="after-tax operating profit over average invested capital">ROIC</td>${rows.map(r => `<td class="${r.roic >= 15 ? "pos" : r.roic != null && r.roic < 8 ? "neg" : ""}"><b>${pl(r.roic, 0)}</b></td>`).join("")}</tr>
      <tr><td title="free cash flow over net income">FCF conversion</td>${rows.map(r => `<td>${pl(r.fcf_conv, 0)}</td>`).join("")}</tr>
      <tr><td>Revenue / net PP&amp;E</td>${rows.map(r => `<td>${r.asset_turn ? nf(r.asset_turn, 1) + "×" : "–"}</td>`).join("")}</tr>
      <tr><td>Returned to holders</td>${rows.map(r => `<td>${big(r.returned, f.currency)}</td>`).join("")}</tr>
    </tbody></table></div>` : ""}
    ${src("ROIC = operating income × (1 − tax rate) ÷ average of (equity + debt − cash). Rules behind each sentence are in pf/research.py; argue with them there.")}`);
}

/* ---------- business mix -------------------------------------------------------- */
let BIZ_G = null;
function renderBiz() {
  const b = S.business;
  if (!b) { setHTML("biz", failed("business", "Business mix")); return; }
  if (!b.available) { setHTML("biz", `<div class="note"><b>No segment data</b>${esc(b.why)}</div>`); setHTML("bizHint", ""); return; }
  setHTML("bizHint", esc(b.source || ""));
  const groups = b.groups || [];
  if (!BIZ_G || !groups.find(g => g.kind === BIZ_G)) BIZ_G = (groups.find(g => g.kind === "product") || groups[0] || {}).kind;
  const g = groups.find(x => x.kind === BIZ_G);
  const fc = (S.fundamentals || {}).currency;
  const growthOf = m => m.ttm_yoy ?? m.q_yoy ?? m.a_yoy;
  const hasM = g && g.members.some(m => m.q_margin != null || m.a_margin != null);
  const table = g ? `<div class="tscroll"><table class="tbl biz"><thead><tr><th>${esc(g.label)}</th>
      <th title="latest quarter">${esc(g.latest_q_label || "")}</th><th>share</th><th title="latest quarter against the same quarter a year earlier">qtr y/y</th>
      <th title="last four quarters against the four before; the fiscal year where only annual figures exist">TTM y/y</th>
      ${hasM ? "<th>margin</th>" : ""}<th>trend</th></tr></thead><tbody>
    ${g.members.map(m => { const share = g.kind === "other" ? m.q_pct_co ?? m.a_pct_co : m.q_share ?? m.a_share;
      const small = (share || 0) < 1;
      return `<tr class="${small ? "tiny" : ""}"><td>${esc(m.label)}</td>
        <td>${m.q != null ? big(m.q, fc) + (m.q_derived ? "*" : "") : m.a != null ? `<span class="dim">${big(m.a, fc)} FY</span>` : "–"}</td>
        <td>${share != null ? `${bar(share)} <span class="dim">${pl(share, 0)}${g.kind === "other" ? " of co." : ""}</span>` : ""}</td>
        <td class="${cls(m.q_yoy)}">${small ? `<span class="dim">${pc(m.q_yoy, 0)}</span>` : `<b>${pc(m.q_yoy, 1)}</b>`}</td>
        <td class="${cls(m.ttm_yoy ?? m.a_yoy)}">${m.ttm_yoy != null ? pc(m.ttm_yoy, 1) : m.a_yoy != null ? pc(m.a_yoy, 1) + ` <span class="dim">FY</span>` : "–"}</td>
        ${hasM ? `<td>${m.q_margin != null ? pl(m.q_margin, 0) : m.a_margin != null ? pl(m.a_margin, 0) : ""}</td>` : ""}
        <td>${spark((m.q_series || m.a_series || []).map(x => x.v), 80, 20)}</td></tr>`; }).join("")}
    </tbody></table></div>` : "";
  // Growth ranking bars — the one-glance answer to "which part is growing".
  const ranked = (g ? g.members : []).filter(m => growthOf(m) != null && ((g.kind === "other" ? m.q_pct_co : m.q_share ?? m.a_share) || 0) >= 1)
    .sort((a, b) => growthOf(b) - growthOf(a));
  const gmax = Math.max(1, ...ranked.map(m => Math.abs(growthOf(m))));
  const co = ((S.fundamentals || {}).investments || {}).ttm || {};
  const growthBars = ranked.length ? `<div class="gbars">
    ${ranked.map(m => { const v = growthOf(m); return `<div class="gb"><span class="gl">${esc(m.label)}</span>
      <span class="gt"><span class="gz"></span><i style="${v >= 0 ? "left:50%" : "right:50%"};width:${Math.abs(v) / gmax * 50}%" class="${v >= 0 ? "up" : "dn"}"></i></span>
      <span class="gv ${cls(v)}">${pc(v, 0)}</span></div>`; }).join("")}
    ${co.rev_yoy != null ? `<div class="gb co"><span class="gl">whole company</span><span class="gt"><span class="gz"></span><i style="left:50%;width:${Math.abs(co.rev_yoy) / gmax * 50}%" class="cmp"></i></span><span class="gv">${pc(co.rev_yoy, 0)}</span></div>` : ""}
    <div class="src">revenue growth, last four quarters against the four before (a full year where only annual figures exist)</div></div>` : "";

  const bl = b.backlog || [];
  const conc = b.concentration || [];
  const cust = `<div class="grp">Customers &amp; contracted demand</div>
    ${bl.length ? bl.map(x => `<div class="kv"><span>${x.label === "Total" ? "Remaining performance obligations" : "RPO — " + esc(x.label)} <span class="dim">${dmy(x.end)}</span></span>
      <span>${spark(x.series.map(s => s.v), 70, 16)} <b>${big(x.v, fc)}</b> <span class="${cls(x.yoy)}">${pc(x.yoy, 0)} y/y</span></span></div>`).join("")
      + `<div class="src">revenue already contracted but not yet recognised — how much of the future is signed</div>`
      : `<div class="dim">No backlog disclosed.</div>`}
    ${conc.length ? conc.map(c => kv(`${esc(c.who)} <span class="dim">${esc(c.basis)} · ${dmy(c.end)}</span>`, pl(c.pct, 1), c.pct >= 10 ? "warnc" : "")).join("")
      : `<div class="kv"><span>Customer concentration</span><span class="dim">no single customer ≥10% of revenue disclosed</span></div>`}`;
  const geo = groups.find(x => x.kind === "geography");
  const geoLine = geo ? `<div class="kv"><span>Where the customers are</span><span>${geo.members.map(m => `${esc(m.label)} ${pl(m.q_share ?? m.a_share, 0)} <span class="${cls(growthOf(m))}">(${pc(growthOf(m), 0)})</span>`).join(" · ")}</span></div>` : "";
  setHTML("biz", `
    <div class="tabs">${groups.map(x => `<button class="tab${x.kind === BIZ_G ? " on" : ""}" data-bg="${x.kind}">${esc(x.label)}</button>`).join("")}</div>
    ${b.engine ? `<div class="engine"><span>Growth engine</span><b>${esc(b.engine.fastest.label)}</b> <span class="pos">${pc(b.engine.fastest.growth, 0)}</span>
      <span class="dim">· slowest</span> ${esc(b.engine.slowest.label)} <span class="${cls(b.engine.slowest.growth)}">${pc(b.engine.slowest.growth, 0)}</span></div>` : ""}
    ${growthBars}${table}${cust}${geoLine}
    ${src("* derived by subtraction · lines renamed or dropped by the company are left out")}`);
  $$("#biz [data-bg]").forEach(x => x.onclick = () => { BIZ_G = x.dataset.bg; renderBiz(); });
}

/* ---------- street -------------------------------------------------------------- */
let ST_N = 15, ST_LOG = false;
const MARK = { "7daysAgo": "7-day", "30daysAgo": "30-day", "60daysAgo": "60-day", "90daysAgo": "90-day" };
const mon = d => new Date(d + "T12:00:00").toLocaleDateString("en-GB", { month: "short", year: "numeric" });
function renderStreet() {
  const s = S.street;
  if (!s) { setHTML("street", failed("street", "Estimates")); return; }
  const q = (S.overview || {}).quote || {};
  const t = s.targets || {};
  setHTML("streetHint", esc(s.source || ""));
  const per = s.periods.filter(p => p.eps || p.rev);
  // ASML's estimates are in euros beside a dollar price, TSMC's EPS in dollars
  // and its revenue in Taiwan dollars: each column says so when it differs.
  const qc = (S.overview || {}).currency, SC = s.currency || {};
  const ccyTag = c => c && c !== qc ? ` <span class="dim">${esc(c)}</span>` : "";
  const name = p => p.fiscal ? `${esc(p.fiscal)} <span class="dim">· ${p.period === "0q" && s.upcoming && s.upcoming.date ? "reports " + dmy(s.upcoming.date) : mon(p.end)}</span>` : esc(p.label);
  // Years past Yahoo's next one, from Zacks via Nasdaq: fewer analysts, a second line.
  const z = s.zacks, lastEnd = (per.find(p => p.period === "+1y") || {}).end || "";
  const zrows = z && z.years ? z.years.filter(y => y.end && y.end > lastEnd) : [];
  const zline = y => z.same_listing
    ? `<tr class="zk"><td>FY${y.end.slice(2, 4)} <span class="dim">· Zacks</span></td><td>${nf(y.eps, 2)}${qc !== "USD" ? ` <span class="dim">US$</span>` : ""}</td>
        <td class="dim">${y.lo != null ? nf(y.lo, 2) + "–" + nf(y.hi, 2) : ""}</td><td class="dim">${y.n ?? ""}</td><td class="${cls(y.growth)}">${pc(y.growth, 1)}</td><td colspan="5" class="dim l">${y.up || y.down ? `▲${y.up || 0} ▼${y.down || 0} in 4 weeks` : ""}</td></tr>`
    : `<tr class="zk"><td>FY${y.end.slice(2, 4)} <span class="dim">· Zacks, ${esc(z.symbol)}</span></td><td class="dim">–</td><td></td><td class="dim">${y.n ?? ""}</td><td class="${cls(y.growth)}">${pc(y.growth, 1)}</td><td colspan="5" class="dim l">growth of the US line's forecast</td></tr>`;
  const cons = `<div class="tscroll"><table class="tbl cons"><thead><tr><th>Period</th><th>EPS est.${ccyTag(SC.eps)}</th><th>range</th><th>#</th><th>growth</th>
      <th>Revenue est.${ccyTag(SC.rev)}</th><th>growth</th><th title="consensus EPS now against 90 days ago">EPS 90d</th><th title="analysts raising / cutting in the last 30 days">revisions 30d</th><th>trend</th></tr></thead><tbody>
    ${per.map(p => { const e = p.eps || {}, r = p.rev || {}, tr = p.trend || {}, rv = p.revisions || {};
      return `<tr><td>${name(p)}</td><td><b>${e.avg != null ? nf(e.avg, 2) : "–"}</b></td><td class="dim">${e.low != null ? nf(e.low, 2) + "–" + nf(e.high, 2) : ""}</td>
      <td class="dim">${e.n ?? ""}</td><td class="${cls(e.growth)}">${pc(e.growth, 1)}</td><td>${big(r.avg, p.rev_ccy || SC.rev || qc)}</td><td class="${cls(r.growth)}">${pc(r.growth, 1)}</td>
      <td class="${cls(tr.chg_90d)}">${pc(tr.chg_90d, 1)}</td>
      <td>${rv.up30 != null ? `<span class="pos">▲${rv.up30}</span> <span class="neg">▼${rv.down30}</span>` : ""}</td>
      <td>${spark([tr["90daysAgo"], tr["60daysAgo"], tr["30daysAgo"], tr["7daysAgo"], tr.current], 64, 18, (tr.chg_90d || 0) >= 0 ? "var(--gain)" : "var(--loss)")}</td></tr>`; }).join("")}
    ${zrows.map(zline).join("")}
    </tbody></table></div>${s.ltg != null ? `<div class="src">long-term EPS growth estimate ${pc(s.ltg, 1)} a year</div>` : ""}
    ${zrows.length ? `<div class="src">later years: ${esc(z.source)}, ${z.same_listing ? esc(z.per) : "growth only: another listing, another currency"}</div>` : ""}`;

  // After each report: the beat, the next session, five and twenty sessions on.
  const af = s.after || [], L = af[af.length - 1];
  const beats = af.filter(x => (x.surprise_pct || 0) > 0), falls = af.filter(x => (x.move || 0) < 0);
  const fellAfterBeat = beats.filter(x => (x.move || 0) < 0).length;
  const est = L && L.est;
  const head = L ? `<div class="after-head">
      ${est && est.periods.length ? `Since the ${dmy(L.date)} report the forecasts moved ${est.periods.map(e => `<b>${esc(e.fiscal)}</b> <span class="${cls(e.chg)}">${pc(e.chg, 1)}</span>`).join(" · ")}
        <span class="dim">(against Yahoo's ${MARK[est.mark] || est.mark} mark, ${est.days} days back)</span>; ` : `Last report ${dmy(L.date)}: `}
      the stock <span class="${cls(L.move)}">${pc(L.move, 1)}</span> the next session${L.since != null ? `, <span class="${cls(L.since)}">${pc(L.since, 1)}</span> since` : ""}.
      <span class="dim">Beat ${beats.length} of the last ${af.length}, and fell the next session after ${fellAfterBeat} of those beats: the beat is rarely what moves it.</span></div>` : "";
  const revSur = revenueSurprises(af);
  const afterTbl = af.length ? `<div class="tscroll"><table class="tbl after"><thead><tr><th>Report</th><th>EPS vs est.</th><th>surprise</th>
      ${revSur.any ? `<th title="revenue against the consensus the day before, from the record">revenue</th>` : ""}
      <th>next session</th><th>5 sessions</th><th>20 sessions</th><th>since</th></tr></thead><tbody>
    ${af.slice().reverse().map(x => `<tr><td>${dmy(x.date)}</td><td>${x.eps_act != null ? nf(x.eps_act, 2) : "–"} <span class="dim">vs ${x.eps_est != null ? nf(x.eps_est, 2) : "–"}</span></td>
      <td class="${cls(x.surprise_pct)}">${pc(x.surprise_pct, 1)}</td>
      ${revSur.any ? `<td class="${cls(revSur[x.date])}">${revSur[x.date] != null ? pc(revSur[x.date], 1) : `<span class="dim">–</span>`}</td>` : ""}
      <td class="${cls(x.move)}"><b>${pc(x.move, 1)}</b></td><td class="${cls(x.d5)}">${x.d5 != null ? pc(x.d5, 1) : `<span class="dim">${x.sessions} so far</span>`}</td>
      <td class="${cls(x.d20)}">${x.d20 != null ? pc(x.d20, 1) : ""}</td><td class="${cls(x.since)}">${pc(x.since, 1)}</td></tr>`).join("")}
    </tbody></table></div>
    ${src("moves from the close before the report's session; " + (revSur.any ? "revenue beats from the consensus record" : "revenue beats start with the consensus record: from the next report on"))}` : "";

  // Surprises: bars for the surprise, dots for the stock's reaction — both in %.
  const su = (s.surprises || []).slice(-12);
  let sur = "";
  if (su.length) {
    const W = 520, H = 150, P = { l: 30, r: 6, t: 10, b: 30 };
    const vals = su.flatMap(x => [x.surprise_pct, (x.reaction || {}).move]).filter(v => v != null);
    const mx0 = Math.max(2, ...vals), mn0 = Math.min(-2, ...vals);
    const Y = v => P.t + (1 - (v - mn0) / (mx0 - mn0)) * (H - P.t - P.b);
    const bw = (W - P.l - P.r) / su.length;
    const beat = su.filter(x => (x.surprise_pct || 0) > 0).length;
    const avgR = su.map(x => (x.reaction || {}).move).filter(v => v != null);
    sur = `<figure class="mini wide"><figcaption>Earnings surprises and the next session
        <span class="lgd"><i class="swatch" style="background:var(--accent)"></i>EPS surprise %</span>
        <span class="lgd"><i class="dot up"></i><i class="dot dn"></i>stock reaction %</span>
        <span class="dim">beat ${beat}/${su.length} · average absolute reaction ${pl(avgR.reduce((a, b) => a + Math.abs(b), 0) / (avgR.length || 1), 1)}</span></figcaption>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="earnings surprises">
      ${[mn0, 0, mx0].map(v => `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="${v === 0 ? "var(--rule-strong)" : "var(--rule)"}"/><text x="${P.l - 4}" y="${Y(v) + 3}" text-anchor="end" class="ax">${v === 0 ? "0" : pc(v, 0)}</text>`).join("")}
      ${su.map((x, i) => { const v = x.surprise_pct, rx = (x.reaction || {}).move, cx = P.l + i * bw + bw / 2;
        return `${v != null ? `<rect x="${cx - bw * .3}" y="${Y(Math.max(v, 0))}" width="${bw * .6}" height="${Math.max(1, Math.abs(Y(v) - Y(0)))}" rx="2" fill="${v >= 0 ? "var(--accent)" : "var(--loss)"}" opacity=".85"><title>${dmy(x.date)}: EPS ${x.eps_act} vs ${x.eps_est}${SC.hist && SC.hist !== qc ? " " + SC.hist : ""} (${pc(v, 1)})</title></rect>` : ""}
          ${rx != null ? `<circle cx="${cx}" cy="${Y(rx)}" r="5" fill="${rx >= 0 ? "var(--gain)" : "var(--loss)"}" stroke="var(--surface)" stroke-width="2"><title>stock ${pc(rx, 1)} the next session (${dmy(x.reaction.day)})</title></circle>` : ""}
          <text x="${cx}" y="${H - 16}" text-anchor="middle" class="ax">${x.date.slice(2, 7).replace("-", "/")}</text>
          <text x="${cx}" y="${H - 5}" text-anchor="middle" class="ax ${rx >= 0 ? "pos" : "neg"}">${rx != null ? (rx >= 0 ? "+" : "") + rx.toFixed(0) : ""}</text>`; }).join("")}
      </svg></figure>`;
  }

  // Targets: the range, the mean, and every firm's standing target as a tick.
  const br = s.brokers || [];
  const ticks = br.filter(b => b.pt);
  const tgt = t.low && t.high && q.price ? (() => {
    const all = [t.low, t.high, q.price, ...ticks.map(b => b.pt)];
    const lo = Math.min(...all) * .97, hi = Math.max(...all) * 1.03, X = v => (v - lo) / (hi - lo) * 100;
    const nT = (S.overview || {}).analyst?.n || s.n_targets;
    return `<div class="tgt"><div class="tk">Price targets <span class="dim">(${nT ? nT + " analysts" : "analyst count not given"}${qc ? ", " + esc(qc) : ""}${ticks.length ? ` · ticks: ${ticks.filter(b => !b.stale).length} firms' targets, faint when older than six months` : ""})</span></div>
      <div class="ttrack"><span class="tr" style="left:${X(t.low)}%;width:${X(t.high) - X(t.low)}%"></span>
        ${ticks.map(b => `<i class="mk firm${b.stale ? " stale" : ""}" style="left:${X(b.pt)}%" title="${esc(b.firm)}: ${nf(b.pt, 0)} (${dmy(b.date)})"></i>`).join("")}
        <i class="mk now" style="left:${X(q.price)}%" title="price ${nf(q.price, 2)}"></i>
        <i class="mk mean" style="left:${X(t.mean)}%" title="mean ${nf(t.mean, 2)}"></i></div>
      <div class="tlab"><span>low ${nf(t.low, 0)}</span><span>price <b>${nf(q.price, 2)}</b></span><span>mean <b>${nf(t.mean, 0)}</b> <span class="${cls(t.mean - q.price)}">${pc((t.mean / q.price - 1) * 100, 0)}</span></span><span>high ${nf(t.high, 0)}</span></div></div>`;
  })() : "";
  const recs = (s.recommendations || []).slice().reverse();
  const REC = [["strongBuy", "strong buy", "var(--gain)"], ["buy", "buy", "rgba(63,179,128,.55)"], ["hold", "hold", "var(--ink-soft)"], ["sell", "sell", "rgba(224,100,79,.55)"], ["strongSell", "strong sell", "var(--loss)"]];
  const ratings = recs.length ? `<div class="ratings"><div class="tk">Ratings, by month</div>${recs.map(r => { const tot = REC.reduce((a, [k]) => a + (r[k] || 0), 0) || 1;
    return `<div class="rt"><span class="dim">${r.period === "0m" ? "now" : r.period.replace("-", "") + " ago"}</span><span class="rbar2">${REC.map(([k, lab, c]) => r[k] ? `<i style="width:${r[k] / tot * 100}%;background:${c}" title="${lab}: ${r[k]}"></i>` : "").join("")}</span>
      <span class="dim">${r.strongBuy + r.buy} buy · ${r.hold} hold · ${r.sell + r.strongSell} sell</span></div>`; }).join("")}
    <div class="legend">${REC.map(([, lab, c]) => `<span><i class="swatch" style="background:${c};height:8px"></i>${lab}</span>`).join("")}</div></div>` : "";

  // The consensus target rebuilt from the actions, against the price.
  const tp = s.target_path || [];
  const pathFig = tp.length >= 4 ? (() => {
    const W = 520, H = 170, P = { l: 40, r: 8, t: 10, b: 20 };
    const vs = tp.flatMap(x => [x.lo, x.hi, x.price]).filter(v => v != null);
    const lo = Math.min(...vs) * .95, hi = Math.max(...vs) * 1.03;
    const X = i => P.l + i / (tp.length - 1) * (W - P.l - P.r), Y = v => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);
    const line = k => tp.map((x, i) => x[k] == null ? null : `${X(i).toFixed(1)},${Y(x[k]).toFixed(1)}`).filter(Boolean).join(" ");
    const band = tp.map((x, i) => `${X(i).toFixed(1)},${Y(x.hi).toFixed(1)}`).join(" ") + " " + tp.slice().reverse().map((x, j) => `${X(tp.length - 1 - j).toFixed(1)},${Y(x.lo).toFixed(1)}`).join(" ");
    const a = tp[0], z = tp[tp.length - 1];
    const tch = (z.mean / a.mean - 1) * 100, pch = a.price && z.price ? (z.price / a.price - 1) * 100 : null;
    const yrs = []; let ly = null;
    tp.forEach((x, i) => { const m = x.d.slice(0, 7); if (m.slice(5) === "01" && m !== ly) { yrs.push([i, m.slice(0, 4)]); ly = m; } });
    return `<figure class="mini wide"><figcaption>Consensus target and the price
        <span class="lgd"><i class="swatch" style="background:var(--warn)"></i>mean target</span><span class="lgd"><i class="swatch" style="background:var(--ink)"></i>price</span>
        <span class="lgd"><i class="swatch" style="background:var(--accent-dim)"></i>lowest–highest</span>
        <span class="dim">since ${dmy(a.d)}: targets <b class="${cls(tch)}">${pc(tch, 0)}</b>, price <b class="${cls(pch)}">${pc(pch, 0)}</b>${pch != null ? (Math.abs(tch - pch) < 5 ? " — targets moved with the price" : tch > pch ? " — targets rose more than the price" : " — the price ran ahead of the targets") : ""}</span></figcaption>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="consensus target against the price">
        ${[lo, (lo + hi) / 2, hi].map(v => `<text x="${P.l - 4}" y="${Y(v) + 3}" text-anchor="end" class="ax">${nf(v, 0)}</text>`).join("")}
        ${yrs.map(([i, y]) => `<line x1="${X(i)}" x2="${X(i)}" y1="${P.t}" y2="${H - P.b}" stroke="var(--rule)"/><text x="${X(i)}" y="${H - 5}" text-anchor="middle" class="ax">${y}</text>`).join("")}
        <polygon points="${band}" fill="var(--accent-dim)" opacity=".45"/>
        <polyline points="${line("mean")}" fill="none" stroke="var(--warn)" stroke-width="2" stroke-linejoin="round"/>
        <polyline points="${line("price")}" fill="none" stroke="var(--ink)" stroke-width="1.6" stroke-linejoin="round"/>
        ${tp.map((x, i) => `<rect x="${X(i) - (W - P.l - P.r) / tp.length / 2}" y="${P.t}" width="${(W - P.l - P.r) / tp.length}" height="${H - P.t - P.b}" fill="transparent"><title>${dmy(x.d)}: mean target ${nf(x.mean, 0)} (${x.n} firms, ${nf(x.lo, 0)}–${nf(x.hi, 0)}), price ${nf(x.price, 2)}</title></rect>`).join("")}
      </svg><figcaption class="dim">rebuilt from ${s.n_actions || "the"} analyst actions: each firm's latest target, for up to a year</figcaption></figure>`;
  })() : `<div class="dim" style="padding:1rem 0">Too few analyst targets with dates to rebuild a path.</div>`;

  // The consensus as this terminal recorded it, day by day (pf/consensus.py).
  const rec = recordFig();

  // One row per firm: its latest view and standing target.
  const cut90 = new Date(Date.now() - 90 * 864e5).toISOString().slice(0, 10);
  const acts = s.actions || [];
  const recent = acts.filter(a => a.date >= cut90);
  const ptUp = recent.filter(a => a.pt && a.pt_prior && a.pt > a.pt_prior).length, ptDn = recent.filter(a => a.pt && a.pt_prior && a.pt < a.pt_prior).length;
  const ACT = { up: ["upgrade", "pos"], down: ["downgrade", "neg"], init: ["initiated", "accent"], main: ["maintained", "dim"], reit: ["reiterated", "dim"] };
  const brTbl = br.length ? `<div class="tscroll"><table class="tbl brk"><thead><tr><th class="l">Firm</th><th class="l">Rating</th><th>Target</th><th>vs before</th><th>vs price</th><th>Date</th></tr></thead><tbody>
    ${br.slice(0, ST_N).map(b => { const ch = b.pt && b.pt_prior ? (b.pt / b.pt_prior - 1) * 100 : null, up = b.pt && q.price ? (b.pt / q.price - 1) * 100 : null, [lab, c] = ACT[b.action] || [b.action || "", ""];
      return `<tr class="${b.stale ? "stale" : ""}" title="${b.stale ? "older than six months" : ""}"><td class="l">${esc(b.firm)}</td>
        <td class="l">${b.from && b.from !== b.rating ? `<span class="dim">${esc(b.from)} →</span> ` : ""}${esc(b.rating || "")}${b.action === "up" || b.action === "down" || b.action === "init" ? ` <span class="${c === "accent" ? "" : c}">${esc(lab)}</span>` : ""}</td>
        <td>${b.pt ? `<b>${nf(b.pt, 0)}</b>` : "–"}</td><td class="${cls(ch)}">${ch ? pc(ch, 0) : ""}</td><td class="${cls(up)}">${pc(up, 0)}</td>
        <td>${dmy(b.date)} <span class="dim">${b.age < 1 ? "today" : b.age + "d"}</span></td></tr>`; }).join("")}</tbody></table></div>
    ${br.length > ST_N ? `<button class="tbtn more" id="stMore">All ${br.length} firms</button>` : ""}` : "";
  const log = ST_LOG && acts.length ? `<div class="tscroll"><table class="tbl acts"><thead><tr><th>Date</th><th class="l">Firm</th><th class="l">Action</th><th class="l">Rating</th><th>Target</th><th>change</th></tr></thead><tbody>
    ${acts.map(a => { const [lab, c] = ACT[a.action] || [a.action, ""], ch = a.pt && a.pt_prior ? (a.pt / a.pt_prior - 1) * 100 : null;
      return `<tr><td>${dmy(a.date)}</td><td class="l">${esc(a.firm)}</td><td class="l ${c === "accent" ? "" : c}">${esc(lab)}</td>
        <td class="l">${a.from && a.from !== a.to ? `<span class="dim">${esc(a.from)} →</span> ` : ""}${esc(a.to || "")}</td>
        <td>${a.pt ? (a.pt_prior && a.pt_prior !== a.pt ? `<span class="dim">${nf(a.pt_prior, 0)} →</span> ` : "") + `<b>${nf(a.pt, 0)}</b>` : "–"}</td>
        <td class="${cls(ch)}">${ch ? pc(ch, 0) : ""}</td></tr>`; }).join("")}</tbody></table></div>` : "";
  setHTML("street", `${cons}
    ${head}
    <div class="cols c-2 inner"><div>${sur}</div><div>${tgt}${ratings}</div></div>
    <div class="grp">After each report</div>${afterTbl}
    <div class="cols c-2 inner"><div>${pathFig}</div><div>${rec}</div></div>
    <div class="grp">Analysts, one row per firm <span class="dim">— last 90 days: ${recent.filter(a => a.action === "up").length} upgrades, ${recent.filter(a => a.action === "down").length} downgrades, targets raised ${ptUp}× and cut ${ptDn}×</span>
      ${acts.length ? `<button class="linkish" id="stLog">${ST_LOG ? "hide" : "every action (" + acts.length + ")"}</button>` : ""}</div>
    ${brTbl}${log}`);
  const more = $("#stMore"); if (more) more.onclick = () => { ST_N = 999; renderStreet(); };
  const lg = $("#stLog"); if (lg) lg.onclick = () => { ST_LOG = !ST_LOG; renderStreet(); };
}

/* Revenue against the consensus the day before each report, from the record. */
function revenueSurprises(af) {
  const out = { any: false };
  const rows = ((S.consensus || {}).rows || []), qs = ((S.fundamentals || {}).quarters || []);
  if (!rows.length || !qs.length) return out;
  for (const x of af) {
    const qq = qs.filter(r => r.end < x.date && (new Date(x.date) - new Date(r.end)) / 864e5 < 100).pop();
    const before = rows.filter(r => r.d < x.date && r.rev_0q != null && r.end_0q).pop();
    if (!qq || !before || qq.revenue == null) continue;
    if (Math.abs(new Date(before.end_0q) - new Date(qq.end)) / 864e5 > 10) continue;
    out[x.date] = (qq.revenue / before.rev_0q - 1) * 100;
    out.any = true;
  }
  return out;
}

/* The recorded consensus: this year's and next year's EPS, and the mean target against the price. */
function recordFig() {
  const c = S.consensus;
  if (!c) return ERR.consensus ? `<div class="note"><b>Consensus record unavailable</b>${esc(ERR.consensus)}</div>` : spin;
  const rows = c.rows || [], kept = c.kept || {};
  const intro = `<div class="tk">Recorded consensus <span class="dim">(${kept.companies ? nf(kept.companies, 0) + " companies kept" : "kept"} · each night's scan and each visit here)</span></div>`;
  if (rows.length < 2) return `${intro}<div class="dim" style="font-family:var(--f-ui);font-size:.78rem;line-height:1.5">Recording since ${rows.length ? dmy(rows[0].d) : "today"}: ${rows.length} ${rows.length === 1 ? "day" : "days"} so far.
    A day is kept when the forecasts or targets moved, so this fills in over weeks: then it shows how this year's and next year's EPS forecasts, and the mean target, moved around each report.</div>`;
  const W = 520, H = 140, P = { l: 40, r: 8, t: 8, b: 18 };
  const series = (k, endK) => { const segs = []; let cur = [], lastEnd = null;
    rows.forEach((r, i) => { if (r[k] == null) return; if (endK && r[endK] && lastEnd && r[endK] !== lastEnd) { segs.push(cur); cur = []; } lastEnd = r[endK] || lastEnd; cur.push([i, r[k]]); });
    if (cur.length) segs.push(cur); return segs; };
  const X = i => P.l + i / Math.max(1, rows.length - 1) * (W - P.l - P.r);
  const fig = (title, ks) => {
    const vals = ks.flatMap(([k]) => rows.map(r => r[k])).filter(v => v != null);
    if (!vals.length) return "";
    const lo = Math.min(...vals) * .97, hi = Math.max(...vals) * 1.03, Y = v => P.t + (1 - (v - lo) / ((hi - lo) || 1)) * (H - P.t - P.b);
    return `<figure class="mini wide"><figcaption>${title} ${ks.map(([k, lab, col]) => `<span class="lgd"><i class="swatch" style="background:${col}"></i>${lab}</span>`).join("")}</figcaption>
      <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(title)}">
        ${[lo, hi].map(v => `<text x="${P.l - 4}" y="${Y(v) + 3}" text-anchor="end" class="ax">${nf(v, v < 100 ? 2 : 0)}</text>`).join("")}
        <text x="${P.l}" y="${H - 4}" class="ax">${dmy(rows[0].d)}</text><text x="${W - P.r}" y="${H - 4}" text-anchor="end" class="ax">${dmy(rows[rows.length - 1].d)}</text>
        ${ks.map(([k, , col, endK]) => series(k, endK).map(seg => `<polyline points="${seg.map(([i, v]) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ")}" fill="none" stroke="${col}" stroke-width="1.8" stroke-linejoin="round"/>`).join("")).join("")}
        ${(S.street && S.street.after || []).map(a => { const i = rows.findIndex(r => r.d >= a.date); return i > 0 ? `<line x1="${X(i)}" x2="${X(i)}" y1="${P.t}" y2="${H - P.b}" stroke="var(--rule-strong)" stroke-dasharray="3 3"><title>report ${dmy(a.date)}</title></line>` : ""; }).join("")}
      </svg></figure>`;
  };
  return intro + fig("EPS forecasts" + (rows[rows.length - 1].eps_ccy && rows[rows.length - 1].eps_ccy !== (S.overview || {}).currency ? ` (${esc(rows[rows.length - 1].eps_ccy)})` : ""),
      [["eps_0y", "this year", "var(--s1)", "end_0y"], ["eps_1y", "next year", "var(--s2)", "end_1y"]])
    + fig("Mean target and price", [["tgt_mean", "mean target", "var(--warn)"], ["price", "price", "var(--ink)"]])
    + src(`${rows.length} recorded days since ${dmy(c.first)}${(c.listings || []).length > 1 || (c.listings || [])[0] !== SYM ? " · from " + (c.listings || []).join(", ") : ""}; dashed: reports; a break in a line is a new fiscal year`);
}

/* ---------- valuation -------------------------------------------------------------- */
let VAL_K = "pe", VAL_W = "10y";
const VAL_LABEL = { pe: "P/E (trailing)", ev_ebit: "EV/EBIT", ps: "Price/sales", ev_sales: "EV/sales", pfcf: "Price/FCF", div_yield: "Dividend yield" };
function renderVal() {
  const v = S.valuation;
  if (!v) { setHTML("val", failed("valuation", "Valuation")); return; }
  setHTML("valHint", esc(v.reported_currency && v.reported_currency !== v.currency ? `${v.reported_currency} figures in ${v.currency}` : (v.series ? "against its own history" : "")));
  const c = v.current || {};
  const stats = v.stats || {};
  const table = Object.keys(VAL_LABEL).filter(k => stats[k] && stats[k]["5y"]).map(k => {
    const d = stats[k]["5y"], dd = stats[k]["10y"] || {};
    const X = x => (x - d.min) / ((d.max - d.min) || 1) * 100;
    const fmt = k === "div_yield" ? x => pl(x, 2) : x => nf(x, 1);
    return `<tr class="${k === VAL_K ? "on" : ""}" data-vk="${k}"><td>${VAL_LABEL[k]}</td><td><b>${fmt(d.current)}</b></td>
      <td class="dim">${fmt(d.min)}</td><td>${fmt(d.median)}</td><td class="dim">${fmt(d.max)}</td>
      <td><span class="prange" title="five-year range; the band is the middle half"><span class="pb" style="left:${X(d.p25)}%;width:${X(d.p75) - X(d.p25)}%"></span><span class="pm" style="left:${X(d.median)}%"></span><i style="left:${X(d.current)}%"></i></span></td>
      <td class="${k === "div_yield" ? cls(d.vs_median) : cls(-d.vs_median)}">${pc(d.vs_median, 0)}</td><td>${nf(d.pct_rank, 0)}th</td><td class="dim">${dd.median != null ? fmt(dd.median) : ""}</td></tr>`;
  }).join("");
  const peers = S.peers && S.peers.median;
  const box = `<div class="vctx">
    ${kv("Forward P/E", c.pe_fwd ? nf(c.pe_fwd, 1) : "–", "", "price over next-twelve-month consensus EPS")}
    ${kv("P/E on this year / next year", `${c.pe_cur_fy ? nf(c.pe_cur_fy, 1) : "–"} / ${c.pe_next_fy ? nf(c.pe_next_fy, 1) : "–"}`)}
    ${kv("PEG (next year)", c.peg ? nf(c.peg, 2) : "–", c.peg && c.peg < 1.2 ? "pos" : c.peg > 2.5 ? "neg" : "", "forward P/E over next year's expected EPS growth")}
    ${kv("EV/EBITDA", c.ev_ebitda ? nf(c.ev_ebitda, 1) : "–")}
    ${kv("FCF yield", pl(c.fcf_yield, 2))}
    ${kv("Earnings yield vs 10y Treasury", c.earnings_yield != null ? `${pl(c.earnings_yield, 2)} vs ${pl(c.treasury_10y, 2)} <span class="${cls(c.spread_vs_10y)}">(${pp(c.spread_vs_10y, 2)})</span>` : "–", "", "1 / P/E against the risk-free rate; negative means you are paid less than a Treasury today and are relying on growth")}
    ${peers ? kv("Peers' median fwd P/E · EV/EBITDA", `${nf(peers.pe_fwd, 1)} · ${nf(peers.ev_ebitda, 1)}`) : ""}
    <div class="grp">What the price assumes</div>
    <div class="dcf">
      <label>FCF now <input id="dcfF" value="${c.fcf_ttm ? (c.fcf_ttm / 1e9).toFixed(1) : ""}" size="6"> B</label>
      <label>discount <input id="dcfR" value="9" size="3">%</label>
      <label>terminal <input id="dcfG" value="3" size="3">%</label>
      <label>years <input id="dcfN" value="10" size="3"></label>
      <div id="dcfOut" class="dcfo"></div></div>
    ${src("Reverse DCF: the constant free-cash-flow growth rate at which discounted cash flows equal today's enterprise value. Change the inputs; nothing is saved.")}
    ${src("Bands: " + (v.source || ""))}</div>`;
  setHTML("val", `
    <div class="cols c-val inner"><div>
      <div class="fbar">${Object.keys(VAL_LABEL).filter(k => stats[k] && stats[k]["5y"]).map(k => `<button class="seg${k === VAL_K ? " on" : ""}" data-vk="${k}">${VAL_LABEL[k]}</button>`).join("")}
        <span class="sp"></span>${["5y", "10y"].map(w => `<button class="seg${w === VAL_W ? " on" : ""}" data-vw="${w}">${w.toUpperCase()}</button>`).join("")}</div>
      ${v.series ? valChart(v, VAL_K, VAL_W) : `<div class="dim" style="padding:2rem 0">No filing history to draw a range from — the current multiples are on the right.</div>`}
      ${table ? `<div class="tscroll"><table class="tbl vtab"><thead><tr><th>Multiple</th><th>now</th><th>5y low</th><th>median</th><th>high</th><th>where it sits</th><th>vs median</th><th>percentile</th><th>10y median</th></tr></thead><tbody>${table}</tbody></table></div>` : ""}
    </div>${box}</div>`);
  $$("#val [data-vk]").forEach(b => b.onclick = () => { VAL_K = b.dataset.vk; renderVal(); });
  $$("#val [data-vw]").forEach(b => b.onclick = () => { VAL_W = b.dataset.vw; renderVal(); });
  ["dcfF", "dcfR", "dcfG", "dcfN"].forEach(id => { const el = $("#" + id); if (el) el.oninput = dcf; });
  dcf();
}
function dcf() {
  const c = (S.valuation || {}).current || {};
  const F = parseFloat(($("#dcfF") || {}).value) * 1e9, r = parseFloat(($("#dcfR") || {}).value) / 100,
    g = parseFloat(($("#dcfG") || {}).value) / 100, N = parseInt(($("#dcfN") || {}).value);
  const out = $("#dcfOut"); if (!out) return;
  if (!(F > 0) || !(r > g) || !(N > 0) || !c.ev) { out.innerHTML = `<span class="dim">needs positive free cash flow and discount > terminal</span>`; return; }
  const val = gr => { let pv = 0, f = F; for (let t = 1; t <= N; t++) { f *= 1 + gr; pv += f / Math.pow(1 + r, t); } return pv + f * (1 + g) / (r - g) / Math.pow(1 + r, N); };
  let lo = -0.5, hi = 1.5;
  if (val(hi) < c.ev) { out.innerHTML = "implied growth above 150% a year"; return; }
  for (let i = 0; i < 80; i++) { const m = (lo + hi) / 2; val(m) < c.ev ? lo = m : hi = m; }
  const ig = (lo + hi) / 2 * 100;
  const cons = c.rev_growth_next_fy;
  const fair = gr => (val(gr) - (c.net_debt || 0)) / (c.shares || 1) / (c.unit || 1);
  out.innerHTML = `The market price needs <b>${nf(ig, 1)}%</b> a year of FCF growth for ${N} years.
    ${cons != null ? `<br>Consensus revenue growth next year: ${pc(cons, 1)}.` : ""}
    <br><span class="dim">Per share at 10% growth: ${nf(fair(0.10), 0)} · 15%: ${nf(fair(0.15), 0)} · 20%: ${nf(fair(0.20), 0)} (price ${nf(c.price, 2)})</span>`;
}
function valChart(v, k, win) {
  const s = v.series, cut = new Date(Date.now() - (win === "5y" ? 1826 : 3653) * 864e5).toISOString().slice(0, 10);
  const idx = s.d.map((d, i) => d >= cut && s[k][i] != null ? i : -1).filter(i => i >= 0);
  if (idx.length < 10) return `<div class="dim">Too little history for ${VAL_LABEL[k]}.</div>`;
  const vals = idx.map(i => s[k][i]);
  const st = (v.stats[k] || {})[win] || (v.stats[k] || {})["5y"];
  const W = 640, H = 200, P = { l: 36, r: 60, t: 10, b: 20 };
  const lo = Math.min(...vals) * .95, hi = Math.max(...vals) * 1.05;
  const X = j => P.l + j / (idx.length - 1) * (W - P.l - P.r), Y = y => P.t + (1 - (y - lo) / (hi - lo)) * (H - P.t - P.b);
  const pts = vals.map((y, j) => `${X(j).toFixed(1)},${Y(y).toFixed(1)}`).join(" ");
  const fmt = k === "div_yield" ? x => pl(x, 2) : x => nf(x, 1);
  const yrs = []; let ly = null;
  idx.forEach((i, j) => { const y = s.d[i].slice(0, 4); if (y !== ly) { if (ly !== null) yrs.push([j, y]); ly = y; } });
  const last = vals[vals.length - 1];
  return `<figure class="mini wide vchart"><svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(VAL_LABEL[k])} history">
    ${st ? `<rect x="${P.l}" width="${W - P.l - P.r}" y="${Y(st.p75)}" height="${Math.max(1, Y(st.p25) - Y(st.p75))}" fill="var(--accent)" opacity=".10"><title>middle half of the range: ${fmt(st.p25)}–${fmt(st.p75)}</title></rect>
      <line x1="${P.l}" x2="${W - P.r}" y1="${Y(st.median)}" y2="${Y(st.median)}" stroke="var(--accent)" stroke-dasharray="4 4"/>
      <text x="${W - P.r + 4}" y="${Y(st.median) + 3}" class="ax">median ${fmt(st.median)}</text>` : ""}
    ${yrs.map(([j, y]) => `<text x="${X(j)}" y="${H - 5}" text-anchor="middle" class="ax">${y}</text><line x1="${X(j)}" x2="${X(j)}" y1="${P.t}" y2="${H - P.b}" stroke="var(--rule)"/>`).join("")}
    ${[lo, (lo + hi) / 2, hi].map(y => `<text x="${P.l - 4}" y="${Y(y) + 3}" text-anchor="end" class="ax">${fmt(y)}</text>`).join("")}
    <polyline points="${pts}" fill="none" stroke="var(--ink)" stroke-width="1.8" stroke-linejoin="round"/>
    <circle cx="${X(vals.length - 1)}" cy="${Y(last)}" r="4" fill="var(--warn)" stroke="var(--surface)" stroke-width="2"/>
    <text x="${W - P.r + 4}" y="${Y(last) + 3}" class="ax" fill="var(--warn)" style="fill:var(--warn)">now ${fmt(last)}</text>
    ${idx.map((i, j) => j % 4 ? "" : `<rect x="${X(j) - 3}" y="${P.t}" width="${(W - P.l - P.r) / idx.length * 4}" height="${H - P.t - P.b}" fill="transparent"><title>${dmy(s.d[i])}: ${fmt(s[k][i])} at price ${nf(s.c[i], 2)}</title></rect>`).join("")}
  </svg><figcaption>${esc(VAL_LABEL[k])}, weekly · shaded: the middle half of the ${win} range · dashed: median</figcaption></figure>`;
}

/* ---------- peers ------------------------------------------------------------------- */
let PR_SORT = "market_cap", PR_DIR = -1;
const PR_COLS = [["market_cap", "Mkt cap", v => big(v)], ["change_pct", "1D", v => pc(v, 1), 1], ["pe_fwd", "Fwd P/E", v => v ? nf(v, 1) : "–"],
  ["ev_ebitda", "EV/EBITDA", v => v ? nf(v, 1) : "–"], ["ps", "P/S", v => v ? nf(v, 1) : "–"], ["rev_growth", "Rev growth", v => pc(v, 0), 1],
  ["gross_margin", "Gross m.", v => pl(v, 0)], ["op_margin", "Op. m.", v => pl(v, 0)], ["roe", "ROE", v => pl(v, 0)], ["fcf_yield", "FCF yld", v => pl(v, 1)],
  ["r1m", "1M", v => pc(v, 0), 1], ["rytd", "YTD", v => pc(v, 0), 1], ["r1y", "1Y", v => pc(v, 0), 1], ["corr", "Corr", v => v == null ? "–" : nf(v, 2)], ["upside", "Target", v => pc(v, 0), 1]];
function renderPeers() {
  const p = S.peers;
  if (!p) { setHTML("peers", failed("peers", "Peers")); return; }
  setHTML("peersHint", `${esc(p.source)} · click a column to sort · correlation of daily returns over a year`);
  const rows = p.rows.slice().sort((a, b) => ((b[PR_SORT] ?? -1e18) - (a[PR_SORT] ?? -1e18)) * (PR_DIR < 0 ? 1 : -1));
  const me = p.rows.find(r => r.self) || {};
  setHTML("peers", `<div class="tscroll"><table class="tbl peers"><thead><tr><th>Symbol</th><th class="l">Name</th><th>Price</th>
      ${PR_COLS.map(([k, lab]) => `<th class="sort${PR_SORT === k ? " on" : ""}" data-ps="${k}">${lab}${PR_SORT === k ? (PR_DIR < 0 ? " ▾" : " ▴") : ""}</th>`).join("")}<th>1Y</th><th></th></tr></thead><tbody>
    ${rows.map(r => `<tr class="${r.self ? "self" : ""}"><td><a href="/t/${encodeURIComponent(r.symbol)}">${esc(r.symbol)}</a></td><td class="nm l">${esc(r.name)}</td><td>${nf(r.price, 2)}</td>
      ${PR_COLS.map(([k, , f, signed]) => { const v = r[k], m = p.median[k];
        const better = !r.self || m == null || v == null ? "" : (["pe_fwd", "ev_ebitda", "ps"].includes(k) ? v < m : v > m) ? "b" : "w";
        return `<td class="${signed ? cls(v) : ""} ${r.self && better ? "cmp-" + better : ""}">${f(v)}</td>`; }).join("")}
      <td>${spark(r.spark || [], 70, 18, r.self ? "var(--ink)" : "var(--ink-soft)")}</td>
      <td>${r.self ? "" : `<button class="x" data-rm="${esc(r.symbol)}" title="remove from this list">×</button>`}</td></tr>`).join("")}
    <tr class="med"><td>median</td><td class="dim">peers</td><td></td>${PR_COLS.map(([k, , f, signed]) => `<td class="${signed ? cls(p.median[k]) : ""}">${f(p.median[k])}</td>`).join("")}<td></td><td></td></tr>
  </tbody></table></div>
  <div class="fbar peered"><input class="srch" id="peerAdd" placeholder="add a peer: ticker, Enter">
    ${p.custom ? `<button class="seg" id="peerReset">reset to automatic</button>` : ""}
    <span class="dim">bold outline: better than the peer median on that measure (cheaper for multiples)</span></div>`);
  $$("#peers [data-ps]").forEach(h => h.onclick = () => { const k = h.dataset.ps; PR_DIR = PR_SORT === k ? -PR_DIR : -1; PR_SORT = k; renderPeers(); });
  const save = async list => {
    setHTML("peers", spin);
    try { await fetch(`/api/pf/research/${encodeURIComponent(SYM)}/peers`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ peers: list }) }); } catch { }
    fetchPart("peers");
  };
  const cur = p.rows.filter(r => !r.self).map(r => r.symbol);
  $$("#peers [data-rm]").forEach(b => b.onclick = () => save(cur.filter(s => s !== b.dataset.rm)));
  const add = $("#peerAdd");
  add.onkeydown = e => { if (e.key === "Enter" && add.value.trim()) save([...cur, ...add.value.toUpperCase().split(/[\s,]+/).filter(Boolean)]); };
  const rs = $("#peerReset"); if (rs) rs.onclick = () => save([]);
  chartToolbar();
}

/* ---------- ownership & company ------------------------------------------------------ */
function renderOwn() {
  const o = S.ownership, st = (S.overview || {}).stats || {};
  if (!o) { setHTML("own", failed("ownership", "Ownership")); return; }
  const s = o.insider_summary || {};
  const KIND = { buy: ["buy", "pos"], sell: ["sale", "neg"], grant: ["grant", "dim"], gift: ["gift", "dim"], exercise: ["exercise", "dim"], other: ["other", "dim"] };
  const short = st.shares_short && st.shares_short_prior ? (st.shares_short / st.shares_short_prior - 1) * 100 : null;
  setHTML("own", `
    <div class="i3 own3">
      <div><span class="k">Insiders, 6 months</span><b><span class="pos">${s.buys} buys</span> · <span class="neg">${s.sells} sales</span></b><span class="dim">${s.buy_value ? big(s.buy_value) + " bought · " : ""}${s.sell_value ? big(s.sell_value) + " sold" : ""}</span></div>
      <div><span class="k">Held by</span><b>${pl(st.inst_pct, 0)} institutions</b><span class="dim">${pl(st.insiders_pct, 2)} insiders</span></div>
      <div><span class="k">Short interest</span><b>${pl(st.short_pct_float, 1)} of float</b><span class="dim">${st.short_ratio ? nf(st.short_ratio, 1) + " days to cover" : ""}${short != null ? ` · ${pc(short, 0)} on the month` : ""}</span></div></div>
    <div class="note plain">Insider sales are mostly planned (10b5-1) and tax-driven; an open-market <b class="pos inl">buy</b> is the rarer, more informative signal.</div>
    <div class="grp">Insider transactions</div>
    ${o.insiders.length ? `<div class="tscroll"><table class="tbl ins"><thead><tr><th>Date</th><th class="l">Who</th><th class="l">Role</th><th class="l"></th><th>Shares</th><th>Value</th></tr></thead><tbody>
      ${o.insiders.slice(0, 12).map(x => { const [lab, c] = KIND[x.kind] || [x.kind, ""]; return `<tr title="${esc(x.text)}"><td>${dmy(x.date)}</td><td class="l">${esc(x.who)}</td><td class="l dim">${esc(x.role || "")}</td>
        <td class="l ${c}">${lab}</td><td>${num(x.shares)}</td><td>${x.value ? big(x.value) : "–"}</td></tr>`; }).join("")}</tbody></table></div>` : `<div class="dim">None reported.</div>`}
    <div class="grp">Largest institutions</div>
    <div class="rows">${o.institutions.map(i => `<div class="row"><span class="nm">${esc(i.holder)}</span><span>${pl(i.pct, 2)}</span><span class="ch ${cls(i.chg)}">${i.chg != null ? pc(i.chg, 1) : ""}</span></div>`).join("") || `<div class="dim">None reported.</div>`}</div>
    ${src("institutional changes are quarter-on-quarter, from 13F filings as of " + (o.institutions[0] ? dmy(o.institutions[0].date) : "–"))}`);
}
function renderProfile() {
  const o = S.overview;
  if (!o) { setHTML("profile", failed("overview", "Profile")); return; }
  const ceo = (o.officers || []).find(x => /chief executive|ceo/i.test(x.title || "")), cfo = (o.officers || []).find(x => /chief financial|cfo/i.test(x.title || ""));
  setHTML("profile", `
    ${o.summary ? `<p class="summ" id="summ">${esc(o.summary)}</p><button class="linkish" onclick="document.getElementById('summ').classList.toggle('open')">more / less</button>` : ""}
    <div class="cols c-2 inner"><div>
      ${kv("Employees", o.employees ? nf(o.employees, 0) : "–")}
      ${kv("Headquarters", esc([o.city, o.country].filter(Boolean).join(", ") || "–"))}
      ${kv("Listed", o.ipo_year ? String(o.ipo_year) : "–")}
      ${o.website ? kv("Website", `<a href="${esc(o.website)}" target="_blank" rel="noopener">${esc(o.website.replace(/^https?:\/\/(www\.)?/, ""))}</a>`) : ""}
      ${ceo ? kv("CEO", esc(ceo.name)) : ""}${cfo ? kv("CFO", esc(cfo.name)) : ""}
    </div><div>
      ${kv("Shares outstanding", num(o.stats.shares_out))}
      ${kv("Float", num(o.stats.float))}
      ${kv("Revenue (TTM)", big(o.stats.revenue))}
      ${kv("Gross · operating margin", `${pl(o.stats.gross_margin, 0)} · ${pl(o.stats.op_margin, 0)}`)}
      ${kv("Cash · debt", `${big(o.stats.cash)} · ${big(o.stats.debt)}`)}
      ${kv("Return on equity", pl(o.stats.roe, 0))}
    </div></div>`);
}

/* ---------- your position ----------------------------------------------------------- */
function renderPosition() {
  const D = S.position, panel = $("#posPanel");
  if (!panel) return;
  if (!D || D.error || (!D.position && !D.closed)) { panel.hidden = true; return; }
  panel.hidden = false;
  const P = D.position || D.closed || {}, ccy = D.currency || "";
  const px2 = n => n == null ? "–" : nf(n, 2) + " " + ccy;
  setHTML("pos", `${D.open ? `
      ${kv("Quantity", P.qty)}${kv("Average cost", px2(P.avg_price))}${kv("Market value", `<b>${eur(P.value_eur, 2)}</b>`)}
      ${kv("Unrealised", eur(P.unreal_eur, 2), sgn(P.unreal_eur))}${kv("of which currency", eur(P.unreal_fx_eur, 2), sgn(P.unreal_fx_eur))}
      ${kv("Weight in the book", (P.weight ?? 0).toFixed(1) + "%")}${kv("Held", (P.held_days ?? "–") + " days")}
      ${kv("XIRR", P.xirr == null ? "–" : pct(P.xirr, 1), sgn(P.xirr))}`
    : `${kv("Closed on", dmy(P.closed))}${kv("Realised", eur(P.pnl_eur, 2), sgn(P.pnl_eur))}`}
    ${kv("Realised to date", eur(D.realised_eur, 2), sgn(D.realised_eur))}
    ${D.dividends_eur ? kv("Dividends", nf(D.dividends_eur, 2) + " " + ccy, "pos") : ""}
    ${D.beta ? kv("Beta in your book · R²", `${D.beta.beta} · ${(D.beta.r2 * 100).toFixed(0)}%`) : ""}
    ${D.contribution ? kv("Contribution to your return", `${eur(D.contribution.total_eur, 0)} · ${pct(D.contribution.points, 1)}`, sgn(D.contribution.total_eur)) : ""}
    <div class="grp">Your trades <span class="dim">— marked on the chart</span></div>
    <div class="rows">${(D.trades || []).slice().reverse().slice(0, 12).map(t => `<div class="row">
      <span class="nm"><b class="${t.kind === "BUY" ? "pos" : "neg"}">${t.kind}</b> ${t.qty} @ ${nf(t.price, 2)}</span>
      <span class="px">${dmy(t.d)}</span><span class="ch">${eur(t.eur, 0)}</span></div>`).join("")}</div>`);
}

/* ---------- thesis ------------------------------------------------------------------ */
let TH_EDIT = false;
function renderThesis() {
  const T = S.thesis;
  if (!T) { setHTML("thesis", `<div class="dim">Could not read your notes.</div>`); return; }
  const cur = T.current, price = ((S.overview || {}).quote || {}).price;
  const since = x => x.price_at && price ? (price / x.price_at - 1) * 100 : null;
  const STANCE = { bull: ["Bull", "pos"], bear: ["Bear", "neg"], neutral: ["Neutral", "dim"], watch: ["Watching", "warnc"] };
  const form = c => `<form id="thForm" class="thform">
    <div class="throw"><span class="lbl">Stance</span>${Object.entries(STANCE).map(([k, [l]]) => `<label class="rad"><input type="radio" name="stance" value="${k}"${(c && c.stance || "watch") === k ? " checked" : ""}>${l}</label>`).join("")}
      <span class="lbl">Conviction</span><select name="conviction">${[1, 2, 3, 4, 5].map(n => `<option${(c && c.conviction || 3) === n ? " selected" : ""}>${n}</option>`).join("")}</select>
      <span class="lbl">Fair value</span><input name="target" size="8" value="${c && c.target ? c.target : ""}" placeholder="${price ? nf(price, 0) : ""}">
      <span class="lbl">Horizon</span><select name="horizon">${["3 months", "6 months", "1 year", "3 years", "5 years+"].map(h => `<option${(c && c.horizon) === h ? " selected" : ""}>${h}</option>`).join("")}</select>
      <span class="lbl">Review on</span><input type="date" name="review_date" value="${c && c.review_date || ""}"></div>
    <div class="thgrid">
      <label><span>Why I own it / would own it</span><textarea name="thesis" rows="5" placeholder="The one-paragraph case. What does the market misunderstand?">${esc(c && c.thesis || "")}</textarea></label>
      <label><span>What has to go right <button type="button" class="linkish" data-fill="drivers">insert facts</button></span><textarea name="drivers" rows="5" placeholder="Measurable: segment growth, margins, backlog, share gains">${esc(c && c.drivers || "")}</textarea></label>
      <label><span>Risks <button type="button" class="linkish" data-fill="risks">insert facts</button></span><textarea name="risks" rows="5" placeholder="What could hurt it, and how likely">${esc(c && c.risks || "")}</textarea></label>
      <label><span>I am wrong if…</span><textarea name="kill" rows="5" placeholder="Concrete and checkable — e.g. cloud growth below 20% for two quarters">${esc(c && c.kill || "")}</textarea></label>
    </div>
    <div class="thbtn"><button class="tbtn on" type="submit">Save as new version</button>${c ? `<button class="tbtn" type="button" id="thCancel">Cancel</button>` : ""}<span id="thMsg" class="dim"></span></div></form>`;
  const card = c => { const [sl, sc] = STANCE[c.stance] || ["–", "dim"], s = since(c), up = c.target && price ? (c.target / price - 1) * 100 : null, rv = c.review_date ? daysTo(c.review_date) : null;
    return `<div class="thcard"><div class="thhead"><span class="chip st-${c.stance}"><b class="${sc}">${sl}</b></span>
      <span title="conviction">${"●".repeat(c.conviction || 0)}${"○".repeat(5 - (c.conviction || 0))}</span>
      ${c.target ? `<span>fair value <b>${nf(c.target, 2)}</b> <span class="${cls(up)}">${pc(up, 0)} from here</span></span>` : ""}
      ${c.horizon ? `<span class="dim">${esc(c.horizon)}</span>` : ""}
      <span class="dim">written ${dmyTime(c.created)} at ${nf(c.price_at, 2)}</span>${s != null ? `<span class="${cls(s)}">${pc(s, 1)} since</span>` : ""}
      ${rv != null ? `<span class="${rv < 0 ? "neg" : rv <= 7 ? "warnc" : "dim"}">review ${dmy(c.review_date)}${rv < 0 ? " — overdue" : ""}</span>` : ""}
      <button class="tbtn" id="thEdit">Revise</button></div>
      <div class="thgrid ro">${[["Why", c.thesis], ["What has to go right", c.drivers], ["Risks", c.risks], ["I am wrong if…", c.kill]].map(([h, t]) => `<div><span class="lbl">${h}</span><p>${esc(t || "—")}</p></div>`).join("")}</div></div>`; };
  const hist = T.versions.slice(1);
  setHTML("thesis", `
    ${!cur || TH_EDIT ? form(cur) : card(cur)}
    <div class="cols c-2 inner">
      <div><div class="grp">Journal <span class="dim">— dated notes, with the price when you wrote them</span></div>
        <div class="noteadd"><textarea id="noteBody" rows="2" placeholder="Earnings call takeaway, a worry, why you added…"></textarea><button class="tbtn on" id="noteBtn">Add</button></div>
        <div id="noteMsg" class="neg" style="font-size:.7rem"></div>
        ${T.notes.map(n => { const s = since(n); return `<div class="jn"><div class="dim">${dmyTime(n.created)} · at ${nf(n.price_at, 2)} ${s != null ? `<span class="${cls(s)}">${pc(s, 1)} since</span>` : ""}
          <button class="x" data-del="${n.id}" title="delete this note">×</button></div><p>${esc(n.body)}</p></div>`; }).join("") || `<div class="dim">No notes yet.</div>`}</div>
      <div><div class="grp">Earlier versions</div>
        ${hist.map(v => { const s = since(v), [sl, sc] = STANCE[v.stance] || ["–", "dim"]; return `<details class="ver"><summary>${dmy(v.created.slice(0, 10))} · <b class="${sc}">${sl}</b> · at ${nf(v.price_at, 2)}${v.target ? " · fair value " + nf(v.target, 0) : ""} ${s != null ? `<span class="${cls(s)}">${pc(s, 1)} since</span>` : ""}</summary>
          ${[["Why", v.thesis], ["Go right", v.drivers], ["Risks", v.risks], ["Wrong if", v.kill]].filter(x => x[1]).map(([h, t]) => `<div><span class="lbl">${h}</span><p>${esc(t)}</p></div>`).join("")}</details>`; }).join("") || `<div class="dim">${cur ? "This is the first version." : "Nothing written yet — the form above saves the first one."}</div>`}</div>
    </div>`);
  const f = $("#thForm");
  if (f) {
    f.onsubmit = async e => {
      e.preventDefault();
      const fd = new FormData(f), body = Object.fromEntries(fd.entries());
      body.conviction = parseInt(body.conviction); body.target = body.target ? parseFloat(String(body.target).replace(",", ".")) : null;
      if (!body.review_date) body.review_date = null;
      const r = await fetch(`/api/pf/research/${encodeURIComponent(SYM)}/thesis`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) { $("#thMsg").textContent = j.detail || "could not save"; $("#thMsg").className = "neg"; return; }
      S.thesis = j; TH_EDIT = false; renderThesis(); renderCatalysts(); renderGlance(); chartEvents(); chartLines();
    };
    const cc = $("#thCancel"); if (cc) cc.onclick = () => { TH_EDIT = false; renderThesis(); };
    $$("#thForm [data-fill]").forEach(b => b.onclick = () => {
      const ta = f.querySelector(`[name=${b.dataset.fill}]`);
      const facts = b.dataset.fill === "drivers" ? factsFor("up") : factsFor("down");
      ta.value = (ta.value ? ta.value.trimEnd() + "\n" : "") + facts.map(x => "• " + x).join("\n");
    });
  }
  const ed = $("#thEdit"); if (ed) ed.onclick = () => { TH_EDIT = true; renderThesis(); };
  $("#noteBtn").onclick = async () => {
    const body = $("#noteBody").value.trim(); if (!body) return;
    // A failed save used to do nothing at all, which looked like a dead button.
    let r = null;
    try { r = await fetch(`/api/pf/research/${encodeURIComponent(SYM)}/note`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ body }) }); }
    catch (e) { $("#noteMsg").textContent = `Not saved: ${e.message}`; return; }
    if (r.ok) { S.thesis = await r.json(); renderThesis(); return; }
    const j = await r.json().catch(() => ({}));
    $("#noteMsg").textContent = `Not saved: ${j.detail || `the server answered ${r.status}`}`;
  };
  $$("#thesis [data-del]").forEach(b => b.onclick = async () => {
    if (!confirm("Delete this note? It cannot be undone.")) return;
    await fetch(`/api/pf/research/note/${b.dataset.del}`, { method: "DELETE" });
    loadThesis();
  });
}
/* Facts to argue with, pulled from the panels — a starting point, not a thesis. */
function factsFor(dir) {
  const out = [];
  for (const i of insights()) {
    if (dir === "up" && (i.tone === "good")) out.push(i.text);
    if (dir === "down" && (i.tone === "warn" || i.tone === "bad")) out.push(i.text);
  }
  const b = S.business;
  if (dir === "up" && b && b.engine) out.push(`${b.engine.fastest.label} keeps growing above ${Math.floor((b.engine.fastest.growth || 0) / 5) * 5}%`);
  if (dir === "down" && S.valuation && S.valuation.current && S.valuation.current.implied_growth != null)
    out.push(`The price already assumes ${nf(S.valuation.current.implied_growth, 0)}% a year FCF growth for ten years`);
  return out.slice(0, 6);
}

/* ---------- wiring --------------------------------------------------------------------- */
const RENDER = {
  overview: () => { renderHeader(); renderProfile(); chartLines(); renderOwn(); if (S.street) renderStreet(); },
  chart: () => { if (S.chart) { CH.setDaily(S.chart); chartEvents(); chartStats(); $("#chartHint").textContent = `${S.chart.bars.length} sessions · ${S.chart.source || ""}`; } else $("#pcHost").innerHTML = failed("chart", "Price history"); },
  // The latest session is explained without a click: "what did it do today,
  // and why" is the first question on most days.
  moves: () => { renderMoves(); CH && CH.draw(); if (document.body.classList.contains("nonco")) renderHeader(); if (!DAY && S.moves && S.moves.days && S.moves.days[0]) pinDay(S.moves.days[0].d); },
  news: () => renderNews(),
  street: () => { renderStreet(); renderVal(); },
  fundamentals: () => { renderFin(); renderBiz(); if (S.street) renderStreet(); },
  consensus: () => { if (S.street) renderStreet(); },
  catalysts: () => { renderCatalysts(); renderHeader(); },
  valuation: () => { renderVal(); renderHeader(); },
  business: () => renderBiz(),
  peers: () => { renderPeers(); renderVal(); },
  ownership: () => renderOwn(),
};
// the chart re-reads its size when fonts arrive
document.fonts && document.fonts.ready.then(() => CH && CH.draw());
window.addEventListener("resize", () => { clearTimeout(window._rz); window._rz = setTimeout(() => CH && chartStats(), 200); });
setInterval(() => { if (!document.hidden && SYM) refreshQuote(); }, 60000);
async function refreshQuote() {
  try {
    const r = await fetch(`/api/pf/research/${encodeURIComponent(SYM)}/overview`);
    if (r.ok) { const j = await r.json(); if (!j.error) { S.overview = j; renderHeader(); } }
  } catch { }
}
load();
