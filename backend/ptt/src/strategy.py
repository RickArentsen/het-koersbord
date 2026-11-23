"""The tracked strategy: every in-remit buy, from every member.

One filter, and only one.  A purchase counts if the ticker's industry falls
under a committee the member actually sits on.  Nobody is pre-selected.

Why the member filter was removed
---------------------------------
Until this was tested, the strategy carried a second gate: a member contributed
only once their own in-remit record had turned positive, judged point-in-time so
the choice could have been made in real time.  ``src/selection.py`` scored that
gate against twenty other candidate rules on identical data, and it does not
work.

Tested like for like - restricted to members who already had at least five
settled in-remit decisions, so both sides are the same people over the same
period, and matched within entry quarter so neither side is credited with the
calendar - trades that **passed** the gate returned **-1.16%** against those
that failed it, at **t = -1.34**, with 47% of quarters favouring the gate.  That
is a coin flip.  For that the gate discarded three quarters of the evidence:
2,286 in-remit decisions became 532, and 45 live members became 11.

Its apparent value came entirely from position sizing.  Equal-weighted - one
share of attention per name, which is what a reader can actually replicate - the
gated rule returned **+14.92%** a year against SPY's **+15.72%**.  It did not
beat the index.  Dropping the gate gives **+21.63%** equal-weighted and
**+22.04%** size-weighted; the two agree, which is what an edge that does not
depend on the weighting choice looks like.

What is left, and how much of it is real
----------------------------------------
The committee filter itself does hold up.  In-remit buys average **+6.93%**
twelve-month alpha per decision at **t = +5.03** over 2,844 decisions, the sign
holds in both halves of a split sample (+6.00% training edge, +6.27% test edge
over an unfiltered baseline), and it survives dropping any single member.

One limit is stated rather than hidden: matched within the same sector *and*
quarter the edge falls to **+4.54% at t = +1.54**, so a good part of it is the
sectors committees happen to cover rather than the committee link itself.

The other limit has been **fixed**.  Committee membership used to be read from
the current roster, so a 2021 trade was judged against the seat its member holds
today.  Quarterly snapshots reconstructed from the source repository's git
history now give every trade the remit that applied when it was made, and the
correction *strengthened* the signal rather than deflating it: +5.25% at
t = +3.25 became **+6.93% at t = +5.03**, with a split-sample edge of +6.00%
training against +6.27% test where it previously decayed from +6.91% to +2.78%.

A note on what this rule no longer needs
----------------------------------------
Selecting members on their record required a walk-forward apparatus to keep the
choice honest, because picking people *because* their trades worked and then
reporting what those trades returned is circular.  Selecting no members at all
has no such bias to defend against: the rule uses a committee roster and a
sector map, neither of which is an outcome.  The machinery is kept only to
report what the discarded gate would have done, on :func:`compare`.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import statistics
import time
from collections import defaultdict

from prices import BENCHMARK, PriceLookup, normalize_ticker

# Only used by the retired member gate, which :func:`compare` still reports so
# the site can show what dropping it cost.  The live rule selects no members.
MIN_CLOSED_TRADES = 5
# Holding period for both the qualification test and the strategy itself.
HOLD_DAYS = 365
# The headline weighting.  Equal, because the size-weighted book put 26% into a
# single name and 94% into two members - not something a reader can replicate -
# and because under this rule the two weightings agree anyway.
HEADLINE_WEIGHT = "equal"


def _shift(date_iso: str, days: int) -> str:
    return (dt.date.fromisoformat(date_iso) + dt.timedelta(days=days)).isoformat()


def decision_key(trade) -> tuple:
    """The identity of one trading *decision*, however many lines it was filed on.

    A member who buys the same stock on the same day across several accounts
    files several rows.  Sullivan's best day is eight tickers bought at once in
    a dependent child's account, disclosed three times over - 24 of his 30
    in-remit rows come from a single morning.  Counting rows would let one
    lucky day stand in for a track record and clear
    :data:`MIN_CLOSED_TRADES` on its own.

    Position *sizes* are still summed across those rows, because the duplication
    is real money - it is only not real repetition.
    """
    return (trade["bioguide_id"], trade["ticker"], trade["tx_date"])


def line_key(trade) -> str:
    """The identity of one *filed line*, stable across re-ingestion.

    ``transactions.id`` is not usable for this and must never be stored across
    runs.  Re-parsing a filing deletes and re-inserts its rows - see the comment
    at ``ingest.py:72`` - so ``AUTOINCREMENT`` mints fresh ids for trades that
    have not changed at all.  Measured on the 2026-08-12 scan: three genuinely
    new filings, and **8,745 rows came back with new ids**, with trade dates
    reaching back to 2018.

    Anything that has to remember "this line has already been handled" from one
    run to the next - the digest's ``sent_items``, the daily scan's seen-set -
    keys on this instead.  ``(doc_id, seq)`` is the filing document and the row's
    position within it, which is what the schema already declares as
    ``UNIQUE(doc_id, seq)``, and it changes only if the Clerk republishes the
    document with different contents.
    """
    return f"{trade['doc_id']}:{trade['seq']}"


def in_remit_trades(conn: sqlite3.Connection) -> list[dict]:
    """Every scored purchase that falls inside the buyer's own committee remit."""
    import congress

    ticker_sectors = {
        r["ticker"]: {s for s in (r["sectors"] or "").split(",") if s}
        for r in conn.execute("SELECT ticker, sectors FROM ticker_meta")
    }

    snapshots = congress.snapshot_dates(conn)

    rows = conn.execute(
        """SELECT t.id, t.doc_id, t.seq, t.bioguide_id, t.member_name, t.ticker, t.asset_type,
                  t.tx_type, COALESCE(t.tx_date_fixed, t.tx_date) AS tx_date, t.tx_date AS tx_date_filed, t.tx_date_note, t.filing_date, t.amount_raw, t.amount_mid,
                  t.owner, t.asset_name, f.pdf_url,
                  p.entry_date, p.ret_12m, p.bench_12m
             FROM transactions t
             JOIN trade_performance p ON p.transaction_id = t.id
        LEFT JOIN filings f ON f.doc_id = t.doc_id
            WHERE t.tx_type = 'purchase' AND t.bioguide_id IS NOT NULL
              AND t.ticker IS NOT NULL AND p.entry_date IS NOT NULL
         ORDER BY p.entry_date"""
    ).fetchall()

    # Remit is judged against the seat held *when the trade was made*, not the
    # one held today. Keyed on the snapshot rather than the exact date so the
    # cache stays small - every trade in a quarter shares one answer.
    cache: dict[tuple, set[str]] = {}
    out = []
    for r in rows:
        bg = r["bioguide_id"]
        snap = congress.snapshot_at(snapshots, r["tx_date"])
        key = (bg, snap)
        if key not in cache:
            _s, _n, sectors = congress.member_access_score(conn, bg, as_of=r["tx_date"])
            cache[key] = set(sectors)
        overlap = sorted(cache[key] & ticker_sectors.get(r["ticker"] or "", set()))
        if not overlap:
            continue
        d = dict(r)
        d["overlap"] = overlap
        d["alpha_12m"] = (r["ret_12m"] - r["bench_12m"]
                          if r["ret_12m"] is not None and r["bench_12m"] is not None
                          else None)
        out.append(d)
    return out


