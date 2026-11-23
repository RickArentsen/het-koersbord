"""
SQLite store. Transactions are the only truth; everything else is cache.

Design rules enforced here:
  * external_id is UNIQUE  -> re-importing the same file is a no-op
  * trade_dt is the TRADE date, never the import date
  * the FX rate that applied is stored on the row, never re-derived
  * every cached price/fx row carries its source and fetch time
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, date
from typing import Optional, Iterable

DB_PATH = os.environ.get(
    "PF_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "portfolio.db"),
)

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS instrument (
    isin        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,              -- EQUITY | ETF | DERIVATIVE | CRYPTO | CASH
    currency    TEXT,
    exchange    TEXT,
    underlying  TEXT,
    underlying_ticker TEXT,
    ticker      TEXT,                       -- resolved quote symbol
    ticker_src  TEXT,                       -- seed | yahoo | manual
    sector      TEXT,
    country     TEXT,
    sector_override  TEXT,
    country_override TEXT,
    manual_price     REAL,                  -- for instruments with no quote feed
    manual_price_at  TEXT
);

CREATE TABLE IF NOT EXISTS txn (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT UNIQUE NOT NULL,
    account     TEXT NOT NULL DEFAULT 'DEGIRO',
    kind        TEXT NOT NULL,
    trade_dt    TEXT NOT NULL,
    settle_date TEXT,
    isin        TEXT,
    product     TEXT,
    quantity    REAL,
    price       REAL,
    currency    TEXT,
    local_value REAL,
    value_eur   REAL,
    fx_rate     REAL,
    autofx_cost_eur REAL DEFAULT 0,
    fee_eur     REAL DEFAULT 0,
    total_eur   REAL,
    exchange    TEXT,
    venue       TEXT,
    order_id    TEXT,
    balance     REAL,
    balance_ccy TEXT,
    seq         INTEGER,
    source      TEXT,
    note        TEXT,
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_txn_isin ON txn(isin);
CREATE INDEX IF NOT EXISTS ix_txn_dt   ON txn(trade_dt);

CREATE TABLE IF NOT EXISTS price (
    ticker     TEXT NOT NULL,
    d          TEXT NOT NULL,
    close      REAL NOT NULL,
    currency   TEXT,
    source     TEXT,
    fetched_at TEXT,
    PRIMARY KEY (ticker, d)
);

CREATE TABLE IF NOT EXISTS fx (
    pair       TEXT NOT NULL,               -- e.g. USDEUR
    d          TEXT NOT NULL,
    rate       REAL NOT NULL,
    source     TEXT,
    fetched_at TEXT,
    PRIMARY KEY (pair, d)
);

CREATE TABLE IF NOT EXISTS snapshot (
    d            TEXT PRIMARY KEY,
    total_eur    REAL,
    cost_eur     REAL,
    cash_eur     REAL,
    net_flow_eur REAL,
    created_at   TEXT
);

CREATE TABLE IF NOT EXISTS snapshot_position (
    d          TEXT NOT NULL,
    isin       TEXT NOT NULL,
    qty        REAL,
    price      REAL,
    value_eur  REAL,
    PRIMARY KEY (d, isin)
);

CREATE TABLE IF NOT EXISTS setting (
    k TEXT PRIMARY KEY,
    v TEXT
);

-- Fetched documents that are expensive to get and fine to serve slightly
-- stale: news, the macro week, the earnings week. Cached here rather than in
-- memory so a restart does not leave the home screen blank while three
-- providers are re-queried.
CREATE TABLE IF NOT EXISTS cache (
    k          TEXT PRIMARY KEY,
    v          TEXT NOT NULL,
    fetched_at REAL NOT NULL
);

-- Your thesis on a security, and dated notes against it. Append-only: a new
-- version is a new row, so "what you said last time" is never overwritten.
-- Keyed by quote symbol rather than ISIN because the research page works for
-- names you have never owned.
CREATE TABLE IF NOT EXISTS thesis (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'thesis',   -- thesis | note
    created     TEXT NOT NULL,
    stance      TEXT,                             -- bull | bear | neutral | watch
    conviction  INTEGER,
    target      REAL,
    horizon     TEXT,
    review_date TEXT,
    thesis      TEXT,
    drivers     TEXT,
    risks       TEXT,
    kill        TEXT,
    body        TEXT,
    price_at    REAL,
    currency    TEXT
);
CREATE INDEX IF NOT EXISTS ix_thesis_sym ON thesis(symbol);

-- Alert rules you set yourself, alongside the ones derived from the book.
CREATE TABLE IF NOT EXISTS alert_rule (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    isin     TEXT,
    ticker   TEXT,
    kind     TEXT NOT NULL,        -- price_below | price_above | move_pct | weight_above
    value    REAL NOT NULL,
    note     TEXT,
    enabled  INTEGER NOT NULL DEFAULT 1,
    created  TEXT
);

-- Earnings (ERN). Sources you add by hand — a slides link from the IR site,
-- a pasted transcript — and the reports written from them. These are kept,
-- not cached: a written report was paid for, and must not expire.
CREATE TABLE IF NOT EXISTS earn_source (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    event_date TEXT NOT NULL,
    kind       TEXT NOT NULL,        -- slides | transcript | release | other
    title      TEXT,
    url        TEXT,
    body       TEXT,                 -- the text read from it
    added      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_earn_source ON earn_source(symbol, event_date);

CREATE TABLE IF NOT EXISTS earn_report (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol     TEXT NOT NULL,
    event_date TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'report',   -- report | answer
    question   TEXT,
    created    TEXT NOT NULL,
    model      TEXT,
    body       TEXT,
    sources    TEXT,                 -- JSON: what the model was given
    usage      TEXT,                 -- JSON: tokens
    cost_usd   REAL
);
CREATE INDEX IF NOT EXISTS ix_earn_report ON earn_report(symbol, event_date);

-- Every headline the news thread has seen, for ten days. The news cache holds
-- only the latest sixty; the morning brief needs yesterday's, all of them.
CREATE TABLE IF NOT EXISTS news_log (
    k          TEXT PRIMARY KEY,     -- the link without its tracking noise
    ts         REAL,                 -- published
    seen       REAL NOT NULL,        -- first fetched
    headline   TEXT NOT NULL,
    summary    TEXT,
    source     TEXT,
    url        TEXT,
    tickers    TEXT,                 -- JSON list
    category   TEXT
);
CREATE INDEX IF NOT EXISTS ix_news_log_ts ON news_log(ts);

-- ALRT: one morning brief per day, as built, and what Claude wrote about it.
CREATE TABLE IF NOT EXISTS brief_day (
    d          TEXT PRIMARY KEY,     -- the morning it is for
    built_at   REAL NOT NULL,
    edition    TEXT,                 -- morning | refresh
    payload    TEXT NOT NULL         -- JSON
);
CREATE TABLE IF NOT EXISTS brief_note (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    d          TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'note',     -- note | answer
    question   TEXT,
    created    TEXT NOT NULL,
    model      TEXT,
    body       TEXT,
    meta       TEXT,                 -- JSON: writer, session, usage
    cost_usd   REAL
);
CREATE INDEX IF NOT EXISTS ix_brief_note ON brief_note(d);

-- OPT: the last option chain read for each underlying, once from live quotes
-- and once from last trades, and one row of what it said per session. The
-- rows are the history: nobody publishes implied volatility after the fact.
CREATE TABLE IF NOT EXISTS opt_chain (
    sym        TEXT NOT NULL,
    mode       TEXT NOT NULL,        -- quotes | trades
    asof       REAL NOT NULL,
    session    TEXT NOT NULL,        -- the New York date the prices belong to
    payload    TEXT NOT NULL,        -- JSON, compact
    PRIMARY KEY (sym, mode)
);
-- SPLC: the supply-chain links read since the map shipped (the shipped map
-- itself is pf/splc_seed.json). A link is one sentence of one filing.
CREATE TABLE IF NOT EXISTS splc_link (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    a       TEXT NOT NULL,           -- for 'supply': a sells to b; for 'stake': a owns part of b
    b       TEXT NOT NULL,
    kind    TEXT NOT NULL,           -- supply | compete | partner | stake
    what    TEXT,
    pct     REAL,                    -- a share the filing states, in percent
    basis   TEXT,                    -- what pct is a share of: a_revenue | a_receivables | b_purchases | b_cost | stake
    by      TEXT NOT NULL,           -- whose filing says so
    form    TEXT,
    filed   TEXT,
    url     TEXT,
    quote   TEXT NOT NULL,           -- the sentence, cut from the filing
    source  TEXT NOT NULL,           -- claude
    model   TEXT,
    created TEXT NOT NULL,
    UNIQUE (a, b, kind, by, quote)
);
CREATE TABLE IF NOT EXISTS splc_name (
    key     TEXT PRIMARY KEY,        -- a Yahoo symbol, or ~slug for a company that is not listed
    label   TEXT NOT NULL,
    aliases TEXT,
    created TEXT
);
CREATE TABLE IF NOT EXISTS splc_note (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    key     TEXT NOT NULL,
    text    TEXT NOT NULL,
    quote   TEXT,
    form    TEXT,
    filed   TEXT,
    url     TEXT,
    source  TEXT NOT NULL,
    created TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS splc_read (
    key        TEXT PRIMARY KEY,
    sym        TEXT,
    form       TEXT,                 -- the annual report that was read
    filed      TEXT,
    acc        TEXT,
    read_at    TEXT NOT NULL,
    model      TEXT,
    n_links    INTEGER,
    n_passages INTEGER,
    usage      TEXT
);

-- OIL: each day's Brent and WTI curve as the page saw it. Yahoo drops an
-- expired contract's history within weeks, so these rows are what lets the
-- curve of three months or a year ago be drawn at all.
CREATE TABLE IF NOT EXISTS oil_curve (
    d          TEXT NOT NULL,
    root       TEXT NOT NULL,        -- BZ | CL
    ym         TEXT NOT NULL,        -- contract month, YYYY-MM
    px         REAL NOT NULL,
    PRIMARY KEY (d, root, ym)
);
-- OIL: what Claude wrote about the oil page, and answers to questions on it.
CREATE TABLE IF NOT EXISTS oil_note (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    d          TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'note',     -- note | answer
    question   TEXT,
    created    TEXT NOT NULL,
    model      TEXT,
    body       TEXT,
    meta       TEXT,                 -- JSON: writer, session, usage
    cost_usd   REAL
);
CREATE INDEX IF NOT EXISTS ix_oil_note ON oil_note(d);

-- WCH: the stocks you pinned to the watchlist, with a buy price if you set one.
CREATE TABLE IF NOT EXISTS wl_pin (
    sym        TEXT PRIMARY KEY,
    added      TEXT NOT NULL,
    note       TEXT,
    buy        REAL
);
-- WCH: every stock the screen flagged as an opportunity, with that day's close
-- and its benchmark's, so its picks can be judged later. Never rewritten.
CREATE TABLE IF NOT EXISTS wl_flag (
    sym         TEXT NOT NULL,
    preset      TEXT NOT NULL,       -- balanced | growth | value
    first_d     TEXT NOT NULL,
    first_px    REAL,
    bench       TEXT,                -- US | EU
    first_bench REAL,
    last_d      TEXT,
    last_px     REAL,
    end_d       TEXT,                -- the scan it stopped qualifying
    end_px      REAL,
    active      INTEGER NOT NULL DEFAULT 1,
    score       REAL,
    name        TEXT,
    sector      TEXT,
    ccy         TEXT,
    PRIMARY KEY (sym, preset, first_d)
);
CREATE INDEX IF NOT EXISTS ix_wl_flag ON wl_flag(preset, active);
-- WCH: Claude's second opinion on one stock, and answers to questions on it.
CREATE TABLE IF NOT EXISTS wl_note (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sym        TEXT NOT NULL,
    d          TEXT NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'note',     -- note | answer
    question   TEXT,
    created    TEXT NOT NULL,
    model      TEXT,
    body       TEXT,
    meta       TEXT,
    cost_usd   REAL
);
CREATE INDEX IF NOT EXISTS ix_wl_note ON wl_note(sym, d);

-- The forecast's track record (FCST): once a week, the range the book was
-- given for its next twelve months, kept so that a year later it can be held
-- against what the book actually did.
CREATE TABLE IF NOT EXISTS fc_snap (
    d        TEXT PRIMARY KEY,       -- the day the range was given
    created  TEXT NOT NULL,
    v0       REAL,                   -- the book's value that day
    beta     REAL,
    s        REAL,                   -- the book's own yearly swing, as a fraction
    cost     REAL,                   -- costs a year, as a fraction
    q        TEXT NOT NULL           -- JSON: p5..p95 of the twelve-month nominal return
);

-- MVO: your own views for the Markowitz page, one per holding: the return a
-- year you expect, and how sure you are (low, mid, high).
CREATE TABLE IF NOT EXISTS mvo_view (
    t        TEXT PRIMARY KEY,
    ret      REAL NOT NULL,          -- percent a year
    conf     TEXT NOT NULL,
    note     TEXT,
    created  TEXT NOT NULL
);

-- MAC: every macro release the feed has shown, with the actual read from
-- FRED or the ECB and what EUR/USD and the S&P 500 future did around it.
CREATE TABLE IF NOT EXISTS mac_event (
    k          TEXT PRIMARY KEY,      -- date|time|currency|title
    d          TEXT NOT NULL,         -- Amsterdam date
    t          TEXT,                  -- Amsterdam time
    ts         REAL,
    ccy        TEXT,
    title      TEXT,
    impact     TEXT,
    rank       INTEGER,
    forecast   TEXT,
    previous   TEXT,
    actual     TEXT,
    actual_src TEXT,
    surprise   REAL,                  -- actual less forecast, in the release's unit
    unit       TEXT,
    rx_fx      REAL,                  -- EUR/USD, % over the half hour after
    rx_spx     REAL,                  -- the S&P 500 future, %
    rx_at      REAL,
    updated    REAL
);
CREATE INDEX IF NOT EXISTS ix_mac_event ON mac_event(d);

-- What the analysts expected, one row a company a day, as read that day
-- (pf/consensus.py). Yahoo keeps no history of revenue forecasts or targets.
CREATE TABLE IF NOT EXISTS est_day (
    sym      TEXT NOT NULL,
    d        TEXT NOT NULL,          -- Amsterdam date it was read
    src      TEXT,                   -- scan | page
    eps_0q REAL, eps_1q REAL, eps_0y REAL, eps_1y REAL,
    rev_0q REAL, rev_1q REAL, rev_0y REAL, rev_1y REAL,
    n_0q INTEGER, n_0y INTEGER, n_1y INTEGER,
    end_0q TEXT, end_1q TEXT, end_0y TEXT, end_1y TEXT,
    eps_ccy TEXT, rev_ccy TEXT,
    tgt_mean REAL, tgt_median REAL, tgt_high REAL, tgt_low REAL, n_tgt INTEGER,
    rec_mean REAL, price REAL, px_ccy TEXT,
    at       REAL,
    PRIMARY KEY (sym, d)
);

-- The S&P 500's and the Nasdaq 100's figures, a row a day (pf/idx.py): two
-- years of breadth and concentration rebuilt from the members' prices once
-- (src backfill), then each night's whole snapshot (src daily).
CREATE TABLE IF NOT EXISTS idx_day (
    ix TEXT NOT NULL, d TEXT NOT NULL, src TEXT,
    level REAL, p200 REAL, p50 REAL, nh INTEGER, nl INTEGER,
    top10 REAL, eff_n REAL, mag7 REAL, fwd_pe REAL, trail_pe REAL,
    vol REAL, vol3m REAL, rev_up INTEGER, rev_down INTEGER, eps_chg30 REAL,
    am_pct REAL, lf_pct REAL,
    PRIMARY KEY (ix, d)
);

-- The FCF model's inputs you changed, per stock (pf/fcfmodel.py); NULL is
-- the company's own. `listed` puts a stock you do not hold on the FCF page.
CREATE TABLE IF NOT EXISTS fcf_model (
    sym      TEXT PRIMARY KEY,
    growth   REAL, multiple REAL, ret REAL,
    base     TEXT, horizon INTEGER,
    listed   INTEGER NOT NULL DEFAULT 0,
    updated  TEXT
);

-- Every quarter Yahoo has shown for a company outside the SEC's quarterly
-- filings: Yahoo keeps five, this keeps them all (research._kept_quarters).
CREATE TABLE IF NOT EXISTS fin_q (
    sym   TEXT NOT NULL,
    end_d TEXT NOT NULL,
    row   TEXT NOT NULL,             -- JSON, as the table shows it
    seen  TEXT,
    PRIMARY KEY (sym, end_d)
);

-- READ (pf/reader.py): a story is a thread, its articles are its versions
-- (version 1 the first, each update the next), body the article as JSON.
-- A scan is one run of the editor: what it picked and what it passed over.
CREATE TABLE IF NOT EXISTS read_story (
    id       INTEGER PRIMARY KEY,
    title    TEXT, kind TEXT, tickers TEXT,
    created  TEXT, updated TEXT
);
CREATE TABLE IF NOT EXISTS read_article (
    id        INTEGER PRIMARY KEY,
    story_id  INTEGER NOT NULL,
    version   INTEGER NOT NULL,
    slot      TEXT, d TEXT NOT NULL,          -- d: the Amsterdam date it was written on
    created   TEXT NOT NULL,
    kind      TEXT, importance INTEGER,
    headline  TEXT, dek TEXT, tickers TEXT,
    body      TEXT NOT NULL,
    meta      TEXT, cost_usd REAL
);
CREATE INDEX IF NOT EXISTS read_article_d ON read_article (d);
CREATE TABLE IF NOT EXISTS read_scan (
    id        INTEGER PRIMARY KEY,
    d         TEXT NOT NULL, slot TEXT NOT NULL,
    started   TEXT, finished TEXT, state TEXT,
    summary   TEXT, picks TEXT, skipped TEXT,
    n_written INTEGER, meta TEXT, error TEXT
);
CREATE TABLE IF NOT EXISTS read_answer (
    id         INTEGER PRIMARY KEY,
    article_id INTEGER NOT NULL,
    question   TEXT, created TEXT, body TEXT, meta TEXT
);
CREATE TABLE IF NOT EXISTS read_seen (
    article_id INTEGER PRIMARY KEY,
    seen_at    TEXT
);

-- KAI (pf/koersai.py): Koers AI's paper-trading contest. A decision is one
-- turn of Claude; its orders wait for the next opening price and become
-- trades. Amounts in euros; qty may be a fraction of a share.
CREATE TABLE IF NOT EXISTS kai_decision (
    id         INTEGER PRIMARY KEY,
    ts         TEXT NOT NULL,                  -- Amsterdam time the answer came, ISO
    kind       TEXT, trigger TEXT, date_given TEXT,
    journal    TEXT, message TEXT,
    trades     TEXT, rejected TEXT, theses TEXT, -- JSON
    universe   TEXT,                           -- JSON: the tickers it was sent and may buy
    meta       TEXT
);
CREATE TABLE IF NOT EXISTS kai_order (
    id          INTEGER PRIMARY KEY,
    decision_id INTEGER, ts TEXT NOT NULL,
    ticker      TEXT NOT NULL, side TEXT NOT NULL,
    amount_eur  REAL, amount_all INTEGER NOT NULL DEFAULT 0,
    reason      TEXT,
    status      TEXT NOT NULL,                 -- pending, filled, rejected
    fill_d      TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS kai_trade (
    id        INTEGER PRIMARY KEY,
    order_id  INTEGER,
    d TEXT NOT NULL, ticker TEXT NOT NULL, side TEXT NOT NULL,
    qty REAL NOT NULL, px REAL, ccy TEXT, unit_eur REAL,
    value_eur REAL NOT NULL, cost_eur REAL NOT NULL,
    note TEXT
);
-- Koers AI's monthly self-review (build 56): its theses graded against what
-- happened, once a month on a weekend; no trades. The period runs from the
-- day after the last review (or the start) to d_to.
CREATE TABLE IF NOT EXISTS kai_review (
    id       INTEGER PRIMARY KEY,
    month    TEXT NOT NULL,                    -- YYYY-MM of d_to
    kind     TEXT NOT NULL,                    -- monthly | manual
    ts       TEXT NOT NULL,                    -- Amsterdam time the answer came, ISO
    d_from   TEXT, d_to TEXT,
    summary  TEXT,
    body     TEXT,                             -- JSON: grades, process, mistakes, keep, rules
    material TEXT, meta TEXT
);

-- The day's low and high of the book's move, as the live feed saw it (pf/dayrange.py).
CREATE TABLE IF NOT EXISTS day_range (
    d        TEXT PRIMARY KEY,       -- Amsterdam date
    lo       REAL NOT NULL, lo_ts REAL,
    hi       REAL NOT NULL, hi_ts REAL,
    last     REAL, last_ts REAL,
    n        INTEGER
);

-- A turbo's financing level on a date: typed in from DEGIRO's product page or
-- pasted as its current name. SG moves the level daily; two of these set the
-- rate (leveraged.FinPath).
CREATE TABLE IF NOT EXISTS lev_fin (
    isin     TEXT NOT NULL,
    d        TEXT NOT NULL,
    fn       REAL NOT NULL,
    sl       REAL,
    ratio    REAL,
    lv       REAL,
    src      TEXT,
    created  TEXT NOT NULL,
    PRIMARY KEY (isin, d)
);

-- FLOW (pf/flow.py, build 51). The NYSE's and the Nasdaq's up and down volume
-- from the WSJ's market diary, which keeps no history: a row per exchange per
-- session, read after the close (final 1) or during it (final 0).
CREATE TABLE IF NOT EXISTS flow_diary (
    d TEXT NOT NULL, exch TEXT NOT NULL,
    adv INTEGER, decl INTEGER, unch INTEGER, nh INTEGER, nl INTEGER,
    upv REAL, dnv REAL, unv REAL, asof TEXT, final INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (d, exch)
);

-- Cboe's put and call volumes, a session a row; ok 0 is a session Cboe has no file for.
CREATE TABLE IF NOT EXISTS flow_pc (
    d TEXT PRIMARY KEY, ok INTEGER NOT NULL,
    tot_p REAL, tot_c REAL, eq_p REAL, eq_c REAL, ix_p REAL, ix_c REAL,
    etp_p REAL, etp_c REAL, spx_p REAL, spx_c REAL, vix_p REAL, vix_c REAL
);

-- FINRA's daily short-sale volume for the shares the terminal follows (your
-- holdings, the S&P 500 and the Nasdaq 100); sym '*' is the whole market, and
-- a '*' row with no total is a session FINRA has no file for.
CREATE TABLE IF NOT EXISTS flow_short (
    d TEXT NOT NULL, sym TEXT NOT NULL, short REAL, total REAL,
    PRIMARY KEY (d, sym)
);

-- WEEK (pf/weekly.py, build 53): one review a week, keyed by its Monday, and
-- the notes Claude wrote from it.
CREATE TABLE IF NOT EXISTS week_review (
    wk TEXT PRIMARY KEY, built_at REAL NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS week_note (
    id INTEGER PRIMARY KEY AUTOINCREMENT, wk TEXT NOT NULL, created TEXT NOT NULL,
    model TEXT, body TEXT NOT NULL, meta TEXT, cost_usd REAL
);

-- Every alert the home screen showed, once a day (build 53): the record the
-- weekly review's "the terminal's week" reads.
CREATE TABLE IF NOT EXISTS alert_log (
    d TEXT NOT NULL, code TEXT NOT NULL, text TEXT NOT NULL, level TEXT, detail TEXT, ts REAL,
    PRIMARY KEY (d, code, text)
);

CREATE TABLE IF NOT EXISTS opt_day (
    sym        TEXT NOT NULL,
    d          TEXT NOT NULL,        -- session
    asof       REAL NOT NULL,
    mode       TEXT NOT NULL,
    spot       REAL,
    iv30       REAL, iv60 REAL, iv90 REAL, iv180 REAL, iv365 REAL,
    put25      REAL,                 -- 30-day, 25-delta
    call25     REAL,
    rv21       REAL,
    pc_vol     REAL,
    pc_oi      REAL,
    PRIMARY KEY (sym, d)
);
"""


