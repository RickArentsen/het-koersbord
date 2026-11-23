"""
FLOW — what the big money is doing (build 51).

Five panels, each from a free source tested before it was built (6 November
2026), each saying how solid it is.

    breadth      Up and down volume. The S&P 500's members come from the IDX
                 build's own download, so they cost no extra request. From
                 them: Lowry's 90% days, the follow-through that has marked
                 bottoms, and Zweig's breadth thrust. The NYSE and Nasdaq
                 composites come from the WSJ's market diary, which keeps no
                 history, so it is saved each evening and the record starts
                 with this build. What followed a 90% day on the NYSE from
                 1965 to 2020 comes from unicorn.us.com's archive.
    hedge funds  The CFTC's Traders in Financial Futures: what leveraged funds
                 and asset managers hold in the index futures, the VIX and the
                 Treasury futures. Gross and net, against ten years, and how
                 fast they are cutting. Goldman's and Morgan Stanley's prime-
                 book figures, which the press quotes, are client notes, so
                 the headlines about them are collected from the news log.
    leverage     The OFR's Hedge Fund Monitor:
                 - Form PF: gross leverage by fund size and strategy,
                   borrowing, equity long and short exposure (quarterly, about
                   four months late);
                 - the Fed's dealer survey on hedge funds' use of leverage;
                 - sponsored repo;
                 - the leveraged funds' Treasury futures in ten-year
                   equivalents.
    dark pools   SqueezeMetrics' DIX is the share of dark-pool volume that is
                 short sales by market makers filling buyers: high means
                 buying. Its GEX is dealers' gamma: low or negative means
                 they add to moves. Cboe's put/call ratios come as a file per
                 session.
    your book    FINRA's daily short-sale volume for every US share you hold
                 or track through a turbo, against its own average. Also the
                 largest institutions' last 13F changes, from Yahoo's
                 ownership data, which the stock page already reads.

A background loop fetches everything after New York's close, one request at
a time. The page reads only what is stored, except the market diary during
New York's session, which it reads at most every five minutes.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
import statistics
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from . import net, store

AMS = ZoneInfo("Europe/Amsterdam")
NY = ZoneInfo("America/New_York")

WSJ_DIARY = ("https://www.wsj.com/market-data/stocks/marketsdiary?id=%7B%22application%22%3A%22WSJ%22%2C"
             "%22marketsDiaryType%22%3A%22overview%22%7D&type=mdc_marketsdiary")
WSJ_H = {"Accept": "application/json, text/plain, */*", "Referer": "https://www.wsj.com/market-data/stocks/marketsdiary"}
UNICORN = "http://unicorn.us.com/advdec/NYSE_{k}.csv"       # HTTP only: the certificate on HTTPS has expired
DIX_CSV = "https://squeezemetrics.com/monitor/static/DIX.csv"
CBOE = "https://cdn.cboe.com/data/us/options/market_statistics/daily/{d}_daily_options"
FINRA = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{d}.txt"
OFR = "https://data.financialresearch.gov/hf/v1/series/multifull?mnemonics={m}"
CFTC = "https://publicreporting.cftc.gov/resource/gpe5-46if.json?"
CFTC_FIELDS = ("report_date_as_yyyy_mm_dd,cftc_contract_market_code,open_interest_all,"
               "lev_money_positions_long,lev_money_positions_short,lev_money_positions_spread,"
               "asset_mgr_positions_long,asset_mgr_positions_short,dealer_positions_long_all,dealer_positions_short_all")
CFTC_FROM = "2015-01-01"

# (CFTC code, name, short, group, dollars per index point, the index)
CONTRACTS = [
    ("13874A", "E-mini S&P 500", "ES", "eq", 50, "^GSPC"),
    ("209742", "E-mini Nasdaq 100", "NQ", "eq", 20, "^NDX"),
    ("239742", "E-mini Russell 2000", "RTY", "eq", 50, "^RUT"),
    ("124603", "E-mini Dow", "YM", "eq", 5, "^DJI"),
    ("1170E1", "VIX futures", "VX", "vol", 1000, "^VIX"),
    ("042601", "2-year Treasury note", "ZT", "ust", None, None),
    ("044601", "5-year Treasury note", "ZF", "ust", None, None),
    ("043602", "10-year Treasury note", "ZN", "ust", None, None),
    ("043607", "Ultra 10-year note", "TN", "ust", None, None),
    ("020601", "Treasury bond", "ZB", "ust", None, None),
    ("020604", "Ultra Treasury bond", "UB", "ust", None, None),
]

OFR_SERIES = {
    "gne": "FPF-ALLQHF_GNE_SUM", "nav": "FPF-ALLQHF_NAV_SUM",
    "lev10": "FPF-ALLQHF_GAVN10_LEVERAGERATIO_AVERAGE", "lev11": "FPF-ALLQHF_GAVN11TO50_LEVERAGERATIO_AVERAGE",
    "lev51": "FPF-ALLQHF_GAVN51_LEVERAGERATIO_AVERAGE",
    "eq_l": "FPF-ASSETCLASS_EQUITIES_LGNE_SUM", "eq_s": "FPF-ASSETCLASS_EQUITIES_SGNE_SUM",
    "ust_l": "FPF-ASSETCLASS_LTREASURY_SUM", "ust_s": "FPF-ASSETCLASS_STREASURY_SUM",
    "pb": "FPF-BORROW_PRIMEBROKER_SUM", "repo": "FPF-BORROW_REPO_SUM", "secured": "FPF-BORROW_OTHERSECURED_SUM",
    "s_equity": "FPF-STRATEGY_EQUITY_LEVERAGERATIO_GAVWMEAN", "s_macro": "FPF-STRATEGY_MACRO_LEVERAGERATIO_GAVWMEAN",
    "s_rv": "FPF-STRATEGY_RV_LEVERAGERATIO_GAVWMEAN", "s_credit": "FPF-STRATEGY_CREDIT_LEVERAGERATIO_GAVWMEAN",
    "s_multi": "FPF-STRATEGY_MULTI_LEVERAGERATIO_GAVWMEAN", "s_futures": "FPF-STRATEGY_FUTURES_LEVERAGERATIO_GAVWMEAN",
    "s_event": "FPF-STRATEGY_EVENT_LEVERAGERATIO_GAVWMEAN",
    "scoos_lev": "SCOOS-NET_HF_LEVERAGEUSE", "scoos_terms": "SCOOS-NET_HF_CREDITTERMS",
    "spons_repo": "FICC-SPONSORED_REPO_VOL", "spons_rev": "FICC-SPONSORED_REVREPO_VOL",
    "tff_l": "TFF-LF_TREAS_LONG_POS10YREQV", "tff_s": "TFF-LF_TREAS_SHORT_POS10YREQV",
}
STRATEGIES = [("s_equity", "Equity"), ("s_macro", "Macro"), ("s_rv", "Relative value"), ("s_credit", "Credit"),
              ("s_multi", "Multi-strategy"), ("s_futures", "Managed futures"), ("s_event", "Event driven")]

TTL = {"cftc": 12 * 3600, "ofr": 24 * 3600, "dix": 6 * 3600, "unicorn": 120 * 86400}
PC_DAYS = 380          # calendar days of Cboe's files kept: a year of sessions, for the percentiles
SHORT_DAYS = 70        # calendar days of FINRA's files fetched back: about 45 sessions
SHORT_KEEP = 400
OWN_AGE = 7 * 86400    # an ownership read (13F, quarterly) is asked again after a week
WINDOW = 63            # sessions after a 90% down day in which a follow-through still counts
PAIR = 5               # two 80% up days this close together count as one 90% up day
HORIZONS = (5, 21, 63, 126, 252)
DEGROSS_4W = -10.0     # a four-week cut in hedge funds' equity-futures gross this deep is flagged...
DEGROSS_RANK = 5       # ...when it is also among the sharpest 5% of four-week changes in the record

# New York's holidays before the brief's own list starts (morning.HOLIDAYS holds 2026 on)
US_HOLIDAYS_2025 = {"2025-01-01", "2025-01-09", "2025-01-20", "2025-02-17", "2025-04-18", "2025-05-26",
                    "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27", "2025-12-25"}


def _f(x) -> Optional[float]:
    try:
        v = float(str(x).replace(",", ""))
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _r(x, d: int = 2):
    return round(x, d) if x is not None and math.isfinite(x) else None


def _rank(vals: list, cur) -> Optional[float]:
    """Where `cur` stands among `vals`: the share of them below it, 0 to 100."""
    v = [x for x in vals if x is not None]
    if not v or cur is None:
        return None
    return round(sum(1 for x in v if x < cur) / len(v) * 100, 0)


def _since(dates: list, vals: list, cur, lower: bool = True) -> Optional[str]:
    """The last earlier date whose value was as low as `cur` (or as high, with lower=False); None: never in the record."""
    for i in range(len(vals) - 2, -1, -1):
        v = vals[i]
        if v is not None and (v <= cur if lower else v >= cur):
            return dates[i]
    return None


def _ema(xs: list, n: int) -> list:
    a, out, e = 2 / (n + 1), [], None
    for x in xs:
        e = x if e is None else e + a * (x - e)
        out.append(e)
    return out


def now_ny() -> datetime:
    return datetime.now(NY)


def us_session(d: date) -> bool:
    from .morning import HOLIDAYS
    iso = d.isoformat()
    return d.weekday() < 5 and iso not in (HOLIDAYS.get("US") or {}) and iso not in US_HOLIDAYS_2025


def sessions(since: date, until: date) -> list[str]:
    """New York's sessions from `since` to `until`, both included, newest first."""
    out, d = [], until
    while d >= since:
        if us_session(d):
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out


