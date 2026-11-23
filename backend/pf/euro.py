"""
EUR — the euro: what it is worth, what moves it, and what it does to a book
that is mostly in dollars.

For a euro investor in American shares the exchange rate is one more holding,
and a big one: four fifths of this book is priced in dollars, so a cent on
EUR/USD moves its euro value as much as a typical share does. This page is
the currency, its drivers, and the book's side of it.

  THE PRICE      EUR/USD since 1999 from the ECB's daily reference rate (set
                 at 14:15 Frankfurt), with Yahoo's quote for today; the euro
                 against thirty currencies; the trade-weighted euro (EER-40),
                 nominal daily and deflated by consumer prices monthly.

  WHAT MOVES IT
    rate gap     the two-year US Treasury against the two-year Bund
                 (Bundesbank). Money goes where it is paid more, so a wider
                 gap pulls the dollar up. EUR/USD is fitted on the gap over
                 the last three years, week by week, for the rate the gap
                 alone would set.
    the path     what futures price for the Fed and the ECB over the next
                 year (ratepath): whether the gap is expected to narrow.
    real rates   each central bank's rate less its own core inflation.
    fair value   purchasing power: the World Bank's PPP for Germany and the
                 Netherlands, and The Economist's Big Mac index (CC BY 4.0).
                 Currencies stay far from PPP for years; an anchor, not a
                 forecast. The real effective rate against its own average
                 since 1999 says the same against forty partners.
    positioning  the CFTC's weekly Traders in Financial Futures report on
                 CME's euro future: what asset managers and leveraged funds
                 hold, against their own last three years.
    the options  what FXE's options imply (the euro trust's, in dollars) for
                 one month to a year: the range, and whether a fall of the
                 euro costs more to insure than a rise.

  YOUR BOOK      how much is in dollars; what a 1, 5 or 10% move does in
                 euros; what the currency has added or taken so far; what
                 hedging the dollar costs now (the gap between the two
                 three-month rates) and what it would have done for the S&P
                 500 month by month since 1999.

Every source is fetched on its own and fails alone. The ECB is asked for one
series at a time or a plain wildcard: a request listing codes that do not
exist is answered with a block page.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import math
import statistics
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

from . import net, store

log = logging.getLogger(__name__)

SYM = "EURUSD=X"
TTL = {"ref": 6 * 3600, "eer": 12 * 3600, "cot": 12 * 3600, "ppp": 7 * 86400, "hedge": 24 * 3600,
       "view": 1800}
FIT_WEEKS = 156             # the rate-gap fit: three years of Fridays
MOVE_ALERT = 1.0            # a day's move in EUR/USD worth an alert, %
GAP_ALERT = 25              # the two-year gap moving this many basis points in a month

# The currencies the ECB fixes, in the order a euro investor meets them.
NAMES = {
    "USD": "US dollar", "GBP": "British pound", "CHF": "Swiss franc", "JPY": "Japanese yen",
    "CNY": "Chinese yuan", "SEK": "Swedish krona", "NOK": "Norwegian krone", "DKK": "Danish krone",
    "PLN": "Polish zloty", "CZK": "Czech koruna", "HUF": "Hungarian forint", "RON": "Romanian leu",
    "BGN": "Bulgarian lev", "ISK": "Icelandic krona", "TRY": "Turkish lira", "CAD": "Canadian dollar",
    "AUD": "Australian dollar", "NZD": "New Zealand dollar", "HKD": "Hong Kong dollar",
    "SGD": "Singapore dollar", "KRW": "South Korean won", "INR": "Indian rupee", "IDR": "Indonesian rupiah",
    "MYR": "Malaysian ringgit", "PHP": "Philippine peso", "THB": "Thai baht", "ZAR": "South African rand",
    "BRL": "Brazilian real", "MXN": "Mexican peso", "ILS": "Israeli shekel",
}
GROUP = {"USD": "Majors", "GBP": "Majors", "CHF": "Majors", "JPY": "Majors", "CNY": "Majors",
         "CAD": "Majors", "AUD": "Majors",
         "SEK": "Europe", "NOK": "Europe", "DKK": "Europe", "PLN": "Europe", "CZK": "Europe",
         "HUF": "Europe", "RON": "Europe", "BGN": "Europe", "ISK": "Europe", "TRY": "Europe"}

# Moments on the chart. Dates only; the levels are read off the data.
EVENTS = [
    ("1999-01-04", "the euro begins"),
    ("2008-09-15", "Lehman Brothers fails"),
    ("2012-07-26", "Draghi: “whatever it takes”"),
    ("2015-01-22", "the ECB starts buying bonds"),
    ("2020-03-11", "the pandemic is declared"),
    ("2022-02-24", "Russia invades Ukraine"),
]

ECB = "https://data-api.ecb.europa.eu/service/data"
CFTC = ("https://publicreporting.cftc.gov/resource/gpe5-46if.json?"
        "$select=report_date_as_yyyy_mm_dd,open_interest_all,asset_mgr_positions_long,asset_mgr_positions_short,"
        "lev_money_positions_long,lev_money_positions_short,dealer_positions_long_all,dealer_positions_short_all"
        "&cftc_contract_market_code=099741&$order=report_date_as_yyyy_mm_dd&$limit=5000")
WB_PPP = "https://api.worldbank.org/v2/country/DEU;NLD/indicator/PA.NUS.PPP?format=json&date=1999:2030&per_page=500"
BIGMAC = "https://raw.githubusercontent.com/TheEconomist/big-mac-data/master/output-data/big-mac-full-index.csv"


def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _ago(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def _cached(k: str, ttl: float, fn):
    from . import bonds
    return bonds._cached(k, ttl, fn)


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _ecb(path: str, start: str) -> list[dict]:
    raw = net.get(f"{ECB}/{path}?startPeriod={start}&format=csvdata&detail=dataonly", timeout=40)
    text = raw.decode("utf-8", "replace")
    if text.lstrip().startswith("<"):
        raise RuntimeError("the ECB answered with a web page, not data")
    return list(csv.DictReader(io.StringIO(text)))


def _load_ref() -> dict:
    """Every reference rate for three years, and EUR/USD since 1999."""
    from . import bonds
    out: dict[str, dict] = {}
    for r in _ecb("EXR/D..EUR.SP00.A", _ago(3 * 366 + 10)):
        v = _f(r.get("OBS_VALUE"))
        if v is not None and r.get("CURRENCY"):
            out.setdefault(r["CURRENCY"], {})[r["TIME_PERIOD"][:10]] = v
    usd = {}
    for r in _ecb("EXR/D.USD.EUR.SP00.A", "1999-01-01"):
        v = _f(r.get("OBS_VALUE"))
        if v is not None:
            usd[r["TIME_PERIOD"][:10]] = v
    if len(usd) < 1000:
        raise RuntimeError("the ECB returned no EUR/USD history")
    out["USD_LONG"] = usd
    return {**bonds._pack(out), "fetched_at": time.time()}


def _load_eer() -> dict:
    """The euro against forty trading partners: nominal daily, real (consumer prices) monthly."""
    from . import bonds
    nom, real = {}, {}
    for r in _ecb("EXR/D.E03.EUR.EN00.A", "1999-01-01"):
        v = _f(r.get("OBS_VALUE"))
        if v is not None:
            nom[r["TIME_PERIOD"][:10]] = v
    for r in _ecb("EXR/M.E03.EUR.ERC0.A", "1999-01"):
        v = _f(r.get("OBS_VALUE"))
        if v is not None:
            real[r["TIME_PERIOD"][:7] + "-01"] = v
    if not nom and not real:
        raise RuntimeError("the ECB returned no effective exchange rate")
    return {**bonds._pack({"EER_NOM": nom, "EER_REAL": real}), "fetched_at": time.time()}


def parse_cot(rows: list[dict]) -> list[dict]:
    """The CFTC's rows as weeks: each group's net contracts and its share of open interest."""
    out = []
    for r in rows:
        d = (r.get("report_date_as_yyyy_mm_dd") or "")[:10]
        oi = _f(r.get("open_interest_all"))
        if not d or not oi:
            continue
        net_ = lambda a, b: (_f(r.get(a)) or 0.0) - (_f(r.get(b)) or 0.0)
        am = net_("asset_mgr_positions_long", "asset_mgr_positions_short")
        lf = net_("lev_money_positions_long", "lev_money_positions_short")
        dl = net_("dealer_positions_long_all", "dealer_positions_short_all")
        out.append({"d": d, "oi": oi, "am": am, "lf": lf, "dealer": dl,
                    "am_pct": round(am / oi * 100, 1), "lf_pct": round(lf / oi * 100, 1)})
    out.sort(key=lambda x: x["d"])
    return out


def _load_cot() -> dict:
    rows = json.loads(net.get(CFTC, timeout=40))
    weeks = parse_cot(rows)
    if len(weeks) < 20:
        raise RuntimeError("the CFTC returned too few weeks for the euro future")
    return {"weeks": weeks, "fetched_at": time.time()}


def _load_ppp() -> dict:
    """PPP from the World Bank, the Big Mac index from The Economist; each may fail alone."""
    out: dict = {"fetched_at": time.time()}
    try:
        js = json.loads(net.get(WB_PPP, timeout=30))
        ppp: dict[str, dict] = {}
        for r in (js[1] if isinstance(js, list) and len(js) > 1 else []) or []:
            v = _f(r.get("value"))
            if v:
                ppp.setdefault(r["countryiso3code"], {})[r["date"]] = v
        out["ppp"] = ppp
    except Exception as e:
        out["ppp_error"] = f"{e.__class__.__name__}: {e}"[:200]
    try:
        text = net.get(BIGMAC, timeout=30).decode("utf-8", "replace")
        out["bigmac"] = [{"d": r["date"], "price": _f(r.get("local_price")), "usd_price": _f(r.get("dollar_price")),
                          "ex": _f(r.get("dollar_ex")), "raw": _f(r.get("USD_raw")), "adj": _f(r.get("USD_adjusted"))}
                         for r in csv.DictReader(io.StringIO(text)) if r.get("iso_a3") == "EUZ"]
    except Exception as e:
        out["bigmac_error"] = f"{e.__class__.__name__}: {e}"[:200]
    if not out.get("ppp") and not out.get("bigmac"):
        raise RuntimeError(out.get("ppp_error") or out.get("bigmac_error") or "no fair-value source answered")
    return out


def _load_hedge() -> dict:
    """The S&P 500 with dividends, monthly, and the euro's three-month rate since 1999."""
    from . import bonds
    data = bonds._yf().download(["^SP500TR"], period="max", interval="1mo", auto_adjust=True,
                                progress=False, group_by="ticker", threads=False)
    spx = bonds._closes(data, "^SP500TR", False)
    eur3 = {}
    for r in _ecb("FM/M.U2.EUR.RT.MM.EURIBOR3MD_.HSTA", "1999-01"):
        v = _f(r.get("OBS_VALUE"))
        if v is not None:
            eur3[r["TIME_PERIOD"][:7]] = v
    if len(spx) < 120 or len(eur3) < 120:
        raise RuntimeError("too little history for the hedging comparison")
    return {"spx": spx, "eur3": eur3, "fetched_at": time.time()}


