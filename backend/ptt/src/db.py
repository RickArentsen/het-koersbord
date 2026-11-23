"""SQLite storage for filings, transactions, legislators, committees and prices."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS filings (
    doc_id           TEXT PRIMARY KEY,
    year             INTEGER,
    filer_last       TEXT,
    filer_first      TEXT,
    member_name      TEXT,
    state_district   TEXT,
    filing_date      TEXT,
    parse_status     TEXT,
    parse_error      TEXT,
    n_transactions   INTEGER DEFAULT 0,
    pdf_url          TEXT,
    ingested_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id              TEXT NOT NULL,
    seq                 INTEGER NOT NULL,
    bioguide_id         TEXT,
    member_name         TEXT,
    state_district      TEXT,
    owner               TEXT,
    asset_name          TEXT,
    ticker              TEXT,
    asset_type          TEXT,
    tx_type             TEXT,
    tx_date             TEXT,
    notification_date   TEXT,
    filing_date         TEXT,
    amount_raw          TEXT,
    amount_low          INTEGER,
    amount_high         INTEGER,
    amount_mid          REAL,
    cap_gains_over_200  INTEGER DEFAULT 0,
    description         TEXT,
    UNIQUE(doc_id, seq),
    FOREIGN KEY(doc_id) REFERENCES filings(doc_id)
);

CREATE INDEX IF NOT EXISTS ix_tx_ticker  ON transactions(ticker);
CREATE INDEX IF NOT EXISTS ix_tx_member  ON transactions(bioguide_id);
CREATE INDEX IF NOT EXISTS ix_tx_date    ON transactions(tx_date);
CREATE INDEX IF NOT EXISTS ix_tx_notif   ON transactions(notification_date);
CREATE INDEX IF NOT EXISTS ix_tx_type    ON transactions(tx_type);

CREATE TABLE IF NOT EXISTS legislators (
    bioguide_id   TEXT PRIMARY KEY,
    full_name     TEXT,
    first_name    TEXT,
    last_name     TEXT,
    chamber       TEXT,
    state         TEXT,
    district      TEXT,
    state_district TEXT,
    party         TEXT,
    start_date    TEXT,
    end_date      TEXT
);

CREATE TABLE IF NOT EXISTS committees (
    committee_id   TEXT PRIMARY KEY,
    name           TEXT,
    chamber        TEXT,
    parent_id      TEXT,
    sensitivity    REAL DEFAULT 0,
    sectors        TEXT
);

CREATE TABLE IF NOT EXISTS committee_memberships (
    bioguide_id   TEXT NOT NULL,
    committee_id  TEXT NOT NULL,
    title         TEXT,
    rank          INTEGER,
    PRIMARY KEY (bioguide_id, committee_id)
);

-- Committee membership as it stood at a series of past dates, reconstructed
-- from the git history of congress-legislators. The project publishes only a
-- *current* membership file, which means a 2021 trade would otherwise be judged
-- against the seat its member holds today - crediting them with a committee
-- they had not yet joined. See congress.sync_committee_history.
CREATE TABLE IF NOT EXISTS committee_membership_history (
    snapshot_date TEXT NOT NULL,
    -- The commit date the membership was actually read from. Equal to
    -- snapshot_date normally; later when the file was mid-rebuild at the start
    -- of a Congress, which is look-ahead and must stay visible.
    source_date   TEXT,
    committee_id  TEXT NOT NULL,
    bioguide_id   TEXT NOT NULL,
    title         TEXT,
    PRIMARY KEY (snapshot_date, committee_id, bioguide_id)
);
CREATE INDEX IF NOT EXISTS ix_cmh_member
    ON committee_membership_history(bioguide_id, snapshot_date);
CREATE INDEX IF NOT EXISTS ix_cmh_date
    ON committee_membership_history(snapshot_date);

-- Email digest subscribers. The token is the only credential: it appears in
-- confirm, preferences and unsubscribe links, so no address is ever exposed in
-- a URL and no login is needed to leave.
CREATE TABLE IF NOT EXISTS subscribers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL UNIQUE,
    token           TEXT NOT NULL UNIQUE,
    want_large      INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    confirmed_at    TEXT,
    unsubscribed_at TEXT,
    consent_ip      TEXT,
    last_sent_at    TEXT,
    bounces         INTEGER DEFAULT 0
);

-- What each subscriber has already been sent. A per-subscriber high-water mark
-- would be wrong: filings are amended and back-filled, so "everything newer
-- than X" either re-sends or silently skips.
--
-- item_key is "<doc_id>:<seq>" (see strategy.line_key), NOT transactions.id.
-- Re-parsing a filing deletes and re-inserts its rows, so the surrogate id
-- changes for trades that did not - one scan re-keyed 8,745 rows going back to
-- 2018. Keyed on the id, this table would forget what it had sent on exactly
-- the amendments it exists to survive.
CREATE TABLE IF NOT EXISTS sent_items (
    subscriber_id  INTEGER NOT NULL,
    item_key       TEXT NOT NULL,
    sent_at        TEXT NOT NULL,
    PRIMARY KEY (subscriber_id, item_key)
);
CREATE INDEX IF NOT EXISTS ix_sent_sub ON sent_items(subscriber_id);

CREATE TABLE IF NOT EXISTS prices (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,
    open    REAL,
    close   REAL,
    PRIMARY KEY (ticker, date)
);

-- Populated by src/sectors.py, but declared here so queries that join it work
-- before the sector classifier has ever been run.
CREATE TABLE IF NOT EXISTS ticker_meta (
    ticker       TEXT PRIMARY KEY,
    yf_sector    TEXT,
    yf_industry  TEXT,
    sectors      TEXT,
    checked_at   TEXT
);

CREATE TABLE IF NOT EXISTS ticker_status (
    ticker       TEXT PRIMARY KEY,
    resolved     INTEGER DEFAULT 0,
    first_date   TEXT,
    last_date    TEXT,
    checked_at   TEXT
);

CREATE TABLE IF NOT EXISTS trade_performance (
    transaction_id  INTEGER PRIMARY KEY,
    entry_date      TEXT,
    entry_price     REAL,
    -- What the member themselves got, entering on their own trade date.  Not
    -- investable by anyone else; kept so the cost of the disclosure lag can be
    -- measured rather than assumed.
    member_ret_3m   REAL,
    member_bench_3m REAL,
    ret_1m          REAL,
    ret_3m          REAL,
    ret_6m          REAL,
    ret_12m         REAL,
    bench_1m        REAL,
    bench_3m        REAL,
    bench_6m        REAL,
    bench_12m       REAL,
    FOREIGN KEY(transaction_id) REFERENCES transactions(id)
);

CREATE TABLE IF NOT EXISTS politician_stats (
    bioguide_id      TEXT PRIMARY KEY,
    member_name      TEXT,
    n_buys           INTEGER,
    n_scored         INTEGER,
    hit_rate         REAL,
    avg_ret_3m       REAL,
    avg_alpha_1m     REAL,
    avg_alpha_3m     REAL,
    avg_alpha_6m     REAL,
    avg_alpha_12m    REAL,
    median_alpha_3m  REAL,
    total_alpha_3m_t REAL,
    avg_lag_days     REAL,
    avg_member_alpha_3m REAL,
    lag_cost_3m      REAL,
    est_volume       REAL,
    sensitivity      REAL,
    committees       TEXT,
    computed_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
    key    TEXT PRIMARY KEY,
    value  TEXT
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open the database, creating the schema if needed."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the dashboard read while an ingest is still writing; the busy
    # timeout keeps a concurrent writer from failing outright on a lock.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first release.  CREATE TABLE IF NOT EXISTS will not
# add a column to a table that already exists, so bring older databases forward
# rather than making the user delete and re-ingest.
_ADDED_COLUMNS = {
    # When the single "you never confirmed" nudge went out. A stamp rather than
    # a counter, because the rule is one reminder ever and a stamp cannot drift
    # into meaning something else. NULL means not yet reminded; it is never
    # cleared, so a confirm-then-unsubscribe-then-resubscribe cycle cannot earn
    # a second nudge.
    "subscribers": [
        ("reminded_at", "TEXT"),
    ],
    # Members who have left Congress. They are carried so their filings stop
    # being orphaned, but they hold no committee seat in any current source, so
    # they can never be "in remit" and never enter the strategy.
    "legislators": [
        ("is_former", "INTEGER DEFAULT 0"),
        ("last_term_end", "TEXT"),
    ],
    # A trade date the calendar rejects is kept exactly as filed; the repair and
    # its reasoning live beside it so the site can show a correction rather than
    # silently editing a public document. See src/dates.py.
    "transactions": [
        ("tx_date_fixed", "TEXT"),
        ("tx_date_note", "TEXT"),
    ],
    "trade_performance": [
        ("member_ret_3m", "REAL"),
        ("member_bench_3m", "REAL"),
        # Long holding periods. The filing history reaches back to 2020, so an
        # early trade has years of outcome that stopping at 12m would discard.
        *[(f"{p}_{h}", "REAL")
          for h in ("18m", "24m", "36m", "48m", "60m")
          for p in ("ret", "bench")],
    ],
    "politician_stats": [
        ("avg_member_alpha_3m", "REAL"),
        ("lag_cost_3m", "REAL"),
        # Dollar-weighted alpha: disclosed sizes span $1k to $5m, so an
        # equal-weighted mean is dominated by the smallest positions.
        ("wt_alpha_3m", "REAL"),
        ("wt_alpha_12m", "REAL"),
        ("n_scored_12m", "INTEGER"),
        ("hit_rate_12m", "REAL"),
        ("alpha_12m_t", "REAL"),
        ("median_alpha_12m", "REAL"),
        # Options are scored as a directional signal on the underlying, not as
        # a replication, so they are counted separately.
        ("n_options", "INTEGER"),
        # Buys that fall inside the member's own committee jurisdiction.
        ("n_in_remit", "INTEGER"),
        ("alpha_in_remit_12m", "REAL"),
        ("alpha_out_remit_12m", "REAL"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    _rekey_sent_items(conn)
    conn.commit()


def _rekey_sent_items(conn: sqlite3.Connection) -> None:
    """Move sent_items off transactions.id onto the stable (doc_id, seq) key.

    Changing a primary key needs a table rebuild, not ALTER TABLE.  Rows whose
    transaction_id still resolves are translated; rows whose id has already been
    recycled by a re-ingest cannot be translated and are dropped, because there
    is no way to know which trade they referred to.  Dropping them risks sending
    a duplicate once; keeping them risks suppressing an unrelated trade forever,
    which is worse and silent.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(sent_items)")}
    if "transaction_id" not in cols:
        return

    before = conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0]
    conn.executescript("""
        CREATE TABLE sent_items_new (
            subscriber_id  INTEGER NOT NULL,
            item_key       TEXT NOT NULL,
            sent_at        TEXT NOT NULL,
            PRIMARY KEY (subscriber_id, item_key)
        );
        INSERT OR IGNORE INTO sent_items_new (subscriber_id, item_key, sent_at)
             SELECT s.subscriber_id, t.doc_id || ':' || t.seq, s.sent_at
               FROM sent_items s JOIN transactions t ON t.id = s.transaction_id;
        DROP TABLE sent_items;
        ALTER TABLE sent_items_new RENAME TO sent_items;
        CREATE INDEX IF NOT EXISTS ix_sent_sub ON sent_items(subscriber_id);
    """)
    after = conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0]
    if before:
        print(f"  sent_items re-keyed onto (doc_id, seq): {after} of {before} "
              f"carried over" + (f", {before - after} unresolvable"
                                 if before > after else ""))


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO meta(key, value) VALUES(?,?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default
