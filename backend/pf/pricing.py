"""
Quotes and FX, behind one narrow interface.

Everything the rest of the app needs is:
    get_quotes(tickers)  -> {ticker: {price, prev_close, currency, asof}}
    get_history(ticker)  -> {YYYY-MM-DD: close}
    get_fx(pair)         -> {YYYY-MM-DD: rate}

The current implementation is yfinance. It is an undocumented scrape and it
WILL break; that is exactly why it lives behind this interface and why every
fetched value is written to the local cache and re-read from there. Swapping
in a paid feed later means rewriting this file and nothing else.

FX convention matches DEGIRO's "Wisselkoers": units of the foreign currency
per 1 EUR, i.e. EURUSD ~ 1.16. Divide a USD amount by the rate to get EUR.
"""

from __future__ import annotations

import time
from datetime import datetime, date, timedelta
from typing import Optional

from . import store

# --------------------------------------------------------------------------
# ISIN -> quote symbol seed map.
# A hint, not gospel: the UI shows which symbol priced each row so a wrong
# one is visible, and any of these can be overridden per instrument.
# --------------------------------------------------------------------------

SEED_TICKERS = {
    # --- US listings -------------------------------------------------------
    "US5949181045": "MSFT",  "US0231351067": "AMZN",  "US67066G1040": "NVDA",
    "US02079K3059": "GOOGL", "US0378331005": "AAPL",  "US5801351017": "MCD",
    "US8740391003": "TSM",   "US0079031078": "AMD",   "US7512121010": "RL",
    "US30303M1027": "META",  "US0937121079": "BE",    "US00724F1012": "ADBE",
    "US57636Q1040": "MA",    "US68389X1054": "ORCL",  "US9581021055": "WDC",
    "US81762P1021": "NOW",   "US92840M1027": "VST",   "US5533681012": "MP",
    "US11135F1012": "AVGO",  "US9311421039": "WMT",   "US46120E6023": "ISRG",
    "US7475251036": "QCOM",  "US4435731009": "HUBS",  "US8808901081": "TX",
    "US03823U1025": "AAOI",  "US4581401001": "INTC",  "US03831W1080": "APP",
    "US88160R1014": "TSLA",  "US4592001014": "IBM",   "US21873S1087": "CRWV",
    "US64110L1061": "NFLX",  "US86800U3023": "SMCI",  "US20825C1045": "COP",
    "US91324P1021": "UNH",   "US92343V1044": "VZ",    "US0420682058": "ARM",
    "US92347M1009": "VERI",  "KYG254571055": "CRDO",  "US5951121038": "MU",
    "US1667641005": "CVX",   "US6745991058": "OXY",   "US14149Y1082": "CAH",
    "US70450Y1038": "PYPL",  "US79466L3024": "CRM",   "US6701002056": "NVO",
    "US6541061031": "NKE",   "US49456B1017": "KMI",   "US1696561059": "CMG",
    "US8923561067": "TSCO",  "US4330001060": "HIMS",  "US4383331067": "HNST",
    "KYG3730V1059": "FTAI",  "US7043261079": "PAYX",  "US76118Y1047": "REZI",
    "US3119001044": "FAST",  "US78409V1044": "SPGI",  "US15135B1017": "CNC",
    "CA2449161025": "DEFT",
    # --- European listings -------------------------------------------------
    "NL0011821202": "INGA.AS",  "NL0010773842": "NN.AS",
    "NL0000289213": "WHA.AS",   "NL0011794037": "AD.AS",
    "NL0010391025": "PHARM.AS", "GB00BP6MXD84": "SHELL.AS",
    "GB00BVZK7T90": "UNA.AS",   "DE0005190003": "BMW.DE",
    "IE0032077012": "EQQQ.DE",  "IE00B579F325": "8PSG.DE",
}

