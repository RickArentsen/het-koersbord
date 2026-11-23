#!/usr/bin/env python3
"""Daily scan for new congressional trade filings. Built to run unattended.

Checks the Clerk's index for filings this tracker has not seen, parses the new
ones, refreshes prices and scores, rebuilds the site's cached strategy, and
reports any new trades by tracked members.

    python scripts/daily_scan.py                 # normal daily run
    python scripts/daily_scan.py --dry-run       # look, change nothing
    python scripts/daily_scan.py --notify-webhook https://...

Designed for cron or a systemd timer on a small VPS:

    # crontab -e   — 06:30 daily, log to file
    30 6 * * *  cd /srv/tracker && /srv/tracker/.venv/bin/python scripts/daily_scan.py >> /var/log/tracker.log 2>&1

Exit codes:  0 nothing new · 10 new tracked trades found · 1 error.
A non-zero-but-not-1 code is deliberate, so a wrapper can act on "found
something" without treating it as a failure.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import traceback
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

EXIT_OK = 0
EXIT_FOUND = 10
EXIT_ERROR = 1

STATE_FILE = ROOT / "data" / "daily_scan_state.json"


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_state() -> dict:
    """Read the seen-set.

    Keyed on ``strategy.line_key`` (``"<doc_id>:<seq>"``).  The earlier version
    stored ``transactions.id``, which is reminted whenever a filing is re-parsed
    - a single scan reported 344 "new" trades, most of them years old, because
    of it.  A state file in the old format is discarded rather than translated:
    it is a cache of what has been *announced*, and one quiet run rebuilds it.
    """
    empty = {"seen_keys": [], "last_run": None}
    if not STATE_FILE.exists():
        return empty
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return empty
    if "seen_keys" not in state:
        log("state file uses the old transaction-id format; rebuilding it")
        return {"seen_keys": [], "last_run": state.get("last_run")}
    return state


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def notify(webhook: str, text: str, blocks: list[dict] | None = None) -> None:
    """POST a plain JSON payload. Works with Slack and Discord webhooks."""
    payload = {"text": text}
    if blocks:
        payload["attachments"] = blocks
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            log(f"notified webhook ({resp.status})")
    except (urllib.error.URLError, OSError) as exc:
        log(f"WARNING: webhook failed: {exc}")


def seed_baseline(conn, congress, strat_mod, seen: set) -> None:
    """Record every tracked trade already on file, announcing none of them.

    Establishing what "already known" means, so that the next run can report a
    genuinely new trade instead of the whole backlog.
    """
    payload = strat_mod.load(conn)
    if not payload:
        log("no cached strategy yet; baseline deferred to the next run")
        return
    remit = {}
    for m in payload["members"]:
        _sc, _nm, sec = congress.member_access_score(conn, m["bioguide_id"])
        remit[m["bioguide_id"]] = set(sec)
    existing = strat_mod.tracked_activity(
        conn, [m["bioguide_id"] for m in payload["members"]], remit, limit=5000)
    seen.update(strat_mod.line_key(a) for a in existing)
    log(f"baseline established: {len(seen)} known trades, none announced")


def catch_up_needed(conn, strat_mod) -> str | None:
    """Is there ingested data the cached strategy has never seen?

    The rebuild is normally gated on "did *this* run ingest anything", which
    silently assumes every run reaches the end. One that dies between the
    ingest and the rebuild - a Ctrl+C, a reboot, an OOM kill - leaves filings
    in the database that no payload knows about, and the next run finds nothing
    pending and returns early. The site then serves data that is provably
    behind its own database until the next filing happens to arrive, which can
    be days.

    Comparing the two timestamps instead of trusting the control flow makes the
    scan self-healing.

    The timezones differ and it matters. ``filings.ingested_at`` defaults to
    SQLite's ``datetime('now')``, which is **UTC**; ``built_at`` comes from
    ``datetime.now()``, which is **local**. Comparing them raw is wrong by the
    UTC offset in the direction that hides the problem - a local timestamp
    reads ahead of a UTC one, so a stale payload looks fresh. Verified: the
    interrupted run of 2026-08-13 ingested at 22:37 UTC against a payload
    stamped 15:53 local, which compares as newer unless converted.
    """
    row = conn.execute("SELECT MAX(ingested_at) FROM filings").fetchone()
    if not row or not row[0]:
        return None
    payload = strat_mod.load(conn)
    if not payload or not payload.get("built_at"):
        return None
    try:
        newest = (dt.datetime.strptime(row[0], "%Y-%m-%d %H:%M:%S")
                  .replace(tzinfo=dt.timezone.utc)
                  .astimezone()
                  .replace(tzinfo=None))
        built = dt.datetime.fromisoformat(payload["built_at"])
    except ValueError:
        return None
    if newest <= built:
        return None
    return (f"the newest filing was ingested {newest:%Y-%m-%d %H:%M}, after the "
            f"cached strategy was built {built:%Y-%m-%d %H:%M}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--year", type=int, default=None,
                    help="filing year to scan (default: current year)")
    ap.add_argument("--catch-up", type=int, default=0, metavar="N",
                    help="also rescan the N previous years")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what is new without writing or rescoring")
    ap.add_argument("--email", action="store_true",
                    help="send the subscriber digest after the rebuild")
    ap.add_argument("--mailer", default="file",
                    choices=["file", "console", "smtp", "resend"])
    ap.add_argument("--notify-webhook", default=os.environ.get("TRACKER_WEBHOOK", ""),
                    help="Slack/Discord webhook for new tracked trades")
    ap.add_argument("--skip-prices", action="store_true",
                    help="skip the price/sector refresh (faster, staler)")
    ap.add_argument("--skip-senate", action="store_true",
                    help="do not attempt efdsearch.senate.gov")
    ap.add_argument("--senate-days", type=int, default=45, metavar="N",
                    help="how far back to re-check Senate filings (default 45)")
    args = ap.parse_args()

    started = dt.datetime.now()
    year = args.year or started.year
    years = list(range(year - args.catch_up, year + 1))

    import congress
    import db as db_mod
    import house
    import ingest
    import strategy as strat_mod

    conn = db_mod.connect(ROOT / "data" / "tracker.db")
    state = load_state()
    seen = set(state.get("seen_keys", []))
    # No baseline means we cannot tell new from merely unseen, so this run
    # records everything and announces nothing. That is true on a fresh install,
    # after the state file is deleted, and after the seen-set format changes -
    # all three used to be distinguished by `last_run is None`, which was wrong
    # for the last two and would have alerted on the entire backlog.
    baseline = not seen

    try:
        # ---- 1. what does the Clerk have that we do not? -------------------
        known = {r["doc_id"] for r in conn.execute("SELECT doc_id FROM filings")}
        pending = []
        for y in years:
            records = house.fetch_index(y, ROOT / "data" / "raw", refresh=True)
            ptrs = house.periodic_transaction_reports(records)
            new = [r for r in ptrs if r.doc_id not in known]
            log(f"{y}: {len(ptrs)} PTRs on file, {len(new)} not yet ingested")
            pending.extend(new)

        # A quiet index does not mean there is nothing to do: a previous run may
        # have ingested filings and died before rebuilding. Check the data
        # rather than the control flow.
        catch_up = None if args.dry_run else catch_up_needed(conn, strat_mod)

        if not pending and not catch_up:
            log("no new filings")
            # Seed the seen-set even on a quiet run. Without this the first run
            # that *does* find filings would treat the entire existing backlog
            # as new and alert on hundreds of stale trades.
            if not args.dry_run:
                if baseline:
                    seed_baseline(conn, congress, strat_mod, seen)
                    state["seen_keys"] = sorted(seen)[-20000:]
                state["last_run"] = started.isoformat(timespec="seconds")
                save_state(state)
            return EXIT_OK

        if not pending:
            log(f"no new filings, but {catch_up}")
            log("rebuilding to catch up - a previous run must have been "
                "interrupted before it finished")

        if args.dry_run:
            log(f"DRY RUN — would ingest {len(pending)} filing(s):")
            for r in pending[:25]:
                log(f"    {r.doc_id}  {r.display_name}  filed {r.filing_date}")
            return EXIT_FOUND

        # ---- 2. ingest, refresh, rescore ------------------------------------
        total_new = 0
        if pending:
            congress.load_into_db(conn, ROOT / "data" / "cache", refresh=True)
            for y in years:
                stats = ingest.ingest_year(conn, y, ROOT / "data",
                                           refresh_index=True, progress=False)
                total_new += stats["downloaded"]
                log(f"{y}: +{stats['downloaded']} filings, "
                    f"{stats['transactions']} transactions, "
                    f"{stats['no_text']} unreadable, {stats['errors']} errors")
            ingest.backfill_bioguide(conn)

        # Senate, if this host can reach it. Unreachable is not an error: the
        # House pipeline is complete on its own, and some networks block
        # senate.gov outright.
        if pending and not args.skip_senate:
            try:
                import senate
                import senate_ingest
                since = (started - dt.timedelta(days=args.senate_days)).strftime("%m/%d/%Y")
                s = senate_ingest.ingest(conn, start_date=since, progress=False)
                log(f"senate: {s['ingested']} new filings, {s['transactions']} "
                    f"transactions, {s['unmatched']} unmatched filers")
                total_new += s["ingested"]
            except senate.SenateBlocked as exc:
                log(f"senate: unreachable from this host ({exc}) - skipped")
            except Exception as exc:  # noqa: BLE001
                log(f"senate: failed ({type(exc).__name__}: {exc}) - skipped")

        if not args.skip_prices:
            import prices as prices_mod
            import sectors as sectors_mod
            p = prices_mod.sync_prices(conn, progress=False)
            log(f"prices: +{p['resolved']} tickers, {p['rows']:,} rows")

            # Extend the series that already exist. Without this the corpus
            # freezes on the day of the last full build, and every trade
            # disclosed after that point is scored as having no entry price and
            # quietly leaves the strategy. See prices.top_up.
            u = prices_mod.top_up(conn, progress=False)
            log(f"price top-up: {u['extended']:,}/{u['stale']:,} series extended, "
                f"{u['rows']:,} rows; {u.get('retried', 0)} re-requested singly "
                f"(+{u.get('retry_rows', 0):,} rows)")
            if u.get("resting") or u.get("went_quiet"):
                log(f"  {u.get('resting', 0)} delisted ticker(s) rested, "
                    f"{u.get('went_quiet', 0)} returned nothing this run "
                    f"(acquired or delisted names members once held)")
            s = sectors_mod.sync_sectors(conn, progress=False)
            log(f"sectors: +{s['resolved']} classified"
                + (" (throttled, will resume next run)" if s.get("stopped_early") else ""))

            # Market caps feed the grade's obscurity component. Yahoo throttles a
            # long run, so this stops itself and picks up where it left off
            # tomorrow rather than recording thousands of phantom "no cap" rows.
            import market as market_mod
            f = market_mod.sync_fundamentals(conn, progress=False)
            log(f"fundamentals: +{f['resolved']} market caps"
                + (" (throttled, will resume next run)" if f["stopped_early"] else ""))

        import backtest as backtest_mod
        b = backtest_mod.run(conn)
        log(f"backtest: {b['scored']:,} scored, {b['members_ranked']} ranked")

        import grading
        g = grading.grade_all(conn, progress=False)
        log(f"grades: {g['graded']:,} trades "
            f"(A={g['grade_A']} B={g['grade_B']} C={g['grade_C']} "
            f"D={g['grade_D']} E={g['grade_E']})")

        # Re-test whether the grade predicts returns. It has not so far, and the
        # methodology page prints the answer either way - so it has to be
        # recomputed whenever the grades change, not written down once.
        import validate as validate_mod
        v = validate_mod.build(conn)
        log(f"grade validation: A+B vs D+E {(v['top_minus_bottom'] or 0)*100:+.2f}pp "
            f"t={v['t_stat']:.2f} -> "
            + ("predictive" if v["predictive"] else "not predictive"))

        payload = strat_mod.build(conn)
        wf = payload["curves"]["strategy"]
        log(f"strategy: {len(payload['members'])} tracked members, "
            f"{wf['final_strategy']:.2f}x vs SPY {wf['final_benchmark']:.2f}x, "
            f"max drawdown {(wf['max_drawdown'] or 0)*100:.1f}%")

        # ---- 3. anything new from a tracked member? -------------------------
        members = payload["members"]
        ids = [m["bioguide_id"] for m in members]
        remit = {}
        for m in members:
            _sc, _nm, sec = congress.member_access_score(conn, m["bioguide_id"])
            remit[m["bioguide_id"]] = set(sec)
        activity = strat_mod.tracked_activity(conn, ids, remit, limit=500)

        # With no baseline, every tracked trade ever filed looks new. Record
        # them and announce nothing rather than sending hundreds of alerts about
        # trades from years ago.
        if baseline:
            log(f"no baseline yet: recording {len(activity)} tracked trades "
                f"without announcing them")
        fresh = ([] if baseline else
                 [a for a in activity if strat_mod.line_key(a) not in seen])
        seen.update(strat_mod.line_key(a) for a in activity)
        # Keys sort by doc_id, which is issued in filing order, so the newest
        # are kept. 20,000 is roughly three years of tracked activity.
        state["seen_keys"] = sorted(seen)[-20000:]
        state["last_run"] = started.isoformat(timespec="seconds")
        save_state(state)

        elapsed = (dt.datetime.now() - started).total_seconds()
        if not fresh:
            log(f"{total_new} new filing(s), none from tracked members "
                f"({elapsed:.0f}s)")
            return EXIT_OK

        log(f"*** {len(fresh)} new tracked trade(s) ***")
        lines = []
        for a in fresh[:40]:
            side = "BUY " if a["tx_type"] == "purchase" else "SELL"
            line = (f"{side} {a['ticker']:<6} {a['member_name'].replace('Hon. ', ''):<26} "
                    f"{a['amount_raw']:<22} traded {a['tx_date']} "
                    f"[{', '.join(a['overlap'])}]")
            log(f"    {line}")
            lines.append(line)

        # The digest goes out after the rebuild, so subscribers never receive a
        # link to a trade page the site cannot render yet.
        if args.email:
            # In-process, like everything else here, and after the rebuild so a
            # subscriber never gets a link to a trade page the site cannot
            # render yet.
            import digest as digest_mod
            import mailer as mailer_mod

            m = mailer_mod.build(args.mailer, ROOT / "data", {
                "user": os.environ.get("TRACKER_SMTP_USER", ""),
                "password": os.environ.get("TRACKER_SMTP_PASS", ""),
                "sender": os.environ.get("TRACKER_MAIL_FROM", ""),
                "host": os.environ.get("TRACKER_SMTP_HOST", "smtp.gmail.com"),
                "port": os.environ.get("TRACKER_SMTP_PORT", "587"),
                "api_key": os.environ.get("RESEND_API_KEY", ""),
            })
            try:
                stats = digest_mod.run(conn, m)
                log(f"digest: {stats}")
            except Exception as exc:  # noqa: BLE001
                # A failed send must not fail the scan - the data is already
                # ingested and the site is already correct.
                log(f"WARNING: digest failed: {exc}")

        if args.notify_webhook:
            notify(args.notify_webhook,
                   f"{len(fresh)} new committee-remit trade(s)\n```\n"
                   + "\n".join(lines[:20]) + "\n```")

        log(f"done in {elapsed:.0f}s")
        return EXIT_FOUND

    except Exception:  # noqa: BLE001 - a scheduled job must log, not vanish
        log("ERROR:\n" + traceback.format_exc())
        return EXIT_ERROR
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
