"""
The week ahead: macro releases, and earnings for things you actually own.

MACRO comes from ForexFactory's own weekly feed
(nfs.faireconomy.media/ff_calendar_thisweek.xml). It is free, needs no key,
and each event carries an impact rating, a forecast and the previous print —
which is the whole job. The alternative was a hand-kept table of central-bank
and statistics-office dates; those are published a year ahead and are stable,
but they need refreshing every January and they carry no forecast. This feed
is the same schedules, maintained by someone else, with the numbers attached.
It is not an official API and could go away, so a failure is reported rather
than papered over.

EARNINGS comes from yfinance, per ticker, and covers YOUR HOLDINGS ONLY.
That is a deliberate limit: an earnings calendar for the whole market is a
list of five hundred companies you have no money in. Finnhub's calendar
endpoint was the obvious alternative and its free tier is documented as
returning an empty array often enough that it cannot be relied on.

Both are slow and both are cached. Nothing here is ever called from a request.
"""

from __future__ import annotations

import re
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import store, net

LOCAL = ZoneInfo("Europe/Amsterdam")

TIMEOUT = 20
UA = "het-koersbord/1.0"

FF_XML = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"
FF_JSON = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

# Currencies that actually move a euro-based book holding mostly US equities.
# Everything else in the feed is noise for this portfolio.
CURRENCIES = {"USD", "EUR", "ALL", "GBP"}
IMPACTS = {"high": 3, "medium": 2, "low": 1, "holiday": 0}


def week_bounds(today: Optional[date] = None) -> tuple[date, date]:
    """Monday to Sunday of the current week."""
    today = today or date.today()
    mon = today - timedelta(days=today.weekday())
    return mon, mon + timedelta(days=6)


# --------------------------------------------------------------------------
# macro
# --------------------------------------------------------------------------

def _parse_ff_date(d: str, t: str) -> tuple[Optional[str], Optional[str]]:
    """
    The feed writes dates as MM-DD-YYYY and times as '6:00pm', 'All Day' or
    'Tentative'. Returns (ISO date, HH:MM or None) — in the FEED's clock,
    which for the XML is UTC (Bowman at 12:15pm there is 8:15 in New York).
    """
    d = (d or "").strip()
    m = re.match(r"^(\d{2})-(\d{2})-(\d{4})$", d)
    if not m:
        try:
            return datetime.fromisoformat(d.replace("Z", "+00:00")).date().isoformat(), None
        except Exception:
            return None, None
    iso = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"

    t = (t or "").strip()
    tm = re.match(r"^(\d{1,2}):(\d{2})\s*(am|pm)$", t, re.I)
    if not tm:
        return iso, None
    h, mi, ap = int(tm.group(1)), int(tm.group(2)), tm.group(3).lower()
    if ap == "pm" and h != 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    return iso, f"{h:02d}:{mi:02d}"


def _local(when: datetime) -> tuple[str, str, float]:
    """An aware moment as (Amsterdam date, Amsterdam HH:MM, unix seconds)."""
    x = when.astimezone(LOCAL)
    return x.date().isoformat(), x.strftime("%H:%M"), when.timestamp()


def _event(iso: Optional[str], hhmm: Optional[str], g, ts_src: Optional[datetime] = None) -> Optional[dict]:
    if not iso:
        return None
    imp = (g("impact") or "").lower()
    ts = None
    if ts_src is not None:
        iso, hhmm, ts = _local(ts_src)
    elif hhmm:
        iso, hhmm, ts = _local(datetime.fromisoformat(f"{iso}T{hhmm}:00+00:00"))
    return {
        "date": iso,
        # Amsterdam time. The feed's own clock (UTC for the XML, New York for
        # the JSON) was shown as-is before, so CPI read 12:30 instead of 14:30.
        "time": hhmm,
        "ts": ts,
        "title": g("title"),
        "currency": (g("country") or "").upper(),
        "impact": imp,
        "rank": IMPACTS.get(imp, 0),
        "forecast": g("forecast") or None,
        "previous": g("previous") or None,
        "url": g("url") or None,
        "all_day": hhmm is None,
    }


