/* ==========================================================================
   SPLC — who sells to whom, in the companies' own words.

   /m/SPLC          your book laid out along its supply links
   /m/SPLC/NVDA     one company between its suppliers and its customers, with
                    its rivals, partners and owners

   Every line on these pages is one sentence of one filing, and the sentence
   is a click away. The map that ships was read once, for the book as it
   stood; "Read with Claude" reads a company it has not seen, on your plan.
   ========================================================================== */

const LAST = decodeURIComponent(location.pathname.replace(/\/+$/, "").split("/").pop() || "");
const SYM = /^splc$/i.test(LAST) ? null : (LAST.startsWith("~") ? LAST.toLowerCase() : LAST.toUpperCase());
const ST = { sel: null, C: null, D: null, open: new Set() };
const enc = encodeURIComponent;

const href = n => "/m/SPLC/" + enc(n.sym || n.key);
const wpc = w => w == null ? "" : (w >= 10 ? w.toFixed(0) : w.toFixed(1)) + "%";
const tag = n => n.held ? `<span class="mine" title="In your book: ${wpc(n.weight)} of it">${wpc(n.weight)}</span>`
  : (n.listed === false ? `<span class="unl" title="Not a listed company">not listed</span>` : "");
const when = d => /^\d{4}-\d{2}-\d{2}/.test(d || "") ? dmy(d) : /^\d{4}-\d{2}$/.test(d || "") ? d.slice(5) + "/" + d.slice(0, 4) : (d || "");
/* Whose document: "Nvidia's 10-K", but a document with a name of its own ("Apple Supplier List") is just that. */
const docName = e => e.title ? esc(e.title) : `${esc(e.by_label)}'s ${esc(e.form || "filing")}`;
const cut = (s, n) => s.length > n ? s.slice(0, Math.max(1, n - 1)) + "…" : s;
const pnum = p => (p % 1 ? p.toFixed(1) : p.toFixed(0)) + "%";
/* "67% of CoreWeave's revenue" is left out when the description already says 67%. */
const shareLine = r => r.share && !(r.what || "").includes(pnum(r.pct)) ? `<span class="warnc">${esc(r.share)}.</span> ` : "";

$("#shell").innerHTML = termBar({ here: SYM ? `SPLC · ${SYM}` : "SPLC", right: `<button class="tbtn" id="reloadBtn">Reload</button>` });
wireShell();
$("#reloadBtn").onclick = () => SYM ? loadCo() : loadBook();

const goBox = () => `<span class="go"><input id="goSym" placeholder="ticker" aria-label="Open another company's supply chain"><button class="seg" id="goBtn">Open</button></span>`;
function wireGo() {
  const i = $("#goSym"), go = () => { const s = i.value.trim().toUpperCase(); if (s) location.assign("/m/SPLC/" + enc(s)); };
  if (!i) return;
  i.addEventListener("keydown", e => { if (e.key === "Enter") go(); });
  $("#goBtn").onclick = go;
}

/* ==========================================================================
   the book
   ========================================================================== */
async function loadBook() {
  let C;
  try { C = await (await fetch("/api/pf/supply")).json(); } catch (e) { C = { ok: false, error: String(e.message || e) }; }
  ST.C = C;
  renderBook(C);
}

