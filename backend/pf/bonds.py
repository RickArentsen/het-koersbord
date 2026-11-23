"""
BOND — the bond market, one page beside MKT.

MKT carries seven rate rows. Each of them raises a question this page answers:

    the curve       where every Treasury maturity is, today against a week,
                    a month, a quarter and a year ago, and what SHAPE it has
    the yields      2, 10, 20 and 30 years: level, changes in basis points,
                    where they sit in their own history ("highest since…")
    the anatomy     a 10-year yield is inflation compensation plus a real
                    yield, or expected short rates plus a term premium;
                    both splits are published and both are shown
    inflation       CPI, core PCE, breakevens, real policy rates, US and euro
    stocks          whether bonds are hedging equities (the stock-bond
                    correlation), which sectors and which of YOUR holdings
                    move with yields, the equity risk premium, rate-shock days
    your book       what a slice of a bond fund would have done to it, with
                    the funds a euro investor can actually buy (UCITS)
    Europe          the euro curve, daily Bunds, member-state spreads, the ECB
    the world       10-year yields and policy rates across eight markets
    credit          investment-grade and high-yield spreads, mortgage rates
    auctions        what the Treasury is selling, and how well it sold

Every source is keyless:

    U.S. Treasury   the par curve and TIPS curve, same day
    FRED            the curve since 1990, real yields, breakevens, policy
                    rates, ICE BofA spreads, CPI/PCE, OECD 10-year yields
    ECB             the euro AAA and all-issuer curves, deposit rate, €STR,
                    member-state 10-year yields (monthly)
    Bundesbank      daily Bund yields at 2, 5, 10 and 30 years
    Bank of England gilt yields and Bank Rate
    Japan MOF       JGB yields from 1 to 40 years
    TreasuryDirect  the auction calendar and every result
    Yahoo           intraday yields (^TNX and friends), MOVE, fund prices

Two pitfalls that are handled here rather than rediscovered later:

  * FRED's keyless CSV silently drops every series after the twelfth in one
    request, and answers a request that mixes daily and monthly series with a
    ZIP file. Requests are therefore batched by ten and by frequency.
  * A distributing bond fund's closing price falls on every ex-date. Its
    return must come from the ADJUSTED close or a 3% yielder looks like a
    fund that loses 3% a year.
"""

from __future__ import annotations

import bisect
import csv
import hashlib
import io
import json
import math
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Optional

from . import store, net, feeds


TTL = {
    "us": 6 * 3600,          # FRED is daily, published the next morning
    "ust": 1800,             # the Treasury's own curve lands ~15:30 New York
    "live": 60,
    "monthly": 12 * 3600,
    "euro": 6 * 3600,
    "auctions": 3 * 3600,
    "mkt": 6 * 3600,
    "fit": 12 * 3600,
    "erp": 6 * 3600,
    "junk": 12 * 3600,
}

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _f(x) -> Optional[float]:
    try:
        if x is None:
            return None
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def _r(x, d: int = 2) -> Optional[float]:
    v = _f(x)
    return None if v is None else round(v, d)


def _bp(a, b) -> Optional[int]:
    """a minus b, in basis points."""
    a, b = _f(a), _f(b)
    return None if a is None or b is None else int(round((a - b) * 100))


def _iso(d) -> str:
    return d if isinstance(d, str) else d.isoformat()


def _ago(days: int, today: Optional[date] = None) -> str:
    return ((today or date.today()) - timedelta(days=days)).isoformat()


_MEM: dict = {}
_MEM_LOCK = threading.Lock()


def _cached(k: str, ttl: float, fn):
    """
    store.cache_through, plus the parsed answer kept in memory for up to ten
    minutes: the curve history is several megabytes of JSON and every panel
    on the page reads it.
    """
    now = time.time()
    hit = _MEM.get(k)
    if hit and now - hit[0] < min(ttl, 600):
        return hit[1]
    v = store.cache_through(k, ttl, fn)
    with _MEM_LOCK:
        _MEM[k] = (now, v)
    return v


# Series travel as one shared date index plus one column per series: a dict
# of {date: value} per series repeats every date string thirty times.
def _pack(series: dict[str, dict[str, float]]) -> dict:
    dates = sorted({d for s in series.values() for d in s})
    return {"dates": dates,
            "cols": {k: [s.get(d) for d in dates] for k, s in series.items()}}


def _unpack(p: dict) -> dict[str, dict[str, float]]:
    dates = p.get("dates") or []
    out = {}
    for k, col in (p.get("cols") or {}).items():
        out[k] = {d: v for d, v in zip(dates, col) if v is not None}
    return out


class Ser:
    """A daily series, sorted, with on-or-before lookup."""

    __slots__ = ("d", "v")

    def __init__(self, m: Optional[dict] = None):
        m = m or {}
        self.d = sorted(m)
        self.v = [m[x] for x in self.d]

    def __bool__(self):
        return bool(self.d)

    def __len__(self):
        return len(self.d)

    def last(self):
        return (self.d[-1], self.v[-1]) if self.d else (None, None)

    def on(self, day: str) -> Optional[float]:
        i = bisect.bisect_right(self.d, day) - 1
        return self.v[i] if i >= 0 else None

    def on_d(self, day: str):
        i = bisect.bisect_right(self.d, day) - 1
        return (self.d[i], self.v[i]) if i >= 0 else (None, None)

    def since(self, day: str) -> list[float]:
        i = bisect.bisect_left(self.d, day)
        return self.v[i:]

    def items_since(self, day: str) -> list[tuple[str, float]]:
        i = bisect.bisect_left(self.d, day)
        return list(zip(self.d[i:], self.v[i:]))


def _pctile(vals: list[float], x: Optional[float]) -> Optional[int]:
    if x is None or len(vals) < 30:
        return None
    return int(round(sum(1 for v in vals if v <= x) / len(vals) * 100))


def _highest_since(s: Ser, x: float, before: str, higher: bool = True) -> Optional[str]:
    """
    The last day BEFORE `before` on which the series was at or beyond x —
    "the highest since August 2007" is the sentence a level alone cannot say.
    None when it never was (the highest on record).
    """
    i = bisect.bisect_left(s.d, before) - 1
    while i >= 0:
        v = s.v[i]
        if (v >= x) if higher else (v <= x):
            return s.d[i]
        i -= 1
    return None


def _ord(n: Optional[int]) -> str:
    if n is None:
        return "–"
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _month_name(d: Optional[str]) -> str:
    if not d:
        return ""
    t = date.fromisoformat(d[:10])
    return t.strftime("%b %Y")


def _changes(s: Ser, today: Optional[str] = None) -> dict:
    """Level and changes in bp over the horizons MKT uses, plus a few longer."""
    if not s:
        return {}
    ld, lv = s.last()
    t = date.fromisoformat(ld)
    prev = s.v[-2] if len(s) > 1 else None
    out = {"last": lv, "asof": ld, "d1": _bp(lv, prev)}
    for k, days in (("w1", 7), ("m1", 30), ("m3", 91), ("y1", 365), ("y3", 3 * 365)):
        out[k] = _bp(lv, s.on((t - timedelta(days=days)).isoformat()))
    out["ytd"] = _bp(lv, s.on(f"{t.year - 1}-12-31"))
    yr = s.since((t - timedelta(days=365)).isoformat())
    if yr:
        out["lo52"], out["hi52"] = min(yr), max(yr)
        rng = out["hi52"] - out["lo52"]
        out["range_pos"] = round((lv - out["lo52"]) / rng * 100) if rng > 0 else None
    return out


# --------------------------------------------------------------------------
# fetching: FRED, the Treasury, the ECB, the Bundesbank, the BoE, the MOF
# --------------------------------------------------------------------------

def _fred(ids: list[str], start: str) -> tuple[dict, list[str]]:
    """
    Daily/weekly/monthly FRED series, ten per request.

    FRED's keyless CSV silently returns only the first twelve ids of a longer
    list — no error, just missing columns — so the batching is not optional.
    Callers pass ids of ONE frequency: mixing them gets a ZIP back.
    """
    days = (date.today() - date.fromisoformat(start)).days + 3
    out: dict = {}
    errs: list[str] = []
    # Twice at most: on 14 November 2026 one batch of ten (the effective fed
    # funds rate, SOFR, the breakevens, the term premium) failed while the
    # others answered, and the gap was cached for six hours as a success. The
    # next-Fed-move panel went dark ("no effective rate") with FRED fine.
    for attempt in range(2):
        todo = [i for i in ids if i not in out]
        if not todo:
            break
        chunks = [todo[i:i + 10] for i in range(0, len(todo), 10)]
        with ThreadPoolExecutor(max_workers=max(1, len(chunks))) as ex:
            for res in ex.map(lambda c: feeds.fred(c, days=days), chunks):
                out.update(res.get("series") or {})
                if res.get("error"):
                    errs.append(str(res["error"])[:160])
    missing = [i for i in ids if i not in out]
    return out, missing + ([f"error: {errs[0]}"] if errs and not out else [])


def _carry(k: str, ser: dict, missing: list) -> tuple[dict, list]:
    """
    Series FRED did not send this time, taken from the previous copy of the
    same cache entry: a day-old fed funds rate beats none. Returns the series
    and the ids carried, which the payload lists.
    """
    want = [m for m in missing if not m.startswith("error")]
    if not want:
        return ser, []
    old = _unpack(store.cache_get(k) or {})
    carried = [m for m in want if old.get(m)]
    return {**ser, **{m: old[m] for m in carried}}, carried


US_DAILY = [
    # the nominal curve
    "DGS1MO", "DGS3MO", "DGS6MO", "DGS1", "DGS2", "DGS3", "DGS5", "DGS7", "DGS10",
    "DGS20", "DGS30",
    # TIPS real yields, breakevens, the 5y5y forward, the Kim-Wright term premium
    "DFII5", "DFII10", "DFII30", "T5YIE", "T10YIE", "T5YIFR", "THREEFYTP10",
    # policy
    "DFF", "SOFR", "DFEDTARU", "DFEDTARL",
    # ICE BofA credit (FRED only publishes the last three years of these)
    "BAMLC0A0CM", "BAMLC0A4CBBB", "BAMLH0A0HYM2", "BAMLHE00EHYIOAS",
    "BAMLC0A0CMEY", "BAMLH0A0HYM2EY", "BAMLHE00EHYIEY",
    # junk by rating, BB / B / CCC and lower: spreads and yields, plus the
    # total-return indexes, US and euro (also three years only)
    "BAMLH0A1HYBB", "BAMLH0A2HYB", "BAMLH0A3HYC", "BAMLH0A1HYBBEY", "BAMLH0A2HYBEY",
    "BAMLH0A3HYCEY", "BAMLHYH0A0HYM2TRIV", "BAMLHE00EHYITRIV",
]

MONTHLY = [
    "CPIAUCSL", "CPILFESL", "PCEPI", "PCEPILFE", "MICH", "USREC", "UNRATE",
    # euro-area HICP, all items and ex energy/food/alcohol/tobacco (Eurostat)
    "CP0000EZ19M086NEST", "00XEFDEZ19M086NEST",
    # OECD long-term (10-year) government yields, monthly averages
    "IRLTLT01DEM156N", "IRLTLT01GBM156N", "IRLTLT01JPM156N", "IRLTLT01ITM156N",
    "IRLTLT01FRM156N", "IRLTLT01ESM156N", "IRLTLT01NLM156N", "IRLTLT01CAM156N",
    "IRLTLT01AUM156N", "IRLTLT01CHM156N",
    # policy/overnight rates elsewhere
    "IRSTCI01JPM156N", "IRSTCI01CAM156N", "IRSTCI01AUM156N", "IRSTCI01CHM156N",
]

WEEKLY = ["MORTGAGE30US"]

TENORS = [("1M", "DGS1MO", 1 / 12), ("3M", "DGS3MO", .25), ("6M", "DGS6MO", .5),
          ("1Y", "DGS1", 1), ("2Y", "DGS2", 2), ("3Y", "DGS3", 3), ("5Y", "DGS5", 5),
          ("7Y", "DGS7", 7), ("10Y", "DGS10", 10), ("20Y", "DGS20", 20), ("30Y", "DGS30", 30)]


def _load_us() -> dict:
    ser, missing = _fred(US_DAILY, "1990-01-01")
    if not ser:
        raise RuntimeError("FRED returned nothing: " + ", ".join(missing[-1:]))
    ser, carried = _carry("bonds:us", ser, missing)
    return {**_pack(ser), "missing": [m for m in missing if m not in carried], "carried": carried,
            "fetched_at": time.time()}


UST_CSV = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
           "daily-treasury-rates.csv/{y}/all?type={t}&field_tdr_date_value={y}&page&_format=csv")
UST_NOMINAL = {"1 Mo": "DGS1MO", "3 Mo": "DGS3MO", "6 Mo": "DGS6MO", "1 Yr": "DGS1",
               "2 Yr": "DGS2", "3 Yr": "DGS3", "5 Yr": "DGS5", "7 Yr": "DGS7",
               "10 Yr": "DGS10", "20 Yr": "DGS20", "30 Yr": "DGS30"}
UST_REAL = {"5 YR": "DFII5", "10 YR": "DFII10", "30 YR": "DFII30"}


def _parse_ust(raw: bytes, cols: dict) -> dict[str, dict]:
    out: dict[str, dict] = {v: {} for v in cols.values()}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))):
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", row.get("Date") or "")
        if not m:
            continue
        d = f"{m[3]}-{m[1]}-{m[2]}"
        for c, sid in cols.items():
            v = _f((row.get(c) or "").strip() or None)
            if v is not None:
                out[sid][d] = v
    return out


def _load_ust() -> dict:
    """
    The Treasury's own daily curve for this year. FRED's DGS series ARE this
    curve, a day later; reading it at the source puts today's close on the
    page the same evening.
    """
    y = date.today().year
    years = [y] if date.today().month > 1 else [y - 1, y]
    out: dict[str, dict] = {}
    for yy in years:
        for typ, cols in (("daily_treasury_yield_curve", UST_NOMINAL),
                          ("daily_treasury_real_yield_curve", UST_REAL)):
            raw = net.get(UST_CSV.format(y=yy, t=typ), timeout=25)
            for k, m in _parse_ust(raw, cols).items():
                out.setdefault(k, {}).update(m)
    return {**_pack(out), "fetched_at": time.time()}


def us_series() -> dict[str, dict[str, float]]:
    """FRED since 1990 with the Treasury's same-day curve laid over the top."""
    base = _unpack(_cached("bonds:us", TTL["us"], _load_us))
    try:
        ust = _unpack(_cached("bonds:ust", TTL["ust"], _load_ust))
    except Exception:
        ust = {}
    out = {k: dict(v) for k, v in base.items()}
    for k, m in ust.items():
        out.setdefault(k, {}).update(m)
    # The breakevens are DEFINED as nominal minus real, so a same-day
    # Treasury print gives a same-day breakeven too.
    for be, nom, real in (("T5YIE", "DGS5", "DFII5"), ("T10YIE", "DGS10", "DFII10")):
        n, r, b = out.get(nom) or {}, out.get(real) or {}, out.setdefault(be, {})
        last_b = max(b) if b else ""
        for d in (set(n) & set(r)):
            if d > last_b:
                b[d] = round(n[d] - r[d], 2)
    return out


def _load_monthly() -> dict:
    m, miss_m = _fred(MONTHLY, "1989-01-01")
    w, miss_w = _fred(WEEKLY, "2000-01-01")
    # Monthly dates are the first of the month in FRED already.
    ser, carried = _carry("bonds:monthly", {**m, **w}, miss_m + miss_w)
    return {**_pack(ser), "missing": [x for x in miss_m + miss_w if x not in carried], "carried": carried,
            "fetched_at": time.time()}