@contextmanager
def conn():
    # A generous busy timeout, because there are now three writers: the web
    # request, the daily snapshot, and the live poller caching prices every 30
    # seconds. SQLite's 5-second default surfaces as "database is locked"
    # exactly when the app is busiest.
    c = sqlite3.connect(DB_PATH, timeout=30.0)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


# Columns added after the first release. Applied on every start so an existing
# portfolio.db upgrades in place instead of needing a rebuild.
MIGRATIONS = [
    ("instrument", "underlying_ticker", "TEXT"),
    ("txn", "balance", "REAL"),
    ("txn", "balance_ccy", "TEXT"),
    ("txn", "seq", "INTEGER"),
    ("kai_decision", "material", "TEXT"),      # what Koers AI was sent for the turn (build 48)
]


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        for table, col, typ in MIGRATIONS:
            have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
            if col not in have:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        # A turbo has no quote of its own anywhere free, so a symbol in its
        # quote field is the share it tracks (build 36): the WDC turbo carried
        # "WDC" there and was priced as the share, at several times its worth.
        c.execute("UPDATE instrument SET underlying_ticker = COALESCE(underlying_ticker, ticker), "
                  "ticker = NULL, ticker_src = NULL WHERE kind = 'DERIVATIVE' AND ticker IS NOT NULL")


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

