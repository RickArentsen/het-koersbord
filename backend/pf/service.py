"""
Everything the dashboard shows, computed from the ledger + cached prices.

No figure in here is stored. Change a transaction, refresh, every number moves.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Optional

import threading
import time

from . import store, pricing, leveraged, risk, behaviour
from .degiro import NON_INSTRUMENT_ISINS
from .engine import build_ledger, xirr, concentration


def _d(x) -> str:
    return x.isoformat() if hasattr(x, "isoformat") else str(x)


# ISO 3166 alpha-2 from the ISIN prefix — the last resort for country.
_ISIN_COUNTRY = {
    "US": "United States", "NL": "Netherlands", "DE": "Germany", "GB": "United Kingdom",
    "IE": "Ireland", "FR": "France", "CH": "Switzerland", "CA": "Canada",
    "KY": "Cayman Islands", "LU": "Luxembourg", "DK": "Denmark", "JE": "Jersey",
    "BE": "Belgium", "ES": "Spain", "IT": "Italy", "SE": "Sweden", "FI": "Finland",
    "NO": "Norway", "AT": "Austria", "PT": "Portugal", "BM": "Bermuda",
}


def _sector_of(meta: dict, ticker, kind: str):
    """Override, then stored value, then the local table, then the underlying."""
    if meta.get("sector_override"):
        return meta["sector_override"]
    if meta.get("sector"):
        return meta["sector"]
    tk = ticker
    if kind == "DERIVATIVE":
        tk = meta.get("underlying_ticker") or tk
    sec, _ = pricing.seed_sector(tk)
    return sec


def _country_of(meta: dict, ticker, isin: str):
    if meta.get("country_override"):
        return meta["country_override"]
    if meta.get("country"):
        return meta["country"]
    tk = ticker
    if meta.get("kind") == "DERIVATIVE":
        tk = meta.get("underlying_ticker") or tk
    _, ctry = pricing.seed_sector(tk)
    return ctry or _ISIN_COUNTRY.get((isin or "")[:2], (isin or "??")[:2])


def _ccy_pair(ccy: str) -> str:
    return "EUR" + ccy if ccy and ccy != "EUR" else "EUREUR"


# --------------------------------------------------------------------------
# instrument metadata (sector / country) — lazy, cached in the DB
# --------------------------------------------------------------------------

def _sector_for(ticker: str, cache: dict) -> tuple[Optional[str], Optional[str]]:
    """(sector, country) for a quote symbol. Local table first, provider second."""
    if ticker in cache:
        return cache[ticker]
    seeded = pricing.seed_sector(ticker)
    if seeded[0]:
        cache[ticker] = seeded
        return seeded
    out = (None, None)
    try:
        import yfinance as yf
        info = yf.Ticker(ticker).get_info()
        out = (info.get("sector"), info.get("country"))
    except Exception:
        pass
    cache[ticker] = out
    return out


def enrich_instruments(isins: list[str]) -> int:
    """
    Fill ticker/sector/country where missing. Slow; call explicitly.

    Leveraged products (turbos, factor certificates, warrants) have no sector
    of their own, so they take the sector and country of the share they track.
    A Nvidia turbo is Nvidia exposure; filing it under "Unclassified" hides
    precisely the concentration you would want to see.
    """
    from .degiro import underlying_of, underlying_ticker_of

    insts = store.load_instruments()
    seccache: dict = {}
    done = 0

    for isin in isins:
        row = insts.get(isin)
        if not row:
            continue
        upd = {}
        kind = row.get("kind")

        if kind == "DERIVATIVE":
            und = row.get("underlying") or underlying_of(row.get("name") or "")
            if und and not row.get("underlying"):
                upd["underlying"] = und
            ut = row.get("underlying_ticker") or underlying_ticker_of(und)
            if ut and not row.get("underlying_ticker"):
                upd["underlying_ticker"] = ut
            if ut and not row.get("sector"):
                sec, ctry = _sector_for(ut, seccache)
                if sec:
                    upd["sector"] = sec
                if ctry:
                    upd["country"] = ctry
        else:
            if not row.get("ticker"):
                tk, src = pricing.resolve_ticker(isin, row.get("name") or "", row.get("currency"), row.get("exchange"))
                if tk:
                    upd["ticker"], upd["ticker_src"] = tk, src
            tk = upd.get("ticker") or row.get("ticker")
            if tk and not row.get("sector") and kind in ("EQUITY", "ETF"):
                sec, ctry = _sector_for(tk, seccache)
                if sec:
                    upd["sector"] = sec
                if ctry:
                    upd["country"] = ctry

        if upd:
            store.set_instrument(isin, **upd)
            done += 1
    return done


# --------------------------------------------------------------------------
# tickers for shares bought after the seed map was written
# --------------------------------------------------------------------------
# A share with no ticker has no quote, and is carried at what you paid for it
# ("last trade"). Until build 41 a ticker came from the seed map or from the
# enrich route, which no page has called for months: Coherent, Applied
# Digital and Alibaba, bought on 15 October 2026, stood at their purchase
# price for six days. The server now looks them up itself, one at a time;
# verify.py and other scripts never start the loop.
TICK = {"t": None, "wake": threading.Event()}
TICK_RETRY_S = 6 * 3600
# Bumped when the lookup itself improves: a share the old one could not find
# is tried again at once rather than six hours later. 2: by the company's name
# when Yahoo's ISIN search lists only a line in another currency (Celestica).
RESOLVE_VER = 2


def missing_tickers(trades=None, insts=None) -> list[str]:
    """Every share or fund ever traded that has no ticker: open ones first."""
    trades = trades if trades is not None else [t for t in store.load_txns() if t.kind in ("BUY", "SELL")]
    insts = insts if insts is not None else store.load_instruments()
    held = set(build_ledger(trades).positions) if trades else set()
    out = [i for i in {t.isin for t in trades if t.isin}
           if insts.get(i, {}).get("kind") != "DERIVATIVE" and i not in NON_INSTRUMENT_ISINS
           and not (insts.get(i, {}).get("ticker") or pricing.SEED_TICKERS.get(i))]
    return sorted(out, key=lambda i: (i not in held, i))


def resolve_missing(now: Optional[float] = None) -> dict:
    """Look up each missing ticker not tried in the last six hours. Returns what was found."""
    now = now or time.time()
    tried = (store.cache_get("ticker:tried") or {}).get("m") or {}
    found = {}
    for isin in missing_tickers():
        t = tried.get(isin)
        last, ver = (t[0], t[1]) if isinstance(t, list) else (t or 0, 1)
        if ver == RESOLVE_VER and now - last < TICK_RETRY_S:
            continue
        try:
            enrich_instruments([isin])
        except Exception:
            pass
        tried[isin] = [now, RESOLVE_VER]
        store.cache_put("ticker:tried", {"m": tried})
        tk = (store.load_instruments().get(isin) or {}).get("ticker")
        if tk:
            found[isin] = tk
            try:                # its closes stored at once: a feed in its cooldown falls back on them
                pricing.get_quotes([tk], fresh=True)
            except Exception:
                pass
    return found


def _tick_loop():
    time.sleep(30)
    while True:
        try:
            resolve_missing()
        except Exception:
            pass
        TICK["wake"].wait(1800)
        TICK["wake"].clear()


def ensure_tickers():
    t = TICK["t"]
    if t and t.is_alive():
        return
    t = threading.Thread(target=_tick_loop, name="tickers", daemon=True)
    TICK["t"] = t
    t.start()


# --------------------------------------------------------------------------
# the dashboard payload
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# leveraged products: price them from the share they track
# --------------------------------------------------------------------------

# How leveraged products are valued. Default is `zero`: a turbo with no feed
# contributes nothing rather than a number nobody can check. `last_trade` carries
# it at what you last paid. `model` prices it off the underlying — only where the
# model reprices your real trades and survives the leverage plausibility guard.
LEV_MODES = ("zero", "last_trade", "model")


def lev_mode() -> str:
    m = store.get_setting("lev_valuation", "zero")
    return m if m in LEV_MODES else "zero"


def _find_underlying(isin: str, row: dict) -> Optional[str]:
    """
    The share a turbo tracks when the instrument row does not say: the name
    list first, then Yahoo by name. Stored once found, so it is asked once.
    """
    from . import degiro
    und = row.get("underlying")
    t = degiro.underlying_ticker_of(und) or (pricing.search_share(und) if und else None)
    if t:
        try:
            store.set_instrument(isin, underlying_ticker=t)
        except Exception:
            pass
    return t


def _day_range(history: bool = False):
    try:
        from . import dayrange
        return dayrange.history(10) if history else dayrange.get()
    except Exception:
        return [] if history else None


def fin_path(isin: str, spec) -> "leveraged.FinPath":
    """
    A turbo's dated financing levels: the one in the name it is stored under,
    dated by the first import that carried it (DEGIRO writes each turbo's
    current name into the export), and every level recorded since, from later
    imports or typed in.
    """
    pts = [(p["d"], p["fn"], p.get("sl"), p.get("src") or "") for p in store.fin_points(isin)]
    d0 = store.first_import(isin)
    if spec.financing and d0 and not any(p[0] == d0 for p in pts):
        pts.append((d0, spec.financing, spec.barrier, "export"))
    return leveraged.FinPath(pts)


def _price_leveraged(L, insts, und_of, trades, start, fxs) -> dict:
    """
    For every open turbo / factor certificate, price it off its underlying and
    check the model against what was actually paid for it.

    Returns {isin: {price, leverage, knockout_pct, model:{...}}} — only where
    the model reprices the real trades closely enough to be believed.
    """
    tickers = sorted({t for t in und_of.values() if t})
    if not tickers:
        return {}
    hist = pricing.get_history_many(tickers, _d(start))
    out: dict = {}

    for isin, ut in und_of.items():
        meta = insts.get(isin, {})
        spec = leveraged.parse(meta.get("name") or "")
        entry = {"family": spec.family, "underlying": meta.get("underlying"),
                 "underlying_ticker": ut, "spec": spec.as_dict()}

        if not ut or spec.family == "WARRANT" or not spec.priceable:
            entry["model"] = {"ok": False, "reason":
                              "warrant — needs implied volatility, no model"
                              if spec.family == "WARRANT" else
                              "name not parseable" if not spec.priceable else
                              "which share it tracks is not known: set it in the editor (Tracks)"}
            out[isin] = entry
            continue

        closes = hist.get(ut) or {}
        if not closes:
            entry["model"] = {"ok": False, "reason": f"no price history for {ut}"}
            out[isin] = entry
            continue
        # The same "last finished session" as every other row (pf/sessions.py):
        # the last two stored days measured a turbo from two sessions back
        # before its underlying opened.
        from . import sessions
        _pk = sessions.pick(sorted(closes.items()), sessions.exchange_of(ut))

        # what he actually paid, on the days he actually traded it
        obs = [(_d(t.trade_dt.date()), abs(t.price))
               for t in trades if t.isin == isin and t.price]
        obs.sort()
        days = sorted(closes)
        s_now, s_prev = closes[days[-1]], (_pk["ref"] if _pk else closes[days[-1]])

        if spec.family == "TURBO":
            # The share's currency decides the conversion, not which fits the
            # trades better; the financing level comes from dated points.
            ccy = leveraged.underlying_ccy(ut)
            fx = None
            if ccy and ccy != "EUR":
                fx = fxs.get(ccy) or pricing.get_fx(_ccy_pair(ccy), _d(start))
            if ccy and ccy != "EUR" and not fx:
                entry["model"] = {"ok": False, "reason": f"no EUR/{ccy} rate to convert the share's price"}
                out[isin] = entry
                continue
            path = fin_path(isin, spec)
            fin_at = (lambda d, _p=path: _p.at(d)[0]) if path.pts else None
            when = sorted((t.trade_dt.isoformat(timespec="seconds"), abs(t.price))
                          for t in trades if t.isin == isin and t.price)
            m = leveraged.calibrate_turbo(spec, when, closes, fx, fin_at=fin_at,
                                          ex=sessions.exchange_of(ut))
            entry["model"] = m.as_dict()
            today = date.today().isoformat()
            fin_now = path.at(today) if path.pts else None
            ref_d = _pk["ref_d"] if _pk else days[-1]
            fin_ref = path.at(ref_d) if path.pts else None
            fx_now = leveraged._close_on(fx, today) if fx else 1.0
            fx_ref = leveraged._close_on(fx, ref_d) if fx else 1.0
            fn_now = fin_now[0] if fin_now else spec.financing
            sl_now = fin_now[1] if fin_now and fin_now[1] else spec.barrier
            entry["financing"] = {
                "fn": round(fn_now, 4) if fn_now else None, "sl": round(sl_now, 4) if sl_now else None,
                "rate_pa": round(path.rate * 100, 2) if path.pts else None,
                "from": ({"d": fin_now[2][0], "fn": fin_now[2][1], "src": fin_now[2][3]} if fin_now else None),
                "points": [{"d": p[0], "fn": p[1], "sl": p[2], "src": p[3]} for p in path.pts],
                "currency": ccy or "EUR", "fx": round(fx_now, 5) if fx else None,
                "share_when_named": round(spec.share_when_named, 2) if spec.share_when_named else None,
            }
            if m.ok:
                px = leveraged.turbo_price(spec, s_now, fx=fx_now, financing=fn_now)
                prev = leveraged.turbo_price(spec, s_prev, fx=fx_ref,
                                             financing=fin_ref[0] if fin_ref else None)
                ko = leveraged.knockout_distance(spec, s_now, sl_now)
                # A close at or under the stop since the first trade: a BEST
                # turbo knocks out on an intraday touch, which daily closes can
                # only hint at, so this is a warning, not a zero.
                first = when[0][0][:10] if when else None
                touched = None
                if first and path.pts:
                    for d in days:
                        if d < first:
                            continue
                        f = path.at(d)
                        b = (f[1] if f and f[1] else None) or spec.barrier
                        if b and ((closes[d] <= b) if spec.direction == "LONG" else (closes[d] >= b)):
                            touched = d
                            break
                entry.update({
                    "price": round(px, 4) if px is not None else None,
                    "prev_price": round(prev, 4) if prev is not None else None,
                    "underlying_price": round(s_now, 4),
                    "asof": days[-1],
                    "leverage": round(leveraged.effective_leverage(spec, s_now, fn_now) or 0, 2) or None,
                    "knockout_pct": round(ko, 2) if ko is not None else None,
                    "barrier": round(sl_now, 4) if sl_now else spec.barrier,
                    "stop_touched": touched,
                })
        else:  # FACTOR — roll it forward day by day from the last observed price
            if obs:
                anchor_date, anchor_px = obs[-1]
                px = leveraged.factor_price(spec, anchor_px, anchor_date, closes)
                prev_closes = {d: v for d, v in closes.items() if d <= (_pk["ref_d"] if _pk else days[-1])}
                prev = leveraged.factor_price(spec, anchor_px, anchor_date, prev_closes)
                err = None
                if len(obs) > 1:
                    # reprice the last trade from the one before it
                    d0, p0 = obs[-2]
                    chk = leveraged.factor_price(
                        spec, p0, d0, {d: v for d, v in closes.items() if d <= anchor_date})
                    if chk and anchor_px:
                        err = round(abs(chk / anchor_px - 1) * 100, 1)
                ok = px is not None and (err is None or err <= 25.0)
                entry["model"] = {"ok": ok, "family": "FACTOR", "error_pct": err,
                                  "checks": max(0, len(obs) - 1),
                                  "reason": "" if ok else
                                  (f"repricing off by {err}%" if err else "could not roll forward")}
                if ok:
                    entry.update({
                        "price": round(px, 4),
                        "prev_price": round(prev, 4) if prev else round(px, 4),
                        "underlying_price": round(s_now, 4),
                        "asof": days[-1],
                        "leverage": spec.leverage,
                        "anchor": anchor_date,
                        "decay_note": "daily-reset leverage — compounded, not multiplied",
                    })
            else:
                entry["model"] = {"ok": False, "reason": "no trade to anchor on"}
        out[isin] = entry
    return out


def live_base() -> dict:
    """
    The expensive half of the day change, computed once so a price tick only
    has to refold arithmetic over it.

    Quantities, cost basis and yesterday's close do not change during a
    session; only prices and FX do. Rebuilding the whole dashboard per tick
    would mean re-reading every transaction and replaying years of FIFO
    for a number that moved by four cents.
    """
    d = dashboard("SP500", light=True)
    if d.get("empty"):
        return {"empty": True, "positions": [], "fx_symbol": {}, "symbols": []}

    pos, ccys = [], set()
    for r in d["positions"]:
        if r.get("excluded") or not r.get("qty"):
            continue
        ccys.add(r.get("currency") or "EUR")
        pos.append({
            "isin": r["isin"], "name": r["name"], "ticker": r.get("ticker"),
            "qty": r["qty"], "currency": r.get("currency") or "EUR",
            "price": r.get("price"), "prev_close": r.get("prev_close"),
            # Both halves of "yesterday" come from the dashboard row, so the
            # live number and the page-load number are measured against the
            # same thing. fx_prev was a copy of TODAY's rate, which made the
            # currency leg measure only the minutes since the base was built.
            "fx": r.get("fx"), "fx_prev": r.get("fx_prev") or r.get("fx"),
            "cost_eur": r.get("cost_eur"),
            "exchange": r.get("exchange"), "session": r.get("session"),
            "last_eur": r.get("last_eur"), "last_pct": r.get("last_pct"), "last_date": r.get("last_date"),
        })
    fx_symbol = {c: f"EUR{c}=X" for c in ccys if c != "EUR"}
    from . import sessions
    exchanges = sorted({p["exchange"] for p in pos if p.get("exchange")})
    nxt = sessions.next_change(exchanges)
    return {
        "positions": pos,
        "fx_symbol": fx_symbol,
        "cash_eur": d["totals"].get("cash_eur") or 0.0,
        "cost_eur": d["totals"].get("cost_eur") or 0.0,
        "symbols": sorted({p["ticker"] for p in pos if p["ticker"]}
                          | set(fx_symbol.values())),
        "asof": d["asof"],
        "exchanges": exchanges,
        # Rebuilt soon after the next open or close, not up to five minutes
        # later: at a close the reference moves to that close, and the day
        # starts again (pf/sessions.py). The two minutes let Yahoo print the
        # closing bar and the quote memo expire.
        "valid_until": nxt + 150 if nxt else None,
        "last_eur": d["totals"].get("last_eur"), "last_pct": d["totals"].get("last_pct"),
        "last_dates": d["totals"].get("last_dates") or [],
    }


def _market_risk(curve: dict, rows: list, cap: dict, fxs: dict) -> dict:
    """
    Beta, R2, tracking error and information ratio, against every benchmark in
    pricing.RISK_BENCHMARKS, plus the forward beta of the book held today.
    All of it on five-day returns: the shares close in New York, the trackers
    in Frankfurt (see risk.HORIZON).

    Runs on the daily curve that has already been built, and on the price
    history build_curve already fetched, so it adds one network call per extra
    benchmark and nothing else.
    """
    series = curve.get("series") or []
    port_r = risk.curve_returns(series)
    out: dict = {
        "n_days": len(port_r),
        "enough": len(port_r) >= risk.MIN_OBS,
        "min_obs": risk.MIN_OBS,
        "coverage_floor_pct": round(risk.MIN_COVERAGE * 100),
        "dropped_days": max(len(series) - 1 - len(port_r), 0),
        "benchmarks": [],
        "volatility": risk.volatility_block(port_r),
    }
    if not out["enough"]:
        out["why"] = (f"{len(port_r)} usable days of history — a beta needs at "
                      f"least {risk.MIN_OBS}")
        return out

    days = [p["d"] for p in series]
    bench_px: dict[str, dict] = {}
    for key in pricing.RISK_BENCHMARKS:
        b = pricing.BENCHMARKS.get(key)
        if not b:
            continue
        # Cached: build_curve already fetched the primary benchmark, so only a
        # genuinely new index costs a round trip.
        h = pricing.get_history(b["ticker"], series[0]["d"])
        if not h:
            out["benchmarks"].append({"key": key, "label": b["label"],
                                      "ticker": b["ticker"], "n": 0,
                                      "enough": False, "beta": None, "r2": None,
                                      "why": "no price history for this index"})
            continue
        bench_px[key] = h
        out["benchmarks"].append(
            risk.market_stats(port_r, h, days, key, b["label"], b["ticker"]))

    # Which index actually explains this portfolio. Reported explicitly,
    # because a beta against the index with the lower R2 is a number about a
    # relationship that is not there.
    scored = [b for b in out["benchmarks"] if b.get("r2") is not None]
    best = max(scored, key=lambda b: b["r2"]) if scored else None
    out["best_fit"] = best["key"] if best else None

    # Forward beta, against the best-fitting index, from per-position history.
    if best and bench_px.get(best["key"]):
        hist = cap.get("hist") or {}
        eur_hist = {}
        for r in rows:
            m = hist.get(r["isin"])
            if not m:
                continue
            ccy = r.get("currency") or "EUR"
            eur_hist[r["isin"]] = risk.eur_prices(m, fxs.get(ccy), ccy)
        pbetas = risk.position_betas(bench_px[best["key"]], eur_hist)
        out["position_betas"] = pbetas
        out["forward"] = {**risk.forward_beta(rows, pbetas),
                          "benchmark": best["key"], "label": best["label"]}
        hb = best.get("beta")
        fb = out["forward"].get("beta")
        out["forward"]["gap"] = round(fb - hb, 2) if (fb is not None and hb is not None) else None
    else:
        out["position_betas"] = {}
        out["forward"] = {"beta": None, "coverage_pct": 0.0,
                          "missing": [], "missing_n": 0}
    return out


def _sweep(t) -> bool:
    """
    A cash-sweep row: money moving between DEGIRO and the flatex bank account
    behind it. It is in the statement for information. The rows after it carry
    on from the balance BEFORE it, so the Saldo printed on it is not a state
    the account was ever in. Leave these rows out and the Saldo column is an
    exact running sum (verify.py checks that on your own statement).
    """
    note = (t.note or "").lower()
    return t.kind == "INTERNAL" and ("sweep" in note or "overboeking" in note)


def cash_account(txns: list, eur=None) -> dict:
    """
    Cash as DEGIRO's own Saldo column has it — authoritative, never rebuilt
    from the rows.

      now    {currency: balance}, the newest state
      src    {currency: which row that was}
      asof   when that row was written
      days   {day: {currency: balance}}, the last state of every day with rows
      flows  [(date, euros)], money entering and leaving, on the day the
             balance moved

    Three things are easy to get wrong, and each was wrong here once.

    Several rows share one timestamp (a trade writes its FX, fee and settlement
    legs together) and the export is written NEWEST FIRST, so within a tie the
    LOWEST row index is the latest state. Sorted on the timestamp alone, the
    last row of an instant is its oldest: after a purchase in dollars that is
    the dollar balance before the conversion, and the day read as if the
    purchase had been paid for twice.

    A cash-sweep row is not part of the balance at all (see _sweep). As the
    day's last row it put the day out by the whole transfer.

    Money is in the balance from the moment an iDEAL payment is reserved, a
    day or two before the deposit row itself is written (the reservation is
    then reversed in the same instant as the deposit). The flow belongs on the
    day of the reservation: dated on the deposit row, the money read as a gain
    on the first day and as a loss of the same size on the second.
    """
    rows = sorted((t for t in txns if t.balance is not None and t.balance_ccy and not _sweep(t)),
                  key=lambda t: (t.trade_dt, -(t.seq if t.seq is not None else 0)))
    now, src, days = {}, {}, {}
    for t in rows:                                   # the last write wins: newest instant, lowest index
        now[t.balance_ccy] = t.balance
        src[t.balance_ccy] = {"date": _d(t.trade_dt.date()), "time": t.trade_dt.strftime("%H:%M"),
                              "row": (t.note or "")[:40], "seq": t.seq}
        days.setdefault(_d(t.trade_dt.date()), {})[t.balance_ccy] = t.balance
    eur = eur or (lambda t: t.local_value or 0.0)
    moved: dict = defaultdict(float)
    for t in txns:
        if t.kind in ("DEPOSIT", "WITHDRAWAL") or (t.kind == "INTERNAL" and not _sweep(t)):
            moved[t.trade_dt.date()] += eur(t)
    return {"now": now, "src": src, "asof": max((t.trade_dt for t in rows), default=None), "days": days,
            "flows": [(d, round(a, 2)) for d, a in sorted(moved.items()) if abs(a) >= 0.005]}


def capital_in(txns: list, flows: list, eur) -> dict:
    """
    The money you put in: deposits less withdrawals, and an iDEAL payment
    still only reserved. That payment is in the balance from its reservation,
    a day or two before DEGIRO writes the deposit row (cash_account). Counted
    from the deposit rows alone it was profit in between: one deposit nearly
    doubled the total profit for two days. The flows date it on the
    reservation, and the capital now agrees with them.
    """
    deposits = sum(eur(t) for t in txns if t.kind == "DEPOSIT")
    withdrawals = sum(eur(t) for t in txns if t.kind == "WITHDRAWAL")
    has = any(t.kind in ("DEPOSIT", "WITHDRAWAL") for t in txns)
    pending = round(sum(a for _, a in flows) - (deposits + withdrawals), 2) if has else 0.0
    return {"deposits": deposits, "withdrawals": withdrawals, "pending": pending,
            "net": deposits + withdrawals + pending}


def dashboard(benchmark_key: str = "SP500", light: bool = False) -> dict:
    txns = store.load_txns()
    insts = store.load_instruments()
    trades = [t for t in txns if t.kind in ("BUY", "SELL")]
    if not trades:
        return {"empty": True, "message": "No transactions imported yet."}

    L = build_ledger(trades)
    today = date.today()
    start = min(t.trade_dt.date() for t in trades)

    # --- symbols we need ---------------------------------------------------
    # Every instrument ever traded, not just the ones still open. A name sold
    # last year was still held for part of the history, and the equity curve
    # has to price it on those days or it sits flat and the curve steps.
    tick_of: dict[str, Optional[str]] = {}
    for isin in {t.isin for t in trades if t.isin}:
        row = insts.get(isin, {})
        if row.get("kind") == "DERIVATIVE":
            continue        # never quoted by a symbol of its own: that is the share it tracks
        tk = row.get("ticker") or pricing.SEED_TICKERS.get(isin)
        if tk:
            tick_of[isin] = tk
    if TICK["t"] and any(i not in tick_of and insts.get(i, {}).get("kind") != "DERIVATIVE" for i in L.positions):
        TICK["wake"].set()          # a holding with no ticker: the server's loop looks it up now

    # Leveraged products have no feed of their own, but their name encodes the
    # contract, so they can be priced off the share they track.
    und_of: dict[str, Optional[str]] = {}
    for isin in L.positions:
        row = insts.get(isin, {})
        if row.get("kind") == "DERIVATIVE":
            und_of[isin] = row.get("underlying_ticker") or _find_underlying(isin, row)

    open_tickers = [tick_of.get(i) for i in L.positions if tick_of.get(i)]
    quotes = pricing.get_quotes(open_tickers + [u for u in und_of.values() if u])

    ccys = {p.currency for p in L.positions.values()} | {"USD"}
    fxs = {c: pricing.get_fx(_ccy_pair(c), _d(start)) for c in ccys if c != "EUR"}

    LEVMODE = lev_mode()
    lev_models = ({} if LEVMODE == "zero"
                  else _price_leveraged(L, insts, und_of, trades, start, fxs))

    def fx_now(ccy):
        if ccy == "EUR":
            return 1.0, 1.0
        m = fxs.get(ccy) or {}
        if not m:
            return None, None
        ks = sorted(m)
        return m[ks[-1]], (m[ks[-2]] if len(ks) > 1 else m[ks[-1]])

    def fx_on(ccy, d):
        """The rate on a date, or the last one before it: the currency leg starts where the price leg does."""
        if ccy == "EUR" or not d:
            return 1.0 if ccy == "EUR" else None
        m = fxs.get(ccy) or {}
        ks = [k for k in sorted(m) if k <= d]
        return m[ks[-1]] if ks else None

    # --- positions ---------------------------------------------------------
    rows = []
    total_mv = 0.0
    for isin, p in L.positions.items():
        meta = insts.get(isin, {})
        kind = meta.get("kind", "EQUITY")
        tk = tick_of.get(isin)
        q = quotes.get(tk) if tk else None

        f_now, f_prev = fx_now(p.currency)
        stale = False
        lev = lev_models.get(isin)
        last_s = None
        if q:
            px, prev_px, asof, psrc = q["price"], q["prev_close"], q["asof"], q["source"]
            if prev_px is None:
                prev_px = px
            # Yesterday's rate is the rate on the day the price is measured
            # from, so the currency leg covers the same stretch of time.
            f_ref = fx_on(p.currency, q.get("ref_date"))
            if f_ref:
                f_prev = f_ref
            last_s = q.get("last_session")
        elif kind == "DERIVATIVE" and LEVMODE == "zero":
            px = prev_px = 0.0
            asof, psrc, stale = None, "excluded — valued at 0", False
        elif (kind == "DERIVATIVE" and lev and lev.get("price") is not None and LEVMODE == "model"):
            # A turbo the model can price is priced by it, also over a manual
            # price: a price typed in for a turbo is stale by the next tick of
            # its share. The manual price is the fallback when it cannot.
            px, prev_px = lev["price"], lev.get("prev_price", lev["price"])
            asof, psrc = lev.get("asof"), f"modelled from {lev.get('underlying_ticker')}"
        elif meta.get("manual_price"):
            # an explicit price you typed beats any model
            px = prev_px = meta["manual_price"]
            asof, psrc, stale = meta.get("manual_price_at"), "manual", False
        elif (lev and lev.get("price") is not None and LEVMODE == "model"):
            px, prev_px = lev["price"], lev.get("prev_price", lev["price"])
            asof, psrc = lev.get("asof"), f"modelled from {lev.get('underlying_ticker')}"
        else:
            px = p.avg_price_native
            prev_px, asof, psrc = px, None, "last trade"
            stale = True
        if f_now is None:
            f_now = f_prev = (p.lots[-1].fx_at_open if p.lots else 1.0)
            stale = True

        mv = p.qty * px / f_now
        cost = p.cost_eur
        unreal = mv - cost
        # attribution excludes fees: price leg + FX leg == market value - clean cost
        unreal_price = (p.qty * px - p.cost_native) / f_now
        unreal_fx = p.cost_native / f_now - p.cost_eur_clean

        day_price = p.qty * (px - prev_px) / f_now
        day_fx = p.qty * prev_px * (1.0 / f_now - 1.0 / f_prev) if f_prev else 0.0

        # The last finished session in full: the day that moved to "last
        # session" when its market closed, so it can still be read.
        last_eur = last_pct = last_d = None
        if last_s and last_s.get("prev"):
            fa = fx_on(p.currency, last_s.get("prev_d")) or f_prev
            fb = fx_on(p.currency, last_s.get("d")) or f_prev
            last_eur = p.qty * last_s["close"] / fb - p.qty * last_s["prev"] / fa
            last_pct, last_d = last_s.get("pct"), last_s.get("d")
        from . import sessions as _ss
        ex = (q or {}).get("exchange") or _ss.exchange_of(tk)
        st = _ss.state(ex) if ex else None

        held_days = (today - p.first_open.date()).days if p.first_open else 0
        flows = [(t.trade_dt.date(), t.total_eur) for t in trades
                 if t.isin == isin and t.total_eur is not None]
        flows.append((today, mv))
        # Annualising a holding of a few days produces a headline number that is
        # arithmetically true and completely meaningless. Below 30 days, don't.
        r_xirr = xirr(flows) if held_days >= 30 else None
        simple_ret = (mv / cost - 1.0) * 100 if cost else None

        total_mv += mv
        rows.append({
            "isin": isin,
            "name": meta.get("name") or isin,
            "kind": kind,
            "ticker": tk,
            "ticker_src": meta.get("ticker_src"),
            "currency": p.currency,
            "qty": round(p.qty, 4),
            "avg_price": round(p.avg_price_native, 4),
            "price": round(px, 4),
            "prev_close": round(prev_px, 4),
            "price_asof": asof,
            "price_source": psrc,
            "stale": stale,
            "fx": round(f_now, 5),
            # Yesterday's rate, carried so the live feed measures the currency
            # leg against the same "yesterday" this row does. It used to be
            # dropped here, and live_base filled the gap with TODAY's rate.
            "fx_prev": round(f_prev, 5) if f_prev else None,
            "cost_eur": round(cost, 2),
            "value_eur": round(mv, 2),
            "unreal_eur": round(unreal, 2),
            "unreal_pct": round(unreal / cost * 100, 2) if cost else None,
            "unreal_price_eur": round(unreal_price, 2),
            "unreal_fx_eur": round(unreal_fx, 2),
            "day_eur": round(day_price + day_fx, 2),
            "day_price_eur": round(day_price, 2),
            "day_fx_eur": round(day_fx, 2),
            "day_pct": round((px / prev_px - 1) * 100, 2) if prev_px else 0.0,
            "ref_date": (q or {}).get("ref_date"),
            "exchange": ex,
            "session": st,
            "day_tag": _ss.tag(st, px, prev_px) if st else None,
            "last_eur": round(last_eur, 2) if last_eur is not None else None,
            "last_pct": last_pct,
            "last_date": last_d,
            "xirr": round(r_xirr * 100, 1) if r_xirr is not None else None,
            "xirr_suppressed": r_xirr is None and held_days < 30,
            "simple_return_pct": round(simple_ret, 2) if simple_ret is not None else None,
            "held_days": held_days or None,
            "sector": _sector_of(meta, tk, kind),
            "country": _country_of(meta, tk, isin),
            "underlying": meta.get("underlying"),
            "underlying_ticker": meta.get("underlying_ticker"),
            "manual_price": meta.get("manual_price"),
            **({"lev": lev} if lev else {}),
            "exposure_eur": round(mv * lev["leverage"], 2)
            if lev and lev.get("leverage") else round(mv, 2),
            "excluded": kind == "DERIVATIVE" and LEVMODE == "zero",
        })

    for r in rows:
        r["weight"] = round(r["value_eur"] / total_mv * 100, 2) if total_mv else 0.0
    rows.sort(key=lambda r: -r["value_eur"])

    total_cost = sum(r["cost_eur"] for r in rows)
    day_total = sum(r["day_eur"] for r in rows)
    day_price_total = sum(r["day_price_eur"] for r in rows)
    day_fx_total = sum(r["day_fx_eur"] for r in rows)
    last_rows = [r for r in rows if r.get("last_eur") is not None and not r.get("excluded")]
    last_total = sum(r["last_eur"] for r in last_rows)
    last_open = sum(r["value_eur"] - r["day_eur"] - r["last_eur"] for r in last_rows)

    # --- breakdowns --------------------------------------------------------
    def group(key, only_kinds=None):
        agg = defaultdict(float)
        for r in rows:
            if only_kinds and r["kind"] not in only_kinds:
                continue
            agg[r.get(key) or "Unclassified"] += r["value_eur"]
        tot = sum(agg.values()) or 1.0
        return sorted(
            [{"label": k, "value_eur": round(v, 2), "pct": round(v / tot * 100, 2)}
             for k, v in agg.items()], key=lambda x: -x["value_eur"])

    # --- leverage: what the book actually controls -------------------------
    lev_rows = [r for r in rows if r.get("lev")]
    exposure_extra = sum(r["exposure_eur"] - r["value_eur"] for r in lev_rows)
    by_underlying = []
    if lev_rows:
        agg = defaultdict(lambda: [0.0, 0.0])
        for r in lev_rows:
            k = r["lev"].get("underlying") or r["name"]
            agg[k][0] += r["value_eur"]
            agg[k][1] += r["exposure_eur"]
        by_underlying = sorted(
            [{"label": k, "value_eur": round(v, 2), "exposure_eur": round(e, 2)}
             for k, (v, e) in agg.items()], key=lambda x: -x["exposure_eur"])

    # --- closed positions --------------------------------------------------
    closed_rows = []
    for isin, ds in L.closed.items():
        meta = insts.get(isin, {})
        pnl = sum(d.pnl_eur for d in ds)
        cost = sum(d.cost_eur for d in ds)
        proceeds = sum(d.proceeds_eur for d in ds)
        opened = min(d.open_dt for d in ds).date()
        shut = max(d.close_dt for d in ds).date()
        held = (shut - opened).days
        cflows = [(t.trade_dt.date(), t.total_eur) for t in trades
                  if t.isin == isin and t.total_eur is not None]
        rr = xirr(cflows) if held >= 30 else None
        closed_rows.append({
            "isin": isin,
            "name": meta.get("name") or isin,
            "kind": meta.get("kind", "EQUITY"),
            "ticker": meta.get("ticker") or pricing.SEED_TICKERS.get(isin),
            "currency": ds[0].currency,
            "qty": round(sum(d.qty for d in ds), 4),
            "cost_eur": round(cost, 2),
            "proceeds_eur": round(proceeds, 2),
            "pnl_eur": round(pnl, 2),
            "pnl_pct": round(pnl / cost * 100, 2) if cost else None,
            "pnl_price_eur": round(sum(d.pnl_price_eur for d in ds), 2),
            "pnl_fx_eur": round(sum(d.pnl_fx_eur for d in ds), 2),
            "fees_eur": round(sum(d.fees_eur for d in ds), 2),
            "opened": opened.isoformat(),
            "closed": shut.isoformat(),
            "held_days": held,
            "xirr": round(rr * 100, 1) if rr is not None else None,
            "trips": len(ds),
            "sector": _sector_of(meta, tk, kind),
            "country": _country_of(meta, tk, isin),
            "underlying": meta.get("underlying"),
            "underlying_ticker": meta.get("underlying_ticker"),
            "manual_price": meta.get("manual_price"),
            "price": None,
        })
    closed_rows.sort(key=lambda r: r["closed"], reverse=True)

    # --- cash / interest ---------------------------------------------------
    def _eur(t):
        """Account rows are in their native currency; convert with the day's rate."""
        v = t.local_value or 0.0
        if not t.currency or t.currency == "EUR":
            return v
        m = fxs.get(t.currency) or {}
        return v / (pricing.fx_on(m, _d(t.trade_dt.date()), 1.0) or 1.0)

    div_gross = sum(_eur(t) for t in txns if t.kind == "DIVIDEND")
    div_tax = sum(_eur(t) for t in txns if t.kind == "DIV_TAX")
    interest = sum(_eur(t) for t in txns if t.kind == "INTEREST")
    acct_fees = sum(_eur(t) for t in txns if t.kind == "FEE"
                    and t.source == "degiro:account")
    # A statement row that is none of these and names no instrument — a
    # promotion credit, a correction — is real money in the balance that
    # belongs to no holding. Named, so the residual below expects it.
    other_rows = [t for t in txns if t.kind == "OTHER" and t.source == "degiro:account"
                  and (not t.isin or t.isin in NON_INSTRUMENT_ISINS)]
    other_acct = sum(_eur(t) for t in other_rows)

    # DEGIRO's own running balance is authoritative — don't reconstruct it.
    acct = cash_account(txns, _eur)
    cash, cash_src, cash_asof = acct["now"], acct["src"], acct["asof"]
    cash_eur = cash.get("EUR", 0.0)
    for ccy, v in cash.items():
        if ccy != "EUR":
            cash_eur += v / (pricing.fx_on(fxs.get(ccy) or {}, _d(today), 1.0) or 1.0)

    deposit_flows = acct["flows"]
    has_deposits = any(t.kind in ("DEPOSIT", "WITHDRAWAL") for t in txns)
    ci = capital_in(txns, deposit_flows, _eur)
    deposits, withdrawals, pending, net_deposits = ci["deposits"], ci["withdrawals"], ci["pending"], ci["net"]

    # A trade entered by hand was never paid for out of this balance: it is a
    # position held where DEGIRO's statement cannot see it. The money is
    # capital all the same, so a hand-entered buy counts as paid in on its
    # date and a hand-entered sale as taken out. Left out, the whole position
    # reads as profit from the day it is typed in, and as one day's return on
    # the curve. Only on the account basis: without a statement there is no
    # cash balance for the trade to be missing from.
    outside_flows = [(t.trade_dt.date(), -t.total_eur) for t in trades
                     if t.source == "manual" and t.total_eur is not None] if has_deposits else []
    outside = sum(a for _, a in outside_flows)
    capital = net_deposits + outside
    external_flows = deposit_flows + outside_flows

    # Daily cash balance: the last state of each day per currency, carried
    # forward, converted at that day's rate.
    _by_day = acct["days"]
    cash_series: dict[str, float] = {}
    _carry: dict[str, float] = {}
    for day in sorted(_by_day):
        _carry.update(_by_day[day])
        tot = 0.0
        for ccy, v in _carry.items():
            tot += v if ccy == "EUR" else v / (
                pricing.fx_on(fxs.get(ccy) or {}, day, 1.0) or 1.0)
        cash_series[day] = tot
    acct_rows = [t for t in txns if t.source == "degiro:account"]
    acct_span = (min(t.trade_dt.date() for t in acct_rows).isoformat(),
                 max(t.trade_dt.date() for t in acct_rows).isoformat()) if acct_rows else None

    # --- curve, TWR, drawdown, benchmark -----------------------------------
    if light:
        # The homepage needs totals and movers, not a reconstructed daily curve.
        curve = {"series": [], "no_history": True, "twr_pct": None,
                 "max_drawdown_pct": None, "current_drawdown_pct": None,
                 "benchmark_gap_eur": None, "benchmark_label": None,
                 "mostly_carried": True, "gaps": 0, "carried_at_last_trade": []}
        cap = {}
    else:
        cap = {}
        curve = build_curve(trades, insts, tick_of, fxs, start, today, benchmark_key,
                            external_flows=external_flows, cash_series=cash_series,
                            cash_eur=cash_eur, lev_mode_v=LEVMODE, capture=cap)

    # With real deposits the honest XIRR is on money entering the ACCOUNT, and
    # the terminal value is securities plus cash. Without them, fall back to
    # money entering securities.
    if has_deposits:
        pf_flows = [(d, -a) for d, a in external_flows]
        pf_flows.append((today, total_mv + cash_eur))
    else:
        pf_flows = [(t.trade_dt.date(), t.total_eur) for t in trades if t.total_eur is not None]
        pf_flows.append((today, total_mv))
    pf_xirr = xirr(pf_flows)

    # Year to date, on the same basis as the inception figure. Withheld with it
    # when the line is mostly carried prices rather than market prices.
    jan1 = f"{today.year}-01-01"
    ytd_twr, ytd_eur, ytd_open, ytd_cov = (None, None, None, None)
    _series = curve.get("series") or []
    _ytd = [x for x in _series if x["d"] >= jan1]
    if _ytd:
        # Judged on this year's coverage, not the whole history: a gap in 2024
        # says nothing about whether 2026 can be measured.
        ytd_cov = round(sum(x.get("q", 1.0) for x in _ytd) / len(_ytd) * 100, 1)
        if ytd_cov >= 50:
            ytd_twr, ytd_eur, ytd_open = window_twr(_series, jan1)

    # Total return never goes blank. Capital in is the right denominator when
    # deposits are known; cost basis is the honest fallback when they are not.
    total_profit = total_mv + cash_eur - capital
    if capital and abs(capital) > 1:
        total_return_pct = total_profit / capital * 100
        total_return_basis = "capital in"
    elif total_cost:
        total_profit = total_mv - total_cost
        total_return_pct = total_profit / total_cost * 100
        total_return_basis = "cost basis"
    else:
        total_return_pct, total_return_basis = None, "unknown"

    conc = concentration([r["value_eur"] for r in rows])

    # --- risk, attribution, discipline -------------------------------------
    names_by_isin = {i: (m.get("name") or i) for i, m in insts.items()}
    div_by_isin: dict[str, float] = defaultdict(float)
    for t in txns:
        if t.kind in ("DIVIDEND", "DIV_TAX") and t.isin:
            div_by_isin[t.isin] += _eur(t)
    realised_by_isin: dict[str, float] = defaultdict(float)
    for d0 in L.disposals:
        realised_by_isin[d0.isin] += d0.pnl_eur

    contrib_basis = (capital if (capital and abs(capital) > 1)
                     else total_cost)
    # What the unattributable residual SHOULD be: margin interest, the account
    # fees that never entered a cost basis (the trade commission did, so it is
    # already inside each position's P&L and must not be counted twice here),
    # and the statement's other credits. Only commission the statement itself
    # charged is taken back out — a fee on a hand-entered trade sits in that
    # trade's cost and was never among the account's fees.
    acct_commission = sum(abs(t.fee_eur or 0.0) for t in trades if t.source != "manual")
    outside_fees = L.fees_eur - acct_commission
    expected_resid = interest + (acct_fees + acct_commission) + other_acct
    converted = sum(abs(_eur(t)) for t in txns if t.source == "degiro:account"
                    and t.kind in ("DIVIDEND", "DIV_TAX", "INTEREST", "FEE", "OTHER")
                    and t.currency and t.currency != "EUR")
    contrib = risk.contributions(
        rows, closed_rows, dict(div_by_isin), total_profit, contrib_basis,
        other={"realised_by_isin": dict(realised_by_isin), "names": names_by_isin},
        expected_residual=expected_resid, converted_eur=converted)

    buys_eur = sum(-t.total_eur for t in trades
                   if t.total_eur is not None and t.total_eur < 0)
    sells_eur = sum(t.total_eur for t in trades
                    if t.total_eur is not None and t.total_eur > 0)
    rt = behaviour.round_trips(trades, L.disposals, names_by_isin)
    avg_cap = behaviour.average_capital(curve.get("series") or [])
    costs = behaviour.cost_drag(
        acct_commission, L.autofx_eur, acct_fees, interest, avg_cap, start, today,
        buys_eur=buys_eur, sells_eur=sells_eur,
        fallback_capital=abs(capital) or total_cost or None,
        outside_fees_eur=outside_fees)
    costs["share_of_gross_return_pct"] = behaviour.drag_share_of_return(
        costs.get("drag_bps"), round(pf_xirr * 100, 1) if pf_xirr is not None else None)

    market = _market_risk(curve, rows, cap, fxs) if not light else {
        "enough": False, "why": "not computed on the home screen"}

    # --- year by year ------------------------------------------------------
    years = {"rows": [], "total": None}
    if not light and (curve.get("series") or []) and not curve.get("mostly_carried"):
        years = year_table(curve["series"], trades, txns, _eur, insts, tick_of,
                           cap.get("marks") or {}, cap.get("bench_hist") or {})
        years["benchmark"] = curve.get("benchmark_label")
        years["basis"] = curve.get("basis")
        # The table values the book the way the curve does: at each day's
        # close, leveraged products at the last price they traded. The Total
        # return tile is live, and values them by the rule chosen for today
        # (zero, by default). Say how far apart the two are, and why.
        last = curve["series"][-1]
        at_close = (cap.get("marks") or {}).get(last["d"]) or {}
        lev = [r for r in rows if r["kind"] == "DERIVATIVE"]
        lev_gap = sum(at_close.get(r["isin"], 0.0) - r["value_eur"] for r in lev)
        gap = last["v"] - (total_mv + (cash_eur if has_deposits else 0.0))
        years["headline"] = {"profit_eur": round(total_profit, 2), "gap_eur": round(gap, 2),
                             "leveraged_eur": round(lev_gap, 2),
                             "leveraged_n": sum(1 for r in lev if abs(at_close.get(r["isin"], 0.0) - r["value_eur"]) >= 0.5),
                             "prices_eur": round(gap - lev_gap, 2), "lev_mode": LEVMODE}

    return {
        "asof": datetime.now().isoformat(timespec="seconds"),
        "totals": {
            "value_eur": round(total_mv, 2),
            "cost_eur": round(total_cost, 2),
            "unreal_eur": round(total_mv - total_cost, 2),
            "unreal_pct": round((total_mv - total_cost) / total_cost * 100, 2) if total_cost else 0,
            "day_eur": round(day_total, 2),
            "day_price_eur": round(day_price_total, 2),
            "day_fx_eur": round(day_fx_total, 2),
            "day_pct": round(day_total / (total_mv - day_total) * 100, 2) if total_mv - day_total else 0,
            "day_tags": sorted({r["day_tag"] for r in rows if r.get("day_tag") and abs(r["day_eur"]) >= 0.5}),
            # the day's low and high so far, recorded from the live feed (pf/dayrange.py)
            "day_range": _day_range(),
            "day_ranges": _day_range(history=True),
            # Each holding's last finished session, added up. Late at night,
            # after both markets have closed, that is today in full; at noon,
            # before New York opens, it is yesterday for both.
            "last_eur": round(last_total, 2) if last_rows else None,
            "last_pct": round(last_total / last_open * 100, 2) if last_rows and last_open else None,
            "last_dates": sorted({r["last_date"] for r in last_rows if r.get("last_date")}),
            "realised_eur": round(L.realised_eur, 2),
            "realised_price_eur": round(L.realised_price_eur, 2),
            "realised_fx_eur": round(L.realised_fx_eur, 2),
            "fees_eur": round(L.fees_eur, 2),
            "outside_fees_eur": round(outside_fees, 2),
            "autofx_eur": round(L.autofx_eur, 2),
            "friction_eur": round(L.fees_eur + L.autofx_eur, 2),
            "dividends_eur": round(div_gross, 2),
            "div_tax_eur": round(div_tax, 2),
            "interest_eur": round(interest, 2),
            "account_fees_eur": round(acct_fees, 2),
            "other_eur": round(other_acct, 2),
            "other_rows": [{"date": _d(t.trade_dt.date()), "what": (t.note or "")[:60],
                            "eur": round(_eur(t), 2)} for t in other_rows[:12]],
            "deposits_eur": round(deposits, 2),
            "withdrawals_eur": round(withdrawals, 2),
            "net_deposits_eur": round(net_deposits, 2),
            "pending_deposits_eur": pending,
            "outside_eur": round(outside, 2),
            "outside_trades": len(outside_flows),
            "capital_eur": round(capital, 2),
            "cash_eur": round(cash_eur, 2),
            "cash_by_ccy": {k: round(v, 2) for k, v in cash.items()},
            "cash_asof": _d(cash_asof.date()) if cash_asof else None,
            "cash_source": cash_src,
            "net_worth_eur": round(total_mv + cash_eur, 2),
            "profit_eur": round(total_profit, 2),
            "basis": "account" if has_deposits else "invested capital",
            "ytd_twr_pct": ytd_twr,
            "ytd_coverage_pct": ytd_cov,
            "ytd_eur": ytd_eur,
            "ytd_open_eur": ytd_open,
            "total_return_pct": round(total_return_pct, 2) if total_return_pct is not None else None,
            "total_return_basis": total_return_basis,
            "twr_pct": curve.get("twr_pct"),
            "xirr": round(pf_xirr * 100, 1) if pf_xirr is not None else None,
            "positions": len(rows),
            "since": _d(start),
        },
        "positions": rows,
        "closed_positions": closed_rows,
        "by_sector": group("sector", only_kinds={"EQUITY", "ETF", "DERIVATIVE"}),
        "by_country": group("country"),
        "by_currency": group("currency"),
        "by_kind": group("kind"),
        "leverage": {
            "mode": LEVMODE,
            "modes": list(LEV_MODES),
            "derivative_positions": sum(1 for r in rows if r["kind"] == "DERIVATIVE"),
            "derivative_cost_eur": round(
                sum(r["cost_eur"] for r in rows if r["kind"] == "DERIVATIVE"), 2),
            "positions": len(lev_rows),
            "market_value_eur": round(sum(r["value_eur"] for r in lev_rows), 2),
            "exposure_eur": round(sum(r["exposure_eur"] for r in lev_rows), 2),
            "hidden_eur": round(exposure_extra, 2),
            "modelled": sum(1 for r in lev_rows if (r["lev"].get("model") or {}).get("ok")),
            "unmodelled": [r["name"] for r in lev_rows
                           if not (r["lev"].get("model") or {}).get("ok")],
            "by_underlying": by_underlying,
        },
        "concentration": {k: (round(v, 4) if isinstance(v, float) else v)
                          for k, v in conc.items()},
        "market_risk": market,
        "contribution": contrib,
        "years": years,
        "costs": costs,
        "discipline": behaviour.discipline(rt["trips"], rows, L.realised_eur),
        "round_trips": rt["trips"][:60],
        "curve": curve,
        "realised": [{
            "isin": d.isin,
            "name": (insts.get(d.isin) or {}).get("name", d.isin),
            "closed": _d(d.close_dt.date()),
            "qty": round(d.qty, 4),
            "pnl_eur": round(d.pnl_eur, 2),
            "pnl_price_eur": round(d.pnl_price_eur, 2),
            "pnl_fx_eur": round(d.pnl_fx_eur, 2),
            "held_days": d.holding_days,
        } for d in sorted(L.disposals, key=lambda x: x.close_dt, reverse=True)],
        "data_quality": {
            "orphan_sells": len(L.orphan_sells),
            "unpriced": [r["name"] for r in rows if r["stale"]],
            "unpriced_no_ticker": [r["name"] for r in rows if r["stale"] and not r.get("ticker")],
            "account_span": acct_span,
            "account_rows": len(acct_rows),
            "trade_rows": len(trades),
            "has_deposits": has_deposits,
            "deposit_count": sum(1 for t in txns if t.kind in ("DEPOSIT", "WITHDRAWAL")),
        },
        "benchmark": pricing.BENCHMARKS.get(benchmark_key, {}),
    }


