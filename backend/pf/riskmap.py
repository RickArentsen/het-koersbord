"""
Risk by holding: who carries the book's risk, what moves together, and what
the book bets on beyond "the market".

Weight says where the money is, not where the risk is. A 3% position in a
share that swings 100% a year and moves with everything else you own carries
more of the book's risk than a 10% position in one that swings 30%. Measured
here on TODAY's holdings at today's weights, over the past year:

  SHARE OF RISK   each holding's part of the book's variance,
                  w_i (Sw)_i / w'Sw. The parts add to 100%; a holding that
                  moves against the rest comes out negative.
  WHAT MOVES      holdings grouped by how they actually moved together,
  TOGETHER        whatever sector they are filed under.
  STYLE           the book's return regressed on the market, the dollar and
                  seven style and industry spreads.

Everything is on five-day returns (risk.HORIZON): Amsterdam and Frankfurt
close four and a half hours before New York, and on one day's returns a
European line looks unrelated to an American one when it is not.

What this is not. It is today's book replayed over a year it was partly
chosen with hindsight for. Forty-eight names on fifty independent weeks is a
noisy matrix, so it is pulled a fifth of the way toward "every pair equally
correlated" before use; the ranking is sturdy, the second decimal is not. And
correlations rise in a sell-off: the diversification shown is the calm-year
figure.
"""

from __future__ import annotations

import hashlib
import math
import statistics
import time
from typing import Optional

from . import risk

H = risk.HORIZON
YEAR = 252
PER_YEAR = YEAR / H
SHRINK = 0.2
LINK = 0.4                  # average correlation that still counts as one group
WORLD, SP, FX = "URTH", "SPY", "EURUSD=X"

# (key, long, short, name, what it is). US-listed on purpose: they close with
# the shares. A euro account cannot buy them; here they are yardsticks.
STYLES = [
    ("growth", "QQQ", SP, "Nasdaq growth", "the Nasdaq 100 against the S&P 500"),
    ("semis", "SOXX", SP, "Chip makers", "semiconductor shares against the S&P 500"),
    ("momentum", "MTUM", SP, "Momentum", "last year's winners against the index"),
    ("value", "VLUE", SP, "Value", "cheap shares against the index"),
    ("quality", "QUAL", SP, "Quality", "profitable, lightly indebted companies against the index"),
    ("lowvol", "USMV", SP, "Low volatility", "calm shares against the index"),
    ("small", "IWM", SP, "Small companies", "the Russell 2000 against the S&P 500"),
]
PROXIES = sorted({WORLD, SP, FX} | {s[1] for s in STYLES})


def _np():
    import numpy as np
    return np


# --------------------------------------------------------------------------
# prices in, five-day returns out
# --------------------------------------------------------------------------

def _load(tickers: list[str]) -> dict:
    from . import bonds
    got = bonds._download(sorted(set(tickers) | set(PROXIES)), period="2y")
    if len(got.get(SP) or {}) < YEAR:
        raise RuntimeError("Yahoo returned no price history for the yardsticks")
    return {**bonds._pack(got), "fetched_at": time.time()}


def prices(tickers: list[str]) -> dict:
    from . import bonds
    key = "riskmap:px:" + hashlib.md5(",".join(sorted(tickers)).encode()).hexdigest()[:10]
    return bonds._unpack(bonds._cached(key, 6 * 3600, lambda: _load(tickers)))


def _filled(m: dict, days: list[str], stale: int = 6) -> list:
    """`m` on every day of `days`, carrying a close forward up to `stale` days."""
    own = sorted(m)
    return [risk._asof(m, own, d, stale) for d in days]


def windows(series: list, h: int = H) -> list:
    """Log return over the `h` days ending on each day; None where a price is missing."""
    out = [None] * len(series)
    for t in range(h, len(series)):
        a, b = series[t - h], series[t]
        if a and b and a > 0 and b > 0:
            out[t] = math.log(b / a)
    return out


# --------------------------------------------------------------------------
# share of risk
# --------------------------------------------------------------------------