function renderBook(C) {
  if (!C.ok) {
    $("#app").innerHTML = `<div class="panel"><div class="body"><div class="note"><b>Could not build the map</b>${esc(C.error || "")}</div></div></div>`;
    return;
  }
  const top = (C.shares || []).find(s => s.a.held && s.basis === "a_revenue");
  const stale = (C.rows || []).filter(r => r.newer);
  const unread = (C.rows || []).filter(r => r.filer && !r.read);
  $("#app").innerHTML = `
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>SUPPLY CHAIN</h1><span class="sub">who sells to whom, in the companies' own words</span><span class="sp"></span>${goBox()}</div>
    <div class="tiles">
      ${tile("On the chain", `${C.n_linked} <small>of ${C.n_book}</small>`, null, "shares tied to another by a supplier or customer link", true)}
      ${tile("Of the book", `${C.w_linked.toFixed(0)}%`, null, "the weight of those shares")}
      ${tile("Largest dependency", top ? `${top.pct.toFixed(0)}%` : "–", null, top ? `of ${esc(top.a.label)}'s revenue comes from ${esc(top.b.label)}` : "no share of revenue disclosed")}
      ${tile("Sentences read", String(C.n_links), null, "each link is one sentence of one filing")}
      ${tile("Read on", when(C.read_on), null, stale.length ? `<span class="warnc">${stale.length} compan${stale.length === 1 ? "y has" : "ies have"} filed a newer annual report since</span>` : "no holding has filed a newer annual report since")}
    </div>
    <div class="prose" style="margin-top:.6rem">A company's annual report has to say who it depends on: the foundry that makes its chips, the customer that is a tenth of its sales, the rivals it loses orders to. Its results releases name the customers taking the new product. This page is those statements, joined up for the shares you hold. <b>It is a floor, not a census.</b> A customer the filing calls "Customer A" stays unnamed here, Apple does not name TSMC in its report, and three of your holdings file nothing in the US at all.</div>
  </div></div>

  <div class="sg">
    <div class="stack">
    <div class="panel"><header><h2>Your book along the chain</h2><span class="hint">suppliers to the left, their customers to the right · click a company to follow its links</span></header>
      <div class="body"><div class="mapwrap"><div id="map"></div></div>
      <div class="mapkey">
        <span><i class="bx"></i>in your book, with its weight</span>
        <span><i class="bx o"></i>not in your book: drawn because it ties ${C.hub_min} or more of your holdings</span>
        <span><i class="ln"></i>sells to</span>
        <span><i class="ln t"></i>a disclosed share of revenue</span>
        <span><i class="ln b"></i>runs against the flow</span>
      </div>
      <div class="src">Left to right is upstream to downstream: each company is placed so that, across the whole web, a customer sits about one step to the right of its supplier. Where two companies sell to each other (Nvidia sells chips to Amazon, Amazon's cloud sells to Nvidia) the heavier link decides and the other is dashed.</div></div></div>
      <div class="panel"><header><h2>Selected</h2><span class="hint" id="selHint">nothing yet</span></header><div class="body" id="selBox"></div></div>
    </div>
    <div class="panel"><header><h2>What it says</h2></header><div class="body"><ul class="say">${(C.say || []).map(s => `<li>${esc(s)}</li>`).join("")}</ul></div></div>
  </div>

  <div class="panel" id="s-shares"><header><h2>Who depends on whom</h2><span class="hint">every share of sales or purchases a filing puts a number on</span></header><div class="body">
    ${(C.shares || []).length ? `<table class="tbl"><thead><tr><th class="l">Seller</th><th class="l">Buyer</th><th>Share</th><th class="l">In the filing's words</th><th class="l">Source</th></tr></thead><tbody>
    ${C.shares.map(s => `<tr><td><a href="${href(s.a)}"><b>${esc(s.a.label)}</b></a> ${tag(s.a)}</td><td class="l"><a href="${href(s.b)}"><b>${esc(s.b.label)}</b></a> ${tag(s.b)}</td>
      <td><b class="${s.pct >= 25 ? "warnc" : ""}">${s.pct % 1 ? s.pct.toFixed(1) : s.pct.toFixed(0)}%</b><span class="pbar ${s.pct >= 25 ? "hi" : ""}"><i style="width:${Math.min(100, s.pct)}%"></i></span>
        <div class="dim" style="font-size:.62rem">${esc((s.share || "").replace(/^[\d.]+% /, ""))}</div></td>
      <td class="q" title="${esc(s.share || "")}">${esc(s.quote || s.what || "")}</td>
      <td class="l">${s.url ? `<a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.form || "filing")} ${when(s.filed)}</a>` : esc(s.form || "")}</td></tr>`).join("")}
    </tbody></table>` : `<div class="lc-empty">No filing puts a number on a link that touches your book.</div>`}
    <div class="src">A share is always a share of something: of the seller's revenue where the filing says "X was 67% of our revenue", of the buyer's purchases where it says "X made 95% of our wafers". What a supplier is as a share of its customer's costs is almost never disclosed, and is not estimated here.</div>
  </div></div>

  <div class="panel" id="s-all"><header><h2>Every holding</h2><span class="hint">what its filings name · click a row to open it</span></header><div class="body">
    <table class="tbl"><thead><tr><th class="l">Holding</th><th>Weight</th><th title="Companies named as selling to it">Suppliers</th><th title="Companies named as buying from it">Customers</th>
      <th title="How many of those are your own holdings">In your book</th><th title="Named as competitors">Rivals</th><th class="l">Read from</th><th class="l"></th></tr></thead><tbody>
    ${(C.rows || []).map(r => `<tr class="pick" data-s="${esc(r.sym)}"><td><a href="/m/SPLC/${enc(r.sym)}"><b>${esc(r.label)}</b></a> <span class="dim">${esc(r.sym)}</span></td>
      <td>${wpc(r.weight)}</td><td>${r.suppliers || `<span class="dim">–</span>`}</td><td>${r.customers || `<span class="dim">–</span>`}</td>
      <td>${r.in_book ? `<b>${r.in_book}</b>` : `<span class="dim">–</span>`}</td><td>${r.competitors || `<span class="dim">–</span>`}</td>
      <td class="l">${r.read ? `${esc(r.read.form || "annual report")} of ${when(r.read.filed)}` : r.filer ? `<span class="warnc">not read yet</span>` : `<span class="dim">files nothing with the SEC</span>`}</td>
      <td class="l">${r.newer ? `<span class="bdg warn" title="A newer annual report was filed on ${when(r.latest.filed)}">newer report filed</span>` : ""}${!r.on_map && r.read ? `<span class="dim">outside the chain</span>` : ""}</td></tr>`).join("")}
    </tbody></table>
    <div class="src">${unread.length ? `${unread.length} holding${unread.length === 1 ? " has" : "s have"} an annual report on file that nobody has read yet: open it and press Read with Claude. ` : ""}The reading that ships with the terminal was done once, on ${when(C.read_on)}. A company that has filed a newer annual report since is flagged; open it and have it read again.</div>
  </div></div>`;
  wireGo(); wrapTables();
  $$("tr.pick").forEach(tr => tr.onclick = e => { if (!e.target.closest("a")) location.assign("/m/SPLC/" + enc(tr.dataset.s)); });
  drawMap();
  renderSel();
}

/* ---------- the map ------------------------------------------------------- */
function drawMap() {
  const C = ST.C, host = $("#map");
  if (!host || !C || !C.nodes.length) { if (host) host.innerHTML = `<div class="lc-empty">None of your holdings names another as a supplier or a customer.</div>`; return; }
  const W = Math.max(host.clientWidth, 940), cols = C.cols;
  const colN = Array(cols).fill(0);
  C.nodes.forEach(n => colN[n.col] = Math.max(colN[n.col], n.order + 1));
  const maxN = Math.max(...colN), rowH = 31, nodeH = 22, padT = 22, padB = 8;
  const H = padT + maxN * rowH + padB;
  const nodeW = Math.max(96, Math.min(172, Math.floor((W - 12) / cols) - 22));
  const cx = c => 6 + nodeW / 2 + c * ((W - 12 - nodeW) / Math.max(1, cols - 1));
  const P = {};
  C.nodes.forEach(n => P[n.key] = { x: cx(n.col), y: padT + (maxN - colN[n.col]) * rowH / 2 + n.order * rowH + nodeH / 2, n });
  const wmax = Math.max(...C.nodes.map(n => n.weight || 0), 1);
  const path = e => {
    const a = P[e.a], b = P[e.b];
    if (a.n.col === b.n.col) {                       // same column: out and back on the right-hand side
      const x = a.x + nodeW / 2, bow = 26 + Math.min(40, Math.abs(a.y - b.y) * 0.25);
      return `M${x} ${a.y} C${x + bow} ${a.y},${x + bow} ${b.y},${x} ${b.y}`;
    }
    const fwd = a.n.col < b.n.col;
    const x1 = a.x + (fwd ? nodeW / 2 : -nodeW / 2), x2 = b.x + (fwd ? -nodeW / 2 : nodeW / 2), dx = (x2 - x1) * 0.5;
    return `M${x1} ${a.y} C${x1 + dx} ${a.y},${x2 - dx} ${b.y},${x2} ${b.y}`;
  };
  const sw = e => e.pct != null && e.basis === "a_revenue" ? 1 + Math.min(5, e.pct / 14) : 1;
  const chars = n => Math.max(6, Math.floor((nodeW - (n.held ? 40 : 14)) / 6.4));
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Your holdings laid out from suppliers on the left to customers on the right, with a line for each supply link their filings disclose">
    <text class="cap" x="6" y="11">upstream · suppliers</text><text class="cap" x="${W - 6}" y="11" text-anchor="end">customers · downstream</text>
    <g id="mEdges">${C.edges.map((e, i) => `<path class="ed ${e.back ? "back" : ""}" data-i="${i}" d="${path(e)}" stroke-width="${sw(e)}"/>`).join("")}</g>
    <g id="mHits">${C.edges.map((e, i) => `<path class="hit" data-i="${i}" d="${path(e)}"/>`).join("")}</g>
    <g id="mNodes">${C.nodes.map(n => { const p = P[n.key], x = p.x - nodeW / 2, y = p.y - nodeH / 2;
      return `<g class="nd ${n.held ? "held" : "out"}" data-k="${esc(n.key)}" tabindex="0" role="button" aria-label="${esc(n.label)}">
        <rect x="${x}" y="${y}" width="${nodeW}" height="${nodeH}"/>
        ${n.held ? `<rect class="bar" x="${x}" y="${y + nodeH - 2}" width="${Math.max(2, nodeW * n.weight / wmax)}" height="2" style="stroke:none"/>` : ""}
        <text x="${x + 6}" y="${p.y + 3.6}">${esc(cut(n.label.length <= chars(n) ? n.label : (n.short || n.label), chars(n)))}</text><title>${esc(n.label)}</title>
        ${n.held ? `<text class="w" x="${x + nodeW - 5}" y="${p.y + 3.4}" text-anchor="end">${wpc(n.weight)}</text>` : ""}</g>`; }).join("")}</g>
  </svg><div class="mtip" hidden></div>`;
  const tip = host.querySelector(".mtip");
  const show = (html, ev) => { tip.innerHTML = html; tip.hidden = false;
    const r = host.getBoundingClientRect(); let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 12;
    if (x + tip.offsetWidth > r.width) x = Math.max(0, ev.clientX - r.left - tip.offsetWidth - 10);
    tip.style.left = x + "px"; tip.style.top = y + "px"; };
  host.querySelectorAll(".hit").forEach(h => {
    h.onmousemove = ev => { const e = C.edges[+h.dataset.i];
      show(`<b>${esc(P[e.a].n.label)} → ${esc(P[e.b].n.label)}</b>${e.share ? `<br><span class="warnc">${esc(e.share)}</span>` : ""}<br>${esc(e.what || "")}<br><span class="dim">${esc(e.by.join(", "))} · ${esc(e.form || "")} ${when(e.filed)}${e.n > 1 ? ` · ${e.n} sentences` : ""}</span>`, ev); };
    h.onmouseleave = () => tip.hidden = true;
  });
  host.querySelectorAll("g.nd").forEach(g => {
    const k = g.dataset.k;
    g.onmouseenter = () => { if (!ST.sel) paint(k); };
    g.onmouseleave = () => { if (!ST.sel) paint(null); };
    g.onclick = ev => { ev.stopPropagation(); ST.sel = ST.sel === k ? null : k; paint(ST.sel); renderSel(); };
    g.ondblclick = () => location.assign(href(P[k].n));
    g.onkeydown = ev => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); g.onclick(ev); } };
  });
  host.querySelector("svg").onclick = () => { if (ST.sel) { ST.sel = null; paint(null); renderSel(); } };
  paint(ST.sel);
}

