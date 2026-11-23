"""
OPT's strategies: several options at once, worked out on the real chain.

A strategy is legs: calls, puts and sometimes a hundred shares, bought or
sold, at one expiry or two. Each template places its legs where the textbook
does, by delta on the chain's own smile (a 30-delta call is the strike the
market gives about a 30% chance of finishing in the money), snapped to a
strike that is listed and priced. Any leg can then be moved.

For the whole position:

  COST        what it costs or pays to open at the middle of bid and ask, and
              what crossing the spread adds; DEGIRO's fee per contract.
  PAYOFF      at the first expiry, today, and half way, from Black's formula
              with each leg at the volatility its own price implies (the
              later leg of a calendar keeps its own).
  ODDS        the chance of a profit at the first expiry, from the
              distribution the chain itself implies (OPT's range).
  EDGE        the expected result at the market's odds (about the spread you
              pay: options are priced to break even on average), and at the
              odds the share's own recent swings would give. The gap between
              the two is the volatility premium: sellers collect it when
              options price more movement than the share delivers.
  GREEKS      delta in shares, gamma, theta a day, vega a point, in dollars.
  CAPITAL     cash or margin tied up: the shares, the strike for a secured
              put, the width less the credit for a spread.

Contracts are a hundred shares. On a private investor's book that matters more than any
greek: a covered call on Microsoft needs a hundred Microsoft shares. The page
says what each strategy needs against what is held.

THE SCREENER reads the chains OPT stored for the holdings during the last
session, never a download: for each, the 30-delta call and put about a month
out, what they pay a year as a share of the price, the chance of being called
or assigned, implied against realised volatility, IV rank once a month of
readings exists, and whether earnings fall before the expiry.
"""

from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta
from typing import Optional

from . import options as O
from . import store

FEE_USD = 0.75              # DEGIRO's published fee for a US option, per contract (check your tariff)
FEE_NOTE = ("DEGIRO lists US options at $0.75 a contract, plus a monthly options connectivity fee; "
            "check your own tariff. Exchange fees are not included.")
TARGET_DAYS = 35
GRID = 161

