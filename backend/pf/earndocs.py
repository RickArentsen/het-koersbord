"""
The documents behind an earnings report, fetched and read.

    8-K, item 2.02   the press release (EX-99.1) and whatever is furnished
                     with it — slides, a shareholder letter, CFO commentary.
                     Foreign filers furnish the same on a 6-K. From EDGAR,
                     usually within five minutes of the release.
    10-Q / 10-K      the filed accounts, the same day for some companies and
                     six weeks later for others. The MD&A is what gets read.
    transcript       the call, from the Motley Fool's free transcripts —
                     often the same evening, sometimes a week later. When it
                     is not there you can paste one, or a link.
    slides           when furnished on EDGAR; otherwise a link you add from
                     the company's investor-relations site.

Everything is read into plain text once and kept: a filing never changes, so
there is no reason to fetch it twice.

The press-release parser is deliberately simple and says so. It finds the
income statement (the table with a revenue row AND a diluted-EPS row, whose
first column is the quarter), reads the first column, and scales it against
the consensus when the table does not say "in millions". Anything it could
not find is left empty rather than guessed. It is right for the ordinary
release and wrong for the odd one, which is why every number links to the
document it came from.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import store, net, sec

TIMEOUT = 30
FOREVER = 10 * 365 * 86400


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _headers(url: str) -> dict:
    return sec._headers() if "sec.gov" in url else {}


def fetch(url: str, timeout: float = TIMEOUT) -> bytes:
    return net.get(url, _headers(url), timeout=timeout)


def _key(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:20]


# --------------------------------------------------------------------------
# HTML and PDF to text
# --------------------------------------------------------------------------

BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6",
         "table", "section", "article", "ul", "ol", "blockquote", "pre", "center"}


def _clean_cells(cells: list[str]) -> list[str]:
    """
    Filings split one figure over several cells: "$" | "4,929" | ")" — join
    the pieces back, and keep a closing parenthesis on the number it closes,
    because "(0.12)" is a loss of twelve cents.
    """
    out = []
    for c in cells:
        c = re.sub(r"\s+", " ", c.replace("\xa0", " ")).strip()
        if c in ("", "$", "€", "£", "¥", "%", "US$", "NT$"):
            continue
        if c in (")", ")%", "%)", "pts)"):
            if out and out[-1].startswith("(") and not out[-1].endswith(")"):
                out[-1] += ")"
            continue
        out.append(c)
    return out


def _parse_doc(raw: bytes):
    from lxml import html as LH
    # Filings declare UTF-8 and are sometimes Windows-1252 underneath (a
    # bullet is byte 0x95), which reads as a replacement character. Decode
    # ourselves, falling back, and hand lxml text without its XML declaration
    # (lxml refuses a str that still has one).
    if isinstance(raw, bytes):
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            s = raw.decode("cp1252", "replace")
    else:
        s = raw
    s = re.sub(r"^\s*<\?xml[^>]*\?>", "", s)
    return LH.document_fromstring(s)


def html_text(raw: bytes) -> dict:
    """
    {"text": the document as lines, tables kept as "a | b | c" rows,
     "tables": [{"rows": [[cells]], "before": the text just above it}]}
    """
    doc = _parse_doc(raw)
    for bad in doc.xpath("//script|//style|//head|//noscript"):
        bad.drop_tree()
    # iXBRL filings carry a hidden header of machine facts; it is not text.
    for bad in doc.xpath("//*[@style and contains(translate(@style,' ',''),'display:none')]"):
        bad.drop_tree()
    for bad in doc.xpath("//*[local-name()='header' and contains(name(),'ix:')]"):
        bad.drop_tree()

    tables = []
    for t in doc.xpath("//table"):
        if t.xpath("ancestor::table"):
            continue
        rows = []
        for tr in t.xpath(".//tr"):
            cells = _clean_cells([td.text_content() for td in tr.xpath("./td|./th")])
            if cells:
                rows.append(cells)
        if not rows:
            continue
        # The heading above a table ("(in millions, except per share data)",
        # "Outlook") is often a paragraph outside it.
        before = []
        prev = t.getprevious()
        while prev is not None and len(" ".join(before)) < 300:
            # A comment has no text of its own, and asking it for one raises:
            # one "<!-- page 2 -->" above a table lost the whole document.
            if isinstance(prev.tag, str):
                s = re.sub(r"\s+", " ", prev.text_content() or "").strip()
                if s:
                    before.insert(0, s)
            prev = prev.getprevious()
        tables.append({"rows": rows, "before": " ".join(before)[-300:]})
        # Leave the table in the text as rows, so a reader of the text still
        # sees the numbers in order.
        flat = "\n".join(" | ".join(r) for r in rows)
        tail = t.tail
        t.clear()
        t.text = "\n" + flat + "\n"
        t.tail = tail

    for el in doc.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.split("}")[-1].lower()
        if tag in BLOCK:
            el.tail = "\n" + (el.tail or "")
        elif tag in ("td", "th"):
            el.tail = " | " + (el.tail or "")
    txt = doc.text_content()
    lines = [re.sub(r"[ \t\xa0]+", " ", l).strip() for l in txt.split("\n")]
    out, blank = [], False
    for l in lines:
        if not l:
            if not blank and out:
                out.append("")
            blank = True
            continue
        out.append(l)
        blank = False
    return {"text": "\n".join(out).strip(), "tables": tables}


def pdf_text(raw: bytes, max_pages: int = 120) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        r = PdfReader(io.BytesIO(raw))
        pages = []
        for i, p in enumerate(r.pages[:max_pages]):
            t = (p.extract_text() or "").strip()
            if t:
                pages.append(f"[page {i + 1}]\n{t}")
        return "\n\n".join(pages)
    except Exception:
        return ""


def is_pdf(raw: bytes, url: str = "") -> bool:
    return raw[:5] == b"%PDF-" or url.lower().split("?")[0].endswith(".pdf")


def read_url(url: str) -> dict:
    """Any document by URL, as text. Kept for good: documents do not change."""
    def load():
        raw = fetch(url)
        if is_pdf(raw, url):
            return {"url": url, "kind": "pdf", "text": pdf_text(raw), "bytes": len(raw)}
        h = html_text(raw)
        return {"url": url, "kind": "html", "text": h["text"], "bytes": len(raw)}
    return store.cache_through(f"ed:txt:{_key(url)}", FOREVER, load)


# --------------------------------------------------------------------------
# which SEC filer, for a symbol
# --------------------------------------------------------------------------

SUFFIX = re.compile(r"\b(n\.?v\.?|plc|ag|se|s\.?a\.?|inc|incorporated|corporation|corp|co|company|"
                    r"holdings?|group|groep|ltd|limited|the|class [a-c]|adr|ads|sponsored|"
                    r"on|de|l\.?p\.?|llc|oyj|asa|ab|spa|s\.?p\.?a\.?|nv|bv)\b")


def norm_name(n: str) -> str:
    n = (n or "").lower().replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]", " ", n)
    n = " ".join(w for w in n.split() if len(w) > 1)     # "n v" from "N.V."
    n = SUFFIX.sub(" ", n)
    return re.sub(r"\s+", " ", n).strip()


def _title_index() -> dict:
    """Normalised SEC company name -> ticker, built once a week."""
    def load():
        m = store.cache_through("sec:tickers", 7 * 86400, sec._load_tickers).get("map") or {}
        idx = {}
        for tk, v in m.items():
            k = norm_name(v.get("title") or "")
            # The first ticker listed for a filer is its common stock; later
            # ones are preferreds, warrants and notes.
            if k and k not in idx:
                idx[k] = tk
        return {"idx": idx}
    return store.cache_through("ed:titles", 7 * 86400, load).get("idx") or {}


def filer(sym: str, name: Optional[str] = None) -> Optional[dict]:
    """
    {ticker, cik, title, foreign} — the SEC filer behind a symbol.

    A US listing maps directly. An Amsterdam or Frankfurt line maps through
    the company's name when the company also has a US listing that files
    (ING, Unilever, Shell file 6-Ks; BMW and NN Group file nothing).
    """
    c = sec.cik_for(sym)
    tk = sym
    if not c and name:
        k = norm_name(name)
        idx = _title_index()
        hit = idx.get(k)
        if not hit and k:
            # "ing groep" vs "ing groep n v" are the same after the suffix
            # strip; a first-word match is only trusted when it is long.
            first = k.split(" ")[0]
            if len(first) >= 5:
                cands = [t for n2, t in idx.items() if n2.split(" ")[0] == first]
                hit = cands[0] if len(cands) == 1 else None
        if hit:
            tk = hit
            c = sec.cik_for(hit)
    if not c:
        return None
    sub = {}
    try:
        sub = sec.submissions(c["cik"])
    except Exception:
        pass
    forms = {f["form"] for f in (sub.get("filings") or [])[:200]}
    foreign = "10-Q" not in forms and ("6-K" in forms or "20-F" in forms)
    # A name match for a European line must be a company that reports to the
    # SEC as a foreign issuer: "NN Group" normalises to the same "nn" as the
    # American NN Inc., and BMW's name finds an ADR shell that files nothing.
    if tk != sym and not foreign:
        return None
    return {"ticker": tk, "cik": c["cik"], "title": c.get("title"), "foreign": foreign}


# --------------------------------------------------------------------------
# the filings for one report
# --------------------------------------------------------------------------

def filing_index(cik: int, acc: str) -> list[dict]:
    """Every document in a filing, with its exhibit type. Immutable, kept forever."""
    def load():
        from lxml import html as LH
        raw = fetch(sec.filing_url(cik, acc))
        doc = LH.document_fromstring(raw)
        out = []
        for tr in doc.xpath("//table[contains(@class,'tableFile')]//tr[td]"):
            tds = [re.sub(r"\s+", " ", t.text_content()).strip() for t in tr.xpath("td")]
            a = tr.xpath(".//a/@href")
            if len(tds) >= 4 and a:
                href = a[0].replace("/ix?doc=", "")
                url = "https://www.sec.gov" + href if href.startswith("/") else href
                out.append({"seq": tds[0], "desc": tds[1], "doc": tds[2].split(" ")[0],
                            "type": tds[3], "size": tds[4] if len(tds) > 4 else "",
                            "url": url})
        return {"docs": out}
    return store.cache_through(f"ed:idx:{acc}", FOREVER, load).get("docs") or []


EXTRA_KIND = [
    ("slides", re.compile(r"slide|presentation|deck|investor[\s_-]?pres|earnings[\s_-]?pres", re.I)),
    ("letter", re.compile(r"letter|shareholder|stockholder", re.I)),
    ("commentary", re.compile(r"commentary|prepared|remarks|script|cfo", re.I)),
    ("supplement", re.compile(r"supplement|financial[\s_-]?(data|summary|info)|fact[\s_-]?sheet|metrics|databook|trending", re.I)),
    ("release", re.compile(r"press|release|results|earnings", re.I)),
]


def classify(d: dict) -> Optional[str]:
    t = (d.get("type") or "").upper()
    if not (t.startswith("EX-99") or t in ("6-K", "EX-1", "EX-13")) and not d["doc"].lower().endswith(".pdf"):
        return None
    hay = f"{d.get('desc', '')} {d.get('doc', '')}"
    for kind, rx in EXTRA_KIND:
        if rx.search(hay):
            return kind
    if d["doc"].lower().endswith(".pdf"):
        return "slides"
    return "release" if t in ("EX-99.1", "EX-99", "6-K") else "exhibit"


def fresh_submissions(cik: int, max_age: float) -> dict:
    """The filing list, no older than max_age — seconds during a live report."""
    k = f"sec:sub:{cik}"
    if store.cache_age(k) > max_age:
        try:
            store.cache_put(k, sec._load_submissions(cik))
        except Exception:
            pass
    return sec.submissions(cik)


def _dd(s: str) -> date:
    return date.fromisoformat(s[:10])


def report_filings(sym: str, event_date: str, name: Optional[str] = None,
                   max_age: float = 6 * 3600) -> dict:
    """
    The earnings filing for a report on `event_date`, and the periodic report
    that goes with it.

    Earnings 8-Ks are item 2.02. A company reporting after the close files on
    the day; one reporting before the open files that morning. A foreign filer
    has no item codes on its 6-K, so the 6-K filed on the day with a results-
    shaped exhibit is taken.
    """
    f = filer(sym, name)
    if not f:
        return {"ok": False, "why": "no SEC filer for this symbol"}
    sub = fresh_submissions(f["cik"], max_age)
    d0 = _dd(event_date)
    release, periodic = None, None
    for x in sub.get("filings") or []:
        try:
            fd = _dd(x["date"])
        except Exception:
            continue
        if release is None and -1 <= (fd - d0).days <= 1:
            if x["form"] in ("8-K", "8-K/A") and "2.02" in (x.get("items") or ""):
                release = x
            elif x["form"] == "6-K" and f["foreign"]:
                release = release or x
        if x["form"] in sec.PERIODIC and -3 <= (fd - d0).days <= 80:
            rep = x.get("report") or ""
            if not rep or _dd(rep) <= d0:
                # Filings are newest first; keep the earliest one after the report.
                periodic = x
    out = {"ok": True, "filer": f, "release": None, "periodic": None}
    if release:
        docs = filing_index(f["cik"], release["acc"])
        for d in docs:
            d["kind"] = classify(d)
        # A 6-K often carries several exhibits; the release is the one that
        # says so, or failing that the first.
        rel = next((d for d in docs if d["kind"] == "release" and d["type"].startswith("EX-99")), None) \
            or next((d for d in docs if d["kind"] == "release"), None) \
            or next((d for d in docs if (d["type"] or "").startswith("EX-99")), None)
        out["release"] = {
            "form": release["form"], "date": release["date"], "acc": release["acc"],
            "accepted": release.get("accepted"), "items": release.get("items"),
            "index": sec.filing_url(f["cik"], release["acc"]),
            "doc": rel,
            "extras": [d for d in docs if d is not rel and d["kind"] in
                       ("slides", "letter", "commentary", "supplement", "release", "exhibit")],
        }
    if periodic:
        out["periodic"] = {
            "form": periodic["form"], "date": periodic["date"], "acc": periodic["acc"],
            "report": periodic.get("report"), "accepted": periodic.get("accepted"),
            "url": sec.filing_url(f["cik"], periodic["acc"], periodic["doc"]),
            "index": sec.filing_url(f["cik"], periodic["acc"]),
        }
    return out


# --------------------------------------------------------------------------
# reading a press release
# --------------------------------------------------------------------------

def num(s) -> Optional[float]:
    """'$1,234.5' -> 1234.5, '(0.12)' -> -0.12, '—' -> None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace("\xa0", "").replace(" ", "")
    t = re.sub(r"^[~≈]|^(US\$|NT\$|\$|€|£|¥)", "", t)
    t = t.replace("$", "").replace("€", "").replace(",", "")
    neg = t.startswith("(") and t.endswith(")")
    t = t.strip("()%")
    if t.startswith("−") or t.startswith("–"):
        t, neg = t[1:], True
    if not re.fullmatch(r"-?\d+(\.\d+)?", t):
        return None
    v = float(t)
    return -v if neg else v