def walk_forward_selection(trades: list[dict],
                           min_closed: int = MIN_CLOSED_TRADES) -> list[dict]:
    """Tag each trade with whether its member had already proven themselves.

    A trade is ``qualified`` when the member's earlier in-remit *decisions* —
    only those whose holding period had finished before this one was disclosed —
    numbered at least ``min_closed`` and averaged positive alpha.

    The evidence counter is per decision, not per filed line; see
    :func:`decision_key`.  Every row still trades, so a decision split across
    four accounts still enters the book at four times the size.
    """
    history: dict[str, list[tuple[str, float]]] = defaultdict(list)
    counted: set[tuple] = set()
    for t in trades:
        bg = t["bioguide_id"]
        settled = [a for close_date, a in history[bg] if close_date <= t["entry_date"]]
        t["n_prior"] = len(settled)
        t["prior_alpha"] = statistics.fmean(settled) if settled else None
        t["qualified"] = (len(settled) >= min_closed
                          and statistics.fmean(settled) > 0)
        key = decision_key(t)
        if t["alpha_12m"] is not None and key not in counted:
            counted.add(key)
            history[bg].append((_shift(t["entry_date"], HOLD_DAYS), t["alpha_12m"]))
    return trades


# Below this many settled in-remit decisions a member's record is displayed as
# "too few to judge" rather than as a number.  It is a *labelling* threshold,
# not a membership one - see :func:`tracked_members`.
MIN_JUDGEABLE = 10


