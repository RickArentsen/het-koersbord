@echo off
REM ---------------------------------------------------------------
REM  Het Koersbord
REM  One shortcut: starts the API, serves the dashboard, opens it.
REM  Right-click this file -> Send to -> Desktop (create shortcut)
REM ---------------------------------------------------------------
setlocal
title Het Koersbord
cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
  echo No virtual environment found in backend\.venv
  echo Create one with:  python -m venv .venv
  pause
  exit /b 1
)

echo Checking dependencies...
".venv\Scripts\python.exe" -m pip install -q -r requirements-portfolio.txt

echo.
echo Het Koersbord starting on http://127.0.0.1:8090
echo Close this window to stop it.
echo.
".venv\Scripts\python.exe" portfolio_app.py

endlocal