def shrunk_cov(R):
    """
    The sample covariance of the columns of R, pulled SHRINK of the way toward
    the matrix in which every pair has the average correlation.
    """
    np = _np()
    S = np.cov(R, rowvar=False)
    S = np.atleast_2d(S)
    sd = np.sqrt(np.diag(S))
    n = len(sd)
    if n < 2:
        return S
    C = S / np.outer(sd, sd)
    rho = (C.sum() - n) / (n * (n - 1))
    F = rho * np.outer(sd, sd)
    np.fill_diagonal(F, sd ** 2)
    return (1 - SHRINK) * S + SHRINK * F


def contributions(S, w) -> dict:
    """Variance, each holding's share of it, and its correlation with the book."""
    np = _np()
    w = np.asarray(w, dtype=float)
    Sw = S @ w
    var = float(w @ Sw)
    sd = np.sqrt(np.diag(S))
    return {"var": var,
            "share": (w * Sw / var) if var > 0 else np.zeros(len(w)),
            "corr": Sw / (sd * math.sqrt(var)) if var > 0 else np.zeros(len(w)),
            "sd": sd}


def clusters(C, link: float = LINK) -> list[list[int]]:
    """
    Average-linkage groups: keep merging the two groups whose members are, on
    average, most correlated, until no pair of groups reaches `link`.
    """
    np = _np()
    groups = [[i] for i in range(len(C))]
    while len(groups) > 1:
        best, pair = -2.0, None
        for a in range(len(groups)):
            for b in range(a + 1, len(groups)):
                c = float(np.mean(C[np.ix_(groups[a], groups[b])]))
                if c > best:
                    best, pair = c, (a, b)
        if best < link:
            break
        a, b = pair
        groups[a] = groups[a] + groups[b]
        del groups[b]
    return groups


# --------------------------------------------------------------------------
# style
# --------------------------------------------------------------------------

def regress(y, X, lags: int = H - 1) -> Optional[dict]:
    """
    Least squares with an intercept, and standard errors that allow for
    overlapping windows (Newey-West, Bartlett weights, `lags` lags).
    """
    np = _np()
    y = np.asarray(y, dtype=float)
    X = np.column_stack([np.ones(len(y))] + [np.asarray(c, dtype=float) for c in X])
    n, k = X.shape
    if n < k + 30:
        return None
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    e = y - X @ b
    tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1 - float((e ** 2).sum()) / tot if tot > 0 else 0.0
    XtXi = np.linalg.pinv(X.T @ X)
    Xe = X * e[:, None]
    Sm = Xe.T @ Xe
    for l in range(1, lags + 1):
        G = Xe[l:].T @ Xe[:-l]
        Sm += (1 - l / (lags + 1)) * (G + G.T)
    se = np.sqrt(np.maximum(np.diag(XtXi @ Sm @ XtXi), 0))
    return {"b": b[1:], "a": float(b[0]), "se": se[1:], "r2": max(0.0, min(1.0, r2)), "n": n}


def style(book_r: list, f: dict) -> Optional[dict]:
    """
    f: {key: five-day returns aligned with book_r}, holding "market" (world
    shares in dollars), "dollar" (the dollar against the euro) and the style
    spreads. Returns each loading, and how much of the book's variance each
    step explains.
    """
    np = _np()
    keys = ["market", "dollar"] + [s[0] for s in STYLES if s[0] in f]
    full = regress(book_r, [f[k] for k in keys])
    if not full:
        return None
    y = np.asarray(book_r, dtype=float)
    var = float(y.var())
    steps = {}
    for name, ks in (("market", ["market"]), ("dollar", ["market", "dollar"])):
        r = regress(book_r, [f[k] for k in ks])
        steps[name] = r["r2"] if r else None
    rows = []
    for i, k in enumerate(keys):
        x = np.asarray(f[k], dtype=float)
        cov = float(((x - x.mean()) * (y - y.mean())).mean())
        t = float(full["b"][i] / full["se"][i]) if full["se"][i] > 0 else 0.0
        rows.append({"k": k, "beta": round(float(full["b"][i]), 3), "t": round(t, 1),
                     "share": round(float(full["b"][i]) * cov / var * 100, 1) if var > 0 else None})
    return {"rows": rows, "r2": round(full["r2"] * 100, 1), "n": full["n"],
            "r2_market": round(steps["market"] * 100, 1) if steps["market"] is not None else None,
            "r2_dollar": round(steps["dollar"] * 100, 1) if steps["dollar"] is not None else None,
            "own": round((1 - full["r2"]) * 100, 1)}


