"""
MVO — Markowitz: what a book can be expected to earn for the swings it takes.

Markowitz's point (1952) was that a share's risk inside a portfolio is not its
own swing but how it moves with everything else. So for every level of risk
there is one best mix, and the line of them is the efficient frontier: each
mix on it earns the most that can be expected for its risk, and every mix
below it could earn more, or swing less, holding the same things in other
amounts.

Three inputs, and the third is the trouble:

  RISK         how every pair moved together over up to three years, on
               five-day returns in euros (risk.HORIZON), pulled a fifth of
               the way toward "every pair equally correlated": the same
               matrix as risk by holding (riskmap.shrunk_cov).
  LIMITS       long only; at most a tenth of the book in one share; a fund
               without limit.
  RETURNS      what each holding can be expected to earn. Past returns are
               the classic input and the classic failure: two years in which
               a chip maker tripled make the optimiser put everything in it.
               So by default the returns are the ones market prices imply
               (Black and Litterman, 1992). Every holding earns the risk-free
               rate plus its beta to world shares times the equity premium:
               the market pays for market risk only. With nothing added, that
               makes the world fund the best mix, which is the honest
               starting point. A view of your own ("Microsoft, 12% a year,
               middling confidence") moves the answer by as much as the
               confidence you give it: a single view moves its own share that
               fraction of the way, and everything correlated with it a part
               of that.

  rf           the euro overnight rate, from the €STR fund's last 63 days.
  premium      world shares over bills, 1871–2020, from FCST's history
               (arithmetic, about 6% a year).

THE DEPOSIT PLAN buys whole shares (DEGIRO sells no fractions) at the costs
the ledger shows: a fee per order by exchange, plus the currency charge on a
dollar order. It never sells. Each month it buys the order that brings the
book closest to the target, measured as tracking error (how far the two
would drift apart in a year), and when every order would cost more than 1%
of itself it waits and adds next month's money.

What this is not: advice, or a forecast. Equilibrium returns say what the
market expects on average, which is exactly why no share looks special in
them. The frontier is drawn from estimates, and the resampled cloud shows how
far it moves when three years of history are drawn again.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from . import risk, riskmap, store

H = risk.HORIZON
YEAR = 252
PER_YEAR = YEAR / H
LOOK = 3 * YEAR             # at most three years of five-day returns
MIN_DAYS = 2 * YEAR         # a holding needs two years to be modelled
CAP = 0.10                  # at most this much of the book in one share
TAU = 0.05                  # Black-Litterman: how uncertain the equilibrium itself is
CONF = {"low": 0.25, "mid": 0.5, "high": 0.75}
MONTHLY = 150.0
MAX_COST = 0.01             # an order that costs more than this share of itself waits
MAX_ORDERS = 3              # orders in one month at most
MONTHS = 24
RESAMPLES = 100
BLOCK = 130                 # resampling draws half-years of history at a time
SEED = 20261013
LINK = 0.55                 # the constellation draws a line from this correlation up

# Funds a euro account can buy at DEGIRO, so the optimiser can say whether the
# book is missing something it does not own. All UCITS, all on Euronext or Xetra.
FUNDS = [
    {"t": "IWDA.AS", "name": "iShares Core MSCI World", "group": "World shares", "venue": "EAM"},
    {"t": "IMAE.AS", "name": "iShares Core MSCI Europe", "group": "European shares", "venue": "EAM"},
    {"t": "EMIM.AS", "name": "iShares Core MSCI EM IMI", "group": "Emerging markets", "venue": "EAM"},
    {"t": "IEGA.AS", "name": "iShares Core € Govt Bond", "group": "Euro government bonds", "venue": "EAM"},
    {"t": "8PSG.DE", "name": "Invesco Physical Gold", "group": "Gold", "venue": "XET"},
]
MARKET, CASH, CAL = "IWDA.AS", "XEON.DE", "SPY"

# What an order cost when the ledger has too few to say: per order, and the
# currency charge as a share of a dollar order.
FEE_DEFAULT = {"US": (2.0, 0.0025), "EAM": (3.0, 0.0), "XET": (4.9, 0.0), "TDG": (1.0, 0.0),
               "OTHER": (3.0, 0.0)}
US_VENUES = {"NDQ", "NSY", "NYS", "NAS", "ASE", "BATS", "ARCA", "PSE", "OTC"}
VENUE_NAME = {"US": "New York", "EAM": "Amsterdam", "XET": "Xetra", "TDG": "Tradegate",
              "OTHER": "another exchange"}


def _np():
    import numpy as np
    return np


# --------------------------------------------------------------------------
# the optimiser: long only, capped, on numpy alone
# --------------------------------------------------------------------------

def project(v, u):
    """
    The nearest point to `v` with 0 <= w <= u and sum(w) = 1, exactly.
    w = clip(v - t, 0, u) for the t at which the sum is 1; that sum is
    piecewise linear in t and falls as t rises, with corners where some
    v_i - t meets 0 or its cap, so t sits between two corners and is read
    off the straight line joining them.
    """
    np = _np()
    v = np.asarray(v, dtype=float)
    u = np.asarray(u, dtype=float)
    if float(u.sum()) < 1.0 - 1e-12:
        raise ValueError("the caps add up to less than the whole book")
    bp = np.sort(np.concatenate([v - u, v]))
    F = np.clip(v[None, :] - bp[:, None], 0.0, u[None, :]).sum(axis=1)     # falls with bp
    k = int(np.searchsorted(-F, -1.0, side="left"))      # first corner with F <= 1
    if k <= 0:
        t = float(bp[0])
    elif k >= len(bp):
        t = float(bp[-1])
    else:
        f0, f1, b0, b1 = float(F[k - 1]), float(F[k]), float(bp[k - 1]), float(bp[k])
        t = b1 if f0 - f1 < 1e-15 else b0 + (f0 - 1.0) * (b1 - b0) / (f0 - f1)
    return np.clip(v - t, 0.0, u)


def solve(mu, S, gamma: float, u, w0=None, lmax: Optional[float] = None,
          iters: int = 4000, tol: float = 1e-10):
    """
    The mix that maximises  mu'w - gamma/2 w'Sw  under the caps: accelerated
    projected gradient (FISTA) with a restart whenever a step goes uphill.
    """
    np = _np()
    mu = np.asarray(mu, dtype=float)
    n = len(mu)
    if lmax is None:
        lmax = float(np.linalg.eigvalsh(S)[-1])
    step = 1.0 / max(gamma * lmax, 1e-12)
    x = project(np.full(n, 1.0 / n) if w0 is None else w0, u)
    y, t = x.copy(), 1.0
    for _ in range(iters):
        g = gamma * (S @ y) - mu
        xn = project(y - step * g, u)
        if float(np.abs(xn - x).max()) < tol:
            x = xn
            break
        if float(g @ (xn - x)) > 0:          # went uphill: restart the momentum
            t, y = 1.0, xn.copy()
        else:
            tn = 0.5 * (1.0 + math.sqrt(1.0 + 4.0 * t * t))
            y = xn + ((t - 1.0) / tn) * (xn - x)
            t = tn
        x = xn
    return x


def stats(w, mu, S) -> tuple[float, float]:
    """(expected return, volatility) of a mix, both a year."""
    np = _np()
    w = np.asarray(w, dtype=float)
    return float(w @ mu), math.sqrt(max(float(w @ S @ w), 0.0))


def max_return(mu, u):
    """The corner of the frontier: the highest returns first, each to its cap."""
    np = _np()
    w = np.zeros(len(mu))
    left = 1.0
    for i in np.argsort(-np.asarray(mu)):
        take = min(float(u[i]), left)
        w[i] = take
        left -= take
        if left <= 1e-12:
            break
    return w


GAMMAS = [0.25 * (1600 ** (k / 47)) for k in range(48)]     # 0.25 … 400


def frontier(mu, S, u) -> list[dict]:
    """The frontier from the least-risk mix to the highest-return one."""
    np = _np()
    lmax = float(np.linalg.eigvalsh(S)[-1])
    pts, w = [], None
    for g in sorted(GAMMAS, reverse=True):
        w = solve(mu, S, g, u, w0=w, lmax=lmax)
        r, v = stats(w, mu, S)
        pts.append({"gamma": g, "ret": r, "vol": v, "w": w})
    wm = max_return(mu, u)
    r, v = stats(wm, mu, S)
    pts.append({"gamma": 0.0, "ret": r, "vol": v, "w": wm})
    out = []
    for p in sorted(pts, key=lambda p: p["vol"]):
        if out and p["ret"] <= out[-1]["ret"] + 1e-9:
            continue                         # not efficient: more risk, no more return
        out.append(p)
    return out


def _bisect(mu, S, u, f, target: float, lmax: float) -> dict:
    """The frontier mix where f(ret, vol) reaches `target`, f falling as gamma rises."""
    np = _np()
    lo, hi = math.log(GAMMAS[0]), math.log(4 * GAMMAS[-1])
    w = None
    for _ in range(48):
        mid = 0.5 * (lo + hi)
        w = solve(mu, S, math.exp(mid), u, w0=w, lmax=lmax)
        r, v = stats(w, mu, S)
        if f(r, v) > target:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-4:
            break
    r, v = stats(w, mu, S)
    return {"gamma": math.exp(0.5 * (lo + hi)), "ret": r, "vol": v, "w": w}


def at_vol(mu, S, u, vol: float, front: list[dict]) -> dict:
    """The best mix with this volatility, or the nearest end of the frontier."""
    np = _np()
    if vol <= front[0]["vol"]:
        return front[0]
    if vol >= front[-1]["vol"]:
        return front[-1]
    return _bisect(mu, S, u, lambda r, v: v, vol, float(np.linalg.eigvalsh(S)[-1]))


def at_ret(mu, S, u, ret: float, front: list[dict]) -> dict:
    """The least-risk mix that is expected to earn `ret`."""
    np = _np()
    if ret <= front[0]["ret"]:
        return front[0]
    if ret >= front[-1]["ret"]:
        return front[-1]
    return _bisect(mu, S, u, lambda r, v: r, ret, float(np.linalg.eigvalsh(S)[-1]))


def tangency(mu, S, u, rf: float, front: list[dict]) -> dict:
    """The mix with the most expected return over cash per unit of risk."""
    np = _np()
    sh = [(p["ret"] - rf) / p["vol"] if p["vol"] > 0 else -1e9 for p in front]
    k = int(np.argmax(sh))
    if front[k]["gamma"] <= 0:
        return front[k]
    lmax = float(np.linalg.eigvalsh(S)[-1])
    # golden section on log gamma between the neighbours of the best grid point
    gs = [p["gamma"] for p in front if p["gamma"] > 0]
    gk = front[k]["gamma"]
    up = min([g for g in gs if g > gk], default=gk * 2)
    dn = max([g for g in gs if g < gk], default=gk / 2)
    a, b = math.log(dn), math.log(up)
    phi = (math.sqrt(5) - 1) / 2
    best = front[k]

    def score(lg, w0):
        w = solve(mu, S, math.exp(lg), u, w0=w0, lmax=lmax)
        r, v = stats(w, mu, S)
        return (r - rf) / v if v > 0 else -1e9, {"gamma": math.exp(lg), "ret": r, "vol": v, "w": w}

    c, d = b - phi * (b - a), a + phi * (b - a)
    sc, pc = score(c, best["w"])
    sd, pd = score(d, best["w"])
    for _ in range(30):
        if sc > sd:
            b, d, sd, pd = d, c, sc, pc
            c = b - phi * (b - a)
            sc, pc = score(c, pd["w"])
        else:
            a, c, sc, pc = c, d, sd, pd
            d = a + phi * (b - a)
            sd, pd = score(d, pc["w"])
        if b - a < 1e-3:
            break
    cand = pc if sc > sd else pd
    best_sh = (best["ret"] - rf) / best["vol"] if best["vol"] > 0 else -1e9
    return cand if max(sc, sd) > best_sh else best


# --------------------------------------------------------------------------
# returns: the market's, yours, or the past's
# --------------------------------------------------------------------------

def premium() -> float:
    """World shares over bills, 1871–2020, arithmetic: what FCST's history says equity risk paid."""
    seed = json.loads(Path(__file__).with_name("fcst_seed.json").read_text(encoding="utf-8"))
    w = seed["world"]
    ex = [(1 + r) / (1 + b) - 1 for r, b in zip(w["real"], w["bill"]) if r is not None and b is not None]
    return statistics.fmean(ex)


