"""
Research on one security — any security, held or not.

The per-ticker page used to know only what the ledger knew: your trades, your
cost, a line of closes. Everything a decision needs beyond that lives here:

    overview     who they are, the quote, the key statistics
    daily        ten years of OHLCV, dividends, splits, and two benchmarks
    street       consensus, how it is moving, surprises, targets, analyst calls
    fundamentals the company's own reported numbers, quarterly and annual
    business     which segment, product line and country the money comes from,
                 backlog, and customer concentration
    valuation    multiples against the company's OWN history, not the market's
    peers        the names it is compared with, on the same yardsticks
    news         categorised, with the deals and the numbers pulled out
    moves        every session split into market, sector and company parts,
                 with the catalyst that most plausibly caused the company part
    ownership    insiders, institutions, short interest
    catalysts    the next earnings date and what the options market expects

Sources, in order of preference: the SEC (what the company filed), Yahoo via
yfinance (quotes, estimates, analyst actions), Finnhub when a key is set
(news with history, peers). Every payload says where it came from and how old
it is, because a number without a source is a number you cannot argue with.
"""

from __future__ import annotations

import bisect
import json
import math
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import store, sec, net
from . import news as newsmod

try:
    from zoneinfo import ZoneInfo
except Exception:                                   # pragma: no cover
    ZoneInfo = None


class NotFound(Exception):
    """The symbol does not exist anywhere we can look."""


TTL = {
    "overview": 300, "daily": 900, "intraday": 60, "street": 3 * 3600,
    "fundamentals": 6 * 3600, "valuation": 3600, "peers": 3 * 3600,
    "peerinfo": 6 * 3600, "news": 900, "moves": 1800, "ownership": 12 * 3600,
    "catalysts": 3600, "daynews": 365 * 86400,
}


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _f(x) -> Optional[float]:
    """A plain float or None. NaN, numpy scalars and strings all normalise."""
    try:
        if x is None:
            return None
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def _r(x, d: int = 4) -> Optional[float]:
    v = _f(x)
    return None if v is None else round(v, d)


def _i(x) -> Optional[int]:
    v = _f(x)
    return None if v is None else int(v)


def _pct(a, b) -> Optional[float]:
    """a over b, minus one, in percent. None when b is missing or not positive."""
    a, b = _f(a), _f(b)
    if a is None or b is None or b <= 0:
        return None
    return round((a / b - 1) * 100, 2)


def _div(a, b) -> Optional[float]:
    a, b = _f(a), _f(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _yf():
    import yfinance as yf
    return yf


def _tk(sym: str):
    return _yf().Ticker(sym)


ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}\d$")


def resolve(key: str) -> str:
    """A quote symbol from whatever was typed: a ticker, or an ISIN you hold."""
    k = (key or "").strip().upper()
    if ISIN.match(k):
        from . import pricing
        inst = store.load_instruments().get(k) or {}
        t = inst.get("ticker") or pricing.SEED_TICKERS.get(k)
        if t:
            return t.upper()
    return k


def _tz(name: Optional[str]):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name or "America/New_York")
    except Exception:
        return ZoneInfo("America/New_York")


def _df_rows(df, index_name: str = "idx") -> list[dict]:
    """A DataFrame as plain dicts with native types — JSON cannot take numpy."""
    if df is None or getattr(df, "empty", True):
        return []
    out = []
    for idx, row in df.iterrows():
        rec = {index_name: str(idx)}
        for k, v in row.items():
            if isinstance(v, (int, float)) or hasattr(v, "dtype"):
                fv = _f(v)
                rec[str(k)] = fv
            elif v is None:
                rec[str(k)] = None
            else:
                s = str(v)
                rec[str(k)] = None if s in ("nan", "NaT", "None") else s
        out.append(rec)
    return out


# --------------------------------------------------------------------------
# currencies: a share priced in one, a company reporting in another
# --------------------------------------------------------------------------
# ASML trades in dollars in New York and reports in euros; TSMC's American
# share is five Taiwanese ones, priced in dollars, reported in Taiwan dollars.
# Every ratio of a price to a reported figure has to cross both gaps first.

