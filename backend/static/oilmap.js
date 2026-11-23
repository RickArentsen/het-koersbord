/* ==========================================================================
   The oil map: a flat Equal Earth map you can zoom and drag, or a globe you
   can turn, with the layers OIL hands it — production shading, chokepoints,
   ports, pipelines, platforms and live tankers.

   Everything is placed by latitude and longitude. The flat map projects
   static/land.json (Natural Earth, public domain) with the Equal Earth
   projection written out below; the globe uses d3-geo, loaded from cdnjs the
   first time it is asked for, because clipping a filled country at the
   horizon is what a library is for. The flat map needs nothing from outside.

   Markers keep a constant size on screen as you zoom, like GEO's. The legend
   is an element of the page's own (opts.legend), never drawn over the map:
   the page puts it beside the globe and under the flat map.
   ========================================================================== */

const OilMap = (() => {
  /* ---- Equal Earth (Šavrič, Patterson & Jenny, 2018) -------------------- */
  const A1 = 1.340264, A2 = -0.081106, A3 = 0.000893, A4 = 0.003796, M = Math.sqrt(3) / 2;
  function ee(lon, lat) {
    const l = lon * Math.PI / 180, t = Math.asin(M * Math.sin(lat * Math.PI / 180));
    const t2 = t * t, t6 = t2 * t2 * t2;
    return [l * Math.cos(t) / (M * (A1 + 3 * A2 * t2 + t6 * (7 * A3 + 9 * A4 * t2))),
            -t * (A1 + A2 * t2 + t6 * (A3 + A4 * t2))];
  }
  const X0 = ee(-180, 0)[0], X1 = ee(180, 0)[0], Y0 = ee(0, 84)[1], Y1 = ee(0, -58)[1];

  const COLORS = {
    ocean: "#060a0d", land: "#111a20", landLine: "#1d2a32", grat: "#0d151a",
    shut: "#ff6b57", severe: "#f0884f", reduced: "#d9a441", normal: "#3fb380", busy: "#4fa8ce",
    exp: "#d9a441", imp: "#4fa8ce", plat: "#c79a52", platN: "#e2b866", pipe: "#5b7684", bypass: "#e0644f",
    tank: "#e6eef1", sel: "#ffffff",
  };
  const STATUS_COL = { "shut": COLORS.shut, "severely reduced": COLORS.severe, "reduced": COLORS.reduced,
                       "normal": COLORS.normal, "busier than a year ago": COLORS.busy };

  const S = {
    host: null, cv: null, ctx: null, tip: null, w: 0, h: 0, dpr: 1, mode: "flat",
    land: [], paths: null, data: { map: null, platforms: [], tankers: [] },
    layers: { shade: true, choke: true, ports: true, pipes: true, plat: true, tank: true },
    shadeBy: "kbd", k: 1, ox: 0, oy: 0, s0: 1, hits: [], hover: null, sel: null, onSelect: null,
    g: { rot: [-48, -22, 0], scale: 1, spin: true, d3: null, feats: null, loading: false },
    raf: 0, dirty: true, fill: false, leg: null, onMode: null, ro: null,
  };

  /* ---- setup ---------------------------------------------------------- */
  async function init(host, opts = {}) {
    S.host = host;
    S.onSelect = opts.onSelect || null;
    S.leg = opts.legend || null;
    S.onMode = opts.onMode || null;
    S.fill = false;
    host.innerHTML = `<canvas></canvas><div class="map-tip" style="display:none"></div>
      <div class="map-ctl"><button data-z="in" title="Zoom in">+</button><button data-z="out" title="Zoom out">−</button>
      <button data-z="home" title="Whole world">⌂</button><button data-z="mode" title="Flat map or globe">◐</button>
      <button data-z="spin" title="Turn the globe, or stop it">⟳</button></div>`;
    S.cv = host.querySelector("canvas");
    S.ctx = S.cv.getContext("2d");
    S.tip = host.querySelector(".map-tip");
    host.querySelectorAll(".map-ctl button").forEach(b => b.onclick = () => ctl(b.dataset.z));
    let lj = null;
    try { lj = await (await fetch("/land.json")).json(); } catch { }
    S.land = (lj && lj.c) || [];
    S.landSrc = lj && lj.src;
    buildPaths();
    modeChanged();
    wire();
    // Only a change the map did not make itself: in the page its height is the
    // canvas's own, so reacting to that would loop.
    if (S.ro) S.ro.disconnect();
    S.ro = new ResizeObserver(() => {
      if (Math.max(280, Math.round(S.host.clientWidth)) !== S.w || (S.fill && Math.max(240, Math.round(S.host.clientHeight)) !== S.h)) resize();
    });
    S.ro.observe(host);
    kick();
  }

  function buildPaths() {
    S.paths = S.land.map(c => {
      const p = new Path2D();
      for (const r of c.p) {
        for (let i = 0; i < r.length; i += 2) {
          const [x, y] = ee(r[i], r[i + 1]);
          if (i === 0) p.moveTo(x, y); else p.lineTo(x, y);
        }
        p.closePath();
      }
      return { iso: c.iso, name: c.name, path: p };
    });
  }

  /* In the page the map's height follows its width (the globe's is taller, so
     it keeps its size with the legend beside it); filling the tab, it takes
     the height the page gives it. The middle of the view stays where it was. */
  function resize() {
    const w = Math.max(280, Math.round(S.host.clientWidth));
    const h = S.fill ? Math.max(240, Math.round(S.host.clientHeight))
      : Math.round(Math.min(Math.max(300, w * (S.mode === "globe" ? 0.72 : 0.5)), window.innerHeight * 0.78));
    const mid = S.placed ? [(S.w / 2 - S.ox) / (S.s0 * S.k), (S.h / 2 - S.oy) / (S.s0 * S.k)] : null;
    S.dpr = window.devicePixelRatio || 1;
    S.w = w; S.h = h;
    S.cv.width = Math.round(w * S.dpr); S.cv.height = Math.round(h * S.dpr);
    S.cv.style.height = S.fill ? "" : h + "px";
    S.s0 = Math.min(w / (X1 - X0), h / (Y1 - Y0));
    if (!S.placed) home();
    else { S.ox = w / 2 - mid[0] * S.s0 * S.k; S.oy = h / 2 - mid[1] * S.s0 * S.k; }
    clamp();
    kick();
  }

  /* the page lays the legend out for the mode, then the map takes its size */
  function modeChanged() {
    if (S.onMode) S.onMode(S.mode);
    const sp = S.host.querySelector('[data-z="spin"]');
    if (sp) { sp.style.display = S.mode === "globe" ? "" : "none"; sp.classList.toggle("on", !!S.g.spin); }
    legend();
    resize();
  }

  function home() {
    S.k = 1;
    S.ox = (S.w - (X1 - X0) * S.s0) / 2;
    S.oy = (S.h - (Y1 - Y0) * S.s0) / 2;
    S.placed = true;
    S.g.rot = [-48, -22, 0]; S.g.scale = 1;
    kick();
  }

  function clamp() {
    const mw = (X1 - X0) * S.s0 * S.k, mh = (Y1 - Y0) * S.s0 * S.k;
    S.ox = mw <= S.w ? (S.w - mw) / 2 : Math.min(0, Math.max(S.w - mw, S.ox));
    S.oy = mh <= S.h ? (S.h - mh) / 2 : Math.min(0, Math.max(S.h - mh, S.oy));
  }

  /* screen position of a latitude and longitude, or null when hidden */
  function at(lon, lat) {
    if (S.mode === "globe" && S.g.proj) {
      const d3 = S.g.d3;
      if (d3.geoDistance([lon, lat], [-S.g.rot[0], -S.g.rot[1]]) > Math.PI / 2 - 0.02) return null;
      return S.g.proj([lon, lat]);
    }
    const [x, y] = ee(lon, lat);
    return [(x - X0) * S.s0 * S.k + S.ox, (y - Y0) * S.s0 * S.k + S.oy];
  }

  /* ---- interaction ---------------------------------------------------- */
  function wire() {
    const cv = S.cv;
    let drag = null;
    cv.addEventListener("pointerdown", e => {
      drag = { x: e.clientX, y: e.clientY, ox: S.ox, oy: S.oy, rot: [...S.g.rot], moved: false };
      cv.setPointerCapture(e.pointerId); cv.classList.add("drag");
      spin(false);
    });
    cv.addEventListener("pointermove", e => {
      if (drag) {
        const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
        if (S.mode === "globe") {
          const f = 0.28 / S.g.scale;
          S.g.rot = [drag.rot[0] + dx * f, Math.max(-80, Math.min(80, drag.rot[1] - dy * f)), 0];
        } else { S.ox = drag.ox + dx; S.oy = drag.oy + dy; clamp(); }
        kick();
        tipOff();
        return;
      }
      hover(e);
    });
    const up = e => {
      if (!drag) return;
      const was = drag; drag = null; cv.classList.remove("drag");
      if (!was.moved) click(e);
    };
    cv.addEventListener("pointerup", up);
    cv.addEventListener("pointercancel", () => { drag = null; cv.classList.remove("drag"); });
    cv.addEventListener("pointerleave", () => tipOff());
    cv.addEventListener("wheel", e => {
      e.preventDefault();
      const f = e.deltaY < 0 ? 1.25 : 0.8;
      if (S.mode === "globe") { S.g.scale = Math.max(0.8, Math.min(8, S.g.scale * f)); spin(false); }
      else zoomAt(e.offsetX, e.offsetY, f);
      kick();
    }, { passive: false });
  }

  function zoomAt(px, py, f) {
    const k2 = Math.max(1, Math.min(60, S.k * f));
    const r = k2 / S.k;
    S.ox = px - (px - S.ox) * r; S.oy = py - (py - S.oy) * r; S.k = k2;
    clamp();
  }

  function ctl(z) {
    if (z === "in" || z === "out") {
      const f = z === "in" ? 1.6 : 1 / 1.6;
      if (S.mode === "globe") S.g.scale = Math.max(0.8, Math.min(8, S.g.scale * f));
      else zoomAt(S.w / 2, S.h / 2, f);
    } else if (z === "home") { home(); clamp(); }
    else if (z === "mode") setMode(S.mode === "flat" ? "globe" : "flat");
    else if (z === "spin") spin(!S.g.spin);
    kick();
  }

  function spin(on) {
    S.g.spin = on;
    const b = S.host && S.host.querySelector('[data-z="spin"]');
    if (b) b.classList.toggle("on", on);
    kick();
  }

  async function setMode(m) {
    if (m === "globe" && !S.g.d3) {
      if (S.g.loading) return;
      S.g.loading = true; kick();
      try { await loadD3(); } catch (e) {
        S.g.loading = false; S.g.err = "The globe needs d3 from cdnjs.cloudflare.com, which did not load. The flat map works offline.";
        kick(); legend(); return;
      }
      S.g.loading = false;
    }
    S.mode = m; S.g.spin = m === "globe";
    modeChanged();
  }

  function loadD3() {
    if (window.d3 && window.d3.geoOrthographic) { S.g.d3 = window.d3; buildFeats(); return Promise.resolve(); }
    return new Promise((ok, bad) => {
      const s = document.createElement("script");
      s.src = "https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js";
      s.onload = () => { S.g.d3 = window.d3; buildFeats(); ok(); };
      s.onerror = bad;
      document.head.appendChild(s);
    });
  }

  /* d3 wants exterior rings wound clockwise on the sphere; a ring that comes
     out covering more than half the globe is the wrong way round. */
  function buildFeats() {
    const d3 = S.g.d3;
    S.g.feats = S.land.map(c => {
      const polys = c.p.map(r => {
        const ring = [];
        for (let i = 0; i < r.length; i += 2) ring.push([r[i], r[i + 1]]);
        if (ring.length && (ring[0][0] !== ring[ring.length - 1][0] || ring[0][1] !== ring[ring.length - 1][1])) ring.push(ring[0]);
        let poly = [ring];
        if (d3.geoArea({ type: "Polygon", coordinates: poly }) > 2 * Math.PI) poly = [ring.slice().reverse()];
        return poly;
      });
      return { type: "Feature", properties: { iso: c.iso, name: c.name }, geometry: { type: "MultiPolygon", coordinates: polys } };
    });
    S.g.grat = d3.geoGraticule10();
  }

  /* ---- hit testing ---------------------------------------------------- */
  function nearest(e) {
    const r = S.cv.getBoundingClientRect();
    const x = (e.clientX - r.left), y = (e.clientY - r.top);
    let best = null, bd = 1e9;
    for (const h of S.hits) {
      const d = Math.hypot(h.x - x, h.y - y);
      if (d <= h.r + 4 && d < bd) { bd = d; best = h; }
    }
    return { best, x, y };
  }

  /* The country under a point. A Path2D is tested under the context's current
     transform, so the test uses the same one the countries were drawn with,
     and the point in device pixels. */
  function countryAt(x, y) {
    if (S.mode === "globe") {
      if (!S.g.proj || !S.g.feats) return null;
      const ll = S.g.proj.invert([x, y]);
      if (!ll || S.g.d3.geoDistance(ll, [-S.g.rot[0], -S.g.rot[1]]) > Math.PI / 2) return null;
      const f = S.g.feats.find(f => S.g.d3.geoContains(f, ll));
      return f ? { iso: f.properties.iso, name: f.properties.name } : null;
    }
    if (!S.paths) return null;
    const c = S.ctx, sc = S.s0 * S.k, dpr = S.dpr;
    c.save();
    c.setTransform(dpr * sc, 0, 0, dpr * sc, dpr * (S.ox - X0 * sc), dpr * (S.oy - Y0 * sc));
    let hit = null;
    for (const p of S.paths) if (c.isPointInPath(p.path, x * dpr, y * dpr)) { hit = p; break; }
    c.restore();
    return hit;
  }

  function hover(e) {
    const { best, x, y } = nearest(e);
    let html = best ? tipFor(best) : null;
    if (!best) {
      const c = countryAt(x, y);
      if (c) html = countryTip(c);
    }
    if (!html) return tipOff();
    S.tip.innerHTML = html;
    S.tip.style.display = "";
    const tw = S.tip.offsetWidth, th = S.tip.offsetHeight;
    S.tip.style.left = Math.max(4, Math.min(S.w - tw - 4, x + 14)) + "px";
    S.tip.style.top = Math.max(4, Math.min(S.h - th - 4, y + 12)) + "px";
  }
  function tipOff() { if (S.tip) S.tip.style.display = "none"; }

  function click(e) {
    const { best, x, y } = nearest(e);
    if (best) { S.sel = best.key; kick(); if (S.onSelect) S.onSelect(best.kind, best.item); return; }
    const c = countryAt(x, y);
    if (c && S.onSelect) S.onSelect("country", { iso: c.iso, name: c.name });
  }

  const fmt = (v, d = 0) => v == null ? "–" : Number(v).toLocaleString("en-GB", { maximumFractionDigits: d, minimumFractionDigits: d });

  function tipFor(h) {
    const it = h.item;
    if (h.kind === "choke")
      return `<b>${esc(it.name)}</b><br>${fmt(it.w7, 1)} tankers a day (7 days to ${dmy(it.end)})<br>`
        + `same week last year ${fmt(it.y7, 1)}${it.vs_y7 != null ? ` · <b>${it.vs_y7 > 0 ? "+" : ""}${fmt(it.vs_y7)}%</b>` : ""}<br>`
        + `<span class="dim">${esc(it.status || "")}</span>`;
    if (h.kind === "port")
      return `<b>${esc(it.name)}</b> <span class="dim">${esc(it.country || "")}</span><br>`
        + `loaded ${fmt(it.exp / 1000, 1)} kt a day · discharged ${fmt(it.imp / 1000, 1)} kt<br>`
        + `<span class="dim">${it.calls} tanker calls in ${S.data.map?.ports_window?.days || 30} days (PortWatch)</span>`;
    if (h.kind === "plat")
      return `<b>${esc(it[4] || "Offshore platform")}</b>${it[5] ? `<br>${esc(it[5])}` : ""}<br><span class="dim">${it[2] === "sodir" ? "Sodir (Norway)" : "OpenStreetMap"}${it[3] ? " · " + esc(it[3]) : ""}${it[6] ? " · " + esc(it[6]) : ""}</span>`;
    if (h.kind === "pipe")
      return `<b>${esc(it.name)}</b><br>${esc(it.from)} → ${esc(it.to)}<br>${esc(it.cap || "")}${it.bypass ? `<br><span class="k-warn">avoids ${esc(it.bypass)}</span>` : ""}`;
    if (h.kind === "tank") {
      const t = it;
      return `<b>${esc(t[4] || "Tanker " + t[8])}</b><br>${t[5] ? "to " + esc(t[5]) + "<br>" : ""}`
        + `${t[2] != null ? fmt(t[2], 1) + " knots" : ""}${t[6] ? ` · draught ${fmt(t[6], 1)} m` : ""}<br>`
        + `<span class="dim">AIS type ${t[7] || "?"} · MMSI ${t[8]} · ${t[9] < 120 ? t[9] + " s" : Math.round(t[9] / 60) + " min"} ago · ${t.src}</span>`;
    }
    return null;
  }

  function countryTip(c) {
    const p = (S.data.map?.production || {})[c.iso];
    if (!p) return `<b>${esc(c.name)}</b>`;
    return `<b>${esc(p.name || c.name)}</b><br>${fmt(p.kbd)} kb/d (${esc(p.src)} ${esc(p.kind)}, ${mon(p.month + "-01")})`
      + (p.d12 != null ? `<br>${p.d12 > 0 ? "+" : ""}${fmt(p.d12)} kb/d on a year ago` : "")
      + `<br><span class="dim">click for the country</span>`;
  }

  /* ---- drawing -------------------------------------------------------- */
  /* Draw only when something changed: a frame is asked for by kick(), and
     only the spinning globe asks for the next one itself. An idle map costs
     nothing. */
  function kick() { S.dirty = true; if (!S.raf) S.raf = requestAnimationFrame(frame); }
  document.addEventListener("visibilitychange", () => { if (!document.hidden && S.host) kick(); });
  window.addEventListener("resize", () => { if (S.host && S.host.isConnected) resize(); });   // the window's height caps the map's
  function frame() {
    S.raf = 0;
    const turning = S.mode === "globe" && S.g.spin && S.g.d3 && !document.hidden;
    if (turning) { S.g.rot = [S.g.rot[0] + 0.06, S.g.rot[1], 0]; S.dirty = true; }
    if (S.dirty && S.host.isConnected) { S.dirty = false; draw(); }
    if (turning) S.raf = requestAnimationFrame(frame);
  }

  function shadeColor(iso) {
    const p = (S.data.map?.production || {})[iso];
    if (!p || !S.layers.shade) return COLORS.land;
    if (S.shadeBy === "d12") {
      if (p.d12 == null || !p.kbd) return COLORS.land;
      const r = Math.max(-1, Math.min(1, p.d12 / Math.max(150, p.kbd * 0.25)));
      return r >= 0 ? mix("#111a20", "#2f8f63", r) : mix("#111a20", "#b5503f", -r);
    }
    const t = Math.max(0, Math.min(1, Math.log10(Math.max(10, p.kbd)) / Math.log10(14000)));
    return mix("#111a20", "#2b6f8f", Math.pow(t, 1.6));
  }

  function mix(a, b, t) {
    const pa = [1, 3, 5].map(i => parseInt(a.slice(i, i + 2), 16)), pb = [1, 3, 5].map(i => parseInt(b.slice(i, i + 2), 16));
    return "#" + pa.map((v, i) => Math.round(v + (pb[i] - v) * t).toString(16).padStart(2, "0")).join("");
  }

  function draw() {
    const c = S.ctx, dpr = S.dpr;
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
    c.fillStyle = "#04070a"; c.fillRect(0, 0, S.w, S.h);
    S.hits = [];
    if (S.mode === "globe" && S.g.d3) drawGlobeBase(); else drawFlatBase();
    if (S.layers.pipes) drawPipes();
    if (S.layers.plat) drawPlatforms();
    if (S.layers.ports) drawPorts();
    if (S.layers.choke) drawChokes();
    if (S.layers.tank) drawTankers();
    if (S.g.loading) { c.fillStyle = "rgba(4,7,10,.6)"; c.fillRect(0, 0, S.w, S.h); c.fillStyle = "#83959e"; c.font = "12px IBM Plex Mono, monospace"; c.fillText("loading the globe…", 14, 22); }
  }

  function drawFlatBase() {
    const c = S.ctx, sc = S.s0 * S.k, dpr = S.dpr;
    c.setTransform(dpr * sc, 0, 0, dpr * sc, dpr * (S.ox - X0 * sc), dpr * (S.oy - Y0 * sc));
    // the sphere's outline: the ocean
    c.beginPath();
    for (let lat = -90; lat <= 90; lat += 2) { const [x, y] = ee(-180, lat); lat === -90 ? c.moveTo(x, y) : c.lineTo(x, y); }
    for (let lat = 90; lat >= -90; lat -= 2) { const [x, y] = ee(180, lat); c.lineTo(x, y); }
    c.closePath(); c.fillStyle = COLORS.ocean; c.fill();
    c.lineWidth = 0.8 / sc; c.strokeStyle = COLORS.grat;
    for (let lon = -150; lon <= 180; lon += 30) { c.beginPath(); for (let lat = -80; lat <= 80; lat += 4) { const [x, y] = ee(lon, lat); lat === -80 ? c.moveTo(x, y) : c.lineTo(x, y); } c.stroke(); }
    for (let lat = -60; lat <= 80; lat += 20) { c.beginPath(); for (let lon = -180; lon <= 180; lon += 4) { const [x, y] = ee(lon, lat); lon === -180 ? c.moveTo(x, y) : c.lineTo(x, y); } c.stroke(); }
    c.lineWidth = 0.7 / sc; c.strokeStyle = COLORS.landLine;
    for (const p of S.paths || []) {
      c.fillStyle = shadeColor(p.iso);
      c.fill(p.path); c.stroke(p.path);
    }
    c.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function drawGlobeBase() {
    const d3 = S.g.d3, c = S.ctx;
    const R = Math.min(S.w, S.h) / 2 - 8;
    const proj = d3.geoOrthographic().translate([S.w / 2, S.h / 2]).scale(R * S.g.scale).rotate(S.g.rot).clipAngle(90).precision(0.6);
    S.g.proj = proj;
    const path = d3.geoPath(proj, c);
    c.beginPath(); path({ type: "Sphere" }); c.fillStyle = COLORS.ocean; c.fill();
    c.lineWidth = 0.6; c.strokeStyle = COLORS.grat; c.beginPath(); path(S.g.grat); c.stroke();
    c.lineWidth = 0.6; c.strokeStyle = COLORS.landLine;
    for (const f of S.g.feats || []) {
      c.beginPath(); path(f); c.fillStyle = shadeColor(f.properties.iso); c.fill(); c.stroke();
    }
    c.beginPath(); path({ type: "Sphere" }); c.lineWidth = 1; c.strokeStyle = "#25353e"; c.stroke();
  }

  function zr(base) { return S.mode === "globe" ? base * Math.min(1.8, 0.8 + 0.25 * S.g.scale) : base * Math.min(2.2, 1 + 0.18 * Math.log2(S.k)); }

  function drawPipes() {
    const c = S.ctx, P = (S.data.map?.pipelines?.lines) || [];
    for (const p of P) {
      for (const line of [p.pts, p.branch].filter(Boolean)) {
        c.beginPath();
        let pen = false, mid = null;
        const seg = densify(line);
        for (const [lon, lat] of seg) {
          const q = at(lon, lat);
          if (!q) { pen = false; continue; }
          pen ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1]);
          pen = true;
        }
        c.setLineDash([5, 3]); c.lineWidth = p.bypass ? 2 : 1.4; c.strokeStyle = p.bypass ? COLORS.bypass : COLORS.pipe;
        c.globalAlpha = 0.9; c.stroke(); c.setLineDash([]); c.globalAlpha = 1;
        const m = seg[Math.floor(seg.length / 2)], q = m && at(m[0], m[1]);
        if (q && line === p.pts) S.hits.push({ x: q[0], y: q[1], r: 6, kind: "pipe", item: p, key: "pipe:" + p.k });
      }
    }
  }

  function densify(line) {
    const out = [];
    for (let i = 0; i < line.length - 1; i++) {
      const [a, b] = [line[i], line[i + 1]];
      const n = Math.max(1, Math.ceil(Math.hypot(b[0] - a[0], b[1] - a[1]) / 2));
      for (let j = 0; j < n; j++) out.push([a[0] + (b[0] - a[0]) * j / n, a[1] + (b[1] - a[1]) * j / n]);
    }
    out.push(line[line.length - 1]);
    return out;
  }

  function drawPlatforms() {
    const c = S.ctx, P = S.data.platforms || [];
    const r = zr(1.25);
    c.globalAlpha = 0.85;
    for (const p of P) {
      const q = at(p[1], p[0]);
      if (!q || q[0] < -2 || q[1] < -2 || q[0] > S.w + 2 || q[1] > S.h + 2) continue;
      c.fillStyle = p[2] === "sodir" ? COLORS.platN : COLORS.plat;
      c.fillRect(q[0] - r, q[1] - r, r * 2, r * 2);
      if (S.k >= 3 || S.mode === "globe" && S.g.scale >= 2.5) S.hits.push({ x: q[0], y: q[1], r: r + 1, kind: "plat", item: p, key: "plat:" + p[0] + p[1] });
    }
    c.globalAlpha = 1;
  }

  function drawPorts() {
    const c = S.ctx, P = (S.data.map?.ports) || [];
    for (const p of P) {
      const q = at(p.lon, p.lat);
      if (!q) continue;
      const t = (p.exp + p.imp) / 1000;
      const r = zr(Math.min(15, 1.6 + Math.sqrt(t) * 0.9));
      if (r < 2 && S.k < 2) continue;
      const ex = p.exp >= p.imp;
      c.beginPath(); c.arc(q[0], q[1], r, 0, 2 * Math.PI);
      c.fillStyle = ex ? "rgba(217,164,65,.38)" : "rgba(79,168,206,.32)"; c.fill();
      c.lineWidth = 0.8; c.strokeStyle = ex ? "rgba(217,164,65,.85)" : "rgba(79,168,206,.8)"; c.stroke();
      S.hits.push({ x: q[0], y: q[1], r, kind: "port", item: p, key: "port:" + p.id });
    }
  }

  function drawChokes() {
    const c = S.ctx, P = (S.data.map?.chokepoints) || [];
    c.font = "10px IBM Plex Mono, monospace";
    for (const p of P) {
      const q = at(p.lon, p.lat);
      if (!q) continue;
      const r = zr(4 + Math.sqrt(Math.max(0, p.w7 || 0)) * 0.9);
      const col = STATUS_COL[p.status] || "#83959e";
      c.beginPath(); c.arc(q[0], q[1], r, 0, 2 * Math.PI);
      c.fillStyle = col + "33"; c.fill();
      c.lineWidth = S.sel === "choke:" + p.id ? 2.4 : 1.6; c.strokeStyle = S.sel === "choke:" + p.id ? COLORS.sel : col; c.stroke();
      c.beginPath(); c.arc(q[0], q[1], 1.8, 0, 2 * Math.PI); c.fillStyle = col; c.fill();
      if (p.oil && S.w >= 620) {
        const lab = `${p.name.replace(/ Strait$| Canal$/, "").replace("Strait of ", "")} ${p.w7 != null ? (p.w7 < 10 ? p.w7.toFixed(1) : Math.round(p.w7)) : "–"}/d`;
        c.fillStyle = "rgba(4,7,10,.75)"; const tw = c.measureText(lab).width;
        c.fillRect(q[0] + r + 3, q[1] - 7, tw + 6, 13);
        c.fillStyle = col; c.fillText(lab, q[0] + r + 6, q[1] + 3);
      }
      S.hits.push({ x: q[0], y: q[1], r: Math.max(r, 7), kind: "choke", item: p, key: "choke:" + p.id });
    }
  }

  function drawTankers() {
    const c = S.ctx, T = S.data.tankers || [];
    const s = zr(3.2);
    c.globalAlpha = 0.92;
    for (const t of T) {
      const q = at(t[1], t[0]);
      if (!q || q[0] < -5 || q[1] < -5 || q[0] > S.w + 5 || q[1] > S.h + 5) continue;
      const a = ((t[3] != null && t[3] < 360 ? t[3] : 0) - 90) * Math.PI / 180;
      c.save(); c.translate(q[0], q[1]); c.rotate(a);
      c.beginPath(); c.moveTo(s * 1.6, 0); c.lineTo(-s, s * 0.85); c.lineTo(-s, -s * 0.85); c.closePath();
      c.fillStyle = (t[2] || 0) < 0.8 ? "#9aa9b0" : COLORS.tank; c.fill();
      c.restore();
      S.hits.push({ x: q[0], y: q[1], r: s + 1.5, kind: "tank", item: t, key: "tank:" + t[8] });
    }
    c.globalAlpha = 1;
  }

  /* ---- the legend ----------------------------------------------------- */
  /* One block per layer that is on: a heading and its symbols. The page's CSS
     lays the blocks out as a column beside the globe or a strip under the map. */
  function legend() {
    const L = S.leg;
    if (!L) return;
    const it = (sym, txt) => `<span class="it">${sym}${txt}</span>`;
    const dot = c => `<i style="background:${c}33;border:1.5px solid ${c}"></i>`;
    const sq = c => `<i class="sq" style="background:${c}"></i>`;
    const ln = c => `<i class="ln" style="border-color:${c}"></i>`;
    const blk = (h, desc, items, note = "") => `<div class="lg"><b>${h}</b>${desc ? `<span class="ds">${desc}</span>` : ""}`
      + `<span class="its">${items}</span>${note ? `<span class="nt dim">${note}</span>` : ""}</div>`;
    const out = [];
    if (S.layers.shade) out.push(S.shadeBy === "d12"
      ? blk("Countries", "crude output against a year ago", it(sq("#2f8f63"), "up") + it(sq("#b5503f"), "down"))
      : blk("Countries", "shaded by crude output", it(`<i class="grad"></i>`, "brighter: more")));
    if (S.layers.choke) out.push(blk("Chokepoints", "sized by tankers a day, coloured by the change on a year",
      [["normal", COLORS.normal], ["busier", COLORS.busy], ["reduced", COLORS.reduced], ["severely reduced", COLORS.severe], ["shut", COLORS.shut]]
        .map(([t, c]) => it(dot(c), t)).join("")));
    if (S.layers.ports) out.push(blk("Ports", "sized by tanker cargo, last month of data", it(dot(COLORS.exp), "loads more") + it(dot(COLORS.imp), "discharges more")));
    if (S.layers.pipes) out.push(blk("Pipelines", "", it(ln(COLORS.bypass), "avoids Hormuz") + it(ln(COLORS.pipe), "other"), "routes are schematic"));
    if (S.layers.plat) out.push(blk("Platforms", "", it(sq(COLORS.plat), "offshore platform") + it(sq(COLORS.platN), "Norway's list")));
    if (S.layers.tank) out.push(blk("Live tankers", "", it(`<span class="tri" style="color:${COLORS.tank}">▲</span>`, "under way")
      + it(`<span class="tri" style="color:#9aa9b0">▲</span>`, "stopped"), "pointing the way it sails"));
    if (S.mode === "globe") out.push(`<div class="lg dim">Drag to turn · scroll to zoom · ⟳ starts or stops the turning · ⌂ back to the start</div>`);
    if (S.g.err) out.push(`<div class="lg k-warn">${esc(S.g.err)}</div>`);
    L.innerHTML = out.join("");
  }

  /* ---- the outside interface ------------------------------------------ */
  return {
    init,
    set(part, v) { S.data[part] = v; kick(); legend(); },
    layers(l) { Object.assign(S.layers, l); kick(); legend(); },
    shade(by) { S.shadeBy = by; kick(); legend(); },
    mode: setMode,
    /* true: the page has given the map a height of its own (the full-tab view) */
    fill(on) { S.fill = !!on; resize(); },
    get state() { return { mode: S.mode, layers: { ...S.layers }, shadeBy: S.shadeBy }; },
    focus(lon, lat, k = 6) {
      if (S.mode === "globe") { S.g.rot = [-lon, -lat, 0]; S.g.scale = Math.max(S.g.scale, 2); spin(false); }
      else { const [x, y] = ee(lon, lat); S.k = k; S.ox = S.w / 2 - (x - X0) * S.s0 * k; S.oy = S.h / 2 - (y - Y0) * S.s0 * k; clamp(); }
      kick();
    },
    select(key) { S.sel = key; kick(); },
    ee, landSource: () => S.landSrc,
  };
})();
