"""
FCST — what the portfolio might be worth in 1 to 50 years.

Not a prediction. A range: the outcomes the past would have produced for a
book like this one, with the money you plan to add and the tax you would pay.

HOW
  The history is 16 countries' stock markets, 1871-2020, after inflation
  (Jordà-Schularick-Taylor Macrohistory Database, R6). Two ways to read it:

    world     each year's return of all 16 markets together, weighted by the
              size of their economies: what a spread-out investor got.
    country   a market at a time, as if yours could turn out to be any of
              them: Japan after 1989, Germany in two wars, Italy, Portugal.
              Harsher, and fair for a book that sits mostly in one country.

  Ten thousand paths of up to fifty years are drawn from that history in
  blocks of about ten consecutive years at a time (a stationary bootstrap),
  so a bad decade stays a bad decade and inflation arrives with the years it
  came with, instead of single years shuffled into a smoother past.

  Your book is the market, scaled by its own risk as measured on today's
  holdings over the last two years: its beta to the world index, plus a
  swing of its own that adds risk and no return. That second part is why a
  concentrated book has a lower typical outcome than an index fund with the
  same average return: compounding punishes the swings (volatility drag).

  Deposits go in through the year, growing each year by what you set, in
  today's euros. Box 3 is taken from the portfolio at each year end under the
  rules chosen (the 2028 bill, today's system, or none). Costs come off every
  year.

WHAT IT LEAVES OUT
  Markets that went to zero (Russia 1917, China 1949) are not in the data, so
  even the country view is kind to the past. The data is yearly: a fall that
  recovered within the year does not show. It ends in 2020. The book's risk
  is measured on two years, and a book changes over fifty.

The money-supply question (does money growth make stocks grow?) is answered
from the same data on the page, country-decade by country-decade.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from . import store

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fcst_seed.json")
HORIZONS = (1, 2, 5, 10, 20, 30, 50)
MAX_YEARS = 50
PATHS = 10_000
BLOCK = 10              # mean block length in years
SEED = 20261012         # one fixed draw: the same settings give the same fan
INDEX_COST = 0.20       # % a year: a world index fund
INFL_CAP = 0.5          # a country-year with inflation beyond ±50% is a hyperinflation or a currency reform: left out
VAL_YEARS = 10          # how long a valuation adjustment lasts
QS = (5, 10, 25, 50, 75, 90, 95)
TTL_RISK = 6 * 3600
TTL_CAPE = 30 * 86400
SNAP_EVERY = 7          # days between snapshots for the track record

# ---------------------------------------------------------------------------
# Box 3: only what is known, as of the date below.
# ---------------------------------------------------------------------------
BOX3 = {
    "asof": "2026-10-12",
    "rate": 0.36,
    # Today's system ("forfaitair"), as it applies to 2027.
    "now": {"allowance": 60098.0, "deemed_invest": 0.06, "deemed_savings": 0.0128,
            "what": "36% of a deemed return on everything above the tax-free amount: 6.00% on investments, "
                    "1.28% on savings. The tax-free amount for 2027 is €60,098 a person; 6.00% and 1.28% are "
                    "2026's figures, the last published (1.28% is provisional). If your actual return was lower, "
                    "the lower one is taxed (the tegenbewijsregeling)."},
    # The Wet werkelijk rendement box 3, as the Tweede Kamer passed it.
    "bill": {"start": 2028, "allowance": 1800.0, "loss_min": 500.0,
             "what": "36% of the actual return, every year: dividends plus the change in value, also when nothing is "
                     "sold. The first €1,800 a person is free. A loss of €500 or more is carried forward without "
                     "limit and set against later gains; never back.",
             "status": "Passed by the Tweede Kamer on 12 February 2026. The Eerste Kamer postponed its vote; the "
                       "amendment (novelle) expected on Prinsjesdag 2026 did not come, and the decision moved to "
                       "the Voorjaarsnota 2027. 1 January 2028 is the plan, not a certainty.",
             "considered": "Being discussed, not in the bill, so not used here: 35% instead of 36%, €1,900 instead "
                           "of €1,800, carrying a loss back one year.",
             "assumed": "Not settled anywhere, so chosen here: the €1,800 stays €1,800 (no indexation is "
                        "confirmed), a loss counts from the first euro once it reaches €500, and tax is paid out "
                        "of the portfolio at the year end."},
    "sources": [
        ("SRA: summary of the bill", "https://www.sra.nl/dossiers/dossier-hoge-raad-box-3/box-3-vanaf-2027/box-3-een-overzichtsartikel-met-de-belangrijkste-verwijzingen-box-3-vanaf-2027"),
        ("Van Lanschot Kempen: the bill passed", "https://www.vanlanschotkempen.com/nl-nl/private-banking/inspiratie/vermogensregie/box-3-wetsvoorstel-werkelijk-rendement"),
        ("Lansigt: box 3 after Prinsjesdag 2026", "https://www.lansigt.nl/actueel/box-3-na-prinsjesdag-2026-wat-verandert-er-in-2027-en-2028/"),
        ("Mercurius: no novelle on Prinsjesdag", "https://mercuriusvermogensbeheer.nl/2026/09/19/prinsjesdag-2026-box-3-blijft-nog-even-bij-het-oude/"),
        ("SRA: box 3 figures for 2026", "https://www.sra.nl/nieuws/259001/2026/01/box-3-percentages-vrijstellingen-en-bedragen-2026"),
    ],
}

DEFAULTS = {"start": None, "monthly": 150.0, "growth": 3.0, "dep_years": MAX_YEARS, "hist": "world",
            "risk": "book", "cost": None, "box3": "bill", "partner": False, "savings": 0.0,
            "valuation": False}


def _np():
    import numpy as np
    return np


# ---------------------------------------------------------------------------
# the history
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def hist() -> dict:
    with open(SEED_PATH, encoding="utf-8") as f:
        return json.load(f)


def table(h: dict, mode: str) -> dict:
    """
    The history as rows a path can walk through: real market return,
    inflation, real cash return, and the row that follows it (-1 where the
    run of consecutive years ends, so a path never jumps silently from one
    country, or from 2020, into another year).
    """
    np = _np()
    real, inf, bill, nxt, label = [], [], [], [], []
    blocks = [("World", h["world"])] if mode == "world" else sorted(h["countries"].items())
    for name, s in blocks:
        ys = s["years"]
        base = len(real)
        for i, y in enumerate(ys):
            real.append(s["real"][i])
            inf.append(s["inf"][i])
            b = s["bill"][i]
            bill.append(0.0 if b is None else b)
            label.append((name, y))
            nxt.append(base + i + 1 if i + 1 < len(ys) and ys[i + 1] == y + 1 else -1)
    return {"real": np.array(real), "inf": np.array(inf), "bill": np.array(bill),
            "nxt": np.array(nxt), "label": label}


def draw(tab: dict, n: int, years: int, seed: int = SEED, block: float = BLOCK):
    """
    Row numbers for n paths of `years` years (a stationary bootstrap: each
    year continues the run with probability 1 - 1/block, else starts afresh
    at a random row), and one standard normal draw per path-year for the
    book's own swing. Every variant of a run uses the same draws, so the
    difference between two of them is the setting, not the luck.
    """
    np = _np()
    rng = np.random.default_rng(seed)
    N = len(tab["real"])
    idx = rng.integers(0, N, n)
    out = np.empty((years, n), dtype=np.int64)
    for t in range(years):
        if t:
            nx = tab["nxt"][idx]
            jump = (rng.random(n) < 1.0 / block) | (nx < 0)
            idx = np.where(jump, rng.integers(0, N, n), nx)
        out[t] = idx
    eps = rng.standard_normal((years, n))
    return out, eps


def val_delta(cape: Optional[dict]) -> float:
    """
    The yearly haircut, in log terms, that today's US valuation implies for
    the next ten years: what the regression of ten-year real returns on the
    starting CAPE predicts now, against the average ten years.
    """
    if not cape or cape.get("pred10") is None or cape.get("avg10") is None:
        return 0.0
    return math.log(1 + cape["pred10"]) - math.log(1 + cape["avg10"])


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------

def simulate(tab: dict, idx, eps, *, years: int, v0: float, beta: float, s: float, cost: float,
             monthly: float, growth: float, dep_years: int, box3: str, persons: int = 1,
             savings: float = 0.0, delta: float = 0.0, cash: bool = False, b3: dict = BOX3,
             first_year: Optional[int] = None) -> dict:
    """
    Value of every path at every year end, in euros of the day (nominal) and
    in today's euros (real). `cost` and `growth` are fractions a year;
    `cash` swaps the market for the cash rate (a savings account). The first
    year ahead is taxed as `first_year` (next calendar year by default).

    Returns arrays shaped (years + 1, paths), row 0 being today.
    """
    np = _np()
    n = idx.shape[1]
    first_year = first_year or date.today().year + 1
    rm = tab["real"][idx[:years]]
    pi = tab["inf"][idx[:years]]
    rf = tab["bill"][idx[:years]]
    if cash:
        rb = rf.copy()
    else:
        if delta:
            k = min(VAL_YEARS, years)
            rm = rm.copy()
            rm[:k] = (1 + rm[:k]) * math.exp(delta) - 1
        sysr = np.maximum(rf + beta * (rm - rf), -0.95)
        rb = (1 + sysr) * np.exp(s * eps[:years] - s * s / 2) - 1
    rb = (1 + rb) * (1 - cost) - 1
    R = (1 + rb) * (1 + pi) - 1                         # nominal, after costs
    bill_nom = (1 + rf) * (1 + pi) - 1

    V = np.full(n, float(v0))
    P = np.ones(n)
    carry = np.zeros(n)
    nom = np.empty((years + 1, n)); real = np.empty((years + 1, n))
    taxr = np.zeros((years + 1, n)); depn = np.zeros((years + 1, n))
    nom[0] = V; real[0] = V
    tax_cum = np.zeros(n); dep_cum = np.zeros(n)
    rate = b3["rate"]
    for t in range(years):
        a = 12.0 * monthly * (1 + growth) ** t if t < dep_years else 0.0
        P_mid = P * np.sqrt(1 + pi[t])
        dep = a * P_mid                                  # nominal, paid in through the year
        start = V
        g = 1 + R[t]
        V = start * g + dep * np.sqrt(np.maximum(g, 0.0))
        P = P * (1 + pi[t])
        result = V - start - dep                         # the year's gain in euros of the day
        tax = np.zeros(n)
        year = first_year + t
        sav_nom = savings * P / (1 + pi[t])              # savings kept level in today's euros
        sav_int = sav_nom * bill_nom[t]
        if box3 == "bill" and year >= b3["bill"]["start"]:
            allow = b3["bill"]["allowance"] * persons
            left = np.maximum(allow - np.maximum(sav_int, 0.0), 0.0)
            pos = result > 0
            taxable = np.where(pos, np.maximum(result - left, 0.0), 0.0)
            use = np.minimum(carry, taxable)
            taxable -= use
            carry = carry - use + np.where(result <= -b3["bill"]["loss_min"], -result, 0.0)
            tax = rate * taxable
        elif box3 in ("bill", "now"):
            # Today's system: the deemed return on what is above the
            # tax-free amount, or the actual return if that was lower. The
            # amount is indexed every year; the percentages stay at the last
            # published ones. Only the tax the portfolio adds is charged.
            allow = b3["now"]["allowance"] * persons * (P / (1 + pi[t]))
            def owed(inv, inv_actual):
                base = inv + sav_nom
                deemed = b3["now"]["deemed_invest"] * inv + b3["now"]["deemed_savings"] * sav_nom
                actual = inv_actual + sav_int
                frac = np.where(base > 0, np.maximum(base - allow, 0.0) / np.where(base > 0, base, 1.0), 0.0)
                return rate * np.maximum(np.minimum(deemed, actual), 0.0) * frac
            tax = owed(start, result) - owed(np.zeros(n), np.zeros(n))
            tax = np.maximum(tax, 0.0)
        tax = np.minimum(tax, np.maximum(V, 0.0))
        V = V - tax
        tax_cum += tax / P
        dep_cum += dep
        nom[t + 1] = V; real[t + 1] = V / P
        taxr[t + 1] = tax_cum; depn[t + 1] = dep_cum
    return {"nom": nom, "real": real, "tax_real": taxr, "dep_nom": depn}


def put_in(v0: float, monthly: float, growth: float, dep_years: int, years: int) -> list:
    """What you will have paid in by each year end, in today's euros."""
    out, tot = [float(v0)], float(v0)
    for t in range(years):
        tot += 12.0 * monthly * (1 + growth) ** t if t < dep_years else 0.0
        out.append(tot)
    return out


