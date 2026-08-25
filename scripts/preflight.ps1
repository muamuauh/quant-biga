<#
.SYNOPSIS
    quant-biga 日流程前置检查：把 Clash Verge（系统代理 + TUN）和 qbg 环境
    准备好，再让 run_daily.ps1 跑真正的流程。

.DESCRIPTION
    步骤：
      0. 交易日闸 —— 非交易日直接退出 0，不必叫醒 Clash。
      1. Clash Verge 进程在不在（不在就起，仅限交互会话，见下）。
      2. 等网络稳定 + 探一下混合端口。
      3. 系统代理 + TUN：**先检测真实的 OS 状态**，只有确实关着才去开。
      4. qbg 环境自检：解释器在不在、import 得动不动、（可选）同花顺预检。

    和 quant-trading/scripts/preflight.ps1 的三处关键差异：

    一、**代理不是硬依赖**。本项目行情走 BaoStock（境内直连），只有 P7/P8 的
        LLM 中转站需要出墙，而那两条链路本来就是 fail-open。所以代理/TUN 开不
        起来只告警，不阻断当日清单 —— 退出码 1 表示"能跑但有告警"，2 才是致命。

    二、**会话感知**。热键靠 SendKeys，只能送到**同一个交互会话里**的 Clash。
        计划任务勾了"不管用户是否登录"时跑在 session 0，桌面锁着时输入也进不去，
        这两种情况下 SendKeys 都是**静默失败**（不抛异常、就是没反应，和同花顺
        那个 UIPI 坑一模一样）。所以先判会话，判不过就不发热键，改走注册表。

    三、**注册表兜底带端口守卫**。非交互会话里改注册表是唯一能开系统代理的路，
        但如果混合端口没在监听就把系统代理指过去，**整机 HTTP 断网**，而且表现
        是"网坏了"，没人会往这个脚本上想。所以只有探到端口活着才写注册表。
        TUN 没有等价兜底（要 Clash 的外部控制器），只告警。

.PARAMETER SkipProxyTun
    完全跳过代理/TUN 这一步。

.PARAMETER SkipMarketCheck
    跳过交易日闸，强制往下走。

.PARAMETER SkipThsCheck
    跳过同花顺预检（本来也只在 QBG_PORTFOLIO_SOURCE=easytrader 时才做）。

.OUTPUTS
    退出码：0 = 全绿；1 = 有告警但可以跑；2 = 致命（解释器都没有）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1
#>

param(
    # Clash Verge 可执行文件。本机 2026-08-22 实测路径。
    [string]$ClashExe       = "D:\Clash Verge\clash-verge.exe",
    [string]$ClashProcName  = "clash-verge",
    # 混合端口。取自 verge.yaml 的 verge_mixed_port，那边改了这里也要改。
    [int]   $ProxyPort      = 7897,
    # 全局热键，WScript.Shell SendKeys 语法：^=Ctrl %=Alt +=Shift。
    # 必须和 Clash Verge -> 设置 -> 热键设置里的绑定一致，否则热键打空。
    # 本机 verge.yaml 实际是 toggle_system_proxy=CTRL+ALT+SHIFT+P、
    # toggle_tun_mode=CTRL+ALT+SHIFT+T，下面两个默认值就是它们的 SendKeys 写法。
    [string]$ProxyHotkey    = "^%+p",
    [string]$TunHotkey      = "^%+t",
    # 起完 Clash 等多久让网络落定。10s 是参考仓库实测够用的值。
    [int]   $NetworkWaitSec = 10,
    # qbg 环境解释器。用绝对路径而不是 conda activate：计划任务不加载任何
    # profile，conda 的 shell 函数在那里根本不存在。空 = 按下面的候选表去找。
    [string]$PythonExe      = "",
    # 同花顺下单端。UI 自动化（P9）必须和它同机、**同一个交互会话**，
    # 而且两边权限相同（CLAUDE.md 第七节的 UIPI 坑）。
    [string]$ThsExe         = "D:\tonghuashun\同花顺\xiadan.exe",
    [string]$ThsProcName    = "xiadan",
    # 启动后等主窗口出现的上限。同花顺冷启动 + 连行情比较慢。
    [int]   $ThsWaitSec     = 40,
    [switch]$SkipMarketCheck,
    [switch]$SkipProxyTun,
    [switch]$SkipThsCheck,
    # 持仓源不是 easytrader 时也照样拉起同花顺（手工调试用）。
    [switch]$ForceLaunchThs
)

