/* ==========================================================================
   RRG — relative rotation.

   One request, /api/pf/rotation, returns every name's trend and momentum for
   the last forty periods. The page draws the last few as a tail, and the
   slider replays the rest without asking the server again.

   Right of centre: gaining on the benchmark. Above centre: gaining faster.
   A name travels clockwise through improving, leading, weakening, lagging.
   ========================================================================== */

const Q0 = new URLSearchParams(location.search);
const ST = {
  universe: Q0.get("u") || "book", bench: Q0.get("b"), freq: Q0.get("f") === "d" ? "d" : "w",
  tail: +Q0.get("tail") || 6, show: Q0.get("show") === "all" ? "all" : "top",
  pins: new Set((Q0.get("pin") || "").split(",").filter(Boolean)),
  at: null, hover: null, sort: { k: null, dir: -1 }, timer: null,
  zoom: { k: 1, cx: 100, cy: 100 }, wide: false, view: null, focus: null, dragged: false,
};
try { ST.wide = localStorage.getItem("rrg_wide") === "1"; } catch {}
let R = null;
const TOP = 15;
const QCOL = { leading: "var(--q-leading)", weakening: "var(--q-weakening)", lagging: "var(--q-lagging)", improving: "var(--q-improving)" };
const QTEXT = {
  improving: "still behind the benchmark, but catching up. Where a new leader starts, and where many false starts die.",
  leading: "ahead of the benchmark and pulling away. The strong spot, for as long as the momentum holds.",
  weakening: "still ahead, but losing pace. Leaders often rest here, then either hook back up to leading or slide on to lagging.",
  lagging: "behind, and falling further. The weak spot, until the momentum turns up.",
};
const quad = (x, y) => x >= 100 ? (y >= 100 ? "leading" : "weakening") : (y >= 100 ? "improving" : "lagging");
const pcs = v => v == null ? "–" : `<span class="${sgn(v)}">${v > 0 ? "+" : ""}${v.toFixed(1)}%</span>`;

$("#shell").innerHTML = termBar({ here: "RRG", right: `<button class="tbtn" id="reloadBtn">Reload</button>` });
wireShell();
$("#reloadBtn").onclick = () => load();

function syncUrl() {
  const q = new URLSearchParams();
  if (ST.universe !== "book") q.set("u", ST.universe);
  if (R && R.bench && ST.bench) q.set("b", ST.bench);
  if (ST.freq === "d") q.set("f", "d");
  if (ST.tail !== 6) q.set("tail", ST.tail);
  if (ST.show === "all") q.set("show", "all");
  if (ST.pins.size) q.set("pin", [...ST.pins].join(","));
  history.replaceState(null, "", location.pathname + (q.toString() ? "?" + q : ""));
}

async function load() {
  stop();
  $("#app").innerHTML = `<div class="center">Reading three years of prices<span class="blink">_</span></div>`;
  const q = new URLSearchParams({ universe: ST.universe, freq: ST.freq });
  if (ST.bench) q.set("bench", ST.bench);
  try {
    const r = await fetch("/api/pf/rotation?" + q);
    R = r.ok ? await r.json() : { available: false, why: `HTTP ${r.status}` };
  } catch (e) { R = { available: false, why: String(e.message || e) }; }
  if (!R.available) {
    $("#app").innerHTML = `${controls()}<div class="panel"><div class="body"><div class="note"><b>No rotation graph</b>${esc(R.why || "no answer")}</div></div></div>`;
    wireControls();
    return;
  }
  ST.bench = R.bench;
  ST.at = R.dates.length - 1;
  scaffold();
}

/* ---------- which names are drawn ------------------------------------------ */
function placed() { return R.items.filter(i => i.q); }
function visible() {
  const all = placed();
  if (ST.universe !== "book" || ST.show === "all" || all.length <= TOP) return all;
  const top = new Set([...all].sort((a, b) => (b.weight || 0) - (a.weight || 0)).slice(0, TOP).map(i => i.t));
  return all.filter(i => top.has(i.t) || ST.pins.has(i.t));
}

