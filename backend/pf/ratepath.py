"""
The rate path: what futures price for every central-bank meeting ahead.

THE FED, meeting by meeting. A 30-day fed funds future settles at 100 minus
the AVERAGE effective rate over its calendar month. A month with no meeting
therefore prices one rate; a month with a meeting prices two, weighted by the
days either side of it:

    100 - price = (days up to and including the decision) / N * rate before
                + (days after it)                         / N * rate after

Chained forward from today's rate that gives the rate after every meeting.
Two rules keep it stable, and both are what CME's FedWatch does:

  * When the month AFTER a meeting has no meeting of its own, its contract
    is the post-meeting rate outright, so it is used instead of dividing by
    the handful of days left in the meeting month. The October 2026 meeting
    is on the 28th: backed out of October's own contract, a one basis point
    wobble in the price becomes ten in the answer.
  * The decision day itself still trades at the old rate; the new target
    takes effect the next morning.

Yahoo carries every monthly contract as ZQ<month code><yy>.CBT, about
seventeen months out. Build 13 read only the front one and said the strip
was paid data. It is not.

THE ECB, by quarter. CME's three-month EUR short-term rate futures
(ESR<month code><yy>.CME) settle on compounded overnight rates between two
IMM dates, the third Wednesdays of March, June, September and December. One
price covers two meetings, so the page reports the quarter and names the
meetings inside it rather than pretending to split them. Yahoo keeps only
the last settlement of each, which can be a few days old; every row carries
its date.
"""

from __future__ import annotations

import calendar
import time
from datetime import date, timedelta
from typing import Optional

from . import sentiment, store

STEP = 0.25
AHEAD = 17              # fed funds contracts to read, counting this month
MIN_AFTER = 6           # days after a decision before its own month is trusted
QUARTERS = 8            # three-month euro contracts to read
PAST = (("1 week ago", 7), ("1 month ago", 30), ("3 months ago", 91))
TTL = 900


def _next(ym: tuple) -> tuple:
    return (ym[0] + (ym[1] == 12), ym[1] % 12 + 1)


def _key(ym: tuple) -> str:
    return f"{ym[0]}-{ym[1]:02d}"


def odds(change: float) -> dict:
    """
    A priced change in the rate, as the chance of a quarter-point move.

    One step at a time: 9bp priced is a 36% chance of 25bp. Beyond a full
    step the remainder is the chance of a second one, reported apart so the
    three headline numbers still add to 100.
    """
    steps = change / STEP
    a = abs(steps)
    move = min(1.0, a)
    return {"steps": round(steps, 2),
            "hike": round(move * 100 if steps > 0 else 0.0, 1),
            "cut": round(move * 100 if steps < 0 else 0.0, 1),
            "hold": round((1.0 - move) * 100, 1),
            "second": round(min(1.0, a - 1.0) * 100, 1) if a > 1 else 0.0}


def fed_chain(avgs: dict, meetings: list, r0: Optional[float], today: date) -> dict:
    """
    The rate after every meeting, from the implied monthly averages.

    avgs: {(year, month): 100 - price}. meetings: decision dates. r0: the
    rate in force today, or None to back it out of the first month's own
    contract — which is how a path "as it was priced a month ago" is read
    without the contracts that have expired since. Pure arithmetic;
    `fed_path` supplies the prices.
    """
    months, ym = [], (today.year, today.month)
    while ym in avgs:
        months.append(ym)
        ym = _next(ym)
    ahead = {}
    for m in sorted(meetings):
        if m >= today:
            ahead.setdefault((m.year, m.month), m)

    out, monthly, cur, base = [], [], r0, r0
    for i, ym in enumerate(months):
        avg, n = avgs[ym], calendar.monthrange(*ym)[1]
        m = ahead.get(ym)
        if m is None:
            # One rate all month. The current month's contract is mostly days
            # already lived, so today's rate stands in for it.
            rate = cur if (i == 0 and cur is not None) else avg
            monthly.append({"month": _key(ym), "avg": round(avg, 4), "rate": round(rate, 4)})
            cur = rate
            base = rate if base is None else base
            continue
        before, after_days = m.day, n - m.day
        nxt = months[i + 1] if i + 1 < len(months) else None
        if nxt and nxt not in ahead:
            after, how = avgs[nxt], "next"
            if cur is None:
                cur = (avg * n - after * after_days) / before
        elif cur is None:
            cur = after = avg
            how = "thin"
        elif after_days >= 1:
            after = (avg * n - cur * before) / after_days
            how = "own" if after_days >= MIN_AFTER else "thin"
        else:
            break
        base = cur if base is None else base
        change = after - cur
        out.append({"date": m.isoformat(), "days": (m - today).days,
                    "before": round(cur, 4), "after": round(after, 4),
                    "change_bp": round(change * 100, 1),
                    "cum_bp": round((after - base) * 100, 1),
                    "moves": round((after - base) / STEP, 2),
                    "how": how,
                    "contract": sentiment.zq_symbol(date(*(nxt if how == "next" else ym), 1)),
                    **odds(change)})
        monthly.append({"month": _key(ym), "avg": round(avg, 4), "rate": round(after, 4),
                        "meeting": m.isoformat()})
        cur = after
    return {"meetings": out, "monthly": monthly, "base": base}