# --------------------------------------------------------------------------
# daily equity curve, reconstructed from the ledger + price history
# --------------------------------------------------------------------------

def _day_return(v: float, f: float, prev: Optional[float]) -> Optional[float]:
    """
    One day's return with that day's money taken out: (v - f) / yesterday - 1.

    On the very first day there is no yesterday. The only money there is came
    in that day, so the return is on that money: v / f - 1. Leaving the day
    out, as this did, dropped the first day's result from every return figure
    (EUR 51 lost on the first EUR 2,550, here).
    """
    if prev is not None and prev > 0:
        r = (v - f) / prev - 1.0
        return r if r > -1.0 else None
    if f > 0:
        return v / f - 1.0
    return None


def window_twr(series: list, start_day: str) -> tuple:
    """
    Time-weighted return over a slice of the curve, chained the same way as the
    full-period figure: each day's return neutralises that day's external flow.
    Returns (twr_pct, eur_change_net_of_flows, opening_value).
    """
    pts = [x for x in series if x["d"] >= start_day and x.get("v") is not None]
    if len(pts) < 2:
        return None, None, None
    prior = [x for x in series if x["d"] < start_day and x.get("v") is not None]
    opening = prior[-1]["v"] if prior else 0.0
    twr, prev = 1.0, (prior[-1]["v"] if prior else None)
    flows = 0.0
    for x in pts:
        f = x.get("f") or 0.0
        flows += f
        r = _day_return(x["v"], f, prev)
        if r is not None:
            twr *= (1.0 + r)
        prev = x["v"]
    return round((twr - 1.0) * 100, 2), round(pts[-1]["v"] - opening - flows, 2), round(opening, 2)