def monthly_series() -> dict[str, dict[str, float]]:
    return _unpack(_cached("bonds:monthly", TTL["monthly"], _load_monthly))


ECB = "https://data-api.ecb.europa.eu/service/data"
EA_TENORS = [("3M", .25), ("6M", .5), ("1Y", 1), ("2Y", 2), ("3Y", 3), ("5Y", 5),
             ("7Y", 7), ("10Y", 10), ("15Y", 15), ("20Y", 20), ("30Y", 30)]
EA_COUNTRIES = ["DE", "FR", "IT", "ES", "NL", "BE", "AT", "PT", "GR", "IE", "FI"]
COUNTRY_NAME = {"DE": "Germany", "FR": "France", "IT": "Italy", "ES": "Spain",
                "NL": "Netherlands", "BE": "Belgium", "AT": "Austria", "PT": "Portugal",
                "GR": "Greece", "IE": "Ireland", "FI": "Finland", "US": "United States",
                "GB": "United Kingdom", "JP": "Japan", "CA": "Canada", "AU": "Australia",
                "CH": "Switzerland", "EA": "Euro area"}


def _ecb_csv(path: str, start: str) -> list[dict]:
    raw = net.get(f"{ECB}/{path}?startPeriod={start}&format=csvdata&detail=dataonly",
                  timeout=40)
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))))


def _fetch_ecb_curves() -> dict:
    """The AAA curve (≈ Bunds) and the all-issuer curve (the whole euro area)."""
    ten = "+".join(f"SR_{t}" for t, _ in EA_TENORS)
    rows = _ecb_csv(f"YC/B.U2.EUR.4F.G_N_A+G_N_C.SV_C_YM.{ten}", _ago(400))
    out: dict[str, dict] = {}
    for r in rows:
        v = _f(r.get("OBS_VALUE"))
        if v is None:
            continue
        curve = "EA_AAA" if r.get("INSTRUMENT_FM") == "G_N_A" else "EA_ALL"
        tenor = (r.get("DATA_TYPE_FM") or "").replace("SR_", "")
        out.setdefault(f"{curve}_{tenor}", {})[r["TIME_PERIOD"][:10]] = round(v, 4)
    return out


def _fetch_ecb_policy() -> dict:
    out: dict[str, dict] = {}
    for key, name, start in (("FM/D.U2.EUR.4F.KR.DFR.LEV", "ECB_DFR", "1999-01-01"),
                             ("EST/B.EU000A2X2A25.WT", "ESTR", "2019-10-01")):
        for r in _ecb_csv(key, start):
            v = _f(r.get("OBS_VALUE"))
            if v is not None:
                out.setdefault(name, {})[r["TIME_PERIOD"][:10]] = v
    return out


def _fetch_ecb_countries() -> dict:
    """Member-state 10-year yields, monthly — the convergence-criterion series."""
    rows = _ecb_csv(f"IRS/M.{'+'.join(EA_COUNTRIES)}.L.L40.CI.0000.EUR.N.Z", "2000-01")
    out: dict[str, dict] = {}
    for r in rows:
        v = _f(r.get("OBS_VALUE"))
        if v is not None:
            out.setdefault(f"IRS_{r['REF_AREA']}", {})[r["TIME_PERIOD"][:7] + "-01"] = v
    return out


BUBA = ("https://api.statistiken.bundesbank.de/rest/data/BBSIS/"
        "D.I.ZST.ZI.EUR.S1311.B.A604.{m}.R.A.A._Z._Z.A?format=csv&lang=en&startPeriod={s}")


def _fetch_bunds() -> dict:
    """Daily yields on listed federal securities, by residual maturity."""
    raw = net.get(BUBA.format(m="R02XX+R05XX+R10XX+R30XX", s="1998-01-01"), timeout=40)
    text = raw.decode("utf-8-sig", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {}
    head = rows[0]
    cols = {}
    for i, h in enumerate(head):
        m = re.search(r"\.R(\d\d)XX\.", h)
        if m and not h.endswith("_FLAGS"):
            cols[i] = f"BUND{int(m[1])}"
    out: dict[str, dict] = {v: {} for v in cols.values()}
    for r in rows[1:]:
        if not r or not re.match(r"\d{4}-\d{2}-\d{2}$", r[0]):
            continue
        for i, k in cols.items():
            v = _f(r[i]) if i < len(r) else None
            if v is not None:
                out[k][r[0]] = v
    return {k: v for k, v in out.items() if v}


BOE = ("https://www.bankofengland.co.uk/boeapps/database/_iadb-fromshowcolumns.asp?"
       "csv.x=yes&Datefrom=01/Jan/2000&Dateto=now&SeriesCodes={c}&CSVF=TN&UsingCodes=Y&VPD=Y&VFD=N")
BOE_CODES = {"IUDSNZC": "GB5", "IUDMNZC": "GB10", "IUDLNZC": "GB20", "IUDBEDR": "BOE_RATE"}


def _fetch_boe() -> dict:
    raw = net.get(BOE.format(c=",".join(BOE_CODES)), timeout=40)
    out: dict[str, dict] = {v: {} for v in BOE_CODES.values()}
    for r in csv.DictReader(io.StringIO(raw.decode("utf-8", "replace"))):
        try:
            d = datetime.strptime((r.get("DATE") or "").strip(), "%d %b %Y").date().isoformat()
        except ValueError:
            continue
        for c, k in BOE_CODES.items():
            v = _f((r.get(c) or "").strip() or None)
            if v is not None:
                out[k][d] = v
    return {k: v for k, v in out.items() if v}


MOF_ALL = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv"
MOF_NOW = "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv"
ERA = {"S": 1925, "H": 1988, "R": 2018}


def _mof_date(s: str) -> Optional[str]:
    s = s.strip()
    m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})$", s)
    if m:
        return f"{m[1]}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = re.match(r"([SHR])(\d{1,2})\.(\d{1,2})\.(\d{1,2})$", s)
    if m:
        return f"{ERA[m[1]] + int(m[2])}-{int(m[3]):02d}-{int(m[4]):02d}"
    return None


def _parse_mof(raw: bytes) -> dict:
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8", "replace"))))
    head = None
    out: dict[str, dict] = {}
    for r in rows:
        if r and r[0].strip().lower() == "date":
            head = [c.strip() for c in r]
            continue
        if not head or not r:
            continue
        d = _mof_date(r[0])
        if not d or d < "2000-01-01":
            continue
        for i, h in enumerate(head[1:], start=1):
            if h in ("2Y", "5Y", "10Y", "20Y", "30Y", "40Y") and i < len(r):
                v = _f(r[i].strip() or None) if r[i].strip() not in ("-", "") else None
                if v is not None:
                    out.setdefault(f"JP{h[:-1]}", {})[d] = v
    return out


def _fetch_jgb() -> dict:
    out = _parse_mof(net.get(MOF_ALL, timeout=40))
    try:
        for k, m in _parse_mof(net.get(MOF_NOW, timeout=20)).items():
            out.setdefault(k, {}).update(m)
    except Exception:
        pass
    return out


def _load_euro() -> dict:
    """Every non-US source, fetched in parallel; each one fails alone."""
    jobs = {"ecb_curves": _fetch_ecb_curves, "ecb_policy": _fetch_ecb_policy,
            "ecb_countries": _fetch_ecb_countries, "bundesbank": _fetch_bunds,
            "boe": _fetch_boe, "mof": _fetch_jgb}
    series: dict[str, dict] = {}
    status: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {k: ex.submit(fn) for k, fn in jobs.items()}
        for k, fu in futs.items():
            try:
                got = fu.result()
                series.update(got)
                status[k] = {"ok": bool(got), "series": len(got)}
            except Exception as e:
                status[k] = {"ok": False, "error": f"{e.__class__.__name__}: {e}"[:200]}
    if not series:
        raise RuntimeError("no non-US source answered: " +
                           "; ".join(f"{k} {v.get('error')}" for k, v in status.items()))
    return {**_pack(series), "status": status, "fetched_at": time.time()}


def euro_series() -> tuple[dict, dict]:
    p = _cached("bonds:euro", TTL["euro"], _load_euro)
    return _unpack(p), p.get("status") or {}


# ---- Yahoo ---------------------------------------------------------------

def _yf():
    import yfinance as yf
    return yf


def _closes(data, sym: str, multi: bool = True) -> dict[str, float]:
    # yfinance 1.x keeps the (ticker, field) column levels even for a single
    # symbol when grouped by ticker, so try that layout first whatever `multi`
    # says — the flat layout silently returned nothing for the S&P 500.
    col = None
    for get in (lambda: data[sym]["Close"], lambda: data["Close"]):
        try:
            col = get()
            if hasattr(col, "columns"):          # still a frame: pick the column
                col = col[sym] if sym in col.columns else col.iloc[:, 0]
            break
        except Exception:
            continue
    if col is None:
        return {}
    col = col.dropna()
    return {str(i.date()): float(v) for i, v in col.items() if _f(v) is not None}


def _download(syms: list[str], **kw) -> dict[str, dict[str, float]]:
    if not syms:
        return {}
    data = _yf().download(syms, interval="1d", auto_adjust=True, progress=False,
                          group_by="ticker", threads=True, **kw)
    return {s: _closes(data, s, len(syms) > 1) for s in syms}


LIVE = [("^IRX", "13-week bill"), ("^FVX", "5-year"), ("^TNX", "10-year"),
        ("^TYX", "30-year"), ("^MOVE", "MOVE")]


def _load_live() -> dict:
    """
    Cboe's Treasury yield indices, which Yahoo carries through the session.
    They are quoted in percent (they used to be ten times that; a value above
    20 is taken as the old convention and divided).
    """
    syms = [s for s, _ in LIVE]
    data = _yf().download(syms, period="5d", interval="1d", auto_adjust=False,
                          progress=False, group_by="ticker", threads=True)
    out = {}
    for s, name in LIVE:
        c = _closes(data, s, True)
        if not c:
            continue
        ds = sorted(c)
        last, prev = c[ds[-1]], (c[ds[-2]] if len(ds) > 1 else None)
        if s != "^MOVE" and last > 20:
            last, prev = last / 10, (prev / 10 if prev else prev)
        out[s] = {"name": name, "last": round(last, 3), "prev": _r(prev, 3), "date": ds[-1],
                  "chg_bp": _bp(last, prev) if s != "^MOVE" else None,
                  "chg": _r(last - prev, 2) if (s == "^MOVE" and prev) else None}
    return {"quotes": out, "fetched_at": time.time()}


SECTORS = [("XLK", "Technology"), ("XLC", "Communication"), ("XLY", "Consumer discretionary"),
           ("XLP", "Consumer staples"), ("XLV", "Health care"), ("XLF", "Financials"),
           ("KRE", "Regional banks"), ("XLI", "Industrials"), ("XLB", "Materials"),
           ("XLE", "Energy"), ("XLU", "Utilities"), ("XLRE", "Real estate"),
           ("ITB", "Homebuilders"), ("IWM", "Small caps (Russell 2000)"),
           ("QQQ", "Nasdaq 100"), ("SPY", "S&P 500")]


def _load_mkt() -> dict:
    """The S&P 500 since 1995, the MOVE and VIX, and sector ETFs for three years."""
    long = _download(["^GSPC"], start="1995-01-01")
    short = _download([s for s, _ in SECTORS] + ["^MOVE", "^VIX", "TLT", "EURUSD=X"],
                      period="3y")
    # Without the index most of the stocks panel is empty; raise, so the
    # previous good copy is served instead of this one being cached for hours.
    if len(long.get("^GSPC") or {}) < 1000:
        raise RuntimeError("Yahoo returned no S&P 500 history")
    return {**_pack({**long, **short}), "fetched_at": time.time()}


def mkt_series() -> dict[str, dict[str, float]]:
    return _unpack(_cached("bonds:mkt", TTL["mkt"], _load_mkt))


# --------------------------------------------------------------------------
# the United States: curve, yields, anatomy, policy
# --------------------------------------------------------------------------

SPREADS = [
    ("2s10s", "DGS10", "DGS2", "10y − 2y", "The classic recession gauge. Below zero, "
     "markets expect the Fed to cut; every US recession since 1970 was preceded by it."),
    ("3m10y", "DGS10", "DGS3MO", "10y − 3m", "The Fed's own preferred curve measure: "
     "long rates against today's policy-driven bill rate."),
    ("5s30s", "DGS30", "DGS5", "30y − 5y", "The long end: steepens when investors want "
     "more for holding duration — fiscal worries, inflation risk."),
    ("10s30s", "DGS30", "DGS10", "30y − 10y", "How much extra the market wants for the "
     "last twenty years of maturity."),
    ("2sFF", "DGS2", "DFF", "2y − fed funds", "Below zero: the market expects cuts over "
     "the next two years. Above: hikes."),
]


def _spread_ser(a: dict, b: dict) -> dict[str, float]:
    return {d: round(a[d] - b[d], 4) for d in a if d in b}


def curve_on(ser: dict, day: str) -> list[dict]:
    """Every tenor on (or just before) `day`."""
    out = []
    for lab, sid, yrs in TENORS:
        s = Ser(ser.get(sid))
        d, v = s.on_d(day)
        # A tenor whose last print is weeks older than the day asked for is a
        # gap (the 20-year did not exist 1987–93), not a value.
        if v is not None and (date.fromisoformat(day) - date.fromisoformat(d)).days <= 7:
            out.append({"t": lab, "y": yrs, "v": v, "d": d})
    return out


def _curve_shape(c: list[dict]) -> str:
    v = {p["t"]: p["v"] for p in c}
    short = v.get("3M") or v.get("1M")
    two, ten, thirty = v.get("2Y"), v.get("10Y"), v.get("30Y")
    if None in (short, two, ten):
        return "unknown"
    if ten < two and ten < short:
        return "inverted"
    if two > ten and short < two:
        return "humped"
    if ten < short:
        return "inverted at the front"
    if (thirty or ten) - short > 1.5:
        return "steep"
    if abs(ten - short) < .3:
        return "flat"
    return "upward sloping"


def regime(d2: Optional[int], d10: Optional[int]) -> Optional[dict]:
    """
    The four named curve moves (and the two twists), from how the 2-year and
    10-year changed over the same window. The name says who is driving: the
    front end is the Fed, the long end is growth, inflation and supply.
    """
    if d2 is None or d10 is None:
        return None
    if abs(d2) < 3 and abs(d10) < 3:
        return {"k": "unchanged", "text": "The curve barely moved."}
    steeper = (d10 - d2) > 0
    if d2 >= 0 and d10 >= 0:
        if steeper:
            return {"k": "bear steepener", "text": "Yields rose, the long end most — "
                    "typically inflation, supply or term-premium worries rather than the Fed."}
        return {"k": "bear flattener", "text": "Yields rose, the short end most — "
                "the market pricing a more hawkish Fed."}
    if d2 <= 0 and d10 <= 0:
        if steeper:
            return {"k": "bull steepener", "text": "Yields fell, the short end most — "
                    "the market pricing Fed cuts, often on growth worries."}
        return {"k": "bull flattener", "text": "Yields fell, the long end most — "
                "a flight to duration, or falling inflation expectations."}
    if d2 < 0 < d10:
        return {"k": "twist steepener", "text": "Short yields fell while long yields rose — "
                "easier policy expected, but more inflation or supply risk further out."}
    return {"k": "twist flattener", "text": "Short yields rose while long yields fell — "
            "a tighter Fed now, weaker growth later."}


