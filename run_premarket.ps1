<#
.SYNOPSIS
    盘前复核的外壳：窗口闸 -> 预检 -> scripts\26_premarket.py -> 原样传出退出码。

.DESCRIPTION
    2026-09-09 实测：TradingAgents 复核 5 只票花 58 分钟，而
    `require_trading_session` 是硬闸 —— 09:30 现场复核会跑到上午盘尾甚至收盘
    之后，**整天作废且 LLM 的钱已经花掉**（当天 $0.23）。

    所以把慢活挪到盘前：

        08:00  这个脚本         拉数 + 重训 + 复核 -> data/reviews/<date>.json
        09:30  run_daily.ps1    读缓存 + 风控 + 下单（约 2 分钟）

    **它不下单。** 26_premarket.py 里没有 execution、没有风控闸、不碰三把锁。
    跑飞了的后果只是日流程退回盘中现场复核 —— 慢，但正确。

.NOTES
    **为什么盘前也要跑预检**：复核要调 LLM 中转站，而那条链路走 Clash 代理。
    预检 09:15 才开代理，08:00 时可能还没开。预检是幂等的（代理开着就不发热键、
    同花顺在跑就只报告），多跑一次没有副作用。

    副作用是好的：同花顺会在 08:00 就被拉起来，给人工登录留出一个半小时，
    而不是 09:15~09:30 那 15 分钟。

    **锁和日流程分开**。共用一把锁的话，盘前跑超时（LLM 慢）会把 09:30 的日流程
    直接挡掉 —— 而那是当天唯一能下单的机会。宁可两边并行：日流程发现缓存还没
    写好就退回现场复核，慢但至少能交易。
#>

[CmdletBinding()]
param(
    [switch]$SkipPreflight,
    [switch]$Pause,
    # 由 setup_schedule.ps1 写进任务动作；手工运行不带，因此不受窗口限制。
    [string]$ScheduledAt   = "",
    [int]   $WindowMinutes = 90,
    [switch]$IgnoreWindow,
    # 透传给 26_premarket.py：--skip-ingest / --no-retrain / --dry-run / --date
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

$ErrorActionPreference = "Continue"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

$env:PYTHONUTF8       = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }

$LogDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$RunLog = Join-Path $LogDir ("premarket_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

function Write-Run([string]$msg) {
    Write-Host $msg
    try { Add-Content -LiteralPath $RunLog -Value $msg -Encoding UTF8 } catch { }
}
filter Write-Tee { Write-Run ([string]$_) }

# --- 过期不补跑 -------------------------------------------------------------
# 窗口比日流程短（90 vs 120）：盘前任务的价值在于**赶在 09:30 之前跑完**，
# 09:30 之后才被唤起的话，跑完也赶不上当天的日流程了。
$guardPath = Join-Path $ProjectRoot "scripts\window_guard.ps1"
if (Test-Path $guardPath) {
    . $guardPath
    if (-not $IgnoreWindow) {
        $skipReason = Get-QbgWindowSkipReason -ScheduledAt $ScheduledAt -WindowMinutes $WindowMinutes
        if ($skipReason) {
            Write-Run ("{0} SKIP {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $skipReason)
            Write-Run "这是 -StartWhenAvailable 的过期补跑，已跳过。强制运行请加 -IgnoreWindow。"
            exit 0
        }
    }
} elseif ($ScheduledAt) {
    Write-Run "[WARN] 找不到 $guardPath —— 本次不做过期判断，照常运行。"
}

$LockPath = Join-Path $LogDir "premarket.lock"
$lock = $null
try {
    $lock = [System.IO.File]::Open($LockPath, 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    Write-Run ("{0} SKIP 已有盘前任务在跑（{1}）" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $LockPath)
    exit 0
}

function Stop-All([int]$code) {
    if ($lock) { try { $lock.Close() } catch { } }
    if ($Pause) { Write-Host ""; Read-Host "按回车关闭" | Out-Null }
    exit $code
}

# --- 解释器（和 run_daily.ps1 同一份候选表）---------------------------------
function Resolve-Python {
    $candidates = @()
    if ($env:QBG_PYTHON) { $candidates += $env:QBG_PYTHON }
    $candidates += (Join-Path $env:USERPROFILE ".conda\envs\qbg\python.exe")
    $candidates += "C:\ProgramData\miniconda3\envs\qbg\python.exe"
    $candidates += "C:\ProgramData\Anaconda3\envs\qbg\python.exe"
    foreach ($c in $candidates) { if ($c -and (Test-Path $c)) { return $c } }
    return $null
}
$py = Resolve-Python
if (-not $py) {
    Write-Run "[ERROR] 找不到 qbg 环境的 Python。设 QBG_PYTHON 或先建环境。"
    Stop-All 127
}

# --- 预检：复核要调 LLM，那条链路走代理 -------------------------------------
if (-not $SkipPreflight) {
    $pf = Join-Path $ProjectRoot "scripts\preflight.ps1"
    if (Test-Path $pf) {
        $env:QBG_RUN_LOG = $RunLog
        & $pf 2>&1 | Write-Tee
        if ($LASTEXITCODE -eq 2) {
            Write-Run "[ERROR] 预检致命失败（exit 2）—— qbg 环境不可用，中止。"
            Stop-All 2
        }
    }
}

Write-Run ""
Write-Run "============================================================"
Write-Run (" quant-biga 盘前复核   {0}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
Write-Run ("  解释器 : {0}" -f $py)
Write-Run ("  日志   : {0}" -f $RunLog)
Write-Run "============================================================"

$script = Join-Path $ProjectRoot "scripts\26_premarket.py"
if ($ScriptArgs) {
    & $py $script @ScriptArgs 2>&1 | Write-Tee
} else {
    & $py $script 2>&1 | Write-Tee
}
$rc = $LASTEXITCODE

Write-Run ("盘前复核退出码: {0}" -f $rc)
Stop-All $rc
