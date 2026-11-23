/* ==========================================================================
   PriceChart — one security's price, drawn on a canvas.

   One axis, always. Comparing against the market or the sector switches the
   whole plot to percent change from the first visible session, so every line
   shares the same scale; two y-axes would invent a relationship that is not
   in the data.

   What it carries beyond a price line, because the question on this page is
   always "why":
     · events on the day they moved the price — earnings (green beat, red
       miss), analyst up/downgrades, 8-K filings, ex-dividend days, your own
       trades, your thesis entries, and the sessions the move analysis flags
       as company-specific
     · a crosshair whose tooltip shows the day's bar, its events and the
       one-line explanation of the move
     · drag to measure a stretch; click a day to pin its full explanation

   No library. The rest of the terminal is hand-drawn SVG and the app has no
   build step; a chart dependency from a CDN would be the only thing on the
   page that stops working offline.
   ========================================================================== */

const PC_RANGES = ["1D", "5D", "1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y", "10Y"];
const PC_DAYS = { "1M": 31, "3M": 92, "6M": 183, "1Y": 366, "3Y": 1096, "5Y": 1827, "10Y": 99999 };

class PriceChart {
  constructor(host, opts = {}) {
    this.host = host;
    this.opts = opts;           // {onPick(day), dayInfo(day)->{driver,why}, onRange(r), ccy}
    host.classList.add("pc");
    host.innerHTML = `<canvas class="pc-cv" tabindex="0" aria-label="price chart; arrow keys move the cursor, Enter explains the day"></canvas>
      <div class="pc-tip" hidden></div>`;
    this.cv = host.querySelector("canvas");
    this.tip = host.querySelector(".pc-tip");
    this.ctx = this.cv.getContext("2d");
    this.st = {
      range: "1Y", type: "auto", log: false, ma50: true, ma200: true, vol: true,
      cmp: { market: false, sector: false, peer: false },
      ev: { earn: true, analyst: true, filing: true, div: true, trade: true, move: true, thesis: true },
    };
    this.daily = null; this.intra = {}; this.events = {}; this.lines = [];
    this.peer = null; this.hover = null; this.drag = null; this.V = null;
    this._wire();
    // Redraw on a width change only, and on the next frame: drawing inside
    // the observer resizes the canvas, which would notify the observer again.
    this._w = 0;
    this._ro = new ResizeObserver(() => {
      const w = this.host.clientWidth;
      if (w === this._w) return;
      this._w = w;
      cancelAnimationFrame(this._raf);
      this._raf = requestAnimationFrame(() => this.draw());
    });
    this._ro.observe(host);
  }

  /* ---------- data in ------------------------------------------------ */
  setDaily(chart) {
    // bars: [d,o,h,l,c,v,ac]; ac (dividend-adjusted) is only used for returns.
    const b = chart.bars || [];
    this.daily = {
      bars: b, bm: chart.benchmarks || {},
      ma50: this._ma(b, 50), ma200: this._ma(b, 200),
    };
    this.draw();
  }
  setIntra(rng, data) { this.intra[rng] = data; this.draw(); }
  setEvents(map) { this.events = map || {}; this.draw(); }
  setLines(lines) { this.lines = lines || []; this.draw(); }
  setPeer(sym, closes) { this.peer = sym ? { sym, closes } : null; this.draw(); }
  set(k, v) { this.st[k] = v; this.hover = null; this.draw(); }

  _ma(b, n) {
    const out = new Array(b.length).fill(null);
    let s = 0;
    for (let i = 0; i < b.length; i++) {
      s += b[i][4];
      if (i >= n) s -= b[i - n][4];
      if (i >= n - 1) out[i] = s / n;
    }
    return out;
  }