TXN_FIELDS = ("external_id", "account", "kind", "trade_dt", "settle_date", "isin",
              "product", "quantity", "price", "currency", "local_value", "value_eur",
              "fx_rate", "autofx_cost_eur", "fee_eur", "total_eur", "exchange",
              "venue", "order_id", "balance", "balance_ccy", "seq", "source",
              "note", "imported_at")


def stage_import(txns: list, account: str = "DEGIRO") -> dict:
    """
    Dry run: report what a commit would do. Never writes.
    Returns {new: [...], duplicates: n, unmapped: [isin...]}
    """
    with conn() as c:
        existing = {r[0] for r in c.execute("SELECT external_id FROM txn")}
        known = {r[0] for r in c.execute("SELECT isin FROM instrument")}
    new = [t for t in txns if t.external_id not in existing]
    unmapped = sorted({t.isin for t in new if t.isin and t.isin not in known})
    return {
        "parsed": len(txns),
        "duplicates": len(txns) - len(new),
        "new": len(new),
        "unmapped_isins": unmapped,
        "sample": [t.as_dict() for t in new[:25]],
    }


def commit_import(txns: list, instruments: dict, account: str = "DEGIRO") -> dict:
    """Idempotent: rows whose external_id already exists are skipped."""
    now = datetime.utcnow().isoformat(timespec="seconds")
    ins = dup = 0
    with conn() as c:
        # Decide new-vs-existing BEFORE writing. Inferring it afterwards from a
        # timestamp is unreliable: two imports inside the same second are
        # indistinguishable, which made every row look freshly inserted.
        existing = {r[0] for r in c.execute("SELECT external_id FROM txn")}
        for isin, inst in instruments.items():
            c.execute(
                """INSERT INTO instrument (isin,name,kind,currency,exchange,underlying,underlying_ticker)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(isin) DO UPDATE SET
                     underlying_ticker=COALESCE(instrument.underlying_ticker, excluded.underlying_ticker),
                     name=CASE WHEN length(excluded.name)>length(instrument.name)
                               THEN excluded.name ELSE instrument.name END,
                     currency=COALESCE(instrument.currency, excluded.currency),
                     exchange=COALESCE(instrument.exchange, excluded.exchange),
                     underlying=COALESCE(instrument.underlying, excluded.underlying)""",
                (inst.isin, inst.name, inst.kind, inst.currency, inst.exchange,
                 inst.underlying, inst.underlying_ticker),
            )
        # A turbo's name carries its financing level on the day of the export,
        # so each import is a dated point for leveraged.FinPath. Never over
        # one typed in by hand on the same day.
        try:
            from .leveraged import parse as _lev_parse
            for isin, inst in instruments.items():
                if inst.kind != "DERIVATIVE":
                    continue
                sp = _lev_parse(inst.name or "")
                if sp.family == "TURBO" and sp.financing:
                    c.execute("INSERT OR IGNORE INTO lev_fin (isin,d,fn,sl,ratio,lv,src,created) VALUES (?,?,?,?,?,?,?,?)",
                              (isin, now[:10], sp.financing, sp.barrier, sp.ratio, sp.name_leverage, "export", now))
        except Exception:
            pass
        for t in txns:
            row = (
                t.external_id, account, t.kind, t.trade_dt.isoformat(),
                t.settle_date.isoformat() if t.settle_date else None,
                t.isin, t.product, t.quantity, t.price, t.currency, t.local_value,
                t.value_eur, t.fx_rate, t.autofx_cost_eur, t.fee_eur, t.total_eur,
                t.exchange, t.venue, t.order_id, t.balance, t.balance_ccy,
                t.seq, t.source, t.note, now,
            )
            is_new = t.external_id not in existing
            c.execute(
                f"INSERT INTO txn ({','.join(TXN_FIELDS)}) "
                f"VALUES ({','.join('?' * len(TXN_FIELDS))}) "
                # A row already present is not simply skipped: fields added in a
                # later version (balance, seq) are backfilled onto it, otherwise
                # an old import stays permanently blank and silently wrong.
                f"ON CONFLICT(external_id) DO UPDATE SET "
                f"  balance     = COALESCE(txn.balance,     excluded.balance), "
                f"  balance_ccy = COALESCE(txn.balance_ccy, excluded.balance_ccy), "
                f"  seq         = COALESCE(txn.seq,         excluded.seq)", row)
            if is_new:
                ins += 1
                existing.add(t.external_id)
            else:
                dup += 1
    return {"inserted": ins, "skipped_duplicates": dup}


