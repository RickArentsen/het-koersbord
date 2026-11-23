"""
FCF — what a share's free cash flow says it is worth, a few years out.

Qualtrim's model, on every holding and on any stock you ask for. Four inputs:

    trailing free cash flow per share
    the rate it grows at
    the multiple of free cash flow the market pays at the end
    the return you want

and two answers: the yearly return the price offers if those hold, and the
price to pay today for the return you want. For Meta on 23 September 2026:
$16.08 a share growing 30% for five years at 40x is $2,388; from $748.51 that
is 26.12% a year, and $1,187.34 is the price for 15%.

Qualtrim leaves the inputs to you. Here each starts from the company's own
figures, with the reasoning shown, and stays yours to change:

    growth      a starting rate, the average of analysts' revenue growth for
                this year and next and the company's own five-year growth in
                operating cash flow per share (kept between -5% and 40%),
                fading to 5% by the last year: few companies hold 30% for
                five years. The one figure shown is the steady rate that ends
                at the same place.
    multiple    the median price/FCF the stock itself has traded on over
                five years (the valuation band), kept between 8x and 35x;
                its 25th and 75th percentiles for the bear and the bull case
    return      what its risk calls for: the ten-year government yield in its
                currency plus its own beta (three years of weekly returns
                against its market, adjusted toward 1) times a 5% equity
                premium, kept between 7% and 20%

The free cash flow is the last four quarters' operating cash flow less
capital spending, per quoted share, in the price's currency. Two switches:
after stock-based pay, and normalised (operating cash flow less depreciation)
for a company in a heavy investment year, where capital spending runs far
above what keeps the business as it is.
"""
from __future__ import annotations

import json
import math
import statistics
import threading
import time
from datetime import date, datetime
from typing import Optional

from . import store

ERP = 5.0                       # the equity premium over the government bond, % a year
RET_MIN, RET_MAX = 7.0, 20.0
G_MIN, G_MAX = -5.0, 40.0       # the starting rate
G_LONG = 5.0                    # where it fades to by the last year
M_MIN, M_MAX = 8.0, 35.0        # the default multiple; a typed one may be anything
HORIZONS = (3, 5, 7, 10)
BASES = ("reported", "after_sbc", "normalised")
TTL = 3600


# --------------------------------------------------------------------------
# the model itself
# --------------------------------------------------------------------------

def project(fps: Optional[float], growth: Optional[float], multiple: Optional[float],
            ret: Optional[float], horizon: int, price: Optional[float]) -> Optional[dict]:
    """
    The Qualtrim arithmetic. Free cash flow per share grows `growth`% a year
    for `horizon` years and is then worth `multiple` times itself. The return
    is the yearly rate that turns today's price into that value; the entry
    price is that value discounted at the return you want. The path is
    today's price compounding at the return the model offers, year by year.
    """
    if fps is None or growth is None or multiple is None or not price or price <= 0:
        return None
    if fps <= 0:
        return {"ok": False, "why": "free cash flow is negative or nil: nothing to multiply"}
    n = int(horizon)
    fcf_n = fps * (1 + growth / 100) ** n
    value = fcf_n * multiple
    implied = ((value / price) ** (1 / n) - 1) * 100 if value > 0 else None
    entry = value / (1 + ret / 100) ** n if ret is not None else None
    path = [round(price * (1 + (implied or 0) / 100) ** t, 2) for t in range(n + 1)]
    fcf_path = [round(fps * (1 + growth / 100) ** t, 4) for t in range(n + 1)]
    return {"ok": True, "value": round(value, 2), "fcf_n": round(fcf_n, 4),
            "implied": round(implied, 2) if implied is not None else None,
            "entry": round(entry, 2) if entry is not None else None,
            "vs_price": round((entry / price - 1) * 100, 1) if entry else None,
            "path": path, "fcf_path": fcf_path}


