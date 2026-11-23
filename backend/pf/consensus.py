"""
What the analysts expect, kept day by day.

Yahoo keeps ninety days of EPS revisions in five points and no history at all
of revenue forecasts, price targets or the rating. The watchlist's nightly scan
already reads the estimates of about a thousand companies; this keeps one row a
company a day from what it read, and one from every stock page opened. In three
months the Street panel can draw real revision lines; in a year, how each
company's forecasts behaved around its reports.

A row is dated by the day it was read (Amsterdam), not the day it was stored:
a value from the cache is written under the day it was fetched, so a stale
cache never passes for today's consensus. Later reads the same day fill in
what earlier ones lacked and replace what they had.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from . import store

try:
    from zoneinfo import ZoneInfo
    _AMS = ZoneInfo("Europe/Amsterdam")
except Exception:                                   # pragma: no cover
    _AMS = None

COLS = ("eps_0q", "eps_1q", "eps_0y", "eps_1y", "rev_0q", "rev_1q", "rev_0y", "rev_1y",
        "n_0q", "n_0y", "n_1y", "end_0q", "end_1q", "end_0y", "end_1y", "eps_ccy", "rev_ccy",
        "tgt_mean", "tgt_median", "tgt_high", "tgt_low", "n_tgt", "rec_mean", "price", "px_ccy")
_P = {"0q": "0q", "+1q": "1q", "0y": "0y", "+1y": "1y"}


def day_of(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(ts if ts is not None else time.time(), _AMS).date().isoformat()


def _num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def from_est(e: dict) -> dict:
    """A row from the watchlist scan's estimates (watch.parse_summary)."""
    info = e.get("info") or {}
    row = {"tgt_mean": _num(info.get("targetMeanPrice")), "tgt_median": _num(info.get("targetMedianPrice")),
           "tgt_high": _num(info.get("targetHighPrice")), "tgt_low": _num(info.get("targetLowPrice")),
           "n_tgt": _num(info.get("numberOfAnalystOpinions")), "rec_mean": _num(info.get("recommendationMean")),
           "price": _num(info.get("currentPrice")), "px_ccy": info.get("currency")}
    for p, k in _P.items():
        ee, re_, q = e.get("ee" + p) or {}, e.get("re" + p) or {}, e.get("q" + p) or {}
        row["eps_" + k] = _num(ee.get("avg")) if ee else _num(q.get("eps"))
        row["rev_" + k] = _num(re_.get("avg")) if re_ else _num(q.get("rev"))
        if "n_" + k in COLS:
            row["n_" + k] = _num(ee.get("numberOfAnalysts")) if ee else _num(q.get("n"))
        row["end_" + k] = (e.get("end" + p) or q.get("end"))
    c = e.get("ccy0y") or e.get("ccy+1y") or {}
    row["eps_ccy"], row["rev_ccy"] = c.get("eps"), c.get("rev")
    return row


def from_street(st: dict, ov: Optional[dict] = None) -> dict:
    """A row from the stock page's street payload (research._load_street)."""
    ov = ov or {}
    t, a = st.get("targets") or {}, ov.get("analyst") or {}
    row = {"tgt_mean": _num(t.get("mean")), "tgt_median": _num(t.get("median")), "tgt_high": _num(t.get("high")),
           "tgt_low": _num(t.get("low")), "n_tgt": _num(a.get("n") or st.get("n_targets")),
           "rec_mean": _num(str(a.get("rating") or "").split(" ")[0]) if a.get("rating") else None,
           "price": _num((ov.get("quote") or {}).get("price") or t.get("current")), "px_ccy": ov.get("currency")}
    for p in st.get("periods") or []:
        k = _P.get(p.get("period"))
        if not k:
            continue
        row["eps_" + k] = _num((p.get("eps") or {}).get("avg"))
        row["rev_" + k] = _num((p.get("rev") or {}).get("avg"))
        if "n_" + k in COLS:
            row["n_" + k] = _num((p.get("eps") or {}).get("n"))
        row["end_" + k] = p.get("end")
    c = st.get("currency") or {}
    row["eps_ccy"], row["rev_ccy"] = c.get("eps"), c.get("rev")
    return row


# What counts as the consensus having moved: everything but the price.
SAME = tuple(k for k in COLS if k not in ("price", "px_ccy"))


def _same(new, old) -> bool:
    if new is None:
        return True                              # not read this time is not a change
    if isinstance(new, float) and isinstance(old, (int, float)):
        return abs(new - old) <= 1e-9 * max(1.0, abs(old))
    return new == old