def _sym(s: str) -> str:
    """One spelling for a US share: FINRA writes BRK/B, Yahoo BRK-B, the filings BRK.B."""
    return (s or "").strip().upper().replace("/", ".").replace("-", ".")


# --------------------------------------------------------------------------
# reading each source (pure: bytes in, numbers out, so each can be tested)
# --------------------------------------------------------------------------

def _int(s) -> Optional[int]:
    v = _f(s)
    return int(v) if v is not None else None


def parse_diary(js: dict) -> dict:
    """
    The WSJ's market diary: advancing and declining issues, new highs and lows and up and
    down volume for the NYSE and the Nasdaq composite. Its timestamp ("4:00 PM EST 11/06/26")
    dates the reading; one at or after 16:00 is the session's final count.
    """
    data = js.get("data") or {}
    ts = str(data.get("timestamp") or "")
    m = re.search(r"(\d{1,2}):(\d{2})\s*([AP]M)\s+[A-Z]{2,4}\s+(\d{1,2})/(\d{1,2})/(\d{2,4})", ts)
    if not m:
        raise ValueError(f"the diary's timestamp did not read: {ts!r}")
    hh, mm = int(m.group(1)) % 12 + (12 if m.group(3) == "PM" else 0), int(m.group(2))
    yy = int(m.group(6))
    d = date(yy + 2000 if yy < 100 else yy, int(m.group(4)), int(m.group(5)))
    sets = {}
    for s in data.get("instrumentSets") or []:
        lab = ((s.get("headerFields") or [{}])[0]).get("label")
        sets[lab] = {i.get("name"): i for i in s.get("instruments") or []}
    out = {"d": d.isoformat(), "asof": f"{d.isoformat()} {hh:02d}:{mm:02d}", "final": hh * 60 + mm >= 16 * 60}
    for ex in ("NYSE", "NASDAQ"):
        g = lambda grp, name: _int((sets.get(grp) or {}).get(name, {}).get(ex))
        out[ex] = {"adv": g("Issues", "Advancing"), "dec": g("Issues", "Declining"), "unch": g("Issues", "Unchanged"),
                   "nh": g("Issues At", "New Highs"), "nl": g("Issues At", "New Lows"),
                   "upv": g("Share Volume", "Advancing"), "dnv": g("Share Volume", "Declining"),
                   "unv": g("Share Volume", "Unchanged")}
    if not out["NYSE"]["upv"] or not out["NYSE"]["dnv"]:
        raise ValueError("the diary had no NYSE volume")
    return out


CBOE_KEYS = {"SUM OF ALL PRODUCTS": "tot", "EQUITY OPTIONS": "eq", "INDEX OPTIONS": "ix",
             "EXCHANGE TRADED PRODUCTS": "etp", "SPX + SPXW": "spx", "CBOE VOLATILITY INDEX (VIX)": "vix"}


def parse_cboe(js: dict) -> dict:
    """Cboe's session file: the put and call VOLUME of each product group (the ratios are worked out here)."""
    out = {}
    for name, k in CBOE_KEYS.items():
        for row in js.get(name) or []:
            if str(row.get("name", "")).upper() == "VOLUME":
                out[k + "_p"], out[k + "_c"] = _f(row.get("put")), _f(row.get("call"))
    if not out.get("tot_p") or not out.get("tot_c"):
        raise ValueError("Cboe's file had no total put and call volume")
    return out


def parse_finra(text: str, keep: set) -> tuple[dict, tuple]:
    """FINRA's consolidated short-sale volume: {symbol: (short, total)} for `keep`, and the whole market's (short, total)."""
    rows, ms, mt = {}, 0.0, 0.0
    for line in text.splitlines():
        p = line.split("|")
        if len(p) < 5 or not p[0].isdigit():
            continue
        sh, tot = _f(p[2]), _f(p[4])
        if sh is None or not tot:
            continue
        ms += sh
        mt += tot
        s = _sym(p[1])
        if s in keep:
            rows[s] = (sh, tot)
    if mt <= 0:
        raise ValueError("FINRA's file had no volume")
    return rows, (ms, mt)


def parse_dix(text: str) -> list[tuple]:
    """SqueezeMetrics' file: (date, S&P 500, DIX, GEX) a session, since May 2011."""
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        d, px, dx, gx = (r.get("date") or "")[:10], _f(r.get("price")), _f(r.get("dix")), _f(r.get("gex"))
        if d and dx is not None:
            out.append((d, px, dx, gx))
    if len(out) < 500:
        raise ValueError(f"the DIX file had {len(out)} rows")
    return sorted(out)


def parse_unicorn(text: str) -> dict:
    """unicorn.us.com's NYSE file: {ISO date: shares}. Zeros mean the archive stopped (February 2020)."""
    out = {}
    for line in text.splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) >= 2 and len(p[0]) == 8 and p[0].isdigit():
            v = _f(p[1])
            if v and v > 0:
                out[f"{p[0][:4]}-{p[0][4:6]}-{p[0][6:]}"] = v
    return out


def parse_ofr(js: dict) -> dict:
    """The OFR's multifull answer: {key: [[date, value], ...]} for every series in OFR_SERIES, nulls dropped."""
    back = {v: k for k, v in OFR_SERIES.items()}
    out = {}
    for mn, body in (js or {}).items():
        k = back.get(mn)
        ts = ((body or {}).get("timeseries") or {}).get("aggregation") or []
        if k:
            out[k] = [[str(d)[:10], float(v)] for d, v in ts if v is not None]
    if not out.get("gne") or not out.get("nav"):
        raise ValueError("the OFR returned no Form PF exposure")
    return out


def parse_cftc(rows: list) -> dict:
    """The CFTC's rows as weeks per contract: open interest and each group's long and short."""
    by: dict = {}
    for r in rows:
        d, code, oi = (r.get("report_date_as_yyyy_mm_dd") or "")[:10], r.get("cftc_contract_market_code"), _f(r.get("open_interest_all"))
        if not d or not code or not oi:
            continue
        g = lambda k: _f(r.get(k)) or 0.0
        by.setdefault(code, []).append({"d": d, "oi": oi, "lf_l": g("lev_money_positions_long"), "lf_s": g("lev_money_positions_short"),
                                        "lf_sp": g("lev_money_positions_spread"), "am_l": g("asset_mgr_positions_long"),
                                        "am_s": g("asset_mgr_positions_short"), "dl_l": g("dealer_positions_long_all"),
                                        "dl_s": g("dealer_positions_short_all")})
    for w in by.values():
        w.sort(key=lambda x: x["d"])
    return by


# --------------------------------------------------------------------------
# breadth: Lowry's 90% days, the follow-through, Zweig's thrust
# --------------------------------------------------------------------------

def day_kind(upv, dnv, ptu=None, ptd=None) -> Optional[str]:
    """
    Lowry's test. A 90% down day has at least 90% of the up-and-down volume in falling
    shares AND at least 90% of the points lost against those gained; a 90% up day is the
    mirror. Where the points are not known (the NYSE archive, the diary) the volume alone
    decides. 80% days are by volume.
    """
    tot = (upv or 0) + (dnv or 0)
    if tot <= 0:
        return None
    u = upv / tot * 100
    pts = ptu / (ptu + ptd) * 100 if ptu is not None and ptd is not None and (ptu + ptd) > 0 else None
    if u <= 10 and (pts is None or pts <= 10):
        return "90down"
    if u >= 90 and (pts is None or pts >= 90):
        return "90up"
    if u <= 20:
        return "80down"
    if u >= 80:
        return "80up"
    return None


def episodes(d: list, kinds: list, window: int = WINDOW, pair: int = PAIR) -> list[dict]:
    """
    Lowry's sequence. A 90% down day, or a run of them, is a panic. It is answered by a 90%
    up day, or by two 80% up days no more than `pair` sessions apart, within `window` sessions
    of the last down day. A down day after an answer, or after the window has run out, starts
    a new episode.
    """
    out, cur, last80 = [], None, None
    for i, k in enumerate(kinds):
        if k == "90down":
            if cur is None or cur["answered"] or i - cur["il"] > window:
                cur = {"start": d[i], "last": d[i], "downs": 1, "i0": i, "il": i, "answered": None, "how": None, "ia": None}
                out.append(cur)
            else:
                cur.update(last=d[i], il=i, downs=cur["downs"] + 1)
            last80 = None
            continue
        if cur is None or cur["answered"] or i - cur["il"] > window:
            continue
        if k == "90up":
            cur.update(answered=d[i], how="a 90% up day", ia=i)
        elif k == "80up":
            if last80 is not None and i - last80 <= pair:
                cur.update(answered=d[i], how="two 80% up days", ia=i)
            last80 = i
    n = len(d)
    for e in out:
        e["open"] = not e["answered"] and (n - 1 - e["il"]) <= window
        # sessions to the answer; for one still open, sessions so far; one never answered has none
        e["sessions"] = e["ia"] - e["il"] if e["ia"] is not None else (n - 1 - e["il"] if e["open"] else None)
    return out


