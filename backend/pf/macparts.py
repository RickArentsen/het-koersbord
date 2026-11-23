"""
What moved inside a release: MAC's panel for one row (build 33).

A headline number is an average of many. CPI is a basket of food, energy,
goods and services; payrolls are jobs added sector by sector; GDP growth is
consumption, investment, trade and government added up. This module reads
the parts from FRED (and the euro area's HICP divisions, FRED's copy of
Eurostat's figures), for the same reference period as the release, and
works out:

    the change of each part   on the month, on the year, or in jobs
    its contribution          weight x change, where a weight is known:
                              CPI's relative importance (BLS, December 2025),
                              the HICP's item weights (Eurostat, 2025, per mille),
                              retail sales and PCE by their share of spending;
                              GDP's contributions are BEA's own, payrolls add up
    the parts over time       two years of the same measure

Contributions from fixed weights are approximate: the agencies weight each
month by relative prices that drift between the annual updates, so the parts
sum to the headline within a few hundredths, not exactly. A residual row
("everything else") closes the gap where the listed parts do not cover the
whole.
"""
from __future__ import annotations

import re
import time
from datetime import date
from typing import Optional

from . import store

# BLS relative importance, CPI-U, U.S. city average, December 2025 (percent).
# Food 13.698 + energy 6.383 + core 79.919 = 100.
CPI_W = {"CPIAUCSL": 100.0, "CPIUFDSL": 13.698, "CUSR0000SAF11": 8.325, "CUSR0000SEFV": 5.373,
         "CPIENGSL": 6.383, "CUSR0000SETB01": 2.895, "CUSR0000SEHF01": 2.489, "CUSR0000SEHF02": 0.773,
         "CPILFESL": 79.919, "CUSR0000SACL1E": 19.176, "CUSR0000SETA01": 3.838, "CUSR0000SETA02": 2.759,
         "CPIAPPSL": 2.368, "CUSR0000SAM1": 1.489, "CUSR0000SASLE": 60.744, "CUSR0000SAH1": 35.625,
         "CUSR0000SEHC": 26.204, "CUSR0000SEHA": 7.840, "CUSR0000SAM2": 6.935, "CUSR0000SAS4": 6.315,
         "CUSR0000SETG01": 0.881}
# Eurostat HICP item weights, euro area, 2025, per mille (sum 1000).
HICP_W = {"CP0100EZ19M086NEST": 155.47, "CP0200EZ19M086NEST": 37.79, "CP0300EZ19M086NEST": 48.14,
          "CP0400EZ19M086NEST": 147.44, "CP0500EZ19M086NEST": 61.02, "CP0600EZ19M086NEST": 52.62,
          "CP0700EZ19M086NEST": 153.87, "CP0800EZ19M086NEST": 25.51, "CP0900EZ19M086NEST": 90.65,
          "CP1000EZ19M086NEST": 10.33, "CP1100EZ19M086NEST": 113.08, "CP1200EZ19M086NEST": 104.08}
WEIGHTS_AS_OF = {"cpi": "BLS relative importance, December 2025", "hicp": "Eurostat item weights, 2025"}

