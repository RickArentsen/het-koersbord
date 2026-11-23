"""
The supply chain: who sells to whom, in the companies' own words.

Bloomberg's SPLC draws a company between its suppliers and its customers and
puts a share of revenue on every line. Most of those shares are estimates
from a paid data set. What is free, and exact, is what a company has to say
itself: an annual report names the foundry that makes its chips, the customer
that is a tenth of its sales, the rivals it loses orders to and the partner
its biggest contract runs with. This module reads that and nothing else.

How a link gets here

  1. passages()  finds, in a company's latest annual report (10-K or 20-F),
     the sentences that name another organisation next to a word about
     selling, buying, making, competing or owning. Rules only: nothing is
     understood yet, and most of what it finds is not a relationship.
  2. A reader says of each passage which company it names and what that
     company is to this one. The reader was Claude for the map that ships
     with the terminal (splc_seed.json, read once for the book as it stood),
     and it is Claude Code on your own plan for any company you ask for
     later. The reader answers with passage numbers, never with text: the
     quote under a link is cut from the filing by this module, so a link can
     be wrong about what a sentence means but cannot quote a sentence the
     filing does not contain.
  3. links()     the shipped map plus what you have had read since.

A link runs from `a` to `b`. For "supply", a sells to b: goods and services
flow a -> b and the money the other way. A share, where the filing gives one,
is always a share of something named in `basis`: of a's revenue (the common
one: "Customer A was 23% of revenue"), of a's receivables, or of b's own
shares for a stake. A share of the buyer's costs is almost never disclosed and
is never estimated here.

What this cannot see: a company that files nothing with the SEC (BMW, NN
Group, Wereldhave), a customer the filing leaves unnamed ("Customer A"), and
every relationship too small or too sensitive for an annual report. Apple
does not name TSMC. The map is a floor, not a census.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime, timezone
from typing import Optional

from . import earndocs as ed
from . import sec, store

ANNUAL = ("10-K", "20-F", "40-F", "10-KT")
MAX_PASSAGES = 110          # per annual report: a reader's hour, or a minute of Claude's
MAX_RELEASE = 30            # per results release, which is short and mostly figures
PER_NAME = 8                # sentences kept about any one company
RELEASES = 4                # results releases read per company: a year of them
MAX_CHARS = 1400            # one passage; a competitor list can run long
DAY = 86400


# --------------------------------------------------------------------------
# the document
# --------------------------------------------------------------------------

def annual(sym: str, name: Optional[str] = None) -> Optional[dict]:
    """The latest annual report of the SEC filer behind a symbol, or None."""
    f = ed.filer(sym, name)
    if not f:
        return None
    sub = sec.submissions(f["cik"])
    for x in sub.get("filings") or []:
        if x["form"] in ANNUAL and x.get("doc"):
            return {"sym": sym, "filer": f["ticker"], "cik": f["cik"], "title": f.get("title") or sub.get("name"),
                    "form": x["form"], "filed": x["date"], "period": x.get("report") or None, "acc": x["acc"],
                    "url": sec.filing_url(f["cik"], x["acc"], x["doc"])}
    return None


def releases(sym: str, name: Optional[str] = None) -> list[dict]:
    """
    The press releases of the last four results, newest first, and the
    commentary filed with the latest. An annual report says who the suppliers
    are and leaves the customers out ("one direct customer was 22% of
    revenue"); the release names them, because a customer taking the new
    product is good news. Nvidia's annual report does not say Microsoft. Its
    results releases do. A year of them, because each names a different few.
    """
    f = ed.filer(sym, name)
    if not f or f.get("foreign"):
        return []                       # a 6-K carries no item numbers to find the results by
    sub = sec.submissions(f["cik"])
    xs = [x for x in sub.get("filings") or [] if x["form"] == "8-K" and "2.02" in (x.get("items") or "")]
    out = []
    for i, x in enumerate(xs[:RELEASES]):
        try:
            idx = ed.filing_index(f["cik"], x["acc"])
        except Exception:
            continue
        n = 0
        for d in idx:
            t = (d.get("type") or "").upper()
            if t.startswith("EX-99") and not d["doc"].lower().endswith((".pdf", ".jpg", ".png", ".gif")):
                out.append({"sym": sym, "filer": f["ticker"], "cik": f["cik"],
                            "title": f.get("title") or sub.get("name"), "form": "8-K " + t, "filed": x["date"],
                            "period": x.get("report") or None, "acc": x["acc"], "url": d["url"], "release": True})
                n += 1
                if n >= (2 if i == 0 else 1):
                    break
    return out


def documents(sym: str, name: Optional[str] = None) -> list[dict]:
    """What is read for one company: its annual report, then its latest results release."""
    a = annual(sym, name)
    out = [a] if a else []
    try:
        out += releases(sym, name)
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# sentences that might be a relationship
# --------------------------------------------------------------------------

_ABBR = re.compile(r"(?:\b(?:Inc|Corp|Co|Ltd|Cos|No|Nos|U\.S|U\.K|S\.A|N\.V|L\.P|Mr|Ms|Mrs|Dr|Jr|Sr|St|vs|approx|"
                   r"e\.g|i\.e|al|Fig|Bros)\.)$")
_SPLIT = re.compile(r"(?<=[.!?])[\"”’)]*\s+(?=[\"“(]?[A-Z0-9])")


def sentences(par: str) -> list[str]:
    out: list[str] = []
    for p in _SPLIT.split(par):
        if out and _ABBR.search(out[-1]):
            out[-1] += " " + p
        else:
            out.append(p)
    return out


# What a sentence has to say before it is worth reading. The word "customer"
# alone is in every other sentence of an annual report; these are the turns
# of phrase that put a name next to it.
CUES = [
    ("share", re.compile(r"\b(?:accounted|accounting|represented|representing|comprised|constituted)\s+for\b[^.]{0,90}?%|"
                         r"\b(?:accounted for|represented|comprised)\s+(?:approximately\s+|more than\s+|over\s+)?\d+(?:\.\d+)?%|"
                         r"\d+(?:\.\d+)?%\s+(?:and\s+\d+(?:\.\d+)?%\s+)?of\s+(?:our|the company's|total|net|consolidated)?\s*"
                         r"(?:total\s+|net\s+|consolidated\s+)?(?:revenues?|sales|net sales|accounts receivable|receivables|"
                         r"purchases|cost of)", re.I)),
    ("customer", re.compile(r"\bcustomers?\b[^.]{0,60}\b(?:includ|such as|like|are|were|is|was)\b|\bsales to\b|\bsold to\b|"
                            r"\b(?:largest|significant|major|key|principal|primary|anchor)\s+(?:end[- ])?customers?\b|"
                            r"\bend[- ]customers?\b|\brevenues? from\b|\b(?:agreement|contract|order|award|commitment)s?\s+"
                            r"(?:with|from)\b|\bofftake\b|\btake[- ]or[- ]pay\b|\bcustomers?,?\s+(?:including|such as)\b", re.I)),
    ("supplier", re.compile(r"\bsuppl(?:y|ies|ied|ier|iers)\b|\bvendors?\b|\bmanufactur(?:ed|es|ing|e|er|ers)\b|\bfoundr(?:y|ies)\b|"
                            r"\b(?:sole|single|limited)[- ]sourced?\b|\b(?:rely|relies|relied|reliance|depend|depends|dependent|"
                            r"dependence)\s+(?:up)?on\b|\bpurchas(?:e|es|ed|ing)\b[^.]{0,80}\bfrom\b|\bprocur|\bsourced?\s+from\b|"
                            r"\bprovided by\b|\bhosted\b|\bassembl|\boutsourc|\bsubcontract|\blicens(?:e|es|ed|ing|or|ors)\b|"
                            r"\bcloud (?:service|infrastructure|computing)\s+providers?\b|\bdata cent(?:er|re)s?\b", re.I)),
    ("competitor", re.compile(r"\bcompet(?:e|es|ed|ing|itor|itors|ition|itive)\b|\brivals?\b", re.I)),
    ("partner", re.compile(r"\bpartner(?:ship|ships|ed|ing|s)?\b|\bjoint venture\b|\bcollaborat|\balliance\b|"
                           r"\binvest(?:ed|ment|ments)\s+in\b|\bequity (?:interest|stake|investment|method)\b|"
                           r"\b(?:owns?|owned|holds?|held)\b[^.]{0,60}%|\bstake\b|"
                           r"\bshareholders?\b[^.]{0,80}%|\bcontrolling\b", re.I)),
]

# Sentences that name companies for reasons that are not the business.
_NOISE = re.compile(r"\bExhibit\b|\bIndenture\b|\bincorporated (?:herein )?by reference\b|\bTrustee\b|\bfiled herewith\b|"
                    r"\bForm (?:8-K|10-Q|S-\d)\b|\bunderwriter|\bplaintiff|\bdefendant|\bcourt\b|\blawsuit|\bcomplaint\b|"
                    r"\bserved as\b|\bserves (?:on|as)\b|\bBoard of Directors\b|\b(?:Mr|Ms|Mrs|Dr)\.|\bage \d\d\b|"
                    r"\bpeer group\b|\bindex\b[^.]{0,40}\b(?:S&P|Nasdaq|Dow)\b|\bstock performance\b|\bRegistered trademarks?\b|"
                    r"\bare (?:registered )?trademarks\b|\bindependent registered public accounting\b|\bCredit Agreement\b|"
                    r"\bNotes due\b|\bSenior Notes\b|\btransfer agent\b|\bas (?:administrative|collateral) agent\b|"
                    r"\bequity distribution agreement\b|\bsales agents?\b|\blenders?\b|\bunderwritten\b|\bbook-?running\b|"
                    r"\bExecutive Order\b|\bFederal Register\b|\bpassive foreign investment\b|"
                    r"^(?:He|She)\s|\bPrior to (?:joining|attending|that)\b|\bVice President\b|\bGeneral Manager\b|"
                    r"\bChief \w+ Officer\b|\bInvestment Company Act\b|\bregistered investment advis", re.I)

# The endings that make a capitalised phrase a company for certain. "Networks"
# and "Systems" are not among them: "Scale In Networks" is not a company.
_SUFFIXED = (r"(?:Inc|Incorporated|Corp|Corporation|Co|Company|Ltd|Limited|LLC|L\.L\.C|L\.P|LP|plc|PLC|AG|SE|N\.V|"
             r"S\.A|S\.p\.A|GmbH|B\.V|A/S|AB|Oyj|KK|K\.K|Pte)")
_CAP = r"(?:[A-Z]\.(?:[A-Za-z]\.)+|[A-Z][A-Za-z0-9&'’\-]*[A-Za-z0-9]|[A-Z])"
_NAME = re.compile(rf"(?<![A-Za-z0-9]){_CAP}(?:\.?,?\s+(?:of|de|and|&)\s+{_CAP}|\.?,?\s+{_CAP}){{0,5}}\.?")

# Capitalised and not a company. Lower-cased; a name made only of these is
# dropped, and they are stripped from the front of a longer one.
STOP = set("""
we our us the a an in on at as if it its this that these those there their they he she his her for from to of by
with and or but not no nor so such any all each some many most more other others another both either neither
when while where which who whom whose what why how also although because since until unless upon under over
after before during between among against within without through per via than then thus therefore however
moreover furthermore additionally accordingly certain various several
company companies corporation group board management directors director officer officers committee
annual quarterly report reports form item items part note notes table contents page section sections
fiscal year years quarter quarters month months january february march april may june july august september
october november december monday tuesday wednesday thursday friday saturday sunday
united states america american u.s us usa north south east west asia pacific europe european emea apac
china chinese prc taiwan japan japanese korea korean india indian germany german france french israel
netherlands dutch kingdom uk britain british ireland irish singapore malaysia vietnam thailand mexico canada
canadian brazil argentina russia ukraine hong kong middle africa latin international global worldwide
gaap non-gaap sec irs fasb asu asc ifrs nasdaq nyse exchange act securities commission congress government
federal state department treasury court district internal revenue code tax taxes law laws regulation
regulations rule rules common stock class shares share preferred notes senior credit agreement agreements
facility loan term revolving ai ml api cpu gpu soc asic ip it iot pc pcs hpc oem oems odm odms cm cms esg
covid-19 covid pandemic risk factors business overview competition customers suppliers employees
properties legal proceedings md&a results operations liquidity capital resources critical accounting
estimates financial statements consolidated balance sheets income cash flows equity total net revenue
revenues sales cost costs gross margin operating expenses research development general administrative
marketing products product services service segment segments data center centers cloud gaming automotive
networking enterprise client mobile embedded foundry intelligent edge compute infrastructure platform
platforms software hardware systems solutions technology technologies new see including include includes
further forward-looking statements act reform litigation private chief executive financial president vice
ceo cfo cto coo senior executive index total none not applicable yes no additional information available
there here one two three four five six seven eight nine ten first second third fourth
""".split())

_LIST = re.compile(r"\b(?:customers?|competitors?|suppliers?|vendors?|partners?|foundries|manufacturers?|providers?|"
                   r"distributors?|licensees?|resellers?|retailers?|carriers?|operators?|OEMs|ODMs|hyperscalers?)\b"
                   r"[^.;]{0,50}?(?:\binclud(?:e|es|ed|ing)\b|\bsuch as\b|\blike\b|:)|\bcompet(?:e|es|ed|ing)\s+"
                   r"(?:with|against)\b|\bcompetition\s+from\b", re.I)
_STRONG = re.compile(r"\b(?:sole|single)[- ]sourced?\b|\bfoundr|\bcustomers?,?\s+(?:includ|such as)|"
                     r"\bcompetitors?\b[^.]{0,40}\binclud|\bcompete\s+(?:with|against)\b", re.I)
_PARTY = re.compile(r"customer|supplier|vendor|distributor|licensee|partner|purchaser|reseller", re.I)
# "Premium revenues from CMS represented 44%": a share of sales and a name, without the word customer.
_FROM_WHOM = re.compile(r"\b(?:revenues?|sales)\s+(?:from|to)\s+(?:the\s+)?[A-Z][A-Za-z&.\-]+")
_TABLE_OF = re.compile(r"licens|customer|supplier|vendor|partner|distributor|manufactur|foundr", re.I)
_ROW = re.compile(r"^(?:Direct\s+|Indirect\s+|End\s+)?(?:Customer|Supplier|Vendor|Distributor|Licensee|Partner)\s+"
                  r"(?:[A-Z]|\d{1,2})\b\s*\|", re.I)
_NUMROW = re.compile(r"^[A-Z][\w .,&'’()\-]{1,48}\|[\s\d.,*%|—–\-<()]+$")
_FOOT = re.compile(r"^(?:\d{1,3}|[ivxlc]{1,6}|Table of Contents|Index to Financial Statements|F-\d{1,3}|"
                   r"[A-Z][\w .,&'’\-]{0,60}\|\s*\d{4} Form (?:10-K|20-F)\s*\|\s*\d{1,3})$", re.I)
_OPEN_END = re.compile(r"[.!?:;][\"”’)\]]*$")
_DEFINED = re.compile(rf"((?:{_CAP}\.?,?\s+(?:(?:of|de|and|&)\s+)?){{0,6}}{_SUFFIXED}\.?,?(?:\s+{_SUFFIXED}\.?)?)"
                      r"[,]?\s*(?:\((?:the\s+|or\s+|collectively,?\s+|together with[^)]{0,60},\s+)?[“\"]?|,\s+or\s+[“\"]?)"
                      rf"({_CAP}(?:\s{_CAP})?)[”\"]?[),]")


def clean_lines(text: str) -> list[str]:
    """
    The paragraphs of a filing, without the furniture a page break leaves
    in the middle of a sentence: a page number, "Table of Contents", and the
    sentence carrying on below them.

    Some reports (Unilever's, set from the printed book) break every line
    where the page did. There a line of forty characters without a full stop
    is the middle of a sentence, not a heading, and is joined to the next.
    """
    raw_lines = [l.strip() for l in (text or "").split("\n")]
    body = [l for l in raw_lines if len(l) >= 40 and "|" not in l]
    wrapped = len(body) > 200 and sum(1 for l in body if not _OPEN_END.search(l)) / len(body) > 0.45
    need = 40 if wrapped else 80
    out: list[str] = []
    for s in raw_lines:
        if not s or _FOOT.match(s):
            continue
        if (out and "|" not in s and "|" not in out[-1] and len(out[-1]) >= need and not _OPEN_END.search(out[-1])
                and not s.startswith("•")):
            out[-1] += " " + s
        else:
            out.append(s)
    return out


def defined_names(text: str, own: set[str]) -> dict:
    """
    {short name: full name} for the companies a filing introduces the way
    lawyers do: "Taiwan Semiconductor Manufacturing Company Limited (TSMC)".
    From there on the filing says TSMC, and so must this.
    """
    out = {}
    for m in _DEFINED.finditer(text or ""):
        full = re.sub(r"\s+", " ", m.group(1)).strip(" ,")
        short = m.group(2).strip()
        words = full.split(" ")
        while words and words[0].lower().strip(".,'’") in STOP:
            words.pop(0)
        full = " ".join(words)
        low = short.lower()
        if not full or low in STOP or len(short) < 2 or ed.norm_name(full) in own or low in own:
            continue
        if low in ("company", "registrant", "parent", "issuer", "holder", "plan", "notes", "agreement", "merger"):
            continue
        out.setdefault(short, full)
    return out


def org_names(sent: str, own: set[str], loose: bool = False, defined: Optional[dict] = None) -> list[str]:
    """
    The organisations a sentence names. Strict by default: a name with a
    company ending (Amkor Technology Inc.) or one the filing has defined
    (TSMC). `loose` takes every capitalised phrase that is not a stop word,
    and is used only where the sentence itself says a list of companies is
    coming ("our competitors include"), because there a bare "Marvell" is a
    company and elsewhere a bare "Bluebird" is a product.
    """
    out = []
    for m in _NAME.finditer(sent):
        words = re.split(r"[\s,]+", m.group(0).strip(" .,"))
        if m.start() == 0 and len(words) == 1 and not (defined and words[0] in defined):
            continue                            # the capital that starts the sentence
        while words and words[0].lower().strip(".,'’") in STOP:
            words.pop(0)
        while words and words[-1].lower().strip(".,'’") in STOP and not re.fullmatch(_SUFFIXED + r"\.?", words[-1]):
            words.pop()
        if not words:
            continue
        nm = " ".join(words).strip(" .,")
        low = nm.lower()
        if len(nm) < 2 or low in STOP or all(w.lower().strip(".,'’") in STOP for w in words):
            continue
        if any(low == o or low.startswith(o + " ") for o in own):
            continue
        strong = bool(re.search(rf"\s{_SUFFIXED}\.?$", nm)) or bool(defined and (nm in defined or words[0] in defined))
        if (strong or loose) and nm not in out:
            out.append(nm)
    return out


# First words that are a company's name only together with the next one:
# Advanced Micro Devices is not every "Advanced ...", and dropping those as
# the filer's own name would drop Advanced Semiconductor Engineering with it.
_GENERIC = {"international", "american", "general", "united", "advanced", "applied", "national", "western",
            "first", "global", "super", "micro", "royal", "energy", "digital", "bank", "new", "pacific", "taiwan"}


def _own_names(doc: dict, extra: Optional[list[str]] = None) -> set[str]:
    """The filer's own names, lower-cased: what must not come back as somebody else."""
    own = set()
    for n in [doc.get("title") or ""] + list(extra or []):
        k = ed.norm_name(n)
        if k:
            own.add(k)
            first = k.split(" ")[0]
            if first not in _GENERIC and first not in STOP:
                own.add(first)
    return {o for o in own if len(o) >= 2}


def name_rx(names: list[str]) -> Optional[re.Pattern]:
    """
    One pattern for a list of company names, as written or in capitals
    (a filing says NVIDIA where a headline says Nvidia). Case matters
    otherwise: the company is Micron and the unit of length is a micron.
    """
    parts = []
    for n in sorted({x for x in names if x and len(x) >= 2}, key=len, reverse=True):
        if n.lower() in STOP:
            continue
        parts.append("|".join(re.escape(a) for a in sorted({n, n.upper()})))
    return re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(parts) + r")(?![A-Za-z0-9])") if parts else None


def passages_of(text: str, own: set[str], known: Optional[re.Pattern] = None, cap: int = MAX_PASSAGES) -> list[dict]:
    """
    The candidate sentences of one document, in reading order:
    [{n, text, cues, names, score}]. `known` is a pattern of names worth a
    look whatever the sentence says: your holdings, and every company the
    map already has.
    """
    lines = clean_lines(text)
    defined = defined_names(text, own)
    seen, found = set(), []
    order = 0
    i = 0
    while i < len(lines):
        par = lines[i]
        i += 1
        # A table of who was what share of revenue: "Customer A | 49 | 67".
        # The rows mean nothing apart, so the table is one passage, with the
        # heading row above it.
        if _ROW.match(par):
            rows = [par]
            while i < len(lines) and (_ROW.match(lines[i]) or _NUMROW.match(lines[i])):
                rows.append(lines[i])
                i += 1
            head = [l for l in lines[max(0, i - len(rows) - 3):i - len(rows)] if "|" in l or len(l) < 90]
            order += 1
            found.append({"order": order, "text": " / ".join(head[-2:] + rows)[:MAX_CHARS], "cues": ["share"],
                          "names": [], "score": 6.0, "table": True})
            continue
        if "|" in par:
            if known is not None and _NUMROW.match(par) and known.match(par) and "%" in " ".join(lines[max(0, i - 6):i + 3]):
                order += 1
                head = [l for l in lines[max(0, i - 4):i - 1] if "|" in l or len(l) < 90]
                found.append({"order": order, "text": " / ".join(head[-2:] + [par])[:MAX_CHARS], "cues": ["share"],
                              "names": [], "score": 6.0, "table": True})
                continue
            # A table that names companies: "Fragrances | L'Oreal S.A.". The
            # row is kept when the paragraph above the table says what the
            # table is of (licensing partners, suppliers, customers).
            if _NOISE.search(par) or len(par) > 400:
                continue
            row_names = org_names(par.replace("|", ","), own, defined=defined)
            if row_names or (known is not None and known.search(par)):
                j = i - 2
                while j >= 0 and "|" in lines[j]:
                    j -= 1
                above = lines[j] if j >= 0 and i - 2 - j <= 40 else ""
                if above and _TABLE_OF.search(above) and not _NOISE.search(above):
                    order += 1
                    who = {n.lower() for n in row_names}
                    found.append({"order": order, "text": (above[-260:] + " / " + par)[:MAX_CHARS], "cues": ["partner"],
                                  "names": row_names[:14], "score": 3.0 + len(row_names), "table": True, "who": who})
            continue
        if len(par) < 40:
            continue
        for s in sentences(par):
            s = re.sub(r"\s+", " ", s).strip()
            if len(s) < 40:
                continue
            order += 1
            if _NOISE.search(s):
                continue
            cues = [k for k, rx in CUES if rx.search(s)]
            hit = known.search(s) if known is not None else None
            if not cues and not hit:
                continue
            names = org_names(s, own, loose=bool(_LIST.search(s)), defined=defined)
            share = "share" in cues and (_PARTY.search(s) or _FROM_WHOM.search(s))
            if not names and not share and not hit:
                continue
            key = re.sub(r"[^a-z0-9]", "", s.lower())[:110]
            if key in seen:
                continue
            seen.add(key)
            score = (4 if hit else 0) + (3 if share else 0) + (2 if "share" in cues and names else 0)
            score += sum(1 for n in names if re.search(rf"\s{_SUFFIXED}\.?$", n))
            score += 2 if _STRONG.search(s) else 0
            score += min(len(names), 6) * 0.5
            if len(s) > MAX_CHARS:
                s = s[:MAX_CHARS].rsplit(" ", 1)[0] + " …"
            who = {n.lower() for n in names} | ({m.lower() for m in known.findall(s)} if known is not None else set())
            found.append({"order": order, "text": s, "cues": cues, "names": names[:14], "score": round(score, 2),
                          "who": who})
    # One company can fill the whole reading: SoftBank is in two hundred
    # sentences of Arm's report, and they pushed the customers out. The best
    # few sentences about each name are kept and the rest of the report gets
    # read too.
    seen_n: dict = {}
    keep = []
    for p in sorted(found, key=lambda p: (-p["score"], p["order"])):
        who = p.pop("who", set())
        if who and all(seen_n.get(w, 0) >= PER_NAME for w in who):
            continue
        for w in who:
            seen_n[w] = seen_n.get(w, 0) + 1
        keep.append(p)
    found = sorted(keep[:cap], key=lambda p: p["order"])
    for i, p in enumerate(found, 1):
        p["n"] = i
        del p["order"]
    return found


def passages(sym: str, name: Optional[str] = None, known: Optional[re.Pattern] = None,
             extra_own: Optional[list[str]] = None) -> dict:
    """
    {"sym", "docs": [...], "items": [{n, d, text, cues, names, score}]}: the
    candidate sentences of a company's documents, numbered straight through;
    `d` says which document a sentence is from.
    """
    docs = documents(sym, name)
    items, n = [], 0
    for di, doc in enumerate(docs):
        try:
            text = ed.read_url(doc["url"]).get("text") or ""
        except Exception as e:
            doc["error"] = f"{e.__class__.__name__}: {e}"[:160]
            continue
        doc["chars"] = len(text)
        own = _own_names(doc, extra_own)
        ps = passages_of(text, own, known, cap=MAX_RELEASE if doc.get("release") else MAX_PASSAGES)
        if doc.get("release"):
            # A release is figures, a quote from the chief executive and a
            # paragraph of legal caution. Only the lines that name somebody.
            ps = [x for x in ps if x["names"] or (known is not None and known.search(x["text"]))]
            ps = [x for x in ps if not re.search(r"forward-looking statements", x["text"], re.I)]
        for x in ps:
            n += 1
            x["n"], x["d"] = n, di
            items.append(x)
    return {"sym": sym, "docs": docs, "items": items}


# --------------------------------------------------------------------------
# the map: the reading that ships, and what you have had read since
# --------------------------------------------------------------------------

SEED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "splc_seed.json")
KINDS = ("supply", "compete", "partner", "stake")
HUB = 3                     # an outside company is drawn on your map when it ties this many holdings
_SEED: dict = {"v": None}
_BOOK: dict = {"t": 0.0, "v": None}


