@echo off
setlocal
set "PYTHON=C:\Users\gjq00\.conda\envs\qbg\python.exe"
cd /d "%~dp0"
"%PYTHON%" -m qbg.orchestrator.daily_cycle %*
exit /b %ERRORLEVEL%

