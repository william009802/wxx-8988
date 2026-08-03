@echo off
REM Starts wxalpha on http://localhost:8000 against real Polymarket data,
REM paper trading. There is no synthetic-data mode any more: invented numbers
REM that look like results are worse than no results.
cd /d "%~dp0"

if not exist .venv (
  echo.
  echo Run windows-setup.bat first.
  echo.
  pause
  exit /b 1
)

call .venv\Scripts\activate.bat

REM ---- settings. Edit the email. Leave the rest alone for paper trading. ----
set DB_PATH=.\wxalpha.db
set START_BANKROLL=700
set USER_AGENT=wxalpha/1.0 change-me@example.com
set LIVE_TRADING=false
REM A local database sits on the ordinary filesystem, so the Railway volume
REM check has to be off here. Leave it ON when deploying.
set REQUIRE_DURABLE_DB=false

echo.
echo Checking whether the Polymarket API is reachable from this machine ...
python check_apis.py --gamma-only
if errorlevel 1 (
  echo.
  echo   ---------------------------------------------------------------
  echo   The Polymarket API is NOT reachable from this PC.
  echo   Starting anyway: it will keep retrying, but it will find no
  echo   markets until the network allows it. Deploy to Railway for
  echo   real data.
  echo   ---------------------------------------------------------------
) else (
  echo.
  echo   ---------------------------------------------------------------
  echo   Polymarket API reachable. Starting with REAL market data,
  echo   in paper trading mode. No real money is at risk.
  echo   ---------------------------------------------------------------
)

echo.
echo   dashboard      http://localhost:8000
echo   version        http://localhost:8000/version.txt
echo   feed health    http://localhost:8000/api/health/feeds
echo   settlements    http://localhost:8000/api/health/settlement
echo.
echo   Press Ctrl+C in this window to stop.
echo.
python -m uvicorn app.main:app --port 8000
pause