def zweig(d: list, adv: list, dec: list, lo: float = 0.40, hi: float = 0.615, within: int = 10, warm: int = 20) -> tuple[list, list]:
    """
    Zweig's breadth thrust: the ten-day exponential average of advancers' share of advancers
    and decliners going from below 40% to above 61.5% within ten sessions. Rare, and it has
    tended to come early in a new rise. Returns the thrusts and the average itself.
    """
    e = _ema([(a / (a + b)) if (a + b) else 0.5 for a, b in zip(adv, dec)], 10)
    out, last = [], -999
    for i in range(max(warm, 1), len(e)):
        if e[i] > hi >= e[i - 1]:
            lows = [j for j in range(max(0, i - within), i) if e[j] < lo]
            if lows and i - last > 20:
                out.append({"d": d[i], "low_d": d[lows[0]], "low": _r(min(e[j] for j in lows) * 100, 1), "ema": _r(e[i] * 100, 1)})
                last = i
    return out, e


def _fwd_stats(c: list, idx: list, h: int) -> Optional[dict]:
    f = sorted((c[i + h] / c[i] - 1) * 100 for i in idx if i + h < len(c))
    if len(f) < 3:
        return None
    return {"n": len(f), "median": round(statistics.median(f), 2), "mean": round(statistics.fmean(f), 2),
            "pos": round(sum(1 for x in f if x > 0) / len(f) * 100, 0)}


def nyse_study(uv: dict, dv: dict, spx: dict, window: int = WINDOW, cluster: int = 21,
               an: Optional[dict] = None, dn: Optional[dict] = None) -> dict:
    """
    What the S&P 500 did after the NYSE's 90% days, 1965 to February 2020. Volume only:
    the archive has no points gained and lost. A panic counts once, from its first down day;
    a follow-through from the day that answered it; 90% up days once a month. With the
    advancing and declining issues (`an`, `dn`), Zweig's breadth thrusts over the same years.
    """
    days = [x for x in sorted(uv) if x in dv and x in spx]
    if len(days) < 2000:
        return {"available": False, "why": f"only {len(days)} sessions matched the S&P 500's history"}
    c = [spx[x] for x in days]
    kinds = [day_kind(uv[x], dv[x]) for x in days]
    eps = episodes(days, kinds, window)
    downs = [e["i0"] for e in eps]
    answered = [e["ia"] for e in eps if e["ia"] is not None]
    ups, last = [], -999
    for i, k in enumerate(kinds):
        if k == "90up" and i - last >= cluster:
            ups.append(i)
            last = i
    every = list(range(0, len(days), cluster))
    groups = {"down": downs, "answered": answered, "up": ups, "all": every}
    thrusts = []
    if an and dn:
        pos = {x: i for i, x in enumerate(days)}
        zd = [x for x in days if x in an and x in dn]
        thrusts = zweig(zd, [an[x] for x in zd], [dn[x] for x in zd])[0]
        groups["zweig"] = [pos[t["d"]] for t in thrusts]
    return {"available": True, "from": days[0], "to": days[-1], "n_days": {k: sum(1 for x in kinds if x == k) for k in ("90down", "90up", "80down", "80up")},
            "thrusts": [{**t, "f252": _r((c[pos[t["d"]] + 252] / c[pos[t["d"]]] - 1) * 100, 1) if pos[t["d"]] + 252 < len(c) else None}
                        for t in thrusts][::-1] if thrusts else [],
            "episodes": len(eps), "answered_share": _r(len(answered) / len(eps) * 100, 0) if eps else None,
            "median_wait": statistics.median([e["sessions"] for e in eps if e["ia"] is not None]) if answered else None,
            "stats": {g: {str(h): _fwd_stats(c, ix, h) for h in HORIZONS} for g, ix in groups.items()},
            "recent": [{"start": e["start"], "downs": e["downs"], "answered": e["answered"], "how": e["how"], "sessions": e["sessions"],
                        "f63": _r((c[e["i0"] + 63] / c[e["i0"]] - 1) * 100, 1) if e["i0"] + 63 < len(c) else None,
                        "f252": _r((c[e["i0"] + 252] / c[e["i0"]] - 1) * 100, 1) if e["i0"] + 252 < len(c) else None}
                       for e in eps[-10:]][::-1]}


def _closed_at(day: str) -> float:
    """When New York's session of `day` closed, as a timestamp (16:00, or 13:00 on an early close)."""
    from .morning import EARLY_CLOSE
    hh = 13 if day in (EARLY_CLOSE.get("US") or {}) else 16
    d = date.fromisoformat(day)
    return datetime(d.year, d.month, d.day, hh, 0, tzinfo=NY).timestamp()


def spx_breadth(ix: str = "spx") -> dict:
    """The members' up and down volume from the IDX build: each session's share, kind and the signals on them."""
    v = store.cache_get(f"idx:view:{ix}") or {}
    u = v.get("updown") or {}
    if not u.get("d"):
        return {"available": False, "why": "IDX's next build adds the members' volume (it runs each evening after New York closes)"}
    d, upv, dnv, ptu, ptd = u["d"], u["upv"], u["dnv"], u["ptu"], u["ptd"]
    share = [_r(a / (a + b) * 100, 1) if (a + b) else None for a, b in zip(upv, dnv)]
    pts = [_r(a / (a + b) * 100, 1) if (a + b) else None for a, b in zip(ptu, ptd)]
    kinds = [day_kind(a, b, p, q) for a, b, p, q in zip(upv, dnv, ptu, ptd)]
    partial = (v.get("built_at") or 0) < _closed_at(d[-1]) + 600
    # a session still trading is shown, but no signal is read from it
    k_eff = kinds[:-1] + [None] if partial else kinds
    eps = episodes(d, k_eff)
    th, ema = zweig(d[:-1] if partial else d, u["adv"][:-1] if partial else u["adv"], u["dec"][:-1] if partial else u["dec"])
    lvl = {}
    ch = (v.get("chart") or {})
    if ch.get("d"):
        lvl = dict(zip(ch["d"], ch["c"]))
    last_lvl = ch["c"][-1] if ch.get("c") else None
    events = []
    for i, k in enumerate(kinds):
        if k and not (partial and i == len(d) - 1):
            p0 = lvl.get(d[i])
            prev = lvl.get(d[i - 1]) if i else None
            events.append({"d": d[i], "kind": k, "share": share[i], "pts": pts[i], "adv": u["adv"][i], "dec": u["dec"][i],
                           "ratio": _r(upv[i] / dnv[i], 2) if dnv[i] else None,
                           "day": _r((p0 / prev - 1) * 100, 2) if p0 and prev else None,
                           "since": _r((last_lvl / p0 - 1) * 100, 1) if p0 and last_lvl else None})
    last_ep = eps[-1] if eps else None
    return {"available": True, "ix": ix, "name": v.get("name"), "built_at": v.get("built_at"), "partial": partial,
            "d": d, "share": share, "pts": pts, "kind": kinds, "adv": u["adv"], "dec": u["dec"], "n": u.get("n"),
            "ratio_last": _r(upv[-1] / dnv[-1], 2) if dnv[-1] else None,
            "ema10": [_r(x * 100, 1) for x in ema] + ([None] if partial else []),
            "events": events[::-1][:40], "episode": last_ep and {k: last_ep[k] for k in ("start", "last", "downs", "answered", "how", "open", "sessions")},
            "episodes": [{k: e[k] for k in ("start", "last", "downs", "answered", "how", "open", "sessions")} for e in eps][::-1][:12],
            "zweig": th[::-1], "zweig_now": _r(ema[-1] * 100, 1) if ema else None}


def diary_rows(days: int = 400) -> list[dict]:
    cut = (date.today() - timedelta(days=days)).isoformat()
    with store.conn() as c:
        rows = c.execute("SELECT * FROM flow_diary WHERE d >= ? ORDER BY d", (cut,)).fetchall()
    return [dict(r) for r in rows]


def diary_view(live: Optional[dict] = None) -> dict:
    """The NYSE's and the Nasdaq's sessions as recorded here, with today's live reading on top during the session."""
    rows = diary_rows()
    out = {}
    for ex in ("NYSE", "NASDAQ"):
        r = [x for x in rows if x["exch"] == ex]
        if live and live.get(ex) and not any(x["d"] == live["d"] and x["final"] for x in r):
            r = [x for x in r if x["d"] != live["d"]] + [{"d": live["d"], "exch": ex, "asof": live["asof"], "final": 0,
                                                        "adv": live[ex]["adv"], "decl": live[ex]["dec"], "nh": live[ex]["nh"], "nl": live[ex]["nl"],
                                                        "upv": live[ex]["upv"], "dnv": live[ex]["dnv"]}]
        sess = []
        for x in r:
            tot = (x["upv"] or 0) + (x["dnv"] or 0)
            sess.append({"d": x["d"], "final": bool(x["final"]), "asof": x["asof"], "adv": x["adv"], "dec": x["decl"],
                         "nh": x["nh"], "nl": x["nl"], "upv": x["upv"], "dnv": x["dnv"],
                         "share": _r(x["upv"] / tot * 100, 1) if tot else None,
                         "ratio": _r(x["upv"] / x["dnv"], 2) if x["dnv"] else None,
                         "kind": day_kind(x["upv"], x["dnv"]) if x["final"] else None})
        fin = [s for s in sess if s["final"]]
        th = zweig([s["d"] for s in fin], [s["adv"] or 0 for s in fin], [s["dec"] or 0 for s in fin])[0] if len(fin) >= 25 else []
        out[ex] = {"sessions": sess[::-1][:30], "n": len(fin), "since": fin[0]["d"] if fin else None,
                   "episode": (episodes([s["d"] for s in fin], [s["kind"] for s in fin]) or [None])[-1], "zweig": th[::-1]}
    return out