def _label(s: str) -> str:
    s = re.sub(r"\(\d{1,2}\)|\([a-z]\)|\*+", "", s)
    s = re.sub(r"(?<=[a-z])\d{1,2}$", "", s.strip())        # "operating profit1"
    return re.sub(r"\s+", " ", s).strip(" :").lower()


def _nums(row: list[str]) -> list[float]:
    return [v for v in (num(c) for c in row[1:]) if v is not None]


MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()


def _period(cell: str) -> Optional[tuple]:
    """
    A column heading as (sortable month number, is_forecast), or None.
    'June 30, 2026', 'Jun. 30,2026', '6/30/26', "Q2'26", 'Q2 2026', '2Q26',
    "Q3'26 Forecast".
    """
    s = cell.lower().replace("’", "'")
    fc = bool(re.search(r"forecast|guidance|outlook|estimate|projected", s))
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(\d{1,2})?,?\s*(\d{4})", s)
    if m:
        return int(m.group(3)) * 12 + MONTHS.index(m.group(1)), fc
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", s)
    if m:
        y = int(m.group(3))
        return (y + 2000 if y < 100 else y) * 12 + int(m.group(1)) - 1, fc
    m = re.search(r"\bq([1-4])\s*(?:fy)?\s*'?(\d{2}|\d{4})\b", s) or re.search(r"\b([1-4])q\s*'?(\d{2}|\d{4})\b", s)
    if m:
        y = int(m.group(2))
        return (y + 2000 if y < 100 else y) * 12 + int(m.group(1)) * 3 - 1, fc
    return None


