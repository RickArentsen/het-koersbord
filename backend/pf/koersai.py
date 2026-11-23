"""
KAI — Koers AI: a paper-trading contest between Claude, Rick and the index.

The rules are the user's of 28 October 2026 (a first Koers AI, quality growth
with turbos, was withdrawn the night before):

  CONTEST   €100,000 of paper money from the moment Start is pressed. Koers
            AI is Rick's opponent, not an advisor: its decisions are moves in
            a game. It plays a quality compounder: durable advantages, high
            returns on capital, strong balance sheets, growing free cash
            flow, a reasonable price; 8 to 12 positions for one to three
            years, sold when the thesis breaks, not because the price fell.
  RULES     Long only: no leverage, shorting, options or crypto. Fractional
            shares. No position above 20% of the portfolio at the time of
            purchase; cash anywhere from 0 to 100%. It buys only tickers in
            the market data it was sent that turn: the hundred best quality
            compounders of the terminal's universe (US and European companies
            above $5bn, read by the watchlist's nightly scan) and its own
            holdings.
  WHEN      Once a week after the close: Friday from 23:00, once the nightly
            scan has read the day (a missed Friday is made up while the
            markets are shut). On another weekday evening on which a holding
            reported or moved 5% or more, it may check again.
  FILLS     At each share's next opening price on its own exchange, less
            0.01% in costs, never a price it already knew. A decision's
            orders settle together once every one of them has its open:
            sales first, so the buys can use their cash, and each buy cut to
            20% of the portfolio as it stands at that open.
  SCORE     Total return against the S&P 500 since the start, with the
            Sharpe ratio and the worst fall; Rick's return and the Nasdaq 100
            beside them. All in euros: the indices as SPY and QQQ with their
            dividends, the Sharpe ratio over the euro overnight rate (XEON).
  SIGHT     Rick's returns, never Rick's holdings: not the watchlist's "fit",
            no headline the news feed tied to Rick's book. No web search: the
            rules allow only the data in the message.
  SENT      (build 56, the user's choice of 14 November) besides the
            portfolio, scoreboard, journal, headlines and market data: its
            weights by sector, currency and listing; what became of its
            orders; each holding's latest reported quarter against the
            consensus; MKT's read of the markets (never its part about
            Rick's book).
  REVIEW    Once a month, on the first weekend from Saturday 10:00, a
            self-review: each thesis graded against what happened, its
            process, mistakes and rule changes. No trades; the next
            decisions read it.

Nothing here runs unless the server started the loop and Start was pressed:
every decision is one run of the writer on the plan.
"""

from __future__ import annotations

import bisect
import json
import math
import re
import statistics
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import store

AMS = ZoneInfo("Europe/Amsterdam")

CAPITAL = 100_000.0
COST = 0.0001                    # 0.01% of every trade's value
MAX_POS_W = 0.20                 # of the portfolio, at the time of purchase
MIN_TRADE = 10.0                 # euros: a buy cut below this by the limits is refused, not filled as dust
N_MARKET = 100
EVENT_MOVE = 5.0
DECIDE_AT = (23, 0)              # Amsterdam: after New York's close and the watchlist's 22:45 scan
WEEKLY_DAY = 4                   # Friday
STALE_DAYS = 4                   # an open that never appears in the bars: the order is refused after this many days
MIN_SHARPE_DAYS = 20             # trading days before a Sharpe ratio is shown at all
BENCH = {"spx": ("SPY", "S&P 500"), "ndx": ("QQQ", "Nasdaq 100")}
RF_SYM = "XEON.DE"               # the €STR fund: what a euro earns overnight
KIND_LABEL = {"start": "first", "weekly": "weekly", "event": "check", "manual": "asked"}


def now_ams() -> datetime:
    return datetime.now(AMS)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def _rows(sql: str, args=()) -> list[dict]:
    store.init_db()
    with store.conn() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def _j(s, default=None):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


