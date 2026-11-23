"""FastAPI routes for the portfolio module."""

from __future__ import annotations

import json
import math
import re
import tempfile
import time
import typing
from pathlib import Path

from fastapi import APIRouter, UploadFile, File, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel

from . import store, service, pricing, manual, stream, brief, news, market, geo, research, bonds
from . import earnings, earnlive, morning
from .degiro import parse_transactions, parse_account, build_instruments

router = APIRouter(prefix="/api/pf", tags=["portfolio"])


def _clean(o):
    """NaN/Inf are not valid JSON; turn them into null rather than 500ing."""
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean(x) for x in o]
    if isinstance(o, float) and (math.isnan(o) or math.isinf(o)):
        return None
    return o


class Safe(JSONResponse):
    def render(self, content: typing.Any) -> bytes:
        return super().render(_clean(content))


router.default_response_class = Safe


# --------------------------------------------------------------------------
# import
# --------------------------------------------------------------------------

def _parse_upload(f: UploadFile) -> tuple[list, str]:
    raw = f.file.read()
    with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tmp:
        tmp.write(raw)
        path = tmp.name
    head = raw[:200].decode("utf-8-sig", "ignore")
    if "Uitvoeringsplaats" in head or "Wisselkoers" in head:
        return parse_transactions(path), "transactions"
    if "Omschrijving" in head or "Valutadatum" in head:
        return parse_account(path), "account"
    raise HTTPException(400, f"{f.filename}: not a recognised DEGIRO export. "
                             "Expected Transactions.csv or Account.csv.")


@router.post("/import/preview", response_class=Safe)
async def import_preview(files: list[UploadFile] = File(...)):
    """Stage only. Says exactly what a commit would change; writes nothing."""
    allt, kinds = [], []
    for f in files:
        rows, kind = _parse_upload(f)
        allt.extend(rows)
        kinds.append({"file": f.filename, "kind": kind, "rows": len(rows)})
    return {"files": kinds, **store.stage_import(allt)}


@router.post("/import/commit", response_class=Safe)
async def import_commit(files: list[UploadFile] = File(...)):
    allt = []
    for f in files:
        rows, _ = _parse_upload(f)
        allt.extend(rows)
    insts = build_instruments(allt)
    res = store.commit_import(allt, insts)
    stream.STREAM.expire_base()
    return {**res, "instruments": len(insts)}


# --------------------------------------------------------------------------
# dashboard
# --------------------------------------------------------------------------

@router.get("/dashboard", response_class=Safe)
def get_dashboard(benchmark: str = Query("SP500"), snapshot: bool = Query(True)):
    data = service.dashboard(benchmark)
    if snapshot and not data.get("empty"):
        try:
            service.snapshot_today(data)
        except Exception:
            pass
    return data


