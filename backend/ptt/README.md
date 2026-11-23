# Congress Trade Tracker

Two front ends over one pipeline:

| | | |
|---|---|---|
| **Trade of the House** | `python src/cli.py site` | The public site. One filtered strategy: in-remit trades from members with a profitable in-remit record. |
| **Research dashboard** | `python src/cli.py serve` | Everything, unfiltered — for interrogating the data. |

For the site, run `python src/cli.py strategy` first; it caches the equity curve
so no page request rebuilds it.


Tracks stock trades disclosed by sitting members of the U.S. House of
Representatives, scores whether copying each member would actually have beaten
the S&P 500, and alerts on new purchases.

Everything is built from primary sources — no third-party trade-tracking API.

```
Clerk of the House  ──►  PTR index (yearly ZIP)  ──►  PTR PDFs
                                                        │
                        pdfplumber column parser  ◄──────┘
                                                        │
congress-legislators ──►  committees + roster           ▼
                                                    SQLite ──► backtest ──► Flask site
Yahoo Finance ───────►  adjusted daily prices  ─────────┘
```

## Quick start

```bash
pip install -r requirements.txt

python src/cli.py ingest --from 2020 --to 2026   # download + parse filings
python src/cli.py prices                          # price history for traded tickers
python src/cli.py sectors                         # classify tickers by industry
python src/cli.py backtest                        # score trades, rank members
python src/cli.py serve                           # http://127.0.0.1:5000
```

Or run the whole update in one go with `python refresh.py`, which is what you
would put on a schedule.

`python report.py` writes `report.html` — a single self-contained findings page
(no external CSS, fonts or scripts) summarising the headline result, the ranked
members, and the limitations. Every figure is recomputed from the database, so
it is always current with whatever you have ingested.

`ingest` is resumable: filings already parsed are skipped, and PDFs are cached
on disk under `data/raw/ptr/<year>/`. Re-running it later only fetches what is
new, which is what you want on a schedule.

**Run the stages one at a time.** They share a single SQLite file, and an
`ingest` running alongside `sectors` or `backtest` will collide — re-parsing a
filing mints new transaction ids underneath the scorer. Both stages now retry on
a busy lock and skip rows that vanish mid-run rather than dying, but the
supported order is sequential, which is what `refresh.py` does. Reading is
always safe: the dashboard can stay up during an ingest.

Check what you have at any point:

```bash
python src/cli.py status
python src/cli.py verify            # data-quality checks over the database
python src/cli.py alerts --peek     # new buys since the last alert run
```

`verify` is worth running after any parser change. Every check in it corresponds
to a bug that actually occurred during development — leaked sub-table labels,
NUL bytes reaching storage, entries dated before the filing was public — so it
is cheap regression cover for the whole pipeline, not a decorative health check.

To take the data somewhere else — a notebook, R, Excel:

```bash
python src/cli.py export -o data/trades.csv
```

That writes one row per disclosed transaction, joined to the member, their party
and committees, the ticker's sector, the entry price and the forward returns,
with `alpha_1m/3m/6m/12m` precomputed so you don't have to redo the benchmark
join.

## Why the entry date matters

This is the part most congressional-trading analyses get wrong, so it is worth
being explicit.

Under the STOCK Act a member files a Periodic Transaction Report up to **30 days
after being notified** of a trade and no later than **45 days after the trade
itself**. Nobody outside the member's household can act on the trade until that
PDF is published.

So a backtest that measures returns from the *transaction date* is measuring a
trade that was never available to you. This project scores every purchase from
**the first market open after the filing date** — the earliest a reader of this
tool could have bought. The average lag per member is reported alongside their
alpha, because that lag is exactly what erodes the edge.

## What gets measured

For each disclosed purchase, over 1 / 3 / 6 / 12-month windows:

```
alpha = (exit_price / entry_price - 1) - (SPY_exit / SPY_entry - 1)
```

Every purchase is scored **twice on the identical set of trades** — once from the
member's own transaction date, once from the public filing date. That separates
two questions a single number conflates:

| Metric | Question it answers |
|---|---|
| Their own 3m alpha | Does this member pick well at all? |
| Avg 3m alpha | Does any of that edge survive until *you* can act? |
| Lost to the lag | The difference between the two |

A member can look excellent on the first and worthless on the second. Showing
that gap is the main thing this tool exists to do.