  /* ---------- what is on screen --------------------------------------- */
  view() {
    const r = this.st.range;
    if (r === "1D" || r === "5D") {
      const it = this.intra[r];
      if (!it || !it.bars || !it.bars.length) return { pts: [], intraday: true, loading: !it };
      const pts = it.bars.map(b => ({ d: b[0].slice(0, 10), t: b[0], o: b[1], h: b[2], l: b[3], c: b[4], v: b[5] }));
      let prev = null;
      if (r === "1D" && this.daily) {
        const day = pts[0].d, bars = this.daily.bars;
        const k = bars.findIndex(x => x[0] >= day);
        prev = k > 0 ? bars[k - 1][4] : null;
      }
      return { pts, intraday: true, prevClose: prev };
    }
    if (!this.daily) return { pts: [], loading: true };
    const bars = this.daily.bars;
    let cut;
    if (r === "YTD") cut = `${new Date().getFullYear()}-01-01`;
    else {
      const t = new Date(); t.setDate(t.getDate() - PC_DAYS[r]);
      cut = t.toISOString().slice(0, 10);
    }
    let i0 = bars.findIndex(b => b[0] >= cut);
    if (i0 < 0) i0 = 0;
    const bm = this.daily.bm || {};
    let pts = [];
    for (let i = i0; i < bars.length; i++) {
      const b = bars[i];
      pts.push({ d: b[0], d0: b[0], o: b[1], h: b[2], l: b[3], c: b[4], v: b[5],
        ma50: this.daily.ma50[i], ma200: this.daily.ma200[i],
        market: bm.market ? bm.market.c[i] : null, sector: bm.sector ? bm.sector.c[i] : null,
        peer: this.peer ? this.peer.closes[b[0]] ?? null : null, i });
    }
    // Weekly bars past ~3 years: a candle one pixel wide is not a candle.
    let weekly = false;
    if (pts.length > 800) {
      weekly = true;
      const wk = [];
      for (const p of pts) {
        const dt = new Date(p.d + "T12:00:00Z");
        const monday = new Date(dt); monday.setUTCDate(dt.getUTCDate() - ((dt.getUTCDay() + 6) % 7));
        const key = monday.toISOString().slice(0, 10);
        const last = wk[wk.length - 1];
        if (last && last.key === key) {
          last.h = Math.max(last.h, p.h); last.l = Math.min(last.l, p.l); last.c = p.c; last.v += p.v;
          last.d = p.d; last.ma50 = p.ma50; last.ma200 = p.ma200;
          last.market = p.market; last.sector = p.sector; last.peer = p.peer ?? last.peer;
        } else wk.push({ ...p, key, d0: p.d });
      }
      pts = wk;
    }
    return { pts, weekly };
  }

  /* ---------- drawing -------------------------------------------------- */
  _css(v) { return getComputedStyle(this.host).getPropertyValue(v).trim() || "#888"; }

