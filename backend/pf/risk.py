"""
Market risk, benchmark-relative performance, and return attribution.

Everything here is pure arithmetic on series that already exist — the daily
equity curve and the benchmark price history. No I/O, no network, so it is
testable against synthetic data with a known answer.

Everything against an index is measured on FIVE-DAY returns, not daily ones.
Four fifths of this book closes in New York at 22:00 Amsterdam time and the
benchmark trackers close in Frankfurt at 17:30, so one calendar day of each
overlaps for only part of a session. On daily returns the book's beta to MSCI
World came out at 0.70 with an R2 of 0.19; on five-day returns, and against a
US-listed fund that closes when the shares do, it is 1.2 to 1.3 with an R2
near 0.8. Builds 7 to 21 printed the first pair.

Three ideas, and the honest caveat attached to each:

  BETA and R2   Beta is the slope of your daily return regressed on the
                benchmark's. It is NEVER reported without R2, because beta
                alone invites a false conclusion. Beta 1.3 with R2 0.25 means
                "on the quarter of my risk that is market risk, I am geared
                1.3x" — three quarters of the variance is stock-specific and
                beta says nothing about it. Most tools print the slope and
                let you read it as if it described the whole portfolio.

  TRACKING      The only number that answers "is picking 50 names beating
  ERROR / IR    just buying the tracker, by enough to be worth the effort".
                Tracking error is how far your line wanders from the index;
                the information ratio is how much excess return you got per
                unit of that wandering.

  CONTRIBUTION  Who actually made the money, in euros. Deliberately built
                from realised + unrealised + dividends rather than a daily
                w*r decomposition: it needs no price history, it survives
                positions that opened and closed mid-period, and it
                reconciles EXACTLY to total profit — which verify.py asserts.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional

TRADING_DAYS = 252

# Below this many paired observations, an estimate is decoration. 60 trading
# days is about three months — already thin, but it is where a number stops
# being pure noise.
MIN_OBS = 60

# A day whose value is mostly CARRIED at the last traded price teaches beta
# nothing: a carried price does not move, so the day contributes zero variance
# and drags the measured beta toward zero. Days below this coverage are
# dropped rather than damping the estimate silently.
MIN_COVERAGE = 0.90

# A single day's move beyond this is not a market move, it is a data error —
# a stock split the feed did not adjust, a ticker that changed meaning, a
# corporate action. One 900% day will dominate a two-year regression.
SANE_DAILY = 0.60

# The opening weeks of any account are useless for measuring risk. When the
# book held EUR 200, a single EUR 100 purchase settling is a 50% "return" —
# arithmetically true, and it would dominate the variance of two years of a
# EUR 19,000 portfolio. Days whose PRIOR value is below this share of the
# portfolio's typical size are skipped.
MIN_SCALE = 0.05
MIN_DENOM_EUR = 250.0

# A day whose deposit is large relative to the account is dropped rather than
# neutralised. Subtracting the flow assumes it landed at the close; in this
# ledger a transfer often appears a day or two away from the cash actually
# moving, because DEGIRO writes the iDEAL reservation, the deposit and the
# internal sweep as separate rows on separate days. On a small account a EUR
# 2,550 deposit misdated by one day reads as a 50% return. It is a timing
# artefact, and one of them distorts two years of variance.
MAX_FLOW_SHARE = 0.20

# Returns are compared over this many trading days. A week makes the four and
# a half hours between the Frankfurt and New York closes a small part of the
# window instead of most of it. Windows overlap (one ends on every day), which
# keeps the sample; the standard error is widened for it below.
HORIZON = 5


# --------------------------------------------------------------------------
# small statistics, written out rather than imported
# --------------------------------------------------------------------------

def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _sd(xs: list[float]) -> float:
    """Sample standard deviation (n-1). Returns 0.0 for degenerate input."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (n - 1)) ** 0.5


