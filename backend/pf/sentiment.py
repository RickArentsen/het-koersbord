"""
Sentiment and policy: Fear & Greed, the next Fed move, and measured
geopolitical risk.

Three things Rick reads elsewhere, brought in here — each with an honest
account of how solid it is.

FEAR & GREED    CNN publishes the real number through an endpoint their own
                site uses. It is undocumented and can change without notice,
                so a HOUSE version is computed alongside it from data this app
                already holds. When they agree, that is confirmation; when
                they disagree it is worth knowing; and when CNN's endpoint
                breaks the panel still works.

FED WATCH       CME's FedWatch needs the whole 30-day fed funds futures strip,
                which is a paid feed. What is free is the front contract,
                which gives a defensible probability for the NEXT MEETING
                ONLY. That is what is built, and it says so on the page. A
                fabricated multi-meeting curve would look more impressive and
                be worth less than nothing.

GPR             The Caldara & Iacoviello geopolitical risk index: a count of
                newspaper articles about geopolitical tension, maintained at
                the Federal Reserve Board and published free. A measured
                series rather than a feeling.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import urllib.request
from datetime import date, datetime, timedelta
from typing import Optional

from . import store, net

TIMEOUT = 20
UA = net.BROWSER["User-Agent"]

CNN_FG = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"

# CNN's edge returns 418 to anything that fingerprints as a script, and the
# check is on the TLS handshake, not the headers — which is why sending a
# Chrome user-agent from urllib changed nothing. pf.net tries curl_cffi first,
# which impersonates a real Chrome handshake. These headers are the second
# half: the endpoint is an XHR their own page makes, so it expects to be
# called from it.
CNN_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://edition.cnn.com",
    "Referer": "https://edition.cnn.com/markets/fear-and-greed",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "cross-site",
}

# The GPR index is published as a file on a personal academic page, and files
# on personal academic pages move. Rather than hard-code one URL and break on a
# 404, try the known spellings and report which one answered — the Sources
# panel shows it, so a future move is a one-line change with evidence.
# Checked against the publisher's own download page: the daily series is
# offered as ONE file, an .xls. There is no CSV — which is what the 404 was
# really saying. The monthly export is kept as a last resort so the panel
# degrades to a coarser series rather than to nothing.
GPR_URLS = [
    "https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls",
    "https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xlsx",
    "https://www.matteoiacoviello.com/gpr_files/data_gpr_export.xls",
]


def _get(url: str, headers: Optional[dict] = None) -> bytes:
    """Through pf.net: certifi's CA bundle, and a browser TLS fingerprint."""
    return net.get(url, headers, timeout=TIMEOUT)


# --------------------------------------------------------------------------
# Fear & Greed
# --------------------------------------------------------------------------

def band(score: Optional[float]) -> str:
    if score is None:
        return "unknown"
    if score < 25:
        return "extreme fear"
    if score < 45:
        return "fear"
    if score <= 55:
        return "neutral"
    if score <= 75:
        return "greed"
    return "extreme greed"


def cnn_fear_greed() -> dict:
    """
    CNN's own number. Undocumented endpoint — failure is reported, not hidden.

    Two URLs, because the bare endpoint is the one their edge rate-limits
    hardest; the dated form is the call their page actually makes on load and
    tends to be answered when the bare one is not.
    """
    start = (date.today() - timedelta(days=200)).isoformat()
    r = net.first_ok([CNN_FG, f"{CNN_FG}/{start}"], CNN_HEADERS, TIMEOUT,
                     accept=lambda b: b[:1] in (b"{", b"["))
    if not r["ok"]:
        tried = r.get("tried") or []
        detail = "; ".join(f"{t['url'].rsplit('/', 1)[-1] or 'graphdata'} "
                           f"{t.get('error') or 'ok'}" for t in tried)[:240]
        return {"ok": False, "error": f"CNN refused every attempt — {detail}",
                "tried": tried, "fetched_at": time.time()}
    try:
        d = json.loads(r["body"].decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": f"CNN answered but the JSON did not parse: {e}",
                "transport": r.get("transport"), "fetched_at": time.time()}

    fg = d.get("fear_and_greed") or {}
    hist = [{"d": datetime.utcfromtimestamp(x["x"] / 1000).date().isoformat(),
             "v": round(x["y"], 1)}
            for x in (d.get("fear_and_greed_historical") or {}).get("data", [])
            if isinstance(x, dict) and x.get("x") and x.get("y") is not None][-180:]

    comps = []
    for key, label in (
        ("market_momentum_sp125", "Momentum vs 125-day average"),
        ("stock_price_strength", "52-week highs vs lows"),
        ("stock_price_breadth", "Volume breadth"),
        ("put_call_options", "Put / call ratio"),
        ("market_volatility_vix", "Volatility"),
        ("safe_haven_demand", "Safe-haven demand"),
        ("junk_bond_demand", "Junk-bond demand"),
    ):
        c = d.get(key)
        if isinstance(c, dict) and c.get("score") is not None:
            comps.append({"k": key, "name": label,
                          "score": round(float(c["score"]), 1),
                          "rating": c.get("rating")})

    return {
        "ok": fg.get("score") is not None,
        "score": round(float(fg["score"]), 1) if fg.get("score") is not None else None,
        "rating": fg.get("rating") or band(fg.get("score")),
        "prev_close": fg.get("previous_close"),
        "week": fg.get("previous_1_week"),
        "month": fg.get("previous_1_month"),
        "year": fg.get("previous_1_year"),
        "timestamp": fg.get("timestamp"),
        "history": hist,
        "components": comps,
        "error": None if fg.get("score") is not None else "no score in the response",
        "transport": r.get("transport"), "url": r.get("url"),
        "fetched_at": time.time(),
    }