def ref_series() -> dict:
    from . import bonds
    return bonds._unpack(_cached("eur:ref", TTL["ref"], _load_ref))


# --------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------

def on_or_before(m: dict, day: str, keys: Optional[list] = None) -> Optional[tuple]:
    ks = keys if keys is not None else sorted(m)
    import bisect
    i = bisect.bisect_right(ks, day) - 1
    return (ks[i], m[ks[i]]) if i >= 0 else None


def change(m: dict, days: int, today: Optional[str] = None) -> Optional[float]:
    """Percent change of a daily series over `days` calendar days, to its last value or `today`'s."""
    ks = sorted(m)
    if not ks:
        return None
    end = on_or_before(m, today, ks) if today else (ks[-1], m[ks[-1]])
    if not end:
        return None
    start = on_or_before(m, (date.fromisoformat(end[0]) - timedelta(days=days)).isoformat(), ks)
    if not start or start[1] == 0:
        return None
    return (end[1] / start[1] - 1) * 100


def ytd(m: dict) -> Optional[float]:
    ks = sorted(m)
    if not ks:
        return None
    y0 = on_or_before(m, f"{ks[-1][:4]}-01-01", ks)
    if not y0 or not y0[1] or y0[0] == ks[-1]:
        return None
    return (m[ks[-1]] / y0[1] - 1) * 100


