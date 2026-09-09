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
    # 预检任务：比主任务早 15 分钟，**唯一目的是给人留出人工登录同花顺的时间**。
    # 预检本身是幂等的（代理检测到开着就不发热键、同花顺在跑就只报告），
    # 所以 run_daily.ps1 内部那次重跑不会造成任何副作用。
    [ValidatePattern('^\d{1,2}:\d{2}$')]
    [string]$PreflightTime     = '09:15',
    [string]$PreflightTaskName = 'quant_biga_preflight',
    [switch]$NoPreflightTask,
    # 盘前复核任务。**它才是解决"复核太慢"的那一刀。**
    #
    # 2026-09-09 实测：TradingAgents 复核 5 只票花 58 分钟，而
    # require_trading_session 是硬闸 —— 09:30 现场复核会跑到上午盘尾甚至收盘
    # 之后，整天作废且 LLM 的钱已经花掉（当天 $0.23）。
    #
    # 08:00 起跑，给复核留足一个半小时；跑完写 data/reviews/<date>.json，
    # 09:30 的日流程直接读，全程 2 分钟。它**不下单、不碰三把锁**。
    [ValidatePattern('^\d{1,2}:\d{2}$')]
    [string]$PremarketTime     = '08:00',
    [string]$PremarketTaskName = 'quant_biga_premarket',
    [switch]$NoPremarketTask,
    # **默认不加开机/登录触发器。** 2026-08-29 配好自动登录之后，机器 09:00
    # 开机、自动进桌面，于是这个触发器每天都会在 09:16 拉起一次必然早退的运行 ——
    # 而它会占住 MultipleInstances=IgnoreNew 的名额：2026-08-31 那次监听器
    # 把任务钉住到 09:30，真正的日触发器被拒绝（0x800710E0「操作员或系统管理员
    # 拒绝了请求」），**当天一笔单都没下**。
    #
    # 它原本的价值是"到点时机器关着，开机后补跑" —— 而 `-StartWhenAvailable`
    # 已经覆盖了那个场景，不需要额外的触发器。
    [switch]$WithStartupTrigger,
    [switch]$NoStartupTrigger,
    # --- 过期不补跑 ----------------------------------------------------------
    # `-StartWhenAvailable`（"错过计划开始时间后尽快启动"）**没有截止时间**：
    # 早上没开机的那天，晚上一开机 Windows 就把 09:15 的预检和 09:30 的日流程
    # 一起补跑 —— 预检会在晚上打开系统代理、开 TUN、拉起同花顺下单端。
    #
    # 但 `-StartWhenAvailable` 要留着：09:35 才开机那天我们**确实**想补上。
    # 调度器表达不了"只补跑两小时以内的"，脚本可以。所以把计划时间和窗口写进
    # 任务动作，由 scripts\window_guard.ps1 判断这次唤起还算不算数。
    #
    # 120 分钟：09:30 + 2h = 11:30，正好是上午收盘。再晚启动的运行即使跑完也
    # 赶不上有意义的成交，而 daily_cycle 的 session_guard 是硬闸、会在流程跑到
    # 一半才否决 —— 那时 LLM 复核的钱已经花掉了。
    [int]$WindowMinutes = 120,
    # 每天滚动重训（写进 cn_lgb_live，由 load_production_predictions 优先读）。
    # **默认开** —— 不重训的话预测会静默冻结在模型 test 段的最后一天，
    # 而那正是 2026-09-09 查出来的故障。挂在**盘前任务**上（见 PremarketTime）。
    # `-NoRetrain` 可关掉。
    [switch]$NoRetrain,
    # 关掉窗口闸，恢复"任何时候被唤起都跑"的老行为。
    [switch]$NoWindowGuard,
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$Retrain = -not $NoRetrain
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Runner      = Join-Path $ProjectRoot "run_daily.ps1"

function Write-OK($m)   { Write-Host "  [OK] $m"    -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "  [WARN] $m"  -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "  [ERROR] $m" -ForegroundColor Red }

