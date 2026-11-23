"""
What option strategies have actually done, and why sellers earn.

THE HISTORY. Cboe has run option strategies on the S&P 500 since 1986 as
indexes, every roll on its own rules, and publishes each one daily. They are
the honest record of what covered calls, put-writing, collars, condors and
butterflies earned on the index, with the costs of a mechanical rule but no
fees. Each is set against the S&P 500 with dividends (Yahoo's ^SP500TR) over
its own months, in dollars or in euros (at the ECB's month-end rate, from
1999), with the same statistics: growth, swings, worst fall, return per unit of
risk, beta, how much of the market's up and down months it caught, and how it
did by the VIX at the start of each month.

  BXM    long the S&P 500, sells a one-month at-the-money call each month
  BXY    the same with a call 2% out of the money
  BXMD   the same with the call nearest 30 delta
  PUT    sells a one-month at-the-money put each month, secured by Treasury bills
  WPUT   the same each week, with one-week puts
  CMBO   long the S&P 500, sells a one-month at-the-money put (bills behind it)
         and a 2% out-of-the-money call (the shares behind it)
  PPUT   long the S&P 500, buys a one-month 5% out-of-the-money put
  CLL    long the S&P 500, sells monthly calls at 110% and buys quarterly puts at 95%
  CLLZ   long the S&P 500, buys a monthly 2.5%–5% put spread paid for by an
         out-of-the-money call
  CNDR   sells monthly 15–20 delta puts and calls and buys 5-delta wings, on bills
  BFLY   sells the at-the-money put and call and buys 5% wings, on bills

(Cboe's own descriptions; the condor's short deltas are given as about 0.15 in
2015 and 0.20 in 2021.)

WHY SELLERS EARN. The VIX is the market's price for the next thirty days of
S&P 500 movement. Set against what the index then did, the next 21 sessions'
realised volatility, it has been higher most months: buyers of protection pay
more than the movement turns out to be worth, which is the premium sellers
collect. The months it was lower are the crashes, and they are where a
seller's years of premium go. Also: the VIX against its three-month and
nine-day siblings today (the term structure), and Cboe's SKEW index.
"""

from __future__ import annotations

import csv
import io
import math
import statistics
import time
from datetime import date, datetime
from typing import Optional

from . import net, store

CBOE = "https://cdn.cboe.com/api/global/us_indices/daily_prices/{s}_History.csv"
STRATS = [
    ("BXM", "Covered call, at the money", "income", "Long the S&P 500, sells a one-month at-the-money call each month."),
    ("BXY", "Covered call, 2% out", "income", "Long the S&P 500, sells a one-month call 2% out of the money each month."),
    ("BXMD", "Covered call, 30 delta", "income", "Long the S&P 500, sells the one-month call nearest 30 delta each month."),
    ("PUT", "Put-write, monthly", "income", "Sells a one-month at-the-money put each month, secured by Treasury bills."),
    ("WPUT", "Put-write, weekly", "income", "Sells a one-week at-the-money put each week, secured by Treasury bills."),
    ("CMBO", "Covered combo", "income", "Long the S&P 500; sells a one-month at-the-money put (bills behind it) and a 2% out-of-the-money call."),
    ("PPUT", "Protective put, 5%", "protection", "Long the S&P 500, buys a one-month put 5% out of the money each month."),
    ("CLL", "Collar 95–110", "protection", "Long the S&P 500, sells monthly calls at 110% and buys quarterly puts at 95%."),
    ("CLLZ", "Zero-cost put-spread collar", "protection", "Long the S&P 500, buys a monthly 2.5%–5% put spread, paid for by selling an out-of-the-money call."),
    ("CNDR", "Iron condor", "calm", "Sells monthly 15–20 delta puts and calls, buys 5-delta wings; the rest in Treasury bills."),
    ("BFLY", "Iron butterfly", "calm", "Sells the one-month at-the-money put and call, buys 5% out-of-the-money wings; the rest in bills."),
]
VOLS = ["VIX", "VIX9D", "VIX3M", "SKEW", "SPX"]
BENCH = "SPXTR"
REGIMES = [("calm", "VIX under 15", 0, 15), ("normal", "VIX 15 to 25", 15, 25), ("stressed", "VIX 25 or more", 25, 1e9)]
TTL = 12 * 3600


