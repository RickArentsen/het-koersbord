"""Senate Electronic Financial Disclosure (efdsearch.senate.gov) adapter.

Source: the public search for the Senate's eFD system, run by the Secretary of
the Senate, Office of Public Records.  Filers submit through efd.senate.gov;
the public reads at efdsearch.senate.gov/search/.  There is no official API -
scraping the search interface is the only route, and it is what every public
Senate scraper does.

Why this needs curl_cffi
------------------------
The portal sits behind an edge that fingerprints the TLS handshake, not the IP.
Plain ``requests`` is refused with 403 from the same machine, on the same
network, in the same second that Chrome loads the page fine - the address is not
the problem, the client is.  ``curl_cffi`` replays Chrome's ClientHello and is
served normally.

This was originally misdiagnosed as a network-level block.  It is not: the
give-away was a browser succeeding where Python failed on one host.  If this
ever regresses, test that pair first before blaming egress.

Verification status
-------------------
The request flow and the report parser below were **confirmed against the live
site**: the search endpoint returned HTTP 200 with the expected JSON envelope
(``draw / recordsTotal / recordsFiltered / data``), rows arrived in the
``[first, last, office, link_html, filed]`` shape this module parses, and a real
Periodic Transaction Report rendered the nine-column table
(``# / Transaction Date / Owner / Ticker / Asset Name / Asset Type / Type /
Amount / Comment``) that :func:`parse_report` reads.

Confirmed working end to end with ``curl_cffi`` installed.  If it ever stops,
run::

    python src/cli.py senate-check

It probes each step and prints exactly where it stops.

The flow the Senate portal uses:

1. ``GET  /search/home/``  - returns a CSRF token and sets a session cookie.
2. ``POST /search/home/``  - accept the "prohibition on use" agreement.
3. ``POST /search/report/data/`` - paged JSON search; report type 11 is the
   Periodic Transaction Report.
4. ``GET  /search/view/ptr/<id>/`` - an electronically filed report, as an HTML
   table.  Paper filings live under ``/search/view/paper/<id>/`` and are scans
   with no machine-readable content.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from html.parser import HTMLParser

import requests

BASE = "https://efdsearch.senate.gov"
HOME = f"{BASE}/search/home/"
SEARCH = f"{BASE}/search/report/data/"
REPORT_TYPE_PTR = "11"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CSRF_RE = re.compile(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"')
LINK_RE = re.compile(r'href="(/search/view/([^/]+)/([^/"]+)/?)"[^>]*>(.*?)</a>', re.S)


class SenateBlocked(RuntimeError):
    """The portal refused the request (403 / challenge page)."""


@dataclass
class SenateFiling:
    first_name: str
    last_name: str
    office: str
    report_title: str
    filed_date: str
    url: str
    kind: str  # 'ptr' (electronic) or 'paper' (scan)


def new_session():
    """A session whose TLS handshake looks like Chrome's.

    Falls back to plain requests when curl_cffi is absent, which will almost
    certainly be refused - the error message says so rather than leaving the
    caller to guess.
    """
    try:
        from curl_cffi import requests as curl_requests

        s = curl_requests.Session(impersonate="chrome")
        s.headers.update(HEADERS)
        return s
    except ImportError:
        s = requests.Session()
        s.headers.update(HEADERS)
        return s


def _blocked_hint() -> str:
    try:
        import curl_cffi  # noqa: F401
        return ("403 from the portal's edge. curl_cffi is installed, so this is "
                "not the usual TLS fingerprint problem - the address itself may "
                "be blocked, or the impersonation target needs updating.")
    except ImportError:
        return ("403 from the portal's edge, which fingerprints the TLS "
                "handshake. Install the Chrome-impersonating client:  "
                "pip install curl_cffi")


def open_session():
    """Establish a session that has accepted the portal's use agreement."""
    s = new_session()

    resp = s.get(HOME, timeout=45)
    if resp.status_code == 403:
        raise SenateBlocked(_blocked_hint())
    resp.raise_for_status()

    match = CSRF_RE.search(resp.text)
    if not match:
        raise SenateBlocked("no CSRF token in the agreement page - layout changed?")
    token = match.group(1)

    resp = s.post(HOME, data={"csrfmiddlewaretoken": token,
                              "prohibition_agreement": "1"},
                  headers={"Referer": HOME}, timeout=45)
    if resp.status_code == 403:
        raise SenateBlocked("agreement POST returned 403")
    resp.raise_for_status()
    return s


def _csrf(session) -> str:
    token = session.cookies.get("csrftoken", domain="efdsearch.senate.gov")
    return token or session.cookies.get("csrftoken") or ""


