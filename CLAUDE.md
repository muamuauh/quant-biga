# CLAUDE.md — quant-biga 项目约定

给接手这个项目的 AI agent（和人）。**动手前先读 `plan.md`**，那是完整的
背景、架构和分阶段计划；本文件只讲纪律和操作细节。

---

## 一、硬性禁令

### 1. 绝不写入兄弟仓库

`E:\codes\quant-trading` 和 `E:\codes\quant-agent` 是**只读参考**。

- **禁止**修改它们的任何文件
- **禁止**动它们的 Windows 计划任务（`qtf_daily`、`qtf_preflight`）
- **禁止**动它们的 conda 环境（`qtf`、`qtagent`）

`quant-trading` 挂在**真钱账户**上。这条纪律和 `quant-agent/CLAUDE.md` 里
对它自己的约束是同一条。读它们的源码去移植，往本仓库里写。

### 2. 三把锁不许自动改

`QBG_MODE`、`I_CONFIRM_REAL`（`.env`）和 `allow_live_mode`
（`configs/risk_limits.yaml`）是实盘的三道闸。

**任何自动化流程都不得修改它们**，包括 P8 的复盘调参 agent。要开实盘，
人来手动改三个地方。

### 3. 账户数据不入库

`data/portfolio/`、`tools/screenshots/`、`reports/`、`logs/`、`.env`
全部已在 `.gitignore` 里。**提交前确认 `git status` 干净**。

持仓截图含真实账户金额。OCR 工具处理完不要留在仓库里。

---

## 二、环境

```bash
# conda 不在 PATH，用全路径
C:/ProgramData/miniconda3/Scripts/conda.exe run -n qbg python ...

# 或者先激活
conda activate qbg
```

环境名是 `qbg`（python 3.11），**不要**用 `qtf` 或 `qtagent`——那两个环境里
已经各装了一份叫 `qtf` 的 editable 包。

包名是 `qbg`，同样是为了避开 `qtf` 的命名冲突。

---

## 三、代码约定

### 配置参数必须写"为什么"

`src/qbg/config.py` 和 `configs/*.yaml` 里每个参数都带注释说明**为什么是
这个值**——回测依据、实测教训、或明确标注"占位值待压测"。

这是从 `quant-trading` 继承的习惯，也是这套系统能被人接手的原因。
加参数时照做。

### 日志用 `log_event`

```python
from qbg.utils.logging import get_logger, log_event
log = get_logger(__name__)
log_event(log, "ingest.fetch.ok", code="600519.SH", rows=1234)
```

`msg` 用 `阶段.步骤[.状态]` 的点分命名，`logs/qbg.jsonl` 是 store 层的
**真相源**（`data/runs.db` 里每一行都能从它重建）。

### 测试必须离线

**不联网、不碰券商、不调 LLM。** 数据源要 mock。这是两个参考仓库的铁律，
本项目沿用——一套需要联网才能跑的测试等于没有测试。

### 风控闸的两级语义不许简化

`src/qbg/risk/gates.py`：
- **硬闸**失败 → 全盘不交易
- **订单闸**失败 → **只砍 BUY，SELL 永远放行**

第二条容易被"顺手统一"成一票否决。别改——跌得多、现金紧的时候恰恰最需要
能卖出去。

---

## 四、A股规则，最容易写错的地方

改这几处前先读 `plan.md` §5：

| 规则 | 要点 |
|---|---|
| T+1 | 当日买入当日不可卖。**风控闸和回测引擎两处都要有**，只做一处回测收益会虚高 |
| 一手 | 买入必须 100 股整数倍。10万账户 k=3 → 单槽3万 → 只能买 300 元以下的票 |
| 涨跌停 | 主板 ±10%，创业板/科创板 ±20%，主板 ST ±5%，北交所 ±30% |
| 费用 | **不对称**：印花税 0.05% 只在卖出时收 |
| 复权 | 训练/回测用后复权(hfq)，下单清单用不复权 |
| 成交价 | 回测用**次日开盘价**——盘后出信号、次日人工执行，中间隔一个跳空 |

---

## 五、当前进度

**详细进度、实测发现和 P3 开工须知见 `PROGRESS.md`。**

