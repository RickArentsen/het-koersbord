"""
FIFO lot engine + position/P&L derivation.

Everything here is a pure function of the transaction list. Nothing is
stored. If a number looks wrong you fix a transaction, not this file.

Currency convention throughout:
  fx_rate is DEGIRO's "Wisselkoers" = units of the instrument currency
  per 1 EUR (so USD/EUR ~ 1.16). EUR instruments have fx_rate None -> 1.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional


def _fx(t) -> float:
    return t.fx_rate if t.fx_rate else 1.0


@dataclass
class Lot:
    isin: str
    open_dt: datetime
    qty: float
    price_native: float          # per share, in instrument currency
    fx_at_open: float            # units of ccy per EUR
    currency: str
    cost_eur_gross: float        # what actually left the account, incl. fees
    fees_eur: float

    @property
    def cost_eur_clean(self) -> float:
        """Cost excluding fees — used for the price/FX attribution."""
        return self.qty * self.price_native / self.fx_at_open


@dataclass
class Disposal:
    isin: str
    open_dt: datetime
    close_dt: datetime
    qty: float
    open_price: float
    close_price: float
    fx_open: float
    fx_close: float
    currency: str
    proceeds_eur: float          # net of the sell-side costs
    cost_eur: float              # incl. the buy-side costs
    pnl_eur: float
    pnl_price_eur: float
    pnl_fx_eur: float
    fees_eur: float

    @property
    def holding_days(self) -> int:
        return (self.close_dt.date() - self.open_dt.date()).days


@dataclass
class Position:
    isin: str
    qty: float = 0.0
    currency: str = "EUR"
    cost_eur: float = 0.0            # remaining open cost, incl. fees paid
    lots: list = field(default_factory=list)

    @property
    def avg_cost_eur(self) -> float:
        return self.cost_eur / self.qty if self.qty else 0.0

    @property
    def avg_price_native(self) -> float:
        n = sum(l.qty * l.price_native for l in self.lots)
        return n / self.qty if self.qty else 0.0

    @property
    def cost_native(self) -> float:
        return sum(l.qty * l.price_native for l in self.lots)

    @property
    def cost_eur_clean(self) -> float:
        """Cost excluding fees, each lot at the FX rate that actually applied."""
        return sum(l.cost_eur_clean for l in self.lots)

    @property
    def avg_fx(self) -> float:
        """Weighted-average FX across every open lot, not just the first."""
        c = self.cost_eur_clean
        return (self.cost_native / c) if c else 1.0

    @property
    def first_open(self) -> Optional[datetime]:
        return min((l.open_dt for l in self.lots), default=None)


@dataclass
class LedgerResult:
    positions: dict            # isin -> Position (open only)
    disposals: list            # list[Disposal]
    closed: dict               # isin -> list[Disposal] for fully closed names
    orphan_sells: list         # sells with no matching lot (pre-history)
    orphan_proceeds_eur: float # what those sells brought in; no lot ever saw it
    fees_eur: float
    autofx_eur: float
    realised_eur: float
    realised_price_eur: float
    realised_fx_eur: float
    invested_eur: float        # net cash into securities (buys - sells, incl costs)


def build_ledger(trades: list) -> LedgerResult:
    """
    trades: normalised Txn list, BUY/SELL only, any order.
    FIFO: each sell consumes the oldest open lots of that ISIN.
    """
    trades = sorted([t for t in trades if t.kind in ("BUY", "SELL")],
                    key=lambda t: t.trade_dt)

    books: dict[str, list[Lot]] = {}
    disposals: list[Disposal] = []
    orphans: list = []
    fees = autofx = 0.0
    invested = 0.0
    orphan_proceeds = 0.0

    for t in trades:
        ccy = t.currency or "EUR"
        fx = _fx(t)
        fee_row = abs(t.fee_eur or 0.0) + abs(t.autofx_cost_eur or 0.0)
        fees += abs(t.fee_eur or 0.0)
        autofx += abs(t.autofx_cost_eur or 0.0)
        if t.total_eur is not None:
            invested += -t.total_eur          # buys are negative -> positive invested

        if t.kind == "BUY":
            books.setdefault(t.isin, []).append(Lot(
                isin=t.isin,
                open_dt=t.trade_dt,
                qty=abs(t.quantity),
                price_native=abs(t.price) if t.price else 0.0,
                fx_at_open=fx,
                currency=ccy,
                cost_eur_gross=-t.total_eur if t.total_eur is not None else 0.0,
                fees_eur=fee_row,
            ))
            continue

        # SELL --------------------------------------------------------
        remaining = abs(t.quantity)
        sell_price = abs(t.price) if t.price else 0.0
        # The total with its sign, not its size. A knocked-out turbo sold for
        # 4 cents with a 50 cent commission has a total of -0.46: the sale cost
        # money. abs() booked that as 46 cents received, and realised P&L came
        # out 92 cents too high.
        gross_proceeds_eur = t.total_eur if t.total_eur is not None else 0.0
        sold_total = remaining or 1.0
        book = books.get(t.isin, [])

        while remaining > 1e-9 and book:
            lot = book[0]
            take = min(lot.qty, remaining)
            frac_lot = take / lot.qty
            frac_sell = take / sold_total

            cost_eur = lot.cost_eur_gross * frac_lot
            proceeds_eur = gross_proceeds_eur * frac_sell

            cost_clean = take * lot.price_native / lot.fx_at_open
            proc_clean = take * sell_price / fx
            pnl_price = (take * sell_price - take * lot.price_native) / fx
            pnl_fx = (take * lot.price_native) * (1.0 / fx - 1.0 / lot.fx_at_open)

            disposals.append(Disposal(
                isin=t.isin,
                open_dt=lot.open_dt,
                close_dt=t.trade_dt,
                qty=take,
                open_price=lot.price_native,
                close_price=sell_price,
                fx_open=lot.fx_at_open,
                fx_close=fx,
                currency=ccy,
                proceeds_eur=proceeds_eur,
                cost_eur=cost_eur,
                pnl_eur=proceeds_eur - cost_eur,
                pnl_price_eur=pnl_price,
                pnl_fx_eur=pnl_fx,
                fees_eur=lot.fees_eur * frac_lot + fee_row * frac_sell,
            ))

            lot.qty -= take
            lot.cost_eur_gross -= cost_eur
            lot.fees_eur -= lot.fees_eur * frac_lot
            remaining -= take
            if lot.qty <= 1e-9:
                book.pop(0)

        if remaining > 1e-9:
            orphans.append((t.isin, t.product, remaining, t.trade_dt.date()))
            orphan_proceeds += gross_proceeds_eur * (remaining / sold_total)

    positions: dict[str, Position] = {}
    for isin, book in books.items():
        live = [l for l in book if l.qty > 1e-9]
        if not live:
            continue
        p = Position(isin=isin, currency=live[0].currency, lots=live)
        p.qty = sum(l.qty for l in live)
        p.cost_eur = sum(l.cost_eur_gross for l in live)
        positions[isin] = p

    closed: dict[str, list] = {}
    for d in disposals:
        if d.isin not in positions:
            closed.setdefault(d.isin, []).append(d)

    return LedgerResult(
        positions=positions,
        disposals=disposals,
        closed=closed,
        orphan_sells=orphans,
        orphan_proceeds_eur=orphan_proceeds,
        fees_eur=fees,
        autofx_eur=autofx,
        realised_eur=sum(d.pnl_eur for d in disposals),
        realised_price_eur=sum(d.pnl_price_eur for d in disposals),
        realised_fx_eur=sum(d.pnl_fx_eur for d in disposals),
        invested_eur=invested,
    )


# --------------------------------------------------------------------------
# XIRR
# --------------------------------------------------------------------------

def xirr(flows: list[tuple[date, float]], guess: float = 0.1) -> Optional[float]:
    """
    flows: [(date, amount)] — negative = money out, positive = money in.
    Returns the annualised rate, or None when it cannot be solved
    (all one sign, or no convergence).
    """
    if len(flows) < 2:
        return None
    if not (any(a < 0 for _, a in flows) and any(a > 0 for _, a in flows)):
        return None

    t0 = min(d for d, _ in flows)
    years = [((d - t0).days / 365.0, a) for d, a in flows]

    def npv(r: float) -> float:
        return sum(a / (1.0 + r) ** y for y, a in years)

    # bisect on a wide bracket — robust where Newton diverges
    lo, hi = -0.9999, 100.0
    f_lo, f_hi = npv(lo), npv(hi)
    if f_lo * f_hi > 0:
        return None
    for _ in range(300):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9:
            return mid
        if f_lo * f_mid < 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


def position_flows(isin: str, trades: list, market_value_eur: float,
                   asof: date, extra: list = None) -> list[tuple[date, float]]:
    """Cash flows for one position: buys out, sells/dividends in, MV as terminal."""
    fl: list[tuple[date, float]] = []
    for t in trades:
        if t.isin != isin or t.kind not in ("BUY", "SELL"):
            continue
        if t.total_eur is None:
            continue
        fl.append((t.trade_dt.date(), t.total_eur))
    for d, a in (extra or []):
        fl.append((d, a))
    if market_value_eur:
        fl.append((asof, market_value_eur))
    return fl


# --------------------------------------------------------------------------
# Concentration
# --------------------------------------------------------------------------

def concentration(weights: list[float]) -> dict:
    w = [x for x in weights if x > 0]
    s = sum(w) or 1.0
    w = sorted((x / s for x in w), reverse=True)
    hhi = sum(x * x for x in w)
    return {
        "n": len(w),
        "hhi": hhi,
        "effective_n": (1.0 / hhi) if hhi else 0.0,
        "top1": w[0] if w else 0.0,
        "top3": sum(w[:3]),
        "top5": sum(w[:5]),
    }
