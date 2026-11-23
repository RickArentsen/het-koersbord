"""
OPT — what the options market says about a share.

An option's price is a bet on how far a share can move by a date, so a whole
chain of them, every strike and every expiry, is the market's own estimate of
that share's risk, written down in dollars. This module turns one chain into:

  THE SURFACE   implied volatility for every strike and expiry. The level
                says how much movement is priced; the tilt across strikes
                (the skew) how much more a fall is feared than a rise is
                hoped for; the slope across expiries whether the worry is
                this month's or next year's.
  THE RANGE     the distribution of the share price at each expiry that the
                prices imply, as a cone, and the odds of any level.
  THE HISTORY   thirty-day implied volatility next to what the share
                actually did. Yahoo keeps no history of it, so one point is
                stored per session from the day this was built.
  THE CHAIN     open interest and volume by strike.
  ONE CONTRACT  a single call or put worked out: what it costs, what it pays
                at expiry, where it breaks even, the odds the chain itself
                puts on that, and how its price moves with the share, with
                time and with volatility.

One request per share: Yahoo answers the whole chain at once when asked with
getAllData. There are two readings of it:

  quotes   While New York is open the chain carries bids and asks. Implied
           volatility is solved from each out-of-the-money option's mid
           price, against a forward backed out of put-call parity for that
           expiry, so option quotes that are fifteen minutes old are never
           compared with a share price that is not.
  trades   Outside the session Yahoo blanks every bid and ask, and its own
           implied-volatility field reads 0.00001. Then each contract's last
           trade is paired with the share price at the minute it traded.
           Liquid names give a full surface; a strike that did not trade
           that day is simply absent.

Both solve Black's formula for a European option on the forward. US equity
options are American; for the out-of-the-money side used here the difference
is small. The smile is a local quadratic in log-moneyness and is never
extended onto the surface beyond the strikes that priced: a short expiry
covers fewer strikes than a long one, and the chart shows that.

The odds are the ones built into prices. They lean pessimistic, because
protection against a fall costs extra: a yardstick, not a forecast.
"""

from __future__ import annotations

import json
import math
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import store

try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:                                   # pragma: no cover
    NY = timezone(timedelta(hours=-4))

URL = "https://query2.finance.yahoo.com/v7/finance/options/{sym}"
YEAR = 365.0
MONEY = [round(0.5 + 0.025 * i, 3) for i in range(41)]      # strike / forward
DELTAS = [round(0.90 - 0.05 * i, 2) for i in range(17)]     # call delta, .90 … .10
TENORS = (30, 60, 90, 180, 365)
QUANT = (0.025, 0.16, 0.5, 0.84, 0.975)
MIN_DAYS = 1.5              # nearer expiries are all gamma and no information
REACH = 4.0                 # standard deviations from the forward a strike may sit
FRESH_OPEN, FRESH_SHUT = 300, 3600
MIN_HISTORY = 30            # sessions before a rank means anything


class NoOptions(Exception):
    pass


# --------------------------------------------------------------------------
# Black's formula on the forward, and its inverse
# --------------------------------------------------------------------------

def ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black(F: float, K: float, T: float, vol: float, call: bool) -> float:
    """Undiscounted price of a European option on a forward."""
    if vol <= 0 or T <= 0:
        return max(0.0, F - K) if call else max(0.0, K - F)
    s = vol * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * s * s) / s
    d2 = d1 - s
    return F * ncdf(d1) - K * ncdf(d2) if call else K * ncdf(-d2) - F * ncdf(-d1)


def implied(price: float, F: float, K: float, T: float, call: bool) -> Optional[float]:
    """The volatility at which `black` gives `price` (undiscounted), or None."""
    if T <= 0 or F <= 0 or K <= 0:
        return None
    floor = max(0.0, (F - K) if call else (K - F))
    if price <= floor + 1e-9 or price >= (F if call else K):
        return None
    lo, hi = 0.005, 8.0
    if black(F, K, T, hi, call) < price or black(F, K, T, lo, call) > price:
        return None
    for _ in range(64):
        mid = 0.5 * (lo + hi)
        if black(F, K, T, mid, call) > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def call_delta(k: float, vol: float, T: float) -> float:
    """Forward delta of a call at log-moneyness k."""
    s = vol * math.sqrt(T)
    return ncdf((-k + 0.5 * s * s) / s) if s > 0 else (1.0 if k < 0 else 0.0)


# --------------------------------------------------------------------------
# the chain, as Yahoo gives it
# --------------------------------------------------------------------------

def _yf():
    import yfinance as yf
    return yf


def fetch_chain(sym: str) -> dict:
    """
    Every expiry of one underlying in one request, in a compact shape:
    rows are [strike, bid, ask, last, last trade (unix), volume, open interest].
    """
    t = _yf().Ticker(sym)
    raw = t._data.get_raw_json(URL.format(sym=sym), params={"getAllData": "true"})
    res = ((raw.get("optionChain") or {}).get("result") or [None])[0]
    if not res or not res.get("options"):
        raise NoOptions(f"Yahoo lists no options on {sym}")
    q = res.get("quote") or {}
    # After a split the old contracts live on under an adjusted root (NVDA1)
    # with strikes and sizes that no longer mean what they say. Keep only the
    # standard ones: ROOT + yymmdd + C/P + strike.
    std = re.compile(r"^" + re.escape(sym.replace("-", "")) + r"\d{6}[CP]\d{8}$")
    exps = []
    for blk in res["options"]:
        exp = datetime.fromtimestamp(blk["expirationDate"], tz=timezone.utc).date().isoformat()

        def rows(side):
            out = []
            for c in blk.get(side) or []:
                if not c.get("strike") or not std.match(c.get("contractSymbol") or ""):
                    continue
                out.append([float(c["strike"]), float(c.get("bid") or 0), float(c.get("ask") or 0),
                            float(c.get("lastPrice") or 0), int(c.get("lastTradeDate") or 0),
                            int(c.get("volume") or 0), int(c.get("openInterest") or 0)])
            return sorted(out)
        e = {"exp": exp, "calls": rows("calls"), "puts": rows("puts")}
        if e["calls"] or e["puts"]:
            exps.append(e)
    if not exps or not q.get("regularMarketPrice"):
        raise NoOptions(f"Yahoo lists no options on {sym}")
    return {"sym": sym, "fetched": time.time(), "state": q.get("marketState"),
            "spot": float(q["regularMarketPrice"]), "spot_ts": q.get("regularMarketTime"),
            "prev_close": q.get("regularMarketPreviousClose"),
            "div_yield": float(q.get("trailingAnnualDividendYield") or 0.0),
            "earnings_ts": q.get("earningsTimestamp") or q.get("earningsTimestampStart"),
            "name": q.get("shortName") or q.get("longName") or sym,
            "currency": q.get("currency") or "USD", "exps": exps}


def session_of(ch: dict) -> str:
    """The New York date of the session the share price belongs to."""
    ts = ch.get("spot_ts") or ch["fetched"]
    return datetime.fromtimestamp(ts, tz=NY).date().isoformat()


def live(ch: dict) -> bool:
    """Does the chain carry real bids and asks near the money?"""
    spot, n, ok = ch["spot"], 0, 0
    for e in ch["exps"][:6]:
        for row in e["calls"] + e["puts"]:
            if abs(row[0] / spot - 1) <= 0.1:
                n += 1
                ok += row[1] > 0 and row[2] > 0
    return n >= 8 and ok / n >= 0.6


def bars5(sym: str) -> list:
    """[(unix, close)] of five-minute bars for the last five sessions."""
    h = _yf().Ticker(sym).history(period="5d", interval="5m", auto_adjust=False)
    return sorted((int(i.timestamp()), float(v)) for i, v in h["Close"].dropna().items())


def _under_at(bars: list, ts: int) -> Optional[float]:
    """The share price in the five-minute bar that contains `ts`."""
    from bisect import bisect_right
    i = bisect_right(bars, (ts, float("inf"))) - 1
    if i < 0 or ts - bars[i][0] > 1800:
        return None
    return bars[i][1]


