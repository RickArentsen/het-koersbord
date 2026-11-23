/* ==========================================================================
   Surface3D — a surface you can turn, drawn on a plain 2D canvas.

   data = { x: [column values], rows: [{ pos, label, z: [value | null] }],
            zTitle, xFmt(v), zFmt(v), xTicks: [column index], sel: row index | null }

   The view is orthographic: nothing shrinks with distance, so heights can be
   compared across the whole sheet, which a perspective view would not allow.
   Cells are painted farthest first; a cell with a missing corner is left out,
   so a surface that is ragged in the data is ragged on screen.

   Drag to turn, double-click to reset. Hover reads the nearest point and
   lights the row and the column through it. No library: the rest of the
   terminal is hand-drawn too, and a 3D chart from a CDN would be the one
   thing on the page that stops working offline.
   ========================================================================== */

class Surface3D {
  constructor(host, opts = {}) {
    this.host = host; this.opts = opts;
    host.classList.add("s3");
    host.innerHTML = `<canvas tabindex="0" aria-label="${opts.label || "surface"}; arrow keys turn it"></canvas><div class="s3-tip" hidden></div>`;
    this.cv = host.querySelector("canvas"); this.tip = host.querySelector(".s3-tip");
    this.ctx = this.cv.getContext("2d");
    this.home = { yaw: opts.yaw ?? -0.62, pitch: opts.pitch ?? 0.52 };
    this.yaw = this.home.yaw; this.pitch = this.home.pitch;
    this.data = null; this.hover = null; this.quads = [];
    this._wire();
    this._w = 0;
    this._ro = new ResizeObserver(() => {
      const w = host.clientWidth;
      if (w === this._w) return;
      this._w = w; cancelAnimationFrame(this._raf);
      this._raf = requestAnimationFrame(() => this.draw());
    });
    this._ro.observe(host);
  }

  set(data) { this.data = data; this.hover = null; this.tip.hidden = true; this.draw(); }
  reset() { this.yaw = this.home.yaw; this.pitch = this.home.pitch; this.draw(); }
  view(yaw, pitch) { this.yaw = yaw; this.pitch = pitch; this.draw(); }

  _css(v) { return getComputedStyle(this.host).getPropertyValue(v).trim() || "#888"; }

  /* Low to high: deep blue, the terminal's blue, mint, amber. */
  static ramp(t) {
    const S = [[0, [22, 50, 74]], [0.34, [42, 111, 151]], [0.6, [79, 168, 206]], [0.8, [143, 205, 176]], [1, [232, 196, 104]]];
    t = Math.max(0, Math.min(1, t));
    for (let i = 1; i < S.length; i++) if (t <= S[i][0]) {
      const a = S[i - 1], b = S[i], f = (t - a[0]) / (b[0] - a[0]);
      return a[1].map((v, k) => Math.round(v + (b[1][k] - v) * f));
    }
    return S[S.length - 1][1];
  }

