"""
MAC — the macro calendar: the week's releases, what came out, and what it
did to the euro and to American shares.

THE WEEK comes from the calendar the brief already reads (ForexFactory's
weekly feed: time, impact, forecast, previous; calendar.py), kept here in
`mac_event` so that a release stays on record after the feed moves on.

THE ACTUAL is not in that feed, so for the releases that move a euro
investor in American shares it is read from the source the number itself is
published to: FRED for the American ones (CPI, payrolls, unemployment, retail
sales, PPI, PCE, claims, GDP, JOLTS, the Fed's target) and the ECB for the
euro area's (HICP, the refinancing rate). Each release is matched to its
reference period (last month's CPI, the quarter just ended for GDP, the week
to Saturday for claims), not to "the latest number", so a value is never
filled in before it exists. The feed's own "previous" is compared with the
period before as a check.

THE REACTION is read from Yahoo's five-minute bars: EUR/USD, and the S&P 500
future (ES=F, which trades when the releases land, at 14:30 Amsterdam time,
before New York opens), from the close of the bar before the release to the
close half an hour after. Yahoo keeps five-minute bars for about sixty days,
so the record builds forward from build 31, plus whatever the brief's three
weeks of remembered events allow.

THE SENSITIVITY: once a release has a few surprises on record, the move per
point of surprise, through the origin. Thin at first by design.
"""

from __future__ import annotations

import logging
import math
import re
import statistics
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import store

log = logging.getLogger(__name__)

AMS = None
try:
    from zoneinfo import ZoneInfo
    AMS = ZoneInfo("Europe/Amsterdam")
except Exception:                                    # pragma: no cover
    AMS = timezone(timedelta(hours=2))

WINDOW_MIN = 30             # the reaction: from the bar before to half an hour after
SETTLE_S = 35 * 60          # wait this long after a release before reading its reaction
ACTUAL_S = 30 * 60          # and this long before looking for the actual
KEEP_RX_DAYS = 28
FX_SYM, SPX_SYM = "EURUSD=X", "ES=F"