def parse_macro_xml(raw: bytes | str) -> list[dict]:
    """Pure: feed it the XML, get events back. Tested against a fixture."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    root = ET.fromstring(raw)
    out = []
    for ev in root.iter("event"):
        g = lambda tag, ev=ev: (ev.findtext(tag) or "").strip()
        iso, hhmm = _parse_ff_date(g("date"), g("time"))
        e = _event(iso, hhmm, g)
        if e:
            out.append(e)
    return out


def parse_macro_json(raw: bytes | str) -> list[dict]:
    """The JSON twin of the feed: ISO timestamps with New York's offset."""
    import json as _j
    out = []
    for r in _j.loads(raw):
        g = lambda k, r=r: str(r.get(k) or "").strip()
        ds = g("date")
        try:
            when = datetime.fromisoformat(ds.replace("Z", "+00:00"))
        except Exception:
            continue
        # All-day entries (holidays) carry midnight; they have no moment.
        allday = g("impact").lower() == "holiday" or (when.hour == 0 and when.minute == 0)
        e = _event(ds[:10], None, g, None if allday else when)
        if e:
            out.append(e)
    return out


def ff_events() -> dict:
    """Every event of the feed's week, all currencies and impacts. Blocking."""
    events, err, src = [], None, None
    for url, parser in ((FF_XML, parse_macro_xml), (FF_JSON, parse_macro_json)):
        try:
            events = parser(net.get(url, timeout=TIMEOUT))
            src, err = url, None
            break
        except Exception as e:
            err = f"{e.__class__.__name__}: {e}"
    if events:
        _remember(events)
    return {"events": events, "source": src, "error": err, "fetched_at": time.time()}


def _remember(events: list[dict]):
    """
    The feed only ever holds THIS week. Monday's brief looks back at Friday,
    which by then has left it, so every event seen is kept for three weeks.
    """
    log = store.cache_get("macro:log") or {}
    ev = log.get("events") or {}
    for e in events:
        ev[f"{e['date']}|{e.get('time') or ''}|{e['currency']}|{e['title']}"] = e
    cut = (date.today() - timedelta(days=21)).isoformat()
    ev = {k: v for k, v in ev.items() if v.get("date", "") >= cut}
    store.cache_put("macro:log", {"events": ev})


def remembered(d0: str, d1: str) -> list[dict]:
    """Events between two dates (inclusive) from everything the feed has shown."""
    ev = (store.cache_get("macro:log") or {}).get("events") or {}
    out = [v for v in ev.values() if d0 <= v.get("date", "") <= d1]
    out.sort(key=lambda e: (e["date"], e.get("time") or "99:99", -e.get("rank", 0)))
    return out


def macro_week(min_rank: int = 2, today: Optional[date] = None) -> dict:
    """High and medium impact events for the current week, grouped by day."""
    mon, sun = week_bounds(today)
    got = ff_events()
    events, err, src = got["events"], got["error"], got["source"]

    keep = [e for e in events
            if e["rank"] >= min_rank
            and e["currency"] in CURRENCIES
            and mon.isoformat() <= e["date"] <= sun.isoformat()]
    keep.sort(key=lambda e: (e["date"], e["time"] or "99:99", -e["rank"]))

    days = []
    for i in range(7):
        d = (mon + timedelta(days=i)).isoformat()
        days.append({"date": d, "events": [e for e in keep if e["date"] == d]})
    return {
        "week_of": mon.isoformat(), "days": days, "count": len(keep),
        "tz": "Europe/Amsterdam",
        "source": src, "error": err, "fetched_at": time.time(),
    }


# --------------------------------------------------------------------------
# earnings
# --------------------------------------------------------------------------

