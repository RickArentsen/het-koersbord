/* ==========================================================================
   FCF — every holding, every share your turbos track and every stock you add,
   on Qualtrim's free-cash-flow model, each from its own figures. A row opens
   the full model (static/fcfkit.js), the same card as on its stock page; what
   you change there is saved per stock and the row follows.

   One fetch, /api/pf/fcf (pf/fcfmodel.py). The first build reads statements,
   estimates and prices for each stock one after another; after that the
   inputs are kept an hour, the research behind them longer.
   ========================================================================== */

let T = null, OPEN = null, SORT = "edge", DIR = -1, POLL = null;

$("#shell").innerHTML = termBar({ here: "FCF · FREE CASH FLOW MODEL", right:
  `<a class="tbtn" href="/m/MVO" title="Markowitz & forecast">MVO</a>
   <button class="tbtn" id="refBtn" title="Read every stock's inputs again">Rebuild</button>` });
wireShell();
$("#refBtn").onclick = () => load(true);

const fmt2 = v => v == null || !isFinite(v) ? "–" : nf(v, 2);
const sp = (v, d = 1) => v == null || !isFinite(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d) + "%";
const edge = r => r.implied != null && r.ret != null ? r.implied - r.ret : null;

async function load(refresh = false) {
  try {
    const r = await fetch("/api/pf/fcf" + (refresh ? "?refresh=1" : ""));
    T = await r.json();
    if (!r.ok) throw new Error(T.detail || `HTTP ${r.status}`);
  } catch (e) {
    $("#app").innerHTML = `<div class="note"><b>Could not load the FCF table</b>${esc(e.message)}</div>`;
    return;
  }
  render();
  clearTimeout(POLL);
  if (T.state === "running") POLL = setTimeout(() => load(), 3000);
}

const COLS = [
  ["sym", "Stock", r => r.sym], ["price", "Price", r => r.price], ["start", "FCF/share", r => r.start],
  ["fcf_yield", "FCF yield", r => r.fcf_yield], ["growth", "Growth", r => r.growth], ["multiple", "Multiple", r => r.multiple],
  ["ret", "Wanted", r => r.ret], ["implied", "Offers", r => r.implied], ["edge", "vs wanted", edge],
  ["entry", "Entry price", r => r.entry], ["vs_price", "vs price", r => r.vs_price], ["range", "Bear · bull", r => r.bull],
];

function render() {
  const rows = (T.rows || []).slice();
  const key = (COLS.find(c => c[0] === SORT) || COLS[8])[2];
  rows.sort((a, b) => {
    const x = a.available && a.ok ? key(a) : null, y = b.available && b.ok ? key(b) : null;
    if (x == null && y == null) return 0;
    if (x == null) return 1;
    if (y == null) return -1;
    return typeof x === "string" ? DIR * x.localeCompare(y) : DIR * (x - y);
  });
  const ok = rows.filter(r => r.available && r.ok);
  const cheap = ok.filter(r => edge(r) >= 0).length;
  const building = T.state === "running";
  $("#app").innerHTML = `
  <div class="panel"><header><h2>Free cash flow model</h2><span class="hint">Qualtrim's model, every input from the company's own figures</span></header>
    <div class="body">
      <div class="intro">Each share's free cash flow, grown for ${ok.length ? (ok[0].horizon || 5) : 5} years at the company's own rate and valued at the multiple it has traded on:
        <b>offers</b> is the yearly return from today's price if that holds, <b>wanted</b> the return its risk calls for (the ten-year bond in its currency
        plus its beta × ${nf(T.erp || 5, 0)}%), and the <b>entry price</b> is what to pay for that return. Open a row to see why and to change any input; a stock
        you change keeps your numbers. ${ok.length ? `<b>${cheap} of ${ok.length}</b> offer at least the return their risk calls for.` : ""}</div>
    </div></div>
  <div class="panel"><div class="body">
    <div class="fbar"><span class="lbl">Add a stock</span><input class="add" id="addIn" placeholder="ticker, e.g. NVDA" spellcheck="false" autocomplete="off">
      <button class="seg" id="addBtn">Add</button><span class="dim" id="addMsg"></span>
      <span style="flex:1"></span><span class="dim">${building ? `reading ${T.i} of ${T.n}…` : T.finished ? "built " + new Date(T.finished * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" }) : ""}</span></div>
    ${building && !rows.length ? `<div class="prog">The first build reads each stock's statements, estimates and prices, one after another: a few minutes.</div>` : ""}
    <div class="tscroll"><table class="tbl"><thead><tr>${COLS.map(([k, lab]) => `<th data-s="${k}" class="${k === SORT ? "on" : ""}">${lab}</th>`).join("")}<th></th></tr></thead>
    <tbody>${rows.map(rowHtml).join("")}</tbody></table></div>
    <div class="src">Free cash flow: operating cash flow less capital spending over the last four quarters, per quoted share, in the share's currency. Growth: a starting rate,
      the average of analysts' revenue growth for this year and next and its own five-year growth in operating cash flow per share, fading to 5% by the last year; the figure is
      the steady rate that ends at the same place. Multiple: its own median price/FCF over five years, between 8× and 35×. Wanted: the ten-year bond plus beta × 5%, between
      7% and 20%. Bear and bull: growth a few points either side, the band's 25th and 75th percentile multiples. Banks and insurers have no free cash flow to model.</div>
  </div></div>`;
  $$("th[data-s]").forEach(th => th.onclick = () => { if (SORT === th.dataset.s) DIR = -DIR; else { SORT = th.dataset.s; DIR = th.dataset.s === "sym" ? 1 : -1; } render(); });
  $$("tr.fr").forEach(tr => tr.onclick = e => { if (e.target.closest("button,a")) return; OPEN = OPEN === tr.dataset.s ? null : tr.dataset.s; render(); });
  $$("button[data-rm]").forEach(b => b.onclick = async () => {
    await fetch(`/api/pf/fcf/${encodeURIComponent(b.dataset.rm)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ listed: false }) });
    load(true);
  });
  $("#addBtn").onclick = add;
  $("#addIn").onkeydown = e => { if (e.key === "Enter") add(); };
  if (OPEN) openRow(OPEN);
}

