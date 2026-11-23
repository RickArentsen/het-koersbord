"""Live company fundamentals and option chains.

Two jobs:

* **Fundamentals** - market cap, analyst target, business summary and the usual
  ratios, cached in SQLite with a TTL so a page load never waits on the network
  more than once a day per ticker.
* **Option illustration** - given a ticker and a direction, pick a contract that
  would express that view and report honestly what it costs to be wrong.

On the options feature
----------------------
This is an *illustration*, not a recommendation, and the code is written to keep
it that way:

* Option chains are live-only. Yahoo publishes no historical chains, so a
  contract shown against a 2026 filing is priced **today**, not at the time of
  the trade. The move the filing describes may already have happened.
* The suggested contract is deliberately boring - the nearest listed expiry to
  the strategy's own twelve-month horizon, struck near the money. It is not
  screened for maximum payoff.
* :func:`risk_band` never returns "low". A long option can lose its entire
  premium, and the bands run from High to Extreme because that is the truth
  about the instrument, not a disclaimer bolted on afterwards.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_meta (
    ticker      TEXT PRIMARY KEY,
    fetched_at  TEXT,
    payload     TEXT
);
"""

FUNDAMENTALS_TTL_HOURS = 24
CHAIN_TTL_MINUTES = 20

# Fields kept from Yahoo's info blob. Anything not listed is dropped, so a
# change upstream cannot quietly bloat the cache.
INFO_FIELDS = (
    "longName", "shortName", "sector", "industry", "country", "website",
    "marketCap", "currentPrice", "regularMarketPrice",
    "targetMeanPrice", "targetHighPrice", "targetLowPrice",
    "numberOfAnalystOpinions", "recommendationKey",
    "trailingPE", "forwardPE", "beta", "dividendYield",
    "fiftyTwoWeekLow", "fiftyTwoWeekHigh", "averageVolume",
    "fullTimeEmployees", "longBusinessSummary",
)


def _yf_symbol(ticker: str) -> str:
    return (ticker or "").strip().upper().replace(".", "-")


def fundamentals(conn: sqlite3.Connection, ticker: str,
                 max_age_hours: int = FUNDAMENTALS_TTL_HOURS) -> dict:
    """Company facts for a ticker, cached. Returns {} if unavailable."""
    conn.executescript(SCHEMA)
    sym = _yf_symbol(ticker)
    if not sym:
        return {}

    row = conn.execute(
        "SELECT fetched_at, payload FROM market_meta WHERE ticker = ?", (sym,)).fetchone()
    if row and row["fetched_at"]:
        try:
            age = dt.datetime.now() - dt.datetime.fromisoformat(row["fetched_at"])
            if age < dt.timedelta(hours=max_age_hours):
                return json.loads(row["payload"])
        except (ValueError, TypeError):
            pass

    data: dict = {}
    try:
        import yfinance as yf

        info = yf.Ticker(sym).info or {}
        data = {k: info.get(k) for k in INFO_FIELDS if info.get(k) is not None}
    except Exception:  # noqa: BLE001 - a page must render without the network
        data = json.loads(row["payload"]) if row and row["payload"] else {}

    if data:
        conn.execute(
            """INSERT INTO market_meta (ticker, fetched_at, payload) VALUES (?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 fetched_at = excluded.fetched_at, payload = excluded.payload""",
            (sym, dt.datetime.now().isoformat(timespec="seconds"), json.dumps(data)))
        conn.commit()
    return data


# Yahoo throttles a long unpaced run. Left unpaced, every request after roughly
# the 1,450th failed while the loop kept going - which is worse than stopping,
# because a failed fetch that gets written down looks exactly like a company
# with no market cap.  Nothing is written unless the fetch actually returned.
FETCH_PAUSE_SECONDS = 0.25
CONSECUTIVE_FAILURE_LIMIT = 25

