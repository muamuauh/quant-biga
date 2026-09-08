<#
.SYNOPSIS
    quant-biga 每日编排（Windows）。计划任务和手工双击都走这个入口。

.DESCRIPTION
    预检（Clash 代理/TUN + qbg 环境）-> daily_cycle -> 按语义传出退出码。

    对应 Linux 的 run_daily.sh，纪律一致：

      * 不用 conda activate，直接指解释器绝对路径。计划任务不加载任何 profile，
        conda 的 shell 函数在那里根本不存在 —— 这是定时任务最常见的失败点。
      * 互斥锁防止计划任务和手工执行撞车。流程内部有当日幂等标记，但那是跑到
        一半才生效的；两个进程同时写 parquet / runs.db / 报告才是真正的麻烦。
      * 退出码原样传出，**不许吞**：
            0     正常（含"非交易日/今日已跑"这类安静跳过）
            2     硬闸中止 —— 风控拒绝交易，需要人看
            127   连解释器都找不到（Python 没机会跑，告警邮件链是断的）
            其他  异常退出，Python 侧已尽力发过崩溃告警邮件

    预检失败为什么不阻断：本项目行情走 BaoStock（境内直连），代理只服务 P7/P8
    的 LLM 中转站，而那两条链路本来就是 fail-open。代理没开起来照样要出当日
    清单 —— 这一点和 quant-trading 相反，那边 OpenD 端口不通是真的没法交易。
    只有预检返回 2（qbg 环境不可用）才中止。

.PARAMETER SkipPreflight
    跳过 scripts\preflight.ps1，直接跑流程。

.PARAMETER Pause
    结束时停住等回车。给双击用；计划任务**不要**带这个参数。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File run_daily.ps1
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File run_daily.ps1 --dry-run
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File run_daily.ps1 --date 2026-08-21 --force
#>

[CmdletBinding()]
param(
    [switch]$SkipPreflight,
    [switch]$Pause,
    # --- 过期不补跑（见 scripts\window_guard.ps1）-----------------------------
    # 这两个由 setup_schedule.ps1 写进计划任务的动作里，**手工运行不带** ——
    # 不带就没有窗口限制，人在任何时候都能跑。
    [string]$ScheduledAt   = "",
    [int]   $WindowMinutes = 120,
    # 明知过期也要跑（补一次昨天的、或者调试）。
    [switch]$IgnoreWindow,
    # daily_cycle 的参数原样透传：--dry-run / --retrain / --skip-ingest /
    # --date YYYY-MM-DD / --force
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CycleArgs
)

$ErrorActionPreference = "Continue"
$ProjectRoot = $PSScriptRoot
Set-Location $ProjectRoot

# 中文日报和 JSONL 日志都要 UTF-8。计划任务的控制台代码页是 936，不设会乱码。
$env:PYTHONUTF8       = "1"
$env:PYTHONIOENCODING = "utf-8"
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
try { $OutputEncoding = [Text.Encoding]::UTF8 } catch { }

# 注意：Windows 下 **不能**靠 export TZ 定时区 —— CPython 在 Windows 上读的是
# 系统时区，TZ 环境变量不起作用。所以这里不设，改由 preflight.ps1 回读
# Python 实际看到的 UTC 偏移并告警。

$LogDir = Join-Path $ProjectRoot "logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
$BootLog = Join-Path $LogDir "run_daily.log"

# 当天整段输出。计划任务会吞掉控制台，出事只留一句"上次运行结果 0x1"，
# 这个文件是唯一的现场。按天分文件，方便和 reports/daily/<date>.md 对上。
$RunLog = Join-Path $LogDir ("run_daily_{0}.log" -f (Get-Date -Format "yyyyMMdd"))

# ---------------------------------------------------------------------------
# 为什么不用 Start-Transcript
#
# 2026-08-22 本机实测：计划任务用的是 System32 的 powershell.exe（5.1），
# 它的 Start-Transcript 写文件时编不出非 ASCII —— 中文全变成 `????`，
# 而且**不报错**。日报、异常信息、风控理由全是中文，那种日志等于没有。
# 所以自己 tee，显式指定 UTF8。
# ---------------------------------------------------------------------------
function Write-Run([string]$msg) {
    Write-Host $msg
    try { Add-Content -LiteralPath $RunLog -Value $msg -Encoding UTF8 } catch { }
}

# 管道用：子进程/子脚本的每一行都既回显又落盘。
filter Write-Tee { Write-Run ([string]$_) }

# 启动摘要另写一份 run_daily.log：一次运行一行，翻历史比翻整段输出快得多。
function Write-Boot([string]$level, [string]$msg) {
    $line = "{0} {1} {2}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $level, $msg
    try { Add-Content -LiteralPath $BootLog -Value $line -Encoding UTF8 } catch { }
    Write-Run $line
}

# ---------------------------------------------------------------------------
# 过期不补跑
#
# 必须在**互斥锁之前**判：过期的补跑不该去争锁，更不该在拿不到锁时留下一条
# "已有实例在运行"的误导记录。也必须在预检之前 —— 预检有副作用（开系统代理、
# 开 TUN、拉起同花顺），而这正是晚上开机时最不想发生的事。
# ---------------------------------------------------------------------------
$guardPath = Join-Path $ProjectRoot "scripts\window_guard.ps1"
if (Test-Path $guardPath) {
    . $guardPath
    if (-not $IgnoreWindow) {
        $skipReason = Get-QbgWindowSkipReason -ScheduledAt $ScheduledAt -WindowMinutes $WindowMinutes
        if ($skipReason) {
            Write-Boot "SKIP" $skipReason
            Write-Boot "SKIP" "这是 -StartWhenAvailable 的过期补跑，已跳过。强制运行请加 -IgnoreWindow。"
            exit 0
        }
    }
} elseif ($ScheduledAt) {
    # **告警而不是中止。** 这道闸是便利设施不是安全闸 —— 文件丢了就让日流程
    # 从此不跑，比多跑一次过期补跑严重得多。tests/test_schedule_window.py
    # 会在 CI 阶段挡住"文件被删/没接线"这种情况。
    Write-Boot "WARN" "找不到 $guardPath —— 本次不做过期判断，照常运行。"
}