# --------------------------------------------------------------------------
# read back as engine objects
# --------------------------------------------------------------------------

def load_txns() -> list:
    from .degiro import Txn
    out = []
    with conn() as c:
        for r in c.execute("SELECT * FROM txn ORDER BY trade_dt"):
            t = Txn(
                kind=r["kind"],
                trade_dt=datetime.fromisoformat(r["trade_dt"]),
                settle_date=date.fromisoformat(r["settle_date"]) if r["settle_date"] else None,
                isin=r["isin"], product=r["product"], quantity=r["quantity"],
                price=r["price"], currency=r["currency"], local_value=r["local_value"],
                value_eur=r["value_eur"], fx_rate=r["fx_rate"],
                autofx_cost_eur=r["autofx_cost_eur"] or 0.0, fee_eur=r["fee_eur"] or 0.0,
                total_eur=r["total_eur"], exchange=r["exchange"], venue=r["venue"],
                order_id=r["order_id"],
                balance=r["balance"] if "balance" in r.keys() else None,
                balance_ccy=r["balance_ccy"] if "balance_ccy" in r.keys() else None,
                seq=r["seq"] if "seq" in r.keys() else None,
                source=r["source"] or "",
                external_id=r["external_id"], note=r["note"] or "",
            )
            out.append(t)
    return out