Aggregated per member into: average and median alpha, hit rate (share of buys
that beat SPY), a **t-statistic**, average disclosure lag, and a copy-trade
equity curve.

### Position size

Disclosed amounts run from $1,001 to over $5,000,000, and **10,264 of 12,810
scored buys sit in the smallest band**. So an equal-weighted mean answers "how
did the average *filing* do", which is not the question you asked. Every metric
is therefore reported both ways:

| | |
|---|---|
| `avg_alpha_*` | equal-weighted — the average filing |
| `wt_alpha_*` | weighted by the midpoint of the disclosed range — the money |

They disagree, and the gap is the point: pooled over 12 months the equal-weighted
alpha is **+0.61%** while the dollar-weighted alpha is **+3.13%**. The large
positions carry the result. The leaderboard and the dashboard curve both have a
weighting toggle; `copy_trade_curve(..., weight="size")` does the same in code.

### Options

Option purchases were originally excluded, which quietly removed the trades the
most-discussed filers are actually known for — options are **1.5% of purchases
but a third of all disclosed purchase dollars**. They are now scored against the
**underlying** and flagged (`n_options`). The filing carries no strike or expiry,
so this is a directional signal on the ticker, never a replication of the trade,
and the UI says so wherever the number appears.

### Default horizon

Twelve months. A median disclosure lag of ~39 days already consumes a large part
of a three-month window, and the biggest positions are long-dated. All four
horizons (1/3/6/12m) remain available.

The t-statistic is not decoration. Around 200 members trade actively; if you
rank them by mean return, someone finishes first by chance. `mean / (stdev /
sqrt(n))` at three months tells you whether a member's average is large relative
to its own variability. Below roughly |t| = 2, treat the ranking as noise. The
leaderboard shows n and t on every row and defaults to a 10-trade minimum.

**A pooled t-statistic over all trades is not trustworthy**, because the trades
are not independent — members pile into the same names on the same days, and a
few very active filers contribute hundreds of correlated rows. `report.py`
therefore also computes a **member-clustered** statistic: collapse each member to
a single mean, then test across members. Far fewer observations, but genuinely
independent ones. The headline verdict is based on the clustered figure; the
pooled one is reported next to it and is always the more flattering of the two.

## The parser

House PTRs are text-based PDFs with a seven-column transaction table. The parser
(`src/ptr_parser.py`) works from pdfplumber word coordinates rather than raw
text, for three reasons found by inspecting real filings:

1. **Raw text extraction welds the two date columns together** — `12/12/2025`
   and `01/06/2026` come out as `12/12/202501/06/2026`. Column geometry keeps
   them apart.
2. **The filing system has shipped four different renderings**, and all of them
   must be handled or the older filings fail silently:
   - 2021+ — lowercase glyphs carry no character code, so `FILING STATUS:`
     arrives as `F\x00\x00\x00\x00\x00 S\x00\x00\x00\x00\x00:`.
   - 2020 — literal mixed case: `FIlINg sTaTus:`, `[sT]`, `oR03`, and symbols
     like `(BaC)`, `(gOOgl)`, `(DIa)`.
   - 2020–21 — the table header itself in lower case:
     `iD | owner | asset | transaction … amount | cap.`
   - 2019–20 — **seven columns, not eight** (no "Cap. Gains > $200?"), with a
     broken font subset that renders the labels as CJK and private-use glyphs:
     `Fﾛﾞﾛἠﾙ SϹ￿Ϲﾟ;:`. The transaction data itself is clean.

   Each was found by auditing the output, not by reading a spec. The second
   let label text run into the asset name, which dragged the *next* row's
   ticker into the current one — DIA resolved as BX, BDX as BRK.B. The third
   was worse and quieter: an exact header match rejected the table outright,
   marking **983 filings — about a quarter of 2020–21 — as having no
   transactions**, which the ingest counters had folded into "parsed". The
   fourth cost a further 135 filings for the same reason: a required column
   that simply did not exist yet.

   Headers, labels and symbols are now all matched case-insensitively, the
   trailing column is optional, unreadable labels are detected structurally
   (a colon preceded by garbled glyphs) rather than by name, and a denylist
   stops ordinary parenthesised words like `(partial)` becoming symbols.
   `no_table` is counted separately so this class of failure is visible rather
   than reported as success.
3. **Free-text descriptions spill across column boundaries.** A row only counts
   as a transaction if the type column holds a valid code *and* the date column
   holds a real date; the table is cut off at the asset-code footnote.

