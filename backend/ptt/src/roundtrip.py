"""Match a disclosed sale back to the purchase it closes.

A sale on its own says almost nothing: "Hon. X sold NVDA" is only interesting
next to when they bought it and what happened in between.  This pairs the two,
so a sale page can show the position rather than the exit.

How lots are matched
--------------------
**FIFO on disclosed value, within one account.**  Purchases push a lot carrying
the midpoint of the disclosed range; a sale consumes from the oldest open lots
until its own midpoint is used up.  Matching is keyed on
``(member, ticker, owner)`` - a spouse's account and a dependent child's are
different positions, and pooling them would pair a sale with a purchase that was
never in the same portfolio.  Requiring the owner to agree costs almost nothing:
57.3% of sales have a prior purchase of that ticker by that member, and 55.5%
still do once the account has to match too.

Where this is approximate, and it is worth being clear
------------------------------------------------------
Filings disclose a **range**, never a share count, so a lot's size is a midpoint
standing in for a quantity nobody published.  Worse, a sale's disclosed value is
*proceeds* while a purchase's is *cost*, so a position that doubled shows a sale
roughly twice its buy and consumes two lots' worth of notional when it really
closed one.  FIFO is therefore a reasonable reading of the filings, not a
reconstruction of the account.

The remaining 44.5% of sales have no matching purchase at all, almost always
because the position was opened before this dataset begins in 2019.  Those are
reported as unmatched rather than guessed at.

One deliberate gap inside that figure: **83 sales share their trade date with a
purchase that the filing lists after them**.  A looser test - "does a purchase
exist dated on or before this sale" - pairs them and reports 56.0% instead of
55.5%.  They stay unmatched here because a filing's row order is not a
chronology; when a buy and a sell carry the same date, nothing in the document
says which came first, so matching them would be asserting an order that was
never disclosed.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

HOLD_DAYS = 365
BENCHMARK = "SPY"


def _to_iso(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip()
    if len(value) == 10 and value[4] == "-":
        return value
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _price_on(conn: sqlite3.Connection, ticker: str, date: str) -> float | None:
    """Last close on or before ``date``, within a fortnight.

    The window matters: a delisted ticker's final close can be months before the
    date asked for, and pricing a sale against it would invent a return.
    """
    row = conn.execute(
        """SELECT date, close FROM prices
            WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1""",
        (ticker.replace(".", "-").upper(), date)).fetchone()
    if not row or row["close"] is None or row["close"] <= 0:
        return None
    gap = (dt.date.fromisoformat(date) - dt.date.fromisoformat(row["date"])).days
    return float(row["close"]) if gap <= 14 else None


def _events(conn: sqlite3.Connection, bioguide: str, ticker: str,
            owner: str | None) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT t.id, t.tx_type, t.amount_mid, t.amount_raw, t.filing_date,
                  COALESCE(t.tx_date_fixed, t.tx_date) AS tx_date,
                  p.entry_date
             FROM transactions t
        LEFT JOIN trade_performance p ON p.transaction_id = t.id
            WHERE t.bioguide_id = ? AND t.ticker = ? AND t.owner IS ?
              AND t.tx_date IS NOT NULL
         ORDER BY COALESCE(t.tx_date_fixed, t.tx_date), t.id""",
        (bioguide, ticker, owner)).fetchall()


