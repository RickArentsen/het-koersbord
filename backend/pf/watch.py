"""
WCH — the watchlist (build 25).

Every company listed in the US or Europe and worth more than $5bn, sorted into
its sector, with the ones worth a look pulled out: a strong business whose
share price has fallen well below its high while the business itself has not.

The scan is a funnel, because the expensive part cannot be done 2,000 times
a day:

    universe      Yahoo's screener, sector by sector and currency by currency:
                  about 2,000 companies, one listing each (a company quoted in
                  Amsterdam, New York and on Xetra is one company)
    prices        five years of daily closes for every one of them, in batches:
                  how far below its high, how unusual that is for this stock,
                  whether it is still falling, and how much of the fall was the
                  market, the sector and the company
    fundamentals  for every stock at least 15% below its 52-week high, plus your
                  pins and holdings and (build 48) the 300 largest US and 200
                  largest European companies: Yahoo's statements (four years, five
                  quarters), the analysts' estimates and how they moved over 90
                  days, and the key statistics
    checks        about twenty rules, each printed with its number: quality,
                  growth, estimates, valuation, income
    presets       Balanced, Growth and Value weigh the groups differently and
                  say which checks must not fail

The rule that matters most: a stock is down for a reason when the analysts
cut next year's earnings while it falls. Those go into their own list, with
the cut, instead of among the opportunities.

Two more since build 27, both from a test on the S&P 500's members as they
were at the time, 2019-2025:

    the mood      how far below its high the typical one of the region's 500
                  largest companies is.
                  In a calm market a 20%+ dip is nearly always the company's
                  own, and those ranked 43 of 100 a year later, with one in
                  five falling another 30%. So in a calm market a dip the
                  market or its sector does not explain needs a score of 85,
                  not 75. In a correction or a sell-off the rules stay.
    momentum      the 30 strongest twelve-month performers (skipping the last
                  month) among each region's 500 largest, the business checked
                  like the dips: the one factor of Nexus's that held up on
                  honest data.

Not advice: a list of what to read first.
"""

from __future__ import annotations

import bisect
import json
import math
import re
import statistics
import threading
import time
from collections import deque
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import store

try:
    from zoneinfo import ZoneInfo
    AMS = ZoneInfo("Europe/Amsterdam")
except Exception:                                   # pragma: no cover
    AMS = timezone.utc

MIN_CAP_USD = 5e9
DEEP_DD = 15.0            # this far below the 52-week high earns the full check
DEFAULT_THR = 20.0        # the page's starting pullback
# The market's mood: the median company's distance below its own 52-week high.
MOOD_SELLOFF, MOOD_CORRECTION = -15.0, -10.0
OWN_SHARE = 0.25          # a fall the market, or the sector, explains less than a quarter of is the stock's own
STRICT_MIN = 85           # what a dip of its own must score in a calm market
LEADERS, LEAD_TOP = 30, 10   # the momentum list per region, and the part of it the track record holds
TEST_N = 500              # mood and momentum read the 500 largest per region: the size class the test covered
LARGE_N = {"US": 300, "EU": 200}   # the largest are read in full wherever they stand: Koers AI's broad market (build 48)
MOM_PRESET = "momentum"   # the track record's name for the quarterly top ten
SCAN_VER = 2              # the shape of a scan; an older one is redone on the next visit
SCAN_AT = (22, 45)        # Amsterdam: after New York's close, before the morning brief

SECTORS = ["Technology", "Communication Services", "Consumer Cyclical", "Consumer Defensive", "Healthcare",
           "Financial Services", "Industrials", "Energy", "Basic Materials", "Real Estate", "Utilities"]

# The primary exchanges only. Frankfurt, Stuttgart, Munich and the rest quote
# copies of everything, and the US over-the-counter market quotes copies of
# the rest of the world.
US_EXCH = ["NMS", "NYQ", "NGM", "NCM", "ASE", "BTS", "PCX"]
EU_EXCH = {"EUR": ["AMS", "GER", "PAR", "MIL", "MCE", "HEL", "BRU", "ISE", "VIE", "LIS"],
           "GBP": ["LSE"], "CHF": ["EBS"], "SEK": ["STO"], "DKK": ["CPH"], "NOK": ["OSL"], "PLN": ["WSE"]}
EXCH_COUNTRY = {"AMS": "NL", "GER": "DE", "PAR": "FR", "MIL": "IT", "MCE": "ES", "HEL": "FI", "BRU": "BE",
                "ISE": "IE", "VIE": "AT", "LIS": "PT", "LSE": "GB", "EBS": "CH", "STO": "SE", "CPH": "DK",
                "OSL": "NO", "WSE": "PL"}
EUROPE_CCY = {"EUR", "GBP", "CHF", "SEK", "DKK", "NOK", "PLN"}
HOME_EXCH = ("AMS",)      # your own market first, when a company lists in several European places

# US dollars per unit. Yahoo quotes most pairs against the dollar one way only.
FX_SYMS = {"EUR": ("EURUSD=X", False), "GBP": ("GBPUSD=X", False), "AUD": ("AUDUSD=X", False),
           "CHF": ("USDCHF=X", True), "SEK": ("USDSEK=X", True), "DKK": ("USDDKK=X", True),
           "NOK": ("USDNOK=X", True), "PLN": ("USDPLN=X", True), "JPY": ("USDJPY=X", True),
           "CNY": ("USDCNY=X", True), "HKD": ("USDHKD=X", True), "TWD": ("USDTWD=X", True),
           "KRW": ("USDKRW=X", True), "INR": ("USDINR=X", True), "BRL": ("USDBRL=X", True),
           "CAD": ("USDCAD=X", True), "MXN": ("USDMXN=X", True), "ILS": ("USDILS=X", True),
           "ZAR": ("USDZAR=X", True), "SGD": ("USDSGD=X", True)}
FX_FALLBACK = {"USD": 1.0, "EUR": 1.12, "GBP": 1.32, "CHF": 1.2, "SEK": 0.1, "DKK": 0.15, "NOK": 0.104,
               "PLN": 0.256}
BENCH = {"US": ("^GSPC", "S&P 500"), "EU": ("^STOXX", "STOXX Europe 600")}

TTL = {"universe": 20 * 3600, "fin": 7 * 86400, "est": 20 * 3600}

# --------------------------------------------------------------------------
# what is checked, and how each preset weighs it
# --------------------------------------------------------------------------

# key: (group, label, how it is judged). The page prints this table as it is.
CHECKS = {
    "q_margin":   ("quality", "Operating margin", "at least the sector's bar passes; half the bar warns"),
    "q_trend":    ("quality", "Margin against its own average", "within 1.5 points of its 3-year average passes; 5 points below fails"),
    "q_fcf":      ("quality", "Free cash flow", "positive now and in every one of the last four years passes; a negative year warns; negative now fails"),
    "q_roic":     ("quality", "Return on invested capital", "12% passes, 7% warns (6% and 4% for property, 7% and 5% for utilities)"),
    "q_roe":      ("quality", "Return on equity", "for banks and insurers: 12% passes, 8% warns"),
    "q_debt":     ("quality", "Net debt to EBITDA", "net cash or up to 2× passes, 3.5× warns (4.5× and 6.5× for property and utilities)"),
    "q_cover":    ("quality", "Interest cover", "operating profit at least 8× the interest passes, 3× warns"),
    "q_dilution": ("quality", "Share count", "up to +3% over three years passes, +10% warns; buybacks pass"),
    "g_rev":      ("growth", "Revenue, last full year", "+5% passes, flat warns, shrinking fails"),
    "g_rev_q":    ("growth", "Revenue, latest quarter", "+3% on a year ago passes, −3% warns"),
    "g_rev3":     ("growth", "Revenue, three years", "+6% a year passes, +2% warns"),
    "g_eps_ny":   ("growth", "Earnings expected next year", "+10% passes, +3% warns"),
    "e_ny90":     ("estimates", "Next year's forecast, 90 days", "cut by no more than 1% passes, 6% warns; cut 15% or more: down for a reason"),
    "e_cy30":     ("estimates", "This year's forecast, 30 days", "cut by no more than 1% passes, 4% warns"),
    "e_breadth":  ("estimates", "Analysts revising, 30 days", "as many raising as cutting passes; more than twice as many cutting fails"),
    "v_pe":       ("value", "Forward P/E against its sector", "15% below the sector's median passes, 15% above fails"),
    "v_compress": ("value", "P/E since the high", "the multiple 20% lower than at the high passes, 8% warns"),
    "v_fcfy":     ("value", "Free-cash-flow yield", "5% passes, 2.5% warns"),
    "v_peg":      ("value", "P/E against expected growth (PEG)", "1.2 or less passes, 2 warns"),
    "v_target":   ("value", "Analysts' average target", "shown, not scored"),
    "i_yield":    ("income", "Dividend yield", "3% passes, 1.5% warns"),
    "i_payout":   ("income", "Payout ratio", "up to 60% of earnings passes, 85% warns"),
    "i_cover":    ("income", "Dividend covered by free cash flow", "1.5× passes, 1× warns"),
}
GROUPS = {"quality": "Quality", "growth": "Growth", "estimates": "Estimates", "value": "Valuation", "income": "Income"}

# An opportunity fails nothing its preset counts, has had its forecasts judged
# and scores at least "min". "Close" is one counted check short (never a
# must-pass one), or would be an opportunity but for too few analysts.
# On the first full scan (7 October 2026) a looser rule — a score of 65 and no
# must-pass failure — let 361 of 955 through under Balanced; this one, 81.
PRESETS = {
    "balanced": {"label": "Balanced", "w": {"quality": .30, "growth": .20, "estimates": .25, "value": .25},
                 "must": ["e_ny90", "q_fcf", "q_debt"], "min": 75, "close": 65,
                 "about": "Growing sales, healthy margins, free cash flow, manageable debt, forecasts holding, a fair price."},
    "growth":   {"label": "Growth", "w": {"growth": .35, "estimates": .30, "quality": .20, "value": .15},
                 "must": ["e_ny90", "g_rev_q"], "min": 75, "close": 65,
                 "about": "Revenue and earnings growth and rising forecasts count most; a higher valuation is tolerated."},
    "value":    {"label": "Value", "w": {"value": .35, "quality": .30, "estimates": .20, "income": .15},
                 "must": ["e_ny90", "q_debt", "q_fcf"], "min": 75, "close": 65,
                 "about": "Cheap against its sector and its own recent multiple, cash yield, dividends, low debt."},
}

# An operating margin that is good for one sector is poor for another.
MARGIN_BAR = {"Technology": 20, "Communication Services": 15, "Healthcare": 15, "Consumer Cyclical": 10,
              "Consumer Defensive": 6, "Industrials": 12, "Energy": 12, "Basic Materials": 12,
              "Real Estate": 25, "Utilities": 15, "Financial Services": 20}
_BANKLIKE = re.compile(r"bank|insurance|capital markets|mortgage|financial conglomerates|shell compan", re.I)


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _pos(x) -> Optional[float]:
    v = _f(x)
    return v if v is not None and v > 0 else None


def _r(x, d: int = 2) -> Optional[float]:
    v = _f(x)
    return round(v, d) if v is not None else None


def _pct(a, b) -> Optional[float]:
    a, b = _f(a), _f(b)
    return (a / b - 1) * 100 if a is not None and b else None


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _yf():
    import yfinance as yf
    return yf


# Yahoo, politely. yfinance shares one cookie and one "crumb" across the whole
# process, and on any failed request it switches cookie strategy and mints a
# new crumb — so four threads asking at once invalidate each other's crumbs,
# every retry doubles the traffic, and within minutes Yahoo answers 429 to the
# whole machine (it did, on 7 October, a third of the way through the first
# scan). Every request here that needs a crumb goes through one lock, at most
# one every 0.4 seconds.
_YF_LOCK = threading.Lock()
_PACE = {"t": 0.0}
MIN_GAP = 0.4


class RateLimited(Exception):
    pass


def _rl(e: Exception) -> bool:
    s = f"{e.__class__.__name__} {e}".lower()
    return "ratelimit" in s or "too many requests" in s or "429" in s


