/* ==========================================================================
   ALRT — the morning brief.

   One call (/api/pf/morning) returns the brief for a morning, built by the
   server at 06:30 on weekdays or on first ask, and kept. While it is being
   built the page polls; while Claude is writing, only Claude's panel redraws,
   so a half-typed question is not wiped every two seconds.
   ========================================================================== */

$("#shell").innerHTML = termBar({
  here: "ALRT",
  right: `<button class="tbtn" id="refBtn" title="Build the brief again: fresh prices, news and calendars">Refresh</button>
          <button class="tbtn" id="helpBtn">Help</button>
          <a class="tbtn" href="/">Home</a>`
});
wireShell();
$("#helpBtn").onclick = showHelp;
$("#refBtn").onclick = () => rebuild();

let V = null, C = null, D = new URLSearchParams(location.search).get("d") || null, POLL = null, CPOLL = null, BUILT = null, SHOW_ALL = false;
const WD = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MON = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];

async function api(path, opt) {
  const r = await fetch("/api/pf" + path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });

/* ---------- formatting ---------------------------------------------------- */
const dd = s => new Date(String(s).slice(0, 10) + "T12:00:00");
const wday = s => WD[dd(s).getDay()];
const longDate = s => { const d = dd(s); return `${WD[d.getDay()]} ${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}`; };
const short = s => dmy(s).slice(0, 5);
const pc = (v, d = 1) => v == null ? "–" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d) + "%";
const bp = v => v == null ? "–" : (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(0) + "bp";
const eu = v => v == null ? "–" : (Math.round(v) < 0 ? "−" : "") + eur(Math.abs(v));
const eus = v => v == null ? "–" : (Math.round(v) > 0 ? "+" : Math.round(v) < 0 ? "−" : "") + eur(Math.abs(v));
const cl = v => v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "";
const tk = (sym, extra = "") => `<a href="/t/${encodeURIComponent(sym)}${extra}"><b>${esc(sym)}</b></a>`;
const ago = ts => {
  if (!ts) return "";
  const m = Math.max(0, (Date.now() / 1000 - ts) / 60);
  return m < 60 ? Math.round(m) + "m ago" : m < 48 * 60 ? Math.round(m / 60) + "h ago" : Math.round(m / 1440) + "d ago";
};
const hhmm = ts => { const t = new Date(ts * 1000); return `${String(t.getHours()).padStart(2, "0")}:${String(t.getMinutes()).padStart(2, "0")}`; };
const loading = txt => `<div class="dim" style="padding:.4rem 0">${txt}<span class="blink">_</span></div>`;
const panel = (id, title, body, hint = "", code = "", href = "") => `<div class="panel" id="${id}">
  <header><h2>${title}</h2>${hint ? `<span class="hint">${hint}</span>` : ""}${code ? `<a class="code" href="${href || "/m/" + code}">${code} &rsaquo;</a>` : ""}</header>
  <div class="body">${body}</div></div>`;

/* The summary and Claude's text carry Markdown links; only relative links and
   http(s) survive, everything else is escaped first. */
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
const plain = s => String(s || "").replace(/\*\*([^*]+)\*\*/g, "$1").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");

/* ---------- loading ------------------------------------------------------- */
async function load(d, quiet) {
  let v;
  try { v = await api("/morning" + (d ? `?d=${encodeURIComponent(d)}` : "")); }
  catch (e) {
    $("#app").innerHTML = `<div class="panel"><div class="body"><div class="note"><b>Could not load the brief</b>${esc(e.message)}</div></div></div>`;
    return;
  }
  V = v; D = v.d;
  const url = "/m/ALRT" + (D !== v.default ? `?d=${D}` : "") + location.hash;
  if (location.pathname + location.search + location.hash !== url) history.replaceState(null, "", url);
  const b = V.brief;
  if (b && D === V.default) { try { localStorage.setItem("alrt_seen", D); } catch { } }
  const stamp = b ? b.built_ts : null;
  if (!(quiet && stamp === BUILT && $("#claude"))) { BUILT = stamp; render(); }
  clearTimeout(POLL);
  if (V.job && V.job.state === "running") POLL = setTimeout(() => load(D, true), 1500);
  if (b && (!quiet || !C)) loadClaude();
}

/* Claude's panel has its own request: asking Claude Code whether it is signed
   in takes a few seconds, and the brief should not wait for that. */
async function loadClaude() {
  const d = D;
  try { C = await api(`/morning/claude?d=${encodeURIComponent(d)}`); } catch (e) { C = { error: e.message }; }
  if (d !== D) return;
  renderClaude();
  clearTimeout(CPOLL);
  if (C.job && C.job.state === "running") CPOLL = setTimeout(loadClaude, 2000);
}

async function rebuild() {
  try { await api(`/morning/build?d=${encodeURIComponent(D || "")}`, { method: "POST" }); }
  catch (e) { flash(esc(e.message)); return; }
  load(D, true);
}

/* ---------- the page ------------------------------------------------------ */
function render() {
  const b = V.brief, j = V.job;
  document.title = `ALRT ${short(V.d)} · Het Koersbord`;
  let h = header();
  if (j && j.state === "running") {
    h += `<div class="panel"><div class="body"><span class="pulse"></span><b>Building the brief</b>
      <span class="dim"> — ${esc(j.phase || "starting")} · ${j.elapsed_s}s. The first build of a day reads a year of prices for every
      holding and explains the movers; about a minute, then seconds.</span></div></div>`;
  } else if (j && j.state === "error") {
    h += `<div class="panel"><div class="body"><div class="note"><b>The brief could not be built</b>${esc(j.error || "")}</div></div></div>`;
  }
  if (!b) {
    if (!(j && j.state === "running")) {
      const canBuild = V.d >= V.today || V.d === V.default;
      h += `<div class="panel"><div class="body"><div class="note plain"><b>No brief for ${esc(longDate(V.d))}</b>
        ${canBuild ? `Press Refresh to build it now.` : `The server was not running that morning, so none was built. A brief is only built
        for today or the next morning: last week's news window cannot be rebuilt honestly after the fact.`}</div></div></div>`;
    }
    $("#app").innerHTML = h;
    wireHeader();
    return;
  }
  h += tiles(b) + `
    <div class="panel" id="s-short"><header><h2>The short version</h2><span class="hint">written by rules from everything below</span></header>
      <div class="body"><ul class="short">${(b.summary || []).map(s => `<li>${mdInline(esc(s))}</li>`).join("")}</ul>
      ${errs(b)}</div></div>
    <nav class="secnav">${[["s-short", "Short version"], ["s-book", "Your book"], ["s-ern", "Earnings"], ["s-news", "News"],
      ["s-mkt", "Markets"], ["s-open", "Before the open"], ["s-today", "Today"], ["s-pos", "Your positions"],
      ["s-week", "Week ahead"], ["s-claude", "Claude"]].map(([id, t]) => `<a href="#${id}">${t}</a>`).join("")}</nav>
    <div class="two">
      <div class="stack">
        <div class="colh"><b>Yesterday</b> ${esc(wday(b.session))} ${esc(short(b.session))} — the last session</div>
        ${bookPanel(b)}${earningsPanel(b)}${newsPanel(b)}${marketsPanel(b)}
      </div>
      <div class="stack">
        <div class="colh"><b>Today</b> ${esc(wday(b.date))} ${esc(short(b.date))} — Amsterdam time</div>
        ${openPanel(b)}${todayPanel(b)}${positionsPanel(b)}${weekPanel(b)}
      </div>
    </div>
    <div class="panel" id="s-claude"><header><h2>Claude's brief</h2><span class="hint" id="claudeHint"></span></header>
      <div class="body" id="claude"></div></div>
    ${footer(b)}`;
  $("#app").innerHTML = h;
  wireHeader();
  renderClaude();
  const bar = document.querySelector(".term-bar");
  document.documentElement.style.setProperty("--barh", (bar ? bar.offsetHeight : 36) + "px");
  if (location.hash.length > 1 && !render.scrolled) {
    render.scrolled = true;
    const el = document.getElementById(location.hash.slice(1));
    if (el) setTimeout(() => el.scrollIntoView({ block: "start" }), 50);
  }
  $$("[data-all]").forEach(a => a.onclick = e => { e.preventDefault(); SHOW_ALL = !SHOW_ALL; render(); });
}

function header() {
  const ds = [...new Set([V.default, ...(V.dates || []).map(x => x.d), V.d])].sort().reverse();
  const i = ds.indexOf(V.d);
  const b = V.brief;
  const built = b ? new Date(b.built_at) : null;
  const sub = b ? `covers ${wday(b.session)} ${short(b.session)} · built ${short(b.built_at.slice(0, 10)) !== short(b.date) ? short(b.built_at.slice(0, 10)) + " " : ""}${String(built.getHours()).padStart(2, "0")}:${String(built.getMinutes()).padStart(2, "0")}${b.edition === "morning" ? " (the morning build)" : ""}`
    : "";
  return `<div class="hdr">
    <h1>MORNING BRIEF</h1><span class="date">${esc(longDate(V.d))}</span><span class="sub">${esc(sub)}</span>
    <span class="sp"></span>
    <div class="fbar">
      <button class="seg" id="prevD" ${i < ds.length - 1 ? "" : "disabled"} title="the brief before">&lsaquo;</button>
      <select class="seg" id="pickD" title="every brief kept">${ds.map(d => `<option value="${d}"${d === V.d ? " selected" : ""}>${wday(d).slice(0, 3)} ${short(d)}${d === V.default ? " · now" : ""}</option>`).join("")}</select>
      <button class="seg" id="nextD" ${i > 0 ? "" : "disabled"} title="the brief after">&rsaquo;</button>
      ${V.d !== V.default ? `<button class="seg go" id="nowD">Latest</button>` : ""}
      ${b ? `<button class="seg" id="copyB" title="the short version and today's timeline, as plain text">Copy</button>
             <button class="seg" id="printB">Print</button>` : ""}
    </div></div>`;
}

function wireHeader() {
  const ds = [...new Set([V.default, ...(V.dates || []).map(x => x.d), V.d])].sort().reverse();
  const i = ds.indexOf(V.d);
  const go = d => { SHOW_ALL = false; render.scrolled = true; C = null; load(d); window.scrollTo(0, 0); };
  $("#prevD") && ($("#prevD").onclick = () => i < ds.length - 1 && go(ds[i + 1]));
  $("#nextD") && ($("#nextD").onclick = () => i > 0 && go(ds[i - 1]));
  $("#pickD") && ($("#pickD").onchange = e => go(e.target.value));
  $("#nowD") && ($("#nowD").onclick = () => go(V.default));
  $("#printB") && ($("#printB").onclick = () => window.print());
  $("#copyB") && ($("#copyB").onclick = async () => {
    const b = V.brief;
    const lines = [`Morning brief — ${longDate(b.date)}`, ""]
      .concat((b.summary || []).map(s => "• " + plain(s)), ["", `Today (Amsterdam time):`])
      .concat((b.today || []).map(e => `  ${e.time || "     "}  ${plain(e.title)}${e.forecast ? ` (f ${e.forecast}${e.previous ? `, p ${e.previous}` : ""})` : ""}`));
    try { await navigator.clipboard.writeText(lines.join("\n")); flash("Copied the short version and today's timeline."); }
    catch { flash("The browser refused the clipboard."); }
  });
}

function tiles(b) {
  const bk = b.book || {}, pos = (bk.positions || []).filter(x => x.traded);
  const top = pos[0];
  const diff = bk.pct != null && b.spx_eur != null ? bk.pct - b.spx_eur : null;
  const tl = (b.today || []).filter(e => (e.impact || 0) >= 2 || e.mine);
  const first = tl.find(e => e.time) || tl[0];
  const fut = ((b.overnight || {}).rows || []).find(r => r.sym === "ES=F");
  const mine = (b.today || []).filter(e => e.kind === "earnings" && e.mine);
  return `<div class="tiles">
    <a class="tile lead" href="#s-book"><div class="k">Your book · ${esc(wday(b.session).slice(0, 3))}</div>
      <div class="v ${cl(bk.eur)}">${eus(bk.eur)} <small>${pc(bk.pct, 2)}</small></div>
      <div class="s">share prices ${eus(bk.eur_price)} · currency ${eus(bk.eur_fx)}</div></a>
    <div class="tile"><div class="k">Against the S&amp;P 500</div>
      <div class="v ${cl(diff)}">${diff == null ? "–" : (diff >= 0 ? "+" : "−") + Math.abs(diff).toFixed(2) + "pp"}</div>
      <div class="s">S&amp;P 500 ${pc(b.spx_eur, 2)} in euros</div></div>
    <div class="tile"><div class="k">Up · down</div>
      <div class="v"><span class="pos">${bk.up ?? "–"}</span> · <span class="neg">${bk.down ?? "–"}</span></div>
      <div class="s">of ${bk.n ?? 0} positions priced${(bk.unmeasured || []).length ? ` · ${bk.unmeasured.length} not` : ""}</div></div>
    ${top ? `<a class="tile" href="/t/${encodeURIComponent(top.ticker)}?day=${b.session}"><div class="k">Biggest in euros</div>
      <div class="v ${cl(top.eur)}">${esc(top.ticker)} <small>${pc(top.move)}</small></div>
      <div class="s">${eus(top.eur)}${top.reason && top.reason.kind === "headline" ? " · " + esc(top.reason.text) : ""}</div></a>` : ""}
    <a class="tile" href="#s-today"><div class="k">Today</div>
      <div class="v">${tl.length} <small>event${tl.length === 1 ? "" : "s"}${mine.length ? ` · ${mine.length} of yours report` : ""}</small></div>
      <div class="s">${first ? esc((first.time ? first.time + " " : "") + plain(first.short || first.title)) : "nothing of note scheduled"}</div></a>
    <a class="tile" href="#s-open"><div class="k">S&amp;P futures</div>
      <div class="v ${cl(fut && fut.pct)}">${fut && fut.pct != null ? pc(fut.pct, 2) : "–"}</div>
      <div class="s">${fut && fut.pct != null ? "since the close, at " + esc(hhmm(b.built_ts)) : "not trading yet when built"}</div></a>
  </div>`;
}

function errs(b) {
  const e = Object.entries(b.errors || {});
  return e.length ? `<div class="src">Parts that failed this build: ${e.map(([k, v]) => `${esc(k)} (${esc(v)})`).join("; ")} — press Refresh to try again.</div>` : "";
}

/* ---------- yesterday ----------------------------------------------------- */
function reasonHTML(r) {
  if (!r) return "";
  const link = (t, u) => u ? `<a href="${esc(u)}" target="_blank" rel="noopener">${esc(t)}</a>` : esc(t);
  if (r.kind === "catalyst") return `<span class="bdg acc">${esc(r.type || "catalyst")}</span>${link(r.text, r.url)}`;
  if (r.kind === "headline") return `“${link(r.text, r.url)}”${r.source ? ` <span class="dim">${esc(r.source)}</span>` : ""}`;
  if (r.kind === "sector" || r.kind === "market")
    return `<span class="bdg info">${esc(r.kind)}</span>${esc(r.text)}${r.headline ? ` “${link(r.headline, r.url)}”` : ""}`;
  if (r.kind === "none") return `<span class="bdg warn">no cause</span>${esc(r.text)}`;
  return `<span class="dim">${esc(r.text)}</span>`;
}

function flagsHTML(x) {
  const f = [];
  if (x.high52) f.push(`<span class="bdg good">52w high</span>`);
  if (x.low52) f.push(`<span class="bdg bad">52w low</span>`);
  if ((x.rvol || 0) >= 2) f.push(`<span class="bdg warn">volume ${x.rvol.toFixed(1)}×</span>`);
  if (x.gap != null && Math.abs(x.gap) >= 3) f.push(`<span class="bdg info">gap ${pc(x.gap)}</span>`);
  if (x.z != null && Math.abs(x.z) >= 2.5) f.push(`<span class="bdg warn">${x.z > 0 ? "+" : "−"}${Math.abs(x.z).toFixed(1)}σ</span>`);
  if (x.ex_div) f.push(`<span class="bdg info">ex-dividend</span>`);
  if (x.thesis) {
    const t = x.thesis;
    f.push(`<a class="bdg acc" href="/t/${encodeURIComponent(x.ticker)}#s-thesis" title="your thesis">${esc(t.stance || "thesis")}${t.target ? ` · fair ${t.target}${t.to_target != null ? ` (${pc(t.to_target, 0)})` : ""}` : ""}</a>`);
  }
  return f.length ? `<span class="flags">${f.join("")}</span>` : "";
}

function bookPanel(b) {
  const bk = b.book || {};
  if (!bk.n) return panel("s-book", "Your book", `<div class="empty">No priced positions — import your ledger on <a href="/portfolio">PORT</a>.</div>`, "", "PORT", "/portfolio");
  const secs = (bk.sectors || []).filter(s => Math.abs(s.eur || 0) >= 1)
    .sort((a, b) => Math.abs(b.eur) - Math.abs(a.eur)).slice(0, 8).sort((a, b) => a.eur - b.eur);
  const mx = Math.max(1, ...secs.map(s => Math.abs(s.eur || 0)));
  const secHTML = secs.length ? `<div class="grp2">By sector</div>${secs.map(s => `<div class="secbar"><span class="n">${esc(s.sector)}</span>
      <span class="t"><span class="mid"></span><i style="${s.eur >= 0 ? "left:50%" : "right:50%"};width:${Math.abs(s.eur) / mx * 50}%;background:${s.eur >= 0 ? "var(--gain)" : "var(--loss)"}"></i></span>
      <span class="v ${cl(s.eur)}">${eus(s.eur)}</span></div>`).join("")}` : "";
  const rows = (bk.positions || []).filter(x => x.traded);
  const shown = SHOW_ALL ? rows : rows.slice(0, 12);
  const tbl = `<div class="grp2">Positions, biggest euro move first</div><div class="tscroll"><table class="tbl"><thead><tr>
      <th>Position</th><th>Move</th><th>Euros</th><th class="l">Why</th></tr></thead><tbody>
      ${shown.map(x => `<tr><td class="l">${tk(x.ticker, `?day=${b.session}`)} <span class="dim">${x.weight != null ? x.weight.toFixed(1) + "%" : ""}</span></td>
        <td class="${cl(x.move)}">${pc(x.move, 2)}</td><td class="${cl(x.eur)}">${eus(x.eur)}</td>
        <td class="w">${reasonHTML(x.reason)}${flagsHTML(x)}</td></tr>`).join("")}
      </tbody></table></div>
      ${rows.length > 12 ? `<div class="src"><a href="#" data-all>${SHOW_ALL ? "Show the twelve biggest" : `Show all ${rows.length}`}</a></div>` : ""}`;
  const un = (bk.unmeasured || []);
  const closed = (bk.positions || []).filter(x => !x.traded);
  const mine = new Set((bk.positions || []).map(x => x.ticker));
  const filled = Object.entries((b.sources || {}).filled || {}).filter(([s]) => mine.has(s));
  return panel("s-book", "Your book", `${secHTML}${tbl}
    <div class="src">Close to close in euros, at today's quantities: the share-price part and the currency part, added.
      A ticker opens its page on that session, with the market, sector and company parts of the move.
      ${closed.length ? ` Did not trade: ${closed.map(x => esc(x.ticker)).join(", ")}.` : ""}
      ${filled.length ? ` Yahoo's daily history had dropped ${esc(wday(b.session))} for ${filled.map(([s]) => esc(s)).join(", ")};
        that close comes from ${[...new Set(filled.map(([, h]) => h))].map(esc).join(" and ")}.` : ""}
      ${un.length ? ` Not measured (no price history): ${un.map(u => esc(u.ticker || u.name)).join(", ")}.` : ""}</div>`,
    `${eus(bk.eur)} · ${pc(bk.pct, 2)}`, "PORT", "/portfolio");
}

const andList = xs => xs.length < 2 ? (xs[0] || "") : xs.slice(0, -1).join(", ") + " and " + xs[xs.length - 1];
const tieWords = c => [c.sells_to.length ? "sells to " + andList(c.sells_to) : "", c.buys_from.length ? "buys from " + andList(c.buys_from) : ""]
  .filter(Boolean).join("; ") + ` (${c.weight.toFixed(1)}% of your book)`;

function earningsPanel(b) {
  const E = (b.earnings || {}).yesterday || { mine: [], big: [] };
  const n = ((b.earnings || {}).counts || {}).yesterday || 0;
  let h = "";
  if ((E.mine || []).length) {
    h += `<div class="grp2">Yours</div>` + E.mine.map(e => {
      const r = e.results || {}, eps = r.eps || {}, rev = r.revenue || {}, rx = r.reaction || {};
      const sp = eps.surprise != null ? eps.surprise : e.surprise;
      const verdict = sp == null ? `<span class="bdg info">reported</span>` : sp > 0 ? `<span class="bdg good">beat</span>` : sp < 0 ? `<span class="bdg bad">miss</span>` : `<span class="bdg info">in line</span>`;
      return `<div class="li"><span class="r"><a href="${esc(e.href)}"><b>${esc(e.sym)}</b></a></span><span class="x">${verdict}
        ${eps.actual != null ? `EPS ${eps.actual}${eps.est != null ? ` vs ${eps.est}` : ""}${sp != null ? ` (${pc(sp)})` : ""}` : e.eps_act != null ? `EPS ${e.eps_act} vs ${e.eps_est ?? "–"}` : "no figures yet"}
        ${rev.actual != null ? ` · revenue ${(rev.actual / 1e9).toFixed(2)}bn${rev.est ? ` vs ${(rev.est / 1e9).toFixed(2)}bn` : ""}` : ""}
        ${rx.move != null ? ` · <span class="${cl(rx.move)}">${pc(rx.move)}</span> ${esc(rx.phase === "session" ? "on the day" : rx.phase || "")}${rx.eur != null ? ` (${eus(rx.eur)} for you)` : ""}` : ""}
        ${(r.guidance || []).length ? `<br><span class="dim">Guidance: ${esc(r.guidance[0])}</span>` : ""}</span>
        <span class="r"><a href="${esc(e.href)}">report &rsaquo;</a></span></div>`;
    }).join("");
  }
  const big = (E.big || []).filter(e => e.eps_act != null || e.move != null);
  if (big.length) {
    h += `<div class="grp2">Large caps</div><div class="tscroll"><table class="tbl"><thead><tr><th>Company</th><th>EPS</th><th>Surprise</th><th>Move</th></tr></thead><tbody>
      ${big.map(e => `<tr><td class="l"><a href="${esc(e.href)}"><b>${esc(e.sym)}</b></a> <span class="dim">${esc((e.name || "").slice(0, 24))}</span></td>
        <td>${e.eps_act ?? "–"}${e.eps_est != null ? ` <span class="dim">/ ${e.eps_est}</span>` : ""}</td>
        <td class="${cl(e.surprise)}">${e.surprise != null ? pc(e.surprise) : "–"}</td>
        <td class="${cl(e.move)}">${e.move != null ? pc(e.move) : e.when === "amc" ? `<span class="dim">after close</span>` : "–"}</td></tr>`).join("")}
      </tbody></table></div>`;
  }
  const tied = (E.tied || []);
  if (tied.length) {
    h += `<div class="grp2">Read-through: not yours, but they trade with what is</div>` + tied.map(e => `<div class="li"><span class="r"><a href="${esc(e.href)}"><b>${esc(e.sym)}</b></a></span>
      <span class="x">${esc((e.name || "").slice(0, 30))}${e.surprise != null ? ` · <span class="${cl(e.surprise)}">${e.surprise > 0 ? "beat" : e.surprise < 0 ? "missed" : "in line"} ${pc(e.surprise)}</span>` : e.eps_act != null ? " · reported" : " · no figures yet"}${e.move != null ? ` · <span class="${cl(e.move)}">${pc(e.move)}</span>` : ""}
        <br><span class="dim">${esc(tieWords(e.chain))}</span></span>
      <span class="r"><a href="/m/SPLC/${encodeURIComponent(e.sym)}">chain &rsaquo;</a></span></div>`).join("");
  }
  if (!h) h = `<div class="empty">None of your holdings or watched names reported on ${esc(wday(b.session))}${n ? `; ${n} other companies did, none above $20bn` : ""}.
    <a href="/m/ERN">The week on ERN &rsaquo;</a></div>`;
  return panel("s-ern", "Earnings", h, `${esc(wday(b.session))}${n ? ` · ${n} reported` : ""}`, "ERN");
}

function newsItem(x) {
  const chips = (x.about || []).map(t => `<a class="chip hold" href="/t/${encodeURIComponent(t)}">${esc(t)}</a>`).join("")
    + (x.shock ? `<span class="bdg warn">unexpected</span>` : "")
    + (x.kinds || []).filter(k => k !== "macro" || !(x.about || []).length).map(k => `<span class="chip">${esc(k)}</span>`).join("");
  return `<div class="nw"><a class="hl" href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.headline)}</a>
    <div class="mt">${chips}<span>${esc(x.source || "")}${x.ts ? ` · ${esc(ago(x.ts))}` : ""}</span></div></div>`;
}

function newsPanel(b) {
  const N = b.news || {};
  if (!N.n) return panel("s-news", "News", `<div class="empty">No headlines were stored for this window. The news thread keeps every
    headline for ten days from build 21 on; with a Finnhub key (Settings on the home screen) it covers far more than your holdings.</div>`, "", "NEWS");
  const by = (N.holdings || []).filter(h => (h.items || []).length);
  return panel("s-news", "News", `
    <div class="grp2">Big or unexpected, on what you own</div>${(N.big || []).map(newsItem).join("") || `<div class="empty">Nothing that stands out.</div>`}
    <div class="grp2">The world</div>${(N.world || []).map(newsItem).join("") || `<div class="empty">No macro or geopolitical headlines scored high enough.</div>`}
    ${by.length ? `<details class="more"><summary>Every holding in the news (${by.length})</summary>
      ${by.map(h => `<div class="byco"><div class="h">${tk(h.ticker)} <span class="dim">${h.weight != null ? h.weight.toFixed(1) + "%" : ""}</span></div>
        ${h.items.map(newsItem).join("")}</div>`).join("")}</details>` : ""}
    <div class="src">${N.n} headlines published between ${esc(dmyTime(N.since * 1000))} and ${esc(dmyTime(N.until * 1000))}, scored by what you hold,
      whether it moves markets, and surprise words; clickbait is dropped. <a href="/m/GEO">GEO</a> maps the geopolitics to your positions.</div>`,
    "since the start of the session", "NEWS");
}

function marketsPanel(b) {
  const M = b.markets || [];
  if (!M.length) return panel("s-mkt", "Markets", `<div class="empty">No market prices in this build.</div>`, "", "MKT");
  const groups = [...new Set(M.map(m => m.g))];
  const fmt = m => m.kind === "yield" ? m.last.toFixed(2) + "%" : m.kind === "fx" ? m.last.toFixed(4) : nf(m.last, 2);
  const tbl = `<div class="tscroll"><table class="tbl"><thead><tr><th>Market</th><th>Close</th><th>Day</th><th>YTD</th></tr></thead><tbody>
    ${groups.map(g => `<tr class="sec"><td colspan="4" class="l" style="color:var(--ink-soft);font-size:.58rem;letter-spacing:.14em;text-transform:uppercase;padding-top:.5rem">${esc(g)}</td></tr>` +
      M.filter(m => m.g === g).map(m => `<tr><td class="l"><a href="${esc(m.href)}">${esc(m.name)}</a>${m.stale ? ` <span class="dim">· ${esc(short(m.day))}</span>` : ""}</td>
        <td>${fmt(m)}</td>
        <td class="${m.roll ? "" : cl(m.bp != null ? m.bp : m.pct)}">${m.roll ? `<span class="dim" title="the continuous futures series switched to the next contract; the change is the roll, not the market">roll</span>` : m.bp != null ? bp(m.bp) : pc(m.pct, 2)}</td>
        <td class="${cl(m.ytd)}">${m.ytd != null ? pc(m.ytd) : ""}</td></tr>`).join("")).join("")}
    </tbody></table></div>`;
  const ym = b.yesterday_macro || [];
  const rel = ym.length ? `<div class="grp2">Released on ${esc(wday(b.session))}</div>${ym.map(e => `<div class="li"><span class="r dim">${esc(e.time || "")}</span>
      <span class="x"><span class="imp i${e.impact}"></span>${esc(e.title)} <span class="dim">${esc(e.ccy || "")}</span></span>
      <span class="r dim">${e.forecast ? `f ${esc(e.forecast)}` : ""}${e.previous ? ` · p ${esc(e.previous)}` : ""}</span></div>`).join("")}
      <div class="src">The free calendar carries forecasts, not the released figures; the reaction is in the yields above and on <a href="/m/MKT/bonds">BOND</a>.</div>` : "";
  return panel("s-mkt", "Markets", tbl + rel, "close to close", "MKT");
}

/* ---------- today --------------------------------------------------------- */
function openPanel(b) {
  const O = b.overnight || {}, oh = ((b.watch || {}).outside) || [];
  const rows = (O.rows || []).filter(r => r.pct != null || r.bp != null);
  let h = rows.length ? `<div class="tscroll"><table class="tbl"><tbody>${rows.map(r => `<tr><td class="l"><a href="${esc(r.href || "/t/" + r.sym)}">${esc(r.name)}</a></td>
      <td>${r.sym === "^TNX" ? r.last.toFixed(2) + "%" : nf(r.last, r.sym.endsWith("=X") ? 4 : 2)}</td>
      <td class="${cl(r.bp != null ? r.bp : r.pct)}">${r.bp != null ? bp(r.bp) : pc(r.pct, 2)}</td></tr>`).join("")}</tbody></table></div>`
    : `<div class="empty">${esc(O.note || "Nothing has traded since the close yet.")}</div>`;
  if (oh.length) h += `<div class="grp2">Your holdings outside hours</div>` + oh.map(m => `<div class="li"><span class="r">${tk(m.ticker)}</span>
      <span class="x">${esc(m.phase)}${m.price ? ` at ${nf(m.price, 2)}` : ""}</span>
      <span class="r ${cl(m.pct)}">${pc(m.pct, 1)} <span class="dim">${eus(m.eur)}</span></span></div>`).join("");
  return panel("s-open", "Before the open", h + (rows.length ? `<div class="src">Futures and Asia against the last close; after-hours and pre-market moves of your US names
    beyond 1.5%. As of ${esc(hhmm(b.built_ts))}${b.edition === "morning" ? "" : " — the 06:30 build refreshes this"}.</div>` : ""),
    "overnight", "MKT");
}

const KIND = { macro: ["macro", "info"], cb: ["central bank", "warn"], earnings: ["earnings", "acc"], auction: ["auction", "info"],
  market: ["market", "info"], election: ["election", "warn"], news: ["in the news", "info"] };

function evDetail(e) {
  const d = [];
  if (e.kind === "macro" && (e.forecast || e.previous)) d.push(`forecast ${esc(e.forecast || "–")} · previous ${esc(e.previous || "–")}`);
  if (e.kind === "earnings") {
    if (e.name) d.push(esc(e.name));
    if (e.eps_est != null) d.push(`EPS est ${e.eps_est}`);
    if (e.implied != null) d.push(`options price ±${e.implied.toFixed(1)}%${e.implied_eur != null ? ` ≈ ${eur(e.implied_eur)} on your position` : ""}`);
    if (e.mine && e.weight != null) d.push(`${e.weight.toFixed(1)}% of your book`);
    if (e.chain && e.chain.strong && !e.mine) d.push(`<a href="${esc(e.chain.href)}">${esc(tieWords(e.chain))}</a>`);
    if (!e.time_exact && e.time) d.push("time estimated");
  }
  if (e.detail) d.push(esc(e.detail));
  if (e.why) d.push(esc(e.why));
  if (e.kind === "news" && (e.about || []).length) d.push(e.about.map(t => tk(t)).join(" "));
  return d.join(" · ");
}

function evTitle(e) {
  if (e.kind === "earnings") return `<a href="${esc(e.href)}">${esc(e.title)}</a>${e.mine ? ` <span class="bdg acc">yours</span>` : e.watch ? ` <span class="bdg info">watched</span>` : ""}`;
  if (e.kind === "news" && e.url) return `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title)}</a>`;
  if (e.href) return `<a href="${esc(e.href)}">${esc(e.title)}</a>`;
  return esc(e.title);
}

function todayPanel(b) {
  const T = b.today || [];
  if (!T.length) return panel("s-today", "Today", `<div class="empty">Nothing scheduled that the calendars carry.</div>`, "", "ERN");
  const h = `<div class="tl">${T.map(e => {
    const k = KIND[e.kind] || [e.kind, "info"];
    return `<div class="tm">${esc(e.time || "all day")}</div>
      <div class="ev i${e.impact || 1}${e.mine ? " mine" : ""}"><div class="tt"><span class="imp i${e.impact || 1}"></span><span class="bdg ${k[1]}">${esc(k[0])}</span>${evTitle(e)}</div>
        ${evDetail(e) ? `<div class="dt">${evDetail(e)}</div>` : ""}</div>`;
  }).join("")}</div>
  <div class="src">Macro releases from ForexFactory's calendar (medium and high impact, dollar, euro and sterling; minor central-bank
    speakers grouped); Fed and ECB decisions; earnings of yours, watched names and companies above $20bn (ERN); US Treasury auctions;
    exchange holidays (list as of ${esc(b.holidays_as_of || "")}); option expiries and quarter-ends; elections (GEO); and events the news
    says are scheduled today — a heuristic, labelled “in the news”.</div>`;
  return panel("s-today", "Today", h, `${T.length} scheduled`, "ERN");
}

function positionsPanel(b) {
  const W = b.watch || {};
  const mine = (b.today || []).filter(e => e.kind === "earnings" && e.mine);
  let h = "";
  if (mine.length) h += `<div class="grp2">Reporting today</div>` + mine.map(e => `<div class="li"><span class="r"><a href="${esc(e.href)}"><b>${esc(e.sym)}</b></a></span>
    <span class="x">${esc(e.time || "time not announced")} · ${evDetail(e)}</span><span class="r"><a href="${esc(e.href)}">ERN &rsaquo;</a></span></div>`).join("");
  if ((W.outside || []).length) h += `<div class="grp2">Moving outside hours</div>` + W.outside.map(m => `<div class="li"><span class="r">${tk(m.ticker)}</span>
    <span class="x">${esc(m.phase)}</span><span class="r ${cl(m.pct)}">${pc(m.pct)} <span class="dim">${eus(m.eur)}</span></span></div>`).join("");
  if ((W.dividends || []).length) h += `<div class="grp2">Ex-dividend</div>` + W.dividends.map(d => `<div class="li"><span class="r">${tk(d.ticker)}</span>
    <span class="x">${d.past ? `went ex on ${esc(wday(d.ex))} — the drop that day was the dividend, not the market` : d.ex === b.date ? "ex-dividend today — it opens lower by about the dividend" : `ex-dividend ${esc(wday(d.ex))} ${esc(short(d.ex))}`}${d.rate ? ` · ${d.rate} a year${d.yield ? ` (${d.yield.toFixed(1)}%)` : ""}` : ""}${d.pay ? ` · paid ${esc(short(d.pay))}` : ""}</span></div>`).join("");
  if ((W.reviews || []).length) h += `<div class="grp2">Your theses</div>` + W.reviews.map(r => `<div class="li"><span class="r">${tk(r.ticker, "#s-thesis")}</span>
    <span class="x">${r.due === "moved" ? "moved hard yesterday — does the thesis still hold?" : `review ${esc(r.due)}${r.review ? ` (${esc(dmy(r.review))})` : ""}`}${r.target ? ` · fair value ${r.target}${r.to_target != null ? ` (${pc(r.to_target, 0)} away)` : ""}` : ""}${r.kill ? `<br><span class="dim">You are wrong if: ${esc(r.kill)}</span>` : ""}</span></div>`).join("");
  if ((W.rules || []).length) h += `<div class="grp2">Your alert rules</div>` + W.rules.map(r => `<div class="li"><span class="r">${tk(r.ticker)}</span>
    <span class="x">${esc(r.text)}${r.note ? ` <span class="dim">— ${esc(r.note)}</span>` : ""}</span><span class="r ${r.state === "fired" ? "warnc" : "dim"}">${esc(r.state)}</span></div>`).join("");
  if ((W.knockouts || []).length) h += `<div class="grp2">Knock-outs near the barrier</div>` + W.knockouts.map(k => `<div class="li"><span class="r">${esc(k.ticker || "")}</span>
    <span class="x">${esc(k.name)}</span><span class="r warnc">${k.pct}% away</span></div>`).join("");
  if (!h) h = `<div class="empty">Nothing on your positions needs attention today: none of yours report, none moved beyond 1.5% outside hours,
    no ex-dividend date this week, no thesis review due, no alert rule close to firing.</div>`;
  return panel("s-pos", "Your positions today", h, "", "PORT", "/portfolio");
}

function weekPanel(b) {
  const W = b.week || {};
  let h = (W.days || []).map(d => `<div class="d">${esc(wday(d.date))} ${esc(short(d.date))}</div>` + d.items.map(e => {
    const k = KIND[e.kind] || [e.kind, "info"];
    return `<div class="li"><span class="r dim">${esc(e.time || "")}</span><span class="x"><span class="bdg ${k[1]}">${esc(k[0])}</span>${evTitle(e)}${e.forecast ? ` <span class="dim">f ${esc(e.forecast)}</span>` : ""}</span></div>`;
  }).join("")).join("");
  if ((W.later || []).length) h += `<div class="d">Further out</div>` + W.later.map(e => `<div class="li"><span class="r dim">${esc(short(e.date))}</span>
    <span class="x"><span class="bdg ${(KIND[e.kind] || ["", "info"])[1]}">${esc((KIND[e.kind] || [e.kind])[0])}</span><a href="${esc(e.href)}">${esc(e.title)}</a></span></div>`).join("");
  if (!h) h = `<div class="empty">Nothing high-impact in the next seven days.</div>`;
  return panel("s-week", "Week ahead", `<div class="wk">${h}</div><div class="src">High-impact releases only (the free macro calendar holds this week, not the next),
    your holdings' and watched names' reports, companies above $200bn, long-bond auctions, holidays and expiries.</div>`, "the next seven days", "ERN");
}

function footer(b) {
  const s = b.sources || {};
  return `<div class="dim" style="font-size:.64rem;line-height:1.55;padding:.2rem .1rem 1rem">
    Built ${esc(dmyTime(b.built_at))} in ${b.took_s}s from ${s.prices || 0} price series (Yahoo), ${s.headlines || 0} headlines (${esc(s.news_provider || "news")}),
    the macro calendar (ForexFactory), Nasdaq and Finnhub earnings calendars, TreasuryDirect and the security pages' own move analysis.
    The brief is built at 06:30 on weekdays while the app runs, kept for a year, and never changes once the morning has passed unless you press Refresh.</div>`;
}

/* ---------- Claude -------------------------------------------------------- */
function renderClaude() {
  const el = $("#claude");
  if (!el) return;
  if (!C) { el.innerHTML = loading("asking Claude Code whether it is signed in"); return; }
  if (C.error) { el.innerHTML = `<div class="note"><b>Claude's panel failed</b>${esc(C.error)}</div>`; return; }
  const c = C, st = c.cc || {}, job = c.job, latest = (c.notes || []).slice(-1)[0];
  const cc = c.writer === "claude-code";
  $("#claudeHint").textContent = cc ? `Claude Code · ${c.cc_model_label} · on your Claude plan, no charge per brief`
    : `${c.model_label} · billed to your API key`;
  const keepQ = $("#askQ") ? $("#askQ").value : "";
  let h = "";
  if (cc && !st.found) {
    h += `<div class="note plain"><b>Claude Code was not found</b>The Claude desktop app installs it. Or switch the writer to an API key under Settings on the <a href="/">home screen</a>.</div>`;
  } else if (cc && !st.logged_in) {
    h += `<div class="note plain"><b>One-time sign-in</b>Claude Code writes on your Claude subscription — no charge per brief; each counts toward
      your plan's usage like a short chat. It has to be signed in once: press “Sign in”, finish in the browser page that opens (claude.ai),
      then press “Check again”.
      <div class="fbar" style="margin-top:.5rem"><button class="seg go" id="ccLogin">Sign in</button><button class="seg" id="ccCheck">Check again</button>
      <span class="dim" id="ccMsg" style="font-size:.64rem"></span></div>
      <div class="src">Or in a terminal: <code>"${esc(st.path || "claude")}" auth login --claudeai</code></div></div>`;
  } else if (!cc && !c.has_key) {
    h += `<div class="note plain"><b>Needs an Anthropic API key</b>Add one under Settings on the <a href="/">home screen</a>, or switch the writer to Claude Code.</div>`;
  } else {
    const running = job && job.state === "running";
    h += `<div class="fbar">
      <button class="seg go" id="clGo" ${running ? "disabled" : ""}>${latest ? "Write it again" : "Write it with Claude"}</button>
      <label class="dim" style="font-size:.66rem"><input type="checkbox" id="clWeb"> let Claude search the web for why a position moved when the brief has no cause</label>
      ${c.auto ? `<span class="dim" style="font-size:.64rem">· written automatically at 06:30 (Settings)</span>` : ""}</div>`;
    if (job && (running || job.state === "error")) {
      h += job.state === "error" ? `<div class="note"><b>Not written</b>${esc(job.error || "")}</div>`
        : `<div class="dim" style="font-size:.68rem;margin-top:.4rem"><span class="pulse"></span>${esc(job.question ? "Answering" : job.phase)} · ${job.elapsed_s}s</div>
           ${job.text ? `<div class="writing md">${md(job.text)}</div>` : ""}`;
    }
    if (latest) {
      const m = latest.meta || {};
      h += `<div class="md" style="margin-top:.6rem">${md(latest.body)}</div>
        <div class="src">${esc(latest.model || "")} · ${esc(dmyTime(latest.created))}${latest.cost_usd != null ? ` · $${latest.cost_usd.toFixed(3)}` : m.writer === "claude-code" ? ` · no charge, on your Claude plan${m.api_equivalent_usd != null ? ` (≈$${m.api_equivalent_usd.toFixed(2)} at API prices)` : ""}` : ""}${m.web ? " · web search on" : ""}
          · <a href="#" data-del="${latest.id}">delete</a></div>
        <div id="claudeAsk"><div class="grp2">Ask about it</div>
          <textarea class="ask" id="askQ" placeholder="Why did BE fall? What would a hot CPI do to this book?"></textarea>
          <div class="fbar" style="margin-top:.3rem"><button class="seg go" id="askGo" ${running ? "disabled" : ""}>Ask</button>
            <span class="dim" style="font-size:.64rem">${m.writer === "claude-code" ? "continues the brief's own conversation" : "re-uses the brief from the prompt cache"}</span></div>
          ${(c.answers || []).map(a => `<div style="margin-top:.6rem"><div class="dim" style="font-size:.68rem">Q: ${esc(a.question)}</div><div class="md">${md(a.body)}</div>
            <div class="src">${esc(dmyTime(a.created))} · <a href="#" data-del="${a.id}">delete</a></div></div>`).join("")}</div>`;
    } else if (!running) {
      h += `<div class="src">Claude reads this brief — the numbers, the reasons, the headlines, today's schedule — and writes it up as a note:
        what happened, what it means for this book, what to watch. It adds no figures of its own; with web search it may look up why a
        position moved when the brief found no cause.</div>`;
    }
  }
  el.innerHTML = h;
  if ($("#askQ")) $("#askQ").value = keepQ;
  $("#ccLogin") && ($("#ccLogin").onclick = async () => {
    try { await api("/earnings/claude-code/login", { method: "POST" }); $("#ccMsg").textContent = "a sign-in window opened — finish it in the browser, then press Check again"; }
    catch (e) { $("#ccMsg").textContent = e.message; }
  });
  $("#ccCheck") && ($("#ccCheck").onclick = async () => {
    $("#ccMsg").textContent = "checking…";
    try { const s = await api("/earnings/claude-code"); if (s.logged_in) loadClaude(); else $("#ccMsg").textContent = "not signed in yet"; }
    catch (e) { $("#ccMsg").textContent = e.message; }
  });
  $("#clGo") && ($("#clGo").onclick = async () => {
    try { await post(`/morning/note?d=${D}`, { web: $("#clWeb").checked }); loadClaude(); }
    catch (e) { flash(esc(e.message)); }
  });
  $("#askGo") && ($("#askGo").onclick = async () => {
    const q = $("#askQ").value.trim();
    if (!q) return;
    try { await post(`/morning/note?d=${D}`, { question: q }); $("#askQ").value = ""; loadClaude(); }
    catch (e) { flash(esc(e.message)); }
  });
  $$("#claude [data-del]").forEach(a => a.onclick = async e => {
    e.preventDefault();
    if (!confirm("Delete this write-up?")) return;
    await api(`/morning/note/${a.dataset.del}`, { method: "DELETE" });
    loadClaude();
  });
}

load(D);