@router.get("/riskmap", response_class=Safe)
def get_riskmap():
    """Who carries the book's risk, what moves together, and what the book bets on."""
    from . import riskmap
    try:
        return riskmap.build()
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/rotation", response_class=Safe)
def get_rotation(universe: str = Query("book"), bench: str | None = Query(None),
                 freq: str = Query("w")):
    """RRG: trend and momentum of relative strength, with a tail, for a set of names."""
    from . import rotation
    if universe not in rotation.UNIVERSES:
        raise HTTPException(400, f"universe must be one of {sorted(rotation.UNIVERSES)}")
    try:
        return rotation.build(universe, bench, freq)
    except Exception as e:
        return {"available": False, "universe": universe,
                "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.post("/enrich", response_class=Safe)
def enrich(only_missing: bool = Query(True)):
    """Resolve tickers and fetch sector/country. Slow — call from a button."""
    insts = store.load_instruments()
    todo = [i for i, r in insts.items()
            if not only_missing or not r.get("sector") or not r.get("ticker")]
    return {"updated": service.enrich_instruments(todo), "considered": len(todo)}


class InstrumentPatch(BaseModel):
    ticker: str | None = None
    underlying_ticker: str | None = None    # a turbo's: the share it tracks
    sector_override: str | None = None
    country_override: str | None = None
    manual_price: float | None = None


@router.patch("/instrument/{isin}", response_class=Safe)
def patch_instrument(isin: str, body: InstrumentPatch):
    fields = {k: v for k, v in body.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "nothing to update")
    if "ticker" in fields:
        inst = store.load_instruments().get(isin) or {}
        if inst.get("kind") == "DERIVATIVE":
            # a turbo has no quote of its own: the symbol is the share it tracks
            fields["underlying_ticker"] = fields.pop("ticker")
        else:
            fields["ticker_src"] = "manual"
    if "underlying_ticker" in fields:
        fields["underlying_ticker"] = fields["underlying_ticker"].strip().upper()
    if "manual_price" in fields:
        from datetime import datetime
        fields["manual_price_at"] = datetime.utcnow().isoformat(timespec="seconds")
    store.set_instrument(isin, **fields)
    return {"ok": True, "isin": isin, "updated": list(fields)}


class FinIn(BaseModel):
    text: str                       # DEGIRO's current name for the turbo, or just the financing level
    d: str | None = None            # the day it is valid; today when left out


@router.post("/instrument/{isin}/financing", response_class=Safe)
def set_financing(isin: str, body: FinIn):
    """
    A turbo's financing level on a date, from DEGIRO's product page. Paste the
    turbo's current name ("SG ABBOTT LABS BEST TURBO LONG SL 73.9439 FN
    73.9439 R 5 LV 3.33") or type the level. Two of these, a few weeks apart,
    give the rate SG moves it at (leveraged.FinPath).
    """
    from datetime import date as _date
    from . import leveraged
    inst = store.load_instruments().get(isin)
    if not inst or inst.get("kind") != "DERIVATIVE":
        raise HTTPException(404, "no leveraged product with that ISIN")
    spec = leveraged.parse(inst.get("name") or "")
    if spec.family != "TURBO":
        raise HTTPException(400, "only a turbo has a financing level")
    txt = (body.text or "").strip()
    sl = ratio = lv = None
    if re.search(r"\b(FN|STR)\s", txt.upper()):
        got = leveraged.parse(txt)
        if not got.financing:
            raise HTTPException(400, "no financing level (FN) in that name")
        if got.ratio and spec.ratio and abs(got.ratio - spec.ratio) > 1e-9:
            raise HTTPException(400, f"that name has ratio {got.ratio:g}, this turbo {spec.ratio:g}: is it the same product?")
        if got.direction != spec.direction:
            raise HTTPException(400, f"that name is {got.direction.lower()}, this turbo {spec.direction.lower()}")
        fn, sl, ratio, lv = got.financing, got.barrier, got.ratio, got.name_leverage
    else:
        try:
            fn = float(txt.replace(",", "."))
        except ValueError:
            raise HTTPException(400, "paste the turbo's current name from DEGIRO, or type its financing level")
    if fn <= 0 or (spec.financing and not 0.5 < fn / spec.financing < 1.5):
        raise HTTPException(400, f"{fn:g} is far from the name's {spec.financing:g}: check it")
    d = body.d or _date.today().isoformat()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        raise HTTPException(400, "d must be a date, YYYY-MM-DD")
    if sl is None and spec.barrier and spec.financing:
        sl = fn * spec.barrier / spec.financing        # the stop keeps its distance
    store.add_fin(isin, d, fn, sl, ratio, lv, "typed")
    return {"ok": True, "isin": isin, "d": d, "fn": fn, "sl": sl, "points": store.fin_points(isin)}


@router.delete("/instrument/{isin}/financing/{d}", response_class=Safe)
def drop_financing(isin: str, d: str):
    if not store.drop_fin(isin, d):
        raise HTTPException(404, "no financing level recorded for that day")
    return {"ok": True}


@router.get("/instruments", response_class=Safe)
def list_instruments():
    return list(store.load_instruments().values())


@router.get("/transactions", response_class=Safe)
def list_transactions(limit: int = 500):
    return [t.as_dict() for t in store.load_txns()[-limit:]][::-1]


@router.get("/snapshots", response_class=Safe)
def list_snapshots():
    return store.load_snapshots()


# --------------------------------------------------------------------------
# hand-entered transactions
# --------------------------------------------------------------------------

class ManualTxn(BaseModel):
    kind: str                       # BUY | SELL
    isin: str
    trade_date: str                 # YYYY-MM-DD
    quantity: float
    price: float
    currency: str = "EUR"
    fx_rate: float | None = None
    fee_eur: float = 0.0
    name: str | None = None
    ticker: str | None = None
    account: str = "MANUAL"
    note: str = ""


@router.post("/transaction", response_class=Safe)
def add_transaction(body: ManualTxn):
    try:
        res = manual.add_transaction(**body.model_dump())
    except manual.ManualError as e:
        raise HTTPException(400, str(e))
    stream.STREAM.expire_base()
    return res


@router.get("/transaction/manual", response_class=Safe)
def list_manual_transactions(limit: int = 100):
    return manual.list_manual(limit)


@router.delete("/transaction/{txn_id}", response_class=Safe)
def delete_transaction(txn_id: int):
    try:
        res = manual.delete_manual(txn_id)
    except manual.ManualError as e:
        raise HTTPException(400, str(e))
    stream.STREAM.expire_base()
    return res


@router.get("/holdings/simple", response_class=Safe)
def holdings_simple():
    """Everything ever traded, with units currently held — for the sell picker."""
    from .engine import build_ledger
    txns = store.load_txns()
    L = build_ledger([t for t in txns if t.kind in ("BUY", "SELL")])
    insts = store.load_instruments()
    out = []
    for isin, meta in insts.items():
        p = L.positions.get(isin)
        out.append({
            "isin": isin,
            "name": meta.get("name") or isin,
            "ticker": meta.get("ticker") or pricing.SEED_TICKERS.get(isin),
            "currency": (p.currency if p else meta.get("currency")) or "EUR",
            "qty": round(p.qty, 6) if p else 0.0,
            "avg_price": round(p.avg_price_native, 4) if p else None,
        })
    out.sort(key=lambda r: (-r["qty"], r["name"]))
    return out


class LevMode(BaseModel):
    mode: str


@router.post("/leverage-mode", response_class=Safe)
def set_leverage_mode(body: LevMode):
    """How leveraged products are valued: zero | last_trade | model."""
    if body.mode not in service.LEV_MODES:
        raise HTTPException(400, f"mode must be one of {service.LEV_MODES}")
    store.set_setting("lev_valuation", body.mode)
    return {"ok": True, "mode": body.mode}


@router.get("/market", response_class=Safe)
def market_strip():
    """
    The short board the HOME screen shows. Named so it does not shadow the
    `market` module imported above — a route function and a module sharing a
    name at module scope is a collision Python resolves silently in favour of
    whichever came last.
    """
    return {"rows": pricing.market_board()}


# The home strip, taken from the board the background thread already builds.
#
# It used to call Yahoo synchronously inside the /home request, which made it
# the slowest thing on the page — and pricing's circuit breaker disables the
# feed for three minutes after a SINGLE failure, so one bad moment left the
# panel blank with no explanation until the cooldown expired. The MKT board is
# already assembled off-thread every two minutes; the home strip is eleven of
# its rows.
HOME_ROWS = [
    ("AEX", "Indices"), ("SPX", "Indices"), ("NDX", "Indices"),
    ("SX5E", "Indices"), ("DAX", "Indices"),
    ("EURUSD", "FX & rates"), ("EURGBP", "FX & rates"), ("US10Y", "FX & rates"),
    ("BRENT", "Commodities"), ("GOLD", "Commodities"),
    ("VIX", "Risk"),
]


def _home_market() -> dict:
    board = store.cache_get("market") or {}
    by = {r.get("k"): r for r in (board.get("rows") or [])}
    rows = []
    for k, group in HOME_ROWS:
        r = by.get(k)
        if not r or r.get("last") is None:
            continue
        is_rate = r.get("u") == "pct"
        rows.append({
            "g": group, "k": k, "n": r.get("n"), "u": r.get("u"),
            "price": r.get("last"),
            # A ten-year going 4.00 to 4.10 is +10bp, not +2.5%. The board
            # already makes this distinction; the home strip was dropping it.
            "chg_pct": None if is_rate else r.get("d_pct"),
            "chg_bp": round(r["d_abs"] * 100) if is_rate and r.get("d_abs") is not None else None,
            "asof": r.get("asof"), "stale": bool(r.get("stale")),
        })
    if rows:
        return {"rows": rows, "src": "board",
                "age_s": round(store.cache_age("market")), "note": None}

    # Nothing cached yet — the thread starts a few seconds after the app does.
    # Fall back to a direct read so the first page load is not empty, and say
    # which of the two states this is rather than rendering an empty box.
    live = pricing.market_board()
    if any(r.get("price") is not None for r in live):
        return {"rows": live, "src": "live", "age_s": 0, "note": None}
    return {"rows": live, "src": "none", "age_s": None,
            "note": ("the board has not been built yet — it is assembled a few "
                     "seconds after the app starts"
                     if not by else
                     "the price feed is not answering; MKT's Sources panel says why")}


@router.get("/home", response_class=Safe)
def home():
    """
    The home screen's portfolio half.

    This builds the FULL dashboard, curve included, which the light version
    skipped — and skipping it was why year-to-date read "open PORT to compute"
    here while PORT itself showed a number. With a warm price cache the curve
    costs about two tenths of a second, and this route is synchronous so it
    runs in a worker thread rather than on the event loop.
    """
    brief.BRIEF.start()
    mk = _home_market()
    d = service.dashboard("SP500")
    if d.get("empty"):
        return {"empty": True, "market": mk["rows"], "market_meta": mk}
    return {
        "asof": d["asof"],
        "totals": d["totals"],
        "concentration": d.get("concentration"),
        "changed": brief.what_changed(d["positions"]),
        "alerts": service.alerts(d),
        "market": mk["rows"],
        "market_meta": {k: v for k, v in mk.items() if k != "rows"},
        "data_quality": d.get("data_quality"),
        "curve_note": {"mostly_carried": d["curve"].get("mostly_carried"),
                       "ytd_coverage_pct": d["totals"].get("ytd_coverage_pct")},
    }


# --------------------------------------------------------------------------
# news, calendars, alert rules
# --------------------------------------------------------------------------

@router.get("/brief", response_class=Safe)
def get_brief():
    """
    News, the macro week, the earnings week and today's preview.

    Deliberately SEPARATE from /home so a slow or broken news provider can
    never delay the portfolio figures. The page asks for both at once and
    renders whichever arrives first.
    """
    brief.BRIEF.start()
    return brief.BRIEF.get()


@router.post("/brief/refresh", response_class=Safe)
async def refresh_brief(what: str = Query("all")):
    if what not in ("all", "news", "macro", "earnings"):
        raise HTTPException(400, "what must be all, news, macro or earnings")
    return {"refreshed": await run_in_threadpool(brief.BRIEF.refresh_now, what)}


@router.get("/market/board", response_class=Safe)
def market_board():
    """
    The MKT board. Served from the cache the background thread fills, so a
    slow rate feed can never hold up the page.
    """
    brief.BRIEF.start()
    return brief.BRIEF.market()


@router.post("/market/refresh", response_class=Safe)
async def refresh_market(what: str = Query("market")):
    if what not in ("market", "rates", "all"):
        raise HTTPException(400, "what must be market, rates or all")
    return {"refreshed": await run_in_threadpool(brief.BRIEF.refresh_now, what)}


@router.get("/geo", response_class=Safe)
def geo_board():
    """Risks joined to the book, chokepoints, elections and the shock table."""
    brief.BRIEF.start()
    return brief.BRIEF.geo()


@router.get("/sentiment", response_class=Safe)
def sentiment_board():
    """Fear & Greed (CNN and house), the next Fed move, and the GPR index."""
    brief.BRIEF.start()
    s = store.cache_get("sentiment")
    if not s:
        return {"error": "not fetched yet",
                "running": bool(brief.BRIEF._thread and brief.BRIEF._thread.is_alive())}
    s["age_s"] = round(store.cache_age("sentiment"))
    return s


@router.post("/geo/refresh", response_class=Safe)
async def refresh_geo(what: str = Query("geo")):
    if what not in ("geo", "sentiment", "all"):
        raise HTTPException(400, "what must be geo, sentiment or all")
    return {"refreshed": await run_in_threadpool(brief.BRIEF.refresh_now, what)}


@router.get("/position/{key}", response_class=Safe)
def position_detail(key: str):
    """One holding in full — for the PORT popup and the per-ticker page."""
    d = service.position_detail(key)
    if d.get("error"):
        raise HTTPException(404, d["error"])
    return d


# --------------------------------------------------------------------------
# research: one security, held or not (build 18)
# --------------------------------------------------------------------------

def _research(sym: str, fn):
    """Run one research part. A missing symbol is a 404; anything else is
    reported IN the payload so one broken panel never blanks the page."""
    s = research.resolve(sym)
    try:
        return fn(s)
    except research.NotFound as e:
        raise HTTPException(404, str(e))
    except HTTPException:
        raise
    except Exception as e:
        return {"symbol": s, "error": f"{e.__class__.__name__}: {e}"[:300]}


class ThesisBody(BaseModel):
    stance: str | None = None
    conviction: int | None = None
    target: float | None = None
    horizon: str | None = None
    review_date: str | None = None
    thesis: str = ""
    drivers: str = ""
    risks: str = ""
    kill: str = ""


@router.get("/morning/claude", response_class=Safe)
def morning_claude(d: str = Query(...)):
    """Claude's write-ups of a brief, the writer's state, and the job while it writes.
    Separate from /morning because asking Claude Code whether it is signed in
    takes seconds, and the brief itself should not wait for that."""
    return morning.note_state(_day(d))


class JournalBody(BaseModel):
    body: str


class PeersBody(BaseModel):
    peers: list[str] = []


@router.get("/research/{sym}/intraday", response_class=Safe)
def research_intraday(sym: str, range: str = Query("1D")):
    return _research(sym, lambda s: research.intraday(s, range.upper()))


@router.get("/research/{sym}/day/{d}", response_class=Safe)
def research_day(sym: str, d: str):
    import re as _re
    if not _re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    return _research(sym, lambda s: research.day(s, d))


@router.get("/research/{sym}/thesis", response_class=Safe)
def research_thesis(sym: str):
    return research.thesis_get(research.resolve(sym))


@router.post("/research/{sym}/thesis", response_class=Safe)
def research_thesis_save(sym: str, body: ThesisBody):
    try:
        return research.thesis_save(research.resolve(sym), body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/research/{sym}/note", response_class=Safe)
def research_note_add(sym: str, body: JournalBody):
    try:
        return research.note_add(research.resolve(sym), body.body)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/research/note/{note_id}", response_class=Safe)
def research_note_delete(note_id: int):
    if not research.note_delete(note_id):
        raise HTTPException(404, "no such note")
    return {"ok": True}


@router.post("/research/{sym}/peers", response_class=Safe)
def research_peers_set(sym: str, body: PeersBody):
    return research.set_peers(research.resolve(sym), body.peers)


# --------------------------------------------------------------------------
# READ: the day's live coverage, and articles written for you (build 43)
# --------------------------------------------------------------------------

class ReadWriteBody(BaseModel):
    title: str
    url: typing.Optional[str] = None
    why: typing.Optional[str] = None


class ReadAskBody(BaseModel):
    question: str


class ReadAutoBody(BaseModel):
    on: bool


@router.get("/read", response_class=Safe)
def read_overview():
    from . import reader
    return reader.overview()


@router.get("/read/home", response_class=Safe)
def read_home():
    from . import reader
    return reader.home()


@router.get("/read/live", response_class=Safe)
def read_live():
    from . import reader
    return reader.live()


@router.get("/read/job", response_class=Safe)
def read_job():
    from . import reader
    return reader._pub(reader.JOB) or {"state": "idle"}


@router.get("/read/a/{aid}", response_class=Safe)
def read_article(aid: int):
    from . import reader
    a = reader.article(aid)
    if not a:
        raise HTTPException(404, "no such article")
    return a


def _read_start(fn):
    from . import reader
    why = reader.writer_ready()
    if why:
        raise HTTPException(409, why)
    return fn()


@router.post("/read/scan", response_class=Safe)
def read_scan():
    """Scan and write now, within today's budget: the same run the schedule starts."""
    from . import reader
    return _read_start(lambda: reader.start_scan("manual"))


@router.post("/read/write", response_class=Safe)
def read_write(body: ReadWriteBody):
    """An article on a story the editor passed over, because you asked."""
    from . import reader
    return _read_start(lambda: reader.write_this(body.title, body.url, body.why or ""))


@router.post("/read/a/{aid}/ask", response_class=Safe)
def read_ask(aid: int, body: ReadAskBody):
    from . import reader
    try:
        return reader.ask(aid, body.question)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except PermissionError as e:
        raise HTTPException(409, str(e))


@router.get("/read/a/{aid}/answers", response_class=Safe)
def read_answers(aid: int):
    """The answers to an article, and the one being written: light enough to poll."""
    from . import reader
    return {"answers": reader.answers(aid), "job": reader._pub(reader.ASK_JOBS.get(aid))}


@router.post("/read/auto", response_class=Safe)
def read_auto(body: ReadAutoBody):
    """The schedule on or off: off, nothing is written unless you press Scan."""
    store.set_setting("read_auto", "1" if body.on else "0")
    return {"auto": body.on}


# --------------------------------------------------------------------------
# KAI: Koers AI, the paper-trading contest against you and the index (build 47)
# --------------------------------------------------------------------------

class KaiAutoBody(BaseModel):
    on: bool


@router.get("/kai", response_class=Safe)
def kai_overview():
    from . import koersai
    return koersai.overview()


@router.get("/kai/job", response_class=Safe)
def kai_job():
    from . import koersai
    return koersai._pub(koersai.JOB) or {"state": "idle"}


@router.get("/kai/decision/{did}", response_class=Safe)
def kai_decision(did: int):
    from . import koersai
    d = koersai.decision(did)
    if not d:
        raise HTTPException(404, "no such decision")
    return d


@router.post("/kai/start", response_class=Safe)
def kai_start():
    """Start the contest with €100,000 of paper money now, and its first decision at once."""
    from . import koersai, reader
    why = reader.writer_ready()
    if why:
        raise HTTPException(409, why)
    try:
        koersai.start()
    except ValueError as e:
        raise HTTPException(409, str(e))
    # The first decision reads the watchlist's scan: on an evening before the scan has
    # read the close, the scheduler starts it once the scan is done.
    if koersai.first_due():
        return koersai.start_decision("start")
    return {"state": "waiting"}


@router.post("/kai/decide", response_class=Safe)
def kai_decide():
    """A decision now, outside the schedule."""
    from . import koersai, reader
    if not koersai.started():
        raise HTTPException(409, "Koers AI has not started")
    why = reader.writer_ready()
    if why:
        raise HTTPException(409, why)
    return koersai.start_decision("manual")


@router.get("/kai/review/{rid}", response_class=Safe)
def kai_review_one(rid: int):
    """One monthly self-review in full, with what it was sent (build 56)."""
    from . import koersai
    r = koersai.review_one(rid)
    if not r:
        raise HTTPException(404, "no such review")
    return r


@router.post("/kai/review", response_class=Safe)
def kai_review_now():
    """A self-review now, of the days since the last one (or the start) to yesterday: one run on the plan."""
    from datetime import timedelta
    from . import koersai, reader
    if not koersai.started():
        raise HTTPException(409, "Koers AI has not started")
    why = reader.writer_ready()
    if why:
        raise HTTPException(409, why)
    d0, d1 = koersai.review_period(koersai.now_ams().date() - timedelta(days=1))
    if koersai.ny_sessions(d0, d1) < 3:
        raise HTTPException(409, "fewer than three sessions since the last review")
    return koersai.start_review("manual", d0, d1)


@router.post("/kai/auto", response_class=Safe)
def kai_auto(body: KaiAutoBody):
    """The schedule on or off: off, Koers AI decides only when you press Decide now."""
    store.set_setting("kai_auto", "1" if body.on else "0")
    return {"auto": body.on}


# --------------------------------------------------------------------------
# POL: politicians' trades, from the user's own tracker in backend/ptt (build 49)
# --------------------------------------------------------------------------

@router.get("/pol", response_class=Safe)
def pol_overview():
    from . import pol
    return pol.overview()


@router.get("/pol/filings", response_class=Safe)
def pol_filings(year: int):
    """Every trade filed in one year: the page's year switch (this year's comes with /pol)."""
    from . import pol
    if not 2000 <= year <= 2100:
        raise HTTPException(400, "a year, like 2025")
    return pol.filings(year)


@router.get("/pol/job", response_class=Safe)
def pol_job():
    from . import pol
    return {**pol._pub(pol.JOB), "lock": pol.busy(), "last": pol.status().get("last")}


@router.get("/pol/member/{bg}", response_class=Safe)
def pol_member(bg: str):
    from . import pol
    m = pol.member(bg)
    if not m:
        raise HTTPException(404, "no such member")
    return m


@router.get("/pol/committee/{cid}", response_class=Safe)
def pol_committee(cid: str):
    from . import pol
    c = pol.committee(cid)
    if not c:
        raise HTTPException(404, "no such committee")
    return c


@router.get("/pol/ticker/{sym}", response_class=Safe)
def pol_ticker(sym: str):
    from . import pol
    return pol.ticker(sym)


@router.post("/pol/build", response_class=Safe)
def pol_build():
    """The first build (every filing since 2020, some hours) when there is none, else a nightly run now."""
    from . import pol
    if pol.busy():
        raise HTTPException(409, "a run of the tracker is already going")
    return pol.start_run("nightly" if (pol.status().get("done") or {}).get("first") else "first")


# --------------------------------------------------------------------------
# FLOW: what institutions are doing (build 51): up and down volume, hedge
# funds' futures, their leverage, dark pools and options, your holdings
# --------------------------------------------------------------------------

@router.get("/flow", response_class=Safe)
def flow_view():
    from . import flow
    return flow.view()


@router.get("/flow/job", response_class=Safe)
def flow_job():
    """The collector's state: light enough to poll while a refresh runs."""
    from . import flow
    return {k: flow.JOB.get(k) for k in ("state", "phase", "started", "finished", "error", "results")}


@router.post("/flow/refresh", response_class=Safe)
def flow_refresh():
    """Every source now, ahead of the evening's schedule; Cboe's and FINRA's back files in larger steps."""
    from . import flow
    return flow.start(force=True)


# --------------------------------------------------------------------------
# SYS, the terminal's own health, and WEEK, the week in review (build 53)
# --------------------------------------------------------------------------

@router.get("/sys", response_class=Safe)
def sys_view():
    from . import health
    return health.view()


@router.get("/week", response_class=Safe)
def week_view(wk: str | None = Query(None, description="the week's Monday, YYYY-MM-DD; the latest when left out")):
    from . import weekly
    if wk and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", wk):
        raise HTTPException(400, "a week is its Monday, like 2026-11-02")
    return weekly.view(wk)


@router.get("/week/job", response_class=Safe)
def week_job(wk: str | None = Query(None)):
    from . import weekly
    w = wk or (weekly.weeks()[:1] or [weekly.last_week().isoformat()])[0]
    return {"build": dict(weekly.JOB), "note": weekly._pub(weekly.NOTE_JOBS.get(w)), "week": w}


@router.post("/week/build", response_class=Safe)
def week_build(wk: str | None = Query(None)):
    """The review again now: the last complete week, or the week given (its Monday)."""
    from . import weekly
    from datetime import date as _date
    try:
        mon = _date.fromisoformat(wk) if wk else None
    except ValueError:
        raise HTTPException(400, "a week is its Monday, like 2026-11-02")
    if mon and mon.weekday() != 0:
        raise HTTPException(400, "a week is its Monday")
    return weekly.start_build(mon)


@router.post("/week/note", response_class=Safe)
def week_note(wk: str = Query(...)):
    """Claude's note on a week's review, written now (the Saturday run writes one by itself)."""
    from . import weekly
    try:
        return weekly.start_note(wk)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))