def _cols(t: dict) -> dict:
    """
    Which figure column is this quarter, which is the same quarter a year
    earlier, and which (if any) is a forecast. Statements put the quarter
    first; shareholder letters run oldest to newest with the forecast last.
    Without dated headings the first column is taken to be the quarter.
    """
    for r in t["rows"][:6]:
        per = [_period(c) for c in r]
        if sum(1 for p in per if p) < 2:
            continue
        # Headings either start at the first cell or after a label cell.
        if per[0] is None:
            per = per[1:]
        keys = [p for p in per]
        actual = [(i, p[0]) for i, p in enumerate(keys) if p and not p[1]]
        if not actual:
            continue
        top = max(k for _, k in actual)
        cur = next(i for i, k in actual if k == top)
        ya = next((i for i, k in actual if k == top - 12), None)
        fc = next((i for i, p in enumerate(keys) if p and p[1]), None)
        return {"cur": cur, "ya": ya, "fc": fc, "key": top,
                "fc_label": r[fc + (1 if _period(r[0]) is None else 0)] if fc is not None else None}
    # Headings of plain years under "Three Months Ended June 30": 2026 | 2025.
    for r in t["rows"][:6]:
        ys = [int(c) if re.fullmatch(r"(19|20)\d\d", c.strip()) else None for c in r]
        if sum(1 for y in ys if y) >= 2:
            if ys[0] is None:
                ys = ys[1:]
            yy = [(i, y) for i, y in enumerate(ys) if y]
            top = max(y for _, y in yy)
            cur = next(i for i, y in yy if y == top)
            ya = next((i for i, y in yy if y == top - 1), None)
            return {"cur": cur, "ya": ya, "fc": None, "key": None, "fc_label": None}
    return {"cur": 0, "ya": None, "fc": None, "key": None, "fc_label": None}


def _vals(row: list[str]) -> list[Optional[float]]:
    return [num(c) for c in row[1:]]


def _at(vals: list, i: Optional[int]) -> Optional[float]:
    return vals[i] if i is not None and 0 <= i < len(vals) else None


UNIT = re.compile(r"in (millions|thousands|billions)", re.I)
QTR = re.compile(r"three months|quarters? ended|3 mos|fourth quarter|third quarter|second quarter|"
                 r"first quarter|\bq[1-4]\b|13 weeks|twelve weeks|12 weeks|16 weeks", re.I)
YTD = re.compile(r"six months|nine months|twelve months|year ended|years ended|fiscal year|6 mos|9 mos|"
                 r"12 mos|26 weeks|39 weeks|52 weeks|53 weeks|year to date|ytd", re.I)

REV = re.compile(r"^(total )?(net )?(operating )?(revenues?|sales)( and other income)?(, net)?$|"
                 r"^(total )?net (revenues?|sales)$|^revenues?, net$|^total revenues? and other income$|"
                 r"^total (net )?revenues?$|^net revenues?$|^total (net )?sales$|^net sales$|^total operating revenues?$")
NI = re.compile(r"^net (income|earnings|income \(loss\)|loss|\(loss\) income)( attributable to [a-z0-9 ,.'&]+)?$")
OI = re.compile(r"^(total )?(operating income|income from operations|operating profit|operating income \(loss\)|"
                r"operating \(loss\) income|business segment operating profit)$")
EPS_ROW = re.compile(r"diluted|per diluted share|earnings per share|net income per share")
# A table's unit caption, "($ in millions, except per share data)": it names the units, not a row.
UNIT_CAPTION = re.compile(r"\bexcept\b|\bin (?:thousands|millions|billions)\b|\$\s*in\b|amounts in|unaudited", re.I)
ADJ = re.compile(r"non-?gaap|adjusted|core|excluding|ex-items|underlying|comparable", re.I)


def _table_kind(t: dict) -> Optional[str]:
    """'q' when the first figure column is a quarter, 'ytd' when it is not."""
    head = " ".join(" ".join(r) for r in t["rows"][:4]) + " " + t["before"][-160:]
    q, y = QTR.search(head), YTD.search(head)
    if q and (not y or q.start() < y.start() or head.find(q.group(0)) < head.find(y.group(0))):
        return "q"
    if y and not q:
        return "ytd"
    return None


def _unit(t: dict) -> Optional[float]:
    head = t["before"] + " " + " ".join(" ".join(r) for r in t["rows"][:3])
    m = UNIT.search(head)
    return {"millions": 1e6, "thousands": 1e3, "billions": 1e9}[m.group(1).lower()] if m else None


def _scale(v: Optional[float], unit: Optional[float], ref: Optional[float]) -> tuple[Optional[float], str]:
    """Figure in dollars. Checked against the consensus when there is one."""
    if v is None:
        return None, ""
    if ref:
        best = min((1e3, 1e6, 1e9, 1.0), key=lambda u: abs(v * u / ref - 1))
        if unit and 0.5 <= v * unit / ref <= 2:
            return v * unit, "table"
        if 0.5 <= v * best / ref <= 2:
            return v * best, "scaled to consensus"
        return v * (unit or 1e6), "unit assumed" if not unit else "table"
    return v * (unit or 1e6), "table" if unit else "unit assumed (millions)"


def _row_pick(t: dict, rx, prefer=None, cols: Optional[dict] = None) -> Optional[dict]:
    cols = cols or _cols(t)
    hits = []
    for i, r in enumerate(t["rows"]):
        lab = _label(r[0])
        if rx.search(lab) and not re.search(r"non-?controlling|minority", lab):
            vs = _vals(r)
            if _at(vs, cols["cur"]) is not None:
                hits.append((i, lab, vs))
    if not hits:
        return None
    if prefer:
        pref = [h for h in hits if prefer in h[1]]
        hits = pref or hits
    i, lab, vs = hits[0]
    return {"label": lab, "value": _at(vs, cols["cur"]), "year_ago": _at(vs, cols["ya"]),
            "forecast": _at(vs, cols["fc"]), "row": i}


def _eps_pick(t: dict, adjusted: bool = False, cols: Optional[dict] = None) -> Optional[dict]:
    """
    Diluted EPS: the row that says 'diluted' with per-share-sized figures.
    Some statements put 'Diluted' as a heading and the figure on the next row
    ('Net income attributable to Verizon | 0.92').
    """
    cols = cols or _cols(t)
    rows = t["rows"]
    top = " ".join(" ".join(r) for r in rows[:2]) + " " + t["before"][-100:]
    table_adj = bool(ADJ.search(top))
    heading, heading_i = None, -9       # the last label-only row above, and where it stood
    under_hit = None                    # a figure found only by its heading: used when no row says EPS itself
    for i, r in enumerate(rows):
        lab = _label(r[0])
        vs = _vals(r)
        v = _at(vs, cols["cur"])
        if v is None:
            if not any(x is not None for x in vs):
                heading, heading_i = lab, i
            continue
        # A heading names the row directly under it, and only that one (Verizon's
        # "Diluted" over "Net income attributable to Verizon | 0.92"). A unit caption
        # is no heading: NVIDIA's "($ in billions, except earnings per share)" made
        # every row below it EPS, and on 17 November 2026 its revenue, $115.8bn,
        # was read as $115.80 a share.
        under = bool(heading and i == heading_i + 1 and EPS_ROW.search(heading)
                     and not UNIT_CAPTION.search(heading))
        if not (EPS_ROW.search(lab) or under):
            continue
        if re.search(r"weighted|shares used|number of shares|\bbasic\b|shares outstanding|"
                     r"dilutive (securities|shares|effect)|growth|change", lab):
            continue
        cell = r[1 + cols["cur"]] if 1 + cols["cur"] < len(r) else ""
        if abs(v) >= 500 or "." not in cell:
            continue
        ctx = lab + " " + (heading or "")
        if re.search(r"\bgaap\b", ctx) and not re.search(r"non-?\s?gaap", ctx):
            row_adj = False
        else:
            row_adj = bool(ADJ.search(ctx)) or table_adj
        if row_adj != adjusted:
            continue
        hit = {"label": lab, "value": v, "year_ago": _at(vs, cols["ya"]),
               "forecast": _at(vs, cols["fc"]), "row": i}
        if EPS_ROW.search(lab):
            return hit                  # a row that says what it is wins
        under_hit = under_hit or hit
    return under_hit


