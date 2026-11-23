"""
The two rate sources Yahoo cannot provide.

Yahoo carries 13-week, 5-year, 10-year and 30-year Treasuries and nothing
else useful for rates: no 2-year, so no 10y-2y curve, no policy rates, no
credit spreads, no Bund. So:

  FRED   The St. Louis Fed's series database. US 2y and 10y, the curve
         spread, effective Fed funds, and ICE BofA option-adjusted credit
         spreads. A free key, pasted into Settings. There is also a keyless
         CSV download which has worked for years; it is tried first and the
         key is used if it fails, so a key that expires degrades rather than
         breaks.

  ECB    The ECB Data Portal. The deposit facility rate and the euro-area
         AAA government yield curve, which stands in for the Bund. No key at
         all.

Both are DAILY series, published after the close. A credit spread on this
page is yesterday's, and the page says so rather than implying it is live.

Everything here is blocking and goes over the network, so it runs on the
background thread and writes to the cache. Nothing calls it from a request.
"""

from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
import urllib.request
from datetime import date, timedelta
from typing import Optional

from . import store, net

TIMEOUT = 20
UA = net.BROWSER["User-Agent"]

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
FRED_API = "https://api.stlouisfed.org/fred/series/observations"
ECB_API = "https://data-api.ecb.europa.eu/service/data"

# What we pull, and what each one is for.
FRED_SERIES = {
    "DGS2":          "US 2-year Treasury",
    "DGS10":         "US 10-year Treasury",
    "DGS30":         "US 30-year Treasury",
    "T10YIE":        "US 10-year breakeven inflation",
    "T10Y2Y":        "US 10y minus 2y",
    "DFF":           "Fed funds, effective",
    "EFFR":          "Fed funds, effective (NY Fed)",
    "BAMLH0A0HYM2":  "US high yield OAS",
    "BAMLC0A0CM":    "US investment grade OAS",
    "BAMLHE00EHYIOAS": "Euro high yield OAS",
}

# ECB Data Portal series keys.
#   FM/D.U2.EUR.4F.KR.DFR.LEV        deposit facility rate, daily
#   YC/B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y   AAA euro-area 10y spot rate
ECB_SERIES = {
    "ECB_DFR": ("FM", "D.U2.EUR.4F.KR.DFR.LEV", "ECB deposit facility rate"),
    "EA_10Y":  ("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y", "Euro-area AAA 10-year"),
}


def fred_key() -> Optional[str]:
    k = (store.get_setting("fred_api_key") or "").strip()
    return k or None


def _get(url: str) -> bytes:
    """
    Every outbound call in this module goes through pf.net, which carries
    certifi's CA bundle. Python on Windows does not populate OpenSSL's trust
    store, so the plain-urllib version of this function raised
    CERTIFICATE_VERIFY_FAILED against both FRED and the ECB while yfinance —
    which ships certifi — worked in the same process.
    """
    return net.get(url, timeout=TIMEOUT)


# --------------------------------------------------------------------------
# FRED
# --------------------------------------------------------------------------

def _parse_fred_csv(raw: bytes) -> dict[str, dict[str, float]]:
    """
    FRED's CSV puts one column per series and writes missing values as '.'.
    A '.' is a bank holiday, not a zero, and treating it as one would put a
    0.00% print in the middle of a yield series.
    """
    text = raw.decode("utf-8", "replace")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return {}
    head = [h.strip() for h in rows[0]]
    out: dict[str, dict[str, float]] = {h: {} for h in head[1:]}
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        d = r[0].strip()[:10]
        for i, h in enumerate(head[1:], start=1):
            if i >= len(r):
                continue
            v = (r[i] or "").strip()
            if v in ("", ".", "NA"):
                continue
            try:
                out[h][d] = float(v)
            except ValueError:
                continue
    return {k: v for k, v in out.items() if v}