# --------------------------------------------------------------------------
# IDX: the S&P 500 and the Nasdaq 100 from the inside (build 40)
# --------------------------------------------------------------------------

@router.get("/idx/compare", response_class=Safe)
def idx_compare():
    from . import idx
    try:
        return idx.compare()
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:200]}


@router.get("/idx/{ix}", response_class=Safe)
def idx_view(ix: str, rebuild: bool = False):
    from . import idx
    try:
        return idx.view(ix.lower(), rebuild)
    except KeyError:
        raise HTTPException(404, "the index is spx or ndx")


@router.get("/idx/{ix}/book", response_class=Safe)
def idx_book(ix: str):
    from . import idx
    if ix.lower() not in idx.INDICES:
        raise HTTPException(404, "the index is spx or ndx")
    try:
        return store.cache_through(f"idx:book:{ix.lower()}", 600, lambda: idx.book(ix.lower()))
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:200]}


# --------------------------------------------------------------------------
# FCF: what a share's free cash flow says it is worth (build 39)
# --------------------------------------------------------------------------

class FcfBody(BaseModel):
    growth: typing.Optional[float] = None
    multiple: typing.Optional[float] = None
    ret: typing.Optional[float] = None
    base: typing.Optional[str] = None
    horizon: typing.Optional[int] = None
    listed: typing.Optional[bool] = None