After any parser change, re-parse the cached PDFs — nothing is re-downloaded:

```bash
python src/cli.py ingest --from 2020 --to 2026 --reparse --skip-roster
```

Column boundaries are re-derived from the table header on every page, so the
parser survives layout drift between filing-system versions.

Scanned paper filings have no text layer and are recorded as
`parse_status='no_text'` rather than silently producing zero trades — so the
coverage gap is visible instead of hidden.

Run the regression tests — they fetch five real filings once, cache them, and
cover all three layout renderings:

```bash
python tests/test_parser.py
```

## Committee access score

`src/congress.py` assigns each committee a 0–1 weighting for how much
market-moving non-public information a seat plausibly exposes a member to, based
on the industries it legislates for, whether it receives classified or
pre-release briefings, and whether it directs federal spending. Financial
Services, Ways and Means, Energy and Commerce, and Intelligence sit at the top;
Ethics and House Administration at the bottom. Chairs and ranking members get a
1.15–1.30× multiplier.

A member's score is their **strongest** seat, plus a capped breadth bonus for
additional seats (subcommittees count for less, since their jurisdiction already
sits inside the parent's), normalised by the strongest seat anyone can hold —
chair of a top-tier committee.

The first version of this summed across every seat, and because members hold
about seven each, almost everyone saturated at 1.0: mean 0.947, and the "ranking"
was alphabetical noise. The current formula spreads members across 0.23–1.00 with
a median of 0.69, and the top of the list is the people you would expect —
the chairs of Financial Services, Ways and Means and Energy and Commerce, then
the ranking members of the same committees.

**This is a stated editorial judgement, not a measurement**, and it is not
evidence about any individual trade. It exists to give context to a name.

## Committee / sector overlap — the one result that holds up

`src/sectors.py` classifies each traded ticker into the same sector taxonomy the
committee weights use, then flags purchases where a member bought into an
industry their own committee has jurisdiction over — an Armed Services member
buying a defence contractor, say. Those alerts are boosted in the ranking.

This turns out to be the sharpest test of the "inside information" premise, and
unlike the access score it produces a real effect:

| | trades | 12-month alpha |
|---|---:|---:|
| Bought **inside** their committee's remit | 2,635 | **+2.66%** |
| Everything else those same members bought | 7,326 | **−0.46%** |
| Difference | | **+3.12pp** (t = 2.03) |

Every member page carries a "Committees vs. what they actually bought" table
listing each ticker, its sector, and whether it falls in that member's remit,
with the in-remit and out-of-remit alpha side by side.

Read it carefully: the pooled t-statistic flatters itself because one member's
trades are not independent of each other, and buying well in an industry you
work on is exactly what expertise looks like too. It is evidence of an
informational edge, not of wrongdoing.

Yahoo's `sector` field alone is not enough for this: Lockheed Martin is sector
"Industrials", and only its `industry` ("Aerospace & Defense") reveals the
overlap. Both fields are used.

An overlap is **context, not an accusation** — buying a stock your committee
oversees is legal and often unremarkable.

## Trade of the House — the public site

`web/site_app.py` serves four pages: an overview with the equity curve and the
latest tracked buys and sells, every recorded trade with filters, the tracked
politicians and the committees that define their remit, and the method.

**One filter defines what appears:** the traded company's industry falls under a
committee the member sits on. Nobody is pre-selected.

### The filter that was removed, and why

There used to be a second filter — a member contributed only once their own
settled in-remit trades averaged positive alpha — and `src/selection.py` was
written to test it against twenty other candidate rules on identical data. It
does not work.

Tested like for like (restricted to members who already had at least five
settled in-remit decisions, so both sides are the same people over the same
period, and matched within entry quarter so neither side is credited with the
calendar), trades that **passed** the gate returned **−1.16%** against those
that failed it, at **t = −1.34**, with 47% of quarters favouring the gate. A
coin flip. The raw unmatched gap looked far worse (−8.60%, t = −3.07) but that
was mostly timing, and saying so matters more than the bigger number.

For that coin flip the gate discarded three quarters of the evidence: 2,286
in-remit decisions became 532, and 45 live members became 11.

Its apparent value was position sizing, not selection:

| Rule | Return | S&P 500 | CAGR | Max drawdown | Trades |
|---|---:|---:|---:|---:|---:|
| **Every in-remit buy** (headline, equal weight) | **3.30x** | 2.62x | **+19.8%** | −32.9% | 3,817 |
| Every in-remit buy, size-weighted | 4.01x | 2.62x | +23.4% | −29.5% | 3,817 |
| Retired: positive-record members only | 2.13x | 2.62x | +12.1% | −28.0% | 725 |

The retired rule **lost to the S&P 500** once the size tilt was removed. All
three curves are pinned to the same window; without that the gated curve would
start a year later, because a member could not qualify until five of their
decisions had settled.

Removing a member filter also removes the thing that made point-in-time testing
necessary. The current rule uses a committee roster and a sector map, neither of
which is an outcome, so there is no selection bias left to correct for.

### What the committee filter is worth, honestly

In-remit buys average **+6.93%** twelve-month alpha per decision at
**t = +5.03** over 2,844 decisions. The sign holds in both halves of a split
sample, and unusually evenly: **+6.00% training edge, +6.27% test edge** over an
unfiltered baseline. It survives dropping any single member.

**Committee membership is read at the date of each trade**, not as it stands
today. `congress-legislators` publishes only a current file, but the source
repository's git history is a historical one — the current file at any past
commit is that date's membership — so quarterly snapshots back to 2019 are
reconstructed from it (`python src/cli.py committee-history`). Correcting this
*strengthened* the signal rather than deflating it: it was +5.25% at t = +3.25
under the current-roster reading, and the split-sample edge decayed from +6.91%
to +2.78% instead of holding.

One limit is stated rather than hidden: matched within the same sector **and**
quarter, the edge falls to **+4.54% at t = +1.54**. A good part of it is the
sectors committees happen to cover.

The grade is **not** used as a filter, and testing says it must not be: A-graded
trades returned **−5.93%** (t = −1.94) and were negative in both halves of the
split sample. It describes trade shape on the trade pages, nothing more.

**One decision counts once.** A member who buys the same stock on the same day
across several accounts files several lines; the statistics treat those as one
judgement, keyed on `(member, ticker, trade date)`. Position *sizes* are still
summed across the lines, because the duplication is real money; it is just not
real repetition.

**A warning from the comparison.** The rank correlation between a rule's
training-window edge and its test-window edge, across all 21 rules, was
**−0.378**. In-sample ranking *anti*-predicts. The rule shipped here was chosen
by which rules held their sign across both halves, never by which posted the
biggest backtest number.

```bash
python src/cli.py selection   # reproduce the whole 21-rule comparison
```

```bash
python src/cli.py strategy    # rebuild the cache after any ingest
python src/cli.py site        # http://127.0.0.1:8080
```

Port 8080 rather than 8000: Windows reserves 8000 on some machines — check with
`netsh interface ipv4 show excludedportrange protocol=tcp`.

## Trade grades (A–E)

`src/grading.py` scores how much a trade resembles a **deliberate, informed
position** rather than routine portfolio maintenance. It does not score legality,
and it deliberately excludes the outcome — a grade describes the decision, not
whether it paid.

| Signal | Weight | Why |
|---|---:|---|
| Inside committee remit | 32 | The only committee link with measurable predictive value |
| Isolation | 22 | A lone trade is a decision; one line of sixty-six is administration |
| Obscurity | 10 | Nobody needs inside information to buy a mega-cap |
| New position | 8 | A name never held before, vs the ninth purchase of the same one |
| Member's own account | 8 | Not a spouse's or dependent child's |
| Committee chair / ranking | 8 | Briefed first, sets the agenda |
| Unusual size *for them* | 6 | Against that member's own median, capped at 3× |
| Position size | 3 | Absolute dollars — deliberately small, see below |
| Option | 3 | A directional bet with a deadline |

The weights must sum to 100 and a module-level assertion enforces it. Adding the
three new components without rebalancing the old ones pushed the total to 115,
which inflated every score and nearly doubled the A grades before a test caught
it.

**Absolute size was cut from 15 to 3** after measuring it: trades larger than
three times a member's own median averaged **−5.92%** alpha. Rewarding raw
dollars was scoring the wrong thing, so `size_vs_self` replaces it against the
right baseline — and is capped, treating an outsized trade as *unusual* rather
than *better*.

**The three conviction components exist for one comparison.** Salazar's first
purchase of Voyager Technologies — a $2.5bn space company, filed alone, in
defence, which her seat on Foreign Affairs covers — grades **A (86.5)**.
Gottheimer's ninth purchase of ServiceNow, a $131bn name every index fund owns,
grades **B (64.1)**.

The two trades score *identically* on remit (32/32) and isolation (22/22). The
whole 22-point gap is obscurity (8.5 vs 2.1) and never having held the name
before (8 vs 0) — exactly what those components were added to catch. Market caps
come from `python src/cli.py fundamentals`; where none exists the component takes
the median and is flagged *estimated* on the trade page.

### The grade does not predict returns, and the site says so

`src/validate.py` tests it on every run and `/methodology` publishes the table.
Across 11,007 graded, settled decisions: A-graded trades averaged **−1.02%**
alpha against C-graded trades' **+4.87%**; A+B minus D+E came to **−2.20pp at
t = −1.71**, and the rank correlation between score and alpha was **−0.016**.

Adding the conviction components made the grade a sharper *description* of a
trade without making it any better at prediction. Both facts are published,
because a letter grade sitting next to a trade will otherwise be read as a
forecast it cannot make.

**Isolation does most of the work.** Filing 20034906 is the clearest case in the
dataset: 66 transactions disclosed at once, alphabetical, on four shared dates,
almost all in the $1,001–$15,000 band. That is an adviser rebalancing a managed
account — nobody forms 66 convictions on a Tuesday. In that same filing, the
SpaceX purchase sits alone on its date and inside Armed Services jurisdiction,
and grades **A (79)** while the 17-line 30 June block reaches only B.

Distribution across 35,709 graded trades (both chambers): A 548 · B 3,128 ·
C 4,750 · D 10,652 · E 16,631. Most congressional trading is exactly what it
looks like — managed money on autopilot, and an A is now genuinely rare at 1.5%.

```bash
python src/cli.py grade
```

A high grade is **not an allegation**. Every input is public, and buying into an
industry you work on daily is ordinary expertise.

## Company data and the options illustration

`src/market.py` pulls live fundamentals (market cap, analyst target and range,
P/E, beta, business summary, employees) and caches them in SQLite for 24 hours,
so a page load waits on the network at most once per ticker per day.

It also builds an **options illustration**: given a ticker and a direction, the
nearest listed expiry to the strategy's twelve-month horizon, struck near the
money, with bid/ask, IV, open interest, break-even and the move required.

This is deliberately built to be honest rather than flattering:

- **Chains are live-only.** Yahoo publishes no historical chains, so a contract
  shown against a 2026 filing is priced *today*. The move may already have
  happened. The page says so.
- **The contract is boring by construction** — nearest expiry, nearest strike.
  It is not screened for maximum payoff.
- **`risk_band()` never returns "low".** The rungs are High → Very high →
  Severe → Extreme, because a long option can lose 100% of its premium and any
  scale implying otherwise would be lying. Score inputs: move required to break
  even, time to expiry, implied volatility, open interest, bid/ask spread.
- **Maximum loss is stated in dollars**, not buried.

The risk score earns its keep. A CDRE call surfaces as **Severe** on 1 contract
of open interest and a 196% bid/ask spread — a trap a naive "suggested contract"
would have hidden behind a plausible-looking mid price.

It is an illustration of how a view could be expressed, not a recommendation,
and the page says that too.

## Detail pages

- `/trade/<id>` — the grade with its component breakdown, entry price vs latest
  with a marked price chart, 52-week range, **company profile** (market cap,
  1-year analyst target and range, P/E, beta, employees, business summary), the
  **options illustration** with its risk band, the outcome at 1/3/6/12 months,
  the rest of the filing it came from, and every other congressional trade in
  that ticker.
- `/politician/<bioguide>` — a **trading profile** (transactions, buys vs sells,
  distinct securities, estimated volume, family-account share, active range,
  busiest single day), **what they buy** by sector with in-remit marked,
  in-remit vs out-of-remit alpha, committees and the jurisdiction they confer,
  grade distribution, largest positions, and every disclosed trade.

The trades table filters on side, member, **grade, committee sector, account
(own vs spouse/child), minimum position size, date**, and ticker, and sorts by
date, size or grade.

## Running unattended on a VPS

`scripts/daily_scan.py` does one full cycle: check the Clerk's index for filings
this database has never seen, ingest and parse them, refresh prices and sectors,
rescore, rebuild the strategy cache, and report any new trades by a tracked
member.

```bash
python scripts/daily_scan.py --dry-run                  # look, change nothing
python scripts/daily_scan.py                            # the real thing
python scripts/daily_scan.py --notify-webhook https://… # Slack/Discord
```

Exit codes are meaningful so a wrapper can branch on them: **0** nothing new,
**10** new tracked trades found, **1** error. The systemd unit lists 10 as a
success so the timer stays healthy.

`scripts/deploy/` has the systemd service and timer, a gunicorn unit for the
site, and `scripts/deploy/README.md` with the full VPS walkthrough — user setup,
first build, TLS via Caddy or nginx, and backups.

The scan seeds its baseline on first run, so it never alerts on the existing
backlog.

## Alerts

`python src/cli.py alerts` reports purchases from filings ingested since the
last alert run, ranked by the buyer's backtested edge (shrunk toward zero when
the sample is small), their committee access, and position size. `GET
/api/alerts.json` returns the same data as JSON for wiring into a notifier.

