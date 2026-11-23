"""
Pricing leveraged products from their own product name + the underlying share.

Nothing free quotes a SocGen turbo, but the name is not decoration — it carries
the entire contract. Three families, three different answers:

TURBO / knockout   "SG NVIDIA BEST TURBO LONG SL 103.448 FN 103.448 R 5 LV 3.33"
    SL = stop-loss (knockout barrier), FN/STR = financing level, R = ratio,
    LV = the leverage on the day the name was written.
        long:  price = (S - FN) / R / fx
        short: price = (FN - S) / R / fx
    with S and FN in the share's currency and fx that currency per euro (the
    turbo trades in euros). The intrinsic value, exact on the day the name's
    FN is valid; SG moves FN daily by the cost of funding, which FinPath
    tracks from dated levels. Verified against Rick's own equity
    trades in the same names: where his share trade sits within days of the
    turbo trade, the implied underlying lands within ~2%.

FACTOR certificate "SG ADVANCED MICRO DEV. FACTOR (MULTI) LONG LEV 5"
    Constant leverage, RESET DAILY. So it is NOT
        price x (1 + L x total_return)
    which is the intuitive formula and is wrong over more than one day. It is
        V_t = V_(t-1) x (1 + L x r_t)
    compounded day by day, which is path-dependent. A 5x certificate on a share
    that rises 10% then falls 9.09% (back to flat) does not come back to flat —
    it loses about 4.5%. That decay is the whole reason these bleed sideways.

WARRANT            "SG COINBASE WARRANT (STANDARD) CALL 365 R 50 18/09/2026"
    An option. Needs implied volatility, which we do not have. No model —
    these fall back to a manual price.

Everything here self-validates: the model reprices the certificate on the day
it was actually traded and compares with what was actually paid. If that does
not reconcile, the model is marked untrusted and the position falls back to the
last trade price rather than showing a confident wrong number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from datetime import date
from typing import Optional

# Turbo financing levels creep upward for longs (and downward for shorts) as
# the issuer charges overnight funding — very roughly 4-6%/yr at current rates.
# Left at 0.0 by default: an unverifiable parameter that quietly moves every
# number is worse than a documented, named limitation.
FINANCING_DRIFT_PA = 0.0

_NUM = r'([\d]+(?:[.,]\d+)?)'


def _n(s: Optional[str]) -> Optional[float]:
    return float(s.replace(",", ".")) if s else None


def _find(pattern: str, text: str) -> Optional[float]:
    m = re.search(pattern, text, re.I)
    return _n(m.group(1)) if m else None


@dataclass
class Spec:
    family: str                      # TURBO | FACTOR | WARRANT | UNKNOWN
    direction: str = "LONG"          # LONG | SHORT
    financing: Optional[float] = None   # FN or STR — the level the price is struck off
    barrier: Optional[float] = None     # SL — knockout
    ratio: Optional[float] = None       # R
    leverage: Optional[float] = None    # LEV n, factor certificates only
    strike: Optional[float] = None      # warrants
    expiry: Optional[str] = None
    name_leverage: Optional[float] = None   # LV n in a turbo's name: its leverage the day the name was written

    @property
    def share_when_named(self) -> Optional[float]:
        """The share price the name's LV implies: L = S / (S - FN), so S = FN x L / (L - 1)."""
        if self.family != "TURBO" or not self.name_leverage or self.financing is None or self.name_leverage <= 1:
            return None
        L = self.name_leverage
        return self.financing * L / (L - 1) if self.direction == "LONG" else self.financing * L / (L + 1)

    @property
    def priceable(self) -> bool:
        if self.family == "TURBO":
            return self.financing is not None and self.ratio
        if self.family == "FACTOR":
            return bool(self.leverage)
        return False

    def as_dict(self):
        return asdict(self)


