"""
WEEK — the week in review (build 53).

Built each Saturday morning, once every market's week is complete, and kept:
one review a week, with an archive. Four parts, then a note Claude writes from
them:

    your book        the week's return in euros and as a time-weighted figure,
                     against the S&P 500 bought with the same deposits; what
                     drove it, holding by holding, split into the price and the
                     currency; the best and the worst; the trades and the
                     dividends booked; what the attribution does not cover
                     (turbos, cash, sold positions) as one line, so the parts
                     add up to the whole
    markets          the market board's week (indices, rates in basis points,
                     the euro, oil, gold, volatility); FLOW's breadth days,
                     hedge funds' futures, DIX and put/call; the macro releases
                     that surprised
    the week ahead   your holdings' reports (turbos' shares too), the macro
                     calendar's big releases, central-bank decisions, holidays,
                     ex-dividend dates
    the terminal     the alerts that fired (recorded from this build on), READ's
                     articles, Congress's trades in your stocks, Koers AI's
                     trades and standing, the watchlist's new names

The note is written by the writer chosen in Settings (Claude Code on your plan,
or an API key), once, right after the Saturday build; the page can ask for it
again. Everything else is the terminal's own data: nothing here is invented.
"""
from __future__ import annotations

import json
import math
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import store

AMS = ZoneInfo("Europe/Amsterdam")
BUILD_AT = (8, 0)              # Saturday, Amsterdam: every market's week is complete
NOTE_RETRY_S = 2 * 3600
NOTE_TRIES = 3


def now_ams() -> datetime:
    return datetime.now(AMS)


def _r(x, d: int = 2):
    return round(x, d) if x is not None and isinstance(x, (int, float)) and math.isfinite(x) else None


def last_week(today: Optional[date] = None) -> date:
    """The Monday of the last complete week: this week's from Saturday on, the one before on a weekday."""
    today = today or now_ams().date()
    mon = today - timedelta(days=today.weekday())
    return mon if today.weekday() >= 5 else mon - timedelta(days=7)


def _on_or_before(h: dict, day: str, stale: int = 6) -> Optional[tuple]:
    ks = [k for k in h if k <= day]
    if not ks:
        return None
    k = max(ks)
    return (k, h[k]) if (date.fromisoformat(day) - date.fromisoformat(k)).days <= stale else None


def _twr(series: list, start: str, end: str, key: str = "v") -> tuple:
    """Time-weighted return of `key` from the close before `start` to `end`, each day's flow neutralised."""
    from .service import _day_return
    pts = [x for x in series if start <= x["d"] <= end and x.get(key) is not None]
    prior = [x for x in series if x["d"] < start and x.get(key) is not None]
    if not pts or not prior:
        return None, None, None
    prev = opening = prior[-1][key]
    twr, flows = 1.0, 0.0
    for x in pts:
        f = x.get("f") or 0.0
        flows += f
        r = _day_return(x[key], f, prev)
        if r is not None:
            twr *= 1.0 + r
        prev = x[key]
    return _r((twr - 1) * 100, 2), _r(pts[-1][key] - opening - flows, 2), _r(opening, 2)


# --------------------------------------------------------------------------
# 1. your book
# --------------------------------------------------------------------------

