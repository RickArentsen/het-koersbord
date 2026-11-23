"""Which rule should decide whose trades get tracked?

The site currently answers this one way - in-remit buys from members whose own
in-remit record is already positive - and that answer has never been tested
against the alternatives.  This module tests it.

A *selection rule* is a predicate over one disclosed purchase.  Every rule here
sees exactly the same universe (every scored purchase by every matched member)
and is measured exactly the same way, so the comparison is about the rule and
nothing else.

Two measurements, because they answer different questions
---------------------------------------------------------
``mean alpha per decision``
    Twelve-month return against SPY, deduplicated by
    :func:`strategy.decision_key`, with a t-statistic.  This is the testable
    quantity: it has a sample size, and the difference between two rules can be
    checked against noise.

``equity curve``
    CAGR, drawdown, volatility and exposure for actually holding the picks.
    This is what a follower experiences - but it is a *single path*, so a
    two-point CAGR gap between rules is not evidence of anything on its own.

Both are reported.  Ranking on the curve alone is how a backtest lies.

The split-sample test
---------------------
Eighteen rules scored on one dataset guarantees that the best-looking one is
partly best-looking by luck.  :func:`run` therefore scores every rule three
times: over the whole history, over a *training* window, and over a *test*
window that the training window cannot see.  A rule that only wins in-sample
is a rule that was fitted to this data.

Point-in-time honesty, and where it breaks
------------------------------------------
Every track-record gate here is walk-forward: a member's record counts only
trades whose twelve-month window had already closed before the trade being
judged was disclosed.  Without that, "members with a positive record" wins
every comparison by construction, because the record is the outcome.

Two inputs are **not** point-in-time and cannot be made so from this data:

* **Committee membership.**  ``congress-legislators`` publishes only
  ``committee-membership-current.json``; there is no historical file.  So a
  2021 trade is judged against the member's 2026 seat.  Assignments are sticky
  - most members keep a committee for many terms - but this is a real advantage
  handed to every remit-based rule, and it inflates them all equally.
* **Market cap and committee leadership**, used by the grade, are likewise
  current-day values.

These are stated rather than corrected, because correcting them is not possible
with the available source.  They mean the remit and grade families are being
measured at their most flattering.
"""

from __future__ import annotations

import datetime as dt
import math
import sqlite3
import statistics
from collections import defaultdict

import strategy
from prices import PriceLookup

# Windows for the split-sample test.  The training window ends far enough back
# that its trades have settled and could genuinely have informed a choice made
# at that point; the test window is everything after.
TRAIN_END = "2023-06-30"
TEST_START = "2023-07-01"

# A rule that fires a handful of times cannot be told apart from noise however
# good it looks, so anything below this is reported but never recommended.
MIN_DECISIONS = 30


# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------

def universe(conn: sqlite3.Connection) -> list[dict]:
    """Every scored purchase by a matched member, with everything a rule needs.

    Ordered by entry date, which is what makes the walk-forward annotation in
    :func:`annotate` a single pass rather than a per-trade re-query.
    """
    import congress
    import disclosure
    import grading

    ticker_sectors = {
        r["ticker"]: {s for s in (r["sectors"] or "").split(",") if s}
        for r in conn.execute("SELECT ticker, sectors FROM ticker_meta")
    }
    caps = grading.market_caps_for(conn)
    snapshots = congress.snapshot_dates(conn)

    rows = conn.execute(
        """SELECT t.id, t.bioguide_id, t.member_name, t.ticker, t.asset_type,
                  COALESCE(t.tx_date_fixed, t.tx_date) AS tx_date,
                  t.filing_date, t.owner, t.amount_mid,
                  p.entry_date, p.ret_12m, p.bench_12m,
                  g.grade, g.score AS grade_score, g.cluster_size, g.components
             FROM transactions t
             JOIN trade_performance p ON p.transaction_id = t.id
        LEFT JOIN trade_grades g ON g.transaction_id = t.id
            WHERE t.tx_type = 'purchase' AND t.bioguide_id IS NOT NULL
              AND t.ticker IS NOT NULL AND p.entry_date IS NOT NULL
         ORDER BY p.entry_date, t.id"""
    ).fetchall()

    remit_cache: dict[tuple, tuple[float, set[str]]] = {}
    committees_cache: dict[tuple, list[str]] = {}
    out: list[dict] = []
    for r in rows:
        bg = r["bioguide_id"]
        # Point-in-time, exactly as the strategy does it, or the comparison would
        # be scoring rules against a remit the members did not have yet.
        key = (bg, congress.snapshot_at(snapshots, r["tx_date"]))
        if key not in remit_cache:
            sens, names, sectors = congress.member_access_score(
                conn, bg, as_of=r["tx_date"])
            remit_cache[key] = (sens, set(sectors))
            committees_cache[key] = list(names)
        sensitivity, member_sectors = remit_cache[key]
        overlap = sorted(member_sectors & ticker_sectors.get(r["ticker"] or "", set()))

        comp = dict(p.split("=", 1) for p in (r["components"] or "").split(";")
                    if "=" in p)
        d = dict(r)
        d.update({
            "in_remit": bool(overlap),
            "overlap": overlap,
            "sectors": sorted(ticker_sectors.get(r["ticker"] or "", set())),
            "sensitivity": sensitivity,
            "committees": committees_cache[key],
            "market_cap": caps.get(grading._cap_key(r["ticker"] or "")),
            "new_position": float(comp.get("new_position", 0) or 0) > 0,
            # None when the filing date is missing or predates its own trade;
            # a rule must never read either of those as a prompt filing.
            "lag_days": disclosure.lag_days(r["tx_date"], r["filing_date"]),
            "alpha_12m": (r["ret_12m"] - r["bench_12m"]
                          if r["ret_12m"] is not None and r["bench_12m"] is not None
                          else None),
        })
        out.append(d)
    return out


