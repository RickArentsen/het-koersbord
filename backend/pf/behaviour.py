"""
Cost drag and trading discipline.

Nothing here needs price history. It reads the ledger, which is exact, so
these figures are available even when every feed is down — which also makes
them the ones worth trusting most.

Two questions:

  WHAT DOES THIS COST?   Fees, the AutoFX spread and margin interest, as an
                         annual drag in basis points against the capital
                         actually at work. A euro total is easy to dismiss;
                         "95 bps a year against a 7.4% return" is not.

  AM I ANY GOOD AT       Hit rate is the metric people quote and it is close
  SELLING?               to meaningless alone — 90% winners is a disaster if
                         the 10% are ruinous. It is only reported here paired
                         with the payoff ratio and an expectancy in euros.
                         And with the disposition check: how long winners are
                         held versus losers, which is the one behavioural
                         number a broker will never show you.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from statistics import median
from typing import Optional

FLAT = 1e-6            # a quantity this small is zero; floats from CSV drift


# --------------------------------------------------------------------------
# round trips
# --------------------------------------------------------------------------

def round_trips(trades: list, disposals: list, names: dict[str, str]) -> dict:
    """
    Group the FIFO disposals into complete round trips.

    A round trip is one episode of owning something: quantity goes from zero,
    up, and back to zero. That is the unit an investor actually thinks in
    ("I bought Nvidia, later I sold out of Nvidia"), and it is not the same as
    a disposal — one sale can close five lots, and one name can be traded in
    and out four separate times. Counting disposals would flatter the hit rate
    of anyone who scales out of a winner in pieces.

    Positions still open are excluded from every statistic and counted
    separately, because including them would mean marking a live loser as a
    win the moment it is up.
    """
    by_isin: dict[str, list] = defaultdict(list)
    for t in trades:
        if t.isin:
            by_isin[t.isin].append(t)

    # each ISIN's ownership episodes: (start_dt, end_dt or None)
    episodes: dict[str, list] = {}
    for isin, ts in by_isin.items():
        ts = sorted(ts, key=lambda x: x.trade_dt)
        eps, qty, start = [], 0.0, None
        for t in ts:
            was_flat = abs(qty) < FLAT
            qty += (t.quantity or 0.0)
            if was_flat and abs(qty) >= FLAT:
                start = t.trade_dt
            elif abs(qty) < FLAT and start is not None:
                eps.append([start, t.trade_dt])
                start = None
        if start is not None:
            eps.append([start, None])          # still open
        episodes[isin] = eps

    trips, open_eps = [], 0
    dis_by_isin: dict[str, list] = defaultdict(list)
    for d in disposals:
        dis_by_isin[d.isin].append(d)

    for isin, eps in episodes.items():
        ds = sorted(dis_by_isin.get(isin, []), key=lambda d: d.close_dt)
        for start, end in eps:
            if end is None:
                open_eps += 1
                continue
            mine = [d for d in ds if start <= d.close_dt <= end]
            if not mine:
                continue
            cost = sum(d.cost_eur for d in mine)
            pnl = sum(d.pnl_eur for d in mine)
            # Holding period weighted by euros at risk, not a plain average:
            # closing 90% on day 5 and the tail on day 400 is a five-day trade
            # with a forgotten stub, not a 200-day conviction hold.
            wsum = sum(d.cost_eur for d in mine) or 1.0
            held = sum(d.holding_days * d.cost_eur for d in mine) / wsum
            trips.append({
                "isin": isin,
                "name": names.get(isin, isin),
                "opened": start.date().isoformat(),
                "closed": end.date().isoformat(),
                "held_days": round(held),
                "cost_eur": round(cost, 2),
                "pnl_eur": round(pnl, 2),
                "pnl_pct": round(pnl / cost * 100, 2) if cost else None,
                "fees_eur": round(sum(d.fees_eur for d in mine), 2),
                "sales": len(mine),
                "win": pnl > 0,
            })
    trips.sort(key=lambda t: t["closed"], reverse=True)
    return {"trips": trips, "still_open": open_eps}


def discipline(trips: list[dict], open_rows: list[dict] | None = None,
               total_realised_eur: float | None = None) -> dict:
    """Hit rate, payoff, expectancy and the disposition check."""
    if not trips:
        return {"enough": False, "n": 0,
                "why": "no completed round trips in the ledger yet"}

    wins = [t for t in trips if t["win"]]
    losses = [t for t in trips if not t["win"]]
    n = len(trips)
    hit = len(wins) / n

    avg_win = sum(t["pnl_eur"] for t in wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(t["pnl_eur"] for t in losses) / len(losses)) if losses else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else None
    expectancy = hit * avg_win - (1 - hit) * avg_loss

    # The break-even hit rate for the payoff actually achieved. This is the
    # number that turns a hit rate from a grade into a decision: at a 2.5x
    # payoff you only need to be right 29% of the time.
    breakeven = (1 / (1 + payoff)) if payoff else None

    win_days = [t["held_days"] for t in wins]
    loss_days = [t["held_days"] for t in losses]

    # Disposition effect: the tendency to sell winners early and hold losers.
    # Measured on medians — one forgotten position held for three years would
    # drag a mean anywhere.
    med_w = median(win_days) if win_days else None
    med_l = median(loss_days) if loss_days else None
    ratio = (med_l / med_w) if (med_w and med_l and med_w > 0) else None

    # The honest caveat: closed trips are a self-selected sample. If the open
    # book is full of deep losers, a fine hit rate on what was sold just means
    # the losers have not been sold yet.
    open_losers = open_winners = 0
    open_loss_eur = 0.0
    for r in (open_rows or []):
        u = r.get("unreal_eur") or 0.0
        if u < 0:
            open_losers += 1
            open_loss_eur += u
        elif u > 0:
            open_winners += 1

    # Realised P&L that is NOT in these statistics: money taken off a position
    # that is still open. Trimming a winner is a real gain, but it is not a
    # completed round trip, so it cannot be scored as a win or a loss without
    # pre-judging what the rest of the position does. Reported rather than
    # quietly dropped — otherwise these numbers appear not to reconcile with
    # the realised figure on the headline tiles.
    trip_pnl = sum(t["pnl_eur"] for t in trips)
    partial = (round(total_realised_eur - trip_pnl, 2)
               if total_realised_eur is not None else None)

    return {
        "enough": True,
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "hit_rate_pct": round(hit * 100, 1),
        "avg_win_eur": round(avg_win, 2),
        "avg_loss_eur": round(-avg_loss, 2),
        "payoff": round(payoff, 2) if payoff else None,
        "expectancy_eur": round(expectancy, 2),
        "breakeven_hit_pct": round(breakeven * 100, 1) if breakeven else None,
        "edge": (None if breakeven is None else round((hit - breakeven) * 100, 1)),
        "best": max(trips, key=lambda t: t["pnl_eur"]),
        "worst": min(trips, key=lambda t: t["pnl_eur"]),
        "median_hold_win": med_w,
        "median_hold_loss": med_l,
        "disposition_ratio": round(ratio, 2) if ratio else None,
        "total_realised_eur": round(trip_pnl, 2),
        "partial_sales_eur": partial,
        "open_winners": open_winners,
        "open_losers": open_losers,
        "open_unrealised_loss_eur": round(open_loss_eur, 2),
    }


# --------------------------------------------------------------------------
# cost drag
# --------------------------------------------------------------------------

def average_capital(series: list[dict]) -> Optional[float]:
    """
    Mean daily account value — the right denominator for a drag figure.

    Not net deposits: money deposited last month has not been at risk for the
    whole period, and dividing by it would overstate the drag. Not today's
    value either, which says nothing about the years before.
    """
    vals = [p["v"] for p in series if p.get("v")]
    return (sum(vals) / len(vals)) if vals else None


def cost_drag(fees_eur: float, autofx_eur: float, account_fees_eur: float,
              interest_eur: float, avg_capital: Optional[float],
              start: date, today: date,
              buys_eur: float = 0.0, sells_eur: float = 0.0,
              fallback_capital: Optional[float] = None,
              outside_fees_eur: float = 0.0) -> dict:
    """
    Friction as an annual rate, plus turnover.

    account_fees_eur already CONTAINS the trade commission — DEGIRO writes
    every fee to the cash ledger, and the Transactions.csv commission is the
    same money seen from the other side. Adding both would double count, so
    the account figure is authoritative and the trade commission is shown
    inside it as a breakdown line only.

    outside_fees_eur is the one fee the account never saw: what was paid on
    trades entered by hand. It is added, because nothing else contains it.
    """
    friction = (abs(account_fees_eur) + abs(outside_fees_eur)
                + abs(autofx_eur) + abs(interest_eur))
    years = max((today - start).days / 365.25, 1e-9)
    cap = avg_capital or fallback_capital

    ann_bps = (friction / cap / years * 10_000) if cap else None
    # Turnover: the smaller side of the two flows, because buying and never
    # selling is not turnover. Annualised against the same capital.
    turn = (min(abs(buys_eur), abs(sells_eur)) / cap / years) if cap else None

    return {
        "friction_eur": round(friction, 2),
        "commission_eur": round(abs(fees_eur), 2),
        "autofx_eur": round(abs(autofx_eur), 2),
        "account_fees_eur": round(abs(account_fees_eur), 2),
        "outside_fees_eur": round(abs(outside_fees_eur), 2),
        "interest_eur": round(abs(interest_eur), 2),
        "avg_capital_eur": round(cap, 2) if cap else None,
        "avg_capital_basis": "mean daily account value" if avg_capital
                             else ("net capital in" if fallback_capital else None),
        "years": round(years, 2),
        "drag_bps": round(ann_bps, 0) if ann_bps is not None else None,
        "drag_pct": round(ann_bps / 100, 2) if ann_bps is not None else None,
        "total_pct_of_capital": round(friction / cap * 100, 2) if cap else None,
        "turnover_pct": round(turn * 100, 0) if turn is not None else None,
        "buys_eur": round(abs(buys_eur), 2),
        "sells_eur": round(abs(sells_eur), 2),
    }


def drag_share_of_return(drag_bps: Optional[float], xirr_pct: Optional[float]) -> Optional[float]:
    """
    What share of the gross annual return went to friction.

    Only meaningful when the return is positive: expressing costs as a
    percentage of a negative return produces a number that looks like good
    news, which is why it is withheld rather than printed.
    """
    if drag_bps is None or xirr_pct is None:
        return None
    gross = xirr_pct + drag_bps / 100.0
    if gross <= 0:
        return None
    return round((drag_bps / 100.0) / gross * 100, 1)
