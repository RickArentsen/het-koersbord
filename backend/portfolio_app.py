"""
Het Koersbord — the terminal.

Runs standalone: serves both the JSON API and the dashboard itself, so the
whole thing is one process on one port. That is deliberately the same shape
a Tauri desktop build needs later (static frontend + FastAPI sidecar).

    python portfolio_app.py            # http://127.0.0.1:8090

The existing terminal app on :8080 is untouched. To fold these endpoints
into it instead, mount the router:

    from pf.api import router as portfolio_router
    app.include_router(portfolio_router)
"""

from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path

try:
    from fastapi import FastAPI, Request
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import FileResponse, HTMLResponse
    from fastapi.staticfiles import StaticFiles
except ModuleNotFoundError as exc:            # almost always the wrong python
    _venv = Path(__file__).resolve().parent / ".venv" / "Scripts" / "python.exe"
    _unix = Path(__file__).resolve().parent / ".venv" / "bin" / "python"
    _has = _venv.exists() or _unix.exists()
    print(f"\n  {exc.name} is not installed for {sys.executable}.\n")
    if _has:
        print("  The dependencies live in this project's virtual environment, not in")
        print("  your system Python. Start it with that interpreter instead:\n")
        print(f'      {_venv if _venv.exists() else _unix} portfolio_app.py\n')
        print("  Or double-click start-portfolio.bat in the folder above, which does")
        print("  exactly that and installs anything missing first.\n")
    else:
        print("  Create the environment and install the dependencies:\n")
        print("      python -m venv .venv")
        print("      .venv\\Scripts\\python.exe -m pip install -r requirements-portfolio.txt\n")
    raise SystemExit(1)

from pf.api import router
from pf import store, BUILD

BASE = Path(__file__).resolve().parent
STATIC = BASE / "static"

app = FastAPI(title="Het Koersbord")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000",
                   "http://localhost:8090", "http://127.0.0.1:8090"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}     # testserver: FastAPI's TestClient in verify.py
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _host_name(h: str) -> str:
    h = (h or "").strip().lower()
    return h[: h.find("]") + 1] if h.startswith("[") else h.split(":")[0]


def foreign(method: str, headers) -> str | None:
    """
    Why a request is refused, or None (build 52). The terminal listens on 127.0.0.1
    only, but a web page you visit can still reach it two ways:

      DNS rebinding   a page points its own name at 127.0.0.1 and then reads the
                      answers as its own: your holdings, your trades. Its requests
                      carry its own name in the Host header, so only this
                      machine's names are answered.
      a plain POST    any page can send a form to 127.0.0.1:8090 and browsers
                      deliver it without asking; the page cannot read the answer,
                      but the POST still starts what it names: a Claude article,
                      a Koers AI turn, a scan. Browsers mark such a request with the
                      other site in Origin (and Sec-Fetch-Site: cross-site), so a
                      state-changing request from another site is refused.
    """
    if _host_name(headers.get("host")) not in LOCAL_HOSTS:
        return "this terminal answers only to 127.0.0.1"
    if method.upper() in UNSAFE:
        o = headers.get("origin")
        if o is not None:
            from urllib.parse import urlsplit
            name = (urlsplit(o).hostname or "").lower() if o != "null" else ""
            if name not in LOCAL_HOSTS:
                return "a change from another site's page is refused"
        if (headers.get("sec-fetch-site") or "").lower() == "cross-site":
            return "a change from another site's page is refused"
    return None


@app.middleware("http")
async def local_only(request: Request, call_next):
    why = foreign(request.method, request.headers)
    if why:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": why}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def no_cache(request: Request, call_next):
    """
    The dashboard is one static HTML file that changes whenever the app is
    updated. Without this the browser happily serves yesterday's copy after an
    update, and you spend an evening debugging a bug that was already fixed.
    """
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


app.include_router(router)


# Screens. Explicit routes rather than one file per stub: every unbuilt module
# renders from module.html, so there are no nine near-identical files to drift.
def _page(name: str) -> FileResponse:
    return FileResponse(STATIC / name)


@app.get("/", include_in_schema=False)
def home_page():
    return _page("index.html")


@app.get("/portfolio", include_in_schema=False)
def portfolio_page():
    return _page("portfolio.html")


@app.get("/portfolio.html", include_in_schema=False)
def portfolio_page_alias():
    return _page("portfolio.html")


# Modules that are built get their own file; the rest all render from
# module.html as honest placeholders. Registered before the catch-all so the
# built ones win.
BUILT_MODULES = {"NEWS": "news.html", "MKT": "market.html", "OIL": "oil.html",
                 "GEO": "geo.html", "BOND": "bonds.html", "ERN": "earnings.html",
                 "ALRT": "brief.html", "RRG": "rrg.html", "OPT": "options.html",
                 "SPLC": "supply.html", "WCH": "watch.html",
                 "MVO": "mvo.html", "EUR": "euro.html", "MAC": "mac.html", "FCF": "fcf.html", "IDX": "idx.html",
                 "READ": "read.html", "KAI": "kai.html", "POL": "pol.html", "FLOW": "flow.html",
                 "SYS": "sys.html", "WEEK": "week.html"}


@app.get("/m/MKT/bonds", include_in_schema=False)
def market_bonds_page():
    """The bond market, as a second page of MKT. /m/BOND is the same page."""
    return _page("bonds.html")


@app.get("/m/MVO/forecast", include_in_schema=False)
def mvo_forecast_page():
    """The forecast, as MVO's second page (build 32). /m/FCST leads here."""
    return _page("fcst.html")


@app.get("/m/FCST", include_in_schema=False)
def fcst_moved():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/m/MVO/forecast", status_code=307)


