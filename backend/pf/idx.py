"""
IDX — the S&P 500 and the Nasdaq 100, from the inside.

An index level says where the market is. It does not say how many shares are
carrying it, whether the analysts behind the earnings are raising or cutting,
what the options market is paying for protection, or what happened the last
times it stood like this. This page reads the members, one by one, and adds
those up.

    level & trend      price and total return, the 50- and 200-day lines,
                       the distance from the high, equal weight beside cap
                       weight
    breadth            how many members are above their 200- and 50-day
                       averages, new 52-week highs against lows, the
                       advance-decline line; two years rebuilt from the
                       members' prices, then a day each night
    concentration      the ten largest weights and how that share has moved,
                       the effective number of shares, the "Magnificent
                       Seven" against the rest
    members & sectors  every member's weight, return and contribution to
                       the index's move; sectors the same way
    valuation          forward and trailing P/E built from the members,
                       earnings yield against the ten-year, CAPE
    earnings           this reporting season's beats and misses, and the
                       share of members whose estimates are being raised
    volatility         VIX or VXN against what the index actually moved,
                       the term structure, skew, and the moves options price
    positioning        the CFTC's weekly count of what asset managers and
                       hedge funds hold in the index future
    history            what followed when the index stood this far from its
                       200-day line and this far below its high, and the
                       months of the year
    your book          how much of it is the index: overlap through your
                       shares, your trackers and your turbos

Sources: State Street's daily SPY holdings (the S&P 500's members and
weights), Nasdaq's Nasdaq-100 list (members and market values; the weights
are by market value, which the index caps slightly differently), Yahoo for
prices and estimates (the watchlist scan's, topped up for members it did not
read), Nasdaq's earnings calendar, the CFTC, Shiller's CAPE.
"""
from __future__ import annotations

import io
import json
import math
import re
import statistics
import threading
import time
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from . import net, store

try:
    from zoneinfo import ZoneInfo
    _AMS = ZoneInfo("Europe/Amsterdam")
except Exception:                                   # pragma: no cover
    _AMS = None