# Each leg: type C/P/S (shares), side +1 bought / −1 sold, which expiry, and
# where: "d" a call delta (for a put, the put's delta as a positive number),
# "atm", or "pct" of the share price. n is contracts (or lots of 100 shares).
TEMPLATES = {
    "long_call": {"name": "Long call", "view": "bullish", "legs": [("C", 1, "near", {"d": 0.50})],
                  "what": "Buy a call: the right to buy the shares at the strike until expiry.",
                  "when": "You expect a rise, soon and large enough to cover what the call cost.",
                  "risk": "All of the premium if the share is below the strike at expiry; time works against you every day."},
    "bull_call": {"name": "Bull call spread", "view": "bullish",
                  "legs": [("C", 1, "near", {"d": 0.50}), ("C", -1, "near", {"d": 0.25})],
                  "what": "Buy a call and sell a higher one: a cheaper bet on a rise, capped at the higher strike.",
                  "when": "You expect a moderate rise and want to pay less than for a call alone.",
                  "risk": "The net debit; gains stop at the upper strike."},
    "bull_put": {"name": "Bull put spread", "view": "bullish",
                 "legs": [("P", -1, "near", {"d": 0.30}), ("P", 1, "near", {"d": 0.15})],
                 "what": "Sell a put and buy a lower one for a credit you keep if the share stays above the upper strike.",
                 "when": "You expect the share to hold up, and options are dear.",
                 "risk": "The width between the strikes less the credit, if the share falls below the lower one."},
    "csp": {"name": "Cash-secured put", "view": "bullish",
            "legs": [("P", -1, "near", {"d": 0.30})],
            "what": "Sell a put with the cash to buy the shares at the strike set aside.",
            "when": "You would happily buy the shares lower and are paid to wait. Cboe's PUT and WPUT indexes do this on the S&P 500.",
            "risk": "If the share falls through the strike you buy it there, whatever it is worth by then."},
    "long_put": {"name": "Long put", "view": "bearish", "legs": [("P", 1, "near", {"d": 0.50})],
                 "what": "Buy a put: the right to sell at the strike.",
                 "when": "You expect a fall, or want insurance you can hold without the shares.",
                 "risk": "All of the premium if the share is above the strike at expiry."},
    "bear_put": {"name": "Bear put spread", "view": "bearish",
                 "legs": [("P", 1, "near", {"d": 0.50}), ("P", -1, "near", {"d": 0.25})],
                 "what": "Buy a put and sell a lower one: a cheaper bet on a fall, capped at the lower strike.",
                 "when": "You expect a moderate fall.",
                 "risk": "The net debit; gains stop at the lower strike."},
    "bear_call": {"name": "Bear call spread", "view": "bearish",
                  "legs": [("C", -1, "near", {"d": 0.30}), ("C", 1, "near", {"d": 0.15})],
                  "what": "Sell a call and buy a higher one for a credit you keep if the share stays below the lower strike.",
                  "when": "You expect the share to stall, and options are dear.",
                  "risk": "The width less the credit, if the share rises past the upper strike."},
    "covered_call": {"name": "Covered call", "view": "income",
                     "legs": [("S", 1, "near", {}), ("C", -1, "near", {"d": 0.30})],
                     "what": "Own a hundred shares and sell a call on them: income now, the upside sold above the strike.",
                     "when": "You hold the shares and expect them to drift. Cboe's BXM, BXY and BXMD indexes do this on the S&P 500.",
                     "risk": "Nearly all of a fall in the shares, less the premium; a big rise is given away above the strike."},
    "protective_put": {"name": "Protective put", "view": "protection",
                       "legs": [("S", 1, "near", {}), ("P", 1, "near", {"d": 0.25})],
                       "what": "Own a hundred shares and buy a put under them: a floor for a price.",
                       "when": "You want to keep the shares through something that could hurt. Cboe's PPUT index does this with a 5% out-of-the-money put.",
                       "risk": "The put's cost, every time; it pays only if the fall goes through the strike."},
    "collar": {"name": "Collar", "view": "protection",
               "legs": [("S", 1, "near", {}), ("P", 1, "near", {"d": 0.25}), ("C", -1, "near", {"d": 0.25})],
               "what": "Own the shares, buy a put below and pay for it by selling a call above: a band with a floor and a ceiling.",
               "when": "You want protection for little or no cost and can live without the upside. Cboe's CLL and CLLZ indexes are collars.",
               "risk": "The fall down to the put's strike; everything above the call's."},
    "iron_condor": {"name": "Iron condor", "view": "calm",
                    "legs": [("P", 1, "near", {"d": 0.05}), ("P", -1, "near", {"d": 0.20}),
                             ("C", -1, "near", {"d": 0.20}), ("C", 1, "near", {"d": 0.05})],
                    "what": "Sell a put and a call outside the likely range and buy further ones as wings: paid to be right that it stays in between.",
                    "when": "You expect a quiet month and options are dear. Cboe's CNDR index sells 15 to 20-delta options and buys 5-delta wings.",
                    "risk": "A wing's width less the credit, if the share leaves the range either way."},
    "iron_fly": {"name": "Iron butterfly", "view": "calm",
                 "legs": [("P", 1, "near", {"pct": -0.05}), ("P", -1, "near", {"atm": True}),
                          ("C", -1, "near", {"atm": True}), ("C", 1, "near", {"pct": 0.05})],
                 "what": "Sell the at-the-money put and call and buy 5% wings: a bigger credit than a condor, needing the share to stay closer.",
                 "when": "You expect the share near today's price at expiry. Cboe's BFLY index does exactly this monthly.",
                 "risk": "The wing width less the credit; profit only within the credit's reach of the strike."},
    "calendar": {"name": "Calendar spread", "view": "calm",
                 "legs": [("C", -1, "near", {"atm": True}), ("C", 1, "far", {"atm": True})],
                 "what": "Sell a near call and buy the same strike further out: near options lose value faster than far ones.",
                 "when": "You expect the share to sit near the strike until the near expiry, and the far options are not dear.",
                 "risk": "The debit, if the share runs far from the strike either way; a fall in implied volatility hurts the far leg."},
    "straddle": {"name": "Long straddle", "view": "move",
                 "legs": [("C", 1, "near", {"atm": True}), ("P", 1, "near", {"atm": True})],
                 "what": "Buy the at-the-money call and put: paid by a big move either way.",
                 "when": "You expect a move larger than the options price, an event say, without knowing its direction.",
                 "risk": "Both premiums, if the share ends near the strike; this is what the move must beat."},
    "strangle": {"name": "Long strangle", "view": "move",
                 "legs": [("C", 1, "near", {"d": 0.25}), ("P", 1, "near", {"d": 0.25})],
                 "what": "Buy an out-of-the-money call and put: cheaper than a straddle, needing a bigger move.",
                 "when": "You expect a very large move either way.",
                 "risk": "Both premiums, if the share ends between the strikes."},
}
VIEWS = {"bullish": "If you expect a rise", "bearish": "If you expect a fall", "income": "Income on shares you hold",
         "protection": "Protecting shares you hold", "calm": "If you expect it to stay put", "move": "If you expect a big move"}