To run it on a schedule, re-run `ingest` (only new filings are fetched) and then
`alerts`.

## Known limitations

- **Amounts are ranges**, never exact figures. Position sizes use the geometric
  mean of the disclosed range, so every dollar total is an estimate.
### Committee coverage

All three chambers are loaded from `congress-legislators`, because that dataset
is not House-only. Senate filings are now ingested too — see below.

**The number that matters is 49** — the full committees, which are what carry
jurisdiction:

| Chamber | Full committees | (of which) | Subcommittees |
|---|---:|---|---:|
| House | 23 | 20 standing + 3 select | 109 |
| Senate | 21 | 16 standing + 4 select/special + 1 caucus | 72 |
| Joint | 5 | — | 0 |
| **Total** | **49** | | **181** |

All 49 carry an explicit sensitivity weight; none fall back to the default.

**The weight does not predict returns.** The site's `/committees` page ranks every
full committee by what trading inside its own jurisdiction actually returned, and
correlates that against the hand-assigned weight: **r = +0.13** across the 24
committees with enough data. Senate Commerce (0.80) returned +21.5%, Senate HELP
(0.75) returned −19.2%, and House Financial Services carries the maximum 1.00 and
returned +1.8%. The weight is a judgement about *access* and is stated on the page
as not being a forecast.