def _paced(fn):
    with _YF_LOCK:
        wait = _PACE["t"] + MIN_GAP - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            return fn()
        except Exception as e:
            if _rl(e):
                raise RateLimited(str(e)[:120])
            raise
        finally:
            _PACE["t"] = time.time()


def _yfget(url: str, params: dict) -> dict:
    """One JSON request through yfinance's session, cookie and crumb; paced."""
    from yfinance.data import YfData
    r = _paced(lambda: YfData().get(url, params=params, timeout=30))
    if r.status_code == 429:
        raise RateLimited("HTTP 429")
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:120]}")
    return r.json()


def _v(x):
    """A quoteSummary value, raw or wrapped."""
    return x.get("raw") if isinstance(x, dict) else x


def _money(v: Optional[float], ccy: str = "") -> str:
    v = _f(v)
    if v is None:
        return "–"
    a = abs(v)
    s = f"{a / 1e12:.2f}tn" if a >= 1e12 else f"{a / 1e9:.1f}bn" if a >= 1e9 else f"{a / 1e6:.0f}m"
    return ("−" if v < 0 else "") + s + (f" {ccy}" if ccy else "")


def _major(ccy: Optional[str]) -> Optional[str]:
    return "GBP" if ccy == "GBp" else ccy


def norm_name(n: str) -> str:
    """A company's name with the legal form and the share class taken out, so its listings match."""
    s = (n or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\b(the|incorporated|inc|corporation|corp|company|co|plc|limited|ltd|llc|lp|n v|nv|s a|sa|s e|se|"
               r"ag|ab|publ|asa|a s|oyj|s p a|spa|bv|kgaa|holding|holdings|class [abc]|cl [abc]|ord|adr|ads)\b",
               " ", s)
    return re.sub(r"\s+", " ", s).strip()


# --------------------------------------------------------------------------
# the universe: Yahoo's screener, one listing per company
# --------------------------------------------------------------------------

def usd_per() -> dict:
    """US dollars per unit of each currency, today; a rough table when Yahoo does not answer."""
    def load():
        yf = _yf()
        df = yf.download([v[0] for v in FX_SYMS.values()], period="10d", progress=False, auto_adjust=True,
                         threads=True)
        last = df["Close"].ffill().iloc[-1]
        out = {"USD": 1.0}
        for ccy, (s, inv) in FX_SYMS.items():
            v = _f(last.get(s))
            if v:
                out[ccy] = 1 / v if inv else v
        if "EUR" not in out:
            raise RuntimeError("no exchange rates")
        return {"r": out}
    try:
        r = store.cache_through("wl:fx", 6 * 3600, load).get("r")
    except Exception:
        r = None
    return {**FX_FALLBACK, **(r or {})}


def _screen_all(q, cap: int = 3000) -> list[dict]:
    yf = _yf()
    out, off = [], 0
    while off < cap:
        r = None
        for attempt in range(3):
            try:
                r = _paced(lambda: yf.screen(q, offset=off, size=250, sortField="intradaymarketcap", sortAsc=False))
                break
            except RateLimited:
                raise
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(3 * (attempt + 1))
        qs = (r or {}).get("quotes") or []
        out += qs
        off += len(qs)
        if not qs or off >= ((r or {}).get("total") or 0):
            break
    return out


_US_JUNK = re.compile(r"-(P[A-Z]?|W[ST]?|WT|U|UN|R|RT)$|\.(U|W|WS|R)$")
_EU_JUNK = re.compile(r"-(PREF|PRF|PREF[A-Z])\.|-(TO|BTA|UR)\.", re.I)     # preference shares, subscription rights


def qrow(x: dict, sector: str, region: str, usd: dict) -> Optional[dict]:
    """One screener quote as a universe row, or None when it cannot be one."""
    sym = x.get("symbol")
    mc, px = _f(x.get("marketCap")), _f(x.get("regularMarketPrice"))
    if not sym or not mc or not px or (x.get("quoteType") or "EQUITY") != "EQUITY":
        return None
    ccy = x.get("currency") or "USD"
    rate = usd.get(_major(ccy))
    if rate is None:                       # Toyota in yen on the London board
        return None
    unit = 0.01 if ccy == "GBp" else 1.0   # London quotes in pence; its market caps are in pounds
    exch = x.get("exchange") or ""
    hi = _f(x.get("fiftyTwoWeekHigh"))
    return {"sym": sym, "name": (x.get("longName") or x.get("shortName") or x.get("displayName") or sym).strip(),
            "sector": sector, "exch": exch, "region": region, "country": EXCH_COUNTRY.get(exch, "US"),
            "ccy": ccy, "fin_ccy": x.get("financialCurrency") or _major(ccy),
            "px": px, "mcap_usd": round(mc * rate), "adv_usd": round((_f(x.get("averageDailyVolume3Month")) or 0) * px * unit * rate),
            "hi52": hi, "dd52": _r((px / hi - 1) * 100) if hi else None,
            "fpe": _r(_pos(x.get("forwardPE")), 2), "tpe": _r(_pos(x.get("trailingPE")), 2),
            "eps_fwd": _f(x.get("epsForward")), "pb": _r(_pos(x.get("priceToBook")), 2),
            "dy": _r(_f(x.get("dividendYield")), 2), "rating": x.get("averageAnalystRating"),
            "earn_ts": x.get("earningsTimestamp"), "chg": _r(x.get("regularMarketChangePercent"))}


def _home(r: dict) -> bool:
    """Trades in the currency it reports in: ASML in Amsterdam, not the New York copy."""
    return _major(r.get("ccy")) == _major(r.get("fin_ccy"))


def pick(group: list[dict], held: set) -> dict:
    """The one listing to show for a company quoted in several places."""
    for r in group:
        if r["sym"] in held:
            return r
    pool = [r for r in group if _home(r)] or [r for r in group if r["region"] == "US"] or group
    return sorted(pool, key=lambda r: (r["exch"] not in HOME_EXCH, -(r.get("adv_usd") or 0), r["sym"]))[0]


def dedupe(rows: list[dict], held: set) -> list[dict]:
    keep = []
    for r in rows:
        if (r["region"] == "US" and _US_JUNK.search(r["sym"])) or (r["region"] == "EU" and _EU_JUNK.search(r["sym"])):
            continue                       # preferred shares, warrants, units
        fc = _major(r.get("fin_ccy"))
        if r["region"] == "EU" and fc not in EUROPE_CCY and fc != "USD":
            continue                       # Samsung in Vienna, Toyota in London: not a European company
        keep.append(r)
    groups: dict[str, list[dict]] = {}
    for r in keep:
        groups.setdefault(norm_name(r["name"]) or r["sym"], []).append(r)
    out = []
    for g in groups.values():
        best = pick(g, held)
        out.append({**best, "also": sorted({x["sym"] for x in g} - {best["sym"]})})
    return out


def _load_universe() -> dict:
    from yfinance import EquityQuery as Q
    usd = usd_per()
    held = set(_held())
    jobs = []
    for sec in SECTORS:
        jobs.append((sec, "US", "USD", US_EXCH))
        for ccy, exs in EU_EXCH.items():
            jobs.append((sec, "EU", ccy, exs))

    def run(job):
        sec, region, ccy, exs = job
        thr = MIN_CAP_USD * 0.95 / usd.get(ccy, 1.0)
        ex_q = Q("is-in", ["exchange", *exs]) if len(exs) > 1 else Q("eq", ["exchange", exs[0]])
        q = Q("and", [ex_q, Q("eq", ["sector", sec]), Q("gt", ["intradaymarketcap", thr])])
        try:
            return job, _screen_all(q), None
        except Exception as e:
            return job, [], f"{e.__class__.__name__}: {e}"[:160]

    raw, errs = [], {}
    for job in jobs:                       # one at a time: see _paced
        (sec, region, ccy, _), qs, err = run(job)
        if err:
            errs[f"{sec} / {ccy}"] = err
            if "ratelimit" in err.lower().replace(" ", "") or "429" in err:
                break
        for x in qs:
            r = qrow(x, sec, region, usd)
            if r and r["mcap_usd"] >= MIN_CAP_USD:
                raw.append(r)
    if errs:
        # Half a universe would show empty sectors as if nothing were in them;
        # the last complete one is better, and the next scan tries again.
        raise RuntimeError(f"Yahoo's screener failed for {len(errs)} of {len(jobs)} queries: {next(iter(errs.values()))}")
    rows = dedupe(raw, held)
    return {"rows": rows, "n_raw": len(raw), "errors": errs, "fx": usd, "fetched_at": time.time()}


def universe() -> dict:
    return store.cache_through("wl:universe", TTL["universe"], _load_universe)


# --------------------------------------------------------------------------
# prices: what the chart says
# --------------------------------------------------------------------------

def each_series(syms: list[str], period: str = "5y", interval: str = "1d", chunk: int = 150,
                volumes: Optional[dict] = None):
    """
    (symbol, dates, closes) for every symbol Yahoo has, a batch at a time, so memory stays small.

    With `volumes`, each symbol's share volume on the same dates goes into it as a list: the
    download carries it anyway, and the indices' up and down volume is read from it (build 51).
    """
    yf = _yf()
    import pandas as pd
    for i in range(0, len(syms), chunk):
        part = syms[i:i + chunk]
        df = None
        for attempt in range(3):
            try:
                df = yf.download(part, period=period, interval=interval, auto_adjust=True, progress=False,
                                 threads=True, group_by="column", timeout=60)
                break
            except Exception:
                time.sleep(4 * (attempt + 1))
        if i + chunk < len(syms):
            time.sleep(1.5)                # a breath between batches; the chart API is lenient, not limitless
        if df is None or getattr(df, "empty", True):
            continue
        vo = None
        if isinstance(df.columns, pd.MultiIndex):
            if "Close" not in df.columns.get_level_values(0):
                continue
            cl = df["Close"]
            if volumes is not None and "Volume" in df.columns.get_level_values(0):
                vo = df["Volume"]
        else:
            cl = df[["Close"]].rename(columns={"Close": part[0]})
            if volumes is not None and "Volume" in df.columns:
                vo = df[["Volume"]].rename(columns={"Volume": part[0]})
        for s in part:
            if s not in cl.columns:
                continue
            ser = cl[s].dropna()
            ser = ser[ser > 0]
            if ser.empty:
                continue
            if vo is not None and s in vo.columns:
                volumes[s] = [float(v) if v == v else 0.0 for v in vo[s].reindex(ser.index).values]
            yield s, [str(x)[:10] for x in ser.index], [float(v) for v in ser.values]


def price_metrics(days: list[str], c: list[float], window: int = 252) -> Optional[dict]:
    """
    Everything the chart says about a pullback, from adjusted daily closes:
    the 52-week high and how far below it, the low since, how often the stock
    has been this far down before, whether it is still falling.
    """
    n = len(c)
    if n < 60 or not c[-1] or c[-1] <= 0:
        return None
    last, dl = c[-1], days[-1]
    i0 = bisect.bisect_left(days, (date.fromisoformat(dl) - timedelta(days=365)).isoformat())
    i0 = min(i0, max(0, n - 2))
    j = max(range(i0, n), key=lambda k: c[k])
    hi = c[j]
    k = min(range(j, n), key=lambda t: c[t])
    lo = c[k]
    dd = (last / hi - 1) * 100

    # The drawdown from the running one-year high on every day we have: how
    # deep today's is against this stock's own history.
    dq: deque = deque()
    hist = []
    for t in range(n):
        while dq and c[dq[-1]] <= c[t]:
            dq.pop()
        dq.append(t)
        if dq[0] <= t - window:
            dq.popleft()
        if t >= window // 4:
            hist.append(c[t] / c[dq[0]] - 1)
    cur = hist[-1] if hist else dd / 100
    past = hist[:-1]
    pctile = round(sum(1 for h in past if h > cur) / len(past) * 100) if len(past) >= 60 else None

    rets = [c[t] / c[t - 1] - 1 for t in range(max(1, n - 252), n)]
    vol = statistics.pstdev(rets) * math.sqrt(252) * 100 if len(rets) > 20 else None
    ma50 = sum(c[-50:]) / min(50, n)
    ma200 = sum(c[-200:]) / 200 if n >= 200 else None
    since_low = n - 1 - k
    off_low = (last / lo - 1) * 100
    if dd > -3:
        trend = "at the high"
    elif since_low <= 5:
        trend = "falling"
    elif last > ma50 and off_low >= 8:
        trend = "recovering"
    else:
        trend = "basing"
    drops = sorted(((days[t], (c[t] / c[t - 1] - 1) * 100) for t in range(j + 1, n)), key=lambda x: x[1])[:3]
    spark = [round(v, 4) for v in c[max(i0, 0)::5]]
    if spark and spark[-1] != round(last, 4):
        spark.append(round(last, 4))
    mom, r12 = momentum(days, c)
    return {"last": last, "d": dl, "hi": hi, "hi_d": days[j], "dd52": round(dd, 2), "mom": mom, "r12": r12,
            "lo": lo, "lo_d": days[k], "since_low": since_low, "off_low": round(off_low, 2),
            "days_hi": n - 1 - j, "pctile": pctile, "vol": _r(vol, 1),
            "ma50_gap": _r(_pct(last, ma50), 1), "ma200_gap": _r(_pct(last, ma200), 1) if ma200 else None,
            "trend": trend, "drops": [[d, round(r, 2)] for d, r in drops if r < 0], "spark": spark,
            "hi5": max(c), "hi5_d": days[c.index(max(c))], "dd5": round((last / max(c) - 1) * 100, 2)}


def momentum(days: list[str], c: list[float]) -> tuple[Optional[float], Optional[float]]:
    """
    Twelve-month momentum skipping the last month, and the plain twelve-month
    return, both in %: the close 30 days ago against the close a year ago, in
    calendar days, as in the test. None without a year of history.
    """
    if not days or not c:
        return None, None
    d = date.fromisoformat(days[-1])
    y_ago, m_ago = (d - timedelta(days=365)).isoformat(), (d - timedelta(days=30)).isoformat()
    if days[0] > y_ago:
        return None, None

    def at(x):
        i = bisect.bisect_right(days, x) - 1
        return c[i] if i >= 0 else None
    p12, p1 = at(y_ago), at(m_ago)
    if not p12 or p12 <= 0 or not p1:
        return None, None
    return round((p1 / p12 - 1) * 100, 2), round((c[-1] / p12 - 1) * 100, 2)


def mood_of(dds: list) -> Optional[dict]:
    """
    The market's mood from every company's distance below its 52-week high:
    calm while the median company is less than 10% below, a correction to 15%,
    a sell-off beyond. Too few companies: no reading.
    """
    v = [x for x in dds if x is not None]
    if len(v) < 30:
        return None
    med = statistics.median(v)
    state = "sell-off" if med <= MOOD_SELLOFF else "correction" if med <= MOOD_CORRECTION else "calm"
    return {"median_dd": round(med, 1), "state": state, "n": len(v),
            "share20": round(sum(1 for x in v if x <= -20) / len(v) * 100)}


def fall_tag(f: Optional[dict]) -> Optional[dict]:
    """
    Who the fall belongs to. The market's share is the part of the stock's
    fall (in log terms) the market's own fall over the same days accounts for;
    the sector's share uses the sector index's whole move. A quarter or more
    from the market: "market"; from the sector: "sector"; less: "own".
    """
    if not f or f.get("stock") is None or f["stock"] >= 0:
        return None
    S = math.log1p(f["stock"] / 100)
    m = f.get("market") or 0.0
    ms = max(0.0, min(1.0, math.log1p(m / 100) / S)) if m < 0 else 0.0
    if f.get("sector") is not None:
        sec = (1 + m / 100) * (1 + f["sector"] / 100) - 1
        ss = max(0.0, min(1.0, math.log1p(sec) / S)) if sec < 0 else 0.0
    else:
        ss = ms
    tag = "market" if ms >= OWN_SHARE else "sector" if ss >= OWN_SHARE else "own"
    return {"tag": tag, "market_share": round(ms * 100), "sector_share": round(max(ss, ms) * 100)}


def largest(rows: list[dict], reg: str, k: int = TEST_N) -> list[dict]:
    """The region's k largest companies by market value in dollars."""
    return sorted((r for r in rows if r.get("region") == reg), key=lambda r: -(r.get("mcap_usd") or 0))[:k]


def full_read(by_sym: dict, pm: dict, pinned, held, rows: list[dict]) -> list[str]:
    """
    Who gets the fundamentals and the checks: every stock 15% or more below
    its 52-week high, your pins and holdings, and (since build 48) each
    region's largest companies wherever they stand, so Koers AI has the broad
    market to choose from. A large company near its high moves from the
    page's "near their high" list into the rows; the page's lists of
    opportunities still take only the dips.
    """
    cand = [s for s in by_sym if (pm.get(s) and pm[s]["dd52"] <= -DEEP_DD) or s in pinned or s in held]
    have = set(cand)
    for reg, k in LARGE_N.items():
        for r in largest(rows, reg, k):
            if r["sym"] in by_sym and r["sym"] not in have:
                have.add(r["sym"])
                cand.append(r["sym"])
    return cand


def rank_leaders(rows: list[dict], pm: dict, n: int = LEADERS, pool: int = TEST_N) -> dict:
    """Each region's n strongest of its `pool` largest, by twelve-month momentum skipping the last month."""
    out = {}
    for reg in BENCH:
        ranked = sorted(((pm[r["sym"]]["mom"], r["sym"]) for r in largest(rows, reg, pool)
                         if pm.get(r["sym"]) and pm[r["sym"]].get("mom") is not None),
                        reverse=True)
        out[reg] = [x for _, x in ranked[:n]]
    return out


def strict_for(dd52: Optional[float], tag: Optional[dict], mood_state: Optional[str]) -> bool:
    """The higher bar: a dip deep enough to be checked, which is its own, in a calm market."""
    return bool(dd52 is not None and dd52 <= -DEEP_DD and (tag or {}).get("tag") == "own" and mood_state == "calm")


def lead_weak(row: dict) -> list[str]:
    """What weakens a momentum leader's business: down for a reason, an estimates check failing, too much debt."""
    out = [row["broken"]] if row.get("broken") else []
    st = {k: (v, t) for k, v, t in row.get("ck") or []}
    for k in ("e_ny90", "e_cy30", "e_breadth"):
        if st.get(k, ("",))[0] == "fail" and not row.get("broken"):
            out.append(f"{CHECKS[k][1].lower()}: {st[k][1]}")
    if st.get("q_debt", ("",))[0] == "fail":
        out.append(f"debt: {st['q_debt'][1]}")
    return out


def fall_parts(stock: Optional[float], sector: Optional[float], market: Optional[float]) -> Optional[dict]:
    """
    The fall since the high, in context: what the market did over the same
    days, how the sector did against the market, and how the company did
    against its sector — each a relative return, and the three multiply back
    to the stock's own move. Percentage points that add up read wrongly when
    the moves are large: HubSpot down 55% while US technology rose 43% is not
    "98 points" the company's; it is 69% behind its sector.
    """
    if stock is None:
        return None
    m = market if market is not None else 0.0

    def rel(a, b):
        return ((1 + a / 100) / (1 + b / 100) - 1) * 100
    # No sector index: its part is unknown, not zero, and the company is measured against the market.
    return {"stock": round(stock, 2), "market": round(m, 2),
            "sector": round(rel(sector, m), 2) if sector is not None else None,
            "company": round(rel(stock, sector if sector is not None else m), 2)}


def _wk(d: str) -> str:
    y, w, _ = date.fromisoformat(d).isocalendar()
    return f"{y}-{w:02d}"


def weekly(days: list[str], c: list[float], since: str) -> dict:
    """{ISO week: last close in that week} from `since` on."""
    out = {}
    for d, v in zip(days, c):
        if d >= since:
            out[_wk(d)] = v
    return out


def corr_weekly(a: dict, b: dict) -> Optional[float]:
    """Correlation of weekly returns on the weeks both have."""
    ks = sorted(set(a) & set(b))
    ra, rb = [], []
    for p, q in zip(ks, ks[1:]):
        if a[p] and b[p]:
            ra.append(a[q] / a[p] - 1)
            rb.append(b[q] / b[p] - 1)
    if len(ra) < 40:
        return None
    try:
        return round(statistics.correlation(ra, rb), 2)
    except Exception:
        return None


# --------------------------------------------------------------------------
# fundamentals: Yahoo's statements, estimates and statistics
# --------------------------------------------------------------------------

INFO_KEYS = ("sector", "industry", "country", "longBusinessSummary", "revenueGrowth", "earningsGrowth",
             "grossMargins", "operatingMargins", "profitMargins", "freeCashflow", "operatingCashflow",
             "totalDebt", "totalCash", "ebitda", "returnOnEquity", "returnOnAssets", "debtToEquity",
             "currentRatio", "payoutRatio", "dividendYield", "heldPercentInsiders", "recommendationMean",
             "recommendationKey", "targetMeanPrice", "targetMedianPrice", "targetLowPrice", "targetHighPrice", "numberOfAnalystOpinions",
             "forwardEps", "trailingEps", "enterpriseValue", "enterpriseToEbitda", "beta", "sharesOutstanding",
             "marketCap", "currentPrice", "financialCurrency", "currency", "fullTimeEmployees", "website",
             "longName", "shortName")
FIN_KEYS = ("end", "revenue", "gross_profit", "op_income", "net_income", "eps", "shares", "interest", "dna",
            "ocf", "capex", "fcf", "dividends", "buybacks", "cash", "st_inv", "debt", "equity", "tax", "pretax",
            "revenue_yoy", "op_margin", "gross_margin", "fcf_margin")

# Yahoo's statement lines, each with the names it goes by, first match wins.
# One request for all of them, annual and quarterly, where yfinance makes six.
TS_LINES = {
    "revenue": ["TotalRevenue", "OperatingRevenue"], "cost_of_revenue": ["CostOfRevenue"],
    "gross_profit": ["GrossProfit"], "op_income": ["OperatingIncome", "TotalOperatingIncomeAsReported"],
    "net_income": ["NetIncome", "NetIncomeCommonStockholders"], "eps": ["DilutedEPS", "BasicEPS"],
    "shares": ["DilutedAverageShares", "BasicAverageShares"], "interest": ["InterestExpense"],
    "dna": ["ReconciledDepreciation", "DepreciationAndAmortization"], "pretax": ["PretaxIncome"],
    "tax": ["TaxProvision"], "ocf": ["OperatingCashFlow"], "capex": ["CapitalExpenditure"],
    "dividends": ["CashDividendsPaid"], "buybacks": ["RepurchaseOfCapitalStock"],
    "cash": ["CashAndCashEquivalents"], "st_inv": ["OtherShortTermInvestments"], "debt": ["TotalDebt"],
    "equity": ["StockholdersEquity"],
}
_OUT = {"capex", "dividends", "buybacks"}          # outflows, kept as positive spend
TS_URL = "https://query2.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/"
QS_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/"
QS_MODULES = "financialData,defaultKeyStatistics,assetProfile,summaryDetail,earningsTrend,price"


def parse_timeseries(js: dict) -> tuple[list[dict], list[dict]]:
    """Yahoo's timeseries answer as (annual rows, quarterly rows), oldest first, research.py's names."""
    raw = {"annual": {}, "quarterly": {}}
    for item in ((js.get("timeseries") or {}).get("result")) or []:
        t = ((item.get("meta") or {}).get("type") or [None])[0]
        scale = next((s for s in raw if t and t.startswith(s)), None)
        if not scale:
            continue
        name = t[len(scale):]
        for x in item.get(t) or []:
            if not x:
                continue
            v = _f((x.get("reportedValue") or {}).get("raw"))
            if x.get("asOfDate") and v is not None:
                raw[scale].setdefault(x["asOfDate"], {})[name] = v
    out = []
    for scale in ("annual", "quarterly"):
        rows = []
        for d in sorted(raw[scale]):
            src = raw[scale][d]
            r = {"end": d, "label": d, "derived": False, "debt_st": None}
            for k, names in TS_LINES.items():
                v = next((src[n] for n in names if src.get(n) is not None), None)
                r[k] = abs(v) if v is not None and k in _OUT else v
            rows.append(r)
        out.append([r for r in rows if r.get("revenue") is not None])
    return out[0], out[1]


def _load_fin(sym: str) -> dict:
    from . import research
    types = ",".join([f"annual{n}" for v in TS_LINES.values() for n in v] +
                     [f"quarterly{n}" for v in TS_LINES.values() for n in v])
    js = _yfget(TS_URL + sym, {"symbol": sym, "type": types, "period1": 1483142400,
                               "period2": int(time.time()) + 86400})
    a, q = parse_timeseries(js)
    if not a and not q:
        raise RuntimeError("no statements")
    a, q = research._enrich(a, "a"), research._enrich(q, "q")
    return {"a": [{k: r.get(k) for k in FIN_KEYS} for r in a][-5:],
            "q": [{k: r.get(k) for k in FIN_KEYS} for r in q][-6:], "fetched_at": time.time()}


def parse_summary(js: dict) -> dict:
    """Yahoo's quoteSummary answer: the statistics, the profile and the estimates, by period."""
    res = (((js.get("quoteSummary") or {}).get("result")) or [None])[0]
    if not res:
        raise RuntimeError("no quote summary")
    fd, ks, ap, sd, pr = (res.get(k) or {} for k in ("financialData", "defaultKeyStatistics", "assetProfile",
                                                       "summaryDetail", "price"))
    info = {}
    for src in (pr, sd, ks, fd, ap):
        for k, v in src.items():
            if k in INFO_KEYS and info.get(k) is None:
                info[k] = _v(v)
    # summaryDetail gives the yield as a fraction; the screener and the rest of the page use percent
    if _f(info.get("dividendYield")) is not None:
        info["dividendYield"] = round(info["dividendYield"] * 100, 3)
    if info.get("longBusinessSummary"):
        info["longBusinessSummary"] = info["longBusinessSummary"][:700]
    out = {"info": info, "fetched_at": time.time()}
    for t in ((res.get("earningsTrend") or {}).get("trend")) or []:
        p = t.get("period")
        ee, re_ = t.get("earningsEstimate") or {}, t.get("revenueEstimate") or {}
        if p in ("0q", "+1q"):
            # only what the consensus record keeps (pf/consensus.py), not the trend
            out["q" + p] = {"end": t.get("endDate"), "eps": _f(_v(ee.get("avg"))), "rev": _f(_v(re_.get("avg"))),
                            "n": _f(_v(ee.get("numberOfAnalysts")))}
            continue
        if p not in ("0y", "+1y"):
            continue
        out["end" + p] = t.get("endDate")
        out["ccy" + p] = {"eps": _v(ee.get("earningsCurrency")), "rev": _v(re_.get("revenueCurrency"))}
        tr, rv = t.get("epsTrend") or {}, t.get("epsRevisions") or {}
        out["trend" + p] = {k: _f(_v(tr.get(k))) for k in ("current", "7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo")}
        out["rev" + p] = {k: _f(_v(rv.get(k))) for k in ("upLast7days", "upLast30days", "downLast30days", "downLast7Days")}
        out["ee" + p] = {k: _f(_v(ee.get(k))) for k in ("avg", "low", "high", "yearAgoEps", "numberOfAnalysts", "growth")}
        out["re" + p] = {k: _f(_v(re_.get(k))) for k in ("avg", "numberOfAnalysts", "growth")}
    return out


def _load_est(sym: str) -> dict:
    return parse_summary(_yfget(QS_URL + sym, {"modules": QS_MODULES, "formatted": "false",
                                               "corsDomain": "finance.yahoo.com", "symbol": sym}))


WL_VER = 1      # the shape of the cached statements and estimates; bump when it changes


def _fin_ttl(sym: str) -> float:
    """Five to nine days, fixed per stock, so a thousand statements do not all fall due on the same evening."""
    import zlib
    return TTL["fin"] * (0.75 + (zlib.crc32(sym.encode()) % 50) / 100)


def cached(key: str, ttl: float, loader, fetch: bool = True) -> tuple[Optional[dict], Optional[str]]:
    """
    (value, error). The cached value when it is young enough; otherwise a fresh
    one, or — when the fetch fails or fetching is off — whatever is cached,
    however old. A rate limit is raised, so the scan can stop asking.
    """
    hit = store.cache_get(key)
    age = (hit or {}).get("_age_s")
    fresh = bool(hit) and hit.get("ver") == WL_VER and age is not None and age <= ttl
    if fresh or not fetch:
        return (hit if hit and hit.get("ver") == WL_VER else None), None
    try:
        v = {**loader(), "ver": WL_VER}
        store.cache_put(key, v)
        return v, None
    except RateLimited:
        raise
    except Exception as e:
        return (hit if hit and hit.get("ver") == WL_VER else None), f"{e.__class__.__name__}: {e}"[:160]


def fin(sym: str) -> dict:
    v, err = cached(f"wl:fin:{sym}", _fin_ttl(sym), lambda: _load_fin(sym))
    if v is None:
        raise RuntimeError(err or "no statements")
    return v


def est(sym: str) -> dict:
    v, err = cached(f"wl:est:{sym}", TTL["est"], lambda: _load_est(sym))
    if v is None:
        raise RuntimeError(err or "no estimates")
    return v


def kind_of(sector: Optional[str], industry: Optional[str]) -> str:
    if sector == "Financial Services" and (not industry or _BANKLIKE.search(industry)):
        return "fin"
    if sector == "Real Estate":
        return "reit"
    if sector == "Utilities":
        return "util"
    return "std"


def _ttm4(q: list[dict]) -> Optional[dict]:
    """The last four quarters added up, when they span a year."""
    if len(q) < 4:
        return None
    w = q[-4:]
    try:
        span = (date.fromisoformat(w[-1]["end"]) - date.fromisoformat(w[0]["end"])).days
    except Exception:
        return None
    if not 250 <= span <= 300:
        return None
    out = {"end": w[-1]["end"]}
    for k in ("revenue", "gross_profit", "op_income", "net_income", "interest", "dna", "ocf", "capex", "fcf",
              "dividends"):
        vals = [x.get(k) for x in w]
        out[k] = sum(vals) if all(v is not None for v in vals) else None
    for k in ("cash", "st_inv", "debt", "equity", "shares"):
        out[k] = w[-1].get(k)
    return out


def metrics(row: dict, fin_d: dict, est_d: dict, usd: dict) -> dict:
    """The numbers the checks read, from statements, estimates and the quote."""
    a, q = fin_d.get("a") or [], fin_d.get("q") or []
    info = est_d.get("info") or {}
    sector = row.get("sector") or info.get("sector")
    m: dict = {"kind": kind_of(sector, info.get("industry")), "industry": info.get("industry"),
               "country": info.get("country")}
    ttm = _ttm4(q)
    rev = (ttm or {}).get("revenue") or (a[-1].get("revenue") if a else None)
    m["rev_ttm"] = rev

    def margin(key):
        if ttm and ttm.get(key) is not None and ttm.get("revenue"):
            return ttm[key] / ttm["revenue"] * 100
        if a and a[-1].get(key) is not None and a[-1].get("revenue"):
            return a[-1][key] / a[-1]["revenue"] * 100
        return None
    m["om"] = margin("op_income")
    if m["om"] is None and _f(info.get("operatingMargins")) is not None:
        m["om"] = info["operatingMargins"] * 100
    oms = [x["op_margin"] for x in a[-3:] if x.get("op_margin") is not None]
    m["om_avg3"], m["om_n"] = (sum(oms) / len(oms), len(oms)) if oms else (None, 0)
    m["gm"] = margin("gross_profit")

    m["rev_g_fy"] = a[-1].get("revenue_yoy") if a else None
    m["rev_g_q"] = q[-1].get("revenue_yoy") if q else None
    if m["rev_g_q"] is None and _f(info.get("revenueGrowth")) is not None:
        m["rev_g_q"] = info["revenueGrowth"] * 100
    if len(a) >= 4 and a[-1].get("revenue") and a[-4].get("revenue") and a[-4]["revenue"] > 0 and a[-1]["revenue"] > 0:
        m["rev_cagr3"] = ((a[-1]["revenue"] / a[-4]["revenue"]) ** (1 / 3) - 1) * 100
    else:
        m["rev_cagr3"] = None

    fcf_t = (ttm or {}).get("fcf")
    if fcf_t is None and a:
        fcf_t = a[-1].get("fcf")
    if fcf_t is None:
        fcf_t = _f(info.get("freeCashflow"))
    m["fcf_ttm"] = fcf_t
    yrs = [x.get("fcf") for x in a[-4:] if x.get("fcf") is not None]
    m["fcf_n"], m["fcf_pos"] = len(yrs), sum(1 for v in yrs if v > 0)
    m["fcf_margin"] = fcf_t / rev * 100 if fcf_t is not None and rev else None

    from . import research
    if len(a) >= 2:
        nop, ic, ic0 = research._nopat(a[-1]), research._invested(a[-1]), research._invested(a[-2])
        avg_ic = (ic + ic0) / 2 if ic is not None and ic0 is not None else ic
        m["roic"] = nop / avg_ic * 100 if nop is not None and avg_ic and avg_ic > 0 else None
    else:
        m["roic"] = None
    m["roe"] = _f(info.get("returnOnEquity")) * 100 if _f(info.get("returnOnEquity")) is not None else None

    bal = q[-1] if q and q[-1].get("debt") is not None else (a[-1] if a else {})
    if bal.get("debt") is not None or bal.get("cash") is not None:
        m["net_debt"] = (bal.get("debt") or 0) - (bal.get("cash") or 0) - (bal.get("st_inv") or 0)
    elif info.get("totalDebt") is not None:
        m["net_debt"] = (_f(info.get("totalDebt")) or 0) - (_f(info.get("totalCash")) or 0)
    else:
        m["net_debt"] = None
    ebitda = None
    src = ttm or (a[-1] if a else None)
    if src and src.get("op_income") is not None and src.get("dna") is not None:
        ebitda = src["op_income"] + src["dna"]
    if ebitda is None:
        ebitda = _f(info.get("ebitda"))
    m["ebitda"] = ebitda
    m["nd_ebitda"] = m["net_debt"] / ebitda if m["net_debt"] is not None and ebitda and ebitda > 0 else None
    intr = abs((src or {}).get("interest") or 0)
    m["int_cov"] = src["op_income"] / intr if src and src.get("op_income") is not None and intr > 0 else None

    sh = [(x["end"], x["shares"]) for x in a if x.get("shares")]
    if len(sh) >= 2:
        m["shares_chg"] = (sh[-1][1] / sh[max(0, len(sh) - 4)][1] - 1) * 100
        m["shares_yrs"] = min(3, len(sh) - 1)
    else:
        m["shares_chg"], m["shares_yrs"] = None, None

    t1, t0 = est_d.get("trend+1y") or {}, est_d.get("trend0y") or {}
    m["eps_ny"], m["eps_ny_90"] = t1.get("current"), t1.get("90daysAgo")
    m["est_ny_90"] = _pct(t1.get("current"), t1.get("90daysAgo")) if (t1.get("90daysAgo") or 0) > 0 else None
    m["est_ny_30"] = _pct(t1.get("current"), t1.get("30daysAgo")) if (t1.get("30daysAgo") or 0) > 0 else None
    m["est_cy_30"] = _pct(t0.get("current"), t0.get("30daysAgo")) if (t0.get("30daysAgo") or 0) > 0 else None
    rv = est_d.get("rev+1y") or est_d.get("rev0y") or {}
    m["up30"], m["dn30"] = rv.get("upLast30days"), rv.get("downLast30days")
    e1 = est_d.get("ee+1y") or {}
    m["eps_g_ny"] = e1.get("growth") * 100 if e1.get("growth") is not None else None
    r1 = est_d.get("re+1y") or {}
    m["rev_g_ny"] = r1.get("growth") * 100 if r1.get("growth") is not None else None
    m["n_an"] = int(_f(info.get("numberOfAnalystOpinions")) or e1.get("numberOfAnalysts") or 0)
    m["target"] = _f(info.get("targetMeanPrice"))
    px = row.get("px")
    m["upside"] = _pct(m["target"], px) if m["target"] and px else None
    m["rec"] = info.get("recommendationKey")

    m["fpe"] = row.get("fpe")
    if m["eps_ny"] and m["eps_ny"] > 0 and px:
        m["fpe_ny"] = px / m["eps_ny"]
    else:
        m["fpe_ny"] = None
    hi = (row.get("pr") or {}).get("hi_px") or row.get("hi52")
    if hi and m["eps_ny_90"] and m["eps_ny_90"] > 0 and m["fpe_ny"]:
        m["fpe_hi"] = hi / m["eps_ny_90"]
    else:
        m["fpe_hi"] = None
    m["peg"] = (m["fpe"] or m["fpe_ny"]) / m["eps_g_ny"] if (m["fpe"] or m["fpe_ny"]) and m["eps_g_ny"] and m["eps_g_ny"] > 0 else None
    fc = _major(info.get("financialCurrency") or row.get("fin_ccy"))
    rate = usd.get(fc) if fc else None
    m["fcf_yield"] = fcf_t * rate / row["mcap_usd"] * 100 if fcf_t is not None and rate and row.get("mcap_usd") else None
    m["dy"] = row.get("dy") if row.get("dy") is not None else _f(info.get("dividendYield"))
    m["payout"] = _f(info.get("payoutRatio")) * 100 if _f(info.get("payoutRatio")) is not None else None
    lastfy = a[-1] if a else {}
    m["div_cover"] = lastfy["fcf"] / lastfy["dividends"] if lastfy.get("fcf") is not None and (lastfy.get("dividends") or 0) > 0 else None
    m["fin_ccy"] = fc
    m["summary"] = info.get("longBusinessSummary")
    m["beta"] = _r(info.get("beta"), 2)
    m["insiders"] = _r((_f(info.get("heldPercentInsiders")) or 0) * 100, 1) if info.get("heldPercentInsiders") is not None else None
    return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in m.items()}


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