def attribution(positions: list, txns: list, mon: date, fri: date, prices_of, fx_of) -> dict:
    """
    Each holding's euros this week: the shares held all week priced from last
    Friday's close to this Friday's (the price, and the currency on its own),
    plus what shares bought or sold in the week made against what they cost.
    `prices_of(ticker)` and `fx_of(pair)` return {date: value}; a held price is
    scaled by the holding's own euro value, so pence-quoted lines come out right.
    """
    from .service import _ccy_pair
    p0d, p1d = (mon - timedelta(days=3)).isoformat(), fri.isoformat()
    lo, hi = mon.isoformat(), (fri + timedelta(days=1)).isoformat()
    flows: dict = {}
    for t in txns:
        if t.kind in ("BUY", "SELL") and lo <= str(t.trade_dt)[:10] < hi:
            e = flows.setdefault(t.isin, [0.0, 0.0])
            e[0] += t.quantity or 0.0
            e[1] += -(t.value_eur or 0.0)            # what the trade cost in euros (a sale brings euros in: negative)
    rows = []
    for p in positions:
        tk, q1 = p.get("ticker"), p.get("qty") or 0.0
        if not tk or p.get("kind") == "DERIVATIVE" or p.get("excluded") or not q1:
            continue
        px = prices_of(tk) or {}
        a, b = _on_or_before(px, p0d), _on_or_before(px, p1d)
        ccy = p.get("currency") or "EUR"
        fx = fx_of(_ccy_pair(ccy)) if ccy != "EUR" else {}
        xa = _on_or_before(fx, p0d)[1] if fx and _on_or_before(fx, p0d) else (1.0 if ccy == "EUR" else None)
        xb = _on_or_before(fx, p1d)[1] if fx and _on_or_before(fx, p1d) else (1.0 if ccy == "EUR" else None)
        if not (a and b and xa and xb):
            continue
        p0, p1 = a[1], b[1]
        k = (p.get("value_eur") or 0) / (q1 * p1 / xb) if q1 * p1 else 1.0
        if not (0.005 < k < 200):
            k = 1.0
        dq, cost = flows.get(p.get("isin"), (0.0, 0.0))
        q0 = q1 - dq
        price = k * q0 * (p1 - p0) / xb
        fxe = k * q0 * p0 * (1 / xb - 1 / xa)
        traded = k * dq * p1 / xb - cost if dq else 0.0
        rows.append({"ticker": tk, "name": p.get("name"), "sector": p.get("sector") or "Other", "ccy": ccy,
                     "eur": _r(price + fxe + traded), "price_eur": _r(price), "fx_eur": _r(fxe), "traded_eur": _r(traded) if dq else None,
                     "pct": _r((p1 / p0 - 1) * 100) if p0 else None, "pct_eur": _r(((p1 / xb) / (p0 / xa) - 1) * 100) if p0 else None,
                     "value_eur": _r(p.get("value_eur")), "weight": p.get("weight")})
    rows.sort(key=lambda r: -(r["eur"] or 0))
    sec: dict = {}
    for r in rows:
        sec[r["sector"]] = sec.get(r["sector"], 0.0) + (r["eur"] or 0)
    return {"rows": rows, "sectors": sorted(({"sector": k, "eur": _r(v)} for k, v in sec.items()), key=lambda x: -x["eur"]),
            "sum_eur": _r(sum(r["eur"] or 0 for r in rows)), "fx_eur": _r(sum(r["fx_eur"] or 0 for r in rows))}


def book(mon: date, fri: date) -> dict:
    from . import service, pricing
    d = service.dashboard("SP500")
    if d.get("empty"):
        return {"available": False}
    series = [x for x in (d.get("curve") or {}).get("series") or [] if x["d"] <= fri.isoformat()]
    twr, eur, opening = _twr(series, mon.isoformat(), fri.isoformat())
    btwr = _twr(series, mon.isoformat(), fri.isoformat(), key="b")[0]
    txns = store.load_txns()
    att = attribution(d.get("positions") or [], txns, mon, fri, store.cached_prices, store.cached_fx)
    lo, hi = mon.isoformat(), (fri + timedelta(days=1)).isoformat()
    trades = [{"d": str(t.trade_dt)[:10], "side": t.kind, "product": t.product, "qty": t.quantity, "price": t.price,
               "ccy": t.currency, "eur": _r(t.value_eur)} for t in txns if t.kind in ("BUY", "SELL") and lo <= str(t.trade_dt)[:10] < hi]
    div = sum((t.value_eur or t.total_eur or 0) for t in txns if t.kind in ("DIVIDEND", "DIV_TAX") and lo <= str(t.trade_dt)[:10] < hi)
    idx = {}
    for k, sym in (("spx", "^GSPC"), ("aex", "^AEX"), ("eurusd", "EURUSD=X")):
        try:
            h = pricing.get_history(sym, (mon - timedelta(days=14)).isoformat()) or {}
            a, b = _on_or_before(h, (mon - timedelta(days=3)).isoformat()), _on_or_before(h, fri.isoformat())
            idx[k] = _r((b[1] / a[1] - 1) * 100) if a and b else None
        except Exception:
            idx[k] = None
    rest = _r((eur or 0) - (att["sum_eur"] or 0)) if eur is not None else None
    span = (d.get("data_quality") or {}).get("account_span") or [None, None]
    return {"available": twr is not None, "twr_pct": twr, "eur": eur, "opening_eur": opening, "value_eur": _r((d.get("totals") or {}).get("value_eur")),
            "bench_pct": btwr, "bench_label": "the S&P 500 in euros, bought with the same deposits", "spx_pct": idx.get("spx"),
            "aex_pct": idx.get("aex"), "eurusd_pct": idx.get("eurusd"), "attribution": att, "rest_eur": rest,
            "best": att["rows"][:5], "worst": [r for r in att["rows"][::-1] if (r["eur"] or 0) < 0][:5],
            "trades": trades, "dividends_eur": _r(div), "import_to": span[1]}


