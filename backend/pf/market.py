"""
MKT — the market snapshot.

A board of about thirty rows, each carrying rather more than a level and a
day change, because a level alone answers almost nothing. Is gold at 3,500
high? Is a VIX of 14 calm or complacent? You cannot tell without the
distribution, so every row gets:

    1D / 1W / 1M / YTD     four horizons, because "down today" and "up 22%
                           this year" are both true and the second matters more
    52-week range          where today sits between the year's low and high
    sparkline              thirty days of shape
    percentile             for VIX and credit spreads, where the level means
                           nothing without knowing what is normal

And three things a price screen usually leaves out:

    SPREADS as their own rows. 10y-2y is the recession gauge, 10y-Bund drives
    EUR/USD, BTP-Bund is European stress. Each is more informative than
    either leg, and nobody shows them by default.

    SESSIONS. Whether Tokyo is open changes what a Nikkei "day change" even
    means. A price from a closed market is a close, not a quote, and the page
    should not present the two identically.

    A RISK READ across equities, credit, volatility, gold and the dollar —
    with every input shown, because a single risk-on/risk-off score with
    hidden workings is astrology.
"""

from __future__ import annotations

import bisect
import time
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from . import pricing, store

# --------------------------------------------------------------------------
# the board
# --------------------------------------------------------------------------
#
# src:  yahoo  — a quote symbol, streamable where Yahoo pushes it
#       fred   — a daily FRED series, published after the close
#       ecb    — a daily ECB series
#       calc   — derived from two other rows
#
# unit: idx | pct (a yield, already in percent) | fx | usd | eur | bp

BOARD = [
    # ---- indices ---------------------------------------------------------
    {"g": "Indices", "k": "AEX",    "n": "AEX",            "src": "yahoo", "s": "^AEX",       "u": "idx", "tz": "Europe/Amsterdam"},
    {"g": "Indices", "k": "AMX",    "n": "AMX (midkap)",   "src": "yahoo", "s": "^AMX",       "u": "idx", "tz": "Europe/Amsterdam"},
    {"g": "Indices", "k": "SX5E",   "n": "Euro Stoxx 50",  "src": "yahoo", "s": "^STOXX50E",  "u": "idx", "tz": "Europe/Berlin"},
    {"g": "Indices", "k": "DAX",    "n": "DAX",            "src": "yahoo", "s": "^GDAXI",     "u": "idx", "tz": "Europe/Berlin"},
    {"g": "Indices", "k": "SPX",    "n": "S&P 500",        "src": "yahoo", "s": "^GSPC",      "u": "idx", "tz": "America/New_York"},
    {"g": "Indices", "k": "NDX",    "n": "Nasdaq 100",     "src": "yahoo", "s": "^NDX",       "u": "idx", "tz": "America/New_York"},
    {"g": "Indices", "k": "N225",   "n": "Nikkei 225",     "src": "yahoo", "s": "^N225",      "u": "idx", "tz": "Asia/Tokyo"},
    {"g": "Indices", "k": "HSI",    "n": "Hang Seng",      "src": "yahoo", "s": "^HSI",       "u": "idx", "tz": "Asia/Hong_Kong"},

    # ---- rates and the curve --------------------------------------------
    {"g": "Rates", "k": "US2Y",   "n": "US 2-year",         "src": "fred", "s": "DGS2",   "u": "pct"},
    {"g": "Rates", "k": "US10Y",  "n": "US 10-year",        "src": "fred", "s": "DGS10",  "u": "pct"},
    {"g": "Rates", "k": "US30Y",  "n": "US 30-year",        "src": "fred", "s": "DGS30",  "u": "pct"},
    {"g": "Rates", "k": "US10BE", "n": "US 10y breakeven",  "src": "fred", "s": "T10YIE", "u": "pct",
     "note": "the inflation the bond market prices for ten years"},
    {"g": "Rates", "k": "US10_2", "n": "US 10y − 2y",       "src": "fred", "s": "T10Y2Y", "u": "pct",
     "note": "below zero has preceded every recent US recession"},
    {"g": "Rates", "k": "EA10Y",  "n": "Euro-area AAA 10y", "src": "ecb",  "s": "EA_10Y", "u": "pct",
     "note": "the Bund, in effect"},
    {"g": "Rates", "k": "US_EA",  "n": "US 10y − euro 10y", "src": "calc", "s": ("US10Y", "EA10Y"), "u": "pct",
     "note": "the rate gap that pulls EUR/USD around"},
    {"g": "Rates", "k": "DFR",    "n": "ECB deposit rate",  "src": "ecb",  "s": "ECB_DFR", "u": "pct"},
    {"g": "Rates", "k": "FF",     "n": "Fed funds (eff.)",  "src": "fred", "s": "DFF",    "u": "pct"},

    # ---- FX --------------------------------------------------------------
    {"g": "FX", "k": "EURUSD", "n": "EUR/USD", "src": "yahoo", "s": "EURUSD=X",  "u": "fx"},
    {"g": "FX", "k": "EURGBP", "n": "EUR/GBP", "src": "yahoo", "s": "EURGBP=X",  "u": "fx"},
    {"g": "FX", "k": "DXY",    "n": "Dollar index", "src": "yahoo", "s": "DX-Y.NYB", "u": "idx"},

    # ---- commodities -----------------------------------------------------
    {"g": "Commodities", "k": "BRENT",  "n": "Brent crude",  "src": "yahoo", "s": "BZ=F",  "u": "usd"},
    {"g": "Commodities", "k": "TTF",    "n": "TTF gas",      "src": "yahoo", "s": "TTF=F", "u": "eur",
     "note": "European industrial input cost"},
    {"g": "Commodities", "k": "GOLD",   "n": "Gold",         "src": "yahoo", "s": "GC=F",  "u": "usd"},
    {"g": "Commodities", "k": "COPPER", "n": "Copper",       "src": "yahoo", "s": "HG=F",  "u": "usd",
     "note": "the cyclical bellwether"},

    # ---- volatility and credit ------------------------------------------
    {"g": "Volatility & credit", "k": "VIX",   "n": "VIX",          "src": "yahoo", "s": "^VIX",    "u": "idx", "pctile": True},
    # Yahoo dropped V2TX.DE (no prices, no quote) by November 2026; STOXX publishes the index itself, daily since 1999.
    {"g": "Volatility & credit", "k": "V2TX",  "n": "VSTOXX",       "src": "stoxx", "s": "V2TX",    "u": "idx", "pctile": True},
    {"g": "Volatility & credit", "k": "MOVE",  "n": "MOVE (bond vol)", "src": "yahoo", "s": "^MOVE", "u": "idx", "pctile": True,
     "note": "the VIX of the Treasury market"},
    {"g": "Volatility & credit", "k": "USHY",  "n": "US high yield OAS",  "src": "fred", "s": "BAMLH0A0HYM2", "u": "pct", "pctile": True},
    {"g": "Volatility & credit", "k": "USIG",  "n": "US investment grade OAS", "src": "fred", "s": "BAMLC0A0CM", "u": "pct", "pctile": True},
    {"g": "Volatility & credit", "k": "EUHY",  "n": "Euro high yield OAS", "src": "fred", "s": "BAMLHE00EHYIOAS", "u": "pct", "pctile": True},
]