@router.get("/fcf", response_class=Safe)
def fcf_table(refresh: bool = False):
    from . import fcfmodel
    return fcfmodel.table(refresh)


@router.get("/fcf/{sym}", response_class=Safe)
def fcf_one(sym: str):
    from . import fcfmodel
    return _research(sym, lambda s: fcfmodel.model(s))


@router.post("/fcf/{sym}", response_class=Safe)
def fcf_save(sym: str, body: FcfBody):
    """Only the fields sent change; a field sent as null goes back to the company's own."""
    from . import fcfmodel
    s = research.resolve(sym)
    _research(s, lambda x: fcfmodel.inputs(x))      # a symbol that does not exist is a 404, and not saved
    try:
        fcfmodel.save(s, body.model_dump(exclude_unset=True))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return _research(s, lambda x: fcfmodel.model(x))


@router.get("/research/{sym}/consensus", response_class=Safe)
def research_consensus(sym: str):
    """The day-by-day record of what analysts expected (pf/consensus.py)."""
    from . import consensus
    s = research.resolve(sym)
    ccy = (_safe_ov(s) or {}).get("currency")
    return {**consensus.history(s, consensus.siblings(s), ccy), "kept": consensus.count()}


def _safe_ov(s: str):
    try:
        return research.overview(s)
    except Exception:
        return None


@router.get("/research/{sym}/{part}", response_class=Safe)
def research_part(sym: str, part: str):
    fn = research.PARTS.get(part)
    if not fn:
        raise HTTPException(404, f"no research part {part!r}; one of {sorted(research.PARTS)}")
    return _research(sym, fn)


@router.get("/theses", response_class=Safe)
def list_theses():
    return research.theses()


# --------------------------------------------------------------------------
# bonds: the bond market beside MKT (build 19)
# --------------------------------------------------------------------------

@router.get("/bonds/history", response_class=Safe)
def bonds_history(keys: str = Query(..., description="comma-separated series ids"),
                  start: str = Query("2000-01-01")):
    """Daily series for the charts, thinned to weekly or monthly for long ranges."""
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
        raise HTTPException(400, "start must be YYYY-MM-DD")
    try:
        return bonds.history([k.strip() for k in keys.split(",") if k.strip()], start)
    except Exception as e:
        return {"series": {}, "error": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/bonds/sources", response_class=Safe)
def bonds_sources():
    return bonds.sources()


@router.get("/bonds/{part}", response_class=Safe)
def bonds_part(part: str):
    """One panel's worth. Each fails alone, with the reason in the payload."""
    if part not in bonds.PARTS:
        raise HTTPException(404, f"no bonds part {part!r}; one of {sorted(bonds.PARTS)}")
    try:
        return bonds.part(part)
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:300]}


# --------------------------------------------------------------------------
# earnings: ERN (build 20)
# --------------------------------------------------------------------------

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _d(d: str | None) -> str | None:
    if d and not _DATE.match(d):
        raise HTTPException(400, "date must be YYYY-MM-DD")
    return d


@router.get("/earnings/week", response_class=Safe)
def earnings_week(week: str | None = Query(None, description="any day in the week, YYYY-MM-DD"),
                  scope: str = Query("market")):
    if scope not in ("mine", "market", "all"):
        raise HTTPException(400, "scope must be mine, market or all")
    earnlive.ensure()
    try:
        w = earnings.week(_d(week), scope)
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:300], "days": []}
    # Live watches mark their events on the grid.
    live = {(x["symbol"], x["date"]): x for x in earnlive.active()}
    for day in w["days"]:
        for e in day["events"]:
            lv = live.get((e["sym"], e["date"]))
            if lv:
                e["live"] = {"status": lv["status"], "found": bool(lv.get("found"))}
    return w


