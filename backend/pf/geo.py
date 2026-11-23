"""
GEO — geopolitics, measured rather than narrated.

A geopolitics page inside a finance terminal should not be a news digest;
NEWS already is one. What belongs here is the two things a news site cannot
tell you:

    WHAT IS THIS WORTH TO ME?   Every risk is mapped onto the actual positions
                                it touches, in euros. "Taiwan escalation" is an
                                abstraction; "EUR 2,000 of your book, 10%, is
                                in names that depend on one island's fabs" is
                                a fact you can act on.

    WHAT IS THE MARKET PRICING? Commentary is cheap and lagging. Brent's term
                                structure, gold against real rates, the Swiss
                                franc, freight rates and defence-sector
                                relative strength are what people are actually
                                paying for, right now.

The RISK REGISTER below is hand-written and deliberately STRUCTURAL: it names
durable flashpoints and the channel through which each one reaches a portfolio.
It does not assert what happened this week — the news feed attaches itself to
each risk by keyword and supplies that. A hand-kept file that tried to track
current events would be wrong within days; one that describes mechanisms stays
true for years.
"""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta
from typing import Optional

from . import store

# --------------------------------------------------------------------------
# the register
# --------------------------------------------------------------------------
#
# tickers   names whose fortunes hang on this specific risk, beyond what
#           their sector or country would suggest
# sectors   broad exposure through the sector classification
# countries ISO-2, used for both the map and country-level exposure
# kw        what a headline about this risk looks like

