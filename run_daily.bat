@echo off
setlocal
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHON=C:\Users\gjq00\.conda\envs\qbg\python.exe"
cd /d "%~dp0"
"%PYTHON%" -m qbg.orchestrator.daily_cycle %*
exit /b %ERRORLEVEL%
