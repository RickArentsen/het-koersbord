"""
News, behind one narrow interface.

Two providers, picked by whether an API key exists:

  finnhub   A real news endpoint with general, company and macro coverage.
            Needs a free key, pasted into Settings on the home screen and
            stored in the database — never in this file, never in git.

  yahoo     No key, per-ticker only, via yfinance. It covers your holdings
            adequately and macro barely at all, which is why it is the
            fallback rather than the default. The page says which one it is
            using, because "no macro news today" and "this provider has no
            macro news" are different facts.

The hard part is not fetching, it is DECIDING WHAT MATTERS. Two headlines on
the home screen means 98% of what arrives has to be thrown away, and throwing
away the wrong 98% is worse than showing nothing. So importance is scored
explicitly, from things that are actually known:

  * Does it name something you own, and how much of your money is in it?
    A headline about a 12% position outranks one about a 0.3% position.
  * Is it about a company big enough to move the market you are exposed to,
    even if you do not own it? A small hand-kept list, because "influential"
    cannot be inferred from a news feed.
  * Is it macro or geopolitical? Those move everything at once.
  * How old is it? A four-hour-old headline is not news at breakfast.

The score is returned with every item so the ranking can be argued with
rather than taken on faith.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from . import store, net

TIMEOUT = 15
UA = "het-koersbord/1.0"

# Companies whose news moves the whole market whether or not you hold them.
# Hand-kept on purpose: there is no field in any feed that means "influential",
# and inferring it from headline volume just measures which press office is
# busiest.
BELLWETHERS = {
    "NVDA": "Nvidia", "AAPL": "Apple", "MSFT": "Microsoft", "GOOGL": "Alphabet",
    "AMZN": "Amazon", "META": "Meta", "TSLA": "Tesla", "AVGO": "Broadcom",
    "TSM": "TSMC", "ASML": "ASML", "JPM": "JPMorgan", "BRK.B": "Berkshire",
    "XOM": "Exxon", "LLY": "Eli Lilly", "UNH": "UnitedHealth", "WMT": "Walmart",
}

# Words that mark a story as macro or geopolitical. Crude, and deliberately so:
# a keyword list is inspectable and fixable, which a model score is not.
MACRO_WORDS = re.compile(
    r"\b(fed|federal reserve|fomc|ecb|boj|bank of england|interest rate|rate cut|"
    r"rate hike|inflation|cpi|ppi|payrolls|unemployment|gdp|recession|tariff|"
    r"sanction|opec|oil price|treasury|yield|bond market|dollar|euro|"
    r"stimulus|budget|deficit|debt ceiling|election|war|ceasefire|invasion|"
    r"strait|export controls|trade deal|trade war|central bank)\b", re.I)

GEO_WORDS = re.compile(
    r"\b(china|taiwan|russia|ukraine|israel|iran|gaza|nato|opec|"
    r"north korea|venezuela|red sea|hormuz|strait of taiwan)\b", re.I)

# Sources that get a small nudge. Not a quality judgement on the others —
# these simply break more market-moving stories first.
STRONG_SOURCES = re.compile(
    r"(reuters|bloomberg|financial times|wall street journal|wsj|"
    r"associated press|\bap\b|cnbc|marketwatch|barron)", re.I)


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def api_key() -> Optional[str]:
    k = (store.get_setting("news_api_key") or "").strip()
    return k or None


def provider_name() -> str:
    return store.get_setting("news_provider") or ("finnhub" if api_key() else "yahoo")


def _get_json(url: str) -> Optional[object]:
    """
    Through pf.net, like every other feed. Left on plain urllib this would hit
    the same Windows certificate failure the rate feeds did — silently, and
    only once a Finnhub key was set, which is the worst moment to discover it.
    """
    raw = net.get(url, {"Accept": "application/json"}, timeout=TIMEOUT)
    return json.loads(raw.decode("utf-8", "replace"))


def _ts(v) -> Optional[float]:
    """Finnhub sends unix seconds; Yahoo sometimes sends milliseconds."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v > 1e11:            # milliseconds
        v /= 1000.0
    return v if 9e8 < v < 4e9 else None


