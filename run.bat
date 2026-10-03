@echo off
cd /d "%~dp0"
rem Python 3.12 virtualenv (needed for modern yt-dlp)
venv\Scripts\python.exe bot.py
pause
