"""Parser for House of Representatives Periodic Transaction Report (PTR) PDFs.

The electronically-filed PTRs are text-based PDFs with a stable seven-column
transaction table.  Rather than relying on raw text extraction (which welds the
transaction date and notification date together into one token), this parser
uses pdfplumber word coordinates and derives the column boundaries from the
table header on each page.  That survives the layout drift that shows up between
filing-system versions.

Paper filings are scanned images with no text layer; they are reported as
``parse_status='no_text'`` rather than silently yielding zero transactions.
"""

from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Iterable

import pdfplumber

# --------------------------------------------------------------------------
# Column model
# --------------------------------------------------------------------------

# Header labels, in table order, as they appear in the PTR header row.  Each
# column starts at the x0 of its header word and runs to the start of the next.
HEADER_SEQUENCE = ["ID", "Owner", "Asset", "Transaction", "Date", "Notification",
                   "Amount", "Cap."]
COLUMN_NAMES = ["id", "owner", "asset", "type", "date", "notification",
                "amount", "capgains"]
# Filings from 2019-2020 predate the "Cap. Gains > $200?" column and have seven
# columns, not eight.  Requiring all eight rejected them outright.
REQUIRED_HEADERS = 7

# Transaction type codes used by the Clerk's filing system.
TRANSACTION_TYPES = {
    "P": "purchase",
    "S": "sale",
    "S (partial)": "sale_partial",
    "E": "exchange",
}

DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")
# Sub-table label lines inside a transaction block.  The filing system renders
# these labels in a small-caps font whose lowercase glyphs carry no usable
# character code, so pdfplumber returns them as NUL bytes: "FILING STATUS:"
# arrives as "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00:".  Text is passed
# through _clean() (which drops the NULs) before these patterns are applied.
#
# The filing system has shipped two different small-caps renderings, and the
# parser must handle both or the older filings silently corrupt:
#
#   2021+   "FILING STATUS:" -> "F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00:"
#           (lowercase glyphs carry no character code), collapsing to "F S:".
#   2020    "FILING STATUS:" -> "FIlINg sTaTus:" - literal mixed case.
#
# Missing the second form lets label text run into the asset name, which then
# drags a *later* asset's ticker into this row: "(DIA) ... Blackstone (BX)"
# resolved to BX. So both spellings are matched, case-insensitively.
LABEL_WORDS = (r"FILING\s*STATUS|SUB\s*HOLDING\s*OF|SUBHOLDING\s*OF|LOCATION"
               r"|DESCRIPTION|COMMENTS")
LABEL_RE = re.compile(rf"^({LABEL_WORDS}|F\s*S|S\s*O|L|D|C)\s*:", re.IGNORECASE)

# Belt and braces: if a label still reaches the asset name, cut it off there so
# no downstream ticker match can see the leaked text.
ASSET_TRAILING_LABEL_RE = re.compile(rf"\s*(?:{LABEL_WORDS})\s*:.*$",
                                     re.IGNORECASE | re.DOTALL)

# The transaction table always ends with this footnote; everything below it
# (Investment Vehicle Details, IPOs, Certification) must not be read as data.
FOOTNOTE_RE = re.compile(r"^\*\s*For the complete list of asset type", re.IGNORECASE)
# Post-table section headings collapse to spaced capitals once NULs are dropped
# ("INVESTMENT VEHICLE DETAILS" -> "I V D"), or appear spelled out in the older
# mixed-case rendering.
SECTION_HEAD_RE = re.compile(
    r"^(?:[A-Z](?:\s+[A-Z]){1,4}"
    r"|INVESTMENT\s+VEHICLE\s+DETAILS"
    r"|INITIAL\s+PUBLIC\s+OFFERINGS?"
    r"|CERTIFICATION\s+AND\s+SIGNATURE)$", re.IGNORECASE)

AMOUNT_RANGES = [
    (1_001, 15_000), (15_001, 50_000), (50_001, 100_000),
    (100_001, 250_000), (250_001, 500_000), (500_001, 1_000_000),
    (1_000_001, 5_000_000), (5_000_001, 25_000_000),
    (25_000_001, 50_000_000), (50_000_001, 100_000_000),
]

