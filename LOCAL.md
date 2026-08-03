# Running wxalpha on your own PC (Windows)

For checking everything works before Railway. Three files do all of it.

**One thing worth knowing first:** `gamma-api.polymarket.com` *is* Polymarket —
it is a subdomain, not a separate service. The bot never opens `polymarket.com`
itself, only the API. So the website being blocked in your browser does **not**
necessarily mean the API is blocked. The launcher tests this for you and tells
you which it is before it starts, so you do not have to find out first.

---

## Step 1 — Install Python

Download from **https://www.python.org/downloads/** and run the installer.

**Tick "Add Python to PATH" on the first screen of the installer.** It is easy
to miss and nothing works without it.

Close any terminal windows you had open afterwards.

> If you see *"Python was not found; run without arguments to install from the
> Microsoft Store"*, that message comes from a Windows placeholder, not Python.
> It means Python is not installed, or PATH was not ticked.

## Step 2 — Extract

Right-click `wxalpha.zip` → **Extract All**. Open the extracted `wxalpha`
folder. You should see `app`, `windows-setup.bat`, `windows-run.bat` and the
rest.

## Step 3 — Double-click `windows-setup.bat`

One time only. It will:

1. check Python is installed, and tell you exactly what to do if not
2. build a private `.venv` folder so nothing touches your system Python
3. install the three dependencies
4. run `selftest.py` — **completely offline**, works regardless of any blocking
5. run `check_apis.py` to show what your network can reach

Expect **`ALL CHECKS PASSED`** from the self-test. That alone confirms the
pricing model, all eight strategies, the fee arithmetic, position accounting,
settlement and export are correct.

The connectivity section then prints something like:

```
[ ok  ] Gamma  (market discovery)    REQUIRED
[BLOCK] Polymarket website           not used by the bot
[ ok  ] Open-Meteo ensemble          REQUIRED
```

Gamma `ok` and the website `BLOCK` is a perfectly good outcome — the bot only
needs the first one.

## Step 4 — Double-click `windows-run.bat`

It probes the API and chooses for you:

**If the API is reachable**

```
Polymarket API reachable. Starting with REAL market data,
in paper trading mode. No real money is at risk.
```

Real markets, real prices, real forecasts. Identical to what Railway will do.

**If it is not**

```
The Polymarket API is NOT reachable from this PC.
Starting anyway: it will keep retrying, but it will find no
markets until the network allows it.
```

It starts, ticks, and discovers nothing. That is the honest outcome and it is
the intended one: there used to be a synthetic-data fallback here, and a single
failed lookup at boot would have quietly turned a data-collection run into
invented numbers that look exactly like results.

Either way, open **http://localhost:8000**.

Press **Ctrl+C** in the black window to stop it.

---

## What to check once it is running

| URL | Expected |
|---|---|
| http://localhost:8000 | dashboard, ribbon populated, eight tabs at $100 each |
| http://localhost:8000/version.txt | `wxalpha 0.16.1`, 8 strategies |
| http://localhost:8000/api/health/feeds | `"verdict": "ok"` |
| http://localhost:8000/api/health/settlement | `"nothing settled yet"` on a fresh database |

In the black console window, within about 45 seconds:

```
wxalpha 0.16.1 starting, mode=paper, 8 strategies
gamma: 20 by slug, 0 from tags, 0 more from sweep, 20 unique candidates
discovery: 20 events scanned, 20 usable, 220 buckets stored
forecast refresh: 8/8 cities, 8 with live observations
book sources: {'clob': 220}
```

Then click around the dashboard:

- The **isotherm ribbon** at the top shows model probability as coloured bands
  with the market price as a dashed line over it. The gap between them is the
  trade.
- Each of the eight tabs has an **evaluations** panel listing every decision
  that strategy made, *including the ones it passed on and why*. This is the
  most useful panel in the app. A silent strategy is usually working correctly.
- The **market scan** panel shows every event discovery accepted or rejected.

## There is no demo mode any more

Earlier builds shipped a synthetic simulator (`app/demo.py`,
`windows-run-demo.bat`) that invented markets, order books and observations when
the API was unreachable. Both are deleted.

It invented the truth temperature *and* the market's mispricing, so its profit
and loss said nothing about whether these strategies make money — and worse, it
was reachable by accident: one failed lookup at boot and a run you believed was
real was synthetic. For a build whose entire purpose is measurement, a data
source that can silently substitute itself for reality is the most expensive
feature in the codebase. Only settled real markets answer the question, and that
has to happen on Railway.

If you want to see the dashboard populated without waiting, run it against real
markets in paper mode — which is what `windows-run.bat` already does.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Python was not found` | Not installed, or "Add Python to PATH" was not ticked. Reinstall from python.org. |
| Setup fails installing dependencies | Send me the last few lines of the black window. |
| `selftest.py` fails | Send me the output. This should never happen — it needs no network. |
| Window flashes and closes instantly | Open Command Prompt, `cd` to the folder, and run `windows-run.bat` from there so you can read the error. |
| Dashboard is blank | Hard-refresh with Ctrl-Shift-R. |
| `0 by slug` and connection errors | The API is blocked from this PC. Nothing to fix locally — Railway sits in the US/EU and reaches it fine. |
| `STORAGE NOT DURABLE` at startup | Only happens with `REQUIRE_DURABLE_DB=true`. Locally, leave it false; on Railway, mount the volume. |
| Port 8000 already in use | Edit `windows-run.bat`, change `--port 8000` to `--port 8100`. |

## Files

```
windows-setup.bat       run once: installs and self-tests
windows-run.bat         normal start, real markets, paper trading
check_apis.py           what can this machine reach
selftest.py             offline test suite, no network needed
GUIDE.md                the Railway deployment guide
README.md               technical reference
```

## When you move to Railway

Nothing changes in the code. Railway sits in the US or EU, so it reaches the API
regardless of what your PC can see. Two things you must set there and not here:

- **a Volume mounted at `/data`** — without it the database is deleted by the
  next deploy, and `REQUIRE_DURABLE_DB=true` makes the bot refuse to start
  rather than collect two weeks onto disposable storage
- **`REQUIRE_DURABLE_DB=true`** — locally `windows-run.bat` sets it false,
  because a throwaway database is the point of a local run

Follow `GUIDE.md` for the rest.

Delete `wxalpha.db` before you start collecting data you intend to calibrate
from, so local experiments do not contaminate it.
