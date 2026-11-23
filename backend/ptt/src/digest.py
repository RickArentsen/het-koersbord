"""Choosing what goes in the email, and rendering it.

The tier
--------
**In remit, filed within fourteen days.**  Of everything tested on this project
that is the sharpest cut: +11.25% twelve-month alpha at t = +5.65 on 711
decisions, and it survives the split sample in both halves (+7.15% training edge,
+17.71% test).  It also fires about three times a month with two or three items,
which is a volume a person can actually read.

Those figures were +16.52% at t = +4.07 until 2026-08-17, when a Daimler ADR with
a $0.00000028 close was found to be inflating them; see
:func:`prices.unusable_tickers`.  The mean fell and the t-statistic *rose*,
because removing the outlier cut the variance by more than it cut the mean.

**Optional: trades of $100,000 or more.**  Slightly weaker on its own (+7.02%,
t = +3.95) and offered because large trades are *interesting*, not because they
are better - absolute size was tested and is not monotonic.  It adds roughly
1.4 emails a month and barely overlaps the fast tier: only six items in twelve
months qualified on both counts.

**No A-grade tier, deliberately.**  A-graded trades returned -9.93% at
t = -3.13, negative in both halves of the split sample.  Offering it would mean
mailing people the worst trades on the site under the impression they were the
best ones.

What is never sent twice
------------------------
Selection is per subscriber and subtracts ``sent_items``, rather than taking
everything newer than a timestamp.  Filings get amended and back-filled, so a
high-water mark would either repeat items or skip them silently.

That record is keyed on :func:`strategy.line_key` - ``"<doc_id>:<seq>"`` - and
**not** on ``transactions.id``.  The surrogate id looks like the obvious choice
and is a trap: re-parsing a filing deletes and re-inserts its rows, so ids are
reminted for trades that have not changed.  One scan re-keyed 8,745 of them,
some dated 2018.  Keyed on the id, this table would forget what it had sent on
precisely the amendments it exists to survive, and re-send them.
"""

from __future__ import annotations

import datetime as dt
import html as html_mod
import os
import sqlite3

FAST_DAYS = 14
LARGE_FLOOR = 100_000.0

# Links have to work from an inbox, so nothing may be relative. Local by
# default; one environment variable moves the whole thing to a real domain.
DEFAULT_BASE_URL = "http://localhost:8080"


def base_url() -> str:
    return os.environ.get("TRACKER_SITE_URL", DEFAULT_BASE_URL).rstrip("/")


def candidates(conn: sqlite3.Connection, want_large: bool,
               since: str | None = None) -> list[dict]:
    """Everything that qualifies for the digest, newest filing first.

    ``since`` bounds how far back a brand-new subscriber's first email reaches -
    without it they would receive years of history as a welcome present.
    """
    import consensus
    import disclosure
    import strategy

    rows = strategy.in_remit_trades(conn)
    grades = {r["transaction_id"]: r["grade"] for r in
              conn.execute("SELECT transaction_id, grade FROM trade_grades")}

    out = []
    for r in rows:
        if r["tx_type"] != "purchase":
            continue
        public = disclosure.to_iso(r.get("filing_date"))
        if not public or (since and public < since):
            continue
        lag = disclosure.lag_days(r["tx_date"], r.get("filing_date"))
        is_fast = lag is not None and lag <= FAST_DAYS
        is_large = float(r.get("amount_mid") or 0) >= LARGE_FLOOR
        if not (is_fast or (want_large and is_large)):
            continue
        d = dict(r)
        d.update({"public": public, "lag": lag, "is_fast": is_fast,
                  "is_large": is_large, "grade": grades.get(r["id"]),
                  "key": strategy.line_key(r),
                  "disclosure": disclosure.describe(lag)})
        d["consensus"] = consensus.rank_for(conn, d)
        out.append(d)

    out.sort(key=lambda d: (d["public"], d["id"]), reverse=True)
    return out