# --- 硬护栏：绝不碰兄弟仓库的任务 -------------------------------------------
# quant-trading 挂在真钱账户上，它的 qtf_daily / qtf_preflight 由那个仓库自己
# 管。手滑传个同名参数就会把人家的任务覆盖掉，而且覆盖是静默的。
# 两个名字都要查。兄弟仓库的任务正好也叫 qtf_daily / qtf_preflight，
# 而 quant-trading 挂在真钱账户上 —— 手滑传个同名参数就会静默覆盖掉人家的。
foreach ($n in @($TaskName, $PreflightTaskName, $PremarketTaskName)) {
    if ($n -match '^(qtf|qtagent)') {
        Write-Err "拒绝操作 '$n' —— qtf_* / qtagent_* 是兄弟仓库的任务，CLAUDE.md 明令禁止动。"
        exit 1
    }
}
if ($TaskName -eq $PreflightTaskName) {
    Write-Err "主任务和预检任务不能同名（都是 '$TaskName'）—— 后注册的会把前一个覆盖掉。"
    exit 1
}
# 预检必须在主任务**之前**跑，否则它存在的意义（留出登录同花顺的时间）就没了。
# [timespan] 而不是 [datetime]：前者不依赖区域设置，"09:30" 恒等于 9 小时 30 分。
$leadMinutes = ([timespan]$Time - [timespan]$PreflightTime).TotalMinutes
if (-not $NoPreflightTask -and $leadMinutes -le 0) {
    Write-Err "预检时间 $PreflightTime 不早于主任务 $Time —— 那就起不到提前准备的作用了。"
    exit 1
}