# (currency, title in the feed) → where the actual is published and how to read it.
#   how: mom (percent change on the month), yoy (on the year), diff (change in level),
#        level; unit as the feed writes it; div scales the level first.
SOURCES = {
    ("USD", "CPI m/m"): {"src": "fred", "id": "CPIAUCSL", "how": "mom", "unit": "%", "dec": 1, "freq": "m"},
    ("USD", "CPI y/y"): {"src": "fred", "id": "CPIAUCNS", "how": "yoy", "unit": "%", "dec": 1, "freq": "m"},
    ("USD", "Core CPI m/m"): {"src": "fred", "id": "CPILFESL", "how": "mom", "unit": "%", "dec": 1, "freq": "m"},
    ("USD", "Core CPI y/y"): {"src": "fred", "id": "CPILFENS", "how": "yoy", "unit": "%", "dec": 1, "freq": "m"},
    ("USD", "Non-Farm Employment Change"): {"src": "fred", "id": "PAYEMS", "how": "diff", "unit": "K", "dec": 0, "freq": "m"},
    ("USD", "Unemployment Rate"): {"src": "fred", "id": "UNRATE", "how": "level", "unit": "%", "dec": 1, "freq": "m"},
    ("USD", "Average Hourly Earnings m/m"): {"src": "fred", "id": "CES0500000003", "how": "mom", "unit": "%", "dec": 1, "freq": "m"},
    ("USD", "Retail Sales m/m"): {"src": "fred", "id": "RSAFS", "how": "mom", "unit": "%", "dec": 1, "freq": "m"},
    ("USD", "Core Retail Sales m/m"): {"src": "fred", "id": "RSFSXMV", "how": "mom", "unit": "%", "dec": 1, "freq": "m"},
    ("USD", "PPI m/m"): {"src": "fred", "id": "PPIFIS", "how": "mom", "unit": "%", "dec": 1, "freq": "m"},
    ("USD", "Core PCE Price Index m/m"): {"src": "fred", "id": "PCEPILFE", "how": "mom", "unit": "%", "dec": 1, "freq": "m"},
    ("USD", "Unemployment Claims"): {"src": "fred", "id": "ICSA", "how": "level", "unit": "K", "dec": 0, "div": 1000, "freq": "w"},
    ("USD", "Advance GDP q/q"): {"src": "fred", "id": "A191RL1Q225SBEA", "how": "level", "unit": "%", "dec": 1, "freq": "q"},
    ("USD", "Prelim GDP q/q"): {"src": "fred", "id": "A191RL1Q225SBEA", "how": "level", "unit": "%", "dec": 1, "freq": "q"},
    ("USD", "Final GDP q/q"): {"src": "fred", "id": "A191RL1Q225SBEA", "how": "level", "unit": "%", "dec": 1, "freq": "q"},
    ("USD", "JOLTS Job Openings"): {"src": "fred", "id": "JTSJOL", "how": "level", "unit": "M", "dec": 2, "div": 1000, "freq": "m2"},
    ("USD", "Federal Funds Rate"): {"src": "fred", "id": "DFEDTARU", "how": "level", "unit": "%", "dec": 2, "freq": "d"},
    # The euro area's HICP from FRED (Eurostat's figures): the ECB's own ICP
    # series and Eurostat's prc_hicp_manr both stop at December 2025, the month
    # before Bulgaria joined. FRED carries the final figures, so a flash
    # estimate's actual is filled when the final one lands, about two weeks on.
    ("EUR", "CPI Flash Estimate y/y"): {"src": "fred", "id": "CP0000EZ19M086NEST", "how": "yoy", "unit": "%", "dec": 1, "freq": "mf", "later": True},
    ("EUR", "Core CPI Flash Estimate y/y"): {"src": "fred", "id": "00XEFDEZ19M086NEST", "how": "yoy", "unit": "%", "dec": 1, "freq": "mf", "later": True},
    ("EUR", "Final CPI y/y"): {"src": "fred", "id": "CP0000EZ19M086NEST", "how": "yoy", "unit": "%", "dec": 1, "freq": "m"},
    ("EUR", "Final Core CPI y/y"): {"src": "fred", "id": "00XEFDEZ19M086NEST", "how": "yoy", "unit": "%", "dec": 1, "freq": "m"},
    ("EUR", "Main Refinancing Rate"): {"src": "ecb", "id": "FM/D.U2.EUR.4F.KR.MRR_FR.LEV", "how": "level", "unit": "%", "dec": 2, "freq": "d7"},
}


# --------------------------------------------------------------------------
# numbers as the feed writes them
# --------------------------------------------------------------------------

_NUM = re.compile(r"^\s*[<>]?\s*([+-]?\d+(?:\.\d+)?)\s*([%KMBT]?)\s*$", re.I)


def parse_num(s: Optional[str]) -> Optional[tuple[float, str]]:
    """"0.3%" → (0.3, "%"), "195K" → (195.0, "K"), "-0.1%" → (-0.1, "%"); None for anything else."""
    if not s:
        return None
    m = _NUM.match(str(s))
    if not m:
        return None
    return float(m.group(1)), m.group(2).upper()


def fmt(v: float, unit: str, dec: int) -> str:
    return f"{v:.{dec}f}{unit}"


# --------------------------------------------------------------------------
# the reference period a release is about
# --------------------------------------------------------------------------

def _month_start(y: int, m: int) -> date:
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return date(y, m, 1)