RISKS = [
    {
        "k": "TAIWAN", "name": "Taiwan Strait", "kind": "conflict",
        "countries": ["TW", "CN", "US", "JP", "KR"],
        "exposed": ["TW", "CN"],
        "sectors": ["Technology"],
        "tickers": ["TSM", "NVDA", "AVGO", "AAPL", "QCOM", "AMD", "INTC",
                    "CRDO", "MU", "ARM", "SNDK", "ASML", "8PSG.DE"],
        "channel": "Leading-edge semiconductor fabrication is concentrated on "
                   "one island. Nothing in the supply chain reroutes quickly, "
                   "so disruption is a price and volume shock to everything "
                   "downstream of a chip — which by now is most of the market.",
        "watch": ["TSM", "VIX", "GOLD"],
        "kw": r"taiwan|strait|tsmc|chip (export|controls)|semiconductor export",
    },
    {
        "k": "UKRAINE", "name": "Russia — Ukraine", "kind": "conflict",
        "countries": ["UA", "RU", "PL", "DE", "NL", "LT", "LV", "EE"],
        "exposed": ["UA", "RU", "PL", "LT", "LV", "EE"],
        "sectors": ["Energy", "Basic Materials", "Industrials"],
        "tickers": ["SHELL.AS", "COP", "CVX", "OXY", "TX", "MP"],
        "channel": "European gas and power prices, grain and fertiliser, and "
                   "the defence budgets of every NATO member. The gas channel "
                   "reaches a euro investor through industrial input costs "
                   "long before it reaches the energy names directly.",
        "watch": ["TTF", "BRENT", "GOLD", "EURUSD"],
        "kw": r"russia|ukraine|kyiv|moscow|nato|gazprom|nord stream|drone strike",
    },
    {
        "k": "MIDEAST", "name": "Middle East", "kind": "conflict",
        "countries": ["IL", "IR", "SA", "AE", "QA", "EG", "LB", "YE", "IQ"],
        "exposed": ["IL", "IR", "SA", "AE", "QA", "EG", "LB", "YE", "IQ"],
        "sectors": ["Energy"],
        "tickers": ["COP", "CVX", "OXY", "SHELL.AS", "KMI", "VLO"],
        "channel": "Oil supply and the shipping lanes that carry it. A "
                   "European book feels this as an input-cost and inflation "
                   "shock, which then reaches rates, which then reaches "
                   "everything with a long duration — technology most of all.",
        "watch": ["BRENT", "GOLD", "VIX"],
        "kw": r"israel|iran|gaza|lebanon|houthi|yemen|hormuz|red sea|opec",
    },
    {
        "k": "USCHINA", "name": "US — China technology controls", "kind": "export controls",
        "countries": ["US", "CN", "NL", "TW", "JP", "KR"],
        "exposed": ["CN", "TW"],
        "sectors": ["Technology"],
        "tickers": ["NVDA", "AMD", "ASML", "AVGO", "QCOM", "INTC", "MU",
                    "TSM", "ARM", "APP", "CRWV"],
        "channel": "Export licences on advanced chips and the tools that make "
                   "them, and China's answer in rare earths and critical "
                   "minerals. This is the one risk on this list that has "
                   "already repriced individual names repeatedly.",
        "watch": ["NDX", "COPPER"],
        "kw": r"export control|entity list|chip ban|rare earth|critical mineral|"
              r"tariff.*(china|beijing)|beijing.*(restrict|curb)",
    },
    {
        "k": "TARIFF", "name": "Tariffs and trade policy", "kind": "tariffs",
        "countries": ["US", "CN", "EU", "MX", "CA", "DE", "NL", "VN"],
        "exposed": ["CN", "MX", "CA", "VN", "TW"],
        "sectors": ["Consumer Cyclical", "Industrials", "Technology", "Basic Materials"],
        "tickers": ["AAPL", "TSLA", "WMT", "NKE", "BMW.DE", "TX", "MP", "AMZN"],
        "channel": "Margin, not revenue, is where a tariff lands first. "
                   "Importers with thin margins and long supply chains take it "
                   "worst; the currency usually absorbs part of it, which is "
                   "why the dollar row on MKT matters here.",
        "watch": ["DXY", "EURUSD", "COPPER"],
        "kw": r"tariff|trade war|section 301|import dut|customs|trade deal|wto",
    },
    {
        "k": "SANCTION", "name": "Sanctions regimes", "kind": "sanctions",
        "countries": ["RU", "IR", "CN", "VE", "KP", "US", "EU"],
        "exposed": ["RU", "IR", "CN", "VE", "KP"],
        "sectors": ["Energy", "Financial Services", "Technology"],
        "tickers": ["SHELL.AS", "INGA.AS", "NN.AS", "MA", "PYPL"],
        "channel": "Banks and payment networks carry the compliance cost; "
                   "energy and commodity names carry the volume cost. "
                   "Secondary sanctions are the ones that reach companies "
                   "with no direct exposure at all.",
        "watch": ["BRENT", "GOLD"],
        "kw": r"sanction|ofac|asset freeze|swift|secondary sanction|embargo",
    },
    {
        "k": "ELECTION", "name": "Elections", "kind": "elections",
        "countries": ["US", "FR", "NL", "DE", "BR", "IN", "AR", "IT"],
        "exposed": "SCHEDULED",
        "sectors": [],
        "tickers": [],
        "channel": "Elections move policy risk, not usually earnings. The "
                   "reliable effects are on the currency, on regulated "
                   "sectors, and on the term premium if fiscal policy is "
                   "genuinely in question.",
        "watch": ["EURUSD", "US10Y", "VIX"],
        "kw": r"election|midterm|referendum|coalition|parliament vote|ballot",
    },
    {
        "k": "ENERGY", "name": "European energy security", "kind": "structural",
        "countries": ["NL", "DE", "NO", "QA", "RU", "US"],
        "exposed": ["NL", "DE", "BE", "FR", "IT", "ES", "AT", "PT", "FI", "SE",
                    "DK", "NO", "IE", "LU"],
        "sectors": ["Energy", "Basic Materials", "Industrials", "Utilities"],
        "tickers": ["SHELL.AS", "TX", "BMW.DE", "UNA.AS", "AD.AS"],
        "channel": "TTF gas is a direct input cost for European industry and "
                   "a direct driver of euro-area inflation. For a book listed "
                   "in Amsterdam and Frankfurt this is closer to home than "
                   "anything else on this page.",
        "watch": ["TTF", "BRENT", "EA10Y"],
        "kw": r"ttf|gas price|lng|pipeline|energy crisis|electricity price|opec",
    },
]

BY_KEY = {r["k"]: r for r in RISKS}