function paint(k) {
  const C = ST.C, host = $("#map");
  if (!host) return;
  const near = new Set(k ? [k] : []);
  host.querySelectorAll(".ed").forEach(p => {
    const e = C.edges[+p.dataset.i], on = k && (e.a === k || e.b === k);
    if (on) { near.add(e.a); near.add(e.b); }
    p.classList.toggle("hot", !!on); p.classList.toggle("dim", !!k && !on);
  });
  host.querySelectorAll("g.nd").forEach(g => {
    g.classList.toggle("dim", !!k && !near.has(g.dataset.k));
    g.classList.toggle("hot", !!k && near.has(g.dataset.k) && g.dataset.k !== k);
    g.classList.toggle("sel", g.dataset.k === k);
  });
}

function renderSel() {
  const C = ST.C, box = $("#selBox"), k = ST.sel;
  if (!box) return;
  if (!k) {
    $("#selHint").textContent = "nothing yet";
    box.innerHTML = `<div class="prose">Click a company on the map: its suppliers and customers light up and are listed here. Double-click opens its own page, with every sentence.</div>
      ${(C.alone || []).length ? `<div class="sell">Outside the chain</div><div class="prose" style="font-size:.74rem">${C.alone.map(a => `<a href="${href(a)}">${esc(a.label)}</a>`).join(" · ")}</div>
      <div class="src">No filing ties these to another of your holdings as supplier or customer. That is information too: they do not move with the chain.</div>` : ""}`;
    return;
  }
  const by = Object.fromEntries(C.nodes.map(n => [n.key, n])), n = by[k];
  const row = (o, e) => `<div class="selr"><span><a href="${href(o)}"><b>${esc(o.label)}</b></a> ${tag(o)}</span>
    <span class="t">${shareLine(e)}${esc(e.what || "")}</span></div>`;
  const from = C.edges.filter(e => e.b === k).sort((a, b) => (by[b.a].weight || 0) - (by[a.a].weight || 0));
  const to = C.edges.filter(e => e.a === k).sort((a, b) => (b.pct || 0) - (a.pct || 0) || (by[b.b].weight || 0) - (by[a.b].weight || 0));
  $("#selHint").textContent = "click it again to let go";
  box.innerHTML = `<div class="selh"><b>${esc(n.label)}</b> ${tag(n)} <span class="sp"></span><a href="${href(n)}">every sentence ›</a></div>
    <div class="two even"><div><div class="sell">Buys from · ${from.length}</div>${from.map(e => row(by[e.a], e)).join("") || `<div class="dim" style="font-size:.72rem">nobody on this map</div>`}</div>
    <div><div class="sell">Sells to · ${to.length}</div>${to.map(e => row(by[e.b], e)).join("") || `<div class="dim" style="font-size:.72rem">nobody on this map</div>`}</div></div>`;
}

