/* ==========================================================================
   KAI — Koers AI, a paper-trading contest: Claude's €100,000 against Rick
   and the index. /api/pf/kai is the page (the scoreboard, the curve, the
   positions and their theses, the orders waiting for their open, the
   decisions and the trades); the decisions are made in the background
   (pf/koersai.py).
   ========================================================================== */

let O = null, POLL = null;
const LINES = [
  { k: "kai", name: "Koers AI", color: "var(--accent)", w: 2.2 },
  { k: "spx", name: "S&P 500", color: "var(--s2)", w: 1.6 },
  { k: "ndx", name: "Nasdaq 100", color: "var(--s3)", dash: "4 3" },
  { k: "you", name: "Rick", color: "var(--warn)", w: 1.6 },
];
const KIND = { start: "first", weekly: "weekly", event: "check", manual: "asked", review: "review" };

$("#shell").innerHTML = termBar({ here: "KAI · KOERS AI", right:
  `<button class="tbtn" id="decBtn" title="A decision now, outside the schedule" style="display:none">Decide now</button>` });
wireShell();

async function api(path, opt) {
  const r = await fetch("/api/pf" + path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
  return j;
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const when = iso => { const t = new Date(iso); return isNaN(t) ? "" :
  t.toLocaleString("en-GB", { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", timeZone: "Europe/Amsterdam" }); };
const sh = v => v == null ? "–" : nf(v, 2);

async function load() {
  try { O = await api("/kai"); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load KAI</b>${esc(e.message)}</div>`; return; }
  O.started ? render() : renderStart();
  pollJob(O.job);
  if (O.first_waiting && (!O.job || O.job.state !== "running")) POLL = setTimeout(load, 60000);
}

/* ---------- before the start ---------- */
function renderStart() {
  const R = O.rules;
  $("#decBtn").style.display = "none";
  $("#app").innerHTML = `<div class="panel"><div class="body">
    <div class="hdr"><h1>KOERS AI</h1><span class="sub">a paper-trading contest: Claude against you and the index</span></div>
    <p class="intro">Koers AI manages <b>${eur(R.capital)}</b> of paper money as a <b>${esc(R.profile)}</b>: durable competitive
      advantages, high returns on capital, strong balance sheets and growing free cash flow, at a reasonable price, in 8 to 12
      positions held for one to three years. Its aim is to finish ahead of <b>you</b> and of the <b>S&P 500</b> and
      <b>Nasdaq 100</b> on risk-adjusted return. It is your opponent, not your advisor: it sees your returns on the
      scoreboard, never your holdings.</p>
    <div class="rules">
      <div><b>Rules</b>Long only: no leverage, shorting, options or crypto. Fractional shares. No position above ${R.max_pos_pct}% at the time of purchase; cash anywhere from 0 to 100%.</div>
      <div><b>Universe</b>The ${R.n_market} highest-quality companies above $5bn in the US and Europe, by the watchlist's nightly scan, and its own holdings.</div>
      <div><b>When</b>Every Friday after the close, from ${esc(R.decide_at)}. On another weekday evening on which a holding reported or moved ${R.event_move}% or more, it may check again.</div>
      <div><b>Fills</b>At each share's next opening price, less ${R.cost_pct}% in costs. It never knows its fill price in advance.</div>
      <div><b>Score</b>Total return against the S&P 500 since the start, with the Sharpe ratio and the worst fall. All in euros.</div>
      <div><b>Cost to you</b>Each decision is one Claude Code run on your plan, without web search: it uses only the data the terminal sends it.</div>
    </div>
    <p class="intro">It starts the moment you press the button and only goes forward. Results mean little before about a year.</p>
    <div id="jobBox">${jobBox(O.job)}</div>
    <p><button class="go" id="goBtn">Start the contest with ${eur(R.capital)}</button></p>
  </div></div>`;
  $("#goBtn").onclick = async () => {
    if (!confirm(`Start the contest with ${eur(R.capital)} of paper money now? Koers AI's first decision runs at once, on your Claude plan.`)) return;
    $("#goBtn").disabled = true;
    try { const j = await post("/kai/start"); if (j.state === "waiting") return load(); pollJob(j); }
    catch (e) { $("#jobBox").innerHTML = `<div class="job err">Could not start: ${esc(e.message)}</div>`; $("#goBtn").disabled = false; }
  };
}

/* ---------- the running decision ---------- */
function jobBox(j) {
  if (!j || j.state === "idle") return "";
  if (j.state === "running") return `<div class="job">${j.kind === "review" ? "Koers AI is writing its self-review" :
    `Koers AI is deciding (${esc(KIND[j.kind] || j.kind)})`}: <b>${esc(j.phase || "starting")}</b>
    · ${Math.round((Date.now() / 1000 - j.started) / 60)} min${j.text ? `<div class="tail">${esc(j.text.slice(-400))}</div>` : ""}</div>`;
  if (j.state === "error") return `<div class="job err">The last ${j.kind === "review" ? "review" : "decision"} did not complete: ${esc(j.error)}</div>`;
  return "";
}

function pollJob(j) {
  clearTimeout(POLL);
  if ($("#jobBox")) $("#jobBox").innerHTML = jobBox(j);
  if (!j || j.state !== "running") return;
  POLL = setTimeout(async () => {
    let n;
    try { n = await api("/kai/job"); } catch { n = j; }
    if (n.state !== "running") return load();
    pollJob(n);
  }, 4000);
}

/* ---------- the contest ---------- */
function render() {
  const S = O.stats || {}, C = O.curve || {}, st = O.start, T = S.trades || {};
  const ret = k => S[k] ? S[k].ret : null;
  const gap = (a, b) => ret(a) != null && ret(b) != null ? ret(a) - ret(b) : null;
  const gapTile = (lab, k, sub) => tile(lab, gap("kai", k) == null ? "–" : `<span class="${sgn(gap("kai", k))}">${pp(gap("kai", k), 2)}</span>`,
    null, sub, k === "spx");
  $("#decBtn").style.display = "";
  $("#decBtn").onclick = async () => {
    if (!confirm("Ask Koers AI for a decision now, outside the schedule? One Claude Code run on your plan.")) return;
    try { pollJob(await post("/kai/decide")); } catch (e) { alert(e.message); }
  };
  $("#app").innerHTML = `
    <div class="panel"><div class="body">
      <div class="status">
        <span><b>Koers AI</b> since ${when(st.at)} · ${eur(st.capital)} of paper money · ${esc(O.rules.profile)}</span>
        <span class="sp"></span>
        <span>Decides on Friday after the close; checks on an evening a holding reported or moved ${O.rules.event_move}%;
          reviews itself on the first Saturday of each month</span>
        <label title="Off: it decides only when you press Decide now"><input type="checkbox" id="auto" ${O.auto ? "checked" : ""}> on schedule</label>
      </div>
      <div id="jobBox">${jobBox(O.job)}</div>
      ${O.first_waiting ? `<div class="job">The contest started ${when(st.at)}. Its first decision runs once the watchlist's scan has
        read the day's close, so its prices are fresh: the scan starts at 22:45 Amsterdam and can take up to an hour.</div>` : ""}
      ${O.message ? `<div class="rival"><span>Koers AI to Rick</span>${esc(O.message)}</div>` : ""}
      <div class="tiles">
        ${tile("Koers AI", eur(O.value), pcc(ret("kai"), 2), `cash ${eur(O.cash)} · ${O.positions.length} position${O.positions.length === 1 ? "" : "s"}`)}
        ${gapTile("Against the S&P 500", "spx", `the S&P 500 ${pc(ret("spx"), 2)} · the score`)}
        ${gapTile("Against the Nasdaq 100", "ndx", `the Nasdaq 100 ${pc(ret("ndx"), 2)}`)}
        ${gapTile("Against Rick", "you", `Rick ${pc(ret("you"), 2)} over the same days`)}
        ${tile("Sharpe ratio", sh(S.kai && S.kai.sharpe), null, `the S&P 500 ${sh(S.spx && S.spx.sharpe)} · over the euro overnight rate`)}
        ${tile("Worst fall", S.kai ? pc(S.kai.max_dd, 2) : "–", null, `the S&P 500 ${S.spx ? pc(S.spx.max_dd, 2) : "–"}`)}
        ${tile("Trades", T.n ?? 0, null, `${eur(T.costs || 0, 2)} in costs${T.hit != null ? ` · ${T.hit}% of sales at a profit` : ""}`)}
      </div>
      <div class="lc" id="chart" style="margin-top:.6rem"></div>
      ${boardHtml(S)}
    </div></div>
    <div class="grid">
      <div>
        <div class="panel"><div class="body">
          <div class="sec">Positions</div>${positionsHtml()}
          ${O.pending.length ? `<div class="sec">Waiting for the next open</div>${pendingHtml()}` : ""}
          <div class="sec">Trades</div>${tradesHtml()}
        </div></div>
      </div>
      <div>
        <div class="panel"><div class="body">
          <div class="sec">Its monthly reviews <button class="revBtn" id="revBtn" title="A self-review of the days since the last one, now">Review now</button></div>
          ${reviewsHtml()}
        </div></div>
        <div class="panel" style="margin-top:1rem"><div class="body">
          <div class="sec">Its decisions</div>
          <p class="foot" style="margin:.2rem 0 .6rem">What it writes: its journal, a reason for each trade, a thesis and an exit rule
            for each holding. Its private thinking is not passed on; <i>What it was sent</i> shows the data it decided from, word for word.</p>
          ${O.decisions.length ? O.decisions.map(decHtml).join("") : `<p class="intro">No decision yet.</p>`}
        </div></div>
      </div>
    </div>
    <p class="foot">Paper money only: nothing here places an order anywhere. Each order fills at the first opening price after the
      decision on its own exchange, less ${O.rules.cost_pct}% in costs; a decision's orders settle together once all have their open,
      sales first. A buy is cut to ${O.rules.max_pos_pct}% of the portfolio at that open, and to the cash. The lines are rebased to 100
      at the last New York close before the start: Rick by the time-weighted return, the indices as SPY and QQQ with dividends, all
      in euros. The Sharpe ratio is over the euro overnight rate (XEON).</p>`;
  $("#auto").onchange = e => post("/kai/auto", { on: e.target.checked }).catch(() => {});
  $("#revBtn").onclick = async () => {
    if (!confirm("Ask Koers AI for a self-review of the days since its last review (or the start), now? One Claude Code run on your plan; it places no orders.")) return;
    try { pollJob(await post("/kai/review")); } catch (e) { alert(e.message); }
  };
  $$(".revSent").forEach(b => b.onclick = async () => {
    const p = $("#rsent" + b.dataset.id), open = p.style.display === "none";
    if (open && !p.textContent) {
      p.textContent = "Loading…";
      try { p.textContent = (await api(`/kai/review/${b.dataset.id}`)).material || "Not kept for this review."; }
      catch (e) { p.textContent = e.message; }
    }
    p.style.display = open ? "" : "none";
    b.textContent = open ? "Hide what it was sent" : "What it was sent";
  });
  $$(".more").forEach(b => b.onclick = () => { const p = b.previousElementSibling; p.style.display = p.style.display ? "" : "none"; b.remove(); });
  // what it was sent comes from /kai/decision/{id}: some 60,000 characters, fetched when asked for
  $$(".sentBtn").forEach(b => b.onclick = async () => {
    const p = $("#sent" + b.dataset.id), open = p.style.display === "none";
    if (open && !p.textContent) {
      p.textContent = "Loading…";
      try { p.textContent = (await api(`/kai/decision/${b.dataset.id}`)).material || "Not kept for this decision."; }
      catch (e) { p.textContent = e.message; }
    }
    p.style.display = open ? "" : "none";
    b.textContent = open ? "Hide what it was sent" : "What it was sent";
  });
  wrapTables();
  drawCurve(C);
}

function boardHtml(S) {
  const row = (lab, k) => { const x = S[k];
    return `<tr><td>${lab}</td><td>${x ? pcc(x.ret, 2) : "–"}</td><td>${x ? sh(x.sharpe) : "–"}</td><td>${x ? pc(x.max_dd, 2) : "–"}</td>
      <td>${x && x.vol != null ? nf(x.vol, 1) + "%" : "–"}</td></tr>`; };
  const days = S.kai ? S.kai.days : 0;
  return `<table class="tbl" style="margin-top:.6rem"><thead><tr><th>Since the start</th><th>Return</th><th>Sharpe</th><th>Worst fall</th><th>Swing a year</th></tr></thead>
    <tbody>${row("Koers AI", "kai")}${row("S&P 500", "spx")}${row("Nasdaq 100", "ndx")}${row("Rick", "you")}</tbody></table>
    ${days < 60 ? `<p class="foot" style="margin-top:.3rem">${days} trading day${days === 1 ? "" : "s"} so far: too few for the Sharpe ratio or the worst fall to mean much.</p>` : ""}`;
}

function drawCurve(C) {
  const host = $("#chart");
  if (!C || !C.available) { host.innerHTML = `<div class="lc-empty">${esc((C && C.why) || "The curve starts after its first trading day.")}</div>`; return; }
  lchart(host, {
    series: LINES.filter(l => C[l.k]).map(l => ({ ...l, pts: C.d.map((d, i) => [d, C[l.k][i]]) })),
    yfmt: v => v.toFixed(1), tfmt: v => `${v.toFixed(2)} (${pc(v - 100, 2)})`, endLabels: true, refs: [{ y: 100 }],
    height: 240, label: "Koers AI against the S&P 500, the Nasdaq 100 and Rick, rebased to 100" });
}

function positionsHtml() {
  if (!O.positions.length) return `<p class="intro">All cash.</p>`;
  // The thesis gets its own line under each position: in a column of its own, the longest company name
  // (cells do not wrap) squeezed it to 220px and every row stood 170px tall.
  return `<table class="tbl pos"><thead><tr><th>Holding</th><th>Shares</th><th>Avg cost</th><th>Price</th><th>Value</th><th>Weight</th><th>Profit</th><th></th></tr></thead><tbody>
    ${O.positions.map(p => `<tr class="pr">
      <td><a href="/t/${encodeURIComponent(p.ticker)}">${esc(p.ticker)}</a>${p.name ? ` <span class="dim nm" title="${esc(p.name)}">${esc(p.name)}</span>` : ""}</td>
      <td>${nf(p.qty, 4)}</td><td>${eur(p.avg_eur, 2)}</td><td>${eur(p.px_eur, 2)}</td><td>${eur(p.value_eur)}</td><td>${nf(p.w, 1)}%</td>
      <td class="${sgn(p.pl_eur)}">${eur(p.pl_eur)}</td><td>${pcc(p.pl_pct, 1)}</td></tr>
      ${p.thesis || p.exit_if ? `<tr class="thr"><td colspan="8" class="w">${esc(p.thesis || "")}${p.exit_if ? ` <span class="dim">Exit if: ${esc(p.exit_if)}</span>` : ""}</td></tr>` : ""}`).join("")}
  </tbody></table>`;
}

function pendingHtml() {
  return O.pending.map(o => `<div class="ord"><span class="a ${esc(o.side)}">${esc(o.side)}</span>
    <span>${esc(o.ticker)} ${o.amount_all ? "all" : eur(o.amount_eur, 2)}</span>
    <span class="st">decided ${when(o.ts)}; fills at its market's next open</span></div>`).join("");
}

function tradesHtml() {
  if (!O.trades.length) return `<p class="intro">No trades yet.</p>`;
  return `<table class="tbl"><thead><tr><th>Day</th><th class="l">Trade</th><th>Open</th><th>Value</th><th>Cost</th><th class="l"></th></tr></thead><tbody>
    ${O.trades.map(t => `<tr><td>${dmy(t.d)}</td>
      <td class="l"><span class="${t.side === "BUY" ? "pos" : "neg"}">${t.side}</span> ${nf(t.qty, 4)} ${esc(t.ticker)}</td>
      <td>${nf(t.px)} ${esc(t.ccy || "")}</td><td>${eur(t.value_eur, 2)}</td><td>${eur(t.cost_eur, 2)}</td>
      <td class="l dim">${esc(t.note || "")}</td></tr>`).join("")}</tbody></table>`;
}

/* ---------- its monthly self-reviews (build 56) ---------- */
function reviewsHtml() {
  const R = O.reviews || [];
  const next = O.next_review ? `The next one: ${when(O.next_review)}, covering the days since the last${R.length ? "" : " (the contest's start)"}.` : "";
  if (!R.length) return `<p class="foot" style="margin:.2rem 0">Once a month, on the first weekend from Saturday ${esc(O.rules.review_at || "10:00")},
    Koers AI grades each thesis it held against what happened, judges its own process, and sets rule changes its next decisions
    read. It places no orders. ${next} A period under ${O.rules.review_min_days || 10} trading days waits for the next month.</p>`;
  const list = (lab, xs) => xs && xs.length ? `<div class="lab">${lab}</div><ul class="lst">${xs.map(x => `<li>${esc(x)}</li>`).join("")}</ul>` : "";
  const one = (r, i) => {
    const b = r.body || {}, m = r.meta || {};
    const grades = (b.grades || []).map(g => `<div class="gr"><span class="g ${esc(g.grade)}">${esc(g.grade)}</span><b>${esc(g.ticker)}</b>
      <span class="ts">${esc(g.thesis || "")}</span><span class="w">${esc(g.what_happened || "")}${g.lesson ? ` <span class="dim">Lesson: ${esc(g.lesson)}</span>` : ""}</span></div>`).join("");
    const detail = `${grades ? `<div class="lab">Its theses, graded</div>${grades}` : ""}
      ${b.process ? `<div class="lab">Its process</div><div class="lst" style="padding:0">${esc(b.process)}</div>` : ""}
      ${list("Mistakes", b.mistakes)}${list("Keep doing", b.keep_doing)}${list("Rule changes from now on", b.rule_changes)}
      ${r.has_material ? `<button class="revSent" data-id="${r.id}">What it was sent</button><pre class="sent" id="rsent${r.id}" style="display:none"></pre>` : ""}`;
    return `<div class="dec rv">
      <div class="hd"><span class="kind review">${r.kind === "manual" ? "asked" : "review"}</span><span>${dmy(r.d_from)} to ${dmy(r.d_to)}</span>
        <span style="flex:1"></span><span>written ${when(r.ts)}${m.model ? ` · ${esc(m.model)}` : ""}${m.ms ? ` · ${Math.max(1, Math.round(m.ms / 60000))} min` : ""}</span></div>
      <div class="sum">${esc(r.summary || "")}</div>
      ${i ? `<div style="display:none">${detail}</div><button class="more">The grades and lessons</button>` : detail}
    </div>`;
  };
  return R.map(one).join("") + (next ? `<p class="foot" style="margin:.2rem 0">${next}</p>` : "");
}

function decHtml(d, i) {
  const m = d.meta || {};
  // the order book keeps a decision's accepted trades in the order it gave them
  const trade = (t, k) => {
    const f = (d.fills || [])[k];
    return `<div class="ord"><span class="a ${esc(t.action)}">${esc(t.action)}</span>
      <span>${esc(t.ticker)} ${t.amount === "ALL" ? "all" : eur(t.amount, 2)}${t.note ? ` <span class="dim">(${esc(t.note)})</span>` : ""}</span>
      <span class="w">${esc(t.reason)}</span>
      ${f ? `<span class="st">${f.status === "filled" ? `filled ${dmy(f.fill_d)}: ${esc(f.note || "")}` : f.status === "pending" ? "waiting for the open" : `not filled: ${esc(f.note || "")}`}</span>` : ""}</div>`;
  };
  const rej = t => `<div class="ord rej"><span class="a">${esc(t.action || "")}</span><span>${esc(t.ticker)} ${typeof t.amount === "number" ? eur(t.amount, 2) : esc(t.amount || "")}</span>
    <span class="st">refused: ${esc(t.why)}</span></div>`;
  const th = (d.theses || []).map(t => `<div class="th"><b>${esc(t.ticker)}</b>${esc(t.thesis)}${t.exit_if ? ` <span class="dim">Exit if: ${esc(t.exit_if)}</span>` : ""}</div>`).join("");
  return `<div class="dec">
    <div class="hd"><span class="kind ${esc(d.kind)}">${esc(KIND[d.kind] || d.kind)}</span><span>${when(d.ts)}</span>
      <span style="flex:1"></span><span>${esc(m.model || "")}${m.ms ? ` · ${Math.max(1, Math.round(m.ms / 60000))} min` : ""} · ${d.n_universe} tickers sent</span></div>
    ${d.kind === "event" && d.trigger ? `<div class="why">${esc(d.trigger)}</div>` : ""}
    <div class="jr">${esc(d.journal)}</div>
    ${d.message ? `<div class="msg">“${esc(d.message)}”</div>` : ""}
    ${(d.trades || []).map(trade).join("") || `<div class="why">No trades.</div>`}
    ${(d.rejected || []).map(rej).join("")}
    ${th ? `<div ${i ? 'style="display:none"' : ""}>${th}</div>${i ? `<button class="more">Its theses</button>` : ""}` : ""}
    ${d.has_material ? `<button class="sentBtn" data-id="${d.id}">What it was sent</button><pre class="sent" id="sent${d.id}" style="display:none"></pre>` : ""}
  </div>`;
}

load();
