"""
ALRT — the morning brief.

One page, read once before the open: what happened in the last session and
what is scheduled for the day ahead, for THIS book. Not a live feed. It is
built at 06:30 on weekdays by a background thread (and whenever you press
Refresh), stored, and kept — last Tuesday's brief can still be read on Friday.

    yesterday   the book's move in euros and what drove it; the movers with
                the catalyst or headline behind each move; unusual volume and
                52-week extremes; the earnings that came out (yours, and the
                companies big enough to move the market); big or unexpected
                headlines; the markets, close to close
    today       where futures and Asia stand; the day's timeline in Amsterdam
                time — macro releases, central banks, earnings, Treasury
                auctions, market holidays, options expiry, elections, and
                events the news says are scheduled; then what touches your
                positions: reports, moves outside hours, ex-dividend dates,
                thesis reviews due, alert rules close to firing
    week ahead  the rest of the week's scheduled risk

"Yesterday" is the last weekday before the brief's date. A Monday brief covers
Friday, and its news window runs through the weekend. After 22:30 the brief
you are shown is the next weekday's: by then New York has closed and the day
is done.

Everything here is rules and arithmetic, the summary included. Claude writes a
narrative version only when asked — through Claude Code on your plan, or the
API key — exactly as the earnings reports are written.
"""

from __future__ import annotations

import json
import math
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait as fwait
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import store

TZ = ZoneInfo("Europe/Amsterdam")
NY = ZoneInfo("America/New_York")
CUTOFF = (22, 30)          # after this, the default brief is tomorrow's
MORNING = (6, 30)          # the scheduled build
KEEP_DAYS = 400

# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def _f(x) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def _r(x, d: int = 2) -> Optional[float]:
    return None if x is None else round(x, d)


def _pct(a, b) -> Optional[float]:
    return None if a is None or not b else (a / b - 1) * 100


def _dd(s: str) -> date:
    return date.fromisoformat(s[:10])


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def now_local() -> datetime:
    return datetime.now(TZ)


def next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def prev_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def brief_day(now: Optional[datetime] = None) -> date:
    """The morning the brief you should be reading is for."""
    n = now or now_local()
    d = n.date()
    if d.weekday() >= 5 or (n.hour, n.minute) >= CUTOFF:
        return next_weekday(d)
    return d


def _at(d: date, hh: int, mm: int, tz=TZ) -> datetime:
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz)