YAHOO_SYMBOLS = [r["s"] for r in BOARD if r["src"] == "yahoo"]
BY_KEY = {r["k"]: r for r in BOARD}


# --------------------------------------------------------------------------
# per-row statistics — pure, so they can be tested exactly
# --------------------------------------------------------------------------

def _on_or_before(hist: dict[str, float], day: str) -> Optional[float]:
    ks = [k for k in hist if k <= day]
    return hist[max(ks)] if ks else None


def row_stats(hist: dict[str, float], last: Optional[float] = None,
              today: Optional[date] = None, want_pctile: bool = False,
              last_d: Optional[str] = None) -> dict:
    """
    Horizons, range and percentile from a daily series.

    `last` overrides the newest cached close — that is how a streamed price
    gets folded in without refetching anything — and `last_d` is the session
    date that price belongs to (today when not given).

    1D is measured from the last stored close BEFORE that date. The old rule,
    "yesterday is the newest stored close", dates from when the history never
    held the session in progress. Since build 33 it does (pricing.history_due
    refetches it every quarter of an hour), so a streamed price was measured
    against a bar at most fifteen minutes old and every live row read about
    0.0% all session; at a weekend the whole board read 0.0% and the risk
    read "mixed". Now a finished session's move stays on the board until the
    next one starts. A quote dated before the newest stored day is ignored.

    Changes are in PERCENT for prices and in BASIS POINTS for anything
    already quoted as a rate. A 10-year yield going 4.00 -> 4.10 is +10bp;
    calling it "+2.5%" is technically true and useless.
    """
    today = today or date.today()
    if not hist:
        # NOT "n": the row dict this is merged into already uses that for the
        # instrument's display name, and a silent overwrite turned every name
        # into an observation count.
        return {"last": last, "stale": True, "obs": 0}

    days = sorted(hist)
    if last is not None:
        last_d = last_d or today.isoformat()
        if last_d < days[-1]:
            last = None                     # the stored history is newer than the quote
    if last is None:
        cur, asof = hist[days[-1]], days[-1]
    else:
        cur, asof = last, last_d

    def chg(then: Optional[float]):
        if then in (None, 0):
            return None, None
        return (cur - then), (cur / then - 1) * 100

    i = bisect.bisect_left(days, asof)      # the first stored day on or after the price's own
    prev = hist[days[i - 1]] if i >= 1 else None

    d_abs, d_pct = chg(prev)
    w_abs, w_pct = chg(_on_or_before(hist, (today - timedelta(days=7)).isoformat()))
    m_abs, m_pct = chg(_on_or_before(hist, (today - timedelta(days=30)).isoformat()))
    y_abs, y_pct = chg(_on_or_before(hist, f"{today.year - 1}-12-31"))

    window = [v for d, v in hist.items() if d >= (today - timedelta(days=365)).isoformat()]
    lo, hi = (min(window), max(window)) if window else (None, None)
    pos = ((cur - lo) / (hi - lo) * 100) if (lo is not None and hi is not None and hi > lo) else None

    pctile = None
    if want_pctile and len(window) > 30:
        pctile = round(sum(1 for v in window if v <= cur) / len(window) * 100)

    # Thirty points of shape, evenly sampled so a gappy series still draws, counted back from
    # the newest close so the line ends on it (sampled from the front, it often stopped a day short).
    tail = [hist[d] for d in days[-60:]]
    step = max(1, len(tail) // 30)
    spark = tail[::-1][::step][::-1][-30:]
    if last is not None and spark:          # a live price: today's point, or the session so far
        spark = (spark + [cur])[-30:] if asof > days[-1] else spark[:-1] + [cur]

    return {
        "last": round(cur, 4) if cur is not None else None,
        "prev": round(prev, 4) if prev is not None else None,
        "asof": asof,
        "d_abs": round(d_abs, 4) if d_abs is not None else None,
        "d_pct": round(d_pct, 2) if d_pct is not None else None,
        "w_pct": round(w_pct, 2) if w_pct is not None else None,
        "m_pct": round(m_pct, 2) if m_pct is not None else None,
        "y_pct": round(y_pct, 2) if y_pct is not None else None,
        "w_abs": round(w_abs, 4) if w_abs is not None else None,
        "m_abs": round(m_abs, 4) if m_abs is not None else None,
        "y_abs": round(y_abs, 4) if y_abs is not None else None,
        "lo52": round(lo, 4) if lo is not None else None,
        "hi52": round(hi, 4) if hi is not None else None,
        "range_pos": round(pos, 1) if pos is not None else None,
        "pctile": pctile,
        "spark": [round(v, 4) for v in spark],
        "obs": len(hist),
        "stale": False,
    }


# --------------------------------------------------------------------------
# sessions
# --------------------------------------------------------------------------

SESSIONS = [
    {"k": "TYO", "n": "Tokyo",     "tz": "Asia/Tokyo",      "open": (9, 0),  "close": (15, 30)},
    {"k": "HKG", "n": "Hong Kong", "tz": "Asia/Hong_Kong",  "open": (9, 30), "close": (16, 0)},
    {"k": "AMS", "n": "Amsterdam", "tz": "Europe/Amsterdam", "open": (9, 0), "close": (17, 40)},
    {"k": "FRA", "n": "Frankfurt", "tz": "Europe/Berlin",   "open": (9, 0),  "close": (17, 30)},
    {"k": "LON", "n": "London",    "tz": "Europe/London",   "open": (8, 0),  "close": (16, 30)},
    {"k": "NYC", "n": "New York",  "tz": "America/New_York", "open": (9, 30), "close": (16, 0)},
]


def sessions(now: Optional[datetime] = None) -> list[dict]:
    """
    Which exchanges are trading, and the next bell.

    Public holidays are NOT modelled. Doing it properly means a maintained
    calendar per exchange; doing it badly means confidently saying a market
    is open on Christmas Day. So the page says "regular hours" and leaves the
    judgement visible rather than pretending to a precision it does not have.
    """
    out = []
    for s in SESSIONS:
        tz = ZoneInfo(s["tz"])
        t = (now or datetime.now(tz)).astimezone(tz)
        o = t.replace(hour=s["open"][0], minute=s["open"][1], second=0, microsecond=0)
        c = t.replace(hour=s["close"][0], minute=s["close"][1], second=0, microsecond=0)
        weekday = t.weekday() < 5
        is_open = weekday and o <= t < c

        if is_open:
            nxt, label = c, "closes"
        else:
            nxt = o if (weekday and t < o) else (o + timedelta(days=1))
            while nxt.weekday() >= 5:
                nxt += timedelta(days=1)
            label = "opens"
        mins = max(0, int((nxt - t).total_seconds() // 60))
        out.append({
            "k": s["k"], "n": s["n"], "open": is_open,
            "local": t.strftime("%H:%M"),
            "next": label,
            "in_min": mins,
            "in_txt": f"{mins // 60}h {mins % 60:02d}m" if mins >= 60 else f"{mins}m",
        })
    return out


# The three exchanges this book trades on, for the top bar on every page:
# holidays and early closes from the brief's own calendar, New York's pre-
# market and after hours as Yahoo trades them.
# The bar shows Amsterdam and New York. Frankfurt keeps Amsterdam's hours, so
# it was left out on 15 October 2026 at the user's request.
HOURS = [("AMS", "Amsterdam", "Europe/Amsterdam", (9, 0), (17, 30), "NL"),
         ("NYC", "New York", "America/New_York", (9, 30), (16, 0), "US")]
NY_EXT = ((4, 0), (20, 0))


def _early(mk: str, iso: str) -> Optional[tuple]:
    from .morning import EARLY_CLOSE
    txt = (EARLY_CLOSE.get(mk) or {}).get(iso)
    if not txt:
        return None
    hh, mm = txt.split(" ")[0].split(":")
    return int(hh), int(mm)


def _trading_day(mk: str, d) -> bool:
    from .morning import HOLIDAYS
    return d.weekday() < 5 and d.isoformat() not in (HOLIDAYS.get(mk) or {})


def hours(now: Optional[datetime] = None) -> dict:
    """
    Each exchange's state now and when it next changes, as unix times so the
    page can count down by itself. New York also says pre-market and after
    hours (04:00–09:30 and 16:00–20:00 New York time).
    """
    from .morning import HOLIDAYS, HOLIDAYS_AS_OF
    out = []
    for k, name, tzname, o, c, mk in HOURS:
        tz = ZoneInfo(tzname)
        t = (now or datetime.now(tz)).astimezone(tz)

        def at(d, hm):
            return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=tz)

        today = t.date()
        close = _early(mk, today.isoformat()) or c
        state, nxt, label = "shut", None, "opens"
        holiday = (HOLIDAYS.get(mk) or {}).get(today.isoformat())
        if _trading_day(mk, today) and at(today, o) <= t < at(today, close):
            state, nxt, label = "open", at(today, close), "closes"
        else:
            if k == "NYC" and _trading_day(mk, today):
                if at(today, NY_EXT[0]) <= t < at(today, o):
                    state = "pre"
                elif at(today, close) <= t < at(today, NY_EXT[1]):
                    state = "post"
            d = today if (_trading_day(mk, today) and t < at(today, o)) else today + timedelta(days=1)
            while not _trading_day(mk, d):
                d += timedelta(days=1)
            nxt = at(d, o)
        out.append({"k": k, "n": name, "state": state, "next": label, "at": nxt.timestamp(),
                    "local": t.strftime("%H:%M"), "holiday": holiday,
                    "early": f"{close[0]:02d}:{close[1]:02d}" if close != c else None})
    return {"exchanges": out, "now": (now or datetime.now()).timestamp(), "holidays_as_of": HOLIDAYS_AS_OF}


# --------------------------------------------------------------------------
# the risk read
# --------------------------------------------------------------------------

#
# Thirteen signals in five groups. Each move is measured against that
# instrument's own normal day (the standard deviation of its daily moves over
# the last year), so a 3% day in the VIX, which is an ordinary Tuesday, no
# longer counts the same as a 3% day in the S&P 500. Each group is the mean of
# its members, and the verdict the mean of the groups, so four equity indices
# cannot outvote credit on their own. The first version (build 11) reduced
# five raw moves to -1/0/+1 at a 0.05% threshold: noise in gold or the dollar
# swung it.
#
# key, label, group, +1 when a rise is risk-on, how the move is measured, a typical day (used until a year of history is in)
READ_SIGNALS = [
    ("SPX", "S&P 500", "Equities", +1, "pct", 1.0),
    ("NDX", "Nasdaq 100", "Equities", +1, "pct", 1.3),
    ("SX5E", "Euro Stoxx 50", "Equities", +1, "pct", 1.0),
    ("AEX", "AEX", "Equities", +1, "pct", 0.9),
    ("VIX", "VIX", "Volatility", -1, "pct", 6.0),
    ("V2TX", "VSTOXX", "Volatility", -1, "pct", 5.0),
    ("MOVE", "MOVE", "Volatility", -1, "pct", 3.5),
    ("USHY", "US high yield", "Credit", -1, "bp", 5.0),
    ("USIG", "US investment grade", "Credit", -1, "bp", 1.5),
    ("EUHY", "Euro high yield", "Credit", -1, "bp", 5.0),
    ("GOLD", "Gold", "Havens", -1, "pct", 1.0),
    ("DXY", "Dollar index", "Havens", -1, "pct", 0.4),
    ("COPPER", "Copper", "Growth", +1, "pct", 1.4),
]
READ_GROUPS = ["Equities", "Volatility", "Credit", "Havens", "Growth"]
GROUP_NOTE = {"Equities": "the indices themselves", "Volatility": "what protection costs; a rise is fear",
              "Credit": "what risky borrowers pay over Treasuries; widening is stress",
              "Havens": "where money hides; a bid is caution", "Growth": "the cyclical bellwether"}
STRESS_KEYS = ["VIX", "V2TX", "MOVE", "USHY", "USIG", "EUHY"]
HORIZONS = (("1D", "d", 1), ("1W", "w", 5), ("1M", "m", 21))
CALL_AT, FIRM_AT = 0.35, 1.0         # the composite, in normal days, at which the read commits, and commits firmly


def _move(r: dict, unit: str, h: str = "d") -> Optional[float]:
    """A row's move over a horizon: percent for prices, basis points for spreads and yields."""
    if not r:
        return None
    if unit == "bp":
        v = r.get(f"{h}_abs")
        return None if v is None else v * 100
    return r.get(f"{h}_pct")


def daily_moves(hist: dict, unit: str) -> dict:
    """{date: that day's move} from a daily series, in the signal's own unit."""
    days = sorted(hist)
    out = {}
    for a, b in zip(days, days[1:]):
        x, y = hist[a], hist[b]
        if x is None or y is None:
            continue
        if unit == "bp":
            out[b] = (y - x) * 100
        elif x:
            out[b] = (y / x - 1) * 100
    return out


def normal_day(hist: Optional[dict], unit: str, fallback: float, n: int = 260) -> float:
    """The standard deviation of a year of daily moves; the typical day until there are sixty of them."""
    mv = list(daily_moves(hist or {}, unit).values())[-n:]
    if len(mv) < 60:
        return fallback
    m = sum(mv) / len(mv)
    sd = (sum((v - m) ** 2 for v in mv) / (len(mv) - 1)) ** 0.5
    return sd if sd > 0 else fallback


def _verdict(c: Optional[float]) -> tuple[str, str]:
    if c is None:
        return "no read", "dim"
    if c >= CALL_AT:
        return "risk-on", "pos"
    if c <= -CALL_AT:
        return "risk-off", "neg"
    return "mixed", "warnc"


def _strength(c: Optional[float]) -> str:
    if c is None:
        return ""
    a = abs(c)
    return "firmly" if a >= FIRM_AT * 1.6 else "clearly" if a >= FIRM_AT else "leaning" if a >= CALL_AT else "no clear lean"


def composite(zs: dict) -> tuple[Optional[float], dict]:
    """{key: z in the risk-on direction} -> (the mean of the group means, {group: mean})."""
    groups: dict = {}
    for key, _, grp, *_ in READ_SIGNALS:
        if zs.get(key) is not None:
            groups.setdefault(grp, []).append(max(-3.0, min(3.0, zs[key])))
    gm = {g: sum(v) / len(v) for g, v in groups.items()}
    return (sum(gm.values()) / len(gm) if gm else None), gm


def curve_shape(d2: Optional[float], d10: Optional[float], small: float = 2.0) -> Optional[dict]:
    """How the Treasury curve moved, from the 2-year and 10-year changes in basis points."""
    if d2 is None or d10 is None:
        return None
    if abs(d2) < small and abs(d10) < small:
        return {"kind": "little changed", "gloss": "", "d2": round(d2), "d10": round(d10)}
    bull = (d2 + d10) / 2 < 0
    steep = d10 > d2
    kind = ("bull" if bull else "bear") + (" steepener" if steep else " flattener")
    gloss = {"bull steepener": "short rates falling faster: the market pricing cuts",
             "bull flattener": "long yields falling faster: a growth scare or a rush into duration",
             "bear steepener": "long yields rising faster: term premium, supply or inflation worries",
             "bear flattener": "short rates rising faster: the market pricing a tighter Fed"}[kind]
    return {"kind": kind, "gloss": gloss, "d2": round(d2), "d10": round(d10)}


def _stress_label(p: Optional[float]) -> str:
    if p is None:
        return ""
    return "calm" if p < 30 else "normal" if p < 60 else "elevated" if p < 85 else "stressed"


def history_read(hists: dict, sds: dict, days: int = 60) -> list[dict]:
    """The composite on each of the last sessions of the S&P 500, from the stored closes."""
    moves = {key: daily_moves(hists.get(key) or {}, unit) for key, _, _, _, unit, _ in READ_SIGNALS}
    out = []
    for d in sorted(hists.get("SPX") or {})[-days:]:
        zs = {}
        for key, _, _, sign, _, _ in READ_SIGNALS:
            v = moves[key].get(d)
            if v is not None and sds.get(key):
                zs[key] = sign * v / sds[key]
        c, _ = composite(zs)
        if c is not None:
            out.append({"d": d, "c": round(c, 2)})
    return out


def _f(v: Optional[float], d: int = 1, plus: bool = True) -> str:
    if v is None:
        return "–"
    return f"{v:+.{d}f}" if plus else f"{v:.{d}f}"


def _bp(v: Optional[float]) -> str:
    return "–" if v is None else f"{v:+.0f}bp"


def risk_read(rows: dict[str, dict], hists: Optional[dict] = None,
              today: Optional[date] = None) -> dict:
    """
    The read: is the market taking risk or shedding it, how hard, over which
    horizon, with what stress underneath, and where the parts disagree.
    Rules, not a model, and every input is on the page: the useful part is
    seeing them disagree, which is exactly when one number would mislead.

    `rows` are the board's rows by key (live prices folded in), `hists` their
    daily closes for each instrument's normal day and the sixty-session strip.
    """
    hists = hists or {}
    sds = {key: normal_day(hists.get(key), unit, fb) for key, _, _, _, unit, fb in READ_SIGNALS}
    # The session the read is about: the newest day any of the equity rows is dated. FRED publishes
    # spreads a day late, so on most days credit's latest move belongs to the session before: it is
    # shown, dated, and kept out of the day's score (the week and the month take it in).
    session = max((rows[k]["asof"] for k in ("SPX", "NDX", "SX5E", "AEX") if (rows.get(k) or {}).get("asof")),
                  default=None)

    # each horizon: every signal's move, in normal days for that horizon
    hz, today_z, sig = [], {}, []
    for label, h, n in HORIZONS:
        zs = {}
        for key, name, grp, sign, unit, _ in READ_SIGNALS:
            v = _move(rows.get(key) or {}, unit, h)
            if v is None:
                continue
            z = sign * v / (sds[key] * n ** 0.5)
            r = rows[key]
            lag = bool(session and r.get("asof") and r["asof"] < session)
            if h == "d":
                sig.append({"key": key, "label": name, "group": grp, "move": round(v, 2), "unit": unit,
                            "z": round(z, 2), "dir": 0 if abs(z) < 0.5 else (1 if z > 0 else -1),
                            "raw_z": round(v / sds[key], 2), "sd": round(sds[key], 2),
                            "level": r.get("last"), "u": r.get("u"), "pctile": r.get("pctile"),
                            "asof": r.get("asof"), "src": r.get("src"), "lag": lag})
                if lag:
                    continue
            zs[key] = z
        c, gm = composite(zs)
        v, tone = _verdict(c)
        hz.append({"h": label, "composite": round(c, 2) if c is not None else None, "verdict": v, "tone": tone,
                   "groups": {g: round(x, 2) for g, x in gm.items()}})
        if h == "d":
            today_z = zs
    day = hz[0]
    c = day["composite"]
    gm = day["groups"]
    groups = [{"group": g, "z": gm.get(g), "note": GROUP_NOTE[g],
               "dir": None if gm.get(g) is None else 0 if abs(gm[g]) < 0.25 else (1 if gm[g] > 0 else -1),
               "signals": [s for s in sig if s["group"] == g]}
              for g in READ_GROUPS if any(s["group"] == g for s in sig)]
    scored = [g for g in groups if g["dir"] is not None]
    agree = sum(1 for g in scored if g["dir"] == (1 if c > 0 else -1)) if c else 0
    score = sum(g["dir"] for g in scored)
    verdict, tone = _verdict(c)

    # stress: where the fear gauges stand in their own year, whatever they did today
    st = [{"key": k, "label": BY_KEY[k]["n"], "pctile": rows[k]["pctile"], "level": rows[k].get("last"),
           "u": rows[k].get("u")} for k in STRESS_KEYS if (rows.get(k) or {}).get("pctile") is not None]
    sp = round(sum(x["pctile"] for x in st) / len(st)) if st else None
    stress = {"pctile": sp, "label": _stress_label(sp), "parts": st}

    # rates: the curve's shape, real yields, the transatlantic gap
    R = lambda k, h="d": _move(rows.get(k) or {}, "bp", h)
    rates = {"d10": R("US10Y"), "d2": R("US2Y"), "w10": R("US10Y", "w"), "w2": R("US2Y", "w"),
             "be": R("US10BE"), "gap": R("US_EA"), "gap_w": R("US_EA", "w"), "bund": R("EA10Y"),
             "us10": (rows.get("US10Y") or {}).get("last"), "us2": (rows.get("US2Y") or {}).get("last"),
             "asof": (rows.get("US10Y") or {}).get("asof"),
             "day": curve_shape(R("US2Y"), R("US10Y")), "week": curve_shape(R("US2Y", "w"), R("US10Y", "w"), 4)}
    same_day = (rows.get("US10Y") or {}).get("asof") == (rows.get("US10BE") or {}).get("asof")
    rates["real"] = (rates["d10"] - rates["be"]) if same_day and rates["d10"] is not None and rates["be"] is not None else None
    rates = {k: (round(v, 1) if isinstance(v, float) else v) for k, v in rates.items()}

    # leadership: who led whom, in percentage points
    P = lambda k, h="d": (rows.get(k) or {}).get(f"{h}_pct")

    def spread(a, b, h):
        x, y = P(a, h), P(b, h)
        return round(x - y, 2) if x is not None and y is not None else None
    lead = [{"key": k, "label": lab, "a": a, "b": b, "d": spread(a, b, "d"), "w": spread(a, b, "w"), "m": spread(a, b, "m"),
             "up": up, "down": down}
            for k, lab, a, b, up, down in (
                ("tech", "Nasdaq 100 vs S&P 500", "NDX", "SPX", "tech led", "tech lagged"),
                ("europe", "Euro Stoxx 50 vs S&P 500", "SX5E", "SPX", "Europe led", "the US led"),
                ("mid", "AMX vs AEX", "AMX", "AEX", "Dutch midcaps led", "Dutch large caps led"),
                ("cu_au", "Copper vs gold", "COPPER", "GOLD", "growth over fear", "fear over growth"))]

    # where the parts disagree
    rz = lambda k: (today_z.get(k) or 0.0) * dict((s[0], s[3]) for s in READ_SIGNALS)[k]   # the raw move, in normal days
    eq_z, cr_z = gm.get("Equities"), gm.get("Credit")
    flags = []
    if eq_z is not None and cr_z is not None:
        if eq_z >= 0.75 and cr_z <= -0.5:
            flags.append("Stocks rose while credit spreads widened: the bond market is not confirming the rally.")
        elif eq_z <= -0.75 and cr_z >= 0.5:
            flags.append("Stocks fell but credit spreads tightened: a selloff the credit market is not joining.")
    if eq_z is not None and "VIX" in today_z:
        if eq_z >= 0.75 and rz("VIX") >= 0.75:
            flags.append("Stocks and the VIX rose together: investors paid up for protection into the rally.")
        elif eq_z <= -0.75 and rz("VIX") <= 0:
            flags.append("Stocks fell without the VIX rising: an orderly selloff, not a panic.")
    if "GOLD" in today_z and eq_z is not None and rz("GOLD") >= 0.75 and eq_z >= 0.75:
        flags.append("Gold rose with stocks: more a liquidity or debasement bid than fear.")
    if "GOLD" in today_z and "DXY" in today_z and rz("GOLD") >= 0.75 and rz("DXY") >= 0.75:
        flags.append("Gold and the dollar were both bid: a flight to safety.")
    if rates["d10"] is not None and eq_z is not None and rates["d10"] >= 6 and eq_z <= -0.5:
        flags.append(f"The US 10-year rose {rates['d10']:.0f}bp as stocks fell: rates are doing the damage.")
    for x in lead:
        if x["key"] == "europe" and x["d"] is not None and abs(x["d"]) >= 1.0:
            flags.append(f"Europe and the US parted by {abs(x['d']):.1f} points: {x['up'] if x['d'] > 0 else x['down']}.")
    if hz[2]["composite"] is not None and c is not None and abs(c) >= CALL_AT and abs(hz[2]["composite"]) >= CALL_AT \
            and (c > 0) != (hz[2]["composite"] > 0):
        flags.append(f"Today runs against the month: a {'bounce inside a weak' if c > 0 else 'pullback inside a strong'} month.")

    hist = history_read(hists, sds)
    last20 = hist[-20:]
    run = {"on": sum(1 for x in last20 if x["c"] >= CALL_AT), "off": sum(1 for x in last20 if x["c"] <= -CALL_AT),
           "n": len(last20)}

    out = {"verdict": verdict, "tone": tone, "strength": _strength(c), "composite": c,
           "score": score, "of": len(scored), "agree": agree, "signals": sig, "groups": groups,
           "horizons": hz, "stress": stress, "rates": rates, "leaders": lead, "flags": flags,
           "history": hist, "run": run, "session": session,
           "note": f"{len(sig)} signals in {len(groups)} groups, each against its own normal day · rules, not a model"}
    out["story"] = read_story(out, rows)
    return out


def read_story(R: dict, rows: dict) -> list[str]:
    """The read in sentences, every figure taken from the rows beside it."""
    S = []
    lv = lambda k: (rows.get(k) or {}).get("last")
    m = lambda k: (rows.get(k) or {}).get("d_pct")
    c = R["composite"]
    if c is None:
        return ["Not enough of the board has prices yet for a read."]
    g = {x["group"]: x for x in R["groups"]}
    with_ = [x["group"].lower() for x in R["groups"] if x["dir"] == (1 if c > 0 else -1)]
    against = [x["group"].lower() for x in R["groups"] if x["dir"] == (-1 if c > 0 else 1)]
    if R["verdict"] == "mixed":
        lean = "risk-on" if c > 0 else "risk-off"
        S.append("Mixed: " + (f"{', '.join(with_) or 'nothing'} leaned {lean} and {', '.join(against)} the other way."
                              if against else f"only {', '.join(with_)} leaned {lean}, and not far." if with_
                              else "no group moved far from an ordinary day."))
    else:
        S.append(f"{R['verdict'].capitalize()}, {R['strength']}: {R['agree']} of {R['of']} groups agree"
                 + (f"; {', '.join(against)} did not" if against else "") + ".")
    eq = [(lab, m(k)) for k, lab in (("SPX", "the S&P 500"), ("NDX", "the Nasdaq 100"), ("SX5E", "the Euro Stoxx 50"),
                                      ("AEX", "the AEX")) if m(k) is not None]
    if eq:
        S.append("Stocks: " + ", ".join(f"{lab} {v:+.1f}%" for lab, v in eq) + ".")
    v = [s for s in (g.get("Volatility") or {}).get("signals", [])]
    vix = next((s for s in v if s["key"] == "VIX"), None)
    if vix and vix["level"] is not None:
        p = vix.get("pctile")
        S.append(f"Fear: the VIX {vix['move']:+.1f}% to {vix['level']:.1f}"
                 + (f", the {p}th percentile of its year" if p is not None else "")
                 + (f"; overall stress is {R['stress']['label']} ({R['stress']['pctile']}th percentile across "
                    f"{len(R['stress']['parts'])} gauges)." if R["stress"]["pctile"] is not None else "."))
    hy = next((s for s in (g.get("Credit") or {}).get("signals", []) if s["key"] == "USHY"), None)
    if hy and hy["level"] is not None:
        S.append(f"Credit: US high yield pays {hy['level']:.2f}% over Treasuries ({hy['move']:+.0f}bp"
                 + (f", FRED as of {hy['asof'][8:10]}/{hy['asof'][5:7]}" if hy.get("asof") else "")
                 + (", a session behind, so it is not in today's score" if hy.get("lag") else "") + ").")
    rt = R["rates"]
    if rt.get("d10") is not None and rt.get("us10") is not None:
        sh = rt.get("day") or {}
        S.append(f"Rates: the US 10-year {rt['d10']:+.0f}bp to {rt['us10']:.2f}%, the 2-year {_bp(rt.get('d2'))}"
                 + (f": a {sh['kind']}, {sh['gloss']}" if sh.get("gloss") else "")
                 + (f"; real yields {rt['real']:+.0f}bp" if rt.get("real") is not None else "") + ".")
    eu = m("EURUSD")
    if eu is not None and lv("EURUSD") is not None:
        S.append(f"The euro: EUR/USD {eu:+.2f}% to {lv('EURUSD'):.4f}"
                 + (f", the dollar index {m('DXY'):+.2f}%" if m("DXY") is not None else "")
                 + (". A stronger euro shrinks dollar holdings in euros." if eu > 0.15 else
                    ". A weaker euro lifts dollar holdings in euros." if eu < -0.15 else "."))
    cm = [(lab, k) for k, lab in (("BRENT", "Brent"), ("GOLD", "gold"), ("COPPER", "copper")) if m(k) is not None]
    if cm:
        S.append("Commodities: " + ", ".join(f"{lab} {m(k):+.1f}%" for lab, k in cm) + ".")
    tech = next((x for x in R["leaders"] if x["key"] == "tech"), None)
    if tech and tech["d"] is not None and abs(tech["d"]) >= 0.3:
        S.append(f"Leadership: {tech['up'] if tech['d'] > 0 else tech['down']} by {abs(tech['d']):.1f} points today"
                 + (f" and {tech['w']:+.1f} over the week." if tech.get("w") is not None else "."))
    w, mo = R["horizons"][1], R["horizons"][2]
    if w["composite"] is not None and mo["composite"] is not None:
        S.append(f"Wider view: the week reads {w['verdict']} ({w['composite']:+.2f}), the month {mo['verdict']} "
                 f"({mo['composite']:+.2f}); {R['run']['off']} of the last {R['run']['n']} sessions were risk-off "
                 f"and {R['run']['on']} risk-on.")
    return S


def book_read(rows: dict[str, dict], you: Optional[dict]) -> Optional[dict]:
    """
    What the session implies for this book, in two legs: the S&P 500's move
    times the book's forward beta (the holdings' own betas, each measured
    against the S&P 500 in the share's currency), and the currency leg, the
    dollar share of the book times the euro's move against it. An estimate,
    labelled as one; PORT has the real day change.
    """
    if not you:
        return None
    beta = you.get("beta") if you.get("beta") is not None else you.get("beta_hist")
    spx = (rows.get("SPX") or {}).get("d_pct")
    eu = (rows.get("EURUSD") or {}).get("d_pct")
    usd = you.get("usd_pct")
    if beta is None or spx is None:
        return None
    # A stronger euro shrinks every dollar the book holds, whatever the shares did.
    fx = -(usd or 0) / 100 * eu if eu is not None and usd else None
    price = beta * spx
    return {"beta": round(beta, 2), "spx": spx, "eurusd": eu, "price": round(price, 2),
            "usd_pct": usd, "fx": round(fx, 2) if fx is not None else None,
            "total": round(price + (fx or 0), 2), "tech_pct": you.get("tech_pct")}


# --------------------------------------------------------------------------
# correlation across asset classes
# --------------------------------------------------------------------------

CORR_KEYS = ["SPX", "NDX", "SX5E", "AEX", "EURUSD", "DXY", "BRENT", "GOLD", "VIX", "US10Y"]


def correlations(hists: dict[str, dict], port_returns: Optional[dict] = None,
                 window: int = 90) -> dict:
    """
    A grid small enough to read. Ten asset classes plus your portfolio.

    A 50x50 grid of individual stocks is 1,225 numbers that change no
    decision. Ten asset classes is a picture of whether diversification is
    working — and in a real selloff, of the moment it stops.
    """
    from .risk import series_returns, _mean, _sd

    series: dict[str, dict] = {}
    for k in CORR_KEYS:
        h = hists.get(k)
        if h and len(h) > 30:
            series[k] = series_returns(h)
    if port_returns and len(port_returns) > 30:
        series["YOU"] = port_returns
    if len(series) < 2:
        # Say WHY, rather than returning an empty grid the page then hides.
        # A panel that silently disappears is indistinguishable from one that
        # was never built, which is exactly how this went unnoticed.
        have = {k: len(hists.get(k) or {}) for k in CORR_KEYS}
        return {"keys": [], "matrix": [], "window": window, "n": 0,
                "why": "needs at least two series with 30+ days of history; have "
                       + (", ".join(f"{k}:{v}" for k, v in have.items() if v) or "nothing cached yet"),
                "have": have, "port": bool(port_returns)}

    cutoff = (date.today() - timedelta(days=window)).isoformat()
    series = {k: {d: v for d, v in s.items() if d >= cutoff} for k, s in series.items()}
    keys = [k for k in (CORR_KEYS + ["YOU"]) if k in series and len(series[k]) > 10]

    def corr(a: str, b: str) -> Optional[float]:
        days = sorted(set(series[a]) & set(series[b]))
        if len(days) < 15:
            return None
        x = [series[a][d] for d in days]
        y = [series[b][d] for d in days]
        mx, my = _mean(x), _mean(y)
        sx, sy = _sd(x), _sd(y)
        if not sx or not sy:
            return None
        cov = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y)) / (len(days) - 1)
        return round(cov / (sx * sy), 2)

    if len(keys) < 2:
        return {"keys": [], "matrix": [], "window": window, "n": 0,
                "why": f"only {len(keys)} series had enough overlapping days "
                       f"in the last {window}",
                "have": {k: len(v) for k, v in series.items()},
                "port": "YOU" in series}

    matrix = [[1.0 if a == b else corr(a, b) for b in keys] for a in keys]
    return {"keys": keys, "matrix": matrix, "window": window, "why": None,
            "port": "YOU" in series,
            "n": min(len(series[k]) for k in keys) if keys else 0,
            "labels": {k: (BY_KEY[k]["n"] if k in BY_KEY else "Your portfolio") for k in keys}}


