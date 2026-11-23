/* ==========================================================================
   Kit — formatting, controls and the line chart shared by the pages built
   after the home and portfolio screens (BOND, RRG, OPT, SPLC). Needs term.js
   for esc, dmy, sgn, $ and $$. The stylesheet is kit.css.

   Charts are plain SVG drawn here — one axis each, direct end labels, a
   crosshair that reads every line at once.
   ========================================================================== */

/* ---------- formatting ---------------------------------------------------- */
const yv = (v, d = 2) => v == null ? "–" : v.toFixed(d) + "%";
const bp = v => v == null ? "–" : (v > 0 ? "+" : "") + Math.round(v) + "bp";
const bpc = v => `<span class="${sgn(v)}">${bp(v)}</span>`;
const pp = (v, d = 2) => v == null ? "–" : (v > 0 ? "+" : "") + v.toFixed(d) + "pp";
const pc = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : "") + v.toFixed(d) + "%";
const pcc = (v, d = 1) => `<span class="${sgn(v)}">${pc(v, d)}</span>`;
const dm = d => { const m = String(d || "").match(/^\d{4}-(\d{2})-(\d{2})/); return m ? `${m[2]}/${m[1]}` : "–"; };
const mon = d => { if (!d) return "–"; const t = new Date(d.slice(0, 10) + "T00:00:00Z");
  return t.toLocaleString("en-GB", { month: "short", year: "numeric", timeZone: "UTC" }); };
const ord = n => { if (n == null) return "–"; const s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); };
const pctl = p => p == null ? "" : `${ord(p)} pct`;
const pbar = p => p == null ? "" : `<span class="pbar ${p >= 90 || p <= 10 ? "hi" : ""}" title="${ord(p)} percentile"><i style="width:${p}%"></i></span>`;
const rbar = r => r.range_pos == null ? "" :
  `<span class="rbar" title="52-week range ${yv(r.lo52)} – ${yv(r.hi52)}"><i style="left:${Math.max(0, Math.min(100, r.range_pos))}%"></i></span>`;
const COL = { s1: "var(--s1)", s2: "var(--s2)", s3: "var(--s3)", s4: "var(--s4)", ink: "var(--ink)", mid: "var(--ink-mid)" };

/* ==========================================================================
   charts
   ========================================================================== */
const CHARTS = new Set();

function nice(lo, hi, n = 5) {
  if (!(hi > lo)) { hi = lo + 1; lo = lo - 1; }
  const raw = (hi - lo) / n, mag = 10 ** Math.floor(Math.log10(raw)), f = raw / mag;
  const step = (f < 1.5 ? 1 : f < 3.5 ? 2 : f < 7.5 ? 5 : 10) * mag;
  const a = Math.floor(lo / step) * step, b = Math.ceil(hi / step) * step;
  const ticks = [];
  for (let v = a; v <= b + step / 2; v += step) ticks.push(+v.toFixed(10));
  return { lo: a, hi: b, ticks, step };
}

const MS = d => Date.parse(String(d).slice(0, 10) + "T00:00:00Z");

function timeTicks(x0, x1, maxN) {
  const days = (x1 - x0) / 864e5, out = [];
  const d0 = new Date(x0), d1 = new Date(x1);
  if (days > 365 * 2.5) {
    const yrs = days / 365, step = [1, 2, 5, 10, 20].find(s => yrs / s <= maxN) || 20;
    for (let y = Math.ceil(d0.getUTCFullYear() / step) * step; y <= d1.getUTCFullYear(); y += step) {
      const t = Date.UTC(y, 0, 1); if (t >= x0 && t <= x1) out.push([t, String(y)]);
    }
  } else if (days > 75) {
    const months = days / 30.4, step = [1, 2, 3, 6, 12].find(s => months / s <= maxN) || 12;
    let y = d0.getUTCFullYear(), m = d0.getUTCMonth() + 1;
    for (; Date.UTC(y, m, 1) <= x1; m++) {
      if (m > 11) { m = 0; y++; }
      const t = Date.UTC(y, m, 1);
      if (t > x1) break;
      if (m % step === 0) out.push([t, new Date(t).toLocaleString("en-GB", { month: "short", timeZone: "UTC" }) + (m === 0 || step >= 6 ? " " + String(y).slice(2) : "")]);
    }
  } else {
    const step = days > 35 ? 14 : days > 12 ? 7 : 2;
    for (let t = x0; t <= x1; t += step * 864e5) {
      const d = new Date(t); out.push([t, `${String(d.getUTCDate()).padStart(2, "0")}/${String(d.getUTCMonth() + 1).padStart(2, "0")}`]);
    }
  }
  return out;
}

