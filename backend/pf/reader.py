"""
READ — the day's live coverage, and articles written for one reader.

The home screen's two news items were ranked by whether a headline names a
holding, so a Motley Fool listicle tagged GOOGL beat Reuters on the Strait of
Hormuz. This replaces them with two things:

  LIVE COVERAGE   the day's running market story, found without AI in
                  Yahoo's RSS feeds for the S&P 500, the Dow and the Nasdaq:
                  IBD's "Stock Market Today: … (Live Coverage)", The Motley
                  Fool's "Stock Market Midday/Today, Oct. 23: …", TheStreet's
                  "Stock Market Today (Oct. 23, 2026)". At a weekend or before
                  the open there is none, and the last session's is shown as
                  such.

  ARTICLES        written by Claude, through the same writer as the morning
                  brief (Claude Code on your plan, or an API key):
     gather       the terminal's own news feed (Finnhub, scored by news.py),
                  Yahoo's feeds for the holdings, The Motley Fool's coverage,
                  today's macro releases, the holdings' reports. No AI.
     triage       one run with web search: the editor reads the candidates,
                  searches for what they miss (a war, a central bank, a widely
                  read research note), groups duplicates, scores importance 1-5
                  and picks between none and the day's remaining budget.
     write        one run per pick, with web search and fetch: the sources
                  read and turned into a structured article in its own words.
                  The terminal draws the figures from real data and adds what
                  the story means for the book in euros.

  Three scans on a weekday (07:15, 16:30, 22:45 Amsterdam), one at a weekend
  with its edition (Saturday the week in review, Sunday the week ahead), at
  most eight articles a day. A story already written is updated in place, a
  new version with what changed, rather than written again (threads). Each
  article can be asked about, in the session that wrote it.

Copyright: the articles are the model's own words with links to every
source, and at most one quote of under fifteen words; a paywalled page is
replaced by another report of the same facts, never worked around.

Nothing here runs by itself unless the server started the loop (ensure());
verify.py and scripts never do, because every run spends the plan.
"""

from __future__ import annotations

import email.utils
import json
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import store

AMS = ZoneInfo("Europe/Amsterdam")
NYZ = ZoneInfo("America/New_York")

SLOTS = {"morning": (7, 15), "midday": (16, 30), "evening": (22, 45)}
WEEKEND = {5: ("review", (9, 30)), 6: ("ahead", (9, 30))}
SLOT_LABEL = {"morning": "morning scan", "midday": "midday scan", "evening": "evening scan",
              "review": "Saturday: the week in review", "ahead": "Sunday: the week ahead", "manual": "scan on request"}
CATCH_UP_H = 3                 # a slot missed by more than this (the laptop was off) waits for the next one
DAILY_MAX = 8                  # articles a day, the user's choice of 23 October 2026
PER_SCAN = {"morning": 3, "midday": 3, "evening": 4, "review": 2, "ahead": 2, "manual": 4}
MIN_IMPORTANCE = 3
KINDS = {"event": "Event", "earnings": "Earnings", "research": "Research", "macro": "Macro",
         "theme": "Theme", "review": "The week", "ahead": "The week ahead"}
THREAD_DAYS = 7                # a story not updated in a week is closed: news of it is a new story
FIG_TYPES = ("price", "map", "timeline", "bars")

RSS = "https://feeds.finance.yahoo.com/rss/2.0/headline"
LIVE_FEEDS = ("^GSPC", "^DJI", "^IXIC")
FOOL_SITEMAP = "https://www.fool.com/news-sitemap.xml"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/141.0 Safari/537.36"}
SYM_RX = re.compile(r"^\^?[A-Z0-9]{1,6}([.\-=][A-Z0-9]{1,4})?$")

PUBLISHERS = {"fool.com": "The Motley Fool", "thestreet.com": "TheStreet", "investors.com": "Investor's Business Daily",
              "reuters.com": "Reuters", "cnbc.com": "CNBC", "bloomberg.com": "Bloomberg", "wsj.com": "The Wall Street Journal",
              "ft.com": "Financial Times", "nytimes.com": "The New York Times", "apnews.com": "AP", "barrons.com": "Barron's",
              "marketwatch.com": "MarketWatch", "investing.com": "Investing.com", "finance.yahoo.com": "Yahoo Finance",
              "247wallst.com": "24/7 Wall St.", "investopedia.com": "Investopedia", "benzinga.com": "Benzinga",
              "seekingalpha.com": "Seeking Alpha", "economist.com": "The Economist", "theguardian.com": "The Guardian",
              "bbc.co.uk": "BBC", "bbc.com": "BBC", "axios.com": "Axios", "politico.com": "Politico", "sec.gov": "SEC",
              "federalreserve.gov": "Federal Reserve", "ecb.europa.eu": "ECB", "goldmansachs.com": "Goldman Sachs",
              "morganstanley.com": "Morgan Stanley", "jpmorgan.com": "J.P. Morgan", "fd.nl": "Het Financieele Dagblad"}


def publisher(url: str) -> str:
    host = urllib.parse.urlparse(url or "").netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    for k, v in PUBLISHERS.items():
        if host == k or host.endswith("." + k):
            return v
    return host or "?"


def now_ams() -> datetime:
    return datetime.now(AMS)


# --------------------------------------------------------------------------
# live coverage
# --------------------------------------------------------------------------

# How sure a title is the day's running market story. "(Live Coverage)" is
# IBD's live blog; "Stock Market Today/Midday" opens Fool's and TheStreet's.
LIVE_RX = [(re.compile(r"\(live coverage\)|\blive updates?\b|\blive blog\b", re.I), 3),
           (re.compile(r"^stock market (today|midday)\b", re.I), 2),
           (re.compile(r"^(dow|s&p 500|nasdaq)\b.*\b(today|live)\b", re.I), 1)]
# The Motley Fool files single-company pieces under the same "Stock Market
# Today, Oct. 23:" heading ("SpaceX Achieves Rare Double Upgrade…"). A title
# about the market names it: an index, stocks in the plural, sectors.
MARKET_RX = re.compile(r"\b(dow|s&p|nasdaq|russell|wall street|stocks|indexes|indices|sectors?|market[s]? (rise|fall|slide|rally|close|drop|gain))\b"
                       r"|\b(stock market midday)\b", re.I)


def live_rank(title: str) -> int:
    """3 a live blog, 2 the day's market story, 1 a single company's piece under the same heading, 0 none."""
    for rx, r in LIVE_RX:
        if rx.search(title or ""):
            if r == 2 and not MARKET_RX.search((title or "").split(":", 1)[-1] + (" stock market midday" if re.match(r"^stock market midday", title or "", re.I) else "")):
                return 1
            return r
    return 0


def parse_rss(raw: bytes) -> list[dict]:
    """Items of a Yahoo RSS feed: title, link without its tracking tail, time, publisher."""
    root = ET.fromstring(raw)
    out = []
    for it in root.iter("item"):
        t = (it.findtext("title") or "").strip()
        url = (it.findtext("link") or "").strip().split("?.tsrc=")[0]
        if not t or not url:
            continue
        try:
            ts = email.utils.parsedate_to_datetime(it.findtext("pubDate") or "").timestamp()
        except Exception:
            ts = None
        out.append({"title": t, "url": url, "ts": ts, "source": publisher(url),
                    "summary": re.sub(r"<[^>]+>", "", it.findtext("description") or "")[:400].strip()})
    return out


def _rss(symbols: str) -> list[dict]:
    from . import net
    q = urllib.parse.urlencode({"s": symbols, "region": "US", "lang": "en-US"})
    return parse_rss(net.get(f"{RSS}?{q}", UA, timeout=12))


