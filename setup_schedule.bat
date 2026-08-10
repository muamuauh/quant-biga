@echo off
setlocal
set "TASK_NAME=quant_biga_daily"
set "RUNNER=%~dp0run_daily.bat"
schtasks /Create /TN "%TASK_NAME%" /TR "\"%RUNNER%\"" /SC DAILY /ST 17:45 /F
echo 已创建 %TASK_NAME%（每日 17:45；脚本内部会判断是否交易日）