OWNER_CODES = {"SP": "spouse", "DC": "dependent_child", "JT": "joint"}


@dataclass
class Transaction:
    doc_id: str = ""
    member_name: str = ""
    state_district: str = ""
    status: str = ""
    owner: str = "self"
    asset_name: str = ""
    ticker: str | None = None
    asset_type: str | None = None
    tx_type: str = ""
    tx_date: str | None = None
    notification_date: str | None = None
    amount_raw: str = ""
    amount_low: int | None = None
    amount_high: int | None = None
    cap_gains_over_200: bool = False
    description: str = ""
    filing_status: str = ""
    page: int = 0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ParsedFiling:
    doc_id: str
    member_name: str = ""
    state_district: str = ""
    status: str = ""
    parse_status: str = "ok"          # ok | no_text | no_table | error
    error: str = ""
    transactions: list[Transaction] = field(default_factory=list)


# --------------------------------------------------------------------------
# Low-level helpers
# --------------------------------------------------------------------------

# Some 2019-2020 filings embed a font subset with a broken encoding, so the
# small-caps labels extract as CJK and private-use glyphs ("Fﾛﾞﾛἠﾙ SϹ￿Ϲﾟ;:").
# The transaction data itself is unaffected - only these decorative labels.
# Written as explicit escapes, and deliberately *excluding* U+2000-U+206F
# (General Punctuation): that block holds the en and em dashes that appear
# legitimately in asset names and in filers' own descriptions.
GARBLED_RE = re.compile(
    "["
    "\u0080-\u009f"   # C1 controls
    "\u0370-\u03ff"   # Greek and Coptic
    "\u1f00-\u1fff"   # Greek Extended
    "\u3000-\u9fff"   # CJK punctuation and ideographs
    "\ua000-\ua4cf"   # Yi
    "\uac00-\ud7af"   # Hangul syllables
    "\ue000-\uf8ff"   # Private use area
    "\uff00-\uffef"   # Halfwidth and fullwidth forms
    "\ufff0-\uffff"   # Specials
    "]")


def _clean(text: str) -> str:
    """Drop the NUL padding emitted by the small-caps font and tidy spacing."""
    return re.sub(r"\s+", " ", text.replace("\x00", "")).strip()


def _looks_like_label(flat: str) -> bool:
    """Is this a sub-table label line rendered in a broken font?

    Such a line has a colon early on and unreadable glyphs before it.  Asset
    names legitimately contain a colon only in an exchange prefix
    ("NYSEARCA: DIA"), which is excluded.
    """
    head = flat[:26]
    if ":" not in head:
        return False
    before = head.split(":", 1)[0]
    if re.search(r"(?:NYSE|NASDAQ|NYSEARCA|AMEX|BATS|OTC)\s*$", before, re.IGNORECASE):
        return False
    return bool(GARBLED_RE.search(before))


def _group_lines(words: list[dict], tolerance: float = 2.5) -> list[list[dict]]:
    """Cluster words into visual lines by their vertical position."""
    buckets: dict[int, list[dict]] = collections.defaultdict(list)
    for w in words:
        buckets[round(w["top"] / tolerance)].append(w)
    lines = []
    for key in sorted(buckets):
        lines.append(sorted(buckets[key], key=lambda w: w["x0"]))
    # Merge buckets whose tops are within tolerance of each other (a word can
    # land either side of a rounding boundary).
    merged: list[list[dict]] = []
    for line in lines:
        if merged and abs(line[0]["top"] - merged[-1][0]["top"]) <= tolerance:
            merged[-1] = sorted(merged[-1] + line, key=lambda w: w["x0"])
        else:
            merged.append(line)
    return merged


def _is_header_line(line: list[dict]) -> bool:
    """Is this the transaction-table header row?

    Matched case-insensitively: one older layout renders the header as
    "iD | owner | asset | transaction ... amount | cap." rather than title
    case.  Comparing exactly discarded roughly a quarter of the 2020-2021
    filings as having no table at all.
    """
    texts = [w["text"].strip().lower() for w in line]
    expected = [h.lower() for h in HEADER_SEQUENCE[:4]]
    return texts[:4] == expected and "amount" in texts