MINOR = {"GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01), "ZAc": ("ZAR", 0.01), "ILA": ("ILS", 0.01)}


def major(ccy: Optional[str]) -> tuple[Optional[str], float]:
    """('GBP', 0.01) for London's pence: a price in pence, a market value in pounds."""
    return MINOR.get(ccy or "", (ccy, 1.0))


def _load_usd_hist(ccy: str) -> dict:
    """US dollars per unit of `ccy`, each trading day for ten years."""
    from .watch import FX_SYMS
    if ccy not in FX_SYMS:
        raise NotFound(f"no exchange rate kept for {ccy}")
    sym, inv = FX_SYMS[ccy]
    h = _tk(sym).history(period="10y", interval="1d", auto_adjust=False)
    days, rates = [], []
    for idx, row in (h.iterrows() if h is not None else []):
        c = _f(row.get("Close"))
        if c and c > 0:
            days.append(str(idx.date()))
            rates.append(round(1 / c if inv else c, 8))
    if len(days) < 20:
        raise NotFound(f"no exchange-rate history for {ccy}")
    return {"ccy": ccy, "d": days, "r": rates, "source": f"Yahoo Finance {sym}"}


def usd_hist(ccy: str) -> Optional[dict]:
    if ccy == "USD":
        return None
    return store.cache_through(f"rs:fx:{ccy}", 12 * 3600, lambda: _load_usd_hist(ccy))


class FX:
    """
    Turns an amount in one currency into another at a given day's rate.

    `total(d)` is for whole amounts (revenue, debt: a market value is in
    pounds even when the price is in pence); `per_share(d)` lands in the
    price's own unit. Both are None when a rate is missing, so a ratio is
    left out rather than computed in the wrong currency.
    """

    def __init__(self, frm: Optional[str], to: Optional[str]):
        (self.f, self.fu), (self.t, self.tu) = major(frm), major(to)
        self.same = not self.f or not self.t or self.f == self.t
        self.ok = True
        self._s = {}
        if not self.same:
            for c in (self.f, self.t):
                if c == "USD":
                    continue
                try:
                    self._s[c] = usd_hist(c)
                except Exception:
                    self.ok = False

    def _usd(self, c: str, d: Optional[str]) -> Optional[float]:
        if c == "USD":
            return 1.0
        s = self._s.get(c)
        if not s:
            return None
        i = len(s["d"]) - 1 if d is None else max(0, bisect.bisect_right(s["d"], d) - 1)
        return s["r"][i]

    def total(self, d: Optional[str] = None) -> Optional[float]:
        if self.same:
            return self.fu
        if not self.ok:
            return None
        a, b = self._usd(self.f, d), self._usd(self.t, d)
        return a / b * self.fu if a and b else None

    def per_share(self, d: Optional[str] = None) -> Optional[float]:
        m = self.total(d)
        return m / self.tu if m is not None else None

    def label(self) -> str:
        return "" if self.same else f"{self.f} → {self.t}"


# A quoted share and a filer's share are not always the same thing: 5 for a
# TSMC ADR, 8 for Alibaba's, 1/1500 for a Berkshire B against the A shares
# its accounts count in.
_NICE = sorted({*(float(n) for n in (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100, 1500)),
                *(1 / n for n in (2, 3, 4, 5, 8, 10, 20, 25, 50, 100, 1500))})


def units_ratio(shares: Optional[float], ov: Optional[dict]) -> float:
    """How many of the filer's shares one quoted share stands for, from Yahoo's market value."""
    st, q = (ov or {}).get("stats") or {}, (ov or {}).get("quote") or {}
    mcap, px = _f(st.get("market_cap")), _f(q.get("price"))
    if not shares or not mcap or not px:
        return 1.0
    quoted = mcap / (px * major((ov or {}).get("currency"))[1])
    if quoted <= 0:
        return 1.0
    r = shares / quoted
    for c in _NICE:
        if abs(r / c - 1) < 0.04:
            return c
    return 1.0 if abs(r - 1) < 0.12 else float(f"{r:.3g}")


# --------------------------------------------------------------------------
# overview: the quote, the profile, the key statistics
# --------------------------------------------------------------------------

def _load_overview(sym: str) -> dict:
    info = _tk(sym).info or {}
    g = info.get
    price = _f(g("currentPrice")) or _f(g("regularMarketPrice"))
    if price is None and not g("shortName") and not g("longName"):
        raise NotFound(f"Yahoo has no security under {sym}")
    prev = _f(g("regularMarketPreviousClose")) or _f(g("previousClose"))
    div_rate = _f(g("dividendRate"))
    fcf, mcap = _f(g("freeCashflow")), _f(g("marketCap"))
    officers = []
    for o in (g("companyOfficers") or [])[:6]:
        officers.append({"name": o.get("name"), "title": o.get("title"),
                         "age": o.get("age"), "pay": _f(o.get("totalPay"))})

    def ts_date(k):
        v = _f(g(k))
        return (date(1970, 1, 1) + timedelta(seconds=v)).isoformat() if v else None

    out = {
        "symbol": sym,
        "name": g("longName") or g("shortName") or sym,
        "short": _short_name(g("shortName") or g("longName") or sym),
        "quote_type": g("quoteType"),
        "exchange": g("fullExchangeName") or g("exchange"),
        "tz": g("exchangeTimezoneName") or "America/New_York",
        "currency": g("currency"), "fin_currency": g("financialCurrency"),
        "sector": g("sector") or g("sectorDisp"), "industry": g("industry") or g("industryDisp"),
        "country": g("country"), "city": g("city"),
        "employees": _i(g("fullTimeEmployees")), "website": g("website"),
        "summary": g("longBusinessSummary"),
        "officers": officers,
        "ipo_year": ((datetime(1970, 1, 1) + timedelta(
            milliseconds=_f(g("firstTradeDateMilliseconds")))).year
            if _f(g("firstTradeDateMilliseconds")) is not None else None),
        "quote": {
            "price": price, "prev_close": prev,
            "change": _r(price - prev, 4) if price and prev else None,
            "change_pct": _pct(price, prev),
            "open": _f(g("regularMarketOpen") or g("open")),
            "day_low": _f(g("regularMarketDayLow") or g("dayLow")),
            "day_high": _f(g("regularMarketDayHigh") or g("dayHigh")),
            "volume": _i(g("regularMarketVolume") or g("volume")),
            "avg_volume": _i(g("averageVolume")), "avg_volume_10d": _i(g("averageVolume10days")),
            "market_state": g("marketState"),
            "post_price": _f(g("postMarketPrice")), "post_change_pct": _r(g("postMarketChangePercent"), 3),
            "pre_price": _f(g("preMarketPrice")), "pre_change_pct": _r(g("preMarketChangePercent"), 3),
            "low_52w": _f(g("fiftyTwoWeekLow")), "high_52w": _f(g("fiftyTwoWeekHigh")),
            "ma50": _f(g("fiftyDayAverage")), "ma200": _f(g("twoHundredDayAverage")),
            "ath": _f(g("allTimeHigh")), "atl": _f(g("allTimeLow")),
            "time": _i(g("regularMarketTime")),
        },
        "stats": {
            "market_cap": mcap, "ev": _f(g("enterpriseValue")),
            "pe_ttm": _f(g("trailingPE")), "pe_fwd": _f(g("forwardPE")),
            "peg": _f(g("trailingPegRatio")) or _f(g("pegRatio")),
            "ps": _f(g("priceToSalesTrailing12Months")), "pb": _f(g("priceToBook")),
            "ev_ebitda": _f(g("enterpriseToEbitda")), "ev_rev": _f(g("enterpriseToRevenue")),
            "eps_ttm": _f(g("trailingEps")), "eps_fwd": _f(g("forwardEps")),
            "eps_cur_year": _f(g("epsCurrentYear")),
            # Computed, not read: Yahoo changed dividendYield from a fraction
            # to a percentage in 2025, and 0.73 reads the same either way.
            "div_rate": div_rate,
            "div_yield": round(div_rate / price * 100, 3) if div_rate and price else None,
            "payout": _r((_f(g("payoutRatio")) or 0) * 100, 1) if g("payoutRatio") is not None else None,
            "fcf_yield": round(fcf / mcap * 100, 2) if fcf and mcap else None,
            "beta": _f(g("beta")),
            "shares_out": _f(g("sharesOutstanding")), "float": _f(g("floatShares")),
            "short_pct_float": _r((_f(g("shortPercentOfFloat")) or 0) * 100, 2)
            if g("shortPercentOfFloat") is not None else None,
            "short_ratio": _f(g("shortRatio")), "shares_short": _f(g("sharesShort")),
            "shares_short_prior": _f(g("sharesShortPriorMonth")),
            "short_date": ts_date("dateShortInterest"),
            "insiders_pct": _r((_f(g("heldPercentInsiders")) or 0) * 100, 2),
            "inst_pct": _r((_f(g("heldPercentInstitutions")) or 0) * 100, 2),
            "gross_margin": _r((_f(g("grossMargins")) or 0) * 100, 2) if g("grossMargins") is not None else None,
            "op_margin": _r((_f(g("operatingMargins")) or 0) * 100, 2) if g("operatingMargins") is not None else None,
            "net_margin": _r((_f(g("profitMargins")) or 0) * 100, 2) if g("profitMargins") is not None else None,
            "roe": _r((_f(g("returnOnEquity")) or 0) * 100, 2) if g("returnOnEquity") is not None else None,
            "roa": _r((_f(g("returnOnAssets")) or 0) * 100, 2) if g("returnOnAssets") is not None else None,
            "rev_growth": _r((_f(g("revenueGrowth")) or 0) * 100, 2) if g("revenueGrowth") is not None else None,
            "earn_growth": _r((_f(g("earningsQuarterlyGrowth")) or 0) * 100, 2)
            if g("earningsQuarterlyGrowth") is not None else None,
            "cash": _f(g("totalCash")), "debt": _f(g("totalDebt")),
            "revenue": _f(g("totalRevenue")), "ebitda": _f(g("ebitda")),
            "fcf": fcf, "ocf": _f(g("operatingCashflow")),
            "current_ratio": _f(g("currentRatio")), "debt_equity": _f(g("debtToEquity")),
            "chg_52w": _r((_f(g("52WeekChange")) or 0) * 100, 2) if g("52WeekChange") is not None else None,
            "sp_chg_52w": _r((_f(g("SandP52WeekChange")) or 0) * 100, 2)
            if g("SandP52WeekChange") is not None else None,
        },
        # Funds: what an ETF page needs instead of an income statement.
        "fund": {
            "assets": _f(g("totalAssets")), "expense": _f(g("netExpenseRatio")),
            "yield": _r((_f(g("yield")) or 0) * 100, 2) if g("yield") is not None else None,
            "family": g("fundFamily"), "category": g("category"), "nav": _f(g("navPrice")),
            "ret_3y": _r((_f(g("threeYearAverageReturn")) or 0) * 100, 2)
            if g("threeYearAverageReturn") is not None else None,
            "ret_5y": _r((_f(g("fiveYearAverageReturn")) or 0) * 100, 2)
            if g("fiveYearAverageReturn") is not None else None,
        } if g("totalAssets") or g("netExpenseRatio") is not None else None,
        "analyst": {
            "rating": g("averageAnalystRating"), "key": g("recommendationKey"),
            "n": _i(g("numberOfAnalystOpinions")),
            "target_mean": _f(g("targetMeanPrice")), "target_median": _f(g("targetMedianPrice")),
            "target_high": _f(g("targetHighPrice")), "target_low": _f(g("targetLowPrice")),
        },
        "dates": {
            "earnings": ts_date("earningsTimestampStart") or ts_date("earningsTimestamp"),
            "earnings_end": ts_date("earningsTimestampEnd"),
            "earnings_ts": _i(g("earningsTimestamp")),
            "earnings_call_ts": _i(g("earningsCallTimestampStart")),
            "earnings_estimated": bool(g("isEarningsDateEstimate")),
            "ex_div": ts_date("exDividendDate"), "div_pay": ts_date("dividendDate"),
            "last_split": ts_date("lastSplitDate"), "last_split_factor": g("lastSplitFactor"),
        },
        "source": "Yahoo Finance",
    }
    if out["stats"].get("beta") is None:
        try:
            b1 = beta_1y(sym, out)
        except Exception:
            b1 = None
        if b1:
            out["stats"]["beta"], out["stats"]["beta_basis"] = b1["beta"], b1["basis"]
    return one_currency(out)


def beta_1y(sym: str, ov: dict) -> Optional[dict]:
    """
    A beta measured here, for a share Yahoo gives none. Yahoo's is five years
    of monthly returns, so a company listed for less has none: Sandisk, spun
    off from Western Digital in February 2025. One year of overlapping
    five-day returns against its own market (the S&P 500, the AEX, the DAX),
    the way the book's beta is measured (risk.HORIZON): daily returns against
    a market that closes at another hour understate it.
    """
    from . import risk
    bm = benchmarks_for(sym, ov)["market"]
    if bm["sym"] == sym:
        return None
    cut = str(date.today() - timedelta(days=365))
    mine = {b[0]: b[6] for b in daily(sym)["bars"] if b[0] >= cut}
    mkt = {d: v for d, v in _closes(bm["sym"]).items() if d >= cut}
    ys, xs = risk.price_windows(mine, mkt)
    if len(xs) < 120:                    # about half a year of sessions at least
        return None
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    var = sum((x - mx) ** 2 for x in xs)
    if not var:
        return None
    beta = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / var
    return {"beta": round(beta, 2), "basis": f"one year of five-day returns against the {bm['label']}", "n": len(xs)}


def one_currency(ov: dict) -> dict:
    """
    Yahoo's statistics with the reported figures put in the price's currency.

    Yahoo divides a dollar price by euro or Taiwan-dollar figures: ASML's
    New York line came out at an EV/EBITDA of 2,158 and an enterprise value of
    $34.9 trillion, TSMC's at a price/sales of 0.43. The cash, debt, revenue,
    EBITDA and free cash flow are converted at today's rate and the ratios
    built again from them; price/book, which has no figure here to rebuild
    from, is dropped. P/E and forward P/E are left as they are: Yahoo already
    converts the earnings per share behind them.
    """
    st = ov.get("stats") or {}
    fin, ccy = ov.get("fin_currency"), ov.get("currency")
    if not fin or not ccy or major(fin)[0] == major(ccy)[0]:
        return ov
    m = FX(fin, ccy).total()
    ov["converted"] = {"from": fin, "to": major(ccy)[0], "rate": _r(m, 6)}
    keys = ("cash", "debt", "revenue", "ebitda", "fcf", "ocf")
    for k in keys:
        st[k] = st[k] * m if (m and st.get(k) is not None) else None
    mcap = st.get("market_cap")
    ev = mcap + (st.get("debt") or 0) - (st.get("cash") or 0) if mcap and m else None
    st["ev"] = ev
    st["ev_ebitda"] = round(ev / st["ebitda"], 3) if ev and st.get("ebitda") and st["ebitda"] > 0 else None
    st["ev_rev"] = round(ev / st["revenue"], 3) if ev and st.get("revenue") else None
    st["ps"] = round(mcap / st["revenue"], 3) if mcap and st.get("revenue") else None
    st["fcf_yield"] = round(st["fcf"] / mcap * 100, 2) if mcap and st.get("fcf") else None
    st["pb"] = None
    st["eps_cur_year"] = None          # this year's estimate, in the reporting currency: the street panel has it with its currency
    return ov


_CORP = re.compile(r"[,.]?\s+(corporation|corp|incorporated|inc|holdings?|group|plc|"
                   r"n\.?v\.?|s\.?a\.?|ag|se|ltd|limited|co|company|class [a-c]|"
                   r"the|platforms|technologies)\.?$", re.I)


def _short_name(n: str) -> str:
    """'Microsoft Corporation' -> 'Microsoft', for matching headlines."""
    n = (n or "").strip()
    for _ in range(3):
        m = _CORP.sub("", n).strip(" ,.")
        if m == n:
            break
        n = m
    return n


def overview(sym: str) -> dict:
    return store.cache_through(f"rs:ov:{sym}", TTL["overview"], lambda: _load_overview(sym),
                               stale_ok=True)


# --------------------------------------------------------------------------
# price history
# --------------------------------------------------------------------------

def _load_daily(sym: str, period: str = "10y") -> dict:
    h = _tk(sym).history(period=period, interval="1d", auto_adjust=False, actions=True)
    if h is None or h.empty:
        raise NotFound(f"no price history for {sym}")
    bars, divs, splits = [], [], []
    has_adj = "Adj Close" in h.columns
    for idx, row in h.iterrows():
        d = str(idx.date())
        c = _f(row.get("Close"))
        if c is None:
            continue
        o, hi, lo = _f(row.get("Open")) or c, _f(row.get("High")) or c, _f(row.get("Low")) or c
        v = _f(row.get("Volume")) or 0
        ac = _f(row.get("Adj Close")) if has_adj else c
        bars.append([d, round(o, 4), round(hi, 4), round(lo, 4), round(c, 4), int(v),
                     round(ac or c, 4)])
        dv = _f(row.get("Dividends"))
        if dv:
            divs.append([d, round(dv, 4)])
        sp = _f(row.get("Stock Splits"))
        if sp:
            splits.append([d, sp])
    return {"symbol": sym, "bars": bars, "divs": divs, "splits": splits,
            "cols": ["d", "o", "h", "l", "c", "v", "ac"], "source": "Yahoo Finance"}


def daily(sym: str) -> dict:
    return store.cache_through(f"rs:daily:{sym}", TTL["daily"], lambda: _load_daily(sym))


def _load_intraday(sym: str, rng: str) -> dict:
    period, interval = {"1D": ("1d", "5m"), "5D": ("5d", "15m")}.get(rng, ("1d", "5m"))
    h = _tk(sym).history(period=period, interval=interval, auto_adjust=False, prepost=False)
    if h is None or h.empty:
        raise NotFound(f"no intraday prices for {sym}")
    bars = []
    for idx, row in h.iterrows():
        c = _f(row.get("Close"))
        if c is None:
            continue
        bars.append([idx.isoformat(), _r(row.get("Open"), 4) or c, _r(row.get("High"), 4) or c,
                     _r(row.get("Low"), 4) or c, round(c, 4), int(_f(row.get("Volume")) or 0)])
    return {"symbol": sym, "range": rng, "interval": interval, "bars": bars}


def intraday(sym: str, rng: str = "1D") -> dict:
    rng = rng if rng in ("1D", "5D") else "1D"
    return store.cache_through(f"rs:intra:{sym}:{rng}", TTL["intraday"],
                               lambda: _load_intraday(sym, rng))


# Benchmarks: a broad index for the listing's market, and a sector fund.
MARKET_FOR_SUFFIX = {
    ".AS": ("^AEX", "AEX"), ".DE": ("^GDAXI", "DAX"), ".PA": ("^FCHI", "CAC 40"),
    ".L": ("^FTSE", "FTSE 100"), ".MI": ("FTSEMIB.MI", "FTSE MIB"),
    ".SW": ("^SSMI", "SMI"), ".MC": ("^IBEX", "IBEX 35"), ".BR": ("^BFX", "BEL 20"),
    ".CO": ("^OMXC25", "OMX C25"), ".ST": ("^OMX", "OMX S30"), ".TO": ("^GSPTSE", "TSX"),
    ".HK": ("^HSI", "Hang Seng"), ".T": ("^N225", "Nikkei 225"),
}
SECTOR_ETF = {
    "Technology": ("XLK", "Tech (XLK)"), "Communication Services": ("XLC", "Comms (XLC)"),
    "Consumer Cyclical": ("XLY", "Discretionary (XLY)"),
    "Consumer Defensive": ("XLP", "Staples (XLP)"), "Healthcare": ("XLV", "Health (XLV)"),
    "Financial Services": ("XLF", "Financials (XLF)"), "Energy": ("XLE", "Energy (XLE)"),
    "Industrials": ("XLI", "Industrials (XLI)"), "Utilities": ("XLU", "Utilities (XLU)"),
    "Basic Materials": ("XLB", "Materials (XLB)"), "Real Estate": ("XLRE", "Real estate (XLRE)"),
}
# Where a sector fund is too blunt: a chip designer does not trade with Apple.
INDUSTRY_ETF = [
    (re.compile(r"semiconductor", re.I), ("SOXX", "Semis (SOXX)")),
    (re.compile(r"software", re.I), ("IGV", "Software (IGV)")),
    (re.compile(r"biotech", re.I), ("XBI", "Biotech (XBI)")),
    (re.compile(r"banks.*regional", re.I), ("KRE", "Regional banks (KRE)")),
    (re.compile(r"oil & gas e&p", re.I), ("XOP", "Oil & gas E&P (XOP)")),
    (re.compile(r"aerospace|defense", re.I), ("ITA", "Aerospace & defense (ITA)")),
    (re.compile(r"internet retail", re.I), ("XLY", "Discretionary (XLY)")),
]


def benchmarks_for(sym: str, ov: Optional[dict]) -> dict:
    suffix = next((s for s in MARKET_FOR_SUFFIX if sym.endswith(s)), None)
    market = MARKET_FOR_SUFFIX.get(suffix, ("^GSPC", "S&P 500"))
    sector = None
    ind, sct = (ov or {}).get("industry") or "", (ov or {}).get("sector") or ""
    for rx, etf in INDUSTRY_ETF:
        if rx.search(ind):
            sector = etf
            break
    if not sector and sct in SECTOR_ETF:
        sector = SECTOR_ETF[sct]
    if sector and sector[0] == sym:
        sector = SECTOR_ETF.get(sct) if SECTOR_ETF.get(sct, (None,))[0] != sym else None
    return {"market": {"sym": market[0], "label": market[1]},
            "sector": {"sym": sector[0], "label": sector[1]} if sector else None}


def _closes(sym: str) -> dict:
    """{date: adjusted close} for a benchmark, from the same cache."""
    try:
        return {b[0]: b[6] for b in daily(sym)["bars"]}
    except Exception:
        return {}


def chart(sym: str) -> dict:
    """Ten years of daily bars plus the benchmarks, aligned to the stock's dates."""
    d = daily(sym)
    try:
        ov = overview(sym)
    except Exception:
        ov = {}
    bm = benchmarks_for(sym, ov)
    days = [b[0] for b in d["bars"]]
    out_bm = {}
    for k, b in bm.items():
        if not b:
            continue
        cl = _closes(b["sym"])
        if not cl:
            continue
        ser, last = [], None
        for day in days:
            last = cl.get(day, last)
            ser.append(last)
        out_bm[k] = {**b, "c": ser}
    return {**{k: v for k, v in d.items() if not k.startswith("_")},
            "benchmarks": out_bm, "currency": (ov or {}).get("currency"),
            "_age_s": d.get("_age_s")}


# --------------------------------------------------------------------------
# the street: consensus, revisions, surprises, targets, analyst actions
# --------------------------------------------------------------------------

PERIOD_LABEL = {"0q": "Current quarter", "+1q": "Next quarter",
                "0y": "Current fiscal year", "+1y": "Next fiscal year", "LTG": "Long-term"}


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _trading_days(sym: str) -> list[str]:
    try:
        return [b[0] for b in daily(sym)["bars"]]
    except Exception:
        return []


def _next_session(days: list[str], d: str, after: bool) -> Optional[str]:
    """The session an event on day d moves: d itself, or the next one if after the close."""
    i = bisect.bisect_right(days, d) if after else bisect.bisect_left(days, d)
    return days[i] if i < len(days) else None


def _reaction(bars_by_day: dict, days: list[str], day: Optional[str]) -> Optional[dict]:
    """Close-to-close move on `day`, with the gap at the open."""
    if not day or day not in bars_by_day:
        return None
    i = days.index(day)
    if i == 0:
        return None
    prev = bars_by_day[days[i - 1]]
    b = bars_by_day[day]
    return {"day": day, "move": _pct(b[6], prev[6]), "gap": _pct(b[1], prev[4])}


def _load_street(sym: str) -> dict:
    t = _tk(sym)
    ee = _safe(lambda: t.earnings_estimate)
    re_ = _safe(lambda: t.revenue_estimate)
    tr = _safe(lambda: t.eps_trend)
    rv = _safe(lambda: t.eps_revisions)
    gr = _safe(lambda: t.growth_estimates)

    periods = []
    for key in ("0q", "+1q", "0y", "+1y"):
        row = {"period": key, "label": PERIOD_LABEL[key]}
        try:
            e = ee.loc[key]
            row["eps"] = {"avg": _r(e.get("avg")), "low": _r(e.get("low")), "high": _r(e.get("high")),
                          "year_ago": _r(e.get("yearAgoEps")), "n": _i(e.get("numberOfAnalysts")),
                          "growth": _r((_f(e.get("growth")) or 0) * 100, 2)
                          if _f(e.get("growth")) is not None else None}
        except Exception:
            pass
        try:
            r = re_.loc[key]
            row["rev"] = {"avg": _f(r.get("avg")), "low": _f(r.get("low")), "high": _f(r.get("high")),
                          "year_ago": _f(r.get("yearAgoRevenue")), "n": _i(r.get("numberOfAnalysts")),
                          "growth": _r((_f(r.get("growth")) or 0) * 100, 2)
                          if _f(r.get("growth")) is not None else None}
        except Exception:
            pass
        try:
            x = tr.loc[key]
            cur, ago = _f(x.get("current")), {k: _f(x.get(k)) for k in
                                               ("7daysAgo", "30daysAgo", "60daysAgo", "90daysAgo")}
            row["trend"] = {"current": cur, **ago,
                            "chg_30d": _pct(cur, ago["30daysAgo"]),
                            "chg_90d": _pct(cur, ago["90daysAgo"])}
        except Exception:
            pass
        try:
            x = rv.loc[key]
            row["revisions"] = {"up7": _i(x.get("upLast7days")), "up30": _i(x.get("upLast30days")),
                                "down30": _i(x.get("downLast30days")),
                                "down7": _i(x.get("downLast7Days"))}
        except Exception:
            pass
        try:
            x = gr.loc[key]
            row["index_growth"] = _r((_f(x.get("indexTrend")) or 0) * 100, 2) \
                if _f(x.get("indexTrend")) is not None else None
        except Exception:
            pass
        periods.append(row)
    ltg = None
    try:
        ltg = _r((_f(gr.loc["LTG"].get("stockTrend")) or 0) * 100, 2) \
            if _f(gr.loc["LTG"].get("stockTrend")) is not None else None
    except Exception:
        pass

    # Surprise history, with what the stock did about it.
    days = _trading_days(sym)
    try:
        bars_by_day = {b[0]: b for b in daily(sym)["bars"]}
    except Exception:
        bars_by_day = {}
    history, upcoming = [], None
    ed = _safe(lambda: t.get_earnings_dates(limit=28))
    if ed is not None and not ed.empty:
        for idx, row in ed.iterrows():
            est, rep = _f(row.get("EPS Estimate")), _f(row.get("Reported EPS"))
            sur = _f(row.get("Surprise(%)"))
            d = str(idx.date())
            hour = idx.hour + idx.minute / 60
            when = "after close" if hour >= 16 else ("before open" if hour < 9.5 else "during session")
            rec = {"date": d, "when": when, "eps_est": est, "eps_act": rep,
                   "surprise_pct": _r(sur, 2)}
            if rep is None:
                if d >= date.today().isoformat():
                    upcoming = rec if (upcoming is None or d < upcoming["date"]) else upcoming
                continue
            rec["reaction"] = _reaction(bars_by_day, days,
                                        _next_session(days, d, after=hour >= 16))
            history.append(rec)
    history.sort(key=lambda r: r["date"])

    pt = _safe(lambda: t.analyst_price_targets) or {}
    recs = []
    rd = _safe(lambda: t.recommendations)
    if rd is not None and not rd.empty:
        for _, row in rd.iterrows():
            recs.append({"period": str(row.get("period")),
                         **{k: _i(row.get(k)) or 0 for k in
                            ("strongBuy", "buy", "hold", "sell", "strongSell")}})

    actions = []
    ud = _safe(lambda: t.upgrades_downgrades)
    if ud is not None and not ud.empty:
        for idx, row in ud.head(600).iterrows():
            ts = idx.to_pydatetime().replace(tzinfo=timezone.utc) if idx.tzinfo is None \
                else idx.to_pydatetime()
            actions.append({
                "ts": ts.timestamp(), "date": ts.date().isoformat(),
                "firm": row.get("Firm"), "to": row.get("ToGrade") or None,
                "from": row.get("FromGrade") or None, "action": row.get("Action"),
                "pt_action": row.get("priceTargetAction") or None,
                "pt": _f(row.get("currentPriceTarget")) or None,
                "pt_prior": _f(row.get("priorPriceTarget")) or None,
            })

    cal = _safe(lambda: t.calendar) or {}
    nxt = cal.get("Earnings Date") or []
    # Which currency each figure is in. Yahoo's tables do not say, and they
    # differ by company: TSMC's EPS forecasts are dollars per American share,
    # its revenue forecasts Taiwan dollars; ASML's are all euros.
    qs = _safe(lambda: summary(sym)) or {}
    tr_c = qs.get("trend") or {}
    for row in periods:
        c = tr_c.get(row["period"]) or {}
        row["end"] = c.get("end")
        row["eps_ccy"], row["rev_ccy"] = c.get("eps_ccy"), c.get("rev_ccy")
    c0 = tr_c.get("0y") or tr_c.get("0q") or {}
    ccys = {"eps": c0.get("eps_ccy"), "rev": c0.get("rev_ccy"), "hist": qs.get("hist_ccy")}
    if upcoming and upcoming.get("date"):
        upcoming["period_end"] = (tr_c.get("0q") or {}).get("end")
    _fiscal_names(periods)
    actions.sort(key=lambda a: a["ts"], reverse=True)
    brokers, path = _brokers(actions, bars_by_day, days)
    out = {
        "symbol": sym, "periods": periods, "ltg": ltg, "currency": ccys,
        "n_targets": qs.get("n_targets"),
        "surprises": history[-16:], "upcoming": upcoming,
        "next_earnings": [str(x) for x in nxt] if isinstance(nxt, list) else [str(nxt)],
        "targets": {k: _f(v) for k, v in pt.items()} if isinstance(pt, dict) else {},
        "recommendations": recs, "actions": actions[:80], "n_actions": len(actions),
        "after": _after_reports(history, bars_by_day, days, periods),
        "brokers": brokers, "target_path": path,
        "zacks": _safe(lambda: zacks(sym)),
        "source": "Yahoo Finance consensus",
    }
    # Every fresh read is a day in the consensus record (pf/consensus.py).
    try:
        from . import consensus
        consensus.record(sym, consensus.from_street(out, _safe(lambda: overview(sym))), "page")
    except Exception:
        pass
    return out


def street(sym: str) -> dict:
    return store.cache_through(f"rs:street:{sym}", TTL["street"], lambda: _load_street(sym))


def _fiscal_names(periods: list[dict]) -> None:
    """'Q1 FY27' and 'FY27' for Microsoft, not 'current quarter': the year's own end gives the fiscal calendar."""
    y = next((p for p in periods if p["period"] == "0y" and p.get("end")), None)
    fye = (y["end"][5:7] + y["end"][8:10]) if y else None
    for p in periods:
        if p.get("end"):
            p["fiscal"] = fiscal_label(p["end"], fye, annual=p["period"] in ("0y", "+1y"))


TREND_MARKS = ((7, "7daysAgo"), (30, "30daysAgo"), (60, "60daysAgo"), (90, "90daysAgo"))


def _after_reports(history: list[dict], bars_by_day: dict, days: list[str], periods: list[dict],
                   today: Optional[date] = None) -> list[dict]:
    """
    What followed each report: the beat, the next session, five and twenty
    sessions on, and for the latest one how the forecasts moved since. ASML
    beat in eleven of twelve and fell the next day in eight: the beat is not
    what moves the stock, the forecasts after it are.
    """
    today = today or date.today()
    out = []
    for h in history[-12:]:
        rx = h.get("reaction") or {}
        day = rx.get("day")
        if not day or day not in bars_by_day:
            continue
        i = days.index(day)
        if i == 0:
            continue
        base = bars_by_day[days[i - 1]][4]

        def drift(n):
            j = i + n - 1
            return _pct(bars_by_day[days[j]][4], base) if j < len(days) else None
        out.append({"date": h["date"], "day": day, "surprise_pct": h.get("surprise_pct"),
                    "eps_act": h.get("eps_act"), "eps_est": h.get("eps_est"),
                    "move": rx.get("move"), "d5": drift(5), "d20": drift(20),
                    "since": _pct(bars_by_day[days[-1]][4], base), "sessions": len(days) - i})
    if out:
        last = out[-1]
        since = (today - _dt(last["date"])).days
        mark = next((c for n, c in TREND_MARKS if since < n), None)
        if mark:
            est = []
            for p in periods:
                tr = p.get("trend") or {}
                b, n = tr.get(mark), tr.get("current")
                if b and n:
                    est.append({"period": p["period"], "fiscal": p.get("fiscal") or p.get("label"),
                                "before": b, "now": n, "chg": _pct(n, b)})
            last["est"] = {"mark": mark, "days": since, "periods": est}
    return out


def _brokers(actions: list[dict], bars_by_day: dict, days: list[str],
             today: Optional[date] = None) -> tuple[list[dict], Optional[list]]:
    """
    One row per firm: its latest view and standing target, greyed when it is
    more than six months old. And the consensus target rebuilt week by week
    from the same actions, each firm's latest target for up to a year, set
    against the price: do the targets lead the stock, or follow it?
    """
    today = today or date.today()
    latest: dict = {}
    for a in actions:                                   # newest first
        f = a.get("firm")
        if not f:
            continue
        if f not in latest:
            latest[f] = dict(a)
        elif not latest[f].get("pt") and a.get("pt"):
            latest[f]["pt"] = a["pt"]
    rows = []
    for f, a in latest.items():
        age = (today - _dt(a["date"])).days
        if age > 730:
            continue
        rows.append({"firm": f, "date": a["date"], "rating": a.get("to"), "from": a.get("from"),
                     "action": a.get("action"), "pt": a.get("pt"), "pt_prior": a.get("pt_prior"),
                     "age": age, "stale": age > 180})
    rows.sort(key=lambda r: r["date"], reverse=True)

    acts = sorted((a for a in actions if a.get("pt") and a.get("firm")), key=lambda a: a["date"])
    if len(acts) < 3:
        return rows, None
    start = max(_dt(acts[0]["date"]), today - timedelta(days=400))
    path, d, k, standing = [], start, 0, {}
    while d <= today:
        ds = d.isoformat()
        while k < len(acts) and acts[k]["date"] <= ds:
            standing[acts[k]["firm"]] = (acts[k]["pt"], _dt(acts[k]["date"]))
            k += 1
        vals = sorted(pt for pt, ad in standing.values() if (d - ad).days <= 365)
        if len(vals) >= 3:
            j = bisect.bisect_right(days, ds) - 1
            path.append({"d": ds, "mean": round(statistics.mean(vals), 2), "median": round(statistics.median(vals), 2),
                         "lo": vals[0], "hi": vals[-1], "n": len(vals),
                         "price": bars_by_day[days[j]][4] if j >= 0 else None})
        d += timedelta(days=7)
    return rows, (path or None)


# Zacks' consensus, as Nasdaq publishes it: five quarters and four fiscal
# years, where Yahoo stops at next year. Fewer analysts, so it is a second
# line, never the first. In US dollars per US-listed share (TSMC 16.65 against
# Yahoo's 17.50 for 2026; Shell 11.62 for an American share of two).
NASDAQ_HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/128.0 Safari/537.36",
              "Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
              "Referer": "https://www.nasdaq.com/"}