def _grade(v: Optional[float], good: float, ok: float, higher: bool = True) -> str:
    if v is None:
        return "na"
    if higher:
        return "pass" if v >= good else "warn" if v >= ok else "fail"
    return "pass" if v <= good else "warn" if v <= ok else "fail"


def checks(m: dict, row: dict, ctx: dict) -> list[list]:
    """[key, pass|warn|fail|na|info, what was found] for every check that applies."""
    out: list[list] = []
    add = lambda k, st, txt: out.append([k, st, txt])
    kind, sec = m.get("kind", "std"), row.get("sector") or ""
    ccy = m.get("fin_ccy") or ""

    # quality -----------------------------------------------------------
    if kind != "fin":
        bar = MARGIN_BAR.get(sec, 12)
        om = m.get("om")
        add("q_margin", _grade(om, bar, bar / 2),
            f"{om:.1f}% (the bar for {sec.lower()}: {bar}%)" if om is not None else "no figures")
        if om is not None and m.get("om_avg3") is not None and m.get("om_n", 0) >= 2:
            d = om - m["om_avg3"]
            add("q_trend", _grade(d, -1.5, -5), f"{om:.1f}% now against {m['om_avg3']:.1f}% on average over {m['om_n']} years")
        else:
            add("q_trend", "na", "not enough years")
        ft, n, pos = m.get("fcf_ttm"), m.get("fcf_n") or 0, m.get("fcf_pos") or 0
        if ft is None:
            add("q_fcf", "na", "no cash-flow figures")
        elif ft <= 0:
            add("q_fcf", "fail", f"negative over the last twelve months ({_money(ft, ccy)})")
        elif n and pos < n:
            add("q_fcf", "warn", f"{_money(ft, ccy)} over twelve months, but negative in {n - pos} of the last {n} years")
        else:
            add("q_fcf", "pass", f"{_money(ft, ccy)} over twelve months"
                + (f" ({m['fcf_margin']:.0f}% of revenue)" if m.get("fcf_margin") is not None else "")
                + (f"; positive in each of the last {n} years" if n else ""))
        good, ok = {"reit": (6, 4), "util": (7, 5)}.get(kind, (12, 7))
        ro = m.get("roic")
        add("q_roic", _grade(ro, good, ok), f"{ro:.1f}% in the last full year" if ro is not None else "no figures")
        nd, eb = m.get("net_debt"), m.get("ebitda")
        good, ok = (4.5, 6.5) if kind in ("reit", "util") else (2.0, 3.5)
        if nd is None:
            add("q_debt", "na", "no balance-sheet figures")
        elif nd <= 0:
            add("q_debt", "pass", f"net cash of {_money(-nd, ccy)}")
        elif not eb or eb <= 0:
            add("q_debt", "fail", f"net debt of {_money(nd, ccy)} and no operating profit to carry it")
        else:
            x = nd / eb
            add("q_debt", _grade(x, good, ok, higher=False), f"{x:.1f}× EBITDA (net debt {_money(nd, ccy)})")
        ic = m.get("int_cov")
        if ic is None:
            add("q_cover", "pass" if nd is not None and nd <= 0 else "na",
                "net cash" if nd is not None and nd <= 0 else "no interest figure")
        else:
            add("q_cover", _grade(ic, 8, 3), f"operating profit {ic:.1f}× the interest")
    else:
        roe = m.get("roe")
        add("q_roe", _grade(roe, 12, 8), f"{roe:.1f}%" if roe is not None else "no figure")
    sc, yrs = m.get("shares_chg"), m.get("shares_yrs")
    add("q_dilution", _grade(sc, 3, 10, higher=False),
        f"{sc:+.1f}% over {yrs} year{'s' if yrs != 1 else ''}" + (" (buybacks)" if sc is not None and sc < -1 else "")
        if sc is not None else "no figures")

    # growth ------------------------------------------------------------
    v = m.get("rev_g_fy")
    add("g_rev", _grade(v, 5, 0), f"{v:+.1f}% in the last full year" if v is not None else "no figures")
    v = m.get("rev_g_q")
    add("g_rev_q", _grade(v, 3, -3), f"{v:+.1f}% on the same quarter a year ago" if v is not None else "no figures")
    v = m.get("rev_cagr3")
    add("g_rev3", _grade(v, 6, 2), f"{v:+.1f}% a year over three years" if v is not None else "not enough years")
    v = m.get("eps_g_ny")
    add("g_eps_ny", _grade(v, 10, 3), f"{v:+.1f}% expected next fiscal year" if v is not None else "no estimate")

    # estimates: the test of whether the business is still what it was ----
    n_an = m.get("n_an") or 0
    if n_an < 3:
        for k in ("e_ny90", "e_cy30", "e_breadth"):
            add(k, "na", "fewer than three analysts follow it")
    else:
        v = m.get("est_ny_90")
        add("e_ny90", _grade(v, -1, -6),
            f"{v:+.1f}% ({m['eps_ny_90']:.2f} → {m['eps_ny']:.2f} a share)" if v is not None and m.get("eps_ny") is not None
            else "no estimate history")
        v = m.get("est_cy_30")
        add("e_cy30", _grade(v, -1, -4), f"{v:+.1f}%" if v is not None else "no estimate history")
        up, dn = m.get("up30"), m.get("dn30")
        if up is None and dn is None:
            add("e_breadth", "na", "no revision counts")
        else:
            up, dn = int(up or 0), int(dn or 0)
            st = "pass" if dn <= up else ("fail" if dn > 2 * up and dn >= 3 else "warn")
            add("e_breadth", st, f"{up} raised, {dn} cut")

    # valuation -----------------------------------------------------------
    fpe, med = m.get("fpe") or m.get("fpe_ny"), ctx.get("pe_med")
    eps_fwd = row.get("eps_fwd")
    if fpe is None and eps_fwd is not None and eps_fwd <= 0:
        add("v_pe", "fail", "losses expected over the next year")
    elif fpe is None or not med:
        add("v_pe", "na", "no forward P/E")
    else:
        add("v_pe", _grade(fpe / med, 0.85, 1.15, higher=False),
            f"{fpe:.1f}× against {med:.1f}× for {ctx.get('pe_label') or sec}")
    fh, fn_ = m.get("fpe_hi"), m.get("fpe_ny")
    if fh and fn_:
        comp = (1 - fn_ / fh) * 100
        add("v_compress", _grade(comp, 20, 8), f"{fn_:.1f}× next year's earnings now, about {fh:.1f}× at the high")
    else:
        add("v_compress", "na", "no estimate history")
    if kind != "fin":
        fy = m.get("fcf_yield")
        add("v_fcfy", _grade(fy, 5, 2.5), f"{fy:.1f}%" if fy is not None else "no figures")
    g = m.get("eps_g_ny")
    if m.get("peg") is not None:
        add("v_peg", _grade(m["peg"], 1.2, 2.0, higher=False), f"{m['peg']:.2f} ({fpe or fn_:.1f}× for {g:+.0f}% growth)")
    elif g is not None and g <= 0:
        add("v_peg", "fail", "no earnings growth expected")
    else:
        add("v_peg", "na", "no growth estimate")
    if m.get("upside") is not None:
        add("v_target", "info", f"{m['upside']:+.0f}% to {m['target']:.2f} ({n_an} analysts)")

    # income --------------------------------------------------------------
    dy = m.get("dy")
    if not dy:
        add("i_yield", "fail", "no dividend")
        add("i_payout", "na", "no dividend")
        add("i_cover", "na", "no dividend")
    else:
        add("i_yield", _grade(dy, 3, 1.5), f"{dy:.2f}%")
        po = m.get("payout")
        add("i_payout", _grade(po, 60, 85, higher=False), f"{po:.0f}% of earnings" if po is not None else "no figure")
        dc = m.get("div_cover")
        if kind == "fin":
            add("i_cover", "na", "not used for banks and insurers")
        else:
            add("i_cover", _grade(dc, 1.5, 1.0), f"{dc:.1f}× in the last full year" if dc is not None else "no figures")
    return out