def faded(g0: float, n: int, g_long: float = G_LONG) -> float:
    """
    The steady rate that ends where a fading one does: g0 in the first year,
    falling in a straight line to g_long in year n. 30% fading over five years
    is 30, 24, 18, 11, 5 and ends 2.2 times higher: 17.2% a year.
    """
    if n <= 1:
        return round(g0, 2)
    f = 1.0
    for t in range(n):
        f *= 1 + (g0 + (g_long - g0) * t / (n - 1)) / 100
    return round((f ** (1 / n) - 1) * 100, 2)


def spread(g: float) -> float:
    """How far the bear and bull growth sit from the base: four points, or three tenths of it."""
    return max(4.0, 0.3 * abs(g))


def cases(base_g: float, base_m: float, m_lo: Optional[float], m_hi: Optional[float],
          m_med: Optional[float]) -> dict:
    """Bear and bull around a base. The multiple keeps its band's shape when the base is yours."""
    s = spread(base_g)
    # The band's own shape, within reason: ASML's quarterly cash flow swings
    # with customers' prepayments, and its 75th percentile was 75x.
    lo_k = max(0.7, (m_lo / m_med)) if m_lo and m_med else 0.8
    hi_k = min(1.4, (m_hi / m_med)) if m_hi and m_med else 1.2
    clamp = lambda v, a, b: max(a, min(b, v))
    return {"bear": {"growth": round(clamp(base_g - s, G_MIN - 5, G_MAX + 10), 2), "multiple": round(clamp(base_m * lo_k, M_MIN * 0.75, M_MAX), 1)},
            "base": {"growth": round(base_g, 2), "multiple": round(base_m, 1)},
            "bull": {"growth": round(clamp(base_g + s, G_MIN, G_MAX + 10), 2), "multiple": round(clamp(base_m * hi_k, M_MIN, M_MAX * 1.25), 1)}}


# --------------------------------------------------------------------------
# what the company's own figures suggest
# --------------------------------------------------------------------------

def rf_for(ccy: Optional[str]) -> tuple[Optional[float], str]:
    """The ten-year government yield in the share's currency, from the market board."""
    rows = {r.get("k"): r.get("last") for r in (store.cache_get("market") or {}).get("rows") or []}
    from .research import major
    c = major(ccy)[0]
    if c == "EUR" and rows.get("EA10Y") is not None:
        return float(rows["EA10Y"]), "the euro area's ten-year yield"
    if rows.get("US10Y") is not None:
        return float(rows["US10Y"]), ("the ten-year Treasury" if c in (None, "USD") else
                                      f"the ten-year Treasury (no {c} yield kept)")
    return None, "no ten-year yield on the market board yet"


def beta_of(sym: str, ov: dict) -> dict:
    """
    Three years of weekly returns against the stock's own market (the S&P 500,
    the AEX, the DAX), adjusted a third of the way toward 1: a measured beta
    of 2 is more likely 1.67 next time, one of 0.4 more likely 0.6.
    """
    from . import research as rs
    try:
        bm = rs.benchmarks_for(sym, ov)["market"]
        mine = {b[0]: b[6] for b in rs.daily(sym)["bars"]}
        mkt = rs._closes(bm["sym"])
        days = sorted(d for d in mine if d in mkt)[-3 * 252:]
        wk = days[::5]
        a = [mine[y] / mine[x] - 1 for x, y in zip(wk, wk[1:])]
        b = [mkt[y] / mkt[x] - 1 for x, y in zip(wk, wk[1:])]
        if len(a) >= 52:
            ma, mb = statistics.fmean(a), statistics.fmean(b)
            cov = sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (len(a) - 1)
            var = statistics.variance(b)
            raw = cov / var if var else None
            if raw is not None:
                return {"raw": round(raw, 2), "adj": round(0.67 * raw + 0.33, 2), "weeks": len(a), "vs": bm["label"]}
    except Exception:
        pass
    yb = ((ov or {}).get("stats") or {}).get("beta")
    if yb:
        return {"raw": round(yb, 2), "adj": round(0.67 * yb + 0.33, 2), "weeks": None, "vs": "Yahoo's five-year monthly beta"}
    return {"raw": None, "adj": 1.0, "weeks": None, "vs": "no price history: a beta of 1"}