def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def parse_cboe(text: str) -> dict[str, float]:
    """Cboe's CSV: DATE then one value column, or OPEN/HIGH/LOW/CLOSE; the close is kept."""
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {}
    head = [h.strip().upper() for h in rows[0]]
    col = head.index("CLOSE") if "CLOSE" in head else 1
    out = {}
    for r in rows[1:]:
        if len(r) <= col:
            continue
        try:
            d = datetime.strptime(r[0].strip(), "%m/%d/%Y").date().isoformat()
        except ValueError:
            continue
        v = _f(r[col])
        if v is not None:
            out[d] = v
    return out


def _load() -> dict:
    from . import bonds
    series, missing = {}, []
    for s in [k for k, *_ in STRATS] + VOLS:
        try:
            text = net.get(CBOE.format(s=s), timeout=40).decode("utf-8", "replace")
            m = parse_cboe(text)
            if len(m) > 100:
                series[s] = m
            else:
                missing.append(s)
        except Exception:
            missing.append(s)
        time.sleep(0.2)
    data = bonds._yf().download(["^SP500TR"], period="max", interval="1d", auto_adjust=True,
                                progress=False, group_by="ticker", threads=False)
    tr = bonds._closes(data, "^SP500TR", False)
    if len(tr) < 1000:
        raise RuntimeError("Yahoo returned no history for the S&P 500 with dividends")
    series[BENCH] = tr
    return {**bonds._pack(series), "missing": missing, "fetched_at": time.time()}


def raw() -> dict:
    from . import bonds
    return bonds._unpack(bonds._cached("strat:hist", TTL, _load))


# --------------------------------------------------------------------------
# the arithmetic
# --------------------------------------------------------------------------

def month_ends(m: dict) -> dict[str, float]:
    """The last value of each calendar month, keyed YYYY-MM."""
    out = {}
    for d in sorted(m):
        out[d[:7]] = m[d]
    return out


def _next_month(m: str) -> str:
    y, mm = int(m[:4]), int(m[5:7])
    return f"{y + (mm == 12)}-{mm % 12 + 1:02d}"


def unbroken(months: list[str]) -> list[str]:
    """
    The last run of consecutive calendar months. Cboe's file for PUT has seven
    scattered values before 2007; a gap of years read as one month would make
    its early history look like a single wild month.
    """
    if not months:
        return []
    ms = sorted(months)
    i = len(ms) - 1
    while i > 0 and _next_month(ms[i - 1]) == ms[i]:
        i -= 1
    return ms[i:]


def returns(levels: dict[str, float], months: list[str]) -> list[Optional[float]]:
    out = []
    for a, b in zip(months, months[1:]):
        x, y = levels.get(a), levels.get(b)
        out.append(y / x - 1 if x and y else None)
    return out


def stats(r: list[float], bench: list[float], rf: list[float]) -> dict:
    """Monthly returns in, the yearly figures out. `bench` and `rf` are the same months'."""
    n = len(r)
    lvl, peak, worst = 1.0, 1.0, 0.0
    for x in r:
        lvl *= 1 + x
        peak = max(peak, lvl)
        worst = min(worst, lvl / peak - 1)
    cagr = lvl ** (12 / n) - 1
    vol = statistics.pstdev(r) * math.sqrt(12)
    ex = [x - f for x, f in zip(r, rf)]
    sharpe = statistics.fmean(ex) * 12 / vol if vol > 0 else None
    mb = statistics.fmean(bench)
    vb = statistics.pvariance(bench)
    beta = sum((x - statistics.fmean(r)) * (b - mb) for x, b in zip(r, bench)) / n / vb if vb > 0 else None
    up = [(x, b) for x, b in zip(r, bench) if b > 0]
    dn = [(x, b) for x, b in zip(r, bench) if b < 0]
    cap = lambda pairs: (statistics.fmean(x for x, _ in pairs) / statistics.fmean(b for _, b in pairs) * 100) if pairs else None
    return {"cagr": round(cagr * 100, 2), "vol": round(vol * 100, 1), "worst": round(worst * 100, 1),
            "sharpe": round(sharpe, 2) if sharpe is not None else None, "beta": round(beta, 2) if beta is not None else None,
            "up": round(cap(up)) if up else None, "down": round(cap(dn)) if dn else None,
            "worst_month": round(min(r) * 100, 1), "pos_months": round(sum(1 for x in r if x > 0) / n * 100), "months": n,
            "growth": round(lvl, 3)}