# Each family: its rows (label, FRED id, depth), what it measures, which rows
# partition the headline (for the "what drove it" sentence and the residual),
# and how often it comes out.
FAMILIES = {
    "us_cpi": {
        "name": "US consumer prices (CPI)", "freq": "m", "head": "CPIAUCSL", "measures": ["mom", "yoy", "c_mom", "c_yoy"],
        "weights": "cpi", "sa": True,
        "rows": [("All items", "CPIAUCSL", 0), ("Food", "CPIUFDSL", 1), ("Food at home", "CUSR0000SAF11", 2),
                 ("Eating out", "CUSR0000SEFV", 2), ("Energy", "CPIENGSL", 1), ("Petrol", "CUSR0000SETB01", 2),
                 ("Electricity", "CUSR0000SEHF01", 2), ("Gas (piped)", "CUSR0000SEHF02", 2),
                 ("Core: less food and energy", "CPILFESL", 1), ("Core goods", "CUSR0000SACL1E", 2),
                 ("New cars", "CUSR0000SETA01", 3), ("Used cars", "CUSR0000SETA02", 3), ("Clothing", "CPIAPPSL", 3),
                 ("Medical goods", "CUSR0000SAM1", 3), ("Core services", "CUSR0000SASLE", 2),
                 ("Shelter", "CUSR0000SAH1", 3), ("Owners' equivalent rent", "CUSR0000SEHC", 4), ("Rent", "CUSR0000SEHA", 4),
                 ("Medical services", "CUSR0000SAM2", 3), ("Transport services", "CUSR0000SAS4", 3),
                 ("Airfares", "CUSR0000SETG01", 4)],
        "parts": ["CPIUFDSL", "CPIENGSL", "CUSR0000SACL1E", "CUSR0000SAH1", "CUSR0000SAM2", "CUSR0000SAS4"],
        "rest": "other services", "over": ["CPIAUCSL", "CPIUFDSL", "CPIENGSL", "CUSR0000SACL1E", "CUSR0000SAH1", "CUSR0000SASLE"],
        "over_measure": "yoy",
    },
    "us_jobs": {
        "name": "US jobs (payrolls)", "freq": "m", "head": "PAYEMS", "measures": ["diff", "avg3"],
        "rows": [("Total nonfarm", "PAYEMS", 0), ("Mining and logging", "CES1021000001", 1), ("Construction", "USCONS", 1),
                 ("Manufacturing", "MANEMP", 1), ("Wholesale trade", "USWTRADE", 1), ("Retail trade", "USTRADE", 1),
                 ("Transport and warehousing", "CES4300000001", 1), ("Information", "USINFO", 1),
                 ("Finance and real estate", "USFIRE", 1), ("Professional and business services", "USPBS", 1),
                 ("Education and health", "USEHS", 1), ("Health care", "CES6562000001", 2),
                 ("Leisure and hospitality", "USLAH", 1), ("Government", "USGOVT", 1), ("Federal government", "CES9091000001", 2)],
        "parts": ["CES1021000001", "USCONS", "MANEMP", "USWTRADE", "USTRADE", "CES4300000001", "USINFO", "USFIRE",
                  "USPBS", "USEHS", "USLAH", "USGOVT"],
        "rest": "utilities and other services", "over": ["PAYEMS", "CES6562000001", "USLAH", "USGOVT", "MANEMP", "USPBS"],
        "over_measure": "diff",
        "cards": [("Unemployment rate", "UNRATE", "level", "%"), ("U-6, the broad rate", "U6RATE", "level", "%"),
                  ("Taking part", "CIVPART", "level", "%"), ("Hourly earnings, a year", "CES0500000003", "yoy", "%"),
                  ("Hourly earnings, a month", "CES0500000003", "mom", "%"), ("Weekly hours", "AWHAETP", "level", "h")],
    },
    "us_retail": {
        "name": "US retail sales", "freq": "m", "head": "RSAFS", "measures": ["mom", "c_mom", "yoy"], "weights": "share",
        "rows": [("All retail and food services", "RSAFS", 0), ("Excluding cars and parts", "RSFSXMV", 1, True),
                 ("Cars and parts", "RSMVPD", 1), ("Petrol stations", "RSGASS", 1), ("Online and other non-store", "RSNSR", 1),
                 ("Restaurants and bars", "RSFSDP", 1), ("General merchandise", "RSGMS", 1), ("Food and drink stores", "RSDBS", 1),
                 ("Building and garden", "RSBMGESD", 1), ("Health and personal care", "RSHPCS", 1), ("Clothing", "RSCCAS", 1),
                 ("Furniture", "RSFHFS", 1), ("Electronics and appliances", "RSEAS", 1),
                 ("Sports, hobbies, books", "RSSGHBMS", 1), ("Miscellaneous stores", "RSMSR", 1)],
        "parts": ["RSMVPD", "RSGASS", "RSNSR", "RSFSDP", "RSGMS", "RSDBS", "RSBMGESD", "RSHPCS", "RSCCAS", "RSFHFS",
                  "RSEAS", "RSSGHBMS", "RSMSR"],
        "rest": "the rest", "over": ["RSAFS", "RSNSR", "RSFSDP", "RSMVPD", "RSGASS"], "over_measure": "yoy",
    },
    "us_ppi": {
        "name": "US producer prices (PPI)", "freq": "m", "head": "PPIFIS", "measures": ["mom", "yoy"],
        "rows": [("Final demand", "PPIFIS", 0), ("Goods", "PPIDGS", 1), ("Foods", "PPIDFS", 2), ("Energy", "PPIDES", 2),
                 ("Services", "PPIDSS", 1), ("Less foods and energy", "PPIFES", 1, True)],
        "over": ["PPIFIS", "PPIDGS", "PPIDSS", "PPIDES", "PPIFES"], "over_measure": "yoy",
    },
    "us_pce": {
        "name": "US spending prices (PCE)", "freq": "m", "head": "PCEPI", "measures": ["mom", "yoy", "c_mom", "c_yoy"],
        "weights": "pce",
        "rows": [("PCE prices", "PCEPI", 0), ("Goods", "DGDSRG3M086SBEA", 1), ("Durable goods", "DDURRG3M086SBEA", 2),
                 ("Non-durable goods", "DNDGRG3M086SBEA", 2), ("Services", "DSERRG3M086SBEA", 1),
                 ("Food", "DFXARG3M086SBEA", 1, True), ("Energy", "DNRGRG3M086SBEA", 1, True),
                 ("Core: less food and energy", "PCEPILFE", 1, True)],
        "parts": ["DGDSRG3M086SBEA", "DSERRG3M086SBEA"], "rest": None,
        "over": ["PCEPI", "PCEPILFE", "DGDSRG3M086SBEA", "DSERRG3M086SBEA", "DNRGRG3M086SBEA"], "over_measure": "yoy",
        "extra": ["PCE", "PCEDG", "PCEND", "PCES"],
    },
    "us_gdp": {
        "name": "US growth (GDP)", "freq": "q", "head": "A191RL1Q225SBEA", "measures": ["level"],
        "rows": [("GDP, annualised growth", "A191RL1Q225SBEA", 0), ("Consumption", "DPCERY2Q224SBEA", 1),
                 ("Goods", "DGDSRY2Q224SBEA", 2), ("Services", "DSERRY2Q224SBEA", 2),
                 ("Investment", "A006RY2Q224SBEA", 1), ("Business investment", "A008RY2Q224SBEA", 2),
                 ("Housing", "A011RY2Q224SBEA", 2), ("Inventories", "A014RY2Q224SBEA", 2),
                 ("Net exports", "A019RY2Q224SBEA", 1), ("Exports", "A020RY2Q224SBEA", 2), ("Imports", "A021RY2Q224SBEA", 2),
                 ("Government", "A822RY2Q224SBEA", 1)],
        "parts": ["DPCERY2Q224SBEA", "A006RY2Q224SBEA", "A019RY2Q224SBEA", "A822RY2Q224SBEA"], "rest": None,
        "over": ["A191RL1Q225SBEA", "DPCERY2Q224SBEA", "A006RY2Q224SBEA", "A019RY2Q224SBEA", "A822RY2Q224SBEA"],
        "over_measure": "level", "contrib_rows": True,
    },
    "us_jolts": {
        "name": "US job openings (JOLTS)", "freq": "m2", "head": "JTSJOL", "measures": ["level_m", "diff_m"],
        "rows": [("Job openings", "JTSJOL", 0), ("Construction", "JTS2300JOL", 1), ("Manufacturing", "JTS3000JOL", 1),
                 ("Trade, transport and utilities", "JTS4000JOL", 1), ("Retail", "JTS4400JOL", 2),
                 ("Professional and business services", "JTS540099JOL", 1), ("Health care and social help", "JTS6200JOL", 1),
                 ("Leisure and hospitality", "JTS7000JOL", 1), ("Government", "JTS9000JOL", 1),
                 ("Hires", "JTSHIL", 0, True), ("Quits", "JTSQUL", 0, True), ("Layoffs", "JTSLDL", 0, True)],
        "over": ["JTSJOL", "JTSHIL", "JTSQUL", "JTSLDL"], "over_measure": "level_m",
        "cards": [("Openings per unemployed", "RATIO_OU", "level", "×")], "extra": ["UNEMPLOY"],
    },
    "us_claims": {
        "name": "US jobless claims", "freq": "w", "head": "ICSA", "measures": ["level_k", "diff_k"],
        "rows": [("Initial claims", "ICSA", 0), ("Four-week average", "IC4WSA", 0, True), ("Continuing claims", "CCSA", 0, True)],
        "over": ["ICSA", "IC4WSA"], "over_measure": "level_k",
        "note": "Continuing claims run a week behind the initial ones.",
    },
    "ea_hicp": {
        "name": "Euro-area consumer prices (HICP)", "freq": "m", "head": "CP0000EZ19M086NEST", "measures": ["yoy", "c_yoy", "mom"],
        "weights": "hicp",
        "rows": [("All items", "CP0000EZ19M086NEST", 0), ("Core: less energy, food, alcohol, tobacco", "00XEFDEZ19M086NEST", 1, True),
                 ("Food and soft drinks", "CP0100EZ19M086NEST", 1), ("Alcohol and tobacco", "CP0200EZ19M086NEST", 1),
                 ("Clothing and shoes", "CP0300EZ19M086NEST", 1), ("Housing and energy", "CP0400EZ19M086NEST", 1),
                 ("Furnishings", "CP0500EZ19M086NEST", 1), ("Health", "CP0600EZ19M086NEST", 1),
                 ("Transport, with fuel", "CP0700EZ19M086NEST", 1), ("Communication", "CP0800EZ19M086NEST", 1),
                 ("Recreation and culture", "CP0900EZ19M086NEST", 1), ("Education", "CP1000EZ19M086NEST", 1),
                 ("Restaurants and hotels", "CP1100EZ19M086NEST", 1), ("Other goods and services", "CP1200EZ19M086NEST", 1)],
        "parts": [k for k in HICP_W], "rest": None,
        "over": ["CP0000EZ19M086NEST", "00XEFDEZ19M086NEST", "CP0400EZ19M086NEST", "CP0100EZ19M086NEST", "CP1100EZ19M086NEST"],
        "over_measure": "yoy",
        "note": "Not seasonally adjusted, as Eurostat publishes it: read it on the year. The twelve divisions overlap the energy and food aggregates (energy sits in housing and in transport).",
    },
    "fed": {
        "name": "The Fed's target", "freq": "d", "head": "DFEDTARU", "measures": ["level"],
        "rows": [("Upper bound", "DFEDTARU", 0), ("Lower bound", "DFEDTARL", 0, True), ("Effective rate", "DFF", 0, True)],
        "over": ["DFEDTARU", "DFF"], "over_measure": "level",
    },
}