def _curve_day(d: date) -> str:
    """
    The day on the curve that something dated `d` belongs to. The curve has no
    weekends, so a Saturday or a Sunday goes on the Monday after. DEGIRO never
    trades on one, but a deposit can arrive on one and a trade typed in by hand
    can carry any date — and a flow keyed to a day the curve never visits is
    never neutralised, so the money reads as Monday's return.
    """
    return _d(d + timedelta(days=7 - d.weekday()) if d.weekday() >= 5 else d)


def _cash_on(series: dict | None, day: str, fallback: float) -> float:
    """Cash balance on `day`, carried forward from the last known balance."""
    if not series:
        return 0.0
    if day in series:
        return series[day]
    earlier = [k for k in series if k <= day]
    return series[max(earlier)] if earlier else 0.0


def build_curve(trades, insts, tick_of, fxs, start: date, today: date,
                benchmark_key: str = "SP500",
                external_flows: list | None = None,
                cash_series: dict | None = None,
                cash_eur: float = 0.0,
                lev_mode_v: str = "zero",
                capture: dict | None = None) -> dict:
    """
    Rebuilds portfolio value for every day since the first trade, so TWR,
    drawdown and the same-cashflow benchmark all work from day one instead
    of only from the day snapshots started.

    When the account export contains real deposits and withdrawals, the curve
    is ACCOUNT level: value = securities + cash, and the flows that TWR
    neutralises are the deposits. That is the honest comparison, because it
    charges you for cash sitting idle and for margin.

    Without deposit history it falls back to INVESTED CAPITAL: flows are the
    euros moving into and out of positions, which ignores cash drag.

    `capture` is an optional sink for the internals — the per-instrument price
    history and the last traded prices. The risk panel needs exactly the same
    two years of daily closes to compute per-position betas, and fetching them
    a second time would double the slowest part of a page load. Filling a dict
    the caller passes in keeps them out of the JSON payload, which must not
    grow by a price series per instrument.
    """
    bench = pricing.BENCHMARKS.get(benchmark_key)
    bench_hist = pricing.get_history(bench["ticker"], _d(start)) if bench else {}

    # one batched round trip for all symbols, not one call per position
    # last price actually traded per instrument — the fallback for anything
    # with no feed, used only when the valuation rule asks for it
    #
    # Per instrument, every price ever traded with its date — not just the
    # final one. Carrying an unpriced name at the LAST price it ever traded
    # applies that price retroactively to the whole history, so the day a
    # turbo was bought for EUR 500 at 1.00 and later sold at 5.00 shows EUR
    # 2,500 of value appearing against EUR 500 of cash leaving. The line then
    # has a step in it that no market made, and a volatility or beta measured
    # off that line is measuring the step.
    px_at: dict[str, list] = defaultdict(list)
    last_px: dict[str, float] = {}
    for t in sorted(trades, key=lambda x: x.trade_dt):
        if t.price:
            px_at[t.isin].append((_d(t.trade_dt.date()), abs(t.price)))
            last_px[t.isin] = abs(t.price)

    def carried_px(isin: str, day: str) -> float:
        """The price actually traded nearest before `day`; the earliest if none."""
        lst = px_at.get(isin)
        if not lst:
            return 0.0
        i = bisect_right([d0 for d0, _ in lst], day)
        return lst[i - 1][1] if i else lst[0][1]

    hmap = pricing.get_history_many([t for t in tick_of.values() if t], _d(start))
    hist: dict[str, dict] = {isin: hmap.get(tk, {}) for isin, tk in tick_of.items() if tk}

    # anything with no usable series at all, for the whole period
    all_isins = {t.isin for t in trades if t.isin}
    unpriceable = {i for i in all_isins if not hist.get(i)}

    if capture is not None:
        capture["hist"] = hist
        capture["last_px"] = last_px
        capture["bench_hist"] = bench_hist

    account_mode = bool(external_flows)

    # position timeline
    by_day_qty: dict[str, dict] = defaultdict(dict)
    running: dict[str, float] = defaultdict(float)
    cur: dict[str, dict] = {}
    trade_flows: dict[str, float] = defaultdict(float)
    for t in sorted(trades, key=lambda x: x.trade_dt):
        d = _curve_day(t.trade_dt.date())
        running[t.isin] += (t.quantity or 0.0)
        cur = {k: v for k, v in running.items() if abs(v) > 1e-9}
        by_day_qty[d] = dict(cur)
        if t.total_eur is not None:
            trade_flows[d] += -t.total_eur     # money into securities is positive

    # the flows TWR must neutralise
    flows: dict[str, float] = defaultdict(float)
    if account_mode:
        for d0, amt in external_flows:
            flows[_curve_day(d0)] += amt       # deposits positive, withdrawals negative
    else:
        flows = trade_flows

    days = []
    d = start
    while d <= today:
        if d.weekday() < 5:
            days.append(_d(d))
        d += timedelta(days=1)

    def px_on(m: dict, day: str) -> Optional[float]:
        if not m:
            return None
        if day in m:
            return m[day]
        earlier = [k for k in m if k <= day]
        return m[max(earlier)] if earlier else None

    # The last day of each calendar year (and the last day of all): what each
    # holding was worth then is kept for the year table.
    mark_days = {max(x for x in days if x[:4] == y) for y in {x[:4] for x in days}}
    marks: dict[str, dict] = {}

    held: dict[str, float] = {}
    series, bench_units = [], 0.0
    peak, max_dd, twr, top = 0.0, 0.0, 1.0, 1.0
    prev_v = None
    unpriced_days = 0
    cov_sum = 0.0

    for day in days:
        if day in by_day_qty:
            held = by_day_qty[day]

        v = 0.0
        want = priced = 0
        priced_v = carried_v = 0.0
        worth = {} if day in mark_days else None
        for isin, qty in held.items():
            meta = insts.get(isin) or {}
            m = hist.get(isin)

            # Structurally unpriceable: a leveraged product with no feed, or a
            # share whose ticker never resolved. Either way we have NO series
            # for it in any period, so a day it is missing from is not evidence
            # of a data outage — it is a known constant.
            #
            # Treating these as missing data is what tore a ten-month hole in
            # this curve. One unresolved name (DeFi Technologies) held from
            # Nov 2025 to Jun 2026 was enough to push coverage under the
            # threshold and void every day it was held.
            ccy = meta.get("currency") or "EUR"
            f = pricing.fx_on(fxs.get(ccy) or {}, day, 1.0) if ccy != "EUR" else 1.0
            p = px_on(m, day) if m else None

            if p is None:
                cpx = carried_px(isin, day)
                carried_v += abs(qty * cpx)
                # No price for this instrument on this day — because it has no
                # feed at all, or because its series starts later than the day.
                # Carry it at the last price actually traded. This never voids
                # the day: a hole in the line is worse than a carried value,
                # and the honest thing is to say how much was carried.
                # Leveraged products are carried here even when the dashboard
                # values them at zero. Zeroing answers "what is it worth now";
                # applying that backwards would claim you had almost no money
                # through late 2025, when eight of your positions were turbos.
                # The history is about shape, and the shape was real.
                v += qty * cpx
                if worth is not None:
                    worth[isin] = qty * cpx
                if meta.get("kind") != "DERIVATIVE":
                    want += 1
                continue

            want += 1
            priced += 1
            val = qty * p / (f or 1.0)
            priced_v += abs(val)
            v += val
            if worth is not None:
                worth[isin] = val
        if worth is not None:
            marks[day] = worth

        # Coverage is a question about MONEY, not about headcount. Counting
        # positions meant one unpriced EUR 30 holding marked the whole day
        # unpriced, and holding it for half the history withheld TWR, drawdown
        # and year-to-date on a EUR 25,000 portfolio.
        denom = priced_v + carried_v
        coverage = (priced_v / denom) if denom > 0 else 1.0
        if coverage < 0.999:
            unpriced_days += 1
        cov_sum += coverage

        # The securities line, kept separately from the account line.
        #
        # Risk is measured on securities alone, neutralising the euros that
        # went into and out of positions that day. Cash has no beta; its
        # dilution belongs in the forward-beta weighting. (This was first done
        # because the account line jumped by a third on some days and beta
        # measured off it was a fact about those jumps. They were not cash
        # moving: the day's balance was being read off a cash-sweep row, or
        # off the oldest row of an instant — see cash_account. The account
        # line is clean now, and the separation is still the right one.)
        sec_v = v
        sec_f = trade_flows.get(day, 0.0)
        if account_mode:
            v += _cash_on(cash_series, day, cash_eur)

        f_day = flows.get(day, 0.0)
        r = _day_return(v, f_day, prev_v)
        if r is not None:
            twr *= (1.0 + r)
        prev_v = v
        # The fall from a high is measured on the return index, not on euros.
        # A deposit lifts the account's value, and in euros it fills in the
        # very fall it arrived during: EUR 2,500 paid in three weeks before
        # April 2025 made an 18% drop read as 14.
        peak = max(peak, v)
        top = max(top, twr)
        max_dd = min(max_dd, twr / top - 1.0)

        bp = px_on(bench_hist, day)
        if bp and f_day:
            bench_units += f_day / bp
        bench_v = bench_units * bp if bp else None

        series.append({"d": day,
                       "v": round(v, 2),
                       "s": round(sec_v, 2),
                       "sf": round(sec_f, 2) if sec_f else 0.0,
                       "q": round(coverage, 3),
                       "b": round(bench_v, 2) if bench_v else None,
                       "f": round(f_day, 2) if f_day else 0.0,
                       "x": round(twr, 5)})

    if capture is not None:
        capture["marks"] = marks

    priced_series = [s for s in series if s["v"]]
    # The line is always drawn, but a line built mostly from carried prices
    # must not also publish a confident TWR and drawdown off it.
    # Average share of portfolio VALUE that came from a real price. Withhold
    # the return figures only when most of the money was carried, not when a
    # rounding-error position happened to be missing.
    mean_cov = (cov_sum / len(series)) if series else 0.0
    carried_share = 1.0 - mean_cov
    mostly_carried = mean_cov < 0.5

    cur_v = priced_series[-1]["v"] if priced_series else 0.0
    cur_b = next((s["b"] for s in reversed(series) if s["b"] is not None), None)
    cur_dd = twr / top - 1.0

    if not priced_series:
        return {
            "series": [], "twr_pct": None, "max_drawdown_pct": None,
            "current_drawdown_pct": None, "peak_eur": None,
            "benchmark_value_eur": None, "benchmark_gap_eur": None,
            "benchmark_label": bench["label"] if bench else None,
            "benchmark_ticker": bench["ticker"] if bench else None,
            "basis": "account" if account_mode else "invested capital",
            "unpriced_days": unpriced_days,
            "no_history": True,
        }

    return {
        "series": series,
        "twr_pct": None if mostly_carried else round((twr - 1.0) * 100, 2),
        "max_drawdown_pct": None if mostly_carried else round(max_dd * 100, 2),
        "current_drawdown_pct": None if mostly_carried else round(cur_dd * 100, 2),
        "mostly_carried": mostly_carried,
        "carried_share_pct": round(carried_share * 100, 1),
        "peak_eur": round(peak, 2),
        "benchmark_value_eur": round(cur_b, 2) if cur_b else None,
        "benchmark_gap_eur": round(cur_v - cur_b, 2) if cur_b else None,
        "benchmark_label": bench["label"] if bench else None,
        "benchmark_ticker": bench["ticker"] if bench else None,
        "basis": "account" if account_mode else "invested capital",
        "unpriced_days": unpriced_days,
        "mean_coverage_pct": round(mean_cov * 100, 1),
        "worst_coverage_pct": round(min((x["q"] for x in series), default=1.0) * 100, 1),
        "no_history": False,
        "gaps": sum(1 for a, b in zip(series, series[1:])
                    if a["v"] is not None and b["v"] is None),
        "carried_at_last_trade": sorted(
            (insts.get(i) or {}).get("name", i) for i in unpriceable
            if (insts.get(i) or {}).get("kind") != "DERIVATIVE"),
    }