# --------------------------------------------------------------------------
# 2. markets
# --------------------------------------------------------------------------

def markets(mon: date, fri: date) -> dict:
    mk = store.cache_get("market") or {}
    rows = []
    for r in mk.get("rows") or []:
        if r.get("last") is None or r.get("stale"):
            continue
        rate = r.get("u") == "pct"
        rows.append({"k": r["k"], "name": r.get("n"), "g": r.get("g"), "last": r.get("last"), "asof": r.get("asof"),
                     "w_pct": None if rate else r.get("w_pct"), "w_bp": _r((r.get("w_abs") or 0) * 100, 0) if rate else None})
    lo, hi = mon.isoformat(), fri.isoformat()
    macro = []
    with store.conn() as c:
        for e in c.execute("SELECT d, t, ccy, title, impact, actual, forecast, previous, surprise, rx_spx FROM mac_event "
                           "WHERE d >= ? AND d <= ? AND actual IS NOT NULL AND impact = 'high' ORDER BY d, t", (lo, hi)).fetchall():
            macro.append(dict(e))
    flow = {}
    try:
        from . import flow as fl
        g = fl.digest()
        b = g.get("breadth") or {}
        flow["days"] = [e for e in (b.get("events") or []) if lo <= e["d"] <= hi and e["kind"]]
        flow["episode"] = b.get("episode")
        eq = g.get("equity") or {}
        flow["hedge_funds"] = {k: eq.get(k) for k in ("gross_now", "net_now", "chg1", "chg4", "peak52", "degrossing")} if eq else None
        flow["pos_asof"] = g.get("pos_asof")
        dk = g.get("dark") or {}
        flow["dix"] = {k: dk.get(k) for k in ("d", "dix", "dix_rank", "gex", "gex_rank")} if dk.get("available") else None
        pc = g.get("pc") or {}
        flow["putcall"] = {"avg5": (pc.get("avg5") or {}).get("tot"), "rank": (pc.get("rank5") or {}).get("tot")} if pc.get("available") else None
    except Exception:
        pass
    breadth = {}
    for ix in ("spx", "ndx"):
        v = store.cache_get(f"idx:view:{ix}") or {}
        bd = v.get("breadth") or {}
        if bd.get("d"):
            pairs = dict(zip(bd["d"], bd["p200"]))
            a, b2 = _on_or_before(pairs, (mon - timedelta(days=3)).isoformat()), _on_or_before(pairs, hi)
            breadth[ix] = {"p200": b2[1] if b2 else None, "chg": _r(b2[1] - a[1], 1) if a and b2 else None}
    return {"rows": rows, "macro": macro, "flow": flow, "breadth": breadth}


# --------------------------------------------------------------------------
# 3. the week ahead
# --------------------------------------------------------------------------

