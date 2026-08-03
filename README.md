# wxalpha

A Polymarket weather trading bot with eight independent strategies and a live
dashboard. Runs as a single Python process on Railway's free tier.

It starts in **paper mode**. Nothing touches real money until you set
`LIVE_TRADING=true` and supply keys.

---

## Read this first: what this build is for

This is a **measurement build**, version 0.16.0. It exists to run unattended for
10–15 days and produce data you can trust, not to make money in the meantime.

A three-day run of the previous version reported **+70.6%**. The real figure was
**−25.8%**. The bot was not trading badly; it was *scoring itself* wrong. Two
independent faults:

1. Open-Meteo publishes its daily maximum hours before Polymarket resolves. The
   old settlement path read that number, wrote `closed=1` with its own winner,
   and the authoritative Gamma verdict then had nothing left to correct. **38 of
   180 settlements were booked against the wrong winner.**
2. The forecast gridpoint is not the station Polymarket resolves on. Houston,
   Tokyo, Moscow and Seoul missed the winning bucket on *every* observed day.
   All `BIAS_*` values are zero, meaning nobody has measured that gap yet.

Plus an amplifier: S6 treated a gridpoint reading as a hard floor and declared
live buckets worthless. 12 of its 16 "impossible bucket" trades named a
temperature above the day's actual high. Two of them accounted for ~\$295 of the
~\$565 of phantom profit.

So this version changes **only what makes the run measurable, and nothing that
makes it profitable.** Settlement is now Gamma-only. Every strategy threshold,
kernel constant and bias value is untouched, deliberately: tuning them from
three days of data would fit the bot to noise and destroy the out-of-sample
value of the run that is supposed to measure them. That work happens on day 15,
against the data this run produces.

---

## What it actually does

Every 45 seconds:

1. **Discover** open weather events on Gamma, parse each bucket
   (`84-85°F` → `[83.5, 85.5)`) and group buckets by event.
2. **Forecast** — pull ensemble members and five deterministic models from
   Open-Meteo (free, no key) for each city in play.
3. **Price** — build a kernel density over ensemble members, integrate over each
   bucket, normalise across the event.
4. **Evaluate** — run the enabled strategies against the current order book.
5. **Risk-check** — cash, position caps, tail floor, edge sanity cap, daily loss
   limit.
6. **Execute** — paper fill against the book, or a real CLOB order.
7. **Mark and persist** — equity curve, every signal including the skipped ones,
   and a full-resolution price track for every market currently held.

## How a market settles

This is the part that was wrong, so it is worth stating exactly. In order, every
tick:

1. `discover()` refreshes the live set.
2. `refresh_closures()` asks Gamma which markets have closed and reads
   `outcomePrices`. A winner is recorded **only** if those have converged past
   `SETTLE_MIN_CONVICTION` (0.95). Anything less decisive is left open and asked
   again next tick. The row is stamped `winner_src='gamma'`.
3. `record_observed_highs()` fetches the temperature actually recorded and
   stores it in `settled_highs`. **It settles nothing.** It is evidence for the
   per-city bias measurement, and that is all it is now allowed to be.
4. `settle()` books P&L, and refuses any row whose `winner_src` is not `gamma`.
5. `record_resolutions()` writes one durable row per resolved market: the
   winning bucket, the forecast at the time, the book's mid, the kernel width
   and the recorded high, captured together so none of it has to be
   reconstructed later from ticks that may already have been rolled up.
6. `sweep_stale()` retires markets Polymarket never resolved after
   `STALE_MARKET_H`. No winner is invented and nothing is booked; the market
   just stops being priced, and any position stranded in it is logged rather
   than valued at its last mid forever.

`/api/health/settlement` shows the breakdown by source. Anything outside
`gamma` is not a real result, and now says so.

---

## The strategies

Each keeps its own bankroll, positions and P&L, so the tabs are genuinely
independent experiments rather than several views of one book.

| | Depends on | Fires when |
|---|---|---|
| **S1 Edge** | forecast accuracy | model probability beats the ask by the edge threshold; fractional Kelly sizing |
| **S2 Consensus** | model *agreement* | ensemble spread AND cross-model spread both tight → concentrate into the modal bucket |
| **S3 Dutch** | nothing at all | bucket asks sum below \$1 → buy the whole set, which pays \$1 whatever the weather does |
| **S4 Drift** | forecast *revisions* | inside the closing window, trades the direction of the model update before the book reprices |
| **S5 Quote** | neither | rests two-sided quotes inside the reward window; never crosses, so pays no taker fee |
| **S6 Observed** | today's *measured* high | **OFF by default.** Buckets below the recorded high supposedly cannot resolve yes |
| **S7 Fade** | the longshot bias | outer buckets bidding well above model fair get sold |