def year_table(series: list, trades: list, txns: list, eur, insts: dict, tick_of: dict,
               marks: dict, bench_hist: dict) -> dict:
    """
    Each calendar year on its own, read off the same daily line the curve
    draws, so a year's figures and the tiles above them cannot disagree.

    A year's result is what the account gained with the money paid in taken
    out: value at the end - value at the start - paid in. It is then split by
    where it came from, and the split is an identity, not an estimate:

        result = realised on sales + change in unrealised
                 + dividends after tax + interest and account fees + credits

    Commission and AutoFX are inside the first two, because they are part of
    what a position cost, so they are reported beside the split and not in it.
    `residual_eur` is what the identity leaves over. verify.py holds it to a
    euro, which is also the test that the daily cash line is right: read off
    the wrong row it left hundreds of euros over in one year.

    Per holding, the year's result needs no ledger replay: what it was worth
    at the end, less what it was worth at the start, less the euros put into
    it during the year, plus its dividends.
    """
    pts_all = [p for p in series if p.get("v") is not None]
    if len(pts_all) < 2:
        return {"rows": [], "total": None}
    trades = sorted((t for t in trades if t.kind in ("BUY", "SELL")), key=lambda t: t.trade_dt)
    on = lambda t: _curve_day(t.trade_dt.date())          # the day it lands on the curve
    L = build_ledger(trades)

    acc: dict = defaultdict(lambda: defaultdict(float))
    put: dict = defaultdict(lambda: defaultdict(float))    # year -> isin -> euros put in
    div: dict = defaultdict(lambda: defaultdict(float))    # year -> isin -> dividends
    for d0 in L.disposals:
        acc[_curve_day(d0.close_dt.date())[:4]]["realised"] += d0.pnl_eur
    for t in trades:
        a = acc[on(t)[:4]]
        a["trades"] += 1
        a["buys" if t.kind == "BUY" else "sells"] += 1
        a["commission"] += abs(t.fee_eur or 0.0)
        a["autofx"] += abs(t.autofx_cost_eur or 0.0)
        if t.source != "manual":
            a["commission_acct"] += abs(t.fee_eur or 0.0)
        if t.total_eur is not None:
            put[on(t)[:4]][t.isin] += -t.total_eur
    for t in txns:
        if t.source != "degiro:account":
            continue
        y = on(t)[:4]
        if t.kind in ("DIVIDEND", "DIV_TAX"):
            acc[y]["dividends"] += eur(t)
            if t.isin:
                div[y][t.isin] += eur(t)
        elif t.kind == "INTEREST":
            acc[y]["interest"] += eur(t)
        elif t.kind == "FEE":
            acc[y]["fees"] += eur(t)
        elif t.kind == "OTHER" and (not t.isin or t.isin in NON_INSTRUMENT_ISINS):
            acc[y]["other"] += eur(t)

    def cost_at(day: str) -> float:
        open_ = build_ledger([t for t in trades if on(t) <= day]).positions
        return sum(q.cost_eur for q in open_.values())

    def bench(day: str) -> Optional[float]:
        if day in bench_hist:
            return bench_hist[day]
        earlier = [k for k in bench_hist if k <= day]
        return bench_hist[max(earlier)] if earlier else None

    def label(isin: str) -> dict:
        meta = insts.get(isin) or {}
        name = meta.get("name") or isin
        if meta.get("kind") == "DERIVATIVE" and (meta.get("underlying_ticker") or meta.get("underlying")):
            what = "warrant" if "WARRANT" in name.upper() else "factor" if "FACTOR" in name.upper() else "turbo"
            return {"t": f"{meta.get('underlying_ticker') or meta['underlying']} {what}", "name": name}
        return {"t": tick_of.get(isin) or name[:22], "name": name}

    def holdings(m1: dict, m0: dict, years: list) -> tuple:
        res = defaultdict(float)
        for i, v in m1.items():
            res[i] += v
        for i, v in m0.items():
            res[i] -= v
        for y in years:
            for i, v in put[y].items():
                res[i] -= v
            for i, v in div[y].items():
                res[i] += v
        ranked = sorted(((v, i) for i, v in res.items() if abs(v) >= 0.5), reverse=True)
        best = [{**label(i), "eur": round(v, 2)} for v, i in ranked[:3] if v > 0]
        worst = [{**label(i), "eur": round(v, 2)} for v, i in ranked[::-1][:3] if v < 0]
        return best, worst

    def span(pts: list, prev: Optional[dict], years: list, un_start: float) -> dict:
        start = prev["v"] if prev else 0.0
        paid = sum(x.get("f") or 0.0 for x in pts)
        end = pts[-1]["v"]
        twr, top, dd, pv = 1.0, 1.0, 0.0, (prev["v"] if prev else None)
        for x in pts:
            r = _day_return(x["v"], x.get("f") or 0.0, pv)
            if r is not None:
                twr *= 1.0 + r
            pv = x["v"]
            top = max(top, twr)
            dd = min(dd, twr / top - 1.0)
        result = end - start - paid
        b0 = (prev.get("b") or 0.0) if prev else 0.0
        b1 = next((x["b"] for x in reversed(pts) if x.get("b") is not None), None)
        p0, p1 = bench(prev["d"] if prev else pts[0]["d"]), bench(pts[-1]["d"])
        b_pct = (p1 / p0 - 1.0) * 100 if p0 and p1 else None
        b_res = (b1 - b0 - paid) if b1 is not None else None
        a = defaultdict(float)
        for y in years:
            for k, v in acc[y].items():
                a[k] += v
        un_end = pts[-1]["s"] - cost_at(pts[-1]["d"])
        costs = a["interest"] + a["fees"] + a["commission_acct"]
        named = a["realised"] + (un_end - un_start) + a["dividends"] + costs + a["other"]
        best, worst = holdings(marks.get(pts[-1]["d"]) or {}, (marks.get(prev["d"]) or {}) if prev else {}, years)
        r2 = lambda v: None if v is None else round(v, 2)
        return {
            "from": pts[0]["d"], "to": pts[-1]["d"], "days": len(pts),
            "start_eur": r2(start), "paid_in_eur": r2(paid), "end_eur": r2(end),
            "result_eur": r2(result), "twr_pct": r2((twr - 1.0) * 100),
            "bench_pct": r2(b_pct),
            "ahead_pts": r2((twr - 1.0) * 100 - b_pct) if b_pct is not None else None,
            "bench_result_eur": r2(b_res),
            "ahead_eur": r2(result - b_res) if b_res is not None else None,
            "realised_eur": r2(a["realised"]), "unreal_change_eur": r2(un_end - un_start),
            "dividends_eur": r2(a["dividends"]), "costs_eur": r2(costs), "other_eur": r2(a["other"]),
            "residual_eur": r2(result - named),
            "unreal_end_eur": r2(un_end), "drawdown_pct": r2(dd * 100),
            "trades": int(a["trades"]), "buys": int(a["buys"]), "sells": int(a["sells"]),
            "trading_costs_eur": r2(a["commission"] + a["autofx"]),
            "best": best, "worst": worst,
        }

    years = sorted({x["d"][:4] for x in pts_all})
    rows, prev, un = [], None, 0.0
    for y in years:
        pts = [x for x in pts_all if x["d"][:4] == y]
        row = {"year": y, **span(pts, prev, [y], un)}
        rows.append(row)
        prev, un = pts[-1], row["unreal_end_eur"]
    # whatever is dated after the curve's last day belongs to no year yet
    return {"rows": rows, "total": {"year": "all", **span(pts_all, None, sorted(acc), 0.0)}}