def pick_live(items: list[dict], now: Optional[datetime] = None) -> dict:
    """
    The day's live coverage among feed items: the newest New York day that has
    any, the surest title first, the rest of that day beside it. 'live' while
    New York trades, 'today' before or after, 'last' for an earlier session.
    """
    from . import sessions
    now = now or datetime.now(timezone.utc)
    seen, cands = set(), []
    for x in items:
        r = live_rank(x.get("title"))
        if not r or not x.get("ts") or x["url"] in seen:
            continue
        seen.add(x["url"])
        cands.append({**x, "rank": r, "nyd": datetime.fromtimestamp(x["ts"], NYZ).date().isoformat()})
    if not cands:
        return {"state": "none", "main": None, "more": [], "label": "No live coverage found"}
    day = max(c["nyd"] for c in cands)
    same = sorted((c for c in cands if c["nyd"] == day), key=lambda c: (-c["rank"], -c["ts"]))
    today = now.astimezone(NYZ).date().isoformat()
    if day == today:
        state = "live" if sessions.state("US", now) == "open" else "today"
    else:
        state = "last"
    d = date.fromisoformat(day)
    label = {"live": "Live now", "today": "Today's coverage"}.get(state) or f"Last session · {d:%a} {d.day} {d:%b}"
    clean = lambda c: {k: c[k] for k in ("title", "url", "ts", "source", "summary", "rank")}
    return {"state": state, "label": label, "day": day, "main": clean(same[0]),
            "more": [clean(c) for c in sorted(same[1:], key=lambda c: -c["ts"])[:4]]}


def _load_live() -> dict:
    items, errs = [], []
    for s in LIVE_FEEDS:
        try:
            items += _rss(s)
        except Exception as e:
            errs.append(f"{s}: {e.__class__.__name__}")
        time.sleep(0.4)
    if errs and not items:                # every feed failed: say so, and do not keep that answer ten minutes
        raise RuntimeError("; ".join(errs))
    try:                                  # Fool's own list has every edition of the day, not only those Yahoo carries
        items += [{**x, "summary": ""} for x in _fool(time.time() - 36 * 3600) if "/stock-market-today/" in x["url"]]
    except Exception as e:
        errs.append(f"Fool: {e.__class__.__name__}")
    out = pick_live(items)
    out["errors"] = errs
    out["fetched_at"] = time.time()
    return out


def live() -> dict:
    try:
        return store.cache_through("read:live", 600, _load_live)
    except Exception as e:
        return {"state": "none", "main": None, "more": [], "label": "No live coverage found",
                "errors": [f"{e.__class__.__name__}: {e}"[:200]]}


# --------------------------------------------------------------------------
# gathering: what the editor gets to read
# --------------------------------------------------------------------------

def _book() -> dict:
    """The holdings as the writer needs them: what, how much, and how it moved today."""
    from . import service
    d = service.dashboard("SP500", light=True)
    if d.get("empty"):
        return {"total": 0, "rows": [], "sectors": {}}
    T = d.get("totals") or {}
    tot = T.get("value_eur") or 0
    rows = []
    for p in d.get("positions") or []:
        if p.get("excluded"):
            continue
        t = p.get("ticker") or p.get("underlying_ticker")
        if not t:
            continue
        v = p.get("value_eur") or 0
        spec = ((p.get("lev") or {}).get("spec") or {}) if p.get("kind") == "DERIVATIVE" else {}
        rows.append({"t": t, "name": p.get("name"), "turbo": p.get("kind") == "DERIVATIVE",
                     "direction": spec.get("direction"), "barrier": spec.get("barrier"),
                     "value": round(v), "exposure": round(p.get("exposure_eur") or v),
                     "w": round(v / tot * 100, 2) if tot else None, "day": p.get("day_pct"), "last": p.get("last_pct"),
                     "sector": p.get("sector") or "Unclassified"})
    rows.sort(key=lambda r: -r["value"])
    sec: dict = {}
    for r in rows:
        sec[r["sector"]] = round(sec.get(r["sector"], 0) + (r["w"] or 0), 2)
    return {"total": round(tot), "cash": round(T.get("cash_eur") or 0), "day_pct": T.get("day_pct"),
            "rows": rows, "sectors": dict(sorted(sec.items(), key=lambda kv: -kv[1]))}


def _hold_line(r: dict) -> str:
    """One holding as the writer reads it; a turbo with its direction, exposure and knock-out level."""
    t = ""
    if r.get("turbo"):
        t = (f" (turbo {(r.get('direction') or '').lower() or 'long'} on {r['t']}, exposure €{r['exposure']:,}"
             + (f", knock-out at {r['barrier']}" if r.get("barrier") else "") + ")")
    day = r.get("day") if r.get("day") else r.get("last")
    return f"{r['t']} {(r['name'] or '')[:40]}: {r['w']}% of the book{t}" + (f", last move {day}%" if day is not None else "")


def _market() -> list[dict]:
    rows = (store.cache_get("market") or {}).get("rows") or []
    return [{"k": r.get("k"), "name": r.get("n"), "last": r.get("last"), "day_pct": r.get("d_pct"),
             "week_pct": r.get("w_pct"), "day_abs": r.get("d_abs"), "week_abs": r.get("w_abs"),
             "unit": r.get("u"), "asof": r.get("asof")} for r in rows if r.get("last") is not None]


def _market_lines(n: int = 40) -> list[str]:
    """
    The market rows as the writer reads them: each with the date it is from
    (FRED's yields lag a day or two), yields' moves in basis points, the rest
    in percent.
    """
    out = []
    for m in _market()[:n]:
        f = lambda v, d=2: "?" if v is None else f"{v:+.{d}f}"
        if m["unit"] == "pct":
            mv = (f"{f(m['day_abs'] * 100 if m['day_abs'] is not None else None, 0)}bp on the day, "
                  f"{f(m['week_abs'] * 100 if m['week_abs'] is not None else None, 0)}bp on the week")
            lvl = f"{m['last']}%"
        else:
            mv = f"{f(m['day_pct'])}% on the day, {f(m['week_pct'])}% on the week"
            lvl = f"{m['last']}{'' if m['unit'] in ('idx', 'fx', None) else ' ' + m['unit']}"
        out.append(f"{m['name'] or m['k']}: {lvl} as of {m['asof'] or '?'} ({mv})")
    return out