_MON = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _month_end(s: str) -> Optional[str]:
    try:
        mon, yr = s.split()
        m, y = _MON[mon[:3]], int(yr)
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        return (nxt - timedelta(days=1)).isoformat()
    except Exception:
        return None


def _load_zacks(us: str) -> dict:
    raw = net.get(f"https://api.nasdaq.com/api/analyst/{us}/earnings-forecast", NASDAQ_HDR, timeout=15)
    d = (json.loads(raw.decode("utf-8", "replace")).get("data")) or {}

    def rows(key):
        out = []
        for r in ((d.get(key) or {}).get("rows")) or []:
            out.append({"fiscal_end": r.get("fiscalEnd"), "end": _month_end(r.get("fiscalEnd") or ""),
                        "eps": _f(r.get("consensusEPSForecast")), "hi": _f(r.get("highEPSForecast")),
                        "lo": _f(r.get("lowEPSForecast")), "n": _i(r.get("noOfEstimates")),
                        "up": _i(r.get("up")), "down": _i(r.get("down"))})
        return out
    years = rows("yearlyForecast")
    for a, b in zip(years, years[1:]):
        b["growth"] = _pct(b["eps"], a["eps"]) if a.get("eps") and a["eps"] > 0 else None
    if not years:
        raise NotFound(f"Nasdaq has no forecasts for {us}")
    return {"symbol": us, "years": years, "quarters": rows("quarterlyForecast"),
            "source": "Zacks consensus via Nasdaq", "per": "US$ per US-listed share"}


def zacks(sym: str) -> Optional[dict]:
    """For a US listing its own; for ASML.AS or SHELL.AS, the US line's (growth only: another share, another currency)."""
    us, same = sym, True
    if re.search(r"\.[A-Z]{1,3}$", sym):
        ci = filer_cik(sym, _safe(lambda: overview(sym)))
        us, same = ((ci or {}).get("via") or None), False
    if not us or "=" in us or us.startswith("^"):
        return None
    z = store.cache_through(f"rs:zacks:{us}", 12 * 3600, lambda: _load_zacks(us))
    return {**{k: v for k, v in z.items() if not k.startswith("_")}, "same_listing": same}


# --------------------------------------------------------------------------
# fundamentals: the company's own numbers
# --------------------------------------------------------------------------

FLOW_KEYS = ("revenue", "cost_of_revenue", "gross_profit", "rnd", "sga", "op_income",
             "pretax", "tax", "net_income", "ocf", "capex", "acquisitions", "sbc",
             "buybacks", "dividends", "dna", "interest")
INST_KEYS = ("cash", "st_inv", "debt", "debt_st", "equity", "assets", "ppe", "goodwill",
             "rpo", "deferred_rev", "inventory", "receivables")