def _window(fund: dict) -> Optional[dict]:
    """
    The last four quarters that all have a cash flow statement, or the last
    year. A quarter known only from its earnings release (provisional) has
    no cash flow yet, and Yahoo leaves out some quarters' cash flow.
    """
    q = [r for r in fund.get("quarters") or [] if not r.get("provisional")]
    from .research import _dt
    for i in range(len(q) - 1, 2, -1):
        win = q[i - 3:i + 1]
        span = (_dt(win[-1]["end"]) - _dt(win[0]["end"])).days
        if 250 <= span <= 300 and all(w.get("ocf") is not None and w.get("capex") is not None for w in win):
            s = lambda k: sum(w[k] for w in win) if all(w.get(k) is not None for w in win) else None
            sh = next((x.get("shares") for x in reversed(win) if x.get("shares")), None) or _any_shares(fund)
            return {"end": win[-1]["end"], "basis": "the last four quarters", "label": f"four quarters to {win[-1]['label']}",
                    "ocf": s("ocf"), "capex": s("capex"), "sbc": s("sbc"), "dna": s("dna"), "shares": sh}
    for a in reversed(fund.get("annual") or []):
        if a.get("ocf") is not None and a.get("capex") is not None:
            return {"end": a["end"], "basis": "the last year", "label": a["label"], "ocf": a["ocf"], "capex": a["capex"],
                    "sbc": a.get("sbc"), "dna": a.get("dna"), "shares": a.get("shares") or _any_shares(fund)}
    return None


def _any_shares(fund: dict) -> Optional[float]:
    """The newest share count anywhere in the accounts: Visa's quarters carry none."""
    for r in reversed((fund.get("quarters") or []) + (fund.get("annual") or [])):
        if r.get("shares"):
            return r["shares"]
    return None


def _cagr(rows: list[dict], years: int = 5, key: str = "ocfps") -> tuple[Optional[float], int]:
    """
    Growth per share over up to five years, when both ends are positive.
    Measured on operating cash flow, not free cash flow: Amazon's and Meta's
    capital spending pushed their FCF per share down while the business grew,
    and a negative FCF growth said they were shrinking.
    """
    pts = [{"end": r["end"], "fps": r.get(key)} for r in rows if r.get(key) is not None]
    if len(pts) < 4:
        return None, 0
    last = pts[-1]
    first = pts[max(0, len(pts) - 1 - years)]
    n = int(round((datetime.fromisoformat(last["end"]) - datetime.fromisoformat(first["end"])).days / 365.25))
    if n < 3 or first["fps"] <= 0 or last["fps"] <= 0:
        return None, n
    return round(((last["fps"] / first["fps"]) ** (1 / n) - 1) * 100, 2), n


