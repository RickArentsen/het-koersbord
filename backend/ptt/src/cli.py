"""Command line entry point for the politician trading tracker.

    python src/cli.py ingest --from 2020 --to 2026
    python src/cli.py prices
    python src/cli.py backtest
    python src/cli.py alerts
    python src/cli.py serve

Sub-modules are imported lazily so that a command only pays for what it uses.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent
ROOT = SRC.parent
DATA = ROOT / "data"
sys.path.insert(0, str(SRC))

DB_PATH = DATA / "tracker.db"
EXIT_REFUSED = 75          # a Yahoo sweep stopped because Yahoo refused it: run it again later


def cmd_ingest(args: argparse.Namespace) -> None:
    import congress
    import db
    import ingest

    conn = db.connect(DB_PATH)
    if not args.skip_roster:
        info = congress.load_into_db(conn, DATA / "cache", refresh=args.refresh)
        print(f"roster: {info['legislators']} legislators, "
              f"{info['committees']} committees, {info['memberships']} memberships")

    # Accumulate whatever keys ingest_year reports rather than a fixed set, so
    # adding a counter there can never crash a multi-hour run here.
    totals: dict[str, int] = {}
    for year in range(args.year_from, args.year_to + 1):
        print(f"\n=== {year} ===", flush=True)
        stats = ingest.ingest_year(conn, year, DATA, limit=args.limit,
                                   refresh_index=args.refresh,
                                   reparse=args.reparse)
        for k, v in stats.items():
            totals[k] = totals.get(k, 0) + v
        print(f"  {year}: {stats}", flush=True)

    filled = ingest.backfill_bioguide(conn)
    print(f"\nTOTAL: {totals}")
    print(f"bioguide backfilled: {filled}")
    conn.close()


def cmd_prices(args: argparse.Namespace) -> None:
    import db
    import prices

    conn = db.connect(DB_PATH)
    stats = prices.sync_prices(conn, force=args.refresh)
    print(f"prices: {stats}")
    conn.close()


def cmd_rematch(args: argparse.Namespace) -> None:
    """Re-resolve every House filer after a matcher or roster change."""
    import db
    import ingest

    conn = db.connect(DB_PATH)
    stats = ingest.rematch_members(conn, progress=not args.quiet)
    print(f"\nrematch: {stats}")
    if stats["cleared"] or stats["repointed"]:
        print("\nMember attribution changed. Re-run:  backtest -> grade "
              "-> validate -> strategy")
    conn.close()


def cmd_fundamentals(args: argparse.Namespace) -> None:
    import db
    import market

    conn = db.connect(DB_PATH)
    stats = market.sync_fundamentals(conn, refresh=args.refresh)
    print(f"fundamentals: {stats}")
    if stats.get("no_data"):
        print(f"  {stats['no_data']} tickers have no market cap (delisted, funds, "
              f"bonds) - grading excludes them from the obscurity component "
              f"rather than scoring them zero.")
    conn.close()
    if stats["stopped_early"]:
        sys.exit(EXIT_REFUSED)


def cmd_validate(args: argparse.Namespace) -> None:
    """Test whether the grade predicts returns, and cache the answer."""
    import db
    import validate as validate_mod

    conn = db.connect(DB_PATH)
    payload = validate_mod.build(conn)
    print(validate_mod.report(payload))
    conn.close()


def cmd_selection(args: argparse.Namespace) -> None:
    """Compare every candidate rule for deciding whose trades get tracked."""
    import db
    import selection

    conn = db.connect(DB_PATH)
    payload = selection.build(conn)
    print()
    print(selection.report(payload))
    conn.close()


def cmd_subscribe(args: argparse.Namespace) -> None:
    """Add an address and send its confirmation email."""
    import db
    import digest
    import mailer as mailer_mod
    import subscribers as subs

    conn = db.connect(DB_PATH)
    row = subs.subscribe(conn, args.email, want_large=args.large)
    m = mailer_mod.build(args.mailer, DATA, _mailer_config())
    subject, html, text = digest.confirmation(row)
    if not m.send(row["email"], subject, html, text):
        print(f"  MAIL FAILED to {row['email']}: "
              f"{m.last_error or 'no reason given'}")
    print(f"invited {row['email']} (large trades: "
          f"{'yes' if row['want_large'] else 'no'})")
    if args.confirm:
        subs.confirm(conn, row["token"])
        print("  confirmed immediately (--confirm)")
    else:
        print(f"  confirm link: {digest.base_url()}/email/confirm/{row['token']}")
    conn.close()


def cmd_resend_confirm(args: argparse.Namespace) -> None:
    """Send the confirmation email again to people who never received one.

    Written because the site spent its first days telling visitors to check an
    inbox while writing their confirmation into ``data/outbox`` - so there is a
    row that consented, asked for an email, and was never sent one.  They cannot
    fix that themselves: no reminder is ever sent and the row just sits there.

    Two rules make this a repair rather than unsolicited mail:

    **The existing token is reused.**  Calling ``subscribe`` again would mint a
    new token and overwrite ``created_at`` and ``consent_ip`` - destroying the
    consent record, which is the one thing that must survive.

    **Only recent rows, by default.**  Re-confirming a signup from months ago is
    a surprise email to someone who has forgotten the site exists.  Fourteen days
    keeps this to people who plausibly still remember asking, and ``--all`` is
    there for a deliberate exception.
    """
    import datetime as dt

    import db
    import digest
    import mailer as mailer_mod

    conn = db.connect(DB_PATH)
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM subscribers "
        " WHERE confirmed_at IS NULL AND unsubscribed_at IS NULL "
        " ORDER BY created_at")]

    if args.email:
        want = (args.email or "").strip().lower()
        rows = [r for r in rows if r["email"] == want]
        if not rows:
            print(f"{want}: no unconfirmed subscription. Either it is already "
                  f"confirmed, it unsubscribed, or it was never created.")
            conn.close()
            raise SystemExit(1)
    elif not args.all:
        cutoff = (dt.datetime.now()
                  - dt.timedelta(days=args.within_days)).isoformat()
        older = [r for r in rows if (r["created_at"] or "") < cutoff]
        rows = [r for r in rows if (r["created_at"] or "") >= cutoff]
        if older:
            print(f"skipping {len(older)} row(s) older than {args.within_days} "
                  f"days; --all includes them")

    if not rows:
        print("nobody is waiting on a confirmation email.")
        conn.close()
        return

    m = mailer_mod.build(args.mailer, DATA, _mailer_config())
    print(f"{len(rows)} unconfirmed subscriber(s), sending via {m.name}"
          f"{' (DRY RUN)' if args.dry_run else ''}:")
    sent = failed = 0
    for r in rows:
        link = f"{digest.base_url()}/email/confirm/{r['token']}"
        if args.dry_run:
            print(f"  would send to {r['email']}  (signed up {r['created_at']})")
            print(f"    {link}")
            continue
        subject, html, text = digest.confirmation(r)
        # Checked, always. Discarding this is what caused the problem this
        # command exists to repair.
        if m.send(r["email"], subject, html, text):
            sent += 1
            print(f"  sent to {r['email']}")
        else:
            failed += 1
            print(f"  FAILED  {r['email']}: {m.last_error or 'no reason given'}")

    if not args.dry_run and not m.transmits:
        where = (f" They were written to {DATA / 'outbox'}."
                 if m.name == "file" else "")
        print(f"\n  NOTE: the {m.name!r} backend does not transmit, so nothing "
              f"reached anyone.{where}\n"
              f"  Add --mailer resend to actually send.")
    print(f"\nresend-confirm: {sent} sent, {failed} failed")
    conn.close()
    if failed:
        raise SystemExit(1)


def cmd_remind_unconfirmed(args: argparse.Namespace) -> None:
    """Nudge signups that never clicked their confirmation link, once each.

    Distinct from ``resend-confirm``, which is a manual repair for mail this
    site failed to send.  This is the standing job for mail that was sent
    correctly and simply never acted on - the ordinary case, and the larger
    one.  Runs nightly; safe to run twice because ``reminded_at`` is stamped
    per address as it goes.
    """
    import db
    import digest
    import mailer as mailer_mod

    conn = db.connect(DB_PATH)
    m = mailer_mod.build(args.mailer, DATA, _mailer_config())
    import subscribers as subs_mod
    owed = subs_mod.awaiting_confirmation(conn, min_days=args.min_days,
                                          max_days=args.max_days)
    if not owed:
        print(f"nobody is owed a reminder "
              f"({args.min_days}-{args.max_days} days old, not yet reminded).")
        conn.close()
        return

    print(f"{len(owed)} unconfirmed signup(s) owed a one-time reminder, "
          f"via {m.name}{' (DRY RUN)' if args.dry_run else ''}:")
    for r in owed[:args.limit]:
        print(f"  {r['email']:34} signed up {(r['created_at'] or '')[:16]}")

    res = digest.remind_unconfirmed(conn, m, min_days=args.min_days,
                                    max_days=args.max_days, limit=args.limit,
                                    dry_run=args.dry_run)
    for email, err in res["failed"]:
        print(f"  FAILED  {email}: {err or 'no reason given'}")

    if not args.dry_run and not m.transmits:
        where = (f" They were written to {DATA / 'outbox'}."
                 if m.name == "file" else "")
        print(f"\n  NOTE: the {m.name!r} backend does not transmit, so nothing "
              f"reached anyone.{where}\n"
              f"  Add --mailer resend to actually send.")
    print(f"\nremind-unconfirmed: {res['sent']} sent, "
          f"{len(res['failed'])} failed")
    conn.close()
    if res["failed"]:
        raise SystemExit(1)


def cmd_subscribers(args: argparse.Namespace) -> None:
    import db
    import subscribers as subs

    conn = db.connect(DB_PATH)
    rows = conn.execute("SELECT * FROM subscribers ORDER BY id").fetchall()
    print(f"{len(rows)} subscriber(s), {len(subs.active(conn))} active")
    for r in rows:
        state = ("unsubscribed" if r["unsubscribed_at"]
                 else "active" if r["confirmed_at"] else "unconfirmed")
        print(f"  {r['email']:34} {state:13} "
              f"large={'y' if r['want_large'] else 'n'}  "
              f"last sent {r['last_sent_at'] or '-'}")
    conn.close()


def _mailer_config() -> dict:
    """Credentials come from the environment, never from the command line."""
    import os

    return {
        "user": os.environ.get("TRACKER_SMTP_USER", ""),
        "password": os.environ.get("TRACKER_SMTP_PASS", ""),
        "sender": os.environ.get("TRACKER_MAIL_FROM", ""),
        "host": os.environ.get("TRACKER_SMTP_HOST", "smtp.gmail.com"),
        "port": os.environ.get("TRACKER_SMTP_PORT", "587"),
        "api_key": os.environ.get("RESEND_API_KEY", ""),
    }


def cmd_digest(args: argparse.Namespace) -> None:
    """Send the digest to everyone with something new waiting."""
    import db
    import digest
    import mailer as mailer_mod

    conn = db.connect(DB_PATH)
    m = mailer_mod.build(args.mailer, DATA, _mailer_config())
    stats = digest.run(conn, m, dry_run=args.dry_run)
    print(f"\ndigest: {stats}")
    if args.mailer == "file":
        print(f"  written to {DATA / 'outbox'}")
    conn.close()


def cmd_committee_history(args):
    """Reconstruct point-in-time committee membership from the repo git history."""
    import congress
    import db

    conn = db.connect(DB_PATH)
    stats = congress.sync_committee_history(
        conn, DATA / "cache", refresh=args.refresh, progress=not args.quiet)
    print(f"\ncommittee-history: {stats}")
    if stats["fetched"]:
        print("\nRemit now depends on the seat held at the time. Re-run:  "
              "backtest -> grade -> validate -> strategy -> selection")
    conn.close()


def cmd_repair_dates(args: argparse.Namespace) -> None:
    """Recompute corrections for trade dates the calendar rejects."""
    import dates
    import db

    conn = db.connect(DB_PATH)
    stats = dates.repair_all(conn, progress=not args.quiet)
    print(f"\nrepair-dates: {stats}")
    conn.close()


def cmd_sectors(args: argparse.Namespace) -> None:
    import db
    import sectors

    conn = db.connect(DB_PATH)
    stats = sectors.sync_sectors(conn, limit=args.limit)
    print(f"sectors: {stats}")
    conn.close()
    if stats["stopped_early"]:
        sys.exit(EXIT_REFUSED)


def cmd_backtest(args: argparse.Namespace) -> None:
    import backtest
    import db

    conn = db.connect(DB_PATH)
    stats = backtest.run(conn)
    print(f"backtest: {stats}")
    conn.close()


def cmd_alerts(args: argparse.Namespace) -> None:
    import alerts
    import db

    conn = db.connect(DB_PATH)
    new = alerts.detect_new_buys(conn, mark_seen=not args.peek)
    print(f"{len(new)} new buy alert(s)")
    for a in new[:40]:
        print(f"  {a['notification_date']}  {a['member_name']:28.28s} "
              f"{str(a['ticker']):6.6s}  {a['amount_raw']}")
    conn.close()


def cmd_serve(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(ROOT / "web"))
    from app import create_app

    app = create_app(DB_PATH)
    app.run(host=args.host, port=args.port, debug=args.debug)


def cmd_grade(args: argparse.Namespace) -> None:
    import db
    import grading

    conn = db.connect(DB_PATH)
    stats = grading.grade_all(conn)
    print(f"graded: {stats['graded']:,} transactions")
    conn.close()


def cmd_strategy(args: argparse.Namespace) -> None:
    import db
    import strategy

    conn = db.connect(DB_PATH)
    payload = strategy.build(conn, min_member_trades=args.min_member_trades)
    s = payload["summary"]
    wf = payload["curves"]["strategy"]
    sz = payload["curves"]["strategy_size"]
    ins = payload["curves"]["member_gated"]

    def line(label: str, c: dict) -> str:
        if c.get("final_strategy") is None:           # no trades to follow: an empty curve
            return f"{label:26} no trades"
        return (f"{label:26} {c['final_strategy']:5.2f}x vs SPY "
                f"{c['final_benchmark']:.2f}x   CAGR "
                f"{(c['cagr'] or 0) * 100:+6.2f}% vs "
                f"{(c['bench_cagr'] or 0) * 100:+.2f}%   "
                f"MDD {(c['max_drawdown'] or 0) * 100:+.1f}%")

    print(f"tracked members:     {len(payload['members'])}")
    print(f"in-remit purchases:  {s['n_in_remit']:,} "
          f"from {s['n_members']} members")
    print()
    print(line("HEADLINE (equal-weight)", wf))
    print(line("  same trades, by size", sz))
    # The retired member gate, on the same trades and the same calendar, so the
    # cost of the filter that was removed stays visible instead of vanishing
    # with it.
    print(line("  retired member gate", ins))
    conn.close()


def cmd_site(args: argparse.Namespace) -> None:
    sys.path.insert(0, str(ROOT / "web"))
    from site_app import create_site

    app = create_site(DB_PATH)
    app.run(host=args.host, port=args.port, debug=args.debug)


def cmd_earnings(args: argparse.Namespace) -> None:
    """Announcement dates and surprises for every ticker a member has traded."""
    import db
    import earnings

    conn = db.connect(DB_PATH)
    stats = earnings.sync(conn, force=args.refresh)
    print(f"earnings: {stats}")
    n = conn.execute("SELECT COUNT(*) FROM earnings").fetchone()[0]
    t = conn.execute("SELECT COUNT(DISTINCT ticker) FROM earnings").fetchone()[0]
    print(f"  stored: {n:,} announcements over {t:,} tickers")
    if stats["stopped_early"]:
        print("  stopped early on a run of empty responses (throttled); "
              "re-run to continue")
    conn.close()


def cmd_ocr(args: argparse.Namespace) -> None:
    """Read image-only filings that the PDF text parser cannot.

    Writes to ``ocr_transactions`` and nothing else. OCR'd rows carry weaker
    provenance than parsed ones, so they are staged for review rather than
    dropped straight into the numbers the site publishes.
    """
    import datetime as dt
    import urllib.request

    import db
    import ocr

    if not ocr.available():
        print("Windows OCR is unavailable here. Install with: pip install winsdk")
        return

    conn = db.connect(DB_PATH)
    sql = "SELECT doc_id, year, member_name, filing_date, pdf_url FROM filings " \
          "WHERE parse_status = 'no_text'"
    params: list = []
    if args.doc:
        sql += " AND doc_id = ?"
        params.append(args.doc)
    if args.member:
        sql += " AND filer_last LIKE ?"
        params.append(f"%{args.member}%")
    sql += " ORDER BY filing_date DESC"
    rows = conn.execute(sql, params).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print("no matching unreadable filings")
        return
    print(f"{len(rows)} unreadable filing(s) to read\n")

    from backtest import _to_iso
    total = 0
    for r in rows:
        cache = ROOT / "data" / "raw" / "ptr" / str(r["year"]) / f"{r['doc_id']}.pdf"
        if not cache.exists():
            cache.parent.mkdir(parents=True, exist_ok=True)
            try:
                urllib.request.urlretrieve(r["pdf_url"], cache)
            except Exception as exc:  # noqa: BLE001
                print(f"  {r['doc_id']}: download failed ({exc})")
                continue
        filed = _to_iso(r["filing_date"])
        # A PTR covers recent activity; a recovered date years either side of
        # the filing is a misread year, not an old trade.
        before = after = None
        if filed:
            fd = dt.date.fromisoformat(filed)
            before = (fd - dt.timedelta(days=760)).isoformat()
            after = (fd + dt.timedelta(days=2)).isoformat()
        try:
            found, pages = ocr.read_filing(str(cache), not_before=before,
                                           not_after=after)
        except Exception as exc:  # noqa: BLE001
            print(f"  {r['doc_id']}: OCR failed ({type(exc).__name__}: {exc})")
            continue
        ok = sum(1 for p in pages if p["status"] == "ok")
        sure = sum(1 for t in found if t["confident"])
        print(f"  {r['doc_id']}  {r['member_name'][:26]:<28} {filed}  "
              f"{ok}/{len(pages)} pages, {len(found):>4} rows "
              f"({sure} confident)")
        if args.store:
            ocr.store(conn, r["doc_id"], found)
        total += len(found)
    print(f"\n{total:,} transaction(s) read"
          + (" and staged in ocr_transactions" if args.store else
             " (nothing written; pass --store to stage them)"))
    conn.close()


def cmd_verify(args: argparse.Namespace) -> None:
    """Data-quality checks over the stored data.

    Every one of these corresponds to a real bug found during development, so
    they are cheap regression cover for the whole pipeline rather than a
    decorative health check.
    """
    import db
    import prices
    from backtest import _to_iso

    conn = db.connect(DB_PATH)
    problems = 0

    def check(label: str, sql: str, tolerate: int = 0, detail: str | None = None):
        nonlocal problems
        n = conn.execute(sql).fetchone()[0]
        ok = n <= tolerate
        if not ok:
            problems += 1
        print(f"  {'ok  ' if ok else 'WARN'} {label}: {n:,}")
        if not ok and detail:
            for r in conn.execute(detail):
                print(f"         {tuple(r)}")

    def info(label: str, sql: str) -> None:
        print(f"  info {label}: {conn.execute(sql).fetchone()[0]:,}")

    print("Data quality:")
    # House-only: this detects a PDF-parsing failure where a sub-table label
    # bleeds into the asset name.  The Senate's HTML reports have no such
    # labels, but their asset-name cell legitimately reads "Company: ...
    # Description: ..." for private holdings, so running this over both
    # chambers reports 161 healthy rows as damage.
    check("sub-table labels leaked into asset names (House PDFs)",
          "SELECT COUNT(*) FROM transactions WHERE doc_id NOT LIKE 'sen:%' "
          "AND (asset_name LIKE '%STATUS:%' OR asset_name LIKE '%DESCRIPTION:%' "
          "OR asset_name LIKE '%SUBHOLDING%')",
          detail="SELECT doc_id, ticker, substr(asset_name,1,60) FROM transactions "
                 "WHERE doc_id NOT LIKE 'sen:%' AND asset_name LIKE '%STATUS:%' LIMIT 5")

    # SQLite's char(0) evaluates to an empty string, so a LIKE pattern built
    # from it matches every row.  Scan in Python instead.
    nul = sum(1 for (a, b) in conn.execute(
        "SELECT asset_name, description FROM transactions")
        if ("\x00" in (a or "")) or ("\x00" in (b or "")))
    print(f"  {'ok  ' if nul == 0 else 'WARN'} NUL bytes surviving into stored text: {nul:,}")
    if nul:
        problems += 1

    info("filings whose table was never found (see `status`)",
         "SELECT COUNT(*) FROM filings WHERE parse_status='no_table'")
    check("parse errors", "SELECT COUNT(*) FROM filings WHERE parse_status='error'")
    check("purchases with an unparsed amount",
          "SELECT COUNT(*) FROM transactions WHERE tx_type='purchase' "
          "AND amount_raw <> '' AND amount_low IS NULL", tolerate=25)
    check("trades dated in the future",
          "SELECT COUNT(*) FROM transactions WHERE tx_date > date('now')", tolerate=10)
    info("trades the filer dated after their own notification date",
         "SELECT COUNT(*) FROM transactions WHERE tx_date IS NOT NULL "
         "AND notification_date IS NOT NULL AND tx_date > notification_date")
    info("scored entries preceding the filer's notification date",
         """SELECT COUNT(*) FROM trade_performance p JOIN transactions t
              ON t.id = p.transaction_id
             WHERE p.entry_date < t.notification_date""")

    # The invariant that actually matters. Entry is taken from the *filing*
    # date, because that is when the trade became public; a notification date
    # is whatever the filer typed and is sometimes inconsistent with it.
    # Anything entering on or before the filing date would be lookahead bias.
    lookahead = 0
    for filed, entry in conn.execute(
            """SELECT t.filing_date, p.entry_date FROM trade_performance p
                 JOIN transactions t ON t.id = p.transaction_id"""):
        iso = _to_iso(filed)
        if iso and entry and entry <= iso:
            lookahead += 1
    if lookahead:
        problems += 1
    print(f"  {'ok  ' if lookahead == 0 else 'WARN'} "
          f"LOOKAHEAD: entries on or before the public filing date: {lookahead}")
    check("scored trades with a non-positive entry price",
          "SELECT COUNT(*) FROM trade_performance WHERE entry_price <= 0")

    # The check above passed for months while the headline was wrong: DAIUF's
    # entry price was 0.00000028, which is positive. An impossible *price* and an
    # unusable *denominator* are separate faults and both are now tested.
    check("impossible prices stored (negative or zero close)",
          "SELECT COUNT(*) FROM prices WHERE close IS NULL OR close <= 0",
          detail="SELECT ticker, COUNT(*), MIN(close) FROM prices "
                 "WHERE close IS NULL OR close <= 0 GROUP BY ticker LIMIT 10")
    check("scored trades entered below the usable price floor",
          f"SELECT COUNT(*) FROM trade_performance "
          f"WHERE entry_price < {prices.MIN_PLAUSIBLE_PRICE}",
          detail="""SELECT t.ticker, p.entry_date, p.entry_price, p.ret_12m
                      FROM trade_performance p JOIN transactions t
                        ON t.id = p.transaction_id
                     WHERE p.entry_price < 0.01 LIMIT 10""")
    info("tickers quarantined for unusable prices",
         "SELECT COUNT(DISTINCT ticker) FROM prices "
         f"WHERE close IS NULL OR close <= 0 OR close < {prices.MIN_PLAUSIBLE_PRICE}")
    # A forward return below -100% cannot happen to a long position, so it is
    # proof of a bad price rather than a bad trade.
    check("impossible forward returns (worse than -100%)",
          "SELECT COUNT(*) FROM trade_performance WHERE " + " OR ".join(
              f"ret_{h} < -1.0" for h in
              ("1m", "3m", "6m", "12m", "18m", "24m", "36m", "48m", "60m")))
    check("politician_stats rows with no scored trades",
          "SELECT COUNT(*) FROM politician_stats WHERE n_scored IS NULL OR n_scored = 0")

    print(f"\n{problems} check(s) flagged." if problems else "\nall checks passed.")
    conn.close()


def cmd_export(args: argparse.Namespace) -> None:
    """Dump the joined trade-level table to CSV for external analysis."""
    import csv

    import db

    conn = db.connect(DB_PATH)
    rows = conn.execute(
        """SELECT t.id, t.doc_id, t.bioguide_id, t.member_name, t.state_district,
                  l.party, l.chamber, t.owner, t.ticker, t.asset_name, t.asset_type,
                  t.tx_type, t.tx_date, t.notification_date, t.filing_date,
                  t.amount_raw, t.amount_low, t.amount_high, t.amount_mid,
                  t.cap_gains_over_200,
                  p.entry_date, p.entry_price,
                  p.ret_1m, p.ret_3m, p.ret_6m, p.ret_12m,
                  p.bench_1m, p.bench_3m, p.bench_6m, p.bench_12m,
                  p.member_ret_3m, p.member_bench_3m,
                  s.sensitivity, s.committees, m.sectors AS ticker_sectors
             FROM transactions t
        LEFT JOIN trade_performance p ON p.transaction_id = t.id
        LEFT JOIN legislators l       ON l.bioguide_id = t.bioguide_id
        LEFT JOIN politician_stats s  ON s.bioguide_id = t.bioguide_id
        LEFT JOIN ticker_meta m       ON m.ticker = t.ticker
         ORDER BY t.tx_date DESC, t.id""").fetchall()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    horizons = ("1m", "3m", "6m", "12m")

    if not rows:
        print("nothing to export — run ingest first")
        conn.close()
        return

    fields = list(rows[0].keys()) + [f"alpha_{h}" for h in horizons]
    with out.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        for r in rows:
            d = dict(r)
            # Alpha is what most analysis actually wants, so derive it here and
            # save every consumer from redoing the benchmark join.
            for h in horizons:
                ret, bench = d.get(f"ret_{h}"), d.get(f"bench_{h}")
                d[f"alpha_{h}"] = (ret - bench) if (ret is not None
                                                   and bench is not None) else None
            writer.writerow(d)

    print(f"wrote {len(rows)} rows to {out}")
    conn.close()


def cmd_senate_ingest(args: argparse.Namespace) -> None:
    import db
    import senate
    import senate_ingest

    conn = db.connect(DB_PATH)
    try:
        stats = senate_ingest.ingest(conn, start_date=args.since,
                                     end_date=args.until, limit=args.limit)
    except senate.SenateBlocked as exc:
        print(f"\nefdsearch.senate.gov is not reachable from here: {exc}")
        print("Run this on a network that can reach senate.gov "
              "(check with: python src/cli.py senate-check).")
        conn.close()
        return
    print(f"\nsenate ingest: {stats}")
    print("\nNow re-run:  prices -> sectors -> backtest -> grade -> strategy")
    conn.close()


def cmd_senate_check(args: argparse.Namespace) -> None:
    import senate

    print("Probing efdsearch.senate.gov (needs curl_cffi — the edge fingerprints TLS):")
    result = senate.check(verbose=True)
    if result["report"]:
        print("\nSenate portal is reachable and parseable from this network.")
    else:
        print("\nSenate data is NOT available from this network. The House "
              "pipeline is unaffected.")


def cmd_status(args: argparse.Namespace) -> None:
    import db

    conn = db.connect(DB_PATH)
    def q(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    checks = [
        ("filings", "SELECT COUNT(*) FROM filings"),
        ("  parsed ok", "SELECT COUNT(*) FROM filings WHERE parse_status = 'ok'"),
        ("  scanned/no text", "SELECT COUNT(*) FROM filings WHERE parse_status = 'no_text'"),
        ("  no table found", "SELECT COUNT(*) FROM filings WHERE parse_status = 'no_table'"),
        ("  parse errors", "SELECT COUNT(*) FROM filings WHERE parse_status = 'error'"),
        ("transactions", "SELECT COUNT(*) FROM transactions"),
        ("  purchases", "SELECT COUNT(*) FROM transactions WHERE tx_type = 'purchase'"),
        ("  with ticker", "SELECT COUNT(*) FROM transactions WHERE ticker IS NOT NULL"),
        ("  with member", "SELECT COUNT(*) FROM transactions WHERE bioguide_id IS NOT NULL"),
        ("members traded", "SELECT COUNT(DISTINCT bioguide_id) FROM transactions"),
        ("price rows", "SELECT COUNT(*) FROM prices"),
        ("scored trades", "SELECT COUNT(*) FROM trade_performance"),
        ("ranked members", "SELECT COUNT(*) FROM politician_stats"),
    ]
    for label, sql in checks:
        print(f"{label + ':':18s}{q(sql)}")
    row = conn.execute("SELECT MIN(tx_date) a, MAX(tx_date) b FROM transactions").fetchone()
    print(f"date range:     {row['a']} .. {row['b']}")
    conn.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Politician trading tracker")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("ingest", help="download and parse House PTR filings")
    p.add_argument("--from", dest="year_from", type=int, default=2020)
    p.add_argument("--to", dest="year_to", type=int, default=2026)
    p.add_argument("--limit", type=int, default=None,
                   help="max filings per year (for testing)")
    p.add_argument("--refresh", action="store_true",
                   help="re-download indexes and roster")
    p.add_argument("--skip-roster", action="store_true")
    p.add_argument("--reparse", action="store_true",
                   help="re-parse every cached PDF (after a parser fix); "
                        "downloads nothing")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("prices", help="fetch price history for traded tickers")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=cmd_prices)

    p = sub.add_parser("sectors",
                       help="classify traded tickers into committee-jurisdiction sectors")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_sectors)

    p = sub.add_parser("rematch",
                       help="re-resolve House filers to members (overwrites wrong ids)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_rematch)

    p = sub.add_parser("fundamentals",
                       help="backfill company facts (market caps) for every traded ticker")
    p.add_argument("--refresh", action="store_true", help="re-fetch already-cached tickers")
    p.set_defaults(func=cmd_fundamentals)

    p = sub.add_parser("validate",
                       help="test whether the A-E grade predicts returns")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("earnings",
                       help="fetch earnings announcement dates and surprises")
    p.add_argument("--refresh", action="store_true",
                   help="re-fetch tickers already recorded")
    p.set_defaults(func=cmd_earnings)

    p = sub.add_parser("ocr", help="read scanned (image-only) filings")
    p.add_argument("--doc", help="a single doc_id")
    p.add_argument("--member", help="surname, e.g. Khanna")
    p.add_argument("--limit", type=int, default=0, help="stop after N filings")
    p.add_argument("--store", action="store_true",
                   help="write results to the ocr_transactions staging table")
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("subscribe", help="add an email address to the digest")
    p.add_argument("email")
    p.add_argument("--large", action="store_true",
                   help="also send trades of $100,000 or more")
    p.add_argument("--confirm", action="store_true",
                   help="skip double opt-in (local testing only)")
    p.add_argument("--mailer", default="file",
                   choices=["file", "console", "smtp", "resend"])
    p.set_defaults(func=cmd_subscribe)

    p = sub.add_parser("resend-confirm",
                       help="send the confirmation email again to anyone who "
                            "signed up but never received one")
    p.add_argument("--email", help="just this address")
    p.add_argument("--all", action="store_true",
                   help="include signups older than --within-days")
    p.add_argument("--within-days", type=int, default=14,
                   help="how recent a signup must be (default 14)")
    p.add_argument("--dry-run", action="store_true",
                   help="list who would be mailed, and their confirm links")
    p.add_argument("--mailer", default="file",
                   choices=["file", "console", "smtp", "resend"])
    p.set_defaults(func=cmd_resend_confirm)

    p = sub.add_parser("remind-unconfirmed",
                       help="one-time nudge to signups that never clicked "
                            "their confirmation link")
    p.add_argument("--min-days", type=int, default=3,
                   help="how old a signup must be before nudging (default 3)")
    p.add_argument("--max-days", type=int, default=30,
                   help="beyond this it is written off rather than chased "
                        "(default 30)")
    p.add_argument("--limit", type=int, default=200,
                   help="cap on one run (default 200)")
    p.add_argument("--dry-run", action="store_true",
                   help="list who would be nudged, and send nothing")
    p.add_argument("--mailer", default="file",
                   choices=["file", "console", "smtp", "resend"])
    p.set_defaults(func=cmd_remind_unconfirmed)

    p = sub.add_parser("subscribers", help="list everyone on the digest")
    p.set_defaults(func=cmd_subscribers)

    p = sub.add_parser("digest", help="send the email digest")
    p.add_argument("--dry-run", action="store_true",
                   help="render and report, but send nothing to a real address "
                        "and record nothing as sent")
    p.add_argument("--mailer", default="file",
                   choices=["file", "console", "smtp", "resend"])
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("committee-history",
                       help="fetch quarterly committee membership snapshots")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_committee_history)

    p = sub.add_parser("repair-dates",
                       help="flag and correct impossible trade dates (filer typos)")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_repair_dates)

    p = sub.add_parser("selection",
                       help="compare selection rules (committees / grade / "
                            "track record) head to head")
    p.set_defaults(func=cmd_selection)

    p = sub.add_parser("backtest", help="score trades and rank politicians")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("alerts", help="list buys not yet seen")
    p.add_argument("--peek", action="store_true", help="do not mark as seen")
    p.set_defaults(func=cmd_alerts)

    p = sub.add_parser("serve", help="run the web dashboard")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5000)
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("grade",
                       help="score how deliberate each trade looks (A-E)")
    p.set_defaults(func=cmd_grade)

    p = sub.add_parser("strategy",
                       help="compute and cache the tracked-strategy curve")
    p.add_argument("--min-member-trades", type=int, default=1,
                   help="in-remit trades a member needs to appear on the roster")
    p.set_defaults(func=cmd_strategy)

    p = sub.add_parser("site", help="run the public tracker website")
    p.add_argument("--host", default="127.0.0.1")
    # 8080 rather than 8000: Windows reserves 8000 on some machines (see
    # `netsh interface ipv4 show excludedportrange protocol=tcp`).
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--debug", action="store_true")
    p.set_defaults(func=cmd_site)

    p = sub.add_parser("verify", help="run data-quality checks over the database")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("export", help="dump the trade-level table to CSV")
    p.add_argument("-o", "--out", default=str(DATA / "trades.csv"))
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("senate-ingest",
                       help="fetch and store Senate PTRs from efdsearch.senate.gov")
    p.add_argument("--since", default="01/01/2020", help="MM/DD/YYYY")
    p.add_argument("--until", default=None, help="MM/DD/YYYY (default: today)")
    p.add_argument("--limit", type=int, default=None)
    p.set_defaults(func=cmd_senate_ingest)

    p = sub.add_parser("senate-check",
                       help="probe whether Senate EFD is reachable from this network")
    p.set_defaults(func=cmd_senate_check)

    p = sub.add_parser("status", help="summarise what is in the database")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
