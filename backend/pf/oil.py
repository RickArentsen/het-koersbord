"""
OIL — the oil market on one page.

What the page answers, and where each answer comes from (every source is
keyless; aisstream.io, for worldwide live tankers, takes a free key — ais.py):

    prices       Brent, WTI and the products from Yahoo's futures (about ten
                 minutes late); Dated Brent and WTI spot from the EIA via FRED
                 (a day or two late); the OPEC basket from OPEC; Dubai (IMF)
    the curve    every Brent and WTI contract month two years out, and the same
                 curve a week, a month, three months and a year ago, rebuilt
                 from each contract's own history
    refining     crack spreads from the product futures: what a refinery earns
    supply       production by country: JODI (the governments' own monthly
                 figures), the EIA's Short-Term Energy Outlook (OPEC+ member by
                 member, capacity, spare capacity, outages, the world balance)
    flows        IMF PortWatch: tankers through 28 chokepoints a day, and the
                 tanker cargo loaded per port and per country, from satellite AIS
    stocks       the EIA's Weekly Petroleum Status Report
    OPEC+        OPEC's own press releases, read for decisions and the next
                 meeting; required production typed in from the latest table
    who trades   CFTC and ICE Commitments of Traders; UN Comtrade crude imports
                 by supplier
    pump prices  the EU Weekly Oil Bulletin (all 27 members, with and without
                 tax), the UK's weekly road fuel prices, the EIA's US retail

Pitfalls handled here rather than rediscovered:

  * BZ=F rolls at the end of each month and prints a jump that never traded.
    Day changes are taken on ONE contract month, and the curve drops a month
    once its exchange expiry has passed: Yahoo keeps quoting an expired
    contract at its final settlement for weeks.
  * PortWatch's "tanker" is every tanker — crude, products, chemicals, gas.
    Qatar's figures are mostly LNG. Barrels are an approximation (7.33 per
    tonne, the factor for crude) and the page says so.
  * JODI writes "-" for a figure nobody reported. Iran, Iraq and the UAE report
    nothing, so the EIA fills them.
  * ArcGIS answers a thousand rows at a time, aggregated queries included, so
    every PortWatch read pages through resultOffset.
"""

from __future__ import annotations

import calendar
import csv
import html as _html
import io
import json
import logging
import math
import re
import statistics
import threading
import time
import urllib.parse
from bisect import bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from . import store, net
from .bonds import (Ser, _cached, _pack, _unpack, _downsample, _fred, _closes, _yf,
                    _pctile, _highest_since, _ord, _f, _r)

BBL_PER_T = 7.33                 # barrels in a tonne of crude (the usual factor)
L_PER_BBL = 158.987294928
GAL_PER_BBL = 42.0
L_PER_GAL = 3.785411784

TTL = {
    "live": 60, "curve": 900, "fut": 3600, "fred": 6 * 3600, "orb": 6 * 3600,
    "eia_w": 3 * 3600, "steo": 24 * 3600, "jodi": 3 * 86400, "pw": 8 * 3600,
    "pw_db": 30 * 86400, "cot": 8 * 3600, "comtrade": 30 * 86400, "pump": 12 * 3600,
    "opec": 3 * 3600, "eq": 3600, "news": 1800,
}

SEED_PATH = Path(__file__).resolve().parent / "oil_seed.json"
LAND_PATH = Path(__file__).resolve().parent.parent / "static" / "land.json"