# --------------------------------------------------------------------------
# the panel
# --------------------------------------------------------------------------

def _list(words: list) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _names(rows: list, n: int = 3) -> str:
    t = [r["label"] for r in rows[:n]]
    more = len(rows) - len(t)
    return ", ".join(t) + f" and {more} more" if more > 0 else _list(t)


STYLE_WORD = {"growth": "Nasdaq growth", "semis": "chip makers", "momentum": "momentum",
              "value": "value", "quality": "quality", "lowvol": "low volatility",
              "small": "small companies"}


def _say(out: dict) -> dict:
    """The panel in sentences: about the holdings, the groups, and the style."""
    rows, b = out["rows"], out["book"]
    say, group, lean = [], None, None
    heavy = [r for r in rows[:8] if r["weight"] > 0.5 and r["ratio"] is not None]
    if heavy:
        r = max(heavy, key=lambda x: x["ratio"])
        say.append(f"{r['label']} is {r['weight']:.1f}% of your money and {r['risk']:.1f}% of your risk.")
    say.append(f"The five holdings that carry the most risk ({_names(rows[:5], 5)}) are "
               f"{b['top5_weight']:.0f}% of the money and {b['top5_risk']:.0f}% of the risk.")
    big = max(rows, key=lambda r: r["weight"])
    if abs(big["risk"] - big["weight"]) >= 1.5:
        say.append(f"Your largest holding, {big['label']} ({big['weight']:.1f}%), carries {big['risk']:.1f}%.")
    if b.get("bets"):
        say.append(f"On their own your holdings swing {b['avg_vol_pct']:.0f}% a year on average; together the "
                   f"book swings {b['vol_pct']:.0f}%. That is the diversification of about "
                   f"{b['bets']:.0f} independent bets, not {out['n']}.")
    calm = [r for r in rows if r["risk"] <= 0.05 and r["weight"] >= 0.7]
    if calm:
        calm.sort(key=lambda r: -r["weight"])
        tot = sum(r["risk"] for r in calm)
        say.append(f"{_names(calm, 4)} are {sum(r['weight'] for r in calm):.0f}% of the money and "
                   + ("take a little risk off the book: they tend to move against the rest."
                      if tot < 0 else f"{tot:.1f}% of the risk."))
    cl = out.get("clusters") or []
    if cl and cl[0]["risk"] >= 30:
        c = cl[0]
        group = (f"The largest group that moves together ({_names([{'label': x} for x in c['labels']], 4)}) "
                 f"is {c['weight']:.0f}% of the money and {c['risk']:.0f}% of the risk.")
    st = out.get("style")
    if st:
        to = [STYLE_WORD[r["k"]] for r in st["rows"] if r["k"] in STYLE_WORD and r["t"] >= 2]
        away = [STYLE_WORD[r["k"]] for r in st["rows"] if r["k"] in STYLE_WORD and r["t"] <= -2]
        tilt = ""
        if to or away:
            tilt = (" Beyond the market it leans" + (" toward " + _list(to) if to else "")
                    + (" and" if to and away else "") + (" away from " + _list(away) if away else "") + ".")
        lean = (f"The market alone explains {st['r2_market']:.0f}% of the book's week-to-week moves; "
                f"with the dollar and seven styles it is {st['r2']:.0f}%, leaving {st['own']:.0f}% "
                f"that is specific to your shares." + tilt)
    return {"say": say, "say_group": group, "say_style": lean}