TXT_REV = re.compile(r"(?:total |net )?(?:revenues?|net sales|sales)\s+(?:was|were|of|totaled|totalled|reached|came in at|"
                     r"(?:increased|grew|rose|decreased|declined|fell)[^$.]{0,60}?to)\s+(?:a record\s+)?"
                     r"(?:US)?\$\s?(\d[\d.,]*)\s*(billion|million|bn|m)\b", re.I)
TXT_EPS_ADJ = re.compile(r"(?:non-?gaap|adjusted|core)\s+(?:diluted\s+)?(?:eps|earnings per (?:diluted )?share|"
                         r"net income per (?:diluted )?share)\s+(?:was|were|of|totaled|came in at|"
                         r"(?:increased|grew|rose|decreased|declined|fell)[^$.]{0,40}?to)?\s*(?:a record\s+)?"
                         r"\(?\$\s?(-?\d+\.\d{2})", re.I)
TXT_EPS_ADJ2 = re.compile(r"\$\s?(-?\d+\.\d{2})\s+(?:per (?:diluted )?share\s+)?(?:on an? )?(?:non-?gaap|adjusted)\b", re.I)
TXT_EPS = re.compile(r"(?:diluted )?(?:eps|earnings per (?:diluted )?share|net income per (?:diluted )?share)\s+"
                     r"(?:was|were|of|totaled|came in at|(?:increased|grew|rose|decreased|declined|fell)[^$.]{0,40}?to)\s*"
                     r"\(?\$\s?(-?\d+\.\d{2})", re.I)

GUIDE = re.compile(r"\b(expects?|expected|expecting|outlook|guidance|guides?|guiding|forecasts?|anticipat\w*|"
                   r"projects?|projected|now sees|reaffirm\w*|reiterat\w*|rais(?:e|es|ed|ing) (?:its|the|our)|"
                   r"narrow\w*|lower(?:s|ed)? (?:its|the|our)|target(?:s|ing)?)\b", re.I)
PERIOD = re.compile(r"\b(quarter|q[1-4]|fiscal|full[- ]year|full year|the year|20\d\d|second half|first half|"
                    r"next (?:quarter|year)|annual)\b", re.I)
FIGURE = re.compile(r"\$\s?\d|\d\s?%|\d+(?:\.\d+)?\s?(?:billion|million|percent|bps|basis points)", re.I)
BOILER = re.compile(r"forward-looking|safe harbor|risks and uncertainties|undue reliance|private securities|"
                    r"actual results (?:may|could)|no obligation to update|webcast|conference call|"
                    r"will host|dial[- ]in|replay", re.I)
METRIC = [
    ("revenue", re.compile(r"revenue|net sales|\bsales\b|top[- ]line", re.I)),
    ("eps", re.compile(r"\beps\b|per share|earnings per", re.I)),
    ("op_margin", re.compile(r"operating margin", re.I)),
    ("gross_margin", re.compile(r"gross margin", re.I)),
    ("op_income", re.compile(r"operating (income|profit)|ebit\b", re.I)),
    ("capex", re.compile(r"capital expenditure|capex", re.I)),
    ("fcf", re.compile(r"free cash flow", re.I)),
    ("ebitda", re.compile(r"ebitda", re.I)),
]
RANGE = re.compile(r"(?:US)?\$?\s?(\d[\d,]*(?:\.\d+)?)\s*(billion|million|bn|mm|b|m|%)?(?![a-z])\s*"
                   r"(?:to|-|–|—|and)\s*(?:US)?\$?\s?(\d[\d,]*(?:\.\d+)?)\s*(billion|million|bn|mm|b|m|%)?(?![a-z])",
                   re.I)


def _mult(u: Optional[str]) -> float:
    u = (u or "").lower()
    return 1e9 if u in ("billion", "bn", "b") else 1e6 if u in ("million", "mm", "m") else 1.0


def sentences(text: str) -> list[str]:
    out = []
    for para in text.split("\n"):
        para = para.strip()
        if not para or para.count("|") >= 3:
            continue
        for s in re.split(r"(?<=[.!?])\s+(?=[A-Z“\"(•◦▪●])", para):
            s = s.strip().lstrip("•◦▪●·*–— \t").strip()
            if 25 <= len(s) <= 600:
                out.append(s)
    return out


# "Revenue was in line with our forecast" looks forward in its words and back
# in its meaning; it is a result, not guidance.
PASTREF = re.compile(r"(in[- ]line with|than|vs\.?|versus|above|below|ahead of|short of|exceeded|beat|met)\s+"
                     r"(our |the |its |prior |previous |consensus )*(forecast|guidance|outlook|expectations?)|"
                     r"\b(as|than)\s+(we\s+)?(expected|anticipated|forecast|planned)\b", re.I)


def fmt_amount(v: float, per_share: bool = False) -> str:
    if per_share:
        return f"${v:,.2f}"
    a = abs(v)
    return f"${v / 1e9:,.2f}bn" if a >= 1e9 else (f"${v / 1e6:,.1f}m" if a >= 1e6 else f"${v:,.0f}")


def guidance_lines(text: str, tables: Optional[list] = None, limit: int = 14) -> list[dict]:
    """Forward-looking statements with a figure and a period in them."""
    out, seen = [], set()
    for s in sentences(text):
        if not GUIDE.search(s) or not FIGURE.search(s) or not PERIOD.search(s) or BOILER.search(s):
            continue
        if PASTREF.search(s) and not re.search(r"\b(expect|anticipate|will|now sees|outlook for|guidance for)\b", s, re.I):
            continue
        k = s[:80].lower()
        if k in seen:
            continue
        seen.add(k)
        g = {"text": s, "metric": next((m for m, rx in METRIC if rx.search(s)), None)}
        m = RANGE.search(s)
        if m:
            lo, hi = num(m.group(1)), num(m.group(3))
            u = m.group(4) or m.group(2)
            if lo is not None and hi is not None and hi >= lo:
                g.update(low=lo * (1 if u == "%" else _mult(u)), high=hi * (1 if u == "%" else _mult(u)),
                         unit="%" if u == "%" else "$")
        pm = re.search(r"\b(fourth|third|second|first) quarter\b|\bq[1-4]\b|\bfull[- ]year\b|\bfiscal (?:year )?20\d\d\b|"
                       r"\bfy ?20\d\d\b|\bfy\d\d\b|\bnext quarter\b", s, re.I)
        g["period"] = pm.group(0) if pm else None
        out.append(g)
        if len(out) >= limit:
            break
    # Outlook tables: "Sales | ~$79,750 - $81,750 | $77,500 - $80,000"
    for t in tables or []:
        head = (t["before"] + " " + " ".join(" ".join(r) for r in t["rows"][:2])).lower()
        if not re.search(r"outlook|guidance|forecast|expect|current update|prior|range", head):
            continue
        unit = _unit(t) or 1e6
        for r in t["rows"]:
            cell = r[1] if len(r) > 1 else ""
            m = RANGE.search(cell)
            if not m:
                continue
            lab = _label(r[0])
            lo, hi = num(m.group(1)), num(m.group(3))
            if lo is None or hi is None:
                continue
            pct = "%" in cell
            metric = next((mm for mm, rx in METRIC if rx.search(lab)), None)
            per = metric == "eps" or "per share" in lab
            mult = 1 if pct or per else unit
            out.append({"text": f"{r[0]}: {cell}" + (f" (was {r[2]})" if len(r) > 2 else ""),
                        "metric": metric, "low": lo * mult, "high": hi * mult,
                        "unit": "%" if pct else "$", "period": None, "table": True,
                        "prior": r[2] if len(r) > 2 else None})
    return out