def _s(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _r(v, d: int = 1):
    return round(v, d) if isinstance(v, (int, float)) and math.isfinite(v) else None


def started() -> Optional[dict]:
    s = store.get_setting("kai_start")
    if not s:
        return None
    return {"at": s, "d": s[:10], "capital": float(store.get_setting("kai_capital") or CAPITAL)}


def start(capital: float = CAPITAL, at: Optional[datetime] = None) -> dict:
    if started():
        raise ValueError("Koers AI has already started")
    at = (at or now_ams()).astimezone(AMS)
    store.set_setting("kai_capital", str(float(capital)))
    store.set_setting("kai_start", at.isoformat(timespec="seconds"))
    return started()


# --------------------------------------------------------------------------
# prices, in each share's own currency and in euros
# --------------------------------------------------------------------------

def bars(sym: str) -> dict:
    """{date: (open, high, low, close, adjusted close)} from the terminal's daily history."""
    from . import research
    out = {}
    for b in research.daily(sym)["bars"]:
        if b[4]:
            out[b[0]] = (b[1] or b[4], b[2] or b[4], b[3] or b[4], b[4], b[6] or b[4])
    return out


def _series(sym: str, cache: dict) -> tuple[list, dict]:
    if sym not in cache:
        b = bars(sym)
        cache[sym] = (sorted(b), b)
    return cache[sym]


def _on(sym: str, d: str, cache: dict) -> tuple[Optional[str], Optional[tuple]]:
    """The bar on a date, or the last one before it."""
    ds, b = _series(sym, cache)
    i = bisect.bisect_right(ds, d) - 1
    return (ds[i], b[ds[i]]) if i >= 0 else (None, None)


def _scan_rows() -> list[dict]:
    return (store.cache_get("wl:scan") or {}).get("rows") or []


def _ccy_of(sym: str) -> str:
    """The currency a symbol is quoted in: the watchlist's word for it, else its suffix."""
    for r in _scan_rows() + ((store.cache_get("wl:universe") or {}).get("rows") or []):
        if r.get("sym") == sym and r.get("ccy"):
            return r["ccy"]
    from . import pricing
    suf = sym.rsplit(".", 1)[1].upper() if "." in sym else ""
    return "GBp" if suf == "L" else pricing.SUFFIX_CCY.get(suf, "USD")


class FX:
    """Euros per unit of a currency on a date, the last rate before it when the day has none."""

    def __init__(self, start: str):
        self.start, self.cache = start, {}

    def eur(self, amount: float, ccy: str, d: str) -> float:
        if ccy in ("EUR", None):
            return amount
        scale = 1.0
        if ccy in ("GBp", "GBX"):
            ccy, scale = "GBP", 0.01
        if ccy not in self.cache:
            from . import pricing
            m = pricing.get_fx("EUR" + ccy, (date.fromisoformat(self.start) - timedelta(days=10)).isoformat())
            self.cache[ccy] = (sorted(m), m)
        days, m = self.cache[ccy]
        i = bisect.bisect_right(days, d) - 1
        if i < 0:
            raise LookupError(f"no {ccy} rate on {d}")
        return amount * scale / m[days[i]]


def fill_day(sym: str, after: datetime, now: Optional[datetime] = None) -> Optional[str]:
    """
    The first trading day whose opening bell rings after a moment, on the
    symbol's own exchange, once that bell has rung; None until then. A
    decision on Friday night fills New York and Amsterdam shares at Monday's
    opens; one at 13:30 would fill a New York share the same afternoon.
    """
    from . import sessions
    ex = sessions.exchange_of(sym)
    if not ex:
        return None
    now = now or now_ams()
    d = after.astimezone(AMS).date()
    for _ in range(12):
        s = sessions.session(ex, d)
        if s and s[0] > after:
            return d.isoformat() if s[0] <= now else None
        d += timedelta(days=1)
    return None


# --------------------------------------------------------------------------
# the ledger: what is held, from the trades
# --------------------------------------------------------------------------

def trades() -> list[dict]:
    return _rows("SELECT * FROM kai_trade ORDER BY d, id")


def holdings(upto: Optional[str] = None) -> dict:
    """{ticker: {qty, cost_eur, ccy, opened}} and the cash, after the trades through a date. Costs are in the basis."""
    st = started()
    cash = st["capital"] if st else CAPITAL
    pos: dict = {}
    for t in trades():
        if upto and t["d"] > upto:
            continue
        p = pos.setdefault(t["ticker"], {"qty": 0.0, "cost_eur": 0.0, "ccy": t["ccy"], "opened": t["d"]})
        if p["qty"] <= 1e-9:
            p["opened"] = t["d"]
        if t["side"] == "BUY":
            p["qty"] += t["qty"]
            p["cost_eur"] += t["value_eur"] + t["cost_eur"]
            cash -= t["value_eur"] + t["cost_eur"]
        else:
            if p["qty"] > 0:
                p["cost_eur"] *= max(0.0, (p["qty"] - t["qty"]) / p["qty"])
            p["qty"] -= t["qty"]
            cash += t["value_eur"] - t["cost_eur"]
    return {"pos": {k: v for k, v in pos.items() if v["qty"] > 1e-9}, "cash": cash}


def value_on(d: str, fx: FX, cache: dict) -> dict:
    """The portfolio at a day's close: cash plus every holding at its last close on or before that day, in euros."""
    h = holdings(d)
    total, rows = h["cash"], []
    for sym, p in h["pos"].items():
        bd, bar = _on(sym, d, cache)
        if not bar:
            continue
        v = fx.eur(p["qty"] * bar[3], p["ccy"], d)
        total += v
        rows.append({**p, "ticker": sym, "px": bar[3], "px_d": bd, "value_eur": v})
    return {"d": d, "value": total, "cash": h["cash"], "rows": rows}


# --------------------------------------------------------------------------
# the trades it asks for: checked against the rules
# --------------------------------------------------------------------------

def _amount(v):
    """A number of euros, or 'ALL'; None for anything else."""
    if isinstance(v, str):
        if v.strip().upper() == "ALL":
            return "ALL"
        v = v.replace("€", "").replace(",", "").strip()
    try:
        x = float(v)
    except Exception:
        return None
    return x if math.isfinite(x) and x > 0 else None


def check_trades(asked: list, book: dict, allowed: set, pending_buys: float = 0.0) -> tuple[list, list]:
    """
    The decision's trades kept to the rules, sales first: a sale of what is
    held, ALL or an amount (an amount within 0.1% of the position is all of
    it); a buy of a ticker in this turn's market data, cut to 20% of the
    portfolio and to the cash (with the sales' proceeds at the last price).
    The fill checks both again at the open.
    """
    ok, bad = [], []
    value = book["value"]
    cash = book["cash"] - pending_buys
    held = {r["ticker"]: r for r in book["rows"]}
    bought: dict = {}
    clean = []
    for t in asked or []:
        if not isinstance(t, dict):
            continue
        clean.append({"action": str(t.get("action") or "").strip().upper(), "ticker": str(t.get("ticker") or "").strip().upper(),
                      "amount": _amount(t.get("amount_eur")), "reason": _s(t.get("reason"), 400)})
    for t in [x for x in clean if x["action"] == "SELL"] + [x for x in clean if x["action"] != "SELL"]:
        why, note = None, None
        if t["action"] == "SELL":
            p = held.get(t["ticker"])
            if not p:
                why = "not held"
            elif t["amount"] is None:
                why = "a sale needs an amount in euros or ALL"
            else:
                if t["amount"] == "ALL" or t["amount"] >= p["value_eur"] * 0.999:
                    t["amount"] = "ALL"
                cash += (p["value_eur"] if t["amount"] == "ALL" else min(t["amount"], p["value_eur"])) * (1 - COST)
        elif t["action"] == "BUY":
            if t["ticker"] not in allowed:
                why = "not in this turn's market data"
            elif not isinstance(t["amount"], float):
                why = "a buy needs an amount in euros"
            else:
                have = (held.get(t["ticker"]) or {}).get("value_eur", 0.0) + bought.get(t["ticker"], 0.0)
                room, can = MAX_POS_W * value - have, cash / (1 + COST)
                eur = min(t["amount"], room, can)
                if t["amount"] < MIN_TRADE:
                    why = f"under €{MIN_TRADE:.0f}"
                elif room < MIN_TRADE:
                    why = "the position is already at 20% of the portfolio"
                elif can < MIN_TRADE:
                    why = "no cash left"
                else:
                    if eur < t["amount"] - 0.005:
                        note = f"cut to €{eur:,.2f}: " + ("20% of the portfolio" if room <= can else "the cash there is")
                    t["amount"] = round(eur, 2)
                    cash -= eur * (1 + COST)
                    bought[t["ticker"]] = bought.get(t["ticker"], 0.0) + eur
        else:
            why = f"unknown action '{t['action']}'"
        if why:
            bad.append({**t, "why": why})
        else:
            ok.append({**t, "note": note} if note else t)
    return ok, bad


def _place(decision_id: int, ts: datetime, orders: list) -> None:
    with store.conn() as c:
        for o in orders:
            c.execute("INSERT INTO kai_order (decision_id, ts, ticker, side, amount_eur, amount_all, reason, status) "
                      "VALUES (?,?,?,?,?,?,?, 'pending')",
                      (decision_id, ts.isoformat(timespec="seconds"), o["ticker"], o["action"],
                       None if o["amount"] == "ALL" else o["amount"], 1 if o["amount"] == "ALL" else 0, o["reason"]))


# --------------------------------------------------------------------------
# the fills: at the next open, a decision at a time
# --------------------------------------------------------------------------

def fill_pending(now: Optional[datetime] = None) -> list[dict]:
    """
    Every decision whose orders all have their opening price now, settled in
    turn: sales first, then buys in the order given. A decision still waiting
    for a bell holds back the ones after it, so the cash is always in order.
    """
    from . import sessions
    st = started()
    if not st:
        return []
    now = now or now_ams()
    fx, cache, done = FX(st["d"]), {}, []
    for did in [r["decision_id"] for r in _rows("SELECT DISTINCT decision_id FROM kai_order WHERE status='pending' "
                                                "ORDER BY decision_id")]:
        ready = []
        for o in _rows("SELECT * FROM kai_order WHERE status='pending' AND decision_id=? ORDER BY id", (did,)):
            if not sessions.exchange_of(o["ticker"]):
                ready.append((o, None, None, "its exchange's hours are not known here"))
                continue
            d = fill_day(o["ticker"], datetime.fromisoformat(o["ts"]), now)
            if not d:
                return done
            try:
                ds, b = _series(o["ticker"], cache)
            except Exception:
                return done
            if d not in b:
                if (now.date() - date.fromisoformat(d)).days > STALE_DAYS:
                    ready.append((o, d, None, f"no opening price for {d}"))
                    continue
                return done
            ready.append((o, d, b[d][0], None))
        for o, d, px, why in sorted(ready, key=lambda x: (x[0]["side"] != "SELL", x[0]["id"])):
            try:
                r = {"status": "rejected", "note": why} if why else _settle(o, d, px, fx, cache)
            except Exception as e:
                r = {"status": "rejected", "note": f"{e.__class__.__name__}: {e}"[:200]}
            with store.conn() as c:
                c.execute("UPDATE kai_order SET status=?, fill_d=?, note=? WHERE id=?", (r["status"], d, r.get("note"), o["id"]))
            done.append({**o, **r, "fill_d": d})
    return done


def _unit_at_open(sym: str, ccy: str, d: str, fx: FX, cache: dict) -> Optional[float]:
    """One share in euros at a day's open, or at the last close before it where that day has no bar."""
    bd, bar = _on(sym, d, cache)
    if not bar:
        return None
    return fx.eur(bar[0] if bd == d else bar[3], ccy, d)


def _settle(o: dict, d: str, px: float, fx: FX, cache: dict) -> dict:
    sym = o["ticker"]
    ccy = _ccy_of(sym)
    unit = fx.eur(px, ccy, d)
    h = holdings()
    p = h["pos"].get(sym)
    with store.conn() as c:
        if o["side"] == "SELL":
            if not p:
                return {"status": "rejected", "note": "no longer held"}
            qty = p["qty"] if o["amount_all"] else min(p["qty"], (o["amount_eur"] or 0.0) / unit)
            if qty >= p["qty"] * 0.999:
                qty = p["qty"]
            value = qty * unit
            if value < 0.01:
                return {"status": "rejected", "note": "nothing to sell"}
            c.execute("INSERT INTO kai_trade (order_id, d, ticker, side, qty, px, ccy, unit_eur, value_eur, cost_eur, note) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (o["id"], d, sym, "SELL", qty, px, ccy, unit, value, value * COST, ""))
            return {"status": "filled", "note": f"{qty:,.4f} at {px:,.2f} {ccy} (€{value:,.2f})"}
        total = h["cash"]
        for s, q in h["pos"].items():
            u = _unit_at_open(s, q["ccy"], d, fx, cache)
            total += q["qty"] * (u or 0.0)
        have = p["qty"] * unit if p else 0.0
        room, can = MAX_POS_W * total - have, h["cash"] / (1 + COST)
        eur = min(o["amount_eur"] or 0.0, room, can)
        if eur < MIN_TRADE:
            return {"status": "rejected", "note": ("20% of the portfolio reached at the open" if room < MIN_TRADE
                                                   else "not enough cash at the open")}
        qty = eur / unit
        c.execute("INSERT INTO kai_trade (order_id, d, ticker, side, qty, px, ccy, unit_eur, value_eur, cost_eur, note) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?)", (o["id"], d, sym, "BUY", qty, px, ccy, unit, eur, eur * COST,
                                                     "" if eur >= (o["amount_eur"] or 0) - 0.005 else
                                                     ("cut to 20% at the open" if room <= can else "cut to the cash")))
        return {"status": "filled", "note": f"{qty:,.4f} at {px:,.2f} {ccy} (€{eur:,.2f})"}


# --------------------------------------------------------------------------
# the scoreboard
# --------------------------------------------------------------------------

def _base_day(spx_days: list, at: datetime) -> Optional[str]:
    """The last New York close at or before the start: everyone stands at 100 there."""
    from . import sessions
    for d in reversed(spx_days):
        s = sessions.session("US", date.fromisoformat(d))
        if s and s[1] <= at:
            return d
    return None


def curve() -> dict:
    """Koers AI's value each New York trading day since the start, beside Rick, the S&P 500 and the Nasdaq 100, rebased to 100."""
    st = started()
    if not st:
        return {"available": False}
    fx, cache = FX(st["d"]), {}
    try:
        spx_days, spx = _series(BENCH["spx"][0], cache)
    except Exception as e:
        return {"available": False, "why": f"no S&P 500 prices ({e.__class__.__name__})"}
    base_d = _base_day(spx_days, datetime.fromisoformat(st["at"]))
    if not base_d:
        return {"available": False, "why": "no close before the start"}
    today = now_ams().date().isoformat()
    days = [d for d in spx_days if base_d <= d <= today]
    kai = [round(value_on(d, fx, cache)["value"], 2) for d in days]
    out = {"available": True, "d": days, "kai_eur": kai, "kai": [round(v / st["capital"] * 100, 3) for v in kai]}

    def rebased(sym):
        ccy = _ccy_of(sym)
        ser = []
        for d in days:
            bd, bar = _on(sym, d, cache)
            ser.append(fx.eur(bar[4], ccy, bd) if bar else None)
        base = ser[0]
        return [round(v / base * 100, 3) if base and v else None for v in ser]

    for k, (sym, _) in BENCH.items():
        try:
            out[k] = rebased(sym)
        except Exception:
            out[k] = None
    try:
        rf = rebased(RF_SYM)
        out["rf"] = [0.0] + [(rf[i] / rf[i - 1] - 1) if rf[i] and rf[i - 1] else 0.0 for i in range(1, len(days))]
    except Exception:
        out["rf"] = [0.0] * len(days)
    try:
        xs = _rick_index()
        base = next((xs[x] for x in sorted(xs, reverse=True) if x <= base_d), None)
        out["you"] = [round(xs[d] / base * 100, 3) if base and d in xs else None for d in days]
    except Exception:
        out["you"] = None
    return out


def _rick_index() -> dict:
    """Rick's own time-weighted index by day, from the dashboard's curve (rebuilt at most twice an hour)."""
    def build():
        from . import service
        ser = (service.dashboard("SP500").get("curve") or {}).get("series") or []
        return {"x": {p["d"]: p["x"] for p in ser if p.get("x")}}
    return store.cache_through("kai:you", 1800, build).get("x") or {}


def stats(cv: dict) -> dict:
    """Each line's total return, Sharpe ratio over the euro overnight rate, worst fall and swing; the trades' costs and record."""
    out = {}
    rf = cv.get("rf") or []
    for k in ("kai", "you", "spx", "ndx"):
        s = cv.get(k) or []
        pts = [(i, v) for i, v in enumerate(s) if v]
        if len(pts) < 2:
            out[k] = None
            continue
        r, ex = [], []
        for (i0, v0), (i1, v1) in zip(pts, pts[1:]):
            r.append(v1 / v0 - 1)
            ex.append(r[-1] - sum(rf[i0 + 1:i1 + 1]))
        peak, dd = pts[0][1], 0.0
        for _, v in pts:
            peak = max(peak, v)
            dd = min(dd, v / peak - 1)
        sd = statistics.stdev(ex) if len(ex) > 1 else 0.0
        vol = statistics.stdev(r) if len(r) > 1 else 0.0
        # A line that has not moved (all cash) has no Sharpe ratio: what is left is the overnight rate's own noise.
        out[k] = {"ret": round(pts[-1][1] / pts[0][1] * 100 - 100, 2), "max_dd": round(dd * 100, 2),
                  "sharpe": round(statistics.fmean(ex) / sd * 252 ** 0.5, 2) if sd > 0 and vol > 1e-6 and len(ex) >= MIN_SHARPE_DAYS
                  else None,
                  "vol": round(vol * 252 ** 0.5 * 100, 1) if vol > 0 else None, "days": len(pts) - 1}
    sales = _round_trips()
    tr = trades()
    out["trades"] = {"n": len(tr), "costs": round(sum(t["cost_eur"] for t in tr), 2),
                     "bought": round(sum(t["value_eur"] for t in tr if t["side"] == "BUY"), 2),
                     "sold": round(sum(t["value_eur"] for t in tr if t["side"] == "SELL"), 2),
                     "realised": round(sum(x["pnl"] for x in sales), 2), "sales": len(sales),
                     "hit": round(sum(1 for x in sales if x["pnl"] > 0) / len(sales) * 100) if sales else None}
    return out


def _round_trips() -> list[dict]:
    """Each sale against what that part had cost, costs on both sides counted."""
    out, pos = [], {}
    for t in trades():
        p = pos.setdefault(t["ticker"], {"qty": 0.0, "cost": 0.0})
        if t["side"] == "BUY":
            p["qty"] += t["qty"]
            p["cost"] += t["value_eur"] + t["cost_eur"]
        elif p["qty"] > 0:
            part = p["cost"] * min(1.0, t["qty"] / p["qty"])
            pnl = t["value_eur"] - t["cost_eur"] - part
            out.append({"ticker": t["ticker"], "d": t["d"], "pnl": round(pnl, 2), "pct": round(pnl / part * 100, 1) if part else None})
            p["cost"] -= part
            p["qty"] -= t["qty"]
    return out


# --------------------------------------------------------------------------
# what it is sent
# --------------------------------------------------------------------------

def universe() -> set:
    """What may be traded at all: the watchlist's companies, on an exchange whose opening hours the terminal knows."""
    from . import sessions
    syms = {r["sym"] for r in _scan_rows()} | {r["sym"] for r in (store.cache_get("wl:universe") or {}).get("rows") or []}
    return {s for s in syms if sessions.exchange_of(s)}


def _qc_ok(r: dict) -> bool:
    m = r.get("m") or {}
    return (((r.get("gs") or {}).get("quality") or 0) >= 70 and (m.get("fcf_margin") is None or m["fcf_margin"] > 0)
            and (m.get("nd_ebitda") is None or m["nd_ebitda"] <= 3.5))


def _steady(om, om3) -> float:
    """1 when this year's operating margin is its three-year average, 0 when it is half again or half of it."""
    if not isinstance(om, (int, float)) or not isinstance(om3, (int, float)) or om3 <= 0:
        return 0.5
    return max(0.0, 1.0 - abs(om - om3) / max(om3, 5.0) * 2)


def qc_score(r: dict) -> float:
    """
    A quality compounder in one number. The scan's scores are shares of its
    checks passed, so most good companies tie: the ties are broken by the
    return on capital, the free cash flow margin, three years of revenue
    growth, a light balance sheet and, most of all, a margin that holds. A
    first version without that last term (28 October) ranked a Chinese ADR
    first and took half a dozen gold miners into the hundred, each at the top
    of its cycle (operating margins far above their three-year average); a
    compounder's margin is steady.
    """
    g, m = r.get("gs") or {}, r.get("m") or {}
    clip = lambda v, hi: max(0.0, min(1.0, v / hi)) if isinstance(v, (int, float)) else 0.0
    nd = m.get("nd_ebitda")
    return (0.35 * (g.get("quality") or 0) + 0.1 * (g.get("growth") or 0) + 0.05 * (g.get("estimates") or 0)
            + 0.05 * (g.get("value") or 0) + 10 * clip(m.get("roic"), 40) + 8 * clip(m.get("fcf_margin"), 30)
            + 6 * clip(m.get("rev_cagr3"), 15) + 12 * _steady(m.get("om"), m.get("om_avg3"))
            + (4 * (1 - clip(nd, 3)) if isinstance(nd, (int, float)) else 2))


LEGEND = ("t ticker · n name · s sector · c the currency it trades in · px price in EUR · r1m, r3m, r12m return over 1, 3 and "
          "12 months (%) · ma200 200-day average in EUR · pe trailing P/E · fpe forward P/E · mcap market cap ($bn) · q, g the scan's "
          "quality and growth scores (0-100) · rg revenue growth this year (%) · rgn next year (%) · rg3 a year over three years (%) · "
          "gm, om, fcfm gross, operating and free cash flow margins (%) · om3 the operating margin's three-year average (%) · "
          "roic return on invested capital (%) · fcfy free cash flow yield (%) · nd net debt / EBITDA · epsg EPS growth next year (%) · "
          "est90 change in next year's EPS estimate over 90 days (%) · dy dividend yield (%) · rep next report date. "
          "Missing means the terminal does not have it.")


def pools(scan: dict, urows: list) -> tuple[set, set]:
    """
    What the market data is drawn from: the broad market, and Rick's stocks.

    The broad market is the scan's rows that are there for the market's own
    reasons: each region's largest companies (read in full since build 48),
    every stock 15% or more below its 52-week high, and the momentum leaders.
    Rick's pins are read too and are left out unless one qualifies anyway.

    Rick's stocks are every share Rick holds, sent whatever their score: the
    user's wish of 28 October that Koers AI can trade the same stocks. They go
    in unmarked, among the rest in sector order, and their "fit" never goes.
    """
    from . import watch
    lead = {s for v in (scan.get("leaders") or {}).values() for s in (v or []) if isinstance(s, str)}
    big = {r["sym"] for reg, k in watch.LARGE_N.items() for r in watch.largest(urows, reg, k)}
    rows = scan.get("rows") or []
    broad = {r["sym"] for r in rows if r["sym"] in big or r["sym"] in lead
             or (isinstance(r.get("dd52"), (int, float)) and r["dd52"] <= -watch.DEEP_DD)}
    rick = {r["sym"] for r in rows if (r.get("fit") or {}).get("held")}
    return broad, rick


def market_rows(held: set, fx: FX, today: str, n: int = N_MARKET) -> list[dict]:
    """
    The hundred best quality compounders of the broad market, every stock
    Rick holds and every one Koers AI holds: the only tickers it may buy this
    turn, in sector order and then by size, so nothing in the order says why
    a ticker is there.
    """
    ok = universe()
    sc = store.cache_get("wl:scan") or {}
    urows = (store.cache_get("wl:universe") or {}).get("rows") or []
    broad, rick = pools(sc, urows)
    scan = {r["sym"]: r for r in sc.get("rows") or []}
    uni = {r["sym"]: r for r in urows}
    top = [r["sym"] for r in sorted((r for r in scan.values() if r["sym"] in broad and r["sym"] in ok and _qc_ok(r)),
                                    key=qc_score, reverse=True)[:n]]
    pick = set(top) | {s for s in rick if s in ok} | set(held)
    out = []
    for sym in sorted(pick, key=lambda s: ((scan.get(s) or uni.get(s) or {}).get("sector") or "~",
                                           -((scan.get(s) or uni.get(s) or {}).get("mcap_usd") or 0), s)):
        r = scan.get(sym) or uni.get(sym)
        if not r or not r.get("px"):
            continue
        m, g, pr = r.get("m") or {}, r.get("gs") or {}, r.get("pr") or {}
        if not m and sym in held:
            m, g = holding_figures(sym, r, (store.cache_get("wl:universe") or {}).get("fx") or {})
        ccy = r.get("ccy") or _ccy_of(sym)
        try:
            px = fx.eur(r["px"], ccy, today)
        except Exception:
            continue
        sp = pr.get("spark") or []
        ret = lambda k: _r((sp[-1] / sp[-1 - k] - 1) * 100) if len(sp) > k and sp[-1 - k] else None
        gap = pr.get("ma200_gap")
        row = {"t": sym, "n": _s(r.get("name"), 40), "s": r.get("sector"), "c": ccy, "px": _r(px, 2),
               "r1m": ret(4), "r3m": ret(13), "r12m": _r(pr.get("r12")),
               "ma200": _r(px / (1 + gap / 100), 2) if isinstance(gap, (int, float)) and gap > -100 else None,
               "pe": _r(r.get("tpe")), "fpe": _r(r.get("fpe")), "mcap": _r((r.get("mcap_usd") or 0) / 1e9, 0),
               "q": g.get("quality"), "g": g.get("growth"), "rg": _r(m.get("rev_g_fy")), "rgn": _r(m.get("rev_g_ny")),
               "rg3": _r(m.get("rev_cagr3")), "gm": _r(m.get("gm")), "om": _r(m.get("om")), "om3": _r(m.get("om_avg3")),
               "fcfm": _r(m.get("fcf_margin")), "roic": _r(m.get("roic")), "fcfy": _r(m.get("fcf_yield")),
               "nd": _r(m.get("nd_ebitda")), "epsg": _r(m.get("eps_g_ny")), "est90": _r(m.get("est_ny_90")), "dy": _r(r.get("dy")),
               "rep": _next_report(r.get("earn_ts"), today)}
        out.append({k: v for k, v in row.items() if v is not None})
    return out


def holding_figures(sym: str, row: dict, usd: dict) -> tuple[dict, dict]:
    """
    (metrics, group scores) for a holding the evening scan did not read in full,
    from the scan's own caches of its statements and estimates: nothing is
    fetched (build 54). From 4 to 13 November ResMed reached Koers AI as a price,
    a P/E and a yield only, and it said so in every turn: it had been bought in
    a dip, and once the dip recovered the scan read it no more.
    """
    from . import watch
    try:
        f, _ = watch.cached(f"wl:fin:{sym}", 0, dict, fetch=False)
        e, _ = watch.cached(f"wl:est:{sym}", 0, dict, fetch=False)
        if not (f or e):
            return {}, {}
        m = watch.metrics(dict(row), f or {}, e or {}, usd)
        return {k: v for k, v in m.items() if k != "summary"}, watch.group_scores(watch.checks(m, row, {}))
    except Exception:
        return {}, {}


def _next_report(ts, today: str) -> Optional[str]:
    """A report date still to come; the scan keeps a past one until Yahoo moves it on."""
    if not ts:
        return None
    d = datetime.fromtimestamp(ts, AMS).date().isoformat()
    return d if d >= today else None


MARKET_FEEDS = "^GSPC,^IXIC,^DJI"
HEADLINE_DAYS = 4
PER_DAY = 6                      # market headlines from any one day, so a weekly decision sees the week
MARKET_MAX = 20
PER_HOLDING = 2                  # from each holding's own feed, which go first: one feed for ten names was mostly Visa
# Listicles and penny-stock notices: in the 13 November material they took a fifth of the thirty lines.
JUNK_RX = re.compile(r"\b(stocks? to buy|according to reddit|multibaggers?|insane upside|reverse (share|stock) split|"
                     r"investor relations agreement|millionaires?|should you buy|buy and hold forever|"
                     r"\d+ (top|best|great|cheap|unstoppable|magnificent) (\w+ )?stocks?)\b", re.I)


def headlines(held: set, now: datetime) -> list[str]:
    """
    The last four days: first two headlines from Yahoo's feed on each of
    Koers AI's own holdings, then the market's, at most six from any one day:
    the news feed's general headlines (never one it tied to Rick's book) and
    Yahoo's feeds for the three US indices first, the news log's untagged
    headlines after them. Until 14 November they were one list cut at thirty,
    newest first, so the Friday decision of 13 November read Friday evening
    from 21:28 to 22:49 and none of the week it was judging.
    """
    from . import news, reader
    t_now = now.timestamp()
    cut, seen = t_now - HEADLINE_DAYS * 86400, set()

    def take(items, n, cap_day=None):
        out, per = [], {}
        for pri, ts, src, h in sorted(items, key=lambda x: (x[0], -x[1])):
            key = re.sub(r"\W+", " ", h.lower()).strip()
            if ts < cut or ts > t_now or key in seen or JUNK_RX.search(h):
                continue
            day = datetime.fromtimestamp(ts, AMS).date()
            if cap_day and per.get(day, 0) >= cap_day:
                continue
            seen.add(key)
            per[day] = per.get(day, 0) + 1
            out.append((ts, f"- {datetime.fromtimestamp(ts, AMS):%d %b %H:%M} · {_s(src, 40)}: {_s(h, 220)}"))
            if len(out) >= n:
                break
        return out

    mine = []
    for sym in sorted(held):
        try:
            mine += take([(0, x.get("ts") or 0, x.get("source") or "Yahoo", x["title"]) for x in reader._rss(sym)], PER_HOLDING)
        except Exception:
            pass
    general = [(0, x.get("ts") or 0, x.get("source") or "", x["headline"])
               for x in (store.cache_get("news") or {}).get("items") or []
               if isinstance(x, dict) and x.get("headline") and not x.get("mine")]
    try:
        general += [(0, x.get("ts") or 0, x.get("source") or "Yahoo", x["title"]) for x in reader._rss(MARKET_FEEDS)]
    except Exception:
        pass
    # The feeds above hold about a day; the news log keeps ten. Its untagged items only: a company headline
    # there was fetched for one of Rick's stocks, and which ones were fetched would show his book.
    # Within a day: top news and business before currency wraps, and small deals ("merger") last.
    try:
        general += [({"top news": 1, "business": 1, "forex": 2}.get(x.get("category"), 3), x.get("ts") or 0,
                     x.get("source") or "", x["headline"]) for x in news.logged(cut, t_now)
                    if x.get("headline") and not x.get("tickers")]
    except Exception:
        pass
    rest = take(general, MARKET_MAX, PER_DAY)
    return [x for _, x in sorted(mine, key=lambda x: -x[0])] + [x for _, x in sorted(rest, key=lambda x: -x[0])]


# --------------------------------------------------------------------------
# its holdings' latest results (build 56)
# --------------------------------------------------------------------------
#
# Four times in its first two weeks it said it could not judge a report it had
# only headlines about (BKNG on 3 November, GOOGL). Each holding's latest
# quarter now comes from what the terminal keeps: Yahoo's earnings history
# for the EPS against the estimate and the report's time (the statements' EPS
# is GAAP, the consensus adjusted, and TSMC's is per ADR in dollars), the
# watchlist scan's quarterly statements for revenue and margins, the
# consensus record (pf/consensus.py) for the revenue expected and how the
# forecasts moved since, and the daily bars for the shares' reaction.

EARN_TTL = 12 * 3600


def _load_earn(sym: str) -> dict:
    from . import research
    ed = research._tk(sym).get_earnings_dates(limit=8)
    rows = []
    if ed is not None and not ed.empty:
        for idx, r in ed.iterrows():
            rows.append({"ts": idx.timestamp(), "est": research._f(r.get("EPS Estimate")),
                         "act": research._f(r.get("Reported EPS")), "sur": research._f(r.get("Surprise(%)"))})
    return {"rows": rows}


def earn_dates(sym: str) -> list[dict]:
    """Yahoo's report dates for a share, EPS expected and reported; asked again within the hour once a report is due."""
    key = f"kai:earn:{sym}"
    hit = store.cache_get(key) or {}
    due = any(r.get("act") is None and r.get("ts") and r["ts"] < time.time() - 1800 for r in hit.get("rows") or [])
    try:
        return store.cache_through(key, 3600 if due else EARN_TTL, lambda: _load_earn(sym)).get("rows") or []
    except Exception:
        return hit.get("rows") or []


def moved_session(sym: str, ts: float) -> tuple[Optional[str], Optional[str]]:
    """
    (the session a report moved, when it came against that exchange's own
    hours): its own day when it came before the close, the next one after it.
    """
    from . import sessions
    ex = sessions.exchange_of(sym)
    if not ex:
        return None, None
    t = datetime.fromtimestamp(ts, timezone.utc)
    d = t.astimezone(ZoneInfo(sessions.EXCHANGES[ex][0])).date()
    s = sessions.session(ex, d)
    when = ("before the open" if t < s[0] else "during the session" if t < s[1] else "after the close") if s else None
    for _ in range(10):
        s = sessions.session(ex, d)
        if s and t < s[1]:
            return d.isoformat(), when
        d += timedelta(days=1)
    return None, when


def _near_end(a: Optional[str], b: Optional[str], days: int = 12) -> bool:
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days) <= days
    except Exception:
        return False


def latest_result(sym: str, now: Optional[datetime] = None, cache: Optional[dict] = None) -> dict:
    """A holding's latest reported quarter, as far as the terminal has it; nothing is fetched but Yahoo's report dates."""
    from . import consensus, watch
    now = now or now_ams()
    t_now = now.timestamp()
    ed = earn_dates(sym)
    past = [r for r in ed if r.get("ts") and r["ts"] <= t_now and r.get("act") is not None]
    nxt = min((r["ts"] for r in ed if r.get("ts") and r["ts"] > t_now), default=None)
    out: dict = {"t": sym, "next": datetime.fromtimestamp(nxt, AMS).date().isoformat() if nxt else None}
    rep = max(past, key=lambda r: r["ts"]) if past else None
    if not rep:
        return out
    rt = datetime.fromtimestamp(rep["ts"], AMS)
    rep_d = rt.date().isoformat()
    out.update(rep_d=rep_d, rep_ts=rep["ts"], eps=rep.get("act"), eps_est=rep.get("est"), eps_sur=_r(rep.get("sur")))
    # the shares' reaction: the session the report moved, against the close before it
    day, out["when"] = moved_session(sym, rep["ts"])
    try:
        ds, b = _series(sym, cache if cache is not None else {})
        if day and day in b:
            i = ds.index(day)
            if i >= 1:
                base = b[ds[i - 1]][3]
                out.update(move_d=day, move=_r((b[day][3] / base - 1) * 100), since=_r((b[ds[-1]][3] / base - 1) * 100))
    except Exception:
        pass
    # the quarter in the statements: the newest one that ended in the 120 days before the report
    f, _ = watch.cached(f"wl:fin:{sym}", 0, dict, fetch=False)
    e, _ = watch.cached(f"wl:est:{sym}", 0, dict, fetch=False)
    fin_ccy = ((e or {}).get("info") or {}).get("financialCurrency")
    qs = [x for x in (f or {}).get("q") or [] if x.get("end") and x.get("revenue")]
    q = next((x for x in sorted(qs, key=lambda x: x["end"], reverse=True)
              if x["end"] < rep_d and (date.fromisoformat(rep_d) - date.fromisoformat(x["end"][:10])).days <= 120), None)
    if q:
        ya = next((x for x in qs if _near_end(x["end"], (date.fromisoformat(q["end"][:10]) - timedelta(days=365)).isoformat(), 20)), None)
        out.update(q_end=q["end"][:10], rev=q["revenue"], ccy=fin_ccy, om=_r(q.get("op_margin")), fcfm=_r(q.get("fcf_margin")),
                   om_ya=_r((ya or {}).get("op_margin")),
                   rev_yoy=_r((q["revenue"] / ya["revenue"] - 1) * 100) if ya and ya.get("revenue") else None)
    # the consensus before the report and now
    try:
        rows = consensus.history(sym, consensus.siblings(sym))["rows"]
    except Exception:
        rows = []
    before = [r for r in rows if r["d"] < rep_d]
    after = rows[-1] if rows and rows[-1]["d"] >= rep_d else None
    pre = next((r for r in reversed(before) if q and _near_end(r.get("end_0q"), q["end"])), None)
    last = before[-1] if before else None
    if pre and q and pre.get("rev_0q") and pre.get("rev_ccy") and pre["rev_ccy"] == fin_ccy:
        out.update(rev_est=pre["rev_0q"], rev_sur=_r((q["revenue"] / pre["rev_0q"] - 1) * 100))
    if last and after:
        # the next quarter: what was expected for it before the report, and now
        if last.get("rev_1q") and after.get("rev_0q") and _near_end(last.get("end_1q"), after.get("end_0q")) \
                and last.get("rev_ccy") == after.get("rev_ccy"):
            out.update(nq_rev_before=last["rev_1q"], nq_rev_now=after["rev_0q"], nq_ccy=after.get("rev_ccy"),
                       nq_chg=_r((after["rev_0q"] / last["rev_1q"] - 1) * 100))
        for k in ("1y", "0y"):
            if last.get(f"eps_{k}") and after.get(f"eps_{k}") and last.get(f"end_{k}") == after.get(f"end_{k}"):
                out.update(eps_y_before=last[f"eps_{k}"], eps_y_now=after[f"eps_{k}"], eps_y_end=after[f"end_{k}"],
                           eps_y_chg=_r((after[f"eps_{k}"] / last[f"eps_{k}"] - 1) * 100))
                break
        if last.get("tgt_mean") and after.get("tgt_mean") and last.get("px_ccy") == after.get("px_ccy"):
            out.update(tgt_before=last["tgt_mean"], tgt_now=after["tgt_mean"], tgt_ccy=after.get("px_ccy"))
    return out


def _bn(v: Optional[float], ccy: Optional[str]) -> str:
    if v is None:
        return "–"
    return (f"{v / 1e9:,.2f}bn" if abs(v) >= 1e9 else f"{v / 1e6:,.0f}m") + (f" {ccy}" if ccy else "")


def _dm(iso: Optional[str], year: bool = False) -> str:
    try:
        d = date.fromisoformat(iso[:10])
        return f"{d.day} {d:%b}" + (f" {d.year}" if year else "")
    except Exception:
        return "–"


def result_line(r: dict, since: Optional[str] = None) -> str:
    """One holding's latest quarter in a sentence or three; `since` marks a report after the last turn."""
    t = r["t"] + (f" ({_s(r.get('n'), 40)})" if r.get("n") else "")
    nxt = f" Next report {_dm(r['next'], True)}." if r.get("next") else ""
    if not r.get("rep_d"):
        return f"- {t}: no reported quarter in the terminal.{nxt}"
    new = " NEW since your last turn:" if since and r.get("rep_ts") and r["rep_ts"] > since_ts(since) else ""
    head = (f"- {t},{new} {'quarter to ' + _dm(r['q_end'], True) + ', ' if r.get('q_end') else ''}reported {_dm(r['rep_d'])}"
            + (f" {r['when']}" if r.get("when") else "") + ":")
    parts = []
    if r.get("eps") is not None:
        parts.append(f"EPS {r['eps']:g}" + (f" against {r['eps_est']:g} expected ({r['eps_sur']:+.1f}%)"
                                             if r.get("eps_est") is not None and r.get("eps_sur") is not None else ""))
    if r.get("rev") is not None:
        parts.append(f"revenue {_bn(r['rev'], r.get('ccy'))}" + (f", {r['rev_yoy']:+.1f}% on a year" if r.get("rev_yoy") is not None else "")
                     + (f", against {_bn(r['rev_est'], None)} expected ({r['rev_sur']:+.1f}%)" if r.get("rev_sur") is not None else ""))
    if r.get("om") is not None:
        parts.append(f"operating margin {r['om']:.1f}%" + (f" ({r['om_ya']:.1f}% a year earlier)" if r.get("om_ya") is not None else ""))
    if r.get("fcfm") is not None:
        parts.append(f"free cash flow margin {r['fcfm']:.1f}%")
    if r.get("rep_d") and r.get("rev") is None:
        parts.append("the quarter's statements are not in the terminal")
    s = f"{head} {'; '.join(parts)}."
    if r.get("move") is not None:
        s += f" The shares {r['move']:+.1f}% on {_dm(r['move_d'])}, {r['since']:+.1f}% since."
    after = []
    if r.get("nq_chg") is not None:
        after.append(f"the next quarter's revenue consensus {_bn(r['nq_rev_before'], None)} -> {_bn(r['nq_rev_now'], r.get('nq_ccy'))} "
                     f"({r['nq_chg']:+.1f}%)")
    if r.get("eps_y_chg") is not None:
        after.append(f"EPS for the year to {_dm(r['eps_y_end'], True)} {r['eps_y_before']:.2f} -> {r['eps_y_now']:.2f} ({r['eps_y_chg']:+.1f}%)")
    if r.get("tgt_now") is not None:
        after.append(f"the mean price target {r['tgt_before']:,.0f} -> {r['tgt_now']:,.0f} {r.get('tgt_ccy') or ''}".rstrip())
    if after:
        s += " Since the report: " + "; ".join(after) + "."
    return s + nxt


def since_ts(iso: str) -> float:
    try:
        return datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def results_block(held: set, names: dict, now: datetime, last_turn: Optional[str]) -> list[str]:
    if not held:
        return []
    cache: dict = {}
    out = []
    for sym in sorted(held):
        try:
            r = latest_result(sym, now, cache)
        except Exception as e:
            r = {"t": sym, "err": e.__class__.__name__}
        r["n"] = names.get(sym)
        out.append(result_line(r, last_turn) if "err" not in r else f"- {sym}: its results could not be read ({r['err']}).")
    return ["", "YOUR HOLDINGS' LATEST RESULTS (each one's last reported quarter: EPS as Yahoo records it against the "
                "consensus, revenue and margins from its statements, the revenue consensus as the terminal recorded it before "
                "the report, the shares' move on the session the report reached; 'next quarter' is the analysts' new "
                "consensus against what they expected for it before the report: how the Street read the outlook)", *out]


# --------------------------------------------------------------------------
# the markets, from MKT's read (build 56)
# --------------------------------------------------------------------------

READ_LEVELS = (("SPX", "S&P 500"), ("NDX", "Nasdaq 100"), ("SX5E", "Euro Stoxx 50"), ("AEX", "AEX"), ("US10Y", "US 10-year"),
               ("US2Y", "US 2-year"), ("EA10Y", "Euro-area 10-year"), ("VIX", "VIX"), ("USHY", "US high yield spread"),
               ("EURUSD", "EUR/USD"), ("BRENT", "Brent"), ("GOLD", "Gold"), ("COPPER", "Copper"))


def markets_block() -> list[str]:
    """
    The terminal's read of the markets (pf/market.risk_read) and the levels
    behind it. Only the market's own parts: the read's estimate for Rick's
    book, its link to READ (picked for his holdings) and the board's
    correlation with his portfolio never go.
    """
    b = store.cache_get("market") or {}
    R = b.get("risk") or {}
    rows = {r.get("k"): r for r in b.get("rows") or [] if isinstance(r, dict)}
    if not R.get("horizons") or not rows:
        return []
    hz = {h["h"]: h for h in R["horizons"]}
    f = lambda h: f"{hz[h]['verdict']} ({hz[h]['composite']:+.2f})" if h in hz and hz[h].get("composite") is not None else "n/a"
    st = R.get("stress") or {}
    L = ["", f"THE MARKETS (the terminal's rule-based read of the last session, {_dm(R.get('session'), True)}: each move in its "
             "own normal days, groups averaged; not a forecast)",
         f"Session {f('1D')}, week {f('1W')}, month {f('1M')}."
         + (f" Stress {st['label']} ({st['pctile']}th percentile of the year across the fear gauges)." if st.get("label") else ""),
         *[f"- {s}" for s in R.get("story") or []],
         *[f"- Note: {x}" for x in R.get("flags") or []]]

    def lv(k, lab):
        r = rows.get(k) or {}
        if r.get("last") is None:
            return None
        rate = r.get("u") == "pct"
        ch = lambda h: (f"{r[h + '_abs'] * 100:+.0f}bp" if r.get(h + "_abs") is not None else "–") if rate else \
            (f"{r[h + '_pct']:+.1f}%" if r.get(h + "_pct") is not None else "–")
        level = f"{r['last']:.2f}%" if rate else (f"{r['last']:.4f}" if r.get("u") == "fx" else f"{r['last']:,.2f}")
        return f"{lab} {level} (day {ch('d')}, week {ch('w')}, month {ch('m')}, year to date {ch('y')})"
    lvls = [x for x in (lv(k, lab) for k, lab in READ_LEVELS) if x]
    if lvls:
        L.append("Levels: " + "; ".join(lvls) + ".")
    return L


# --------------------------------------------------------------------------
# what became of its orders, and where its money sits (build 56)
# --------------------------------------------------------------------------

def orders_block(now: datetime, days: int = 14) -> list[str]:
    """Its orders of the last two weeks and what became of each: filled, cut at the open, refused at the decision or the open."""
    cut = (now - timedelta(days=days)).isoformat(timespec="seconds")
    decs = _rows("SELECT id, ts, kind, rejected FROM kai_decision WHERE ts >= ? ORDER BY id", (cut,))
    L = []
    for d in decs:
        tag = f"{_dm(d['ts'])} ({KIND_LABEL.get(d['kind'], d['kind'])})"
        for o in _rows("SELECT o.*, t.qty, t.px, t.ccy, t.value_eur, t.note AS tnote FROM kai_order o "
                       "LEFT JOIN kai_trade t ON t.order_id = o.id WHERE o.decision_id = ? ORDER BY o.id", (d["id"],)):
            amt = "ALL" if o["amount_all"] else f"EUR {o['amount_eur']:,.0f}"
            if o["status"] == "filled" and o.get("qty") is not None:
                fate = (f"filled at the open on {_dm(o['fill_d'])}: {o['qty']:,.4f} at {o['px']:,.2f} {o['ccy'] or ''}, EUR {o['value_eur']:,.2f}"
                        + (f" ({o['tnote']})" if o.get("tnote") else ""))
            elif o["status"] == "filled":
                fate = f"filled on {_dm(o['fill_d'])}: {o['note'] or ''}"
            elif o["status"] == "pending":
                fate = "still waiting for its open"
            else:
                fate = f"refused at the open: {o['note'] or 'no reason recorded'}"
            L.append(f"- {tag}: {o['side']} {o['ticker']} {amt} -> {fate}")
        for t in _j(d["rejected"], []) or []:
            amt = t.get("amount")
            amt = "ALL" if amt == "ALL" else (f"EUR {amt:,.0f}" if isinstance(amt, (int, float)) else str(amt or ""))
            L.append(f"- {tag}: {t.get('action')} {t.get('ticker')} {amt} -> refused at the decision: {t.get('why')}")
    return ["", f"WHAT BECAME OF YOUR ORDERS (the last {days} days)", *(L or ["None in the last two weeks."])]


def _meta_of(sym: str) -> dict:
    for r in _scan_rows() + ((store.cache_get("wl:universe") or {}).get("rows") or []):
        if r.get("sym") == sym:
            return r
    return {}


def weights_lines(book: dict) -> list[str]:
    """Where its money sits: by sector, by currency and by region, cash included."""
    v = book["value"] or 1.0
    sec, ccy, reg = {}, {}, {}
    from . import sessions
    for p in book["rows"]:
        m = _meta_of(p["ticker"])
        w = p["value_eur"] / v * 100
        s = m.get("sector") or "unknown sector"
        sec.setdefault(s, [0.0, []])
        sec[s][0] += w
        sec[s][1].append(p["ticker"])
        c = "GBP" if p["ccy"] in ("GBp", "GBX") else (p["ccy"] or "?")
        ccy[c] = ccy.get(c, 0.0) + w
        r = "US" if sessions.exchange_of(p["ticker"]) == "US" else "Europe"
        reg[r] = reg.get(r, 0.0) + w
    cash = book["cash"] / v * 100
    fmt = lambda d: ", ".join(f"{k} {x:.1f}%" for k, x in sorted(d.items(), key=lambda kv: -kv[1]))
    out = ["Weights by sector: " + ", ".join(f"{k} {x[0]:.1f}% ({', '.join(x[1])})" for k, x in sorted(sec.items(), key=lambda kv: -kv[1][0]))
           + f", cash {cash:.1f}%."] if sec else []
    if ccy:
        out.append(f"By currency: {fmt(ccy)}, cash (EUR) {cash:.1f}%. By listing: {fmt(reg)}.")
    return out


def current_theses() -> dict:
    """The latest thesis it gave for each ticker."""
    out = {}
    for d in _rows("SELECT theses FROM kai_decision ORDER BY id DESC LIMIT 60"):
        for t in _j(d["theses"], []) or []:
            out.setdefault(t.get("ticker"), t)
    return out


SYSTEM = """You are Koers AI, an autonomous portfolio manager in a paper-trading contest. You manage a simulated portfolio: no real money is involved and you never place real orders. Your opponents are:
1. Rick, a human trader managing their own real portfolio
2. The benchmarks, the S&P 500 and the Nasdaq 100

Your goal: finish the contest ahead of both on risk-adjusted return.

## Your role
- Your decisions are moves in a game, not recommendations to Rick or anyone else.
- You are Rick's opponent, not their advisor. If they ask what they should buy, you can say what you are doing and why, but you don't make calls on their real portfolio.
- You see Rick's returns on the scoreboard, not their holdings. Don't mirror or dodge them. Play your own strategy.

## Contest rules
- Start: the contest's first day (on the scoreboard), with €100,000 in paper money. All values in EUR; the terminal handles currency conversion.
- Universe: stocks listed in the US and Europe, nearly all with a market cap above $5bn. Only trade tickers that appear in the market data you're given.
- Long only. No leverage, shorting, options or crypto. Fractional shares allowed.
- No position above 20% of portfolio value at the time of purchase. Cash may be anywhere from 0-100%.
- You decide once per week after market close. If there are earnings for a stock you own, or if a stock you own moves 5%+ in a day, you can check too. Orders fill at the next session's opening price minus 0.01% transaction costs, so you never know your fill price in advance.
- The terminal keeps the ledger. The cash, positions and values it sends you are the truth; don't recompute or override them.

## Scoring
Primary: total return vs the S&P 500 since the start date.
Also tracked: Sharpe ratio and maximum drawdown. Beating the index by taking a 50% drawdown is not a win.

## Investor profile
Quality compounder: You buy companies with durable competitive advantages, high return on capital, strong balance sheets and growing free cash flow, at a reasonable price. Horizon 1-3 years, 8-12 positions. You sell when the thesis breaks (margins erode, competitive position weakens, capital is misallocated) or the valuation becomes extreme, not because the price fell. Low turnover.

Stay in character. Consistency beats cleverness: don't abandon your strategy after one bad week, and don't chase whatever just worked for someone else.

## Information discipline
- Use only the data in the current message plus general knowledge of how businesses and markets work, as of today.
- Never state a price, financial figure or news item that isn't in the input. If you need data you don't have, say so in the journal and don't trade on a guess.

## Each turn, you receive
- Date
- Your portfolio: cash, positions (ticker, shares, avg cost, current value, P/L), and your weights by sector, currency and listing
- What became of your orders of the last two weeks: filled, cut, or refused and why
- Your holdings' latest reported quarters: EPS and revenue against the consensus, margins, the shares' reaction, how the forecasts moved since
- Scoreboard: you vs Rick vs the S&P 500 and the Nasdaq 100
- Your last monthly self-review, once you have written one
- Your last 5 journal entries
- The terminal's rule-based read of the markets and the main levels
- Recent headlines
- Market data for the universe (price, 1m/3m/12m return, 200-day average, P/E, sector, and whatever fundamentals the terminal provides)

## Process
1. Review: what changed since your last decision? Did any thesis break?
2. Check your exit rules on current holdings first.
3. Then look for new opportunities that fit your profile.
4. Size positions within the rules. Doing nothing is a valid decision, and often the right one.

## Output
Respond with only this JSON:

{
  "date": "YYYY-MM-DD",
  "trades": [
    {"action": "BUY",  "ticker": "ASML.AS", "amount_eur": 1500, "reason": "one sentence"},
    {"action": "SELL", "ticker": "XYZ",     "amount_eur": "ALL", "reason": "one sentence"}
  ],
  "theses": [
    {"ticker": "ASML.AS", "thesis": "why you own it", "exit_if": "what would make you sell"}
  ],
  "journal": "3-6 sentences: what you saw, what you did, what you're watching. Name your own mistakes plainly.",
  "message_to_rival": "optional one-liner for the scoreboard"
}"""


def _turn(kind: str, trigger: str) -> str:
    if kind == "start":
        return "the first decision: the contest starts now, with EUR 100,000 in cash."
    if kind == "weekly":
        return "the weekly decision, after the close." + (f" Also today: {trigger}" if trigger else "")
    if kind == "event":
        return f"a check between weekly decisions, because {trigger} Doing nothing is a valid decision, and often the right one."
    return "a decision asked for outside the schedule."


def material(kind: str, trigger: str = "") -> tuple[str, list[str]]:
    """The turn as Koers AI reads it, and the tickers it may buy this turn."""
    st = started()
    n = now_ams()
    today = n.date().isoformat()
    fx, cache = FX(st["d"]), {}
    book = value_on(today, fx, cache)
    held = {r["ticker"] for r in book["rows"]}
    rows = market_rows(held, fx, today)
    names = {r["t"]: r.get("n") for r in rows}
    th = current_theses()
    L = [f"DATE: {today} ({n:%A %d %B %Y}, {n:%H:%M} in Amsterdam). Orders fill at each share's next opening price on its own exchange.",
         f"THIS TURN: {_turn(kind, trigger)}", "",
         "YOUR PORTFOLIO (the terminal's ledger: these numbers are the truth)",
         f"Cash EUR {book['cash']:,.2f} ({book['cash'] / book['value'] * 100:.1f}% of the portfolio). "
         f"Total value EUR {book['value']:,.2f}."]
    if book["rows"]:
        L.append("ticker | name | shares | avg cost EUR | price EUR | value EUR | weight | P/L EUR | P/L % | held since")
        for p in sorted(book["rows"], key=lambda r: -r["value_eur"]):
            unit = p["value_eur"] / p["qty"]
            L.append(f"{p['ticker']} | {names.get(p['ticker']) or ''} | {p['qty']:,.4f} | {p['cost_eur'] / p['qty']:,.2f} | {unit:,.2f} | "
                     f"{p['value_eur']:,.2f} | {p['value_eur'] / book['value'] * 100:.1f}% | {p['value_eur'] - p['cost_eur']:+,.2f} | "
                     f"{(p['value_eur'] / p['cost_eur'] - 1) * 100:+.1f}% | {p['opened']}")
        L += weights_lines(book)
        mine = [f"- {t}: {th[t].get('thesis')} Exit if: {th[t].get('exit_if')}" for t in sorted(held) if t in th]
        if mine:
            L += ["Your theses on them:", *mine]
    else:
        L.append("No positions: all cash.")
    pend = _rows("SELECT ticker, side, amount_eur, amount_all, ts FROM kai_order WHERE status='pending' ORDER BY id")
    if pend:
        L += ["", "ORDERS STILL WAITING FOR THEIR OPEN",
              *[f"- {o['side']} {o['ticker']} " + ("ALL" if o["amount_all"] else f"EUR {o['amount_eur']:,.2f}") for o in pend]]
    last_turn = (_rows("SELECT ts FROM kai_decision ORDER BY id DESC LIMIT 1") or [{}])[0].get("ts")
    L += orders_block(n)
    L += results_block(held, names, n, last_turn)
    try:
        cv = curve()
        s = stats(cv) if cv.get("available") else {}
    except Exception:
        cv, s = {}, {}
    line = lambda lab, x: (f"- {lab}: {x['ret']:+.2f}%, Sharpe {x['sharpe'] if x['sharpe'] is not None else 'n/a'}, "
                           f"max drawdown {x['max_dd']:.2f}%" if x else f"- {lab}: no data yet")
    L += ["", f"SCOREBOARD since the start ({st['d']}; in EUR; total return, Sharpe ratio over the euro overnight rate, max drawdown)",
          line("Koers AI (you)", s.get("kai")), line("Rick", s.get("you")),
          line("S&P 500 (SPY, dividends in)", s.get("spx")), line("Nasdaq 100 (QQQ, dividends in)", s.get("ndx"))]
    days = (s.get("kai") or {}).get("days") or 0
    if days < 60:
        L.append(f"({days} trading day{'s' if days != 1 else ''} so far: too few for the Sharpe ratio or the drawdown to mean much.)")
    L += last_review_block()
    prev = _rows("SELECT ts, kind, journal, trades FROM kai_decision ORDER BY id DESC LIMIT 5")
    L += ["", "YOUR LAST 5 JOURNAL ENTRIES"]
    if prev:
        for d in reversed(prev):
            tr = ", ".join(f"{t['action']} {t['ticker']} {t['amount'] if t['amount'] == 'ALL' else 'EUR ' + format(t['amount'], ',.0f')}"
                           for t in _j(d["trades"], []) or [])
            L.append(f"- {d['ts'][:10]} ({KIND_LABEL.get(d['kind'], d['kind'])}): {d['journal']} Trades: {tr or 'none'}.")
    else:
        L.append("None yet: this is your first turn.")
    L += markets_block()
    hl = headlines(held, n)
    L += ["", "RECENT HEADLINES (the last four days: on your holdings first, then the market's, at most eight a day)",
          *(hl or ["None in the last four days."])]
    L += ["", f"MARKET DATA: {len(rows)} companies in sector order. The {N_MARKET} highest-quality of the broad market the terminal "
              "reads in full each night (the 300 largest US and 200 largest European companies, every company above $5bn that is "
              "15% or more below its 52-week high, and the year's momentum leaders), more that it reads, and your holdings. "
              "Only these tickers may be traded this turn.", "Keys: " + LEGEND,
          *[json.dumps(r, ensure_ascii=False, separators=(", ", ": ")) for r in rows]]
    return "\n".join(L), [r["t"] for r in rows]


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------

JOB: dict = {"state": "idle"}
_LOCK = threading.Lock()


def _pub(j: Optional[dict]) -> Optional[dict]:
    return {k: j.get(k) for k in ("state", "phase", "kind", "started", "finished", "error", "decision", "review", "text")
            if k in j} if j else None


def decide(job: dict, kind: str = "weekly", trigger: str = "") -> int:
    """One turn: the material, Claude's answer, the rules, the order book."""
    from . import reader
    st = started()
    if not st:
        raise RuntimeError("Koers AI has not started")
    why = reader.writer_ready()
    if why:
        raise PermissionError(why)
    asked = now_ams()
    job["phase"] = "reading the portfolio and the market"
    text, sent = material(kind, trigger)
    job["phase"] = "deciding"
    a, out = reader.run_json(job, SYSTEM, text, "", f"kai-{asked:%Y-%m-%d-%H%M}-{kind}")
    if not isinstance(a, dict):
        raise ValueError("the reply was not one JSON object")
    # Stamped when the answer came, not when the question went: an open that rang
    # meanwhile is a price it may already have seen.
    done = now_ams()
    fx = FX(st["d"])
    book = value_on(done.date().isoformat(), fx, {})
    pend = sum(r["amount_eur"] or 0 for r in _rows("SELECT amount_eur FROM kai_order WHERE status='pending' AND side='BUY'"))
    ok, bad = check_trades(a.get("trades") or [], book, set(sent), pend)
    theses = [{"ticker": _s(t.get("ticker"), 24).upper(), "thesis": _s(t.get("thesis"), 500), "exit_if": _s(t.get("exit_if"), 300)}
              for t in (a.get("theses") or []) if isinstance(t, dict) and t.get("ticker")][:30]
    with store.conn() as c:
        did = c.execute("INSERT INTO kai_decision (ts, kind, trigger, date_given, journal, message, trades, rejected, theses, "
                        "universe, material, meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (done.isoformat(timespec="seconds"), kind, trigger, _s(a.get("date"), 10), str(a.get("journal") or "")[:3000],
                         _s(a.get("message_to_rival"), 200), json.dumps(ok), json.dumps(bad), json.dumps(theses), json.dumps(sent), text,
                         json.dumps({"model": out["model"], "usage": out["usage"], "api_equivalent_usd": out["api_usd"],
                                     "ms": out["ms"], "session": out["session"], "asked": asked.isoformat(timespec="seconds"),
                                     "date_ok": _s(a.get("date"), 10) == done.date().isoformat()}))).lastrowid
    _place(did, done, ok)
    fill_pending()
    return did


