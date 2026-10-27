@echo off
title ProtoBot
cd /d "%~dp0"

if not exist venv python -m venv venv
rem Every start, so new dependencies reach an existing venv (fast when already installed).
venv\Scripts\pip install -q -r requirements.txt

:loop
venv\Scripts\python dispatch.py
echo [%date% %time%] dispatcher exited, restarting in 5s (Ctrl+C = stop)
timeout /t 5 >nul
goto loop
