# Setting it up

Het Koersbord runs on one Windows laptop: a Python server on port 8090 and
the pages it serves. Your data stays on that laptop; what goes out are the
requests to the public sources it reads.

---

## Install

You need **Windows 10 or 11** (POL reads scanned filings with Windows' own
OCR) and **Python 3.11 or newer**.

```bash
git clone <this repository's URL> het-koersbord
cd het-koersbord/backend
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-portfolio.txt
```

Then double-click `start-portfolio.bat`. It installs anything missing,
starts the server and opens `http://127.0.0.1:8090`. The bat uses paths
relative to itself, so the folder can live anywhere. Right-click it, then
*Send to → Desktop (create shortcut)*, for a one-click start.

It starts with an **empty ledger**.

## Your data

The ledger is derived entirely from transactions. In DEGIRO, download
Transactions.csv and Account.csv, and use Import on the home screen. The
importer is idempotent: check 6 in `verify.py` proves that re-importing the
same file changes nothing. So import a fresh export as often as you like.

None of this goes into git. The `.gitignore` keeps out:

| Never committed | Why |
|---|---|
| `backend/portfolio.db` | every transaction, every position, and your API keys in the `setting` table |
| any `.csv` | a DEGIRO export is your entire trading history |
| `backend/*_work/` | what Claude was given for each write-up, your holdings with it |
| `backend/ptt/data/` | POL's filings and its own database |
| `backend/.venv/` | machine-specific; rebuilt in a minute |

Git history is permanent. A database committed once stays recoverable from
the history even if a later commit deletes it. So it never goes in at all.

## Keys, all optional

Paste them into Settings on the home screen. They are stored in
`portfolio.db`, never in a file.

| Key | What it adds |
|---|---|
| FRED (free, fred.stlouisfed.org) | a fallback: the macro series come from FRED's public CSV, and the key is used when that fails |
| Finnhub (free) | general and macro news; without it, news comes from Yahoo per ticker |
| aisstream.io (free) | live tankers worldwide on the oil page (the Baltic needs none) |
| Anthropic API | the writer, if you would rather not use Claude Code |

The write-ups (the morning brief, ERN's reports, READ, Koers AI and the
weekly note) use **Claude Code** on your Claude plan by default. It needs
signing in once, from the morning brief's page.

## Two machines

**Copy the database file.** Close the app on both machines, then copy
`backend\portfolio.db` across: USB stick, OneDrive, anything. That one file
is your entire state: transactions, instruments, prices, FX history,
settings and API keys. Copy `portfolio.db-wal` and `portfolio.db-shm` too
**if they exist**. SQLite writes recent changes there, and copying only the
main file can lose the last few. Closing the app first normally folds them
back in and removes them.

**Do not open one database from both machines through OneDrive.** It is
tempting and wrong. SQLite in WAL mode does not survive a file-sync service
copying it mid-write. Two machines writing the same file through OneDrive
produce conflicted copies, not a merged ledger.

**One laptop is the live one at a time.** Finish on one, close the app,
copy `portfolio.db` across, carry on. If you forget and enter trades on
both, re-import the DEGIRO exports on one machine and let the ledger
rebuild itself.

## After an update

**Restart the app after pulling.** The HTML pages are read from disk on every
request, but the Python routes only exist in the running process. A pulled
update can therefore serve brand new pages whose own links 404 against the
old server. The pages check for this and show a bar telling you to restart;
check 56 in `verify.py` keeps the two build numbers honest.

Then confirm the install is sound:

```bash
python verify.py
```

That runs two hundred and sixty-nine checks against your own ledger, and all
of them should pass. To run them on a copy instead, set `PF_DB` to its path.
Two skip when they have nothing to work with:
- 6 wants your two CSV exports (`TXCSV`, `ACCSV`);
- 15 tests the turbo pricing model, which is off while turbos are valued at
  zero (the default).

---

## Live tankers worldwide (optional)

The oil page (`/m/MKT/oil`) shows live tankers in the Baltic with no key. For
the whole world it needs a free key from **aisstream.io**: sign in there with
GitHub, open *API keys*, create one, and paste it into Settings on the home
screen. The key is stored in `portfolio.db`, so it travels with the database
when you copy it across; on a laptop with its own database, paste it there
too. The stream runs only while the oil page is open.

The globe view on the same page loads d3 from cdnjs.cloudflare.com the first
time you press ◐; the flat map needs nothing from outside.

## The watchlist's nightly scan

WCH (`/m/WCH`) scans every US and European company above $5bn once a weekday,
at 22:45 Amsterdam time, and at start-up when that scan was missed (the laptop
was off). The first scan takes fifteen to twenty minutes: it reads statements
and estimates for about a thousand companies, one request at a time. After
that it takes a few minutes, because statements are kept for five to nine days
and only the estimates are read again each night. The momentum leaders, near their highs,
get the same check, which adds about a minute the first time. Since build 48
the 300 largest US and 200 largest European companies are read in full
too, wherever they stand, so Koers AI has the broad market: some 290 more
companies, about a third more time each night. Nothing needs a key.