def prior(S, m: int, rf: float, erp: float):
    """
    Equilibrium returns: rf + beta to the market times the premium. The same
    as Black-Litterman's  pi = delta S w_market  with delta = erp / var(market),
    the market here being the world fund.
    """
    np = _np()
    beta = S[:, m] / S[m, m]
    return rf + beta * erp, beta


def posterior(pi, S, views: list[tuple[int, float, float]], tau: float = TAU):
    """
    Black-Litterman with views on single holdings: (index, expected return,
    confidence 0..1). A view's uncertainty is set so that, alone, it moves its
    own holding `confidence` of the way from equilibrium to the view.
    """
    np = _np()
    if not views:
        return np.asarray(pi, dtype=float).copy()
    n, k = len(pi), len(views)
    P = np.zeros((k, n))
    Q = np.zeros(k)
    for j, (i, q, _) in enumerate(views):
        P[j, i] = 1.0
        Q[j] = q
    tS = tau * S
    PSP = P @ tS @ P.T
    Om = np.diag([(1.0 / max(min(c, 0.999), 0.001) - 1.0) * PSP[j, j] for j, (_, _, c) in enumerate(views)])
    adj = tS @ P.T @ np.linalg.solve(PSP + Om, Q - P @ pi)
    return np.asarray(pi, dtype=float) + adj