# ---------------------------------------------------------------------------
# the run the page asks for
# ---------------------------------------------------------------------------

def _num(v, lo, hi, default):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(v):
        return default
    return min(max(v, lo), hi)


def params(p: Optional[dict], base: dict) -> dict:
    """The settings, clipped to sensible ranges, with the book's own figures where none are given."""
    p = {**DEFAULTS, **{k: v for k, v in (p or {}).items() if v is not None}}
    risk = base.get("risk") or {}
    out = {
        "start": _num(p["start"] if p["start"] is not None else base.get("value"), 0, 1e9, 0.0),
        "monthly": _num(p["monthly"], 0, 1e6, 150.0),
        "growth": _num(p["growth"], -10, 15, 3.0),
        "dep_years": int(_num(p["dep_years"], 0, MAX_YEARS, MAX_YEARS)),
        "hist": p["hist"] if p["hist"] in ("world", "country") else "world",
        "risk": p["risk"] if p["risk"] in ("book", "index") else "book",
        "box3": p["box3"] if p["box3"] in ("bill", "now", "none") else "bill",
        "partner": bool(p["partner"]),
        "savings": _num(p["savings"], 0, 1e8, 0.0),
        "valuation": bool(p["valuation"]),
    }
    book_ok = risk.get("available")
    if out["risk"] == "book" and book_ok:
        out["beta"], out["s"] = risk["beta"], risk["s"]
        dflt = base.get("cost_pct")
        out["cost"] = _num(p["cost"], 0, 10, dflt if dflt is not None else INDEX_COST)
    else:
        if out["risk"] == "book":
            out["risk_note"] = risk.get("why") or "The book's risk could not be measured; an index fund's is used."
        out["beta"], out["s"] = 1.0, 0.0
        out["cost"] = _num(p["cost"], 0, 10, INDEX_COST) if out["risk"] == "index" or p["cost"] is not None else INDEX_COST
    return out


