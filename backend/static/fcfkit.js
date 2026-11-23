/* ==========================================================================
   The FCF model's card, shared by the stock page (/t/SYM, "FCF model") and
   the FCF page (/m/FCF, a row opened). Qualtrim's model: free cash flow per
   share, its growth, the multiple paid for it at the end, the return wanted;
   out come the yearly return today's price offers and the price that would
   give the return wanted. Every input starts from the company's own figures
   (pf/fcfmodel.py says how) and is yours to change; a blank box is the
   company's figure. Changes are saved per stock, a moment after typing.
   ========================================================================== */

const FCFK = (() => {
  const G = [-5, 40], M = [8, 35];
  const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
  const spread = g => Math.max(4, 0.3 * Math.abs(g));

  /* The same arithmetic as fcfmodel.project, so a typed number answers at once. */
  function project(fps, g, m, r, n, price) {
    if (fps == null || g == null || m == null || !price) return null;
    if (fps <= 0) return { ok: false, why: "free cash flow is negative or nil: nothing to multiply" };
    const fcfN = fps * Math.pow(1 + g / 100, n), value = fcfN * m;
    const implied = value > 0 ? (Math.pow(value / price, 1 / n) - 1) * 100 : null;
    const entry = r != null ? value / Math.pow(1 + r / 100, n) : null;
    return { ok: true, value, fcfN, implied, entry, vs: entry ? (entry / price - 1) * 100 : null,
      path: Array.from({ length: n + 1 }, (_, t) => price * Math.pow(1 + (implied || 0) / 100, t)),
      fcfPath: Array.from({ length: n + 1 }, (_, t) => fps * Math.pow(1 + g / 100, t)) };
  }
  function cases(g, m, mu) {
    const s = spread(g);
    const lo = mu.p25 && mu.median ? Math.max(0.7, mu.p25 / mu.median) : 0.8;
    const hi = mu.p75 && mu.median ? Math.min(1.4, mu.p75 / mu.median) : 1.2;
    return { bear: { growth: clamp(g - s, G[0] - 5, G[1] + 10), multiple: clamp(m * lo, M[0] * 0.75, M[1]) },
             base: { growth: g, multiple: m },
             bull: { growth: clamp(g + s, G[0], G[1] + 10), multiple: clamp(m * hi, M[0], M[1] * 1.25) } };
  }

  const CC = { USD: "$", EUR: "€", GBP: "£", JPY: "¥", CAD: "C$", AUD: "A$", HKD: "HK$", CHF: "CHF ", DKK: "kr ", SEK: "kr " };
  const money = (v, ccy, d = 2) => v == null || !isFinite(v) ? "–" : (CC[ccy] ?? (ccy ? ccy + " " : "")) + nf(v, d);
  const short = (v, ccy) => v == null ? "–" : Math.abs(v) >= 1000 ? (CC[ccy] ?? "") + nf(v / 1000, 2) + "k" : money(v, ccy, 0);
  const pct = (v, d = 2) => v == null || !isFinite(v) ? "–" : nf(v, d) + "%";
  const spct = (v, d = 1) => v == null || !isFinite(v) ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + nf(Math.abs(v), d) + "%";
  const quarter = (d, add) => { const y = d.getFullYear() + add; return `Q${Math.floor(d.getMonth() / 3) + 1} ${y}`; };
  const BASE_LAB = { reported: "reported", after_sbc: "after stock-based pay", normalised: "normalised" };

  /* The card. `M` is /api/pf/fcf/{sym}; `opts.onSaved(model)` after a save. */
  function card(host, M0, opts = {}) {
    let Mx = M0, timer = null;
    if (!Mx || !Mx.available) {
      host.innerHTML = `<div class="note plain"><b>No FCF model</b>${esc((Mx && (Mx.why || Mx.error)) || "not available")}</div>`;
      return;
    }
    const st = () => {
      const sv = Mx.saved || {};
      return { base: sv.base || Mx.base_kind, horizon: +(sv.horizon || Mx.horizon || 5),
        growth: sv.growth, multiple: sv.multiple, ret: sv.ret };
    };
    let S0 = st();
    /* Drawn in the space it has: a phone gets a chart its own size, not a
       desktop one shrunk until its lettering cannot be read. The card can be
       drawn before the page has laid out, so the window's width bounds it. */
    const width = () => Math.max(440, Math.min(1200, host.clientWidth || 1200, (window.innerWidth || 1200) - 24));
    let lastW = null;
    window.addEventListener("resize", () => { clearTimeout(host._fkRz); host._fkRz = setTimeout(() => {
      if (document.body.contains(host) && width() !== lastW && !host.contains(document.activeElement)) draw(); }, 250); });
    if (!(Mx.saved || {}).base) S0.base = "reported";

    const gDef = n => ((Mx.growth.by_horizon || {})[n] ?? Mx.growth.base);
    function used() {
      const g = S0.growth ?? gDef(S0.horizon), m = S0.multiple ?? Mx.multiple.base, r = S0.ret ?? Mx.ret.base;
      return { g, m, r, fps: Mx.fps[S0.base], n: S0.horizon };
    }
    function draw() {
      const U = used(), ccy = Mx.currency, P = Mx.price;
      lastW = width();
      const cs = cases(U.g, U.m, Mx.multiple);
      const res = Object.fromEntries(Object.entries(cs).map(([k, c]) => [k, project(U.fps, c.growth, c.multiple, U.r, U.n, P)]));
      const b = res.base;
      const good = b && b.ok && b.implied != null ? (b.implied >= U.r ? "pos" : b.implied >= U.r - 3 ? "warnc" : "neg") : "";
      const box = (k, lab, val, unit, dflt, title) => `<label class="fk-in" title="${esc(title)}"><span class="k">${lab}</span>
        <span class="fk-v"><input data-k="${k}" type="number" step="${k === "multiple" ? 0.5 : 0.1}" value="${val ?? ""}" placeholder="${nf(dflt, 1)}">${unit}</span>
        <span class="fk-d">${val == null ? "the company's" : `<button class="linkish" data-reset="${k}">back to ${nf(dflt, 1)}${unit}</button>`}</span></label>`;
      const now = new Date();
      host.innerHTML = `<div class="fk">
        <div class="fk-ins">
          <div class="fk-in fk-fps" title="free cash flow per share over ${esc(Mx.window)}, in ${esc(ccy)} per quoted share">
            <span class="k">Trailing FCF/share</span><span class="fk-v"><b>${money(U.fps, ccy)}</b></span>
            <span class="fk-d"><select data-k="base" class="seg">${Object.keys(BASE_LAB).filter(k => Mx.fps[k] != null).map(k => `<option value="${k}"${k === S0.base ? " selected" : ""}>${BASE_LAB[k]}</option>`).join("")}</select></span></div>
          ${box("growth", "FCF growth rate", S0.growth, "%", gDef(U.n), "a year on average over the years chosen; " + Mx.growth.why)}
          ${box("multiple", "FCF multiple", S0.multiple, "×", Mx.multiple.base, "price over free cash flow at the end; " + Mx.multiple.why)}
          ${box("ret", "Desired return", S0.ret, "%", Mx.ret.base, "a year; " + Mx.ret.why)}
          <label class="fk-in"><span class="k">Years</span><span class="fk-v"><select data-k="horizon" class="seg">${[3, 5, 7, 10].map(n => `<option${n === U.n ? " selected" : ""}>${n}</option>`).join("")}</select></span>
            <span class="fk-d" id="fkSave">${Mx.edited ? "your inputs, saved" : ""}</span></label>
        </div>
        ${b && !b.ok ? `<div class="note plain"><b>No answer at this base</b>${esc(b.why)}${Mx.fps.normalised > 0 && S0.base !== "normalised" ? " — the normalised base is positive." : ""}</div>` : ""}
        <div class="fk-out">
          <div><span class="k">Return from today's price</span><b class="${good}">${b && b.ok ? pct(b.implied) : "–"}</b>
            <span class="dim">a year for ${U.n} years, if FCF grows ${pct(U.g, 1)} and ends at ${nf(U.m, 1)}×</span></div>
          <div><span class="k">Entry price for ${pct(U.r, 1)} return</span><b>${b && b.ok ? money(b.entry, ccy) : "–"}</b>
            <span class="dim">${b && b.ok ? `<span class="${sgn(b.vs)}">${spct(b.vs, 0)}</span> against ${money(P, ccy)} today` : ""}</span></div>
          <div><span class="k">Bear · bull</span><b><span class="${res.bear && res.bear.implied >= U.r ? "pos" : "neg"}">${res.bear && res.bear.ok ? pct(res.bear.implied, 1) : "–"}</span> · <span class="${res.bull && res.bull.implied >= U.r ? "pos" : "neg"}">${res.bull && res.bull.ok ? pct(res.bull.implied, 1) : "–"}</span></b>
            <span class="dim">growth ${pct(cs.bear.growth, 0)}/${pct(cs.bull.growth, 0)}, ${nf(cs.bear.multiple, 0)}×/${nf(cs.bull.multiple, 0)}×</span></div>
        </div>
        ${b && b.ok ? chart(res, U, ccy, now) : ""}
        <div class="fk-why">
          <div><b>Growth ${pct(gDef(U.n), 1)}</b> ${esc(Mx.growth.why)}: ${pct(gDef(U.n), 1)} a year on average over ${U.n} years.</div>
          <div><b>Multiple ${nf(Mx.multiple.base, 1)}×</b> ${esc(Mx.multiple.why)}.</div>
          <div><b>Return ${pct(Mx.ret.base, 1)}</b> ${esc(Mx.ret.why)}.</div>
          ${(Mx.notes || []).map(n => `<div class="fk-note">${esc(n)}</div>`).join("")}
          <div class="dim">Free cash flow: operating cash flow less capital spending, ${esc(Mx.window)}${Mx.reported_currency && Mx.reported_currency !== ccy ? `, from ${esc(Mx.reported_currency)} at today's rate` : ""}${Mx.ratio && Mx.ratio !== 1 ? `, per quoted share (${Mx.ratio} of the company's)` : ""}. Dividends are not added: the multiple is what the cash flow is worth.</div>
        </div>
        ${hist(U, ccy, b)}
      </div>`;
      wire();
    }
    function chart(res, U, ccy, now) {
      const W = width(), H = W < 700 ? 260 : 300, P = { l: 58, r: 40, t: 26, b: 28 };
      const b = res.base, ser = [["bear", "var(--loss)"], ["bull", "var(--accent)"], ["base", "var(--gain)"]].filter(([k]) => res[k] && res[k].ok);
      const vals = ser.flatMap(([k]) => res[k].path);
      const hi = Math.max(...vals) * 1.08, lo = 0;
      const X = i => P.l + i / U.n * (W - P.l - P.r), Y = v => P.t + (1 - (v - lo) / (hi - lo)) * (H - P.t - P.b);
      const ticks = [0, hi / 4, hi / 2, hi * 3 / 4];
      return `<figure class="fk-ch"><figcaption><span class="lgd"><i class="swatch" style="background:var(--gain)"></i>projected price</span>
          <span class="lgd"><i class="swatch" style="background:var(--loss)"></i>bear</span><span class="lgd"><i class="swatch" style="background:var(--accent)"></i>bull</span>
          <span class="dim">today's price compounding at the return each case offers</span></figcaption>
        <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="projected price">
        ${ticks.map(v => `<line x1="${P.l}" x2="${W - P.r}" y1="${Y(v)}" y2="${Y(v)}" stroke="var(--rule)"/><text x="${P.l - 6}" y="${Y(v) + 3}" text-anchor="end" class="ax">${short(v, ccy)}</text>`).join("")}
        ${Array.from({ length: U.n + 1 }, (_, i) => `<line x1="${X(i)}" x2="${X(i)}" y1="${P.t}" y2="${H - P.b}" stroke="var(--rule)"/>${W < 700 && U.n > 5 && i % 2 ? "" : `<text x="${X(i)}" y="${H - 7}" text-anchor="middle" class="ax">${quarter(now, i)}</text>`}`).join("")}
        ${ser.map(([k, c]) => `<polyline points="${res[k].path.map((v, i) => `${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ")}" fill="none" stroke="${c}" stroke-width="${k === "base" ? 2.4 : 1.4}" ${k === "base" ? "" : `stroke-dasharray="5 4" opacity=".8"`}/>`).join("")}
        ${b.path.map((v, i) => `<circle cx="${X(i)}" cy="${Y(v)}" r="4.5" fill="var(--gain)" stroke="var(--surface)" stroke-width="2"><title>${quarter(now, i)}: ${money(v, ccy)}</title></circle>
          <text x="${X(i)}" y="${Y(v) - 10}" text-anchor="middle" class="fk-lab">${short(v, ccy)}</text>`).join("")}
        ${["bear", "bull"].filter(k => res[k] && res[k].ok).map(k => { const v = res[k].path[U.n]; return `<text x="${X(U.n) - 4}" y="${Y(v) + (k === "bear" ? 14 : -8)}" text-anchor="end" class="ax">${k} ${short(v, ccy)}</text>`; }).join("")}
        </svg></figure>`;
    }
    function hist(U, ccy, b) {
      const h = (Mx.history || []).filter(x => x.fps != null);
      if (h.length < 2) return "";
      const key = S0.base === "after_sbc" ? "fps_sbc" : "fps";
      const proj = b && b.ok ? b.fcfPath.slice(1) : [];
      const all = h.map(x => x[key] ?? x.fps).concat(proj);
      const W = width(), H = 150, P = { l: 58, r: 8, t: 14, b: 20 }, n = all.length, bw = (W - P.l - P.r) / n;
      const mx = Math.max(0, ...all), mn = Math.min(0, ...all), Y = v => P.t + (1 - (v - mn) / ((mx - mn) || 1)) * (H - P.t - P.b);
      const now = new Date();
      return `<figure class="fk-ch"><figcaption>FCF per share, ${S0.base === "after_sbc" ? "after stock-based pay, " : ""}each year, then the growth assumed <span class="dim">(${esc(ccy)}, at today's exchange rate)</span></figcaption>
        <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="free cash flow per share">
        <line x1="${P.l}" x2="${W - P.r}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--rule-strong)"/>
        <text x="${P.l - 6}" y="${Y(mx) + 3}" text-anchor="end" class="ax">${money(mx, ccy, mx < 10 ? 2 : 0)}</text>
        ${all.map((v, i) => { const isP = i >= h.length, y = Y(Math.max(v, 0)), hh = Math.abs(Y(v) - Y(0));
          const lab = isP ? "’" + quarter(now, i - h.length + 1).slice(5) + "E" : h[i].label;
          return `<rect x="${P.l + i * bw + 3}" y="${y}" width="${Math.max(1, bw - 6)}" height="${Math.max(1, hh)}" rx="1.5" fill="${v < 0 ? "var(--loss)" : isP ? "var(--accent-dim)" : "var(--accent)"}"><title>${esc(lab)}: ${money(v, ccy)}</title></rect>
            <text x="${P.l + i * bw + bw / 2}" y="${H - 5}" text-anchor="middle" class="ax">${esc(lab.replace("FY", "’"))}</text>`; }).join("")}
        </svg></figure>`;
    }
    function wire() {
      host.querySelectorAll("input[data-k]").forEach(el => el.oninput = () => {
        const v = el.value.trim() === "" ? null : +el.value;
        S0[el.dataset.k] = v != null && isFinite(v) ? v : null;
        redraw(el.dataset.k); queue();
      });
      host.querySelectorAll("select[data-k]").forEach(el => el.onchange = () => {
        S0[el.dataset.k] = el.dataset.k === "horizon" ? +el.value : el.value; draw(); queue();
      });
      host.querySelectorAll("[data-reset]").forEach(el => el.onclick = e => { e.preventDefault(); S0[el.dataset.reset] = null; draw(); queue(); });
    }
    /* Redraw, then put the cursor back where it was typing. */
    function redraw(k) {
      const el = host.querySelector(`input[data-k="${k}"]`), pos = el ? el.selectionStart : null, val = el ? el.value : null;
      draw();
      const n = host.querySelector(`input[data-k="${k}"]`);
      if (n) { n.focus(); n.value = val; try { if (pos != null) n.setSelectionRange(pos, pos); } catch { } }
    }
    function queue() {
      clearTimeout(timer);
      const s = host.querySelector("#fkSave"); if (s) s.textContent = "saving…";
      timer = setTimeout(async () => {
        try {
          const r = await fetch(`/api/pf/fcf/${encodeURIComponent(Mx.symbol)}`, { method: "POST", headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ growth: S0.growth, multiple: S0.multiple, ret: S0.ret, base: S0.base === Mx.base_kind && !(Mx.saved || {}).base ? null : S0.base,
              horizon: S0.horizon === 5 && !(Mx.saved || {}).horizon ? null : S0.horizon }) });
          const j = await r.json();
          if (!r.ok) throw new Error(j.detail || `HTTP ${r.status}`);
          Mx = j; const s2 = host.querySelector("#fkSave"); if (s2) s2.textContent = Mx.edited ? "your inputs, saved" : "the company's figures";
          opts.onSaved && opts.onSaved(Mx);
        } catch (e) { const s2 = host.querySelector("#fkSave"); if (s2) s2.textContent = "not saved: " + e.message; }
      }, 700);
    }
    draw();
  }
  return { card, project, cases, money, pct };
})();