def highlights(text: str, limit: int = 10) -> list[str]:
    """The top of the release: the lines with figures in them, before the tables."""
    out = []
    for s in sentences(text[:25000]):
        if FIGURE.search(s) and not BOILER.search(s) and len(s) < 420:
            out.append(s)
        if len(out) >= limit:
            break
    return out


CCY = [("TWD", r"NT\$"), ("EUR", r"€|\bEUR\b|\beuros?\b"), ("GBP", r"£|\bGBP\b"), ("JPY", r"¥|\bJPY\b|\byen\b"),
       ("CNY", r"\bRMB\b|\bCNY\b"), ("KRW", r"\bKRW\b"), ("CHF", r"\bCHF\b"), ("CAD", r"C\$|\bCAD\b"),
       ("DKK", r"\bDKK\b"), ("SEK", r"\bSEK\b")]


def _currency(head: str) -> str:
    """The currency a release reports in: dollars unless another clearly dominates."""
    usd = len(re.findall(r"(?<![A-Z])(?:US)?\$", head)) - len(re.findall(r"NT\$|C\$|A\$", head))
    best = max(((len(re.findall(rx, head)), c) for c, rx in CCY), default=(0, "USD"))
    return best[1] if best[0] > max(3, usd) else "USD"


def _adj_eps_text(text: str) -> Optional[dict]:
    """
    'adjusted net earnings were $6.38 per share', 'Diluted earnings per share
    was $4.81 on a GAAP basis and $4.74 ... on a non-GAAP basis': the dollar
    figure nearest the word that makes it adjusted, in a sentence about the
    quarter rather than the outlook.
    """
    for s in sentences(text[:40000]):
        a = ADJ.search(s)
        if not a or GUIDE.search(s) or re.search(r"full[- ]year|outlook|prior[- ]year|year[- ]ago", s, re.I):
            continue
        if not re.search(r"per (?:diluted )?share|\beps\b", s, re.I):
            continue
        # The figure after the word first ("adjusted EPS, excluding special
        # items, was $1.30"), else the one just before it ("$4.74 ... on a
        # non-GAAP basis") — never the year-ago figure further back.
        amts = [(m.start(), float(m.group(1))) for m in
                re.finditer(r"\$\s?(-?\d+\.\d{2})(?!\d)(?!\s*(?:billion|million|bn|b\b|m\b))", s)]
        after = [(p - a.end(), v) for p, v in amts if 0 <= p - a.end() <= 90]
        before = [(a.start() - p, v) for p, v in amts if 0 < a.start() - p <= 60]
        pick = min(after)[1] if after else (min(before)[1] if before else None)
        if pick is not None:
            return {"value": pick, "label": "adjusted EPS (text)", "src": "text", "year_ago": None,
                    "sentence": s[:300]}
    return None


def parse_release(raws, cons_rev: Optional[float] = None,
                  cons_eps: Optional[float] = None) -> dict:
    """
    The headline numbers of an earnings release — one document, or the
    release plus what was furnished with it (UnitedHealth puts its tables in a
    second exhibit). `cons_*` are only used to choose the unit when a table
    does not state it, and to decide which EPS is the comparable one — never
    to fill a gap.
    """
    docs = raws if isinstance(raws, (list, tuple)) else [raws]
    texts, tables = [], []
    for raw in docs:
        if not raw:
            continue
        if is_pdf(raw):
            texts.append(pdf_text(raw))
        else:
            h = html_text(raw)
            texts.append(h["text"])
            tables += h["tables"]
    text = "\n\n".join(texts)
    res = {"revenue": None, "eps_gaap": None, "eps_adj": None, "net_income": None,
           "op_income": None, "guidance": [], "highlights": [], "notes": [], "period": None}

    cands = []
    for ti, t in enumerate(tables):
        if _table_kind(t) == "ytd":
            continue
        cols = _cols(t)
        rev = _row_pick(t, REV, "total", cols)
        eps = _eps_pick(t, False, cols)
        if not (rev or eps):
            continue
        ni = _row_pick(t, NI, "attributable", cols)
        score = (2 if rev else 0) + (2 if eps else 0) + (1 if ni else 0) + \
                (1 if _table_kind(t) == "q" else 0)
        cands.append({"i": ti, "t": t, "cols": cols, "rev": rev, "eps": eps, "ni": ni, "score": score})

    # The quarter being reported is the latest dated one; undated tables
    # (headings of plain years) stay in the running.
    keys = [c["cols"]["key"] for c in cands if c["cols"]["key"]]
    if keys:
        latest = max(keys)
        cands = [c for c in cands if c["cols"]["key"] in (latest, None)]
        res["period"] = f"{MONTHS[latest % 12].title()} {latest // 12}"

    def scaled(c):
        unit = _unit(c["t"])
        v, how = _scale(c["rev"]["value"], unit, cons_rev)
        return v, how, (v / c["rev"]["value"] if v and c["rev"]["value"] else unit)

    with_rev = [c for c in cands if c["rev"]]
    if cons_rev:
        near = [c for c in with_rev if abs(scaled(c)[0] / cons_rev - 1) <= 0.25]
        with_rev = near or with_rev
    with_rev.sort(key=lambda c: (-c["score"], c["i"]))
    unit_used = None
    if with_rev:
        c = with_rev[0]
        v, how, unit_used = scaled(c)
        ya = c["rev"]["year_ago"] * unit_used if c["rev"]["year_ago"] and unit_used else None
        res["revenue"] = {"value": v, "year_ago": ya, "label": c["rev"]["label"], "src": how,
                          "yoy": round((v / ya - 1) * 100, 2) if v and ya else None}
        for key, row in (("net_income", c["ni"]), ("op_income", _row_pick(c["t"], OI, None, c["cols"]))):
            if row:
                u = unit_used or 1e6
                res[key] = {"value": row["value"] * u, "label": row["label"],
                            "year_ago": row["year_ago"] * u if row["year_ago"] is not None else None}
    with_eps = sorted([c for c in cands if c["eps"]], key=lambda c: (-c["score"], c["i"]))
    if with_eps:
        e = with_eps[0]["eps"]
        res["eps_gaap"] = {"value": e["value"], "year_ago": e["year_ago"], "label": e["label"], "src": "table"}

    for t in tables:
        if _table_kind(t) == "ytd":
            continue
        cols = _cols(t)
        if keys and cols["key"] and cols["key"] != max(keys):
            continue
        e = _eps_pick(t, True, cols)
        if e:
            res["eps_adj"] = {"value": e["value"], "year_ago": e["year_ago"], "label": e["label"], "src": "table"}
            break
    # A per-share figure fifteen times the consensus is a misread row (revenue, a
    # share count), not a result: drop it, and let the release's own sentence speak.
    if cons_eps and abs(cons_eps) >= 0.05:
        for k in ("eps_gaap", "eps_adj"):
            if res[k] and abs(res[k]["value"]) > 15 * abs(cons_eps) + 1:
                res["notes"].append(f"{'a GAAP' if k == 'eps_gaap' else 'an adjusted'} EPS of {res[k]['value']} read from the "
                                    f"'{res[k]['label']}' row was {abs(res[k]['value'] / cons_eps):.0f}× the consensus: left out")
                res[k] = None
    head = text[:40000]
    if not res["eps_adj"]:
        res["eps_adj"] = _adj_eps_text(head)
    if not res["eps_gaap"]:
        m = TXT_EPS.search(head)
        if m:
            res["eps_gaap"] = {"value": float(m.group(1)), "label": "EPS (text)", "src": "text", "year_ago": None}
    if not res["revenue"]:
        m = TXT_REV.search(head)
        if m:
            v = num(m.group(1)) * _mult(m.group(2))
            if not cons_rev or 0.5 <= v / cons_rev <= 2:
                res["revenue"] = {"value": v, "label": "revenue (text)", "src": "text", "year_ago": None, "yoy": None}

    # Which EPS the consensus is on: usually the adjusted one where a company
    # reports one. Take the one nearer the consensus, and say which.
    # The street's number is on the adjusted basis whenever a company reports
    # one; nearness to the consensus is no guide, since that is the surprise.
    # Only a figure wildly off it (a misread) falls back to GAAP.
    res["eps_basis"] = "eps_adj" if res["eps_adj"] else ("eps_gaap" if res["eps_gaap"] else None)
    if res["eps_adj"] and res["eps_gaap"] and cons_eps:
        off = lambda v: abs(v - cons_eps) / max(abs(cons_eps), 0.05)
        if off(res["eps_adj"]["value"]) > 0.75 and off(res["eps_gaap"]["value"]) < 0.25:
            res["eps_basis"] = "eps_gaap"
    res["currency"] = _currency(text[:20000])
    if res["currency"] != "USD":
        res["notes"].append(f"reported in {res['currency']}; not compared with a dollar consensus")

    guide = guidance_lines(text, tables)
    # A forecast column (Netflix writes next quarter's numbers into the table).
    for c in cands:
        if c["cols"]["fc"] is None:
            continue
        u = _unit(c["t"]) or unit_used or 1e6
        for metric, row, mult in (("revenue", c["rev"], u), ("eps", c["eps"], 1),
                                  ("op_income", _row_pick(c["t"], OI, None, c["cols"]), u)):
            if row and row.get("forecast") is not None:
                amt = fmt_amount(row["forecast"] * mult, per_share=metric == "eps")
                guide.insert(0, {"text": f"{row['label'][:1].upper() + row['label'][1:]}: {amt} ({c['cols']['fc_label']})",
                                 "metric": metric, "low": row["forecast"] * mult, "high": row["forecast"] * mult,
                                 "unit": "$", "period": "next quarter", "table": True})
        break
    res["guidance"] = guide
    res["highlights"] = highlights(text)
    res["words"] = len(text.split())
    return res