PTS = {"pass": 1.0, "warn": 0.5, "fail": 0.0}


def group_scores(ck: list[list]) -> dict:
    acc: dict = {}
    for k, st, _ in ck:
        if st in PTS:
            acc.setdefault(CHECKS[k][0], []).append(PTS[st])
    return {g: round(sum(v) / len(v) * 100) for g, v in acc.items()}


def preset_score(gs: dict, preset: str) -> Optional[float]:
    w = PRESETS[preset]["w"]
    have = {g: x for g, x in w.items() if gs.get(g) is not None}
    if sum(have.values()) < 0.6:
        return None
    return round(sum(gs[g] * x for g, x in have.items()) / sum(have.values()), 1)


def broken_reason(m: dict) -> Optional[str]:
    """Why a fall is the business's, not the price's — or None."""
    e, n = m.get("est_ny_90"), m.get("n_an") or 0
    if n >= 3 and e is not None and e <= -15:
        return f"analysts cut next year's earnings forecast by {abs(e):.0f}% in 90 days"
    rq = m.get("rev_g_q")
    if n >= 3 and e is not None and e <= -8 and rq is not None and rq < 0:
        return f"forecasts cut {abs(e):.0f}% in 90 days while revenue shrinks ({rq:+.0f}% on a year ago)"
    if m.get("eps_ny") is not None and m["eps_ny"] <= 0 and m.get("fcf_ttm") is not None and m["fcf_ttm"] <= 0:
        return "losing money, with no profit expected next year"
    return None