def load_instruments() -> dict:
    with conn() as c:
        return {r["isin"]: dict(r) for r in c.execute("SELECT * FROM instrument")}


def add_fin(isin: str, d: str, fn: float, sl=None, ratio=None, lv=None, src: str = "") -> None:
    """One financing level per turbo per day; a second on the same day replaces the first."""
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO lev_fin (isin,d,fn,sl,ratio,lv,src,created) VALUES (?,?,?,?,?,?,?,?)",
                  (isin, d, fn, sl, ratio, lv, src, datetime.utcnow().isoformat(timespec="seconds")))


def fin_points(isin: str) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM lev_fin WHERE isin=? ORDER BY d", (isin,))]


def drop_fin(isin: str, d: str) -> bool:
    with conn() as c:
        return c.execute("DELETE FROM lev_fin WHERE isin=? AND d=?", (isin, d)).rowcount > 0


def first_import(isin: str) -> Optional[str]:
    """The date of the first import that carried this product: the date of the name it was stored under."""
    with conn() as c:
        r = c.execute("SELECT min(imported_at) FROM txn WHERE isin=? AND source LIKE 'degiro%'", (isin,)).fetchone()
    return r[0][:10] if r and r[0] else None


def set_instrument(isin: str, **fields):
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE instrument SET {cols} WHERE isin=?",
                  (*fields.values(), isin))


