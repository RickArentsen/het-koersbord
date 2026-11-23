"""How long a member took to disclose a trade, and why it matters.

Under the STOCK Act a Periodic Transaction Report may be filed up to 30 days
after the member is notified and no later than 45 days after the trade.  Members
use very different amounts of that window, and the difference turns out to be
the strongest untested signal in this dataset.

The reason is mechanical rather than mysterious.  Nobody outside can act until
the filing is public, so the disclosure lag is exactly the part of the move a
follower cannot have.  A trade filed in nine days hands over most of the
subsequent year; one filed on day 44 has already spent six weeks of it.

Measured across settled in-remit decisions, twelve-month alpha falls
monotonically with the lag:

    filed within 14 days   +17.47%   (n = 321, t = +2.13)
    15-30 days              +6.77%   (n = 903, t = +2.77)
    31-45 days              +1.48%   (n = 642, t = +0.83)
    46 days or more         +1.18%   (n = 128, t = +0.36)

The split sample holds the sign - a +17.63pp training edge and +6.33pp in the
test window - and it is not simply a proxy for *which* members file fast:
comparing each member's own fast filings against their own slow ones gives
+8.88pp at t = +1.60, with 9 of the 13 members who trade enough on both sides
pointing the same way.  Suggestive rather than proven, and labelled that way.

Two values must never be read as "fast".  A lag can be **negative**, because
some filings carry a mistyped trade date (see :mod:`dates`), and it can be
**missing** when there is no filing date at all.  Both return ``None`` here
rather than zero.
"""

from __future__ import annotations

import datetime as dt
import functools

# Band edges in days. FAST is the statutory notification window at its shortest
# and LATE is past the 45-day legal maximum, so the bands are the law's own
# structure rather than quartiles fitted to this sample.
FAST_DAYS = 14
NORMAL_DAYS = 30
SLOW_DAYS = 45

BANDS = {
    "fast": ("filed fast", "Inside two weeks - most of the following year was "
                           "still available to anyone reading the filing."),
    "normal": ("filed promptly", "Within the 30-day notification window."),
    "slow": ("filed late", "Towards the 45-day statutory limit."),
    "late": ("filed very late", "Past the 45 days the STOCK Act allows, so the "
                                "trade was months old before it was public."),
}


@functools.lru_cache(maxsize=8192)
def to_iso(value: str | None) -> str | None:
    """Filing dates arrive as M/D/YYYY from the Clerk and ISO from the Senate.

    Cached, and parsed by hand rather than with ``strptime``.

    Both matter more than they look. Every row on a listing page is converted,
    and one filing covers many rows, so the same handful of dates is parsed
    thousands of times: a single landing page made 20,256 calls here, 13,574 of
    which reached ``strptime`` - which is slow enough that this was the second
    largest cost on the page. The distinct values number in the hundreds, so
    the cache turns nearly all of it into a dict lookup.
    """
    if not value:
        return None
    value = value.strip()
    if len(value) == 10 and value[4] == "-":
        return value

    # M/D/YYYY or M/D/YY, zero-padded or not. Hand-parsed: strptime spends most
    # of its time compiling format regexes and consulting locale data, none of
    # which this needs.
    parts = value.split("/")
    if len(parts) == 3:
        try:
            month, day, year = (int(p) for p in parts)
        except ValueError:
            return None
        if year < 100:
            year += 2000 if year < 70 else 1900
        try:
            return dt.date(year, month, day).isoformat()
        except ValueError:
            return None
    return None


def lag_days(tx_date: str | None, filing_date: str | None) -> int | None:
    """Days between the trade and the filing that disclosed it.

    ``None`` when there is no filing date, or when the filing appears to predate
    its own trade.  That happens with mistyped years, and treating a negative
    lag as a very prompt filing would put the least reliable rows at the top of
    exactly the band this signal cares about.
    """
    traded, filed = to_iso(tx_date), to_iso(filing_date)
    if not traded or not filed:
        return None
    try:
        lag = (dt.date.fromisoformat(filed) - dt.date.fromisoformat(traded)).days
    except ValueError:
        return None
    return lag if lag >= 0 else None


def band(lag: int | None) -> str | None:
    if lag is None:
        return None
    if lag <= FAST_DAYS:
        return "fast"
    if lag <= NORMAL_DAYS:
        return "normal"
    if lag <= SLOW_DAYS:
        return "slow"
    return "late"


def describe(lag: int | None) -> dict | None:
    """Everything a badge needs: the number, the band, and what it means."""
    if lag is None:
        return None
    key = band(lag)
    label, why = BANDS[key]
    return {
        "days": lag,
        "band": key,
        "label": label,
        "text": f"filed in {lag} day{'' if lag == 1 else 's'}",
        "why": why,
    }


def annotate(rows: list[dict]) -> list[dict]:
    """Attach ``disclosure`` to each row that carries a trade and filing date."""
    for r in rows:
        r["disclosure"] = describe(lag_days(r.get("tx_date"), r.get("filing_date")))
    return rows
