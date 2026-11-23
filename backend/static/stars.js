/* ==========================================================================
   The constellation: the home screen's picture of the book.

   Every holding is a star, placed in three dimensions by how it moved with
   every other over the past year (classical scaling refined by weighted
   stress minimisation, pf/mvo.py stars()), so stars that rise and fall
   together sit together. The world fund is a gold reference star: the
   nearer a holding sits to it, the more it simply moves with the market.
   Size is weight, colour today's move; a faint cloud marks each group that
   moves as one, and a line joins two holdings whose correlation is 0.55 or
   more. Each live tick sends a ring out from its star.

   Click a star to fly to it: its lines light, the rest dims, and the side
   panel says what it moves with; double-click opens it. The time-lapse
   re-forms the sky for the year to each of the last eight quarter-ends,
   each laid onto the next so that only real change moves a star.

   It turns slowly by itself, and by hand when dragged. It stops when the
   tab is hidden or the panel is off screen, and stands still for anyone who
   asked their system for less motion.

   The home screen rebuilds its HTML whenever a fetch lands, so the panel is
   built once and moved into its slot after every render: the canvas, the
   angle, the focus and the rings survive.
   ========================================================================== */

const Stars = (() => {
  let D = null, el = null, cv = null, ctx = null, tip = null, side = null;
  let W = 600, H = 360, DPR = 1, R = 140;
  let rotY = 0.5, rotX = -0.32, vY = 0, vX = 0;
  let drag = null, hover = -1, pulses = [], last = 0, raf = 0, onScreen = true;
  let dust = [], seen = {}, loading = false, err = null;
  let focus = -1, fz = 0, fpt = [0, 0, 0];
  const L = { on: false, t: 0, playing: false };
  const reduce = !!(window.matchMedia && matchMedia("(prefers-reduced-motion: reduce)").matches);
  const HUE = ["57,135,229", "217,89,38", "25,158,112", "168,121,224", "217,164,65", "79,168,206"];
  const BASE = [127, 166, 186], UP = [63, 179, 128], DN = [224, 100, 79], GOLD = [226, 182, 92];
  const SPIN = 0.00008;                     // radians a millisecond: a turn in about 80 seconds
  const LAPSE_MS = 1700;                    // a quarter of the time-lapse

  function build() {
    el = document.createElement("div");
    el.className = "panel stars";
    el.innerHTML = `<header><h2>Constellation</h2>
        <span class="hint">your holdings, placed by how they moved together over the past year · the gold star is the world</span>
        <a class="code" href="/m/MVO">MVO &rsaquo;</a></header>
      <div class="body st-body">
        <div class="st-sky"><canvas aria-label="your holdings as stars, placed by how they move together"></canvas>
          <div class="st-tip" style="display:none"></div>
          <div class="st-when" style="display:none"></div>
          <div class="st-msg"></div></div>
        <div class="st-side"></div>
      </div>`;
    cv = el.querySelector("canvas");
    ctx = cv.getContext("2d");
    tip = el.querySelector(".st-tip");
    side = el.querySelector(".st-side");
    // far-off dust, the same every visit
    let s = 20261013;
    const rnd = () => (s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296;
    dust = Array.from({ length: 150 }, () => {
      const u = rnd() * 2 - 1, th = rnd() * Math.PI * 2, r = Math.sqrt(1 - u * u);
      return { x: r * Math.cos(th) * 4, y: u * 4, z: r * Math.sin(th) * 4, b: 0.15 + rnd() * 0.35, ph: rnd() * 6.3 };
    });
    wire();
    if (window.ResizeObserver) new ResizeObserver(() => requestAnimationFrame(resize)).observe(el.querySelector(".st-sky"));
    if (window.IntersectionObserver)
      new IntersectionObserver(es => { onScreen = es.some(e => e.isIntersecting); kick(); }).observe(cv);
    document.addEventListener("visibilitychange", kick);
    document.addEventListener("keydown", e => { if (e.key === "Escape" && focus >= 0) setFocus(-1); });
  }

  function mount(slot) {
    if (!slot) return;
    if (!el) build();
    if (el.parentNode !== slot) slot.appendChild(el);
    resize();
    if (!D && !loading && !err) load();
  }

  async function load() {
    loading = true; err = null;
    msg("placing the stars<span class='blink'>_</span>");
    try {
      const r = await fetch("/api/pf/stars");
      const d = await r.json();
      if (!d.available) throw new Error(d.why || "no constellation");
      D = d;
      D.nodes.forEach((n, i) => { n.i = i; n.ph = (i * 2.399) % 6.283; });
      D.frames = D.frames || [];
      L.t = Math.max(0, D.frames.length - 1);
      msg("");
      legend();
    } catch (e) {
      err = e.message; msg(esc(err));
    } finally { loading = false; kick(); }
  }

  function msg(h) { const m = el && el.querySelector(".st-msg"); if (m) { m.innerHTML = h; m.style.display = h ? "" : "none"; } }

  function resize() {
    if (!el) return;
    const sky = el.querySelector(".st-sky");
    const w = Math.max(240, sky.clientWidth || 600), h = w < 520 ? 320 : 440;
    DPR = Math.min(2, window.devicePixelRatio || 1);
    if (w === W && h === H && cv.width === Math.round(w * DPR)) return;
    W = w; H = h; R = Math.min(W * 0.36, H * 0.38);
    cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR);
    cv.style.width = W + "px"; cv.style.height = H + "px";
    draw(performance.now());
  }

  /* ---------- turning, focusing, opening ---------- */
  function wire() {
    cv.addEventListener("pointerdown", e => {
      drag = { x: e.clientX, y: e.clientY, moved: 0, t: performance.now() };
      cv.setPointerCapture(e.pointerId);
    });
    cv.addEventListener("pointermove", e => {
      const r = cv.getBoundingClientRect(), mx = e.clientX - r.left, my = e.clientY - r.top;
      if (drag) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y, now = performance.now(), dt = Math.max(8, now - drag.t);
        drag.moved += Math.abs(dx) + Math.abs(dy);
        rotY += dx * 0.006; rotX = Math.max(-1.25, Math.min(1.25, rotX + dy * 0.006));
        vY = dx * 0.006 / dt; vX = dy * 0.006 / dt;
        drag.x = e.clientX; drag.y = e.clientY; drag.t = now;
        kick();
      }
      pick(mx, my);
    });
    const up = () => {
      if (!drag) return;
      const clicked = drag.moved < 5;
      drag = null;
      if (!clicked || !D) return;
      setFocus(hover >= 0 ? hover : -1);         // a star flies in; the sky flies back out
    };
    cv.addEventListener("pointerup", up);
    cv.addEventListener("dblclick", () => {
      if (hover >= 0 && D && !D.nodes[hover].world) location.assign("/t/" + encodeURIComponent(D.nodes[hover].t));
    });
    cv.addEventListener("pointercancel", () => { drag = null; });
    cv.addEventListener("pointerleave", () => { if (!drag) { hover = -1; tip.style.display = "none"; kick(); } });
  }

  function setFocus(i) {
    focus = i;
    legend();
    kick();
  }

  function kick() {
    if (raf || !el || document.hidden || !onScreen) return;
    raf = requestAnimationFrame(frame);
  }

  function frame(t) {
    raf = 0;
    const dt = last ? Math.min(64, t - last) : 16;
    last = t;
    if (!drag) {
      rotY += vY * dt + (reduce ? 0 : SPIN * dt);
      rotX = Math.max(-1.25, Math.min(1.25, rotX + vX * dt));
      vY *= Math.pow(0.996, dt); vX *= Math.pow(0.996, dt);
      if (Math.abs(vY) < 1e-6) vY = 0;
      if (Math.abs(vX) < 1e-6) vX = 0;
    }
    // the camera eases toward the focused star, and back
    const goal = focus >= 0 ? 1 : 0;
    fz += (goal - fz) * Math.min(1, dt / (reduce ? 1 : 260));
    if (Math.abs(goal - fz) < 0.002) fz = goal;
    // the time-lapse
    if (L.playing && D && D.frames.length > 1) {
      L.t = Math.min(D.frames.length - 1, L.t + dt / LAPSE_MS);
      if (L.t >= D.frames.length - 1) { L.playing = false; lapseUi(); }
      lapseLabel();
    }
    pulses = pulses.filter(p => t - p.t0 < 1400);
    draw(t);
    const busy = !reduce || drag || vX || vY || pulses.length || fz !== goal || L.playing;
    if (busy && !document.hidden && onScreen) raf = requestAnimationFrame(frame);
    else last = 0;
  }

  /* ---------- where a star is now ---------- */
  const ease = u => u < 0.5 ? 2 * u * u : 1 - Math.pow(-2 * u + 2, 2) / 2;
  function place(i) {
    // [x, y, z, visibility]: today's, or between two quarter-ends in the time-lapse
    const n = D.nodes[i];
    if (!L.on || !D.frames.length) return [n.x, n.y, n.z, 1];
    const F = D.frames, k = Math.min(F.length - 2, Math.floor(L.t)), u = F.length > 1 ? ease(L.t - k) : 0;
    const a = F[k] && F[k].pos[i], b = F[Math.min(F.length - 1, k + 1)] && F[Math.min(F.length - 1, k + 1)].pos[i];
    if (a && b) return [a[0] + (b[0] - a[0]) * u, a[1] + (b[1] - a[1]) * u, a[2] + (b[2] - a[2]) * u, 1];
    if (a) return [...a, 1 - u];
    if (b) return [...b, u];
    return [n.x, n.y, n.z, 0];
  }

  function proj(x, y, z) {
    // rotate about the focused star, and come closer to it
    x -= fpt[0] * fz; y -= fpt[1] * fz; z -= fpt[2] * fz;
    const cy = Math.cos(rotY), sy = Math.sin(rotY), cx = Math.cos(rotX), sx = Math.sin(rotX);
    const x1 = x * cy + z * sy, z1 = -x * sy + z * cy;
    const y1 = y * cx - z1 * sx, z2 = y * sx + z1 * cx;
    const f = 3.2 / (3.2 + z2), zoom = 1 + 0.9 * fz;
    return { x: W / 2 + x1 * f * R * zoom, y: H / 2 - y1 * f * R * zoom, f, z: z2 };
  }

  const mix = (a, b, k) => a.map((v, i) => Math.round(v + (b[i] - v) * k));
  function colour(n) {
    if (n.world) return GOLD;
    const d = n.day;
    if (d == null || Math.abs(d) < 0.05) return BASE;
    return mix(BASE, d > 0 ? UP : DN, Math.min(1, Math.abs(d) / 2) * 0.75 + 0.25);
  }

  function linksNow() {
    if (!L.on || !D.frames.length) return D.links;
    const f = D.frames[Math.round(L.t)];
    return f ? f.links.map(([a, b, c]) => ({ a, b, c })) : D.links;
  }

  function draw(t) {
    if (!ctx) return;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.clearRect(0, 0, W, H);
    // dust on a far sphere, turning a little slower than the stars
    for (const s of dust) {
      const p = proj(s.x * 0.9 + fpt[0] * fz, s.y * 0.9 + fpt[1] * fz, s.z * 0.9 + fpt[2] * fz);
      if (p.z < -2.6) continue;
      const tw = reduce ? 1 : 0.75 + 0.25 * Math.sin(t * 0.0012 + s.ph);
      ctx.fillStyle = `rgba(199,213,219,${(s.b * tw * 0.6).toFixed(3)})`;
      ctx.fillRect(p.x, p.y, 1.1, 1.1);
    }
    if (!D) return;
    const pl = D.nodes.map((n, i) => place(i));
    if (focus >= 0) fpt = pl[focus].slice(0, 3);
    const P = D.nodes.map((n, i) => ({ n, vis: pl[i][3], ...proj(pl[i][0], pl[i][1], pl[i][2]) }));
    const links = linksNow();
    const near = new Set(focus >= 0 ? links.filter(l => l.a === focus || l.b === focus).flatMap(l => [l.a, l.b]) : []);
    const dimOf = i => focus < 0 ? 1 : (i === focus || near.has(i) ? 1 : 1 - 0.75 * fz);
    // the groups that move as one: a faint cloud round each
    const groups = {};
    for (const p of P) if (p.n.g >= 0 && p.vis > 0.3) (groups[p.n.g] = groups[p.n.g] || []).push(p);
    for (const [g, ps] of Object.entries(groups)) {
      const cx = ps.reduce((s, p) => s + p.x, 0) / ps.length, cy = ps.reduce((s, p) => s + p.y, 0) / ps.length;
      const sp = Math.sqrt(ps.reduce((s, p) => s + (p.x - cx) ** 2 + (p.y - cy) ** 2, 0) / ps.length);
      const rr = sp * 1.6 + 26;
      const gr = ctx.createRadialGradient(cx, cy, 0, cx, cy, rr);
      const h = HUE[g % HUE.length], al = 0.13 * (focus >= 0 ? 1 - 0.6 * fz : 1);
      gr.addColorStop(0, `rgba(${h},${al.toFixed(3)})`); gr.addColorStop(1, `rgba(${h},0)`);
      ctx.fillStyle = gr;
      ctx.beginPath(); ctx.arc(cx, cy, rr, 0, 6.2832); ctx.fill();
    }
    // the lines between pairs that move together
    ctx.lineWidth = 1;
    for (const l of links) {
      const a = P[l.a], b = P[l.b];
      if (!a || !b) continue;
      const depth = Math.min(1, Math.max(0.25, (a.f + b.f) / 2 - 0.45)) * Math.min(a.vis, b.vis);
      const lit = hover === l.a || hover === l.b || focus === l.a || focus === l.b;
      const al = (lit ? 0.8 : Math.min(0.42, (l.c - 0.45) * 0.9) * (focus >= 0 ? 1 - 0.8 * fz : 1)) * depth;
      ctx.strokeStyle = lit ? `rgba(79,168,206,${al.toFixed(3)})` : `rgba(131,149,158,${al.toFixed(3)})`;
      ctx.lineWidth = lit ? 1.4 : 1;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    }
    // a focused star's tie to the world, drawn as a thread
    if (focus >= 0 && D.world != null && focus !== D.world && fz > 0.05) {
      const a = P[focus], b = P[D.world];
      ctx.setLineDash([3, 4]); ctx.strokeStyle = `rgba(226,182,92,${(0.55 * fz).toFixed(3)})`; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); ctx.setLineDash([]);
    }
    // the stars, far ones first
    const order = [...P].sort((a, b) => b.z - a.z);
    for (const p of order) {
      const n = p.n, c = colour(n);
      if (p.vis <= 0.02) continue;
      const tw = reduce ? 1 : 0.92 + 0.08 * Math.sin(t * 0.002 + n.ph);
      const r = (n.world ? 4.6 : 2 + Math.sqrt(Math.max(n.w, 0.05)) * 2.1) * p.f * (1 + 0.25 * fz);
      const fade = Math.min(1, Math.max(0.35, p.f - 0.25)) * p.vis * dimOf(n.i);
      const gr = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * (n.world ? 5.5 : 4.2));
      gr.addColorStop(0, `rgba(${c},${((n.world ? 0.5 : 0.42) * fade * tw).toFixed(3)})`); gr.addColorStop(1, `rgba(${c},0)`);
      ctx.fillStyle = gr;
      ctx.beginPath(); ctx.arc(p.x, p.y, r * (n.world ? 5.5 : 4.2), 0, 6.2832); ctx.fill();
      if (n.world) {
        // four rays and a ring: the reference star
        ctx.strokeStyle = `rgba(${c},${(0.75 * fade).toFixed(3)})`; ctx.lineWidth = 1;
        const rr = r * 2.6;
        ctx.beginPath(); ctx.moveTo(p.x - rr, p.y); ctx.lineTo(p.x + rr, p.y); ctx.moveTo(p.x, p.y - rr); ctx.lineTo(p.x, p.y + rr); ctx.stroke();
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 3.5, 0, 6.2832); ctx.stroke();
      }
      ctx.fillStyle = `rgba(${c},${(0.95 * fade).toFixed(3)})`;
      ctx.beginPath(); ctx.arc(p.x, p.y, r, 0, 6.2832); ctx.fill();
      ctx.fillStyle = `rgba(240,248,252,${(0.75 * fade * tw).toFixed(3)})`;
      ctx.beginPath(); ctx.arc(p.x, p.y, Math.max(0.6, r * 0.42), 0, 6.2832); ctx.fill();
      if (n.i === hover || n.i === focus) {
        ctx.strokeStyle = `rgba(79,168,206,${n.i === focus ? 0.95 : 0.8})`; ctx.lineWidth = n.i === focus ? 1.6 : 1.2;
        ctx.beginPath(); ctx.arc(p.x, p.y, r + 4, 0, 6.2832); ctx.stroke();
      }
      p.r = r;
    }
    // a ring for every live tick
    for (const q of pulses) {
      const p = P[q.i]; if (!p) continue;
      const k = (t - q.t0) / 1400, c = q.up ? UP : DN;
      ctx.strokeStyle = `rgba(${c},${(0.8 * (1 - k) * dimOf(q.i)).toFixed(3)})`; ctx.lineWidth = 1.4;
      ctx.beginPath(); ctx.arc(p.x, p.y, (p.r || 3) + 3 + k * 24, 0, 6.2832); ctx.stroke();
    }
    // names: the world, the focused star and its ties, the biggest holdings, whichever is pointed at
    ctx.font = "10px 'IBM Plex Mono', monospace";
    ctx.textBaseline = "middle";
    let named = [...D.nodes].filter(n => !n.world).sort((a, b) => b.w - a.w).slice(0, W < 520 ? 7 : 12).map(n => n.i);
    if (focus >= 0) named = [focus, ...near, ...named.slice(0, 4)];
    if (D.world != null) named.unshift(D.world);
    if (hover >= 0) named.unshift(hover);
    const boxes = [];
    for (const i of [...new Set(named)]) {
      const p = P[i];
      if (!p || p.vis < 0.3) continue;
      const w = ctx.measureText(p.n.label).width, r = (p.r || 3) + 5;
      let box = null;
      for (const x0 of [p.x + r, p.x - r - w]) {
        const b = [x0 - 2, p.y - 7, x0 + w + 2, p.y + 7];
        if (i === hover || i === focus || !boxes.some(o => !(b[2] < o[0] || b[0] > o[2] || b[3] < o[1] || b[1] > o[3]))) { box = b; break; }
      }
      if (!box) continue;
      boxes.push(box);
      const a = Math.min(1, Math.max(0.3, p.f - 0.2)) * (i === hover || i === focus ? 1 : 0.8) * p.vis * dimOf(i);
      ctx.fillStyle = p.n.world ? `rgba(226,182,92,${a.toFixed(3)})` : `rgba(199,213,219,${a.toFixed(3)})`;
      ctx.fillText(p.n.label, box[0] + 2, p.y);
    }
    Stars._last = P;
  }

  function pick(mx, my) {
    const P = Stars._last;
    if (!P) return;
    let best = -1, bd = 18 * 18;
    for (const p of P) {
      if (p.vis < 0.3) continue;
      const d = (p.x - mx) ** 2 + (p.y - my) ** 2;
      if (d < Math.max(bd, 0) && d < ((p.r || 3) + 10) ** 2 + 120) { bd = d; best = p.n.i; }
    }
    if (best !== hover) { hover = best; kick(); }
    if (best < 0) { tip.style.display = "none"; cv.style.cursor = "grab"; return; }
    cv.style.cursor = "pointer";
    const n = D.nodes[best];
    if (n.world) {
      tip.innerHTML = `<div class="h">The world <span class="dim">${esc(n.t)}</span></div>
        <div class="dt">The world fund, MSCI World: the reference star. The nearer a holding sits to it, the more it simply moves with the
          market; far from it, a holding goes its own way.</div>`;
    } else {
      const near = linksNow().filter(l => l.a === best || l.b === best).sort((a, b) => b.c - a.c).slice(0, 3)
        .map(l => `${esc(D.nodes[l.a === best ? l.b : l.a].label)} ${l.c.toFixed(2)}`);
      const grp = n.g >= 0 ? (D.clusters.find(c => c.g === n.g) || {}).labels : null;
      tip.innerHTML = `<div class="h">${esc(n.label)} <span class="dim">${esc(n.t)}</span></div>
        <div class="r"><span>weight</span><b>${n.w.toFixed(1)}%</b></div>
        <div class="r"><span>today</span><b class="${sgn(n.day)}">${n.day == null ? "–" : pct(n.day)}</b></div>
        <div class="r"><span>with the world</span><b>${n.cw != null ? n.cw.toFixed(2) : "–"}</b></div>
        ${near.length ? `<div class="r"><span>moves with</span><b>${near.join(", ")}</b></div>` : `<div class="r"><span>moves with</span><b>nothing closely</b></div>`}
        ${grp ? `<div class="dt">group of ${grp.length}: ${esc(grp.slice(0, 4).join(", "))}${grp.length > 4 ? "…" : ""}</div>` : ""}
        <div class="dt">click to fly to it · double-click to open it</div>`;
    }
    tip.style.display = "";
    const P2 = P.find(p => p.n.i === best);
    const tw = tip.offsetWidth;
    tip.style.left = Math.max(0, Math.min(W - tw, P2.x + (P2.x > W / 2 ? -tw - 14 : 14))) + "px";
    tip.style.top = Math.max(0, Math.min(H - tip.offsetHeight, P2.y - 20)) + "px";
  }

  /* ---------- the side panel ---------- */
  function legend() {
    if (!D) return;
    const movers = [...D.nodes].filter(n => n.day != null && !n.world).sort((a, b) => Math.abs(b.day) - Math.abs(a.day)).slice(0, 4);
    const F = D.frames || [];
    side.innerHTML = `${focus >= 0 ? focusHtml() : ""}
      <div class="st-k">Moving as one</div>
      ${D.clusters.slice(0, 5).map(c => `<div class="st-g"><i style="background:rgba(${HUE[c.g % HUE.length]},.85)"></i>
        <span><b>${c.w.toFixed(0)}%</b> ${esc(c.labels.slice(0, 3).join(", "))}${c.labels.length > 3 ? ` <span class="dim">+${c.labels.length - 3}</span>` : ""}</span></div>`).join("")}
      <div class="st-k">Brightest today</div>
      <div class="st-m" id="stMovers">${moversHtml(movers)}</div>
      ${F.length > 1 ? `<div class="st-k">Two years in motion</div>
        <div class="st-lapse"><button class="seg" id="stPlay">▶ Play</button>
          <input type="range" id="stScrub" min="0" max="${F.length - 1}" step="0.01" value="${L.t}" aria-label="Quarter shown">
          <button class="seg" id="stNow" title="Back to today">now</button></div>
        <div class="st-n" id="stWhen2"></div>` : ""}
      <div class="st-n">${D.n} stars and the world. Close stars rose and fell together over the year to ${esc(dmy(D.asof))}; a line joins
        two that correlate ${D.link.toFixed(2)} or more. Three dimensions cannot hold every distance between ${D.n + 1} stars exactly:
        the picture's follow the real ones with r = ${D.fidelity.toFixed(2)}, and distances to the world star are held tightest.
        Size is weight, colour today's move. Drag to turn it; click a star to fly to it, double-click to open it.</div>`;
    const b = side.querySelector("#focusOut");
    if (b) b.onclick = () => setFocus(-1);
    side.querySelectorAll("[data-fo]").forEach(a => a.onclick = e => { e.preventDefault(); setFocus(+a.dataset.fo); });
    lapseUi();
  }

  function focusHtml() {
    const n = D.nodes[focus];
    if (n.world) return `<div class="st-focus"><div class="st-k">Focus <button class="lnk" id="focusOut">back</button></div>
      <div><b>The world</b>: what the market as a whole did. ${D.nodes.filter(x => !x.world && x.cw >= 0.6).length} holdings move closely with it,
        ${D.nodes.filter(x => !x.world && x.cw < 0.2).length} hardly at all.</div></div>`;
    const ties = linksNow().filter(l => l.a === focus || l.b === focus).sort((a, b) => b.c - a.c)
      .map(l => ({ j: l.a === focus ? l.b : l.a, c: l.c }));
    return `<div class="st-focus"><div class="st-k">Focus <button class="lnk" id="focusOut">back</button></div>
      <div><b>${esc(n.label)}</b> <span class="dim">${esc(n.t)}</span> · ${n.w.toFixed(1)}% of the book${n.day != null ? ` · today <span class="${sgn(n.day)}">${pct(n.day)}</span>` : ""}</div>
      <div class="st-n" style="margin-top:.25rem">With the world ${n.cw != null ? n.cw.toFixed(2) : "–"}${n.cw != null ? (n.cw >= 0.6 ? ": it mostly moves with the market" : n.cw < 0.2 ? ": it goes its own way" : "") : ""}.
        ${ties.length ? `Moves most with ${ties.slice(0, 5).map(x => `<a href="#" data-fo="${x.j}">${esc(D.nodes[x.j].label)}</a> ${x.c.toFixed(2)}`).join(", ")}.` : "No holding moves closely with it."}
        <a href="/t/${encodeURIComponent(n.t)}">Open ${esc(n.t)} ›</a></div></div>`;
  }

  /* ---------- the time-lapse ---------- */
  function lapseUi() {
    const play = side.querySelector("#stPlay"), scrub = side.querySelector("#stScrub"), now = side.querySelector("#stNow");
    if (!play) return;
    play.textContent = L.playing ? "■ Stop" : "▶ Play";
    play.onclick = () => {
      if (L.playing) { L.playing = false; lapseUi(); return; }
      L.on = true;
      if (L.t >= D.frames.length - 1.001) L.t = 0;
      L.playing = true; lapseUi(); lapseLabel(); kick();
    };
    scrub.oninput = () => { L.on = true; L.playing = false; L.t = +scrub.value; lapseUi(); lapseLabel(); kick(); };
    now.onclick = () => { L.playing = false; L.t = D.frames.length - 1; L.on = false; lapseUi(); lapseLabel(); kick(); };
    lapseLabel();
  }

  function lapseLabel() {
    const w = el.querySelector(".st-when"), w2 = side.querySelector("#stWhen2"), sc = side.querySelector("#stScrub");
    if (sc && L.playing) sc.value = L.t;
    if (!D || !D.frames.length || !L.on) { if (w) w.style.display = "none"; if (w2) w2.textContent = "The year to each of the last eight quarter-ends, laid onto each other."; return; }
    const f = D.frames[Math.round(L.t)], d = new Date(f.d + "T12:00");
    const lab = `the year to ${d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })}`;
    if (w) { w.style.display = ""; w.textContent = lab; }
    if (w2) w2.textContent = `${lab} · ${f.n} stars · ${f.links.length} lines`;
  }

  const moversHtml = ms => ms.map(n => `<span><a href="/t/${encodeURIComponent(n.t)}">${esc(n.label)}</a> <b class="${sgn(n.day)}">${pct(n.day)}</b></span>`).join("");

  /* ---------- the live feed ---------- */
  function tick(rows) {
    if (!D || !rows || !rows.length) return;
    const at = {};
    D.nodes.forEach(n => { if (!n.world) at[n.t] = n; });
    let moved = false;
    for (const r of rows) {
      const n = at[r.ticker];
      if (!n || r.price == null) continue;
      const was = seen[r.ticker];
      seen[r.ticker] = r.price;
      if (r.day_pct != null) n.day = r.day_pct;
      if (was != null && r.price !== was) {
        pulses.push({ i: n.i, t0: performance.now(), up: r.price > was });
        moved = true;
      }
    }
    if (pulses.length > 40) pulses = pulses.slice(-40);
    const m = el && el.querySelector("#stMovers");
    if (m) m.innerHTML = moversHtml([...D.nodes].filter(n => n.day != null && !n.world)
      .sort((a, b) => Math.abs(b.day) - Math.abs(a.day)).slice(0, 4));
    if (moved || reduce) kick();
  }

  function reload() { D = null; err = null; seen = {}; focus = -1; fz = 0; L.on = false; L.playing = false; if (el) load(); }

  return { mount, tick, reload };
})();
