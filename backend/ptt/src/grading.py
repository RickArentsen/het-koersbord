"""Grade how much a disclosed trade looks like a deliberate, informed bet.

What this measures
------------------
Not whether a trade was illegal.  Not whether it made money.  It scores how
closely a trade resembles a *decision* rather than routine portfolio
maintenance, using only what the filing itself reveals.

The distinction that does most of the work is **isolation**.  Filing 20034906
is the clearest example in the dataset: Gilbert Cisneros disclosed 66
transactions at once, in alphabetical order, most of them on four shared dates
and all in the $1,001-$15,000 band.  That is an adviser rebalancing a managed
portfolio - nobody forms 66 individual convictions on a Tuesday.  A single
trade, filed alone, in an industry the member's own committee regulates, is a
completely different object, and the grade separates them.

The components
--------------
=========================  ======  ====================================
Signal                     Weight  Why it counts
=========================  ======  ====================================
In committee remit             32  The only committee link with measurable
                                   predictive value in this dataset.
Isolation                      22  A lone trade is a decision; one line of
                                   sixty-six is administration.
Obscurity                      10  Nobody needs inside information to buy
                                   Microsoft.  A $2.5bn space company is a
                                   different kind of purchase from a
                                   $131bn one.
New position                    8  A name the member has never held before
                                   is a decision; the ninth purchase of the
                                   same holding is a standing order.
Self-directed                   8  The member's own account, not a spouse's
                                   or a dependent child's.
Committee leadership            8  Chairs and ranking members are briefed
                                   first and set the agenda.
Unusual size for them           6  Measured against that member's *own*
                                   median trade, not an absolute threshold:
                                   $50k means something different from
                                   someone who normally trades $5k.
Position size                   3  Absolute dollars, kept small - see below.
Leveraged instrument            3  An option is a directional bet with a
                                   deadline, not a parking place.
=========================  ======  ====================================
                              100

Grades run A to E.  A high grade means "this trade has the shape of an informed,
deliberate position"; it is emphatically **not** an allegation.  Buying into an
industry you spend your working life on is ordinary expertise, and every input
here is public.

What the grade does *not* do
----------------------------
**It does not predict returns, and the site says so.**  Measured across 11,007
graded, settled decisions, A-graded trades averaged **-1.02%** alpha against
C-graded trades' **+4.87%**; A+B minus D+E came to -2.20 percentage points at
**t = -1.71**, and the rank correlation between score and alpha was **-0.016**.

Adding the three conviction components sharpened the *description* - the two
trades this was built to separate moved from 80/68.8 to 86.5/64.1 - without
improving prediction at all.  ``src/validate.py`` recomputes the test on every
run and the methodology page publishes whatever it finds, so the letter can never
quietly imply a forecast it cannot make.

Absolute position size was cut from 15 to 3 for the same reason: trades larger
than three times the member's own median averaged **-5.92%** alpha, so rewarding
raw bigness was scoring the wrong thing.  ``size_vs_self`` measures the same
instinct against the right baseline, and treats an outsized trade as *unusual*
rather than *better*.

Outcome - what the trade actually returned - stays out of the score entirely and
is reported alongside it, so the grade describes the decision rather than being
contaminated by hindsight.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from collections import defaultdict


def _cap_key(ticker: str) -> str:
    """market_meta stores Yahoo symbols (BRK.B -> BRK-B)."""
    return (ticker or "").strip().upper().replace(".", "-")


def market_caps_for(conn: sqlite3.Connection) -> dict[str, float]:
    """Market caps keyed by Yahoo symbol, empty if none have been fetched."""
    try:
        import market

        return market.market_caps(conn)
    except Exception:  # noqa: BLE001 - grading must work before `fundamentals` runs
        return {}

WEIGHTS = {
    "remit": 32,
    "isolation": 22,
    "obscurity": 10,
    "new_position": 8,
    "self_directed": 8,
    "leadership": 8,
    "size_vs_self": 6,
    "size": 3,
    "instrument": 3,
}

# The grade is a percentage and GRADE_BANDS is calibrated against a 100-point
# scale, so the weights have to sum to exactly that. Adding three components
# without re-balancing the old ones silently pushed the total to 115, inflating
# every score and nearly doubling the number of A grades before this caught it.
assert sum(WEIGHTS.values()) == 100, (
    f"grade weights must sum to 100, got {sum(WEIGHTS.values())}")

# Market cap bounds for the obscurity component, on a log scale. Below the floor
# a company is a micro-cap nobody covers; above the ceiling it is a name every
# index fund already owns.
CAP_FLOOR = 1e9        # $1bn
CAP_CEILING = 5e11     # $500bn

# How far above a member's own median a trade must sit to count as unusual for
# them. Beyond the upper bound it is capped: past a point an outsized trade is a
# portfolio event (an IPO, a vesting, a liquidation), not a louder conviction -
# and empirically those do worse, so the curve must not keep rewarding size.
SELF_SIZE_START = 1.0
SELF_SIZE_FULL = 3.0

# Above this many same-day trades in one filing, isolation scores nothing: the
# filing is plainly a bulk rebalance.
BULK_THRESHOLD = 20

# Position size is scored on a log scale between these two bounds, because the
# disclosed bands are themselves roughly logarithmic.
SIZE_FLOOR = 5_000.0
SIZE_CEILING = 1_000_000.0

GRADE_BANDS = [
    (75, "A", "Concentrated bet inside their own jurisdiction"),
    (60, "B", "Deliberate position with a committee link"),
    (45, "C", "Mixed signals"),
    (28, "D", "Looks like ordinary portfolio activity"),
    (0, "E", "Routine managed-account rebalancing"),
]

LEADERSHIP_TITLES = {"chairman", "chair", "chairwoman", "ranking member",
                     "vice chairman", "vice chair"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_grades (
    transaction_id  INTEGER PRIMARY KEY,
    score           REAL,
    grade           TEXT,
    label           TEXT,
    in_remit        INTEGER,
    cluster_size    INTEGER,
    components      TEXT,
    FOREIGN KEY(transaction_id) REFERENCES transactions(id)
);
CREATE INDEX IF NOT EXISTS ix_grade ON trade_grades(grade);
"""