The same scan keeps each night's analyst consensus for the companies it
read (`est_day` in `portfolio.db`): forecasts, targets, ratings, one row a
company on the days something moved, a few megabytes a year. It travels with
the database. The stock page adds a row whenever it is opened, and reads the
later years' forecasts from Nasdaq, which needs no key.

Yahoo limits how fast one machine may ask. The scan stays under that limit,
and if Yahoo still says "too many requests" it pauses for five minutes, at
most twice, and then finishes with what it already has. The other pages read
Yahoo through the same connection, so a scan running at the same moment as a
busy page can make either one slower, never wrong.

## Articles (READ)

READ (`/m/READ`, also `TOP`, `ARTICLES`) writes articles about the news that
matters to the market and your book, with the same writer as the morning
brief: Claude Code on your Claude plan (no charge per article), or an API key
if you switched the writer to one. It needs Claude Code signed in once, which
the morning brief's page does. The editor scans at 07:15, 16:30 and 22:45 on a
weekday and at 09:30 on Saturday and Sunday (with the week in review and the
week ahead), and writes at most eight articles a day: a scan takes a minute or
two, each article five to ten. A scan missed by more than three hours (the
laptop was off) waits for the next one. Untick *on schedule* on the page to
write only when you press *Scan now*. The day's live coverage at the top needs
nothing: it comes from Yahoo's RSS feeds.

What each scan and article was written from is kept in `backend/read_work/`,
which git ignores; the articles themselves are in `portfolio.db`.

## Koers AI (KAI)

