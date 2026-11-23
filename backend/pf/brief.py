"""
The morning-brief data: news, the macro week, the earnings week.

All three are slow, all three come from someone else's server, and none of
them may ever run inside a request. That lesson is already paid for: building
the live-price base on the web server's event loop froze the whole app every
time the network was slow. So this module owns a background thread, writes
everything to the SQLite cache, and hands requests whatever it has — with the
age attached, so a stale panel says it is stale instead of pretending.

Refresh intervals are set by how fast each thing actually changes:

    news        10 minutes   headlines are the only thing that moves hourly
    macro        6 hours     a published release schedule does not change
    earnings    12 hours     one call per holding, and dates move rarely
"""

from __future__ import annotations

import threading
import time
from datetime import date, timedelta
from typing import Optional

from . import store, news, feeds, market, geo, sentiment, calendar as cal

TTL = {"news": 600, "macro": 6 * 3600, "earnings": 12 * 3600,
       # Rates are daily series published after the close; refetching them
       # more often than this asks the same question of the same answer.
       "rates": 6 * 3600,
       # The board rebuilds from cached history plus whatever the live feed
       # has, so this is cheap — it is the freshness of the non-streaming
       # rows that this interval sets.
       "market": 120,
       # Sentiment is a published number that moves through the session;
       # geopolitics is structure plus a news join, and changes far slower.
       "sentiment": 900,
       "geo": 600}

# After a failure, wait this long before trying that source again rather than
# hammering a provider that is down or a key that is wrong.
BACKOFF = 300



def book_tickers(positions: list) -> tuple[list, dict, dict, list]:
    """
    (tickers, weights, names, reporters) for the news and the earnings week.

    A turbo has no ticker of its own, but its share's report is the day it is
    most likely to be knocked out: it counts through its underlying, weighted by
    its exposure, and adds to a share held outright (build 52). A fund or an ETC
    reports nothing, so it is not among the reporters. Most valuable first: the
    per-symbol calls are rate-limited, so they are spent on what matters.
    """
    pos = [r for r in positions if not r.get("excluded")]
    book = sum(r.get("value_eur") or 0 for r in pos)
    tickers, weights, names, reporters = [], {}, {}, []
    for r in pos:
        if r.get("kind") == "DERIVATIVE":
            tk = r.get("underlying_ticker")
            w = (r.get("exposure_eur") or 0) / book * 100 if book else 0.0
            nm = f"{r.get('underlying') or tk} (turbo)"
        else:
            tk, w, nm = r.get("ticker"), r.get("weight") or 0.0, r.get("name") or r.get("ticker")
        if not tk:
            continue
        if tk not in weights:
            tickers.append(tk)
            names[tk] = nm
        weights[tk] = weights.get(tk, 0.0) + w
        if r.get("kind") != "ETF" and tk not in reporters:
            reporters.append(tk)
    tickers.sort(key=lambda t: -weights.get(t, 0))
    reporters.sort(key=lambda t: -weights.get(t, 0))
    return tickers, weights, names, reporters

