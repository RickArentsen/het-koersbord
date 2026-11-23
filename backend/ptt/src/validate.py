"""Does the A-E grade predict anything? Measured, cached, and published.

The grade describes the *shape* of a trade - isolated, in remit, in an obscure
name, unusual for that member. It is easy to read the letter as a forecast, so
this module tests that reading directly and the methodology page prints the
answer whether it flatters the grade or not.

The honest result as of the last run: **it does not predict returns.** A-graded
trades averaged +3.87% alpha against C-graded trades' +4.30%, and A+B beat D+E
by 1.41 percentage points at t = 0.90 - nowhere near significant. Score deciles
wander in both directions.

That is not a reason to delete the grade. "Which trades look like decisions
rather than administration" is a different question from "which trades made
money", and the first is the one this project can actually answer from filings.
It *is* a reason to never let the letter imply the second, which is what this
module exists to prevent.

Everything here counts one decision once, using
:func:`strategy.decision_key` - the same rule as the qualification gate, so the
numbers on this page and the roster cannot drift apart.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import statistics
from collections import defaultdict

CACHE_KEY = "grade_validation_v1"
GRADES = ("A", "B", "C", "D", "E")
N_DECILES = 10


def _welch_t(a: list[float], b: list[float]) -> float | None:
    """Welch's t: the two groups differ hugely in size and spread."""
    if len(a) < 2 or len(b) < 2:
        return None
    va, vb = statistics.variance(a), statistics.variance(b)
    se = (va / len(a) + vb / len(b)) ** 0.5
    if se == 0:
        return None
    return (statistics.fmean(a) - statistics.fmean(b)) / se


def _summarise(alphas: list[float]) -> dict:
    if not alphas:
        return {"n": 0, "mean": None, "median": None, "hit": None}
    return {
        "n": len(alphas),
        "mean": statistics.fmean(alphas),
        "median": statistics.median(alphas),
        "hit": sum(1 for a in alphas if a > 0) / len(alphas),
    }


def grade_vs_outcome(conn: sqlite3.Connection) -> dict:
    """Alpha by grade letter and by score decile, plus the A+B vs D+E test."""
    from strategy import decision_key

    rows = conn.execute(
        """SELECT g.grade, g.score, t.bioguide_id, t.ticker, t.tx_date,
                  p.ret_12m, p.bench_12m
             FROM trade_grades g
             JOIN transactions t ON t.id = g.transaction_id
             JOIN trade_performance p ON p.transaction_id = t.id
            WHERE t.tx_type = 'purchase'
              AND p.ret_12m IS NOT NULL AND p.bench_12m IS NOT NULL"""
    ).fetchall()

    seen: set[tuple] = set()
    by_grade: dict[str, list[float]] = defaultdict(list)
    scored: list[tuple[float, float]] = []
    for r in rows:
        key = decision_key(r)
        if key in seen:
            continue
        seen.add(key)
        alpha = r["ret_12m"] - r["bench_12m"]
        by_grade[r["grade"]].append(alpha)
        scored.append((r["score"], alpha))

    letters = [{"grade": g, **_summarise(by_grade.get(g, []))} for g in GRADES]

    top = by_grade.get("A", []) + by_grade.get("B", [])
    bottom = by_grade.get("D", []) + by_grade.get("E", [])
    t_stat = _welch_t(top, bottom)
    gap = (statistics.fmean(top) - statistics.fmean(bottom)) if top and bottom else None

    scored.sort(key=lambda x: x[0])
    deciles = []
    size = len(scored) // N_DECILES
    if size:
        for i in range(N_DECILES):
            chunk = scored[i * size:(i + 1) * size] if i < N_DECILES - 1 \
                else scored[(N_DECILES - 1) * size:]
            if chunk:
                deciles.append({
                    "decile": i + 1,
                    "score_lo": chunk[0][0], "score_hi": chunk[-1][0],
                    **_summarise([a for _s, a in chunk]),
                })

    # Rank correlation between score and alpha. Spearman rather than Pearson:
    # alpha has fat tails and a handful of 200% winners would otherwise decide
    # the answer on their own.
    rho = _spearman([s for s, _a in scored], [a for _s, a in scored])

    return {
        "n_decisions": len(seen),
        "by_grade": letters,
        "deciles": deciles,
        "top_minus_bottom": gap,
        "t_stat": t_stat,
        "spearman": rho,
        "predictive": bool(t_stat is not None and abs(t_stat) >= 2.0),
    }


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None

    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                out[order[k]] = avg
            i = j + 1
        return out

    rx, ry = ranks(xs), ranks(ys)
    try:
        return statistics.correlation(rx, ry)
    except statistics.StatisticsError:
        return None


def build(conn: sqlite3.Connection) -> dict:
    """Compute and cache the validation, like strategy.build does."""
    import json

    import db as db_mod

    payload = {"built_at": dt.datetime.now().isoformat(timespec="seconds"),
               **grade_vs_outcome(conn)}
    db_mod.set_meta(conn, CACHE_KEY, json.dumps(payload, default=str))
    conn.commit()
    return payload


def load(conn: sqlite3.Connection) -> dict | None:
    import json

    import db as db_mod

    raw = db_mod.get_meta(conn, CACHE_KEY)
    return json.loads(raw) if raw else None


def report(payload: dict) -> str:
    """Human-readable summary for the CLI."""
    lines = [f"{payload['n_decisions']:,} graded, settled decisions", ""]
    lines.append(f"{'grade':7}{'n':>7}{'mean':>10}{'median':>10}{'hit':>7}")
    lines.append("-" * 41)
    for g in payload["by_grade"]:
        if not g["n"]:
            continue
        lines.append(f"{g['grade']:7}{g['n']:>7}{g['mean']*100:>9.2f}%"
                     f"{g['median']*100:>9.2f}%{g['hit']*100:>6.0f}%")
    gap, t = payload["top_minus_bottom"], payload["t_stat"]
    if gap is not None and t is not None:
        lines += ["", f"A+B minus D+E: {gap*100:+.2f}pp   t = {t:.2f}"]
    if payload.get("spearman") is not None:
        lines.append(f"score vs alpha, Spearman rho = {payload['spearman']:+.3f}")
    lines.append("")
    lines.append("VERDICT: the grade predicts returns."
                 if payload["predictive"] else
                 "VERDICT: the grade does NOT predict returns - it describes trade "
                 "shape, and the site says so.")
    return "\n".join(lines)