# --------------------------------------------------------------------------
# placing the legs
# --------------------------------------------------------------------------

def pick_expiries(rows: list[dict], days: float = TARGET_DAYS) -> tuple[Optional[dict], Optional[dict]]:
    """The expiry nearest to a month out (at least two weeks), and one at least 25 days after it."""
    ok = [r for r in rows if r["days"] >= 14]
    if not ok:
        return None, None
    near = min(ok, key=lambda r: abs(r["days"] - days))
    far = next((r for r in rows if r["days"] >= near["days"] + 25), None)
    return near, far


def _priced(r: list) -> Optional[float]:
    bid, ask, last = r[1], r[2], r[3]
    if bid > 0 and ask >= bid:
        return 0.5 * (bid + ask)
    return last if last > 0 else None


def snap(strikes: list[list], target: float) -> Optional[list]:
    """The listed, priced strike nearest the target."""
    ok = [r for r in strikes if _priced(r)]
    return min(ok, key=lambda r: abs(r[0] - target)) if ok else None


def target_strike(spec: dict, typ: str, er: dict, a: dict, S: float) -> Optional[float]:
    F = er["fwd"]
    if spec.get("atm"):
        return F
    if spec.get("pct") is not None:
        return S * (1 + spec["pct"])
    d = spec.get("d")
    if d is None:
        return None
    call_delta = d if typ == "C" else 1.0 - d
    i = next((j for j, x in enumerate(O.DELTAS) if abs(x - round(call_delta * 20) / 20) < 1e-9), None)
    ix = next((j for j, r in enumerate(a.get("expiries") or []) if r["exp"] == er["exp"]), None)
    m = None
    if i is not None and ix is not None:
        m = (a.get("m_delta") or [[]])[ix][i] if ix < len(a.get("m_delta") or []) else None
    if m:
        return m * F
    # Off the smile's grid (a 5-delta wing): Black's formula at the at-the-money volatility.
    vol, T = er["atm"], er["t"]
    s = vol * math.sqrt(T)
    from statistics import NormalDist
    z = NormalDist().inv_cdf(min(max(call_delta, 1e-4), 1 - 1e-4))
    return F * math.exp(-(z * s) + 0.5 * s * s)


def default_legs(tpl: str, ch: dict, a: dict) -> list[dict]:
    t = TEMPLATES[tpl]
    near, far = pick_expiries(a["expiries"])
    if not near:
        raise O.NoOptions("no expiry two weeks or more away has enough priced strikes")
    if any(l[2] == "far" for l in t["legs"]) and not far:
        raise O.NoOptions("no expiry far enough beyond the near one for a calendar")
    S = ch["spot"]
    out, used = [], set()
    for typ, side, which, spec in t["legs"]:
        er = near if which == "near" else far
        if typ == "S":
            out.append({"type": "S", "side": side, "n": 1, "exp": er["exp"]})
            continue
        e = next(x for x in ch["exps"] if x["exp"] == er["exp"])
        K = target_strike(spec, typ, er, a, S)
        row = snap(e["calls"] if typ == "C" else e["puts"], K)
        if not row:
            raise O.NoOptions(f"no priced {'call' if typ == 'C' else 'put'} near {K:.2f} for {er['exp']}")
        k = row[0]
        # Wings must sit outside the body: never the same strike twice on one side and type.
        if (typ, er["exp"], k) in used:
            rows = [r for r in (e["calls"] if typ == "C" else e["puts"]) if _priced(r) and (typ, er["exp"], r[0]) not in used]
            if rows:
                k = min(rows, key=lambda r: abs(r[0] - K))[0]
        used.add((typ, er["exp"], k))
        out.append({"type": typ, "side": side, "n": 1, "exp": er["exp"], "strike": k})
    return out