# --------------------------------------------------------------------------
# prices
# --------------------------------------------------------------------------

def _on(ser: dict, day: str, within: int = 7) -> Optional[float]:
    """The value on `day`, or the newest one in the `within` days before it."""
    if day in ser:
        return ser[day]
    lo = (date.fromisoformat(day) - timedelta(days=within)).isoformat()
    older = [d for d in ser if lo <= d <= day]
    return ser[max(older)] if older else None


def _zq_months(today: date) -> list:
    out, ym = [], (today.year, today.month)
    for _ in range(AHEAD):
        out.append(ym)
        ym = _next(ym)
    return out


def _load_zq() -> dict:
    from . import bonds
    months = _zq_months(date.today())
    syms = [sentiment.zq_symbol(date(y, m, 1)) for y, m in months]
    got = bonds._download(syms, period="6mo")
    ser = {_key(ym): got.get(s) or {} for ym, s in zip(months, syms)}
    if sum(1 for v in ser.values() if v) < 3:
        raise RuntimeError("Yahoo returned fewer than three fed funds contracts")
    return {"series": ser, "fetched_at": time.time()}


def _avgs(ser: dict, day: str) -> dict:
    out = {}
    for k, m in ser.items():
        px = _on(m, day)
        if px:
            y, mo = k.split("-")
            out[(int(y), int(mo))] = 100.0 - px
    return out


def _meetings() -> list:
    return [date.fromisoformat(d) for d in sentiment.FOMC]


def _baseline(avgs: dict, effr: dict, today: date) -> tuple:
    """
    The rate in force today, and where it came from.

    The published effective rate lags by a day or two. Just after a meeting
    it is still the old rate, and chaining from it would read a move that
    has happened as one still to come. When the last observation predates
    the last decision, the rate is taken from the futures instead: the next
    month's contract when that month has no meeting, else this month's.
    """
    obs = max((d for d in effr if d <= today.isoformat()), default=None)
    rate = effr.get(obs) if obs else None
    done = [m for m in _meetings() if m < today]
    last = done[-1] if done else None
    if rate is not None and (last is None or obs > last.isoformat()):
        return rate, f"the effective rate published for {obs}", False
    ym = (today.year, today.month)
    nxt = _next(ym)
    ahead = {(m.year, m.month) for m in _meetings() if m >= today}
    if nxt in avgs and nxt not in ahead and ym not in ahead:
        return avgs[nxt], f"the {date(*nxt, 1):%B} contract, which has no meeting in it", True
    if ym in avgs and ym not in ahead and last and (last.year, last.month) == ym and rate is not None:
        n = calendar.monthrange(*ym)[1]
        if n - last.day >= MIN_AFTER:
            return ((avgs[ym] * n - rate * last.day) / (n - last.day),
                    "this month's contract, after the decision", True)
    return rate, (f"the effective rate published for {obs}, before the last decision"
                  if obs else "no effective rate"), True