@router.get("/earnings/exposure", response_class=Safe)
def earnings_exposure(week: str | None = Query(None)):
    """What options price for the holdings reporting in a week, in euros."""
    try:
        return earnings.exposure(_d(week))
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:300], "rows": []}


@router.get("/earnings/upcoming", response_class=Safe)
def earnings_upcoming(days: int = Query(100, ge=1, le=200)):
    return {"events": earnings.upcoming_mine(days)}


@router.get("/earnings/season", response_class=Safe)
def earnings_season():
    try:
        return earnings.season()
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/earnings/calendar.ics")
def earnings_ics():
    from fastapi.responses import Response
    return Response(earnings.ics(), media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="earnings.ics"'})


@router.get("/earnings/live", response_class=Safe)
def earnings_live():
    return {"watches": earnlive.active(), "thread": earnlive.status()}


@router.post("/earnings/live/{sym}", response_class=Safe)
def earnings_live_start(sym: str, d: str = Query(...)):
    ev = earnings.resolve(sym, _d(d))
    if not ev.get("ok"):
        raise HTTPException(404, ev.get("why") or "no such event")
    # A manual watch starts now and runs four hours; the schedule decides
    # nothing here — you asked.
    earnings.take_snapshot(ev["symbol"], ev["date"], force=True)
    return earnlive.start(ev["symbol"], ev["date"], ev.get("name"), ev.get("when"),
                          ev.get("ts") if ev.get("time_exact") else None, manual=True)


@router.delete("/earnings/live/{sym}", response_class=Safe)
def earnings_live_stop(sym: str, d: str = Query(...)):
    return {"ok": earnlive.stop(earnings.ysym(sym), _d(d))}


@router.get("/earnings/live/{sym}", response_class=Safe)
def earnings_live_one(sym: str, d: str = Query(...)):
    return {"watch": earnlive.state(earnings.ysym(sym), _d(d))}


class WatchBody(BaseModel):
    on: bool = True


@router.post("/earnings/watch/{sym}", response_class=Safe)
def earnings_watch(sym: str, body: WatchBody):
    return {"watchlist": earnings.set_watch(sym, body.on)}


class SourceBody(BaseModel):
    kind: str
    url: str | None = None
    body: str | None = None
    title: str | None = None


@router.post("/earnings/sources/{sym}", response_class=Safe)
def earnings_source_add(sym: str, body: SourceBody, d: str = Query(...)):
    from . import earndocs
    try:
        r = earndocs.add_source(earnings.ysym(sym), _d(d), body.kind, body.url, body.body, body.title)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"could not read that link: {e.__class__.__name__}: {e}"[:300])
    _forget_event(earnings.ysym(sym), d)
    return r


@router.delete("/earnings/sources/{sid}", response_class=Safe)
def earnings_source_delete(sid: int, sym: str = Query(...), d: str = Query(...)):
    from . import earndocs
    ok = earndocs.delete_source(sid)
    _forget_event(earnings.ysym(sym), _d(d))
    return {"ok": ok}


def _forget_event(sym: str, d: str):
    """A source was added or removed: the parts built from the old set are stale."""
    with store.conn() as c:
        for p in ("docs", "call", "analysis", "results"):
            c.execute("DELETE FROM cache WHERE k=?", (f"ern:{p}:{sym}:{d}",))


class ReportBody(BaseModel):
    question: str | None = None
    web: bool = False


@router.post("/earnings/report/{sym}", response_class=Safe)
def earnings_report_start(sym: str, body: ReportBody, d: str = Query(...)):
    from . import earnai
    q = (body.question or "").strip() or None
    if q and len(q) > 2000:
        raise HTTPException(400, "keep the question under 2,000 characters")
    try:
        return earnai.start(earnings.ysym(sym), _d(d), q, body.web)
    except PermissionError as e:
        raise HTTPException(400, str(e))


@router.get("/earnings/claude-code", response_class=Safe)
def earnings_cc_status():
    """Is Claude Code on this computer, and signed in to your Claude plan?"""
    from . import earnai
    return earnai.cc_status(force=True)


@router.post("/earnings/claude-code/login", response_class=Safe)
def earnings_cc_login():
    """Open the one-time sign-in in a window of its own; you finish it in the browser."""
    from . import earnai
    try:
        return earnai.cc_login()
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))


@router.delete("/earnings/report/{rid}", response_class=Safe)
def earnings_report_delete(rid: int):
    from . import earnai
    return {"ok": earnai.delete(rid)}


@router.get("/earnings/event/{sym}", response_class=Safe)
def earnings_event(sym: str, d: str | None = Query(None)):
    earnlive.ensure()
    try:
        return earnings.resolve(sym, _d(d))
    except Exception as e:
        return {"ok": False, "symbol": sym.upper(), "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/earnings/event/{sym}/{part}", response_class=Safe)
def earnings_event_part(sym: str, part: str, d: str | None = Query(None)):
    """One part of an event. Each fails alone, with the reason in the payload."""
    if part not in earnings.PARTS:
        raise HTTPException(404, f"no earnings part {part!r}; one of {list(earnings.PARTS)}")
    try:
        return earnings.part(sym, part, _d(d))
    except Exception as e:
        return {"ok": False, "error": f"{e.__class__.__name__}: {e}"[:300]}


# --------------------------------------------------------------------------
# ALRT: the morning brief
# --------------------------------------------------------------------------

def _day(d: str | None) -> str | None:
    if d is None:
        return None
    try:
        from datetime import date as _date
        return _date.fromisoformat(d[:10]).isoformat()
    except ValueError:
        raise HTTPException(400, "d must be a date, YYYY-MM-DD")


@router.get("/morning", response_class=Safe)
def morning_view(d: str | None = Query(None)):
    """The brief for a morning (today's, or tomorrow's after 22:30). Built on first ask."""
    morning.ensure()
    return morning.view(_day(d))


@router.post("/morning/build", response_class=Safe)
def morning_build(d: str | None = Query(None)):
    """Build it again now: fresh prices, news and calendars."""
    return morning.start_build(_day(d) or morning.brief_day().isoformat(), "refresh")


@router.get("/morning/latest", response_class=Safe)
def morning_latest():
    """Two lines for the home screen."""
    morning.ensure()
    return morning.latest_summary()


class NoteBody(BaseModel):
    question: str | None = None
    web: bool = False


@router.post("/morning/note", response_class=Safe)
def morning_note(body: NoteBody, d: str = Query(...)):
    """Claude's version of the brief, or an answer about it."""
    q = (body.question or "").strip() or None
    if q and len(q) > 2000:
        raise HTTPException(400, "keep the question under 2,000 characters")
    try:
        return morning.start_note(_day(d), q, body.web)
    except (PermissionError, LookupError) as e:
        raise HTTPException(400, str(e))


@router.delete("/morning/note/{nid}", response_class=Safe)
def morning_note_delete(nid: int):
    return {"ok": morning.delete_note(nid)}


# --------------------------------------------------------------------------
# OPT: what the options market says (build 22)
# --------------------------------------------------------------------------

@router.get("/options", response_class=Safe)
def options_overview():
    """Every holding with an option chain: the last stored reading of each."""
    from . import options
    options.ensure()
    try:
        return options.overview()
    except Exception as e:
        return {"rows": [], "say": [], "error": f"{e.__class__.__name__}: {e}"[:300]}


@router.post("/options/collect", response_class=Safe)
def options_collect():
    """Read every chain now, in the background, one a second."""
    from . import options
    return options.collect()


@router.get("/options/{sym}", response_class=Safe)
def options_one(sym: str, fresh: bool = Query(False)):
    """The surface, the range, the history and the flow for one underlying."""
    from . import options
    options.ensure()
    try:
        return options.build(research.resolve(sym), force=fresh)
    except Exception as e:
        return {"available": False, "sym": sym.upper(), "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/options/{sym}/chain", response_class=Safe)
def options_chain(sym: str, exp: str = Query(...)):
    """One expiry as a table."""
    from . import options
    try:
        return options.chain_rows(research.resolve(sym), _d(exp))
    except options.NoOptions as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        return {"rows": [], "error": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/options/{sym}/contract", response_class=Safe)
def options_contract(sym: str, exp: str = Query(...), k: float = Query(..., gt=0),
                     type: str = Query("C", pattern="^[CPcp]$")):
    """One call or put worked out: premium, payoff, break-even, odds, how it moves."""
    from . import options
    try:
        return options.contract(research.resolve(sym), _d(exp), k, type.upper() == "C")
    except options.NoOptions as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:300]}