function rowHtml(r) {
  const tags = (r.why === "via a turbo" ? `<span class="tag">turbo</span>` : r.why === "your list" ? `<span class="tag">added</span>` : "")
    + (r.edited ? `<span class="tag ed" title="you changed its inputs">yours</span>` : "");
  const head = `<td><a href="/t/${encodeURIComponent(r.sym)}#s-fcf" class="tkr"><b>${esc(r.sym)}</b></a><span class="nm">${esc(r.name || "")}</span>${tags}</td>`;
  const rm = r.why === "your list" ? `<button class="x" data-rm="${esc(r.sym)}" title="take off the list">×</button>` : "";
  if (!r.available || !r.ok) return `<tr class="fr na${OPEN === r.sym ? " open" : ""}" data-s="${esc(r.sym)}">${head}<td colspan="${COLS.length - 1}" style="text-align:left">${esc(r.why_not || "no model")}</td><td>${rm}</td></tr>`
    + (OPEN === r.sym ? `<tr class="det"><td colspan="${COLS.length + 1}"><div class="det-in" id="det"></div></td></tr>` : "");
  const e = edge(r), c = e == null ? "" : e >= 0 ? "pos" : e >= -3 ? "warnc" : "neg";
  const kind = r.base_kind && r.base_kind !== "reported" ? ` <span class="dim" title="${r.base_kind === "normalised" ? "operating cash flow less depreciation" : "after stock-based pay"}">${r.base_kind === "normalised" ? "n" : "s"}</span>` : "";
  return `<tr class="fr${OPEN === r.sym ? " open" : ""}" data-s="${esc(r.sym)}">${head}
    <td>${FCFK.money(r.price, r.currency)}</td><td>${fmt2(r.start)}${kind}</td><td>${FCFK.pct(r.fcf_yield, 1)}</td>
    <td>${FCFK.pct(r.growth, 1)}</td><td>${nf(r.multiple, 1)}×</td><td>${FCFK.pct(r.ret, 1)}</td>
    <td class="${c}"><b>${FCFK.pct(r.implied, 1)}</b></td><td class="${c}">${e == null ? "–" : (e >= 0 ? "+" : "−") + nf(Math.abs(e), 1) + "pp"}</td>
    <td>${FCFK.money(r.entry, r.currency)}</td><td class="${sgn(r.vs_price)}">${sp(r.vs_price, 0)}</td>
    <td class="dim">${FCFK.pct(r.bear, 0)} · ${FCFK.pct(r.bull, 0)}</td><td>${rm}</td></tr>`
    + (OPEN === r.sym ? `<tr class="det"><td colspan="${COLS.length + 1}"><div class="det-in" id="det">loading<span class="blink">_</span></div></td></tr>` : "");
}

async function openRow(sym) {
  const host = $("#det");
  if (!host) return;
  try {
    const r = await fetch(`/api/pf/fcf/${encodeURIComponent(sym)}`);
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    host.innerHTML = `<div class="det-hd"><b>${esc(sym)}</b><span class="dim">${esc(j.name || "")}</span><span class="sp"></span>
      <a class="code" href="/t/${encodeURIComponent(sym)}#s-fcf">its page ›</a><button class="x" id="detX" title="close">×</button></div><div id="detCard"></div>`;
    $("#detX").onclick = () => { OPEN = null; render(); };
    FCFK.card($("#detCard"), j, { onSaved: m => {
      const row = (T.rows || []).find(x => x.sym === sym);
      if (row && m.cases) {
        const b = m.cases.base;
        Object.assign(row, { growth: m.growth_used, multiple: m.multiple_used, ret: m.ret_used, start: m.start, base_kind: m.base_kind,
          implied: b.implied, entry: b.entry, vs_price: b.vs_price, ok: b.ok, bear: m.cases.bear.implied, bull: m.cases.bull.implied,
          edited: m.edited, horizon: m.horizon });
        const tr = document.querySelector(`tr.fr[data-s="${CSS.escape(sym)}"]`);
        if (tr) tr.outerHTML = rowHtml(row).split(`<tr class="det">`)[0];
        $$("tr.fr").forEach(t => t.onclick = e => { if (e.target.closest("button,a")) return; OPEN = OPEN === t.dataset.s ? null : t.dataset.s; render(); });
      }
    } });
  } catch (e) {
    host.innerHTML = `<div class="note"><b>No model</b>${esc(e.message)}</div>`;
  }
}

async function add() {
  const v = ($("#addIn").value || "").trim().toUpperCase();
  if (!v) return;
  $("#addMsg").textContent = "checking " + v + "…";
  try {
    const r = await fetch(`/api/pf/fcf/${encodeURIComponent(v)}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ listed: true }) });
    const j = await r.json();
    if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
    OPEN = j.symbol || v;
    $("#addMsg").textContent = "";
    load(true);
  } catch (e) {
    $("#addMsg").textContent = "not added: " + e.message;
  }
}

load();