def reference(freq: str, released: date) -> Optional[str]:
    """
    The observation date a release reports, as FRED and the ECB date them:
      m   last month (CPI in October is September's)
      m2  JOLTS: the month 40 days before the release. JOLTS comes out 30
          to 40 days after the month it covers, at the start of a month or
          at the end of one: on 6 October and on 29 September it is
          August's. "The month before last" (build 31) read 29 September
          as July's, and stored July's figure as the actual.
      mf  a flash estimate: this month from the 25th on, else last month
      w   the week to the Saturday before (claims on Thursday)
      q   the last quarter that ended at least 20 days before
      d   the day after (the Fed's target moves the next morning)
      d7  a week on (the ECB's rates take effect the following Wednesday)
    """
    if freq == "m":
        return _month_start(released.year, released.month - 1).isoformat()
    if freq == "m2":
        d = released - timedelta(days=40)
        return _month_start(d.year, d.month).isoformat()
    if freq == "d7":
        return (released + timedelta(days=7)).isoformat()
    if freq == "mf":
        back = 0 if released.day >= 25 else 1
        return _month_start(released.year, released.month - back).isoformat()
    if freq == "w":
        d = released - timedelta(days=(released.weekday() - 5) % 7 or 7)
        return d.isoformat()
    if freq == "q":
        d = released - timedelta(days=20)
        q0 = (d.month - 1) // 3 * 3 + 1             # this quarter's first month
        return _month_start(d.year, q0 - 3).isoformat()
    if freq == "d":
        return (released + timedelta(days=1)).isoformat()
    return None


def value_at(series: dict, spec: dict, ref: str) -> Optional[tuple[float, Optional[float]]]:
    """(value for the reference period, value for the period before) in the feed's units."""
    keys = sorted(series)
    if spec["freq"] in ("d", "d7"):
        after = [k for k in keys if k >= ref]
        if not after:
            return None
        i = keys.index(after[0])
    else:
        if ref not in series:
            return None
        i = keys.index(ref)
    div = spec.get("div") or 1.0

    def one(j):
        if j < 0:
            return None
        v = series[keys[j]]
        how = spec["how"]
        if how == "level":
            return v / div
        if how == "diff":
            return (v - series[keys[j - 1]]) / (div if spec.get("div") else 1.0) if j >= 1 else None
        if how == "mom":
            return (v / series[keys[j - 1]] - 1) * 100 if j >= 1 and series[keys[j - 1]] else None
        if how == "yoy":
            # the same month a year before, by date: a gap in the series must
            # not turn eleven months into a year
            d = keys[j]
            prev = series.get(f"{int(d[:4]) - 1}{d[4:]}")
            return (v / prev - 1) * 100 if prev else None
        return None
    cur = one(i)
    if cur is None:
        return None
    return round(cur, spec["dec"]), (round(one(i - 1), spec["dec"]) if one(i - 1) is not None else None)


# --------------------------------------------------------------------------
# reactions from five-minute bars
# --------------------------------------------------------------------------

def move(bars: list, ts: float, minutes: int = WINDOW_MIN) -> Optional[float]:
    """
    Percent change from the close of the last bar finished by `ts` to the
    close of the last bar finished by `ts` + minutes. Bars are (start unix,
    close), five minutes each. None when either side has no bar near it.
    """
    if not bars:
        return None
    import bisect
    starts = [b[0] for b in bars]

    def close_by(t):
        i = bisect.bisect_right(starts, t - 300) - 1        # a bar that started 5 min before t has finished by t
        if i < 0 or t - (bars[i][0] + 300) > 900:          # nothing within a quarter of an hour
            return None
        return bars[i][1]
    a, b = close_by(ts), close_by(ts + minutes * 60)
    if not a or not b:
        return None
    return round((b / a - 1) * 100, 3)


def _bars(sym: str) -> list:
    from . import bonds
    h = bonds._yf().Ticker(sym).history(period="1mo", interval="5m", auto_adjust=False)
    return sorted((int(i.timestamp()), float(v)) for i, v in h["Close"].dropna().items())


def bars(sym: str) -> list:
    def load():
        return {"bars": _bars(sym), "fetched_at": time.time()}
    return store.cache_through(f"mac:bars:{sym}", 20 * 60, load).get("bars") or []