def _load_inputs(sym: str) -> dict:
    from . import research as rs
    ov = rs.overview(sym)
    if (ov.get("quote_type") or "EQUITY") != "EQUITY":
        return {"symbol": sym, "available": False,
                "why": f"{sym} is a {str(ov.get('quote_type') or 'fund').lower()}: it has no free cash flow of its own"}
    # A bank's or an insurer's operating cash flow moves with its loans and
    # deposits: there is no free cash flow to multiply. Visa and Mastercard
    # (credit services) are payment networks and stay in.
    import re
    if ov.get("sector") == "Financial Services" and re.search(
            r"bank|insurance|asset management|capital markets|mortgage|financial conglomerates|shell companies",
            ov.get("industry") or "", re.I):
        return {"symbol": sym, "available": False,
                "why": f"{ov.get('industry')}: a bank's or an insurer's cash flow moves with its loans, deposits and premiums, "
                       "not free cash flow, so the model does not apply"}
    fund = rs.fundamentals(sym)
    val = rs._safe(lambda: rs.valuation(sym)) or {}
    st = rs._safe(lambda: rs.street(sym)) or {}
    ccy, rccy = ov.get("currency"), fund.get("currency") or ov.get("fin_currency") or ov.get("currency")
    price = (ov.get("quote") or {}).get("price")
    w = _window(fund)
    if not w or not price:
        return {"symbol": sym, "available": False, "why": "no cash flow statement to start from"}
    fx = rs.FX(rccy, ccy)
    m_ps = fx.per_share()
    ratio = val.get("ratio") or rs.units_ratio(w.get("shares"), ov)
    if not w.get("shares"):
        sh = ((ov.get("stats") or {}).get("shares_out"))
        if sh:
            w["shares"], ratio = sh, 1.0
    if not m_ps or not w.get("shares"):
        why = f"no {rccy}→{ccy} exchange rate" if not m_ps else "no share count in the accounts"
        return {"symbol": sym, "available": False, "why": why + " to put the cash flow per share"}
    units = w["shares"] / ratio

    def per(x):
        return round(x * m_ps / units, 4) if x is not None else None
    fcf = w["ocf"] - w["capex"]
    fps = {"reported": per(fcf),
           "after_sbc": per(fcf - w["sbc"]) if w.get("sbc") is not None else None,
           "normalised": per(w["ocf"] - w["dna"]) if w.get("dna") is not None else None}
    # Building, not maintaining: capital spending at twice what wears out.
    heavy = w.get("dna") and w["capex"] > 2.0 * w["dna"]

    # FCF per share, year by year, at today's rate: the history the growth is measured on
    hist = []
    for a in fund.get("annual") or []:
        if a.get("ocf") is None or a.get("capex") is None or not a.get("shares"):
            continue
        hist.append({"end": a["end"], "label": a["label"], "fps": round((a["ocf"] - a["capex"]) * m_ps / (a["shares"] / ratio), 4),
                     "ocfps": round(a["ocf"] * m_ps / (a["shares"] / ratio), 4),
                     "fps_sbc": round((a["ocf"] - a["capex"] - (a.get("sbc") or 0)) * m_ps / (a["shares"] / ratio), 4)
                     if a.get("sbc") is not None else None})
    g_own, g_years = _cagr(hist)

    per_ = {p["period"]: p for p in st.get("periods") or []}
    rg = [((per_.get(k) or {}).get("rev") or {}).get("growth") for k in ("0y", "+1y")]
    rg = [x for x in rg if x is not None]
    g_cons = round(statistics.fmean(rg), 2) if rg else None
    have = [x for x in (g_cons, g_own) if x is not None]
    g_raw = statistics.fmean(have) if have else G_LONG
    g0 = round(max(G_MIN, min(G_MAX, g_raw)), 2)
    by_h = {n: faded(g0, n) for n in HORIZONS}
    g_why = " and ".join(x for x in (
        f"analysts' revenue growth {g_cons:+.1f}% a year for this year and next" if g_cons is not None else "",
        f"its own operating cash flow per share {g_own:+.1f}% a year over {g_years} years" if g_own is not None else "") if x)
    g_why = (f"starts at {g0:.1f}%, the average of {g_why}" if len(have) == 2
             else f"starts at {g0:.1f}%, {g_why}" if have else f"no growth figures: starts at {G_LONG:.0f}%")
    if not G_MIN <= g_raw <= G_MAX:
        g_why += f" (held at {g0:.0f}%)"
    g_why += f", fading to {G_LONG:.0f}% by the last year"

    pf = ((val.get("stats") or {}).get("pfcf") or {}).get("5y") or {}
    m_med, m_lo, m_hi = pf.get("median"), pf.get("p25"), pf.get("p75")
    cur_pfcf = round(price / fps["reported"], 1) if fps["reported"] and fps["reported"] > 0 else None
    if m_med:
        m_base = max(M_MIN, min(M_MAX, m_med))
        m_why = f"its own median price/FCF over five years, {m_med:.0f}× (middle half {m_lo:.0f}–{m_hi:.0f}×)"
        if not M_MIN <= m_med <= M_MAX:
            m_why += f", held at {m_base:.0f}×"
    else:
        m_base = max(10.0, min(35.0, cur_pfcf or 20.0))
        m_why = (f"no five-year band: today's {cur_pfcf:.0f}× held between 10 and 35" if cur_pfcf
                 else "no band and no positive FCF: 20× assumed")

    rf, rf_label = rf_for(ccy)
    b = beta_of(sym, ov)
    r_raw = (rf if rf is not None else 4.5) + b["adj"] * ERP
    r_base = round(max(RET_MIN, min(RET_MAX, r_raw)), 2)
    r_why = (f"{rf_label} {rf:.2f}%" if rf is not None else "a 4.5% yield (none on the board)") \
        + f" + beta {b['adj']:.2f} × {ERP:.0f}% equity premium"
    if b.get("raw") is not None and b.get("weeks"):
        r_why += f" (measured {b['raw']:.2f} over {b['weeks']} weeks against the {b['vs']}, adjusted toward 1)"
    elif b.get("raw") is not None:
        r_why += f" ({b['vs']})"
    if not RET_MIN <= r_raw <= RET_MAX:
        r_why += f", held at {r_base:.0f}%"

    notes = []
    avg5 = [h["fps"] for h in hist[-5:] if h.get("fps") is not None]
    if fps["reported"] and len(avg5) >= 3 and statistics.fmean(avg5) > 0 and fps["reported"] > 2 * statistics.fmean(avg5):
        notes.append(f"Free cash flow per share is {fps['reported'] / statistics.fmean(avg5):.1f}× its average of the last "
                     f"{len(avg5)} years: a cyclical high or a step change. The model grows it from here; for a cyclical "
                     "business a lower growth rate or multiple is the safer reading.")
    if heavy:
        notes.append(f"Capital spending is {w['capex'] / w['dna']:.1f}× depreciation: an investment phase. "
                     "The normalised base (operating cash flow less depreciation) shows what the business earns before it.")
    if fps["reported"] is not None and fps["reported"] <= 0:
        notes.append("Free cash flow is negative over " + w["label"] + ": the model needs a positive base; try the normalised one.")
    return {
        "symbol": sym, "available": True, "name": ov.get("short") or ov.get("name"), "currency": ccy,
        "reported_currency": rccy, "price": price, "unit": rs.major(ccy)[1], "ratio": ratio,
        "window": w["label"], "window_end": w["end"], "fps": fps, "heavy": bool(heavy),
        "fcf_yield": round(fps["reported"] / price * 100, 2) if fps["reported"] else None,
        "p_fcf": cur_pfcf, "history": hist[-10:],
        "growth": {"base": by_h[5], "start": g0, "long": G_LONG, "by_horizon": by_h, "consensus": g_cons,
                   "own": g_own, "own_years": g_years, "why": g_why},
        "multiple": {"base": round(m_base, 1), "median": m_med, "p25": m_lo, "p75": m_hi, "current": cur_pfcf, "why": m_why},
        "ret": {"base": r_base, "rf": rf, "rf_label": rf_label, "beta": b, "erp": ERP, "why": r_why},
        "notes": notes, "fetched_at": time.time(),
    }