def _fool(since: float) -> list[dict]:
    """The Motley Fool's news sitemap: every coverage piece, with its own title and time."""
    from . import net
    raw = net.get(FOOL_SITEMAP, UA, timeout=20)
    out = []
    for blk in re.findall(r"<url>(.*?)</url>", raw.decode("utf-8", "replace"), re.S):
        loc = re.search(r"<loc>(.*?)</loc>", blk)
        title = re.search(r"<news:title>(.*?)</news:title>", blk, re.S)
        when = re.search(r"<news:publication_date>(.*?)</news:publication_date>", blk)
        if not (loc and title and when) or "/coverage/" not in loc.group(1):
            continue
        try:
            ts = datetime.fromisoformat(when.group(1).strip().replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        if ts < since:
            continue
        t = re.sub(r"<!\[CDATA\[|\]\]>", "", title.group(1)).strip()
        out.append({"title": t, "url": loc.group(1).strip(), "ts": ts, "source": "The Motley Fool"})
    return out


def dedupe(items: list[dict]) -> list[dict]:
    """One per URL, and one per story where two titles share most of their words."""
    from . import news
    out, keys, urls = [], [], set()
    for x in sorted(items, key=lambda x: -(x.get("score") or 0)):
        u = news.url_key(x["url"])
        if u in urls:
            continue
        k = news._key(x["title"])
        if k and any(len(k & o) / max(1, len(k | o)) >= 0.6 for o in keys):
            continue
        urls.add(u)
        keys.append(k)
        out.append(x)
    return out


def gather(now: Optional[float] = None, fetch: bool = True, book: Optional[dict] = None) -> list[dict]:
    """Candidates for the editor: the feed's own, the holdings' Yahoo feeds and Fool's coverage, newest first."""
    now = now or time.time()
    weekend = datetime.fromtimestamp(now, AMS).weekday() >= 5
    since = now - (72 if weekend else 36) * 3600
    items = []
    for x in (store.cache_get("news") or {}).get("items") or []:
        if isinstance(x, dict) and x.get("headline") and x.get("url"):
            items.append({"title": x["headline"], "url": x["url"], "source": x.get("source"), "ts": x.get("ts"),
                          "summary": (x.get("summary") or "")[:300], "tickers": x.get("tickers") or [],
                          "mine": bool(x.get("mine")), "macro": bool(x.get("macro")), "score": x.get("score") or 0,
                          "via": "news feed"})
    if fetch:
        try:
            syms = [r["t"] for r in (book or _book())["rows"]][:40]
            for i in range(0, len(syms), 10):
                for x in _rss(",".join(syms[i:i + 10])):
                    items.append({**x, "tickers": [], "mine": True, "macro": False, "score": 40, "via": "holdings feed"})
                time.sleep(0.4)
        except Exception:
            pass
        try:
            items += [{**x, "summary": "", "tickers": [], "mine": False, "macro": False, "score": 30, "via": "Motley Fool"}
                      for x in _fool(since)]
        except Exception:
            pass
    items = [x for x in items if (x.get("ts") or now) >= since]
    for x in items:                       # a fresh story outranks a stale one with the same score
        x["score"] = (x.get("score") or 0) - (now - (x.get("ts") or now)) / 3600
    return dedupe(items)[:80]


def _calendar(days: int = 2) -> dict:
    from . import earnings, macro
    t = now_ams().date()
    out = {"earnings": [], "macro": []}
    try:
        out["earnings"] = [{"sym": e["sym"], "d": e["date"], "when": e.get("when"), "name": e.get("name")}
                           for e in earnings.upcoming_mine(days + 1)][:20]
    except Exception:
        pass
    try:
        rows = macro.rows((t - timedelta(days=1)).isoformat(), (t + timedelta(days=days)).isoformat())
        out["macro"] = [{"d": r["d"], "t": r.get("t"), "ccy": r.get("ccy"), "title": r.get("title"),
                         "actual": r.get("actual"), "forecast": r.get("forecast"), "previous": r.get("previous")}
                        for r in rows if (r.get("rank") or 0) >= 2][:30]
    except Exception:
        pass
    return out


# --------------------------------------------------------------------------
# the store
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


def open_threads(now: Optional[float] = None) -> list[dict]:
    now = now or time.time()
    cut = datetime.fromtimestamp(now - THREAD_DAYS * 86400, timezone.utc).isoformat(timespec="seconds")
    out = []
    for s in _rows("SELECT * FROM read_story WHERE updated >= ? ORDER BY updated DESC", (cut,)):
        a = _rows("SELECT id, version, headline, dek, created, body FROM read_article WHERE story_id=? "
                  "ORDER BY version DESC LIMIT 1", (s["id"],))
        if a:
            b = _j(a[0]["body"], {})
            out.append({"thread": s["id"], "title": a[0]["headline"], "kind": s["kind"], "version": a[0]["version"],
                        "updated": s["updated"], "dek": a[0]["dek"], "tickers": _j(s["tickers"], []),
                        "takeaways": b.get("takeaways") or []})
    return out


def written_on(d: str) -> int:
    return _rows("SELECT COUNT(*) AS n FROM read_article WHERE d=?", (d,))[0]["n"]


def save_article(a: dict, slot: str, d: str, meta: dict, cost_usd: Optional[float], thread: Optional[int] = None) -> int:
    """A new story, or the next version of a thread."""
    store.init_db()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with store.conn() as c:
        if thread and c.execute("SELECT 1 FROM read_story WHERE id=?", (thread,)).fetchone():
            sid = thread
            ver = (c.execute("SELECT MAX(version) FROM read_article WHERE story_id=?", (sid,)).fetchone()[0] or 0) + 1
            c.execute("UPDATE read_story SET updated=?, tickers=? WHERE id=?", (ts, json.dumps(a["tickers"]), sid))
        else:
            sid = c.execute("INSERT INTO read_story (title, kind, tickers, created, updated) VALUES (?,?,?,?,?)",
                            (a["headline"], a["kind"], json.dumps(a["tickers"]), ts, ts)).lastrowid
            ver = 1
        return c.execute("INSERT INTO read_article (story_id, version, slot, d, created, kind, importance, headline, dek, "
                         "tickers, body, meta, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (sid, ver, slot, d, ts, a["kind"], a["importance"], a["headline"], a["dek"],
                          json.dumps(a["tickers"]), json.dumps(a), json.dumps(meta), cost_usd)).lastrowid


def _scan_row(d: str, slot: str) -> int:
    store.init_db()
    with store.conn() as c:
        return c.execute("INSERT INTO read_scan (d, slot, started, state) VALUES (?,?,?,?)",
                         (d, slot, datetime.now(timezone.utc).isoformat(timespec="seconds"), "running")).lastrowid


def _scan_done(sid: int, **kw):
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in kw.values()]
    with store.conn() as c:
        c.execute(f"UPDATE read_scan SET {cols} WHERE id=?", (*vals, sid))


def scans(d: str) -> list[dict]:
    out = _rows("SELECT * FROM read_scan WHERE d=? ORDER BY id", (d,))
    for s in out:
        for k in ("picks", "skipped", "meta"):
            s[k] = _j(s.get(k), [] if k != "meta" else {})
    return out


# --------------------------------------------------------------------------
# what the model returns, checked
# --------------------------------------------------------------------------

def parse_json(text: str) -> dict:
    """The JSON object in a reply: the last ```json block, or the outermost braces."""
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text or "", re.S)
    s = blocks[-1] if blocks else (text or "")[(text or "").find("{"):(text or "").rfind("}") + 1]
    v = json.loads(s)
    if not isinstance(v, dict):
        raise ValueError("the reply is not a JSON object")
    return v


def _s(v, n: int) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


def _clip(v, n: int) -> str:
    """Short, at a word: a step of the chain cut in the middle of a word reads as a fault."""
    s = _s(v, 4000)
    if len(s) <= n:
        return s
    return s[:n].rsplit(" ", 1)[0].rstrip(",;:") + "…"


def _syms(v, n: int = 6) -> list[str]:
    out = []
    for x in v or []:
        x = str(x or "").strip().upper()
        if SYM_RX.match(x) and x not in out:
            out.append(x)
    return out[:n]


def _url(v) -> Optional[str]:
    u = str(v or "").strip()
    return u if re.match(r"^https?://[^\s]+$", u) else None


def _sources(v, n: int = 12) -> list[dict]:
    out, seen = [], set()
    for s in v or []:
        if not isinstance(s, dict):
            continue
        u = _url(s.get("url"))
        if not u or u in seen:
            continue
        seen.add(u)
        out.append({"url": u, "title": _s(s.get("title"), 200), "publisher": _s(s.get("publisher"), 60) or publisher(u),
                    "used": _s(s.get("used"), 200)})
    return out[:n]


def _date(v) -> Optional[str]:
    s = str(v or "")[:10]
    try:
        return date.fromisoformat(s).isoformat()
    except Exception:
        return None


def clean_triage(t: dict, budget: int, threads: list[dict]) -> dict:
    """The editor's picks, kept to the rules: importance 3 and up, the day's budget, real threads only."""
    ids = {x["thread"] for x in threads}
    picks, skipped = [], []
    for p in t.get("picks") or []:
        if not isinstance(p, dict):
            continue
        try:
            imp = max(1, min(5, int(p.get("importance") or 0)))
        except Exception:
            imp = 1
        title = _s(p.get("title"), 200)
        if not title:
            continue
        if imp < MIN_IMPORTANCE:
            skipped.append({"title": title, "why": f"importance {imp}: " + _s(p.get("why"), 200)})
            continue
        kind = p.get("kind") if p.get("kind") in KINDS and p.get("kind") not in ("review", "ahead") else "event"
        th = p.get("thread")
        try:
            th = int(th) if th is not None and int(th) in ids else None
        except Exception:
            th = None
        picks.append({"kind": kind, "title": title, "importance": imp, "why": _s(p.get("why"), 500),
                      "tickers": _syms(p.get("tickers"), 8), "angle": _s(p.get("angle"), 700), "thread": th,
                      "changed": _s(p.get("changed"), 400) if th else "", "sources": _sources(p.get("sources"), 6)})
    picks.sort(key=lambda p: -p["importance"])
    for p in picks[max(0, budget):]:
        skipped.append({"title": p["title"], "why": f"importance {p['importance']}, over today's budget"})
    for s in t.get("skipped") or []:
        if isinstance(s, dict) and s.get("title"):
            skipped.append({"title": _s(s.get("title"), 200), "why": _s(s.get("why"), 200)})
    return {"summary": _s(t.get("summary"), 400), "picks": picks[:max(0, budget)], "skipped": skipped[:30]}


