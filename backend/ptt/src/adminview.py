"""The admin dashboard: subscribers, sends, and the tail of each job log.

Authentication
--------------
This page lists real email addresses, so it is the one part of the site that
must never be world-readable. Three rules, in order of how much they matter:

1. **Off unless configured.** Without ``TRACKER_ADMIN_PASSWORD`` in the
   environment the routes return 404 - not 403, which would confirm to a
   scanner that an admin area exists. The access log already shows steady
   probing for ``/wp-login.php`` and ``/.env``; there is no reason to answer
   differently for this one.
2. **HTTP Basic over TLS**, checked with :func:`hmac.compare_digest`. A naive
   ``==`` leaks the password one character at a time through timing.
3. **Never in a URL.** A token in the path would be written to Caddy's access
   log in plain text and sent onward in ``Referer`` headers - which is exactly
   how the unsubscribe tokens must *not* be treated either.

Set it on the server, machine scope, alongside the Resend key:

    [Environment]::SetEnvironmentVariable("TRACKER_ADMIN_PASSWORD", "...", "Machine")

The username defaults to ``admin`` and can be changed with
``TRACKER_ADMIN_USER``.
"""
from __future__ import annotations

import functools
import hmac
import os
import sqlite3
from pathlib import Path

# Only these are readable, by exact name. Anything driven by a path from the
# query string would be a file-disclosure bug: ?log=../../../.aws/credentials
# is the whole of that attack, and this site has AWS credentials on the box.
LOGS = {
    "pull": ("pull.log", "Server pull: database swap, then the digest"),
    "nightly": ("nightly.log", "Laptop nightly: scan, rebuild, publish"),
    "scan": ("daily_scan.log", "Filing scan"),
    "site-debug": ("site-debug.log", "Web application"),
}
TAIL_BYTES = 60_000


def configured() -> bool:
    return bool(os.environ.get("TRACKER_ADMIN_PASSWORD"))


def check(auth) -> bool:
    """Constant-time credential check. False whenever anything is missing."""
    want_user = os.environ.get("TRACKER_ADMIN_USER", "admin")
    want_pass = os.environ.get("TRACKER_ADMIN_PASSWORD", "")
    if not want_pass or auth is None:
        return False
    # Both compared, and always both, so the reply takes the same time whether
    # the username was wrong, the password was wrong, or both.
    ok_user = hmac.compare_digest((auth.username or ""), want_user)
    ok_pass = hmac.compare_digest((auth.password or ""), want_pass)
    return ok_user and ok_pass


def protect(view):
    """404 when unconfigured, 401 challenge when unauthenticated."""
    from flask import Response, request

    @functools.wraps(view)
    def wrapped(*a, **kw):
        from flask import abort

        if not configured():
            abort(404)
        if not check(request.authorization):
            return Response(
                "Authentication required.", 401,
                {"WWW-Authenticate": 'Basic realm="Trade of the House admin"'})
        return view(*a, **kw)

    return wrapped


def subscribers(conn: sqlite3.Connection) -> list[dict]:
    """Everyone on the list, with what they have actually been sent."""
    rows = conn.execute("""
        SELECT s.id, s.email, s.want_large, s.created_at, s.confirmed_at,
               s.unsubscribed_at, s.consent_ip, s.last_sent_at, s.bounces,
               s.reminded_at,
               (SELECT COUNT(*) FROM sent_items i WHERE i.subscriber_id = s.id)
                   AS n_sent
        FROM subscribers s
        ORDER BY s.created_at DESC
    """).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d["unsubscribed_at"]:
            d["state"], d["tone"] = "unsubscribed", "off"
        elif d["confirmed_at"]:
            d["state"], d["tone"] = "confirmed", "on"
        elif d.get("reminded_at"):
            # Still pending, but the one nudge has been spent - so this row is
            # now as far as it will ever get on its own. Distinguished from the
            # one above because the two ask different questions: a growing
            # "awaiting" count means the reminder job has not run yet, a
            # growing "reminded" count means the confirmation step itself is
            # losing people and the mail is what needs looking at.
            d["state"], d["tone"] = "reminded, still waiting", "pending"
        else:
            # Worth showing separately rather than counting as a subscriber:
            # an unconfirmed row has consented to nothing and receives nothing.
            d["state"], d["tone"] = "awaiting confirmation", "pending"
        out.append(d)
    return out


def summary(conn: sqlite3.Connection, subs: list[dict]) -> dict:
    return {
        "total": len(subs),
        "confirmed": sum(1 for s in subs if s["state"] == "confirmed"),
        # Both pending states, together: the headline question is how many
        # people asked for this and are not getting it.
        "pending": sum(1 for s in subs if s["tone"] == "pending"),
        "reminded": sum(1 for s in subs if s["state"] == "reminded, still waiting"),
        "gone": sum(1 for s in subs if s["state"] == "unsubscribed"),
        "items_sent": conn.execute("SELECT COUNT(*) FROM sent_items").fetchone()[0],
        "bounces": sum(s["bounces"] or 0 for s in subs),
    }