# Sector and country, keyed by quote symbol. A local table because the
# alternative is ~70 separate yfinance .info calls that are slow, rate-limited
# and fail silently — which is why every sector read "Unclassified". The
# provider still fills anything missing here, and your own override wins.
SEED_SECTORS = {
    # Technology
    **{t: ("Technology", "United States") for t in (
        "MSFT","NVDA","AAPL","AMD","ADBE","ORCL","WDC","NOW","AVGO","QCOM","HUBS",
        "AAOI","INTC","APP","IBM","CRWV","SMCI","VERI","CRDO","MU","CRM","REZI",
        "SNDK","FIG","INTU","SNOW","TTD","NTAP","DELL")},
    "TSM": ("Technology", "Taiwan"), "ARM": ("Technology", "United Kingdom"),
    # Communication services
    **{t: ("Communication Services", "United States") for t in ("GOOGL","GOOG","META","NFLX","VZ")},
    # Consumer cyclical
    **{t: ("Consumer Cyclical", "United States") for t in ("AMZN","MCD","RL","TSLA","NKE","CMG","TSCO")},
    "BMW.DE": ("Consumer Cyclical", "Germany"),
    # Consumer defensive
    **{t: ("Consumer Defensive", "United States") for t in ("WMT","HNST","GIS")},
    "AD.AS": ("Consumer Defensive", "Netherlands"),
    "UNA.AS": ("Consumer Defensive", "United Kingdom"),
    # Healthcare
    **{t: ("Healthcare", "United States") for t in ("ISRG","UNH","CAH","HIMS","CNC","ABT","ABBV")},
    "NVO": ("Healthcare", "Denmark"), "PHARM.AS": ("Healthcare", "Netherlands"),
    # Financial services
    **{t: ("Financial Services", "United States") for t in ("MA","PYPL","SPGI","COIN","DEFT","V")},
    "INGA.AS": ("Financial Services", "Netherlands"),
    "NN.AS": ("Financial Services", "Netherlands"),
    # Energy
    **{t: ("Energy", "United States") for t in ("COP","CVX","OXY","KMI","VLO")},
    "SHELL.AS": ("Energy", "United Kingdom"),
    # Industrials
    **{t: ("Industrials", "United States") for t in ("BE","FTAI","PAYX","FAST","FDX","UPS")},
    # Utilities / materials / real estate
    "VST": ("Utilities", "United States"), "EXC": ("Utilities", "United States"),
    "MP": ("Basic Materials", "United States"), "TX": ("Basic Materials", "Luxembourg"),
    "WHA.AS": ("Real Estate", "Netherlands"),
    # Funds
    "EQQQ.DE": ("Index fund", "Ireland"), "SXR8.DE": ("Index fund", "Ireland"),
    "SXRV.DE": ("Index fund", "Ireland"), "8PSG.DE": ("Commodity", "Ireland"),
}


def seed_sector(ticker: str):
    """(sector, country) from the local table, or (None, None)."""
    return SEED_SECTORS.get(ticker or "", (None, None))


# EUR-quoted accumulating UCITS trackers: using these as benchmarks gives
# total return (dividends reinvested) already converted to EUR, instead of
# hand-building a total-return index from a price index.
BENCHMARKS = {
    "SP500": {"ticker": "SXR8.DE", "label": "S&P 500 (iShares Core, EUR acc)"},
    "NDX":   {"ticker": "SXRV.DE", "label": "Nasdaq 100 (iShares, EUR acc)"},
    "WORLD": {"ticker": "EUNL.DE", "label": "MSCI World (iShares Core, EUR acc)"},
}

# Which benchmarks the risk panel regresses against. Two, deliberately: a
# portfolio holding European, US and Canadian names is not a US portfolio, and
# the only way to find out which index actually explains it is to run both and
# compare the R2. Whichever wins is the one whose beta means anything.
RISK_BENCHMARKS = ("SP500", "WORLD")