def _hm(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%H:%M")


WEEKDAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _eur(v: Optional[float]) -> str:
    if v is None:
        return "–"
    s = f"{abs(round(v)):,.0f}".replace(",", ".")
    return ("−€" if round(v) < 0 else "€") + s


def _pc(v: Optional[float], d: int = 1) -> str:
    if v is None:
        return "–"
    return ("+" if v >= 0 else "−") + f"{abs(v):.{d}f}%"


# --------------------------------------------------------------------------
# the calendar the market keeps: holidays, expiries, quarter-ends
# --------------------------------------------------------------------------

# Exchange holidays, hand-kept like the FOMC dates: published a year ahead,
# refreshed each December. The page shows how old the list is.
HOLIDAYS_AS_OF = "2026-09"
HOLIDAYS = {
    # New York (NYSE, Nasdaq)
    "US": {"2026-01-01": "New Year's Day", "2026-01-19": "Martin Luther King Jr. Day",
           "2026-02-16": "Presidents' Day", "2026-04-03": "Good Friday", "2026-05-25": "Memorial Day",
           "2026-06-19": "Juneteenth", "2026-07-03": "Independence Day (observed)",
           "2026-09-07": "Labor Day", "2026-11-26": "Thanksgiving", "2026-12-25": "Christmas",
           "2027-01-01": "New Year's Day", "2027-01-18": "Martin Luther King Jr. Day",
           "2027-02-15": "Presidents' Day", "2027-03-26": "Good Friday", "2027-05-31": "Memorial Day",
           "2027-06-18": "Juneteenth (observed)", "2027-07-05": "Independence Day (observed)",
           "2027-09-06": "Labor Day", "2027-11-25": "Thanksgiving", "2027-12-24": "Christmas (observed)"},
    # Amsterdam (Euronext)
    "NL": {"2026-01-01": "New Year's Day", "2026-04-03": "Good Friday", "2026-04-06": "Easter Monday",
           "2026-05-01": "Labour Day", "2026-12-25": "Christmas",
           "2027-01-01": "New Year's Day", "2027-03-26": "Good Friday", "2027-03-29": "Easter Monday"},
    # Frankfurt (Xetra)
    "DE": {"2026-01-01": "New Year's Day", "2026-04-03": "Good Friday", "2026-04-06": "Easter Monday",
           "2026-05-01": "Labour Day", "2026-12-24": "Christmas Eve", "2026-12-25": "Christmas",
           "2026-12-31": "New Year's Eve",
           "2027-01-01": "New Year's Day", "2027-03-26": "Good Friday", "2027-03-29": "Easter Monday",
           "2027-12-24": "Christmas Eve", "2027-12-31": "New Year's Eve"},
}
EARLY_CLOSE = {
    "US": {"2026-11-27": "13:00 New York, the day after Thanksgiving", "2026-12-24": "13:00 New York, Christmas Eve",
           "2027-11-26": "13:00 New York, the day after Thanksgiving"},
    "NL": {"2026-12-24": "14:05 Amsterdam, Christmas Eve", "2026-12-31": "14:05 Amsterdam, New Year's Eve",
           "2027-12-24": "14:05 Amsterdam, Christmas Eve", "2027-12-31": "14:05 Amsterdam, New Year's Eve"},
}
# The US bond market (SIFMA) closes on two days the stock market trades.
BONDS_SHUT = {"2026-10-12": "Columbus Day", "2026-11-11": "Veterans Day",
              "2027-10-11": "Columbus Day", "2027-11-11": "Veterans Day"}
MARKET_NAME = {"US": "New York", "NL": "Amsterdam", "DE": "Frankfurt"}


def third_friday(y: int, m: int) -> date:
    d = date(y, m, 15)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d


def last_business_day(y: int, m: int) -> date:
    d = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
    while d.weekday() >= 5 or d.isoformat() in HOLIDAYS["US"]:
        d -= timedelta(days=1)
    return d


def market_events(d: date) -> list[dict]:
    """What the exchanges' own calendar holds on one day."""
    iso, out = d.isoformat(), []
    for mk, days in HOLIDAYS.items():
        if iso in days:
            out.append({"kind": "market", "title": f"{MARKET_NAME[mk]} closed — {days[iso]}",
                        "impact": 3 if mk in ("US", "NL") else 2, "time": None, "mk": mk})
    for mk, days in EARLY_CLOSE.items():
        if iso in days:
            out.append({"kind": "market", "title": f"{MARKET_NAME[mk]} closes early — {days[iso]}",
                        "impact": 2, "time": None, "mk": mk})
    if iso in BONDS_SHUT:
        out.append({"kind": "market", "title": f"US bond market closed ({BONDS_SHUT[iso]}); stocks trade",
                    "impact": 1, "time": None})
    tf = third_friday(d.year, d.month)
    if d == tf:
        quad = d.month in (3, 6, 9, 12)
        out.append({"kind": "market", "impact": 2 if quad else 1, "time": "22:00",
                    "title": ("Quarterly options and futures expiry (\"triple witching\")" if quad
                              else "Monthly options expiry"),
                    "detail": "heavy volume into the close; pinned prices around big strikes"})
    nm = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    if d == third_friday(nm.year, nm.month) - timedelta(days=30):
        out.append({"kind": "market", "impact": 1, "time": None, "title": "VIX options expire",
                    "detail": "the volatility index can move for no reason beyond the expiry"})
    lb = last_business_day(d.year, d.month)
    if d == lb:
        q = d.month in (3, 6, 9, 12)
        out.append({"kind": "market", "impact": 2 if q else 1, "time": None,
                    "title": "Quarter-end" if q else "Month-end",
                    "detail": ("pension and fund rebalancing flows, window dressing; the "
                               + ("US federal fiscal year ends too" if d.month == 9 else "last day of the quarter's books")
                               if q else "rebalancing flows into the close")})
    return out


# --------------------------------------------------------------------------
# prices: one batched download for everything
# --------------------------------------------------------------------------

def bars(syms: list[str], period: str = "1y") -> dict[str, list[tuple]]:
    """{sym: [(date, open, high, low, close, adj close, volume)]}, oldest first."""
    import yfinance as yf
    syms = sorted({s for s in syms if s})
    out: dict[str, list[tuple]] = {}
    for i in range(0, len(syms), 60):
        chunk = syms[i:i + 60]
        try:
            df = yf.download(chunk, period=period, interval="1d", auto_adjust=False, progress=False,
                             group_by="ticker", threads=True, timeout=25)
        except Exception:
            continue
        lvl0 = set(df.columns.get_level_values(0)) if hasattr(df.columns, "levels") else set()
        for s in chunk:
            try:
                sub = df[s] if s in lvl0 else df
                sub = sub.dropna(subset=["Close"])
                rows = []
                for idx, r in sub.iterrows():
                    c = _f(r["Close"])
                    if c is None:
                        continue
                    rows.append((idx.date().isoformat(), _f(r.get("Open")), _f(r.get("High")), _f(r.get("Low")),
                                 c, _f(r.get("Adj Close")) or c, _f(r.get("Volume"))))
                if rows:
                    out[s] = rows
            except Exception:
                continue
    return out


def _session(rows: list[tuple], bd: str) -> tuple[Optional[tuple], Optional[tuple], list[tuple]]:
    """The last completed bar before the brief's day, the one before it, and what traded since."""
    past = [r for r in rows if r[0] < bd]
    after = [r for r in rows if r[0] >= bd]
    return (past[-1] if past else None), (past[-2] if len(past) > 1 else None), after


def _rate_on(rows: Optional[list[tuple]], d: str) -> Optional[float]:
    if not rows:
        return None
    v = None
    for r in rows:
        if r[0] <= d:
            v = r[4]
        else:
            break
    return v if v is not None else rows[0][4]


def _market_of(sym: str) -> Optional[str]:
    """Which holiday list a symbol trades under, where the brief keeps one."""
    if re.search(r"\.AS$", sym) or sym in ("^AEX", "^AMX"):
        return "NL"
    if re.search(r"\.(DE|F)$", sym) or sym in ("^GDAXI", "^STOXX50E"):
        return "DE"
    if re.search(r"\.[A-Z]{1,3}$", sym) or sym.startswith("^") or "=" in sym:
        return None
    return "US"


# When each market's session is over, closing auction included.
CLOSES = {"NL": ("Europe/Amsterdam", 17, 40), "DE": ("Europe/Berlin", 17, 40), "US": ("America/New_York", 16, 5)}


def stored_close(sym: str, d: str) -> tuple[Optional[float], Optional[str]]:
    """The close this app stored for a day, and when it stored it (UTC)."""
    with store.conn() as c:
        r = c.execute("SELECT close, fetched_at FROM price WHERE ticker=? AND d=?", (sym, d)).fetchone()
    return (r["close"], r["fetched_at"]) if r else (None, None)


def _after_close(sym: str, S: date, fetched_utc: Optional[str]) -> bool:
    """Was a price stored after that session had closed — so it is the close?"""
    if not fetched_utc:
        return False
    try:
        t = datetime.fromisoformat(fetched_utc[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    tz, hh, mm = CLOSES.get(_market_of(sym) or "", (None, 0, 0))
    if tz is None:
        return t.date() > S               # elsewhere: stored on a later day
    return t >= datetime(S.year, S.month, S.day, hh, mm, tzinfo=ZoneInfo(tz))


def fill_gaps(px: dict, syms: list[str], S: date, bd: date) -> dict[str, str]:
    """
    Put back a session Yahoo's daily history has dropped.

    Once the next session opens, Yahoo's daily bars for European listings can
    lose the previous day: at 10:30 on a Tuesday, ING's history ran Friday,
    Tuesday — no Monday — and a brief built then read eight holdings as shut
    on Monday. A missing session is filled, when its exchange was open that
    day, from the best there is:

      1. the close this app stored after that session had closed — Yahoo's own
         daily close, from before it dropped the bar;
      2. Yahoo's hourly bars: the last hour's close, which misses the closing
         auction (ING: 31.96 against an official 31.88);
      3. a price stored during that session, as a last resort.

    Returns {symbol: where its close came from}.
    """
    Ss, bds = S.isoformat(), bd.isoformat()
    need = []
    for s in syms:
        rows = px.get(s)
        if not rows:
            continue
        b1, _b0, _after = _session(rows, bds)
        mk = _market_of(s)
        if not b1 or b1[0] >= Ss or (mk and Ss in HOLIDAYS.get(mk, {})):
            continue
        need.append(s)
    out: dict[str, str] = {}

    def put(s, bar, how):
        px[s] = sorted([r for r in px[s] if r[0] != Ss] + [bar])
        out[s] = how

    later = {}
    for s in need:
        c, at = stored_close(s, Ss)
        if c and _after_close(s, S, at):
            put(s, (Ss, None, None, None, c, c, None), "the close stored after the session")
        elif c:
            later[s] = c

    def hourly(s):
        import yfinance as yf
        h = yf.Ticker(s).history(period="5d", interval="60m", auto_adjust=False)
        h = h[[d == S for d in h.index.date]].dropna(subset=["Close"])
        if not len(h):
            return None
        return (Ss, _f(h["Open"].iloc[0]), _f(h["High"].max()), _f(h["Low"].min()), _f(h["Close"].iloc[-1]),
                _f(h["Close"].iloc[-1]), _f(h["Volume"].sum()) or None)
    rest = [s for s in need if s not in out]
    if rest:
        ex = ThreadPoolExecutor(6)
        futs = {ex.submit(hourly, s): s for s in rest}
        done, _ = fwait(futs, timeout=40)
        for f in done:
            try:
                bar = f.result()
            except Exception:
                bar = None
            if bar and bar[4]:
                put(futs[f], bar, "Yahoo's hourly bars (the last hour's close)")
        ex.shutdown(wait=False, cancel_futures=True)
    for s, c in later.items():
        if s not in out:
            put(s, (Ss, None, None, None, c, c, None), "a price stored during the session")
    return out


# --------------------------------------------------------------------------
# yesterday: the book
# --------------------------------------------------------------------------

ALIAS = {
    "GOOGL": ["Alphabet", "Google"], "GOOG": ["Alphabet", "Google"], "META": ["Meta"],
    "TSM": ["TSMC", "Taiwan Semiconductor"], "INGA.AS": ["ING"], "UNA.AS": ["Unilever"],
    "SHELL.AS": ["Shell"], "BMW.DE": ["BMW"], "NN.AS": ["NN Group"], "WHA.AS": ["Wereldhave"],
    "SMCI": ["Super Micro", "Supermicro"], "LMT": ["Lockheed"], "UNH": ["UnitedHealth"],
    "ISRG": ["Intuitive Surgical", "Intuitive"], "MCD": ["McDonald's", "McDonald"], "NOW": ["ServiceNow"],
    "APP": ["AppLovin"], "CRWV": ["CoreWeave"], "AAOI": ["Applied Optoelectronics", "Applied Opto"],
    "HUBS": ["HubSpot"], "MP": ["MP Materials"], "RL": ["Ralph Lauren"], "TX": ["Ternium"],
    "COP": ["ConocoPhillips"], "WDC": ["Western Digital"], "VST": ["Vistra"], "MA": ["Mastercard"],
    "BE": ["Bloom Energy"], "CRDO": ["Credo"], "ARM": ["Arm Holdings", "Arm"], "AMD": ["AMD"],
    "NVDA": ["Nvidia"], "IBM": ["IBM"], "AMZN": ["Amazon"], "WMT": ["Walmart"], "VZ": ["Verizon"],
    "ASML": ["ASML"], "ASML.AS": ["ASML"], "ADYEN.AS": ["Adyen"], "PRX.AS": ["Prosus"],
}
_SUFFIX = re.compile(r"\b(corporation|corp|incorporated|inc|holdings?|group|plc|n\.?v\.?|s\.?a\.?|ag|se|"
                     r"ltd|limited|co|company|class [a-c]|aktiengesellschaft|the|groep|platforms|"
                     r"technologies|technology|communications|ucits|etf|etc)\b\.?", re.I)
_GENERIC = {"international", "american", "general", "united", "advanced", "applied", "national", "western",
            "first", "global", "super", "micro", "royal", "energy", "digital", "invesco", "bank"}
_COMMON_TICKERS = {"NOW", "APP", "ARM", "ALL", "ONE", "CAT", "KEY", "RUN", "MA", "BE", "IT", "ON", "TX",
                   "RL", "MP", "MU", "A", "T", "F", "C", "K", "O", "V", "D", "GE", "AI", "AN", "ARE"}


def brands(sym: str, name: str) -> list[str]:
    """The names a headline would use for a company."""
    base = sym.split(".")[0].upper()
    out = list(ALIAS.get(sym, ALIAS.get(base, [])))
    n = re.sub(r"^adr on\s+", "", name or "", flags=re.I)
    n = re.sub(r"\.com\b", "", n, flags=re.I)
    n = _SUFFIX.sub(" ", n)
    n = re.sub(r"[,.]", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    if n and not re.search(r"\d", n):
        t = " ".join(w if len(w) <= 3 and w.isupper() else w.capitalize() for w in n.split())
        if t and t not in out and len(t) >= 3:
            out.append(t)
        first = t.split(" ")[0] if t else ""
        if len(first) >= 5 and first.lower() not in _GENERIC and first not in out:
            out.append(first)
    return out


def _brand_rx(sym: str, name: str) -> Optional[re.Pattern]:
    parts = []
    for b in brands(sym, name):
        if len(b) >= 6:
            parts.append(r"(?i:\b" + re.escape(b) + r")\b")
        else:
            parts.append(r"\b(?:" + re.escape(b) + "|" + re.escape(b.upper()) + r")\b")
    base = sym.split(".")[0].upper()
    if len(base) >= 3 and base not in _COMMON_TICKERS:
        parts.append(r"\b" + re.escape(base) + r"\b")
    parts.append(r"[($:]" + re.escape(base) + r"\b")
    return re.compile("|".join(parts)) if parts else None


def _book(bd: date, S: date, dash: dict, px: dict) -> dict:
    bds, Ss = bd.isoformat(), S.isoformat()
    pos = [p for p in dash.get("positions", []) if not p.get("excluded") and (p.get("value_eur") or 0) > 0]
    rows, unmeasured = [], []
    for p in pos:
        tk = p.get("ticker")
        b = px.get(tk) if tk else None
        if not b:
            unmeasured.append({"name": p.get("name"), "ticker": tk, "kind": p.get("kind"),
                               "value_eur": p.get("value_eur")})
            continue
        b1, b0, _after = _session(b, bds)
        if not b1 or not b0:
            unmeasured.append({"name": p.get("name"), "ticker": tk, "kind": p.get("kind"),
                               "value_eur": p.get("value_eur")})
            continue
        ccy = (p.get("currency") or "EUR").upper()
        scale = 0.01 if ccy in ("GBX", "GBP_PENCE") else 1.0
        pair = {"GBX": "EURGBP=X"}.get(ccy, f"EUR{ccy}=X")
        fx1 = 1.0 if ccy == "EUR" else _rate_on(px.get(pair), b1[0])
        fx0 = 1.0 if ccy == "EUR" else _rate_on(px.get(pair), b0[0])
        if not fx1 or not fx0:
            fx1 = fx0 = p.get("fx") or 1.0
        qty = p.get("qty") or 0
        c1, c0 = b1[4] * scale, b0[4] * scale
        traded = b1[0] == Ss
        past = [r for r in b if r[0] < bds]
        rets = [past[i][5] / past[i - 1][5] - 1 for i in range(max(1, len(past) - 61), len(past) - 1)
                if past[i - 1][5]]
        sd = statistics.pstdev(rets) if len(rets) > 20 else None
        r = (b1[5] / b0[5] - 1) if traded and b0[5] else 0.0
        vols = [x[6] for x in past[-52:-1] if x[6]]
        rvol = (b1[6] / (sum(vols) / len(vols))) if traded and vols and b1[6] else None
        prior = [x[4] for x in past[-253:-1]]
        e_price = qty * (c1 - c0) / fx1 if traded else 0.0
        e_fx = qty * c0 * (1 / fx1 - 1 / fx0) if traded else 0.0
        v0 = qty * c0 / fx0
        rows.append({
            "ticker": tk, "name": p.get("name"), "kind": p.get("kind"), "sector": p.get("sector") or
            ("ETF" if p.get("kind") == "ETF" else None), "currency": ccy, "weight": p.get("weight"),
            "value_eur": p.get("value_eur"), "traded": traded, "day": b1[0],
            "close": _r(b1[4], 4), "prev_close": _r(b0[4], 4),
            "move": _r(r * 100, 2), "eur": _r(e_price + e_fx, 2), "eur_price": _r(e_price, 2),
            "eur_fx": _r(e_fx, 2), "value0": v0,
            "z": _r(r / sd, 2) if sd else None, "rvol": _r(rvol, 2),
            "gap": _r(_pct(b1[1], b0[4]), 2) if traded and b1[1] else None,
            "d5": _r(_pct(past[-1][5], past[-6][5]), 2) if len(past) > 6 else None,
            "high52": bool(traded and prior and b1[4] > max(prior)),
            "low52": bool(traded and prior and b1[4] < min(prior)),
            "from_high": _r(_pct(b1[4], max(prior + [b1[4]])), 1) if prior else None,
        })
    tot = sum(x["eur"] or 0 for x in rows)
    base = sum(x["value0"] for x in rows if x["traded"]) or sum(x["value0"] for x in rows)
    for x in rows:
        x.pop("value0", None)
    rows.sort(key=lambda x: -abs(x["eur"] or 0))
    by_sector: dict[str, float] = {}
    for x in rows:
        by_sector[x["sector"] or "Other"] = by_sector.get(x["sector"] or "Other", 0.0) + (x["eur"] or 0)
    return {
        "eur": _r(tot, 2), "pct": _r(tot / base * 100, 2) if base else None,
        "eur_price": _r(sum(x["eur_price"] or 0 for x in rows), 2),
        "eur_fx": _r(sum(x["eur_fx"] or 0 for x in rows), 2),
        "value_eur": _r(sum(p.get("value_eur") or 0 for p in pos), 0),
        "n": len(rows), "n_traded": sum(1 for x in rows if x["traded"]),
        "up": sum(1 for x in rows if (x["move"] or 0) > 0), "down": sum(1 for x in rows if (x["move"] or 0) < 0),
        "positions": rows, "unmeasured": unmeasured,
        "sectors": sorted(({"sector": k, "eur": _r(v, 0)} for k, v in by_sector.items()), key=lambda s: s["eur"]),
    }


def _explain(rows: list[dict], S: str, budget: float = 70.0) -> dict:
    """
    Why the movers moved: the security page's own model — market, sector and
    company parts of the day, and the earnings, analyst, filing or headline
    that lines up with it. Only for the moves worth explaining.
    """
    from . import research
    pick = []
    for x in rows[:5]:
        pick.append(x)
    for x in rows:
        if x in pick or not x["traded"]:
            continue
        if abs(x["z"] or 0) >= 2 or abs(x["move"] or 0) >= 4:
            pick.append(x)
    pick = [x for x in pick if x["kind"] != "ETF" and x["traded"] and abs(x["move"] or 0) >= 0.8][:10]
    out = {}

    def one(sym):
        mv = research.moves(sym)
        rec = next((d for d in mv.get("days") or [] if d["d"] == S), None)
        if not rec:
            return None
        return {"driver": rec.get("driver"), "why": rec.get("why"), "headline": rec.get("headline"),
                "catalysts": [{"type": c.get("type"), "text": c.get("text"), "url": c.get("url"), "w": c.get("w")}
                              for c in (rec.get("catalysts") or [])[:3]],
                "mkt": rec.get("mkt"), "sec": rec.get("sec"), "idio": rec.get("idio"), "zm": rec.get("z"),
                "benchmarks": mv.get("benchmarks")}
    ex = ThreadPoolExecutor(4)
    futs = {ex.submit(one, x["ticker"]): x["ticker"] for x in pick}
    done, _ = fwait(futs, timeout=budget)
    for f in done:
        try:
            v = f.result()
        except Exception:
            v = None
        if v:
            out[futs[f]] = v
    ex.shutdown(wait=False, cancel_futures=True)
    return out


DOWN = re.compile(r"\b(falls?|fell|drops?|dropped|slides?|slid|sinks?|sank|tumbles?|tumbled|plunges?|plunged|"
                  r"slumps?|slumped|sheds?|dives?|dived|lower|declines?|declined|slips?|slipped|retreats?|"
                  r"sell-?off|losses)\b", re.I)
UP = re.compile(r"\b(rises?|rose|jumps?|jumped|gains?|gained|soars?|soared|surges?|surged|rall(y|ies|ied)|"
                r"climbs?|climbed|higher|pops?|popped|spikes?|spiked|advances?|record high)\b", re.I)


def _reason(x: dict, w: Optional[dict], items: list[dict]) -> Optional[dict]:
    """
    One line on why a position moved, from the best evidence there is: a
    catalyst (earnings, an up- or downgrade, a filing) first, then a headline
    that names the company and moves the same way, then the market or the
    sector if that is what it was. Clickbait never counts as a reason.
    """
    mv = x.get("move") or 0
    same = DOWN if mv < 0 else UP
    w = w or {}
    for c in w.get("catalysts") or []:
        if (c.get("w") or 0) >= 2:
            return {"kind": "catalyst", "type": c.get("type"), "text": c.get("text"), "url": c.get("url")}
    # A headline counts only if it names the company itself: a market wrap
    # that mentions Nvidia in its third paragraph explains nothing about it.
    rx = _brand_rx(x["ticker"], x.get("name") or "")
    heads = [i for i in items if not JUNK.search(i["headline"]) and rx and rx.search(i["headline"])]
    rh = w.get("headline") or {}
    if rh.get("text") and not JUNK.search(rh["text"]):
        heads.append({"headline": rh["text"], "url": rh.get("url"), "source": rh.get("source")})
    moving = next((h for h in heads if same.search(h["headline"])), None)
    drv = w.get("driver")
    if drv in ("market", "sector") and w.get("why"):
        out = {"kind": drv, "text": w["why"]}
        if moving:
            out.update(headline=moving["headline"], url=moving.get("url"), source=moving.get("source"))
        return out
    best = moving or (heads[0] if heads and drv in ("company", "mixed") else None)
    if best:
        return {"kind": "headline", "text": best["headline"], "url": best.get("url"), "source": best.get("source")}
    if drv == "company" and abs(w.get("zm") or 0) >= 2:
        return {"kind": "none", "text": "No cause found in earnings, analyst actions, filings or the news."}
    if drv in ("company", "mixed") and w.get("why"):
        # The model's own sentence, without the headline it tacked on: that
        # one failed the tests above.
        return {"kind": "model", "text": re.split(r"\s(?:In the news|Likely cause|Also that day):", w["why"])[0]}
    if drv == "quiet":
        return {"kind": "quiet", "text": "Inside its normal daily range."}
    return None


# --------------------------------------------------------------------------
# yesterday: the markets
# --------------------------------------------------------------------------

MARKETS = [
    ("^GSPC", "S&P 500", "idx", "US"), ("^NDX", "Nasdaq 100", "idx", "US"), ("^DJI", "Dow Jones", "idx", "US"),
    ("^RUT", "Russell 2000", "idx", "US"),
    ("^AEX", "AEX", "idx", "EU"), ("^STOXX50E", "Euro Stoxx 50", "idx", "EU"), ("^GDAXI", "DAX", "idx", "EU"),
    ("^N225", "Nikkei 225", "idx", "Asia"), ("^HSI", "Hang Seng", "idx", "Asia"),
    ("^FVX", "US 5-year", "yield", "Rates"), ("^TNX", "US 10-year", "yield", "Rates"),
    ("^TYX", "US 30-year", "yield", "Rates"),
    ("EURUSD=X", "EUR/USD", "fx", "FX"), ("EURGBP=X", "EUR/GBP", "fx", "FX"), ("DX-Y.NYB", "Dollar index", "idx", "FX"),
    ("BZ=F", "Brent crude", "usd", "Commodities"), ("CL=F", "WTI crude", "usd", "Commodities"),
    ("GC=F", "Gold", "usd", "Commodities"),
    ("HG=F", "Copper", "usd", "Commodities"),
    ("^VIX", "VIX", "idx", "Volatility"), ("BTC-USD", "Bitcoin", "usd", "Crypto"),
]
OVERNIGHT = [
    ("ES=F", "S&P 500 futures"), ("NQ=F", "Nasdaq 100 futures"), ("YM=F", "Dow futures"),
    ("^N225", "Nikkei 225"), ("^HSI", "Hang Seng"), ("000300.SS", "CSI 300 (China)"),
    ("EURUSD=X", "EUR/USD"), ("BZ=F", "Brent crude"), ("GC=F", "Gold"), ("^TNX", "US 10-year yield"),
]


def _link_for(sym: str, kind: str) -> str:
    if kind == "yield":
        return "/m/MKT/bonds"
    return "/t/" + sym


def _markets(bd: date, S: date, px: dict) -> list[dict]:
    bds, out = bd.isoformat(), []
    for sym, name, kind, grp in MARKETS:
        b = px.get(sym)
        if not b:
            continue
        b1, b0, _ = _session(b, bds)
        if not b1 or not b0:
            continue
        row = {"sym": sym, "name": name, "kind": kind, "g": grp, "day": b1[0], "last": _r(b1[4], 4),
               "stale": b1[0] != S.isoformat(), "href": _link_for(sym, kind)}
        if kind == "yield":
            row["bp"] = round((b1[4] - b0[4]) * 100, 1)
        else:
            row["pct"] = _r(_pct(b1[4], b0[4]), 2)
        prior = [r[4] for r in b if r[0] < b1[0]][-252:]
        if prior and kind != "yield":
            row["ytd"] = _r(_pct(b1[4], next((r[4] for r in reversed(b) if r[0] < f"{S.year}-01-01"), None)), 1)
        out.append(row)
    # A continuous futures series jumps when it rolls to the next contract:
    # Brent's front month rolls at the end of the month and printed -5.5% on
    # a day WTI rose 1%. When the two crudes disagree that much, it is the
    # roll, not the market, and neither is quoted as a move.
    bz = next((r for r in out if r["sym"] == "BZ=F"), None)
    cl = next((r for r in out if r["sym"] == "CL=F"), None)
    if (bz and cl and bz.get("pct") is not None and cl.get("pct") is not None
            and abs(bz["pct"] - cl["pct"]) > 3 and bz["pct"] * cl["pct"] < 0):
        odd = bz if abs(bz["pct"]) > abs(cl["pct"]) else cl
        odd["roll"] = True
    return out


def _spx_eur(px: dict, bd: date) -> Optional[float]:
    """The S&P 500's session in euros: the benchmark a euro investor actually had."""
    g, fx = px.get("^GSPC"), px.get("EURUSD=X")
    if not g or not fx:
        return None
    b1, b0, _ = _session(g, bd.isoformat())
    if not b1 or not b0:
        return None
    f1, f0 = _rate_on(fx, b1[0]), _rate_on(fx, b0[0])
    return _r(((b1[4] / f1) / (b0[4] / f0) - 1) * 100, 2) if f1 and f0 else None


def _overnight(bd: date, S: date, px: dict, built: datetime) -> dict:
    bds, rows = bd.isoformat(), []
    for sym, name in OVERNIGHT:
        b = px.get(sym)
        if not b:
            continue
        b1, _b0, after = _session(b, bds)
        if not b1 or not after:
            rows.append({"sym": sym, "name": name, "pct": None, "note": "not traded yet"})
            continue
        last = after[-1]
        row = {"sym": sym, "name": name, "last": _r(last[4], 4), "href": _link_for(sym, "yield" if sym == "^TNX" else "")}
        if sym == "^TNX":
            row["bp"] = round((last[4] - b1[4]) * 100, 1)
        else:
            row["pct"] = _r(_pct(last[4], b1[4]), 2)
        rows.append(row)
    live = any(r.get("pct") is not None or r.get("bp") is not None for r in rows)
    return {"rows": rows, "as_of": built.isoformat(timespec="minutes"), "live": live,
            "note": None if live else ("Built before the overnight session opened; the morning build at "
                                       f"{MORNING[0]:02d}:{MORNING[1]:02d} fills this in.")}


# --------------------------------------------------------------------------
# news: yesterday's, sorted into what matters
# --------------------------------------------------------------------------

KINDS = [
    ("earnings", re.compile(r"\b(earnings|quarterly results|results|q[1-4] (profit|revenue|sales)|eps|beats? "
                            r"estimates|miss(es|ed)? estimates|tops estimates|falls short)\b", re.I)),
    ("guidance", re.compile(r"\b(guidance|outlook|raises? (its |full[- ]year )?(forecast|outlook|guidance)|"
                            r"cuts? (its |full[- ]year )?(forecast|outlook|guidance)|profit warning|warns)\b", re.I)),
    ("analyst", re.compile(r"\b(upgrades?|upgraded|downgrades?|downgraded|price target|initiates? coverage|"
                           r"overweight|underweight|outperform|underperform)\b", re.I)),
    ("deal", re.compile(r"\b(acquir(e|es|ed|ing)|acquisition|merger|takeover|buyout|bid for|stake in|"
                        r"spin[- ]?off|to buy \w+ (for|in)|agrees to buy|ipo)\b", re.I)),
    ("legal", re.compile(r"\b(lawsuit|sued|sues|jury|court|judge|probe|investigation|antitrust|fined|"
                         r"penalt(y|ies)|settle(s|d|ment)|recall)\b", re.I)),
    ("management", re.compile(r"\b(ceo|cfo|chief executive|steps down|resigns?|resigned|appoints?|"
                              r"appointed|succession|ousted)\b", re.I)),
    ("policy", re.compile(r"\b(tariffs?|sanctions?|export (controls?|curbs?|ban)|executive order|"
                          r"subsid(y|ies)|chips act|regulators?)\b", re.I)),
]
SHOCK = re.compile(r"\b(unexpected(ly)?|surprise[sd]?|surprising(ly)?|shock|plunge[sd]?|plummet(s|ed)?|"
                   r"soar(s|ed)?|surge[sd]?|tumble[sd]?|crash(es|ed)?|halt(s|ed)?|record (high|low|loss|fine)|"
                   r"biggest (drop|fall|gain|jump)|collapse[sd]?|default(s|ed)?|bankrupt(cy)?|emergency|"
                   r"sinks?|skyrockets?|craters?|spikes?)\b", re.I)
JUNK = re.compile(r"(\b\d+ (\w+ )?(stocks?|reasons?|things)\b|should you (buy|sell)|is it time to|"
                  r"\bbuy (the|this) dip\b|stocks? to (buy|watch|own)|\bzacks\b|motley fool|top (dow|s&p|nasdaq) "
                  r"movers|stay informed|here'?s why|history offers|what you actually pay|priced right|"
                  r"could (unlock|make you)|millionaire|before it'?s too late|best .* to buy|"
                  r"\bis [a-z .]+ stock a buy\b|how has .* story changed|worth a buy|buy or sell|"
                  r"(bull|bear) case|chance to buy|a buy\?)", re.I)


def _clean(s: str) -> str:
    s = (s or "").replace("\ufffd", "\u2019")
    s = re.sub(r"\s+[-–|]\s+(Reuters|Bloomberg|CNBC|AP|[\w.]+\.(com|co\.uk|net|org))\s*$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _news(since: float, until: float, held: dict, weights: dict) -> dict:
    """Big or unexpected headlines, and the ones about what you own."""
    from . import news as newsmod
    pool, seen = [], set()
    cache = (store.cache_get("news") or {}).get("items") or []
    for it in newsmod.logged(since, until) + [i for i in cache if (i.get("ts") or 0) >= since]:
        k = newsmod.url_key(it.get("url") or "") or it["headline"]
        if k in seen:
            continue
        seen.add(k)
        pool.append(it)
    rx = {t: _brand_rx(t, h["name"]) for t, h in held.items()}
    items = []
    for it in pool:
        hl, sm = _clean(it["headline"]), _clean(it.get("summary"))
        text = f"{hl} {sm[:220]}"
        about_hl = [t for t, r in rx.items() if r and r.search(hl)]
        about = about_hl + [t for t, r in rx.items() if r and t not in about_hl
                            and t in (it.get("tickers") or []) and r.search(sm[:220])]
        others = [t for t in (it.get("tickers") or []) if t not in held]
        # Scored on the companies the HEADLINE names; one only mentioned in
        # the summary adds a little, not a position's full weight.
        sc = newsmod.score({**it, "headline": hl, "summary": sm, "tickers": about_hl + others}, weights, now=until)
        s = sc["score"] + (8 if about and not about_hl else 0)
        kinds = [k for k, r in KINDS if r.search(text)]
        if sc.get("macro"):
            kinds.append("macro")
        shock = bool(SHOCK.search(hl))
        if shock:
            s += 22
        if about and any(k in kinds for k in ("guidance", "deal", "legal", "management")):
            s += 12
        junk = bool(JUNK.search(hl))
        if junk:
            s -= 45
        items.append({"headline": hl, "summary": sm[:300], "source": it.get("source"), "url": it.get("url"),
                      "ts": it.get("ts"), "about": about, "about_hl": about_hl, "kinds": kinds[:3],
                      "shock": shock, "junk": junk,
                      "macro": sc.get("macro"), "score": round(s, 1), "why": sc.get("why")})
    items.sort(key=lambda x: -x["score"])
    items = newsmod.dedupe([{**x, "tickers": x["about"]} for x in items])
    # Two lists, because they answer different questions: what happened to
    # the companies you own, and what happened to the world they trade in.
    # One ranked list let four stories about holdings crowd out the war.
    big, world, per_co = [], [], {}
    for x in items:
        if x["junk"]:
            continue
        if x["about_hl"]:
            if len(big) < 6 and (x["shock"] or x["score"] >= 85 or
                                 any(k in x["kinds"] for k in ("guidance", "deal", "legal", "management", "earnings"))):
                if sum(1 for b in big if set(b["about"]) & set(x["about"])) < 2:
                    big.append(x)
        elif len(world) < 6 and (x["macro"] or x["shock"] or "policy" in x["kinds"] or x["score"] >= 70):
            world.append(x)
        for t in x["about"]:
            per_co.setdefault(t, [])
            if len(per_co[t]) < 3:
                per_co[t].append(x)
    order = sorted(per_co, key=lambda t: -(held.get(t, {}).get("weight") or 0))
    return {"big": big, "world": world,
            "holdings": [{"ticker": t, "name": held[t]["name"], "weight": held[t].get("weight"),
                          "items": per_co[t]} for t in order[:14]],
            "n": len(pool), "since": since, "until": until,
            "all": items}


# --------------------------------------------------------------------------
# earnings: yesterday's and today's
# --------------------------------------------------------------------------

NOTABLE = 20e9          # ERN's "large cap" line


def _ern_days(days: list[date]) -> dict[str, dict]:
    """{date: {events: yours, watched and above $20bn, total: every company reporting}}."""
    from . import earnings
    out, weeks = {}, {}
    for d in days:
        mon = earnings.monday(d).isoformat()
        if mon not in weeks:
            weeks[mon] = _safe(lambda: earnings.week(mon, "market")) or {"days": []}
        day = next((x for x in weeks[mon]["days"] if x["date"] == d.isoformat()), None) or {}
        out[d.isoformat()] = {"events": day.get("events") or [], "total": day.get("total") or 0}
    return out


def _slim_ev(e: dict) -> dict:
    return {"sym": e["sym"], "name": e.get("name"), "date": e["date"], "when": e.get("when"),
            "time": e.get("time_local"), "time_exact": e.get("time_exact"),
            "eps_est": e.get("eps_est"), "eps_act": e.get("eps_act"), "surprise": _r(_f(e.get("surprise")), 1),
            "rev_est": e.get("rev_est"), "rev_act": e.get("rev_act"), "mcap": e.get("mcap"),
            "held": e.get("held"), "watch": e.get("watch"), "status": e.get("status"),
            "chain": e.get("chain"), "href": f"/m/ERN/{e['sym']}?d={e['date']}"}


def _results_line(sym: str, d: str) -> dict:
    """The parts of ERN's Results tab a brief needs, for a report of yours."""
    from . import earnings
    r = earnings.part(sym, "results", d)
    if not r.get("reported"):
        return {"reported": False}
    rx = r.get("reaction") or {}
    return {"reported": True, "eps": r.get("eps"), "revenue": r.get("revenue"),
            "guidance": [g.get("text") for g in (r.get("guidance") or [])[:2]],
            "reaction": {k: rx.get(k) for k in ("phase", "move", "gap", "eur", "vs_implied", "day")} if rx else None,
            "analysts": len(r.get("analysts_after") or [])}


def _earnings(bd: date, S: date, px: dict, budget: float = 60.0) -> dict:
    Ss, bds = S.isoformat(), bd.isoformat()
    days = _ern_days([S, bd])
    yest = [_slim_ev(e) for e in days[Ss]["events"]]
    today = [_slim_ev(e) for e in days[bds]["events"]]
    mine_y = [e for e in yest if e["held"] or e["watch"]]
    big_y = [e for e in yest if not (e["held"] or e["watch"]) and (e["mcap"] or 0) >= NOTABLE]
    # Not yours, but a supplier or a customer of something that is.
    tied_y = [e for e in yest if (e.get("chain") or {}).get("strong") and not (e["held"] or e["watch"])]
    need = list(dict.fromkeys(e["sym"] for e in big_y + mine_y + tied_y if e["sym"] not in px))
    if need:
        px = {**px, **(_safe(lambda: bars(need, period="1mo")) or {})}
    for e in {id(e): e for e in big_y + mine_y + tied_y}.values():
        b = px.get(e["sym"])
        if b and e.get("when") != "amc":
            b1, b0, _ = _session(b, bds)
            if b1 and b0 and b1[0] == Ss:
                e["move"] = _r(_pct(b1[4], b0[4]), 2)
    ex = ThreadPoolExecutor(4)
    futs = {ex.submit(_results_line, e["sym"], Ss): e for e in mine_y}
    done, _ = fwait(futs, timeout=budget)
    for f in done:
        try:
            futs[f]["results"] = f.result()
        except Exception as err:
            futs[f]["results"] = {"error": f"{err.__class__.__name__}"}
    ex.shutdown(wait=False, cancel_futures=True)
    mine_t = [e for e in today if e["held"] or e["watch"]]
    big_t = sorted([e for e in today if not (e["held"] or e["watch"]) and (e["mcap"] or 0) >= NOTABLE],
                   key=lambda e: -(e["mcap"] or 0))
    tied_t = [e for e in today if (e.get("chain") or {}).get("strong") and not (e["held"] or e["watch"])]
    by_w = lambda e: -((e.get("chain") or {}).get("weight") or 0)
    return {"yesterday": {"mine": mine_y, "big": sorted(big_y, key=lambda e: -(e["mcap"] or 0))[:12],
                          "tied": sorted(tied_y, key=by_w)[:8]},
            "today": {"mine": mine_t, "big": big_t[:12], "tied": sorted(tied_t, key=by_w)[:8]},
            "counts": {"yesterday": days[Ss]["total"], "today": days[bds]["total"]}}


# --------------------------------------------------------------------------
# today: the timeline
# --------------------------------------------------------------------------

MACRO_WHY = [
    (r"\bCPI\b|inflation|PCE|price index", "inflation — moves rate expectations, yields and growth stocks"),
    (r"non-farm|payrolls|unemployment|jobless|JOLTS|ADP|employment", "the labour market — the Fed's other mandate"),
    (r"federal funds rate|FOMC statement|FOMC press|monetary policy statement",
     "a Fed decision — rates, the dollar and every valuation"),
    (r"main refinancing|deposit facility|ECB press conference|monetary policy statement",
     "an ECB decision — the euro and European banks"),
    (r"GDP", "growth — the economy's pace"),
    (r"PMI|ISM", "business surveys — the first read on the month"),
    (r"retail sales|consumer (confidence|sentiment)|spending", "the consumer — two thirds of the US economy"),
    (r"speaks|testifies|testimony", "a central banker speaking — markets listen for the next move"),
    (r"treasury|auction|bond", "government borrowing — long yields"),
    (r"crude|oil|inventories", "oil — energy positions and inflation"),
]


def _macro_why(title: str) -> Optional[str]:
    for rx, why in MACRO_WHY:
        if re.search(rx, title, re.I):
            return why
    return None


SPEAKER = re.compile(r"^(?:(FOMC Member|Fed Governor|Fed Vice Chair|German Buba President|ECB Chief Economist|"
                     r"ECB Vice President|ECB (?:Executive Board )?Member|Fed Chair|ECB President)\s+)?(.+?) Speaks$")


def _ff_day(d: date, events: list[dict]) -> list[dict]:
    iso, out = d.isoformat(), []
    # Six Fed speakers in an afternoon is itself the news; one line each is
    # noise. The minor ones are grouped per central bank.
    talk: dict[str, list] = {}
    for e in events:
        if e.get("date") != iso or e.get("rank", 0) >= 2:
            continue
        m = SPEAKER.match(e.get("title") or "")
        if m and e.get("currency") in ("USD", "EUR"):
            talk.setdefault(e["currency"], []).append((e.get("time"), m.group(2), e.get("ts")))
    for cur, xs in talk.items():
        xs.sort(key=lambda x: x[0] or "99")
        who = "Fed" if cur == "USD" else "ECB and Bundesbank"
        out.append({"kind": "macro", "time": xs[0][0], "ts": xs[0][2], "ccy": cur,
                    "impact": 2 if len(xs) >= 3 else 1,
                    "title": f"{who} speakers: " + ", ".join(f"{n} {t or ''}".strip() for t, n, _ in xs),
                    "short": f"{len(xs)} {who} speakers from {xs[0][0]}" if len(xs) > 1 else None,
                    "why": "markets listen for hints about the next rate move", "href": "/m/MKT/bonds"})
    for e in events:
        if e.get("date") != iso:
            continue
        cur, rank = e.get("currency"), e.get("rank", 0)
        keep = (rank >= 2 and cur in ("USD", "EUR", "GBP", "ALL")) or (rank >= 3 and cur in ("CNY", "JPY")) or \
            (rank == 0 and e.get("impact") == "holiday" and cur in ("USD", "EUR"))
        if not keep:
            continue
        out.append({"kind": "macro", "time": e.get("time"), "ts": e.get("ts"), "title": e.get("title"),
                    "ccy": cur, "impact": max(rank, 1), "forecast": e.get("forecast"),
                    "previous": e.get("previous"), "url": e.get("url"), "why": _macro_why(e.get("title") or ""),
                    "href": "/m/MKT/bonds" if cur in ("USD", "EUR") else "/m/MKT"})
    return out


def _cb_day(d: date, ff: list[dict]) -> list[dict]:
    from . import sentiment
    iso, out = d.isoformat(), []
    titles = " ".join((e.get("title") or "") for e in ff)
    if iso in sentiment.FOMC and not re.search(r"federal funds rate|FOMC statement", titles, re.I):
        t = _at(d, 14, 0, NY)
        out.append({"kind": "cb", "time": _hm(t), "ts": t.timestamp(), "impact": 3,
                    "title": "Fed decision (FOMC); press conference 30 minutes later",
                    "why": "rates, the dollar and every valuation", "href": "/m/MKT/bonds"})
    if iso in sentiment.ECB_MEETINGS and not re.search(r"main refinancing|deposit facility", titles, re.I):
        t = _at(d, 14, 15)
        out.append({"kind": "cb", "time": "14:15", "ts": t.timestamp(), "impact": 3,
                    "title": "ECB decision; press conference at 14:45",
                    "why": "the euro, Bund yields and European banks", "href": "/m/MKT/bonds"})
    return out


def _auctions_between(d0: date, d1: date) -> list[dict]:
    from . import bonds
    a = _safe(bonds.part_auctions) or {}
    out = []
    for u in a.get("upcoming") or []:
        if not (d0.isoformat() <= u["d"] <= d1.isoformat()):
            continue
        t = _at(_dd(u["d"]), 13, 0, NY)
        size = f"${u['size_bn']:g}bn " if u.get("size_bn") else ""
        long_end = bool(re.search(r"\b(10|20|30)-Year", u.get("term") or ""))
        out.append({"kind": "auction", "date": u["d"], "time": _hm(t), "ts": t.timestamp(),
                    "impact": 2 if long_end else 1,
                    "title": f"US Treasury auction: {size}{u['term']}{' (reopening)' if u.get('reopen') else ''}",
                    "why": "a weak auction pushes long yields up" if long_end else None,
                    "href": "/m/MKT/bonds"})
    return out


def _elections_between(d0: date, d1: date) -> list[dict]:
    from . import geo
    out = []
    for e in geo.elections():
        if d0.isoformat() <= e["d"] <= d1.isoformat():
            out.append({"kind": "election", "date": e["d"], "time": None, "impact": 3,
                        "title": e["what"], "why": e.get("why"), "href": "/m/GEO"})
    return out


SCHED_WORDS = re.compile(r"\b(votes?|vote on|deadline|summit|talks|meets?|meeting|hearing|testif\w*|ruling|verdict|"
                         r"decision|speech|address(es)?|unveil\w*|keynote|investor day|launch(es)?|shutdown|"
                         r"expires?|takes effect|comes into force|announcement)\b", re.I)


def _flagged(news: dict, bd: date) -> list[dict]:
    """
    Events the news says are scheduled for today — a vote, a deadline, a
    hearing — that no calendar feed carries. A heuristic, and labelled one.
    """
    day = WEEKDAY[bd.weekday()]
    when = re.compile(rf"\b(today|on {day}|{day}'s|this {day}|{day} (morning|afternoon|evening))\b", re.I)
    out = []
    for x in news.get("all") or []:
        if x["junk"] or not when.search(x["headline"]) or not SCHED_WORDS.search(x["headline"]):
            continue
        if "today" in x["headline"].lower() and x.get("ts") and x["ts"] < _at(bd, 0, 0).timestamp():
            continue      # "today" in yesterday's headline was yesterday
        out.append({"kind": "news", "time": None, "impact": 2 if (x["macro"] or x["about"]) else 1,
                    "title": x["headline"], "detail": x.get("source"), "url": x.get("url"),
                    "about": x.get("about")})
        if len(out) >= 5:
            break
    return out


def _timeline(bd: date, ff: list[dict], ern: dict, news: dict, expo: dict) -> list[dict]:
    ffd = _ff_day(bd, ff)
    ev = ffd + _cb_day(bd, ffd) + _auctions_between(bd, bd) + _elections_between(bd, bd)
    ev += [{**m, "ts": None} for m in market_events(bd)]
    moves = {r["sym"]: r for r in (expo or {}).get("rows") or []}
    for e in ern["today"]["mine"]:
        mv = moves.get(e["sym"]) or {}
        ev.append({"kind": "earnings", "time": e.get("time"), "impact": 3 if e.get("held") else 2,
                   "title": f"{e['sym']} reports {'before the open' if e.get('when') == 'bmo' else 'after the close' if e.get('when') == 'amc' else ''}".strip(),
                   "sym": e["sym"], "mine": bool(e.get("held")), "watch": bool(e.get("watch")),
                   "eps_est": e.get("eps_est"), "rev_est": e.get("rev_est"), "time_exact": e.get("time_exact"),
                   "implied": mv.get("move"), "implied_eur": mv.get("eur"), "href": e["href"],
                   "weight": (e.get("held") or {}).get("weight")})
    shown = set()
    for e in ern["today"]["big"][:6]:
        shown.add(e["sym"])
        ev.append({"kind": "earnings", "time": e.get("time"),
                   "impact": 2 if (e.get("mcap") or 0) >= 200e9 or (e.get("chain") or {}).get("strong") else 1,
                   "title": f"{e['sym']} reports", "sym": e["sym"], "name": e.get("name"),
                   "eps_est": e.get("eps_est"), "mcap": e.get("mcap"), "href": e["href"], "chain": e.get("chain")})
    for e in (ern["today"].get("tied") or [])[:5]:
        if e["sym"] not in shown:
            ev.append({"kind": "earnings", "time": e.get("time"), "impact": 2, "title": f"{e['sym']} reports",
                       "sym": e["sym"], "name": e.get("name"), "eps_est": e.get("eps_est"), "mcap": e.get("mcap"),
                       "href": e["href"], "chain": e["chain"]})
    ev += _flagged(news, bd)
    ev.sort(key=lambda e: (e.get("time") is not None, e.get("time") or "", -(e.get("impact") or 0)))
    return ev


def _week_ahead(bd: date, ff: list[dict], held_syms: set) -> dict:
    from . import earnings, sentiment
    end = bd + timedelta(days=7)
    days = []
    d = next_weekday(bd)
    ern_up = _safe(lambda: earnings.upcoming_mine(days=10)) or []
    weeks = {}
    while d <= end:
        iso, items = d.isoformat(), []
        for e in _ff_day(d, ff):
            if e["impact"] >= 3:
                items.append(e)
        items += _cb_day(d, [e for e in ff if e.get("date") == iso])
        for e in ern_up:
            if e["date"] == iso:
                items.append({"kind": "earnings", "time": e.get("time_local"), "impact": 3 if e.get("held") else 2,
                              "title": f"{e['sym']} reports", "sym": e["sym"], "mine": bool(e.get("held")),
                              "watch": bool(e.get("watch")), "href": f"/m/ERN/{e['sym']}?d={e['date']}"})
        mon = earnings.monday(d).isoformat()
        if mon not in weeks:
            weeks[mon] = _safe(lambda: earnings.week(mon, "market")) or {"days": []}
        day = next((x for x in weeks[mon]["days"] if x["date"] == iso), None) or {}
        for e in day.get("events") or []:
            if (e.get("mcap") or 0) >= 200e9 and not (e.get("held") or e.get("watch")):
                items.append({"kind": "earnings", "time": e.get("time_local"), "impact": 2,
                              "title": f"{e['sym']} reports", "sym": e["sym"], "name": e.get("name"),
                              "href": f"/m/ERN/{e['sym']}?d={iso}"})
        items += [a for a in _auctions_between(d, d) if a["impact"] >= 2]
        items += [{**m, "ts": None} for m in market_events(d) if m["impact"] >= 2]
        if items:
            items.sort(key=lambda e: (e.get("time") is None, e.get("time") or "", -(e.get("impact") or 0)))
            days.append({"date": iso, "items": items})
        d = next_weekday(d)
    later = []
    for dd in sentiment.FOMC:
        if end.isoformat() < dd <= (bd + timedelta(days=45)).isoformat():
            later.append({"date": dd, "kind": "cb", "title": "Fed decision (FOMC)", "href": "/m/MKT/bonds"})
            break
    for dd in sentiment.ECB_MEETINGS:
        if end.isoformat() < dd <= (bd + timedelta(days=45)).isoformat():
            later.append({"date": dd, "kind": "cb", "title": "ECB decision", "href": "/m/MKT/bonds"})
            break
    later += [{"date": e["date"], "kind": "election", "title": e["title"], "href": "/m/GEO"}
              for e in _elections_between(end + timedelta(days=1), bd + timedelta(days=60))]
    later.sort(key=lambda e: e["date"])
    return {"days": days, "later": later}


# --------------------------------------------------------------------------
# today: what touches your positions
# --------------------------------------------------------------------------

def _outside_hours(held: dict, S: date, budget: float = 40.0) -> tuple[list[dict], dict]:
    """After-hours and pre-market moves of your US names, and every holding's dividend dates."""
    from . import research
    ovs = {}
    ex = ThreadPoolExecutor(8)
    futs = {ex.submit(research.overview, t): t for t in held}
    done, _ = fwait(futs, timeout=budget)
    for f in done:
        try:
            ovs[futs[f]] = f.result()
        except Exception:
            pass
    ex.shutdown(wait=False, cancel_futures=True)
    moves = []
    for t, ov in ovs.items():
        q = ov.get("quote") or {}
        if re.search(r"\.[A-Z]{1,3}$", t):
            continue
        st = (q.get("market_state") or "").upper()
        pre, post = q.get("pre_change_pct"), q.get("post_change_pct")
        pick = None
        if pre is not None and st in ("PRE", "PREPRE", "REGULAR") and q.get("pre_price"):
            pick = ("pre-market", pre, q.get("pre_price"))
        elif post is not None and q.get("post_price"):
            pick = ("after hours", post, q.get("post_price"))
        if pick and abs(pick[1]) >= 1.5:
            h = held[t]
            moves.append({"ticker": t, "phase": pick[0], "pct": _r(pick[1], 2), "price": pick[2],
                          "eur": _r((h.get("value_eur") or 0) * pick[1] / 100, 0), "weight": h.get("weight"),
                          "href": f"/t/{t}"})
    moves.sort(key=lambda m: -abs(m["eur"] or 0))
    return moves, ovs


def _positions_watch(bd: date, S: date, dash: dict, held: dict, ovs: dict, book: dict) -> dict:
    from . import research
    bds = bd.isoformat()
    until = (bd + timedelta(days=7)).isoformat()
    divs = []
    for t, ov in ovs.items():
        dt = ov.get("dates") or {}
        ex_ = dt.get("ex_div")
        if ex_ and S.isoformat() <= ex_ <= until:
            st = ov.get("stats") or {}
            divs.append({"ticker": t, "ex": ex_, "pay": dt.get("div_pay"), "rate": st.get("div_rate"),
                         "yield": st.get("div_yield"), "currency": ov.get("currency"),
                         "past": ex_ < bds, "href": f"/t/{t}"})
    divs.sort(key=lambda x: x["ex"])
    reviews, theses = [], {}
    last = {x["ticker"]: x for x in book.get("positions") or []}
    for th in _safe(research.theses, []) or []:
        sym = th.get("symbol")
        rd = th.get("review_date")
        mv = last.get(sym) or {}
        px = mv.get("close") or (held.get(sym) or {}).get("price")
        tgt = _f(th.get("target"))
        row = {"ticker": sym, "stance": th.get("stance"), "target": tgt, "price": px,
               "to_target": _r(_pct(tgt, px), 1) if tgt and px else None, "review": rd,
               "kill": (th.get("kill") or "")[:200] or None, "href": f"/t/{sym}#s-thesis"}
        theses[sym] = {k: row[k] for k in ("stance", "target", "to_target", "review")}
        if rd and rd <= until:
            row["due"] = "overdue" if rd < bds else ("today" if rd == bds else "this week")
            reviews.append(row)
        elif mv and abs(mv.get("move") or 0) >= 4:
            row["due"] = "moved"
            reviews.append(row)
    rules = []
    by_tk = {p.get("ticker"): p for p in dash.get("positions", []) if p.get("ticker")}
    by_isin = {p.get("isin"): p for p in dash.get("positions", [])}
    for rule in _safe(store.list_rules, []) or []:
        if not rule.get("enabled"):
            continue
        p = by_isin.get(rule.get("isin")) or by_tk.get(rule.get("ticker"))
        if not p:
            continue
        lab, v, kind = rule.get("ticker") or p.get("ticker") or "?", rule["value"], rule["kind"]
        px, w = p.get("price"), p.get("weight")
        row = None
        if kind in ("price_below", "price_above") and px:
            dist = (px / v - 1) * 100
            hit = px < v if kind == "price_below" else px > v
            if hit or abs(dist) <= 3:
                row = {"ticker": lab, "text": f"{lab} at {px:,.2f} — your rule: {kind.replace('_', ' ')} {v:g}",
                       "state": "fired" if hit else f"{abs(dist):.1f}% away"}
        elif kind == "move_pct":
            mv = (last.get(p.get("ticker")) or {}).get("move")
            if mv is not None and abs(mv) >= abs(v):
                row = {"ticker": lab, "text": f"{lab} moved {mv:+.1f}% yesterday — your threshold is {abs(v):g}%",
                       "state": "fired"}
        elif kind == "weight_above" and w is not None and w > v * 0.95:
            row = {"ticker": lab, "text": f"{lab} is {w:.1f}% of the book — your limit {v:g}%",
                   "state": "fired" if w > v else "close"}
        if row:
            rules.append({**row, "note": rule.get("note"), "href": f"/t/{lab}"})
    ko = []
    for p in dash.get("positions", []):
        k = (p.get("lev") or {}).get("knockout_pct")
        if k is not None and k < 20:
            ko.append({"name": p.get("name"), "ticker": p.get("underlying_ticker"), "pct": _r(k, 1),
                       "value_eur": p.get("value_eur")})
    return {"dividends": divs, "reviews": reviews, "theses": theses, "rules": rules, "knockouts": ko}


# --------------------------------------------------------------------------
# the short version
# --------------------------------------------------------------------------

def _lk(sym: str) -> str:
    return f"[{sym}](/t/{sym})"


def _andlist(xs: list[str]) -> str:
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]


def summary(b: dict) -> list[str]:
    """A handful of sentences, with links, written by rules from the brief itself."""
    out = []
    book, S = b.get("book") or {}, b["session"]
    day = WEEKDAY[_dd(S).weekday()]
    spx = b.get("spx_eur")
    if book.get("eur") is not None and book.get("n_traded"):
        verb = "rose" if book["eur"] >= 0 else "fell"
        s = f"Your book {verb} **{_eur(abs(book['eur']))}** ({_pc(book['pct'], 2)}) on {day}"
        if spx is not None and book.get("pct") is not None:
            diff = book["pct"] - spx
            rel = "ahead of" if diff > 0.15 else ("behind" if diff < -0.15 else "in line with")
            s += f", {rel} the S&P 500 in euros ({_pc(spx, 2)})"
        fx = book.get("eur_fx") or 0
        if abs(fx) >= max(50, abs(book["eur"]) * 0.25):
            s += f"; the currency {'added' if fx > 0 else 'took'} {_eur(abs(fx))} of it"
        out.append(s + ".")
        pos = [x for x in book.get("positions") or [] if x["traded"]]
        same = [x for x in pos if (x["eur"] or 0) * (book["eur"] or 0) > 0][:3]
        if same:
            s = "Most of it: " + ", ".join(f"{_lk(x['ticker'])} {_pc(x['move'])} ({_eur(x['eur'])})" for x in same)
            secs = [s_ for s_ in book.get("sectors") or [] if s_["sector"] not in ("Other", None)]
            if secs and book["eur"]:
                top = max(secs, key=lambda s_: abs(s_["eur"] or 0) * (1 if (s_["eur"] or 0) * book["eur"] > 0 else 0))
                if top["eur"] and abs(top["eur"]) >= abs(book["eur"]) * 0.5 and top["eur"] * book["eur"] > 0:
                    s += f"; {top['sector']} accounted for {_eur(top['eur'])}"
            out.append(s + ".")
        told = []
        for x in pos[:8]:
            rs = x.get("reason") or {}
            if rs.get("kind") in ("catalyst", "headline"):
                src = f" ({rs['source']})" if rs.get("source") else ""
                told.append(f"{_lk(x['ticker'])} {_pc(x['move'])}: " +
                            (rs["text"] if rs["kind"] == "catalyst" else f"“{rs['text']}”{src}"))
            elif rs.get("kind") == "none" and abs(x.get("move") or 0) >= 3:
                told.append(f"{_lk(x['ticker'])} {_pc(x['move'])}: no cause found in the news or filings")
            if len(told) >= 2:
                break
        if told:
            out.append("Why: " + "; ".join(told) + ".")
        hi = [x["ticker"] for x in pos if x["high52"]]
        lo = [x["ticker"] for x in pos if x["low52"]]
        vol = [x for x in pos if (x["rvol"] or 0) >= 2.5]
        bits = []
        if hi:
            bits.append("52-week highs: " + ", ".join(_lk(t) for t in hi[:5]))
        if lo:
            bits.append("52-week lows: " + ", ".join(_lk(t) for t in lo[:5]))
        if vol:
            bits.append("heavy volume: " + ", ".join(f"{_lk(x['ticker'])} {x['rvol']:.1f}×" for x in vol[:4]))
        if bits:
            s = "; ".join(bits)
            out.append(s[0].upper() + s[1:] + ".")
    ey = (b.get("earnings") or {}).get("yesterday") or {}
    rep = []
    for e in ey.get("mine") or []:
        r = e.get("results") or {}
        eps = r.get("eps") or {}
        if not r.get("reported") and e.get("eps_act") is None:
            continue
        sp = eps.get("surprise") if eps else e.get("surprise")
        verdict = "beat" if (sp or 0) > 0 else ("missed" if (sp or 0) < 0 else "reported")
        s = f"[{e['sym']}]({e['href']}) {verdict}"
        if eps.get("actual") is not None and eps.get("est") is not None:
            s += f" (EPS {eps['actual']:g} vs {eps['est']:g})"
        rx = r.get("reaction") or {}
        if rx.get("move") is not None:
            s += f", {_pc(rx['move'])} {'after hours' if rx.get('phase') == 'after hours' else 'on the day' if rx.get('phase') == 'session' else rx.get('phase') or ''}".rstrip()
        rep.append(s)
    if rep:
        out.append("Earnings: " + "; ".join(rep) + ".")
    elif ey.get("big"):
        bb = [e for e in ey["big"] if e.get("eps_act") is not None][:3]
        if bb:
            out.append("None of yours reported. Among large caps: " + "; ".join(
                f"[{e['sym']}]({e['href']}) {'beat' if (e.get('surprise') or 0) > 0 else 'missed' if (e.get('surprise') or 0) < 0 else 'in line'}"
                + (f", {_pc(e['move'])}" if e.get("move") is not None else "") for e in bb) + ".")
    # Read-through: a company you do not hold reported, and it trades with ones you do.
    def tie(c: dict) -> str:
        bits = []
        if c.get("sells_to"):
            bits.append("sells to " + _andlist(c["sells_to"][:3]))
        if c.get("buys_from"):
            bits.append("buys from " + _andlist(c["buys_from"][:3]))
        return " and ".join(bits)
    tied = [e for e in ey.get("tied") or [] if e.get("eps_act") is not None or e.get("move") is not None][:2]
    if tied:
        out.append("Read-through: " + "; ".join(
            f"[{e['sym']}]({e['href']}), which {tie(e['chain'])}, "
            + ("beat" if (e.get("surprise") or 0) > 0 else "missed" if (e.get("surprise") or 0) < 0 else "reported")
            + (f" ({_pc(e['move'])})" if e.get("move") is not None else "") for e in tied) + ".")
    tied_t = ((b.get("earnings") or {}).get("today") or {}).get("tied") or []
    if tied_t:
        out.append("Reporting today and tied to your book: " + "; ".join(
            f"[{e['sym']}]({e['href']}) {tie(e['chain'])}" for e in tied_t[:3]) + ".")
    mk = {m["sym"]: m for m in b.get("markets") or []}
    parts = []
    for sym in ("^GSPC", "^NDX", "^AEX", "^STOXX50E"):
        m = mk.get(sym)
        if m and m.get("pct") is not None and not m.get("stale"):
            parts.append(f"{m['name']} {'flat' if abs(m['pct']) < 0.05 else _pc(m['pct'])}")
    extra = []
    t10 = mk.get("^TNX")
    if t10 and t10.get("bp") is not None and abs(t10["bp"]) >= 4:
        extra.append(f"US 10-year {t10['last']:.2f}% ({t10['bp']:+.0f}bp)")
    for sym, lim in (("BZ=F", 2), ("CL=F", 2), ("GC=F", 1.5), ("EURUSD=X", 0.5), ("^VIX", 8), ("BTC-USD", 5)):
        m = mk.get(sym)
        if m and m.get("pct") is not None and abs(m["pct"]) >= lim and not m.get("roll") and not m.get("stale"):
            extra.append(f"{m['name']} {_pc(m['pct'])}")
    if parts:
        out.append("Markets: " + ", ".join(parts) + ("; " + ", ".join(extra) if extra else "") + ".")
    if b.get("oil"):
        out.append(b["oil"])
    if b.get("wl"):
        out.append(b["wl"])
    if b.get("eur"):
        out.append(b["eur"])
    if b.get("idx"):
        out.append(b["idx"])
    if b.get("read"):
        out.append(b["read"])
    if b.get("kai"):
        out.append(b["kai"])
    if b.get("pol"):
        out.append(b["pol"])
    if b.get("flow"):
        out.append(b["flow"])
    nb = ((b.get("news") or {}).get("world") or [])[:1]
    if nb:
        out.append(f"In the news: “{nb[0]['headline']}” ({nb[0].get('source') or 'news'}).")
    tl = [e for e in b.get("today") or [] if (e.get("impact") or 0) >= 2 or e.get("mine")]
    tl.sort(key=lambda e: (-(e.get("mine") and 1 or 0), -(e.get("impact") or 0)))
    tl = sorted(tl[:5], key=lambda e: (e.get("time") is not None, e.get("time") or ""))
    wd = WEEKDAY[_dd(b["date"]).weekday()]
    if tl:
        def t(e):
            if e["kind"] == "earnings":
                return (f"[{e['sym']}]({e['href']}) reports" + (" — yours" if e.get("mine") else "") +
                        (f" {e['time']}" if e.get("time") else ""))
            return (f"{e['time']} " if e.get("time") and not e.get("short") else "") + (e.get("short") or e["title"])
        out.append(f"Today ({wd}): " + "; ".join(t(e) for e in tl) + ".")
    else:
        out.append(f"Today ({wd}): nothing of medium or high impact is scheduled.")
    on = b.get("overnight") or {}
    fut = next((r for r in on.get("rows") or [] if r["sym"] == "ES=F" and r.get("pct") is not None), None)
    oh = (b.get("watch") or {}).get("outside") or []
    bits = []
    if fut:
        bits.append(f"S&P futures {_pc(fut['pct'], 2)}")
    bits += [f"{_lk(m['ticker'])} {_pc(m['pct'])} {m['phase']}" for m in oh[:3]]
    if bits:
        out.append("Before the open: " + ", ".join(bits) + ".")
    return out


# --------------------------------------------------------------------------
# building it
# --------------------------------------------------------------------------

JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def build(bd: date, edition: str = "refresh", job: Optional[dict] = None) -> dict:
    """Assemble one brief. Blocking — a minute on a cold cache; call it off the event loop."""
    from . import service, earnings, calendar as cal, news as newsmod
    job = job if job is not None else {}
    t0 = time.time()
    built = now_local()
    S = prev_weekday(bd)
    errors = {}

    def phase(p):
        job["phase"] = p

    def part(name, fn, default=None):
        try:
            return fn()
        except Exception as e:
            errors[name] = f"{e.__class__.__name__}: {e}"[:240]
            return default

    phase("reading the book")
    dash = part("book", lambda: service.dashboard("SP500", light=True), {}) or {}
    held = earnings.held()
    weights = {t: h.get("weight") or 0 for t, h in held.items()}

    phase("fetching a year of prices")
    ccys = {(p.get("currency") or "EUR").upper() for p in dash.get("positions", [])}
    fx = [{"GBX": "EURGBP=X"}.get(c, f"EUR{c}=X") for c in ccys if c != "EUR"]
    syms = [p.get("ticker") for p in dash.get("positions", []) if p.get("ticker")]
    syms += [m[0] for m in MARKETS] + [o[0] for o in OVERNIGHT] + fx
    px = part("prices", lambda: bars(syms), {}) or {}
    # A brief without prices is no brief. When Windows blocked pandas one
    # morning, the 06:30 build ran anyway and saved an empty book over the
    # good brief of the night before. Keep the one that has numbers.
    if not px:
        prior = stored(bd.isoformat())
        if prior and (prior.get("book") or {}).get("n"):
            raise RuntimeError(f"no prices could be read ({errors.get('prices') or 'Yahoo returned nothing'}); "
                               f"the brief built {str(prior.get('built_at', ''))[:16].replace('T', ' ')} is kept")
    filled = part("gaps", lambda: fill_gaps(px, list(px), S, bd), {}) or {}

    phase("working out yesterday's moves")
    book = part("book", lambda: _book(bd, S, dash, px), {}) or {}
    markets = part("markets", lambda: _markets(bd, S, px), []) or []

    # The slow, independent parts run side by side.
    phase("explaining the movers, reading the earnings and the calendars")
    since = _at(S, 0, 0).timestamp()
    pool = ThreadPoolExecutor(6)
    f_why = pool.submit(_explain, book.get("positions") or [], S.isoformat())
    f_ern = pool.submit(_earnings, bd, S, px)
    f_ff = pool.submit(lambda: store.cache_through("brief:ff", 3600, cal.ff_events))
    f_oh = pool.submit(_outside_hours, held, S)
    f_expo = pool.submit(lambda: earnings.exposure(bd.isoformat()))
    news = part("news", lambda: _news(since, built.timestamp(), held, weights), {}) or {}

    def res(name, f, default, timeout=150):
        try:
            return f.result(timeout=timeout)
        except Exception as e:
            errors[name] = f"{e.__class__.__name__}: {e}"[:240]
            return default
    why = res("why", f_why, {})
    ern = res("earnings", f_ern, {"yesterday": {"mine": [], "big": []}, "today": {"mine": [], "big": []}})
    ffp = res("macro", f_ff, {}) or {}
    ff = (ffp.get("events") or []) + [e for e in cal.remembered(S.isoformat(), S.isoformat())
                                      if not any(x.get("title") == e.get("title") and x.get("date") == e.get("date")
                                                 for x in ffp.get("events") or [])]
    if ffp.get("error") and not ffp.get("events"):
        errors["macro"] = ffp["error"]
    outside, ovs = res("outside hours", f_oh, ([], {}))
    expo = res("exposure", f_expo, {})
    pool.shutdown(wait=False, cancel_futures=True)

    phase("laying out today")
    today = part("today", lambda: _timeline(bd, ff, ern, news, expo), []) or []
    yest_macro = [e for e in _ff_day(S, ff)]
    week = part("week", lambda: _week_ahead(bd, ff, set(held)), {"days": [], "later": []})
    watch = part("watch", lambda: _positions_watch(bd, S, dash, held, ovs, book), {}) or {}
    watch["outside"] = outside
    overnight = part("overnight", lambda: _overnight(bd, S, px, built), {}) or {}

    # Each mover's reason and headlines on the day, for the table.
    for x in (book.get("positions") or [])[:40]:
        w = why.get(x["ticker"])
        if w:
            x["why"] = w
        nws = next((h for h in (news.get("holdings") or []) if h["ticker"] == x["ticker"]), None)
        items = [{"headline": i["headline"], "url": i["url"], "source": i["source"]}
                 for i in (nws or {}).get("items", [])]
        if items:
            x["news"] = items[:2]
        if x["traded"] and abs(x.get("move") or 0) >= 0.8:
            x["reason"] = _reason(x, w, items)
        ov = ovs.get(x["ticker"]) or {}
        if (ov.get("dates") or {}).get("ex_div") == S.isoformat():
            x["ex_div"] = True
        th = (watch.get("theses") or {}).get(x["ticker"])
        if th:
            x["thesis"] = th

    news.pop("all", None)
    b = {
        "date": bd.isoformat(), "session": S.isoformat(), "edition": edition,
        "built_at": built.isoformat(timespec="seconds"), "built_ts": built.timestamp(),
        "took_s": round(time.time() - t0, 1),
        "book": book, "spx_eur": _spx_eur(px, bd), "why": why, "markets": markets,
        "earnings": ern, "news": news, "yesterday_macro": yest_macro,
        "overnight": overnight, "today": today, "watch": watch, "week": week,
        "errors": errors, "holidays_as_of": HOLIDAYS_AS_OF,
        "window": {"since": since, "until": built.timestamp()},
        "sources": {"prices": len(px), "headlines": news.get("n"), "macro": ffp.get("source"),
                    "news_provider": _safe(newsmod.provider_name), "filled": filled},
    }
    from . import oil as oilmod
    b["oil"] = part("oil", oilmod.brief_line, None)
    from . import watch as wlmod
    b["wl"] = part("watchlist", wlmod.brief_line, None)
    from . import euro as eurmod
    b["eur"] = part("euro", eurmod.brief_line, None)
    from . import idx as idxmod
    b["idx"] = part("indices", lambda: (lambda t: t and f"Indices: {t}.")(idxmod.brief_line()), None)
    from . import reader as readmod
    b["read"] = part("articles", lambda: (lambda t: t and f"{t}.")(readmod.brief_line()), None)
    from . import koersai as kaimod
    b["kai"] = part("koers ai", lambda: (lambda t: t and f"{t}.")(kaimod.brief_line()), None)
    from . import pol as polmod
    b["pol"] = part("congress", lambda: (lambda t: t and f"{t}.")(polmod.brief_line()), None)
    from . import flow as flowmod
    b["flow"] = part("flow", lambda: (lambda t: t and f"{t}.")(flowmod.brief_line()), None)
    b["summary"] = part("summary", lambda: summary(b), []) or []
    save(b, edition)
    return b


def save(b: dict, edition: str):
    store.init_db()
    with store.conn() as c:
        c.execute("INSERT OR REPLACE INTO brief_day (d, built_at, edition, payload) VALUES (?,?,?,?)",
                  (b["date"], b["built_ts"], edition, json.dumps(b, default=str)))
        cut = (date.today() - timedelta(days=KEEP_DAYS)).isoformat()
        c.execute("DELETE FROM brief_day WHERE d < ?", (cut,))


def stored(d: str) -> Optional[dict]:
    store.init_db()
    with store.conn() as c:
        r = c.execute("SELECT payload FROM brief_day WHERE d=?", (d,)).fetchone()
    if not r:
        return None
    try:
        return json.loads(r["payload"])
    except Exception:
        return None


def built_at(d: str) -> Optional[float]:
    store.init_db()
    with store.conn() as c:
        r = c.execute("SELECT built_at FROM brief_day WHERE d=?", (d,)).fetchone()
    return r["built_at"] if r else None


def dates(n: int = 90) -> list[dict]:
    store.init_db()
    with store.conn() as c:
        rows = c.execute("SELECT d, built_at, edition FROM brief_day ORDER BY d DESC LIMIT ?", (n,)).fetchall()
    return [{"d": r["d"], "built_at": r["built_at"], "edition": r["edition"]} for r in rows]


def _run_build(job: dict, bd: date, edition: str):
    try:
        build(bd, edition, job)
        job.update(state="done", phase="done")
    except Exception as e:
        job.update(state="error", error=f"{e.__class__.__name__}: {e}"[:300])
    finally:
        job["finished"] = time.time()


def start_build(d: str, edition: str = "refresh") -> dict:
    bd = _dd(d)
    with _LOCK:
        j = JOBS.get(d)
        if j and j["state"] == "running":
            return _pub(j)
        j = {"state": "running", "phase": "starting", "started": time.time(), "finished": None,
             "error": None, "d": d, "edition": edition}
        JOBS[d] = j
    threading.Thread(target=_run_build, args=(j, bd, edition), daemon=True, name=f"alrt-{d}").start()
    return _pub(j)


def _pub(j: Optional[dict]) -> Optional[dict]:
    if not j:
        return None
    return {**j, "elapsed_s": round((j.get("finished") or time.time()) - j["started"])}


def view(d: Optional[str] = None) -> dict:
    """The page's one call: the brief for a day (built on first ask), and its state."""
    default = brief_day().isoformat()
    d = d or default
    b = stored(d)
    j = JOBS.get(d)
    running = bool(j and j["state"] == "running")
    if b is None and not running and prev_weekday(date.today()).isoformat() <= d <= default:
        start_build(d, "morning" if d == date.today().isoformat() else "refresh")
        j = JOBS.get(d)
    return {"d": d, "default": default, "today": date.today().isoformat(),
            "now": now_local().isoformat(timespec="seconds"),
            "brief": b, "job": _pub(j), "dates": dates()}


def latest_summary() -> dict:
    """For the home screen: the brief to read now, in two lines."""
    d = brief_day().isoformat()
    b = stored(d)
    if b is None:
        rows = dates(1)
        b = stored(rows[0]["d"]) if rows else None
    if not b:
        return {"ok": False, "default": d}
    bk = b.get("book") or {}
    return {"ok": True, "d": b["date"], "session": b["session"], "default": d, "built_at": b["built_at"],
            "summary": (b.get("summary") or [])[:2], "book": {"eur": bk.get("eur"), "pct": bk.get("pct")},
            "today_n": len([e for e in b.get("today") or [] if (e.get("impact") or 0) >= 2 or e.get("mine")])}


# --------------------------------------------------------------------------
# the scheduler
# --------------------------------------------------------------------------

_STOP = threading.Event()
_THREAD: dict = {"t": None}


def _tick():
    n = now_local()
    if n.weekday() >= 5 or not (MORNING <= (n.hour, n.minute) < (12, 0)):
        return
    d = n.date().isoformat()
    b_at = built_at(d)
    cut = _at(n.date(), 5, 0).timestamp()
    if b_at is None or b_at < cut:
        j = JOBS.get(d)
        if j and j["state"] == "running":
            return
        # A build that failed is retried every half hour, not every minute.
        if j and j["state"] == "error" and time.time() - (j.get("finished") or 0) < 1800:
            return
        j = {"state": "running", "phase": "starting", "started": time.time(), "finished": None,
             "error": None, "d": d, "edition": "morning"}
        JOBS[d] = j
        _run_build(j, n.date(), "morning")
    if store.get_setting("alrt_auto_claude") == "1" and not notes(d):
        nj = NOTE_JOBS.get(d)
        if not (nj and nj["state"] in ("running", "error")):
            try:
                start_note(d)
            except Exception as e:
                NOTE_JOBS[d] = {"state": "error", "error": str(e)[:300], "started": time.time(),
                                "finished": time.time(), "text": "", "phase": ""}


def _loop():
    _STOP.wait(25)
    while not _STOP.is_set():
        try:
            _tick()
        except Exception:
            pass
        _STOP.wait(60)


def ensure():
    t = _THREAD["t"]
    if t and t.is_alive():
        return
    _STOP.clear()
    t = threading.Thread(target=_loop, daemon=True, name="alrt-scheduler")
    _THREAD["t"] = t
    t.start()


# --------------------------------------------------------------------------
# Claude's version
# --------------------------------------------------------------------------

SYSTEM = """You write the morning brief for one private investor in the Netherlands, read before the European open. The material is a JSON document the investor's own terminal assembled: their portfolio's move in the last session in euros and what drove each position, the earnings that came out, the headlines, the markets, and today's schedule in Amsterdam time.

Everything you write must come from the material, or — only if you were given web search — from pages you found, which you then cite by name. Never invent or round a number into a different one. When the material has no cause for a move, say no cause was found rather than guessing.

Write for someone who knows markets: plain, specific, short. Structure, in Markdown:
# a one-line headline for the day
**The short version** — three bullets.
## Yesterday
The book (what moved it and why), earnings that matter to it, the news that matters, the markets — in that order, in short paragraphs or bullets.
## Today
The schedule that matters for this book, in time order (Amsterdam time), and what each event could do to it.
## Worth watching
At most four bullets.

Link tickers the way the material does: [MSFT](/t/MSFT); an earnings report as [MU](/m/ERN/MU?d=2026-09-23). Do not recommend buying or selling anything. Stay under 500 words."""

NOTE_JOBS: dict[str, dict] = {}


def _material(b: dict) -> str:
    """The brief, trimmed to what a writer needs."""
    bk = dict(b.get("book") or {})
    bk["positions"] = [{k: v for k, v in x.items() if k not in ("prev_close",)}
                       for x in (bk.get("positions") or [])[:25]]
    news = b.get("news") or {}
    slim = {
        "brief_for": b["date"], "session_reviewed": b["session"], "built": b["built_at"],
        "book": bk, "sp500_in_euros_pct": b.get("spx_eur"), "markets": b.get("markets"),
        "earnings": b.get("earnings"),
        "headlines_on_holdings": [{k: x.get(k) for k in ("headline", "summary", "source", "about", "kinds", "shock")}
                                  for x in news.get("big") or []],
        "headlines_world": [{k: x.get(k) for k in ("headline", "summary", "source", "kinds", "shock")}
                            for x in news.get("world") or []],
        "headlines_by_holding": [{"ticker": h["ticker"], "items": [i["headline"] for i in h["items"]]}
                                 for h in news.get("holdings") or []],
        "yesterday_macro_releases": b.get("yesterday_macro"),
        "overnight": b.get("overnight"), "today": b.get("today"), "your_positions_today": b.get("watch"),
        "week_ahead": b.get("week"), "rule_based_summary": b.get("summary"),
    }
    return "<brief>\n" + json.dumps(slim, default=str, ensure_ascii=False) + "\n</brief>"


def notes(d: str) -> list[dict]:
    store.init_db()
    with store.conn() as c:
        rows = c.execute("SELECT id,kind,question,created,model,body,meta,cost_usd FROM brief_note "
                         "WHERE d=? ORDER BY id", (d,)).fetchall()
    out = []
    for r in rows:
        x = dict(r)
        try:
            x["meta"] = json.loads(x["meta"] or "{}")
        except Exception:
            x["meta"] = {}
        out.append(x)
    return out


def delete_note(nid: int) -> bool:
    with store.conn() as c:
        return c.execute("DELETE FROM brief_note WHERE id=?", (nid,)).rowcount > 0


def _save_note(d: str, question: Optional[str], body: str, model_name: str, meta: dict,
               cost_usd: Optional[float]) -> int:
    store.init_db()
    with store.conn() as c:
        cur = c.execute("INSERT INTO brief_note (d,kind,question,created,model,body,meta,cost_usd) "
                        "VALUES (?,?,?,?,?,?,?,?)",
                        (d, "answer" if question else "note", question,
                         datetime.now(timezone.utc).isoformat(timespec="seconds"), model_name, body,
                         json.dumps(meta), cost_usd))
        return cur.lastrowid


def _ask(question: str) -> str:
    return (f"The investor has read your brief and asks:\n\n{question}\n\n"
            "Answer directly from the material. As long as it needs, no longer.")


def _work_dir(d: str) -> str:
    import os
    w = os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), "alrt_work", d)
    os.makedirs(w, exist_ok=True)
    return w


def _run_note(job: dict, d: str, question: Optional[str], web: bool):
    from . import earnai
    try:
        b = stored(d)
        if not b:
            raise RuntimeError("there is no brief for this day to write about")
        material = _material(b)
        ask = (_ask(question) if question else
               f"Write the morning brief for {WEEKDAY[_dd(d).weekday()]} {d}, covering {b['session']}.")
        if web and not question:
            ask += (" You have web search: use it only to find why a position moved when the material gives no "
                    "cause, or to confirm a big story. At most four searches; cite what you used.")
        prior = [n for n in notes(d) if n["kind"] == "note"]
        if job["writer"] == "claude-code":
            import os
            work = _work_dir(d)
            tools = "WebSearch,WebFetch" if web and not question else ""
            resume = (prior[-1]["meta"] or {}).get("session") if (question and prior) else None
            out = None
            if resume:
                try:
                    job["phase"] = "answering"
                    out = earnai.cc_stream(earnai.cc_command("", resume, system=SYSTEM), _ask(question), work, job)
                except RuntimeError as e:
                    if "conversation" not in str(e).lower() and "session" not in str(e).lower():
                        raise
                    out = None
            if out is None:
                text = material + ("\n\n<your_brief>\n" + prior[-1]["body"] + "\n</your_brief>" if question and prior else "") + "\n\n" + ask
                with open(os.path.join(work, "material.md"), "w", encoding="utf-8") as f:
                    f.write(text)
                job["phase"] = "writing"
                out = earnai.cc_stream(earnai.cc_command(tools, system=SYSTEM), text, work, job)
            if not out["text"]:
                raise RuntimeError("Claude Code returned no text")
            name = f"Claude Code · {out['model'] or 'default model'}"
            meta = {"writer": "claude-code", "session": out["session"], "web": web,
                    "usage": out["usage"], "api_equivalent_usd": out["api_usd"], "ms": out["ms"]}
            _save_note(d, question, out["text"], name, meta, None)
            job.update(state="done", phase="done", model=name, api_usd=out["api_usd"])
        else:
            import anthropic
            m = earnai.model()
            content = [{"type": "text", "text": material, "cache_control": {"type": "ephemeral"}}]
            if question and prior:
                content.append({"type": "text", "text": "<your_brief>\n" + prior[-1]["body"] + "\n</your_brief>"})
            content.append({"type": "text", "text": ask})
            kw = dict(model=m, max_tokens=8000, system=SYSTEM, messages=[{"role": "user", "content": content}],
                      thinking={"type": "adaptive"}, output_config={"effort": earnai.WRITE_EFFORT})
            betas = []
            if m in earnai.FALLBACK_OK:
                betas.append("server-side-fallback-2026-07-01")
                kw["fallbacks"] = "default"
            if web and not question:
                kw["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 4}]
            job["phase"] = "writing"
            client = earnai._client()
            usage, parts, served = {}, [], m
            for _ in range(3):
                with client.beta.messages.stream(betas=betas, **kw) as stream:
                    for ev in stream:
                        if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                            job["text"] += ev.delta.text
                    msg = stream.get_final_message()
                served = getattr(msg, "model", m) or m
                for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
                    usage[k] = usage.get(k, 0) + (getattr(msg.usage, k, 0) or 0)
                if msg.stop_reason == "refusal":
                    raise RuntimeError("the model declined this request")
                parts += [bl.text for bl in msg.content if getattr(bl, "type", "") == "text"]
                if msg.stop_reason == "pause_turn":
                    kw["messages"] = kw["messages"] + [{"role": "assistant", "content": msg.content}]
                    continue
                break
            body = "\n".join(p for p in parts if p).strip()
            if not body:
                raise RuntimeError("the model returned no text")
            c = earnai.cost(served if served in earnai.MODELS else m, usage)
            _save_note(d, question, body, served, {"writer": "api", "usage": usage, "web": web}, c)
            job.update(state="done", phase="done", model=served, cost_usd=c)
    except Exception as e:
        msg = f"{e.__class__.__name__}: {e}"
        try:
            import anthropic
            if isinstance(e, anthropic.AuthenticationError):
                msg = "the Anthropic API key was refused — check it in Settings"
        except Exception:
            pass
        job.update(state="error", error=msg[:300])
    finally:
        job["finished"] = time.time()


def start_note(d: str, question: Optional[str] = None, web: bool = False) -> dict:
    from . import earnai
    w = earnai.writer()
    if w == "api" and not earnai.api_key():
        raise PermissionError("no Anthropic API key — add one in Settings, or switch the writer to Claude Code")
    if w == "claude-code":
        st = earnai.cc_status()
        if not st["found"]:
            raise PermissionError("Claude Code was not found on this computer — install the Claude desktop app, "
                                  "or switch the writer to an API key in Settings")
        if not st["logged_in"]:
            st = earnai.cc_status(force=True)
            if not st["logged_in"]:
                raise PermissionError("Claude Code is not signed in yet — press Sign in below (a one-time step)")
    if not stored(d):
        raise LookupError("there is no brief for this day yet")
    with _LOCK:
        j = NOTE_JOBS.get(d)
        if j and j["state"] == "running":
            return _pub(j)
        j = {"state": "running", "phase": "starting", "text": "", "started": time.time(), "finished": None,
             "question": question, "error": None, "writer": w, "web": web,
             "model": (earnai.cc_model() or "default") if w == "claude-code" else earnai.model()}
        NOTE_JOBS[d] = j
    threading.Thread(target=_run_note, args=(j, d, question, web), daemon=True, name=f"alrt-note-{d}").start()
    return _pub(j)


def note_state(d: str) -> dict:
    from . import earnai
    w = earnai.writer()
    ns = notes(d)
    return {"writer": w, "writer_label": earnai.WRITERS[w], "has_key": bool(earnai.api_key()),
            "model_label": earnai.MODELS[earnai.model()][0], "cc_model_label": earnai.CC_MODELS[earnai.cc_model()],
            "cc": earnai.cc_status() if w == "claude-code" else None,
            "auto": store.get_setting("alrt_auto_claude") == "1",
            "job": _pub(NOTE_JOBS.get(d)),
            "notes": [n for n in ns if n["kind"] == "note"], "answers": [n for n in ns if n["kind"] == "answer"]}