class Brief:
    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._fail: dict[str, float] = {}
        self._lock = threading.Lock()
        self.last_run: Optional[float] = None

    # -- reading: never blocks, never fetches --------------------------------
    def get(self) -> dict:
        n = store.cache_get("news") or {"items": [], "provider": news.provider_name(),
                                        "has_key": bool(news.api_key()),
                                        "error": "not fetched yet", "count": 0}
        m = store.cache_get("macro") or {"days": [], "count": 0,
                                         "error": "not fetched yet"}
        e = store.cache_get("earnings") or {"days": [], "count": 0,
                                            "error": "not fetched yet"}
        s = news.split(n)
        return {
            "news": {k: v for k, v in s.items() if k != "rest"},
            "news_total": len(s.get("rest") or []) + len(s.get("top") or []),
            "macro": m,
            "earnings": e,
            "preview": cal.day_preview(m, e),
            "ages": {"news": round(store.cache_age("news")),
                     "macro": round(store.cache_age("macro")),
                     "earnings": round(store.cache_age("earnings"))},
            "running": bool(self._thread and self._thread.is_alive()),
        }

    def market(self) -> dict:
        """The board, as last built. Never blocks, never fetches."""
        m = store.cache_get("market")
        if not m:
            return {"rows": [], "groups": [], "sessions": market.sessions(),
                    "error": "not built yet", "running": bool(
                        self._thread and self._thread.is_alive())}
        # Sessions are a clock, not a fetch, so they are recomputed on every
        # read rather than served from a cache that is up to two minutes old.
        m["sessions"] = market.sessions()
        m["age_s"] = round(store.cache_age("market"))
        # So is the stream's state. It was being stored at BUILD time, and the
        # board is built by this background thread — usually before any page
        # has opened a connection, so the streamer was legitimately "off" and
        # the Sources panel then reported it as broken for the rest of the
        # session, while the live dot in the header said otherwise.
        from . import stream
        snap = stream.STREAM.snapshot()
        m.setdefault("sources", {})["stream"] = {
            "state": snap.get("state"), "detail": snap.get("detail"),
            "symbols": snap.get("symbols"), "last_tick": snap.get("last_tick"),
        }
        return m

    def geo(self) -> dict:
        """The GEO payload, as last built. Never blocks, never fetches."""
        g = store.cache_get("geo")
        if not g:
            return {"error": "not built yet", "risks": [], "countries": {},
                    "running": bool(self._thread and self._thread.is_alive())}
        g["age_s"] = round(store.cache_age("geo"))
        # Recomputed on read: a countdown that is ten minutes stale is wrong.
        g["elections"] = geo.elections()
        return g

    def all_news(self) -> dict:
        """Everything held, for the NEWS page."""
        n = store.cache_get("news") or {"items": []}
        return news.split(n, top_n=0, banner_n=0)

    # -- the thread ----------------------------------------------------------
    def start(self):
        self._stop.clear()
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="pf-brief", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def refresh_now(self, what: Optional[str] = None) -> dict:
        """Force a refresh. Blocking — only called off the event loop."""
        with self._lock:
            self._fail.pop(what, None) if what else self._fail.clear()
            return self._tick(force=what or "all")

    # A payload that FAILED is retried on this interval instead of its normal
    # one. Rates are fetched every six hours because they are daily series —
    # but that also meant a source which failed once kept serving its error for
    # six hours, straight through a restart that had fixed it. Three feeds were
    # repaired and went on reporting themselves broken for the rest of the
    # evening. A stale success is fine; a stale failure is a lie.
    RETRY_FAILED = 300

    def _due(self, name: str, force: str) -> bool:
        if force in ("all", name):
            return True
        if time.time() < self._fail.get(name, 0):
            return False
        age = store.cache_age(name)
        cached = store.cache_get(name)
        if cached is None:
            return True
        if isinstance(cached, dict):
            # Built by older code: rebuild now, whatever its age. A payload
            # keeps the SHAPE of the build that made it, and a page reading a
            # field that build never wrote shows "no data" while looking fine.
            from . import BUILD
            if cached.get("_build") != BUILD:
                return True
            if self._payload_failed(cached):
                return age > self.RETRY_FAILED
        return age > TTL[name]

    @staticmethod
    def _payload_failed(payload: dict) -> bool:
        """
        Did this cached answer carry a failure? Either its own error, or one of
        the sub-sources it reports on — the sentiment payload has four, and
        three of them being broken does not set a top-level error.
        """
        if payload.get("error"):
            return True
        for v in payload.values():
            if isinstance(v, dict) and v.get("ok") is False:
                return True
        srcs = payload.get("sources")
        if isinstance(srcs, dict):
            for v in srcs.values():
                if isinstance(v, dict) and v.get("ok") is False:
                    return True
        return False

    def _tick(self, force: str = "") -> dict:
        done = {}

        # Tickers and weights come from a light dashboard: it skips the two
        # years of price history the curve needs, so it costs a tenth of a
        # second, and news ranking only needs to know what you hold and how
        # much of it.
        tickers, weights, names, reporters = [], {}, {}, []
        try:
            from . import service
            d = service.dashboard("SP500", light=True)
            tickers, weights, names, reporters = book_tickers(d.get("positions", []))
        except Exception as e:
            done["portfolio"] = f"could not read holdings: {e}"

        for name, fn in (
            ("news",     lambda: news.fetch(tickers, weights)),
            ("macro",    lambda: cal.macro_week()),
            ("earnings", lambda: cal.earnings_week(reporters, names, weights)),
            ("rates",    lambda: feeds.rates()),
            ("market",   self._build_market),
            ("sentiment", self._build_sentiment),
            ("geo",      self._build_geo),
        ):
            if not self._due(name, force):
                continue
            try:
                payload = fn()
                # A payload that failed is still cached — but only briefly, and
                # the error travels with it so the panel can say what is wrong
                # rather than showing an empty box.
                store.cache_put(name, payload)
                if payload.get("error"):
                    self._fail[name] = time.time() + BACKOFF
                done[name] = payload.get("error") or "ok"
            except Exception as e:
                self._fail[name] = time.time() + BACKOFF
                done[name] = f"{e.__class__.__name__}: {e}"

        self.last_run = time.time()
        return done

    def _build_market(self) -> dict:
        """
        Assemble the board: the daily rate series from the cache, the live
        quotes from the streamer, a year of history per symbol, and the
        correlation grid.
        """
        from . import stream, service, risk

        rates = (store.cache_get("rates") or {})
        snap = stream.STREAM.snapshot()
        b = market.board(rates.get("series") or {}, snap.get("quotes") or {})
        hists = b.pop("_hists", {})

        # Correlation wants the portfolio's own daily returns, measured the
        # same way the risk panel measures them.
        port_r = None
        you = None
        try:
            d = service.dashboard("SP500")
            if not d.get("empty"):
                port_r = risk.curve_returns(d["curve"].get("series") or [])
                you = _you_block(d)
        except Exception:
            pass

        b["correlations"] = market.correlations(hists, port_r)
        b["you"] = you
        if isinstance(b.get("risk"), dict):
            by_key = {r["k"]: r for r in b["rows"]}
            b["risk"]["book"] = market.book_read(by_key, you)
            # READ's live card and today's newest article, from READ's own cache: never a fetch from here
            try:
                from . import reader
                lv = store.cache_get("read:live") or {}
                today = [a for a in reader.listing(days=1) if a.get("d") == date.today().isoformat()]
                b["risk"]["coverage"] = {
                    "label": lv.get("label"), "main": lv.get("main"),
                    "article": ({k: today[0].get(k) for k in ("id", "headline", "kind_label", "importance")}
                                if today else None)}
            except Exception:
                b["risk"]["coverage"] = None
        b["sources"] = {
            "fred": rates.get("fred") or {"ok": False, "error": "not fetched"},
            "ecb": rates.get("ecb") or {"ok": False, "error": "not fetched"},
            "yahoo": {"ok": any(r.get("last") is not None
                                for r in b["rows"] if r["src"] == "yahoo")},
            "stream": {"state": snap.get("state"), "detail": snap.get("detail")},
        }
        return b

    def _build_sentiment(self) -> dict:
        """
        Fear & Greed from CNN and from here, the next Fed move, and GPR.

        Every part is isolated. This panel spent an evening showing three stale
        errors because ONE of the four raised — the GPR parser hit a pandas
        Timestamp where it expected a column name — and the exception discarded
        the whole payload, so the previous build's cached answers were served
        on and on. A source that fails must fail alone and say so; it must not
        be able to hide the state of its neighbours.
        """
        from . import pricing

        def safe(name, fn, default=None):
            try:
                return fn()
            except Exception as e:
                return {"ok": False, "error": f"{name} raised "
                        f"{e.__class__.__name__}: {e}"[:300],
                        "fetched_at": time.time()} if default is None else default

        cnn = safe("CNN Fear & Greed", sentiment.cnn_fear_greed)
        gp = safe("the GPR index", sentiment.gpr)

        # The house version reuses the board's own history rather than
        # fetching anything again.
        hists, rates = {}, (store.cache_get("rates") or {}).get("series") or {}
        start = (date.today() - timedelta(days=430)).isoformat()
        for k, sym in (("SPX", "^GSPC"), ("VIX", "^VIX"), ("GOLD", "GC=F")):
            try:
                hists[k] = pricing.get_history(sym, start) or {}
            except Exception:
                hists[k] = {}
        # Put/call and volume breadth, the two CNN components with no source before
        # FLOW (build 51): read from what FLOW has stored, never fetched here.
        from . import flow
        extra = safe("FLOW's put/call and volume breadth", flow.fg_components, [])
        house = safe("the house fear & greed index",
                     lambda: sentiment.house_fear_greed(hists, rates, extra if isinstance(extra, list) else []))

        # The next Fed move is the first link of the chain BOND draws in full:
        # every fed funds contract, meeting by meeting. It used to be worked
        # out here from two contracts, which was right only when the month
        # after the meeting was not read (see ratepath.py).
        from . import ratepath
        fed = safe("the fed funds futures", ratepath.next_fed)

        return {"cnn": cnn, "house": house, "fed": fed, "gpr": gp,
                "error": None, "fetched_at": time.time()}

    def _build_geo(self) -> dict:
        """Risks and chokepoints joined to the book, plus the scenario table."""
        from . import service, pricing
        import json as _j
        from pathlib import Path

        d = service.dashboard("SP500")
        positions = [] if d.get("empty") else d.get("positions", [])
        news_items = (store.cache_get("news") or {}).get("items") or []

        centroids, drawable = {}, None
        try:
            wp = Path(__file__).resolve().parent.parent / "static" / "world.json"
            _w = _j.loads(wp.read_text())
            centroids = _w.get("centroids") or {}
            drawable = set(_w.get("paths") or {}) or None
        except Exception:
            pass

        start = (date.today() - timedelta(days=430)).isoformat()
        factor_hists = {}
        for k, sym in (("BRENT", "BZ=F"), ("EURUSD", "EURUSD=X"),
                       ("SPX", "^GSPC"), ("NDX", "^NDX")):
            try:
                factor_hists[k] = pricing.get_history(sym, start) or {}
            except Exception:
                factor_hists[k] = {}
        hists = {}
        for p in positions:
            tk = p.get("ticker")
            if not tk or p.get("excluded"):
                continue
            try:
                hists[tk] = pricing.get_history(tk, start) or {}
            except Exception:
                continue
        betas = geo.factor_betas(hists, factor_hists)

        # The book itself, trimmed to what the page draws. The matrix needs
        # every row, not only the ones a given risk happens to touch.
        book = [{"ticker": p.get("ticker") or (p.get("name") or "")[:10],
                 "name": p.get("name"), "sector": p.get("sector"),
                 "country": p.get("country"),
                 "value_eur": round(p.get("value_eur") or 0, 2),
                 "weight": p.get("weight"), "day_pct": p.get("day_pct")}
                for p in positions
                if not p.get("excluded") and (p.get("value_eur") or 0) > 0]
        book.sort(key=lambda r: -r["value_eur"])

        return {
            "positions": book,
            "risks": geo.risk_exposure(positions, news_items),
            "countries": geo.country_exposure(positions, on_map=drawable),
            "chokepoints": [{**c, "xy": geo.chokepoint_xy(c, centroids),
                             "news": [n for n in news_items
                                      if __import__("re").search(c["kw"],
                                          f"{n.get('headline','')} {n.get('summary','')}",
                                          __import__("re").I)][:3]}
                            for c in geo.CHOKEPOINTS],
            "elections": geo.elections(),
            "elections_as_of": geo.ELECTIONS_AS_OF,
            "shocks": geo.shock_table(positions, betas),
            "beta_coverage": len(betas),
            "total_eur": round(sum(p.get("value_eur") or 0 for p in positions
                                   if not p.get("excluded")), 2),
            "error": None, "fetched_at": time.time(),
        }

    def _run(self):
        # A short delay so starting the app is not three network calls at once
        # while the first page is still loading.
        self._stop.wait(3)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(30)