def sensitivity(rows: list[dict], key: str) -> Optional[dict]:
    """Move per unit of surprise, through the origin, once four surprises are on record."""
    pts = [(r["surprise"], r[key]) for r in rows if r.get("surprise") not in (None, 0) and r.get(key) is not None]
    if len(pts) < 4:
        return None
    sxx = sum(s * s for s, _ in pts)
    if sxx <= 0:
        return None
    b = sum(s * m for s, m in pts) / sxx
    return {"per_unit": round(b, 3), "n": len(pts)}


# --------------------------------------------------------------------------
# keeping the record
# --------------------------------------------------------------------------

def key(e: dict) -> str:
    return f"{e['date']}|{e.get('time') or ''}|{e.get('currency')}|{e.get('title')}"


def upsert(events: list[dict]) -> int:
    n = 0
    now = time.time()
    with store.conn() as c:
        for e in events:
            if not e.get("date") or not e.get("title"):
                continue
            k = key(e)
            old = c.execute("SELECT k FROM mac_event WHERE k=?", (k,)).fetchone()
            if old:
                c.execute("UPDATE mac_event SET impact=?, rank=?, forecast=?, previous=?, ts=?, updated=? WHERE k=?",
                          (e.get("impact"), e.get("rank") or 0, e.get("forecast"), e.get("previous"), e.get("ts"), now, k))
            else:
                c.execute("INSERT INTO mac_event (k, d, t, ts, ccy, title, impact, rank, forecast, previous, updated) "
                          "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (k, e["date"], e.get("time"), e.get("ts"), e.get("currency"), e["title"], e.get("impact"),
                           e.get("rank") or 0, e.get("forecast"), e.get("previous"), now))
                n += 1
    return n


def rows(d0: str, d1: str) -> list[dict]:
    with store.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM mac_event WHERE d BETWEEN ? AND ? ORDER BY d, t, ccy", (d0, d1))]


def _load_series(spec_ids: dict) -> dict:
    """{source id: {date: value}} for the FRED and ECB series asked for."""
    from . import feeds, euro
    out = {}
    fred_ids = [i for i, s in spec_ids.items() if s == "fred"]
    if fred_ids:
        got = feeds.fred(fred_ids, days=900)
        out.update(got.get("series") or {})
    for i, s in spec_ids.items():
        if s != "ecb":
            continue
        try:
            vals = {}
            for r in euro._ecb(i, (date.today() - timedelta(days=900)).isoformat()[:7]):
                v = r.get("OBS_VALUE")
                if v not in (None, ""):
                    tp = r["TIME_PERIOD"]
                    vals[(tp + "-01") if len(tp) == 7 else tp[:10]] = float(v)
            out[i] = vals
        except Exception as e:
            log.warning("mac ecb %s: %s", i, e)
    return out


