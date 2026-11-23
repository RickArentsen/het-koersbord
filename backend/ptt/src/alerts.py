"""Buy alerts: which purchases appeared since the last time we looked.

A filing becomes actionable the moment it is published, so "new" is defined by
when this tracker first ingested the filing, not by the trade date.

Alerts are *ranked* by the buyer's past edge and committee access, which is a
different thing from *selecting* on it.  The strategy deliberately no longer
filters members by their record - that gate was tested and added nothing - but
an ordering still has to put something at the top, and past edge is a defensible
tiebreak for attention as long as nothing is excluded by it.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

import consensus
import db

LAST_RUN_KEY = "alerts_last_run"


def detect_new_buys(conn: sqlite3.Connection, mark_seen: bool = True,
                    limit: int = 500) -> list[dict]:
    """Purchases from filings ingested since the previous alert run."""
    last_run = db.get_meta(conn, LAST_RUN_KEY)
    now = dt.datetime.now().isoformat(timespec="seconds")

    if last_run:
        where = "f.ingested_at > ?"
        params: list = [last_run]
    else:
        # First run after a backfill, every filing was ingested seconds ago, so
        # ingestion time says nothing about what is new.  Seed from trade
        # recency instead: the STOCK Act caps disclosure at 45 days, so this
        # window is what a first-time reader would actually consider current.
        where = "COALESCE(t.tx_date_fixed, t.tx_date) >= ?"
        params = [(dt.date.today() - dt.timedelta(days=45)).isoformat()]

    rows = conn.execute(
        f"""SELECT t.id, t.member_name, t.bioguide_id, t.ticker, t.asset_name,
                   t.amount_raw, t.amount_mid, COALESCE(t.tx_date_fixed, t.tx_date) AS tx_date, t.tx_date AS tx_date_filed, t.tx_date_note, t.notification_date,
                   t.filing_date, t.owner, t.state_district, t.tx_type,
                   p.entry_date, f.pdf_url,
                   s.avg_alpha_3m, s.n_scored, s.sensitivity, s.committees
              FROM transactions t
              JOIN filings f ON f.doc_id = t.doc_id
         LEFT JOIN trade_performance p ON p.transaction_id = t.id
         LEFT JOIN politician_stats s ON s.bioguide_id = t.bioguide_id
             WHERE t.tx_type = 'purchase' AND {where}
          ORDER BY t.filing_date DESC, t.id
             LIMIT ?""", (*params, limit)).fetchall()

    import sectors as sectors_mod

    alerts = sectors_mod.annotate(conn, [dict(r) for r in rows])
    # How many members were already in this name when the filing landed. The
    # alert is the moment a reader sees it, so the rank shown is the rank it had
    # then - peers who bought later are not counted.
    consensus.annotate(conn, alerts)
    for a in alerts:
        a["priority"] = _priority(a)
    alerts.sort(key=lambda a: a["priority"], reverse=True)

    if mark_seen:
        db.set_meta(conn, LAST_RUN_KEY, now)
        conn.commit()
    return alerts


def _priority(alert: dict) -> float:
    """Rank an alert: proven edge first, then access, then position size."""
    score = 0.0
    alpha = alert.get("avg_alpha_3m")
    n = alert.get("n_scored") or 0
    if alpha is not None and n >= 5:
        # Shrink the alpha toward zero when the sample is thin, so a member with
        # three lucky trades does not outrank one with fifty good ones.
        score += alpha * min(1.0, n / 25.0) * 3.0
    score += (alert.get("sensitivity") or 0) * 0.5
    mid = alert.get("amount_mid") or 0
    if mid > 0:
        score += min(mid / 1_000_000, 1.0) * 0.3
    # Buying inside your own committee's remit is the most notable case.
    if alert.get("overlap"):
        score += 0.6
    return round(score, 4)


def recent_buys(conn: sqlite3.Connection, days: int = 30, limit: int = 200) -> list[dict]:
    """Buys disclosed in the last ``days`` days, newest first (for the feed)."""
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = conn.execute(
        """SELECT t.id, t.member_name, t.bioguide_id, t.ticker, t.asset_name,
                  t.amount_raw, t.amount_mid, COALESCE(t.tx_date_fixed, t.tx_date) AS tx_date, t.tx_date AS tx_date_filed, t.tx_date_note, t.notification_date,
                  t.filing_date, t.owner, t.state_district, t.tx_type,
                  p.entry_date, f.pdf_url,
                  s.avg_alpha_3m, s.n_scored, s.sensitivity
             FROM transactions t
             JOIN filings f ON f.doc_id = t.doc_id
        LEFT JOIN trade_performance p ON p.transaction_id = t.id
        LEFT JOIN politician_stats s ON s.bioguide_id = t.bioguide_id
            WHERE t.tx_type = 'purchase' AND COALESCE(t.tx_date_fixed, t.tx_date) >= ?
         ORDER BY COALESCE(t.tx_date_fixed, t.tx_date) DESC, t.id DESC
            LIMIT ?""", (cutoff, limit)).fetchall()

    import sectors as sectors_mod

    return consensus.annotate(conn, sectors_mod.annotate(conn, [dict(r) for r in rows]))
