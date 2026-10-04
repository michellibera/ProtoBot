@echo off
title ProtoBot
cd /d "%~dp0"

if not exist venv (
    python -m venv venv
    venv\Scripts\pip install -r requirements.txt
)

:loop
venv\Scripts\python dispatch.py
echo [%date% %time%] dispatcher exited, restarting in 5s (Ctrl+C = stop)
timeout /t 5 >nul
goto loop