# --------------------------------------------------------------------------
# the whole position
# --------------------------------------------------------------------------

def _leg_model(leg: dict, ch: dict, a: dict) -> dict:
    """Price, volatility and time for one option leg, as OPT's contract page works them out."""
    e = next((x for x in ch["exps"] if x["exp"] == leg["exp"]), None)
    call = leg["type"] == "C"
    row = e and next((r for r in (e["calls"] if call else e["puts"]) if abs(r[0] - leg["strike"]) < 1e-6), None)
    if not row:
        raise O.NoOptions(f"{ch['sym']} lists no {leg['exp']} {leg['strike']:g} {'call' if call else 'put'}")
    er = next((r for r in a["expiries"] if r["exp"] == leg["exp"]), None)
    K, bid, ask, last = row[0], row[1], row[2], row[3]
    S = ch["spot"]
    T = er["t"] if er else max((O._expiry_ts(leg["exp"]) - ch["fetched"]) / (O.YEAR * 86400), 1.0 / O.YEAR / 24)
    r = O._rate(T)
    F = er["fwd"] if er else S * math.exp((r - (ch.get("div_yield") or 0.0)) * T)
    q = r - math.log(F / S) / T
    df = math.exp(-r * T)
    quoted = bid > 0 and ask >= bid
    px = 0.5 * (bid + ask) if quoted else last
    if px <= 0:
        raise O.NoOptions(f"the {leg['exp']} {K:g} {'call' if call else 'put'} has no price")
    own = O.implied(px / df, F, K, T, call)
    vol = own or O._smile_at(a, leg["exp"], K / F) or (er or {}).get("atm")
    return {"K": K, "call": call, "T": T, "r": r, "q": q, "F": F, "px": px, "vol": vol, "bid": bid, "ask": ask,
            "quoted": quoted, "half_spread": 0.5 * (ask - bid) if quoted else 0.0, "volume": row[5], "oi": row[6],
            "iv": own, "er": er}


def value(legs: list[dict], models: list[Optional[dict]], S: float, t: float) -> float:
    """The position's worth with the share at S and t years gone, per the legs' models."""
    v = 0.0
    Sx = max(S, 1e-8)                  # Black's formula takes the log of the price
    for leg, m in zip(legs, models):
        k = leg["side"] * leg["n"] * O.SHARES
        if leg["type"] == "S":
            v += k * S
        else:
            left = m["T"] - t
            v += k * (O.worth(Sx, m["K"], left, m["vol"] or 0.0, m["r"], m["q"], m["call"]) if left > 1e-9
                      else (max(0.0, S - m["K"]) if m["call"] else max(0.0, m["K"] - S)))
    return v


def crossings(xs: list[float], ys: list[float]) -> list[float]:
    out = []
    for i in range(1, len(xs)):
        a, b = ys[i - 1], ys[i]
        if a == 0:
            out.append(xs[i - 1])
        elif (a < 0 < b) or (a > 0 > b):
            out.append(xs[i - 1] + (xs[i] - xs[i - 1]) * (-a) / (b - a))
    return sorted({round(x, 2) for x in out})


def odds(xs: list[float], pl: list[float], below) -> dict:
    """Chance of a profit and the expected result, from a cdf `below(x)`, on the grid's segments."""
    p, ev = 0.0, 0.0
    c_prev = below(xs[0])
    if pl[0] > 0:
        p += c_prev
    ev += c_prev * pl[0]
    for i in range(1, len(xs)):
        c = below(xs[i])
        mass = max(0.0, c - c_prev)
        mid = 0.5 * (pl[i - 1] + pl[i])
        if mid > 0:
            p += mass
        ev += mass * mid
        c_prev = c
    tail = max(0.0, 1.0 - c_prev)
    if pl[-1] > 0:
        p += tail
    ev += tail * pl[-1]
    return {"p": min(1.0, p), "ev": ev}


def lognormal_below(F: float, vol: float, T: float):
    s = vol * math.sqrt(T)

    def below(x: float) -> float:
        if x <= 0:
            return 0.0
        return O.ncdf((math.log(x / F) + 0.5 * s * s) / s)
    return below