# Marks a ticker Yahoo answered for but had no data on - a delisted shell, a
# bond, a private fund.  Distinct from "we never got an answer", which stores
# nothing at all so the next run retries it.
NO_DATA = {"_no_data": True}

# yfinance does not raise when Yahoo refuses it. A 401 (a rate-limited crumb)
# and a 404 (an unknown symbol) both come back as an info dict with nothing in
# it, so "the fetch raised" never fired: on 4 November 2026 the first ~450
# tickers of a run were written down as "no market cap", and every sector of
# another run as "none". A real answer always carries a quoteType (EQUITY,
# ETF, ...). Without one, a symbol Yahoo surely knows tells "unknown" from
# "refused".
CANARY = "AAPL"
STREAK_CHECK = CONSECUTIVE_FAILURE_LIMIT


def yahoo_info(sym: str) -> dict | None:
    """Yahoo's info for one symbol, or None when no answer came back."""
    try:
        import yfinance as yf

        info = yf.Ticker(sym).info or {}
    except Exception:  # noqa: BLE001 - one bad symbol must not stop the run
        return None
    return info if info.get("quoteType") else None


def sweep(items, symbol, store, pause: float = 0.0, every=None) -> int:
    """Ask Yahoo about each item, one at a time, and hand ``store(item, info)`` each answer.

    ``info`` is None for an item Yahoo had nothing on. Items without an answer
    are held until a real answer comes, or until :data:`STREAK_CHECK` of them
    pile up and the canary is asked. A canary that answers means they are
    unknown symbols, and they are stored; one that does not means Yahoo is
    refusing the session, and the sweep stops with them unrecorded, for the
    next run to ask again. Returns how many items it left unrecorded (0: all).
    """
    import time

    items = list(items)
    held: list = []

    def settle() -> bool:
        if yahoo_info(CANARY) is None:
            return False
        for h in held:
            store(h, None)
        held.clear()
        return True

    for i, item in enumerate(items, 1):
        info = yahoo_info(symbol(item))
        if info is None:
            held.append(item)
            if len(held) >= STREAK_CHECK and not settle():
                return len(items) - i + len(held)
        else:
            for h in held:                # a real answer: the session works, so those were unknown
                store(h, None)
            held.clear()
            store(item, info)
        if every:
            every(i)
        if pause:
            time.sleep(pause)
    if held and not settle():
        return len(held)
    return 0


def sync_fundamentals(conn: sqlite3.Connection, tickers: list[str] | None = None,
                      progress: bool = True, refresh: bool = False,
                      pause: float = FETCH_PAUSE_SECONDS) -> dict:
    """Backfill company facts for every traded ticker.

    :func:`fundamentals` fetches lazily, one ticker per page view, which is fine
    for a detail page but leaves the table at a few dozen rows. Grading needs
    market cap for *every* ticker at once, so this walks the whole set.

    Resumable: a ticker is skipped when a row already exists, and a row exists
    only for tickers Yahoo actually answered for.  A request that raised writes
    nothing, so the next run picks it up again rather than recording a phantom
    "no market cap".  After :data:`CONSECUTIVE_FAILURE_LIMIT` failures in a row
    the run stops and says so, because that is throttling rather than bad
    symbols and continuing would just burn through the list.
    """
    import time

    conn.executescript(SCHEMA)
    if tickers is None:
        tickers = [r[0] for r in conn.execute(
            """SELECT DISTINCT ticker FROM transactions
                WHERE ticker IS NOT NULL AND ticker <> ''""")]

    have: set[str] = set()
    if not refresh:
        have = {r[0] for r in conn.execute("SELECT ticker FROM market_meta")}

    pending = [t for t in tickers if _yf_symbol(t) not in have]
    stats = {"wanted": len(tickers), "pending": len(pending), "resolved": 0,
             "no_data": 0, "failed": 0, "stopped_early": False}

    def store(sym: str, info: dict | None) -> None:
        data = {k: info.get(k) for k in INFO_FIELDS if info.get(k) is not None} if info else {}
        if data.get("marketCap"):
            stats["resolved"] += 1
        else:
            stats["no_data"] += 1
            data = data or dict(NO_DATA)
        conn.execute(
            """INSERT INTO market_meta (ticker, fetched_at, payload) VALUES (?,?,?)
               ON CONFLICT(ticker) DO UPDATE SET
                 fetched_at = excluded.fetched_at, payload = excluded.payload""",
            (sym, dt.datetime.now().isoformat(timespec="seconds"), json.dumps(data)))

    def every(i: int) -> None:
        if i % 50 == 0:
            conn.commit()
            if progress:
                print(f"  fundamentals {i}/{len(pending)} "
                      f"({stats['resolved']} with a market cap)", flush=True)

    syms = list(dict.fromkeys(s for s in map(_yf_symbol, pending) if s))
    left = sweep(syms, lambda s: s, store, pause=pause, every=every)
    conn.commit()
    if left:
        stats.update(failed=left, stopped_early=True)
        if progress:
            print(f"  stopping: Yahoo is refusing requests (the canary {CANARY} got no "
                  f"answer either); {left} tickers left for the next run.", flush=True)
    return stats