# The market board. Kept deliberately short: things that actually move a
# EUR-based book holding mostly US equities, not an inventory of every index.
MARKET_BOARD = [
    {"g": "Indices",     "t": "^AEX",       "n": "AEX",            "u": "idx"},
    {"g": "Indices",     "t": "^GSPC",      "n": "S&P 500",        "u": "idx"},
    {"g": "Indices",     "t": "^NDX",       "n": "Nasdaq 100",     "u": "idx"},
    {"g": "Indices",     "t": "^STOXX50E",  "n": "Euro Stoxx 50",  "u": "idx"},
    {"g": "Indices",     "t": "^GDAXI",     "n": "DAX",            "u": "idx"},
    {"g": "FX & rates",  "t": "EURUSD=X",   "n": "EUR/USD",        "u": "fx"},
    {"g": "FX & rates",  "t": "EURGBP=X",   "n": "EUR/GBP",        "u": "fx"},
    {"g": "FX & rates",  "t": "^TNX",       "n": "US 10y",         "u": "pct"},
    {"g": "Commodities", "t": "BZ=F",       "n": "Brent",          "u": "usd"},
    {"g": "Commodities", "t": "GC=F",       "n": "Gold",           "u": "usd"},
    {"g": "Risk",        "t": "^VIX",       "n": "VIX",            "u": "idx"},
]


def market_board() -> list:
    """Live board rows. Never raises — an unreachable feed yields stale rows."""
    q = get_quotes([r["t"] for r in MARKET_BOARD])
    out = []
    for r in MARKET_BOARD:
        d = q.get(r["t"])
        row = {**r, "price": None, "chg_pct": None, "asof": None, "stale": True}
        if d and d.get("price"):
            prev = d.get("prev_close") or d["price"]
            row.update({
                "price": round(d["price"], 4),
                "chg_pct": round((d["price"] / prev - 1) * 100, 2) if prev else 0.0,
                "asof": d.get("asof"),
                "stale": str(d.get("source", "")).startswith("cache"),
            })
        out.append(row)
    return out


_MEM: dict = {}
_MEM_TTL = 120  # seconds

# Every outbound call is bounded. The feed being slow must cost one request,
# not a thread that never comes back.
FEED_TIMEOUT = 20

# Circuit breaker: if the feed is unreachable, stop hammering it for the rest
# of this process and serve the local cache instead. Without this a dropped
# connection turns into one timeout per ticker and the dashboard hangs.
_FAILS = {"n": 0, "until": 0.0}
_FAIL_LIMIT = 1
_COOLDOWN = 180.0


def feed_down() -> bool:
    return time.time() < _FAILS["until"]


def _note_fail():
    _FAILS["n"] += 1
    if _FAILS["n"] >= _FAIL_LIMIT:
        _FAILS["until"] = time.time() + _COOLDOWN


def _note_ok():
    _FAILS["n"] = 0
    _FAILS["until"] = 0.0


def _yf():
    import yfinance as yf
    return yf


_SHARE_SEARCHED: dict[str, Optional[str]] = {}