# Scheduled votes worth a marker. Deliberately short: only the ones that move
# a currency or a policy rate. Refresh once a year — the page shows the age.
ELECTIONS = [
    {"d": "2026-11-03", "c": "US", "what": "US midterms",
     "why": "control of Congress, and therefore the fiscal path"},
    {"d": "2027-04-18", "c": "FR", "what": "French presidential, first round",
     "why": "the euro's biggest standing political risk premium"},
    {"d": "2027-10-24", "c": "AR", "what": "Argentine general",
     "why": "peso and sovereign debt"},
]
ELECTIONS_AS_OF = "2026-09"

# --------------------------------------------------------------------------
# chokepoints
# --------------------------------------------------------------------------
#
# Positioned as a blend of country centroids rather than by latitude and
# longitude: the map's projection is not documented, and a pin that is roughly
# between Iran and the UAE is right, while a pin computed from a guessed
# projection is confidently wrong.

CHOKEPOINTS = [
    {"k": "HORMUZ", "name": "Strait of Hormuz", "at": {"IR": .5, "AE": .5},
     "flows": "About a fifth of the world's oil, and most Qatari LNG",
     "proxies": ["BRENT", "TTF"], "risk": "MIDEAST",
     "kw": r"hormuz|persian gulf|strait of hormuz|tanker.*(gulf|iran)"},
    {"k": "REDSEA", "name": "Red Sea / Bab el-Mandeb", "at": {"YE": .6, "ER": .4},
     "flows": "Europe–Asia container traffic; the alternative is around Africa",
     "proxies": ["BRENT"], "risk": "MIDEAST",
     "kw": r"red sea|bab el.?mandeb|houthi|suez traffic|shipping divert"},
    {"k": "TAIWAN", "name": "Taiwan Strait", "at": {"TW": .7, "CN": .3},
     "flows": "Most advanced semiconductors, and a large share of container traffic",
     "proxies": ["NDX"], "risk": "TAIWAN",
     "kw": r"taiwan strait|taiwan|median line|pla drill"},
    {"k": "SUEZ", "name": "Suez Canal", "at": {"EG": 1.0},
     "flows": "Roughly a tenth of world trade by volume",
     "proxies": ["BRENT"], "risk": "MIDEAST",
     "kw": r"suez|canal transit|egypt canal"},
    {"k": "MALACCA", "name": "Strait of Malacca", "at": {"MY": .5, "ID": .5},
     "flows": "Most Middle East oil bound for China, Japan and Korea",
     "proxies": ["BRENT"], "risk": "TAIWAN",
     "kw": r"malacca|south china sea|spratly"},
    {"k": "PANAMA", "name": "Panama Canal", "at": {"PA": 1.0},
     "flows": "US east coast to Asia; constrained by drought as much as by politics",
     "proxies": [], "risk": None,
     "kw": r"panama canal|gatun|canal draft"},
    {"k": "BOSPORUS", "name": "Turkish Straits", "at": {"TR": 1.0},
     "flows": "Black Sea grain and Russian crude",
     "proxies": ["BRENT"], "risk": "UKRAINE",
     "kw": r"bosporus|dardanelles|black sea|grain corridor"},
]


def chokepoint_xy(cp: dict, centroids: dict) -> Optional[list]:
    """Blend the named country centroids into one point on the map."""
    num_x = num_y = wsum = 0.0
    for iso, w in cp["at"].items():
        c = centroids.get(iso)
        if not c:
            continue
        num_x += c[0] * w
        num_y += c[1] * w
        wsum += w
    if not wsum:
        return None
    return [round(num_x / wsum, 1), round(num_y / wsum, 1)]


# --------------------------------------------------------------------------
# exposure
# --------------------------------------------------------------------------

# ISO-2 for the country names the dashboard carries, so the map can shade them.
COUNTRY_ISO = {
    "United States": "US", "Netherlands": "NL", "Germany": "DE",
    "United Kingdom": "GB", "Ireland": "IE", "France": "FR",
    "Switzerland": "CH", "Canada": "CA", "Cayman Islands": "KY",
    "Luxembourg": "LU", "Denmark": "DK", "Jersey": "JE", "Belgium": "BE",
    "Spain": "ES", "Italy": "IT", "Sweden": "SE", "Finland": "FI",
    "Norway": "NO", "Austria": "AT", "Portugal": "PT", "Bermuda": "BM",
    "Taiwan": "TW", "Japan": "JP", "China": "CN", "South Korea": "KR",
    "India": "IN", "Brazil": "BR", "Australia": "AU", "Israel": "IL",
    "Mexico": "MX", "Singapore": "SG", "Hong Kong": "HK",
}


