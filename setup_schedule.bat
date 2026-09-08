@echo off
REM ============================================================
REM  注册计划任务的双击入口。逻辑在 scripts\setup_schedule.ps1。
REM
REM  默认 -Mode Interactive（只在用户登录时运行），**不需要管理员权限**。
REM  有真桌面，所以 Clash 热键、TUN、同花顺 UI 自动化全都能用。
REM
REM  想要无人登录也跑的模式（代价：TUN 开不了、同花顺自动化不可用）：
REM    右键「以管理员身份运行」，或管理员 PowerShell 里：
REM    powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1 -Mode Background
REM
REM  两种模式的取舍见 docs\windows-schedule.md。
REM ============================================================
setlocal
chcp 65001 >nul
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_schedule.ps1" %*
echo.
pause
exit /b %ERRORLEVEL%