# --------------------------------------------------------------------------
# SPLC: who sells to whom, in the companies' own words (build 22)
# --------------------------------------------------------------------------

@router.get("/supply", response_class=Safe)
def supply_chain():
    """Your book laid out along its supply links."""
    from . import supply
    try:
        return supply.chain()
    except Exception as e:
        return {"ok": False, "nodes": [], "edges": [], "alone": [], "shares": [], "say": [],
                "error": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/supply/{sym}", response_class=Safe)
def supply_company(sym: str):
    """One company between its suppliers and customers, with the sentence behind every line."""
    from . import supply
    try:
        return supply.company(sym)
    except Exception as e:
        return {"ok": False, "key": sym.upper(), "error": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/supply/{sym}/mentions", response_class=Safe)
def supply_mentions(sym: str):
    """The other companies whose annual reports name this one (SEC full-text search)."""
    from . import supply
    return supply.mentions(sym)


@router.get("/supply/{sym}/passages", response_class=Safe)
def supply_passages(sym: str):
    """What a reader is given: the sentences cut from the company's filings."""
    from . import supply
    try:
        m = supply.material(sym)
    except Exception as e:
        return {"ok": False, "items": [], "docs": [], "error": f"{e.__class__.__name__}: {e}"[:300]}
    return {"ok": True, "key": m["key"], "sym": m["sym"], "label": m["label"], "docs": m["docs"],
            "items": [{"n": p["n"], "d": p["d"], "text": p["text"]} for p in m["items"]]}


@router.post("/supply/{sym}/read", response_class=Safe)
def supply_read(sym: str):
    """Have Claude read the company's filings: your plan or your key, as set for the reports."""
    from . import supply
    try:
        return {"ok": True, "job": supply.start_read(sym)}
    except (PermissionError, LookupError) as e:
        raise HTTPException(409, str(e))


@router.get("/supply/{sym}/read", response_class=Safe)
def supply_read_state(sym: str):
    from . import supply
    key = supply.key_of(sym)
    return {"ok": True, "key": key, "job": supply._pub(supply.JOBS.get(key))}


@router.delete("/supply/{sym}/read", response_class=Safe)
def supply_forget(sym: str):
    """Drop what Claude read for this company since the map shipped."""
    from . import supply
    return supply.forget(sym)


@router.get("/diagnostics", response_class=Safe)
async def diagnostics():
    """Call every external source for real and report what came back."""
    from . import feeds
    return await run_in_threadpool(feeds.self_test)


@router.get("/news", response_class=Safe)
def all_news(limit: int = Query(80, ge=1, le=300)):
    brief.BRIEF.start()
    p = brief.BRIEF.all_news()
    return {**p, "items": (p.get("items") or [])[:limit],
            "age_s": round(store.cache_age("news"))}


class Settings(BaseModel):
    news_api_key: str | None = None
    news_provider: str | None = None
    fred_api_key: str | None = None
    anthropic_api_key: str | None = None
    aisstream_api_key: str | None = None
    ern_model: str | None = None
    ern_writer: str | None = None
    ern_cc_model: str | None = None
    alrt_auto_claude: bool | None = None


@router.get("/settings", response_class=Safe)
def get_settings():
    """The key itself is never returned — only whether one is set."""
    from . import feeds
    k, f = news.api_key(), feeds.fred_key()
    hint = lambda v: (v[:4] + "…" + v[-3:]) if v and len(v) > 8 else None
    from . import earnai, ais
    a = earnai.api_key()
    ak = ais.api_key()
    return {"has_news_key": bool(k), "news_key_hint": hint(k),
            "has_ais_key": bool(ak), "ais_key_hint": hint(ak),
            "news_provider": news.provider_name(),
            "providers": ["finnhub", "yahoo"],
            "has_fred_key": bool(f), "fred_key_hint": hint(f),
            "has_anthropic_key": bool(a), "anthropic_key_hint": hint(a),
            "ern_model": earnai.model(),
            "ern_writer": earnai.writer(), "ern_writers": earnai.WRITERS,
            "ern_cc_model": earnai.cc_model(), "ern_cc_models": earnai.CC_MODELS,
            "alrt_auto_claude": store.get_setting("alrt_auto_claude") == "1",
            "ern_models": {m: {"label": v[0], "in": v[1], "out": v[2]} for m, v in earnai.MODELS.items() if m == earnai.WRITE_MODEL}}


@router.post("/settings", response_class=Safe)
def post_settings(body: Settings):
    if body.news_provider is not None:
        if body.news_provider not in ("finnhub", "yahoo"):
            raise HTTPException(400, "provider must be finnhub or yahoo")
        store.set_setting("news_provider", body.news_provider)
    if body.news_api_key is not None:
        store.set_setting("news_api_key", body.news_api_key.strip())
    if body.fred_api_key is not None:
        store.set_setting("fred_api_key", body.fred_api_key.strip())
    if body.anthropic_api_key is not None:
        store.set_setting("anthropic_api_key", body.anthropic_api_key.strip())
    if body.aisstream_api_key is not None:
        store.set_setting("aisstream_api_key", body.aisstream_api_key.strip())
    if body.ern_model is not None or body.ern_cc_model is not None:
        # The model is pinned (earnai.WRITE_MODEL, high effort): whatever a page sends, that is what is kept.
        from . import earnai
        store.set_setting("ern_model", earnai.WRITE_MODEL)
        store.set_setting("ern_cc_model", earnai.WRITE_MODEL)
    if body.ern_writer is not None:
        from . import earnai
        if body.ern_writer not in earnai.WRITERS:
            raise HTTPException(400, f"writer must be one of {sorted(earnai.WRITERS)}")
        store.set_setting("ern_writer", body.ern_writer)
    if body.alrt_auto_claude is not None:
        store.set_setting("alrt_auto_claude", "1" if body.alrt_auto_claude else "0")
    return get_settings()


class Rule(BaseModel):
    kind: str
    value: float
    isin: str | None = None
    ticker: str | None = None
    note: str = ""


@router.get("/alerts/rules", response_class=Safe)
def list_rules():
    return {"kinds": list(service.RULE_KINDS), "rules": store.list_rules()}


@router.post("/alerts/rules", response_class=Safe)
def add_rule(body: Rule):
    if body.kind not in service.RULE_KINDS:
        raise HTTPException(400, f"kind must be one of {service.RULE_KINDS}")
    if not (body.isin or body.ticker):
        raise HTTPException(400, "a rule needs an isin or a ticker")
    rid = store.add_rule(body.isin, (body.ticker or "").upper() or None,
                         body.kind, body.value, body.note)
    return {"ok": True, "id": rid}


@router.delete("/alerts/rules/{rule_id}", response_class=Safe)
def delete_rule(rule_id: int):
    if not store.delete_rule(rule_id):
        raise HTTPException(404, "no such rule")
    return {"ok": True}


# --------------------------------------------------------------------------
# live prices
# --------------------------------------------------------------------------

_BOARD_SYMS = set(market.YAHOO_SYMBOLS)


@router.get("/live/status", response_class=Safe)
def live_status():
    """Is the feed streaming, polling, or off — and when did it last tick."""
    s = stream.STREAM.snapshot()
    return {"state": s["state"], "detail": s["detail"], "symbols": s["symbols"],
            "version": s["version"], "last_tick": s["last_tick"],
            "market_open": stream.market_open(s["quotes"]),
            "poll_seconds": stream.POLL_SECONDS}


@router.get("/live")
async def live(request: Request):
    """
    Server-sent events: the day change, recomputed whenever a price moves.

    SSE rather than a websocket back to the browser, because this is one-way —
    the page never sends anything — and SSE reconnects on its own, survives a
    laptop lid closing, and needs no extra dependency on either end.

    The base is rebuilt every few minutes rather than per tick: a transaction
    added in another tab should appear without a reload, but replaying the
    ledger for every price change would be absurd.
    """
    import asyncio

    async def gen():
        # Both acquire() and release() live inside the generator so they are
        # always paired. Acquiring in the handler and releasing here meant a
        # client that went away before the body was consumed left the feed
        # running for a viewer that no longer existed.
        stream.STREAM.acquire()
        try:
            # NOTHING BLOCKING ON THE EVENT LOOP. Building the base replays
            # the ledger and calls the quote API, which takes as long as the
            # network takes. Run on the loop it froze the entire server —
            # page loads, stylesheet, every other request — for its duration,
            # once per connect and again every five minutes. That was the
            # freeze. The background thread owns it now; this only waits for
            # the first one, and only off the loop.
            base = stream.STREAM.base()
            if base is None:
                base = await run_in_threadpool(stream.STREAM.refresh_base)
            if base is None:
                yield ("data: " + json.dumps({
                    "state": "error", "detail": stream.STREAM.base_error
                    or "could not read the portfolio", "positions": [],
                    "totals": None}) + "\n\n")
                return

            last_version, last_beat = -1, 0.0
            while True:
                if await request.is_disconnected():
                    break
                now = time.time()
                snap = stream.STREAM.snapshot()

                # Picked up whenever the background thread has a newer one.
                fresh = stream.STREAM.base()
                if fresh is not None:
                    base = fresh

                changed = snap["version"] != last_version
                # A heartbeat every 15s even when nothing moved, so the page
                # can tell "connected, market quiet" from "connection died".
                if changed or now - last_beat > 15:
                    from . import sessions
                    states = {ex: sessions.state(ex) for ex in base.get("exchanges") or []}
                    payload = stream.fold(base, snap["quotes"], states)
                    # the day's low and high, from what the feed shows (pf/dayrange.py)
                    from . import dayrange
                    if snap["state"] in ("streaming", "polling") and payload.get("live_rows"):
                        mv = dayrange.today_move(payload["positions"], dayrange.today(now))
                        payload["totals"]["today_move"] = mv
                        payload["day_range"] = dayrange.note(mv, now)
                    else:
                        payload["day_range"] = dayrange.get()
                    payload.update({
                        "state": snap["state"], "detail": snap["detail"],
                        # How many symbols the streamer holds. MKT's Sources
                        # panel reports the feed from the tick rather than from
                        # the board payload, which is fetched before this
                        # connection exists and therefore always said "off".
                        "symbols": snap.get("symbols"),
                        "version": snap["version"], "last_tick": snap["last_tick"],
                        "market_open": stream.market_open(snap["quotes"]),
                        "changed": changed, "ts": now,
                        "base_age": round(stream.STREAM.base_age()),
                        # The MKT page listens to this same stream; only the
                        # board's own symbols are sent, not the whole quote map.
                        "market": {s2: {"price": q.get("price"),
                                        "prev_close": q.get("prev_close"),
                                        "src": q.get("src")}
                                   for s2, q in snap["quotes"].items()
                                   if s2 in _BOARD_SYMS},
                    })
                    yield f"data: {json.dumps(_clean(payload))}\n\n"
                    last_version, last_beat = snap["version"], now
                await asyncio.sleep(1)
        finally:
            stream.STREAM.release()

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    })


