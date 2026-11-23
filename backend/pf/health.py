"""
SYS — the terminal's own health (build 53).

Some twenty background jobs keep the terminal current, and most swallow their
errors on purpose: a source that fails must fail alone and not take a page
down with it. The cost is that a job can fail for days without anyone
noticing. This page lists every job with its last success, its last error and
how old its data is against how old it may be. The home screen gets one alert
when a job has failed, or not run, for a day beyond its own rhythm.

    jobs        each with its rhythm ("every ten minutes", "weekday evenings"),
                the last success, the last error, and a state:
                ok, late (older than its rhythm allows), failing (late by a
                day or more: this is what the home screen hears about), or
                idle (off by design: the contest not started, no key)
    threads     every background thread that should be alive, and whether it is
    process     how long the server has run, its CPU time, memory and handles
    database    the file, the cache inside it, the last daily maintenance
    sources     what the pages report as unanswered: market rows without a
                price, holdings without an earnings date, FLOW's sources

Everything is read from what the jobs already record; nothing is fetched.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from . import store

AMS = ZoneInfo("Europe/Amsterdam")
GRACE_H = 24            # "failing" is late by a day beyond the job's own rhythm


def now_ams() -> datetime:
    return datetime.now(AMS)


def _age(k: str) -> Optional[float]:
    a = store.cache_age(k)
    return None if a >= 1e8 else a


def _ts(iso_or_ts) -> Optional[float]:
    if iso_or_ts is None:
        return None
    if isinstance(iso_or_ts, (int, float)):
        return float(iso_or_ts)
    try:
        d = datetime.fromisoformat(str(iso_or_ts).replace("Z", "+00:00"))
        return (d if d.tzinfo else d.replace(tzinfo=AMS)).timestamp()
    except Exception:
        return None


def weekend_hours(since: float, now: Optional[float] = None) -> float:
    """Hours of Saturday and Sunday between `since` and now: a weekday job owes nothing for them."""
    now = now or time.time()
    h, t = 0.0, since
    while t < now:
        d = datetime.fromtimestamp(t, AMS)
        nxt = min(now, (d.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).timestamp())
        if d.weekday() >= 5:
            h += (nxt - t) / 3600
        t = nxt
    return h


def judge(job: dict, now: Optional[float] = None) -> dict:
    """ok, late, failing or idle, from the last success against the rhythm (+ a day's grace for failing)."""
    now = now or time.time()
    if job.get("idle"):
        return {**job, "status": "idle", "late_h": None}
    ok = job.get("last_ok")
    allow = float(job.get("every_h") or 24)
    if ok is None:
        st = "failing" if job.get("error") else "late"
        return {**job, "status": st, "late_h": None}
    age_h = (now - ok) / 3600
    if job.get("weekdays"):
        age_h -= weekend_hours(ok, now)
    late = age_h - allow
    st = "ok" if late <= 0 else ("failing" if late >= GRACE_H else "late")
    if st == "ok" and job.get("error") and (job.get("last_try") or 0) > ok:
        st = "late"                                  # the last attempt failed, the data is still within its rhythm
    return {**job, "status": st, "late_h": round(late, 1) if late > 0 else 0.0, "age_h": round((now - ok) / 3600, 1)}


def _safe(fn, key: str, name: str) -> dict:
    try:
        return fn()
    except Exception as e:
        return {"key": key, "name": name, "error": f"the check itself failed: {e.__class__.__name__}: {e}"[:200], "last_ok": None}


# --------------------------------------------------------------------------
# the jobs, each from what it already records
# --------------------------------------------------------------------------

def _brief_parts() -> list[dict]:
    out = []
    for k, name, every in (("news", "News", 2), ("market", "Market board", 2), ("rates", "Rates (FRED, ECB)", 12),
                           ("sentiment", "Fear & greed, the Fed, GPR", 12), ("geo", "Geopolitics", 12),
                           ("earnings", "Earnings week", 24), ("macro", "Macro week", 24)):
        v = store.cache_get(k) or {}
        a = _age(k)
        err = v.get("error") if isinstance(v, dict) else None
        out.append({"key": f"brief:{k}", "name": name, "group": "Home and markets", "thread": "pf-brief",
                    "every": f"every {every} hours or less" if every > 2 else "every few minutes",
                    "every_h": every, "last_ok": (time.time() - a) if a is not None and not err else None,
                    "last_try": (time.time() - a) if a is not None else None, "error": err})
    return out


def _morning() -> dict:
    from . import morning
    with store.conn() as c:
        r = c.execute("SELECT d, built_at FROM brief_day ORDER BY built_at DESC LIMIT 1").fetchone()
    j = next(iter(sorted((morning.JOBS or {}).items(), reverse=True)), (None, {}))[1] or {}
    return {"key": "morning", "name": "Morning brief", "group": "Briefs", "thread": "alrt-scheduler",
            "every": "weekdays at 06:30", "every_h": 26, "weekdays": True,
            "last_ok": r["built_at"] if r else None, "last_try": j.get("finished"),
            "error": j.get("error") if j.get("state") == "error" else None, "detail": f"latest for {r['d']}" if r else None}


def _watch() -> dict:
    from . import watch
    a = _age("wl:scan")
    j = watch.JOB or {}
    return {"key": "watch", "name": "Watchlist scan", "group": "Pages", "thread": "wl-loop", "every": "weekday evenings at 22:45",
            "every_h": 26, "weekdays": True, "last_ok": (time.time() - a) if a is not None else None,
            "last_try": j.get("finished"), "error": j.get("error"), "state": j.get("state")}


def _idx() -> list[dict]:
    from . import idx
    out = []
    for ix, cfg in idx.INDICES.items():
        v = store.cache_get(f"idx:view:{ix}") or {}
        j = idx._JOB.get(ix) or {}
        out.append({"key": f"idx:{ix}", "name": f"IDX: {cfg['name']}", "group": "Pages", "thread": "idx-loop",
                    "every": "weekday evenings after 22:30", "every_h": 26, "weekdays": True,
                    "last_ok": v.get("built_at"), "last_try": j.get("finished"),
                    "error": j.get("error") or ("; ".join(f"{k}: {e}" for k, e in (v.get("errors") or {}).items())[:200] or None),
                    "state": j.get("state")})
    return out


def _reader() -> dict:
    from . import reader
    with store.conn() as c:
        ok = c.execute("SELECT finished FROM read_scan WHERE state = 'done' ORDER BY id DESC LIMIT 1").fetchone()
        last = c.execute("SELECT finished, state, error FROM read_scan ORDER BY id DESC LIMIT 1").fetchone()
    j = reader.JOB or {}
    return {"key": "read", "name": "READ, the article writer", "group": "Claude", "thread": "read-scheduler",
            "every": "three scans a weekday, one at a weekend", "every_h": 30,
            "last_ok": _ts(ok["finished"]) if ok else None, "last_try": _ts(last["finished"]) if last else None,
            "error": (last["error"] if last and last["state"] == "error" else None) or (j.get("error") if j.get("state") == "error" else None)}


def _kai() -> dict:
    from . import koersai
    started = bool(store.get_setting("kai_start"))
    r = _one("SELECT ts FROM kai_decision ORDER BY id DESC LIMIT 1")
    j = koersai.JOB or {}
    return {"key": "kai", "name": "Koers AI", "group": "Claude", "thread": "kai-scheduler",
            "every": "a turn each Friday after the close", "every_h": 8 * 24, "idle": not started,
            "last_ok": _ts(r[0]) if r else None, "last_try": j.get("finished"),
            "error": j.get("error") if j.get("state") == "error" else None}


def _pol() -> dict:
    from . import pol
    st = pol.status()
    last = st.get("last") or {}
    done = st.get("done") or {}
    return {"key": "pol", "name": "POL, Congress's trades", "group": "Pages", "thread": "pol-scheduler",
            "every": "every night at 05:30", "every_h": 26, "idle": not done.get("first"),
            "last_ok": _ts(done.get("nightly") or done.get("first")), "last_try": _ts(last.get("at")),
            "error": None if last.get("ok", True) else (last.get("error") or "the last run failed")}


def _flow() -> list[dict]:
    from . import flow
    st = store.cache_get("flow:status") or {}
    every = {"cftc": 8 * 24, "ofr": 3 * 24, "dix": 30, "nyse": 130 * 24, "diary": 30, "pc": 30, "short": 30, "own": 8 * 24}
    wk = {"dix", "diary", "pc", "short"}
    out = []
    for k, label, _ in flow.STEPS:
        s = st.get(k) or {}
        out.append({"key": f"flow:{k}", "name": f"FLOW: {label}", "group": "FLOW", "thread": "flow-loop",
                    "every": "after New York's close" if k in wk else "as it is published",
                    "every_h": every.get(k, 30), "weekdays": k in wk,
                    "last_ok": s.get("last_ok"), "last_try": s.get("at"), "error": None if s.get("ok", True) else s.get("error")})
    return out


def _options() -> dict:
    from . import options
    j = options._JOB or {}
    a = _one("SELECT MAX(asof) FROM opt_day")
    errs = j.get("errors") or []
    return {"key": "options", "name": "Option chains", "group": "Pages", "thread": "opt-loop", "every": "once a New York session",
            "every_h": 30, "weekdays": True, "last_ok": a[0] if a and a[0] else None, "last_try": j.get("finished"),
            "error": None, "detail": f"{len(errs)} holding(s) not read last time" if errs else None}


def _euro() -> dict:
    a = _age("eur:view")
    v = store.cache_get("eur:view") or {}
    return {"key": "euro", "name": "The euro", "group": "Pages", "thread": "euro-warm", "every": "every two hours",
            "every_h": 4, "last_ok": (time.time() - a) if a is not None else None, "error": v.get("error")}


def _macro() -> dict:
    r = _one("SELECT MAX(updated) FROM mac_event")
    return {"key": "macro", "name": "Macro calendar", "group": "Pages", "thread": "mac-loop", "every": "every quarter of an hour",
            "every_h": 3, "last_ok": _ts(r[0]) if r and r[0] else None}


def _earnlive() -> dict:
    from . import earnlive
    s = earnlive.status()
    return {"key": "earnlive", "name": "Earnings watcher", "group": "Pages", "thread": "earnings-live",
            "every": "while a holding reports", "every_h": 26, "last_ok": _ts(s.get("last_schedule")),
            "error": s.get("error"), "detail": f"{s.get('watches')} watch(es) open"}


def _tickers() -> dict:
    from . import service
    miss = service.missing_tickers()
    return {"key": "tickers", "name": "Tickers for new holdings", "group": "Portfolio", "thread": "tickers",
            "every": "when a holding has none", "every_h": 24 * 7, "last_ok": time.time() if not miss else None,
            "error": f"no ticker yet for {len(miss)} holding(s): {', '.join(miss[:4])}" if miss else None}


def _maint() -> dict:
    import json
    v = store.get_setting("db_maintained")
    m = json.loads(v) if v else {}
    return {"key": "maintain", "name": "Database upkeep", "group": "Portfolio", "thread": "pf-maintain",
            "every": "daily: stale cache copies pruned, space given back", "every_h": 26,
            "last_ok": _ts_utc(m.get("at")), "detail": (f"{sum((m.get('pruned') or {}).values())} copies pruned, "
                                                      f"{m.get('freed_pages', 0)} pages freed") if m else None}


def _ts_utc(x) -> Optional[float]:
    """store's utcnow().timestamp() reads a UTC time as local: the same correction its own cache ages make."""
    if x is None:
        return None
    return time.time() - (datetime.utcnow().timestamp() - float(x))


def _one(sql: str, args=()):
    try:
        with store.conn() as c:
            return c.execute(sql, args).fetchone()
    except sqlite3.OperationalError:
        return None


CHECKS = [("brief", _brief_parts), ("morning", _morning), ("watch", _watch), ("idx", _idx), ("read", _reader), ("kai", _kai),
          ("pol", _pol), ("flow", _flow), ("options", _options), ("euro", _euro), ("macro", _macro),
          ("earnlive", _earnlive), ("tickers", _tickers), ("maintain", _maint)]
THREADS = ["pf-brief", "alrt-scheduler", "wl-loop", "idx-loop", "read-scheduler", "kai-scheduler", "pol-scheduler",
           "flow-loop", "opt-loop", "euro-warm", "mac-loop", "earnings-live", "tickers", "pf-maintain"]


def jobs(now: Optional[float] = None) -> list[dict]:
    out = []
    for key, fn in CHECKS:
        r = _safe(fn, key, key)
        for j in (r if isinstance(r, list) else [r]):
            out.append(judge(j, now))
    return out


# --------------------------------------------------------------------------
# the process, the database, the sources
# --------------------------------------------------------------------------

def process() -> dict:
    from . import STARTED
    out = {"pid": os.getpid(), "started": STARTED, "uptime_h": round((time.time() - STARTED) / 3600, 1),
           "cpu_s": round(time.process_time()), "threads": threading.active_count()}
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32")
        k32.GetCurrentProcess.restype = wintypes.HANDLE          # a pointer-sized pseudo-handle: an int would truncate it
        k32.GetProcessHandleCount.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        h = k32.GetCurrentProcess()
        n = wintypes.DWORD()
        if k32.GetProcessHandleCount(h, ctypes.byref(n)):
            out["handles"] = n.value

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        ps = ctypes.WinDLL("psapi")
        ps.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        if ps.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            out["memory_mb"] = round(pmc.WorkingSetSize / 1e6)
            out["private_mb"] = round(pmc.PagefileUsage / 1e6)
    except Exception:
        pass
    return out


_DB: dict = {"t": 0.0, "v": None}


def database() -> dict:
    """The file and the cache inside it; the cache's size costs a read of it, so it is kept ten minutes."""
    if _DB["v"] is not None and time.time() - _DB["t"] < 600:
        return _DB["v"]
    import json
    p = str(store.DB_PATH)
    out = {"path": p, "size_mb": round(os.path.getsize(p) / 1e6, 1) if os.path.exists(p) else None,
           "wal_mb": round(os.path.getsize(p + "-wal") / 1e6, 1) if os.path.exists(p + "-wal") else 0.0,
           "auto_vacuum": store.vacuum_mode()}
    with store.conn() as c:
        n, b = c.execute("SELECT COUNT(*), SUM(LENGTH(v)) FROM cache").fetchone()
        out.update(cache_keys=n, cache_mb=round((b or 0) / 1e6, 1),
                   free_pages=c.execute("PRAGMA freelist_count").fetchone()[0],
                   rows={t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                         for t in ("txn", "price", "news_log", "flow_short", "est_day", "opt_day", "brief_day", "read_article")})
    m = store.get_setting("db_maintained")
    out["maintained"] = json.loads(m) if m else None
    out["maintained_at"] = _ts_utc((out["maintained"] or {}).get("at"))
    _DB.update(t=time.time(), v=out)
    return out


def sources() -> dict:
    from . import brief, news
    mk = store.cache_get("market") or {}
    er = store.cache_get("earnings") or {}
    nw = store.cache_get("news") or {}
    return {"market_missing": mk.get("missing") or [], "earnings_failed": er.get("failed_symbols") or [],
            "news_provider": news.provider_name(), "news_error": nw.get("error"),
            "brief_thread": bool(brief.BRIEF._thread and brief.BRIEF._thread.is_alive())}


def threads() -> list[dict]:
    alive = {t.name for t in threading.enumerate()}
    return [{"name": n, "alive": n in alive} for n in THREADS]


def view() -> dict:
    from . import BUILD
    js = jobs()
    counts = {s: sum(1 for j in js if j["status"] == s) for s in ("ok", "late", "failing", "idle")}
    return {"build": BUILD, "now": time.time(), "jobs": js, "counts": counts, "threads": threads(),
            "process": process(), "database": database(), "sources": sources()}


_ALERT: dict = {"t": 0.0, "v": []}


def alerts() -> list[dict]:
    """One line on the home screen when any job is failing: late by a day beyond its rhythm. Kept five minutes."""
    if time.time() - _ALERT["t"] < 300:
        return _ALERT["v"]
    try:
        bad = [j for j in jobs() if j["status"] == "failing"]
    except Exception:
        bad = []
    out = []
    if bad:
        names = ", ".join(j["name"] for j in bad[:4]) + (f" and {len(bad) - 4} more" if len(bad) > 4 else "")
        first = next((j for j in bad if j.get("error")), bad[0])
        out.append({"level": "warn", "code": "SYS", "text": f"{len(bad)} background job{'s' if len(bad) != 1 else ''} failing: {names}",
                    "detail": (first.get("error") or f"{first['name']}: no success for {first.get('age_h', '?')} hours")[:160] + " · SYS",
                    "href": "/m/SYS"})
    _ALERT.update(t=time.time(), v=out)
    return out