def seed() -> dict:
    """The map as it shipped: read once, from the filings of the book as it then stood."""
    if _SEED["v"] is None:
        try:
            with open(SEED_PATH, encoding="utf-8") as f:
                _SEED["v"] = json.load(f)
        except Exception:
            _SEED["v"] = {"names": {}, "read": {}, "docs": {}, "links": [], "notes": {}}
    return _SEED["v"]


def book() -> dict:
    """
    {key: {key, sym, name, label, weight, value_eur, sector, brands}} for the
    shares you hold. The key is the symbol the company files under in the US
    where it has one (UL for the Amsterdam line of Unilever), because that
    is the name a link read from a filing carries.
    """
    if _BOOK["v"] is not None and time.time() - _BOOK["t"] < 120:
        return _BOOK["v"]
    import hashlib
    from . import earnings
    from .morning import brands
    held = earnings.held()

    def load():
        m = {}
        for s, h in held.items():
            try:
                f = ed.filer(s, h.get("name"))
            except Exception:
                f = None
            m[s] = f["ticker"] if f else s
        return {"map": m}
    sig = hashlib.md5(",".join(sorted(held)).encode()).hexdigest()[:10]
    try:
        cm = store.cache_through(f"splc:canon:{sig}", 7 * DAY, load).get("map") or {}
    except Exception:
        cm = {}
    out = {}
    for s, h in held.items():
        key = cm.get(s, s)
        b = brands(s, h.get("name") or "")
        out[key] = {"key": key, "sym": s, "name": h.get("name"), "label": b[0] if b else s,
                    "weight": h.get("weight") or 0.0, "value_eur": h.get("value_eur"),
                    "sector": h.get("sector"), "brands": b}
    _BOOK.update(t=time.time(), v=out)
    return out


