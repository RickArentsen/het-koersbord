/* ==========================================================================
   MVO — Markowitz: what today's holdings and a few funds could be expected
   to earn for the swings they take, where the book sits against that, and
   where a monthly deposit should go to move it there.

   The server (pf/mvo.py) estimates the risk, solves the frontier, draws the
   history again for the cloud and plans the orders; this page asks again
   when a setting changes. A model, not advice: the returns are the ones
   market prices imply unless you add your own.
   ========================================================================== */

let V = null;
let SEQ = 0, TMR = 0, MIX = "target";
let S = { mode: "market", target: "same", cap: 10, monthly: 150, max_cost: 1, erp: null };
try { const s = JSON.parse(localStorage.getItem("mvo_settings") || "null"); if (s) S = { ...S, ...s }; } catch {}

/* ---------- formatting ---------------------------------------------------- */
const fx = (v, d = 0) => v == null || isNaN(v) ? "–" :
  Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
const p1 = (v, d = 1) => v == null ? "–" : fx(v, d) + "%";
const sg = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + fx(Math.abs(v), d);
const e0 = v => v == null ? "–" : "€" + fx(v, 0);
const e2 = v => v == null ? "–" : "€" + fx(v, 2);
const api = async (u, o) => { const r = await fetch(u, o); if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "HTTP " + r.status); return r.json(); };
const MODE = { market: "the market's returns", views: "your views", history: "past returns" };
const PT = { target: "Where the plan is heading", tangency: "The one fund", same_ret: "Your return, least risk",
             least: "Least risk" };
const FUNDC = { "World shares": "var(--s1)", "European shares": "var(--s4)", "Emerging markets": "var(--s2)",
                "Euro government bonds": "var(--s3)", "Gold": "var(--warn)" };

$("#shell").innerHTML = termBar({ here: "MVO · MARKOWITZ", right:
  `<a class="tbtn" href="/portfolio#riskmap" title="Risk by holding">RISK</a>` });
wireShell();

function scaffold() {
  $("#app").innerHTML = `
  <nav class="mtabs"><a class="on" href="/m/MVO">Markowitz</a><a href="/m/MVO/forecast">Forecast</a></nav>
  <div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>MVO</h1><span class="sub">Markowitz: what your holdings and a few funds could earn for the swings they take — a model, not advice</span></div>
    <div class="tiles" id="tiles"></div>
    <div class="disc" id="disc"></div>
  </div></div>
  <div id="ctl"></div>
  <div class="panel" id="s-front"><header><h2>The efficient frontier</h2><span class="hint" id="frHint"></span></header>
    <div class="body"><div class="fr" id="fr"></div><div class="fkey" id="fkey"></div><div class="read" id="read"></div></div></div>
  <div class="panel" id="s-one"><header><h2>The one fund</h2><span class="hint">Tobin's separation: one risky mix for everyone, cash decides the risk</span></header>
    <div class="body" id="one"></div></div>
  <div class="panel" id="s-mix"><header><h2>The mix</h2><span class="hint">your weight, the model's, and the range the model gives it when the history is drawn again</span></header>
    <div class="body" id="mix"></div></div>
  <div class="panel" id="s-plan"><header><h2>The deposit plan</h2><span class="hint">buy-only, whole shares, at what your orders actually cost</span></header>
    <div class="body" id="plan"></div></div>
  <div class="panel" id="s-imp"><header><h2>What your weights say you expect</h2><span class="hint">the returns at which your book as it stands would be the best mix</span></header>
    <div class="body" id="imp"></div></div>
  <div class="panel" id="s-views"><header><h2>Your views</h2><span class="hint">what you expect of a holding, and how sure you are</span></header>
    <div class="body" id="views"></div></div>
  <div class="panel" id="s-all"><header><h2>Every holding in the model</h2><span class="hint">risk, beta and the three kinds of expected return</span></header>
    <div class="body" id="all"></div></div>
  <div class="panel" id="s-how"><header><h2>How it works</h2><span class="hint">and what it leaves out</span></header>
    <div class="body" id="how"></div></div>`;
}

/* ==========================================================================
   loading
   ========================================================================== */
function qs() {
  const p = new URLSearchParams({ mode: S.mode, target: S.target, cap: S.cap, monthly: S.monthly, max_cost: S.max_cost });
  if (S.erp != null && S.erp !== "") p.set("erp", S.erp);
  return p.toString();
}

async function load() {
  scaffold();
  $("#tiles").innerHTML = `<div class="tile"><div class="k">Markowitz</div><div class="v">…</div><div class="s">reading three years of prices; the first time takes about a minute</div></div>`;
  await rerun(true);
}

function schedule() {
  try { localStorage.setItem("mvo_settings", JSON.stringify(S)); } catch {}
  clearTimeout(TMR);
  $("#app").classList.add("busy");
  TMR = setTimeout(() => rerun(false), 280);
}

async function rerun(first) {
  const seq = ++SEQ;
  try {
    const r = await api("/api/pf/mvo?" + qs());
    if (seq !== SEQ) return;
    if (!r.available) {
      $("#app").innerHTML = `<div class="note"><b>Markowitz has nothing to work on</b>${esc(r.why || "")}</div>`;
      return;
    }
    V = r;
    if (V.mode !== S.mode) S.mode = V.mode;
    render();
  } catch (e) {
    if (first) $("#app").innerHTML = `<div class="note"><b>Could not load Markowitz</b>${esc(e.message)}</div>`;
    else flash(`Markowitz could not run: ${esc(e.message)}`);
  } finally {
    if (seq === SEQ) $("#app").classList.remove("busy");
  }
}

function render() {
  tiles(); controls(); drawFront(); oneFund(); mixPanel(); planPanel(); impPanel(); viewsPanel(); allPanel(); how();
  wrapTables();
}

/* ==========================================================================
   tiles and controls
   ========================================================================== */