def ahead(sat: date) -> dict:
    """Monday to Friday after `sat`: reports, macro, central banks, holidays, ex-dividend dates."""
    from . import earnings, sentiment, morning, service, brief
    mon = sat + timedelta(days=(7 - sat.weekday()) % 7 or 7)
    fri = mon + timedelta(days=4)
    lo, hi = mon.isoformat(), fri.isoformat()
    d = service.dashboard("SP500", light=True)
    _, weights, names, reporters = brief.book_tickers(d.get("positions") or [])
    rep = []
    for e in (earnings.upcoming_mine(days=16) or []):
        if lo <= e.get("date", "") <= hi:
            rep.append({"date": e["date"], "sym": e["sym"], "time": e.get("time_local"), "held": bool(e.get("held")),
                        "watch": bool(e.get("watch")), "weight": _r(weights.get(e["sym"]), 2)})
    have = {x["sym"] for x in rep}
    # a turbo's share is not "held" to ERN: ask the calendar for those by themselves
    turbos = [t for t in reporters if (names.get(t) or "").endswith("(turbo)") and t not in have]
    if turbos:
        try:
            from . import calendar as cal
            w = cal.earnings_week(turbos, names, weights, today=mon)
            for dd in w.get("days") or []:
                for ev in dd.get("events") or []:
                    if ev.get("ticker") not in turbos:      # the calendar adds every held share it finds: only the turbos' here
                        continue
                    rep.append({"date": ev["date"], "sym": ev["ticker"], "time": ev.get("time"), "held": True, "turbo": True,
                                "weight": _r(weights.get(ev["ticker"]), 2)})
        except Exception:
            pass
    rep.sort(key=lambda x: (x["date"], -(x.get("weight") or 0)))
    with store.conn() as c:
        macro = [dict(r) for r in c.execute("SELECT d, t, ccy, title, impact, forecast, previous FROM mac_event WHERE d >= ? AND d <= ? "
                                            "AND impact = 'high' ORDER BY d, t", (lo, hi)).fetchall()]
    cb = [{"date": x, "title": "Fed decision (FOMC)"} for x in sentiment.FOMC if lo <= x <= hi] + \
         [{"date": x, "title": "ECB decision"} for x in sentiment.ECB_MEETINGS if lo <= x <= hi]
    hol = [{"date": k, "market": mk, "title": v} for mk, days in morning.HOLIDAYS.items() for k, v in days.items() if lo <= k <= hi]
    exd = []
    for t in reporters:
        ov = store.cache_get(f"rs:ov:{t}") or {}
        x = (ov.get("dates") or {}).get("ex_div")
        if x and lo <= str(x)[:10] <= hi:
            exd.append({"date": str(x)[:10], "sym": t})
    return {"from": lo, "to": hi, "earnings": rep, "macro": macro, "central_banks": cb, "holidays": hol, "ex_div": sorted(exd, key=lambda x: x["date"])}


# --------------------------------------------------------------------------
# 4. the terminal's week
# --------------------------------------------------------------------------

_SEEN: set = set()          # (day, code, text) already written: the home screen asks every few seconds


def record_alerts(alerts: list[dict], today: Optional[str] = None) -> int:
    """Each alert the home screen showed, once a day: the record the weekly review reads (from build 53 on)."""
    today = today or now_ams().date().isoformat()
    rows = [(today, a.get("code") or "", a.get("text") or "", a.get("level") or "", a.get("detail") or "", time.time())
            for a in alerts if a.get("code") not in (None, "QUIET", "WEEK") and a.get("text")
            and (today, a.get("code") or "", a.get("text")) not in _SEEN]
    if not rows:
        return 0
    _SEEN.update((r[0], r[1], r[2]) for r in rows)
    with store.conn() as c:
        n = 0
        for r in rows:
            n += c.execute("INSERT OR IGNORE INTO alert_log (d, code, text, level, detail, ts) VALUES (?,?,?,?,?,?)", r).rowcount
    return n