# Earnings dates move rarely and Yahoo rate-limits the endpoint that serves
# them hard, so each answer is kept for two days. Without this, a rebuild is
# forty-six scraped requests and most of them come back empty.
EARN_TTL = 48 * 3600
EARN_BUDGET_S = 75          # never let one build run away with the thread
# A failure is not an answer. On 19 October 2026 the laptop woke without a
# network: every lookup failed on DNS, and all fifty were kept for two days as
# "no date on Yahoo", so the home screen showed a quiet week while Netflix and
# Intuitive Surgical reported. Yahoo saying it has no date is asked again in
# half a day; nothing answering at all, in a quarter of an hour.
EARN_NODATE_TTL = 12 * 3600
EARN_DOWN_TTL = 15 * 60
_DOWN = re.compile(r"dns|resolve|connect|timeout|timed ?out|curl|ssl|network|unreachable|"
                   r"rate.?limit|too many|429|50[0234]|reset|refused|max retries", re.I)


def earn_transient(got: Optional[dict]) -> bool:
    """A failed lookup where nothing answered: every source raised, and on the network."""
    if not got or got.get("ok"):
        return False
    if got.get("transient") is not None:
        return bool(got["transient"])
    tried = got.get("tried") or []
    return bool(tried) and all(_DOWN.search(t.split(":", 1)[-1]) for t in tried)


def earn_ttl(got: Optional[dict]) -> float:
    """How long a cached lookup stands: two days for dates, half a day for none, minutes when nothing answered."""
    if not got:
        return 0
    if got.get("ok"):
        return EARN_TTL
    return EARN_DOWN_TTL if earn_transient(got) else EARN_NODATE_TTL


def cached_dates(tk: str) -> tuple[Optional[dict], bool]:
    """(the cached lookup if it still stands, whether it came from the cache)."""
    key = f"earn:{tk}"
    got = store.cache_get(key)
    age = store.cache_age(key)
    if got and age is not None and age < earn_ttl(got):
        return got, True
    return None, False


def _earnings_dates(tk: str) -> dict:
    """
    The next earnings dates for one symbol, from whichever source answers.

    Three sources, because the first one is the one that breaks:

      get_earnings_dates   the richest — dates, EPS estimate, reported EPS —
                           and a scraped endpoint that Yahoo rate-limits and
                           has changed shape more than once.
      .calendar            a small dict, usually just the date, but served by
                           a different endpoint that tends to answer when the
                           first does not.
      .info                one timestamp, no estimate. Last resort, and enough
                           to put a name on the right day.

    European lines are the ones that fall through to the bottom: Yahoo carries
    far less for Amsterdam and Frankfurt listings than for US ones.
    """
    import yfinance as yf
    t = yf.Ticker(tk)
    tried = []

    try:
        df = t.get_earnings_dates(limit=8)
        if df is not None and len(df):
            out = []
            for idx, row in df.iterrows():
                try:
                    d = idx.date()
                except Exception:
                    continue
                hhmm = None
                try:
                    hhmm = idx.strftime("%H:%M")
                    if hhmm == "00:00":
                        hhmm = None
                except Exception:
                    pass
                est, act = row.get("EPS Estimate"), row.get("Reported EPS")
                out.append({"date": d.isoformat(), "time": hhmm,
                            "eps_estimate": None if est is None or est != est
                                            else round(float(est), 3),
                            "eps_actual": None if act is None or act != act
                                          else round(float(act), 3)})
            if out:
                return {"ok": True, "src": "earnings_dates", "dates": out}
        tried.append("earnings_dates: empty")
    except Exception as e:
        tried.append(f"earnings_dates: {e.__class__.__name__}")

    try:
        cal = t.calendar or {}
        raw = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if raw is not None:
            seq = raw if isinstance(raw, (list, tuple)) else [raw]
            out = []
            for x in seq:
                try:
                    out.append({"date": (x.date() if hasattr(x, "date") else x).isoformat(),
                                "time": None, "eps_estimate": None, "eps_actual": None})
                except Exception:
                    continue
            if out:
                return {"ok": True, "src": "calendar", "dates": out}
        tried.append("calendar: empty")
    except Exception as e:
        tried.append(f"calendar: {e.__class__.__name__}")

    try:
        ts = (t.info or {}).get("earningsTimestamp")
        if ts:
            d = datetime.utcfromtimestamp(float(ts)).date()
            return {"ok": True, "src": "info",
                    "dates": [{"date": d.isoformat(), "time": None,
                               "eps_estimate": None, "eps_actual": None}]}
        tried.append("info: no earningsTimestamp")
    except Exception as e:
        tried.append(f"info: {e.__class__.__name__}")

    out = {"ok": False, "tried": tried}
    out["transient"] = earn_transient(out)
    return out


