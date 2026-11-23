"""Ingestion of House of Representatives financial disclosure filings.

Two public endpoints from the Clerk of the House are used:

* ``/public_disc/financial-pdfs/<YEAR>FD.zip`` - a yearly index containing a
  tab-delimited list of every disclosure filed that year.  ``FilingType == 'P'``
  marks a Periodic Transaction Report (the stock-trade filings).
* ``/public_disc/ptr-pdfs/<YEAR>/<DOC_ID>.pdf`` - the filing itself.

Both were verified live before this module was written.  Downloads are cached on
disk so a re-run only fetches filings it has never seen.
"""

from __future__ import annotations

import csv
import io
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests

INDEX_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

# The Clerk's site is a public records service; identify the client honestly and
# stay well inside a polite request rate.
HEADERS = {
    "User-Agent": "politician-trading-tracker/1.0 (research project; contact: local user)",
    "Accept": "*/*",
}
REQUEST_DELAY = 0.4  # seconds between PDF downloads


@dataclass
class FilingRecord:
    doc_id: str
    year: int
    last: str
    first: str
    suffix: str
    prefix: str
    filing_type: str
    state_district: str
    filing_date: str

    @property
    def display_name(self) -> str:
        name = f"{self.first} {self.last}".strip()
        if self.suffix:
            name = f"{name} {self.suffix}".strip()
        return name

    @property
    def pdf_url(self) -> str:
        return PTR_URL.format(year=self.year, doc_id=self.doc_id)


def new_session() -> requests.Session:
    """A configured session; reuse one across a run to keep the connection warm."""
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


def fetch_index(year: int, cache_dir: Path, session: requests.Session | None = None,
                refresh: bool = False) -> list[FilingRecord]:
    """Download (or reuse) the yearly disclosure index and return its rows."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    zip_path = cache_dir / f"{year}FD.zip"

    if refresh or not zip_path.exists():
        sess = session or new_session()
        resp = sess.get(INDEX_URL.format(year=year), timeout=60)
        resp.raise_for_status()
        zip_path.write_bytes(resp.content)

    records: list[FilingRecord] = []
    with zipfile.ZipFile(zip_path) as zf:
        txt_name = next((n for n in zf.namelist() if n.lower().endswith(".txt")), None)
        if txt_name is None:
            return records
        raw = zf.read(txt_name).decode("utf-8-sig", errors="replace")

    reader = csv.DictReader(io.StringIO(raw), delimiter="\t")
    for row in reader:
        doc_id = (row.get("DocID") or "").strip()
        if not doc_id:
            continue
        try:
            row_year = int((row.get("Year") or year))
        except ValueError:
            row_year = year
        records.append(FilingRecord(
            doc_id=doc_id,
            year=row_year,
            last=(row.get("Last") or "").strip(),
            first=(row.get("First") or "").strip(),
            suffix=(row.get("Suffix") or "").strip(),
            prefix=(row.get("Prefix") or "").strip(),
            filing_type=(row.get("FilingType") or "").strip(),
            state_district=(row.get("StateDst") or "").strip(),
            filing_date=(row.get("FilingDate") or "").strip(),
        ))
    return records


def periodic_transaction_reports(records: list[FilingRecord]) -> list[FilingRecord]:
    """Keep only Periodic Transaction Reports (the filings that list trades)."""
    return [r for r in records if r.filing_type.upper() == "P"]


def download_ptr(record: FilingRecord, pdf_dir: Path,
                 session: requests.Session | None = None) -> Path | None:
    """Fetch one PTR PDF, reusing the cached copy when present.

    Returns the local path, or ``None`` if the Clerk has no PDF for that
    document id (which happens for a small number of withdrawn filings).
    """
    pdf_dir.mkdir(parents=True, exist_ok=True)
    dest = pdf_dir / f"{record.doc_id}.pdf"
    if dest.exists() and dest.stat().st_size > 0:
        return dest

    sess = session or new_session()
    try:
        resp = sess.get(record.pdf_url, timeout=60)
    except requests.RequestException:
        return None
    if resp.status_code != 200 or not resp.content[:5].startswith(b"%PDF"):
        return None

    dest.write_bytes(resp.content)
    time.sleep(REQUEST_DELAY)
    return dest