def alerts(d: dict, move_pct: float = 3.0) -> list:
    """
    What changed, from data already on hand. Deliberately not a live feed —
    each item names a position and says what to look at.
    """
    if d.get("empty"):
        return []
    out, T = [], d["totals"]

    for r in sorted(d.get("positions", []), key=lambda x: -abs(x.get("day_pct") or 0)):
        if abs(r.get("day_pct") or 0) >= move_pct:
            out.append({"level": "up" if r["day_pct"] > 0 else "down",
                        "code": "MOVE",
                        "text": f"{r['name'][:34]} {r['day_pct']:+.2f}% today",
                        "detail": f"{r['day_eur']:+,.0f} EUR"})

    for r in d.get("positions", []):
        ko = (r.get("lev") or {}).get("knockout_pct")
        if ko is not None and ko < 20:
            out.append({"level": "warn", "code": "KNOCKOUT",
                        "text": f"{r['name'][:34]} is {ko:.1f}% from its barrier",
                        "detail": "a knockout loses the position entirely"})

    if T.get("cash_eur", 0) < 0:
        out.append({"level": "warn", "code": "MARGIN",
                    "text": f"Cash is {T['cash_eur']:,.2f} EUR — you are on margin",
                    "detail": f"{T.get('interest_eur', 0):,.2f} EUR of interest paid to date"})

    c = d.get("concentration") or {}
    if c.get("top1", 0) > 0.15:
        out.append({"level": "warn", "code": "CONCENTRATION",
                    "text": f"Largest position is {c['top1']*100:.1f}% of the book",
                    "detail": f"effective positions {c.get('effective_n', 0):.1f} of {c.get('n', 0)}"})

    q = d.get("data_quality") or {}
    if q.get("unpriced"):
        names = ", ".join(q["unpriced"][:3]) + (f" and {len(q['unpriced']) - 3} more" if len(q["unpriced"]) > 3 else "")
        why = (" — no ticker yet. Tickers are looked up on Yahoo by themselves; one that stays here has none in its"
               " currency: type it in under 'edit' on its row." if q.get("unpriced_no_ticker") else
               " — Yahoo did not answer for them and no close is stored yet; priced on its next answer.")
        out.append({"level": "info", "code": "STALE",
                    "text": f"{len(q['unpriced'])} holding(s) have no live price",
                    "detail": names + why + " Until then they stand at what you paid."})

    out.extend(rule_alerts(d))

    # The watchlist's: stocks that newly qualify, and pins at your buy price —
    # read from the last scan, never fetched on the home screen's time.
    try:
        from . import watch
        out.extend(watch.alerts())
    except Exception:
        pass
    # The euro's: a big day against your dollars, the rate gap moving fast.
    try:
        from . import euro
        out.extend(euro.alerts(d))
    except Exception:
        pass
    # The indices': breadth, concentration and the VIX curve at extremes (pf/idx.py).
    try:
        from . import idx
        out.extend(idx.alerts())
    except Exception:
        pass
    # READ: a story of importance 5 written in the last day (pf/reader.py).
    try:
        from . import reader
        out.extend(reader.alerts())
    except Exception:
        pass
    # POL: a member of Congress bought a stock you hold, filed in the last days (pf/pol.py).
    try:
        from . import pol
        out.extend(pol.alerts())
    except Exception:
        pass
    # FLOW: 90% days, a follow-through or a breadth thrust, hedge funds de-grossing, DIX at an extreme (pf/flow.py).
    try:
        from . import flow
        out.extend(flow.alerts())
    except Exception:
        pass
    # SYS: a background job failing, a day beyond its own rhythm (pf/health.py).
    try:
        from . import health
        out.extend(health.alerts())
    except Exception:
        pass
    # WEEK: at the weekend, the week's review is ready (pf/weekly.py); and every alert is kept, once a day, for it.
    try:
        from . import weekly
        out.extend(weekly.alerts())
        weekly.record_alerts(out)
    except Exception:
        pass

    if not out:
        out.append({"level": "ok", "code": "QUIET",
                    "text": "Nothing moved beyond ±3% and nothing needs attention",
                    "detail": "no action today"})
    return out