/* o = { series:[{name, color, pts:[[x,y]], w, dash, hidden, dots}], xType:"time"|"cat", cats,
         height, yfmt, tfmt, zero, bands:[[d0,d1]], refs:[{y,label}], endLabels, empty } */
function lchart(host, o) {
  if (!host) return;
  host.__o = o;
  CHARTS.add(host);
  drawChart(host);
}

function drawChart(host) {
  const o = host.__o;
  if (!o) return;
  const W = Math.max(260, Math.round(host.clientWidth || 600));
  host.__w = W;
  const H = o.height || (W < 560 ? 210 : 270);
  const cat = o.xType === "cat";
  const ser = (o.series || []).filter(s => !s.hidden && s.pts && s.pts.length);
  if (!ser.length) { host.innerHTML = `<div class="lc-empty">${esc(o.empty || "No data for this range")}</div>`; return; }
  const S = ser.map(s => ({ ...s, xs: s.pts.map(p => cat ? p[0] : MS(p[0])), ys: s.pts.map(p => p[1]) }));
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const s of S) {
    for (let i = 0; i < s.xs.length; i++) {
      if (s.ys[i] == null) continue;
      x0 = Math.min(x0, s.xs[i]); x1 = Math.max(x1, s.xs[i]);
      y0 = Math.min(y0, s.ys[i]); y1 = Math.max(y1, s.ys[i]);
    }
  }
  if (cat) { x0 = 0; x1 = Math.max(1, (o.cats || []).length - 1); }
  for (const r of o.refs || []) { y0 = Math.min(y0, r.y); y1 = Math.max(y1, r.y); }
  if (o.zero) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }
  const pad = (y1 - y0) * 0.06 || 0.5;
  let lo = y0 - pad;
  if (y0 >= 0 && lo < 0) lo = 0;
  const yt = nice(lo, y1 + pad, H < 240 ? 4 : 5);
  const yfmt = o.yfmt || (v => v.toFixed(2));
  const lw = Math.max(...yt.ticks.map(t => yfmt(t).length)) * 6.4 + 10;
  const M = { l: Math.max(30, lw), r: o.endLabels ? 58 : 12, t: 8, b: 20 };
  const pw = W - M.l - M.r, ph = H - M.t - M.b;
  const X = x => M.l + (x1 === x0 ? pw / 2 : (x - x0) / (x1 - x0) * pw);
  const Y = y => M.t + (yt.hi - y) / (yt.hi - yt.lo) * ph;
  let g = "";
  for (const [a, b] of o.bands || []) {
    const ta = MS(a), tb = b ? MS(b) : x1;
    if (tb < x0 || ta > x1) continue;
    g += `<rect class="band" x="${X(Math.max(ta, x0)).toFixed(1)}" y="${M.t}" width="${(X(Math.min(tb, x1)) - X(Math.max(ta, x0))).toFixed(1)}" height="${ph}"/>`;
  }
  for (const t of yt.ticks) {
    const y = Y(t).toFixed(1);
    g += `<line class="gl" x1="${M.l}" x2="${M.l + pw}" y1="${y}" y2="${y}"/><text class="ax" x="${M.l - 6}" y="${(+y + 3).toFixed(1)}" text-anchor="end">${esc(yfmt(t))}</text>`;
  }
  if (yt.lo < 0 && yt.hi > 0) g += `<line class="zl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(0).toFixed(1)}" y2="${Y(0).toFixed(1)}"/>`;
  for (const r of o.refs || []) {
    const y = Y(r.y).toFixed(1);
    g += `<line x1="${M.l}" x2="${M.l + pw}" y1="${y}" y2="${y}" stroke="var(--warn)" stroke-dasharray="4 4" stroke-width="1" opacity=".7"/>`
      + (r.label ? `<text class="ax" x="${M.l + 4}" y="${(+y - 4).toFixed(1)}" style="fill:var(--warn)">${esc(r.label)}</text>` : "");
  }
  const xt = cat ? (o.cats || []).map((c, i) => [i, c]) : timeTicks(x0, x1, Math.max(3, Math.floor(pw / 70)));
  // Category labels are thinned by their own width: "10Y" fits where "19 Feb 27" does not.
  const lab = cat ? Math.max(...xt.map(t => String(t[1]).length), 1) * 6.2 + 8 : 0;
  const skip = cat ? Math.max(1, Math.ceil(lab / (pw / Math.max(1, xt.length)))) : 1;
  xt.forEach(([t, lab], i) => {
    if (i % skip) return;
    g += `<text class="ax" x="${X(t).toFixed(1)}" y="${H - 5}" text-anchor="middle">${esc(lab)}</text>`;
  });
  for (const s of S) {
    let d = "", pen = false;
    for (let i = 0; i < s.xs.length; i++) {
      if (s.ys[i] == null) { pen = false; continue; }
      d += (pen ? "L" : "M") + X(s.xs[i]).toFixed(1) + " " + Y(s.ys[i]).toFixed(1);
      pen = true;
    }
    g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${s.w || 1.5}" ${s.dash ? `stroke-dasharray="${s.dash}"` : ""} stroke-linejoin="round"/>`;
    if ((cat && S.length <= 6) || s.dots) for (let i = 0; i < s.xs.length; i++) if (s.ys[i] != null)
      g += `<circle cx="${X(s.xs[i]).toFixed(1)}" cy="${Y(s.ys[i]).toFixed(1)}" r="${s.w > 2 ? 2.6 : 1.8}" fill="${s.color}"/>`;
  }
  if (o.endLabels) {
    const labs = S.filter(s => !s.noEnd).map(s => {
      let i = s.ys.length - 1; while (i >= 0 && s.ys[i] == null) i--;
      return i < 0 ? null : { y: Y(s.ys[i]), v: s.ys[i], c: s.color, n: s.short || s.name };
    }).filter(Boolean).sort((a, b) => a.y - b.y);
    for (let i = 1; i < labs.length; i++) if (labs[i].y - labs[i - 1].y < 11) labs[i].y = labs[i - 1].y + 11;
    for (const l of labs) g += `<text class="el" x="${M.l + pw + 5}" y="${(l.y + 3).toFixed(1)}" fill="${l.c}">${esc((o.efmt || yfmt)(l.v))}</text>`;
  }
  g += `<line class="xh" id="" x1="0" x2="0" y1="${M.t}" y2="${M.t + ph}" style="display:none"/>`;
  g += S.map((s, i) => `<circle class="hd" data-i="${i}" r="3" fill="${s.color}" style="display:none"/>`).join("");
  g += `<rect class="hov" x="${M.l}" y="${M.t}" width="${pw}" height="${ph}"/>`;
  const leg = !o.noLegend && S.length > 1 ? `<div class="legend">${S.map(s =>
    `<span class="chip on" style="cursor:default"><i class="sw" style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div>` : "";
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(o.label || "chart")}">${g}</svg>${leg}<div class="lc-tip" style="display:none"></div>`;

  const svg = host.querySelector("svg"), tip = host.querySelector(".lc-tip");
  const xh = svg.querySelector(".xh"), dots = [...svg.querySelectorAll(".hd")];
  const tfmt = o.tfmt || yfmt;
  const near = (xs, x) => {
    let lo = 0, hi = xs.length - 1;
    while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] < x) lo = m; else hi = m; }
    return Math.abs(xs[lo] - x) <= Math.abs(xs[hi] - x) ? lo : hi;
  };
  const move = ev => {
    const r = svg.getBoundingClientRect();
    const px = (ev.clientX - r.left) * (W / r.width);
    const xv = x0 + (px - M.l) / pw * (x1 - x0);
    let hdr = "", rows = "", xs = null;
    S.forEach((s, i) => {
      const k = near(s.xs, xv);
      const ok2 = s.ys[k] != null && (cat ? Math.round(xv) === s.xs[k] : Math.abs(s.xs[k] - xv) < (x1 - x0) / 25);
      dots[i].style.display = ok2 ? "" : "none";
      if (!ok2) return;
      dots[i].setAttribute("cx", X(s.xs[k]).toFixed(1)); dots[i].setAttribute("cy", Y(s.ys[k]).toFixed(1));
      if (xs == null) { xs = s.xs[k]; hdr = cat ? (o.cats || [])[s.xs[k]] : dmy(s.pts[k][0]); }
      rows += `<div class="r"><span><i class="sw" style="background:${s.color}"></i>${esc(s.name)}${cat && s.pts[k][2] ? ` <span class="dim">${esc(s.pts[k][2])}</span>` : ""}</span><b>${esc(tfmt(s.ys[k]))}</b></div>`;
    });
    if (xs == null) { tip.style.display = "none"; xh.style.display = "none"; return; }
    xh.setAttribute("x1", X(xs).toFixed(1)); xh.setAttribute("x2", X(xs).toFixed(1)); xh.style.display = "";
    tip.innerHTML = `<div class="h">${esc(hdr)}</div>${rows}${o.tipExtra ? o.tipExtra(xs) : ""}`;
    tip.style.display = "";
    const hw = host.clientWidth, tw = tip.offsetWidth, sx = (X(xs) / W) * r.width;
    tip.style.left = Math.max(0, Math.min(hw - tw, sx + (sx > hw / 2 ? -tw - 12 : 12))) + "px";
    tip.style.top = "6px";
  };
  const hov = svg.querySelector(".hov");
  hov.addEventListener("mousemove", move);
  hov.addEventListener("mouseleave", () => { tip.style.display = "none"; xh.style.display = "none"; dots.forEach(d => d.style.display = "none"); });
}