def group(items: list[dict]) -> list[dict]:
    """Collapse one member's repeated buys of one name into a single entry.

    A member topping up the same holding across a week files a separate line for
    each purchase, and they all arrive in one document. Rendered literally that
    was six consecutive FWONK lines from April McClain Delaney in one email -
    technically accurate and useless to read. This is the same "one decision,
    several filed lines" collapse the rest of the site does; here it is keyed on
    (member, ticker, filing) because the whole point is that they were disclosed
    together.
    """
    from collections import OrderedDict

    groups: OrderedDict = OrderedDict()
    for i in items:
        key = (i["bioguide_id"], i["ticker"], i["public"])
        g = groups.get(key)
        if g is None:
            groups[key] = dict(i, n_lines=1, lag_min=i["lag"], lag_max=i["lag"],
                               first_traded=i["tx_date"], last_traded=i["tx_date"],
                               total_mid=float(i.get("amount_mid") or 0))
            continue
        g["n_lines"] += 1
        g["total_mid"] += float(i.get("amount_mid") or 0)
        for a, b in (("lag_min", min), ("lag_max", max)):
            if i["lag"] is not None:
                g[a] = i["lag"] if g[a] is None else b(g[a], i["lag"])
        g["first_traded"] = min(g["first_traded"] or "", i["tx_date"] or "") or None
        g["last_traded"] = max(g["last_traded"] or "", i["tx_date"] or "") or None
        # Any line being fast or large makes the group qualify, and the link
        # points at the most recent line, which is the one it opened on.
        g["is_fast"] = g["is_fast"] or i["is_fast"]
        g["is_large"] = g["is_large"] or i["is_large"]
        g.setdefault("line_keys", []).append(i["key"])
    for g in groups.values():
        g.setdefault("line_keys", [])
        g["line_keys"] = [g["key"]] + g["line_keys"]
    return list(groups.values())


def for_subscriber(conn: sqlite3.Connection, sub: dict,
                   lookback_days: int = 30) -> list[dict]:
    """What this subscriber has not already been sent."""
    import subscribers as subs_mod

    # A first email reaches back only a month; after that the floor is whenever
    # they confirmed, so nothing that predates their consent ever arrives.
    floor = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    if sub.get("confirmed_at"):
        floor = max(floor, sub["confirmed_at"][:10])

    seen = subs_mod.already_sent(conn, sub["id"])
    return [c for c in candidates(conn, bool(sub["want_large"]), since=floor)
            if c["key"] not in seen]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _money(item: dict) -> str:
    return item.get("amount_raw") or ""


def _why(item: dict) -> str:
    bits = []
    if item["is_fast"]:
        bits.append(f"filed in {item['lag']} days")
    if item["is_large"]:
        bits.append("$100k+")
    return " · ".join(bits)


def subject(items: list[dict]) -> str:
    items = group(items)
    n = len(items)
    if n == 1:
        i = items[0]
        who = (i["member_name"] or "").replace("Hon. ", "")
        return f"{who} bought {i['ticker']}"
    tickers = ", ".join(dict.fromkeys(i["ticker"] for i in items[:3]))
    more = f" +{n - 3} more" if n > 3 else ""
    return f"{n} new in-remit trades — {tickers}{more}"


