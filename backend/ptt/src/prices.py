"""Price history for every ticker that appears in the disclosures.

Uses yfinance with ``auto_adjust=True`` so closes are adjusted for splits and
dividends - required for the return maths in :mod:`backtest` to be correct.
Everything is cached in SQLite; a re-run only fetches what is missing.

Two entry points, and a daily job needs both.  :func:`sync_prices` downloads
history for tickers seen for the first time.  :func:`top_up` extends series that
are already cached but have stopped short of today - without it a freshly
disclosed trade has no entry price and vanishes from the strategy without
comment.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import logging
import sqlite3
import warnings

import pandas as pd

warnings.filterwarnings("ignore", category=FutureWarning)

BENCHMARK = "SPY"
BATCH_SIZE = 40

# The floor below which a quote is not usable for return arithmetic.
#
# Yahoo serves genuinely impossible data for thinly traded OTC foreign ADRs.
# Measured 2026-08-17: DAIUF (a Daimler ADR) carried 957 non-positive closes out
# of 2,373 - including -1.52 - and a positive close of $0.00000028. JGCCF and
# OCLCF are worse; OCLCF reaches -539,041.
#
# A negative price is impossible, so any series containing one cannot be trusted
# anywhere. A sub-cent price is not impossible - real sub-penny OTC names trade
# there - but it is useless as a return denominator, because a one-tick move is
# a several-hundred-percent return. DAIUF entered the strategy at $0.00000028 and
# was scored as +1,913% over twelve months, which alone inflated the site's
# headline from 3.30x to 3.68x and its CAGR from 19.8% to 21.8%.
#
# So both rules exist and they are complementary: :func:`_store` refuses the
# impossible values at write time, and :func:`unusable_tickers` quarantines the
# whole series when either fault is present, because a source that reports a
# negative share price has not earned trust in its other rows.
MIN_PLAUSIBLE_PRICE = 0.01

# Rows refused by _store since the process started, per ticker. A counter rather
# than a raised exception: one bad ticker must not abort a 3,000-ticker sync, but
# silently dropping rows is how the fault above went unnoticed for months.
REJECTED: dict[str, int] = {}

# Tickers ever found to carry impossible prices, recorded in ``meta`` so the
# finding outlives the rows that proved it. See :func:`unusable_tickers`.
QUARANTINE_KEY = "price_quarantine_v1"

# A ticker that has not traded in three weeks has almost certainly been
# delisted or acquired - members hold names that later vanish, and those series
# simply end. Stop chasing them nightly, but re-test the assumption weekly so a
# genuine data outage at Yahoo does not permanently retire a live ticker.
DELISTED_AFTER_DAYS = 21
RECHECK_EVERY_DAYS = 7


@contextlib.contextmanager
def _quiet_yfinance():
    """Swallow yfinance's per-ticker narration.

    yfinance reports a delisted symbol by printing to the console and logging a
    warning, neither of which this code can route anywhere useful. On a corpus
    holding Express Scripts, Time Warner and a dozen other acquired companies
    that is a page of "possibly delisted" complaints on every run, which reads
    like a crash and buries the lines that matter.

    The information is not lost: the caller records which tickers returned
    nothing and reports the count.
    """
    logger = logging.getLogger("yfinance")
    previous = logger.level
    logger.setLevel(logging.CRITICAL)
    sink = io.StringIO()
    try:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            yield sink
    finally:
        logger.setLevel(previous)
# Buy the widest window the disclosures can need: the earliest trade plus a
# 12-month forward horizon.
PAD_BEFORE = dt.timedelta(days=10)
PAD_AFTER = dt.timedelta(days=400)

# yfinance uses '-' where the filings use '.' or '/' for share classes.
TICKER_FIXUPS = {".": "-", "/": "-"}


def normalize_ticker(ticker: str) -> str:
    t = (ticker or "").strip().upper()
    for old, new in TICKER_FIXUPS.items():
        t = t.replace(old, new)
    return t


def tickers_to_fetch(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """SELECT DISTINCT ticker FROM transactions
            WHERE ticker IS NOT NULL AND ticker <> ''
              AND asset_type IN ('ST', 'OT', 'ETF', 'CS', 'EF', 'OP')"""
    ).fetchall()
    tickers = {normalize_ticker(r["ticker"]) for r in rows}
    tickers.discard("")
    tickers.add(BENCHMARK)
    return sorted(tickers)


def _date_bounds(conn: sqlite3.Connection) -> tuple[dt.date, dt.date]:
    row = conn.execute(
        "SELECT MIN(tx_date) a, MAX(notification_date) b FROM transactions "
        "WHERE tx_date IS NOT NULL").fetchone()
    if not row or not row["a"]:
        today = dt.date.today()
        return today - dt.timedelta(days=365), today
    start = dt.date.fromisoformat(row["a"]) - PAD_BEFORE
    end = min(dt.date.today(), dt.date.fromisoformat(row["b"] or row["a"]) + PAD_AFTER)
    return start, max(end, dt.date.today() - dt.timedelta(days=1))


def _store(conn: sqlite3.Connection, ticker: str, frame: pd.DataFrame) -> int:
    if frame is None or frame.empty:
        return 0
    if "Close" not in frame.columns:
        return 0
    frame = frame.dropna(subset=["Close"])
    if frame.empty:
        return 0

    # Vectorised: iterrows() over ~2M price rows is minutes of pure overhead.
    dates = frame.index.strftime("%Y-%m-%d").tolist()
    closes = frame["Close"].astype(float).tolist()
    if "Open" in frame.columns:
        opens = [None if pd.isna(v) else float(v) for v in frame["Open"]]
    else:
        opens = [None] * len(closes)
    # Refuse impossible closes rather than storing them. A negative or zero
    # share price is not a price, and one stored row of it propagates into every
    # derived number - the entry price, the forward return, the daily return the
    # equity curve is built from - with nothing anywhere reporting a fault.
    #
    # A non-positive *open* is dropped to None instead of dropping the row: the
    # close is still usable, and first_on_or_after falls back to it.
    rows = []
    rejected = 0
    for d, o, cl in zip(dates, opens, closes):
        if cl is None or cl != cl or cl <= 0:   # cl != cl catches NaN
            rejected += 1
            continue
        rows.append((ticker, d, (o if o is not None and o == o and o > 0 else None), cl))
    if rejected:
        REJECTED[ticker] = REJECTED.get(ticker, 0) + rejected
    if not rows:
        return 0
    conn.executemany(
        "INSERT INTO prices (ticker, date, open, close) VALUES (?,?,?,?) "
        "ON CONFLICT(ticker, date) DO UPDATE SET open=excluded.open, close=excluded.close",
        rows)
    return len(rows)


def unusable_tickers(conn: sqlite3.Connection) -> set[str]:
    """Tickers whose stored series cannot support return arithmetic.

    Quarantines the *whole* ticker rather than the offending rows, and that is
    deliberate. Deleting DAIUF's 957 negative closes would leave its
    $0.00000028 close in place, still ready to be picked as an entry price and
    still worth +1,913% over the following year. A series that a data source has
    demonstrably got wrong cannot be trusted in the rows where the error happens
    to be invisible.

    Two sources, unioned, because neither alone is sufficient:

    * What the stored data still shows - a non-positive or sub-cent close.
    * What it *used* to show, from :data:`QUARANTINE_KEY`. Deleting the bad rows
      does not make a series trustworthy; it makes it gappy, and a return
      computed across a 1,100-day hole is not a return. Measured after the
      2026-08-17 purge: JGCCF's remaining rows imply a +1,959% single step.
      Recomputing from data alone would have quietly readmitted it.

    Callers: :func:`backtest.score_trades` skips these, which removes them from
    ``trade_performance`` and so from the strategy, the grade and the curve
    without any of those needing to know about it.
    """
    import json

    computed = {r["ticker"] for r in conn.execute(
        "SELECT DISTINCT ticker FROM prices "
        "WHERE close IS NULL OR close <= 0 OR close < ?",
        (MIN_PLAUSIBLE_PRICE,))}
    row = conn.execute("SELECT value FROM meta WHERE key = ?",
                       (QUARANTINE_KEY,)).fetchone()
    recorded = set(json.loads(row["value"])) if row and row["value"] else set()
    return computed | recorded


def purge_impossible(conn: sqlite3.Connection) -> dict:
    """Delete stored rows that :func:`_store` would now refuse.

    One-shot repair for rows written before the write-time gate existed. Safe to
    re-run: after the first pass it deletes nothing.

    Every ticker it touches is recorded in :data:`QUARANTINE_KEY` *before* the
    delete, so removing the evidence does not remove the verdict. Sub-cent closes
    are left in place because they are not impossible, only unusable, and
    deleting them would make a broken series look healthy.
    """
    import json

    before = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    affected = {r["ticker"] for r in conn.execute(
        "SELECT DISTINCT ticker FROM prices WHERE close IS NULL OR close <= 0")}

    row = conn.execute("SELECT value FROM meta WHERE key = ?",
                       (QUARANTINE_KEY,)).fetchone()
    known = set(json.loads(row["value"])) if row and row["value"] else set()
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (QUARANTINE_KEY, json.dumps(sorted(known | affected))))

    conn.execute("DELETE FROM prices WHERE close IS NULL OR close <= 0")
    # A non-positive open with a usable close: keep the row, lose the open.
    opens = conn.execute(
        "UPDATE prices SET open = NULL WHERE open IS NOT NULL AND open <= 0").rowcount
    after = conn.execute("SELECT COUNT(*) FROM prices").fetchone()[0]
    conn.commit()
    return {"deleted": before - after, "opens_cleared": opens,
            "tickers": sorted(affected), "quarantined": sorted(known | affected)}


def top_up(conn: sqlite3.Connection, progress: bool = True,
           max_history_days: int = 400) -> dict:
    """Extend already-cached series to the present.

    :func:`sync_prices` deliberately skips any ticker it has downloaded before.
    That is right for the first build and wrong every day after it, because the
    backtest enters a position **the session after the trade is disclosed**: a
    filing that lands before the price series reaches that date scores as having
    no entry price, and drops out of the strategy silently rather than loudly.

    Measured on 2026-08-12: the corpus was frozen at 2026-08-07, Gottheimer
    disclosed on 08-10, the entry target was 08-11, and all eight of his in-remit
    purchases were marked untradable - not because anything was wrong with them,
    but because the prices were two sessions behind.

    Only the missing tail is fetched. ``_store`` upserts on (ticker, date), so
    re-fetching the overlap is harmless, and tickers are grouped by the date they
    need so the common case is one span for almost the whole corpus.

    Delisted names are not chased every night. Members traded companies that
    have since been acquired - Express Scripts, Time Warner, Mazor Robotics -
    and those series simply end. Retrying them daily costs requests, slows the
    run, and makes yfinance narrate a page of "possibly delisted" complaints
    that read like a crash. A series that has not moved in
    :data:`DELISTED_AFTER_DAYS` is treated as closed and only re-checked every
    :data:`RECHECK_EVERY_DAYS`, in case the assumption is wrong.
    """
    import collections

    import yfinance as yf

    wanted = set(tickers_to_fetch(conn))
    today = dt.date.today()
    # One day past today: yfinance treats `end` as exclusive.
    end = today + dt.timedelta(days=1)
    floor = today - dt.timedelta(days=max_history_days)

    # When each ticker was last attempted, so a dormant one can be rested.
    last_try = {r["ticker"]: r["checked_at"] for r in
                conn.execute("SELECT ticker, checked_at FROM ticker_status")}
    recheck_before = (dt.datetime.now()
                      - dt.timedelta(days=RECHECK_EVERY_DAYS)).isoformat()

    groups: dict[str, list[str]] = collections.defaultdict(list)
    resting = 0
    for r in conn.execute("SELECT ticker, MAX(date) AS last FROM prices "
                          "GROUP BY ticker"):
        ticker = r["ticker"]
        if ticker not in wanted:
            continue
        last = dt.date.fromisoformat(r["last"])
        if last >= today:
            continue
        if (today - last).days > DELISTED_AFTER_DAYS:
            seen = last_try.get(ticker)
            if seen and seen > recheck_before:
                resting += 1
                continue
        groups[max(last, floor).isoformat()].append(ticker)

    stats = {"stale": sum(len(v) for v in groups.values()),
             "extended": 0, "rows": 0, "spans": len(groups),
             "resting": resting, "went_quiet": 0}
    if progress and stats["stale"]:
        print(f"  topping up {stats['stale']:,} ticker(s) across "
              f"{len(groups)} span(s)"
              + (f"; {resting} delisted and resting" if resting else ""))

    now = dt.datetime.now().isoformat(timespec="seconds")
    for start, tickers in sorted(groups.items()):
        for i in range(0, len(tickers), BATCH_SIZE):
            batch = tickers[i:i + BATCH_SIZE]
            try:
                with _quiet_yfinance():
                    data = yf.download(batch, start=start, end=end.isoformat(),
                                       auto_adjust=True, progress=False,
                                       group_by="ticker", threads=True,
                                       actions=False)
            except Exception as exc:  # noqa: BLE001
                print(f"  top-up batch failed ({start}): {exc}", flush=True)
                continue
            for ticker in batch:
                try:
                    if len(batch) == 1:
                        frame = data
                    elif ticker not in data.columns.get_level_values(0):
                        frame = None
                    else:
                        frame = data[ticker]
                except Exception:  # noqa: BLE001
                    frame = None
                n = _store(conn, ticker, frame)
                stats["rows"] += n
                if n:
                    stats["extended"] += 1
                else:
                    stats["went_quiet"] += 1
                # Record the attempt either way, so a ticker that has stopped
                # trading can be rested rather than retried every night.
                conn.execute(
                    """INSERT INTO ticker_status (ticker, resolved, checked_at)
                       VALUES (?, ?, ?) ON CONFLICT(ticker) DO UPDATE SET
                       checked_at = excluded.checked_at""",
                    (ticker, 1 if n else 0, now))
    conn.commit()
    stats.update(_retry_recent_singly(conn, end, progress=progress))
    return stats


# A batched yfinance download drops the newest bar for some tickers while
# returning it for others in the same call - verified 2026-08-12, when CRWD,
# PANW and SAIL came back one session short but NICE and SPY did not, and a
# single-ticker request returned the missing bar for all five. Yahoo aligns a
# multi-ticker frame on a union index and leaves the unsettled bar NaN.
#
# That newest session is exactly the one a just-disclosed trade enters on, so
# the tickers that matter are re-requested one at a time. Only those referenced
# by a recent unscored purchase, so this stays a handful of calls rather than
# thousands.
RETRY_WINDOW_DAYS = 45
MAX_SINGLE_RETRIES = 120


def _retry_recent_singly(conn: sqlite3.Connection, end: dt.date,
                         progress: bool = True) -> dict:
    import yfinance as yf

    cutoff = (dt.date.today() - dt.timedelta(days=RETRY_WINDOW_DAYS)).isoformat()
    rows = conn.execute("""
        SELECT DISTINCT t.ticker
          FROM transactions t
         WHERE t.tx_type = 'purchase' AND t.ticker IS NOT NULL
           AND COALESCE(t.tx_date_fixed, t.tx_date) >= ?
           AND NOT EXISTS (SELECT 1 FROM trade_performance p
                            WHERE p.transaction_id = t.id
                              AND p.entry_date IS NOT NULL)
    """, (cutoff,)).fetchall()

    stats = {"retried": 0, "retry_rows": 0}
    pending = [normalize_ticker(r["ticker"]) for r in rows][:MAX_SINGLE_RETRIES]
    if not pending:
        return stats
    if progress:
        print(f"  re-requesting {len(pending)} recent ticker(s) individually")

    start = (dt.date.today() - dt.timedelta(days=RETRY_WINDOW_DAYS)).isoformat()
    for ticker in pending:
        try:
            with _quiet_yfinance():
                frame = yf.download(ticker, start=start, end=end.isoformat(),
                                    auto_adjust=True, progress=False,
                                    actions=False)
        except Exception:  # noqa: BLE001
            continue
        # A single-ticker frame still carries a ticker level in some yfinance
        # versions; flatten it so _store sees plain 'Close'/'Open' columns.
        if frame is not None and getattr(frame.columns, "nlevels", 1) > 1:
            frame = frame.droplevel(-1, axis=1)
        n = _store(conn, ticker, frame)
        stats["retry_rows"] += n
        if n:
            stats["retried"] += 1
    conn.commit()
    return stats


def sync_prices(conn: sqlite3.Connection, force: bool = False,
                progress: bool = True) -> dict:
    """Download price history for every traded ticker that is not cached yet."""
    import yfinance as yf

    wanted = tickers_to_fetch(conn)
    start, end = _date_bounds(conn)

    if force:
        pending = wanted
    else:
        have = {r["ticker"] for r in conn.execute(
            "SELECT DISTINCT ticker FROM prices")}
        checked = {r["ticker"] for r in conn.execute(
            "SELECT ticker FROM ticker_status WHERE resolved = 0")}
        pending = [t for t in wanted if t not in have and t not in checked]

    stats = {"wanted": len(wanted), "pending": len(pending),
             "resolved": 0, "unresolved": 0, "rows": 0}
    now = dt.datetime.now().isoformat(timespec="seconds")

    for i in range(0, len(pending), BATCH_SIZE):
        batch = pending[i:i + BATCH_SIZE]
        try:
            with _quiet_yfinance():
                data = yf.download(batch, start=start.isoformat(),
                                   end=end.isoformat(), auto_adjust=True,
                                   progress=False, group_by="ticker",
                                   threads=True, actions=False)
        except Exception as exc:  # noqa: BLE001
            print(f"  batch {i // BATCH_SIZE} failed: {exc}", flush=True)
            continue

        for ticker in batch:
            try:
                if len(batch) == 1:
                    frame = data
                else:
                    if ticker not in data.columns.get_level_values(0):
                        frame = None
                    else:
                        frame = data[ticker]
            except Exception:  # noqa: BLE001
                frame = None

            n = _store(conn, ticker, frame)
            stats["rows"] += n
            if n:
                stats["resolved"] += 1
                bounds = conn.execute(
                    "SELECT MIN(date) a, MAX(date) b FROM prices WHERE ticker=?",
                    (ticker,)).fetchone()
                conn.execute(
                    """INSERT INTO ticker_status (ticker, resolved, first_date, last_date, checked_at)
                       VALUES (?,1,?,?,?) ON CONFLICT(ticker) DO UPDATE SET
                       resolved=1, first_date=excluded.first_date,
                       last_date=excluded.last_date, checked_at=excluded.checked_at""",
                    (ticker, bounds["a"], bounds["b"], now))
            else:
                stats["unresolved"] += 1
                conn.execute(
                    """INSERT INTO ticker_status (ticker, resolved, checked_at)
                       VALUES (?,0,?) ON CONFLICT(ticker) DO UPDATE SET
                       resolved=0, checked_at=excluded.checked_at""", (ticker, now))

        conn.commit()
        if progress:
            print(f"  prices {min(i + BATCH_SIZE, len(pending))}/{len(pending)} "
                  f"({stats['rows']} rows)", flush=True)

    return stats


class PriceLookup:
    """In-memory price series with as-of lookups, built once per backtest run."""

    def __init__(self, conn: sqlite3.Connection, tickers: list[str] | None = None):
        """Load price series into memory.

        Pass ``tickers`` to load only what you need - the full table runs to
        millions of rows, which is far too much to rebuild per web request.
        """
        self.series: dict[str, list[tuple[str, float, float | None]]] = {}
        sql = "SELECT ticker, date, open, close FROM prices"
        params: tuple = ()
        if tickers is not None:
            wanted = sorted({normalize_ticker(t) for t in tickers} | {BENCHMARK})
            sql += f" WHERE ticker IN ({','.join('?' * len(wanted))})"
            params = tuple(wanted)
        sql += " ORDER BY ticker, date"

        for r in conn.execute(sql, params):
            self.series.setdefault(r["ticker"], []).append(
                (r["date"], r["close"], r["open"]))
        # Dates are shared string objects with the tuples above, so this index
        # costs a list of pointers rather than a second copy of the data.
        self._dates: dict[str, list[str]] = {
            t: [d for d, _, _ in v] for t, v in self.series.items()}

    def has(self, ticker: str) -> bool:
        return normalize_ticker(ticker) in self.series

    def first_on_or_after(self, ticker: str, date: str) -> tuple[str, float] | None:
        """Price on the first trading day at or after ``date`` (the entry)."""
        import bisect

        t = normalize_ticker(ticker)
        dates = self._dates.get(t)
        if not dates:
            return None
        i = bisect.bisect_left(dates, date)
        if i >= len(dates):
            return None
        d, close, open_ = self.series[t][i]
        # Prefer the open: a copier reacting to a disclosure buys at the next
        # open, not at that day's close.
        return d, (open_ if open_ else close)

    def last_on_or_before(self, ticker: str, date: str) -> tuple[str, float] | None:
        """Closing price on the last trading day at or before ``date`` (the exit)."""
        import bisect

        t = normalize_ticker(ticker)
        dates = self._dates.get(t)
        if not dates:
            return None
        i = bisect.bisect_right(dates, date) - 1
        if i < 0:
            return None
        d, close, _ = self.series[t][i]
        return d, close

    def coverage(self, ticker: str) -> tuple[str, str] | None:
        t = normalize_ticker(ticker)
        dates = self._dates.get(t)
        if not dates:
            return None
        return dates[0], dates[-1]
