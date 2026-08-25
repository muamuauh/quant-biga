<#
.SYNOPSIS
    注册/更新 quant-biga 的 Windows 计划任务（默认每天 09:30 + 开机后补跑）。

.DESCRIPTION
    两种运行模式，差别只有一处 —— **有没有交互桌面** —— 但那一处决定了
    哪些功能能用：

      -Mode Interactive（默认，对应"我会自己开机并解锁"）
          安全选项 = "只在用户登录时运行"。
          触发器 = 每天 $Time + 登录后 3 分钟。
          ✔ 有真桌面：Clash 热键、TUN、同花顺 UI 自动化全都能用。
          ✔ 不需要管理员权限就能注册。
          ✘ 没人登录时任务直接不跑；锁屏时进程会跑但 UI 自动化仍然失败。

      -Mode Background（备选，对应"开机没解锁也要跑"）
          安全选项 = "不管用户是否登录都运行"（S4U，不存密码）。
          触发器 = 每天 $Time + 开机后 5 分钟。
          ✔ 无人登录、锁屏、刚开机 —— 都照跑。
          ✘ 跑在 session 0，**没有桌面**。于是：
              - Clash 热键（SendKeys）送不到 -> preflight 改走注册表开系统代理；
              - **TUN 开不了**（没有非交互入口，见 preflight.ps1 Step 3 注释）；
              - **同花顺 UI 自动化必定失败** -> 持仓自动降级到 CSV，日报会标注。
          ⚠ 注册 S4U 任务需要管理员权限；本脚本会先检查。

    想两样都要 —— 开机免解锁 **且** 有真桌面 —— 只有一条路：**让 Windows 自动
    登录**（AutoAdminLogon），开机自动进桌面，再用 -Mode Interactive。
    这会把密码写进注册表，是一台挂着真钱账户的机器上的安全权衡，所以本脚本
    **不替你改**，办法写在 docs/windows-schedule.md。

    为什么加"开机"/"登录"触发器不会重复跑：daily_cycle 有当日幂等标记
    （already_completed_today）和交易日闸，已经跑过的当天再触发就是一次几秒的
    空转。真正的价值是：09:30 时机器关着/睡着的那天，开机后能自动补上。
    -StartWhenAvailable 也是为这个。

.PARAMETER Mode
    Interactive（默认）或 Background，语义见上。

.PARAMETER Time
    每日触发时间，HH:mm。默认 09:30 —— 开盘。

    **为什么不是盘前的 07:30。** risk_limits.yaml 里 require_trading_session
    现在是 true（自动下单必须开），而它是**硬闸**：07:30 跑的话 session_guard
    直接不过，hard_ok=False，当天什么都不做，日报写"硬闸中止"。
    盘前出清单给人工执行的那套用 07:30 是对的；自动下单必须在盘中。

    时序：09:30 触发 -> 预检（起 Clash/同花顺、开代理）+ 拉数 + 打分 +
    LLM 逐票复核，订单大约 09:40-09:50 落到券商。想更早进场就得把
    LLM 复核关掉（QBG_AGENTS_ENABLED=0），那是另一个取舍。

    ⚠ 不要为了"早点成交"把时间提到 09:30 之前 —— 硬闸会在流程跑到一半时
    判定非交易时段，整天作废，而且失败得很晚。

.PARAMETER TaskName
    任务名，默认 quant_biga_daily。**不允许**叫 qtf_* 或 qtagent_*：
    那是兄弟仓库的任务，CLAUDE.md 明令禁止动。

.PARAMETER Remove
    删除该任务后退出。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1
.EXAMPLE
    # 无人值守模式，需要管理员 PowerShell
    powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1 -Mode Background
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1 -Remove
#>