function tiles() {
  const b = V.book, P = V.points, sr = P.same_risk, sret = P.same_ret, tan = P.tangency;
  const top = tan.mix.slice(0, 2).map(m => `${esc(m.label)} ${fx(m.w, 0)}%`).join(", ");
  const first = V.plan.months.find(m => m.orders.length);
  $("#tiles").innerHTML = [
    tile("Your book", p1(b.ret), null, `expected a year · risk ${p1(b.vol)} · ${MODE[V.mode]}`, true),
    tile("Same risk, best mix", p1(sr.ret), `<span class="${sgn(sr.ret - b.ret)}">${sg(sr.ret - b.ret)} pts</span>`,
      `what ${p1(b.vol)} of risk could be expected to earn`),
    tile("Your return, least risk", p1(sret.vol), `<span class="${sgn(b.vol - sret.vol)}">${sg(sret.vol - b.vol)} pts</span>`,
      `of risk for the same ${p1(b.ret)} a year`),
    tile("Best per unit of risk", p1(tan.ret), null, `${top} · Sharpe ${fx(tan.sharpe, 2)} against your ${fx(b.sharpe, 2)}`),
    tile("Next order", first ? `month ${first.m}` : "none",
      null, first ? first.orders.map(o => o.cash ? `keep ${e0(o.value)} as cash` : `${o.n} × ${esc(o.label)} ${e0(o.value)}`).join(", ") : "nothing passes the cost limit in two years"),
    tile("Inputs", p1(V.rf, 2), null, `cash, €STR · premium ${p1(V.erp)} · ${V.weeks} weeks to ${dmy(V.asof).slice(0, 5)}`),
  ].join("");
  const short = (V.short || []).map(s => `${esc(s.label)} (${e0(s.value)}, ${esc(s.why)})`).join("; ");
  $("#disc").innerHTML = `Not advice, and not a forecast. ${V.mode === "market"
    ? "Every holding is expected to earn cash plus its beta to world shares times the equity premium, so the market pays only for market risk; add a view to change that."
    : V.mode === "views" ? "The market's returns, moved toward your views by as much as the confidence you gave them."
    : "<b style='color:var(--warn)'>Past returns:</b> what each holding did over the window, which is what makes Markowitz fail."}
    ${short ? ` Left out, needing two years of prices: ${short}; it stays in the book as it is.` : ""}
    <a href="#s-how">How it works</a>.`;
}

function seg(key, opts) {
  return `<span class="grp2">${opts.map(([v, l, t]) =>
    `<button class="seg ${String(S[key]) === String(v) ? "on" : ""}" data-k="${key}" data-v="${v}" title="${esc(t || "")}">${l}</button>`).join("")}</span>`;
}

function controls() {
  const nv = (V.views || []).length;
  const custom = !["same", "one_fund", "tangency", "least"].includes(String(S.target));
  $("#ctl").innerHTML = `
    <span><span class="lbl">Returns</span>${seg("mode", [
      ["market", "Market's", "what market prices imply: cash plus beta times the premium"],
      ["views", `Your views${nv ? ` (${nv})` : ""}`, nv ? "the market's, moved toward your views" : "add a view below first"],
      ["history", "Past ⚠", "the past returns: see how Markowitz goes wrong"]])}</span>
    <span><span class="lbl">Plan toward</span>${seg("target", [
      ["same", "Same risk", "the best mix with the risk your book takes now"],
      ["one_fund", "One fund", "the one fund at your book's risk, the rest kept as cash"],
      ["tangency", "Best per risk", "the most expected return over cash per unit of risk"],
      ["least", "Least risk", "the mix that swings least"]])}
      <input type="number" id="cTgt" min="2" max="60" step="0.5" placeholder="risk %" value="${custom ? S.target : ""}" title="or a level of risk, % a year"></span>
    <span><span class="lbl">Cap</span><input type="number" id="cCap" min="2" max="100" step="1" value="${S.cap}">% a share</span>
    <span><span class="lbl">Deposit</span>€ <input type="number" id="cMon" data-eur min="0" step="25" value="${S.monthly}"> a month</span>
    <span><span class="lbl">Cost limit</span><input type="number" id="cCost" min="0.1" max="5" step="0.1" value="${S.max_cost}">% an order</span>
    <span><span class="lbl">Premium</span><input type="number" id="cErp" min="0" max="15" step="0.25" value="${S.erp ?? V.erp_hist}">%
      <span class="why">${S.erp != null && S.erp !== "" && +S.erp !== V.erp_hist ? `<button class="lnk" id="cErpR">history's ${p1(V.erp_hist)}</button>` : "1871–2020"}</span></span>`;
  $$("#ctl .seg").forEach(b => b.onclick = () => {
    if (b.dataset.k === "mode" && b.dataset.v === "views" && !(V.views || []).length) {
      flash("Add a view first, in Your views below."); return;
    }
    S[b.dataset.k] = b.dataset.v;
    $$(`#ctl .seg[data-k="${b.dataset.k}"]`).forEach(x => x.classList.toggle("on", x === b));
    if (b.dataset.k === "target") $("#cTgt").value = "";
    schedule();
  });
  const num = (id, key, lo, hi) => $(id).onchange = () => {
    const v = parseFloat($(id).value);
    if (isNaN(v)) return;
    S[key] = Math.min(hi, Math.max(lo, v));
    schedule();
  };
  num("#cCap", "cap", 2, 100); num("#cMon", "monthly", 0, 100000); num("#cCost", "max_cost", 0.1, 5);
  num("#cErp", "erp", 0, 15);
  $("#cTgt").onchange = () => {
    const v = parseFloat($("#cTgt").value);
    S.target = isNaN(v) ? "same" : String(Math.min(60, Math.max(2, v)));
    schedule();
  };
  const r = $("#cErpR"); if (r) r.onclick = () => { S.erp = null; schedule(); };
}

/* ==========================================================================
   the frontier
   ========================================================================== */