let _rsz = null;
window.addEventListener("resize", () => {
  clearTimeout(_rsz);
  _rsz = setTimeout(() => {
    for (const h of CHARTS) {
      if (!h.isConnected) { CHARTS.delete(h); continue; }
      if (Math.round(h.clientWidth) !== h.__w) h.__draw ? h.__draw() : drawChart(h);
    }
  }, 150);
});

/* a scatter with its least-squares line */
function scatter(host, pts, o = {}) {
  if (!host) return;
  host.__draw = () => scatter(host, pts, o);
  CHARTS.add(host);
  const W = Math.max(260, Math.round(host.clientWidth || 400)); host.__w = W;
  const H = o.height || 240;
  if (!pts || pts.length < 10) { host.innerHTML = `<div class="lc-empty">Not enough days</div>`; return; }
  const xs = pts.map(p => p[0]), ys = pts.map(p => p[1]);
  // o.tight: the axes fit the data rather than reaching for zero (an exchange rate never gets there).
  const lo = a => o.tight ? Math.min(...a) : Math.min(...a, 0), hi = a => o.tight ? Math.max(...a) : Math.max(...a, 0);
  const xt = nice(lo(xs), hi(xs), 5), yt = nice(lo(ys), hi(ys), 5);
  const M = { l: 40, r: 10, t: 8, b: 30 }, pw = W - M.l - M.r, ph = H - M.t - M.b;
  const X = x => M.l + (x - xt.lo) / (xt.hi - xt.lo) * pw, Y = y => M.t + (yt.hi - y) / (yt.hi - yt.lo) * ph;
  let g = "";
  const yf = o.yfmt || (t => `${t}${o.yu || ""}`), xf = o.xfmt || (t => `${t > 0 ? "+" : ""}${t}${o.xu || ""}`);
  for (const t of yt.ticks) g += `<line class="gl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(t)}" y2="${Y(t)}"/><text class="ax" x="${M.l - 5}" y="${Y(t) + 3}" text-anchor="end">${yf(t)}</text>`;
  for (const t of xt.ticks) g += `<text class="ax" x="${X(t)}" y="${M.t + ph + 13}" text-anchor="middle">${xf(t)}</text>`;
  if (xt.lo <= 0 && xt.hi >= 0) g += `<line class="zl" x1="${X(0)}" x2="${X(0)}" y1="${M.t}" y2="${M.t + ph}"/>`;
  if (yt.lo <= 0 && yt.hi >= 0) g += `<line class="zl" x1="${M.l}" x2="${M.l + pw}" y1="${Y(0)}" y2="${Y(0)}"/>`;
  for (const [x, y] of pts) g += `<circle cx="${X(x).toFixed(1)}" cy="${Y(y).toFixed(1)}" r="2.2" fill="var(--s1)" opacity=".5"/>`;
  const n = xs.length, mx = xs.reduce((a, b) => a + b) / n, my = ys.reduce((a, b) => a + b) / n;
  let sxy = 0, sxx = 0; for (let i = 0; i < n; i++) { sxy += (xs[i] - mx) * (ys[i] - my); sxx += (xs[i] - mx) ** 2; }
  const b = sxx ? sxy / sxx : 0, a = my - b * mx;
  g += `<line x1="${X(xt.lo)}" x2="${X(xt.hi)}" y1="${Y(a + b * xt.lo)}" y2="${Y(a + b * xt.hi)}" stroke="var(--ink)" stroke-width="1.5"/>`;
  g += `<text class="ax" x="${M.l + pw}" y="${H - 2}" text-anchor="end">${esc(o.xl || "")}</text>`;
  g += `<text class="ax" x="${M.l + 4}" y="${M.t + 10}">${esc(o.yl || "")}</text>`;
  host.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="${esc(o.label || "scatter")}">${g}</svg>`;
  return b;
}

const RANGES = [["1M", 30], ["3M", 91], ["6M", 182], ["YTD", "ytd"], ["1Y", 365], ["2Y", 730],
  ["5Y", 1826], ["10Y", 3652], ["20Y", 7305], ["Max", "max"]];
function startOf(r, maxStart = "1990-01-01") {
  const t = new Date();
  if (r === "ytd") return `${t.getFullYear() - 1}-12-31`;
  if (r === "max") return maxStart;
  return new Date(t - r * 864e5).toISOString().slice(0, 10);
}
function rangeBar(id, cur, opts = RANGES) {
  return `<span class="grp" id="${id}">${opts.map(([l, v]) =>
    `<button class="seg ${String(v) === String(cur) ? "on" : ""}" data-r="${v}">${l}</button>`).join("")}</span>`;
}
function wireRange(id, fn) {
  $$(`#${id} .seg`).forEach(b => b.onclick = () => {
    $$(`#${id} .seg`).forEach(x => x.classList.toggle("on", x === b));
    fn(isNaN(+b.dataset.r) ? b.dataset.r : +b.dataset.r);
  });
}
function chips(id, list, on) {
  return `<span class="legend" id="${id}" style="margin:0">${list.map(s =>
    `<button class="chip ${on.includes(s.k) ? "on" : ""}" data-k="${s.k}"><i class="sw" style="background:${s.color}"></i>${esc(s.name)}</button>`).join("")}</span>`;
}
function wireChips(id, state, fn) {
  $$(`#${id} .chip`).forEach(b => b.onclick = () => {
    const k = b.dataset.k, i = state.indexOf(k);
    if (i >= 0) { if (state.length > 1) state.splice(i, 1); } else state.push(k);
    b.classList.toggle("on", state.includes(k));
    fn();
  });
}

/* Every table scrolls sideways inside its own box on a phone, rather than
   widening the page. */
function wrapTables() {
  for (const t of $$("table.tbl")) {
    if (t.parentElement.classList.contains("tscroll")) continue;
    const w = document.createElement("div");
    w.className = "tscroll";
    t.parentNode.insertBefore(w, t);
    w.appendChild(t);
  }
}

function tile(k, v, chg, sub, lead = false, title = "") {
  return `<div class="tile ${lead ? "lead" : ""}" title="${esc(title)}"><div class="k">${esc(k)}</div>
    <div class="v">${v}${chg != null ? `<small>${chg}</small>` : ""}</div><div class="s">${sub || ""}</div></div>`;
}