[CmdletBinding()]
param(
    # 默认 Interactive：2026-08-22 用户确认「我会自己开机并且解锁」，
    # 那就用有真桌面的模式 —— Clash 热键、TUN、同花顺 UI 自动化才全都能用。
    # Background 保留给「无人值守也要跑」的场景，代价见上面的 .DESCRIPTION。
    [ValidateSet('Interactive', 'Background')]
    [string]$Mode     = 'Interactive',
    # 09:30 = 开盘。自动下单要求 require_trading_session=true，而那是硬闸，
    # 盘前跑会被它一票否决（理由见上面的 .PARAMETER Time）。
    [ValidatePattern('^\d{1,2}:\d{2}$')]
    [string]$Time     = '09:30',
    [string]$TaskName = 'quant_biga_daily',
    [switch]$NoStartupTrigger,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner      = Join-Path $ProjectRoot "run_daily.ps1"

function Write-OK($m)   { Write-Host "  [OK] $m"    -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "  [WARN] $m"  -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "  [ERROR] $m" -ForegroundColor Red }

# --- 硬护栏：绝不碰兄弟仓库的任务 -------------------------------------------
# quant-trading 挂在真钱账户上，它的 qtf_daily / qtf_preflight 由那个仓库自己
# 管。手滑传个同名参数就会把人家的任务覆盖掉，而且覆盖是静默的。
if ($TaskName -match '^(qtf|qtagent)') {
    Write-Err "拒绝操作 '$TaskName' —— qtf_* / qtagent_* 是兄弟仓库的任务，CLAUDE.md 明令禁止动。"
    exit 1
}

if ($Remove) {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $existing) { Write-Warn2 "任务 '$TaskName' 本来就不存在。"; exit 0 }
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-OK "已删除任务 '$TaskName'。"
    exit 0
}

if (-not (Test-Path $Runner)) {
    Write-Err "找不到 $Runner"
    exit 1
}

