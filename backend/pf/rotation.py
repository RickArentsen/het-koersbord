"""
RRG — relative rotation: which holdings and sectors lead the benchmark,
which are fading, and which way each is turning.

The graph was invented by Julius de Kempenaer and has been on Bloomberg since
2011. Each name is one point with a tail behind it:

  right of centre   its price has been gaining on the benchmark
  above centre      that gain is speeding up

so a name travels clockwise: IMPROVING (still behind, catching up), LEADING
(ahead and pulling away), WEAKENING (ahead, losing pace), LAGGING (behind and
falling further).

His two indicators, JdK RS-Ratio and JdK RS-Momentum, are proprietary. What
is drawn here is an open approximation with the same construction and the
same reading, not his numbers:

  relative strength   price / benchmark
  trend (x)           how far relative strength sits from its own 14-period
                      average, in units of how far it usually strays (its
                      standard deviation over a year), smoothed, plus 100
  momentum (y)        the change in the trend from one period to the next, in
                      units of its usual size, smoothed, plus 100

Dividing by "usual" is what lets a utility and a chip maker share one chart.
Because the vertical axis is the rate of change of the horizontal one, the
rotation is clockwise by construction; on the eleven US sectors over two
years it turns clockwise in four steps out of five.

Weekly by default, on Friday closes. Holdings are compared in euros, so the
benchmark is converted too and the dollar cancels out of the ratio.
"""

from __future__ import annotations

import hashlib
import math
import time
from datetime import date
from typing import Optional

from . import risk

N = 14              # periods in the relative-strength average
USUAL = 52          # periods over which "usual" is measured
SMOOTH = 5
KEEP = 40           # points per line sent to the page
FLAT = 1e-6         # a spread, in percent, below which a series has not moved against its benchmark
FX = "EURUSD=X"

SECTORS = [("XLK", "Technology"), ("XLC", "Communication"), ("XLY", "Consumer discretionary"),
           ("XLP", "Consumer staples"), ("XLV", "Health care"), ("XLF", "Financials"),
           ("XLI", "Industrials"), ("XLB", "Materials"), ("XLE", "Energy"),
           ("XLU", "Utilities"), ("XLRE", "Real estate")]
MARKETS = [("SPY", "S&P 500"), ("QQQ", "Nasdaq 100"), ("IWM", "US small companies"),
           ("VGK", "Europe"), ("EWJ", "Japan"), ("EEM", "Emerging markets"), ("FXI", "China"),
           ("SOXX", "Chip makers"), ("IGV", "Software"), ("XBI", "Biotech"),
           ("KRE", "US regional banks"), ("GLD", "Gold"), ("USO", "Oil"),
           ("TLT", "Long Treasuries"), ("HYG", "High-yield bonds")]
BENCH = {"WORLD": ("URTH", "MSCI World"), "SP500": ("SPY", "S&P 500"),
         "NDX": ("QQQ", "Nasdaq 100"), "BOOK": (None, "Your book")}
UNIVERSES = {"book": "Your holdings", "sectors": "US sectors", "markets": "Markets"}
DEFAULT_BENCH = {"book": "WORLD", "sectors": "SP500", "markets": "WORLD"}
QUADS = ("leading", "weakening", "lagging", "improving")


# --------------------------------------------------------------------------
# the two indicators
# --------------------------------------------------------------------------

def _roll(xs: list, n: int, min_n: int, fn) -> list:
    out = [None] * len(xs)
    for t in range(len(xs)):
        w = [v for v in xs[max(0, t - n + 1):t + 1] if v is not None]
        if len(w) >= min_n:
            out[t] = fn(w)
    return out


def _mean(w: list) -> float:
    return sum(w) / len(w)


def _sd(w: list) -> float:
    m = _mean(w)
    return math.sqrt(sum((v - m) ** 2 for v in w) / (len(w) - 1)) if len(w) > 1 else 0.0


def _ema(xs: list, span: int) -> list:
    a, out, cur = 2.0 / (span + 1), [None] * len(xs), None
    for t, v in enumerate(xs):
        if v is None:
            continue
        cur = v if cur is None else a * v + (1 - a) * cur
        out[t] = cur
    return out


