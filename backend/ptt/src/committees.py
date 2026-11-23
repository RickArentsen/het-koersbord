"""Per-committee statistics: does a given jurisdiction actually produce alpha?

The rest of the project asks "is this member worth following". This asks the
question one level up: **which committee's remit is the signal coming from?**
Armed Services and Financial Services are not equally informative, and pooling
them hides that.

Two things about the numbers here need stating before they are read.

Attribution is many-to-many
---------------------------
A member sits on several committees. A purchase of Lockheed Martin by someone on
both Armed Services and Appropriations is in remit for *both*, and is counted
under both. The committee columns therefore do not sum to the site total, and
they are not a partition of the trades — they answer "what happened under this
jurisdiction", not "who gets the credit".

One decision counts once
------------------------
Same rule as :func:`strategy.decision_key`: a member who buys the same stock on
the same day across four accounts made one judgement, so the counters and the
mean alpha see it once. Dollar volume still sums every line, because the
duplication is real money.
"""

from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict

# A committee needs this many settled in-remit decisions before its alpha is
# worth printing as anything but a curiosity. Below it the mean is noise.
MIN_DECISIONS_FOR_ALPHA = 10


def _sector_set(raw: str | None) -> set[str]:
    return {s for s in (raw or "").split(",") if s}


def load_committees(conn: sqlite3.Connection, full_only: bool = True) -> list[dict]:
    """Every committee row, with its sectors already split."""
    where = "WHERE parent_id IS NULL OR parent_id = ''" if full_only else ""
    return [
        {"committee_id": r["committee_id"], "name": r["name"],
         "chamber": r["chamber"], "sensitivity": r["sensitivity"],
         "sectors": sorted(_sector_set(r["sectors"])),
         "parent_id": r["parent_id"]}
        for r in conn.execute(
            f"SELECT committee_id, name, chamber, parent_id, sensitivity, sectors "
            f"FROM committees {where} ORDER BY chamber, name")
    ]


def _short(name: str) -> str:
    """'House Committee on Armed Services' -> 'Armed Services'."""
    for prefix in ("House Permanent Select Committee on ",
                   "Senate Select Committee on ", "House Select Committee on ",
                   "Senate Special Committee on ", "House Committee on ",
                   "Senate Committee on ", "Joint Committee on ",
                   "Joint Committee of Congress on the ", "Commission on ",
                   "United States Senate Caucus on "):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    # "House Committee on the Judiciary" would otherwise shorten to "the
    # Judiciary" and sort under T.
    return name[4:] if name.startswith("the ") else name