_READY = {"v": False}


def _init():
    if not _READY["v"]:
        store.init_db()
        _READY["v"] = True


def _db(sql: str, args: tuple = ()) -> list[dict]:
    _init()
    with store.conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def names() -> dict:
    """{key: [label, other names ...]}: the shipped names, those read since, and your holdings under their own."""
    out = {k: list(v) for k, v in (seed().get("names") or {}).items()}
    for r in _db("SELECT key, label, aliases FROM splc_name"):
        try:
            al = json.loads(r["aliases"] or "[]")
        except Exception:
            al = []
        cur = out.setdefault(r["key"], [])
        for x in [r["label"]] + al:
            if x and x not in cur:
                cur.append(x)
    for key, h in book().items():
        out[key] = list(dict.fromkeys(list(h["brands"]) + out.get(key, [])))
    return out


def label(key: str, N: Optional[dict] = None) -> str:
    v = (N if N is not None else names()).get(key)
    return v[0] if v else key.lstrip("~").replace("-", " ")


def short(key: str, N: Optional[dict] = None, room: int = 13) -> str:
    """A name that fits a box on the map: "Samsung" for Samsung Electronics, "Applied Opto"."""
    al = (N if N is not None else names()).get(key) or [label(key, N)]
    if len(al[0]) <= room:
        return al[0]
    fit = [a for a in al if 3 <= len(a) <= room]
    return min(fit, key=len) if fit else al[0]