def _run(job: dict, kind: str, trigger: str):
    try:
        job["decision"] = decide(job, kind, trigger)
        job.update(state="done", phase="done")
    except Exception as e:
        job.update(state="error", error=f"{e.__class__.__name__}: {e}"[:300])
    finally:
        job["finished"] = time.time()


def start_decision(kind: str = "manual", trigger: str = "") -> dict:
    global JOB
    with _LOCK:
        if JOB.get("state") == "running":
            return _pub(JOB)
        JOB = {"state": "running", "phase": "starting", "kind": kind, "started": time.time(), "finished": None,
               "error": None, "text": ""}
        j = JOB
    threading.Thread(target=_run, args=(j, kind, trigger), daemon=True, name=f"kai-{kind}").start()
    return _pub(j)


# --------------------------------------------------------------------------
# the monthly self-review (build 56)
# --------------------------------------------------------------------------
#
# Once a month, on the first weekend from Saturday 10:00, Koers AI grades the
# theses it held against what happened and judges its own process. No trades:
# the next weekly decision reads the review (its summary, mistakes and rule
# changes). The period runs from the day after the last review, or the start,
# to the month's last day; a period under ten New York sessions waits for the
# next month (the contest began on 28 October: October's three days roll into
# November's review). One run on the plan a month.