def band(score: float) -> tuple[str, str]:
    for cutoff, letter, label in GRADE_BANDS:
        if score >= cutoff:
            return letter, label
    return "E", GRADE_BANDS[-1][2]


def _size_points(amount_mid: float | None) -> float:
    if not amount_mid or amount_mid <= 0:
        return 0.0
    lo, hi = math.log(SIZE_FLOOR), math.log(SIZE_CEILING)
    frac = (math.log(max(amount_mid, 1.0)) - lo) / (hi - lo)
    return WEIGHTS["size"] * max(0.0, min(1.0, frac))


def _isolation_points(cluster: int) -> float:
    """Full marks for a lone trade, nothing for one line of a bulk filing."""
    if cluster <= 1:
        return float(WEIGHTS["isolation"])
    frac = 1.0 - min(1.0, (cluster - 1) / BULK_THRESHOLD)
    return WEIGHTS["isolation"] * frac


def _obscurity_points(market_cap: float | None) -> float | None:
    """Small, thinly-followed companies score; mega-caps do not.

    Returns ``None`` when the cap is unknown, which is *not* the same as zero.
    A missing value must not quietly read as "obscure" - roughly one traded
    ticker in six is a bond, fund or delisted shell with no market cap at all,
    and scoring those full marks would invert the whole component.
    """
    if not market_cap or market_cap <= 0:
        return None
    lo, hi = math.log(CAP_FLOOR), math.log(CAP_CEILING)
    frac = (math.log(market_cap) - lo) / (hi - lo)
    # Inverted: the smaller the company, the higher the score.
    return WEIGHTS["obscurity"] * (1.0 - max(0.0, min(1.0, frac)))


def _self_size_points(amount_mid: float | None, member_median: float | None) -> float:
    """How outsized this trade is for this particular member.

    Ramps between :data:`SELF_SIZE_START` and :data:`SELF_SIZE_FULL` multiples of
    their own median and then stops. It is deliberately capped rather than
    open-ended: trades beyond 3x a member's median averaged -5.92% alpha, so
    treating ever-larger as ever-more-meaningful would be scoring noise.
    """
    if not amount_mid or not member_median or member_median <= 0:
        return 0.0
    ratio = amount_mid / member_median
    if ratio <= SELF_SIZE_START:
        return 0.0
    frac = (ratio - SELF_SIZE_START) / (SELF_SIZE_FULL - SELF_SIZE_START)
    return WEIGHTS["size_vs_self"] * max(0.0, min(1.0, frac))