function drawFront() {
  const host = $("#fr");
  host.__draw = drawFront;
  CHARTS.add(host);
  const W = Math.max(300, Math.round(host.clientWidth || 700)); host.__w = W;
  const H = W < 560 ? 300 : 400;
  const F = V.frontier, P = V.points, b = V.book;
  const assets = V.assets;
  // Wide enough for the frontier and twice the book's risk; a share that
  // swings more is drawn at the edge, fainter.
  let xmax = Math.min(75, Math.max(F[F.length - 1].vol, b.vol * 1.8) * 1.05);
  const inX = a => a.vol <= xmax;
  const ys = [...F.map(p => p.ret), b.ret, V.rf, ...assets.filter(inX).map(a => a.mu), ...V.cloud.map(c => c[1])];
  const yt = nice(Math.min(0, ...ys), Math.max(...ys) * 1.05, H < 340 ? 5 : 6);
  const xt = nice(0, xmax, W < 560 ? 5 : 8);
  xmax = xt.hi;
  const M = { l: 42, r: 16, t: 10, b: 32 }, pw = W - M.l - M.r, ph = H - M.t - M.b;
  const X = v => M.l + v / xmax * pw, Y = r => M.t + (yt.hi - r) / (yt.hi - yt.lo) * ph;
  let g = "";
  for (const t of yt.ticks) g += `<line class="gl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(t).toFixed(1)}" y2="${Y(t).toFixed(1)}"/><text class="ax" x="${M.l - 6}" y="${(Y(t) + 3).toFixed(1)}" text-anchor="end">${fx(t, 0)}%</text>`;
  for (const t of xt.ticks) g += `<text class="ax" x="${X(t).toFixed(1)}" y="${H - 14}" text-anchor="middle">${fx(t, 0)}%</text>`;
  if (yt.lo < 0) g += `<line class="zl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(0)}" y2="${Y(0)}"/>`;
  g += `<text class="ax" x="${M.l + pw}" y="${H - 1}" text-anchor="end">risk: how much it swings, % a year →</text>`;
  g += `<text class="ax" x="${M.l + 4}" y="${M.t + 10}">expected return, % a year</text>`;

  // the cloud: the history drawn again, each answer judged by today's estimates
  for (const [v, r] of V.cloud) if (v <= xmax) g += `<circle cx="${X(v).toFixed(1)}" cy="${Y(r).toFixed(1)}" r="1.7" fill="var(--accent)" opacity=".28"/>`;
  // each holding and fund
  const pts = [], labs = [];
  for (const a of assets) {
    const v = Math.min(a.vol, xmax), clipped = a.vol > xmax;
    const x = X(v), y = Y(a.mu);
    if (a.fund) {
      const c = FUNDC[a.group] || "var(--ink-mid)";
      g += `<rect x="${(x - 3.5).toFixed(1)}" y="${(y - 3.5).toFixed(1)}" width="7" height="7" fill="${a.held ? c : "none"}" stroke="${c}" stroke-width="1.4"/>`;
      labs.push({ x, y, text: a.label, color: c, pri: 5 });
    } else {
      g += `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="${(1.8 + Math.sqrt(a.w_book) * 1.1).toFixed(1)}" fill="var(--ink-soft)" opacity="${clipped ? .35 : .65}"/>`;
    }
    pts.push({ x, y, kind: "asset", a });
  }
  // the one-fund line: cash, then the one fund, and every mix of the two
  // between (Tobin). Past the one fund it would need borrowing: drawn faint.
  const T = P.tangency, slope = (T.ret - V.rf) / T.vol;
  g += `<line x1="${X(0)}" y1="${Y(V.rf).toFixed(1)}" x2="${X(T.vol).toFixed(1)}" y2="${Y(T.ret).toFixed(1)}" stroke="var(--s3)" stroke-width="1.6"/>`;
  const xEnd = Math.min(xmax, T.vol * 2.2);
  g += `<line x1="${X(T.vol).toFixed(1)}" y1="${Y(T.ret).toFixed(1)}" x2="${X(xEnd).toFixed(1)}" y2="${Y(V.rf + slope * xEnd).toFixed(1)}" stroke="var(--s3)" stroke-width="1" stroke-dasharray="3 4" opacity=".55"/>`;
  g += `<circle cx="${X(0)}" cy="${Y(V.rf).toFixed(1)}" r="3.5" fill="var(--s3)"/>`;
  labs.push({ x: X(0), y: Y(V.rf), text: "cash", color: "var(--s3)", pri: 4 });
  for (let f = 0.1; f <= 2.2001; f += 0.05) {
    const v = T.vol * f;
    if (v > xmax) break;
    pts.push({ x: X(v), y: Y(V.rf + slope * v), kind: "cml", f, v, r: V.rf + slope * v });
  }
  // the frontier
  g += `<path d="${F.filter(p => p.vol <= xmax).map((p, i) => (i ? "L" : "M") + X(p.vol).toFixed(1) + " " + Y(p.ret).toFixed(1)).join("")}" fill="none" stroke="var(--accent)" stroke-width="2.2" stroke-linejoin="round"/>`;
  // from the book to the frontier, both ways
  g += `<line x1="${X(b.vol).toFixed(1)}" y1="${Y(b.ret).toFixed(1)}" x2="${X(P.same_risk.vol).toFixed(1)}" y2="${Y(P.same_risk.ret).toFixed(1)}" stroke="var(--gain)" stroke-dasharray="2 3" stroke-width="1.2"/>`;
  g += `<line x1="${X(b.vol).toFixed(1)}" y1="${Y(b.ret).toFixed(1)}" x2="${X(P.same_ret.vol).toFixed(1)}" y2="${Y(P.same_ret.ret).toFixed(1)}" stroke="var(--gain)" stroke-dasharray="2 3" stroke-width="1.2"/>`;
  const mark = (p, kind, label, dy = -9) => {
    const x = X(Math.min(p.vol, xmax)), y = Y(p.ret);
    if (kind === "tan") g += `<path d="M${x} ${y - 6}L${x + 6} ${y}L${x} ${y + 6}L${x - 6} ${y}Z" fill="var(--s3)" stroke="var(--ground)"/>`;
    else if (kind === "least") g += `<rect x="${x - 4.5}" y="${y - 4.5}" width="9" height="9" fill="none" stroke="var(--s3)" stroke-width="1.6"/>`;
    else g += `<circle cx="${x}" cy="${y}" r="5.5" fill="none" stroke="var(--gain)" stroke-width="1.6"/>`;
    if (label) labs.push({ x, y, text: label, color: "var(--ink-mid)", pri: kind === "tan" ? 2 : 3 });
    pts.push({ x, y, kind, p, label });
  };
  mark(P.least, "least", "least risk");
  mark(P.tangency, "tan", "the one fund");
  mark(P.same_ret, "ret", "");
  mark(P.same_risk, "risk", "same risk");
  const tg = P.target;
  if (!["same"].includes(String(V.target))) {
    const x = X(Math.min(tg.vol, xmax)), y = Y(tg.ret);
    g += `<circle cx="${x}" cy="${y}" r="8" fill="none" stroke="var(--accent)" stroke-width="1.4" stroke-dasharray="2 2"/>`;
    labs.push({ x, y, text: "plan's target", color: "var(--accent)", pri: 1 });
  }
  // the book, on top of everything
  const bx = X(Math.min(b.vol, xmax)), by = Y(b.ret);
  g += `<circle cx="${bx}" cy="${by}" r="6.5" fill="var(--ink)" stroke="var(--ground)" stroke-width="2"/>`;
  labs.push({ x: bx, y: by, text: "your book", color: "var(--ink)", pri: 0, bold: true });
  pts.push({ x: bx, y: by, kind: "book", p: b });
  g += placeLabels(labs, pts.filter(p => p.kind !== "asset"), M, pw, ph);

  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="the efficient frontier">${g}
    <rect class="hov" x="${M.l}" y="${M.t}" width="${pw}" height="${ph}" fill="transparent"/></svg><div class="tip" style="display:none"></div>`;
  const svg = host.querySelector("svg"), tip = host.querySelector(".tip");
  svg.addEventListener("mousemove", ev => {
    const r = svg.getBoundingClientRect(), k = W / r.width;
    const mx = (ev.clientX - r.left) * k, my = (ev.clientY - r.top) * k;
    let best = null, bd = 196;
    for (const p of pts) { const d = (p.x - mx) ** 2 + (p.y - my) ** 2; if (d < bd) { bd = d; best = p; } }
    if (!best) { tip.style.display = "none"; return; }
    tip.innerHTML = tipOf(best);
    tip.style.display = "";
    const left = best.x / k, top = best.y / k;
    tip.style.left = Math.max(0, Math.min(r.width - tip.offsetWidth, left + (left > r.width / 2 ? -tip.offsetWidth - 12 : 12))) + "px";
    tip.style.top = Math.max(0, top - 20) + "px";
  });
  svg.addEventListener("mouseleave", () => tip.style.display = "none");

  $("#frHint").textContent = `${V.n} holdings and funds · ${V.weeks} weeks of five-day returns in euros · at most ${fx(V.cap, 0)}% in one share`;
  $("#fkey").innerHTML = `
    <span><svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--accent)" stroke-width="2.2"/></svg>the frontier</span>
    <span><svg width="10" height="10"><circle cx="5" cy="5" r="4" fill="var(--ink)"/></svg>your book</span>
    <span><svg width="10" height="10"><circle cx="5" cy="5" r="3.5" fill="none" stroke="var(--gain)" stroke-width="1.5"/></svg>the frontier at your risk, and at your return</span>
    <span><svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke="var(--s3)" stroke-width="1.6"/></svg>the one-fund line: cash and the one fund</span>
    <span><svg width="12" height="12"><path d="M6 1L11 6L6 11L1 6Z" fill="var(--s3)"/></svg>the one fund, best per unit of risk</span>
    <span><svg width="12" height="8"><circle cx="3" cy="4" r="1.6" fill="var(--accent)" opacity=".5"/><circle cx="9" cy="4" r="1.6" fill="var(--accent)" opacity=".5"/></svg>the history drawn again, ${V.cloud.length} times</span>
    <span><svg width="10" height="10"><circle cx="5" cy="5" r="3" fill="var(--ink-soft)"/></svg>one holding, sized by weight</span>
    <span><svg width="10" height="10"><rect x="1.5" y="1.5" width="7" height="7" fill="none" stroke="var(--s1)" stroke-width="1.4"/></svg>a fund (filled if you hold it)</span>`;
  $("#read").innerHTML = (V.say || []).map(s => `<p>${esc(s)}</p>`).join("") + cloudWords();
}

/* Labels go where they fit: right of the point, then left, above, below,
   never over another label or a marker, most important first. */
function placeLabels(labs, marks, M, pw, ph) {
  const boxes = marks.map(m => [m.x - 7, m.y - 7, m.x + 7, m.y + 7]);
  let out = "";
  for (const l of [...labs].sort((a, b) => a.pri - b.pri)) {
    const w = l.text.length * 6.2 + 4, h = 11;
    const tries = [[9, 4], [9, -6], [9, 14], [-9 - w, 4], [-9 - w, -6], [-9 - w, 14], [-w / 2, -10], [-w / 2, 20]];
    for (const [dx, dy] of tries) {
      const box = [l.x + dx, l.y + dy - 9, l.x + dx + w, l.y + dy + 2];
      if (box[0] < M.l || box[2] > M.l + pw + 14 || box[1] < M.t || box[3] > M.t + ph + 4) continue;
      if (boxes.some(b => !(box[2] < b[0] || box[0] > b[2] || box[3] < b[1] || box[1] > b[3]))) continue;
      boxes.push(box);
      out += `<text class="lab" x="${(l.x + dx).toFixed(1)}" y="${(l.y + dy).toFixed(1)}" fill="${l.color}"${l.bold ? ` font-weight="600"` : ""}>${esc(l.text)}</text>`;
      break;
    }
  }
  return out;
}

function cloudWords() {
  const c = V.cloud;
  if (!c.length) return "";
  const vs = c.map(x => x[0]).sort((a, b) => a - b), rs = c.map(x => x[1]).sort((a, b) => a - b);
  const q = (a, f) => a[Math.min(a.length - 1, Math.floor(a.length * f))];
  return `<p>Drawn again from the same ${V.weeks} weeks in half-year blocks, the best mix at this risk appetite lands anywhere
    from ${p1(q(vs, .1))} to ${p1(q(vs, .9))} of risk and ${p1(q(rs, .1))} to ${p1(q(rs, .9))} of return (judged by today's
    estimates): that spread is how much the answer is worth. The mix panel shows the same for each weight.</p>`;
}

function tipOf(p) {
  const row = (k, v) => `<div class="r"><span>${k}</span><b>${v}</b></div>`;
  if (p.kind === "asset") {
    const a = p.a;
    return `<div class="h">${esc(a.label)}${a.fund ? "" : ` <span class="dim">${esc(a.t)}</span>`}</div>
      ${row("expected", p1(a.mu))}${row("risk", p1(a.vol))}${row("beta to world", fx(a.beta, 2))}
      ${row("your weight", p1(a.w_book))}${row("model's", p1(a.w_tgt))}${a.fund ? row("", esc(a.name)) : ""}`;
  }
  if (p.kind === "cml") return `<div class="h">${p.f <= 1 ? `${fx(p.f * 100, 0)}% one fund, ${fx(100 - p.f * 100, 0)}% cash` : `${fx(p.f * 100, 0)}% one fund, borrowing ${fx(p.f * 100 - 100, 0)}%`}</div>
      ${row("expected", p1(p.r))}${row("risk", p1(p.v))}${p.f > 1 ? row("", "before the cost of borrowing") : ""}`;
  if (p.kind === "book") return `<div class="h">Your book</div>${row("expected", p1(p.p.ret))}${row("risk", p1(p.p.vol))}${row("Sharpe", fx(p.p.sharpe, 2))}`;
  const name = { tan: "The one fund: best per unit of risk", least: "Least risk", ret: "Your return, least risk", risk: "Your risk, best return" }[p.kind];
  const mix = (p.p.mix || []).slice(0, 4).map(m => row(esc(m.label), p1(m.w, 0))).join("");
  return `<div class="h">${name}</div>${row("expected", p1(p.p.ret))}${row("risk", p1(p.p.vol))}${row("Sharpe", fx(p.p.sharpe, 2))}${mix}`;
}

/* ==========================================================================
   the one fund
   ========================================================================== */
function oneFund() {
  const o = V.one_fund, b = V.book;
  if (!o) { $("#one").innerHTML = ""; return; }
  const market = V.mode === "market" && o.mix.length && o.mix[0].t === V.market.t && o.mix[0].w >= 95;
  $("#one").innerHTML = `<div class="two even"><div>
      <div class="txt"><p><b>One risky mix for everyone.</b> James Tobin showed in 1958 that once there is something riskless to hold,
        cash here (€STR, ${p1(o.rf, 2)} a year), every efficient book is the <b>same</b> risky mix, the one with the most expected return over
        cash per unit of risk, held together with cash. A cautious investor holds more cash, a bold one less; nobody needs a different mix.
        That mix is the one fund: the diamond on the frontier, where the straight line from cash touches the curve.</p>
        <p>${market ? `With the market's own returns the one fund is the market itself, here the world fund, 100%: this is where the
          capital asset pricing model comes from. Your views are what would make it anything else.`
          : `With ${V.mode === "views" ? "your views" : "past returns"} it is the mix on the right; the market's returns would make it the world fund alone.`}</p>
        <p>${o.reach ? `At your book's risk (${p1(b.vol)}) the one-fund answer is <b>${p1(o.share_at, 0)} in the one fund and ${p1(o.cash_at, 0)} in cash</b>,
          expected to earn ${p1(o.ret_at)} a year against your book's ${p1(b.ret)}.`
          : `Your book swings more (${p1(b.vol)}) than the one fund itself (${p1(o.vol)}). On the line, that much risk means
          <b>${p1(o.lever * 100, 0)} in the one fund</b>, borrowing the rest, for an expected ${p1(o.ret_levered)} before the cost of borrowing.
          DEGIRO lends against your shares at a rate above €STR, which takes much of that away; without borrowing the line stops at the one
          fund, and the only way to swing more is the curve above it, into fewer, more volatile shares.`}</p></div>
      <table class="tbl"><thead><tr><th class="l">In the one fund</th><th>In cash</th><th>Risk</th><th>Expected a year</th></tr></thead><tbody>
        ${o.steps.map(r => `<tr><td class="l">${r.share}%</td><td>${100 - r.share}%</td><td>${p1(r.vol)}</td><td>${p1(r.ret)}</td></tr>`).join("")}
        ${!o.reach ? `<tr class="dim"><td class="l">${p1(o.lever * 100, 0)}</td><td>borrowing ${p1(o.lever * 100 - 100, 0)}</td><td>${p1(b.vol)}</td><td>${p1(o.ret_levered)} before interest</td></tr>` : ""}
      </tbody></table></div>
    <div><div class="sec">The one fund</div>
      <table class="tbl"><thead><tr><th class="l">Holding</th><th>Weight</th></tr></thead><tbody>
        ${o.mix.map(m => `<tr><td class="l">${esc(m.label)}</td><td>${p1(m.w)}</td></tr>`).join("")}</tbody></table>
      <div class="disc">Expected ${p1(o.ret)} a year with ${p1(o.vol)} of risk; Sharpe ${fx(o.sharpe, 2)} against your book's ${fx(b.sharpe, 2)}.
        Hover the green line on the chart for any mix of it with cash. The plan can aim at it: <i>Plan toward → One fund</i>.</div></div></div>`;
}

/* ==========================================================================
   the mix
   ========================================================================== */
function mixPanel() {
  const A = Object.fromEntries(V.assets.map(a => [a.t, a]));
  const pt = V.points[MIX];
  const tabs = `<span class="grp" id="mixTabs" style="margin-bottom:.5rem;display:inline-flex">${Object.entries(PT).map(([k, l]) =>
    `<button class="seg ${k === MIX ? "on" : ""}" data-k="${k}">${l}</button>`).join("")}</span>`;
  const own = new Map(pt.mix.map(m => [m.t, m.w]));
  // the model's names, then your biggest holdings it leaves out
  const rows = [...pt.mix.map(m => m.t)];
  for (const a of [...V.assets].sort((x, y) => y.w_book - x.w_book)) {
    if (rows.length >= 22) break;
    if (!own.has(a.t) && a.w_book >= 2) rows.push(a.t);
  }
  const mx = Math.max(...rows.map(t => Math.max(A[t].w_book, own.get(t) || 0, MIX === "target" ? A[t].q90 : 0)), 5);
  const W = v => `${Math.min(100, v / mx * 100).toFixed(1)}%`;
  const other = 100 - pt.mix.reduce((s, m) => s + m.w, 0);
  $("#mix").innerHTML = tabs + `
    <div class="mix">
      <span class="h">Holding</span><span class="h"><span style="color:var(--ink-soft)">■</span> yours <span style="color:var(--accent)">■</span> the model's${MIX === "target" ? " · ⊢⊣ the range" : ""}</span>
      <span class="h num">Yours</span><span class="h num">Model</span><span class="h rng">${MIX === "target" ? "Range" : ""}</span>
      ${rows.map(t => {
        const a = A[t], m = own.get(t) || 0;
        return `<span class="n" title="${esc(a.name || "")}">${esc(a.label)}${a.fund ? "<small>fund</small>" : ""}</span>
          <span class="bars"><i class="b" style="width:${W(a.w_book)}"></i><i class="t" style="width:${W(m)}"></i>${MIX === "target" && a.q90 > 0 ?
            `<i class="w" style="left:${W(a.q10)};width:calc(${W(a.q90)} - ${W(a.q10)})" title="${fx(a.q10, 0)}–${fx(a.q90, 0)}% in eight of ten redrawn histories"></i>` : ""}</span>
          <span class="num">${p1(a.w_book)}</span><span class="num" style="color:${m ? "var(--accent)" : "var(--ink-soft)"}">${m ? p1(m) : "–"}</span>
          <span class="rng">${MIX === "target" ? (a.q90 >= 0.5 ? `${fx(a.q10, 0)}–${fx(a.q90, 0)}%` : "0%") : ""}</span>`;
      }).join("")}
    </div>
    <div class="disc">${other > 0.5 ? `The model's other names hold ${p1(other)} between them. ` : ""}
      ${MIX === "target" ? `The range is where the weight lands in eight of ten of the ${V.cloud.length} redrawn histories: a weight whose range runs from 0 to 20% is a guess, not an answer.` : ""}
      ${V.mode === "market" ? "With the market's returns the model reaches higher risk through the shares that move most with the market for the least swing of their own: that is beta, not a judgement on the companies." : ""}</div>`;
  $$("#mixTabs .seg").forEach(b => b.onclick = () => { MIX = b.dataset.k; mixPanel(); });
}

/* ==========================================================================
   the plan
   ========================================================================== */
function planPanel() {
  const pl = V.plan, M = pl.months, A = Object.fromEntries(V.assets.map(a => [a.t, a]));
  const venues = new Set(V.assets.map(a => a.venue));
  const fee = Object.entries(V.fees).filter(([k]) => venues.has(k)).map(([k, f]) =>
    `<span>${esc(f.where)}: ${e2(f.fee)}${f.fx ? ` + ${fx(f.fx * 100, 2)}% currency` : ""}
      <span class="dim">${f.from === "ledger" ? `your last ${f.n} buys` : f.n ? `DEGIRO's usual; you bought there ${f.n}×` : "DEGIRO's usual"}</span></span>`).join("");
  const first = M.find(m => m.orders.length);
  const wait = first ? first.m - 1 : M.length;
  const tgtName = { same: "the best mix with your book's risk", tangency: "the one fund, alone", least: "the least-risk mix",
    one_fund: V.one_fund && V.one_fund.reach ? `the one fund at your book's risk (${p1(V.one_fund.share_at, 0)}), the rest kept as cash` : "the one fund alone (your book swings more than it)" }[V.target]
    || `the best mix with ${p1(+V.target)} of risk`;
  const firstTxt = first ? `${wait ? `Months 1${wait > 1 ? `–${wait}` : ""}: nothing; every order would cost more than ${p1(V.max_cost)} of itself, or a better one is a month away. ` : ""}
      <b>Month ${first.m}:</b> ${first.orders.map(o => o.cash ? `<b>keep ${e0(o.value)} as cash</b>` : `<b>${o.n} × ${esc(o.label)}</b> at ${e2(o.px)} = ${e0(o.value)} (cost ${e2(o.cost)}, ${p1(o.cost_pct, 2)})`).join(", then ")}.`
    : `Nothing in ${M.length} months: no order of ${e0(V.monthly)} or more passes the cost limit. Raise the deposit or the limit.`;
  const gap0 = pl.te0, gapN = M.length ? M[M.length - 1].te : gap0;
  $("#plan").innerHTML = `
    <div class="txt"><p>${e0(V.monthly)} a month toward ${tgtName}, buying whole shares (DEGIRO sells no fractions) and never selling. Each month
      it buys the order that takes the most of the gap off per euro, and waits when every order would cost more than
      ${p1(V.max_cost)} of itself, or when next month's money buys a clearly better one. Prices stay at today's.</p></div>
    <div class="costs">${fee}</div>
    <div class="first">${firstTxt}</div>
    <div class="two even"><div>
      <table class="tbl"><thead><tr><th>Month</th><th class="l">Buy</th><th>Order</th><th>Cost</th><th>Saved for</th><th>Cash left</th><th>Gap after</th></tr></thead><tbody>
      ${orderRows(M)}
      </tbody></table>
      <div class="disc">Only the months with an order; in the others the deposit waits.</div></div>
      <div><div class="lc" id="gapChart"></div>
        <div class="txt"><p>The <b>gap</b> is the tracking error between your book and the target: how far apart the two would
          drift in a typical year, ${p1(gap0, 2)} today. ${M.length} months of deposits take it to ${p1(gapN, 2)}, spending
          ${e0(pl.spent)} of ${e0(pl.deposits)} with ${e2(pl.paid)} in costs. Deposits move a ${e0(V.book.value)} book slowly;
          selling would move it faster, and in Box 3 a sale costs no tax, only the order.</p></div></div></div>`;
  lchart($("#gapChart"), { xType: "cat", cats: ["now", ...M.map(m => `m${m.m}`)], height: 200,
    series: [{ name: "gap to target", color: "var(--accent)", pts: [[0, gap0], ...M.map((m, i) => [i + 1, m.te])], w: 2 }],
    yfmt: v => v.toFixed(1) + "%", tfmt: v => v.toFixed(2) + "%", label: "the gap to the target over the months" });
}