BRIEF = Brief()


def _you_block(d: dict) -> dict:
    """
    What the board means for this particular book — one summary, not a column
    on every row. Everything here is already computed elsewhere; this only
    puts it next to the market it refers to.
    """
    T = d.get("totals") or {}
    M = d.get("market_risk") or {}
    best = None
    for b in (M.get("benchmarks") or []):
        if b.get("key") == M.get("best_fit"):
            best = b
    ccy = {r["label"]: r["pct"] for r in (d.get("by_currency") or [])}
    sec = {r["label"]: r["pct"] for r in (d.get("by_sector") or [])}
    return {
        "beta": (M.get("forward") or {}).get("beta"),
        "beta_hist": (best or {}).get("beta"),
        "r2": (best or {}).get("r2"),
        "benchmark": (best or {}).get("label"),
        "vol_pct": (M.get("volatility") or {}).get("vol_pct"),
        "usd_pct": round(ccy.get("USD", 0.0), 1),
        "eur_pct": round(ccy.get("EUR", 0.0), 1),
        "energy_pct": round(sec.get("Energy", 0.0), 1),
        "tech_pct": round(sec.get("Technology", 0.0), 1),
        "day_fx_eur": T.get("day_fx_eur"),
        "day_price_eur": T.get("day_price_eur"),
        "cash_eur": T.get("cash_eur"),
        "net_worth_eur": T.get("net_worth_eur"),
    }