@app.get("/m/MKT/oil", include_in_schema=False)
def market_oil_page():
    """The oil market, as a third page of MKT. /m/OIL is the same page."""
    return _page("oil.html")


@app.get("/m/ERN/{symbol}", include_in_schema=False)
def earnings_event_page(symbol: str):
    """ERN with one company's report open. The page reads the symbol (and ?d=) from the URL."""
    return _page("earnings.html")


@app.get("/m/OPT/{symbol}", include_in_schema=False)
def options_page(symbol: str):
    """OPT for one underlying. The page reads the symbol from the URL."""
    return _page("options.html")


@app.get("/m/SPLC/{symbol}", include_in_schema=False)
def supply_page(symbol: str):
    """SPLC for one company: who supplies it, and who it sells to."""
    return _page("supply.html")


@app.get("/t/{symbol}", include_in_schema=False)
def ticker_page(symbol: str):
    """One page per security. The symbol is read from the URL by the page."""
    return _page("ticker.html")


@app.get("/m/{code}", include_in_schema=False)
def module_page(code: str):
    return _page(BUILT_MODULES.get(code.upper(), "module.html"))


@app.exception_handler(404)
async def not_found(request: Request, exc):
    """
    A bare {"detail":"Not Found"} is the least useful thing to hand someone.
    Say where they are, what exists, and the one thing that usually causes it.
    """
    if request.url.path.startswith("/api/"):
        # A route that exists and answered 404 on purpose ("no security under
        # XYZ") keeps its own reason; only a path with no route gets this one.
        detail = getattr(exc, "detail", None)
        if detail and detail != "Not Found":
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": detail}, status_code=404)
        return HTMLResponse(f'{{"detail":"no such endpoint: {request.url.path}"}}',
                            status_code=404, media_type="application/json")
    return HTMLResponse(f"""<!doctype html><meta charset="utf-8">
      <title>Not found</title><link rel="stylesheet" href="/term.css">
      <div class="wrap" style="max-width:760px;padding-top:3rem">
        <div class="panel"><header><h2>Not found</h2>
          <span class="hint">build {BUILD}</span></header>
          <div class="body">
            <div class="note"><b>{request.url.path} does not exist</b>
              If this was a link from inside the app, the server you are running is
              older than the pages on disk — stop it and start it again.</div>
            <div class="grp">Screens</div>
            <div class="rows">
              <div class="row"><span class="nm"><a href="/">/</a></span><span class="dim">home</span><span class="ch dim">live</span></div>
              <div class="row"><span class="nm"><a href="/portfolio">/portfolio</a></span><span class="dim">portfolio</span><span class="ch dim">live</span></div>
              <div class="row"><span class="nm"><a href="/m/MKT">/m/CODE</a></span><span class="dim">a module</span><span class="ch dim">placeholder</span></div>
            </div>
          </div></div></div>""", status_code=404)


app.mount("/", StaticFiles(directory=str(STATIC), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    store.init_db()
    store.checkpoint()          # fold any stale -wal sidecar into the database
    # Once, before anything else writes: incremental auto-vacuum, so the space
    # the daily cache prune frees goes back to the disk (build 52). The first
    # start after the upgrade rewrites the file once, some seconds.
    if store.vacuum_mode() != 2:
        print("Compacting the database once (incremental vacuum)...")
        store.make_incremental()
    store.ensure_maintenance()
    # News and the two calendars are fetched by a background thread from the
    # moment the app starts, so the first page load reads a warm cache instead
    # of waiting on three other people's servers.
    from pf.brief import BRIEF
    BRIEF.start()
    # The earnings watcher: snapshots consensus before reports and watches
    # today's releases from your holdings for the numbers.
    from pf import earnlive
    earnlive.ensure()
    # The morning brief: built at 06:30 on weekdays, kept for the archive.
    from pf import morning
    morning.ensure()
    # OPT: one reading of every holding's option chain per session, so the
    # history of implied volatility builds itself.
    from pf import options
    options.ensure()
    # WCH: the watchlist scans every weekday evening after New York closes,
    # and at start when that scan was missed.
    from pf import watch
    watch.ensure()
    # EUR: the euro page rebuilt every two hours, so its alerts and the brief's
    # line have something to read; FXE's options read once a session.
    from pf import euro
    euro.ensure()
    # MAC: the macro calendar's actuals and reactions, every quarter of an hour.
    from pf import macro
    macro.ensure()
    # IDX: the S&P 500 and the Nasdaq 100 rebuilt each evening after New York
    # closes, once the watchlist's scan is done, and at start when the last
    # build is more than a day old.
    from pf import idx
    idx.ensure()
    # Tickers for shares bought since the seed map was written, looked up on
    # Yahoo in the holding's own currency (build 41).
    from pf import service
    service.ensure_tickers()
    # READ: the editor scans three times a weekday (07:15, 16:30, 22:45) and
    # once at a weekend, and writes at most eight articles a day.
    from pf import reader
    reader.ensure()
    # KAI: Koers AI settles its orders at the next open, decides on Friday after
    # the close and checks on an evening a holding reported or moved 5%;
    # nothing until Start is pressed on its page.
    from pf import koersai
    koersai.ensure()
    # POL: the politician tracker's nightly run at 05:30 (new filings, prices,
    # scores), in its own process; idle until its first build is done.
    from pf import pol
    pol.ensure()
    # FLOW: up and down volume, hedge funds' futures and leverage, dark pools,
    # put/call and short volume, collected after New York's close, a source at a time.
    from pf import flow
    flow.ensure()
    # WEEK: the week in review, built each Saturday at 08:00 with Claude's note after it.
    from pf import weekly
    weekly.ensure()
    port = int(os.environ.get("PF_PORT", "8090"))
    print(f"Het Koersbord — build {BUILD} — http://127.0.0.1:{port}/")
    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        except Exception:
            pass
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