function orderRows(M) {
  let out = "", since = 0;
  for (const m of M) {
    if (!m.orders.length) { since++; continue; }
    m.orders.forEach((o, i) => {
      const last = i === m.orders.length - 1;
      out += `<tr><td>${i ? "" : m.m}</td><td class="l">${o.cash ? "keep as cash" : `${o.n} × ${esc(o.label)}`}</td><td>${e0(o.value)}</td>
        <td>${p1(o.cost_pct, 2)}</td><td class="dim">${i ? "" : since ? `${since + 1} months` : "1 month"}</td>
        <td>${last ? e0(m.left) : ""}</td><td>${last ? p1(m.te, 2) : ""}</td></tr>`;
    });
    since = 0;
  }
  return out || `<tr><td colspan="7" class="l dim">no order in ${M.length} months</td></tr>`;
}

/* ==========================================================================
   implied views, and your own
   ========================================================================== */
function impPanel() {
  const A = V.assets.filter(a => a.held);
  const whole = A.reduce((t, a) => t + a.w_book * a.pi, 0) / 100;
  const d = A.map(a => ({ ...a, diff: a.implied - a.pi })).sort((x, y) => y.diff - x.diff);
  const up = d.filter(a => a.diff > 0.05).slice(0, 8), dn = d.filter(a => a.diff < -0.05).slice(-5).reverse();
  const row = a => `<tr><td class="l">${esc(a.label)}</td><td>${p1(a.w_book)}</td><td>${p1(a.pi)}</td><td>${p1(a.implied)}</td>
    <td class="${sgn(a.diff)}">${sg(a.diff)} pts</td><td><button class="lnk" data-t="${esc(a.t)}" data-r="${fx(a.implied, 1)}">+ view</button></td></tr>`;
  $("#imp").innerHTML = `<div class="txt"><p>Turn Markowitz round: given your weights, which returns would make your book the best mix?
    Over the book as a whole they add up to what the market expects of it, ${p1(whole)} a year; only how that is shared out differs. A holding
    that moves with the rest of your book needs a higher return to earn its place, so this is about the whole book, not each
    position's size.</p></div>
    <table class="tbl"><thead><tr><th class="l">Holding</th><th>Weight</th><th>Market's</th><th>Your book needs</th><th>Difference</th><th></th></tr></thead><tbody>
      <tr class="sec"><td colspan="6">You lean on these: your book only makes sense if they beat the market's estimate</td></tr>
      ${up.map(row).join("") || `<tr><td colspan="6" class="l dim">none</td></tr>`}
      <tr class="sec"><td colspan="6">and need less of these</td></tr>
      ${dn.map(row).join("") || `<tr><td colspan="6" class="l dim">none</td></tr>`}
    </tbody></table>`;
  $$("#imp .lnk").forEach(b => b.onclick = () => openView(b.dataset.t, b.dataset.r));
}

