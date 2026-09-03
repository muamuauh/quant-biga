<#
.SYNOPSIS
    日流程收尾：按安全顺序拆掉现场，然后关机。是 preflight.ps1 的镜像。

.DESCRIPTION
    步骤：

      0. **守卫：日流程还在跑就中止。** 绝不在交易中途关机。
      1. 关掉系统代理 + TUN —— **趁 Clash 还活着**。检测优先，只有确实开着
         才发热键。
      2. 关掉 Clash Verge（代理软件本体）。
      3. 关掉同花顺下单端。先请它自己退出，不肯退再强杀。
      4. `shutdown /s /t <秒>` —— 留一个可取消的窗口（`shutdown /a` 撤销）。

    **为什么先关代理再关同花顺**（和 quant-trading 的 postflight 相反）：
    那边先关 OpenD，是因为 OpenD 的会话本身走代理，先掐代理会把它的连接
    打断在半路。同花顺连的是境内券商服务器、走直连，所以先把代理和 TUN 摘掉
    反而让它在整个收尾过程中都有一条干净的网络；等它退干净了再关机。

    **为什么第 1 步必须在第 2 步之前**：先关 Clash 再关系统代理的话，系统代理
    会短暂指向一个已经死掉的端口 —— 整机 HTTP 断网，而且表现是"网坏了"，
    没人会往这个脚本上想。这和 preflight.ps1 里注册表兜底要带端口守卫是同一条。

    检测优先（和 preflight 一样）：本来就关着的功能绝不会被热键打开。
    热键靠 SendKeys，需要交互桌面 —— session 0 的计划任务里送不到 Clash。

    **不会被自动触发。** 要么你自己跑，要么经过鉴权的「关机」邮件命令
    （scripts/email_listener.py）。

.PARAMETER DryRun
    只检测并打印**会做什么**：不杀进程、不发热键、不关机。先用它验一遍。

.PARAMETER NoShutdown
    做完收尾但跳过最后的关机。邮件命令走的就是这条 —— 先收尾、再发确认信、
    最后才由监听器触发关机，这样确认信里能写清楚收尾到底成没成。

.PARAMETER ShutdownDelay
    关机前的缓冲秒数（默认 60）。窗口期内 `shutdown /a` 可以撤销。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\postflight.ps1 -DryRun
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\postflight.ps1 -NoShutdown
#>

param(
    [string]$ClashExe       = "D:\Clash Verge\clash-verge.exe",
    [string]$ClashProcName  = "clash-verge",
    [string]$ThsProcName    = "xiadan",
    [int]   $ProxyPort      = 7897,
    # 和 preflight.ps1 同一组全局热键（Clash Verge -> 设置 -> 热键设置）。
    [string]$ProxyHotkey    = "^%+p",
    [string]$TunHotkey      = "^%+t",
    [int]   $ShutdownDelay  = 60,
    [switch]$DryRun,
    [switch]$NoShutdown
)

$ErrorActionPreference = "Continue"
$script:Failures = 0

function Write-Step($msg) { Write-Host "==== $msg ====" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "  [OK] $msg"    -ForegroundColor Green }
function Write-Warn2($msg){ Write-Host "  [WARN] $msg"  -ForegroundColor Yellow; $script:Failures++ }
function Write-Err($msg)  { Write-Host "  [ERROR] $msg" -ForegroundColor Red;   $script:Failures++ }

function Test-ProcRunning([string]$name) {
    return [bool](Get-Process -Name $name -ErrorAction SilentlyContinue)
}

# 系统代理算开 iff 注册表 ProxyEnable=1 **且** ProxyServer 指向 Clash 的端口。
# 后半句不能省：别的软件也会开系统代理，认错了就会去关别人的。
function Test-SystemProxyOn([int]$port) {
    $reg = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    try {
        $p = Get-ItemProperty -Path $reg -ErrorAction Stop
        return ($p.ProxyEnable -eq 1) -and ($p.ProxyServer -match ":$port(;|$|\b)")
    } catch { return $false }
}