# --------------------------------------------------------------------------
# a bad day, a bad week: value at risk and expected shortfall, in euros
# --------------------------------------------------------------------------
# Measured, not assumed: today's book replayed over the two years of prices
# fetched here, in euros, a turbo as the share it tracks at its exposure and
# never losing more than it is worth. VaR is the loss exceeded one day (or
# week) in twenty, or in a hundred; ES, the average of the losses beyond it.
# On 21 October 2026 the 95% and 99% VaR came within a tenth of what the
# volatility alone implies, so VaR by itself says little the page did not;
# the tail beyond it did not: the worst 1% of weeks cost 1.34 times what a
# normal distribution with the same volatility gives. That is why the panel
# leads with ES and shows the normal figure beside it.
TAIL_FLOOR = 0.85            # a window counts when holdings worth this much of the book have a price
NORMAL = {"var95": 1.645, "var99": 2.326, "es975": 2.338, "es99": 2.665}   # in standard deviations


def _simple(series: list, h: int) -> list:
    out = [None] * len(series)
    for t in range(h, len(series)):
        a, b = series[t - h], series[t]
        if a and b and a > 0 and b > 0:
            out[t] = b / a - 1
    return out


def tail_stats(pnl: list[float]) -> dict:
    """VaR and ES of a list of profits and losses, as positive losses; and what a normal curve would say."""
    s = sorted(pnl)
    n = len(s)
    sd = statistics.pstdev(pnl)
    var = lambda a: -s[min(n - 1, int(a * n))]
    es = lambda a: -statistics.fmean(s[:max(1, int(a * n))])
    out = {"var95": var(0.05), "var99": var(0.01), "es975": es(0.025), "es99": es(0.01), "worst": -s[0], "sd": sd, "n": n}
    out["normal"] = {k: f * sd for k, f in NORMAL.items()}
    return out


def tail(book: dict, px: dict, days: list[str]) -> Optional[dict]:
    from . import bonds, pricing
    from .morning import brands
    lab = lambda t, name: (brands(t, name or "") or [t.split(".")[0]])[0]
    items = [{"t": r["t"], "v": r["w"] * book["total"], "x": None, "ccy": r["ccy"], "label": lab(r["t"], r["name"])}
             for r in book["rows"]]
    ccy_of = {r["t"]: r["ccy"] for r in book["rows"]}
    for x in book.get("turbos") or []:
        suf = x["t"].rsplit(".", 1)[1].upper() if "." in x["t"] else ""
        ccy = ccy_of.get(x["t"]) or ("GBp" if suf == "L" else pricing.SUFFIX_CCY.get(suf, "USD"))
        items.append({"t": x["t"], "v": x["value"], "x": x["exposure"] if x["exposure"] is not None else x["value"],
                      "ccy": ccy, "label": "turbo on " + lab(x["t"], "")})
    value = sum(i["v"] for i in items)
    if value <= 0:
        return None
    own = value + (book.get("cash") or 0)
    eur = {}
    for i in items:
        if i["t"] not in eur:
            eur[i["t"]] = _filled(bonds._to_eur(px.get(i["t"]) or {}, i["ccy"], px), days)
    out = {"from": days[0], "to": days[-1], "value_eur": round(value), "own_eur": round(own),
           "cash_eur": round(book.get("cash") or 0), "turbos": sum(1 for i in items if i["x"] is not None)}
    for h, key in ((1, "day"), (H, "week")):
        R = {t: _simple(v, h) for t, v in eur.items()}
        pnl, ends, parts = [], [], []
        for t in range(h, len(days)):
            tot = cov = 0.0
            row = {}
            for k, it in enumerate(items):
                r = R[it["t"]][t]
                if r is None:
                    continue
                p = it["v"] * r if it["x"] is None else max(-it["v"], it["x"] * r)
                tot += p
                cov += it["v"]
                row[k] = p
            if cov < TAIL_FLOOR * value:
                continue
            k_ = value / cov              # the holdings without a price that day move like the rest
            pnl.append(tot * k_)
            ends.append(days[t])
            parts.append({k: p * k_ for k, p in row.items()})
        if len(pnl) < 200:
            return None
        st = tail_stats(pnl)
        worst = min(range(len(pnl)), key=lambda k: pnl[k])
        blk = {k: round(v) for k, v in st.items() if k not in ("normal", "n", "sd")}
        blk.update({"sd": round(st["sd"]), "n": st["n"], "worst_d": ends[worst],
                    "normal": {k: round(v) for k, v in st["normal"].items()},
                    "pct": {k: round(st[k] / own * 100, 1) for k in ("var95", "var99", "es975", "es99", "worst")} if own > 0 else None,
                    "last_year_es975": round(tail_stats(pnl[-YEAR:])["es975"]) if len(pnl) >= YEAR else None})
        if key == "week":
            # separate episodes: the worst weeks, each at least two weeks from the next
            eps = []
            for k in sorted(range(len(pnl)), key=lambda k: pnl[k]):
                if pnl[k] >= 0 or len(eps) == 3:
                    break
                if all(abs(k - e) >= 10 for e in eps):
                    eps.append(k)
            blk["episodes"] = [{"end": ends[k], "loss": round(-pnl[k]),
                                "pct": round(-pnl[k] / own * 100, 1) if own > 0 else None} for k in eps]
            # who carries the tail: each holding's average part of the worst 2.5% of weeks
            m = max(1, int(0.025 * len(pnl)))
            bad = sorted(range(len(pnl)), key=lambda k: pnl[k])[:m]
            share = {}
            for k in bad:
                for j, p in parts[k].items():
                    share[j] = share.get(j, 0.0) + p / m
            es = st["es975"]
            blk["drivers"] = [{"t": items[j]["t"], "label": items[j]["label"], "loss": round(-v),
                               "share": round(-v / es * 100, 1) if es else None,
                               "weight": round(items[j]["v"] / value * 100, 1)}
                              for j, v in sorted(share.items(), key=lambda kv: kv[1])[:8]]
        out[key] = blk
    return out