def market_caps(conn: sqlite3.Connection) -> dict[str, float]:
    """Ticker -> market cap, for every ticker that has one cached."""
    conn.executescript(SCHEMA)
    out: dict[str, float] = {}
    for ticker, payload in conn.execute("SELECT ticker, payload FROM market_meta"):
        try:
            cap = (json.loads(payload) or {}).get("marketCap")
        except (ValueError, TypeError):
            continue
        if cap:
            out[ticker] = float(cap)
    return out


NEWS_TTL_MINUTES = 45
_news_cache: dict[str, tuple[dt.datetime, list]] = {}


# Words that mark a general market wrap rather than company news. Yahoo's
# per-ticker feed is full of these, and they are the same story under every
# symbol, which makes a news page look busy while saying nothing.
MARKET_WRAP_MARKERS = (
    "stock market today", "equities fall", "equities rise", "stocks to watch",
    "market wrap", "midday movers", "premarket", "pre-market",
    "s&p 500 companies", "dow jones futures", "market close", "closing bell",
    "opening bell", "what to watch", "movers &", "top stocks",
)


def _company_keywords(name: str | None) -> list[str]:
    """Distinctive words from a company name, for relevance matching."""
    if not name:
        return []
    stop = {"inc", "inc.", "corp", "corp.", "corporation", "company", "co",
            "co.", "ltd", "ltd.", "plc", "holdings", "group", "the", "and",
            "technologies", "technology", "international", "&", "class",
            "common", "stock", "incorporated", "limited", "sa", "nv", "ag"}
    words = [w.strip(",.()").lower() for w in name.split()]
    return [w for w in words if len(w) > 2 and w not in stop]


def _is_relevant(article: dict, sym: str, keywords: list[str]) -> bool:
    """Is this article actually about the company, or just market noise?"""
    blob = f"{article['title']} {article['summary']}".lower()
    if any(m in blob for m in MARKET_WRAP_MARKERS):
        # A wrap still counts if it names the company explicitly.
        return bool(keywords and any(k in blob for k in keywords))
    if sym.lower() in blob or f"({sym.lower()})" in blob:
        return True
    return bool(keywords and any(k in blob for k in keywords))