def _fig(f: dict) -> Optional[dict]:
    if not isinstance(f, dict) or f.get("type") not in FIG_TYPES:
        return None
    t, title = f["type"], _s(f.get("title"), 140)
    if t == "price":
        syms = _syms(f.get("tickers"), 5)
        if not syms:
            return None
        try:
            days = max(5, min(400, int(f.get("days") or 30)))
        except Exception:
            days = 30
        return {"type": t, "title": title, "tickers": syms, "days": days, "mark": _date(f.get("mark")),
                "rebase": bool(f.get("rebase", len(syms) > 1))}
    if t == "map":
        pts = []
        for p in f.get("points") or []:
            try:
                la, lo = float(p.get("lat")), float(p.get("lon"))
            except Exception:
                continue
            if -90 <= la <= 90 and -180 <= lo <= 180:
                pts.append({"lat": round(la, 3), "lon": round(lo, 3), "label": _s(p.get("label"), 60)})
        ctry = [str(c).upper()[:2] for c in f.get("countries") or [] if re.match(r"^[A-Za-z]{2}$", str(c))][:16]
        if not pts and not ctry:
            return None
        return {"type": t, "title": title, "points": pts[:14], "countries": ctry}
    if t == "timeline":
        ev = [{"d": _date(e.get("d")), "t": _s(e.get("t"), 200)} for e in f.get("events") or []
              if isinstance(e, dict) and _date(e.get("d")) and e.get("t")]
        return {"type": t, "title": title, "events": sorted(ev, key=lambda e: e["d"])[:14]} if len(ev) >= 2 else None
    items = []
    for it in f.get("items") or []:
        try:
            items.append({"k": _s(it.get("k"), 50), "v": float(it.get("v"))})
        except Exception:
            continue
    if len(items) < 2 or not f.get("source"):
        return None                       # a bar chart of numbers nobody can trace is not drawn
    return {"type": "bars", "title": title, "unit": _s(f.get("unit"), 12), "items": items[:14], "source": _s(f.get("source"), 160)}


def clean_article(a: dict, kind: str = "event", importance: int = 3) -> dict:
    """An article as the page shows it: every field typed and bounded, sources required, quotes kept short."""
    sec = []
    for s in a.get("sections") or []:
        if isinstance(s, dict) and s.get("h") and s.get("p"):
            ps = s["p"] if isinstance(s["p"], list) else [s["p"]]
            ps = [_s(p, 1800) for p in ps if _s(p, 10)][:5]
            if ps:
                sec.append({"h": _s(s["h"], 80), "p": ps})
    src = _sources(a.get("sources"))
    if not sec:
        raise ValueError("the article has no sections")
    if not src:
        raise ValueError("the article cites no sources")
    q = a.get("quote") if isinstance(a.get("quote"), dict) else None
    if q and (not q.get("text") or len(str(q["text"]).split()) >= 15):
        q = None                          # a longer quote is somebody else's writing, not ours to copy
    nums = a.get("numbers") if isinstance(a.get("numbers"), dict) else None
    if nums:
        rows = [[_s(c, 60) for c in r][:6] for r in nums.get("rows") or [] if isinstance(r, list) and r][:14]
        nums = {"title": _s(nums.get("title"), 120), "cols": [_s(c, 40) for c in nums.get("cols") or []][:6],
                "rows": rows, "note": _s(nums.get("note"), 300)} if rows else None
    try:
        imp = max(1, min(5, int(a.get("importance") or importance)))
    except Exception:
        imp = importance
    k = a.get("kind") if a.get("kind") in KINDS else kind
    return {
        "kind": k, "headline": _s(a.get("headline"), 120) or "Untitled", "dek": _s(a.get("dek"), 260),
        "importance": imp, "importance_why": _s(a.get("importance_why"), 300),
        "tickers": _syms(a.get("tickers"), 6), "sectors": [_s(x, 40) for x in a.get("sectors") or []][:5],
        "takeaways": [_s(x, 300) for x in a.get("takeaways") or [] if _s(x, 5)][:5],
        "sections": sec[:8],
        "chain": [_clip(x, 70) for x in a.get("chain") or [] if _s(x, 2)][:7],
        "numbers": nums,
        "figures": [f for f in (_fig(x) for x in (a.get("figures") or [])[:6]) if f],
        "bull": [_s(x, 300) for x in a.get("bull") or []][:5], "bear": [_s(x, 300) for x in a.get("bear") or []][:5],
        "book": _s(a.get("book"), 1500),
        "watch": [{"d": _date(w.get("d")), "t": _s(w.get("t"), 200)} for w in a.get("watch") or []
                  if isinstance(w, dict) and w.get("t")][:8],
        "quote": {"text": _s(q["text"], 140), "who": _s(q.get("who"), 80)} if q else None,
        "sources": src, "changed": _s(a.get("changed"), 600) or None,
    }


# --------------------------------------------------------------------------
# the prompts
# --------------------------------------------------------------------------

TRIAGE_SYSTEM = """You are the editor of a one-reader financial newsroom. The reader is a private investor in the Netherlands with a concentrated portfolio, mostly US technology (AI, semiconductors, cloud, data-centre power) with some European shares and a few leveraged turbos. Several times a day the reader's terminal hands you the stories it collected (its own news feed, Yahoo's feeds for the holdings, The Motley Fool's coverage), the holdings with their weights, the markets, today's calendar, the stories already written in the last days, and how many articles may still be written today.

Decide which stories deserve a full article now. An article is worth writing when it explains something that moves the market or the reader's holdings and would otherwise have to be pieced together from several chaotic reports. Score each candidate's importance:
5 moves the whole market or the book by several percent: a war escalating around oil supply, a central-bank surprise, a mega-cap's results shock.
4 clearly moves a sector the reader holds, or is a holding's own results, guidance or major news.
3 useful context with consequences: a major firm's research on the AI trade, a significant analyst call, a policy change in the pipeline.
2 or less is noise: listicles, "should you buy", price-target chatter, recycled opinion, promotion.

Rules:
- Pick only importance 3 and above, at most the number of articles allowed, fewer on a quiet day. Zero picks is a good answer when nothing qualifies.
- One article per story: group candidates about the same event.
- A story in the open threads is picked again only when something material changed since its last version: give its thread id and say what changed. Otherwise leave it out.
- Use web search, at most five searches, to catch what the list misses (geopolitics, macro, a widely cited research report) and to find the first-rate source behind a rewrite: Reuters, AP, Bloomberg, the FT, the WSJ, the New York Times, CNBC, the company's release, the filing, the central bank.
- Prefer stories with a clear chain to prices over stories about prices alone.

Reply with one JSON object in a ```json block and nothing after it:
{"summary": "the day in one sentence",
 "picks": [{"kind": "event|earnings|research|macro|theme", "title": "working title", "importance": 3,
            "why": "why it matters now, one or two sentences", "tickers": ["most affected symbols, Yahoo style"],
            "angle": "the questions the article must answer", "thread": null, "changed": "",
            "sources": [{"url": "https://...", "title": "...", "publisher": "..."}]}],
 "skipped": [{"title": "...", "why": "..."}]}"""