def seed() -> dict:
    try:
        return json.loads(SEED_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


_ISO_CACHE: dict = {}


def countries() -> dict:
    """ISO-2 → {name, iso3, n3} from the map file, and the reverse lookups."""
    if _ISO_CACHE:
        return _ISO_CACHE
    try:
        land = json.loads(LAND_PATH.read_text(encoding="utf-8"))
    except Exception:
        land = {"c": []}
    by2, by3, byn = {}, {}, {}
    for c in land.get("c", []):
        if not c.get("iso"):
            continue
        by2[c["iso"]] = {"name": c["name"], "iso3": c.get("iso3"), "n3": c.get("n3")}
        if c.get("iso3"):
            by3[c["iso3"]] = c["iso"]
        if c.get("n3"):
            byn[str(int(c["n3"]))] = c["iso"]
    # Names the sources spell their own way.
    for k, v in {"US": "United States", "GB": "United Kingdom", "RU": "Russia", "IR": "Iran",
                 "KR": "South Korea", "AE": "UAE", "CG": "Congo", "CD": "DR Congo",
                 "TW": "Taiwan", "VE": "Venezuela", "SY": "Syria", "BO": "Bolivia"}.items():
        if k in by2:
            by2[k]["name"] = v
    # UN Comtrade's own numeric codes where they differ from ISO 3166.
    byn.update({"842": "US", "251": "FR", "699": "IN", "757": "CH", "579": "NO", "490": "TW",
                "381": "IT", "711": "ZA"})
    _ISO_CACHE.update({"by2": by2, "by3": by3, "byn": byn})
    return _ISO_CACHE


def cname(iso2: Optional[str]) -> str:
    if not iso2:
        return "?"
    return (countries()["by2"].get(iso2) or {}).get("name") or iso2


def _iso(d) -> str:
    return d if isinstance(d, str) else d.isoformat()


def _quiet_yf():
    """yfinance logs every missing contract month as an error; they are expected."""
    lg = logging.getLogger("yfinance")
    old = lg.level
    lg.setLevel(logging.CRITICAL)
    return lg, old


def _download_all(syms: list[str], **kw) -> dict[str, dict[str, float]]:
    """Daily closes per symbol, unadjusted (futures and FX have nothing to adjust)."""
    if not syms:
        return {}
    lg, old = _quiet_yf()
    try:
        data = _yf().download(syms, interval="1d", auto_adjust=False, progress=False,
                              group_by="ticker", threads=True, **kw)
    finally:
        lg.setLevel(old)
    return {s: _closes(data, s, len(syms) > 1) for s in syms}


def _pct(a, b) -> Optional[float]:
    a, b = _f(a), _f(b)
    return None if a is None or not b else (a / b - 1.0) * 100.0


def _chg(s: Ser, days: int) -> Optional[float]:
    ld, lv = s.last()
    if ld is None:
        return None
    return _pct(lv, s.on((date.fromisoformat(ld) - timedelta(days=days)).isoformat()))


def _spark(s: Ser, days: int = 90) -> list[list]:
    ld, _ = s.last()
    if ld is None:
        return []
    since = (date.fromisoformat(ld) - timedelta(days=days)).isoformat()
    return [[d, round(v, 3)] for d, v in s.items_since(since)]


def _month_lab(ym: str) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    return f"{calendar.month_abbr[m]} {y % 100:02d}"


# --------------------------------------------------------------------------
# futures: the contract months, their expiries, the curve
# --------------------------------------------------------------------------

MCODE = "FGHJKMNQUVXZ"
ROOTS = {"BZ": "Brent", "CL": "WTI"}


def contract_sym(root: str, y: int, m: int) -> str:
    return f"{root}{MCODE[m - 1]}{y % 100:02d}.NYM"


def contract_ym(sym: str) -> tuple[int, int]:
    m = MCODE.index(sym[2]) + 1
    return 2000 + int(sym[3:5]), m


def _bday_back(d: date, n: int) -> date:
    while n > 0:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def expiry(root: str, y: int, m: int) -> date:
    """
    The last trading day of a contract month by the exchange's rule, ignoring
    holidays (so at worst a day early or late around one).

    WTI (NYMEX CL): three business days before the 25th of the month before
    delivery, counting from the last business day before the 25th when the
    25th itself is not one. Brent (ICE, mirrored by NYMEX BZ): the last
    business day of the second month before delivery.
    """
    if root == "CL":
        py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
        d = date(py, pm, 25)
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return _bday_back(d, 3)
    py, pm = y, m - 2
    if pm <= 0:
        pm += 12
        py -= 1
    last = date(py, pm, calendar.monthrange(py, pm)[1])
    while last.weekday() >= 5:
        last -= timedelta(days=1)
    return last


MAX_STEP = 3           # months between neighbours on a curve: two missing months are a gap, three a hole
MIN_CURVE = 6          # fewer months than this is not a curve


def ym_gap(a: str, b: str) -> int:
    """Months from contract month a to b ("2026-12" to "2027-12" is 12)."""
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def ym_spread(pts: list, g: int) -> Optional[float]:
    """The front month less the month exactly g months later, when the curve has it."""
    if not pts:
        return None
    y0 = pts[0][0] if isinstance(pts[0], (list, tuple)) else pts[0]["ym"]
    p0 = pts[0][1] if isinstance(pts[0], (list, tuple)) else pts[0]["px"]
    for p in pts[1:]:
        ym, px = (p[0], p[1]) if isinstance(p, (list, tuple)) else (p["ym"], p["px"])
        k = ym_gap(y0, ym)
        if k == g:
            return round(p0 - px, 2)
        if k > g:
            break
    return None


def front_month(root: str, d: date) -> tuple[int, int]:
    """The contract month that is the front on day `d`: the first not yet expired."""
    y, m = d.year, d.month
    for _ in range(6):
        if expiry(root, y, m) >= d:
            return y, m
        y, m = (y, m + 1) if m < 12 else (y + 1, 1)
    return y, m


def _contract_months(today: Optional[date] = None, back: int = 14, ahead: int = 26) -> list[tuple[int, int]]:
    t = today or date.today()
    out = []
    for i in range(-back, ahead):
        m0 = t.month - 1 + i
        out.append((t.year + m0 // 12, m0 % 12 + 1))
    return out


def _load_curve() -> dict:
    """
    Two years of daily settlements for every Brent and WTI month from fourteen
    months back to twenty-six ahead. The expired months are what lets the
    curve of three months or a year ago be drawn from the same data.
    """
    syms = [contract_sym(r, y, m) for r in ROOTS for (y, m) in _contract_months()]
    got = _download_all(syms, period="2y")
    got = {s: v for s, v in got.items() if v}
    if sum(1 for s in got if s.startswith("BZ")) < 12:
        raise RuntimeError("Yahoo returned fewer than twelve Brent contract months")
    return {**_pack(got), "fetched_at": time.time()}


def contracts() -> dict[str, dict[str, float]]:
    return _unpack(_cached("oil:curve", TTL["curve"], _load_curve))


class Book:
    """The contract months of one root, with their expiries, ready to price any day."""

    def __init__(self, px: dict[str, dict[str, float]], root: str):
        self.root = root
        self.rows = []
        for sym, m in px.items():
            if not sym.startswith(root) or not m:
                continue
            y, mo = contract_ym(sym)
            self.rows.append((f"{y}-{mo:02d}", sym, expiry(root, y, mo), Ser(m)))
        self.rows.sort()
        self.days = sorted({d for r in self.rows for d in r[3].d})

    def latest(self) -> Optional[str]:
        return self.days[-1] if self.days else None

    def curve(self, day: str, n: int = 24) -> list[dict]:
        d = date.fromisoformat(day)
        out = []
        for ym, sym, exp, s in self.rows:
            if exp < d:
                continue
            dd, v = s.on_d(day)
            if v is None or (d - date.fromisoformat(dd)).days > 6:
                continue
            if out and ym_gap(out[-1]["ym"], ym) > MAX_STEP:
                break           # a hole of months: what follows is another stretch of the curve
            out.append({"ym": ym, "lab": _month_lab(ym), "sym": sym, "px": round(v, 2),
                        "asof": dd, "expires": exp.isoformat()})
            if len(out) >= n:
                break
        # A curve must begin with the month that WAS the front that day, and
        # run on without a hole. Yahoo keeps some expired months and drops
        # others: on a day in June 2025 it had the front month and then
        # nothing until December 2026, which drawn as a curve is a different
        # market. Say nothing rather than that.
        fy, fm = front_month(self.root, d)
        if out and out[0]["ym"] != f"{fy}-{fm:02d}":
            return []
        if n >= MIN_CURVE and len(out) < MIN_CURVE:
            return []
        return out

    def front(self, day: Optional[str] = None) -> Optional[dict]:
        day = day or self.latest()
        if not day:
            return None
        c = self.curve(day, 1)
        if not c:
            return None
        row = next(r for r in self.rows if r[1] == c[0]["sym"])
        s = row[3]
        i = bisect_right(s.d, day) - 1
        prev = s.v[i - 1] if i >= 1 else None
        return {**c[0], "prev": _r(prev, 2), "chg": _r(c[0]["px"] - prev, 2) if prev else None,
                "pct": _r(_pct(c[0]["px"], prev), 2), "prev_d": s.d[i - 1] if i >= 1 else None}


_SNAP = {"done": None}


def _store_curves(books: dict) -> None:
    """
    Keep a copy of every curve Yahoo can still price. Older days are written
    once (a settled contract's history does not change); the last week is
    rewritten, because today's settlement is not final until the evening.
    """
    latest = max((b.latest() or "") for b in books.values())
    stamp = f"{latest}:{int(time.time() // 3600)}"
    if not latest or _SNAP["done"] == stamp:
        return
    recent = (date.fromisoformat(latest) - timedelta(days=7)).isoformat()
    old, new = [], []
    for b in books.values():
        for day in b.days:
            for p in b.curve(day, 30):
                (new if day >= recent else old).append((day, b.root, p["ym"], p["px"]))
    store.init_db()
    with store.conn() as c:
        c.executemany("INSERT OR IGNORE INTO oil_curve (d, root, ym, px) VALUES (?,?,?,?)", old)
        c.executemany("INSERT OR REPLACE INTO oil_curve (d, root, ym, px) VALUES (?,?,?,?)", new)
    _SNAP["done"] = stamp


def stored_curves(root: str, since: str) -> dict[str, list[tuple[str, float]]]:
    """Every stored curve of `root` since a day: {d: [(ym, px), ...]}, months in order."""
    try:
        with store.conn() as c:
            rows = c.execute("SELECT d, ym, px FROM oil_curve WHERE root=? AND d>=? ORDER BY d, ym",
                             (root, since)).fetchall()
    except Exception:
        return {}
    out: dict = {}
    for r in rows:
        out.setdefault(r["d"], []).append((r["ym"], r["px"]))
    return out


def _snap_curve(snaps: dict, day: str, n: int = 24) -> list[dict]:
    """The stored curve on or before `day`, within six days, shaped like Book.curve's."""
    ds = [d for d in snaps if d <= day]
    if not ds:
        return []
    d = max(ds)
    if (date.fromisoformat(day) - date.fromisoformat(d)).days > 6:
        return []
    return [{"ym": ym, "lab": _month_lab(ym), "px": round(px, 2), "asof": d} for ym, px in snaps[d][:n]]


def spreads(snaps: dict, gaps=(1, 6, 12)) -> dict:
    """Front month minus the month `g` months later, for every stored day that has both."""
    out = {g: [] for g in gaps}
    for d in sorted(snaps):
        for g in gaps:
            v = ym_spread(snaps[d], g)
            if v is not None:
                out[g].append([d, v])
    return out


def part_curve() -> dict:
    px = contracts()
    books = {root: Book(px, root) for root in ROOTS}
    try:
        _store_curves(books)
    except Exception as e:                       # a locked database is not a reason to show nothing
        logging.getLogger(__name__).warning("oil curve snapshot: %s", e)
    out = {"roots": {}, "fetched_at": time.time()}
    for root, name in ROOTS.items():
        b = books[root]
        last = b.latest()
        if not last:
            continue
        ld = date.fromisoformat(last)
        snaps = stored_curves(root, (ld - timedelta(days=400)).isoformat())
        curves = []
        for lab, days in (("Today", 0), ("1 week ago", 7), ("1 month ago", 30),
                          ("3 months ago", 91), ("1 year ago", 365)):
            day = (ld - timedelta(days=days)).isoformat()
            c = b.curve(day) or _snap_curve(snaps, day)
            if c:
                curves.append({"label": lab, "day": c[0]["asof"] if days else last, "pts": c})
        now = curves[0]["pts"] if curves else []
        sp = spreads({d: v for d, v in snaps.items() if d >= (ld - timedelta(days=372)).isoformat()})

        m13 = ym_spread(now, 12)
        out["roots"][root] = {
            "name": name, "asof": last, "curves": curves, "front": b.front(),
            "m1m2": ym_spread(now, 1), "m1m7": ym_spread(now, 6), "m1m13": m13,
            "m1m13_pct": _r(m13 / now[0]["px"] * 100, 1) if m13 is not None else None,
            "hist": {"m1m2": sp[1], "m1m7": sp[6], "m1m13": sp[12]},
            "kept_from": min(snaps) if snaps else None,
        }
    out["insights"] = _curve_insights(out)
    return out


def _structure(v: Optional[float]) -> str:
    if v is None:
        return "–"
    return "backwardation" if v > 0.25 else "contango" if v < -0.25 else "flat"


def _curve_insights(p: dict) -> list[dict]:
    ins = []
    bz = p["roots"].get("BZ")
    if bz and bz["m1m13"] is not None:
        now = bz["curves"][0]["pts"]
        later = next(q for q in now if ym_gap(now[0]["ym"], q["ym"]) == 12)
        txt = (f"Brent's curve is in {_structure(bz['m1m13'])}: {now[0]['lab']} ${now[0]['px']:.2f} against "
               f"{later['lab']} ${later['px']:.2f}, {abs(bz['m1m13']):.2f} dollars "
               f"({abs(bz['m1m13_pct'] or 0):.0f}%) over twelve months")
        h = bz["hist"]["m1m13"]
        if len(h) > 200:
            then = h[0][1]
            txt += f"; a year ago the same spread was {then:+.2f}"
        ins.append({"tone": "warn" if (bz["m1m13_pct"] or 0) > 10 else "info", "sec": "curve", "text": txt + "."})
        if bz["m1m2"] is not None and abs(bz["m1m2"]) >= 1.5:
            ins.append({"tone": "warn", "sec": "curve",
                        "text": f"The next month alone is {abs(bz['m1m2']):.2f} dollars "
                                f"{'below' if bz['m1m2'] > 0 else 'above'} the front: "
                                f"{'oil for delivery now is scarce' if bz['m1m2'] > 0 else 'there is oil to store'}."})
    cl = p["roots"].get("CL")
    if bz and cl and bz.get("front") and cl.get("front"):
        d = cl["front"]["px"] - bz["front"]["px"]
        ins.append({"tone": "info", "sec": "curve",
                    "text": f"WTI trades {abs(d):.2f} dollars {'below' if d < 0 else 'above'} Brent "
                            f"({cl['front']['lab']} against {bz['front']['lab']})."})
    return ins


# --------------------------------------------------------------------------
# continuous futures, spot prices, the OPEC basket
# --------------------------------------------------------------------------

FRONT = [("BZ=F", "Brent"), ("CL=F", "WTI"), ("HO=F", "ULSD (diesel)"), ("RB=F", "RBOB (gasoline)"),
         ("NG=F", "US natural gas"), ("TTF=F", "Dutch TTF gas"), ("^OVX", "Oil volatility (OVX)"),
         ("EURUSD=X", "EUR/USD"), ("EURGBP=X", "EUR/GBP")]


def _load_fut() -> dict:
    """Five years of the continuous front months: the crack spreads and the sparklines."""
    got = _download_all([s for s, _ in FRONT], period="5y")
    if len(got.get("BZ=F") or {}) < 500:
        raise RuntimeError("Yahoo returned no Brent history")
    return {**_pack(got), "fetched_at": time.time()}


def fut_series() -> dict[str, dict[str, float]]:
    return _unpack(_cached("oil:fut", TTL["fut"], _load_fut))


FRED_DAILY = ["DCOILBRENTEU", "DCOILWTICO", "DEXUSEU"]
FRED_WEEKLY = ["GASREGW", "GASDESW"]
FRED_MONTHLY = ["POILDUBUSDM", "POILBREUSDM", "POILWTIUSDM", "CPIAUCSL"]


def _load_fred_oil() -> dict:
    d, m1 = _fred(FRED_DAILY, "1986-01-01")
    w, m2 = _fred(FRED_WEEKLY, "1990-08-01")
    m, m3 = _fred(FRED_MONTHLY, "1980-01-01")
    got = {**d, **w, **m}
    if len(got.get("DCOILBRENTEU") or {}) < 1000:
        raise RuntimeError("FRED returned no Brent spot history")
    return {**_pack(got), "missing": m1 + m2 + m3, "fetched_at": time.time()}


def fred_series() -> dict[str, dict[str, float]]:
    return _unpack(_cached("oil:fred", TTL["fred"], _load_fred_oil))


ORB_URL = "https://www.opec.org/basket/basketDayArchives.xml"


def parse_orb(xml: str) -> dict[str, float]:
    return {d: float(v) for d, v in re.findall(r'data="(\d{4}-\d{2}-\d{2})"\s+val="([\d.]+)"', xml)}


def _load_orb() -> dict:
    orb = parse_orb(net.get(ORB_URL, timeout=40).decode("utf-8", "replace"))
    if len(orb) < 1000:
        raise RuntimeError("OPEC's basket file had fewer than a thousand days")
    return {"orb": orb, "fetched_at": time.time()}


def orb_series() -> dict[str, float]:
    return _cached("oil:orb", TTL["orb"], _load_orb).get("orb") or {}


LIVE_SYMS = ["BZ=F", "CL=F", "HO=F", "RB=F", "TTF=F", "^OVX", "EURUSD=X"]


def _load_live() -> dict:
    """
    Today's quotes. The front CONTRACTS are fetched next to the continuous
    symbols, so the day's change is measured on one contract and a roll cannot
    print a move that never traded.
    """
    px = contracts()
    fronts = {}
    for root in ROOTS:
        f = Book(px, root).front()
        if f:
            fronts[root] = f["sym"]
    got = _download_all(LIVE_SYMS + list(fronts.values()), period="5d")
    q = {}
    for s, m in got.items():
        ds = sorted(m)
        if not ds:
            continue
        q[s] = {"last": m[ds[-1]], "date": ds[-1], "prev": m[ds[-2]] if len(ds) > 1 else None,
                "prev_d": ds[-2] if len(ds) > 1 else None}
    return {"q": q, "fronts": fronts, "fetched_at": time.time()}


def live() -> dict:
    return _cached("oil:live", TTL["live"], _load_live)


def _tile(k, name, last, prev=None, unit="$/bbl", asof=None, sub="", d=2, pct=True, **extra) -> dict:
    """One header tile. A spread or a difference gets no percentage: it can cross zero."""
    return {"k": k, "name": name, "last": _r(last, d), "prev": _r(prev, d),
            "chg": _r(last - prev, d) if last is not None and prev is not None else None,
            "pct": _r(_pct(last, prev), 2) if pct else None, "unit": unit, "asof": asof, "sub": sub, **extra}


def part_prices() -> dict:
    L = live()
    q, fronts = L.get("q") or {}, L.get("fronts") or {}
    fs = fred_series()
    orb = Ser(orb_series())
    fut = fut_series()
    tiles = []

    def qt(sym):
        return q.get(sym) or {}

    bz = qt(fronts.get("BZ", "")) or qt("BZ=F")
    cl = qt(fronts.get("CL", "")) or qt("CL=F")
    bz_lab = _month_lab(f"{contract_ym(fronts['BZ'])[0]}-{contract_ym(fronts['BZ'])[1]:02d}") if fronts.get("BZ") else "front month"
    cl_lab = _month_lab(f"{contract_ym(fronts['CL'])[0]}-{contract_ym(fronts['CL'])[1]:02d}") if fronts.get("CL") else "front month"
    if bz:
        tiles.append(_tile("BRENT", "Brent", bz["last"], bz.get("prev"), asof=bz["date"], sub=f"ICE/NYMEX {bz_lab}", lead=True))
    if cl:
        tiles.append(_tile("WTI", "WTI", cl["last"], cl.get("prev"), asof=cl["date"], sub=f"NYMEX {cl_lab}"))
    dated = Ser(fs.get("DCOILBRENTEU"))
    if dated:
        dd, dv = dated.last()
        prem = None
        bzc = Ser(fut.get("BZ=F"))
        if bzc:
            fv = bzc.on(dd)
            prem = _r(dv - fv, 2) if fv else None
        tiles.append(_tile("DATED", "Dated Brent", dv, dated.v[-2] if len(dated) > 1 else None, asof=dd,
                           sub="physical North Sea, EIA" + (f" · {prem:+.2f} vs futures" if prem is not None else ""),
                           premium=prem))
    if orb:
        od, ov = orb.last()
        tiles.append(_tile("ORB", "OPEC basket", ov, orb.v[-2] if len(orb) > 1 else None, asof=od,
                           sub="average of members' crudes"))
    fx = qt("EURUSD=X")
    if bz and fx.get("last"):
        eur = bz["last"] / fx["last"]
        prev_eur = bz["prev"] / fx["prev"] if bz.get("prev") and fx.get("prev") else None
        tiles.append(_tile("EUR", "Brent in euros", eur, prev_eur, unit="€/bbl", asof=bz["date"],
                           sub=f"€{eur / L_PER_BBL:.3f} a litre of crude", per_litre=_r(eur / L_PER_BBL, 4)))
    ho, rb = qt("HO=F"), qt("RB=F")
    if ho:
        crack = ho["last"] * GAL_PER_BBL - bz["last"] if bz else None
        tiles.append(_tile("ULSD", "Diesel (ULSD)", ho["last"], ho.get("prev"), unit="$/gal", asof=ho["date"], d=3,
                           sub=f"${ho['last'] * GAL_PER_BBL:.0f}/bbl" + (f" · {crack:+.0f} over Brent" if crack is not None else ""),
                           crack=_r(crack, 2)))
    if rb:
        crack = rb["last"] * GAL_PER_BBL - bz["last"] if bz else None
        tiles.append(_tile("RBOB", "Gasoline (RBOB)", rb["last"], rb.get("prev"), unit="$/gal", asof=rb["date"], d=3,
                           sub=f"${rb['last'] * GAL_PER_BBL:.0f}/bbl" + (f" · {crack:+.0f} over Brent" if crack is not None else ""),
                           crack=_r(crack, 2)))
    if bz and cl:
        tiles.append(_tile("SPREAD", "WTI − Brent", cl["last"] - bz["last"],
                           (cl["prev"] - bz["prev"]) if cl.get("prev") and bz.get("prev") else None,
                           asof=cl["date"], sub=f"{cl_lab} against {bz_lab}", pct=False))
    ovx = qt("^OVX")
    if ovx:
        hist = Ser(fut.get("^OVX"))
        tiles.append(_tile("OVX", "Oil volatility", ovx["last"], ovx.get("prev"), unit="", asof=ovx["date"],
                           sub=f"Cboe OVX · {_ord(_pctile(hist.v, ovx['last']))} percentile of 5 years" if hist else "Cboe OVX"))
    ttf = qt("TTF=F")
    if ttf:
        tiles.append(_tile("TTF", "Dutch TTF gas", ttf["last"], ttf.get("prev"), unit="€/MWh", asof=ttf["date"],
                           sub="front month, ICE Endex"))
    spark = {}
    for k, sym in (("BRENT", "BZ=F"), ("WTI", "CL=F"), ("ULSD", "HO=F"), ("RBOB", "RB=F"), ("OVX", "^OVX"), ("TTF", "TTF=F")):
        spark[k] = _spark(Ser(fut.get(sym)), 120)
    spark["DATED"] = _spark(dated, 120)
    spark["ORB"] = _spark(orb, 120)
    out = {"tiles": tiles, "spark": spark, "fronts": fronts, "fetched_at": time.time(),
           "live_age_s": round(time.time() - (L.get("fetched_at") or time.time()))}
    out["insights"] = _price_insights(out, dated, fut)
    return out


def _price_insights(p: dict, dated: Ser, fut: dict) -> list[dict]:
    ins = []
    T = {t["k"]: t for t in p["tiles"]}
    b = T.get("BRENT")
    if b and dated:
        since = _highest_since(dated, b["last"], dated.d[-1])
        hi = since is None or since < (date.today() - timedelta(days=365)).isoformat()
        if hi:
            ins.append({"tone": "warn", "sec": "prices",
                        "text": f"Brent at ${b['last']:.2f} is the highest since "
                                f"{datetime.strptime(since, '%Y-%m-%d').strftime('%B %Y') if since else 'records began'}."})
        s = Ser(fut.get("BZ=F"))
        m1 = _chg(s, 30)
        if m1 is not None and abs(m1) >= 8:
            ins.append({"tone": "bad" if m1 > 0 else "good", "sec": "prices",
                        "text": f"Brent is {m1:+.0f}% in a month."})
    d = T.get("DATED")
    if d and d.get("premium") is not None and abs(d["premium"]) >= 3:
        ins.append({"tone": "warn", "sec": "prices",
                    "text": f"Physical (Dated) Brent is {abs(d['premium']):.2f} dollars "
                            f"{'above' if d['premium'] > 0 else 'below'} the front future: "
                            f"{'refiners are paying up for oil they can load now' if d['premium'] > 0 else 'prompt cargoes are going begging'}."})
    u = T.get("ULSD")
    if u and u.get("crack") is not None:
        ins.append({"tone": "warn" if u["crack"] > 45 else "info", "sec": "refine",
                    "text": f"Diesel trades {u['crack']:.0f} dollars a barrel over Brent."})
    o = T.get("OVX")
    if o and o.get("last") and o["last"] >= 45:
        ins.append({"tone": "warn", "sec": "prices", "text": f"The oil volatility index is {o['last']:.0f}: "
                    f"options price daily moves of about {o['last'] / 16:.1f}%."})
    return ins


# --------------------------------------------------------------------------
# history for the charts
# --------------------------------------------------------------------------

HIST_KEYS = {
    "BRENT": ("DCOILBRENTEU", "Brent (Dated)"), "WTI": ("DCOILWTICO", "WTI (Cushing)"),
    "DUBAI": ("POILDUBUSDM", "Dubai (monthly)"), "ORB": (None, "OPEC basket"),
    "GASUS": ("GASREGW", "US gasoline, retail"), "DSLUS": ("GASDESW", "US diesel, retail"),
}


def history(keys: list[str], start: str, ccy: str = "USD", real: bool = False) -> dict:
    fs = fred_series()
    fx = Ser(fs.get("DEXUSEU"))
    cpi = Ser(fs.get("CPIAUCSL"))
    cpi_last = cpi.last()[1] if cpi else None
    out, meta = {}, {}
    for k in keys:
        if k not in HIST_KEYS:
            continue
        fid, name = HIST_KEYS[k]
        m = orb_series() if k == "ORB" else (fs.get(fid) or {})
        items = [(d, v) for d, v in sorted(m.items()) if d >= start]
        if ccy == "EUR":
            items = [(d, v / r) for d, v in items if (r := fx.on(d))]
        if real and cpi_last:
            items = [(d, v * cpi_last / c) for d, v in items if (c := cpi.on(d))]
        out[k] = _downsample(items)
        meta[k] = {"name": name, "last": items[-1] if items else None}
    return {"series": out, "meta": meta, "ccy": ccy, "real": real,
            "cpi_month": cpi.last()[0] if (real and cpi) else None}


# --------------------------------------------------------------------------
# refining: crack spreads
# --------------------------------------------------------------------------

def crack_series(fut: dict) -> dict[str, dict[str, float]]:
    """
    What a refinery earns per barrel of crude, from the futures.
    3-2-1: three barrels of WTI make two of gasoline and one of diesel.
    The single cracks are against Brent, the crude Europe and Asia price from.
    """
    cl, bz, ho, rb = (fut.get(s) or {} for s in ("CL=F", "BZ=F", "HO=F", "RB=F"))
    days = sorted(set(cl) & set(ho) & set(rb))
    c321 = {d: (2 * rb[d] * GAL_PER_BBL + ho[d] * GAL_PER_BBL - 3 * cl[d]) / 3 for d in days}
    db = {d: ho[d] * GAL_PER_BBL - bz[d] for d in sorted(set(ho) & set(bz))}
    gb = {d: rb[d] * GAL_PER_BBL - bz[d] for d in sorted(set(rb) & set(bz))}
    return {"c321": c321, "diesel": db, "gasoline": gb}


CRACKS = [("diesel", "Diesel over Brent", "ULSD futures less Brent"),
          ("gasoline", "Gasoline over Brent", "RBOB futures less Brent"),
          ("c321", "3-2-1 crack (WTI)", "two barrels of gasoline and one of diesel from three of WTI")]


def _high_words(since: Optional[str], asof: str, min_days: int = 60) -> str:
    """
    ", the highest since March 2022" — but only when it is a high. A level that
    was beaten yesterday is not "the highest since yesterday".
    """
    if since is None:
        return ", a five-year high"
    if (date.fromisoformat(asof) - date.fromisoformat(since)).days < min_days:
        return ""
    return f", the highest since {datetime.strptime(since, '%Y-%m-%d').strftime('%B %Y')}"


def part_cracks() -> dict:
    fut = fut_series()
    cs = crack_series(fut)
    rows, series = [], {}
    for k, name, how in CRACKS:
        s = Ser(cs[k])
        if not s:
            continue
        ld, lv = s.last()
        t = date.fromisoformat(ld)
        five = s.since((t - timedelta(days=5 * 365)).isoformat())
        rows.append({"k": k, "name": name, "how": how, "last": _r(lv, 2), "asof": ld,
                     "w1": _r(lv - (s.on((t - timedelta(days=7)).isoformat()) or lv), 2),
                     "m1": _r(lv - (s.on((t - timedelta(days=30)).isoformat()) or lv), 2),
                     "y1": _r(lv - (s.on((t - timedelta(days=365)).isoformat()) or lv), 2),
                     "pctile": _pctile(five, lv), "avg5": _r(statistics.mean(five), 2) if five else None,
                     "since": _highest_since(s, lv, ld)})
        series[k] = _downsample(list(zip(s.d, [round(v, 2) for v in s.v])), 1200)
    out = {"rows": rows, "series": series, "fetched_at": time.time()}
    ins = []
    for r in rows:
        if r["pctile"] is not None and r["pctile"] >= 90:
            ins.append({"tone": "warn", "sec": "refine",
                        "text": f"{r['name']} is {r['last']:.0f} dollars a barrel, the {_ord(r['pctile'])} percentile "
                                f"of five years{_high_words(r['since'], r['asof'])}."})
    out["insights"] = ins
    return out


# --------------------------------------------------------------------------
# supply: the EIA's Short-Term Energy Outlook and JODI
# --------------------------------------------------------------------------

STEO_URL = "https://www.eia.gov/outlooks/steo/xls/STEO_m.xlsx"
STEO_TABS = ("2tab", "3atab", "3btab", "3ctab", "3dtab")

# STEO's country codes. Lower case are crude oil (Table 3d, OPEC+ members);
# upper case are all liquids (Table 3b, everyone else). "tc" is the UAE,
# which the EIA has listed outside OPEC since mid-2026.
STEO_ISO = {"ag": "DZ", "cf": "CG", "ek": "GQ", "gb": "GA", "IR": "IR", "iz": "IQ", "ku": "KW", "ly": "LY",
            "ni": "NG", "sa": "SA", "ve": "VE", "aj": "AZ", "ba": "BH", "bx": "BN", "kz": "KZ", "my": "MY",
            "mx": "MX", "mu": "OM", "rs": "RU", "od": "SS", "su": "SD"}
STEO_LIQ = {"CA": "CA", "MX": "MX", "US": "US", "AR": "AR", "BR": "BR", "CO": "CO", "GY": "GY", "NO": "NO",
            "UK": "GB", "AJ": "AZ", "KZ": "KZ", "RS": "RU", "MU": "OM", "QA": "QA", "tc": "AE", "AO": "AO",
            "EG": "EG", "CH": "CN", "IN": "IN", "ID": "ID", "MY": "MY"}
OPEC_CODES = ["ag", "cf", "ek", "gb", "IR", "iz", "ku", "ly", "ni", "sa", "ve"]
PLUS_CODES = ["aj", "ba", "bx", "kz", "my", "mx", "mu", "rs", "od", "su"]


def parse_steo(raw: bytes) -> dict:
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    meta = {}
    for row in wb["Dates"].iter_rows(values_only=True):
        if not row or not row[0]:
            continue
        k = str(row[0])
        vals = [v for v in row[1:] if v not in (None, "")]   # the value is not always in the next column
        if not vals:
            continue
        v = vals[0]
        if k.startswith("Forecast Month"):
            meta["edition"] = str(v)
        elif k.startswith("Table Beginning Month"):
            meta["begin"] = int(v)
        elif k.startswith("Last Historical Month"):
            meta["last_hist"] = int(v)
        elif k.startswith("Modeling and analysis completion"):
            meta["completed"] = v.date().isoformat() if hasattr(v, "date") else str(v)
    b = meta.get("begin", 202201)
    y0, m0 = b // 100, b % 100
    tabs = {}
    for tab in STEO_TABS:
        rows, labels = {}, {}
        for row in wb[tab].iter_rows(values_only=True):
            code = row[0] if row else None
            if not isinstance(code, str) or " " in code or code in rows:
                continue
            vals = {}
            for j, v in enumerate(row[2:]):
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    mm = m0 - 1 + j
                    vals[f"{y0 + mm // 12}-{mm % 12 + 1:02d}"] = float(v)
            if vals:
                rows[code] = vals
                labels[code] = str(row[1] or "").strip()
        tabs[tab] = {"rows": rows, "labels": labels}
    lh = meta.get("last_hist")
    meta["last_hist_ym"] = f"{lh // 100}-{lh % 100:02d}" if lh else None
    return {"meta": meta, "tabs": tabs}


def _load_steo() -> dict:
    p = parse_steo(net.get(STEO_URL, timeout=120))
    if len(p["tabs"]["3dtab"]["rows"]) < 20:
        raise RuntimeError("the EIA outlook workbook had no world crude table")
    return {**p, "fetched_at": time.time()}


def steo() -> dict:
    return _cached("oil:steo", TTL["steo"], _load_steo)


JODI_BASE = "https://www.jodidata.org/_resources/files/downloads/oil-data/annual-csv/primary/"
JODI_FLOWS = {"INDPROD": "prod", "TOTEXPSB": "exp", "TOTIMPSB": "imp", "REFINOBS": "runs"}


def parse_jodi(text: str, out: dict) -> int:
    """
    Crude oil only, in thousand barrels a day (and closing stocks in thousand
    barrels). A "-" is a figure nobody reported, not a zero, and is skipped.
    """
    rd = csv.reader(io.StringIO(text.lstrip("﻿")))
    hdr = next(rd, None)
    if not hdr:
        return 0
    ix = {h: i for i, h in enumerate(hdr)}
    n = 0
    for r in rd:
        if len(r) < len(hdr) or r[ix["ENERGY_PRODUCT"]] != "CRUDEOIL":
            continue
        flow, unit = r[ix["FLOW_BREAKDOWN"]], r[ix["UNIT_MEASURE"]]
        if flow in JODI_FLOWS and unit == "KBD":
            key = JODI_FLOWS[flow]
        elif flow == "CLOSTLV" and unit == "KBBL":
            key = "stock"
        else:
            continue
        try:
            v = float(r[ix["OBS_VALUE"]])
        except ValueError:
            continue
        out.setdefault(r[ix["REF_AREA"]], {}).setdefault(key, {})[r[ix["TIME_PERIOD"]]] = v
        n += 1
    return n


def _load_jodi() -> dict:
    y = date.today().year
    out, years = {}, []
    ok = lambda b: b.lstrip(b"\xef\xbb\xbf")[:8] == b"REF_AREA"
    for yr in (y, y - 1, y - 2):
        r = net.first_ok([f"{JODI_BASE}primaryyear{yr}.csv", f"{JODI_BASE}{yr}.csv"], timeout=150, accept=ok)
        if r["ok"]:
            parse_jodi(r["body"].decode("utf-8", "replace"), out)
            years.append(yr)
        this_year = {m for c in out.values() for m in c.get("prod", {}) if m.startswith(str(y))}
        if yr == y - 1 and len(this_year) >= 6:
            break                         # two files are enough once this year has half a year in it
    if not out:
        raise RuntimeError("JODI's crude files did not load")
    return {"c": out, "years": years, "fetched_at": time.time()}


def jodi() -> dict:
    return _cached("oil:jodi", TTL["jodi"], _load_jodi)


def _hist(m: dict, upto: Optional[str]) -> dict:
    return {k: v for k, v in m.items() if upto is None or k <= upto}


def _ago_m(ym: str, n: int) -> str:
    y, m = int(ym[:4]), int(ym[5:7])
    m0 = y * 12 + m - 1 - n
    return f"{m0 // 12}-{m0 % 12 + 1:02d}"


def _mrow(m: dict) -> dict:
    """Latest month, and its change on one, six and twelve months — in kb/d."""
    ks = sorted(k for k, v in m.items() if v is not None)
    if not ks:
        return {}
    last = ks[-1]
    v = m[last]
    g = lambda n: m.get(_ago_m(last, n))
    return {"month": last, "kbd": round(v), "d1": _rnd(v, g(1)), "d6": _rnd(v, g(6)), "d12": _rnd(v, g(12)),
            "pct12": _r(_pct(v, g(12)), 1), "spark": [[k, round(m[k])] for k in ks[-25:]]}


def _rnd(a, b):
    return None if a is None or b is None else round(a - b)


def production() -> dict:
    """
    One production series per country, with the source it came from:

      EIA crude    OPEC+ members and the US (Table 3d): consistent across the
                   group, and the only figures for Iran, Iraq and the UAE's peers
      JODI crude   everyone else who reports — the governments' own numbers
      EIA liquids  where neither has crude (the UAE, Qatar, Brazil): crude plus
                   condensate, NGLs and biofuels, and labelled as such
    """
    st, jd = steo(), jodi()["c"]
    lh = st["meta"].get("last_hist_ym")
    t3d, t3b = st["tabs"]["3dtab"]["rows"], st["tabs"]["3btab"]["rows"]
    rows: dict = {}
    for code in OPEC_CODES + PLUS_CODES:
        s = _hist(t3d.get("copr_" + code) or {}, lh)
        if s:
            iso = STEO_ISO[code]
            rows[iso] = {"iso": iso, "group": "OPEC" if code in OPEC_CODES else "OPEC+",
                         "series": {k: v * 1000 for k, v in s.items()}, "src": "EIA", "kind": "crude"}
    us = _hist(t3d.get("coprpus") or {}, lh)
    if us:
        rows["US"] = {"iso": "US", "group": "", "series": {k: v * 1000 for k, v in us.items()},
                      "src": "EIA", "kind": "crude"}
    by2 = countries()["by2"]
    for iso, d in jd.items():
        if iso in rows or iso not in by2:
            continue
        p = {k: v for k, v in (d.get("prod") or {}).items() if v > 0}
        if len(p) >= 3:
            rows[iso] = {"iso": iso, "group": "", "series": p, "src": "JODI", "kind": "crude"}
    for code, iso in STEO_LIQ.items():
        if iso in rows:
            continue
        s = _hist(t3b.get("papr_" + code) or {}, lh)
        if s:
            rows[iso] = {"iso": iso, "group": "", "series": {k: v * 1000 for k, v in s.items()},
                         "src": "EIA", "kind": "all liquids"}
    for iso, r in rows.items():
        r["name"] = cname(iso)
        r.update(_mrow(r["series"]))
        x = (jd.get(iso) or {}).get("exp") or {}
        if x:
            r["exports"] = _mrow({k: v for k, v in x.items() if v is not None})
    return {"rows": rows, "last_hist": lh}


def part_supply() -> dict:
    st = steo()
    lh = st["meta"].get("last_hist_ym")
    t3d, t3a = st["tabs"]["3dtab"]["rows"], st["tabs"]["3atab"]["rows"]
    pr = production()
    rows = sorted(pr["rows"].values(), key=lambda r: -(r.get("kbd") or 0))
    world_now = (t3d.get("copr_world") or {}).get(lh)
    for r in rows:
        r["share"] = _r((r.get("kbd") or 0) / (world_now * 1000) * 100, 1) if world_now and r.get("kind") == "crude" else None
        r.pop("series", None)
    def ser(code, tab=t3d, scale=1.0):
        return [[k, round(v * scale, 3), k > (lh or "")] for k, v in sorted((tab.get(code) or {}).items())]
    world = {"crude": ser("copr_world"), "opec": ser("copr_opec"), "opecplus": ser("copr_opecplus"),
             "us": ser("coprpus"), "others": ser("copr_nonopecplus_xus"),
             "liquids": ser("papr_world", t3a), "demand": ser("patc_world", t3a),
             "draw": ser("t3_stchange_world", t3a), "oecd_stocks": ser("pasc_oecd_t3", t3a)}
    out = {"rows": rows[:60], "world": world, "last_hist": lh, "edition": st["meta"].get("edition"),
           "jodi_years": jodi().get("years"), "fetched_at": time.time()}
    out["insights"] = _supply_insights(out, t3d, lh)
    return out


def _supply_insights(p: dict, t3d: dict, lh: Optional[str]) -> list[dict]:
    ins = []
    w = t3d.get("copr_world") or {}
    if lh and lh in w:
        v, y1 = w[lh], w.get(_ago_m(lh, 12))
        txt = f"World crude output was {v:.1f} mb/d in {_mlong(lh)} (EIA)"
        if y1:
            txt += f", {v - y1:+.1f} on a year earlier"
        ins.append({"tone": "bad" if y1 and v - y1 < -2 else "info", "sec": "supply", "text": txt + "."})
    big = [r for r in p["rows"] if (r.get("kbd") or 0) >= 300 and r.get("d12") is not None]
    for r in sorted(big, key=lambda r: r["d12"])[:2]:
        if r["d12"] <= -150:
            ins.append({"tone": "bad", "sec": "supply",
                        "text": f"{r['name']} produced {r['kbd']:,} kb/d in {_mlong(r['month'])}, {r['d12']:+,} on a year "
                                f"earlier ({r['src']} {r['kind']})."})
    for r in sorted(big, key=lambda r: -r["d12"])[:2]:
        if r["d12"] >= 100:
            ins.append({"tone": "good", "sec": "supply",
                        "text": f"{r['name']} produced {r['kbd']:,} kb/d in {_mlong(r['month'])}, {r['d12']:+,} on a year "
                                f"earlier ({r['src']} {r['kind']})."})
    return ins


def _mlong(ym: str) -> str:
    return f"{calendar.month_name[int(ym[5:7])]} {ym[:4]}"


# --------------------------------------------------------------------------
# flows: IMF PortWatch
# --------------------------------------------------------------------------

PW = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/{}/FeatureServer/0/query"
PW_COUNTRIES = ["SAU", "IRQ", "ARE", "KWT", "IRN", "QAT", "OMN", "BHR", "RUS", "USA", "CAN", "BRA", "GUY",
                "VEN", "MEX", "COL", "ECU", "ARG", "NOR", "GBR", "NLD", "NGA", "AGO", "LBY", "DZA", "EGY",
                "COG", "GAB", "GNQ", "TUR", "MYS", "SGP", "IND", "CHN", "KOR", "JPN"]


def _pw(svc: str, params: dict, pages: int = 60) -> list[dict]:
    """Every row of a PortWatch query, a thousand at a time."""
    rows, off = [], 0
    for _ in range(pages):
        q = {**params, "f": "json", "resultOffset": off, "resultRecordCount": 1000}
        d = json.loads(net.get(PW.format(svc) + "?" + urllib.parse.urlencode(q), timeout=90))
        if isinstance(d, dict) and d.get("error"):
            e = d["error"]
            raise RuntimeError(f"PortWatch {svc}: {e.get('message') if isinstance(e, dict) else e}")
        fs = [f["attributes"] for f in (d.get("features") or [])]
        rows += fs
        if not fs or not d.get("exceededTransferLimit"):
            break
        off += len(fs)
    return rows


def _sum_stats(*fields) -> str:
    return json.dumps([{"statisticType": "sum", "onStatisticField": f, "outStatisticFieldName": f} for f in fields])


def _load_choke() -> dict:
    db = _pw("PortWatch_chokepoints_database",
             {"where": "1=1", "outFields": "portid,portname,lat,lon,vessel_count_total,vessel_count_tanker"})
    since = (date.today() - timedelta(days=760)).isoformat()

    def one(pid):
        return _pw("Daily_Chokepoints_Data", {"where": f"portid='{pid}' AND date >= DATE '{since}'",
                                              "outFields": "date,n_tanker,capacity_tanker,n_total",
                                              "orderByFields": "date"})
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, [c["portid"] for c in db]))
    cp = {}
    for c, rows in zip(db, res):
        cp[c["portid"]] = {"name": c["portname"], "lat": c["lat"], "lon": c["lon"],
                           "n": {r["date"]: r["n_tanker"] for r in rows if r.get("n_tanker") is not None},
                           "dwt": {r["date"]: r["capacity_tanker"] for r in rows if r.get("capacity_tanker") is not None},
                           "all": {r["date"]: r["n_total"] for r in rows if r.get("n_total") is not None}}
    if not any(v["n"] for v in cp.values()):
        raise RuntimeError("PortWatch returned no chokepoint transits")
    return {"cp": cp, "fetched_at": time.time()}


def choke() -> dict:
    return _cached("oil:pw:choke", TTL["pw"], _load_choke)


def _load_country_flows() -> dict:
    since = (date.today() - timedelta(days=760)).isoformat()
    stats = _sum_stats("export_tanker", "import_tanker", "portcalls_tanker")

    def one(iso3):
        return iso3, _pw("Daily_Ports_Data", {"where": f"ISO3='{iso3}' AND date >= DATE '{since}'",
                                              "outStatistics": stats, "groupByFieldsForStatistics": "date",
                                              "orderByFields": "date"})
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(one, PW_COUNTRIES))
    out = {}
    for iso3, rows in res:
        out[iso3] = {"exp": {r["date"]: r["export_tanker"] or 0 for r in rows},
                     "imp": {r["date"]: r["import_tanker"] or 0 for r in rows},
                     "calls": {r["date"]: r["portcalls_tanker"] or 0 for r in rows}}
    if not any(v["exp"] for v in out.values()):
        raise RuntimeError("PortWatch returned no port data")
    return {"c": out, "fetched_at": time.time()}