**S6 ships disabled, and it earned that.** The argument for it was the best in
the bot: once 85F is on the board, a bucket topping out below it is worth
exactly zero — not unlikely, impossible — and thin books are slow to mark it.
The argument fails on the premise. `fetch_observed` reads the hourly analysis
endpoint, which disagrees with the daily-maximum endpoint by 1–3 °F, so the
bot's own two sources for "what already happened" contradict each other. Both
are gridpoint values, while Polymarket resolves on a station that ran up to
2.8 °F away in the measured cities. That is not a floor; it is a forecast with
unwarranted confidence attached, and it was used to declare live buckets
worthless. Moscow was recorded as having already reached 19 °C at midnight local
time.

If you turn it on anyway, it now needs the floor to clear the bucket top by
`hard_margin_f` (2.5 °F, wider than the largest measured station gap) **and** the
city to have a measured `BIAS_*`. During the observation run no city has one, so
it will produce nothing until you have earned the right to enable it.

S7 is the other one to be suspicious of. Selling wings is short volatility: it
wins small and often, then gives it back on the day that surprises everyone. The
`max_open_fades` cap matters more than its win rate, because every short tail
loses together.

**If you only keep two, keep S3 and S4.** S3 is structural — it survives your
model being wrong and keeps working when the weather API is down. S4 has the
strongest information edge of the forecast-based ones. S1 is the honest
benchmark to measure the rest against: if a strategy can't beat plain
forecast-versus-price, it isn't earning its complexity.

---

## Where to start

**Read `GUIDE.md`.** It is the complete deploy-and-run process, browser only,
no git or Python needed. This file is the technical reference behind it.

## Deploy to Railway

```bash
git init && git add -A && git commit -m "wxalpha"
# push to GitHub, then in Railway: New Project -> Deploy from GitHub repo
```

Railway autodetects Python and uses `railway.json`. Then:

**1. Add a volume, mounted at `/data`.** Railway → your service → *Volumes* →
mount path `/data`. **This is not optional and the bot will refuse to start
without it.**

That refusal is deliberate. Without a volume, `/data` is ordinary container
filesystem: `makedirs` succeeds, SQLite opens, every write lands, nothing
anywhere reports a problem — and the entire database is discarded by the next
deploy. For an unattended two-week collection run that is the worst failure mode
there is, because you only find out at the end, when the data you were gathering
is gone. `db._durable()` checks whether the DB directory is on a different
device from `/`; if it is not, startup aborts with the fix printed in the log.
`REQUIRE_DURABLE_DB=false` overrides it, and should only ever be set locally.

**2. One replica.** The store is SQLite on a single volume. Two replicas means
two processes writing the same file and both trading the same book.
`railway.json` pins `numReplicas: 1`; leave it.

**3. Pick a region Polymarket serves.** Polymarket geoblocks some
jurisdictions. Run `python check_apis.py --gamma-only` from the deployed
container's shell before starting the run — a `BLOCKED` result on day one is
cheap, and on day ten it is a lost run.

**4. Set variables** (copy from `.env.example`):

```
DB_PATH=/data/wxalpha.db
REQUIRE_DURABLE_DB=true
LIVE_TRADING=false
START_BANKROLL=800
USER_AGENT=wxalpha/1.0 (your@email.com)
```

Leave every `BIAS_*` at zero. Measuring them is what the run is for.

**5. Open the URL.** The dashboard is at `/`, health check at `/health`.

`/health` returns **503** once the trading loop has not ticked for
`HEALTH_MAX_TICK_AGE_S` (default 15 minutes), so Railway restarts the container.
The old version returned ok unconditionally, which meant an asyncio loop wedged
on a socket that never times out looked perfectly healthy while the bot
collected nothing. The same value doubles as a startup grace period, so a slow
first tick cannot put the deploy into a restart loop. `restartPolicyType` is
`ALWAYS` with no retry cap — a run that dies on day nine because it had already
used its ten restarts is not a run.

