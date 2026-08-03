# wxalpha — deploy and run

A Polymarket weather trading bot. Eight strategies, live dashboard, one Python
process on Railway.

**Build 0.16.1.** Starts in paper mode with $100 per strategy. Nothing touches
real money until you set `LIVE_TRADING=true` and add keys.

No git, no Python, no command line needed. Browser only.

**What this build is for.** It is set up for a 10–15 day unattended observation
run, not for making money yet. An earlier 3-day run reported +70.6% and the real
figure was −25.8% — the settlement path was scoring trades against the wrong
winner. Everything in 0.16.1 exists to make the next run *measurable*: nothing
was tuned to make it profitable, and every `BIAS_*` is deliberately left at zero.
See `README.md` for the full story, and *Part 4* here for the day-15 checklist.

To try it on your own PC first, see **`LOCAL.md`**. There is no synthetic-data
mode any more — if the API is unreachable it finds no markets and says so.

---

# Part 1 — Deploy

## Step 1. Put the code on GitHub

1. Right-click `wxalpha.zip` → **Extract All**
2. github.com → **New repository** → give it a name → **Create repository**
3. On the empty repo page click **uploading an existing file**
4. Open the extracted `wxalpha` folder. Select **everything inside it**
   (Ctrl+A) and drag it onto the GitHub page
5. **Commit changes**

Your repo must look like this:

```
app/                  <- this must be at the top level
requirements.txt
railway.json
Procfile
runtime.txt
README.md
```

**Not** a single `wxalpha` folder containing those. Dragging the folder instead
of its contents is the most common reason a deploy silently keeps serving old
code. If you did that, see *Root Directory* under Troubleshooting — there is a
one-setting fix, no need to redo the upload.

## Step 2. Create the Railway project

**New Project → Deploy from GitHub repo →** choose your repo.

Railway detects Python from `requirements.txt` and uses the start command in
`railway.json`. No Dockerfile, no configuration.

## Step 3. Add a volume — do not skip this

Service → **Settings → Volumes → Add volume**, mount path:

```
/data
```

Without it, `/data` is ordinary container filesystem. Every write succeeds,
nothing errors, and the whole database is destroyed by the next deploy — you
would reach day 14 of a 15-day run and find out then.

0.16.1 no longer lets that happen: with `REQUIRE_DURABLE_DB=true` the bot checks
at startup whether the database directory is a real mount and **refuses to
start** if it is not, printing:

```
====================================================================
STORAGE NOT DURABLE
...
====================================================================
```

If you see that, the volume is missing or mounted somewhere other than the
directory `DB_PATH` points at. The mount path and `DB_PATH`'s directory must be
the same — `/data` and `/data/wxalpha.db`.

## Step 4. Add variables

Service → **Variables** → add these five:

| Variable | Value |
|---|---|
| `DB_PATH` | `/data/wxalpha.db` |
| `REQUIRE_DURABLE_DB` | `true` |
| `START_BANKROLL` | `800` |
| `USER_AGENT` | `wxalpha/1.0 your@email.com` |
| `LIVE_TRADING` | `false` |

`START_BANKROLL` splits evenly across the eight strategies, so 800 gives each
one $100. Set it before the first tick — changing it later shifts every equity
curve's baseline.

Put a real address in `USER_AGENT`. Open-Meteo throttles anonymous traffic.

`.env.example` lists everything else with the reasoning attached. The defaults
are chosen for a free-tier volume and a two-week run; you should not need to
touch them. **Do not set any `BIAS_*`** — see Part 4.

## Step 5. One replica, always restart

Both are already in `railway.json`, but check them under **Settings** because a
change here silently corrupts the run:

- **Replicas: 1.** The database is SQLite on a single volume. Two replicas means
  two processes trading the same bankroll through the same file. Railway will
  happily scale it for you if asked; do not ask.
- **Restart policy: ALWAYS**, healthcheck `/health`, timeout 300s.