def tracked_members(conn: sqlite3.Connection, min_trades: int = 1) -> list[dict]:
    """Every member who trades inside their own committee's jurisdiction.

    Two filters used to live here and both were wrong.

    The first kept only members whose in-remit record was *positive*, which made
    the roster a leaderboard of past winners and, worse, made it the basis of the
    strategy. Testing showed that gate added nothing (see the module docstring).

    The second was subtler and survived longer: a ``min_trades`` of ten, which
    hid 43 members who *are* in the strategy - their in-remit buys enter the book
    - purely because they had too few of them to rank. Tim Moore has 174
    disclosed buys and 2 inside his Financial Services remit; the strategy trades
    those 2, so the page must show him. A display threshold that silently
    disagrees with the strategy is worse than no threshold.

    So everyone with in-remit activity appears, and :data:`MIN_JUDGEABLE` decides
    only whether their record is shown as a figure or as "too few to judge".
    Ordered by how much in-remit activity there is to watch, not by how well it
    went, so the ordering cannot be read as a recommendation.
    """
    rows = conn.execute(
        """SELECT s.*, l.party, l.state, l.district, l.chamber,
                  COALESCE(l.is_former, 0) AS is_former, l.last_term_end
             FROM politician_stats s
        LEFT JOIN legislators l ON l.bioguide_id = s.bioguide_id
            WHERE s.n_in_remit >= ?
         ORDER BY s.n_in_remit DESC, s.alpha_in_remit_12m DESC""",
        (min_trades,)).fetchall()
    return [dict(r) | {"judgeable": (r["n_in_remit"] or 0) >= MIN_JUDGEABLE}
            for r in rows]


def former_members(conn: sqlite3.Connection, min_buys: int = 10) -> list[dict]:
    """Members who have left Congress, listed but deliberately not traded.

    They are here because leaving office used to erase them: the roster source
    lists only sitting members, so 10,377 transactions - a quarter of the
    database - belonged to nobody. Marjorie Taylor Greene's 560 disclosures were
    among them, and she was one of the better traders in the period.

    They cannot enter the strategy, and the reason is structural rather than a
    choice. "In remit" needs a committee jurisdiction; no public source records
    committee membership for people who have left, so their remit is empty and
    every trade they made scores as out-of-remit. Listing them without that
    caveat would imply the strategy had passed judgement on them, and it has
    not - it cannot see them at all.
    """
    rows = conn.execute(
        """SELECT s.*, l.party, l.state, l.district, l.chamber, l.last_term_end
             FROM politician_stats s
             JOIN legislators l ON l.bioguide_id = s.bioguide_id
            WHERE l.is_former = 1 AND s.n_buys >= ?
         ORDER BY s.n_buys DESC""", (min_buys,)).fetchall()
    return [dict(r) for r in rows]


def _max_drawdown(series: list[float]) -> tuple[float | None, int | None]:
    """Worst peak-to-trough fall, and the index it bottomed at."""
    if not series:
        return None, None
    peak, worst, at = series[0], 0.0, 0
    for i, v in enumerate(series):
        peak = max(peak, v)
        if peak > 0:
            dd = v / peak - 1
            if dd < worst:
                worst, at = dd, i
    return worst, at


def open_positions(trades: list[dict], as_of: str | None = None,
                   hold_days: int = HOLD_DAYS) -> list[dict]:
    """The qualified trades whose holding period has not expired yet.

    This is what a reader who starts today would actually be holding, which the
    track record alone never tells them. A trade entered 400 days ago is history;
    one entered last week is a position.
    """
    today = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    out = []
    for t in trades:
        if not t.get("qualified") or not t.get("entry_date"):
            continue
        held = (today - dt.date.fromisoformat(t["entry_date"])).days
        if 0 <= held <= hold_days:
            d = dict(t)
            d["days_held"] = held
            d["days_left"] = hold_days - held
            d["exit_date"] = _shift(t["entry_date"], hold_days)
            out.append(d)
    return out