def fred(series: Optional[list[str]] = None, days: int = 500) -> dict:
    """
    Daily observations per series. Tries the keyless CSV first; falls back to
    the keyed JSON API. Returns {"series": {...}, "source": ..., "error": ...}.
    """
    ids = series or list(FRED_SERIES)
    # The keyless CSV answers a list of more than twelve ids with the first
    # twelve and no error at all, so longer lists go in batches of ten.
    if len(ids) > 10:
        parts = [fred(ids[i:i + 10], days) for i in range(0, len(ids), 10)]
        got = {k: v for p in parts for k, v in (p.get("series") or {}).items()}
        errs = [p["error"] for p in parts if p.get("error")]
        return {"series": got, "source": next((p["source"] for p in parts if p.get("source")), None),
                "error": None if got else (errs[0] if errs else "no data"),
                "fetched_at": time.time()}
    start = (date.today() - timedelta(days=days)).isoformat()
    err = None

    try:
        raw = _get(f"{FRED_CSV}?" + urllib.parse.urlencode(
            {"id": ",".join(ids), "cosd": start}))
        got = _parse_fred_csv(raw)
        if got:
            return {"series": got, "source": "fred-csv", "error": None,
                    "fetched_at": time.time()}
        err = "the CSV endpoint returned no rows"
    except Exception as e:
        err = f"{e.__class__.__name__}: {e}"

    key = fred_key()
    if not key:
        return {"series": {}, "source": None, "fetched_at": time.time(),
                "error": f"{err} — and no FRED key is set (Settings)"}

    out: dict[str, dict[str, float]] = {}
    for sid in ids:
        try:
            raw = _get(f"{FRED_API}?" + urllib.parse.urlencode({
                "series_id": sid, "api_key": key, "file_type": "json",
                "observation_start": start}))
            obs = json.loads(raw.decode("utf-8", "replace")).get("observations") or []
            vals = {}
            for o in obs:
                v = (o.get("value") or "").strip()
                if v in ("", ".", "NA"):
                    continue
                try:
                    vals[o["date"][:10]] = float(v)
                except (ValueError, KeyError):
                    continue
            if vals:
                out[sid] = vals
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"
    return {"series": out, "source": "fred-api" if out else None,
            "error": None if out else err, "fetched_at": time.time()}


# --------------------------------------------------------------------------
# ECB
# --------------------------------------------------------------------------

# The deposit rate has one obvious key. The yield curve has had more than one
# published spelling over the years, so both are tried and the page is told
# which answered — better than one 404 silently emptying the Rates section.
ECB_FALLBACKS = {
    "EA_10Y": [("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.SR_10Y"),
               ("YC", "B.U2.EUR.4F.G_N_A.SV_C_YM.PY_10Y"),
               ("YC", "B.U2.EUR.4F.G_N_C.SV_C_YM.SR_10Y")],
    "ECB_DFR": [("FM", "D.U2.EUR.4F.KR.DFR.LEV")],
}


def _parse_ecb_csv(raw: bytes) -> dict[str, float]:
    text = raw.decode("utf-8", "replace")
    out: dict[str, float] = {}
    for row in csv.DictReader(io.StringIO(text)):
        d = (row.get("TIME_PERIOD") or "").strip()[:10]
        v = (row.get("OBS_VALUE") or "").strip()
        if not d or not v:
            continue
        try:
            out[d] = float(v)
        except ValueError:
            continue
    return out


def ecb(days: int = 500) -> dict:
    """
    Each series is tried against every spelling we know, and the outcome of
    every attempt is recorded. A source that fails should say which URL it
    tried and what came back, not just go blank.
    """
    start = (date.today() - timedelta(days=days)).isoformat()
    out, tried, err = {}, [], None
    for name, variants in ECB_FALLBACKS.items():
        for flow, key in variants:
            url = f"{ECB_API}/{flow}/{key}?" + urllib.parse.urlencode(
                {"startPeriod": start, "format": "csvdata"})
            try:
                raw = _get(url)
                vals = _parse_ecb_csv(raw)
                tried.append({"series": name, "key": f"{flow}/{key}",
                              "bytes": len(raw), "rows": len(vals),
                              "ok": bool(vals)})
                if vals:
                    out[name] = vals
                    break
            except Exception as e:
                tried.append({"series": name, "key": f"{flow}/{key}",
                              "ok": False, "error": f"{e.__class__.__name__}: {e}"})
                err = f"{e.__class__.__name__}: {e}"
    missing = [n for n in ECB_FALLBACKS if n not in out]
    return {"series": out, "source": "ecb" if out else None,
            "tried": tried, "missing": missing,
            "error": None if not missing else
                     (f"no data for {', '.join(missing)}" + (f" — {err}" if err else "")),
            "fetched_at": time.time()}