**4. Watch the feeds.** `/api/health/feeds` answers "is this actually working"
in one call: latest discovery line, book sources, forecast status, recent errors,
and a plain `verdict` field. Logs also go to stdout now, so **Railway's own log
viewer works** — previously they only landed in SQLite and the log tab looked
empty. Filter with `/api/logs?q=gamma` or `/api/logs?level=error`.

**5. Check the first scan.** Open the **market scan** panel on any tab, or hit
`/api/discovery`. It lists every weather event found, how many buckets it
parsed, and — importantly — every event it *rejected* and why. If accepted is
zero while skipped is not, the parser is out of date rather than the market
being quiet. That distinction is the first thing to establish and it takes one
tick to see.

### Bankroll

`START_BANKROLL` is split evenly across the eight strategies, so `800` gives each
one \$100. Set it **before** the first tick — `alloc()` reads it live, so
changing it mid-run shifts every equity curve's baseline. If you change it
later, hit `Reset book` on each tab. S6's sleeve is allocated whether or not it
is enabled, so with it off the effective working capital is \$700.

Default position caps are sized for a \$100 book: \$12 per market on Edge,
Consensus, Drift, Observed and Fade, \$40 per set on Dutch, \$20 per market and
\$15 inventory on Quote. These are **total exposure caps, not per-fill limits** — repeat fills
accumulate toward them.

### Keeping it small on a free-tier volume

Three days of the previous version produced 179 MB, which extrapolates to about
900 MB over fifteen. The rule applied to get that down is: **prune only what is
reconstructible or was never read.**

What ages out is the raw order-book tick for the ~900 markets we merely watch —
the overwhelming bulk of the file. Those are averaged into `quotes_hourly`
before deletion and nothing in the analysis reads them individually, so the
window is 6 hours (`QUOTE_RETENTION_H`). Untaken signals go after 3 days.

What is never pruned at any age: `trades`, `positions`, `settlements`,
`resolutions`, `settled_highs`, `mark_ticks`, `quotes_hourly`, `discovery`.
`forecasts` are kept 45 days, which must outlast the run — they are the raw
material for the per-city bias measurement, and a collection run that quietly
deleted its own findings on day eight would be worse than one that never
started.

Two things make the difference beyond retention. `maintain()` runs `VACUUM` and
a WAL truncate every 12 hours, because SQLite does not return deleted pages to
the filesystem on its own — the tables shrink while the file keeps growing, and
a full volume stops every write mid-run. And identical forecast rows are
skipped: Open-Meteo republishes an ensemble every few hours, so most refreshes
wrote a byte-identical ~120-member row for nothing.

Held markets get their own table, `mark_ticks`, at full resolution and kept for
the whole run. That is roughly sixty markets every 5 minutes instead of nine
hundred every 45 seconds, and it is what makes `/api/analysis/cashout`
answerable: an hourly mean cannot tell you what bid was available at 14:20, so
it cannot tell you whether selling early would have beaten holding.

`Download data` gives you **everything** — every table as CSV plus a raw copy of
the `.db` file, in one zip, **stored uncompressed** so it opens straight into
Excel or pandas with no unpacking step. Set `EXPORT_COMPRESS=true` if you are
pulling it over a slow link; the CSVs deflate roughly tenfold.

---

## Fees are the main adversary

Polymarket charges a taker fee on weather markets. The live `feeSchedule` reads
`rate 0.05, exponent 1, takerOnly`, which works out to:

```
fee per share = 0.05 * min(price, 1 - price)
```

At a 28c price that is **1.4c a share** — 5% of notional, and comparable to the
entire edge most of these strategies look for. At 50c it is 2.5c. Every strategy
now compares its edge *net of this fee*, the dutch book prices it per leg, and
the quoter refuses to quote inside `fee_multiple` times it. A gross 2-point edge
at 50c is a losing trade and the bot will decline it.

If you take one number from this document: **a strategy that looks marginally
profitable gross is losing money net.** Set `FEE_RATE=0` only if you have
confirmed the schedule changed.

## Calibration — read this before going live

The whole edge rests on three numbers in `app/model.py`:

```
KERNEL_BASE     0.65   station uncertainty at zero lead time (°F)
KERNEL_PER_DAY  0.30   how fast error grows with lead time
SPREAD_WEIGHT   0.55   how much to trust the ensemble's own spread
```