def annotate(trades: list[dict]) -> list[dict]:
    """Attach every member's *prior settled* record to each trade.

    One ordered pass builds two histories per member - all their buys, and only
    their in-remit buys - and each trade reads the slice of those histories
    whose holding periods had already closed when it was disclosed.  Everything
    the track-record family of rules needs is derived here, so no rule has to
    reach forward in time on its own.

    Decisions, not filed lines: a member who files the same buy across four
    accounts made one judgement, and counting four would let a single morning
    clear an evidence threshold by itself.
    """
    hist: dict[str, dict[str, list]] = defaultdict(
        lambda: {"all": [], "remit": []})
    counted: dict[str, set] = {"all": set(), "remit": set()}

    # Committee-level record: how the members of a given committee have done on
    # trades inside that committee's jurisdiction.  Selecting by committee
    # rather than by member is one of the alternatives under test.
    comm_hist: dict[str, list] = defaultdict(list)
    comm_counted: set = set()

    for t in trades:
        bg = t["bioguide_id"]
        now = t["entry_date"]
        for stream in ("all", "remit"):
            settled = [a for close, a in hist[bg][stream] if close <= now]
            t[f"n_{stream}"] = len(settled)
            t[f"mean_{stream}"] = statistics.fmean(settled) if settled else None
            t[f"hit_{stream}"] = (sum(1 for a in settled if a > 0) / len(settled)
                                  if settled else None)
            # Last twenty settled decisions: a record from 2020 may say nothing
            # about how someone trades now.
            recent = settled[-20:]
            t[f"recent_{stream}"] = statistics.fmean(recent) if recent else None

        best_n, best_mean = 0, None
        for name in t["committees"]:
            settled = [a for close, a in comm_hist[name] if close <= now]
            if len(settled) > best_n:
                best_n, best_mean = len(settled), statistics.fmean(settled)
        t["n_committee"] = best_n
        t["mean_committee"] = best_mean

        alpha = t["alpha_12m"]
        if alpha is None:
            continue
        close = strategy._shift(t["entry_date"], strategy.HOLD_DAYS)
        key = strategy.decision_key(t)
        if key not in counted["all"]:
            counted["all"].add(key)
            hist[bg]["all"].append((close, alpha))
        if t["in_remit"] and key not in counted["remit"]:
            counted["remit"].add(key)
            hist[bg]["remit"].append((close, alpha))
            ckey = (t["ticker"], t["tx_date"], bg)
            if ckey not in comm_counted:
                comm_counted.add(ckey)
                for name in t["committees"]:
                    comm_hist[name].append((close, alpha))
    return trades


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------
# Grouped by the four families under consideration.  Each is a predicate over
# one annotated trade; the harness handles everything else.

MIN_CLOSED = strategy.MIN_CLOSED_TRADES  # 5

RULES: dict[str, tuple[str, str, callable]] = {
    # -- baseline ----------------------------------------------------------
    "all_buys": (
        "baseline", "Every disclosed buy, no filter",
        lambda t: True),

    # -- 1. committees -----------------------------------------------------
    "remit": (
        "committee", "Buy is inside the member's committee jurisdiction",
        lambda t: t["in_remit"]),
    "remit_sensitive": (
        "committee", "In remit, and on a high-sensitivity committee (>=0.8)",
        lambda t: t["in_remit"] and (t["sensitivity"] or 0) >= 0.8),
    "remit_committee_record": (
        "committee", "In remit, and that committee's own record is positive",
        lambda t: t["in_remit"] and t["n_committee"] >= 20
        and (t["mean_committee"] or 0) > 0),

    # -- 2. committees + grading -------------------------------------------
    "remit_grade_a": (
        "committee+grade", "In remit and graded A",
        lambda t: t["in_remit"] and t["grade"] == "A"),
    "remit_grade_ab": (
        "committee+grade", "In remit and graded A or B",
        lambda t: t["in_remit"] and t["grade"] in ("A", "B")),
    "remit_isolated": (
        "committee+grade", "In remit and filed alone, not in a block",
        lambda t: t["in_remit"] and (t["cluster_size"] or 99) <= 1),
    "remit_self_directed": (
        "committee+grade", "In remit and in the member's own account",
        lambda t: t["in_remit"] and (t["owner"] or "") == "self"),
    "remit_new_position": (
        "committee+grade", "In remit and a name they had never held",
        lambda t: t["in_remit"] and t["new_position"]),
    "remit_obscure": (
        "committee+grade", "In remit and market cap under $50bn",
        lambda t: t["in_remit"] and (t["market_cap"] or 0) > 0
        and t["market_cap"] < 5e10),

    # -- disclosure speed --------------------------------------------------
    # The lag is exactly the part of the move a follower cannot have: nobody can
    # act until the filing is public. These test whether that is worth trading.
    "remit_fast": (
        "disclosure speed", "In remit and filed within 14 days of the trade",
        lambda t: t["in_remit"] and t["lag_days"] is not None
        and t["lag_days"] <= 14),
    "remit_fast_30": (
        "disclosure speed", "In remit and filed within 30 days",
        lambda t: t["in_remit"] and t["lag_days"] is not None
        and t["lag_days"] <= 30),
    "remit_slow": (
        "disclosure speed", "In remit but filed after 45 days - the control",
        lambda t: t["in_remit"] and t["lag_days"] is not None
        and t["lag_days"] > 45),
    "fast_only": (
        "disclosure speed", "Filed within 14 days, no remit filter",
        lambda t: t["lag_days"] is not None and t["lag_days"] <= 14),

    # -- 3. pre-selected politicians ---------------------------------------
    "remit_track": (
        "track record", "CURRENT SITE RULE: in remit, member's prior in-remit "
        "record positive over >=5 settled decisions",
        lambda t: t["in_remit"] and t["n_remit"] >= MIN_CLOSED
        and (t["mean_remit"] or 0) > 0),
    "track_only": (
        "track record", "Member's prior record positive over all their buys, "
        "no remit filter",
        lambda t: t["n_all"] >= 10 and (t["mean_all"] or 0) > 0),
    "remit_track_hit": (
        "track record", "In remit, member beat SPY on >50% of prior decisions",
        lambda t: t["in_remit"] and t["n_remit"] >= MIN_CLOSED
        and (t["hit_remit"] or 0) > 0.5),
    "remit_track_recent": (
        "track record", "In remit, member's last 20 settled decisions positive",
        lambda t: t["in_remit"] and t["n_remit"] >= MIN_CLOSED
        and (t["recent_remit"] or 0) > 0),
    "remit_track_strict": (
        "track record", "In remit, >=10 prior decisions averaging >+5% alpha",
        lambda t: t["in_remit"] and t["n_remit"] >= 10
        and (t["mean_remit"] or 0) > 0.05),
    "track_all_positive": (
        "track record", "Any buy from a member whose prior in-remit record is "
        "positive (remit filter dropped, member filter kept)",
        lambda t: t["n_remit"] >= MIN_CLOSED and (t["mean_remit"] or 0) > 0),

    # -- 4. other forms of grading -----------------------------------------
    "grade_a_only": (
        "grade only", "Graded A, regardless of member record",
        lambda t: t["grade"] == "A"),
    "grade_ab_only": (
        "grade only", "Graded A or B, regardless of member record",
        lambda t: t["grade"] in ("A", "B")),
    "isolated_only": (
        "grade only", "Filed alone, regardless of remit or record",
        lambda t: (t["cluster_size"] or 99) <= 1),
    "remit_track_grade": (
        "stacked", "Current rule PLUS graded A or B",
        lambda t: t["in_remit"] and t["n_remit"] >= MIN_CLOSED
        and (t["mean_remit"] or 0) > 0 and t["grade"] in ("A", "B")),
    "remit_track_isolated": (
        "stacked", "Current rule PLUS filed alone",
        lambda t: t["in_remit"] and t["n_remit"] >= MIN_CLOSED
        and (t["mean_remit"] or 0) > 0 and (t["cluster_size"] or 99) <= 1),
}


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

