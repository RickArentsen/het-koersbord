"""
The earnings moment: watching for the numbers, and reading them the second
they land.

A background thread keeps a watch on every report due today from your
holdings and watched names — and on anything you press "Watch live" on. From
a quarter of an hour before the expected time it asks three places, on three
clocks:

    EDGAR      every 10 s   the company's filing list (data.sec.gov, which the
                            SEC updates as filings are disseminated). An 8-K
                            with item 2.02, or a foreign filer's 6-K, is the
                            release; it is read and compared with the
                            consensus snapshot at once.
    the wire   every 30 s   Yahoo's news for the symbol: the press release
                            often shows up there a minute or two before EDGAR.
    the price  every 15 s   one-minute bars including pre- and after-market,
                            so the reaction is on the page next to the numbers.

The SEC allows ten requests a second; five simultaneous watches use one every
two seconds. A watch ends an hour after the numbers are found, or when the
window closes without them (the company moved the date, or files late).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import store

EDGAR_EVERY = 10
WIRE_EVERY = 30
PRICE_EVERY = 15
SCHEDULE_EVERY = 300
KEEP_AFTER_FOUND = 3600

_LOCK = threading.Lock()
WATCHES: dict = {}          # "SYM:date" -> state
_THREAD = {"t": None, "stop": threading.Event(), "last_schedule": 0.0, "err": None}


def _now() -> float:
    return time.time()


def _iso(ts: Optional[float]) -> Optional[str]:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds") if ts else None


def _key(sym: str, d: str) -> str:
    return f"{sym}:{d}"


def _log(w: dict, msg: str):
    w["log"].append({"t": _now(), "msg": msg})
    w["log"] = w["log"][-40:]


# --------------------------------------------------------------------------
# public
# --------------------------------------------------------------------------

def state(sym: str, d: str) -> Optional[dict]:
    """The watch on (sym, d): live if running, else what was found and kept."""
    with _LOCK:
        w = WATCHES.get(_key(sym, d))
        if w:
            return _public(w)
    got = store.cache_get(f"ern:live:{sym}:{d}")
    if not (got and got.get("found")):
        return None
    # Numbers read by an older build's parser are read again from the release, which a
    # newer build re-reads too: NVIDIA's EPS of 17 November 2026 was its revenue (build 57).
    from . import BUILD
    if got.get("_build") != BUILD and got.get("numbers"):
        try:
            fresh = numbers(sym, d, got.get("name") or got.get("symbol"), _now())
            fresh["read_s"] = got["numbers"].get("read_s")
            got = {k: v for k, v in got.items() if not k.startswith("_")}
            got["numbers"] = fresh
            store.cache_put(f"ern:live:{sym}:{d}", got)
        except Exception:
            pass
    return got


def _public(w: dict) -> dict:
    out = {k: v for k, v in w.items() if not k.startswith("_")}
    out["age_s"] = round(_now() - (w.get("last_edgar") or w["started"]))
    out["active"] = w["status"] in ("waiting", "found")
    return out


def active() -> list[dict]:
    ensure()
    with _LOCK:
        return [_public(w) for w in WATCHES.values()]


def start(sym: str, d: str, name: Optional[str] = None, when: Optional[str] = None,
          ts: Optional[float] = None, manual: bool = True, hours: float = 4) -> dict:
    ensure()
    k = _key(sym, d)
    with _LOCK:
        w = WATCHES.get(k)
        if w and w["status"] in ("waiting", "found"):
            w["until"] = max(w["until"], _now() + hours * 3600)
            w["manual"] = w["manual"] or manual
            return _public(w)
        w = {"symbol": sym, "date": d, "name": name or sym, "when": when, "expected_ts": ts,
             "manual": manual, "started": _now(), "until": _now() + hours * 3600,
             "status": "waiting", "checks": 0, "last_edgar": None, "last_wire": None, "last_price": None,
             "found": None, "wire": None, "price": None, "numbers": None, "error": None, "log": [],
             "_cik": None, "_seen": None}
        _log(w, "watch started" + (" by you" if manual else " (on your holdings' calendar)"))
        WATCHES[k] = w
        return _public(w)


def stop(sym: str, d: str) -> bool:
    with _LOCK:
        w = WATCHES.pop(_key(sym, d), None)
    return w is not None


def ensure():
    """Start the watcher thread if it is not running."""
    t = _THREAD["t"]
    if t and t.is_alive():
        return
    _THREAD["stop"].clear()
    t = threading.Thread(target=_loop, name="earnings-live", daemon=True)
    _THREAD["t"] = t
    t.start()


def status() -> dict:
    t = _THREAD["t"]
    return {"running": bool(t and t.is_alive()), "watches": len(WATCHES),
            "last_schedule": _iso(_THREAD["last_schedule"]), "error": _THREAD["err"]}


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

def _loop():
    while not _THREAD["stop"].is_set():
        try:
            if _now() - _THREAD["last_schedule"] > SCHEDULE_EVERY:
                _THREAD["last_schedule"] = _now()
                _schedule()
            with _LOCK:
                ws = list(WATCHES.values())
            for w in ws:
                _tick(w)
            _THREAD["err"] = None
        except Exception as e:                          # never let the thread die
            _THREAD["err"] = f"{e.__class__.__name__}: {e}"[:200]
        _THREAD["stop"].wait(2)


def _window(d: str, when: Optional[str], ts: Optional[float]) -> tuple[float, float]:
    """When to watch, as unix times: around the exact time, or the usual session edges."""
    from .earnings import ET
    base = datetime(*map(int, d.split("-")), tzinfo=ET)
    if ts:
        return ts - 15 * 60, ts + 3 * 3600
    if when == "bmo":
        return (base + timedelta(hours=5, minutes=45)).timestamp(), (base + timedelta(hours=10, minutes=30)).timestamp()
    if when == "amc":
        return (base + timedelta(hours=15, minutes=45)).timestamp(), (base + timedelta(hours=19, minutes=30)).timestamp()
    return (base + timedelta(hours=5, minutes=45)).timestamp(), (base + timedelta(hours=19, minutes=30)).timestamp()


def _sweep():
    """
    Freeze the consensus of every report in the next three days, whether or
    not anyone opens the page: once a company has reported, the revenue
    consensus it was judged against is gone from every free source.
    Also keeps the season scoreboard warm.
    """
    from . import earnings as E
    for u in E.upcoming_mine(14):
        try:
            if u["days"] <= 3:
                E.take_snapshot(u["sym"], u["date"], force=True)
            # Warms what the event page reads (last quarter's release and
            # call, the segments) so opening it is quick.
            if u["held"]:
                E.part(u["sym"], "expect", u["date"])
        except Exception:
            pass
    try:
        E.season()
    except Exception:
        pass


def _schedule():
    """Today's reports from what you hold and watch get a watch when their window opens."""
    from . import earnings as E
    if _now() - _THREAD.get("last_sweep", 0) > 3 * 3600:
        _THREAD["last_sweep"] = _now()
        threading.Thread(target=_sweep, daemon=True, name="earnings-sweep").start()
    t = E.today_et().isoformat()
    try:
        wk = E.week(t, "mine")
    except Exception:
        return
    for day in wk["days"]:
        if day["date"] != t:
            continue
        for e in day["events"]:
            if e.get("status") == "reported":
                continue
            exact = E.event_ts(t, e.get("when"), e.get("time_et")) if e.get("time_et") else None
            a, b = _window(t, e.get("when"), exact)
            if a <= _now() <= b and _key(e["sym"], t) not in WATCHES:
                if store.cache_get(f"ern:live:{e['sym']}:{t}"):
                    continue
                start(e["sym"], t, e.get("name"), e.get("when"), exact, manual=False,
                      hours=max(0.5, (b - _now()) / 3600))
                # Freeze the expectation now, if it was not already.
                try:
                    E.take_snapshot(e["sym"], t, force=True)
                except Exception:
                    pass