def weekly(m: dict, keys: Optional[list] = None) -> dict:
    """The last value of each ISO week, keyed by that day."""
    out = {}
    for d in sorted(m) if keys is None else keys:
        y, w, _ = date.fromisoformat(d).isocalendar()
        out[(y, w)] = d
    return {d: m[d] for d in out.values()}


def ols(x: list, y: list) -> Optional[dict]:
    n = len(x)
    if n < 20:
        return None
    mx, my = statistics.fmean(x), statistics.fmean(y)
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx <= 0:
        return None
    b = sum((a - mx) * (c - my) for a, c in zip(x, y)) / sxx
    a = my - b * mx
    res = [c - (a + b * v) for v, c in zip(x, y)]
    tot = sum((c - my) ** 2 for c in y)
    return {"a": a, "b": b, "r2": 1 - sum(e * e for e in res) / tot if tot > 0 else 0.0,
            "sd": statistics.pstdev(res), "n": n}


def gap_fit(fx: dict, us2: dict, de2: dict, weeks: int = FIT_WEEKS) -> Optional[dict]:
    """
    EUR/USD on the two-year gap (US minus German, in points), weekly, over the
    last `weeks`: the level the gap alone would set today, and how far the
    euro sits from it.
    """
    days = sorted(set(fx) & set(us2) & set(de2))
    wk = sorted(weekly({d: 1 for d in days}))[-weeks:]
    if len(wk) < 52:
        return None
    x = [us2[d] - de2[d] for d in wk]
    y = [fx[d] for d in wk]
    f = ols(x, y)
    if not f:
        return None
    gap_now = x[-1]
    fair = f["a"] + f["b"] * gap_now
    return {"b": round(f["b"], 4), "a": round(f["a"], 4), "r2": round(f["r2"], 2), "n": f["n"],
            "from": wk[0], "to": wk[-1], "gap": round(gap_now, 2), "fair": round(fair, 4),
            "fx": round(y[-1], 4), "gap_pct": round((y[-1] / fair - 1) * 100, 1), "sd": round(f["sd"], 4),
            "per_point": round(f["b"] / statistics.fmean(y) * 100, 1),
            "pts": [[round(a, 3), round(c, 4)] for a, c in zip(x, y)]}


def sensitivity(usd_eur: float, moves=(-10, -5, -1, 1, 5, 10)) -> list[dict]:
    """What a move in EUR/USD does to the euro value of dollar holdings: V × (1/(1+m) − 1)."""
    return [{"move": m, "eur": round(usd_eur * (1 / (1 + m / 100) - 1), 2)} for m in moves]


def hedge_history(spx: dict, fx: dict, eur3: dict, usd3: dict, since: str = "1999-01") -> Optional[dict]:
    """
    The S&P 500 with dividends, held from a euro account month by month:
    unhedged, it earns the index and the dollar's move; hedged, it earns the
    index plus the gap between the euro's and the dollar's three-month rates
    (covered interest parity: selling dollars forward costs what the dollar
    pays over the euro). Monthly closes; FX at the last ECB fixing of the month.
    """
    months = sorted({d[:7] for d in spx if d[:7] >= since})
    sp = {}
    for d in sorted(spx):
        sp[d[:7]] = spx[d]                          # Yahoo's monthly bars carry the month's first day
    fxk = sorted(fx)
    fxm = {}
    for mth in months:
        y, mm = int(mth[:4]), int(mth[5:7])
        end = (date(y + (mm == 12), mm % 12 + 1, 1) - timedelta(days=1)).isoformat()
        hit = on_or_before(fx, end, fxk)
        if hit and hit[0][:7] == mth:
            fxm[mth] = hit[1]
    rows, un, hd = [], 1.0, 1.0
    prev = None
    for mth in months:
        if mth not in sp or mth not in fxm or mth not in eur3 or mth not in usd3:
            prev = None
            continue
        if prev:
            r_usd = sp[mth] / sp[prev] - 1
            r_un = (1 + r_usd) * fxm[prev] / fxm[mth] - 1
            r_hd = r_usd + (eur3[prev] - usd3[prev]) / 1200
            un *= 1 + r_un
            hd *= 1 + r_hd
            rows.append({"m": mth, "un": r_un, "hd": r_hd, "U": un, "Hd": hd})
        prev = mth
    if len(rows) < 60:
        return None

    def stats(key):
        rs = [r[key] for r in rows]
        yrs = len(rs) / 12
        g = (rows[-1]["U" if key == "un" else "Hd"]) ** (1 / yrs) - 1
        vol = statistics.pstdev(rs) * math.sqrt(12)
        peak, worst, lvl = 1.0, 0.0, 1.0
        pk_m, at = rows[0]["m"], (None, None)
        for row, r in zip(rows, rs):
            lvl *= 1 + r
            if lvl > peak:
                peak, pk_m = lvl, row["m"]
            if lvl / peak - 1 < worst:
                worst, at = lvl / peak - 1, (pk_m, row["m"])
        return {"cagr": round(g * 100, 2), "vol": round(vol * 100, 1), "worst": round(worst * 100, 1),
                "worst_from": at[0], "worst_to": at[1]}

    years = {}
    for r in rows:
        y = r["m"][:4]
        u, h = years.get(y, (1.0, 1.0))
        years[y] = (u * (1 + r["un"]), h * (1 + r["hd"]))
    return {"from": rows[0]["m"], "to": rows[-1]["m"], "months": len(rows),
            "unhedged": stats("un"), "hedged": stats("hd"),
            "series": [[r["m"] + "-01", round(r["U"], 4), round(r["Hd"], 4)] for r in rows],
            "years": [{"y": y, "un": round((u - 1) * 100, 1), "hd": round((h - 1) * 100, 1)}
                      for y, (u, h) in sorted(years.items())]}


