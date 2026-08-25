# Windows 定时运行方案

两个计划任务：**09:15 预检**（起 Clash + 同花顺，留出人工登录时间）、
**09:30 主流程**（下单）。开机后自动补跑。**本机（Windows 11）实测**，
不是设想。

最后更新：2026-08-22

---

## 一、三个脚本，各管一段

| 脚本 | 干什么 | 谁调它 |
|---|---|---|
| `scripts/preflight.ps1` | Clash 代理/TUN + qbg 环境预检 | `run_daily.ps1` 自动调 |
| `run_daily.ps1` | 锁 → 预检 → `daily_cycle` → 传出退出码 | 计划任务 / `run_daily.bat` |
| `scripts/setup_schedule.ps1` | 注册计划任务 | 人，一次性 |

`run_daily.bat` 和 `setup_schedule.bat` 只是双击用的壳，逻辑全在 `.ps1` 里。
`run_daily.sh` 是 Linux 版本，当前不是主路径（见 CLAUDE.md §七）。

### 为什么定 09:30

**因为自动下单必须在盘中。** `configs/risk_limits.yaml` 的
`require_trading_session` 现在是 `true`（P9c 接了券商就必须开），而它是
**硬闸** —— 盘前跑的话 `session_guard` 一票否决，`hard_ok=False`，当天什么
都不做，日报写「硬闸中止」。

信号口径没变：仍然是 **T-1 收盘数据**（`plan.md` §5），BaoStock 那份早就
落库了。变的只是成交价 —— 从"T 日开盘价"变成"T 日 09:40 前后的市价"，
比回测假设略差一点，这是自动执行换来的。

时序（实测量级）：

```
09:30  触发 -> 预检：起 Clash + 同花顺、开系统代理/TUN     ~1 min
09:31  拉数据（增量）+ 打分                                ~2 min
09:33  LLM 逐票复核 5 只（约 80 次调用）                   ~5-8 min
09:41  风控闸 -> 顾问清单 -> 券商下单 -> 回读校验
09:45  日报 + 邮件
```

⚠ **不要为了早点成交把时间提前到 09:30 之前。** 硬闸是在流程跑到一半才
判定的，非交易时段会让整天作废 —— 而且失败得很晚，LLM 的钱已经花掉了。
想更早进场，正确做法是关掉 LLM 复核（`QBG_AGENTS_ENABLED=0`），
而不是提前触发时间。

历史：17:45（盘后出 T+1 清单）→ 07:30（盘前出当日清单，人工执行）
→ 09:30（盘中自动下单）。前两个是半自动时代的值。

---

## 二、两种模式，差别只有「有没有桌面」

`setup_schedule.ps1 -Mode` 二选一。这不是偏好问题，是**能力问题**：

| | `-Mode Interactive`（默认） | `-Mode Background` |
|---|---|---|
| 计划任务安全选项 | 只在用户登录时运行 | 不管用户是否登录都运行（S4U，不存密码） |
| 触发器 | 每天 09:30 + 登录后 3 分钟 | 每天 09:30 + 开机后 5 分钟 |
| 无人登录时 | ✘ 不跑 | ✔ 照跑 |
| 锁屏时 | ✔ 进程跑（但见下） | ✔ 照跑 |
| 行情 / 选股 / 风控 / 清单 / 日报 / 邮件 | ✔ | ✔ |
| Clash 系统代理 | ✔ 走热键 | ✔ 走注册表 |
| **Clash TUN 模式** | ✔ 热键 | ✘ **开不了** |
| **同花顺 UI 自动化**（P9） | ✔（**但锁屏时也失败**） | ✘ **必定失败** |
| 注册时要管理员权限 | ✘ 不要 | ✔ 要 |

### 那两个 ✘ 为什么是硬的

**TUN**：TUN 由 Clash 内核 + `clash-verge-service` 拉起，开关在 Clash Verge 的
托盘应用里。托盘应用是**每用户**的 GUI 进程，session 0 里没有它，也没有能接
全局热键的桌面。唯一的非交互入口是 mihomo 的外部控制器（`PATCH /configs`），
而本机 `verge.yaml` 里 `enable_external_controller` 现在是 `false`。

**同花顺**：pywinauto 靠模拟键鼠驱动 `xiadan.exe`。跨会话的输入被系统直接丢弃，
锁屏时输入桌面被 `LogonUI` 占着 —— 两种情况都是**静默失败**：找得到窗口、
不报错、就是不动。和 CLAUDE.md §七 记的 UIPI 那个坑是同一类。