MEASURES = {"mom": ("On the month", "%"), "yoy": ("On the year", "%"), "c_mom": ("Its share of the month", "pp"),
            "c_yoy": ("Its share of the year", "pp"), "diff": ("Jobs added", "K"), "avg3": ("A month, last three", "K"),
            "level": ("Contribution", "pp"), "level_m": ("Level", "M"), "diff_m": ("On the month", "M"),
            "level_k": ("Level", "K"), "diff_k": ("On the week", "K")}

FAMILY_OF = [  # (currency, title pattern, family, but not when this matches)
    ("USD", r"^(core )?cpi\b", "us_cpi", None),
    # ADP's count is a private survey with sectors of its own, not BLS's payrolls
    ("USD", r"non-farm|nonfarm|unemployment rate|average hourly earnings|participation", "us_jobs", r"\badp\b"),
    ("USD", r"retail sales", "us_retail", None),
    ("USD", r"^(core )?ppi\b", "us_ppi", None),
    ("USD", r"\bpce\b", "us_pce", None),
    # GDP's price index is the deflator, not growth
    ("USD", r"\bgdp\b", "us_gdp", r"price index|deflator"),
    ("USD", r"jolts", "us_jolts", None),
    ("USD", r"claims", "us_claims", None),
    # the euro area's own figures only: "German Prelim CPI" is Germany's
    ("EUR", r"^(final )?(core )?cpi\b|^hicp", "ea_hicp", None),
    ("USD", r"federal funds|fomc statement|fed interest rate", "fed", None),
]