def render(items: list[dict], sub: dict) -> tuple[str, str]:
    """(html, text). Both are built here so they can never drift apart."""
    url = base_url()
    unsub = f"{url}/email/unsubscribe/{sub['token']}"
    prefs = f"{url}/email/prefs/{sub['token']}"
    esc = html_mod.escape

    rows_html, lines = [], []
    for i in group(items):
        # /t/<doc_id>/<seq>, never /trade/<id>. An email is permanent and the
        # row id is not: re-parsing a filing remints it, and a link already sent
        # then 404s. Verified by simulating a re-ingest - the id link died, the
        # permalink resolved to the new row.
        link = f"{url}/t/{i['doc_id']}/{i['seq']}"
        who = (i["member_name"] or "").replace("Hon. ", "")
        sectors = ", ".join(i.get("overlap") or [])
        lag_txt = (f'{i["lag_min"]}d' if i["lag_min"] == i["lag_max"]
                   else f'{i["lag_min"]}-{i["lag_max"]}d')
        many = (f' · {i["n_lines"]} purchases' if i.get("n_lines", 1) > 1 else '')
        badges = []
        if i["is_fast"]:
            badges.append(
                f'<span style="background:#dcfce7;color:#166534;border-radius:99px;'
                f'padding:2px 8px;font-size:12px">filed in {lag_txt}</span>')
        if i["is_large"]:
            badges.append(
                '<span style="background:#fef3c7;color:#92400e;border-radius:99px;'
                'padding:2px 8px;font-size:12px">$100k+</span>')
        if i.get("consensus") and i["consensus"]["clustered"]:
            badges.append(
                f'<span style="background:#e0e7ff;color:#3730a3;border-radius:99px;'
                f'padding:2px 8px;font-size:12px">{esc(i["consensus"]["text"])}</span>')

        rows_html.append(f"""
        <tr><td style="padding:14px 0;border-bottom:1px solid #e5e7eb">
          <div style="font-size:16px">
            <strong>{esc(i['ticker'] or '')}</strong>
            <span style="color:#6b7280">— {esc(who)}</span>
          </div>
          <div style="color:#6b7280;font-size:14px;margin:4px 0">
            {esc(_money(i))}{esc(many)} · traded {esc(i['first_traded'] or '')}
            {('to ' + esc(i['last_traded'])) if i['last_traded'] != i['first_traded'] else ''} ·
            disclosed {esc(i['public'])} · {esc(sectors)}
          </div>
          <div style="margin:6px 0">{' '.join(badges)}</div>
          <a href="{link}"
             style="color:#2563eb;font-size:14px">See the filing and the grade →</a>
        </td></tr>""")

        traded = (i["first_traded"] if i["first_traded"] == i["last_traded"]
                  else f"{i['first_traded']} to {i['last_traded']}")
        lines.append(
            f"* {i['ticker']} — {who}{many}\n"
            f"  {_money(i)} · traded {traded} · disclosed {i['public']}\n"
            f"  {sectors} · filed in {lag_txt}"
            + ("  · $100k+" if i["is_large"] else "") + "\n"
            f"  {link}")

    n = len(rows_html)
    heading = (f"{n} new trade{'s' if n != 1 else ''} inside a member's own "
               f"committee jurisdiction")

    html = f"""<!doctype html>
<html><body style="margin:0;background:#f9fafb;font-family:-apple-system,
  Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111827">
<div style="max-width:600px;margin:0 auto;padding:24px">
  <div style="font-size:13px;letter-spacing:.08em;text-transform:uppercase;
       color:#6b7280">Trade of the House</div>
  <h1 style="font-size:20px;margin:8px 0 4px">{esc(heading)}</h1>
  <p style="color:#6b7280;font-size:14px;margin:0 0 8px">
    Filed within {FAST_DAYS} days of the trade{
      ', or worth $100,000 or more' if sub['want_large'] else ''}.
  </p>
  <table style="width:100%;border-collapse:collapse">{''.join(rows_html)}</table>
  <p style="color:#6b7280;font-size:13px;margin:18px 0 0">
    Disclosure speed matters because it is the part of the move you cannot have:
    in-remit trades filed within two weeks have returned <strong>+11.3%</strong>
    against the S&amp;P over twelve months, those filed after 45 days
    <strong>&minus;3.3%</strong>.
    <a href="{url}/methodology#speed" style="color:#2563eb">How that was measured</a>.
  </p>
  <hr style="border:0;border-top:1px solid #e5e7eb;margin:22px 0">
  <p style="color:#9ca3af;font-size:12px;line-height:1.6">
    You are receiving this because you confirmed a subscription at
    Trade of the House. This is a research tool, not investment advice, and
    nothing here alleges wrongdoing by any member.<br>
    <a href="{prefs}" style="color:#6b7280">Change what you receive</a> ·
    <a href="{unsub}" style="color:#6b7280">Unsubscribe</a>
  </p>
</div></body></html>"""

    text = f"""TRADE OF THE HOUSE
{heading}

Filed within {FAST_DAYS} days of the trade{
  ', or worth $100,000 or more' if sub['want_large'] else ''}.

{chr(10).join(lines)}

---
Disclosure speed matters because it is the part of the move you cannot have:
in-remit trades filed within two weeks returned +11.3% against the S&P over
twelve months; those filed after 45 days, -3.3%.
{url}/methodology#speed

You are receiving this because you confirmed a subscription at Trade of the
House. Research tool, not investment advice.
Change what you receive: {prefs}
Unsubscribe: {unsub}
"""
    return html, text