def earnings_week(tickers: list[str], names: Optional[dict] = None,
                  weights: Optional[dict] = None,
                  today: Optional[date] = None) -> dict:
    """
    Next earnings date per holding, kept if it falls in the current week.

    Three things this has to get right, and the first version got none of them:

      SPEED       one call per ticker against a rate-limited scrape, forty-six
                  times, is minutes — long enough that the panel was still
                  saying "not fetched yet" an hour after the app started. Each
                  answer is now cached for two days and the build has a budget.

      FALLBACK    Yahoo's earnings endpoint fails often and fails per symbol.
                  Three sources are tried before a symbol is given up on.

      HONESTY     "no holdings report this week" and "the calendar could not be
                  read" look identical in an empty panel and mean opposite
                  things. They are now separate states, and a partial answer
                  says how partial it is.
    """
    mon, sun = week_bounds(today)
    names, weights = names or {}, weights or {}
    rows, failed, resolved, from_cache = [], [], 0, 0
    upcoming = []
    t0 = time.time()
    budget_hit = False

    try:
        import yfinance  # noqa: F401
    except Exception as e:
        return {"week_of": mon.isoformat(), "days": _blank_week(mon), "count": 0,
                "symbols": len(tickers), "failed": len(tickers),
                "error": f"yfinance unavailable: {e}", "fetched_at": time.time()}

    down = []
    for tk in tickers:
        key = f"earn:{tk}"
        got, hit = cached_dates(tk)
        if hit:
            from_cache += 1
        if got is None:
            if time.time() - t0 > EARN_BUDGET_S:
                budget_hit = True
                break
            got = _earnings_dates(tk)
            store.cache_put(key, got)

        if not got.get("ok"):
            failed.append(tk)
            if earn_transient(got):
                down.append(tk)
            continue
        resolved += 1
        for d in got.get("dates", []):
            try:
                dd = date.fromisoformat(d["date"])
            except Exception:
                continue
            # Everything still ahead of us, for the "what's next" line. A week
            # with no reporters is the correct answer most of the year, and an
            # empty grid with nothing beside it reads as a broken panel.
            if dd > sun:
                upcoming.append({"date": d["date"], "ticker": tk,
                                 "name": names.get(tk, tk),
                                 "weight": round(weights.get(tk, 0.0), 2),
                                 "days": (dd - (today or date.today())).days})
            if not (mon <= dd <= sun):
                continue
            rows.append({
                "date": d["date"], "time": d.get("time"), "ticker": tk,
                "name": names.get(tk, tk), "weight": round(weights.get(tk, 0.0), 2),
                "eps_estimate": d.get("eps_estimate"),
                "eps_actual": d.get("eps_actual"),
                "reported": d.get("eps_actual") is not None,
                "src": got.get("src"),
            })

    # Nasdaq's calendar, day by day, as ERN reads it: a second source for every
    # US holding, and the one that still answers when Yahoo's per-symbol
    # endpoint does not. A European line is found through its US listing.
    nq_added = 0
    for d in [mon + timedelta(days=i) for i in range(5)]:
        for r in _nasdaq_rows(d):
            tk = r.get("held")
            if not tk:
                continue
            ds = d.isoformat()
            mine = next((x for x in rows if x["ticker"] == tk), None)
            if mine and mine["date"] == ds:
                if mine.get("eps_estimate") is None and r.get("eps_est") is not None:
                    mine["eps_estimate"] = r["eps_est"]
                mine["time"] = mine.get("time") or WHEN.get(r.get("when"))
                continue
            if mine:
                continue            # Yahoo has it on another day this week: one event, Yahoo's
            rows.append({"date": ds, "time": WHEN.get(r.get("when")), "ticker": tk,
                         "name": names.get(tk, tk), "weight": round(weights.get(tk, 0.0), 2),
                         "eps_estimate": r.get("eps_est"), "eps_actual": r.get("eps_act"),
                         "reported": r.get("eps_act") is not None, "src": "nasdaq"})
            nq_added += 1

    # An empty week is only an ERROR if nothing could be read at all. If two
    # thirds of the book answered and none of them report this week, that is a
    # quiet week — which is a fact, not a failure. But when most lookups failed
    # because nothing answered, the week is not quiet, it is unknown: say so.
    degraded = None
    if down and len(down) * 2 >= len(tickers):
        degraded = (f"Yahoo could not be reached for {len(down)} of {len(tickers)} holdings"
                    + (f"; {nq_added} found on Nasdaq's calendar" if nq_added else "")
                    + "; asked again within a quarter of an hour")
    err = None
    if resolved == 0 and tickers and not nq_added:
        err = (f"no earnings dates could be read for any of {len(tickers)} "
               f"holdings — Yahoo's earnings endpoint is refusing")
    elif budget_hit and not rows:
        err = (f"ran out of time after {resolved} of {len(tickers)} symbols; "
               f"the rest are fetched on the next pass")

    # Biggest position first within a day: if two report on Thursday, the one
    # holding more of your money is the one to read about.
    rows.sort(key=lambda r: (r["date"], -(r["weight"] or 0), r["ticker"]))
    upcoming.sort(key=lambda r: (r["date"], -(r["weight"] or 0)))
    seen, next_up = set(), []
    for u in upcoming:
        if u["ticker"] in seen:
            continue
        seen.add(u["ticker"])
        next_up.append(u)
        if len(next_up) >= 6:
            break
    days = _blank_week(mon)
    for d in days:
        d["events"] = [r for r in rows if r["date"] == d["date"]]
    return {"week_of": mon.isoformat(), "days": days, "count": len(rows),
            "next_up": next_up,
            "symbols": len(tickers), "resolved": resolved,
            "failed": len(failed), "failed_symbols": failed[:12],
            "unreachable": len(down), "from_nasdaq": nq_added, "degraded": degraded,
            # the brief retries a payload carrying a failed source within minutes
            "yahoo": {"ok": not degraded},
            "cached": from_cache, "partial": budget_hit,
            "took_s": round(time.time() - t0, 1),
            "error": err, "fetched_at": time.time()}