def key_of(sym: str) -> str:
    """The map's key for a symbol as you would type it: INGA.AS and ING are the same company."""
    s = (sym or "").strip()
    if s.startswith("~"):
        return s.lower()
    s = s.upper()
    B = book()
    for k, h in B.items():
        if h["sym"].upper() == s:
            return k
    if s in B:
        return s
    N = seed().get("names") or {}
    if s not in N and "." in s and s.split(".")[0] in N:
        return s.split(".")[0]
    return s


def node(key: str, B: Optional[dict] = None, N: Optional[dict] = None) -> dict:
    B = B if B is not None else book()
    h = B.get(key)
    return {"key": key, "label": label(key, N), "short": short(key, N),
            "sym": h["sym"] if h else (None if key.startswith("~") else key),
            "held": bool(h), "weight": h["weight"] if h else None, "listed": not key.startswith("~")}


def known(exclude: Optional[str] = None, N: Optional[dict] = None) -> Optional[re.Pattern]:
    """The pattern of every company name the map knows, less one company's own."""
    N = N if N is not None else names()
    al = [a for k, v in N.items() if k != exclude for a in v]
    return name_rx(al)


def _reread() -> dict:
    """{key: filed date} for the companies read again since the map shipped."""
    return {r["key"]: r["filed"] for r in _db("SELECT key, filed FROM splc_read")}


def links() -> list[dict]:
    """
    Every link with the document it rests on:
    {a, b, kind, what, pct, basis, by, form, filed, url, quote, source, id}.
    A company you have had read again speaks through the new reading only:
    its shipped sentences came from last year's report.
    """
    s = seed()
    docs = s.get("docs") or {}
    again = _reread()
    out = []
    for l in s.get("links") or []:
        d = docs.get(l.get("doc")) or {}
        if l.get("by") in again and d.get("form") != "Supplier list":
            continue
        out.append({"a": l["a"], "b": l["b"], "kind": l["kind"], "what": l.get("what"), "pct": l.get("pct"),
                    "basis": l.get("basis"), "by": l.get("by"), "quote": l.get("quote"), "form": d.get("form"),
                    "filed": d.get("filed"), "url": d.get("url"), "title": d.get("title"), "source": "seed", "id": None})
    for r in _db("SELECT id, a, b, kind, what, pct, basis, by, form, filed, url, quote, source FROM splc_link"):
        r["title"] = None
        out.append(r)
    return out


def edges(ls: Optional[list] = None) -> list[dict]:
    """One edge per pair and kind, with every sentence behind it, newest first."""
    out: dict = {}
    for l in (ls if ls is not None else links()):
        a, b = l["a"], l["b"]
        if l["kind"] in ("compete", "partner") and a > b:
            a, b = b, a
        e = out.setdefault((a, b, l["kind"]), {"a": a, "b": b, "kind": l["kind"], "pct": None, "basis": None, "ev": []})
        e["ev"].append(l)
    for e in out.values():
        e["ev"].sort(key=lambda l: ((l.get("pct") is not None), l.get("filed") or ""), reverse=True)
        top = e["ev"][0]
        if top.get("pct") is not None:
            e["pct"], e["basis"] = top["pct"], top["basis"]
        e["what"] = top.get("what")
    return list(out.values())


def notes_of(key: str) -> list[dict]:
    """What a company's filings say without naming anyone: "one customer was 22% of revenue"."""
    s = seed()
    docs = s.get("docs") or {}
    out = []
    if key not in _reread():
        for n in (s.get("notes") or {}).get(key) or []:
            d = docs.get(n.get("doc")) or {}
            out.append({"text": n["text"], "quote": n.get("quote"), "form": d.get("form"), "filed": d.get("filed"),
                        "url": d.get("url"), "source": "seed"})
    else:
        for n in (s.get("notes") or {}).get(key) or []:
            d = docs.get(n.get("doc")) or {}
            if d.get("form") == "Supplier list":
                out.append({"text": n["text"], "quote": n.get("quote"), "form": d.get("form"), "filed": d.get("filed"),
                            "url": d.get("url"), "source": "seed"})
    out += _db("SELECT text, quote, form, filed, url, source FROM splc_note WHERE key=? ORDER BY id", (key,))
    return out


def _ev(l: dict, N: dict) -> dict:
    return {"by": l.get("by"), "by_label": label(l.get("by") or "", N), "form": l.get("form"), "filed": l.get("filed"),
            "url": l.get("url"), "quote": l.get("quote"), "what": l.get("what"), "pct": l.get("pct"),
            "basis": l.get("basis"), "source": l.get("source"), "id": l.get("id"), "title": l.get("title")}


BASIS_WORDS = {"a_revenue": "of {a} revenue", "a_receivables": "of {a} receivables",
               "b_purchases": "of {b} purchases", "b_cost": "of {b} cost of sales", "stake": "of {b} shares"}


def _s(name: str) -> str:
    """Whose: Nvidia's, Applied Optoelectronics'."""
    return name + ("'" if name.endswith("s") else "'s")


def _the(name: str) -> str:
    return "the " + name if name.startswith(("U.S.", "US ")) else name


def _share(e: dict, N: dict) -> Optional[str]:
    """"67% of CoreWeave's revenue": a disclosed share always says what it is a share of."""
    t = BASIS_WORDS.get(e.get("basis") or "")
    if e.get("pct") is None or not t:
        return None
    p = e["pct"]
    num = f"{p:.0f}%" if abs(p - round(p)) < 0.05 else f"{p:.1f}%"
    return num + " " + t.format(a=_s(label(e["a"], N)), b=_s(label(e["b"], N)))


def read_state(key: str) -> dict:
    """
    Which report the links of a company were read from, and whether a newer
    one has been filed since.
    """
    B = book()
    h = B.get(key)
    sym = h["sym"] if h else (None if key.startswith("~") else key)
    out = {"key": key, "filer": False, "read": None, "latest": None, "newer": False, "docs": [], "by": None}
    rr = _db("SELECT * FROM splc_read WHERE key=?", (key,))
    sr = (seed().get("read") or {}).get(key)
    if rr:
        r = rr[0]
        out["read"] = {"form": r["form"], "filed": r["filed"], "acc": r["acc"], "when": r["read_at"]}
        out["by"] = r["model"] or "Claude"
        out["source"] = "claude"
    elif sr:
        out["read"] = {"form": sr.get("form"), "filed": sr.get("filed"), "acc": sr.get("acc"),
                       "when": seed().get("read_on")}
        out["by"] = "Claude, when the map was built"
        out["source"] = "seed"
    if not sym:
        return out
    try:
        a = annual(sym, h.get("name") if h else None)
    except Exception:
        a = None
    if a:
        out["filer"] = True
        out["latest"] = {"form": a["form"], "filed": a["filed"], "acc": a["acc"], "url": a["url"], "period": a["period"]}
        if out["read"] and (out["read"].get("filed") or "") < a["filed"]:
            out["newer"] = True
    return out


def _rank(r: dict) -> tuple:
    return (-(r["pct"] or 0) if r.get("basis") in ("a_revenue", "b_purchases", "stake") else 0,
            0 if r["held"] else 1, -len(r["ev"]), -(r["weight"] or 0), r["label"].lower())