def _t_stat(values: list[float]) -> float | None:
    """Sample t against zero.  n-1, because these trades are a sample."""
    if len(values) < 2:
        return None
    sd = statistics.stdev(values)
    if sd <= 0:
        return None
    return statistics.fmean(values) / (sd / math.sqrt(len(values)))


def _welch(a: list[float], b: list[float]) -> float | None:
    """Welch's t between two rules' decision alphas - unequal variance."""
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a) / len(a), statistics.variance(b) / len(b)
    if va + vb <= 0:
        return None
    return (statistics.fmean(a) - statistics.fmean(b)) / math.sqrt(va + vb)


def decision_alphas(picks: list[dict]) -> list[float]:
    """Settled 12m alphas, one per decision rather than per filed line."""
    seen: set = set()
    out = []
    for t in picks:
        key = strategy.decision_key(t)
        if key in seen or t["alpha_12m"] is None:
            continue
        seen.add(key)
        out.append(t["alpha_12m"])
    return out


def _dollar_alpha(picks: list[dict]) -> float | None:
    """Mean 12m alpha weighted by disclosed size, deduplicated by decision.

    Sizes are summed across the lines of one decision, because a buy filed
    across four accounts really is four times the money even though it is only
    one judgement.
    """
    by_key: dict[tuple, list] = {}
    for t in picks:
        if t["alpha_12m"] is None:
            continue
        key = strategy.decision_key(t)
        entry = by_key.setdefault(key, [t["alpha_12m"], 0.0])
        entry[1] += float(t["amount_mid"] or 0)
    num = sum(a * w for a, w in by_key.values())
    den = sum(w for _a, w in by_key.values())
    return (num / den) if den else None


def _annualised_vol(equity: list[float]) -> float | None:
    if len(equity) < 30:
        return None
    rets = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))
            if equity[i - 1] > 0]
    if len(rets) < 30:
        return None
    return statistics.stdev(rets) * math.sqrt(252)


def evaluate(conn: sqlite3.Connection, lookup: PriceLookup, trades: list[dict],
             predicate, window: tuple[str, str],
             entry_range: tuple[str, str]) -> dict:
    """Score one rule over one window.

    ``entry_range`` bounds which trades may be *entered*; ``window`` pins the
    curve's calendar so every rule is measured over the same market.
    """
    picks = []
    for t in trades:
        if not (entry_range[0] <= t["entry_date"] <= entry_range[1]):
            continue
        try:
            ok = bool(predicate(t))
        except (TypeError, KeyError):
            ok = False
        if ok:
            picks.append(dict(t, qualified=True))

    alphas = decision_alphas(picks)
    row = {
        "n_rows": len(picks),
        "n_decisions": len(alphas),
        "n_members": len({t["bioguide_id"] for t in picks}),
        "n_tickers": len({t["ticker"] for t in picks}),
        "mean_alpha": statistics.fmean(alphas) if alphas else None,
        "median_alpha": statistics.median(alphas) if alphas else None,
        "hit_rate": (sum(1 for a in alphas if a > 0) / len(alphas)) if alphas else None,
        "t_stat": _t_stat(alphas),
        "alphas": alphas,
        # Disclosed sizes span $1k to $5m, so the average *decision* and the
        # average *dollar* can point in opposite directions. When they do, a
        # size-weighted equity curve is being carried by a handful of positions
        # and the rule is not doing the work the curve appears to credit it with.
        "dollar_alpha": _dollar_alpha(picks),
    }

    # Concentration: how much of the money rides on the two biggest names.
    by_member: dict[str, float] = defaultdict(float)
    for t in picks:
        by_member[t["bioguide_id"]] += float(t["amount_mid"] or 0)
    total = sum(by_member.values())
    top2 = sorted(by_member.values(), reverse=True)[:2]
    row["top2_member_share"] = (sum(top2) / total) if total else None

    if picks:
        c = strategy.curve(conn, lookup, picks, weight="size",
                           only_qualified=True, window=window)
        e = strategy.curve(conn, lookup, picks, weight="equal",
                           only_qualified=True, window=window)
        vol = _annualised_vol(c.get("strategy") or [])
        row.update({
            "cagr": c.get("cagr"), "bench_cagr": c.get("bench_cagr"),
            "max_drawdown": c.get("max_drawdown"),
            "bench_max_drawdown": c.get("bench_max_drawdown"),
            "exposure": c.get("exposure"),
            "final": c.get("final_strategy"),
            "bench_final": c.get("final_benchmark"),
            "vol": vol,
            # Return per unit of risk, which is the only fair way to compare a
            # concentrated book against a diversified one.
            "return_vol": (c["cagr"] / vol) if vol and c.get("cagr") else None,
            "equal_cagr": e.get("cagr"),
            "equal_max_drawdown": e.get("max_drawdown"),
        })
    return row