def headers_for(sub: dict) -> dict:
    """List-Unsubscribe, required by Gmail and Yahoo of bulk senders."""
    url = base_url()
    return {
        "List-Unsubscribe": f"<{url}/email/unsubscribe/{sub['token']}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
    }


def confirmation(sub: dict) -> tuple[str, str, str]:
    """(subject, html, text) for the double opt-in email."""
    url = base_url()
    link = f"{url}/email/confirm/{sub['token']}"
    subj = "Confirm your Trade of the House alerts"
    html = f"""<!doctype html>
<html><body style="margin:0;background:#f9fafb;font-family:-apple-system,
  Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111827">
<div style="max-width:600px;margin:0 auto;padding:24px">
  <h1 style="font-size:20px">One click and you are subscribed</h1>
  <p style="font-size:15px;line-height:1.6">
    You will get an email when a member of Congress buys inside their own
    committee's jurisdiction <strong>and discloses it within {FAST_DAYS} days</strong>
    &mdash; about three a month, two or three trades each.
  </p>
  <p><a href="{link}" style="background:#2563eb;color:#fff;padding:12px 20px;
     border-radius:8px;text-decoration:none;display:inline-block">
     Confirm subscription</a></p>
  <p style="color:#9ca3af;font-size:12px;line-height:1.6">
    If you did not ask for this, ignore it &mdash; nothing is sent until you
    click. The link is yours alone; anyone with it can change or cancel the
    subscription.
  </p>
</div></body></html>"""
    text = (f"Confirm your Trade of the House alerts\n\n"
            f"You will get an email when a member of Congress buys inside their\n"
            f"own committee's jurisdiction and discloses it within {FAST_DAYS} days\n"
            f"- about three a month.\n\nConfirm: {link}\n\n"
            f"If you did not ask for this, ignore it. Nothing is sent until you click.\n")
    return subj, html, text


def welcome(sub: dict) -> tuple[str, str, str]:
    """(subject, html, text) for the mail sent once, on confirmation.

    Not decoration.  Three jobs, in order of how much they matter:

    1. **It is the delivery test.**  The confirmation mail proved the address
       accepts mail.  This one proves the *digest* reaches the inbox, days or
       weeks before there is a real alert to lose.  Without it the first live
       send is also the first test.
    2. **It asks for the one action that fixes deliverability.**  This domain is
       weeks old with no sending history, so early mail lands in spam on
       reputation alone - not on SPF, DKIM or DMARC, which are all correct.
       A recipient moving a message out of spam is the strongest positive signal
       a mailbox provider takes, and it can only be asked for while the person
       is paying attention.
    3. **It sets the pace**, so a fortnight of silence reads as normal rather
       than as broken.
    """
    url = base_url()
    prefs = f"{url}/email/prefs/{sub['token']}"
    subj = "You are on the list - what happens next"
    html = f"""<!doctype html>
<html><body style="margin:0;background:#f9fafb;font-family:-apple-system,
  Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111827">
<div style="max-width:600px;margin:0 auto;padding:24px">
  <h1 style="font-size:20px">You are on the list</h1>
  <p style="font-size:15px;line-height:1.6">
    From now on you get an email when a member of Congress buys a company inside
    their own committee's jurisdiction <strong>and discloses it within
    {FAST_DAYS} days</strong>. That is roughly <strong>three emails a month</strong>,
    two or three trades in each.
  </p>
  <p style="font-size:15px;line-height:1.6">
    Filings arrive in bursts, so a quiet fortnight is normal and means nobody
    filed - not that something broke.
  </p>
  <div style="background:#fffbeb;border:1px solid #fde68a;border-radius:8px;
       padding:14px 16px;margin:18px 0">
    <p style="font-size:14px;line-height:1.6;margin:0">
      <strong>One favour, and it is the useful one.</strong>
      If this landed in spam or promotions, drag it to your inbox and mark it
      <em>not spam</em>. This domain is new, so mailbox providers have no history
      to judge it on yet, and that single action is what stops a real alert being
      filtered later. Replying &mdash; even one word &mdash; works even better.
    </p>
  </div>
  <p style="font-size:15px;line-height:1.6">
    Nothing here is investment advice. Disclosures are self-reported, published
    only as broad ranges, and are already weeks old when they appear.
  </p>
  <p style="color:#9ca3af;font-size:12px;line-height:1.6">
    <a href="{prefs}" style="color:#6b7280">Change what you receive</a> &middot;
    <a href="{url}/email/unsubscribe/{sub['token']}" style="color:#6b7280">Unsubscribe</a>
    &mdash; one click, no account.
  </p>
</div></body></html>"""
    text = (
        "You are on the list\n\n"
        "From now on you get an email when a member of Congress buys a company\n"
        f"inside their own committee's jurisdiction and discloses it within {FAST_DAYS}\n"
        "days - roughly three emails a month, two or three trades in each.\n\n"
        "Filings arrive in bursts, so a quiet fortnight is normal.\n\n"
        "One favour: if this landed in spam or promotions, move it to your inbox\n"
        "and mark it not spam. This domain is new, so mailbox providers have no\n"
        "history to judge it on, and that one action is what stops a real alert\n"
        "being filtered later. Replying works even better.\n\n"
        "Nothing here is investment advice.\n\n"
        f"Change what you receive: {prefs}\n"
        f"Unsubscribe: {url}/email/unsubscribe/{sub['token']}\n")
    return subj, html, text