def recent_changes(trades: list[dict], days: int = 7, as_of: str | None = None,
                   hold_days: int = HOLD_DAYS) -> dict:
    """What the book did this week: positions opened, and positions timed out.

    The live basket answers "what am I holding" but not "what moved", and those
    are different questions for anyone already following along. An exit here is
    never a member selling - the strategy ignores that, and following members out
    was measured as costing 3.91 points - it is the twelve-month timer expiring,
    which is the only thing that ever closes a position.

    Entries are grouped by ticker, because one decision filed across four
    accounts is one thing to buy, not four.
    """
    today = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    since = (today - dt.timedelta(days=days)).isoformat()

    entries: dict[str, dict] = {}
    exits: dict[str, dict] = {}
    for t in trades:
        entry = t.get("entry_date")
        if not t.get("qualified") or not entry:
            continue
        member = (t.get("member_name") or "").replace("Hon. ", "")

        if since <= entry <= today.isoformat():
            e = entries.setdefault(t["ticker"], {
                "ticker": t["ticker"], "date": entry, "members": set(),
                "size": 0.0, "n": 0})
            e["members"].add(member)
            e["size"] += float(t.get("amount_mid") or 0)
            e["n"] += 1
            e["date"] = min(e["date"], entry)

        closed = _shift(entry, hold_days)
        if since <= closed <= today.isoformat():
            x = exits.setdefault(t["ticker"], {
                "ticker": t["ticker"], "date": closed, "members": set(),
                "entry_date": entry, "n": 0})
            x["members"].add(member)
            x["n"] += 1
            x["date"] = min(x["date"], closed)

    def tidy(d: dict) -> list[dict]:
        out = [{**v, "members": sorted(v["members"])} for v in d.values()]
        out.sort(key=lambda r: (r["date"], r["ticker"]), reverse=True)
        return out

    # A ticker can appear on both sides at once - an old lot timing out while a
    # fresh one opens - and that is worth seeing rather than collapsing.
    return {"days": days, "since": since, "entries": tidy(entries),
            "exits": tidy(exits),
            "quiet": not entries and not exits}


def basket(positions: list[dict]) -> dict:
    """Collapse open positions into a portfolio you could actually buy.

    Reported both size-weighted (what the strategy backtests) and equal-weighted
    per ticker, because the size weighting concentrates hard - one member's
    disclosed millions can be a quarter of the book - and an equal-weighted
    version of the same names is a defensible thing for a reader to prefer.
    """
    from collections import defaultdict as _dd

    by_ticker: dict[str, dict] = {}
    by_member: dict[str, float] = _dd(float)
    for p in positions:
        tk = p["ticker"]
        e = by_ticker.setdefault(tk, {"ticker": tk, "size": 0.0, "n": 0,
                                      "members": set(), "first_entry": p["entry_date"],
                                      "last_entry": p["entry_date"], "days_left": 0})
        e["size"] += float(p.get("amount_mid") or 0)
        e["n"] += 1
        e["members"].add(p["member_name"].replace("Hon. ", ""))
        e["first_entry"] = min(e["first_entry"], p["entry_date"])
        e["last_entry"] = max(e["last_entry"], p["entry_date"])
        # The freshest entry decides how long the name stays in the book: a
        # top-up extends the holding even though an earlier lot is nearly done.
        e["days_left"] = max(e["days_left"], int(p.get("days_left") or 0))
        by_member[p["member_name"].replace("Hon. ", "")] += float(p.get("amount_mid") or 0)

    total = sum(e["size"] for e in by_ticker.values()) or 1.0
    n_tickers = len(by_ticker) or 1
    rows = []
    for e in by_ticker.values():
        rows.append({**e, "members": sorted(e["members"]),
                     "size_weight": e["size"] / total,
                     "equal_weight": 1.0 / n_tickers})
    rows.sort(key=lambda r: -r["size_weight"])

    members = [{"name": m, "weight": v / total}
               for m, v in sorted(by_member.items(), key=lambda kv: -kv[1])]
    top2 = sum(m["weight"] for m in members[:2])
    return {
        "positions": rows, "members": members,
        "n_rows": len(positions), "n_tickers": len(by_ticker),
        "n_members": len(by_member), "volume": total,
        # The single most important caveat for a new reader.
        "top2_share": top2,
    }


