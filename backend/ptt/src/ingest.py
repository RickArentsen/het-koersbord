"""Orchestrates: yearly index -> PTR download -> PDF parse -> SQLite."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import house
import matching
from matching import MemberMatcher
from ptr_parser import amount_midpoint, parse_ptr


def ingest_year(conn: sqlite3.Connection, year: int, data_dir: Path,
                limit: int | None = None, refresh_index: bool = False,
                progress: bool = True, reparse: bool = False) -> dict:
    """Ingest every Periodic Transaction Report filed in ``year``.

    With ``reparse=True`` every filing is parsed again from its cached PDF.
    Use this after a parser fix: nothing is re-downloaded, so it runs in
    minutes rather than hours.
    """
    raw_dir = data_dir / "raw"
    pdf_dir = raw_dir / "ptr" / str(year)

    records = house.fetch_index(year, raw_dir, refresh=refresh_index)
    ptrs = house.periodic_transaction_reports(records)
    if limit:
        ptrs = ptrs[:limit]

    already = set() if reparse else {row["doc_id"] for row in conn.execute(
        "SELECT doc_id FROM filings WHERE parse_status='ok'")}

    matcher = MemberMatcher(conn)
    session = house.new_session()
    # 'no_table' is tracked separately: folding it into 'parsed' once hid 983
    # filings whose header row did not match, which looked like success.
    stats = {"seen": len(ptrs), "downloaded": 0, "parsed": 0, "skipped": 0,
             "no_text": 0, "no_table": 0, "no_pdf": 0, "errors": 0,
             "transactions": 0}

    for i, rec in enumerate(ptrs, 1):
        if rec.doc_id in already:
            stats["skipped"] += 1
            continue

        path = house.download_ptr(rec, pdf_dir, session=session)
        if path is None:
            stats["no_pdf"] += 1
            _record_filing(conn, rec, parse_status="no_pdf", n=0)
            continue
        stats["downloaded"] += 1

        filing = parse_ptr(str(path), rec.doc_id)
        if filing.parse_status == "no_text":
            stats["no_text"] += 1
        elif filing.parse_status == "no_table":
            stats["no_table"] += 1
        elif filing.parse_status == "error":
            stats["errors"] += 1
        else:
            stats["parsed"] += 1

        member_name = filing.member_name or rec.display_name
        state_district = filing.state_district or rec.state_district
        bioguide = matcher.resolve(member_name, state_district)

        _record_filing(conn, rec, parse_status=filing.parse_status,
                       n=len(filing.transactions), member_name=member_name,
                       state_district=state_district, error=filing.error)

        # Scores reference transaction ids, and re-parsing mints new ones, so
        # the dependent rows must go first or the foreign key rejects the
        # delete.  They are rebuilt wholesale by the next backtest run.
        conn.execute(
            """DELETE FROM trade_performance WHERE transaction_id IN
               (SELECT id FROM transactions WHERE doc_id=?)""", (rec.doc_id,))
        conn.execute("DELETE FROM transactions WHERE doc_id=?", (rec.doc_id,))
        for seq, tx in enumerate(filing.transactions):
            conn.execute(
                """INSERT INTO transactions
                   (doc_id, seq, bioguide_id, member_name, state_district, owner,
                    asset_name, ticker, asset_type, tx_type, tx_date,
                    notification_date, filing_date, amount_raw, amount_low,
                    amount_high, amount_mid, cap_gains_over_200, description)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rec.doc_id, seq, bioguide, member_name, state_district, tx.owner,
                 tx.asset_name, tx.ticker, tx.asset_type, tx.tx_type, tx.tx_date,
                 tx.notification_date, rec.filing_date, tx.amount_raw,
                 tx.amount_low, tx.amount_high,
                 amount_midpoint(tx.amount_low, tx.amount_high),
                 int(tx.cap_gains_over_200), tx.description))
        stats["transactions"] += len(filing.transactions)

        if progress and i % 25 == 0:
            conn.commit()
            print(f"  [{year}] {i}/{len(ptrs)} filings, "
                  f"{stats['transactions']} transactions", flush=True)

    conn.commit()
    return stats


def _record_filing(conn: sqlite3.Connection, rec: house.FilingRecord,
                   parse_status: str, n: int, member_name: str = "",
                   state_district: str = "", error: str = "") -> None:
    conn.execute(
        """INSERT INTO filings
           (doc_id, year, filer_last, filer_first, member_name, state_district,
            filing_date, parse_status, parse_error, n_transactions, pdf_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(doc_id) DO UPDATE SET
             parse_status=excluded.parse_status, parse_error=excluded.parse_error,
             n_transactions=excluded.n_transactions,
             member_name=excluded.member_name,
             state_district=excluded.state_district""",
        (rec.doc_id, rec.year, rec.last, rec.first,
         member_name or rec.display_name, state_district or rec.state_district,
         rec.filing_date, parse_status, error, n, rec.pdf_url))