def terminal(mon: date, fri: date) -> dict:
    lo, hi = mon.isoformat(), fri.isoformat()
    sun = (fri + timedelta(days=2)).isoformat()
    out: dict = {}
    with store.conn() as c:
        out["alerts"] = [dict(r) for r in c.execute("SELECT d, code, text, level, detail FROM alert_log WHERE d >= ? AND d <= ? "
                                                    "ORDER BY d, ts", (lo, sun)).fetchall()]
        first = c.execute("SELECT MIN(d) FROM alert_log").fetchone()[0]
        out["alerts_since"] = first
        # a story is rewritten as it develops: its latest version, once
        out["read"] = [dict(r) for r in c.execute(
            "SELECT a.id, a.d, a.kind, a.importance, a.headline, a.version FROM read_article a WHERE a.d >= ? AND a.d <= ? "
            "AND a.version = (SELECT MAX(b.version) FROM read_article b WHERE b.story_id = a.story_id) "
            "ORDER BY a.importance DESC, a.d DESC", (lo, sun)).fetchall()]
        out["kai_trades"] = [dict(r) for r in c.execute("SELECT d, ticker, side, qty, value_eur FROM kai_trade WHERE d >= ? AND d <= ? "
                                                        "ORDER BY d", (lo, sun)).fetchall()]
        # a stock that qualified under several presets is one name, with its presets together
        new: dict = {}
        for r in c.execute("SELECT sym, name, preset, first_d, score FROM wl_flag WHERE first_d >= ? AND first_d <= ? "
                           "ORDER BY score DESC", (lo, sun)).fetchall():
            e = new.setdefault(r["sym"], {"sym": r["sym"], "name": r["name"], "first_d": r["first_d"], "score": r["score"], "presets": []})
            if r["preset"] not in e["presets"]:
                e["presets"].append(r["preset"])
        out["wl_new"] = [{**e, "preset": ", ".join(e["presets"])} for e in new.values()][:12]
    try:
        from . import koersai
        out["kai_line"] = koersai.brief_line()
    except Exception:
        out["kai_line"] = None
    try:
        from . import pol
        mine = pol.mine().get("trades") or []
        out["congress_mine"] = [{k: t.get(k) for k in ("member", "party", "ticker", "type", "amount", "traded", "filed")}
                                for t in mine if lo <= (t.get("filed") or "") <= sun][:20]
        yr = pol.filings(mon.year).get("trades") or []
        out["congress_all"] = sum(1 for t in yr if lo <= (t.get("filed") or "") <= sun)
    except Exception:
        out["congress_mine"], out["congress_all"] = [], None
    return out


# --------------------------------------------------------------------------
# the review, the record, the note
# --------------------------------------------------------------------------

def build(mon: Optional[date] = None) -> dict:
    mon = mon or last_week()
    fri, sat = mon + timedelta(days=4), mon + timedelta(days=5)
    errs = {}

    def part(name, fn, default=None):
        try:
            return fn()
        except Exception as e:
            errs[name] = f"{e.__class__.__name__}: {e}"[:200]
            return default
    out = {"week": mon.isoformat(), "to": fri.isoformat(), "built_at": time.time(),
           "book": part("book", lambda: book(mon, fri), {"available": False}),
           "markets": part("markets", lambda: markets(mon, fri), {}),
           "ahead": part("ahead", lambda: ahead(sat), {}),
           "terminal": part("terminal", lambda: terminal(mon, fri), {}), "errors": errs}
    out["lines"] = part("lines", lambda: lines(out), [])
    with store.conn() as c:
        c.execute("INSERT OR REPLACE INTO week_review (wk, built_at, payload) VALUES (?, ?, ?)",
                  (out["week"], out["built_at"], json.dumps(out, default=str)))
    return out


def _eur(v) -> str:
    return "–" if v is None else f"{'−' if v < 0 else '+'}€{abs(v):,.0f}"


def _pc(v, d=1) -> str:
    return "–" if v is None else f"{v:+.{d}f}%"