def evaluate(ch: dict, a: dict, legs: list[dict], rv: Optional[float] = None, held: float = 0.0,
             fx: Optional[float] = None) -> dict:
    S = ch["spot"]
    models = [None if l["type"] == "S" else _leg_model(l, ch, a) for l in legs]
    opts = [m for m in models if m]
    if not opts:
        raise O.NoOptions("a strategy needs at least one option")
    T1 = min(m["T"] for m in opts)
    first_exp = next(l["exp"] for l, m in zip(legs, models) if m and abs(m["T"] - T1) < 1e-12)
    er1 = next((r for r in a["expiries"] if r["exp"] == first_exp), None)

    cost = 0.0
    for l, m in zip(legs, models):
        cost += l["side"] * l["n"] * O.SHARES * (S if l["type"] == "S" else m["px"])
    contracts = sum(l["n"] for l in legs if l["type"] != "S")
    spread = sum(l["n"] * O.SHARES * m["half_spread"] for l, m in zip(legs, models) if m)
    fees = contracts * FEE_USD

    # the grid: wide enough for every strike, the range priced, and some room
    ks = [m["K"] for m in opts]
    lo = min(0.55 * S, 0.85 * min(ks), (er1["q"][0] * 0.95) if er1 and er1.get("q") else S)
    hi = max(1.5 * S, 1.15 * max(ks), (er1["q"][4] * 1.05) if er1 and er1.get("q") else S)
    lo = max(lo, 0.01)
    xs = [lo + (hi - lo) * i / (GRID - 1) for i in range(GRID)]
    end = [value(legs, models, x, T1) - cost for x in xs]
    now = [value(legs, models, x, 0.0) - cost for x in xs]
    half = [value(legs, models, x, T1 / 2) - cost for x in xs]

    # beyond the grid: at a share price of zero, and the slope far above
    at0 = value(legs, models, 0.0, T1) - cost
    far1, far2 = value(legs, models, hi * 3, T1), value(legs, models, hi * 4, T1)
    slope_up = (far2 - far1) / hi
    unlimited_gain = slope_up > 1.0
    unlimited_loss = slope_up < -1.0
    max_gain = None if unlimited_gain else max(max(end), at0, far2 - cost)
    max_loss = None if unlimited_loss else min(min(end), at0, far2 - cost)

    # the odds the chain gives, and those the share's own swings would give
    cdf = (er1 or {}).get("cdf")
    m1 = next(m for m in opts if abs(m["T"] - T1) < 1e-12)
    chain_below = (lambda x: O._below(cdf, x)) if cdf else lognormal_below(m1["F"], (er1 or {}).get("atm") or m1["vol"], T1)
    imp = odds(xs, end, lambda x: chain_below(x) or 0.0)
    real = odds(xs, end, lognormal_below(m1["F"], rv, T1)) if rv else None
    # Against what the money would have earned in the meantime: a position that
    # holds shares is otherwise credited the interest the forward carries.
    carry = cost * (math.exp(m1["r"] * T1) - 1.0)
    imp["ev"] -= carry
    if real:
        real["ev"] -= carry

    # greeks, in dollars, for the whole position
    g = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for l, m in zip(legs, models):
        k = l["side"] * l["n"] * O.SHARES
        if l["type"] == "S":
            g["delta"] += k
            continue
        if not m["vol"]:
            continue
        gg = O.greeks(S, m["K"], m["T"], m["vol"], m["r"], m["q"], m["call"])
        for key in g:
            g[key] += k * gg[key]

    # what it ties up
    shares = sum(l["side"] * l["n"] * O.SHARES for l in legs if l["type"] == "S")
    naked_calls = unlimited_loss
    csp = [m["K"] * l["n"] * O.SHARES for l, m in zip(legs, models)
           if m and not m["call"] and l["side"] < 0]
    if naked_calls:
        capital, capital_how = None, "margin: the loss has no limit, so DEGIRO sets the margin"
    elif max_loss is not None:
        capital = max(cost, -max_loss)
        capital_how = ("the shares, less what the options pay" if shares > 0 else
                       "the most it can lose, set aside" if cost < 0 else "the net cost")
    else:
        capital, capital_how = cost, "the net cost"

    legs_out = []
    for l, m in zip(legs, models):
        if l["type"] == "S":
            legs_out.append({**l, "px": S, "label": f"{'buy' if l['side'] > 0 else 'sell'} {l['n'] * O.SHARES} shares"})
            continue
        gg = O.greeks(S, m["K"], m["T"], m["vol"], m["r"], m["q"], m["call"]) if m["vol"] else {}
        legs_out.append({**l, "px": round(m["px"], 4), "bid": m["bid"] or None, "ask": m["ask"] or None,
                         "iv": round(m["iv"], 4) if m["iv"] else None, "vol": round(m["vol"], 4) if m["vol"] else None,
                         "delta": round(gg.get("delta", 0.0), 3), "days": round(m["T"] * O.YEAR, 1),
                         "volume": m["volume"], "oi": m["oi"], "moneyness": round((m["K"] / S - 1) * 100, 1),
                         "label": f"{'buy' if l['side'] > 0 else 'sell'} {l['n']} {l['exp']} {m['K']:g} {'call' if m['call'] else 'put'}"})

    out = {
        "sym": ch["sym"], "spot": S, "currency": ch.get("currency") or "USD", "asof": ch["fetched"],
        "mode": a.get("mode"), "session": a.get("session"), "first_exp": first_exp, "days": round(T1 * O.YEAR, 1),
        "legs": legs_out, "cost": round(cost, 2), "debit": cost > 0,
        "spread_cost": round(spread, 2), "fees": round(fees, 2), "contracts": contracts,
        "max_gain": None if max_gain is None else round(max_gain, 2), "max_loss": None if max_loss is None else round(max_loss, 2),
        "unlimited_gain": unlimited_gain, "unlimited_loss": unlimited_loss,
        "breakevens": crossings(xs, end),
        "p_profit": round(imp["p"], 4), "ev_market": round(imp["ev"], 2), "odds_from": "chain" if cdf else "model",
        "carry": round(carry, 2), "rate": round(m1["r"], 4),
        "rv": round(rv, 4) if rv else None,
        "p_profit_rv": round(real["p"], 4) if real else None, "ev_rv": round(real["ev"], 2) if real else None,
        "greeks": {k: round(v, 2) for k, v in g.items()},
        "capital": None if capital is None else round(capital, 2), "capital_how": capital_how,
        "shares_needed": max(0, shares), "shares_held": held,
        "range": (er1 or {}).get("q"),
        "curve": {"x": [round(x, 2) for x in xs], "end": [round(v, 2) for v in end],
                  "now": [round(v, 2) for v in now], "half": [round(v, 2) for v in half]},
        "half_date": (date.fromtimestamp(ch["fetched"]) + timedelta(days=T1 * O.YEAR / 2)).isoformat(),
        "fee_note": FEE_NOTE,
    }
    if fx:
        out["fx"] = fx
        for k in ("cost", "max_gain", "max_loss", "capital", "ev_market", "ev_rv"):
            if out.get(k) is not None:
                out[k + "_eur"] = round(out[k] / fx, 2)
    return out