`/health` is a real watchdog in 0.16.1, not a liveness stub: it returns **503**
once the trading loop has not completed a tick for `HEALTH_MAX_TICK_AGE_S`
(default 900s), so Railway restarts a wedged asyncio loop. Previously a hung bot
reported itself healthy indefinitely — which for an unattended run means silently
collecting nothing.

Region: keep the service in a US or EU region. Both APIs are reachable from
those; run `python check_apis.py` locally if you want to confirm before deploying.

## Step 6. Generate a domain

Service → **Settings → Networking → Generate Domain**.

## Step 7. Verify — the step that actually matters

Open:

```
https://your-app.up.railway.app/version.txt
```

It must print:

```
wxalpha 0.16.1
strategies 8: s1 s2 s3 s4 s5 s6 s7 s8
cities 16
fee_rate 0.05
live False
db /data/wxalpha.db (4.2 mb)
tick 3 running True
```

Two lines to read carefully: `live` must be `False`, and `db` must be the path
under your volume mount. The size figure is the same number the retention logic
polices, so it is also your free-tier gauge.

**If this 404s or shows an older version, your code is not live.** Railway keeps
serving the last healthy deployment when a build fails, so a working site proves
nothing on its own. Open the **Deployments** tab, find the failed build, and read
its log.

Do this check after *every* deploy.

---

# Part 2 — Confirm it works

## Two URLs

```
/version.txt                 what is running
/api/health/feeds            whether the data is flowing
/api/health/settlement       whether the P&L can be trusted
```

The third one is new in 0.16.1 and is the one that matters for this run. It
groups every settlement by where its winner came from and gives a verdict. Any
P&L not sourced from `gamma` is unverified — that is exactly the failure that
turned −25.8% into a reported +70.6%. It also reports
`closed_awaiting_resolution`, `retired_unresolved` and `stranded_positions`, so
markets Polymarket never resolves are visible rather than invented.

`/api/health/feeds` returns a plain `verdict`:

| verdict | meaning |
|---|---|
| `ok` | markets found, prices flowing, forecasts priced |
| `no markets` | discovery found nothing — see the `gamma:` log line |
| `no quotes` | markets exist but no prices — both price paths failed |
| `quotes but no forecast prices` | Open-Meteo is unreachable |

## The Railway log

Within about 45 seconds of the first boot you should see:

```
wxalpha 0.16.1 starting, mode=paper, 8 strategies, db=/data/wxalpha.db
gamma: 20 by slug, 0 from tags, 0 more from sweep, 20 unique candidates
discovery: 20 events scanned, 20 usable, 220 buckets stored
forecast refresh: 8/8 cities, 8 with live observations
book sources: {'clob': 220}
```

What to read into each:

- **`gamma:`** — `by slug` is the reliable path. If it is 0 while the others are
  not, the event-slug pattern changed; the error log prints the slug it tried.
- **`discovery:`** — 8 cities x 11 buckets x ~2-3 days. Around 20 events.
- **`book sources:`** — `clob` is best, `gamma` is fine, **`gamma-stale` means
  the per-tick price refresh is failing** and the late strategies are unreliable
  until it is fixed.

Filter the log with `/api/logs?q=gamma` or `/api/logs?level=error`.

## The dashboard

Open the root URL. You should see the isotherm ribbon populated with the next
event to settle, and eight tabs each starting at $100.

- **Market scan** panel — every event discovery accepted or rejected, with the
  reason. If a market you expected is missing, the answer is here.
- **Evaluations** panel — every decision each strategy made, *including the ones
  it passed on and why*. A silent strategy is usually not broken. This is the
  panel to read.

---

# Part 3 — The eight strategies

| | Depends on | Trades markets closing in |
|---|---|---|
| **S1 Edge** | forecast accuracy | 2h – 96h |
| **S2 Consensus** | models agreeing with each other | 3h – 72h |
| **S3 Dutch** | nothing — pure order-book arithmetic | any |
| **S4 Drift** | forecast *revisions* | inside 14h only |
| **S5 Quote** | neither — earns the spread | 6h – 120h, pulls at 6h |
| **S6 Observed** | the high *already measured* today | same-day only |
| **S7 Fade** | the longshot bias | 1h – 72h |
| **S8 Shape** | nothing — the ladder must be unimodal | 1h – 96h |