_CURVE: dict = {"t": 0.0, "pts": None}


def _rate(T: float) -> float:
    """The Treasury yield for about T years, as a fraction."""
    from . import bonds
    if _CURVE["pts"] is None or time.time() - _CURVE["t"] > 3600:
        # Kept for an hour: reading the curve unpacks thirty-five years of
        # yields, and this is asked once per expiry.
        try:
            _CURVE.update(t=time.time(), pts=bonds._us_curve_now(bonds.us_series()))
        except Exception:
            _CURVE.update(t=time.time(), pts=[])
    try:
        y = bonds._interp(_CURVE["pts"], max(T, 1 / 12)) if _CURVE["pts"] else None
    except Exception:
        y = None
    return (y or 4.0) / 100.0


def _expiry_ts(exp: str) -> float:
    y, m, d = (int(x) for x in exp.split("-"))
    return datetime(y, m, d, 16, 0, tzinfo=NY).timestamp()


# --------------------------------------------------------------------------
# one expiry: points, the forward, the fitted smile
# --------------------------------------------------------------------------

def _mid(row: list) -> float:
    return 0.5 * (row[1] + row[2])


def _quoted(row: list) -> bool:
    bid, ask = row[1], row[2]
    if bid <= 0 or ask < bid:
        return False
    mid = 0.5 * (bid + ask)
    return mid >= 0.03 and (ask - bid) / mid <= 1.0