def _dt(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def _near(lst: list, end: str, tol: int = 12) -> Optional[dict]:
    e = _dt(end)
    best = None
    for r in lst or []:
        gap = abs((_dt(r["end"]) - e).days)
        if gap <= tol and (best is None or gap < best[0]):
            best = (gap, r)
    return best[1] if best else None


def fiscal_label(end: str, fye: Optional[str], annual: bool = False) -> str:
    """'Q1 FY27' for Microsoft's September quarter. FY = the year it ends in."""
    d = _dt(end) - timedelta(days=7)            # 52/53-week years end a few days late
    fm = int(fye[:2]) if fye and len(str(fye)) == 4 and str(fye)[:2].isdigit() else 12
    fy = d.year if d.month <= fm else d.year + 1
    if annual:
        return f"FY{str(fy)[-2:]}"
    q = ((d.month - fm - 1) % 12) // 3 + 1
    return f"Q{q} FY{str(fy)[-2:]}"


def _fix_basis(rows: list[dict]) -> list[dict]:
    """
    One share count throughout. Alibaba divided each share into eight in
    2019 and its American share stayed one share, eight of the new ones: no
    split in the price history, so the accounts before 2019 count per old
    share. A jump of a whole factor between one period's share count and the
    next puts the older periods on the newer basis.
    """
    for i in range(len(rows) - 1, 0, -1):
        a, b = rows[i - 1].get("shares"), rows[i].get("shares")
        if not a or not b:
            continue
        k = b / a
        if k < 1.8 and k > 1 / 1.8:
            continue
        if not any(abs(k / n - 1) < 0.08 for n in _NICE):
            continue                      # not a whole factor: a real change in the count
        c = _snap(k)
        for r in rows[:i]:
            if r.get("shares"):
                r["shares"] *= c
            if r.get("eps") is not None:
                r["eps"] = round(r["eps"] / c, 4)
    return rows


def _rows_from_sec(ser: dict, fye: Optional[str], freq: str, n: int,
                   splits: Optional[list] = None) -> list[dict]:
    """
    Rows of reported figures. Per-share numbers are put on TODAY's share basis:
    a value is as the newest filing carrying it reported it, so it is scaled by
    the splits that came after THAT filing — never by the splits after the
    period first appeared, which would adjust an already-restated figure twice.
    """
    rev = ser.get("revenue") or ser.get("net_income") or {}
    ends = [r["end"] for r in rev.get(freq, [])][-n:]
    rows = []
    for e in ends:
        row = {"end": e, "label": fiscal_label(e, fye, annual=(freq == "a")), "derived": False}
        for k in FLOW_KEYS + ("eps", "shares"):
            r = _near((ser.get(k) or {}).get(freq), e)
            row[k] = r["v"] if r else None
            if r and splits and k in ("eps", "shares"):
                f = _split_factor(splits, r.get("filed") or "")
                row[k] = row[k] / f if k == "eps" else row[k] * f
            if r and r.get("derived") and k in ("revenue", "op_income", "net_income", "eps"):
                row["derived"] = True
            if k == "revenue" and r:
                row["filed"] = r.get("first") or r.get("filed")
            if k == "eps" and row[k] is not None:
                row[k] = round(row[k], 4)
        for k in INST_KEYS:
            r = _near((ser.get(k) or {}).get("i"), e)
            row[k] = r["v"] if r else None
        if row.get("eps") is None and row.get("net_income") is not None and row.get("shares"):
            row["eps"] = round(row["net_income"] / row["shares"], 4)
            row["derived"] = True
        rows.append(row)
    return _fix_basis(rows)


YF_MAP = {
    "revenue": ["Total Revenue", "Operating Revenue"], "cost_of_revenue": ["Cost Of Revenue"],
    "gross_profit": ["Gross Profit"], "rnd": ["Research And Development"],
    "sga": ["Selling General And Administration"],
    "op_income": ["Operating Income", "Total Operating Income As Reported"],
    "pretax": ["Pretax Income"], "tax": ["Tax Provision"],
    "net_income": ["Net Income", "Net Income Common Stockholders"],
    "eps": ["Diluted EPS", "Basic EPS"], "shares": ["Diluted Average Shares", "Basic Average Shares"],
    "interest": ["Interest Expense"], "dna": ["Reconciled Depreciation",
                                              "Depreciation And Amortization"],
    "ocf": ["Operating Cash Flow"], "capex": ["Capital Expenditure"],
    "acquisitions": ["Purchase Of Business"], "sbc": ["Stock Based Compensation"],
    "buybacks": ["Repurchase Of Capital Stock"], "dividends": ["Cash Dividends Paid"],
    "cash": ["Cash And Cash Equivalents"], "st_inv": ["Other Short Term Investments"],
    "debt": ["Total Debt"], "equity": ["Stockholders Equity"], "assets": ["Total Assets"],
    "ppe": ["Net PPE"], "goodwill": ["Goodwill"], "inventory": ["Inventory"],
    "receivables": ["Accounts Receivable"],
    "deferred_rev": ["Current Deferred Revenue", "Current Deferred Liabilities"],
}
# Cash flows Yahoo reports as negative outflows; stored here as positive spend.
_OUTFLOWS = {"capex", "acquisitions", "buybacks", "dividends"}


def _rows_from_yf(sym: str, freq: str, fye: Optional[str]) -> list[dict]:
    t = _tk(sym)
    frames = ([_safe(lambda: t.quarterly_income_stmt), _safe(lambda: t.quarterly_cashflow),
               _safe(lambda: t.quarterly_balance_sheet)] if freq == "q" else
              [_safe(lambda: t.income_stmt), _safe(lambda: t.cashflow),
               _safe(lambda: t.balance_sheet)])
    ends = set()
    for f in frames:
        if f is not None and not f.empty:
            ends |= {str(c.date()) for c in f.columns}
    rows = []
    for e in sorted(ends):
        row = {"end": e, "label": fiscal_label(e, fye, annual=(freq == "a")), "derived": False}
        for k, names in YF_MAP.items():
            v = None
            for f in frames:
                if f is None or f.empty:
                    continue
                col = next((c for c in f.columns if str(c.date()) == e), None)
                if col is None:
                    continue
                for nm in names:
                    if nm in f.index:
                        v = _f(f.at[nm, col])
                        if v is not None:
                            break
                if v is not None:
                    break
            if v is not None and k in _OUTFLOWS:
                v = abs(v)
            row[k] = v
        row["debt_st"] = None
        rows.append(row)
    # Yahoo pads the oldest column with blanks; a row with no revenue is noise.
    return [r for r in rows if r.get("revenue") is not None]


def _enrich(rows: list[dict], freq: str) -> list[dict]:
    """Margins, free cash flow and growth, on rows already in date order."""
    lag = 4 if freq == "q" else 1
    for i, r in enumerate(rows):
        rev = r.get("revenue")
        if r.get("gross_profit") is None and rev is not None and r.get("cost_of_revenue") is not None:
            r["gross_profit"] = rev - r["cost_of_revenue"]
        r["fcf"] = (r["ocf"] - (r.get("capex") or 0)) if r.get("ocf") is not None else None
        for k, base in (("gross_margin", "gross_profit"), ("op_margin", "op_income"),
                        ("net_margin", "net_income"), ("fcf_margin", "fcf"),
                        ("rnd_pct", "rnd"), ("sbc_pct", "sbc"), ("capex_pct", "capex")):
            v = _div(r.get(base), rev)
            r[k] = round(v * 100, 2) if v is not None else None
        prev = None
        if i >= lag:
            cand = rows[i - lag]
            # Only a comparison a year apart counts; a gap in the series must
            # not turn a two-year change into a "year-on-year" one.
            if 330 <= (_dt(r["end"]) - _dt(cand["end"])).days <= 400:
                prev = cand
        for k in ("revenue", "op_income", "net_income", "eps", "fcf", "capex", "gross_profit"):
            a, b = r.get(k), (prev or {}).get(k)
            r[k + "_yoy"] = round((a / b - 1) * 100, 2) if a is not None and b and b > 0 else None
    return rows


def _ttm(rows_q: list[dict], end_idx: int) -> Optional[dict]:
    """Sums of four consecutive quarters ending at rows_q[end_idx]."""
    if end_idx < 3:
        return None
    win = rows_q[end_idx - 3:end_idx + 1]
    span = (_dt(win[-1]["end"]) - _dt(win[0]["end"])).days
    if not 250 <= span <= 300:
        return None
    out = {"end": win[-1]["end"]}
    for k in FLOW_KEYS + ("eps", "fcf"):
        vals = [w.get(k) for w in win]
        out[k] = sum(vals) if all(v is not None for v in vals) else None
    for k in INST_KEYS + ("shares",):
        out[k] = win[-1].get(k)
    return out


def _nopat(r: dict) -> Optional[float]:
    oi = r.get("op_income")
    if oi is None:
        return None
    rate = _div(r.get("tax"), r.get("pretax"))
    rate = 0.21 if rate is None or not 0 <= rate <= 0.45 else rate
    return oi * (1 - rate)


def _invested(r: dict) -> Optional[float]:
    eq = r.get("equity")
    if eq is None:
        return None
    return eq + (r.get("debt") or 0) + (r.get("debt_st") or 0) - (r.get("cash") or 0) \
        - (r.get("st_inv") or 0)


def _investments(annual: list[dict], quarters: list[dict]) -> dict:
    """
    Is the money being spent earning a return? The question behind "are the
    investments working", answered from the reported numbers only.
    """
    rows = []
    for i, r in enumerate(annual):
        nop, ic = _nopat(r), _invested(r)
        prev_ic = _invested(annual[i - 1]) if i else None
        avg_ic = (ic + prev_ic) / 2 if ic is not None and prev_ic is not None else ic
        rows.append({
            "end": r["end"], "label": r["label"], "revenue": r.get("revenue"),
            "rev_yoy": r.get("revenue_yoy"), "capex": r.get("capex"),
            "capex_pct": r.get("capex_pct"), "capex_yoy": r.get("capex_yoy"),
            "dna": r.get("dna"),
            "capex_dna": round(r["capex"] / r["dna"], 2) if r.get("capex") and r.get("dna") else None,
            "rnd": r.get("rnd"), "rnd_pct": r.get("rnd_pct"),
            "acquisitions": r.get("acquisitions"), "nopat": nop, "invested": ic,
            "roic": round(nop / avg_ic * 100, 2) if nop is not None and avg_ic and avg_ic > 0 else None,
            "fcf": r.get("fcf"),
            "fcf_conv": round(r["fcf"] / r["net_income"] * 100, 1)
            if r.get("fcf") is not None and r.get("net_income") and r["net_income"] > 0 else None,
            "sbc_pct": r.get("sbc_pct"),
            "returned": (r.get("buybacks") or 0) + (r.get("dividends") or 0),
            "shares": r.get("shares"), "ppe": r.get("ppe"),
            "asset_turn": round(r["revenue"] / r["ppe"], 2) if r.get("revenue") and r.get("ppe") else None,
        })

    t_now = _ttm(quarters, len(quarters) - 1) if quarters else None
    t_ago = _ttm(quarters, len(quarters) - 5) if len(quarters) >= 8 else None
    ttm = None
    if t_now:
        ttm = {"end": t_now["end"], "revenue": t_now.get("revenue"), "capex": t_now.get("capex"),
               "ocf": t_now.get("ocf"), "fcf": t_now.get("fcf"), "op_income": t_now.get("op_income"),
               "rev_yoy": _pct(t_now.get("revenue"), (t_ago or {}).get("revenue")),
               "capex_yoy": _pct(t_now.get("capex"), (t_ago or {}).get("capex")),
               "op_income_yoy": _pct(t_now.get("op_income"), (t_ago or {}).get("op_income")),
               "fcf_yoy": _pct(t_now.get("fcf"), (t_ago or {}).get("fcf")),
               "capex_pct": round(t_now["capex"] / t_now["revenue"] * 100, 1)
               if t_now.get("capex") and t_now.get("revenue") else None}

    inc = None
    if len(rows) >= 4:
        a, b = rows[-1], rows[-4]
        d_nop = a["nopat"] - b["nopat"] if a["nopat"] is not None and b["nopat"] is not None else None
        d_ic = a["invested"] - b["invested"] if a["invested"] is not None and b["invested"] is not None else None
        spent = sum((r.get("capex") or 0) + (r.get("acquisitions") or 0) for r in rows[-3:])
        inc = {"from": b["label"], "to": a["label"], "d_nopat": d_nop, "d_invested": d_ic,
               "roiic": round(d_nop / d_ic * 100, 1) if d_nop is not None and d_ic and d_ic > 0 else None,
               "spent_3y": spent,
               "nopat_per_spent": round(d_nop / spent * 100, 1) if d_nop is not None and spent > 0 else None,
               "rev_growth_3y": _pct(a["revenue"], b["revenue"]),
               "capex_growth_3y": _pct(a["capex"], b["capex"])}

    rpo = None
    rq = [q for q in quarters if q.get("rpo")]
    if rq:
        last = rq[-1]
        ago = next((q for q in rq if 330 <= (_dt(last["end"]) - _dt(q["end"])).days <= 400), None)
        rpo = {"end": last["end"], "v": last["rpo"], "yoy": _pct(last["rpo"], (ago or {}).get("rpo")),
               "cover": round(last["rpo"] / t_now["revenue"], 2)
               if t_now and t_now.get("revenue") else None}

    return {"annual": rows, "ttm": ttm, "incremental": inc, "rpo": rpo,
            "findings": _invest_findings(rows, ttm, inc, rpo)}


def _bn(v) -> str:
    v = _f(v)
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e12:
        return f"{v / 1e12:.2f}T"
    if a >= 1e9:
        return f"{v / 1e9:.1f}B"
    if a >= 1e6:
        return f"{v / 1e6:.0f}M"
    return f"{v:,.0f}"


def _invest_findings(rows, ttm, inc, rpo) -> list[dict]:
    """Plain sentences with a tone. Each rule is here to be argued with."""
    out = []
    if ttm and ttm.get("capex") is not None and ttm.get("rev_yoy") is not None:
        cg, rg = ttm.get("capex_yoy"), ttm["rev_yoy"]
        tone = "info"
        if cg is not None and cg > 20 and cg > 2 * max(rg, 1):
            tone = "warn"
        elif cg is not None and rg >= cg:
            tone = "good"
        out.append({"tone": tone, "k": "capex",
                    "text": f"Capital spending {_bn(ttm['capex'])} over the last four quarters"
                            + (f", {cg:+.0f}% on a year earlier" if cg is not None else "")
                            + f"; revenue {rg:+.0f}%. Capex is {ttm.get('capex_pct') or 0:.0f}% of revenue."})
    live = [r for r in rows if r.get("roic") is not None]
    if len(live) >= 2:
        now, then = live[-1], live[max(0, len(live) - 4)]
        drop = now["roic"] - then["roic"]
        tone = "good" if now["roic"] >= 15 and drop > -5 else ("warn" if drop <= -5 else "info")
        out.append({"tone": tone, "k": "roic",
                    "text": f"Return on invested capital {now['roic']:.0f}% in {now['label']}, "
                            f"against {then['roic']:.0f}% in {then['label']}."})
    if inc and inc.get("roiic") is not None:
        v = inc["roiic"]
        tone = "good" if v >= 15 else ("warn" if v >= 8 else "bad")
        out.append({"tone": tone, "k": "roiic",
                    "text": f"Incremental return {inc['from']}→{inc['to']}: every extra dollar of "
                            f"invested capital added {v:.0f}¢ of after-tax operating profit a year."
                            + (" New money is earning less than the old." if live and v < live[-1]["roic"] - 5 else "")})
    if rows and rows[-1].get("capex_dna"):
        r = rows[-1]["capex_dna"]
        if r >= 1.5:
            out.append({"tone": "info", "k": "build",
                        "text": f"Capex is {r:.1f}× depreciation — the company is building capacity, "
                                "not maintaining it. The return on that shows up with a lag."})
    if rows and rows[-1].get("fcf_conv") is not None:
        c = rows[-1]["fcf_conv"]
        out.append({"tone": "good" if c >= 90 else ("warn" if c < 60 else "info"), "k": "conv",
                    "text": f"Free cash flow was {c:.0f}% of net income in {rows[-1]['label']}."
                            + (" Earnings are turning into cash." if c >= 90 else
                               " Much of the profit is being reinvested before it becomes cash." if c < 60 else "")})
    if rpo and rpo.get("yoy") is not None:
        out.append({"tone": "good" if rpo["yoy"] > 10 else "info", "k": "rpo",
                    "text": f"Contracted backlog (remaining performance obligations) {_bn(rpo['v'])}, "
                            f"{rpo['yoy']:+.0f}% on a year earlier"
                            + (f" — {rpo['cover']:.1f}× a year of revenue already signed." if rpo.get("cover") else ".")})
    sh = [r for r in rows if r.get("shares")]
    if len(sh) >= 4:
        chg = _pct(sh[-1]["shares"], sh[-4]["shares"])
        if chg is not None:
            out.append({"tone": "good" if chg < -1 else ("warn" if chg > 2 else "info"), "k": "shares",
                        "text": f"Diluted share count {chg:+.1f}% over three years"
                                + (" — buybacks outrun dilution." if chg < -1 else
                                   " — dilution is eating into per-share growth." if chg > 2 else ".")})
    return out


QS_URL = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/"


def _iso(ts) -> Optional[str]:
    v = _f(ts)
    return (date(1970, 1, 1) + timedelta(seconds=v)).isoformat() if v else None


def _load_summary(sym: str) -> dict:
    """
    The parts of Yahoo's quote summary that yfinance's tables drop: which
    currency each estimate is in (TSMC's EPS forecasts are in dollars per
    American share, its revenue forecasts in Taiwan dollars), and the latest
    quarter's headline numbers, there days before the full statements.
    """
    from .watch import _yfget, RateLimited
    params = {"modules": "earningsTrend,earningsHistory,earnings,financialData", "formatted": "false", "symbol": sym}
    try:
        js = _yfget(QS_URL + sym, params)
    except RateLimited:
        raise
    except Exception:
        # Yahoo answers "no fundamentals data" now and then for a line it has
        time.sleep(1.5)
        js = _yfget(QS_URL + sym, params)
    res = (((js.get("quoteSummary") or {}).get("result")) or [None])[0]
    if not res:
        raise NotFound(f"no quote summary for {sym}")
    v = lambda x: x.get("raw") if isinstance(x, dict) else x
    trend = {}
    for t in ((res.get("earningsTrend") or {}).get("trend")) or []:
        ee, re_ = t.get("earningsEstimate") or {}, t.get("revenueEstimate") or {}
        trend[t.get("period")] = {"end": t.get("endDate"), "eps_ccy": v(ee.get("earningsCurrency")),
                                  "rev_ccy": v(re_.get("revenueCurrency"))}
    hist = ((res.get("earningsHistory") or {}).get("history")) or []
    e = res.get("earnings") or {}
    chart = [{"cal": x.get("date"), "end": _iso(x.get("periodEndDate")), "reported": _iso(x.get("reportedDate")),
              "eps": _f(v(x.get("actual"))), "est": _f(v(x.get("estimate")))}
             for x in ((e.get("earningsChart") or {}).get("quarterly")) or []]
    fin = [{"cal": x.get("date"), "fiscal": x.get("fiscalQuarter"), "revenue": _f(v(x.get("revenue"))),
            "earnings": _f(v(x.get("earnings")))}
           for x in ((e.get("financialsChart") or {}).get("quarterly")) or []]
    fd = res.get("financialData") or {}
    return {"symbol": sym, "trend": trend,
            "hist_ccy": next((v(h.get("currency")) for h in reversed(hist) if h.get("currency")), None),
            "fin_ccy": e.get("financialCurrency") or v(fd.get("financialCurrency")),
            "chart": chart, "fin": fin, "n_targets": _i(v(fd.get("numberOfAnalystOpinions")))}


def summary(sym: str) -> dict:
    return store.cache_through(f"rs:qs:{sym}", 1800, lambda: _load_summary(sym))


def filer_cik(sym: str, ov: Optional[dict] = None) -> Optional[dict]:
    """
    The SEC filer behind a symbol. ASML.AS, SHELL.AS and UNA.AS are the same
    companies as the New York lines that file 20-Fs: ten years of annual
    accounts, which Yahoo's four years cannot give.
    """
    ci = sec.cik_for(sym)
    if ci or not re.search(r"\.[A-Z]{1,3}$", (sym or "").upper()):
        return ci
    from . import earndocs
    f = _safe(lambda: earndocs.filer(sym, (ov or {}).get("name")))
    return {"cik": f["cik"], "title": f.get("title"), "via": f.get("ticker")} if f else None


_MONEY_KEYS = FLOW_KEYS + INST_KEYS


def _convert_rows(rows: list[dict], frm: Optional[str], to: Optional[str]) -> bool:
    """Money in `rows` from one currency to another, each row at its own period end. False when a rate is missing."""
    fx = FX(frm, to)
    if fx.same:
        return True
    for r in rows:
        m = fx.total(r["end"])
        if m is None:
            return False
        for k in _MONEY_KEYS + ("eps",):
            if r.get(k) is not None:
                r[k] = r[k] * m
    return True


def _snap(r: float) -> float:
    for c in _NICE:
        if abs(r / c - 1) < 0.08:
            return c
    return r


def nf_ratio(k: float) -> str:
    return f"{k:g}" if k >= 1 else f"1/{1 / k:g}"


def _rebase(rows: list[dict], factor: float) -> None:
    """Per-share figures onto another share count: Yahoo counts TSMC per American share, the SEC per Taiwanese one."""
    for r in rows:
        if r.get("eps") is not None:
            r["eps"] = round(r["eps"] / factor, 4)
        if r.get("shares") is not None:
            r["shares"] = r["shares"] * factor


def _provisional(sym: str, quarters: list[dict], ccy: Optional[str], fye: Optional[str],
                 ov: dict) -> list[dict]:
    """
    Quarters reported but not yet in any statement: revenue, net income and
    EPS from the earnings release summary. ASML reported its third quarter on
    14 October; Yahoo's statements still ended in June four days later, and
    the SEC never gets quarterly accounts from a foreign company at all.
    """
    if not quarters:
        return []
    s = _safe(lambda: summary(sym)) or {}
    last = quarters[-1]
    fin = {x["cal"]: x for x in s.get("fin") or [] if x.get("cal")}
    ratio = units_ratio(last.get("shares"), ov)
    out = []
    for c in s.get("chart") or []:
        end = c.get("end")
        if not end:
            continue
        gap = (_dt(end) - _dt(last["end"])).days
        if not 20 <= gap <= 200:
            continue
        f = fin.get(c.get("cal")) or {}
        if f.get("revenue") is None and c.get("eps") is None:
            continue
        m_tot = FX(s.get("fin_ccy") or ccy, ccy).total(end)
        m_eps = FX(s.get("hist_ccy") or ccy, ccy).total(end)
        out.append({
            "end": end, "label": fiscal_label(end, fye), "derived": False, "provisional": True,
            "filed": c.get("reported") or end,
            "revenue": f["revenue"] * m_tot if f.get("revenue") is not None and m_tot else None,
            "net_income": f["earnings"] * m_tot if f.get("earnings") is not None and m_tot else None,
            # per quoted share in the summary, per filer share in the table
            "eps": round(c["eps"] * m_eps / ratio, 4) if c.get("eps") is not None and m_eps else None,
            "shares": last.get("shares"),
        })
    return out


def _load_fundamentals(sym: str) -> dict:
    ov = _safe(lambda: overview(sym)) or {}
    ci = filer_cik(sym, ov)
    fye, src, quarters, annual, unit, err = None, None, [], [], None, None
    if ci:
        try:
            sub = sec.submissions(ci["cik"])
            fye = sub.get("fye")
            fx = sec.facts(ci["cik"])
            ser = fx.get("series") or {}
            if ser.get("revenue") or ser.get("net_income"):
                # A European line's splits are its own; the filer's numbers
                # follow the company, so only a direct match uses them.
                splits = [] if ci.get("via") else ((_safe(lambda: daily(sym)) or {}).get("splits") or [])
                quarters = _rows_from_sec(ser, fye, "q", 20, splits)
                annual = _rows_from_sec(ser, fye, "a", 10, splits)
                unit = sec.unit_currency((ser.get("revenue") or ser.get("net_income") or {}).get("unit"))
                src = f"SEC filings, CIK {ci['cik']}" + (f" (the {ci['via']} filer)" if ci.get("via") else "")
        except Exception as e:
            err = f"SEC: {e.__class__.__name__}: {e}"[:160]
    yf_ccy = ov.get("fin_currency") or ov.get("currency")
    # Foreign filers send the SEC annual reports only (20-F, no quarters), so
    # the quarters come from Yahoo while the years can still come from the SEC.
    from_sec = [k for k, rows in (("quarters", quarters), ("years", annual)) if rows]
    yq = ya = False
    if len(quarters) < 4:
        rows = _rows_from_yf(sym, "q", fye)
        if rows:
            quarters, yq = rows, True
    if len(annual) < 2:
        rows = _rows_from_yf(sym, "a", fye)
        if rows:
            annual, ya = rows, True
    sec_q = "quarters" in from_sec and not yq and len(quarters) >= 4
    sec_a = "years" in from_sec and not ya and len(annual) >= 2
    note = []
    if sec_q and sec_a:
        pass
    elif sec_a:
        src += " (years) · Yahoo Finance (quarters)"
    else:
        src = "Yahoo Finance statements (four years, five quarters)"
        unit = yf_ccy
    unit = unit or yf_ccy
    # Yahoo's quarters beside the SEC's years: one currency, one share count.
    if sec_a and yq:
        if yf_ccy and unit and major(yf_ccy)[0] != unit and not _convert_rows(quarters, yf_ccy, unit):
            note.append(f"quarters left in {yf_ccy}: no exchange rate to {unit}")
        a_sh = next((r["shares"] for r in reversed(annual) if r.get("shares")), None)
        q_sh = next((r["shares"] for r in reversed(quarters) if r.get("shares")), None)
        if a_sh and q_sh:
            k = _snap(a_sh / q_sh)
            if abs(k - 1) > 0.12:
                _rebase(quarters, k)
                note.append(f"Yahoo's quarters were per quoted share; put on the filer's count ({nf_ratio(k)} to one)")
    # A foreign filer's newest 20-F can be missing from the SEC's data (TSMC's
    # stops at 2024): the years after it come from Yahoo, on the same basis.
    if sec_a and annual and (date.today() - _dt(annual[-1]["end"])).days > 450:
        extra = _safe(lambda: _rows_from_yf(sym, "a", fye)) or []
        late = [r for r in extra if (_dt(r["end"]) - _dt(annual[-1]["end"])).days > 300]
        if late and (not yf_ccy or major(yf_ccy)[0] == unit or _convert_rows(extra, yf_ccy, unit)):
            k = None
            for r in extra:
                twin = next((a_ for a_ in annual if abs((_dt(a_["end"]) - _dt(r["end"])).days) <= 10), None)
                if twin and twin.get("shares") and r.get("shares"):
                    k = _snap(twin["shares"] / r["shares"])
            if k and abs(k - 1) > 0.12:
                _rebase(late, k)
            for r in late:
                r["from_yahoo"] = True
            annual = annual + late
            note.append(f"{', '.join(r['label'] for r in late)} from Yahoo: not yet in the SEC's data")
    if not quarters and not annual:
        raise NotFound(f"no reported financials for {sym}")
    # Yahoo leaves a quarter's share count and EPS out now and then (Shell's
    # third quarter of 2025); with the net income there, the neighbours'
    # share count gives it back, marked as derived.
    for i, r in enumerate(quarters):
        if r.get("shares") is None:
            nb = [x.get("shares") for x in (quarters[i - 1] if i else {}, quarters[i + 1] if i + 1 < len(quarters) else {}) if x.get("shares")]
            if nb:
                r["shares"] = sum(nb) / len(nb)
        if r.get("eps") is None and r.get("net_income") is not None and r.get("shares"):
            r["eps"] = round(r["net_income"] / r["shares"], 4)
            r["derived"] = True
    if not sec_q:
        quarters, kept = _kept_quarters(sym, quarters)
        if kept:
            note.append(f"{kept} older quarters kept from earlier reads")
    quarters = quarters + _provisional(sym, quarters, unit, fye, ov)
    quarters, annual = _enrich(quarters, "q"), _enrich(annual, "a")
    _ratios(quarters, "q")
    _ratios(annual, "a")
    st = _safe(lambda: street(sym)) or {}
    _street_eps(quarters, annual, st, unit, ov)
    full = [q for q in quarters if not q.get("provisional")]
    ttm = _ttm(quarters, len(quarters) - 1) if quarters else None
    return {"symbol": sym, "source": src, "currency": unit,
            "quote_currency": ov.get("currency"),
            "fye": fye, "quarters": quarters[-16:], "annual": annual[-10:],
            "ttm": ttm, "investments": _investments(annual, full),
            "estimates": _estimate_columns(st, unit, quarters, annual, ov),
            "flags": _quality_flags(full, annual),
            "cik": ci["cik"] if ci else None, "filer_via": (ci or {}).get("via"),
            "note": "; ".join(x for x in [err] + note if x) or None}


def _kept_quarters(sym: str, quarters: list[dict]) -> tuple[list[dict], int]:
    """
    Every quarter Yahoo has shown, kept. Yahoo gives a company outside the
    SEC's quarterly filings its last five quarters, so each new one pushes
    the oldest out and most year-on-year growth cells stay empty; kept here,
    the history grows by one a quarter. Rows are stored as shown: already in
    the table's currency and share count.
    """
    keep = [q for q in quarters if not q.get("provisional")]
    with store.conn() as c:
        for q in keep:
            c.execute("INSERT INTO fin_q (sym, end_d, row, seen) VALUES (?, ?, ?, ?) "
                      "ON CONFLICT(sym, end_d) DO UPDATE SET row = excluded.row, seen = excluded.seen",
                      (sym, q["end"], json.dumps(q), date.today().isoformat()))
        old = [json.loads(r[0]) for r in c.execute(
            "SELECT row FROM fin_q WHERE sym = ? ORDER BY end_d", (sym,)).fetchall()]
    if not quarters:
        return quarters, 0
    first = _dt(quarters[0]["end"])
    older = [r for r in old if (first - _dt(r["end"])).days > 20]
    for r in older:
        r["kept"] = True
    return older + quarters, len(older)


def _ratios(rows: list[dict], freq: str) -> None:
    """
    The rows a balance sheet needs: what the debt is against a year's
    operating earnings, how many times the interest is covered, how many
    days of sales sit in inventory and in receivables, free cash flow after
    paying staff in shares, and for quarters the sequential growth and
    whether year-on-year growth is speeding up. Flows over the last four
    quarters for a quarter, the year itself for a year.
    """
    for i, r in enumerate(rows):
        if freq == "q":
            win = rows[i - 3:i + 1] if i >= 3 else []
            span_ok = bool(win) and 250 <= (_dt(win[-1]["end"]) - _dt(win[0]["end"])).days <= 300

            def T(k, win=win, span_ok=span_ok):
                return sum(w[k] for w in win) if span_ok and all(w.get(k) is not None for w in win) else None
        else:
            def T(k, r=r):
                return r.get(k)
        rev, gp = T("revenue"), T("gross_profit")
        cogs = T("cost_of_revenue")
        if cogs is None and rev is not None and gp is not None:
            cogs = rev - gp
        oi, dna, intr = T("op_income"), T("dna"), T("interest")
        ebitda = oi + dna if oi is not None and dna is not None else None
        nd = None
        if r.get("cash") is not None and (r.get("debt") is not None or r.get("debt_st") is not None):
            nd = (r.get("debt") or 0) + (r.get("debt_st") or 0) - (r.get("cash") or 0) - (r.get("st_inv") or 0)
        r["ebitda_12m"] = ebitda
        r["nd_ebitda"] = round(nd / ebitda, 2) if nd is not None and ebitda and ebitda > 0 else None
        r["int_cover"] = round(oi / intr, 1) if oi is not None and intr and intr > 0 else None
        r["inv_days"] = round(r["inventory"] / cogs * 365) if r.get("inventory") and cogs and cogs > 0 else None
        r["rec_days"] = round(r["receivables"] / rev * 365) if r.get("receivables") and rev and rev > 0 else None
        r["fcf_sbc"] = r["fcf"] - r["sbc"] if r.get("fcf") is not None and r.get("sbc") is not None else None
        if freq == "q" and i:
            pr = rows[i - 1]
            if 80 <= (_dt(r["end"]) - _dt(pr["end"])).days <= 100:
                r["revenue_qoq"] = _pct(r.get("revenue"), pr.get("revenue")) if pr.get("revenue") else None
                if r.get("revenue_yoy") is not None and pr.get("revenue_yoy") is not None:
                    r["rev_accel"] = round(r["revenue_yoy"] - pr["revenue_yoy"], 2)


def _street_eps(quarters: list[dict], annual: list[dict], st: dict, ccy: Optional[str], ov: dict) -> None:
    """
    The EPS analysts count beside the reported one. Microsoft's December 2025
    quarter reported $5.16 and +60% growth; the street counted $4.14, the
    gap a one-off gain. A quarter where the two differ by more than a tenth
    is flagged, in the table and in the warnings.
    """
    hist = [h for h in st.get("surprises") or [] if h.get("eps_act") is not None]
    if not hist or not quarters:
        return
    hccy = (st.get("currency") or {}).get("hist") or ccy
    ratio = units_ratio(next((q["shares"] for q in reversed(quarters) if q.get("shares")), None), ov)
    fx = FX(hccy, ccy)
    for q in quarters:
        rep = next((h for h in hist if 0 < (_dt(h["date"]) - _dt(q["end"])).days < 100), None)
        m = fx.total(q["end"]) if rep else None
        if rep and m:
            q["eps_street"] = round(rep["eps_act"] * m / ratio, 4)
            if q.get("eps") is not None and not q.get("provisional") and q["eps_street"]:
                gap = (q["eps"] / q["eps_street"] - 1) * 100 if q["eps_street"] > 0 else None
                q["eps_gap"] = round(gap, 1) if gap is not None else None
    for q in quarters:
        ago = next((x for x in quarters if x.get("eps_street") and 330 <= (_dt(q["end"]) - _dt(x["end"])).days <= 400), None)
        if q.get("eps_street") is not None and ago and ago["eps_street"] > 0:
            q["eps_street_yoy"] = _pct(q["eps_street"], ago["eps_street"])
    for a in annual:
        e = _dt(a["end"])
        inside = [q for q in quarters if 0 <= (e - _dt(q["end"])).days < 340]
        if len(inside) == 4 and all(q.get("eps_street") is not None for q in inside):
            a["eps_street"] = round(sum(q["eps_street"] for q in inside), 4)
    for prev, a in zip(annual, annual[1:]):
        if a.get("eps_street") is not None and (prev.get("eps_street") or 0) > 0:
            a["eps_street_yoy"] = _pct(a["eps_street"], prev["eps_street"])


def _estimate_columns(st: dict, ccy: Optional[str], quarters: list[dict], annual: list[dict],
                      ov: dict) -> Optional[dict]:
    """
    The forecast as columns after the last actual: the next two quarters and
    years from Yahoo, the years after from Zacks, put in the table's own
    currency and share count (TSMC's EPS forecasts are in dollars per
    American share, its table in Taiwan dollars per Taiwanese share). EPS on
    the analysts' basis, so it sits in the street row, not the reported one.
    """
    per = st.get("periods") or []
    if not per:
        return None
    sc = st.get("currency") or {}
    ratio = units_ratio(next((q["shares"] for q in reversed(quarters) if q.get("shares")), None), ov)
    out = {"q": [], "a": []}
    for p in per:
        end = p.get("end")
        if not end:
            continue
        kind = "q" if p["period"] in ("0q", "+1q") else "a"
        base = quarters if kind == "q" else annual
        if base and end <= base[-1]["end"]:
            continue
        e, r = p.get("eps") or {}, p.get("rev") or {}
        m_e = FX(p.get("eps_ccy") or sc.get("eps") or ccy, ccy).total()
        m_r = FX(p.get("rev_ccy") or sc.get("rev") or ccy, ccy).total()
        out[kind].append({
            "end": end, "label": (p.get("fiscal") or p.get("label") or "") + "E", "estimate": True,
            "eps_street": round(e["avg"] * m_e / ratio, 4) if e.get("avg") is not None and m_e else None,
            "revenue": r["avg"] * m_r if r.get("avg") is not None and m_r else None,
            "eps_street_yoy": e.get("growth"), "revenue_yoy": r.get("growth"), "n": e.get("n"),
            "source": "Yahoo consensus"})
    z = st.get("zacks") or {}
    if z.get("same_listing") and z.get("years"):
        last = max([x["end"] for x in out["a"]] + ([annual[-1]["end"]] if annual else []), default="")
        m_z = FX("USD", ccy).total()
        for y in z["years"]:
            if y.get("end") and y["end"] > last and y.get("eps") is not None and m_z:
                out["a"].append({"end": y["end"], "label": f"FY{y['end'][2:4]}E", "estimate": True, "zacks": True,
                                 "eps_street": round(y["eps"] * m_z / ratio, 4), "eps_street_yoy": y.get("growth"),
                                 "n": y.get("n"), "source": "Zacks via Nasdaq"})
    return out if out["q"] or out["a"] else None


def _quality_flags(quarters: list[dict], annual: list[dict]) -> list[dict]:
    """Warning signs in plain sentences, each with its rule here to be argued with."""
    out = []
    q = [r for r in quarters if r.get("revenue") is not None]
    t_now = _ttm(q, len(q) - 1) if len(q) >= 4 else None
    t_ago = _ttm(q, len(q) - 5) if len(q) >= 8 else None
    last = q[-1] if q else {}
    ago = next((r for r in q if 330 <= (_dt(last["end"]) - _dt(r["end"])).days <= 400), None) if last else None
    if t_now and t_now.get("net_income") and t_now["net_income"] > 0 and t_now.get("ocf") is not None:
        c = t_now["ocf"] / t_now["net_income"]
        if c < 0.8:
            out.append({"tone": "warn", "k": "accruals",
                        "text": f"Profit is not all turning into cash: operating cash flow {_bn(t_now['ocf'])} against net income "
                                f"{_bn(t_now['net_income'])} over the last four quarters ({c * 100:.0f}%)."})
    rev_g = _pct((t_now or {}).get("revenue"), (t_ago or {}).get("revenue"))
    # Only where it matters: Microsoft's inventory is five days of cost of
    # sales, and its growing half again means nothing.
    for k, what, days_k, floor in (("receivables", "Receivables", "rec_days", 20), ("inventory", "Inventory", "inv_days", 30)):
        if (last.get(days_k) or 0) < floor:
            continue
        if ago and last.get(k) and ago.get(k) and rev_g is not None:
            g = _pct(last[k], ago[k])
            if g is not None and g > rev_g + 15:
                out.append({"tone": "warn", "k": k,
                            "text": f"{what} {g:+.0f}% on a year earlier, revenue {rev_g:+.0f}%: "
                                    + ("customers are paying more slowly, or sales are being pulled forward."
                                       if k == "receivables" else "stock is building faster than sales.")})
    if ago and last.get("deferred_rev") and ago.get("deferred_rev") and rev_g is not None:
        g = _pct(last["deferred_rev"], ago["deferred_rev"])
        if g is not None and g < -5 and rev_g > 0:
            out.append({"tone": "warn", "k": "prepay",
                        "text": f"Customer prepayments {g:+.0f}% on a year earlier while revenue grew {rev_g:+.0f}%: "
                                "fewer orders paid in advance."})
        elif g is not None and g > rev_g + 15:
            out.append({"tone": "good", "k": "prepay",
                        "text": f"Customer prepayments {g:+.0f}% on a year earlier, ahead of revenue ({rev_g:+.0f}%): "
                                "demand paid for before it is delivered."})
    if t_now and t_now.get("sbc") and t_now.get("fcf") and t_now["fcf"] > 0:
        sh = t_now["sbc"] / t_now["fcf"] * 100
        if sh >= 20:
            out.append({"tone": "warn" if sh >= 40 else "info", "k": "sbc",
                        "text": f"Stock-based pay is {sh:.0f}% of free cash flow: the cash figure flatters what owners keep."})
    nd = last.get("nd_ebitda") if last else None
    if nd is not None and nd > 3:
        out.append({"tone": "warn", "k": "debt", "text": f"Net debt is {nd:.1f}× a year's operating earnings (EBITDA)."})
    ic = last.get("int_cover") if last else None
    if ic is not None and ic < 4:
        out.append({"tone": "warn", "k": "interest", "text": f"Operating profit covers the interest {ic:.1f}× over the last four quarters."})
    gaps = [r for r in quarters[-8:] if r.get("eps_gap") is not None and abs(r["eps_gap"]) > 10]
    if gaps:
        g = gaps[-1]
        out.append({"tone": "info", "k": "gaap",
                    "text": f"In {len(gaps)} of the last {min(8, len(quarters))} quarters the reported EPS differs from the analysts' "
                            f"by more than a tenth — most recently {g['label']}: {g['eps']:.2f} reported, {g['eps_street']:.2f} as "
                            f"analysts count it ({g['eps_gap']:+.0f}%). One-off gains or charges: the growth rates in the "
                            "reported row carry them."})
    return out


def fundamentals(sym: str) -> dict:
    return store.cache_through(f"rs:fund:{sym}", TTL["fundamentals"],
                               lambda: _load_fundamentals(sym))


# --------------------------------------------------------------------------
# the business: where the revenue comes from, and who pays it
# --------------------------------------------------------------------------

def _mix_member(m: dict, latest_q: Optional[str], latest_a: Optional[str]) -> Optional[dict]:
    q, a = m.get("q") or [], m.get("a") or []
    fresh_q = bool(q) and latest_q and abs((_dt(q[-1]["end"]) - _dt(latest_q)).days) <= 20
    fresh_a = bool(a) and latest_a and abs((_dt(a[-1]["end"]) - _dt(latest_a)).days) <= 20
    if not fresh_q and not fresh_a:
        return None                     # a renamed or discontinued line
    out = {"label": m["label"], "member": m["member"]}
    if fresh_q:
        last = q[-1]
        ago = next((x for x in q if 330 <= (_dt(last["end"]) - _dt(x["end"])).days <= 400), None)
        out.update({"q_end": last["end"], "q": last["v"], "q_yoy": _pct(last["v"], (ago or {}).get("v")),
                    "q_derived": last.get("derived", False),
                    "q_series": [{"end": x["end"], "v": x["v"]} for x in q[-9:]]})
        # trailing four quarters, when four consecutive ones exist
        if len(q) >= 4 and 250 <= (_dt(q[-1]["end"]) - _dt(q[-4]["end"])).days <= 300:
            out["ttm"] = sum(x["v"] for x in q[-4:])
            if len(q) >= 8 and 250 <= (_dt(q[-5]["end"]) - _dt(q[-8]["end"])).days <= 300:
                out["ttm_yoy"] = _pct(out["ttm"], sum(x["v"] for x in q[-8:-4]))
        oq = m.get("oi_q") or []
        if oq and abs((_dt(oq[-1]["end"]) - _dt(last["end"])).days) <= 20:
            out["q_oi"] = oq[-1]["v"]
            out["q_margin"] = round(oq[-1]["v"] / last["v"] * 100, 1) if last["v"] else None
    if fresh_a:
        last = a[-1]
        prev = a[-2] if len(a) > 1 else None
        out.update({"a_end": last["end"], "a": last["v"], "a_yoy": _pct(last["v"], (prev or {}).get("v")),
                    "a_series": [{"end": x["end"], "v": x["v"]} for x in a[-5:]]})
        oa = m.get("oi_a") or []
        if oa and abs((_dt(oa[-1]["end"]) - _dt(last["end"])).days) <= 20:
            out["a_oi"] = oa[-1]["v"]
            out["a_margin"] = round(oa[-1]["v"] / last["v"] * 100, 1) if last["v"] else None
    return out


GROUP_LABEL = {"segment": "Business segments", "product": "Product lines",
               "geography": "Geography", "other": "Other disclosed lines",
               "customer": "Customer groups", "type": "Products vs services"}


def _growth_of(m: dict) -> Optional[float]:
    for k in ("ttm_yoy", "q_yoy", "a_yoy"):
        if m.get(k) is not None:
            return m[k]
    return None


def _load_business(sym: str) -> dict:
    ci = filer_cik(sym, _safe(lambda: overview(sym)))
    if not ci:
        return {"symbol": sym, "available": False,
                "why": f"Segment, product and customer data is read from SEC filings, and {sym} "
                       "does not file 10-Ks and 10-Qs with the SEC. The fundamentals panel "
                       "still has its consolidated figures."}
    fye = sec.submissions(ci["cik"]).get("fye")
    mix = sec.business(ci["cik"])
    if mix.get("error"):
        return {"symbol": sym, "available": False, "why": mix["error"]}
    co_q, co_a = {}, {}
    fund = _safe(lambda: fundamentals(sym)) or {}
    for r in fund.get("quarters") or []:
        co_q[r["end"]] = r.get("revenue")
    for r in fund.get("annual") or []:
        co_a[r["end"]] = r.get("revenue")
    groups = []
    for kind in ("segment", "product", "other", "geography", "customer", "type"):
        members = mix.get("groups", {}).get(kind) or []
        if not members:
            continue
        latest_q = max((m["q"][-1]["end"] for m in members if m.get("q")), default=None)
        latest_a = max((m["a"][-1]["end"] for m in members if m.get("a")), default=None)
        rows = [x for x in (_mix_member(m, latest_q, latest_a) for m in members) if x]
        if not rows:
            continue
        tot_q = sum(r.get("q") or 0 for r in rows if r.get("q_end") == latest_q)
        tot_a = sum(r.get("a") or 0 for r in rows if r.get("a_end") == latest_a)
        for r in rows:
            if r.get("q_end") == latest_q and tot_q:
                r["q_share"] = round((r.get("q") or 0) / tot_q * 100, 1)
            if r.get("a_end") == latest_a and tot_a:
                r["a_share"] = round((r.get("a") or 0) / tot_a * 100, 1)
        for r in rows:
            # Share of the whole company, which is what a line like "Microsoft
            # Cloud" (the only member of its axis) can meaningfully be given.
            cq = next((v for e, v in co_q.items() if r.get("q_end") and v
                       and abs((_dt(e) - _dt(r["q_end"])).days) <= 12), None)
            ca = next((v for e, v in co_a.items() if r.get("a_end") and v
                       and abs((_dt(e) - _dt(r["a_end"])).days) <= 12), None)
            r["q_pct_co"] = round(r["q"] / cq * 100, 1) if cq and r.get("q") else None
            r["a_pct_co"] = round(r["a"] / ca * 100, 1) if ca and r.get("a") else None
        rows.sort(key=lambda r: -((r.get("ttm") or r.get("a") or r.get("q") or 0)))
        groups.append({"kind": kind, "label": GROUP_LABEL.get(kind, kind),
                       "axis": members[0].get("axis"),
                       "latest_q": latest_q,
                       "latest_q_label": fiscal_label(latest_q, fye) if latest_q else None,
                       "latest_a": latest_a,
                       "latest_a_label": fiscal_label(latest_a, fye, True) if latest_a else None,
                       "members": rows})

    if not groups and not mix.get("rpo") and not mix.get("concentration"):
        return {"symbol": sym, "available": False,
                "why": "The company's SEC filings carry no revenue breakdown by segment, product "
                       "or region in machine-readable form (typical of 20-F annual reports)."}

    # Backlog by label, with growth.
    rpo = {}
    for r in mix.get("rpo") or []:
        rpo.setdefault(r["label"], []).append(r)
    backlog = []
    for lab, pts in rpo.items():
        pts.sort(key=lambda r: r["end"])
        last = pts[-1]
        ago = next((p for p in pts if 330 <= (_dt(last["end"]) - _dt(p["end"])).days <= 400), None)
        backlog.append({"label": lab, "end": last["end"], "v": last["v"],
                        "yoy": _pct(last["v"], (ago or {}).get("v")),
                        "series": [{"end": p["end"], "v": p["v"]} for p in pts[-8:]]})
    backlog.sort(key=lambda r: (r["label"] != "Total", -r["v"]))

    conc = []
    for c in mix.get("concentration") or []:
        who = next((lab for ax, lab in c["dims"] if "Customer" in ax), None)
        basis = next((lab for ax, lab in c["dims"] if "Benchmark" in ax), None)
        conc.append({"end": c["end"], "pct": round(c["pct"] * 100, 1) if c["pct"] <= 1 else c["pct"],
                     "who": who or "Undisclosed customer", "basis": basis or "revenue"})

    engine = None
    cands = [m for g in groups if g["kind"] in ("segment", "product", "other")
             for m in g["members"] if _growth_of(m) is not None
             and (m.get("q_share") or m.get("a_share") or 0) >= 5]
    if cands:
        fast = max(cands, key=_growth_of)
        slow = min(cands, key=_growth_of)
        engine = {"fastest": {"label": fast["label"], "growth": _growth_of(fast)},
                  "slowest": {"label": slow["label"], "growth": _growth_of(slow)}}
    return {"symbol": sym, "available": True, "source": mix.get("source"),
            "filings": mix.get("filings"), "groups": groups, "backlog": backlog,
            "deferred": (mix.get("deferred") or [])[-12:],
            "concentration": conc, "engine": engine}


def business(sym: str) -> dict:
    return store.cache_through(f"rs:biz:{sym}", 12 * 3600, lambda: _load_business(sym))


# --------------------------------------------------------------------------
# valuation in context: against the company's own history
# --------------------------------------------------------------------------

def _split_factor(splits: list, filed: str) -> float:
    """How many of today's shares one share was, when a number was filed."""
    f = 1.0
    for d, ratio in splits:
        if filed and d > filed and ratio and ratio > 0:
            f *= ratio
    return f


def _ttm_points(fund: dict, splits: list) -> list[dict]:
    """TTM fundamentals, each stamped with the date the market could first know it."""
    q = fund.get("quarters") or []
    pts = []
    for i in range(3, len(q)):
        t = _ttm(q, i)
        if not t:
            continue
        win = q[i - 3:i + 1]
        # The filing date is when the 10-Q arrived. The earnings release came
        # a day or two sooner, never later — so this errs toward old news
        # rather than toward knowing the future.
        known = max((w.get("filed") or str(_dt(w["end"]) + timedelta(days=40))) for w in win)
        eps = t.get("eps")
        sh = q[i].get("shares")
        pts.append({
            "end": t["end"], "known": known,
            "eps": eps, "shares": sh,
            "revenue": t.get("revenue"), "fcf": t.get("fcf"), "op_income": t.get("op_income"),
            "net_debt": ((t.get("debt") or 0) + (t.get("debt_st") or 0)
                         - (t.get("cash") or 0) - (t.get("st_inv") or 0)),
        })
    pts.sort(key=lambda p: p["known"])
    return _carry(pts)


def _carry(pts: list[dict]) -> list[dict]:
    """
    A figure missing from a point stays what the last point had. A quarter
    known only from its earnings release has revenue and profit but no cash
    flow yet; Yahoo leaves a quarter's EPS out now and then (Shell's third
    quarter of 2025), which would blank every P/E for a year.
    """
    for prev, cur in zip(pts, pts[1:]):
        for k in ("fcf", "op_income", "revenue", "eps", "shares"):
            if cur.get(k) is None and prev.get(k) is not None:
                cur[k] = prev[k]
    return pts


def _annual_points(annual: list[dict]) -> list[dict]:
    """
    One point a year from the annual accounts, known from the day they were
    filed. A foreign filer sends the SEC no quarters, so for ASML this is the
    only history before Yahoo's last five quarters.
    """
    out = []
    for r in annual:
        if r.get("revenue") is None and r.get("eps") is None:
            continue
        # ASML's 2016-2018 accounts first appear in machine-readable form in
        # the 2019 filing, as comparatives. They were public long before: a
        # filing more than a year late is the tagging, not the news.
        known = r.get("filed")
        if not known or (_dt(known) - _dt(r["end"])).days > 400:
            known = str(_dt(r["end"]) + timedelta(days=75))
        out.append({"end": r["end"], "known": known,
                    "eps": r.get("eps"), "shares": r.get("shares"), "revenue": r.get("revenue"),
                    "fcf": r.get("fcf"), "op_income": r.get("op_income"), "annual": True,
                    "net_debt": ((r.get("debt") or 0) + (r.get("debt_st") or 0)
                                 - (r.get("cash") or 0) - (r.get("st_inv") or 0))})
    return sorted(out, key=lambda p: p["known"])


def _history_quarters(sym: str, fund: dict) -> list[dict]:
    """Every reported quarter (not just the sixteen the table shows), for the bands."""
    if fund.get("cik"):
        try:
            ser = sec.facts(fund["cik"]).get("series") or {}
            splits = (_safe(lambda: daily(sym)) or {}).get("splits") or []
            rows = _enrich(_rows_from_sec(ser, fund.get("fye"), "q", 70, splits), "q")
            if rows:
                prov = [q for q in fund.get("quarters") or []
                        if q.get("provisional") and q["end"] > rows[-1]["end"]]
                return rows + prov
        except Exception:
            pass
    return fund.get("quarters") or []


def _dist(vals: list, cur: Optional[float]) -> Optional[dict]:
    v = sorted(x for x in vals if x is not None and x > 0)
    if len(v) < 20 or cur is None:
        return None

    def q(p):
        k = (len(v) - 1) * p
        lo = int(k)
        return v[lo] + (v[min(lo + 1, len(v) - 1)] - v[lo]) * (k - lo)
    rank = bisect.bisect_left(v, cur) / len(v) * 100
    return {"min": round(v[0], 2), "p10": round(q(.1), 2), "p25": round(q(.25), 2),
            "median": round(q(.5), 2), "p75": round(q(.75), 2), "p90": round(q(.9), 2),
            "max": round(v[-1], 2), "current": round(cur, 2), "pct_rank": round(rank, 0),
            "vs_median": round((cur / q(.5) - 1) * 100, 1), "n": len(v)}


def _treasury_10y() -> Optional[float]:
    try:
        for r in (store.cache_get("market") or {}).get("rows") or []:
            if r.get("k") == "US10Y" and r.get("last") is not None:
                return float(r["last"])
    except Exception:
        pass
    return None


def implied_growth(fcf: float, ev: float, r: float = 0.09, g_term: float = 0.03,
                   years: int = 10) -> Optional[float]:
    """The ten-year FCF growth rate at which a DCF equals today's enterprise value."""
    if not fcf or fcf <= 0 or not ev or ev <= 0 or r <= g_term:
        return None

    def value(g):
        pv, f = 0.0, fcf
        for t in range(1, years + 1):
            f *= 1 + g
            pv += f / (1 + r) ** t
        return pv + f * (1 + g_term) / (r - g_term) / (1 + r) ** years
    lo, hi = -0.5, 1.0
    if value(hi) < ev or value(lo) > ev:
        return None
    for _ in range(80):
        mid = (lo + hi) / 2
        if value(mid) < ev:
            lo = mid
        else:
            hi = mid
    return round((lo + hi) / 2 * 100, 2)


VAL_KEYS = ("pe", "ps", "pfcf", "ev_ebit", "ev_sales", "div_yield")


def _load_valuation(sym: str) -> dict:
    ov = _safe(lambda: overview(sym)) or {}
    d = daily(sym)
    bars, splits, divs = d["bars"], d.get("splits") or [], d.get("divs") or []
    fund = _safe(lambda: fundamentals(sym)) or {}
    ccy = ov.get("currency")
    rccy = fund.get("currency") or ov.get("fin_currency") or ccy
    qu = major(ccy)[1]
    # A price in dollars against figures in euros: each week at its own rate.
    fx = FX(rccy, ccy)
    pts = _ttm_points({"quarters": _history_quarters(sym, fund)}, splits) if fund.get("quarters") else []
    first = pts[0]["known"] if pts else "9999"
    years = [p for p in _annual_points(fund.get("annual") or []) if p["known"] < first]
    pts = _carry(sorted(years + pts, key=lambda p: p["known"])) if fx.ok else []
    last_sh = next((p["shares"] for p in reversed(pts) if p.get("shares")), None)
    ratio = units_ratio(last_sh, ov) if pts else 1.0

    series = {"d": [], "c": [], **{k: [] for k in VAL_KEYS}}
    cut = (date.today() - timedelta(days=3653)).isoformat()
    div_days = [x[0] for x in divs]
    known = [p["known"] for p in pts]
    for i, b in enumerate(bars):
        if b[0] < cut or (i % 5 and i != len(bars) - 1):
            continue
        day, close = b[0], b[4]
        k = bisect.bisect_right(known, day) - 1
        p = pts[k] if k >= 0 else None
        lo = bisect.bisect_left(div_days, str(_dt(day) - timedelta(days=365)))
        hi = bisect.bisect_right(div_days, day)
        dps = sum(x[1] for x in divs[lo:hi])
        m_t, m_s = (fx.total(day), fx.per_share(day)) if p else (None, None)
        mcap = close * qu * p["shares"] / ratio if p and p.get("shares") and m_t else None
        ev = mcap + p["net_debt"] * m_t if mcap is not None else None
        series["d"].append(day)
        series["c"].append(close)
        series["pe"].append(round(close / (p["eps"] * ratio * m_s), 2)
                            if p and p.get("eps") and p["eps"] > 0 and m_s else None)
        series["ps"].append(round(mcap / (p["revenue"] * m_t), 2) if mcap and p.get("revenue") else None)
        series["pfcf"].append(round(mcap / (p["fcf"] * m_t), 2) if mcap and p.get("fcf") and p["fcf"] > 0 else None)
        series["ev_ebit"].append(round(ev / (p["op_income"] * m_t), 2)
                                 if ev and p.get("op_income") and p["op_income"] > 0 else None)
        series["ev_sales"].append(round(ev / (p["revenue"] * m_t), 2) if ev and p.get("revenue") else None)
        series["div_yield"].append(round(dps / close * 100, 3) if dps else None)

    stats = {}
    if pts:
        cut5 = (date.today() - timedelta(days=1826)).isoformat()
        for k in VAL_KEYS:
            vals = series[k]
            cur = vals[-1] if vals else None
            v5 = [v for dd, v in zip(series["d"], vals) if dd >= cut5]
            stats[k] = {"5y": _dist(v5, cur), "10y": _dist(vals, cur)}

    st = ov.get("stats") or {}
    last = pts[-1] if pts else None
    m_now = fx.total() if last else None
    price = (ov.get("quote") or {}).get("price") or (bars[-1][4] if bars else None)
    units = last["shares"] / ratio if last and last.get("shares") else None
    mcap = price * qu * units if units and price else st.get("market_cap")
    # the overview's cash and debt are already in the price's currency
    net_debt = last["net_debt"] * m_now if last and m_now else ((st.get("debt") or 0) - (st.get("cash") or 0))
    fcf = last["fcf"] * m_now if last and last.get("fcf") is not None and m_now else st.get("fcf")
    ev = mcap + net_debt if mcap is not None else None
    strt = _safe(lambda: street(sym)) or {}
    nfy = next((p for p in strt.get("periods", []) if p["period"] == "+1y"), {})
    cfy = next((p for p in strt.get("periods", []) if p["period"] == "0y"), {})
    # Yahoo's forecasts are per quoted share, in a currency of their own
    # (TSMC's in dollars, ASML's in euros); only the currency needs crossing.
    e_fx = FX((strt.get("currency") or {}).get("eps") or rccy, ccy).per_share()
    fwd_eps = (nfy.get("eps") or {}).get("avg")
    cur_eps = (cfy.get("eps") or {}).get("avg")
    fwd_q = fwd_eps * e_fx if fwd_eps and e_fx else None
    cur_q = cur_eps * e_fx if cur_eps and e_fx else None
    pe_now = series["pe"][-1] if pts and series["pe"] else st.get("pe_ttm")
    t10 = _treasury_10y()
    ey = round(100 / pe_now, 2) if pe_now else None
    g_next = (nfy.get("eps") or {}).get("growth")
    src = []
    if pts:
        src.append("price ÷ trailing-twelve-month figures from "
                   + ("SEC filings" if fund.get("cik") and not str(fund.get("source", "")).endswith("(quarters)")
                      else "the company's statements")
                   + ", each used only from the date it was filed")
        if years:
            src.append((f"before {first[:4]}, " if first != "9999" else "")
                       + "the annual accounts, one step a year")
        if not fx.same:
            src.append(f"{fx.f} figures in {fx.t} at each week's rate")
        if ratio != 1:
            src.append(f"one quoted share is {nf_ratio(ratio)} of the company's")
    else:
        src.append("current multiples from Yahoo — no filing history to build a range from"
                   + (f" (no {fx.f}→{fx.t} rate)" if not fx.ok else ""))
    return {
        "symbol": sym, "currency": ccy, "reported_currency": rccy, "ratio": ratio,
        "source": "; ".join(src),
        "series": series if pts else None, "stats": stats,
        "current": {
            "price": price, "market_cap": mcap, "ev": ev, "net_debt": net_debt,
            "pe_ttm": pe_now, "pe_fwd": st.get("pe_fwd"),
            "pe_next_fy": round(price / fwd_q, 2) if price and fwd_q and fwd_q > 0 else None,
            "pe_cur_fy": round(price / cur_q, 2) if price and cur_q and cur_q > 0 else None,
            "peg": round((price / fwd_q) / g_next, 2)
            if price and fwd_q and fwd_q > 0 and g_next and g_next > 0 else st.get("peg"),
            "ev_ebitda": round(ev / st["ebitda"], 2) if ev and st.get("ebitda") and st["ebitda"] > 0
            else st.get("ev_ebitda"),
            "ps": series["ps"][-1] if pts and series["ps"] else st.get("ps"),
            "pb": st.get("pb"), "fcf_ttm": fcf,
            "fcf_yield": round(fcf / mcap * 100, 2) if fcf and mcap else st.get("fcf_yield"),
            "earnings_yield": ey, "treasury_10y": t10,
            "spread_vs_10y": round(ey - t10, 2) if ey is not None and t10 is not None else None,
            "div_yield": st.get("div_yield"),
            "implied_growth": implied_growth(fcf, ev) if fcf and ev else None,
            "rev_growth_next_fy": (nfy.get("rev") or {}).get("growth"),
            "eps_growth_next_fy": g_next,
            "shares": units or st.get("shares_out"), "unit": qu,
        },
    }


def valuation(sym: str) -> dict:
    return store.cache_through(f"rs:val:{sym}", TTL["valuation"], lambda: _load_valuation(sym))


# --------------------------------------------------------------------------
# Finnhub, when a key is set
# --------------------------------------------------------------------------

def _finnhub(path: str, **params):
    key = newsmod.api_key()
    if not key:
        return None
    import urllib.parse
    params["token"] = key
    raw = net.get(f"https://finnhub.io/api/v1/{path}?" + urllib.parse.urlencode(params),
                  {"Accept": "application/json"}, timeout=15)
    return json.loads(raw.decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# peers
# --------------------------------------------------------------------------

def _peer_info(sym: str) -> dict:
    def load():
        ov = _load_overview(sym)
        s, q, a = ov["stats"], ov["quote"], ov["analyst"]
        return {"symbol": sym, "name": ov["short"] or ov["name"], "currency": ov.get("currency"),
                "price": q.get("price"), "change_pct": q.get("change_pct"),
                "market_cap": s.get("market_cap"), "pe_ttm": s.get("pe_ttm"), "pe_fwd": s.get("pe_fwd"),
                "peg": s.get("peg"), "ev_ebitda": s.get("ev_ebitda"), "ps": s.get("ps"),
                "rev_growth": s.get("rev_growth"), "gross_margin": s.get("gross_margin"),
                "op_margin": s.get("op_margin"), "net_margin": s.get("net_margin"),
                "roe": s.get("roe"), "fcf_yield": s.get("fcf_yield"), "div_yield": s.get("div_yield"),
                "beta": s.get("beta"),
                "upside": _pct(a.get("target_mean"), q.get("price")),
                "rating": a.get("rating"), "industry": ov.get("industry")}
    return store.cache_through(f"rs:peer:{sym}", TTL["peerinfo"], load)


def peer_list(sym: str) -> dict:
    """Your own list if you saved one; otherwise Yahoo's and Finnhub's, merged."""
    saved = store.get_setting(f"peers:{sym}")
    if saved:
        try:
            lst = [s for s in json.loads(saved) if s and s != sym]
            if lst:
                return {"peers": lst[:12], "source": "your list"}
        except Exception:
            pass
    found, srcs = [], []
    try:
        raw = net.get(f"https://query2.finance.yahoo.com/v6/finance/recommendationsbysymbol/{sym}")
        res = (json.loads(raw).get("finance") or {}).get("result") or []
        ys = [r["symbol"] for r in (res[0].get("recommendedSymbols") or [])] if res else []
        if ys:
            found += ys
            srcs.append("Yahoo")
    except Exception:
        pass
    try:
        fh = _finnhub("stock/peers", symbol=sym) or []
        fh = [s for s in fh if s != sym and "." not in s][:6]
        if fh:
            found += fh
            srcs.append("Finnhub")
    except Exception:
        pass
    out, seen = [], set()
    for s in found:
        # GOOG and GOOGL are one company; keep whichever arrived first.
        base = re.sub(r"(GOOGL?|BRK-?[AB])", lambda m: m.group(0)[:4], s)
        if s == sym or s in seen or base in seen:
            continue
        seen |= {s, base}
        out.append(s)
    return {"peers": out[:9], "source": " + ".join(srcs) or "none found"}


def _industry_index() -> dict:
    """
    Industry -> the watchlist universe's companies in it, one listing each.
    The universe has the sector; the industry comes from the estimates the
    nightly scan already read. Nothing is fetched here.
    """
    def load():
        u = store.cache_get("wl:universe") or {}
        idx = {}
        for r in u.get("rows") or []:
            info = ((store.cache_get(f"wl:est:{r['sym']}") or {}).get("info")) or {}
            ind = info.get("industry")
            if ind:
                idx.setdefault(ind, []).append({"sym": r["sym"], "name": r.get("name"), "mcap_usd": r.get("mcap_usd"),
                                                "also": r.get("also") or []})
        return {"idx": idx}
    try:
        return store.cache_through("rs:industries", 12 * 3600, load).get("idx") or {}
    except Exception:
        return {}


def _same_company(a: dict, b_sym: str, b_name: str) -> bool:
    from .earndocs import norm_name
    return b_sym == a["sym"] or b_sym in (a.get("also") or []) or (
        bool(b_name) and norm_name(a.get("name") or "") == norm_name(b_name))


def _usd_rate(ccy: Optional[str]) -> Optional[float]:
    """US dollars per unit of the currency a market value is in (pounds, for a price in pence)."""
    from .watch import usd_per
    c = major(ccy)[0]
    return (_safe(usd_per) or {}).get(c) if c else None


def _load_peers(sym: str) -> dict:
    pl = peer_list(sym)
    syms = [sym] + pl["peers"]
    with ThreadPoolExecutor(max_workers=8) as ex:
        infos = list(ex.map(lambda s: _safe(lambda: _peer_info(s)), syms))
    rows = [r for r in infos if r]
    # Yahoo's list is who else its readers look at: CrowdStrike and Arm for
    # ASML. Unless the list is your own, a peer is in the same industry, and
    # the watchlist's universe fills the gaps with the closest in size.
    me0 = next((r for r in rows if r["symbol"] == sym), None)
    filled = []
    if me0 and me0.get("industry") and pl["source"] != "your list":
        ind, name = me0["industry"], me0.get("name") or ""
        same = [r for r in rows if r["symbol"] != sym and r.get("industry") == ind]
        if len(same) < 5:
            rate = _usd_rate(me0.get("currency"))
            my_usd = (me0.get("market_cap") or 0) * (rate or 0) or None
            have = {r["symbol"] for r in rows}
            cands = [c for c in _industry_index().get(ind, [])
                     if c["sym"] not in have and not _same_company(c, sym, name) and c.get("mcap_usd")
                     and not any(_same_company(c, r["symbol"], r.get("name") or "") for r in rows)]
            if my_usd:
                cands.sort(key=lambda c: abs(math.log(c["mcap_usd"] / my_usd)))
            else:
                cands.sort(key=lambda c: -c["mcap_usd"])
            add = [c["sym"] for c in cands[:6 - len(same)]]
            if add:
                with ThreadPoolExecutor(max_workers=4) as ex:
                    more = [r for r in ex.map(lambda s: _safe(lambda: _peer_info(s)), add) if r]
                rows += more
                filled = [r["symbol"] for r in more]
                syms += filled
                same += [r for r in more if r.get("industry") == ind]
        if len(same) >= 3:
            rows = [r for r in rows if r["symbol"] == sym or r.get("industry") == ind]
            syms = [x for x in syms if any(r["symbol"] == x for r in rows)]
    # Market values in the page's currency: ASM International is in euros.
    if me0:
        to = _usd_rate(me0.get("currency"))
        for r in rows:
            fr = _usd_rate(r.get("currency"))
            if r.get("market_cap") and fr and to and major(r.get("currency"))[0] != major(me0.get("currency"))[0]:
                r["market_cap_local"] = r["market_cap"]
                r["market_cap"] = r["market_cap"] * fr / to

    # Price performance and co-movement from one batched download.
    perf = {}
    try:
        data = _yf().download(syms, period="13mo", interval="1d", auto_adjust=True,
                              progress=False, group_by="ticker", threads=True, timeout=20)
        closes = {}
        for s in syms:
            try:
                col = (data[s]["Close"] if len(syms) > 1 else data["Close"]).dropna()
                closes[s] = {str(i.date()): float(v) for i, v in col.items()}
            except Exception:
                continue
        base = closes.get(sym) or {}
        bdays = sorted(base)
        brets = {d: base[d] / base[p] - 1 for p, d in zip(bdays, bdays[1:])}
        today = date.today()
        for s, cl in closes.items():
            days = sorted(cl)
            if len(days) < 30:
                continue
            last = cl[days[-1]]

            def back(n_days):
                cut = str(today - timedelta(days=n_days))
                i = bisect.bisect_right(days, cut) - 1
                return cl[days[max(i, 0)]]
            ytd_i = bisect.bisect_left(days, f"{today.year}-01-01") - 1
            rets = {d: cl[d] / cl[p] - 1 for p, d in zip(days, days[1:])}
            common = [d for d in brets if d in rets][-250:]
            corr = None
            if len(common) > 60 and s != sym:
                import numpy as np
                corr = round(float(np.corrcoef([brets[d] for d in common],
                                               [rets[d] for d in common])[0, 1]), 2)
            perf[s] = {"r1m": _pct(last, back(30)), "r3m": _pct(last, back(91)),
                       "rytd": _pct(last, cl[days[ytd_i]]) if ytd_i >= 0 else None,
                       "r1y": _pct(last, back(365)), "corr": corr,
                       "spark": [round(cl[d], 2) for d in days[-252::5]]}
    except Exception:
        pass
    for r in rows:
        r.update(perf.get(r["symbol"], {}))
        r["self"] = r["symbol"] == sym

    keys = ("market_cap", "pe_ttm", "pe_fwd", "peg", "ev_ebitda", "ps", "rev_growth",
            "gross_margin", "op_margin", "net_margin", "roe", "fcf_yield", "div_yield",
            "r1m", "r3m", "rytd", "r1y", "upside", "beta")
    others = [r for r in rows if not r["self"]]
    med = {k: _median([r.get(k) for r in others]) for k in keys}
    me = next((r for r in rows if r["self"]), {})
    rank = {}
    for k in keys:
        vals = [r.get(k) for r in rows if r.get(k) is not None]
        if me.get(k) is not None and len(vals) > 1:
            rank[k] = {"pos": sorted(vals, reverse=True).index(me[k]) + 1, "of": len(vals)}
    return {"symbol": sym, "rows": rows, "median": med, "rank": rank,
            "source": pl["source"] + (" · same industry, filled from the watchlist's universe" if filled else ""),
            "filled": filled, "custom": pl["source"] == "your list"}


def peers(sym: str) -> dict:
    pl = peer_list(sym)
    tag = ",".join(pl["peers"])
    import hashlib
    key = hashlib.md5(tag.encode()).hexdigest()[:8]
    return store.cache_through(f"rs:peers:{sym}:{key}", TTL["peers"],
                               lambda: _load_peers(sym))


def set_peers(sym: str, lst: list[str]) -> dict:
    clean = []
    for s in lst or []:
        s = re.sub(r"[^A-Z0-9.\-^=]", "", (s or "").upper())[:14]
        if s and s != sym and s not in clean:
            clean.append(s)
    if clean:
        store.set_setting(f"peers:{sym}", json.dumps(clean[:12]))
    else:
        store.set_setting(f"peers:{sym}", "")
    return peer_list(sym)


# --------------------------------------------------------------------------
# news: categorised, scored, with the numbers pulled out
# --------------------------------------------------------------------------

CATEGORIES = [
    ("analyst", re.compile(r"\b(upgrade[sd]?|downgrade[sd]?|price target|pt (raised|cut)|"
                           r"initiat\w+ (coverage|at)|reiterat\w+|top pick|"
                           r"(buy|sell|hold|overweight|underweight|outperform|underperform) rating)\b",
                           re.I)),
    ("earnings", re.compile(r"\b(earnings|results|quarter(ly)?|q[1-4]\b|fy\d{2}|guidance|outlook|"
                            r"forecast|eps|revenue (beat|miss|rose|fell|grew)|beats?|miss(es|ed)?|"
                            r"profit (rose|fell|jumps?|slumps?))\b", re.I)),
    ("deal", re.compile(r"\b(acqui\w+|merger|merge|buyout|takeover|to buy|to acquire|deal|"
                        r"partner(s|ship|ing)?|agreement|contract|stake|joint venture|alliance|"
                        r"licens\w+|invest(s|ment|ing)|pledg\w+|commit(s|ted|ment)? \$|signs|"
                        r"wins? .* (order|contract)|"
                        r"divest\w*|spin[- ]?off|sells? (its|unit|stake|business))\b", re.I)),
    ("analyst2", re.compile(r"\b(rating|overweight|underweight|outperform|underperform|analysts?)\b",
                            re.I)),
    ("legal", re.compile(r"\b(lawsuit|sues?|sued|antitrust|probe|investigat\w+|regulat\w+|ftc|"
                         r"doj|justice department|sec (charges|probe)|european commission|"
                         r"fined?|penalt\w+|court|ruling|judge|settle\w*|ban(s|ned)?|"
                         r"tariff|export (controls?|curbs?))\b", re.I)),
    ("management", re.compile(r"\b(ceo|cfo|coo|chief (executive|financial|operating)|executive|"
                              r"resign\w*|appoint\w*|steps? down|succession|names? .* (chief|head|"
                              r"president)|board of directors|layoffs?|job cuts|restructur\w+)\b",
                              re.I)),
    ("capital", re.compile(r"\b(buyback|repurchase|dividend|bond (sale|offering)|debt (offering|"
                           r"sale)|notes offering|stock split|share sale|secondary offering|"
                           r"capital return|raises? \$)\b", re.I)),
    ("product", re.compile(r"\b(launch\w*|unveil\w*|releas\w+|introduc\w+|rolls? out|debut\w*|"
                           r"new (model|product|feature|chip|service|device|version)|ai (model|"
                           r"agent|assistant)|preview|announce[sd]?)\b", re.I)),
]
CAT_WEIGHT = {"earnings": 3.0, "deal": 2.5, "legal": 2.2, "management": 2.0, "analyst": 1.6,
              "capital": 1.6, "product": 1.2, "market": 0.6, "other": 0.5, "opinion": 0.2}

# Listicles, "should you buy" pieces and market-research press releases. They
# mention the company and quote big numbers, which is exactly what the impact
# score rewards — so they are named and kept out of the way.
OPINION = re.compile(r"\b(\d+ (unstoppable|top|best|magnificent|brilliant|no-brainer)|stocks? to buy|"
                     r"buy this|sell this|: (buy|sell|hold|strong buy)\b|"
                     r"should you (buy|sell)|buy (now|before|and hold)|millionaire|could make you|"
                     r"is it (time|too late)|price prediction|better buy|vs\.? .* which|"
                     r"market (to reach|size|report|share analysis)|cagr|industry analysis|"
                     r"forecast (to|till|by|through) 20\d\d|here'?s why|what you need to know|"
                     r"motley fool|zacks rank|dividend aristocrat)\b", re.I)

MONEY = re.compile(r"(?:US\$|\$|€|£|¥)\s?\d[\d,.]*\s?(?:trillion|billion|million|bn|mn|tn|[bmk])?\b"
                   r"|\b\d[\d,.]*\s?(?:trillion|billion|million)\s(?:dollars|euros|usd|eur)\b", re.I)
PCTNUM = re.compile(r"[+\-−]?\d+(?:\.\d+)?\s?%")

POS = re.compile(r"\b(beat|beats|tops?|surg\w+|soar\w*|jump\w*|rall\w+|record|upgrade\w*|rais\w+|"
                 r"strong\w*|growth|grow\w*|gain\w*|wins?|bullish|outperform\w*|expand\w*|boost\w*|"
                 r"accelerat\w+|climb\w*|rises?|rising|higher|optimis\w+|upbeat|buy)\b", re.I)
NEG = re.compile(r"\b(miss\w*|plung\w+|drop\w*|fall\w*|fell|slump\w*|slid\w*|slides?|tumbl\w+|"
                 r"sink\w*|sank|downgrade\w*|cut\w*|weak\w*|loss\w*|lawsuit|probe|fined?|warn\w*|"
                 r"bearish|underperform\w*|layoffs?|delay\w*|concern\w*|risk\w*|declin\w+|lower|"
                 r"slow\w*|disappoint\w*|pressure|sell-?off|crash\w*|halt\w*|recall\w*)\b", re.I)


def sentiment(text: str) -> float:
    p, n = len(POS.findall(text)), len(NEG.findall(text))
    return 0.0 if p + n == 0 else round((p - n) / (p + n), 2)


def classify(item: dict, sym: str, short: str) -> dict:
    text = f"{item.get('headline', '')} {item.get('summary', '')}"
    head = item.get("headline", "")
    name_rx = re.compile(rf"\b({re.escape(sym)}|{re.escape(short)})\b", re.I) if short else \
        re.compile(rf"\b{re.escape(sym)}\b", re.I)
    rel = "direct" if name_rx.search(head) else ("mention" if name_rx.search(text) else "related")
    cat = next((c for c, rx in CATEGORIES if rx.search(head)), None)
    if not cat and rel != "related":
        cat = next((c for c, rx in CATEGORIES if rx.search(text)), None)
    cat = "analyst" if cat == "analyst2" else cat
    if not cat:
        cat = "market" if re.search(r"\b(stocks?|shares|market|nasdaq|s&p|dow)\b", text, re.I) else "other"
    if OPINION.search(head) and cat not in ("legal", "management"):
        cat = "opinion"
    money, seen_m = [], set()
    for m in MONEY.findall(text):
        k = re.sub(r"[^0-9.a-z]", "", m.lower().replace("us$", "$").replace("billion", "b")
                   .replace("trillion", "t").replace("million", "m"))
        if k not in seen_m:
            seen_m.add(k)
            money.append(m.strip())
    money = money[:4]
    pcts = [m.strip() for m in PCTNUM.findall(head)][:3]
    big = any(re.search(r"(trillion|billion|bn|tn|\d[\d,.]*\s?b\b)", m, re.I) for m in money)
    s = sentiment(text)
    head_money = bool(MONEY.search(head))
    score = CAT_WEIGHT.get(cat, 0.5) + (1.5 if rel == "direct" else 0.5 if rel == "mention" else 0) \
        + ((1.2 if big else 0.4) if head_money and cat != "opinion" else 0) + abs(s) * 0.8 \
        + (0.6 if newsmod.STRONG_SOURCES.search(item.get("source") or "") else 0)
    if item.get("ts"):
        hrs = max((time.time() - item["ts"]) / 3600, 0)
        score += max(0.0, 1.5 - hrs / 48)
    return {**item, "cat": cat, "rel": rel, "money": money, "pcts": pcts, "big": big,
            "sent": s, "impact": round(score, 2)}


def _dedupe_one(items: list[dict]) -> list[dict]:
    """
    One company's feed: every item carries the same ticker, so the shared-
    ticker rule in news.dedupe (right for the home screen) would merge any two
    headlines that happen to share half their words. Here only the same URL or
    near-identical wording counts as the same story.
    """
    out, urls, keys = [], set(), []
    for it in items:
        u = newsmod.url_key(it.get("url"))
        if u and u in urls:
            continue
        k = newsmod._key(it.get("headline") or "")
        if not k or any(len(k & p) / max(len(k | p), 1) > 0.6 for p in keys):
            continue
        urls.add(u)
        keys.append(k)
        out.append(it)
    return out


def _load_news(sym: str) -> dict:
    ov = _safe(lambda: overview(sym)) or {}
    short = ov.get("short") or ""
    items, provs, errs = [], [], []
    if newsmod.api_key():
        try:
            # Finnhub answers with the newest ~250 items whatever the range —
            # for a megacap that is three days. Four windows cover the month.
            to = date.today()
            rows = []
            for a, b in ((0, 3), (3, 10), (10, 20), (20, 30)):
                try:
                    rows += _finnhub("company-news", symbol=sym,
                                     **{"from": str(to - timedelta(days=b)),
                                        "to": str(to - timedelta(days=a))}) or []
                except Exception as e:
                    errs.append(f"Finnhub {a}-{b}d: {e.__class__.__name__}")
            for r in rows:
                it = newsmod._item(r.get("headline"), r.get("url"), r.get("source"),
                                   newsmod._ts(r.get("datetime")), r.get("summary"),
                                   [sym] + [x for x in str(r.get("related") or "").split(",") if x],
                                   "company")
                if it:
                    items.append(it)
            provs.append("Finnhub")
        except Exception as e:
            errs.append(f"Finnhub: {e.__class__.__name__}")
    try:
        y = newsmod._yahoo([sym], per=30)
        if y:
            items += y
            provs.append("Yahoo")
    except Exception as e:
        errs.append(f"Yahoo: {e.__class__.__name__}")
    items = [classify(i, sym, short) for i in items]
    items.sort(key=lambda i: -(i.get("ts") or 0))
    items = _dedupe_one(items)
    # Finnhub tags anything that mentions the company in passing. Those stay
    # in the payload, flagged "related", so the page can offer them without
    # letting another company's news pass for this one's.
    mine = [i for i in items if i["rel"] != "related"][:220]
    items = mine + [i for i in items if i["rel"] == "related"][:60]
    items.sort(key=lambda i: -(i.get("ts") or 0))

    filings = []
    ci = sec.cik_for(sym)
    if ci:
        try:
            filings = sec.events(ci["cik"], limit=40)
        except Exception as e:
            errs.append(f"SEC: {e.__class__.__name__}")
    deals = [i for i in mine if i["cat"] == "deal"][:12]
    deals += [{"headline": f["what"], "url": f["url"], "source": f"SEC {f['form']}",
               "ts": datetime.fromisoformat(f["date"]).replace(tzinfo=timezone.utc).timestamp(),
               "cat": "deal", "filing": True} for f in filings if f["kind"] == "deal"][:6]
    deals.sort(key=lambda x: -(x.get("ts") or 0))
    impactful = sorted((i for i in mine if i.get("ts") and time.time() - i["ts"] < 14 * 86400),
                       key=lambda i: -i["impact"])[:6]
    counts = {}
    for i in mine:
        counts[i["cat"]] = counts.get(i["cat"], 0) + 1
    tone = [i["sent"] for i in mine if i.get("ts") and time.time() - i["ts"] < 7 * 86400]
    return {"symbol": sym, "items": items, "filings": filings, "deals": deals,
            "impactful": impactful, "counts": counts,
            "tone_7d": round(sum(tone) / len(tone), 2) if tone else None,
            "tone_n": len(tone), "providers": provs, "errors": errs,
            "n_mine": len(mine), "n_related": len(items) - len(mine),
            "has_key": bool(newsmod.api_key())}


def news(sym: str) -> dict:
    return store.cache_through(f"rs:news:{sym}", TTL["news"], lambda: _load_news(sym))


# --------------------------------------------------------------------------
# moves: what the stock did each day, and why
# --------------------------------------------------------------------------

MOVE_WORDS = re.compile(r"\b(falls?|fell|drops?|slid\w*|slides?|sinks?|sank|plung\w+|tumbl\w+|"
                        r"jump\w*|soar\w*|surg\w+|rall\w+|rises?|rose|gains?|climb\w*|slump\w*|"
                        r"sell-?off|rebound\w*|spik\w+|skid\w*|wiped|sheds?|adds?)\b|"
                        r"\bwhy\b.*\b(stock|shares)\b|\b(stock|shares)\b.*\b(up|down)\b", re.I)


def _asof(series: dict, days: list[str]) -> dict:
    """A benchmark re-read on the stock's own sessions, carrying the last close
    over a day the index feed skipped. A missing day used to leave two rows
    with no market move at all, which read as "not analysable"."""
    if not series:
        return {}
    ks = sorted(series)
    out, j, last = {}, 0, None
    for d in days:
        while j < len(ks) and ks[j] <= d:
            last = series[ks[j]]
            j += 1
        if last is not None:
            out[d] = last
    return out


def _ols2(y, x1, x2):
    """y = a + b1·x1 + b2·x2, by least squares. Returns (a, b1, b2, resid sd)."""
    import numpy as np
    X = np.column_stack([np.ones(len(y)), x1, x2])
    coef, *_ = np.linalg.lstsq(X, np.array(y), rcond=None)
    res = np.array(y) - X @ coef
    return float(coef[0]), float(coef[1]), float(coef[2]), float(res.std(ddof=3))


def _event_session(ts: float, days: list[str], tz) -> Optional[str]:
    """The trading session an event at `ts` would move."""
    loc = datetime.fromtimestamp(ts, tz)
    d = loc.date().isoformat()
    after = loc.hour + loc.minute / 60 >= 16
    return _next_session(days, d, after=after)


def _day_news(sym: str, day: str, short: str, tz) -> list[dict]:
    """Headlines around one session, from Finnhub's archive; cached for good."""
    def load():
        d = _dt(day)
        rows = _finnhub("company-news", symbol=sym,
                        **{"from": str(d - timedelta(days=1)), "to": str(d + timedelta(days=1))}) or []
        out = []
        for r in rows:
            ts = newsmod._ts(r.get("datetime"))
            if not ts:
                continue
            it = classify({"headline": (r.get("headline") or "")[:240], "summary":
                           (r.get("summary") or "")[:300], "url": r.get("url"),
                           "source": r.get("source"), "ts": ts}, sym, short)
            out.append(it)
        return {"items": out}
    try:
        return store.cache_through(f"rs:day:{sym}:{day}", TTL["daynews"], load).get("items") or []
    except Exception:
        return []


def _pick_headline(cands: list[dict], sym: str, short: str, up: bool) -> Optional[dict]:
    best, bs = None, -1e9
    for it in cands:
        h = it.get("headline") or ""
        s = 0.0
        if it.get("rel") == "direct":
            s += 3
        elif it.get("rel") == "mention":
            s += 1
        if MOVE_WORDS.search(h):
            s += 2
        s += {"earnings": 2, "analyst": 1.5, "deal": 1.5, "legal": 1.5, "management": 1}.get(
            it.get("cat"), 0)
        if it.get("sent") and (it["sent"] > 0) == up:
            s += 1
        if s > bs:
            best, bs = it, s
    return best if best and bs >= 4 else None


def _load_moves(sym: str) -> dict:
    ov = _safe(lambda: overview(sym)) or {}
    tz = _tz(ov.get("tz"))
    short = ov.get("short") or sym
    d = daily(sym)
    bars = d["bars"]
    if len(bars) < 70:
        raise NotFound("not enough price history to analyse daily moves")
    bm = benchmarks_for(sym, ov)
    mk = _closes(bm["market"]["sym"])
    sc = _closes(bm["sector"]["sym"]) if bm.get("sector") else {}
    days = [b[0] for b in bars]
    by = {b[0]: b for b in bars}
    mk, sc = _asof(mk, days), _asof(sc, days) if sc else {}

    # Daily returns, on adjusted closes so an ex-dividend day is not a "drop".
    recs = []
    for i in range(1, len(bars)):
        p, b = bars[i - 1], bars[i]
        r = b[6] / p[6] - 1 if p[6] else None
        m = (mk[b[0]] / mk[p[0]] - 1) if b[0] in mk and p[0] in mk and mk[p[0]] else None
        s = (sc[b[0]] / sc[p[0]] - 1) if sc and b[0] in sc and p[0] in sc and sc[p[0]] else None
        vols = [x[5] for x in bars[max(0, i - 50):i] if x[5]]
        rv = round(b[5] / (sum(vols) / len(vols)), 2) if vols and b[5] else None
        recs.append({"d": b[0], "c": b[4], "r": r, "m": m, "s": s, "rvol": rv,
                     "gap": (b[1] / p[4] - 1) if p[4] else None, "o": b[1], "h": b[2], "l": b[3],
                     "v": b[5]})

    # Two-factor model on the last two years: market, and sector beyond market.
    win = [x for x in recs[-504:] if x["r"] is not None and x["m"] is not None]
    use_sec = bool(sc) and sum(1 for x in win if x["s"] is not None) > len(win) * 0.9
    fit = [x for x in win if not use_sec or x["s"] is not None]
    a = b1 = b2 = 0.0
    sd = statistics.pstdev([x["r"] for x in fit]) if fit else 0.02
    if len(fit) > 120:
        a, b1, b2, sd = _ols2([x["r"] for x in fit], [x["m"] for x in fit],
                              [(x["s"] - x["m"]) if use_sec else 0.0 for x in fit])
    sd_total = statistics.pstdev([x["r"] for x in fit]) if fit else None

    for x in recs:
        if x["r"] is None or x["m"] is None:
            x["idio"] = None
            continue
        x["mkt_part"] = b1 * x["m"]
        x["sec_part"] = b2 * ((x["s"] - x["m"]) if use_sec and x["s"] is not None else 0.0)
        x["idio"] = x["r"] - x["mkt_part"] - x["sec_part"]
        x["z"] = x["idio"] / sd if sd else None

    # Catalysts: earnings, analyst actions, filings, ex-dividend dates, news.
    cats = {}

    def add(day, c):
        if day:
            cats.setdefault(day, []).append(c)
    strt = _safe(lambda: street(sym)) or {}
    for e in strt.get("surprises") or []:
        rd = (e.get("reaction") or {}).get("day")
        if rd:
            sp = e.get("surprise_pct")
            add(rd, {"type": "earnings", "text": (
                f"Earnings {e['when']} {dmy_s(e['date'])}: EPS {e.get('eps_act')} vs "
                f"{e.get('eps_est')} expected" + (f" ({sp:+.1f}% surprise)" if sp is not None else "")),
                "w": 5})
    for ac in strt.get("actions") or []:
        day = _event_session(ac["ts"], days, tz)
        verb = {"up": "upgrades", "down": "downgrades", "init": "starts at",
                "main": "keeps", "reit": "reiterates"}.get(ac.get("action"), ac.get("action") or "")
        pt = ""
        if ac.get("pt"):
            pt = f", target {ac['pt']:.0f}" + (f" (was {ac['pt_prior']:.0f})" if ac.get("pt_prior")
                                                 and ac["pt_prior"] != ac["pt"] else "")
        w = 3 if ac.get("action") in ("up", "down") else 1
        add(day, {"type": "analyst", "text": f"{ac.get('firm')} {verb} {ac.get('to') or ''}{pt}".strip(),
                  "w": w, "dir": 1 if ac.get("action") == "up" else -1 if ac.get("action") == "down" else 0})
    for dv in d.get("divs") or []:
        add(dv[0] if dv[0] in by else None, {"type": "dividend",
                                             "text": f"Ex-dividend ({dv[1]:.2f} per share)", "w": 0.5})
    ci = sec.cik_for(sym)
    if ci:
        for f in _safe(lambda: sec.events(ci["cik"], limit=80)) or []:
            if f["form"] in ("10-K", "10-Q") or f.get("kind") == "earnings":
                continue
            acc = f.get("accepted") or ""
            day = f["date"]
            if len(acc) >= 16:
                # EDGAR stamps acceptance in Eastern time despite the "Z".
                hh = int(acc[11:13])
                day = _next_session(days, acc[:10], after=hh >= 16)
            else:
                day = _next_session(days, day, after=False)
            add(day, {"type": "filing", "text": f"{f['form']}: {f['what']}", "url": f["url"],
                      "w": 2 if f["kind"] in ("deal", "risk", "management") else 1})
    nws = _safe(lambda: news(sym)) or {}
    for it in nws.get("items") or []:
        if it.get("ts"):
            day = _event_session(it["ts"], days, tz)
            if day:
                cats.setdefault("_news:" + day, []).append(it)

    # The biggest company-specific days of the last two years get their
    # archived headlines looked up — the recent ones are already in the feed.
    scored = [x for x in recs[-504:] if x.get("z") is not None]
    big = sorted(scored, key=lambda x: -abs(x["z"]))[:24]
    have_fh = bool(newsmod.api_key())
    recent_cut = str(date.today() - timedelta(days=28))
    need = [x["d"] for x in big if x["d"] < recent_cut and abs(x["z"]) >= 2][:20] if have_fh else []
    searched = set(need) | {x["d"] for x in recs if x["d"] >= recent_cut}
    if need:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for day, items in zip(need, ex.map(lambda dd: _day_news(sym, dd, short, tz), need)):
                for it in items:
                    sd_ = _event_session(it["ts"], days, tz)
                    if sd_ == day or (sd_ and days.index(sd_) == days.index(day) + 1
                                      and datetime.fromtimestamp(it["ts"], tz).hour < 10):
                        cats.setdefault("_news:" + day, []).append(it)

    mlab = bm["market"]["label"]
    slab = bm["sector"]["label"] if bm.get("sector") else None

    def explain(x) -> dict:
        r, z = x.get("r"), x.get("z")
        c = sorted(cats.get(x["d"], []), key=lambda c: -c["w"])
        hl = _pick_headline(cats.get("_news:" + x["d"], []), sym, short, (r or 0) >= 0)
        if r is None:
            return {"driver": "n/a", "why": ""}
        if z is None:
            return {"driver": "n/a", "why": "", "catalysts": c}
        mp, spart, idio = x["mkt_part"], x["sec_part"], x["idio"]
        parts = sorted([("market", abs(mp)), ("sector", abs(spart)), ("company", abs(idio))],
                       key=lambda p: -p[1])
        if abs(z) < 1 and abs(r) < (sd_total or sd) * 0.8:
            drv = "quiet"
        elif abs(z) >= 2:
            drv = "company"
        else:
            drv = parts[0][0]
            if drv == "company" and abs(z) < 1.3:
                drv = "mixed"
        why = []
        if drv == "market":
            why.append(f"Moved with the market: {mlab} {x['m'] * 100:+.1f}% accounts for "
                       f"{mp * 100:+.1f}pp of the {r * 100:+.1f}%.")
        elif drv == "sector" and slab:
            why.append(f"Sector day: {slab} {x['s'] * 100:+.1f}% against {mlab} {x['m'] * 100:+.1f}%.")
        elif drv in ("company", "mixed"):
            why.append(f"{idio * 100:+.1f}pp beyond what the market and sector explain "
                       f"({z:+.1f}σ).")
        elif drv == "quiet":
            why.append("Inside its normal range.")
        strong = [x for x in c if x["w"] >= 2]
        if strong and drv in ("company", "mixed"):
            why.append("Likely cause: " + strong[0]["text"] + ".")
        elif strong and drv in ("market", "sector"):
            why.append("Also that day: " + strong[0]["text"] + ".")
        elif hl and drv in ("company", "mixed"):
            why.append("In the news: " + hl["headline"])
        elif drv == "company" and abs(z) >= 2:
            why.append("No catalyst found in earnings, analyst actions, filings"
                       + (" or the news archive." if x["d"] in searched else
                          "; the news archive was not searched for this day."))
        return {"driver": drv, "why": " ".join(why), "catalysts": c[:4],
                "headline": {"text": hl["headline"], "url": hl.get("url"), "source": hl.get("source")}
                if hl else None}

    def pack(x) -> dict:
        e = explain(x)
        return {"d": x["d"], "c": x["c"], "r": _r((x.get("r") or 0) * 100, 2) if x.get("r") is not None else None,
                "m": _r(x["m"] * 100, 2) if x.get("m") is not None else None,
                "s": _r(x["s"] * 100, 2) if x.get("s") is not None else None,
                "mkt": _r((x.get("mkt_part") or 0) * 100, 2), "sec": _r((x.get("sec_part") or 0) * 100, 2),
                "idio": _r((x.get("idio") or 0) * 100, 2) if x.get("idio") is not None else None,
                "z": _r(x.get("z"), 2), "rvol": x.get("rvol"),
                "gap": _r((x.get("gap") or 0) * 100, 2) if x.get("gap") is not None else None,
                "range": _r((x["h"] / x["l"] - 1) * 100, 2) if x.get("l") else None,
                "v": x.get("v"), **e}

    recent = [pack(x) for x in recs[-120:]][::-1]
    biggest = [pack(x) for x in big[:20]]
    # A one-line reason for every session in the model window, so the chart
    # tooltip can answer "why" on any day of the last two years, not just
    # the ones in the table.
    explained = {}
    for x in recs[-504:]:
        if x.get("r") is None:
            continue
        e = explain(x)
        explained[x["d"]] = [_r(x["r"] * 100, 2), e.get("driver"), e.get("why"), _r(x.get("z"), 2)]

    # Returns over standard windows, against both benchmarks.
    def ret_since(series_by_day, days_back=None, since=None):
        ks = sorted(series_by_day)
        if not ks:
            return None
        last = series_by_day[ks[-1]]
        cut = since or str(date.today() - timedelta(days=days_back))
        i = bisect.bisect_right(ks, cut) - 1
        if i < 0:
            if (_dt(ks[0]) - _dt(cut)).days > 15:
                return None
            i = 0
        return _pct(last, series_by_day[ks[i]])
    adj = {b[0]: b[6] for b in bars}
    ytd = f"{date.today().year - 1}-12-31"
    windows = []
    for lab, n, since in (("1W", 7, None), ("1M", 30, None), ("3M", 91, None), ("6M", 182, None),
                          ("YTD", None, ytd), ("1Y", 365, None), ("3Y", 1095, None), ("5Y", 1826, None),
                          ("10Y", 3652, None)):
        windows.append({"k": lab, "stock": ret_since(adj, n, since), "market": ret_since(mk, n, since),
                        "sector": ret_since(sc, n, since) if sc else None})

    last_year = [x for x in recs if x["d"] >= str(date.today() - timedelta(days=365)) and x.get("r") is not None]
    ups = [x["r"] for x in last_year if x["r"] > 0]
    dns = [x["r"] for x in last_year if x["r"] < 0]
    streak, sgn0 = 0, None
    for x in reversed(recs):
        if x.get("r") is None or x["r"] == 0:
            break
        sg = x["r"] > 0
        if sgn0 is None:
            sgn0 = sg
        if sg != sgn0:
            break
        streak += 1
    peak, mdd = 0.0, 0.0
    for x in last_year:
        peak = max(peak, x["c"])
        mdd = min(mdd, x["c"] / peak - 1)
    vol30 = statistics.pstdev([x["r"] for x in recs[-21:] if x.get("r") is not None]) if len(recs) > 21 else None
    vol1y = statistics.pstdev([x["r"] for x in last_year]) if len(last_year) > 30 else None

    monthly = {}
    for b in bars:
        monthly[b[0][:7]] = b[6]
    mk_keys = sorted(monthly)
    months = [{"m": k, "r": _pct(monthly[k], monthly[p])} for p, k in zip(mk_keys, mk_keys[1:])][-120:]

    return {
        "symbol": sym, "benchmarks": {"market": mlab, "sector": slab},
        "model": {"beta_market": round(b1, 2), "beta_sector": round(b2, 2) if use_sec else None,
                  "alpha_daily_bp": round(a * 1e4, 2), "sd_idio": round(sd * 100, 2),
                  "sd_total": round((sd_total or 0) * 100, 2), "n": len(fit),
                  "share_company": round((sd ** 2) / (sd_total ** 2) * 100, 0) if sd_total else None},
        "stats": {"up_pct": round(len(ups) / len(last_year) * 100, 0) if last_year else None,
                  "avg_up": _r(statistics.mean(ups) * 100, 2) if ups else None,
                  "avg_down": _r(statistics.mean(dns) * 100, 2) if dns else None,
                  "best": max(({"d": x["d"], "r": round(x["r"] * 100, 2)} for x in last_year),
                              key=lambda x: x["r"], default=None),
                  "worst": min(({"d": x["d"], "r": round(x["r"] * 100, 2)} for x in last_year),
                               key=lambda x: x["r"], default=None),
                  "streak": streak * (1 if sgn0 else -1) if sgn0 is not None else 0,
                  "max_dd_1y": round(mdd * 100, 2), "vol30": _r((vol30 or 0) * math.sqrt(252) * 100, 1),
                  "vol1y": _r((vol1y or 0) * math.sqrt(252) * 100, 1)},
        "windows": windows, "days": recent, "biggest": biggest, "explained": explained,
        "calendar": [[x["d"], _r((x.get("r") or 0) * 100, 2), _r(x.get("z"), 2)]
                     for x in recs if x["d"] >= str(date.today() - timedelta(days=371))],
        "months": months,
        "archive": "Finnhub news archive" if have_fh else "no news archive (add a Finnhub key)",
    }


def dmy_s(iso: str) -> str:
    try:
        y, m, d = iso[:10].split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return iso


def moves(sym: str) -> dict:
    return store.cache_through(f"rs:moves:{sym}", TTL["moves"], lambda: _load_moves(sym))


# --------------------------------------------------------------------------
# ownership
# --------------------------------------------------------------------------

def _load_ownership(sym: str) -> dict:
    t = _tk(sym)
    ins = []
    df = _safe(lambda: t.insider_transactions)
    if df is not None and not df.empty:
        for _, r in df.head(60).iterrows():
            text = str(r.get("Text") or "")
            kind = ("buy" if re.search(r"purchase|bought|buy", text, re.I) else
                    "sell" if re.search(r"sale|sold", text, re.I) else
                    "grant" if re.search(r"award|grant", text, re.I) else
                    "gift" if re.search(r"gift", text, re.I) else
                    "exercise" if re.search(r"exercise|conversion", text, re.I) else "other")
            ins.append({"date": str(r.get("Start Date") or "")[:10], "who": r.get("Insider"),
                        "role": r.get("Position"), "shares": _f(r.get("Shares")),
                        "value": _f(r.get("Value")) or None, "text": text or r.get("Transaction"),
                        "kind": kind, "own": r.get("Ownership")})
    cut = str(date.today() - timedelta(days=182))
    recent = [x for x in ins if x["date"] >= cut]
    summ = {"buys": sum(1 for x in recent if x["kind"] == "buy"),
            "sells": sum(1 for x in recent if x["kind"] == "sell"),
            "buy_value": sum(x["value"] or 0 for x in recent if x["kind"] == "buy"),
            "sell_value": sum(x["value"] or 0 for x in recent if x["kind"] == "sell"),
            "sellers": len({x["who"] for x in recent if x["kind"] == "sell"}),
            "buyers": len({x["who"] for x in recent if x["kind"] == "buy"})}
    inst = []
    df = _safe(lambda: t.institutional_holders)
    if df is not None and not df.empty:
        for _, r in df.head(10).iterrows():
            inst.append({"holder": r.get("Holder"), "pct": _r((_f(r.get("pctHeld")) or 0) * 100, 2),
                         "shares": _f(r.get("Shares")), "value": _f(r.get("Value")),
                         "chg": _r((_f(r.get("pctChange")) or 0) * 100, 2)
                         if r.get("pctChange") is not None else None,
                         "date": str(r.get("Date Reported") or "")[:10]})
    funds = []
    df = _safe(lambda: t.mutualfund_holders)
    if df is not None and not df.empty:
        for _, r in df.head(6).iterrows():
            funds.append({"holder": r.get("Holder"), "pct": _r((_f(r.get("pctHeld")) or 0) * 100, 2),
                          "chg": _r((_f(r.get("pctChange")) or 0) * 100, 2)
                          if r.get("pctChange") is not None else None})
    major = {}
    df = _safe(lambda: t.major_holders)
    if df is not None and not df.empty:
        for idx, r in df.iterrows():
            major[str(idx)] = _f(r.iloc[0])
    return {"symbol": sym, "insiders": ins[:30], "insider_summary": summ,
            "institutions": inst, "funds": funds, "major": major, "source": "Yahoo Finance"}


def ownership(sym: str) -> dict:
    return store.cache_through(f"rs:own:{sym}", TTL["ownership"], lambda: _load_ownership(sym))


# --------------------------------------------------------------------------
# catalysts: what is coming, and what the options market thinks of it
# --------------------------------------------------------------------------

def _mid(row) -> Optional[float]:
    b, a, last = _f(row.get("bid")), _f(row.get("ask")), _f(row.get("lastPrice"))
    if b and a and a >= b > 0:
        return (a + b) / 2
    return last


def _straddle(t, exp: str, price: float) -> Optional[dict]:
    ch = t.option_chain(exp)
    calls, puts = ch.calls, ch.puts
    if calls is None or calls.empty or puts is None or puts.empty:
        return None
    k = min(calls["strike"], key=lambda s: abs(s - price))
    c = calls[calls["strike"] == k].iloc[0]
    p = puts[puts["strike"] == k]
    if p.empty:
        return None
    p = p.iloc[0]
    cm, pm = _mid(c), _mid(p)
    if not cm or not pm:
        return None
    iv = _median([_f(c.get("impliedVolatility")), _f(p.get("impliedVolatility"))])
    return {"expiry": exp, "strike": float(k), "straddle": round(cm + pm, 2),
            "move_pct": round((cm + pm) / price * 100, 2), "iv": _r((iv or 0) * 100, 1)}


def _load_catalysts(sym: str) -> dict:
    ov = _safe(lambda: overview(sym)) or {}
    price = (ov.get("quote") or {}).get("price")
    strt = _safe(lambda: street(sym)) or {}
    dates = ov.get("dates") or {}
    nxt = dates.get("earnings") or (strt.get("next_earnings") or [None])[0]
    estimated = dates.get("earnings_estimated")
    # Yahoo keeps the last report's date until it announces the next one:
    # ASML's page said 14 October four days after it. The earnings list
    # already holds the next one.
    if nxt and nxt < date.today().isoformat():
        up = (strt.get("upcoming") or {}).get("date")
        nxt, estimated = (up, None) if up and up >= date.today().isoformat() else (None, None)
    cur_q = next((p for p in strt.get("periods", []) if p["period"] == "0q"), {})
    past = [e for e in strt.get("surprises") or [] if (e.get("reaction") or {}).get("move") is not None]
    moves_ = [abs(e["reaction"]["move"]) for e in past[-12:]]
    beats = [e for e in past[-12:] if (e.get("surprise_pct") or 0) > 0]
    out = {"symbol": sym, "earnings": None, "options": None, "dividend": None, "events": []}
    if nxt:
        days_to = (_dt(nxt) - date.today()).days
        out["earnings"] = {
            "date": nxt, "days": days_to, "estimated": estimated,
            "call_ts": dates.get("earnings_call_ts") if nxt == dates.get("earnings") else None,
            "eps_est": (cur_q.get("eps") or {}).get("avg"), "rev_est": (cur_q.get("rev") or {}).get("avg"),
            "eps_growth": (cur_q.get("eps") or {}).get("growth"),
            "rev_growth": (cur_q.get("rev") or {}).get("growth"),
            "hist_avg_move": round(statistics.mean(moves_), 2) if moves_ else None,
            "hist_max_move": round(max(moves_), 2) if moves_ else None,
            "beat_rate": round(len(beats) / len(past[-12:]) * 100, 0) if past else None,
            "n_hist": len(past[-12:]),
        }
    if price and (ov.get("quote_type") or "EQUITY") in ("EQUITY", "ETF"):
        try:
            t = _tk(sym)
            exps = list(t.options or [])
            if exps:
                opt = {"front": _safe(lambda: _straddle(t, exps[0], price))}
                if nxt:
                    e_exp = next((e for e in exps if e >= nxt), None)
                    if e_exp:
                        opt["earnings"] = _safe(lambda: _straddle(t, e_exp, price))
                        prior = [e for e in exps if e < nxt]
                        if prior and opt.get("earnings"):
                            # The straddle through earnings minus the one just
                            # before it isolates what is priced for the event.
                            pre = _safe(lambda: _straddle(t, prior[-1], price))
                            if pre:
                                e = opt["earnings"]
                                e["pre_expiry"] = pre["expiry"]
                                e["pre_move_pct"] = pre["move_pct"]
                                # Variance is additive in time: take out the
                                # ordinary days, scaled to the longer expiry,
                                # and what is left is priced for the report.
                                t_e = max((_dt(e["expiry"]) - date.today()).days, 1)
                                t_p = max((_dt(pre["expiry"]) - date.today()).days, 1)
                                ev2 = e["move_pct"] ** 2 - pre["move_pct"] ** 2 * t_e / t_p
                                e["event_move_pct"] = round(math.sqrt(ev2), 2) if ev2 > 0 else None
                out["options"] = opt
        except Exception as e:
            out["options"] = {"error": f"{e.__class__.__name__}"}
    if dates.get("ex_div") or dates.get("div_pay"):
        out["dividend"] = {"ex": dates.get("ex_div"), "pay": dates.get("div_pay"),
                           "rate": (ov.get("stats") or {}).get("div_rate"),
                           "yield": (ov.get("stats") or {}).get("div_yield")}
    return out


def catalysts(sym: str) -> dict:
    return store.cache_through(f"rs:cat:{sym}", TTL["catalysts"], lambda: _load_catalysts(sym))


# --------------------------------------------------------------------------
# your thesis, and dated notes against it
# --------------------------------------------------------------------------

THESIS_FIELDS = ("stance", "conviction", "target", "horizon", "review_date", "thesis",
                 "drivers", "risks", "kill")


def thesis_get(sym: str) -> dict:
    store.init_db()
    with store.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM thesis WHERE symbol=? ORDER BY id DESC", (sym,))]
    versions = [r for r in rows if r["kind"] == "thesis"]
    notes = [r for r in rows if r["kind"] == "note"]
    return {"symbol": sym, "current": versions[0] if versions else None,
            "versions": versions, "notes": notes}


def _now_price(sym: str) -> tuple[Optional[float], Optional[str]]:
    try:
        ov = overview(sym)
        return (ov.get("quote") or {}).get("price"), ov.get("currency")
    except Exception:
        return None, None


def thesis_save(sym: str, body: dict) -> dict:
    fields = {k: body.get(k) for k in THESIS_FIELDS}
    if not any((fields.get(k) or "") for k in ("thesis", "drivers", "risks", "kill")):
        raise ValueError("write at least one of: thesis, drivers, risks, what would make you wrong")
    if fields.get("stance") not in (None, "", "bull", "bear", "neutral", "watch"):
        raise ValueError("stance must be bull, bear, neutral or watch")
    price, ccy = _now_price(sym)
    store.init_db()
    with store.conn() as c:
        c.execute("INSERT INTO thesis (symbol,kind,created,stance,conviction,target,horizon,"
                  "review_date,thesis,drivers,risks,kill,price_at,currency) "
                  "VALUES (?,'thesis',?,?,?,?,?,?,?,?,?,?,?,?)",
                  (sym, datetime.now().isoformat(timespec="seconds"), fields.get("stance") or None,
                   _i(fields.get("conviction")), _f(fields.get("target")), fields.get("horizon") or None,
                   fields.get("review_date") or None, fields.get("thesis") or "",
                   fields.get("drivers") or "", fields.get("risks") or "", fields.get("kill") or "",
                   price, ccy))
    return thesis_get(sym)


def note_add(sym: str, body: str) -> dict:
    body = (body or "").strip()
    if not body:
        raise ValueError("an empty note says nothing")
    price, ccy = _now_price(sym)
    store.init_db()
    with store.conn() as c:
        c.execute("INSERT INTO thesis (symbol,kind,created,body,price_at,currency) "
                  "VALUES (?,'note',?,?,?,?)",
                  (sym, datetime.now().isoformat(timespec="seconds"), body[:4000], price, ccy))
    return thesis_get(sym)


def note_delete(note_id: int) -> bool:
    with store.conn() as c:
        return c.execute("DELETE FROM thesis WHERE id=? AND kind='note'", (note_id,)).rowcount > 0


def theses() -> list[dict]:
    """The latest thesis per symbol — for ERN's watchlist and the morning brief."""
    store.init_db()
    with store.conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT t.* FROM thesis t JOIN (SELECT symbol, MAX(id) mid FROM thesis "
            "WHERE kind='thesis' GROUP BY symbol) m ON t.id=m.mid ORDER BY t.created DESC")]


def day(sym: str, d: str) -> dict:
    """One session in full: the decomposition, the catalysts, and its headlines."""
    mv = moves(sym)
    rec = next((x for x in (mv.get("days") or []) + (mv.get("biggest") or []) if x["d"] == d), None)
    ov = _safe(lambda: overview(sym)) or {}
    tz, short = _tz(ov.get("tz")), ov.get("short") or sym
    days = _trading_days(sym)
    if rec is None:
        bars = {b[0]: b for b in daily(sym)["bars"]}
        ex = (mv.get("explained") or {}).get(d)
        if d in bars:
            i = days.index(d)
            prev = bars[days[i - 1]] if i else None
            rec = {"d": d, "c": bars[d][4],
                   "r": _pct(bars[d][6], prev[6]) if prev else None,
                   "gap": _pct(bars[d][1], prev[4]) if prev else None,
                   "range": _pct(bars[d][2], bars[d][3]), "v": bars[d][5],
                   "driver": ex[1] if ex else None, "why": ex[2] if ex else "",
                   "partial": True}
    items = []
    nws = _safe(lambda: news(sym)) or {}
    for it in nws.get("items") or []:
        if it.get("ts") and _event_session(it["ts"], days, tz) == d:
            items.append(it)
    if not items and newsmod.api_key() and d >= str(date.today() - timedelta(days=365)):
        items = [it for it in _day_news(sym, d, short, tz)
                 if _event_session(it["ts"], days, tz) == d]
    rank = {"direct": 0, "mention": 1, "related": 2}
    items.sort(key=lambda i: (rank.get(i.get("rel"), 3), -(i.get("impact") or 0)))
    return {"symbol": sym, "d": d, "move": rec, "news": items[:14],
            "archive": bool(newsmod.api_key())}


PARTS = {
    "overview": overview, "chart": chart, "street": street, "fundamentals": fundamentals,
    "business": business, "valuation": valuation, "peers": peers, "news": news,
    "moves": moves, "ownership": ownership, "catalysts": catalysts,
}