def fill_actuals(now: Optional[float] = None, series: Optional[dict] = None) -> int:
    now = now or time.time()
    with store.conn() as c:
        todo = [dict(r) for r in c.execute(
            "SELECT * FROM mac_event WHERE actual IS NULL AND ts IS NOT NULL AND ts <= ? AND d >= ?",
            (now - ACTUAL_S, (date.today() - timedelta(days=60)).isoformat()))]
    todo = [r for r in todo if (r["ccy"], r["title"]) in SOURCES]
    if not todo:
        return 0
    need = {SOURCES[(r["ccy"], r["title"])]["id"]: SOURCES[(r["ccy"], r["title"])]["src"] for r in todo}
    series = _load_series(need) if series is None else series
    n = 0
    with store.conn() as c:
        for r in todo:
            spec = SOURCES[(r["ccy"], r["title"])]
            ser = series.get(spec["id"]) or {}
            ref = reference(spec["freq"], date.fromisoformat(r["d"]))
            got = value_at(ser, spec, ref) if ref and ser else None
            if not got:
                continue
            val, prev = got
            fc = parse_num(r.get("forecast"))
            pv = parse_num(r.get("previous"))
            surprise = round(val - fc[0], spec["dec"] + 1) if fc else None
            check = None
            # GDP's "previous" in the feed is the last estimate of the same
            # quarter, not the quarter before, so there is nothing to check it on.
            if pv and prev is not None and spec["freq"] != "q":
                tol = 0.15 if spec["unit"] == "%" else (40 if spec["unit"] == "K" else 0.2)
                check = "ok" if abs(pv[0] - prev) <= tol else "revised"
            extra = ", ".join(x for x in (check, "the final figure, not the flash" if spec.get("later") else None) if x)
            c.execute("UPDATE mac_event SET actual=?, actual_src=?, surprise=?, unit=?, updated=? WHERE k=?",
                      (fmt(val, spec["unit"], spec["dec"]), f"{spec['src']}:{spec['id']}" + (f" ({extra})" if extra else ""),
                       surprise, spec["unit"], time.time(), r["k"]))
            n += 1
    return n


def fill_reactions(now: Optional[float] = None, bar_sets: Optional[dict] = None) -> int:
    now = now or time.time()
    with store.conn() as c:
        todo = [dict(r) for r in c.execute(
            "SELECT * FROM mac_event WHERE rx_at IS NULL AND ts IS NOT NULL AND ts <= ? AND ts >= ? "
            "AND ccy IN ('USD','EUR') AND rank >= 2", (now - SETTLE_S, now - KEEP_RX_DAYS * 86400))]
    if not todo:
        return 0
    if bar_sets is None:
        bar_sets = {}
        for s in (FX_SYM, SPX_SYM):
            try:
                bar_sets[s] = bars(s)
            except Exception as e:
                log.warning("mac bars %s: %s", s, e)
                bar_sets[s] = []
    n = 0
    with store.conn() as c:
        for r in todo:
            fx_m, sp_m = move(bar_sets.get(FX_SYM) or [], r["ts"]), move(bar_sets.get(SPX_SYM) or [], r["ts"])
            if fx_m is None and sp_m is None:
                # The bars do not reach back that far, or the market was shut: mark it read.
                if r["ts"] < now - 3 * 86400:
                    c.execute("UPDATE mac_event SET rx_at=? WHERE k=?", (time.time(), r["k"]))
                continue
            c.execute("UPDATE mac_event SET rx_fx=?, rx_spx=?, rx_at=? WHERE k=?", (fx_m, sp_m, time.time(), r["k"]))
            n += 1
    return n


def fix_jolts() -> int:
    """
    Once, in build 33: JOLTS actuals stored under the old reference rule are
    read again, so a release that covered August stops showing July's figure.
    """
    if store.get_setting("mac_fix_jolts") == "33":
        return 0
    with store.conn() as c:
        n = c.execute("UPDATE mac_event SET actual=NULL, actual_src=NULL, surprise=NULL "
                      "WHERE title LIKE 'JOLTS%' AND actual IS NOT NULL").rowcount
    store.set_setting("mac_fix_jolts", "33")
    return n


def refresh() -> dict:
    """From what the brief remembers: the record, then actuals, then reactions."""
    from . import calendar
    today = date.today()
    ev = calendar.remembered((today - timedelta(days=21)).isoformat(), (today + timedelta(days=14)).isoformat())
    out = {"new": upsert(ev), "jolts_reread": fix_jolts()}
    for k, fn in (("actuals", fill_actuals), ("reactions", fill_reactions)):
        try:
            out[k] = fn()
        except Exception as e:
            out[k + "_error"] = f"{e.__class__.__name__}: {e}"[:200]
    out["at"] = time.time()
    store.cache_put("mac:last", out)
    return out


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