def inputs(sym: str) -> dict:
    return store.cache_through(f"rs:fcf:{sym}", TTL, lambda: _load_inputs(sym))


# --------------------------------------------------------------------------
# what you changed, per stock
# --------------------------------------------------------------------------

def saved(sym: str) -> dict:
    with store.conn() as c:
        r = c.execute("SELECT growth, multiple, ret, base, horizon, listed, updated FROM fcf_model WHERE sym = ?",
                      (sym,)).fetchone()
    if not r:
        return {}
    return {k: v for k, v in zip(("growth", "multiple", "ret", "base", "horizon", "listed", "updated"), r) if v is not None}


def save(sym: str, body: dict) -> dict:
    """Each field set, or None to go back to the company's. `listed` puts a stock you do not hold on the FCF page."""
    cur = saved(sym)
    new = dict(cur)
    for k in ("growth", "multiple", "ret", "base", "horizon", "listed"):
        if k in body:
            new[k] = body[k]
    if new.get("base") not in (None, *BASES):
        raise ValueError(f"base is one of {', '.join(BASES)}")
    if new.get("horizon") is not None and int(new["horizon"]) not in HORIZONS:
        raise ValueError(f"horizon is one of {', '.join(map(str, HORIZONS))} years")
    for k, lo, hi in (("growth", -50, 100), ("multiple", 1, 200), ("ret", 0, 50)):
        if new.get(k) is not None and not lo <= float(new[k]) <= hi:
            raise ValueError(f"{k} must be between {lo} and {hi}")
    with store.conn() as c:
        c.execute("INSERT INTO fcf_model (sym, growth, multiple, ret, base, horizon, listed, updated) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                  "ON CONFLICT(sym) DO UPDATE SET growth = excluded.growth, multiple = excluded.multiple, ret = excluded.ret, "
                  "base = excluded.base, horizon = excluded.horizon, listed = excluded.listed, updated = excluded.updated",
                  (sym, new.get("growth"), new.get("multiple"), new.get("ret"), new.get("base"), new.get("horizon"),
                   int(bool(new.get("listed"))), datetime.now().isoformat(timespec="seconds")))
    _TABLE["stale"] = True
    return saved(sym)


