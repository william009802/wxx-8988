@echo off
REM One-time setup. Double-click this, or run it from a terminal.
cd /d "%~dp0"

echo.
echo === wxalpha setup ===
echo.

py --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found.
  echo.
  echo Install it from https://www.python.org/downloads/
  echo IMPORTANT: tick "Add Python to PATH" on the first screen of the installer.
  echo Then close this window, open a new one, and run this file again.
  echo.
  pause
  exit /b 1
)

echo Python found:
py --version
echo.

if not exist .venv (
  echo Creating a private environment in .venv ...
  py -m venv .venv
  if errorlevel 1 goto fail
)

call .venv\Scripts\activate.bat
if errorlevel 1 goto fail

echo Installing dependencies ...
python -m pip install --upgrade pip --quiet
pip install -r requirements.txt
if errorlevel 1 goto fail

echo.
echo === Offline self-test ===
echo This needs no internet and checks the pricing, all seven strategies,
echo the fee maths, accounting and export.
echo.
python selftest.py
echo.

echo === Connectivity check ===
python check_apis.py
echo.
echo Setup finished.
echo.
echo   Start the bot:   windows-run.bat
echo   Full guide:      LOCAL.md
echo.
pause
exit /b 0

:fail
echo.
echo Setup failed above. Send me the last few lines.
pause
exit /b 1