def pctile(vals: list, x: float) -> Optional[int]:
    v = [a for a in vals if a is not None]
    if not v or x is None:
        return None
    return round(sum(1 for a in v if a <= x) / len(v) * 100)


def _down(items: list, n: int = 1400) -> list:
    if len(items) <= n:
        return items
    step = len(items) / n
    out = [items[int(i * step)] for i in range(n)]
    if out[-1] != items[-1]:
        out.append(items[-1])
    return out


# --------------------------------------------------------------------------
# the parts
# --------------------------------------------------------------------------

def _quote() -> Optional[dict]:
    from . import pricing
    try:
        q = pricing.get_quotes([SYM]).get(SYM)
    except Exception:
        q = None
    if q and q.get("price"):
        return {"v": q["price"], "prev": q.get("prev_close"), "asof": q.get("asof"), "src": "Yahoo"}
    return None


def part_price(ref: dict) -> dict:
    usd = ref.get("USD_LONG") or {}
    ks = sorted(usd)
    q = _quote()
    last_d, last_v = ks[-1], usd[ks[-1]]
    now = q["v"] if q else last_v
    hi_d = max(ks, key=lambda d: usd[d])
    lo_d = min(ks, key=lambda d: usd[d])
    yr = [usd[d] for d in ks if d >= _ago(365)]
    first_par = next((d for d in ks if d >= "2022-01-01" and usd[d] < 1.0), None)
    ev = [{"d": d, "label": t, "v": round(on_or_before(usd, d, ks)[1], 4)} for d, t in EVENTS
          if on_or_before(usd, d, ks)]
    ev += [{"d": hi_d, "label": "the high", "v": usd[hi_d]}, {"d": lo_d, "label": "the low", "v": usd[lo_d]}]
    if first_par:
        ev.append({"d": first_par, "label": "below parity", "v": usd[first_par]})
    ev.sort(key=lambda e: e["d"])
    return {
        "now": round(now, 4), "now_src": q["src"] if q else "ECB", "now_asof": (q or {}).get("asof") or last_d,
        "day": round((q["v"] / q["prev"] - 1) * 100, 2) if q and q.get("prev") else None,
        "ecb": round(last_v, 4), "ecb_asof": last_d,
        "w1": change(usd, 7), "m1": change(usd, 30), "m3": change(usd, 91), "ytd": ytd(usd),
        "y1": change(usd, 365), "y5": change(usd, 1826),
        "hi": {"d": hi_d, "v": usd[hi_d]}, "lo": {"d": lo_d, "v": usd[lo_d]},
        "y_hi": max(yr), "y_lo": min(yr),
        "pctile": pctile([usd[d] for d in ks], now),
        "avg": round(statistics.fmean(usd[d] for d in ks), 4),
        "series": _down([[d, usd[d]] for d in ks], 2000), "events": ev,
    }


# Currencies the ECB no longer fixes because their country adopted the euro.
JOINED = {"BGN": "2026-01-01"}


def part_crosses(ref: dict) -> dict:
    out, gone = [], []
    usd = ref.get("USD") or {}
    latest = max(usd) if usd else date.today().isoformat()
    stale = (date.fromisoformat(latest) - timedelta(days=14)).isoformat()
    for c, name in NAMES.items():
        m = ref.get(c) or {}
        if len(m) < 30:
            continue
        ks = sorted(m)
        if ks[-1] < stale:
            gone.append({"c": c, "name": name, "last": ks[-1], "v": m[ks[-1]], "joined": JOINED.get(c)})
            continue
        yr = [m[d] for d in ks if d >= _ago(365)]
        lo, hi = min(yr), max(yr)
        out.append({"c": c, "name": name, "group": GROUP.get(c, "Rest of the world"), "v": m[ks[-1]],
                    "asof": ks[-1], "w1": change(m, 7), "m1": change(m, 30), "ytd": ytd(m), "y1": change(m, 365),
                    "y3": change(m, 3 * 365),
                    "pos": round((m[ks[-1]] - lo) / (hi - lo) * 100) if hi > lo else None,
                    "spark": [round(m[d], 6) for d in ks[-260::5]]})
    return {"rows": out, "gone": gone}


def part_eer() -> dict:
    from . import bonds
    e = bonds._unpack(_cached("eur:eer", TTL["eer"], _load_eer))
    nom, real = e.get("EER_NOM") or {}, e.get("EER_REAL") or {}
    out = {}
    if nom:
        ks = sorted(nom)
        out["nominal"] = {"v": nom[ks[-1]], "asof": ks[-1], "y1": change(nom, 365), "m1": change(nom, 30),
                          "pctile": pctile(list(nom.values()), nom[ks[-1]]),
                          "series": _down([[d, nom[d]] for d in ks], 1200)}
    if real:
        ks = sorted(real)
        avg = statistics.fmean(real.values())
        out["real"] = {"v": real[ks[-1]], "asof": ks[-1], "avg": round(avg, 2),
                       "vs_avg": round((real[ks[-1]] / avg - 1) * 100, 1),
                       "pctile": pctile(list(real.values()), real[ks[-1]]),
                       "series": [[d, real[d]] for d in ks]}
    return out


