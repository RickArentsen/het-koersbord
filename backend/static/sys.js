/* ==========================================================================
   SYS — the terminal's own health: every background job's last success and
   last error against its own rhythm, the threads, the process, the database,
   the sources the pages report as unanswered. /api/pf/sys (pf/health.py).
   ========================================================================== */

let V = null, POLL = null;
$("#shell").innerHTML = termBar({ here: "SYS · THE TERMINAL'S HEALTH", right: `<button class="tbtn" id="rl" title="Read again">Refresh</button>` });
wireShell();
$("#rl").onclick = () => load();

const ago = ts => { if (!ts) return "never"; const s = Date.now() / 1000 - ts; return s < 90 ? "just now" : s < 5400 ? Math.round(s / 60) + " min ago"
  : s < 172800 ? Math.round(s / 3600) + " h ago" : Math.round(s / 86400) + " days ago"; };
const LAB = { ok: "ok", late: "late", failing: "failing", idle: "idle" };

async function load() {
  clearTimeout(POLL);
  try { const r = await fetch("/api/pf/sys"); V = await r.json(); if (!r.ok) throw new Error(V.detail || r.status); }
  catch (e) { $("#app").innerHTML = `<div class="note"><b>Could not load SYS</b>${esc(String(e.message || e))}</div>`; return; }
  render();
  POLL = setTimeout(load, 60000);
}

function render() {
  const c = V.counts || {}, P = V.process || {}, D = V.database || {}, S = V.sources || {};
  const order = { failing: 0, late: 1, ok: 2, idle: 3 };
  const jobs = (V.jobs || []).slice().sort((a, b) => (order[a.status] - order[b.status]) || (a.group || "").localeCompare(b.group || ""));
  const m = D.maintained || {};
  $("#app").innerHTML = `
  <div class="panel"><div class="body">
    <div class="fhead"><h1>The terminal's health</h1><span class="dim">build ${V.build} · read ${new Date(V.now * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" })}</span></div>
    <div class="tiles" style="margin:.6rem 0">
      ${tile("Jobs ok", `<span class="pos">${c.ok || 0}</span>`, null, `of ${jobs.length}`)}
      ${tile("Late", c.late ? `<span class="warnc">${c.late}</span>` : "0", null, "older than their rhythm")}
      ${tile("Failing", c.failing ? `<span class="neg">${c.failing}</span>` : "0", null, "a day beyond it: on the home screen")}
      ${tile("Up", P.uptime_h != null ? nf(P.uptime_h, 1) + " h" : "–", null, `since ${P.started ? new Date(P.started * 1000).toLocaleString("en-GB", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" }) : "–"}`)}
      ${tile("Memory", P.memory_mb != null ? nf(P.memory_mb, 0) + " MB" : "–", null, P.private_mb != null ? `${nf(P.private_mb, 0)} MB private` : "")}
      ${tile("Handles", P.handles != null ? nf(P.handles, 0) : "–", null, `${P.threads} threads · ${nf(P.cpu_s, 0)} s of CPU`)}
      ${tile("Database", D.size_mb != null ? nf(D.size_mb, 0) + " MB" : "–", null, `cache ${nf(D.cache_mb, 0)} MB in ${nf(D.cache_keys, 0)} keys`)}
      ${tile("Upkeep", D.maintained_at ? ago(D.maintained_at) : "not yet", null, D.maintained_at ? `${Object.values(m.pruned || {}).reduce((a, b) => a + b, 0)} copies pruned, ${nf(m.freed_pages || 0, 0)} pages freed` : "ten minutes after start, then daily")}
    </div>
    <table class="tbl"><thead><tr><th class="l">Job</th><th>State</th><th class="l">Rhythm</th><th>Last success</th><th>Late by</th><th class="l">Last error, or what it says</th></tr></thead><tbody>
      ${jobs.map(j => `<tr><td class="l"><span class="grp">${esc(j.group || "")}</span><br>${esc(j.name)}</td><td><span class="st ${j.status}">${LAB[j.status]}</span></td>
        <td class="what">${esc(j.every || "")}</td><td>${ago(j.last_ok)}</td><td>${j.late_h ? nf(j.late_h, 1) + " h" : "–"}</td>
        <td class="${j.error ? "err" : "what"}">${esc(j.error || j.detail || (j.status === "idle" ? "off by design" : ""))}</td></tr>`).join("")}
    </tbody></table>
    <div class="expl"><b>Late</b> is older than the job's own rhythm allows (a weekday job owes nothing for the weekend); <b>failing</b> is a day beyond that,
      and puts one line on the home screen. <b>Idle</b> jobs are off by design: Koers AI before its contest starts, POL before its first build.
      Everything here is read from what the jobs record; SYS fetches nothing.</div>
  </div></div>
  <div class="two even">
    <div class="panel"><header><h2>Threads</h2><span class="hint">the background loops that should be running</span></header><div class="body">
      <div class="thr">${(V.threads || []).map(t => `<span><i class="dot ${t.alive ? "on" : "off"}"></i>${esc(t.name)}</span>`).join("")}</div></div></div>
    <div class="panel"><header><h2>Sources the pages report</h2></header><div class="body">
      <div class="kv"><span>News</span><span>${esc(S.news_provider || "–")}${S.news_error ? ` · <span class="neg">${esc(S.news_error)}</span>` : ""}</span></div>
      <div class="kv"><span>Market rows without a price</span><span>${(S.market_missing || []).length ? esc(S.market_missing.join(", ")) : "none"}</span></div>
      <div class="kv"><span>Holdings without an earnings date</span><span>${(S.earnings_failed || []).length ? esc(S.earnings_failed.join(", ")) : "none"}</span></div>
      <div class="kv"><span>Database file</span><span>${nf(D.size_mb, 1)} MB · write-ahead log ${nf(D.wal_mb, 1)} MB · ${D.auto_vacuum === 2 ? "incremental vacuum" : "no vacuum"} · ${nf(D.free_pages, 0)} free pages</span></div>
      ${Object.entries(D.rows || {}).map(([k, n]) => `<div class="kv"><span class="dim">${esc(k)}</span><span>${nf(n, 0)} rows</span></div>`).join("")}
    </div></div>
  </div>`;
  wrapTables();
}

load();