$ErrorActionPreference = "Continue"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

# ---------------------------------------------------------------------------
# 所有输出都走这里：既上屏（带颜色，手工跑时好读），又落盘到 QBG_RUN_LOG。
#
# **必须自己写文件，不能指望调用方用管道接住。** PowerShell 5.1 的 Write-Host
# 直接写宿主，**不进管道**（information 流是 6+ 才有的），而计划任务里那个
# 宿主的输出是被丢弃的 —— 不自己落盘就等于没有日志。
# run_daily.ps1 会在调用前把当天的运行日志路径塞进 QBG_RUN_LOG；
# 手工直接跑这个脚本时该变量为空，就只上屏，不产生多余文件。
# ---------------------------------------------------------------------------
function Write-Line {
    param([Parameter(Position = 0)][string]$Message = "", [string]$ForegroundColor)
    if ($ForegroundColor) {
        Microsoft.PowerShell.Utility\Write-Host $Message -ForegroundColor $ForegroundColor
    } else {
        Microsoft.PowerShell.Utility\Write-Host $Message
    }
    if ($env:QBG_RUN_LOG) {
        try { Add-Content -LiteralPath $env:QBG_RUN_LOG -Value $Message -Encoding UTF8 } catch { }
    }
}

function Write-Step($m) { Write-Line "==== $m ====" -ForegroundColor Cyan }
function Write-OK($m)   { Write-Line "  [OK] $m"    -ForegroundColor Green }
function Write-Warn2($m){ Write-Line "  [WARN] $m"  -ForegroundColor Yellow }
function Write-Err($m)  { Write-Line "  [ERROR] $m" -ForegroundColor Red }

$script:WarnCount = 0
function Add-Warn($m) { $script:WarnCount++; Write-Warn2 $m }

# --- 找解释器 ---------------------------------------------------------------
function Resolve-Python {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit)       { $candidates += $Explicit }
    if ($env:QBG_PYTHON) { $candidates += $env:QBG_PYTHON }
    $candidates += (Join-Path $env:USERPROFILE ".conda\envs\qbg\python.exe")
    $candidates += "C:\ProgramData\miniconda3\envs\qbg\python.exe"
    $candidates += "C:\ProgramData\Anaconda3\envs\qbg\python.exe"
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

# --- 会话性质 ---------------------------------------------------------------
# SendKeys 能不能送到 Clash，取决于四件事，缺一不可：
#   1. 我们不在 session 0（勾了"不管用户是否登录"的任务就跑在 session 0）
#   2. 本会话有桌面（explorer 在同一会话里）
#   3. 桌面没锁（LogonUI.exe 在跑 = 锁屏/登录界面占着输入桌面）
#   4. Clash 和我们在**同一个会话**（跨会话的键盘输入直接被丢弃）
# 任何一条不满足，SendKeys 都是静默失败。所以必须先判、判不过就别发，
# 否则日志上写着"已发送热键"，实际什么都没发生 —— 那种日志比没有还糟。
function Get-SessionFacts {
    param([string]$ClashProcName)
    $sid = (Get-Process -Id $PID).SessionId
    $facts = [ordered]@{
        SessionId    = $sid
        InSession0   = ($sid -eq 0)
        HasDesktop   = $false
        Locked       = $false
        ClashSession = $null
    }
    $facts.HasDesktop = [bool](Get-Process -Name explorer -ErrorAction SilentlyContinue |
                               Where-Object { $_.SessionId -eq $sid })
    $facts.Locked     = [bool](Get-Process -Name LogonUI -ErrorAction SilentlyContinue)
    $clash = Get-Process -Name $ClashProcName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($clash) { $facts.ClashSession = $clash.SessionId }
    return $facts
}