  static nice(lo, hi, n = 5) {
    if (!(hi > lo)) { hi = lo + 1; }
    const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
    const step = (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * mag;
    const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step, ticks = [];
    for (let v = a; v <= b + step / 2; v += step) ticks.push(+v.toFixed(10));
    return { lo: a, hi: b, ticks };
  }

  draw() {
    const D = this.data, cv = this.cv, ctx = this.ctx;
    const W = Math.max(280, this.host.clientWidth), H = Math.round(Math.max(300, Math.min(560, W * 0.6)));
    const dpr = window.devicePixelRatio || 1;
    if (cv.width !== Math.round(W * dpr) || cv.height !== Math.round(H * dpr)) {
      cv.width = Math.round(W * dpr); cv.height = Math.round(H * dpr);
      cv.style.width = W + "px"; cv.style.height = H + "px";
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    const C = { ink: this._css("--ink"), mid: this._css("--ink-mid"), soft: this._css("--ink-soft"),
      rule: this._css("--rule"), strong: this._css("--rule-strong"), ground: this._css("--ground"), surface: this._css("--surface") };
    ctx.font = "10px 'IBM Plex Mono', monospace";
    this.quads = [];
    if (!D || !D.rows || !D.rows.length || !D.x.length) {
      ctx.fillStyle = C.soft; ctx.textAlign = "center"; ctx.fillText("no surface to draw", W / 2, H / 2);
      return;
    }

    // ---- the data in a unit box: x and y in [-1, 1], z in [0, ZH] ------------
    const xs = D.x, nx = xs.length, ny = D.rows.length, ZH = 0.95;
    let zlo = Infinity, zhi = -Infinity;
    for (const r of D.rows) for (const v of r.z) if (v != null) { zlo = Math.min(zlo, v); zhi = Math.max(zhi, v); }
    const zt = Surface3D.nice(zlo, zhi, 5); zlo = zt.lo; zhi = zt.hi;
    // Colour runs between the 3rd and 97th percentile: a few extreme corners
    // would otherwise take the whole ramp and leave the rest one shade.
    const all = [];
    for (const r of D.rows) for (const v of r.z) if (v != null) all.push(v);
    all.sort((a, b) => a - b);
    const c0 = all[Math.floor(all.length * 0.03)], c1 = all[Math.min(all.length - 1, Math.ceil(all.length * 0.97))];
    const tone = v => c1 > c0 ? (v - c0) / (c1 - c0) : 0.5;
    const x0 = xs[0], x1 = xs[nx - 1], y0 = D.rows[0].pos, y1 = D.rows[ny - 1].pos;
    const ux = j => x1 === x0 ? 0 : (xs[j] - x0) / (x1 - x0) * 2 - 1;
    const uy = i => y1 === y0 ? 0 : (D.rows[i].pos - y0) / (y1 - y0) * 2 - 1;
    const uz = v => (v - zlo) / (zhi - zlo) * ZH;

    // ---- projection --------------------------------------------------------------
    const cy = Math.cos(this.yaw), sy = Math.sin(this.yaw), cp = Math.cos(this.pitch), sp = Math.sin(this.pitch);
    const proj = (x, y, z) => {
      const xr = x * cy - y * sy, yr = x * sy + y * cy;
      return [xr, yr * sp + z * cp, yr * cp - z * sp];          // [right, up, away]
    };
    // Fit the whole box, whichever way it is turned.
    let a0 = Infinity, a1 = -Infinity, b0 = Infinity, b1 = -Infinity;
    for (const x of [-1, 1]) for (const y of [-1, 1]) for (const z of [0, ZH]) {
      const p = proj(x, y, z); a0 = Math.min(a0, p[0]); a1 = Math.max(a1, p[0]); b0 = Math.min(b0, p[1]); b1 = Math.max(b1, p[1]);
    }
    const M = { l: 46, r: 46, t: 14, b: 40 };
    const sc = Math.min((W - M.l - M.r) / (a1 - a0), (H - M.t - M.b) / (b1 - b0));
    const ox = M.l + ((W - M.l - M.r) - (a1 - a0) * sc) / 2 - a0 * sc;
    const oy = M.t + ((H - M.t - M.b) - (b1 - b0) * sc) / 2 + b1 * sc;
    const P = (x, y, z) => { const p = proj(x, y, z); return [ox + p[0] * sc, oy - p[1] * sc, p[2]]; };
    this._P = P;

    // ---- the box: floor, and the two walls behind the surface ---------------------
    const depthOf = (x, y) => proj(x, y, 0)[2];
    const farX = depthOf(1, 0) > depthOf(-1, 0) ? 1 : -1, farY = depthOf(0, 1) > depthOf(0, -1) ? 1 : -1;
    const line = (a, b, col, w = 1, dash) => {
      ctx.strokeStyle = col; ctx.lineWidth = w; ctx.setLineDash(dash || []);
      ctx.beginPath(); ctx.moveTo(a[0], a[1]); ctx.lineTo(b[0], b[1]); ctx.stroke(); ctx.setLineDash([]);
    };
    const xticks = D.xTicks || this._pick(xs, 6), yticks = D.yTicks || this._pickRows(D.rows, 7);
    for (const j of xticks) line(P(ux(j), -1, 0), P(ux(j), 1, 0), C.rule);
    for (const i of yticks) line(P(-1, uy(i), 0), P(1, uy(i), 0), C.rule);
    for (const t of zt.ticks) {
      const z = uz(t);
      line(P(-1, farY, z), P(1, farY, z), C.rule);
      line(P(farX, -1, z), P(farX, 1, z), C.rule);
    }
    for (const [a, b] of [[[-1, -1], [1, -1]], [[1, -1], [1, 1]], [[1, 1], [-1, 1]], [[-1, 1], [-1, -1]]])
      line(P(a[0], a[1], 0), P(b[0], b[1], 0), C.strong);
    for (const [x, y] of [[farX, farY], [-farX, farY], [farX, -farY]]) line(P(x, y, 0), P(x, y, ZH), C.strong);
    line(P(-1, farY, ZH), P(1, farY, ZH), C.strong); line(P(farX, -1, ZH), P(farX, 1, ZH), C.strong);

    // ---- the cells -------------------------------------------------------------------
    const V = D.rows.map((r, i) => r.z.map((v, j) => v == null ? null : P(ux(j), uy(i), uz(v))));
    const quads = [];
    for (let i = 0; i < ny - 1; i++) for (let j = 0; j < nx - 1; j++) {
      const p = [V[i][j], V[i][j + 1], V[i + 1][j + 1], V[i + 1][j]];
      if (p.some(q => !q)) continue;
      const zs = [D.rows[i].z[j], D.rows[i].z[j + 1], D.rows[i + 1].z[j + 1], D.rows[i + 1].z[j]];
      const zm = (zs[0] + zs[1] + zs[2] + zs[3]) / 4;
      // A little shading from the slope, so the shape reads without the colour.
      const dzx = ((zs[1] + zs[2]) - (zs[0] + zs[3])) / 2 / (zhi - zlo), dzy = ((zs[2] + zs[3]) - (zs[0] + zs[1])) / 2 / (zhi - zlo);
      const shade = Math.max(0.72, Math.min(1.12, 0.94 - dzx * 1.1 * cy + dzy * 1.1 * sy));
      quads.push({ p, i, j, d: (p[0][2] + p[1][2] + p[2][2] + p[3][2]) / 4, t: tone(zm), shade });
    }
    quads.sort((a, b) => b.d - a.d);
    ctx.lineJoin = "round";
    for (const q of quads) {
      const c = Surface3D.ramp(q.t).map(v => Math.round(Math.min(255, v * q.shade)));
      ctx.fillStyle = `rgb(${c[0]},${c[1]},${c[2]})`;
      ctx.strokeStyle = "rgba(5,8,10,.55)"; ctx.lineWidth = 0.6;
      ctx.beginPath(); ctx.moveTo(q.p[0][0], q.p[0][1]);
      for (let k = 1; k < 4; k++) ctx.lineTo(q.p[k][0], q.p[k][1]);
      ctx.closePath(); ctx.fill(); ctx.stroke();
    }
    this.quads = quads; this._V = V;

    // ---- a row and a column lit: the one selected, and the one under the cursor ---
    const trace = (pts, col, w) => {
      ctx.strokeStyle = col; ctx.lineWidth = w; ctx.beginPath();
      let pen = false;
      for (const p of pts) { if (!p) { pen = false; continue; } pen ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1]); pen = true; }
      ctx.stroke();
    };
    if (D.sel != null && V[D.sel]) trace(V[D.sel], C.ink, 2);
    if (this.hover) {
      const { i, j } = this.hover;
      trace(V[i], "#fff", 1.6); trace(V.map(r => r[j]), "#fff", 1.2);
      const p = V[i][j];
      if (p) { ctx.fillStyle = C.ground; ctx.beginPath(); ctx.arc(p[0], p[1], 5, 0, 7); ctx.fill();
        ctx.fillStyle = "#fff"; ctx.beginPath(); ctx.arc(p[0], p[1], 3.2, 0, 7); ctx.fill(); }
    }

    // ---- labels on the edges nearest the eye ------------------------------------------
    ctx.fillStyle = C.soft; ctx.textBaseline = "middle";
    const out = (a, b, k = 12) => { const dx = a[0] - b[0], dy = a[1] - b[1], n = Math.hypot(dx, dy) || 1; return [a[0] + dx / n * k, a[1] + dy / n * k]; };
    const xf = D.xFmt || (v => String(v)), zf = D.zFmt || (v => String(v));
    for (const j of xticks) {
      const p = out(P(ux(j), -farY, 0), P(ux(j), farY, 0), 13);
      ctx.textAlign = "center"; ctx.fillText(xf(xs[j]), p[0], p[1]);
    }
    for (const i of yticks) {
      const p = out(P(-farX, uy(i), 0), P(farX, uy(i), 0), 8);
      ctx.textAlign = p[0] < P(farX, uy(i), 0)[0] ? "right" : "left"; ctx.fillText(D.rows[i].label, p[0], p[1]);
    }
    const zx = farX, zy = -farY;                       // the upright on the side
    for (const t of zt.ticks) {
      const p = out(P(zx, zy, uz(t)), P(-zx, zy, uz(t)), 8);
      ctx.textAlign = p[0] < P(-zx, zy, uz(t))[0] ? "right" : "left"; ctx.fillText(zf(t), p[0], p[1]);
    }
    line(P(zx, zy, 0), P(zx, zy, ZH), C.strong);
    // Only the upright gets a title on the canvas. The other two axes say
    // what they are by their own labels, and a title that has to sit on a
    // slanted edge ends up on top of them; the page names them underneath.
    ctx.fillStyle = C.mid; ctx.textAlign = "left"; if (D.zTitle) ctx.fillText(D.zTitle, 8, 10);
    this._meta = { xs, zlo, zhi };
  }