def family_of(ccy: str, title: str) -> Optional[str]:
    t = (title or "").lower()
    for c, rx, fam, no in FAMILY_OF:
        if c == ccy and re.search(rx, t) and not (no and re.search(no, t)):
            return fam
    return None


def default_measure(fam: str, title: str) -> str:
    F = FAMILIES[fam]
    t = (title or "").lower()
    if "y/y" in t and "yoy" in F["measures"]:
        return "yoy"
    if "m/m" in t and "mom" in F["measures"]:
        return "mom"
    return F["measures"][0]


# --------------------------------------------------------------------------
# arithmetic on a series of {date: value}
# --------------------------------------------------------------------------

def _shift(d: str, months: int) -> str:
    y, m = int(d[:4]), int(d[5:7]) + months
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return f"{y:04d}-{m:02d}-01"


def measure(s: dict, d: str, how: str, freq: str) -> Optional[float]:
    """One measure of a series at a period: by date, so a gap never turns eleven months into a year."""
    v = s.get(d)
    if v is None and freq in ("d", "w"):
        # a daily rate published a day late, continuing claims a week behind:
        # the last value on or before the date
        ks = [k for k in s if k <= d]
        if ks:
            d = max(ks)
            v = s[d]
    if v is None:
        return None
    if freq == "w":
        ks = sorted(k for k in s if k <= d)
        prev = s[ks[-2]] if len(ks) > 1 else None
    else:
        step = 3 if freq == "q" else 1
        prev = s.get(_shift(d, -step))
    # JOLTS comes in thousands (shown in millions), claims in persons (shown
    # in thousands), payrolls in thousands (shown as they are)
    scale = 1000.0 if how in ("level_m", "diff_m", "level_k", "diff_k") else 1.0
    if how in ("level", "level_m", "level_k"):
        return v / scale
    if how in ("diff", "diff_m", "diff_k"):
        return (v - prev) / scale if prev is not None else None
    if how == "mom":
        return (v / prev - 1) * 100 if prev else None
    if how == "yoy":
        if freq == "w":
            return None
        p = s.get(_shift(d, -12))
        return (v / p - 1) * 100 if p else None
    if how == "avg3":
        p3 = s.get(_shift(d, -3))
        return (v - p3) / 3 if p3 is not None else None
    return None