/* ==========================================================================
   one company
   ========================================================================== */
async function loadCo() {
  let D;
  try { D = await (await fetch("/api/pf/supply/" + enc(SYM))).json(); } catch (e) { D = { ok: false, error: String(e.message || e) }; }
  ST.D = D;
  renderCo(D);
  if (D.ok) { loadMentions(); if (D.job && D.job.state === "running") setTimeout(pollRead, 1500); }
}

const srcLine = e => `${esc(e.by_label)} · ${e.url ? `<a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.title || e.form || "filing")}${e.filed ? " of " + when(e.filed) : ""}</a>` : esc(e.form || "")}${e.source === "claude" ? " · read by Claude for you" : ""}`;

function relRow(r, id) {
  const e0 = r.ev[0], open = ST.open.has(id);
  const bar = r.pct != null && (r.basis === "a_revenue" || r.basis === "b_purchases");
  return `<div class="rel ${r.held ? "held" : ""} ${open ? "open" : ""}" data-id="${esc(id)}" data-pct="${r.pct ?? ""}" data-held="${r.held ? 1 : 0}">
    <div class="rh"><a class="nm" href="${href(r)}">${esc(r.label)}</a>${tag(r)}${r.share ? `<span class="sh" title="${esc(r.share)}">${r.pct % 1 ? r.pct.toFixed(1) : r.pct.toFixed(0)}%</span>` : ""}</div>
    ${bar ? `<div class="shb" title="${esc(r.share)}"><i style="width:${Math.min(100, r.pct)}%"></i></div>` : ""}
    <div class="rw">${shareLine(r)}${esc(r.what || "")}</div>
    <div class="rs">${docName(e0)}${e0.filed ? ", " + when(e0.filed) : ""}${r.ev.length > 1 ? ` · ${r.ev.length} sentences` : ""}</div>
    ${open ? `<div class="ev">${r.ev.map(e => `<blockquote>${esc(e.quote || "")}</blockquote><div class="es">${srcLine(e)}${e.what && e.what !== r.what ? ` · ${esc(e.what)}` : ""}</div>`).join("")}</div>` : ""}
  </div>`;
}