def rematch_members(conn: sqlite3.Connection, progress: bool = True) -> dict:
    """Re-resolve every House filer, overwriting ids that are already set.

    :func:`backfill_bioguide` only fills blanks, so it cannot undo a *wrong*
    match. When the matcher itself is corrected the stored ids have to be
    recomputed - the seat-only fallback had assigned 6,373 transactions to
    whoever holds that district now, including 567 of Donna Shalala's to Maria
    Elvira Salazar.

    Senate rows are left alone: they carry no district and are resolved by
    :meth:`MemberMatcher.resolve_senator` at ingest time.
    """
    matcher = MemberMatcher(conn)
    rows = conn.execute(
        """SELECT DISTINCT member_name, state_district, bioguide_id
             FROM transactions
            WHERE doc_id NOT LIKE 'sen:%' AND member_name IS NOT NULL""").fetchall()

    senate_rows = conn.execute(
        """SELECT DISTINCT member_name, bioguide_id FROM transactions
            WHERE doc_id LIKE 'sen:%' AND member_name IS NOT NULL""").fetchall()

    stats = {"groups": len(rows) + len(senate_rows), "unchanged": 0,
             "cleared": 0, "repointed": 0, "filled": 0}

    # Senate first: these carry no district, so they must go through the
    # senator resolver. Running the House one over them is what put Senator
    # Markwayne Mullin's trades under a California representative.
    for r in senate_rows:
        name = (r["member_name"] or "").replace("Hon. ", "").strip()
        # name_parts, not a raw split: "A. Mitchell McConnell, Jr." ends in a
        # suffix, and passing "Jr." as the surname unmatched four sitting
        # senators on the first attempt.
        first, last = matching.name_parts(name)
        new = matcher.resolve_senator(first, last) if last else None
        old = r["bioguide_id"]
        if new == old:
            stats["unchanged"] += 1
            continue
        cur = conn.execute(
            "UPDATE transactions SET bioguide_id = ? "
            "WHERE doc_id LIKE 'sen:%' AND member_name IS ?",
            (new, r["member_name"]))
        n = cur.rowcount
        if new is None:
            stats["cleared"] += n
        elif old is None:
            stats["filled"] += n
        else:
            stats["repointed"] += n
        if progress:
            print(f"  [senate] {name[:30]:32} {old or 'none':>9} -> "
                  f"{new or 'UNMATCHED'}  ({n} tx)", flush=True)

    for r in rows:
        new = matcher.resolve(r["member_name"], r["state_district"])
        old = r["bioguide_id"]
        if new == old:
            stats["unchanged"] += 1
            continue
        cur = conn.execute(
            """UPDATE transactions SET bioguide_id = ?
                WHERE doc_id NOT LIKE 'sen:%' AND member_name IS ?
                  AND state_district IS ?""",
            (new, r["member_name"], r["state_district"]))
        n = cur.rowcount
        if new is None:
            stats["cleared"] += n
        elif old is None:
            stats["filled"] += n
        else:
            stats["repointed"] += n
        if progress:
            print(f"  {r['member_name'].replace('Hon. ','')[:32]:34}"
                  f"[{r['state_district'] or '--':6}] {old or 'none':>9} -> "
                  f"{new or 'UNMATCHED'}  ({n} tx)", flush=True)
    conn.commit()
    return stats


def backfill_bioguide(conn: sqlite3.Connection) -> int:
    """Fill in missing member ids for House filings after a roster refresh.

    House only. Senate rows carry no district, so running the House resolver
    over them falls through to a bare surname lookup - which handed Senator
    Markwayne Mullin's 559 transactions to Kevin Mullin, a representative from
    California. Senate rows are resolved by ``resolve_senator`` at ingest time
    and re-resolved by :func:`rematch_members`.
    """
    matcher = MemberMatcher(conn)
    updated = 0
    rows = conn.execute(
        "SELECT DISTINCT member_name, state_district FROM transactions "
        "WHERE bioguide_id IS NULL AND doc_id NOT LIKE 'sen:%'").fetchall()
    for r in rows:
        bioguide = matcher.resolve(r["member_name"], r["state_district"])
        if bioguide:
            cur = conn.execute(
                "UPDATE transactions SET bioguide_id=? "
                "WHERE bioguide_id IS NULL AND doc_id NOT LIKE 'sen:%' "
                "AND member_name=? AND state_district=?",
                (bioguide, r["member_name"], r["state_district"]))
            updated += cur.rowcount
    conn.commit()
    return updated
