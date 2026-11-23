"""Map traded tickers onto the sector taxonomy used for committee jurisdiction.

The point of this module is the overlap flag: a member of the Armed Services
Committee buying a defence contractor is a materially more interesting event
than the same member buying an S&P 500 index fund.  :func:`overlapping_sectors`
computes that intersection.

Yahoo's ``sector`` alone is not sufficient - Lockheed Martin is sector
"Industrials", and only its ``industry`` field ("Aerospace & Defense") reveals
what a defence-committee seat actually overlaps with.  Both fields are used.

An overlap is context, not an accusation: buying a stock your committee oversees
is legal, common, and usually innocuous.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time

# (sector, industry) -> the committee-jurisdiction sectors used in congress.py.
SECTOR_MAP = {
    "financial services": ("financials",),
    "real estate": ("real_estate",),
    "healthcare": ("healthcare",),
    "technology": ("technology",),
    "communication services": ("telecom", "technology"),
    "energy": ("energy",),
    "utilities": ("utilities",),
    "industrials": ("industrials",),
    "basic materials": ("materials",),
    "consumer cyclical": (),
    "consumer defensive": (),
}

# Industry keywords override or extend the sector mapping.
INDUSTRY_KEYWORDS = (
    ("aerospace", ("defense", "aerospace")),
    ("defense", ("defense",)),
    ("drug manufacturers", ("pharma", "healthcare")),
    ("biotechnology", ("pharma", "healthcare")),
    ("pharmaceutical", ("pharma", "healthcare")),
    ("medical", ("healthcare",)),
    ("healthcare", ("healthcare",)),
    ("telecom", ("telecom",)),
    ("banks", ("financials",)),
    ("capital markets", ("financials",)),
    ("insurance", ("financials",)),
    ("asset management", ("financials",)),
    ("credit services", ("financials",)),
    ("mortgage", ("financials", "real_estate")),
    ("reit", ("real_estate",)),
    ("airlines", ("transport",)),
    ("railroads", ("transport",)),
    ("trucking", ("transport",)),
    ("marine shipping", ("transport",)),
    ("integrated freight", ("transport",)),
    ("farm", ("agriculture",)),
    ("agricultural", ("agriculture", "commodities")),
    ("food distribution", ("agriculture",)),
    ("oil & gas", ("energy",)),
    ("uranium", ("energy", "materials")),
    ("gold", ("commodities", "materials")),
    ("silver", ("commodities", "materials")),
    ("copper", ("commodities", "materials")),
    ("semiconductor", ("technology",)),
    ("software", ("technology",)),
    ("internet", ("technology",)),
    ("utilities", ("utilities",)),
    ("crypto", ("crypto", "financials")),
    ("digital assets", ("crypto", "financials")),
)

# ticker_meta lives in db.SCHEMA so that queries joining it work even before
# this module has run; kept here as a no-op safety net for standalone use.
SCHEMA = """
CREATE TABLE IF NOT EXISTS ticker_meta (
    ticker       TEXT PRIMARY KEY,
    yf_sector    TEXT,
    yf_industry  TEXT,
    sectors      TEXT,
    checked_at   TEXT
);
"""


def classify(yf_sector: str | None, yf_industry: str | None) -> list[str]:
    """Translate a Yahoo sector/industry pair into jurisdiction sectors."""
    out: set[str] = set()
    sector = (yf_sector or "").strip().lower()
    industry = (yf_industry or "").strip().lower()

    out.update(SECTOR_MAP.get(sector, ()))
    for keyword, mapped in INDUSTRY_KEYWORDS:
        if keyword in industry:
            out.update(mapped)
    return sorted(out)


def _commit_with_retry(conn: sqlite3.Connection, attempts: int = 6) -> None:
    """Commit, tolerating a writer holding the database briefly.

    This loop exists because a concurrent ingest once killed a twelve-minute
    sector run outright with 'database is locked'.  Losing that much work to a
    transient lock is not acceptable, so back off and try again.
    """
    for attempt in range(attempts):
        try:
            conn.commit()
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
            time.sleep(2 * (attempt + 1))


def sync_sectors(conn: sqlite3.Connection, limit: int | None = None,
                 progress: bool = True, delay: float = 0.05) -> dict:
    """Fetch and cache sector/industry for every purchased ticker.

    A ticker is recorded only once Yahoo has answered for it, or has been shown
    to be a symbol Yahoo does not know (``market.sweep``). Before 4 November 2026
    a refused request was stored as "no sector", and since only tickers without
    a row are asked, never asked again: a run under a rate-limited crumb stored
    all 2,104 that way, and the in-remit strategy came out empty.
    """
    import json

    import market

    conn.executescript(SCHEMA)
    rows = conn.execute(
        """SELECT DISTINCT t.ticker FROM transactions t
            WHERE t.tx_type='purchase' AND t.ticker IS NOT NULL AND t.ticker <> ''
              AND t.ticker NOT IN (SELECT ticker FROM ticker_meta)
         ORDER BY t.ticker""").fetchall()
    pending = [r["ticker"] for r in rows]
    if limit:
        pending = pending[:limit]

    stats = {"pending": len(pending), "resolved": 0, "unknown": 0,
             "cached": 0, "left": 0, "stopped_early": False}
    now = dt.datetime.now().isoformat(timespec="seconds")

    def store(ticker: str, info: dict | None) -> None:
        sector = (info or {}).get("sector")
        industry = (info or {}).get("industry")
        mapped = classify(sector, industry)
        if mapped:
            stats["resolved"] += 1
        else:
            stats["unknown"] += 1
        conn.execute(
            """INSERT INTO ticker_meta (ticker, yf_sector, yf_industry, sectors, checked_at)
               VALUES (?,?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET
                 yf_sector=excluded.yf_sector, yf_industry=excluded.yf_industry,
                 sectors=excluded.sectors, checked_at=excluded.checked_at""",
            (ticker, sector, industry, ",".join(mapped), now))

    # The market-cap sweep keeps the same two fields from the same Yahoo
    # answer: a ticker it already has costs no request here.
    cache: dict[str, dict] = {}
    try:
        for sym, payload in conn.execute("SELECT ticker, payload FROM market_meta"):
            d = json.loads(payload or "{}")
            if d.get("sector") or d.get("industry"):
                cache[sym] = d
    except sqlite3.OperationalError:          # no market_meta yet
        pass
    ask = []
    for ticker in pending:
        hit = cache.get(market._yf_symbol(ticker))
        if hit:
            store(ticker, hit)
            stats["cached"] += 1
        else:
            ask.append(ticker)
    _commit_with_retry(conn)

    def every(i: int) -> None:
        if i % 50 == 0:
            _commit_with_retry(conn)
            if progress:
                print(f"  sectors {i}/{len(ask)} asked "
                      f"({stats['resolved']} resolved)", flush=True)

    left = market.sweep(ask, market._yf_symbol, store, pause=delay, every=every)
    _commit_with_retry(conn)
    if left:
        stats.update(left=left, stopped_early=True)
        if progress:
            print(f"  stopping: Yahoo is refusing requests (the canary {market.CANARY} "
                  f"got no answer either); {left} tickers left for the next run.", flush=True)
    return stats


def overlapping_sectors(conn: sqlite3.Connection, bioguide_id: str,
                        ticker: str) -> list[str]:
    """Sectors shared between a member's committee remit and a ticker."""
    import congress

    _score, _names, member_sectors = congress.member_access_score(conn, bioguide_id)
    if not member_sectors:
        return []
    row = conn.execute("SELECT sectors FROM ticker_meta WHERE ticker=?",
                       (ticker,)).fetchone()
    if not row or not row["sectors"]:
        return []
    return sorted(set(member_sectors) & set(row["sectors"].split(",")))


def annotate(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    """Attach an ``overlap`` list to each trade row, for the feed and alerts."""
    try:
        conn.execute("SELECT 1 FROM ticker_meta LIMIT 1")
    except sqlite3.OperationalError:
        for r in rows:
            r["overlap"] = []
        return rows

    ticker_sectors = {r["ticker"]: set((r["sectors"] or "").split(",")) - {""}
                      for r in conn.execute("SELECT ticker, sectors FROM ticker_meta")}

    import congress
    cache: dict[str, set[str]] = {}
    for r in rows:
        bioguide = r.get("bioguide_id")
        ticker = r.get("ticker")
        if not bioguide or not ticker:
            r["overlap"] = []
            continue
        if bioguide not in cache:
            _s, _n, sectors = congress.member_access_score(conn, bioguide)
            cache[bioguide] = set(sectors)
        r["overlap"] = sorted(cache[bioguide] & ticker_sectors.get(ticker, set()))
    return rows