def house_fear_greed(hists: dict, rates: dict, extra: Optional[list] = None) -> dict:
    """
    The same idea, computed from what this app already holds.

    Five of CNN's seven components are rebuilt here from prices and rates.
    The other two come from FLOW since build 51 (`extra`, pf/flow.py): Cboe's
    put/call ratio, and volume breadth from the S&P 500 members' up and down
    volume where CNN uses the NYSE's. Either can be missing while FLOW's
    record is still being collected, and the list says which. Each component
    is scored 0–100 where 50 is neutral, which is the same convention CNN uses.
    """
    from .risk import _mean, _sd

    def series(k):
        return hists.get(k) or {}

    def last(h):
        return h[max(h)] if h else None

    comps = []

    def add(name, score, detail):
        if score is None:
            return
        comps.append({"name": name, "score": round(max(0, min(100, score)), 1),
                      "detail": detail})

    # Momentum: the S&P against its own 125-day average, scaled so ±5% spans
    # most of the range — the same idea CNN uses.
    spx = series("SPX")
    if len(spx) > 130:
        days = sorted(spx)
        ma = _mean([spx[d] for d in days[-125:]])
        cur = spx[days[-1]]
        gap = (cur / ma - 1) if ma else 0
        add("Momentum vs 125-day average", 50 + gap * 1000,
            f"{gap*100:+.1f}% against the average")

    # Volatility: VIX against its 50-day average, INVERTED — high volatility
    # is fear, and fear is a low score.
    vix = series("VIX")
    if len(vix) > 55:
        days = sorted(vix)
        ma = _mean([vix[d] for d in days[-50:]])
        cur = vix[days[-1]]
        gap = (cur / ma - 1) if ma else 0
        add("Volatility", 50 - gap * 180,
            f"VIX {cur:.1f} against a 50-day average of {ma:.1f}")

    # Safe-haven demand: 20-day equity return minus 20-day bond return. Gold
    # stands in for bonds here, which is not identical but moves for the same
    # reason on the days this matters.
    gold = series("GOLD")
    if len(spx) > 25 and len(gold) > 25:
        sd_, gd = sorted(spx), sorted(gold)
        sr = spx[sd_[-1]] / spx[sd_[-21]] - 1
        gr = gold[gd[-1]] / gold[gd[-21]] - 1
        add("Safe-haven demand", 50 + (sr - gr) * 400,
            f"equities {sr*100:+.1f}% vs gold {gr*100:+.1f}% over 20 sessions")

    # Junk-bond demand: the high-yield spread against its own year.
    hy = (rates or {}).get("BAMLH0A0HYM2") or {}
    if len(hy) > 60:
        days = sorted(hy)
        cur = hy[days[-1]]
        yr = [hy[d] for d in days[-250:]]
        lo, hi = min(yr), max(yr)
        pos = (cur - lo) / (hi - lo) if hi > lo else 0.5
        add("Junk-bond demand", (1 - pos) * 100,
            f"high-yield spread {cur:.2f}%, {pos*100:.0f}% of its year")

    # Price strength: where the index sits in its own 52-week range.
    if len(spx) > 200:
        days = sorted(spx)
        yr = [spx[d] for d in days[-250:]]
        lo, hi = min(yr), max(yr)
        cur = spx[days[-1]]
        pos = (cur - lo) / (hi - lo) if hi > lo else 0.5
        add("Price strength", pos * 100,
            f"{pos*100:.0f}% of the 52-week range")

    for c in extra or []:
        if c.get("score") is not None:
            add(c["name"], c["score"], c.get("detail") or "")

    score = round(_mean([c["score"] for c in comps]), 1) if comps else None
    have = {c["name"] for c in comps}
    missing = [m for m in ("Put / call ratio", "Volume breadth") if m not in have]
    return {
        "ok": score is not None,
        "score": score,
        "rating": band(score),
        "components": comps,
        "n": len(comps),
        "missing": missing,
        "note": f"{len(comps)} of CNN's seven components"
                + (f"; {' and '.join(missing).lower()} not yet: FLOW is still collecting their record" if missing else
                   "; put/call from Cboe and volume breadth from the S&P 500's members (FLOW)"),
        "fetched_at": time.time(),
    }