# --------------------------------------------------------------------------
# for the page
# --------------------------------------------------------------------------

def _context(sym: str) -> tuple[dict, dict, Optional[float], float, Optional[float]]:
    sym = O.usym(sym)
    ch, bars = O.reading(sym)
    a = O.analysed(ch, bars)
    try:
        rv = O.realised(sym).get("rv63")
    except Exception:
        rv = None
    held = 0.0
    try:
        from . import earnings
        pos = earnings.position(sym)
        if pos and pos.get("as") == sym:
            held = float(pos.get("qty") or 0.0)
    except Exception:
        pass
    fx = None
    try:
        from . import pricing
        fx = pricing.fx_on(pricing.get_fx("EURUSD", (date.today() - timedelta(days=14)).isoformat()),
                           date.today().isoformat(), 0.0) or None
    except Exception:
        pass
    return ch, a, rv, held, fx


def strikes_for(ch: dict, exp: str) -> dict:
    e = next((x for x in ch["exps"] if x["exp"] == exp), None)
    if not e:
        return {"C": [], "P": []}
    return {"C": [r[0] for r in e["calls"] if _priced(r)], "P": [r[0] for r in e["puts"] if _priced(r)]}


def build(sym: str, tpl: str, legs: Optional[list] = None) -> dict:
    if tpl not in TEMPLATES and not legs:
        raise ValueError(f"no strategy called {tpl}")
    ch, a, rv, held, fx = _context(sym)
    if not legs:
        legs = default_legs(tpl, ch, a)
    clean = []
    for l in legs:
        typ = l.get("type")
        if typ not in ("C", "P", "S"):
            raise ValueError("a leg is a call (C), a put (P) or shares (S)")
        side = 1 if int(l.get("side") or 1) > 0 else -1
        n = max(1, min(50, int(l.get("n") or 1)))
        leg = {"type": typ, "side": side, "n": n, "exp": l.get("exp")}
        if typ != "S":
            leg["strike"] = float(l["strike"])
        clean.append(leg)
    out = evaluate(ch, a, clean, rv=rv, held=held, fx=fx)
    t = TEMPLATES.get(tpl) or {}
    exps = sorted({l["exp"] for l in clean if l["type"] != "S"})
    out.update({"tpl": tpl, "name": t.get("name"), "view": t.get("view"), "what": t.get("what"),
                "when": t.get("when"), "risk": t.get("risk"),
                "expiries": [{"exp": r["exp"], "days": r["days"]} for r in a["expiries"] if r["days"] >= 2],
                "strikes": {e: strikes_for(ch, e) for e in exps},
                "earnings": a.get("earnings"), "name_sym": ch.get("name")})
    out["say"] = _say(out)
    return out