def sector_control(trades: list[dict]) -> dict:
    """Is the committee edge real, or just the sectors committees happen to cover?

    Committee jurisdictions are technology, defense, healthcare and financials -
    exactly the sectors that led the market over this period.  So "in-remit
    trades beat SPY" could mean members know something, or it could mean the
    filter is a long-tech proxy with extra steps.

    This separates the two by comparing in-remit against out-of-remit decisions
    *within the same sector and the same quarter*, then averaging those matched
    differences weighted by cell size.  A cell needs both kinds of trade to
    contribute, so sector and timing are held fixed and only the committee link
    varies.
    """
    cells: dict[tuple, dict[str, list]] = defaultdict(lambda: {"in": [], "out": []})
    seen: set = set()
    for t in trades:
        if t["alpha_12m"] is None or not t["sectors"]:
            continue
        key = strategy.decision_key(t)
        if key in seen:
            continue
        seen.add(key)
        quarter = f"{t['entry_date'][:4]}Q{(int(t['entry_date'][5:7]) - 1) // 3 + 1}"
        for sector in t["sectors"]:
            cells[(sector, quarter)]["in" if t["in_remit"] else "out"].append(
                t["alpha_12m"])

    diffs, weights, n_cells, n_in, n_out = [], [], 0, 0, 0
    for (_sector, _q), v in cells.items():
        if not v["in"] or not v["out"]:
            continue
        n_cells += 1
        n_in += len(v["in"])
        n_out += len(v["out"])
        diffs.append(statistics.fmean(v["in"]) - statistics.fmean(v["out"]))
        # Harmonic-style weight: a cell contributes in proportion to the
        # smaller of its two sides, since that is what limits its precision.
        weights.append(min(len(v["in"]), len(v["out"])))

    if not diffs:
        return {"n_cells": 0}
    total = sum(weights)
    weighted = sum(d * w for d, w in zip(diffs, weights)) / total
    return {
        "n_cells": n_cells, "n_in": n_in, "n_out": n_out,
        "mean_diff": statistics.fmean(diffs),
        "weighted_diff": weighted,
        "t_stat": _t_stat(diffs),
        "positive_cells": sum(1 for d in diffs if d > 0) / len(diffs),
    }