def build() -> dict:
    from . import bonds
    from .morning import brands
    np = _np()
    book = bonds._book()
    if not book["rows"]:
        return {"available": False, "why": "No priced holdings."}
    px = prices(sorted({r["t"] for r in book["rows"]} | {x["t"] for x in book.get("turbos") or []}))
    days = sorted(px.get(SP) or {})
    if len(days) < YEAR + H:
        return {"available": False, "why": "Not enough price history for the yardsticks."}

    # Every holding in euros on New York's calendar, then five-day returns.
    cols, used, skipped = {}, [], []
    for r in book["rows"]:
        m = bonds._to_eur(px.get(r["t"]) or {}, r["ccy"], px)
        w = windows(_filled(m, days))
        if sum(1 for v in w[-YEAR:] if v is not None) < YEAR:
            n = sum(1 for v in w if v is not None)
            skipped.append({"t": r["t"], "name": r["name"], "weight": round(r["w"] * 100, 2),
                            "why": "no price history" if not m else f"only {n} days of history"})
            continue
        cols[r["t"]] = w
        used.append(r)
    if len(used) < 2:
        return {"available": False, "why": "Fewer than two holdings have a year of prices."}

    R = np.array([[cols[r["t"]][t] for r in used] for t in range(len(days) - YEAR, len(days))])
    w = np.array([r["w"] for r in used], dtype=float)
    w = w / w.sum()
    S = shrunk_cov(R) * PER_YEAR
    c = contributions(S, w)
    vol = math.sqrt(c["var"])
    avg = float(w @ c["sd"])

    rows = []
    for i, r in enumerate(used):
        wt, sh = float(w[i]) * 100, float(c["share"][i]) * 100
        rows.append({"t": r["t"], "name": r["name"],
                     "label": (brands(r["t"], r["name"] or "") or [r["t"].split(".")[0]])[0],
                     "sector": r.get("sector") or "Unclassified",
                     "weight": round(wt, 2), "risk": round(sh, 2),
                     "ratio": round(sh / wt, 2) if wt > 0 else None,
                     "vol": round(float(c["sd"][i]) * 100, 1), "corr": round(float(c["corr"][i]), 2)})
    order = sorted(range(len(rows)), key=lambda i: -rows[i]["risk"])
    by_sector: dict = {}
    for r in rows:
        g = by_sector.setdefault(r["sector"], {"label": r["sector"], "weight": 0.0, "risk": 0.0, "n": 0})
        g["weight"] += r["weight"]
        g["risk"] += r["risk"]
        g["n"] += 1

    # Groups come from the correlations as measured, not the shrunk ones:
    # shrinking pulls every pair toward the average, which is the one thing
    # a search for what stands out from the average should not do.
    C = np.corrcoef(R, rowvar=False)
    cl = []
    for g in clusters(C):
        if len(g) < 2:
            continue
        g = sorted(g, key=lambda i: -rows[i]["weight"])
        sub = C[np.ix_(g, g)]
        cl.append({"tickers": [rows[i]["t"] for i in g], "labels": [rows[i]["label"] for i in g],
                   "weight": round(sum(rows[i]["weight"] for i in g), 1),
                   "risk": round(sum(rows[i]["risk"] for i in g), 1),
                   "corr": round(float((sub.sum() - len(g)) / (len(g) * (len(g) - 1))), 2)})
    cl.sort(key=lambda x: -x["risk"])

    top5 = order[:5]
    out = {
        "available": True, "asof": days[-1], "from": days[-YEAR], "n": len(rows),
        "horizon_days": H, "shrink": SHRINK, "link": LINK,
        "avg_corr": round(float((C.sum() - len(rows)) / (len(rows) * (len(rows) - 1))), 2),
        "book": {"vol_pct": round(vol * 100, 1), "avg_vol_pct": round(avg * 100, 1),
                 "bets": round((avg / vol) ** 2, 1) if vol > 0 else None,
                 "top5_weight": round(sum(rows[i]["weight"] for i in top5), 1),
                 "top5_risk": round(sum(rows[i]["risk"] for i in top5), 1),
                 "covered_pct": round(sum(r["w"] for r in used) * 100, 1)},
        "rows": [rows[i] for i in order],
        "sectors": sorted(({**g, "weight": round(g["weight"], 1), "risk": round(g["risk"], 1)}
                           for g in by_sector.values()), key=lambda g: -g["risk"]),
        "clusters": cl, "skipped": skipped, "fetched_at": time.time(),
    }

    # Style, on the whole two years: the book's return each week from the
    # holdings that had a price, re-weighted.
    try:
        fx = windows(_filled(px.get(FX) or {}, days))
        fac = {"market": windows(_filled(px.get(WORLD) or {}, days)),
               "dollar": [None if v is None else -v for v in fx]}
        for k, lo, sh, _, _ in STYLES:
            a, b = windows(_filled(px.get(lo) or {}, days)), windows(_filled(px.get(sh) or {}, days))
            fac[k] = [None if (x is None or y is None) else x - y for x, y in zip(a, b)]
        allw = {r["t"]: windows(_filled(bonds._to_eur(px.get(r["t"]) or {}, r["ccy"], px), days))
                for r in book["rows"]}
        wt = {r["t"]: r["w"] for r in book["rows"]}
        yy, ff = [], {k: [] for k in fac}
        for t in range(H, len(days)):
            if any(fac[k][t] is None for k in fac):
                continue
            num = den = 0.0
            for tk, ser in allw.items():
                if ser[t] is not None:
                    num += wt[tk] * ser[t]
                    den += wt[tk]
            if den < 0.8:
                continue
            yy.append(num / den)
            for k in fac:
                ff[k].append(fac[k][t])
        st = style(yy, ff)
        if st:
            meta = {"market": ("The market", "world shares, in dollars"),
                    "dollar": ("The dollar", "the dollar against the euro"),
                    **{s[0]: (s[3], s[4]) for s in STYLES}}
            for r in st["rows"]:
                r["name"], r["what"] = meta[r["k"]]
            # Beta in euros against the world index, for comparison with PORT's.
            wx = [m + d for m, d in zip(ff["market"], ff["dollar"])]
            one = regress(yy, [wx])
            st["beta_world"] = round(float(one["b"][0]), 2) if one else None
            st["weeks"] = round(len(yy) / H)
            out["style"] = st
    except Exception as e:
        out["style_error"] = f"{e.__class__.__name__}: {e}"[:200]

    try:
        out["tail"] = tail(book, px, days)
    except Exception as e:
        out["tail_error"] = f"{e.__class__.__name__}: {e}"[:200]

    out.update(_say(out))
    return out
