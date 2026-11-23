"""
Which close a holding's day is measured from.

A day change is "now against the last close", and the last close depends on
where the market is in its day. Yahoo's daily bars do not say: before New
York opens, the newest bar is yesterday's close and the one before it the
day before, and taking "the last two bars" measured a US share from two
sessions back. The terminal then showed yesterday's whole move as today's,
and added the pre-market on top of it (15 October 2026).

The rule, chosen by the user on 15 October 2026:

    a holding's day starts again at its market's close.

So the reference is the close of the newest session that has FINISHED:

    New York, 15:00 Amsterdam (open)     yesterday's close
    New York, 22:30 Amsterdam (closed)   today's close: after-hours counts from it
    New York, 12:00 the next day (pre)   still that close: the pre-market counts from it
    Amsterdam, 08:30 (before the open)   yesterday's close

Each exchange keeps its own clock and calendar: the Dutch and German
holidays and early closes come from the brief's list (morning.HOLIDAYS),
the rest are weekdays only, which only matters on their own holidays, and
then only until Yahoo shows no bar for that day.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

# exchange: time zone, regular open and close, holiday calendar, extended hours
EXCHANGES = {
    "US": ("America/New_York", (9, 30), (16, 0), "US", ((4, 0), (20, 0))),
    "EURONEXT": ("Europe/Amsterdam", (9, 0), (17, 30), "NL", None),
    "LISBON": ("Europe/Lisbon", (8, 0), (16, 30), "NL", None),
    "DUBLIN": ("Europe/Dublin", (8, 0), (16, 30), None, None),
    "XETRA": ("Europe/Berlin", (9, 0), (17, 30), "DE", None),
    "GERMANY": ("Europe/Berlin", (8, 0), (22, 0), "DE", None),      # Frankfurt floor and the regional exchanges
    "LSE": ("Europe/London", (8, 0), (16, 30), None, None),
    "MILAN": ("Europe/Rome", (9, 0), (17, 30), None, None),
    "MADRID": ("Europe/Madrid", (9, 0), (17, 30), None, None),
    "SIX": ("Europe/Zurich", (9, 0), (17, 30), None, None),
    "VIENNA": ("Europe/Vienna", (9, 0), (17, 30), None, None),
    "COPENHAGEN": ("Europe/Copenhagen", (9, 0), (17, 0), None, None),
    "STOCKHOLM": ("Europe/Stockholm", (9, 0), (17, 30), None, None),
    "HELSINKI": ("Europe/Helsinki", (10, 0), (18, 30), None, None),
    "OSLO": ("Europe/Oslo", (9, 0), (16, 20), None, None),
    "TORONTO": ("America/Toronto", (9, 30), (16, 0), None, None),
    "TOKYO": ("Asia/Tokyo", (9, 0), (15, 30), None, None),
    "HONGKONG": ("Asia/Hong_Kong", (9, 30), (16, 0), None, None),
}
SUFFIX = {"AS": "EURONEXT", "PA": "EURONEXT", "BR": "EURONEXT", "LS": "LISBON", "IR": "DUBLIN",
          "DE": "XETRA", "F": "GERMANY", "DU": "GERMANY", "MU": "GERMANY", "SG": "GERMANY", "BE": "GERMANY",
          "HM": "GERMANY", "HA": "GERMANY", "L": "LSE", "IL": "LSE", "MI": "MILAN", "MC": "MADRID",
          "SW": "SIX", "VI": "VIENNA", "CO": "COPENHAGEN", "ST": "STOCKHOLM", "HE": "HELSINKI", "OL": "OSLO",
          "TO": "TORONTO", "V": "TORONTO", "T": "TOKYO", "HK": "HONGKONG"}


def exchange_of(ticker: Optional[str]) -> Optional[str]:
    """The exchange a Yahoo symbol trades on; None for FX, futures, indices and the unknown."""
    if not ticker or any(c in ticker for c in "=^"):
        return None
    if "." not in ticker:
        return "US"                  # Yahoo writes US share classes with a dash (BRK-B)
    return SUFFIX.get(ticker.rsplit(".", 1)[1].upper())


def _cal(ex: str) -> tuple[dict, dict]:
    from .morning import HOLIDAYS, EARLY_CLOSE
    key = EXCHANGES[ex][3]
    return (HOLIDAYS.get(key) or {}) if key else {}, (EARLY_CLOSE.get(key) or {}) if key else {}


def trading_day(ex: str, d: date) -> bool:
    hol, _ = _cal(ex)
    return d.weekday() < 5 and d.isoformat() not in hol


def session(ex: str, d: date) -> Optional[tuple[datetime, datetime]]:
    """The regular session's open and close on a local date, early closes applied; None on a day without one."""
    if not trading_day(ex, d):
        return None
    tzname, o, c, _, _ = EXCHANGES[ex]
    tz = ZoneInfo(tzname)
    _, early = _cal(ex)
    txt = early.get(d.isoformat())
    if txt:
        hh, mm = txt.split(" ")[0].split(":")
        c = (int(hh), int(mm))
    return (datetime.combine(d, dtime(*o), tz), datetime.combine(d, dtime(*c), tz))