def judge(ck: list[list], gs: dict, preset: str, broken: Optional[str], strict: bool = False) -> dict:
    """
    {score, ok, close, fails, must, bar}: fails are the counted checks that
    failed, must the must-pass ones among them. ok needs no fails, the
    forecasts judged and the score; close allows one fail that is not a
    must-pass, or forecasts nobody could judge. strict (a dip of its own in a
    calm market) raises the score an opportunity needs to STRICT_MIN.
    """
    P = PRESETS[preset]
    bar = max(P["min"], STRICT_MIN) if strict else P["min"]
    sc = preset_score(gs, preset)
    st = {k: s for k, s, _ in ck}
    fails = [k for k, s, _ in ck if s == "fail" and P["w"].get(CHECKS[k][0])]
    must = [k for k in P["must"] if st.get(k) == "fail"]
    judged = st.get("e_ny90") in ("pass", "warn")
    ok = broken is None and sc is not None and sc >= bar and not fails and judged
    close = (not ok and broken is None and sc is not None and sc >= P["close"] and not must and len(fails) <= 1)
    return {"score": sc, "ok": ok, "close": close, "fails": fails, "must": must, "bar": bar}


def pullback_score(dd52: Optional[float], pctile: Optional[float]) -> int:
    """How much on sale, 0 to 100: the depth (15% to 50%) and how rare that depth is for this stock."""
    if dd52 is None:
        return 0
    depth = min(1.0, max(0.0, (-dd52 - DEEP_DD) / 35))
    return round(100 * (0.6 * depth + 0.4 * ((pctile or 0) / 100)))


