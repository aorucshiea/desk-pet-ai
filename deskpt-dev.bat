@echo off
rem Double-click launcher for the dev environment.
rem (.ps1 files cannot be double-clicked directly - Windows would show
rem the "how do you want to open this file" dialog. This wrapper calls
rem the script with the right execution policy.)
rem ASCII-only + CRLF on purpose: cmd.exe parses .bat with the system
rem ANSI codepage, so UTF-8 Chinese here turns into mojibake commands.
title MiniCPM Desk Pet Launcher
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deskpt-dev.ps1"
echo.
echo (Launcher finished. The pet should be running; closing this window does not stop it.)
pause