def parity_forward(e: dict, spot: float, df: float, carry: float) -> float:
    """
    The forward the option quotes themselves imply: K + (C - P) / DF at the
    strikes where call and put are closest in price, median of five.
    """
    calls = {r[0]: r for r in e["calls"] if _quoted(r)}
    puts = {r[0]: r for r in e["puts"] if _quoted(r)}
    both = sorted((abs(_mid(calls[k]) - _mid(puts[k])), k) for k in calls if k in puts)[:5]
    fs = sorted(k + (_mid(calls[k]) - _mid(puts[k])) / df for _, k in both)
    f = fs[len(fs) // 2] if fs else spot * carry
    return f if 0.75 < f / spot < 1.25 else spot * carry


def points_quotes(e: dict, F: float, T: float, df: float) -> list:
    pts = []
    for call, rows in ((False, e["puts"]), (True, e["calls"])):
        for row in rows:
            K = row[0]
            if (K < F) == call or not _quoted(row):         # out of the money only
                continue
            mid = _mid(row)
            iv = implied(mid / df, F, K, T, call)
            if iv:
                pts.append((math.log(K / F), iv, 1.0 / (0.05 + (row[2] - row[1]) / mid)))
    return pts


def points_trades(e: dict, exp_ts: float, bars: list, session: str, r: float, q: float) -> list:
    pts = []
    for call, rows in ((False, e["puts"]), (True, e["calls"])):
        for K, _, _, last, ts, vol, _ in rows:
            if last < 0.03 or not ts or datetime.fromtimestamp(ts, tz=NY).date().isoformat() != session:
                continue
            S = _under_at(bars, ts)
            T = (exp_ts - ts) / (YEAR * 86400)
            if not S or T <= 0:
                continue
            F = S * math.exp((r - q) * T)
            if (K < F) == call:
                continue
            iv = implied(last * math.exp(r * T), F, K, T, call)
            if iv:
                pts.append((math.log(K / F), iv, math.log1p(vol or 1)))
    return pts


class Smile:
    """Implied volatility as a smooth function of log-moneyness for one expiry."""

    def __init__(self, pts: list, T: float):
        import numpy as np
        pts = sorted(pts)
        self.np, self.T = np, T
        self.k = np.array([p[0] for p in pts])
        self.v = np.array([p[1] for p in pts])
        self.w = np.array([p[2] for p in pts])
        self.lo, self.hi = float(self.k.min()), float(self.k.max())
        atm = float(self.v[int(np.argmin(np.abs(self.k)))])
        gap = float(np.median(np.diff(self.k))) if len(pts) > 1 else 0.05
        # Wide enough to average out quote noise, narrow enough to keep the
        # shape: a third of a standard deviation, and never under two strikes.
        self.h = max(0.33 * atm * math.sqrt(T), 2.0 * gap, 0.012)

    def _fit(self, k0: float) -> float:
        np = self.np
        d = self.k - k0
        w = self.w * np.exp(-0.5 * (d / self.h) ** 2)
        if int((w > 1e-4 * w.max()).sum()) < 4:
            return float(np.interp(k0, self.k, self.v))
        X = np.column_stack([np.ones_like(d), d, d * d])
        A = X.T @ (X * w[:, None])
        b = X.T @ (w * self.v)
        try:
            v = float(np.linalg.solve(A + 1e-10 * np.eye(3), b)[0])
        except Exception:
            return float(np.interp(k0, self.k, self.v))
        near = self.v[np.abs(d) <= 2.5 * self.h]
        if len(near):
            v = min(max(v, float(near.min()) * 0.8), float(near.max()) * 1.2)
        return v

    def grid(self):
        """(k, volatility, call delta) on 161 points across the strikes that priced."""
        if getattr(self, "_g", None) is None:
            np = self.np
            gk = np.linspace(self.lo, self.hi, 161)
            gv = np.array([self._fit(float(k)) for k in gk])
            gd = np.array([call_delta(float(k), float(v), self.T) for k, v in zip(gk, gv)])
            self._g = (gk, gv, gd)
        return self._g

    def at(self, k: float) -> Optional[float]:
        """The fitted volatility, or None outside the strikes that priced."""
        if k < self.lo - 0.012 or k > self.hi + 0.012:
            return None
        return self.flat(k)

    def flat(self, k: float) -> float:
        """As `at`, with the edge value carried outward. For the tails only."""
        gk, gv, _ = self.grid()
        return float(self.np.interp(k, gk, gv))


def within_reach(pts: list, T: float) -> list:
    """
    The points within REACH standard deviations of the forward. Further out
    an option costs a few cents, its last trade may be days old, and the
    volatility solved from it is noise: a strike 30% below the price with a
    week to run "implies" 90% and means nothing.
    """
    if not pts:
        return pts
    atm = min(pts, key=lambda p: abs(p[0]))[1]
    lim = max(REACH * atm * math.sqrt(T), 0.04)
    return [p for p in pts if abs(p[0]) <= lim]


def fit_smile(pts: list, T: float) -> Optional[Smile]:
    """A smile through the points, after one pass that drops the strays."""
    import numpy as np
    pts = within_reach(pts, T)
    if len(pts) < 5:
        return None
    s = Smile(pts, T)
    res = np.array([p[1] - s._fit(p[0]) for p in sorted(pts)])
    mad = float(np.median(np.abs(res - np.median(res)))) * 1.4826
    keep = [p for p, r in zip(sorted(pts), res) if abs(r) <= max(4 * mad, 0.12 * p[1])]
    if len(keep) < len(pts) and len(keep) >= 5:
        s = Smile(keep, T)
    return s if s.lo < 0.02 and s.hi > -0.02 else None


def delta_strike(s: Smile, target: float) -> Optional[tuple]:
    """(log-moneyness, volatility) where the call delta equals `target`."""
    gk, gv, gd = s.grid()
    for i in range(1, len(gk)):
        d0, d1 = gd[i - 1], gd[i]
        if (d0 - target) * (d1 - target) <= 0 and d0 != d1:
            a = (d0 - target) / (d0 - d1)
            return float(gk[i - 1] + a * (gk[i] - gk[i - 1])), float(gv[i - 1] + a * (gv[i] - gv[i - 1]))
    return None


def distribution(s: Smile, F: float) -> dict:
    """
    The distribution of the price at expiry that the smile implies: the
    chance of finishing below K is one plus the slope of the call price in K.
    Beyond the last strike that priced, its volatility is carried flat.
    """
    import numpy as np
    ks = np.linspace(-1.4, 1.0, 241)
    K = F * np.exp(ks)
    gk, gv, _ = s.grid()
    vols = np.interp(ks, gk, gv)
    C = np.array([black(F, float(kk), s.T, float(v), True) for kk, v in zip(K, vols)])
    cdf = 1.0 + np.gradient(C, K)
    cdf = np.maximum.accumulate(np.clip(cdf, 0.0, 1.0))
    qs = [float(np.interp(p, cdf, K)) for p in QUANT]
    keep = (cdf > 0.004) & (cdf < 0.996)
    idx = np.where(keep)[0][::4]
    return {"q": qs, "cdf": [[round(float(K[i]), 2), round(float(cdf[i]), 4)] for i in idx]}


# --------------------------------------------------------------------------
# the analysis
# --------------------------------------------------------------------------

def _cm(rows: list, days: float, key: str = "atm") -> Optional[float]:
    """Constant-maturity value by interpolating total variance between expiries."""
    pts = [(r["days"], r[key]) for r in rows if r.get(key)]
    if not pts:
        return None
    if days <= pts[0][0]:
        return pts[0][1]
    for (d0, v0), (d1, v1) in zip(pts, pts[1:]):
        if d0 <= days <= d1:
            w = (v0 * v0 * d0 + (v1 * v1 * d1 - v0 * v0 * d0) * (days - d0) / (d1 - d0)) / days
            return math.sqrt(max(w, 0.0))
    return pts[-1][1]


def _flow(ch: dict) -> dict:
    spot, per, active = ch["spot"], [], []
    tot = {"cv": 0, "pv": 0, "co": 0, "po": 0}
    for e in ch["exps"]:
        cv, pv = sum(r[5] for r in e["calls"]), sum(r[5] for r in e["puts"])
        co, po = sum(r[6] for r in e["calls"]), sum(r[6] for r in e["puts"])
        up = max((r for r in e["calls"] if r[0] >= spot), key=lambda r: r[6], default=None)
        dn = max((r for r in e["puts"] if r[0] <= spot), key=lambda r: r[6], default=None)
        per.append({"exp": e["exp"], "call_vol": cv, "put_vol": pv, "call_oi": co, "put_oi": po,
                    "call_wall": up[0] if up and up[6] else None, "call_wall_oi": up[6] if up else None,
                    "put_wall": dn[0] if dn and dn[6] else None, "put_wall_oi": dn[6] if dn else None})
        for k, v in (("cv", cv), ("pv", pv), ("co", co), ("po", po)):
            tot[k] += v
        for side, rows in (("C", e["calls"]), ("P", e["puts"])):
            for r in rows:
                if r[5]:
                    active.append({"exp": e["exp"], "type": side, "strike": r[0], "last": r[3],
                                   "vol": r[5], "oi": r[6],
                                   "ratio": round(r[5] / r[6], 1) if r[6] else None})
    active.sort(key=lambda a: -a["vol"])
    return {"expiries": per, "call_vol": tot["cv"], "put_vol": tot["pv"],
            "call_oi": tot["co"], "put_oi": tot["po"],
            "pc_vol": round(tot["pv"] / tot["cv"], 2) if tot["cv"] else None,
            "pc_oi": round(tot["po"] / tot["co"], 2) if tot["co"] else None,
            "active": active[:14]}


_DONE: dict = {}


def analysed(ch: dict, bars: Optional[list] = None) -> dict:
    """`analyse`, remembered for as long as the chain it read is the current one."""
    key = (ch["sym"], ch["fetched"], bool(bars))
    hit = _DONE.get(ch["sym"])
    if not hit or hit[0] != key:
        hit = (key, analyse(ch, bars))
        _DONE[ch["sym"]] = hit
    return dict(hit[1])


def analyse(ch: dict, bars: Optional[list] = None) -> dict:
    """Everything the page shows that comes out of one chain."""
    spot, q, now = ch["spot"], ch.get("div_yield") or 0.0, ch["fetched"]
    mode = "quotes" if live(ch) else "trades"
    session = session_of(ch)
    bars = [tuple(b) for b in bars or []]           # lists once they have been through JSON
    if mode == "trades":
        # The share price that goes with the last trades is the close.
        now = min(now, datetime.fromisoformat(session + "T16:00:00").replace(tzinfo=NY).timestamp())
    rows, smiles = [], {}
    for e in ch["exps"]:
        exp_ts = _expiry_ts(e["exp"])
        T = (exp_ts - now) / (YEAR * 86400)
        if T * YEAR < MIN_DAYS:
            continue
        r = _rate(T)
        df, carry = math.exp(-r * T), math.exp((r - q) * T)
        if mode == "quotes":
            F = parity_forward(e, spot, df, carry)
            pts = points_quotes(e, F, T, df)
        else:
            F = spot * carry
            pts = points_trades(e, exp_ts, bars or [], session, r, q)
        s = fit_smile(pts, T)
        if not s:
            continue
        atm = s.at(0.0)
        if not atm:
            continue
        row = {"exp": e["exp"], "days": round(T * YEAR, 2), "t": round(T, 5), "fwd": round(F, 2),
               "atm": round(atm, 4), "n": len(s.k),
               "lo": round(math.exp(s.lo), 3), "hi": round(math.exp(s.hi), 3),
               "move": round(atm * math.sqrt(T) * 100, 2)}
        for name, target in (("p25", 0.75), ("c25", 0.25)):
            hit = delta_strike(s, target)
            if hit:
                row[name], row["m_" + name] = round(hit[1], 4), round(math.exp(hit[0]), 4)
        try:
            dist = distribution(s, F)
            row["q"] = [round(x, 2) for x in dist["q"]]
            row["cdf"] = dist["cdf"]
        except Exception:
            pass
        rows.append(row)
        smiles[e["exp"]] = s
    if not rows:
        raise NoOptions(f"no expiry of {ch['sym']} had enough priced strikes to read"
                        + ("" if mode == "quotes" else " — the market is closed and too few contracts traded"))

    # the surface on two grids: by strike (ragged, as priced) and by delta
    cols = [i for i, m in enumerate(MONEY)
            if sum(1 for r in rows if smiles[r["exp"]].at(math.log(m)) is not None) >= 2]
    money = [MONEY[i] for i in cols]
    zm = [[(lambda v: round(v, 4) if v else None)(smiles[r["exp"]].at(math.log(m))) for m in money] for r in rows]
    zd, md = [], []
    for r in rows:
        s = smiles[r["exp"]]
        hits = [delta_strike(s, d) for d in DELTAS]
        zd.append([round(h[1], 4) if h else None for h in hits])
        md.append([round(math.exp(h[0]), 4) if h else None for h in hits])
    smile_pts = {r["exp"]: [[round(math.exp(float(k)), 4), round(float(v), 4)]
                            for k, v in zip(smiles[r["exp"]].k, smiles[r["exp"]].v)] for r in rows}

    cm = {str(d): (lambda v: round(v, 4) if v else None)(_cm(rows, d)) for d in TENORS}
    p30, c30 = _cm(rows, 30, "p25"), _cm(rows, 30, "c25")
    return {"sym": ch["sym"], "name": ch["name"], "currency": ch["currency"], "mode": mode,
            "session": session, "asof": ch["fetched"], "state": ch.get("state"),
            "spot": spot, "prev_close": ch.get("prev_close"),
            "expiries": rows, "money": money, "z_money": zm, "deltas": DELTAS, "z_delta": zd,
            "m_delta": md, "points": smile_pts, "cm": cm,
            "skew30": {"put": round(p30, 4) if p30 else None, "call": round(c30, 4) if c30 else None,
                       "gap": round((p30 - c30) * 100, 1) if p30 and c30 else None},
            "flow": _flow(ch),
            "earnings": datetime.fromtimestamp(ch["earnings_ts"], tz=NY).date().isoformat()
            if ch.get("earnings_ts") else None}


# --------------------------------------------------------------------------
# what the share actually did
# --------------------------------------------------------------------------

def _load_closes(sym: str) -> dict:
    from . import bonds
    m = bonds._download([sym], period="2y").get(sym) or {}
    if len(m) < 30:
        raise RuntimeError(f"no price history for {sym}")
    return {"closes": m}


def realised(sym: str) -> dict:
    m = store.cache_through(f"opt:px:{sym}", 6 * 3600, lambda: _load_closes(sym))["closes"]
    days = sorted(m)
    r = [math.log(m[b] / m[a]) for a, b in zip(days, days[1:]) if m[a] > 0 and m[b] > 0]

    def vol(xs):
        if len(xs) < 5:
            return None
        mu = sum(xs) / len(xs)
        return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1) * 252)
    series = []
    for i in range(21, len(r) + 1):
        if (len(r) - i) % 3 == 0:                       # every third day is plenty for a chart
            series.append([days[i], round(vol(r[i - 21:i]), 4)])
    cur = vol(r[-21:])
    all21 = [v for _, v in series]
    return {"rv10": vol(r[-10:]), "rv21": cur, "rv63": vol(r[-63:]), "rv252": vol(r[-252:]),
            "series": series, "closes": [[d, round(m[d], 2)] for d in days[-260:]],
            "pct": round(sum(1 for v in all21 if v <= cur) / len(all21) * 100) if all21 and cur else None}