def catalogue() -> dict:
    return {"templates": [{"k": k, "name": t["name"], "view": t["view"], "what": t["what"]} for k, t in TEMPLATES.items()],
            "views": VIEWS, "fee": FEE_USD, "fee_note": FEE_NOTE}


def _usd(v: Optional[float]) -> str:
    return "unlimited" if v is None else f"${abs(v):,.0f}"


def _sd(v: float) -> str:
    """Dollars with the sign in front: −$84, $1,053."""
    return ("−$" if v < 0 else "$") + f"{abs(v):,.0f}"


def _say(o: dict) -> list[str]:
    out = []
    c = o["cost"]
    out.append(f"It {'costs' if c > 0 else 'pays'} ${abs(c):,.0f} to open at mid prices"
               + (f" (€{abs(o['cost_eur']):,.0f})" if o.get("cost_eur") is not None else "")
               + f"; crossing the spread adds about ${o['spread_cost']:,.0f} and DEGIRO's fee ${o['fees']:,.2f}.")
    gain = "no limit" if o["unlimited_gain"] else _usd(o["max_gain"])
    loss = "no limit" if o["unlimited_loss"] else _usd(o["max_loss"])
    be = ", ".join(f"${x:,.2f}" for x in o["breakevens"]) or "none in range"
    out.append(f"At the {o['first_exp']} expiry the most it makes is {gain}, the most it loses {loss}; break-even at {be}.")
    out.append(f"The chain gives it a {o['p_profit'] * 100:.0f}% chance of a profit at that expiry, and an expected result "
               f"of {_sd(o['ev_market'])} after the interest the money would have earned: options are priced so that, on the "
               f"market's own odds, neither side wins on average, and the spread is the house's cut.")
    if o.get("ev_rv") is not None and o.get("rv"):
        out.append(f"With the swings the share has shown over three months ({o['rv'] * 100:.0f}% a year) instead, the odds would be "
                   f"{o['p_profit_rv'] * 100:.0f}% and the expected result {_sd(o['ev_rv'])}. The difference is what the market charges "
                   f"for uncertainty: sellers collect it, until a move larger than either estimate.")
    if o["shares_needed"] > 0:
        out.append(f"It needs {o['shares_needed']:.0f} shares; you hold {o['shares_held']:g}.")
    if o.get("capital") is not None:
        out.append(f"It ties up about ${o['capital']:,.0f}" + (f" (€{o['capital_eur']:,.0f})" if o.get("capital_eur") is not None else "")
                   + f": {o['capital_how']}.")
    if o.get("earnings") and date.today().isoformat() <= o["earnings"] <= o["first_exp"]:
        out.append(f"Earnings fall on {o['earnings']}, before the expiry: the options price that move, and it is the biggest risk to a seller.")
    return out


