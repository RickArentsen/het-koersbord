"""Repair trade dates that cannot be true, without discarding what was filed.

A Periodic Transaction Report is typed by hand, and the year field is where it
goes wrong.  Four rows in this dataset carry a date the calendar rejects:

    2026-12-26   Steve Cohen, SONY      filed 2026-02-09
    2202-09-19   Virginia Foxx, NEWT    filed 2022-10-12
    2220-04-07   Virginia Foxx, MMP     filed 2022-05-04
    3031-04-30   Pete Sessions, IBM     filed 2021-05-03

Each is a digit slip, and each is recoverable, because **a trade cannot happen
after it was disclosed**.  Substituting the filing year - or the year before it,
when the trade sits late in the calendar and the filing sits early - lands every
one of them on a date that is both plausible and obviously what was meant.

The filed value is never overwritten.  ``tx_date`` keeps exactly what the
document said, the repair goes in ``tx_date_fixed``, and ``tx_date_note``
records the reasoning so a reader sees a correction rather than a silent edit.
That matters more than the four rows do: the Cohen row is the most recent
transaction in the database, so an uncorrected future date sorts to the top of
every "latest activity" list and makes the whole site look broken.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

# A trade more than this far before its filing is not a typo, it is a genuinely
# late disclosure - members really do file years behind, and that is a fact worth
# keeping rather than smoothing away.
MAX_PLAUSIBLE_LAG_DAYS = 730

# How far a trade date must overshoot its own filing before a *year* typo is the
# only explanation left.
#
# This threshold is the whole difference between correcting data and inventing
# it. Twenty-two rows are dated after the filing that disclosed them, but they
# are two different faults. Ten overshoot by months - Cohen's SONY buy is dated
# 26 December 2026 on a filing from 9 February 2026 - and only a wrong year
# explains those. The other twelve overshoot by one to twenty days, and for
# those a slipped day or month, or an index date that lags the document, is far
# likelier than a member filing a full year early. Rolling those back a year
# would turn a two-day discrepancy into a fabricated 364-day disclosure lag.
#
# So: correct the unambiguous ones, flag the rest, and change nothing that
# cannot be recovered with confidence.
AMBIGUOUS_OVERSHOOT_DAYS = 60


def _iso(value: str | None) -> str | None:
    """Filing dates arrive as M/D/YYYY from the Clerk's index, ISO from the Senate."""
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


def repair(tx_date: str | None, filing_date: str | None,
           notification_date: str | None = None,
           today: str | None = None) -> tuple[str | None, str | None]:
    """Return ``(corrected_date, note)``, or ``(None, None)`` if nothing is wrong.

    Two things make a filed date impossible: it is in the future, or it is after
    the filing that disclosed it.  Both are tested, because a date can be in the
    past and still impossible - Foxx's "2202-09-19" is neither.
    """
    if not tx_date or len(tx_date) < 10:
        return None, None
    today = today or dt.date.today().isoformat()
    public = _iso(filing_date) or _iso(notification_date)

    impossible_future = tx_date > today
    impossible_order = bool(public and tx_date > public)
    if not (impossible_future or impossible_order):
        return None, None

    month_day = tx_date[4:]          # "-12-26"

    # Every correction here is "the year should be the one implied by the
    # filing". Without a filing date there is no such implication: a trade dated
    # 2027-01-05 is equally explained by 2026 or 2025, and picking the nearer one
    # would be a guess dressed as a repair. Flag it and stop.
    if not public:
        return None, (f"dated {tx_date}, which is in the future and cannot be "
                      f"right, and the filing carries no date to recover the "
                      f"intended year from")

    anchor = public
    anchor_year = int(anchor[:4])

    # A year the calendar itself rejects ("2202", "3031") needs no threshold -
    # nothing but a typo produces it.
    year = int(tx_date[:4])
    absurd_year = year > int(today[:4]) + 1 or year < 2000
    try:
        overshoot = (dt.date.fromisoformat(tx_date)
                     - dt.date.fromisoformat(anchor)).days
    except ValueError:
        overshoot = None        # unparseable year; absurd_year already caught it

    if not absurd_year and (overshoot is None
                            or overshoot <= AMBIGUOUS_OVERSHOOT_DAYS):
        # Wrong, but not recoverably wrong. Say so and leave the date alone.
        return None, (f"dated {overshoot} day{'' if overshoot == 1 else 's'} after "
                      f"the filing that disclosed it, which cannot be right; too "
                      f"small a gap to tell whether the day, the month or the year "
                      f"was mistyped, so it is shown as filed and treated as "
                      f"unreliable")

    # Try the filing year first, then the year before it. A December trade filed
    # in February belongs to the previous year, which is exactly the Cohen case.
    for candidate_year in (anchor_year, anchor_year - 1):
        candidate = f"{candidate_year:04d}{month_day}"
        try:
            made = dt.date.fromisoformat(candidate)
        except ValueError:
            continue          # 29 February in a non-leap year
        if candidate > anchor or candidate > today:
            continue
        lag = (dt.date.fromisoformat(anchor) - made).days
        if 0 <= lag <= MAX_PLAUSIBLE_LAG_DAYS:
            why = ("dated after it was filed" if impossible_order
                   else "dated in the future")
            return candidate, (f"filed as {tx_date} — {why}; the year is the only "
                               f"digit that can be wrong by this much, so it is "
                               f"read as {candidate_year} from the filing date")

    return None, (f"filed as {tx_date}, which is impossible, and no plausible "
                  f"year could be recovered from the filing date")


def repair_all(conn: sqlite3.Connection, progress: bool = True) -> dict:
    """Scan every transaction and record a correction where one is needed.

    Idempotent: it recomputes from ``tx_date`` each time, so running it twice -
    or after a re-parse has minted new rows - cannot compound a correction.
    """
    rows = conn.execute(
        """SELECT id, tx_date, filing_date, notification_date, member_name, ticker
             FROM transactions WHERE tx_date IS NOT NULL""").fetchall()

    fixed, unfixable, cleared = [], [], []
    for r in rows:
        corrected, note = repair(r["tx_date"], r["filing_date"],
                                 r["notification_date"])
        if corrected:
            fixed.append((corrected, note, r["id"]))
            if progress:
                print(f"  {r['tx_date']} -> {corrected}  "
                      f"{(r['member_name'] or '').replace('Hon. ', '')[:26]:28}"
                      f"{r['ticker'] or ''}", flush=True)
        elif note:
            unfixable.append((None, note, r["id"]))
            if progress:
                print(f"  {r['tx_date']} -> UNFIXABLE  "
                      f"{(r['member_name'] or '').replace('Hon. ', '')[:26]}", flush=True)
        else:
            cleared.append(r["id"])

    conn.executemany(
        "UPDATE transactions SET tx_date_fixed=?, tx_date_note=? WHERE id=?",
        fixed + unfixable)
    # A row that used to look broken and now parses cleanly - because the filing
    # date was corrected upstream - must lose its old note, or the site keeps
    # explaining a problem that is gone.
    conn.executemany(
        "UPDATE transactions SET tx_date_fixed=NULL, tx_date_note=NULL "
        "WHERE id=? AND (tx_date_fixed IS NOT NULL OR tx_date_note IS NOT NULL)",
        [(i,) for i in cleared])
    conn.commit()
    return {"scanned": len(rows), "corrected": len(fixed),
            "unfixable": len(unfixable)}