# --------------------------------------------------------------------------
# storage: the latest chain per reading, and one row per session
# --------------------------------------------------------------------------

def _stored(sym: str) -> dict:
    with store.conn() as c:
        return {r["mode"]: {"asof": r["asof"], "session": r["session"], "chain": json.loads(r["payload"])}
                for r in c.execute("SELECT * FROM opt_chain WHERE sym=?", (sym,))}


def _save_chain(sym: str, mode: str, ch: dict):
    with store.conn() as c:
        c.execute("INSERT OR REPLACE INTO opt_chain (sym,mode,asof,session,payload) VALUES (?,?,?,?,?)",
                  (sym, mode, ch["fetched"], session_of(ch), json.dumps(ch, separators=(",", ":"))))


def _save_day(a: dict, rv21: Optional[float]):
    """One row per session. A reading from quotes replaces one from trades, never the reverse."""
    f = a["flow"]
    with store.conn() as c:
        old = c.execute("SELECT mode FROM opt_day WHERE sym=? AND d=?", (a["sym"], a["session"])).fetchone()
        if old and old["mode"] == "quotes" and a["mode"] != "quotes":
            return
        c.execute("INSERT OR REPLACE INTO opt_day (sym,d,asof,mode,spot,iv30,iv60,iv90,iv180,iv365,"
                  "put25,call25,rv21,pc_vol,pc_oi) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (a["sym"], a["session"], a["asof"], a["mode"], a["spot"],
                   *(a["cm"].get(str(d)) for d in TENORS),
                   a["skew30"]["put"], a["skew30"]["call"], rv21, f.get("pc_vol"), f.get("pc_oi")))


def history(sym: str) -> list[dict]:
    with store.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM opt_day WHERE sym=? ORDER BY d", (sym,))]


# --------------------------------------------------------------------------
# which chain to read now
# --------------------------------------------------------------------------

def ny_open(now: Optional[datetime] = None) -> bool:
    """Regular hours on a weekday. Holidays are caught by the chain having no quotes."""
    t = (now or datetime.now(NY)).astimezone(NY)
    return t.weekday() < 5 and (9, 30) <= (t.hour, t.minute) < (16, 0)


_LOCKS: dict = {}
_GUARD = threading.Lock()


def reading(sym: str, force: bool = False) -> tuple[dict, Optional[list]]:
    """
    (chain, five-minute bars or None) to analyse now: live quotes while New
    York trades; otherwise the quotes saved during the last session if there
    are any, and failing that the last trades.
    """
    with _GUARD:
        lock = _LOCKS.setdefault(sym, threading.Lock())
    with lock:
        st = _stored(sym)
        newest = max(st.values(), key=lambda s: s["asof"], default=None)
        ttl = FRESH_OPEN if ny_open() else FRESH_SHUT
        if newest and not force and time.time() - newest["asof"] < ttl:
            ch = newest["chain"]
        else:
            try:
                ch = fetch_chain(sym)
            except NoOptions:
                raise
            except Exception:
                if not newest:
                    raise
                ch = newest["chain"]
        if live(ch):
            if ch is not (newest or {}).get("chain"):
                _save_chain(sym, "quotes", ch)
            return ch, None
        # No quotes: prefer what was saved while there were, if it is the same session.
        q = st.get("quotes")
        if q and q["session"] == session_of(ch):
            return q["chain"], None
        t = st.get("trades")
        if t and t["chain"].get("bars") and t["session"] == session_of(ch) and ch is t["chain"]:
            return ch, ch["bars"]
        try:
            bars = bars5(sym)
        except Exception:
            bars = []
        ch = {**ch, "bars": bars}
        _save_chain(sym, "trades", ch)
        return ch, bars


# --------------------------------------------------------------------------
# one underlying, for the page
# --------------------------------------------------------------------------

def usym(sym: str) -> str:
    return (sym or "").strip().upper()


def us_line(sym: str) -> Optional[str]:
    """The New York line of an Amsterdam or Frankfurt share, when it has one."""
    if not re.search(r"\.[A-Z]{1,3}$", sym):
        return None
    try:
        from . import earndocs, earnings
        h = earnings.held().get(sym) or {}
        f = earndocs.filer(sym, h.get("name"))
        return f["ticker"] if f and f.get("ticker") and f["ticker"] != sym else None
    except Exception:
        return None


def _pct(x: Optional[float]) -> str:
    return "–" if x is None else f"{x * 100:.0f}%"


def _money_txt(v: float) -> str:
    return f"{v:,.0f}" if v >= 100 else f"{v:,.2f}"


def _say(a: dict, rv: dict, hist: list, pos: Optional[dict]) -> list[str]:
    say = []
    iv30, rv21 = a["cm"].get("30"), rv.get("rv21")
    sym = a["sym"]
    if iv30:
        s = (f"Options price {sym} to move about {iv30 / math.sqrt(252) * 100:.1f}% a day over the next "
             f"month ({iv30 * 100:.0f}% a year).")
        if rv21:
            gap = iv30 - rv21
            s += (f" Over the past month it actually moved {rv21 * 100:.0f}% a year, so options price "
                  + ("about what it has been doing." if abs(gap) < 0.03 else
                     f"{'more' if gap > 0 else 'less'} movement than it has delivered."))
        say.append(s)
    near = next((r for r in a["expiries"] if r["days"] >= 21 and r.get("q")), None) or \
        next((r for r in reversed(a["expiries"]) if r.get("q")), None)
    if near:
        lo, hi = near["q"][1], near["q"][3]
        s = (f"By {date.fromisoformat(near['exp']):%d %B}, prices put two-in-three odds on "
             f"{_money_txt(lo)} to {_money_txt(hi)} ({(lo / a['spot'] - 1) * 100:+.0f}% to "
             f"{(hi / a['spot'] - 1) * 100:+.0f}%).")
        if pos and pos.get("value_eur"):
            v = pos["value_eur"]
            s += (f" On your {v:,.0f} euro position that is about {v * (lo / a['spot'] - 1):+,.0f} to "
                  f"{v * (hi / a['spot'] - 1):+,.0f} euros.")
        say.append(s)
    sk = a["skew30"]
    ref = next((r for r in a["expiries"] if r["days"] >= 21 and r.get("m_p25") and r.get("m_c25")), None)
    if sk.get("gap") is not None and ref:
        dn, up = (1 - ref["m_p25"]) * 100, (ref["m_c25"] - 1) * 100
        if sk["gap"] >= 2:
            say.append(f"Protection costs extra: a put about {dn:.0f}% below the price is priced at "
                       f"{sk['put'] * 100:.0f} volatility, a call about {up:.0f}% above at "
                       f"{sk['call'] * 100:.0f}, a gap of {sk['gap']:.0f} points.")
        elif sk["gap"] <= -2:
            say.append(f"Calls cost more than puts ({sk['call'] * 100:.0f} against {sk['put'] * 100:.0f} "
                       f"volatility): the market is paying up for a rise, which is unusual for a share.")
        else:
            say.append(f"Puts and calls are priced about alike ({sk['put'] * 100:.0f} and "
                       f"{sk['call'] * 100:.0f} volatility): no unusual fear of a fall.")
    far = a["cm"].get("180")
    if iv30 and far:
        d = (iv30 - far) * 100
        if d >= 3:
            e = a.get("earnings")
            inside = e and date.today().isoformat() <= e <= (date.today() + timedelta(days=35)).isoformat()
            say.append(f"Near-dated options are dearer than far-dated ones ({iv30 * 100:.0f} against "
                       f"{far * 100:.0f}): the worry is close by"
                       + (f", and results on {date.fromisoformat(e):%d %B} fall inside the month." if inside else "."))
        elif d <= -3:
            say.append(f"Far-dated options are dearer than near-dated ones ({far * 100:.0f} against "
                       f"{iv30 * 100:.0f}): calm now, more uncertainty further out, which is the usual shape.")
    f = a["flow"]
    if f.get("call_vol") or f.get("put_vol"):
        s = f"{f['call_vol']:,} calls and {f['put_vol']:,} puts traded"
        if f.get("pc_vol") is not None:
            s += f", {f['pc_vol']:.2f} puts per call"
        say.append(s + (" in the last session." if a["mode"] == "trades" else " so far today."))
    ivs = [h["iv30"] for h in hist if h.get("iv30")]
    if len(ivs) >= MIN_HISTORY and iv30:
        rank = round(sum(1 for v in ivs if v <= iv30) / len(ivs) * 100)
        say.append(f"Thirty-day implied volatility is higher than on {rank}% of the {len(ivs)} sessions stored.")
    return say