def _header_starts(line: list[dict]) -> list[float] | None:
    """x0 of each header label present, or None if the required ones are missing."""
    starts: list[float] = []
    for label in HEADER_SEQUENCE:
        match = next((w for w in line
                      if w["text"].strip().lower() == label.lower()), None)
        if match is None:
            # 'Cap.' is absent from the pre-2021 seven-column layout; anything
            # earlier missing means this is not a transaction table.
            if len(starts) >= REQUIRED_HEADERS:
                break
            return None
        starts.append(match["x0"])
    return starts


def _find_column_bounds(lines: list[list[dict]]) -> dict[str, tuple[float, float]] | None:
    """Locate the transaction-table header and derive column x-ranges from it."""
    for line in lines:
        if not _is_header_line(line):
            continue
        starts = _header_starts(line)
        if starts is None:
            return None
        # Nudge each boundary left slightly: body text sometimes starts a
        # hair before its header word (e.g. asset at 104.4 vs header 104.8).
        starts = [s - 1.5 for s in starts]
        bounds = {}
        for i, name in enumerate(COLUMN_NAMES):
            if i >= len(starts):
                # Column absent in this layout: give it an empty range so
                # _cell() returns "" rather than swallowing another column.
                bounds[name] = (1e9, 1e9)
                continue
            lo = starts[i]
            hi = starts[i + 1] if i + 1 < len(starts) else 1e9
            bounds[name] = (lo, hi)
        return bounds
    return None


def _cell(line: list[dict], bounds: tuple[float, float]) -> str:
    """Join the words of a line that fall inside a column's x-range."""
    lo, hi = bounds
    parts = [w["text"] for w in line if lo <= w["x0"] < hi]
    return _clean(" ".join(parts))


def _parse_amount(raw: str) -> tuple[int | None, int | None]:
    """Turn '$15,001 - $50,000' or 'Over $50,000,000' into numeric bounds."""
    nums = [int(n.replace(",", "")) for n in re.findall(r"\$\s?([\d,]+)", raw)]
    if not nums:
        return None, None
    if len(nums) == 1:
        low = nums[0]
        # 'Over $50,000,000' has no upper bound; otherwise look the range up.
        if re.search(r"\bover\b", raw, re.IGNORECASE):
            return low, None
        for lo, hi in AMOUNT_RANGES:
            if lo == low:
                return lo, hi
        return low, None
    return nums[0], nums[1]


# Ticker matching is case-insensitive because the older font subset lowercases
# an unpredictable subset of letters: BAC arrives as "BaC", GOOGL as "gOOgl",
# DIA as "DIa".  The mapping is not consistent enough to invert, so symbols are
# matched in any case and upper-cased.  The trade-off is that ordinary
# parenthesised words could be mistaken for symbols, which NOT_TICKERS blocks.
TICKER_RE = re.compile(r"\(([A-Za-z][A-Za-z0-9.\-/]{0,6})\)")
EXCHANGE_TICKER_RE = re.compile(
    r"(?:NYSE|NASDAQ|NYSEARCA|AMEX|BATS|OTC)\s*:\s*([A-Za-z][A-Za-z0-9.\-/]{0,6})",
    re.IGNORECASE)
# Case-insensitive: the 2020 rendering emits "[sT]" rather than "[ST]".
ASSET_TYPE_RE = re.compile(r"\[([A-Za-z]{2,3})\]")

# Parenthesised words that are not stock symbols.  Without this, matching
# case-insensitively would turn "(partial)" or "(new)" into tickers.
# Deliberately excludes SEE, ADS, CALL, PLAN and NOTE: those look like ordinary
# words but are real symbols (Sealed Air, Alliance Data, magicJack, Anaplan,
# FiscalNote), and blocking them would drop genuine trades.  The list is
# restricted to tokens that cannot be a symbol, because a false negative here
# is silent while a false positive is visible on the ticker page.
NOT_TICKERS = {
    "ETF", "ETN", "LLC", "LLP", "INC", "CORP", "CO", "LTD", "PLC", "USD",
    "IRA", "N/A", "NA", "NEW", "OLD", "THE", "AND", "PARTIAL", "CLASS",
    "FUND", "TRUST", "OWNER", "SP", "JT", "DC", "JOINT", "SPOUSE", "SELF",
    "COMMON", "STOCK", "SHARES", "UNITS", "BOND", "PUT", "SERIES",
    "ROTH", "401K", "529", "REIT", "ADR", "CUSIP", "YES", "NONE",
    "VARIOUS", "MULTIPLE", "NOTES", "PENDING",
}