# --- 选宿主 shell -----------------------------------------------------------
# 优先真实路径的 pwsh，但**跳过 WindowsApps 下的那个** —— 那是应用执行别名
# （一个零字节重解析点），只在有用户 profile 的交互会话里解析得开；
# session 0 的计划任务调它会直接"找不到文件"，而且错误信息毫无提示性。
# 兜底用 System32 的 powershell.exe：它永远在，永远能起。
function Resolve-Shell {
    $pwshCmd = Get-Command pwsh.exe -ErrorAction SilentlyContinue
    if ($pwshCmd -and $pwshCmd.Source -and ($pwshCmd.Source -notmatch 'WindowsApps')) {
        return $pwshCmd.Source
    }
    foreach ($p in @("C:\Program Files\PowerShell\7\pwsh.exe")) {
        if (Test-Path $p) { return $p }
    }
    return (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe")
}
$Shell = Resolve-Shell

# --- 权限检查 ---------------------------------------------------------------
$isAdmin = ([Security.Principal.WindowsPrincipal] `
            [Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if ($Mode -eq 'Background' -and -not $isAdmin) {
    Write-Err "注册 -Mode Background（不管用户是否登录）需要管理员权限。"
    Write-Err "请用「以管理员身份运行」的 PowerShell 重跑，或改用 -Mode Interactive。"
    exit 1
}

# --- 组装任务 ---------------------------------------------------------------
# -NoProfile：不加载任何 profile，计划任务环境要可复现。
# -NonInteractive：明确声明没有交互，避免任何 Read-Host 把任务挂死等到超时。
$argLine = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $Runner
$action  = New-ScheduledTaskAction -Execute $Shell -Argument $argLine -WorkingDirectory $ProjectRoot

$triggers = @()
$triggers += New-ScheduledTaskTrigger -Daily -At $Time

if (-not $NoStartupTrigger) {
    if ($Mode -eq 'Background') {
        # 开机触发 + 延迟 5 分钟：给网卡拿到 IP、Clash 起来、磁盘落定留时间。
        # 刚开机就抢跑是最容易出"拉数据超时"的时候。
        $t = New-ScheduledTaskTrigger -AtStartup
        $t.Delay = 'PT5M'
        $triggers += $t
    } else {
        $t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        $t.Delay = 'PT3M'
        $triggers += $t
    }
}

# StartWhenAvailable：09:30 时机器关着/睡着的那天，开机后尽快补跑。
# WakeToRun：机器睡着时到点唤醒它（BIOS/电源计划禁用唤醒定时器则无效）。
# IgnoreNew：上一次还没跑完就不再起第二个 —— run_daily.ps1 里的文件锁是第二道，
#            这里是第一道，两道都要，因为文件锁挡不住任务本身被重复排队。
# ExecutionTimeLimit 2h：--retrain 那条路径最慢（三 seed 串行），2 小时足够，
#            又能保证卡死的任务不会一直占着锁到第二天。
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2)

# **刻意不设 -RestartCount。** 计划任务的"失败后重启"只看退出码非零，
# 分不清失败的种类，而我们这里非零的多数情况都不该重试：
#   2   硬闸中止 —— 风控说了今天别交易，重试只会再烧一遍 LLM 复核的钱，
#       然后得出同一个结论。
#   127 连解释器都没找到 —— 重试一百次也没有。
# 真正值得重试的只有网络抖动，而那种情况 daily_cycle 内部已经有降级和
# fail-open，再加上当日幂等标记，外层重试拿不到什么。

# RunLevel Limited 是刻意的：同花顺以普通权限跑，Python 也必须是普通权限，
# 否则 UIPI 会静默丢弃模拟输入（CLAUDE.md §七 的第一个坑）。
# 而且定时任务权限越小越好 —— 它每天无人值守地跑。
if ($Mode -eq 'Background') {
    # S4U = "不管用户是否登录都运行" 且**不存密码**。代价是拿不到网络凭据
    # （访问远程共享会失败），但本流程只走 HTTPS 出网和本地磁盘，够用。
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
}

$desc = "quant-biga 每日编排（$Mode 模式；$Time + " +
        $(if ($NoStartupTrigger) { "无开机触发" } elseif ($Mode -eq 'Background') { "开机后 5 分钟" } else { "登录后 3 分钟" }) +
        "）。脚本内部判交易日与当日幂等，重复触发是空转。"

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers `
    -Settings $settings -Principal $principal -Description $desc -Force | Out-Null

# --- 回读校验 ---------------------------------------------------------------
# 不拿"没抛异常"当成功判据 —— 这条纪律和 ths_client 取表用哨兵值是同一条。
$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) { Write-Err "注册后回读不到任务 '$TaskName'。"; exit 1 }
$info = Get-ScheduledTaskInfo -TaskName $TaskName

Write-Host ""
Write-Host "============================================================"
Write-Host " 已注册计划任务"
Write-Host "============================================================"
Write-Host ("  任务名     : {0}" -f $TaskName)
Write-Host ("  模式       : {0}（{1}）" -f $Mode, $task.Principal.LogonType)
Write-Host ("  运行身份   : {0}  RunLevel={1}" -f $task.Principal.UserId, $task.Principal.RunLevel)
Write-Host ("  宿主 shell : {0}" -f $Shell)
Write-Host ("  命令       : {0}" -f $argLine)
Write-Host ("  触发器     : {0}" -f (($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ", "))
Write-Host ("  下次运行   : {0}" -f $info.NextRunTime)
Write-Host "============================================================"
Write-Host ""

if ($Mode -eq 'Background') {
    Write-Warn2 "Background 模式跑在 session 0，没有桌面。已知代价："
    Write-Warn2 "  · TUN 模式开不了（只能开系统代理，走注册表）"
    Write-Warn2 "  · 同花顺 UI 自动化必定失败 -> 持仓降级到 CSV（日报会标注）"
    Write-Warn2 "  两样都要的话看 docs/windows-schedule.md 的「自动登录」一节。"
    Write-Host ""
}

Write-Host "验证方式：" -ForegroundColor Cyan
Write-Host "  Start-ScheduledTask -TaskName $TaskName      # 立刻跑一次"
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName    # 看上次结果/下次时间"
Write-Host "  Get-Content logs\run_daily.log -Tail 20      # 看启动日志"
Write-Host "  powershell -ExecutionPolicy Bypass -File run_daily.ps1 --dry-run -Pause   # 手工试跑"