These are defensible defaults, not calibrated ones. Too narrow and the bot
overtrades on imaginary edge; too wide and it starts buying tails that never
come in. Calibrating them is the *output* of the observation run, not an input
to it.

### The day-15 checklist

Everything below reads the `resolutions` table, which now captures the winning
bucket, the forecast, the book's mid and the kernel width together at the moment
the verdict landed — so none of it has to be reconstructed by hand.

1. **Per-city bias.** Median of (winning bucket midpoint − `fc_mean_f`) at a
   fixed lead, n ≈ 15 per city. That offset is the gridpoint-to-station gap;
   put it in `BIAS_NYC`, `BIAS_LON` and so on. Polymarket resolves on one
   specific station and the forecast gridpoint is not it. **This is usually
   worth more than any strategy tweak.**
2. **Per-strategy P&L on `winner_src='gamma'` rows only**, gross and net of
   fees. `/api/health/settlement` will tell you if anything else crept in.
3. **Calibration.** Bucket the predicted probabilities (0–10%, 10–20%, …) and
   compare each bucket's predicted rate to its actual hit rate. If the 30%
   bucket hits 45% of the time the kernel is too wide — lower `KERNEL_BASE`.
   Cross-check against realised z-scores versus `kernel_sd`.
4. **Cash-out.** `/api/analysis/cashout?take=0.85&stop=0.15` replays every
   settled position over its own price track. Read `perfect_timing_gain` first:
   it is a hindsight upper bound nobody could achieve, so if *it* is small there
   is no cash-out edge to chase and the honest answer is to stop looking.
5. Then apply the biases and the calibration, and run a **second, shorter
   window** to confirm the fix helped — before any live capital.

**S3 Dutch is the only strategy whose profitability does not depend on getting
any of this right.**

### Sample size, honestly

Fifteen days gives roughly 900 settlements, which sounds like plenty and is not.
The buckets within a single city-day are one bet, not eight: they resolve
together off one temperature. The effective sample is about **16 cities × 15 days
≈ 240 independent observations** — thin for detecting an edge of a few cents,
and thinner still once you split it per strategy. Treat any single strategy's
fifteen-day P&L as a hypothesis, not a result.

---

## Going live

1. Fund a Polymarket account and get your private key.
2. Set `LIVE_TRADING=true`, `POLYMARKET_PRIVATE_KEY`, `POLYMARKET_FUNDER`.
   `POLYMARKET_SIG_TYPE=1` for an email/Magic proxy wallet, `0` for an EOA.
3. Turn **one** strategy on, with `max_position_usd` set to something you'd
   shrug at. Watch it for a day.

`Reset book` is disabled while live trading is on. The daily loss limit halts
everything at −12% of the day's opening equity; `Resume` rebases it.

Polymarket restricts access from some jurisdictions and its terms prohibit
automated trading in some circumstances — worth checking where you stand before
you fund an account.

---

## Dashboard

Five channel tabs, one per strategy. Each shows:

- KPI strip — equity, P&L, return, realised, open risk, win rate
- Equity curve
- Live P&L — equity, total, **today**, and unrealised all repaint in place every
  six seconds without redrawing the tab, and flash on change
- Live-editable parameters, saved to the database and picked up on the next tick
- Open positions, recent fills
- **Evaluations** — including the trades it passed on, with the reason. If a
  strategy is silent, this is the tab that tells you why.
- Settled markets

Above the tabs, the **isotherm ribbon** shows the next event to resolve: model
probability as filled bands along a cold-to-hot ramp, market mid as a dashed
hairline over it. The gap between the two is the trade.

Controls in the masthead: `Halt` stops all trading immediately, `Download data`
exports everything.

---

## Local run

```bash
pip install -r requirements.txt
DB_PATH=./wxalpha.db uvicorn app.main:app --reload
```

```bash
python3 selftest.py     # offline: parsing, pricing, every strategy, settlement
                        # accounting, maker-fill gates, risk caps, retention,
                        # export. Must print ALL CHECKS PASSED.
```

There is no synthetic-data mode. `app/demo.py` was removed in 0.16.0 along with
`run.sh`'s silent fallback to it, which would have switched a multi-week
collection run onto invented numbers on a single bad DNS lookup at boot. If the
API is unreachable the launcher now says so and starts anyway, finding nothing
until the network allows it.