function renderCo(D) {
  if (!D.ok) {
    $("#app").innerHTML = `<div class="panel"><div class="body"><div class="hdr"><h1>${esc(SYM)}</h1><span class="sp"></span>${goBox()}</div>
      <div class="note"><b>Could not load</b>${esc(D.error || "")}</div><div class="hd-links"><a href="/m/SPLC">‹ the whole book</a></div></div></div>`;
    wireGo(); return;
  }
  const R = D.relations, S = D.state || {}, cu = R.customers, su = R.suppliers;
  const mine = [...new Map([...su, ...cu].filter(r => r.held).map(r => [r.key, r])).values()];
  const top = cu.find(r => r.basis === "a_revenue" && r.pct != null);
  const readTxt = S.read ? `${esc(S.read.form || "annual report")} of ${when(S.read.filed)}` : S.filer ? "not read yet" : "nothing to read";
  const canRead = S.filer && D.listed;
  const btn = !canRead ? "" : `<button class="seg ${!S.read || S.newer ? "on" : ""}" id="readBtn" title="Claude reads the sentences cut from its annual report and results releases and says who is what. About a minute; it uses your Claude plan, or your API key if that is what the reports are set to.">${!S.read ? "Read with Claude" : S.newer ? "Read the new report" : "Read again"}</button>`;
  /* Apple's supplier list names thirty companies in the same words. Those you
     hold keep a box each; the rest share one, as names to click. */
  const lists = k => {
    const n = {}, rows = R[k];
    rows.forEach(r => { if (!r.held) n[r.what] = (n[r.what] || 0) + 1; });
    const many = w => n[w] >= 6, done = new Set();
    return rows.map(r => {
      if (r.held || !many(r.what)) return relRow(r, k + ":" + r.key);
      if (done.has(r.what)) return "";
      done.add(r.what);
      const grp = rows.filter(x => !x.held && x.what === r.what), e0 = r.ev[0];
      return `<div class="rel many" data-pct="" data-held="0" style="cursor:auto"><div class="rw" style="margin:0 0 .25rem"><b style="color:var(--ink)">${grp.length} more</b> ${esc(r.what)}</div>
        <div class="glist">${grp.map(x => `<a href="${href(x)}" title="${esc((x.ev[0] || {}).quote || "")}">${esc(x.label)}</a>`).join(" · ")}</div>
        <div class="rs">${e0.url ? `<a href="${esc(e0.url)}" target="_blank" rel="noopener">${esc(e0.title || e0.form || "document")}</a>` : esc(e0.title || e0.form || "")}${e0.filed ? ", " + when(e0.filed) : ""}</div></div>`;
    }).join("");
  };
  const others = [...R.owners.map(r => ["Owns part of it", r, "owners"]), ...R.stakes.map(r => ["It owns part of", r, "stakes"]), ...R.partners.map(r => ["Partner", r, "partners"])];
  $("#app").innerHTML = `
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>${esc(D.label)}</h1><span class="sub">${esc(D.sym || "not listed")}${D.held ? ` · <span class="pos">${wpc(D.weight)} of your book</span>` : " · not in your book"}${D.sector ? " · " + esc(D.sector) : ""}</span>
      <span class="sp"></span>${goBox()}${btn}</div>
    <div class="tiles">
      ${tile("Suppliers named", String(su.length), null, su.length ? esc(su.slice(0, 3).map(r => r.label).join(", ")) : "none in what was read", true)}
      ${tile("Customers named", String(cu.length), null, cu.length ? esc(cu.slice(0, 3).map(r => r.label).join(", ")) : "none in what was read")}
      ${tile("In your book", String(mine.length), null, mine.length ? `${mine.reduce((a, r) => a + (r.weight || 0), 0).toFixed(1)}% of it sits on either side` : "none of them is a holding")}
      ${tile("Largest customer", top ? `${top.pct % 1 ? top.pct.toFixed(1) : top.pct.toFixed(0)}%` : "–", null, top ? `of revenue: ${esc(top.label)}` : "no share of revenue with a name on it")}
      ${tile("Rivals named", String(R.competitors.length), null, R.competitors.length ? esc(R.competitors.slice(0, 3).map(r => r.label).join(", ")) : "none")}
      ${tile("Read from", S.read ? when(S.read.filed) : "–", null, S.newer ? `<span class="warnc">a newer ${esc(S.latest.form)} was filed ${when(S.latest.filed)}</span>` : readTxt)}
    </div>
    <div class="hd-links"><a href="/m/SPLC">‹ the whole book</a>${D.sym ? `<a href="/t/${enc(D.sym)}">${esc(D.sym)} research ›</a><a href="/m/OPT/${enc(D.sym)}">options ›</a><a href="/m/ERN/${enc(D.sym)}">earnings ›</a>` : ""}</div>
    <div id="jobBox"></div>
  </div></div>

  <div class="panel"><header><h2>What it says</h2><span class="hint">${S.read ? `read by ${esc(S.by || "Claude")}` : ""}</span></header><div class="body">
    <ul class="say">${(D.say || []).map(s => `<li>${esc(s)}</li>`).join("")}</ul></div></div>

  <div class="panel" id="s-flow"><header><h2>Between its suppliers and its customers</h2><span class="hint">click a line for the sentence behind it</span></header><div class="body">
    <div class="flow" id="flow">
      <div class="fcol l" id="fL"><div class="fcap">Sells to it · ${su.length}</div>${lists("suppliers") || `<div class="none">${S.read ? "Its filings name no supplier. Many companies keep their suppliers out of the annual report." : S.filer ? "Nothing read yet, and no other company's filing names it as a customer." : "No other company's filing names it as a customer."}</div>`}</div>
      <div class="fmid"><div class="me" id="fMe"><b>${esc(D.label)}</b><small>${D.held ? `${wpc(D.weight)} of your book` : "not in your book"}<br>${su.length} in · ${cu.length} out</small></div></div>
      <div class="fcol r" id="fR"><div class="fcap">Buys from it · ${cu.length}</div>${lists("customers") || `<div class="none">${S.read ? "Its filings name no customer." : S.filer ? "Nothing read yet, and no other company's filing names a customer of it." : "No other company's filing names a customer of it."}</div>`}</div>
      <svg class="wires" id="fWires" aria-hidden="true"></svg>
    </div>
    <div class="src">A green edge marks a company you hold. An amber figure is a share the filing states, and says what it is a share of. Some lines come from the other company's filing: Microsoft appears as Nvidia's customer because Nvidia's results release says so, not Microsoft's report.</div>
  </div></div>

  <div class="two even">
    <div class="panel"><header><h2>Rivals it names</h2><span class="hint">${R.competitors.length}</span></header><div class="body">
      ${R.competitors.length ? `<div class="chips">${lists("competitors")}</div>` : `<div class="lc-empty">No competitor is named in what was read.</div>`}</div></div>
    <div class="panel"><header><h2>Owners, stakes and partners</h2><span class="hint">${others.length}</span></header><div class="body">
      ${others.length ? `<div class="stack" style="gap:.3rem">${others.map(([lab, r, k]) => relRow(r, k + ":" + r.key).replace('<div class="rh">', `<div class="rh"><span class="unl">${lab}</span>`)).join("")}</div>` : `<div class="lc-empty">None named.</div>`}</div></div>
  </div>

  ${(D.notes || []).length ? `<div class="panel" id="s-notes"><header><h2>What it says without naming anyone</h2><span class="hint">concentration the filing admits to</span></header><div class="body">
    ${D.notes.map(n => `<div class="in info" style="grid-template-columns:1fr"><div class="ix">${esc(n.text)}${n.quote ? `<div class="src" style="margin-top:.15rem">“${esc(cut(n.quote, 420))}” — ${n.url ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.form || "filing")} ${when(n.filed)}</a>` : esc(n.form || "")}</div>` : ""}</div></div>`).join("")}
  </div></div>` : ""}

  <div class="panel" id="s-mentions"><header><h2>Named by others</h2><span class="hint">annual reports of the last fourteen months that name it · SEC full-text search</span></header>
    <div class="body" id="mentionsBox"><div class="center ld">Looking through other companies' filings<span class="blink">_</span></div></div></div>

  <div class="panel" id="s-read"><header><h2>What was read</h2><span class="hint">${(D.docs || []).length} document${(D.docs || []).length === 1 ? "" : "s"} with a sentence in use</span></header><div class="body">
    ${(D.docs || []).length ? `<table class="tbl"><thead><tr><th class="l">Document</th><th class="l">Filed</th><th>Sentences in use</th></tr></thead><tbody>
      ${D.docs.map(d => `<tr><td><a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title || d.form || "filing")}</a></td><td class="l">${when(d.filed)}</td><td>${d.n}</td></tr>`).join("")}</tbody></table>`
      : `<div class="prose">${S.filer ? "Nothing has been read for this company yet." : D.listed ? "This company files no annual report with the SEC, so there is nothing of its own to read. Lines above come from other companies naming it." : "Not a listed company: everything above comes from the filings of companies that deal with it."}</div>`}
    ${canRead ? `<div class="fbar" style="margin-top:.5rem"><button class="seg" id="psgBtn">Show the sentences a reader is given</button>
      ${S.source === "claude" ? `<span class="lbl" style="margin-left:.6rem">read for you by ${esc(S.by || "Claude")} on ${when((S.read.when || "").slice(0, 10))}</span><button class="seg" id="undoBtn" title="Drop this reading. If the company was on the map that shipped, that reading comes back.">Undo this reading</button>` : ""}</div><div id="psgBox"></div>` : ""}
    <div class="src">How a line gets here: the terminal cuts from the annual report and the last four results releases every sentence that names another organisation next to a word about selling, buying, making, competing or owning. A reader then says who is what, by sentence number. The quote under a line is the filing's own sentence, never the reader's words.</div>
  </div></div>`;
  wireGo(); wrapTables();
  $$(".rel").forEach(el => el.onclick = e => {
    if (e.target.closest("a") || e.target.closest(".ev") || el.classList.contains("many")) return;
    const id = el.dataset.id; ST.open.has(id) ? ST.open.delete(id) : ST.open.add(id);
    const y = scrollY; renderCo(ST.D); scrollTo(0, y); renderMentions(); renderJob(ST.D.job);
  });
  if ($("#readBtn")) $("#readBtn").onclick = startRead;
  if ($("#psgBtn")) $("#psgBtn").onclick = showPassages;
  if ($("#undoBtn")) $("#undoBtn").onclick = async () => { await fetch(`/api/pf/supply/${enc(SYM)}/read`, { method: "DELETE" }); loadCo(); };
  wires(); renderJob(D.job);
}

/* The lines from each supplier into the company and out to each customer.
   Drawn from where the boxes actually are, so they follow a row that opens. */
function wires() {
  const flow = $("#flow"), svg = $("#fWires"), me = $("#fMe");
  if (!flow || !svg || !me || getComputedStyle(svg).display === "none") return;
  const fr = flow.getBoundingClientRect(), mr = me.getBoundingClientRect();
  const one = (el, left) => {
    const r = el.getBoundingClientRect(), y1 = r.top - fr.top + Math.min(r.height / 2, 16);
    const x1 = left ? r.right - fr.left : r.left - fr.left, x2 = left ? mr.left - fr.left : mr.right - fr.left;
    const y2 = mr.top - fr.top + mr.height / 2, dx = (x2 - x1) * 0.55, p = parseFloat(el.dataset.pct);
    return `<path class="${el.dataset.held === "1" ? "h" : ""}" stroke-width="${isNaN(p) ? 1 : 1 + Math.min(5, p / 14)}" d="M${x1} ${y1} C${x1 + dx} ${y1},${x2 - dx} ${y2},${x2} ${y2}"/>`;
  };
  svg.innerHTML = [...$$("#fL .rel")].map(el => one(el, true)).join("") + [...$$("#fR .rel")].map(el => one(el, false)).join("");
}
addEventListener("resize", () => { clearTimeout(wires._t); wires._t = setTimeout(() => { wires(); if (!SYM && ST.C && ST.C.ok) drawMap(); }, 150); });
addEventListener("scroll", () => { if (SYM) { clearTimeout(wires._s); wires._s = setTimeout(wires, 60); } }, { passive: true });

/* ---------- named by others ----------------------------------------------- */
let MEN = null;
async function loadMentions() {
  try { MEN = await (await fetch(`/api/pf/supply/${enc(SYM)}/mentions`)).json(); } catch (e) { MEN = { ok: false, error: String(e.message || e), rows: [] }; }
  renderMentions();
}
function renderMentions() {
  const box = $("#mentionsBox"), M = MEN;
  if (!box || !M) return;
  if (!M.ok) { box.innerHTML = `<div class="lc-empty">The SEC's search did not answer${M.error ? ": " + esc(M.error) : ""}.</div>`; return; }
  if (!M.rows.length) { box.innerHTML = `<div class="lc-empty">No other company's annual report of the last fourteen months names “${esc(M.q)}”.</div>`; return; }
  box.innerHTML = `<table class="tbl"><thead><tr><th class="l">Named in the report of</th><th class="l">Filed</th><th class="l">A sentence that names it</th></tr></thead><tbody>
    ${M.rows.map(r => `<tr><td>${r.ticker ? `<a href="/m/SPLC/${enc(r.ticker)}"><b>${esc(r.ticker)}</b></a> ` : ""}<span class="${r.ticker ? "dim" : ""}">${esc(cut(r.name, 34))}</span>${r.held ? ` <span class="mine">yours</span>` : ""}</td>
      <td class="l"><a href="${esc(r.url)}" target="_blank" rel="noopener">${esc(r.form)} ${when(r.filed)}</a></td>
      <td class="q">${r.quote ? esc(r.quote) : `<span class="dim">${r.opened ? "named only in a table or an exhibit" : "not opened"}</span>`}</td></tr>`).join("")}</tbody></table>
    <div class="src">Searched for “${esc(M.q)}”${M.total != null ? `: ${M.total} documents` : ""}. These are leads: nobody has read them, a sentence may be about a lawsuit or a namesake, and only the first ${M.opened || 0} reports were opened for their sentence. A company that matters here can be opened and read in full.</div>`;
  wrapTables();
}