/* ---------- page -------------------------------------------------------------- */
function controls() {
  const U = (R && R.universes) || { book: "Your holdings", sectors: "US sectors", markets: "Markets" };
  const B = (R && R.benches) || {};
  const seg = (id, opts, cur) => `<span class="grp" id="${id}">${opts.map(([v, l]) => `<button class="seg ${String(v) === String(cur) ? "on" : ""}" data-v="${v}">${l}</button>`).join("")}</span>`;
  return `<div class="panel" id="top"><div class="body">
    <div class="hdr"><h1>ROTATION</h1><span class="sub">which names lead the benchmark, which are fading, and which way each is turning</span></div>
    <div class="fbar" style="margin:0">
      ${seg("cU", Object.entries(U), ST.universe)}
      <span class="lbl" style="margin-left:.6rem">against</span>
      <select class="seg" id="cB">${Object.entries(B).map(([k, l]) => `<option value="${k}" ${k === ST.bench ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>
      ${seg("cF", [["w", "Weekly"], ["d", "Daily"]], ST.freq)}
      <span class="lbl" style="margin-left:.6rem">Tail</span>${seg("cT", [[4, "4"], [6, "6"], [8, "8"], [12, "12"]], ST.tail)}
      ${ST.universe === "book" && R && placed().length > TOP ? `<span class="lbl" style="margin-left:.6rem">Show</span>${seg("cS", [["top", `Largest ${TOP}`], ["all", `All ${placed().length}`]], ST.show)}` : ""}
    </div></div></div>`;
}

function wireControls() {
  const on = (id, fn) => $$(`#${id} .seg`).forEach(b => b.onclick = () => fn(b.dataset.v));
  on("cU", v => { ST.universe = v; ST.bench = null; ST.pins.clear(); syncUrl(); load(); });
  on("cF", v => { ST.freq = v; syncUrl(); load(); });
  on("cT", v => { ST.tail = +v; syncUrl(); $$("#cT .seg").forEach(x => x.classList.toggle("on", x.dataset.v === v)); draw(); });
  on("cS", v => { ST.show = v; syncUrl(); $$("#cS .seg").forEach(x => x.classList.toggle("on", x.dataset.v === v)); draw(); });
  const b = $("#cB");
  if (b) b.onchange = () => { ST.bench = b.value; syncUrl(); load(); };
}

function scaffold() {
  const per = R.per, n = R.dates.length;
  document.body.classList.toggle("rrg-wide", ST.wide);
  $("#app").innerHTML = `${controls()}
  <div class="rg">
    <div class="panel" id="graphPanel"><header><h2>Relative rotation</h2>
      <span class="hint">against ${esc(R.bench_label)} · ${R.freq === "w" ? "weekly" : "daily"} · in ${R.currency === "EUR" ? "euros" : "dollars"}</span>
      <span class="zbar"><span class="grp"><button class="seg" id="zIn" title="Zoom in on the middle (or Ctrl + mouse wheel where you point)">+</button><button class="seg" id="zOut" title="Zoom out">−</button><button class="seg" id="zFit" title="Show every name">Fit</button></span>
        <span class="grp"><button class="seg ${ST.wide ? "on" : ""}" id="zWide" title="The graph across the whole page, the text below it">Full width</button><button class="seg" id="zFull" title="Full screen; Esc to leave">Full screen</button></span></span></header>
      <div class="body">
        <div id="rrg"></div>
        <div class="play"><button class="seg" id="playBtn" title="Replay the last ${n} ${per}s">▶ Replay</button>
          <input type="range" id="scrub" min="${Math.min(ST.tail, n - 1)}" max="${n - 1}" value="${n - 1}" step="1" aria-label="Period shown">
          <span class="when" id="when"></span></div>
        <div class="src">Hover a name for its figures; click to keep it lit and read it on the right. Ctrl + wheel (or a pinch) zooms where you point, + zooms on the middle, where the names crowd. ${R.running ? `The last point is this week so far, to ${dmy(R.asof)}.` : `Prices to ${dmy(R.asof)}.`}</div>
      </div></div>
    <div class="stack">
      <div class="panel"><header><h2>What it says</h2></header>
        <div class="body"><ul class="say">${(R.say || []).map(s => `<li>${esc(s)}</li>`).join("") || `<li class="dim">Nothing placed yet.</li>`}</ul></div></div>
      <div class="panel"><header><h2>One name</h2><span class="hint">click a name</span></header>
        <div class="body" id="detail"></div></div>
      <div class="panel"><header><h2>What it tells you</h2></header>
        <div class="body">
          <div class="prose">It answers one question: <b>is this beating ${esc(R.bench_label)}, and is that getting better or worse?</b> Not whether it goes up. A share can rise 20% and still lag, if the benchmark rose 30%.</div>
          <div class="prose" style="margin-top:.4rem"><b>Across</b>, the trend: right of 100 it has been doing better than the benchmark lately. <b>Up</b>, the momentum: above 100 that relative trend is improving, below it is fading. So up comes before right: a name turns up first, and only later gets ahead.</div>
          <div class="qk" style="margin-top:.5rem">${["improving", "leading", "weakening", "lagging"].map(q =>
            `<span class="qb ${q}">${q}</span><span class="t">${QTEXT[q]}</span>`).join("")}</div>
          <ul class="say" style="margin-top:.5rem;font-size:.74rem">
            <li><b>The tail</b> is where it has been, one dot a ${per}. A long tail is a fast move; the head's direction says more than its spot.</li>
            <li><b>Clockwise</b> is the usual cycle: improving, leading, weakening, lagging, improving. A name that turns back (weakening to leading, say) is speeding up again.</li>
            <li><b>The distance from the centre</b> is how strong the reading is. Near the centre a name is just moving with the benchmark, and a crossing there means little.</li>
            <li><b>Your book:</b> the large holdings in leading are carrying it against the benchmark; the large ones in lagging are dragging it.</li></ul>
          <div class="src"><b>What it does not tell you.</b> Nothing about value or whether the price is fair. Nothing absolute: in a falling market a leader can still lose money. It is not a forecast: the panel below shows what followed past crossings, and it is a weak edge at best. The distance is in units of how far each name usually strays, so a utility and a chip maker can share one graph.</div>
        </div></div>
    </div>
  </div>
  <div class="panel"><header><h2>What followed, here</h2>
    <span class="hint">every crossing in these names over three years, and the return against the benchmark after it</span></header>
    <div class="body" id="follow"></div></div>
  <div class="panel"><header><h2>${R.universe === "book" ? "Your holdings" : esc(R.universes[R.universe])}</h2>
    <span class="hint">click a row to keep it lit on the graph · click a heading to sort</span></header>
    <div class="body"><div class="tscroll" id="tblBox"></div>
    <div class="src">Trend and momentum are an open approximation of Julius de Kempenaer's RS-Ratio and RS-Momentum, which are proprietary: relative strength is price divided by the benchmark; <b>trend</b> is how far it sits from its own ${R.params.n}-${per} average, in units of its usual spread over ${R.params.usual} ${per}s; <b>momentum</b> is the change in trend from one ${per} to the next, scaled the same way. Both are smoothed and centred on 100. Same construction and reading as the original, not the same numbers.</div></div></div>`;
  wireControls();
  $("#scrub").oninput = e => { stop(); ST.at = +e.target.value; draw(); };
  $("#playBtn").onclick = () => ST.timer ? stop() : play();
  draw(); table(); wireZoom(); followedPanel(); detail();
  if (!scaffold.once) { scaffold.once = true; window.addEventListener("resize", onResize); document.addEventListener("fullscreenchange", onFull); }
}
function onFull() {
  const b = $("#zFull"); if (b) b.textContent = document.fullscreenElement ? "Leave full screen" : "Full screen";
  setTimeout(() => { if (R && R.available && $("#rrg")) draw(); }, 80);
}
let _rz = null;
function onResize() { clearTimeout(_rz); _rz = setTimeout(() => { if (R && R.available && $("#rrg")) draw(); }, 150); }

/* ---------- replay ------------------------------------------------------------ */
function play() {
  const n = R.dates.length, lo = Math.min(ST.tail, n - 1);
  if (ST.at >= n - 1) ST.at = lo;
  $("#playBtn").textContent = "■ Stop";
  ST.timer = setInterval(() => {
    ST.at++;
    if (ST.at >= n - 1) { ST.at = n - 1; stop(); }
    draw();
  }, R.freq === "w" ? 420 : 220);
}
function stop() {
  if (ST.timer) clearInterval(ST.timer);
  ST.timer = null;
  const b = $("#playBtn"); if (b) b.textContent = "▶ Replay";
}

/* ---------- the graph ----------------------------------------------------------- */
function draw() {
  const host = $("#rrg"); if (!host) return;
  const items = visible(), n = R.dates.length, at = Math.max(0, Math.min(n - 1, ST.at ?? n - 1));
  const full = !!document.fullscreenElement;
  const W = Math.max(300, Math.round(host.clientWidth));
  const H = full ? Math.max(360, window.innerHeight - 130)
    : Math.round(Math.max(340, Math.min(ST.wide ? 860 : 640, W * (ST.wide ? 0.55 : 0.62))));
  const M = { l: 40, r: 16, t: 16, b: 26 }, pw = W - M.l - M.r, ph = H - M.t - M.b;

  // One scale for the whole replay, so the axes do not jump while it plays.
  // The zoom then shows a window of it, which cannot wander off the edge.
  let rx = 1, ry = 1;
  for (const it of items) for (const p of it.pts) if (p) { rx = Math.max(rx, Math.abs(p[0] - 100)); ry = Math.max(ry, Math.abs(p[1] - 100)); }
  rx *= 1.08; ry *= 1.08;
  const Z = ST.zoom, zx = rx / Z.k, zy = ry / Z.k;
  Z.cx = Math.max(100 - rx + zx, Math.min(100 + rx - zx, Z.cx));
  Z.cy = Math.max(100 - ry + zy, Math.min(100 + ry - zy, Z.cy));
  const X = v => M.l + (v - (Z.cx - zx)) / (2 * zx) * pw, Y = v => M.t + ((Z.cy + zy) - v) / (2 * zy) * ph;
  ST.view = { M, pw, ph, W, H, rx, ry, zx, zy };
  const cx = Math.max(M.l, Math.min(M.l + pw, X(100))), cy = Math.max(M.t, Math.min(M.t + ph, Y(100)));

  let g = "";
  const qr = (q, x, y, w, h, tx, ty, anchor) => w < 1 || h < 1 ? "" : `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${QCOL[q]}" opacity=".055"/>
    ${w > 90 && h > 26 ? `<text class="ql" x="${tx}" y="${ty}" text-anchor="${anchor}" fill="${QCOL[q]}">${q.toUpperCase()}</text>` : ""}`;
  g += qr("improving", M.l, M.t, cx - M.l, cy - M.t, M.l + 8, M.t + 16, "start");
  g += qr("leading", cx, M.t, M.l + pw - cx, cy - M.t, M.l + pw - 8, M.t + 16, "end");
  g += qr("lagging", M.l, cy, cx - M.l, M.t + ph - cy, M.l + 8, M.t + ph - 8, "start");
  g += qr("weakening", cx, cy, M.l + pw - cx, M.t + ph - cy, M.l + pw - 8, M.t + ph - 8, "end");
  for (const t of nice(Z.cx - zx, Z.cx + zx, 6).ticks) {
    if (t <= Z.cx - zx || t >= Z.cx + zx) continue;
    g += `<line class="${t === 100 ? "cl" : "gl"}" x1="${X(t).toFixed(1)}" x2="${X(t).toFixed(1)}" y1="${M.t}" y2="${M.t + ph}"/>
      <text class="ax" x="${X(t).toFixed(1)}" y="${H - 8}" text-anchor="middle">${t}</text>`;
  }
  for (const t of nice(Z.cy - zy, Z.cy + zy, 5).ticks) {
    if (t <= Z.cy - zy || t >= Z.cy + zy) continue;
    g += `<line class="${t === 100 ? "cl" : "gl"}" x1="${M.l}" x2="${M.l + pw}" y1="${Y(t).toFixed(1)}" y2="${Y(t).toFixed(1)}"/>
      <text class="ax" x="${M.l - 6}" y="${(Y(t) + 3).toFixed(1)}" text-anchor="end">${t}</text>`;
  }
  g += `<text class="ax" x="${M.l + pw}" y="${H - 8}" text-anchor="end">trend →</text><text class="ax" x="${M.l - 34}" y="${M.t + 4}">mom. ↑</text>`;

  // Tails, heads, then labels placed so they do not sit on each other.
  const heads = [];
  for (const it of items) {
    const tail = [];
    for (let k = Math.max(0, at - ST.tail); k <= at; k++) if (it.pts[k]) tail.push(it.pts[k]); else tail.length = 0;
    if (!tail.length || !it.pts[at]) continue;
    const head = tail[tail.length - 1], q = quad(head[0], head[1]), col = QCOL[q];
    let s = "";
    for (let k = 0; k < tail.length - 1; k++) {
      const o = (0.25 + 0.65 * (k + 1) / (tail.length - 1)).toFixed(2);
      s += `<line x1="${X(tail[k][0]).toFixed(1)}" y1="${Y(tail[k][1]).toFixed(1)}" x2="${X(tail[k + 1][0]).toFixed(1)}" y2="${Y(tail[k + 1][1]).toFixed(1)}" stroke="${col}" stroke-width="1.5" stroke-linecap="round" opacity="${o}"/>
        <circle cx="${X(tail[k][0]).toFixed(1)}" cy="${Y(tail[k][1]).toFixed(1)}" r="1.7" fill="${col}" opacity="${o}"/>`;
    }
    s += `<circle cx="${X(head[0]).toFixed(1)}" cy="${Y(head[1]).toFixed(1)}" r="4.4" fill="${col}" stroke="var(--surface)" stroke-width="1.5"/>`;
    heads.push({ it, s, x: X(head[0]), y: Y(head[1]), q, head });
  }
  heads.sort((a, b) => a.y - b.y);
  const boxes = [];
  for (const h of heads) {
    const w = h.it.label.length * 6.4 + 4, right = h.x + 8 + w <= M.l + pw;
    let lx = right ? h.x + 8 : h.x - 8 - w, ly = h.y + 3.5;
    for (let tries = 0; tries < 8 && boxes.some(b => Math.abs(b.y - ly) < 11 && lx < b.x + b.w && lx + w > b.x); tries++) ly += 11;
    boxes.push({ x: lx, y: ly, w });
    h.lx = right ? h.x + 8 : h.x - 8; h.ly = ly; h.anchor = right ? "start" : "end";
  }
  const lit = ST.hover ? new Set([ST.hover]) : ST.pins.size ? ST.pins : null;
  g += heads.map(h => `<g class="it ${lit ? (lit.has(h.it.t) ? "on" : "dimmed") : ""}" data-t="${esc(h.it.t)}">${h.s}
      <text class="nm" x="${h.lx.toFixed(1)}" y="${h.ly.toFixed(1)}" text-anchor="${h.anchor}">${esc(h.it.label)}</text></g>`).join("");

  const zoomed = Z.k > 1.001;
  host.classList.toggle("zoomed", zoomed);
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="Relative rotation graph against ${esc(R.bench_label)}">
    <defs><clipPath id="rrgClip"><rect x="${M.l}" y="${M.t}" width="${pw}" height="${ph}"/></clipPath></defs>
    ${g.replace(/<g class="it/, `<g clip-path="url(#rrgClip)"><g class="it`)}${heads.length ? "</g>" : ""}</svg><div class="rtip" style="display:none"></div>
    ${zoomed ? `<div class="znote">${Z.k.toFixed(1)}× · drag to move · double-click to see everything</div>` : ""}`;
  const tip = host.querySelector(".rtip");
  host.querySelectorAll("g.it").forEach(el => {
    const h = heads.find(x => x.it.t === el.dataset.t);
    el.addEventListener("mouseenter", () => {
      ST.hover = h.it.t; light();
      const p = h.head, prev = at > 0 && h.it.pts[at - 1];
      tip.innerHTML = `<b>${esc(h.it.label)}</b> <span class="qb ${h.q}">${h.q}</span><br>
        trend ${p[0].toFixed(2)} · momentum ${p[1].toFixed(2)}${prev ? `<br><span class="dim">a ${R.per} earlier ${prev[0].toFixed(2)} · ${prev[1].toFixed(2)}</span>` : ""}
        ${at === n - 1 && h.it.rel1 != null ? `<br>vs benchmark ${pcs(h.it.rel1)} in a month, ${pcs(h.it.rel3)} in three` : ""}
        ${h.it.weight != null ? `<br><span class="dim">${h.it.weight.toFixed(1)}% of your book</span>` : ""}`;
      tip.style.display = "";
      const r = host.getBoundingClientRect(), sx = h.x / W * r.width, tw = tip.offsetWidth;
      tip.style.left = Math.max(0, Math.min(r.width - tw, sx + (sx > r.width / 2 ? -tw - 12 : 12))) + "px";
      tip.style.top = Math.max(0, h.y / H * host.querySelector("svg").getBoundingClientRect().height - 10) + "px";
    });
    el.addEventListener("mouseleave", () => { ST.hover = null; light(); tip.style.display = "none"; });
    el.addEventListener("click", () => { if (!ST.dragged) pin(h.it.t); });
  });
  const sc = $("#scrub"); if (sc && +sc.value !== at) sc.value = at;
  $("#when").textContent = (R.freq === "w" ? "week to " : "") + dmy(R.dates[at]);
}

function light() {
  const lit = ST.hover ? new Set([ST.hover]) : ST.pins.size ? ST.pins : null;
  $$("#rrg g.it").forEach(el => {
    el.classList.toggle("on", !!lit && lit.has(el.dataset.t));
    el.classList.toggle("dimmed", !!lit && !lit.has(el.dataset.t));
  });
  $$("#tblBox tr[data-t]").forEach(tr => tr.classList.toggle("pin", ST.pins.has(tr.dataset.t)));
}

function pin(t) {
  ST.pins.has(t) ? ST.pins.delete(t) : ST.pins.add(t);
  ST.focus = ST.pins.has(t) ? t : null;
  syncUrl(); draw(); light(); detail();
}

/* ---------- zoom, pan, full width, full screen ------------------------------------ */
function zoomAt(f, px, py) {
  const v = ST.view, Z = ST.zoom;
  if (!v) return;
  const k = Math.max(1, Math.min(12, Z.k * f));
  if (px != null) {
    // keep the point under the pointer where it is
    const fx = (px - v.M.l) / v.pw, fy = (py - v.M.t) / v.ph;
    const dx = (Z.cx - v.zx) + fx * 2 * v.zx, dy = (Z.cy + v.zy) - fy * 2 * v.zy;
    const nzx = v.rx / k, nzy = v.ry / k;
    Z.cx = dx - fx * 2 * nzx + nzx;
    Z.cy = dy + fy * 2 * nzy - nzy;
  }
  Z.k = k;
  if (k === 1) { Z.cx = 100; Z.cy = 100; }
  draw();
}
function fit() { ST.zoom = { k: 1, cx: 100, cy: 100 }; draw(); }

function wireZoom() {
  const host = $("#rrg");
  const at = e => {
    const r = host.querySelector("svg").getBoundingClientRect(), v = ST.view;
    return [(e.clientX - r.left) * v.W / r.width, (e.clientY - r.top) * v.H / r.height];
  };
  // Ctrl + wheel, or a trackpad pinch, zooms; in full screen the wheel alone does
  host.addEventListener("wheel", e => {
    if (!e.ctrlKey && !e.metaKey && !document.fullscreenElement) return;
    e.preventDefault();
    const [x, y] = at(e);
    zoomAt(Math.pow(1.0018, -e.deltaY), x, y);
  }, { passive: false });
  let pan = null;
  host.addEventListener("mousedown", e => {
    ST.dragged = false;
    if (ST.zoom.k > 1.001 && e.button === 0) { pan = { x: e.clientX, y: e.clientY, cx: ST.zoom.cx, cy: ST.zoom.cy }; e.preventDefault(); }
  });
  window.addEventListener("mousemove", e => {
    if (!pan || !$("#rrg")) return;
    const r = host.querySelector("svg").getBoundingClientRect(), v = ST.view;
    const dx = (e.clientX - pan.x) * v.W / r.width, dy = (e.clientY - pan.y) * v.H / r.height;
    if (Math.abs(dx) + Math.abs(dy) > 3) ST.dragged = true;
    if (!ST.dragged) return;
    ST.zoom.cx = pan.cx - dx / v.pw * 2 * v.zx;
    ST.zoom.cy = pan.cy + dy / v.ph * 2 * v.zy;
    draw();
  });
  window.addEventListener("mouseup", () => { pan = null; setTimeout(() => { ST.dragged = false; }, 0); });
  host.addEventListener("dblclick", e => { e.preventDefault(); fit(); });
  $("#zIn").onclick = () => zoomAt(1.6);
  $("#zOut").onclick = () => zoomAt(1 / 1.6);
  $("#zFit").onclick = fit;
  $("#zWide").onclick = () => {
    ST.wide = !ST.wide;
    try { localStorage.setItem("rrg_wide", ST.wide ? "1" : "0"); } catch {}
    $("#zWide").classList.toggle("on", ST.wide);
    document.body.classList.toggle("rrg-wide", ST.wide);
    draw();
  };
  $("#zFull").onclick = () => {
    const panel = $("#graphPanel");
    if (document.fullscreenElement) document.exitFullscreen();
    else if (panel.requestFullscreen) panel.requestFullscreen().catch(() => {});
  };
}

/* ---------- what followed, and one name in depth ------------------------------------ */
function followedPanel() {
  const F = R.followed, hz = R.horizons || [], box = $("#follow");
  if (!box) return;
  if (!F || !hz.length) { box.innerHTML = `<div class="dim" style="font-size:.72rem">Not enough history to count crossings.</div>`; return; }
  const lab = h => (R.freq === "w" ? h <= 4 : h <= 21) ? "a month" : "a quarter";
  const cell = c => c ? `${pcs(c.avg)} <span class="dim">· median ${pcs(c.med)} · beat it ${c.beat}% of ${c.n}</span>` : `<span class="dim">no crossings</span>`;
  const med = (q, h) => F[q] && F[q][String(h)] ? F[q][String(h)].med : null;
  const h1 = hz[hz.length - 1];
  const up = [med("leading", h1), med("improving", h1)].filter(v => v != null), dn = [med("weakening", h1), med("lagging", h1)].filter(v => v != null);
  const mean = a => a.reduce((s, v) => s + v, 0) / a.length;
  const edge = up.length && dn.length ? mean(up) - mean(dn) : null;
  box.innerHTML = `<div class="tscroll"><table class="tbl"><thead><tr><th class="l">After crossing into</th>${hz.map(h => `<th class="l">${lab(h)} later, against the benchmark</th>`).join("")}</tr></thead><tbody>
    ${["improving", "leading", "weakening", "lagging"].map(q => `<tr><td class="l"><span class="qb ${q}">${q}</span></td>${hz.map(h => `<td class="l">${cell(F[q] && F[q][String(h)])}</td>`).join("")}</tr>`).join("")}
    </tbody></table></div>
    ${edge != null ? `<div class="prose" style="margin-top:.45rem">Over ${lab(h1)}, names that had just crossed into leading or improving did ${edge >= 0 ? "better" : "worse"} than names that had just crossed into weakening or lagging, by <b>${Math.abs(edge).toFixed(1)} points</b> at the median (the average is pulled about by a few big winners). ${Math.abs(edge) < 1 ? "That is too small to lean on." : edge > 0 ? "An edge, for these names in these years; small samples and one stretch of market, so hold it loosely." : "The graph would have misled you here: a crossing into the strong side was, if anything, a sign the run was late."}</div>` : ""}
    <div class="src">Every time a name ${R.universe === "book" ? "you hold today" : "in this set"} crossed from one quadrant into another${R.span ? `, ${dmy(R.span[0])} to ${dmy(R.span[1])}` : ""}, and what it did against ${esc(R.bench_label)} in the ${hz.map(h => `${h} ${R.per}s`).join(" and ")} that followed: the average, the median, how often it beat the benchmark, and how many crossings.
      ${R.universe === "book" ? "These are the shares you hold today, so the ones that did badly and were sold are missing: the record flatters. " : ""}Crossings close together overlap, so the counts overstate how much evidence there is.</div>`;
}

function detail() {
  const box = $("#detail");
  if (!box) return;
  const t = ST.focus || (ST.pins.size ? [...ST.pins][ST.pins.size - 1] : null);
  const it = t && R.items.find(i => i.t === t);
  if (!it || !it.q) { box.innerHTML = `<div class="dim" style="font-size:.72rem;line-height:1.5">Click a name on the graph or in the table: where it is, which way it is heading, how fast, and the path it took to get there.</div>`; return; }
  const m = it.motion || {}, hs = it.hist || {}, per = R.per;
  const strip = (hs.path || []).map(([q, k]) => `<span class="qs ${q}" style="flex:${k}" title="${q}, ${k} ${per}${k > 1 ? "s" : ""}">${k > 2 ? k : ""}</span>`).join("");
  const card = (k, v, t) => `<div class="dc"><div class="k">${k}</div><div class="v">${v}</div><div class="t">${t}</div></div>`;
  box.innerHTML = `<div class="dhd"><b>${esc(it.label)}</b> <span class="qb ${it.q}">${it.q}</span>${it.weight != null ? ` <span class="dim">${it.weight.toFixed(1)}% of your book</span>` : ""}</div>
    <div class="prose" style="margin:.35rem 0">${esc(it.read || "")}</div>
    ${strip ? `<div class="qstrip">${strip}</div><div class="src" style="margin-top:.15rem">Its last moves between quadrants, oldest on the left; the numbers are ${per}s.</div>` : ""}
    <div class="dgrid">
      ${card("Heading", m.heading == null ? "–" : `<span class="turn" style="transform:rotate(${-m.heading}deg)">→</span> ${m.heading}°`, "0° toward a stronger trend, 90° rising momentum")}
      ${card("Speed", m.speed == null ? "–" : m.speed.toFixed(2), `the average step over four ${per}s`)}
      ${card("From the centre", m.distance == null ? "–" : m.distance.toFixed(2), "under 1, it is moving with the benchmark")}
      ${card("Clockwise", m.clockwise == null ? "–" : m.clockwise + "%", "of its turns in the last year")}
    </div>
    ${hs.share ? `<div class="src" style="margin-top:.35rem">Time spent, the last ${R.dates.length} ${per}s: ${["leading", "weakening", "lagging", "improving"].map(q => `<span class="qb ${q}">${q} ${hs.share[q] || 0}%</span>`).join(" ")}</div>` : ""}
    ${it.rel1 != null ? `<div class="src">Against the benchmark: ${pcs(it.rel1)} in a month, ${pcs(it.rel3)} in three.${R.universe === "book" ? ` <a href="/t/${encodeURIComponent(it.t)}">Open ${esc(it.label)}</a>` : ""}</div>` : ""}`;
}

/* ---------- the table ------------------------------------------------------------ */
const QORD = { leading: 0, weakening: 1, lagging: 2, improving: 3 };
function table() {
  const book = R.universe === "book", per = R.per;
  const cols = [["label", book ? "Holding" : "Name", "l"], ...(book ? [["weight", "Weight"]] : []), ["q", "Quadrant", "l"],
    ["x", "Trend"], ["y", "Momentum"], ["turn", "Turning", "l"], ["speed", "Speed"], ["cw", "Clockwise"],
    ["since", `${per[0].toUpperCase() + per.slice(1)}s there`], ["rel1", "vs benchmark, 1M"], ["rel3", "3M"]];
  const k = ST.sort.k || (book ? "weight" : "x"), dir = ST.sort.dir;
  const val = (i, key) => key === "q" ? QORD[i.q] ?? 9 : key === "label" ? i.label.toLowerCase() : key === "turn" ? Math.atan2(i.dy || 0, i.dx || 0)
    : key === "speed" ? (i.motion ? i.motion.speed : null) : key === "cw" ? (i.motion ? i.motion.clockwise : null) : i[key];
  const rows = [...R.items].sort((a, b) => {
    const va = val(a, k), vb = val(b, k);
    if (va == null) return 1; if (vb == null) return -1;
    return (va > vb ? 1 : va < vb ? -1 : 0) * (k === "label" || k === "q" ? 1 : dir);
  });
  const turn = i => i.dx == null ? "" : `<span class="turn" style="transform:rotate(${(-Math.atan2(i.dy, i.dx) * 180 / Math.PI).toFixed(0)}deg)" title="trend ${i.dx > 0 ? "+" : ""}${i.dx}, momentum ${i.dy > 0 ? "+" : ""}${i.dy} this ${per}">→</span>`;
  $("#tblBox").innerHTML = `<table class="tbl"><thead><tr>${cols.map(([key, lab, cl]) =>
    `<th class="sort ${cl || ""}" data-k="${key}">${lab}${k === key ? (key === "label" || key === "q" ? "" : dir < 0 ? " ▾" : " ▴") : ""}</th>`).join("")}</tr></thead><tbody>
    ${rows.map(i => `<tr data-t="${esc(i.t)}" class="${ST.pins.has(i.t) ? "pin" : ""}">
      <td>${book ? `<a href="/t/${encodeURIComponent(i.t)}" title="${esc(i.name || "")}">${esc(i.label)}</a>${i.label.toUpperCase() === i.t.split(".")[0] ? "" : ` <span class="dim">${esc(i.t.split(".")[0])}</span>`}` : `${esc(i.label)} <span class="dim">${esc(i.t)}</span>`}</td>
      ${book ? `<td>${i.weight == null ? "–" : i.weight.toFixed(1) + "%"}</td>` : ""}
      ${i.q ? `<td class="l" title="${esc(i.read || "")}"><span class="qb ${i.q}">${i.q}</span>${i.was && i.was !== i.q ? ` <span class="dim" title="was ${i.was} a ${per} ago">new</span>` : ""}</td>
        <td>${i.x.toFixed(1)}</td><td>${i.y.toFixed(1)}</td><td class="l">${turn(i)}</td>
        <td title="the average step over the last four ${per}s">${i.motion ? i.motion.speed.toFixed(2) : "–"}</td>
        <td title="the share of its turns in the last year that went the usual way round">${i.motion && i.motion.clockwise != null ? i.motion.clockwise + "%" : "–"}</td>
        <td>${i.since}${i.since >= R.dates.length ? "+" : ""}</td>
        <td>${pcs(i.rel1)}</td><td>${pcs(i.rel3)}</td>`
      : `<td class="l dim" colspan="9">${esc(i.why || "not placed")}</td>`}</tr>`).join("")}</tbody></table>`;
  $$("#tblBox th.sort").forEach(th => th.onclick = () => {
    ST.sort = { k: th.dataset.k, dir: ST.sort.k === th.dataset.k ? -ST.sort.dir : -1 };
    table();
  });
  $$("#tblBox tr[data-t]").forEach(tr => {
    tr.onclick = e => { if (e.target.closest("a")) return; pin(tr.dataset.t); };
    tr.onmouseenter = () => { ST.hover = tr.dataset.t; light(); };
    tr.onmouseleave = () => { ST.hover = null; light(); };
  });
}

load();