Each keeps its own bankroll, positions and P&L, so the eight tabs are
independent experiments rather than seven views of one book.

**S6 is the strongest.** It is the only one trading something already measured
rather than forecast: once 85F is on the board, every bucket topping out below it
is worth exactly zero, and thin books are slow to mark that.

**Watch `max_region_exposure`.** Cities under one air mass move together —
NYC, Boston, Philadelphia and DC on the same day are one weather bet, not four.
A dollar cap alone does not notice. This caps exposure per region per day at 18%
of the book by default, and it matters far more now that 23 cities are
configured than it did with 8.

**`CITY_WHITELIST`** is empty by default, meaning trade everywhere. Once
calibration tells you which cities the ensemble actually has skill on, list them
there and the bot trades nothing else.

**Watch `max_total_exposure`.** Defaults to 55% of the book. Without it a $100
strategy facing 200 buckets is fully invested within minutes and has nothing left
for a better trade an hour later.

**S8 is the interesting new one.** A daily temperature distribution has one
peak, so no bucket should be worth less than *both* of its neighbours. When one
is, that is thin books or a stale order rather than weather, and it can be bought
with no view on the forecast at all. Forecast-free like Dutch, but it fires far
more often. It ignores the two open-ended end buckets, where a genuine tail looks
identical to a dip.

**S7 is the one to distrust.** Selling the wings is short volatility — small
frequent wins, then a heatwave takes it back. Watch `max_open_fades`, not its
win rate.

Expect S1, S5, S6 and S7 to be active. S3 fires rarely by design. S4 only wakes
near settlement.

## The width problem

The model's real information is the ensemble **mean** — where the distribution
sits. Its **width** is a parameter, and a guessed one. Guess it too wide and the
model marks every event's favourite too cheap, shorts all of them at once, and
turns one wrong number into thirty correlated losing trades.

Check `/api/health/calibration`. If `model_minus_market_by_rank.rank_1` is a
large negative number across many events, that is not thirty opportunities — it
is one disagreement repeated thirty times.

`MARKET_SD_WEIGHT` (default 0.5) anchors the kernel halfway to the width the
book implies, so the bot trades on where the distribution sits rather than on how
wide it is. Set it to 0 to trust the prior outright, or 1 to adopt the market's
width and trade only on location. Move it toward 0 once settled markets show the
prior is actually better.

`max_side_exposure` (default 0.75) refuses to let a strategy put more than
three-quarters of its book on one outcome side.

## Two risk bugs fixed after the first overnight run

**Exposure was measured by current mark value, not by what was actually spent.**
As a losing position's price fell, its marked value shrank, making the position
cap think there was room for more — so the bot kept buying the same market at a
worse price as it went against it. One real position spent $14 against a $5 cap
this way. Exposure is now tracked by cost basis, which does not move with the
price.

**S5's fills never passed through any risk check at all.** Resting quotes are
posted without checking capacity, correctly — an unfilled order has committed no
capital. But nothing checked capacity at the other end either, when a fill turns
that resting order into real capital at risk. One overnight run recorded $240 of
buy notional against a $100 book. Fills now check the portfolio cap, the cash
balance and the side cap before they are allowed to happen, with a running total
across every fill in the same tick so the check cannot be fooled by acting on a
stale snapshot.

## Fees are the main adversary

Polymarket charges a taker fee on weather markets:

```
fee per share = 0.05 x min(price, 1 - price)
```

At a 28c price that is **1.4c a share, 5% of notional** — comparable to the
entire edge these strategies look for. Every edge is compared net of it, the
dutch book prices it per leg, and S5 avoids it entirely by never crossing the
spread.

A gross 2-point edge at 50c is a losing trade, and the bot declines it.

---

# Part 4 — Running it

## First day

Leave it alone. Read the evaluations panel to learn what each strategy is doing.

Position caps are sized for a $100 book: $12 per market on Edge, Consensus,
Drift, Observed and Fade; $40 per set on Dutch; $20 per market and $15 inventory
on Quote. These are **total exposure caps, not per-fill limits**.