  draw() {
    const cv = this.cv, ctx = this.ctx;
    const W = Math.max(this.host.clientWidth, 280);
    const H = window.innerWidth < 700 ? 250 : window.innerWidth >= 1200 ? 440 : 360;
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      cv.style.width = W + "px"; cv.style.height = H + "px";
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const C = {
      ink: this._css("--ink"), mid: this._css("--ink-mid"), soft: this._css("--ink-soft"),
      rule: this._css("--rule"), strong: this._css("--rule-strong"), gain: this._css("--gain"),
      loss: this._css("--loss"), accent: this._css("--accent"), warn: this._css("--warn"),
      s1: this._css("--s1"), s2: this._css("--s2"), s3: this._css("--s3"), ground: this._css("--ground"),
    };
    const V = this.view();
    this.V = V;
    const pts = V.pts;
    ctx.font = "10px 'IBM Plex Mono', monospace";
    if (!pts.length) {
      ctx.fillStyle = C.soft; ctx.textAlign = "center";
      ctx.fillText(V.loading ? "loading…" : "no prices for this range", W / 2, H / 2);
      return;
    }

    const pctMode = !V.intraday && (this.st.cmp.market || this.st.cmp.sector || (this.st.cmp.peer && this.peer));
    const showVol = this.st.vol;
    const L = 6, R = 62, T = 20, B = 20;
    const plotH = H - T - B;
    const laneH = V.intraday ? 0 : 14;
    const volH = showVol ? Math.round(plotH * 0.16) : 0;
    const priceH = plotH - volH - laneH - (showVol ? 4 : 0);
    const n = pts.length, pw = W - L - R, bw = pw / n;
    const X = i => L + (i + 0.5) * bw;

    // Series in the units being plotted.
    const baseOf = key => { for (const p of pts) if (p[key] != null) return p[key]; return null; };
    const series = [];
    if (pctMode) {
      const b0 = pts[0].c;
      series.push({ key: "c", color: C.ink, w: 1.8, label: this.opts.sym || "", f: p => (p.c / b0 - 1) * 100 });
      const add = (key, color, label) => {
        const b = baseOf(key);
        if (b) series.push({ key, color, w: 1.5, label, f: p => p[key] == null ? null : (p[key] / b - 1) * 100 });
      };
      if (this.st.cmp.market && this.daily.bm.market) add("market", C.s1, this.daily.bm.market.label);
      if (this.st.cmp.sector && this.daily.bm.sector) add("sector", C.s2, this.daily.bm.sector.label);
      if (this.st.cmp.peer && this.peer) add("peer", C.s3, this.peer.sym);
    }

    // y-range
    let lo = Infinity, hi = -Infinity;
    if (pctMode) {
      for (const s of series) for (const p of pts) { const v = s.f(p); if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }
    } else {
      for (const p of pts) { lo = Math.min(lo, p.l); hi = Math.max(hi, p.h); }
      if (V.prevClose) { lo = Math.min(lo, V.prevClose); hi = Math.max(hi, V.prevClose); }
      const span0 = hi - lo;
      for (const k of ["ma50", "ma200"]) if (this.st[k] && !V.intraday)
        for (const p of pts) if (p[k] != null && p[k] > lo - span0 * .35 && p[k] < hi + span0 * .35) {
          lo = Math.min(lo, p[k]); hi = Math.max(hi, p[k]);
        }
      for (const ln of this.lines) if (ln.v != null && ln.v > lo - (hi - lo) * .3 && ln.v < hi + (hi - lo) * .3) {
        lo = Math.min(lo, ln.v); hi = Math.max(hi, ln.v);
      }
    }
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    const pad = (hi - lo) * 0.06 || Math.abs(hi) * 0.02 || 1;
    lo -= pad; hi += pad;
    const useLog = this.st.log && !pctMode && lo > 0;
    const tf = v => useLog ? Math.log(v) : v;
    const tlo = tf(lo), thi = tf(hi);
    const Y = v => T + (1 - (tf(v) - tlo) / (thi - tlo)) * priceH;
    const Yinv = y => { const t = tlo + (1 - (y - T) / priceH) * (thi - tlo); return useLog ? Math.exp(t) : t; };
    this._geo = { L, R, T, B, W, H, n, bw, X, Y, Yinv, priceH, pctMode, pts, laneTop: T + priceH, laneH, volTop: T + priceH + laneH + 4, volH };

    // grid + y labels
    ctx.lineWidth = 1;
    const ticks = this._ticks(lo, hi, priceH > 180 ? 6 : 4, useLog);
    ctx.textAlign = "left"; ctx.textBaseline = "middle";
    for (const t of ticks) {
      const y = Math.round(Y(t)) + .5;
      if (y < T - 1 || y > T + priceH + 1) continue;
      ctx.strokeStyle = C.rule; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
      ctx.fillStyle = C.soft;
      ctx.fillText(pctMode ? (t > 0 ? "+" : "") + this._fmt(t, 1) + "%" : this._fmt(t), W - R + 6, y);
    }
    // x labels
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    for (const [i, lab] of this._xticks(pts, V)) {
      const x = X(i);
      ctx.fillStyle = C.soft; ctx.fillText(lab, x, H - B + 5);
      ctx.strokeStyle = C.rule; ctx.beginPath(); ctx.moveTo(Math.round(x) + .5, T); ctx.lineTo(Math.round(x) + .5, T + priceH); ctx.stroke();
    }

    // previous close (1D)
    if (V.prevClose && !pctMode) {
      const y = Math.round(Y(V.prevClose)) + .5;
      ctx.setLineDash([2, 3]); ctx.strokeStyle = C.soft; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke(); ctx.setLineDash([]);
      this._tag(ctx, W - R, y, "prev " + this._fmt(V.prevClose), C.soft, C);
    }

    // reference lines: your average cost, your target, the street's target
    if (!pctMode) for (const ln of this.lines) {
      if (ln.v == null) continue;
      const y = Y(ln.v);
      if (y < T || y > T + priceH) {
        ctx.fillStyle = ln.color || C.accent; ctx.textAlign = "right"; ctx.textBaseline = y < T ? "top" : "bottom";
        ctx.fillText(`${ln.label} ${this._fmt(ln.v)} ${y < T ? "↑" : "↓"}`, W - R - 4, y < T ? T + 2 : T + priceH - 2);
        continue;
      }
      ctx.setLineDash([5, 4]); ctx.strokeStyle = ln.color || C.accent; ctx.globalAlpha = .85;
      ctx.beginPath(); ctx.moveTo(L, Math.round(y) + .5); ctx.lineTo(W - R, Math.round(y) + .5); ctx.stroke();
      ctx.setLineDash([]); ctx.globalAlpha = 1;
      ctx.fillStyle = ln.color || C.accent; ctx.textAlign = "left"; ctx.textBaseline = "bottom";
      ctx.fillText(`${ln.label} ${this._fmt(ln.v)}`, L + 4, Math.round(y) - 2);
    }

    // moving averages (price mode)
    if (!pctMode && !V.intraday) {
      for (const [k, col, lab] of [["ma200", C.soft, "200d"], ["ma50", C.mid, "50d"]]) {
        if (!this.st[k]) continue;
        ctx.strokeStyle = col; ctx.lineWidth = 1.1; ctx.beginPath();
        let started = false, lastI = null;
        pts.forEach((p, i) => { if (p[k] == null) return; const x = X(i), y = Y(p[k]);
          if (y < T || y > T + priceH) { started = false; return; }
          started ? ctx.lineTo(x, y) : ctx.moveTo(x, y); started = true; lastI = i; });
        ctx.stroke();
        if (lastI != null) { ctx.fillStyle = col; ctx.textAlign = "left"; ctx.textBaseline = "middle";
          ctx.fillText(lab, Math.min(X(lastI) + 3, W - R - 26), Y(pts[lastI][k]) - 7); }
      }
    }

    // price
    const type = this.st.type === "auto" ? (n <= 180 && !V.intraday ? "candle" : "line") : this.st.type;
    const up0 = pts[n - 1].c >= (V.prevClose ?? pts[0].c);
    if (pctMode) {
      for (const s of series.slice().reverse()) this._line(ctx, pts, X, v => Y(v), s.f, s.color, s.w);
      // direct labels at the right end
      const ends = series.map(s => ({ s, v: s.f(pts[n - 1]) })).filter(e => e.v != null).sort((a, b) => b.v - a.v);
      let lastY = -99;
      for (const e of ends) {
        let y = Math.max(Y(e.v), lastY + 12); lastY = y;
        this._tag(ctx, W - R, y, `${(e.v >= 0 ? "+" : "") + e.v.toFixed(1)}%`, e.s.color, C, true);
      }
    } else if (type === "candle") {
      const w = Math.max(1, Math.min(bw * 0.68, 12));
      for (let i = 0; i < n; i++) {
        const p = pts[i], x = X(i), up = p.c >= p.o;
        ctx.strokeStyle = ctx.fillStyle = up ? C.gain : C.loss;
        ctx.beginPath(); ctx.moveTo(Math.round(x) + .5, Y(p.h)); ctx.lineTo(Math.round(x) + .5, Y(p.l)); ctx.stroke();
        const y1 = Y(Math.max(p.o, p.c)), y2 = Y(Math.min(p.o, p.c));
        ctx.fillRect(x - w / 2, y1, w, Math.max(1, y2 - y1));
      }
    } else {
      const col = up0 ? C.gain : C.loss;
      // a faint area under the line, the only fill on the chart
      const g = ctx.createLinearGradient(0, T, 0, T + priceH);
      g.addColorStop(0, col + "33"); g.addColorStop(1, col + "00");
      ctx.beginPath(); pts.forEach((p, i) => i ? ctx.lineTo(X(i), Y(p.c)) : ctx.moveTo(X(i), Y(p.c)));
      ctx.lineTo(X(n - 1), T + priceH); ctx.lineTo(X(0), T + priceH); ctx.closePath(); ctx.fillStyle = g; ctx.fill();
      this._line(ctx, pts, X, v => Y(v), p => p.c, col, 1.7);
    }
    if (!pctMode) this._tag(ctx, W - R, Y(pts[n - 1].c), this._fmt(pts[n - 1].c), up0 ? C.gain : C.loss, C, true);

    // volume
    if (showVol) {
      const vt = T + priceH + laneH + 4;
      const vmax = Math.max(...pts.map(p => p.v || 0)) || 1;
      const w = Math.max(1, bw * 0.7);
      for (let i = 0; i < n; i++) {
        const p = pts[i], h = (p.v || 0) / vmax * volH;
        const up = i ? p.c >= pts[i - 1].c : p.c >= p.o;
        ctx.fillStyle = (up ? C.gain : C.loss) + "66";
        ctx.fillRect(X(i) - w / 2, vt + volH - h, w, h);
      }
      ctx.fillStyle = C.soft; ctx.textAlign = "left"; ctx.textBaseline = "top";
      ctx.fillText("vol " + this._big(vmax), W - R + 6, vt);
    }

    // events
    if (!V.intraday) this._drawEvents(ctx, C);

    // measure band
    if (this.drag && this.drag.i1 != null && this.drag.i1 !== this.drag.i0) {
      const a = Math.min(this.drag.i0, this.drag.i1), b = Math.max(this.drag.i0, this.drag.i1);
      ctx.fillStyle = C.accent + "22"; ctx.fillRect(X(a) - bw / 2, T, (b - a + 1) * bw, priceH);
      const pa = pts[a], pb = pts[b];
      const ch = (pb.c / pa.c - 1) * 100;
      const days = Math.round((new Date(pb.d) - new Date(pa.d)) / 864e5);
      const txt = `${ch >= 0 ? "+" : ""}${ch.toFixed(2)}%  ·  ${b - a} ${V.weekly ? "weeks" : V.intraday ? "bars" : "sessions"}${V.intraday ? "" : "  ·  " + days + " days"}`;
      ctx.font = "600 11px 'IBM Plex Mono', monospace";
      const tw = ctx.measureText(txt).width + 12, xm = Math.min(Math.max(X((a + b) / 2) - tw / 2, L), W - R - tw);
      ctx.fillStyle = C.ground; ctx.fillRect(xm, T + 2, tw, 18);
      ctx.strokeStyle = C.accent; ctx.strokeRect(xm + .5, T + 2.5, tw - 1, 17);
      ctx.fillStyle = ch >= 0 ? C.gain : C.loss; ctx.textAlign = "left"; ctx.textBaseline = "middle";
      ctx.fillText(txt, xm + 6, T + 11);
      ctx.font = "10px 'IBM Plex Mono', monospace";
    }

    // crosshair
    if (this.hover && this.hover.i != null && this.hover.i < n) {
      const i = this.hover.i, x = Math.round(X(i)) + .5;
      ctx.strokeStyle = C.strong; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, H - B); ctx.stroke();
      if (this.hover.y != null && this.hover.y >= T && this.hover.y <= T + priceH) {
        const y = Math.round(this.hover.y) + .5;
        ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(W - R, y); ctx.stroke();
        const v = Yinv(this.hover.y);
        this._tag(ctx, W - R, y, pctMode ? (v >= 0 ? "+" : "") + v.toFixed(1) + "%" : this._fmt(v), C.strong, C, true);
      }
      const p = pts[i];
      const lab = V.intraday ? p.t.slice(11, 16) + (this.st.range === "5D" ? " " + p.d.slice(8, 10) + "/" + p.d.slice(5, 7) : "")
        : this._dmy(p.d);
      ctx.font = "10px 'IBM Plex Mono', monospace";
      const tw = ctx.measureText(lab).width + 10;
      const bx = Math.min(Math.max(x - tw / 2, L), W - R - tw);
      ctx.fillStyle = C.strong; ctx.fillRect(bx, H - B + 2, tw, 15);
      ctx.fillStyle = C.ink; ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillText(lab, bx + tw / 2, H - B + 4);
      const cy = pctMode ? Y(series[0].f(p)) : Y(p.c);
      ctx.fillStyle = C.ground; ctx.beginPath(); ctx.arc(X(i), cy, 4.5, 0, 7); ctx.fill();
      ctx.fillStyle = C.ink; ctx.beginPath(); ctx.arc(X(i), cy, 3, 0, 7); ctx.fill();
    }
  }

  _line(ctx, pts, X, Yf, f, color, w) {
    ctx.strokeStyle = color; ctx.lineWidth = w; ctx.lineJoin = "round"; ctx.beginPath();
    let started = false;
    pts.forEach((p, i) => { const v = f(p); if (v == null) { started = false; return; }
      const x = X(i), y = Yf(v); started ? ctx.lineTo(x, y) : ctx.moveTo(x, y); started = true; });
    ctx.stroke(); ctx.lineWidth = 1;
  }

  _tag(ctx, x, y, txt, bg, C, solid) {
    ctx.font = "600 10px 'IBM Plex Mono', monospace";
    const w = ctx.measureText(txt).width + 8;
    ctx.fillStyle = solid ? bg : C.ground;
    ctx.fillRect(x + 2, y - 8, w, 16);
    if (!solid) { ctx.strokeStyle = bg; ctx.strokeRect(x + 2.5, y - 7.5, w - 1, 15); }
    ctx.fillStyle = solid ? C.ground : bg; ctx.textAlign = "left"; ctx.textBaseline = "middle";
    ctx.fillText(txt, x + 6, y + .5);
    ctx.font = "10px 'IBM Plex Mono', monospace";
  }

  /* Events, mapped onto whichever bar contains their day. */
  _eventsAt(p) {
    if (!p) return [];
    const out = [];
    if (p.d0 === p.d) return (this.events[p.d] || []).filter(e => this.st.ev[e.k] !== false);
    for (const [day, list] of Object.entries(this.events)) {
      if (day >= p.d0 && day <= p.d) out.push(...list.filter(e => this.st.ev[e.k] !== false));
    }
    return out;
  }

  _drawEvents(ctx, C) {
    const g = this._geo, pts = g.pts;
    const lane = g.laneTop + g.laneH / 2;
    const days = pts.map(p => p.d), first = pts[0].d0 || pts[0].d, last = pts[pts.length - 1].d;
    const idxOf = day => {
      if (day < first || day > last) return -1;
      let lo = 0, hi = days.length - 1;
      while (lo < hi) { const m = (lo + hi) >> 1; if (days[m] < day) lo = m + 1; else hi = m; }
      return lo;
    };
    const laneSlots = {};
    for (const [day, list] of Object.entries(this.events)) {
      const i = idxOf(day);
      if (i < 0) continue;
      const x = g.X(i), p = pts[i];
      for (const e of list) {
        if (this.st.ev[e.k] === false) continue;
        const col = C[e.c] || e.c || C.accent;
        if (e.k === "trade") {
          const buy = e.dir > 0, y = buy ? g.Y(p.l) + 5 : g.Y(p.h) - 5;
          ctx.fillStyle = col; ctx.beginPath();
          if (buy) { ctx.moveTo(x, y); ctx.lineTo(x - 5, y + 8); ctx.lineTo(x + 5, y + 8); }
          else { ctx.moveTo(x, y); ctx.lineTo(x - 5, y - 8); ctx.lineTo(x + 5, y - 8); }
          ctx.closePath(); ctx.fill();
        } else if (e.k === "analyst") {
          const y = g.T - 9;
          ctx.fillStyle = col; ctx.beginPath();
          if (e.dir > 0) { ctx.moveTo(x, y - 4); ctx.lineTo(x - 4, y + 3); ctx.lineTo(x + 4, y + 3); }
          else { ctx.moveTo(x, y + 3); ctx.lineTo(x - 4, y - 4); ctx.lineTo(x + 4, y - 4); }
          ctx.closePath(); ctx.fill();
        } else if (e.k === "move") {
          if (g.pctMode) continue;
          ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.arc(x, g.Y(p.c), 5.5, 0, 7); ctx.stroke(); ctx.lineWidth = 1;
        } else {
          // lane glyphs: E earnings, D dividend, F filing, T thesis
          const slot = (laneSlots[i] = (laneSlots[i] || 0) + 1) - 1;
          const cx = x + slot * 11;
          const glyph = { earn: "E", div: "D", filing: "F", thesis: "T" }[e.k] || "•";
          ctx.fillStyle = e.k === "earn" || e.k === "thesis" ? col : C.ground;
          ctx.strokeStyle = col;
          ctx.fillRect(cx - 5, lane - 6, 10, 12); ctx.strokeRect(cx - 4.5, lane - 5.5, 9, 11);
          ctx.fillStyle = e.k === "earn" || e.k === "thesis" ? C.ground : col;
          ctx.font = "600 9px 'IBM Plex Mono', monospace"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
          ctx.fillText(glyph, cx, lane + .5);
          ctx.font = "10px 'IBM Plex Mono', monospace";
        }
      }
    }
  }

  /* ---------- axes ------------------------------------------------------ */
  _ticks(lo, hi, n, log) {
    const span = hi - lo;
    if (!(span > 0)) return [lo];
    const raw = span / n, mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 2.5, 5, 10].map(s => s * mag).find(s => span / s <= n) || 10 * mag;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(10));
    return out;
  }
  _xticks(pts, V) {
    const n = pts.length, out = [];
    const W = this.host.clientWidth || 800, maxT = Math.max(3, Math.floor(W / 90));
    if (V.intraday) {
      const step = Math.max(1, Math.ceil(n / maxT));
      for (let i = 0; i < n; i += step) {
        const p = pts[i];
        out.push([i, this.st.range === "5D" && (i === 0 || pts[i - 1].d !== p.d) ? p.d.slice(8, 10) + "/" + p.d.slice(5, 7) : p.t.slice(11, 16)]);
      }
      return out;
    }
    const spanDays = (new Date(pts[n - 1].d) - new Date(pts[0].d)) / 864e5;
    const M = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const key = spanDays > 1400 ? p => p.d.slice(0, 4) : spanDays > 200 ? p => p.d.slice(0, 7) : p => p.d.slice(0, 7) + (p.d.slice(8) < "16" ? "a" : "b");
    let lastKey = null, lastI = -99;
    const minGap = n / maxT;
    for (let i = 0; i < n; i++) {
      const k = key(pts[i]);
      if (k !== lastKey) {
        if (lastKey !== null && i - lastI >= minGap) {
          const d = pts[i].d;
          out.push([i, spanDays > 1400 ? d.slice(0, 4) : spanDays > 200 ? (d.slice(5, 7) === "01" ? d.slice(0, 4) : M[+d.slice(5, 7) - 1]) : d.slice(8, 10) + " " + M[+d.slice(5, 7) - 1]]);
          lastI = i;
        }
        lastKey = k;
      }
    }
    return out;
  }
  _fmt(v, d) {
    if (v == null || !isFinite(v)) return "–";
    const a = Math.abs(v);
    d = d ?? (a >= 1000 ? 0 : a >= 100 ? 1 : 2);
    return v.toLocaleString("nl-NL", { minimumFractionDigits: d, maximumFractionDigits: d });
  }
  _big(v) { return v >= 1e9 ? (v / 1e9).toFixed(1) + "B" : v >= 1e6 ? (v / 1e6).toFixed(1) + "M" : v >= 1e3 ? (v / 1e3).toFixed(0) + "K" : String(v); }
  _dmy(d) { return d.slice(8, 10) + "/" + d.slice(5, 7) + "/" + d.slice(0, 4); }

  /* ---------- interaction ------------------------------------------------ */
  _idx(ev) {
    const g = this._geo; if (!g) return null;
    const r = this.cv.getBoundingClientRect();
    const x = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
    const y = (ev.touches ? ev.touches[0].clientY : ev.clientY) - r.top;
    const i = Math.max(0, Math.min(g.n - 1, Math.floor((x - g.L) / g.bw)));
    return { i, x, y };
  }
  _wire() {
    const cv = this.cv;
    cv.addEventListener("mousemove", ev => {
      const h = this._idx(ev); if (!h) return;
      this.hover = h;
      if (this.drag) this.drag.i1 = h.i;
      this.draw(); this._tipShow(h);
    });
    cv.addEventListener("mouseleave", () => { this.hover = null; if (!this.drag) this.draw(); this.tip.hidden = true; });
    cv.addEventListener("mousedown", ev => { const h = this._idx(ev); if (h) this.drag = { i0: h.i, i1: h.i }; });
    window.addEventListener("mouseup", () => {
      if (!this.drag) return;
      const d = this.drag; this.drag = null;
      if (d.i1 === d.i0 || d.i1 == null) {
        const p = this.V && this.V.pts[d.i0];
        if (p && this.opts.onPick && !this.V.intraday) this.opts.onPick(p.d, p);
      }
      this.draw();
    });
    cv.addEventListener("touchstart", ev => { const h = this._idx(ev); if (h) { this.hover = h; this.draw(); this._tipShow(h); } }, { passive: true });
    cv.addEventListener("touchmove", ev => { const h = this._idx(ev); if (h) { this.hover = h; this.draw(); this._tipShow(h); } }, { passive: true });
    cv.addEventListener("touchend", () => {
      const p = this.hover && this.V && this.V.pts[this.hover.i];
      if (p && this.opts.onPick && !this.V.intraday) this.opts.onPick(p.d, p);
    });
    cv.addEventListener("keydown", ev => {
      const g = this._geo; if (!g) return;
      if (ev.key === "ArrowLeft" || ev.key === "ArrowRight") {
        ev.preventDefault();
        const i = this.hover ? this.hover.i : g.n - 1;
        const j = Math.max(0, Math.min(g.n - 1, i + (ev.key === "ArrowLeft" ? -1 : 1) * (ev.shiftKey ? 5 : 1)));
        this.hover = { i: j, x: g.X(j), y: null }; this.draw(); this._tipShow(this.hover);
      } else if (ev.key === "Enter" && this.hover) {
        const p = this.V.pts[this.hover.i]; if (p && this.opts.onPick && !this.V.intraday) this.opts.onPick(p.d, p);
      } else if (ev.key === "Escape") { this.hover = null; this.tip.hidden = true; this.draw(); }
    });
  }

  _tipShow(h) {
    const V = this.V, g = this._geo; if (!V || !g) return;
    const p = V.pts[h.i]; if (!p) { this.tip.hidden = true; return; }
    const prev = h.i ? V.pts[h.i - 1].c : (V.prevClose ?? p.o);
    const ch = prev ? (p.c / prev - 1) * 100 : null;
    const f = v => this._fmt(v);
    const e = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    let html = `<div class="pc-th"><b>${V.intraday ? p.t.slice(11, 16) + " · " + this._dmy(p.d) : (V.weekly ? "week to " : "") + this._dmy(p.d)}</b>
      <span class="${ch == null ? "" : ch >= 0 ? "pos" : "neg"}">${ch == null ? "" : (ch >= 0 ? "+" : "") + ch.toFixed(2) + "%"}</span></div>
      <div class="pc-ohlc"><span>O ${f(p.o)}</span><span>H ${f(p.h)}</span><span>L ${f(p.l)}</span><span>C <b>${f(p.c)}</b></span></div>
      <div class="pc-v">vol ${this._big(p.v || 0)}</div>`;
    if (g.pctMode) {
      const rows = [];
      const b0 = V.pts[0];
      const add = (k, lab, cls) => { const b = V.pts.find(x => x[k] != null); if (b && p[k] != null) rows.push(`<span><i class="sw ${cls}"></i>${e(lab)} ${((p[k] / b[k] - 1) * 100).toFixed(1)}%</span>`); };
      rows.push(`<span><i class="sw sw0"></i>${e(this.opts.sym || "")} ${((p.c / b0.c - 1) * 100).toFixed(1)}%</span>`);
      if (this.st.cmp.market && this.daily.bm.market) add("market", this.daily.bm.market.label, "sw1");
      if (this.st.cmp.sector && this.daily.bm.sector) add("sector", this.daily.bm.sector.label, "sw2");
      if (this.st.cmp.peer && this.peer) add("peer", this.peer.sym, "sw3");
      html += `<div class="pc-cmp">${rows.join("")}</div>`;
    }
    if (!V.intraday) {
      const evs = this._eventsAt(p);
      if (evs.length) html += `<div class="pc-ev">${evs.slice(0, 5).map(x => `<div><b class="evk ${x.k}">${e(x.tag || x.k)}</b> ${e(x.text)}</div>`).join("")}</div>`;
      const info = !V.weekly && this.opts.dayInfo ? this.opts.dayInfo(p.d) : null;
      if (info && info.why) html += `<div class="pc-why"><b class="drv ${e(info.driver)}">${e(info.driver)}</b> ${e(info.why)}</div>`;
      if (!V.weekly) html += `<div class="pc-hint">click for the full story · drag to measure</div>`;
    }
    this.tip.innerHTML = html;
    this.tip.hidden = false;
    const tw = this.tip.offsetWidth, W = this.host.clientWidth;
    const x = g.X(h.i);
    this.tip.style.left = (x + 16 + tw > W ? Math.max(4, x - tw - 16) : x + 16) + "px";
    this.tip.style.top = "26px";
  }
}