# --------------------------------------------------------------------------
# price / fx cache
# --------------------------------------------------------------------------

def cached_prices(ticker: str) -> dict:
    with conn() as c:
        return {r["d"]: r["close"]
                for r in c.execute("SELECT d, close FROM price WHERE ticker=?", (ticker,))}


def put_prices(ticker: str, rows: Iterable[tuple], currency: str, source: str):
    now = datetime.utcnow().isoformat(timespec="seconds")
    with conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO price (ticker,d,close,currency,source,fetched_at)"
            " VALUES (?,?,?,?,?,?)",
            [(ticker, d, v, currency, source, now) for d, v in rows])


def cached_fx(pair: str) -> dict:
    with conn() as c:
        return {r["d"]: r["rate"]
                for r in c.execute("SELECT d, rate FROM fx WHERE pair=?", (pair,))}


def put_fx(pair: str, rows: Iterable[tuple], source: str):
    now = datetime.utcnow().isoformat(timespec="seconds")
    with conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO fx (pair,d,rate,source,fetched_at) VALUES (?,?,?,?,?)",
            [(pair, d, v, source, now) for d, v in rows])


def last_fetch(ticker: str) -> Optional[str]:
    with conn() as c:
        r = c.execute("SELECT MAX(fetched_at) f FROM price WHERE ticker=?", (ticker,)).fetchone()
        return r["f"] if r else None