If you changed `START_BANKROLL` or upgraded the build, hit **Reset book** on
every tab. Fills recorded under different settings are not comparable.

## Tuning — not during the run

Each tab has a settings panel. Changes save to the database and take effect on
the next tick. **Restore defaults** is there if you overshoot.

**Do not touch it for the first 10–15 days.** The point of this run is an
out-of-sample measurement, and a parameter changed on day 6 means fills before
and after it are not comparable — you end up with two half-runs of five days
each instead of one run of fifteen. Same for `Reset book`.

## Downloading data

**Download data** in the header gives you a zip: every table as CSV plus a
manifest. Uncompressed by default, so it opens straight into Excel or pandas.
Take one weekly and keep them; see Appendix A.

Raw order-book ticks for the ~900 markets that are merely watched are kept
`QUOTE_RETENTION_H` hours and rolled into hourly bars first, which is what keeps
the file inside a free-tier volume. Everything the analysis reads — trades,
settlements, resolutions, `mark_ticks`, `settled_highs`, and forecasts for
`FORECAST_RETENTION_D=45` days — is never pruned during the run.

## Day 15 — what to actually compute

In this order, and stop at the first one that fails.

**0. Is the P&L real?** `/api/health/settlement`. Every settlement should be
`winner_src=gamma`. Anything else is not evidence.

**1. The calibration gate.** The only test that matters:

> Compute the Brier score for the model's probabilities and for the market mid,
> over the same settled markets. **If the market's score is lower, you have no
> edge**, and no amount of parameter tuning will create one.

**2. Net of fees, per strategy.** The taker fee is ~1.4c per share at 28c —
the same order of magnitude as the entire edge being hunted. A strategy that is
profitable gross and flat net is not profitable.

**3. Sample size, honestly.** ~900 settlements sounds like a lot, but the buckets
within one city-day are a single correlated bet. The effective sample is roughly
16 cities × 15 days ≈ **240 independent observations** — thin for detecting a
few cents of edge, and worth remembering before concluding anything.

**4. Would cashing out have been better?** `/api/analysis/cashout?take=0.85&stop=0.15`
replays `mark_ticks` for every held position and compares holding to settlement
against a take-profit/stop rule. Note that `best_exit_pnl` is a hindsight upper
bound, not a strategy — it tells you how much was on the table, not how much you
could have captured.

Only if all of that holds up, tune — in this order:

1. **`BIAS_<CITY>`** — the median of `actual_high − forecast_mean` per city at a
   fixed lead. Median, not mean; one frontal day skews it. This is the
   gridpoint-to-station gap and it is worth more than any strategy tweak. It is
   also the single most valuable thing the run produces, which is exactly why
   every `BIAS_*` must stay at 0 *during* the run: fitting it from the same days
   you then evaluate on is how you measure noise and call it edge.
   The `settled_highs` table records the recorded temperature per city and day, so
   this is a join against `forecasts`, not a reconstruction from winning buckets.
2. **`KERNEL_BASE` / `KERNEL_PER_DAY`** — compute `z = residual / σ_predicted`
   for every settled market. If `stdev(z)` is 1.3 your kernel is 30% too narrow;
   scale up and repeat. Split by lead time to separate the two constants.
3. **Then run a second, shorter window** to confirm the corrections helped, before
   any live capital. A fix validated only on the data it was fitted to is not
   validated.

---

# Part 5 — Going live

Only after the calibration gate passes, and after the confirmation window in
step 3 above.

One thing to be clear-eyed about: **the live order path has never executed a
single order.** `py-clob-client` is commented out of `requirements.txt` and every
trade in the database is `mode=paper`. Switching `LIVE_TRADING=true` is not the
last step of this project; it is the start of a separate piece of work with its
own failure modes (signing, allowances, partial fills, rejected sizes). Treat the
first live day as a test of the plumbing, not of the strategy.

1. Edit `requirements.txt`, uncomment `py-clob-client`, commit. It is commented
   out by default because it pulls in `web3` and `eth-account`, which need a
   compiler and are a common cause of failed builds. Paper mode never uses it.