ARTICLE_SHAPE = """{"kind": "event|earnings|research|macro|theme|review|ahead",
 "headline": "at most 90 characters", "dek": "one sentence, at most 200 characters",
 "importance": 1-5, "importance_why": "one sentence",
 "tickers": ["the most affected symbols, Yahoo style, at most 6"], "sectors": ["Technology", "Energy"],
 "takeaways": ["3 to 5 short points"],
 "sections": [{"h": "heading", "p": ["paragraph", "paragraph"]}],
 "chain": ["3 to 6 steps from the event to prices, each at most eight words"],
 "numbers": {"title": "...", "cols": ["", "Actual", "Expected", "A year ago"], "rows": [["Revenue", "$1.2bn", "$1.1bn", "$0.9bn"]], "note": "source and basis"} or null,
 "figures": [{"type": "price", "title": "...", "tickers": ["CL=F", "XOM", "^GSPC"], "days": 30, "mark": "YYYY-MM-DD", "rebase": true},
             {"type": "map", "title": "...", "countries": ["IR", "IL"], "points": [{"lat": 26.6, "lon": 56.3, "label": "Strait of Hormuz"}]},
             {"type": "timeline", "title": "...", "events": [{"d": "YYYY-MM-DD", "t": "what happened"}]},
             {"type": "bars", "title": "...", "unit": "%", "items": [{"k": "label", "v": 12.5}], "source": "where the numbers come from"}],
 "bull": ["the case for"], "bear": ["the case against"],
 "book": "what it means for the reader's holdings: which positions, which direction, how much of the book",
 "watch": [{"d": "YYYY-MM-DD or null", "t": "what to watch"}],
 "quote": {"text": "under fifteen words", "who": "who said it"} or null,
 "sources": [{"url": "https://...", "title": "...", "publisher": "...", "used": "what you took from it"}],
 "changed": "for an update of an earlier article: what changed since it; otherwise null"}"""

WRITE_SYSTEM = """You write one article for a private investor's own terminal. The reader holds the portfolio in the material (mostly US technology, AI and semiconductors, with European shares and some leveraged turbos) and wants the story made clear: what happened, why it moves prices, the numbers, the other side, what comes next, and what it means for the holdings.

How to work:
- Read the sources given and find better ones where needed: search the web, then fetch the pages, at most eight fetches. When a page is paywalled or blocked, find the same facts in another report; never try to get around a paywall.
- Write in your own words. Never copy a sentence. At most one direct quote of under fifteen words, attributed, in the "quote" field.
- Every number comes from a source you read or from the material; list each source you used. Where sources disagree, say so. Keep what is known apart from what is expected.
- The material's market lines carry the date they are from (government yields lag a day or two). Where a source you read has a later figure, use that one, and do not comment on the terminal's data in the article.
- Explain the mechanism: the chain from the event to prices, e.g. strikes on Iran, oil supply at risk, crude up 6%, inflation expectations up, yields up, long-duration tech down.
- Say plainly what it means for the reader's holdings in the material: which positions are exposed, in which direction, how much of the book that is. Describe exposure; do not tell the reader to buy or sell.
- Figures are drawn by the terminal from real data: ask for them, never invent a data series. "price" charts symbols over a number of days and marks the event's date; "map" places points and highlights countries (two-letter codes); "timeline" lists dated events; "bars" shows numbers you took from a source, which you name.
- Plain, precise English for an informed reader. No hype, no filler, no closing summary. About 600 to 1,000 words across the sections.

Sections by kind; adapt them and leave out what does not apply:
- event: What happened, Why markets care, The numbers so far, How it could go (base case, upside, downside), What to watch
- earnings: The quarter in one line, Why the stock moved, Guidance and the call, The debate, Valuation after the move
- research: The thesis, The evidence, What would prove it wrong, Who is exposed
- macro: The number, Why it matters for rates, What markets price now
- theme: What is changing, Who wins and who loses, The numbers, What to watch
- review (the week in review) and ahead (the week ahead): by day or by theme, the material's own data first

Reply with one JSON object in a ```json block and nothing after it, shaped like this:
""" + ARTICLE_SHAPE

ASK_SYSTEM = """You wrote the article below for a private investor's terminal. Answer the reader's question about it: directly, from the article's sources and what you know, saying where you are unsure. As long as it needs, no longer. Plain text, no JSON."""


# --------------------------------------------------------------------------
# running the model
# --------------------------------------------------------------------------

JOB: dict = {"state": "idle"}
PHASES = {"WebSearch": "searching the web", "WebFetch": "reading a source"}
ASK_JOBS: dict = {}
_LOCK = threading.Lock()


def _pub(j: Optional[dict]) -> Optional[dict]:
    if not j:
        return None
    return {k: j.get(k) for k in ("state", "phase", "slot", "step", "current", "started", "finished", "error",
                                  "written", "text") if k in j}


def _work_dir(key: str) -> str:
    import os
    w = os.path.join(os.path.dirname(os.path.abspath(store.DB_PATH)), "read_work", key)
    os.makedirs(w, exist_ok=True)
    return w


def writer_ready() -> Optional[str]:
    """None when the writer can run; otherwise why not, in words."""
    from . import earnai
    w = earnai.writer()
    if w == "api":
        return None if earnai.api_key() else "no Anthropic API key: add one in Settings, or switch the writer to Claude Code"
    st = earnai.cc_status()
    if not st.get("found"):
        return "Claude Code was not found on this computer"
    if not st.get("logged_in"):
        st = earnai.cc_status(force=True)
        if not st.get("logged_in"):
            return "Claude Code is not signed in: sign in once on the morning brief's page"
    return None