def part_rates(fx: dict) -> dict:
    """The gaps between the two economies' rates, and the fit of the euro on the two-year one."""
    from . import bonds
    us = bonds.us_series()
    eu, _ = bonds.euro_series()
    us2, de2 = us.get("DGS2") or {}, eu.get("BUND2") or {}
    us10, de10 = us.get("DGS10") or {}, eu.get("BUND10") or {}
    out = {}
    both2 = sorted(set(us2) & set(de2))
    if both2:
        g = {d: us2[d] - de2[d] for d in both2}
        ks = sorted(g)
        now = g[ks[-1]]
        m1 = on_or_before(g, (date.fromisoformat(ks[-1]) - timedelta(days=30)).isoformat(), ks)
        y1 = on_or_before(g, (date.fromisoformat(ks[-1]) - timedelta(days=365)).isoformat(), ks)
        out["gap2"] = {"v": round(now, 2), "asof": ks[-1], "us": us2[ks[-1]], "de": de2[ks[-1]],
                       "m1_bp": round((now - m1[1]) * 100) if m1 else None,
                       "y1_bp": round((now - y1[1]) * 100) if y1 else None,
                       "series": _down([[d, round(g[d], 3)] for d in ks if d >= "2005-01-01"], 1400)}
    both10 = sorted(set(us10) & set(de10))
    if both10:
        d = both10[-1]
        out["gap10"] = {"v": round(us10[d] - de10[d], 2), "asof": d, "us": us10[d], "de": de10[d]}
    fit = gap_fit(fx, us2, de2)
    if fit:
        out["fit"] = fit
    # real policy rates: each bank's rate less its own core inflation
    try:
        mo = bonds.monthly_series()
        def yoy(m):
            ks = sorted(m)
            if len(ks) < 13:
                return None
            return ks[-1], (m[ks[-1]] / m[ks[-13]] - 1) * 100
        uc, ec = yoy(mo.get("CPILFESL") or {}), yoy(mo.get("00XEFDEZ19M086NEST") or {})
        ff = bonds.Ser(us.get("DFF")).last()
        dfr = bonds.Ser(eu.get("ECB_DFR")).last()
        if uc and ec and ff[1] is not None and dfr[1] is not None:
            out["real"] = {"us": round(ff[1] - uc[1], 2), "ea": round(dfr[1] - ec[1], 2),
                           "us_rate": ff[1], "ea_rate": dfr[1], "us_core": round(uc[1], 2), "ea_core": round(ec[1], 2),
                           "us_month": uc[0], "ea_month": ec[0]}
            out["real"]["gap"] = round(out["real"]["us"] - out["real"]["ea"], 2)
    except Exception as e:
        out["real_error"] = f"{e.__class__.__name__}: {e}"[:200]
    return out


def part_path() -> dict:
    """What futures price for each bank about a year out, and so for the gap between them."""
    from . import ratepath
    p = ratepath.part()
    out = {}
    fed, ecb = p.get("fed"), p.get("ecb")
    horizon = (date.today() + timedelta(days=365)).isoformat()
    if fed and fed.get("meetings"):
        now = fed["now"]["rate"]
        ms = [m for m in fed["meetings"] if m["date"] <= horizon] or fed["meetings"][:1]
        end = ms[-1]
        out["fed"] = {"now": round(now, 3), "then": round(end["after"], 3), "by": end["date"],
                      "bp": round((end["after"] - now) * 100), "next": fed["meetings"][0]}
    if ecb and ecb.get("quarters"):
        qs = ecb["quarters"]
        # each quarter's row is the deposit rate expected on average over it
        last = [q for q in qs if q["to"] <= horizon] or qs[:1]
        q = last[-1]
        out["ecb"] = {"now": ecb["dfr"], "then": round(q["rate"], 3), "by": q["to"],
                      "bp": round((q["rate"] - ecb["dfr"]) * 100), "next": ecb.get("next_meeting")}
    if out.get("fed") and out.get("ecb"):
        out["gap_bp"] = out["fed"]["bp"] - out["ecb"]["bp"]
    out["insights"] = p.get("insights") or []
    return out


def part_ppp(fx_now: float) -> dict:
    p = _cached("eur:ppp", TTL["ppp"], _load_ppp)
    out = {}
    for iso, name in (("DEU", "Germany"), ("NLD", "the Netherlands")):
        m = (p.get("ppp") or {}).get(iso) or {}
        if m:
            y = max(m)
            out.setdefault("ppp", []).append({"iso": iso, "name": name, "year": y, "lcu": m[y],
                                              "eurusd": round(1 / m[y], 4),
                                              "gap_pct": round((fx_now * m[y] - 1) * 100, 1),
                                              "series": [[f"{k}-07-01", round(1 / v, 4)] for k, v in sorted(m.items())]})
    bm = p.get("bigmac") or []
    if bm:
        b = bm[-1]
        usd_ex = 1 / b["ex"] if b.get("ex") else None
        out["bigmac"] = {"d": b["d"], "price": b["price"], "usd_price": b["usd_price"],
                         "raw_pct": round(b["raw"] * 100, 1) if b.get("raw") is not None else None,
                         "adj_pct": round(b["adj"] * 100, 1) if b.get("adj") is not None else None,
                         "fx_then": round(usd_ex, 4) if usd_ex else None,
                         "implied": round(usd_ex / (1 + b["raw"]), 4) if usd_ex and b.get("raw") is not None else None,
                         "implied_adj": round(usd_ex / (1 + b["adj"]), 4) if usd_ex and b.get("adj") is not None else None,
                         "series": [[x["d"], round(x["raw"] * 100, 1)] for x in bm if x.get("raw") is not None]}
    for k in ("ppp_error", "bigmac_error"):
        if p.get(k):
            out[k] = p[k]
    return out