def country_flows() -> dict:
    return _cached("oil:pw:countries", TTL["pw"], _load_country_flows)


def window(m: dict, end: str, days: int) -> Optional[float]:
    """
    Average per day over the `days` days ending on `end`. A day with no row
    counts as zero; with fewer than 60% of the days present there is no answer.
    """
    e = date.fromisoformat(end)
    ds = [(e - timedelta(days=i)).isoformat() for i in range(days)]
    have = [m[d] for d in ds if d in m]
    if len(have) < days * 0.6:
        return None
    return sum(have) / len(ds)


def flow_stats(m: dict, latest: Optional[str] = None) -> dict:
    """7- and 30-day averages, and how they compare with before and with a year ago."""
    if not m:
        return {}
    end = latest or max(m)
    e = date.fromisoformat(end)
    w7, w30 = window(m, end, 7), window(m, end, 30)
    p30 = window(m, (e - timedelta(days=30)).isoformat(), 30)
    y7 = window(m, (e - timedelta(days=364)).isoformat(), 7)
    y30 = window(m, (e - timedelta(days=364)).isoformat(), 30)
    return {"end": end, "w7": _r(w7, 1), "w30": _r(w30, 1), "p30": _r(p30, 1), "y7": _r(y7, 1), "y30": _r(y30, 1),
            "vs_p30": _r(_pct(w30, p30), 1), "vs_y7": _r(_pct(w7, y7), 1), "vs_y30": _r(_pct(w30, y30), 1)}