# --------------------------------------------------------------------------
# what changed: the home-screen movers panel
# --------------------------------------------------------------------------

def what_changed(positions: list[dict], n_weight: int = 5, n_side: int = 2) -> dict:
    """
    The five biggest positions and how they moved, then the best two and worst
    two movers that are NOT already in that five.

    The deduplication is the point. Your largest holding is usually also among
    the day's biggest movers in euro terms, and showing it twice wastes a row
    on a screen that only has nine. When a mover is already listed above, the
    next one down takes its place, so the panel always carries four movers
    rather than silently shrinking.
    """
    live = [p for p in positions if not p.get("excluded") and (p.get("value_eur") or 0) > 0]

    def moved(p):
        # the move the panel shows: now, or the last session when the market
        # has not traded since its close
        if (p.get("session") and p["session"] != "open" and abs(p.get("day_pct") or 0) < 0.01
                and p.get("last_pct") is not None):
            return p["last_pct"]
        return p.get("day_pct")

    weights = sorted(live, key=lambda p: -(p.get("value_eur") or 0))[:n_weight]
    shown = {p["isin"] for p in weights}

    def row(p, why):
        return {
            "isin": p["isin"],
            # Ticker only: on a dense screen a column of full company names is
            # a wall of text, and the ticker is what he trades under anyway.
            "ticker": p.get("ticker") or p.get("underlying_ticker") or (p.get("name") or "")[:8],
            "day_pct": p.get("day_pct"),
            "day_eur": p.get("day_eur"),
            # The page shows the session that just finished when a holding's
            # market has not traded since its close (term.js, shownMove).
            "session": p.get("session"), "day_tag": p.get("day_tag"),
            "last_pct": p.get("last_pct"), "last_eur": p.get("last_eur"),
            "weight": p.get("weight"),
            "value_eur": p.get("value_eur"),
            "stale": p.get("stale"),
            "why": why,
        }

    by_move = sorted([p for p in live if moved(p) is not None], key=lambda p: -(moved(p) or 0))

    ups, downs = [], []
    for p in by_move:
        if len(ups) >= n_side:
            break
        if p["isin"] in shown or (moved(p) or 0) <= 0:
            continue
        ups.append(row(p, "up"))
        shown.add(p["isin"])
    for p in reversed(by_move):
        if len(downs) >= n_side:
            break
        if p["isin"] in shown or (moved(p) or 0) >= 0:
            continue
        downs.append(row(p, "down"))
        shown.add(p["isin"])

    return {
        "weights": [row(p, "weight") for p in weights],
        "movers": ups + downs,
        "n_priced": sum(1 for p in live if not p.get("stale")),
        "n_live": len(live),
    }