def part_cot() -> dict:
    w = _cached("eur:cot", TTL["cot"], _load_cot)["weeks"]
    last = w[-1]
    three = [x for x in w if x["d"] >= (date.fromisoformat(last["d"]) - timedelta(days=3 * 365)).isoformat()]
    prev = w[-2] if len(w) > 1 else None
    return {"d": last["d"], "oi": last["oi"], "am": last["am"], "lf": last["lf"], "dealer": last["dealer"],
            "am_pct": last["am_pct"], "lf_pct": last["lf_pct"],
            "am_w": last["am"] - prev["am"] if prev else None, "lf_w": last["lf"] - prev["lf"] if prev else None,
            "am_rank": pctile([x["am"] for x in three], last["am"]), "lf_rank": pctile([x["lf"] for x in three], last["lf"]),
            "series": [[x["d"], x["am_pct"], x["lf_pct"]] for x in w if x["d"] >= "2010-01-01"],
            "contract_eur": 125000}


def part_book(fx_now: float) -> dict:
    from . import service
    d = service.dashboard("SP500", light=True)
    if d.get("empty"):
        return {"available": False}
    T = d.get("totals") or {}
    by: dict[str, float] = {}
    fxp = []
    for p in d.get("positions", []):
        v = p.get("value_eur") or 0.0
        if v <= 0 or p.get("excluded"):
            continue
        c = p.get("currency") or "EUR"
        by[c] = by.get(c, 0.0) + v
        if c != "EUR" and p.get("unreal_fx_eur") is not None:
            fxp.append({"t": p.get("ticker"), "name": p.get("name"), "fx": p["unreal_fx_eur"], "v": v})
    tot = sum(by.values()) or 1.0
    usd = by.get("USD", 0.0)
    unreal_fx = sum(x["fx"] for x in fxp)
    fxp.sort(key=lambda x: x["fx"])
    return {"available": True, "total": round(tot, 2),
            "ccy": [{"c": c, "v": round(v, 2), "pct": round(v / tot * 100, 1)} for c, v in sorted(by.items(), key=lambda kv: -kv[1])],
            "usd": round(usd, 2), "usd_pct": round(usd / tot * 100, 1),
            "cent": round(usd * (1 / (1 + 0.01 / fx_now) - 1), 2),
            "sens": sensitivity(usd),
            "day_fx": T.get("day_fx_eur"), "unreal_fx": round(unreal_fx, 2), "realised_fx": T.get("realised_fx_eur"),
            "worst": fxp[:3], "best": fxp[-3:][::-1]}


def part_hedge(fx: dict) -> dict:
    from . import bonds
    h = _cached("eur:hedge", TTL["hedge"], _load_hedge)
    us = bonds.us_series()
    d3 = us.get("DGS3MO") or {}
    usd3: dict[str, list] = {}
    for d, v in d3.items():
        usd3.setdefault(d[:7], []).append(v)
    usd3m = {k: statistics.fmean(v) for k, v in usd3.items()}
    out = hedge_history(h["spx"], fx, h["eur3"], usd3m) or {}
    eu, _ = bonds.euro_series()
    estr = bonds.Ser(eu.get("ESTR")).last()
    us3 = bonds.Ser(d3).last()
    eur3_last = h["eur3"][max(h["eur3"])] if h["eur3"] else None
    out["now"] = {"us3": us3[1], "us3_asof": us3[0], "eur3": eur3_last, "eur3_month": max(h["eur3"]) if h["eur3"] else None,
                  "estr": estr[1], "cost": round(us3[1] - eur3_last, 2) if us3[1] is not None and eur3_last is not None else None}
    return out


def part_calendar() -> dict:
    """The next decisions and this week's releases that move the pair."""
    from . import sentiment, calendar
    out = {"ecb": sentiment.next_meeting(sentiment.ECB_MEETINGS), "fed": sentiment.next_meeting(sentiment.FOMC),
           "as_of": sentiment.MEETINGS_AS_OF}
    try:
        ev = store.cache_get("macro:log") or {}
        evs = [e for e in (ev.get("events") or {}).values()
               if e.get("currency") in ("EUR", "USD") and (e.get("rank") or 0) >= 3 and e.get("date", "") >= date.today().isoformat()]
        evs.sort(key=lambda e: (e["date"], e.get("time") or "99:99"))
        out["week"] = evs[:12]
    except Exception:
        out["week"] = []
    return out


def _run(parts: dict, name: str, fn):
    try:
        parts[name] = fn()
    except Exception as e:
        parts[name] = None
        parts[name + "_error"] = f"{e.__class__.__name__}: {e}"[:240]


def build() -> dict:
    t0 = time.time()
    ref = ref_series()
    usd = ref.get("USD_LONG") or {}
    out: dict = {"available": True}
    out["price"] = part_price(ref)
    fx_now = out["price"]["now"]
    _run(out, "crosses", lambda: part_crosses(ref))
    for name, fn in (("eer", part_eer), ("rates", lambda: part_rates(usd)), ("path", part_path),
                     ("ppp", lambda: part_ppp(fx_now)), ("cot", part_cot), ("book", lambda: part_book(fx_now)),
                     ("hedge", lambda: part_hedge(usd)), ("calendar", part_calendar)):
        _run(out, name, fn)
    out["say"] = _say(out)
    out["took_s"] = round(time.time() - t0, 1)
    out["built_at"] = time.time()
    try:
        store.cache_put("eur:alert", alert_snapshot(out))
    except Exception:
        pass
    return out


VIEW_SERVE = 8 * 3600       # an older page than this is rebuilt while the visitor waits
_REBUILD = {"t": None}


def _rebuild_behind():
    t = _REBUILD["t"]
    if t and t.is_alive():
        return
    t = threading.Thread(target=lambda: store.cache_through("eur:view", TTL["view"], build),
                         name="euro-rebuild", daemon=True)
    _REBUILD["t"] = t
    t.start()