def _tick(w: dict):
    now = _now()
    if w["status"] == "found" and now - w["found"]["t"] > KEEP_AFTER_FOUND:
        w["status"] = "done"
    if w["status"] == "waiting" and now > w["until"]:
        w["status"] = "expired"
        _log(w, "window closed without a filing")
    if w["status"] not in ("waiting", "found"):
        with _LOCK:
            WATCHES.pop(_key(w["symbol"], w["date"]), None)
        return
    if w["status"] == "waiting" and (not w["last_edgar"] or now - w["last_edgar"] >= EDGAR_EVERY):
        _edgar(w)
    if w["status"] == "waiting" and (not w["last_wire"] or now - w["last_wire"] >= WIRE_EVERY):
        _wire(w)
    if not w["last_price"] or now - w["last_price"] >= PRICE_EVERY:
        _price(w)


def _edgar(w: dict):
    from . import earndocs as ed, sec
    w["last_edgar"] = _now()
    w["checks"] += 1
    try:
        if w["_cik"] is None:
            from .earnings import position, resolve
            pos = position(w["symbol"])
            f = ed.filer(w["symbol"], (pos or {}).get("name") or w.get("name"))
            w["_cik"] = f["cik"] if f else 0
            w["_foreign"] = bool(f and f.get("foreign"))
            if not f:
                _log(w, "no SEC filer — watching the wire and the price only")
        if not w["_cik"]:
            return
        sub = sec._load_submissions(w["_cik"])
        store.cache_put(f"sec:sub:{w['_cik']}", sub)
        hit = None
        for x in sub.get("filings") or []:
            if x["date"] < w["date"]:
                break
            if x["form"] in ("8-K", "8-K/A") and "2.02" in (x.get("items") or ""):
                hit = x
            elif x["form"] == "6-K" and w.get("_foreign") and not hit:
                # Foreign filers send 6-Ks for everything (TSMC's monthly
                # sales, bond notices). Only one whose documents say results.
                docs = ed.filing_index(w["_cik"], x["acc"])
                hay = " ".join(f"{d.get('desc', '')} {d.get('doc', '')}" for d in docs).lower()
                if any(k in hay for k in ("result", "earning", "quarter", "interim", "trading update", "q1", "q2", "q3", "q4")):
                    hit = x
        if hit:
            _found(w, hit)
    except Exception as e:
        w["error"] = f"EDGAR: {e.__class__.__name__}: {e}"[:200]