def run_ai(job: dict, system: str, text: str, web: str, key: str, resume: Optional[str] = None,
           max_uses: int = 5) -> dict:
    """
    One run of the writer: Claude Code on the plan (the default), or the API.
    `web` is "", "search" or "fetch" (search and fetch). The text, the model,
    the session to resume, the tokens and the cost come back.
    """
    from . import earnai
    if earnai.writer() == "claude-code":
        import os
        work = _work_dir(key)
        with open(os.path.join(work, "material.md"), "w", encoding="utf-8") as f:
            f.write(text)
        tools = {"": "", "search": "WebSearch", "fetch": "WebSearch,WebFetch"}[web]
        out = earnai.cc_stream(earnai.cc_command(tools, resume, system=system), text, work, job, phases=PHASES)
        return {"text": out["text"], "session": out["session"], "model": f"Claude Code · {out['model'] or 'default model'}",
                "usage": out["usage"], "api_usd": out["api_usd"], "cost_usd": None, "writer": "claude-code", "ms": out["ms"]}
    m = earnai.model()
    kw = dict(model=m, max_tokens=16000, system=system, messages=[{"role": "user", "content": text}],
              thinking={"type": "adaptive"}, output_config={"effort": earnai.WRITE_EFFORT})
    betas = []
    if m in earnai.FALLBACK_OK:
        betas.append("server-side-fallback-2026-07-01")
        kw["fallbacks"] = "default"
    if web:
        kw["tools"] = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_uses}]
        if web == "fetch":
            kw["tools"].append({"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8})
    client = earnai._client()
    usage, parts, served, t0 = {}, [], m, time.time()
    for _ in range(4):
        with client.beta.messages.stream(betas=betas, **kw) as stream:
            for ev in stream:
                if ev.type == "content_block_delta" and getattr(ev.delta, "type", "") == "text_delta":
                    job["text"] = job.get("text", "") + ev.delta.text
            msg = stream.get_final_message()
        served = getattr(msg, "model", m) or m
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            usage[k] = usage.get(k, 0) + (getattr(msg.usage, k, 0) or 0)
        if msg.stop_reason == "refusal":
            raise RuntimeError("the model declined this request")
        parts += [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        if msg.stop_reason == "pause_turn":
            kw["messages"] = kw["messages"] + [{"role": "assistant", "content": msg.content}]
            continue
        break
    return {"text": "\n".join(p for p in parts if p).strip(), "session": None, "model": served, "usage": usage,
            "api_usd": None, "cost_usd": earnai.cost(served if served in earnai.MODELS else m, usage),
            "writer": "api", "ms": int((time.time() - t0) * 1000)}


def run_json(job: dict, system: str, text: str, web: str, key: str) -> tuple[dict, dict]:
    """A run whose answer must be JSON; one correction in the same session when it is not."""
    out = run_ai(job, system, text, web, key)
    try:
        return parse_json(out["text"]), out
    except Exception as e:
        if not out.get("session"):
            raise ValueError(f"the reply was not valid JSON ({e})")
        job["phase"] = "correcting its JSON"
        fix = run_ai(job, system, f"Your reply was not valid JSON ({str(e)[:200]}). Reply with only the corrected "
                                  "JSON object in a ```json block.", "", key, resume=out["session"])
        return parse_json(fix["text"]), {**fix, "usage": out["usage"], "api_usd": out["api_usd"]}


def _material(slot: str, cands: list[dict], book: dict, threads: list[dict], budget: int, cal: dict) -> str:
    n = now_ams()
    ago = lambda ts: f"{max(0, (time.time() - ts) / 3600):.0f}h ago" if ts else ""
    lines = [f"- [{i}] {c['title']} ({c.get('source') or '?'}, {ago(c.get('ts'))}"
             + (f", tickers {', '.join(c['tickers'][:4])}" if c.get("tickers") else "") + f", via {c.get('via')})"
             + (f"\n    {c['summary'][:220]}" if c.get("summary") else "") + f"\n    {c['url']}"
             for i, c in enumerate(cands)]
    hold = [_hold_line(r) for r in book["rows"][:35]]
    return "\n".join([
        f"Now: {n:%A %d %B %Y, %H:%M} in Amsterdam ({SLOT_LABEL.get(slot, slot)}). Articles you may still pick today: {budget}.",
        "", "THE BOOK (EUR {:,}; cash {:,}; sectors by weight: {})".format(
            book["total"], book["cash"], ", ".join(f"{k} {v}%" for k, v in list(book["sectors"].items())[:8])),
        *hold, "", "MARKETS (the terminal's last reading, each with its date)", *_market_lines(),
        "", "CALENDAR: the holdings' reports and the main macro releases",
        *[f"{e['d']} {e['sym']} reports ({e.get('when') or 'time unknown'})" for e in cal["earnings"]],
        *[f"{e['d']} {e.get('t') or ''} {e['ccy']} {e['title']}: actual {e['actual']}, forecast {e['forecast']}, previous {e['previous']}"
          for e in cal["macro"]],
        "", "OPEN THREADS (written in the last week; pick again only when something material changed)",
        *([f"- thread {t['thread']}: {t['title']} (version {t['version']}, {t['updated'][:16]}) - {t['dek']}" for t in threads]
          or ["none"]),
        "", f"CANDIDATES ({len(cands)})", *lines])


def _ticker_facts(syms: list[str]) -> list[str]:
    from . import research
    out = []
    for s in syms[:5]:
        try:
            bars = research.daily(s)["bars"]
            c = [b[4] for b in bars]
            pct = lambda n: f"{(c[-1] / c[-1 - n] - 1) * 100:+.1f}%" if len(c) > n and c[-1 - n] else "?"
            out.append(f"{s}: last close {c[-1]:.2f} on {bars[-1][0]}; 1 day {pct(1)}, 5 days {pct(5)}, 1 month {pct(21)}, 1 year {pct(252)}")
        except Exception:
            continue
    return out


def _write_text(pick: dict, book: dict, prev: Optional[dict]) -> str:
    n = now_ams()
    mine = [r for r in book["rows"] if r["t"] in pick["tickers"] or r["t"].split(".")[0] in pick["tickers"]]
    parts = [
        f"Now: {n:%A %d %B %Y, %H:%M} in Amsterdam. Write the article on this pick from your editor:",
        json.dumps(pick, ensure_ascii=False, indent=1), "",
        "THE READER'S BOOK (EUR {:,}; sectors by weight: {})".format(
            book["total"], ", ".join(f"{k} {v}%" for k, v in list(book["sectors"].items())[:8])),
        *[_hold_line(r) for r in book["rows"][:30]],
        "", "Held among the pick's symbols: " + (", ".join(f"{r['t']} {r['w']}%" for r in mine) or "none directly"),
        "", "PRICES FROM THE TERMINAL", *(_ticker_facts(pick["tickers"]) or ["none"]),
        "", "MARKETS (the terminal's last reading, each with its date)", *_market_lines(28),
    ]
    if prev:
        parts += ["", f"THIS UPDATES AN EARLIER ARTICLE (version {prev['version']}, {prev['created'][:16]} UTC). Keep what still "
                      "holds, correct what changed, and say in 'changed' what is new since then:",
                  json.dumps({k: prev["body"].get(k) for k in ("headline", "dek", "takeaways", "sections", "watch")},
                             ensure_ascii=False)[:9000]]
    return "\n".join(parts)


def _edition_text(kind: str, book: dict) -> str:
    """The weekend editions' material: the week's markets and book, or next week's calendar."""
    from . import research
    n = now_ams()
    lines = [f"Now: {n:%A %d %B %Y} in Amsterdam. Write the weekend edition: "
             + ("THE WEEK IN REVIEW for markets and the reader's book." if kind == "review"
                else "THE WEEK AHEAD: what is on the calendar for markets and the reader's holdings, and what to watch."), ""]
    lines += ["MARKETS (the terminal's last reading, each with its date)", *_market_lines()]
    if kind == "review":
        moves = []
        for r in book["rows"][:20]:
            try:
                c = [b[4] for b in research.daily(r["t"])["bars"]]
                if len(c) > 5:
                    moves.append((r, (c[-1] / c[-6] - 1) * 100))
            except Exception:
                continue
        lines += ["", "THE BOOK'S LARGEST HOLDINGS THIS WEEK (weight, five-day move; turbos at their share's move)",
                  *[f"{r['t']} {r['name'][:40]}: {r['w']}% of the book, {m:+.1f}% this week" for r, m in
                    sorted(moves, key=lambda x: -abs(x[1] * (x[0]['w'] or 0)))]]
        week = _rows("SELECT headline, kind, importance, d FROM read_article WHERE d >= ? ORDER BY id",
                     ((n.date() - timedelta(days=7)).isoformat(),))
        lines += ["", "ARTICLES WRITTEN THIS WEEK", *[f"{a['d']} [{a['kind']}, {a['importance']}] {a['headline']}" for a in week]]
    else:
        from . import earnings, macro
        try:
            up = earnings.upcoming_mine(9)
        except Exception:
            up = []
        lines += ["", "THE HOLDINGS' REPORTS NEXT WEEK", *([f"{e['date']} {e['sym']} {e.get('name') or ''} ({e.get('when') or '?'})"
                                                            for e in up] or ["none found"])]
        try:
            rows = macro.rows(n.date().isoformat(), (n.date() + timedelta(days=8)).isoformat())
        except Exception:
            rows = []
        lines += ["", "MACRO RELEASES NEXT WEEK (the important ones)",
                  *[f"{r['d']} {r.get('t') or ''} {r['ccy']} {r['title']}: forecast {r.get('forecast')}, previous {r.get('previous')}"
                    for r in rows if (r.get("rank") or 0) >= 2][:40]]
    lines += ["", "THE BOOK BY WEIGHT", *[f"{r['t']} {r['w']}%" for r in book["rows"][:30]]]
    return "\n".join(lines)


def daily_cap() -> int:
    try:
        return max(0, int(store.get_setting("read_daily_max") or DAILY_MAX))
    except Exception:
        return DAILY_MAX


def _today_budget(d: str) -> int:
    return max(0, daily_cap() - written_on(d))


def _pick_key(d: str, slot: str, title: str) -> str:
    """The folder an article is written in. Claude Code keeps a session per folder, so Ask resumes there."""
    return f"{d}-{slot}-" + (re.sub(r"[^a-z0-9]+", "-", (title or "").lower())[:48].strip("-") or "pick")


def _write_one(job: dict, pick: dict, slot: str, d: str, book: dict) -> int:
    prev = None
    if pick.get("thread"):
        r = _rows("SELECT * FROM read_article WHERE story_id=? ORDER BY version DESC LIMIT 1", (pick["thread"],))
        if r:
            prev = {**r[0], "body": _j(r[0]["body"], {})}
    text = _write_text(pick, book, prev)
    key = _pick_key(d, slot, pick["title"])
    a, out = run_json(job, WRITE_SYSTEM, text, "fetch", key)
    art = clean_article(a, pick["kind"], pick["importance"])
    meta = {"writer": out["writer"], "model": out["model"], "session": out["session"], "usage": out["usage"],
            "api_equivalent_usd": out["api_usd"], "ms": out["ms"], "pick": pick, "work": key}
    return save_article(art, slot, d, meta, out.get("cost_usd"), pick.get("thread"))


def _run_scan(job: dict, slot: str, only: Optional[dict] = None, cap: Optional[int] = None):
    """Gather, triage, write. `only` writes one candidate you chose, without a triage."""
    d = now_ams().date().isoformat()
    sid = _scan_row(d, slot)
    written, picks, summary, skipped, metas = [], [], "", [], []
    try:
        why = writer_ready()
        if why:
            raise PermissionError(why)
        book = _book()
        budget = min(_today_budget(d), PER_SCAN.get(slot, 3) if cap is None else cap)
        if budget <= 0:
            raise RuntimeError(f"today's budget of {daily_cap()} article{'s' if daily_cap() != 1 else ''} is used")
        if slot in ("review", "ahead") and only is None:
            job.update(phase="writing the weekend edition", step=f"{KINDS[slot]}")
            a, out = run_json(job, WRITE_SYSTEM, _edition_text(slot, book), "fetch", f"{d}-{slot}")
            art = clean_article({**a, "kind": slot}, slot, 3)
            written.append(save_article(art, slot, d, {"writer": out["writer"], "model": out["model"],
                                                       "session": out["session"], "usage": out["usage"],
                                                       "api_equivalent_usd": out["api_usd"], "ms": out["ms"],
                                                       "work": f"{d}-{slot}"},
                                        out.get("cost_usd")))
            budget -= 1
        if only is not None:
            picks = [only]
        elif budget > 0:
            job.update(phase="reading the news", step="gathering")
            cands = gather(book=book)
            threads = open_threads()
            job.update(phase="choosing what to write", step="triage")
            t, out = run_json(job, TRIAGE_SYSTEM, _material(slot, cands, book, threads, budget, _calendar()),
                              "search", f"{d}-{slot}-triage")
            metas.append({"step": "triage", "model": out["model"], "usage": out["usage"],
                          "api_equivalent_usd": out["api_usd"], "ms": out["ms"], "candidates": len(cands)})
            tr = clean_triage(t, budget, threads)
            picks, summary, skipped = tr["picks"], tr["summary"], tr["skipped"]
        for i, p in enumerate(picks):
            job.update(phase="writing", step=f"article {i + 1} of {len(picks)}", current=p["title"], text="")
            try:
                written.append(_write_one(job, p, slot, d, book))
            except Exception as e:
                msg = f"{e.__class__.__name__}: {e}"
                skipped.append({"title": p["title"], "why": "could not be written: " + msg[:200]})
                if "usage limit" in msg.lower():
                    for q in picks[i + 1:]:
                        skipped.append({"title": q["title"], "why": "postponed: the plan's usage limit was reached"})
                    break
        job.update(state="done", phase="done", written=written)
        _scan_done(sid, finished=datetime.now(timezone.utc).isoformat(timespec="seconds"), state="done",
                   summary=summary, picks=[{k: p[k] for k in ("title", "kind", "importance", "why", "thread")} for p in picks],
                   skipped=skipped, n_written=len(written), meta={"runs": metas})
    except Exception as e:
        msg = f"{e.__class__.__name__}: {e}"[:300]
        job.update(state="error", error=msg, written=written)
        _scan_done(sid, finished=datetime.now(timezone.utc).isoformat(timespec="seconds"), state="error",
                   error=msg, n_written=len(written), summary=summary, skipped=skipped)
    finally:
        job["finished"] = time.time()


def start_scan(slot: str = "manual", only: Optional[dict] = None, cap: Optional[int] = None) -> dict:
    """Start a scan in the background; one at a time."""
    global JOB
    with _LOCK:
        if JOB.get("state") == "running":
            return _pub(JOB)
        JOB = {"state": "running", "phase": "starting", "slot": slot, "step": "", "current": "", "text": "",
               "started": time.time(), "finished": None, "error": None, "written": []}
        j = JOB
    threading.Thread(target=_run_scan, args=(j, slot, only, cap), daemon=True, name=f"read-{slot}").start()
    return _pub(j)


def write_this(title: str, url: Optional[str] = None, why: str = "") -> dict:
    """An article on a candidate the editor skipped, because you asked for it."""
    pick = {"kind": "event", "title": _s(title, 200), "importance": 3, "why": _s(why, 300) or "asked for by the reader",
            "tickers": [], "angle": "", "thread": None, "changed": "",
            "sources": [{"url": url, "title": title, "publisher": publisher(url)}] if _url(url) else []}
    return start_scan("manual", only=pick, cap=1)


# --------------------------------------------------------------------------
# asking about an article
# --------------------------------------------------------------------------

def answers(aid: int) -> list[dict]:
    out = _rows("SELECT id, question, created, body, meta FROM read_answer WHERE article_id=? ORDER BY id", (aid,))
    for a in out:
        a["meta"] = _j(a["meta"], {})
    return out


def _run_ask(job: dict, aid: int, question: str):
    try:
        a = article(aid, mark_seen=False, figures=False)
        if not a:
            raise LookupError("no such article")
        m = a.get("meta") or {}
        session = m.get("session")
        work = m.get("work") or (_pick_key(a["d"], a["slot"], (m.get("pick") or {}).get("title")) if m.get("pick") else f"ask-{aid}")
        out = None
        if session:
            try:
                out = run_ai(job, ASK_SYSTEM, f"The reader asks about your article:\n\n{question}", "", work,
                             resume=session)
            except RuntimeError as e:
                if "session" not in str(e).lower() and "conversation" not in str(e).lower():
                    raise
        if out is None:
            text = ("THE ARTICLE\n" + json.dumps(a["body"], ensure_ascii=False)[:20000]
                    + f"\n\nThe reader asks:\n\n{question}")
            out = run_ai(job, ASK_SYSTEM, text, "search", f"ask-{aid}")
        if not out["text"]:
            raise RuntimeError("no answer came back")
        with store.conn() as c:
            c.execute("INSERT INTO read_answer (article_id, question, created, body, meta) VALUES (?,?,?,?,?)",
                      (aid, question, datetime.now(timezone.utc).isoformat(timespec="seconds"), out["text"],
                       json.dumps({"model": out["model"], "usage": out["usage"], "api_equivalent_usd": out["api_usd"]})))
        job.update(state="done", phase="done")
    except Exception as e:
        job.update(state="error", error=f"{e.__class__.__name__}: {e}"[:300])
    finally:
        job["finished"] = time.time()


def ask(aid: int, question: str) -> dict:
    q = _s(question, 1000)
    if not q:
        raise ValueError("ask a question")
    why = writer_ready()
    if why:
        raise PermissionError(why)
    with _LOCK:
        j = ASK_JOBS.get(aid)
        if j and j["state"] == "running":
            return _pub(j)
        j = {"state": "running", "phase": "answering", "text": "", "started": time.time(), "finished": None, "error": None}
        ASK_JOBS[aid] = j
    threading.Thread(target=_run_ask, args=(j, aid, q), daemon=True, name=f"read-ask-{aid}").start()
    return _pub(j)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------

def _item(r: dict) -> dict:
    return {"id": r["id"], "story": r["story_id"], "version": r["version"], "kind": r["kind"],
            "kind_label": KINDS.get(r["kind"], r["kind"]), "importance": r["importance"], "headline": r["headline"],
            "dek": r["dek"], "tickers": _j(r["tickers"], []), "created": r["created"], "d": r["d"], "slot": r["slot"],
            "seen": bool(r.get("seen_at"))}


def listing(days: int = 7) -> list[dict]:
    """The newest version of each story of the last days, newest first."""
    cut = (now_ams().date() - timedelta(days=days)).isoformat()
    rows = _rows("SELECT a.*, s.seen_at FROM read_article a LEFT JOIN read_seen s ON s.article_id = a.id "
                 "WHERE a.d >= ? AND a.version = (SELECT MAX(version) FROM read_article b WHERE b.story_id = a.story_id) "
                 "ORDER BY a.created DESC, a.id DESC", (cut,))
    return [_item(r) for r in rows]


def book_block(a: dict) -> dict:
    """What the article touches in the book, from today's positions: never the model's guess."""
    try:
        b = _book()
    except Exception as e:
        return {"available": False, "why": str(e)[:200]}
    tick = set(a.get("tickers") or [])
    rows = [r for r in b["rows"] if r["t"] in tick or r["t"].split(".")[0] in tick]
    secs = {s: b["sectors"].get(s) for s in a.get("sectors") or [] if b["sectors"].get(s)}
    return {"available": True, "rows": rows, "weight": round(sum(r["w"] or 0 for r in rows), 2),
            "value": sum(r["value"] for r in rows), "exposure": sum(r["exposure"] for r in rows),
            "sectors": secs, "total": b["total"]}


def figure_data(f: dict) -> dict:
    """The data behind a price figure: daily closes, rebased to 100 when there are several."""
    if f.get("type") != "price":
        return f
    from . import research
    ser, missing = [], []
    for s in f["tickers"]:
        try:
            bars = research.daily(s)["bars"][-f["days"]:]
            if len(bars) < 3:
                raise ValueError
            c0 = bars[0][4]
            pts = [[b[0], round(b[4] / c0 * 100, 2) if f["rebase"] else b[4]] for b in bars]
            ser.append({"sym": s, "pts": pts, "chg": round((bars[-1][4] / c0 - 1) * 100, 2)})
        except Exception:
            missing.append(s)
    return {**f, "series": ser, "missing": missing}


def article(aid: int, mark_seen: bool = True, figures: bool = True) -> Optional[dict]:
    r = _rows("SELECT * FROM read_article WHERE id=?", (aid,))
    if not r:
        return None
    r = r[0]
    body = _j(r["body"], {})
    out = {**_item({**r, "seen_at": None}), "body": body, "meta": _j(r["meta"], {}), "cost_usd": r["cost_usd"],
           "versions": [{"id": v["id"], "version": v["version"], "created": v["created"], "headline": v["headline"],
                         "changed": (_j(v["body"], {}) or {}).get("changed")}
                        for v in _rows("SELECT id, version, created, headline, body FROM read_article WHERE story_id=? "
                                       "ORDER BY version", (r["story_id"],))]}
    out["latest"] = max(v["id"] for v in out["versions"]) if out["versions"] else aid
    if figures:
        out["figures"] = [figure_data(f) for f in body.get("figures") or []]
        out["book"] = book_block(body)
        out["answers"] = answers(aid)
        out["ask_job"] = _pub(ASK_JOBS.get(aid))
    m = out["meta"]
    out["meta"] = {k: m.get(k) for k in ("writer", "model", "ms", "api_equivalent_usd", "session", "work", "pick")}
    if mark_seen:
        with store.conn() as c:
            c.execute("INSERT OR IGNORE INTO read_seen (article_id, seen_at) VALUES (?, ?)",
                      (aid, datetime.now(timezone.utc).isoformat(timespec="seconds")))
    return out


def home() -> dict:
    """For the home screen: the live coverage and the newest articles, nothing slow."""
    d = now_ams().date().isoformat()
    return {"live": live(), "articles": listing(3)[:4], "today": {"d": d, "written": written_on(d)}}


def next_slot(now: Optional[datetime] = None) -> Optional[dict]:
    n = (now or now_ams()).astimezone(AMS)
    for k in range(0, 8):
        day = n.date() + timedelta(days=k)
        slots = [WEEKEND[day.weekday()]] if day.weekday() >= 5 else list(SLOTS.items())
        for name, (h, m) in slots:
            at = datetime.combine(day, dtime(h, m), AMS)
            if at > n:
                return {"slot": name, "label": SLOT_LABEL[name], "at": at.isoformat(timespec="minutes")}
    return None


def overview() -> dict:
    d = now_ams().date().isoformat()
    sc = scans(d)
    last = sc[-1] if sc else (_rows("SELECT * FROM read_scan ORDER BY id DESC LIMIT 1") or [None])[0]
    # The last scan of an earlier day is read raw: its lists are JSON text, or NULL where it failed
    # before picking (picks NULL, skipped "[]" reached the page as a string, and the page stopped).
    if last and not sc:
        for k in ("picks", "skipped", "meta"):
            last[k] = _j(last.get(k), [] if k != "meta" else {})
    return {"live": live(), "articles": listing(), "today": {"d": d, "written": written_on(d), "max": daily_cap()},
            "last_scan": last, "next": next_slot(), "job": _pub(JOB), "auto": store.get_setting("read_auto") != "0",
            "ready": writer_ready()}


# --------------------------------------------------------------------------
# the schedule
# --------------------------------------------------------------------------

def due_slot(now: Optional[datetime] = None, done: Optional[set] = None) -> Optional[str]:
    """
    The slot to run now: the latest one whose time has passed today, not yet
    run, and missed by no more than CATCH_UP_H (a laptop that was off all day
    does not run three scans at once when it wakes).
    """
    n = (now or now_ams()).astimezone(AMS)
    slots = [WEEKEND[n.weekday()]] if n.weekday() >= 5 else list(SLOTS.items())
    passed = [(k, hm) for k, hm in slots if (n.hour, n.minute) >= hm]
    if not passed:
        return None
    k, (h, m) = passed[-1]
    if done is None:
        done = {s["slot"] for s in scans(n.date().isoformat())}
    if k in done:
        return None
    if n - datetime.combine(n.date(), dtime(h, m), AMS) > timedelta(hours=CATCH_UP_H):
        return None
    return k


_THREAD: dict = {"t": None}


def _tick():
    if store.get_setting("read_auto") == "0":
        return
    k = due_slot()
    if k and JOB.get("state") != "running":
        start_scan(k)


def _loop():
    time.sleep(90)
    while True:
        try:
            _tick()
        except Exception:
            pass
        time.sleep(60)


def ensure():
    """The scheduler, started by the server only: every run spends the plan."""
    t = _THREAD["t"]
    if t and t.is_alive():
        return
    store.init_db()
    t = threading.Thread(target=_loop, daemon=True, name="read-scheduler")
    _THREAD["t"] = t
    t.start()


# --------------------------------------------------------------------------
# the morning brief and the alerts
# --------------------------------------------------------------------------

def since(ts_iso: str) -> list[dict]:
    """Stories written or updated since a moment, each once, at its latest version."""
    rows = _rows("SELECT * FROM read_article a WHERE created >= ? AND version = (SELECT MAX(version) FROM read_article b "
                 "WHERE b.story_id = a.story_id) ORDER BY importance DESC, created DESC, id DESC", (ts_iso,))
    return [_item({**r, "seen_at": None}) for r in rows]


def brief_line(hours: int = 18) -> Optional[str]:
    cut = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).isoformat(timespec="seconds")
    arts = since(cut)
    if not arts:
        return None
    return (f"{len(arts)} article{'s' if len(arts) != 1 else ''} on READ: "
            + "; ".join(f"{a['headline']} ({a['kind_label'].lower()}, {a['importance']})" for a in arts[:5]))


def alerts(hours: int = 24) -> list[dict]:
    cut = datetime.fromtimestamp(time.time() - hours * 3600, timezone.utc).isoformat(timespec="seconds")
    return [{"level": "warn", "code": "READ", "text": a["headline"], "detail": (a["dek"] or "") + " · READ",
             "href": f"/m/READ#a={a['id']}"} for a in since(cut) if a["importance"] >= 5][:3]