# --------------------------------------------------------------------------
# the premium screener: the holdings' stored chains, never a download
# --------------------------------------------------------------------------

def _row_for(a: dict, ch: dict, exp_row: dict, typ: str, delta: float) -> Optional[dict]:
    """The listed option nearest `delta` (a put's as a positive number) at this expiry."""
    K = target_strike({"d": delta}, typ, exp_row, a, ch["spot"])
    e = next((x for x in ch["exps"] if x["exp"] == exp_row["exp"]), None)
    row = e and snap(e["calls"] if typ == "C" else e["puts"], K)
    if not row:
        return None
    px = _priced(row)
    below = O._below(exp_row.get("cdf"), row[0])
    p = None if below is None else (1 - below if typ == "C" else below)
    days = exp_row["days"]
    base = ch["spot"] if typ == "C" else row[0]
    return {"strike": row[0], "px": round(px, 2), "otm": round((row[0] / ch["spot"] - 1) * 100, 1),
            "yield": round(px / base * 100, 2), "yield_y": round(px / base * 365 / days * 100, 1),
            "p": round(p, 3) if p is not None else None, "spread": round(row[2] - row[1], 2) if row[1] > 0 else None}


def screen() -> dict:
    with store.conn() as c:
        rows = {}
        for r in c.execute("SELECT sym, mode, asof, payload FROM opt_chain"):
            cur = rows.get(r["sym"])
            if cur is None or (r["mode"] == "quotes" and cur[0] != "quotes") or (r["mode"] == cur[0] and r["asof"] > cur[1]):
                rows[r["sym"]] = (r["mode"], r["asof"], r["payload"])
        days = {}
        for r in c.execute("SELECT sym, d, iv30, rv21 FROM opt_day ORDER BY d"):
            days.setdefault(r["sym"], []).append(dict(r))
    import json
    try:
        uni = {u["sym"]: u for u in O.universe() if u["how"] == "held"}
    except Exception:
        uni = {}
    held = {}
    try:
        from . import service
        for p in service.dashboard("SP500", light=True).get("positions", []):
            if p.get("ticker"):
                held[p["ticker"]] = held.get(p["ticker"], 0.0) + (p.get("qty") or 0.0)
    except Exception:
        pass
    out, today = [], date.today().isoformat()
    for sym in sorted(uni):
        if sym not in rows:
            continue
        ch = json.loads(rows[sym][2])
        try:
            a = O.analysed(ch, ch.get("bars"))
        except Exception:
            continue
        near, _ = pick_expiries(a["expiries"])
        if not near:
            continue
        call, put = _row_for(a, ch, near, "C", 0.30), _row_for(a, ch, near, "P", 0.30)
        h = days.get(sym) or []
        ivs = [x["iv30"] for x in h if x.get("iv30")]
        iv30 = (a.get("cm") or {}).get("30")
        rv21 = (h[-1] if h else {}).get("rv21")
        rank = round(sum(1 for v in ivs if v <= iv30) / len(ivs) * 100) if iv30 and len(ivs) >= O.MIN_HISTORY else None
        out.append({"sym": sym, "name": uni[sym]["name"], "spot": ch["spot"], "session": a.get("session"),
                    "mode": a.get("mode"), "exp": near["exp"], "days": near["days"],
                    "iv30": round(iv30 * 100, 1) if iv30 else None, "rv21": round(rv21 * 100, 1) if rv21 else None,
                    "ratio": round(iv30 / rv21, 2) if iv30 and rv21 else None, "rank": rank, "n_days": len(ivs),
                    "call": call, "put": put,
                    "earnings": a.get("earnings") if a.get("earnings") and today <= a["earnings"] <= near["exp"] else None,
                    "held": held.get(sym, 0.0), "lots": int(held.get(sym, 0.0) // O.SHARES),
                    "cash_for_put": round(put["strike"] * O.SHARES, 0) if put else None})
    out.sort(key=lambda r: -((r["call"] or {}).get("yield_y") or 0))
    return {"rows": out, "min_history": O.MIN_HISTORY, "built_at": time.time(),
            "sessions": sorted({r["session"] for r in out if r.get("session")})[-1:]}


def screen_view() -> dict:
    return store.cache_through("opt:screen", 1800, screen)