function Test-CanSendKeys($facts) {
    if ($facts.InSession0)             { return $false }
    if (-not $facts.HasDesktop)        { return $false }
    if ($facts.Locked)                 { return $false }
    if ($null -eq $facts.ClashSession) { return $false }
    return ($facts.ClashSession -eq $facts.SessionId)
}

# --- 端口探测 ---------------------------------------------------------------
function Test-Port([string]$h, [int]$p, [int]$timeoutMs = 2000) {
    $c = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $c.BeginConnect($h, $p, $null, $null)
        $ok  = $iar.AsyncWaitHandle.WaitOne($timeoutMs, $false)
        if ($ok -and $c.Connected) { $c.EndConnect($iar); return $true }
        return $false
    } catch { return $false } finally { $c.Close() }
}

# --- 代理 / TUN 状态检测 -----------------------------------------------------
# 系统代理算开 iff 注册表 ProxyEnable=1 **且** ProxyServer 指向 Clash 的端口。
# 后半句不能省：别的软件也会开系统代理，认错了就把它当成 Clash 已就绪。
$script:ProxyReg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"

function Test-SystemProxyOn([int]$port) {
    try {
        $p = Get-ItemProperty -Path $script:ProxyReg -ErrorAction Stop
        return ($p.ProxyEnable -eq 1) -and ($p.ProxyServer -match ":$port(;|$|\b)")
    } catch { return $false }
}

# TUN 开着才会有那块虚拟网卡。认**驱动描述**比认名字稳（本机这块叫 Meta，
# 换个内核就可能改名），名字匹配只作为兜底。
function Test-TunOn {
    try {
        $a = Get-NetAdapter -ErrorAction Stop | Where-Object {
            $_.Status -eq 'Up' -and (
                $_.InterfaceDescription -like '*Wintun*' -or
                $_.InterfaceDescription -like '*Meta Tunnel*' -or
                $_.Name -match 'Meta|Mihomo|Clash|Tun'
            )
        }
        return [bool]$a
    } catch { return $false }
}

# 热键开某个功能：检测到关着才发，最多发两次。
# 发偶数次等于没发，所以就算上面的检测函数写错了，也不会把本来开着的功能关掉。
function Enable-ByHotkey {
    param([string]$Label, [scriptblock]$Test, [string]$Hotkey, [int]$SettleSec = 3)
    if ([string]::IsNullOrWhiteSpace($Hotkey)) {
        Add-Warn "$Label 关着，且没配热键 —— 去 Clash Verge 手动开。"
        return $false
    }
    for ($i = 1; $i -le 2; $i++) {
        Write-Line "  $Label 关着 -> 发热键 '$Hotkey'（第 $i/2 次）"
        try {
            (New-Object -ComObject WScript.Shell).SendKeys($Hotkey)
        } catch {
            Add-Warn "SendKeys 失败：$($_.Exception.Message)"
            return $false
        }
        Start-Sleep -Seconds $SettleSec
        if (& $Test) { Write-OK "$Label 已打开。"; return $true }
    }
    Add-Warn "$Label 发完热键仍是关的。核对 Clash Verge 热键设置是否为 '$Hotkey'。"
    return $false
}