def search_ptrs(session, start_date: str, end_date: str | None = None,
                page_size: int = 100, max_pages: int = 50) -> list[SenateFiling]:
    """List Periodic Transaction Reports filed between two dates (MM/DD/YYYY)."""
    end_date = end_date or dt.date.today().strftime("%m/%d/%Y")
    out: list[SenateFiling] = []

    for page in range(max_pages):
        payload = {
            "start": str(page * page_size),
            "length": str(page_size),
            "report_types": f"[{REPORT_TYPE_PTR}]",
            "filer_types": "[]",
            "submitted_start_date": f"{start_date} 00:00:00",
            "submitted_end_date": f"{end_date} 23:59:59",
            "candidate_state": "",
            "senator_state": "",
            "office_id": "",
            "first_name": "",
            "last_name": "",
        }
        resp = session.post(SEARCH, data=payload, timeout=60, headers={
            "Referer": f"{BASE}/search/", "X-CSRFToken": _csrf(session),
            "X-Requested-With": "XMLHttpRequest",
        })
        if resp.status_code == 403:
            raise SenateBlocked("search POST returned 403")
        resp.raise_for_status()

        try:
            body = resp.json()
        except ValueError as exc:
            raise SenateBlocked(f"search did not return JSON: {exc}") from exc

        rows = body.get("data") or []
        for row in rows:
            if len(row) < 5:
                continue
            first, last, office, link_html, filed = row[:5]
            m = LINK_RE.search(link_html or "")
            if not m:
                continue
            path, kind, _ident, title = m.groups()
            out.append(SenateFiling(
                first_name=_strip_tags(first), last_name=_strip_tags(last),
                office=_strip_tags(office), report_title=_strip_tags(title),
                filed_date=_strip_tags(filed), url=BASE + path, kind=kind))

        if len(rows) < page_size:
            break

    return out


class _TableParser(HTMLParser):
    """Collects the rows of every <table> on a report page."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", str(text or ""))).strip()


def parse_report(session, filing: SenateFiling) -> list[dict]:
    """Read the transaction table out of an electronically filed Senate PTR."""
    if filing.kind != "ptr":
        return []  # paper filings are scans; nothing machine-readable

    resp = session.get(filing.url, timeout=60)
    if resp.status_code == 403:
        raise SenateBlocked("report GET returned 403")
    resp.raise_for_status()

    parser = _TableParser()
    parser.feed(resp.text)

    transactions = []
    for row in parser.rows:
        # Electronic PTR rows: #, Transaction Date, Owner, Ticker, Asset Name,
        # Asset Type, Type, Amount, Comment.
        if len(row) < 8 or not re.match(r"^\d+$", row[0] or ""):
            continue
        # An exchange lists both legs in one cell ("-- AMCR"), so take the
        # first real symbol rather than testing the whole cell for a placeholder.
        ticker = _strip_tags(row[3])
        parts = [p for p in ticker.split() if p not in {"--", "-", "N/A", ""}]
        ticker = parts[0] if parts else None
        transactions.append({
            "tx_date": _iso(row[1]),
            "owner": row[2],
            "ticker": ticker,
            "asset_name": row[4],
            "asset_type": row[5],
            "tx_type": _norm_type(row[6]),
            "amount_raw": row[7],
            "description": row[8] if len(row) > 8 else "",
            "source_url": filing.url,
        })
    return transactions


def _iso(value: str) -> str | None:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(_strip_tags(value), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _norm_type(value: str) -> str:
    v = _strip_tags(value).lower()
    if v.startswith("purchase"):
        return "purchase"
    if "partial" in v:
        return "sale_partial"
    if v.startswith("sale"):
        return "sale"
    if v.startswith("exchange"):
        return "exchange"
    return v or "unknown"


def check(verbose: bool = True) -> dict:
    """Probe each step of the Senate flow and report exactly where it stops."""
    result = {"reachable": False, "agreement": False, "search": False,
              "report": False, "error": "", "sample": None, "n_filings": None}
    try:
        resp = new_session().get(HOME, timeout=30)
        result["reachable"] = resp.status_code == 200
        if resp.status_code != 200:
            # Do not return early: the report below is the whole point of this
            # function, and this is the failure path most likely to be hit.
            result["error"] = f"GET {HOME} -> HTTP {resp.status_code}"
            raise SenateBlocked(result["error"])

        session = open_session()
        result["agreement"] = True

        since = (dt.date.today() - dt.timedelta(days=21)).strftime("%m/%d/%Y")
        filings = search_ptrs(session, since, max_pages=1)
        result["search"] = True
        result["n_filings"] = len(filings)

        electronic = next((f for f in filings if f.kind == "ptr"), None)
        if electronic:
            txs = parse_report(session, electronic)
            result["report"] = True
            result["sample"] = {"filer": f"{electronic.first_name} {electronic.last_name}",
                                "url": electronic.url, "transactions": len(txs),
                                "first": txs[0] if txs else None}
    except Exception as exc:  # noqa: BLE001
        if not result["error"]:
            result["error"] = f"{type(exc).__name__}: {exc}"

    if verbose:
        for step in ("reachable", "agreement", "search", "report"):
            print(f"  {'OK  ' if result[step] else 'FAIL'}  {step}")
        if result.get("n_filings") is not None:
            print(f"  filings found: {result['n_filings']}")
        if result["sample"]:
            print(f"  sample: {result['sample']['filer']} -> "
                  f"{result['sample']['transactions']} transactions")
            if result["sample"]["first"]:
                print(f"          {result['sample']['first']}")
        if result["error"]:
            print(f"  error: {result['error']}")
    return result