def parse(name: str) -> Spec:
    """Read the contract out of the product name."""
    n = (name or "").upper()
    direction = "SHORT" if re.search(r'\b(SHORT|PUT)\b', n) else "LONG"

    if "WARRANT" in n:
        m = re.search(r'\b(?:CALL|PUT)\s+' + _NUM, n)
        return Spec(family="WARRANT", direction=direction,
                    strike=_n(m.group(1)) if m else None,
                    ratio=_find(r'\bR\s+' + _NUM, n),
                    expiry=(re.search(r'(\d{2}/\d{2}/\d{4})', n) or [None])[0]
                    if re.search(r'(\d{2}/\d{2}/\d{4})', n) else None)

    if "FACTOR" in n:
        lev = _find(r'\bLEV\s+' + _NUM, n)
        if lev is None:                      # "FACTOR (MULTI) 5 LONG R 1"
            lev = _find(r'FACTOR\s*\(MULTI\)\s+' + _NUM, n)
        return Spec(family="FACTOR", direction=direction, leverage=lev,
                    ratio=_find(r'\bR\s+' + _NUM, n))

    if "TURBO" in n or re.search(r'\b(FN|STR)\s', n):
        fin = _find(r'\bFN\s+' + _NUM, n)
        if fin is None:
            fin = _find(r'\bSTR\s+' + _NUM, n)
        return Spec(family="TURBO", direction=direction, financing=fin,
                    barrier=_find(r'\bSL\s+' + _NUM, n),
                    ratio=_find(r'\bR\s+' + _NUM, n),
                    name_leverage=_find(r'\bLV\s+' + _NUM, n))

    return Spec(family="UNKNOWN", direction=direction)


# --------------------------------------------------------------------------
# turbo
# --------------------------------------------------------------------------

def turbo_price(spec: Spec, underlying: float, ratio_mode: str = "div",
                days_held: float = 0.0, fx: float = 1.0,
                financing: Optional[float] = None) -> Optional[float]:
    """
    Intrinsic value of a turbo, in the currency it trades in. Never negative:
    a knocked-out turbo is 0.

    The share price and the financing level are both in the SHARE's currency,
    so the gap is converted once, at the end: (S - FN) / R / fx, with fx the
    share's currency per euro (EUR/USD 1.12 for a US share). Until build 33
    the model converted the share price and then subtracted a dollar financing
    level from it, and when it trusted itself it did not convert at all: the
    SNDK turbo read EUR 10.32 on 15 October 2026 where (S - FN) / R / fx gives
    EUR 9.20.

    `financing` overrides the level in the name: the name's is the level on
    one day, and SG moves it every day (FinPath).
    """
    fin = spec.financing if financing is None else financing
    if fin is None or not spec.ratio or underlying is None or not fx:
        return None
    if FINANCING_DRIFT_PA and days_held:
        drift = (1.0 + FINANCING_DRIFT_PA) ** (days_held / 365.0)
        fin = fin * drift if spec.direction == "LONG" else fin / drift
    gap = (underlying - fin) if spec.direction == "LONG" else (fin - underlying)
    v = gap / spec.ratio if ratio_mode == "div" else gap * spec.ratio
    return max(0.0, v / fx)


def implied_underlying(spec: Spec, price: float, ratio_mode: str = "div",
                       fx: float = 1.0, financing: Optional[float] = None) -> Optional[float]:
    """Invert the turbo formula: what was the share worth when this cost `price`?"""
    fin = spec.financing if financing is None else financing
    if fin is None or not spec.ratio or price is None:
        return None
    gap = price * fx * spec.ratio if ratio_mode == "div" else price * fx / spec.ratio
    return fin + gap if spec.direction == "LONG" else fin - gap


def implied_financing(spec: Spec, price: float, underlying: float, fx: float = 1.0) -> Optional[float]:
    """The financing level a traded price implies, given the share at that moment."""
    if not spec.ratio or price is None or underlying is None:
        return None
    gap = price * fx * spec.ratio
    return underlying - gap if spec.direction == "LONG" else underlying + gap


def knockout_distance(spec: Spec, underlying: float, barrier: Optional[float] = None) -> Optional[float]:
    """How far the share can move before the certificate dies, in percent."""
    b = spec.barrier if barrier is None else barrier
    if b is None or not underlying:
        return None
    if spec.direction == "LONG":
        return (underlying - b) / underlying * 100.0
    return (b - underlying) / underlying * 100.0


def effective_leverage(spec: Spec, underlying: float, financing: Optional[float] = None) -> Optional[float]:
    """
    Real leverage of a turbo right now — not a constant, it rises as the share
    approaches the barrier. L = S / (S - FN).
    """
    fin = spec.financing if financing is None else financing
    if fin is None or not underlying:
        return None
    gap = (underlying - fin) if spec.direction == "LONG" else (fin - underlying)
    if gap <= 0:
        return None
    return underlying / gap


# --------------------------------------------------------------------------
# which currency the share is in, and the share at the moment of a trade
# --------------------------------------------------------------------------