def entry_pace(trades: list[dict], days: int = 365,
               as_of: str | None = None) -> dict:
    """How often a new position actually arrives, so nobody waits in the dark."""
    today = dt.date.fromisoformat(as_of) if as_of else dt.date.today()
    days_set = sorted({t["entry_date"] for t in trades
                       if t.get("qualified") and t.get("entry_date")
                       and 0 <= (today - dt.date.fromisoformat(t["entry_date"])).days <= days})
    last = days_set[-1] if days_set else None
    return {
        "n_entry_days": len(days_set),
        "avg_gap_days": (days / len(days_set)) if days_set else None,
        "last_entry": last,
        "days_since_last": (today - dt.date.fromisoformat(last)).days if last else None,
    }


def curve(conn: sqlite3.Connection, lookup: PriceLookup, trades: list[dict],
          weight: str = "size", only_qualified: bool = True,
          window: tuple[str, str] | None = None) -> dict:
    """Equity curve for holding the selected trades, against SPY.

    Positions open at the first market open after the filing was public and are
    held for twelve months; on any day the portfolio is the basket of open
    positions, weighted equally or by disclosed size.  With nothing open it
    sits in cash, which is itself part of the result.

    ``window`` pins the curve to a fixed date range instead of starting at the
    first selected trade.  Without it two selection rules cannot be compared:
    a rule whose first pick lands in 2022 would be measured over a different
    market than one that starts in 2020, and the CAGR gap would be calendar
    luck rather than skill.
    """
    import bisect

    picks = [t for t in trades if (t["qualified"] or not only_qualified)]
    if not picks:
        return {"dates": [], "strategy": [], "benchmark": [], "n_trades": 0}

    daily: dict[str, dict[str, float]] = {}
    for ticker in {normalize_ticker(t["ticker"]) for t in picks} | {BENCHMARK}:
        series = lookup.series.get(ticker)
        if not series:
            continue
        rets = {}
        for i in range(1, len(series)):
            prev, cur = series[i - 1][1], series[i][1]
            if prev and prev > 0:
                rets[series[i][0]] = (cur - prev) / prev
        daily[ticker] = rets

    bench_dates = sorted(daily.get(BENCHMARK, {}))
    if not bench_dates:
        return {"dates": [], "strategy": [], "benchmark": [], "n_trades": 0}

    held: dict[str, list[tuple[str, float]]] = defaultdict(list)
    used = 0
    for t in picks:
        ticker = normalize_ticker(t["ticker"])
        if ticker not in daily:
            continue
        w = float(t["amount_mid"] or 0) if weight == "size" else 1.0
        if w <= 0:
            continue
        used += 1
        start, end = t["entry_date"], _shift(t["entry_date"], HOLD_DAYS)
        lo = bisect.bisect_right(bench_dates, start)
        hi = bisect.bisect_right(bench_dates, end)
        for d in bench_dates[lo:hi]:
            held[d].append((ticker, w))

    start_date = window[0] if window else min(t["entry_date"] for t in picks)
    end_date = window[1] if window else None
    dates, strat, bench = [], [], []
    equity = bench_equity = 1.0
    days_invested = 0
    for d in bench_dates:
        if d < start_date or (end_date and d > end_date):
            continue
        names = held.get(d, [])
        day_ret = 0.0
        if names:
            total_w = sum(w for _n, w in names)
            if total_w > 0:
                day_ret = sum(daily[n].get(d, 0.0) * w for n, w in names) / total_w
                days_invested += 1
        equity *= (1 + day_ret)
        bench_equity *= (1 + daily[BENCHMARK].get(d, 0.0))
        dates.append(d)
        strat.append(round(equity, 6))
        bench.append(round(bench_equity, 6))

    years = len(dates) / 252 if dates else 0
    mdd, mdd_i = _max_drawdown(strat)
    mdd_date = dates[mdd_i] if mdd_i is not None and dates else None
    bench_mdd, _bi = _max_drawdown(bench)
    return {
        "dates": dates, "strategy": strat, "benchmark": bench,
        "n_trades": used, "weight": weight,
        # A CAGR without its drawdown describes half the experience. Anyone
        # deciding whether to follow this needs to know it fell 37% once.
        "max_drawdown": mdd, "max_drawdown_date": mdd_date,
        "bench_max_drawdown": bench_mdd,
        "final_strategy": strat[-1] if strat else 1.0,
        "final_benchmark": bench[-1] if bench else 1.0,
        "cagr": ((strat[-1] ** (1 / years) - 1) if years > 0.5 and strat else None),
        "bench_cagr": ((bench[-1] ** (1 / years) - 1) if years > 0.5 and bench else None),
        "exposure": (days_invested / len(dates)) if dates else 0,
        # Any annualised figure is meaningless without the window it covers, so
        # ship the window with it rather than making the reader ask.
        "start": dates[0] if dates else None,
        "end": dates[-1] if dates else None,
        "years": round(years, 2),
        "trading_days": len(dates),
    }