# --------------------------------------------------------------------------
# snapshots — the thing that makes TWR and drawdown possible later
# --------------------------------------------------------------------------

def write_snapshot(d: str, total_eur: float, cost_eur: float,
                   cash_eur: float, net_flow_eur: float, positions: list):
    now = datetime.utcnow().isoformat(timespec="seconds")
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO snapshot (d,total_eur,cost_eur,cash_eur,net_flow_eur,created_at)"
                  " VALUES (?,?,?,?,?,?)", (d, total_eur, cost_eur, cash_eur, net_flow_eur, now))
        c.executemany("INSERT OR REPLACE INTO snapshot_position (d,isin,qty,price,value_eur)"
                      " VALUES (?,?,?,?,?)",
                      [(d, p["isin"], p["qty"], p["price"], p["value_eur"]) for p in positions])


def load_snapshots() -> list:
    with conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM snapshot ORDER BY d")]


def checkpoint():
    """
    Fold the write-ahead log into the database file.

    Matters when portfolio.db is replaced by a newer copy while stale
    portfolio.db-wal / -shm sidecars are still on disk: SQLite may serve the
    old contents from them, so a fixed number looks unfixed.
    """
    try:
        with conn() as c:
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception:
        pass


# --------------------------------------------------------------------------
# keeping the cache from growing without end (build 52)
# --------------------------------------------------------------------------

# Cache families that are copies of something that can be fetched again, and
# how many days one may go unrefreshed before it is deleted. Nothing else is
# ever pruned: ern:* holds the consensus snapshot taken before each report,
# and other keys are records rather than copies. On 8 November 2026 the cache
# was 225 MB of portfolio.db's 254 MB and growing about 5 MB a day: a page of
# news per stock per day, earnings documents kept forever, a set of prices per
# combination of holdings.
PRUNE = [
    ("riskmap:px:", 21), ("bonds:fit:", 21), ("rrg:px:", 21), ("mvo:px:", 21), ("opt:px:", 21),
    ("rs:day:", 45), ("ed:txt:", 45),
    ("rs:", 60), ("ed:idx:", 60), ("sec:", 60), ("wl:fin:", 60), ("wl:est:", 60), ("fool:quote:", 60), ("splc:fts:", 60),
]


def cache_prune(now: Optional[float] = None) -> dict:
    """Delete the stale copies PRUNE names; {prefix: rows deleted}."""
    now = now or datetime.utcnow().timestamp()
    out = {}
    with conn() as c:
        for p, days in PRUNE:
            like = p.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            n = c.execute("DELETE FROM cache WHERE k LIKE ? ESCAPE '\\' AND fetched_at < ?", (like, now - days * 86400)).rowcount
            if n:
                out[p] = n
    return out


def vacuum_mode() -> int:
    with conn() as c:
        return c.execute("PRAGMA auto_vacuum").fetchone()[0]


def make_incremental() -> bool:
    """
    Once, at start, before anything else writes: switch the file to incremental
    auto-vacuum, which takes one full VACUUM. From then on freed pages can be
    handed back a few at a time while the app runs. False when it could not be
    done now (the file is busy); it is tried again at the next start.
    """
    if vacuum_mode() == 2:
        return True
    try:
        c = sqlite3.connect(DB_PATH, timeout=5.0)
        try:
            c.execute("PRAGMA auto_vacuum = INCREMENTAL")
            c.execute("VACUUM")
        finally:
            c.close()
        return vacuum_mode() == 2
    except sqlite3.OperationalError:
        return False


