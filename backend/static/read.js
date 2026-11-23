/* ==========================================================================
   READ — the day's live coverage, and articles written for you.
   /api/pf/read is the page (live coverage, the list, today's budget, the
   editor's last pass, the running scan); /api/pf/read/a/{id} one article
   with its figures' data and what it touches in your book (pf/reader.py).
   ========================================================================== */

let O = null, A = null, POLL = null, LAND = null, ASKPOLL = null;
const COLORS = ["var(--ink)", "var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"];

$("#shell").innerHTML = termBar({ here: "READ · ARTICLES", right:
  `<a class="tbtn" href="/m/NEWS" title="Every headline, ranked">NEWS</a>
   <button class="tbtn" id="scanBtn" title="Scan the news and write what deserves it, within today's budget">Scan now</button>` });
wireShell();
$("#scanBtn").onclick = scan;

async function api(path, opt) {
  const r = await fetch("/api/pf" + path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const agoS = ts => {
  if (!ts) return "";
  const t = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
  const m = Math.max(0, (Date.now() - t) / 60000);
  return m < 60 ? `${Math.round(m)} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};
const hhmm = iso => { const t = new Date(iso); return isNaN(t) ? "" : t.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }); };
const dayLabel = d => {
  const t = new Date(d + "T12:00:00"), today = new Date(); today.setHours(12, 0, 0, 0);
  const n = Math.round((today - t) / 864e5);
  return n === 0 ? "Today" : n === 1 ? "Yesterday" : t.toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
};
const dots = n => `<span class="imp" title="importance ${n} of 5">${"●".repeat(n)}<i>${"●".repeat(5 - n)}</i></span>`;
const kindChip = (k, lab) => `<span class="kind ${esc(k)}">${esc(lab || k)}</span>`;
const heldSet = () => new Set(((A && A.book && A.book.rows) || []).map(r => r.t));
const sp = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d) + "%";

/* ---------- loading ---------- */
async function load() {
  try { O = await api("/read"); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load READ</b>${esc(e.message)}</div>`; return; }
  render();
  const m = location.hash.match(/a=(\d+)/);
  if (m) openArticle(+m[1]);
  else if (window.innerWidth > 1100 && O.articles.length) openArticle(O.articles[0].id, true);
  pollJob();
}

function render() {
  $("#app").innerHTML = `
    <div class="panel"><div class="body">
      ${liveCard(O.live)}
      <div class="status" id="status">${statusBar()}</div>
      <div id="jobBox">${jobBox(O.job)}</div>
    </div></div>
    <div class="cols" id="cols">
      <div class="list" id="list">${listHtml()}${editorHtml()}</div>
      <div id="reader">${O.articles.length ? `<div class="empty">Choose an article.</div>` : emptyHtml()}</div>
    </div>`;
  wireStatus();
  wireList();
}

/* ---------- the live coverage card ---------- */
function liveCard(L) {
  if (!L || !L.main) return `<div class="lcov"><span class="pill">Live coverage</span>
    <span class="s" style="white-space:normal">None found yet. New York's live pages start before its open, around 13:00 Amsterdam time on a weekday${
      L && (L.errors || []).length ? ` · the feeds did not answer (${esc(L.errors.join("; "))})` : ""}.</span><span></span></div>`;
  const on = L.state === "live";
  return `<div class="lcov"><span class="pill ${on ? "on" : ""}">${esc(L.label)}</span>
    <a class="t" href="${esc(L.main.url)}" target="_blank" rel="noopener">${esc(L.main.title)}</a>
    <span class="s">${esc(L.main.source)} · ${agoS(L.main.ts)}</span>
    ${(L.more || []).length ? `<div class="more">${L.more.map(m => `<span><a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.title)}</a> · ${esc(m.source)}, ${agoS(m.ts)}</span>`).join("")}</div>` : ""}</div>`;
}

/* ---------- status, the running scan ---------- */
function statusBar() {
  const t = O.today, s = O.last_scan, n = O.next;
  return `<span>Today <b>${t.written} of ${t.max}</b> articles written</span>
    ${s ? `<span>last scan ${esc(dayLabel(s.d)).toLowerCase()} ${hhmm(s.started)} · ${esc(s.slot)}${s.state === "error" ? ` · <span class="neg">${esc(s.error || "failed")}</span>` : ` · ${s.n_written || 0} written`}</span>` : `<span>no scan yet</span>`}
    ${n ? `<span>next: ${esc(n.label)} ${n.at.slice(0, 10) === new Date().toISOString().slice(0, 10) ? "" : dmy(n.at.slice(0, 10)) + " "}${n.at.slice(11, 16)}</span>` : ""}
    <span class="sp"></span>
    ${O.ready ? `<span class="warnc">${esc(O.ready)}</span>` : ""}
    <label title="Off: nothing is written unless you press Scan now"><input type="checkbox" id="autoBox" ${O.auto ? "checked" : ""}> on schedule</label>`;
}
function wireStatus() {
  const b = $("#autoBox");
  if (b) b.onchange = async () => { try { await post("/read/auto", { on: b.checked }); O.auto = b.checked; } catch (e) { flash(esc(e.message)); } };
}
function jobBox(j) {
  if (!j || j.state !== "running") return j && j.state === "error" && O.job === j ? `<div class="note"><b>The last scan stopped</b>${esc(j.error || "")}</div>` : "";
  const tail = (j.text || "").slice(-420);
  return `<div class="job"><b>${esc(j.phase || "working")}</b>${j.step ? ` · ${esc(j.step)}` : ""}${j.current ? ` · <span class="dim">${esc(j.current)}</span>` : ""}
    <span class="blink">_</span>${tail ? `<div class="tail">${esc(tail)}</div>` : ""}</div>`;
}
async function scan() {
  try { O.job = await post("/read/scan"); $("#jobBox").innerHTML = jobBox(O.job); pollJob(); }
  catch (e) { flash(esc(e.message)); }
}
function pollJob() {
  clearTimeout(POLL);
  if (!O.job || O.job.state !== "running") return;
  POLL = setTimeout(async () => {
    try { O.job = await api("/read/job"); } catch (e) { return; }
    $("#jobBox").innerHTML = jobBox(O.job);
    if (O.job.state === "running") pollJob();
    else { const keep = A && A.id; await load(); if (keep) openArticle(keep, true); }
  }, 3000);
}

/* ---------- the list ---------- */
function listHtml() {
  if (!O.articles.length) return "";
  let h = "", day = null;
  for (const a of O.articles) {
    if (a.d !== day) { day = a.d; h += `<div class="day">${esc(dayLabel(a.d))}</div>`; }
    h += `<a class="it ${a.seen ? "" : "unread"}" href="#a=${a.id}" data-id="${a.id}">
      <div class="k">${kindChip(a.kind, a.kind_label)}${dots(a.importance)}<span>${hhmm(a.created)}</span>${a.version > 1 ? `<span class="warnc">updated</span>` : ""}</div>
      <div class="h">${esc(a.headline)}</div><div class="d">${esc(a.dek || "")}</div>
      ${(a.tickers || []).length ? `<div class="chips">${a.tickers.map(t => `<span class="chip t">${esc(t)}</span>`).join("")}</div>` : ""}</a>`;
  }
  return h;
}
function wireList() {
  $$("#list .it").forEach(el => el.onclick = e => { e.preventDefault(); openArticle(+el.dataset.id); });
  $$("[data-write]").forEach(b => b.onclick = async () => {
    try { O.job = await post("/read/write", { title: b.dataset.write, why: "asked for by the reader" }); $("#jobBox").innerHTML = jobBox(O.job); pollJob(); window.scrollTo(0, 0); }
    catch (e) { flash(esc(e.message)); }
  });
}
function editorHtml() {
  const s = O.last_scan;
  const sk = (Array.isArray(s && s.skipped) ? s.skipped : []).slice(0, 12);
  if (!s || (!s.summary && !sk.length)) return "";
  return `<div class="day">The editor's last pass · ${esc(s.slot)} ${hhmm(s.started)}</div>
    <div class="ed">${s.summary ? `<p style="margin:.4rem 0">${esc(s.summary)}</p>` : ""}
    ${sk.map(x => `<div class="sk"><span>${esc(x.title)}<br><span class="why">${esc(x.why || "")}</span></span>
      <button class="tbtn" data-write="${esc(x.title)}" title="Write an article on this anyway (counts toward today's ${O.today.max})">Write</button></div>`).join("")}</div>`;
}
function emptyHtml() {
  return `<div class="empty"><b>No articles yet.</b> The editor scans the news three times a weekday (07:15, 16:30 and 22:45) and
    once at a weekend, and writes up what moves the market or your book: between none on a quiet day and eight on a busy one.
    Press <b>Scan now</b> to start one. ${O.ready ? `<br><br><span class="warnc">${esc(O.ready)}</span>` : ""}</div>`;
}

/* ---------- one article ---------- */
async function openArticle(id, quiet) {
  $$("#list .it").forEach(el => el.classList.toggle("on", +el.dataset.id === id));
  if (!quiet) $("#reader").innerHTML = `<div class="empty">Opening<span class="blink">_</span></div>`;
  try { A = await api(`/read/a/${id}`); }
  catch (e) { $("#reader").innerHTML = `<div class="note"><b>Could not open the article</b>${esc(e.message)}</div>`; return; }
  history.replaceState(null, "", `#a=${id}`);
  const li = document.querySelector(`#list .it[data-id="${id}"]`); if (li) li.classList.remove("unread");
  $("#cols").classList.add("reading");
  renderArticle();
  if (!quiet && window.innerWidth <= 1100) window.scrollTo(0, $("#cols").offsetTop - 50);
}

function renderArticle() {
  const a = A, b = a.body, held = heldSet();
  const words = (b.sections || []).reduce((n, s) => n + s.p.join(" ").split(/\s+/).length, 0);
  const figs = a.figures || [];
  const chainAt = Math.max(0, (b.sections || []).findIndex(s => /why|markets care|matter/i.test(s.h)));
  let prose = "";
  (b.sections || []).forEach((s, i) => {
    prose += `<h2>${esc(s.h)}</h2>${s.p.map(p => `<p>${esc(p)}</p>`).join("")}`;
    if (i === chainAt && (b.chain || []).length) prose += chainHtml(b.chain);
    if (figs[i]) prose += `<div class="fig" id="fig${i}"></div>`;
  });
  for (let i = (b.sections || []).length; i < figs.length; i++) prose += `<div class="fig" id="fig${i}"></div>`;
  const prev = a.versions.length > 1 ? a.versions[a.versions.findIndex(v => v.id === a.id) - 1] : null;
  $("#reader").innerHTML = `<article class="art">
    <a class="back" href="#" id="backBtn">‹ all articles</a>
    ${a.id !== a.latest ? `<div class="upd">An earlier version. <a href="#a=${a.latest}" data-open="${a.latest}">Read the latest</a>.</div>` : ""}
    <div class="kick">${kindChip(a.kind, a.kind_label)}${dots(a.importance)}<span>${dmyTime(a.created)}</span>
      ${a.version > 1 ? `<span class="warnc">version ${a.version}</span>` : ""}<span>${Math.max(1, Math.round(words / 230))} min read</span></div>
    <h1>${esc(b.headline)}</h1>
    ${b.dek ? `<p class="dek">${esc(b.dek)}</p>` : ""}
    ${b.changed && prev ? `<div class="upd"><b>What changed</b> since ${dmyTime(prev.created)}: ${esc(b.changed)}</div>` : ""}
    ${(b.takeaways || []).length ? `<div class="take"><div class="lb">The short version</div><ul>${b.takeaways.map(t => `<li>${esc(t)}</li>`).join("")}</ul></div>` : ""}
    ${bookHtml(a.book, b.book)}
    <div class="prose">${prose}</div>
    ${numbersHtml(b.numbers)}
    ${(b.bull || []).length || (b.bear || []).length ? `<div class="bb">
      <div><div class="lb pos">The case for</div><ul>${(b.bull || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul></div>
      <div><div class="lb neg">The case against</div><ul>${(b.bear || []).map(x => `<li>${esc(x)}</li>`).join("")}</ul></div></div>` : ""}
    ${(b.watch || []).length ? `<div class="prose"><h2>On the calendar</h2></div><div class="watch">${b.watch.map(w => `<div class="w"><b>${w.d ? dmy(w.d) : "open"}</b><span>${esc(w.t)}</span></div>`).join("")}</div>` : ""}
    ${b.quote ? `<blockquote class="q">“${esc(b.quote.text)}”<cite>${esc(b.quote.who || "")}</cite></blockquote>` : ""}
    <div class="prose"><h2>Sources</h2></div>
    <ol class="srcs">${(b.sources || []).map(s => `<li><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a>
      · ${esc(s.publisher || "")}${s.used ? `<br><span class="u">${esc(s.used)}</span>` : ""}</li>`).join("")}</ol>
    ${a.versions.length > 1 ? `<div class="prose"><h2>This story so far</h2></div><div class="vers">${a.versions.slice().reverse().map(v =>
      `<div class="v">${v.id === a.id ? "<b>" : `<a href="#a=${v.id}" data-open="${v.id}">`}version ${v.version} · ${dmyTime(v.created)}${v.id === a.id ? "</b>" : "</a>"}
       ${v.changed ? `<br><span class="dim">${esc(v.changed)}</span>` : ""}</div>`).join("")}</div>` : ""}
    <div class="prose"><h2>Ask about this article</h2></div>
    <div class="ask"><textarea id="askQ" placeholder="e.g. How much of my book is exposed if oil stays above $110?"></textarea>
      <div style="margin-top:.35rem"><button class="tbtn" id="askBtn">Ask</button> <span class="dim" style="font-size:.64rem">answered in the session that wrote it, with its sources</span></div>
      <div id="answers">${answersHtml(a.answers, a.ask_job)}</div></div>
    <div class="meta">Written by ${esc(a.meta.model || "Claude")} for this terminal from the sources above, in its own words${
      a.meta.ms ? ` · ${Math.round(a.meta.ms / 1000)} s` : ""}${a.meta.api_equivalent_usd != null ? ` · about $${nf(a.meta.api_equivalent_usd, 2)} at API prices, on your plan` : ""}${
      a.cost_usd != null ? ` · $${nf(a.cost_usd, 2)}` : ""}. It describes exposure; what to do with it is yours.</div>
  </article>`;
  figs.forEach((f, i) => drawFig($("#fig" + i), f));
  $("#backBtn").onclick = e => { e.preventDefault(); $("#cols").classList.remove("reading"); history.replaceState(null, "", "#"); window.scrollTo(0, 0); };
  $$("[data-open]").forEach(x => x.onclick = e => { e.preventDefault(); openArticle(+x.dataset.open); });
  $("#askBtn").onclick = askIt;
  if (a.ask_job && a.ask_job.state === "running") pollAsk();
}

function chainHtml(c) {
  return `<div class="chain">${c.map((x, i) => `${i ? "<i>→</i>" : ""}<span>${esc(x)}</span>`).join("")}</div>`;
}
function bookHtml(bk, text) {
  if (!bk || !bk.available) return text ? `<div class="box"><div class="lb">What it means for your book</div><p>${esc(text)}</p></div>` : "";
  const rows = bk.rows || [];
  const secs = Object.entries(bk.sectors || {});
  return `<div class="box"><div class="lb">What it means for your book</div>
    ${rows.length ? `<div class="tscroll"><table class="btab"><thead><tr><th>Holding</th><th>Value</th><th>Weight</th><th>Today</th></tr></thead><tbody>
      ${rows.map(r => `<tr><td><a class="tkr" href="/t/${encodeURIComponent(r.t)}">${esc(r.t)}</a> <span class="dim">${esc((r.name || "").slice(0, 30))}${r.turbo ? " · turbo, exposure " + eur(r.exposure) : ""}</span></td>
        <td>${eur(r.value)}</td><td>${nf(r.w, 1)}%</td>${r.day || r.last == null
          ? `<td class="${sgn(r.day)}">${sp(r.day)}</td>`
          : `<td class="${sgn(r.last)}" title="the last session: the day starts again at the close">${sp(r.last)}<span class="dim">*</span></td>`}</tr>`).join("")}
      <tr><td class="dim">together</td><td><b>${eur(bk.value)}</b></td><td><b>${nf(bk.weight, 1)}%</b></td><td></td></tr></tbody></table></div>`
      : `<div class="dim" style="font-size:.72rem">None of the article's companies is in your book directly.</div>`}
    ${rows.some(r => !r.day && r.last != null) ? `<div class="dim" style="font-size:.66rem;margin-top:.2rem">* the last session's move: the day starts again at each market's close.</div>` : ""}
    ${secs.length ? `<div class="dim" style="font-size:.7rem;margin-top:.3rem">Your weight in ${secs.map(([k, v]) => `${esc(k)} ${nf(v, 1)}%`).join(", ")}</div>` : ""}
    ${text ? `<p>${esc(text)}</p>` : ""}</div>`;
}
function numbersHtml(n) {
  if (!n || !(n.rows || []).length) return "";
  return `<div class="fig"><div class="ft">${esc(n.title || "The numbers")}</div><div class="tscroll"><table class="btab">
    ${(n.cols || []).length ? `<thead><tr>${n.cols.map(c => `<th>${esc(c)}</th>`).join("")}</tr></thead>` : ""}
    <tbody>${n.rows.map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>
    ${n.note ? `<div class="fs">${esc(n.note)}</div>` : ""}</div>`;
}

/* ---------- the figures: drawn here from real data ---------- */
function drawFig(host, f) {
  if (!host || !f) return;
  if (f.type === "price") return drawPrice(host, f);
  if (f.type === "map") return drawMap(host, f);
  if (f.type === "timeline") {
    host.innerHTML = `<div class="ft">${esc(f.title || "Timeline")}</div><div class="tl">${f.events.map(e => `<div class="e"><b>${dmy(e.d)}</b>${esc(e.t)}</div>`).join("")}</div>`;
    return;
  }
  if (f.type === "bars") {
    const mx = Math.max(...f.items.map(x => Math.abs(x.v)), 1e-9);
    host.innerHTML = `<div class="ft">${esc(f.title || "")}</div><div class="bars">${f.items.map(x => `<div class="b"><span class="l" title="${esc(x.k)}">${esc(x.k)}</span>
      <span class="t"><i style="left:0;width:${(Math.abs(x.v) / mx * 100).toFixed(1)}%;${x.v < 0 ? "background:var(--loss)" : ""}"></i></span>
      <span class="v">${nf(x.v, Math.abs(x.v) >= 100 ? 0 : 1)}${esc(f.unit || "")}</span></div>`).join("")}</div>
      <div class="fs">Source: ${esc(f.source || "")}</div>`;
  }
}
function drawPrice(host, f) {
  const ser = f.series || [];
  if (!ser.length) { host.innerHTML = `<div class="ft">${esc(f.title || "")}</div><div class="dim">No prices for ${esc((f.tickers || []).join(", "))}.</div>`; return; }
  host.innerHTML = `<div class="ft">${esc(f.title || "")}</div><div class="lc" id="${host.id}c"></div><div class="fs">${
    f.rebase ? `Rebased to 100 on ${dmy(ser[0].pts[0][0])}. ` : ""}${f.mark ? `Shaded: ${dmy(f.mark)}. ` : ""}${
    (f.missing || []).length ? `No prices for ${esc(f.missing.join(", "))}. ` : ""}Daily closes; change over the chart: ${ser.map(s => `${esc(s.sym)} ${sp(s.chg)}`).join(", ")}.</div>`;
  const next = d => { const t = new Date(d + "T12:00:00"); t.setDate(t.getDate() + 1); return t.toISOString().slice(0, 10); };
  lchart($(`#${host.id}c`), {
    height: 250, endLabels: true, yfmt: f.rebase ? v => nf(v, 0) : v => nf(v, 2), refs: f.rebase ? [{ y: 100 }] : [],
    bands: f.mark ? [[f.mark, next(f.mark)]] : [],
    series: ser.map((s, i) => ({ name: s.sym, color: COLORS[i % COLORS.length], w: i ? 1.3 : 1.9, pts: s.pts })) });
}
async function land() {
  if (!LAND) LAND = await (await fetch("/land.json")).json();
  return LAND;
}
async function drawMap(host, f) {
  host.innerHTML = `<div class="ft">${esc(f.title || "Map")}</div><div class="map" id="${host.id}m"><div class="dim">drawing<span class="blink">_</span></div></div>`;
  let L;
  try { L = await land(); } catch (e) { $(`#${host.id}m`).innerHTML = `<div class="dim">The map outlines did not load.</div>`; return; }
  const hi = new Set(f.countries || []);
  let x0 = 180, x1 = -180, y0 = 90, y1 = -90;
  const add = (lo, la) => { x0 = Math.min(x0, lo); x1 = Math.max(x1, lo); y0 = Math.min(y0, la); y1 = Math.max(y1, la); };
  (f.points || []).forEach(p => add(p.lon, p.lat));
  for (const c of L.c) if (hi.has(c.iso)) for (const r of c.p) for (let i = 0; i < r.length; i += 2) add(r[i], r[i + 1]);
  if (x0 > x1) { x0 = -20; x1 = 60; y0 = 10; y1 = 60; }
  const span = Math.max(x1 - x0, (y1 - y0) * 1.6, 16), cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  x0 = Math.max(-180, cx - span * 0.65); x1 = Math.min(180, cx + span * 0.65);
  y0 = Math.max(-75, cy - span * 0.42); y1 = Math.min(82, cy + span * 0.42);
  const k = Math.cos(cy * Math.PI / 180), W = 760, s = W / ((x1 - x0) * k), H = Math.round((y1 - y0) * s);
  const X = lo => ((lo - x0) * k * s).toFixed(1), Y = la => ((y1 - la) * s).toFixed(1);
  let g = "";
  for (const c of L.c) {
    const on = hi.has(c.iso);
    for (const r of c.p) {
      let inb = false;
      for (let i = 0; i < r.length; i += 2) if (r[i] >= x0 - 5 && r[i] <= x1 + 5 && r[i + 1] >= y0 - 5 && r[i + 1] <= y1 + 5) { inb = true; break; }
      if (!inb) continue;
      let d = "";
      for (let i = 0; i < r.length; i += 2) d += (i ? "L" : "M") + X(r[i]) + " " + Y(r[i + 1]);
      g += `<path d="${d}Z" fill="${on ? "rgba(79,168,206,.32)" : "#142028"}" stroke="${on ? "var(--accent)" : "#2a3a44"}" stroke-width="${on ? 1.1 : 0.6}"><title>${esc(c.name)}</title></path>`;
    }
  }
  for (const c of L.c) {
    if (!hi.has(c.iso)) continue;
    let sx = 0, sy = 0, n = 0;
    const r = c.p.reduce((a, b) => (b.length > a.length ? b : a), []);
    for (let i = 0; i < r.length; i += 2) { sx += r[i]; sy += r[i + 1]; n++; }
    if (n) g += `<text x="${X(sx / n)}" y="${Y(sy / n)}" text-anchor="middle" fill="#9fc3d4" font-size="11" font-family="IBM Plex Mono" opacity=".9">${esc(c.name)}</text>`;
  }
  for (const p of f.points || []) {
    g += `<circle cx="${X(p.lon)}" cy="${Y(p.lat)}" r="4.5" fill="var(--warn)" stroke="#05080A" stroke-width="1.5"/>
      <text x="${(+X(p.lon) + 8).toFixed(1)}" y="${(+Y(p.lat) + 4).toFixed(1)}" fill="#F2E3C2" font-size="12" font-family="Public Sans" paint-order="stroke" stroke="#05080A" stroke-width="3">${esc(p.label || "")}</text>`;
  }
  $(`#${host.id}m`).innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(f.title || "map")}">${g}</svg>`;
  host.insertAdjacentHTML("beforeend", `<div class="fs">Outlines: Natural Earth (public domain).</div>`);
}

/* ---------- asking ---------- */
/* Answers come back as Markdown (bold, lists, now and then a table). Only
   relative links and http(s) survive; everything else is escaped first. */
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
    if ((m = l.match(/^(#{1,3})\s+(.*)$/))) { flush(); endList(); out += `<h${m[1].length + 2}>${mdInline(m[2])}</h${m[1].length + 2}>`; continue; }
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
function answersHtml(list, job) {
  return (list || []).map(x => `<div class="ans"><div class="qq">Q: ${esc(x.question)} · ${dmyTime(x.created)}</div><div class="md">${md(x.body)}</div></div>`).join("")
    + (job && job.state === "running" ? `<div class="ans"><div class="qq">answering<span class="blink">_</span></div>${job.text ? `<div class="md"><p>${esc(job.text)}</p></div>` : ""}</div>`
      : job && job.state === "error" ? `<div class="note"><b>No answer</b>${esc(job.error || "")}</div>` : "");
}
async function askIt() {
  const q = $("#askQ").value.trim();
  if (!q) return;
  try { const j = await post(`/read/a/${A.id}/ask`, { question: q }); $("#answers").innerHTML = answersHtml(A.answers, j); $("#askQ").value = ""; pollAsk(); }
  catch (e) { flash(esc(e.message)); }
}
function pollAsk() {
  clearTimeout(ASKPOLL);
  const id = A.id;
  ASKPOLL = setTimeout(async () => {
    let r;
    try { r = await api(`/read/a/${id}/answers`); } catch (e) { return; }
    if (!A || A.id !== id) return;
    A.answers = r.answers;
    $("#answers").innerHTML = answersHtml(r.answers, r.job);
    if (r.job && r.job.state === "running") pollAsk();
  }, 2500);
}

window.addEventListener("hashchange", () => { const m = location.hash.match(/a=(\d+)/); if (m && (!A || A.id !== +m[1])) openArticle(+m[1]); });
load();