def _item(headline, url, source, ts, summary="", tickers=None, category="") -> Optional[dict]:
    headline = (headline or "").strip()
    if not headline or not url:
        return None
    return {
        "headline": headline[:240],
        "summary": re.sub(r"\s+", " ", (summary or "")).strip()[:400],
        "source": (source or "").strip()[:40],
        "url": url,
        "ts": ts,
        "tickers": sorted({t.upper() for t in (tickers or []) if t})[:6],
        "category": category or "",
    }


def _finnhub_general(key: str, limit: int = 60) -> list[dict]:
    out = []
    for cat in ("general", "forex", "merger"):
        try:
            rows = _get_json("https://finnhub.io/api/v1/news?"
                             + urllib.parse.urlencode({"category": cat, "token": key}))
        except Exception:
            continue
        for r in (rows or [])[:limit]:
            it = _item(r.get("headline"), r.get("url"), r.get("source"),
                       _ts(r.get("datetime")), r.get("summary"),
                       [x for x in str(r.get("related") or "").split(",") if x],
                       r.get("category") or cat)
            if it:
                out.append(it)
    return out


def _finnhub_company(key: str, tickers: list[str], days: int = 5) -> list[dict]:
    from datetime import date, timedelta
    to = date.today()
    frm = to - timedelta(days=days)
    out = []
    # One call per symbol, so this is capped hard. The free tier allows 60 a
    # minute and there is no point spending them on a 0.2% position.
    for tk in tickers[:20]:
        try:
            rows = _get_json("https://finnhub.io/api/v1/company-news?"
                             + urllib.parse.urlencode(
                                 {"symbol": tk, "from": frm.isoformat(),
                                  "to": to.isoformat(), "token": key}))
        except Exception:
            continue
        for r in (rows or [])[:8]:
            it = _item(r.get("headline"), r.get("url"), r.get("source"),
                       _ts(r.get("datetime")), r.get("summary"),
                       [tk] + [x for x in str(r.get("related") or "").split(",") if x],
                       "company")
            if it:
                out.append(it)
    return out


def _yahoo(tickers: list[str], per: int = 6) -> list[dict]:
    """No key. Per-ticker only — Yahoo has no general feed through yfinance."""
    try:
        import yfinance as yf
    except Exception:
        return []
    out = []
    for tk in tickers[:15]:
        try:
            rows = yf.Ticker(tk).get_news(count=per) or []
        except Exception:
            continue
        for r in rows:
            # yfinance has moved this shape around between versions; read
            # defensively rather than assuming either layout.
            c = r.get("content") if isinstance(r.get("content"), dict) else r
            url = ((c.get("canonicalUrl") or {}).get("url")
                   if isinstance(c.get("canonicalUrl"), dict) else None) \
                or c.get("link") or r.get("link")
            pub = c.get("pubDate") or c.get("providerPublishTime") or r.get("providerPublishTime")
            ts = _ts(pub)
            if ts is None and isinstance(pub, str):
                try:
                    ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = None
            prov = c.get("provider") or {}
            it = _item(c.get("title") or r.get("title"), url,
                       prov.get("displayName") if isinstance(prov, dict) else r.get("publisher"),
                       ts, c.get("summary") or c.get("description"), [tk], "company")
            if it:
                out.append(it)
    return out


# --------------------------------------------------------------------------
# ranking
# --------------------------------------------------------------------------