function viewsPanel() {
  const vs = V.views || [];
  const opts = [...V.assets].sort((a, b) => a.label.localeCompare(b.label)).map(a =>
    `<option value="${esc(a.t)}">${esc(a.label)}${a.fund ? " (fund)" : ""} · ${esc(a.t)}</option>`).join("");
  const A = Object.fromEntries(V.assets.map(a => [a.t, a]));
  $("#views").innerHTML = `
    ${vs.length ? `<table class="tbl"><thead><tr><th class="l">Holding</th><th>You expect</th><th>Confidence</th><th>Market's</th><th>Model now</th><th class="l">Note</th><th></th></tr></thead><tbody>
      ${vs.map(v => { const a = A[v.t]; return `<tr><td class="l">${esc(a ? a.label : v.t)}</td><td>${p1(v.ret)}</td><td>${esc(v.conf)}</td>
        <td>${a ? p1(a.pi) : "–"}</td><td>${a && V.mode === "views" ? p1(a.mu) : "–"}</td><td class="l dim">${esc(v.note)}</td>
        <td><button class="lnk" data-del="${esc(v.t)}">remove</button></td></tr>`; }).join("")}</tbody></table>`
      : `<div class="empty">No views yet. Without one, the model holds only what the market expects.</div>`}
    <div class="vform" id="vform">
      <select id="vT">${opts}</select>
      <span>expect <input class="r" type="number" id="vR" step="0.5" value="10">% a year</span>
      <select id="vC"><option value="low">low confidence: moves it a quarter of the way</option>
        <option value="mid" selected>middling: half way</option><option value="high">high: three quarters</option></select>
      <input class="note" id="vN" placeholder="why (for yourself)" maxlength="300">
      <button class="tbtn on" id="vSave">Save view</button>
    </div>
    <div class="disc">A view is the return a year you expect from one holding, before costs. On its own it moves that holding's
      expected return the confidence's share of the way from the market's to yours, and every holding correlated with it a part
      of that. Saving a view switches the returns to yours.</div>`;
  $("#vSave").onclick = async () => {
    const body = { t: $("#vT").value, ret: parseFloat($("#vR").value), conf: $("#vC").value, note: $("#vN").value };
    if (isNaN(body.ret)) { flash("A return, please."); return; }
    try {
      await api("/api/pf/mvo/view", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      S.mode = "views"; schedule();
    } catch (e) { flash(`Not saved: ${esc(e.message)}`); }
  };
  $$("#views [data-del]").forEach(b => b.onclick = async () => {
    await api("/api/pf/mvo/view/" + encodeURIComponent(b.dataset.del), { method: "DELETE" });
    if ((V.views || []).length <= 1 && S.mode === "views") S.mode = "market";
    schedule();
  });
}

function openView(t, r) {
  $("#vT").value = t; $("#vR").value = r;
  $("#s-views").scrollIntoView({ behavior: "smooth", block: "start" });
  $("#vR").focus();
}

/* ==========================================================================
   every holding, and how it works
   ========================================================================== */
function allPanel() {
  const A = [...V.assets].sort((a, b) => b.w_book - a.w_book || b.w_tgt - a.w_tgt);
  $("#all").innerHTML = `<details><summary>${A.length} holdings and funds</summary>
    <table class="tbl"><thead><tr><th class="l">Holding</th><th class="l">Group</th><th>Yours</th><th>Risk</th><th>Beta</th>
      <th>Market's</th><th>${V.mode === "views" ? "With views" : V.mode === "history" ? "Past" : "Used"}</th><th>Past</th><th>Model</th><th>Range</th></tr></thead><tbody>
    ${A.map(a => `<tr><td class="l">${esc(a.label)} <span class="dim">${esc(a.t)}</span></td><td class="l dim">${esc(a.group || "")}</td>
      <td>${p1(a.w_book)}</td><td>${p1(a.vol)}</td><td>${fx(a.beta, 2)}</td><td>${p1(a.pi)}</td><td>${p1(a.mu)}</td>
      <td class="dim">${p1(a.hist, 0)}</td><td>${a.w_tgt ? p1(a.w_tgt) : "–"}</td><td class="dim">${a.q90 > 0 ? `${fx(a.q10, 0)}–${fx(a.q90, 0)}%` : ""}</td></tr>`).join("")}
    </tbody></table></details>`;
}

function how() {
  $("#how").innerHTML = `<div class="txt">
    <div class="sec">The idea</div>
    <p>Harry Markowitz's point, in 1952, was that a share's risk inside a portfolio is not its own swing but how it moves with
      everything else. So for every level of risk there is one best mix, and the line of them is the <b>efficient frontier</b>.
      A mix below the line could earn more for the same swings, or swing less for the same return, with the same things held
      in other amounts.</p>
    <div class="sec">Three inputs</div>
    <p><b>Risk:</b> how every pair moved together over the last ${V.weeks} weeks, on five-day returns in euros (Amsterdam closes
      four and a half hours before New York, so one day's returns understate how European and American shares move together),
      pulled ${fx(V.shrink * 100, 0)}% of the way toward "every pair equally correlated" so that a noisy estimate is not taken
      at face value. The same matrix as <a href="/portfolio#riskmap">risk by holding</a>.</p>
    <p><b>Limits:</b> no short positions, at most ${fx(V.cap, 0)}% in one share, a fund without limit.</p>
    <p><b>Returns</b>, and they are the trouble. The classic input is past returns, and the classic result is a mix that buys
      whatever went up: switch to <i>Past</i> to see it. By default the returns are those market prices imply (Black and Litterman,
      1992): cash (${p1(V.rf, 2)}, the €STR fund's last three months) plus each holding's beta to world shares times the equity
      premium (${p1(V.erp)}: world shares over bills, 1871–2020, from FCST's history). The market pays only for market risk, so
      with nothing added the best mix per unit of risk is the world fund itself. That is the honest starting point, and why
      the frontier sits above your book: the part of each share's swing that is its own is not paid for, unless you know
      better. A <b>view</b> says you do, and moves the answer as far as your confidence: alone, a view moves its holding a quarter,
      half or three quarters of the way (τ = ${V.tau}).</p>
    <div class="sec">The cloud and the ranges</div>
    <p>The history is drawn again ${V.cloud.length} times in half-year blocks, the risk and returns estimated again from each,
      and the mix solved again at the same appetite for risk. Each answer is judged by today's estimates, so the cloud sits
      below the line: that is the cost of not knowing the inputs, and why weights from any optimiser deserve a range.</p>
    <div class="sec">The deposit plan</div>
    <p>Whole shares at what your orders cost: the median fee of the last year's buys per exchange from the ledger, and the median
      currency charge on dollar orders; DEGIRO's usual fee where you bought fewer than three times. Each month the order that
      takes the most tracking error off per euro is bought; with a ${p1(V.max_cost)} cost limit, a small deposit often waits a
      month. It never sells, and prices stay where they are today.</p>
    <div class="sec">What it leaves out</div>
    <ul>
      <li>It is one period's correlations. In a sell-off they rise toward one and the diversification shrinks.</li>
      <li>Risk here is swing, measured on five-day returns: it says nothing of a company failing or a currency collapsing.</li>
      <li>Beta is measured against a world fund in euros, over the same weeks; a share's beta moves too.</li>
      <li>Holdings with under two years of prices are left out and kept as they are.</li>
      <li>Costs are DEGIRO's per order; the funds' own yearly costs (0.07–0.25%) and any tax on dividends inside them are not modelled.</li>
      <li>It is not advice. The model knows prices and their history, nothing about the companies.</li>
    </ul></div>`;
}

load();