# The financing level is in the share's currency; the turbo trades in euros.
CCY_OF_EXCHANGE = {"US": "USD", "EURONEXT": "EUR", "LISBON": "EUR", "DUBLIN": "EUR", "XETRA": "EUR",
                   "GERMANY": "EUR", "MILAN": "EUR", "MADRID": "EUR", "VIENNA": "EUR", "HELSINKI": "EUR",
                   "LSE": "GBP", "SIX": "CHF", "COPENHAGEN": "DKK", "STOCKHOLM": "SEK", "OSLO": "NOK",
                   "TORONTO": "CAD", "TOKYO": "JPY", "HONGKONG": "HKD"}


def underlying_ccy(ticker: Optional[str]) -> Optional[str]:
    """The share's currency, from where it trades; None when that is not known."""
    from . import sessions
    return CCY_OF_EXCHANGE.get(sessions.exchange_of(ticker) or "")


def underlying_at(closes: dict, when: str, ex: Optional[str] = None) -> Optional[float]:
    """
    The share's price as best the daily closes can say at the moment of a
    trade. DEGIRO writes the time in Amsterdam: a US turbo bought at 12:30,
    before New York opens, was priced off the previous close, not that day's.
    A date without a time, or an exchange this does not know, takes that
    day's close.
    """
    d = when[:10]
    if len(when) > 10 and ex:
        from . import sessions
        from zoneinfo import ZoneInfo
        from datetime import datetime as _dt
        tz = ZoneInfo(sessions.EXCHANGES[ex][0])
        t = _dt.fromisoformat(when[:19]).replace(tzinfo=ZoneInfo("Europe/Amsterdam")).astimezone(tz)
        local = t.date()
        s = sessions.session(ex, local)
        if s is None or t < s[0]:
            earlier = [k for k in closes if k < local.isoformat()]
            return closes[max(earlier)] if earlier else None
        d = local.isoformat()
    return _close_on(closes, d)


# --------------------------------------------------------------------------
# the financing level over time
# --------------------------------------------------------------------------

class FinPath:
    """
    The financing level over time, from dated points: DEGIRO's product name
    in an export (dated by the import), and what you type in from DEGIRO's
    product page or its current name.

    SG moves the level every day by the cost of funding. On the Abbott BEST
    turbo it went from 73.6477 in the export of 8 September 2026 to 73.9439 on
    15 October: about 4% a year. Between two points the level is interpolated
    (so a dividend adjustment between them is in it); beyond the last it moves
    at the rate of the two latest points at least two weeks apart; with one
    point it stands still. The stop moves with it, keeping its distance in
    proportion: a BEST turbo's stop IS its financing level, a classic's is
    reset to the same margin above it each month.
    """
    MIN_GAP = 14
    MAX_RATE = 0.25

    def __init__(self, points):
        by = {}
        for p in points:
            d, fn = p[0], p[1]
            if d and fn and fn > 0:
                by[d[:10]] = (d[:10], float(fn), (float(p[2]) if len(p) > 2 and p[2] else None),
                              (p[3] if len(p) > 3 else ""))
        self.pts = [by[k] for k in sorted(by)]
        self.rate = 0.0
        if len(self.pts) >= 2:
            b = self.pts[-1]
            for a in reversed(self.pts[:-1]):
                days = (date.fromisoformat(b[0]) - date.fromisoformat(a[0])).days
                if days >= self.MIN_GAP:
                    r = (b[1] / a[1]) ** (365.0 / days) - 1.0
                    self.rate = max(-self.MAX_RATE, min(self.MAX_RATE, r))
                    break

    def at(self, d: str):
        """(financing level, stop, the point it came from) on a date; None without points."""
        if not self.pts:
            return None
        d = d[:10]
        before = [p for p in self.pts if p[0] <= d]
        after = [p for p in self.pts if p[0] > d]
        if before and after:
            a, b = before[-1], after[0]
            span = (date.fromisoformat(b[0]) - date.fromisoformat(a[0])).days
            frac = (date.fromisoformat(d) - date.fromisoformat(a[0])).days / span if span else 0.0
            fn, src = a[1] * (b[1] / a[1]) ** frac, a
        elif before:
            a = before[-1]
            fn, src = a[1] * (1.0 + self.rate) ** ((date.fromisoformat(d) - date.fromisoformat(a[0])).days / 365.0), a
        else:
            b = after[0]
            fn, src = b[1] / (1.0 + self.rate) ** ((date.fromisoformat(b[0]) - date.fromisoformat(d)).days / 365.0), b
        sl = fn * (src[2] / src[1]) if src[2] else None
        return fn, sl, src


# --------------------------------------------------------------------------
# factor certificate
# --------------------------------------------------------------------------