def weight_of(fam: str, sid: str, d: str, data: dict) -> Optional[float]:
    """A part's weight in percent of the headline, or None when there is none."""
    F = FAMILIES[fam]
    kind = F.get("weights")
    if kind == "cpi":
        return CPI_W.get(sid)
    if kind == "hicp":
        return HICP_W[sid] / 10.0 if sid in HICP_W else (100.0 if sid == F["head"] else None)
    if kind == "share":
        head = (data.get(F["head"]) or {}).get(_shift(d, -1))
        v = (data.get(sid) or {}).get(_shift(d, -1))
        return v / head * 100 if head and v is not None else None
    if kind == "pce":
        tot = (data.get("PCE") or {}).get(_shift(d, -1))
        part = {"DGDSRG3M086SBEA": ("PCEDG", "PCEND"), "DSERRG3M086SBEA": ("PCES",), "PCEPI": ("PCE",)}.get(sid)
        if not tot or not part:
            return None
        vals = [(data.get(p) or {}).get(_shift(d, -1)) for p in part]
        return sum(vals) / tot * 100 if all(v is not None for v in vals) else None
    return None


# --------------------------------------------------------------------------
# the panel
# --------------------------------------------------------------------------

def _ids(fam: str) -> list[str]:
    F = FAMILIES[fam]
    ids = [r[1] for r in F["rows"]] + [c[1] for c in F.get("cards") or [] if not c[1].startswith("RATIO")] + list(F.get("extra") or [])
    return sorted(set(ids))