- [x] **P0** 项目骨架
- [x] **P1** 数据层（BaoStock 主源 + 增量 parquet 缓存 + 沪深300 universe）
- [x] **P2** A股规则层 + 回测引擎
- [x] **P3** 模型与选股（三 seed Rank IC 全正，见 `docs/p3-model-validation.md`）
- [x] **P4** 风控闸链 + 下单清单
- [x] **P5** 持仓 OCR 工具（真实南京证券截图 5 只持仓验收通过）
- [x] **P6** 编排 + 日报 + store
- [x] **P7** TradingAgents 逐票复核闸（真实调用通过；当前部署已启用，异常 fail-open）
- [x] **P8** 自动复盘 agent（第三方 OpenAI-compatible 中转；严格 JSON、白名单、八闸、回滚、熔断；真实复盘通过）
- [x] **P9** easytrader 接管持仓与下单（见 `plan.md` §2.2.1/§7-P9）
      - [x] **P9a 只读持仓**（模拟账户实测：4 行取表、T+1 可卖量、清仓残留行过滤）
      - [x] **P9b 下单**（三道校验：填单前回读 → 确认框解析 → 提交后
            `today_entrusts` 按合同编号比对。模拟账户实下 3 笔通过）
      - [x] **P9c 编排接入**（`daily_cycle._submit_to_broker`；持仓读不到时
            `broker_refusal` 拒绝下单）

出站邮件通知已接入日流程；SMTP 未配置时静默跳过，任何邮件失败不得影响清单和风控。
当前没有入站邮件命令通道，邮件回复不得进入交易路径。

阶段目标、任务清单和验收标准见 `plan.md` §7。

---

## 六、待补充的外部输入

1. **南京证券实际佣金费率** —— P2 需要，填进 `configs/fee_profile.yaml`
   （当前是行业常见值万2.5，非实测）
2. **邮件 SMTP 凭据与收件地址** —— 只写本机 `.env`，用于出站日报和失败告警
3. （可选）打电话确认南京证券 NXT/QMT 开通门槛 —— 决定是否实现 `QmtAdapter`

---

## 七、同花顺 UI 自动化（P9）

**部署位置：全部在本 Windows 机器。** 同花顺只有 Windows 客户端，UI 自动化
必须和它同机。2026-08-21 用户确认暂不上 Linux 云服务器 —— `docs/deployment.md`
和 `run_daily.sh` 保留备用，但当前不是主路径。

**选 easytrader 而不是 THSAutoTrader**，理由见 `plan.md` §2.2.1。一句话版本：
后者的下单接口没有价格参数（只能闪电买卖），和本项目"限价 + 涨跌停夹逼"的
设计冲突。

**升级同花顺后先跑探针**：

```bash
python tools/probe_ths.py --preflight-only   # 不碰客户端
python tools/probe_ths.py                    # 只读四表 + 字段对照
```

**三个工具脚本，注意哪个会下单**：

| 脚本 | 会不会下单 |
|---|---|
| `tools/probe_ths.py` | ❌ 只读四表 |
| `tools/probe_ths_order.py` | ❌ 只填表单，不点提交 |
| `tools/validate_ths_p9.py` | 🔴 **会真的下单**（三道闸：PAPER + 模拟账户 + 交易时段） |

`tools/probe_ths.py` 和 `src/qbg/portfolio/ths_client.py` **都没有下单代码路径**，
不要往里加。下单走 `src/qbg/execution/`，需要三把锁。

**取表的真实实现在 `ths_client.py`，探针只是它的调用方。** easytrader 自带的
三个 grid 策略在同花顺 9.60.61 上一个都不能用，其中 `Copy`/`WMCopy` 是**静默**
返回空列表（不抛异常）。所以有两条硬规矩：

- **取表的成功判据不能是「没抛异常」** —— 必须用哨兵值确认剪贴板真被覆盖
- **填验证码必须 `type_keys`**，`set_edit_text` 走 WM_SETTEXT 不被认账；
  而且两种方式 `window_text()` 回读都是空，**不能拿回读当校验**

**两个必须知道的坑**（本机 2026-08-21 实测）：

| 坑 | 现象 | 修 |
|---|---|---|
| 同花顺以**管理员**运行，Python 是普通权限 | UIPI **静默丢弃**模拟输入：找得到窗口、不报错、就是不动 | 让同花顺以普通权限运行（优于给 Python 提权：定时任务权限更小） |
| `xiadan.exe` 是 **32 位**，`qbg` 是 64 位 | 多数能跨位数工作，但读控件文本/取表格偶发失败 | 出现空表再建 32 位环境 |