# 非交互兜底：直接写注册表开系统代理。
# **端口守卫是必需的**，理由见文件头 .DESCRIPTION 第三条。
function Enable-ProxyByRegistry([int]$port) {
    if (-not (Test-Port "127.0.0.1" $port 1500)) {
        Add-Warn "127.0.0.1:$port 没在监听，拒绝写注册表（指向死端口会让整机 HTTP 断网）。"
        return $false
    }
    try {
        Set-ItemProperty -Path $script:ProxyReg -Name ProxyServer -Value "127.0.0.1:$port" -ErrorAction Stop
        Set-ItemProperty -Path $script:ProxyReg -Name ProxyEnable -Value 1 -Type DWord -ErrorAction Stop
    } catch {
        Add-Warn "写注册表失败：$($_.Exception.Message)"
        return $false
    }
    # 广播 WinINET 设置变更，让已经在跑的进程也能立刻用上。
    # 拿不到这个 API 不算失败：我们马上要起的 python 是新进程，本来就读最新值。
    try {
        $sig = '[DllImport("wininet.dll", SetLastError = true, CharSet = CharSet.Auto)]' + "`n" +
               'public static extern bool InternetSetOption(IntPtr hInternet, int dwOption, IntPtr lpBuffer, int dwBufferLength);'
        $w = Add-Type -MemberDefinition $sig -Name WinInet -Namespace Qbg -PassThru -ErrorAction Stop
        $w::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0) | Out-Null  # SETTINGS_CHANGED
        $w::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0) | Out-Null  # REFRESH
    } catch { }
    if (Test-SystemProxyOn $port) {
        Write-OK "系统代理已由注册表打开（Clash Verge 界面上的开关不会同步显示）。"
        return $true
    }
    Add-Warn "写完注册表回读仍是关的。"
    return $false
}