def regime_of(vix: Optional[float]) -> Optional[str]:
    if vix is None:
        return None
    return next(k for k, _, lo, hi in REGIMES if lo <= vix < hi)


def history(since: str = "2007-01", ccy: str = "EUR", data: Optional[dict] = None) -> dict:
    data = raw() if data is None else data
    from . import bonds, euro
    tr = month_ends(data[BENCH])
    vix = month_ends(data.get("VIX") or {})
    fx = month_ends(euro.ref_series().get("USD_LONG") or {}) if ccy == "EUR" else {}
    if ccy == "EUR" and since < "1999-01":
        since = "1999-01"
    try:
        bill = {}
        for d, v in (bonds.us_series().get("DGS3MO") or {}).items():
            bill.setdefault(d[:7], []).append(v)
        bill = {k: statistics.fmean(v) / 1200 for k, v in bill.items()}
    except Exception:
        bill = {}

    def conv(levels: dict) -> dict:
        if ccy != "EUR":
            return levels
        return {m: v / fx[m] for m, v in levels.items() if m in fx}

    trc = conv(tr)
    rows, series, years = [], {}, {}
    for key, name, group, what in STRATS:
        lv = data.get(key)
        if not lv:
            continue
        me = conv(month_ends(lv))
        months = [m for m in unbroken(sorted(me)) if m >= since and m in trc]
        if len(months) < 25:
            continue
        r = returns(me, months)
        b = returns(trc, months)
        keep = [(p, m, x, y) for p, m, x, y in zip(months, months[1:], r, b) if x is not None and y is not None]
        if len(keep) < 24:
            continue
        ms = [k[1] for k in keep]
        rr, bb = [k[2] for k in keep], [k[3] for k in keep]
        rf = [bill.get(m, 0.0) for m in ms]
        st, sb = stats(rr, bb, rf), stats(bb, bb, rf)
        # by the VIX at the start of each month
        by = {}
        for m_prev, m, x, y in keep:
            g = regime_of(vix.get(m_prev))
            if g:
                by.setdefault(g, []).append((x, y))
        reg = {g: {"n": len(v), "avg": round(statistics.fmean(a for a, _ in v) * 100, 2),
                   "bench": round(statistics.fmean(b for _, b in v) * 100, 2)} for g, v in by.items()}
        # calendar years
        yr = {}
        for m, x, y in zip(ms, rr, bb):
            a, c = yr.get(m[:4], (1.0, 1.0))
            yr[m[:4]] = (a * (1 + x), c * (1 + y))
        full = {y: v for y, v in yr.items() if sum(1 for m in ms if m.startswith(y)) >= 11}
        won = sum(1 for a, c in full.values() if a > c)
        years[key] = {y: round((a - 1) * 100, 1) for y, (a, c) in yr.items()}
        years[BENCH] = {**years.get(BENCH, {}), **{y: round((c - 1) * 100, 1) for y, (a, c) in yr.items()}}
        lvl, pts = 1.0, [[ms[0][:7] + "-01", 1.0]]
        for m, x in zip(ms, rr):
            lvl *= 1 + x
            pts.append([m + "-28", round(lvl, 4)])
        series[key] = pts
        rows.append({"k": key, "name": name, "group": group, "what": what, "from": ms[0], "to": ms[-1],
                     **st, "bench": sb, "years_won": won, "years": len(full), "regimes": reg})
    # the benchmark's own line over the window
    months = sorted(m for m in trc if m >= since)
    if len(months) > 2:
        lvl, pts = 1.0, [[months[0] + "-01", 1.0]]
        for a, b in zip(months, months[1:]):
            lvl *= trc[b] / trc[a]
            pts.append([b + "-28", round(lvl, 4)])
        series[BENCH] = pts
    return {"since": since, "ccy": ccy, "rows": rows, "series": series, "years": years,
            "regimes": [{"k": k, "label": l} for k, l, *_ in REGIMES],
            "asof": max(data[BENCH]), "missing": data.get("missing") or []}