2. Fund a Polymarket account, then set:

```
LIVE_TRADING=true
POLYMARKET_PRIVATE_KEY=...
POLYMARKET_FUNDER=...
POLYMARKET_SIG_TYPE=1        # 1 = email/Magic proxy wallet, 0 = EOA
```

3. Enable **one** strategy. Set `max_position_usd` to something you would shrug
   at. Watch it for a full day.

The daily loss limit halts everything at −12% of the day's opening equity.
`Reset book` is disabled while live.

Polymarket restricts access from some jurisdictions. Where you can legally trade
is a question about your situation — settle it before funding anything, not
after.

---

# Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `/version.txt` 404s | Old build is live. Your push did not land — check **Deployments** for the failed build. |
| Version string is older than 0.16.1 | Same. Railway is serving the last healthy deployment. |
| `STORAGE NOT DURABLE` and the container restart-loops | Working as intended. No volume, or it is not mounted at the directory `DB_PATH` names. Mount `/data`, keep `DB_PATH=/data/wxalpha.db`. |
| `/health` returning 503 and Railway restarting the service | The trading loop stopped ticking for over `HEALTH_MAX_TICK_AGE_S`. The restart is the fix; check the error log for what wedged it. |
| `db (380 mb)` and climbing near `MAX_DB_MB` | Retention is not keeping up. Lower `QUOTE_RETENTION_H` first — raw ticks are the bulk and they are rolled into `quotes_hourly` before deletion. Never lower `FORECAST_RETENTION_D` below the run length. |
| `S5 maker fills: no volume figure on N quotes` hourly | Gamma is not returning `volume_num`, so no maker fill can be verified and none is booked. Intended: S5 showing zero fills is honest, S5 showing invented ones is not. |
| Build fails on `web3` / `eth-account` | `py-clob-client` is uncommented. Comment it out; paper mode does not need it. |
| `ModuleNotFoundError: app` | Repo root has `wxalpha/` instead of `app/`. Fix: Service → **Settings → Source → Root Directory** → `wxalpha` → redeploy. |
| `no usable weather markets` | Event-slug pattern changed. The error log prints the slug it tried; compare it against a live `polymarket.com/event/<slug>` URL and set `SLUG_TEMPLATES`. |
| `gamma-stale` in book sources | Per-tick price refresh failing. Late strategies unreliable until fixed. |
| `clob depth unavailable` | Sizes fall back to `ASSUMED_SIZE`. Prices still fresh; harmless. |
| `forecast refresh: 6/16 cities` | Fixed in 0.15.0. The ensemble endpoint bills by models x members x hours and was throttling. Requests are now smaller and batched, with a 5-model deterministic fallback so every city gets priced. |
| Half the markets have no fair value | Same cause. Check `priced_last_15m` against `open_markets` in `/api/health/feeds`. |
| Markets found but no trades | Usually correct. Read the skip reasons in evaluations — normally below min notional, or the edge is negative after fees. |
| A tab flat at exactly $100 | No fills yet, or the strategy is paused. Check the tab header. |
| Dashboard blank after a deploy | Cached JavaScript. Hard-refresh with Ctrl-Shift-R. |
| `SERVER UNREACHABLE - RETRYING` | Fixed in 0.15.0. Was an open-ended bucket serialising as `Infinity`, which is not legal JSON, so `/api/ribbon` returned 500 and took the page with it. |
| Signals quoting `31C` with 30-point edges | Fixed in 0.15.0. London, Paris, Moscow and Seoul quote in Celsius; those buckets were being priced against a Fahrenheit forecast. |
| Dozens of identical small fills on Quote | Fixed in 0.15.0. Gamma's `lastTradePrice` is a static field, not a trade feed; a resting quote below it refilled every tick. A fill now requires a *new* print. |
| Settled trades that look wrong | **Fixed in 0.16.0, and it was severe.** 0.15.0 settled on the recorded temperature, which Open-Meteo publishes hours before Polymarket resolves — so the observation wrote a winner first and the authoritative verdict never overwrote it. 38 of 180 settlements were scored against the wrong outcome. Only Polymarket's own resolution settles anything now. Check `/api/health/settlement`. |
| Open risk equals equity, no cash left | Fixed in 0.15.0 with `max_total_exposure`. Per-market caps do not stop a book going all-in across 200 buckets. |
| Everything resets on redeploy | No volume mounted at `/data`. Cannot happen now unless you set `REQUIRE_DURABLE_DB=false`. |
| A market closed days ago and never settled | Correct behaviour. Polymarket never resolved it, so no winner is invented. After `STALE_MARKET_H` it is retired from the live set and counted under `retired_unresolved`. |
| **Nothing at all is settling** — hundreds of markets past close, every one `winner=NULL` | **Fixed in 0.16.1, and it silently voided a full day of the first run.** Settlement had one path: query Gamma's `/markets` by id. That endpoint defaults to *live* markets, so asking it about a resolved one returned an empty list — no error, no non-200, nothing logged. Meanwhile the event scan was handed the verdict every 90 seconds and dropped it at `if m.get("closed"): continue`. Now the scan books what it sees, the id query sends `closed=true` with a fallback, and a query that comes back empty says so in the log. Check `/api/health/settlement` — `settled_by_gamma` must climb within a day of the first market closing. |