# --------------------------------------------------------------------------
# hedge funds' futures
# --------------------------------------------------------------------------

def _last_close(sym: str) -> Optional[float]:
    """The index's latest close from the research cache; asked of Yahoo only when nothing is stored."""
    bars = (store.cache_get(f"rs:daily:{sym}") or {}).get("bars") or []
    if bars:
        return bars[-1][4]
    if _busy():
        return None
    try:
        from . import research as rs
        bars = rs.daily(sym).get("bars") or []
        return bars[-1][4] if bars else None
    except Exception:
        return None


def positioning(by: dict, px: dict) -> dict:
    """
    Per contract, leveraged funds' (hedge funds, CTAs) and asset managers' positions:
    net as a share of open interest against five years, gross (long plus short, spreads
    left out) and how fast it moved. The equity futures are also added up in dollars, at
    today's index levels, so the total moves with positions and not with prices.
    """
    rows, eq = [], {}
    for code, name, short, grp, mult, isym in CONTRACTS:
        w = by.get(code) or []
        if len(w) < 60:
            continue
        g = [x["lf_l"] + x["lf_s"] for x in w]
        net_ = [x["lf_l"] - x["lf_s"] for x in w]
        npct = [n / x["oi"] * 100 for n, x in zip(net_, w)]
        am = [(x["am_l"] - x["am_s"]) / x["oi"] * 100 for x in w]
        c4 = [(g[i] / g[i - 4] - 1) * 100 if i >= 4 and g[i - 4] else None for i in range(len(g))]
        five = slice(-260, None)
        r = {"code": code, "name": name, "short": short, "grp": grp, "d": w[-1]["d"], "oi": w[-1]["oi"],
             "lf_long": w[-1]["lf_l"], "lf_short": w[-1]["lf_s"], "lf_net": net_[-1], "lf_net_pct": _r(npct[-1], 1),
             "lf_net_rank": _rank(npct[five], npct[-1]), "lf_gross": g[-1], "lf_gross_4w": _r(c4[-1], 1),
             "lf_gross_4w_rank": _rank(c4[five], c4[-1]),
             "lf_gross_peak": _r((g[-1] / max(g[-52:]) - 1) * 100, 1) if max(g[-52:]) else None,
             "lf_net_4w": _r(npct[-1] - npct[-5], 1),
             "am_net_pct": _r(am[-1], 1), "am_rank": _rank(am[five], am[-1]), "am_4w": _r(am[-1] - am[-5], 1),
             "series": {"d": [x["d"] for x in w[-520:]], "lf_net_pct": [_r(x, 1) for x in npct[-520:]],
                        "am_net_pct": [_r(x, 1) for x in am[-520:]], "lf_gross": g[-520:]}}
        if mult and px.get(isym):
            r["usd_per"] = mult * px[isym]
            if grp == "eq":
                for x, gg, nn in zip(w, g, net_):
                    e = eq.setdefault(x["d"], [0.0, 0.0, 0])
                    e[0] += gg * r["usd_per"]
                    e[1] += nn * r["usd_per"]
                    e[2] += 1
        rows.append(r)
    out = {"rows": rows, "asof": max((r["d"] for r in rows), default=None)}
    n_eq = sum(1 for r in rows if r["grp"] == "eq" and r.get("usd_per"))
    days = [d for d in sorted(eq) if eq[d][2] == n_eq]
    if n_eq and len(days) > 60:
        gross = [eq[d][0] / 1e9 for d in days]
        netv = [eq[d][1] / 1e9 for d in days]
        c4 = [(gross[i] / gross[i - 4] - 1) * 100 if i >= 4 else None for i in range(len(gross))]
        cur = c4[-1]
        rank = _rank([x for x in c4 if x is not None], cur)
        out["equity"] = {"d": days[-520:], "gross": [_r(x, 1) for x in gross[-520:]], "net": [_r(x, 1) for x in netv[-520:]],
                         "gross_now": _r(gross[-1], 1), "net_now": _r(netv[-1], 1), "chg4": _r(cur, 1), "chg4_rank": rank,
                         "chg4_since": _since(days, c4, cur) if cur is not None and cur < 0 else None,
                         "chg1": _r((gross[-1] / gross[-2] - 1) * 100, 1),
                         "peak52": _r((gross[-1] / max(gross[-52:]) - 1) * 100, 1),
                         "peak52_d": days[-52:][gross[-52:].index(max(gross[-52:]))],
                         "degrossing": cur is not None and cur <= DEGROSS_4W and rank is not None and rank <= DEGROSS_RANK,
                         "contracts": n_eq, "from": days[0]}
    return out


FLOW_WORDS = re.compile(
    r"\b(de-?gross\w*|de-?lever\w*|gross leverage|net leverage|prime brok\w*|prime book|hedge funds? (?:sell|sold|dump|cut|unwind|"
    r"flee|exit|short|pile|crowd|positioning|exposure)\w*|crowded trades?|short squeeze|short covering|"
    r"cta selling|systematic (?:selling|funds|sellers)|forced selling|margin calls?|capitulat\w*|breadth thrust|"
    r"90% (?:down|up)\w* day|dark pools?|basis trade|commitments of traders|positioning data)\b", re.I)


def press(days: int = 10, limit: int = 30) -> list[dict]:
    """Headlines on hedge funds' positioning from the news log: where Goldman's and Morgan Stanley's prime-book numbers surface."""
    from . import news
    out = []
    try:
        for it in news.logged(time.time() - days * 86400):
            text = f"{it.get('headline', '')} {it.get('summary', '')}"
            if FLOW_WORDS.search(text):
                out.append({"headline": it.get("headline"), "source": it.get("source"), "url": it.get("url"), "ts": it.get("ts")})
    except Exception:
        return []
    out.sort(key=lambda x: -(x.get("ts") or 0))
    return out[:limit]


# --------------------------------------------------------------------------
# leverage (OFR)
# --------------------------------------------------------------------------

def leverage(ser: dict) -> dict:
    """Form PF and the rest, as the page reads them: latest, a quarter and a year before, and the record."""
    def s(k):
        return ser.get(k) or []

    def card(pairs, scale=1.0):
        if not pairs:
            return None
        v = [p[1] / scale for p in pairs]
        last = v[-1]
        yago = next((p[1] / scale for p in reversed(pairs) if p[0] <= str(int(pairs[-1][0][:4]) - 1) + pairs[-1][0][4:]), None)
        mx = max(v)
        return {"d": pairs[-1][0], "last": _r(last, 2), "prev": _r(v[-2], 2) if len(v) > 1 else None, "yago": _r(yago, 2),
                "max": _r(mx, 2), "max_d": pairs[v.index(mx)][0], "rank": _rank(v, last), "from": pairs[0][0]}
    nav = dict(s("nav"))
    agg = [[d, v / nav[d]] for d, v in s("gne") if nav.get(d)]
    eql, eqs = dict(s("eq_l")), dict(s("eq_s"))
    eqd = [d for d in sorted(eql) if d in eqs and nav.get(d)]
    eq_gross = [[d, (eql[d] + eqs[d]) / nav[d]] for d in eqd]
    eq_net = [[d, (eql[d] - eqs[d]) / nav[d]] for d in eqd]
    eq_ratio = [[d, (eql[d] - eqs[d]) / (eql[d] + eqs[d]) * 100] for d in eqd if eql[d] + eqs[d]]
    tl, ts = dict(s("tff_l")), dict(s("tff_s"))
    tff = [[d, _r(tl[d] / 1e9, 1), _r(ts[d] / 1e9, 1)] for d in sorted(tl) if d in ts]
    rp = s("spons_repo")
    # sponsored repo is daily: kept weekly for the chart, the latest day for the card
    rp_w = [p for i, p in enumerate(rp) if i == len(rp) - 1 or p[0][:4] + str(date.fromisoformat(p[0]).isocalendar()[1]) !=
            rp[i + 1][0][:4] + str(date.fromisoformat(rp[i + 1][0]).isocalendar()[1])]
    return {"agg": card(agg), "agg_s": [[d, _r(v, 2)] for d, v in agg],
            "lev10": card(s("lev10")), "lev11": card(s("lev11")), "lev51": card(s("lev51")),
            "lev_s": {k: [[d, _r(v, 2)] for d, v in s(k)] for k in ("lev10", "lev11", "lev51")},
            "eq_gross": card(eq_gross), "eq_net": card(eq_net), "eq_ratio": card(eq_ratio),
            "eq_s": {"d": eqd, "gross": [_r(v, 2) for _, v in eq_gross], "net": [_r(v, 2) for _, v in eq_net]},
            "strategies": [dict(card(s(k)) or {}, k=k, name=nm) for k, nm in STRATEGIES if s(k)],
            "borrow": {k: card(s(k), 1e9) for k in ("pb", "repo", "secured")},
            "scoos": {k: s(k)[-16:] for k in ("scoos_lev", "scoos_terms")},
            "repo": card(rp, 1e9), "repo_s": [[d, _r(v / 1e9, 1)] for d, v in rp_w[-520:]],
            "tff": tff[-520:], "tff_card": card([[t[0], t[2]] for t in tff]) if tff else None,
            "gne": card(s("gne"), 1e12), "nav": card(s("nav"), 1e12)}


