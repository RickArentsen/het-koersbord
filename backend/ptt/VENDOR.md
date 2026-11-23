# The politician trading tracker, inside the terminal

This folder is the pipeline of
[RickArentsen/politician-trading-tracker](https://github.com/RickArentsen/politician-trading-tracker)
at commit `8bbf628` (3 November 2026), copied in for POL (`/m/POL`, build 49).
`README.md` beside this file is that repository's own, and describes the
method in full.

What is here, unchanged: `src/` (every module), `scripts/daily_scan.py`,
`refresh.py`. What is not: the public site (`web/`, `site_wsgi.py`), the
tests, the deploy scripts and the data. The terminal draws its own page from
the database instead.

The one addition is `src/winsdk/`: `ocr.py` reads scanned filings with
Windows OCR through `winsdk`, which has no build for Python 3.13, and those
five modules hand it the same classes from pywinrt (`winrt-*`).

Fixes made here, all also needed in the repository:

1. `cmd_fundamentals` in `src/cli.py` printed `stats["no_cap"]`, a key
   `market.sync_fundamentals` no longer returns (it says `no_data`). The
   fetch finished, then the summary line raised `KeyError: 'no_cap'` and the
   command exited 1, which stopped the first build on 4 November. It now
   reads `no_data`.
2. **Yahoo's refusals were stored as answers.** yfinance does not raise when
   Yahoo refuses it. Under a rate-limited crumb (HTTP 429), every `.info` is
   a 401 "Invalid Crumb" that comes back as an all-empty dict, and an unknown
   symbol (a 404) looks the same. So `sync_fundamentals`'s "a request that
   raised writes nothing" never fired, and `sync_sectors` stored every
   result anyway. On 4 November all 2,104 sector lookups and the first 453
   market caps were refusals, saved as "no sector" and "no market cap". Since
   only tickers without a row are asked, they would never have been asked
   again. Every purchase then fell outside its member's remit, and the
   in-remit strategy came out empty.
   - The fix is `market.yahoo_info` and `market.sweep`. A real answer always
     carries a `quoteType`; one without it is held back.
   - When 25 such answers pile up, the sweep asks a symbol Yahoo surely
     knows (`CANARY`, AAPL). If AAPL answers, they were unknown symbols and
     are stored. If it doesn't, Yahoo is refusing, and the sweep stops with
     them unrecorded, for the next run.
   - Both sweeps use it. `cli.py sectors` and `cli.py fundamentals` then exit
     75.
   - `sync_sectors` also reads sector and industry from the market caps'
     cached answers first, and asks Yahoo only for the rest.
3. `cmd_strategy` in `src/cli.py` raised `KeyError: 'final_strategy'`
   printing a curve with no trades in it. It now prints "no trades".

The pipeline runs as its own process (`python ptt/scripts/daily_scan.py`,
`python ptt/src/cli.py ...`), never inside the server: its modules import one
another by bare name (`import db`, `import prices`), which would collide in
the server's process. Its database and downloads live in `ptt/data/`, which
git ignores. `pf/pol.py` starts the runs and reads the database.

To take a newer version of the tracker: copy its `src/`, `scripts/daily_scan.py`
and `refresh.py` over these, keep `src/winsdk/`, and run `verify.py`.