# --------------------------------------------------------------------------
# central-bank meetings (the arithmetic on them lives in ratepath.py)
# --------------------------------------------------------------------------

# Decision days: the second day of each FOMC meeting, and the ECB's "Day 2".
# Both calendars are published about two years ahead (federalreserve.gov/
# monetarypolicy/fomccalendars.htm; ecb.europa.eu/press/calendars/mgcgc) and
# every date is tentative until the meeting before it. The page shows how old
# this list is rather than quietly running off the end of it.
#
# The list this replaced was written from memory and had six of sixteen dates
# wrong — it put the October 2026 meeting on 4 November — so for a fortnight
# the next-move panel priced the wrong month. Copy them from the source.
FOMC = ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29",
        "2026-09-16", "2026-10-28", "2026-12-09",
        "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28",
        "2027-09-15", "2027-10-27", "2027-12-08",
        "2028-01-26"]
ECB_MEETINGS = ["2026-02-05", "2026-03-19", "2026-04-30", "2026-06-11",
                "2026-07-23", "2026-09-10", "2026-10-29", "2026-12-17",
                "2027-02-04", "2027-03-18", "2027-04-29", "2027-06-10",
                "2027-07-22", "2027-09-09", "2027-10-28", "2027-12-16"]
MEETINGS_AS_OF = "2026-10"


def next_meeting(dates: list, today: Optional[date] = None) -> Optional[dict]:
    today = today or date.today()
    for d in dates:
        dd = date.fromisoformat(d)
        if dd >= today:
            return {"date": d, "days": (dd - today).days}
    return None


# --------------------------------------------------------------------------
# geopolitical risk index
# --------------------------------------------------------------------------

# CME delivery-month codes. A 30-day fed funds future is quoted per month, and
# Yahoo carries the dated contracts as e.g. ZQX26.CBT (November 2026).
MONTH_CODES = "FGHJKMNQUVXZ"


def zq_symbol(d: date) -> str:
    return f"ZQ{MONTH_CODES[d.month - 1]}{d.year % 100:02d}.CBT"


def fed_funds_contract(month: date) -> dict:
    """
    The 30-day fed funds future covering a SPECIFIC month.

    The first version used "ZQ=F", the continuous FRONT contract, and then did
    the meeting-month arithmetic on it. Those are different months for most of
    the year: on 17 September, "ZQ=F" is the September contract while the next
    meeting is in November. Dividing one month's price by another month's
    calendar produced a confident 100% probability of a hike out of nothing.
    Ask for the month you actually mean.
    """
    from . import pricing
    sym = zq_symbol(month)
    tried = []

    for s_, how in ((sym, "quote"), ("ZQ=F", "front-month quote")):
        if how.startswith("front") and month.month != date.today().month:
            # Only legitimate when the front contract IS the meeting's month.
            tried.append({"symbol": s_, "skipped": "not the meeting's month"})
            continue
        try:
            q = pricing.get_quotes([s_], fresh=True)
            px = (q.get(s_) or {}).get("price")
            tried.append({"symbol": s_, "price": px})
            if px:
                return {"ok": True, "price": float(px), "symbol": s_,
                        "month": month.strftime("%Y-%m"), "tried": tried}
        except Exception as e:
            tried.append({"symbol": s_, "error": f"{e.__class__.__name__}: {e}"[:120]})

    # A quote can be missing while the daily bars are there — different Yahoo
    # endpoints, and the futures one is the flakier of the two.
    try:
        import yfinance as yf
        h = yf.Ticker(sym).history(period="10d")
        if h is not None and len(h):
            px = float(h["Close"].dropna().iloc[-1])
            tried.append({"symbol": sym + " history", "price": px})
            return {"ok": True, "price": px, "symbol": sym + " (daily bar)",
                    "month": month.strftime("%Y-%m"), "tried": tried}
    except Exception as e:
        tried.append({"symbol": sym + " history",
                      "error": f"{e.__class__.__name__}: {e}"[:120]})

    return {"ok": False, "tried": tried, "month": month.strftime("%Y-%m"),
            "error": f"Yahoo returned no price for {sym}, the fed funds "
                     f"contract covering {month.strftime('%B %Y')}"}


def front_fed_funds() -> dict:
    """The contract covering the next FOMC meeting's month."""
    nxt = next_meeting(FOMC)
    if not nxt:
        return {"ok": False, "error": "the FOMC list has run out — it needs "
                                      f"refreshing (last updated {MEETINGS_AS_OF})"}
    return fed_funds_contract(date.fromisoformat(nxt["date"]))