def rates() -> dict:
    """Both sources, merged, with each one's failure reported separately."""
    f, e = fred(), ecb()
    return {
        "series": {**f.get("series", {}), **e.get("series", {})},
        "fred": {"ok": bool(f.get("series")), "source": f.get("source"),
                 "error": f.get("error"), "has_key": bool(fred_key()),
                 "series_got": sorted((f.get("series") or {}).keys())},
        "ecb": {"ok": bool(e.get("series")), "error": e.get("error"),
                "tried": e.get("tried"), "missing": e.get("missing")},
        "fetched_at": time.time(),
    }


# --------------------------------------------------------------------------
# self-test
# --------------------------------------------------------------------------

def probe(url: str, note: str = "") -> dict:
    """
    One request, fully reported: which transport answered, the size, the first
    line — or every transport's error.

    It goes through pf.net deliberately. A self-test that uses its own HTTP
    code can pass while the app fails, which is worse than no self-test.
    """
    t0 = time.time()
    r = net.fetch(url, timeout=TIMEOUT)
    took = round(time.time() - t0, 2)
    if r["ok"]:
        head = r["body"][:4000].decode("utf-8", "replace").splitlines()
        return {"url": url, "note": note, "ok": True, "status": 200,
                "transport": r["transport"], "bytes": r["bytes"], "took_s": took,
                "first": (head[0][:160] if head else ""),
                "second": (head[1][:160] if len(head) > 1 else "")}
    return {"url": url, "note": note, "ok": False, "status": None,
            "took_s": took, "error": r.get("error"),
            "tried": [f"{a['transport']}: {a['error']}" for a in r["attempts"]]}