def exit_rules(conn: sqlite3.Connection, lookup: PriceLookup,
               trades: list[dict]) -> dict:
    """Should a follower sell when the member sells?  No - and it is not close.

    The strategy exits on a timer: twelve months from entry, whatever the member
    does next.  The obvious objection is that this ignores information - if a
    member closes a position, surely that is exactly the moment to leave.

    So it is measured.  For every in-remit buy, the member's own later disclosed
    sale of that ticker is located, and the position exited on the first
    tradable day after that sale became public rather than at twelve months.
    Same trades, same entries; only the exit changes.

    Also tested: whether a disclosed sale is worth *shorting*.  That needs the
    ticker to reliably fall against the index afterwards, which is a much
    stronger claim than "the member wanted out".
    """
    import datetime as dt

    from backtest import _to_iso
    from prices import BENCHMARK, normalize_ticker

    today = dt.date.today().isoformat()

    def entry_after(row) -> str | None:
        """First tradable day after the filing was public - the copy convention."""
        pub = _to_iso(row["filing_date"]) or row["tx_date"]
        if not pub:
            return None
        target = strategy._shift(pub, 1)
        hit = lookup.first_on_or_after(normalize_ticker(row["ticker"] or ""), target)
        return hit[0] if hit else None

    def rel(ticker: str, d0: str, d1: str) -> float | None:
        a = lookup.first_on_or_after(ticker, d0)
        b = lookup.last_on_or_before(ticker, d1)
        if not a or not b or a[1] <= 0 or b[0] <= a[0]:
            return None
        return b[1] / a[1] - 1

    sells = conn.execute(
        """SELECT t.id, t.bioguide_id, t.ticker, t.tx_date, t.filing_date
             FROM transactions t
            WHERE t.tx_type LIKE 'sale%' AND t.bioguide_id IS NOT NULL
              AND t.ticker IS NOT NULL""").fetchall()
    exits: dict[tuple, list[str]] = defaultdict(list)
    for s in sells:
        when = entry_after(s)
        if when:
            exits[(s["bioguide_id"], normalize_ticker(s["ticker"] or ""))].append(when)
    for key in exits:
        exits[key].sort()

    held, followed, cut = [], [], []
    for t in trades:
        if not t["in_remit"] or not t["entry_date"]:
            continue
        ticker = normalize_ticker(t["ticker"] or "")
        end = strategy._shift(t["entry_date"], strategy.HOLD_DAYS)
        if end > today:
            continue
        base, bench = rel(ticker, t["entry_date"], end), rel(BENCHMARK, t["entry_date"], end)
        if base is None or bench is None:
            continue
        held.append(base - bench)

        later = [d for d in exits.get((t["bioguide_id"], ticker), [])
                 if d > t["entry_date"]]
        stop = min(later[0], end) if later else end
        if stop < end:
            cut.append((dt.date.fromisoformat(stop)
                        - dt.date.fromisoformat(t["entry_date"])).days)
        r, b = rel(ticker, t["entry_date"], stop), rel(BENCHMARK, t["entry_date"], stop)
        followed.append((r - b) if r is not None and b is not None else base - bench)

    changed = [f - h for f, h in zip(followed, held) if abs(f - h) > 1e-9]
    out = {
        "n": len(held), "n_cut_short": len(cut),
        "median_days_when_cut": statistics.median(cut) if cut else None,
        "hold_alpha": statistics.fmean(held) if held else None,
        "hold_t": _t_stat(held),
        "follow_alpha": statistics.fmean(followed) if followed else None,
        "follow_t": _t_stat(followed),
        "cost_on_changed": statistics.fmean(changed) if changed else None,
        "cost_t": _t_stat(changed),
    }

    # Shorting the sale: does the name actually fall against the index after a
    # member discloses getting out?
    out["short"] = {}
    for label, days in (("1m", 30), ("3m", 91), ("12m", 365)):
        alphas = []
        for s in sells:
            when = entry_after(s)
            if not when:
                continue
            end = strategy._shift(when, days)
            if end > today:
                continue
            a = rel(normalize_ticker(s["ticker"] or ""), when, end)
            b = rel(BENCHMARK, when, end)
            if a is not None and b is not None:
                alphas.append(a - b)
        if alphas:
            out["short"][label] = {
                "n": len(alphas), "mean": statistics.fmean(alphas),
                "median": statistics.median(alphas), "t": _t_stat(alphas),
                "share_negative": sum(1 for x in alphas if x < 0) / len(alphas),
            }
    return out


def record_gate_test(trades: list[dict]) -> dict:
    """The cleanest test of pre-selecting politicians, on a like-for-like sample.

    Comparing "members with a positive record" against the whole universe is
    unfair to the gate: it cannot fire until a member has five settled
    decisions, so it never sees 2020 and is judged on a later, different market
    than the rules it is being measured against.

    So restrict to the trades where the gate *could* have fired - in-remit buys
    by members who already had at least :data:`MIN_CLOSED` settled in-remit
    decisions - and split those by which way the gate went.  Same members, same
    period, same remit filter; the only difference is whether their prior record
    was positive.  If pre-selection works, the positive side wins.
    """
    eligible = [t for t in trades if t["in_remit"] and t["n_remit"] >= MIN_CLOSED]
    passed, failed, seen = [], [], set()
    for t in eligible:
        key = strategy.decision_key(t)
        if key in seen or t["alpha_12m"] is None:
            continue
        seen.add(key)
        ((passed if (t["mean_remit"] or 0) > 0 else failed)
         .append(t["alpha_12m"]))

    out = {"n_eligible": len(seen), "n_passed": len(passed), "n_failed": len(failed)}
    if passed:
        out.update(passed_alpha=statistics.fmean(passed), passed_t=_t_stat(passed))
    if failed:
        out.update(failed_alpha=statistics.fmean(failed), failed_t=_t_stat(failed))
    if passed and failed:
        out["gap"] = statistics.fmean(passed) - statistics.fmean(failed)
        out["welch"] = _welch(passed, failed)

    # A raw gap this large has two innocent explanations that must be excluded
    # before it can be called mean reversion:
    #
    #   timing - if the passing trades happen to cluster in a quarter that went
    #            on to be bad, the gate is being blamed for the calendar;
    #   one member - the passing side is dominated by whoever files most, so the
    #            gap could be a single person's bad run rather than a property
    #            of the gate.
    #
    # Both are checked here rather than assumed away.
    cells: dict[str, dict[str, list]] = defaultdict(lambda: {"p": [], "f": []})
    per_member: dict[str, dict[str, list]] = defaultdict(lambda: {"p": [], "f": []})
    names: dict[str, str] = {}
    seen2: set = set()
    for t in eligible:
        key = strategy.decision_key(t)
        if key in seen2 or t["alpha_12m"] is None:
            continue
        seen2.add(key)
        side = "p" if (t["mean_remit"] or 0) > 0 else "f"
        quarter = f"{t['entry_date'][:4]}Q{(int(t['entry_date'][5:7]) - 1) // 3 + 1}"
        cells[quarter][side].append(t["alpha_12m"])
        per_member[t["bioguide_id"]][side].append(t["alpha_12m"])
        names[t["bioguide_id"]] = (t["member_name"] or "").replace("Hon. ", "")

    diffs, weights = [], []
    for v in cells.values():
        if v["p"] and v["f"]:
            diffs.append(statistics.fmean(v["p"]) - statistics.fmean(v["f"]))
            weights.append(min(len(v["p"]), len(v["f"])))
    if diffs:
        out["quarter_matched_gap"] = (sum(d * w for d, w in zip(diffs, weights))
                                      / sum(weights))
        out["quarter_matched_t"] = _t_stat(diffs)
        out["quarter_cells"] = len(diffs)
        out["quarters_favouring_gate"] = sum(1 for d in diffs if d > 0) / len(diffs)

    # Per member, both sides of their own history: does the same person do worse
    # after their record turns positive than before?
    within = []
    detail = []
    for bg, v in per_member.items():
        if len(v["p"]) >= 5 and len(v["f"]) >= 5:
            gap = statistics.fmean(v["p"]) - statistics.fmean(v["f"])
            within.append(gap)
            detail.append({"name": names[bg], "n_passed": len(v["p"]),
                           "n_failed": len(v["f"]),
                           "passed": statistics.fmean(v["p"]),
                           "failed": statistics.fmean(v["f"]), "gap": gap})
    if within:
        out["within_member_gap"] = statistics.fmean(within)
        out["within_member_t"] = _t_stat(within)
        out["within_member_n"] = len(within)
        out["within_member_detail"] = sorted(detail, key=lambda d: d["gap"])
    return out