def view(force: bool = False) -> dict:
    """
    The page. A copy past its half hour is served as it is while a new one is
    built behind it: the build reads the ECB's daily effective rate since 1999,
    and on 14 November 2026 the first visit after the half hour waited 32
    seconds for it. Past eight hours, or by another build, it is rebuilt first.
    """
    if force:
        return store.cache_through("eur:view", 0, build)
    from . import BUILD
    hit = store.cache_get("eur:view")
    if isinstance(hit, dict) and hit.get("_build") == BUILD and hit.get("_age_s", 1e9) <= VIEW_SERVE:
        if hit["_age_s"] > TTL["view"]:
            _rebuild_behind()
        return hit
    return store.cache_through("eur:view", TTL["view"], build)


# --------------------------------------------------------------------------
# what the options say: FXE, the euro trust, in dollars
# --------------------------------------------------------------------------

QS = (0.025, 0.16, 0.5, 0.84, 0.975)        # the same quantiles as OPT's range


def realised_range(fx: dict, now: float, days: int = 63) -> dict:
    """
    The range EUR/USD's own swings over the last `days` trading days would
    give, as a lognormal without drift: the fallback when no options reading
    exists yet. Labelled as such on the page.
    """
    ks = sorted(fx)[-(days + 1):]
    rs = [math.log(fx[b] / fx[a]) for a, b in zip(ks, ks[1:]) if fx[a] > 0 and fx[b] > 0]
    vol = statistics.pstdev(rs) * math.sqrt(252) if len(rs) > 20 else None
    if not vol:
        return {"available": False, "why": "too little history"}
    from statistics import NormalDist
    z = [NormalDist().inv_cdf(q) for q in QS]
    rows = []
    for d in (30, 91, 182, 365):
        t = d / 365
        rows.append({"days": d, "iv": round(vol * 100, 1),
                     "q": [round(now * math.exp(-0.5 * vol * vol * t + zz * vol * math.sqrt(t)), 4) for zz in z]})
    return {"available": True, "source": "realised", "vol": round(vol * 100, 1), "days": days, "fx_now": now, "rows": rows}


def from_chain(a: dict, fx_now: float) -> dict:
    """OPT's reading of FXE turned into EUR/USD: the trust's price is the euro, less its costs, in dollars."""
    k = fx_now / a["spot"] if fx_now and a.get("spot") else None
    rows = []
    for e in a.get("expiries") or []:
        if not e.get("q") or e["days"] < 6:
            continue
        rows.append({"exp": e["exp"], "days": e["days"], "iv": round(e["atm"] * 100, 1),
                     "q": [round(x * k, 4) for x in e["q"]] if k else None})
    pick = []
    for target in (30, 91, 182, 365):
        best = min(rows, key=lambda r: abs(r["days"] - target), default=None)
        if best and best not in pick and abs(best["days"] - target) < target * 0.5:
            pick.append(best)
    cm = a.get("cm") or {}
    sk = a.get("skew30") or {}
    return {"available": True, "source": "implied", "mode": a.get("mode"), "session": a.get("session"),
            "spot": a.get("spot"), "factor": round(k, 5) if k else None, "fx_now": fx_now,
            "iv": {t: (round(cm[t] * 100, 1) if cm.get(t) else None) for t in ("30", "90", "365")},
            "skew": {"put": round(sk["put"] * 100, 1) if sk.get("put") else None,
                     "call": round(sk["call"] * 100, 1) if sk.get("call") else None, "gap": sk.get("gap")},
            "rows": pick, "realised": (a.get("realised") or {}).get("rv21"), "read_at": time.time()}


def options_part(fx_now: Optional[float] = None) -> dict:
    """
    FXE's options while New York trades them. Outside the session too few
    contracts trade to read, so the last good reading is kept and shown with
    its date; before the first one, EUR/USD's own swings stand in.
    """
    from . import options
    usd = ref_series().get("USD_LONG") or {}
    if fx_now is None:
        q = _quote()
        fx_now = q["v"] if q else (usd[max(usd)] if usd else None)
    try:
        a = options.build("FXE")
    except Exception as e:
        a = {"available": False, "why": f"{e.__class__.__name__}: {e}"[:200]}
    if a.get("available"):
        out = from_chain(a, fx_now)
        store.cache_put("eur:opt", out)
        return out
    last = store.cache_get("eur:opt")
    if isinstance(last, dict) and last.get("rows"):
        return {**last, "stale": True, "why": a.get("why")}
    return {**realised_range(usd, fx_now), "why": a.get("why")}


# --------------------------------------------------------------------------
# words
# --------------------------------------------------------------------------

def _say(o: dict) -> list[str]:
    out = []
    p, r, b = o.get("price") or {}, o.get("rates") or {}, o.get("book") or {}
    if p:
        bits = [f"EUR/USD is {p['now']:.4f}"]
        if p.get("y1") is not None:
            bits.append(f"{'up' if p['y1'] >= 0 else 'down'} {abs(p['y1']):.1f}% on a year")
        if p.get("pctile") is not None:
            bits.append(f"higher than on {p['pctile']}% of days since 1999")
        out.append(", ".join(bits) + ".")
    f = r.get("fit") if r else None
    if f:
        side = "above" if f["gap_pct"] > 0 else "below"
        out.append(f"The two-year gap is {f['gap']:.2f} points in the dollar's favour. Over the last three years "
                   f"each point of it has gone with about {abs(f['per_point']):.0f}% on EUR/USD (R² {f['r2']:.2f}); "
                   f"on the gap alone the euro would be {f['fair']:.4f}, so it trades {abs(f['gap_pct']):.1f}% {side} that.")
    pa = o.get("path") or {}
    if pa.get("fed") and pa.get("ecb"):
        out.append(f"Futures price the Fed {pa['fed']['bp']:+d}bp and the ECB {pa['ecb']['bp']:+d}bp over the next year: "
                   + ("the gap is expected to narrow, which has tended to help the euro."
                      if pa["gap_bp"] < -10 else "the gap is expected to widen, which has tended to help the dollar."
                      if pa["gap_bp"] > 10 else "about the same move on both sides."))
    if b and b.get("available"):
        out.append(f"{b['usd_pct']:.0f}% of your book is in dollars: each cent on EUR/USD is about "
                   f"€{abs(b['cent']):,.0f} on its euro value, the other way round.")
    return out