def _when(r: dict) -> Optional[str]:
    if not r.get("ts"):
        return None
    return datetime.fromtimestamp(r["ts"], tz=AMS).isoformat(timespec="minutes")


def view() -> dict:
    today = date.today()
    mon = today - timedelta(days=today.weekday())
    week = rows(mon.isoformat(), (mon + timedelta(days=6)).isoformat())
    past = [r for r in rows((today - timedelta(days=28)).isoformat(), today.isoformat())
            if r["ccy"] in ("USD", "EUR") and (r["rank"] or 0) >= 2 and (r["ts"] or 0) <= time.time()]
    # by release, across everything on record
    with store.conn() as c:
        allr = [dict(r) for r in c.execute("SELECT * FROM mac_event WHERE ccy IN ('USD','EUR') AND (rx_fx IS NOT NULL OR rx_spx IS NOT NULL)")]
    by: dict = {}
    for r in allr:
        by.setdefault((r["ccy"], r["title"]), []).append(r)
    react = []
    for (ccy, title), rs in by.items():
        fx_ = [abs(r["rx_fx"]) for r in rs if r["rx_fx"] is not None]
        sp_ = [abs(r["rx_spx"]) for r in rs if r["rx_spx"] is not None]
        react.append({"ccy": ccy, "title": title, "n": len(rs),
                      "fx_abs": round(statistics.fmean(fx_), 3) if fx_ else None,
                      "spx_abs": round(statistics.fmean(sp_), 3) if sp_ else None,
                      "fx_sens": sensitivity(rs, "rx_fx"), "spx_sens": sensitivity(rs, "rx_spx"),
                      "unit": next((r["unit"] for r in rs if r.get("unit")), None)})
    react.sort(key=lambda x: -((x["fx_abs"] or 0) + (x["spx_abs"] or 0) / 2))
    nxt = next((r for r in week + rows((mon + timedelta(days=7)).isoformat(), (mon + timedelta(days=13)).isoformat())
                if (r["ts"] or 0) > time.time() and r["ccy"] in ("USD", "EUR") and (r["rank"] or 0) >= 3), None)
    from . import sentiment
    banks = {"ecb": sentiment.next_meeting(sentiment.ECB_MEETINGS), "fed": sentiment.next_meeting(sentiment.FOMC),
             "as_of": sentiment.MEETINGS_AS_OF}
    try:
        p = (store.cache_get("eur:view") or {}).get("path") or {}
        banks["fed_priced"] = (p.get("fed") or {}).get("next")
        banks["fed_year_bp"], banks["ecb_year_bp"] = (p.get("fed") or {}).get("bp"), (p.get("ecb") or {}).get("bp")
    except Exception:
        pass
    from . import macparts
    for r in week + past:
        r["when"] = _when(r)
        r["sourced"] = (r["ccy"], r["title"]) in SOURCES
        # a row with a breakdown opens a panel of its parts (pf/macparts.py)
        r["family"] = macparts.family_of(r["ccy"], r["title"])
    last = store.cache_get("mac:last") or {}
    return {"week_of": mon.isoformat(), "week": week, "past": past[::-1], "reactions": react, "next": nxt,
            "banks": banks, "sources": sorted(f"{c} {t}" for c, t in SOURCES), "window_min": WINDOW_MIN,
            "last_refresh": last.get("at"), "refresh": {k: v for k, v in last.items() if k != "at"},
            "currencies": sorted({r["ccy"] for r in week if r.get("ccy")})}


# --------------------------------------------------------------------------
# the loop
# --------------------------------------------------------------------------

_STARTED = {"v": False}


def ensure():
    if _STARTED["v"]:
        return
    _STARTED["v"] = True

    def loop():
        time.sleep(120)
        while True:
            try:
                refresh()
            except Exception as e:
                log.warning("mac refresh: %s", e)
            time.sleep(15 * 60)
    threading.Thread(target=loop, name="mac-loop", daemon=True).start()