def company(sym: str) -> dict:
    """
    One company between its suppliers and its customers, with its rivals,
    partners and owners, and the sentence behind every line.
    """
    key = key_of(sym)
    B, N = book(), names()
    rel = {"suppliers": [], "customers": [], "competitors": [], "partners": [], "owners": [], "stakes": []}
    for e in edges():
        if key not in (e["a"], e["b"]):
            continue
        other = e["b"] if e["a"] == key else e["a"]
        row = {**node(other, B, N), "what": e["what"], "pct": e["pct"], "basis": e["basis"], "share": _share(e, N),
               "ev": [_ev(l, N) for l in e["ev"]],
               "said_by": sorted({l["by"] for l in e["ev"] if l.get("by")})}
        if e["kind"] == "supply":
            rel["customers" if e["a"] == key else "suppliers"].append(row)
        elif e["kind"] == "compete":
            rel["competitors"].append(row)
        elif e["kind"] == "partner":
            rel["partners"].append(row)
        else:
            rel["stakes" if e["a"] == key else "owners"].append(row)
    for k in rel:
        rel[k].sort(key=_rank)
    me = node(key, B, N)
    h = B.get(key)
    st = read_state(key)
    mine: dict = {}
    for l in links():
        if l.get("by") == key and l.get("url"):
            d = mine.setdefault(l["url"], {"form": l.get("form"), "filed": l.get("filed"), "url": l["url"],
                                           "title": l.get("title"), "n": 0})
            d["n"] += 1
    out = {"ok": True, **me, "name": (h or {}).get("name"), "sector": (h or {}).get("sector"),
           "relations": rel, "counts": {k: len(v) for k, v in rel.items()},
           "notes": notes_of(key), "state": st, "job": _pub(JOBS.get(key)),
           "docs": sorted(mine.values(), key=lambda d: d["filed"] or "", reverse=True),
           "book": [{"key": k, "sym": v["sym"], "label": v["label"], "weight": v["weight"]}
                    for k, v in sorted(B.items(), key=lambda kv: -kv[1]["weight"])]}
    out["say"] = _say_company(out, N)
    return out


def _names_list(rows: list[dict], n: int = 4) -> str:
    ls = [r["label"] for r in rows[:n]]
    more = len(rows) - len(ls)
    if more > 0:
        return ", ".join(ls) + f" and {more} more"
    return (", ".join(ls[:-1]) + " and " + ls[-1]) if len(ls) > 1 else (ls[0] if ls else "")


def _say_company(c: dict, N: dict) -> list[str]:
    R, out = c["relations"], []
    lab = c["label"]
    cu, su = R["customers"], R["suppliers"]
    top = next((r for r in cu if r.get("basis") == "a_revenue" and r.get("pct")), None)
    if top:
        out.append(f"Its largest disclosed customer is {_the(top['label'])}: {top['pct']:.0f}% of revenue"
                   + (f", which you also hold ({top['weight']:.1f}% of the book)." if top["held"] else "."))
    if cu or su:
        bits = []
        if su:
            bits.append(f"{len(su)} supplier{'s' if len(su) != 1 else ''} ({_names_list(su)})")
        if cu:
            bits.append(f"{len(cu)} customer{'s' if len(cu) != 1 else ''} ({_names_list(cu)})")
        out.append("The filings name " + " and ".join(bits) + ".")
    mine = [r for k in ("suppliers", "customers") for r in R[k] if r["held"]]
    if mine:
        w = sum(r["weight"] or 0 for r in {r["key"]: r for r in mine}.values())
        ks = list({r["key"]: r for r in mine}.values())
        out.append(f"{len(ks)} of them {'are' if len(ks) != 1 else 'is'} in your book ({_names_list(ks, 5)}), "
                   f"{w:.1f}% of it: news from {lab} is news for them.")
    own = [r for r in R["owners"] if r.get("pct")]
    if own:
        out.append(f"{own[0]['label']} owns {own[0]['pct']:.1f}% of it." if own[0]["pct"] % 1 else
                   f"{own[0]['label']} owns {own[0]['pct']:.0f}% of it.")
    st = c["state"]
    any_rel = bool(cu or su or R["competitors"] or R["partners"] or R["owners"] or R["stakes"])
    if st.get("read"):
        if not any_rel:
            out.append(f"Its {st['read']['form']} names no supplier, customer or rival.")
    elif st.get("filer"):
        out.append(("Everything here comes from other companies naming it. " if any_rel else "")
                   + "Its own annual report is on file with the SEC and has not been read yet: "
                     "Read with Claude adds its side.")
    elif not c.get("listed"):
        out.append("It is not a listed company and files nothing, so every line here is another company's word.")
    else:
        out.append("It files nothing with the SEC, so nothing can be read from it. "
                   + ("Every line here is another company's word." if any_rel else
                      "A line would appear here only if another company named it."))
    return out


# --------------------------------------------------------------------------
# the book as a chain
# --------------------------------------------------------------------------

def levels(nodes: list[str], E: list[tuple]) -> dict:
    """
    How far downstream each company sits: a number per node, chosen so that
    a customer is, as nearly as the whole web allows, one step to the right
    of its supplier. It is the trophic level of a food web (MacKay, Johnson
    and Sansom, 2020): least squares over every edge, so a loop (Nvidia sells
    to Amazon, Amazon's cloud sells to Nvidia) costs something instead of
    breaking the layout. Each connected piece starts at zero.
    """
    import numpy as np
    idx = {n: i for i, n in enumerate(nodes)}
    n = len(nodes)
    if not n:
        return {}
    L = np.zeros((n, n))
    v = np.zeros(n)
    adj: dict = {x: set() for x in nodes}
    for a, b, w in E:
        if a not in idx or b not in idx or a == b:
            continue
        i, j = idx[a], idx[b]
        L[i, i] += w
        L[j, j] += w
        L[i, j] -= w
        L[j, i] -= w
        v[j] += w
        v[i] -= w
        adj[a].add(b)
        adj[b].add(a)
    h = np.linalg.lstsq(L, v, rcond=None)[0]
    out, seen = {}, set()
    for s in nodes:
        if s in seen:
            continue
        comp, todo = [], [s]
        seen.add(s)
        while todo:
            x = todo.pop()
            comp.append(x)
            for y in adj[x]:
                if y not in seen:
                    seen.add(y)
                    todo.append(y)
        lo = min(h[idx[x]] for x in comp)
        for x in comp:
            out[x] = round(float(h[idx[x]] - lo), 3)
    return out


def latest_annuals() -> dict:
    """{key: {form, filed}} for the holdings that file with the SEC; asked once an hour."""
    B = book()

    def load():
        out = {}
        for k, h in B.items():
            try:
                a = annual(h["sym"], h.get("name"))
            except Exception:
                a = None
            if a:
                out[k] = {"form": a["form"], "filed": a["filed"]}
        return {"map": out}
    try:
        return store.cache_through("splc:latest:" + str(len(B)), 3600, load).get("map") or {}
    except Exception:
        return {}


def chain() -> dict:
    """
    Your book laid out along its supply links: suppliers to the left, their
    customers to the right, and the outside companies that tie several of
    your holdings together drawn among them.
    """
    B, N = book(), names()
    E = [e for e in edges() if e["kind"] == "supply"]
    touch: dict = {}
    for e in E:
        if e["a"] in B and e["b"] not in B:
            touch.setdefault(e["b"], set()).add(e["a"])
        if e["b"] in B and e["a"] not in B:
            touch.setdefault(e["a"], set()).add(e["b"])
    hubs = {k for k, v in touch.items() if len(v) >= HUB}
    inside = B.keys() | hubs
    used = [e for e in E if e["a"] in inside and e["b"] in inside and (e["a"] in B or e["b"] in B)]
    linked = sorted({x for e in used for x in (e["a"], e["b"])})
    # A link stated three times, or with a share of revenue on it, pulls harder
    # than one mention in a list: it decides which way a loop is drawn.
    lv = levels(linked, [(e["a"], e["b"], min(3.0, len(e["ev"])) + (1.0 if e["pct"] is not None else 0.0))
                         for e in used])
    # Half a level to a column: at a whole level twenty companies share one.
    raw = sorted({int(round(lv[k] * 2)) for k in linked})
    col_of = {k: raw.index(int(round(lv[k] * 2))) for k in linked}
    cols: dict = {}
    for k in linked:
        cols.setdefault(col_of[k], []).append(k)
    order = {}
    for c, ks in cols.items():
        ks.sort(key=lambda k: (0 if k in B else 1, -(B[k]["weight"] if k in B else len(touch.get(k, ())))))
        for i, k in enumerate(ks):
            order[k] = i
    nb: dict = {k: [] for k in linked}
    for e in used:
        nb[e["a"]].append(e["b"])
        nb[e["b"]].append(e["a"])

    def where(k):                          # 0 at the top of its column, 1 at the bottom
        n = len(cols[col_of[k]])
        return order[k] / (n - 1) if n > 1 else 0.5
    for _ in range(4):                     # a company moves towards the height of those it trades with
        for c in sorted(cols):
            ks = sorted(cols[c], key=lambda k: (sum(where(o) for o in nb[k]) / len(nb[k]) if nb[k] else 0.5,
                                                order[k]))
            for i, k in enumerate(ks):
                order[k] = i
            cols[c] = ks
    nodes = []
    for k in linked:
        nd = node(k, B, N)
        nd.update(level=lv[k], col=col_of[k], order=order[k], sector=(B.get(k) or {}).get("sector"),
                  ties=len(touch.get(k, ())) if k not in B else None,
                  n_in=sum(1 for e in used if e["b"] == k), n_out=sum(1 for e in used if e["a"] == k))
        nodes.append(nd)
    eds = [{"a": e["a"], "b": e["b"], "pct": e["pct"], "basis": e["basis"], "share": _share(e, N), "what": e["what"],
            "n": len(e["ev"]), "back": col_of[e["a"]] >= col_of[e["b"]],
            "by": sorted({l["by"] for l in e["ev"] if l.get("by")}),
            "filed": e["ev"][0].get("filed"), "form": e["ev"][0].get("form")} for e in used]
    alone = [{**node(k, B, N), "sector": v.get("sector")} for k, v in sorted(B.items(), key=lambda kv: -kv[1]["weight"])
             if k not in lv]
    shares = sorted([e for e in E if e["pct"] is not None and e["basis"] in ("a_revenue", "b_purchases")
                     and (e["a"] in B or e["b"] in B)], key=lambda e: -e["pct"])
    # every holding, on the map or not: what its filings name, and how fresh the reading is
    again, sread, latest = _reread(), seed().get("read") or {}, latest_annuals()
    all_e = edges()
    rows = []
    for k, h in sorted(B.items(), key=lambda kv: -kv[1]["weight"]):
        su = {e["a"] for e in all_e if e["kind"] == "supply" and e["b"] == k}
        cu = {e["b"] for e in all_e if e["kind"] == "supply" and e["a"] == k}
        rd = ({"form": None, "filed": again[k]} if k in again else sread.get(k))
        la = latest.get(k)
        rows.append({"key": k, "sym": h["sym"], "label": h["label"], "weight": h["weight"], "sector": h.get("sector"),
                     "suppliers": len(su), "customers": len(cu), "in_book": len((su | cu) & B.keys()),
                     "competitors": sum(1 for e in all_e if e["kind"] == "compete" and k in (e["a"], e["b"])),
                     "read": {"form": (rd or {}).get("form"), "filed": (rd or {}).get("filed")} if rd else None,
                     "latest": la, "filer": bool(la),
                     "newer": bool(rd and la and (rd.get("filed") or "") < la["filed"]),
                     "on_map": k in lv})
    out = {"ok": True, "nodes": nodes, "edges": eds, "alone": alone, "rows": rows,
           "shares": [{"a": node(e["a"], B, N), "b": node(e["b"], B, N), "pct": e["pct"], "basis": e["basis"],
                       "share": _share(e, N), "what": e["what"], "filed": e["ev"][0].get("filed"),
                       "form": e["ev"][0].get("form"), "url": e["ev"][0].get("url"),
                       "quote": e["ev"][0].get("quote")} for e in shares[:14]],
           "n_book": len(B), "n_linked": sum(1 for k in linked if k in B),
           "w_linked": round(sum(B[k]["weight"] for k in linked if k in B), 1),
           "read_on": seed().get("read_on"), "n_links": len(links()), "hub_min": HUB,
           "cols": max(cols) + 1 if cols else 0}
    out["say"] = _say_chain(out, B, N, used)
    return out