Attribution there is deliberately many-to-many: a member sits on several
committees, so one Lockheed purchase counts under both Armed Services and
Appropriations. The columns do not sum to the site total, because there is no
non-arbitrary way to split one trade between two seats.

**The 181 subcommittees are inherited detail, not extra jurisdiction.** Measured
on the live data:

- A subcommittee's sectors are copied from its parent, so it adds no new
  industry.
- **0 of 2,552 subcommittee seats belong to someone who is not also on the
  parent committee** — that is how Congress works, and it is confirmed here
  rather than assumed.
- Consequently **0 of 531 seated members would lose a single remit sector** if
  every subcommittee were deleted.

So the remit test — the site's actual signal — runs entirely on the 49. The
subcommittees are kept because they are genuine assignments worth showing on a
member's page, and they feed a small breadth bonus in the access score
(mean +0.038, max +0.080). That bonus is the one place they change anything, and
it is arguably measuring workload rather than access, since it confers no new
jurisdiction.

The Senate's committees reach 16 jurisdiction sectors against the House's 14 —
adding `pharma` (HELP) and `utilities` (Energy & Natural Resources) — so Senate
filings widen the remit test as well as enlarging the population.

### Senate ingest

```bash
pip install curl_cffi               # REQUIRED - see below
python src/cli.py senate-check      # probes all four steps of the portal flow
python src/cli.py senate-ingest     # 964 reports, roughly 20 minutes
```