def _episodes(s: Ser, below: float = 0.0, since: str = "1990-01-01") -> list[dict]:
    """Stretches where a spread sat below `below` for at least 20 sessions."""
    out, start, n = [], None, 0
    for d, v in s.items_since(since):
        if v < below:
            if start is None:
                start, n, low, low_d = d, 0, v, d
            n += 1
            if v < low:
                low, low_d = v, d
            end = d
        elif start is not None:
            if n >= 20:
                out.append({"from": start, "to": end, "days": n, "low": round(low * 100),
                            "low_d": low_d})
            start = None
    if start is not None and n >= 20:
        out.append({"from": start, "to": None, "days": n, "low": round(low * 100), "low_d": low_d})
    return out


def _recessions(m: dict) -> list[list]:
    r = Ser(m.get("USREC"))
    out, start = [], None
    for d, v in zip(r.d, r.v):
        if v >= 1 and start is None:
            start = d
        elif v < 1 and start is not None:
            out.append([start, d])
            start = None
    if start:
        out.append([start, None])
    return out


def _fed_block(ser: dict) -> dict:
    """The target range, the effective rate, and what the 2-year is pricing."""
    from . import sentiment
    lo, hi = Ser(ser.get("DFEDTARL")).last(), Ser(ser.get("DFEDTARU")).last()
    effr = Ser(ser.get("DFF")).last()
    sofr = Ser(ser.get("SOFR")).last()
    two = Ser(ser.get("DGS2")).last()
    nxt = sentiment.next_meeting(sentiment.FOMC)
    out = {"lower": lo[1], "upper": hi[1], "range_asof": hi[0],
           "effr": effr[1], "effr_asof": effr[0], "sofr": sofr[1], "sofr_asof": sofr[0],
           "next_meeting": nxt}
    if two[1] is not None and effr[1] is not None:
        gap = two[1] - effr[1]
        out["two_minus_effr_bp"] = int(round(gap * 100))
        # A crude read — the 2-year is the average expected rate over two
        # years PLUS a term premium — labelled as such on the page.
        out["implied_moves"] = round(gap / 0.25 * 2, 1)
    # The next meeting as futures price it, from the sentiment builder if it
    # has run. The path panel below has every meeting.
    fw = ((store.cache_get("sentiment") or {}).get("fed") or {})
    if fw.get("ok"):
        out["fedwatch"] = {k: fw.get(k) for k in ("cut", "hold", "hike", "implied_post",
                                                   "implied_move_bp", "next", "contract",
                                                   "path_end")}
        out["fedwatch"]["age_h"] = round(store.cache_age("sentiment") / 3600)
    return out


def part_us() -> dict:
    ser = us_series()
    ten = Ser(ser.get("DGS10"))
    if not ten:
        raise RuntimeError("no Treasury yields available")
    asof = ten.last()[0]
    t = date.fromisoformat(asof)

    snaps = [("Today", asof), ("1 week ago", _ago(7, t)), ("1 month ago", _ago(30, t)),
             ("3 months ago", _ago(91, t)), ("1 year ago", _ago(365, t)),
             ("3 years ago", _ago(3 * 365, t))]
    curves = [{"label": lab, "date": d, "points": curve_on(ser, d)} for lab, d in snaps]
    shape = _curve_shape(curves[0]["points"])

    # The four maturities asked about, plus the bill and the 5-year for context.
    keys = []
    for lab, sid in (("3-month", "DGS3MO"), ("2-year", "DGS2"), ("5-year", "DGS5"),
                     ("10-year", "DGS10"), ("20-year", "DGS20"), ("30-year", "DGS30")):
        s = Ser(ser.get(sid))
        if not s:
            continue
        ch = _changes(s)
        lv = ch["last"]
        hist00 = s.since("2000-01-01")
        hs = _highest_since(s, lv, _ago(365, t))
        ls = _highest_since(s, lv, _ago(365, t), higher=False)
        keys.append({"k": sid, "label": lab, **ch,
                     "pct_2000": _pctile(hist00, lv),
                     "pct_10y": _pctile(s.since(_ago(3652, t)), lv),
                     "high_since": hs, "low_since": ls,
                     "first": s.d[0]})

    spreads = []
    for k, a, b, lab, why in SPREADS:
        sp = Ser(_spread_ser(ser.get(a) or {}, ser.get(b) or {}))
        if not sp:
            continue
        ch = _changes(sp)
        eps = _episodes(sp) if k in ("2s10s", "3m10y") else []
        spreads.append({"k": k, "label": lab, "why": why, "last_bp": round(ch["last"] * 100),
                        "asof": ch["asof"], "d1": ch.get("d1"), "m1": ch.get("m1"),
                        "m3": ch.get("m3"), "y1": ch.get("y1"),
                        "pct_1990": _pctile(sp.since("1990-01-01"), ch["last"]),
                        "inverted": ch["last"] < 0,
                        "episodes": eps[-4:]})

    two = Ser(ser.get("DGS2"))
    reg = {}
    for lab, days in (("1 month", 30), ("3 months", 91), ("1 year", 365)):
        d2 = _bp(two.last()[1], two.on(_ago(days, t)))
        d10 = _bp(ten.last()[1], ten.on(_ago(days, t)))
        r = regime(d2, d10)
        if r:
            reg[lab] = {**r, "d2": d2, "d10": d10}

    # Anatomy of the 10-year.
    real = Ser(ser.get("DFII10"))
    be = Ser(ser.get("T10YIE"))
    tp = Ser(ser.get("THREEFYTP10"))
    fwd = Ser(ser.get("T5YIFR"))
    anat = {"nominal": ten.last()[1], "asof": asof,
            "real": real.last()[1], "real_asof": real.last()[0],
            "breakeven": be.last()[1], "be_asof": be.last()[0],
            "term_premium": tp.last()[1], "tp_asof": tp.last()[0],
            "fwd5y5y": fwd.last()[1], "fwd_asof": fwd.last()[0]}
    if anat["term_premium"] is not None:
        tpv = ten.on(anat["tp_asof"])
        anat["expected_path"] = _r(tpv - anat["term_premium"], 2) if tpv is not None else None
        anat["tp_pct_1990"] = _pctile(tp.since("1990-01-01"), anat["term_premium"])
        anat["tp_y1"] = _bp(anat["term_premium"], tp.on(_ago(365, date.fromisoformat(anat["tp_asof"]))))
    if anat["real"] is not None:
        anat["real_pct"] = _pctile(real.since("2003-01-01"), anat["real"])
        anat["real_high_since"] = _highest_since(real, anat["real"], _ago(365, t))
        anat["real_y1"] = _bp(anat["real"], real.on(_ago(365, t)))
    if anat["breakeven"] is not None:
        anat["be_y1"] = _bp(anat["breakeven"], be.on(_ago(365, t)))

    live = {}
    try:
        live = _cached("bonds:live", TTL["live"], _load_live).get("quotes") or {}
    except Exception as e:
        live = {"_error": f"{e.__class__.__name__}: {e}"[:160]}

    mk = {}
    try:
        mk = mkt_series()
    except Exception:
        pass
    move = Ser(mk.get("^MOVE"))
    vix = Ser(mk.get("^VIX"))
    mv = None
    if move:
        ml = move.last()
        yr = move.since(_ago(365, t))
        mv = {"last": round(ml[1], 1), "asof": ml[0], "pct_1y": _pctile(yr, ml[1]),
              "m1": _r(ml[1] - (move.on(_ago(30, t)) or ml[1]), 1),
              "avg_1y": _r(statistics.mean(yr), 1) if yr else None,
              "vix": _r(vix.last()[1], 1) if vix else None}

    out = {"asof": asof, "curves": curves, "shape": shape, "keys": keys, "spreads": spreads,
           "regime": reg, "anatomy": anat, "fed": _fed_block(ser), "live": live, "move": mv,
           "recessions": _recessions(monthly_series_safe()),
           "tenors": [{"t": a, "y": c} for a, _, c in TENORS],
           "fetched_at": time.time()}
    out["insights"] = _us_insights(out)
    return out


def monthly_series_safe() -> dict:
    try:
        return monthly_series()
    except Exception:
        return {}


def _us_insights(p: dict) -> list[dict]:
    ins = []
    keys = {k["k"]: k for k in p["keys"]}
    ten = keys.get("DGS10")
    if ten:
        hs = ten.get("high_since")
        ls = ten.get("low_since")
        if ten.get("range_pos") is not None and ten["range_pos"] >= 90 and hs:
            ins.append({"tone": "warn", "sec": "us",
                        "text": f"The 10-year at {ten['last']:.2f}% is near its 52-week high "
                                f"and the highest since {_month_name(hs)}."})
        elif ten.get("range_pos") is not None and ten["range_pos"] >= 90 and not hs:
            ins.append({"tone": "warn", "sec": "us",
                        "text": f"The 10-year at {ten['last']:.2f}% is the highest in this "
                                f"series (since {ten['first'][:4]})."})
        elif ten.get("range_pos") is not None and ten["range_pos"] <= 10 and ls:
            ins.append({"tone": "good", "sec": "us",
                        "text": f"The 10-year at {ten['last']:.2f}% is near its 52-week low, "
                                f"the lowest since {_month_name(ls)}."})
        if ten.get("m1") is not None and abs(ten["m1"]) >= 25:
            ins.append({"tone": "warn" if ten["m1"] > 0 else "good", "sec": "us",
                        "text": f"The 10-year has moved {ten['m1']:+d}bp in a month — a large "
                                f"move for the world's benchmark rate."})
    r = (p.get("regime") or {}).get("1 month")
    if r and r["k"] != "unchanged":
        ins.append({"tone": "info", "sec": "us",
                    "text": f"Over the last month the curve made a {r['k']} "
                            f"(2y {r['d2']:+d}bp, 10y {r['d10']:+d}bp). {r['text']}"})
    for s in p["spreads"]:
        if s["k"] == "2s10s":
            eps = s.get("episodes") or []
            if s["inverted"]:
                ins.append({"tone": "warn", "sec": "us",
                            "text": f"The 2s10s curve is inverted at {s['last_bp']}bp."})
            elif eps and eps[-1].get("to"):
                ended = eps[-1]["to"]
                ins.append({"tone": "info", "sec": "us",
                            "text": f"The 2s10s curve is +{s['last_bp']}bp. Its last inversion "
                                    f"ran {_month_name(eps[-1]['from'])} – {_month_name(ended)} "
                                    f"(deepest {eps[-1]['low']}bp); historically the recession "
                                    f"came after the curve re-steepened, not during the inversion."})
    a = p.get("anatomy") or {}
    if a.get("real") is not None and a.get("breakeven") is not None:
        hs = a.get("real_high_since")
        ins.append({"tone": "info", "sec": "us",
                    "text": f"Of the 10-year's {a['nominal']:.2f}%, {a['breakeven']:.2f}pp is "
                            f"inflation compensation and {a['real']:.2f}pp is real yield"
                            + (f" — the highest real yield since {_month_name(hs)}." if hs and
                               (a.get('real_pct') or 0) >= 90 else ".")})
    if a.get("term_premium") is not None and (a.get("tp_pct_1990") or 0) >= 80:
        ins.append({"tone": "warn", "sec": "us",
                    "text": f"The term premium is {a['term_premium']:.2f}pp "
                            f"({_ord(a['tp_pct_1990'])} percentile since 1990): investors are "
                            f"demanding extra to hold long bonds, beyond the expected path of rates."})
    f = p.get("fed") or {}
    if f.get("two_minus_effr_bp") is not None:
        g = f["two_minus_effr_bp"]
        if g <= -25:
            ins.append({"tone": "info", "sec": "fed",
                        "text": f"The 2-year sits {abs(g)}bp below fed funds: the market expects "
                                f"roughly {abs(f['implied_moves']):.0f} quarter-point cuts over two years."})
        elif g >= 25:
            ins.append({"tone": "warn", "sec": "fed",
                        "text": f"The 2-year sits {g}bp above fed funds: the market leans towards "
                                f"hikes over the next two years."})
    mv = p.get("move")
    if mv and mv.get("pct_1y") is not None and mv["pct_1y"] >= 85:
        ins.append({"tone": "warn", "sec": "us",
                    "text": f"Bond volatility (MOVE {mv['last']}) is in the {_ord(mv['pct_1y'])} "
                            f"percentile of the year — rate moves are unusually large."})
    return ins


# --------------------------------------------------------------------------
# history for the charts
# --------------------------------------------------------------------------

DERIVED = {"S2s10s": ("DGS10", "DGS2"), "S3m10y": ("DGS10", "DGS3MO"),
           "S5s30s": ("DGS30", "DGS5"), "S10s30s": ("DGS30", "DGS10"),
           "SUS_DE10": ("DGS10", "BUND10"), "SUS_DE2": ("DGS2", "BUND2")}


def _all_series() -> dict:
    out = dict(us_series())
    try:
        e, _ = euro_series()
        out.update(e)
    except Exception:
        pass
    try:
        m = mkt_series()
        out["MOVE"] = m.get("^MOVE") or {}
    except Exception:
        pass
    return out


def _downsample(items: list[tuple[str, float]], max_pts: int = 1400) -> list[list]:
    """Weekly (last value of each ISO week) when there are too many points."""
    if len(items) <= max_pts:
        return [[d, round(v, 4)] for d, v in items]
    out, last_wk = [], None
    for d, v in items:
        wk = date.fromisoformat(d).isocalendar()[:2]
        if wk == last_wk:
            out[-1] = [d, round(v, 4)]
        else:
            out.append([d, round(v, 4)])
            last_wk = wk
    if len(out) > max_pts * 1.5:          # decades: go monthly
        mo, res = None, []
        for d, v in out:
            if d[:7] == mo:
                res[-1] = [d, v]
            else:
                res.append([d, v])
                mo = d[:7]
        out = res
    return out


HIST_OK = re.compile(r"^(DGS\w+|DFII\d+|T5YIE|T10YIE|T5YIFR|THREEFYTP10|DFF|SOFR|DFEDTAR[UL]|"
                     r"BAML\w+|BUND\d+|EA_(AAA|ALL)_\w+|GB\d+|BOE_RATE|JP\d+|ECB_DFR|ESTR|IRS_\w\w|"
                     r"MOVE|S2s10s|S3m10y|S5s30s|S10s30s|SUS_DE10|SUS_DE2)$")


def history(keys: list[str], start: str) -> dict:
    keys = [k for k in keys if HIST_OK.match(k)][:8]
    allser = _all_series()
    out = {}
    for k in keys:
        if k in DERIVED:
            a, b = DERIVED[k]
            m = _spread_ser(allser.get(a) or {}, allser.get(b) or {})
            m = {d: v * 100 for d, v in m.items()}             # spreads in bp
        else:
            m = allser.get(k) or {}
        s = Ser(m)
        out[k] = _downsample(s.items_since(start))
    return {"series": out, "start": start}


# --------------------------------------------------------------------------
# inflation
# --------------------------------------------------------------------------

def _yoy(m: dict, lag: int = 12) -> dict[str, float]:
    ds = sorted(m)
    out = {}
    for i in range(lag, len(ds)):
        a, b = m[ds[i - lag]], m[ds[i]]
        if a:
            out[ds[i]] = round((b / a - 1) * 100, 2)
    return out


def _ann(m: dict, months: int) -> dict[str, float]:
    ds = sorted(m)
    out = {}
    for i in range(months, len(ds)):
        a, b = m[ds[i - months]], m[ds[i]]
        if a:
            out[ds[i]] = round(((b / a) ** (12 / months) - 1) * 100, 2)
    return out


def _month_end(daily: dict) -> dict[str, float]:
    """The last print of each month, keyed on the first of the month."""
    out = {}
    for d in sorted(daily):
        out[d[:7] + "-01"] = daily[d]
    return out


def _mseries(m: dict, since: str = "2000-01-01") -> list[list]:
    return [[d, v] for d, v in sorted(m.items()) if d >= since]


