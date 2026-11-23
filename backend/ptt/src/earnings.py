"""Earnings announcement dates, and the surprise when they landed.

Fetched from Yahoo, which carries roughly 100 quarters per ticker - back to
2002 on the large names - together with the consensus EPS estimate, the reported
EPS and the surprise percentage.  That is more than a calendar: it allows the
question to be "did they buy before a *beat*", not merely "did they buy before
an announcement".

Resumable in the same shape as :func:`prices.sync_prices`: a ticker already
fetched is skipped unless ``force``, tickers that return nothing are recorded so
they are not retried every night, and a throttle stops the run rather than
poisoning the table with empty rows.

Timezone note.  Yahoo returns a timezone-aware timestamp in US market time, and
the announcement is usually *after* the close on that date.  Only the date is
kept, and every comparison in :mod:`earnings_study` treats an announcement as
happening at the end of its date, so a trade on the announcement date counts as
before the news rather than after it.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
import warnings

warnings.filterwarnings("ignore")

SCHEMA = """
CREATE TABLE IF NOT EXISTS earnings (
    ticker        TEXT NOT NULL,
    date          TEXT NOT NULL,
    eps_estimate  REAL,
    eps_reported  REAL,
    surprise_pct  REAL,
    UNIQUE(ticker, date)
);
CREATE INDEX IF NOT EXISTS ix_earnings_ticker ON earnings(ticker, date);
CREATE TABLE IF NOT EXISTS earnings_status (
    ticker     TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL,
    n_rows     INTEGER NOT NULL DEFAULT 0
);
"""

LIMIT = 100          # quarters per ticker; Yahoo caps out around here anyway
THROTTLE_STRIKES = 8  # consecutive empties that mean "rate limited", not "no data"


def ensure(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def tickers_wanted(conn: sqlite3.Connection, in_remit_only: bool = False) -> list[str]:
    """Tickers worth fetching: those a member actually traded and we can price."""
    sql = """SELECT DISTINCT t.ticker
               FROM transactions t
               JOIN trade_performance p ON p.transaction_id = t.id
              WHERE t.ticker IS NOT NULL AND t.ticker <> ''"""
    rows = [r[0] for r in conn.execute(sql)]
    priced = {r[0] for r in conn.execute("SELECT DISTINCT ticker FROM prices")}
    return sorted(t for t in rows if t in priced)


def sync(conn: sqlite3.Connection, tickers: list[str] | None = None,
         force: bool = False, progress: bool = True) -> dict:
    import yfinance as yf

    ensure(conn)
    wanted = tickers if tickers is not None else tickers_wanted(conn)
    done = {r[0] for r in conn.execute("SELECT ticker FROM earnings_status")}
    todo = [t for t in wanted if force or t not in done]

    stats = {"requested": len(todo), "resolved": 0, "rows": 0, "empty": 0,
             "stopped_early": False}
    strikes = 0
    now = dt.datetime.now().isoformat(timespec="seconds")

    for i, tk in enumerate(todo, 1):
        try:
            df = yf.Ticker(tk).get_earnings_dates(limit=LIMIT)
        except Exception:
            df = None
        rows = []
        if df is not None and not df.empty:
            for ts, row in df.iterrows():
                try:
                    d = ts.date().isoformat()
                except Exception:
                    continue
                def num(v):
                    try:
                        f = float(v)
                        return None if f != f else f
                    except (TypeError, ValueError):
                        return None
                rows.append((tk, d, num(row.get("EPS Estimate")),
                             num(row.get("Reported EPS")),
                             num(row.get("Surprise(%)"))))
        if rows:
            conn.executemany(
                """INSERT INTO earnings (ticker, date, eps_estimate,
                                         eps_reported, surprise_pct)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(ticker, date) DO UPDATE SET
                     eps_estimate=excluded.eps_estimate,
                     eps_reported=excluded.eps_reported,
                     surprise_pct=excluded.surprise_pct""", rows)
            stats["resolved"] += 1
            stats["rows"] += len(rows)
            strikes = 0
        else:
            stats["empty"] += 1
            strikes += 1
        conn.execute(
            """INSERT INTO earnings_status (ticker, checked_at, n_rows)
               VALUES (?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET checked_at=excluded.checked_at,
                                                 n_rows=excluded.n_rows""",
            (tk, now, len(rows)))
        if i % 25 == 0:
            conn.commit()
            if progress:
                print(f"  {i}/{len(todo)}  {stats['resolved']} resolved, "
                      f"{stats['rows']:,} rows", flush=True)
        # A long run of empties is Yahoo throttling, not a run of obscure
        # tickers. Stopping leaves the rest for the next run instead of
        # recording thousands of false "no earnings" verdicts.
        if strikes >= THROTTLE_STRIKES:
            stats["stopped_early"] = True
            break
    conn.commit()
    return stats


def load(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """ticker -> sorted announcement dates."""
    out: dict[str, list[str]] = {}
    for tk, d in conn.execute("SELECT ticker, date FROM earnings ORDER BY ticker, date"):
        out.setdefault(tk, []).append(d)
    return out