def implied(S, w, rf: float, delta: float):
    """What a mix's weights say its owner expects: rf + delta S w (reverse optimisation)."""
    np = _np()
    return rf + delta * (S @ np.asarray(w, dtype=float))


# --------------------------------------------------------------------------
# costs, from the ledger
# --------------------------------------------------------------------------

def venue_class(exchange: Optional[str], ccy: Optional[str]) -> str:
    ex = (exchange or "").upper()
    if ex in US_VENUES or (not ex and ccy == "USD"):
        return "US"
    if ex in ("EAM", "XET", "TDG"):
        return ex
    return "US" if ccy == "USD" else "OTHER"


def fees(txns: Optional[list] = None, today: Optional[date] = None) -> dict:
    """
    What an order costs per exchange: the median fee of the last year's buys
    there, and for dollar orders the median currency charge as a share of the
    order. Fewer than three buys and the default stands.
    """
    txns = store.load_txns() if txns is None else txns
    today = today or date.today()
    since = today - timedelta(days=365)
    seen: dict[str, dict] = {}
    for t in txns:
        if t.kind != "BUY" or t.trade_dt.date() < since:
            continue
        v = abs(t.value_eur or 0.0)
        if v <= 0:
            continue
        k = venue_class(t.exchange, t.currency)
        g = seen.setdefault(k, {"fee": [], "fx": []})
        g["fee"].append(abs(t.fee_eur or 0.0))
        if (t.currency or "EUR") != "EUR":
            g["fx"].append(abs(t.autofx_cost_eur or 0.0) / v)
    out = {}
    for k, (fee, fxr) in FEE_DEFAULT.items():
        g = seen.get(k)
        n = len(g["fee"]) if g else 0
        if n >= 3:
            fee = statistics.median(g["fee"])
            if g["fx"]:
                fxr = statistics.median(g["fx"])
        out[k] = {"fee": round(fee, 2), "fx": round(fxr, 5), "n": n, "where": VENUE_NAME[k],
                  "from": "ledger" if n >= 3 else "default"}
    return out


def order_cost(fee: dict, value: float) -> float:
    return fee["fee"] + fee["fx"] * value


# --------------------------------------------------------------------------
# the deposit plan
# --------------------------------------------------------------------------

def te(values, target, S) -> float:
    """Tracking error between a book of these euro values and the target mix, a year."""
    np = _np()
    v = np.asarray(values, dtype=float)
    tot = float(v.sum())
    if tot <= 0:
        return 0.0
    d = v / tot - np.asarray(target, dtype=float)
    return math.sqrt(max(float(d @ S @ d), 0.0))