def rotate(p: list, b: list, n: int = N, usual: int = USUAL, smooth: int = SMOOTH) -> tuple[list, list]:
    """
    (trend, momentum) for a price series against a benchmark series of the
    same length; None where there is not yet enough history.
    """
    rs = [None if (x is None or y is None or y <= 0 or x <= 0) else 100.0 * x / y for x, y in zip(p, b)]
    avg = _roll(rs, n, n, _mean)
    dev = [None if (r is None or a is None or a <= 0) else (r / a - 1.0) * 100.0 for r, a in zip(rs, avg)]
    sd = _roll(dev, usual, min(usual, 26), _sd)
    # A tracker measured against its own index never leaves it: the deviation
    # is rounding error, a trillionth of a percent, and dividing rounding error
    # by its own spread draws a confident loop out of nothing. No reading.
    x = _ema([None if (d is None or s is None or s <= FLAT) else d / s for d, s in zip(dev, sd)], smooth)
    x = [None if v is None else 100.0 + v for v in x]
    step = [None] + [None if (a is None or c is None) else c - a for a, c in zip(x, x[1:])]
    ssd = _roll(step, usual, min(usual, 26), _sd)
    y = _ema([None if (d is None or s is None or s <= FLAT) else d / s for d, s in zip(step, ssd)], smooth)
    return x, [None if v is None else 100.0 + v for v in y]


def quadrant(x: float, y: float) -> str:
    return ("leading" if y >= 100 else "weakening") if x >= 100 else ("improving" if y >= 100 else "lagging")


NEXT = {"improving": "leading", "leading": "weakening", "weakening": "lagging", "lagging": "improving"}


def motion(pts: list, tail: int = 4) -> Optional[dict]:
    """
    How a name is moving on the graph, from its last few points:

      heading    the direction of the last step in degrees: 0 is toward a
                 stronger trend (right), 90 toward rising momentum (up)
      speed      the average length of the last `tail` steps
      distance   how far the name sits from the centre, the strength of the reading
      clockwise  the share of turns in the last year that went clockwise,
                 the way the graph usually rotates
    """
    p = [q for q in pts if q]
    if len(p) < 3:
        return None
    steps = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(p, p[1:])]
    last = steps[-1]
    tail_steps = steps[-tail:]
    speed = sum(math.hypot(*s) for s in tail_steps) / len(tail_steps)
    turns = [a[0] * b[1] - a[1] * b[0] for a, b in zip(steps[-13:], steps[-12:])]
    turns = [t for t in turns if abs(t) > 1e-9]
    x, y = p[-1]
    return {"heading": round(math.degrees(math.atan2(last[1], last[0])), 0), "speed": round(speed, 2),
            "distance": round(math.hypot(x - 100, y - 100), 2),
            "clockwise": round(sum(1 for t in turns if t < 0) / len(turns) * 100) if turns else None}


def history(pts: list) -> dict:
    """The share of the last periods spent in each quadrant, and the order of the last moves between them."""
    qs = [quadrant(*q) for q in pts if q]
    if not qs:
        return {"share": {}, "path": []}
    share = {q: round(qs.count(q) / len(qs) * 100) for q in QUADS}
    path = []
    for q in qs:
        if not path or path[-1][0] != q:
            path.append([q, 1])
        else:
            path[-1][1] += 1
    return {"share": share, "path": path[-8:]}


def reading(it: dict, per: str) -> str:
    """One sentence on where a name is and which way it is going."""
    q, m = it.get("q"), it.get("motion")
    if not q or not m:
        return ""
    since = it.get("since") or 0
    h = m["heading"]
    toward = ("rising momentum" if 45 <= h < 135 else "a weaker trend" if h >= 135 or h < -135
              else "falling momentum" if -135 <= h < -45 else "a stronger trend")
    out = f"{q.capitalize()} for {since} {per}{'s' if since != 1 else ''}, heading toward {toward}"
    nxt = NEXT[q]
    # clockwise when the step points along the circle round the centre, clockwise:
    # the step dotted with the radius turned a quarter to the right
    rx, ry = it["x"] - 100, it["y"] - 100
    sx, sy = math.cos(math.radians(h)), math.sin(math.radians(h))
    clockwise = sx * ry - sy * rx > 0
    if m["distance"] < 1.0:
        out += "; it sits close to the centre, moving with the benchmark, so the quadrant means little"
    elif clockwise:
        out += f"; on the usual clockwise path the next stop is {nxt}"
    else:
        out += "; that is against the usual clockwise path"
    return out + "."