def build(sym: str, force: bool = False) -> dict:
    sym = usym(sym)
    alt = us_line(sym)
    if alt:
        return {"available": False, "sym": sym, "alt": alt,
                "why": f"Yahoo carries no options for Amsterdam or Frankfurt lines. "
                       f"{sym.split('.')[0]}'s New York line, {alt}, has them."}
    try:
        ch, bars = reading(sym, force)
        a = analysed(ch, bars)
    except NoOptions as e:
        return {"available": False, "sym": sym, "why": str(e)}
    try:
        rv = realised(sym)
    except Exception as e:
        rv = {"error": f"{e.__class__.__name__}: {e}"[:160]}
    try:
        _save_day(a, rv.get("rv21"))
    except Exception:
        pass
    hist = history(sym)
    pos = None
    try:
        from . import earnings
        pos = earnings.position(sym)
    except Exception:
        pass
    _viewed(sym)
    # Levels worth drawing on the range: what you paid, and what you think it is worth.
    levels = []
    if pos and pos.get("as") == sym and pos.get("avg_price"):
        levels.append({"k": "cost", "label": "your average cost", "v": pos["avg_price"]})
    try:
        from . import research
        th = (research.thesis_get(sym) or {}).get("current") or {}
        if th.get("target"):
            levels.append({"k": "target", "label": "your fair value", "v": th["target"]})
    except Exception:
        pass
    a.update({"available": True, "realised": rv, "levels": levels,
              "history": [{k: h.get(k) for k in ("d", "iv30", "iv90", "put25", "call25", "rv21", "mode")}
                          for h in hist],
              "position": pos, "say": _say(a, rv, hist, pos), "min_history": MIN_HISTORY})
    return a


def chain_rows(sym: str, exp: str) -> dict:
    """One expiry as a table: calls, strike, the fitted volatility, puts."""
    sym = usym(sym)
    ch, bars = reading(sym)
    e = next((x for x in ch["exps"] if x["exp"] == exp), None)
    if not e:
        raise NoOptions(f"{sym} has no expiry on {exp}")
    try:
        a = analysed(ch, bars)
    except NoOptions:
        a = {"expiries": [], "points": {}}
    pts = dict((round(m * next((r["fwd"] for r in a["expiries"] if r["exp"] == exp), ch["spot"]), 2), v)
               for m, v in a.get("points", {}).get(exp, []))
    calls, puts = {r[0]: r for r in e["calls"]}, {r[0]: r for r in e["puts"]}
    rows = []
    for k in sorted(set(calls) | set(puts)):
        c, p = calls.get(k), puts.get(k)
        iv = min(pts.items(), key=lambda kv: abs(kv[0] - k), default=(None, None))
        rows.append({"k": k, "iv": iv[1] if iv[0] is not None and abs(iv[0] - k) < 0.011 * max(k, 1) else None,
                     "c": c[1:] if c else None, "p": p[1:] if p else None})
    return {"sym": sym, "exp": exp, "spot": ch["spot"], "rows": rows, "mode": a.get("mode"),
            "asof": ch["fetched"]}


# --------------------------------------------------------------------------
# one contract, worked out
# --------------------------------------------------------------------------

SHARES = 100                # a US share option is on a hundred shares
MOVES = (-0.5, -0.3, -0.2, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2, 0.3, 0.5)


def npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def worth(S: float, K: float, T: float, vol: float, r: float, q: float, call: bool) -> float:
    """What the option is worth, per share, with the share at S and T years to run."""
    if T <= 0 or vol <= 0:
        return max(0.0, S - K) if call else max(0.0, K - S)
    return math.exp(-r * T) * black(S * math.exp((r - q) * T), K, T, vol, call)


def greeks(S: float, K: float, T: float, vol: float, r: float, q: float, call: bool) -> dict:
    """
    How the price moves, per share, for whoever bought it:

      delta   for 1.00 in the share
      gamma   how much delta itself changes for 1.00 in the share
      vega    for one point of implied volatility
      theta   over one calendar day in which nothing else changes
      omega   percent, for one percent in the share

    Theta is the difference between two prices a day apart rather than a
    formula: that is what "a day's decay" means, and it has no sign to get
    wrong.
    """
    s = vol * math.sqrt(T)
    d1 = (math.log(S * math.exp((r - q) * T) / K) + 0.5 * s * s) / s
    carry = math.exp(-q * T)
    price = worth(S, K, T, vol, r, q, call)
    delta = carry * ncdf(d1) if call else -carry * ncdf(-d1)
    day = min(1.0 / YEAR, T)
    return {"delta": delta, "gamma": carry * npdf(d1) / (S * s),
            "vega": S * carry * npdf(d1) * math.sqrt(T) / 100.0,
            "theta": worth(S, K, T - day, vol, r, q, call) - price,
            "omega": delta * S / price if price > 0 else None}


def _below(cdf: list, level: float) -> Optional[float]:
    """The chance of finishing below `level`, from the stored points of an expiry's distribution."""
    if not cdf:
        return None
    if level <= cdf[0][0]:
        return cdf[0][1] * level / cdf[0][0]
    if level >= cdf[-1][0]:
        return cdf[-1][1] + (1.0 - cdf[-1][1]) * (1.0 - cdf[-1][0] / level)
    for (k0, c0), (k1, c1) in zip(cdf, cdf[1:]):
        if level <= k1:
            return c0 + (c1 - c0) * (level - k0) / (k1 - k0)
    return None


def _smile_at(a: dict, exp: str, m: float) -> Optional[float]:
    """The volatility the expiry's own points give at moneyness m, between the strikes that priced."""
    pts = sorted(a.get("points", {}).get(exp) or [])
    if not pts or m < pts[0][0] or m > pts[-1][0]:
        return None
    for (m0, v0), (m1, v1) in zip(pts, pts[1:]):
        if m <= m1:
            return v0 + (v1 - v0) * (m - m0) / (m1 - m0) if m1 > m0 else v0
    return pts[-1][1]