# --------------------------------------------------------------------------
# assembling the board
# --------------------------------------------------------------------------

STOXX_HIST = "https://www.stoxx.com/document/Indices/Current/HistoricalData/h_{s}.txt"


def parse_stoxx(text: str) -> dict:
    """STOXX's history file, "Date;Symbol;Indexvalue" with dates as dd.mm.yyyy: {ISO date: value}."""
    out = {}
    for line in text.splitlines():
        p = line.strip().split(";")
        if len(p) >= 3 and len(p[0]) == 10 and p[0][2] == "." and p[0][5] == ".":
            try:
                out[f"{p[0][6:]}-{p[0][3:5]}-{p[0][:2]}"] = float(p[2])
            except ValueError:
                pass
    return out


def stoxx_history(sym: str) -> dict:
    """A STOXX index's daily closes, from STOXX's own file (kept twelve hours)."""
    from . import net

    def load():
        h = parse_stoxx(net.get(STOXX_HIST.format(s=sym.lower()), timeout=30).decode("utf-8", "replace"))
        if len(h) < 250:
            raise RuntimeError(f"STOXX's file for {sym} had {len(h)} days")
        return {"h": h}
    return store.cache_through(f"mkt:stoxx:{sym}", 12 * 3600, load).get("h") or {}


def _hist_for(r: dict, rates_series: dict, start: str) -> dict:
    if r["src"] == "yahoo":
        return pricing.get_history(r["s"], start) or {}
    if r["src"] in ("fred", "ecb"):
        return rates_series.get(r["s"]) or {}
    if r["src"] == "stoxx":
        try:
            return {d: v for d, v in stoxx_history(r["s"]).items() if d >= start}
        except Exception:
            return {}
    return {}