REVIEW_AT = (10, 0)
REVIEW_MIN_DAYS = 10
GRADES = ("A", "B", "C", "D", "F")
THESIS_STATES = ("intact", "weakened", "broken", "too early to tell")

REVIEW_SYSTEM = """You are Koers AI, an autonomous portfolio manager in a paper-trading contest against Rick, a human trader managing their own real portfolio, and the S&P 500 and the Nasdaq 100. No real money is involved. You play a quality compounder: durable competitive advantages, high return on capital, strong balance sheets, growing free cash flow, at a reasonable price; 8-12 positions held for 1-3 years, sold when the thesis breaks, not because the price fell.

This turn is your monthly self-review. It is not a decision: you cannot trade this turn and no order will be placed. Your next weekly decision reads what you write here.

## What to do
1. Grade each position you held in the period against the thesis you gave for it: did the business do what the thesis said? Separate the business from the share price, and skill from luck: a share that rose while its thesis weakened is not a success, and one that fell with its thesis intact is not a failure.
2. Judge your process: did you follow your own exit rules and sizing, did you trade on data or on headlines, how much did you trade and what did it cost, was your cash level deliberate. If your last review set rule changes, say whether you kept them.
3. Name your mistakes plainly, and what you will keep doing.
4. Set the rule changes you will apply from now on, if any: to your exit rules, sizing, cash or process. Few and concrete. Changing nothing is a valid answer.

## Information discipline
- Use only the data in the message plus general knowledge of how businesses and markets work. Never state a price, figure or event that is not in the input.
- A month is short against a 1-3 year horizon: say so where it matters, and do not over-read it.

## Output
Respond with only this JSON:

{
  "grades": [
    {"ticker": "ASML.AS", "grade": "A", "thesis": "intact", "what_happened": "one or two sentences", "lesson": "one sentence"}
  ],
  "process": "2-4 sentences",
  "mistakes": ["one sentence each"],
  "keep_doing": ["one sentence each"],
  "rule_changes": ["one sentence each; an empty list if none"],
  "summary": "3-6 sentences: the period, what you learned, what changes"
}

grade is one of A, B, C, D, F for how the thesis held up, not for the share price; thesis is one of intact, weakened, broken, too early to tell."""