def country_exposure(positions: list, on_map: Optional[set] = None) -> dict:
    """
    Your money by country, keyed by ISO-2, for the map.

    `on_map` is the set of ISO codes the map file can actually draw. Cayman,
    Jersey, Bermuda and Singapore are real domiciles and too small to be drawn
    at this scale; a country that cannot be shaded is FLAGGED rather than
    dropped, so the page can say what it is not showing instead of quietly
    losing the money.
    """
    agg: dict[str, dict] = {}
    total = sum(p.get("value_eur") or 0 for p in positions if not p.get("excluded"))
    for p in positions:
        if p.get("excluded") or not (p.get("value_eur") or 0) > 0:
            continue
        name = p.get("country") or ""
        iso = COUNTRY_ISO.get(name) or (name if len(name) == 2 else None)
        if not iso:
            continue
        a = agg.setdefault(iso, {"iso": iso, "name": name, "value_eur": 0.0,
                                 "positions": []})
        a["value_eur"] += p["value_eur"]
        a["positions"].append({"ticker": p.get("ticker") or p.get("name", "")[:10],
                               "value_eur": round(p["value_eur"], 2),
                               "weight": p.get("weight"),
                               "day_pct": p.get("day_pct")})
    for a in agg.values():
        a["value_eur"] = round(a["value_eur"], 2)
        a["pct"] = round(a["value_eur"] / total * 100, 2) if total else 0.0
        a["positions"].sort(key=lambda r: -r["value_eur"])
        a["on_map"] = True if on_map is None else (a["iso"] in on_map)
    return agg


def risk_exposure(positions: list, news_items: Optional[list] = None) -> list:
    """
    Each risk, with the euros it touches and the news that mentions it.

    A position counts ONCE per risk, and the reason is recorded: named
    directly, or reached through its sector or its country. Without that,
    a technology name in the United States would be counted three times
    against the same risk and the total would exceed the portfolio.
    """
    news_items = news_items or []
    total = sum(p.get("value_eur") or 0 for p in positions if not p.get("excluded"))
    out = []

    upcoming = {e["c"] for e in elections() if not e["past"] and e["days"] <= 550}

    for r in RISKS:
        tickers = {t.upper() for t in r["tickers"]}
        sectors = set(r["sectors"])
        # `countries` is where the risk LIVES — it shades the map. Exposure is a
        # narrower question: which country of LISTING actually takes the hit.
        # Without the distinction, "European energy security" reaches every
        # American technology name because the register mentions the US, and
        # the page reports 93% of the book against a risk it barely touches.
        exp = r.get("exposed", r["countries"])
        countries = upcoming if exp == "SCHEDULED" else set(exp)
        hits, value = [], 0.0

        for p in positions:
            if p.get("excluded") or not (p.get("value_eur") or 0) > 0:
                continue
            tk = (p.get("ticker") or "").upper()
            iso = COUNTRY_ISO.get(p.get("country") or "", "")
            why = None
            if tk and tk in tickers:
                why = "named"
            elif sectors and p.get("sector") in sectors:
                why = "sector"
            elif iso and iso in countries:
                why = "country"
            if not why:
                continue
            value += p["value_eur"]
            hits.append({"ticker": tk or (p.get("name") or "")[:10],
                         "name": p.get("name"),
                         "value_eur": round(p["value_eur"], 2),
                         "weight": p.get("weight"),
                         "day_pct": p.get("day_pct"),
                         "why": why})

        hits.sort(key=lambda h: -h["value_eur"])
        by_why = {"named": 0.0, "sector": 0.0, "country": 0.0}
        for h in hits:
            by_why[h["why"]] += h["value_eur"]
        rx = re.compile(r["kw"], re.I)
        matched = [n for n in news_items
                   if rx.search(f"{n.get('headline','')} {n.get('summary','')}")][:5]

        out.append({
            "k": r["k"], "name": r["name"], "kind": r["kind"],
            "channel": r["channel"], "countries": r["countries"],
            "exposed": sorted(countries),
            "watch": r["watch"],
            "value_eur": round(value, 2),
            "pct": round(value / total * 100, 1) if total else 0.0,
            "positions": hits[:12],
            "n_positions": len(hits),
            "named": sum(1 for h in hits if h["why"] == "named"),
            "named_eur": round(by_why["named"], 2),
            "sector_eur": round(by_why["sector"], 2),
            "country_eur": round(by_why["country"], 2),
            "named_pct": round(by_why["named"] / total * 100, 1) if total else 0.0,
            # The full hit list, compactly, so the page can draw a matrix of
            # every position against every risk without a second request.
            "hits": {h["ticker"]: h["why"] for h in hits},
            "news": matched,
            "news_n": len(matched),
        })
    out.sort(key=lambda r: -r["value_eur"])
    return out