WHEN = {"bmo": "before open", "amc": "after close", "dmh": "during session"}


def _nasdaq_rows(d: date) -> list[dict]:
    """Nasdaq's reports for one day, each tagged with the holding it is (ING for INGA.AS)."""
    try:
        from . import earnings as ern
        hd, al = ern.held(), ern.us_alias()
        out = []
        for r in ern.nasdaq_day(d):
            sym = r.get("sym")
            tk = sym if sym in hd else al.get(sym)
            if tk:
                out.append({**r, "held": tk})
        return out
    except Exception:
        return []


def _blank_week(mon: date) -> list:
    return [{"date": (mon + timedelta(days=i)).isoformat(), "events": []}
            for i in range(7)]


# --------------------------------------------------------------------------
# today
# --------------------------------------------------------------------------

def day_preview(macro: dict, earnings: dict, today: Optional[date] = None) -> dict:
    """
    What is coming that you would want to know before the open.

    Today first; if today is empty — a quiet Wednesday, or a weekend — it
    looks forward to the next day in the week that has anything, and says so.
    An empty panel teaches nothing.
    """
    today = today or date.today()
    iso = today.isoformat()

    def on(payload, d):
        for day in (payload or {}).get("days", []):
            if day["date"] == d:
                return day["events"]
        return []

    m, e = on(macro, iso), on(earnings, iso)
    when, looked_ahead = iso, False
    if not m and not e:
        for i in range(1, 7):
            d = (today + timedelta(days=i)).isoformat()
            m, e = on(macro, d), on(earnings, d)
            if m or e:
                when, looked_ahead = d, True
                break

    return {
        "date": when,
        "is_today": when == iso,
        "looked_ahead": looked_ahead,
        "macro": sorted(m, key=lambda x: (-x["rank"], x["time"] or "99:99"))[:4],
        "earnings": e[:4],
        "empty": not m and not e,
    }