def run(p: Optional[dict] = None, base: Optional[dict] = None, n: int = PATHS) -> dict:
    np = _np()
    base = base if base is not None else baseline()
    q = params(p, base)
    h = hist()
    tab = table(h, q["hist"])
    Y = MAX_YEARS
    idx, eps = draw(tab, n, Y)
    cape = base.get("cape") or h.get("cape")
    delta = val_delta(cape) if q["valuation"] else 0.0
    common = dict(years=Y, monthly=q["monthly"], growth=q["growth"] / 100, dep_years=q["dep_years"],
                  persons=2 if q["partner"] else 1, savings=q["savings"])
    main = simulate(tab, idx, eps, v0=q["start"], beta=q["beta"], s=q["s"], cost=q["cost"] / 100,
                    box3=q["box3"], delta=delta, **common)
    notax = simulate(tab, idx, eps, v0=q["start"], beta=q["beta"], s=q["s"], cost=q["cost"] / 100,
                     box3="none", delta=delta, **common)
    index = simulate(tab, idx, eps, v0=q["start"], beta=1.0, s=0.0, cost=INDEX_COST / 100,
                     box3=q["box3"], delta=delta, **common)
    # The same book under each Box 3 regime, so the page can set them side by side.
    regime = {q["box3"]: main, "none": notax}
    for rg in ("bill", "now"):
        if rg not in regime:
            regime[rg] = simulate(tab, idx, eps, v0=q["start"], beta=q["beta"], s=q["s"], cost=q["cost"] / 100,
                                  box3=rg, delta=delta, **common)
    cash = simulate(tab, idx, eps, v0=q["start"], beta=1.0, s=0.0, cost=0.0, box3=q["box3"], cash=True, **common)
    alone = simulate(tab, idx, eps, v0=q["start"], beta=q["beta"], s=q["s"], cost=q["cost"] / 100,
                     box3=q["box3"], delta=delta, **{**common, "monthly": 0.0})
    pin = put_in(q["start"], q["monthly"], q["growth"] / 100, q["dep_years"], Y)
    # Under the mattress: the deposits as notes in a drawer, eaten by inflation.
    P = np.cumprod(1 + tab["inf"][idx[:Y]], axis=0)
    drawer = np.vstack([np.full(idx.shape[1], q["start"]),
                        (q["start"] + main["dep_nom"][1:]) / P])

    def pct(a, qs=QS):
        return {f"p{k}": float(v) for k, v in zip(qs, np.percentile(a, qs))}

    fan = []
    for t in range(Y + 1):
        r = np.percentile(main["real"][t], QS)
        m = np.percentile(main["nom"][t], QS)
        fan.append({"y": t, "real": [round(float(v), 2) for v in r], "nom": [round(float(v), 2) for v in m],
                    "put_in": round(pin[t], 2),
                    "index": round(float(np.median(index["real"][t])), 2),
                    "index_nom": round(float(np.median(index["nom"][t])), 2),
                    "cash": round(float(np.median(cash["real"][t])), 2),
                    "cash_nom": round(float(np.median(cash["nom"][t])), 2),
                    "drawer": round(float(np.median(drawer[t])), 2),
                    "drawer_nom": round(float(q["start"] + np.median(main["dep_nom"][t])), 2)})
    rows = []
    for k in HORIZONS:
        a = main["real"][k]
        med, med0 = float(np.median(a)), float(np.median(notax["real"][k]))
        row = {"y": k, "put_in": round(pin[k], 2), "real": pct(a), "nom": pct(main["nom"][k], (5, 50, 95)),
               "below_put_in": round(float((a < pin[k]).mean()) * 100, 1),
               "tax_real": round(float(np.median(main["tax_real"][k])), 2),
               "box3_cost_pct": round((1 - med / med0) * 100, 1) if med0 > 0 else None,
               "box3_by": {rg: round(float(np.median(v["real"][k])), 2) for rg, v in regime.items()},
               "index": round(float(np.median(index["real"][k])), 2),
               "index_p5": round(float(np.percentile(index["real"][k], 5)), 2),
               "index_p95": round(float(np.percentile(index["real"][k], 95)), 2),
               "cash": round(float(np.median(cash["real"][k])), 2),
               "drawer": round(float(np.median(drawer[k])), 2)}
        if q["start"] > 0:
            ar = (np.maximum(alone["real"][k], 1e-9) / q["start"]) ** (1.0 / k) - 1
            row["yearly"] = {kk: round(v * 100, 2) for kk, v in pct(ar, (5, 25, 50, 75, 95)).items()}
            path = alone["real"][: k + 1]
            dd = (path / np.maximum.accumulate(path, axis=0) - 1).min(axis=0)
            row["fall"] = {"p50": round(float(np.median(dd)) * 100, 1),
                           "p10": round(float(np.percentile(dd, 10)) * 100, 1)}
            row["loss"] = round(float((alone["real"][k] < q["start"]).mean()) * 100, 1)
        rows.append(row)
    return {"params": q, "fan": fan, "rows": rows, "paths": n, "block": BLOCK,
            "delta_pct": round((math.exp(delta) - 1) * 100, 2) if delta else 0.0,
            "history": {"mode": q["hist"], "rows": len(tab["real"]),
                        "from": min(l[1] for l in tab["label"]), "to": max(l[1] for l in tab["label"])}}