def stats(conn: sqlite3.Connection, tracked_ids: set[str] | None = None,
          min_decisions: int = MIN_DECISIONS_FOR_ALPHA) -> list[dict]:
    """One row per full committee, with membership and in-remit trading stats.

    ``tracked_ids`` are the members on the site's roster, so each committee can
    report how much of the tracked strategy it actually underwrites.
    ``min_decisions`` is the evidence bar below which alpha is withheld.
    """
    tracked_ids = tracked_ids or set()
    committees = load_committees(conn, full_only=True)

    # Seats on each committee, full committees only. Subcommittee seats are
    # inherited detail — every subcommittee member also holds the parent seat —
    # so counting them would double-count the same person.
    seats: dict[str, list[str]] = defaultdict(list)
    for r in conn.execute(
            """SELECT cm.committee_id, cm.bioguide_id
                 FROM committee_memberships cm
                 JOIN committees c ON c.committee_id = cm.committee_id
                WHERE c.parent_id IS NULL OR c.parent_id = ''"""):
        seats[r["committee_id"]].append(r["bioguide_id"])

    n_subs: dict[str, int] = defaultdict(int)
    for r in conn.execute(
            """SELECT parent_id, COUNT(*) n FROM committees
                WHERE parent_id IS NOT NULL AND parent_id <> '' GROUP BY parent_id"""):
        n_subs[r["parent_id"]] = r["n"]

    ticker_sectors = {r["ticker"]: _sector_set(r["sectors"])
                      for r in conn.execute("SELECT ticker, sectors FROM ticker_meta")}

    # Every scored purchase, with the buyer and the outcome.
    purchases = conn.execute(
        """SELECT t.bioguide_id, t.member_name, t.ticker, COALESCE(t.tx_date_fixed, t.tx_date) AS tx_date, t.tx_date AS tx_date_filed, t.tx_date_note, t.amount_mid,
                  p.ret_12m, p.bench_12m
             FROM transactions t
             JOIN trade_performance p ON p.transaction_id = t.id
            WHERE t.tx_type = 'purchase' AND t.bioguide_id IS NOT NULL
              AND t.ticker IS NOT NULL"""
    ).fetchall()

    by_member: dict[str, list] = defaultdict(list)
    for r in purchases:
        by_member[r["bioguide_id"]].append(r)

    member_names = {r["bioguide_id"]: r["member_name"] for r in conn.execute(
        "SELECT bioguide_id, member_name FROM politician_stats")}

    out = []
    for c in committees:
        remit = set(c["sectors"])
        members = seats.get(c["committee_id"], [])
        on_roster = [b for b in members if b in tracked_ids]

        alphas: list[float] = []
        volume = 0.0
        traders: set[str] = set()
        ticker_hits: dict[str, int] = defaultdict(int)
        seen: set[tuple] = set()

        for bg in members:
            for r in by_member.get(bg, []):
                if not (remit & ticker_sectors.get(r["ticker"], set())):
                    continue
                # Volume counts every filed line: four accounts is four times
                # the money, even though it is one decision.
                volume += r["amount_mid"] or 0
                key = (bg, r["ticker"], r["tx_date"])
                if key in seen:
                    continue
                seen.add(key)
                traders.add(bg)
                ticker_hits[r["ticker"]] += 1
                if r["ret_12m"] is not None and r["bench_12m"] is not None:
                    alphas.append(r["ret_12m"] - r["bench_12m"])

        enough = len(alphas) >= min_decisions
        out.append({
            **c,
            "short_name": _short(c["name"]),
            "n_seats": len(members),
            "n_subcommittees": n_subs.get(c["committee_id"], 0),
            "n_tracked": len(on_roster),
            "tracked": sorted(
                ({"bioguide_id": b,
                  "name": (member_names.get(b) or b).replace("Hon. ", "")}
                 for b in on_roster), key=lambda m: m["name"]),
            "n_decisions": len(seen),
            "n_settled": len(alphas),
            "n_traders": len(traders),
            "volume": volume,
            # Below the threshold the mean is noise, so it is withheld rather
            # than printed with a caveat nobody reads.
            "alpha_12m": statistics.fmean(alphas) if enough else None,
            "median_alpha_12m": statistics.median(alphas) if enough else None,
            "hit_rate": (sum(1 for a in alphas if a > 0) / len(alphas)
                         if enough else None),
            "top_tickers": [t for t, _n in sorted(
                ticker_hits.items(), key=lambda kv: -kv[1])[:6]],
        })
    return out


def summarise_trades(trades: list[dict]) -> dict:
    """Roll a committee's in-remit purchases up the same way :func:`stats` does.

    This exists so the committee index and the committee page cannot disagree.
    They did: the index averaged decisions while the page averaged filed lines,
    and House Financial Services read +1.8% in one place and -0.5% in the other.
    """
    seen: set[tuple] = set()
    settled: list[float] = []
    for x in trades:
        key = (x["bioguide_id"], x["ticker"], x["tx_date"])
        if key in seen:
            continue
        seen.add(key)
        if x.get("alpha_12m") is not None:
            settled.append(x["alpha_12m"])
    return {
        "n_trades": len(trades),
        "n_decisions": len(seen),
        "n_settled": len(settled),
        # Volume counts every line: four accounts is four times the money.
        "volume": sum(x.get("amount_mid") or 0 for x in trades),
        "alpha_12m": statistics.fmean(settled) if settled else None,
        "median_alpha_12m": statistics.median(settled) if settled else None,
        "hit_rate": (sum(1 for a in settled if a > 0) / len(settled)
                     if settled else None),
    }