def _sep() -> list:
    """The Fed's own median projection for the end of each year (FRED)."""
    from . import feeds
    try:
        s = (feeds.fred(["FEDTARMD"], days=365 * 6).get("series") or {}).get("FEDTARMD") or {}
    except Exception:
        return []
    return [{"year": int(d[:4]), "rate": v} for d, v in sorted(s.items())]


def fed_path(today: Optional[date] = None) -> dict:
    from . import bonds
    today = today or date.today()
    zq = bonds._cached("ratepath:zq", TTL, _load_zq)
    ser = zq["series"]
    us = bonds.us_series()
    effr = us.get("DFF") or {}
    lo, hi = bonds.Ser(us.get("DFEDTARL")).last(), bonds.Ser(us.get("DFEDTARU")).last()

    avgs = _avgs(ser, today.isoformat())
    r0, src, derived = _baseline(avgs, effr, today)
    if r0 is None or not avgs:
        raise RuntimeError("no fed funds futures or no effective rate to chain them from")
    chain = fed_chain(avgs, _meetings(), r0, today)
    meets = chain["meetings"]

    # The same meetings as they were priced earlier. Only the contracts still
    # listed are needed: the rate going into this month is backed out of
    # this month's own contract as it stood that day.
    past = []
    for label, days in PAST:
        d0 = today - timedelta(days=days)
        a0 = _avgs(ser, d0.isoformat())
        if not a0:
            continue
        c0 = fed_chain(a0, _meetings(), None, today)
        pts = {m["date"]: m["after"] for m in c0["meetings"]}
        row = [{"date": m["date"], "after": pts[m["date"]]} for m in meets if m["date"] in pts]
        if row and c0["base"] is not None:
            past.append({"label": label, "date": d0.isoformat(),
                         "rate": round(c0["base"], 4), "points": row})

    asof = max((max(m) for m in ser.values() if m), default=None)
    listed = [m for m in _meetings() if m >= today]
    return {
        "now": {"rate": round(r0, 4), "src": src, "derived": derived,
                "lower": lo[1], "upper": hi[1], "range_asof": hi[0]},
        "meetings": meets, "monthly": chain["monthly"], "past": past,
        "sep": [s for s in _sep() if s["year"] >= today.year],
        "asof": asof, "contracts": sum(1 for v in ser.values() if v),
        "missing": [k for k, v in ser.items() if not v],
        "unpriced": [m.isoformat() for m in listed[len(meets):]],
        "meetings_as_of": sentiment.MEETINGS_AS_OF,
    }


def next_fed(today: Optional[date] = None) -> dict:
    """
    The next meeting alone, in the shape the sentiment panel has always
    read — now the first link of the chain rather than a separate sum.
    """
    today = today or date.today()
    nxt = sentiment.next_meeting(sentiment.FOMC, today)
    base = {"as_of": sentiment.MEETINGS_AS_OF,
            "ecb_next": sentiment.next_meeting(sentiment.ECB_MEETINGS, today)}
    if nxt is None:
        return {**base, "ok": False, "error": "the FOMC list has run out — it needs "
                                              f"refreshing (last updated {sentiment.MEETINGS_AS_OF})"}
    try:
        p = fed_path(today)
    except Exception as e:
        return {**base, "ok": False, "next": nxt, "error": f"{e.__class__.__name__}: {e}"[:240]}
    if not p["meetings"]:
        return {**base, "ok": False, "next": nxt, "current": p["now"]["rate"],
                "error": "no fed funds contract covers the next meeting"}
    m, end = p["meetings"][0], p["meetings"][-1]
    out = {**base, "ok": True, "next": nxt, "current": p["now"]["rate"],
           "baseline": p["now"]["rate"], "baseline_src": p["now"]["src"],
           "contract": m["contract"], "implied_post": round(m["after"], 3),
           "implied_move_bp": m["change_bp"], "cut": m["cut"], "hold": m["hold"],
           "hike": m["hike"], "steps": m["steps"],
           "zq": round(100.0 - m["after"], 4) if m["how"] == "next" else None,
           "note": f"{len(p['meetings'])} meetings are priced, to {end['after']:.2f}% after "
                   f"{date.fromisoformat(end['date']):%d %B %Y} — BOND has the whole path",
           "path_end": {"date": end["date"], "after": end["after"], "cum_bp": end["cum_bp"]},
           "sources": {"future": {"ok": True, "symbol": m["contract"], "price": None},
                       "effective_rate": "DFF", "effective_rate_date": None}}
    if p["now"]["derived"]:
        out["stale_baseline"] = f"today's rate is taken from {p['now']['src']}"
    return out