def _local(ex: str, now: Optional[datetime]) -> datetime:
    tz = ZoneInfo(EXCHANGES[ex][0])
    return (now or datetime.now(tz)).astimezone(tz)


def state(ex: Optional[str], now: Optional[datetime] = None) -> str:
    """'open', 'pre', 'post' or 'closed' for the exchange's local day."""
    if ex not in EXCHANGES:
        return "closed"
    t = _local(ex, now)
    s = session(ex, t.date())
    if s and s[0] <= t < s[1]:
        return "open"
    ext = EXCHANGES[ex][4]
    if s and ext:
        tz = t.tzinfo
        if datetime.combine(t.date(), dtime(*ext[0]), tz) <= t < s[0]:
            return "pre"
        if s[1] <= t < datetime.combine(t.date(), dtime(*ext[1]), tz):
            return "post"
    return "closed"


def done_through(ex: str, now: Optional[datetime] = None) -> str:
    """The newest local date whose session is over: today once today's close has passed, else yesterday."""
    t = _local(ex, now)
    s = session(ex, t.date())
    if s and t >= s[1]:
        return t.date().isoformat()
    return (t.date() - timedelta(days=1)).isoformat()


def pick(bars: list[tuple[str, float]], ex: Optional[str], now: Optional[datetime] = None) -> Optional[dict]:
    """
    From daily (date, close) bars in date order:

      ref    the close of the newest finished session: what the day change is measured from
      prev   the finished session before it, so ref against prev is the last session's change
      last   the newest bar, which during a session is that session so far

    An exchange this module does not know keeps the old reading, the last two bars.
    """
    bars = [(d, v) for d, v in bars if v is not None]
    if not bars:
        return None
    if ex in EXCHANGES:
        cut = done_through(ex, now)
        done = [b for b in bars if b[0] <= cut]
    else:
        done = bars[:-1] if len(bars) > 1 else bars
    if not done:
        # nothing finished in the window: the newest bar is all there is to measure from
        done = bars[:1]
    ref = done[-1]
    prev = done[-2] if len(done) > 1 else None
    return {"ref_d": ref[0], "ref": ref[1], "prev_d": prev[0] if prev else None,
            "prev": prev[1] if prev else None, "last_d": bars[-1][0], "last": bars[-1][1]}


def next_change(exchanges, now: Optional[datetime] = None) -> Optional[float]:
    """The next open or close, as a unix time, among these exchanges (extended hours too)."""
    best = None
    for ex in {e for e in exchanges if e in EXCHANGES}:
        t = _local(ex, now)
        ext = EXCHANGES[ex][4]
        for k in range(0, 8):
            d = t.date() + timedelta(days=k)
            s = session(ex, d)
            if not s:
                continue
            marks = [s[0], s[1]]
            if ext:
                marks += [datetime.combine(d, dtime(*ext[0]), t.tzinfo), datetime.combine(d, dtime(*ext[1]), t.tzinfo)]
            later = [m for m in marks if m > t]
            if later:
                m = min(later).timestamp()
                best = m if best is None else min(best, m)
                break
    return best


def tag(st: str, price: Optional[float], ref: Optional[float]) -> Optional[str]:
    """'pre' or 'after' when a price has moved outside the regular session; None otherwise."""
    if st == "open" or price is None or ref is None or abs(price - ref) < 1e-9:
        return None
    return "pre" if st == "pre" else "after"