@router.get("/benchmarks", response_class=Safe)
def list_benchmarks():
    return pricing.BENCHMARKS


# --------------------------------------------------------------------------
# OIL: the oil market (build 24)
# --------------------------------------------------------------------------

@router.get("/oil/history", response_class=Safe)
def oil_history(keys: str = Query("BRENT,WTI"), start: str = Query("2000-01-01"),
                ccy: str = Query("USD"), real: int = Query(0)):
    """Long price series for the chart: in dollars or euros, nominal or in today's money."""
    from . import oil
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", start):
        raise HTTPException(400, "start must be YYYY-MM-DD")
    if ccy not in ("USD", "EUR"):
        raise HTTPException(400, "ccy must be USD or EUR")
    try:
        return oil.history([k.strip().upper() for k in keys.split(",") if k.strip()], start, ccy, bool(real))
    except Exception as e:
        return {"series": {}, "error": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/oil/country/{iso}", response_class=Safe)
def oil_country(iso: str):
    """One producer: its output, exports, tanker loadings by port, OPEC+ target and buyers."""
    from . import oil
    if not re.match(r"^[A-Za-z]{2}$", iso):
        raise HTTPException(400, "a two-letter country code, e.g. SA")
    try:
        return oil.country(iso)
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/oil/map", response_class=Safe)
def oil_map():
    from . import oilmap
    try:
        return oilmap.part_map()
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/oil/platforms", response_class=Safe)
def oil_platforms():
    """Offshore platforms. The first call starts a slow background fetch; ask again later."""
    from . import oilmap
    return oilmap.platforms()


@router.get("/oil/tankers", response_class=Safe)
def oil_tankers():
    """Live tankers: the Baltic always, the world when an aisstream.io key is set."""
    from . import ais
    return ais.tankers()


@router.get("/oil/sources", response_class=Safe)
def oil_sources():
    from . import oil
    return {"sources": oil.sources()}


@router.get("/oil/alerts", response_class=Safe)
def oil_alerts():
    """What the oil alert rules would test right now, and the rules already set."""
    from . import oil
    rules = [r for r in store.list_rules() if str(r.get("kind", "")).startswith("oil_")]
    return {"values": oil.alert_values(),
            "kinds": {k: {"label": v[2], "dir": "above" if v[1] > 0 else "below", "unit": v[3]}
                      for k, v in oil.ALERT_KINDS.items()},
            "rules": rules}


@router.get("/oil/note", response_class=Safe)
def oil_note_state():
    from . import oilnote
    return oilnote.note_state()


@router.post("/oil/note", response_class=Safe)
def oil_note(body: NoteBody):
    """Claude's note on the oil page, or an answer about it."""
    from . import oilnote
    q = (body.question or "").strip() or None
    if q and len(q) > 2000:
        raise HTTPException(400, "keep the question under 2,000 characters")
    try:
        return oilnote.start_note(q, body.web)
    except (PermissionError, LookupError) as e:
        raise HTTPException(400, str(e))


@router.delete("/oil/note/{nid}", response_class=Safe)
def oil_note_delete(nid: int):
    from . import oilnote
    return {"ok": oilnote.delete_note(nid)}


@router.get("/oil/{part}", response_class=Safe)
def oil_part(part: str):
    """One panel's worth. Each fails alone, with the reason in the payload."""
    from . import oil
    if part not in oil.PARTS:
        raise HTTPException(404, f"no oil part {part!r}; one of {sorted(oil.PARTS)}")
    oil.warm()
    try:
        return oil.PARTS[part]()
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:300]}


# --------------------------------------------------------------------------
# WCH: the watchlist (build 25)
# --------------------------------------------------------------------------

class WlPin(BaseModel):
    sym: str
    note: str | None = None
    buy: float | None = None


class WlPrefs(BaseModel):
    preset: str | None = None
    thr: float | None = None


class WlNoteBody(BaseModel):
    question: str | None = None
    web: bool = False


@router.get("/wl", response_class=Safe)
def wl_view():
    """The last scan, the scan in progress, your pins and settings. The first visit starts a scan."""
    from . import watch
    v = watch.view()
    # No scan yet, or one from before this build's additions (the mood, the
    # momentum leaders): scan now, from the cache, so the page fills in.
    old = v["scan"] and (v["scan"].get("ver") or 1) < watch.SCAN_VER
    if (not v["scan"] or old) and v["job"]["state"] in ("idle", "done"):
        v["job"] = watch.start_scan()
    return v


@router.get("/wl/job", response_class=Safe)
def wl_job():
    from . import watch
    return dict(watch.JOB)


@router.post("/wl/scan", response_class=Safe)
def wl_scan():
    """Scan again now: the screener, the prices, and the fundamentals that are out of date."""
    from . import watch
    return watch.start_scan()


@router.post("/wl/prefs", response_class=Safe)
def wl_prefs(body: WlPrefs):
    from . import watch
    if body.preset is not None and body.preset not in watch.PRESETS:
        raise HTTPException(400, f"preset is one of {sorted(watch.PRESETS)}")
    return watch.set_prefs(body.model_dump(exclude_none=True))


@router.post("/wl/pin", response_class=Safe)
def wl_pin(body: WlPin):
    from . import watch
    try:
        return watch.pin(body.sym, body.note, body.buy)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/wl/pin/{sym}", response_class=Safe)
def wl_unpin(sym: str):
    from . import watch
    return {"ok": watch.unpin(sym), "pins": watch.pins()}