  _pick(xs, n) { const step = Math.max(1, Math.round((xs.length - 1) / n)), out = []; for (let j = 0; j < xs.length; j += step) out.push(j); if (out[out.length - 1] !== xs.length - 1) out.push(xs.length - 1); return out; }
  _pickRows(rows, n) {
    // Rows are unevenly spaced (expiries bunch up near today): label those far enough apart.
    const lo = rows[0].pos, hi = rows[rows.length - 1].pos, gap = (hi - lo) / n, out = [];
    let last = -Infinity;
    rows.forEach((r, i) => { if (r.pos - last >= gap || i === rows.length - 1 && r.pos - last >= gap * 0.6) { out.push(i); last = r.pos; } });
    return out;
  }

  /* ---------- interaction ------------------------------------------------------ */
  _at(ev) {
    const r = this.cv.getBoundingClientRect(), x = ev.clientX - r.left, y = ev.clientY - r.top;
    for (let k = this.quads.length - 1; k >= 0; k--) {
      const q = this.quads[k].p;
      let inside = false;
      for (let a = 0, b = 3; a < 4; b = a++) {
        if ((q[a][1] > y) !== (q[b][1] > y) && x < (q[b][0] - q[a][0]) * (y - q[a][1]) / (q[b][1] - q[a][1]) + q[a][0]) inside = !inside;
      }
      if (!inside) continue;
      // the corner of that cell nearest the cursor
      const Q = this.quads[k], cs = [[Q.i, Q.j], [Q.i, Q.j + 1], [Q.i + 1, Q.j + 1], [Q.i + 1, Q.j]];
      let best = 0, bd = Infinity;
      q.forEach((p, n) => { const d = (p[0] - x) ** 2 + (p[1] - y) ** 2; if (d < bd) { bd = d; best = n; } });
      return { i: cs[best][0], j: cs[best][1], x, y };
    }
    return null;
  }