def news(ticker: str, limit: int = 8, company_name: str | None = None,
         relevant_only: bool = True) -> list[dict]:
    """Recent articles about a ticker, normalised, filtered and cached.

    Yahoo nests the useful fields under ``content`` and wraps the URL and
    provider in their own objects, so this flattens them.  It also drops
    general market wraps that do not name the company: they appear under every
    symbol and would otherwise fill the page with the same three stories.
    """
    sym = _yf_symbol(ticker)
    if not sym:
        return []

    keywords = _company_keywords(company_name)

    def finish(items: list[dict]) -> list[dict]:
        if relevant_only:
            kept = [a for a in items if _is_relevant(a, sym, keywords)]
            # If filtering leaves nothing, show the unfiltered feed rather than
            # an empty panel - some small caps genuinely have no direct news.
            items = kept or items
        return items[:limit]

    now = dt.datetime.now()
    hit = _news_cache.get(sym)
    if hit and now - hit[0] < dt.timedelta(minutes=NEWS_TTL_MINUTES):
        return finish(hit[1])

    out: list[dict] = []
    try:
        import yfinance as yf

        for item in (yf.Ticker(sym).news or []):
            c = item.get("content") or item
            url = c.get("canonicalUrl") or c.get("clickThroughUrl") or {}
            provider = c.get("provider") or {}
            title = (c.get("title") or "").strip()
            link = url.get("url") if isinstance(url, dict) else url
            if not title or not link:
                continue
            out.append({
                "ticker": sym,
                "title": title,
                "summary": (c.get("summary") or c.get("description") or "").strip(),
                "url": link,
                "provider": (provider.get("displayName")
                             if isinstance(provider, dict) else provider) or "",
                "published": (c.get("pubDate") or c.get("displayTime") or "")[:10],
            })
    except Exception:  # noqa: BLE001 - a news outage must not break the page
        out = hit[1] if hit else []

    _news_cache[sym] = (now, out)
    return finish(out)


def upside_to_target(f: dict) -> float | None:
    """Analyst mean target versus the current price, as a fraction."""
    price = f.get("currentPrice") or f.get("regularMarketPrice")
    target = f.get("targetMeanPrice")
    if not price or not target:
        return None
    return target / price - 1


# --------------------------------------------------------------------------
# Options
# --------------------------------------------------------------------------

_chain_cache: dict[str, tuple[dt.datetime, list]] = {}


def _expiries(sym: str) -> list[str]:
    now = dt.datetime.now()
    hit = _chain_cache.get(f"exp:{sym}")
    if hit and now - hit[0] < dt.timedelta(minutes=CHAIN_TTL_MINUTES):
        return hit[1]
    import yfinance as yf

    exps = list(yf.Ticker(sym).options or [])
    _chain_cache[f"exp:{sym}"] = (now, exps)
    return exps


