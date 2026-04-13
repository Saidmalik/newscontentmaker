@echo off
cd /d "%~dp0"

set SCRIPT=%~dp0src\auto_collect.py

echo === UZ News Bot - Task Scheduler Setup ===
echo Script: %SCRIPT%
echo.

schtasks /delete /tn "UzNewsBot_12" /f >nul 2>&1
schtasks /delete /tn "UzNewsBot_22" /f >nul 2>&1

schtasks /create /tn "UzNewsBot_12" /tr "python \"%SCRIPT%\" --silent" /sc DAILY /st 12:00 /f /RL HIGHEST
schtasks /create /tn "UzNewsBot_22" /tr "python \"%SCRIPT%\" --silent" /sc DAILY /st 22:00 /f /RL HIGHEST

echo.
if %errorlevel% == 0 (
    echo OK: Tasks created - 12:00 and 22:00 daily
) else (
    echo ERROR: Run this file as Administrator
    echo Right-click - Run as administrator
)
echo.
pause
