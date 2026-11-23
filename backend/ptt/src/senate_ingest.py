"""Ingest Senate Periodic Transaction Reports into the same tables as the House.

Senate filings land in `filings` and `transactions` alongside House ones, so
everything downstream - pricing, scoring, grading, the remit test, the site -
works on both chambers without knowing the difference.

Two things differ from the House and are handled here:

* **Document ids.** The Senate identifies reports by uuid, the House by a
  numeric id, so they can never collide in `filings.doc_id`. Senate rows are
  stored prefixed (`sen:<uuid>`) so the source is legible at a glance and any
  query can separate the chambers.
* **Matching.** Senators have no district, so `MemberMatcher.resolve_senator`
  keys on surname within the Senate rather than state+district.

Amounts, owners and transaction types are normalised to the House vocabulary on
the way in, so a Senate row and a House row mean the same thing everywhere else.
"""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime

import senate
from matching import MemberMatcher
from ptr_parser import AMOUNT_RANGES, amount_midpoint

DOC_PREFIX = "sen:"

# The Senate prints owner as a word; the House uses two-letter codes. Normalise
# to the House vocabulary because that is what the rest of the pipeline reads.
OWNER_MAP = {
    "self": "self", "spouse": "spouse", "joint": "joint",
    "child": "dependent_child", "dependent child": "dependent_child",
}

# Senate asset-type strings -> the House single/double letter codes.
ASSET_TYPE_MAP = {
    "stock": "ST", "stock option": "OP", "corporate bond": "CS",
    "municipal security": "GS", "other securities": "OT",
    "non-public stock": "OT", "etf": "OT", "exchange traded fund": "OT",
    "mutual fund": "MF", "cryptocurrency": "CT", "crypto": "CT",
    "government security": "GS", "variable annuity": "VA",
    "pdf disclosed filing": None,
}

TICKER_OK = re.compile(r"^[A-Z][A-Z0-9.\-]{0,6}$")


def _amount_bounds(raw: str) -> tuple[int | None, int | None]:
    """Parse '$15,001 - $50,000' or 'Over $50,000,000' into numeric bounds."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"\$\s?([\d,]+)", raw or "")]
    if not nums:
        return None, None
    if len(nums) == 1:
        low = nums[0]
        if re.search(r"\bover\b", raw, re.IGNORECASE):
            return low, None
        for lo, hi in AMOUNT_RANGES:
            if lo == low:
                return lo, hi
        return low, None
    return nums[0], nums[1]


def _asset_type(raw: str, ticker: str | None) -> str | None:
    key = (raw or "").strip().lower()
    if key in ASSET_TYPE_MAP:
        return ASSET_TYPE_MAP[key]
    if "option" in key:
        return "OP"
    if "bond" in key or "note" in key:
        return "CS"
    if "municipal" in key or "treasury" in key:
        return "GS"
    if "fund" in key:
        return "MF"
    # Anything with a plausible symbol is treated as equity, which is what the
    # scorer needs; without one it stays unclassified and simply is not scored.
    return "ST" if ticker else None


def _clean_ticker(raw: str | None) -> str | None:
    if not raw:
        return None
    t = raw.strip().upper()
    return t if TICKER_OK.match(t) else None


def ingest(conn: sqlite3.Connection, start_date: str = "01/01/2020",
           end_date: str | None = None, limit: int | None = None,
           progress: bool = True, delay: float = 0.35) -> dict:
    """Fetch, parse and store every Senate PTR in a date window.

    ``start_date`` / ``end_date`` are MM/DD/YYYY, matching the portal.
    """
    stats = {"listed": 0, "electronic": 0, "paper": 0, "ingested": 0,
             "skipped": 0, "transactions": 0, "unmatched": 0, "errors": 0}

    session = senate.open_session()
    filings = senate.search_ptrs(session, start_date, end_date)
    stats["listed"] = len(filings)
    if progress:
        print(f"  {len(filings)} Senate PTRs listed", flush=True)

    known = {r["doc_id"] for r in conn.execute(
        "SELECT doc_id FROM filings WHERE parse_status = 'ok'")}
    matcher = MemberMatcher(conn)

    if limit:
        filings = filings[:limit]

    for i, f in enumerate(filings, 1):
        uuid = f.url.rstrip("/").rsplit("/", 1)[-1]
        doc_id = f"{DOC_PREFIX}{uuid}"

        if f.kind != "ptr":
            stats["paper"] += 1
            _record(conn, doc_id, f, "no_text", 0)
            continue
        stats["electronic"] += 1

        if doc_id in known:
            stats["skipped"] += 1
            continue

        try:
            rows = senate.parse_report(session, f)
        except senate.SenateBlocked:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad report must not stop the run
            stats["errors"] += 1
            _record(conn, doc_id, f, "error", 0, str(exc)[:200])
            continue

        bioguide = matcher.resolve_senator(f.first_name, f.last_name)
        if bioguide is None:
            stats["unmatched"] += 1

        _record(conn, doc_id, f, "ok", len(rows))
        conn.execute("""DELETE FROM trade_performance WHERE transaction_id IN
                        (SELECT id FROM transactions WHERE doc_id = ?)""", (doc_id,))
        conn.execute("DELETE FROM transactions WHERE doc_id = ?", (doc_id,))

        member = f"Hon. {f.first_name} {f.last_name}".strip()
        for seq, tx in enumerate(rows):
            ticker = _clean_ticker(tx.get("ticker"))
            atype = _asset_type(tx.get("asset_type", ""), ticker)
            lo, hi = _amount_bounds(tx.get("amount_raw", ""))
            owner = OWNER_MAP.get((tx.get("owner") or "").strip().lower(), "self")
            conn.execute(
                """INSERT INTO transactions
                   (doc_id, seq, bioguide_id, member_name, state_district, owner,
                    asset_name, ticker, asset_type, tx_type, tx_date,
                    notification_date, filing_date, amount_raw, amount_low,
                    amount_high, amount_mid, cap_gains_over_200, description)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
                (doc_id, seq, bioguide, member, "", owner,
                 tx.get("asset_name", ""), ticker, atype,
                 tx.get("tx_type", "unknown"), tx.get("tx_date"),
                 tx.get("tx_date"), f.filed_date, tx.get("amount_raw", ""),
                 lo, hi, amount_midpoint(lo, hi), tx.get("description", "")[:1000]))
        stats["transactions"] += len(rows)
        stats["ingested"] += 1

        if progress and i % 25 == 0:
            conn.commit()
            print(f"  {i}/{len(filings)} filings, "
                  f"{stats['transactions']} transactions", flush=True)
        if delay:
            time.sleep(delay)

    conn.commit()
    return stats


def _record(conn: sqlite3.Connection, doc_id: str, f: senate.SenateFiling,
            status: str, n: int, error: str = "") -> None:
    year = 0
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            year = datetime.strptime(f.filed_date[:10], fmt).year
            break
        except ValueError:
            continue
    conn.execute(
        """INSERT INTO filings
           (doc_id, year, filer_last, filer_first, member_name, state_district,
            filing_date, parse_status, parse_error, n_transactions, pdf_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(doc_id) DO UPDATE SET
             parse_status=excluded.parse_status, parse_error=excluded.parse_error,
             n_transactions=excluded.n_transactions""",
        (doc_id, year, f.last_name, f.first_name,
         f"Hon. {f.first_name} {f.last_name}".strip(), "",
         f.filed_date, status, error, n, f.url))