# --- 持仓源 -----------------------------------------------------------------
# 环境变量优先，其次读 .env。**只取这一个键**，不解析整个文件 ——
# .env 里还有 API key 和 SMTP 密码，没有任何理由把它们读进这个脚本。
function Get-PortfolioSource {
    param([string]$Root)
    if ($env:QBG_PORTFOLIO_SOURCE) { return $env:QBG_PORTFOLIO_SOURCE.ToLower() }
    $envFile = Join-Path $Root ".env"
    if (Test-Path $envFile) {
        $m = Select-String -Path $envFile -Pattern '^\s*QBG_PORTFOLIO_SOURCE\s*=\s*(\S+)' `
             -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($m) { return $m.Matches[0].Groups[1].Value.ToLower() }
    }
    return ""
}

Write-Line ""
Write-Line "============================================================"
Write-Line " quant-biga preflight: Clash Verge 代理/TUN + 同花顺 + qbg 环境"
Write-Line "============================================================"

$py = Resolve-Python $PythonExe

# --- 0/4 交易日闸 -----------------------------------------------------------
# 非交易日不必叫醒 Clash。判据取 00_market_check.py 打印的 trading_day 字段，
# 而**不是**它的退出码 —— 退出码 1 既可能是"休市"，也可能是脚本自己崩了，
# 拿它当判据会让一次 import 错误安静地跳过一个真正的交易日。
# 只有明确读到 False 才跳过；读不懂就往下走（宁可多跑一次，不可漏跑一天）。
if (-not $SkipMarketCheck) {
    Write-Step "Step 0/5: 今天是交易日吗"
    $check = Join-Path $ProjectRoot "scripts\00_market_check.py"
    if ($py -and (Test-Path $check)) {
        $out = (& $py $check 2>&1 | Out-String)
        Write-Line ("  " + $out.Trim())
        if ($out -match "'trading_day':\s*False") {
            Write-Line "[SKIP] 今天不是 A 股交易日 —— 不启动 Clash，直接结束。" -ForegroundColor Yellow
            exit 0
        }
        if ($out -match "'trading_day':\s*True") {
            Write-OK "交易日，继续预检。"
        } else {
            Add-Warn "交易日检查输出读不懂，按交易日继续（宁可多跑，不可漏跑）。"
        }
    } else {
        Add-Warn "交易日检查跳过（找不到解释器或 00_market_check.py）。"
    }
}

$facts  = Get-SessionFacts $ClashProcName
$canSend = Test-CanSendKeys $facts
Write-Line ("  会话 #{0}{1}{2}" -f $facts.SessionId,
    $(if ($facts.HasDesktop) { "，有桌面" } else { "，无桌面" }),
    $(if ($facts.Locked) { "，锁屏中" } else { "" }))

# --- 1/4 Clash Verge --------------------------------------------------------
Write-Step "Step 1/5: Clash Verge 进程"
$clashOk = [bool](Get-Process -Name $ClashProcName -ErrorAction SilentlyContinue)
if ($clashOk) {
    Write-OK "Clash Verge 已在运行（会话 #$($facts.ClashSession)）。"
} elseif ($facts.InSession0 -or -not $facts.HasDesktop) {
    # 在 session 0 里 Start-Process 一个托盘 GUI：进程会起来，但没有可用桌面和
    # 托盘，全局热键监听器也不工作 —— 起了等于没起，还会和用户登录后自己那份
    # 打架（两个内核抢 7897）。所以这里明确不起。
    Add-Warn "Clash Verge 没在跑，且当前会话没有桌面 —— 不去启动它（session 0 里起托盘程序等于没起）。"
    Add-Warn "  根治：Clash Verge -> 设置 -> 开机自启（verge.yaml 的 enable_auto_launch，本机当前 false）。"
} elseif (-not (Test-Path $ClashExe)) {
    Add-Warn "Clash Verge 没在跑，而且找不到可执行文件：$ClashExe"
} else {
    Write-Line "  Clash Verge 没在跑 -> 启动：$ClashExe"
    try { Start-Process -FilePath $ClashExe | Out-Null } catch { Add-Warn "启动失败：$($_.Exception.Message)" }
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Seconds 1
        if (Get-Process -Name $ClashProcName -ErrorAction SilentlyContinue) { $clashOk = $true; break }
    }
    if ($clashOk) {
        Write-OK "Clash Verge 已启动。"
        $facts   = Get-SessionFacts $ClashProcName
        $canSend = Test-CanSendKeys $facts
    } else {
        Add-Warn "Clash Verge 启动了但 20s 内没看到进程。"
    }
}

# --- 2/4 等网络落定 ---------------------------------------------------------
Write-Step "Step 2/5: 等 ${NetworkWaitSec}s 让网络落定"
Start-Sleep -Seconds $NetworkWaitSec
if (Test-Port "127.0.0.1" $ProxyPort) {
    Write-OK "混合端口 $ProxyPort 在监听。"
} else {
    Add-Warn "混合端口 $ProxyPort 探不通（Clash 可能还没起完，或端口改了）。"
}

# --- 3/4 系统代理 + TUN -----------------------------------------------------
Write-Step "Step 3/5: 系统代理 + TUN"
$proxyOn = $false
$tunOn   = $false
if ($SkipProxyTun) {
    Write-Warn2 "按 -SkipProxyTun 跳过。"
} else {
    $proxyOn = Test-SystemProxyOn $ProxyPort
    $tunOn   = Test-TunOn
    if ($proxyOn) { Write-OK "系统代理已开（-> 127.0.0.1:$ProxyPort）。" }
    if ($tunOn)   { Write-OK "TUN 虚拟网卡在线。" }

    if (-not $proxyOn) {
        if ($canSend) {
            $proxyOn = Enable-ByHotkey "系统代理" { Test-SystemProxyOn $ProxyPort } $ProxyHotkey 3
        } else {
            Write-Line "  当前会话发不了热键 -> 走注册表兜底"
            $proxyOn = Enable-ProxyByRegistry $ProxyPort
        }
    }
    if (-not $tunOn) {
        if ($canSend) {
            $tunOn = Enable-ByHotkey "TUN 模式" { Test-TunOn } $TunHotkey 4
        } else {
            # TUN 没有注册表等价物：它由 Clash 内核 + clash-verge-service 拉起。
            # 非交互会话下唯一的自动化入口是 mihomo 的外部控制器（PATCH /configs），
            # 而本机 verge.yaml 里 enable_external_controller 现在是 false。
            # 所以这里只能如实告警，不假装做了什么。
            Add-Warn "TUN 关着，且当前会话发不了热键。要让它在无人登录时也自动开，"
            Add-Warn "  只能在 Clash Verge 里开「外部控制器」，或让机器自动登录（见 docs/windows-schedule.md）。"
        }
    }
}

# --- 4/5 同花顺下单端 -------------------------------------------------------
# **只在持仓源真的是 easytrader 时才拉它起来。** 别的源用不到下单端，
# 平白开一个连着真钱/模拟账户的交易终端没有道理。
#
# 三件事这个脚本做不到，必须写清楚，否则日志会给人"已就绪"的错觉：
#   1. **登录要人工做。** 起来的是登录框，不是可用的交易界面。同花顺没配
#      自动登录的话，后面 easytrader 取表会失败 -> 持仓降级 -> PAPER 模式
#      按 broker_refusal 拒绝下单（这是对的，但你得知道为什么没下单）。
#   2. **不能用管理员起。** 同花顺以管理员跑、Python 普通权限时，UIPI 会
#      静默丢弃所有模拟输入：找得到窗口、不报错、就是不动。
#   3. **session 0 里起 GUI 等于没起。** 没有可用桌面，窗口句柄拿不到。
$thsLaunched = $false
$thsSource   = Get-PortfolioSource $ProjectRoot
$wantThs     = $ForceLaunchThs -or ($thsSource -eq "easytrader")
Write-Step "Step 4/5: 同花顺下单端"
if ($SkipThsCheck) {
    Write-Warn2 "按 -SkipThsCheck 跳过。"
} elseif (-not $wantThs) {
    Write-Line "  持仓源是 '$thsSource'，用不到下单端 —— 不启动（要强开加 -ForceLaunchThs）。"
} else {
    $proc = Get-Process -Name $ThsProcName -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($proc) {
        $thsLaunched = $true
        Write-OK "同花顺下单端已在运行（PID $($proc.Id)，会话 #$($proc.SessionId)）。"
        if ($proc.SessionId -ne $facts.SessionId) {
            # 跨会话 = UI 自动化必定失败，和跨会话发热键是同一个道理。
            Add-Warn "它在会话 #$($proc.SessionId)，我们在 #$($facts.SessionId) —— 跨会话操作不了它。"
        }
    } elseif ($facts.InSession0 -or -not $facts.HasDesktop) {
        Add-Warn "同花顺没在跑，且当前会话没有桌面 —— 不去启动它（session 0 里起 GUI 等于没起）。"
        Add-Warn "  持仓会降级；PAPER/LIVE 模式下会因此拒绝下单。"
    } elseif (-not (Test-Path $ThsExe)) {
        Add-Warn "同花顺没在跑，而且找不到可执行文件：$ThsExe"
    } else {
        Write-Line "  同花顺没在跑 -> 启动：$ThsExe"
        try {
            # 刻意**不加** -Verb RunAs：提权会触发 UIPI，让后续所有模拟输入
            # 被静默丢弃。工作目录设成程序所在目录，同花顺要在那里找配置。
            Start-Process -FilePath $ThsExe -WorkingDirectory (Split-Path -Parent $ThsExe) | Out-Null
        } catch {
            Add-Warn "启动失败：$($_.Exception.Message)"
        }
        for ($i = 0; $i -lt $ThsWaitSec; $i++) {
            Start-Sleep -Seconds 1
            $proc = Get-Process -Name $ThsProcName -ErrorAction SilentlyContinue |
                    Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
            if ($proc) { $thsLaunched = $true; break }
        }
        if ($thsLaunched) {
            Write-OK "同花顺主窗口已出现（${i}s）：'$($proc.MainWindowTitle)'"
        } else {
            Add-Warn "等了 ${ThsWaitSec}s 没等到同花顺主窗口。"
        }
    }
    # 认窗口标题：登录框和交易界面都有窗口句柄，只看"有没有句柄"会把
    # 停在登录框上的客户端当成已就绪。取表真正需要的是这个标题的那个窗口。
    if ($thsLaunched -and $proc -and $proc.MainWindowTitle -notmatch '网上股票交易系统') {
        Add-Warn "窗口标题是 '$($proc.MainWindowTitle)'，不是「网上股票交易系统5.0」——"
        Add-Warn "  多半停在登录框上。**登录要人工做**，没登录后面取表会失败。"
    }
}

# --- 4/4 qbg 环境 -----------------------------------------------------------
Write-Step "Step 5/5: qbg 环境"
$pyOk = $false
if (-not $py) {
    Write-Err "找不到 qbg 环境的解释器。设 QBG_PYTHON 指向 python.exe，或先建环境："
    Write-Err "  conda env create -f environment.yml"
} else {
    Write-OK "解释器：$py"
    & $py -c "import qbg" 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-OK "import qbg 正常。"
        $pyOk = $true
    } else {
        Write-Err "import qbg 失败 —— 包没装上（pip install -e '.[data,model,llm]'）。"
    }

    # 时区自检：真正决定"今天是哪天"的是 Python 看到的本地时区。
    # 只告警不中止 —— 07:30 跑的话就算时区错成 UTC 也还在同一天；
    # 会真出事的是北京时 00:00-08:00 之间的手动补跑，那时 UTC 还停在前一天。
    $tz = (& $py -c "import datetime;print(datetime.datetime.now().astimezone().strftime('%z'))" 2>&1 | Out-String).Trim()
    if ($tz -ne "+0800") {
        Add-Warn "Python 本地时区是 UTC$tz，不是 +0800 —— 凌晨补跑会算错日期。"
    }
}

# 同花顺预检：只在持仓源真的是 easytrader 时才做，而且**只告警**。
# source.py 有 qbg_ths_fallback_to_csv 降级到 CSV，降级原因会一路进日报，
# 所以没必要为它拦下整个流程。
$thsChecked = $false
if (-not $SkipThsCheck -and $pyOk -and $wantThs) {
    Write-Line "  持仓源是 easytrader -> 跑同花顺预检"
    $thsChecked = $true
    if (-not $canSend) {
        Add-Warn "当前会话无可用桌面/锁屏中 —— 同花顺 UI 自动化必定失败，持仓会降级到 CSV。"
    }
    & $py (Join-Path $ProjectRoot "tools\probe_ths.py") --preflight-only 2>&1 |
        ForEach-Object { Write-Line "    $_" }
    if ($LASTEXITCODE -ne 0) {
        Add-Warn "同花顺预检未通过 —— 持仓会降级到 CSV。"
        Add-Warn "  PAPER/LIVE 模式下会因此**拒绝下单**（daily_cycle.broker_refusal）。"
    } else {
        Write-OK "同花顺预检通过。"
    }
}

# --- 汇总 -------------------------------------------------------------------
Write-Line ""
Write-Line "============================================================"
Write-Line " 汇总"
Write-Line "============================================================"
Write-Line ("  Clash Verge : " + $(if ($clashOk) { "运行中" } else { "未运行" }))
Write-Line ("  系统代理    : " + $(if ($SkipProxyTun) { "跳过" } elseif ($proxyOn) { "开" } else { "关" }))
Write-Line ("  TUN 模式    : " + $(if ($SkipProxyTun) { "跳过" } elseif ($tunOn) { "开" } else { "关" }))
Write-Line ("  同花顺      : " + $(if ($SkipThsCheck) { "跳过" } elseif (-not $wantThs) { "不需要" } elseif ($thsLaunched) { "运行中" } else { "未运行" }))
Write-Line ("  qbg 环境    : " + $(if ($pyOk) { "就绪" } else { "不可用" }))
Write-Line ("  同花顺预检  : " + $(if ($thsChecked) { "已做" } else { "未做" }))
Write-Line "============================================================"

if (-not $pyOk) {
    Write-Line "[FATAL] qbg 环境不可用，流程跑不了。" -ForegroundColor Red
    exit 2
}
if ($script:WarnCount -gt 0) {
    Write-Line "[READY WITH WARNINGS] $($script:WarnCount) 条告警。代理只影响 P7/P8 的 LLM 链路，行情和清单不受影响。" -ForegroundColor Yellow
    exit 1
}
Write-Line "[READY] 预检全绿。" -ForegroundColor Green
exit 0