def _say_chain(c: dict, B: dict, N: dict, used: list) -> list[str]:
    out = []
    if not c["n_linked"]:
        return ["None of your holdings names another as a supplier or a customer in its filings."]
    out.append(f"{c['n_linked']} of your {c['n_book']} shares are tied to another by a supplier or customer link "
               f"their own filings disclose. Together they are {c['w_linked']:.0f}% of the book.")
    buys, sells = {}, {}
    for e in used:
        if e["a"] in B:
            buys.setdefault(e["b"], set()).add(e["a"])
        if e["b"] in B:
            sells.setdefault(e["a"], set()).add(e["b"])
    def top(m, held):
        rows = [(k, v) for k, v in m.items() if (k in B) == held and len(v) >= 3]
        return sorted(rows, key=lambda kv: (-len(kv[1]), -sum(B[x]["weight"] for x in kv[1])))[:2]
    def who(ks):
        ls = [B[k]["label"] for k in sorted(ks, key=lambda k: -B[k]["weight"])]
        return ", ".join(ls[:5]) + (f" and {len(ls) - 5} more" if len(ls) > 5 else "")
    for k, v in top(buys, True):
        out.append(f"{label(k, N)} buys from {len(v)} of your other holdings ({who(v)}): what it spends is their revenue.")
    for k, v in top(sells, True):
        out.append(f"{label(k, N)} supplies {len(v)} of your other holdings ({who(v)}).")
    for k, v in top(buys, False)[:2]:
        out.append(f"{label(k, N)}, which is not in your book, is a customer of {len(v)} of your holdings ({who(v)}).")
    for k, v in top(sells, False)[:1]:
        out.append(f"{label(k, N)}, which is not in your book, supplies {len(v)} of your holdings ({who(v)}).")
    sh = [s for s in c["shares"] if s["a"]["held"] and s["basis"] == "a_revenue"][:3]
    if sh:
        out.append("The largest disclosed dependencies: " + "; ".join(
            f"{s['a']['label']} gets {s['pct']:.0f}% of its revenue from {_the(s['b']['label'])}" for s in sh) + ".")
    if c["alone"]:
        w = sum(a["weight"] or 0 for a in c["alone"])
        out.append(f"{len(c['alone'])} holdings, {w:.0f}% of the book, have no disclosed supply link to the rest: "
                   "they stand outside the chain.")
    return out


_RT: dict = {"t": 0.0, "v": None}


def read_through() -> dict:
    """
    {key: {key, label, sells_to, buys_from, n, weight}} for every company,
    held or not, that a supply link ties to one of your holdings. `sells_to`
    are your holdings that buy from it, `buys_from` your holdings that sell
    to it: when it reports, its numbers say something about them. Dell is
    not in your book; its order book is Nvidia's and Intel's revenue.
    """
    if _RT["v"] is not None and time.time() - _RT["t"] < 120:
        return _RT["v"]
    B, N = book(), names()
    out: dict = {}
    for e in edges():
        if e["kind"] != "supply":
            continue
        for me, other, side in ((e["a"], e["b"], "sells_to"), (e["b"], e["a"], "buys_from")):
            if other in B and other != me:
                h = B[other]
                d = out.setdefault(me, {"key": me, "label": label(me, N), "sells_to": [], "buys_from": []})
                d[side].append({"key": other, "sym": h["sym"], "label": h["label"], "weight": h["weight"],
                                "what": e["what"], "share": _share(e, N), "sentences": len(e["ev"])})
    for d in out.values():
        for k in ("sells_to", "buys_from"):
            d[k].sort(key=lambda r: -r["weight"])
        both = {r["key"]: r for k in ("sells_to", "buys_from") for r in d[k]}
        d["n"] = len(both)
        d["weight"] = round(sum(r["weight"] for r in both.values()), 2)
        # One mention in a list of forty cloud customers is a tie, but not one
        # worth a line in the morning brief. Strong: two holdings, a share of
        # revenue with a number on it, or a link stated more than once.
        d["strong"] = d["n"] >= 2 or any(r["share"] or r["sentences"] >= 2 for r in both.values())
    _RT.update(t=time.time(), v=out)
    return out


def chain_tag(sym: str, rt: Optional[dict] = None) -> Optional[dict]:
    """The read-through of one company, small enough for a calendar chip; None when there is none."""
    d = (rt if rt is not None else read_through()).get(key_of(sym))
    if not d:
        return None
    return {"n": d["n"], "weight": d["weight"], "strong": d["strong"],
            "sells_to": [r["label"] for r in d["sells_to"]][:6],
            "buys_from": [r["label"] for r in d["buys_from"]][:6], "href": "/m/SPLC/" + sym}


def linked(sym: str) -> dict:
    """
    Read-through: the holdings on either side of one company, held or not.
    {key, label, suppliers, customers, competitors}, each a list of
    {sym, label, weight, what, share}. When this company reports, these are
    the positions its numbers say something about.
    """
    key = key_of(sym)
    B, N = book(), names()
    out = {"key": key, "label": label(key, N), "suppliers": [], "customers": [], "competitors": []}
    for e in edges():
        if key not in (e["a"], e["b"]) or e["kind"] not in ("supply", "compete"):
            continue
        other = e["b"] if e["a"] == key else e["a"]
        if other not in B or other == key:
            continue
        h = B[other]
        row = {"key": other, "sym": h["sym"], "label": h["label"], "weight": h["weight"], "what": e["what"],
               "share": _share(e, N)}
        if e["kind"] == "compete":
            out["competitors"].append(row)
        else:
            out["customers" if e["a"] == key else "suppliers"].append(row)
    for k in ("suppliers", "customers", "competitors"):
        out[k].sort(key=lambda r: -r["weight"])
    out["weight"] = round(sum(r["weight"] for r in {r["key"]: r for k in ("suppliers", "customers")
                                                      for r in out[k]}.values()), 2)
    return out


# --------------------------------------------------------------------------
# who names this company: EDGAR full-text search
# --------------------------------------------------------------------------

FTS = "https://efts.sec.gov/LATEST/search-index"
MENTION_DOCS = 10           # other companies' reports opened for their sentence
MENTION_BUDGET = 14.0       # seconds


def _search_name(key: str, N: dict) -> str:
    al = N.get(key) or [key]
    best = max(al, key=lambda a: (len(a.split()) >= 2, len(a)))
    return best