def lines(v: dict) -> list[str]:
    """The review's first lines, each a fact from below."""
    out = []
    b = v.get("book") or {}
    if b.get("available"):
        s = f"Your book {'rose' if (b['eur'] or 0) >= 0 else 'fell'} €{abs(b['eur'] or 0):,.0f} ({_pc(b['twr_pct'], 2)}) this week"
        if b.get("bench_pct") is not None:
            diff = (b["twr_pct"] or 0) - b["bench_pct"]
            s += f", {'ahead of' if diff >= 0 else 'behind'} the S&P 500 in euros ({_pc(b['bench_pct'], 2)})"
        att = b.get("attribution") or {}
        if att.get("fx_eur"):
            s += f"; the currency {'added' if att['fx_eur'] > 0 else 'took away'} €{abs(att['fx_eur']):,.0f}"
        out.append(s + ".")
        if b.get("best"):
            out.append("Most of it: " + ", ".join(f"{r['ticker']} {_pc(r['pct_eur'])} ({_eur(r['eur'])})" for r in b["best"][:3])
                       + (("; against: " + ", ".join(f"{r['ticker']} {_pc(r['pct_eur'])} ({_eur(r['eur'])})" for r in b["worst"][:3])) if b.get("worst") else "") + ".")
    m = v.get("markets") or {}
    pick = {r["k"]: r for r in m.get("rows") or []}
    bits = [f"{pick[k]['name']} {_pc(pick[k]['w_pct'])}" for k in ("SPX", "NDX", "SX5E", "AEX") if k in pick and pick[k].get("w_pct") is not None]
    if "US10Y" in pick and pick["US10Y"].get("w_bp") is not None:
        bits.append(f"the US 10-year {pick['US10Y']['w_bp']:+.0f}bp")
    for k in ("BRENT", "GOLD", "EURUSD"):
        if k in pick and pick[k].get("w_pct") is not None:
            bits.append(f"{pick[k]['name']} {_pc(pick[k]['w_pct'])}")
    if bits:
        out.append("Markets: " + ", ".join(bits) + ".")
    f = (m.get("flow") or {})
    if f.get("days"):
        out.append("Breadth: " + "; ".join(f"{e['d'][8:]}/{e['d'][5:7]} a {e['kind'][:2]}% {'downside' if e['kind'].endswith('down') else 'upside'} day "
                                           f"({e['share']:.0f}% of the volume rising)" for e in f["days"][:4]) + ".")
    a = v.get("ahead") or {}
    nxt = [f"{x['sym']}{' (turbo)' if x.get('turbo') else ''} {x['date'][8:]}/{x['date'][5:7]}" for x in (a.get("earnings") or []) if x.get("held")]
    if nxt:
        out.append("Next week your holdings report: " + ", ".join(nxt[:8]) + ".")
    big = [f"{x['ccy']} {x['title']} {x['d'][8:]}/{x['d'][5:7]}" for x in (a.get("macro") or []) if x.get("ccy") in ("USD", "EUR")]
    if big or a.get("central_banks"):
        out.append("And: " + ", ".join([c["title"] + " " + c["date"][8:] + "/" + c["date"][5:7] for c in a.get("central_banks") or []] + big[:6]) + ".")
    return out


def stored(wk: str) -> Optional[dict]:
    with store.conn() as c:
        r = c.execute("SELECT payload FROM week_review WHERE wk = ?", (wk,)).fetchone()
    return json.loads(r["payload"]) if r else None


def weeks(n: int = 60) -> list[str]:
    with store.conn() as c:
        return [r["wk"] for r in c.execute("SELECT wk FROM week_review ORDER BY wk DESC LIMIT ?", (n,)).fetchall()]


def notes(wk: str) -> list[dict]:
    with store.conn() as c:
        rows = c.execute("SELECT * FROM week_note WHERE wk = ? ORDER BY id", (wk,)).fetchall()
    return [{**dict(r), "meta": json.loads(r["meta"] or "{}")} for r in rows]


SYSTEM = """You write the weekly review for one private investor in the Netherlands, read at the weekend. The material is a JSON document the investor's own terminal assembled: the week's result for their portfolio in euros and what drove it holding by holding, the markets' week, institutional flows, the macro releases, next week's schedule, and what the terminal itself flagged.

Everything you write must come from the material. Never invent or round a number into a different one. When the material has no cause for a move, say so rather than guessing.

Write for someone who knows markets: plain, specific, short. In Markdown:
# a one-line headline for the week
**The short version** — three bullets.
## Your book
What moved it and why, the currency's part, the trades made.
## Markets
The week in the markets and in the flows, the releases that surprised.
## Next week
What on the schedule matters for this book, in date order.
## Worth watching
At most four bullets.

Link tickers as [MSFT](/t/MSFT). Do not recommend buying or selling anything. Stay under 600 words."""