# --------------------------------------------------------------------------
# dark pools and options
# --------------------------------------------------------------------------

def dark(rows: list) -> dict:
    """DIX and GEX: the last session against five years, and two years for the chart."""
    if not rows:
        return {"available": False}
    d = [r[0] for r in rows]
    dix = [r[2] for r in rows]
    gex = [r[3] for r in rows]
    five = slice(-1260, None)
    cur, g = dix[-1], gex[-1]
    lo5 = min(dix[five])
    # the lowest of the last ten sessions, and how far back one as low as it lies
    j = len(dix) - 10 + min(range(min(10, len(dix))), key=lambda k: dix[-10:][k]) if len(dix) >= 10 else dix.index(min(dix))
    rl = {"d": d[j], "v": _r(dix[j] * 100, 1), "since": _since(d[:j + 1], dix[:j + 1], dix[j])}
    return {"available": True, "d": d[-1], "dix": _r(cur * 100, 1), "dix_rank": _rank(dix[five], cur),
            "low5": _r(lo5 * 100, 1), "low5_d": d[five][dix[five].index(lo5)], "recent_low": rl,
            "recent": [[x, _r(y * 100, 1)] for x, y in zip(d[-10:], dix[-10:])],
            "dix_low_since": _since(d, dix, cur), "dix_high_since": _since(d, dix, cur, lower=False),
            "dix20": _r(statistics.fmean(dix[-20:]) * 100, 1), "gex": _r(g / 1e9, 2) if g is not None else None,
            "gex_rank": _rank(gex[five], g), "gex_low_since": _since(d, gex, g) if g is not None else None,
            "spx": rows[-1][1], "from": d[0],
            "chart": {"d": d[-504:], "dix": [_r(x * 100, 2) for x in dix[-504:]], "gex": [_r(x / 1e9, 2) if x is not None else None for x in gex[-504:]],
                      "spx": [r[1] for r in rows[-504:]]}}


def pc_rows(days: int = PC_DAYS) -> list[dict]:
    cut = (date.today() - timedelta(days=days)).isoformat()
    with store.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM flow_pc WHERE ok = 1 AND d >= ? ORDER BY d", (cut,)).fetchall()]


def putcall(rows: list) -> dict:
    """The put/call ratios a session, their five-day average and where it stands in its year."""
    if len(rows) < 6:
        return {"available": False, "n": len(rows)}
    ratio = lambda r, k: _r(r[k + "_p"] / r[k + "_c"], 3) if r.get(k + "_p") is not None and r.get(k + "_c") else None
    ser = {k: [ratio(r, k) for r in rows] for k in ("tot", "eq", "ix", "spx", "vix", "etp")}

    def avg(xs, n, i):
        w = [x for x in xs[max(0, i - n + 1):i + 1] if x is not None]
        return statistics.fmean(w) if len(w) == min(n, i + 1) and w else None
    a5 = {k: [avg(v, 5, i) for i in range(len(v))] for k, v in ser.items()}
    last = {k: v[-1] for k, v in ser.items()}
    yr = slice(-252, None)
    return {"available": True, "d": rows[-1]["d"], "n": len(rows), "last": last,
            "avg5": {k: _r(v[-1], 3) for k, v in a5.items()},
            "rank5": {k: _rank(v[yr], v[-1]) for k, v in a5.items()},
            "chart": {"d": [r["d"] for r in rows], "tot": ser["tot"], "eq": ser["eq"], "tot5": [_r(x, 3) for x in a5["tot"]],
                      "eq5": [_r(x, 3) for x in a5["eq"]]}}


# --------------------------------------------------------------------------
# your holdings: short volume and the institutions
# --------------------------------------------------------------------------

def mine_syms() -> dict:
    """{US ticker: {held, name, how}}: every share you hold and every turbo's underlying (POL's list, so both agree)."""
    try:
        from . import pol
        return pol.my_tickers() or {}
    except Exception:
        return {}


def short_rows(syms: list, days: int = SHORT_KEEP) -> dict:
    cut = (date.today() - timedelta(days=days)).isoformat()
    out: dict = {}
    if not syms:
        return out
    with store.conn() as c:
        q = ",".join("?" * len(syms))
        for r in c.execute(f"SELECT d, sym, short, total FROM flow_short WHERE d >= ? AND sym IN ({q}) AND total > 0 ORDER BY d",
                           (cut, *syms)).fetchall():
            out.setdefault(r["sym"], []).append((r["d"], r["short"], r["total"]))
    return out


def short_stats(rows: list, market: dict) -> Optional[dict]:
    """The last session's short share of volume against the stock's own previous twenty sessions."""
    if not rows:
        return None
    ratio = [s / t * 100 for _, s, t in rows]
    prior = ratio[-21:-1]
    mu = statistics.fmean(prior) if len(prior) >= 10 else None
    sd = statistics.pstdev(prior) if len(prior) >= 10 else None
    d = rows[-1][0]
    return {"d": d, "ratio": _r(ratio[-1], 1), "avg20": _r(mu, 1), "z": _r((ratio[-1] - mu) / sd, 1) if mu is not None and sd else None,
            "vol": rows[-1][2], "mkt": _r(market.get(d), 1), "n": len(rows),
            "series": {"d": [r[0] for r in rows[-60:]], "ratio": [_r(x, 1) for x in ratio[-60:]]}}


def institutions(sym: str) -> Optional[dict]:
    """The ten largest institutions from the stock page's ownership read (Yahoo, from 13F): who added, who cut."""
    v = store.cache_get(f"rs:own:{sym.replace('.', '-')}") or store.cache_get(f"rs:own:{sym}")
    if not v:
        return None
    inst = v.get("institutions") or []
    if not inst:
        return {"n": 0, "age_s": v.get("_age_s")}
    ch = [x.get("chg") for x in inst if x.get("chg") is not None]
    return {"n": len(inst), "added": sum(1 for x in ch if x > 0.5), "cut": sum(1 for x in ch if x < -0.5),
            "held": _r(sum(x.get("pct") or 0 for x in inst), 1), "asof": max((x.get("date") or "" for x in inst), default=None),
            "top": [{k: x.get(k) for k in ("holder", "pct", "chg", "date")} for x in inst[:10]], "age_s": v.get("_age_s"),
            "inst_pct": (v.get("major") or {}).get("institutionsPercentHeld")}


def mine_view() -> dict:
    mine = mine_syms()
    syms = sorted(mine)
    rows = short_rows(syms + ["*"])
    market = {d: s / t * 100 for d, s, t in rows.get("*", [])}
    out = []
    for s in syms:
        st = short_stats(rows.get(s) or [], market)
        out.append({"sym": s, "held": mine[s].get("held"), "name": mine[s].get("name"), "how": mine[s].get("how"),
                    "short": st, "inst": institutions(s)})
    out.sort(key=lambda r: -(r["short"]["z"] if r["short"] and r["short"].get("z") is not None else -99))
    mk = rows.get("*") or []
    return {"rows": out, "market": {"d": mk[-1][0], "ratio": _r(market[mk[-1][0]], 1)} if mk else None,
            "sessions": len(mk)}


# --------------------------------------------------------------------------
# collecting: one source at a time, after New York's close
# --------------------------------------------------------------------------

JOB: dict = {"state": "idle"}
_LOCK = threading.Lock()
_LAST: dict = {}


def _status(src: str, ok: bool, detail: str = "", error: Optional[str] = None) -> None:
    st = store.cache_get("flow:status") or {}
    st.pop("_age_s", None)
    st.pop("_build", None)
    st[src] = {"ok": ok, "at": time.time(), "detail": detail, "error": error,
               "last_ok": time.time() if ok else (st.get(src) or {}).get("last_ok")}
    store.cache_put("flow:status", st)


def _busy() -> bool:
    """The watchlist's scan or an index build is reading Yahoo: the steps that ask Yahoo wait (POL's test, shared)."""
    try:
        from . import pol
        return pol.scan_busy()
    except Exception:
        return False


def _due(k: str, ttl: float, force: bool) -> bool:
    return force or store.cache_age(k) > ttl


