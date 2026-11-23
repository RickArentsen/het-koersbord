"""
ERN — earnings: when things report, what is expected, what usually happens,
the numbers the moment they land, and what they mean.

THE CALENDAR is three free sources merged, because each has a hole:

    Nasdaq      every US company, per day, with the consensus EPS, the number
                of estimates, market cap — and for past days the reported EPS
                and the surprise. Rarely says before-open or after-close.
    Finnhub     (with your key) a week at a time: before-open / after-close,
                and the consensus REVENUE, which Nasdaq does not carry.
    Yahoo       per holding: the exact time, and the only source for your
                Amsterdam and Frankfurt lines.

Your holdings are marked with their weight and value; names you research (a
saved thesis) or pin here are "watched". Everything else is the market, kept
to the big names unless you ask for all of it.

ONE EVENT, clicked, is seven parts, each loaded on its own:

    expect     the consensus for this quarter: EPS and revenue, the range, how
               many analysts, how it moved; what the company guided last time;
               what to watch
    playbook   what the stock did after every report since 2022: beat or miss,
               the move on the day, the drift after, against the market; what
               options price now; what that is in euros for your position
    results    the numbers as reported, against the consensus as it stood
               before — read from the press release the moment it is on EDGAR
    docs       every source: the 8-K and its exhibits, slides, the 10-Q, the
               call transcript, and anything you added
    call       the transcript read: tone, topics, every analyst question
    analysis   the free, rule-based write-up
    report     the one Claude writes, when you ask for it (earnai.py)

The consensus rolls to the next quarter the moment a company reports. So the
expectation for an event is SNAPSHOT while it is still ahead, and the results
are always compared with the snapshot — never with a number that has already
moved on.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from . import store, net, research, earndocs as ed
from . import news as newsmod

try:
    from zoneinfo import ZoneInfo
    ET, AMS = ZoneInfo("America/New_York"), ZoneInfo("Europe/Amsterdam")
except Exception:                                   # pragma: no cover
    ET = AMS = None

NASDAQ_H = {"Accept": "application/json, text/plain, */*", "Origin": "https://www.nasdaq.com",
            "Referer": "https://www.nasdaq.com/"}
BIG = 20e9             # "market" scope: companies at least this large
MEGA = 200e9
FOREVER = 10 * 365 * 86400
PARTS = ("expect", "playbook", "results", "docs", "call", "analysis", "report")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _f(x) -> Optional[float]:
    try:
        v = float(x)
        return None if v != v or v in (float("inf"), float("-inf")) else v
    except Exception:
        return None


def _r(x, d: int = 2) -> Optional[float]:
    v = _f(x)
    return round(v, d) if v is not None else None


def _pct(a, b) -> Optional[float]:
    a, b = _f(a), _f(b)
    return round((a / b - 1) * 100, 2) if a is not None and b not in (None, 0) else None


def _surp(act, est) -> Optional[float]:
    a, e = _f(act), _f(est)
    if a is None or e is None or e == 0:
        return None
    return round((a - e) / abs(e) * 100, 2)


def _dd(s) -> date:
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])


def today_et() -> date:
    return datetime.now(ET).date() if ET else date.today()


def _money(s) -> Optional[float]:
    if s in (None, "", "N/A"):
        return None
    return ed.num(str(s).replace("$", ""))


def ysym(s: str) -> str:
    """Nasdaq and Finnhub write BRK.B; Yahoo, and this app, BRK-B."""
    s = (s or "").upper().strip()
    return re.sub(r"\.([A-Z])$", r"-\1", s) if not re.search(r"\.[A-Z]{2,}$", s) else s


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


# --------------------------------------------------------------------------
# when: before the open, after the close, in your time
# --------------------------------------------------------------------------

WHEN_LABEL = {"bmo": "before the open", "amc": "after the close", "dmh": "during the session"}
TYPICAL_ET = {"bmo": "07:00", "amc": "16:05", "dmh": "12:00"}


def when_of(hhmm: Optional[str]) -> Optional[str]:
    if not hhmm:
        return None
    h, m = (int(x) for x in hhmm.split(":")[:2])
    t = h + m / 60
    # 15:xx counts as after the close: Yahoo's estimated dates past a US
    # clock change carry last quarter's 16:00 an hour early.
    return "amc" if t >= 15 else ("bmo" if t < 9.5 else "dmh")


def local_time(d: str, hhmm_et: Optional[str]) -> Optional[str]:
    """An ET clock time on day d, in Amsterdam time. DST differs by a few weeks a year."""
    if not hhmm_et or not ET:
        return None
    h, m = (int(x) for x in hhmm_et.split(":")[:2])
    t = datetime(*map(int, d.split("-")), h, m, tzinfo=ET).astimezone(AMS)
    return t.strftime("%H:%M")


def event_ts(d: str, when: Optional[str], hhmm: Optional[str] = None) -> Optional[float]:
    """Unix time of the release: the exact time if known, else the usual one."""
    t = hhmm or TYPICAL_ET.get(when or "")
    if not t or not ET:
        return None
    h, m = (int(x) for x in t.split(":")[:2])
    return datetime(*map(int, d.split("-")), h, m, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------
# who you are: holdings, the watchlist
# --------------------------------------------------------------------------

_HELD = {"t": 0.0, "v": None}


def held() -> dict:
    """
    {symbol: {name, weight, value_eur, qty, currency, price}} for the shares
    you own — not funds, not turbos (a turbo's underlying reports, but the
    position is a derivative and its weight is not the share's).
    """
    if _HELD["v"] is not None and time.time() - _HELD["t"] < 120:
        return _HELD["v"]
    out = {}
    try:
        from . import service
        d = service.dashboard("SP500", light=True)
        for r in d.get("positions", []):
            tk = r.get("ticker")
            if not tk or r.get("excluded") or r.get("kind") != "EQUITY":
                continue
            out[tk] = {"name": r.get("name") or tk, "weight": r.get("weight") or 0.0,
                       "value_eur": r.get("value_eur"), "qty": r.get("qty"),
                       "currency": r.get("currency"), "price": r.get("price"),
                       "isin": r.get("isin"), "avg_price": r.get("avg_price"), "sector": r.get("sector")}
    except Exception:
        pass
    _HELD.update(t=time.time(), v=out)
    return out


def us_alias() -> dict:
    """
    {US symbol: held symbol} for the Amsterdam and Frankfurt lines whose
    company also reports in the US (ING -> INGA.AS). The calendars and EDGAR
    know the US symbol; your position is the European one.
    """
    def load():
        m = {}
        for sym, h in held().items():
            if not re.search(r"\.[A-Z]{1,3}$", sym):
                continue
            f = _safe(lambda: ed.filer(sym, h["name"]))
            if f:
                m[f["ticker"]] = sym
        return {"map": m}
    return store.cache_through("ern:alias", 7 * 86400, load).get("map") or {}


def watchlist() -> list[str]:
    """Pinned on this page or on WCH, plus every name with a saved thesis."""
    try:
        pinned = json.loads(store.get_setting("ern_watch") or "[]")
    except Exception:
        pinned = []
    th = [t["symbol"] for t in _safe(research.theses, []) or []]
    from . import watch
    wl = _safe(watch.pinned_syms, []) or []
    return sorted({ysym(s) for s in pinned + th + wl if s})


def set_watch(sym: str, on: bool) -> list[str]:
    try:
        pinned = set(json.loads(store.get_setting("ern_watch") or "[]"))
    except Exception:
        pinned = set()
    (pinned.add if on else pinned.discard)(ysym(sym))
    store.set_setting("ern_watch", json.dumps(sorted(pinned)))
    return watchlist()


def position(sym: str) -> Optional[dict]:
    h = held()
    if sym in h:
        return {**h[sym], "as": sym}
    alias = us_alias().get(sym)
    if alias and alias in h:
        return {**h[alias], "as": alias}
    return None


# --------------------------------------------------------------------------
# calendar sources
# --------------------------------------------------------------------------

def _day_ttl(d: date) -> float:
    t = today_et()
    if d < t - timedelta(days=3):
        return 30 * 86400
    if d <= t:
        return 900
    return 6 * 3600


def _load_nasdaq_day(d: date) -> dict:
    raw = net.get(f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}", NASDAQ_H, timeout=20)
    data = (json.loads(raw).get("data") or {})
    rows = []
    for r in data.get("rows") or []:
        tm = (r.get("time") or "").lower()
        rows.append({
            "sym": ysym(r.get("symbol")), "name": (r.get("name") or "").strip(),
            "when": "bmo" if "pre-market" in tm else ("amc" if "after-hours" in tm else None),
            "eps_est": _money(r.get("epsForecast")), "n_est": _f(r.get("noOfEsts")),
            "mcap": _money(r.get("marketCap")), "fq": r.get("fiscalQuarterEnding"),
            "ly_eps": _money(r.get("lastYearEPS")), "ly_date": r.get("lastYearRptDt"),
            "eps_act": _money(r.get("eps")), "surprise": _f(r.get("surprise")),
        })
    return {"date": d.isoformat(), "rows": rows}


def nasdaq_day(d: date) -> list[dict]:
    try:
        return store.cache_through(f"ern:nq:{d.isoformat()}", _day_ttl(d),
                                   lambda: _load_nasdaq_day(d)).get("rows") or []
    except Exception:
        return []


def _load_finnhub_week(mon: date) -> dict:
    key = newsmod.api_key()
    if not key:
        return {"rows": [], "why": "no Finnhub key"}
    raw = net.get(f"https://finnhub.io/api/v1/calendar/earnings?from={mon.isoformat()}"
                  f"&to={(mon + timedelta(days=6)).isoformat()}&token={key}", {}, timeout=20)
    rows = []
    for r in json.loads(raw).get("earningsCalendar") or []:
        rows.append({"sym": ysym(r.get("symbol")), "date": r.get("date"),
                     "when": (r.get("hour") or None) if r.get("hour") in ("bmo", "amc", "dmh") else None,
                     "eps_est": _f(r.get("epsEstimate")), "rev_est": _f(r.get("revenueEstimate")),
                     "eps_act": _f(r.get("epsActual")), "rev_act": _f(r.get("revenueActual")),
                     "q": r.get("quarter"), "y": r.get("year")})
    return {"rows": rows}


def finnhub_week(mon: date) -> list[dict]:
    t = today_et()
    ttl = 30 * 86400 if mon + timedelta(days=9) < t else (2 * 3600 if mon <= t else 6 * 3600)
    try:
        return store.cache_through(f"ern:fh:{mon.isoformat()}", ttl,
                                   lambda: _load_finnhub_week(mon)).get("rows") or []
    except Exception:
        return []


def yahoo_dates(sym: str) -> list[dict]:
    """The per-holding dates the morning brief already keeps (two days old at most)."""
    from . import calendar as cal
    k = f"earn:{sym}"
    got, _hit = cal.cached_dates(sym)
    if got is None:
        got = _safe(lambda: cal._earnings_dates(sym)) or {"ok": False, "transient": True}
        store.cache_put(k, got)
    dates = got.get("dates") or [] if got.get("ok") else []
    # Yahoo gives the time on the exchange's clock. For an Amsterdam or
    # Frankfurt line that is Central European time; everything here counts
    # in New York time, so convert once, at the source.
    if ET and re.search(r"\.(AS|DE|PA|BR|MI|MC|F|XETRA|SW|VI|LS|HE|CO|ST|OL)$", sym):
        out = []
        for x in dates:
            if x.get("time"):
                try:
                    h, m = (int(v) for v in x["time"].split(":")[:2])
                    t = datetime(*map(int, x["date"].split("-")), h, m, tzinfo=AMS).astimezone(ET)
                    x = {**x, "time": t.strftime("%H:%M"), "date_et": t.date().isoformat()}
                except Exception:
                    pass
            out.append(x)
        return out
    return dates


def monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


# --------------------------------------------------------------------------
# the calendar
# --------------------------------------------------------------------------

def _status(d: str, eps_act) -> str:
    t = today_et().isoformat()
    if eps_act is not None:
        return "reported"
    return "past" if d < t else ("today" if d == t else "upcoming")


def week(week_of: Optional[str] = None, scope: str = "market") -> dict:
    """
    One week of reports, Monday to Sunday. `scope`: mine (holdings and
    watched), market (plus every company above $20bn), all (everything).
    """
    mon = monday(_dd(week_of) if week_of else today_et())
    days = [mon + timedelta(days=i) for i in range(7)]
    with ThreadPoolExecutor(8) as ex:
        nq = dict(zip(days, ex.map(nasdaq_day, days)))
        fh = ex.submit(finnhub_week, mon).result()
    hd, al, wl = held(), us_alias(), set(watchlist())
    fh_by = {(r["sym"], r["date"]): r for r in fh}
    fh_sym = {}
    for r in fh:
        fh_sym.setdefault(r["sym"], r)

    events = {}
    for d in days:
        ds = d.isoformat()
        for r in nq[d]:
            e = events.setdefault((r["sym"], ds), {"sym": r["sym"], "date": ds, "src": []})
            e.update({k: v for k, v in r.items() if v is not None and k != "sym"})
            e["src"].append("nasdaq")
    for r in fh:
        if not r.get("date"):
            continue
        e = events.get((r["sym"], r["date"]))
        if e is None:
            # Finnhub knows things Nasdaq has not listed (and lists odd
            # tickers); keep only the ones that matter here.
            if r["sym"] not in hd and r["sym"] not in wl and r["sym"] not in al:
                continue
            e = events.setdefault((r["sym"], r["date"]), {"sym": r["sym"], "date": r["date"], "src": []})
        e["src"].append("finnhub")
        for k in ("when", "rev_est", "rev_act"):
            if r.get(k) is not None and e.get(k) is None:
                e[k] = r[k]
        if e.get("eps_est") is None and r.get("eps_est") is not None:
            e["eps_est"] = r["eps_est"]
        if e.get("eps_act") is None and r.get("eps_act") is not None:
            e["eps_act"] = r["eps_act"]
        e["fh_q"], e["fh_y"] = r.get("q"), r.get("y")

    # Holdings the US calendars do not carry: the European lines, from Yahoo.
    listed = {e["sym"] for e in events.values()} | set(al)
    for sym, h in hd.items():
        if sym in listed and not re.search(r"\.[A-Z]{1,3}$", sym):
            continue
        for x in yahoo_dates(sym):
            if not (mon.isoformat() <= x["date"] <= days[-1].isoformat()):
                continue
            us = next((u for u, s in al.items() if s == sym), None)
            # Yahoo dates a European release by its own clock; the US
            # listing's calendar can be a day either side. One event, not two.
            if us and any((us, (_dd(x["date"]) + timedelta(days=k)).isoformat()) in events for k in (-1, 0, 1)):
                continue
            e = events.setdefault((sym, x["date"]), {"sym": sym, "date": x["date"], "src": [],
                                                     "name": h["name"].title()})
            e["src"].append("yahoo")
            e.setdefault("time_et", x.get("time"))
            if x.get("eps_estimate") is not None:
                e.setdefault("eps_est", x["eps_estimate"])
            if x.get("eps_actual") is not None:
                e.setdefault("eps_act", x["eps_actual"])

    # Exact times for what you own.
    for (sym, ds), e in events.items():
        hs = al.get(sym, sym)
        if hs in hd and not e.get("time_et"):
            x = next((x for x in yahoo_dates(hs) if x["date"] == ds), None)
            if x and x.get("time"):
                e["time_et"] = x["time"]
        if e.get("time_et") and not e.get("when"):
            e["when"] = when_of(e["time_et"])

    # Read-through: a reporter that sells to, or buys from, something you hold.
    def _rt():
        from . import supply
        return supply, supply.read_through()
    sup, rt = _safe(_rt) or (None, {})

    out_days = []
    for d in days:
        ds = d.isoformat()
        evs = []
        for (sym, dd_), e in events.items():
            if dd_ != ds:
                continue
            hs = al.get(sym, sym)
            pos = hd.get(hs)
            e["chain"] = _safe(lambda: sup.chain_tag(hs, rt)) if sup else None
            e["held"] = {"weight": _r(pos["weight"]), "value_eur": _r(pos["value_eur"], 0), "as": hs} if pos else None
            e["watch"] = sym in wl or hs in wl
            e["big"] = (e.get("mcap") or 0) >= MEGA
            e["status"] = _status(ds, e.get("eps_act"))
            if e.get("eps_act") is not None and e.get("surprise") is None:
                e["surprise"] = _surp(e["eps_act"], e.get("eps_est"))
            e["rev_surprise"] = _surp(e.get("rev_act"), e.get("rev_est"))
            tm = e.get("time_et") or TYPICAL_ET.get(e.get("when") or "")
            e["time_local"] = local_time(ds, tm)
            e["time_exact"] = bool(e.get("time_et"))
            evs.append(e)
        mine = [e for e in evs if e["held"] or e["watch"]]
        rest = sorted([e for e in evs if not (e["held"] or e["watch"])], key=lambda e: -(e.get("mcap") or 0))
        if scope == "mine":
            keep = mine
        elif scope == "all":
            keep = mine + rest[:250]
        else:
            # A small company that supplies three of your holdings matters
            # more to you than a large one that touches none.
            strong = lambda e: bool((e.get("chain") or {}).get("strong"))
            tied = [e for e in rest if strong(e)]
            keep = mine + tied + [e for e in rest if not strong(e) and (e.get("mcap") or 0) >= BIG][:30]
        keep.sort(key=lambda e: (not e["held"], -((e["held"] or {}).get("weight") or 0),
                                 not e["watch"], not (e.get("chain") or {}).get("strong"), -(e.get("mcap") or 0)))
        out_days.append({"date": ds, "events": keep, "total": len(evs),
                         "hidden": len(evs) - len(keep)})
    mine_all = [e for dd_ in out_days for e in dd_["events"] if e["held"]]
    return {
        "week_of": mon.isoformat(), "scope": scope, "today": today_et().isoformat(),
        "days": out_days,
        "summary": {"events": sum(d["total"] for d in out_days),
                    "held": len(mine_all),
                    "held_value_eur": round(sum((e["held"]["value_eur"] or 0) for e in mine_all)),
                    "held_weight": round(sum((e["held"]["weight"] or 0) for e in mine_all), 2),
                    "mega": [e["sym"] for dd_ in out_days for e in dd_["events"] if e["big"]][:12]},
        "sources": {"nasdaq": sum(len(v) for v in nq.values()), "finnhub": len(fh),
                    "finnhub_key": bool(newsmod.api_key())},
    }


def _load_exposure(mon: date) -> dict:
    w = week(mon.isoformat(), "mine")
    evs = [e for d in w["days"] for e in d["events"]
           if e.get("held") and e.get("status") in ("upcoming", "today")]

    def one(e):
        sym = e["sym"]
        cat = _safe(lambda: research.catalysts(sym)) or {}
        o = (cat.get("options") or {}).get("earnings") or {}
        ce = (cat.get("earnings") or {}).get("date")
        # The options answer is for the NEXT report; only use it when that is this one.
        if not o or (ce and abs((_dd(ce) - _dd(e["date"])).days) > 3):
            return {"sym": sym, "date": e["date"], "move": None, "eur": None,
                    "value_eur": e["held"]["value_eur"], "weight": e["held"]["weight"]}
        mv = o.get("event_move_pct") or o.get("move_pct")
        v = e["held"]["value_eur"] or 0
        return {"sym": sym, "date": e["date"], "move": mv, "eur": round(v * mv / 100) if mv else None,
                "value_eur": v, "weight": e["held"]["weight"], "expiry": o.get("expiry")}

    with ThreadPoolExecutor(6) as ex:
        rows = list(ex.map(one, evs))
    priced = [r for r in rows if r["eur"] is not None]
    return {"week_of": mon.isoformat(), "rows": rows,
            "eur": sum(r["eur"] for r in priced),
            # Moves are not all in the same direction on the same day; the sum
            # of the swings is the most the week could move you, not the likely.
            "value_eur": round(sum(r["value_eur"] or 0 for r in rows)),
            "priced": len(priced), "n": len(rows)}


def exposure(week_of: Optional[str] = None) -> dict:
    mon = monday(_dd(week_of) if week_of else today_et())
    return store.cache_through(f"ern:expo:{mon.isoformat()}", 1800, lambda: _load_exposure(mon))


def upcoming_mine(days: int = 100) -> list[dict]:
    """The next report of every holding and watched name, from Yahoo and Nasdaq."""
    hd, wl, t = held(), watchlist(), today_et()
    out = []
    for sym in list(hd) + [w for w in wl if w not in hd]:
        pos = hd.get(sym)
        nxt = None
        for x in yahoo_dates(sym) if pos else []:
            if x["date"] >= t.isoformat():
                nxt = x if nxt is None or x["date"] < nxt["date"] else nxt
        if nxt is None and not pos:
            st = _safe(lambda: research.street(sym)) or {}
            up = st.get("upcoming")
            if up:
                nxt = {"date": up["date"], "time": None, "eps_estimate": up.get("eps_est")}
        if not nxt or _dd(nxt["date"]) > t + timedelta(days=days):
            continue
        when = when_of(nxt.get("time"))
        out.append({"sym": sym, "name": (pos or {}).get("name") or sym, "date": nxt["date"],
                    "days": (_dd(nxt["date"]) - t).days, "when": when, "time_exact": bool(nxt.get("time")),
                    "time_local": local_time(nxt["date"], nxt.get("time") or TYPICAL_ET.get(when or "")),
                    "eps_est": nxt.get("eps_estimate"),
                    "held": {"weight": _r(pos["weight"]), "value_eur": _r(pos["value_eur"], 0)} if pos else None,
                    "watch": sym in wl})
    out.sort(key=lambda e: (e["date"], -((e["held"] or {}).get("weight") or 0)))
    return out


def ics(days: int = 120) -> str:
    """Your holdings' and watched names' next reports, for a calendar app."""
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Het Koersbord//ERN//EN",
             "CALSCALE:GREGORIAN", "X-WR-CALNAME:Earnings — your book"]
    for e in upcoming_mine(days):
        ts = event_ts(e["date"], e["when"])
        tag = "held" if e["held"] else "watched"
        desc = (f"{e['name']} reports {WHEN_LABEL.get(e['when'] or '', 'time not announced')}. "
                f"Consensus EPS {e['eps_est'] if e['eps_est'] is not None else 'n/a'}. "
                + (f"Your position: {e['held']['weight']}% of the book." if e["held"] else "Watched."))
        lines += ["BEGIN:VEVENT", f"UID:ern-{e['sym']}-{e['date']}@personal-bloomberg", f"DTSTAMP:{now}"]
        if ts:
            st = datetime.fromtimestamp(ts, timezone.utc)
            lines += [f"DTSTART:{st.strftime('%Y%m%dT%H%M%SZ')}",
                      f"DTEND:{(st + timedelta(minutes=60)).strftime('%Y%m%dT%H%M%SZ')}"]
        else:
            d = e["date"].replace("-", "")
            lines += [f"DTSTART;VALUE=DATE:{d}"]
        lines += [f"SUMMARY:{e['sym']} earnings ({tag})", f"DESCRIPTION:{desc}",
                  f"URL:http://127.0.0.1:8090/m/ERN/{e['sym']}?d={e['date']}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------------
# the season so far
# --------------------------------------------------------------------------

def season_start(t: Optional[date] = None) -> date:
    """
    Reporting seasons open with the big banks around the 12th of January,
    April, July and October. Before that date, it is still last season.
    """
    t = t or today_et()
    q_month = ((t.month - 1) // 3) * 3 + 1
    start = date(t.year, q_month, 8)
    if t < start + timedelta(days=4):
        m = q_month - 3
        start = date(t.year - (1 if m < 1 else 0), m + 12 if m < 1 else m, 8)
    return start


def _reaction_for(sym: str, d: str, when: Optional[str]) -> Optional[dict]:
    """
    The session a report moved. Yahoo's record knows whether it came before
    the open or after the close; Nasdaq's calendar mostly does not.
    """
    s = next((s for s in _street(sym).get("surprises") or [] if s["date"] == d), None)
    if s and s.get("reaction"):
        return s["reaction"]
    bars = (_safe(lambda: research.daily(sym)) or {}).get("bars") or []
    if not bars or when not in ("amc", "bmo"):
        return None
    days_ = [b[0] for b in bars]
    import bisect
    i = bisect.bisect_right(days_, d) if when == "amc" else bisect.bisect_left(days_, d)
    if i >= len(bars) or i == 0:
        return None
    b, p = bars[i], bars[i - 1]
    return {"day": b[0], "move": _pct(b[6], p[6]), "gap": _pct(b[1], p[4])}


def _load_season() -> dict:
    start, t = season_start(), today_et()
    ds = [start + timedelta(days=i) for i in range((t - start).days + 1)]
    with ThreadPoolExecutor(8) as ex:
        rows = dict(zip(ds, ex.map(nasdaq_day, ds)))
    hd, al = held(), us_alias()
    mine, big = [], []
    for d, rs in rows.items():
        for r in rs:
            if r.get("eps_act") is None:
                continue
            if (r.get("mcap") or 0) >= 10e9:
                big.append(r)
            hs = al.get(r["sym"], r["sym"])
            if hs in hd:
                mine.append({**r, "date": d.isoformat(), "as": hs, "weight": hd[hs]["weight"],
                             "value_eur": hd[hs]["value_eur"]})
    for m in mine:
        rx = _reaction_for(m["sym"], m["date"], m.get("when"))
        m["reaction"] = rx
        if rx and rx.get("move") is not None and m.get("value_eur"):
            m["eur"] = round(m["value_eur"] * rx["move"] / 100)
    surpr = [r["surprise"] for r in big if r.get("surprise") is not None]
    beat = [s for s in surpr if s > 0]
    mv = [m["reaction"]["move"] for m in mine if m.get("reaction") and m["reaction"].get("move") is not None]
    return {
        "start": start.isoformat(), "days": len(ds),
        "market": {"n": len(surpr), "beat_pct": round(len(beat) / len(surpr) * 100, 1) if surpr else None,
                   "median_surprise": round(statistics.median(surpr), 2) if surpr else None,
                   "miss_pct": round(sum(1 for s in surpr if s < 0) / len(surpr) * 100, 1) if surpr else None},
        "mine": sorted(mine, key=lambda m: m["date"], reverse=True),
        "mine_stats": {"n": len(mine),
                       "beat": sum(1 for m in mine if (m.get("surprise") or 0) > 0),
                       "avg_move": round(statistics.mean(mv), 2) if mv else None,
                       "eur": sum(m.get("eur") or 0 for m in mine),
                       "remaining": len([u for u in upcoming_mine(60) if u["held"]])},
    }


def season() -> dict:
    return store.cache_through("ern:season", 1800, _load_season)


# --------------------------------------------------------------------------
# one event
# --------------------------------------------------------------------------

def _street(sym: str) -> dict:
    return _safe(lambda: research.street(sym)) or {}


def resolve(sym: str, d: Optional[str] = None) -> dict:
    """
    The event's header: who, when, whether it is still ahead, and the reports
    either side of it. With no date, the report in the last ten days if there
    was one, otherwise the next.
    """
    sym = ysym(sym)
    ov = _safe(lambda: research.overview(sym)) or {}
    st = _street(sym)
    past = [s["date"] for s in st.get("surprises") or []]
    nxt = (st.get("upcoming") or {}).get("date") or (ov.get("dates") or {}).get("earnings")
    pos = position(sym)
    if pos and pos["as"] != sym:
        for x in yahoo_dates(pos["as"]):
            if x["date"] >= today_et().isoformat() and (not nxt or x["date"] < nxt):
                nxt = x["date"]
    t = today_et()
    if not d:
        recent = [p for p in past if (t - _dd(p)).days <= 10]
        d = recent[-1] if recent else (nxt or (past[-1] if past else None))
    if not d:
        return {"symbol": sym, "ok": False, "why": "no earnings dates known for this symbol"}
    dates = sorted(set(past + ([nxt] if nxt else []) + [d]))
    i = dates.index(d)
    when, hhmm = None, None
    for s in st.get("surprises") or []:
        if s["date"] == d:
            when = {"after close": "amc", "before open": "bmo", "during session": "dmh"}.get(s.get("when"))
    up = st.get("upcoming") or {}
    if up.get("date") == d:
        when = when or {"after close": "amc", "before open": "bmo", "during session": "dmh"}.get(up.get("when"))
    for x in yahoo_dates(pos["as"] if pos else sym) if pos else []:
        if x["date"] == d and x.get("time"):
            hhmm = x["time"]
            when = when or when_of(hhmm)
    if not when:
        r = next((r for r in nasdaq_day(_dd(d)) if r["sym"] == sym), None) if abs((_dd(d) - t).days) < 60 else None
        when = (r or {}).get("when") or when
    ts = event_ts(d, when, hhmm)
    from . import earnlive
    live = earnlive.state(sym, d)
    # Today's report counts as out once the filing is found, or twenty
    # minutes after the usual release time.
    reported = d < t.isoformat() or (d == t.isoformat() and bool(
        (live or {}).get("found") or (ts and time.time() > ts + 1200)))
    if any(s["date"] == d and s.get("eps_act") is not None for s in st.get("surprises") or []):
        reported = True
    return {
        "symbol": sym, "ok": True, "name": ov.get("name") or sym, "short": ov.get("short") or sym,
        "date": d, "when": when, "when_label": WHEN_LABEL.get(when or "", "time not announced"),
        "time_et": hhmm or TYPICAL_ET.get(when or ""), "time_exact": bool(hhmm),
        "time_local": local_time(d, hhmm or TYPICAL_ET.get(when or "")),
        "ts": ts, "days": (_dd(d) - t).days, "reported": bool(reported),
        "estimated": bool((ov.get("dates") or {}).get("earnings_estimated")) if d == nxt else False,
        "prev": dates[i - 1] if i > 0 else None, "next": dates[i + 1] if i + 1 < len(dates) else None,
        "all": dates[-12:],
        "quote": ov.get("quote") or {}, "currency": ov.get("currency"),
        "sector": ov.get("sector"), "industry": ov.get("industry"),
        "market_cap": (ov.get("stats") or {}).get("market_cap"),
        "position": pos, "watch": sym in watchlist(),
        "live": live, "ir": ov.get("website"),
        "chain": _chain(pos["as"] if pos else sym),
    }


def _chain(sym: str) -> Optional[dict]:
    """The holdings on either side of a reporter, from the supply-chain map; None when it touches none."""
    def go():
        from . import supply
        d = supply.linked(sym)
        return d if (d["suppliers"] or d["customers"] or d["competitors"]) else None
    return _safe(go)


# ---- expectations --------------------------------------------------------

def _nasdaq_forecast(sym: str) -> dict:
    def load():
        raw = net.get(f"https://api.nasdaq.com/api/analyst/{sym}/earnings-forecast", NASDAQ_H, timeout=20)
        q = ((json.loads(raw).get("data") or {}).get("quarterlyForecast") or {}).get("rows") or []
        return {"rows": [{"fq": r.get("fiscalEnd"), "avg": _f(r.get("consensusEPSForecast")),
                          "high": _f(r.get("highEPSForecast")), "low": _f(r.get("lowEPSForecast")),
                          "n": _f(r.get("noOfEstimates")), "up": _f(r.get("up")), "down": _f(r.get("down"))}
                         for r in q]}
    return _safe(lambda: store.cache_through(f"ern:nqf:{sym}", 6 * 3600, load), {}) or {}


def snapshot(sym: str, d: str) -> Optional[dict]:
    return store.cache_get(f"ern:snap:{sym}:{d}")


def take_snapshot(sym: str, d: str, force: bool = False) -> Optional[dict]:
    """
    Freeze what was expected, while it is still ahead. Taken whenever the
    event is looked at in its last ten days, and by the live watcher on the
    day; the last one before the report wins.
    """
    t = today_et()
    if _dd(d) < t:
        return snapshot(sym, d)
    if not force and (_dd(d) - t).days > 10:
        return None
    if _dd(d) == t:
        # Never after the release: by then the consensus is on its way to
        # the next quarter.
        old = snapshot(sym, d)
        if old and old.get("release_ts") and time.time() > old["release_ts"] - 300:
            return old
    st = _street(sym)
    q0 = next((p for p in st.get("periods") or [] if p["period"] == "0q"), {})
    q1 = next((p for p in st.get("periods") or [] if p["period"] == "+1q"), {})
    cat = _safe(lambda: research.catalysts(sym)) or {}
    opt = ((cat.get("options") or {}).get("earnings") or {})
    ov = _safe(lambda: research.overview(sym)) or {}
    fh = next((r for r in finnhub_week(monday(_dd(d))) if r["sym"] == sym and r["date"] == d), {})
    snap = {
        "taken": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "eps": (q0.get("eps") or {}), "rev": (q0.get("rev") or {}),
        "trend": q0.get("trend"), "revisions": q0.get("revisions"),
        "fh_eps": fh.get("eps_est"), "fh_rev": fh.get("rev_est"),
        "implied": {"move": opt.get("event_move_pct") or opt.get("move_pct"),
                    "straddle_move": opt.get("move_pct"), "expiry": opt.get("expiry"),
                    "iv": opt.get("iv")} if opt else None,
        "price": (ov.get("quote") or {}).get("price"),
        # Next quarter as expected before the report: what guidance is judged against.
        "next": {"eps": (q1.get("eps") or {}).get("avg"), "rev": (q1.get("rev") or {}).get("avg")},
        "release_ts": _release_ts(sym, d),
    }
    store.cache_put(f"ern:snap:{sym}:{d}", snap)
    return snap


def _release_ts(sym: str, d: str) -> Optional[float]:
    st = _street(sym)
    up = st.get("upcoming") or {}
    if up.get("date") != d:
        return None
    when = {"after close": "amc", "before open": "bmo", "during session": "dmh"}.get(up.get("when"))
    return event_ts(d, when)


def consensus_for(sym: str, d: str) -> dict:
    """The consensus to judge a report on `d` against: the snapshot, or the best record of it."""
    snap = snapshot(sym, d) if _dd(d) <= today_et() else None
    if _dd(d) >= today_et() and not snap:
        snap = take_snapshot(sym, d, force=True)
    eps = rev = n_eps = n_rev = lo = hi = rlo = rhi = None
    src = None
    if snap:
        e, r = snap.get("eps") or {}, snap.get("rev") or {}
        eps, lo, hi, n_eps = e.get("avg"), e.get("low"), e.get("high"), e.get("n")
        rev, rlo, rhi, n_rev = r.get("avg"), r.get("low"), r.get("high"), r.get("n")
        eps = eps if eps is not None else snap.get("fh_eps")
        rev = rev if rev is not None else snap.get("fh_rev")
        src = f"snapshot {snap.get('taken', '')[:16].replace('T', ' ')} UTC"
    if eps is None:
        s = next((s for s in _street(sym).get("surprises") or [] if s["date"] == d), None)
        if s:
            eps, src = s.get("eps_est"), "Yahoo's record of the estimate"
    if eps is None or rev is None:
        r = next((r for r in nasdaq_day(_dd(d)) if r["sym"] == sym), None)
        if r and eps is None:
            eps, n_eps, src = r.get("eps_est"), r.get("n_est"), "Nasdaq (Zacks) consensus"
        fh = next((r for r in finnhub_week(monday(_dd(d))) if r["sym"] == sym and r["date"] == d), None)
        if fh:
            rev = rev if rev is not None else fh.get("rev_est")
            eps = eps if eps is not None else fh.get("eps_est")
    return {"eps": eps, "eps_low": lo, "eps_high": hi, "n_eps": n_eps,
            "rev": rev, "rev_low": rlo, "rev_high": rhi, "n_rev": n_rev,
            "implied": (snap or {}).get("implied"), "price_before": (snap or {}).get("price"),
            "src": src or "none found", "snapshot": bool(snap)}


def _prev_docs(sym: str, prev: Optional[str], name: Optional[str]) -> dict:
    """Last quarter's release and call: what the company promised for this one."""
    if not prev:
        return {}
    out = {}
    rf = _safe(lambda: ed.report_filings(sym, prev, name)) or {}
    rel = (rf.get("release") or {}).get("doc")
    if rel:
        p = _safe(lambda: release_parsed(sym, prev, name)) or {}
        out["guidance"] = [g for g in p.get("guidance") or []][:10]
        out["release_url"] = rel["url"]
    tr = _safe(lambda: transcript(sym, prev))
    if tr and tr.get("ok"):
        out["takeaways"] = tr["tr"].get("takeaways") or []
        out["call_guidance"] = (tr.get("analytics") or {}).get("guidance", [])[:8]
        out["transcript_url"] = tr["tr"].get("url")
    return out


def expect(sym: str, d: str) -> dict:
    ev = resolve(sym, d)
    st = _street(sym)
    periods = {p["period"]: p for p in st.get("periods") or []}
    cons = consensus_for(sym, d)
    if not ev.get("reported"):
        take_snapshot(sym, d)
    q0 = periods.get("0q") if not ev.get("reported") else None
    nqf = (_nasdaq_forecast(sym).get("rows") or [])[:2]
    prev = _prev_docs(sym, ev.get("prev"), ev.get("name"))
    # Guidance against the consensus: last quarter's range for this quarter.
    vs_guide = []
    for g in prev.get("guidance") or []:
        if g.get("low") is None or g.get("unit") != "$":
            continue
        mid = (g["low"] + g["high"]) / 2
        per = (g.get("period") or "").lower()
        full_year = bool(re.search(r"full|fiscal|year|fy", per))
        if g["metric"] == "revenue" and cons.get("rev") and not full_year and 0.5 < mid / cons["rev"] < 2:
            vs_guide.append({"metric": "revenue", "low": g["low"], "high": g["high"], "mid": mid,
                             "consensus": cons["rev"], "gap_pct": _pct(cons["rev"], mid), "text": g["text"]})
        if g["metric"] == "eps" and cons.get("eps") and not full_year and 0.5 < mid / cons["eps"] < 2:
            vs_guide.append({"metric": "eps", "low": g["low"], "high": g["high"], "mid": mid,
                             "consensus": cons["eps"], "gap_pct": _pct(cons["eps"], mid), "text": g["text"]})
    biz = _safe(lambda: research.business(sym)) or {}
    segs = []
    for g in biz.get("groups") or []:
        if g.get("kind") in ("segment", "product"):
            for m in g.get("members") or []:
                if m.get("q") and m.get("q_yoy") is not None:
                    segs.append({"label": m["label"], "q": m["q"], "yoy": m["q_yoy"], "share": m.get("q_share"),
                                 "end": m.get("q_end"), "kind": g["kind"]})
            if segs:
                break
    return {
        "symbol": ev["symbol"], "date": d, "reported": ev.get("reported"),
        "consensus": cons,
        "quarter": q0, "next_quarter": periods.get("+1q"), "year": periods.get("0y"),
        "nasdaq": nqf,
        "dispersion": (round((cons["eps_high"] - cons["eps_low"]) / abs(cons["eps"]) * 100, 1)
                       if cons.get("eps_high") is not None and cons.get("eps_low") is not None and cons.get("eps")
                       else None),
        "guidance_prev": prev.get("guidance") or [],
        "guidance_vs": vs_guide,
        "call_guidance_prev": prev.get("call_guidance") or [],
        "watch": (prev.get("takeaways") or [])[:10],
        "segments": sorted(segs, key=lambda s: -(s.get("share") or 0))[:8],
        "prev_date": ev.get("prev"),
        "prev_sources": {"release": prev.get("release_url"), "transcript": prev.get("transcript_url")},
        "targets": st.get("targets"),
    }


# ---- the playbook: what usually happens -----------------------------------

def _bars(sym: str) -> list:
    return (_safe(lambda: research.daily(sym)) or {}).get("bars") or []


def history(sym: str) -> dict:
    """Every past report with the move on the day, the drift after, and the market."""
    st = _street(sym)
    bars = _bars(sym)
    idx = {b[0]: i for i, b in enumerate(bars)}
    days_ = [b[0] for b in bars]
    spy = {b[0]: b for b in _bars("SPY")}
    import bisect
    rows = []
    for s in st.get("surprises") or []:
        rx = s.get("reaction") or {}
        day = rx.get("day")
        rec = {"date": s["date"], "when": s.get("when"), "eps_est": s.get("eps_est"), "eps_act": s.get("eps_act"),
               "surprise": s.get("surprise_pct"), "move": rx.get("move"), "gap": rx.get("gap"), "day": day}
        if day in idx:
            i = idx[day]
            if i >= 1:
                pc = bars[i - 1][6]
                if i + 5 < len(bars):
                    rec["d5"] = _pct(bars[i + 5][6], pc)
                    rec["drift"] = _pct(bars[i + 5][6], bars[i][6])
                if i >= 21:
                    rec["runup"] = _pct(pc, bars[i - 21][6])
                p0, p1 = spy.get(bars[i - 1][0]), spy.get(day)
                if p0 and p1:
                    rec["mkt"] = _pct(p1[6], p0[6])
                    if rec["move"] is not None and rec["mkt"] is not None:
                        rec["excess"] = round(rec["move"] - rec["mkt"], 2)
                rec["intraday"] = _pct(bars[i][4], bars[i][1])     # open to close: faded or followed
        rows.append(rec)
    rows.sort(key=lambda r: r["date"])
    return {"rows": rows, "source": st.get("source")}


def _stats(rows: list[dict]) -> dict:
    done = [r for r in rows if r.get("move") is not None and r.get("surprise") is not None]
    if not done:
        return {"n": 0}
    mv = [abs(r["move"]) for r in done]
    beats = [r for r in done if r["surprise"] > 0]
    miss = [r for r in done if r["surprise"] < 0]
    big = [r for r in done if r["surprise"] >= 5]
    small = [r for r in done if 0 < r["surprise"] < 5]

    def grp(xs):
        if not xs:
            return {"n": 0}
        m = [x["move"] for x in xs]
        return {"n": len(xs), "avg": round(statistics.mean(m), 2), "median": round(statistics.median(m), 2),
                "up": sum(1 for v in m if v > 0),
                "d5": round(statistics.mean([x["d5"] for x in xs if x.get("d5") is not None]), 2)
                if any(x.get("d5") is not None for x in xs) else None}

    ups = [r for r in done if r.get("runup") is not None]
    hot = [r for r in ups if r["runup"] >= 8]
    faded = [r for r in done if r.get("gap") is not None and r.get("intraday") is not None
             and abs(r["gap"]) >= 2 and (r["gap"] > 0) != (r["intraday"] > 0)]
    return {
        "n": len(done), "beat_rate": round(len(beats) / len(done) * 100),
        "avg_abs_move": round(statistics.mean(mv), 2), "median_abs_move": round(statistics.median(mv), 2),
        "max_up": max(r["move"] for r in done), "max_down": min(r["move"] for r in done),
        "beat": grp(beats), "big_beat": grp(big), "small_beat": grp(small), "miss": grp(miss),
        "beat_fell": sum(1 for r in beats if r["move"] < 0),
        "hot": grp(hot), "hot_threshold": 8,
        "faded": len(faded), "gapped": sum(1 for r in done if r.get("gap") is not None and abs(r["gap"]) >= 2),
        "avg_surprise": round(statistics.mean(r["surprise"] for r in done), 2),
        "corr_surprise_move": _corr([r["surprise"] for r in done], [r["move"] for r in done]),
    }


def _corr(a, b) -> Optional[float]:
    if len(a) < 5:
        return None
    ma, mb = statistics.mean(a), statistics.mean(b)
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    return round(sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (sa * sb), 2) if sa and sb else None


def playbook(sym: str, d: str) -> dict:
    ev = resolve(sym, d)
    h = history(sym)
    rows = [r for r in h["rows"] if r["date"] < d]
    stats = _stats(rows[-16:])
    cons = consensus_for(sym, d)
    imp = cons.get("implied")
    if not imp and not ev.get("reported"):
        cat = _safe(lambda: research.catalysts(sym)) or {}
        o = (cat.get("options") or {}).get("earnings") or {}
        if o:
            imp = {"move": o.get("event_move_pct") or o.get("move_pct"), "straddle_move": o.get("move_pct"),
                   "expiry": o.get("expiry"), "iv": o.get("iv")}
    bars = _bars(sym)
    runup = None
    if len(bars) > 21 and not ev.get("reported"):
        runup = _pct(bars[-1][6], bars[-21][6])
        spy = _bars("SPY")
        if len(spy) > 21:
            runup = {"stock": runup, "market": _pct(spy[-1][6], spy[-21][6])}
    pos = ev.get("position")
    eur = None
    if pos and pos.get("value_eur"):
        v = pos["value_eur"]
        eur = {"value": v,
               "implied": round(v * imp["move"] / 100) if imp and imp.get("move") else None,
               "avg": round(v * stats["avg_abs_move"] / 100) if stats.get("n") else None,
               "beat": round(v * stats["beat"]["median"] / 100) if stats.get("beat", {}).get("n") else None,
               "miss": round(v * stats["miss"]["median"] / 100) if stats.get("miss", {}).get("n") else None,
               "worst": round(v * stats["max_down"] / 100) if stats.get("n") else None}
    # In words: what the record says, and what it cannot.
    say = []
    if stats.get("n"):
        say.append(f"{ev['short']} beat the EPS consensus in {stats['beat_rate']}% of the last {stats['n']} reports "
                   f"(average surprise {stats['avg_surprise']:+.1f}%). A beat is the norm, so it is the size of the "
                   f"beat and the guidance that move the stock.")
        if stats["beat"].get("n"):
            say.append(f"On beats the stock moved {stats['beat']['median']:+.1f}% (median) and rose "
                       f"{stats['beat']['up']} of {stats['beat']['n']} times; "
                       f"{stats['beat_fell']} beat{'s' if stats['beat_fell'] != 1 else ''} still fell.")
        if stats["miss"].get("n"):
            say.append(f"On misses: {stats['miss']['median']:+.1f}% (median), down "
                       f"{stats['miss']['n'] - stats['miss']['up']} of {stats['miss']['n']}.")
        else:
            say.append("It has not missed in this record, so there is no miss reaction to learn from — "
                       "the first one would likely be punished harder than the average move suggests.")
        if imp and imp.get("move"):
            ratio = imp["move"] / stats["avg_abs_move"] if stats["avg_abs_move"] else None
            if ratio:
                say.append(f"Options price a ±{imp['move']:.1f}% move against a {stats['avg_abs_move']:.1f}% average "
                           f"actual move — " + ("cheaper than history: the market expects a quiet report."
                                                if ratio < 0.85 else
                                                "richer than history: the market expects a big one." if ratio > 1.15
                                                else "in line with history."))
        if stats.get("hot", {}).get("n", 0) >= 2:
            say.append(f"When it ran up {stats['hot_threshold']}%+ in the month before, it moved "
                       f"{stats['hot']['median']:+.1f}% (median, {stats['hot']['n']} times).")
        if stats.get("gapped"):
            say.append(f"{stats['faded']} of {stats['gapped']} big opening gaps reversed during the day — "
                       "the first print is not always the verdict.")
        if stats.get("corr_surprise_move") is not None:
            c = stats["corr_surprise_move"]
            say.append(f"The size of the EPS surprise explains {'little' if abs(c) < 0.3 else 'some' if abs(c) < 0.6 else 'much'} "
                       f"of the move (correlation {c:+.2f}): " +
                       ("guidance, margins and the segments investors watch matter more than the headline."
                        if abs(c) < 0.3 else "the headline number does drive this one."))
    if runup and isinstance(runup, dict) and runup.get("stock") is not None:
        rel = runup["stock"] - (runup.get("market") or 0)
        say.append(f"Into this report it is {runup['stock']:+.1f}% over the last month "
                   f"({rel:+.1f}pp against the S&P 500)" +
                   (" — a lot is priced in." if rel > 8 else " — expectations were marked down on the way in." if rel < -8 else "."))
    return {"symbol": ev["symbol"], "date": d, "history": h["rows"][-20:], "stats": stats,
            "implied": imp, "runup": runup, "eur": eur, "say": say,
            "position": pos, "reported": ev.get("reported")}


# ---- results --------------------------------------------------------------

def release_parsed(sym: str, d: str, name: Optional[str] = None, max_age: float = 6 * 3600) -> dict:
    """The press release for the report on d, read. Kept once found."""
    from . import BUILD
    k = f"ern:rel:{sym}:{d}"
    got = store.cache_get(k)
    # Kept once found — a filing does not change — but read again by a newer
    # build, whose parser may read it better.
    if got and got.get("ok") and got.get("_build") == BUILD:
        return got
    rf = ed.report_filings(sym, d, name, max_age=max_age)
    rel = rf.get("release")
    if not rel or not rel.get("doc"):
        out = {"ok": False, "why": rf.get("why") or "no earnings release on EDGAR yet", "filer": rf.get("filer")}
        return out
    cons = consensus_for(sym, d)
    raws = [ed.fetch(rel["doc"]["url"])]
    for x in rel["extras"]:
        if x["kind"] in ("release", "supplement") and not x["doc"].lower().endswith(".pdf"):
            raws.append(_safe(lambda: ed.fetch(x["url"]), b""))
            if len(raws) >= 3:
                break
    p = ed.parse_release(raws, cons.get("rev"), cons.get("eps"))
    out = {"ok": True, "filing": {k2: rel[k2] for k2 in ("form", "date", "acc", "accepted", "items", "index")},
           "doc": rel["doc"], "extras": rel["extras"], "periodic": rf.get("periodic"),
           "filer": rf.get("filer"), "parsed": p, "consensus": cons,
           "parsed_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    # guidance/highlights come straight from here too
    out["guidance"] = p.get("guidance")
    store.cache_put(k, out)
    return out


def _cmp(actual, est, lo=None, hi=None) -> Optional[dict]:
    a, e = _f(actual), _f(est)
    if a is None:
        return None
    out = {"actual": a, "est": e, "surprise": _surp(a, e)}
    if lo is not None and hi is not None:
        out["range"] = [lo, hi]
        out["outside"] = "above" if a > hi else ("below" if a < lo else None)
    return out


def results(sym: str, d: str) -> dict:
    ev = resolve(sym, d)
    if not ev.get("reported") and not (ev.get("live") or {}).get("found"):
        return {"symbol": ev["symbol"], "date": d, "reported": False,
                "why": f"{ev['short']} reports on {d} {ev['when_label']}."}
    cons = consensus_for(ev["symbol"], d)
    rel = _safe(lambda: release_parsed(ev["symbol"], d, ev.get("name")), {}) or {}
    p = rel.get("parsed") or {}
    usd = p.get("currency", "USD") == "USD"
    # The reported figures the data vendors record, on the consensus's basis.
    st = next((s for s in _street(ev["symbol"]).get("surprises") or [] if s["date"] == d), {})
    nq = next((r for r in nasdaq_day(_dd(d)) if r["sym"] == ev["symbol"]), {})
    fh = next((r for r in finnhub_week(monday(_dd(d))) if r["sym"] == ev["symbol"] and r["date"] == d), {})
    vendor_eps = st.get("eps_act") if st.get("eps_act") is not None else (nq.get("eps_act") if nq.get("eps_act") is not None else fh.get("eps_act"))
    eps_key = p.get("eps_basis")
    rel_eps = (p.get(eps_key) or {}).get("value") if eps_key else None
    eps_act = vendor_eps if vendor_eps is not None else (rel_eps if usd else None)
    rev_rel = (p.get("revenue") or {}).get("value") if usd else None
    rev_act = rev_rel if rev_rel is not None else fh.get("rev_act")
    # The filed quarter from XBRL, once the 10-Q is in.
    filed = None
    fund = _safe(lambda: research.fundamentals(ev["symbol"])) or {}
    for q in reversed(fund.get("quarters") or []):
        if q.get("end") and 0 <= (_dd(d) - _dd(q["end"])).days <= 75:
            filed = {k: q.get(k) for k in ("end", "filed", "label", "revenue", "revenue_yoy", "gross_margin",
                                           "op_income", "op_margin", "net_income", "eps", "eps_yoy", "fcf",
                                           "capex", "rpo", "derived")}
            if rev_act is None and filed.get("revenue"):
                rev_act = filed["revenue"]
            break
    # The reaction.
    rx = reaction(ev)
    implied = cons.get("implied") or {}
    if rx and rx.get("move") is not None and implied.get("move"):
        rx["vs_implied"] = round(abs(rx["move"]) / implied["move"], 2)
    pos = ev.get("position")
    if pos and rx and rx.get("move") is not None and pos.get("value_eur"):
        rx["eur"] = round(pos["value_eur"] * rx["move"] / 100)
    # After the report: the analysts.
    after = []
    for a in _street(ev["symbol"]).get("actions") or []:
        if a["date"] >= d and (_dd(a["date"]) - _dd(d)).days <= 10:
            after.append(a)
    q_next = next((p2 for p2 in _street(ev["symbol"]).get("periods") or [] if p2["period"] == "0q"), {})
    rev_trend = q_next.get("trend") or {}
    return {
        "symbol": ev["symbol"], "date": d, "reported": True,
        "eps": _cmp(eps_act, cons.get("eps"), cons.get("eps_low"), cons.get("eps_high")),
        "eps_source": "Yahoo / Nasdaq (the consensus basis)" if vendor_eps is not None else
                      (f"press release ({(p.get(eps_key) or {}).get('label')})" if eps_act is not None else None),
        "eps_release": {k: p.get(k) for k in ("eps_gaap", "eps_adj")},
        "revenue": _cmp(rev_act, cons.get("rev"), cons.get("rev_low"), cons.get("rev_high")),
        "revenue_source": "press release" if rev_rel is not None else ("Finnhub" if fh.get("rev_act") else
                                                                        "10-Q (XBRL)" if filed and filed.get("revenue") else None),
        "revenue_yoy": (p.get("revenue") or {}).get("yoy") if usd else None,
        "release": {"ok": rel.get("ok"), "why": rel.get("why"), "filing": rel.get("filing"),
                    "doc": rel.get("doc"), "net_income": p.get("net_income"), "op_income": p.get("op_income"),
                    "highlights": p.get("highlights") or [], "notes": p.get("notes") or [],
                    "currency": p.get("currency"), "period": p.get("period")},
        "guidance": p.get("guidance") or [],
        "filed": filed,
        "consensus": cons,
        "reaction": rx,
        "analysts_after": after[:20],
        "next_q_trend": rev_trend,
        "live": ev.get("live"),
    }


def reaction(ev: dict) -> Optional[dict]:
    """
    What the stock did: after hours or pre-market while the report is fresh,
    then the first session's close against the close before the report.
    """
    sym, d, when = ev["symbol"], ev["date"], ev.get("when")
    bars = _bars(sym)
    if not bars:
        return None
    days_ = [b[0] for b in bars]
    import bisect
    after = when == "amc"
    i = bisect.bisect_right(days_, d) if after else bisect.bisect_left(days_, d)
    q = ev.get("quote") or {}
    if i < len(bars) and i > 0 and (bars[i][0] != today_et().isoformat() or q.get("market_state") in ("POST", "POSTPOST", "CLOSED")):
        b, p = bars[i], bars[i - 1]
        out = {"phase": "session", "day": b[0], "move": _pct(b[6], p[6]), "gap": _pct(b[1], p[4]),
               "intraday": _pct(b[4], b[1]), "prev_close": p[4], "close": b[4]}
        if i + 5 < len(bars):
            out["d5"] = _pct(bars[i + 5][6], p[6])
        out["since"] = _pct(bars[-1][6], p[6])
        spy = {x[0]: x for x in _bars("SPY")}
        if spy.get(b[0]) and spy.get(p[0]):
            out["mkt"] = _pct(spy[b[0]][6], spy[p[0]][6])
        return out
    # Still in extended hours: the last close against the after-hours price.
    last_close = bars[-1][4] if bars else None
    px = q.get("post_price") if after else q.get("pre_price")
    if px and last_close:
        return {"phase": "after hours" if after else "pre-market", "move": _pct(px, last_close),
                "price": px, "prev_close": last_close}
    return {"phase": "waiting", "move": None}


# ---- docs -----------------------------------------------------------------

def transcript(sym: str, d: str) -> dict:
    """The call: one you added, or the Fool's. With analytics, against last quarter's."""
    mine = [s for s in ed.list_sources(sym, d) if s["kind"] == "transcript"]
    if mine:
        tr = ed.transcript_from_text(mine[-1]["body"], mine[-1]["title"] or "your transcript")
        tr["url"] = mine[-1].get("url")
    else:
        hit = _safe(lambda: ed.find_transcript(sym, d))
        if not hit:
            late = (today_et() - _dd(d)).days > 10
            return {"ok": False, "why": ("the Motley Fool did not publish a transcript of this call (it does not cover "
                                         "every company) — paste one or link it under Sources, or let Claude look on "
                                         "the company's site when writing the report") if late else
                                        ("no transcript yet — the Fool usually posts one within a day or two of the "
                                         "call; you can paste one or add a link under Sources")}
        tr = ed.parse_fool(hit["url"])
    return {"ok": True, "tr": tr}


def docs(sym: str, d: str) -> dict:
    ev = resolve(sym, d)
    out = {"symbol": ev["symbol"], "date": d, "items": [], "user": ed.list_sources(ev["symbol"], d)}
    items = out["items"]
    rf = _safe(lambda: ed.report_filings(ev["symbol"], d, ev.get("name"),
                                         max_age=120 if abs(ev.get("days") or 99) <= 1 else 6 * 3600)) or {}
    rel = rf.get("release")
    if rel:
        items.append({"kind": "8-K" if rel["form"].startswith("8-K") else rel["form"], "title": "Earnings filing",
                      "url": rel["index"], "status": "ok", "when": rel.get("accepted"),
                      "note": f"items {rel.get('items')}" if rel.get("items") else None})
        if rel.get("doc"):
            items.append({"kind": "release", "title": "Press release (" + rel["doc"]["type"] + ")",
                          "url": rel["doc"]["url"], "status": "ok"})
        for x in rel["extras"]:
            items.append({"kind": x["kind"], "title": f"{x['kind'].title()} ({x['type'] or x['doc']})",
                          "url": x["url"], "status": "ok", "note": x.get("desc")})
    else:
        items.append({"kind": "8-K", "title": "Earnings filing", "status": "waiting" if not ev.get("reported") else "missing",
                      "note": (rf.get("why") or "not on EDGAR") if rf else "no SEC filer — a non-US listing"})
    if not any(i["kind"] == "slides" for i in items):
        items.append({"kind": "slides", "title": "Slides", "status": "missing",
                      "note": "not furnished to the SEC — add the link from the company's investor-relations site "
                              "(below), or let Claude look for it when writing the report",
                      "ir": ev.get("ir")})
    per = rf.get("periodic")
    if per:
        items.append({"kind": per["form"], "title": f"{per['form']} for the quarter to {per.get('report')}",
                      "url": per["url"], "status": "ok", "when": per.get("accepted") or per.get("date")})
    else:
        items.append({"kind": "10-Q", "title": "Quarterly report (10-Q)", "status": "waiting",
                      "note": "filed with the release by some companies, up to 40 days later by others"
                      if (rf.get("filer") or {}).get("foreign") is not True else
                      "foreign filers send no 10-Q; the 6-K above is the filing"})
    tr = transcript(ev["symbol"], d) if ev.get("reported") else {"ok": False, "why": "after the call"}
    if tr.get("ok"):
        items.append({"kind": "transcript", "title": tr["tr"].get("title") or "Call transcript",
                      "url": tr["tr"].get("url"), "status": "ok", "when": tr["tr"].get("published"),
                      "note": f"{tr['tr'].get('words', 0):,} words · {tr['tr'].get('source')}"})
    else:
        items.append({"kind": "transcript", "title": "Call transcript", "status": "waiting", "note": tr.get("why")})
    for u in out["user"]:
        items.append({"kind": u["kind"], "title": u["title"], "url": u.get("url"), "status": "ok",
                      "note": f"added by you · {u['words']:,} words", "id": u["id"]})
    out["ir"] = ev.get("ir")
    return out


def call(sym: str, d: str) -> dict:
    ev = resolve(sym, d)
    tr = transcript(ev["symbol"], d)
    if not tr.get("ok"):
        return {"ok": False, "why": tr.get("why")}
    prev = None
    if ev.get("prev"):
        p = _safe(lambda: transcript(ev["symbol"], ev["prev"]))
        prev = p["tr"] if p and p.get("ok") else None
    a = ed.call_analytics(tr["tr"], prev)
    t = tr["tr"]
    return {"ok": True, "symbol": ev["symbol"], "date": d, "url": t.get("url"), "source": t.get("source"),
            "published": t.get("published"), "title": t.get("title"),
            "participants": t.get("participants"), "takeaways": t.get("takeaways"), "risks": t.get("risks"),
            "summary": t.get("summary"), "analytics": a, "prev_date": ev.get("prev") if prev else None,
            "turns": [{"speaker": x["speaker"], "role": x["role"], "section": x["section"],
                       "firm": x.get("firm"), "title": x.get("title"), "text": x["text"]} for x in t.get("turns") or []]}


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

TTL = {"expect": 1800, "playbook": 1800, "results": 120, "docs": 300, "call": 3600}


def part(sym: str, name: str, d: Optional[str] = None) -> dict:
    sym = ysym(sym)
    if name not in PARTS:
        raise KeyError(name)
    if not d:
        d = resolve(sym).get("date")
        if not d:
            return {"ok": False, "why": "no earnings dates known for this symbol"}
    if name == "analysis":
        from . import earnai
        return earnai.analysis(sym, d)
    if name == "report":
        from . import earnai
        return earnai.report_state(sym, d)
    fn = {"expect": expect, "playbook": playbook, "results": results, "docs": docs, "call": call}[name]
    ttl = TTL[name]
    ev_live = abs((_dd(d) - today_et()).days) <= 1
    if name in ("results", "docs") and ev_live:
        ttl = 20
    return store.cache_through(f"ern:{name}:{sym}:{d}", ttl, lambda: fn(sym, d))
