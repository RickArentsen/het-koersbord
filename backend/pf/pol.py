"""
POL — politicians' trades: what members of Congress buy and sell, from their own filings (build 49).

The data is the user's own tracker (RickArentsen/politician-trading-tracker),
copied into ``backend/ptt/`` and run as its own process (see ptt/VENDOR.md):

    House        the Clerk's periodic transaction reports, parsed from the PDFs
    Senate       the EFD portal's reports; scanned paper ones read by Windows
                 OCR and staged unreviewed, as the tracker does
    committees   seats point in time, from congress-legislators' history
    scoring      every purchase from the first open after it was FILED,
                 against SPY: what a reader could have done, not what the
                 member did; the member's own entry beside it
    strategy     every buy inside the buyer's committee remit, its curve
                 against the S&P 500 (the tracker's site, Trade of the House)

What this module adds is the terminal's side. A runner starts the tracker's
steps one at a time, never two at once (they share one SQLite file, and the
tracker says concurrent stages collide), and lets the Yahoo steps wait while
the watchlist's scan or the indices' build is reading. A nightly run at 05:30
Amsterdam does the tracker's daily scan, OCRs new scanned filings, re-reads
the committee seats once a month and rebuilds the terminal's tables
(ptt/terminal_export.py). The page reads those tables read-only, and so do
the stock page's Congress panel, the alerts and the morning brief: which
politicians traded what you hold, shares and turbos' underlyings alike.

The first build (every filing since 2020, some hours) is started by hand,
from the page or by POST /pol/build; the nightly run only follows a finished
first build.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from . import store

AMS = ZoneInfo("Europe/Amsterdam")
PTT = Path(__file__).resolve().parent.parent / "ptt"
DATA = PTT / "data"
DB = Path(os.environ.get("PF_POL_DB") or DATA / "tracker.db")      # PF_POL_DB: a copy, for a test server
STATUS = DATA / "pol_run.json"
LOG = DATA / "pol_run.log"
LOCK = DATA / "pol_run.lock"

RUN_AT = (5, 30)                 # after New York's evening and the watchlist's scan, before the morning brief
OCR_PER_RUN = 40                 # scanned filings read per nightly run; the first build reads them all
HISTORY_EVERY_DAYS = 30          # committee seats change slowly: re-read them monthly
MINE_DAYS = 365                  # the overlap looks back a year
LATEST_DAYS = 30
SCAN_WAIT_S = 3 * 3600           # a Yahoo step waits at most this long for the watchlist or the indices
LOG_MAX = 5_000_000
OK_CODES = {0, 10}               # daily_scan.py: 0 nothing new, 10 new tracked trades, 1 an error
REFUSED = 75                     # cli.py's sectors and market caps: Yahoo refused the session, nothing wrong recorded
REFUSED_WAIT_S = 20 * 60         # then wait, and ask again where it stopped
REFUSED_TRIES = 6                # two hours of refusals, and the run stops with that as its reason

# (label, command or a step of this module's own, reads Yahoo)
STEPS = {
    "first": [
        ("the roster and every House filing since 2020", ["src/cli.py", "ingest", "--from", "2020", "--to", "{year}"], False),
        ("committee seats, quarter by quarter since 2019", "history", False),
        ("every Senate filing since 2020", ["src/cli.py", "senate-ingest"], False),
        ("scanned filings, read by OCR", "ocr", False),
        ("prices", ["src/cli.py", "prices"], True),
        # market caps first: the sectors step reads its answers and asks Yahoo only for the rest
        ("market caps", ["src/cli.py", "fundamentals"], True),
        ("sectors", ["src/cli.py", "sectors"], True),
        ("the scores", ["src/cli.py", "backtest"], False),
        ("the grades", ["src/cli.py", "grade"], False),
        ("whether the grade predicts", ["src/cli.py", "validate"], False),
        ("the in-remit strategy", ["src/cli.py", "strategy"], False),
        ("the terminal's tables", ["terminal_export.py"], False),
    ],
    "nightly": [
        ("new filings, prices, scores and the strategy", ["scripts/daily_scan.py"], True),
        ("committee seats", "history", False),
        ("scanned filings, read by OCR", "ocr", False),
        ("the terminal's tables", ["terminal_export.py"], False),
    ],
}
BEST_EFFORT = {"history", "ocr"}     # a failure here is logged; the run goes on


def now_ams() -> datetime:
    return datetime.now(AMS)


# --------------------------------------------------------------------------
# the run: the tracker's steps, one process at a time
# --------------------------------------------------------------------------

def status() -> dict:
    try:
        return json.loads(STATUS.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(st: dict) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, default=str), encoding="utf-8")
    tmp.replace(STATUS)


def _alive(pid) -> bool:
    try:
        pid = int(pid)
    except Exception:
        return False
    if os.name == "nt":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259            # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def busy() -> Optional[dict]:
    """The lock of a run still going, in this process or another: the tracker's stages must never overlap."""
    try:
        lk = json.loads(LOCK.read_text(encoding="utf-8"))
    except Exception:
        return None
    return lk if _alive(lk.get("pid")) or _alive(lk.get("child")) else None