RULE_KINDS = ("price_below", "price_above", "move_pct", "weight_above",
              # the oil page's: Brent, WTI, Hormuz traffic, Dutch diesel (pf.oil.ALERT_KINDS)
              "oil_brent_above", "oil_brent_below", "oil_wti_above", "oil_wti_below",
              "oil_hormuz_above", "oil_hormuz_below", "oil_nldiesel_above", "oil_nldiesel_below",
              # the euro page's (pf.euro.ALERT_KINDS)
              "eur_usd_above", "eur_usd_below")


def rule_alerts(d: dict) -> list:
    """
    Alerts from rules you set yourself, evaluated against today's figures.

    These sit alongside the derived ones rather than replacing them: the
    derived alerts catch what you did not think to watch for, and these catch
    what you did. A rule on a position you have since sold is reported as
    such rather than silently ignored — a rule that quietly stops firing is
    worse than no rule.
    """
    try:
        rules = store.list_rules()
    except Exception:
        return []
    if not rules:
        return []

    by_isin = {r["isin"]: r for r in d.get("positions", [])}
    by_tk = {r.get("ticker"): r for r in d.get("positions", []) if r.get("ticker")}
    out = []
    oil_vals = None
    for rule in rules:
        if not rule.get("enabled"):
            continue
        if str(rule.get("kind", "")).startswith("eur_"):
            # The euro tests the market: today's rate from a dollar holding if
            # the dashboard has one, else what the euro page last read.
            from . import euro
            mv = euro.live_move(d)
            hit = euro.check_rule(rule["kind"], rule["value"], fx_live=mv["fx"] if mv else None)
            if hit:
                out.append({"level": hit["level"], "code": "EUR", "text": hit["text"],
                            "detail": rule.get("note") or "a euro rule you set on EUR",
                            "rule_id": rule["id"]})
            continue
        if str(rule.get("kind", "")).startswith("oil_"):
            # Oil rules test the market, not a holding — from what the OIL page
            # last fetched, never a download on the home screen's time.
            from . import oil
            if oil_vals is None:
                try:
                    oil_vals = oil.alert_values()
                except Exception:
                    oil_vals = {}
            hit = oil.check_rule(rule["kind"], rule["value"], oil_vals)
            if hit:
                out.append({"level": hit["level"], "code": "OIL", "text": hit["text"],
                            "detail": rule.get("note") or "an oil rule you set on OIL",
                            "rule_id": rule["id"]})
            continue
        p = by_isin.get(rule.get("isin")) or by_tk.get(rule.get("ticker"))
        label = rule.get("ticker") or rule.get("isin") or "?"
        if not p:
            out.append({"level": "info", "code": "RULE",
                        "text": f"Rule on {label} no longer matches a holding",
                        "detail": "you have sold it, or the ticker changed",
                        "rule_id": rule["id"]})
            continue

        kind, v = rule["kind"], rule["value"]
        px, mv, w = p.get("price"), p.get("day_pct"), p.get("weight")
        hit, text = False, ""
        if kind == "price_below" and px is not None and px < v:
            hit, text = True, f"{label} is {px:.2f} {p.get('currency','')} — below {v:g}"
        elif kind == "price_above" and px is not None and px > v:
            hit, text = True, f"{label} is {px:.2f} {p.get('currency','')} — above {v:g}"
        elif kind == "move_pct" and mv is not None and abs(mv) >= abs(v):
            hit, text = True, f"{label} moved {mv:+.2f}% today — your threshold is {abs(v):g}%"
        elif kind == "weight_above" and w is not None and w > v:
            hit, text = True, f"{label} is {w:.1f}% of the book — above {v:g}%"

        if hit:
            out.append({"level": "warn", "code": "RULE", "text": text,
                        "detail": rule.get("note") or "a rule you set",
                        "rule_id": rule["id"]})
    return out