def _extract_ticker(asset: str) -> tuple[str | None, str | None, str]:
    """Pull the ticker and asset-type code out of an asset description."""
    asset_type = None
    m = ASSET_TYPE_RE.search(asset)
    if m:
        asset_type = m.group(1).upper()
        asset = ASSET_TYPE_RE.sub("", asset).strip()

    ticker = None
    m = EXCHANGE_TICKER_RE.search(asset)
    if m:
        ticker = m.group(1)
    else:
        # Prefer the last parenthesised all-caps token: filings write the name
        # first and the symbol last, e.g. "Apple Inc. - Common Stock (AAPL)".
        candidates = TICKER_RE.findall(asset)
        if candidates:
            ticker = candidates[-1]

    if ticker:
        ticker = ticker.strip().upper().rstrip(".")
        if ticker in NOT_TICKERS or not ticker:
            ticker = None
    return ticker, asset_type, asset.strip()


def _parse_date(raw: str) -> str | None:
    raw = raw.strip()
    if not DATE_RE.match(raw):
        return None
    try:
        return datetime.strptime(raw, "%m/%d/%Y").date().isoformat()
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Header block (name / status / district)
# --------------------------------------------------------------------------

def _parse_filing_header(lines: list[list[dict]]) -> dict:
    """Read the filer block.

    All three labels are matched case-insensitively: the older layout renders
    them as "name:", "Status:", "State/District:" with unpredictable casing,
    and an exact match left the member's name blank on those filings.
    """
    info = {"member_name": "", "status": "", "state_district": ""}
    for line in lines:
        flat = _clean(" ".join(w["text"] for w in line))
        if not info["member_name"]:
            m = re.match(r"name\s*:\s*(.+)", flat, re.IGNORECASE)
            if m:
                info["member_name"] = m.group(1).strip()
                continue
        if not info["status"]:
            m = re.match(r"status\s*:\s*(.+)", flat, re.IGNORECASE)
            if m:
                info["status"] = m.group(1).strip()
                continue
        if not info["state_district"]:
            # The 2020 rendering emits "oR03"/"gA12" rather than "OR03"/"GA12".
            m = re.match(r"state\s*/\s*district\s*:\s*([A-Za-z]{2}\d{2})",
                         flat, re.IGNORECASE)
            if m:
                info["state_district"] = m.group(1).strip().upper()
    return info


# --------------------------------------------------------------------------
# Main entry point
# --------------------------------------------------------------------------

