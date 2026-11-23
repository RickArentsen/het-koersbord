"""
Hand-entered transactions.

Anything a broker export cannot give you — an IBKR position you are not
importing, a share bought somewhere else, a correction — goes through here and
lands in exactly the same ledger as the CSV imports.

Downstream cares where a row came from in one respect only: the money. DEGIRO's
cash balance never paid for a hand-entered trade, so the dashboard counts what
a hand-entered buy cost as capital paid in on that date, and what a
hand-entered sale brought in as capital taken out (service.dashboard). Without
that the whole position would read as profit the day it was typed in.

Two rules make this safe to use:

  * a SELL is refused unless the ledger actually holds that many units on that
    date. Otherwise FIFO produces an orphan disposal and every figure that
    touches cost basis goes quietly wrong.
  * every row gets a stable id derived from its content, so entering the same
    trade twice is caught the same way a duplicated CSV row is.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from . import store, pricing
from .degiro import Txn, Instrument, classify, underlying_of, underlying_ticker_of
from .engine import build_ledger


class ManualError(ValueError):
    """Something the user can fix, phrased for the user."""


def _fx_for(currency: str, on: str, given: Optional[float]) -> tuple[float, str]:
    """(rate, where it came from). Rate is units of `currency` per 1 EUR."""
    if not currency or currency == "EUR":
        return 1.0, "EUR"
    if given:
        return float(given), "entered"
    series = pricing.get_fx("EUR" + currency, on)
    if series:
        r = pricing.fx_on(series, on)
        if r:
            return float(r), "cached rate for that date"
    raise ManualError(
        f"No {currency}/EUR rate available for {on}. Enter the FX rate you got, "
        "or enter the trade in EUR.")


def held_on(isin: str, upto: date) -> float:
    """Units held of `isin` at the end of `upto`, from the ledger alone."""
    q = 0.0
    for t in store.load_txns():
        if t.isin == isin and t.kind in ("BUY", "SELL") and t.trade_dt.date() <= upto:
            q += t.quantity or 0.0
    return q


def add_transaction(*, kind: str, isin: str, trade_date: str, quantity: float,
                    price: float, currency: str = "EUR",
                    fx_rate: Optional[float] = None, fee_eur: float = 0.0,
                    name: Optional[str] = None, ticker: Optional[str] = None,
                    account: str = "MANUAL", note: str = "") -> dict:
    kind = (kind or "").upper().strip()
    if kind not in ("BUY", "SELL"):
        raise ManualError("Type must be BUY or SELL.")
    isin = (isin or "").upper().strip()
    if not isin:
        raise ManualError("An ISIN is required — it is how holdings are matched.")
    try:
        d = datetime.strptime(trade_date, "%Y-%m-%d").date()
    except ValueError:
        raise ManualError("Date must be a real calendar date.")
    if d > date.today():
        raise ManualError("That trade date is in the future.")

    quantity = abs(float(quantity or 0))
    price = float(price or 0)
    if quantity <= 0:
        raise ManualError("Quantity must be greater than zero.")
    if price <= 0:
        raise ManualError("Price must be greater than zero.")
    fee = abs(float(fee_eur or 0))
    currency = (currency or "EUR").upper().strip()

    insts = store.load_instruments()
    known = insts.get(isin)
    if not known and not name:
        raise ManualError(f"{isin} is new — give it a name so it can be identified.")

    if kind == "SELL":
        have = held_on(isin, d)
        if quantity > have + 1e-9:
            label = (known or {}).get("name", isin)
            raise ManualError(
                f"You held {have:g} of {label} on {d.strftime('%d/%m/%Y')}, "
                f"so {quantity:g} cannot be sold. Add the buy first, or reduce "
                f"the quantity.")

    fx, fx_src = _fx_for(currency, d.isoformat(), fx_rate)

    signed_qty = quantity if kind == "BUY" else -quantity
    local_value = -quantity * price if kind == "BUY" else quantity * price
    value_eur = local_value / fx
    total_eur = value_eur - fee            # a fee always costs you, either way

    payload = f"{kind}|{isin}|{d}|{quantity}|{price}|{currency}|{fee}"
    ext = "manual:" + hashlib.sha1(payload.encode()).hexdigest()[:20]

    t = Txn(
        kind=kind,
        trade_dt=datetime.combine(d, datetime.min.time()),
        settle_date=d,
        isin=isin,
        product=(known or {}).get("name") or name,
        quantity=signed_qty,
        price=price,
        currency=currency,
        local_value=round(local_value, 4),
        value_eur=round(value_eur, 4),
        fx_rate=fx,
        autofx_cost_eur=0.0,
        fee_eur=-fee,
        total_eur=round(total_eur, 4),
        exchange=(known or {}).get("exchange"),
        order_id=None,
        source="manual",
        external_id=ext,
        note=note or "entered by hand",
    )

    if known:
        instruments = {}
    else:
        kindi = classify(name or "", isin)
        und = underlying_of(name or "") if kindi == "DERIVATIVE" else None
        instruments = {isin: Instrument(
            isin=isin, name=name or isin, kind=kindi, currency=currency,
            exchange=None, underlying=und,
            underlying_ticker=underlying_ticker_of(und), ticker=ticker or None)}

    res = store.commit_import([t], instruments, account=account)
    if res["inserted"] == 0:
        raise ManualError(
            "That exact transaction is already in the ledger — same type, "
            "instrument, date, quantity, price and fee.")
    if ticker and not (known or {}).get("ticker"):
        store.set_instrument(isin, ticker=ticker, ticker_src="manual")

    return {
        "ok": True,
        "external_id": ext,
        "value_eur": round(value_eur, 2),
        "total_eur": round(total_eur, 2),
        "fx_rate": fx,
        "fx_source": fx_src,
    }


def list_manual(limit: int = 100) -> list:
    out = []
    with store.conn() as c:
        for r in c.execute(
                "SELECT id, kind, trade_dt, isin, product, quantity, price, currency,"
                " fee_eur, total_eur, account, note FROM txn WHERE source='manual'"
                " ORDER BY trade_dt DESC, id DESC LIMIT ?", (limit,)):
            out.append(dict(r))
    return out


def delete_manual(txn_id: int) -> dict:
    """Only ever removes a hand-entered row; imported rows are not deletable."""
    with store.conn() as c:
        row = c.execute("SELECT source, product FROM txn WHERE id=?", (txn_id,)).fetchone()
        if not row:
            raise ManualError("That transaction no longer exists.")
        if row["source"] != "manual":
            raise ManualError(
                "That row came from a broker export, so it is not editable here. "
                "Correct it at the source and re-import.")
        c.execute("DELETE FROM txn WHERE id=?", (txn_id,))
    return {"ok": True, "deleted": txn_id, "product": row["product"]}