def weekly(m: dict, days: int = 400) -> list[list]:
    """A 7-day average every week, for the sparklines."""
    if not m:
        return []
    end = date.fromisoformat(max(m))
    out = []
    for i in range(days // 7, -1, -1):
        d = (end - timedelta(days=7 * i)).isoformat()
        v = window(m, d, 7)
        if v is not None:
            out.append([d, round(v, 1)])
    return out


def _status(pct_vs_year: Optional[float]) -> str:
    if pct_vs_year is None:
        return "–"
    r = 1 + pct_vs_year / 100
    return ("shut" if r < 0.15 else "severely reduced" if r < 0.5 else "reduced" if r < 0.85
            else "normal" if r <= 1.15 else "busier than a year ago")


def part_flows() -> dict:
    sd = seed().get("chokepoints") or {}
    C = choke()["cp"]
    latest = max((max(v["n"]) for v in C.values() if v["n"]), default=None)
    cps = []
    for pid, v in C.items():
        st = flow_stats(v["n"], latest)
        dw = flow_stats(v["dwt"], latest)
        meta = sd.get(v["name"]) or {}
        cps.append({"id": pid, "name": v["name"], "lat": v["lat"], "lon": v["lon"], **st,
                    "dwt7": dw.get("w7"), "dwt_vs_y7": dw.get("vs_y7"), "status": _status(st.get("vs_y7")),
                    "oil": bool(meta.get("oil")), "note": meta.get("note"), "geo": meta.get("geo"),
                    "spark": weekly(v["n"])})
    cps.sort(key=lambda c: (not c["oil"], -(c.get("y30") or c.get("w30") or 0)))
    F = country_flows()["c"]
    by3 = countries()["by3"]
    ctry = []
    for iso3, v in F.items():
        ex, im = flow_stats(v["exp"]), flow_stats(v["imp"])
        if not ex:
            continue
        iso = by3.get(iso3)
        ctry.append({"iso3": iso3, "iso": iso, "name": cname(iso) if iso else iso3,
                     "exp": ex, "imp": im,
                     "exp_kbd7": _r((ex.get("w7") or 0) * BBL_PER_T / 1000, 0),
                     "exp_kbd30": _r((ex.get("w30") or 0) * BBL_PER_T / 1000, 0),
                     "imp_kbd30": _r((im.get("w30") or 0) * BBL_PER_T / 1000, 0),
                     "spark": weekly(v["exp"]), "spark_imp": weekly(v["imp"])})
    ctry.sort(key=lambda c: -(c["exp"].get("y30") or c["exp"].get("w30") or 0))
    out = {"chokepoints": cps, "countries": ctry, "latest": latest,
           "pw_latest": max((c["exp"]["end"] for c in ctry), default=None),
           "bbl_per_t": BBL_PER_T, "fetched_at": time.time()}
    out["insights"] = _flow_insights(out)
    return out


def _flow_insights(p: dict) -> list[dict]:
    ins = []
    for c in p["chokepoints"]:
        if not c["oil"] or c.get("vs_y7") is None or c.get("y7") is None or c["y7"] < 3:
            continue
        if c["vs_y7"] <= -40 or c["vs_y7"] >= 40:
            ins.append({"tone": "bad" if c["vs_y7"] < 0 else "warn", "sec": "flows",
                        "text": f"{c['name']}: {c['w7']:.1f} tankers a day in the week to {c['end'][8:10]}/{c['end'][5:7]}, "
                                f"against {c['y7']:.1f} in the same week last year ({c['vs_y7']:+.0f}%)."})
    movers = [c for c in p["countries"] if c["exp"].get("vs_p30") is not None and (c["exp_kbd30"] or 0) >= 300]
    for c in sorted(movers, key=lambda c: -abs(c["exp"]["vs_p30"]))[:3]:
        if abs(c["exp"]["vs_p30"]) >= 12:
            ins.append({"tone": "good" if c["exp"]["vs_p30"] > 0 else "bad", "sec": "flows",
                        "text": f"{c['name']} loaded about {c['exp_kbd30']:,.0f} kb/d onto tankers in the last 30 days, "
                                f"{c['exp']['vs_p30']:+.0f}% on the 30 days before (PortWatch, all tanker cargo)."})
    return ins


# ---- ports --------------------------------------------------------------

def _load_ports_db() -> dict:
    rows = _pw("PortWatch_ports_database",
               {"where": "vessel_count_tanker >= 40",
                "outFields": "portid,portname,country,ISO3,lat,lon,vessel_count_tanker,vessel_count_total,"
                             "industry_top1,share_country_maritime_export"})
    if len(rows) < 100:
        raise RuntimeError("PortWatch's port list came back short")
    return {"ports": {r["portid"]: r for r in rows}, "fetched_at": time.time()}


def ports_db() -> dict:
    return _cached("oil:pw:ports", TTL["pw_db"], _load_ports_db).get("ports") or {}


def _load_port_month() -> dict:
    """Tanker cargo per port over the last 30 days of data: the map's bubbles."""
    end = date.today()
    since = (end - timedelta(days=40)).isoformat()
    rows = _pw("Daily_Ports_Data", {"where": f"date >= DATE '{since}' AND portcalls_tanker > 0",
                                    "outStatistics": _sum_stats("export_tanker", "import_tanker", "portcalls_tanker"),
                                    "groupByFieldsForStatistics": "portid"})
    last = _pw("Daily_Ports_Data", {"where": f"date >= DATE '{since}'", "outFields": "date",
                                    "orderByFields": "date DESC", "resultRecordCount": 1}, pages=1)
    return {"ports": {r["portid"]: [r["export_tanker"] or 0, r["import_tanker"] or 0, r["portcalls_tanker"] or 0]
                      for r in rows},
            "since": since, "latest": last[0]["date"] if last else None, "fetched_at": time.time()}


def port_month() -> dict:
    return _cached("oil:pw:portmonth", TTL["pw"], _load_port_month)


def _load_country_ports(iso3: str) -> dict:
    since = (date.today() - timedelta(days=430)).replace(day=1).isoformat()
    rows = _pw("Daily_Ports_Data", {"where": f"ISO3='{iso3}' AND date >= DATE '{since}'",
                                    "outStatistics": _sum_stats("export_tanker", "import_tanker", "portcalls_tanker"),
                                    "groupByFieldsForStatistics": "portid,portname,year,month"})
    by: dict = {}
    for r in rows:
        p = by.setdefault(r["portid"], {"name": r["portname"], "m": {}})
        p["m"][f"{r['year']}-{r['month']:02d}"] = [r["export_tanker"] or 0, r["import_tanker"] or 0,
                                                   r["portcalls_tanker"] or 0]
    return {"ports": by, "since": since, "fetched_at": time.time()}


def country_ports(iso3: str) -> dict:
    iso3 = re.sub(r"[^A-Z]", "", iso3.upper())[:3]
    return _cached(f"oil:pw:cports:{iso3}", TTL["pw"], lambda: _load_country_ports(iso3))


def country(iso2: str) -> dict:
    """One producer in depth: output, exports, tanker loadings by port, its OPEC+ target, its buyers."""
    iso2 = re.sub(r"[^A-Z]", "", iso2.upper())[:2]
    info = countries()["by2"].get(iso2) or {}
    iso3 = info.get("iso3")
    pr = production()["rows"].get(iso2) or {}
    jd = (jodi()["c"].get(iso2) or {})
    out = {"iso": iso2, "iso3": iso3, "name": cname(iso2), "group": pr.get("group"),
           "production": {"src": pr.get("src"), "kind": pr.get("kind"),
                          "series": sorted([k, round(v)] for k, v in (pr.get("series") or {}).items())[-48:]},
           "jodi": {k: sorted([m, round(v)] for m, v in (jd.get(k) or {}).items())[-36:]
                    for k in ("prod", "exp", "imp", "runs", "stock")}}
    tg = (seed().get("opec_targets") or {})
    if iso2 in (tg.get("kbd") or {}):
        out["target"] = {"kbd": tg["kbd"][iso2], "month": tg.get("month"), "url": tg.get("url")}
    if iso3:
        F = (country_flows()["c"].get(iso3) if iso3 in PW_COUNTRIES else None)
        if F:
            out["tankers"] = {"exp": flow_stats(F["exp"]), "imp": flow_stats(F["imp"]),
                              "spark": weekly(F["exp"], 760), "spark_imp": weekly(F["imp"], 760)}
        try:
            cp = country_ports(iso3)
            db = ports_db()
            ports = []
            for pid, p in cp["ports"].items():
                ms = sorted(p["m"])
                last3 = ms[-3:]
                tot = sum(p["m"][m][0] for m in last3)
                ports.append({"id": pid, "name": p["name"], "lat": (db.get(pid) or {}).get("lat"),
                              "lon": (db.get(pid) or {}).get("lon"),
                              "months": [[m, round(p["m"][m][0]), round(p["m"][m][1]), p["m"][m][2]] for m in ms],
                              "exp3": round(tot)})
            ports.sort(key=lambda x: -x["exp3"])
            out["ports"] = [x for x in ports if x["exp3"] > 0 or any(m[2] for m in x["months"])][:14]
        except Exception as e:
            out["ports_error"] = f"{e.__class__.__name__}: {e}"[:200]
    try:
        out["buyers"] = buyers_of(iso2)
    except Exception as e:
        out["buyers_error"] = f"{e.__class__.__name__}: {e}"[:200]
    return out


# --------------------------------------------------------------------------
# stocks: the EIA's Weekly Petroleum Status Report
# --------------------------------------------------------------------------

DNAV = "https://www.eia.gov/dnav/pet/hist_xls/{}w.xls"
WPSR4 = "https://ir.eia.gov/wpsr/table4.csv"
WEEKLY = [  # key, EIA series, label, unit as published, scale to the unit shown
    ("crude", "WCESTUS1", "Commercial crude", "mb", 1e-3),
    ("cushing", "W_EPC0_SAX_YCUOK_MBBL", "Cushing, Oklahoma", "mb", 1e-3),
    ("spr", "WCSSTUS1", "Strategic reserve (SPR)", "mb", 1e-3),
    ("gasoline", "WGTSTUS1", "Gasoline", "mb", 1e-3),
    ("distillate", "WDISTUS1", "Distillates (diesel, heating oil)", "mb", 1e-3),
    ("jet", "WKJSTUS1", "Jet fuel", "mb", 1e-3),
    ("prod", "WCRFPUS2", "US crude production", "kb/d", 1.0),
    ("util", "WPULEUS3", "Refinery utilisation", "%", 1.0),
    ("imports", "WCRIMUS2", "Crude imports", "kb/d", 1.0),
    ("exports", "WCREXUS2", "Crude exports", "kb/d", 1.0),
    ("supplied", "WRPUPUS2", "Products supplied (demand)", "kb/d", 1.0),
]


def parse_dnav(raw: bytes) -> dict[str, float]:
    import xlrd
    wb = xlrd.open_workbook(file_contents=raw)
    sh = wb.sheet_by_name("Data 1")
    out = {}
    for i in range(3, sh.nrows):
        d, v = sh.row_values(i)[:2]
        if isinstance(d, float) and isinstance(v, float):
            out[xlrd.xldate_as_datetime(d, wb.datemode).date().isoformat()] = v
    return out


def _load_eia_weekly() -> dict:
    def one(item):
        k, sid = item[0], item[1]
        try:
            return k, parse_dnav(net.get(DNAV.format(sid), timeout=60)), None
        except Exception as e:
            return k, {}, f"{e.__class__.__name__}: {e}"[:160]
    with ThreadPoolExecutor(6) as ex:
        res = list(ex.map(one, WEEKLY))
    got = {k: m for k, m, _ in res if m}
    errs = {k: e for k, _, e in res if e}
    if len(got.get("crude") or {}) < 500:
        raise RuntimeError("the EIA's weekly crude stocks did not load")
    # The WPSR table lands at 10:30 New York on release day; the history files
    # can trail it by an hour. Take the newer week from the table when it is.
    try:
        rows = list(csv.reader(io.StringIO(net.get(WPSR4, timeout=40).decode("latin-1"))))
        wk = datetime.strptime(rows[0][1], "%m/%d/%y").date().isoformat()
        names = {"Commercial (Excluding SPR)": "crude", "Cushing": "cushing", "SPR": "spr",
                 "Total Motor Gasoline": "gasoline", "Distillate Fuel Oil": "distillate",
                 "Kerosene-Type Jet Fuel": "jet"}
        for r in rows[1:]:
            k = names.get((r[0] or "").strip())
            if k and k in got and wk not in got[k]:
                got[k][wk] = float(r[1].replace(",", "")) * 1000     # the table is in million barrels
    except Exception as e:
        errs["wpsr"] = f"{e.__class__.__name__}: {e}"[:160]
    return {**_pack(got), "errors": errs, "fetched_at": time.time()}


def eia_weekly() -> dict[str, dict[str, float]]:
    return _unpack(_cached("oil:eiaw", TTL["eia_w"], _load_eia_weekly))


def seasonal(m: dict, years: int = 5) -> dict:
    """
    This year and last against the range of the five years before, week of
    year by week of year — the way the market reads US stocks.
    """
    if not m:
        return {}
    last = max(m)
    y = int(last[:4])
    by: dict = {}
    for d, v in m.items():
        yr, wk = date.fromisoformat(d).isocalendar()[:2]
        by.setdefault(yr, {})[min(wk, 52)] = v
    band = []
    for wk in range(1, 53):
        vals = [by[yy][wk] for yy in range(y - years, y) if wk in by.get(yy, {})]
        if len(vals) >= 3:
            band.append([wk, min(vals), statistics.mean(vals), max(vals)])
    return {"this": sorted(by.get(y, {}).items()), "last": sorted(by.get(y - 1, {}).items()),
            "band": band, "years": f"{y - years}–{y - 1}", "year": y}


def part_stocks() -> dict:
    W = eia_weekly()
    rows, seas = [], {}
    for k, sid, label, unit, sc in WEEKLY:
        s = Ser(W.get(k))
        if not s:
            continue
        ld, lv = s.last()
        t = date.fromisoformat(ld)
        prev = s.v[-2] if len(s) > 1 else None
        y1 = s.on((t - timedelta(days=364)).isoformat())
        wk = t.isocalendar()[1]
        five = [s.on((t - timedelta(days=364 * i)).isoformat()) for i in range(1, 6)]
        five = [v for v in five if v is not None]
        avg5 = statistics.mean(five) if len(five) >= 3 else None
        rows.append({"k": k, "label": label, "unit": unit, "asof": ld, "last": _r(lv * sc, 2),
                     "w1": _r((lv - prev) * sc, 2) if prev is not None else None,
                     "y1": _r((lv - y1) * sc, 2) if y1 is not None else None,
                     "vs5": _r((lv - avg5) * sc, 2) if avg5 is not None else None,
                     "vs5_pct": _r(_pct(lv, avg5), 1), "week": wk,
                     "spark": [[d, round(v * sc, 2)] for d, v in s.items_since((t - timedelta(days=730)).isoformat())]})
        if unit == "mb" or k in ("prod", "util"):
            sz = seasonal(W.get(k))
            seas[k] = {"this": [[w, round(v * sc, 2)] for w, v in sz.get("this", [])],
                       "last": [[w, round(v * sc, 2)] for w, v in sz.get("last", [])],
                       "band": [[w, round(a * sc, 2), round(b * sc, 2), round(c * sc, 2)] for w, a, b, c in sz.get("band", [])],
                       "years": sz.get("years"), "year": sz.get("year")}
    out = {"rows": rows, "seasonal": seas, "fetched_at": time.time(), "next": next_wpsr()}
    out["insights"] = _stock_insights(rows)
    return out


def next_wpsr(now: Optional[datetime] = None) -> str:
    """
    The next Weekly Petroleum Status Report: Wednesdays at 10:30 New York,
    16:30 in Amsterdam. A holiday week moves it a day later; this does not
    know the holidays.
    """
    now = now or datetime.now()
    d = now.date()
    days = (2 - d.weekday()) % 7
    if days == 0 and now.hour * 60 + now.minute >= 16 * 60 + 30:
        days = 7
    return (d + timedelta(days=days)).isoformat()


def _stock_insights(rows: list[dict]) -> list[dict]:
    ins = []
    R = {r["k"]: r for r in rows}
    c = R.get("crude")
    if c and c.get("w1") is not None:
        txt = (f"US commercial crude stocks {'rose' if c['w1'] > 0 else 'fell'} {abs(c['w1']):.1f} mb in the week "
               f"to {c['asof'][8:10]}/{c['asof'][5:7]}, to {c['last']:.1f} mb")
        if c.get("vs5_pct") is not None:
            txt += f", {abs(c['vs5_pct']):.0f}% {'above' if c['vs5_pct'] > 0 else 'below'} the five-year average for the week"
        ins.append({"tone": "info", "sec": "stocks", "text": txt + "."})
    s = R.get("spr")
    if s and s.get("y1") is not None and abs(s["y1"]) >= 10:
        ins.append({"tone": "warn" if s["y1"] < 0 else "info", "sec": "stocks",
                    "text": f"The Strategic Petroleum Reserve holds {s['last']:.0f} mb, {s['y1']:+.0f} mb on a year ago."})
    d = R.get("distillate")
    if d and d.get("vs5_pct") is not None and d["vs5_pct"] <= -10:
        ins.append({"tone": "bad", "sec": "stocks",
                    "text": f"US distillate stocks are {abs(d['vs5_pct']):.0f}% below the five-year average for the week "
                            f"({d['last']:.1f} mb)."})
    return ins


# --------------------------------------------------------------------------
# OPEC+: the press releases, the members, the basket
# --------------------------------------------------------------------------

OPEC_PR = "https://www.opec.org/press-releases.html"
OPEC_PR_ONE = "https://www.opec.org/pr-detail/{}.html"
_MONTHS = {m.lower(): i for i, m in enumerate(calendar.month_name) if m}


def _slug_date(slug: str) -> Optional[str]:
    m = re.match(r"^\d+-(\d{1,2})-([a-z]+)-(\d{4})$", slug)
    if not m or m.group(2) not in _MONTHS:
        return None
    return date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1))).isoformat()


