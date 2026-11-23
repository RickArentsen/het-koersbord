"""Who receives the digest, and the consent record behind each of them.

Every state change is here rather than in the routes, so the rules hold however
the site is driven.  Three of them matter:

**Double opt-in.**  A row starts unconfirmed and receives nothing.  It becomes
active only when the confirmation link is clicked.  This is the lawful basis
under GDPR and, prosaically, the main thing separating this from a spam folder -
anyone can type someone else's address into a form.

**The token is the only credential.**  ``secrets.token_urlsafe(32)`` gives 256
bits, appears in the confirm, preferences and unsubscribe links, and means no
address ever travels in a URL and nobody needs an account to leave.

**Leaving is one click and cannot fail.**  Unsubscribing twice, or unsubscribing
an address that never confirmed, both succeed quietly.  A leave path that can
error is a leave path that traps people.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
import sqlite3

# Deliberately permissive. This is a delivery check, not an identity check - the
# confirmation email is what actually proves the address exists, and a regex
# that tries to be clever mostly rejects valid addresses.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")

LARGE_TRADE_FLOOR = 100_000.0
FAST_FILING_DAYS = 14


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.match((email or "").strip()))


def normalise(email: str) -> str:
    return (email or "").strip().lower()


def subscribe(conn: sqlite3.Connection, email: str, want_large: bool = False,
              consent_ip: str | None = None) -> dict:
    """Record an unconfirmed subscription and return the row.

    Re-subscribing is not an error.  Someone who unsubscribed and came back, or
    who submitted the form twice because the first email went to spam, is
    reactivated on their existing row with a **fresh token** - the old link stops
    working, which is what you want if the old address was ever exposed.
    """
    email = normalise(email)
    if not valid_email(email):
        raise ValueError(f"not a usable email address: {email!r}")

    token = secrets.token_urlsafe(32)
    existing = conn.execute(
        "SELECT * FROM subscribers WHERE email = ?", (email,)).fetchone()
    if existing:
        conn.execute(
            """UPDATE subscribers
                  SET token = ?, want_large = ?, unsubscribed_at = NULL,
                      confirmed_at = NULL, created_at = ?, consent_ip = ?,
                      bounces = 0
                WHERE id = ?""",
            (token, int(bool(want_large)), _now(), consent_ip, existing["id"]))
    else:
        conn.execute(
            """INSERT INTO subscribers
                 (email, token, want_large, created_at, consent_ip)
               VALUES (?,?,?,?,?)""",
            (email, token, int(bool(want_large)), _now(), consent_ip))
    conn.commit()
    return dict(conn.execute(
        "SELECT * FROM subscribers WHERE email = ?", (email,)).fetchone())


def by_token(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM subscribers WHERE token = ?", (token or "",)).fetchone()
    return dict(row) if row else None


def confirm(conn: sqlite3.Connection, token: str) -> dict | None:
    """Activate a subscription. Confirming an already-confirmed one is fine.

    The returned dict carries one key that is not a column, ``just_confirmed``:
    True only on the call that actually made the transition.  The welcome email
    hangs off it, and a mail that fires twice because someone double-clicked the
    link is exactly the fault the digest already had once.  So the guard is the
    ``WHERE confirmed_at IS NULL`` in the UPDATE and its ``rowcount`` - not a
    read followed by a write, which two concurrent clicks can both pass.
    """
    row = by_token(conn, token)
    if not row:
        return None
    cur = conn.execute(
        "UPDATE subscribers SET confirmed_at = ? "
        "WHERE id = ? AND confirmed_at IS NULL", (_now(), row["id"]))
    # Separate statement, deliberately: clearing the unsubscribe stamp is the
    # pre-existing behaviour for someone who clicks an old confirm link, and it
    # must keep happening even when confirmed_at was already set - otherwise
    # that click would answer "you are subscribed" to someone who is not.
    conn.execute("UPDATE subscribers SET unsubscribed_at = NULL WHERE id = ?",
                 (row["id"],))
    conn.commit()
    fresh = by_token(conn, token)
    if fresh:
        fresh["just_confirmed"] = cur.rowcount == 1
    return fresh


def set_prefs(conn: sqlite3.Connection, token: str,
              want_large: bool) -> dict | None:
    row = by_token(conn, token)
    if not row:
        return None
    conn.execute("UPDATE subscribers SET want_large = ? WHERE id = ?",
                 (int(bool(want_large)), row["id"]))
    conn.commit()
    return by_token(conn, token)


def unsubscribe(conn: sqlite3.Connection, token: str) -> bool:
    """One click, idempotent. True when the token was recognised."""
    row = by_token(conn, token)
    if not row:
        return False
    if not row["unsubscribed_at"]:
        conn.execute("UPDATE subscribers SET unsubscribed_at = ? WHERE id = ?",
                     (_now(), row["id"]))
        conn.commit()
    return True


def forget(conn: sqlite3.Connection, token: str) -> bool:
    """Delete the row outright, for a GDPR erasure request.

    Distinct from :func:`unsubscribe`, which keeps the record so the address is
    not re-added by accident.  Erasure means the address stops existing here,
    send history included.
    """
    row = by_token(conn, token)
    if not row:
        return False
    conn.execute("DELETE FROM sent_items WHERE subscriber_id = ?", (row["id"],))
    conn.execute("DELETE FROM subscribers WHERE id = ?", (row["id"],))
    conn.commit()
    return True


def active(conn: sqlite3.Connection) -> list[dict]:
    """Everyone who should receive the next digest."""
    return [dict(r) for r in conn.execute(
        """SELECT * FROM subscribers
            WHERE confirmed_at IS NOT NULL AND unsubscribed_at IS NULL
              AND bounces < 3
         ORDER BY id""")]


def awaiting_confirmation(conn: sqlite3.Connection, min_days: int = 3,
                          max_days: int = 30) -> list[dict]:
    """Signups old enough to have been forgotten, young enough to still care.

    This is the list nobody sees. Someone types their address, the confirmation
    arrives while they are doing something else, and it is never clicked. The
    admin page shows them as "awaiting confirmation" forever and no part of the
    system treats that as a problem - the subscription simply never happens,
    and the only signal is a number that fails to grow.

    Both bounds are deliberate:

    ``min_days``
        Below this the reminder is just nagging - the first mail may be sitting
        unread in an inbox opened twice a day.
    ``max_days``
        Above it, mailing someone who signed up a month ago and never responded
        is what spam complaints are made of. An address that old is written off
        rather than chased; the consent record stays, so if they ever click the
        original link it still works.

    Anything already reminded, already confirmed, unsubscribed, or bouncing is
    excluded. Ordered oldest first so a capped run clears the backlog rather
    than skimming the same recent names each night.
    """
    now = dt.datetime.now()
    oldest = (now - dt.timedelta(days=max_days)).isoformat()
    newest = (now - dt.timedelta(days=min_days)).isoformat()
    return [dict(r) for r in conn.execute(
        """SELECT * FROM subscribers
            WHERE confirmed_at IS NULL
              AND unsubscribed_at IS NULL
              AND reminded_at IS NULL
              AND bounces < 3
              AND created_at <= ?
              AND created_at >= ?
         ORDER BY created_at""", (newest, oldest))]


def mark_reminded(conn: sqlite3.Connection, subscriber_id: int) -> None:
    """Stamp the nudge as sent. Called only after the send actually succeeded.

    Stamped per address as each one goes out rather than in a batch at the end:
    a crash halfway through a run must not re-mail everyone who was already
    reminded before it.
    """
    conn.execute("UPDATE subscribers SET reminded_at = ? WHERE id = ?",
                 (_now(), subscriber_id))
    conn.commit()


def record_bounce(conn: sqlite3.Connection, email: str) -> int:
    """Count a hard bounce; three disables the address.

    Continuing to send to a dead address is the fastest way to lose sender
    reputation, and it is the one deliverability problem entirely within our
    control.
    """
    conn.execute("UPDATE subscribers SET bounces = bounces + 1 WHERE email = ?",
                 (normalise(email),))
    conn.commit()
    row = conn.execute("SELECT bounces FROM subscribers WHERE email = ?",
                       (normalise(email),)).fetchone()
    return row["bounces"] if row else 0


def mark_sent(conn: sqlite3.Connection, subscriber_id: int,
              item_keys: list[str]) -> None:
    """Record what went out, keyed on ``strategy.line_key`` - never on a row id.

    ``transactions.id`` is recycled every time a filing is re-parsed, so storing
    it here would lose the send history for trades that never changed, and the
    subscriber would receive them again.  See :func:`strategy.line_key`.
    """
    now = _now()
    conn.executemany(
        "INSERT OR IGNORE INTO sent_items (subscriber_id, item_key, sent_at) "
        "VALUES (?,?,?)", [(subscriber_id, str(k), now) for k in item_keys])
    conn.execute("UPDATE subscribers SET last_sent_at = ? WHERE id = ?",
                 (now, subscriber_id))
    conn.commit()


def already_sent(conn: sqlite3.Connection, subscriber_id: int) -> set[str]:
    return {r["item_key"] for r in conn.execute(
        "SELECT item_key FROM sent_items WHERE subscriber_id = ?",
        (subscriber_id,))}