def reminder(sub: dict) -> tuple[str, str, str]:
    """(subject, html, text) for the one nudge an unconfirmed signup ever gets.

    This exists because the failure it addresses is invisible.  Someone types
    their address, means it, and never clicks the link - the mail arrived
    mid-something, or landed in promotions.  Nothing is broken, nothing is
    logged, and the row sits in the admin table marked "awaiting confirmation"
    until the end of time.  On a list this size that is the difference between
    a launch working and appearing not to.

    Three rules it follows, all of them about not becoming the thing it is
    trying to escape:

    1. **Once, ever.**  ``reminded_at`` is stamped after the send and never
       cleared.  A reminder sequence is a newsletter nobody agreed to.
    2. **It offers the exit in the same breath as the link.**  Someone who has
       changed their mind should not have to confirm a subscription in order to
       be allowed to leave it, so unsubscribe works from an unconfirmed row.
    3. **It says where the first one probably went.**  Promotions and spam are
       the likeliest answer by far, and naming that is more useful than asking
       again.

    The original token is reused, so ``created_at`` and ``consent_ip`` - the
    proof this person asked - survive untouched.
    """
    url = base_url()
    confirm = f"{url}/email/confirm/{sub['token']}"
    subj = "One click left - confirm your Trade of the House alerts"
    html = f"""<!doctype html>
<html><body style="margin:0;background:#f9fafb;font-family:-apple-system,
  Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#111827">
<div style="max-width:600px;margin:0 auto;padding:24px">
  <h1 style="font-size:20px">Your subscription is not finished yet</h1>
  <p style="font-size:15px;line-height:1.6">
    You signed up to Trade of the House a few days ago, but the confirmation
    link was never clicked &mdash; so nothing has been sent to you, and nothing
    will be until it is. The first email most likely went to
    <strong>promotions or spam</strong>.
  </p>
  <p style="margin:26px 0">
    <a href="{confirm}" style="background:#111827;color:#fff;text-decoration:none;
       padding:13px 22px;border-radius:8px;font-size:15px;display:inline-block">
      Confirm my subscription</a>
  </p>
  <p style="font-size:15px;line-height:1.6">
    Once confirmed you get an email when a member of Congress buys a company
    inside their own committee's jurisdiction and discloses it within
    {FAST_DAYS} days &mdash; about three emails a month.
  </p>
  <p style="color:#6b7280;font-size:13px;line-height:1.6">
    If you did not sign up, or have changed your mind, do nothing at all: this
    is the only reminder you will get, and an unconfirmed address is never
    mailed anything else. You can also
    <a href="{url}/email/unsubscribe/{sub['token']}" style="color:#6b7280">remove
    your address entirely</a>.
  </p>
</div></body></html>"""
    text = (
        "Your subscription is not finished yet\n\n"
        "You signed up to Trade of the House a few days ago, but the\n"
        "confirmation link was never clicked - so nothing has been sent to you,\n"
        "and nothing will be until it is. The first email most likely went to\n"
        "promotions or spam.\n\n"
        f"Confirm here: {confirm}\n\n"
        "Once confirmed you get an email when a member of Congress buys a\n"
        "company inside their own committee's jurisdiction and discloses it\n"
        f"within {FAST_DAYS} days - about three emails a month.\n\n"
        "If you did not sign up, or have changed your mind, do nothing: this is\n"
        "the only reminder you will get, and an unconfirmed address is never\n"
        "mailed anything else.\n"
        f"Remove your address entirely: {url}/email/unsubscribe/{sub['token']}\n")
    return subj, html, text


