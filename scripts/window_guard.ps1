<#
.SYNOPSIS
    计划任务的「过期不补跑」闸。run_daily.ps1 和 scripts\preflight.ps1 共用。

.DESCRIPTION
    Windows 计划任务的 `-StartWhenAvailable`（"错过计划开始时间后尽快启动"）
    **没有截止时间**。于是早上没开机的那天，晚上一开机 Windows 就把 09:15 的
    预检和 09:30 的日流程一起补跑 —— preflight 会在晚上打开系统代理、开 TUN、
    拉起同花顺下单端，daily_cycle 则空转一遍再发一封"非交易时段"的邮件。
    对使用者来说这就是"我没让它跑，它自己跑了"。

    但 `-StartWhenAvailable` 本身要留着：09:35 才开机的那天，我们**确实**想让
    它补上。调度器表达不了"只补跑两小时以内的"，脚本可以。所以分工是：

        调度器负责**唤起**，脚本负责判断这次唤起**还算不算数**。

    ## 和 session_guard 的分工

    这道闸只看"计划时间 + 窗口"过没过，**不看是不是交易时段** —— 那是
    daily_cycle 里 session_guard 的事。两者不要混：一个管"这次启动是不是一次
    过期的补跑"，一个管"现在能不能交易"。混在一起的话，手工在盘后跑
    `run_daily.ps1 --dry-run` 也会被拦掉，而那是个完全正当的用法。

    ## 为什么只设上界，不设下界

    早于计划时间跑没有副作用：daily_cycle 在开盘前会走 `not_trading_session`
    早退（在拉数和 LLM 复核之前，不花钱），preflight 早一点开代理、早一点拉起
    同花顺也无害。加下界只会让 `-WithStartupTrigger` 那条可选路径失效。

    ## 判据只在被调度器叫起来时生效

    `-ScheduledAt` 是 setup_schedule.ps1 写进任务动作里的。人手工双击或在终端
    里跑时不带这个参数，闸自动让路 —— **手工运行永远不受窗口限制**。
#>

function Get-QbgWindowSkipReason {
    <#
    .SYNOPSIS
        该不该跳过这次运行。返回 $null = 放行；返回字符串 = 跳过的理由。
    .PARAMETER ScheduledAt
        本次运行"本该"在几点开始，HH:mm。空 = 手工运行，不设限。
    .PARAMETER WindowMinutes
        计划时间之后多久内启动仍然算数。<= 0 视为不设限。
    .PARAMETER Now
        当前时间，测试用。
    #>
    param(
        [string]$ScheduledAt,
        [int]$WindowMinutes = 120,
        [datetime]$Now = (Get-Date)
    )

    if ([string]::IsNullOrWhiteSpace($ScheduledAt)) { return $null }
    if ($WindowMinutes -le 0) { return $null }

    # `[string[]]` 不能省。不加的话 `@(...)` 是 object[]，PowerShell 会挑中
    # 单格式那个重载、把数组拼成一个字符串当格式用 —— 于是**任何**时间都解析
    # 失败，闸永远放行。而它只会打一条 Write-Warning，看起来像正常运行。
    $formats = [string[]]@('H:mm', 'HH:mm')
    $parsed = [datetime]::MinValue
    $ok = [datetime]::TryParseExact(
        $ScheduledAt.Trim(), $formats,
        [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::None, [ref]$parsed)
    if (-not $ok) {
        # 解析不了就**放行**并且喊出来。这道闸是便利设施不是安全闸：
        # 一个格式笔误让日流程从此静默不跑，比多跑一次过期补跑严重得多。
        Write-Warning "window_guard: 无法解析 ScheduledAt='$ScheduledAt'，本次不设窗口限制。"
        return $null
    }

    $scheduled = $Now.Date.AddHours($parsed.Hour).AddMinutes($parsed.Minute)
    $deadline  = $scheduled.AddMinutes($WindowMinutes)
    if ($Now -le $deadline) { return $null }

    return ("本次启动已超出窗口：计划 {0}，窗口 {1} 分钟（截止 {2}），现在 {3}。" -f
        $scheduled.ToString('HH:mm'), $WindowMinutes,
        $deadline.ToString('HH:mm'), $Now.ToString('yyyy-MM-dd HH:mm:ss'))
}
