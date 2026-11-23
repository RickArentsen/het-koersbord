"""
SEC EDGAR: what the company itself reported, not what a data vendor re-keyed.

Three free endpoints, no key:

    www.sec.gov/files/company_tickers.json        ticker -> CIK
    data.sec.gov/submissions/CIK##########.json    every filing, with 8-K items
    data.sec.gov/api/xbrl/companyfacts/...         every number tagged in a
                                                   10-K or 10-Q, back to ~2009

and, for the part that answers "which business is growing", the XBRL
instance of individual filings. companyfacts deliberately DROPS dimensional
facts — revenue by segment, by product, by country, by customer — so those
are read from the filing itself.

Two things the raw data gets wrong that this module fixes:

  * 10-Ks report the year, not the fourth quarter, and cash-flow statements
    in 10-Qs are year-to-date only. So Q4 is FY minus nine months, and a
    cash-flow quarter is this YTD minus the last one. Every derived value is
    flagged `derived`, because a subtraction inherits every restatement.
  * The same period appears in several filings — as itself, then as the
    prior-year comparative. The LATEST filing wins: that is the restated,
    split-adjusted number.

The SEC asks automated clients to identify themselves. The default below does;
set `sec_user_agent` in the settings table to put your own contact in it.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, timedelta
from typing import Optional

from . import store, net

UA_DEFAULT = "PersonalBloomberg/1.0 personal-research"
TIMEOUT = 30

# Forms whose numbers are the company's own periodic accounts.
PERIODIC = {"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "20-F", "40-F", "20-F/A"}


def _headers() -> dict:
    return {"User-Agent": store.get_setting("sec_user_agent") or UA_DEFAULT,
            "Accept": "application/json, application/xml, text/xml, */*"}


def _get(url: str) -> bytes:
    return net.get(url, _headers(), timeout=TIMEOUT)


def _json(url: str):
    return json.loads(_get(url).decode("utf-8", "replace"))


def _d(s: str) -> date:
    return date.fromisoformat(s[:10])


def _days(a: str, b: str) -> int:
    return (_d(b) - _d(a)).days


# --------------------------------------------------------------------------
# ticker -> CIK
# --------------------------------------------------------------------------

def _load_tickers() -> dict:
    raw = _json("https://www.sec.gov/files/company_tickers.json")
    m = {}
    for r in raw.values():
        m[str(r["ticker"]).upper()] = {"cik": int(r["cik_str"]), "title": r.get("title")}
    return {"map": m}


def cik_for(ticker: str) -> Optional[dict]:
    """{cik, title} for a US-listed symbol, or None (foreign listings, funds)."""
    t = (ticker or "").upper().strip()
    if not t or t.startswith("^") or "=" in t:
        return None
    # Yahoo writes class shares with a dot, the SEC with a dash; an exchange
    # suffix (.AS, .DE) means a listing the SEC does not cover at all.
    if re.search(r"\.[A-Z]{1,3}$", t) and not re.search(r"\.[AB]$", t):
        return None
    t = t.replace(".", "-")
    try:
        m = store.cache_through("sec:tickers", 7 * 86400, _load_tickers)
    except Exception:
        return None
    return (m.get("map") or {}).get(t)


def _cik10(cik: int) -> str:
    return f"{int(cik):010d}"


# --------------------------------------------------------------------------
# filings
# --------------------------------------------------------------------------

# 8-K item codes, in words. Only the ones worth reading are named; 9.01 is the
# exhibit list and says nothing on its own.
ITEMS_8K = {
    "1.01": ("Material agreement", "deal"),
    "1.02": ("Material agreement terminated", "deal"),
    "1.03": ("Bankruptcy or receivership", "risk"),
    "1.05": ("Cybersecurity incident", "risk"),
    "2.01": ("Acquisition or disposal completed", "deal"),
    "2.02": ("Results of operations", "earnings"),
    "2.03": ("New debt or off-balance obligation", "capital"),
    "2.04": ("Debt acceleration triggered", "risk"),
    "2.05": ("Restructuring / exit costs", "risk"),
    "2.06": ("Material impairment", "risk"),
    "3.01": ("Delisting notice", "risk"),
    "3.02": ("Unregistered share sale", "capital"),
    "3.03": ("Shareholder rights modified", "capital"),
    "4.01": ("Auditor changed", "risk"),
    "4.02": ("Prior financials unreliable", "risk"),
    "5.01": ("Change in control", "deal"),
    "5.02": ("Executive or director change", "management"),
    "5.03": ("Bylaws / fiscal year amended", "governance"),
    "5.07": ("Shareholder vote results", "governance"),
    "7.01": ("Reg FD disclosure", "other"),
    "8.01": ("Other material event", "other"),
}

# Forms that are events in themselves.
EVENT_FORMS = {
    "10-K": ("Annual report", "report"), "10-Q": ("Quarterly report", "report"),
    "20-F": ("Annual report (foreign)", "report"), "6-K": ("Foreign issuer report", "other"),
    "S-4": ("Merger registration", "deal"), "425": ("Merger communication", "deal"),
    "DEFM14A": ("Merger proxy", "deal"), "SC TO-T": ("Tender offer", "deal"),
    "SC 13D": ("Activist / 5%+ stake", "deal"), "SC 13D/A": ("5%+ stake amended", "deal"),
    "S-3": ("Shelf registration", "capital"), "S-3ASR": ("Shelf registration", "capital"),
    "424B2": ("Debt / note offering", "capital"), "424B5": ("Securities offering", "capital"),
    "DEF 14A": ("Annual proxy", "governance"),
}


def _load_submissions(cik: int) -> dict:
    raw = _json(f"https://data.sec.gov/submissions/CIK{_cik10(cik)}.json")
    r = (raw.get("filings") or {}).get("recent") or {}
    rows = []
    n = len(r.get("form") or [])
    form4 = defaultdict(int)
    for i in range(n):
        form = r["form"][i]
        if form in ("4", "4/A"):
            form4[r["filingDate"][i][:7]] += 1
            continue
        if form in ("3", "5", "144", "144/A", "SC 13G", "SC 13G/A", "13F-HR",
                    "CORRESP", "UPLOAD", "FWP", "11-K", "S-8", "S-8 POS"):
            continue
        rows.append({
            "form": form, "date": r["filingDate"][i], "acc": r["accessionNumber"][i],
            "doc": r["primaryDocument"][i], "items": r.get("items", [""] * n)[i] or "",
            "report": r.get("reportDate", [""] * n)[i] or "",
            "desc": (r.get("primaryDocDescription") or [""] * n)[i] or "",
            "accepted": (r.get("acceptanceDateTime") or [""] * n)[i] or "",
        })
    return {"cik": cik, "name": raw.get("name"), "sic": raw.get("sic"),
            "sic_desc": raw.get("sicDescription"), "fye": raw.get("fiscalYearEnd"),
            "tickers": raw.get("tickers"), "filings": rows,
            "form4_by_month": dict(sorted(form4.items()))}


def submissions(cik: int) -> dict:
    return store.cache_through(f"sec:sub:{cik}", 6 * 3600, lambda: _load_submissions(cik))


def filing_url(cik: int, acc: str, doc: str = "") -> str:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}"
    return f"{base}/{doc}" if doc else f"{base}/{acc}-index.htm"


def events(cik: int, limit: int = 60) -> list[dict]:
    """Material events from the filing index, newest first, in words."""
    sub = submissions(cik)
    out = []
    for f in sub.get("filings") or []:
        form = f["form"]
        if form in ("8-K", "8-K/A"):
            codes = [c.strip() for c in f["items"].split(",") if c.strip()]
            named = [ITEMS_8K[c] for c in codes if c in ITEMS_8K]
            if not named:
                continue
            kinds = [k for _, k in named]
            kind = next((k for k in ("deal", "risk", "earnings", "management", "capital")
                         if k in kinds), kinds[0])
            out.append({"date": f["date"], "form": form, "kind": kind,
                        "what": "; ".join(n for n, _ in named), "items": codes,
                        "accepted": f.get("accepted"),
                        "url": filing_url(cik, f["acc"], f["doc"])})
        elif form in EVENT_FORMS:
            what, kind = EVENT_FORMS[form]
            if f.get("report"):
                what += f" for period ending {f['report']}"
            out.append({"date": f["date"], "form": form, "kind": kind, "what": what,
                        "items": [], "accepted": f.get("accepted"),
                        "url": filing_url(cik, f["acc"], f["doc"])})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------
# companyfacts: every reported number, as quarterly and annual series
# --------------------------------------------------------------------------

# kind: flow   additive over time (revenue, cash flows) — quarters derivable
#       avg    a period average (diluted shares) — NOT additive, never derived
#       eps    per share: additive enough to derive Q4, flagged as derived
#       inst   a balance at a date
CONCEPTS = {
    "revenue": ("flow", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
                         "SalesRevenueNet", "RevenueFromContractWithCustomerIncludingAssessedTax",
                         "RevenuesNetOfInterestExpense", "SalesRevenueGoodsNet"]),
    "cost_of_revenue": ("flow", ["CostOfRevenue", "CostOfGoodsAndServicesSold",
                                 "CostOfGoodsSold"]),
    "gross_profit": ("flow", ["GrossProfit"]),
    "rnd": ("flow", ["ResearchAndDevelopmentExpense",
                     "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"]),
    "sga": ("flow", ["SellingGeneralAndAdministrativeExpense"]),
    "op_income": ("flow", ["OperatingIncomeLoss"]),
    "pretax": ("flow", ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
    "tax": ("flow", ["IncomeTaxExpenseBenefit"]),
    "net_income": ("flow", ["NetIncomeLoss", "ProfitLoss",
                            "NetIncomeLossAvailableToCommonStockholdersBasic"]),
    "eps": ("eps", ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted",
                    "EarningsPerShareBasic"]),
    "shares": ("avg", ["WeightedAverageNumberOfDilutedSharesOutstanding",
                       "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
                       "WeightedAverageNumberOfSharesOutstandingBasic"]),
    "ocf": ("flow", ["NetCashProvidedByUsedInOperatingActivities",
                     "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    "capex": ("flow", ["PaymentsToAcquirePropertyPlantAndEquipment",
                       "PaymentsToAcquireProductiveAssets",
                       "PaymentsForCapitalImprovements"]),
    "acquisitions": ("flow", ["PaymentsToAcquireBusinessesNetOfCashAcquired",
                              "PaymentsToAcquireBusinessesGross"]),
    "sbc": ("flow", ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]),
    "buybacks": ("flow", ["PaymentsForRepurchaseOfCommonStock"]),
    "dividends": ("flow", ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"]),
    "dna": ("flow", ["DepreciationDepletionAndAmortization",
                     "DepreciationAmortizationAndAccretionNet", "DepreciationAndAmortization",
                     "Depreciation"]),
    "interest": ("flow", ["InterestExpense", "InterestExpenseNonoperating",
                          "InterestExpenseDebt"]),
    "cash": ("inst", ["CashAndCashEquivalentsAtCarryingValue",
                      "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    "st_inv": ("inst", ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                        "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]),
    "debt": ("inst", ["LongTermDebt", "LongTermDebtNoncurrent", "DebtInstrumentCarryingAmount"]),
    "debt_st": ("inst", ["CommercialPaper", "ShortTermBorrowings", "DebtCurrent"]),
    "equity": ("inst", ["StockholdersEquity",
                        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    "assets": ("inst", ["Assets"]),
    "ppe": ("inst", ["PropertyPlantAndEquipmentNet",
                     "PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization"]),
    "goodwill": ("inst", ["Goodwill"]),
    "rpo": ("inst", ["RevenueRemainingPerformanceObligation"]),
    "deferred_rev": ("inst", ["ContractWithCustomerLiabilityCurrent", "DeferredRevenueCurrent"]),
    "inventory": ("inst", ["InventoryNet"]),
    "receivables": ("inst", ["AccountsReceivableNetCurrent"]),
}

# IFRS filers (20-F): the handful of lines worth mapping.
IFRS = {
    "revenue": ["Revenue", "RevenueFromContractsWithCustomers"],
    "gross_profit": ["GrossProfit"],
    "op_income": ["ProfitLossFromOperatingActivities"],
    "net_income": ["ProfitLossAttributableToOwnersOfParent", "ProfitLoss"],
    "eps": ["DilutedEarningsLossPerShare", "BasicEarningsLossPerShare"],
    "ocf": ["CashFlowsFromUsedInOperatingActivities"],
    "capex": ["PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
    "cash": ["CashAndCashEquivalents"],
    "equity": ["EquityAttributableToOwnersOfParent", "Equity"],
    "assets": ["Assets"],
    "rnd": ["ResearchAndDevelopmentExpense"],
    # What a valuation needs beyond the income statement: the share count to
    # get from profit to a price, and the debt to get from a price to a value.
    "shares": ["AdjustedWeightedAverageShares", "WeightedAverageShares"],
    "pretax": ["ProfitLossBeforeTax"],
    "tax": ["IncomeTaxExpenseContinuingOperations"],
    "debt": ["NoncurrentPortionOfNoncurrentBorrowings", "LongtermBorrowings", "Borrowings"],
    "debt_st": ["CurrentPortionOfLongtermBorrowings", "ShorttermBorrowings",
                "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings"],
    "dividends": ["DividendsPaidClassifiedAsFinancingActivities"],
    "inventory": ["Inventories"],
    "dna": ["DepreciationAndAmortisationExpense", "DepreciationExpense"],
}


def pick_unit(units: dict) -> Optional[str]:
    """
    The unit a company reports a line in: the one with the most periodic facts.
    TSMC files every year in Taiwan dollars and adds a dollar translation for
    a few; preferring the dollars kept nine of twenty-six years, and mixed the
    translated years with Yahoo's quarters in Taiwan dollars. A tie goes to
    the dollar, which is all a US filer has anyway.
    """
    def n(u):
        return sum(1 for f in units[u] if f.get("form") in PERIODIC and f.get("val") is not None)

    def newest(u):
        return max((f.get("end") or "" for f in units[u] if f.get("form") in PERIODIC), default="")
    # Only units still in use: Nebius filed as Yandex in roubles for years and
    # reports in dollars now; the roubles have more facts and none recent.
    top = max((newest(u) for u in units), default="")
    live = [u for u in units if not top or not newest(u) or newest(u)[:4] >= str(int(top[:4]) - 1)] or list(units)
    return max(live, key=lambda u: (n(u), u in ("USD", "USD/shares", "shares", "pure")), default=None)


def unit_currency(unit: Optional[str]) -> Optional[str]:
    """'TWD' from 'TWD' or 'TWD/shares'; None for shares and ratios."""
    u = (unit or "").split("/")[0]
    return u if re.fullmatch(r"[A-Z]{3}", u) else None


def _collect(ns: dict, names: list[str]) -> tuple[dict, dict, Optional[str]]:
    """
    Durations {(start,end): rec} and instants {end: rec}, merging candidate
    tags. A company changes tags over the years (SalesRevenueNet became
    RevenueFromContractWithCustomer... in 2018), so the tag with the NEWEST
    data leads and the others only fill periods it does not cover.
    """
    per = []
    for n in names:
        c = ns.get(n)
        if not c:
            continue
        units = c.get("units") or {}
        unit = pick_unit(units)
        if not unit:
            continue
        dur, ins = {}, {}
        for f in units[unit]:
            if f.get("form") not in PERIODIC or f.get("val") is None:
                continue
            # `filed` is the newest filing carrying the period (its value is
            # the restated one); `first` is when the market first saw it,
            # which is what a valuation history must use.
            rec = {"v": float(f["val"]), "filed": f.get("filed") or "", "form": f.get("form")}
            if f.get("start"):
                k, tgt = (f["start"], f["end"]), dur
            else:
                k, tgt = f["end"], ins
            old = tgt.get(k)
            firsts = [x for x in (rec["filed"], (old or {}).get("first")) if x]
            first = min(firsts) if firsts else ""
            if old is None or rec["filed"] >= old["filed"]:
                tgt[k] = {**rec, "first": first}
            else:
                old["first"] = first
        newest = max([e for _, e in dur] + list(ins), default="")
        per.append((newest, len(dur) + len(ins), dur, ins, unit))
    per.sort(key=lambda x: (x[0], x[1]), reverse=True)
    dur, ins = {}, {}
    # Only candidates in the leading tag's unit are merged: a series that is
    # USD for five years and TWD for the rest is not a series.
    lead_unit = per[0][4] if per else None
    for _, _, d, i, u in per:
        if u != lead_unit:
            continue
        for k, v in d.items():
            dur.setdefault(k, v)
        for k, v in i.items():
            ins.setdefault(k, v)
    return dur, ins, lead_unit


def _quarters(dur: dict, kind: str) -> dict:
    """{end: {start, v, filed, derived}} — reported quarters plus derived ones."""
    q = {}
    for (s, e), r in dur.items():
        if 80 <= _days(s, e) <= 100:
            if e not in q or r["filed"] >= q[e]["filed"]:
                q[e] = {"start": s, "v": r["v"], "filed": r["filed"],
                        "first": r.get("first") or r["filed"], "derived": False}
    # Per-share figures are not derived by subtraction: the year and the nine
    # months can come from filings either side of a stock split, and their
    # difference is then meaningless (Nvidia's derived Q4 came out at -1.0).
    # The caller rebuilds a missing quarter from net income and shares.
    if kind in ("avg", "eps"):
        return q
    by_start = defaultdict(list)
    for (s, e), r in dur.items():
        by_start[s].append((e, r))
    for s, lst in by_start.items():
        lst.sort()
        for (e0, r0), (e1, r1) in zip(lst, lst[1:]):
            span = _days(e0, e1)
            if not 80 <= span <= 100:
                continue
            if any(abs(_days(e1, k)) <= 5 for k in q):
                continue
            q[e1] = {"start": str(_d(e0) + timedelta(days=1)), "v": r1["v"] - r0["v"],
                     "filed": r1["filed"], "first": r1.get("first") or r1["filed"],
                     "derived": True}
    return q


def _annual(dur: dict) -> dict:
    a = {}
    for (s, e), r in dur.items():
        if 350 <= _days(s, e) <= 380:
            if e not in a or r["filed"] >= a[e]["filed"]:
                a[e] = {"start": s, "v": r["v"], "filed": r["filed"],
                        "first": r.get("first") or r["filed"]}
    return a


def _load_facts(cik: int) -> dict:
    raw = _json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{_cik10(cik)}.json")
    facts = raw.get("facts") or {}
    gaap, ifrs = facts.get("us-gaap") or {}, facts.get("ifrs-full") or {}
    use_ifrs = len(ifrs) > len(gaap)
    out = {}
    for key, (kind, names) in CONCEPTS.items():
        ns, nm = (ifrs, IFRS.get(key)) if use_ifrs else (gaap, names)
        if not nm:
            continue
        dur, ins, unit = _collect(ns, nm)
        if kind == "inst":
            if ins:
                out[key] = {"kind": kind, "unit": unit,
                            "i": [{"end": e, **r} for e, r in sorted(ins.items())]}
            continue
        q, a = _quarters(dur, kind), _annual(dur)
        if not q and not a:
            continue
        # Shares have no derived Q4; the fiscal year's average stands in for
        # it, which is what the EPS arithmetic would have used anyway.
        if kind == "avg":
            for e, r in a.items():
                if not any(abs(_days(e, k)) <= 5 for k in q):
                    q[e] = {"start": str(_d(e) - timedelta(days=90)), "v": r["v"],
                            "filed": r["filed"], "first": r.get("first") or r["filed"],
                            "derived": True}
        out[key] = {"kind": kind, "unit": unit,
                    "q": [{"end": e, **r} for e, r in sorted(q.items())],
                    "a": [{"end": e, **r} for e, r in sorted(a.items())]}
    dei = facts.get("dei") or {}
    so = (dei.get("EntityCommonStockSharesOutstanding") or {}).get("units", {}).get("shares") or []
    if so:
        last = max(so, key=lambda f: (f.get("end", ""), f.get("filed", "")))
        out["shares_out"] = {"kind": "inst", "i": [{"end": last["end"], "v": float(last["val"]),
                                                     "filed": last.get("filed", "")}]}
    return {"cik": cik, "name": raw.get("entityName"), "gaap": "ifrs" if use_ifrs else "us-gaap",
            "series": out}


def facts(cik: int) -> dict:
    return store.cache_through(f"sec:facts:{cik}", 12 * 3600, lambda: _load_facts(cik))


# --------------------------------------------------------------------------
# the business mix: dimensional facts from individual filings
# --------------------------------------------------------------------------

REV_TAGS = {"RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "SalesRevenueNet",
            "RevenuesNetOfInterestExpense"}
OI_TAGS = {"OperatingIncomeLoss", "SegmentReportingInformationOperatingIncomeLoss"}
AXIS_KIND = [
    (re.compile(r"StatementBusinessSegmentsAxis$"), "segment"),
    (re.compile(r"ProductOrServiceAxis$"), "product"),
    (re.compile(r"StatementGeographicalAxis$"), "geography"),
    (re.compile(r"MajorCustomersAxis$"), "customer"),
    (re.compile(r"Segment", re.I), "segment"),
    (re.compile(r"Product|Categorization|Service", re.I), "other"),
]
# Extra dimensions that do not change what a fact means for our purposes.
BENIGN = {"srt:ConsolidationItemsAxis", "us-gaap:ConsolidationItemsAxis"}

XI = "{http://www.xbrl.org/2003/instance}"
XDI = "{http://xbrl.org/2006/xbrldi}"


# Product vs service is how the income statement splits revenue, not a
# business line — kept apart so it is not ranked against Azure.
REVENUE_TYPE = re.compile(r"^us-gaap:(Product|Service|ServiceOther|License|"
                          r"LicenseAndService|ProductAndService|SubscriptionAndCirculation)Member$")


def _axis_kind(axis: str, member: str = "") -> Optional[str]:
    if member and REVENUE_TYPE.match(member):
        return "type"
    for rx, kind in AXIS_KIND:
        if rx.search(axis):
            return kind
    return None


def _humanize(member: str) -> str:
    m = member.split(":")[-1]
    m = re.sub(r"Member$", "", m)
    m = m.replace("ThreeSixFive", " 365 ")
    m = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", m)
    return re.sub(r"\s+", " ", m).strip()


def _instance_url(cik: int, f: dict) -> Optional[str]:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{f['acc'].replace('-', '')}"
    try:
        idx = _json(base + "/index.json")
    except Exception:
        return None
    names = [i["name"] for i in (idx.get("directory") or {}).get("item", [])]
    pick = next((n for n in names if n.endswith("_htm.xml")), None)
    if not pick:
        pick = next((n for n in names if n.endswith(".xml") and not re.search(
            r"(FilingSummary|MetaLinks|_cal|_def|_lab|_pre)\b|_(cal|def|lab|pre)\.xml$", n)), None)
    return f"{base}/{pick}" if pick else None


def _labels(cik: int, f: dict) -> dict:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{f['acc'].replace('-', '')}"
    try:
        m = _json(base + "/MetaLinks.json")
    except Exception:
        return {}
    out = {}
    for inst in (m.get("instance") or {}).values():
        for tag, v in (inst.get("tag") or {}).items():
            role = ((v.get("lang") or {}).get("en-us") or {}).get("role") or {}
            lab = role.get("terseLabel") or role.get("label")
            if lab:
                out[tag.replace("_", ":", 1)] = re.sub(r"\s*\[(Member|Axis)\]$", "", lab)
    return out


def _parse_dims(cik: int, f: dict) -> dict:
    """Every single-axis revenue / operating-income / backlog fact in one filing."""
    from lxml import etree
    url = _instance_url(cik, f)
    if not url:
        return {"acc": f["acc"], "facts": [], "error": "no XBRL instance in this filing"}
    root = etree.fromstring(_get(url))
    ctx = {}
    for c in root.iter(XI + "context"):
        p = c.find(XI + "period")
        if p is None:
            continue
        st, en, ins = (p.findtext(XI + "startDate"), p.findtext(XI + "endDate"),
                       p.findtext(XI + "instant"))
        dims = [(m.get("dimension"), (m.text or "").strip())
                for m in c.iter(XDI + "explicitMember")]
        typed = c.find(".//" + XDI + "typedMember") is not None
        ctx[c.get("id")] = (st, en or ins, dims, typed)

    labels = _labels(cik, f)
    rows = []
    for el in root:
        if not isinstance(el.tag, str) or not el.get("contextRef"):
            continue
        local = etree.QName(el).localname
        conc = local == "ConcentrationRiskPercentage1"
        rpo = local == "RevenueRemainingPerformanceObligation"
        if not (local in REV_TAGS or local in OI_TAGS or conc or rpo
                or local == "ContractWithCustomerLiability"):
            continue
        c = ctx.get(el.get("contextRef"))
        if not c or c[3]:
            continue
        st, en, dims, _ = c
        try:
            v = float(el.text)
        except (TypeError, ValueError):
            continue
        dims = [d for d in dims if d[0] not in BENIGN]
        if conc:
            rows.append({"t": "conc", "start": st, "end": en, "v": v,
                         "dims": [[a, m, labels.get(m) or _humanize(m)] for a, m in dims]})
            continue
        if not dims:
            if rpo:
                rows.append({"t": "rpo", "end": en, "v": v, "label": "Total"})
            continue
        if len(dims) != 1:
            continue
        axis, member = dims[0]
        kind = _axis_kind(axis, member)
        if not kind:
            continue
        lab = labels.get(member) or _humanize(member)
        if rpo:
            rows.append({"t": "rpo", "end": en, "v": v, "label": lab})
        elif local == "ContractWithCustomerLiability":
            rows.append({"t": "deferred", "end": en, "v": v, "label": lab, "kind": kind})
        else:
            rows.append({"t": "rev" if local in REV_TAGS else "oi", "kind": kind,
                         "axis": axis, "member": member, "label": lab,
                         "axis_label": labels.get(axis) or _humanize(axis),
                         "start": st, "end": en, "v": v, "tag": local})
    return {"acc": f["acc"], "form": f["form"], "date": f["date"], "facts": rows}


def filing_dims(cik: int, f: dict) -> dict:
    # A filing never changes once accepted, so its parse is kept for a month;
    # only a new build (a changed parser) re-reads it sooner.
    return store.cache_through(f"sec:dims:{f['acc']}", 30 * 86400, lambda: _parse_dims(cik, f))


def business_mix(cik: int, n_filings: int = 7) -> dict:
    """
    Revenue and operating income by segment, product line and geography, as
    quarterly and annual series built from the last few periodic filings;
    backlog (remaining performance obligations) and customer concentration.
    """
    sub = submissions(cik)
    periodic = [f for f in sub.get("filings") or [] if f["form"] in PERIODIC
                and not f["form"].endswith("/A")][:n_filings]
    if not periodic:
        return {"error": "no 10-K or 10-Q on file with the SEC"}

    series = {}          # (kind, member, t) -> {(start,end): (v, filed)}
    meta = {}            # (kind, member) -> {label, axis_label}
    rpo, deferred, conc, parsed = {}, {}, {}, []
    for f in periodic:
        try:
            d = filing_dims(cik, f)
        except Exception as e:
            parsed.append({"form": f["form"], "date": f["date"], "error": str(e)[:120]})
            continue
        parsed.append({"form": f["form"], "date": f["date"], "facts": len(d.get("facts") or []),
                       "url": filing_url(cik, f["acc"], f["doc"])})
        for r in d.get("facts") or []:
            if r["t"] in ("rev", "oi"):
                k = (r["kind"], r["member"], r["t"])
                meta.setdefault((r["kind"], r["member"]),
                                {"label": r["label"], "axis": r["axis_label"]})
                per = series.setdefault(k, {})
                pk = (r["start"], r["end"])
                if pk not in per or f["date"] > per[pk][1]:
                    per[pk] = (r["v"], f["date"])
            elif r["t"] == "rpo":
                key = (r["label"], r["end"])
                if key not in rpo or f["date"] > rpo[key][1]:
                    rpo[key] = (r["v"], f["date"])
            elif r["t"] == "deferred":
                key = (r["label"], r["end"])
                deferred.setdefault(key, r["v"])
            elif r["t"] == "conc":
                key = (tuple((a, m) for a, m, _ in r["dims"]), r["end"])
                conc.setdefault(key, {"end": r["end"], "start": r.get("start"), "pct": r["v"],
                                      "dims": [[a.split(":")[-1], lab] for a, _, lab in r["dims"]]})

    groups = defaultdict(list)
    for (kind, member), m in meta.items():
        rev = series.get((kind, member, "rev"), {})
        oi = series.get((kind, member, "oi"), {})
        dur = {k: {"v": v, "filed": fd} for k, (v, fd) in rev.items()}
        q = _quarters(dur, "flow")
        a = _annual(dur)
        odur = {k: {"v": v, "filed": fd} for k, (v, fd) in oi.items()}
        oq, oa = _quarters(odur, "flow"), _annual(odur)
        if not q and not a:
            continue
        groups[kind].append({
            "member": member, "label": m["label"], "axis": m["axis"],
            "q": [{"end": e, "v": r["v"], "derived": r["derived"]} for e, r in sorted(q.items())],
            "a": [{"end": e, "v": r["v"]} for e, r in sorted(a.items())],
            "oi_q": [{"end": e, "v": r["v"]} for e, r in sorted(oq.items())],
            "oi_a": [{"end": e, "v": r["v"]} for e, r in sorted(oa.items())],
        })

    rpo_rows = sorted(({"label": l, "end": e, "v": v} for (l, e), (v, _) in rpo.items()),
                      key=lambda r: (r["label"], r["end"]))
    return {
        "source": "SEC XBRL, last %d periodic filings" % len(periodic),
        "filings": parsed,
        "groups": {k: sorted(v, key=lambda m: -((m["a"] or m["q"] or [{"v": 0}])[-1]["v"]))
                   for k, v in groups.items()},
        "rpo": rpo_rows,
        "deferred": [{"label": l, "end": e, "v": v} for (l, e), v in sorted(deferred.items())],
        "concentration": sorted(conc.values(), key=lambda r: r["end"], reverse=True)[:12],
    }


def business(cik: int) -> dict:
    return store.cache_through(f"sec:mix:{cik}", 12 * 3600, lambda: business_mix(cik))