def search_share(name: str) -> Optional[str]:
    """
    A share's Yahoo symbol from its name, for a turbo whose underlying the
    name list does not know. A US listing (no suffix) is preferred: SG's
    turbos on these names are struck in dollars. Asked once per name per run.
    A wrong match is caught by the model's own check against the trades.
    """
    key = (name or "").strip().upper()
    if not key:
        return None
    if key in _SHARE_SEARCHED:
        return _SHARE_SEARCHED[key]
    found = None
    try:
        import requests
        r = requests.get("https://query2.finance.yahoo.com/v1/finance/search",
                         params={"q": key, "quotesCount": 8, "newsCount": 0},
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        eq = [q for q in r.json().get("quotes", []) if q.get("quoteType") == "EQUITY" and q.get("symbol")]
        us = [q for q in eq if "." not in q["symbol"]]
        found = (us or eq)[0]["symbol"] if (us or eq) else None
    except Exception:
        found = None
    _SHARE_SEARCHED[key] = found
    return found


# The currency a Yahoo symbol is quoted in, by its suffix; no suffix is a US
# listing. Pence for ".L" are handled where the quote is read.
SUFFIX_CCY = {"": "USD", "AS": "EUR", "PA": "EUR", "BR": "EUR", "LS": "EUR", "IR": "EUR", "DE": "EUR", "F": "EUR",
              "MI": "EUR", "MC": "EUR", "HE": "EUR", "VI": "EUR", "L": "GBP", "SW": "CHF", "TO": "CAD", "V": "CAD",
              "HK": "HKD", "T": "JPY", "ST": "SEK", "CO": "DKK", "OL": "NOK", "WA": "PLN", "AX": "AUD"}
# DEGIRO's exchange codes, and the suffix of the same listing on Yahoo.
DEGIRO_SUFFIX = {"NSY": "", "NDQ": "", "ASE": "", "EAM": "AS", "EPA": "PA", "EBR": "BR", "ELI": "LS", "XET": "DE",
                 "TDG": "DE", "FRA": "F", "MIL": "MI", "MAD": "MC", "HSE": "HE", "WSE": "WA", "LSE": "L", "SWX": "SW",
                 "TSE": "TO", "HKS": "HK", "TYO": "T", "OMX": "ST", "CSE": "CO", "OSL": "OL", "ASX": "AX", "VIE": "VI"}


def pick_listing(quotes: list[dict], currency: Optional[str] = None, exchange: Optional[str] = None) -> Optional[str]:
    """
    Of Yahoo's search results for an ISIN, the listing to price a holding by:
    one quoted in the holding's own currency, on its own exchange when Yahoo
    has it. A listing in another currency is never taken, because its price
    would be read as the holding's: Yahoo's first answer for a Dutch ISIN
    can be the American line in dollars.
    """
    cands = []
    for q in quotes:
        sym = q.get("symbol")
        if not sym or q.get("quoteType") not in ("EQUITY", "ETF", None):
            continue
        suf = sym.rsplit(".", 1)[1].upper() if "." in sym and not sym.startswith("^") else ""
        if suf and suf not in SUFFIX_CCY and len(suf) == 1:
            suf = ""                                    # a share class (BRK.B), not an exchange
        cands.append((sym, suf, SUFFIX_CCY.get(suf)))
    if not currency:
        return cands[0][0] if cands else None
    same = [c for c in cands if c[2] == currency.upper()]
    want = DEGIRO_SUFFIX.get((exchange or "").upper())
    home = [c for c in same if want is not None and c[1] == want]
    return (home or same)[0][0] if (home or same) else None


# Words that say nothing about which company a name is.
_NAME_NOISE = {"THE", "ADR", "ON", "INC", "CORP", "CORPORATION", "CO", "COMPANY", "GROUP", "HOLDING", "HOLDINGS",
               "LIMITED", "LTD", "PLC", "AG", "SA", "NV", "SE", "CLASS", "SHARES", "ORDINARY"}


def name_key(name: str) -> Optional[str]:
    """The word of a company's name that identifies it: CELESTICA in 'CELESTICA INC.'."""
    import re as _re
    words = [w for w in _re.findall(r"[A-Z0-9]+", (name or "").upper()) if len(w) >= 3 and w not in _NAME_NOISE]
    return words[0] if words else None


def _yahoo_search(q: str) -> list[dict]:
    import requests
    r = requests.get("https://query2.finance.yahoo.com/v1/finance/search",
                     params={"q": q, "quotesCount": 8, "newsCount": 0},
                     headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
    return r.json().get("quotes", []) or []


def resolve_ticker(isin: str, name: str = "", currency: Optional[str] = None,
                   exchange: Optional[str] = None) -> tuple[Optional[str], str]:
    """
    (ticker, source). Seed map first, then Yahoo's ISIN search, in the
    holding's currency. Yahoo's ISIN search can list only one line of a
    share quoted in several places: for Celestica's Canadian ISIN, CLS.TO in
    Canadian dollars and never the NYSE line held in dollars. Then the
    company's name finds the others, kept to results bearing that name.
    """
    if isin in SEED_TICKERS:
        return SEED_TICKERS[isin], "seed"
    try:
        sym = pick_listing(_yahoo_search(isin), currency, exchange)
        if sym:
            return sym, "yahoo"
        key = name_key(name)
        if key and currency:
            same = [q for q in _yahoo_search(name)
                    if key in f"{q.get('shortname') or ''} {q.get('longname') or ''}".upper()]
            sym = pick_listing(same, currency, exchange)
            if sym:
                return sym, "yahoo (by name)"
    except Exception:
        pass
    return None, "unresolved"


def _quote(t: str, bars: list[tuple[str, float]], source: str, now=None) -> Optional[dict]:
    """
    A quote from daily bars: the newest close is the price, and the reference
    for the day change is the close of the newest FINISHED session, which
    sessions.pick reads off the exchange's clock. `prev_close` keeps its name
    and now means exactly that reference, so every reader of it is right.
    """
    from . import sessions
    ex = sessions.exchange_of(t)
    pk = sessions.pick(bars, ex, now)
    if not pk:
        return None
    last = None
    if pk["prev"] is not None:
        last = {"d": pk["ref_d"], "close": pk["ref"], "prev_d": pk["prev_d"], "prev": pk["prev"],
                "pct": round((pk["ref"] / pk["prev"] - 1) * 100, 2) if pk["prev"] else None}
    return {"price": pk["last"], "prev_close": pk["ref"], "asof": pk["last_d"], "ref_date": pk["ref_d"],
            "last_session": last, "exchange": ex, "session": sessions.state(ex, now) if ex else None,
            "source": source}


def _from_cache(t: str, source: str, now=None) -> Optional[dict]:
    """
    A quote rebuilt from stored closes, for when the feed cannot answer.

    The previous close is the close on the newest stored date BEFORE the one the
    price comes from — never the price itself. The first version returned
    `prev_close = price` here, which reads as "no change today" and looks
    harmless. It was not: the live feed trusts any previous close it is handed,
    so one Yahoo hiccup (and the circuit breaker trips on a single failure)
    wrote a same-day price into the stream as "yesterday's close", and every
    tick for the rest of the session was measured against it. The day change
    collapsed to a few cents while Microsoft, Alphabet and Amazon were up one to
    two percent.

    When there is no earlier stored day, the previous close is None — unknown —
    so that a caller can keep a good value it already has instead of replacing
    it with a guess.
    """
    cached = store.cached_prices(t)
    if not cached:
        return None
    days = sorted(cached)
    if len(days) < 2:
        return {"price": cached[days[-1]], "prev_close": None, "asof": days[-1], "source": source}
    return _quote(t, [(d, cached[d]) for d in days[-10:]], source, now)


def get_quotes(tickers: list[str], fresh: bool = False, now=None) -> dict:
    """
    The newest close and the close the day is measured from, batched. Cached
    in memory for 2 minutes.

    `fresh` skips that cache. Only the live poller passes it: polling every 30
    seconds through a 120-second memo would return the same numbers four times
    and call it live. `now` is for the checks: which close is "the last one"
    depends on the time.
    """
    tickers = sorted({t for t in tickers if t})
    if not tickers:
        return {}
    key = ("q", tuple(tickers))
    hit = _MEM.get(key)
    if hit and not fresh and now is None and time.time() - hit[0] < _MEM_TTL:
        return hit[1]

    out: dict = {}
    if feed_down():
        for t in tickers:
            q = _from_cache(t, "cache (feed offline)", now)
            if q:
                out[t] = q
        return out
    try:
        yf = _yf()
        # A bounded call. Without a timeout a single unresponsive Yahoo
        # request holds this thread indefinitely, and with the poller asking
        # every 30 seconds those pile up until the server has no threads left
        # to answer anything.
        data = yf.download(tickers, period="7d", interval="1d",
                           auto_adjust=False, progress=False,
                           group_by="ticker", threads=True, timeout=FEED_TIMEOUT)
        for t in tickers:
            try:
                col = data[t]["Close"] if len(tickers) > 1 else data["Close"]
                col = col.dropna()
                if len(col) == 0:
                    continue
                bars = {str(i.date()): float(v) for i, v in col.items()}
                # Yahoo's daily history for European listings can drop the
                # previous session once the next one opens: on Tuesday 29
                # September 2026 ING ran Friday, Tuesday. The day change was
                # then measured against Friday and carried Monday's move too
                # (the gold ETC read -2.9% on a day it rose 0.7%). A close
                # stored for a session in between fills the hole.
                lo, hi = min(bars), max(bars)
                for d, v in store.cached_prices(t).items():
                    if lo < d < hi and d not in bars:
                        bars[d] = v
                q = _quote(t, sorted(bars.items()), "yfinance", now)
                if not q:
                    continue
                out[t] = q
                store.put_prices(t, [(str(i.date()), float(v)) for i, v in col.items()],
                                 None, "yfinance")
            except Exception:
                continue
        if out:
            _note_ok()
        else:
            _note_fail()
    except Exception:
        _note_fail()

    # anything the batch missed: fall back to the newest cached close
    for t in tickers:
        if t in out:
            continue
        q = _from_cache(t, "cache", now)
        if q:
            out[t] = q
    if now is None:
        _MEM[key] = (time.time(), out)
    return out


_HIST_AT: dict[str, float] = {}   # when each symbol's history was last asked of Yahoo, in this process
HIST_MIN_GAP = 600                 # never more than once in ten minutes per symbol
HIST_LIVE_EVERY = 900              # while its exchange trades, the day's bar every quarter of an hour
HIST_OTHER_EVERY = 3600            # FX, indices and futures: once an hour


def _last_session(ex: str, now=None) -> Optional[str]:
    """The newest local date with a regular session that has finished."""
    from . import sessions
    d = date.fromisoformat(sessions.done_through(ex, now))
    for _ in range(10):
        if sessions.trading_day(ex, d):
            return d.isoformat()
        d -= timedelta(days=1)
    return None


def _last_close(exchanges, now=None) -> float:
    """The latest regular close, among these exchanges, at or before now (unix time; 0 when none)."""
    from . import sessions
    best = 0.0
    for ex in exchanges:
        ld = _last_session(ex, now)
        if ld:
            s = sessions.session(ex, date.fromisoformat(ld))
            if s:
                best = max(best, s[1].timestamp())
    return best


def history_due(t: str, cached: dict, start: str, now=None) -> bool:
    """
    Whether a stored daily history needs Yahoo again.

    The old rule was "fresh while the newest stored day is under three days
    old" (four for the batch). That let a benchmark's last sessions go missing,
    and a bar stored in the middle of a session stayed that day's close for
    good. On 15 October 2026 the S&P 500 tracker on PORT still stood at a price
    from the middle of 13 October, and its 2026 return read +19.71% against
    the fund's +17.8%.

    Now, by the exchange's own clock (pf/sessions.py), a history is due:

      * when the newest finished session is not stored;
      * when the last fetch came before that session's close, so its bar may
        be a price from during it;
      * every quarter of an hour while the exchange trades.

    Never more than once in ten minutes, so a session Yahoo does not list (it
    skips some for European lines: SXR8 has no bar for 14 October 2026) costs
    one request per ten minutes, not one per page.
    """
    if not cached or min(cached) > start:
        return True
    tnow = time.time() if now is None else now.timestamp()
    last = _HIST_AT.get(t, 0.0)
    if tnow - last < HIST_MIN_GAP:
        return False
    from . import sessions
    ex = sessions.exchange_of(t)
    if ex is None and t.endswith("=X"):
        # A currency trades all week. The day's bar is the rate now, so it is
        # asked again every quarter of an hour, and after every close of New
        # York or Amsterdam: the day change starts again there, its currency
        # leg too, and it must start from the rate at that close, not one from
        # three days before (15 October 2026: "currency -EUR 40" an hour after
        # New York had closed, all of it stale rate).
        return tnow - last > HIST_LIVE_EVERY or last < _last_close(("US", "EURONEXT"), now)
    if ex is None:
        return tnow - last > HIST_OTHER_EVERY
    ld = _last_session(ex, now)
    if ld is None:
        return False
    if max(cached) < ld:
        return True
    if last < sessions.session(ex, date.fromisoformat(ld))[1].timestamp():
        return True
    return sessions.state(ex, now) == "open" and tnow - last > HIST_LIVE_EVERY


def get_history_many(tickers: list[str], start: str) -> dict[str, dict]:
    """
    One network round trip for every symbol instead of one per symbol.
    Anything stored and fresh by its exchange's clock (history_due) is served
    without touching the network.
    """
    tickers = sorted({t for t in tickers if t})
    out: dict[str, dict] = {}
    need: list[str] = []

    for t in tickers:
        cached = store.cached_prices(t)
        out[t] = cached
        if history_due(t, cached, start):
            need.append(t)

    if not need or feed_down():
        return {t: {d: v for d, v in m.items() if d >= start} for t, m in out.items()}

    for t in need:
        _HIST_AT[t] = time.time()
    try:
        yf = _yf()
        data = yf.download(need, start=start, interval="1d", auto_adjust=False,
                           progress=False, group_by="ticker", threads=True)
        got = 0
        for t in need:
            try:
                col = (data[t]["Close"] if len(need) > 1 else data["Close"]).dropna()
                if len(col) == 0:
                    continue
                rows = [(str(i.date()), float(v)) for i, v in col.items()]
                store.put_prices(t, rows, None, "yfinance")
                out[t].update(dict(rows))
                got += 1
            except Exception:
                continue
        _note_ok() if got else _note_fail()
    except Exception:
        _note_fail()

    return {t: {d: v for d, v in m.items() if d >= start} for t, m in out.items()}


def get_history(ticker: str, start: str) -> dict:
    """Daily closes from `start`. Cache-first; only fetches what is missing."""
    cached = store.cached_prices(ticker)
    if feed_down():
        return {d: v for d, v in cached.items() if d >= start}
    if not history_due(ticker, cached, start):
        return {d: v for d, v in cached.items() if d >= start}
    # From the newest stored day itself, so a bar stored during its session
    # is replaced by the close.
    need_from = max(cached) if cached and min(cached) <= start else start
    _HIST_AT[ticker] = time.time()
    try:
        yf = _yf()
        h = yf.Ticker(ticker).history(start=need_from, interval="1d", auto_adjust=False)
        rows = [(str(i.date()), float(v)) for i, v in h["Close"].dropna().items()]
        if rows:
            store.put_prices(ticker, rows, None, "yfinance")
            cached.update(dict(rows))
            _note_ok()
    except Exception:
        _note_fail()
    return {d: v for d, v in cached.items() if d >= start}


def get_fx(pair: str, start: str) -> dict:
    """pair like 'EURUSD' -> {date: units of quote ccy per 1 EUR}."""
    if pair in ("EUREUR", "EUR"):
        return {}
    cached = store.cached_fx(pair)
    if feed_down():
        return cached
    # The same clock as the price histories (history_due): until build 35 a
    # rate could be three days old and still count as today's.
    if not history_due(f"{pair}=X", cached, start):
        return cached
    _HIST_AT[f"{pair}=X"] = time.time()
    since = max(cached) if cached and min(cached) <= start else start
    try:
        yf = _yf()
        h = yf.Ticker(f"{pair}=X").history(start=since, interval="1d", auto_adjust=False)
        rows = [(str(i.date()), float(v)) for i, v in h["Close"].dropna().items()]
        if rows:
            store.put_fx(pair, rows, "yfinance")
            cached.update(dict(rows))
            _note_ok()
    except Exception:
        _note_fail()
    return cached


def fx_on(fxmap: dict, d: str, fallback: float = 1.0) -> float:
    """Rate on date d, or the most recent rate before it."""
    if not fxmap:
        return fallback
    if d in fxmap:
        return fxmap[d]
    earlier = [k for k in fxmap if k <= d]
    if earlier:
        return fxmap[max(earlier)]
    return fxmap[min(fxmap)]