def compare(conn: sqlite3.Connection, lookup: PriceLookup,
            trades: list[dict]) -> dict:
    """The live rule, both weightings, beside the member gate it replaced.

    All three curves are pinned to the same window.  Without that the gated
    curve would start at its first *qualifying* trade - roughly a year later,
    because a member cannot qualify until five of their decisions have settled -
    and it would be scored against a different market than the rule it is being
    compared with.  That is the difference between a comparison and a coincidence.
    """
    window = (min(t["entry_date"] for t in trades),
              dt.date.today().isoformat()) if trades else None

    live = [dict(t, qualified=True) for t in trades]
    equal = curve(conn, lookup, live, weight="equal", window=window)
    size = curve(conn, lookup, live, weight="size", window=window)

    # What the retired gate would have picked, on the same trades and the same
    # calendar.  Kept so the site can show the cost of a filter it removed
    # rather than quietly dropping the evidence along with the rule.
    gated_trades = walk_forward_selection([dict(t) for t in trades])
    gated = curve(conn, lookup, gated_trades, weight="equal",
                  only_qualified=True, window=window)
    gated_size = curve(conn, lookup, gated_trades, weight="size",
                       only_qualified=True, window=window)

    return {"strategy": equal, "strategy_size": size,
            "member_gated": gated, "member_gated_size": gated_size}


CACHE_KEY = "strategy_v1"


def build(conn: sqlite3.Connection, min_member_trades: int = 1) -> dict:
    """Compute everything the site needs and cache it in the database.

    The curve costs a price-table load, which is far too slow to repeat per
    request, so it is built once here and the site reads the result.
    """
    import json

    import db as db_mod

    # Every in-remit buy qualifies. The rule selects trades, not people, so
    # there is no gate to apply here and no selection bias to correct for.
    trades = [dict(t, qualified=True) for t in in_remit_trades(conn)]
    lookup = PriceLookup(conn, tickers=[t["ticker"] for t in trades])
    comparison = compare(conn, lookup, trades)

    positions = open_positions(trades)

    payload = {
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
        "summary": summary(trades),
        "curves": comparison,
        "members": tracked_members(conn, min_member_trades),
        "min_closed_trades": MIN_CLOSED_TRADES,
        "min_member_trades": min_member_trades,
        "min_judgeable": MIN_JUDGEABLE,
        "headline_weight": HEADLINE_WEIGHT,
        # What a reader starting today would hold, and how the same names behave
        # under the size tilt the headline deliberately does not use.
        "basket": basket(positions),
        "size_weighted": {k: v for k, v in comparison["strategy_size"].items()
                          if k not in ("dates", "strategy", "benchmark")},
        "pace": entry_pace(trades),
        # What moved this week, for the page that answers "what do I do".
        "changes": recent_changes(trades),
        "hold_days": HOLD_DAYS,
    }
    db_mod.set_meta(conn, CACHE_KEY, json.dumps(payload, default=str))
    conn.commit()
    return payload