## Useful URLs

```
/                        dashboard
/version.txt             what build is running
/health                  503 if the trading loop has stalled
/api/health/feeds        one-word verdict on the data feeds
/api/health/settlement   is the P&L sourced from Polymarket's own resolution
/api/analysis/cashout    would exiting early have beaten holding to settlement
/api/discovery           every event seen, accepted or rejected, with reasons
/api/logs?q=gamma        filtered log
/api/logs?level=error    problems only
/api/export.zip          all data, uncompressed, one click
```

---

# Appendix A — getting the data off Railway

`/api/export.zip` writes every table out as CSV plus a JSON manifest. Entries are
**stored, not deflated** (`EXPORT_COMPRESS=false`), so the archive opens straight
into Excel or pandas with no unpacking step — at the cost of a larger download.
Set `EXPORT_COMPRESS=true` if you are pulling it over a slow link.

Take one at the end of the run before touching any setting. Retention prunes raw
book ticks after `QUOTE_RETENTION_H` — everything the day-15 analysis needs is
kept for the whole run, but the export is still the artifact you keep.

The two tables that did not exist in 0.15.0 and matter most:

- **`mark_ticks`** — the price track of markets you actually hold, one row per
  held market every `MARK_EVERY_S` (default 300s) or sooner on a `MARK_MIN_MOVE`
  move. This is what makes "would cashing out early have been better" answerable
  after the fact, and it is what `/api/analysis/cashout` replays.
- **`resolutions`** — Polymarket's own verdict per market, with the source
  recorded, so a settlement can always be traced back to what resolved it.

# Appendix B — there is no demo mode

0.15.0 shipped a synthetic-data mode (`DEMO_MODE=true`, `app/demo.py`). It is
deleted.

It invented the truth temperature *and* the market's mispricing, so its P&L said
nothing about whether the strategies make money — and its `market_price`
deliberately overpriced the wings so S7 always had something to trade. Worse, the
local launcher fell back to it automatically, so a single failed API lookup at
boot could turn a run you believed was real into fiction that looked identical on
the dashboard.

For a build whose only job is measurement, a data source that can silently
substitute itself for reality is the most expensive feature in the codebase.

**It is not a backtest.** The truth temperature and the market's mispricing are
both invented, so its P&L says nothing about whether these strategies make
money. Only settled real markets answer that.

---

# What this cannot promise

Nobody can tell you this is profitable, and you should distrust anyone who does.
The forecast edge is thin and depends entirely on calibration you have to do
yourself against your own settled markets. The fee is larger than most of the
edges. S5's paper fills are optimistic because the simulator assumes
front-of-queue and cannot see traded volume.

Run it in paper until the Brier comparison says the model beats the market. If it
does not, that is the answer, and it is a cheaper answer than finding out with
money.