def step_cftc(force: bool = False) -> str:
    if not _due("flow:cftc", TTL["cftc"], force):
        return "fresh"
    w = "cftc_contract_market_code in(" + ",".join(f"'{c[0]}'" for c in CONTRACTS) + f") AND report_date_as_yyyy_mm_dd >= '{CFTC_FROM}'"
    url = CFTC + urllib.parse.urlencode({"$select": CFTC_FIELDS, "$where": w, "$order": "report_date_as_yyyy_mm_dd", "$limit": 20000})
    by = parse_cftc(json.loads(net.get(url, timeout=60)))
    if len(by) < 5:
        raise RuntimeError(f"the CFTC returned {len(by)} contracts")
    px = {c[5]: _last_close(c[5]) for c in CONTRACTS if c[5]}
    pos = positioning(by, px)
    store.cache_put("flow:cftc", {"by": by, "fetched_at": time.time()})
    store.cache_put("flow:pos", pos)
    return f"{len(by)} contracts to {pos['asof']}"


def step_ofr(force: bool = False) -> str:
    if not _due("flow:ofr", TTL["ofr"], force):
        return "fresh"
    ser = parse_ofr(json.loads(net.get(OFR.format(m=",".join(OFR_SERIES.values())), timeout=60)))
    store.cache_put("flow:ofr", {"series": ser, "fetched_at": time.time()})
    return f"{len(ser)} series, Form PF to {ser['gne'][-1][0]}"


def step_dix(force: bool = False) -> str:
    if not _due("flow:dix", TTL["dix"], force):
        return "fresh"
    rows = parse_dix(net.get(DIX_CSV, timeout=60).decode("utf-8", "replace"))
    store.cache_put("flow:dix", {"rows": [r for r in rows if r[0] >= "2016-01-01"], "fetched_at": time.time()})
    return f"{len(rows)} sessions to {rows[-1][0]}"


def step_unicorn(force: bool = False) -> str:
    if not _due("flow:nyse", TTL["unicorn"], force):
        return "fresh"
    got = {}
    for i, k in enumerate(("advv", "declv", "advn", "decln")):      # up and down volume, advancing and declining issues
        if i:
            time.sleep(1.5)
        got[k] = parse_unicorn(net.get(UNICORN.format(k=k), timeout=60).decode("utf-8", "replace"))
    from . import idx
    lg = idx.long_series("^GSPC")
    st = nyse_study(got["advv"], got["declv"], dict(zip(lg["d"], lg["c"])), an=got["advn"], dn=got["decln"])
    if not st.get("available"):
        raise RuntimeError(st.get("why") or "the study had too little data")
    store.cache_put("flow:nyse", st)
    return f"{st['episodes']} panics from {st['from'][:4]} to {st['to'][:7]}"


def _diary_fetch() -> dict:
    return parse_diary(json.loads(net.get(WSJ_DIARY, WSJ_H, timeout=20)))


