"""One-shot refresh: pull new filings, update prices, rescore, print alerts.

Intended for a scheduled task (Windows Task Scheduler / cron).  Only the current
year's index is re-fetched and only filings never seen before are downloaded, so
a daily run is cheap.

    python refresh.py                # current year
    python refresh.py --from 2024    # wider catch-up window
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import alerts as alerts_mod  # noqa: E402
import backtest as backtest_mod  # noqa: E402
import congress  # noqa: E402
import db as db_mod  # noqa: E402
import ingest  # noqa: E402
import prices as prices_mod  # noqa: E402
import sectors as sectors_mod  # noqa: E402

DATA = ROOT / "data"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    year_now = dt.date.today().year
    parser.add_argument("--from", dest="year_from", type=int, default=year_now)
    parser.add_argument("--to", dest="year_to", type=int, default=year_now)
    parser.add_argument("--no-rescore", action="store_true",
                        help="skip the price/backtest refresh")
    args = parser.parse_args()

    started = dt.datetime.now()
    conn = db_mod.connect(DATA / "tracker.db")

    print(f"[{started:%Y-%m-%d %H:%M}] refreshing {args.year_from}..{args.year_to}")

    roster = congress.load_into_db(conn, DATA / "cache", refresh=True)
    print(f"  roster: {roster['legislators']} members, {roster['memberships']} seats")

    total_new = 0
    for year in range(args.year_from, args.year_to + 1):
        # refresh_index=True: the current year's index gains rows continuously.
        stats = ingest.ingest_year(conn, year, DATA, refresh_index=True, progress=False)
        total_new += stats["downloaded"]
        print(f"  {year}: {stats['downloaded']} new filings, "
              f"{stats['transactions']} transactions, "
              f"{stats['no_text']} unreadable")
    ingest.backfill_bioguide(conn)

    if not args.no_rescore:
        pstats = prices_mod.sync_prices(conn, progress=False)
        print(f"  prices: +{pstats['resolved']} tickers, {pstats['rows']} rows")
        sstats = sectors_mod.sync_sectors(conn, progress=False)
        print(f"  sectors: +{sstats['resolved']} classified")
        import grading
        bstats = backtest_mod.run(conn)
        gstats = grading.grade_all(conn, progress=False)
        print(f"  grades: {gstats['graded']:,} trades scored A-E")
        print(f"  backtest: {bstats['scored']} trades scored, "
              f"{bstats['members_ranked']} members ranked")

    new_alerts = alerts_mod.detect_new_buys(conn, mark_seen=True)
    print(f"\n{len(new_alerts)} new buy alert(s)")
    for a in new_alerts[:25]:
        edge = ""
        if a.get("avg_alpha_3m") is not None and (a.get("n_scored") or 0) >= 5:
            edge = f"  [member avg 3m alpha {a['avg_alpha_3m'] * 100:+.1f}%, n={a['n_scored']}]"
        print(f"  {a['filing_date']}  {a['member_name']:30.30s} "
              f"{str(a['ticker'] or '-'):7.7s} {a['amount_raw']}{edge}")

    conn.close()
    print(f"\ndone in {(dt.datetime.now() - started).total_seconds():.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