@router.get("/wl/why/{sym}", response_class=Safe)
def wl_why(sym: str):
    """The days it fell since its high, each with the market, sector and company parts and a catalyst."""
    from . import watch
    try:
        return watch.why(sym.upper())
    except Exception as e:
        return {"error": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/wl/track", response_class=Safe)
def wl_track(preset: str = Query("balanced")):
    from . import watch
    if preset not in watch.PRESETS and preset != watch.MOM_PRESET:
        raise HTTPException(400, f"preset is one of {sorted(watch.PRESETS) + [watch.MOM_PRESET]}")
    return watch.track(preset)


@router.get("/wl/notes", response_class=Safe)
def wl_noted():
    from . import watchnote
    return {"noted": watchnote.noted()}


@router.get("/wl/note/{sym}", response_class=Safe)
def wl_note_state(sym: str):
    from . import watchnote
    return watchnote.note_state(sym)


@router.post("/wl/note/{sym}", response_class=Safe)
def wl_note(sym: str, body: WlNoteBody):
    """Claude's second opinion on one stock, or an answer about it. Only on request: it uses your plan or key."""
    from . import watchnote
    q = (body.question or "").strip() or None
    if q and len(q) > 2000:
        raise HTTPException(400, "keep the question under 2,000 characters")
    try:
        return watchnote.start_note(sym, q, body.web)
    except (PermissionError, LookupError) as e:
        raise HTTPException(400, str(e))


@router.delete("/wl/note/{nid}", response_class=Safe)
def wl_note_delete(nid: int):
    from . import watchnote
    return {"ok": watchnote.delete_note(nid)}


class FcstParams(BaseModel):
    start: float | None = None
    monthly: float | None = None
    growth: float | None = None
    dep_years: int | None = None
    hist: str | None = None
    risk: str | None = None
    cost: float | None = None
    box3: str | None = None
    partner: bool | None = None
    savings: float | None = None
    valuation: bool | None = None


@router.get("/fcst", response_class=Safe)
def fcst_view():
    """The forecast with the default settings, the book's own figures, Box 3's rules, the money-supply test, the track record."""
    from . import fcst
    return fcst.view()


@router.post("/fcst/run", response_class=Safe)
def fcst_run(body: FcstParams):
    """The forecast again with other settings: the same draws, so only the settings differ."""
    from . import fcst
    return fcst.run(body.model_dump())


@router.get("/mvo", response_class=Safe)
def mvo_view(mode: str = Query("market"), target: str = Query("same"), cap: float = Query(10.0),
             monthly: float = Query(150.0), max_cost: float = Query(1.0), erp: float | None = Query(None)):
    """Markowitz: the frontier of today's holdings and a few funds, the book on it, and the deposit plan."""
    from . import mvo
    if mode not in ("market", "views", "history"):
        raise HTTPException(400, "mode must be market, views or history")
    try:
        return mvo.view(mode=mode, target=target, cap=cap, monthly=monthly, max_cost=max_cost, erp=erp)
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:300]}


class MvoView(BaseModel):
    t: str
    ret: float
    conf: str = "mid"
    note: str = ""


@router.post("/mvo/view", response_class=Safe)
def mvo_set_view(body: MvoView):
    """Your view on one holding: the return a year you expect, and how sure you are."""
    from . import mvo
    try:
        return mvo.set_view(body.t, body.ret, body.conf, body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/mvo/view/{t}", response_class=Safe)
def mvo_drop_view(t: str):
    from . import mvo
    return mvo.drop_view(t)


@router.get("/euro", response_class=Safe)
def euro_view(fresh: bool = Query(False)):
    """EUR: the euro, what moves it, and what it does to the book's dollars."""
    from . import euro
    try:
        return euro.view(force=fresh)
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/euro/opt", response_class=Safe)
def euro_options():
    """What FXE's options imply for EUR/USD, or the last reading, or the euro's own swings."""
    from . import euro
    try:
        return euro.options_part()
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/opt/strategies", response_class=Safe)
def opt_strategies():
    """The strategy templates OPT can build, by what you expect."""
    from . import strategy
    return strategy.catalogue()


class StratBody(BaseModel):
    tpl: str = ""
    legs: list[dict] = []


@router.get("/opt/strategy/{sym}", response_class=Safe)
def opt_strategy(sym: str, tpl: str = Query("covered_call")):
    """One template on one share's chain, legs placed by delta."""
    from . import strategy, options
    try:
        return {"available": True, **strategy.build(sym, tpl)}
    except (ValueError, options.NoOptions) as e:
        return {"available": False, "why": str(e)[:300]}
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.post("/opt/strategy/{sym}", response_class=Safe)
def opt_strategy_legs(sym: str, body: StratBody):
    """The same with the legs as you set them."""
    from . import strategy, options
    try:
        return {"available": True, **strategy.build(sym, body.tpl, body.legs or None)}
    except (ValueError, KeyError, options.NoOptions) as e:
        return {"available": False, "why": str(e)[:300]}
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/opt/screen", response_class=Safe)
def opt_screen():
    """The premium on each holding's options, from the chains stored last session."""
    from . import strategy
    try:
        return strategy.screen_view()
    except Exception as e:
        return {"rows": [], "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/opt/history", response_class=Safe)
def opt_history(since: str = Query("2007-01"), ccy: str = Query("EUR")):
    """What Cboe's strategy indexes did against the S&P 500, and implied against realised volatility."""
    from . import strathist
    try:
        return {"available": True, **strathist.view(since, ccy)}
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/mac/detail", response_class=Safe)
def mac_detail(ccy: str = Query(...), title: str = Query(...), d: str = Query("")):
    """What moved inside one release: its parts, their contributions, two years of them (build 33)."""
    from . import macparts
    if d and not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
        raise HTTPException(400, "d must be a date, YYYY-MM-DD")
    try:
        return macparts.detail(ccy.upper()[:3], title[:120], d or None)
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:200]}


@router.get("/mac", response_class=Safe)
def mac_view():
    """MAC: the week's releases, their actuals and what EUR/USD and the S&P 500 future did."""
    from . import macro
    try:
        return {"available": True, **macro.view()}
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.post("/mac/refresh", response_class=Safe)
def mac_refresh():
    """Read the actuals and reactions now rather than at the next quarter hour."""
    from . import macro
    try:
        r = macro.refresh()
        return {"available": True, "refresh": r, **macro.view()}
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/hours", response_class=Safe)
def exchange_hours():
    """Amsterdam, Frankfurt and New York: open or shut, and when that changes. For the top bar."""
    return market.hours()


@router.get("/stars", response_class=Safe)
def stars():
    """The home screen's constellation: the holdings placed by how they moved together over the past year."""
    from . import mvo
    try:
        return mvo.stars()
    except Exception as e:
        return {"available": False, "why": f"{e.__class__.__name__}: {e}"[:300]}


@router.get("/health", response_class=Safe)
def health():
    from . import BUILD, __version__
    txns = store.load_txns()
    return {"ok": True, "build": BUILD, "version": __version__,
            "pages": ["/", "/portfolio", "/m/{code}", "/m/MKT/bonds", "/m/ERN", "/m/ERN/{symbol}", "/m/ALRT",
                      "/m/RRG", "/m/OPT", "/m/OPT/{symbol}", "/m/SPLC", "/m/SPLC/{symbol}", "/m/OIL",
                      "/m/MKT/oil", "/t/{symbol}"],
            "transactions": len(txns),
            "instruments": len(store.load_instruments()),
            "blocked": blocked_imports()}


# The compiled libraries the app cannot do without, and what each one carries.
NATIVE = (("pandas", "pandas — every price table, so prices, history and the brief"),
          ("yfinance", "yfinance — every Yahoo price and quote"),
          ("lxml.etree", "lxml — SEC filings and press releases"),
          ("curl_cffi", "curl_cffi — how yfinance reaches Yahoo"))
_BLOCKED: dict = {"v": None}


def blocked_imports() -> list[dict]:
    """
    Libraries Windows refuses to load. Smart App Control blocks compiled files
    that are neither signed nor known to Microsoft's reputation service, and
    the import then fails with a DLL error that every page downstream reports
    as "no prices" — in September 2026 it blocked pandas 3.0.6 outright. Asked
    once per process: a failed import is retried by every caller, and each
    retry is another block in the Code Integrity log.
    """
    if _BLOCKED["v"] is None:
        import importlib
        out = []
        for mod, what in NATIVE:
            try:
                importlib.import_module(mod)
            except Exception as e:
                msg = str(e)
                if "Application Control" in msg or "DLL load failed" in msg:
                    out.append({"module": mod, "what": what, "error": msg[:240]})
        _BLOCKED["v"] = out
    return _BLOCKED["v"]