def ols(y: list[float], x: list[float]) -> Optional[dict]:
    """
    Least squares y = alpha + beta*x. Daily units in, daily units out.

    Returns None rather than a misleading number when the sample is too small
    or the regressor never moves.
    """
    n = len(y)
    if n < MIN_OBS or n != len(x):
        return None
    mx, my = _mean(x), _mean(y)
    sxx = sum((xi - mx) ** 2 for xi in x)
    if sxx <= 0:
        return None
    sxy = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    syy = sum((yi - my) ** 2 for yi in y)

    beta = sxy / sxx
    alpha = my - beta * mx
    r2 = (sxy * sxy) / (sxx * syy) if syy > 0 else 0.0

    # Standard error of the slope, so the estimate can be reported with a band
    # instead of a spurious two decimal places.
    sse = max(syy - beta * sxy, 0.0)
    resid_sd = (sse / (n - 2)) ** 0.5 if n > 2 else 0.0
    se_beta = resid_sd / (sxx ** 0.5) if sxx > 0 else None

    return {
        "beta": beta,
        "alpha_daily": alpha,
        "r2": max(0.0, min(1.0, r2)),
        "n": n,
        "se_beta": se_beta,
        "resid_sd": resid_sd,
    }


def _annualise(rets: list[float]) -> Optional[float]:
    """Geometric annualised return from a list of daily returns."""
    if not rets:
        return None
    g = 1.0
    for r in rets:
        g *= (1.0 + r)
    if g <= 0:
        return None
    return g ** (TRADING_DAYS / len(rets)) - 1.0


# --------------------------------------------------------------------------
# turning the curve into returns
# --------------------------------------------------------------------------

