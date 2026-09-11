<#
.SYNOPSIS
    计划任务的「过期不补跑」闸。run_daily.ps1 和 scripts\preflight.ps1 共用。

.DESCRIPTION
    Windows 计划任务的 `-StartWhenAvailable`（"错过计划开始时间后尽快启动"）
    **没有截止时间**。于是早上没开机的那天，晚上一开机 Windows 就把 07:45 的
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

    ## 上界管"晚了"，下界管"这是昨天的补跑"

    上界（计划时间 + 窗口）拦不住隔夜补跑：`-ScheduledAt` 写进任务动作的只是
    **时刻，没有日期**。昨天 09:30 那次错过了，今天一开机 Windows 把它补上，
    到达时是 07:45 —— 比今天的 09:30 **早**，于是"没超窗口"，直接放行。
    2026-09-11 实测就是这么跑的。

    所以还要一个下界：**早于当天计划时刻的，只可能是隔夜补跑。**

    这里推翻了本文件早先写的两条理由：

    · "早跑不花钱" —— **不成立**。daily_cycle 确实在 `not_trading_session`
      早退，但 P8 自动复盘在那之后照跑，2026-09-11 那次真的调了 DeepSeek
      （2,212 tokens）。早退不等于免费。

    · "加下界会让 -WithStartupTrigger 失效" —— **不成立**。开机/登录触发器
      真正有用的时刻是**开机晚于计划时间**那天（10:00 开机，触发器 10:05 叫起，
      在下界之上、窗口之内，照常放行）。开机早于计划时间时它叫起的那次本来
      就是多余的 —— 真正的日触发器还在后头。下界只掐掉这种冗余触发。

    留 $EarlyGraceMinutes 分钟的余量：调度器和时钟都有抖动，而隔夜补跑到达时
    是开机时刻，跟计划时刻差着小时级，不会落进这几分钟里。

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
    .PARAMETER EarlyGraceMinutes
        允许比计划时刻早多少分钟启动。再早就判成隔夜补跑。<= 0 视为不设下界。
    .PARAMETER Now
        当前时间，测试用。
    #>
    param(
        [string]$ScheduledAt,
        [int]$WindowMinutes = 120,
        [int]$EarlyGraceMinutes = 5,
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

    # 下界：早于当天计划时刻的，只可能是隔夜补跑（或冗余的开机触发器）。
    if ($EarlyGraceMinutes -gt 0) {
        $earliest = $scheduled.AddMinutes(-$EarlyGraceMinutes)
        if ($Now -lt $earliest) {
            return ("本次启动早于计划时刻：计划 {0}，现在 {1} —— 这是上一次错过的补跑。" -f
                $scheduled.ToString('HH:mm'), $Now.ToString('yyyy-MM-dd HH:mm:ss'))
        }
    }

    $deadline = $scheduled.AddMinutes($WindowMinutes)
    if ($Now -le $deadline) { return $null }

    return ("本次启动已超出窗口：计划 {0}，窗口 {1} 分钟（截止 {2}），现在 {3}。" -f
        $scheduled.ToString('HH:mm'), $WindowMinutes,
        $deadline.ToString('HH:mm'), $Now.ToString('yyyy-MM-dd HH:mm:ss'))
}