def numbers(sym: str, d: str, name: Optional[str], t0: float, max_age: float = 6 * 3600) -> dict:
    """The release's headline numbers against the consensus, as the live panel shows them."""
    from . import earnings as E
    rel = E.release_parsed(sym, d, name, max_age=max_age)
    p = rel.get("parsed") or {}
    cons = rel.get("consensus") or {}
    eps_key = p.get("eps_basis")
    eps = (p.get(eps_key) or {}).get("value") if eps_key else None
    rev = (p.get("revenue") or {}).get("value")
    usd = p.get("currency", "USD") == "USD"
    return {
        "revenue": rev, "rev_est": cons.get("rev"),
        "rev_surprise": E._surp(rev, cons.get("rev")) if usd else None,
        "revenue_yoy": (p.get("revenue") or {}).get("yoy"),
        "eps": eps, "eps_basis": (p.get(eps_key) or {}).get("label") if eps_key else None,
        "eps_est": cons.get("eps"), "eps_surprise": E._surp(eps, cons.get("eps")) if usd else None,
        "eps_gaap": (p.get("eps_gaap") or {}).get("value"), "eps_adj": (p.get("eps_adj") or {}).get("value"),
        "currency": p.get("currency"), "guidance": (p.get("guidance") or [])[:8],
        "highlights": (p.get("highlights") or [])[:6], "notes": p.get("notes") or [],
        "release_url": (rel.get("doc") or {}).get("url"), "index": (rel.get("filing") or {}).get("index"),
        "read_s": round(_now() - t0, 1),
    }