def snapshot_today(payload: dict):
    """Bank today's value so history accrues even if a price feed dies later."""
    if payload.get("empty"):
        return
    t = payload["totals"]
    store.write_snapshot(
        date.today().isoformat(), t["value_eur"], t["cost_eur"], 0.0, 0.0,
        [{"isin": p["isin"], "qty": p["qty"], "price": p["price"],
          "value_eur": p["value_eur"]} for p in payload["positions"]])


# --------------------------------------------------------------------------
# one position, in detail
# --------------------------------------------------------------------------

def position_detail(key: str, days: int = 400) -> dict:
    """
    Everything about a single holding: the popup on PORT and the per-ticker
    page both read this.

    `key` is an ISIN or a ticker, because the popup knows the ISIN and a link
    typed as /t/NVDA knows only the symbol.

    Deliberately includes the TRADES with their dates and prices. A price
    chart without your own entries on it is a chart of a company; with them it
    is a chart of a decision, which is the only version worth looking at.
    """
    key = (key or "").strip()
    d = dashboard("SP500")
    if d.get("empty"):
        return {"error": "no transactions imported yet"}

    up = key.upper()
    row = next((r for r in d["positions"] if r["isin"] == key
                or (r.get("ticker") or "").upper() == up), None)
    closed = next((r for r in d.get("closed_positions", []) if r["isin"] == key
                   or (r.get("ticker") or "").upper() == up), None)
    if not row and not closed:
        return {"error": f"nothing held or ever traded under {key!r}"}

    base = row or closed
    isin = base["isin"]
    ticker = base.get("ticker") or pricing.SEED_TICKERS.get(isin)
    ccy = base.get("currency") or "EUR"

    txns = store.load_txns()
    trades = [t for t in txns if t.isin == isin and t.kind in ("BUY", "SELL")]
    divs = [t for t in txns if t.isin == isin and t.kind in ("DIVIDEND", "DIV_TAX")]

    start = (date.today() - timedelta(days=days)).isoformat()
    hist = pricing.get_history(ticker, start) if ticker else {}

    # The row's live price belongs on the end of the line: the chart should
    # agree with the number above it.
    if row and row.get("price") and hist:
        hist = {**hist, _d(date.today()): row["price"]}

    beta = ((d.get("market_risk") or {}).get("position_betas") or {}).get(isin)
    contrib = next((c for c in (d.get("contribution") or {}).get("items", [])
                    if c["isin"] == isin), None)

    # Its own news, from the cache the brief thread fills.
    items = []
    try:
        cached = store.cache_get("news") or {}
        items = [n for n in (cached.get("items") or [])
                 if ticker and ticker.upper() in (n.get("tickers") or [])][:6]
    except Exception:
        pass

    # Its next earnings date, if this week's sweep happened to catch it.
    earn = None
    try:
        for day in (store.cache_get("earnings") or {}).get("days", []):
            for e in day.get("events", []):
                if e.get("ticker") == ticker:
                    earn = e
                    break
    except Exception:
        pass

    lo = min(hist.values()) if hist else None
    hi = max(hist.values()) if hist else None
    return {
        "isin": isin,
        "ticker": ticker,
        "name": base.get("name") or isin,
        "kind": base.get("kind"),
        "currency": ccy,
        "sector": base.get("sector"),
        "country": base.get("country"),
        "open": bool(row),
        "position": row,
        "closed": closed,
        "history": [{"d": k, "c": round(v, 4)} for k, v in sorted(hist.items())],
        "history_source": ("live + cache" if hist else "none"),
        "lo": round(lo, 4) if lo is not None else None,
        "hi": round(hi, 4) if hi is not None else None,
        "trades": [{
            "d": _d(t.trade_dt.date()),
            "kind": t.kind,
            "qty": round(abs(t.quantity or 0), 4),
            "price": round(abs(t.price or 0), 4),
            "eur": round(t.total_eur or 0, 2),
            "fee_eur": round(abs(t.fee_eur or 0), 2),
        } for t in sorted(trades, key=lambda x: x.trade_dt)],
        "realised_eur": round(sum(x.pnl_eur for x in
                                  build_ledger(trades).disposals), 2) if trades else 0.0,
        "dividends_eur": round(sum(t.local_value or 0 for t in divs
                                   if t.kind == "DIVIDEND"), 2),
        "div_tax_eur": round(sum(t.local_value or 0 for t in divs
                                 if t.kind == "DIV_TAX"), 2),
        "beta": beta,
        "contribution": contrib,
        "news": items,
        "earnings": earn,
        "totals": {"net_worth_eur": d["totals"].get("net_worth_eur"),
                   "value_eur": d["totals"].get("value_eur")},
    }