def score(item: dict, weights: dict[str, float], now: Optional[float] = None) -> dict:
    """
    Why this headline and not another one. Every term is recorded so the
    ranking can be inspected — an unexplained ordering is indistinguishable
    from a random one.
    """
    now = now or time.time()
    text = f"{item['headline']} {item.get('summary','')}"
    why, s = [], 0.0

    # something you own, weighted by how much of your money is in it
    hit = [(t, weights.get(t, 0.0)) for t in item["tickers"] if t in weights]
    if hit:
        t, w = max(hit, key=lambda x: x[1])
        s += 40 + min(w, 20) * 3
        why.append(f"you hold {t} ({w:.1f}% of the book)")

    # a company large enough to drag the market with it
    bell = [t for t in item["tickers"] if t in BELLWETHERS] or \
           [t for t, n in BELLWETHERS.items() if re.search(rf"\b{re.escape(n)}\b", text, re.I)]
    if bell and not hit:
        s += 28
        why.append(f"{BELLWETHERS.get(bell[0], bell[0])} moves the market")

    if MACRO_WORDS.search(text):
        s += 34
        why.append("macro")
    if GEO_WORDS.search(text):
        s += 26
        why.append("geopolitical")
    # Hedge funds' positioning: de-grossing, prime-book data, forced selling. The
    # banks' prime-book figures are client notes and surface only in the press,
    # so FLOW collects these headlines from the log (build 51).
    from .flow import FLOW_WORDS
    if FLOW_WORDS.search(text):
        s += 22
        why.append("positioning")
    if STRONG_SOURCES.search(item.get("source") or ""):
        s += 8
        why.append(item["source"])

    # Recency, as a decay rather than a cliff: a strong story six hours old
    # should still beat a weak one from ten minutes ago.
    if item.get("ts"):
        hrs = max((now - item["ts"]) / 3600.0, 0.0)
        s += max(0.0, 30.0 - hrs * 2.2)
        if hrs < 2:
            why.append("just in")
    else:
        s -= 10                     # undated: probably stale, certainly unverifiable

    return {**item, "score": round(s, 1), "why": why,
            "macro": bool(MACRO_WORDS.search(text) or GEO_WORDS.search(text)),
            "flow": bool(FLOW_WORDS.search(text)),
            "mine": [t for t, _ in hit]}


_WORD = re.compile(r"[a-z0-9]+")

_TRACKING = re.compile(r"(^|&)(utm_[a-z]+|guccounter|guce_[a-z_]+|ncid|cmpid|src|ref)=[^&]*", re.I)


def url_key(url: str) -> str:
    """
    A link, minus the tracking noise, for spotting the same article twice.

    Dropping the whole query string was wrong: every Finnhub link is
    finnhub.io/api/news?id=..., so without the query they were all ONE url,
    and deduplication kept a single Finnhub story out of hundreds.
    """
    u = (url or "").strip()
    if "?" not in u:
        return u.rstrip("/")
    base, q = u.split("?", 1)
    q = _TRACKING.sub("", q).strip("&")
    return base.rstrip("/") + ("?" + q if q else "")


# Wire prefixes that carry no meaning and would otherwise stop two copies of
# one story from matching: "UPDATE 2-", "EXCLUSIVE-", "BRIEF-".
_NOISE = re.compile(r"^\s*(update\s*\d*\s*-|exclusive\s*-|brief\s*-|refile\s*-)+", re.I)

_STOP = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "as", "is",
         "at", "by", "with", "after", "from", "its", "it", "says", "say",
         "amid", "over", "into", "that", "this", "new", "more", "than"}


def _key(headline: str) -> frozenset:
    """A headline's fingerprint: its significant words, order ignored."""
    return frozenset(w for w in _WORD.findall(_NOISE.sub("", headline).lower())
                     if len(w) > 2 and w not in _STOP)


def dedupe(items: list[dict]) -> list[dict]:
    """
    One story, one row. The same event reaches a feed from four outlets with
    four headlines, and a panel showing two items cannot afford to spend both
    on the same event.

    Two tests, because wire copy duplicates in two different ways:

      * Near-identical wording — the same story syndicated. Caught by a high
        Jaccard overlap of the significant words.
      * A rewrite about the same company — "Nvidia beats on data centre
        revenue" and "Nvidia tops estimates as data centre sales surge" share
        only a third of their words but are plainly one event. Caught by
        requiring a SHARED TICKER plus a decent overlap of the smaller
        headline.

    The second test is deliberately conditioned on the ticker. Without that
    condition it would merge genuinely separate stories about different
    companies that happen to share vocabulary; with it, two different Nvidia
    stories — an earnings beat and an antitrust probe — still both survive,
    because they share almost no words beyond the name.

    Items arrive sorted by score, so the copy that survives is the best one.
    """
    out, seen_urls, kept = [], set(), []
    for it in items:
        u = url_key(it.get("url"))
        if u and u in seen_urls:
            continue
        k = _key(it["headline"])
        if not k:
            continue
        tk = set(it.get("tickers") or [])

        dup = False
        for prev_k, prev_tk in kept:
            inter = len(k & prev_k)
            if inter / max(len(k | prev_k), 1) > 0.6:
                dup = True
                break
            if (tk & prev_tk) and inter / max(min(len(k), len(prev_k)), 1) >= 0.5:
                dup = True
                break
        if dup:
            continue

        if u:
            seen_urls.add(u)
        kept.append((k, tk))
        out.append(it)
    return out