def listed() -> list[str]:
    with store.conn() as c:
        return [r[0] for r in c.execute("SELECT sym FROM fcf_model WHERE listed = 1 ORDER BY sym").fetchall()]


# --------------------------------------------------------------------------
# one stock, every case
# --------------------------------------------------------------------------

def model(sym: str, inp: Optional[dict] = None, sv: Optional[dict] = None) -> dict:
    """Inputs, what you changed, and the three cases, for the stock page and the table."""
    inp = inp or inputs(sym)
    if not inp.get("available"):
        return inp
    sv = sv if sv is not None else saved(sym)
    base_k = sv.get("base") or "reported"
    fps = inp["fps"].get(base_k)
    horizon = int(sv.get("horizon") or 5)
    g_def = (inp["growth"].get("by_horizon") or {}).get(horizon, (inp["growth"].get("by_horizon") or {}).get(str(horizon)))
    g = float(sv["growth"]) if sv.get("growth") is not None else (g_def if g_def is not None else inp["growth"]["base"])
    m = float(sv["multiple"]) if sv.get("multiple") is not None else inp["multiple"]["base"]
    r = float(sv["ret"]) if sv.get("ret") is not None else inp["ret"]["base"]
    mu = inp["multiple"]
    cs = cases(g, m, mu.get("p25"), mu.get("p75"), mu.get("median"))
    out = {}
    for name, c in cs.items():
        out[name] = {**c, **(project(fps, c["growth"], c["multiple"], r, horizon, inp["price"]) or {"ok": False})}
    return {**{k: v for k, v in inp.items() if not k.startswith("_")}, "saved": sv, "base_kind": base_k,
            "start": fps, "horizon": horizon, "growth_used": g, "multiple_used": m, "ret_used": r,
            "cases": out, "edited": any(sv.get(k) is not None for k in ("growth", "multiple", "ret", "base", "horizon"))}


# --------------------------------------------------------------------------
# the table: your holdings, what your turbos track, the stocks you added
# --------------------------------------------------------------------------

_TABLE: dict = {"rows": {}, "state": "idle", "i": 0, "n": 0, "started": None, "finished": None, "stale": True,
                "errors": {}}
_LOCK = threading.Lock()