def member_robustness(trades: list[dict], predicate, top: int = 6) -> list[dict]:
    """Recompute a rule's alpha with each of its busiest members removed.

    A rule that only works because one member is in it is a bet on that member,
    not a rule.  This makes that visible instead of leaving it to be discovered
    later.
    """
    picks = [t for t in trades if _safe(predicate, t)]
    base = decision_alphas(picks)
    if not base:
        return []
    counts: dict[str, int] = defaultdict(int)
    seen: set = set()
    for t in picks:
        key = strategy.decision_key(t)
        if key in seen:
            continue
        seen.add(key)
        counts[t["bioguide_id"]] += 1

    out = []
    for bg, n in sorted(counts.items(), key=lambda kv: -kv[1])[:top]:
        rest = decision_alphas([t for t in picks if t["bioguide_id"] != bg])
        name = next((t["member_name"] for t in picks if t["bioguide_id"] == bg), bg)
        out.append({
            "bioguide_id": bg, "name": (name or "").replace("Hon. ", ""),
            "n_decisions": n,
            "alpha_without": statistics.fmean(rest) if rest else None,
            "t_without": _t_stat(rest),
        })
    return out


def _safe(predicate, trade) -> bool:
    try:
        return bool(predicate(trade))
    except (TypeError, KeyError):
        return False


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    """Rank correlation, used to ask whether train performance predicts test."""
    if len(pairs) < 3:
        return None

    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    ra, rb = ranks([p[0] for p in pairs]), ranks([p[1] for p in pairs])
    n = len(pairs)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((a - ma) * (b - mb) for a, b in zip(ra, rb))
    den = math.sqrt(sum((a - ma) ** 2 for a in ra)
                    * sum((b - mb) ** 2 for b in rb))
    return (num / den) if den else None


def run(conn: sqlite3.Connection, progress: bool = True) -> dict:
    """Score every rule over the full history and over a train/test split."""
    trades = annotate(universe(conn))
    if not trades:
        return {"rules": {}, "error": "no scored trades"}

    lookup = PriceLookup(conn, tickers=[t["ticker"] for t in trades])
    first = min(t["entry_date"] for t in trades)
    last = max(t["entry_date"] for t in trades)
    today = dt.date.today().isoformat()

    splits = {
        "full": {"entry": (first, last), "window": (first, today)},
        "train": {"entry": (first, TRAIN_END),
                  "window": (first, strategy._shift(TRAIN_END, strategy.HOLD_DAYS))},
        "test": {"entry": (TEST_START, last), "window": (TEST_START, today)},
    }

    results: dict[str, dict] = {}
    for name, (family, description, predicate) in RULES.items():
        results[name] = {"family": family, "description": description,
                         "splits": {}}
        for split, bounds in splits.items():
            results[name]["splits"][split] = evaluate(
                conn, lookup, trades, predicate, bounds["window"], bounds["entry"])
        if progress:
            f = results[name]["splits"]["full"]
            print(f"  {name:24} n={f['n_decisions']:>5}  "
                  f"alpha={_pct(f['mean_alpha']):>8}  "
                  f"t={f['t_stat'] or 0:>5.2f}  "
                  f"cagr={_pct(f.get('cagr')):>8}", flush=True)

    # Does a rule's training-window alpha say anything about its test-window
    # alpha? If not, choosing a rule on in-sample performance is choosing noise.
    #
    # Measured *relative to the baseline in the same window*. The absolute level
    # shifts hard between the two periods - SPY beat almost every disclosed
    # trade after 2023, so every rule's raw alpha falls - and comparing raw
    # levels across windows would score that market-wide shift as if it were the
    # rules disagreeing with themselves.
    base_tr = results["all_buys"]["splits"]["train"]["mean_alpha"] or 0
    base_te = results["all_buys"]["splits"]["test"]["mean_alpha"] or 0
    for r in results.values():
        r["edge_train"] = ((r["splits"]["train"]["mean_alpha"] - base_tr)
                           if r["splits"]["train"]["mean_alpha"] is not None else None)
        r["edge_test"] = ((r["splits"]["test"]["mean_alpha"] - base_te)
                          if r["splits"]["test"]["mean_alpha"] is not None else None)
        r["consistent"] = (r["edge_train"] is not None and r["edge_test"] is not None
                           and r["edge_train"] * r["edge_test"] > 0
                           and min(r["splits"]["train"]["n_decisions"],
                                   r["splits"]["test"]["n_decisions"]) >= MIN_DECISIONS)

    pairs = [(r["edge_train"], r["edge_test"]) for r in results.values()
             if r["edge_train"] is not None and r["edge_test"] is not None]

    finalists = ["remit", "remit_sensitive", "remit_fast", "all_buys"]
    return {
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
        "splits": {k: {"entry": v["entry"], "window": v["window"]}
                   for k, v in splits.items()},
        "rules": results,
        "n_universe": len(trades),
        "sector_control": sector_control(trades),
        "record_gate": record_gate_test(trades),
        "exit_rules": exit_rules(conn, lookup, trades),
        "train_test_rho": _spearman(pairs),
        "robustness": {name: member_robustness(trades, RULES[name][2])
                       for name in finalists if name in RULES},
    }


