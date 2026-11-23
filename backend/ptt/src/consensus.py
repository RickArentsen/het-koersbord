"""How many other members were already buying this name when this trade landed.

A single member buying a defence contractor is one opinion.  Three of them
buying it inside two months, independently, is a different object - and it
measures differently.  Across settled in-remit decisions, counting only peers
whose purchases were **already public**:

    1 member      +4.78%   (n = 1,881, t = +2.49)
    2 members     +5.57%   (n =   297, t = +2.49)
    3 or more    +12.61%   (n =   108, t = +2.85)

The split sample holds the sign - a +13.69pp training edge over the one-member
case and +6.77pp in the test window - though 108 decisions at the top end is
thin, and the flag is presented as context rather than as a filter.

Point-in-time, and the reason it has to be
------------------------------------------
The first version of this measurement counted every member who bought within
thirty days *either side*, which quietly included people who bought later.  A
reader looking at a filing cannot see purchases that have not happened yet, and
counting them inflated the effect: 3+ members scored +15.87% that way against
+12.61% once the future was excluded.

So a peer counts only if their own entry - the first market open after *their*
filing became public - was on or before this one's.  The rank a trade is given
is the rank it would have had at the moment it appeared.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

# How far back a peer purchase still counts as part of the same cluster. Two
# months is long enough to catch members reacting to the same event through
# staggered disclosure and short enough that it is not simply "who owns this".
WINDOW_DAYS = 60


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def _field(row, key):
    """Read a column from either a dict or a sqlite3.Row.

    ``sqlite3.Row`` has no ``.get``, and callers pass both: the site hands over
    dicts, ad-hoc queries hand over rows. Getting this wrong raised only at the
    call site that happened to use a Row.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def rank_for(conn: sqlite3.Connection, trade: sqlite3.Row | dict,
             window: int = WINDOW_DAYS) -> dict | None:
    """Where this purchase sits in the queue of members buying the same name.

    Returns ``None`` for anything that cannot be ranked - a sale, a trade with
    no ticker, or one with no entry date because it was never priced.
    """
    if str(_field(trade, "tx_type") or "") != "purchase":
        return None
    ticker, entry = _field(trade, "ticker"), _field(trade, "entry_date")
    if not ticker or not entry:
        return None

    since = (dt.date.fromisoformat(entry)
             - dt.timedelta(days=window)).isoformat()
    rows = conn.execute(
        """SELECT t.bioguide_id, t.member_name, p.entry_date
             FROM transactions t
             JOIN trade_performance p ON p.transaction_id = t.id
            WHERE t.ticker = ? AND t.tx_type = 'purchase'
              AND t.bioguide_id IS NOT NULL
              AND p.entry_date IS NOT NULL
              AND p.entry_date >= ? AND p.entry_date <= ?
         ORDER BY p.entry_date, t.id""", (ticker, since, entry)).fetchall()

    # One entry per member: a member who bought the same name three times in the
    # window is one opinion, not three.
    first_seen: dict[str, str] = {}
    names: dict[str, str] = {}
    for r in rows:
        first_seen.setdefault(r["bioguide_id"], r["entry_date"])
        names.setdefault(r["bioguide_id"], (r["member_name"] or "").replace("Hon. ", ""))

    me = _field(trade, "bioguide_id")
    ordered = sorted(first_seen.items(), key=lambda kv: kv[1])
    position = next((i + 1 for i, (bg, _d) in enumerate(ordered) if bg == me),
                    len(ordered))
    total = len(ordered)
    others = [names[bg] for bg, _d in ordered if bg != me]

    return {
        "position": position,
        "total": total,
        "window": window,
        "others": others,
        "clustered": total >= 3,
        "text": (f"{_ordinal(position)} member to buy this in {window} days"
                 if total > 1 else f"only member to buy this in {window} days"),
    }


def annotate(conn: sqlite3.Connection, rows: list[dict],
             window: int = WINDOW_DAYS) -> list[dict]:
    for r in rows:
        r["consensus"] = rank_for(conn, r, window)
    return rows