def followed(series: list, p_all: list, b_all: list, horizons: tuple) -> dict:
    """
    What came after a name crossed into each quadrant, across every name in
    the universe: its return against the benchmark over the next periods.
    """
    out = {q: {h: [] for h in horizons} for q in QUADS}
    for (xs, ys), p, b in zip(series, p_all, b_all):
        prev = None
        for t in range(len(xs)):
            if xs[t] is None or ys[t] is None:
                prev = None
                continue
            q = quadrant(xs[t], ys[t])
            if prev is not None and q != prev:
                for h in horizons:
                    if t + h < len(p) and p[t] and p[t + h] and b[t] and b[t + h]:
                        out[q][h].append((p[t + h] / p[t]) / (b[t + h] / b[t]) - 1)
            prev = q
    res = {}
    for q in QUADS:
        res[q] = {}
        for h in horizons:
            v = sorted(out[q][h])
            if not v:
                res[q][str(h)] = None
                continue
            res[q][str(h)] = {"n": len(v), "avg": round(sum(v) / len(v) * 100, 2),
                              "med": round(v[len(v) // 2] * 100, 2),
                              "beat": round(sum(1 for x in v if x > 0) / len(v) * 100)}
    return res


# --------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------

def _load(tickers: list[str]) -> dict:
    from . import bonds
    got = bonds._download(tickers, period="3y")
    if len(got.get("SPY") or {}) < 300:
        raise RuntimeError("Yahoo returned no price history")
    return {**bonds._pack(got), "fetched_at": time.time()}


def prices(tickers: list[str]) -> dict:
    from . import bonds
    tickers = sorted(set(tickers) | {"SPY", FX})
    key = "rrg:px:" + hashlib.md5(",".join(tickers).encode()).hexdigest()[:10]
    return bonds._unpack(bonds._cached(key, 6 * 3600, lambda: _load(tickers)))


def periods(days: list[str], freq: str) -> list[str]:
    """The day each period ends on: every day, or the last trading day of each week."""
    if freq == "d":
        return days
    out = []
    for a, b in zip(days, days[1:] + [None]):
        if b is None or date.fromisoformat(a).isocalendar()[:2] != date.fromisoformat(b).isocalendar()[:2]:
            out.append(a)
    return out


def _on(m: dict, ends: list[str]) -> list:
    own = sorted(m)
    return [risk._asof(m, own, d, 6) for d in ends]


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

def _list(words: list) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _say(items: list, universe: str, per: str) -> list[str]:
    say = []
    live = [i for i in items if i.get("q")]
    if not live:
        return say
    if universe == "book":
        tot = sum(i["weight"] or 0 for i in live) or 1
        share = {q: sum(i["weight"] or 0 for i in live if i["q"] == q) / tot * 100 for q in QUADS}
        say.append(f"By value, {share['leading']:.0f}% of your book is leading, "
                   f"{share['weakening']:.0f}% weakening, {share['lagging']:.0f}% lagging and "
                   f"{share['improving']:.0f}% improving.")
        big = sorted(live, key=lambda i: -(i["weight"] or 0))[:5]
        say.append("Your five largest: " + ", ".join(f"{i['label']} {i['q']}" for i in big) + ".")
    else:
        for q in ("leading", "lagging"):
            names = [i["label"] for i in sorted(live, key=lambda i: -abs(i["x"] - 100)) if i["q"] == q]
            if names:
                say.append(f"{q.capitalize()}: {_list(names[:6])}.")
    moved = [i for i in live if i.get("was") and i["was"] != i["q"]]
    for q in ("lagging", "weakening", "leading", "improving"):
        names = [i["label"] for i in sorted(moved, key=lambda i: -(i["weight"] or 0)) if i["q"] == q]
        if names:
            say.append(f"Crossed into {q} {'this week' if per == 'week' else 'today'}: "
                       + (", ".join(names[:6]) + f" and {len(names) - 6} more" if len(names) > 6
                          else _list(names)) + ".")
    far = max(live, key=lambda i: math.hypot(i["x"] - 100, i["y"] - 100))
    say.append(f"Furthest from the benchmark: {far['label']}, {far['q']} "
               f"(trend {far['x']:.1f}, momentum {far['y']:.1f}).")
    return say


def build(universe: str = "book", bench: Optional[str] = None, freq: str = "w") -> dict:
    from . import bonds
    from .morning import brands
    if universe not in UNIVERSES:
        raise ValueError(f"universe must be one of {sorted(UNIVERSES)}")
    freq = "d" if freq == "d" else "w"
    bench = bench if bench in BENCH else DEFAULT_BENCH[universe]
    if bench == "BOOK" and universe != "book":
        bench = DEFAULT_BENCH[universe]

    book = bonds._book() if universe == "book" or bench == "BOOK" else {"rows": []}
    if universe == "book":
        rows = [{"t": r["t"], "name": r["name"], "ccy": r["ccy"], "weight": r["w"] * 100,
                 "sector": r.get("sector") or "Unclassified",
                 "label": (brands(r["t"], r["name"] or "") or [r["t"].split(".")[0]])[0]}
                for r in book["rows"]]
        if not rows:
            return {"available": False, "why": "No priced holdings.", "universe": universe}
    else:
        rows = [{"t": t, "name": n, "label": n, "ccy": "USD", "weight": None, "sector": None}
                for t, n in (SECTORS if universe == "sectors" else MARKETS)]

    bsym = BENCH[bench][0]
    px = prices([r["t"] for r in rows] + [r["t"] for r in book["rows"]] + ([bsym] if bsym else []))
    days = sorted(px.get("SPY") or {})
    ends = periods(days, freq)
    in_eur = universe == "book"

    def series(t: str, ccy: str) -> list:
        m = px.get(t) or {}
        return _on(bonds._to_eur(m, ccy, px) if in_eur else m, ends)

    if bsym:
        b = series(bsym, "USD")
    else:
        # Today's holdings, held all along: each one's price rebased to today
        # and weighted by today's share of the book.
        parts = []
        for r in book["rows"]:
            s = series(r["t"], r["ccy"])
            if s[-1]:
                parts.append((r["w"], [None if v is None else v / s[-1] for v in s]))
        b = []
        for t in range(len(ends)):
            num = sum(w * s[t] for w, s in parts if s[t] is not None)
            den = sum(w for w, s in parts if s[t] is not None)
            b.append(num / den if den >= 0.8 else None)

    items, full, prices_all = [], [], []
    for r in rows:
        if r["t"] == bsym:
            continue
        p = series(r["t"], r["ccy"])
        x, y = rotate(p, b)
        full.append((x, y))
        prices_all.append(p)
        pts = [None if (a is None or c is None) else [round(a, 2), round(c, 2)] for a, c in zip(x, y)][-KEEP:]
        it = {"t": r["t"], "label": r["label"], "name": r["name"], "sector": r["sector"],
              "weight": round(r["weight"], 2) if r["weight"] is not None else None, "pts": pts}
        if pts and pts[-1]:
            it["x"], it["y"] = pts[-1]
            it["q"] = quadrant(*pts[-1])
            since = 0
            for q in reversed(pts):
                if not q or quadrant(*q) != it["q"]:
                    break
                since += 1
            it["since"] = since
            it["was"] = quadrant(*pts[-2]) if len(pts) > 1 and pts[-2] else None
            if len(pts) > 1 and pts[-2]:
                it["dx"], it["dy"] = round(pts[-1][0] - pts[-2][0], 2), round(pts[-1][1] - pts[-2][1], 2)
            for k, key in ((4 if freq == "w" else 21, "rel1"), (13 if freq == "w" else 63, "rel3")):
                if len(p) > k and p[-1] and p[-1 - k] and b[-1] and b[-1 - k]:
                    it[key] = round(((p[-1] / p[-1 - k]) / (b[-1] / b[-1 - k]) - 1) * 100, 1)
        else:
            it["why"] = "not enough history"
        if it.get("q"):
            it["motion"] = motion(pts)
            it["hist"] = history(pts)
            it["read"] = reading(it, "week" if freq == "w" else "day")
        items.append(it)

    per = "week" if freq == "w" else "day"
    return {"available": True, "universe": universe, "universes": UNIVERSES,
            "bench": bench, "bench_label": BENCH[bench][1],
            "benches": {k: v[1] for k, v in BENCH.items() if universe == "book" or k != "BOOK"},
            "freq": freq, "per": per, "dates": ends[-KEEP:], "asof": days[-1] if days else None,
            "running": bool(freq == "w" and ends and date.fromisoformat(ends[-1]).weekday() < 4),
            "items": items, "say": _say(items, universe, per),
            "followed": followed(full, prices_all, [b] * len(full), (4, 13) if freq == "w" else (21, 63)),
            "horizons": [4, 13] if freq == "w" else [21, 63], "span": [ends[0], ends[-1]] if ends else None,
            "params": {"n": N, "usual": USUAL, "smooth": SMOOTH},
            "currency": "EUR" if in_eur else "USD", "fetched_at": time.time()}