# ---------------------------------------------------------------------------
# the book: value, deposits, costs, and its risk measured on today's holdings
# ---------------------------------------------------------------------------

def _fit(y: list, x: list) -> Optional[dict]:
    n = len(y)
    if n < 40:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sxx = sum((a - mx) ** 2 for a in x)
    if sxx <= 0:
        return None
    b = sum((a - mx) * (c - my) for a, c in zip(x, y)) / sxx
    a0 = my - b * mx
    res = [c - a0 - b * a for a, c in zip(x, y)]
    syy = sum((c - my) ** 2 for c in y)
    sd = lambda v: math.sqrt(sum(w * w for w in v) / (len(v) - 2))
    return {"beta": b, "res_sd": sd(res), "y_sd": math.sqrt(syy / (n - 1)), "x_sd": math.sqrt(sxx / (n - 1)),
            "r2": 1 - sum(w * w for w in res) / syy if syy > 0 else 0.0, "n": n}


def _book_risk() -> dict:
    from . import bonds, riskmap
    book = bonds._book()
    if not book["rows"]:
        return {"available": False, "why": "No priced holdings."}
    px = riskmap.prices([r["t"] for r in book["rows"]])
    days = sorted(px.get(riskmap.SP) or {})
    world = riskmap.windows(riskmap._filled(bonds._to_eur(px.get(riskmap.WORLD) or {}, "USD", px), days))
    cols = {r["t"]: riskmap.windows(riskmap._filled(bonds._to_eur(px.get(r["t"]) or {}, r["ccy"], px), days))
            for r in book["rows"]}
    y, x, used = [], [], set()
    for t in range(len(days)):
        if world[t] is None:
            continue
        num = den = 0.0
        for r in book["rows"]:
            v = cols[r["t"]][t]
            if v is not None:
                num += r["w"] * v
                den += r["w"]
                used.add(r["t"])
        if den < 0.8:
            continue
        y.append(num / den)
        x.append(world[t])
    f = _fit(y, x)
    if not f:
        return {"available": False, "why": "Not enough weeks with most of the book priced."}
    k = math.sqrt(riskmap.PER_YEAR)
    return {"available": True, "beta": round(f["beta"], 3), "s": round(f["res_sd"] * k, 4),
            "vol": round(f["y_sd"] * k, 4), "mvol": round(f["x_sd"] * k, 4), "r2": round(f["r2"], 3),
            "weeks": round(f["n"] / riskmap.H), "from": days[0] if days else None, "to": days[-1] if days else None,
            "holdings": len(book["rows"]), "priced": len(used), "fetched_at": time.time()}