def status(row: dict, preset: str, thr: float) -> str:
    """opp | close | reason | weak | near | nodata — what the page shows, for one preset and threshold."""
    dd = (row.get("pr") or {}).get("dd52", row.get("dd52"))
    if dd is None:
        return "nodata"
    if dd > -thr:
        return "near"
    if not row.get("ck"):
        return "nodata"
    if row.get("broken"):
        return "reason"
    j = (row.get("ps") or {}).get(preset) or {}
    if j.get("ok"):
        return "opp"
    if j.get("close"):
        return "close"
    return "weak"


# --------------------------------------------------------------------------
# you: holdings, pins, the book
# --------------------------------------------------------------------------

def _held() -> dict:
    try:
        from . import earnings
        return earnings.held()
    except Exception:
        return {}


def _kai_held() -> set:
    """
    Koers AI's own holdings (build 54), read in full every scan like yours. They
    are not "held" in the page's sense (that is your book), so they only join the
    full read. Without this a stock it bought in a dip lost its figures the night
    the dip recovered: ResMed, bought 30 October 15% below its high, sat outside
    the 300 largest and was read only as a price and a P/E from 4 November on.
    """
    try:
        from . import koersai
        if not koersai.started():
            return set()
        return set(koersai.holdings()["pos"])
    except Exception:
        return set()


def book_sectors() -> dict:
    """{sector: % of the book}, turbos counted under their underlying's sector."""
    try:
        from . import service
        d = service.dashboard("SP500", light=True)
        return {g["label"]: g["pct"] for g in d.get("by_sector") or []}
    except Exception:
        return {}


def pins() -> list[dict]:
    store.init_db()
    with store.conn() as c:
        return [dict(r) for r in c.execute("SELECT sym, added, note, buy FROM wl_pin ORDER BY added")]


def pinned_syms() -> list[str]:
    try:
        return [p["sym"] for p in pins()]
    except Exception:
        return []


def pin(sym: str, note: Optional[str] = None, buy: Optional[float] = None) -> dict:
    sym = sym.strip().upper()
    if not re.match(r"^\^?[A-Z0-9][A-Z0-9.\-=]{0,14}$", sym):
        raise ValueError("not a symbol")
    store.init_db()
    with store.conn() as c:
        old = c.execute("SELECT added FROM wl_pin WHERE sym=?", (sym,)).fetchone()
        c.execute("INSERT OR REPLACE INTO wl_pin (sym, added, note, buy) VALUES (?,?,?,?)",
                  (sym, old["added"] if old else datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   (note or "").strip()[:500] or None, _pos(buy)))
    return {"sym": sym, "pins": pins()}


def unpin(sym: str) -> bool:
    store.init_db()
    with store.conn() as c:
        return c.execute("DELETE FROM wl_pin WHERE sym=?", (sym.strip().upper(),)).rowcount > 0


def prefs() -> dict:
    try:
        p = json.loads(store.get_setting("wl_prefs") or "{}")
    except Exception:
        p = {}
    pr = p.get("preset") if p.get("preset") in PRESETS else "balanced"
    thr = _f(p.get("thr"))
    thr = min(60.0, max(DEEP_DD, thr)) if thr is not None else DEFAULT_THR
    return {"preset": pr, "thr": thr}


def set_prefs(body: dict) -> dict:
    p = {**prefs(), **{k: v for k, v in (body or {}).items() if k in ("preset", "thr")}}
    store.set_setting("wl_prefs", json.dumps(p))
    return prefs()


# --------------------------------------------------------------------------
# the scan
# --------------------------------------------------------------------------

JOB: dict = {"state": "idle", "phase": "", "i": 0, "n": 0, "started": None, "finished": None, "error": None,
             "warn": None}
_LOCK = threading.Lock()


def _job(**kw):
    JOB.update(kw)


COOL_S = 300          # when Yahoo says 429: five minutes' silence, at most twice a scan


def deep_all(cand: list[str], progress=None, sleep=time.sleep) -> tuple[dict, dict, int]:
    """
    Statements and estimates for every candidate, one at a time (see _paced),
    each from the cache while it is young enough. On a 429 the scan goes quiet
    for five minutes and carries on; after the second, it stops asking and
    uses whatever is cached, however old, so the list is complete if dated.
    Returns ({sym: (statements, estimates)}, {sym: error}, cool-offs taken).
    """
    deep, errors, fetch, cools = {}, {}, True, 0
    for i, s in enumerate(cand):
        while True:
            try:
                f, ef = cached(f"wl:fin:{s}", _fin_ttl(s), lambda: _load_fin(s), fetch)
                e, ee = cached(f"wl:est:{s}", TTL["est"], lambda: _load_est(s), fetch)
                break
            except RateLimited:
                cools += 1
                if cools > 2:
                    fetch = False
                    continue
                if progress:
                    progress(phase=f"Yahoo asked for a pause — waiting five minutes ({cools} of 2)")
                sleep(COOL_S)
                if progress:
                    progress(phase="reading fundamentals and estimates")
        deep[s] = (f or {}, e or {})
        err = "; ".join(x for x in (f"statements: {ef}" if ef else "", f"estimates: {ee}" if ee else "") if x)
        if not fetch and not (f and e):
            err = err or "not read: Yahoo was rate-limiting this scan"
        if err:
            errors[s] = err
        if progress and i % 5 == 0:
            progress(i=i)
    return deep, errors, cools


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def pe_medians(rows: list[dict]) -> dict:
    """The forward P/E a sector trades on, in the US and in Europe separately when each has enough names."""
    out = {}
    for sec in SECTORS:
        allv = [r["fpe"] for r in rows if r["sector"] == sec and r.get("fpe") and r["fpe"] < 150]
        for reg in ("US", "EU"):
            v = [r["fpe"] for r in rows if r["sector"] == sec and r["region"] == reg and r.get("fpe") and r["fpe"] < 150]
            if len(v) >= 10:
                out[(reg, sec)] = (round(statistics.median(v), 2), f"{sec.lower()} in {'the US' if reg == 'US' else 'Europe'}")
            elif allv:
                out[(reg, sec)] = (round(statistics.median(allv), 2), f"{sec.lower()}, US and Europe")
    return out