/* ---------- reading ------------------------------------------------------- */
function renderJob(j) {
  const box = $("#jobBox");
  if (!box) return;
  if (!j) { box.innerHTML = ""; return; }
  if (j.state === "running") box.innerHTML = `<div class="job run">Claude is reading ${esc(ST.D.label)}: ${esc(j.phase === "writing" ? "saying who is what" : (j.phase || ""))}${j.passages ? ` · ${j.passages} sentences from ${j.docs} documents` : ""} · ${j.elapsed_s}s<span class="blink">_</span></div>`;
  else if (j.state === "error") box.innerHTML = `<div class="job err">The reading did not finish: ${esc(j.error || "")}</div>`;
  else box.innerHTML = `<div class="job ok">Read by ${esc(j.model || "Claude")} in ${j.elapsed_s}s: ${j.links} line${j.links === 1 ? "" : "s"} kept from ${j.passages} sentences${j.dropped ? `, ${j.dropped} dropped because the sentence did not say what the reader claimed` : ""}.
    ${j.api_usd != null ? `On your plan; the same reading would have cost about $${j.api_usd.toFixed(2)} through the API.` : j.cost_usd != null ? `Cost $${j.cost_usd.toFixed(2)}.` : ""}
    <button class="seg" id="forgetBtn" style="margin-left:.5rem" title="Drop this reading and go back to what shipped with the terminal">Undo this reading</button></div>`;
  if ($("#forgetBtn")) $("#forgetBtn").onclick = async () => { await fetch(`/api/pf/supply/${enc(SYM)}/read`, { method: "DELETE" }); loadCo(); };
}
async function startRead() {
  const r = await fetch(`/api/pf/supply/${enc(SYM)}/read`, { method: "POST" });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) { flash(esc(j.detail || "The reading could not start.")); return; }
  ST.D.job = j.job; renderJob(j.job); setTimeout(pollRead, 1500);
}
async function pollRead() {
  let j = null;
  try { j = (await (await fetch(`/api/pf/supply/${enc(SYM)}/read`)).json()).job; } catch { return setTimeout(pollRead, 4000); }
  if (!j) return;
  if (j.state === "running") { ST.D.job = j; renderJob(j); return setTimeout(pollRead, 2000); }
  await loadCo();
}
async function showPassages() {
  const box = $("#psgBox");
  box.innerHTML = `<div class="center ld">Cutting the sentences out of the filings<span class="blink">_</span></div>`;
  let P;
  try { P = await (await fetch(`/api/pf/supply/${enc(SYM)}/passages`)).json(); } catch (e) { P = { ok: false, error: String(e.message || e) }; }
  if (!P.ok) { box.innerHTML = `<div class="lc-empty">${esc(P.error || "Could not cut the passages.")}</div>`; return; }
  box.innerHTML = `<div class="src">${P.items.length} sentences from ${P.docs.map(d => `${esc(d.form)} of ${when(d.filed)}`).join(", ")}.</div>
    <div class="psg">${P.items.map(p => `<p><b>${p.n}</b>${esc(p.text)}</p>`).join("") || "No sentence in these filings names another organisation next to a word about buying, selling or competing."}</div>`;
}

SYM ? loadCo() : loadBook();