def factor_price(spec: Spec, anchor_price: float, anchor_date: str,
                 closes: dict) -> Optional[float]:
    """
    Roll a daily-reset factor certificate forward from a known price.

    `closes` is {YYYY-MM-DD: close} for the underlying. Compounding daily is
    the point: applying the leverage to the whole-period return instead would
    ignore the volatility decay these products are famous for.
    """
    if not spec.leverage or anchor_price is None or not closes:
        return None
    days = sorted(d for d in closes if d >= anchor_date)
    if len(days) < 2:
        return None
    v = float(anchor_price)
    prev = closes[days[0]]
    for d in days[1:]:
        c = closes[d]
        if not prev:
            prev = c
            continue
        r = c / prev - 1.0
        v *= (1.0 + spec.leverage * r)
        if v <= 0:                    # a factor certificate cannot go negative
            return 0.0
        prev = c
    return v


# --------------------------------------------------------------------------
# model selection + self-validation
# --------------------------------------------------------------------------

@dataclass
class Model:
    ok: bool
    family: str
    ratio_mode: str = "div"
    fx_applied: bool = False
    error_pct: Optional[float] = None      # worst repricing error at a real trade
    checks: int = 0
    reason: str = ""

    def as_dict(self):
        return asdict(self)


# A turbo's leverage is S/(S-FN). Real retail turbos run maybe 2x-20x; by the
# time it reaches 50x it is within a whisker of its barrier and would have
# knocked out. Anything past that is not a leverage figure, it is a wrong model.
MAX_PLAUSIBLE_LEVERAGE = 50.0


def calibrate_turbo(spec: Spec, observations: list, closes: dict,
                    fx: dict | None = None, tolerance: float = 12.0,
                    fin_at=None, ex: Optional[str] = None) -> Model:
    """
    observations: [(when, price_paid)] — the certificate's real traded prices,
                  `when` a date or DEGIRO's Amsterdam date and time
    closes:       {date_str: underlying close}
    fx:           {date_str: the share's currency per euro}, or None when the
                  share is in euros. Decided by where the share trades, not by
                  which fits better: with one trade both would "fit".
    fin_at:       date -> financing level that day (FinPath), else the name's
    ex:           the share's exchange, to read its price at the trade's time

    Reprices the certificate on the days it was actually traded and rejects the
    model unless every one of them lands within `tolerance`.

    There is deliberately only ONE ratio convention here. An earlier version
    also tried price = (S - FN) * R and kept whichever fitted better. That is
    unsound: with a single observed trade both conventions fit it exactly, so
    the choice is made by noise. On the SNDK turbo it picked the multiply,
    which implied 2,624x leverage at purchase and valued the position at
    EUR 53,561 instead of about EUR 5. Divide is the market convention, it is
    what the multi-trade cross-check supports, and a model that cannot be
    validated should refuse rather than guess.
    """
    if not spec.priceable or not observations or not closes:
        return Model(False, spec.family, reason="no underlying price history")

    use_fx = bool(fx)
    errs, levs = [], []
    for when, paid in observations:
        s = underlying_at(closes, when, ex)
        if s is None or not paid:
            continue
        f = (_close_on(fx, when[:10]) if fx else 1.0) or None
        if not f:
            continue
        fin = fin_at(when[:10]) if fin_at else None
        model_px = turbo_price(spec, s, fx=f, financing=fin)
        if model_px is None or model_px <= 0:
            continue
        errs.append(abs(model_px / paid - 1.0) * 100.0)
        lv = effective_leverage(spec, s, fin)
        if lv:
            levs.append(lv)
    if not errs:
        return Model(False, spec.family, "div", use_fx, reason="could not reprice any trade")
    worst = round(max(errs), 1)
    if levs and max(levs) > MAX_PLAUSIBLE_LEVERAGE:
        return Model(False, spec.family, "div", use_fx, worst, len(errs),
                     f"implies {max(levs):,.0f}x leverage — not a real turbo, "
                     f"the financing level or the underlying must be wrong")
    if worst <= tolerance:
        return Model(True, spec.family, "div", use_fx, worst, len(errs))
    return Model(False, spec.family, "div", use_fx, worst, len(errs),
                 f"repricing off by {worst}% — model not trusted")


def _close_on(series: dict, day: str) -> Optional[float]:
    if not series:
        return None
    if day in series:
        return series[day]
    earlier = [k for k in series if k <= day]
    return series[max(earlier)] if earlier else None


close_on = _close_on