def best_order(values, prices, costs, target, S, budget: float, max_cost: float) -> Optional[dict]:
    """
    The one order, in whole shares, of something still below its target
    weight, that takes the most tracking error off the book per euro it
    costs, among those that cost at most `max_cost` of themselves and fit
    the budget with their costs. Per euro, so a fixed fee
    counts against a small order, and so more money alone never makes an
    order look better. None when nothing qualifies or helps.
    """
    np = _np()
    v = np.asarray(values, dtype=float)
    now = te(v, target, S)
    tot = float(v.sum())
    tw = np.asarray(target, dtype=float)
    best = None
    for i, p in enumerate(prices):
        if not p or p <= 0 or costs[i] is None:
            continue
        # Only what is below its target weight, and not for a target too small
        # to mean anything. Without this, cash or a bond fund (nearly riskless)
        # "helps" any book by diluting what it holds too much of.
        if tw[i] < MIN_TARGET or (tot > 0 and v[i] / tot >= tw[i] - 1e-12):
            continue
        f = costs[i]
        nmax = int((budget - f["fee"]) // (p * (1 + f["fx"])))
        if nmax < 1:
            continue
        ns = range(1, nmax + 1) if nmax <= 40 else sorted({1, *range(max(1, nmax - 40), nmax + 1)})
        for n in ns:
            val = n * p
            c = order_cost(f, val)
            if c / val > max_cost + 1e-12 or val + c > budget + 1e-9:
                continue
            # a whole share may overshoot; a bigger order not by more than a point
            if n > 1 and (v[i] + val) / (tot + val) > tw[i] + OVERSHOOT:
                continue
            v2 = v.copy()
            v2[i] += val
            after = te(v2, target, S)
            if after >= now - 1e-12:
                continue
            eff = (now - after) / (val + c)
            if best is None or eff > best["eff"] * (1 + 1e-9):
                best = {"i": i, "n": n, "value": val, "cost": c, "te": after, "eff": eff}
    return best


WAIT = 1.2                  # wait a month when next month's best order is this much better per euro
MIN_TARGET = 0.005          # targets under half a percent are noise, not something to buy
OVERSHOOT = 0.01            # how far past its target one order may take a holding


def _better_to_wait(v, o, prices, costs, target, S, cash: float, monthly: float, max_cost: float) -> bool:
    """
    One month of look-ahead. The cost limit can rule out the order that does
    most good until there is more money (three world-fund units at €3 a
    trade, say), and buying something small now would only spend that money.
    So: wait when next month's best order is clearly better per euro.
    """
    later = best_order(v, prices, costs, target, S, cash + monthly, max_cost)
    return bool(later) and later["eff"] > WAIT * o["eff"]


def plan(values, prices, costs, target, S, monthly: float = MONTHLY, months: int = MONTHS,
         max_cost: float = MAX_COST) -> list[dict]:
    """
    Month by month at today's prices: the deposit plus what was left over
    buys the best order, then the next best with what remains, up to
    MAX_ORDERS; what cannot be spent within the cost limit waits.
    """
    np = _np()
    v = np.asarray(values, dtype=float).copy()
    cash = 0.0
    out = []
    for m in range(1, months + 1):
        cash += monthly
        before = te(v, target, S)
        orders = []
        while len(orders) < MAX_ORDERS:
            o = best_order(v, prices, costs, target, S, cash, max_cost)
            if not o:
                break
            if not orders and _better_to_wait(v, o, prices, costs, target, S, cash, monthly, max_cost):
                break
            v[o["i"]] += o["value"]
            cash -= o["value"] + o["cost"]
            orders.append(o)
        out.append({"m": m, "orders": orders, "left": round(cash, 2), "te_before": before,
                    "te": te(v, target, S), "invested": float(v.sum())})
    return out


# --------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------

PX_TTL = 12 * 3600
_BG = threading.Lock()
_BG_KEYS: set = set()


def _load(syms: list[str]) -> dict:
    """Three years of daily closes, one request after another (never Yahoo from parallel threads)."""
    from . import bonds
    data = bonds._yf().download(syms, period="3y", interval="1d", auto_adjust=True, progress=False,
                                group_by="ticker", threads=False)
    got = {s: bonds._closes(data, s, True) for s in syms}
    if len(got.get(CAL) or {}) < YEAR or len(got.get(MARKET) or {}) < YEAR:
        raise RuntimeError("Yahoo returned no price history for the world fund")
    return {**bonds._pack(got), "fetched_at": time.time()}


def _refresh(key: str, syms: list[str]):
    with _BG:
        if key in _BG_KEYS:
            return
        _BG_KEYS.add(key)

    def run():
        try:
            store.cache_put(key, _load(syms))
        except Exception:
            pass
        finally:
            with _BG:
                _BG_KEYS.discard(key)
    threading.Thread(target=run, daemon=True, name="mvo-prices").start()


def prices(syms: list[str]) -> dict:
    """
    The closes, from the cache whatever its age, so the page and the home
    screen never wait on a download once there has been one. An answer older
    than twelve hours is served and refreshed behind it.
    """
    from . import bonds
    syms = sorted(syms)
    key = "mvo:px:" + hashlib.md5(",".join(syms).encode()).hexdigest()[:10]
    hit = store.cache_get(key)
    if isinstance(hit, dict) and hit.get("dates") and hit.get("cols"):
        if hit.get("_age_s", 1e9) > PX_TTL:
            _refresh(key, syms)
        return bonds._unpack(hit)
    v = _load(syms)
    store.cache_put(key, v)
    return bonds._unpack(v)


def _symbols(rows: list[dict]) -> list[str]:
    """Holdings, funds, the calendar, cash and the currencies: one download serves the page and the constellation."""
    from . import bonds
    syms = {r["t"] for r in rows} | {f["t"] for f in FUNDS} | {CAL, CASH, MARKET}
    syms |= {bonds.FX_OF[c] for c in {r["ccy"] for r in rows} if c in bonds.FX_OF}
    return sorted(syms)


def _holdings() -> dict:
    """Today's priced holdings, one row per ticker, with what a share costs in euros."""
    from . import service
    d = service.dashboard("SP500", light=True)
    if d.get("empty"):
        return {"rows": [], "cash": 0.0}
    venue = {}
    for t in sorted(store.load_txns(), key=lambda t: t.trade_dt):
        if t.kind == "BUY" and t.isin:
            venue[t.isin] = venue_class(t.exchange, t.currency)
    rows: dict[str, dict] = {}
    for p in d.get("positions", []):
        tk, v = p.get("ticker"), p.get("value_eur") or 0.0
        if not tk or p.get("excluded") or v <= 0 or p.get("kind") == "DERIVATIVE":
            continue
        r = rows.get(tk)
        qty = p.get("qty") or 0.0
        if r is None:
            rows[tk] = {"t": tk, "name": p.get("name"), "sector": p.get("sector") or "Unclassified",
                        "ccy": p.get("currency") or "EUR", "value": v, "qty": qty,
                        "px": v / qty if qty else None, "day": p.get("day_pct"),
                        "venue": venue.get(p.get("isin")) or venue_class(None, p.get("currency")),
                        "kind": "fund" if p.get("kind") == "ETF" else "share", "big": v}
        else:
            r["value"] += v
            r["qty"] += qty
            if v > r["big"]:
                r.update(name=p.get("name"), day=p.get("day_pct"), big=v,
                         px=v / qty if qty else r["px"])
    return {"rows": sorted(rows.values(), key=lambda r: -r["value"]),
            "cash": (d.get("totals") or {}).get("cash_eur") or 0.0}


def _returns(names: list[dict], px: dict, min_days: int = MIN_DAYS) -> dict:
    """Five-day euro log returns on New York's calendar, for every name with `min_days` of them."""
    from . import bonds
    np = _np()
    days = sorted(px.get(CAL) or {})
    cols, short = {}, []
    for r in names:
        m = bonds._to_eur(px.get(r["t"]) or {}, r.get("ccy") or "EUR", px)
        w = riskmap.windows(riskmap._filled(m, days))
        n = 0
        for x in reversed(w):
            if x is None:
                break
            n += 1
        if n < min_days:
            short.append({"t": r["t"], "days": n,
                          "why": "no price history" if not m else f"only {n} days of history"})
            continue
        cols[r["t"]] = (w, n, m)
    if len(cols) < 3:
        raise RuntimeError("fewer than three holdings have enough prices")
    L = min(LOOK, min(n for _, n, _ in cols.values()))
    keep = [r["t"] for r in names if r["t"] in cols]
    R = np.array([[cols[t][0][k] for t in keep] for k in range(len(days) - L, len(days))])
    last = {t: (cols[t][2][max(cols[t][2])] if cols[t][2] else None) for t in keep}
    return {"R": R, "keep": keep, "short": short, "days": days, "L": L, "last": last,
            "from": days[len(days) - L - H] if len(days) > L + H else days[0], "to": days[-1]}


def _rf(px: dict) -> float:
    s = px.get(CASH) or {}
    d = sorted(s)
    if len(d) < 70:
        return 0.02
    a, b = s[d[-64]], s[d[-1]]
    return (b / a) ** (YEAR / 63) - 1 if a > 0 else 0.02


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------

def views() -> list[dict]:
    with store.conn() as c:
        rows = c.execute("SELECT t, ret, conf, note, created FROM mvo_view ORDER BY t").fetchall()
    return [{"t": r[0], "ret": r[1], "conf": r[2], "note": r[3] or "", "created": r[4]} for r in rows]


def set_view(t: str, ret: float, conf: str, note: str = "") -> dict:
    t = (t or "").strip().upper()
    if not t or len(t) > 16:
        raise ValueError("a ticker, please")
    if conf not in CONF:
        raise ValueError(f"confidence must be one of {sorted(CONF)}")
    if not (-50.0 <= ret <= 100.0):
        raise ValueError("a yearly return between −50% and +100%, please")
    with store.conn() as c:
        c.execute("INSERT INTO mvo_view(t, ret, conf, note, created) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(t) DO UPDATE SET ret=excluded.ret, conf=excluded.conf, note=excluded.note, "
                  "created=excluded.created",
                  (t, float(ret), conf, (note or "")[:300], datetime.now().isoformat(timespec="seconds")))
    return {"ok": True, "views": views()}


def drop_view(t: str) -> dict:
    with store.conn() as c:
        c.execute("DELETE FROM mvo_view WHERE t = ?", ((t or "").upper(),))
    return {"ok": True, "views": views()}


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

def _label(t: str, name: str) -> str:
    from .morning import brands
    return (brands(t, name or "") or [t.split(".")[0]])[0]


def _mix(w, keep, meta, k: int = 12, floor: float = 0.005) -> list[dict]:
    order = sorted(range(len(keep)), key=lambda i: -w[i])
    return [{"t": keep[i], "label": meta[keep[i]]["label"], "w": round(float(w[i]) * 100, 2)}
            for i in order[:k] if w[i] >= floor]


def _pt(p: dict, rf: float) -> dict:
    return {"ret": round(p["ret"] * 100, 2), "vol": round(p["vol"] * 100, 2),
            "sharpe": round((p["ret"] - rf) / p["vol"], 3) if p["vol"] > 0 else None}


def one_fund(tan: dict, rf: float, vol: float) -> dict:
    """
    Tobin's separation (1958): with a riskless asset in the menu, every
    efficient mix is the same risky mix, the tangency portfolio, held with
    cash. Appetite for risk decides only how much is in it:

        share = target risk / the one fund's risk
        return = rf + share x (the one fund's return - rf)

    Long only and without borrowing, the line stops at the one fund itself;
    more risk than that means borrowing (a share above one) or leaving the
    line for the more concentrated mixes the frontier curves through.
    """
    sig, ret = tan["vol"], tan["ret"]
    share = vol / sig if sig > 0 else 1.0
    steps = [{"share": round(f * 100), "vol": round(f * sig * 100, 2), "ret": round((rf + f * (ret - rf)) * 100, 2)}
             for f in (0.25, 0.5, 0.75, 1.0)]
    return {"vol": round(sig * 100, 2), "ret": round(ret * 100, 2), "rf": round(rf * 100, 2),
            "sharpe": round((ret - rf) / sig, 3) if sig > 0 else None,
            "share_at": round(min(share, 1.0) * 100, 1), "cash_at": round(max(0.0, 1 - share) * 100, 1),
            "reach": share <= 1.0 + 1e-9, "lever": round(share, 2),
            "ret_at": round((rf + min(share, 1.0) * (ret - rf)) * 100, 2),
            "ret_levered": round((rf + share * (ret - rf)) * 100, 2), "steps": steps}


CASH_T = "CASH"             # the plan's cash: money kept aside, at no cost


def with_cash(S, vals, prices, costs, target_w):
    """The plan's inputs with a riskless cash line added: no variance, no fee, a euro a unit."""
    np = _np()
    n = len(vals)
    S2 = np.zeros((n + 1, n + 1))
    S2[:n, :n] = S
    return (S2, np.append(np.asarray(vals, dtype=float), 0.0), list(prices) + [1.0],
            list(costs) + [{"fee": 0.0, "fx": 0.0}], target_w)


def _resample(Rm, mode, m, rf, erp, vw, gamma, u, mu0, S0):
    """Draw the history again in half-year blocks and solve at the same risk appetite."""
    np = _np()
    rng = np.random.default_rng(SEED)
    L, n = Rm.shape
    cloud, ws = [], []
    w0 = None
    for _ in range(RESAMPLES):
        idx = []
        while len(idx) < L:
            s = int(rng.integers(0, L))
            idx.extend(((s + j) % L) for j in range(BLOCK))
        Rb = Rm[np.array(idx[:L])]
        Sb = riskmap.shrunk_cov(Rb) * PER_YEAR
        if mode == "history":
            mub = Rb.mean(axis=0) * PER_YEAR + 0.5 * np.diag(Sb)
        else:
            pib, _ = prior(Sb, m, rf, erp)
            mub = posterior(pib, Sb, vw) if mode == "views" else pib
        w = solve(mub, Sb, gamma, u, w0=w0)
        w0 = w
        r, v = stats(w, mu0, S0)
        cloud.append([round(v * 100, 2), round(r * 100, 2)])
        ws.append(w)
    return cloud, np.array(ws)


_MEMO: dict = {}
_MEMO_TTL = 300


def view(**kw) -> dict:
    """build(), remembered for five minutes per setting: the page asks again on every control."""
    key = json.dumps([kw, views()], sort_keys=True, default=str)
    now = time.time()
    hit = _MEMO.get(key)
    if hit and now - hit[0] < _MEMO_TTL:
        return hit[1]
    out = build(**kw)
    if len(_MEMO) > 32:
        _MEMO.clear()
    _MEMO[key] = (now, out)
    return out


def build(mode: str = "market", target: str = "same", cap: float = CAP * 100,
          monthly: float = MONTHLY, max_cost: float = MAX_COST * 100,
          erp: Optional[float] = None) -> dict:
    np = _np()
    t0 = time.time()
    cap = min(max(float(cap), 2.0), 100.0) / 100
    max_cost = min(max(float(max_cost), 0.1), 5.0) / 100
    monthly = min(max(float(monthly), 0.0), 100000.0)
    hold = _holdings()
    if not hold["rows"]:
        return {"available": False, "why": "No priced holdings."}
    held = {r["t"] for r in hold["rows"]}
    names = [dict(r) for r in hold["rows"]]
    for f in FUNDS:
        if f["t"] not in held:
            names.append({"t": f["t"], "name": f["name"], "sector": f["group"], "ccy": "EUR", "value": 0.0,
                          "qty": 0.0, "px": None, "day": None, "venue": f["venue"], "kind": "fund"})
    fundset = {f["t"]: f for f in FUNDS}
    px = prices(_symbols(hold["rows"]))
    data = _returns(names, px)
    keep, R = data["keep"], data["R"]
    if MARKET not in keep:
        return {"available": False, "why": "The world fund has no price history."}
    S = riskmap.shrunk_cov(R) * PER_YEAR
    m = keep.index(MARKET)
    rf = _rf(px)
    erp_hist = premium()
    erp = erp_hist if erp is None else min(max(float(erp) / 100, 0.0), 0.15)
    pi, beta = prior(S, m, rf, erp)
    delta = erp / float(S[m, m])

    meta = {}
    for r in names:
        if r["t"] in keep:
            f = fundset.get(r["t"])
            meta[r["t"]] = {**r, "label": f["group"] if f else _label(r["t"], r["name"]),
                            "group": f["group"] if f else r["sector"], "fund": bool(f)}

    vw_all = views()
    vw = [(keep.index(v["t"]), v["ret"] / 100, CONF[v["conf"]]) for v in vw_all if v["t"] in keep]
    if mode == "views" and not vw:
        mode = "market"
    if mode == "history":
        mu = R.mean(axis=0) * PER_YEAR + 0.5 * np.diag(S)
    elif mode == "views":
        mu = posterior(pi, S, vw)
    else:
        mode, mu = "market", pi.copy()

    u = np.array([1.0 if meta[t]["fund"] else cap for t in keep])
    if float(u.sum()) < 1.0:
        u = np.maximum(u, 1.0 / len(u))
    front = frontier(mu, S, u)

    vals = np.array([meta[t]["value"] for t in keep], dtype=float)
    wb = vals / vals.sum()
    book_r, book_v = stats(wb, mu, S)
    book = {"ret": book_r, "vol": book_v, "w": wb}
    tan = tangency(mu, S, u, rf, front)
    same_risk = at_vol(mu, S, u, book_v, front)
    same_ret = at_ret(mu, S, u, book_r, front)
    least = front[0]
    one = one_fund(tan, rf, book_v)
    if target == "tangency":
        tgt = tan
    elif target == "one_fund":
        # The one fund scaled to the book's risk, the rest in cash; above the
        # one fund's own risk it is the one fund alone (no borrowing).
        a = min(1.0, book_v / tan["vol"]) if tan["vol"] > 0 else 1.0
        tgt = {**tan, "w": tan["w"] * a, "cash": 1.0 - a, "ret": rf + a * (tan["ret"] - rf), "vol": a * tan["vol"]}
    elif target == "least":
        tgt = least
    elif target == "same":
        tgt = same_risk
    else:
        try:
            tgt = at_vol(mu, S, u, float(target) / 100, front)
            target = f"{float(target):.1f}"
        except ValueError:
            target, tgt = "same", same_risk
    gamma_t = tgt["gamma"] if tgt["gamma"] > 0 else GAMMAS[0]

    cloud, ws = _resample(R, mode, m, rf, erp, vw, gamma_t, u, mu, S)
    q10, q50, q90 = (np.quantile(ws, q, axis=0) for q in (0.1, 0.5, 0.9))

    # Implied views: how the book's weights share out the return the market
    # expects of the book as a whole. With the book's own risk aversion, so the
    # total is the same and only the split differs from the market's.
    delta_b = (float(wb @ pi) - rf) / max(float(wb @ S @ wb), 1e-12)
    imp = implied(S, wb, rf, delta_b)

    assets = []
    for i, t in enumerate(keep):
        r = meta[t]
        sd = math.sqrt(float(S[i, i]))
        assets.append({
            "t": t, "label": r["label"], "name": r["name"], "group": r["group"], "fund": r["fund"],
            "held": r["value"] > 0, "w_book": round(float(wb[i]) * 100, 2),
            "vol": round(sd * 100, 1), "beta": round(float(beta[i]), 2),
            "pi": round(float(pi[i]) * 100, 2), "mu": round(float(mu[i]) * 100, 2),
            "hist": round(float(R[:, i].mean() * PER_YEAR + 0.5 * S[i, i]) * 100, 1),
            "implied": round(float(imp[i]) * 100, 2),
            "w_tgt": round(float(tgt["w"][i]) * 100, 2),
            "q10": round(float(q10[i]) * 100, 1), "q50": round(float(q50[i]) * 100, 1),
            "q90": round(float(q90[i]) * 100, 1),
            "px": round(r["px"], 2) if r.get("px") else (round(data["last"][t], 2) if data["last"].get(t) else None),
            "venue": r["venue"],
        })

    # The plan. Aimed at the one fund it may also keep money aside as cash.
    fee = fees()
    price = [a["px"] for a in assets]
    costs = [fee.get(a["venue"]) or fee["OTHER"] for a in assets]
    p_S, p_vals, p_price, p_costs, p_w, p_keep = S, vals, price, costs, tgt["w"], list(keep)
    if tgt.get("cash", 0.0) > 1e-6:
        p_S, p_vals, p_price, p_costs, _ = with_cash(S, vals, price, costs, None)
        p_w = np.append(tgt["w"], tgt["cash"])
        p_keep = p_keep + [CASH_T]
    label_of = lambda t: "cash, kept aside" if t == CASH_T else meta[t]["label"]
    months = plan(p_vals, p_price, p_costs, p_w, p_S, monthly, MONTHS, max_cost)
    plan_out = []
    for mo in months:
        plan_out.append({"m": mo["m"], "left": mo["left"], "te": round(mo["te"] * 100, 2),
                         "orders": [{"t": p_keep[o["i"]], "label": label_of(p_keep[o["i"]]), "n": o["n"],
                                     "value": round(o["value"], 2), "cost": round(o["cost"], 2),
                                     "cost_pct": round(o["cost"] / o["value"] * 100, 2),
                                     "px": round(p_price[o["i"]], 2), "cash": p_keep[o["i"]] == CASH_T}
                                    for o in mo["orders"]]})
    te0 = te(p_vals, p_w, p_S)
    spent = sum(o["value"] + o["cost"] for mo in plan_out for o in mo["orders"])
    paid = sum(o["cost"] for mo in plan_out for o in mo["orders"])

    out = {
        "available": True, "asof": data["to"], "from": data["from"], "weeks": round(data["L"] / H),
        "mode": mode, "target": target, "cap": round(cap * 100, 1), "monthly": monthly,
        "max_cost": round(max_cost * 100, 2), "rf": round(rf * 100, 2),
        "erp": round(erp * 100, 2), "erp_hist": round(erp_hist * 100, 2), "delta": round(delta, 2),
        "delta_book": round(delta_b, 2), "beta_book": round(float(wb @ beta), 2),
        "tau": TAU, "conf": CONF, "shrink": riskmap.SHRINK, "horizon_days": H,
        "market": {"t": MARKET, "vol": round(math.sqrt(float(S[m, m])) * 100, 1)},
        "n": len(keep), "n_held": int(sum(1 for t in keep if meta[t]["value"] > 0)),
        "short": [{**s, "label": next((_label(r["t"], r["name"]) for r in names if r["t"] == s["t"]), s["t"]),
                   "value": round(next((r["value"] for r in names if r["t"] == s["t"]), 0.0), 2)}
                  for s in data["short"] if any(r["t"] == s["t"] and r["value"] > 0 for r in names)],
        "covered_pct": round(float(vals.sum()) / sum(r["value"] for r in hold["rows"]) * 100, 1),
        "frontier": [{"vol": round(p["vol"] * 100, 2), "ret": round(p["ret"] * 100, 2)} for p in front],
        "book": {**_pt(book, rf), "value": round(float(vals.sum()), 2)},
        "points": {"tangency": {**_pt(tan, rf), "mix": _mix(tan["w"], keep, meta)},
                   "same_risk": {**_pt(same_risk, rf), "mix": _mix(same_risk["w"], keep, meta)},
                   "same_ret": {**_pt(same_ret, rf), "mix": _mix(same_ret["w"], keep, meta)},
                   "least": {**_pt(least, rf), "mix": _mix(least["w"], keep, meta)},
                   "target": {**_pt(tgt, rf), "mix": _mix(tgt["w"], keep, meta, k=20),
                              "cash": round(tgt.get("cash", 0.0) * 100, 1)}},
        "one_fund": {**one, "mix": _mix(tan["w"], keep, meta, k=12)},
        "cloud": cloud,
        "assets": assets,
        "views": vw_all,
        "fees": fee,
        "plan": {"months": plan_out, "te0": round(te0 * 100, 2), "spent": round(spent, 2),
                 "paid": round(paid, 2), "deposits": round(monthly * MONTHS, 2)},
        "took_s": round(time.time() - t0, 2),
    }
    out["say"] = _say(out)
    return out


def _say(o: dict) -> list[str]:
    b, sr, ret, tan = o["book"], o["points"]["same_risk"], o["points"]["same_ret"], o["points"]["tangency"]
    out = []
    gain = sr["ret"] - b["ret"]
    less = b["vol"] - ret["vol"]
    if gain > 0.05:
        out.append(f"At the risk your book takes now ({b['vol']:.1f}% a year), the best mix of these holdings and funds "
                   f"is expected to earn {sr['ret']:.1f}% a year against your {b['ret']:.1f}%: "
                   f"{gain:.1f} points more for the same swings.")
    if less > 0.3:
        out.append(f"The same {b['ret']:.1f}% is available with {ret['vol']:.1f}% of risk instead of {b['vol']:.1f}%.")
    if o["mode"] == "market":
        out.append("These are the market's returns, so the gap is only the risk the market does not pay for: "
                   "the part of each share's swing that is its own. Your picks earn it back only if they beat the "
                   "market's estimate, which is what a view says.")
    elif o["mode"] == "history":
        out.append("These are the past returns, and the frontier believes them: it buys whatever went up most. "
                   "This is the textbook way Markowitz goes wrong.")
    of = o.get("one_fund") or {}
    if of:
        if of["reach"]:
            out.append(f"The one-fund answer: {of['share_at']:.0f}% in the one fund and {of['cash_at']:.0f}% in cash takes "
                       f"your book's risk and is expected to earn {of['ret_at']:.1f}% a year.")
        else:
            out.append(f"Your book swings more ({b['vol']:.1f}%) than the one fund ({of['vol']:.1f}%). On the one-fund line that "
                       f"much risk means {of['lever'] * 100:.0f}% in the one fund, borrowing the rest (expected "
                       f"{of['ret_levered']:.1f}% before the cost of borrowing); without borrowing, the line stops at the one fund "
                       f"and only more concentrated mixes swing more.")
    top = tan["mix"][:3]
    if top and o["mode"] == "market" and top[0]["t"] == MARKET and top[0]["w"] >= 95:
        out.append("With the market's returns the best mix per unit of risk is always the market itself: here the world "
                   f"fund, {top[0]['w']:.0f}%. That is Tobin's one fund: with cash beside it, every lower risk is the "
                   "world fund plus cash (the green line), and above it only borrowing, or the shares that carry most "
                   "market risk, swing more.")
    elif top:
        out.append("The best mix per unit of risk is " + ", ".join(f"{x['label']} {x['w']:.0f}%" for x in top) + ".")
    return out


# --------------------------------------------------------------------------
# the home screen's constellation
# --------------------------------------------------------------------------

def embed3(C):
    """
    Classical scaling: points whose distances are sqrt(2(1 - correlation)),
    so holdings that move together sit together. Three dimensions, scaled so
    that most of them sit inside a unit ball and none far beyond it.
    """
    np = _np()
    C = np.asarray(C, dtype=float)
    n = len(C)
    D2 = 2.0 * (1.0 - C)
    J = np.eye(n) - np.ones((n, n)) / n
    B = -0.5 * J @ D2 @ J
    val, vec = np.linalg.eigh(B)
    order = np.argsort(-val)[:3]
    X = vec[:, order] * np.sqrt(np.maximum(val[order], 0.0))
    if X.shape[1] < 3:
        X = np.column_stack([X, np.zeros((n, 3 - X.shape[1]))])
    # Scaled by the typical star, not the farthest: one holding that moves
    # with nothing would otherwise squeeze the rest into the middle. Beyond
    # the unit sphere distances are compressed, so it stays in the picture.
    rad = np.sqrt((X ** 2).sum(axis=1))
    r = float(np.quantile(rad, 0.85)) or float(rad.max()) or 1.0
    X = X / r
    rad = rad / r
    far = rad > 1.0
    if far.any():
        squeeze = (1.0 + 0.35 * np.tanh((rad[far] - 1.0) / 0.35)) / rad[far]
        X[far] = X[far] * squeeze[:, None]
    return X, val[order] / max(float(val[val > 0].sum()), 1e-12)


def procrustes(A, B):
    """
    The rotation (or reflection) that best lays A onto B, both n x 3: the
    orthogonal Procrustes answer U V' from the SVD of A'B. Classical scaling
    returns each window's picture in arbitrary axes; this keeps a time-lapse
    from spinning or flipping between frames for no reason.
    """
    np = _np()
    U, _, Vt = np.linalg.svd(A.T @ B)
    return U @ Vt


def smacof(D, W, X0, iters: int = 250):
    """
    Weighted stress minimisation (de Leeuw's SMACOF, the Guttman transform):
    moves the points so their distances match D, pair by pair, as closely as
    the weights W ask. Classical scaling keeps only the three directions with
    most variance and squeezes the rest; this refines it, and lets the world
    star's distances weigh more, so that "near the world star" means "moves
    with the market".
    """
    np = _np()
    n = len(D)
    V = -W.copy()
    np.fill_diagonal(V, 0.0)
    np.fill_diagonal(V, -V.sum(axis=1))
    Vp = np.linalg.pinv(V)
    X = X0.copy()
    for _ in range(iters):
        d = np.sqrt(((X[:, None, :] - X[None, :, :]) ** 2).sum(axis=2))
        with np.errstate(divide="ignore", invalid="ignore"):
            Bm = np.where(d > 1e-12, -W * D / d, 0.0)
        np.fill_diagonal(Bm, 0.0)
        np.fill_diagonal(Bm, -Bm.sum(axis=1))
        Xn = Vp @ Bm @ X
        if float(np.abs(Xn - X).max()) < 1e-7:
            X = Xn
            break
        X = Xn
    return X


def fidelity(D, X) -> float:
    """How well the picture's distances follow the true ones: their correlation, pair by pair."""
    np = _np()
    n = len(D)
    iu = np.triu_indices(n, 1)
    d = np.sqrt(((X[:, None, :] - X[None, :, :]) ** 2).sum(axis=2))[iu]
    return float(np.corrcoef(d, D[iu])[0, 1])


WORLD_WEIGHT = 6.0          # how much more a distance to the world star counts in the picture


def place3(C, world: Optional[int] = None):
    """Classical scaling refined by SMACOF; scaled so most stars sit inside the unit sphere."""
    np = _np()
    D = np.sqrt(np.maximum(2.0 * (1.0 - np.asarray(C, dtype=float)), 0.0))
    X0, _ = embed3(C)
    scale = float(np.median(D[np.triu_indices(len(D), 1)])) / max(float(np.median(
        np.sqrt(((X0[:, None, :] - X0[None, :, :]) ** 2).sum(axis=2))[np.triu_indices(len(D), 1)])), 1e-9)
    W = np.ones_like(D)
    if world is not None:
        W[world, :] = W[:, world] = WORLD_WEIGHT
    np.fill_diagonal(W, 0.0)
    X = smacof(D, W, X0 * scale)
    fid = fidelity(D, X)
    X = X - X.mean(axis=0)
    rad = np.sqrt((X ** 2).sum(axis=1))
    r = float(np.quantile(rad, 0.85)) or float(rad.max()) or 1.0
    X, rad = X / r, rad / r
    far = rad > 1.0
    if far.any():
        X[far] = X[far] * ((1.0 + 0.35 * np.tanh((rad[far] - 1.0) / 0.35)) / rad[far])[:, None]
    return X, fid


def _top_links(C, idx: list, skip: set) -> list:
    pairs = sorted(((float(C[a, b]), a, b) for a in range(len(idx)) for b in range(a + 1, len(idx))
                    if C[a, b] >= LINK and idx[a] not in skip and idx[b] not in skip), reverse=True)
    per, out = {}, []
    for c, a, b in pairs:
        if per.get(a, 0) >= 4 or per.get(b, 0) >= 4:
            continue
        per[a] = per.get(a, 0) + 1
        per[b] = per.get(b, 0) + 1
        out.append((idx[a], idx[b], round(c, 2)))
    return out


FRAMES = 9                  # the time-lapse: a year ending each quarter, the last two years and now
QSTEP = 63                  # trading days in a quarter


def stars() -> dict:
    """
    The constellation: today's holdings and the world fund, placed by how
    they moved together over the past year, and the same picture for the
    year to each of the last eight quarter-ends, laid onto each other.
    """
    np = _np()
    from . import bonds
    hold = _holdings()
    rows = hold["rows"]
    if len(rows) < 3:
        return {"available": False, "why": "Fewer than three priced holdings."}
    px = prices(_symbols(rows))
    names = [r for r in rows if r["t"] != MARKET] + [{"t": MARKET, "name": "MSCI World", "ccy": "EUR", "value": 0.0,
                                                       "sector": "World shares", "day": None, "world": True}]
    days = sorted(px.get(CAL) or {})
    cols = {}
    for r in names:
        m = bonds._to_eur(px.get(r["t"]) or {}, r.get("ccy") or "EUR", px)
        if m:
            cols[r["t"]] = riskmap.windows(riskmap._filled(m, days))
    meta = {r["t"]: r for r in names}
    order = [r["t"] for r in names if r["t"] in cols]
    tot = sum(r["value"] for r in rows) or 1.0

    def frame(end: int):
        lo = end - YEAR + 1
        if lo < H:
            return None
        ok = [t for t in order if all(cols[t][k] is not None for k in range(lo, end + 1))]
        if len(ok) < 3 or MARKET not in ok:
            return None
        Rm = np.array([[cols[t][k] for t in ok] for k in range(lo, end + 1)])
        C = np.corrcoef(Rm, rowvar=False)
        X, fid = place3(C, ok.index(MARKET))
        return {"end": end, "ok": ok, "C": C, "X": X, "fid": fid}

    now = frame(len(days) - 1)
    if not now:
        return {"available": False, "why": "Not a year of prices for the holdings and the world fund."}
    # Today's picture keeps the biggest holding on the near side, so it looks the same each day.
    X = now["X"]
    big = max(range(len(now["ok"])), key=lambda i: meta[now["ok"][i]].get("value") or 0)
    for k in range(3):
        if X[big, k] < 0:
            X[:, k] = -X[:, k]
    frames = [now]
    end = len(days) - 1
    for _ in range(FRAMES - 1):
        end -= QSTEP
        f = frame(end)
        if not f:
            break
        nxt = frames[-1]
        common = [t for t in f["ok"] if t in nxt["ok"]]
        if len(common) >= 4:
            A = np.array([f["X"][f["ok"].index(t)] for t in common])
            B = np.array([nxt["X"][nxt["ok"].index(t)] for t in common])
            f["X"] = f["X"] @ procrustes(A, B)
        frames.append(f)
    frames.reverse()

    keep = now["ok"]
    C = now["C"]
    world = {MARKET}
    sub = [i for i, t in enumerate(keep) if t not in world]
    groups = riskmap.clusters(C[np.ix_(sub, sub)], riskmap.LINK)
    gid = {}
    for g, mem in enumerate(sorted(groups, key=lambda g: -sum(meta[keep[sub[i]]]["value"] for i in g))):
        for i in mem:
            gid[sub[i]] = g if len(mem) > 1 else -1
    wi = keep.index(MARKET)
    nodes = []
    for i, t in enumerate(keep):
        r = meta[t]
        nodes.append({"t": t, "label": "World" if t == MARKET else _label(t, r["name"]), "sector": r.get("sector"),
                      "w": round((r.get("value") or 0) / tot * 100, 2), "day": r.get("day"),
                      "x": round(float(X[i, 0]), 4), "y": round(float(X[i, 1]), 4), "z": round(float(X[i, 2]), 4),
                      "g": gid.get(i, -1), "world": t == MARKET, "cw": round(float(C[i, wi]), 2)})
    index = {t: i for i, t in enumerate(keep)}
    links = [{"a": index[a], "b": index[b], "c": c} for a, b, c in _top_links(C, keep, world)]
    cl = []
    for g in sorted({n["g"] for n in nodes if n["g"] >= 0}):
        mem = [n for n in nodes if n["g"] == g]
        cl.append({"g": g, "labels": [n["label"] for n in sorted(mem, key=lambda n: -n["w"])],
                   "w": round(sum(n["w"] for n in mem), 1)})
    lapse = []
    for f in frames:
        pos = [None] * len(keep)
        for j, t in enumerate(f["ok"]):
            if t in index:
                pos[index[t]] = [round(float(v), 4) for v in f["X"][j]]
        lapse.append({"d": days[f["end"]], "pos": pos,
                      "links": [[index[a], index[b], c] for a, b, c in _top_links(f["C"], f["ok"], world)
                                if a in index and b in index],
                      "n": sum(1 for p in pos if p)})
    return {"available": True, "asof": days[-1], "from": days[now["end"] - YEAR + 1], "n": len(nodes) - 1,
            "nodes": nodes, "links": links, "clusters": cl, "link": LINK, "world": wi,
            "fidelity": round(now["fid"], 2), "frames": lapse,
            "short": [r["t"] for r in names if r["t"] not in keep and r["t"] != MARKET]}
