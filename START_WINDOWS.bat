@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if not errorlevel 1 (
  py -3 launcher.py
  goto finished
)
where python >nul 2>nul
if not errorlevel 1 (
  python launcher.py
  goto finished
)
echo Python was not found. Install Python 3.11 or newer, then reopen this file.
:finished
pause