# --------------------------------------------------------------------------
# alerts and the brief, from what the page last built: never a download
# --------------------------------------------------------------------------

def alert_snapshot(o: dict) -> dict:
    p, r, b = o.get("price") or {}, o.get("rates") or {}, o.get("book") or {}
    g = r.get("gap2") or {}
    pa = o.get("path") or {}
    return {"fx": p.get("now"), "fx_asof": p.get("now_asof"), "day": p.get("day"), "m1": p.get("m1"),
            "gap": g.get("v"), "gap_m1_bp": g.get("m1_bp"), "gap_asof": g.get("asof"),
            "usd_pct": b.get("usd_pct"), "cent": b.get("cent"),
            "fed_bp": (pa.get("fed") or {}).get("bp"), "ecb_bp": (pa.get("ecb") or {}).get("bp"),
            "ecb_next": ((o.get("calendar") or {}).get("ecb") or {}).get("date"), "built_at": time.time()}


ALERT_KINDS = {"eur_usd_above": (1, "EUR/USD"), "eur_usd_below": (-1, "EUR/USD")}


def alert_values() -> dict:
    return store.cache_get("eur:alert") or {}


def check_rule(kind: str, value: float, vals: Optional[dict] = None, fx_live: Optional[float] = None) -> Optional[dict]:
    """None when the rule is quiet. The live rate, if the dashboard has one, wins over the page's."""
    if kind not in ALERT_KINDS:
        return None
    sign, label = ALERT_KINDS[kind]
    vals = alert_values() if vals is None else vals
    x = fx_live if fx_live else vals.get("fx")
    if not x:
        return {"level": "info", "text": f"{label}: no rate yet — open EUR once to load it"}
    if (sign > 0 and x > value) or (sign < 0 and x < value):
        return {"level": "warn", "text": f"{label} is {x:.4f} — {'above' if sign > 0 else 'below'} {value:.4f}"}
    return None


def live_move(d: dict) -> Optional[dict]:
    """Today's EUR/USD move as the dashboard saw it: from a dollar position's rate now and yesterday."""
    for p in d.get("positions", []):
        if p.get("currency") == "USD" and p.get("fx") and p.get("fx_prev"):
            return {"fx": p["fx"], "prev": p["fx_prev"], "pct": (p["fx"] / p["fx_prev"] - 1) * 100}
    return None


def alerts(d: dict) -> list[dict]:
    """A big day for the euro against your dollars, and the rate gap moving fast."""
    out = []
    mv = live_move(d)
    T = d.get("totals") or {}
    if mv and abs(mv["pct"]) >= MOVE_ALERT:
        out.append({"level": "down" if (T.get("day_fx_eur") or 0) < 0 else "up", "code": "EUR",
                    "text": f"EUR/USD {mv['pct']:+.2f}% today, to {mv['fx']:.4f}",
                    "detail": f"{(T.get('day_fx_eur') or 0):+,.0f} EUR on your dollar holdings from the currency alone"})
    v = alert_values()
    g = v.get("gap_m1_bp")
    if g is not None and abs(g) >= GAP_ALERT and v.get("gap") is not None:
        out.append({"level": "info", "code": "EUR",
                    "text": f"The two-year rate gap {'widened' if g > 0 else 'narrowed'} {abs(g)}bp in a month, to {v['gap']:.2f} points",
                    "detail": "a narrower gap has tended to lift the euro, a wider one the dollar"})
    return out


def brief_line() -> Optional[str]:
    v = alert_values()
    if not v.get("fx"):
        return None
    bits = [f"EUR/USD {v['fx']:.4f}" + (f" ({v['day']:+.1f}% on the day" if v.get("day") is not None else "")
            + (f", {v['m1']:+.1f}% on a month)" if v.get("m1") is not None else (")" if v.get("day") is not None else ""))]
    if v.get("usd_pct") is not None:
        bits.append(f"{v['usd_pct']:.0f}% of your book in dollars")
    if v.get("gap") is not None:
        bits.append(f"the two-year gap {v['gap']:.2f} points" + (f" ({v['gap_m1_bp']:+d}bp on a month)" if v.get("gap_m1_bp") is not None else ""))
    if v.get("fed_bp") is not None and v.get("ecb_bp") is not None:
        bits.append(f"futures see the Fed {v['fed_bp']:+d}bp and the ECB {v['ecb_bp']:+d}bp in a year")
    if v.get("ecb_next"):
        bits.append(f"next ECB {v['ecb_next'][8:10]}/{v['ecb_next'][5:7]}")
    return "Euro: " + "; ".join(bits) + ". [EUR](/m/EUR)"


# --------------------------------------------------------------------------
# keeping it warm: built every two hours in the background, so the alerts
# and the brief have something to read without anyone opening the page
# --------------------------------------------------------------------------

_STARTED = {"v": False}


def ensure():
    if _STARTED["v"]:
        return
    _STARTED["v"] = True

    def loop():
        time.sleep(90)
        last_opt = None
        while True:
            try:
                view(force=store.cache_age("eur:view") > 2 * 3600)
            except Exception as e:
                log.warning("euro warm: %s", e)
            try:
                from . import options
                if options.ny_open() and last_opt != date.today():
                    if options_part().get("source") == "implied":
                        last_opt = date.today()
            except Exception as e:
                log.warning("euro options: %s", e)
            time.sleep(2 * 3600 if last_opt == date.today() else 1800)
    threading.Thread(target=loop, name="euro-warm", daemon=True).start()