def _text_date(s: str) -> Optional[str]:
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", s.strip())
    if not m or m.group(2).lower() not in _MONTHS:
        return None
    return date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1))).isoformat()


def parse_release(slug: str, body_html: str) -> dict:
    """What one OPEC press release says: who met, what they decided, when they meet next."""
    b = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body_html, flags=re.S)
    paras = [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", p))).strip()
             for p in re.findall(r"<p[^>]*>(.*?)</p>", b, re.S)]
    paras = [p for p in paras if len(p) > 40]
    text = " ".join(paras)
    kind = "other"
    if re.search(r"Joint Ministerial Monitoring Committee", text):
        kind = "JMMC"
    elif re.search(r"OPEC\+ countries.*?voluntary adjustments", text):
        kind = "voluntary"
    elif re.search(r"Ministerial Meeting|Declaration of Cooperation \(DoC\)", text) and "decided" in text:
        kind = "ministerial"
    members = []
    m = re.search(r"namely ([A-Z][A-Za-z ,]+?) met", text) or re.search(r"comprising ([A-Z][A-Za-z ,]+?) holds", text)
    if m:
        members = [re.sub(r"^and\s+", "", x.strip()) for x in re.split(r",\s*|\s+and\s+", m.group(1))]
        members = [x for x in members if x]
    n = re.search(r"\b(seven|eight|nine|ten|six|five)\s+OPEC\+\s+countries", text)
    decision = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text)
                if re.search(r"\b(decided|agreed|implement a production adjustment|maintain|reaffirm)\b", s)][:3]
    nxt = re.search(r"next meeting will be held on (\d{1,2} [A-Z][a-z]+ \d{4})", text) or \
        re.search(r"will meet on (\d{1,2} [A-Z][a-z]+ \d{4})", text)
    adj = re.search(r"production adjustment of ([\d,]+) thousand barrels per day", text)
    return {"slug": slug, "date": _slug_date(slug), "kind": kind, "group_size": n.group(1) if n else None,
            "members": members, "lead": paras[0][:400] if paras else "", "decision": decision,
            "next": _text_date(nxt.group(1)) if nxt else None,
            "adjust_kbd": int(adj.group(1).replace(",", "")) if adj else None,
            "has_table": "table below" in text or "table above" in text,
            "url": OPEC_PR_ONE.format(slug)}


def _load_opec() -> dict:
    page = net.get(OPEC_PR, timeout=40).decode("utf-8", "replace")
    slugs = []
    for s in re.findall(r"pr-detail/(\d+-[\w-]+?)\.html", page):
        if s not in slugs:
            slugs.append(s)
    slugs = sorted(slugs, key=lambda s: int(s.split("-")[0]), reverse=True)[:16]

    def one(slug):
        try:
            return parse_release(slug, net.get(OPEC_PR_ONE.format(slug), timeout=40).decode("utf-8", "replace"))
        except Exception as e:
            return {"slug": slug, "date": _slug_date(slug), "error": f"{e.__class__.__name__}: {e}"[:120]}
    with ThreadPoolExecutor(6) as ex:
        rel = list(ex.map(one, slugs))
    if not rel:
        raise RuntimeError("OPEC's press release list was empty")
    return {"releases": rel, "fetched_at": time.time()}


def opec_releases() -> list[dict]:
    return _cached("oil:opec", TTL["opec"], _load_opec).get("releases") or []


def targets_newer(rel: list[dict], sd: dict) -> Optional[dict]:
    """
    The newest release of the voluntary group that carries a required-production
    table, when it is newer than the one typed into oil_seed.json — else None.
    OPEC publishes the table as an image, so the page cannot read it; it can
    only say that the copy it holds has been overtaken.
    """
    vol = [r for r in rel if r.get("kind") == "voluntary" and r.get("has_table")]
    if not vol or not sd.get("release"):
        return None
    last = max(vol, key=lambda r: (r.get("date") or "", int(r["slug"].split("-")[0])))
    if last["slug"] != sd["release"] and (last.get("date") or "") > (sd.get("decided") or ""):
        return last
    return None