def _load(fam: str) -> dict:
    from . import feeds
    ids = _ids(fam)
    days = 1500 if FAMILIES[fam]["freq"] != "q" else 2200
    got = {}
    # ten at a time, as the keyless CSV wants; one unknown id would empty its batch
    for i in range(0, len(ids), 10):
        got.update((feeds.fred(ids[i:i + 10], days=days).get("series") or {}))
    if not got:
        raise RuntimeError("FRED returned none of the parts")
    return {"series": got, "missing": [i for i in ids if i not in got], "fetched_at": time.time()}


def data(fam: str) -> dict:
    return store.cache_through(f"mac:parts:{fam}", 6 * 3600, lambda: _load(fam))


def _label(d: str, freq: str) -> str:
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
              "November", "December"]
    if freq == "q":
        return f"the {['first', 'second', 'third', 'fourth'][(int(d[5:7]) - 1) // 3]} quarter of {d[:4]}"
    if freq == "w":
        return f"the week to {int(d[8:10])} {months[int(d[5:7]) - 1]} {d[:4]}"
    if freq == "d":
        return d
    return f"{months[int(d[5:7]) - 1]} {d[:4]}"


def build(fam: str, ref: Optional[str], ser: dict, title: str = "") -> dict:
    """The panel from the series: pure, so the checks can feed it."""
    F = FAMILIES[fam]
    freq = F["freq"] if F["freq"] != "m2" else "m"
    head = ser.get(F["head"]) or {}
    if not head:
        return {"available": False, "why": f"FRED has no {F['head']} yet"}
    if freq == "d":
        d = max(head)
    else:
        have = sorted(head)
        d = ref if ref in head else None
        if d is None:
            d = max(k for k in have if not ref or k <= ref) if any(not ref or k <= ref for k in have) else have[-1]
    exact = (d == ref) or freq == "d"
    rows = []
    for r in F["rows"]:
        label, sid, depth = r[0], r[1], r[2]
        side = len(r) > 3 and r[3]
        s = ser.get(sid) or {}
        row = {"label": label, "id": sid, "depth": depth, "side": bool(side), "has": bool(s)}
        for m in F["measures"]:
            if m.startswith("c_"):
                w = weight_of(fam, sid, d, ser)
                base = measure(s, d, m[2:], freq) if s else None
                row[m] = round(w / 100 * base, 3) if (w is not None and base is not None) else None
                row["w"] = round(w, 2) if w is not None else None
            elif m == "level" and F.get("contrib_rows"):
                row[m] = round(s[d], 2) if d in s else None
            else:
                v = measure(s, d, m, freq) if s else None
                row[m] = round(v, 3) if v is not None else None
        rows.append(row)
    by = {r["id"]: r for r in rows}

    # the residual: what the listed parts leave of the headline
    for m in F["measures"]:
        if not F.get("parts") or F.get("rest") is None:
            continue
        if not (m.startswith("c_") or m in ("diff", "avg3")):
            continue
        hv = by[F["head"]].get(m) if not m.startswith("c_") else by[F["head"]].get(m[2:])
        parts = [by[p].get(m) for p in F["parts"] if p in by]
        if hv is None or any(v is None for v in parts):
            continue
        rest = next((r for r in rows if r["id"] == "_rest"), None)
        if rest is None:
            rest = {"label": f"Everything else ({F['rest']})", "id": "_rest", "depth": 1, "side": False, "has": True}
            rows.append(rest)
        rest[m] = round(hv - sum(parts), 3)
    rows = [r for r in rows if r["id"] != "_rest" or any(abs(r.get(m) or 0) >= 0.01 for m in F["measures"])]

    say = drivers(fam, by, title)

    # over time
    om = F.get("over_measure", F["measures"][0])
    keys = sorted(head)
    span = keys[-104:] if freq == "w" else (keys[-12:] if freq == "q" else keys[-24:]) if freq != "d" else [k for k in keys if k >= _shift(keys[-1][:7] + "-01", -24)]
    over = []
    for sid in F.get("over") or []:
        s = ser.get(sid) or {}
        if not s:
            continue
        if freq == "d":
            pts = [[k, s[k]] for k in sorted(s) if k >= span[0]][::5] + ([[max(s), s[max(s)]]] if s else [])
        elif F.get("contrib_rows") or om == "level":
            pts = [[k, round(s[k], 2)] for k in span if k in s]
        else:
            pts = [[k, round(v, 3)] for k in span if (v := measure(s, k, om, freq)) is not None]
        over.append({"id": sid, "label": by.get(sid, {}).get("label", sid), "pts": pts})

    cards = []
    for lab, sid, how, unit in F.get("cards") or []:
        prevv = None
        if sid == "RATIO_OU":
            o, u = (ser.get("JTSJOL") or {}).get(d), (ser.get("UNEMPLOY") or {}).get(d)
            v = o / u if o and u else None
        else:
            s = ser.get(sid) or {}
            v = measure(s, d, how, freq) if s else None
            prevv = measure(s, _shift(d, -1), how, freq) if s else None
        if v is None:
            continue
        # a rate reads to one decimal, as the agencies print it; a change to two
        cards.append({"k": lab, "v": round(v, 2), "unit": unit, "prev": round(prevv, 2) if prevv is not None else None,
                      "dec": 1 if how == "level" and unit in ("%", "h") else 2})

    return {"available": True, "family": fam, "name": F["name"], "ref": d, "asked": ref, "exact": exact,
            "period": _label(d, F["freq"] if F["freq"] != "m2" else "m"), "freq": freq,
            "measures": [{"k": m, "label": MEASURES[m][0], "unit": MEASURES[m][1]} for m in F["measures"]],
            "default": default_measure(fam, title), "rows": rows, "say": say,
            "over": {"measure": om, "label": MEASURES[om][0], "unit": MEASURES[om][1], "series": over},
            "cards": cards, "note": F.get("note"), "weights": WEIGHTS_AS_OF.get(F.get("weights") or ""),
            "sa": F.get("sa")}