def elections(today: Optional[date] = None) -> list:
    today = today or date.today()
    out = []
    for e in ELECTIONS:
        d = date.fromisoformat(e["d"])
        out.append({**e, "days": (d - today).days, "past": d < today})
    out.sort(key=lambda e: e["d"])
    return out


# --------------------------------------------------------------------------
# scenarios
# --------------------------------------------------------------------------

SHOCKS = [
    {"k": "OIL", "factor": "BRENT", "name": "Brent +30%", "move": 0.30,
     "why": "a supply shock out of Hormuz or a wider Middle East war"},
    {"k": "OILDN", "factor": "BRENT", "name": "Brent −20%", "move": -0.20,
     "why": "demand cracking, or OPEC discipline failing"},
    {"k": "USD", "factor": "EURUSD", "name": "EUR/USD −10%", "move": -0.10,
     "why": "a flight to the dollar; your book is mostly dollar-denominated"},
    {"k": "EQ", "factor": "SPX", "name": "S&P −15%", "move": -0.15,
     "why": "a broad risk-off, whatever starts it"},
    {"k": "TECH", "factor": "NDX", "name": "Nasdaq −25%", "move": -0.25,
     "why": "a Taiwan or export-control shock landing on semiconductors"},
]


def shock_table(positions: list, betas: dict) -> list:
    """
    What a move in one factor would do to this book, in euros.

    `betas` is {ticker: {factor: beta}} measured on five-day returns. The result
    is an ESTIMATE and is labelled as one: beta is a linear fit to ordinary
    days, and the scenarios here are explicitly not ordinary days. In a real
    shock correlations converge and the true loss is usually worse than this
    table says. It is still far better than having no number at all.
    """
    out = []
    for s in SHOCKS:
        hit, covered, value = 0.0, 0.0, 0.0
        movers = []
        for p in positions:
            v = p.get("value_eur") or 0
            if p.get("excluded") or v <= 0:
                continue
            value += v
            b = (betas.get((p.get("ticker") or "").upper()) or {}).get(s["factor"])
            if b is None:
                continue
            covered += v
            eur = v * b * s["move"]
            hit += eur
            movers.append({"ticker": p.get("ticker"), "eur": round(eur, 0),
                           "beta": round(b, 2)})
        movers.sort(key=lambda m: m["eur"])
        out.append({
            **s,
            "impact_eur": round(hit, 0),
            "impact_pct": round(hit / value * 100, 2) if value else None,
            "coverage_pct": round(covered / value * 100, 0) if value else 0,
            "worst": movers[:3],
            "best": movers[-3:][::-1],
        })
    return out


def factor_betas(hists: dict, factor_hists: dict, min_obs: int = 60) -> dict:
    """
    Beta of each holding to each scenario factor, on five-day returns: an
    Amsterdam line closes four and a half hours before the S&P 500 it is
    measured against, and one day of each is not the same day (risk.HORIZON).

    hists:        {ticker: {day: close}} for your holdings
    factor_hists: {factor key: {day: close}}
    """
    from .risk import price_windows, ols

    out: dict[str, dict] = {}
    for tk, h in hists.items():
        if not h or len(h) < min_obs:
            continue
        row = {}
        for fk, f in factor_hists.items():
            if not f:
                continue
            fit = ols(*price_windows(h, f))
            if fit:
                row[fk] = round(fit["beta"], 3)
        if row:
            out[tk.upper()] = row
    return out
