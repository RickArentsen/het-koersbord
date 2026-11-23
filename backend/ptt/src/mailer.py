"""Where a rendered email goes. One interface, four backends.

The default is :class:`FileMailer`, which writes to ``data/outbox/`` and sends
nothing.  That is deliberate: the whole subscription flow - opt-in, digest
selection, rendering, unsubscribe - can be exercised end to end and eyeballed in
a browser before any domain exists, any DNS record is set, or any provider
account is created.  Nothing about the code changes when a real sender is wired
in; only ``--mailer`` does.

Every message carries ``List-Unsubscribe`` and ``List-Unsubscribe-Post``.  Gmail
and Yahoo have required these of bulk senders since February 2024, and without
them delivery degrades no matter how good the content is.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path


class Mailer:
    """send() returns True when the message was accepted for delivery."""

    #: Does send() put a message in front of a real person?
    #:
    #: This is what makes ``--dry-run`` safe. A dry run must not transmit, but
    #: it *should* still write to the local outbox, because inspecting the
    #: rendered message is the whole reason for running one. Backends declare
    #: which they are rather than the digest guessing from the mailer's name.
    transmits = True

    #: Why the last send() returned False. None when it returned True.
    #:
    #: The failure used to go to ``print()``, which under waitress started by a
    #: scheduled task goes nowhere at all - so a confirmation email that Resend
    #: rejected left no trace anywhere, while the site told the visitor to check
    #: an inbox that would never receive it. The caller needs the reason, not
    #: just the boolean, to put it in a log a human will actually read.
    last_error: str | None = None

    #: Short name for the backend, for logs and the admin page.
    name = "mailer"

    def send(self, to: str, subject: str, html: str, text: str,
             headers: dict | None = None) -> bool:
        raise NotImplementedError


class ConsoleMailer(Mailer):
    """Prints a summary. Used by the tests."""

    transmits = False
    name = "console"

    def __init__(self):
        self.sent: list[dict] = []

    def send(self, to, subject, html, text, headers=None) -> bool:
        self.sent.append({"to": to, "subject": subject, "html": html,
                          "text": text, "headers": headers or {}})
        print(f"  [console] -> {to}: {subject}")
        return True


class FileMailer(Mailer):
    """Writes each message to disk instead of sending it.

    Two files per message: the HTML to open in a browser, and the plain-text
    part, which is what most spam filters actually score and therefore the one
    worth reading before going live.
    """

    transmits = False
    name = "file"

    def __init__(self, outbox: Path):
        self.outbox = Path(outbox)
        self.outbox.mkdir(parents=True, exist_ok=True)
        self.written: list[Path] = []

    def send(self, to, subject, html, text, headers=None) -> bool:
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^a-z0-9]+", "-", to.lower()).strip("-")
        base = self.outbox / f"{stamp}-{safe}"
        # Two messages to one address inside the same second used to land on the
        # same filename, and the second silently overwrote the first - which is
        # exactly what confirmation-then-welcome does. A debugging backend that
        # quietly loses a message is worse than no backend, because reading the
        # outbox is the whole point of it.
        n = 2
        while base.with_suffix(".html").exists():
            base = self.outbox / f"{stamp}-{safe}-{n}"
            n += 1
        hdr = "\n".join(f"{k}: {v}" for k, v in (headers or {}).items())
        base.with_suffix(".html").write_text(html, encoding="utf-8")
        base.with_suffix(".txt").write_text(
            f"To: {to}\nSubject: {subject}\n{hdr}\n\n{text}", encoding="utf-8")
        self.written.append(base.with_suffix(".html"))
        print(f"  [outbox] {base.with_suffix('.html').name}")
        return True


class SmtpMailer(Mailer):
    """Plain SMTP - works with Gmail using an App Password.

    Gmail dropped basic authentication, so the password here must be a 16
    character App Password generated with 2FA enabled on the account, never the
    account password itself.  Free Gmail allows roughly 500 recipients a day,
    which caps the list at about that many subscribers since a digest goes to
    everyone at once.
    """

    name = "smtp"

    def __init__(self, host: str, port: int, user: str, password: str,
                 sender: str | None = None, use_ssl: bool = False):
        self.host, self.port = host, port
        self.user, self.password = user, password
        self.sender = sender or user
        self.use_ssl = use_ssl

    def send(self, to, subject, html, text, headers=None) -> bool:
        import smtplib
        import ssl
        from email.message import EmailMessage

        self.last_error = None
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        msg["Subject"] = subject
        for k, v in (headers or {}).items():
            msg[k] = v
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")

        ctx = ssl.create_default_context()
        # Same reason as ResendMailer: this runs inside a request on a public
        # signup form, so a refused login or an unreachable host must come back
        # as False with a reason, not as a 500.
        try:
            if self.use_ssl:
                with smtplib.SMTP_SSL(self.host, self.port, context=ctx,
                                      timeout=30) as s:
                    s.login(self.user, self.password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(self.host, self.port, timeout=30) as s:
                    s.ehlo()
                    s.starttls(context=ctx)
                    s.ehlo()
                    s.login(self.user, self.password)
                    s.send_message(msg)
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        return True


class ResendMailer(Mailer):
    """Resend's HTTP API. Free tier: 3,000 a month, 100 a day, one domain."""

    ENDPOINT = "https://api.resend.com/emails"
    name = "resend"

    def __init__(self, api_key: str, sender: str):
        self.api_key, self.sender = api_key, sender

    def send(self, to, subject, html, text, headers=None) -> bool:
        import requests

        self.last_error = None
        # Missing configuration is checked here rather than left to the API,
        # because an empty key produces a 401 whose message ("API key is
        # invalid") sends you looking for a typo in a key that was never set.
        if not self.api_key:
            self.last_error = "RESEND_API_KEY is not set in this process"
            return False
        if not self.sender:
            self.last_error = "TRACKER_MAIL_FROM is not set in this process"
            return False
        try:
            r = requests.post(
                self.ENDPOINT, timeout=30,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={"from": self.sender, "to": [to], "subject": subject,
                      "html": html, "text": text, "headers": headers or {}})
        except Exception as exc:
            # A network blip must not become a 500 on a public signup form.
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        if r.status_code >= 400:
            self.last_error = f"HTTP {r.status_code}: {r.text[:300]}"
            return False
        return True


def build(kind: str, data_dir: Path, config: dict | None = None) -> Mailer:
    """Pick a backend by name. Unknown names fall back to the outbox.

    Falling back rather than raising is deliberate: a misspelled ``--mailer`` on
    a scheduled job should write files nobody reads, not crash the nightly run
    or - worse - silently send nothing while reporting success.
    """
    config = config or {}
    if kind == "console":
        return ConsoleMailer()
    if kind == "smtp":
        return SmtpMailer(
            host=config.get("host", "smtp.gmail.com"),
            port=int(config.get("port", 587)),
            user=config["user"], password=config["password"],
            sender=config.get("sender"),
            use_ssl=bool(config.get("use_ssl")))
    if kind == "resend":
        return ResendMailer(config["api_key"], config["sender"])
    if kind != "file":
        print(f"  unknown mailer {kind!r}; writing to the outbox instead")
    return FileMailer(Path(data_dir) / "outbox")