def mentions(sym: str) -> dict:
    """
    The other companies whose annual reports of the last fourteen months name
    this one, from the SEC's full-text search, with the sentence for the
    first few. These are leads, not links: nobody has read them.
    """
    key = key_of(sym)
    N = names()
    q = _search_name(key, N)

    def load():
        import urllib.parse
        from datetime import timedelta
        today = date.today()
        url = (f"{FTS}?q={urllib.parse.quote(chr(34) + q + chr(34))}&forms=10-K,20-F"
               f"&dateRange=custom&startdt={(today - timedelta(days=430)).isoformat()}&enddt={today.isoformat()}")
        j = json.loads(sec._get(url))
        hits = (j.get("hits") or {}).get("hits") or []
        me = None
        h = book().get(key)
        try:
            f = ed.filer(h["sym"] if h else key, h.get("name") if h else None)
            me = f["cik"] if f else None
        except Exception:
            pass
        rows, seen = [], set()
        for x in hits:
            s = x.get("_source") or {}
            if (s.get("file_type") or "") not in ("10-K", "20-F", "10-K/A", "20-F/A"):
                continue                      # the report itself, not its exhibits
            ciks = [int(c) for c in s.get("ciks") or []]
            if not ciks or (me and me in ciks) or ciks[0] in seen:
                continue
            seen.add(ciks[0])
            adsh, _, fn = (x.get("_id") or "").partition(":")
            disp = (s.get("display_names") or [""])[0]
            m = re.match(r"^(.*?)\s+\(([A-Z0-9.\-, ]+)\)\s+\(CIK", disp)
            rows.append({"name": (m.group(1) if m else disp.split("  (CIK")[0]).strip(),
                         "ticker": (m.group(2).split(",")[0].strip() if m else None), "cik": ciks[0],
                         "form": s.get("file_type"), "filed": s.get("file_date"),
                         "url": f"https://www.sec.gov/Archives/edgar/data/{ciks[0]}/{adsh.replace('-', '')}/{fn}",
                         "score": x.get("_score"), "quote": None, "opened": False})
        total = ((j.get("hits") or {}).get("total") or {}).get("value")
        rx = name_rx(N.get(key) or [q])
        t0 = time.time()
        opened = 0
        for r in rows:
            if opened >= MENTION_DOCS or time.time() - t0 > MENTION_BUDGET or rx is None:
                break
            try:
                text = ed.read_url(r["url"]).get("text") or ""
            except Exception:
                continue
            opened += 1
            r["opened"] = True
            best, score = None, -1
            for par in clean_lines(text):
                if "|" in par or not rx.search(par):
                    continue
                for s2 in sentences(par):
                    if rx.search(s2) and not _NOISE.search(s2) and len(s2) >= 40:
                        sc = sum(2 for _, c in CUES if c.search(s2)) + (1 if len(s2) < 400 else 0)
                        if sc > score:
                            best, score = s2, sc
            if best:
                r["quote"] = re.sub(r"\s+", " ", best)[:600]
        rows.sort(key=lambda r: (0 if r["quote"] else 1 if not r["opened"] else 2))
        return {"q": q, "rows": rows[:40], "total": total, "opened": opened}
    try:
        d = store.cache_through(f"splc:fts:{key}", 7 * DAY, load)
    except Exception as e:
        return {"ok": False, "key": key, "q": q, "rows": [], "error": f"{e.__class__.__name__}: {e}"[:200]}
    B = book()
    rows = []
    for r in d.get("rows") or []:
        k = key_of(r["ticker"]) if r.get("ticker") else None
        rows.append({**r, "held": bool(k and k in B), "key": k})
    return {"ok": True, "key": key, "q": d.get("q"), "rows": rows, "total": d.get("total"), "opened": d.get("opened"),
            "age_s": d.get("_age_s")}


# --------------------------------------------------------------------------
# a reader for a company the map has not read: Claude, on your plan
# --------------------------------------------------------------------------

JOBS: dict = {}
_LOCK = threading.Lock()
READ_KINDS = {"supplier", "customer", "competitor", "partner", "stake_in", "owner"}
# A sentence that names two companies for an occasion, not for business.
_OCCASION = re.compile(r"\bwebinar\b|\bkeynote\b|\bMagic Quadrant\b|\bpanel discussion\b|\bsummit\b|\baward", re.I)
READ_BASIS = {("customer", "revenue"): "a_revenue", ("customer", "receivables"): "a_receivables",
              ("supplier", "purchases"): "b_purchases", ("supplier", "cost"): "b_cost",
              ("stake_in", "stake"): "stake", ("owner", "stake"): "stake"}

SYSTEM = """You read numbered passages cut from one company's SEC filings and say which other organisations they name and what each is to the company. You are filling a supply-chain map that a private investor relies on. Every line you return is shown to them next to the passage it came from, so record only what a passage states.

For each relationship return:
  p      the passage number
  other  the organisation, as the passage writes it
  key    its key from the list of known companies when it is one of them, otherwise null
  ticker its stock symbol when you are certain it is listed and it has no key, otherwise null
  kind   one of: supplier, customer, competitor, partner, stake_in, owner
  what   at most 14 plain words on what flows or what the tie is; include a figure the passage gives
  pct    a percentage the passage states, otherwise null
  basis  what pct is a share of: "revenue", "receivables", "purchases", "cost" or "stake"; null without pct

The kinds, from the company's side:
  supplier    the other sells to the company: makes its product, provides parts, cloud, a platform it depends on, a licence
  customer    the company sells to the other
  competitor  the passage calls it a competitor, or says they compete
  partner     a joint venture, an alliance, co-development: no clear buyer and seller
  stake_in    the company owns part of the other
  owner       the other owns part of the company

Rules:
  One line per organisation and passage. Use the passage that states the relationship most directly, and at most two passages for the same organisation and kind.
  Skip a passage that states no relationship: regulators, lawsuits, an executive's former employer, index members, banks in a loan or a share sale, auditors, the company's own subsidiaries, brands and products.
  A company bought outright is not a relationship. Nor is a joint webinar, a conference, an award or an analyst's ranking. A product name stands for its maker only when the maker is beyond doubt (Azure is Microsoft, AWS is Amazon).
  Begin "what" with a small letter unless it begins with a name.
  "Customer A" is not an organisation. Put such a statement in notes.
  pct with basis "revenue" means the other is that share of the company's revenue. Never compute a percentage; copy one the passage states.
  Use nothing you know from outside the passages.

Also return notes: at most five short statements the passages make about concentration without naming anyone ("One customer was 22% of revenue. Not named."), each with its passage number.

Answer with one JSON object and nothing else:
{"links": [{"p": 0, "other": "", "key": null, "ticker": null, "kind": "", "what": "", "pct": null, "basis": null}], "notes": [{"p": 0, "text": ""}]}"""


def _pub(j: Optional[dict]) -> Optional[dict]:
    if not j:
        return None
    return {k: v for k, v in j.items() if k not in ("text",)} | {
        "elapsed_s": round((j.get("finished") or time.time()) - j["started"]), "chars": len(j.get("text") or "")}


def material(sym: str) -> dict:
    """What a reader is given for one company: its passages and the names that occur in them."""
    key = key_of(sym)
    B, N = book(), names()
    h = B.get(key)
    use = h["sym"] if h else key
    P = passages(use, h.get("name") if h else None, known(key, N), extra_own=N.get(key))
    hay = "\n".join(p["text"] for p in P["items"])
    seen = []
    for k, al in N.items():
        if k == key:
            continue
        rx = name_rx(al)
        if rx is not None and rx.search(hay):
            seen.append(f"{k}: {', '.join(al[:3])}")
    if key in N:
        lab = label(key, N)
    else:
        from .morning import brands
        title = (P["docs"][0].get("title") if P["docs"] else None) or key
        lab = (brands(use, title) or [title])[0]
    head = [f"Company: {lab} ({use})", "", "Documents:"]
    head += [f"  {i}: {d['form']} filed {d['filed']}" for i, d in enumerate(P["docs"])]
    head += ["", "Known companies (key: names). Use the key when a passage names one of them:"] + ["  " + s for s in seen]
    head += ["", "Passages:"]
    body = [f"[{p['n']}] (document {p['d']}) {p['text']}" for p in P["items"]]
    return {"key": key, "sym": use, "label": lab, "docs": P["docs"], "items": P["items"],
            "text": "\n".join(head + body)}


def _json_of(text: str) -> dict:
    t = (text or "").strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    if m:
        t = m.group(1)
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise ValueError("the reader returned no JSON")
    return json.loads(t[i:j + 1])


def _slug(name: str) -> str:
    return "~" + re.sub(r"[^a-z0-9]+", "-", ed.norm_name(name) or name.lower()).strip("-")[:40]