def curve_returns(series: list[dict], min_coverage: float = MIN_COVERAGE,
                  basis: str = "securities") -> dict[str, float]:
    """
    Flow-neutralised daily portfolio returns, keyed by day.

        r = (value_today - flow_today) / value_yesterday - 1

    `basis` picks which line. "securities" uses the holdings alone with the
    euros traded that day neutralised, and is the default because the account
    line's cash component moves when DEGIRO sweeps money to and from the
    linked flatex balance — a transfer with no trade behind it, which would
    otherwise be measured as a return. "account" uses securities plus cash
    with deposits neutralised, which is right for TWR and wrong for beta.

    The flow subtraction is not optional and is the easiest thing in this
    whole file to get wrong. The curve's value includes deposits, so a EUR
    2,000 deposit into a EUR 20,000 account reads as a +10% day. Left in, a
    handful of deposit days would dominate a two-year regression and the beta
    that came out would be a measure of when Rick gets paid, not of market
    sensitivity. This is the same neutralisation the TWR figure uses.

    Days where most of the value was carried at a last trade price rather than
    a market price are skipped: a carried price cannot move, so the day
    contributes a fake zero return and biases beta toward zero.

    Days early enough that the account was a rounding error are skipped too —
    see MIN_SCALE. Without that filter the measured volatility of this
    portfolio came out at 63% a year, which is not a fact about the portfolio,
    it is a fact about dividing by EUR 200.
    """
    vk, fk = ("s", "sf") if (basis == "securities" and series
                             and "s" in series[0]) else ("v", "f")

    vals = sorted(p[vk] for p in series if p.get(vk) and p[vk] > 0)
    scale = vals[len(vals) // 2] if vals else 0.0
    floor = max(scale * MIN_SCALE, MIN_DENOM_EUR) if scale else 0.0

    out: dict[str, float] = {}
    prev_v: Optional[float] = None
    prev_ok = False
    for p in series:
        v = p.get(vk)
        q = p.get("q", 1.0)
        ok = (q is None or q >= min_coverage)
        if prev_v is not None and prev_v > floor and ok and prev_ok and v is not None:
            f = p.get(fk) or 0.0
            if abs(f) <= MAX_FLOW_SHARE * prev_v:
                r = (v - f) / prev_v - 1.0
                if abs(r) <= SANE_DAILY:
                    out[p["d"]] = r
        if v is not None and v > 0:
            prev_v, prev_ok = v, ok
    return out


def series_returns(prices: dict[str, float]) -> dict[str, float]:
    """Daily returns of a price series, keyed by the later day."""
    out: dict[str, float] = {}
    days = sorted(prices)
    for a, b in zip(days, days[1:]):
        pa, pb = prices[a], prices[b]
        if pa and pb and pa > 0:
            r = pb / pa - 1.0
            if abs(r) <= SANE_DAILY:
                out[b] = r
    return out


def eur_prices(prices: dict[str, float], fx: dict[str, float] | None,
               ccy: str) -> dict[str, float]:
    """
    A native-currency price series restated in euros.

    For a euro investor holding a US share, the return that lands in the
    account is the dollar move AND the euro/dollar move. Measuring beta on the
    dollar price would describe a portfolio Rick does not own.
    """
    if ccy == "EUR" or not fx:
        return dict(prices)
    days = sorted(fx)
    out: dict[str, float] = {}
    for d, p in prices.items():
        f = fx.get(d)
        if f is None:
            earlier = [k for k in days if k <= d]
            f = fx[earlier[-1]] if earlier else None
        if f:
            out[d] = p / f
    return out


def _align(a: dict[str, float], b: dict[str, float]) -> tuple[list[float], list[float], list[str]]:
    days = sorted(set(a) & set(b))
    return [a[d] for d in days], [b[d] for d in days], days


def _asof(prices: dict[str, float], days: list[str], day: str, stale: int = 6) -> Optional[float]:
    """The price on `day`, or the newest one in the `stale` days before it."""
    from bisect import bisect_right
    from datetime import date
    i = bisect_right(days, day) - 1
    if i < 0:
        return None
    if days[i] != day and (date.fromisoformat(day) - date.fromisoformat(days[i])).days > stale:
        return None
    return prices[days[i]]


def curve_windows(port_r: dict[str, float], bench_px: dict[str, float],
                  calendar: list[str], h: int = HORIZON) -> tuple[list[float], list[float], list[str]]:
    """
    The portfolio's return and the benchmark's over every run of `h`
    consecutive usable days, keyed by the day each run ends.

    `port_r` has holes: days dropped for coverage, deposits or scale. A run
    that touches one is skipped rather than patched, for the same reason the
    day was dropped.
    """
    bdays = sorted(bench_px)
    ys, xs, ends = [], [], []
    for t in range(h, len(calendar)):
        run = calendar[t - h + 1:t + 1]
        if any(d not in port_r for d in run):
            continue
        b0, b1 = _asof(bench_px, bdays, calendar[t - h]), _asof(bench_px, bdays, calendar[t])
        if not b0 or not b1:
            continue
        g = 1.0
        for d in run:
            g *= 1.0 + port_r[d]
        ys.append(g - 1.0)
        xs.append(b1 / b0 - 1.0)
        ends.append(calendar[t])
    return ys, xs, ends


def price_windows(prices: dict[str, float], bench_px: dict[str, float],
                  h: int = HORIZON) -> tuple[list[float], list[float]]:
    """The same pairing for one instrument, over `h` of its own trading days."""
    days, bdays = sorted(prices), sorted(bench_px)
    ys, xs = [], []
    for t in range(h, len(days)):
        p0, p1 = prices[days[t - h]], prices[days[t]]
        b0, b1 = _asof(bench_px, bdays, days[t - h]), _asof(bench_px, bdays, days[t])
        if not (p0 and p1 and b0 and b1) or p0 <= 0:
            continue
        y = p1 / p0 - 1.0
        if abs(y) > 1.5:            # an unadjusted split, not a week of trading
            continue
        ys.append(y)
        xs.append(b1 / b0 - 1.0)
    return ys, xs


# --------------------------------------------------------------------------
# the benchmark-relative block
# --------------------------------------------------------------------------

def market_stats(port_r: dict[str, float], bench_px: dict[str, float],
                 calendar: list[str], key: str, label: str, ticker: str) -> dict:
    """
    Beta, R2, tracking error and information ratio against one benchmark.

    Beta, R2 and tracking error come from five-day windows (see HORIZON); the
    annualised returns, and so the active return, from the daily series over
    the days both have. Everything is measured on the overlap only, so a
    benchmark whose history starts later is compared fairly rather than being
    handed a head start of zero-return days.
    """
    y, x, ends = curve_windows(port_r, bench_px, calendar)
    yd, xd, days = _align(port_r, series_returns(bench_px))
    base = {"key": key, "label": label, "ticker": ticker,
            "n": len(y), "enough": len(y) >= MIN_OBS, "horizon_days": HORIZON,
            "from": days[0] if days else None, "to": days[-1] if days else None}
    if len(y) < MIN_OBS:
        return {**base, "beta": None, "r2": None, "tracking_error_pct": None,
                "information_ratio": None, "why": f"only {len(y)} comparable weeks"}

    fit = ols(y, x)
    per_year = TRADING_DAYS / HORIZON
    active = [yi - xi for yi, xi in zip(y, x)]
    te = _sd(active) * (per_year ** 0.5)
    ann_p, ann_b = _annualise(yd), _annualise(xd)
    ir = ((ann_p - ann_b) / te) if (te > 0 and ann_p is not None and ann_b is not None) else None

    # Overlapping windows share four of their five days, so the plain standard
    # error is too narrow by about the square root of the horizon.
    se = fit["se_beta"] * (HORIZON ** 0.5) if fit and fit["se_beta"] else None
    return {
        **base,
        "beta": round(fit["beta"], 3) if fit else None,
        "beta_lo": round(fit["beta"] - 1.96 * se, 2) if fit and se else None,
        "beta_hi": round(fit["beta"] + 1.96 * se, 2) if fit and se else None,
        "r2": round(fit["r2"], 3) if fit else None,
        "corr": round(fit["r2"] ** 0.5, 3) if fit else None,
        "alpha_ann_pct": round(fit["alpha_daily"] * per_year * 100, 2) if fit else None,
        "tracking_error_pct": round(te * 100, 2),
        "information_ratio": round(ir, 2) if ir is not None else None,
        "active_return_pct": round((ann_p - ann_b) * 100, 2)
        if ann_p is not None and ann_b is not None else None,
        "port_ann_pct": round(ann_p * 100, 2) if ann_p is not None else None,
        "bench_ann_pct": round(ann_b * 100, 2) if ann_b is not None else None,
    }


def volatility_block(port_r: dict[str, float]) -> dict:
    """Standalone risk: how much the line itself moves, market or not."""
    y = list(port_r.values())
    if len(y) < MIN_OBS:
        return {"enough": False, "n": len(y), "vol_pct": None, "downside_pct": None}
    down = [min(r, 0.0) for r in y]
    dd = (sum(d * d for d in down) / len(down)) ** 0.5 * (TRADING_DAYS ** 0.5)
    best = max(y)
    worst = min(y)
    return {
        "enough": True,
        "n": len(y),
        "vol_pct": round(_sd(y) * (TRADING_DAYS ** 0.5) * 100, 1),
        "downside_pct": round(dd * 100, 1),
        "best_day_pct": round(best * 100, 2),
        "worst_day_pct": round(worst * 100, 2),
        "positive_days_pct": round(sum(1 for r in y if r > 0) / len(y) * 100, 1),
    }


# --------------------------------------------------------------------------
# forward beta: the book you hold NOW, not the book you used to hold
# --------------------------------------------------------------------------

def position_betas(bench_px: dict[str, float], eur_hist: dict[str, dict[str, float]]) -> dict[str, dict]:
    """
    Beta and R2 for each instrument that has enough EUR price history, on
    five-day returns against the benchmark's prices.

    eur_hist: isin -> {day: price in EUR}
    """
    out: dict[str, dict] = {}
    for isin, prices in eur_hist.items():
        if not prices or len(prices) < MIN_OBS:
            continue
        y, x = price_windows(prices, bench_px)
        fit = ols(y, x)
        if fit:
            out[isin] = {"beta": round(fit["beta"], 2),
                         "r2": round(fit["r2"], 2),
                         "n": fit["n"]}
    return out


def forward_beta(rows: list[dict], betas: dict[str, dict]) -> dict:
    """
    The weighted beta of the positions held today.

    This is a different number from the regression beta above, and conflating
    them is the standard mistake. The regression beta describes two years of a
    portfolio whose composition changed completely — it includes 45 names that
    are already sold. This one describes the book as it stands. When they
    disagree, the gap is the story: it says the portfolio you hold now is
    riskier (or tamer) than the one you ran.

    Weighted over the covered value only, and the covered share is reported —
    a forward beta computed over 40% of the book is not a portfolio beta.
    """
    tot = sum(r["value_eur"] for r in rows if r["value_eur"] > 0) or 0.0
    num = cov = 0.0
    missing = []
    for r in rows:
        v = r["value_eur"]
        if v <= 0:
            continue
        b = betas.get(r["isin"])
        if b:
            num += v * b["beta"]
            cov += v
        else:
            missing.append(r["name"])
    if cov <= 0 or tot <= 0:
        return {"beta": None, "coverage_pct": 0.0, "missing": missing[:8],
                "missing_n": len(missing)}
    return {
        "beta": round(num / cov, 2),
        "coverage_pct": round(cov / tot * 100, 1),
        "missing": missing[:8],
        "missing_n": len(missing),
    }


# --------------------------------------------------------------------------
# contribution to return
# --------------------------------------------------------------------------

def contributions(rows: list[dict], closed_rows: list[dict],
                  div_by_isin: dict[str, float],
                  profit_eur: float, denominator: float,
                  other: dict | None = None,
                  expected_residual: float | None = None,
                  converted_eur: float = 0.0) -> dict:
    """
    Where the money came from, per name, in euros and in points of return.

    Built from the three things that are already exact — unrealised P&L,
    realised P&L and dividends received — rather than from a daily w*r
    decomposition. A daily decomposition needs unbroken price history for
    every name including the ones sold last year, and it quietly drops any
    position that opened mid-period. This one cannot drop anything, because
    it is an accounting identity:

        profit = unrealised + realised + net dividends + interest - fees

    The residual is reported, not hidden — but it is NOT expected to be zero.
    Interest and the account fees that never became part of a cost basis are
    real profit that belongs to no single instrument. `expected_residual` is
    what those come to, so the check is whether the residual matches what it
    should be, not whether it vanishes. Only the leftover after that is
    evidence of a bug, and it gets its own number.
    """
    agg: dict[str, dict] = {}

    def slot(isin, name):
        if isin not in agg:
            agg[isin] = {"isin": isin, "name": name, "unreal_eur": 0.0,
                         "realised_eur": 0.0, "div_eur": 0.0, "open": False}
        return agg[isin]

    for r in rows:
        s = slot(r["isin"], r["name"])
        s["unreal_eur"] += r.get("unreal_eur") or 0.0
        s["open"] = True
        s["value_eur"] = r.get("value_eur")
        s["weight"] = r.get("weight")
    for r in closed_rows:
        s = slot(r["isin"], r["name"])
        s["realised_eur"] += r.get("pnl_eur") or 0.0
    # A name can be open AND have realised P&L from an earlier partial sale,
    # so realised is applied from the disposal ledger for every ISIN, not only
    # for the ones that are fully closed.
    for isin, amt in (other or {}).get("realised_by_isin", {}).items():
        s = slot(isin, (other or {}).get("names", {}).get(isin, isin))
        s["realised_eur"] = amt          # authoritative, replaces the closed-row sum
    for isin, amt in div_by_isin.items():
        s = slot(isin, (other or {}).get("names", {}).get(isin, isin))
        s["div_eur"] += amt

    items = []
    for s in agg.values():
        total = s["unreal_eur"] + s["realised_eur"] + s["div_eur"]
        if abs(total) < 0.005 and not s.get("open"):
            continue
        items.append({**s, "total_eur": round(total, 2),
                      "unreal_eur": round(s["unreal_eur"], 2),
                      "realised_eur": round(s["realised_eur"], 2),
                      "div_eur": round(s["div_eur"], 2),
                      "points": round(total / denominator * 100, 2) if denominator else None})
    items.sort(key=lambda r: -r["total_eur"])

    named = sum(i["total_eur"] for i in items)
    resid = profit_eur - named
    winners = [i for i in items if i["total_eur"] > 0]
    losers = [i for i in items if i["total_eur"] < 0]

    # Unexplained = the part of the residual that interest and non-trade fees
    # do NOT account for. It is never exactly zero: fees charged in dollars
    # are converted at the day's rate here and were folded into a cost basis
    # at the trade's own rate, so a few euros of FX difference is expected.
    unexplained = (resid - expected_residual) if expected_residual is not None else None
    # How far off it may be. `converted_eur` is what reached the balance in
    # another currency: converted here at the day's rate and by the broker at
    # its own, a day later and less its fee, so the two sides differ by a
    # fraction of a percent of those rows. The rest is cents of rounding. This
    # was half a percent of PROFIT, which grows with the market and not with
    # anything that can go wrong here — at EUR 6,000 of profit it waved through
    # a EUR 28 credit nothing had named.
    tol = max(5.0, abs(converted_eur) * 0.01)

    return {
        "items": items,
        "denominator_eur": round(denominator, 2) if denominator else None,
        "named_eur": round(named, 2),
        "profit_eur": round(profit_eur, 2),
        "residual_eur": round(resid, 2),
        "residual_expected_eur": round(expected_residual, 2)
        if expected_residual is not None else None,
        "unexplained_eur": round(unexplained, 2) if unexplained is not None else None,
        "tolerance_eur": round(tol, 2),
        "residual_note": "interest, account fees, broker credits and anything "
                         "else not attributable to a single instrument",
        "reconciles": (abs(unexplained) <= tol) if unexplained is not None
                      else abs(resid) <= tol,
        "winners": len(winners),
        "losers": len(losers),
        "top_eur": round(sum(i["total_eur"] for i in items[:3]), 2),
        "top_share_pct": round(sum(i["total_eur"] for i in items[:3]) / named * 100, 1)
        if named else None,
        "worst_eur": round(sum(i["total_eur"] for i in items[-3:]), 2) if losers else None,
    }