def _material(v: dict) -> str:
    slim = json.loads(json.dumps(v, default=str))
    b = slim.get("book") or {}
    att = b.get("attribution") or {}
    if att.get("rows"):
        att["rows"] = att["rows"][:25]
    return "<material>\n" + json.dumps(slim, ensure_ascii=False)[:60000] + "\n</material>"


NOTE_JOBS: dict = {}
_LOCK = threading.Lock()


def _save_note(wk: str, body: str, model: str, meta: dict, cost: Optional[float]) -> None:
    with store.conn() as c:
        c.execute("INSERT INTO week_note (wk, created, model, body, meta, cost_usd) VALUES (?,?,?,?,?,?)",
                  (wk, datetime.now(timezone.utc).isoformat(timespec="seconds"), model, body, json.dumps(meta), cost))


def _run_note(job: dict, wk: str):
    import os
    from . import earnai
    try:
        v = stored(wk)
        if not v:
            raise RuntimeError("there is no review for this week to write about")
        text = _material(v) + f"\n\nWrite the weekly review for the week of {wk} to {v['to']}."
        if job["writer"] == "claude-code":
            work = os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), "week_work", wk)
            os.makedirs(work, exist_ok=True)
            with open(os.path.join(work, "material.md"), "w", encoding="utf-8") as f:
                f.write(text)
            job["phase"] = "writing"
            out = earnai.cc_stream(earnai.cc_command("", system=SYSTEM), text, work, job)
            if not out["text"]:
                raise RuntimeError("Claude Code returned no text")
            _save_note(wk, out["text"], f"Claude Code · {out['model'] or 'default model'}",
                       {"writer": "claude-code", "usage": out["usage"], "api_equivalent_usd": out["api_usd"], "ms": out["ms"]}, None)
        else:
            m = earnai.model()
            job["phase"] = "writing"
            client = earnai._client()
            msg = client.messages.create(model=m, max_tokens=16000, system=SYSTEM, thinking={"type": "adaptive"},
                                         output_config={"effort": earnai.WRITE_EFFORT},
                                         messages=[{"role": "user", "content": text}])
            body = "\n".join(bl.text for bl in msg.content if getattr(bl, "type", "") == "text").strip()
            if not body:
                raise RuntimeError("the model returned no text")
            usage = {k: getattr(msg.usage, k, 0) or 0 for k in ("input_tokens", "output_tokens")}
            _save_note(wk, body, getattr(msg, "model", m) or m, {"writer": "api", "usage": usage}, earnai.cost(m, usage))
        job.update(state="done", phase="done")
    except Exception as e:
        job.update(state="error", error=f"{e.__class__.__name__}: {e}"[:300])
    finally:
        job["finished"] = time.time()


def start_note(wk: str) -> dict:
    from . import earnai
    w = earnai.writer()
    if w == "api" and not earnai.api_key():
        raise PermissionError("no Anthropic API key — add one in Settings, or switch the writer to Claude Code")
    if w == "claude-code":
        st = earnai.cc_status()
        if not st["found"]:
            raise PermissionError("Claude Code was not found on this computer")
        if not st["logged_in"] and not earnai.cc_status(force=True)["logged_in"]:
            raise PermissionError("Claude Code is not signed in yet (ALRT's page has the button)")
    if not stored(wk):
        raise LookupError("there is no review for this week yet")
    with _LOCK:
        j = NOTE_JOBS.get(wk)
        if j and j["state"] == "running":
            return _pub(j)
        j = {"state": "running", "phase": "starting", "text": "", "started": time.time(), "finished": None, "error": None,
             "writer": w, "tries": (j or {}).get("tries", 0) + 1}
        NOTE_JOBS[wk] = j
    threading.Thread(target=_run_note, args=(j, wk), daemon=True, name=f"week-note-{wk}").start()
    return _pub(j)


