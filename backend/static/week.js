/* ==========================================================================
   WEEK — the week in review, built each Saturday at 08:00 (pf/weekly.py):
   your book and what drove it, the markets and the flows, the week ahead,
   what the terminal flagged; Claude's note on top. /api/pf/week?wk= is one
   week (its Monday), /week/build and /week/note make them again.
   ========================================================================== */

let V = null, WK = new URLSearchParams(location.search).get("wk"), POLL = null, ALL = false;
$("#shell").innerHTML = termBar({ here: "WEEK · THE WEEK IN REVIEW", right:
  `<a class="tbtn" href="/m/ALRT" title="The morning brief">ALRT</a><button class="tbtn" id="bld" title="Build this week's review again">Build again</button>` });
wireShell();

const eu = v => v == null ? "–" : (v < 0 ? "−" : "+") + "€" + nf(Math.abs(v), 0);
const ceu = v => `<span class="${sgn(v)}">${eu(v)}</span>`;
const wpc = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d) + "%";
const cpc = (v, d = 1) => `<span class="${sgn(v)}">${wpc(v, d)}</span>`;
const dd = d => { if (!d) return "–"; const t = new Date(d + "T00:00:00Z"); return t.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", timeZone: "UTC" }); };

function mdInline(s) {
  return s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>").replace(/\[([^\]]+)\]\((\/[^\s)]*)\)/g, '<a href="$2">$1</a>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function md(src) {
  const lines = esc(src || "").split("\n");
  let out = "", list = null, para = [];
  const flush = () => { if (para.length) { out += `<p>${mdInline(para.join(" "))}</p>`; para = []; } };
  const endList = () => { if (list) { out += `</${list}>`; list = null; } };
  for (const l of lines) {
    let m;
    if ((m = l.match(/^(#{1,3})\s+(.*)$/))) { flush(); endList(); out += `<h${m[1].length}>${mdInline(m[2])}</h${m[1].length}>`; continue; }
    if ((m = l.match(/^\s*[-*]\s+(.*)$/))) { flush(); if (list !== "ul") { endList(); out += "<ul>"; list = "ul"; } out += `<li>${mdInline(m[1])}</li>`; continue; }
    if ((m = l.match(/^\s*\d+[.)]\s+(.*)$/))) { flush(); if (list !== "ol") { endList(); out += "<ol>"; list = "ol"; } out += `<li>${mdInline(m[1])}</li>`; continue; }
    if (!l.trim()) { flush(); endList(); continue; }
    if (list) endList();
    para.push(l.trim());
  }
  flush(); endList();
  return out;
}

async function api(path, opt) {
  const r = await fetch("/api/pf" + path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}

async function load(quiet) {
  clearTimeout(POLL);
  try { V = await api("/week" + (WK ? `?wk=${WK}` : "")); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load the review</b>${esc(e.message)}</div>`; return; }
  WK = V.week;
  render();
  const busy = (V.job || {}).state === "running" || ((V.note_job || {}).state === "running");
  if (busy) POLL = setTimeout(() => load(true), 4000);
}

$("#bld").onclick = async () => {
  try { await api("/week/build" + (WK ? `?wk=${WK}` : ""), { method: "POST" }); } catch (e) { alert(e.message); return; }
  V.job = { state: "running" }; render(); POLL = setTimeout(() => load(true), 4000);
};

function render() {
  const R = V.review;
  const sel = `<select id="wkSel">${(V.weeks.length ? V.weeks : [V.week]).map(w => `<option value="${w}" ${w === V.week ? "selected" : ""}>week of ${dd(w)}</option>`).join("")}</select>`;
  const job = (V.job || {}).state === "running" ? `<div class="job"><b>Building the review</b> · the book, the markets, the week ahead: half a minute</div>`
    : (V.job || {}).state === "error" ? `<div class="note"><b>The build failed</b>${esc(V.job.error || "")}</div>` : "";
  if (!R) {
    $("#app").innerHTML = `<div class="panel"><div class="body"><div class="fhead"><h1>The week in review</h1>${sel}</div>${job}
      <p class="expl">No review for this week yet. It is built ${esc(V.next)}, once every market's week is complete, with Claude's note after it.
      <b>Build again</b> makes it now.</p></div></div>`;
    wire(); return;
  }
  $("#app").innerHTML = `
  <div class="panel"><div class="body">
    <div class="fhead"><h1>The week of ${dd(R.week)} to ${dd(R.to)}</h1>${sel}<span class="sp"></span>
      <span class="dim">built ${new Date(R.built_at * 1000).toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}</span></div>
    ${job}
    <ul class="lines">${(R.lines || []).map(l => `<li>${esc(l)}</li>`).join("")}</ul>
    ${Object.keys(R.errors || {}).length ? `<div class="dim" style="font-size:.66rem">Not read this time: ${esc(Object.entries(R.errors).map(([k, e]) => k + " (" + e + ")").join("; "))}</div>` : ""}
  </div></div>
  ${notePanel()}
  ${bookPanel(R.book || {})}
  ${marketsPanel(R.markets || {})}
  ${aheadPanel(R.ahead || {})}
  ${terminalPanel(R.terminal || {})}`;
  wire();
}

function wire() {
  const s = $("#wkSel");
  if (s) s.onchange = () => { WK = s.value; history.replaceState(null, "", `/m/WEEK?wk=${WK}`); load(); };
  const nb = $("#noteBtn");
  if (nb) nb.onclick = async () => {
    try { V.note_job = await api(`/week/note?wk=${WK}`, { method: "POST" }); } catch (e) { alert(e.message); return; }
    render(); POLL = setTimeout(() => load(true), 4000);
  };
  const ab = $("#allBtn");
  if (ab) ab.onclick = () => { ALL = !ALL; render(); };
  wrapTables();
}

function notePanel() {
  const n = (V.notes || []).slice(-1)[0], j = V.note_job || {};
  const who = V.writer === "claude-code" ? "Claude Code, on your plan" : "the Anthropic API";
  let body;
  if (j.state === "running") body = `<div class="job"><b>Claude is writing the note</b> · ${esc(j.phase || "starting")} (${who})</div>`;
  else if (n) body = `<div class="md">${md(n.body)}</div><div class="dim" style="font-size:.62rem;margin-top:.4rem">${esc(n.model || "")} · ${esc((n.created || "").slice(0, 16).replace("T", " "))} UTC</div>`;
  else body = `<p class="expl">${j.state === "error" ? `<span class="neg">${esc(j.error || "")}</span> ` : ""}Claude writes a note from this review after each
    Saturday's build, through ${who}.</p>`;
  return `<div class="panel"><header><h2>Claude's note</h2><span class="hint">written from the review below, nothing else</span>
    <button class="tbtn" id="noteBtn" ${j.state === "running" ? "disabled" : ""}>${n ? "Write again" : "Write it now"}</button></header><div class="body">${body}</div></div>`;
}

function bookPanel(B) {
  if (!B.available) return `<div class="panel"><header><h2>Your book</h2></header><div class="body"><div class="dim">No result for this week.</div></div></div>`;
  const A = B.attribution || {}, rows = A.rows || [];
  const shown = ALL ? rows : rows.slice(0, 8).concat(rows.length > 16 ? rows.slice(-8) : rows.slice(8));
  const mx = Math.max(1, ...(A.sectors || []).map(s => Math.abs(s.eur || 0)));
  return `<div class="panel"><header><h2>Your book</h2><span class="hint">Friday's close against the Friday before, in euros</span></header><div class="body">
    <div class="tiles">
      ${tile("The week", ceu(B.eur), null, `${wpc(B.twr_pct, 2)} time-weighted`)}
      ${tile("S&P 500, same deposits", cpc(B.bench_pct, 2), null, B.twr_pct != null && B.bench_pct != null ? `you ${B.twr_pct >= B.bench_pct ? "ahead" : "behind"} by ${nf(Math.abs(B.twr_pct - B.bench_pct), 2)} points` : "")}
      ${tile("S&P 500", cpc(B.spx_pct), null, "in dollars")}
      ${tile("AEX", cpc(B.aex_pct), null, "")}
      ${tile("EUR/USD", cpc(B.eurusd_pct, 2), null, "a rising euro costs your dollar shares")}
      ${tile("The currency", ceu(A.fx_eur), null, "its part of the week")}
      ${tile("Dividends", B.dividends_eur ? eu(B.dividends_eur) : "–", null, "booked this week, after tax")}
      ${tile("Worth", B.value_eur != null ? "€" + nf(B.value_eur, 0) : "–", null, "on Friday's close")}
    </div>
    <div class="two even">
      <div><div class="ctitle">What drove it, holding by holding</div>
        <table class="tbl"><thead><tr><th class="l">Share</th><th>Week</th><th>Price</th><th>Currency</th><th>In euros</th></tr></thead><tbody>
        ${shown.map(r => `<tr><td class="l"><a class="tkr" href="/t/${encodeURIComponent(r.ticker)}"><b>${esc(r.ticker)}</b></a><span class="nm">${esc((r.name || "").slice(0, 26))}</span></td>
          <td>${ceu(r.eur)}</td><td>${ceu(r.price_eur)}</td><td>${ceu(r.fx_eur)}</td><td>${cpc(r.pct_eur)}</td></tr>`).join("")}
        ${B.rest_eur ? `<tr><td class="l dim">turbos, cash, sold positions and the rest</td><td>${ceu(B.rest_eur)}</td><td></td><td></td><td></td></tr>` : ""}
        </tbody></table>
        ${rows.length > 16 ? `<p style="margin:.4rem 0"><button class="tbtn" id="allBtn">${ALL ? "Show the top and bottom eight" : `Show all ${rows.length}`}</button></p>` : ""}
      </div>
      <div><div class="ctitle">By sector</div>
        ${(A.sectors || []).map(s => { const w = Math.abs(s.eur || 0) / mx * 50; return `<div class="bar"><span>${esc(s.sector)}</span><span class="t"><span class="z"></span>
          <i style="${(s.eur || 0) >= 0 ? `left:50%;width:${w}%;background:var(--gain)` : `right:50%;width:${w}%;background:var(--loss)`}"></i></span><span style="text-align:right">${ceu(s.eur)}</span></div>`; }).join("")}
        <div class="ctitle">Trades this week</div>
        ${(B.trades || []).length ? `<table class="tbl"><tbody>${B.trades.map(t => `<tr><td class="l">${dd(t.d)}</td><td class="l">${esc(t.side === "BUY" ? "bought" : "sold")} ${nf(Math.abs(t.qty), 0)} ${esc(t.product || "")}</td><td>${nf(t.price, 2)} ${esc(t.ccy || "")}</td><td>${eu(t.eur)}</td></tr>`).join("")}</tbody></table>`
          : `<div class="dim" style="font-size:.7rem">None booked${B.import_to ? `: your last DEGIRO import runs to ${dmy(B.import_to)}` : ""}.</div>`}
      </div>
    </div>
    <div class="expl">Each holding's euros are the shares held all week, from last Friday's close to this one: the price in its own currency, and the currency by itself.
      Shares bought or sold in the week count what they made against what they cost. Turbos, cash, interest and positions sold before the week ended are one line, so the parts add up to the book's change.</div>
  </div></div>`;
}

function marketsPanel(M) {
  const g = {};
  for (const r of M.rows || []) (g[r.g] = g[r.g] || []).push(r);
  const F = M.flow || {}, hf = F.hedge_funds || {}, dk = F.dix || {}, br = M.breadth || {};
  return `<div class="panel"><header><h2>Markets</h2><span class="hint">the market board's week, the flows, the releases</span></header><div class="body">
    <div class="two even"><div>${Object.entries(g).map(([k, rows]) => `<div class="ctitle">${esc(k)}</div><table class="tbl"><tbody>
      ${rows.map(r => `<tr><td class="l">${esc(r.name)}</td><td>${nf(r.last, r.last > 100 ? 0 : 2)}</td><td>${r.w_bp != null ? `<span class="${sgn(r.w_bp)}">${r.w_bp > 0 ? "+" : ""}${nf(r.w_bp, 0)}bp</span>` : cpc(r.w_pct)}</td></tr>`).join("")}</tbody></table>`).join("")}</div>
    <div>
      <div class="ctitle">Breadth and flows</div>
      ${br.spx ? `<div class="kv"><span>S&P 500 members above their 200-day</span><span>${nf(br.spx.p200, 0)}% (${br.spx.chg > 0 ? "+" : ""}${nf(br.spx.chg, 1)} points)</span></div>` : ""}
      ${br.ndx ? `<div class="kv"><span>Nasdaq 100 members above their 200-day</span><span>${nf(br.ndx.p200, 0)}% (${br.ndx.chg > 0 ? "+" : ""}${nf(br.ndx.chg, 1)} points)</span></div>` : ""}
      ${(F.days || []).map(e => `<div class="kv"><span>${dd(e.d)}: a ${e.kind.slice(0, 2)}% ${e.kind.endsWith("down") ? "downside" : "upside"} day</span><span>${nf(e.share, 1)}% of the volume rising</span></div>`).join("")}
      ${F.episode && F.episode.open ? `<div class="kv"><span>The selling of ${dd(F.episode.start)}</span><span class="warnc">not answered yet</span></div>` : ""}
      ${hf.gross_now != null ? `<div class="kv"><span>Hedge funds' equity futures, gross</span><span>$${nf(hf.gross_now, 0)}bn · ${wpc(hf.chg1)} on the week${hf.degrossing ? ` · <span class="neg">de-grossing</span>` : ""}</span></div>` : ""}
      ${dk.dix != null ? `<div class="kv"><span>DIX on ${dd(dk.d)}</span><span>${nf(dk.dix, 1)}% (${ord(Math.round(dk.dix_rank))} percentile of five years)</span></div>` : ""}
      ${F.putcall ? `<div class="kv"><span>Put/call, five days</span><span>${nf(F.putcall.avg5, 2)} (${ord(Math.round(F.putcall.rank))} percentile)</span></div>` : ""}
      <p style="margin:.4rem 0"><a href="/m/FLOW">FLOW</a> · <a href="/m/IDX">IDX</a></p>
      <div class="ctitle">The big releases</div>
      ${(M.macro || []).length ? `<table class="tbl"><thead><tr><th class="l">Release</th><th>Actual</th><th>Expected</th><th>Before</th></tr></thead><tbody>
        ${M.macro.map(e => `<tr><td class="l">${dd(e.d)} · ${esc(e.ccy)} ${esc(e.title)}</td><td><b>${esc(e.actual || "–")}</b></td><td>${esc(e.forecast || "–")}</td><td>${esc(e.previous || "–")}</td></tr>`).join("")}</tbody></table>`
        : `<div class="dim" style="font-size:.7rem">No high-impact release this week.</div>`}
    </div></div>
  </div></div>`;
}

function aheadPanel(A) {
  const days = {};
  const add = (d, h) => (days[d] = days[d] || []).push(h);
  for (const e of A.earnings || []) add(e.date, `<a href="/m/ERN/${encodeURIComponent(e.sym)}">${esc(e.sym)}</a> reports${e.time ? " " + esc(e.time) : ""}${e.turbo ? `<span class="tag warn">your turbo</span>` : e.held ? `<span class="tag mine">yours</span>` : e.watch ? `<span class="tag">watching</span>` : ""}`);
  for (const e of A.macro || []) add(e.d, `${esc(e.ccy)} ${esc(e.title)}${e.t ? " " + esc(e.t) : ""}${e.forecast ? ` <span class="dim">expected ${esc(e.forecast)}</span>` : ""}`);
  for (const e of A.central_banks || []) add(e.date, `<b>${esc(e.title)}</b>`);
  for (const e of A.holidays || []) add(e.date, `${esc(e.market)} closed: ${esc(e.title)}`);
  for (const e of A.ex_div || []) add(e.date, `${esc(e.sym)} goes ex-dividend`);
  const keys = Object.keys(days).sort();
  return `<div class="panel"><header><h2>The week ahead</h2><span class="hint">${dd(A.from)} to ${dd(A.to)}</span></header><div class="body">
    ${keys.length ? keys.map(d => `<div class="day"><b>${dd(d)}</b>${days[d].join(" · ")}</div>`).join("") : `<div class="dim">Nothing on the calendar for your book.</div>`}
  </div></div>`;
}

function terminalPanel(T) {
  return `<div class="panel"><header><h2>The terminal's week</h2></header><div class="body"><div class="two even">
    <div>
      <div class="ctitle">Alerts it raised</div>
      <div class="list">${(T.alerts || []).map(a => `<div>${dd(a.d)} · <b>${esc(a.code)}</b> ${esc(a.text)}</div>`).join("")
        || `<div class="dim">${T.alerts_since ? "None this week." : "The alert record starts with this build (53): from next week every alert is here."}</div>`}</div>
      <div class="ctitle">READ's articles</div>
      <div class="list">${(T.read || []).map(a => `<a href="/m/READ#a=${a.id}">${esc(a.headline)}<span class="dim">${dd(a.d)} · importance ${a.importance}</span></a>`).join("") || `<div class="dim">None this week.</div>`}</div>
    </div>
    <div>
      <div class="ctitle">Congress in your stocks</div>
      <div class="list">${(T.congress_mine || []).map(t => `<div>${esc(t.member)} ${t.type === "purchase" ? "bought" : "sold"} <b>${esc(t.ticker)}</b> <span class="dim">${esc(t.amount || "")} · filed ${dmy(t.filed)}</span></div>`).join("")
        || `<div class="dim">No trade in your stocks filed this week.</div>`}</div>
      ${T.congress_all != null ? `<div class="dim" style="font-size:.66rem;margin-top:.2rem">${T.congress_all} trades filed in all · <a href="/m/POL">POL</a></div>` : ""}
      <div class="ctitle">Koers AI</div>
      <div class="list">${T.kai_line ? `<div>${esc(T.kai_line)}</div>` : ""}${(T.kai_trades || []).map(t => `<div>${dd(t.d)} · ${esc(t.side)} ${nf(t.qty, 0)} <b>${esc(t.ticker)}</b> <span class="dim">€${nf(t.value_eur, 0)}</span></div>`).join("")
        || (T.kai_line ? "" : `<div class="dim">No contest running.</div>`)}</div>
      <div class="ctitle">New on the watchlist</div>
      <div class="list">${(T.wl_new || []).map(w => `<div><a href="/m/WCH#s=${encodeURIComponent(w.sym)}"><b>${esc(w.sym)}</b></a> ${esc((w.name || "").slice(0, 34))} <span class="dim">${esc(w.preset || "")} · score ${nf(w.score, 0)}</span></div>`).join("")
        || `<div class="dim">No new name this week.</div>`}</div>
    </div>
  </div></div></div>`;
}

load();