# ---------------------------------------------------------------------------
# 互斥锁
#
# 用文件独占句柄而不是 Mutex：Global\ 前缀的 Mutex 需要 SeCreateGlobalPrivilege，
# 普通权限的计划任务不一定有；而 Local\ 前缀跨不了会话，正好挡不住"计划任务在
# session 0 跑 + 人在 session 1 手动跑"这个最需要挡的场景。文件锁是文件系统对象，
# 跨会话天然有效，进程退出时由 OS 释放，不会留下需要手工清的僵尸锁。
#
# 拿不到锁直接退出 0：说明另一个实例正在跑，这不是错误，不该惊动计划任务的
# 失败告警。
# ---------------------------------------------------------------------------
$LockPath = Join-Path $LogDir "run_daily.lock"
$lock = $null
try {
    $lock = [System.IO.File]::Open($LockPath, 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    Write-Boot "SKIP" "已有实例在运行（$LockPath），本次退出"
    exit 0
}

Write-Run ""
Write-Run ("######## {0} run_daily.ps1 pid={1} ########" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $PID)

function Stop-All([int]$code) {
    if ($lock) { try { $lock.Close() } catch { } }
    if ($Pause) { Write-Host ""; Read-Host "按回车关闭" | Out-Null }
    exit $code
}

# ---------------------------------------------------------------------------
# 找解释器 —— 和 preflight.ps1 的候选表保持一致
# ---------------------------------------------------------------------------
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
    # Python 一旦跑起来，崩溃有 notify_failure 兜底发邮件。但连解释器都找不到时
    # （环境没建好、盘没挂上、目录被删）Python 根本没机会运行，通知链是断的 ——
    # 表现就是"今天没收到邮件"，而这和周末、和真的没交易，长得一模一样。
    # 所以这里必须以非零码退出，让计划任务的"上次运行结果"留下痕迹。
    Write-Boot "ERROR" "找不到 qbg 环境的 Python。设 QBG_PYTHON 指向解释器，或先建环境："
    Write-Boot "ERROR" "  conda env create -f environment.yml"
    Stop-All 127
}

# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------
$preflightRc = 0
if ($SkipPreflight) {
    Write-Run "预检已按 -SkipPreflight 跳过。"
} else {
    $pf = Join-Path $ProjectRoot "scripts\preflight.ps1"
    if (Test-Path $pf) {
        # preflight 用 Write-Host，PS 5.1 下**不进管道**，所以它自己往
        # QBG_RUN_LOG 落盘（见 preflight.ps1 里 Write-Line 的注释）。
        # 这里的 2>&1 | Write-Tee 只是兜住万一有东西走了 stderr。
        $env:QBG_RUN_LOG = $RunLog
        & $pf 2>&1 | Write-Tee
        $preflightRc = $LASTEXITCODE
        if ($preflightRc -eq 2) {
            Write-Boot "ERROR" "预检致命失败（exit 2）—— qbg 环境不可用，中止。"
            Stop-All 2
        }
        if ($preflightRc -ne 0) {
            Write-Boot "WARN" "预检有告警（exit $preflightRc）—— 继续跑流程（代理只影响 LLM 链路）。"
        }
    } else {
        Write-Boot "WARN" "找不到 $pf，跳过预检。"
    }
}

# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
$argsText = if ($CycleArgs) { $CycleArgs -join " " } else { "" }
Write-Boot "START" "$py -m qbg.orchestrator.daily_cycle $argsText"

Write-Run ""
Write-Run "============================================================"
Write-Run " quant-biga daily cycle"
Write-Run "  时间     : $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Run "  项目     : $ProjectRoot"
Write-Run "  解释器   : $py"
Write-Run "  参数     : $(if ($argsText) { $argsText } else { '（无）' })"
Write-Run "  运行日志 : $RunLog"
Write-Run "============================================================"
Write-Run ""

# 2>&1 是必须的：Python 的 traceback 和 JSONL 日志都走 stderr，不并进来的话
# 崩溃现场只剩一个退出码。
if ($CycleArgs) {
    & $py -m qbg.orchestrator.daily_cycle @CycleArgs 2>&1 | Write-Tee
} else {
    & $py -m qbg.orchestrator.daily_cycle 2>&1 | Write-Tee
}
$rc = $LASTEXITCODE

switch ($rc) {
    0 { Write-Boot "DONE" "正常结束" }
    2 { Write-Boot "WARN" "硬闸中止（exit 2）—— 风控拒绝交易，需要人看" }
    default { Write-Boot "ERROR" "异常退出 exit=$rc（Python 侧应已尝试发送崩溃告警邮件）" }
}

Write-Run ""
Write-Run "============================================================"
Write-Run ("  daily_cycle 退出码: {0}" -f $rc)
Write-Run ("  预检退出码        : {0}" -f $preflightRc)
Write-Run ("  日报              : {0}" -f (Join-Path $ProjectRoot ("reports\daily\{0}.md" -f (Get-Date -Format 'yyyy-MM-dd'))))
Write-Run ("  结构化日志        : {0}" -f (Join-Path $LogDir "qbg.jsonl"))
Write-Run "============================================================"

Stop-All $rc
