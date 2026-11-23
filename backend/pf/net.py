"""
One HTTP client for every feed, because each of them was getting it wrong in
its own way.

THE CERTIFICATE PROBLEM. Python on Windows does not populate OpenSSL's trust
store, so `urllib.request` raises

    SSL: CERTIFICATE_VERIFY_FAILED — unable to get local issuer certificate

against perfectly ordinary, correctly configured sites — the ECB's data portal
and FRED among them — while yfinance, running in the same process against the
same internet, works fine. The difference is that yfinance ships `certifi` and
urllib does not use it. That is the whole bug, and it is why this module
exists: every feed now goes through one client with one CA bundle.

THE BOT PROBLEM. CNN's Fear & Greed endpoint answers HTTP 418 to urllib no
matter what User-Agent string it sends, because the check is on the TLS
handshake, not the headers: a Python client fingerprints as a script before it
has sent a single header.

So three transports, tried in order, and the one that worked is recorded:

    curl_cffi   impersonates a real Chrome TLS fingerprint. Already installed —
                it is what yfinance uses to talk to Yahoo.
    requests    ordinary HTTPS with certifi's bundle.
    urllib      the standard library, with an SSL context built from certifi,
                and one last try with the system default.

Recording the transport matters. When a feed comes back, the Sources panel can
say HOW it came back — and "CNN answers curl_cffi but not requests" is the
difference between a feed that works and one that works until someone
simplifies this file.
"""

from __future__ import annotations

import ssl
import time
import urllib.error
import urllib.request
from typing import Optional

TIMEOUT = 20

# What a browser actually sends. The User-Agent alone is not enough for the
# endpoints that check, but it is enough for the ones that only look.
BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/csv, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,nl;q=0.8",
    "Cache-Control": "no-cache",
}


class FeedError(Exception):
    """Every transport failed. The attempts travel with the exception."""

    def __init__(self, url: str, attempts: list):
        self.url = url
        self.attempts = attempts
        last = attempts[-1] if attempts else {}
        super().__init__(f"{last.get('error') or 'all transports failed'} "
                         f"({len(attempts)} attempted)")


# --------------------------------------------------------------------------
# the CA bundle
# --------------------------------------------------------------------------

_CTX = None
_CA_SOURCE = "unknown"


def ssl_context() -> ssl.SSLContext:
    """An SSL context that trusts certifi's roots, falling back to the system."""
    global _CTX, _CA_SOURCE
    if _CTX is not None:
        return _CTX
    try:
        import certifi
        _CTX = ssl.create_default_context(cafile=certifi.where())
        _CA_SOURCE = f"certifi {getattr(certifi, '__version__', '')}".strip()
    except Exception:
        _CTX = ssl.create_default_context()
        _CA_SOURCE = "system trust store"
    return _CTX


def ca_source() -> str:
    ssl_context()
    return _CA_SOURCE


# --------------------------------------------------------------------------
# the transports
# --------------------------------------------------------------------------

def _try_curl_cffi(url: str, headers: dict, timeout: float) -> bytes:
    from curl_cffi import requests as cr          # yfinance's own transport
    r = cr.get(url, headers=headers, timeout=timeout, impersonate="chrome")
    if r.status_code >= 400:
        raise urllib.error.HTTPError(url, r.status_code, f"HTTP {r.status_code}",
                                     None, None)
    return r.content


def _try_requests(url: str, headers: dict, timeout: float) -> bytes:
    import requests
    r = requests.get(url, headers=headers, timeout=timeout)
    if r.status_code >= 400:
        raise urllib.error.HTTPError(url, r.status_code, f"HTTP {r.status_code}",
                                     None, None)
    return r.content


def _try_urllib(url: str, headers: dict, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context()) as r:
        return r.read()


def _try_urllib_plain(url: str, headers: dict, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


TRANSPORTS = [
    ("curl_cffi", _try_curl_cffi),
    ("requests", _try_requests),
    ("urllib+certifi", _try_urllib),
    ("urllib", _try_urllib_plain),
]


def fetch(url: str, headers: Optional[dict] = None, timeout: float = TIMEOUT,
          order: Optional[list] = None) -> dict:
    """
    Try each transport in turn. Returns a record rather than raising, so a
    probe can report what happened without a try/except around every call.

        {ok, body, transport, ms, attempts: [{transport, error, ms}]}
    """
    h = dict(BROWSER)
    h.update(headers or {})
    names = order or [n for n, _ in TRANSPORTS]
    attempts = []

    for name, fn in TRANSPORTS:
        if name not in names:
            continue
        t0 = time.time()
        try:
            body = fn(url, h, timeout)
            ms = round((time.time() - t0) * 1000)
            attempts.append({"transport": name, "error": None, "ms": ms})
            return {"ok": True, "body": body, "transport": name, "ms": ms,
                    "bytes": len(body), "attempts": attempts, "url": url}
        except Exception as e:
            attempts.append({"transport": name,
                             "error": f"{e.__class__.__name__}: {e}"[:200],
                             "ms": round((time.time() - t0) * 1000)})

    return {"ok": False, "body": b"", "transport": None, "attempts": attempts,
            "bytes": 0, "url": url,
            "error": attempts[-1]["error"] if attempts else "no transport available"}


def get(url: str, headers: Optional[dict] = None, timeout: float = TIMEOUT) -> bytes:
    """Bytes, or FeedError carrying every attempt."""
    r = fetch(url, headers, timeout)
    if r["ok"]:
        return r["body"]
    raise FeedError(url, r["attempts"])


def first_ok(urls: list, headers: Optional[dict] = None,
             timeout: float = TIMEOUT, accept=None) -> dict:
    """
    Try several URLs and keep the first that answers — and, optionally, whose
    body passes `accept`. Used where a publisher has moved a file and the new
    location has to be discovered rather than assumed.
    """
    tried = []
    for u in urls:
        r = fetch(u, headers, timeout)
        tried.append({"url": u, "ok": r["ok"], "bytes": r.get("bytes", 0),
                      "transport": r.get("transport"),
                      "error": r["attempts"][-1]["error"] if r["attempts"] else None})
        if r["ok"] and (accept is None or accept(r["body"])):
            r["tried"] = tried
            return r
        if r["ok"]:
            tried[-1]["error"] = "answered, but not in the expected format"
    return {"ok": False, "body": b"", "transport": None, "tried": tried,
            "error": "none of the candidate URLs answered with usable data"}


def transports_available() -> dict:
    """Which transports this install actually has, for the self-test."""
    out = {}
    for name, mod in (("curl_cffi", "curl_cffi"), ("requests", "requests"),
                      ("certifi", "certifi")):
        try:
            m = __import__(mod)
            out[name] = getattr(m, "__version__", "present")
        except Exception as e:
            out[name] = f"missing ({e.__class__.__name__})"
    return out