def part_opec() -> dict:
    rel = opec_releases()
    sd = seed().get("opec_targets") or {}
    st = steo()
    lh = st["meta"].get("last_hist_ym")
    t3d = st["tabs"]["3dtab"]["rows"]
    today = date.today().isoformat()
    vol = [r for r in rel if r.get("kind") == "voluntary"]
    nxt = next((r["next"] for r in vol if r.get("next") and r["next"] >= today), None)
    latest_table = targets_newer(rel, sd)
    stale = latest_table is not None
    targets = sd.get("kbd") or {}
    members = []
    for code in OPEC_CODES + PLUS_CODES:
        iso = STEO_ISO[code]
        s = t3d.get("copr_" + code) or {}
        h = {k: v for k, v in s.items() if lh and k <= lh}
        if not h:
            continue
        ks = sorted(h)
        last = ks[-1]
        y1 = h.get(_ago_m(last, 12))
        fc = sorted((k, v) for k, v in s.items() if lh and k > lh)
        tg = targets.get(iso)
        members.append({"iso": iso, "name": cname(iso), "group": "OPEC" if code in OPEC_CODES else "OPEC+",
                        "month": last, "kbd": round(h[last] * 1000), "y1": round((h[last] - y1) * 1000) if y1 else None,
                        "target": tg, "gap": round(h[last] * 1000 - tg) if tg else None,
                        "fc_end": [fc[-1][0], round(fc[-1][1] * 1000)] if fc else None,
                        "spark": [[k, round(h[k] * 1000)] for k in ks[-24:]]})
    members.sort(key=lambda m: -m["kbd"])
    def ser(code, scale=1.0):
        return [[k, round(v * scale, 3), k > (lh or "")] for k, v in sorted((t3d.get(code) or {}).items())]
    orb = Ser(orb_series())
    od, ov = orb.last()
    group_now = vol[0] if vol else None
    out = {"releases": [{k: r.get(k) for k in ("slug", "date", "kind", "group_size", "members", "lead", "decision",
                                                "next", "adjust_kbd", "has_table", "url", "error")} for r in rel],
           "next_meeting": nxt, "group": group_now and {"size": group_now.get("group_size"),
                                                        "members": group_now.get("members"), "date": group_now.get("date")},
           "targets": {**{k: v for k, v in sd.items() if k != "kbd"}, "kbd": targets, "stale": stale,
                       "newer": latest_table and {"slug": latest_table["slug"], "date": latest_table.get("date"),
                                                  "url": latest_table["url"]} if stale else None},
           "members": members, "last_hist": lh,
           "capacity": {"opec": ser("copc_opec"), "spare": ser("cops_opec"), "spare_me": ser("cops_opec_r05"),
                        "outages": ser("padi_OPEC"), "production": ser("copr_opec")},
           "orb": {"last": ov, "asof": od, "series": _downsample(orb.items_since("2015-01-01"), 900)},
           "fetched_at": time.time()}
    out["insights"] = _opec_insights(out)
    return out


def _opec_insights(p: dict) -> list[dict]:
    ins = []
    tg = [m for m in p["members"] if m.get("gap") is not None]
    if tg:
        short = sum(-m["gap"] for m in tg if m["gap"] < 0)
        worst = min(tg, key=lambda m: m["gap"])
        ins.append({"tone": "warn" if short > 1000 else "info", "sec": "opec",
                    "text": f"The {len(tg)} countries with voluntary targets produced {short:,} kb/d below them in "
                            f"{_mlong(tg[0]['month'])} (EIA); {worst['name']} is {abs(worst['gap']):,} kb/d short of "
                            f"{worst['target']:,}."})
    sp = [x for x in p["capacity"]["spare"] if not x[2]]
    if sp:
        ins.append({"tone": "warn" if sp[-1][1] < 1 else "info", "sec": "opec",
                    "text": f"OPEC's spare capacity is {sp[-1][1]:.1f} mb/d ({_mlong(sp[-1][0])}, EIA): "
                            f"{'almost none left to call on' if sp[-1][1] < 1 else 'room to add oil if needed'}."})
    if p.get("next_meeting"):
        d = (date.fromisoformat(p["next_meeting"]) - date.today()).days
        ins.append({"tone": "info", "sec": "opec",
                    "text": f"The OPEC+ group meets next on {dmy_long(p['next_meeting'])}, in {d} day{'s' if d != 1 else ''}."})
    if p["targets"].get("stale"):
        ins.append({"tone": "warn", "sec": "opec",
                    "text": "OPEC has published a newer required-production table than the one this page holds."})
    return ins


def dmy_long(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.day} {calendar.month_name[d.month]} {d.year}"


# --------------------------------------------------------------------------
# who trades it: positioning and trade partners
# --------------------------------------------------------------------------

CFTC = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
CFTC_MKTS = [("067651", "WTI", "NYMEX"), ("06765T", "Brent (NYMEX)", "NYMEX"),
             ("111659", "RBOB gasoline", "NYMEX"), ("022651", "NY Harbor diesel", "NYMEX")]
ICE_COT = "https://www.ice.com/publicdocs/futures/COTHist{}.csv"
ICE_MKTS = [("ICE Brent Crude Futures - ICE Futures Europe", "Brent", "ICE"),
            ("ICE Gasoil Futures - ICE Futures Europe", "Gasoil (Europe's diesel)", "ICE")]


def _cot_row(d, oi, pml, pms, swl, sws, mml, mms, orl, ors, nrl, nrs) -> dict:
    f = lambda x: int(float(x or 0))
    oi, pml, pms, swl, sws, mml, mms, orl, ors, nrl, nrs = map(f, (oi, pml, pms, swl, sws, mml, mms, orl, ors, nrl, nrs))
    return {"d": d, "oi": oi, "mm_long": mml, "mm_short": mms, "mm_net": mml - mms, "pm_net": pml - pms,
            "sw_net": swl - sws, "or_net": orl - ors, "nr_net": nrl - nrs}


def _load_cot() -> dict:
    since = (date.today() - timedelta(days=3 * 365 + 30)).isoformat()
    out, errs = {}, {}
    sel = ("report_date_as_yyyy_mm_dd,open_interest_all,prod_merc_positions_long,prod_merc_positions_short,"
           "swap_positions_long_all,swap__positions_short_all,m_money_positions_long_all,m_money_positions_short_all,"
           "other_rept_positions_long,other_rept_positions_short,nonrept_positions_long_all,nonrept_positions_short_all")
    for code, name, exch in CFTC_MKTS:
        try:
            q = {"$select": sel, "$where": f"cftc_contract_market_code='{code}' AND report_date_as_yyyy_mm_dd >= '{since}T00:00:00.000'",
                 "$order": "report_date_as_yyyy_mm_dd", "$limit": "2000"}
            rows = json.loads(net.get(CFTC + "?" + urllib.parse.urlencode(q), timeout=60))
            out[name] = {"exch": exch, "rows": [_cot_row(r["report_date_as_yyyy_mm_dd"][:10], r.get("open_interest_all"),
                                                         r.get("prod_merc_positions_long"), r.get("prod_merc_positions_short"),
                                                         r.get("swap_positions_long_all"), r.get("swap__positions_short_all"),
                                                         r.get("m_money_positions_long_all"), r.get("m_money_positions_short_all"),
                                                         r.get("other_rept_positions_long"), r.get("other_rept_positions_short"),
                                                         r.get("nonrept_positions_long_all"), r.get("nonrept_positions_short_all"))
                                                for r in rows]}
        except Exception as e:
            errs[name] = f"{e.__class__.__name__}: {e}"[:160]
    y = date.today().year
    ice: dict = {}
    for yr in (y, y - 1, y - 2, y - 3):
        try:
            raw = net.get(ICE_COT.format(yr), timeout=60).decode("utf-8-sig", "replace")
        except Exception as e:
            errs[f"ICE {yr}"] = f"{e.__class__.__name__}: {e}"[:120]
            continue
        for r in csv.DictReader(io.StringIO(raw)):
            nm = (r.get("Market_and_Exchange_Names") or "").strip()
            hit = next((m for m in ICE_MKTS if nm == m[0]), None)
            if not hit or (r.get("FutOnly_or_Combined") or "FutOnly").strip() not in ("FutOnly", ""):
                continue
            try:
                d = datetime.strptime(r["As_of_Date_In_Form_YYMMDD"].strip(), "%y%m%d").date().isoformat()
            except Exception:
                continue
            ice.setdefault(hit[1], {})[d] = _cot_row(
                d, r.get("Open_Interest_All"), r.get("Prod_Merc_Positions_Long_All"), r.get("Prod_Merc_Positions_Short_All"),
                r.get("Swap_Positions_Long_All"), r.get("Swap_Positions_Short_All"), r.get("M_Money_Positions_Long_All"),
                r.get("M_Money_Positions_Short_All"), r.get("Other_Rept_Positions_Long_All"),
                r.get("Other_Rept_Positions_Short_All"), r.get("NonRept_Positions_Long_All"), r.get("NonRept_Positions_Short_All"))
    for name, m in ice.items():
        out[name + " (ICE)"] = {"exch": "ICE", "rows": [m[d] for d in sorted(m) if d >= since]}
    if not out:
        raise RuntimeError("neither the CFTC nor ICE answered")
    return {"m": out, "errors": errs, "fetched_at": time.time()}


def cot() -> dict:
    return _cached("oil:cot", TTL["cot"], _load_cot)


COMTRADE = "https://comtradeapi.un.org/public/v1/preview/C/A/HS"
IMPORTERS = ["156", "699", "842", "392", "410", "528", "276", "380", "724", "251", "702", "764", "826", "616", "490"]


def _load_comtrade() -> dict:
    """
    Crude imports (HS 2709) by supplier, as the IMPORTERS report them: the
    exporters that matter most (Saudi Arabia, Iraq, Iran, Russia) are slow or
    silent reporters, the importers' customs are not.
    """
    y = date.today().year
    out, errs = {}, {}
    for code in IMPORTERS:
        got = None
        for yr in (y - 1, y - 2):
            try:
                d = json.loads(net.get(f"{COMTRADE}?reporterCode={code}&period={yr}&cmdCode=2709&flowCode=M", timeout=60))
            except Exception as e:
                errs[code] = f"{e.__class__.__name__}: {e}"[:120]
                time.sleep(1.0)
                continue
            rows = [r for r in (d.get("data") or []) if r.get("partnerCode") not in (0, None)]
            if rows:
                got = (yr, rows)
                break
            time.sleep(0.6)
        if got:
            yr, rows = got
            out[code] = {"year": yr, "partners": {str(r["partnerCode"]): [r.get("netWgt") or 0, r.get("primaryValue") or 0]
                                                  for r in rows}}
        time.sleep(0.6)
    if not out:
        raise RuntimeError("UN Comtrade returned nothing")
    return {"imp": out, "errors": errs, "fetched_at": time.time()}


def comtrade() -> dict:
    return _cached("oil:comtrade", TTL["comtrade"], _load_comtrade)


def flow_kbd(kg: float, usd: float, brent: Optional[float]) -> tuple[float, str]:
    """
    Barrels a day from a customs line. Weight is the better measure, but some
    customs offices report it in the wrong unit: a tonne of crude is worth
    roughly five to eight barrels of Brent, so a weight that implies far less
    or far more than that is set aside and the value, at the year's average
    Brent price, is used instead.
    """
    kg, usd = kg or 0.0, usd or 0.0
    by_w = kg / 1000 * BBL_PER_T / 365 / 1000
    if brent and usd > 0:
        by_v = usd / brent / 365 / 1000
        if kg <= 0:
            return by_v, "value"
        per_t = usd / (kg / 1000)
        if not (brent * 3.5 <= per_t <= brent * 12):
            return by_v, "value"
    return by_w, "weight"


def _brent_year(year: int) -> Optional[float]:
    m = fred_series().get("POILBREUSDM") or {}
    vals = [v for k, v in m.items() if k.startswith(str(year))]
    return statistics.mean(vals) if len(vals) >= 6 else None


def trade_matrix() -> dict:
    """Who sells crude to whom, in thousand barrels a day, from the importers' customs data."""
    ct = comtrade()["imp"]
    byn = countries()["byn"]
    imps, flows, years = [], [], {}
    for code, v in ct.items():
        imp = byn.get(str(int(code)))
        if not imp:
            continue
        years[imp] = v["year"]
        brent = _brent_year(v["year"])
        tot = 0.0
        for pc, (kg, usd) in v["partners"].items():
            exp = byn.get(str(int(pc)))
            kbd, how = flow_kbd(kg, usd, brent)
            if kbd < 1:
                continue
            tot += kbd
            flows.append({"from": exp or f"#{pc}", "to": imp, "kbd": round(kbd, 1),
                          "usd_bn": round((usd or 0) / 1e9, 2), "year": v["year"], "by": how})
        imps.append({"iso": imp, "name": cname(imp), "kbd": round(tot), "year": v["year"]})
    imps.sort(key=lambda r: -r["kbd"])
    names = {i: cname(i) for i in {f["from"] for f in flows} | {f["to"] for f in flows} if not i.startswith("#")}
    return {"importers": imps, "flows": flows, "years": years, "names": names}


def buyers_of(iso2: str) -> list[dict]:
    """Who imports a country's crude, as the importers' customs report it."""
    m = trade_matrix()
    rows = [f for f in m["flows"] if f["from"] == iso2]
    rows.sort(key=lambda f: -f["kbd"])
    return [{**f, "name": cname(f["to"])} for f in rows]


def _positioning() -> list[dict]:
    C = cot()["m"]
    out = []
    for name, v in C.items():
        rows = v["rows"]
        if not rows:
            continue
        last = rows[-1]
        prev = rows[-2] if len(rows) > 1 else None
        nets = [r["mm_net"] for r in rows]
        out.append({"name": name, "exch": v["exch"], "asof": last["d"], **last,
                    "mm_w1": last["mm_net"] - prev["mm_net"] if prev else None,
                    "mm_pctile": _pctile(nets, last["mm_net"]),
                    "mm_share": _r(last["mm_net"] / last["oi"] * 100, 1) if last["oi"] else None,
                    "hist": [[r["d"], r["mm_net"], r["pm_net"], r["sw_net"]] for r in rows]})
    order = ["Brent (ICE)", "WTI", "Gasoil (Europe's diesel) (ICE)", "NY Harbor diesel", "RBOB gasoline", "Brent (NYMEX)"]
    out.sort(key=lambda r: order.index(r["name"]) if r["name"] in order else 99)
    return out