def maintain() -> dict:
    """The daily pass: prune the stale copies, then give the freed pages back to the disk."""
    out = {"pruned": cache_prune()}
    if vacuum_mode() == 2:
        with conn() as c:
            free = c.execute("PRAGMA freelist_count").fetchone()[0]
            c.execute("PRAGMA incremental_vacuum").fetchall()
            out["freed_pages"] = free
        checkpoint()            # in WAL mode the file shrinks only when the log is folded back in
    set_setting("db_maintained", json.dumps({"at": datetime.utcnow().timestamp(), **out}))
    return out


def ensure_maintenance(delay: float = 600, every: float = 86400) -> None:
    """A thread that runs maintain() ten minutes after start and then once a day."""
    import threading

    def loop():
        time.sleep(delay)
        while True:
            try:
                maintain()
            except Exception:
                pass
            time.sleep(every)
    if not any(t.name == "pf-maintain" for t in threading.enumerate()):
        threading.Thread(target=loop, name="pf-maintain", daemon=True).start()


def get_setting(k: str, default=None):
    with conn() as c:
        r = c.execute("SELECT v FROM setting WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default


def set_setting(k: str, v: str):
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO setting (k,v) VALUES (?,?)", (k, str(v)))


# --------------------------------------------------------------------------
# cache: expensive fetches that may be served slightly stale
# --------------------------------------------------------------------------

def cache_get(k: str, max_age: Optional[float] = None):
    """
    The cached value, or None. Age is returned WITH it, never hidden.

    A database written by an older build has no cache table. That means "no
    cache", not a broken app, so it is caught here rather than surfacing as a
    500 on the home screen the first time someone upgrades.
    """
    import json as _j
    try:
        with conn() as c:
            r = c.execute("SELECT v, fetched_at FROM cache WHERE k=?", (k,)).fetchone()
    except sqlite3.OperationalError:
        return None
    if not r:
        return None
    age = datetime.utcnow().timestamp() - r["fetched_at"]
    if max_age is not None and age > max_age:
        return None
    try:
        v = _j.loads(r["v"])
    except Exception:
        return None
    if isinstance(v, dict):
        v["_age_s"] = round(age)
    return v


def cache_age(k: str) -> float:
    try:
        with conn() as c:
            r = c.execute("SELECT fetched_at FROM cache WHERE k=?", (k,)).fetchone()
    except sqlite3.OperationalError:
        return 1e9
    return 1e9 if not r else datetime.utcnow().timestamp() - r["fetched_at"]


def cache_put(k: str, value):
    """
    Cache a payload, stamped with the build that produced it.

    The stamp is what stops a code change from being invisible for hours. Cached
    answers outlive restarts by design — that is the point of them — but a
    payload built by OLDER code can have an older SHAPE, and a page that reads a
    field the old code never wrote falls back to "no data" while looking
    perfectly healthy. That happened twice in one evening: a fixed sentiment
    build served the previous version's errors, and an earnings payload with a
    twelve-hour life went on claiming no holding had a future date while the
    per-ticker cache underneath it held forty-four of them.
    """
    import json as _j
    from . import BUILD
    if isinstance(value, dict):
        value = {**value, "_build": BUILD}
    init_db()               # an upgraded database may not have the table yet
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO cache (k,v,fetched_at) VALUES (?,?,?)",
                  (k, _j.dumps(value, default=str), datetime.utcnow().timestamp()))


_CT_LOCKS: dict = {}
_CT_GUARD = __import__("threading").Lock()


def cache_through(k: str, ttl: float, fn, stale_ok: bool = True) -> dict:
    """
    Serve `k` from the cache if it is younger than `ttl` and was built by this
    build; otherwise call `fn()`, store and return its answer.

    One fetch per key at a time. The research page asks for six panels at
    once and four of them need the same price history — without the lock
    that is four identical downloads racing each other.

    When the fetch fails and an older answer exists, the older answer is
    returned with `_stale` set to the reason. Yesterday's fundamentals are
    worth showing; an empty panel is not.
    """
    from . import BUILD

    def fresh(v):
        return (isinstance(v, dict) and v.get("_build") == BUILD
                and v.get("_age_s", 1e9) <= ttl)

    hit = cache_get(k)
    if fresh(hit):
        return hit
    with _CT_GUARD:
        lock = _CT_LOCKS.setdefault(k, __import__("threading").Lock())
    with lock:
        hit = cache_get(k)
        if fresh(hit):
            return hit
        try:
            v = fn()
        except Exception as e:
            if stale_ok and isinstance(hit, dict):
                return {**hit, "_stale": f"{e.__class__.__name__}: {e}"[:200]}
            raise
        if not isinstance(v, dict):
            v = {"value": v}
        cache_put(k, v)
        return {**v, "_age_s": 0}


# --------------------------------------------------------------------------
# user-defined alert rules
# --------------------------------------------------------------------------

def list_rules() -> list[dict]:
    try:
        with conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM alert_rule ORDER BY id DESC")]
    except sqlite3.OperationalError:
        return []


def add_rule(isin, ticker, kind, value, note="") -> int:
    init_db()
    with conn() as c:
        cur = c.execute(
            "INSERT INTO alert_rule (isin,ticker,kind,value,note,enabled,created)"
            " VALUES (?,?,?,?,?,1,?)",
            (isin, ticker, kind, float(value), note,
             datetime.utcnow().isoformat(timespec="seconds")))
        return cur.lastrowid


def delete_rule(rule_id: int) -> bool:
    with conn() as c:
        return c.execute("DELETE FROM alert_rule WHERE id=?", (rule_id,)).rowcount > 0
