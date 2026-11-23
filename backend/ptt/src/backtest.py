"""Score disclosed purchases and rank politicians by copy-tradeable edge.

The central methodological point
--------------------------------
A member's trade is *not* investable on the day it happens.  Under the STOCK Act
the Periodic Transaction Report may be filed up to 30 days after the member is
notified and no later than 45 days after the trade, so the public only learns of
it when the PDF appears on the Clerk's site - the filing date.

This module therefore scores every purchase twice:

``member``      return from the member's own transaction date.  Measures the
                information they appear to have had.
``copy``        return from the first market open *after* the filing became
                public.  This is the only one you can actually trade, and it is
                what the leaderboard ranks on.

The gap between the two is the cost of the disclosure lag, and it is reported so
the difference is visible rather than assumed away.

Returns are compared against SPY over the identical holding window, so the
headline figure is alpha, not the direction of the market.  Because ranking ~200
members by mean return would surface noise, every row also carries a sample size
and a t-statistic.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import statistics
from collections import defaultdict

import prices as prices_mod
from prices import BENCHMARK, PriceLookup, normalize_ticker

# Scored holding periods. The long tail matters: the earliest filings are from
# 2020, so a trade from 2021 has five years of history behind it and stopping at
# twelve months discards most of what actually happened to it. Horizons whose
# window has not closed yet are stored as NULL, never as a flat return.
HORIZONS = {
    "1m": 30, "3m": 91, "6m": 182, "12m": 365,
    "18m": 548, "24m": 730, "36m": 1095, "48m": 1461, "60m": 1826,
}

# Equity curves are only built for these. Every horizon x weighting would mean
# eighteen full curve computations per backtest for no benefit - the long
# horizons are a per-trade outcome question, not a strategy the site offers.
CURVE_HORIZONS = ("1m", "3m", "6m", "12m")
# Asset classes where "buy the ticker" is a faithful replication.  Options (OP)
# are excluded: disclosing an option purchase is not the same trade as buying
# the underlying, and the payoff cannot be reconstructed from the filing.
SCOREABLE_TYPES = {"ST", "OT", "ETF", "CS", "EF"}

# Option purchases (OP) are scored against the *underlying*, and flagged.
# The filing gives no strike or expiry, so the option's own payoff cannot be
# reconstructed - but excluding them entirely was worse: options are only 1.5%
# of purchases yet a third of all disclosed purchase dollars, and they are the
# trades the most-discussed filers are actually known for.  Treat them as a
# directional signal on the ticker, never as a replication of the trade.
OPTION_TYPES = {"OP"}
SCOREABLE_WITH_OPTIONS = SCOREABLE_TYPES | OPTION_TYPES

MIN_TRADES_FOR_RANKING = 10
# Default holding period. Twelve months suits the disclosure lag (a median of
# ~39 days already eats a short horizon) and the long-dated options that show
# up in the largest positions.
DEFAULT_HORIZON = "12m"

# A fill must land close to the date we claim to have traded on.  If the nearest
# available price is weeks away the ticker was not trading then (listed later,
# delisted, or a symbol that now belongs to a different company), and scoring it
# would invent a trade that could not have happened.
MAX_ENTRY_GAP_DAYS = 10
MAX_EXIT_GAP_DAYS = 10


def _gap(a: str, b: str) -> int:
    return abs((dt.date.fromisoformat(a) - dt.date.fromisoformat(b)).days)


def _shift(date_iso: str, days: int) -> str:
    return (dt.date.fromisoformat(date_iso) + dt.timedelta(days=days)).isoformat()


def _pct(a: float, b: float) -> float | None:
    if a is None or b is None or a <= 0:
        return None
    return (b - a) / a


def score_trades(conn: sqlite3.Connection, lookup: PriceLookup) -> dict:
    """Compute forward returns and benchmark-relative alpha for every purchase."""
    rows = conn.execute(
        """SELECT id, ticker, asset_type, tx_date, notification_date, filing_date
             FROM transactions
            WHERE tx_type = 'purchase'
              AND ticker IS NOT NULL AND ticker <> ''
              AND tx_date IS NOT NULL"""
    ).fetchall()

    stats = {"candidates": len(rows), "scored": 0, "no_price": 0,
             "wrong_type": 0, "no_entry": 0, "quarantined": 0}
    conn.execute("DELETE FROM trade_performance")
    today = dt.date.today().isoformat()
    payload = []

    # Tickers whose price series contains impossible or unusable values. Dropped
    # here rather than in each consumer: nothing without a trade_performance row
    # reaches the strategy, the grade or the curve, so one check covers all
    # three. See prices.unusable_tickers for why the whole series goes.
    quarantined = prices_mod.unusable_tickers(conn)

    for r in rows:
        if (r["asset_type"] or "") not in SCOREABLE_WITH_OPTIONS:
            stats["wrong_type"] += 1
            continue
        if (r["asset_type"] or "") in OPTION_TYPES:
            stats["options"] = stats.get("options", 0) + 1
        ticker = normalize_ticker(r["ticker"])
        if ticker in quarantined:
            stats["quarantined"] += 1
            continue
        if not lookup.has(ticker):
            stats["no_price"] += 1
            continue

        # A copier learns of the trade when the filing posts; assume they can
        # act at the next session's open.
        public_date = r["filing_date"] or r["notification_date"] or r["tx_date"]
        public_date = _to_iso(public_date) or r["tx_date"]
        target_entry = _shift(public_date, 1)
        entry = lookup.first_on_or_after(ticker, target_entry)
        bench_entry = lookup.first_on_or_after(BENCHMARK, target_entry)
        if entry is None or bench_entry is None:
            stats["no_entry"] += 1
            continue

        entry_date, entry_price = entry
        if _gap(entry_date, target_entry) > MAX_ENTRY_GAP_DAYS:
            # The security was not trading around the disclosure date.
            stats["no_entry"] += 1
            continue
        _, bench_price = bench_entry

        record = {"transaction_id": r["id"], "entry_date": entry_date,
                  "entry_price": entry_price}
        for label, days in HORIZONS.items():
            target = _shift(entry_date, days)
            # An unfinished holding period must not be scored as a flat return.
            if target > today:
                record[f"ret_{label}"] = None
                record[f"bench_{label}"] = None
                continue
            exit_ = lookup.last_on_or_before(ticker, target)
            bench_exit = lookup.last_on_or_before(BENCHMARK, target)
            # A delisted ticker's last close can be months before the horizon
            # ends; scoring that as the horizon return would be wrong.
            if exit_ and _gap(exit_[0], target) > MAX_EXIT_GAP_DAYS:
                exit_ = None
            record[f"ret_{label}"] = _pct(entry_price, exit_[1]) if exit_ else None
            record[f"bench_{label}"] = _pct(bench_price, bench_exit[1]) if bench_exit else None

        # The member's own entry, on their trade date.  Nobody else could have
        # traded here, but comparing it with the copy entry is the only way to
        # see what the disclosure delay actually costs.
        record["member_ret_3m"] = None
        record["member_bench_3m"] = None

        # A handful of filings carry mistyped trade dates - "3031-04-30" for
        # 2021, "2220-04-07" for 2022.  The values are read faithfully from the
        # document, but a trade cannot happen after it was filed, so those rows
        # are excluded from the member-entry figure rather than trusted.
        plausible = (r["tx_date"] <= public_date
                     and _gap(r["tx_date"], public_date) <= 730)
        m_entry = lookup.first_on_or_after(ticker, r["tx_date"]) if plausible else None
        m_bench_entry = lookup.first_on_or_after(BENCHMARK, r["tx_date"]) if plausible else None
        if (m_entry and m_bench_entry
                and _gap(m_entry[0], r["tx_date"]) <= MAX_ENTRY_GAP_DAYS):
            m_target = _shift(m_entry[0], HORIZONS["3m"])
            if m_target <= today:
                m_exit = lookup.last_on_or_before(ticker, m_target)
                m_bench_exit = lookup.last_on_or_before(BENCHMARK, m_target)
                if m_exit and _gap(m_exit[0], m_target) <= MAX_EXIT_GAP_DAYS:
                    record["member_ret_3m"] = _pct(m_entry[1], m_exit[1])
                if m_bench_exit:
                    record["member_bench_3m"] = _pct(m_bench_entry[1], m_bench_exit[1])

        payload.append(record)
        stats["scored"] += 1

    # Scoring reads the transaction list, then writes rows that reference it.
    # If an ingest re-parsed a filing in between, those ids are gone and the
    # foreign key rejects the whole batch.  Drop the stale rows instead of
    # losing the run; a subsequent backtest picks them up.
    live_ids = {r[0] for r in conn.execute("SELECT id FROM transactions")}
    dropped = [p for p in payload if p["transaction_id"] not in live_ids]
    if dropped:
        payload = [p for p in payload if p["transaction_id"] in live_ids]
        stats["stale_skipped"] = len(dropped)
        stats["scored"] -= len(dropped)

    # Built from HORIZONS rather than spelled out, so adding a holding period
    # needs no SQL edit here.
    columns = (["transaction_id", "entry_date", "entry_price"]
               + [f"ret_{h}" for h in HORIZONS]
               + [f"bench_{h}" for h in HORIZONS]
               + ["member_ret_3m", "member_bench_3m"])
    conn.executemany(
        f"""INSERT INTO trade_performance ({', '.join(columns)})
            VALUES ({', '.join(':' + c for c in columns)})""",
        payload)
    conn.commit()
    return stats


def _to_iso(value: str | None) -> str | None:
    """Filing dates arrive as M/D/YYYY from the Clerk's index."""
    if not value:
        return None
    value = value.strip()
    if len(value) == 10 and value[4] == "-":
        return value
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return dt.datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def rank_politicians(conn: sqlite3.Connection) -> dict:
    """Aggregate scored trades per member into the leaderboard table."""
    import congress

    rows = conn.execute(
        """SELECT t.bioguide_id, t.member_name, t.tx_date, t.filing_date,
                  t.amount_mid, t.asset_type, t.ticker,
                  p.ret_1m, p.ret_3m, p.ret_6m, p.ret_12m,
                  p.bench_1m, p.bench_3m, p.bench_6m, p.bench_12m,
                  p.member_ret_3m, p.member_bench_3m,
                  COALESCE(t.tx_date_fixed, t.tx_date) AS tx_date_eff
             FROM transactions t
             JOIN trade_performance p ON p.transaction_id = t.id
            WHERE t.bioguide_id IS NOT NULL"""
    ).fetchall()

    # Ticker -> jurisdiction sectors, for the in-remit / out-of-remit split.
    ticker_sectors: dict[str, set[str]] = {}
    try:
        for r in conn.execute("SELECT ticker, sectors FROM ticker_meta"):
            ticker_sectors[r["ticker"]] = set(
                s for s in (r["sectors"] or "").split(",") if s)
    except sqlite3.OperationalError:
        pass

    snapshots = congress.snapshot_dates(conn)

    buckets: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        buckets[r["bioguide_id"]].append(r)

    buy_counts = {r["bioguide_id"]: r["n"] for r in conn.execute(
        """SELECT bioguide_id, COUNT(*) n FROM transactions
            WHERE tx_type='purchase' AND bioguide_id IS NOT NULL
            GROUP BY bioguide_id""")}

    # Disclosure lag describes how promptly a member files, so it is measured
    # over all of their purchases - not only the subset that could be scored,
    # which would make the figure depend on price-data coverage.
    lag_by_member: dict[str, list[int]] = defaultdict(list)
    for r in conn.execute(
            """SELECT bioguide_id, tx_date, filing_date FROM transactions
                WHERE tx_type='purchase' AND bioguide_id IS NOT NULL
                  AND tx_date IS NOT NULL AND filing_date IS NOT NULL"""):
        filed = _to_iso(r["filing_date"])
        if not filed:
            continue
        lag = (dt.date.fromisoformat(filed) - dt.date.fromisoformat(r["tx_date"])).days
        # A filing cannot predate its own trade. When it appears to, the filer
        # mistyped the year - "3031-04-30" for 2021, "2202" for 2022 - and one
        # such row drags a member's mean lag to minus three thousand days.
        # Long *positive* lags are kept: members really do file years late, and
        # that is exactly what this statistic exists to show.
        if lag < 0:
            continue
        lag_by_member[r["bioguide_id"]].append(lag)

    conn.execute("DELETE FROM politician_stats")
    written = 0
    for bioguide, trades in buckets.items():
        # Only the horizons politician_stats actually stores; the longer ones
        # are a per-trade outcome, not a member-level statistic.
        alphas = {}
        for label in ("1m", "3m", "6m", "12m"):
            alphas[label] = [
                r[f"ret_{label}"] - r[f"bench_{label}"] for r in trades
                if r[f"ret_{label}"] is not None and r[f"bench_{label}"] is not None]

        a3 = alphas["3m"]
        if not a3:
            continue

        lags = lag_by_member.get(bioguide, [])
        rets3 = [r["ret_3m"] for r in trades if r["ret_3m"] is not None]

        # Same trades, measured from the member's own entry, so the difference
        # isolates the disclosure delay rather than a change in sample.
        paired = [(r["ret_3m"] - r["bench_3m"],
                   r["member_ret_3m"] - r["member_bench_3m"])
                  for r in trades
                  if r["ret_3m"] is not None and r["bench_3m"] is not None
                  and r["member_ret_3m"] is not None and r["member_bench_3m"] is not None]
        member_alpha = statistics.fmean([m for _c, m in paired]) if paired else None
        lag_cost = (statistics.fmean([m - c for c, m in paired])
                    if paired else None)
        t_stat = None
        if len(a3) >= 2:
            # Sample standard deviation (n-1): these trades are a sample of the
            # member's behaviour, not the whole population.  pstdev would shrink
            # the denominator and overstate t precisely when n is small.
            sd = statistics.stdev(a3)
            if sd > 0:
                t_stat = (statistics.fmean(a3) / (sd / len(a3) ** 0.5))

        sensitivity, committee_names, member_sectors = congress.member_access_score(
            conn, bioguide)

        # Dollar-weighted alpha. Disclosed sizes run from $1k to $5m, so an
        # equal-weighted mean says what the average *filing* did, not what the
        # member's money did. Both are reported; they can disagree sharply.
        def weighted(label: str) -> float | None:
            num = den = 0.0
            for r in trades:
                ret, bench = r[f"ret_{label}"], r[f"bench_{label}"]
                w = r["amount_mid"] or 0
                if ret is None or bench is None or w <= 0:
                    continue
                num += (ret - bench) * w
                den += w
            return (num / den) if den else None

        # Did the member buy inside their own committee's remit?
        #
        # Counted per decision, not per filed line. The same stock bought on the
        # same day across several accounts is one judgement disclosed several
        # times; counting the rows would inflate a member's apparent record
        # exactly where they trade in blocks. Sizes are summed elsewhere, so the
        # extra rows still carry their money into the book.
        # Remit is resolved per trade, against the seat held on that date. Using
        # the member's *current* committees for their whole history credits them
        # with jurisdictions they gained later and strips ones they have since
        # left - and for a member who has left Congress entirely it would leave
        # them with no remit at all, hiding their record rather than reporting it.
        in_remit, out_remit, n_in = [], [], 0
        remit_at: dict = {}
        seen: set[tuple] = set()
        for r in trades:
            key = (r["ticker"], r["tx_date_eff"])
            if key in seen:
                continue
            seen.add(key)
            snap = congress.snapshot_at(snapshots, r["tx_date_eff"])
            if snap not in remit_at:
                remit_at[snap] = set(congress.member_access_score(
                    conn, bioguide, as_of=r["tx_date_eff"])[2])
            remit = remit_at[snap]
            a12 = (r["ret_12m"] - r["bench_12m"]
                   if r["ret_12m"] is not None and r["bench_12m"] is not None else None)
            overlap = bool(remit & ticker_sectors.get(r["ticker"] or "", set()))
            if overlap:
                n_in += 1
                if a12 is not None:
                    in_remit.append(a12)
            elif a12 is not None:
                out_remit.append(a12)

        a12 = alphas["12m"]
        t_12 = None
        if len(a12) >= 2:
            sd12 = statistics.stdev(a12)
            if sd12 > 0:
                t_12 = statistics.fmean(a12) / (sd12 / len(a12) ** 0.5)
        n_options = sum(1 for r in trades
                        if (r["asset_type"] or "") in OPTION_TYPES)

        conn.execute(
            """INSERT INTO politician_stats
               (bioguide_id, member_name, n_buys, n_scored, hit_rate, avg_ret_3m,
                avg_alpha_1m, avg_alpha_3m, avg_alpha_6m, avg_alpha_12m,
                median_alpha_3m, total_alpha_3m_t, avg_lag_days,
                avg_member_alpha_3m, lag_cost_3m, est_volume,
                sensitivity, committees,
                wt_alpha_3m, wt_alpha_12m, n_scored_12m, hit_rate_12m,
                alpha_12m_t, median_alpha_12m, n_options,
                n_in_remit, alpha_in_remit_12m, alpha_out_remit_12m)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?,?,?,?,?)""",
            (bioguide, trades[0]["member_name"],
             buy_counts.get(bioguide, len(trades)), len(a3),
             sum(1 for v in a3 if v > 0) / len(a3),
             statistics.fmean(rets3) if rets3 else None,
             statistics.fmean(alphas["1m"]) if alphas["1m"] else None,
             statistics.fmean(a3),
             statistics.fmean(alphas["6m"]) if alphas["6m"] else None,
             statistics.fmean(alphas["12m"]) if alphas["12m"] else None,
             statistics.median(a3), t_stat,
             statistics.fmean(lags) if lags else None,
             member_alpha, lag_cost,
             sum(r["amount_mid"] or 0 for r in trades),
             sensitivity, "; ".join(committee_names),
             weighted("3m"), weighted("12m"), len(a12),
             (sum(1 for v in a12 if v > 0) / len(a12)) if a12 else None,
             t_12, statistics.median(a12) if a12 else None, n_options,
             n_in, statistics.fmean(in_remit) if in_remit else None,
             statistics.fmean(out_remit) if out_remit else None))
        written += 1

    conn.commit()
    return {"members_ranked": written}