def part_trade() -> dict:
    pos, mat, err = [], None, {}
    try:
        pos = _positioning()
    except Exception as e:
        err["positioning"] = f"{e.__class__.__name__}: {e}"[:200]
    try:
        mat = trade_matrix()
    except Exception as e:
        err["comtrade"] = f"{e.__class__.__name__}: {e}"[:200]
    if not pos and not mat:
        raise RuntimeError("; ".join(err.values()) or "no data")
    out = {"positioning": pos, "matrix": mat, "traders": (seed().get("traders") or {}).get("groups") or [],
           "errors": err, "next_cot": next_cot(), "fetched_at": time.time()}
    ins = []
    for r in pos[:2]:
        if r.get("mm_pctile") is not None:
            ins.append({"tone": "warn" if r["mm_pctile"] >= 90 or r["mm_pctile"] <= 10 else "info", "sec": "trade",
                        "text": f"Hedge funds hold a net {'long' if r['mm_net'] >= 0 else 'short'} of {abs(r['mm_net']):,} {r['name']} "
                                f"contracts ({r['asof'][8:10]}/{r['asof'][5:7]}), the {_ord(r['mm_pctile'])} percentile of three years"
                                + (f", {r['mm_w1']:+,} on the week" if r.get("mm_w1") is not None else "") + "."})
    if mat and mat["importers"]:
        top = mat["importers"][0]
        sup = sorted((f for f in mat["flows"] if f["to"] == top["iso"]), key=lambda f: -f["kbd"])[:3]
        if sup:
            ins.append({"tone": "info", "sec": "trade",
                        "text": f"{top['name']} imported {top['kbd']:,} kb/d of crude in {top['year']}; its biggest suppliers were "
                                + ", ".join(f"{cname(s['from'])} ({s['kbd']:,.0f})" for s in sup) + "."})
    out["insights"] = ins
    return out


def next_cot(now: Optional[datetime] = None) -> str:
    """Commitments of Traders: Fridays at 15:30 New York (21:30 here), positions as of Tuesday."""
    now = now or datetime.now()
    d = now.date()
    days = (4 - d.weekday()) % 7
    if days == 0 and now.hour * 60 + now.minute >= 21 * 60 + 30:
        days = 7
    return (d + timedelta(days=days)).isoformat()


# --------------------------------------------------------------------------
# pump prices
# --------------------------------------------------------------------------

EU_PAGE = "https://energy.ec.europa.eu/data-and-analysis/weekly-oil-bulletin_en"
EU_HIST = ("https://energy.ec.europa.eu/document/download/906e60ca-8b6a-44e7-8589-652854d2fd3f_en"
           "?filename=Weekly_Oil_Bulletin_Prices_History_maticni_4web.xlsx")
UK_PAGE = "https://www.gov.uk/government/statistics/weekly-road-fuel-prices"
EU_NAMES = {"EU": "EU average", "EUR": "Euro area average"}


def parse_eu_bulletin(raw: bytes, since: str = "2019-01-01") -> dict:
    """€ per litre, Euro-super 95 and diesel, every EU country, with and without taxes."""
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    out: dict = {}
    for sheet, tag in (("Prices with taxes", "tax"), ("Prices wo taxes", "net")):
        if sheet not in wb.sheetnames:
            continue
        it = wb[sheet].iter_rows(values_only=True)
        hdr = next(it)
        cols = {}
        for i, h in enumerate(hdr):
            m = re.match(r"^([A-Z]{2,3})_price_(?:with|wo)_tax_(euro95|diesel)$", str(h or ""))
            if m:
                cols[i] = (m.group(1), m.group(2))
        for row in it:
            d = row[0]
            if not hasattr(d, "date"):
                continue
            ds = d.date().isoformat()
            if ds < since:
                continue
            for i, (c, prod) in cols.items():
                v = row[i] if i < len(row) else None
                if isinstance(v, (int, float)) and v > 0:
                    out.setdefault(c, {}).setdefault(f"{prod}_{tag}", {})[ds] = round(v / 1000, 4)
    return out


def _load_eu_pump() -> dict:
    url = EU_HIST
    try:
        page = net.get(EU_PAGE, timeout=40).decode("utf-8", "replace")
        m = re.search(r'href="([^"]+Prices_History[^"]+\.xlsx)"', page)
        if m:
            url = urllib.parse.urljoin(EU_PAGE, _html.unescape(m.group(1)))
    except Exception:
        pass
    data = parse_eu_bulletin(net.get(url, timeout=180))
    if len(data) < 20:
        raise RuntimeError("the EU Weekly Oil Bulletin had fewer than twenty countries")
    return {"c": data, "fetched_at": time.time()}


def eu_pump() -> dict:
    return _cached("oil:eupump", TTL["pump"], _load_eu_pump).get("c") or {}


def _load_uk_pump() -> dict:
    page = net.get(UK_PAGE, timeout=40).decode("utf-8", "replace")
    m = re.search(r'href="(https://assets\.publishing\.service\.gov\.uk/[^"]+CSV[^"]*\.csv)"', page)
    if not m:
        raise RuntimeError("the UK fuel price CSV was not linked from its page")
    rows = list(csv.reader(io.StringIO(net.get(m.group(1), timeout=60).decode("utf-8-sig", "replace"))))
    out = {"petrol": {}, "diesel": {}}
    for r in rows[1:]:
        try:
            d = datetime.strptime(r[0].strip(), "%d/%m/%Y").date().isoformat()
            out["petrol"][d], out["diesel"][d] = float(r[1]), float(r[2])
        except (ValueError, IndexError):
            continue
    if len(out["petrol"]) < 100:
        raise RuntimeError("the UK fuel price CSV was short")
    return {"p": out, "fetched_at": time.time()}


def uk_pump() -> dict:
    return _cached("oil:ukpump", TTL["pump"], _load_uk_pump).get("p") or {}


CCYS = [("EUR", "EURUSD=X", True), ("GBP", "GBPUSD=X", True), ("JPY", "USDJPY=X", False), ("CNY", "USDCNY=X", False),
        ("INR", "USDINR=X", False), ("KRW", "USDKRW=X", False), ("TRY", "USDTRY=X", False), ("BRL", "USDBRL=X", False),
        ("ZAR", "USDZAR=X", False), ("CAD", "USDCAD=X", False), ("NOK", "USDNOK=X", False), ("CHF", "USDCHF=X", False),
        ("AUD", "AUDUSD=X", True)]


def _load_fx() -> dict:
    got = _download_all([s for _, s, _ in CCYS], period="2y")
    return {**_pack(got), "fetched_at": time.time()}


def fx_series() -> dict:
    return _unpack(_cached("oil:fx", TTL["fut"], _load_fx))


def part_pump() -> dict:
    eu, err = {}, {}
    try:
        eu = eu_pump()
    except Exception as e:
        err["eu"] = f"{e.__class__.__name__}: {e}"[:200]
    rows = []
    for c, m in eu.items():
        p, d = Ser(m.get("euro95_tax")), Ser(m.get("diesel_tax"))
        if not p and not d:
            continue
        ld = max(p.last()[0] or "", d.last()[0] or "")
        t = date.fromisoformat(ld)
        ago = lambda s, n: s.on((t - timedelta(days=n)).isoformat())
        pn, dn = Ser(m.get("euro95_net")), Ser(m.get("diesel_net"))
        rows.append({"c": c, "name": EU_NAMES.get(c) or cname(c), "asof": ld,
                     "petrol": p.on(ld), "diesel": d.on(ld),
                     "petrol_y1": _r(_pct(p.on(ld), ago(p, 364)), 1), "diesel_y1": _r(_pct(d.on(ld), ago(d, 364)), 1),
                     "petrol_m1": _r(_pct(p.on(ld), ago(p, 28)), 1), "diesel_m1": _r(_pct(d.on(ld), ago(d, 28)), 1),
                     "petrol_tax": _r((1 - pn.on(ld) / p.on(ld)) * 100, 0) if pn and p.on(ld) and pn.on(ld) else None,
                     "diesel_tax": _r((1 - dn.on(ld) / d.on(ld)) * 100, 0) if dn and d.on(ld) and dn.on(ld) else None})
    rows.sort(key=lambda r: -(r["petrol"] or 0))
    fs, fut = fred_series(), fut_series()
    usd = Ser(fut.get("EURUSD=X"))
    gbp = Ser(fut.get("EURGBP=X"))
    extra = []
    for key, name, unit in (("GASREGW", "United States", "petrol"), ("GASDESW", "United States", "diesel")):
        s = Ser(fs.get(key))
        if s:
            d, v = s.last()
            fx = usd.on(d)
            if fx:
                extra.append({"c": "US", "name": name, "kind": unit, "asof": d, "local": v, "local_unit": "$/gal",
                              "eur_l": round(v / L_PER_GAL / fx, 3),
                              "y1": _r(_pct(v, s.on((date.fromisoformat(d) - timedelta(days=364)).isoformat())), 1)})
    try:
        uk = uk_pump()
        for kind in ("petrol", "diesel"):
            s = Ser(uk.get(kind))
            d, v = s.last()
            fx = gbp.on(d)
            if fx:
                extra.append({"c": "GB", "name": "United Kingdom", "kind": kind, "asof": d, "local": v, "local_unit": "p/l",
                              "eur_l": round(v / 100 / fx, 3),
                              "y1": _r(_pct(v, s.on((date.fromisoformat(d) - timedelta(days=364)).isoformat())), 1)})
    except Exception as e:
        err["uk"] = f"{e.__class__.__name__}: {e}"[:200]
    hist = {}
    for c in ("NL", "DE", "BE", "FR", "EU"):
        m = eu.get(c) or {}
        for prod in ("euro95_tax", "diesel_tax"):
            s = Ser(m.get(prod))
            if s:
                hist[f"{c}:{prod.split('_')[0]}"] = [[d, v] for d, v in s.items_since("2019-01-01")]
    for key, lab in (("GASREGW", "US:euro95"), ("GASDESW", "US:diesel")):
        s = Ser(fs.get(key))
        hist[lab] = [[d, round(v / L_PER_GAL / f, 3)] for d, v in s.items_since("2019-01-01") if (f := usd.on(d))]
    # Brent in each currency: what the same barrel costs where the money is not dollars.
    bz = Ser(fs.get("DCOILBRENTEU"))
    fxs = fx_series()
    ccy = []
    if bz:
        d0, b0 = bz.last()
        t = date.fromisoformat(d0)
        for code, sym, inverse in [("USD", None, False)] + CCYS:
            s = Ser(fxs.get(sym)) if sym else None
            def local(day):
                b = bz.on(day)
                if b is None:
                    return None
                if not sym:
                    return b
                r = s.on(day) if s else None
                if not r:
                    return None
                return b / r if inverse else b * r
            now = local(d0)
            if now is None:
                continue
            ccy.append({"ccy": code, "asof": d0, "brent": round(now, 2),
                        "y1": _r(_pct(now, local((t - timedelta(days=364)).isoformat())), 1),
                        "ytd": _r(_pct(now, local(f"{t.year - 1}-12-31")), 1)})
    out = {"eu": rows, "other": extra, "hist": hist, "currencies": ccy, "errors": err, "fetched_at": time.time()}
    ins = []
    nl = next((r for r in rows if r["c"] == "NL"), None)
    if nl and nl.get("diesel"):
        ins.append({"tone": "warn" if (nl.get("diesel_y1") or 0) > 15 else "info", "sec": "pump",
                    "text": f"In the Netherlands diesel costs €{nl['diesel']:.3f} a litre and Euro 95 €{nl['petrol']:.3f} "
                            f"(week of {nl['asof'][8:10]}/{nl['asof'][5:7]}); diesel is {nl['diesel_y1']:+.0f}% on a year ago."})
    dear = [r for r in rows if r["c"] not in ("EU", "EUR") and r.get("diesel")]
    if dear:
        hi = max(dear, key=lambda r: r["diesel"])
        lo = min(dear, key=lambda r: r["diesel"])
        ins.append({"tone": "info", "sec": "pump",
                    "text": f"Diesel ranges from €{lo['diesel']:.2f} a litre in {lo['name']} to €{hi['diesel']:.2f} in {hi['name']}."})
    out["insights"] = ins
    return out


# --------------------------------------------------------------------------
# your portfolio and oil
# --------------------------------------------------------------------------

def equity_list() -> list[tuple[str, str, str]]:
    return [tuple(x) for x in ((seed().get("equities") or {}).get("list") or [])]


def _load_equities() -> dict:
    from .bonds import _download
    syms = [s for s, _, _ in equity_list()]
    got = _download(syms, period="2y")              # adjusted: a dividend is not a loss
    return {**_pack(got), "fetched_at": time.time()}


def equities() -> dict:
    return _unpack(_cached("oil:eq", TTL["eq"], _load_equities))


def _beta_to(prices: dict, bench: dict) -> Optional[dict]:
    """
    Sensitivity to oil from five-day returns, not daily ones: shares close in
    New York or Amsterdam, Brent is assessed in London, and on one day's
    returns the two barely overlap (see risk.HORIZON). The standard error is
    widened for the overlap of the windows.
    """
    from . import risk
    ys, xs = risk.price_windows(prices, bench)
    f = risk.ols(ys, xs)
    if not f:
        return None
    se = f["se_beta"] * math.sqrt(risk.HORIZON) if f.get("se_beta") else None
    return {"beta": round(f["beta"], 2), "r2": round(f["r2"], 2), "se": _r(se, 2), "n": f["n"]}