所以 `preflight.ps1` 会先判会话（session id、有没有 explorer、有没有 LogonUI、
Clash 在不在同一会话），判不过就**不发热键**，改走注册表兜底或如实告警。
它绝不会在日志里写「已发送热键」然后什么都没发生。

### 本机当前用的是 Interactive

2026-08-22 用户确认：**「我会自己开机并且解锁」**。所以默认就是 Interactive，
上表右边那一列的两个 ✘ 都不会碰到 —— Clash 热键、TUN、同花顺自动化全可用，
注册也不需要管理员。

代价只有一条：**机器没登录的那天，任务不跑**（`StartWhenAvailable` 会在登录后
尽快补上，见 §四）。真要做到无人值守，再看下一节。

---

## 三、既要开机免解锁，又要有桌面

只有一条路：**让 Windows 开机自动登录**，然后用 `-Mode Interactive`。

自动登录会把密码写进注册表（或用 `netplwiz` 存进 LSA）。这台机器上挂着真钱
账户，是一个真实的安全权衡，所以**脚本不替你改**，要做自己动手：

```powershell
# 方式一（推荐，密码存 LSA 不是明文注册表）：
#   Win+R -> netplwiz -> 取消勾选"要使用本计算机，用户必须输入用户名和密码"
#   （Win11 若没有这个勾选框，先执行下面这条再重开 netplwiz）
Set-ItemProperty "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\PasswordLess\Device" `
    -Name DevicePasswordLessBuildVersion -Value 0

# 之后注册交互模式的任务：
powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1 -Mode Interactive
```

配套还要开两个开关，否则自动登录进桌面了 Clash 还是没起来：

1. **Clash Verge → 设置 → 开机自启**（`verge.yaml` 的 `enable_auto_launch`，
   本机当前是 `false`）。不开的话 `preflight.ps1` 会去启动它，但那要多花十几秒。
2. **同花顺独立下单程序**：记住密码 + 自动登录，且保持**专业模式**、
   **普通权限**运行（CLAUDE.md §七）。

   `preflight.ps1` 的 Step 4/5 会在持仓源是 `easytrader` 时自动拉起
   `xiadan.exe`，但**它只能把程序起起来，登录要人工做**（或靠同花顺自己的
   自动登录）。脚本会回读窗口标题：不是「网上股票交易系统5.0」就告警，
   因为停在登录框上的客户端和已就绪的客户端一样有窗口句柄 ——
   只看「进程在不在」会给出假的就绪信号。

   没登录的后果是**确定的、且是安全的**：取表失败 → 持仓降级 →
   PAPER/LIVE 模式按 `daily_cycle.broker_refusal` 拒绝下单，日报和邮件
   主题都会写「⚠ 持仓读不到·结果不可用」。不会拿着假账户去下单。

> **别用「锁屏但保持登录」凑合。** 那样 `explorer` 在、进程能跑，但输入桌面被
> `LogonUI` 占着，同花顺自动化照样静默失败。要么真的留在桌面，要么接受降级。

### 为什么拆成两个任务

`run_daily.ps1` 内部本来就会调 `preflight.ps1`，那为什么还要单独注册一个？

**因为同花顺的登录只能人工做。** 如果只有 09:30 一个任务，预检发现客户端停在
登录框上时已经来不及了 —— 那一天就只能降级、拒绝下单。提前 15 分钟跑一次，
窗口就摆在桌面上等你，主流程到点时客户端已经可用。

重复跑没有副作用，这是预检从一开始就按幂等设计的：

| 步骤 | 第二次跑时 |
|---|---|
| Clash 进程 | 已在跑 → 只报告 |
| 系统代理 / TUN | **检测到开着就不发热键**（热键是 toggle，发偶数次等于没发） |
| 等网络落定 | 端口已在监听 → 跳过那 10 秒 |
| 同花顺 | 已在跑 → 只报告并回读窗口标题 |
| 交易日闸 | 非交易日两次都直接退出 |

预检任务**不加**开机/登录触发器：登录后再跑一次预检没有意义（主任务自己会跑），
只会多弹一个同花顺窗口。

> 预检的退出码 `1` 表示「有告警但可以跑」，**不是失败**。计划任务的
> `LastTaskResult` 会显示 `1`，别被它吓到 —— 真正的致命是 `2`（qbg 环境不可用）。

---

## 四、装

```powershell
# 默认 Interactive，不需要管理员：双击 setup_schedule.bat，或者
powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1

# 无人值守模式（需要管理员 PowerShell）：
powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1 -Mode Background

# 改时间 / 改任务名 / 不要开机触发器：
powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1 -Time 09:30 -NoStartupTrigger