def _gpr_table(body: bytes) -> dict:
    """
    The GPR file as a list of (ISO date, value) pairs, whatever shape it is in.

    The publisher offers exactly one daily file and it is an .xls — there is no
    CSV, which is what the 404 was really telling us. Reading it is fiddlier
    than it sounds: pandas takes the first row as the header, and when that row
    holds a date the column NAME comes back as a `Timestamp`. The first version
    called `.strip()` on it, raised AttributeError, and took the whole sentiment
    payload down with it — which is why CNN and the Fed panel went on showing
    errors from the previous build for hours afterwards. So: normalise every key
    to a string, FIND the header row rather than assuming it, and fall back to
    positional columns if there is no header at all.
    """
    import pandas as pd

    excel = body[:2] in (b"PK", b"\xd0\xcf")

    def read(**kw):
        buf = io.BytesIO(body)
        return pd.read_excel(buf, **kw) if excel else pd.read_csv(buf, **kw)

    def norm(c):
        return str(c).strip().lower()

    df = read()
    if not any(norm(c).startswith("gpr") for c in df.columns):
        raw = read(header=None)
        hdr = None
        for i in range(min(12, len(raw))):
            if any(norm(v).startswith("gpr") for v in raw.iloc[i].tolist()):
                hdr = i
                break
        df = read(header=hdr) if hdr is not None else raw
        if hdr is None:
            df.columns = ["date"] + [f"c{j}" for j in range(1, df.shape[1])]

    df.columns = [str(c).strip() for c in df.columns]
    low = {c.lower(): c for c in df.columns}

    dcol = next((low[c] for c in ("date", "day", "dates") if c in low), df.columns[0])
    vcol = next((low[c] for c in ("gprd", "gpr", "gprd_ma7", "gprd_ma30") if c in low), None)
    if vcol is None:
        for c in df.columns[1:]:
            if pd.api.types.is_numeric_dtype(df[c]):
                vcol = c
                break
    if vcol is None:
        return {"rows": [], "dcol": str(dcol), "vcol": None,
                "columns": [str(c) for c in df.columns[:8]],
                "format": "excel" if excel else "csv"}

    dates = pd.to_datetime(df[dcol], errors="coerce")
    vals = pd.to_numeric(df[vcol], errors="coerce")
    rows = [(d.date().isoformat(), float(v))
            for d, v in zip(dates, vals)
            if d is not pd.NaT and d == d and v == v]
    return {"rows": rows, "dcol": str(dcol), "vcol": str(vcol),
            "columns": [str(c) for c in df.columns[:8]],
            "format": "excel" if excel else "csv"}


def gpr() -> dict:
    """
    The Caldara & Iacoviello index. Daily, free, and a real measurement rather
    than a feeling: it counts newspaper articles about geopolitical tension.

    Never raises. A source that can take down the panel it feeds is worse than
    a source that is simply missing.
    """
    r = net.first_ok(GPR_URLS, {"Accept": "application/vnd.ms-excel,text/csv,*/*"},
                     TIMEOUT)
    if not r["ok"]:
        tried = r.get("tried") or []
        return {"ok": False, "tried": tried,
                "error": "no GPR file found — tried " + ", ".join(
                    f"{t['url'].rsplit('/', 1)[-1]} ({t.get('error') or 'ok'})"
                    for t in tried)[:300],
                "fetched_at": time.time()}

    try:
        tab = _gpr_table(r["body"])
    except ImportError as e:
        return {"ok": False, "url": r["url"],
                "error": f"the GPR file is a spreadsheet and this install cannot "
                         f"read one ({e}). Run  pip install xlrd openpyxl  in "
                         f"backend/.venv and press Refresh.",
                "fetched_at": time.time()}
    except Exception as e:
        return {"ok": False, "url": r["url"],
                "error": f"the GPR file downloaded but would not parse — "
                         f"{e.__class__.__name__}: {e}", "fetched_at": time.time()}

    if not tab["rows"]:
        return {"ok": False, "url": r["url"],
                "error": f"no usable observations; the file's columns are "
                         f"{tab['columns']}", "fetched_at": time.time()}

    series = dict(tab["rows"])
    days = sorted(series)
    cur = series[days[-1]]
    yr = [series[d] for d in days[-250:]]
    pctile = round(sum(1 for v in yr if v <= cur) / len(yr) * 100)
    return {
        "ok": True, "value": round(cur, 1), "asof": days[-1],
        "pctile": pctile,
        "mean": round(sum(yr) / len(yr), 1),
        "history": [{"d": d, "v": round(series[d], 1)} for d in days[-180:]],
        "column": tab["vcol"], "url": r["url"], "format": tab["format"],
        "transport": r.get("transport"), "obs": len(series),
        "error": None, "fetched_at": time.time(),
    }
