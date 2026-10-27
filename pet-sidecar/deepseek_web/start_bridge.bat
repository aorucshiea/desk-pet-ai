@echo off
setlocal
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.8+ is required. Install it and try again.
  pause
  exit /b 1
)
python bridge.py
pause