# 拆
powershell -ExecutionPolicy Bypass -File scripts\setup_schedule.ps1 -Remove
```

任务名默认 `quant_biga_daily`。脚本**拒绝**接受 `qtf_*` / `qtagent_*` 开头的
任务名 —— 那是兄弟仓库的任务，CLAUDE.md 明令禁止动，而覆盖是静默的。

### 开机触发器为什么不会重复跑

`daily_cycle` 有当日幂等标记（`already_completed_today`）和交易日闸。09:30
跑过之后当天再触发就是一次几秒的空转。它真正的价值是：**09:30 那会儿机器
关着或睡着的那天，开机后能自动补上**。任务设置里的 `StartWhenAvailable`
（错过就尽快补）和 `WakeToRun`（到点唤醒）也是为这个。

---

## 五、验

```powershell
# 立刻跑一次
Start-ScheduledTask -TaskName quant_biga_daily

# 上次结果 / 下次时间（LastTaskResult 0 = 正常）
Get-ScheduledTaskInfo -TaskName quant_biga_daily

# 只跑预检，不碰流程
powershell -ExecutionPolicy Bypass -File scripts\preflight.ps1

# 手工试跑（不提交，跑完停住）
.\run_daily.bat --dry-run
```

日志三处，出事按这个顺序看：

| 文件 | 内容 |
|---|---|
| `logs/run_daily.log` | 每次启动一行：START / DONE / WARN / ERROR |
| `logs/run_daily_YYYYMMDD.log` | 当天整段控制台输出（计划任务会吞掉控制台，这是唯一的现场） |
| `logs/qbg.jsonl` | store 层的真相源 |

### 退出码

`run_daily.ps1` **原样传出** `daily_cycle` 的退出码，不许吞：

| 码 | 含义 |
|---:|---|
| 0 | 正常（含「非交易日 / 今日已跑」这类安静跳过） |
| 2 | 硬闸中止 —— 风控拒绝交易，**需要人看** |
| 127 | 连解释器都找不到 —— Python 没机会跑，**告警邮件链是断的** |
| 其他 | 异常退出，Python 侧已尽力发过崩溃告警邮件 |

127 那条要特别当回事：环境没建好 / 盘没挂上 / 目录被删的时候，表现就是
「今天没收到邮件」，而这和周末、和真的没交易，长得一模一样。所以计划任务的
`LastTaskResult` 是这种情况下**唯一的痕迹**。

---

## 六、排错

| 现象 | 原因 | 修 |
|---|---|---|
| 任务显示 0x1，`logs/` 里什么都没有 | 宿主 shell 没起来 | 别用 WindowsApps 下的 `pwsh.exe`（应用执行别名，session 0 解析不开），脚本已默认用 System32 的 `powershell.exe` |
| 任务跑了，`LastTaskResult=127` | 找不到 qbg 解释器 | 设 `QBG_PYTHON`，或 `conda env create -f environment.yml` |
| 预检说「系统代理开着」但 Python 出不了网 | 系统代理指向的端口没在监听 | `preflight.ps1` 写注册表前有端口守卫，但**热键路径**不管；看 Clash 是不是刚崩了 |
| 预检发了热键但代理还是关的 | Clash Verge 里的热键绑定和脚本参数对不上 | 核对 `verge.yaml` 的 `hotkeys`，或改脚本的 `-ProxyHotkey` / `-TunHotkey` |
| 日报里 `portfolio.degraded` 有值 | 同花顺读失败，降级到 CSV | Background 模式下这是**预期行为**（没有桌面）；Interactive 模式下看 `python tools\probe_ths.py --preflight-only` |
| 中文全是乱码 | `.ps1` 丢了 UTF-8 BOM | 三个 `.ps1` 必须**带 BOM** 保存，否则 `powershell.exe` 5.1 按 GBK 读 |
| `logs/run_daily_*.log` 里中文变成 `????` | 用了 `Start-Transcript` | PS 5.1 的 transcript 编不出非 ASCII 且**不报错**。已改成自己 `Add-Content -Encoding UTF8`，别改回去 |
| 预检的输出没进日志，只有 daily_cycle 的 | 靠管道接 `Write-Host` | PS 5.1 的 `Write-Host` **不进管道**。preflight 自己往 `QBG_RUN_LOG` 落盘，run_daily 调用前设这个变量 |
| 凌晨手动补跑出错日期 | Python 本地时区不是 +0800 | 预检会告警。注意 **Windows 下 `TZ` 环境变量对 CPython 无效**，只能改系统时区 |