def book_risk() -> dict:
    try:
        return store.cache_through("fcst:risk", TTL_RISK, _book_risk)
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:200]}


def deposits() -> dict:
    """What you actually paid in: on average a month since the first deposit, and over the last twelve months."""
    txns = [t for t in store.load_txns() if t.kind in ("DEPOSIT", "WITHDRAWAL")
            and (not t.currency or t.currency == "EUR")]
    if not txns:
        return {"n": 0}
    first = min(t.trade_dt.date() for t in txns)
    today = date.today()
    months = max(1.0, (today - first).days / 30.44)
    yr = date(today.year - 1, today.month, min(today.day, 28))
    tot = sum(t.local_value or 0.0 for t in txns)
    last = sum(t.local_value or 0.0 for t in txns if t.trade_dt.date() > yr)
    return {"n": len(txns), "since": first.isoformat(), "per_month": round(tot / months, 2),
            "last12_per_month": round(last / 12, 2), "total": round(tot, 2)}


def cape() -> dict:
    """Today's US CAPE and what it has meant for the next ten years, from Shiller's file; the seed if that fails."""
    seed = hist().get("cape") or {}
    try:
        v = store.cache_through("fcst:cape", TTL_CAPE, _fetch_cape)
        return v if v and v.get("pred10") is not None else {**seed, "source": "seed"}
    except Exception:
        return {**seed, "source": "seed"}


def _fetch_cape() -> dict:
    import re
    from . import net
    page = net.get("https://shillerdata.com/").decode("utf-8", "replace")
    m = re.search(r'(?:https?:)?//[^"\']+ie_data\.xls[^"\']*', page)
    if not m:
        raise RuntimeError("no ie_data.xls link on shillerdata.com")
    url = m.group(0)
    url = "https:" + url if url.startswith("//") else url
    return {**cape_fit(net.get(url, timeout=60)), "source": "shillerdata.com", "fetched_at": time.time()}