def universe() -> list[dict]:
    """[{sym, why, weight, name}]: shares you hold, the shares your turbos track, then your list."""
    out, seen = [], set()
    try:
        from . import service
        insts = store.load_instruments()
        d = service.dashboard("SP500", light=True)
        for r in sorted(d.get("positions", []), key=lambda r: -(r.get("weight") or 0)):
            if r.get("excluded"):
                continue
            kind = r.get("kind")
            if kind == "EQUITY" and r.get("ticker"):
                s, why = r["ticker"].upper(), "held"
            elif kind == "DERIVATIVE":
                s = ((insts.get(r.get("isin")) or {}).get("underlying_ticker") or "").upper()
                why = "via a turbo"
            else:
                continue
            if s and s not in seen:
                seen.add(s)
                out.append({"sym": s, "why": why, "weight": r.get("weight"), "name": r.get("name")})
    except Exception:
        pass
    for s in listed():
        if s not in seen:
            seen.add(s)
            out.append({"sym": s, "why": "your list", "weight": None, "name": None})
    return out


def _row(u: dict) -> dict:
    m = model(u["sym"])
    if not m.get("available"):
        return {**u, "available": False, "why_not": m.get("why")}
    b = m["cases"]["base"]
    why_not = None if b.get("ok") else b.get("why")
    if not b.get("ok") and m["base_kind"] == "reported" and (m["fps"].get("normalised") or 0) > 0:
        alt = project(m["fps"]["normalised"], m["growth_used"], m["multiple_used"], m["ret_used"], m["horizon"], m["price"])
        if alt and alt.get("ok"):
            why_not = (f"free cash flow is negative over {m['window']}; on the normalised base (operating cash flow "
                       f"less depreciation) it offers {alt['implied']:.1f}% a year")
    return {**u, "available": True, "name": m.get("name") or u.get("name"), "currency": m["currency"], "price": m["price"],
            "start": m["start"], "base_kind": m["base_kind"], "fcf_yield": m.get("fcf_yield"),
            "growth": m["growth_used"], "multiple": m["multiple_used"], "ret": m["ret_used"], "horizon": m["horizon"],
            "implied": b.get("implied"), "entry": b.get("entry"), "vs_price": b.get("vs_price"), "ok": b.get("ok"),
            "bear": m["cases"]["bear"].get("implied"), "bull": m["cases"]["bull"].get("implied"),
            "edited": m["edited"], "why_not": why_not}


def _build():
    """One stock at a time: each reads its statements, estimates and prices, and Yahoo is asked one request at a time."""
    try:
        uni = universe()
        _TABLE.update(state="running", i=0, n=len(uni), started=time.time(), stale=False, errors={})
        keep = {}
        for i, u in enumerate(uni):
            try:
                keep[u["sym"]] = _row(u)
            except Exception as e:
                keep[u["sym"]] = {**u, "available": False, "why_not": f"{e.__class__.__name__}: {e}"[:160]}
                _TABLE["errors"][u["sym"]] = keep[u["sym"]]["why_not"]
            _TABLE["rows"] = {**_TABLE["rows"], u["sym"]: keep[u["sym"]]}
            _TABLE["i"] = i + 1
            time.sleep(0.2)
        _TABLE["rows"] = keep
        _TABLE.update(state="done", finished=time.time())
    except Exception as e:
        _TABLE.update(state="error", finished=time.time(), errors={"_": f"{e.__class__.__name__}: {e}"[:200]})


def table(refresh: bool = False) -> dict:
    """What is ready, and the build running behind it when the rows are older than an hour or you changed one."""
    with _LOCK:
        old = _TABLE["finished"] is None or time.time() - _TABLE["finished"] > TTL
        if _TABLE["state"] != "running" and (refresh or old or _TABLE["stale"]):
            _TABLE["state"] = "running"
            threading.Thread(target=_build, name="fcf-table", daemon=True).start()
    rows = sorted(_TABLE["rows"].values(), key=lambda r: (not r.get("available") or not r.get("ok"),
                                                         -((r.get("implied") or -99) - (r.get("ret") or 0))))
    return {"rows": rows, "state": _TABLE["state"], "i": _TABLE["i"], "n": _TABLE["n"],
            "finished": _TABLE["finished"], "errors": _TABLE["errors"], "erp": ERP,
            "bounds": {"growth": [G_MIN, G_MAX], "multiple": [M_MIN, M_MAX], "ret": [RET_MIN, RET_MAX]},
            "horizons": HORIZONS}