def first_saturday(y: int, m: int) -> date:
    d = date(y, m, 1)
    return d + timedelta(days=(5 - d.weekday()) % 7)


def ny_sessions(d0: date, d1: date) -> int:
    from . import sessions
    n, d = 0, d0
    while d <= d1:
        n += 1 if sessions.session("US", d) else 0
        d += timedelta(days=1)
    return n


def review_period(until: date) -> tuple[date, date]:
    """From the day after the last review, or the contest's first day, to `until`."""
    last = _rows("SELECT d_to FROM kai_review ORDER BY id DESC LIMIT 1")
    if last and last[0]["d_to"]:
        return date.fromisoformat(last[0]["d_to"]) + timedelta(days=1), until
    return date.fromisoformat(started()["d"]), until


def review_due(now: Optional[datetime] = None) -> Optional[tuple[date, date]]:
    """The period to review when the month's review is due: from the first Saturday of a month at 10:00, at weekends, once."""
    n = (now or now_ams()).astimezone(AMS)
    if not started() or n.weekday() < 5:
        return None
    if n < datetime.combine(first_saturday(n.year, n.month), dtime(*REVIEW_AT), AMS):
        return None
    end = date(n.year, n.month, 1) - timedelta(days=1)
    if _rows("SELECT 1 FROM kai_review WHERE kind='monthly' AND month=? LIMIT 1", (end.isoformat()[:7],)):
        return None
    d0, d1 = review_period(end)
    if d1 < d0 or ny_sessions(d0, d1) < REVIEW_MIN_DAYS:
        return None
    return d0, d1