**The portal fingerprints TLS, and `requests` fails that fingerprint.**
efdsearch.senate.gov returns 403 to plain `requests` while Chrome loads the same
page from the same machine in the same second. `curl_cffi` replays Chrome's
ClientHello and is served normally. This repo **previously misdiagnosed that 403
as a network-level block on `senate.gov`** — it never was one. The giveaway was a
browser succeeding where Python failed on one host; check that pair before
blaming egress.

Ingested: **964 PTRs since 2020** (833 electronic, 131 scanned paper), **8,436
transactions, 0 parse errors**, of which 5,373 match a sitting senator. The other
3,063 belong to 13 filers who have all since left the Senate, and the tracker
only ranks sitting members.

Senate filings land in the same `filings` / `transactions` tables as the House,
with a `sen:` doc-id prefix so the chambers never collide and any query can
separate them. Amounts, owners and asset types are normalised to the House
vocabulary on the way in, so pricing, scoring, grading and the remit test treat
both chambers identically. Senators are matched on surname within the Senate,
since they have no district — `resolve_senator()` never crosses chambers, so a
Representative of the same name cannot be picked up by mistake.

The command is idempotent and resumable: re-running skips filings already
stored. If the portal refuses the session it prints why and exits cleanly
without touching the database. `daily_scan.py` attempts the Senate on every run
and logs-and-continues when it is blocked (`--skip-senate` to disable) — **the
VPS needs `curl_cffi` installed** or that half of the scan will 403.