if ($Remove) {
    $removed = 0
    foreach ($n in @($TaskName, $PreflightTaskName, $PremarketTaskName)) {
        if (Get-ScheduledTask -TaskName $n -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $n -Confirm:$false
            Write-OK "已删除任务 '$n'。"
            $removed++
        } else {
            Write-Warn2 "任务 '$n' 本来就不存在。"
        }
    }
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
#
# RunLevel Limited 是刻意的：同花顺以普通权限跑，Python 也必须是普通权限，
# 否则 UIPI 会静默丢弃模拟输入（CLAUDE.md §七 的第一个坑）。
# 而且定时任务权限越小越好 —— 它每天无人值守地跑。
function New-QbgPrincipal {
    param([string]$Mode)
    if ($Mode -eq 'Background') {
        # S4U = "不管用户是否登录都运行" 且**不存密码**。代价是拿不到网络凭据
        # （访问远程共享会失败），但本流程只走 HTTPS 出网和本地磁盘，够用。
        return New-ScheduledTaskPrincipal `
            -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
    }
    return New-ScheduledTaskPrincipal `
        -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
}

function Register-QbgTask {
    param(
        [string]$Name,
        [string]$Script,
        [string]$At,
        [string]$Description,
        [switch]$WithStartupTrigger,
        [int]$TimeLimitHours = 2
    )
    # 把"本该几点跑"和"迟到多久还算数"写进任务动作本身。任务因此是自描述的：
    # 从任务计划程序里看动作那一行，就知道它的窗口是什么，不用去翻脚本默认值。
    # **手工运行不带这两个参数，所以永远不受窗口限制**（window_guard.ps1）。
    $argLine = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{0}"' -f $Script
    if (-not $NoWindowGuard) {
        $argLine += ' -ScheduledAt "{0}" -WindowMinutes {1}' -f $At, $WindowMinutes
    }
    # **只有日流程任务加 --retrain，预检不加。**
    #
    # 2026-09-09 实测：日流程连着一个月每天选出完全相同的三只票。根因是
    # `latest_date_scores` 取"预测里最后一天"，而静态模型的 test 段止于
    # 2026-08-10 —— 那一天的分数被反复用了 30 天。日流程从不重训，而且
    # 就算重训了，`train(live=True)` 写的 `cn_lgb_live` 当时也没人读。
    #
    # 现在 `load_production_predictions()` 优先读 live、回退静态，所以这里
    # 加上 --retrain 才真正闭环。代价约 2 分钟/天，换来预测每天都是新的。
    # 重训归**盘前**任务。日流程再训一遍是白花 2 分钟 —— 盘前刚训完，
    # 同样的数据同样的 seed，结果逐位相同。
    # 盘前任务没跑（关机/失败）时，日流程仍会读到上一次的 cn_lgb_live，
    # 而它够不够新由 prediction_freshness_guard 判 —— 那才是该管这件事的地方。
    if ($Retrain -and $Name -eq $PremarketTaskName) { $argLine += ' --retrain' }
    $action  = New-ScheduledTaskAction -Execute $Shell -Argument $argLine -WorkingDirectory $ProjectRoot

    $triggers = @(New-ScheduledTaskTrigger -Daily -At $At)
    if ($WithStartupTrigger) {
        if ($Mode -eq 'Background') {
            # 开机触发 + 延迟 5 分钟：给网卡拿到 IP、Clash 起来、磁盘落定留时间。
            # 刚开机就抢跑是最容易出"拉数据超时"的时候。
            $t = New-ScheduledTaskTrigger -AtStartup
            $t.Delay = 'PT5M'
        } else {
            $t = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
            $t.Delay = 'PT3M'
        }
        $triggers += $t
    }

    # StartWhenAvailable：到点时机器关着/睡着的那天，开机后尽快补跑。
    #     **它没有截止时间** —— 晚上才开机也会补跑。截止时间由脚本侧的
    #     window_guard 判（见上面 -WindowMinutes）。分工：调度器负责唤起，
    #     脚本负责判断这次唤起还算不算数。两者缺一：只留调度器 = 晚上乱跑；
    #     只留脚本 = 09:35 开机那天根本不会被唤起。
    # WakeToRun：机器睡着时到点唤醒它（BIOS/电源计划禁用唤醒定时器则无效）。
    # IgnoreNew：上一次还没跑完就不再起第二个 —— run_daily.ps1 里的文件锁是
    #            第二道，这里是第一道，两道都要，因为文件锁挡不住任务本身被
    #            重复排队。
    #
    # **刻意不设 -RestartCount。** 计划任务的"失败后重启"只看退出码非零，
    # 分不清失败的种类，而我们这里非零的多数情况都不该重试：
    #   2   硬闸中止 —— 风控说了今天别交易，重试只会再烧一遍 LLM 复核的钱，
    #       然后得出同一个结论。
    #   127 连解释器都没找到 —— 重试一百次也没有。
    # 预检更是如此：它的退出码 1 表示"有告警但能跑"，那根本不是失败。
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -WakeToRun `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours $TimeLimitHours)

    Register-ScheduledTask -TaskName $Name -Action $action -Trigger $triggers `
        -Settings $settings -Principal (New-QbgPrincipal $Mode) `
        -Description $Description -Force | Out-Null

    # 回读校验：不拿"没抛异常"当成功判据 —— 这条纪律和 ths_client 取表
    # 用哨兵值确认剪贴板真被覆盖是同一条。
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
    if (-not $task) { Write-Err "注册后回读不到任务 '$Name'。"; exit 1 }
    $info = Get-ScheduledTaskInfo -TaskName $Name

    Write-Host ""
    Write-Host "------------------------------------------------------------"
    Write-Host ("  任务名     : {0}" -f $Name)
    Write-Host ("  模式       : {0}（{1}）" -f $Mode, $task.Principal.LogonType)
    Write-Host ("  运行身份   : {0}  RunLevel={1}" -f $task.Principal.UserId, $task.Principal.RunLevel)
    Write-Host ("  命令       : {0}" -f $argLine)
    Write-Host ("  触发器     : {0}" -f (($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ", "))
    Write-Host ("  下次运行   : {0}" -f $info.NextRunTime)
    Write-Host ("  过期窗口   : {0}" -f $(if ($NoWindowGuard) { "已关闭（任何时候被唤起都跑）" }
                                        else { "计划时间后 $WindowMinutes 分钟内有效" }))
    if ($Name -eq $PremarketTaskName) {
        Write-Host ("  每日重训   : {0}" -f $(if ($Retrain) { "开（--retrain，约 +2 分钟）" }
                                            else { "**关** —— 预测会冻结在模型 test 段最后一天" }))
    }
    Write-Host "------------------------------------------------------------"
}

$startupNote = $(if (-not $WithStartupTrigger -or $NoStartupTrigger) { "无开机触发" }
                 elseif ($Mode -eq 'Background') { "开机后 5 分钟" }
                 else { "登录后 3 分钟" })

Write-Host ""
Write-Host "============================================================"
Write-Host " 已注册计划任务"
Write-Host "============================================================"

# --- 预检任务（先注册，它先跑）---------------------------------------------
# **它存在的唯一理由是给人留出登录同花顺的时间。** 预检做的事 run_daily.ps1
# 内部本来也会做一遍，但那时已经 09:30，发现同花顺停在登录框上就来不及了。
# 提前 15 分钟跑一次，窗口就摆在桌面上等你，主流程到点时客户端已经可用。
#
# 重复运行没有副作用：代理/TUN 检测到开着就不发热键（热键是 toggle，
# 发偶数次等于没发），同花顺在跑就只报告，端口在监听就跳过 10s 等待。
if (-not $NoPreflightTask) {
    $preflightScript = Join-Path $ProjectRoot "scripts\preflight.ps1"
    if (-not (Test-Path $preflightScript)) {
        Write-Err "找不到 $preflightScript"
        exit 1
    }
    # 不给预检加开机/登录触发器：登录后 3 分钟跑一次预检没有意义
    # （主任务自己会跑预检），只会多弹一个同花顺窗口。
    # 1 小时上限：预检最慢的一步是等同花顺主窗口（40s），给足余量即可。
    Register-QbgTask -Name $PreflightTaskName -Script $preflightScript -At $PreflightTime `
        -TimeLimitHours 1 `
        -Description ("quant-biga 盘前预检（$Mode 模式；每天 $PreflightTime）。" +
                      "起 Clash + 同花顺、开系统代理/TUN，并留出人工登录同花顺的时间。" +
                      "非交易日会自行跳过。退出码 1 = 有告警但可以跑。")
}

# --- 盘前复核 ---------------------------------------------------------------
# 排在预检**之前**：复核要跑一小时，越早开始越安全。
# run_premarket.ps1 自己会先跑一次预检 —— 复核要调 LLM 中转站，那条链路走
# Clash 代理，而预检 09:15 才开代理。副作用是好的：同花顺 08:00 就被拉起来，
# 人工登录从 15 分钟的窗口变成一个半小时。
if (-not $NoPremarketTask) {
    $premarketScript = Join-Path $ProjectRoot "run_premarket.ps1"
    if (-not (Test-Path $premarketScript)) {
        Write-Err "找不到 $premarketScript"
        exit 1
    }
    # 2 小时上限：复核实测 58 分钟 + 重训 2 分钟，留一倍余量。
    # 窗口闸由 run_premarket.ps1 自己默认成 90 分钟（比日流程的 120 短）——
    # 09:30 之后才被唤起的盘前任务，跑完也赶不上当天的日流程了。
    Register-QbgTask -Name $PremarketTaskName -Script $premarketScript -At $PremarketTime `
        -TimeLimitHours 2 `
        -Description ("quant-biga 盘前复核（$Mode 模式；每天 $PremarketTime）。" +
                      "预检 + 拉数 + 滚动重训 + TradingAgents 逐票复核，" +
                      "结论写 data/reviews/。**不下单、不碰三把锁。** " +
                      "跑飞了日流程只是退回盘中现场复核 —— 慢，但正确。")
}

# --- 主任务 -----------------------------------------------------------------
Register-QbgTask -Name $TaskName -Script $Runner -At $Time `
    -WithStartupTrigger:($WithStartupTrigger -and -not $NoStartupTrigger) `
    -Description ("quant-biga 每日编排（$Mode 模式；$Time + $startupNote）。" +
                  "脚本内部判交易日与当日幂等，重复触发是空转。")

Write-Host ""
if (-not $NoPreflightTask) {
    Write-Host ("时序：{0} 预检（起 Clash + 同花顺）-> 你在这 {1} 分钟内登录同花顺 -> {2} 主流程下单" -f `
        $PreflightTime, [int]$leadMinutes, $Time) -ForegroundColor Cyan
    Write-Host "      没登录也不会出事：取表失败 -> 持仓降级 -> PAPER/LIVE 拒绝下单。" -ForegroundColor Cyan
    Write-Host ""
}

if ($Mode -eq 'Background') {
    Write-Warn2 "Background 模式跑在 session 0，没有桌面。已知代价："
    Write-Warn2 "  · TUN 模式开不了（只能开系统代理，走注册表）"
    Write-Warn2 "  · 同花顺 UI 自动化必定失败 -> 持仓降级 -> PAPER/LIVE 拒绝下单"
    Write-Warn2 "  两样都要的话看 docs/windows-schedule.md 的「自动登录」一节。"
    Write-Host ""
}

Write-Host "验证方式：" -ForegroundColor Cyan
if (-not $NoPreflightTask) {
    Write-Host "  Start-ScheduledTask -TaskName $PreflightTaskName   # 只跑预检"
}
Write-Host "  Start-ScheduledTask -TaskName $TaskName      # 立刻跑一次主流程"
Write-Host "  Get-ScheduledTaskInfo -TaskName $TaskName    # 看上次结果/下次时间"
Write-Host "  Get-Content logs\run_daily.log -Tail 20      # 看启动日志"
Write-Host "  powershell -ExecutionPolicy Bypass -File run_daily.ps1 --dry-run -Pause   # 手工试跑"