def totals(rows: list[dict]) -> dict:
    """Headline counters for the page, and the chamber split."""
    by_chamber: dict[str, int] = defaultdict(int)
    for r in rows:
        by_chamber[r["chamber"]] += 1
    scored = [r for r in rows if r["alpha_12m"] is not None]

    # Does the hand-assigned jurisdiction weight predict anything? This is the
    # one number on the page that can embarrass the method, so it is computed
    # rather than asserted.
    corr = None
    if len(scored) >= 3:
        xs = [r["sensitivity"] for r in scored]
        ys = [r["alpha_12m"] for r in scored]
        if len(set(xs)) > 1 and len(set(ys)) > 1:
            corr = statistics.correlation(xs, ys)

    return {
        "n_committees": len(rows),
        "by_chamber": dict(sorted(by_chamber.items())),
        "n_subcommittees": sum(r["n_subcommittees"] for r in rows),
        "n_seats": sum(r["n_seats"] for r in rows),
        "n_scored": len(scored),
        "n_no_remit": sum(1 for r in rows if not r["sectors"]),
        "n_with_tracked": sum(1 for r in rows if r["n_tracked"]),
        "min_decisions": MIN_DECISIONS_FOR_ALPHA,
        "best": max(scored, key=lambda r: r["alpha_12m"]) if scored else None,
        "worst": min(scored, key=lambda r: r["alpha_12m"]) if scored else None,
        "weight_alpha_corr": corr,
        "sectors": sorted({s for r in rows for s in r["sectors"]}),
    }


def detail(conn: sqlite3.Connection, committee_id: str,
           tracked_ids: set[str] | None = None) -> dict | None:
    """Everything the committee page needs: roster, subcommittees, trades."""
    tracked_ids = tracked_ids or set()
    row = conn.execute(
        "SELECT committee_id, name, chamber, parent_id, sensitivity, sectors "
        "FROM committees WHERE committee_id = ?", (committee_id,)).fetchone()
    if row is None:
        return None

    remit = _sector_set(row["sectors"])
    members = conn.execute(
        """SELECT cm.bioguide_id, cm.title, cm.rank, l.first_name, l.last_name,
                  l.party, l.state, l.district, l.chamber,
                  s.n_in_remit, s.alpha_in_remit_12m, s.avg_lag_days
             FROM committee_memberships cm
        LEFT JOIN legislators l ON l.bioguide_id = cm.bioguide_id
        LEFT JOIN politician_stats s ON s.bioguide_id = cm.bioguide_id
            WHERE cm.committee_id = ?
         ORDER BY cm.rank, l.last_name""", (committee_id,)).fetchall()

    ticker_sectors = {r["ticker"]: _sector_set(r["sectors"])
                      for r in conn.execute("SELECT ticker, sectors FROM ticker_meta")}
    ids = [m["bioguide_id"] for m in members]
    trades = []
    if ids:
        marks = ",".join("?" * len(ids))
        for r in conn.execute(
                f"""SELECT t.id, t.bioguide_id, t.member_name, t.ticker, COALESCE(t.tx_date_fixed, t.tx_date) AS tx_date, t.tx_date AS tx_date_filed, t.tx_date_note,
                           t.filing_date, t.amount_raw, t.amount_mid, t.tx_type,
                           p.ret_12m, p.bench_12m
                      FROM transactions t
                      JOIN trade_performance p ON p.transaction_id = t.id
                     WHERE t.bioguide_id IN ({marks}) AND t.tx_type = 'purchase'
                       AND t.ticker IS NOT NULL
                  ORDER BY COALESCE(t.tx_date_fixed, t.tx_date) DESC""", ids):
            if not (remit & ticker_sectors.get(r["ticker"], set())):
                continue
            d = dict(r)
            d["alpha_12m"] = (r["ret_12m"] - r["bench_12m"]
                              if r["ret_12m"] is not None
                              and r["bench_12m"] is not None else None)
            d["tracked"] = r["bioguide_id"] in tracked_ids
            d["grade"] = None
            trades.append(d)

        # Grades are attached separately because `trade_grades` is created by
        # the grading step, not the base schema: a database that has been
        # ingested and scored but not yet graded must still render this page
        # rather than 500 on a missing table.
        try:
            grades = {r["transaction_id"]: r["grade"] for r in conn.execute(
                "SELECT transaction_id, grade FROM trade_grades")}
        except sqlite3.OperationalError:
            grades = {}
        for d in trades:
            d["grade"] = grades.get(d["id"])

    subs = [dict(r) for r in conn.execute(
        """SELECT committee_id, name, sensitivity FROM committees
            WHERE parent_id = ? ORDER BY name""", (committee_id,))]

    return {
        "committee_id": row["committee_id"], "name": row["name"],
        "short_name": _short(row["name"]), "chamber": row["chamber"],
        "sensitivity": row["sensitivity"], "sectors": sorted(remit),
        "parent_id": row["parent_id"],
        "members": [dict(m) for m in members],
        "n_tracked": sum(1 for m in members if m["bioguide_id"] in tracked_ids),
        "subcommittees": subs,
        "trades": trades,
    }