- **The Senate is in, but it barely moves the curve.** Senators are 6 of the 29
  tracked members, 4.8% of qualified trades, and **0.22% of the size-weighted
  book** — House positions are two orders of magnitude larger. Adding the Senate
  changed who is listed, not what the strategy returns. 131 of 964 Senate
  reports are also scanned paper with no text layer.
- **Filer errors pass through.** The parser reports what the document says, and
  the documents contain mistakes: one filing lists two identical `SPYB` rows
  where the filer's own description says SPY and SPYD, and four carry mistyped
  trade dates (`3031-04-30` for 2021, `2220-04-07` for 2022). Impossible dates
  are excluded from the member-entry figure and from the disclosure-lag average
  — a filing cannot predate its own trade, and one such row previously gave Pete
  Sessions a mean lag of −3,741 days — but they are left in the data; the source
  PDF is linked on every row so any figure can be checked.
- **Amounts are occasionally exact, not ranged.** A few filers type a precise
  figure, and spouse/dependent-child holdings top out at a single open-ended
  "Over $1,000,000" category. Both are handled, but they make the estimated
  volume figures rougher still.
- **Roughly one purchase in ten has no public ticker** — Treasuries, private
  credit funds, non-listed partnerships. These are stored but not scored.
  Options are excluded from scoring because buying the underlying is a
  materially different trade.
- **Only sitting members are ranked.** Trades by members who have since left
  Congress remain in the database but are excluded from the leaderboard, since
  you cannot copy someone who no longer files.
- **No costs.** Commissions, taxes, spread and slippage are not modelled, and
  the curve assumes a fill at the open.

## Layout

```
src/ptr_parser.py   PDF → transactions (coordinate-based table parser)
src/house.py        Clerk index + PTR download, cached
src/congress.py     roster, committees, access scoring
src/matching.py     filer name + district → bioguide id
src/ingest.py       orchestration into SQLite
src/prices.py       yfinance fetch + cache + as-of lookups
src/sectors.py      ticker -> industry -> committee-jurisdiction overlap
src/backtest.py     trade scoring, member ranking, equity curves
src/alerts.py       new-buy detection and prioritisation
src/committees.py   per-committee stats: whose jurisdiction produced the alpha
src/validate.py     does the grade predict returns? (it does not, and it says so)
src/senate.py       Senate EFD adapter (needs curl_cffi; verified live)
src/senate_ingest.py Senate rows → the House vocabulary and tables
src/strategy.py     in-remit filter, equal-weighted headline curve
src/selection.py    head-to-head comparison of 21 candidate selection rules
src/db.py           schema
web/app.py          research dashboard (everything, unfiltered)
web/site_app.py     Trade of the House — the public site
site_wsgi.py        gunicorn entry point for the site
scripts/daily_scan.py        unattended daily filing scan
scripts/deploy/              systemd units + VPS walkthrough
web/static/_charttest.html   renders the charts on synthetic data, no DB needed
refresh.py          one-shot scheduled update
report.py           generates a self-contained findings report (report.html)
tests/test_parser.py     parser regression, against real filings
tests/test_backtest.py   scoring/ranking/curve, on a synthetic database
tests/test_senate.py     Senate report parsing, offline
tests/test_senate_ingest.py  Senate → House vocabulary mapping, offline
tests/test_strategy.py   decision counting + point-in-time gate, offline
tests/test_committees.py per-committee stats, on a synthetic database
tests/test_grading.py    conviction components + weights sum to 100, synthetic DB
```

Each test file runs standalone:

```bash
python tests/test_parser.py     # fetches 5 real filings once, then caches them
python tests/test_backtest.py   # no network
python tests/test_senate.py     # no network
```

## Disclaimer

Research tool, not investment advice. Disclosure data is self-reported, published
in ranges, and weeks stale by construction. Nothing here asserts that any member
has broken the law.