def accept(key: str, M: dict, answer: dict, N: Optional[dict] = None) -> dict:
    """
    What survives of a reader's answer. A line is kept only if its passage
    exists and the passage really names the organisation: by one of the
    names the map has for the key the reader gave, or by the name the reader
    wrote. The quote is the passage, never the reader's words.
    """
    N = N if N is not None else names()
    by_n = {p["n"]: p for p in M["items"]}
    out, dropped, new_names = [], [], {}
    per: dict = {}
    for x in answer.get("links") or []:
        try:
            p = by_n[int(x.get("p"))]
            kind = str(x.get("kind") or "").strip()
            other = str(x.get("other") or "").strip()
        except Exception:
            dropped.append({"why": "no such passage", "line": x})
            continue
        if kind not in READ_KINDS or len(other) < 2:
            dropped.append({"why": "unknown kind or no name", "line": x})
            continue
        k = x.get("key") if isinstance(x.get("key"), str) and x.get("key") in N else None
        al = (N.get(k) or []) if k else []
        rx = name_rx(al + [other])
        if rx is None or not rx.search(p["text"]):
            dropped.append({"why": "the passage does not name it", "line": x})
            continue
        if k == key:
            dropped.append({"why": "it is the company itself", "line": x})
            continue
        if kind == "partner" and _OCCASION.search(p["text"]):
            dropped.append({"why": "an occasion, not a relationship", "line": x})
            continue
        if not k:
            tk = str(x.get("ticker") or "").upper().strip()
            c = sec.cik_for(tk) if tk else None
            first = (ed.norm_name(other).split(" ") or [""])[0]
            if c and first and first in ed.norm_name(c.get("title") or ""):
                k = tk
            else:
                hit = next((kk for kk, v in N.items() if any(a.lower() == other.lower() for a in v)), None)
                k = hit or _slug(other)
            if k not in N:
                new_names[k] = [other]
        if k == key:
            continue
        if per.get((k, kind), 0) >= 2:
            continue
        per[(k, kind)] = per.get((k, kind), 0) + 1
        pct, basis = x.get("pct"), None
        try:
            pct = float(pct) if pct is not None else None
        except Exception:
            pct = None
        if pct is not None:
            basis = READ_BASIS.get((kind, str(x.get("basis") or "")))
            num = r"(?<![\d.])" + re.escape(f"{pct:g}")
            stated = re.search(num + r"\s?(?:%|percent)", p["text"]) or (
                p.get("table") and re.search(num + r"(?![\d.])", p["text"]))
            if basis is None or not (0 < pct <= 100) or not stated:
                pct, basis = None, None           # a share the passage does not state is not kept
        doc = M["docs"][p["d"]]
        a, b, kd = {"supplier": (k, key, "supply"), "customer": (key, k, "supply"),
                    "competitor": (key, k, "compete"), "partner": (key, k, "partner"),
                    "stake_in": (key, k, "stake"), "owner": (k, key, "stake")}[kind]
        what = re.sub(r"\s+", " ", str(x.get("what") or "")).strip()[:180]
        w0 = what.split(" ", 1)[0] if what else ""
        # "Predominant vendor" begins a sentence; "Azure hosting" begins with a
        # name. A name is in the passage with its capital; the other is not.
        if w0[:1].isupper() and not w0.isupper() and not re.search(r"(?<![A-Za-z])" + re.escape(w0) + r"(?![A-Za-z])", p["text"]):
            what = what[0].lower() + what[1:]
        out.append({"a": a, "b": b, "kind": kd, "what": what,
                    "pct": pct, "basis": basis, "by": key, "form": doc["form"], "filed": doc["filed"],
                    "url": doc["url"], "quote": p["text"]})
    notes = []
    for x in (answer.get("notes") or [])[:5]:
        try:
            p = by_n[int(x.get("p"))]
            t = re.sub(r"\s+", " ", str(x.get("text") or "")).strip()
        except Exception:
            continue
        if t:
            doc = M["docs"][p["d"]]
            notes.append({"text": t[:240], "quote": p["text"], "form": doc["form"], "filed": doc["filed"], "url": doc["url"]})
    return {"links": out, "notes": notes, "names": new_names, "dropped": dropped}


def store_reading(key: str, M: dict, acc: dict, model_name: str, usage: Optional[dict] = None) -> int:
    """Replace what was read before for this company with the new reading."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    ann = next((d for d in M["docs"] if not d.get("release")), None) or (M["docs"][0] if M["docs"] else {})
    have = names()                     # before the write: a second connection must not wait on this one
    _init()
    with store.conn() as c:
        c.execute("DELETE FROM splc_link WHERE by=? AND source='claude'", (key,))
        c.execute("DELETE FROM splc_note WHERE key=? AND source='claude'", (key,))
        for k, al in acc["names"].items():
            c.execute("INSERT OR IGNORE INTO splc_name (key, label, aliases, created) VALUES (?,?,?,?)",
                      (k, al[0], json.dumps(al[1:]), now))
        if key not in have:
            c.execute("INSERT OR IGNORE INTO splc_name (key, label, aliases, created) VALUES (?,?,?,?)",
                      (key, M["label"], "[]", now))
        n = 0
        for l in acc["links"]:
            cur = c.execute("INSERT OR IGNORE INTO splc_link (a,b,kind,what,pct,basis,by,form,filed,url,quote,source,model,created) "
                            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (l["a"], l["b"], l["kind"], l["what"], l["pct"], l["basis"], l["by"], l["form"], l["filed"],
                             l["url"], l["quote"], "claude", model_name, now))
            n += cur.rowcount
        for x in acc["notes"]:
            c.execute("INSERT INTO splc_note (key,text,quote,form,filed,url,source,created) VALUES (?,?,?,?,?,?,?,?)",
                      (key, x["text"], x["quote"], x["form"], x["filed"], x["url"], "claude", now))
        c.execute("INSERT OR REPLACE INTO splc_read (key,sym,form,filed,acc,read_at,model,n_links,n_passages,usage) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (key, M["sym"], ann.get("form"), ann.get("filed"), ann.get("acc"), now, model_name, n,
                   len(M["items"]), json.dumps(usage or {})))
    return n


def _work_dir(key: str) -> str:
    w = os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), "splc_work", re.sub(r"[^A-Za-z0-9_.\-]", "_", key))
    os.makedirs(w, exist_ok=True)
    return w


def _run_read(job: dict, sym: str):
    from . import earnai
    try:
        job["phase"] = "cutting the passages out of the filings"
        M = material(sym)
        key = M["key"]
        if not M["docs"]:
            raise LookupError("this company files nothing with the SEC, so there is nothing to read")
        if not M["items"]:
            raise LookupError("its filings name no other organisation next to a word about buying, selling or competing")
        job.update(passages=len(M["items"]), docs=len(M["docs"]))
        ask = M["text"] + "\n\nReturn the JSON object."
        if job["writer"] == "claude-code":
            work = _work_dir(key)
            with open(os.path.join(work, "material.md"), "w", encoding="utf-8") as f:
                f.write(ask)
            job["phase"] = "reading"
            out = earnai.cc_stream(earnai.cc_command("", system=SYSTEM), ask, work, job)
            text, name = out["text"], f"Claude Code · {out['model'] or 'default model'}"
            usage = {**(out.get("usage") or {}), "api_equivalent_usd": out.get("api_usd"), "ms": out.get("ms")}
            job["api_usd"] = out.get("api_usd")
        else:
            m = earnai.model()
            kw = dict(model=m, max_tokens=16000, system=SYSTEM, messages=[{"role": "user", "content": ask}],
                      thinking={"type": "adaptive"}, output_config={"effort": earnai.WRITE_EFFORT})
            betas = []
            if m in earnai.FALLBACK_OK:
                betas.append("server-side-fallback-2026-07-01")
                kw["fallbacks"] = "default"
            job["phase"] = "reading"
            with earnai._client().beta.messages.stream(betas=betas, **kw) as stream:
                for ev in stream:
                    if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                        job["text"] += ev.delta.text
                msg = stream.get_final_message()
            text = "\n".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            name = getattr(msg, "model", m) or m
            usage = {k: getattr(msg.usage, k, 0) or 0 for k in ("input_tokens", "output_tokens")}
            job["cost_usd"] = earnai.cost(name if name in earnai.MODELS else m, usage)
        job["phase"] = "checking every line against its passage"
        acc = accept(key, M, _json_of(text))
        n = store_reading(key, M, acc, name, usage)
        _BOOK["t"] = _RT["t"] = 0.0
        job.update(state="done", phase="done", model=name, links=n, notes=len(acc["notes"]),
                   dropped=len(acc["dropped"]), dropped_why=[d["why"] for d in acc["dropped"]][:20])
    except Exception as e:
        job.update(state="error", error=f"{e.__class__.__name__}: {e}"[:300])
    finally:
        job["finished"] = time.time()


def start_read(sym: str) -> dict:
    """Have Claude read one company's filings: Claude Code on your plan, or your API key, as set for the reports."""
    from . import earnai
    key = key_of(sym)
    if key.startswith("~"):
        raise LookupError("this company is not listed, so it has no filings to read")
    w = earnai.writer()
    if w == "api" and not earnai.api_key():
        raise PermissionError("no Anthropic API key — add one in Settings, or switch the writer to Claude Code")
    if w == "claude-code":
        st = earnai.cc_status()
        if not st["found"]:
            raise PermissionError("Claude Code was not found on this computer — install the Claude desktop app, "
                                  "or switch the writer to an API key in Settings")
        if not st["logged_in"]:
            st = earnai.cc_status(force=True)
            if not st["logged_in"]:
                raise PermissionError("Claude Code is not signed in yet — sign in on the Report tab of an earnings page")
    with _LOCK:
        j = JOBS.get(key)
        if j and j["state"] == "running":
            return _pub(j)
        j = {"state": "running", "phase": "starting", "text": "", "started": time.time(), "finished": None,
             "error": None, "writer": w, "sym": sym, "key": key,
             "model": (earnai.cc_model() or "default") if w == "claude-code" else earnai.model()}
        JOBS[key] = j
    threading.Thread(target=_run_read, args=(j, sym), daemon=True, name=f"splc-read-{key}").start()
    return _pub(j)


def forget(sym: str) -> dict:
    """Drop what Claude read for one company since the map shipped; the shipped reading returns."""
    key = key_of(sym)
    _init()
    with store.conn() as c:
        n = c.execute("DELETE FROM splc_link WHERE by=? AND source='claude'", (key,)).rowcount
        c.execute("DELETE FROM splc_note WHERE key=? AND source='claude'", (key,))
        c.execute("DELETE FROM splc_read WHERE key=?", (key,))
    JOBS.pop(key, None)
    return {"ok": True, "key": key, "removed": n}