def load(conn: sqlite3.Connection) -> dict | None:
    """Read the cached strategy payload, or None if it was never built."""
    import json

    import db as db_mod

    raw = db_mod.get_meta(conn, CACHE_KEY)
    return json.loads(raw) if raw else None


_ACTIVITY_CACHE: dict[tuple, list[dict]] = {}
_ACTIVITY_IDENT: tuple | None = None


def _db_identity(conn: sqlite3.Connection) -> tuple:
    """Size and modification time of the main database file.

    The web process is long-lived and the database underneath it is replaced
    wholesale by the nightly pull, which renames a new file over the old one
    and never touches the site. Keying on the file itself is what lets a cache
    survive across requests without surviving across deployments.
    """
    import os

    try:
        row = conn.execute("PRAGMA database_list").fetchone()
        st = os.stat(row[2])
    except (sqlite3.Error, OSError, TypeError, IndexError):
        # In-memory database, or a file being renamed underneath us. Return
        # something that never repeats, so callers fall through to a real read
        # rather than trusting whatever was cached last.
        return ("unknown", time.time())
    return (st.st_mtime_ns, st.st_size)


def clear_activity_cache() -> None:
    """Drop the cached activity. For tests, and for anything that writes."""
    global _ACTIVITY_IDENT
    _ACTIVITY_CACHE.clear()
    _ACTIVITY_IDENT = None


