@echo off
REM ============================================================
REM  双击入口。真正的逻辑在 run_daily.ps1，这里只是个壳。
REM
REM  用 System32 的 powershell.exe 而不是 pwsh：pwsh 在本机是 Store 安装，
REM  路径是 WindowsApps 下的应用执行别名，非交互会话解析不开。
REM
REM  -Pause 是给双击用的（跑完停住让人看结果）。
REM  **计划任务不要调这个 bat**，让它直接调 run_daily.ps1，
REM  否则 -Pause 会把任务挂到超时。scripts\setup_schedule.ps1 已经这么做了。
REM
REM  参数原样透传给 daily_cycle：
REM    --dry-run / --retrain / --skip-ingest / --date YYYY-MM-DD / --force
REM ============================================================
setlocal
chcp 65001 >nul
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_daily.ps1" -Pause %*
exit /b %ERRORLEVEL%