---

## Honest limits

- **Nobody can promise this is profitable.** The forecast edge is real but thin,
  and it is entirely dependent on calibration you have to do yourself against
  your own settled markets.
- Paper fills assume you cross the spread and get filled at the touch plus
  slippage. Real fills on thin weather books are worse.
- **The live order path has never executed a single order.** `py-clob-client` is
  commented out of `requirements.txt` and every trade in the database to date is
  `mode=paper`. Going live is separate work, not a config flag you flip at the
  end of the run.
- Discovery, pricing and settlement *have* run against live Gamma and Open-Meteo
  for three days. That run is what produced the settlement bug, so the field
  names and endpoint shapes are no longer guesses — but three days is all the
  exposure they have had. Check `/api/discovery` and the market-scan panel in the
  first ten minutes of any new deploy.
- In-memory state does not survive a restart: the forecast cache, the observation
  cache and the resolved-high cache all rebuild from scratch, and `STATE` counters
  reset to zero. Nothing is lost that is not in the database, but a restart shows
  up as a discontinuity in the tick counter and a burst of API calls.
- Discovery tries three paths, in order: **direct slug lookup** (deterministic,
  needs no search/tags/pagination), a tag sweep, then a volume-ordered sweep of
  active events. If Polymarket renames the event-slug pattern, set
  `SLUG_TEMPLATES` — the error log prints the slug it tried so you can diff it
  against a live `polymarket.com/event/<slug>` URL.
- Gamma caps a page at **100** regardless of the `limit` you send, and does not
  support sorting by `endDate`. Both cost me a release.
- Discovery needs a title containing a configured city and either a date or a
  usable close time. Anything it rejects is recorded in the `discovery` table
  with a reason — nothing is skipped silently, but a format it does not
  recognise is still a market it will not trade.
- Timing runs off local settlement midnight, not Gamma's `endDate`. Gamma
  reports these events ending at noon UTC on the observation day, which is
  *before* the afternoon peak; timing off it made the late strategies quit hours
  early.
- Only eight cities are configured. Add more in `config.CITIES` with lat, lon,
  timezone and aliases. **The coordinates must be the resolving station**, not
  the city centre — Polymarket settles NYC on LaGuardia via Wunderground, and
  LGA vs Central Park can differ by 2-3F on a sea-breeze day.
- Daily **minimum** temperature markets are skipped on purpose. Their titles are
  nearly identical to the maximum ones, and pricing them off a max forecast
  would be confidently wrong rather than merely wrong. Supporting them needs a
  daily-min path through the forecast and the observed-floor strategy.
- S5 is a genuine maker: it rests limit orders, never crosses the spread, caps
  its half-spread at the market's `rewardsMaxSpread` so the rebate is not
  forfeited, and re-prices only when fair value drifts past `requote_ticks`.
  **Its paper fills are still optimistic, but by a bounded amount now.** The old
  simulation filled on any change in Gamma's `lastTradePrice` while the quote was
  at the touch: 378 fills in three days, all free, in markets trading a couple of
  shares an hour. Three gates now apply — the order must have rested
  `min_rest_s` (90s), Gamma's cumulative volume must have actually *risen* since
  it was posted, and the fill is capped at `volume_share` (25%) of that increase,
  so a print of eight shares cannot fill a resting sixty. It still assumes
  front-of-queue and that the volume traded against us rather than past us.
  If Gamma reports no volume figure at all the quote does not fill, and the log
  says so hourly; `require_volume=0` reverts to the old print-only test, which is
  how the fictional fills happened in the first place.
- Prices refresh from Gamma every tick (~3 calls for 220 markets), with the CLOB
  layered on for depth when reachable. `book sources` in the log tells you which
  is in use; `gamma-stale` means the per-tick refresh failed and you are trading
  off discovery-pass prices.

## Layout

```
app/config.py       environment configuration, city table
app/db.py           SQLite schema, retention rollup, export
app/weather.py      Open-Meteo ensemble + deterministic models
app/model.py        bucket parsing, kernel density pricing, Kelly
app/pmclient.py     Gamma discovery, CLOB books, paper + live execution
app/strategies.py   the strategies and their tunable parameters
app/engine.py       the loop: price, risk, execute, mark, settle
app/main.py         FastAPI routes
app/static/         dashboard
selftest.py         offline test harness
```