def self_test() -> dict:
    """
    Call every source for real and report exactly what came back.

    This exists because "unavailable" is not a diagnosis. When a feed stops
    working the useful facts are the URL that was tried, the status code and
    the first line of the response — not a shrug in a panel.
    """
    start = (date.today() - timedelta(days=10)).isoformat()
    out = [{"note": "HTTP client", "ok": True,
            "first": f"CA bundle: {net.ca_source()}",
            "second": ", ".join(f"{k} {v}" for k, v in
                                net.transports_available().items())}]

    out.append(probe(f"{FRED_CSV}?" + urllib.parse.urlencode(
        {"id": "DGS10,DGS2", "cosd": start}), "FRED, keyless CSV"))
    k = fred_key()
    if k:
        out.append(probe(f"{FRED_API}?" + urllib.parse.urlencode(
            {"series_id": "DGS10", "api_key": k, "file_type": "json",
             "observation_start": start}), "FRED, with your key"))
    else:
        out.append({"note": "FRED, with your key", "ok": None,
                    "error": "no key set — add one under Settings"})

    for name, variants in ECB_FALLBACKS.items():
        for flow, key in variants:
            r = probe(f"{ECB_API}/{flow}/{key}?" + urllib.parse.urlencode(
                {"startPeriod": start, "format": "csvdata"}), f"ECB {name}")
            out.append(r)
            if r.get("ok"):
                break

    try:
        import yfinance as yf
        ver = getattr(yf, "__version__", "?")
        has_ws = hasattr(yf, "AsyncWebSocket")
        out.append({"note": "yfinance", "ok": True,
                    "first": f"version {ver}; streaming client "
                             f"{'present' if has_ws else 'MISSING — needs 1.7+'}"})
    except Exception as e:
        out.append({"note": "yfinance", "ok": False, "error": str(e)})

    try:
        from . import pricing
        q = pricing.get_quotes(["^GSPC", "EURUSD=X"], fresh=True)
        out.append({"note": "Yahoo quotes", "ok": bool(q),
                    "first": ", ".join(f"{t}={(v or {}).get('price')}"
                                       for t, v in q.items()) or "nothing returned",
                    "error": None if q else "no quotes came back"})
    except Exception as e:
        out.append({"note": "Yahoo quotes", "ok": False,
                    "error": f"{e.__class__.__name__}: {e}"})

    from . import stream
    snap = stream.STREAM.snapshot()
    out.append({"note": "Live stream", "ok": snap["state"] == "streaming",
                "first": f"state={snap['state']} · {snap['detail']} · "
                         f"{snap['symbols']} symbols · {len(snap['quotes'])} quotes held",
                "error": None if snap["state"] in ("streaming", "polling")
                         else "the feed only runs while a page is watching — "
                              "open PORT or MKT and re-run this test"})

    # The GEO page's three sources. They live in sentiment.py, but a self-test
    # split across two screens is a self-test nobody runs.
    #
    # Each block is wrapped. A self-test that dies on its third check and
    # returns HTTP 500 tells you less than no self-test at all — that is
    # exactly what happened when the GPR parser raised, and it hid the answer
    # to the question being asked.
    from . import sentiment as _s

    def _guard(note, fn):
        try:
            return fn()
        except Exception as e:
            out.append({"note": note, "ok": False,
                        "error": f"the check itself raised "
                                 f"{e.__class__.__name__}: {e}"[:260]})
            return None

    cnn = _guard("CNN Fear & Greed", _s.cnn_fear_greed) or {}
    if cnn: out.append({"note": "CNN Fear & Greed", "ok": bool(cnn.get("ok")),
                "transport": cnn.get("transport"),
                "first": (f"score {cnn.get('score')} ({cnn.get('rating')}) via "
                          f"{cnn.get('transport')}" if cnn.get("ok") else ""),
                "error": cnn.get("error"),
                "tried": [f"{t['url'].rsplit('/', 1)[-1] or 'graphdata'}: "
                          f"{t.get('error') or 'ok'}" for t in (cnn.get("tried") or [])]})

    g = _guard("Geopolitical risk index", _s.gpr) or {}
    if g: out.append({"note": "Geopolitical risk index", "ok": bool(g.get("ok")),
                "transport": g.get("transport"),
                "first": (f"{g.get('value')} on {g.get('asof')} from "
                          f"{str(g.get('url','')).rsplit('/', 1)[-1]} "
                          f"({g.get('format')}, column {g.get('column')})"
                          if g.get("ok") else ""),
                "error": g.get("error"),
                "tried": [f"{t['url'].rsplit('/', 1)[-1]}: {t.get('error') or 'ok'}"
                          for t in (g.get("tried") or [])]})

    f = _guard("Fed funds future (ZQ=F)", _s.front_fed_funds) or {}
    if f: out.append({"note": "Fed funds future (ZQ=F)", "ok": bool(f.get("ok")),
                "first": (f"{f.get('price')} from {f.get('symbol')}"
                          if f.get("ok") else ""),
                "error": f.get("error"),
                "tried": [f"{t.get('symbol')}: {t.get('price', t.get('error'))}"
                          for t in (f.get("tried") or [])]})

    # Earnings: which of the three per-symbol sources actually answers here.
    try:
        from . import service, calendar as _cal
        tks = [p.get("ticker") for p in
               service.dashboard("SP500", light=True).get("positions", [])
               if p.get("ticker")][:3]
        probes = {tk: _cal._earnings_dates(tk) for tk in tks}
        good = {tk: r for tk, r in probes.items() if r.get("ok")}
        out.append({"note": "Earnings dates (Yahoo)", "ok": bool(good),
                    "first": ", ".join(f"{tk} via {r['src']} → {r['dates'][0]['date']}"
                                       for tk, r in good.items()) or "",
                    "error": None if good else
                             "none of the first three holdings returned a date",
                    "tried": [f"{tk}: {', '.join(r.get('tried') or [])}"
                              for tk, r in probes.items() if not r.get("ok")]})
    except Exception as e:
        out.append({"note": "Earnings dates (Yahoo)", "ok": False,
                    "error": f"{e.__class__.__name__}: {e}"})

    return {"checks": out, "ran_at": time.time(),
            "client": {"ca": net.ca_source(), **net.transports_available()}}