def _diary_save(r: dict) -> None:
    with store.conn() as c:
        for ex in ("NYSE", "NASDAQ"):
            x = r[ex]
            old = c.execute("SELECT final FROM flow_diary WHERE d = ? AND exch = ?", (r["d"], ex)).fetchone()
            if old and old["final"] and not r["final"]:
                continue                         # a session's final count is never replaced by a reading from inside it
            c.execute("INSERT OR REPLACE INTO flow_diary (d, exch, adv, decl, unch, nh, nl, upv, dnv, unv, asof, final) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (r["d"], ex, x["adv"], x["dec"], x["unch"], x["nh"], x["nl"], x["upv"], x["dnv"], x["unv"], r["asof"], int(r["final"])))


def diary_due(now: Optional[datetime] = None) -> Optional[str]:
    """The session whose final count should be read now, or None: after 16:15 New York, or before the next open."""
    now = now or now_ny()
    t = now.hour * 60 + now.minute
    d = now.date()
    if us_session(d) and t >= 16 * 60 + 15:
        want = d
    elif not us_session(d) or t < 9 * 60:
        want = d - timedelta(days=1)          # a weekend, a holiday, or the morning before the open: the last session
        while not us_session(want):
            want -= timedelta(days=1)
    else:
        return None
    with store.conn() as c:
        r = c.execute("SELECT final FROM flow_diary WHERE d = ? AND exch = 'NYSE'", (want.isoformat(),)).fetchone()
    return None if r and r["final"] else want.isoformat()


def step_diary(force: bool = False) -> str:
    want = diary_due()
    if not want and not force:
        return "recorded"
    if not force and time.time() - _LAST.get("diary", 0) < (3 * 3600 if _LAST.get("diary_other") == want else 15 * 60):
        return "asked a moment ago"
    _LAST["diary"] = time.time()
    r = _diary_fetch()
    _diary_save(r)
    # the diary answered with another session than the one wanted: asked again in three hours, not every quarter
    _LAST["diary_other"] = want if want and r["d"] != want else None
    store.cache_put("flow:diary:live", r)
    return f"{r['d']} at {r['asof'][11:]}{' (final)' if r['final'] else ''}"


def live_diary() -> Optional[dict]:
    """During New York's session, the diary as it stands, read at most every five minutes when the page asks."""
    now = now_ny()
    t = now.hour * 60 + now.minute
    if not (us_session(now.date()) and 9 * 60 + 30 <= t < 16 * 60 + 15):
        return None
    try:
        return store.cache_through("flow:diary:live", 300, _diary_fetch)
    except Exception:
        return None


def _have(table: str, since: str, col: str = "d") -> set:
    with store.conn() as c:
        q = f"SELECT DISTINCT {col} FROM {table} WHERE {col} >= ?" + (" AND sym = '*'" if table == "flow_short" else "")
        return {r[0] for r in c.execute(q, (since,)).fetchall()}


def _targets(days: int, ready_at: tuple, table: str) -> list[str]:
    """Sessions in the window with no row yet, newest first; today's only once its file should be out."""
    now = now_ny()
    until = now.date() if (now.hour, now.minute) >= ready_at else now.date() - timedelta(days=1)
    since = (now.date() - timedelta(days=days))
    have = _have(table, since.isoformat())
    return [d for d in sessions(since, until) if d not in have]


def _missing(d: str) -> bool:
    """A file refused for a session more than three days ago is recorded as missing; a recent one is asked again later."""
    return (now_ny().date() - date.fromisoformat(d)).days > 3


def _refused(r: dict) -> bool:
    """The publisher said there is no such file (403 or 404), rather than failing to answer."""
    return bool(re.search(r"\b40[34]\b", str(r.get("error") or "")))


_REFUSED: dict = {}


def _backfill(who: str, what: str, todo: list, url, keep, none_row, pause: float) -> tuple[int, int]:
    """
    Each session's file, newest first, one at a time. A file refused for an old session is
    recorded as missing (a holiday the list does not know, a file never published) only when
    the publisher answered for other sessions in the same run, or refused it three runs
    running. Three refusals in a row with nothing read is a block, not three holidays: the
    step stops there and records nothing, so the sessions are asked again later.
    """
    got, fails, refused = 0, 0, []
    for i, d in enumerate(todo):
        if i:
            time.sleep(pause)
        JOB["phase"] = f"{what} for {d} ({i + 1} of {len(todo)})"
        r = net.fetch(url(d), timeout=60)
        if not r["ok"]:
            fails += 1
            if _missing(d) and _refused(r):
                refused.append(d)
                _REFUSED[(who, d)] = _REFUSED.get((who, d), 0) + 1
            if fails >= 3 and not got:
                raise RuntimeError(f"{who} refused three files in a row ({r.get('error')})")
            continue
        fails = 0
        keep(d, r["body"])
        got += 1
    none = [d for d in refused if got or _REFUSED.get((who, d), 0) >= 3]
    for d in none:
        none_row(d)
    return got, len(none)


def step_pc(force: bool = False, budget: int = 60) -> str:
    todo = _targets(PC_DAYS, (17, 30), "flow_pc")[: (300 if force else budget)]

    def keep(d, body):
        x = parse_cboe(json.loads(body))
        with store.conn() as c:
            c.execute("INSERT OR REPLACE INTO flow_pc (d, ok, " + ", ".join(x) + ") VALUES (?, 1, " + ",".join("?" * len(x)) + ")",
                      (d, *x.values()))

    def none_row(d):
        with store.conn() as c:
            c.execute("INSERT OR REPLACE INTO flow_pc (d, ok) VALUES (?, 0)", (d,))
    got, none = _backfill("Cboe", "Cboe's put/call file", todo, lambda d: CBOE.format(d=d), keep, none_row, 1.2)
    left = len(_targets(PC_DAYS, (17, 30), "flow_pc"))
    return f"{got} read, {none} without a file, {left} to go" if todo else "up to date"


def short_universe() -> set:
    """The shares whose short volume is kept: yours, the S&P 500's and the Nasdaq 100's."""
    keep = set(mine_syms())
    for ix in ("spx", "ndx"):
        for r in (store.cache_get(f"idx:members:{ix}") or {}).get("rows") or []:
            keep.add(_sym(r["sym"]))
    return keep


def step_short(force: bool = False, budget: int = 8) -> str:
    todo = _targets(SHORT_DAYS, (18, 30), "flow_short")[: (50 if force else budget)]
    syms = short_universe()

    def keep(d, body):
        rows, (ms, mt) = parse_finra(body.decode("utf-8", "replace"), syms)
        with store.conn() as c:
            c.executemany("INSERT OR REPLACE INTO flow_short (d, sym, short, total) VALUES (?,?,?,?)",
                          [(d, s, a, b) for s, (a, b) in rows.items()] + [(d, "*", ms, mt)])

    def none_row(d):
        with store.conn() as c:
            c.execute("INSERT OR REPLACE INTO flow_short (d, sym, short, total) VALUES (?, '*', NULL, NULL)", (d,))
    got, none = _backfill("FINRA", "FINRA's short volume", todo, lambda d: FINRA.format(d=d.replace("-", "")), keep, none_row, 2)
    with store.conn() as c:
        c.execute("DELETE FROM flow_short WHERE d < ?", ((date.today() - timedelta(days=SHORT_KEEP)).isoformat(),))
    left = len(_targets(SHORT_DAYS, (18, 30), "flow_short"))
    return f"{got} read, {none} without a file, {left} to go" if todo else "up to date"


def step_own(force: bool = False, per_run: int = 3) -> str:
    """A few holdings' ownership a run, the oldest read first, one at a time and only while nothing else is reading Yahoo."""
    todo = []
    for s in sorted(mine_syms()):
        y = s.replace(".", "-")
        age = store.cache_age(f"rs:own:{y}")
        if age > OWN_AGE:
            todo.append((age, y))
    if not todo:
        return "every holding read this week"
    todo.sort(reverse=True)
    from . import research as rs
    done = []
    for i, (_, y) in enumerate(todo[:per_run]):
        if _busy():
            break
        if i:
            time.sleep(3)
        JOB["phase"] = f"the institutions in {y}"
        store.cache_put(f"rs:own:{y}", rs._load_ownership(y))
        done.append(y)
    if not done:
        return "waiting for Yahoo to be free"
    return f"{', '.join(done)} read, {len(todo) - len(done)} to go"


STEPS = [("cftc", "CFTC positioning", step_cftc), ("ofr", "OFR leverage", step_ofr), ("dix", "SqueezeMetrics DIX", step_dix),
         ("nyse", "NYSE history", step_unicorn), ("diary", "WSJ market diary", step_diary), ("pc", "Cboe put/call", step_pc),
         ("short", "FINRA short volume", step_short), ("own", "13F holders", step_own)]


def run(force: bool = False) -> dict:
    """Every step that is due, one after another; one failing stops only itself."""
    store.init_db()
    JOB.update(state="running", started=time.time(), error=None, force=force, results={})
    for k, label, fn in STEPS:
        JOB["phase"] = label
        try:
            msg = fn(force)
            JOB["results"][k] = msg
            if msg not in ("fresh", "recorded", "asked a moment ago"):
                _status(k, True, msg)
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"[:240]
            JOB["results"][k] = err
            _status(k, False, error=err)
    _CACHE.clear()
    JOB.update(state="done", finished=time.time(), phase="")
    return dict(JOB)


def start(force: bool = True) -> dict:
    with _LOCK:
        if JOB.get("state") == "running":
            return dict(JOB)
        JOB["state"] = "running"
        threading.Thread(target=run, args=(force,), name="flow-run", daemon=True).start()
    return dict(JOB)


_LOOP: dict = {"t": None}


def _loop():
    time.sleep(90)
    while True:
        try:
            if JOB.get("state") != "running":
                run(False)
        except Exception:
            pass
        time.sleep(600)


def ensure():
    t = _LOOP["t"]
    if t and t.is_alive():
        return
    store.init_db()
    t = threading.Thread(target=_loop, name="flow-loop", daemon=True)
    _LOOP["t"] = t
    t.start()


# --------------------------------------------------------------------------
# the page, the alerts, the brief, the house fear & greed
# --------------------------------------------------------------------------

_CACHE: dict = {}


def _memo(k: str, ttl: float, fn):
    hit = _CACHE.get(k)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    v = fn()
    _CACHE[k] = (time.time(), v)
    return v


def _safe(fn, default=None):
    try:
        return fn()
    except Exception as e:
        return default if default is not None else {"available": False, "why": f"{e.__class__.__name__}: {e}"[:200]}


def digest() -> dict:
    """The few figures the alerts, the brief and the fear & greed index read; recomputed at most every five minutes."""
    def build():
        b = _safe(spx_breadth)
        pos = store.cache_get("flow:pos") or {}
        dk = _safe(lambda: dark((store.cache_get("flow:dix") or {}).get("rows") or []))
        pc = _safe(lambda: putcall(pc_rows()))
        return {"breadth": b, "equity": pos.get("equity"), "pos_asof": pos.get("asof"), "dark": dk, "pc": pc}
    return _memo("digest", 300, build)


def view() -> dict:
    from . import BUILD
    ensure()
    live = live_diary()
    pos = store.cache_get("flow:pos") or {}
    ofr = (store.cache_get("flow:ofr") or {}).get("series") or {}
    dix_rows = (store.cache_get("flow:dix") or {}).get("rows") or []
    st = store.cache_get("flow:status") or {}
    for k in ("_age_s", "_build"):
        st.pop(k, None)
    out = {"build": BUILD, "now": time.time(), "job": {k: JOB.get(k) for k in ("state", "phase", "started", "finished", "error", "results")},
           "status": st, "labels": {k: lab for k, lab, _ in STEPS},
           "breadth": {"spx": _safe(spx_breadth), "ndx": _safe(lambda: spx_breadth("ndx")), "diary": _safe(lambda: diary_view(live)),
                       "live": live, "nyse": store.cache_get("flow:nyse") or {"available": False}},
           "positioning": {**pos, "press": press()} if pos else {"available": False, "press": press()},
           "leverage": _safe(lambda: leverage(ofr)) if ofr else {"available": False},
           "dark": _safe(lambda: dark(dix_rows)), "putcall": _safe(lambda: putcall(pc_rows())),
           "mine": _safe(mine_view)}
    out["insights"] = _safe(lambda: insights(out), [])
    return out


def _day(iso: Optional[str]) -> str:
    try:
        d = date.fromisoformat(str(iso)[:10])
        return f"{d.day} {d.strftime('%b')}" + ("" if d.year == date.today().year else f" {d.year}")
    except Exception:
        return str(iso or "")


def _ord(n) -> str:
    n = int(round(n))
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def _ep_text(e: dict) -> str:
    if e.get("answered"):
        return f"answered on {_day(e['answered'])} by {e['how']}, {e['sessions']} sessions after the last down day"
    if e.get("open"):
        return f"no follow-through yet: {e['sessions']} session{'s' if e['sessions'] != 1 else ''} since the last down day"
    return "never answered within three months"


def insights(v: dict) -> list[dict]:
    """The page's first lines, each a fact from below."""
    out = []
    b = (v.get("breadth") or {}).get("spx") or {}
    if b.get("available") and b.get("d"):
        s, k = b["share"][-1], b["kind"][-1]
        when = "so far today" if b.get("partial") else f"on {_day(b['d'][-1])}"
        lab = {"90down": "a 90% downside day", "90up": "a 90% upside day", "80down": "an 80% downside day", "80up": "an 80% upside day"}.get(k)
        out.append({"tone": "bad" if k in ("90down", "80down") else "good" if k in ("90up", "80up") else "info",
                    "text": f"Among the S&P 500's members, {s:.0f}% of the volume was in rising shares {when}"
                            + (f" ({b['ratio_last']:.1f} to 1)" if b.get("ratio_last") and b["ratio_last"] >= 1 else
                               f" ({1 / b['ratio_last']:.1f} to 1 down)" if b.get("ratio_last") else "")
                            + (f": {lab}{', by the volume and by the points' if k.startswith('90') else ''}." if lab else ".")})
        e = b.get("episode")
        if e and (e.get("open") or (e.get("answered") and e["answered"] >= b["d"][max(0, len(b["d"]) - 30)])):
            out.append({"tone": "warn" if e.get("open") else "good",
                        "text": f"The last panic began on {_day(e['start'])} ({e['downs']} day{'s' if e['downs'] != 1 else ''} of 90% selling); "
                                + _ep_text(e) + "."})
        if b.get("zweig") and b["zweig"][0]["d"] >= b["d"][max(0, len(b["d"]) - 30)]:
            z = b["zweig"][0]
            out.append({"tone": "good", "text": f"A Zweig breadth thrust on {_day(z['d'])}: the ten-day average of advancers went from "
                                                f"{z['low']:.0f}% to {z['ema']:.0f}% within ten sessions."})
    eq = (v.get("positioning") or {}).get("equity")
    if eq:
        cut = (eq.get("chg4") or 0) < -5
        tail = ""
        if cut:
            tail = (f"; the sharpest four-week cut since {_day(eq['chg4_since'])}" if eq.get("chg4_since")
                    else f"; the sharpest four-week cut since the record starts ({_day(eq['from'])})")
        out.append({"tone": "bad" if eq.get("degrossing") else "warn" if cut else "info",
                    "text": f"Hedge funds' gross in the equity index futures is ${eq['gross_now']:,.0f}bn at today's prices, "
                            f"{eq['chg4']:+.0f}% in four weeks and {eq['peak52']:+.0f}% from its peak of {_day(eq['peak52_d'])}"
                            + tail + f" (CFTC, to {_day(v['positioning'].get('asof'))})."})
    lv = v.get("leverage") or {}
    if lv.get("agg"):
        a, t10 = lv["agg"], lv.get("lev10") or {}
        out.append({"tone": "info", "text": f"Form PF: hedge funds' gross exposure was {a['last']:.1f} times their capital at {_day(a['d'])} "
                                            f"({a['prev']:.1f} a quarter before, {a['yago']:.1f} a year before)"
                                            + (f"; the ten largest averaged {t10['last']:.1f}× leverage, against {t10['prev']:.1f}." if t10.get("last") else ".")})
    if lv.get("eq_ratio") and lv.get("eq_gross"):
        er, eg = lv["eq_ratio"], lv["eq_gross"]
        if (er.get("rank") or 50) <= 10 or (eg.get("rank") or 50) >= 90:
            def where(c):
                return (f"the lowest since {c['from'][:4]}" if c["rank"] <= 0 else f"the highest since {c['from'][:4]}"
                        if c["rank"] >= 99.5 else f"the {_ord(c['rank'])} percentile since {c['from'][:4]}")
            out.append({"tone": "warn", "text": f"Their equity books: gross {eg['last']:.1f} times capital ({where(eg)}), net only "
                                                f"{er['last']:.0f}% of gross ({where(er)}): large and heavily hedged, the shape that has "
                                                "to be cut on both sides at once when it is cut."})
    dk = v.get("dark") or {}
    if dk.get("available"):
        ten = (dk.get("recent") or [[dk["d"]]])[0][0]
        lo = dk.get("dix_low_since")
        far = lo and lo < ten          # "the lowest since" only when it reaches back past ten sessions
        rl = dk.get("recent_low") or {}
        # a low a few sessions ago that reaches far back is worth saying beside today's figure
        back = rl.get("d") and rl["d"] != dk["d"] and (rl.get("since") is None or rl["since"] < ten)
        out.append({"tone": "warn" if (dk.get("dix_rank") or 50) <= 5 else "good" if (dk.get("dix_rank") or 50) >= 95 else "info",
                    "text": f"DIX {dk['dix']:.1f}% on {_day(dk['d'])}, the {_ord(dk['dix_rank'])} percentile of five years"
                            + (f", the lowest since {_day(lo)}" if far and (dk.get("dix_rank") or 50) <= 5 else "")
                            + (f" ({rl['v']:.1f}% on {_day(rl['d'])} was the lowest since "
                               + (_day(rl["since"]) if rl.get("since") else f"the record began, {_day(dk['from'])}") + ")" if back else "")
                            + f"; dealers' gamma ${dk['gex']:.1f}bn ({_ord(dk['gex_rank'])} percentile)"
                            + (": negative, so their hedging adds to moves." if (dk.get("gex") or 0) < 0 else ".")})
    pc = v.get("putcall") or {}
    if pc.get("available"):
        out.append({"tone": "warn" if (pc["rank5"].get("tot") or 50) >= 90 else "info",
                    "text": f"Put/call: {pc['last']['tot']:.2f} overall and {pc['last']['eq']:.2f} on single stocks on {_day(pc['d'])}; "
                            f"the five-day average {pc['avg5']['tot']:.2f}, the {_ord(pc['rank5']['tot'])} percentile of its year."})
    return out


def alerts() -> list[dict]:
    """For the home screen and the morning brief: 90% days, follow-through, thrusts, de-grossing, DIX extremes. Read, never fetched."""
    try:
        g = digest()
    except Exception:
        return []
    out = []
    b = g.get("breadth") or {}
    if b.get("available"):
        d = b["d"]
        recent = set(d[-2:])
        for ev in b.get("events") or []:
            if ev["d"] in recent and ev["kind"] in ("90down", "90up"):
                out.append({"level": "warn" if ev["kind"] == "90down" else "info", "code": "FLOW",
                            "text": f"{_day(ev['d'])}: a 90% {'downside' if ev['kind'] == '90down' else 'upside'} day among the S&P 500's members",
                            "detail": f"{ev['share']:.0f}% of the volume in rising shares, {ev['adv']} up and {ev['dec']} down · FLOW", "href": "/m/FLOW"})
        e = b.get("episode") or {}
        if e.get("answered") in recent:
            out.append({"level": "info", "code": "FLOW", "text": f"The selling of {_day(e['start'])} answered by {e['how']}",
                        "detail": "Lowry's follow-through, which has tended to mark the low · FLOW", "href": "/m/FLOW"})
        for z in b.get("zweig") or []:
            if z["d"] in set(d[-3:]):
                out.append({"level": "info", "code": "FLOW", "text": f"A Zweig breadth thrust on {_day(z['d'])}",
                            "detail": f"advancers' ten-day average from {z['low']:.0f}% to {z['ema']:.0f}% in ten sessions · FLOW", "href": "/m/FLOW"})
    eq = g.get("equity") or {}
    if eq.get("degrossing") and g.get("pos_asof") and (date.today() - date.fromisoformat(g["pos_asof"])).days <= 10:
        out.append({"level": "warn", "code": "FLOW", "text": f"Hedge funds cut their equity-futures gross {eq['chg4']:+.0f}% in four weeks",
                    "detail": (f"the sharpest since {_day(eq['chg4_since'])}" if eq.get("chg4_since") else "the sharpest in the record")
                              + f" · CFTC to {_day(g['pos_asof'])} · FLOW", "href": "/m/FLOW"})
    dk = g.get("dark") or {}
    if dk.get("available") and dk.get("dix_rank") is not None and (dk["dix_rank"] <= 3 or dk["dix_rank"] >= 97) \
            and (date.today() - date.fromisoformat(dk["d"])).days <= 4:
        out.append({"level": "info", "code": "FLOW", "text": f"DIX {dk['dix']:.1f}%, a five-year {'low' if dk['dix_rank'] <= 3 else 'high'}",
                    "detail": ("dark-pool buying has dried up" if dk["dix_rank"] <= 3 else "heavy dark-pool buying, which has tended to precede gains")
                              + " · FLOW", "href": "/m/FLOW"})
    return out[:4]


def brief_line() -> Optional[str]:
    try:
        g = digest()
    except Exception:
        return None
    bits = []
    b = g.get("breadth") or {}
    if b.get("available") and not b.get("partial"):
        s, k = b["share"][-1], b["kind"][-1]
        bits.append(f"{s:.0f}% of the S&P 500 members' volume rose on {_day(b['d'][-1])}"
                    + ({"90down": " (a 90% downside day)", "90up": " (a 90% upside day)"}.get(k, "")))
        e = b.get("episode") or {}
        if e.get("open"):
            bits.append(f"no follow-through yet to the selling of {_day(e['start'])}")
        elif e.get("answered") and e["answered"] in b["d"][-5:]:
            bits.append(f"the selling of {_day(e['start'])} answered by {e['how']}")
    eq = g.get("equity") or {}
    if eq.get("chg4") is not None and eq["chg4"] <= -5:
        bits.append(f"hedge funds' equity-futures gross {eq['chg4']:+.0f}% in four weeks")
    dk = g.get("dark") or {}
    if dk.get("available") and dk.get("dix_rank") is not None and (dk["dix_rank"] <= 5 or dk["dix_rank"] >= 95):
        bits.append(f"DIX {dk['dix']:.1f}% ({_ord(dk['dix_rank'])} percentile of five years)")
    return ("Flow: " + "; ".join(bits)) if bits else None


def fg_components() -> list[dict]:
    """
    The two parts of CNN's index the house version lacked, now that there is a source for each.

    Put/call: CNN reads the five-day average of the put/call ratio. Here it is Cboe's total,
    scored by where its five-day average stands in its own year, inverted: many puts are fear.

    Volume breadth: CNN reads the McClellan Volume Summation Index of the NYSE. Here it is the
    same sum on the S&P 500 members' up and down volume (the nineteen-day less the
    thirty-nine-day average of the net share, added up), scored by where it stands in its year.
    """
    out = []
    g = digest()
    pc = g.get("pc") or {}
    if pc.get("available") and pc.get("n", 0) >= 60 and pc["rank5"].get("tot") is not None:
        out.append({"name": "Put / call ratio", "score": _r(100 - pc["rank5"]["tot"], 1),
                    "detail": f"Cboe's five-day average {pc['avg5']['tot']:.2f}, the {_ord(pc['rank5']['tot'])} percentile of its year"})
    b = g.get("breadth") or {}
    if b.get("available") and len(b.get("share") or []) >= 300:
        net_ = [((s or 50) - 50) / 50 for s in b["share"]]
        osc = [a - c for a, c in zip(_ema(net_, 19), _ema(net_, 39))]
        summ, run_ = [], 0.0
        for x in osc:
            run_ += x
            summ.append(run_)
        yr = summ[-252:]
        lo, hi = min(yr), max(yr)
        pos = (summ[-1] - lo) / (hi - lo) if hi > lo else 0.5
        out.append({"name": "Volume breadth", "score": _r(pos * 100, 1),
                    "detail": f"the S&P 500 members' McClellan volume summation at {pos * 100:.0f}% of its year's range"})
    return out