def _pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:+.2f}%"


def report(result: dict) -> str:
    """A plain-text table of every rule, and the split-sample verdict."""
    rules = result["rules"]
    baseline = rules["all_buys"]["splits"]["full"]["alphas"]
    lines: list[str] = []

    lines.append(f"Universe: {result['n_universe']:,} scored purchases")
    for k, v in result["splits"].items():
        lines.append(f"  {k:6} entries {v['entry'][0]}..{v['entry'][1]}   "
                     f"curve {v['window'][0]}..{v['window'][1]}")
    lines.append("")

    bench = rules["all_buys"]["splits"]["full"].get("bench_cagr")
    lines.append(f"SPY over the full window: {_pct(bench)} a year")
    lines.append("")
    lines.append("Per-decision alpha is equal-weighted; $-alpha weights by "
                 "disclosed size. Where they")
    lines.append("disagree, the curve is being carried by a few large positions "
                 "rather than by the rule.")
    lines.append("")

    header = (f"{'rule':26} {'n':>5} {'alpha':>9} {'t':>6} {'$-alpha':>9} "
              f"{'hit':>6} {'CAGR':>8} {'exCAGR':>8} {'MDD':>8} {'ret/vol':>8} "
              f"{'mem':>4} {'vs all':>7}")
    for family in ("baseline", "committee", "disclosure speed",
                   "committee+grade", "track record", "grade only", "stacked"):
        lines.append(f"--- {family} " + "-" * (len(header) - len(family) - 5))
        lines.append(header)
        for name, r in rules.items():
            if r["family"] != family:
                continue
            f = r["splits"]["full"]
            w = _welch(f["alphas"], baseline) if name != "all_buys" else None
            excess = (f["cagr"] - f["bench_cagr"]
                      if f.get("cagr") is not None and f.get("bench_cagr") is not None
                      else None)
            lines.append(
                f"{name:26} {f['n_decisions']:>5} {_pct(f['mean_alpha']):>9} "
                f"{(f['t_stat'] or 0):>6.2f} {_pct(f.get('dollar_alpha')):>9} "
                f"{(f['hit_rate'] or 0) * 100:>5.1f}% "
                f"{_pct(f.get('cagr')):>8} {_pct(excess):>8} "
                f"{_pct(f.get('max_drawdown')):>8} "
                f"{(f.get('return_vol') or 0):>8.2f} {f['n_members']:>4} "
                f"{('' if w is None else f'{w:+.2f}'):>7}")
        lines.append("")

    lines.append("=== split-sample: does an in-sample winner stay a winner? ===")
    lines.append("Test-period alpha is measured against the same period's "
                 "all_buys baseline, because")
    lines.append("in 2023-2025 SPY beat almost every disclosed trade and the "
                 "absolute figures all go negative.")
    lines.append("")
    base_test = rules["all_buys"]["splits"]["test"]
    lines.append(f"{'rule':26} {'train n':>8} {'train a':>9} {'test n':>7} "
                 f"{'test a':>9} {'vs base':>8} {'Welch':>6} {'test exCAGR':>12}")
    ranked = sorted(rules.items(),
                    key=lambda kv: -(kv[1]["splits"]["train"]["mean_alpha"] or -9))
    for name, r in ranked:
        tr, te = r["splits"]["train"], r["splits"]["test"]
        rel = (te["mean_alpha"] - base_test["mean_alpha"]
               if te["mean_alpha"] is not None
               and base_test["mean_alpha"] is not None else None)
        w = _welch(te["alphas"], base_test["alphas"]) if name != "all_buys" else None
        ex = (te["cagr"] - te["bench_cagr"]
              if te.get("cagr") is not None and te.get("bench_cagr") is not None
              else None)
        lines.append(
            f"{name:26} {tr['n_decisions']:>8} {_pct(tr['mean_alpha']):>9} "
            f"{te['n_decisions']:>7} {_pct(te['mean_alpha']):>9} "
            f"{_pct(rel):>8} {('' if w is None else f'{w:+.2f}'):>6} "
            f"{_pct(ex):>12}")

    rho = result.get("train_test_rho")
    lines.append("")
    lines.append("=== the survivors: edge over the baseline, in BOTH halves ===")
    lines.append("A rule whose edge changes sign between the halves is not a "
                 "rule, it is a coin that")
    lines.append(f"landed twice. Needs >= {MIN_DECISIONS} decisions in each half "
                 f"to qualify at all.")
    lines.append("")
    lines.append(f"{'rule':26} {'train edge':>11} {'test edge':>11} "
                 f"{'holds?':>8}   family")
    for name, r in sorted(rules.items(),
                          key=lambda kv: -(kv[1].get("edge_test") or -9)):
        if name == "all_buys":
            continue
        lines.append(
            f"{name:26} {_pct(r.get('edge_train')):>11} "
            f"{_pct(r.get('edge_test')):>11} "
            f"{('YES' if r.get('consistent') else 'no'):>8}   {r['family']}")

    lines.append("")
    lines.append(f"Rank correlation, train edge vs test edge across all "
                 f"{len(rules)} rules: "
                 f"{'n/a' if rho is None else f'{rho:+.3f}'}")
    lines.append("Near zero or negative means in-sample ranking does not carry "
                 "forward, so a rule chosen")
    lines.append("on its backtest alpha alone is being chosen by luck. The "
                 "sign test above is the check")
    lines.append("that survives this - it asks only for direction, not for "
                 "magnitude or ordering.")

    rg = result.get("record_gate") or {}
    if rg.get("n_eligible"):
        lines.append("")
        lines.append("=== does pre-selecting politicians work? (like-for-like) ===")
        lines.append("Only in-remit buys by members who already had >=5 settled "
                     "in-remit decisions, so")
        lines.append("both sides are the same members over the same period. The "
                     "gate is the only difference.")
        lines.append(f"  record positive (site trades these): "
                     f"{rg['n_passed']:>5} decisions  "
                     f"{_pct(rg.get('passed_alpha')):>9}  "
                     f"t={(rg.get('passed_t') or 0):+.2f}")
        lines.append(f"  record negative (site skips these):  "
                     f"{rg['n_failed']:>5} decisions  "
                     f"{_pct(rg.get('failed_alpha')):>9}  "
                     f"t={(rg.get('failed_t') or 0):+.2f}")
        lines.append(f"  gap {_pct(rg.get('gap'))}   "
                     f"Welch t = {(rg.get('welch') or 0):+.2f}")
        if rg.get("quarter_cells"):
            lines.append(f"  controlling for timing (matched within entry "
                         f"quarter, {rg['quarter_cells']} quarters):")
            lines.append(f"      gap {_pct(rg['quarter_matched_gap'])}   "
                         f"t = {(rg.get('quarter_matched_t') or 0):+.2f}   "
                         f"{rg['quarters_favouring_gate'] * 100:.0f}% of quarters "
                         f"favour the gate")
        if rg.get("within_member_n"):
            lines.append(f"  within the same member, before vs after their "
                         f"record turned positive "
                         f"({rg['within_member_n']} members with >=5 each side):")
            lines.append(f"      mean gap {_pct(rg['within_member_gap'])}   "
                         f"t = {(rg.get('within_member_t') or 0):+.2f}")
            for d in rg.get("within_member_detail", []):
                lines.append(f"        {d['name'][:26]:28} "
                             f"record+ {_pct(d['passed']):>9} ({d['n_passed']:>3})   "
                             f"record- {_pct(d['failed']):>9} ({d['n_failed']:>3})   "
                             f"gap {_pct(d['gap']):>9}")

    er = result.get("exit_rules") or {}
    if er.get("n"):
        lines.append("")
        lines.append("=== should a follower sell when the member sells? ===")
        lines.append(f"  {er['n']} settled in-remit buys; the member later "
                     f"disclosed selling {er['n_cut_short']} of them inside the "
                     f"twelve months")
        if er.get("median_days_when_cut"):
            lines.append(f"  median holding when that happens: "
                         f"{er['median_days_when_cut']:.0f} days")
        lines.append(f"  hold to 12 months      {_pct(er['hold_alpha']):>9}   "
                     f"t={(er.get('hold_t') or 0):+.2f}")
        lines.append(f"  follow them out        {_pct(er['follow_alpha']):>9}   "
                     f"t={(er.get('follow_t') or 0):+.2f}")
        lines.append(f"  cost, on the trades it changes: "
                     f"{_pct(er['cost_on_changed'])}  t={(er.get('cost_t') or 0):+.2f}")
        lines.append("")
        lines.append("  is a disclosed sale worth SHORTING? (needs reliably "
                     "negative forward alpha)")
        for label, d in (er.get("short") or {}).items():
            lines.append(f"    {label:>4}  n={d['n']:>5}  mean {_pct(d['mean']):>8}  "
                         f"median {_pct(d['median']):>8}  t={d['t'] or 0:+.2f}  "
                         f"fell {d['share_negative']*100:.0f}% of the time")

    sc = result.get("sector_control") or {}
    if sc.get("n_cells"):
        lines.append("")
        lines.append("=== is the committee edge just a sector bet? ===")
        lines.append("In-remit minus out-of-remit alpha, matched within the "
                     "same sector and quarter:")
        lines.append(f"  {sc['n_cells']} cells ({sc['n_in']:,} in-remit vs "
                     f"{sc['n_out']:,} out-of-remit decisions)")
        lines.append(f"  weighted difference {_pct(sc['weighted_diff'])}   "
                     f"unweighted {_pct(sc['mean_diff'])}   "
                     f"t = {(sc['t_stat'] or 0):+.2f}")
        lines.append(f"  {sc['positive_cells'] * 100:.0f}% of cells favour the "
                     f"in-remit side")

    rob = result.get("robustness") or {}
    if rob:
        lines.append("")
        lines.append("=== leave-one-member-out: is a rule really a bet on one person? ===")
        for name, rows in rob.items():
            full = rules[name]["splits"]["full"]
            lines.append(f"  {name}  (all members: {_pct(full['mean_alpha'])}, "
                         f"t={(full['t_stat'] or 0):+.2f})")
            for row in rows:
                lines.append(f"      without {row['name'][:26]:28} "
                             f"({row['n_decisions']:>4} dec)  "
                             f"{_pct(row['alpha_without']):>9}  "
                             f"t={(row['t_without'] or 0):+.2f}")
    return "\n".join(lines)


CACHE_KEY = "selection_v1"


def build(conn: sqlite3.Connection, progress: bool = True) -> dict:
    import json

    import db as db_mod

    result = run(conn, progress=progress)
    # The raw alpha lists are only needed for the Welch tests inside one run;
    # storing tens of thousands of floats per rule would bloat the meta row.
    slim = {
        **result,
        "rules": {
            name: {**r, "splits": {s: {k: v for k, v in d.items() if k != "alphas"}
                                   for s, d in r["splits"].items()}}
            for name, r in result["rules"].items()
        },
    }
    db_mod.set_meta(conn, CACHE_KEY, json.dumps(slim, default=str))
    conn.commit()
    return result


def load(conn: sqlite3.Connection) -> dict | None:
    import json

    import db as db_mod

    raw = db_mod.get_meta(conn, CACHE_KEY)
    return json.loads(raw) if raw else None