def run_scan() -> dict:
    """The whole funnel. Blocking — ten to fifteen minutes the first time, a few after."""
    t0 = time.time()
    _job(state="running", phase="listing companies (Yahoo's screener)", i=0, n=0, started=t0, finished=None,
         error=None, warn=None)
    u = store.cache_through("wl:universe", TTL["universe"], _load_universe)
    usd = u.get("fx") or usd_per()
    rows = [dict(r) for r in u["rows"]]
    held = _held()
    kai = _kai_held()
    pin_rows = pins()
    pinned = {p["sym"] for p in pin_rows}
    by_sym = {r["sym"]: r for r in rows}
    also = {a: r["sym"] for r in rows for a in r.get("also") or []}
    extra = list(dict.fromkeys(s for s in list(pinned) + list(held) + sorted(kai) if s not in by_sym and s not in also))

    # ---- prices ---------------------------------------------------------
    _job(phase="reading five years of prices", n=len(rows) + len(extra))
    bench = [b[0] for b in BENCH.values()]
    syms = [r["sym"] for r in rows] + extra + bench
    since_wk = (date.today() - timedelta(days=740)).isoformat()
    since_1y = (date.today() - timedelta(days=372)).isoformat()
    pm: dict = {}
    wk: dict = {}
    acc: dict = {}                   # (region, sector) -> {day: [sum of returns, count]}
    bench_px: dict = {}
    i = 0
    for s, days, c in each_series(syms):
        i += 1
        if i % 50 == 0:
            _job(i=i)
        if s in bench:
            bench_px[s] = dict(zip(days, c))
            continue
        x = price_metrics(days, c)
        if not x:
            continue
        pm[s] = x
        wk[s] = weekly(days, c, since_wk)      # every stock: momentum leaders sit near their high and need it too
        r = by_sym.get(s)
        if r:
            a = acc.setdefault((r["region"], r["sector"]), {})
            j0 = bisect.bisect_left(days, since_1y)
            for t in range(max(1, j0), len(days)):
                ret = max(-0.25, min(0.25, c[t] / c[t - 1] - 1))
                z = a.setdefault(days[t], [0.0, 0])
                z[0] += ret
                z[1] += 1

    # the sector indexes: every member equally, day by day
    sec_idx = {}
    for key, a in acc.items():
        lvl, out = 1.0, {}
        for d in sorted(a):
            s_, n_ = a[d]
            if n_ >= 3:
                lvl *= 1 + s_ / n_
            out[d] = lvl
        sec_idx[key] = out

    def at(series: dict, keys: list, d: str) -> Optional[float]:
        i_ = bisect.bisect_right(keys, d) - 1
        return series[keys[i_]] if i_ >= 0 else None

    sec_keys = {k: sorted(v) for k, v in sec_idx.items()}
    bench_keys = {k: sorted(v) for k, v in bench_px.items()}

    # ---- who gets the full check ------------------------------------------
    for s in extra:
        by_sym[s] = {"sym": s, "name": s, "sector": None, "exch": "", "region": "EU" if "." in s else "US",
                     "country": None, "ccy": None, "fin_ccy": None, "px": (pm.get(s) or {}).get("last"),
                     "mcap_usd": None, "adv_usd": None, "hi52": None, "dd52": None, "fpe": None, "tpe": None,
                     "eps_fwd": None, "pb": None, "dy": None, "rating": None, "earn_ts": None, "chg": None,
                     "also": [], "outside": True}
    cand = full_read(by_sym, pm, pinned, set(held) | kai, rows)

    # ---- the market's mood, and the momentum leaders ------------------------------
    # Both from the region's 500 largest companies (one listing each), never
    # from pins or holdings outside the universe: the test covered the S&P
    # 500, and the whole universe down to $5bn reads deeper and fills the
    # momentum list with small, loss-making biotechs.
    mood = {reg: mood_of([pm[r["sym"]]["dd52"] for r in largest(rows, reg) if pm.get(r["sym"])])
            for reg in BENCH}
    leaders = rank_leaders(rows, pm)
    lead_rank = {x: (reg, i + 1) for reg, lst in leaders.items() for i, x in enumerate(lst)}
    have = set(cand)
    cand += [x for x in lead_rank if x not in have]      # the leaders get the full check too

    # All-time highs, from monthly closes: read once per stock and kept, then
    # moved up whenever five years of dailies show a higher close; re-read
    # after a month, because adjusted prices drift with every dividend.
    athc = (store.cache_get("wl:ath") or {}).get("a") or {}
    now_ts = time.time()
    need = [s for s in cand if s not in athc or now_ts - athc[s][2] > 30 * 86400][:1200]
    _job(phase="finding all-time highs", i=0, n=len(need))
    for s, days, c in each_series(need, period="max", interval="1mo", chunk=150):
        j = max(range(len(c)), key=lambda k: c[k])
        athc[s] = [c[j], days[j], now_ts]
    for s in cand:
        p = pm.get(s)
        if p and (s not in athc or p["hi5"] > athc[s][0]):
            # a five-year high stands in until the monthly history is read (time 0: read it next scan)
            athc[s] = [p["hi5"], p["hi5_d"], athc[s][2] if s in athc else 0]
    store.cache_put("wl:ath", {"a": athc})
    ath = {s: (v[0], v[1], v[2] == 0) for s, v in athc.items()}

    # ---- fundamentals ------------------------------------------------------
    _job(phase="reading fundamentals and estimates", i=0, n=len(cand))
    deep, errors, cools = deep_all(cand, progress=_job)
    _job(i=len(cand))
    # Keep the day's consensus for every company read: the record the Street
    # panel draws its revision lines from (pf/consensus.py). No extra requests.
    try:
        from . import consensus
        consensus.note_scan(deep)
    except Exception:
        pass

    # ---- assemble -------------------------------------------------------------
    _job(phase="running the checks")
    pe_med = pe_medians(rows)
    bs = book_sectors()
    hw = {s: (h.get("weight") or 0) for s, h in held.items()}
    tot = sum(w for s, w in hw.items() if s in wk) or 0
    book_wk = {}
    if tot:
        keys = set()
        for s in hw:
            if s in wk:
                keys |= set(wk[s])
        ks = sorted(keys)
        lvl, prev = 1.0, None
        for k in ks:
            if prev is not None:
                r_, w_ = 0.0, 0.0
                for s, w in hw.items():
                    ser = wk.get(s)
                    if ser and k in ser and prev in ser and ser[prev]:
                        r_ += w * (ser[k] / ser[prev] - 1)
                        w_ += w
                if w_:
                    lvl *= 1 + r_ / w_
            book_wk[k] = lvl
            prev = k
    pin_map = {p["sym"]: p for p in pin_rows}

    out_rows, near = [], []
    for s, r in by_sym.items():
        p = pm.get(s)
        if s not in cand:
            if p:
                near.append({"sym": s, "name": r["name"], "sector": r["sector"], "region": r["region"],
                             "country": r["country"], "px": round(p["last"], 4), "ccy": r["ccy"],
                             "mcap_usd": r["mcap_usd"], "dd52": p["dd52"], "fpe": r.get("fpe"), "dy": r.get("dy"),
                             "trend": p["trend"], "mom": p.get("mom"), "r12": p.get("r12")})
            continue
        f, e = deep.get(s, ({}, {}))
        info = (e or {}).get("info") or {}
        if r.get("outside"):
            r["name"] = info.get("longName") or info.get("shortName") or s
            r["sector"] = info.get("sector")
            r["ccy"] = info.get("currency")
            r["fin_ccy"] = info.get("financialCurrency")
            mc, rate = _f(info.get("marketCap")), usd.get(_major(info.get("currency") or ""))
            r["mcap_usd"] = round(mc * rate) if mc and rate else None
        row = dict(r)
        if p:
            row["px"] = round(p["last"], 4)
            row["pr"] = {k: p[k] for k in ("d", "dd52", "hi_d", "lo_d", "since_low", "off_low", "days_hi", "pctile",
                                           "vol", "ma50_gap", "ma200_gap", "trend", "drops", "spark", "dd5",
                                           "mom", "r12")}
            row["pr"]["hi_px"] = round(p["hi"], 4)
            row["pr"]["lo_px"] = round(p["lo"], 4)
            if s in ath:
                row["pr"]["ath"], row["pr"]["ath_d"] = round(ath[s][0], 4), ath[s][1]
                row["pr"]["dd_ath"] = round((p["last"] / ath[s][0] - 1) * 100, 2) if ath[s][0] else None
                if ath[s][2]:
                    row["pr"]["ath_5y"] = True      # only five years read so far
            reg = r.get("region") or "US"
            bsym = BENCH.get(reg, BENCH["US"])[0]
            mk = bench_px.get(bsym) or {}
            mkk = bench_keys.get(bsym) or []
            m0, m1 = at(mk, mkk, p["hi_d"]), at(mk, mkk, p["d"])
            sk = (reg, r.get("sector"))
            si, sik = sec_idx.get(sk) or {}, sec_keys.get(sk) or []
            s0, s1 = at(si, sik, p["hi_d"]), at(si, sik, p["d"])
            row["fall"] = fall_parts(p["dd52"], _pct(s1, s0) if s0 else None, _pct(m1, m0) if m0 else None)
            if row["fall"]:
                row["fall"]["bench"] = BENCH.get(reg, BENCH["US"])[1]
            row["tag"] = fall_tag(row["fall"])
            # what the sector did on each of its three worst days
            for dr in row["pr"]["drops"]:
                k_ = bisect.bisect_left(sik, dr[0])
                if 0 < k_ < len(sik) and sik[k_] == dr[0]:
                    dr.append(round((si[sik[k_]] / si[sik[k_ - 1]] - 1) * 100, 2))
        # A dip of its own while the market is calm needs the higher score.
        row["strict"] = strict_for(p["dd52"] if p else None, row.get("tag"),
                                   (mood.get(r.get("region") or "US") or {}).get("state"))
        if f or e:
            m = metrics(row, f or {}, e or {}, usd)
            pmed = pe_med.get((r.get("region") or "US", r.get("sector")))
            ctx = {"pe_med": pmed[0] if pmed else None, "pe_label": pmed[1] if pmed else None}
            ck = checks(m, row, ctx)
            gs = group_scores(ck)
            br = broken_reason(m)
            row.update(m={k: v for k, v in m.items() if k != "summary"}, ck=ck, gs=gs, broken=br,
                       ps={k: judge(ck, gs, k, br, strict=row["strict"]) for k in PRESETS},
                       about=(m.get("summary") or "")[:400], fin_end=(f.get("a") or [{}])[-1].get("end") if f else None)
            row["ctx"] = ctx
        if p:
            row["pb"] = pullback_score(p["dd52"], p.get("pctile"))
        row["fit"] = {"held": s in held, "w": _r(hw.get(s), 2), "sector_w": bs.get(r.get("sector") or ""),
                      "corr": corr_weekly(wk.get(s) or {}, book_wk) if book_wk and s not in held else None}
        if s in pin_map:
            row["pin"] = {k: pin_map[s][k] for k in ("note", "buy", "added")}
        if s in lead_rank:
            row["lead"] = {"region": lead_rank[s][0], "rank": lead_rank[s][1], "top": lead_rank[s][1] <= LEAD_TOP,
                           "weak": lead_weak(row), "checked": bool(row.get("ck"))}
        if errors.get(s):
            row["err"] = errors[s]
        out_rows.append(row)

    sectors = []
    for sec in SECTORS:
        sectors.append({"sector": sec, "n": sum(1 for r in by_sym.values() if r.get("sector") == sec),
                        "cand": sum(1 for r in out_rows if r.get("sector") == sec),
                        "book": bs.get(sec),
                        "pe_us": (pe_med.get(("US", sec)) or (None,))[0], "pe_eu": (pe_med.get(("EU", sec)) or (None,))[0]})
    bench_ret = {}
    for reg, (bsym, lab) in BENCH.items():
        ks = bench_keys.get(bsym) or []
        if ks:
            px = bench_px[bsym]
            bench_ret[reg] = {"label": lab, "last": px[ks[-1]], "d": ks[-1],
                              "dd52": _r(_pct(px[ks[-1]], max(px[k] for k in ks if k >= since_1y))) if ks else None}
    res = {"scanned_at": time.time(), "took_s": round(time.time() - t0), "rows": out_rows, "near": near,
           "sectors": sectors, "n_universe": len(u["rows"]), "n_raw": u.get("n_raw"),
           "universe_at": u.get("fetched_at"), "universe_errors": u.get("errors"),
           "n_cand": len(cand), "n_deep": sum(1 for r in out_rows if r.get("ck")),
           "errors": dict(list(errors.items())[:60]), "n_errors": len(errors), "bench": bench_ret,
           "fx_eur_usd": usd.get("EUR"), "cools": cools, "mood": mood, "leaders": leaders, "ver": SCAN_VER}
    pf = prefs()
    prev = store.cache_get("wl:scan") or {}
    res["new"] = new_since(prev, res, pf["preset"], pf["thr"])
    res["lead_new"], res["lead_out"] = lead_changes(prev.get("leaders"), leaders)
    store.cache_put("wl:scan", res)
    _safe(lambda: update_flags(res, pf["thr"]))
    _safe(lambda: update_leaders(res))
    _job(state="done", phase="done", finished=time.time(),
         warn="; ".join(x for x in (f"{len(errors)} stocks could not be read fresh" if errors else "",
                                    f"Yahoo asked for {cools} pause{'s' if cools != 1 else ''}" if cools else "") if x) or None)
    return res


def new_since(prev: dict, cur: dict, preset: str, thr: float) -> list[str]:
    """Opportunities in this scan that were not in the one before, for the alert and the brief."""
    if not prev or not prev.get("rows"):
        return []
    before = {r["sym"] for r in prev["rows"] if status(r, preset, thr) == "opp"}
    return sorted(r["sym"] for r in cur["rows"] if status(r, preset, thr) == "opp" and r["sym"] not in before)


def lead_changes(before: Optional[dict], now: dict) -> tuple[dict, dict]:
    """Who entered and who left each region's top ten since the last scan; nothing on the first."""
    if not before:
        return {}, {}
    new = {reg: [x for x in lst[:LEAD_TOP] if x not in (before.get(reg) or [])[:LEAD_TOP]] for reg, lst in now.items()}
    out = {reg: [x for x in (before.get(reg) or [])[:LEAD_TOP] if x not in lst[:LEAD_TOP]] for reg, lst in now.items()}
    return {k: v for k, v in new.items() if v}, {k: v for k, v in out.items() if v}


def _scan_thread():
    try:
        run_scan()
    except Exception as e:
        _job(state="error", error=f"{e.__class__.__name__}: {e}"[:300], finished=time.time())


def start_scan() -> dict:
    with _LOCK:
        if JOB["state"] == "running":
            return dict(JOB)
        _job(state="running", phase="starting", i=0, n=0, started=time.time(), finished=None, error=None)
        threading.Thread(target=_scan_thread, name="wl-scan", daemon=True).start()
    return dict(JOB)


def last_slot(now: Optional[datetime] = None) -> datetime:
    """The most recent weekday 22:45 in Amsterdam at or before now."""
    now = now or datetime.now(AMS)
    d = now.date()
    slot = datetime(d.year, d.month, d.day, *SCAN_AT, tzinfo=AMS)
    if now < slot or d.weekday() >= 5:
        d -= timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        slot = datetime(d.year, d.month, d.day, *SCAN_AT, tzinfo=AMS)
    return slot


def next_slot(now: Optional[datetime] = None) -> datetime:
    """The next weekday 22:45 in Amsterdam after the last one."""
    s = last_slot(now) + timedelta(days=1)
    while s.weekday() >= 5:
        s += timedelta(days=1)
    return s


def due(now: Optional[datetime] = None) -> bool:
    s = store.cache_get("wl:scan")
    if not s or not s.get("scanned_at"):
        return True
    return s["scanned_at"] < last_slot(now).timestamp()


_THREAD: dict = {"t": None}


def _loop():
    time.sleep(90)              # let the app and its own warm-ups start first
    try:
        from . import consensus
        consensus.backfill_once()     # build 37: start the consensus record from what the scan holds
    except Exception:
        pass
    while True:
        try:
            if JOB["state"] != "running" and due():
                # A scan that failed is retried after an hour, not every five minutes.
                if not (JOB["state"] == "error" and time.time() - (JOB.get("finished") or 0) < 3600):
                    _scan_thread()
        except Exception:
            pass
        time.sleep(300)


def ensure():
    t = _THREAD["t"]
    if t and t.is_alive():
        return
    store.init_db()
    t = threading.Thread(target=_loop, name="wl-loop", daemon=True)
    _THREAD["t"] = t
    t.start()


# --------------------------------------------------------------------------
# the track record: every stock the screen flagged, and how it did since
# --------------------------------------------------------------------------