  _wire() {
    const cv = this.cv;
    let drag = null;
    cv.addEventListener("pointerdown", ev => { drag = { x: ev.clientX, y: ev.clientY, yaw: this.yaw, pitch: this.pitch, moved: false }; cv.setPointerCapture(ev.pointerId); });
    cv.addEventListener("pointermove", ev => {
      if (drag) {
        const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
        this.yaw = drag.yaw + dx * 0.008;
        this.pitch = Math.max(0.12, Math.min(1.5, drag.pitch + dy * 0.006));
        this.hover = null; this.tip.hidden = true; this.draw();
        return;
      }
      const h = this._at(ev);
      const same = h && this.hover && h.i === this.hover.i && h.j === this.hover.j;
      this.hover = h;
      if (!same) this.draw();
      this._tip(h);
    });
    const end = ev => {
      if (drag && !drag.moved && this.opts.onPick) { const h = this._at(ev); if (h) this.opts.onPick(h); }
      drag = null;
    };
    cv.addEventListener("pointerup", end);
    cv.addEventListener("pointercancel", () => { drag = null; });
    cv.addEventListener("pointerleave", () => { if (drag) return; this.hover = null; this.tip.hidden = true; this.draw(); });
    cv.addEventListener("dblclick", () => this.reset());
    cv.addEventListener("keydown", ev => {
      const k = { ArrowLeft: [-0.12, 0], ArrowRight: [0.12, 0], ArrowUp: [0, -0.08], ArrowDown: [0, 0.08] }[ev.key];
      if (!k) return;
      ev.preventDefault();
      this.yaw += k[0]; this.pitch = Math.max(0.12, Math.min(1.5, this.pitch + k[1])); this.draw();
    });
  }

  _tip(h) {
    const D = this.data;
    if (!h || !D) { this.tip.hidden = true; return; }
    const v = D.rows[h.i].z[h.j];
    if (v == null) { this.tip.hidden = true; return; }
    const html = this.opts.tip ? this.opts.tip(h.i, h.j, v) : `${D.rows[h.i].label} · ${(D.xFmt || String)(D.x[h.j])}: <b>${(D.zFmt || String)(v)}</b>`;
    this.tip.innerHTML = html; this.tip.hidden = false;
    const w = this.host.clientWidth, tw = this.tip.offsetWidth;
    this.tip.style.left = Math.max(0, Math.min(w - tw, h.x + (h.x > w / 2 ? -tw - 14 : 14))) + "px";
    this.tip.style.top = Math.max(0, h.y - 34) + "px";
  }
}