KAI (`/m/KAI`) is a paper-trading contest: Claude manages €100,000 of paper
money against you and the S&P 500. It does nothing until you press *Start*
on its page. Its first decision waits until the watchlist's nightly scan
has read the day's close, so on a weekday evening it runs after that scan.
After that it decides every Friday after the close (from 23:00
Amsterdam, once the watchlist's scan has run). On another weekday evening
after a holding reported or moved 5% or more, it may check again.

Each turn is one run of the same writer as the morning brief and READ:
Claude Code on your plan, signed in once on the morning brief's page. It
runs without web search and takes a few minutes. A Friday missed because the
laptop was off is made up when the server next runs while the markets are
shut. Orders fill at the next real opening price, checked every five minutes
while the server runs.

Koers AI sees the broad market: the 100 best quality companies among the
largest and those the scan reads, and every stock you hold, mixed in by
sector so it cannot tell which are yours. Under each decision, *What it was
sent* shows the data it decided from, word for word.

Everything it holds and decided is in `portfolio.db` (`kai_*` tables), so it
travels with the database. The rule below matters doubly here: two laptops
running on two copies would each play their own game, and only one copy's
would survive.

## Politicians' trades (POL)

POL (`/m/POL`) shows what members of Congress buy and sell, from their own
filings. It runs the author's politician-trading-tracker, copied into
`backend/ptt/`, as its own process.

The first build is started from the page (*Build the data*). It reads every
House filing since 2020 from the Clerk (about an hour), the Senate's from the
EFD portal, the scanned filings by Windows OCR, then prices from Yahoo: a few
hours in all. After that it runs every night at 05:30 Amsterdam, or at the
next start when the laptop was off.

Its data is **not in `portfolio.db`**. It lives in `backend/ptt/data/`
(`tracker.db` and the downloaded PDFs, which git ignores), so copying
`portfolio.db` does not bring it across. Either copy that folder too, with the
app closed, or press *Build the data* on the other laptop and let it rebuild.

What it needs is installed by `start-portfolio.bat` from
`requirements-portfolio.txt`: pdfplumber, PyYAML, OpenCV, and pywinrt for
Windows OCR (with its English recogniser, which Windows ships). The Senate
portal is read through `curl_cffi`, which the terminal already uses.

## The week in review and the health page

WEEK (`/m/WEEK`) is built each Saturday at 08:00 Amsterdam (or at the next
start before Monday 09:00), and Claude's note follows through the writer chosen
in Settings, once a week. SYS (`/m/SYS`) needs nothing: it reads what the
background jobs record. A job that has failed for a day puts a line on the home
screen.

From build 52 the database looks after its own size. Each day, copies in the
cache that can be fetched again are deleted once unrefreshed for three to eight
weeks, and the space is given back to the disk. The first start after the
upgrade compacts the file once, in a couple of seconds.

## Institutional flow (FLOW)

FLOW (`/m/FLOW`) needs nothing to set up. It collects by itself after New
York's close, one request at a time: the CFTC, the OFR, SqueezeMetrics,
unicorn.us.com, the WSJ's market diary, Cboe and FINRA, none with a key.

The first evenings it reads its back files: a year of Cboe's put/call files,
about sixty each ten-minute run, and two months of FINRA's short volume
(about 550 KB a session), eight a run. *Refresh now* on the page takes
larger steps. The WSJ's diary keeps no history, so the NYSE's and Nasdaq's
own record starts the day this build first runs.

Everything it keeps is in `portfolio.db` (`flow_diary`, `flow_pc`,
`flow_short` and the cache), so it travels with the database.

## The indices page

IDX (`/m/IDX`) needs nothing to set up. The first time it opens it builds
both indices in the background: every member's prices for three years, about
two minutes for the S&P 500 and half a minute for the Nasdaq 100, one request
at a time. After that it is rebuilt each weekday evening after 22:30 Amsterdam
time, once the watchlist's scan has run (it waits until 23:30 at most), and at
start-up when the last build is more than a day old. The *Rebuild* button
reads everything again. The S&P's weights come from State Street's daily SPY
file and the Nasdaq's members from nasdaq.com; neither needs a key. Each
evening adds a row to `idx_day` in `portfolio.db`, so the record of breadth,
concentration and the forward P/E travels with the database.

## The forecast's history

The forecast (`/m/MVO/forecast`, also `FCST`) needs nothing to set up. Its 150 years of history ship with
the code in `backend/pf/fcst_seed.json`, built from the Macrohistory Database
(free for non-commercial use, CC BY-NC-SA 4.0) and Shiller's data. Once a
month the page reads the latest US CAPE from shillerdata.com, and keeps the
last one if that fails. Each week, the first visit stores the range given for
the next twelve months, so that a year later the page can show how the book
did against it.

## Markowitz, the euro, options strategies, the macro calendar

None of these need a key or any setup. What runs on its own:

* **MVO** (`/m/MVO`) reads three years of prices for the holdings and five
  funds the first time, one request after another, in about a minute. After
  that it serves the stored prices and refreshes them behind the page every
  twelve hours. Your views are kept in `portfolio.db`. The home screen's
  constellation uses the same prices.
* **EUR** (`/m/EUR`) rebuilds every two hours in the background, from the
  ECB, FRED, the Bundesbank, the CFTC, the World Bank and The Economist's Big
  Mac data, so its alerts and the brief's euro line have something to read.
  While New York is open it reads FXE's options once a session.
* **OPT's strategies** read the option chains OPT already stores for your
  holdings, and Cboe's strategy histories twice a day.
* **MAC** (`/m/MAC`) reads every quarter hour: the releases the brief already
  fetched, their actuals from FRED and the ECB once published, and EUR/USD and
  S&P 500 future reactions from Yahoo's five-minute bars. The record of
  reactions starts in October 2026 and grows from there.

## Smart App Control

Windows 11's **Smart App Control** loads a program or compiled library only if
it is signed or Microsoft's reputation service knows it. Python packages ship
unsigned compiled files (`.pyd`), so it comes down to reputation, file by file.
It starts in *evaluation* mode, where it only watches, and Windows later
switches it on or off by itself. On the author's laptop it switched on
overnight to 29 September 2026, and at once refused `pandas\_libs\interval.pyd` from pandas
3.0.6. Without pandas, yfinance cannot import, so no prices, no history and no
brief builds. The only sign was a notification and empty panels.

**What the app does about it.** `requirements-portfolio.txt` pins pandas to
3.0.5, whose files load (all but two SAS-file readers the app never uses).
`start-portfolio.bat` installs the pin on its next start. If Windows blocks a
library the app needs, every page shows an amber bar naming it.

**Seeing what was blocked.** Event Viewer → Applications and Services Logs →
Microsoft → Windows → CodeIntegrity → Operational: event 3077 names the file,
3118 has the details. Or in PowerShell:

```powershell
Get-WinEvent -LogName "Microsoft-Windows-CodeIntegrity/Operational" -MaxEvents 200 | Where-Object Id -eq 3077 | Select-Object TimeCreated, Message
```

**Upgrading a library.** Upgrade, restart, and run `verify.py`. If the bar
appears, go back to the version that loaded. A file Windows has not seen before
can be refused once while it asks Microsoft, then load on the next try. One it
keeps refusing has no reputation: pandas 3.0.6 was refused over a hundred times.
Reinstalling the same version does not help, because the verdict follows the
file's contents.

**Turning Smart App Control off** (Windows Security → App & browser control →
Smart App Control settings) makes all of this go away. That is your call: it
is a real protection against unknown programs, and on many Windows 11 versions
it cannot be turned back on without resetting Windows. The settings page says
whether that applies to yours.

**Claude Code from the Microsoft Store app.** The Store build of the Claude
desktop app is a packaged app with a private AppData: its copy of Claude Code
lives in `%LOCALAPPDATA%\Packages\Claude_<id>\LocalCache\Roaming\Claude\claude-code`,
visible under `%APPDATA%` only to programs the app itself starts. The server
looks in both places, so ERN and ALRT find it however the server was started.