**control_id 是硬编码的**（1032 代码 / 1033 价格 / 1034 数量 / 1006 提交 /
1047 表格）。同花顺重排控件就会失效，**而且失效往往是静默的** ——
读到空表，或把数字打进错误的框。所以下单后**必须回读 `today_entrusts` 校验**
代码/方向/数量/价格，不一致立即停止后续订单。

---

## 八、定时运行（Windows 计划任务）

**详见 `docs/windows-schedule.md`。** 这里只记纪律。

```
scripts/preflight.ps1      Clash 代理/TUN + 同花顺 + qbg 环境预检（不跑流程）
run_daily.ps1              锁 → 预检 → daily_cycle → 原样传出退出码
scripts/setup_schedule.ps1 注册**两个**计划任务
```

`run_daily.bat` / `setup_schedule.bat` 只是双击用的壳。

### 为什么是两个任务、为什么是 09:15 / 09:30

```
09:15  quant_biga_preflight   起 Clash + 同花顺，开系统代理/TUN
       ↓  这 15 分钟是留给**人工登录同花顺**的 —— 脚本做不到这件事
09:30  quant_biga_daily       预检（幂等重跑）→ 拉数 → 打分 → 复核 → 下单
```

**09:30 不是随便选的。** `configs/risk_limits.yaml` 的 `require_trading_session`
随 P9c 改成了 `true`，而它是硬闸 —— 盘前跑会被 `session_guard` 一票否决，
当天什么都不做。07:30 是半自动时代的值（盘前出清单、开盘人工执行）。
**不要为了早点成交把时间往前提**：硬闸是流程跑到一半才判的，
非交易时段会让整天作废，而那时 LLM 复核的钱已经花掉了。

预检跑两遍是**故意的**，它从设计上就幂等：代理/TUN 检测到开着就不发热键
（热键是 toggle，发偶数次等于没发）、同花顺在跑就只报告、端口在监听就跳过
等待。预检的退出码 `1` 是「有告警但可以跑」，**不是失败**。

### 三条不许简化的地方

1. **退出码原样传出**。`0` 正常（含安静跳过）、`2` 硬闸中止、`127` 连解释器
   都没找到、其他为异常。`127` 尤其要留：那时 Python 根本没跑，
   `notify_failure` 的邮件链是断的，计划任务的 `LastTaskResult` 是唯一痕迹。

2. **交易日闸看输出，不看退出码**。`00_market_check.py` 的退出码 `1` 既可能是
   「休市」也可能是「脚本自己崩了」，拿它当判据会让一次 import 错误安静地跳过
   一个真正的交易日。只有明确读到 `'trading_day': False` 才跳过。

3. **代理/TUN 失败只告警，不阻断**。行情走 BaoStock（境内直连），代理只服务
   P7/P8 的 LLM 中转站，而那两条链路本来就 fail-open。这一点和
   `quant-trading` 相反 —— 那边 OpenD 端口不通是真的没法交易。

### 会话感知：热键和 UI 自动化都要先判会话

SendKeys 只能送到**同一个交互会话**里的 Clash；同花顺自动化同理。
在 session 0（「不管用户是否登录」的任务）或锁屏时，两者都是**静默失败**。
`preflight.ps1` 因此先判会话（session id / explorer / LogonUI / Clash 在哪个
会话），判不过就不发热键，改走注册表兜底或如实告警。

**注册表兜底必须有端口守卫**：把系统代理指向一个没在监听的端口会让整机 HTTP
断网，而且表现是「网坏了」，没人会往这个脚本上想。

### 两个不许动的地方

- `setup_schedule.ps1` **拒绝** `qtf_*` / `qtagent_*` 开头的任务名（两个任务名
  都查）。那是兄弟仓库的任务（§一），而且它那两个正好也叫 `qtf_daily` /
  `qtf_preflight` —— 覆盖是静默的。
- 计划任务的 `RunLevel` 固定 `Limited`。同花顺以普通权限跑，Python 也必须是
  普通权限，否则 UIPI 静默丢输入（§七）。

### 三个 `.ps1` 必须带 UTF-8 BOM

计划任务用 System32 的 `powershell.exe`（5.1），没有 BOM 它按 GBK 读，
中文全乱。**不要**改用 `pwsh.exe`：本机那份装在 WindowsApps 下，是应用执行
别名，session 0 里解析不开，报的还是「找不到文件」。
