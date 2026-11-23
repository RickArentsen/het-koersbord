/* ==========================================================================
   OPT's strategies: the builder on one share's page, and on the book's page
   the premium screener, what Cboe's strategy indexes did, and why option
   sellers earn (and when they stop).

   Server side: pf/strategy.py (templates, the multi-leg arithmetic, the
   screener) and pf/strathist.py (Cboe's indexes, the VIX against realised).
   ========================================================================== */

const OptStrat = (() => {
  const f = (v, d = 2) => v == null || isNaN(v) ? "–" : Number(v).toLocaleString("en-GB", { minimumFractionDigits: d, maximumFractionDigits: d });
  const usd = (v, d = 0) => v == null ? "–" : (v < 0 ? "−$" : "$") + f(Math.abs(v), d);
  const eu0 = v => v == null ? "" : ` <span class="dim">€${f(Math.abs(v), 0)}</span>`;
  const p0 = v => v == null ? "–" : f(v * 100, 0) + "%";
  const sg = (v, d = 1, u = "%") => { if (v == null) return "–"; const r = Number(Math.abs(v).toFixed(d)); return (r === 0 ? "" : v > 0 ? "+" : "−") + f(r, d) + u; };
  const sgc = (v, d = 1, u = "%") => `<span class="${sgn(v)}">${sg(v, d, u)}</span>`;
  const get = async u => { const r = await fetch(u); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); };
  const HIST_FOR = { covered_call: ["BXM", "BXY", "BXMD"], csp: ["PUT", "WPUT"], protective_put: ["PPUT"], collar: ["CLL", "CLLZ"],
    iron_condor: ["CNDR"], iron_fly: ["BFLY"] };

  /* ======================================================================
     the builder, on /m/OPT/SYM
     ====================================================================== */
  let B = { sym: null, cat: null, tpl: "covered_call", R: null, legs: null, seq: 0 };

  async function builder(host, sym) {
    B.sym = sym;
    host.innerHTML = `<header><h2>Strategies</h2><span class="hint">several options at once, worked out on this chain: pick one, then move any leg</span></header>
      <div class="body"><div id="stPick" class="stpick"></div><div id="stBody">${loadingTxt("placing the legs")}</div></div>`;
    try { B.cat = await get("/api/pf/opt/strategies"); } catch (e) { $("#stBody").innerHTML = note(e.message); return; }
    pickBar();
    run(null);
  }
  const loadingTxt = t => `<div class="dim ld">${t}<span class="blink">_</span></div>`;
  const note = t => `<div class="note"><b>Could not work it out</b>${esc(t)}</div>`;

  function pickBar() {
    const by = {};
    for (const t of B.cat.templates) (by[t.view] = by[t.view] || []).push(t);
    $("#stPick").innerHTML = Object.entries(B.cat.views).map(([v, label]) => by[v] ? `<div class="stgrp"><span class="lbl">${esc(label)}</span>
      ${by[v].map(t => `<button class="seg ${t.k === B.tpl ? "on" : ""}" data-k="${t.k}" title="${esc(t.what)}">${esc(t.name)}</button>`).join("")}</div>` : "").join("");
    $$("#stPick .seg").forEach(b => b.onclick = () => { B.tpl = b.dataset.k; $$("#stPick .seg").forEach(x => x.classList.toggle("on", x === b)); run(null); });
  }

  async function run(legs) {
    const seq = ++B.seq;
    $("#stBody").classList.add("busy");
    let R;
    try {
      R = legs ? await (await fetch(`/api/pf/opt/strategy/${encodeURIComponent(B.sym)}`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tpl: B.tpl, legs }) })).json()
        : await get(`/api/pf/opt/strategy/${encodeURIComponent(B.sym)}?tpl=${B.tpl}`);
    } catch (e) { R = { available: false, why: e.message }; }
    if (seq !== B.seq) return;
    $("#stBody").classList.remove("busy");
    if (!R.available) { $("#stBody").innerHTML = note(R.why || "no answer"); return; }
    B.R = R;
    B.legs = R.legs.map(l => ({ type: l.type, side: l.side, n: l.n, exp: l.exp, strike: l.strike }));
    render();
  }

  function render() {
    const R = B.R;
    const capNote = R.shares_needed > 0 ? `<div class="note ${R.shares_held >= R.shares_needed ? "plain" : ""}"><b>${R.shares_needed} shares</b>
      An option is on a hundred shares. This needs ${R.shares_needed} ${esc(R.sym)} shares (${usd(R.spot * R.shares_needed)}${R.fx ? `, €${f(R.spot * R.shares_needed / R.fx, 0)}` : ""}); you hold ${f(R.shares_held, 0)}.
      ${R.shares_held < R.shares_needed ? "Buying the rest is part of the strategy's cost below." : ""}</div>` : "";
    const hist = HIST_FOR[R.tpl];
    $("#stBody").innerHTML = `
      <div class="stwhat"><b>${esc(R.name || "Your legs")}.</b> ${esc(R.what || "")} <span class="dim">${esc(R.when || "")}</span>
        ${R.risk ? `<br><span class="warnc">Risk:</span> ${esc(R.risk)}` : ""}
        ${hist ? ` <a href="/m/OPT#s-shist">How Cboe's ${hist.join(", ")} did since 1986 ›</a>` : ""}</div>
      ${capNote}
      <table class="tbl stlegs"><thead><tr><th class="l">Leg</th><th class="l">Buy / sell</th><th>Contracts</th><th class="l">Expiry</th><th class="l">Strike</th>
        <th>Price</th><th>Bid / ask</th><th>Implied vol</th><th>Delta</th><th>Open interest</th><th></th></tr></thead><tbody>
        ${R.legs.map((l, i) => legRow(l, i)).join("")}</tbody></table>
      <div class="stadd"><span class="dim">Add a leg:</span> <button class="lnk" data-add="C">+ call</button> <button class="lnk" data-add="P">+ put</button>
        <button class="lnk" data-add="S">+ 100 shares</button></div>
      <div class="tiles sttiles">
        ${tile(R.cost > 0 ? "Costs" : "Pays", usd(R.cost), null, `${R.cost_eur != null ? `€${f(Math.abs(R.cost_eur), 0)} · ` : ""}at mid prices`, true)}
        ${tile("Most it makes", R.unlimited_gain ? "no limit" : usd(R.max_gain), null, `by ${dmy(R.first_exp)}`)}
        ${tile("Most it loses", R.unlimited_loss ? "no limit" : usd(R.max_loss), null, R.breakevens.length ? `break-even ${R.breakevens.map(x => "$" + f(x)).join(" / ")}` : "no break-even in range")}
        ${tile("Chance of a profit", p0(R.p_profit), null, `at expiry, from the chain's own odds${R.p_profit_rv != null ? ` · ${p0(R.p_profit_rv)} by its own swings` : ""}`)}
        ${tile("Expected result", usd(R.ev_market), null, `market's odds, after interest${R.ev_rv != null ? ` · ${usd(R.ev_rv)} by the share's swings` : ""}`)}
        ${tile("Ties up", R.capital == null ? "margin" : usd(R.capital), null, esc(R.capital_how || ""))}
        ${tile("Greeks", `${sg(R.greeks.delta, 0, "")} sh`, null, `delta in shares · θ ${usd(R.greeks.theta)} a day · vega ${usd(R.greeks.vega)} a point`)}
      </div>
      <div class="lc" id="stPay"></div>
      <div class="fkey"><span><i style="border-color:var(--accent)"></i>at ${dmy(R.first_exp)}</span><span><i style="border-color:var(--s4);border-top-style:dashed"></i>half way, ${dmy(R.half_date)}</span>
        <span><i style="border-color:var(--ink-mid);border-top-style:dotted"></i>today</span><span><i class="box" style="background:rgba(79,168,206,.12)"></i>2 in 3 · 19 in 20 range the chain prices</span></div>
      <ul class="say">${(R.say || []).map(s => `<li>${esc(s)}</li>`).join("")}</ul>
      <div class="src">${esc(R.fee_note)} Figures for one set of contracts; each contract is ${100} shares. Prices from ${R.mode === "quotes" ? "the session's quotes" : "the last trades"}, session of ${dmy(R.session)}.
        The expected results use each expiry's implied distribution, and a lognormal at the share's three-month realised volatility (${R.rv != null ? f(R.rv * 100, 0) + "%" : "–"}), both net of what the cash would earn at ${f((R.rate || 0) * 100, 2)}%. Not advice.</div>`;
    wireLegs();
    drawPay();
    wrapTables();
  }

  function legRow(l, i) {
    const R = B.R;
    if (l.type === "S") return `<tr><td class="l"><b>Shares</b></td><td class="l">${sideSel(l, i)}</td><td>${nIn(l, i)}×100</td><td class="l dim">–</td><td class="l dim">–</td>
      <td>${usd(l.px, 2)}</td><td class="dim">–</td><td class="dim">–</td><td>${f(l.side * l.n * 100, 0)} sh</td><td class="dim">–</td><td><button class="lnk" data-del="${i}">remove</button></td></tr>`;
    const ks = (R.strikes[l.exp] || {})[l.type] || [];
    return `<tr><td class="l"><b>${l.type === "C" ? "Call" : "Put"}</b> <span class="dim">${sg(l.moneyness, 1)}</span></td>
      <td class="l">${sideSel(l, i)}</td><td>${nIn(l, i)}</td>
      <td class="l"><select class="seg" data-i="${i}" data-f="exp">${R.expiries.map(e => `<option value="${e.exp}" ${e.exp === l.exp ? "selected" : ""}>${dmy(e.exp)} · ${Math.round(e.days)}d</option>`).join("")}</select></td>
      <td class="l"><select class="seg" data-i="${i}" data-f="strike">${(ks.includes(l.strike) ? ks : [l.strike, ...ks]).map(k => `<option value="${k}" ${k === l.strike ? "selected" : ""}>${f(k, k % 1 ? 2 : 0)}</option>`).join("")}</select></td>
      <td>${usd(l.px, 2)}</td><td class="dim">${l.bid ? `${f(l.bid)} / ${f(l.ask)}` : "last trade"}</td><td>${l.iv ? f(l.iv * 100, 1) + "%" : "–"}</td>
      <td>${f(l.delta * l.side, 2)}</td><td>${l.oi != null ? l.oi.toLocaleString("en-GB") : "–"}</td><td><button class="lnk" data-del="${i}">remove</button></td></tr>`;
  }
  const sideSel = (l, i) => `<select class="seg" data-i="${i}" data-f="side"><option value="1" ${l.side > 0 ? "selected" : ""}>buy</option><option value="-1" ${l.side < 0 ? "selected" : ""}>sell</option></select>`;
  const nIn = (l, i) => `<input class="stn" type="number" min="1" max="50" step="1" value="${l.n}" data-i="${i}" data-f="n">`;

  function wireLegs() {
    $$("#stBody [data-f]").forEach(el => el.onchange = () => {
      const i = +el.dataset.i, k = el.dataset.f, L = B.legs.map(x => ({ ...x }));
      if (k === "side") L[i].side = +el.value;
      else if (k === "n") L[i].n = Math.max(1, Math.min(50, parseInt(el.value) || 1));
      else if (k === "strike") L[i].strike = +el.value;
      else if (k === "exp") {
        L[i].exp = el.value;
        const ks = ((B.R.strikes[el.value] || {})[L[i].type]) || null;
        if (ks && ks.length) L[i].strike = ks.reduce((a, b) => Math.abs(b - L[i].strike) < Math.abs(a - L[i].strike) ? b : a);
      }
      run(L);
    });
    $$("#stBody [data-del]").forEach(b => b.onclick = () => {
      const L = B.legs.filter((_, i) => i !== +b.dataset.del);
      if (!L.some(l => l.type !== "S")) { flash("A strategy needs at least one option."); return; }
      run(L);
    });
    $$("#stBody [data-add]").forEach(b => b.onclick = () => {
      const t = b.dataset.add, L = B.legs.map(x => ({ ...x }));
      const exp = (L.find(l => l.type !== "S") || {}).exp || (B.R.expiries.find(e => e.days >= 14) || B.R.expiries[0]).exp;
      if (t === "S") L.push({ type: "S", side: 1, n: 1, exp });
      else {
        const ks = ((B.R.strikes[exp] || {})[t]) || [];
        const k = ks.reduce((a, x) => Math.abs(x - B.R.spot) < Math.abs(a - B.R.spot) ? x : a, ks[0]);
        if (k == null) { flash("No priced strike at that expiry."); return; }
        L.push({ type: t, side: 1, n: 1, exp, strike: k });
      }
      run(L);
    });
  }

  function drawPay() {
    const host = $("#stPay"), R = B.R, C = R.curve;
    host.__draw = drawPay; CHARTS.add(host);
    const W = Math.max(300, Math.round(host.clientWidth || 700)); host.__w = W;
    const H = W < 560 ? 240 : 300, M = { l: 58, r: 14, t: 10, b: 26 }, pw = W - M.l - M.r, ph = H - M.t - M.b;
    const ys = [...C.end, ...C.now, ...C.half];
    const yt = nice(Math.min(...ys, 0), Math.max(...ys, 0), 5), xt = nice(C.x[0], C.x[C.x.length - 1], W < 560 ? 5 : 8);
    const X = x => M.l + (x - xt.lo) / (xt.hi - xt.lo) * pw, Y = y => M.t + (yt.hi - y) / (yt.hi - yt.lo) * ph;
    let g = "";
    const q = R.range;
    if (q) {
      g += `<rect x="${X(q[0])}" y="${M.t}" width="${X(q[4]) - X(q[0])}" height="${ph}" fill="rgba(79,168,206,.06)"/>`;
      g += `<rect x="${X(q[1])}" y="${M.t}" width="${X(q[3]) - X(q[1])}" height="${ph}" fill="rgba(79,168,206,.08)"/>`;
    }
    for (const t of yt.ticks) g += `<line class="gl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(t)}" y2="${Y(t)}"/><text class="ax" x="${M.l - 6}" y="${Y(t) + 3}" text-anchor="end">${usd(t)}</text>`;
    for (const t of xt.ticks) g += `<text class="ax" x="${X(t)}" y="${H - 8}" text-anchor="middle">$${f(t, 0)}</text>`;
    g += `<line x1="${M.l}" x2="${M.l + pw}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--ink-soft)"/>`;
    g += `<line x1="${X(R.spot)}" x2="${X(R.spot)}" y1="${M.t}" y2="${M.t + ph}" stroke="var(--ink-mid)" stroke-dasharray="2 3"/>
      <text class="ax" x="${X(R.spot) + 4}" y="${M.t + 10}">now $${f(R.spot)}</text>`;
    // profit green, loss red, under the expiry line
    let pos = "", neg = "";
    C.x.forEach((x, i) => {
      const v = C.end[i], y0 = Y(0), y1 = Y(v);
      const seg = `M${X(x).toFixed(1)} ${y0.toFixed(1)}L${X(x).toFixed(1)} ${y1.toFixed(1)}`;
      if (v > 0) pos += seg; else if (v < 0) neg += seg;
    });
    g += `<path d="${pos}" stroke="rgba(63,179,128,.18)" stroke-width="${(pw / C.x.length + 0.6).toFixed(1)}"/><path d="${neg}" stroke="rgba(224,100,79,.15)" stroke-width="${(pw / C.x.length + 0.6).toFixed(1)}"/>`;
    const line = (ys, color, w, dash) => `<path d="${C.x.map((x, i) => (i ? "L" : "M") + X(x).toFixed(1) + " " + Y(ys[i]).toFixed(1)).join("")}" fill="none" stroke="${color}" stroke-width="${w}" ${dash ? `stroke-dasharray="${dash}"` : ""}/>`;
    g += line(C.now, "var(--ink-mid)", 1.2, "1.5 3") + line(C.half, "var(--s4)", 1.3, "5 4") + line(C.end, "var(--accent)", 2.2);
    for (const b of R.breakevens) g += `<circle cx="${X(b)}" cy="${Y(0)}" r="3.5" fill="var(--warn)"/><text class="ax" x="${X(b)}" y="${Y(0) - 7}" text-anchor="middle" style="fill:var(--warn)">$${f(b)}</text>`;
    for (const l of R.legs) if (l.type !== "S") g += `<line x1="${X(l.strike)}" x2="${X(l.strike)}" y1="${M.t + ph - 6}" y2="${M.t + ph}" stroke="${l.type === "C" ? "var(--s1)" : "var(--s2)"}" stroke-width="2"/>`;
    g += `<line class="xh" x1="0" x2="0" y1="${M.t}" y2="${M.t + ph}" style="display:none" stroke="var(--ink-soft)" stroke-dasharray="2 3"/>`;
    host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="the strategy's payoff">${g}<rect class="hov" x="${M.l}" y="${M.t}" width="${pw}" height="${ph}" fill="transparent"/></svg><div class="lc-tip" style="display:none"></div>`;
    const svg = host.querySelector("svg"), tip = host.querySelector(".lc-tip"), xh = svg.querySelector(".xh");
    svg.querySelector(".hov").addEventListener("mousemove", ev => {
      const r = svg.getBoundingClientRect(), x = xt.lo + ((ev.clientX - r.left) * (W / r.width) - M.l) / pw * (xt.hi - xt.lo);
      let i = 0; while (i < C.x.length - 1 && C.x[i + 1] < x) i++;
      xh.setAttribute("x1", X(C.x[i])); xh.setAttribute("x2", X(C.x[i])); xh.style.display = "";
      tip.innerHTML = `<div class="h">${R.sym} at $${f(C.x[i])} <span class="dim">${sg((C.x[i] / R.spot - 1) * 100, 1)}</span></div>
        <div class="r"><span>at ${dmy(R.first_exp)}</span><b class="${sgn(C.end[i])}">${usd(C.end[i])}</b></div>
        <div class="r"><span>half way</span><b>${usd(C.half[i])}</b></div><div class="r"><span>today</span><b>${usd(C.now[i])}</b></div>`;
      tip.style.display = ""; const sx = (X(C.x[i]) / W) * r.width, tw = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(host.clientWidth - tw, sx + (sx > host.clientWidth / 2 ? -tw - 12 : 12))) + "px"; tip.style.top = "6px";
    });
    svg.querySelector(".hov").addEventListener("mouseleave", () => { tip.style.display = "none"; xh.style.display = "none"; });
  }

  /* ======================================================================
     the book's page: the screener, the history, why sellers earn
     ====================================================================== */
  let H = { since: "2007-01", ccy: "EUR", on: ["BXM", "PUT", "CNDR", "PPUT"], D: null };
  // The book's page redraws its own table whenever it sorts or re-reads, and
  // this block is moved back in afterwards: so it finds its parts in itself.
  let OV = null;
  const q = sel => OV.querySelector(sel), qq = sel => [...OV.querySelectorAll(sel)];

  async function overview(host) {
    OV = host;
    host.innerHTML = `
      <div class="panel" id="s-screen"><header><h2>Premium on your holdings</h2><span class="hint">a month out, the 30-delta call and put on each share you hold · click one to build it</span></header>
        <div class="body" id="scrB">${loadingTxt("reading the stored chains")}</div></div>
      <div class="panel" id="s-shist"><header><h2>What option strategies have done</h2><span class="hint">Cboe's strategy indexes on the S&amp;P 500, against the index with dividends</span></header>
        <div class="body" id="shB">${loadingTxt("reading Cboe's histories")}</div></div>
      <div class="panel" id="s-sellers"><header><h2>Why sellers earn, and when they stop</h2><span class="hint">the VIX against what the S&amp;P 500 then did, month by month since 1990</span></header>
        <div class="body" id="selB">${loadingTxt("reading the VIX")}</div></div>`;
    get("/api/pf/opt/screen").then(renderScreen).catch(e => { q("#scrB").innerHTML = note(e.message); });
    loadHist();
  }

  function renderScreen(S) {
    const rows = S.rows || [];
    if (!rows.length) { q("#scrB").innerHTML = `<div class="empty">No stored chains yet: OPT reads every holding once a New York session. ${esc(S.why || "")}</div>`; return; }
    q("#scrB").innerHTML = `<table class="tbl"><thead><tr><th class="l">Share</th><th>Price</th><th class="l">Expiry</th>
        <th title="Thirty-day implied volatility">Priced</th><th title="What the share did over the last month">Delivered</th><th title="Priced over delivered">Ratio</th><th title="Today's priced figure among the sessions stored">Rank</th>
        <th class="sec2">Sell a call at</th><th>Pays</th><th>A year</th><th>Called</th>
        <th class="sec2">Sell a put at</th><th>Pays</th><th>A year</th><th>Assigned</th><th>Cash for it</th><th>You hold</th></tr></thead><tbody>
      ${rows.map(r => `<tr class="pk" data-s="${esc(r.sym)}">
        <td class="l"><a href="/m/OPT/${encodeURIComponent(r.sym)}#s-strat"><b>${esc(r.sym)}</b></a> <span class="dim">${esc((r.name || "").slice(0, 18))}</span>${r.earnings ? ` <span class="bdg warn" title="Earnings ${esc(r.earnings)}, before the expiry">earnings ${dm(r.earnings)}</span>` : ""}</td>
        <td>${f(r.spot)}</td><td class="l">${dm(r.exp)} <span class="dim">${Math.round(r.days)}d</span></td>
        <td>${r.iv30 != null ? f(r.iv30, 0) + "%" : "–"}</td><td>${r.rv21 != null ? f(r.rv21, 0) + "%" : "–"}</td>
        <td class="${r.ratio >= 1.3 ? "warnc" : ""}">${r.ratio != null ? f(r.ratio, 2) : "–"}</td><td>${r.rank != null ? ord(r.rank) : `<span class="dim">${r.n_days}/${S.min_history}</span>`}</td>
        ${r.call ? `<td class="sec2">${f(r.call.strike, r.call.strike % 1 ? 2 : 0)} <span class="dim">${sg(r.call.otm, 0)}</span></td><td>${f(r.call.yield, 1)}%</td><td><b>${f(r.call.yield_y, 0)}%</b></td><td>${p0(r.call.p)}</td>` : `<td class="sec2" colspan="4">–</td>`}
        ${r.put ? `<td class="sec2">${f(r.put.strike, r.put.strike % 1 ? 2 : 0)} <span class="dim">${sg(r.put.otm, 0)}</span></td><td>${f(r.put.yield, 1)}%</td><td><b>${f(r.put.yield_y, 0)}%</b></td><td>${p0(r.put.p)}</td><td>$${f(r.cash_for_put, 0)}</td>` : `<td class="sec2" colspan="5">–</td>`}
        <td class="${r.lots ? "" : "dim"}">${f(r.held, 0)}${r.lots ? ` <span class="dim">${r.lots} lot${r.lots > 1 ? "s" : ""}</span>` : ""}</td></tr>`).join("")}
      </tbody></table>
      <div class="src">From the chains OPT stored in the last session (${esc((S.sessions || []).join(", ") || "–")}), never a fresh download. "Pays" is the option's middle price as a share of the
        share price (the call) or of the strike (the put); "a year" scales it by the days to expiry, as if it could be repeated, which it cannot always be. "Called" and "assigned" are the chain's own odds of
        finishing beyond the strike. A high ratio of priced to delivered is where selling has been paid most; it is also where the market expects something, earnings above all.
        One contract is a hundred shares: covering a call needs a hundred of them, securing a put needs the strike times a hundred in cash. Not advice.</div>`;
    qq("#scrB tr.pk").forEach(tr => tr.onclick = e => { if (!e.target.closest("a")) location.assign("/m/OPT/" + encodeURIComponent(tr.dataset.s) + "#s-strat"); });
    wrapTables();
  }

  async function loadHist() {
    try { H.D = await get(`/api/pf/opt/history?since=${H.since}&ccy=${H.ccy}`); }
    catch (e) { q("#shB").innerHTML = note(e.message); q("#selB").innerHTML = ""; return; }
    if (!H.D.available) { q("#shB").innerHTML = note(H.D.why); return; }
    renderHist(); renderSellers();
  }

  function renderHist() {
    const h = H.D.history, rows = h.rows;
    const span = [["1991-01", "Since 1991"], ["2000-01", "2000"], ["2007-01", "2007"], ["2016-01", "10 years"], ["2021-01", "5 years"]];
    q("#shB").innerHTML = `<div class="fbar"><span class="lbl">From</span><span class="grp" id="shSince">${span.map(([v, l]) => `<button class="seg ${v === H.since ? "on" : ""}" data-v="${v}">${l}</button>`).join("")}</span>
        <span class="lbl" style="margin-left:.6rem">In</span><span class="grp" id="shCcy">${["EUR", "USD"].map(c => `<button class="seg ${c === H.ccy ? "on" : ""}" data-v="${c}">${c === "EUR" ? "euros" : "dollars"}</button>`).join("")}</span>
        ${H.ccy === "EUR" && H.since < "1999-01" ? `<span class="dim">euros start in 1999</span>` : ""}</div>
      <table class="tbl"><thead><tr><th class="l">Strategy</th><th class="l">From</th><th>A year</th><th class="dim">S&amp;P</th><th>Swings</th><th class="dim">S&amp;P</th><th>Worst fall</th><th class="dim">S&amp;P</th>
        <th title="Return over Treasury bills per unit of swing">Sharpe</th><th class="dim">S&amp;P</th><th>Beta</th><th title="Of the S&P's average up month, and down month">Up / down</th><th>Years ahead</th></tr></thead><tbody>
      ${["income", "protection", "calm"].map(g => `<tr class="sec"><td colspan="13">${{ income: "Selling for income", protection: "Buying protection", calm: "Betting on calm" }[g]}</td></tr>` +
        rows.filter(r => r.group === g).map(r => `<tr title="${esc(r.what)}"><td class="l"><b>${esc(r.k)}</b> ${esc(r.name)}</td><td class="l dim">${mon(r.from + "-01")}</td>
          <td><b>${f(r.cagr, 1)}%</b></td><td class="dim">${f(r.bench.cagr, 1)}%</td><td>${f(r.vol, 1)}%</td><td class="dim">${f(r.bench.vol, 1)}%</td>
          <td class="neg">${f(r.worst, 0)}%</td><td class="dim">${f(r.bench.worst, 0)}%</td><td>${f(r.sharpe, 2)}</td><td class="dim">${f(r.bench.sharpe, 2)}</td>
          <td>${f(r.beta, 2)}</td><td>${r.up ?? "–"}% / ${r.down ?? "–"}%</td><td>${r.years_won} of ${r.years}</td></tr>`).join("")).join("")}
      </tbody></table>
      <div class="fbar" style="margin-top:.6rem"><span class="lbl">Draw</span>${chips("shOn", rows.map((r, i) => ({ k: r.k, name: r.k, color: ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--warn)"][i % 5] })), H.on)}</div>
      <div class="lc" id="shChart"></div>
      <div class="two even"><div><div class="sec">By the VIX at the start of the month</div><div id="shReg"></div></div>
        <div><div class="sec">Year by year</div><div id="shYears"></div></div></div>
      <div class="txt"><p>${histWords(h)}</p></div>
      <div class="src">Cboe's indexes, daily, ${esc(h.asof)}; the S&amp;P 500 with dividends from Yahoo (^SP500TR); ${h.ccy === "EUR" ? "in euros at the ECB's month-end rate; " : ""}Treasury bills for Sharpe. Each strategy is measured over its own unbroken months from the start chosen, the S&amp;P over the same months.
        The indexes pay no fees or taxes and roll mechanically; a private investor pays both, and wider spreads. ${(h.missing || []).length ? "Not read: " + h.missing.join(", ") + "." : ""}</div>`;
    qq("#shSince .seg").forEach(b => b.onclick = () => { H.since = b.dataset.v; loadHist(); });
    qq("#shCcy .seg").forEach(b => b.onclick = () => { H.ccy = b.dataset.v; loadHist(); });
    qq("#shOn .chip").forEach(b => b.onclick = () => {
      const k = b.dataset.k, i = H.on.indexOf(k);
      if (i >= 0) { if (H.on.length > 1) H.on.splice(i, 1); } else H.on.push(k);
      b.classList.toggle("on", H.on.includes(k));
      drawHist(); years();
    });
    drawHist(); regimes(); years();
    wrapTables();
  }

  function histWords(h) {
    const R = h.rows;
    if (!R.length) return "";
    const lag = R.filter(r => r.cagr < r.bench.cagr), soft = R.filter(r => r.worst > r.bench.worst);
    const best = [...R].sort((a, b) => (b.sharpe || -9) - (a.sharpe || -9))[0];
    const out = [`Over these months ${lag.length} of the ${R.length} strategies earned less than the S&P 500 itself, and ${soft.length} fell less in their worst stretch.`];
    out.push(`The best return per unit of risk was ${best.k}'s (${f(best.sharpe, 2)} against the index's ${f(best.bench.sharpe, 2)}).`);
    out.push(`Selling options trades upside for income: in a year the market runs, a covered call or a put-write is left behind, and in a crash it still falls, only less. Buying protection costs every month it is not needed.
      The condor and the butterfly bet on calm and make little in the long run once the bad months are paid.`);
    return out.join(" ");
  }

  function drawHist() {
    const h = H.D.history, cols = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--warn)"];
    const ser = h.rows.map((r, i) => ({ k: r.k, name: r.k, color: cols[i % 5], pts: h.series[r.k], w: 1.5 })).filter(s => H.on.includes(s.k));
    ser.unshift({ name: "S&P 500", color: "var(--ink)", pts: h.series.SPXTR, w: 2 });
    lchart(q("#shChart"), { series: ser, yfmt: v => (h.ccy === "EUR" ? "€" : "$") + v.toFixed(1), tfmt: v => (h.ccy === "EUR" ? "€" : "$") + v.toFixed(2), height: 280, label: "growth of 1" });
  }

  function regimes() {
    const h = H.D.history;
    const G = h.regimes;
    q("#shReg").innerHTML = `<table class="tbl"><thead><tr><th class="l">Average month</th>${G.map(g => `<th>${esc(g.label)}</th>`).join("")}</tr></thead><tbody>
      ${h.rows.map(r => `<tr><td class="l">${esc(r.k)}</td>${G.map(g => { const x = r.regimes[g.k]; return `<td>${x ? sgc(x.avg, 2) : "–"}</td>`; }).join("")}</tr>`).join("")}
      <tr><td class="l dim">S&amp;P 500</td>${G.map(g => { const x = (h.rows[0] || {}).regimes ? h.rows[0].regimes[g.k] : null; return `<td class="dim">${x ? sg(x.bench, 2) + ` <small>${x.n}m</small>` : "–"}</td>`; }).join("")}</tr>
      </tbody></table><div class="disc">The VIX at the end of the month before decides the column. Option premiums are richest when it is high, and so are the losses.</div>`;
  }

  function years() {
    const h = H.D.history, ys = Object.keys(h.years.SPXTR || {}).sort().slice(-12).reverse();
    const show = h.rows.filter(r => H.on.includes(r.k));
    q("#shYears").innerHTML = `<table class="tbl"><thead><tr><th class="l">Year</th><th>S&amp;P</th>${show.map(r => `<th>${esc(r.k)}</th>`).join("")}</tr></thead><tbody>
      ${ys.map(y => `<tr><td class="l">${y}${+y === new Date().getFullYear() ? ` <span class="dim">to date</span>` : ""}</td><td>${sgc(h.years.SPXTR[y])}</td>${show.map(r => `<td>${sgc((h.years[r.k] || {})[y])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
  }

  function renderSellers() {
    const s = H.D.sellers;
    if (!s) { q("#selB").innerHTML = note(H.D.sellers_error || "no VIX history"); return; }
    const n = s.now || {};
    const shape = n.vix3m && n.vix ? (n.vix > n.vix3m ? "upside down: the next month is feared more than the next three, as in a sell-off" : "the usual way up: calm now, more uncertainty further out") : "";
    q("#selB").innerHTML = `<div class="cards">
        <div class="card"><div class="k">Priced over delivered</div><div class="v">${sg(s.gap_avg, 1, " pts")}</div><div class="t">the VIX averaged ${f(s.vix_avg, 1)}, the next month's movement ${f(s.rv_avg, 1)}, since ${mon(s.from)}</div></div>
        <div class="card"><div class="k">Months it paid to sell</div><div class="v">${s.share_over}%</div><div class="t">of ${s.n} months the VIX priced more movement than came</div></div>
        <div class="card"><div class="k">The VIX today</div><div class="v">${f(n.vix, 1)}</div><div class="t">nine days ${f(n.vix9d, 1)} · three months ${f(n.vix3m, 1)}: ${shape}</div></div>
        <div class="card"><div class="k">SKEW today</div><div class="v">${f(n.skew, 0)}</div><div class="t">what deep out-of-the-money puts cost: higher than on ${s.skew_pct}% of days since 1990</div></div></div>
      <div class="lc" id="selChart"></div>
      <div class="two even"><div><div class="sec">The months it did not pay</div>
        <table class="tbl"><thead><tr><th class="l">Month</th><th>VIX priced</th><th>Then came</th><th>Shortfall</th></tr></thead><tbody>
        ${s.worst.map(w => `<tr><td class="l">${mon(w.d)}</td><td>${f(w.vix, 1)}</td><td>${f(w.rv, 1)}</td><td class="neg">${f(w.gap, 1)} pts</td></tr>`).join("")}</tbody></table></div>
        <div class="txt"><div class="sec">Reading it</div>
          <p>An option's price carries the market's estimate of movement, and buyers pay a little more than it usually turns out to be worth, because what they buy is insurance.
            That gap, ${f(s.gap_avg, 1)} points on average, is the premium every strategy in the table above collects when it sells.</p>
          <p>It is paid in small amounts and taken back in large ones: in ${mon(s.worst[0].d)} the VIX priced ${f(s.worst[0].vix, 0)} and ${f(s.worst[0].rv, 0)} came.
            A seller needs to survive those months to keep the rest. ${fallWords()}</p>
          ${s.inverted_share != null ? `<p>The VIX has been above its three-month version on ${f(s.inverted_share, 1)}% of days since ${mon(s.inverted_since)}. In the month after such a day the S&amp;P 500
            averaged ${sg(s.after_inverted, 2)}, against ${sg(s.after_normal, 2)} otherwise: fear priced at its height has tended to be overpaid.</p>` : ""}</div></div>`;
    lchart(q("#selChart"), { series: [{ name: "VIX at the month's start", color: "var(--s2)", pts: s.series.map(x => [x[0], x[1]]), w: 1.3 },
      { name: "what the next 21 sessions did", color: "var(--s1)", pts: s.series.map(x => [x[0], x[2]]), w: 1.3 }],
      yfmt: v => v.toFixed(0), tfmt: v => v.toFixed(1), height: 260, label: "the VIX against realised volatility" });
  }

  function fallWords() {
    const R = ((H.D || {}).history || {}).rows || [], inc = R.filter(r => r.group === "income");
    if (!inc.length) return "";
    const lo = Math.max(...inc.map(r => r.worst)), hi = Math.min(...inc.map(r => r.worst));
    return `Over the period in the table the selling indexes' worst falls were ${f(-lo, 0)}–${f(-hi, 0)}%, against the index's ${f(-inc[0].bench.worst, 0)}%: smaller, not small.`;
  }

  return { builder, overview };
})();