def _usd(v: float) -> str:
    """Money in a sentence. Halves round up, as the page's own figures do: 2.295 is 2.30, not 2.29."""
    from decimal import Decimal, ROUND_HALF_UP
    big = abs(v) >= 1000
    d = Decimal(repr(float(v))).quantize(Decimal("1") if big else Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{d:,.0f}" if big else f"{d:,.2f}"


def contract(sym: str, exp: str, strike: float, call: bool) -> dict:
    """
    One option, for the page that opens when a cell of the chain is clicked.

    Everything is per share unless it says `per contract`; a contract is a
    hundred shares. Figures are for the buyer. The seller's are the same with
    the sign turned round, and `words` says both sides in sentences.

    The price worked from is the middle of bid and ask while there are quotes,
    and the last trade otherwise. The volatility is the one that price itself
    implies, against the forward the chain's own quotes give for that expiry,
    so the model reproduces the price it started from and the "what if"
    figures move away from that, not from a theoretical value.
    """
    sym = usym(sym)
    ch, bars = reading(sym)
    e = next((x for x in ch["exps"] if x["exp"] == exp), None)
    row = e and next((r for r in (e["calls"] if call else e["puts"]) if abs(r[0] - strike) < 1e-6), None)
    kind = "call" if call else "put"
    if not row:
        raise NoOptions(f"{sym} lists no {exp} {strike:g} {kind}")
    try:
        a = analysed(ch, bars)
    except NoOptions:
        a = {"expiries": [], "points": {}, "mode": "quotes" if live(ch) else "trades", "session": session_of(ch)}
    er = next((r for r in a["expiries"] if r["exp"] == exp), None)

    K, bid, ask, last, last_ts, volume, oi = row
    S = ch["spot"]
    T = er["t"] if er else max((_expiry_ts(exp) - ch["fetched"]) / (YEAR * 86400), 1.0 / YEAR / 24)
    r = _rate(T)
    F = er["fwd"] if er else S * math.exp((r - (ch.get("div_yield") or 0.0)) * T)
    q = r - math.log(F / S) / T                 # the carry the forward itself implies
    df = math.exp(-r * T)
    quoted = bid > 0 and ask >= bid
    px = 0.5 * (bid + ask) if quoted else last
    if px <= 0:
        raise NoOptions(f"the {exp} {strike:g} {kind} has no quote and no trade to price it from")

    # `own` is the volatility this price implies; with it the model hands back
    # the price it started from, and only then do "the same price reached today"
    # figures mean anything. How the option MOVES can be read at the smile's
    # volatility when the price itself gives none (a last trade from before the
    # share moved, below what the option is worth exercised).
    own = implied(px / df, F, K, T, call)
    fitted = _smile_at(a, exp, K / F)
    vol = own or fitted or (er or {}).get("atm")
    out = {"sym": sym, "name": ch.get("name"), "currency": ch.get("currency") or "USD", "exp": exp,
           "strike": K, "type": "C" if call else "P", "kind": kind, "shares": SHARES,
           "spot": S, "forward": round(F, 2), "days": round(T * YEAR, 1), "t": round(T, 5),
           "rate": round(r, 4), "mode": a.get("mode"), "asof": ch["fetched"], "session": a.get("session"),
           "bid": bid or None, "ask": ask or None, "last": last or None, "last_ts": last_ts or None,
           "volume": volume, "oi": oi, "price": round(px, 4), "price_from": "mid" if quoted else "last",
           "spread": round(ask - bid, 4) if quoted else None,
           "spread_pct": round((ask - bid) / px * 100, 2) if quoted else None,
           "iv": round(own, 4) if own else None, "iv_fitted": round(fitted, 4) if fitted else None,
           "iv_atm": (er or {}).get("atm"), "iv_used": round(vol, 4) if vol else None,
           "expiry_move_pct": (er or {}).get("move"), "earnings": a.get("earnings")}

    intrinsic = max(0.0, S - K) if call else max(0.0, K - S)
    be = K + px if call else K - px
    out.update({
        "premium": round(px * SHARES, 2), "intrinsic": round(intrinsic, 4),
        "time_value": round(px - intrinsic, 4),
        "moneyness_pct": round((K / S - 1.0) * 100, 2),
        "in_the_money": intrinsic > 0,
        "breakeven": round(be, 2), "breakeven_pct": round((be / S - 1.0) * 100, 2),
        "max_loss": round(px * SHARES, 2),
        "max_gain": None if call else round((K - px) * SHARES, 2),      # a call has none
        "double_at": round(K + 2 * px if call else K - 2 * px, 2),
        "notional": round(S * SHARES, 2), "gearing": round(S / px, 2),
        "stale": px < intrinsic - 0.005,
    })

    # the odds the chain itself puts on it
    cdf = (er or {}).get("cdf")
    lo_k, lo_be = _below(cdf, K), _below(cdf, be)
    if lo_k is None and vol:
        s = vol * math.sqrt(T)
        lo_k = ncdf(-(math.log(F / K) - 0.5 * s * s) / s)
        lo_be = ncdf(-(math.log(F / be) - 0.5 * s * s) / s) if be > 0 else 0.0
    if lo_k is not None:
        out["p_itm"] = round(1.0 - lo_k if call else lo_k, 4)
        out["p_profit"] = round(1.0 - lo_be if call else lo_be, 4)
        out["odds_from"] = "chain" if cdf else "model"
    if er and er.get("q"):
        out["range"] = er["q"]

    if vol:
        g = greeks(S, K, T, vol, r, q, call)
        out["greeks"] = {k: (round(v, 5) if v is not None else None) for k, v in g.items()}
        s = vol * math.sqrt(T)
        out["breakeven_sd"] = round(abs(math.log(be / F)) / s, 2) if be > 0 else None
    if own:
        out["model"] = round(worth(S, K, T, own, r, q, call), 4)

    # The payoff on the last day needs no model: it is the distance from the strike.
    def at(share: float) -> dict:
        end = max(0.0, share - K) if call else max(0.0, K - share)
        row = {"s": round(share, 2), "move_pct": round((share / S - 1.0) * 100, 1) + 0.0,
               "end": round(end, 4), "pl_end": round(end - px, 4)}
        if own:
            row.update(now=round(worth(share, K, T, own, r, q, call), 4),
                       half=round(worth(share, K, T / 2, own, r, q, call), 4))
        return row
    marks = {round(S * (1 + m), 2): None for m in MOVES}
    marks[round(K, 2)] = "strike"
    if be > 0:
        marks[round(be, 2)] = "break-even"
    out["scenarios"] = [{**at(x), "mark": marks[x]} for x in sorted(marks) if x > 0]
    # the curve for the picture: wide enough for the strike, break-even and the range priced
    lo = min(0.6 * S, 0.9 * min(K, max(be, 0.01)), (er["q"][0] * 0.97) if er and er.get("q") else S)
    hi = max(1.4 * S, 1.1 * max(K, be), (er["q"][4] * 1.03) if er and er.get("q") else S)
    xs = [max(lo, 0.01) + (hi - max(lo, 0.01)) * i / 90 for i in range(91)]
    out["curve"] = {"x": [round(x, 2) for x in xs],
                    "end": [round((max(0.0, x - K) if call else max(0.0, K - x)) - px, 4) for x in xs]}
    if own:
        out["curve"]["now"] = [round(worth(x, K, T, own, r, q, call) - px, 4) for x in xs]
        out["curve"]["half"] = [round(worth(x, K, T / 2, own, r, q, call) - px, 4) for x in xs]
        out["half_date"] = (date.fromtimestamp(ch["fetched"]) + timedelta(days=T * YEAR / 2)).isoformat()

    # euros, and what it is next to what you hold
    cur = out["currency"]
    if cur != "EUR":
        try:
            from . import pricing
            fx = pricing.fx_on(pricing.get_fx("EUR" + cur, (date.today() - timedelta(days=14)).isoformat()),
                               date.today().isoformat(), 0.0)
            if fx:
                out["fx"] = round(fx, 4)
                out["premium_eur"] = round(px * SHARES / fx, 2)
                out["notional_eur"] = round(S * SHARES / fx, 2)
        except Exception:
            pass
    try:
        from . import earnings
        pos = earnings.position(sym)
        if pos and pos.get("qty"):
            out["position"] = {"qty": pos["qty"], "value_eur": pos.get("value_eur"),
                               "weight": pos.get("weight"), "as": pos.get("as")}
    except Exception:
        pass
    out["words"] = {"bought": _contract_words(out, False), "sold": _contract_words(out, True)}
    return out


def _contract_words(c: dict, sold: bool) -> dict:
    """
    The contract in sentences, for the buyer or for the seller. `what` is the
    one-line definition; `lines` are [heading, sentence] pairs.
    """
    sym, call, K, px, n = c["sym"], c["type"] == "C", c["strike"], c["price"], c["shares"]
    ccy = "$" if c["currency"] == "USD" else c["currency"] + " "
    exp = date.fromisoformat(c["exp"])
    until = f"{exp.day} {exp:%B %Y}"
    prem, m = f"{ccy}{_usd(c['premium'])}", f"{ccy}{_usd(px)}"
    eur = f" (about € {c['premium_eur']:,.0f})" if c.get("premium_eur") else ""
    kind = c["kind"]
    if not sold:
        what = (f"The right, not the duty, to {'buy' if call else 'sell'} {n} {sym} at {ccy}{_usd(K)} a share on any "
                f"day up to {until}. It costs {m} a share, {prem} for the contract{eur}, paid now.")
    else:
        what = (f"You are paid {prem}{eur} now. In return you must "
                + (f"deliver {n} {sym} at {ccy}{_usd(K)} a share" if call else
                   f"buy {n} {sym} at {ccy}{_usd(K)} a share")
                + f" if the holder asks, on any day up to {until}.")
    lines = []
    be, be_pct = c["breakeven"], c["breakeven_pct"]
    way = "rise" if be_pct > 0 else "fall"
    side = "above" if call else "below"
    if not sold:
        lines.append(["At expiry", (
            f"Worth nothing if {sym} is {'at or below' if call else 'at or above'} {ccy}{_usd(K)}: you lose the "
            f"{prem} and no more. {side.capitalize()} that it is worth the distance from the strike, so you are "
            f"in profit {side} {ccy}{_usd(be)}, which needs {sym} to {way} {abs(be_pct):.1f}% from here."
            + ("" if call else f" The most it can pay is {ccy}{_usd(c['max_gain'])}, with the share at zero."))])
    else:
        lines.append(["At expiry", (
            f"You keep all {prem} if {sym} is {'at or below' if call else 'at or above'} {ccy}{_usd(K)}. "
            f"{side.capitalize()} that you pay the distance from the strike: you are out of pocket {side} "
            f"{ccy}{_usd(be)}, "
            + ("and there is no limit to the loss unless you own the shares." if call else
               f"and the most you can lose is {ccy}{_usd(c['max_gain'])}, with the share at zero."))])
    if c.get("p_itm") is not None:
        p_in, p_win = c["p_itm"], (1 - c["p_profit"]) if sold else c["p_profit"]
        src = "The chain's own prices put" if c.get("odds_from") == "chain" else "At this volatility the model puts"
        lines.append(["The odds priced", (
            f"{src} {p_in * 100:.0f}% on it finishing in the money and {p_win * 100:.0f}% on "
            f"{'you keeping a profit' if sold else 'it making money'} at expiry. These lean pessimistic about the "
            f"share: a yardstick, not a forecast.")])
    tv, iv = c["time_value"], c["intrinsic"]
    if c.get("stale"):
        lines.append(["A stale price", (
            f"The {'last trade' if c['price_from'] == 'last' else 'quote'}, {m}, is below the {ccy}{_usd(iv)} this option "
            f"would fetch if exercised today: {sym} has moved since. Read what follows as what that old price "
            f"implied, not as what the option costs now.")])
    elif iv > 0:
        lines.append(["What the price is made of", (
            f"{ccy}{_usd(iv)} of the {m} is value it already has: {sym} is {abs(c['moneyness_pct']):.1f}% "
            f"{'above' if call else 'below'} the strike. The other {ccy}{_usd(tv)} is paid for time, and is gone by expiry.")])
    else:
        lines.append(["What the price is made of", (
            f"All of the {m} is paid for time: {sym} is {abs(c['moneyness_pct']):.1f}% "
            f"{'below' if call else 'above'} the strike, so exercised today it would be worth nothing. "
            f"That whole amount is gone by expiry unless the share moves.")])
    g = c.get("greeks")
    if g:
        d, sgn = g["delta"], (-1 if sold else 1)
        lines.append(["With the share", (
            f"Delta {d * sgn:+.2f}: for each {ccy}1 {sym} {'rises' if (d * sgn) > 0 else 'falls'}, "
            f"{'your side of ' if sold else ''}the contract gains about {ccy}{_usd(abs(d) * n)}. "
            f"Today it moves like {'owning' if (d * sgn) > 0 else 'being short'} {abs(d) * n:.0f} shares"
            + (f", and about {abs(g['omega']):.1f}% for each 1% in the share" if g.get("omega") else "")
            + f". That is not fixed: a {ccy}1 move changes the delta by {g['gamma']:.3f} (gamma), so it "
            f"{'bites harder against you' if sold else 'grows'} as the share moves "
            f"{'up' if call else 'down'}.")])
        th = g["theta"] * n
        lines.append(["With time", (
            f"Each day that passes with nothing else changing takes about {ccy}{_usd(abs(th))} off the contract "
            f"(theta), {'which you earn' if sold else 'which you pay'}. "
            f"That is {abs(th) / c['premium'] * 100:.1f}% of the price a day now, and it speeds up in the last weeks.")])
        if c.get("iv_used"):
            s = (f"It is priced at {c['iv_used'] * 100:.1f}% a year"
                 + (f", against {c['iv_atm'] * 100:.1f}% at the money for this expiry" if c.get("iv_atm") else "")
                 + f". One point more of implied volatility {'costs you' if sold else 'adds'} about "
                 f"{ccy}{_usd(g['vega'] * n)} a contract (vega), one point less "
                 f"{'gives it back' if sold else 'takes it off'}. Volatility usually drops the day after results.")
            lines.append(["With volatility", s])
    if c.get("breakeven_sd") is not None and c.get("expiry_move_pct"):
        lines.append(["How far that is", (
            f"Break-even is {abs(be_pct):.1f}% away. Options price a move of about ±{c['expiry_move_pct']:.0f}% by "
            f"{exp.day} {exp:%B}, so that is {c['breakeven_sd']:.1f} times the move priced."
            + (f" Doubling the money needs {sym} at {ccy}{_usd(c['double_at'])}." if not sold and c["double_at"] > 0 else ""))])
    if c.get("spread") is not None:
        lines.append(["Dealing", (
            f"Bid {ccy}{_usd(c['bid'])}, ask {ccy}{_usd(c['ask'])}: the gap is {c['spread_pct']:.1f}% of the price, "
            f"{ccy}{_usd(c['spread'] * n)} a contract lost between buying and selling straight back. "
            f"{c['volume']:,} traded this session, {c['oi']:,} open.")])
    else:
        lines.append(["Dealing", (
            f"No bid or ask now, so the price is the last trade, {m}. "
            f"{c['volume']:,} traded in that session, {c['oi']:,} open.")])
    note = (f"A contract covers {n} shares: {ccy}{_usd(c['notional'])} of {sym}"
            + (f" (about € {c['notional_eur']:,.0f})" if c.get("notional_eur") else "") + " at today's price")
    pos = c.get("position")
    if pos:
        note += (f". You hold {pos['qty']:g}"
                 + (f", worth € {pos['value_eur']:,.0f}" if pos.get("value_eur") else "")
                 + (f": one contract is {n / pos['qty']:.0f} times your position" if pos["qty"] < n else
                    f": enough to cover {int(pos['qty'] // n)} contract{'s' if pos['qty'] // n != 1 else ''}"))
    if not sold:
        note += (f". The option gives you the move in those shares for {prem} and cannot lose more than it cost. "
                 f"The shares do not expire; the option does.")
    else:
        note += ("." + (" Sold without owning the shares, a call's loss has no ceiling." if call else
                        f" If the shares are handed to you, you pay {ccy}{_usd(K * n)} for them."))
    lines.append(["Next to the shares", note])
    if c.get("earnings") and date.today().isoformat() <= c["earnings"] <= c["exp"]:
        e = date.fromisoformat(c["earnings"])
        lines.append(["Results", f"Results on {e.day} {e:%B} fall before it expires, so its price carries that day's move."])
    return {"what": what, "lines": lines}


# --------------------------------------------------------------------------
# the book: every holding that has a chain
# --------------------------------------------------------------------------

def _viewed(sym: str):
    try:
        v = json.loads(store.get_setting("opt_viewed") or "{}")
        v[sym] = int(time.time())
        cut = time.time() - 30 * 86400
        store.set_setting("opt_viewed", json.dumps({k: t for k, t in v.items() if t > cut}))
    except Exception:
        pass


def universe() -> list[dict]:
    """Holdings with a New York line, the shares your turbos track, and what you looked at lately."""
    out, seen = [], set()
    try:
        from . import service
        from .morning import brands
        d = service.dashboard("SP500", light=True)
    except Exception:
        d, brands = {}, (lambda t, n: [])
    for r in d.get("positions") or []:
        if not r.get("qty"):
            continue
        if r.get("kind") == "DERIVATIVE":
            t, how = r.get("underlying_ticker"), "turbo"
        else:
            t, how = r.get("ticker"), "held"
        if not t or "." in t or t in seen:
            continue
        seen.add(t)
        name = r.get("underlying") if how == "turbo" else r.get("name")
        out.append({"sym": t, "how": how, "name": (brands(t, name or "") or [name or t])[0],
                    "weight": r.get("weight") if how == "held" else None,
                    "value_eur": r.get("value_eur") if how == "held" else None})
    try:
        for t in json.loads(store.get_setting("opt_viewed") or "{}"):
            if t not in seen and "." not in t:
                seen.add(t)
                out.append({"sym": t, "how": "viewed", "name": t, "weight": None, "value_eur": None})
    except Exception:
        pass
    return out


def overview() -> dict:
    uni = universe()
    with store.conn() as c:
        last = {}
        for r in c.execute("SELECT * FROM opt_day ORDER BY d"):
            last.setdefault(r["sym"], []).append(dict(r))
    rows = []
    for u in uni:
        h = last.get(u["sym"]) or []
        row = {**u, "n": len(h)}
        if h:
            cur = h[-1]
            ivs = [x["iv30"] for x in h if x.get("iv30")]
            row.update({k: cur.get(k) for k in ("d", "mode", "spot", "iv30", "iv90", "iv180", "put25",
                                                "call25", "rv21", "pc_vol", "asof")})
            if cur.get("iv30") and cur.get("rv21"):
                row["gap"] = round((cur["iv30"] - cur["rv21"]) * 100, 1)
            if cur.get("put25") and cur.get("call25"):
                row["skew"] = round((cur["put25"] - cur["call25"]) * 100, 1)
            if cur.get("iv30") and cur.get("iv180"):
                row["slope"] = round((cur["iv180"] - cur["iv30"]) * 100, 1)
            if len(ivs) >= MIN_HISTORY and cur.get("iv30"):
                row["rank"] = round(sum(1 for v in ivs if v <= cur["iv30"]) / len(ivs) * 100)
            if cur.get("iv30"):
                row["week"] = round(cur["iv30"] * math.sqrt(5 / 252) * 100, 1)
        rows.append(row)
    read = [r for r in rows if r.get("iv30")]
    say = []
    if read:
        hi = max(read, key=lambda r: r["iv30"])
        lo = min(read, key=lambda r: r["iv30"])
        say.append(f"The widest moves are priced for {hi['sym']} ({hi['iv30'] * 100:.0f}% a year), the "
                   f"narrowest for {lo['sym']} ({lo['iv30'] * 100:.0f}%).")
        rich = sorted((r for r in read if r.get("gap") is not None), key=lambda r: -r["gap"])
        if rich and rich[0]["gap"] >= 5:
            say.append("Options price much more movement than the share has delivered for "
                       + ", ".join(f"{r['sym']} (+{r['gap']:.0f})" for r in rich[:4] if r["gap"] >= 5) + ".")
        if rich and rich[-1]["gap"] <= -5:
            say.append("Less than it has delivered for "
                       + ", ".join(f"{r['sym']} ({r['gap']:.0f})" for r in rich[::-1][:4] if r["gap"] <= -5) + ".")
        sk = sorted((r for r in read if r.get("skew") is not None), key=lambda r: -r["skew"])
        if sk:
            say.append(f"Protection against a fall is dearest for {sk[0]['sym']} "
                       f"(puts {sk[0]['skew']:.0f} points over calls)"
                       + (f" and cheapest for {sk[-1]['sym']} ({sk[-1]['skew']:.0f})." if len(sk) > 1 else "."))
        inv = [r["sym"] for r in read if r.get("slope") is not None and r["slope"] <= -3]
        if inv:
            say.append("Near-dated options are dearer than far-dated ones for " + ", ".join(inv[:8])
                       + ": something close by is being priced.")
    return {"rows": rows, "say": say, "collector": status(), "min_history": MIN_HISTORY,
            "open": ny_open(), "fetched_at": time.time()}


# --------------------------------------------------------------------------
# the collector: one reading per holding per session
# --------------------------------------------------------------------------

_JOB: dict = {"state": "idle", "done": 0, "total": 0, "started": None, "finished": None,
              "errors": [], "last_session": None}
_THREAD: dict = {"t": None, "last_tick": 0.0}


def status() -> dict:
    return {k: (v[-5:] if k == "errors" else v) for k, v in _JOB.items()}


def collect(symbols: Optional[list] = None) -> dict:
    """Read every chain once, a second apart, storing the chain and the day's row."""
    if _JOB["state"] == "running":
        return status()
    syms = symbols or [u["sym"] for u in universe()]
    _JOB.update(state="running", done=0, total=len(syms), started=time.time(), finished=None, errors=[])
    opened = ny_open()

    def run():
        quoted = 0
        for s in syms:
            try:
                a = build(s, force=True)
                if a.get("available"):
                    _JOB["last_session"] = a["session"]
                    quoted += a["mode"] == "quotes"
                elif a.get("why"):
                    _JOB["errors"].append(f"{s}: {a['why']}"[:160])
            except Exception as e:
                _JOB["errors"].append(f"{s}: {e.__class__.__name__}: {e}"[:160])
            _JOB["done"] += 1
            time.sleep(1.0)
        # The clock said open and nothing carried a quote: a holiday. Do not
        # ask again today.
        if opened and syms and not quoted:
            _JOB["no_session"] = datetime.now(NY).date().isoformat()
        _JOB.update(state="idle", finished=time.time())
    threading.Thread(target=run, name="opt-collect", daemon=True).start()
    return status()


def _due() -> bool:
    """
    Once per session while New York is open, and again in its last hour so
    the stored reading is near the close; once after a session nobody read.
    """
    syms = [u["sym"] for u in universe()]
    if not syms:
        return False
    now = datetime.now(NY)
    with store.conn() as c:
        rows = {r["sym"]: dict(r) for r in c.execute(
            "SELECT sym, MAX(d) d, MAX(asof) asof FROM opt_day GROUP BY sym")}
    today = now.date().isoformat()
    if ny_open(now) and _JOB.get("no_session") != today:
        if (now.hour, now.minute) < (10, 0):
            return False
        late = now.hour >= 15
        for s in syms:
            r = rows.get(s)
            if not r or r["d"] != today:
                return True
            if late and datetime.fromtimestamp(r["asof"], tz=NY).hour < 15:
                return True
        return False
    # Shut: make sure the last finished session has a row for everything.
    d = now.date() if (now.weekday() < 5 and now.hour >= 16) else None
    if d is None:
        d = now.date() - timedelta(days=1)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
    return any((rows.get(s) or {}).get("d", "") < d.isoformat() for s in syms) \
        and time.time() - (_JOB.get("finished") or 0) > 6 * 3600


def _loop():
    while True:
        _THREAD["last_tick"] = time.time()
        try:
            if _JOB["state"] != "running" and _due():
                collect()
        except Exception as e:
            _JOB["errors"].append(f"tick: {e.__class__.__name__}: {e}"[:160])
        time.sleep(120)


def ensure():
    t = _THREAD["t"]
    if t and t.is_alive():
        return
    store.init_db()
    t = threading.Thread(target=_loop, name="opt-loop", daemon=True)
    _THREAD["t"] = t
    t.start()