def release_text(url: str) -> str:
    return (read_url(url) or {}).get("text") or ""


# --------------------------------------------------------------------------
# the 10-Q: management's discussion
# --------------------------------------------------------------------------

MDA_START = re.compile(r"item\s*[27]\.?\s*[\.:\-–—]?\s*management[’'`]?s?\s+discussion\s+and\s+analysis", re.I)
MDA_END = re.compile(r"item\s*[37]a?\.?\s*[\.:\-–—]?\s*(quantitative\s+and\s+qualitative|controls\s+and\s+procedures|"
                     r"financial\s+statements\s+and\s+supplementary)", re.I)


def mdna(url: str) -> dict:
    """The MD&A section of a 10-Q or 10-K, whole. The table of contents is skipped."""
    def load():
        t = read_url(url).get("text") or ""
        best = ""
        for m in MDA_START.finditer(t):
            e = MDA_END.search(t, m.end())
            seg = t[m.start(): e.start() if e else m.start() + 400000]
            if len(seg) > len(best):
                best = seg
        return {"url": url, "text": best, "words": len(best.split()),
                "found": bool(best), "doc_words": len(t.split())}
    return store.cache_through(f"ed:mdna:{_key(url)}", FOREVER, load)


# --------------------------------------------------------------------------
# call transcripts (the Motley Fool)
# --------------------------------------------------------------------------

FOOL = "https://www.fool.com"
SLUG = re.compile(r"/earnings/call-transcripts/(\d{4})/(\d\d)/(\d\d)/([a-z0-9\-]+)/")
SLUG_TK = re.compile(r"(?:^|-)([a-z]{1,5}(?:-[a-z])?)-q([1-4])-(\d{4})-earnings")


def _fool_links(html_: str) -> list[dict]:
    out = {}
    for m in SLUG.finditer(html_):
        slug = m.group(4)
        t = SLUG_TK.search(slug)
        if not t:
            continue
        url = FOOL + m.group(0)
        out[url] = {"url": url, "date": f"{m.group(1)}-{m.group(2)}-{m.group(3)}",
                    "ticker": t.group(1).upper(), "fq": int(t.group(2)), "fy": int(t.group(3))}
    return list(out.values())


def _fool_page(n: int) -> list[dict]:
    url = f"{FOOL}/earnings-call-transcripts/" + (f"page/{n}/" if n > 1 else "")
    ttl = 900 if n <= 3 else 3 * 3600
    return store.cache_through(f"fool:list:{n}", ttl,
                               lambda: {"links": _fool_links(net.get(url, {}, 20).decode("utf-8", "replace"))}
                               ).get("links") or []


def _fool_quote(sym: str) -> list[dict]:
    def load():
        tk = sym.lower().replace(".", "-")
        for ex in ("nasdaq", "nyse", "nysemkt", "otc"):
            r = net.fetch(f"{FOOL}/quote/{ex}/{tk}/", {}, 20)
            if r["ok"]:
                links = [l for l in _fool_links(r["body"].decode("utf-8", "replace"))
                         if l["ticker"] == sym.upper().replace(".", "-")]
                return {"links": links, "exchange": ex}
        return {"links": [], "exchange": None}
    return store.cache_through(f"fool:quote:{sym}", 6 * 3600, load).get("links") or []


def _fool_month(y: int, m: int) -> list[dict]:
    """Every transcript the Fool published in a month, from its sitemap."""
    cur = date.today()
    ttl = 1800 if (y, m) == (cur.year, cur.month) else 7 * 86400
    return store.cache_through(
        f"fool:map:{y}-{m:02d}", ttl,
        lambda: {"links": _fool_links(net.get(f"{FOOL}/sitemap/{y}/{m:02d}", {}, 30).decode("utf-8", "replace"))}
    ).get("links") or []


def find_transcript(sym: str, event_date: str, deep: bool = True) -> Optional[dict]:
    """
    The Fool's transcript of the call on or just after `event_date`.

    Looked up on the company's quote page (which lists only some), then in the
    Fool's monthly sitemaps (every transcript it published), then — in the
    first days after a call, before the sitemap catches up — in the running
    list of the newest ones. The Fool does not transcribe every call: when
    none of these has it, there is none to find.
    """
    tk = sym.upper().split(".")[0].replace("-", "")
    d0 = _dd(event_date)

    def fits(l):
        dd = _dd(l["date"])
        return l["ticker"].replace("-", "") == tk and -2 <= (dd - d0).days <= 21

    cands = [l for l in _fool_quote(sym) if fits(l)]
    if not cands and deep:
        months = sorted({(d0.year, d0.month), ((d0 + timedelta(days=21)).year, (d0 + timedelta(days=21)).month)})
        for y, m in months:
            if date(y, m, 1) > date.today():
                continue
            try:
                cands += [l for l in _fool_month(y, m) if fits(l)]
            except Exception:
                pass
    if not cands and deep and 0 <= (date.today() - d0).days <= 4:
        for n in range(1, 16):
            try:
                page = _fool_page(n)
            except Exception:
                break
            cands += [l for l in page if fits(l)]
            if cands or not page or min(_dd(l["date"]) for l in page) < d0 - timedelta(days=2):
                break
    if not cands:
        return None
    return min(cands, key=lambda l: abs((_dd(l["date"]) - d0).days))


OPERATOR_Q = re.compile(r"(?:question|line)\s+(?:is\s+)?(?:will\s+)?(?:comes?\s+)?from\s+(?:the\s+line\s+of\s+)?"
                        r"([A-Z][A-Za-z.'\- ]{2,40}?)\s+(?:with|from|of|at)\s+([A-Z][A-Za-z&.,'\- ]{1,50}?)"
                        r"(?:[.,;]|\s+(?:please|your|you)|$)")