# TUN 开着才会有那块虚拟网卡。认驱动描述比认名字稳。
function Test-TunOn() {
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

# 关掉 Clash 的某个功能：检测到确实开着才发热键，最多发两次。
# 发偶数次等于没发，所以即使检测函数写错也不会把本来关着的功能打开。
function Disable-ClashFeature {
    param([string]$Label, [scriptblock]$Test, [string]$Hotkey, [int]$SettleSec = 3)
    if (-not (& $Test)) { Write-OK "$Label 本来就是关的。"; return $true }
    if ([string]::IsNullOrWhiteSpace($Hotkey)) {
        Write-Warn2 "$Label 开着但没配热键 —— 请手动关。"; return $false
    }
    if ($DryRun) { Write-Host "  [dry-run] 会发热键 '$Hotkey' 关掉 $Label。"; return $true }
    for ($i = 1; $i -le 2; $i++) {
        Write-Host "  $Label 开着 -> 发热键 '$Hotkey'（第 $i/2 次）"
        try { (New-Object -ComObject WScript.Shell).SendKeys($Hotkey) }
        catch { Write-Err "SendKeys 失败（需要交互桌面）：$($_.Exception.Message)"; return $false }
        Start-Sleep -Seconds $SettleSec
        if (-not (& $Test)) { Write-OK "$Label 已关闭。"; return $true }
    }
    Write-Warn2 "$Label 发完热键仍是开的。核对 Clash 热键设置是否为 '$Hotkey'。"
    return $false
}

# 关一个程序：**先请它自己退出**，不肯退再强杀。
# 同花顺挂着账户会话，强杀会留下未落盘的状态；给它 8 秒体面退出的机会。
function Stop-App {
    param([string]$ProcName, [string]$Label, [int]$GraceSec = 8)
    $procs = @(Get-Process -Name $ProcName -ErrorAction SilentlyContinue)
    if ($procs.Count -eq 0) { Write-OK "$Label 没在运行。"; return $true }
    if ($DryRun) { Write-Host "  [dry-run] 会关闭 $Label（$ProcName，$($procs.Count) 个进程）。"; return $true }
    foreach ($p in $procs) {
        try { $p.CloseMainWindow() | Out-Null } catch { }
    }
    for ($i = 0; $i -lt $GraceSec; $i++) {
        Start-Sleep -Seconds 1
        if (-not (Test-ProcRunning $ProcName)) { Write-OK "$Label 已正常退出。"; return $true }
    }
    try {
        Stop-Process -Name $ProcName -Force -ErrorAction Stop
        Write-OK "$Label 未响应，已强制结束。"
        return $true
    } catch {
        Write-Err "关闭 $Label 失败：$($_.Exception.Message)"
        return $false
    }
}

Write-Host ""
Write-Host "============================================================"
Write-Host " quant-biga postflight: 收尾并关机"
Write-Host "============================================================"

# --- Step 0：绝不在交易中途关机 ---------------------------------------------
# 这一条是整个脚本里最重要的。下单是「填单 → 提交 → 确认框 → 回读校验」四步，
# 中间被断电，我们会永远不知道那一笔到底进没进券商 —— 而那正是
# easytrader_adapter 里 verified=False 那种最危险的状态。
Write-Step "Step 0/4: 安全守卫（确认日流程没在跑）"
# **不要把 email_listener 列进来。** 监听器正是触发关机的那个进程，
# 列进来它就会永远否决自己 —— 邮件命令一次都执行不了。
# 这里要挡的是**交易流程**，而监听器不是交易。
$markers = 'daily_cycle|run_daily|01_ingest|04_plan_orders'
$active = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match $markers -and $_.ProcessId -ne $PID })
if ($active.Count -gt 0) {
    Write-Host "  [ERROR] 还有 $($active.Count) 个日流程进程在跑 —— 中止收尾和关机。" -ForegroundColor Red
    $active | ForEach-Object { Write-Host "    PID $($_.ProcessId): $($_.CommandLine)" }
    exit 2
}
Write-OK "没有正在运行的日流程。"

# --- Step 1：先摘代理/TUN，此时 Clash 还活着 --------------------------------
Write-Step "Step 1/4: 关闭系统代理 + TUN"
if (-not (Test-ProcRunning $ClashProcName)) {
    Write-OK "Clash 没在运行 —— 代理/TUN 视为已经不在。"
} else {
    Disable-ClashFeature "系统代理" { Test-SystemProxyOn $ProxyPort } $ProxyHotkey 3 | Out-Null
    Disable-ClashFeature "TUN 模式" { Test-TunOn }                    $TunHotkey 4 | Out-Null
}

# --- Step 2：关代理软件 ------------------------------------------------------
Write-Step "Step 2/4: 关闭 Clash Verge"
Stop-App $ClashProcName "Clash Verge" 5 | Out-Null

# --- Step 3：关同花顺 --------------------------------------------------------
Write-Step "Step 3/4: 关闭同花顺下单端"
Stop-App $ThsProcName "同花顺下单端" 8 | Out-Null

# --- 汇总 -------------------------------------------------------------------
Write-Host ""
Write-Host "============================================================"
Write-Host (" 收尾汇总：{0}" -f $(if ($script:Failures -eq 0) { "全部完成" } else { "$($script:Failures) 项未达预期" }))
Write-Host ("  Clash Verge   : " + $(if (Test-ProcRunning $ClashProcName) { "仍在运行" } else { "已关闭" }))
Write-Host ("  同花顺下单端  : " + $(if (Test-ProcRunning $ThsProcName)   { "仍在运行" } else { "已关闭" }))
Write-Host ("  系统代理      : " + $(if (Test-SystemProxyOn $ProxyPort)   { "仍开着" }   else { "已关闭" }))
Write-Host ("  TUN 模式      : " + $(if (Test-TunOn)                      { "仍开着" }   else { "已关闭" }))
Write-Host "============================================================"

# --- Step 4：关机 ------------------------------------------------------------
Write-Step "Step 4/4: 关机"
if ($DryRun -or $NoShutdown) {
    Write-Host ("  收尾结束，**跳过关机**（{0}）。" -f $(if ($DryRun) { "dry-run" } else { "-NoShutdown" })) -ForegroundColor Yellow
} else {
    Write-Host "  $ShutdownDelay 秒后关机。反悔请执行： shutdown /a" -ForegroundColor Yellow
    shutdown /s /t $ShutdownDelay /c "quant-biga postflight: 收尾完成，即将关机。"
}

# 退出码：0 = 全绿；1 = 收尾有项目没达预期（但仍可关机）；2 = 守卫中止。
if ($script:Failures -gt 0) { exit 1 }
exit 0