def _field(row, key):
    """Read a column from either a dict or a sqlite3.Row.

    ``sqlite3.Row`` has no ``.get``; callers pass both.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def match_for_sale(conn: sqlite3.Connection, sale: sqlite3.Row | dict) -> dict:
    """Which purchases does this sale close, and what did the position do?

    Replays every event in that account and ticker up to this sale, so the FIFO
    queue reflects the sales that came before it too - otherwise a member's third
    sale would be matched against a lot their first sale had already closed.
    """
    bioguide, ticker = _field(sale, "bioguide_id"), _field(sale, "ticker")
    if not bioguide or not ticker:
        return {"lots": [], "matched": False}

    events = _events(conn, bioguide, ticker, sale["owner"])
    open_lots: list[dict] = []
    consumed: list[dict] = []

    for e in events:
        if e["tx_type"] == "purchase":
            open_lots.append({"id": e["id"], "tx_date": e["tx_date"],
                              "entry_date": e["entry_date"],
                              "amount_raw": e["amount_raw"],
                              "filing_date": e["filing_date"],
                              "remaining": float(e["amount_mid"] or 0)})
            continue
        if not str(e["tx_type"]).startswith("sale"):
            continue

        need = float(e["amount_mid"] or 0)
        taken = []
        while open_lots and need > 0:
            lot = open_lots[0]
            used = min(lot["remaining"], need)
            taken.append({**lot, "used": used})
            need -= used
            lot["remaining"] -= used
            if lot["remaining"] <= 1e-9:
                open_lots.pop(0)
        # A full sale closes the position even when the disclosed proceeds do not
        # add up to the disclosed cost - the ranges are too coarse to insist.
        if e["tx_type"] == "sale" and open_lots and not taken:
            taken.append({**open_lots.pop(0), "used": 0.0})
        elif e["tx_type"] == "sale" and open_lots:
            open_lots.clear()

        if e["id"] == sale["id"]:
            consumed = taken
            break

    if not consumed:
        return {"lots": [], "matched": False}

    sale_date = sale["tx_date"]
    sale_public = _to_iso(sale["filing_date"]) or sale_date
    sell_price = _price_on(conn, ticker, sale_date)
    bench_sell = _price_on(conn, BENCHMARK, sale_date)

    lots = []
    for lot in consumed:
        buy_price = _price_on(conn, ticker, lot["tx_date"])
        bench_buy = _price_on(conn, BENCHMARK, lot["tx_date"])
        held = (dt.date.fromisoformat(sale_date)
                - dt.date.fromisoformat(lot["tx_date"])).days
        ret = ((sell_price / buy_price - 1)
               if buy_price and sell_price else None)
        bench = ((bench_sell / bench_buy - 1)
                 if bench_buy and bench_sell else None)

        # The follower's clock is not the member's. A copier entered the day
        # after the purchase was filed and leaves 365 days later, so what decides
        # the advice is whether that window was still open when this sale became
        # public - not how long the member happened to hold.
        entry = lot["entry_date"] or _to_iso(lot["filing_date"])
        exit_due = (dt.date.fromisoformat(entry)
                    + dt.timedelta(days=HOLD_DAYS)).isoformat() if entry else None

        # A quarter of matched sales are disclosed in the *same filing* as the
        # purchase they close: Rulli's PLTR round trip ran from January to May
        # and both halves appeared on 7 August. A follower never held that
        # position - it was already over when it became public - so telling them
        # to keep holding would be advice about a position they cannot own.
        never_opened = bool(entry and sale_public <= entry)
        still_open = bool(exit_due and not never_opened and sale_public < exit_due)
        days_left = ((dt.date.fromisoformat(exit_due)
                      - dt.date.fromisoformat(sale_public)).days
                     if still_open else 0)

        lots.append({
            "buy_id": lot["id"], "buy_date": lot["tx_date"],
            "buy_amount": lot["amount_raw"], "entry_date": entry,
            "held_days": held, "member_return": ret,
            "bench_return": bench,
            "member_alpha": (ret - bench) if ret is not None and bench is not None else None,
            "exit_due": exit_due, "still_open": still_open,
            "never_opened": never_opened, "days_left": days_left,
        })

    longest = max(lots, key=lambda x: x["held_days"])
    any_open = any(x["still_open"] for x in lots)
    all_stale = all(x["never_opened"] for x in lots)
    return {
        "matched": True, "lots": lots, "n_lots": len(lots),
        "held_days": longest["held_days"],
        "member_alpha": longest["member_alpha"],
        "member_return": longest["member_return"],
        "still_open": any_open, "never_opened": all_stale,
        "days_left": max((x["days_left"] for x in lots), default=0),
        "advice": _advice(any_open, all_stale),
    }


def _advice(still_open: bool, never_opened: bool) -> dict:
    """What a follower should do, and why - never a bare instruction.

    The reasoning is the measured one: exiting when members disclose selling,
    rather than at twelve months, cut in-remit alpha from +4.75% to +2.83%, and
    cost 3.91 points on the positions where it changed anything (t = -3.93).
    So a sale is news about the member, not an instruction to the reader.
    """
    if never_opened:
        return {
            "action": "nothing to do",
            "why": ("This purchase and this sale were disclosed in the same "
                    "filing, so the whole round trip was already over by the "
                    "time anyone outside could see it. There was never a "
                    "position to follow - which is the disclosure lag doing its "
                    "worst, not a judgement about the trade."),
        }
    if still_open:
        return {
            "action": "keep holding",
            "why": ("The member is out, but the tracked position is not due to "
                    "close yet. Following members out of positions was tested "
                    "and it lost 3.91 points against holding the full term - "
                    "disclosure arrives too late to be worth acting on."),
        }
    return {
        "action": "already closed",
        "why": ("The tracked position had already reached its twelve months and "
                "been sold on the calendar before this sale was disclosed, so "
                "there is nothing to act on."),
    }


def match_for_buy(conn: sqlite3.Connection, buy: sqlite3.Row | dict) -> dict:
    """Which sales, if any, closed this purchase - the reverse of the above.

    Same FIFO replay, recording which sale consumed each lot rather than which
    lot each sale took.  A purchase can be closed by several sales and can be
    only partly closed, so this reports every sale that touched it and how much
    of the lot is left rather than picking one and calling it the exit.

    "Still held" here means *as far as the filings show*.  A member who sold
    without disclosing, or whose sale is still inside its 45-day window, looks
    identical to one who is holding, and the page says so rather than asserting
    a position that may already be gone.
    """
    bioguide, ticker = _field(buy, "bioguide_id"), _field(buy, "ticker")
    if not bioguide or not ticker or _field(buy, "tx_type") != "purchase":
        return {"matched": False, "sales": []}

    events = _events(conn, bioguide, ticker, _field(buy, "owner"))
    open_lots: list[dict] = []
    target = _field(buy, "id")
    sales: list[dict] = []
    lot_size = 0.0

    for e in events:
        if e["tx_type"] == "purchase":
            open_lots.append({"id": e["id"], "tx_date": e["tx_date"],
                              "remaining": float(e["amount_mid"] or 0)})
            if e["id"] == target:
                lot_size = float(e["amount_mid"] or 0)
            continue
        if not str(e["tx_type"]).startswith("sale"):
            continue

        need = float(e["amount_mid"] or 0)
        while open_lots and need > 0:
            lot = open_lots[0]
            used = min(lot["remaining"], need)
            if lot["id"] == target and used > 0:
                sales.append({"sale_id": e["id"], "sale_date": e["tx_date"],
                              "tx_type": e["tx_type"], "used": used,
                              "filing_date": e["filing_date"]})
            need -= used
            lot["remaining"] -= used
            if lot["remaining"] <= 1e-9:
                open_lots.pop(0)
        if e["tx_type"] == "sale" and open_lots:
            if any(l["id"] == target for l in open_lots):
                sales.append({"sale_id": e["id"], "sale_date": e["tx_date"],
                              "tx_type": e["tx_type"], "used": 0.0,
                              "filing_date": e["filing_date"]})
            open_lots.clear()

    still_open = next((l["remaining"] for l in open_lots if l["id"] == target), 0.0)
    if not sales:
        return {"matched": False, "sales": [], "still_held": True,
                "advice": {
                    "action": "still held, as far as the filings show",
                    "why": ("No disclosed sale of this name from this account "
                            "since the purchase. A sale inside its 45-day "
                            "disclosure window would look exactly the same, so "
                            "this is the absence of a filing rather than "
                            "evidence of a position."),
                }}

    buy_date = _field(buy, "tx_date")
    buy_price = _price_on(conn, ticker, buy_date)
    bench_buy = _price_on(conn, BENCHMARK, buy_date)
    for x in sales:
        sell_price = _price_on(conn, ticker, x["sale_date"])
        bench_sell = _price_on(conn, BENCHMARK, x["sale_date"])
        x["held_days"] = (dt.date.fromisoformat(x["sale_date"])
                          - dt.date.fromisoformat(buy_date)).days
        x["member_return"] = ((sell_price / buy_price - 1)
                              if buy_price and sell_price else None)
        bench = ((bench_sell / bench_buy - 1)
                 if bench_buy and bench_sell else None)
        x["member_alpha"] = ((x["member_return"] - bench)
                             if x["member_return"] is not None and bench is not None
                             else None)
        x["partial"] = str(x["tx_type"]).endswith("partial")

    last = sales[-1]
    return {
        "matched": True, "sales": sales, "n_sales": len(sales),
        "still_held": still_open > 1e-9,
        "fully_closed": still_open <= 1e-9,
        "held_days": last["held_days"],
        "member_return": last["member_return"],
        "member_alpha": last["member_alpha"],
        "lot_size": lot_size,
        "advice": {
            "action": ("closed by the member" if still_open <= 1e-9
                       else "partly sold by the member"),
            "why": ("What the member did afterwards, for context. The strategy "
                    "does not act on it: exiting when members disclose selling "
                    "cost 3.91 points against holding the full twelve months."),
        },
    }


def coverage(conn: sqlite3.Connection) -> dict:
    """How many disclosed sales can be paired with a purchase at all."""
    from collections import defaultdict

    rows = conn.execute(
        """SELECT bioguide_id, ticker, owner, tx_type,
                  COALESCE(tx_date_fixed, tx_date) AS d
             FROM transactions
            WHERE bioguide_id IS NOT NULL AND ticker IS NOT NULL
              AND ticker <> '' AND tx_date IS NOT NULL
         ORDER BY d, id""").fetchall()

    seen_buy: dict[tuple, int] = defaultdict(int)
    sales = matched = 0
    for r in rows:
        key = (r["bioguide_id"], r["ticker"], r["owner"])
        if r["tx_type"] == "purchase":
            seen_buy[key] += 1
        elif str(r["tx_type"]).startswith("sale"):
            sales += 1
            if seen_buy.get(key):
                matched += 1
    return {"sales": sales, "matchable": matched,
            "share": (matched / sales) if sales else 0.0}