def part_inflation() -> dict:
    mo = monthly_series()
    us = us_series()
    cpi, core = _yoy(mo.get("CPIAUCSL") or {}), _yoy(mo.get("CPILFESL") or {})
    pce, cpce = _yoy(mo.get("PCEPI") or {}), _yoy(mo.get("PCEPILFE") or {})
    core3 = _ann(mo.get("CPILFESL") or {}, 3)
    cpce3 = _ann(mo.get("PCEPILFE") or {}, 3)
    hicp = _yoy(mo.get("CP0000EZ19M086NEST") or {})
    hcore = _yoy(mo.get("00XEFDEZ19M086NEST") or {})
    mich = mo.get("MICH") or {}

    def last(m):
        s = Ser(m)
        return s.last() if s else (None, None)

    def row(name, m, note="", target=None):
        d, v = last(m)
        if v is None:
            return None
        s = Ser(m)
        prev = s.v[-2] if len(s) > 1 else None
        y1 = s.on((date.fromisoformat(d) - timedelta(days=365)).isoformat())
        return {"name": name, "v": v, "asof": d, "prev": prev, "y1": y1, "note": note,
                "target": target, "gap": _r(v - target, 2) if target is not None else None}

    effr = Ser(us.get("DFF")).last()
    euro, _st = ({}, {})
    try:
        euro, _st = euro_series()
    except Exception:
        pass
    dfr = Ser(euro.get("ECB_DFR")).last()

    us_rows = [r for r in (
        row("CPI, headline", cpi, "all items, year on year"),
        row("CPI, core", core, "ex food and energy"),
        row("Core CPI, 3m annualised", core3, "the recent pace, before the year-on-year catches up"),
        row("PCE, headline", pce, "the Fed's 2% target is on this", target=2.0),
        row("PCE, core", cpce, "the Fed's preferred gauge"),
        row("Core PCE, 3m annualised", cpce3, "recent pace"),
        row("Consumers expect (1y)", mich, "University of Michigan survey"),
    ) if r]
    ea_rows = [r for r in (
        row("HICP, headline", hicp, "the ECB's 2% target is on this", target=2.0),
        row("HICP, core", hcore, "ex energy, food, alcohol, tobacco"),
    ) if r]

    mkt = []
    for sid, name, note in (("T5YIE", "5-year breakeven", "average CPI priced for the next 5 years"),
                            ("T10YIE", "10-year breakeven", "…for the next 10 years"),
                            ("T5YIFR", "5y5y forward", "inflation expected 5–10 years out — "
                                                        "the Fed watches this for credibility"),
                            ("DFII5", "5-year real yield", "TIPS: the return after inflation"),
                            ("DFII10", "10-year real yield", ""),
                            ("DFII30", "30-year real yield", "")):
        s = Ser(us.get(sid))
        if not s:
            continue
        ch = _changes(s)
        mkt.append({"k": sid, "name": name, "note": note, **ch,
                    "pct_2003": _pctile(s.since("2003-01-01"), ch["last"])})

    real_policy = []
    cp = last(cpce)
    if effr[1] is not None and cp[1] is not None:
        real_policy.append({"name": "Fed: fed funds − core PCE", "v": _r(effr[1] - cp[1], 2),
                            "rate": effr[1], "infl": cp[1], "asof": cp[0]})
    hc = last(hcore)
    if dfr[1] is not None and hc[1] is not None:
        real_policy.append({"name": "ECB: deposit rate − core HICP", "v": _r(dfr[1] - hc[1], 2),
                            "rate": dfr[1], "infl": hc[1], "asof": hc[0]})

    chart = {
        "CPI": _mseries(cpi), "Core PCE": _mseries(cpce),
        "Fed funds": _mseries(_month_end(us.get("DFF") or {})),
        "10-year": _mseries(_month_end(us.get("DGS10") or {})),
        "Euro HICP": _mseries(hicp),
        "ECB deposit": _mseries(_month_end(euro.get("ECB_DFR") or {})),
    }
    out = {"us": us_rows, "ea": ea_rows, "market": mkt, "real_policy": real_policy,
           "chart": chart, "recessions": _recessions(mo), "fetched_at": time.time()}
    out["insights"] = _infl_insights(out)
    return out


def _infl_insights(p: dict) -> list[dict]:
    ins = []
    by = {r["name"]: r for r in p["us"]}
    c = by.get("PCE, core")
    c3 = by.get("Core PCE, 3m annualised")
    if c:
        trend = ""
        if c3 and c3["v"] is not None:
            trend = (" and the last three months ran hotter, at "
                     f"{c3['v']:.1f}% annualised" if c3["v"] > c["v"] + .3 else
                     " and the last three months ran cooler, at "
                     f"{c3['v']:.1f}% annualised" if c3["v"] < c["v"] - .3 else "")
        ins.append({"tone": "warn" if c["v"] > 2.5 else "info", "sec": "inflation",
                    "text": f"Core PCE, the Fed's gauge, is {c['v']:.1f}% year on year"
                            f"{trend}."})
    m = {r["k"]: r for r in p["market"]}
    b10, f55 = m.get("T10YIE"), m.get("T5YIFR")
    if b10:
        ins.append({"tone": "info", "sec": "inflation",
                    "text": f"Bond markets price {b10['last']:.2f}% average inflation over ten "
                            f"years ({b10['y1']:+d}bp in a year)." if b10.get("y1") is not None else
                            f"Bond markets price {b10['last']:.2f}% average inflation over ten years."})
    if f55 and f55["last"] > 2.6:
        ins.append({"tone": "warn", "sec": "inflation",
                    "text": f"Long-run inflation expectations (5y5y {f55['last']:.2f}%) are above "
                            f"the range consistent with a 2% target — a credibility question for the Fed."})
    for r in p["real_policy"]:
        if r["v"] is not None and r["name"].startswith("Fed"):
            ins.append({"tone": "info", "sec": "inflation",
                        "text": f"The real fed funds rate is {r['v']:+.1f}% — "
                                + ("restrictive: policy is squeezing demand." if r["v"] > 1 else
                                   "roughly neutral." if r["v"] > -0.5 else
                                   "negative: policy is still stimulating.")})
    return ins


# --------------------------------------------------------------------------
# stocks and rates
# --------------------------------------------------------------------------

def _rets(m: dict) -> dict[str, float]:
    ds = sorted(m)
    out = {}
    for a, b in zip(ds, ds[1:]):
        if m[a] and m[a] > 0:
            r = m[b] / m[a] - 1
            if abs(r) < 0.5:
                out[b] = r
    return out


def _diffs(m: dict, max_gap_days: int = 6) -> dict[str, float]:
    """Day-on-day changes of a yield, in percentage points."""
    ds = sorted(m)
    out = {}
    for a, b in zip(ds, ds[1:]):
        if (date.fromisoformat(b) - date.fromisoformat(a)).days <= max_gap_days:
            out[b] = m[b] - m[a]
    return out


def _corr(x: list[float], y: list[float]) -> Optional[float]:
    n = len(x)
    if n < 20:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    if sxx <= 0 or syy <= 0:
        return None
    return sum((a - mx) * (b - my) for a, b in zip(x, y)) / math.sqrt(sxx * syy)


def _windows(r: dict, days: list[str], h: int = 5) -> list[float]:
    """
    Compounded returns over every run of `h` consecutive days in `days`.

    For correlating series that close at different hours: your shares close
    in New York, a UCITS bond fund in Frankfurt four and a half hours
    earlier, and on one day's returns the two barely overlap.
    """
    out = []
    for i in range(h, len(days) + 1):
        g = 1.0
        for d in days[i - h:i]:
            g *= 1.0 + r[d]
        out.append(g - 1.0)
    return out


def _ols(y: list[float], xs: list[list[float]]) -> Optional[dict]:
    """
    Ordinary least squares with an intercept, one or two regressors. Plain
    normal equations: two regressors never need anything cleverer.
    """
    n, k = len(y), len(xs)
    if n < 30 or k not in (1, 2):
        return None
    my = sum(y) / n
    mx = [sum(x) / n for x in xs]
    X = [[x[i] - mx[j] for j, x in enumerate(xs)] for i in range(n)]
    Y = [v - my for v in y]
    if k == 1:
        sxx = sum(r[0] ** 2 for r in X)
        if sxx <= 0:
            return None
        b = [sum(r[0] * yy for r, yy in zip(X, Y)) / sxx]
    else:
        a11 = sum(r[0] * r[0] for r in X)
        a12 = sum(r[0] * r[1] for r in X)
        a22 = sum(r[1] * r[1] for r in X)
        c1 = sum(r[0] * yy for r, yy in zip(X, Y))
        c2 = sum(r[1] * yy for r, yy in zip(X, Y))
        det = a11 * a22 - a12 * a12
        if abs(det) < 1e-18:
            return None
        b = [(c1 * a22 - c2 * a12) / det, (a11 * c2 - a12 * c1) / det]
    fit = [sum(b[j] * r[j] for j in range(k)) for r in X]
    ss_res = sum((yy - f) ** 2 for yy, f in zip(Y, fit))
    ss_tot = sum(yy ** 2 for yy in Y)
    return {"b": b, "r2": 1 - ss_res / ss_tot if ss_tot > 0 else None, "n": n}


def rate_beta(r: dict, dy: dict, mkt: Optional[dict] = None, since: str = "") -> Optional[dict]:
    """
    How a return series moves with the 10-year: percent per +10bp, alone and
    after the market's own move is taken out. The second number is the one
    that says a stock is a rate play rather than just a stock.
    """
    days = sorted(d for d in r if d in dy and d >= since and (mkt is None or d in mkt))
    if len(days) < 60:
        return None
    y = [r[d] * 100 for d in days]
    x = [dy[d] * 10 for d in days]               # per 10bp
    one = _ols(y, [x])
    if not one:
        return None
    out = {"per10bp": round(one["b"][0], 3), "r2": round(one["r2"] or 0, 3), "n": len(days),
           "corr": _r(_corr(y, x), 2)}
    if mkt is not None:
        two = _ols(y, [x, [mkt[d] * 100 for d in days]])
        if two:
            out["per10bp_ex_mkt"] = round(two["b"][0], 3)
            out["beta_mkt"] = round(two["b"][1], 2)
    return out


def _rolling_corr(a: dict, b: dict, window: int = 252, step: int = 5,
                  since: str = "") -> list[list]:
    days = sorted(d for d in a if d in b)
    out = []
    xs = [a[d] for d in days]
    ys = [b[d] for d in days]
    for i in range(window, len(days), step):
        if days[i] < since:
            continue
        c = _corr(xs[i - window:i], ys[i - window:i])
        if c is not None:
            out.append([days[i], round(c, 3)])
    return out


def _book() -> dict:
    """Current holdings, weights and currencies — equities only, excluded out."""
    from . import service
    d = service.dashboard("SP500", light=True)
    if d.get("empty"):
        return {"rows": [], "total": 0}
    rows = [{"t": p["ticker"], "w": p.get("value_eur") or 0, "ccy": p.get("currency") or "EUR",
             "name": p.get("name"), "sector": p.get("sector"), "kind": p.get("kind")}
            for p in d.get("positions", [])
            if p.get("ticker") and not p.get("excluded") and (p.get("value_eur") or 0) > 0]
    tot = sum(r["w"] for r in rows)
    for r in rows:
        r["w"] = r["w"] / tot if tot else 0
    rows.sort(key=lambda r: -r["w"])
    # Turbos have no symbol of their own and are not in `rows`; the tail risk
    # (riskmap.tail) counts them as the share they track, at their exposure.
    turbos = [{"t": p["underlying_ticker"], "value": p.get("value_eur") or 0, "exposure": p.get("exposure_eur"),
               "name": p.get("name")}
              for p in d.get("positions", [])
              if p.get("kind") == "DERIVATIVE" and p.get("underlying_ticker") and not p.get("excluded")
              and (p.get("value_eur") or 0) > 0]
    return {"rows": rows, "total": round(tot, 2), "turbos": turbos,
            "cash": (d.get("totals") or {}).get("cash_eur")}


# The bond funds a euro investor can actually buy. US-domiciled ETFs (TLT,
# IEF) are off limits to EU retail accounts under PRIIPs, so every one of
# these is a UCITS fund on a European exchange. Durations are the typical
# figure for the index each tracks — check the factsheet before relying on
# one — and `curve` says which yield on this page stands in for the fund's.
CANDIDATES = [
    {"sym": "XEON.DE", "name": "Xtrackers EUR Overnight Rate Swap", "group": "Cash",
     "ccy": "EUR", "ter": 0.10, "dur": 0.0, "curve": ("ESTR", None),
     "note": "earns €STR, the overnight rate — the no-duration benchmark"},
    {"sym": "IBGS.AS", "name": "iShares € Govt Bond 1-3yr", "group": "Euro government",
     "ccy": "EUR", "ter": 0.20, "dur": 1.9, "curve": ("EA_ALL", 2)},
    {"sym": "IBGX.AS", "name": "iShares € Govt Bond 3-5yr", "group": "Euro government",
     "ccy": "EUR", "ter": 0.20, "dur": 3.8, "curve": ("EA_ALL", 4)},
    {"sym": "IBGM.AS", "name": "iShares € Govt Bond 7-10yr", "group": "Euro government",
     "ccy": "EUR", "ter": 0.20, "dur": 7.6, "curve": ("EA_ALL", 8.5)},
    {"sym": "IBGL.AS", "name": "iShares € Govt Bond 15-30yr", "group": "Euro government",
     "ccy": "EUR", "ter": 0.20, "dur": 17.0, "curve": ("EA_ALL", 22)},
    {"sym": "IEGA.AS", "name": "iShares Core € Govt Bond", "group": "Euro government",
     "ccy": "EUR", "ter": 0.09, "dur": 7.0, "curve": ("EA_ALL", 9)},
    {"sym": "IBCI.AS", "name": "iShares € Inflation Linked Govt Bond", "group": "Inflation-linked",
     "ccy": "EUR", "ter": 0.09, "dur": 7.5, "curve": None,
     "note": "pays a real yield plus euro inflation; no quoted nominal yield"},
    {"sym": "IEAC.AS", "name": "iShares Core € Corp Bond", "group": "Corporate",
     "ccy": "EUR", "ter": 0.20, "dur": 4.5, "curve": ("EA_ALL", 5), "spread": 1.0,
     "note": "investment-grade euro companies; yield ≈ government + ~1pp credit spread"},
    {"sym": "AGGH.AS", "name": "iShares Core Global Aggregate (EUR hedged)", "group": "Global",
     "ccy": "EUR", "ter": 0.10, "dur": 6.5, "curve": None,
     "note": "every investment-grade bond market, currency risk hedged to euros"},
    {"sym": "IBTE.L", "name": "iShares $ Treasury 1-3yr (EUR hedged)", "group": "US Treasuries",
     "ccy": "EUR", "ter": 0.10, "dur": 1.9, "curve": ("UST_H", 2), "hedged": True},
    {"sym": "IBTS.AS", "name": "iShares $ Treasury 1-3yr (unhedged)", "group": "US Treasuries",
     "ccy": "EUR", "ter": 0.07, "dur": 1.9, "curve": ("UST", 2),
     "note": "carries the dollar: you also own EUR/USD"},
    {"sym": "IUSM.DE", "name": "iShares $ Treasury 7-10yr (unhedged)", "group": "US Treasuries",
     "ccy": "EUR", "ter": 0.07, "dur": 7.2, "curve": ("UST", 8.5),
     "note": "carries the dollar: you also own EUR/USD"},
    {"sym": "DTLE.L", "name": "iShares $ Treasury 20+yr (EUR hedged)", "group": "US Treasuries",
     "ccy": "EUR", "ter": 0.10, "dur": 16.5, "curve": ("UST_H", 25), "hedged": True},
    {"sym": "IDTL.L", "name": "iShares $ Treasury 20+yr (unhedged)", "group": "US Treasuries",
     "ccy": "USD", "ter": 0.07, "dur": 16.5, "curve": ("UST", 25),
     "note": "carries the dollar: you also own EUR/USD"},
    # Junk: a yield well above governments, paid for in defaults and in falling
    # with stocks. The yield is the index's, less the fee; defaults come out of it.
    {"sym": "EUNW.DE", "name": "iShares € High Yield Corp Bond", "group": "High yield",
     "ccy": "EUR", "ter": 0.50, "dur": 3.0, "curve": ("HY_EUR", None), "junk": True,
     "note": "euro junk bonds; the yield is before defaults, which come out of it"},
    {"sym": "IHYU.L", "name": "iShares $ High Yield Corp Bond (unhedged)", "group": "High yield",
     "ccy": "USD", "ter": 0.50, "dur": 3.2, "curve": ("HY_US", None), "junk": True,
     "note": "US junk bonds in dollars: you also own EUR/USD; the yield is before defaults"},
]
CAND = {c["sym"]: c for c in CANDIDATES}