def drivers(fam: str, by: dict, title: str = "") -> Optional[str]:
    """
    "Shelter added the most (+0.12pp), energy took away the most (−0.05pp), of +0.30%."

    Read off the parts that partition the headline, in the measure the
    release is about: its share of the month for an m/m figure, of the year
    for y/y, jobs for payrolls, percentage points of growth for GDP.
    """
    F = FAMILIES[fam]
    if not F.get("parts"):
        return None
    t = (title or "").lower()
    ms = F["measures"]
    key = ("c_mom" if "m/m" in t and "c_mom" in ms else "c_yoy" if "c_yoy" in ms and ("y/y" in t or "c_mom" not in ms)
           else "c_mom" if "c_mom" in ms else "diff" if "diff" in ms else "level" if "level" in ms else None)
    if not key:
        return None
    pr = [(by[p]["label"], by[p][key]) for p in F["parts"] if p in by and by[p].get(key) is not None]
    head = by.get(F["head"]) or {}
    hv = head.get(key[2:] if key.startswith("c_") else key)
    if not pr or hv is None:
        return None
    if key == "diff":
        f = lambda v: f"{v:+,.0f}K"
        whole = f"{hv:+,.0f}K jobs"
    else:
        f = lambda v: f"{v:+.2f}pp"
        whole = f"{hv:+.1f}% annualised" if key == "level" else f"{hv:+.2f}%"
    up, dn = max(pr, key=lambda x: x[1]), min(pr, key=lambda x: x[1])
    out = f"{up[0]} added the most ({f(up[1])})" if up[1] > 0 else f"Nothing added: the best was {up[0].lower()} ({f(up[1])})"
    if dn[1] < 0 and dn[0] != up[0]:
        out += f"; {dn[0].lower()} took away the most ({f(dn[1])})"
    return out + f", of {whole}."


def detail(ccy: str, title: str, d: Optional[str]) -> dict:
    fam = family_of(ccy, title)
    if not fam:
        return {"available": False, "why": "No breakdown for this release yet."}
    from . import macro
    F = FAMILIES[fam]
    ref = None
    if d and F["freq"] != "d":
        try:
            ref = macro.reference(F["freq"], date.fromisoformat(d))
        except Exception:
            ref = None
    got = data(fam)
    out = build(fam, ref, got.get("series") or {}, title)
    out["missing"] = got.get("missing") or []
    out["title"] = title
    return out