def next_review(now: Optional[datetime] = None) -> Optional[str]:
    """When the next monthly review runs: the month before still owed one from this month's first Saturday 10:00, else next month's."""
    n = (now or now_ams()).astimezone(AMS)
    if not started():
        return None
    end = date(n.year, n.month, 1) - timedelta(days=1)
    this = datetime.combine(first_saturday(n.year, n.month), dtime(*REVIEW_AT), AMS)
    d0, d1 = review_period(end)
    owed = (not _rows("SELECT 1 FROM kai_review WHERE kind='monthly' AND month=? LIMIT 1", (end.isoformat()[:7],))
            and d1 >= d0 and ny_sessions(d0, d1) >= REVIEW_MIN_DAYS)
    if owed:
        if n < this:
            return this.isoformat(timespec="minutes")
        if n.weekday() >= 5:
            return n.isoformat(timespec="minutes")
        return datetime.combine(n.date() + timedelta(days=5 - n.weekday()), dtime(*REVIEW_AT), AMS).isoformat(timespec="minutes")
    y, m = (n.year + 1, 1) if n.month == 12 else (n.year, n.month + 1)
    return datetime.combine(first_saturday(y, m), dtime(*REVIEW_AT), AMS).isoformat(timespec="minutes")


def _theses_by_ticker() -> dict:
    """Every thesis it gave, oldest first, per ticker: [(decision date, thesis, exit rule)]."""
    out: dict = {}
    for d in _rows("SELECT ts, theses FROM kai_decision ORDER BY id"):
        for t in _j(d["theses"], []) or []:
            if t.get("ticker"):
                lst = out.setdefault(t["ticker"], [])
                if not lst or (lst[-1][1], lst[-1][2]) != (t.get("thesis"), t.get("exit_if")):
                    lst.append((d["ts"][:10], t.get("thesis"), t.get("exit_if")))
    return out


