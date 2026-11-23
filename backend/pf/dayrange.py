"""
The day's low and high: how far the book was down and up today, recorded as
the live feed sees it (build 35).

What is tracked is "today so far": the book's move since the last closes, as
the Day tile shows it while the markets trade. The tile itself starts again at
each market's close (pf/sessions.py), so a holding whose market has already
closed today counts its finished session here, plus anything since:

    move today = sum over holdings of
                   the day change now            (its market has not closed today)
                   + its last session            (it closed today: that session is today's)

That keeps the line continuous through Amsterdam's close at 17:30 and New
York's at 22:00; at midnight a new day starts. One row a day in `day_range`:
the low and the high with the moment each was seen, and the last value.

Recorded only while the terminal is open in a browser: the live feed runs only
when a page is watching it, and a low reached with no page open is not seen.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from typing import Optional

from . import store

try:
    from zoneinfo import ZoneInfo
    AMS = ZoneInfo("Europe/Amsterdam")
except Exception:                                    # pragma: no cover
    AMS = None

SAVE_EVERY = 60          # the last value is written at most once a minute; a new low or high at once

_LOCK = threading.Lock()
_MEM: dict = {}          # today's row, as last written or read
_SAVED = {"at": 0.0}


def today(now: Optional[float] = None) -> str:
    t = datetime.fromtimestamp(now or time.time(), tz=AMS) if AMS else datetime.fromtimestamp(now or time.time())
    return t.date().isoformat()


def today_move(rows: list[dict], day: str) -> Optional[float]:
    """The book's move today from the live rows: day change, plus the last session where it was today's."""
    if not rows:
        return None
    total = 0.0
    for r in rows:
        total += r.get("day_eur") or 0.0
        if r.get("last_date") == day and r.get("last_eur") is not None:
            total += r["last_eur"]
    return round(total, 2)


def _read(day: str) -> Optional[dict]:
    with store.conn() as c:
        r = c.execute("SELECT * FROM day_range WHERE d=?", (day,)).fetchone()
    return dict(r) if r else None


def note(value: Optional[float], now: Optional[float] = None) -> Optional[dict]:
    """Take one observation of today's move; returns today's row (low, high, last, with times)."""
    if value is None:
        return get()
    now = now or time.time()
    day = today(now)
    with _LOCK:
        # a copy: _MEM is cleared and refilled from it below
        row = dict(_MEM) if _MEM.get("d") == day else (_read(day) or {})
        if not row:
            row = {"d": day, "lo": value, "lo_ts": now, "hi": value, "hi_ts": now, "last": value, "last_ts": now, "n": 0}
        new_extreme = False
        if value < row["lo"]:
            row["lo"], row["lo_ts"], new_extreme = value, now, True
        if value > row["hi"]:
            row["hi"], row["hi_ts"], new_extreme = value, now, True
        row["last"], row["last_ts"] = value, now
        row["n"] = (row.get("n") or 0) + 1
        first = _MEM.get("d") != day
        _MEM.clear()
        _MEM.update(row)
        if new_extreme or first or now - _SAVED["at"] >= SAVE_EVERY:
            with store.conn() as c:
                c.execute("INSERT OR REPLACE INTO day_range (d,lo,lo_ts,hi,hi_ts,last,last_ts,n) VALUES (?,?,?,?,?,?,?,?)",
                          (row["d"], row["lo"], row["lo_ts"], row["hi"], row["hi_ts"], row["last"], row["last_ts"], row["n"]))
            _SAVED["at"] = now
        return dict(row)


def get(day: Optional[str] = None) -> Optional[dict]:
    day = day or today()
    with _LOCK:
        if _MEM.get("d") == day:
            return dict(_MEM)
    return _read(day)


def history(n: int = 30) -> list[dict]:
    with store.conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM day_range ORDER BY d DESC LIMIT ?", (n,))]


def reset_memory():
    """For the checks: forget the in-memory row."""
    with _LOCK:
        _MEM.clear()
        _SAVED["at"] = 0.0