def _lock(kind: str, child=None) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(json.dumps({"pid": os.getpid(), "child": child, "kind": kind,
                                "started": now_ams().isoformat(timespec="seconds")}), encoding="utf-8")


def scan_busy() -> bool:
    """The watchlist's scan or an index build is reading Yahoo. Asked of the live server too, for a run outside it."""
    try:
        from . import idx, watch
        if watch.JOB.get("state") == "running" or any((v or {}).get("state") == "running" for v in idx._JOB.values()):
            return True
    except Exception:
        pass
    if os.environ.get("PF_POL_OUTSIDE"):
        try:
            from . import net
            port = os.environ.get("PF_PORT", "8090")
            return json.loads(net.get(f"http://127.0.0.1:{port}/api/pf/wl/job", timeout=5).decode("utf-8")).get("state") == "running"
        except Exception:
            return False
    return False


def _log(line: str) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > LOG_MAX:
        LOG.replace(LOG.with_suffix(".old.log"))
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"[{now_ams():%Y-%m-%d %H:%M:%S}] {line}\n")


def _exec(args: list, kind: str) -> int:
    cmd = [sys.executable, "-u", *[a.replace("{year}", str(now_ams().year)) for a in args]]
    _log("$ " + " ".join(cmd[2:]))
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    with open(LOG, "a", encoding="utf-8") as f:
        p = subprocess.Popen(cmd, cwd=str(PTT), stdout=f, stderr=subprocess.STDOUT, env=env,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _lock(kind, p.pid)
        rc = p.wait()
    _log(f"exit {rc}")
    return rc


def _ocr_pending() -> list[str]:
    """Scanned filings with no OCR yet and never tried: newest first."""
    c = _conn()
    if not c:
        return []
    try:
        if not _has(c, "filings"):
            return []
        done = {r["doc_id"] for r in c.execute("SELECT DISTINCT doc_id FROM ocr_transactions")} if _has(c, "ocr_transactions") else set()
        rows = c.execute("SELECT doc_id, filing_date FROM filings WHERE parse_status = 'no_text'").fetchall()
    finally:
        c.close()
    tried = set(status().get("ocr_tried") or [])
    return [r["doc_id"] for r in sorted(rows, key=lambda r: _iso(r["filing_date"]) or "", reverse=True)
            if r["doc_id"] not in done and r["doc_id"] not in tried]


def _step_ocr(kind: str) -> int:
    docs = _ocr_pending()
    if kind != "first":
        docs = docs[:OCR_PER_RUN]
    st = status()
    tried = list(st.get("ocr_tried") or [])
    bad = 0
    for i, d in enumerate(docs):
        JOB.update(detail=f"{i + 1} of {len(docs)}")
        if _exec(["src/cli.py", "ocr", "--store", "--doc", d], kind) != 0:
            bad += 1
        tried.append(d)
    st = status()
    st["ocr_tried"] = tried[-20000:]
    _save(st)
    return 0 if not bad else 1


def _step_history(kind: str) -> int:
    last = (status().get("done") or {}).get("history")
    if kind != "first" and last and datetime.fromisoformat(last) > now_ams() - timedelta(days=HISTORY_EVERY_DAYS):
        _log("committee seats read less than a month ago: skipped")
        return 0
    rc = _exec(["src/cli.py", "committee-history", "--quiet"], kind)
    if rc == 0:
        st = status()
        st.setdefault("done", {})["history"] = now_ams().isoformat(timespec="seconds")
        _save(st)
    return rc


JOB: dict = {"state": "idle"}
_RUN_LOCK = threading.Lock()


def _pub(j: dict) -> dict:
    return {k: j.get(k) for k in ("state", "kind", "step", "i", "n", "detail", "started", "finished", "error")}


def run(kind: str) -> dict:
    """Every step of a run, in order: the first build or the nightly one. Returns how it ended."""
    if busy():
        JOB.update(state="error", kind=kind, error="another run of the tracker is going", finished=time.time())
        return _pub(JOB)
    steps = STEPS[kind]
    JOB.clear()
    JOB.update(state="running", kind=kind, step=None, i=0, n=len(steps), started=time.time(), finished=None, error=None)
    _lock(kind)
    _log(f"=== {kind} run ===")
    failed = []
    # An interrupted first build carries on where it stopped: its steps take hours, and each
    # one already finished is in the database (a nightly run is short, and always runs whole).
    done_steps = set(status().get("first_steps") or []) if kind == "first" else set()
    try:
        for i, (label, what, yahoo) in enumerate(steps):
            JOB.update(step=label, i=i + 1, detail=None)
            if label in done_steps:
                _log(f"{label}: already done in this first build")
                continue
            if yahoo:
                t0 = time.time()
                while scan_busy() and time.time() - t0 < SCAN_WAIT_S:
                    JOB.update(detail="waiting for the watchlist's scan or the indices to finish with Yahoo")
                    time.sleep(60)
                JOB.update(detail=None)
            if what == "ocr":
                rc = _step_ocr(kind)
            elif what == "history":
                rc = _step_history(kind)
            else:
                rc = _exec(what, kind)
                tries = 0
                while yahoo and rc == REFUSED and tries < REFUSED_TRIES:
                    tries += 1
                    at = (now_ams() + timedelta(seconds=REFUSED_WAIT_S)).strftime("%H:%M")
                    JOB.update(detail=f"Yahoo is refusing requests: trying again at {at}")
                    _log(f"{label}: Yahoo is refusing requests; trying again at {at} ({tries}/{REFUSED_TRIES})")
                    time.sleep(REFUSED_WAIT_S)
                    JOB.update(detail=None)
                    rc = _exec(what, kind)
                if rc == REFUSED:
                    raise RuntimeError(f"{label}: Yahoo kept refusing requests; press Build later, "
                                       f"it carries on from here")
            if rc not in OK_CODES:
                failed.append(label)
                # `what` is a command (a list) or one of this module's own steps (a word):
                # only the words can be best effort. Testing a list against the set raised
                # "unhashable type: 'list'" on 4 November and hid the real failure.
                if not (isinstance(what, str) and what in BEST_EFFORT):
                    raise RuntimeError(f"{label}: exit {rc} (the log is ptt/data/pol_run.log)")
            if kind == "first":
                st = status()
                st["first_steps"] = sorted(set(st.get("first_steps") or []) | {label})
                _save(st)
        st = status()
        st.setdefault("done", {})[kind] = now_ams().isoformat(timespec="seconds")
        st["last"] = {"kind": kind, "ok": True, "at": st["done"][kind], "partial": failed}
        if kind == "first":
            st.pop("first_steps", None)
        _save(st)
        JOB.update(state="done")
    except Exception as e:
        JOB.update(state="error", error=f"{e.__class__.__name__}: {e}"[:300])
        st = status()
        st["last"] = {"kind": kind, "ok": False, "at": now_ams().isoformat(timespec="seconds"), "error": JOB["error"]}
        _save(st)
        _log(f"failed: {JOB['error']}")
    finally:
        JOB["finished"] = time.time()
        try:
            LOCK.unlink()
        except OSError:
            pass
    return _pub(JOB)


def start_run(kind: str) -> dict:
    with _RUN_LOCK:
        if JOB.get("state") == "running" or busy():
            return {**_pub(JOB), "state": "running"}
        JOB.update(state="running", kind=kind, started=time.time())
    threading.Thread(target=run, args=(kind,), daemon=True, name=f"pol-{kind}").start()
    return _pub(JOB)


def nightly_due(now: Optional[datetime] = None) -> bool:
    """A finished first build, and no nightly run since the last 05:30."""
    n = (now or now_ams()).astimezone(AMS)
    done = status().get("done") or {}
    if not done.get("first"):
        return False
    slot = datetime.combine(n.date(), dtime(*RUN_AT), AMS)
    if n < slot:
        slot -= timedelta(days=1)
    last = max(done.get("first"), done.get("nightly") or "")
    return datetime.fromisoformat(last) < slot


_THREAD: dict = {"t": None}


def _tick():
    if JOB.get("state") == "running" or busy():
        return
    if nightly_due() and not scan_busy():
        start_run("nightly")


def _loop():
    time.sleep(180)
    while True:
        try:
            _tick()
        except Exception:
            pass
        time.sleep(300)


def ensure():
    """The nightly run, started by the server only; idle until the first build is done."""
    t = _THREAD["t"]
    if t and t.is_alive():
        return
    t = threading.Thread(target=_loop, daemon=True, name="pol-scheduler")
    _THREAD["t"] = t
    t.start()


# --------------------------------------------------------------------------
# reading the tracker's database
# --------------------------------------------------------------------------

def _conn() -> Optional[sqlite3.Connection]:
    if not DB.exists():
        return None
    c = sqlite3.connect(f"{DB.as_uri()}?mode=ro", uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def _has(c, table: str) -> bool:
    return bool(c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _q(sql: str, args=()) -> list[dict]:
    c = _conn()
    if not c:
        return []
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    except sqlite3.OperationalError:
        return []
    finally:
        c.close()


def _meta(key: str):
    rows = _q("SELECT value FROM meta WHERE key = ?", (key,))
    try:
        return json.loads(rows[0]["value"]) if rows else None
    except Exception:
        return None


def _iso(v) -> Optional[str]:
    """The Clerk's M/D/YYYY, or ISO already."""
    if not v:
        return None
    v = str(v).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", v):
        return v[:10]
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", v)
    if not m:
        return None
    y = int(m.group(3))
    return f"{y + 2000 if y < 100 else y:04d}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"


def export_info() -> Optional[dict]:
    return _meta("pol_export")


_CACHE: dict = {"key": None}


def _cached(name: str, fn):
    """Parsed once per export: the strategy payload alone is some 440 KB of JSON."""
    key = (export_info() or {}).get("built_at")
    if _CACHE.get("key") != key:
        _CACHE.clear()
        _CACHE["key"] = key
    if name not in _CACHE:
        _CACHE[name] = fn()
    return _CACHE[name]


PARTY = {"Republican": "R", "Democrat": "D", "Democratic": "D", "Independent": "I"}


def _trade(r: dict) -> dict:
    lag = None
    try:
        lag = (date.fromisoformat(r["filed"]) - date.fromisoformat(r["traded"][:10])).days if r.get("filed") and r.get("traded") else None
    except ValueError:
        pass
    return {"id": r["id"], "doc": r["doc_id"], "member": r["member"], "bg": r["bioguide_id"], "chamber": r["chamber"],
            "party": PARTY.get(r.get("party") or "", (r.get("party") or "")[:1]), "seat": r.get("state_district"),
            "owner": r.get("owner"), "ticker": r.get("ticker"), "asset": r.get("asset_name"), "type": r.get("tx_type"),
            "option": (r.get("asset_type") or "").upper() == "OP", "traded": r.get("traded"), "filed": r.get("filed"),
            "lag": lag, "amount": r.get("amount_raw"), "mid": r.get("amount_mid"), "entry": r.get("entry_date"),
            "alpha": {h: r.get(f"alpha_{h}") for h in ("1m", "3m", "6m", "12m")}, "grade": r.get("grade"),
            "remit": [s for s in (r.get("remit") or "").split(",") if s],
            "sectors": [s for s in (r.get("sectors") or "").split(",") if s], "pdf": r.get("pdf_url")}


# --------------------------------------------------------------------------
# you: what you hold, as Congress's filings name it
# --------------------------------------------------------------------------

_MINE: dict = {"t": 0.0, "v": None}


def _norm(t: str) -> str:
    """The filings write BRK.B; Yahoo writes BRK-B."""
    return (t or "").strip().upper().replace("-", ".")


def _us_names(sym: str, uni: dict, alias_back: dict) -> set:
    """A held symbol's US tickers: itself if American, its US line from EDGAR, the watchlist's other listings."""
    out = set()
    if "." not in sym:
        out.add(sym)
    if sym in alias_back:
        out.add(alias_back[sym])
    out |= {x for x in (uni.get(sym) or {}).get("also") or [] if "." not in x and x[:1].isalpha()}
    return {_norm(x) for x in out}


def my_tickers() -> dict:
    """{US ticker: {held, name, how}} for every share you hold and every turbo's underlying (kept ten minutes)."""
    if _MINE["v"] is not None and time.time() - _MINE["t"] < 600:
        return _MINE["v"]
    out: dict = {}
    try:
        from . import earnings, service
        d = service.dashboard("SP500", light=True)
        alias_back = {v: k for k, v in (earnings.us_alias() or {}).items()}
        uni = {r["sym"]: r for r in (store.cache_get("wl:universe") or {}).get("rows") or []}
        for r in d.get("positions") or []:
            if r.get("excluded"):
                continue
            if r.get("kind") == "EQUITY" and r.get("ticker"):
                for u in _us_names(r["ticker"], uni, alias_back):
                    out.setdefault(u, {"held": r["ticker"], "name": r.get("name"), "how": "share"})
            elif r.get("kind") == "DERIVATIVE" and r.get("underlying_ticker"):
                out.setdefault(_norm(r["underlying_ticker"]), {"held": r["underlying_ticker"], "name": r.get("name"), "how": "turbo"})
    except Exception:
        pass
    _MINE.update(t=time.time(), v=out)
    return out


def _in(tickers) -> tuple[str, list]:
    t = sorted(tickers)
    return ",".join("?" * len(t)), t


def mine(days: int = MINE_DAYS) -> dict:
    """Every trade Congress filed in the last year in a stock you hold, and per ticker who bought and who sold."""
    held = my_tickers()
    if not held:
        return {"tickers": {}, "trades": [], "by": []}
    ph, args = _in(held)
    cut = (now_ams().date() - timedelta(days=days)).isoformat()
    rows = [_trade(r) for r in _q(f"SELECT * FROM pol_trade WHERE ticker IN ({ph}) AND filed >= ? ORDER BY filed DESC, id DESC",
                                  (*args, cut))]
    by: dict = {}
    for t in rows:
        e = by.setdefault(t["ticker"], {"ticker": t["ticker"], "held": held[t["ticker"]]["held"], "how": held[t["ticker"]]["how"],
                                        "buys": 0, "sells": 0, "buy_usd": 0.0, "sell_usd": 0.0, "members": set(), "last": t["filed"]})
        side = "buys" if t["type"] == "purchase" else "sells" if (t["type"] or "").startswith("sale") else None
        if side:
            e[side] += 1
            e["buy_usd" if side == "buys" else "sell_usd"] += t["mid"] or 0
        e["members"].add(t["member"])
    out = sorted(({**v, "members": sorted(v["members"])} for v in by.values()), key=lambda v: v["last"] or "", reverse=True)
    return {"tickers": held, "trades": rows[:400], "by": out}


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

def latest(days: int = LATEST_DAYS, limit: int = 400) -> list[dict]:
    cut = (now_ams().date() - timedelta(days=days)).isoformat()
    return [_trade(r) for r in _q("SELECT * FROM pol_trade WHERE filed >= ? ORDER BY filed DESC, id DESC LIMIT ?", (cut, limit))]


def filing_years() -> dict:
    """How many trades were filed in each year, newest year first."""
    return _cached("years", lambda: {r["y"]: r["n"] for r in _q(
        "SELECT substr(filed, 1, 4) AS y, COUNT(*) AS n FROM pol_trade WHERE filed IS NOT NULL GROUP BY y ORDER BY y DESC")})


def filings(year: Optional[int] = None) -> dict:
    """Every trade filed in one calendar year, newest first: this year's unless another is asked for.
    A year is some 3,700 (2026 to early November) to 7,700 trades (2025), 2 to 4.5 MB as JSON."""
    y = int(year or now_ams().year)
    read = lambda: [_trade(r) for r in _q("SELECT * FROM pol_trade WHERE filed >= ? AND filed < ? ORDER BY filed DESC, id DESC",
                                          (f"{y}-01-01", f"{y + 1}-01-01"))]
    # this year's comes with every page load, so it is kept per export; an older year is read when asked (about 0.1 s)
    return {"year": y, "trades": _cached(f"filings{y}", read) if y == now_ams().year else read()}


def _member(r: dict) -> dict:
    s = json.loads(r["stats"]) if r.get("stats") else {}
    keys = ("n_buys", "n_scored", "n_scored_12m", "hit_rate", "hit_rate_12m", "avg_alpha_3m", "avg_alpha_12m", "wt_alpha_12m",
            "median_alpha_12m", "alpha_12m_t", "total_alpha_3m_t", "avg_lag_days", "avg_member_alpha_3m", "lag_cost_3m",
            "est_volume", "n_options", "n_in_remit", "alpha_in_remit_12m", "alpha_out_remit_12m")
    return {"bg": r["bioguide_id"], "name": r["name"], "chamber": r.get("chamber"),
            "party": PARTY.get(r.get("party") or "", (r.get("party") or "")[:1]), "state": r.get("state"),
            "seat": r.get("state_district"), "former": bool(r.get("is_former")), "score": r.get("score"),
            "remit": [x for x in (r.get("remit") or "").split(",") if x], "tracked": bool(r.get("tracked")),
            "n_trades": r.get("n_trades"), "last_filed": r.get("last_filed"), "first": r.get("first_traded"),
            "last": r.get("last_traded"), **{k: s.get(k) for k in keys}}


def leaders() -> list[dict]:
    """Every sitting member with a scored record, best first by twelve-month alpha from the filing date."""
    rows = [_member(r) for r in _q("SELECT * FROM pol_member WHERE is_former = 0 AND stats IS NOT NULL")]
    return sorted(rows, key=lambda m: (m.get("avg_alpha_12m") is None, -(m.get("avg_alpha_12m") or 0)))


def _thin(c: dict, n: int = 520) -> dict:
    """A curve for the chart: every k-th day and the last, so a five-year line stays a few hundred points."""
    ds = c.get("dates") or []
    if not ds:
        return {}
    k = max(1, len(ds) // n)
    idx = list(range(0, len(ds), k)) + ([len(ds) - 1] if (len(ds) - 1) % k else [])
    return {"d": [ds[i] for i in idx], "s": [round(c["strategy"][i], 4) for i in idx], "b": [round(c["benchmark"][i], 4) for i in idx],
            **{k_: c.get(k_) for k_ in ("final_strategy", "final_benchmark", "cagr", "bench_cagr", "max_drawdown",
                                         "bench_max_drawdown", "n_trades", "start", "end", "years", "exposure")}}


def strategy() -> Optional[dict]:
    def build():
        p = _meta("strategy_v1")
        if not p:
            return None
        cv = p.get("curves") or {}
        return {"built_at": p.get("built_at"), "summary": p.get("summary"), "equal": _thin(cv.get("strategy") or {}),
                "size": _thin(cv.get("strategy_size") or {}), "basket": ((p.get("basket") or {}).get("rows") or [])[:25],
                "basket_members": ((p.get("basket") or {}).get("members") or [])[:10], "changes": p.get("changes"),
                "n_members": len(p.get("members") or []), "hold_days": p.get("hold_days")}
    return _cached("strategy", build)


def committees() -> list[dict]:
    def build():
        out = []
        for r in _q("SELECT row FROM pol_committee"):
            c = json.loads(r["row"])
            out.append({k: c.get(k) for k in ("committee_id", "name", "short_name", "chamber", "sensitivity", "sectors", "n_seats",
                                               "n_tracked", "n_decisions", "n_settled", "n_traders", "volume", "alpha_12m",
                                               "median_alpha_12m", "hit_rate", "top_tickers")})
        return sorted(out, key=lambda c: (c["alpha_12m"] is None, -(c["alpha_12m"] or 0)))
    return _cached("committees", build)


def validation() -> Optional[dict]:
    return _cached("validation", lambda: _meta("grade_validation_v1"))


def freshness() -> dict:
    info = export_info() or {}
    checked = (_q("SELECT MAX(ingested_at) AS t FROM filings") or [{}])[0].get("t")
    newest = info.get("newest_filed")
    days = (now_ams().date() - date.fromisoformat(newest)).days if newest else None
    return {"newest_filed": newest, "days_since": days, "last_checked": checked, "exported": info.get("built_at")}


def ocr_rows(limit: int = 60) -> list[dict]:
    return _q("SELECT * FROM pol_ocr ORDER BY filed DESC, id DESC LIMIT ?", (limit,))


def unread(year: Optional[int] = None) -> dict:
    """
    A year's scanned filings that neither the text reader nor the OCR could read:
    trades POL does not have, each with the Clerk's PDF to read by hand (build 52).
    On 8 November 2026 that was 47 of the year's 56 scanned House filings.
    """
    y = int(year or now_ams().year)

    def read():
        rows = _q("SELECT f.doc_id, f.member_name, f.filing_date, f.pdf_url FROM filings f WHERE f.parse_status = 'no_text' "
                  "AND f.year = ? AND NOT EXISTS (SELECT 1 FROM ocr_transactions o WHERE o.doc_id = f.doc_id)", (y,))
        out = [{"doc": r["doc_id"], "member": r["member_name"], "filed": _iso(r["filing_date"]), "url": r["pdf_url"]} for r in rows]
        return sorted(out, key=lambda x: (x["filed"] or "", x["doc"]), reverse=True)
    return {"year": y, "filings": _cached(f"unread{y}", read)}


def overview() -> dict:
    info = export_info()
    st = status()
    out = {"built": bool(info), "run": {**_pub(JOB), "lock": busy(), "last": st.get("last"), "done": st.get("done")},
           "export": info, "next": "every night at 05:30 Amsterdam"}
    if not info:
        return out
    out.update(freshness=freshness(), filings=filings(), years=filing_years(), mine=mine(), leaders=leaders(), strategy=strategy(),
               committees=committees(), validation=validation(), ocr=ocr_rows(), unread=unread(),
               counts={"trades": info.get("trades"), "members": info.get("members"), "in_remit": info.get("in_remit"),
                       "ocr": info.get("ocr"), "committees": info.get("committees"),
                       # the Senate's portal refuses some networks outright (this laptop's on 4 November, a browser too)
                       "senate": (_q("SELECT COUNT(*) AS n FROM pol_trade WHERE chamber = 'senate'") or [{"n": 0}])[0]["n"]})
    return out


def member(bg: str) -> Optional[dict]:
    rows = _q("SELECT * FROM pol_member WHERE bioguide_id = ?", (bg,))
    if not rows:
        return None
    m = _member(rows[0])
    m["committees"] = json.loads(rows[0]["committees"] or "[]")
    trades = [_trade(r) for r in _q("SELECT * FROM pol_trade WHERE bioguide_id = ? ORDER BY filed DESC, id DESC LIMIT 600", (bg,))]
    by: dict = {}
    for t in trades:
        if t["type"] != "purchase":
            continue
        for s in t["sectors"] or ["unclassified"]:
            e = by.setdefault(s, {"sector": s, "n": 0, "usd": 0.0, "in_remit": s in m["remit"]})
            e["n"] += 1
            e["usd"] += t["mid"] or 0
    m.update(trades=trades, sectors=sorted(by.values(), key=lambda e: -e["usd"]), held=sorted(set(my_tickers()) & {t["ticker"] for t in trades}))
    return m


def committee(cid: str) -> Optional[dict]:
    c = next((x for x in committees() if x["committee_id"] == cid), None)
    if not c:
        return None
    seated = [_member(r) | {"title": next((s.get("title") for s in json.loads(r["committees"] or "[]") if s.get("committee_id") == cid), None)}
              for r in _q("SELECT * FROM pol_member WHERE committees LIKE ?", (f'%"{cid}"%',))]
    raw = c.get("sectors") or []                       # committees.stats() parses them into a list
    secs = set(raw if isinstance(raw, list) else str(raw).split(",")) - {""}
    ids = [m["bg"] for m in seated]
    trades = []
    if ids and secs:
        ph, args = _in(ids)
        for r in _q(f"SELECT * FROM pol_trade WHERE bioguide_id IN ({ph}) AND remit != '' ORDER BY filed DESC LIMIT 400", args):
            t = _trade(r)
            if secs & set(t["remit"]):
                trades.append(t)
    return {**c, "members": sorted(seated, key=lambda m: m["name"] or ""), "trades": trades[:150]}


def ticker(sym: str) -> dict:
    """Congress's trades in one company, for the stock page: its US tickers, whatever line you look at."""
    from . import earnings
    uni = {r["sym"]: r for r in (store.cache_get("wl:universe") or {}).get("rows") or []}
    alias_back = {v: k for k, v in (earnings.us_alias() or {}).items()}
    names = _us_names(sym.upper(), uni, alias_back) or {_norm(sym)}
    ph, args = _in(names)
    rows = [_trade(r) for r in _q(f"SELECT * FROM pol_trade WHERE ticker IN ({ph}) ORDER BY filed DESC, id DESC LIMIT 300", args)]
    year = (now_ams().date() - timedelta(days=365)).isoformat()
    recent = [t for t in rows if (t["filed"] or "") >= year]
    return {"built": bool(export_info()), "tickers": sorted(names), "trades": rows,
            "year": {"buys": sum(1 for t in recent if t["type"] == "purchase"),
                     "sells": sum(1 for t in recent if (t["type"] or "").startswith("sale")),
                     "members": len({t["member"] for t in recent})}}


# --------------------------------------------------------------------------
# the rest of the terminal: alerts and the morning brief
# --------------------------------------------------------------------------

def _who(t: dict) -> str:
    return f"{t['member']} ({t['party']}{'-' + t['seat'][:2] if t.get('seat') else ''})"


def alerts(days: int = 3) -> list[dict]:
    """A purchase in a stock you hold, filed in the last few days."""
    if not export_info():
        return []
    cut = (now_ams().date() - timedelta(days=days)).isoformat()
    buys = [t for t in mine()["trades"] if t["type"] == "purchase" and (t["filed"] or "") >= cut]
    return [{"level": "info", "code": "POL", "text": f"{_who(t)} bought {t['ticker']}",
             "detail": f"{t['amount']} · traded {t['traded']}, filed {t['filed']}" + (" · inside their committee's remit" if t["remit"] else "")
                       + " · POL", "href": f"/m/POL#t={t['ticker']}"} for t in buys][:3]


def brief_line(hours: int = 30) -> Optional[str]:
    if not export_info():
        return None
    cut = (now_ams() - timedelta(hours=hours)).date().isoformat()
    new = [t for t in latest(days=3) if (t["filed"] or "") >= cut]
    if not new:
        return None
    held = set(my_tickers())
    yours = [t for t in new if t["ticker"] in held]
    s = f"Congress: {len(new)} trade{'s' if len(new) != 1 else ''} disclosed since yesterday"
    if yours:
        s += "; in your holdings: " + ", ".join(f"{t['member']} {'bought' if t['type'] == 'purchase' else 'sold'} {t['ticker']}"
                                                 for t in yours[:4])
    return s
