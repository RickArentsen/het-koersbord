"""
Live prices.

Yahoo runs a websocket that pushes a message every time one of your symbols
trades. yfinance ships a client for it, so the day change on this page can be
genuinely live rather than a number that moves when you press Refresh.

Three honest caveats, all of which this module is built around:

  * The endpoint is undocumented. Yahoo can change the protocol, throttle it,
    or drop it entirely, and there will be no announcement. So the websocket
    is never the only path: if it fails to connect, or connects and then goes
    quiet, the streamer falls back to polling the ordinary quote API and SAYS
    SO. The page shows which mode it is in. A feed that silently degrades to
    stale numbers is worse than one that admits it is polling.

  * Nothing ticks when the market is shut. Silence at 22:00 CET is not a bug,
    and the page distinguishes "connected, nothing trading" from "not
    connected".

  * A tick is one instrument. Rebuilding the whole dashboard for each one
    would be absurd, so the expensive part — quantities, cost basis, the FX
    rate that applied at purchase — is computed once into a BASE, and each
    tick only refolds arithmetic over it. `fold()` is a pure function and is
    tested against hand-computed numbers.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

from . import pricing

# How often the fallback poller asks for quotes. Yahoo's daily bar updates
# through the session, so this is genuinely fresh, just coarser than a tick.
POLL_SECONDS = 30

# If the socket connects but nothing arrives in this long, assume it is not
# actually delivering and fall back. Generous, because a quiet market is
# normal and must not be mistaken for a broken feed — the page distinguishes
# the two from `market_open`, not from this timer.
SILENCE_BEFORE_FALLBACK = 180

# Give up on the websocket for this long after it fails, rather than
# reconnecting in a tight loop against an endpoint that is refusing us.
RETRY_AFTER = 300

# How long the base — quantities, cost basis, yesterday's close — may go
# without being rebuilt. A trade entered in another tab appears within this
# long; shorter than this is pointless, because none of it moves intraday.
BASE_TTL = 300


class Streamer:
    """One background thread, one symbol set, one dictionary of live prices."""

    def __init__(self):
        self._lock = threading.Lock()
        self._quotes: dict[str, dict] = {}
        self._symbols: set[str] = set()
        self._thread: Optional[threading.Thread] = None
        self._base_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ws = None
        self.state = "off"            # off | connecting | streaming | polling
        self.detail = ""
        self.version = 0              # bumped whenever a price actually changes
        self.last_tick: Optional[float] = None
        self.connected_at: Optional[float] = None
        self._listeners = 0
        self._base: Optional[dict] = None
        self._base_at = 0.0
        self._base_lock = threading.Lock()
        self.base_error: Optional[str] = None
        self._mode: Optional[tuple] = None   # what the thread is doing, even while "off"

    # -- the expensive half, owned by this thread ------------------------
    #
    # Building the base replays the whole ledger and asks the quote API for
    # prices, so it takes anywhere from a tenth of a second to however long
    # Yahoo feels like taking. It must therefore NEVER happen on the web
    # server's event loop: while it ran, every other request — the dashboard,
    # the stylesheet, the page itself — was stuck behind it, which is exactly
    # what "the dashboard freezes" looked like.
    #
    # So it is cached here, refreshed by this background thread, and shared by
    # every connected page. A browser reconnecting in a loop now costs nothing
    # instead of triggering a rebuild per attempt.

    def base(self) -> Optional[dict]:
        with self._base_lock:
            return self._base

    def base_age(self) -> float:
        return time.time() - self._base_at if self._base_at else 1e9

    def expire_base(self):
        """
        The ledger just changed (an import, a trade typed in or removed). The
        background loop rebuilds at its next pass, within 15 seconds, rather
        than when the five minutes are up: until then the live net worth is
        the old book's while capital in is already the new one, and the home
        screen's total return is out by the whole trade.
        """
        with self._base_lock:
            self._base_at = 0.0

    def refresh_base(self) -> Optional[dict]:
        """Rebuild the base. Blocking — only ever called off the event loop."""
        try:
            # Imported here rather than at module scope: service is the layer
            # above this one, and importing it at the top would make the
            # dependency circular.
            from . import service
            b = service.live_base()
        except Exception as e:
            with self._base_lock:
                self.base_error = f"{e.__class__.__name__}: {e}"
            return self.base()
        with self._base_lock:
            self._base = b
            self._base_at = time.time()
            self.base_error = None
        # The market board's symbols ride along on the same subscription.
        # They are the indices, FX and futures on MKT; Yahoo pushes some of
        # them and not others, and each row says which it got.
        try:
            from . import market
            extra = market.YAHOO_SYMBOLS
        except Exception:
            extra = []
        self.set_symbols(list(b.get("symbols") or []) + list(extra))
        return b

    # -- public ----------------------------------------------------------
    def set_symbols(self, symbols):
        with self._lock:
            self._symbols = {s for s in symbols if s}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "state": self.state,
                "detail": self.detail,
                "version": self.version,
                "symbols": len(self._symbols),
                "last_tick": self.last_tick,
                "quotes": dict(self._quotes),
            }

    def acquire(self):
        """A page is watching. Start if this is the first one."""
        with self._lock:
            self._listeners += 1
            first = self._listeners == 1
        if first:
            self.start()

    def release(self):
        """A page stopped watching. Stop the feed when the last one goes."""
        with self._lock:
            self._listeners = max(0, self._listeners - 1)
            last = self._listeners == 0
        if last:
            self.stop()

    def start(self):
        # Clear the stop flag BEFORE testing whether the old thread is alive.
        # A page closing and reopening quickly leaves the previous thread
        # still winding down; if we returned early while it was mid-shutdown
        # the feed would stop for good. Clearing first means that thread
        # simply carries on.
        self._stop.clear()
        # A SECOND thread for the base, because the feed thread spends its
        # life blocked inside a websocket session. Sharing one thread would
        # mean the base went stale for as long as the socket stayed up — a
        # trade made in another tab would not appear for hours. Started
        # before the early return below, or a reconnect would leave the app
        # with a feed and no base.
        if not (self._base_thread and self._base_thread.is_alive()):
            self._base_thread = threading.Thread(target=self._base_loop,
                                                 name="pf-base", daemon=True)
            self._base_thread.start()
        if self._thread and self._thread.is_alive():
            # Revived mid-shutdown. Reloading a page closes its connection and
            # opens a new one within milliseconds: the listener count touches
            # zero, stop() writes "off", and the thread — which never actually
            # stopped — carries on streaming without re-announcing itself,
            # because it only reports its state when it (re)connects. So the
            # panel said "off · nobody is watching" all day while prices
            # ticked. Restore what the thread was last doing.
            with self._lock:
                if getattr(self, "_mode", None):
                    self.state, self.detail = self._mode
            return
        self.state, self.detail = "connecting", "opening the price feed"
        self._thread = threading.Thread(target=self._run, name="pf-stream", daemon=True)
        self._thread.start()

    def _base_loop(self):
        while not self._stop.is_set():
            b = self.base()
            due = bool(b and b.get("valid_until") and time.time() > b["valid_until"])
            if self.base_age() > BASE_TTL or due:
                self.refresh_base()
            self._stop.wait(15)

    def stop(self):
        self._stop.set()
        self.state, self.detail = "off", "nobody is watching"

    # -- the thread ------------------------------------------------------
    def _run(self):
        blocked_until = 0.0
        while not self._stop.is_set():
            if time.time() >= blocked_until and self._try_stream():
                # It worked and then ended. Reconnect, but never in a tight
                # loop: a socket that accepts us and drops immediately would
                # otherwise become a reconnect storm.
                self._stop.wait(3)
                continue
            blocked_until = time.time() + RETRY_AFTER
            self._poll_for(RETRY_AFTER)

    def _try_stream(self) -> bool:
        """
        Run the websocket until it dies or goes quiet. Returns True if it did
        real work (so the caller loops straight back and reconnects), False if
        it could not connect at all (so the caller falls back to polling).
        """
        try:
            import asyncio
            from yfinance import AsyncWebSocket
        except Exception as e:
            self._set_state("polling", f"streaming client unavailable ({e.__class__.__name__})")
            return False

        with self._lock:
            syms = sorted(self._symbols)
        if not syms:
            self._set_state("polling", "no symbols to subscribe to")
            return False

        got_any = False

        def on_message(msg):
            nonlocal got_any
            got_any = True
            self._apply_tick(msg)

        async def main():
            ws = AsyncWebSocket(verbose=False)
            self._ws = ws
            await ws.subscribe(syms)
            self._set_state("streaming", f"live from Yahoo, {len(syms)} symbols")
            self.connected_at = time.time()
            listen = asyncio.create_task(ws.listen(on_message))
            try:
                while not self._stop.is_set():
                    await asyncio.sleep(1)
                    quiet = time.time() - (self.last_tick or self.connected_at)
                    if quiet > SILENCE_BEFORE_FALLBACK:
                        raise TimeoutError(f"no ticks for {int(quiet)}s")
                    if listen.done():
                        raise ConnectionError("the socket closed")
            finally:
                listen.cancel()
                try:
                    await ws.close()
                except Exception:
                    pass
                self._ws = None

        try:
            asyncio.run(main())
            return True
        except Exception as e:
            # Connected and then failed is worth retrying soon; never
            # connected is worth backing off from.
            self._set_state("polling", f"stream dropped: {e.__class__.__name__} — polling instead")
            return bool(got_any)

    def _poll_for(self, seconds: float):
        """Fallback: ask the ordinary quote API on a timer."""
        end = time.time() + seconds
        while not self._stop.is_set() and time.time() < end:
            with self._lock:
                syms = sorted(self._symbols)
            if syms:
                try:
                    q = pricing.get_quotes(syms, fresh=True)
                    n = 0
                    for sym, d in q.items():
                        if d.get("price"):
                            # A quote rebuilt from the cache has a DERIVED
                            # previous close; never let it replace one the feed
                            # already holds.
                            from_cache = str(d.get("source", "")).startswith("cache")
                            n += self._put(sym, d["price"],
                                           None if from_cache else d.get("prev_close"),
                                           src="poll", asof=d.get("asof"))
                    self._set_state("polling",
                                    f"polling every {POLL_SECONDS}s, {len(q)} symbols"
                                    if q else "polling — the quote API returned nothing")
                except Exception as e:
                    self._set_state("polling", f"quote API error: {e.__class__.__name__}")
            self._stop.wait(POLL_SECONDS)

    # -- state -----------------------------------------------------------
    def _set_state(self, state: str, detail: str):
        # Once stopped, stay stopped. A poll loop mid-iteration when the last
        # viewer left would otherwise write "polling" back over "off" on its
        # way out, and the status endpoint would claim a feed was running
        # when no thread was left to run it.
        #
        # But remember what the thread was DOING either way. See start().
        with self._lock:
            if state != "off":
                self._mode = (state, detail)
        if self._stop.is_set() and state != "off":
            return
        with self._lock:
            self.state, self.detail = state, detail

    def _apply_tick(self, msg: dict):
        try:
            sym = msg.get("id")
            px = msg.get("price")
            if not sym or px is None:
                return
            self._put(sym, float(px), msg.get("previous_close"),
                      src="stream", hours=msg.get("market_hours"),
                      ts=msg.get("time"))
        except Exception:
            pass

    def _put(self, sym, price, prev_close=None, src="", hours=None,
             ts=None, asof=None) -> int:
        """Record a price. Returns 1 if it actually changed, 0 if identical."""
        with self._lock:
            old = self._quotes.get(sym)
            if old and old.get("price") == price:
                old["seen"] = time.time()
                self.last_tick = time.time()
                return 0
            self._quotes[sym] = {
                "price": price,
                # A tick does not always carry the previous close; the one we
                # already had is right, so never overwrite a good value with None.
                "prev_close": (float(prev_close) if prev_close not in (None, 0)
                               else (old or {}).get("prev_close")),
                "market_hours": hours if hours is not None else (old or {}).get("market_hours"),
                "src": src,
                "asof": asof,
                "ts": (float(ts) / 1000.0 if ts else time.time()),
                "seen": time.time(),
            }
            self.version += 1
            self.last_tick = time.time()
            return 1


STREAM = Streamer()


# --------------------------------------------------------------------------
# folding a tick into the day change
# --------------------------------------------------------------------------

def fold(base: dict, quotes: dict, states: Optional[dict] = None) -> dict:
    """
    Recompute value and day change from live prices. Pure — no I/O, no clock:
    `states` says which session each exchange is in ("open", "pre", "post",
    "closed"), from the caller's clock, so a pre-market or after-hours move
    can be tagged as one.

    `base` is the expensive half, built once per dashboard load:
        {"positions": [{isin, name, ticker, qty, currency, prev_close, fx_prev,
                        cost_eur, price (last known), fx (last known)}],
         "fx_symbol": {"USD": "EURUSD=X", ...},
         "cash_eur": float}

    The two legs of the day change, and what they actually mean:

      PRICE  what the share did, measured in its own currency and then
             converted at today's rate:   qty x (price - prev_close) / fx_now

      FX     what the currency did to the position you already owned, holding
             the share price still at yesterday's close:
                                          qty x prev_close x (1/fx_now - 1/fx_prev)

    They are separate because for a euro investor holding US shares they
    routinely pull in opposite directions, and "Microsoft rose but the dollar
    fell so you made nothing" is a different fact from "Microsoft fell".
    Together they are exactly the change in the position's euro value.
    """
    # ONE definition of yesterday, and it is the dashboard's.
    #
    # The base is rebuilt from the dashboard every five minutes and carries the
    # previous close and the previous FX rate that the page itself shows on
    # load. Quotes arriving on the feed may carry their own "previous close",
    # and that used to win — which let a same-day price, written in by the
    # quote API's cache fallback after a single Yahoo hiccup, become yesterday
    # for the rest of the session. The live number then disagreed with the
    # page-load number for the same stock at the same moment. The feed now
    # supplies PRICES; the base supplies the reference they are measured from.
    fxsym = base.get("fx_symbol") or {}
    live_fx: dict[str, float] = {}
    prev_fx: dict[str, float] = {}
    for ccy, sym in fxsym.items():
        q = quotes.get(sym)
        if q and q.get("price"):
            live_fx[ccy] = float(q["price"])
            if q.get("prev_close"):
                prev_fx[ccy] = float(q["prev_close"])

    from .sessions import tag as _tag
    rows, total_v, d_px, d_fx, moved, tags = [], 0.0, 0.0, 0.0, 0, set()
    for p in base.get("positions", []):
        ccy = p.get("currency") or "EUR"
        fx_now = 1.0 if ccy == "EUR" else (live_fx.get(ccy) or p.get("fx") or 1.0)
        fx_prev = 1.0 if ccy == "EUR" else (p.get("fx_prev") or prev_fx.get(ccy)
                                            or p.get("fx") or fx_now)
        q = quotes.get(p.get("ticker")) if p.get("ticker") else None

        live = bool(q and q.get("price"))
        px = float(q["price"]) if live else (p.get("price") or 0.0)
        prev = (p.get("prev_close")
                or (float(q["prev_close"]) if (q and q.get("prev_close")) else None)
                or px)

        qty = p.get("qty") or 0.0
        v = qty * px / (fx_now or 1.0)
        leg_px = qty * (px - prev) / (fx_now or 1.0)
        leg_fx = (qty * prev * (1.0 / (fx_now or 1.0) - 1.0 / (fx_prev or 1.0))
                  if fx_prev else 0.0)

        total_v += v
        d_px += leg_px
        d_fx += leg_fx
        if live:
            moved += 1
        st = (states or {}).get(p.get("exchange")) or p.get("session")
        tg = _tag(st, px, prev) if st else None
        if tg and abs(leg_px + leg_fx) >= 0.5:
            tags.add(tg)
        rows.append({
            "isin": p.get("isin"),
            "ticker": p.get("ticker"),
            "price": round(px, 4),
            "prev_close": round(prev, 4),
            "value_eur": round(v, 2),
            "day_eur": round(leg_px + leg_fx, 2),
            "day_price_eur": round(leg_px, 2),
            "day_fx_eur": round(leg_fx, 2),
            "day_pct": round((px / prev - 1) * 100, 2) if prev else 0.0,
            "unreal_eur": round(v - (p.get("cost_eur") or 0.0), 2),
            "live": live,
            "src": (q or {}).get("src"),
            "session": st,
            "day_tag": tg,
            "last_eur": p.get("last_eur"),
            "last_pct": p.get("last_pct"),
            "last_date": p.get("last_date"),
        })

    day = d_px + d_fx
    opening = total_v - day
    return {
        "positions": rows,
        "live_rows": moved,
        "totals": {
            "value_eur": round(total_v, 2),
            "day_eur": round(day, 2),
            "day_price_eur": round(d_px, 2),
            "day_fx_eur": round(d_fx, 2),
            # As a share of what the book was worth at yesterday's close —
            # the only denominator that makes the percentage mean anything.
            "day_pct": round(day / opening * 100, 2) if opening else 0.0,
            "net_worth_eur": round(total_v + (base.get("cash_eur") or 0.0), 2),
            "unreal_eur": round(total_v - (base.get("cost_eur") or 0.0), 2),
            "day_tags": sorted(tags),
            "last_eur": base.get("last_eur"),
            "last_pct": base.get("last_pct"),
            "last_dates": base.get("last_dates") or [],
        },
    }


# Yahoo's stream says which session a tick belongs to as a NUMBER (its
# MarketHoursType: 0 pre-market, 1 regular, 2 post-market, 3 extended), and
# yfinance hands it over as such. Builds 9 to 31 looked for the word "REGULAR"
# in it, so a streaming feed never read as open: the bar said "market shut"
# all day.
REGULAR = 1
# A symbol's state is only as good as its last tick. Amsterdam stops ticking
# at 17:30 with its last tick "regular"; without this it read as open at night.
FRESH_TICK_S = 900


def is_regular(h) -> bool:
    if isinstance(h, bool):
        return False
    if isinstance(h, (int, float)):
        return int(h) == REGULAR
    s = str(h).strip().upper()
    return s == str(REGULAR) or "REGULAR" in s


def market_open(quotes: dict, now: Optional[float] = None) -> Optional[bool]:
    """
    True if any symbol that ticked in the last fifteen minutes says it is in
    regular trading; False when ticks carry a session and none recent is
    regular.

    Returns None when nothing has told us either way, which the page shows as
    "waiting" rather than guessing — a wrong claim about whether the market is
    open is worse than admitting we do not know.
    """
    now = now or time.time()
    seen = [(q.get("market_hours"), q.get("seen") or q.get("ts") or 0.0)
            for q in quotes.values() if q.get("market_hours") is not None]
    if not seen:
        return None
    return any(is_regular(h) and now - t <= FRESH_TICK_S for h, t in seen)