def tracked_activity(conn: sqlite3.Connection, members: list[str],
                     sectors_by_member: dict[str, set[str]],
                     limit: int = 400, order: str = "traded") -> list[dict]:
    """Recent buys *and* sells by the tracked members, flagged for remit.

    Both sides are listed because both are news, but only one of them is the
    strategy, and each row says which. Over half these rows are sales, and the
    strategy does not act on a single one of them - see ``in_strategy`` below and
    :func:`selection.exit_rules` for the measurement that settled it.

    ``order`` picks which date "recent" means, and the two are not
    interchangeable:

    ``"traded"``
        When the member dealt. The right axis for studying behaviour.
    ``"filed"``
        When the trade became public - the only thing a reader can act on, and
        the right axis for anything labelled *latest*. Members have up to 45
        days to disclose and many use most of them, so a slow filer's fresh news
        carries an old trade date. Gottheimer disclosed on 2026-08-10 trades
        dated 2026-07-14 to 07-24; ordered by trade date that filing sat below
        three-week-old entries and the landing page reported no filings in a
        week, two days after his arrived.

    Sorting happens in Python because ``filing_date`` is stored as filed -
    ``"8/10/2026"`` - which does not sort as text.
    """
    import consensus
    import disclosure

    if not members:
        return []

    # Cached across requests, because this is the most expensive thing the site
    # does and its answer only changes when the database is replaced. Measured
    # on the landing page: 574 ms and a 53 MB peak allocation *per request*,
    # from materialising ~6,800 four-way-joined rows and filtering them in
    # Python. The peak is the more dangerous half - several concurrent requests
    # on a 1 GB box is how this falls over, and it would have fallen over under
    # exactly the traffic that would make it worth reading.
    #
    # `sectors_by_member` is deliberately not part of the key: it is derived
    # from the same database, so the file identity already covers it.
    global _ACTIVITY_IDENT
    ident = _db_identity(conn)
    if ident != _ACTIVITY_IDENT:
        _ACTIVITY_CACHE.clear()      # a new file; nothing from the old one is valid
        _ACTIVITY_IDENT = ident
    key = (tuple(members), limit, order)
    hit = _ACTIVITY_CACHE.get(key)
    if hit is not None:
        return hit

    marks = ",".join("?" * len(members))
    # ``limit`` counts rows that survive the remit filter below, so it has to be
    # applied there rather than in SQL. Capping the query instead took the most
    # recent N transactions and *then* filtered, which made the total depend on
    # how many out-of-remit trades happened to be recent: the trades page
    # announced "every recorded trade" over 1,781 rows when there were 6,775.
    rows = conn.execute(
        f"""SELECT t.id, t.doc_id, t.seq, t.bioguide_id, t.member_name, t.ticker, t.asset_name,
                   t.asset_type, t.tx_type, COALESCE(t.tx_date_fixed, t.tx_date) AS tx_date, t.tx_date AS tx_date_filed, t.tx_date_note, t.filing_date, t.owner,
                   t.amount_raw, t.amount_mid, f.pdf_url, m.sectors,
                   g.grade, g.score AS grade_score, g.label AS grade_label,
                   g.cluster_size
              FROM transactions t
         LEFT JOIN filings f ON f.doc_id = t.doc_id
         LEFT JOIN ticker_meta m ON m.ticker = t.ticker
         LEFT JOIN trade_grades g ON g.transaction_id = t.id
             WHERE t.bioguide_id IN ({marks}) AND t.ticker IS NOT NULL
          ORDER BY COALESCE(t.tx_date_fixed, t.tx_date) DESC, t.id DESC""",
        members).fetchall()

    # Which rows the strategy actually buys. A purchase needs a usable price
    # series to be tradable at all, so "in remit" is necessary but not
    # sufficient - 20 in-remit buys have no series and are shown, correctly, as
    # not traded rather than quietly dropped.
    tradable = {r["transaction_id"] for r in conn.execute(
        """SELECT p.transaction_id FROM trade_performance p
             JOIN transactions t ON t.id = p.transaction_id
            WHERE t.tx_type = 'purchase' AND p.entry_date IS NOT NULL""")}

    out = []
    for r in rows:
        sectors = {s for s in (r["sectors"] or "").split(",") if s}
        overlap = sorted(sectors_by_member.get(r["bioguide_id"], set()) & sectors)
        if not overlap:
            continue  # the site only reports trades inside the member's remit
        d = dict(r)
        d["overlap"] = overlap
        is_buy = r["tx_type"] == "purchase"
        d["in_strategy"] = bool(is_buy and r["id"] in tradable)
        # The date the trade became public, ISO and sortable. Everything that
        # calls itself "latest" has to use this rather than tx_date.
        d["public"] = disclosure.to_iso(r["filing_date"])
        d["disclosure"] = disclosure.describe(
            disclosure.lag_days(r["tx_date"], r["filing_date"]))
        # Only purchases can be ranked, and only the clustered ones are worth
        # the query, so this is deferred to the rows that will actually use it.
        d["consensus"] = None
        d["why_not"] = (None if d["in_strategy"] else
                        ("the strategy holds for a fixed term and does not follow "
                         "members out of a position" if not is_buy else
                         "no usable price history for this security"))
        out.append(d)
        # Truncating by trade date while sorting by filing date would drop the
        # very rows the caller asked for, so the cap is applied after the sort.
        if order != "filed" and len(out) >= limit:
            break

    if order == "filed":
        # Rows with no parseable filing date sort last rather than crashing the
        # comparison or silently jumping to the top.
        out.sort(key=lambda d: (d["public"] or "", d["tx_date"] or ""),
                 reverse=True)
        out = out[:limit]

    consensus.annotate(conn, [d for d in out if d["tx_type"] == "purchase"])
    # Stored under the identity read *before* the query. If the file was
    # swapped while we were reading it, the next call sees a different identity
    # and rebuilds; storing a post-read identity would instead label rows from
    # the old file as current and never expire them.
    if ident == _ACTIVITY_IDENT:
        _ACTIVITY_CACHE[key] = out
    return out


def summary(trades: list[dict]) -> dict:
    """Headline counters for the landing page."""
    qualified = [t for t in trades if t["qualified"]]
    scored = [t["alpha_12m"] for t in qualified if t["alpha_12m"] is not None]
    return {
        "n_in_remit": len(trades),
        "n_qualified": len(qualified),
        "n_members": len({t["bioguide_id"] for t in qualified}),
        "mean_alpha": statistics.fmean(scored) if scored else None,
        "median_alpha": statistics.median(scored) if scored else None,
        "hit_rate": (sum(1 for a in scored if a > 0) / len(scored)) if scored else None,
        "volume": sum(t["amount_mid"] or 0 for t in qualified),
        "n_settled": len(scored),
    }