def cape_fit(raw: bytes) -> dict:
    """Ten-year real return on log CAPE, every month with ten years after it; the prediction at the latest CAPE."""
    import xlrd
    sh = xlrd.open_workbook(file_contents=raw).sheet_by_name("Data")
    S = []
    for r in range(8, sh.nrows):
        v = sh.row_values(r)
        try:
            y, mo = str(v[0]).split(".")
            mo = int(mo + "0") if len(mo) == 1 else int(mo)
            tr = float(v[9]) if v[9] not in ("", "NA") else None
            c = float(v[12]) if v[12] not in ("", "NA") else None
        except (ValueError, IndexError):
            continue
        S.append((int(y), mo, tr, c))
    at = {(y, mo): k for k, (y, mo, _, _) in enumerate(S)}
    xs, ys = [], []
    for y, mo, tr, c in S:
        j = at.get((y + 10, mo))
        if c and tr and j is not None and S[j][2]:
            xs.append(math.log(c))
            ys.append((S[j][2] / tr) ** 0.1 - 1)
    n = len(xs)
    if n < 240:
        raise RuntimeError("too little CAPE history")
    mx, my = sum(xs) / n, sum(ys) / n
    b = sum((a - mx) * (c - my) for a, c in zip(xs, ys)) / sum((a - mx) ** 2 for a in xs)
    a0 = my - b * mx
    res = [c - a0 - b * a for a, c in zip(xs, ys)]
    syy = sum((c - my) ** 2 for c in ys)
    cur = [x for x in S if x[3]][-1]
    hi = [c for a, c in zip(xs, ys) if math.exp(a) > 30]
    return {"asof": f"{cur[0]}-{cur[1]:02d}", "value": round(cur[3], 2), "slope": round(b, 5), "intercept": round(a0, 5),
            "r2": round(1 - sum(w * w for w in res) / syy, 3), "n": n, "avg10": round(my, 5),
            "pred10": round(a0 + b * math.log(cur[3]), 5), "res_sd": round(math.sqrt(sum(w * w for w in res) / n), 5),
            "above30": {"n": len(hi), "median": round(sorted(hi)[len(hi) // 2], 5) if hi else None}}


def baseline() -> dict:
    """Everything the run needs from the book and the outside world."""
    from . import service
    out: dict = {"value": None, "cost_pct": None}
    try:
        d = service.dashboard("SP500", light=True)
        if not d.get("empty"):
            out["value"] = (d.get("totals") or {}).get("value_eur")
            c = d.get("costs") or {}
            out["cost_pct"] = c.get("drag_pct")
            out["costs"] = {k: c.get(k) for k in ("drag_pct", "commission_eur", "autofx_eur", "account_fees_eur",
                                                  "avg_capital_eur", "years", "turnover_pct")}
    except Exception as e:
        out["book_error"] = f"{e.__class__.__name__}: {e}"[:200]
    out["risk"] = book_risk()
    out["cape"] = cape()
    try:
        out["deposits"] = deposits()
    except Exception:
        out["deposits"] = {"n": 0}
    return out


# ---------------------------------------------------------------------------
# the track record: does the book land inside the range it was given?
# ---------------------------------------------------------------------------

def snapshot(base: dict, today: Optional[date] = None) -> Optional[dict]:
    """
    Once a week, the range for the book's next twelve months: nominal
    return, no deposits, no tax, costs in, today's risk, the world history.
    In a year it is compared with what the book actually did.
    """
    np = _np()
    today = today or date.today()
    store.init_db()
    with store.conn() as c:
        last = c.execute("SELECT MAX(d) d FROM fc_snap").fetchone()["d"]
    if last and (today - date.fromisoformat(last)).days < SNAP_EVERY:
        return None
    risk = base.get("risk") or {}
    if not risk.get("available") or not base.get("value"):
        return None
    tab = table(hist(), "world")
    idx, eps = draw(tab, PATHS, 1)
    cost = (base.get("cost_pct") if base.get("cost_pct") is not None else INDEX_COST) / 100
    r = simulate(tab, idx, eps, years=1, v0=1.0, beta=risk["beta"], s=risk["s"], cost=cost,
                 monthly=0.0, growth=0.0, dep_years=0, box3="none")
    qv = {f"p{k}": round(float(v) - 1, 5) for k, v in zip(QS, np.percentile(r["nom"][1], QS))}
    row = {"d": today.isoformat(), "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "v0": base["value"], "beta": risk["beta"], "s": risk["s"], "cost": cost, "q": json.dumps(qv)}
    with store.conn() as c:
        c.execute("INSERT OR REPLACE INTO fc_snap (d,created,v0,beta,s,cost,q) VALUES (:d,:created,:v0,:beta,:s,:cost,:q)", row)
    return {**row, "q": qv}


def band_of(x: float, q: dict) -> str:
    """Where an actual return fell in the range it was given."""
    if x < q["p5"]:
        return "below 5%"
    for lo, hi in ((5, 10), (10, 25), (25, 50), (50, 75), (75, 90), (90, 95)):
        if x < q[f"p{hi}"]:
            return f"{lo}–{hi}%"
    return "above 95%"


def track(curve: Optional[list] = None, today: Optional[date] = None) -> dict:
    """Every snapshot; for the ones a year old, the book's actual time-weighted return beside its range."""
    today = today or date.today()
    store.init_db()
    with store.conn() as c:
        snaps = [dict(r) for r in c.execute("SELECT * FROM fc_snap ORDER BY d")]
    due = [s for s in snaps if (today - date.fromisoformat(s["d"])).days >= 365]
    out = {"n": len(snaps), "first": snaps[0]["d"] if snaps else None, "rows": [],
           "next_due": None,
           "latest": {"d": snaps[-1]["d"], "q": {k: round(v * 100, 1) for k, v in json.loads(snaps[-1]["q"]).items()},
                      "beta": snaps[-1]["beta"], "s": snaps[-1]["s"]} if snaps else None}
    pend = [s for s in snaps if s not in due]
    if pend:
        out["next_due"] = (date.fromisoformat(pend[0]["d"]) + timedelta(days=365)).isoformat()
    if not due:
        return out
    if curve is None:
        from . import service
        curve = store.cache_through("fcst:curve", 6 * 3600,
                                    lambda: {"series": [{"d": p["d"], "x": p["x"]} for p in
                                                        (service.dashboard("SP500").get("curve") or {}).get("series") or []]}
                                    ).get("series") or []
    xs = sorted((p["d"], p["x"]) for p in curve if p.get("x"))

    def x_on(d):
        best = None
        for dd, x in xs:
            if dd <= d:
                best = x
            else:
                break
        return best
    for s in due:
        d0 = s["d"]
        d1 = (date.fromisoformat(d0) + timedelta(days=365)).isoformat()
        a, b = x_on(d0), x_on(d1)
        q = json.loads(s["q"])
        if a and b:
            act = b / a - 1
            out["rows"].append({"d": d0, "to": d1, "actual": round(act * 100, 2),
                                "range": {k: round(v * 100, 1) for k, v in q.items()}, "band": band_of(act, q)})
    inside = [r for r in out["rows"] if r["band"] not in ("below 5%", "above 95%")]
    out["inside_pct"] = round(len(inside) / len(out["rows"]) * 100) if out["rows"] else None
    return out


# ---------------------------------------------------------------------------
# what the page loads
# ---------------------------------------------------------------------------

def view(p: Optional[dict] = None) -> dict:
    base = baseline()
    try:
        snapshot(base)
    except Exception:
        pass
    h = hist()
    out = {"base": base, "run": run(p, base), "box3": BOX3, "thesis": h.get("thesis"),
           "about": h.get("_about"), "summary": h.get("summary"), "defaults": DEFAULTS,
           "horizons": list(HORIZONS)}
    try:
        out["track"] = track()
    except Exception as e:
        out["track"] = {"n": 0, "rows": [], "error": f"{e.__class__.__name__}: {e}"[:200]}
    return out


# ---------------------------------------------------------------------------
# building the seed (once, from the downloaded files; the app reads the JSON)
#
#   python -m pf.fcst seed JSTdatasetR6.xlsx ie_data.xls
#
# JST: https://www.macrohistory.net/database/ (CC BY-NC-SA 4.0)
# Shiller: https://shillerdata.com/ (ie_data.xls)
# ---------------------------------------------------------------------------

JST_CITE = ("Òscar Jordà, Moritz Schularick, and Alan M. Taylor. 2017. “Macrofinancial History and the New Business "
            "Cycle Facts.” NBER Macroeconomics Annual 2016, volume 31, edited by Martin Eichenbaum and Jonathan A. "
            "Parker. Chicago: University of Chicago Press. Equity returns: Jordà, Knoll, Kuvshinov, Schularick and "
            "Taylor (2019), “The Rate of Return on Everything, 1870–2015”, QJE 134(3). Database release R6.")


def build_seed(jst_xlsx: str, shiller_xls: str) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(jst_xlsx, read_only=True, data_only=True)
    rows = list(wb[wb.sheetnames[0]].iter_rows(values_only=True))
    with open(shiller_xls, "rb") as f:
        cp = cape_fit(f.read())
    return {
        "_about": {
            "made": date.today().isoformat(),
            "source": "Jordà-Schularick-Taylor Macrohistory Database, release R6 (1870-2020), "
                      "https://www.macrohistory.net/database/; Robert Shiller's ie_data.xls, https://shillerdata.com/",
            "cite": JST_CITE,
            "license": "Derived from the Macrohistory Database under Creative Commons Attribution-NonCommercial-"
                       "ShareAlike 4.0 (https://creativecommons.org/licenses/by-nc-sa/4.0/). This file is shared "
                       "under the same licence, for non-commercial use.",
            "method": "Real return = (1 + eq_tr) / (1 + CPI inflation) - 1, in each country's own currency. World = "
                      "those returns weighted by the previous year's GDP in dollars (gdp / xrusd), with Dutch "
                      "inflation and the Dutch bill rate beside them. Country-years with inflation beyond ±50% "
                      "(hyperinflations, currency reforms) are dropped. Currency moves are left out: the dataset's "
                      "exchange rates are annual averages, which put jumps into converted returns that never "
                      "happened (it has the guilder at 6.50 to the dollar in 1946).",
        },
        **seed_parts(rows),
        "cape": {**cp, "source": "seed"},
    }


def seed_parts(rows: list, min_n: int = 5, home: str = "NLD") -> dict:
    """
    The JST sheet (header row first) as the world, the countries, the money
    test and a summary. A world year needs `min_n` markets; `home` supplies
    the inflation and the cash rate.
    """
    import collections
    import statistics as st
    ix = {h: i for i, h in enumerate(rows[0])}
    D: dict = collections.defaultdict(dict)
    for r in rows[1:]:
        D[r[ix["iso"]]][int(r[ix["year"]])] = {k: r[ix[k]] for k in ("cpi", "xrusd", "eq_tr", "bill_rate", "money", "gdp")}
    eq = sorted(c for c in D if any(v.get("eq_tr") is not None for v in D[c].values()))

    def infl(c, y):
        a, b = D[c].get(y - 1, {}), D[c].get(y, {})
        if a.get("cpi") and b.get("cpi"):
            return b["cpi"] / a["cpi"] - 1
        return None

    countries, dropped = {}, []
    for c in eq:
        ys, re_, inf_, bill_ = [], [], [], []
        for y in sorted(D[c]):
            e, pi = D[c][y].get("eq_tr"), infl(c, y)
            if e is None:
                continue
            if pi is None:                  # the first year of a series has no inflation to set it against
                continue
            if abs(pi) > INFL_CAP:
                dropped.append(f"{c} {y}")
                continue
            b = D[c][y].get("bill_rate")
            ys.append(y)
            re_.append(round((1 + e) / (1 + pi) - 1, 6))
            inf_.append(round(pi, 6))
            bill_.append(round((1 + b) / (1 + pi) - 1, 6) if b is not None else None)
        countries[c] = {"years": ys, "real": re_, "inf": inf_, "bill": bill_}

    # The world: each country's real return in its own currency, weighted by
    # the size of its economy the year before (in dollars). Dutch inflation
    # and the Dutch cash rate go with it: the investor is Dutch.
    wy, wr, wi, wb_, wn = [], [], [], [], []
    for y in range(min(min(v["years"]) for v in countries.values() if v["years"]),
                   max(max(v["years"]) for v in countries.values() if v["years"]) + 1):
        num = den = 0.0
        k = 0
        for c, s in countries.items():
            if y not in s["years"]:
                continue
            prev = D[c].get(y - 1, {})
            if not prev.get("gdp") or not prev.get("xrusd"):
                continue
            w = prev["gdp"] / prev["xrusd"]
            num += w * s["real"][s["years"].index(y)]
            den += w
            k += 1
        pi_nl = infl(home, y)
        b_nl = D[home].get(y, {}).get("bill_rate")
        if k < min_n or pi_nl is None or b_nl is None:
            continue
        wy.append(y)
        wr.append(round(num / den, 6))
        wi.append(round(pi_nl, 6))
        wb_.append(round((1 + b_nl) / (1 + pi_nl) - 1, 6))
        wn.append(k)

    # Does money growth make stocks grow? Country by decade.
    pts = []
    for c in eq:
        for d0 in range(1870, 2020, 10):
            a, b = D[c].get(d0, {}), D[c].get(d0 + 10, {})
            if not all(v is not None for v in (a.get("money"), b.get("money"), a.get("cpi"), b.get("cpi"))):
                continue
            rets = [D[c].get(y, {}).get("eq_tr") for y in range(d0 + 1, d0 + 11)]
            if any(v is None for v in rets):
                continue
            mg = (b["money"] / a["money"]) ** 0.1 - 1
            pi = (b["cpi"] / a["cpi"]) ** 0.1 - 1
            if abs(pi) > 0.3 or abs(mg) > 0.5:
                continue
            nomr = math.prod(1 + v for v in rets) ** 0.1 - 1
            pts.append({"c": c, "d": d0, "money": round(mg * 100, 2), "inf": round(pi * 100, 2),
                        "real": round(((1 + nomr) / (1 + pi) - 1) * 100, 2), "nom": round(nomr * 100, 2)})
    col = lambda k: [p[k] for p in pts]
    buckets = []
    for lo, hi, lab in ((-99, 4, "under 4%"), (4, 7, "4–7%"), (7, 10, "7–10%"), (10, 99, "10% or more")):
        sel = [p for p in pts if lo <= p["money"] < hi]
        if not sel:
            continue
        buckets.append({"label": lab, "n": len(sel), "inf": round(st.median(p["inf"] for p in sel), 1),
                        "real": round(st.median(p["real"] for p in sel), 1),
                        "loss_pct": round(sum(p["real"] < 0 for p in sel) / len(sel) * 100)})
    thesis = {"points": pts, "buckets": buckets,
              "corr": {"money_inf": round(st.correlation(col("money"), col("inf")), 2),
                       "money_real": round(st.correlation(col("money"), col("real")), 2),
                       "money_nom": round(st.correlation(col("money"), col("nom")), 2),
                       "inf_real": round(st.correlation(col("inf"), col("real")), 2)}}

    def geo(x):
        return math.exp(sum(math.log(1 + v) for v in x) / len(x)) - 1
    summary = {"world": {"from": wy[0], "to": wy[-1], "n": len(wy), "geo": round(geo(wr), 5),
                         "mean": round(sum(wr) / len(wr), 5), "sd": round(st.pstdev(wr), 5),
                         "worst": [round(min(wr), 4), wy[wr.index(min(wr))]],
                         "best": [round(max(wr), 4), wy[wr.index(max(wr))]],
                         "inf_geo": round(geo(wi), 5), "bill_geo": round(geo(wb_), 5)},
               "countries": {c: {"from": s["years"][0], "to": s["years"][-1], "n": len(s["years"]),
                                 "geo": round(geo(s["real"]), 5), "sd": round(st.pstdev(s["real"]), 5),
                                 "worst": [round(min(s["real"]), 4), s["years"][s["real"].index(min(s["real"]))]]}
                             for c, s in countries.items()},
               "dropped": dropped}
    return {"world": {"years": wy, "real": wr, "inf": wi, "bill": wb_, "n": wn},
            "countries": countries, "thesis": thesis, "summary": summary}


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 4 and sys.argv[1] == "seed":
        s = build_seed(sys.argv[2], sys.argv[3])
        with open(SEED_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, separators=(",", ":"))
        print("wrote", SEED_PATH, os.path.getsize(SEED_PATH), "bytes;",
              len(s["world"]["years"]), "world years,", len(s["countries"]), "countries")
    else:
        print(__doc__)