def realised_forward(px: dict, days: list[str], i: int, n: int = 21) -> Optional[float]:
    """Annualised volatility of daily log returns over the n sessions after day i."""
    if i + n >= len(days):
        return None
    rs = [math.log(px[days[j + 1]] / px[days[j]]) for j in range(i, i + n)]
    return statistics.pstdev(rs) * math.sqrt(252) * 100


def sellers(data: Optional[dict] = None) -> dict:
    data = raw() if data is None else data
    vix, spx = data.get("VIX") or {}, data.get("SPX") or {}
    days = sorted(d for d in spx if d >= "1990-01-01")
    first_of_month, seen = [], set()
    for i, d in enumerate(days):
        if d[:7] not in seen and d in vix:
            seen.add(d[:7])
            first_of_month.append(i)
    pts = []
    for i in first_of_month:
        rv = realised_forward(spx, days, i)
        if rv is None:
            continue
        pts.append({"d": days[i], "vix": round(vix[days[i]], 2), "rv": round(rv, 2), "gap": round(vix[days[i]] - rv, 2)})
    if len(pts) < 60:
        raise RuntimeError("too little VIX history")
    gaps = [p["gap"] for p in pts]
    worst = sorted(pts, key=lambda p: p["gap"])[:8]
    out = {"n": len(pts), "from": pts[0]["d"], "to": pts[-1]["d"],
           "vix_avg": round(statistics.fmean(p["vix"] for p in pts), 1), "rv_avg": round(statistics.fmean(p["rv"] for p in pts), 1),
           "gap_avg": round(statistics.fmean(gaps), 1), "gap_med": round(statistics.median(gaps), 1),
           "share_over": round(sum(1 for g in gaps if g > 0) / len(gaps) * 100), "worst": worst,
           "series": [[p["d"], p["vix"], p["rv"]] for p in pts]}
    # the term structure today, and how often it has been upside down
    v9, v3 = data.get("VIX9D") or {}, data.get("VIX3M") or {}
    last = max(vix)
    out["now"] = {"d": last, "vix": vix[last], "vix9d": v9.get(last), "vix3m": v3.get(last),
                  "skew": (data.get("SKEW") or {}).get(max(data.get("SKEW") or {last: 0}))}
    both = sorted(d for d in vix if d in v3)
    if both:
        inv = [d for d in both if vix[d] > v3[d]]
        out["inverted_share"] = round(len(inv) / len(both) * 100, 1)
        out["inverted_since"] = both[0]
        sidx = {d: i for i, d in enumerate(days)}

        def fwd(ds):
            r = []
            for d in ds:
                i = sidx.get(d)
                if i is not None and i + 21 < len(days):
                    r.append(spx[days[i + 21]] / spx[d] - 1)
            return round(statistics.fmean(r) * 100, 2) if r else None
        out["after_inverted"] = fwd(inv)
        out["after_normal"] = fwd([d for d in both if vix[d] <= v3[d]])
    sk = data.get("SKEW") or {}
    if sk:
        v = list(sk.values())
        out["skew_pct"] = round(sum(1 for x in v if x <= sk[max(sk)]) / len(v) * 100)
    return out


def view(since: str = "2007-01", ccy: str = "EUR") -> dict:
    since = since if len(since) == 7 and since[4] == "-" else "2007-01"
    ccy = "USD" if ccy == "USD" else "EUR"

    def build():
        data = raw()
        out = {"history": history(since, ccy, data)}
        try:
            out["sellers"] = sellers(data)
        except Exception as e:
            out["sellers_error"] = f"{e.__class__.__name__}: {e}"[:200]
        return out
    return store.cache_through(f"strat:view:{since}:{ccy}", 3600, build)