# --------------------------------------------------------------------------
# the ECB, by quarter
# --------------------------------------------------------------------------

def imm(y: int, m: int) -> date:
    """The third Wednesday of the month."""
    d = date(y, m, 15)
    return d + timedelta(days=(2 - d.weekday()) % 7)


def esr_symbol(y: int, m: int) -> str:
    return f"ESR{sentiment.MONTH_CODES[m - 1]}{y % 100:02d}.CME"


def _quarters(today: date) -> list:
    """(start, end) of the IMM quarter that contains today, and those after it."""
    y, m = today.year, (today.month - 1) // 3 * 3 + 3
    if imm(y, m) > today:
        y, m = (y, m - 3) if m > 3 else (y - 1, 12)
    out = []
    for _ in range(QUARTERS):
        ny, nm = (y, m + 3) if m < 12 else (y + 1, 3)
        out.append((imm(y, m), imm(ny, nm)))
        y, m = ny, nm
    return out


def _load_esr() -> dict:
    from . import bonds
    qs = _quarters(date.today())
    syms = [esr_symbol(a.year, a.month) for a, _ in qs]
    got = bonds._download(syms, period="1mo")
    return {"series": {a.isoformat(): got.get(s) or {} for (a, _), s in zip(qs, syms)},
            "fetched_at": time.time()}


def ecb_quarters(prices: dict, estr: float, dfr: float, today: date) -> list:
    """
    prices: {quarter start ISO: (price, day)}. Each contract's implied rate
    is an overnight rate; the deposit rate sits a few basis points above it,
    and that gap is carried along unchanged.
    """
    gap = max(0.0, min(0.25, dfr - estr))
    out = []
    for a, b in _quarters(today):
        px = prices.get(a.isoformat())
        if not px:
            continue
        implied = 100.0 - px[0]
        if a <= today:
            # Part of this quarter is already lived at today's rate.
            n, done = (b - a).days, (today - a).days
            if n - done < 14:
                continue
            implied = (implied * n - estr * done) / (n - done)
        rate = implied + gap
        out.append({"from": max(a, today).isoformat(), "to": b.isoformat(),
                    "estr": round(implied, 3), "rate": round(rate, 3),
                    "cum_bp": round((rate - dfr) * 100, 1),
                    "moves": round((rate - dfr) / STEP, 2),
                    "meetings": [d for d in sentiment.ECB_MEETINGS
                                 if max(a, today).isoformat() <= d < b.isoformat()],
                    "contract": esr_symbol(a.year, a.month), "price_date": px[1],
                    "running": a <= today})
    return out


def ecb_path(today: Optional[date] = None) -> dict:
    from . import bonds
    today = today or date.today()
    euro, _ = bonds.euro_series()
    dfr, estr = bonds.Ser(euro.get("ECB_DFR")).last(), bonds.Ser(euro.get("ESTR")).last()
    if dfr[1] is None or estr[1] is None:
        raise RuntimeError("no ECB deposit rate or euro short-term rate")
    ser = bonds._cached("ratepath:esr", TTL, _load_esr)["series"]
    prices = {k: (m[max(m)], max(m)) for k, m in ser.items() if m}
    rows = ecb_quarters(prices, estr[1], dfr[1], today)
    if not rows:
        raise RuntimeError("Yahoo returned no three-month euro short-term rate futures")
    return {"dfr": dfr[1], "dfr_asof": dfr[0], "estr": estr[1], "estr_asof": estr[0],
            "quarters": rows, "oldest_price": min(r["price_date"] for r in rows),
            "next_meeting": sentiment.next_meeting(sentiment.ECB_MEETINGS, today),
            "meetings_as_of": sentiment.MEETINGS_AS_OF}


# --------------------------------------------------------------------------
# the part the bond page asks for
# --------------------------------------------------------------------------