def rank(items: list[dict], weights: dict[str, float]) -> list[dict]:
    scored = [score(i, weights) for i in items]
    scored.sort(key=lambda x: -x["score"])
    return dedupe(scored)


# --------------------------------------------------------------------------
# the one call the rest of the app makes
# --------------------------------------------------------------------------

def fetch(tickers: list[str], weights: dict[str, float]) -> dict:
    """
    Everything, ranked. Blocking and slow — call it from a background thread,
    never from a request.
    """
    key = api_key()
    prov, items, err = provider_name(), [], None
    t0 = time.time()
    try:
        if key and prov == "finnhub":
            items = _finnhub_general(key) + _finnhub_company(key, tickers)
            if not items:
                err = "the news API returned nothing — check the key in Settings"
        else:
            items = _yahoo(tickers)
            prov = "yahoo"
            if not items:
                err = "no headlines available without an API key"
    except Exception as e:
        err = f"{e.__class__.__name__}: {e}"

    ranked = rank(items, weights)
    try:
        log(ranked)
    except Exception:
        pass
    return {
        "provider": prov,
        "has_key": bool(key),
        "error": err,
        "fetched_at": time.time(),
        "took_s": round(time.time() - t0, 2),
        "count": len(ranked),
        "items": ranked[:60],
    }


LOG_DAYS = 10


def log(items: list[dict]):
    """
    Keep every headline seen, not only the sixty the cache holds. The morning
    brief reads back over yesterday; by breakfast the feed has moved on.
    """
    if not items:
        return
    now = time.time()
    rows = [(url_key(i.get("url")) or i["headline"][:200], i.get("ts"), now, i["headline"],
             i.get("summary") or "", i.get("source") or "", i.get("url") or "",
             json.dumps(i.get("tickers") or []), i.get("category") or "") for i in items]
    store.init_db()
    with store.conn() as c:
        c.executemany("INSERT OR IGNORE INTO news_log (k,ts,seen,headline,summary,source,url,tickers,category) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", rows)
        c.execute("DELETE FROM news_log WHERE seen < ?", (now - LOG_DAYS * 86400,))


def logged(since: float, until: Optional[float] = None) -> list[dict]:
    """Headlines published in [since, until], as items the ranking understands."""
    store.init_db()
    with store.conn() as c:
        rows = c.execute("SELECT * FROM news_log WHERE COALESCE(ts, seen) >= ? AND COALESCE(ts, seen) <= ?",
                         (since, until or time.time() + 60)).fetchall()
    out = []
    for r in rows:
        try:
            tks = json.loads(r["tickers"] or "[]")
        except Exception:
            tks = []
        out.append({"headline": r["headline"], "summary": r["summary"] or "", "source": r["source"] or "",
                    "url": r["url"], "ts": r["ts"] or r["seen"], "tickers": tks,
                    "category": r["category"] or ""})
    return out


def split(payload: dict, top_n: int = 2, banner_n: int = 8) -> dict:
    """
    The two headlines for the top of the screen, and the banner below it.

    The top two are picked for SPREAD as well as score: two stories about the
    same company is a worse briefing than one company and one macro story,
    even when the second company story scores higher.
    """
    items = payload.get("items") or []
    top: list[dict] = []
    for it in items:
        if len(top) >= top_n:
            break
        if top:
            prev = top[-1]
            same_co = bool(set(it["tickers"]) & set(prev["tickers"]))
            same_kind = it["macro"] and prev["macro"]
            if same_co or same_kind:
                continue
        top.append(it)
    # If being picky left a gap, fill it by score — a thin feed should still
    # fill the slots rather than leave them empty out of principle.
    for it in items:
        if len(top) >= top_n:
            break
        if it not in top:
            top.append(it)

    rest = [i for i in items if i not in top]
    return {**payload, "top": top, "banner": rest[:banner_n], "rest": rest}