def _interp(points: list[tuple[float, float]], x: float) -> Optional[float]:
    pts = sorted(points)
    if not pts:
        return None
    if x <= pts[0][0]:
        return pts[0][1]
    if x >= pts[-1][0]:
        return pts[-1][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return None


def _us_curve_now(us: dict) -> list[tuple[float, float]]:
    return [(yrs, Ser(us.get(sid)).last()[1]) for _, sid, yrs in TENORS
            if Ser(us.get(sid)).last()[1] is not None]


def _ea_curve_now(euro: dict, which: str = "EA_ALL") -> list[tuple[float, float]]:
    return [(yrs, Ser(euro.get(f"{which}_{t}")).last()[1]) for t, yrs in EA_TENORS
            if Ser(euro.get(f"{which}_{t}")).last()[1] is not None]


def hedge_cost(us: dict, euro: dict) -> Optional[dict]:
    """
    What it costs a euro investor to hedge dollars back into euros for a year:
    by covered interest parity, roughly the gap between the two currencies'
    short rates (US 3-month bill against the euro 3-month). The cross-currency
    basis is ignored, which is a few basis points in calm markets.
    """
    us3 = Ser(us.get("DGS3MO")).last()
    ea3 = Ser(euro.get("EA_ALL_3M")).last()
    if ea3[1] is None:
        ea3 = Ser(euro.get("ESTR")).last()
    if us3[1] is None or ea3[1] is None:
        return None
    return {"us3m": us3[1], "ea3m": ea3[1], "cost": round(us3[1] - ea3[1], 2),
            "asof": min(us3[0], ea3[0])}


def fund_yield(c: dict, us: dict, euro: dict, hc: Optional[dict]) -> Optional[float]:
    """An estimate of the fund's yield to maturity, from the curve it lives on."""
    cv = c.get("curve")
    if not cv:
        return None
    kind, yrs = cv
    if kind == "ESTR":
        v = Ser(euro.get("ESTR")).last()[1]
        return _r(v - c["ter"], 2) if v is not None else None
    if kind in ("HY_EUR", "HY_US"):
        v = Ser(us.get("BAMLHE00EHYIEY" if kind == "HY_EUR" else "BAMLH0A0HYM2EY")).last()[1]
        return _r(v - c["ter"], 2) if v is not None else None
    if kind in ("UST", "UST_H"):
        v = _interp(_us_curve_now(us), yrs)
        if v is None:
            return None
        if kind == "UST_H":
            if not hc:
                return None
            v -= hc["cost"]
        return _r(v - c["ter"], 2)
    v = _interp(_ea_curve_now(euro, kind), yrs)
    if v is None:
        return None
    return _r(v + c.get("spread", 0) - c["ter"], 2)


def scenarios(y: Optional[float], dur: float, shifts=(-100, -50, 0, 50, 100)) -> list[dict]:
    """
    Twelve-month return if yields move by `s` basis points at once and stay:
    carry, minus duration times the move, plus a convexity term approximated
    as duration squared. Approximate, and labelled as such.
    """
    if y is None:
        return []
    out = []
    for s in shifts:
        dy = s / 100
        ret = y - dur * dy + 0.5 * (dur ** 2) * (dy ** 2) / 100
        out.append({"shift": s, "ret": round(ret, 2)})
    return out


def _max_dd(idx: list[float]) -> float:
    peak, dd = -1e18, 0.0
    for v in idx:
        peak = max(peak, v)
        dd = min(dd, v / peak - 1)
    return dd


def _stats(rets: list[float], days: list[str]) -> dict:
    if len(rets) < 60:
        return {}
    idx, v = [], 1.0
    for r in rets:
        v *= 1 + r
        idx.append(v)
    yrs = max((date.fromisoformat(days[-1]) - date.fromisoformat(days[0])).days / 365.25, .1)
    ann = v ** (1 / yrs) - 1
    vol = statistics.pstdev(rets) * math.sqrt(252)
    return {"ann": round(ann * 100, 2), "vol": round(vol * 100, 2),
            "maxdd": round(_max_dd(idx) * 100, 2), "total": round((v - 1) * 100, 2)}


def blend(book_r: dict, fund_r: dict, w: float) -> dict[str, float]:
    """
    The book with a fraction `w` in the fund, rebalanced daily to that weight.
    Daily rebalancing flatters nothing here: at these weights the gap to a
    monthly rebalance is a few basis points a year.
    """
    return {d: (1 - w) * book_r[d] + w * fund_r[d] for d in book_r if d in fund_r}


def _load_fit(tickers: list[str]) -> dict:
    """Five years of adjusted closes for the holdings and the candidate funds."""
    syms = sorted(set(tickers) | set(CAND) | {"EURUSD=X", "EURGBP=X", "EURCHF=X"})
    got = _download(syms, period="6y")
    return {**_pack(got), "fetched_at": time.time()}


FX_OF = {"USD": "EURUSD=X", "GBP": "EURGBP=X", "CHF": "EURCHF=X"}


def _to_eur(px: dict, ccy: str, fx: dict) -> dict:
    if ccy == "EUR":
        return px
    scale = 1.0
    if ccy in ("GBp", "GBX"):
        ccy, scale = "GBP", 0.01
    f = Ser(fx.get(FX_OF.get(ccy, "")))
    if not f:
        return {}
    return {d: p * scale / fv for d, p in px.items() if (fv := f.on(d))}


def _book_returns(book: dict, px: dict) -> tuple[dict, list[dict]]:
    """
    Daily EUR returns of TODAY's holdings, held at today's weights. A holding
    without a price that day is left out and the rest are re-weighted, so a
    2024 IPO does not zero out the years before it existed.
    """
    rets = {}
    cover = []
    for r in book["rows"]:
        m = _to_eur(px.get(r["t"]) or {}, r["ccy"], px)
        rr = _rets(m)
        if rr:
            rets[r["t"]] = rr
            cover.append({"t": r["t"], "w": r["w"], "from": min(rr)})
    days = sorted({d for m in rets.values() for d in m})
    out = {}
    wt = {r["t"]: r["w"] for r in book["rows"]}
    for d in days:
        num = den = 0.0
        for t, m in rets.items():
            if d in m:
                num += wt[t] * m[d]
                den += wt[t]
        # Require most of today's book to be priced that day.
        if den >= 0.6:
            out[d] = num / den
    return out, cover


def fit_data() -> dict:
    book = _book()
    tick = [r["t"] for r in book["rows"]]
    key = "bonds:fit:" + hashlib.md5(",".join(sorted(tick)).encode()).hexdigest()[:10]
    px = _unpack(_cached(key, TTL["fit"], lambda: _load_fit(tick)))
    br, cover = _book_returns(book, px) if tick else ({}, [])
    return {"book": book, "px": px, "book_r": br, "cover": cover}


def part_stocks() -> dict:
    us = us_series()
    mk = mkt_series()
    spx = mk.get("^GSPC") or {}
    dy10 = _diffs(us.get("DGS10") or {})
    dy2 = _diffs(us.get("DGS2") or {})
    spx_r = _rets(spx)
    # Bond returns are roughly minus duration times the yield change, so the
    # stock-bond correlation is the correlation of stock returns with the
    # NEGATIVE of the yield change.
    neg = {d: -v for d, v in dy10.items()}
    roll = _rolling_corr(spx_r, neg, window=252, step=5, since="1997-01-01")
    last_c = roll[-1][1] if roll else None
    corr_since = {}
    for lab, since in (("1998–2020", ("1998-01-01", "2020-12-31")), ("2021–now", ("2021-01-01", "9999"))):
        days = [d for d in spx_r if d in neg and since[0] <= d <= since[1]]
        corr_since[lab] = _r(_corr([spx_r[d] for d in days], [neg[d] for d in days]), 2)

    t = date.today()
    since1y = _ago(365, t)
    sect = []
    for sym, name in SECTORS:
        r = _rets(mk.get(sym) or {})
        b = rate_beta(r, dy10, spx_r if sym != "SPY" else None, since1y)
        if b:
            sect.append({"sym": sym, "name": name, **b})
    sect.sort(key=lambda x: x.get("per10bp_ex_mkt", x["per10bp"]))

    fit = {}
    try:
        fit = fit_data()
    except Exception as e:
        fit = {"error": f"{e.__class__.__name__}: {e}"[:200]}
    book_r = fit.get("book_r") or {}
    holdings = []
    you = None
    if book_r:
        you = rate_beta(book_r, dy10, spx_r, since1y)
        you_de = rate_beta(book_r, _diffs(_all_series().get("BUND10") or {}), None, since1y)
        if you:
            you["vs_bund"] = you_de
        px = fit["px"]
        for r in fit["book"]["rows"]:
            rr = _rets(px.get(r["t"]) or {})          # local currency: a rate story, not FX
            b = rate_beta(rr, dy10, spx_r, since1y)
            if b:
                holdings.append({"t": r["t"], "name": r["name"], "w": round(r["w"] * 100, 2),
                                 "sector": r["sector"], **b})
        holdings.sort(key=lambda x: x.get("per10bp_ex_mkt", x["per10bp"]))

    # Rate-shock days: the biggest 10-year moves of the last year, and what
    # equities — and this book — did the same day.
    fomc = set()
    try:
        from . import sentiment
        fomc = set(sentiment.FOMC) | {"2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18",
                                      "2025-07-30", "2025-09-17", "2025-10-29", "2025-12-10"}
    except Exception:
        pass
    shock = []
    for d, v in dy10.items():
        if d >= since1y and d in spx_r:
            shock.append({"d": d, "d10": round(v * 100), "d2": round(dy2[d] * 100) if d in dy2 else None,
                          "spx": round(spx_r[d] * 100, 2),
                          "you": round(book_r[d] * 100, 2) if d in book_r else None,
                          "fomc": d in fomc})
    shock.sort(key=lambda x: -abs(x["d10"]))
    shock = shock[:12]

    # When the 10-year jumps, what do stocks do? Conditional averages, two years.
    since2y = _ago(730, t)
    cond = []
    for lab, lo, hi in (("10y up ≥ 10bp", .10, 9), ("10y up 5–10bp", .05, .10),
                        ("10y within ±5bp", -.05, .05), ("10y down 5–10bp", -.10, -.05),
                        ("10y down ≥ 10bp", -9, -.10)):
        ds = [d for d, v in dy10.items() if d >= since2y and lo <= v < hi and d in spx_r]
        if not ds:
            continue
        cond.append({"label": lab, "n": len(ds),
                     "spx": round(statistics.mean(spx_r[d] for d in ds) * 100, 2),
                     "you": (round(statistics.mean(book_r[d] for d in ds if d in book_r) * 100, 2)
                             if any(d in book_r for d in ds) else None)})

    scatter = [[round(dy10[d] * 100, 1), round(spx_r[d] * 100, 2)]
               for d in sorted(dy10) if d >= since1y and d in spx_r]

    erp = None
    try:
        erp = _cached("bonds:erp", TTL["erp"], _load_erp)
    except Exception:
        pass
    if erp and erp.get("rows"):
        ten = Ser(us.get("DGS10")).last()[1]
        real = Ser(us.get("DFII10")).last()[1]
        for r in erp["rows"]:
            ey = r.get("ey_fwd") or r.get("ey")
            r["gap_nominal"] = _r(ey - ten, 2) if ey is not None and ten is not None else None
            r["gap_real"] = _r(ey - real, 2) if ey is not None and real is not None else None
        erp["ten"], erp["real"] = ten, real

    out = {"rolling_corr": roll, "corr_now": last_c, "corr_eras": corr_since,
           "sectors": sect, "you": you, "holdings": holdings, "shocks": shock,
           "conditional": cond, "scatter": scatter, "erp": erp,
           "fit_error": fit.get("error"), "fetched_at": time.time()}
    out["insights"] = _stock_insights(out)
    return out


def _load_erp() -> dict:
    yf = _yf()
    rows = []
    for sym, name in (("SPY", "S&P 500"), ("QQQ", "Nasdaq 100")):
        try:
            i = yf.Ticker(sym).info
        except Exception:
            continue
        pe, fpe = _f(i.get("trailingPE")), _f(i.get("forwardPE"))
        rows.append({"sym": sym, "name": name, "pe": _r(pe, 1), "fpe": _r(fpe, 1),
                     "ey": _r(100 / pe, 2) if pe else None,
                     "ey_fwd": _r(100 / fpe, 2) if fpe else None})
    return {"rows": rows, "fetched_at": time.time()}


def _stock_insights(p: dict) -> list[dict]:
    ins = []
    c = p.get("corr_now")
    if c is not None:
        if c > 0.15:
            ins.append({"tone": "warn", "sec": "stocks",
                        "text": f"Stocks and Treasuries have moved together over the past year "
                                f"(correlation {c:+.2f}): bonds have NOT been hedging equity "
                                f"sell-offs, as in 2022. That usually happens when inflation, "
                                f"not growth, is the market's main worry."})
        elif c < -0.15:
            ins.append({"tone": "good", "sec": "stocks",
                        "text": f"Stocks and Treasuries have moved in opposite directions over the "
                                f"past year (correlation {c:+.2f}): bonds have been a hedge."})
        else:
            ins.append({"tone": "info", "sec": "stocks",
                        "text": f"The stock–bond correlation is near zero ({c:+.2f}): bonds have "
                                f"been diversifying, but not hedging."})
    y = p.get("you")
    if y and y.get("per10bp") is not None:
        ins.append({"tone": "warn" if y["per10bp"] < -0.25 else "info", "sec": "stocks",
                    "text": f"Your book has moved {y['per10bp']:+.2f}% on average for each +10bp "
                            f"in the US 10-year over the past year"
                            + (f" ({y['per10bp_ex_mkt']:+.2f}% beyond what the market's own move explains)."
                               if y.get("per10bp_ex_mkt") is not None else ".")})
    h = p.get("holdings") or []
    if len(h) >= 3:
        worst = [x for x in h[:3] if x.get("per10bp_ex_mkt", 0) < -0.1]
        if worst:
            ins.append({"tone": "info", "sec": "stocks",
                        "text": "Most rate-sensitive of your holdings (beyond the market): "
                                + ", ".join(f"{x['t']} {x['per10bp_ex_mkt']:+.2f}%/10bp" for x in worst) + "."})
    e = p.get("erp") or {}
    for r in e.get("rows") or []:
        if r["sym"] == "SPY" and r.get("gap_nominal") is not None:
            ins.append({"tone": "warn" if r["gap_nominal"] < 1 else "info", "sec": "stocks",
                        "text": f"The S&P 500's earnings yield ({(r.get('ey_fwd') or r.get('ey')):.2f}%) "
                                f"is {r['gap_nominal']:+.2f}pp against the 10-year Treasury: "
                                + ("stocks offer little extra over risk-free bonds." if r["gap_nominal"] < 1
                                   else "stocks still pay a premium over bonds.")})
    return ins


# --------------------------------------------------------------------------
# your book: would a bond fund have helped?
# --------------------------------------------------------------------------

WEIGHTS = [0.1, 0.2, 0.3, 0.4]


def part_portfolio() -> dict:
    us = us_series()
    try:
        euro, _ = euro_series()
    except Exception:
        euro = {}
    fit = fit_data()
    book_r = fit["book_r"]
    px = fit["px"]
    if not book_r:
        return {"available": False,
                "why": "No priced holdings — the what-if needs today's portfolio and its history."}
    hc = hedge_cost(us, euro)
    days_b = sorted(book_r)
    book_stats = _stats([book_r[d] for d in days_b], days_b)
    worst = sorted(days_b, key=lambda d: book_r[d])[: max(10, len(days_b) // 20)]

    cands = []
    weekly = {}
    for c in CANDIDATES:
        m = _to_eur(px.get(c["sym"]) or {}, c["ccy"], px)
        fr = _rets(m)
        common = sorted(d for d in days_b if d in fr)
        if len(common) < 250:
            cands.append({**{k: c.get(k) for k in ("sym", "name", "group", "ter", "dur", "note", "hedged")},
                          "available": False, "why": f"only {len(common)} days of shared history"})
            continue
        br = [book_r[d] for d in common]
        f = [fr[d] for d in common]
        y = fund_yield(c, us, euro, hc)
        row = {**{k: c.get(k) for k in ("sym", "name", "group", "ter", "dur", "note", "hedged", "junk")},
               "available": True, "from": common[0], "to": common[-1], "n": len(common),
               "yield": y,
               "cushion_bp": int(round(y / c["dur"] * 100)) if (y and c["dur"]) else None,
               "scen": scenarios(y, c["dur"]),
               "fund": _stats(f, common), "book": _stats(br, common),
               "corr": _r(_corr(_windows(book_r, common), _windows(fr, common)), 2),
               "corr_1y": _r(_corr(_windows(book_r, [d for d in common if d >= _ago(365)]),
                                   _windows(fr, [d for d in common if d >= _ago(365)])), 2),
               "on_worst": _r(statistics.mean(fr[d] for d in worst if d in fr) * 100, 2)
               if any(d in fr for d in worst) else None,
               "book_on_worst": _r(statistics.mean(book_r[d] for d in worst if d in fr) * 100, 2)
               if any(d in fr for d in worst) else None,
               "y2022": _r((math.prod(1 + fr[d] for d in common if d.startswith("2022")) - 1) * 100, 1)
               if any(d.startswith("2022") for d in common) else None,
               "blends": []}
        for w in WEIGHTS:
            bl = blend({d: book_r[d] for d in common}, fr, w)
            ds = sorted(bl)
            row["blends"].append({"w": w, **_stats([bl[d] for d in ds], ds)})
        cands.append(row)
        # Weekly index for the chart; the page blends these weekly returns
        # itself when the slider moves.
        idx, v, wk = [], 1.0, None
        vb, idxb = 1.0, []
        for d in common:
            v *= 1 + fr[d]
            vb *= 1 + book_r[d]
            k = date.fromisoformat(d).isocalendar()[:2]
            if k == wk:
                idx[-1], idxb[-1] = [d, round(v, 5)], [d, round(vb, 5)]
            else:
                idx.append([d, round(v, 5)])
                idxb.append([d, round(vb, 5)])
                wk = k
        weekly[c["sym"]] = {"fund": idx, "book": idxb}

    # What a euro investor earns on each government curve, hedged or not.
    rows = []
    bund = {2: "BUND2", 5: "BUND5", 10: "BUND10", 30: "BUND30"}
    for yrs, sid in ((2, "DGS2"), (5, "DGS5"), (10, "DGS10"), (30, "DGS30")):
        u = Ser(us.get(sid)).last()[1]
        b = Ser(euro.get(bund[yrs])).last()[1]
        ea = _interp(_ea_curve_now(euro), yrs)
        if u is None:
            continue
        h = _r(u - hc["cost"], 2) if hc else None
        rows.append({"yrs": yrs, "ust": u, "ust_hedged": h, "bund": b, "ea_all": _r(ea, 2),
                     "pickup_hedged_bp": _bp(h, b) if (h is not None and b is not None) else None})

    out = {"available": True, "book": {"n": len(fit["book"]["rows"]), "stats": book_stats,
                                        "from": days_b[0], "to": days_b[-1],
                                        "value": fit["book"]["total"],
                                        "coverage": fit["cover"][:60]},
           "candidates": cands, "weekly": weekly, "hedge": hc, "curves": rows,
           "cash": {"estr": Ser(euro.get("ESTR")).last()[1],
                    "estr_asof": Ser(euro.get("ESTR")).last()[0],
                    "dfr": Ser(euro.get("ECB_DFR")).last()[1]},
           "weights": WEIGHTS, "fetched_at": time.time()}
    out["insights"] = _fit_insights(out)
    return out


def _fit_insights(p: dict) -> list[dict]:
    ins = []
    ok = [c for c in p["candidates"] if c.get("available") and c["sym"] != "XEON.DE"]
    if not ok:
        return ins
    b0 = p["book"]["stats"]
    # The fund that cut drawdown most at 20%, over the same window as the book.
    def at(c, w):
        return next((b for b in c["blends"] if abs(b["w"] - w) < 1e-9), None)
    best = min(ok, key=lambda c: (at(c, 0.2) or {}).get("maxdd", 0) * -1)
    b20 = at(best, 0.2)
    if b20 and best["book"].get("maxdd") is not None:
        ins.append({"tone": "info", "sec": "portfolio",
                    "text": f"Since {_month_name(best['from'])}, 20% in {best['name']} would have "
                            f"changed your worst drawdown from {best['book']['maxdd']:.1f}% to "
                            f"{b20['maxdd']:.1f}%, volatility from {best['book']['vol']:.1f}% to "
                            f"{b20['vol']:.1f}%, and annual return from {best['book']['ann']:+.1f}% "
                            f"to {b20['ann']:+.1f}%."})
    hedge = [c for c in ok if c.get("on_worst") is not None]
    if hedge:
        h = max(hedge, key=lambda c: c["on_worst"])
        ins.append({"tone": "good" if h["on_worst"] > 0 else "info", "sec": "portfolio",
                    "text": f"On your book's worst days (average {h['book_on_worst']:+.2f}%), "
                            f"{h['name']} did best, averaging {h['on_worst']:+.2f}%."})
    rows = {r["yrs"]: r for r in p.get("curves") or []}
    r10 = rows.get(10)
    if r10 and r10.get("pickup_hedged_bp") is not None and p.get("hedge"):
        pu = r10["pickup_hedged_bp"]
        ins.append({"tone": "info", "sec": "portfolio",
                    "text": f"A US 10-year yields {r10['ust']:.2f}%, but hedging the dollar costs "
                            f"~{p['hedge']['cost']:.2f}pp a year, leaving {r10['ust_hedged']:.2f}% — "
                            + (f"{pu}bp more than a 10-year Bund." if pu > 0 else
                               f"{abs(pu)}bp less than a 10-year Bund; unhedged, the extra yield "
                               f"is a bet on the dollar.")})
    cash = p.get("cash") or {}
    ea10 = next((c for c in ok if c["sym"] == "IBGM.AS"), None)
    if cash.get("estr") is not None and ea10 and ea10.get("yield") is not None:
        ins.append({"tone": "info", "sec": "portfolio",
                    "text": f"Euro cash (€STR) pays {cash['estr']:.2f}%; 7–10 year euro government "
                            f"bonds about {ea10['yield']:.2f}% after costs. Their yield would absorb "
                            f"a rise of ~{ea10['cushion_bp']}bp in a year before the fund lost money."})
    return ins


# --------------------------------------------------------------------------
# Europe and the world
# --------------------------------------------------------------------------

def part_europe() -> dict:
    euro, status = euro_series()
    us = us_series()
    mo = monthly_series_safe()
    anyd = Ser(euro.get("EA_AAA_10Y")) or Ser(euro.get("BUND10"))
    asof = anyd.last()[0] if anyd else date.today().isoformat()
    t = date.fromisoformat(asof)

    def curve(which, day):
        pts = []
        for lab, yrs in EA_TENORS:
            s = Ser(euro.get(f"{which}_{lab}"))
            d, v = s.on_d(day)
            if v is not None and (date.fromisoformat(day) - date.fromisoformat(d)).days <= 7:
                pts.append({"t": lab, "y": yrs, "v": round(v, 3), "d": d})
        return pts

    curves = [{"label": lab, "date": d, "aaa": curve("EA_AAA", d), "all": curve("EA_ALL", d)}
              for lab, d in (("Today", asof), ("1 month ago", _ago(30, t)),
                             ("3 months ago", _ago(91, t)), ("1 year ago", _ago(365, t)))]

    bunds = []
    for k, lab in (("BUND2", "Bund 2-year"), ("BUND5", "Bund 5-year"),
                   ("BUND10", "Bund 10-year"), ("BUND30", "Bund 30-year")):
        s = Ser(euro.get(k))
        if s:
            ch = _changes(s)
            bunds.append({"k": k, "label": lab, **ch,
                          "pct_2000": _pctile(s.since("2000-01-01"), ch["last"]),
                          "high_since": _highest_since(s, ch["last"], _ago(365, t))})

    # Member states: monthly, spread over Germany.
    de = Ser(euro.get("IRS_DE"))
    countries = []
    for c in EA_COUNTRIES:
        s = Ser(euro.get(f"IRS_{c}"))
        if not s:
            continue
        d, v = s.last()
        dev = de.on(d)
        y1d = (date.fromisoformat(d) - timedelta(days=365)).isoformat()
        sp = _bp(v, dev) if c != "DE" else 0
        sp1 = _bp(s.on(y1d), de.on(y1d)) if c != "DE" else 0
        spreads = [(dd, (vv - (de.on(dd) or vv)) * 100) for dd, vv in s.items_since("2008-01-01")]
        countries.append({"c": c, "name": COUNTRY_NAME.get(c, c), "v": v, "asof": d,
                          "y1": _bp(v, s.on(y1d)), "spread": sp,
                          "spread_y1": (sp - sp1) if (sp is not None and sp1 is not None) else None,
                          "spread_max": round(max(x[1] for x in spreads)) if spreads and c != "DE" else None,
                          "spread_max_d": max(spreads, key=lambda x: x[1])[0] if spreads and c != "DE" else None})
    countries.sort(key=lambda x: x["spread"] if x["spread"] is not None else 0)

    dfr = Ser(euro.get("ECB_DFR"))
    estr = Ser(euro.get("ESTR"))
    b2 = Ser(euro.get("BUND2"))
    from . import sentiment
    ecb = {"dfr": dfr.last()[1], "dfr_asof": dfr.last()[0],
           "dfr_y1": _bp(dfr.last()[1], dfr.on(_ago(365, t))) if dfr else None,
           "estr": estr.last()[1], "estr_asof": estr.last()[0],
           "next_meeting": sentiment.next_meeting(sentiment.ECB_MEETINGS)}
    if b2 and dfr:
        g = b2.last()[1] - dfr.last()[1]
        ecb["two_minus_dfr_bp"] = int(round(g * 100))
        ecb["implied_moves"] = round(g / 0.25 * 2, 1)
    # Last change of the deposit rate.
    if dfr:
        for i in range(len(dfr) - 1, 0, -1):
            if dfr.v[i] != dfr.v[i - 1]:
                ecb["last_change"] = {"d": dfr.d[i], "bp": _bp(dfr.v[i], dfr.v[i - 1])}
                break

    us10, de10 = Ser(us.get("DGS10")), Ser(euro.get("BUND10"))
    gap = None
    if us10 and de10:
        g = Ser(_spread_ser(us.get("DGS10") or {}, euro.get("BUND10") or {}))
        if g:
            ch = _changes(g)
            gap = {"last_bp": round(ch["last"] * 100), "asof": ch["asof"], "m1": ch.get("m1"),
                   "y1": ch.get("y1"), "pct_2000": _pctile(g.since("2000-01-01"), ch["last"])}

    hicp = _yoy(mo.get("CP0000EZ19M086NEST") or {})
    out = {"asof": asof, "curves": curves, "bunds": bunds, "countries": countries,
           "ecb": ecb, "us_bund": gap,
           "hicp": Ser(hicp).last() if hicp else None,
           "status": status, "fetched_at": time.time()}
    out["insights"] = _eu_insights(out)
    return out


def _eu_insights(p: dict) -> list[dict]:
    ins = []
    b = {x["k"]: x for x in p["bunds"]}
    b10 = b.get("BUND10")
    if b10:
        hs = b10.get("high_since")
        ins.append({"tone": "info", "sec": "europe",
                    "text": f"The 10-year Bund yields {b10['last']:.2f}% ({b10['y1']:+d}bp in a year)"
                            + (f", the highest since {_month_name(hs)}." if hs and (b10.get('range_pos') or 0) >= 85 else ".")
                            if b10.get("y1") is not None else f"The 10-year Bund yields {b10['last']:.2f}%."})
    it = next((c for c in p["countries"] if c["c"] == "IT"), None)
    fr = next((c for c in p["countries"] if c["c"] == "FR"), None)
    if it and it.get("spread") is not None:
        ins.append({"tone": "warn" if it["spread"] > 200 else "info", "sec": "europe",
                    "text": f"Italy pays {it['spread']}bp over Germany ({_month_name(it['asof'])}), "
                            f"{it['spread_y1']:+d}bp in a year; the peak since 2008 was "
                            f"{it['spread_max']}bp in {_month_name(it['spread_max_d'])}."
                            if it.get("spread_y1") is not None else
                            f"Italy pays {it['spread']}bp over Germany."})
    if fr and it and fr.get("spread") is not None and it.get("spread") is not None \
            and fr["spread"] >= it["spread"] - 20:
        ins.append({"tone": "warn", "sec": "europe",
                    "text": f"France ({fr['spread']}bp over Bunds) now borrows at nearly Italy's "
                            f"spread ({it['spread']}bp) — a fiscal story worth watching."})
    g = p.get("us_bund")
    if g:
        ins.append({"tone": "info", "sec": "europe",
                    "text": f"US 10-year Treasuries yield {g['last_bp']}bp more than Bunds "
                            f"({_ord(g['pct_2000'])} percentile since 2000) — the gap that pulls EUR/USD."})
    e = p.get("ecb") or {}
    if e.get("two_minus_dfr_bp") is not None:
        gg = e["two_minus_dfr_bp"]
        ins.append({"tone": "info", "sec": "europe",
                    "text": f"The 2-year Bund is {gg:+d}bp against the ECB deposit rate "
                            f"({e['dfr']:.2f}%): "
                            + ("the market leans towards ECB cuts." if gg <= -20 else
                               "the market leans towards ECB hikes." if gg >= 20 else
                               "the market expects the ECB to stay roughly where it is.")})
    return ins


def part_global() -> dict:
    us = us_series()
    euro, _ = euro_series()
    mo = monthly_series_safe()
    rows = []

    def add(c, ten_ser, two_ser=None, policy=None, freq="daily", src=""):
        s = Ser(ten_ser)
        if not s:
            return
        d, v = s.last()
        t = date.fromisoformat(d)
        r = {"c": c, "name": COUNTRY_NAME.get(c, c), "ten": v, "asof": d, "freq": freq, "src": src,
             "m1": _bp(v, s.on((t - timedelta(days=31)).isoformat())),
             "y1": _bp(v, s.on((t - timedelta(days=365)).isoformat())),
             "pct_2000": _pctile(s.since("2000-01-01"), v)}
        if two_ser:
            s2 = Ser(two_ser)
            if s2:
                r["two"] = s2.last()[1]
                r["slope_bp"] = _bp(v, s2.on(d))
        if policy:
            p = Ser(policy[0])
            if p:
                r["policy"], r["policy_name"], r["policy_asof"] = p.last()[1], policy[1], p.last()[0]
        rows.append(r)

    add("US", us.get("DGS10"), us.get("DGS2"), (us.get("DFF"), "fed funds"), src="Treasury")
    add("DE", euro.get("BUND10"), euro.get("BUND2"), (euro.get("ECB_DFR"), "ECB deposit"), src="Bundesbank")
    add("GB", euro.get("GB10"), None, (euro.get("BOE_RATE"), "Bank Rate"), src="Bank of England (zero-coupon)")
    add("JP", euro.get("JP10"), euro.get("JP2"), (mo.get("IRSTCI01JPM156N"), "call rate"), src="Japan MOF")
    for c, sid, pol in (("FR", "IRS_FR", None), ("IT", "IRS_IT", None), ("ES", "IRS_ES", None),
                        ("NL", "IRS_NL", None)):
        add(c, euro.get(sid), None, (euro.get("ECB_DFR"), "ECB deposit"), freq="monthly", src="ECB")
    for c in ("CA", "AU", "CH"):
        add(c, mo.get(f"IRLTLT01{c}M156N"), None, (mo.get(f"IRSTCI01{c}M156N"), "overnight"),
            freq="monthly", src="OECD via FRED")

    jp_now, jp_1y = [], []
    for lab, yrs in (("2Y", 2), ("5Y", 5), ("10Y", 10), ("20Y", 20), ("30Y", 30), ("40Y", 40)):
        s = Ser(euro.get(f"JP{yrs}"))
        if s:
            d, v = s.last()
            jp_now.append({"t": lab, "y": yrs, "v": v})
            y1 = s.on((date.fromisoformat(d) - timedelta(days=365)).isoformat())
            if y1 is not None:
                jp_1y.append({"t": lab, "y": yrs, "v": y1})
    out = {"rows": rows, "jgb": {"now": jp_now, "y1": jp_1y}, "fetched_at": time.time()}
    ins = []
    jp = next((r for r in rows if r["c"] == "JP"), None)
    if jp and jp.get("pct_2000") is not None and jp["pct_2000"] >= 95:
        ins.append({"tone": "warn", "sec": "global",
                    "text": f"Japan's 10-year yield ({jp['ten']:.2f}%) is at its highest in this "
                            f"century: the last big source of near-zero yields is gone, which "
                            f"pulls on yields everywhere as Japanese money comes home."})
    hi = [r for r in rows if (r.get("y1") or 0) >= 40]
    if len(hi) >= 3:
        ins.append({"tone": "warn", "sec": "global",
                    "text": "Long yields are up 40bp+ in a year in "
                            + ", ".join(r["name"] for r in hi) + " — a global move, not a US one."})
    out["insights"] = ins
    return out


# --------------------------------------------------------------------------
# credit
# --------------------------------------------------------------------------

def part_credit() -> dict:
    us = us_series()
    mo = monthly_series_safe()
    rows = []
    for sid, name, kind, note in (
            ("BAMLC0A0CM", "US investment grade", "oas", "ICE BofA US Corporate index"),
            ("BAMLC0A4CBBB", "US BBB", "oas", "the lowest investment-grade rung"),
            ("BAMLH0A0HYM2", "US high yield", "oas", "junk bonds"),
            ("BAMLHE00EHYIOAS", "Euro high yield", "oas", ""),
            ("BAMLC0A0CMEY", "US investment grade", "yield", "all-in yield"),
            ("BAMLH0A0HYM2EY", "US high yield", "yield", ""),
            ("BAMLHE00EHYIEY", "Euro high yield", "yield", "")):
        s = Ser(us.get(sid))
        if not s:
            continue
        ch = _changes(s)
        rows.append({"k": sid, "name": name, "kind": kind, "note": note, **ch,
                     "pct": _pctile(s.v, ch["last"]), "since": s.d[0]})
    mort = Ser(mo.get("MORTGAGE30US"))
    mg = None
    if mort:
        d, v = mort.last()
        ten = Ser(us.get("DGS10")).on(d)
        mg = {"last": v, "asof": d, "y1": _bp(v, mort.on(_ago(365, date.fromisoformat(d)))),
              "spread_bp": _bp(v, ten), "pct_2000": _pctile(mort.since("2000-01-01"), v),
              "high_since": _highest_since(mort, v, _ago(365, date.fromisoformat(d)))}
    out = {"rows": rows, "mortgage": mg, "fetched_at": time.time()}
    ins = []
    hy = next((r for r in rows if r["k"] == "BAMLH0A0HYM2"), None)
    if hy:
        ins.append({"tone": "warn" if (hy.get("pct") or 0) <= 15 else "info", "sec": "credit",
                    "text": f"US high-yield spreads are {round(hy['last'] * 100)}bp "
                            f"({_ord(hy['pct'])} percentile since {hy['since'][:4]})"
                            + (": investors are paid very little for default risk." if (hy.get("pct") or 0) <= 15
                               else ": credit markets are pricing stress." if (hy.get("pct") or 0) >= 85
                               else ".")})
    if mg:
        ins.append({"tone": "info", "sec": "credit",
                    "text": f"The 30-year mortgage rate is {mg['last']:.2f}%, {mg['spread_bp']}bp "
                            f"over the 10-year Treasury."})
    out["insights"] = ins
    return out


# --------------------------------------------------------------------------
# junk bonds
# --------------------------------------------------------------------------

# S&P's average one-year default rates for each rating, global companies
# 1981-2024 (its 2024 annual default and rating transition study).
LONG_RUN_DEFAULT = {"BB": 0.56, "B": 2.93, "CCC": 26.12}
# Roughly what a defaulted junk bond has paid back, on average over decades.
RECOVERY = 0.40
JUNK = [("BB", "BB", "BAMLH0A1HYBB", "BAMLH0A1HYBBEY", "the top rung of junk, one step below investment grade"),
        ("B", "B", "BAMLH0A2HYB", "BAMLH0A2HYBEY", "the middle of junk"),
        ("CCC", "CCC and lower", "BAMLH0A3HYC", "BAMLH0A3HYCEY", "the weakest, where most defaults come from"),
        ("US", "US high yield", "BAMLH0A0HYM2", "BAMLH0A0HYM2EY", "the whole US index"),
        ("EU", "Euro high yield", "BAMLHE00EHYIOAS", "BAMLHE00EHYIEY", "the whole euro index")]
# The US index's spread at its turning points, from ICE BofA's published
# history: FRED now serves only the last three years. "approx" marks the
# ones quoted differently from one source to the next.
EPISODES = [{"d": "2007-06", "what": "Before the crisis, the tightest on record", "oas": 2.4, "approx": True},
            {"d": "2008-12", "what": "The financial crisis, the widest", "oas": 20.0, "approx": True},
            {"d": "2016-02", "what": "The oil bust", "oas": 8.87, "approx": False},
            {"d": "2020-03", "what": "Covid, 23 March", "oas": 10.87, "approx": False},
            {"d": "2022-07", "what": "The rate shock", "oas": 6.0, "approx": True}]
LONG_AVG = 5.0           # the US index's average spread since 1997, roughly
JUNK_PX = ["HYG", "SPY"]
JUNK_WINDOWS = [("2008", "The financial crisis", "2007-10-01", "2009-03-31"),
                ("2011", "The euro crisis", "2011-04-01", "2011-10-31"),
                ("2015", "The oil bust", "2015-05-01", "2016-02-29"),
                ("2020", "Covid", "2020-02-01", "2020-04-30"),
                ("2022", "The rate shock", "2022-01-01", "2022-12-31")]


def implied_default(oas: Optional[float], recovery: float = RECOVERY) -> Optional[float]:
    """
    The yearly default rate the spread would just pay for if it paid for
    nothing else: a lender who gets back `recovery` of a defaulted bond
    loses (1 - recovery) of it, so a spread s covers s / (1 - recovery).
    Real spreads also pay for the risk of a bad year, so this is an upper bound
    on what the market expects.
    """
    if oas is None:
        return None
    return round(oas / (1 - recovery), 2)


def default_cover(oas: Optional[float], avg_default: Optional[float], recovery: float = RECOVERY) -> Optional[dict]:
    """The spread against the losses an average year of defaults would cost."""
    if oas is None or not avg_default:
        return None
    loss = avg_default * (1 - recovery)
    return {"loss": round(loss, 2), "left": round(oas - loss, 2), "times": round(oas / loss, 1)}


def _load_junk_px() -> dict:
    # One symbol at a time: Yahoo answers a burst of parallel requests badly.
    yf = _yf()
    got = {}
    for sym in JUNK_PX:
        try:
            d = yf.download(sym, period="max", interval="1d", auto_adjust=True, progress=False, threads=False)
            got[sym] = _closes(d, sym, False)
        except Exception:
            got[sym] = {}
    if len(got.get("HYG") or {}) < 500:
        raise RuntimeError("Yahoo returned no history for HYG")
    return {**_pack(got), "fetched_at": time.time()}


def _window_dd(m: dict, a: str, b: str) -> Optional[float]:
    v = [m[d] for d in sorted(m) if a <= d <= b]
    return round(_max_dd(v) * 100, 1) if len(v) > 5 else None


def junk_vs_stocks(hy: dict, eq: dict) -> Optional[dict]:
    """
    Junk against stocks on the same days: the whole period, the sell-offs,
    and the stock market's worst months. Both are total returns in dollars.
    """
    days = sorted(set(hy) & set(eq))
    if len(days) < 500:
        return None
    hy = {d: hy[d] for d in days}
    eq = {d: eq[d] for d in days}
    rh, re_ = _rets(hy), _rets(eq)
    common = sorted(set(rh) & set(re_))
    out = {"from": days[0], "to": days[-1],
           "hy": _stats([rh[d] for d in common], common), "eq": _stats([re_[d] for d in common], common)}
    out["windows"] = [{"k": k, "what": w, "from": a, "to": b, "hy": _window_dd(hy, a, b), "eq": _window_dd(eq, a, b)}
                      for k, w, a, b in JUNK_WINDOWS if days[0] <= a <= days[-1]]
    mh, me = _month_end(hy), _month_end(eq)
    ms = sorted(set(mh) & set(me))
    mr = [(b, mh[b] / mh[a] - 1, me[b] / me[a] - 1) for a, b in zip(ms, ms[1:])]
    if len(mr) >= 24:
        worst = sorted(mr, key=lambda x: x[2])[:12]
        out["worst_months"] = {"n": len(worst), "eq": round(statistics.mean(x[2] for x in worst) * 100, 1),
                               "hy": round(statistics.mean(x[1] for x in worst) * 100, 1),
                               "hy_down": sum(1 for x in worst if x[1] < 0)}
        out["corr_m"] = _r(_corr([x[1] for x in mr], [x[2] for x in mr]), 2)
        out["capture"] = round(statistics.mean(x[1] for x in worst) / statistics.mean(x[2] for x in worst) * 100)
    # weekly drawdowns from the running high, for the chart
    dd, ph, pe, wk = [], 0.0, 0.0, None
    for d in days:
        ph, pe = max(ph, hy[d]), max(pe, eq[d])
        k = date.fromisoformat(d).isocalendar()[:2]
        row = [d, round((hy[d] / ph - 1) * 100, 2), round((eq[d] / pe - 1) * 100, 2)]
        if k == wk:
            dd[-1] = row
        else:
            dd.append(row)
            wk = k
    out["dd"] = dd
    return out


def realised(tr: dict, ey: dict, oas: dict) -> Optional[dict]:
    """What the index returned a year over the window FRED shows, against the yield it started on."""
    t, y, o = Ser(tr), Ser(ey), Ser(oas)
    if len(t) < 250 or not y:
        return None
    d0, v0 = t.d[0], t.v[0]
    d1, v1 = t.last()
    yrs = (date.fromisoformat(d1) - date.fromisoformat(d0)).days / 365.25
    if yrs < 0.5:
        return None
    return {"from": d0, "to": d1, "ret": round(((v1 / v0) ** (1 / yrs) - 1) * 100, 2),
            "yield0": y.on(d0) if y.on(d0) is not None else y.v[0], "oas0": o.on(d0) if o else None,
            "oas1": o.last()[1] if o else None}


def part_junk() -> dict:
    us = us_series()
    rows = []
    for k, name, oid, yid, note in JUNK:
        s = Ser(us.get(oid))
        if not s:
            continue
        ch = _changes(s)
        y = Ser(us.get(yid)).last()
        lo_i = min(range(len(s.v)), key=lambda i: s.v[i])
        hi_i = max(range(len(s.v)), key=lambda i: s.v[i])
        lr = LONG_RUN_DEFAULT.get(k)
        rows.append({"k": k, "name": name, "note": note, "sid": oid, "oas": ch["last"], "asof": ch["asof"],
                     "m1": ch.get("m1"), "y1": ch.get("y1"), "pct": _pctile(s.v, ch["last"]),
                     "since": s.d[0], "lo": s.v[lo_i], "lo_d": s.d[lo_i], "hi": s.v[hi_i], "hi_d": s.d[hi_i],
                     "yield": y[1], "breakeven": implied_default(ch["last"]), "lr_default": lr,
                     "cover": default_cover(ch["last"], lr)})
    by = {r["k"]: r for r in rows}

    # The gap between the weakest and the strongest junk: when it widens
    # while BB is calm, the stress is in the companies closest to default.
    disp = None
    ccc, bb = Ser(us.get("BAMLH0A3HYC")), Ser(us.get("BAMLH0A1HYBB"))
    if ccc and bb:
        gap = {d: ccc.on(d) - bb.on(d) for d in ccc.d if bb.on(d) is not None}
        g = Ser(gap)
        if g:
            disp = {"gap": round(g.last()[1], 2), "pct": _pctile(g.v, g.last()[1]),
                    "y1": _bp(g.last()[1], g.on(_ago(365, date.fromisoformat(g.last()[0]))))}

    # spreads against the stock market, over the years FRED shows
    mk = {}
    try:
        mk = mkt_series()
    except Exception:
        pass
    spx = mk.get("^GSPC") or {}
    vs = None
    if by.get("US") and spx:
        do = _diffs(us.get("BAMLH0A0HYM2") or {})
        r = _rets(spx)
        common = sorted(set(do) & set(r))
        sp = Ser(spx)
        hi = max(sp.since(_ago(365))) if sp.since(_ago(365)) else None
        dd = round((sp.last()[1] / hi - 1) * 100, 1) if hi else None
        pct = by["US"]["pct"]
        mood = None
        if dd is not None and pct is not None:
            if dd > -5 and pct >= 75:
                mood = "credit"      # spreads are wide while stocks sit near their high
            elif dd < -10 and pct <= 40:
                mood = "stocks"      # stocks have fallen and spreads have not followed
            else:
                mood = "agree"
        vs = {"corr": _r(_corr([do[d] for d in common], [r[d] for d in common]), 2), "n": len(common),
              "spx_dd": dd, "hy_pct": pct, "mood": mood}

    long = None
    try:
        px = _unpack(_cached("bonds:junkpx", TTL["junk"], _load_junk_px))
        long = junk_vs_stocks(px.get("HYG") or {}, px.get("SPY") or {})
    except Exception as e:
        long = {"error": f"{e.__class__.__name__}: {e}"[:200]}

    real = []
    for name, tr, ey, oid in (("US high yield", "BAMLHYH0A0HYM2TRIV", "BAMLH0A0HYM2EY", "BAMLH0A0HYM2"),
                              ("Euro high yield", "BAMLHE00EHYITRIV", "BAMLHE00EHYIEY", "BAMLHE00EHYIOAS")):
        x = realised(us.get(tr) or {}, us.get(ey) or {}, us.get(oid) or {})
        if x:
            real.append({"name": name, **x})

    ey = None
    try:
        erp = _cached("bonds:erp", TTL["erp"], _load_erp)
        spy = next((r for r in erp.get("rows") or [] if r["sym"] == "SPY"), None)
        if spy and by.get("US"):
            e = spy.get("ey_fwd") or spy.get("ey")
            ey = {"earn": e, "hy": by["US"]["yield"], "gap": _r(by["US"]["yield"] - e, 2) if e and by["US"]["yield"] else None,
                  "fwd": bool(spy.get("ey_fwd"))}
    except Exception:
        pass

    try:
        euro, _ = euro_series()
    except Exception:
        euro = {}
    hc = hedge_cost(us, euro)
    funds = [{"sym": c["sym"], "name": c["name"], "ccy": c["ccy"], "ter": c["ter"], "dur": c["dur"],
              "yield": fund_yield(c, us, euro, hc), "note": c.get("note")}
             for c in CANDIDATES if c.get("junk")]
    ea3 = _interp(_ea_curve_now(euro), 3) if euro else None

    out = {"rows": rows, "episodes": EPISODES, "long_avg": LONG_AVG, "recovery": RECOVERY,
           "vs": vs, "long": long, "realised": real, "earn": ey, "funds": funds, "dispersion": disp,
           "ea3": _r(ea3, 2), "fetched_at": time.time()}
    out["insights"] = _junk_insights(out)
    return out


def _junk_insights(p: dict) -> list[dict]:
    ins = []
    by = {r["k"]: r for r in p["rows"]}
    us = by.get("US")
    if us:
        below = [e for e in p["episodes"] if us["oas"] < e["oas"]]
        txt = (f"US junk pays {round(us['oas'] * 100)}bp over Treasuries, the {_ord(us['pct'])} percentile "
               f"of the last three years and {'below' if us['oas'] < p['long_avg'] else 'above'} the long-run average of about {p['long_avg']:.0f}%")
        if us["oas"] < 3.0:
            txt += "; investors are paid very little for default risk"
        ins.append({"tone": "warn" if us["oas"] < 3.0 else "info", "sec": "junk", "text": txt + "."})
    ccc, bb = by.get("CCC"), by.get("BB")
    if ccc and ccc.get("cover"):
        c = ccc["cover"]
        if c["times"] < 1:
            ins.append({"tone": "warn", "sec": "junk",
                        "text": f"CCC bonds yield {round(ccc['oas'] * 100)}bp over Treasuries, less than an average year of "
                                f"CCC defaults would cost ({c['loss']:.1f}pp at {round(p['recovery'] * 100)}% recovery): "
                                f"the bottom of junk is priced for a better than average year."})
        elif bb and bb.get("cover"):
            ins.append({"tone": "info", "sec": "junk",
                        "text": f"BB spreads cover an average year of BB defaults {bb['cover']['times']}×, CCC {c['times']}×."})
    d = p.get("dispersion") or {}
    if d.get("pct") is not None and d["pct"] >= 85 and bb and (bb.get("pct") or 0) < 85:
        ins.append({"tone": "warn", "sec": "junk",
                    "text": f"CCC spreads sit {round(d['gap'] * 100)}bp over BB, the {_ord(d['pct'])} percentile of three years, "
                            f"while BB is at its {_ord(bb['pct'])}: the stress is in the weakest borrowers, not junk as a whole."})
    v = p.get("vs") or {}
    if v.get("mood") == "credit":
        ins.append({"tone": "warn", "sec": "junk",
                    "text": f"Junk spreads are wide for the last three years ({_ord(v['hy_pct'])} percentile) while the S&P 500 sits "
                            f"{abs(v['spx_dd']):.1f}% from its high: credit is more worried than stocks."})
    elif v.get("mood") == "stocks":
        ins.append({"tone": "info", "sec": "junk",
                    "text": f"The S&P 500 is {abs(v['spx_dd']):.1f}% off its high, but junk spreads have not followed "
                            f"({_ord(v['hy_pct'])} percentile): the bond market sees less trouble than stocks do."})
    return ins


# --------------------------------------------------------------------------
# auctions
# --------------------------------------------------------------------------

TD = "https://www.treasurydirect.gov/TA_WS/securities"


def _load_auctions() -> dict:
    up = json.loads(net.get(f"{TD}/upcoming?format=json", timeout=25))
    done = []
    for typ in ("Note", "Bond", "TIPS"):
        try:
            done += json.loads(net.get(f"{TD}/auctioned?format=json&type={typ}&days=800",
                                       timeout=40))
        except Exception:
            continue
    keep = ("cusip", "securityType", "securityTerm", "auctionDate", "issueDate", "offeringAmount",
            "highYield", "highDiscountRate", "bidToCoverRatio", "indirectBidderAccepted",
            "directBidderAccepted", "primaryDealerAccepted", "competitiveAccepted",
            "totalAccepted", "reopening", "tips", "type", "announcementDate", "maturityDate",
            "floatingRate", "originalSecurityTerm")
    slim = lambda a: {k: a.get(k) for k in keep}
    return {"upcoming": [slim(a) for a in up], "done": [slim(a) for a in done],
            "fetched_at": time.time()}


def _term_key(a: dict) -> str:
    # A reopened 10-year is sold as a "9-Year 10-Month"; it is still the
    # 10-year auction, and its history is the 10-year's.
    t = a.get("originalSecurityTerm") or a.get("securityTerm") or ""
    return ("TIPS " if a.get("tips") == "Yes" else "") + ("FRN " if a.get("floatingRate") == "Yes" else "") + t


def part_auctions() -> dict:
    raw = _cached("bonds:auctions", TTL["auctions"], _load_auctions)
    today = date.today().isoformat()

    def share(a, k):
        acc = _f(a.get(k))
        comp = _f(a.get("competitiveAccepted"))
        return round(acc / comp * 100, 1) if (acc is not None and comp) else None

    done = []
    for a in raw.get("done") or []:
        if not a.get("bidToCoverRatio") or not a.get("auctionDate"):
            continue
        done.append({"d": a["auctionDate"][:10], "term": _term_key(a), "type": a.get("securityType"),
                     "reopen": a.get("reopening") == "Yes",
                     "yield": _f(a.get("highYield")), "btc": _f(a.get("bidToCoverRatio")),
                     "indirect": share(a, "indirectBidderAccepted"),
                     "direct": share(a, "directBidderAccepted"),
                     "dealer": share(a, "primaryDealerAccepted"),
                     "size_bn": round((_f(a.get("offeringAmount")) or 0) / 1e9, 1)})
    done.sort(key=lambda x: x["d"])
    by: dict[str, list] = {}
    for a in done:
        by.setdefault(a["term"], []).append(a)
    recent = []
    for a in reversed(done):
        if a["d"] < _ago(75):
            break
        prior = [x for x in by[a["term"]] if x["d"] < a["d"]][-6:]
        if prior:
            a["btc_avg"] = _r(statistics.mean(x["btc"] for x in prior if x["btc"]), 2)
            dl = [x["dealer"] for x in prior if x["dealer"] is not None]
            a["dealer_avg"] = _r(statistics.mean(dl), 1) if dl else None
            score = 0
            if a["btc"] and a["btc_avg"]:
                score += 1 if a["btc"] > a["btc_avg"] * 1.03 else -1 if a["btc"] < a["btc_avg"] * .97 else 0
            if a["dealer"] is not None and a.get("dealer_avg") is not None:
                score += 1 if a["dealer"] < a["dealer_avg"] - 2 else -1 if a["dealer"] > a["dealer_avg"] + 2 else 0
            a["grade"] = "strong" if score >= 1 else "weak" if score <= -1 else "average"
        recent.append(a)
    upcoming = sorted(
        ({"d": (a.get("auctionDate") or "")[:10], "term": _term_key(a), "type": a.get("securityType"),
          "size_bn": round((_f(a.get("offeringAmount")) or 0) / 1e9, 1) or None,
          "reopen": a.get("reopening") == "Yes", "announced": (a.get("announcementDate") or "")[:10]}
         for a in raw.get("upcoming") or [] if (a.get("auctionDate") or "")[:10] >= today),
        key=lambda x: x["d"])
    coupons = [u for u in upcoming if u["type"] in ("Note", "Bond", "TIPS")]
    bills = [u for u in upcoming if u["type"] == "Bill"]
    size_30 = sum(x["size_bn"] for x in done if x["d"] >= _ago(30))
    out = {"recent": recent[:24], "upcoming": coupons[:20],
           "bills": {"n": len(bills), "size_bn": round(sum(b["size_bn"] or 0 for b in bills), 1),
                     "next": bills[:6]},
           "coupon_30d_bn": round(size_30, 1), "fetched_at": time.time()}
    weak = [a for a in recent[:8] if a.get("grade") == "weak" and a["type"] in ("Note", "Bond")]
    out["insights"] = ([{"tone": "warn", "sec": "auctions",
                         "text": "Recent weak Treasury auctions: " + ", ".join(
                             f"{a['term']} on {a['d'][8:10]}/{a['d'][5:7]}" for a in weak[:3])
                         + " — dealers had to absorb more than usual, a sign of soft demand for duration."}]
                       if weak else [])
    return out


# --------------------------------------------------------------------------
# news and the calendar
# --------------------------------------------------------------------------

BOND_NEWS = re.compile(
    r"\b(treasur(y|ies)|yields?|bonds?|gilts?|bunds?|JGBs?|FOMC|Fed(eral Reserve)?|Powell|"
    r"rate (cut|hike)s?|ECB|Lagarde|BoE|Bank of England|BOJ|Bank of Japan|inflation|CPI|PCE|"
    r"payrolls?|jobs report|auction|deficit|debt ceiling|credit spread|term premium|"
    r"curve|duration|mortgage rates?)\b", re.I)

LISTICLE = re.compile(r"dividend|stocks? to (buy|watch)|^\d+ \w+ stocks|covered call|"
                      r"52-week (low|high)", re.I)

MACRO_WORDS = re.compile(r"CPI|PCE|payroll|employment|jobless|GDP|FOMC|Fed|ECB|interest rate|"
                         r"inflation|PPI|retail sales|ISM|PMI|auction|HICP|BoE|BoJ|Treasury", re.I)


TOPICS = [("Fed", r"\b(Fed|FOMC|Powell|Federal Reserve|rate (cut|hike))"),
          ("Inflation", r"\b(inflation|CPI|PCE|prices)\b"),
          ("Jobs", r"\b(payrolls?|jobs|unemployment|labor)\b"),
          ("Europe", r"\b(ECB|Lagarde|Bunds?|euro ?zone|gilts?|BoE|Bank of England|France|Italy)\b"),
          ("Japan", r"\b(BOJ|Bank of Japan|JGBs?|Japan)\b"),
          ("Fiscal", r"\b(deficit|debt|Treasury Department|Bessent|auction|supply|budget)\b"),
          ("Mortgages", r"\bmortgage"),
          ("Credit", r"\b(credit|junk|high.yield|spreads?)\b")]


def _load_news() -> dict:
    """
    Treasury-market headlines. Yahoo attaches news to the yield indices and
    the long-bond ETF, which is as close to a "bonds" feed as there is
    without a key; Finnhub's general feed and the brief's cache are filtered
    for the same words.
    """
    from . import news as newsmod
    items = newsmod._yahoo(["^TNX", "TLT", "^TYX", "ZN=F"], per=25)
    key = newsmod.api_key()
    if key:
        try:
            items += newsmod._finnhub_general(key, limit=80)
        except Exception:
            pass
    return {"items": items, "fetched_at": time.time()}


def part_news() -> dict:
    from . import news as newsmod
    pool = []
    try:
        pool += _cached("bonds:news", 900, _load_news).get("items") or []
    except Exception:
        pass
    n = store.cache_get("news") or {}
    pool += [x for x in (n.get("items") or []) if isinstance(x, dict)]
    items, seen = [], set()
    for it in sorted(pool, key=lambda x: -(x.get("ts") or 0)):
        text = f"{it.get('headline', '')} {it.get('summary', '')}"
        hits = BOND_NEWS.findall(text)
        # The headline itself must be about rates; "3 dividend stocks with a
        # 3% yield" mentions yield and is not bond news.
        if not hits or not BOND_NEWS.search(it.get("headline") or "") \
                or LISTICLE.search(it.get("headline") or ""):
            continue
        k1 = newsmod.url_key(it.get("url") or "")
        k2 = re.sub(r"\W+", " ", (it.get("headline") or "").lower())[:70]
        if k1 in seen or k2 in seen:
            continue
        seen.update((k1, k2))
        topics = [t for t, rx in TOPICS if re.search(rx, text, re.I)][:3]
        items.append({"headline": it.get("headline"), "source": it.get("source"),
                      "url": it.get("url"), "ts": it.get("ts"), "topics": topics,
                      "summary": (it.get("summary") or "")[:260], "hits": len(hits)})
    m = store.cache_get("macro") or {}
    events = []
    for d in m.get("days") or []:
        for ev in d.get("events") or []:
            name = ev.get("title") or ""
            if MACRO_WORDS.search(name) and d.get("date", "") >= date.today().isoformat():
                events.append({"d": d.get("date"), "name": name, "country": ev.get("currency"),
                               "time": ev.get("time"), "impact": ev.get("impact"),
                               "forecast": ev.get("forecast"), "prev": ev.get("previous")})
    from . import sentiment
    meetings = ([{"d": d, "name": "FOMC decision", "country": "US"} for d in sentiment.FOMC
                 if d >= date.today().isoformat()][:3] +
                [{"d": d, "name": "ECB decision", "country": "EA"} for d in sentiment.ECB_MEETINGS
                 if d >= date.today().isoformat()][:3])
    return {"items": items[:40], "events": events, "meetings": meetings,
            "news_age_s": round(store.cache_age("news")) if n else None,
            "fetched_at": time.time()}


def part_path() -> dict:
    """What futures price for every Fed meeting, and for the ECB by quarter."""
    from . import ratepath
    return ratepath.part()


PARTS = {"us": part_us, "inflation": part_inflation, "stocks": part_stocks,
         "portfolio": part_portfolio, "europe": part_europe, "global": part_global,
         "credit": part_credit, "junk": part_junk, "auctions": part_auctions, "news": part_news,
         "path": part_path}


def part(name: str) -> dict:
    fn = PARTS.get(name)
    if not fn:
        raise KeyError(name)
    return fn()


def sources() -> dict:
    """What answered, for the Sources panel."""
    out = {}
    for k, lab in (("bonds:us", "FRED (curve since 1990, real yields, credit)"),
                   ("bonds:ust", "U.S. Treasury (same-day curve)"),
                   ("bonds:monthly", "FRED monthly (CPI, PCE, HICP, OECD yields)"),
                   ("bonds:euro", "ECB · Bundesbank · Bank of England · Japan MOF"),
                   ("bonds:auctions", "TreasuryDirect (auctions)"),
                   ("bonds:junkpx", "Yahoo (HYG and SPY since 2007, for junk against stocks)"),
                   ("bonds:mkt", "Yahoo (S&P 500, sectors, MOVE)"),
                   ("bonds:live", "Yahoo (intraday yields)"),
                   ("ratepath:zq", "Yahoo (fed funds futures, every month)"),
                   ("ratepath:esr", "Yahoo (three-month euro short-term rate futures)")):
        v = store.cache_get(k)
        row = {"label": lab, "ok": bool(v), "age_s": round(store.cache_age(k)) if v else None}
        if isinstance(v, dict):
            if v.get("missing"):
                row["missing"] = v["missing"][:12]
            if v.get("carried"):
                row["carried"] = v["carried"][:12]
            if v.get("status"):
                row["status"] = v["status"]
        out[k] = row
    return out