FOOL_BOILER = re.compile(r"motley fool analyst|email protected|\[email|stock advisor|should you invest|"
                         r"premium investing services", re.I)


def parse_fool(url: str) -> dict:
    """A Fool transcript as sections and turns — prepared remarks, then Q&A."""
    def load():
        import html as H
        raw = net.get(url, {}, 25).decode("utf-8", "replace")
        pub = re.search(r'"datePublished":"([^"]+)"', raw)
        i = raw.find("article-body")
        body = raw[i:] if i >= 0 else raw
        j = body.find("</article>")
        body = body[:j] if j > 0 else body
        body = re.sub(r"<script.*?</script>", "", body, flags=re.S)
        body = re.sub(r"<h2[^>]*>(.*?)</h2>", lambda m: "\n##" + re.sub(r"<[^>]+>", "", m.group(1)) + "\n", body, flags=re.S)
        body = re.sub(r"<(p|h3|li|br|div)[^>]*>", "\n", body)
        text = H.unescape(re.sub(r"<[^>]+>", "", body))
        lines = [re.sub(r"\s+", " ", l).strip() for l in text.split("\n")]
        lines = [l for l in lines if l]

        sections, cur = {}, "_"
        for l in lines:
            if l.startswith("##"):
                cur = l[2:].strip().upper()
                sections.setdefault(cur, [])
                continue
            sections.setdefault(cur, []).append(l)

        participants = []
        for l in sections.get("CALL PARTICIPANTS", []):
            m = re.match(r"^(.+?)\s+[-–—]\s+(.+)$", l)
            if m:
                participants.append({"title": m.group(1).strip(), "name": m.group(2).strip()})
        execs = {p["name"] for p in participants}

        full = next((v for k, v in sections.items() if "TRANSCRIPT" in k), [])
        turns, analysts = [], {}
        speaker_rx = re.compile(r"^([A-Z][A-Za-z.'\- ]{1,40}?):\s+(.*)$")
        for l in full:
            m = speaker_rx.match(l)
            if m and (m.group(1) in execs or m.group(1) == "Operator" or m.group(1) in analysts
                      or len(m.group(1).split()) in (2, 3)):
                turns.append({"speaker": m.group(1), "text": m.group(2)})
            elif turns:
                turns[-1]["text"] += "\n" + l
            if turns and turns[-1]["speaker"] == "Operator":
                for om in OPERATOR_Q.finditer(turns[-1]["text"]):
                    analysts.setdefault(om.group(1).strip(), om.group(2).strip())
        qa = False
        for t in turns:
            if t["speaker"] == "Operator" and re.search(r"question", t["text"], re.I) and \
                    re.search(r"first question|(comes|come) from|line of", t["text"], re.I):
                qa = True
            t["section"] = "qa" if qa else "prepared"
            t["role"] = ("operator" if t["speaker"] == "Operator" else
                         "exec" if t["speaker"] in execs else
                         "analyst" if t["speaker"] in analysts else
                         ("exec" if not qa else "analyst"))
            if t["role"] == "analyst":
                t["firm"] = analysts.get(t["speaker"])
            else:
                t["title"] = next((p["title"] for p in participants if p["name"] == t["speaker"]), None)
        title = re.search(r"<title>(.*?)</title>", raw, re.S)
        return {
            "url": url, "source": "The Motley Fool",
            "title": H.unescape(title.group(1)).split("|")[0].strip() if title else None,
            "published": pub.group(1) if pub else None,
            "participants": participants,
            "takeaways": [x for x in sections.get("TAKEAWAYS", []) if not FOOL_BOILER.search(x)],
            "risks": [x for x in sections.get("RISKS", []) if not FOOL_BOILER.search(x)],
            "summary": " ".join(sections.get("SUMMARY", [])),
            "turns": turns, "words": sum(len(t["text"].split()) for t in turns),
        }
    return store.cache_through(f"tr:{_key(url)}", FOREVER, load)


def transcript_from_text(body: str, title: str = "pasted transcript") -> dict:
    """A transcript you pasted: 'Name: text' turns if it has them, one block if not."""
    turns, qa = [], False
    rx = re.compile(r"^([A-Z][A-Za-z.'\- ]{1,40}?)(?:\s+--\s+[^:]{0,80})?:\s*(.*)$")
    for l in body.splitlines():
        l = l.strip()
        if not l:
            continue
        m = rx.match(l)
        if m and len(m.group(1).split()) <= 4:
            turns.append({"speaker": m.group(1), "text": m.group(2)})
        elif turns:
            turns[-1]["text"] += "\n" + l
        else:
            turns.append({"speaker": "—", "text": l})
    # Whoever speaks before the questions is management; the operator names
    # each analyst and firm as they come in.
    execs, analysts = set(), {}
    for t in turns:
        if t["speaker"] == "Operator":
            for om in OPERATOR_Q.finditer(t["text"]):
                analysts.setdefault(om.group(1).strip(), om.group(2).strip())
            if re.search(r"question", t["text"], re.I):
                qa = True
        elif not qa:
            execs.add(t["speaker"])
        t["section"] = "qa" if qa else "prepared"
    for t in turns:
        s = t["speaker"]
        t["role"] = ("operator" if s == "Operator" else "analyst" if s in analysts
                     else "exec" if s in execs or t["section"] == "prepared" else "analyst")
        if t["role"] == "analyst" and s in analysts:
            t["firm"] = analysts[s]
    return {"url": None, "source": title, "title": title, "published": None, "participants": [],
            "takeaways": [], "risks": [], "summary": "", "turns": turns,
            "words": sum(len(t["text"].split()) for t in turns)}


def transcript_plain(tr: dict) -> str:
    out = []
    for t in tr.get("turns") or []:
        who = t["speaker"] + (f" ({t.get('firm')})" if t.get("firm") else
                              f" ({t.get('title')})" if t.get("title") else "")
        out.append(f"{who}: {t['text']}")
    return "\n\n".join(out)


# --------------------------------------------------------------------------
# reading the call: tone, topics, the questions asked
# --------------------------------------------------------------------------

# Tone words, after Loughran & McDonald's finance dictionaries — a short list
# written for this page rather than their full one, weighted towards what
# managements actually say on calls.
POS_W = set("""able accelerate accelerated accelerating acceleration achieve achieved achievement advance advantage
attractive benefit benefited beneficial best better boost breakthrough confident confidence strong stronger strongest
strength strengthen encouraged encouraging enhance enhanced excellent exceed exceeded exceeding exceptional excited
exciting expand expanded expanding expansion favorable gain gains good great growth healthy improve improved improvement
improving increase increased leadership leading momentum opportunity opportunities optimistic outperform outperformed
pleased positive profitable progress record records resilient robust solid succeed success successful surpass
tailwind tailwinds upside win wins winning durable disciplined efficiency efficient milestone""".split())
NEG_W = set("""adverse adversely against challenge challenged challenges challenging closure concern concerns decline
declined declines declining decrease decreased decreasing deteriorate deteriorated deteriorating deterioration
difficult difficulties disappoint disappointed disappointing disruption disruptions downturn drag headwind headwinds
impairment impairments lawsuit litigation lose losing loss losses lower miss missed negative negatively pressure
pressured pressures problem problems restructuring risk risks shortfall shortage shortages slow slowdown slowed slower
slowing soft softer softness tough uncertain weak weaken weakened weaker weakness worse worsen worsening volatile
volatility cautious caution delay delayed delays constrained constraint constraints tariff tariffs""".split())
UNC_W = set("""approximately assume assumed assumption assumptions believe could depend depending depends estimate
estimated fluctuate fluctuation fluctuations may might perhaps possible possibly predict preliminary probable probably
roughly seem seems somewhat suggest tentative uncertain uncertainty unclear unknown unpredictable variable visibility
hard-to-predict""".split())
EVASIVE = re.compile(r"(don't|do not|won't|will not) (break (that )?out|disclose|comment|provide (specific|that)|"
                     r"guide (to|on) that|give (specific|that))|too early to (say|tell)|not going to (get into|comment|"
                     r"speculate)|we'll (share|talk about) (more|that) (later|at)|not (ready|in a position) to", re.I)