def _found(w: dict, filing: dict):
    from . import earnings as E
    t = _now()
    acc = filing.get("accepted") or ""
    try:
        acc_ts = datetime.fromisoformat(acc.replace("Z", "+00:00")).timestamp() if acc else None
    except Exception:
        acc_ts = None
    w["found"] = {"t": t, "form": filing["form"], "accepted": acc, "acc": filing["acc"],
                  "latency_s": round(t - acc_ts) if acc_ts else None}
    w["status"] = "found"
    _log(w, f"{filing['form']} on EDGAR (accepted {acc[11:19]} UTC)")
    try:
        w["numbers"] = numbers(w["symbol"], w["date"], w.get("name"), t, max_age=0)
        _log(w, f"release read in {w['numbers']['read_s']}s")
    except Exception as e:
        w["error"] = f"reading the release: {e.__class__.__name__}: {e}"[:200]
        _log(w, "the filing is in but the release could not be read — open it from the link")
    store.cache_put(f"ern:live:{w['symbol']}:{w['date']}", _public(w))
    # The results and sources for this event are stale now.
    for part in ("results", "docs", "analysis"):
        try:
            with store.conn() as c:
                c.execute("DELETE FROM cache WHERE k=?", (f"ern:{part}:{w['symbol']}:{w['date']}",))
        except Exception:
            pass


RESULT_WORDS = ("result", "report", "earnings", "quarter", "revenue", "fiscal")


def _wire(w: dict):
    w["last_wire"] = _now()
    if w.get("wire"):
        return
    try:
        import yfinance as yf
        items = yf.Ticker(w["symbol"]).news or []
    except Exception:
        return
    start = w["started"] - 3 * 3600
    for it in items[:20]:
        c = it.get("content") or it
        title = (c.get("title") or "").strip()
        pub = c.get("pubDate") or c.get("providerPublishTime")
        try:
            ts = pub if isinstance(pub, (int, float)) else datetime.fromisoformat(str(pub).replace("Z", "+00:00")).timestamp()
        except Exception:
            ts = None
        low = title.lower()
        if ts and ts >= start and any(k in low for k in RESULT_WORDS) and \
                ("report" in low or "result" in low or "announce" in low):
            url = ((c.get("canonicalUrl") or {}).get("url") if isinstance(c.get("canonicalUrl"), dict)
                   else c.get("link"))
            w["wire"] = {"title": title, "url": url, "ts": ts, "seen": _now(),
                         "provider": ((c.get("provider") or {}).get("displayName")
                                      if isinstance(c.get("provider"), dict) else c.get("publisher"))}
            _log(w, f"on the wire: {title[:90]}")
            break


def _price(w: dict):
    w["last_price"] = _now()
    try:
        import yfinance as yf
        h = yf.Ticker(w["symbol"]).history(period="2d", interval="1m", prepost=True, auto_adjust=False)
        if h is None or h.empty:
            return
        last = h.iloc[-1]
        px = float(last["Close"])
        ts = h.index[-1].to_pydatetime().timestamp()
        # The reference is the last regular-session close before the report.
        from . import earnings as E
        bars = E._bars(w["symbol"])
        ref = None
        if bars:
            ref = bars[-1][4] if bars[-1][0] < E.today_et().isoformat() or w.get("when") == "amc" else \
                (bars[-2][4] if len(bars) > 1 else None)
        w["price"] = {"price": round(px, 4), "ts": ts, "ref": ref,
                      "move": round((px / ref - 1) * 100, 2) if ref else None}
    except Exception:
        pass