def review_material(d0: date, d1: date, kind: str = "monthly") -> str:
    """What the review reads: the period's scoreboard, each position against its thesis, its decisions and its process."""
    st = started()
    n = now_ams()
    fx, cache = FX(st["d"]), {}
    s0, s1 = d0.isoformat(), d1.isoformat()
    try:
        cv = curve()
    except Exception:
        cv = {}
    days = cv.get("d") or []

    def at(key, d, before=False):
        """A rebased line's value at the last curve day on (or before) a date."""
        ser = cv.get(key) or []
        idx = [i for i, x in enumerate(days) if (x < d if before else x <= d)]
        return ser[idx[-1]] if idx and idx[-1] < len(ser) and ser[idx[-1]] else None

    def ret(key, a, b):
        x, y = at(key, a, before=True) or at(key, a), at(key, b)
        return (y / x - 1) * 100 if x and y else None
    fmt = lambda v: f"{v:+.2f}%" if v is not None else "n/a"
    L = [f"DATE: {n.date().isoformat()} ({n:%A %d %B %Y}). THIS TURN: your {'monthly' if kind == 'monthly' else 'asked-for'} "
         f"self-review of {_dm(s0, True)} to {_dm(s1, True)} ({ny_sessions(d0, d1)} New York sessions). No trades this turn.", "",
         "THE PERIOD'S SCOREBOARD (in EUR, total return over the period)",
         f"- Koers AI (you): {fmt(ret('kai', s0, s1))}", f"- Rick: {fmt(ret('you', s0, s1))}",
         f"- S&P 500 (SPY, dividends in): {fmt(ret('spx', s0, s1))}", f"- Nasdaq 100 (QQQ, dividends in): {fmt(ret('ndx', s0, s1))}"]
    try:
        S = stats(cv) if cv.get("available") else {}
        k = S.get("kai") or {}
        L.append(f"Since the start ({st['d']}): you {k.get('ret', 0):+.2f}%, the S&P 500 {(S.get('spx') or {}).get('ret', 0):+.2f}%, "
                 f"your worst fall {k.get('max_dd', 0):.2f}%, Sharpe {k.get('sharpe') if k.get('sharpe') is not None else 'n/a'}.")
    except Exception:
        pass

    # each position held at some point in the period
    before = holdings((d0 - timedelta(days=1)).isoformat())["pos"]
    trs = [t for t in trades() if s0 <= t["d"] <= s1]
    tick = sorted(set(before) | {t["ticker"] for t in trs})
    end_book = value_on(s1, fx, cache)
    start_book = value_on((d0 - timedelta(days=1)).isoformat(), fx, cache)
    v_end = {r["ticker"]: r for r in end_book["rows"]}
    v_start = {r["ticker"]: r["value_eur"] for r in start_book["rows"]}
    th = _theses_by_ticker()
    names = {r["sym"]: r.get("name") for r in _scan_rows()}
    L += ["", f"EACH POSITION YOU HELD IN THE PERIOD (portfolio value at the start EUR {start_book['value']:,.2f}, at the end "
              f"EUR {end_book['value']:,.2f})"]
    for t in tick:
        tt = [x for x in trs if x["ticker"] == t]
        flow = sum(-(x["value_eur"] + x["cost_eur"]) if x["side"] == "BUY" else (x["value_eur"] - x["cost_eur"]) for x in tt)
        pl = (v_end.get(t) or {}).get("value_eur", 0.0) - v_start.get(t, 0.0) + flow
        first_buy = min((x["d"] for x in trades() if x["ticker"] == t and x["side"] == "BUY"), default=None)
        r = v_end.get(t)
        line = f"- {t}{' (' + _s(names.get(t), 40) + ')' if names.get(t) else ''}: P/L in the period EUR {pl:+,.2f}"
        if start_book["value"]:
            line += f" ({pl / start_book['value'] * 100:+.2f}% of the portfolio)"
        if r:
            line += (f"; held at the end, {r['value_eur'] / end_book['value'] * 100:.1f}% of the portfolio, "
                     f"{(r['value_eur'] / r['cost_eur'] - 1) * 100:+.1f}% since you bought it")
            if first_buy:
                sp = ret("spx", first_buy, s1)
                line += f" on {_dm(first_buy, True)} (the S&P 500 {fmt(sp)} over the same days)"
        else:
            line += "; sold by the end"
        if tt:
            line += ". Trades: " + ", ".join(f"{x['side']} {x['qty']:,.4f} on {_dm(x['d'])} at {x['px']:,.2f} {x['ccy'] or ''} "
                                             f"(EUR {x['value_eur']:,.0f})" for x in tt)
        L.append(line + ".")
        h = th.get(t) or []
        if h:
            L.append(f"  Your first thesis ({_dm(h[0][0], True)}): {h[0][1]} Exit if: {h[0][2]}")
            if len(h) > 1:
                L.append(f"  Your latest ({_dm(h[-1][0], True)}, after {len(h) - 1} change{'s' if len(h) > 2 else ''}): "
                         f"{h[-1][1]} Exit if: {h[-1][2]}")
        try:
            L.append("  " + result_line({**latest_result(t, n, cache), "n": None})[2:])
        except Exception:
            pass

    # its decisions in the period, word for word
    hi = (d1 + timedelta(days=1)).isoformat()
    decs = [d for d in _rows("SELECT * FROM kai_decision ORDER BY id") if s0 <= d["ts"][:10] < hi]
    L += ["", f"YOUR DECISIONS IN THE PERIOD ({len(decs)})"]
    for d in decs:
        tr = ", ".join(f"{t['action']} {t['ticker']} {t['amount'] if t['amount'] == 'ALL' else 'EUR ' + format(t['amount'], ',.0f')}"
                       for t in _j(d["trades"], []) or [])
        rj = "; ".join(f"{t.get('action')} {t.get('ticker')}: {t.get('why')}" for t in _j(d["rejected"], []) or [])
        L.append(f"- {d['ts'][:16].replace('T', ' ')} ({KIND_LABEL.get(d['kind'], d['kind'])}"
                 + (f", because {d['trigger']}" if d.get("trigger") else "") + f"): {d['journal']} Trades: {tr or 'none'}."
                 + (f" Refused: {rj}." if rj else ""))

    # its process in numbers
    orders = [o for o in _rows("SELECT * FROM kai_order ORDER BY id") if s0 <= o["ts"][:10] < hi]
    kinds: dict = {}
    for d in decs:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    ev = [d for d in decs if d["kind"] == "event"]
    ev_tr = sum(1 for d in ev if _j(d["trades"], []))
    bought = sum(t["value_eur"] for t in trs if t["side"] == "BUY")
    sold = sum(t["value_eur"] for t in trs if t["side"] == "SELL")
    avg_v = (start_book["value"] + end_book["value"]) / 2 or 1.0
    kname = {"start": ("first decision", "first decisions"), "weekly": ("weekly decision", "weekly decisions"),
             "event": ("event check", "event checks"), "manual": ("decision asked for", "decisions asked for")}
    L += ["", "YOUR PROCESS IN NUMBERS",
          "- Decisions: " + (", ".join(f"{v} {kname.get(k, (k, k))[v != 1]}" for k, v in sorted(kinds.items())) or "none")
          + f"; {ev_tr} of the {len(ev)} event check{'s' if len(ev) != 1 else ''} led to a trade.",
          f"- Bought EUR {bought:,.0f}, sold EUR {sold:,.0f}: turnover (the lesser of the two) {min(bought, sold) / avg_v * 100:.1f}% "
          f"of the average portfolio; trading costs EUR {sum(t['cost_eur'] for t in trs):,.2f}.",
          f"- Orders: {len(orders)}, of which {sum(1 for o in orders if o['status'] == 'filled')} filled, "
          f"{sum(1 for o in orders if o['status'] == 'rejected')} refused at the open; "
          f"{sum(len(_j(d['rejected'], []) or []) for d in decs)} refused at the decision.",
          f"- Cash at the end: EUR {end_book['cash']:,.2f} ({end_book['cash'] / (end_book['value'] or 1) * 100:.1f}%)."]
    L += last_review_block(title="YOUR PREVIOUS REVIEW (did you keep its rule changes?)")
    return "\n".join(L)


def clean_review(a: dict) -> dict:
    def lst(v, n=8, k=300):
        return [_s(x, k) for x in (v or []) if isinstance(x, str) and x.strip()][:n] if isinstance(v, list) else []
    grades = []
    for g in a.get("grades") or []:
        if not isinstance(g, dict) or not g.get("ticker"):
            continue
        gr = str(g.get("grade") or "").strip().upper()[:1]
        ts = str(g.get("thesis") or "").strip().lower()
        grades.append({"ticker": _s(g["ticker"], 24).upper(), "grade": gr if gr in GRADES else "?",
                       "thesis": ts if ts in THESIS_STATES else _s(ts, 40), "what_happened": _s(g.get("what_happened"), 500),
                       "lesson": _s(g.get("lesson"), 300)})
    return {"grades": grades[:25], "process": _s(a.get("process"), 1500), "mistakes": lst(a.get("mistakes")),
            "keep_doing": lst(a.get("keep_doing")), "rule_changes": lst(a.get("rule_changes")),
            "summary": str(a.get("summary") or "").strip()[:2500]}


def review(job: dict, kind: str = "monthly", d0: Optional[date] = None, d1: Optional[date] = None) -> int:
    """One review: the material, Claude's answer, kept. No orders."""
    from . import reader
    if not started():
        raise RuntimeError("Koers AI has not started")
    why = reader.writer_ready()
    if why:
        raise PermissionError(why)
    if d0 is None or d1 is None:
        d0, d1 = review_period(now_ams().date() - timedelta(days=1))
    if d1 < d0:
        raise ValueError("nothing to review since the last review")
    asked = now_ams()
    job["phase"] = "reading its month"
    text = review_material(d0, d1, kind)
    job["phase"] = "reviewing"
    a, out = reader.run_json(job, REVIEW_SYSTEM, text, "", f"kai-{asked:%Y-%m-%d-%H%M}-review")
    if not isinstance(a, dict):
        raise ValueError("the reply was not one JSON object")
    body = clean_review(a)
    done = now_ams()
    with store.conn() as c:
        return c.execute("INSERT INTO kai_review (month, kind, ts, d_from, d_to, summary, body, material, meta) "
                         "VALUES (?,?,?,?,?,?,?,?,?)",
                         (d1.isoformat()[:7], kind, done.isoformat(timespec="seconds"), d0.isoformat(), d1.isoformat(),
                          body["summary"], json.dumps(body), text,
                          json.dumps({"model": out["model"], "usage": out["usage"], "api_equivalent_usd": out["api_usd"],
                                      "ms": out["ms"], "session": out["session"],
                                      "asked": asked.isoformat(timespec="seconds")}))).lastrowid


def _run_review(job: dict, kind: str, d0: Optional[date], d1: Optional[date]):
    try:
        job["review"] = review(job, kind, d0, d1)
        job.update(state="done", phase="done")
    except Exception as e:
        job.update(state="error", error=f"{e.__class__.__name__}: {e}"[:300])
    finally:
        job["finished"] = time.time()


def start_review(kind: str = "manual", d0: Optional[date] = None, d1: Optional[date] = None) -> dict:
    global JOB
    with _LOCK:
        if JOB.get("state") == "running":
            return _pub(JOB)
        JOB = {"state": "running", "phase": "starting", "kind": "review", "started": time.time(), "finished": None,
               "error": None, "text": ""}
        j = JOB
    threading.Thread(target=_run_review, args=(j, kind, d0, d1), daemon=True, name="kai-review").start()
    return _pub(j)


def _review_row(r: dict, material: bool = False) -> dict:
    out = {k: r[k] for k in ("id", "month", "kind", "ts", "d_from", "d_to", "summary")}
    out["body"] = _j(r.get("body"), {}) or {}
    out["meta"] = _j(r.get("meta"), {}) or {}
    out["has_material"] = bool(r.get("material"))
    if material:
        out["material"] = r.get("material")
    return out


def reviews(limit: int = 6) -> list[dict]:
    return [_review_row(r) for r in _rows("SELECT * FROM kai_review ORDER BY id DESC LIMIT ?", (limit,))]


def review_one(rid: int) -> Optional[dict]:
    r = _rows("SELECT * FROM kai_review WHERE id=?", (rid,))
    return _review_row(r[0], material=True) if r else None