def parse_ptr(path: str, doc_id: str = "") -> ParsedFiling:
    """Parse one House PTR PDF into a :class:`ParsedFiling`."""
    filing = ParsedFiling(doc_id=doc_id)
    try:
        with pdfplumber.open(path) as pdf:
            all_pages = []
            total_chars = 0
            for pageno, page in enumerate(pdf.pages):
                words = page.extract_words(use_text_flow=False,
                                           keep_blank_chars=False)
                total_chars += sum(len(w["text"]) for w in words)
                all_pages.append((pageno, _group_lines(words)))

            if total_chars < 200:
                filing.parse_status = "no_text"
                return filing

            if all_pages:
                filing.__dict__.update(_parse_filing_header(all_pages[0][1]))

            current: Transaction | None = None
            in_metadata = False
            saw_table = False

            for pageno, lines in all_pages:
                bounds = _find_column_bounds(lines)
                if bounds is None:
                    continue
                saw_table = True
                header_top = None
                for line in lines:
                    if _is_header_line(line):
                        header_top = line[0]["top"]
                        break

                for line in lines:
                    # Skip everything above (and including) the header block.
                    if header_top is not None and line[0]["top"] <= header_top + 26:
                        continue

                    flat_line = _clean(" ".join(w["text"] for w in line))

                    # The table ends at the asset-code footnote; the sections
                    # below it (holdings, IPOs, certification) are not trades.
                    if FOOTNOTE_RE.match(flat_line) or SECTION_HEAD_RE.match(flat_line):
                        if current is not None:
                            filing.transactions.append(current)
                            current = None
                        in_metadata = False
                        break

                    type_cell = _cell(line, bounds["type"])
                    date_cell = _cell(line, bounds["date"])
                    notif_cell = _cell(line, bounds["notification"])
                    asset_cell = _cell(line, bounds["asset"])
                    amount_cell = _cell(line, bounds["amount"])

                    is_new_row = (
                        type_cell in TRANSACTION_TYPES
                        and DATE_RE.match(date_cell or "")
                    )

                    if is_new_row:
                        if current is not None:
                            filing.transactions.append(current)
                        owner_cell = _cell(line, bounds["owner"]).strip().upper()
                        cap_cell = _cell(line, bounds["capgains"]).strip().lower()
                        current = Transaction(
                            doc_id=doc_id,
                            member_name=filing.member_name,
                            state_district=filing.state_district,
                            status=filing.status,
                            owner=OWNER_CODES.get(owner_cell, "self"),
                            asset_name=asset_cell,
                            tx_type=TRANSACTION_TYPES[type_cell],
                            tx_date=_parse_date(date_cell),
                            notification_date=_parse_date(notif_cell),
                            amount_raw=amount_cell,
                            cap_gains_over_200=cap_cell.startswith("y"),
                            page=pageno,
                        )
                        in_metadata = False
                        continue

                    if current is None:
                        continue

                    label = LABEL_RE.match(flat_line)
                    if label is None and _looks_like_label(flat_line):
                        # Broken-font label: the name is unreadable but the
                        # line is still metadata, not part of the asset.
                        in_metadata = True
                        continue
                    if label:
                        in_metadata = True
                        kind = label.group(1).strip().upper()[:1]
                        value = flat_line[label.end():].strip()
                        if kind == "D":
                            current.description = value
                        elif kind == "F":
                            current.filing_status = value
                        continue

                    if in_metadata:
                        # Wrapped description text keeps flowing under the label.
                        if current.description:
                            current.description += " " + flat_line
                        continue

                    # Pre-metadata continuation: the asset name and the amount
                    # range both wrap onto the following line.
                    if asset_cell:
                        current.asset_name = f"{current.asset_name} {asset_cell}".strip()
                    if amount_cell:
                        current.amount_raw = f"{current.amount_raw} {amount_cell}".strip()

            if current is not None:
                filing.transactions.append(current)

            if not saw_table:
                filing.parse_status = "no_table"

    except Exception as exc:  # noqa: BLE001 - report, never crash the pipeline
        filing.parse_status = "error"
        filing.error = f"{type(exc).__name__}: {exc}"
        return filing

    # Post-process: tickers, amounts, tidy whitespace.
    for tx in filing.transactions:
        tx.asset_name = ASSET_TRAILING_LABEL_RE.sub("", _clean(tx.asset_name))
        tx.asset_name = _clean(GARBLED_RE.sub("", tx.asset_name))
        tx.ticker, tx.asset_type, tx.asset_name = _extract_ticker(tx.asset_name)
        tx.amount_raw = _clean(tx.amount_raw)
        tx.amount_low, tx.amount_high = _parse_amount(tx.amount_raw)
        tx.description = _clean(tx.description)[:1000]
        tx.doc_id = doc_id
        tx.member_name = filing.member_name
        tx.state_district = filing.state_district
        tx.status = filing.status

    return filing


def amount_midpoint(low: int | None, high: int | None) -> float:
    """Representative dollar size for a disclosed range.

    Ranges are wide and right-skewed, so the geometric mean is a better central
    estimate than the arithmetic one.  Open-ended top ranges use 2x the floor.
    """
    if low is None:
        return 0.0
    if high is None:
        return float(low) * 2.0
    return (float(low) * float(high)) ** 0.5