QUOTE_MAX_AGE = 1800     # seconds: a quote the feed stopped updating gives way to the stored history


def row_tz(r: dict) -> str:
    """The clock a row's daily bars are dated by: its exchange, London for currencies, else New York."""
    return r.get("tz") or ("Europe/London" if str(r.get("s", "")).endswith("=X") else "America/New_York")


def quote_day(q: dict, tz: str, now: Optional[float] = None) -> Optional[str]:
    """
    The session date a feed quote belongs to, or None when it is too old to
    use. A poll carries the date of the bar it came from; a streamed tick
    carries its own time. With nobody watching, the feed keeps its last
    quotes for hours: the stored history (refetched every quarter of an hour
    while a market trades) is newer by then.
    """
    now = time.time() if now is None else now
    seen = q.get("seen") or q.get("ts")
    if not q.get("price") or (seen and now - seen > QUOTE_MAX_AGE):
        return None
    if q.get("asof"):
        return str(q["asof"])[:10]
    t = q.get("ts") or now
    return datetime.fromtimestamp(t, ZoneInfo(tz)).date().isoformat()


def board(rates_series: Optional[dict] = None,
          live: Optional[dict] = None,
          today: Optional[date] = None,
          now: Optional[float] = None) -> dict:
    """
    Every row, priced and measured. Blocking: it reads a year of history for
    about twenty symbols, so it runs on the background thread like everything
    else that touches a network.
    """
    today = today or date.today()
    now = time.time() if now is None else now
    start = (today - timedelta(days=430)).isoformat()
    rates_series = rates_series or {}
    live = live or {}

    hists: dict[str, dict] = {}
    rows: dict[str, dict] = {}

    for r in BOARD:
        if r["src"] == "calc":
            continue
        h = _hist_for(r, rates_series, start)
        hists[r["k"]] = h
        q = live.get(r["s"]) if r["src"] == "yahoo" else None
        qd = quote_day(q, row_tz(r), now) if q else None
        if qd is None:
            q = None
        st = row_stats(h, last=(q or {}).get("price"), today=today,
                       want_pctile=bool(r.get("pctile")), last_d=qd)
        rows[r["k"]] = {**{k: r[k] for k in ("g", "k", "n", "src", "u")},
                        "symbol": r["s"] if isinstance(r["s"], str) else None,
                        "note": r.get("note"),
                        "streaming": bool(q and q.get("src") == "stream" and st.get("asof") == qd),
                        **st}

    # Derived rows, after the legs exist.
    for r in BOARD:
        if r["src"] != "calc":
            continue
        a, b = r["s"]
        ha, hb = hists.get(a) or {}, hists.get(b) or {}
        days = sorted(set(ha) & set(hb))
        h = {d: ha[d] - hb[d] for d in days}
        hists[r["k"]] = h
        st = row_stats(h, today=today, want_pctile=bool(r.get("pctile")))
        rows[r["k"]] = {**{k: r[k] for k in ("g", "k", "n", "src", "u")},
                        "symbol": None, "note": r.get("note"),
                        "streaming": False, "derived_from": [a, b], **st}

    ordered = [rows[r["k"]] for r in BOARD if r["k"] in rows]
    missing = [r["n"] for r in ordered if r.get("stale") or r.get("last") is None]
    return {
        "rows": ordered,
        "groups": list(dict.fromkeys(r["g"] for r in BOARD)),
        "sessions": sessions(),
        "risk": risk_read(rows, hists, today),
        "missing": missing,
        "fetched_at": time.time(),
        "_hists": hists,          # stripped before the payload leaves the API
    }