def last_review_block(title: str = "YOUR LAST MONTHLY REVIEW") -> list[str]:
    """The latest review, short: what the next decisions read of it."""
    r = _rows("SELECT * FROM kai_review ORDER BY id DESC LIMIT 1")
    if not r:
        return []
    r = _review_row(r[0])
    b = r["body"]
    L = ["", f"{title}: {_dm(r['d_from'], True)} to {_dm(r['d_to'], True)}, written {_dm(r['ts'], True)}",
         f"Summary: {r['summary']}"]
    if b.get("rule_changes"):
        L += ["Rule changes you set:", *[f"- {x}" for x in b["rule_changes"]]]
    if b.get("mistakes"):
        L += ["Mistakes you named:", *[f"- {x}" for x in b["mistakes"]]]
    weak = [g for g in b.get("grades") or [] if g.get("thesis") in ("weakened", "broken")]
    if weak:
        L += ["Theses you judged weakened or broken:", *[f"- {g['ticker']} ({g['grade']}, {g['thesis']}): {g['lesson']}" for g in weak]]
    return L


# --------------------------------------------------------------------------
# the schedule: Friday after the close, and checks on other evenings
# --------------------------------------------------------------------------

def _closed(n: datetime) -> bool:
    """The markets shut: a weekend, or a weekday from 23:00 to 08:00 Amsterdam."""
    return n.weekday() >= 5 or n.hour >= DECIDE_AT[0] or n.hour < 8


def _data_ready(n: datetime) -> bool:
    """The watchlist's scan has read the day since New York's last close, or three hours have passed without it."""
    from . import sessions, watch
    if watch.JOB.get("state") == "running":
        return False
    d = n.date()
    for _ in range(10):
        s = sessions.session("US", d)
        if s and s[1] <= n:
            scanned = (store.cache_get("wl:scan") or {}).get("scanned_at") or 0
            return scanned >= s[1].timestamp() or n - s[1] > timedelta(hours=3)
        d -= timedelta(days=1)
    return True


def weekly_slot(n: datetime) -> datetime:
    """The last Friday 23:00 at or before a moment."""
    fri = n.date() - timedelta(days=(n.weekday() - WEEKLY_DAY) % 7)
    slot = datetime.combine(fri, dtime(*DECIDE_AT), AMS)
    return slot if slot <= n else slot - timedelta(days=7)


def first_due(now: Optional[datetime] = None) -> bool:
    """Started, no decision yet, and the market data read since the last close: the first decision waits for fresh prices."""
    if not started() or _rows("SELECT 1 FROM kai_decision LIMIT 1"):
        return False
    return _data_ready((now or now_ams()).astimezone(AMS))


def _decided_since(kinds: tuple, since: datetime) -> bool:
    return any(r["kind"] in kinds and datetime.fromisoformat(r["ts"]) >= since
               for r in _rows("SELECT ts, kind FROM kai_decision ORDER BY id DESC LIMIT 30"))


def weekly_due(now: Optional[datetime] = None) -> bool:
    """Once a week from Friday 23:00; a missed Friday is made up while the markets are shut, never during a session."""
    n = (now or now_ams()).astimezone(AMS)
    st = started()
    slot = weekly_slot(n)
    if not st or datetime.fromisoformat(st["at"]) >= slot or _decided_since(("weekly", "start"), slot):
        return False
    return _closed(n) and _data_ready(n)


def _remember_reports():
    """Each holding's report dates as the scan showed them, kept: the scan moves a date on once the report is out."""
    held = holdings()["pos"]
    if not held:
        return
    seen = _j(store.get_setting("kai_reports"), {}) or {}
    scan = {r["sym"]: r for r in _scan_rows()}
    for sym in held:
        ts = (scan.get(sym) or {}).get("earn_ts")
        if ts:
            d = datetime.fromtimestamp(ts, AMS).date().isoformat()
            seen[sym] = sorted(set(seen.get(sym, [])) | {d})[-4:]
    store.set_setting("kai_reports", json.dumps({k: v for k, v in seen.items() if k in held}))


def events(today: Optional[date] = None) -> list[str]:
    """A holding that moved 5% or more on the day, or that reported that day."""
    today = today or now_ams().date()
    t = today.isoformat()
    held = holdings()["pos"]
    seen = _j(store.get_setting("kai_reports"), {}) or {}
    scan = {r["sym"]: r for r in _scan_rows()}
    out, cache = [], {}
    for sym in sorted(held):
        try:
            ds, b = _series(sym, cache)
        except Exception:
            ds, b = [], {}
        i = bisect.bisect_left(ds, t)
        if 1 <= i < len(ds) and ds[i] == t:
            chg = (b[t][3] / b[ds[i - 1]][3] - 1) * 100
            if abs(chg) >= EVENT_MOVE:
                out.append(f"{sym} moved {chg:+.1f}% today.")
        ts = (scan.get(sym) or {}).get("earn_ts")
        if t in seen.get(sym, []) or (ts and datetime.fromtimestamp(ts, AMS).date() == today):
            out.append(f"{sym} reported earnings today.")
    return out


LATE_UNTIL = 3                   # an evening's check may still run until 03:00 the next morning


def _evening(n: datetime) -> Optional[date]:
    """The evening a moment belongs to for the checks: from 23:00 until 03:00 the next morning."""
    if n.hour >= DECIDE_AT[0]:
        return n.date()
    if n.hour < LATE_UNTIL:
        return n.date() - timedelta(days=1)
    return None


def event_due(now: Optional[datetime] = None) -> Optional[str]:
    """
    Monday to Thursday evenings from 23:00, once an evening: what happened to
    the holdings that day, if anything did. The check waits for the nightly
    scan; when the scan runs past midnight it still belongs to the evening
    before (until 14 November it was simply lost after 23:59).
    """
    n = (now or now_ams()).astimezone(AMS)
    ev_d = _evening(n)
    if ev_d is None or ev_d.weekday() >= WEEKLY_DAY:
        return None
    day = ev_d.isoformat()
    if store.get_setting("kai_ev_day") == day:
        return None
    # any decision in this evening's day, from 03:00 to 03:00, already looked at it
    lo = datetime.combine(ev_d, dtime(LATE_UNTIL, 0), AMS)
    hi = lo + timedelta(days=1)
    if any(lo <= datetime.fromisoformat(r["ts"]) < hi
           for r in _rows("SELECT ts FROM kai_decision ORDER BY id DESC LIMIT 5")):
        return None
    if not _data_ready(n):
        return None
    ev = events(ev_d)
    store.set_setting("kai_ev_day", day)
    return " ".join(ev) if ev else None


_THREAD: dict = {"t": None}
_FAILS: dict = {"day": None, "n": 0}
MAX_FAILS = 3


def _may_run() -> bool:
    """Not after a failure in the last hour, nor after three in a day: a broken run must not keep spending the plan."""
    if JOB.get("state") == "running":
        return False
    if JOB.get("state") == "error":
        day = now_ams().date().isoformat()
        if _FAILS["day"] != day:
            _FAILS.update(day=day, n=0)
        if JOB.get("_counted") is not True:
            JOB["_counted"] = True
            _FAILS["n"] += 1
        if _FAILS["n"] >= MAX_FAILS or time.time() - (JOB.get("finished") or 0) < 3600:
            return False
    return True


def _tick():
    if not started():
        return
    _remember_reports()
    fill_pending()
    if store.get_setting("kai_auto") == "0" or not _may_run():
        return
    n = now_ams()
    if first_due(n):
        start_decision("start")
        return
    if weekly_due(n):
        d = _evening(n) or n.date()
        ev = events(d) if d.weekday() < 5 else []
        start_decision("weekly", " ".join(ev))
        return
    ev = event_due(n)
    if ev:
        start_decision("event", ev)
        return
    rv = review_due(n)
    if rv:
        start_review("monthly", *rv)


def _loop():
    time.sleep(120)
    while True:
        try:
            _tick()
        except Exception:
            pass
        time.sleep(300)


def ensure():
    """The scheduler, started by the server only: every decision spends the plan."""
    t = _THREAD["t"]
    if t and t.is_alive():
        return
    store.init_db()
    t = threading.Thread(target=_loop, daemon=True, name="kai-scheduler")
    _THREAD["t"] = t
    t.start()


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

def _decoded(d: dict) -> dict:
    """A decision for the page: what it was sent (some 60,000 characters) only says it is there; decision() gives it."""
    for k in ("trades", "rejected", "theses", "universe", "meta"):
        d[k] = _j(d.get(k), [] if k != "meta" else {})
    d["n_universe"] = len(d.pop("universe") or [])
    d["has_material"] = bool(d.pop("material", None))
    d["fills"] = _rows("SELECT ticker, side, status, fill_d, note FROM kai_order WHERE decision_id=? ORDER BY id", (d["id"],))
    return d


def overview() -> dict:
    st = started()
    if not st:
        return {"started": False, "rules": rules(), "job": _pub(JOB)}
    fx = FX(st["d"])
    book = value_on(now_ams().date().isoformat(), fx, {})
    th = current_theses()
    names = {r["sym"]: r.get("name") for r in _scan_rows()}
    rows = [{"ticker": p["ticker"], "name": names.get(p["ticker"]), "qty": p["qty"], "avg_eur": p["cost_eur"] / p["qty"],
             "px_eur": p["value_eur"] / p["qty"], "value_eur": round(p["value_eur"], 2),
             "w": round(p["value_eur"] / book["value"] * 100, 2), "pl_eur": round(p["value_eur"] - p["cost_eur"], 2),
             "pl_pct": round((p["value_eur"] / p["cost_eur"] - 1) * 100, 2) if p["cost_eur"] else None, "opened": p["opened"],
             "thesis": (th.get(p["ticker"]) or {}).get("thesis"), "exit_if": (th.get(p["ticker"]) or {}).get("exit_if")}
            for p in sorted(book["rows"], key=lambda r: -r["value_eur"])]
    cv = curve()
    dec = [_decoded(d) for d in _rows("SELECT * FROM kai_decision ORDER BY id DESC LIMIT 12")]
    return {"started": True, "start": st, "value": round(book["value"], 2), "cash": round(book["cash"], 2),
            "positions": rows, "curve": cv, "stats": stats(cv) if cv.get("available") else None,
            "pending": _rows("SELECT * FROM kai_order WHERE status='pending' ORDER BY id"),
            "decisions": dec, "trades": list(reversed(trades()))[:80], "rules": rules(),
            "message": next((d["message"] for d in dec if d.get("message")), None),
            "first_waiting": not dec and JOB.get("state") != "running",
            "auto": store.get_setting("kai_auto") != "0", "job": _pub(JOB),
            "reviews": reviews(), "next_review": next_review()}


def decision(did: int) -> Optional[dict]:
    """One decision in full: what it was sent, word for word, what it answered and what became of its orders."""
    d = _rows("SELECT * FROM kai_decision WHERE id=?", (did,))
    if not d:
        return None
    d = d[0]
    sent, material = _j(d.get("universe"), []), d.get("material")
    d = _decoded(d)
    d["universe"], d["material"] = sent, material
    return d


def rules() -> dict:
    return {"capital": CAPITAL, "cost_pct": round(COST * 100, 3), "max_pos_pct": round(MAX_POS_W * 100), "n_market": N_MARKET,
            "event_move": EVENT_MOVE, "decide_at": f"{DECIDE_AT[0]:02d}:{DECIDE_AT[1]:02d}", "profile": "quality compounder",
            "review_at": f"{REVIEW_AT[0]:02d}:{REVIEW_AT[1]:02d}", "review_min_days": REVIEW_MIN_DAYS}


def brief_line() -> Optional[str]:
    st = started()
    if not st:
        return None
    try:
        cv = curve()
        if not cv.get("available"):
            return None
        s = stats(cv)
        if not s.get("kai"):
            return None
        f = lambda x: f"{x['ret']:+.1f}%" if x else "n/a"
        return f"Koers AI {f(s.get('kai'))} since {st['d']}, against the S&P 500's {f(s.get('spx'))} and your {f(s.get('you'))}"
    except Exception:
        return None