def suggest_contract(ticker: str, direction: str = "call",
                     target_days: int = 365) -> dict | None:
    """A contract expressing ``direction`` on ``ticker`` over roughly a year.

    Picks the listed expiry nearest ``target_days`` out and the strike nearest
    the money, then reports what it costs and what it takes to break even.
    Returns ``None`` when the ticker has no options or the network is down.
    """
    sym = _yf_symbol(ticker)
    if not sym:
        return None

    try:
        import yfinance as yf

        exps = _expiries(sym)
        if not exps:
            return None

        today = dt.date.today()
        target = today + dt.timedelta(days=target_days)
        expiry = min(exps, key=lambda e: abs(
            (dt.date.fromisoformat(e) - target).days))
        days_out = (dt.date.fromisoformat(expiry) - today).days

        tk = yf.Ticker(sym)
        now = dt.datetime.now()
        key = f"{sym}:{expiry}"
        hit = _chain_cache.get(key)
        if hit and now - hit[0] < dt.timedelta(minutes=CHAIN_TTL_MINUTES):
            chain = hit[1]
        else:
            ch = tk.option_chain(expiry)
            chain = [ch.calls, ch.puts]
            _chain_cache[key] = (now, chain)
        table = chain[0] if direction == "call" else chain[1]
        if table is None or table.empty:
            return None

        spot = None
        try:
            hist = tk.history(period="1d")
            if not hist.empty:
                spot = float(hist["Close"].iloc[-1])
        except Exception:  # noqa: BLE001
            pass
        if not spot:
            itm = table[table["inTheMoney"]]
            spot = float(itm["strike"].max()) if not itm.empty else float(
                table["strike"].median())

        # Nearest-the-money strike: the least opinionated choice available.
        table = table.copy()
        table["distance"] = (table["strike"] - spot).abs()
        row = table.sort_values("distance").iloc[0]

        bid, ask = float(row.get("bid") or 0), float(row.get("ask") or 0)
        mid = (bid + ask) / 2 if bid and ask else float(row.get("lastPrice") or 0)
        if mid <= 0:
            return None
        strike = float(row["strike"])
        breakeven = strike + mid if direction == "call" else strike - mid
        move_needed = (breakeven / spot - 1) if spot else None

        return {
            "symbol": str(row.get("contractSymbol") or ""),
            "direction": direction,
            "expiry": expiry,
            "days_to_expiry": days_out,
            "strike": strike,
            "spot": spot,
            "bid": bid, "ask": ask, "mid": mid,
            "cost_per_contract": mid * 100,
            "iv": float(row.get("impliedVolatility") or 0),
            "open_interest": int(row.get("openInterest") or 0),
            "volume": int(row.get("volume") or 0),
            "in_the_money": bool(row.get("inTheMoney")),
            "breakeven": breakeven,
            "move_needed": move_needed,
            "spread_pct": ((ask - bid) / mid) if (mid and ask and bid) else None,
            "as_of": dt.datetime.now().isoformat(timespec="minutes"),
        }
    except Exception:  # noqa: BLE001 - never break a page over a data feed
        return None


# Bands deliberately start at "High". A long option can expire worthless, so
# there is no honest "low risk" rung on this ladder.
RISK_BANDS = [
    (0, "High", "You can lose the entire premium. Everything below makes that more likely."),
    (35, "Very high", "Needs a real move in your favour, and time is working against you."),
    (60, "Severe", "Wide spreads, thin interest or a big required move. Easy to lose it all."),
    (80, "Extreme", "Closer to a lottery ticket than a position."),
]


def risk_band(c: dict) -> dict:
    """Score how punishing a long option is, on observable facts only."""
    if not c:
        return {}
    score, notes = 0.0, []

    move = abs(c.get("move_needed") or 0)
    if move > 0.30:
        score += 30; notes.append(f"needs a {move * 100:.0f}% move just to break even")
    elif move > 0.15:
        score += 20; notes.append(f"needs a {move * 100:.0f}% move to break even")
    elif move > 0.05:
        score += 10; notes.append(f"needs a {move * 100:.0f}% move to break even")

    days = c.get("days_to_expiry") or 0
    if days < 45:
        score += 25; notes.append("expires soon, so time decay bites hard")
    elif days < 120:
        score += 15; notes.append("only a few months of time value")

    iv = c.get("iv") or 0
    if iv > 0.80:
        score += 20; notes.append(f"implied volatility {iv * 100:.0f}% - the premium is expensive")
    elif iv > 0.45:
        score += 10; notes.append(f"implied volatility {iv * 100:.0f}%")

    oi = c.get("open_interest") or 0
    if oi < 50:
        score += 20; notes.append(f"only {oi} contracts of open interest - you may not get out")
    elif oi < 500:
        score += 8; notes.append(f"{oi:,} contracts of open interest")

    spread = c.get("spread_pct")
    if spread is not None and spread > 0.25:
        score += 15; notes.append(f"bid/ask spread is {spread * 100:.0f}% of the price")
    elif spread is not None and spread > 0.10:
        score += 6; notes.append(f"bid/ask spread {spread * 100:.0f}%")

    label, blurb = RISK_BANDS[0][1], RISK_BANDS[0][2]
    for cutoff, name, text in RISK_BANDS:
        if score >= cutoff:
            label, blurb = name, text
    return {"score": round(min(score, 100), 0), "label": label,
            "blurb": blurb, "notes": notes}