def _moves(bp: float) -> str:
    n = abs(bp) / 25
    word = "hike" if bp > 0 else "cut"
    return f"{n:.1f} quarter-point {word}s" if round(n, 1) != 1 else f"one quarter-point {word}"


def _insights(fed: Optional[dict], ecb: Optional[dict]) -> list:
    ins = []
    if fed and fed.get("meetings"):
        m, end = fed["meetings"][0], fed["meetings"][-1]
        d1, d2 = date.fromisoformat(m["date"]), date.fromisoformat(end["date"])
        lead = max((("hike", m["hike"]), ("cut", m["cut"]), ("hold", m["hold"])), key=lambda x: x[1])
        ins.append({"tone": "warn" if end["cum_bp"] >= 37 else "good" if end["cum_bp"] <= -37 else "info",
                    "sec": "path",
                    "text": f"Futures price fed funds at {end['after']:.2f}% after the {d2:%B %Y} meeting, "
                            f"{end['cum_bp']:+.0f}bp from today's {fed['now']['rate']:.2f}%"
                            + (f": about {_moves(end['cum_bp'])}." if abs(end["cum_bp"]) >= 12 else
                               ": no change priced.")
                            + f" The next decision, {d1:%d %B}, is priced {lead[1]:.0f}% {lead[0]}."})
        sep = {s["year"]: s["rate"] for s in fed.get("sep") or []}
        dec = [x for x in fed["meetings"] if x["date"][5:7] == "12" and int(x["date"][:4]) in sep]
        if dec:
            x = dec[-1]
            gap = round((x["after"] - sep[int(x["date"][:4])]) * 100)
            if abs(gap) >= 20:
                ins.append({"tone": "info", "sec": "path",
                            "text": f"For the end of {x['date'][:4]} the market prices {x['after']:.2f}%, "
                                    f"{abs(gap)}bp {'above' if gap > 0 else 'below'} the Fed's own median "
                                    f"projection of {sep[int(x['date'][:4])]:.1f}%."})
        mo = next((p for p in fed.get("past") or [] if p["label"] == "1 month ago"), None)
        if mo:
            old = {p["date"]: p["after"] for p in mo["points"]}
            common = [x for x in fed["meetings"] if x["date"] in old]
            if common:
                x = common[-1]
                shift = round((x["after"] - old[x["date"]]) * 100)
                if abs(shift) >= 10:
                    ins.append({"tone": "warn" if shift > 0 else "good", "sec": "path",
                                "text": f"A month ago the {date.fromisoformat(x['date']):%B %Y} meeting was "
                                        f"priced at {old[x['date']]:.2f}%; it is {x['after']:.2f}% now, "
                                        f"{shift:+d}bp."})
    if ecb and ecb.get("quarters"):
        # About a year out: further contracts trade rarely and Yahoo's last
        # price for them can be a week old.
        horizon = (date.today() + timedelta(days=456)).isoformat()
        far = [q for q in ecb["quarters"] if not q["running"] and q["from"] <= horizon]
        if far:
            q = far[-1]
            ins.append({"tone": "warn" if q["cum_bp"] >= 37 else "good" if q["cum_bp"] <= -37 else "info",
                        "sec": "path",
                        "text": f"Euro futures price the ECB deposit rate near {q['rate']:.2f}% for the "
                                f"quarter from {date.fromisoformat(q['from']):%B %Y}, {q['cum_bp']:+.0f}bp "
                                f"from today's {ecb['dfr']:.2f}%"
                                + (f": about {_moves(q['cum_bp'])}." if abs(q["cum_bp"]) >= 12 else ".")})
    return ins


def part() -> dict:
    out = {"fetched_at": time.time()}
    for k, fn in (("fed", fed_path), ("ecb", ecb_path)):
        try:
            out[k] = fn()
        except Exception as e:
            out[k] = None
            out[k + "_error"] = f"{e.__class__.__name__}: {e}"[:240]
    if not out["fed"] and not out["ecb"]:
        raise RuntimeError(out.get("fed_error") or out.get("ecb_error") or "no futures")
    out["insights"] = _insights(out["fed"], out["ecb"])
    return out