def remind_unconfirmed(conn: sqlite3.Connection, mailer, min_days: int = 3,
                       max_days: int = 30, limit: int = 200,
                       dry_run: bool = False) -> dict:
    """Send the one-time nudge to everyone owed it. Returns a summary.

    Every send is checked. The stamp is written only when ``send`` returned
    true, so a provider outage means these addresses are retried tomorrow
    rather than silently marked as reminded and dropped forever - which is the
    exact bug this feature exists to fix, and would be an embarrassing one to
    reintroduce while fixing it.
    """
    import subscribers as subs_mod

    owed = subs_mod.awaiting_confirmation(conn, min_days=min_days,
                                          max_days=max_days)[:limit]
    sent, failed = 0, []
    for sub in owed:
        subject, html, text = reminder(sub)
        if dry_run:
            sent += 1
            continue
        if mailer.send(sub["email"], subject, html, text):
            subs_mod.mark_reminded(conn, sub["id"])
            sent += 1
        else:
            failed.append((sub["email"], getattr(mailer, "last_error", None)))
    return {"owed": len(owed), "sent": sent, "failed": failed,
            "dry_run": dry_run}


def run(conn: sqlite3.Connection, mailer, dry_run: bool = False,
        progress: bool = True) -> dict:
    """Send today's digest to everyone who has something new waiting."""
    import subscribers as subs_mod

    # A dry run must not put a message in front of a real person. This used to
    # skip only the recording, so `--dry-run --mailer resend` delivered a real
    # email and then deliberately forgot it had - and the documented "test it
    # first, then send" sequence therefore mailed every subscriber twice. It
    # did exactly that once, to a real inbox.
    #
    # Local backends still write, because reading the rendered message is the
    # entire point of a dry run.
    withhold = dry_run and getattr(mailer, "transmits", True)

    stats = {"subscribers": 0, "sent": 0, "items": 0, "skipped_empty": 0,
             "failed": 0, "withheld": 0}
    for sub in subs_mod.active(conn):
        stats["subscribers"] += 1
        items = for_subscriber(conn, sub)
        if not items:
            stats["skipped_empty"] += 1
            continue

        # Rendered either way: a dry run that skipped this would not catch a
        # broken template, which is one of the things it exists to catch.
        html, text = render(items, sub)
        if withhold:
            stats["withheld"] += 1
            stats["items"] += len(items)
            if progress:
                print(f"  {sub['email']}: {len(items)} item(s)"
                      f"  [dry run - NOT sent, not recorded]")
            continue

        ok = mailer.send(sub["email"], subject(items), html, text,
                         headers_for(sub))
        if not ok:
            stats["failed"] += 1
            continue
        stats["sent"] += 1
        stats["items"] += len(items)
        # A dry run must not record the items as sent, or the real run that
        # follows would think everyone had already received them.
        if not dry_run:
            subs_mod.mark_sent(conn, sub["id"], [i["key"] for i in items])
        if progress:
            print(f"  {sub['email']}: {len(items)} item(s)"
                  + ("  [dry run, not recorded]" if dry_run else ""))
    return stats