def leadership_members(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute("SELECT bioguide_id, title FROM committee_memberships")
    return {r["bioguide_id"] for r in rows
            if (r["title"] or "").strip().lower() in LEADERSHIP_TITLES}


def grade_all(conn: sqlite3.Connection, progress: bool = True) -> dict:
    """Score every transaction that has a ticker, and store the result."""
    import congress

    conn.executescript(SCHEMA)

    ticker_sectors = {
        r["ticker"]: {s for s in (r["sectors"] or "").split(",") if s}
        for r in conn.execute("SELECT ticker, sectors FROM ticker_meta")
    }
    leaders = leadership_members(conn)

    # Ordered by date so "has this member bought this name before?" can be
    # answered from what precedes each row, not from the whole file.
    snapshots = congress.snapshot_dates(conn)
    rows = conn.execute(
        """SELECT id, doc_id, bioguide_id, ticker, asset_type, tx_type,
                  COALESCE(tx_date_fixed, tx_date) AS tx_date,
                  owner, amount_mid
             FROM transactions
            WHERE ticker IS NOT NULL AND ticker <> ''
         ORDER BY tx_date, id"""
    ).fetchall()

    # Cluster = trades sharing a filing *and* a trade date. Same-day blocks are
    # what a rebalance looks like; a filing spanning weeks is not one event.
    clusters: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        clusters[(r["doc_id"], r["tx_date"] or "")] += 1

    caps = market_caps_for(conn)

    # The median obscurity across tickers we *can* price, used to stand in for
    # the ones we cannot. Falls back to half marks when nothing is cached at
    # all, so grading still runs before `fundamentals` has ever been executed.
    known = [p for p in (_obscurity_points(caps.get(_cap_key(r["ticker"] or "")))
                         for r in rows) if p is not None]
    median_obscurity = (statistics.median(known) if known
                        else WEIGHTS["obscurity"] / 2)

    # A member's typical trade size, over their whole history. This is a
    # description of the member rather than a point-in-time signal, so using all
    # of their trades is right here - unlike the strategy's qualification gate,
    # nothing downstream trades on it.
    sizes: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r["bioguide_id"] and r["amount_mid"]:
            sizes[r["bioguide_id"]].append(float(r["amount_mid"]))
    medians = {bg: statistics.median(v) for bg, v in sizes.items() if v}

    seen_names: dict[str, set[str]] = defaultdict(set)

    remit_cache: dict[tuple, set[str]] = {}
    payload = []
    counts: dict[str, int] = defaultdict(int)
    no_cap = 0

    for r in rows:
        bg = r["bioguide_id"]
        # The remit that applied on the trade date, so a member is never graded
        # for sitting on a committee they had not joined.
        key = (bg, congress.snapshot_at(snapshots, r["tx_date"]))
        if bg and key not in remit_cache:
            _s, _n, sectors = congress.member_access_score(
                conn, bg, as_of=r["tx_date"])
            remit_cache[key] = set(sectors)
        remit = remit_cache.get(key, set())
        sectors = ticker_sectors.get(r["ticker"] or "", set())
        overlap = sorted(remit & sectors)

        cluster = clusters[(r["doc_id"], r["tx_date"] or "")]
        ticker = r["ticker"] or ""

        # First sight of this name for this member? Checked before recording it.
        is_new = bool(bg) and ticker not in seen_names[bg]
        if bg:
            seen_names[bg].add(ticker)

        comp = {
            "remit": float(WEIGHTS["remit"]) if overlap else 0.0,
            "isolation": _isolation_points(cluster),
            "self_directed": float(WEIGHTS["self_directed"]) if (r["owner"] or "") == "self" else 0.0,
            "leadership": float(WEIGHTS["leadership"]) if bg in leaders else 0.0,
            "new_position": float(WEIGHTS["new_position"]) if is_new else 0.0,
            "size_vs_self": _self_size_points(r["amount_mid"], medians.get(bg)),
            "size": _size_points(r["amount_mid"]),
            "instrument": float(WEIGHTS["instrument"]) if (r["asset_type"] or "") == "OP" else 0.0,
        }

        # Obscurity needs a market cap, and roughly a third of traded tickers
        # have none (bonds, funds, delisted shells). Missing values take the
        # median of the tickers that do - neutral imputation.
        #
        # The obvious alternative, dropping the component and scaling the rest
        # up to 100, is wrong: it would let an unknown-cap trade reach 100 while
        # a known mega-cap tops out at 90, so every unrecognised ticker would
        # outrank Microsoft purely for being unrecognised.
        obscurity = _obscurity_points(caps.get(_cap_key(ticker)))
        if obscurity is None:
            no_cap += 1
            obscurity = median_obscurity
            imputed = True
        else:
            imputed = False
        comp["obscurity"] = obscurity

        score = round(sum(comp.values()), 1)
        letter, label = band(score)
        counts[letter] += 1

        parts = [f"{k}={v:.1f}" for k, v in comp.items()]
        if imputed:
            # Recorded so the trade page can say the figure was estimated rather
            # than presenting an imputed number as if it were measured.
            parts.append("obscurity_imputed=1")
        payload.append({
            "transaction_id": r["id"], "score": score, "grade": letter,
            "label": label, "in_remit": int(bool(overlap)),
            "cluster_size": cluster, "components": ";".join(parts),
        })

    conn.execute("DELETE FROM trade_grades")
    conn.executemany(
        """INSERT INTO trade_grades
           (transaction_id, score, grade, label, in_remit, cluster_size, components)
           VALUES (:transaction_id, :score, :grade, :label, :in_remit,
                   :cluster_size, :components)""", payload)
    conn.commit()

    if progress:
        print("  grade distribution: "
              + ", ".join(f"{g}={counts[g]}" for g in "ABCDE"))
        if no_cap:
            print(f"  {no_cap:,} of {len(rows):,} trades had no market cap; "
                  f"obscurity used the median ({median_obscurity:.1f} of "
                  f"{WEIGHTS['obscurity']}) and is flagged as estimated on those "
                  f"trades. Run `fundamentals` to shrink this.")
    return {"graded": len(payload), "no_market_cap": no_cap,
            **{f"grade_{g}": counts[g] for g in "ABCDE"}}


def explain(components: str | None) -> list[dict]:
    """Turn the stored component string into rows for the UI."""
    if not components:
        return []
    parts = dict(p.split("=", 1) for p in components.split(";") if "=" in p)
    imputed = parts.pop("obscurity_imputed", None) == "1"
    out = []
    for key, val in parts.items():
        out.append({
            "key": key,
            "label": {
                "remit": "Inside their committee's jurisdiction",
                "isolation": "Isolated decision, not a bulk rebalance",
                "obscurity": "Small, thinly-followed company",
                "self_directed": "Member's own account",
                "leadership": "Committee chair or ranking member",
                "new_position": "A name they had never held before",
                "size_vs_self": "Unusually large for this member",
                "size": "Position size",
                "instrument": "Leveraged instrument (option)",
            }.get(key, key),
            "note": {
                "obscurity": ("No market cap on file for this security, so this "
                              "is the median across everything that has one - an "
                              "estimate, not a measurement."
                              if imputed else
                              "Nobody needs inside information to buy a mega-cap."),
                "new_position": "A first purchase is a decision; the ninth purchase "
                                "of the same holding is a standing order.",
                "size_vs_self": "Against their own median trade, not an absolute "
                                "threshold - and capped, because trades beyond 3x "
                                "did worse, not better.",
            }.get(key),
            "estimated": bool(imputed and key == "obscurity"),
            "points": float(val),
            "max": float(WEIGHTS.get(key, 0)),
        })
    # Heaviest component first, so the panel reads in order of what actually
    # moves the score rather than in whatever order the dict was built.
    out.sort(key=lambda c: (-c["max"], -c["points"]))
    return out