def _pub(j: Optional[dict]) -> Optional[dict]:
    return {k: v for k, v in (j or {}).items() if k != "text"} if j else None


JOB: dict = {"state": "idle"}


def start_build(mon: Optional[date] = None) -> dict:
    with _LOCK:
        if JOB.get("state") == "running":
            return dict(JOB)
        JOB.update(state="running", started=time.time(), error=None, finished=None)

    def run():
        try:
            build(mon)
            JOB.update(state="done")
        except Exception as e:
            JOB.update(state="error", error=f"{e.__class__.__name__}: {e}"[:300])
        finally:
            JOB["finished"] = time.time()
    threading.Thread(target=run, daemon=True, name="week-build").start()
    return dict(JOB)


def due(now: Optional[datetime] = None) -> Optional[date]:
    """The week to build now, or None: from Saturday 08:00 to Monday 09:00 Amsterdam, if its review is missing."""
    now = now or now_ams()
    wd, hm = now.weekday(), (now.hour, now.minute)
    if not ((wd == 5 and hm >= BUILD_AT) or wd == 6 or (wd == 0 and hm < (9, 0))):
        return None
    mon = last_week(now.date())
    return None if exists(mon.isoformat()) else mon


def exists(wk: str) -> bool:
    with store.conn() as c:
        return c.execute("SELECT 1 FROM week_review WHERE wk = ?", (wk,)).fetchone() is not None


def _tick():
    mon = due()
    if mon and JOB.get("state") != "running":
        JOB.update(state="running", started=time.time(), error=None, finished=None)
        try:
            build(mon)
            JOB.update(state="done", finished=time.time())
        except Exception as e:
            JOB.update(state="error", error=f"{e.__class__.__name__}: {e}"[:300], finished=time.time())
    # the note, once, after the Saturday build; a failure is tried again later, three times at most
    wk = last_week().isoformat()
    n = now_ams()
    if n.weekday() in (5, 6) and stored(wk) and not notes(wk):
        j = NOTE_JOBS.get(wk) or {}
        if j.get("state") == "running" or j.get("tries", 0) >= NOTE_TRIES:
            return
        if j.get("state") == "error" and time.time() - (j.get("finished") or 0) < NOTE_RETRY_S:
            return
        try:
            start_note(wk)
        except Exception as e:
            NOTE_JOBS[wk] = {"state": "error", "error": str(e)[:300], "finished": time.time(), "tries": j.get("tries", 0) + 1}


_THREAD: dict = {"t": None}


def _loop():
    time.sleep(120)
    while True:
        try:
            _tick()
        except Exception:
            pass
        time.sleep(600)


def ensure():
    t = _THREAD["t"]
    if t and t.is_alive():
        return
    store.init_db()
    t = threading.Thread(target=_loop, daemon=True, name="week-scheduler")
    _THREAD["t"] = t
    t.start()


def view(wk: Optional[str] = None) -> dict:
    from . import BUILD, earnai
    ensure()
    ws = weeks()
    wk = wk or (ws[0] if ws else last_week().isoformat())
    v = stored(wk)
    return {"build": BUILD, "week": wk, "weeks": ws, "review": v, "job": dict(JOB), "notes": notes(wk),
            "note_job": _pub(NOTE_JOBS.get(wk)), "writer": earnai.writer(), "next": "each Saturday at 08:00"}


def alerts() -> list[dict]:
    """At the weekend: the week's review is ready (one line, the first lines in its detail)."""
    n = now_ams()
    if n.weekday() < 5:
        return []
    wk = last_week(n.date()).isoformat()
    v = stored(wk)
    if not v:
        return []
    return [{"level": "info", "code": "WEEK", "text": f"The week of {date.fromisoformat(wk).day} {date.fromisoformat(wk).strftime('%b')} in review",
             "detail": ((v.get("lines") or [""])[0])[:150] + " · WEEK", "href": "/m/WEEK"}]