def _write(c, sym: str, d: str, row: dict, src: str, at: float) -> bool:
    """
    Today's row, merged into what today already has. A day on which nothing
    moved since the last row is not stored: the record is the days the
    consensus changed, about a third of the days for a company analysts
    follow closely, which keeps a year of a thousand companies to megabytes.
    """
    vals = [row.get(k) for k in COLS]
    if all(row.get(k) is None for k in SAME):
        return False                             # nothing worth a row
    if not c.execute("SELECT 1 FROM est_day WHERE sym = ? AND d = ?", (sym, d)).fetchone():
        keep = SAME + ("px_ccy",)
        prev = c.execute(f"SELECT {', '.join(keep)} FROM est_day WHERE sym = ? AND d < ? ORDER BY d DESC LIMIT 1",
                         (sym, d)).fetchone()
        if prev and all(_same(row.get(k), v) for k, v in zip(SAME, prev)):
            return False
        if prev:
            # a new day starts from the last one: what this read did not
            # cover (a page has no quarter counts) is what it was
            last = dict(zip(keep, prev))
            row = {**row, **{k: last[k] for k in keep if row.get(k) is None}}
            vals = [row.get(k) for k in COLS]
    c.execute(f"INSERT INTO est_day (sym, d, src, {', '.join(COLS)}, at) VALUES (?, ?, ?, {', '.join('?' * len(COLS))}, ?) "
              f"ON CONFLICT(sym, d) DO UPDATE SET src = excluded.src, at = excluded.at, "
              + ", ".join(f"{k} = COALESCE(excluded.{k}, est_day.{k})" for k in COLS),
              (sym, d, src, *vals, at))
    return True


def record(sym: str, row: dict, src: str, ts: Optional[float] = None) -> None:
    ts = ts if ts is not None else time.time()
    with store.conn() as c:
        _write(c, sym, day_of(ts), row, src, ts)


def note_scan(deep: dict) -> int:
    """Every company the nightly scan read estimates for, in one transaction. Returns rows written."""
    n = 0
    with store.conn() as c:
        for sym, pair in deep.items():
            e = (pair or (None, None))[1] or {}
            ts = e.get("fetched_at")
            if not ts:
                continue
            n += _write(c, sym, day_of(ts), from_est(e), "scan", ts)
    return n


def backfill_once() -> Optional[int]:
    """
    At the first start of build 37: the estimates the scan already holds, each
    under the day it was read, so the record does not start from nothing.
    """
    if store.get_setting("est_backfill"):
        return None
    u = store.cache_get("wl:universe") or {}
    deep = {}
    for r in u.get("rows") or []:
        e = store.cache_get(f"wl:est:{r['sym']}")
        if e:
            deep[r["sym"]] = (None, e)
    n = note_scan(deep)
    store.set_setting("est_backfill", str(n))
    return n


def history(sym: str, also: Optional[list] = None, ccy: Optional[str] = None) -> dict:
    """
    The rows for a symbol, oldest first; with `also`, the company's other
    listings fill the days the symbol itself was not read (the scan reads
    ASML in Amsterdam, the page may be New York's). Their targets and price
    are kept only when they are in the page's currency, `ccy`.
    """
    syms = [sym] + [s for s in (also or []) if s and s != sym]
    with store.conn() as c:
        rows = c.execute(f"SELECT sym, d, src, {', '.join(COLS)} FROM est_day WHERE sym IN ({', '.join('?' * len(syms))}) "
                         f"ORDER BY d", syms).fetchall()
    keys = ("sym", "d", "src") + COLS
    by_day: dict = {}
    own_ccy = ccy
    for r in rows:
        x = dict(zip(keys, r))
        if x["sym"] == sym and x.get("px_ccy") and not own_ccy:
            own_ccy = x["px_ccy"]
    for r in rows:
        x = dict(zip(keys, r))
        cur = by_day.get(x["d"])
        if cur and cur["sym"] == sym:
            continue
        if x["sym"] != sym:
            if cur:
                continue
            if x.get("px_ccy") != own_ccy:
                for k in ("tgt_mean", "tgt_median", "tgt_high", "tgt_low", "price"):
                    x[k] = None
        by_day[x["d"]] = x
    out = [by_day[d] for d in sorted(by_day)]
    return {"symbol": sym, "rows": out, "n": len(out), "first": out[0]["d"] if out else None,
            "listings": sorted({r["sym"] for r in out})}


def siblings(sym: str) -> list:
    """The company's other listings, from the watchlist universe: ASML.AS for ASML."""
    u = store.cache_get("wl:universe") or {}
    for r in u.get("rows") or []:
        group = [r.get("sym")] + list(r.get("also") or [])
        if sym in group:
            return [x for x in group if x and x != sym]
    return []


def count() -> dict:
    with store.conn() as c:
        n, s, lo, hi = c.execute("SELECT COUNT(*), COUNT(DISTINCT sym), MIN(d), MAX(d) FROM est_day").fetchone()
    return {"rows": n, "companies": s, "first": lo, "last": hi}