INDICES = {
    "spx": {"name": "S&P 500", "sym": "^GSPC", "tr": "^SP500TR", "ew": "^SPXEW", "etf": "SPY", "vol": "^VIX",
            "cot": "13874A", "fut": "E-mini S&P 500", "n": "about 500"},
    "ndx": {"name": "Nasdaq 100", "sym": "^NDX", "tr": "^XNDX", "ew": "^NDXE", "etf": "QQQ", "vol": "^VXN",
            "cot": "209742", "fut": "E-mini Nasdaq 100", "n": "about 100"},
}
MAG7 = {"AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "TSLA"}
VOL_SYMS = {"spx": ["^VIX9D", "^VIX", "^VIX3M", "^VIX6M", "^SKEW", "^VVIX"], "ndx": ["^VXN", "^VIX"]}
TTL_VIEW = 30 * 3600      # the evening build is the regular one; this only catches a laptop that was off
SSGA_SPY = ("https://www.ssga.com/us/en/intermediary/etfs/library-content/products/fund-data/etfs/us/"
            "holdings-daily-us-en-spy.xlsx")
NASDAQ_NDX = "https://api.nasdaq.com/api/quote/list-type/nasdaq100"
CFTC = ("https://publicreporting.cftc.gov/resource/gpe5-46if.json?$where=cftc_contract_market_code='{code}'"
        "&$order=report_date_as_yyyy_mm_dd%20DESC&$limit=330")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0 Safari/537.36", "Accept": "*/*"}
NASDAQ_H = {**UA, "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/"}
PERIODS = {"1d": 1, "1w": 5, "1m": 21, "3m": 63, "ytd": None, "1y": 252}


def _f(x) -> Optional[float]:
    try:
        v = float(str(x).replace(",", "").replace("$", "").replace("%", ""))
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r(x, d=2):
    return round(x, d) if x is not None and math.isfinite(x) else None


def _ysym(s: str) -> str:
    """Yahoo's spelling: BRK.B is BRK-B."""
    return (s or "").strip().upper().replace(".", "-").replace("/", "-")


# --------------------------------------------------------------------------
# members and weights
# --------------------------------------------------------------------------

def _load_spx_members() -> dict:
    import openpyxl
    raw = net.get(SSGA_SPY, UA, timeout=40)
    ws = openpyxl.load_workbook(io.BytesIO(raw), read_only=True).active
    rows, head, asof = [], None, None
    for r in ws.iter_rows(values_only=True):
        cells = list(r)
        if cells and isinstance(cells[0], str) and cells[0].startswith("Holdings:"):
            asof = str(cells[1] or "").replace("As of", "").strip()
        if head is None:
            if cells and cells[0] == "Name" and "Weight" in cells:
                head = {k: i for i, k in enumerate(cells) if k}
            continue
        tk, w = cells[head["Ticker"]], _f(cells[head["Weight"]])
        if not tk or w is None or w <= 0 or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,6}", str(tk).strip()):
            continue                       # cash, futures, and placeholders for a corporate action
        rows.append({"sym": _ysym(str(tk)), "name": str(cells[head["Name"]] or tk).title(), "w": w})
    tot = sum(x["w"] for x in rows)
    if len(rows) < 400 or not tot:
        raise RuntimeError(f"State Street's SPY file had {len(rows)} holdings")
    for x in rows:
        x["w"] = x["w"] / tot * 100
    return {"rows": rows, "asof": asof, "source": "State Street's daily SPY holdings", "approx": False}


def _load_ndx_members() -> dict:
    d = (json.loads(net.get(NASDAQ_NDX, NASDAQ_H, timeout=30)).get("data")) or {}
    rows = []
    for r in ((d.get("data") or {}).get("rows")) or []:
        mc = _f(r.get("marketCap"))
        if r.get("symbol") and mc:
            nm = (r.get("companyName") or r["symbol"]).replace(" Common Stock", "").replace(" Class A", " A").replace(" Class C", " C")
            rows.append({"sym": _ysym(r["symbol"]), "name": nm.strip(), "mcap": mc})
    tot = sum(x["mcap"] for x in rows)
    if len(rows) < 90 or not tot:
        raise RuntimeError(f"Nasdaq's list had {len(rows)} members")
    for x in rows:
        x["w"] = x["mcap"] / tot * 100
    return {"rows": rows, "asof": d.get("date"), "approx": True,
            "source": "Nasdaq's Nasdaq-100 list; weights by market value (the index caps its largest weights slightly)"}


def members(ix: str) -> dict:
    fn = _load_spx_members if ix == "spx" else _load_ndx_members
    return store.cache_through(f"idx:members:{ix}", 12 * 3600, fn)


def _sector_map() -> dict:
    """Sector per symbol from what the watchlist already knows; nothing fetched."""
    out = {}
    u = store.cache_get("wl:universe") or {}
    for r in u.get("rows") or []:
        for s in [r.get("sym")] + list(r.get("also") or []):
            if s and r.get("sector"):
                out[s] = r["sector"]
    return out


def _universe_rows() -> dict:
    out = {}
    for r in (store.cache_get("wl:universe") or {}).get("rows") or []:
        for s in [r.get("sym")] + list(r.get("also") or []):
            if s:
                out[s] = r
    return out


# --------------------------------------------------------------------------
# the members' prices: breadth, concentration, contributions
# --------------------------------------------------------------------------

def _frame(syms: list[str], period: str = "3y", with_volume: bool = False):
    """The members' closes; with `with_volume`, their share volumes beside them, from the same download."""
    import pandas as pd
    from . import watch
    cols, vcols, vols = {}, {}, ({} if with_volume else None)
    for s, days, c in watch.each_series(syms, period=period, chunk=150, volumes=vols):
        ix = pd.to_datetime(days)
        cols[s] = pd.Series(c, index=ix)
        if vols is not None and s in vols:
            vcols[s] = pd.Series(vols[s], index=ix)
    df = pd.DataFrame(cols).sort_index() if cols else pd.DataFrame()
    if with_volume:
        return df, (pd.DataFrame(vcols).sort_index() if vcols else pd.DataFrame())
    return df


def updown_series(df, vdf, idx_col: str, n_days: int = 756) -> Optional[dict]:
    """
    Per session, among the members: how many rose and how many fell, the shares traded in
    each, and the points (dollars a share) gained and lost. The index's own version of the
    NYSE's up and down volume; FLOW reads Lowry's 90% days and Zweig's thrust from it
    (pf/flow.py). A member with no bar that day neither rose nor fell, and a session where
    fewer than nine in ten members have a bar is left out rather than read as a lopsided day.
    """
    if vdf is None or getattr(vdf, "empty", True):
        return None
    cols = [c for c in df.columns if c != idx_col and c in vdf.columns]
    if not cols:
        return None
    m = df[cols]
    v = vdf[cols].reindex(m.index)
    ch = m.diff()
    up, dn = ch > 0, ch < 0
    n = (ch.notna() & v.notna()).sum(axis=1)
    full = n >= n.iloc[1:].median() * 0.9 if len(n) > 1 else n > 0
    upv, dnv = v.where(up).sum(axis=1), v.where(dn).sum(axis=1)
    ptu, ptd = ch.where(up).sum(axis=1), (-ch.where(dn)).sum(axis=1)
    keep = [i for i in range(1, len(m.index)) if bool(full.iloc[i]) and (upv.iloc[i] + dnv.iloc[i]) > 0][-n_days:]
    return {"d": [m.index[i].strftime("%Y-%m-%d") for i in keep],
            "adv": [int(up.iloc[i].sum()) for i in keep], "dec": [int(dn.iloc[i].sum()) for i in keep],
            "upv": [int(upv.iloc[i]) for i in keep], "dnv": [int(dnv.iloc[i]) for i in keep],
            "ptu": [_r(float(ptu.iloc[i]), 2) for i in keep], "ptd": [_r(float(ptd.iloc[i]), 2) for i in keep],
            "n": [int(n.iloc[i]) for i in keep]}


def breadth_series(df, idx_col: str, n_days: int = 504) -> dict:
    """
    Per day, of the members with enough history: the share above their 200-
    and 50-day averages, new 52-week highs and lows (a close beyond every one
    of the 251 before it), those within 5% of their high, and the advance-
    decline line. Two years, rebuilt from the members' own closes.
    """
    m = df.drop(columns=[c for c in (idx_col,) if c in df.columns]).ffill(limit=5)
    s200, s50 = m.rolling(200, min_periods=200).mean(), m.rolling(50, min_periods=50).mean()
    prev_hi = m.shift(1).rolling(251, min_periods=240).max()
    prev_lo = m.shift(1).rolling(251, min_periods=240).min()
    hi252 = m.rolling(252, min_periods=240).max()
    ok200, ok50, ok252 = s200.notna(), s50.notna(), prev_hi.notna()
    p200 = ((m > s200) & ok200).sum(axis=1) / ok200.sum(axis=1).where(lambda x: x > 0) * 100
    p50 = ((m > s50) & ok50).sum(axis=1) / ok50.sum(axis=1).where(lambda x: x > 0) * 100
    nh = ((m > prev_hi) & ok252).sum(axis=1)
    nl = ((m < prev_lo) & ok252).sum(axis=1)
    near = ((m >= hi252 * 0.95) & ok252).sum(axis=1) / ok252.sum(axis=1).where(lambda x: x > 0) * 100
    ch = m.diff()
    adv, dec = (ch > 0).sum(axis=1), (ch < 0).sum(axis=1)
    ad = (adv - dec).cumsum()
    tail = slice(-n_days, None)
    d = [x.strftime("%Y-%m-%d") for x in m.index[tail]]
    lvl = df[idx_col].ffill() if idx_col in df.columns else None
    ad_t = ad.iloc[tail]
    return {"d": d,
            "p200": [_r(v, 1) for v in p200.iloc[tail]], "p50": [_r(v, 1) for v in p50.iloc[tail]],
            "nh": [int(v) for v in nh.iloc[tail]], "nl": [int(v) for v in nl.iloc[tail]],
            "near": [_r(v, 1) for v in near.iloc[tail]],
            "ad": [int(v - ad_t.iloc[0]) for v in ad_t],
            "idx": [_r(v, 2) for v in lvl.iloc[tail]] if lvl is not None else None,
            "n": int(ok200.iloc[-1].sum())}


def weights_path(df, w_now: dict, n_days: int = 504) -> dict:
    """
    Each member's weight on each day, from today's weight and its price since:
    a share count held constant, so buybacks, issues and index changes are not
    in it. Good for the shape of concentration, not for its third decimal.
    """
    syms = [s for s in w_now if s in df.columns]
    m = df[syms].ffill(limit=5).iloc[-n_days:]
    last = m.iloc[-1]
    rel = m.divide(last)
    raw = rel.multiply([w_now[s] for s in syms], axis=1)
    w = raw.divide(raw.sum(axis=1), axis=0)
    return {"syms": syms, "w": w, "m": m}


def concentration(wp: dict) -> dict:
    w = wp["w"]
    top10 = w.apply(lambda row: row.nlargest(10).sum(), axis=1) * 100
    eff = 1 / (w.pow(2).sum(axis=1))
    mag = [s for s in wp["syms"] if s in MAG7]
    mag_share = w[mag].sum(axis=1) * 100 if mag else None
    d = [x.strftime("%Y-%m-%d") for x in w.index]
    # the Magnificent Seven against the rest, each cap-weighted by yesterday's weights
    r = wp["m"].pct_change()
    wl = w.shift(1)
    rest = [s for s in wp["syms"] if s not in MAG7]

    def group(cols):
        if not cols:
            return None
        num = (wl[cols] * r[cols]).sum(axis=1)
        den = wl[cols].where(r[cols].notna()).sum(axis=1)
        g = (num / den.where(den > 0)).fillna(0)
        return [_r(v, 2) for v in (1 + g).cumprod() * 100]
    return {"d": d, "top10": [_r(v, 2) for v in top10], "eff_n": [_r(v, 1) for v in eff],
            "mag7": [_r(v, 2) for v in mag_share] if mag_share is not None else None,
            "mag7_line": group(mag), "rest_line": group(rest), "mag7_syms": sorted(mag)}


def member_stats(df, w_now: dict, idx_col: str) -> tuple[list[dict], dict]:
    """Returns, trend and contribution per member; the index's own returns over the same periods."""
    m = df.ffill(limit=5)
    cal = m.index
    yr0 = next((i for i in range(len(cal) - 1, -1, -1) if cal[i].year < cal[-1].year), 0)
    start = {k: (len(cal) - 1 - v if v else yr0) for k, v in PERIODS.items()}
    last = m.iloc[-1]
    s200 = m.iloc[-200:].mean() if len(m) >= 200 else None
    s50 = m.iloc[-50:].mean()
    hi = m.iloc[-252:].max()
    rows, idx_r = [], {}
    if idx_col in m.columns:
        for k, i in start.items():
            a = m[idx_col].iloc[max(0, i)]
            idx_r[k] = _r((last[idx_col] / a - 1) * 100, 2) if a else None
    # start-of-period weights, from today's and the moves since
    wst = {}
    for k, i in start.items():
        ws = {s: w_now[s] * (m[s].iloc[max(0, i)] / last[s]) for s in w_now
              if s in m.columns and last.get(s) and m[s].iloc[max(0, i)] == m[s].iloc[max(0, i)]}
        tot = sum(ws.values()) or 1
        wst[k] = {s: v / tot * 100 for s, v in ws.items()}
    for s, w in w_now.items():
        if s not in m.columns or not last.get(s) or last[s] != last[s]:
            rows.append({"sym": s, "w": w})
            continue
        rr, cc = {}, {}
        for k, i in start.items():
            a = m[s].iloc[max(0, i)]
            if a and a == a:
                rr[k] = _r((last[s] / a - 1) * 100, 2)
                cc[k] = _r(wst[k].get(s, 0) * (last[s] / a - 1), 3)
        rows.append({"sym": s, "w": w, "px": _r(float(last[s]), 2), "r": rr, "c": cc,
                     "a200": bool(s200 is not None and s200.get(s) == s200.get(s) and last[s] > s200[s]),
                     "a50": bool(last[s] > s50[s]) if s50.get(s) == s50.get(s) else None,
                     "dd52": _r((last[s] / hi[s] - 1) * 100, 1) if hi.get(s) else None})
    return rows, idx_r


def sector_stats(rows: list[dict]) -> list[dict]:
    """
    Each sector's weight, members, return and contribution per period. The
    return is the sector held from the start of the period: today's weights
    moved back by each member's return. Weighting by today's weights would
    favour the period's winners, which have grown into theirs.
    """
    secs = {}
    for r in rows:
        s = secs.setdefault(r["sector"], {"sector": r["sector"], "w": 0.0, "n": 0, "c": {}, "r": {}})
        s["w"] += r["w"]
        s["n"] += 1
        for k, v in (r.get("c") or {}).items():
            s["c"][k] = s["c"].get(k, 0) + (v or 0)
    for s in secs.values():
        for k in PERIODS:
            members_k = [r for r in rows if r["sector"] == s["sector"] and (r.get("r") or {}).get(k) is not None]
            ww = sum(r["w"] for r in members_k)
            w0 = sum(r["w"] / (1 + r["r"][k] / 100) for r in members_k if r["r"][k] > -100)
            s["r"][k] = round((ww / w0 - 1) * 100, 2) if ww and w0 else None
        s["w"] = round(s["w"], 2)
        s["c"] = {k: round(v, 2) for k, v in s["c"].items()}
    return sorted(secs.values(), key=lambda s: -s["w"])


# --------------------------------------------------------------------------
# valuation, revisions, the reporting season
# --------------------------------------------------------------------------

def _harm(pairs: list[tuple[float, float]]) -> Optional[float]:
    """The index's multiple: total weight over total weight-per-multiple (price over summed earnings)."""
    num = sum(w for w, x in pairs if x and x > 0)      # a loss-maker has no multiple to add
    den = sum(w / x for w, x in pairs if x and x > 0)
    return round(num / den, 2) if den else None


def valuation(rows: list[dict], ix: str) -> dict:
    uni = _universe_rows()
    fp, tp, top_fp, rest_fp, eq_fp = [], [], [], [], []
    top = {r["sym"] for r in sorted(rows, key=lambda r: -r["w"])[:10]}
    cov_w = 0.0
    for r in rows:
        u = uni.get(r["sym"]) or {}
        fpe, tpe = _f(u.get("fpe")), _f(u.get("tpe"))
        if fpe is None:
            info = ((store.cache_get(f"wl:est:{r['sym']}") or {}).get("info")) or {}
            px, fe = _f(info.get("currentPrice")), _f(info.get("forwardEps"))
            fpe = px / fe if px and fe and fe > 0 else None
        r["fpe"], r["tpe"] = _r(fpe, 1), _r(tpe, 1)
        if fpe and fpe > 0:
            fp.append((r["w"], fpe))
            eq_fp.append((1.0, fpe))
            (top_fp if r["sym"] in top else rest_fp).append((r["w"], fpe))
            cov_w += r["w"]
        if tpe and tpe > 0:
            tp.append((r["w"], tpe))
    out = {"fwd_pe": _harm(fp), "trail_pe": _harm(tp), "top10_fwd_pe": _harm(top_fp), "rest_fwd_pe": _harm(rest_fp),
           "equal_fwd_pe": _harm(eq_fp), "coverage_w": round(cov_w, 1),
           "note": "members with positive earnings only; a loss-maker has no P/E to add"}
    rows_b = (store.cache_get("market") or {}).get("rows") or []
    t10 = next((r.get("last") for r in rows_b if r.get("k") == "US10Y"), None)
    if out["fwd_pe"]:
        out["earn_yield"] = round(100 / out["fwd_pe"], 2)
        if t10 is not None:
            out["us10y"] = t10
            out["erp"] = round(out["earn_yield"] - t10, 2)
    if ix == "spx":
        try:
            from . import fcst
            c = fcst.cape()
            out["cape"] = {k: c.get(k) for k in ("value", "asof", "pred10", "avg10", "above30", "source")}
        except Exception:
            pass
    return out


def revisions(rows: list[dict]) -> dict:
    """
    How analysts are moving next year's EPS for the members, from the estimates
    the watchlist scan reads each night (and this page tops up): the share of
    members raised and cut over thirty days, by count and by weight, and the
    weighted change in the index's forward earnings.
    """
    up = dn = flat = 0
    wu = wd = 0.0
    chg30, chg90, wsum = 0.0, 0.0, 0.0
    ups = downs = 0
    seen = 0
    for r in rows:
        e = store.cache_get(f"wl:est:{r['sym']}") or {}
        tr, rv = e.get("trend+1y") or {}, e.get("rev+1y") or {}
        cur, a30, a90 = _f(tr.get("current")), _f(tr.get("30daysAgo")), _f(tr.get("90daysAgo"))
        if not cur or not a30:
            continue
        seen += 1
        c30 = (cur / a30 - 1) * 100
        r["rev30"] = _r(c30, 2)
        if c30 > 0.1:
            up += 1
            wu += r["w"]
        elif c30 < -0.1:
            dn += 1
            wd += r["w"]
        else:
            flat += 1
        chg30 += r["w"] * c30
        if a90:
            chg90 += r["w"] * (cur / a90 - 1) * 100
        wsum += r["w"]
        ups += int(_f(rv.get("upLast30days")) or 0)
        downs += int(_f(rv.get("downLast30days")) or 0)
    if not seen:
        return {"available": False, "why": "no members' estimates read yet"}
    return {"available": True, "n": seen, "up": up, "down": dn, "flat": flat,
            "up_w": round(wu, 1), "down_w": round(wd, 1), "cover_w": round(wsum, 1),
            "eps_chg30": round(chg30 / wsum, 2) if wsum else None, "eps_chg90": round(chg90 / wsum, 2) if wsum else None,
            "analysts_up": ups, "analysts_down": downs,
            "net_ratio": round((ups - downs) / (ups + downs), 2) if ups + downs else None}


def top_up_estimates(syms: list[str], progress=None, cap: int = 600) -> int:
    """Read estimates for members the nightly scan skipped, one request at a time; kept like the scan's."""
    from . import watch
    n = 0
    deep = {}
    for i, s in enumerate(syms[:cap]):
        hit = store.cache_get(f"wl:est:{s}")
        age = (hit or {}).get("_age_s")
        if hit and hit.get("ver") == watch.WL_VER and age is not None and age <= watch.TTL["est"]:
            continue
        try:
            e, _ = watch.cached(f"wl:est:{s}", watch.TTL["est"], lambda: watch._load_est(s), True)
            if e:
                deep[s] = (None, e)
                n += 1
        except watch.RateLimited:
            break
        except Exception:
            continue
        if progress and i % 10 == 0:
            progress(f"reading estimates: {i} of {len(syms)}")
    if deep:
        try:
            from . import consensus
            consensus.note_scan(deep)
        except Exception:
            pass
    return n


def _quarter_end(d: date) -> date:
    q = (d.month - 1) // 3
    return date(d.year, q * 3 + 1, 1) - timedelta(days=1)


def season(rows: list[dict], today: Optional[date] = None) -> dict:
    """
    This reporting season for the members, from Nasdaq's calendar day by day:
    how many reported, how many beat, the blended growth against a year ago,
    and who reports in the coming week.
    """
    from . import earnings as ern
    today = today or date.today()
    qe = _quarter_end(today)
    start = qe + timedelta(days=10)
    if start > today:
        qe = _quarter_end(qe)
        start = qe + timedelta(days=10)
    w = {r["sym"]: r["w"] for r in rows}
    rep, nxt = [], []
    # Nasdaq's day file drops last year's EPS once a company has reported:
    # it is remembered while the report is still ahead.
    memo = (store.cache_get("idx:ly") or {}).get("m") or {}
    d = start
    while d <= today + timedelta(days=7):
        if d.weekday() < 5:
            for x in ern.nasdaq_day(d):
                if x["sym"] not in w:
                    continue
                key = f"{x['sym']}|{x.get('fq')}"
                if x.get("ly_eps") is not None:
                    memo[key] = x["ly_eps"]
                item = {"sym": x["sym"], "d": d.isoformat(), "w": round(w[x["sym"]], 3), "when": x.get("when"),
                        "est": x.get("eps_est"), "act": x.get("eps_act"), "ly": x.get("ly_eps", None) or memo.get(key)}
                if d <= today and x.get("eps_act") is not None:
                    rep.append(item)
                elif d >= today:
                    nxt.append(item)
        d += timedelta(days=1)
    try:
        store.cache_put("idx:ly", {"m": memo})
    except Exception:
        pass
    beat = [x for x in rep if x["est"] is not None and x["act"] > x["est"] * (1 + 0.005 * (1 if x["est"] >= 0 else -1))]
    miss = [x for x in rep if x["est"] is not None and x["act"] < x["est"] * (1 - 0.005 * (1 if x["est"] >= 0 else -1))]
    gr = [(x["w"], x["act"] / x["ly"] - 1) for x in rep if x.get("ly") and x["ly"] > 0 and x["act"] is not None]
    sur = [(x["w"], (x["act"] - x["est"]) / abs(x["est"])) for x in rep if x["est"]]
    wt = lambda pairs: sum(a * b for a, b in pairs) / sum(a for a, _ in pairs) * 100 if pairs else None
    return {"quarter_end": qe.isoformat(), "since": start.isoformat(), "reported": len(rep),
            "reported_w": round(sum(x["w"] for x in rep), 1), "beat": len(beat), "miss": len(miss),
            "inline": len(rep) - len(beat) - len(miss),
            "beat_pct": round(len(beat) / len(rep) * 100, 0) if rep else None,
            "growth_w": _r(wt(gr), 1), "growth_n": len(gr), "surprise_w": _r(wt(sur), 1),
            "best": sorted([{**x, "s": round((x["act"] - x["est"]) / abs(x["est"]) * 100, 1)} for x in rep if x["est"]],
                           key=lambda x: -x["s"])[:5],
            "worst": sorted([{**x, "s": round((x["act"] - x["est"]) / abs(x["est"]) * 100, 1)} for x in rep if x["est"]],
                            key=lambda x: x["s"])[:5],
            "next": sorted(nxt, key=lambda x: (x["d"], -x["w"])), "next_w": round(sum(x["w"] for x in nxt), 1),
            "source": "Nasdaq's earnings calendar"}


# --------------------------------------------------------------------------
# volatility, options, positioning
# --------------------------------------------------------------------------

def _pct_rank(vals: list, cur) -> Optional[float]:
    v = sorted(x for x in vals if x is not None)
    if not v or cur is None:
        return None
    import bisect
    return round(bisect.bisect_left(v, cur) / len(v) * 100, 0)


def volatility(ix: str, idx_closes: list[float], idx_days: list[str]) -> dict:
    """The volatility index against what the index actually moved, its percentile over ten years, the curve."""
    from . import research as rs
    out = {"curve": [], "series": {}}
    rv = {}
    for i in range(21, len(idx_closes)):
        rets = [math.log(idx_closes[j] / idx_closes[j - 1]) for j in range(i - 20, i + 1)]
        rv[idx_days[i]] = statistics.pstdev(rets) * math.sqrt(252) * 100
    out["realised_21d"] = _r(rv[idx_days[-1]], 1) if rv else None
    for s in VOL_SYMS[ix]:
        try:
            bars = rs.daily(s)["bars"]
        except Exception:
            continue
        c = [b[4] for b in bars]
        d = [b[0] for b in bars]
        if not c:
            continue
        m1 = c[-22] if len(c) > 22 else None
        out["curve"].append({"sym": s, "last": _r(c[-1], 2), "m1": _r(m1, 2), "pct10y": _pct_rank(c, c[-1]),
                             "lo": _r(min(c), 2), "hi": _r(max(c), 2), "median": _r(statistics.median(c), 2)})
        if s == INDICES[ix]["vol"]:
            out["series"] = {"d": d[-504:], "v": [_r(x, 2) for x in c[-504:]],
                             "rv": [_r(rv.get(x), 1) for x in d[-504:]]}
    main = next((x for x in out["curve"] if x["sym"] == INDICES[ix]["vol"]), None)
    if main and out["realised_21d"] is not None:
        out["premium"] = _r(main["last"] - out["realised_21d"], 1)
    cur = {x["sym"]: x for x in out["curve"]}
    if "^VIX" in cur and "^VIX3M" in cur and cur["^VIX3M"]["last"]:
        out["slope"] = _r(cur["^VIX"]["last"] / cur["^VIX3M"]["last"], 3)
        out["inverted"] = out["slope"] > 1
    if ix == "ndx" and "^VXN" in cur and "^VIX" in cur and cur["^VIX"]["last"]:
        out["vxn_vix"] = _r(cur["^VXN"]["last"] / cur["^VIX"]["last"], 2)
    return out


def implied_moves(ix: str) -> dict:
    """What the ETF's options price: a straddle at the money, for the next week, month and quarter."""
    from . import research as rs
    etf = INDICES[ix]["etf"]
    try:
        t = rs._tk(etf)
        price = (rs.overview(etf).get("quote") or {}).get("price")
        exps = list(t.options or [])
    except Exception as e:
        return {"available": False, "why": f"no option chain: {e.__class__.__name__}"}
    if not exps or not price:
        return {"available": False, "why": "no option chain"}
    today = date.today()
    out = []
    for label, days in (("about a week", 7), ("about a month", 30), ("about three months", 91)):
        e = min(exps, key=lambda x: abs((date.fromisoformat(x) - today).days - days))
        s = rs._safe(lambda: rs._straddle(t, e, price))
        if s:
            n = max((date.fromisoformat(e) - today).days, 1)
            out.append({**s, "label": label, "days": n, "daily": round(s["move_pct"] / math.sqrt(n * 252 / 365), 2)})
        time.sleep(0.3)
    # the coming week's big US releases, from the macro calendar
    ev = []
    try:
        with store.conn() as c:
            ev = [{"d": r[0], "t": r[1], "title": r[2]} for r in c.execute(
                "SELECT d, t, title FROM mac_event WHERE ccy = 'USD' AND rank >= 3 AND d >= ? ORDER BY d, t LIMIT 12",
                (today.isoformat(),)).fetchall()]
    except Exception:
        pass
    return {"available": bool(out), "etf": etf, "price": price, "moves": out, "events": ev}


def _load_cot(code: str) -> dict:
    from . import euro
    rows = json.loads(net.get(CFTC.format(code=code), timeout=40))
    weeks = euro.parse_cot(rows)
    if len(weeks) < 20:
        raise RuntimeError("the CFTC returned too few weeks")
    return {"weeks": weeks, "fetched_at": time.time()}


def positioning(ix: str) -> dict:
    """Asset managers and leveraged funds in the index future, net, as a share of open interest; five years."""
    try:
        w = store.cache_through(f"idx:cot:{ix}", 12 * 3600, lambda: _load_cot(INDICES[ix]["cot"]))["weeks"]
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:160]}
    w = w[-260:]
    last = w[-1]
    am, lf = [x["am_pct"] for x in w], [x["lf_pct"] for x in w]
    ago = w[-5] if len(w) > 5 else w[0]
    return {"available": True, "future": INDICES[ix]["fut"], "asof": last["d"],
            "am_pct": last["am_pct"], "lf_pct": last["lf_pct"],
            "am_rank": _pct_rank(am, last["am_pct"]), "lf_rank": _pct_rank(lf, last["lf_pct"]),
            "am_4w": _r(last["am_pct"] - ago["am_pct"], 1), "lf_4w": _r(last["lf_pct"] - ago["lf_pct"], 1),
            "series": {"d": [x["d"] for x in w], "am": am, "lf": lf}, "source": "CFTC Traders in Financial Futures"}


# --------------------------------------------------------------------------
# what history says
# --------------------------------------------------------------------------

def _load_long(sym: str) -> dict:
    from . import research as rs
    h = rs._tk(sym).history(period="max", interval="1d", auto_adjust=False)
    d, c = [], []
    for i, row in h.iterrows():
        v = _f(row.get("Close"))
        if v and v > 0:
            d.append(str(i.date()))
            c.append(round(v, 4))
    if len(c) < 2000:
        raise RuntimeError(f"too little history for {sym}")
    return {"d": d, "c": c}


def long_series(sym: str) -> dict:
    return store.cache_through(f"idx:long:{sym}", 20 * 3600, lambda: _load_long(sym))


def dd_bucket(dd: float) -> str:
    for lim, lab in ((-2, "within 2% of its high"), (-5, "2–5% below its high"), (-10, "5–10% below its high"),
                     (-20, "10–20% below its high")):
        if dd >= lim:
            return lab
    return "more than 20% below its high"


def history_study(d: list[str], c: list[float], horizons=(21, 63, 126, 252), band: float = 2.0) -> dict:
    """
    Days that stood as today does: within `band` points of today's distance
    from the 200-day average, in the same band below the all-time high. What
    followed over one, three, six and twelve months, against every day.
    Overlapping days count once a month, so one long rally is not twenty
    separate pieces of evidence.
    """
    n = len(c)
    if n < 600:
        return {"available": False}
    sma, run, peak = [None] * n, 0.0, 0.0
    for i in range(n):
        run += c[i]
        if i >= 200:
            run -= c[i - 200]
        if i >= 199:
            sma[i] = run / 200
    dist = [((c[i] / sma[i] - 1) * 100) if sma[i] else None for i in range(n)]
    dd = []
    for v in c:
        peak = max(peak, v)
        dd.append((v / peak - 1) * 100)
    cur_dist, cur_dd = dist[-1], dd[-1]
    bucket = dd_bucket(cur_dd)
    similar, last_pick = [], -999
    for i in range(199, n - 1):
        if dist[i] is None or abs(dist[i] - cur_dist) > band or dd_bucket(dd[i]) != bucket:
            continue
        if i - last_pick >= 21:
            similar.append(i)
            last_pick = i
    every = list(range(199, n - 1, 21))

    def stats(idx: list[int], h: int):
        f = [(c[i + h] / c[i] - 1) * 100 for i in idx if i + h < n]
        if len(f) < 5:
            return None
        f.sort()
        q = lambda p: f[min(len(f) - 1, int(p * (len(f) - 1)))]
        return {"n": len(f), "median": round(q(0.5), 2), "mean": round(statistics.fmean(f), 2),
                "pos": round(sum(1 for x in f if x > 0) / len(f) * 100, 0), "p10": round(q(0.1), 2), "p90": round(q(0.9), 2)}
    out = {"available": True, "from": d[0], "dist200": _r(cur_dist, 2), "dd": _r(cur_dd, 2), "bucket": bucket,
           "n_similar": len(similar), "band": band,
           "similar": {str(h): stats(similar, h) for h in horizons}, "all": {str(h): stats(every, h) for h in horizons},
           "recent_examples": [{"d": d[i], "dist": _r(dist[i], 1), "dd": _r(dd[i], 1),
                                "f63": _r((c[i + 63] / c[i] - 1) * 100, 1) if i + 63 < n else None,
                                "f252": _r((c[i + 252] / c[i] - 1) * 100, 1) if i + 252 < n else None}
                               for i in similar[-8:]]}
    # the months of the year
    mo = {}
    last_m = None
    for i in range(1, n):
        if d[i][:7] != d[i - 1][:7]:
            if last_m is not None:
                k = int(d[i - 1][5:7])
                mo.setdefault(k, []).append((c[i - 1] / c[last_m] - 1) * 100)
            last_m = i - 1
    out["months"] = [{"m": k, "mean": round(statistics.fmean(v), 2), "median": round(statistics.median(v), 2),
                      "pos": round(sum(1 for x in v if x > 0) / len(v) * 100, 0), "n": len(v)}
                     for k, v in sorted(mo.items())]
    # the deepest falls, with how long they took to recover
    falls, i, peak_i = [], 0, 0
    while i < n:
        if c[i] >= c[peak_i]:
            peak_i = i
            i += 1
            continue
        trough_i = i
        while i < n and c[i] < c[peak_i]:
            if c[i] < c[trough_i]:
                trough_i = i
            i += 1
        depth = (c[trough_i] / c[peak_i] - 1) * 100
        if depth <= -15:
            falls.append({"peak": d[peak_i], "trough": d[trough_i], "depth": round(depth, 1),
                          "down_days": trough_i - peak_i, "recovered": d[i] if i < n else None,
                          "up_days": (i - trough_i) if i < n else None})
        peak_i = i if i < n else peak_i
    out["falls"] = sorted(falls, key=lambda x: x["depth"])[:10]
    return out


# --------------------------------------------------------------------------
# your book against the index
# --------------------------------------------------------------------------

def _fund_index(name: str) -> Optional[str]:
    n = (name or "").upper()
    if "S&P 500" in n or "S&P500" in n or "SP 500" in n:
        return "spx"
    if "NASDAQ" in n and "100" in n:
        return "ndx"
    return None


def book(ix: str) -> dict:
    """
    How much of the book is this index. Shares count at their value; a tracker
    of the S&P 500 or the Nasdaq 100 as its members at their weights (looked
    through); a turbo as the share it tracks at its exposure (value times
    leverage). Overlap is the part of the book that is the index at index
    weight; active share the rest.
    """
    from . import service
    d = service.dashboard("SP500", light=True)
    if d.get("empty"):
        return {"available": False, "why": "no holdings"}
    insts = store.load_instruments()
    mem = members(ix)["rows"]
    w_idx = {r["sym"]: r["w"] / 100 for r in mem}
    look = {k: {r["sym"]: r["w"] / 100 for r in members(k)["rows"]} for k in ("spx", "ndx")}
    exp, via = {}, {}
    other = 0.0
    for p in d.get("positions") or []:
        if p.get("excluded"):
            continue
        v = p.get("exposure_eur") if p.get("kind") == "DERIVATIVE" else p.get("value_eur")
        v = v or 0.0
        if v <= 0:
            continue
        kind = p.get("kind")
        if kind == "EQUITY" and p.get("ticker"):
            s = p["ticker"].upper()
            exp[s] = exp.get(s, 0) + v
            via.setdefault(s, set()).add("shares")
        elif kind == "DERIVATIVE":
            s = ((insts.get(p.get("isin")) or {}).get("underlying_ticker") or "").upper()
            if s:
                exp[s] = exp.get(s, 0) + v
                via.setdefault(s, set()).add("turbo")
            else:
                other += v
        elif kind == "ETF" and _fund_index(p.get("name")):
            for s, w in look[_fund_index(p.get("name"))].items():
                exp[s] = exp.get(s, 0) + v * w
                via.setdefault(s, set()).add("tracker")
        else:
            other += v
    tot = sum(exp.values()) + other
    if not tot:
        return {"available": False, "why": "no equity exposure"}
    wb = {s: v / tot for s, v in exp.items()}
    overlap = sum(min(wb.get(s, 0), w_idx[s]) for s in w_idx)
    in_idx = sum(v for s, v in wb.items() if s in w_idx)
    diffs = sorted(((s, wb.get(s, 0) - w_idx.get(s, 0)) for s in set(wb) | set(w_idx)), key=lambda x: x[1])
    over = [{"sym": s, "book": round(wb.get(s, 0) * 100, 2), "index": round(w_idx.get(s, 0) * 100, 2),
             "diff": round(x * 100, 2), "via": sorted(via.get(s, []))} for s, x in diffs[::-1][:12] if x > 0]
    under = [{"sym": s, "book": round(wb.get(s, 0) * 100, 2), "index": round(w_idx.get(s, 0) * 100, 2),
              "diff": round(x * 100, 2)} for s, x in diffs[:10] if x < 0]
    out = {"available": True, "exposure_eur": round(tot, 0), "overlap": round(overlap * 100, 1),
           "active_share": round((1 - overlap) * 100, 1), "in_index": round(in_idx * 100, 1),
           "outside": round((1 - in_idx) * 100, 1), "over": over, "under": under,
           "names_in_book": sum(1 for s in wb if s in w_idx), "members": len(w_idx),
           # held yourself, as shares or a turbo; a tracker alone would mark every member
           "held": sorted(s for s in wb if s in w_idx and via.get(s, set()) - {"tracker"})}
    out.update(_book_beta(ix))
    return out


def _book_beta(ix: str) -> dict:
    """Beta, correlation and tracking error of the book (in euros) against the index (in euros), weekly, two years."""
    try:
        from . import bonds, riskmap, risk
        b = bonds._book()
        if not b["rows"]:
            return {}
        sym = INDICES[ix]["sym"]
        px = riskmap.prices([r["t"] for r in b["rows"]] + [sym])
        br, _ = bonds._book_returns(b, px)
        ix_eur = bonds._to_eur(px.get(sym) or {}, "USD", px)
        days = sorted(set(br) & set(ix_eur))
        # five-day windows, as everywhere else in the terminal
        lv, acc = [], 1.0
        for dd in days:
            acc *= 1 + br[dd]
            lv.append(acc)
        ixs = [ix_eur[dd] for dd in days]
        y = [math.log(lv[t] / lv[t - 5]) for t in range(5, len(lv), 5)]
        x = [math.log(ixs[t] / ixs[t - 5]) for t in range(5, len(ixs), 5)]
        f = risk.ols(y, x)
        if not f:
            return {}
        act = [a - b_ for a, b_ in zip(y, x)]
        return {"beta": round(f["beta"], 2), "corr": round(math.sqrt(max(f["r2"], 0)), 2),
                "te": round(statistics.pstdev(act) * math.sqrt(52) * 100, 1), "weeks": len(y)}
    except Exception:
        return {}


# --------------------------------------------------------------------------
# the build, the record, the alerts
# --------------------------------------------------------------------------

_JOB: dict = {}
_LOCK = threading.Lock()


def _build(ix: str) -> dict:
    cfg = INDICES[ix]
    st = _JOB.setdefault(ix, {})
    errs = {}

    def step(name, fn, default=None):
        st["phase"] = name
        try:
            return fn()
        except Exception as e:
            errs[name] = f"{e.__class__.__name__}: {e}"[:200]
            return default
    mem = members(ix)
    w_now = {r["sym"]: r["w"] for r in mem["rows"]}
    names = {r["sym"]: r["name"] for r in mem["rows"]}
    st["phase"] = f"reading three years of prices for {len(w_now)} members"
    df, vdf = _frame(list(w_now) + [cfg["sym"], cfg["ew"]], with_volume=True)
    if df.empty or cfg["sym"] not in df.columns:
        raise RuntimeError("Yahoo returned no prices for the index")
    idx_s = df[cfg["sym"]].dropna()
    b = step("breadth", lambda: breadth_series(df[[c for c in df.columns if c != cfg["ew"]]], cfg["sym"]))
    ud = step("up and down volume", lambda: updown_series(df[[c for c in df.columns if c != cfg["ew"]]], vdf, cfg["sym"]))
    del vdf
    wp = step("weights", lambda: weights_path(df, w_now))
    conc = step("concentration", lambda: concentration(wp)) if wp else None
    rows, idx_r = step("members", lambda: member_stats(df[[c for c in df.columns if c != cfg["ew"]]], w_now, cfg["sym"]), ([], {}))
    sec_of = _sector_map()
    for r in rows:
        r["name"] = names.get(r["sym"])
        r["sector"] = sec_of.get(r["sym"]) or "Other"
    st["phase"] = "reading the members' estimates"
    step("estimates", lambda: top_up_estimates([r["sym"] for r in sorted(rows, key=lambda r: -r["w"])],
                                               progress=lambda p: st.update(phase=p)))
    val = step("valuation", lambda: valuation(rows, ix), {})
    rev = step("revisions", lambda: revisions(rows), {})
    sea = step("season", lambda: season(rows), {})
    secs = sector_stats(rows)
    # the level
    from . import research as rs
    lvl = {}
    try:
        bars = rs.daily(cfg["sym"])["bars"]
        cc = [x[4] for x in bars]
        dd_ = [x[0] for x in bars]
        lv = cc[-1]
        ma = lambda k: statistics.fmean(cc[-k:]) if len(cc) >= k else None
        ath = max(cc)
        lvl = {"last": lv, "d": dd_[-1], "chg1d": _r((lv / cc[-2] - 1) * 100, 2), "ma50": _r(ma(50)), "ma200": _r(ma(200)),
               "dist50": _r((lv / ma(50) - 1) * 100), "dist200": _r((lv / ma(200) - 1) * 100) if ma(200) else None,
               "ath": ath, "ath_d": dd_[cc.index(ath)], "dd": _r((lv / ath - 1) * 100),
               "hi52": max(cc[-252:]), "lo52": min(cc[-252:]), "r": idx_r}
        for k, n_ in (("3y", 756), ("5y", 1260), ("10y", 2510)):
            if len(cc) > n_:
                lvl["r"][k] = _r((lv / cc[-n_ - 1] - 1) * 100, 1)
                lvl["r"][k + "_pa"] = _r(((lv / cc[-n_ - 1]) ** (252 / n_) - 1) * 100, 1)
        # With dividends: the ETF's adjusted close (after its small fee). Yahoo's
        # Nasdaq-100 total-return index has gaps; SPY and QQQ do not.
        trb = rs._safe(lambda: rs.daily(cfg["etf"])["bars"]) or []
        if len(trb) > 252:
            tc = [x[6] for x in trb]
            yr0 = next((i for i in range(len(trb) - 1, -1, -1) if trb[i][0][:4] < trb[-1][0][:4]), 0)
            lvl["tr_ytd"] = _r((tc[-1] / tc[yr0] - 1) * 100, 2)
            lvl["tr_1y"] = _r((tc[-1] / tc[-253] - 1) * 100, 2)
        chart = {"d": dd_, "c": [_r(x, 2) for x in cc]}
        ewb = rs._safe(lambda: rs.daily(cfg["ew"])["bars"]) or []
        ewm = {x[0]: x[4] for x in ewb}
        chart["ew"] = [_r(ewm.get(x), 2) for x in dd_]
    except Exception as e:
        errs["level"] = f"{e.__class__.__name__}: {e}"[:200]
        chart = None
    vol = step("volatility", lambda: volatility(ix, [float(v) for v in idx_s.values], [x.strftime("%Y-%m-%d") for x in idx_s.index]), {})
    opt = step("options", lambda: implied_moves(ix), {})
    cot = step("positioning", lambda: positioning(ix), {})
    longs = step("history", lambda: long_series(cfg["sym"]))
    hist = step("history study", lambda: history_study(longs["d"], longs["c"])) if longs else None
    # equal weight against cap weight
    ew = None
    if cfg["ew"] in df.columns:
        both = df[[cfg["sym"], cfg["ew"]]].dropna()
        if len(both) > 100:
            rat = both[cfg["ew"]] / both[cfg["sym"]]
            rat = rat / rat.iloc[0] * 100
            ew = {"d": [x.strftime("%Y-%m-%d") for x in rat.index], "v": [_r(v, 2) for v in rat.values],
                  "chg_1y": _r((rat.iloc[-1] / rat.iloc[-253] - 1) * 100, 1) if len(rat) > 253 else None}
    out = {"ix": ix, "name": cfg["name"], "built_at": time.time(), "asof": idx_s.index[-1].strftime("%Y-%m-%d"),
           "members_src": {k: mem.get(k) for k in ("source", "asof", "approx")}, "n_members": len(rows),
           "level": lvl, "chart": chart, "breadth": b, "updown": ud, "conc": _slim_conc(conc, rows), "ew": ew,
           "members": rows, "sectors": secs, "valuation": val,
           "revisions": rev, "season": sea, "vol": vol, "options": opt, "cot": cot, "history": hist, "errors": errs}
    out["contrib_sum"] = {k: round(sum(((r.get("c") or {}).get(k) or 0) for r in rows), 2) for k in PERIODS}
    out["sectors"] = [x for x in out["sectors"] if x["w"] >= 0.01]
    out["insights"] = insights(out)
    _record(ix, out)
    return out


def _slim_conc(conc: Optional[dict], rows: list[dict]) -> Optional[dict]:
    if not conc:
        return None
    top = sorted(rows, key=lambda r: -r["w"])[:10]
    return {**conc, "top": [{"sym": r["sym"], "name": r.get("name"), "w": round(r["w"], 2),
                             "ytd": (r.get("r") or {}).get("ytd")} for r in top],
            "top10_now": round(sum(r["w"] for r in top), 2),
            "eff_now": round(1 / sum((r["w"] / 100) ** 2 for r in rows), 1) if rows else None,
            "mag7_now": round(sum(r["w"] for r in rows if r["sym"] in MAG7), 2)}


def _fx(x, d: int = 1, sign: str = "", comma: bool = False) -> str:
    """
    A number as the page prints it: the shortest decimal rounded half away from
    zero, and a true minus. Python's own format rounds the binary value, so
    5.55 came out 5.5 here and 5,6 in the tile beside it.
    """
    q = Decimal(repr(float(x))).quantize(Decimal(1).scaleb(-d), rounding=ROUND_HALF_UP)
    if q == 0:
        q = abs(q)
    return format(q, f"{sign}{',' if comma else ''}.{d}f").replace("-", "−")


def _day(iso: str) -> str:
    """6 Oct 2026, the way the page writes a date."""
    try:
        d = date.fromisoformat(iso)
        return f"{d.day} {d.strftime('%b')} {d.year}"
    except Exception:
        return str(iso)


def ordinal(n) -> str:
    n = int(round(n))
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def insights(v: dict) -> list[dict]:
    """The page's first lines: each a fact from below, said plainly, with a tone."""
    out = []
    L, b, c, val = v.get("level") or {}, v.get("breadth") or {}, v.get("conc") or {}, v.get("valuation") or {}
    if L.get("dd") is not None:
        d200 = L.get("dist200") or 0
        trend = f"{_fx(abs(d200), 1)}% {'above' if d200 >= 0 else 'below'} its 200-day average"
        out.append({"tone": "good" if L["dd"] > -3 else ("warn" if L["dd"] > -10 else "bad"),
                    "text": (f"The {v['name']} closed at a record high, {trend}." if L["dd"] >= -0.05 else
                             f"The {v['name']} is {_fx(abs(L['dd']), 1)}% below its high of {_fx(L['ath'], 0, comma=True)} on {_day(L['ath_d'])}, {trend}.")})
    if b and b.get("p200"):
        p = b["p200"][-1]
        p_m = b["p200"][-22] if len(b["p200"]) > 22 else None
        narrow = L.get("dd") is not None and L["dd"] > -3 and p < 55
        out.append({"tone": "warn" if narrow or p < 30 else ("good" if p > 60 else "info"),
                    "text": f"{_fx(p, 0)}% of members are above their 200-day average"
                            + ((f" ({_fx(p - p_m, 0, sign='+')} points in a month)" if _fx(abs(p - p_m), 0) != "0" else " (unchanged on a month)") if p_m is not None else "")
                            + (": the index is near its high on fewer shares than usual, a narrow market." if narrow else ".")})
    ud = v.get("updown") or {}
    if ud.get("d") and (ud["upv"][-1] + ud["dnv"][-1]) > 0:
        from .flow import day_kind
        sh = ud["upv"][-1] / (ud["upv"][-1] + ud["dnv"][-1]) * 100
        k = day_kind(ud["upv"][-1], ud["dnv"][-1], ud["ptu"][-1], ud["ptd"][-1])
        if k:
            lab = {"90down": "a 90% downside day: panic selling, by the volume and by the points",
                   "90up": "a 90% upside day: the kind of buying that has answered panics",
                   "80down": "an 80% downside day", "80up": "an 80% upside day"}[k]
            out.append({"tone": "bad" if k.endswith("down") else "good",
                        "text": f"On {_day(ud['d'][-1])} {_fx(sh, 0)}% of the members' volume was in rising shares, "
                                f"{ud['adv'][-1]} up and {ud['dec'][-1]} down: {lab} (FLOW reads what followed such days)."})
    if c.get("top10_now"):
        s = c.get("top10") or []
        hi = max(s) if s else None
        out.append({"tone": "warn" if hi and c["top10_now"] >= hi - 0.3 else "info",
                    "text": f"The ten largest members are {_fx(c['top10_now'], 1)}% of the index"
                            + (" — the most in the two years rebuilt here." if hi and c["top10_now"] >= hi - 0.3 else ".")
                            + (f" The effective number of shares is {_fx(c['eff_now'], 0)}." if c.get("eff_now") else "")})
    if v.get("ew") and v["ew"].get("chg_1y") is not None:
        g = v["ew"]["chg_1y"]
        out.append({"tone": "info", "text": f"Equal weight {'beat' if g > 0 else 'lagged'} cap weight by {_fx(abs(g), 1)} points over a year: "
                                            + ("the average member did better than the giants." if g > 0 else "the giants carried the index.")})
    if val.get("fwd_pe"):
        out.append({"tone": "warn" if val["fwd_pe"] > 22 else "info",
                    "text": f"Forward P/E {_fx(val['fwd_pe'], 1)}"
                            + (f" (the ten largest {_fx(val['top10_fwd_pe'], 1)}, the rest {_fx(val['rest_fwd_pe'], 1)})" if val.get("top10_fwd_pe") and val.get("rest_fwd_pe") else "")
                            + (f"; an earnings yield of {_fx(val['earn_yield'], 1)}% against {_fx(val['us10y'], 2)}% on the ten-year." if val.get("us10y") else ".")})
    r = v.get("revisions") or {}
    if r.get("available"):
        out.append({"tone": "good" if r["up"] > r["down"] else "warn",
                    "text": f"Analysts raised next year's EPS for {r['up']} members and cut it for {r['down']} in thirty days; "
                            f"the index's forward earnings {_fx(r['eps_chg30'], 1, sign='+')}%."})
    s = v.get("season") or {}
    if s.get("reported"):
        out.append({"tone": "good" if (s.get("beat_pct") or 0) >= 70 else "info",
                    "text": f"{s['reported']} members ({_fx(s['reported_w'], 0)}% of the index) have reported: {_fx(s['beat_pct'], 0)}% beat"
                            + (f", earnings {_fx(s['growth_w'], 0, sign='+')}% on a year." if s.get("growth_w") is not None else ".")})
    vv = v.get("vol") or {}
    main = next((x for x in vv.get("curve") or [] if x["sym"] == INDICES[v["ix"]]["vol"]), None)
    if main:
        out.append({"tone": "warn" if vv.get("inverted") else "info",
                    "text": f"{main['sym'].lstrip('^')} {_fx(main['last'], 1)}, the {ordinal(main['pct10y'])} percentile of ten years"
                            + (f"; the index moved {_fx(vv['realised_21d'], 1)}% annualised over a month" if vv.get("realised_21d") is not None else "")
                            + ("; the curve is inverted: protection for the next month costs more than for three." if vv.get("inverted") else ".")})
    sk = next((x for x in vv.get("curve") or [] if x["sym"] == "^SKEW"), None)
    if sk and sk.get("pct10y") is not None and sk["pct10y"] >= 90:
        out.append({"tone": "warn", "text": f"SKEW {_fx(sk['last'], 0)}, the {ordinal(sk['pct10y'])} percentile of ten years: "
                                            "far-out puts, the insurance against a crash, are unusually dear."})
    co = v.get("cot") or {}
    if co.get("available"):
        out.append({"tone": "info", "text": f"In the {co['future']} future asset managers are net long {_fx(co['am_pct'], 0)}% of open interest "
                                            f"({ordinal(co['am_rank'])} percentile of five years), hedge funds {_fx(co['lf_pct'], 0, sign='+')}% "
                                            f"({ordinal(co['lf_rank'])})."})
    h = v.get("history") or {}
    sm = (h.get("similar") or {}).get("252")
    if sm:
        out.append({"tone": "info", "text": f"From {h['n_similar']} months since {h['from'][:4]} that stood like today "
                                            f"({h['bucket']}, {_fx(h['dist200'], 1, sign='+')}% against the 200-day line), the next year's median was "
                                            f"{_fx(sm['median'], 1, sign='+')}%, up {_fx(sm['pos'], 0)}% of the time."})
    return out


def _record(ix: str, v: dict) -> None:
    """Today's figures in idx_day; the first build also writes the two rebuilt years of breadth and concentration."""
    b, c = v.get("breadth") or {}, v.get("conc") or {}
    vol = {x["sym"]: x for x in (v.get("vol") or {}).get("curve") or []}
    with store.conn() as cn:
        have = cn.execute("SELECT COUNT(*) FROM idx_day WHERE ix = ? AND src = 'backfill'", (ix,)).fetchone()[0]
        if not have and b.get("d"):
            cd = {d: i for i, d in enumerate(c.get("d") or [])}
            for i, d in enumerate(b["d"][:-1]):
                j = cd.get(d)
                cn.execute("INSERT OR IGNORE INTO idx_day (ix, d, src, level, p200, p50, nh, nl, top10, eff_n, mag7) "
                           "VALUES (?, ?, 'backfill', ?, ?, ?, ?, ?, ?, ?, ?)",
                           (ix, d, (b.get("idx") or [None] * len(b["d"]))[i], b["p200"][i], b["p50"][i], b["nh"][i], b["nl"][i],
                            (c.get("top10") or [None] * 9999)[j] if j is not None else None,
                            (c.get("eff_n") or [None] * 9999)[j] if j is not None else None,
                            (c.get("mag7") or [None] * 9999)[j] if j is not None and c.get("mag7") else None))
        if b.get("d"):
            val, rev, cot = v.get("valuation") or {}, v.get("revisions") or {}, v.get("cot") or {}
            cn.execute("INSERT OR REPLACE INTO idx_day (ix, d, src, level, p200, p50, nh, nl, top10, eff_n, mag7, fwd_pe, trail_pe, "
                       "vol, vol3m, rev_up, rev_down, eps_chg30, am_pct, lf_pct) VALUES (?, ?, 'daily', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (ix, b["d"][-1], (v.get("level") or {}).get("last"), b["p200"][-1], b["p50"][-1], b["nh"][-1], b["nl"][-1],
                        c.get("top10_now"), c.get("eff_now"), c.get("mag7_now"), val.get("fwd_pe"), val.get("trail_pe"),
                        (vol.get(INDICES[ix]["vol"]) or {}).get("last"), (vol.get("^VIX3M") or {}).get("last"),
                        rev.get("up"), rev.get("down"), rev.get("eps_chg30"), cot.get("am_pct"), cot.get("lf_pct")))


def record_series(ix: str) -> dict:
    with store.conn() as cn:
        rows = cn.execute("SELECT d, fwd_pe, trail_pe, p200, top10, eps_chg30 FROM idx_day WHERE ix = ? ORDER BY d", (ix,)).fetchall()
    return {"d": [r[0] for r in rows], "fwd_pe": [r[1] for r in rows], "trail_pe": [r[2] for r in rows],
            "p200": [r[3] for r in rows], "top10": [r[4] for r in rows], "eps_chg30": [r[5] for r in rows]}


def _run(ix: str):
    st = _JOB.setdefault(ix, {})
    st.update(state="running", started=time.time(), phase="starting", error=None)
    try:
        v = _build(ix)
        store.cache_put(f"idx:view:{ix}", v)
        st.update(state="done", finished=time.time(), phase="")
    except Exception as e:
        st.update(state="error", finished=time.time(), error=f"{e.__class__.__name__}: {e}"[:300])


def start(ix: str) -> None:
    with _LOCK:
        st = _JOB.setdefault(ix, {})
        if st.get("state") == "running":
            return
        st["state"] = "running"
        threading.Thread(target=_run, args=(ix,), name=f"idx-{ix}", daemon=True).start()


def view(ix: str, rebuild: bool = False) -> dict:
    """The cached deep dive, the build behind it when it is a day old, or it is rebuilt on request."""
    if ix not in INDICES:
        raise KeyError(ix)
    from . import BUILD
    v = store.cache_get(f"idx:view:{ix}")
    fresh = bool(v) and v.get("_build") == BUILD and (v.get("_age_s") or 1e9) < TTL_VIEW
    if rebuild or not fresh:
        start(ix)
    st = dict(_JOB.get(ix) or {})
    if not v:
        return {"ix": ix, "name": INDICES[ix]["name"], "building": True, "job": st}
    try:                                   # the lines are read again from the build: cheap, and always today's wording
        v = {**v, "insights": insights(v)}
    except Exception:
        pass
    return {**v, "job": st, "stale": not fresh, "record": record_series(ix)}


def compare() -> dict:
    """The two side by side: the Nasdaq 100 against the S&P 500 over five years, and how much of one the other is."""
    from . import research as rs
    a = {b[0]: b[4] for b in rs.daily("^GSPC")["bars"]}
    n = {b[0]: b[4] for b in rs.daily("^NDX")["bars"]}
    days = sorted(set(a) & set(n))[-1260:]
    ratio = [n[d] / a[d] for d in days]
    base = ratio[0]
    ra = [math.log(a[days[i]] / a[days[i - 1]]) for i in range(1, len(days))]
    rn = [math.log(n[days[i]] / n[days[i - 1]]) for i in range(1, len(days))]
    corr, beta = [], []
    for i in range(63, len(ra) + 1):
        x, y = ra[i - 63:i], rn[i - 63:i]
        mx, my = statistics.fmean(x), statistics.fmean(y)
        cov = sum((p - mx) * (q - my) for p, q in zip(x, y))
        vx, vy = sum((p - mx) ** 2 for p in x), sum((q - my) ** 2 for q in y)
        corr.append(round(cov / math.sqrt(vx * vy), 3) if vx and vy else None)
        beta.append(round(cov / vx, 3) if vx else None)
    out = {"d": days, "ratio": [round(r / base * 100, 2) for r in ratio], "corr63": corr, "beta63": beta,
           "corr_d": days[63:]}
    try:
        s, q = members("spx")["rows"], members("ndx")["rows"]
        qs = {r["sym"] for r in q}
        out["ndx_in_spx"] = round(sum(r["w"] for r in s if r["sym"] in qs), 1)
        out["shared"] = sum(1 for r in s if r["sym"] in qs)
    except Exception:
        pass
    for ix in ("spx", "ndx"):
        v = store.cache_get(f"idx:view:{ix}") or {}
        out[ix] = {"fwd_pe": (v.get("valuation") or {}).get("fwd_pe"), "top10": (v.get("conc") or {}).get("top10_now"),
                   "p200": ((v.get("breadth") or {}).get("p200") or [None])[-1], "ytd": ((v.get("level") or {}).get("r") or {}).get("ytd"),
                   "r1y": ((v.get("level") or {}).get("r") or {}).get("1y"), "dd": (v.get("level") or {}).get("dd")}
    return out


# the nightly build, after New York's close
_LOOP: dict = {"t": None}


def _scan_busy(now: datetime) -> bool:
    """
    The watchlist's evening scan (22:45) reads Yahoo too, and writes the
    estimates the valuation and revisions here are read from: the evening
    build waits while it runs, and until 23:30 for one that has not started.
    """
    try:
        from . import watch
        return watch.JOB.get("state") == "running" or (watch.due() and (now.hour, now.minute) < (23, 30))
    except Exception:
        return False


def _loop():
    time.sleep(240)
    while True:
        try:
            for ix in INDICES:
                v = store.cache_get(f"idx:view:{ix}")
                from . import BUILD
                due = not v or v.get("_build") != BUILD or (v.get("_age_s") or 1e9) > TTL_VIEW
                now = datetime.now(_AMS)
                after_close = now.weekday() < 5 and (now.hour, now.minute) >= (22, 30)
                if due or (after_close and (v.get("_age_s") or 0) > 6 * 3600 and not _scan_busy(now)):
                    if (_JOB.get(ix) or {}).get("state") != "running":
                        _run(ix)
        except Exception:
            pass
        time.sleep(600)


def ensure():
    t = _LOOP["t"]
    if t and t.is_alive():
        return
    store.init_db()
    t = threading.Thread(target=_loop, name="idx-loop", daemon=True)
    _LOOP["t"] = t
    t.start()


def alerts() -> list[dict]:
    """For the home screen and the morning brief: breadth, concentration and the VIX curve at extremes. Read, never fetched."""
    out = []
    for ix, cfg in INDICES.items():
        v = store.cache_get(f"idx:view:{ix}") or {}
        b, c, L, vol = v.get("breadth") or {}, v.get("conc") or {}, v.get("level") or {}, v.get("vol") or {}
        if b.get("p200"):
            p = b["p200"][-1]
            if L.get("dd") is not None and L["dd"] > -3 and p < 50:
                out.append({"level": "warn", "code": "IDX", "text": f"{cfg['name']} near its high with only {_fx(p, 0)}% of members above their 200-day average",
                            "detail": "a narrow market: fewer shares are carrying the index"})
            elif p < 25:
                out.append({"level": "info", "code": "IDX", "text": f"{cfg['name']}: only {_fx(p, 0)}% of members above their 200-day average",
                            "detail": "washed out; such lows have often come near turns"})
            elif p > 85:
                out.append({"level": "info", "code": "IDX", "text": f"{cfg['name']}: {_fx(p, 0)}% of members above their 200-day average",
                            "detail": "broad, and stretched"})
        s = c.get("top10") or []
        if s and c.get("top10_now") and c["top10_now"] >= max(s) - 0.05:
            out.append({"level": "info", "code": "IDX", "text": f"{cfg['name']}: the ten largest members at {_fx(c['top10_now'], 1)}%, a two-year high",
                        "detail": "concentration is rising"})
        if ix == "spx" and vol.get("inverted"):
            out.append({"level": "warn", "code": "IDX", "text": f"The VIX curve is inverted ({_fx(vol.get('slope', 0), 2)})",
                        "detail": "near-term protection costs more than three-month: the market is pricing stress now"})
        cot = v.get("cot") or {}
        for k, lab in (("am_rank", "asset managers"), ("lf_rank", "hedge funds")):
            if cot.get(k) is not None and (cot[k] >= 97 or cot[k] <= 3):
                out.append({"level": "info", "code": "IDX", "text": f"{cfg['fut']}: {lab}' net position at a five-year {'high' if cot[k] >= 97 else 'low'}",
                            "detail": f"{_fx(cot['am_pct' if k == 'am_rank' else 'lf_pct'], 1, sign='+')}% of open interest"})
    return out


def brief_line() -> Optional[str]:
    bits = []
    for ix, cfg in INDICES.items():
        v = store.cache_get(f"idx:view:{ix}") or {}
        b, L, c = v.get("breadth") or {}, v.get("level") or {}, v.get("conc") or {}
        if not b.get("p200"):
            continue
        bits.append(f"{cfg['name']} {_fx(L.get('dd', 0), 1, sign='+')}% from its high, {_fx(b['p200'][-1], 0)}% of members above the 200-day"
                    + (f", top ten {_fx(c['top10_now'], 0)}%" if c.get("top10_now") else ""))
    return "; ".join(bits) or None