def copy_trade_curve(conn: sqlite3.Connection, lookup: PriceLookup,
                     bioguide_id: str | None = None,
                     horizon: str = DEFAULT_HORIZON,
                     weight: str = "equal") -> dict:
    """Equity curve for copying disclosed buys.

    A position opens at the first open after the filing goes public and is held
    for ``horizon``.  On any day the portfolio holds the open positions; with
    none open it sits in cash.  The benchmark is SPY bought and held over the
    identical window.

    ``weight='equal'`` treats every disclosed buy the same.  ``weight='size'``
    scales each position by the midpoint of its disclosed amount range, which
    is what actually following the money would look like - the two can diverge
    a long way, because most filings are small and a few are enormous.
    """
    hold_days = HORIZONS[horizon]
    params: list = []
    sql = """SELECT t.ticker, p.entry_date, t.amount_mid
               FROM transactions t
               JOIN trade_performance p ON p.transaction_id = t.id
              WHERE t.tx_type = 'purchase'"""
    if bioguide_id:
        sql += " AND t.bioguide_id = ?"
        params.append(bioguide_id)
    trades = conn.execute(sql, params).fetchall()
    if not trades:
        return {"dates": [], "strategy": [], "benchmark": [], "n_trades": 0}

    # Daily return series per ticker, computed once.
    daily: dict[str, dict[str, float]] = {}
    for ticker in {normalize_ticker(t["ticker"]) for t in trades} | {BENCHMARK}:
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

    # Map each trading day to the tickers held that day.  Locating each
    # position's window with bisect keeps this linear in the days actually
    # held, instead of rescanning the whole calendar for every trade.
    import bisect

    held: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for t in trades:
        ticker = normalize_ticker(t["ticker"])
        if ticker not in daily:
            continue
        w = 1.0
        if weight == "size":
            w = float(t["amount_mid"] or 0)
            if w <= 0:
                continue
        start, end = t["entry_date"], _shift(t["entry_date"], hold_days)
        lo = bisect.bisect_right(bench_dates, start)
        hi = bisect.bisect_right(bench_dates, end)
        for d in bench_dates[lo:hi]:
            held[d].append((ticker, w))

    start_date = min(t["entry_date"] for t in trades)
    dates, strat, bench = [], [], []
    equity, bench_equity = 1.0, 1.0
    for d in bench_dates:
        if d < start_date:
            continue
        names = held.get(d, [])
        day_ret = 0.0
        if names:
            total_w = sum(w for _n, w in names)
            if total_w > 0:
                day_ret = sum(daily[n].get(d, 0.0) * w for n, w in names) / total_w
        equity *= (1 + day_ret)
        bench_equity *= (1 + daily[BENCHMARK].get(d, 0.0))
        dates.append(d)
        strat.append(round(equity, 6))
        bench.append(round(bench_equity, 6))

    return {"dates": dates, "strategy": strat, "benchmark": bench,
            "n_trades": len(trades), "horizon": horizon, "weight": weight,
            "final_strategy": strat[-1] if strat else 1.0,
            "final_benchmark": bench[-1] if bench else 1.0}


def run(conn: sqlite3.Connection) -> dict:
    # Load prices only for tickers that were actually purchased.  The price
    # table runs to millions of rows across every symbol ever seen, and
    # materialising all of it costs about a gigabyte for no benefit.
    tickers = [r["ticker"] for r in conn.execute(
        """SELECT DISTINCT ticker FROM transactions
            WHERE tx_type = 'purchase' AND ticker IS NOT NULL AND ticker <> ''""")]
    lookup = PriceLookup(conn, tickers=tickers)
    scored = score_trades(conn, lookup)
    ranked = rank_politicians(conn)

    # Precompute the all-members curve while the price data is already in
    # memory.  Building it on demand costs about a minute, which is not an
    # acceptable first page load for the dashboard.
    import json

    import db as db_mod

    curves = {}
    for horizon in CURVE_HORIZONS:
        for weight in ("equal", "size"):
            key = horizon if weight == "equal" else f"{horizon}:size"
            curves[key] = copy_trade_curve(conn, lookup, None, horizon, weight)
    db_mod.set_meta(conn, "curves_all", json.dumps(curves))
    conn.commit()

    return {**scored, **ranked, "curves_cached": len(curves)}