def update_flags(res: dict, thr: float, d: Optional[str] = None) -> None:
    """
    A stock that becomes an opportunity gets a row with that day's close and the
    benchmark's; one that stops being one is closed. Nothing is ever rewritten,
    so the record cannot flatter the screen after the fact.
    """
    d = d or datetime.fromtimestamp(res["scanned_at"], AMS).date().isoformat()
    bench = {reg: b["last"] for reg, b in (res.get("bench") or {}).items()}
    store.init_db()
    with store.conn() as c:
        act = {(r["sym"], r["preset"]): dict(r) for r in
               c.execute("SELECT * FROM wl_flag WHERE active=1")}
        for preset in PRESETS:
            now = {r["sym"]: r for r in res["rows"] if status(r, preset, thr) == "opp"}
            for s, r in now.items():
                if (s, preset) in act:
                    c.execute("UPDATE wl_flag SET last_d=?, last_px=? WHERE sym=? AND preset=? AND active=1",
                              (d, r["px"], s, preset))
                else:
                    reg = r.get("region") or "US"
                    c.execute("INSERT OR IGNORE INTO wl_flag (sym, preset, first_d, first_px, bench, first_bench, "
                              "last_d, last_px, active, score, name, sector, ccy) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)",
                              (s, preset, d, r["px"], reg, bench.get(reg), d, r["px"],
                               ((r.get("ps") or {}).get(preset) or {}).get("score"), r.get("name"), r.get("sector"),
                               r.get("ccy")))
            for (s, p), f in act.items():
                if p == preset and s not in now:
                    if f["first_d"] == d:
                        # In and out on the same day (a rescan, a borderline score): not a pick.
                        c.execute("DELETE FROM wl_flag WHERE sym=? AND preset=? AND active=1 AND first_d=?", (s, p, d))
                        continue
                    r = next((x for x in res["rows"] if x["sym"] == s), None) or \
                        next((x for x in res.get("near") or [] if x["sym"] == s), None)
                    c.execute("UPDATE wl_flag SET active=0, end_d=?, end_px=? WHERE sym=? AND preset=? AND active=1",
                              (d, (r or {}).get("px") or f["last_px"], s, p))


def quarter(d: str) -> str:
    return f"{d[:4]}-Q{(int(d[5:7]) - 1) // 3 + 1}"


def update_leaders(res: dict, d: Optional[str] = None) -> None:
    """
    The momentum track record, held the way the test held it: each region's
    top ten written down at the first scan of a quarter, at that close and its
    market's, and closed at the first scan of the next. In between only the
    latest price moves; nothing is rewritten.
    """
    d = d or datetime.fromtimestamp(res["scanned_at"], AMS).date().isoformat()
    q = quarter(d)
    px = {r["sym"]: r.get("px") for r in (res.get("rows") or []) + (res.get("near") or [])}
    info = {r["sym"]: r for r in res.get("rows") or []}
    bench = {reg: b["last"] for reg, b in (res.get("bench") or {}).items()}
    store.init_db()
    with store.conn() as c:
        act = [dict(r) for r in c.execute("SELECT * FROM wl_flag WHERE preset=? AND active=1", (MOM_PRESET,))]
        if act and quarter(max(f["first_d"] for f in act)) == q:
            for f in act:
                c.execute("UPDATE wl_flag SET last_d=?, last_px=? WHERE sym=? AND preset=? AND active=1",
                          (d, px.get(f["sym"]) or f["last_px"], f["sym"], MOM_PRESET))
            return
        for f in act:
            c.execute("UPDATE wl_flag SET active=0, end_d=?, end_px=? WHERE sym=? AND preset=? AND active=1",
                      (d, px.get(f["sym"]) or f["last_px"], f["sym"], MOM_PRESET))
        for reg, lst in (res.get("leaders") or {}).items():
            for i, x in enumerate(lst[:LEAD_TOP]):
                if px.get(x) is None:
                    continue
                r = info.get(x) or {}
                c.execute("INSERT OR IGNORE INTO wl_flag (sym, preset, first_d, first_px, bench, first_bench, last_d, "
                          "last_px, active, score, name, sector, ccy) VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)",
                          (x, MOM_PRESET, d, px[x], reg, bench.get(reg), d, px[x], i + 1, r.get("name"),
                           r.get("sector"), r.get("ccy")))


def track(preset: str = "balanced") -> dict:
    s = store.cache_get("wl:scan") or {}
    px = {r["sym"]: r.get("px") for r in (s.get("rows") or []) + (s.get("near") or [])}
    bench = {reg: b["last"] for reg, b in (s.get("bench") or {}).items()}
    store.init_db()
    with store.conn() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM wl_flag WHERE preset=? ORDER BY first_d DESC, sym", (preset,))]
    out = []
    for f in rows:
        now = px.get(f["sym"]) or f.get("end_px") or f.get("last_px")
        b_now = bench.get(f["bench"])
        ret = _pct(now, f["first_px"])
        bret = _pct(b_now, f["first_bench"]) if f.get("first_bench") else None
        out.append({**f, "now_px": now, "ret": _r(ret), "bench_ret": _r(bret),
                    "excess": _r(ret - bret) if ret is not None and bret is not None else None})
    rs = [x for x in out if x["ret"] is not None]
    ex = [x["excess"] for x in out if x["excess"] is not None]
    return {"preset": preset, "rows": out[:300],
            "summary": {"n": len(out), "active": sum(1 for x in out if x["active"]),
                        "avg_ret": _r(statistics.mean(x["ret"] for x in rs)) if rs else None,
                        "avg_excess": _r(statistics.mean(ex)) if ex else None,
                        "beat": sum(1 for v in ex if v > 0), "of": len(ex),
                        "since": min((x["first_d"] for x in out), default=None)}}


# --------------------------------------------------------------------------
# what other screens read: the page, alerts, the brief
# --------------------------------------------------------------------------

def view() -> dict:
    s = store.cache_get("wl:scan") or {}
    return {"scan": s or None, "job": dict(JOB), "prefs": prefs(), "pins": _safe(pins, []),
            "checks": {k: {"g": v[0], "label": v[1], "how": v[2]} for k, v in CHECKS.items()},
            "groups": GROUPS, "presets": {k: {kk: v[kk] for kk in ("label", "w", "must", "min", "about")}
                                          for k, v in PRESETS.items()},
            "sectors": SECTORS, "deep_dd": DEEP_DD, "min_cap": MIN_CAP_USD, "due": _safe(due, None),
            "next_slot": next_slot().isoformat(),
            "rules": {"sell_off": MOOD_SELLOFF, "correction": MOOD_CORRECTION, "own_share": OWN_SHARE, "test_n": TEST_N,
                      "strict_min": STRICT_MIN, "leaders": LEADERS, "lead_top": LEAD_TOP,
                      "mom_preset": MOM_PRESET, "scan_ver": SCAN_VER}}


def _price_now(sym: str, s: dict) -> Optional[float]:
    for r in (s.get("rows") or []) + (s.get("near") or []):
        if r["sym"] == sym:
            return r.get("px")
    return None


def alerts() -> list[dict]:
    """Home-screen alerts, from the last scan only — never a download on the home screen's time."""
    s = store.cache_get("wl:scan") or {}
    if not s.get("scanned_at"):
        return []
    out = []
    age_h = (time.time() - s["scanned_at"]) / 3600
    new = s.get("new") or []
    if new and age_h < 36:
        pf = prefs()
        out.append({"level": "info", "code": "WCH",
                    "text": f"{len(new)} new on the watchlist: " + ", ".join(new[:5]) + ("…" if len(new) > 5 else ""),
                    "detail": f"strong businesses at least {pf['thr']:.0f}% below their high ({PRESETS[pf['preset']]['label']})"})
    ln = s.get("lead_new") or {}
    if ln and age_h < 36:
        out.append({"level": "info", "code": "WCH",
                    "text": "Momentum top 10, new: " + "; ".join(
                        f"{', '.join(v[:5])} ({'US' if reg == 'US' else 'Europe'})" for reg, v in ln.items()),
                    "detail": "the strongest twelve-month performers above $5bn, skipping the last month"})
    for p in _safe(pins, []) or []:
        if p.get("buy"):
            px = _price_now(p["sym"], s)
            if px is not None and px <= p["buy"]:
                out.append({"level": "warn", "code": "WCH",
                            "text": f"{p['sym']} at {px:.2f} — at or below your buy price {p['buy']:.2f}",
                            "detail": p.get("note") or "a price you set on the watchlist (last scan's close)"})
    return out


def brief_line() -> Optional[str]:
    """One sentence for the morning brief, from the last scan."""
    s = store.cache_get("wl:scan") or {}
    if not s.get("rows"):
        return None
    pf = prefs()
    opp = [r for r in s["rows"] if status(r, pf["preset"], pf["thr"]) == "opp"]
    secs = {r.get("sector") for r in opp}
    bits = [f"Watchlist: {len(opp)} opportunit{'y' if len(opp) == 1 else 'ies'} across {len(secs)} sector{'s' if len(secs) != 1 else ''}"]
    new = s.get("new") or []
    if new:
        bits.append("new since the last scan: " + ", ".join(f"[{x}](/m/WCH#s={x})" for x in new[:5]))
    hits = []
    for p in _safe(pins, []) or []:
        px = _price_now(p["sym"], s)
        if p.get("buy") and px is not None and px <= p["buy"]:
            hits.append(f"[{p['sym']}](/m/WCH#s={p['sym']})")
    ln, lo = s.get("lead_new") or {}, s.get("lead_out") or {}
    if ln or lo:
        reg_name = {"US": "US", "EU": "Europe"}
        part = []
        for reg in BENCH:
            if ln.get(reg) or lo.get(reg):
                txt = f"{reg_name[reg]}: " + (("new " + ", ".join(f"[{x}](/m/WCH#s={x})" for x in ln.get(reg, [])[:4]))
                                              if ln.get(reg) else "")
                if lo.get(reg):
                    txt += (", " if ln.get(reg) else "") + "dropped " + ", ".join(lo.get(reg, [])[:4])
                part.append(txt)
        bits.append("momentum top 10, " + "; ".join(part))
    md = s.get("mood") or {}
    if md:
        bits.append("market mood: " + ", ".join(f"{'US' if reg == 'US' else 'Europe'} {m['state']}"
                                                for reg, m in md.items() if m))
    line = "; ".join(bits)
    if hits:
        line += "; at or below your buy price: " + ", ".join(hits)
    return line + "."


# --------------------------------------------------------------------------
# one stock in depth, on demand
# --------------------------------------------------------------------------

def row_of(sym: str) -> Optional[dict]:
    s = store.cache_get("wl:scan") or {}
    sym = sym.upper()
    for r in s.get("rows") or []:
        if r["sym"] == sym or sym in (r.get("also") or []):
            return r
    for r in s.get("near") or []:
        if r["sym"] == sym:
            return {**r, "near_only": True}
    return None


def why(sym: str) -> dict:
    """
    The days the stock fell since its high, each with the market, sector and
    company parts and the catalyst the security page finds for it: earnings,
    an analyst, a filing, a headline.
    """
    from . import research
    r = row_of(sym) or {}
    hi_d = (r.get("pr") or {}).get("hi_d")
    mv = research.moves(sym)
    days = [x for x in (mv.get("days") or []) + (mv.get("biggest") or [])]
    seen, picked = set(), []
    for x in days:
        if x["d"] in seen or (hi_d and x["d"] < hi_d) or (x.get("r") or 0) >= 0:
            continue
        seen.add(x["d"])
        picked.append(x)
    picked.sort(key=lambda x: (x.get("idio") if x.get("idio") is not None else x.get("r") or 0))
    st = _safe(lambda: research.street(sym)) or {}
    ern = [e for e in st.get("surprises") or [] if not hi_d or e["date"] >= hi_d]
    return {"sym": sym, "hi_d": hi_d, "days": picked[:8], "earnings": ern, "model": mv.get("model"),
            "benchmarks": mv.get("benchmarks"), "archive": mv.get("archive"),
            "actions": [a for a in st.get("actions") or [] if not hi_d or a["date"] >= hi_d][:12]}