def recent_sends(conn: sqlite3.Connection, limit: int = 60) -> list[dict]:
    rows = conn.execute("""
        SELECT i.sent_at, i.item_key, s.email
        FROM sent_items i JOIN subscribers s ON s.id = i.subscriber_id
        ORDER BY i.sent_at DESC, i.rowid DESC
        LIMIT ?
    """, (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # The key is "<doc_id>:<seq>", which is also the permalink.
        doc, _, seq = (d["item_key"] or "").partition(":")
        d["url"] = f"/t/{doc}/{seq}" if doc and seq else None
        out.append(d)
    return out


def mail_status(data_dir: Path) -> dict:
    """Can this process actually send mail, and does it know its own address?

    Exists because both ways of getting this wrong were completely silent.
    ``TRACKER_MAILER`` unset makes the web app build a FileMailer: signup looks
    like it worked, the confirmation goes into ``data/outbox`` and no email is
    ever sent.  ``TRACKER_SITE_URL`` unset is just as fatal one step later -
    every confirm and unsubscribe link points at ``http://localhost:8080``,
    which works perfectly on the server and for nobody else.

    Neither shows up in a test, because both are environment, not code.  So they
    are reported on a page that gets looked at.

    Environment variables are reported as set/unset only.  The Resend key must
    never be rendered into a page, and its length is enough to tell a real key
    from an empty string left behind by a bad quote.
    """
    import mailer as mailer_mod

    kind = os.environ.get("TRACKER_MAILER", "")
    m = mailer_mod.build(kind or "file", Path(data_dir), {
        "user": os.environ.get("TRACKER_SMTP_USER", ""),
        "password": os.environ.get("TRACKER_SMTP_PASS", ""),
        "sender": os.environ.get("TRACKER_MAIL_FROM", ""),
        "host": os.environ.get("TRACKER_SMTP_HOST", "smtp.gmail.com"),
        "port": os.environ.get("TRACKER_SMTP_PORT", "587"),
        "api_key": os.environ.get("RESEND_API_KEY", ""),
    })
    site_url = os.environ.get("TRACKER_SITE_URL", "")
    key = os.environ.get("RESEND_API_KEY", "")
    sender = os.environ.get("TRACKER_MAIL_FROM", "")

    problems = []
    if not m.transmits:
        problems.append(
            f"TRACKER_MAILER is {kind or 'unset'}, so mail is written to "
            f"data/outbox and never sent. Signup will look like it worked.")
    if m.name == "resend" and not key:
        problems.append("RESEND_API_KEY is not set in this process.")
    if m.name == "resend" and not sender:
        problems.append("TRACKER_MAIL_FROM is not set in this process.")
    if not site_url:
        problems.append(
            "TRACKER_SITE_URL is not set, so every confirm and unsubscribe "
            "link in every email points at http://localhost:8080.")

    outbox = Path(data_dir) / "outbox"
    recent = []
    if outbox.exists():
        files = sorted(outbox.glob("*.html"), key=lambda p: p.stat().st_mtime,
                       reverse=True)[:5]
        recent = [p.name for p in files]

    return {
        "backend": m.name,
        "transmits": m.transmits,
        "site_url": site_url or "(unset - links will point at localhost)",
        "key_set": bool(key),
        "key_len": len(key),
        "sender": sender or "(unset)",
        "problems": problems,
        "outbox_files": recent,
        "outbox_dir": str(outbox),
    }


def log_tail(data_dir: Path, name: str) -> dict:
    """The end of one named log. `name` must be a key of LOGS."""
    if name not in LOGS:
        return {"name": name, "error": "unknown log"}
    filename, label = LOGS[name]
    path = Path(data_dir) / filename
    if not path.exists():
        return {"name": name, "label": label, "file": filename,
                "error": "not written yet"}
    size = path.stat().st_size
    with open(path, "rb") as fh:
        if size > TAIL_BYTES:
            fh.seek(-TAIL_BYTES, os.SEEK_END)
            fh.readline()          # discard the partial first line
        text = fh.read().decode("utf-8", errors="replace")

    # The S3 CLI writes a progress line per megabyte, which is 140 lines of
    # "Completed 12.0 MiB/136.4 MiB" for every single run. Unreadable, and it
    # pushes the lines that matter out of the window.
    lines = [ln.rstrip() for ln in text.splitlines()
             if not ln.startswith("Completed ")]
    return {"name": name, "label": label, "file": filename,
            "size_kb": round(size / 1024, 1),
            "modified": path.stat().st_mtime,
            "lines": lines[-400:]}