def part_book() -> dict:
    from . import bonds, risk
    fs = fred_series()
    brent_all = fs.get("DCOILBRENTEU") or {}      # physical Brent: no futures roll to distort a week
    since = (date.today() - timedelta(days=730)).isoformat()
    brent = {d: v for d, v in brent_all.items() if d >= since}
    bz = Ser(brent_all)
    b_last = bz.last()[1]
    eq = equities()
    eq_names = {s: (n, g) for s, n, g in equity_list()}
    out = {"brent": b_last, "brent_asof": bz.last()[0], "fetched_at": time.time()}
    holdings, you = [], None
    try:
        fit = bonds.fit_data()
        tot = fit["book"]["total"] or 0
        for r in fit["book"]["rows"]:
            px = {d: v for d, v in (fit["px"].get(r["t"]) or {}).items() if d >= since}
            b = _beta_to(px, brent)
            if not b:
                continue
            value = r["w"] * tot
            holdings.append({"t": r["t"], "name": r["name"], "sector": r.get("sector"), "w": round(r["w"] * 100, 2),
                             "value": round(value), **b, "oil_name": r["t"] in eq_names or (r.get("sector") or "") == "Energy"})
        holdings.sort(key=lambda h: -h["beta"])
        br = {d: v for d, v in fit["book_r"].items() if d >= since}
        ys, xs, _ = risk.curve_windows(br, brent, sorted(br))
        f = risk.ols(ys, xs)
        if f:
            you = {"beta": round(f["beta"], 3), "r2": round(f["r2"], 2), "n": f["n"],
                   "se": _r(f["se_beta"] * math.sqrt(risk.HORIZON), 3) if f.get("se_beta") else None, "total": round(tot)}
    except Exception as e:
        out["book_error"] = f"{e.__class__.__name__}: {e}"[:200]
    move = 10.0
    pct10 = move / b_last if b_last else None
    if you and pct10:
        you["per10"] = round(you["total"] * you["beta"] * pct10)
    for h in holdings:
        h["per10"] = round(h["value"] * h["beta"] * pct10) if pct10 else None
    # The oil shares, held or not.
    held = {h["t"] for h in holdings}
    board = []
    for sym, (name, grp) in eq_names.items():
        s = Ser(eq.get(sym))
        if not s:
            continue
        ld, lv = s.last()
        t = date.fromisoformat(ld)
        b = _beta_to({d: v for d, v in s.items_since((t - timedelta(days=365)).isoformat())}, brent)
        board.append({"sym": sym, "name": name, "group": grp, "last": round(lv, 2), "asof": ld,
                      "d1": _r(_pct(lv, s.v[-2] if len(s) > 1 else None), 2),
                      "ytd": _r(_pct(lv, s.on(f"{t.year - 1}-12-31")), 1),
                      "y1": _r(_pct(lv, s.on((t - timedelta(days=365)).isoformat())), 1),
                      "beta": b["beta"] if b else None, "held": sym in held})
    b_ytd = _r(_pct(b_last, bz.on(f"{date.today().year - 1}-12-31")), 1) if bz else None
    out.update({"you": you, "holdings": holdings, "board": board, "move": move, "pct10": _r((pct10 or 0) * 100, 1),
                "brent_ytd": b_ytd})
    ins = []
    if you:
        ins.append({"tone": "info", "sec": "book",
                    "text": f"Your portfolio has moved {you['beta']:+.2f}% for each 1% in Brent over two years of weekly "
                            f"returns: a ${move:.0f} rise in Brent ({out['pct10']:.1f}%) has meant about "
                            f"€{you['per10']:+,} (R² {you['r2']:.2f})."})
    names = [h for h in holdings if h["oil_name"]]
    if names:
        ins.append({"tone": "info", "sec": "book",
                    "text": "Your oil holdings: " + ", ".join(f"{h['name'].split()[0].title()} (beta {h['beta']:+.2f})" for h in names[:5]) + "."})
    out["insights"] = ins
    return out


# --------------------------------------------------------------------------
# news
# --------------------------------------------------------------------------

OIL_RSS = [("OilPrice.com", "https://oilprice.com/rss/main"),
           ("EIA Today in Energy", "https://www.eia.gov/rss/todayinenergy.xml")]
OIL_KW = re.compile(r"\b(oil|crude|brent|wti|opec\+?|barrels?|refiner(y|ies|s)?|diesel|gasoline|petrol|gasoil|tankers?|"
                    r"hormuz|shale|pipelines?|aramco|spr|strategic petroleum|bab el-mandeb|red sea|jet fuel|fuel oil|"
                    r"drilling|rig count|oilfields?)\b", re.I)


def _rss(src: str, url: str) -> list[dict]:
    from email.utils import parsedate_to_datetime
    b = net.get(url, timeout=30).decode("utf-8", "replace")
    out = []
    for it in re.findall(r"<item>(.*?)</item>", b, re.S)[:40]:
        g = lambda tag: (re.search(rf"<{tag}>(.*?)</{tag}>", it, re.S) or [None, ""])[1]
        title = _html.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", "", g("title"))).strip()
        link = re.sub(r"<!\[CDATA\[|\]\]>", "", g("link")).strip()
        desc = _html.unescape(re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>", " ", g("description")))
        try:
            ts = parsedate_to_datetime(g("pubDate").strip()).timestamp()
        except Exception:
            ts = None
        if title:
            out.append({"headline": title, "url": link, "source": src, "ts": ts,
                        "summary": re.sub(r"\s+", " ", desc).strip()[:300]})
    return out


def _load_news() -> dict:
    items, errs = [], {}
    for src, url in OIL_RSS:
        try:
            got = _rss(src, url)
            # OilPrice.com covers all of energy (solar, hydrogen, wind); keep what is about oil.
            items += [x for x in got if OIL_KW.search(x["headline"] + " " + x["summary"])]
        except Exception as e:
            errs[src] = f"{e.__class__.__name__}: {e}"[:120]
    own = store.cache_get("news") or {}
    for x in own.get("items") or []:
        h = x.get("headline") or ""
        if OIL_KW.search(h + " " + (x.get("summary") or "")):
            items.append({"headline": h, "url": x.get("url"), "source": x.get("source") or "news",
                          "ts": x.get("ts"), "summary": (x.get("summary") or "")[:300]})
    seen, out = set(), []
    for x in sorted(items, key=lambda x: -(x.get("ts") or 0)):
        k = re.sub(r"\W+", " ", x["headline"].lower()).strip()[:80]
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    if not out:
        raise RuntimeError("; ".join(errs.values()) or "no oil headlines")
    return {"items": out[:50], "errors": errs, "fetched_at": time.time()}


def part_news() -> dict:
    return _cached("oil:news", TTL["news"], _load_news)


# --------------------------------------------------------------------------
# what is coming up, the sources, the numbers the alerts read
# --------------------------------------------------------------------------

def part_calendar() -> dict:
    ev = [{"d": next_wpsr(), "time": "16:30", "k": "EIA", "t": "Weekly Petroleum Status Report (US stocks)"},
          {"d": next_cot(), "time": "21:30", "k": "CFTC/ICE", "t": "Commitments of Traders (positions as of Tuesday)"}]
    try:
        rel = opec_releases()
        today = date.today().isoformat()
        n = next((r["next"] for r in rel if r.get("kind") == "voluntary" and r.get("next") and r["next"] >= today), None)
        if n:
            ev.append({"d": n, "k": "OPEC+", "t": "The voluntary group's monthly meeting (online)"})
    except Exception:
        pass
    try:
        px = contracts()
        for root, name in ROOTS.items():
            f = Book(px, root).front()
            if f:
                ev.append({"d": f["expires"], "k": name, "t": f"{name} {f['lab']} contract expires; the front month rolls"})
    except Exception:
        pass
    ev.sort(key=lambda e: e["d"])
    return {"events": ev}


SOURCES = [
    ("oil:live", "Yahoo — futures quotes (about 10 minutes late)", "https://finance.yahoo.com/quote/BZ=F"),
    ("oil:curve", "Yahoo — Brent and WTI contract months", "https://finance.yahoo.com/quote/BZ=F"),
    ("oil:fut", "Yahoo — continuous futures, five years", "https://finance.yahoo.com/quote/CL=F"),
    ("oil:fred", "EIA spot prices and US retail fuel, via FRED", "https://fred.stlouisfed.org/series/DCOILBRENTEU"),
    ("oil:orb", "OPEC Reference Basket, daily", ORB_URL),
    ("oil:steo", "EIA Short-Term Energy Outlook", "https://www.eia.gov/outlooks/steo/"),
    ("oil:jodi", "JODI Oil World Database (crude)", "https://www.jodidata.org/oil/"),
    ("oil:pw:choke", "IMF PortWatch — chokepoint transits", "https://portwatch.imf.org/"),
    ("oil:pw:countries", "IMF PortWatch — tanker cargo by country", "https://portwatch.imf.org/"),
    ("oil:pw:ports", "IMF PortWatch — ports", "https://portwatch.imf.org/"),
    ("oil:eiaw", "EIA Weekly Petroleum Status Report", "https://www.eia.gov/petroleum/supply/weekly/"),
    ("oil:opec", "OPEC press releases", OPEC_PR),
    ("oil:cot", "CFTC and ICE Commitments of Traders", "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm"),
    ("oil:comtrade", "UN Comtrade — crude imports by supplier", "https://comtradeplus.un.org/"),
    ("oil:eupump", "EU Weekly Oil Bulletin", EU_PAGE),
    ("oil:ukpump", "UK weekly road fuel prices", UK_PAGE),
    ("oil:fx", "Yahoo — currencies", "https://finance.yahoo.com/"),
    ("oil:eq", "Yahoo — oil shares", "https://finance.yahoo.com/"),
    ("oil:news", "OilPrice.com and EIA feeds, and your news", "https://oilprice.com/"),
    ("oil:platforms", "Platforms: OpenStreetMap (ODbL) and Norway's Sodir", "https://www.openstreetmap.org/copyright"),
    ("oil:baltic", "Digitraffic (Fintraffic) — live ships in the Baltic", "https://www.digitraffic.fi/en/marine-traffic/"),
]


def sources() -> list[dict]:
    out = []
    for k, label, url in SOURCES:
        age = store.cache_age(k)
        hit = store.cache_get(k)
        out.append({"k": k, "label": label, "url": url, "ok": hit is not None,
                    "age_s": round(age) if age < 1e8 else None,
                    "stale": (hit or {}).get("_stale") if isinstance(hit, dict) else None})
    return out


def alert_values() -> dict:
    """
    The numbers the oil alert rules test, read from what is already cached —
    never a download: the home screen evaluates rules on every load.
    """
    out = {}
    L = store.cache_get("oil:live") or {}
    q, fr = L.get("q") or {}, L.get("fronts") or {}
    for k, root, cont in (("brent", "BZ", "BZ=F"), ("wti", "CL", "CL=F")):
        x = q.get(fr.get(root, "")) or q.get(cont)
        if x and x.get("last") is not None:
            out[k] = {"v": round(x["last"], 2), "asof": x.get("date"),
                      "pct": _r(_pct(x["last"], x.get("prev")), 2)}
    C = (store.cache_get("oil:pw:choke") or {}).get("cp") or {}
    hz = next((v for v in C.values() if v.get("name") == "Strait of Hormuz"), None)
    if hz and hz.get("n"):
        st = flow_stats(hz["n"])
        if st.get("w7") is not None:
            out["hormuz"] = {"v": st["w7"], "asof": st["end"]}
    eu = (store.cache_get("oil:eupump") or {}).get("c") or {}
    nl = Ser((eu.get("NL") or {}).get("diesel_tax"))
    if nl:
        d, v = nl.last()
        out["nl_diesel"] = {"v": v, "asof": d}
    return out


def brief_line() -> Optional[str]:
    """
    One sentence for the morning brief, from what the oil page last fetched —
    never a download: the brief is built at 06:30 against a time budget.
    """
    bits = []
    v = alert_values()
    b = v.get("brent")
    if b:
        bits.append(f"Brent ${b['v']:.2f}" + (f" ({b['pct']:+.1f}%)" if b.get("pct") is not None else ""))
    C = (store.cache_get("oil:pw:choke") or {}).get("cp") or {}
    hz = next((x for x in C.values() if x.get("name") == "Strait of Hormuz"), None)
    if hz and hz.get("n"):
        st = flow_stats(hz["n"])
        if st.get("w7") is not None and st.get("y7"):
            bits.append(f"Hormuz {st['w7']:.1f} tankers a day against {st['y7']:.1f} a year ago")
    cv = store.cache_get("oil:curve")
    if cv:
        try:
            bk = Book(_unpack(cv), "BZ")
            sp = ym_spread(bk.curve(bk.latest(), 24), 12)
            if sp is not None:
                bits.append(f"the Brent curve {abs(sp):.1f} dollars in {_structure(sp)} over a year")
        except Exception:
            pass
    w = store.cache_get("oil:eiaw")
    if w:
        s = Ser(_unpack(w).get("crude"))
        if len(s) > 1:
            d = (s.v[-1] - s.v[-2]) / 1000
            bits.append(f"US crude stocks {d:+.1f} mb in the week to {s.d[-1][8:10]}/{s.d[-1][5:7]}")
    if not bits:
        return None
    return "Oil: " + "; ".join(bits) + ". [OIL](/m/MKT/oil)"


ALERT_KINDS = {
    "oil_brent_above": ("brent", 1, "Brent", "$"), "oil_brent_below": ("brent", -1, "Brent", "$"),
    "oil_wti_above": ("wti", 1, "WTI", "$"), "oil_wti_below": ("wti", -1, "WTI", "$"),
    "oil_hormuz_above": ("hormuz", 1, "Hormuz tankers a day (7-day average)", ""),
    "oil_hormuz_below": ("hormuz", -1, "Hormuz tankers a day (7-day average)", ""),
    "oil_nldiesel_above": ("nl_diesel", 1, "Dutch diesel (€ a litre)", "€"),
    "oil_nldiesel_below": ("nl_diesel", -1, "Dutch diesel (€ a litre)", "€"),
}


def check_rule(kind: str, value: float, vals: Optional[dict] = None) -> Optional[dict]:
    """None when the rule is quiet; otherwise what to say. Missing data says so."""
    if kind not in ALERT_KINDS:
        return None
    key, sign, label, unit = ALERT_KINDS[kind]
    vals = alert_values() if vals is None else vals
    x = vals.get(key)
    if not x:
        return {"level": "info", "text": f"{label}: no data yet — open OIL once to load it"}
    v = x["v"]
    if (sign > 0 and v > value) or (sign < 0 and v < value):
        fmt = (lambda n: f"{unit}{n:,.2f}") if unit else (lambda n: f"{n:,.1f}")
        return {"level": "warn",
                "text": f"{label} is {fmt(v)} — {'above' if sign > 0 else 'below'} {fmt(value)}"
                        + (f" ({x['asof'][8:10]}/{x['asof'][5:7]})" if x.get("asof") else "")}
    return None


# --------------------------------------------------------------------------
# warming: the slow sources load in the background the first time
# --------------------------------------------------------------------------

_WARM = {"t": 0.0, "running": False}


def warm(force: bool = False) -> None:
    """
    Load every part once in a background thread, slowest last, so a page that
    opens finds most of them cached. At most once every ten minutes.
    """
    if _WARM["running"] or (not force and time.time() - _WARM["t"] < 600):
        return
    _WARM.update(running=True, t=time.time())

    def run():
        try:
            for fn in (part_prices, part_curve, part_cracks, part_stocks, part_supply, part_opec,
                       part_flows, part_pump, part_book, part_news, part_trade):
                try:
                    fn()
                except Exception as e:
                    logging.getLogger(__name__).warning("oil warm %s: %s", fn.__name__, e)
        finally:
            _WARM["running"] = False
    threading.Thread(target=run, name="oil-warm", daemon=True).start()


PARTS = {"prices": part_prices, "curve": part_curve, "cracks": part_cracks, "supply": part_supply,
         "flows": part_flows, "stocks": part_stocks, "opec": part_opec, "trade": part_trade,
         "pump": part_pump, "book": part_book, "news": part_news, "calendar": part_calendar}