STOP = set("""a about above after again all also am an and any are as at be because been before being below between
both but by can could did do does doing down during each few for from further had has have having he her here hers
him his how i if in into is it its itself just let like ll me more most my no nor not now of off on once only or other
our ours out over own re s same she should so some such t than that the their them then there these they this those
through to too under until up us ve very was we were what when where which while who whom why will with would you your
usd billion billions million millions percent percentage points basis noted earlier comparable prior constant currency
operator next question line please go ahead
roughly approximately sequentially sequential year-over-year year-on-year compared versus relative respectively
yeah okay think thank thanks know going really kind sort get got lot lots things thing just maybe much well said say
see look one two three first second quarter year years us question questions call good great terms also bit little
great able way right sure want make made across time continue continued within""".split())


def tone(text: str) -> dict:
    words = re.findall(r"[a-z][a-z\-']+", (text or "").lower())
    n = len(words) or 1
    p = sum(1 for w in words if w in POS_W)
    q = sum(1 for w in words if w in NEG_W)
    u = sum(1 for w in words if w in UNC_W)
    return {"words": len(words), "pos": round(p / n * 1000, 1), "neg": round(q / n * 1000, 1),
            "unc": round(u / n * 1000, 1),
            "net": round((p - q) / (p + q), 3) if p + q else None}


def phrases(text: str, top: int = 25) -> list[tuple[str, int]]:
    """Two- and three-word phrases, minus filler: what the call was about."""
    words = [w for w in re.findall(r"[a-z][a-z0-9\-]+", (text or "").lower())]
    c = Counter()
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            g = words[i:i + n]
            if g[0] in STOP or g[-1] in STOP or any(len(x) < 3 for x in g):
                continue
            if sum(1 for x in g if x in STOP) > (n - 2):
                continue
            c[" ".join(g)] += 1
    # A three-word phrase subsumes the two-word ones inside it when it is as common.
    out = []
    for p, k in c.most_common(top * 4):
        if k < 3:
            break
        if any(p in q and kq >= k * 0.8 for q, kq in out):
            continue
        out = [(q, kq) for q, kq in out if not (q in p and k >= kq * 0.8)] + [(p, k)]
        if len(out) >= top:
            break
    return sorted(out, key=lambda x: -x[1])


GUIDE_CALL = re.compile(r"\b(?:we|i) (?:now |still |continue to )?(?:expect|anticipate|forecast|project|see)\b|"
                        r"\bexpect(?:s|ing)? (?:revenue|growth|margins?|capex|capital|opex|operating|eps|earnings|"
                        r"it to|this to|that to|to (?:be|grow|see|deliver|remain|decline|increase))\b|"
                        r"\b(?:our|the) (?:outlook|guidance)\b|\bwe(?:'re| are) guiding\b", re.I)

STRONG = re.compile(r"\brecord\b|accelerat\w*|decelerat\w*|strongest|weakest|headwind|tailwind|inflection|"
                    r"all-time|unprecedented|softness|slowdown|pressure|exceed\w*|ahead of|below our|"
                    r"raising|lowering|capacity constrain\w*|demand", re.I)


def call_analytics(tr: dict, prev: Optional[dict] = None) -> dict:
    turns = tr.get("turns") or []
    prep = " ".join(t["text"] for t in turns if t["section"] == "prepared" and t["role"] == "exec")
    ans = " ".join(t["text"] for t in turns if t["section"] == "qa" and t["role"] == "exec")
    qs = []
    for i, t in enumerate(turns):
        if t["role"] != "analyst" or t["section"] != "qa":
            continue
        answer = []
        for u in turns[i + 1:]:
            if u["role"] == "exec":
                answer.append(u)
            elif answer or u["role"] == "analyst":
                break
        a_text = " ".join(u["text"] for u in answer)
        first = sentences(t["text"])
        qs.append({"analyst": t["speaker"], "firm": t.get("firm"),
                   "question": " ".join(first[:3])[:600] or t["text"][:600],
                   "answered_by": sorted({u["speaker"] for u in answer}),
                   "answer_words": len(a_text.split()),
                   "evasive": bool(EVASIVE.search(a_text)),
                   "topics": [p for p, _ in phrases(t["text"], 3)]})
    guidance = []
    for t in turns:
        if t["role"] != "exec":
            continue
        for s in sentences(t["text"]):
            # On a call the outlook is a run of "we expect ..." sentences, most
            # without a period in them: the forward verb and a figure suffice.
            if (GUIDE_CALL.search(s) or (GUIDE.search(s) and PERIOD.search(s))) and FIGURE.search(s) \
                    and not BOILER.search(s):
                guidance.append({"speaker": t["speaker"], "text": s})
    quotes = []
    for t in turns:
        if t["role"] != "exec":
            continue
        for s in sentences(t["text"]):
            k = len(STRONG.findall(s))
            if k and 60 <= len(s) <= 320:
                quotes.append((k + (1 if FIGURE.search(s) else 0), t["speaker"], s))
    quotes.sort(key=lambda x: -x[0])

    out = {
        "tone": {"prepared": tone(prep), "answers": tone(ans)},
        "topics": phrases(prep + " " + ans + " " + " ".join(q["question"] for q in qs), 20),
        "questions": qs,
        "guidance": guidance[:16],
        "quotes": [{"speaker": s, "text": x} for _, s, x in quotes[:10]],
        "evasive": sum(1 for q in qs if q["evasive"]),
        "words": {"prepared": len(prep.split()), "answers": len(ans.split())},
        "analysts": len({q["analyst"] for q in qs}),
    }
    if prev and prev.get("turns"):
        pa = call_analytics({"turns": prev["turns"]}) if "tone" not in prev else prev
        now_t = {p: k for p, k in out["topics"]}
        was_t = {p: k for p, k in pa["topics"]}
        out["vs_prev"] = {
            "tone_prepared": pa["tone"]["prepared"], "tone_answers": pa["tone"]["answers"],
            "new_topics": [p for p in now_t if p not in was_t][:8],
            "dropped_topics": [p for p in was_t if p not in now_t][:8],
            "questions": len(pa["questions"]), "evasive": pa["evasive"],
        }
    return out


# --------------------------------------------------------------------------
# sources you add yourself
# --------------------------------------------------------------------------

def add_source(sym: str, event_date: str, kind: str, url: Optional[str] = None,
               body: Optional[str] = None, title: Optional[str] = None) -> dict:
    if kind not in ("slides", "transcript", "release", "other"):
        raise ValueError("kind must be slides, transcript, release or other")
    if not url and not (body or "").strip():
        raise ValueError("give a link or paste the text")
    text = (body or "").strip()
    if url and not text:
        r = read_url(url)
        text = (r.get("text") or "").strip()
        if not text:
            raise ValueError("that link answered, but no text could be read from it "
                             "(a scanned PDF, or a page that needs a browser) — paste the text instead")
    store.init_db()
    with store.conn() as c:
        cur = c.execute("INSERT INTO earn_source (symbol,event_date,kind,title,url,body,added) "
                        "VALUES (?,?,?,?,?,?,?)",
                        (sym.upper(), event_date, kind, title or (url or kind), url, text,
                         datetime.now(timezone.utc).isoformat(timespec="seconds")))
        sid = cur.lastrowid
    return {"id": sid, "words": len(text.split())}


def list_sources(sym: str, event_date: str) -> list[dict]:
    store.init_db()
    with store.conn() as c:
        rows = c.execute("SELECT id,symbol,event_date,kind,title,url,body,added FROM earn_source "
                         "WHERE symbol=? AND event_date=? ORDER BY id", (sym.upper(), event_date)).fetchall()
    return [{**dict(r), "words": len((r["body"] or "").split())} for r in rows]


def delete_source(sid: int) -> bool:
    with store.conn() as c:
        return c.execute("DELETE FROM earn_source WHERE id=?", (sid,)).rowcount > 0
