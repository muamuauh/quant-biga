# quant-biga — A股量化交易系统 实施计划与交接文档

> **文档定位**：这是一份**自包含的交接文档**。任何人（或任何 AI agent）拿到这份文档，无需再去问原作者，即可从零开始执行。
> **文档位置**：批准后第一个动作就是把本文件复制为 `E:\codes\quant-biga\plan.md`，随项目一起版本管理。
> **最后更新**：2026-08-10
> **状态**：待实施（`E:\codes\quant-biga` 目前是空目录）

---

## 目录

1. [背景](#1-背景)
2. [调研结论](#2-调研结论)
3. [需求与决策记录](#3-需求与决策记录)
4. [系统架构](#4-系统架构)
5. [A股特有规则详解](#5-a股特有规则详解)
6. [复用与重写清单](#6-复用与重写清单)
7. [分阶段实施](#7-分阶段实施)
8. [风险与限制](#8-风险与限制)
9. [验证策略](#9-验证策略)
10. [待补充的输入](#10-待补充的输入)
11. [附录](#11-附录)

---

## 1. 背景

### 1.1 用户与目标

用户是 `E:\codes` 下多个项目的作者，已有两套**成熟的美股量化系统在运行**（其中一套挂在真钱账户上）。现在想做**第三套，针对 A股**，券商是**南京证券**。

核心诉求：
- 用已经跑通的架构做 A股，不重新发明轮子
- 数据源需要探索确定（用户明确提到 tushare 之类）
- 持仓数据接入方式需要探索确定（南京证券）

### 1.2 两个参考仓库

这两个仓库是本项目的**架构母本**，必须先理解它们再动手。

#### `E:\codes\quant-trading` — 包名 `qtf`，实盘骨架

**一句话**：moomoo OpenD 拉 50 只美股日K → qlib LightGBM + Alpha158 打分 → TradingAgents 多 agent LLM 复核 → 5 道风控闸 → 限价单进 moomoo → 中文 Markdown 日报。

**状态**：`.env` 里 `FUTU_TRD_ENV=REAL` + `I_CONFIRM_REAL=1` + `risk_limits.yaml` 的 `allow_real_env: true` —— **三把锁全开，正在跑真钱**。最新日报显示 Futu SG 保证金账户约 $3,509，持有 US.ORCL + US.ABT。

**分层**（`src/qtf/` 下 10 层，约 7,200 行）：

| 层 | 职责 | 关键文件 |
|---|---|---|
| `config.py` | pydantic-settings 单一配置源，**每个参数带注释写清"为什么是这个值/回测依据"** | — |
| `data/` | 拉日K、转 qlib bin、财报日历 | `ingest.py` `moomoo_kline.py` `schema.py` `earnings.py` |
| `model/` | LightGBM 训练（跳过 qlib workflow，Windows joblib 会崩） | `train.py` `handlers.py` |
| `strategy/` | 预测加载、top-K 权重、市场择时 | `predict.py` `topk_weights.py` `regime.py` |
| `agents/` | TradingAgents LLM 逐票复核 | `review.py` `ticker_map.py` `token_tracker.py` |
| `execution/` | 订单规划、broker 提交、费率、成交分析 | `order_planner.py` `moomoo_executor.py` `fees.py` `fills.py` |
| `risk/` | 风控闸链、交易时段、止损、移动止盈 | `gates.py` `market_hours.py` `stops.py` `trailing.py` |
| `backtest/` | **自研向量化回测**（非 backtrader/vectorbt） | `engine.py` `metrics.py` `regime_stress.py` `report.py` |
| `report/` | 中文日报、快照 | `daily_report.py` `snapshot.py` |
| `orchestrator/` | 日循环编排、幂等标记 | `daily_cycle.py` `run_marker.py` |
| `notify/` | SMTP 外发 + IMAP 命令通道 | `mailer.py` `inbox.py` `tokens.py` |
| `utils/` | JSONL 结构化日志、子进程桥 | `logging.py` `subprocess_runner.py` |

**技术栈**：Python 3.11（conda env `qtf`）；`pydantic-settings` / `python-dotenv` / `pandas_market_calendars` / `mlflow` / `pyyaml`；vendored `qlib`（microsoft）和 `TradingAgents`（TauricResearch），都是 gitignored 的本地 clone + `pip install -e`；ruff line-length 110，select `E,F,I,B,UP`；pytest 26 个测试文件约 200 个测试函数。

**存储**：**没有数据库**。qlib `.bin` 列存 + CSV + JSON 缓存 + MLflow 本地 file store + JSONL 日志 + Markdown 报告。

**关键设计（必须理解，本项目要照搬）**：

1. **风控闸链的两级语义**（`risk/gates.py`）——
   - **硬闸**（`env_guard` / `market_hours_guard` / `currency_guard`）：任一失败 → `hard_ok=False`，`allowed_orders` 清空，**什么都不交易**。
   - **订单闸**（`daily_loss_kill_switch` / `max_position_guard` / `min_cash_guard`）：**只砍 BUY，SELL 永远放行**。因为卖出是降风险的，跌得多、现金紧的时候恰恰最需要能卖出去。这是和"一票否决"式闸链的本质区别。
2. **T+1 结算感知下单**（`daily_cycle._submit_settled`）：先提交 SELL → 轮询直到持仓变成预期数量 → 重新读券商现金 → 才用真实到手的钱下 BUY。加这个是因为 2026-06-15 纸面账户现金曾被打到 −$24,322。
3. **幂等**（`orchestrator/run_marker.py`）：当日标记阻止重复提交。
4. **可负担性过滤**（`strategy/topk_weights.py::affordable_scores`）：小账户上，单槽预算买不起一股的票直接从候选里剔除，否则规划器会静默不下单、槽位空着变成现金拖累。
5. **迟滞选股**（`select_with_hysteresis`）：持仓股只要还在 top `keep_rank` 内就保留，只有掉出去才换人。实测把日均换手 0.745 降到 0.524。

**实测基线**（`docs/roadmap.md`）：Rank IC 0.0128 → 0.0245（防过拟合调参 + 3 seed 集成）；换手 0.745 → 0.524；最大回撤 −24% → −16%；首笔真实成交滑点 +3.08 bp。

**已部署参数**：`QTF_TOP_K=3`、`QTF_REBALANCE_EVERY_DAYS=10`、`QTF_KEEP_RANK=15`、`QTF_MARKET_SMA=100`、`QTF_ENSEMBLE_SEEDS=3`、`QTF_EARNINGS_BLACKOUT_DAYS=12`、`QTF_AGENTS_MIN_RATING=Hold`。
`configs/risk_limits.yaml`：`max_position_pct: 0.34`、`min_cash_buffer_pct: 0.05`、`max_daily_loss_pct: 0.03`、`stop_loss_pct: 0.08`、`rebalance_drift_band: 0.03`、`order_slippage_pct: 0.002`。

#### `E:\codes\quant-agent` — 同样包名 `qtf`，agent 复盘/调参层

**一句话**：在同一个 qtf 骨架**下游**长出的自我复盘 + 自我调参层。骨架照常跑，agent 每天自动复盘、在回测把关下提出/应用/回滚参数改进。

**状态**：永久 `FUTU_TRD_ENV=SIMULATE`（模拟盘），三把锁 + 一个 PreToolUse 熔断禁止 agent 修改 `FUTU_TRD_ENV`/`FUTU_ACC_ID`。

**比 quant-trading 多出来的层**：

| 层 | 职责 |
|---|---|
| `store/` | **SQLite 派生索引**：19 张表 + 2 个视图。**永远是派生的、不是真相源**，可从 JSONL 日志 + CSV + mlruns 全量重建；交易路径不读它，删掉也不影响下单 |
| `analysis/` | **零 LLM 事实计算**：归因、运行健康诊断、记分卡、持仓、假设、触发器、评级稳定性、新闻 |
| `tuning/` | 参数白名单（分层 `A/A_risk/A_slow/A_replay/B/B_paper_blind/frozen`）、**8 项回测把关**（含子区间稳定性、参数高原、+75% 成本鲁棒性）、overlay 应用、审计、回滚 |
| `agent/` | Claude Agent SDK：20 个 MCP 工具、3 级权限（allow/confirm/deny，无人值守时 confirm ⇒ Deny）、PreToolUse 熔断、转录脱敏 |
| `research/` | 离线实验：Alpha Vantage 情绪、SEC 基本面、LLM 行业分类、LLM 因子挖掘 |
| `notify/` | 邮件日报 + **入站邮件命令通道**（复盘/回滚/状态/关机/重跑，需发件人白名单 + 一次性 token） |

**store schema 的关键设计**：`trd_env` 在**每张账户级表的主键里**。继承来的 REAL 历史和本仓库自己的 SIMULATE 历史并排存放，**绝不能被加总成一条净值曲线**。`queries.py` 默认按它过滤。本项目要把这个语义换成 `mode`（`ADVISORY`/`PAPER`/`LIVE`）但保留同样的隔离。

**19 张表**：`runs` `scores` `targets` `verdicts` `orders` `fills` `gates` `equity` `positions` `prices` `llm_usage` `stops` `events` `proposals` `param_changes` `reviews` `findings` `hypotheses`
**2 个视图**：`v_fwd_returns`（次日收益）、`v_decisions`（"我们当时怎么想的 / 做了什么 / 结果如何"的那个 join）

`hypotheses` 表的 `discriminator` 字段是 `NOT NULL` —— **一个没有任何观测能证伪它的信念不是假设**，这张表拒绝存这种东西。这个设计值得照搬。

**Agent 配置**：`DEFAULT_MODEL = "claude-sonnet-5"`，`max_turns=24`，`max_budget_usd=0.80`。
`.claude/` 表面：4 个 subagent（`quant-analyst` `risk-auditor` `param-tuner` `news-explainer`）、8 个斜杠命令（`/review /positions /health /why /run /tune /rollback /backfill`）、5 个 skill。

#### 两个仓库的边界纪律

`quant-agent/CLAUDE.md` 明文规定：quant-trading 是**真钱仓库**，quant-agent **禁止**碰它的任何文件，也禁止碰它的 `qtf_daily` / `qtf_preflight` 计划任务。

> **这条纪律对 quant-biga 同样适用**：quant-biga 只读这两个仓库作为参考，**绝不写入它们的任何文件、不修改它们的计划任务、不动它们的 conda 环境**。

### 1.3 机器环境现状

```
系统 Python:  C:\Python314\python.exe  →  Python 3.14.4   ← 太新，qlib/lightgbm 生态跑不了
conda:        C:\ProgramData\miniconda3\Scripts\conda.exe  →  conda 26.1.1 (base py 3.13.12)
              ⚠ conda 不在 PATH，Bash 里要用全路径或先 shell hook
uv / poetry:  未安装 / 不在 PATH

现有 conda 环境（C:\Users\gjq00\.conda\envs\）：
  qtf                  py3.11.15  pyqlib 0.9.8.dev31 (editable ← quant-trading/qlib)
                                  lightgbm 4.6.0, mlflow 3.12.0, moomoo_api 10.6.6608, yfinance 1.4.0
  qtagent              py3.11.15  同栈，pyqlib editable ← quant-agent/qlib
  multi-agent-trading  py3.11.15
  miniClaudeCode       py3.10.20
  damaihelper          py3.10.20
  ticket-purchase      py3.10.20

A股数据库安装情况：tushare / akshare / baostock 在系统 Python 和所有 conda 环境里 **全部缺失**。
```

`E:\codes\quant-biga` **存在但完全为空**（创建于 2026-08-10 17:39），无 git，无 CLAUDE.md，无 `.claude/`。`E:\codes` 本身也不是 git 仓库。

全局 `C:\Users\gjq00\.claude\settings.json` 有一个很长的 `permissions.allow` 列表，大多指向 `E:\codes\quant-trading`、`E:\codes\miniClaudeCode` 和 `qtf` / `miniClaudeCode` 两个 conda 环境。**没有任何一条 scoped 到 quant-biga** —— 新项目会频繁触发权限提示，P0 阶段应考虑加一批项目级 `.claude/settings.json` 允许项。

---

## 2. 调研结论

### 2.1 A股数据源

| 源 | 机制 | 优点 | 缺点 | 本项目定位 |
|---|---|---|---|---|
| **BaoStock** | 自建服务器，SDK 直连 | 免费无限量；**唯一不靠爬网页的免费源**；有 `adjustflag`(前/后复权)、`tradestatus`(停牌)、`isST`、`turn`(换手)、`peTTM`；2026 年仍在更新 | 无实时行情；无指数成分股 | **主源**（日K + 复权 + 停牌/ST 标志） |
| **AKShare** | 爬东方财富/新浪等 | 覆盖最全：指数成分、申万行业、财务三表、个股新闻、公告、券商研报、股吧人气 | **批量拉会限频/封 IP/跳验证页**；接口无预告变更；长期稳定性差 | **补充源**（低频元数据 + 复核层的新闻/财务） |
| **Tushare Pro** | 官方 API，积分制 | 数据规范、稳定 | **新注册 0 积分**；120 积分档只给**不复权**日线（50 次/分、8000 次/天）；实用档位要充值（200元=2000积分起）；近年"积分通胀" | **本期不用**，写成可插拔源留着 |
| **Mootdx** | 直连通达信服务器 | 免费、快、可读本地离线数据 | 非官方；复权要自己算 | **第三备胎** |

**决策**：`QBG_DATA_SOURCES=baostock,akshare,mootdx`，实现降级链。**所有行情落 parquet 增量缓存** —— 这是在限频环境下活下来的关键，也是后面喂给 LLM 复核层的同一份数据。

### 2.2 南京证券的程序化接入

调研了官网、迅投社区、券商门槛表：

| 产品 | 类型 | 说明 |
|---|---|---|
| 鑫易通综合交易平台 V8.77 | PC | 官网主推的 PC 交易端 |
| 南京证券大智慧 V9.79 | 手机 + PC | 行情 + 普通交易 + 两融 |
| 南京证券金罗盘 V8.03.020 | 手机 | "智能理财鑫方向" |
| **NXT 极速策略交易系统** | PC | **就是迅投 QMT 换皮**。官网原文："针对量化私募、高净值个人等活跃交易用户量身定制"，开通条件页面**只写"具体开通事宜请联系开户营业部"**，未列门槛，未提 Python/miniQMT/xtquant |

**迅投官方社区的券商门槛表**（84 家券商）把**南京证券列在"高"档（≈ 100 万人民币）**，产品名标注为"南京 NXT 极速策略交易系统"。

**结论**：
- 南京证券**没有任何公开的 API / 开放平台**。
- QMT 路线对 10 万以内账户**走不通**（门槛约 100 万）。
- ⚠️ **网上的门槛表不一定准，建议用户打电话给开户营业部亲自确认一次**。有确切答复后再决定是否实现 `QmtAdapter`。

**其他可能路径及评估**：

| 路径 | 可行性 | 评估 |
|---|---|---|
| 手动维护 CSV | ✅ 立即可用 | 零依赖，但每次调仓要手敲 |
| **手机 APP 持仓截图 + 多模态 LLM 读图** | ✅ **用户选定** | 最贴合实际使用习惯；风险在 OCR 读错数字，靠校验层兜住 |
| 解析 PC 客户端导出的对账单 | ⚠️ 可行 | 需要用户先导出样本；中文券商导出多是 GBK 编码 xls，列名各家不同 |
| easytrader + 同花顺客户端 | ⚠️ 脆弱 | **easytrader 久未维护**；社区反馈同花顺已难驱动，有人转用 thstrader（版本也旧）。只能做 opt-in 只读 |
| QMT / miniQMT (xtquant) | ❌ 门槛不够 | 见上 |

### 2.3 OCR 方案

| 方案 | 评估 |
|---|---|
| **多模态 LLM 读图** | **用户选定**。对"中文股票名 + 数字表格"远比传统 OCR 准，且能理解表格语义（哪列是成本价、哪列是市值）。依赖 API，单张成本极低 |
| RapidOCR | PaddleOCR 模型转 ONNX，pip 装、无 paddle 依赖、Windows 友好、1–2 秒出结果、完全离线免费。中文准确率与 PaddleOCR 相当，比 PaddleOCR 快 4–5 倍且无内存泄漏。**但表格列对齐要自己写启发式，数字易错位** |
| PaddleOCR | 中文最强开源，但 Windows 装 paddlepaddle 重 |
| Tesseract | 中文表格效果差，不考虑 |

### 2.4 TradingAgents 在 A股的可行性

**上游 TradingAgents**（`E:\codes\quant-trading\TradingAgents`，vendored clone，v0.2.5）的数据层结构：

```
tradingagents/
├── dataflows/
│   ├── interface.py          ← route_to_vendor() 在第 134 行；VENDOR_METHODS 注册表
│   ├── config.py             ← get_vendor(category, method)
│   ├── y_finance.py  yfinance_news.py  reddit.py  stocktwits.py
│   ├── alpha_vantage*.py     ← 5 个文件
│   └── stockstats_utils.py
└── agents/utils/
    ├── core_stock_tools.py           ← 1 个 @tool: get_stock_data
    ├── technical_indicators_tools.py ← 1 个 @tool
    ├── fundamental_data_tools.py     ← 4 个 @tool
    ├── news_data_tools.py            ← 3 个 @tool
    └── agent_utils.py                ← Toolkit 聚合点
```

`route_to_vendor` 的实现（已读源码确认）：

```python
def route_to_vendor(method: str, *args, **kwargs):
    """Route method calls to appropriate vendor implementation with fallback support."""
    category = get_category_for_method(method)
    vendor_config = get_vendor(category, method)      # 读 default_config 的 "*_apis" 键
    primary_vendors = [v.strip() for v in vendor_config.split(',')]
    ...
    for vendor in fallback_vendors:
        vendor_impl = VENDOR_METHODS[method][vendor]  # ← 注册表
        ...
```

`default_config` 里当前是 `"core_stock_apis": "yfinance"`（注释写 `Options: alpha_vantage, yfinance`）。

> **这是一个干净的供应商插件架构。加 A股支持 = 往 `VENDOR_METHODS` 注册一个 `ashare` vendor + 把 config 的 `*_apis` 指过去，不需要逐个 patch 工具函数。**

**备选方案 TradingAgents-CN**（`hsliuping/TradingAgents-CN`）：中文分支，v1.1.0（2026-07-24），1244 commits，活跃维护，已支持 Tushare/AkShare/BaoStock + DeepSeek/Qwen/GLM/火山方舟，有 FastAPI + Vue 3 前端。
**未选用的原因**：带一整套 Web 栈和 Docker，偏重；`app/` 和 `frontend/` 商用授权受限（只有核心是 Apache 2.0）；**最关键是它自己管数据源** —— 会出现"回测用 BaoStock、LLM 看东财"的口径分裂。自写 vendor 可以让 LLM 直接读 quant-biga 自己的 parquet 缓存。

**A股的数据替代映射**：

| TradingAgents 原用 | A股替代 |
|---|---|
| yfinance 行情 | **quant-biga 自己的 parquet 缓存**（同一口径） |
| stockstats 指标 | 同上，stockstats 跑在同一份 parquet |
| yfinance/Alpha Vantage 基本面 | AKShare `stock_financial_abstract`、`stock_financial_analysis_indicator`、`stock_a_indicator_lg`(PE/PB/股息率) |
| Finnhub / Google News | AKShare `stock_news_em`(个股新闻) + `stock_notice_report`(公告) + `stock_research_report_em`(券商研报评级) |
| Reddit / StockTwits | **A股无对应物**。用东财股吧人气 `stock_comment_em` + 研报评级分布替代；取不到就明确返回"无数据"，**绝不让 LLM 编** |

---

## 3. 需求与决策记录

以下是与用户确认过的每一个决策，包含选项、选择和理由。**接手者若要推翻某条决策，先读理由。**

### D1 — 系统定位

**问**：这个系统最终要做到哪一步？
**选项**：半自动出清单 / 全自动实盘下单 / 纯研究回测 / **先半自动，架构预留全自动**
**选择**：**先半自动，架构预留全自动**
**含义**：默认走"输出下单清单，用户在南京证券 APP 手动执行"，但执行层做成 `ExecutionAdapter` 协议，将来插 QMT/easytrader 不改上层。
**理由**：南京证券无公开 API，QMT 门槛不够（见 §2.2），半自动是唯一当天就能跑起来的路径；adapter 化保证将来不用返工。

### D2 — 资金规模

**问**：账户资金规模？（决定选股数量、100 股约束、能否开 QMT）
**选择**：**10 万以内**
**含义**：
- `qbg_top_k = 3`（单槽约 3 万）
- **可负担性过滤必须按一手 = 100 股算**：`price × 100 ≤ slot_budget` → 只能买 300 元以下的票
- QMT（约 100 万门槛）确认走不通
- 最低 5 元佣金在 3 万单笔上只有 1.7 bp，不构成负担；**真正的敌人是换手率**

### D3 — 持仓数据接入

**问**：持仓数据怎么进系统？
**初次回答**：easytrader + 解析导出对账单 + 手动 CSV（三条都要）
**修正后的最终选择**：**手机 APP 持仓截图 → 多模态 LLM 读图 → 维护本地 CSV**，脚本独立写，放在系统的工具目录
**含义**：`tools/ocr_positions.py` 是独立工具，不 import 主流水线，可单独运行。手动 CSV 作为同一份文件的降级编辑方式；easytrader 降为 P9 可选。
**理由**：用户日常就是看手机 APP，截图是最低摩擦的输入方式；PC 客户端导出需要额外步骤且用户没有现成样本。

### D4 — LLM 层次与顺序

**问**：要不要 LLM 复核层？
**初次回答**：只要自动复盘调参 agent
**修正后的最终选择**：**要 TradingAgents 逐票复核（P7），自动复盘调参 agent 在它之后（P8）**
**理由**：用户明确要求两个都要，且有先后顺序。逐票复核直接影响每天的选股质量（在交易路径上），复盘调参 agent 在下游（不在交易路径上），所以先做前者。

### D5 — TradingAgents 基座

**问**：用哪个基座？
**选项**：**上游 TA + 自写 ashare vendor** / TradingAgents-CN / 两个都试
**选择**：**上游 TA + 自写 `ashare` vendor**
**理由**：见 §2.4。核心优势是 LLM 看到的行情就是 quant-biga 自己的 parquet 缓存，与模型/回测同一口径。quant-trading 已有 vendored clone + 幂等 patch 脚本的先例可直接照搬。prompt 是英文但 `TRADINGAGENTS_OUTPUT_LANGUAGE=中文` 能强制中文输出（quant-trading 实盘就这么用）。

### D6 — OCR 方案

**问**：持仓截图识别用哪种？
**选项**：**多模态 LLM 读图** / 本地 RapidOCR / LLM 主 + RapidOCR 备
**选择**：**多模态 LLM 读图**
**理由**：见 §2.3。中文股票名 + 数字表格场景下 vision 模型准确率和语义理解都远超传统 OCR。
**注意**：这意味着账户持仓截图会上传到 LLM 提供商。若用户后续介意，RapidOCR 备用方案的接口位置已在设计中预留（`qbg/portfolio/` 下多一个 source 实现即可）。

### D7 — 复核层 LLM 提供商

**问**：复核层用哪家 LLM？（A股新闻/公告/研报全中文，12 次调用/票成本敏感）
**选项**：**DeepSeek 为主** / 分层(国产快思考+大模型深思考) / 沿用 ZenMux relay / 先不定做成可切换
**选择**：**DeepSeek 为主**
**理由**：中文金融文本理解强；价格约为 gpt-5 类模型的 1/20；用户已有 `DEEPSEEK_API_KEY`。
**成本估算**：12 调用/票 × 9 候选 ≈ 110 调用/次调仓；两周一次 → 约 2900 调用/年；单次约 5–10k token → **年成本约 ¥50–150**。用 `TokenTracker` 记录真实值进日报，超预算就调 `min_rating` 或减候选数。

### D8 — Python 环境

**问**：环境怎么建？
**选择**：**conda 新建环境 `qbg`（python=3.11）**
**理由**：系统 Python 3.14 跑不了 qlib/lightgbm 生态；现有两个量化环境都是 3.11.15，对齐可减少踩坑；conda 是这台机器上所有项目的既定工作流。
**注意**：conda 不在 PATH，命令要用 `C:\ProgramData\miniconda3\Scripts\conda.exe` 全路径。

### D9 — 包名

**选择**：**`qbg`**（不叫 `qtf`）
**理由**：`qtf` 已被两个仓库以 editable 方式装进 `qtf` 和 `qtagent` 两个环境，重名会 import 撞车。

---

## 4. 系统架构

### 4.1 总体数据流

```
┌─ 数据获取（每日盘后 17:30 后）───────────────────────────────────┐
│                                                                  │
│  BaoStock ─┐                                                     │
│  AKShare  ─┼─► DailyBarSource 降级链 ─► parquet 增量缓存 ─┐      │
│  Mootdx   ─┘                            data/parquet/     │      │
│                                                            │      │
│  AKShare ─► 沪深300成分 / 申万行业 / 交易日历（低频，带缓存）│      │
│                                                            ▼      │
│                                              qlib bin (region=CN) │
└───────────────────────────────────────────────────────────┬──────┘
                                                             │
┌─ 打分与选股 ───────────────────────────────────────────────▼──────┐
│  qlib LightGBM + Alpha158（3 seed 集成）─► 全市场分数            │
│         │                                                         │
│         ├─► 申万一级行业中性化（组内 demean）                     │
│         ├─► 可负担性过滤（price × 100 ≤ 单槽预算）  ← A股关键     │
│         ├─► ST / 停牌 / 次新股 过滤                               │
│         └─► 迟滞 top-K（keep_rank）─► top 8~10 候选               │
└───────────────────────────────────────────────────────┬──────────┘
                                                         │
┌─ LLM 逐票复核（P7）────────────────────────────────────▼──────────┐
│  TradingAgents (LangGraph) + ashare vendor                        │
│  5 分析师 → 多空辩论 → 交易员 → 3 方风险辩论 → PM                 │
│  ≈12 次 DeepSeek 调用/票，输出 Buy/Overweight/Hold/Underweight/Sell│
│  只保留 ≥ min_rating(默认 Hold) ─► renormalize 回投资总额          │
│  fail-open：LLM 挂了不阻塞 qlib 信号                              │
└───────────────────────────────────────────────────────┬──────────┘
                                                         │
┌─ 市场择时 ─────────────────────────────────────────────▼──────────┐
│  沪深300 vs N 日均线；跌破 → 全部现金（N 由 5 年压力测试确定）     │
└───────────────────────────────────────────────────────┬──────────┘
                                                         │
┌─ 订单规划 ─────────────────────────────────────────────▼──────────┐
│  持仓快照（OCR CSV）+ 目标权重 ─► order_planner                   │
│  · 整手取整 floor(budget/price/100)×100                           │
│  · 限价按次日涨跌停区间夹逼                                       │
│  · 漂移带 3%（小偏离不动，省成本）                                │
└───────────────────────────────────────────────────────┬──────────┘
                                                         │
┌─ 风控闸链（8 道）──────────────────────────────────────▼──────────┐
│  硬闸（失败→全盘停）: mode / session / data_freshness             │
│  订单闸（只砍BUY，SELL放行）:                                      │
│      price_limit / suspension / st / t1 / lot                     │
│      max_position / min_cash / daily_loss_kill_switch             │
└───────────────────────────────────────────────────────┬──────────┘
                                                         │
┌─ 执行（adapter）───────────────────────────────────────▼──────────┐
│  AdvisoryAdapter (默认) ─► reports/orders/YYYY-MM-DD.{md,csv}     │
│  EasytraderAdapter (P9)   QmtAdapter (stub)                       │
└───────────────────────────────────────────────────────┬──────────┘
                                                         │
      ┌──────────────────────────────────────────────────┤
      │  用户次日开盘后在南京证券 APP 照单手动执行        │
      │  执行完再截图 ─► tools/ocr_positions.py ─► CSV    │
      └──────────────────────────────────────────────────┤
                                                         ▼
┌─ 记录与复盘 ──────────────────────────────────────────────────────┐
│  reconcile: 昨日清单 vs 今日持仓 ─► 实际成交 / 未成交 / 价差       │
│  SQLite store（派生，可重建）─► analysis/（零 LLM 事实）           │
│  中文 Markdown 日报                                                │
│  P8: Claude Agent SDK 复盘 ─► 假设 ─► 参数提议 ─► 回测把关 ─► 应用/回滚│
└───────────────────────────────────────────────────────────────────┘
```

### 4.2 目录结构

```
E:\codes\quant-biga\
├── plan.md                    ← 本文档
├── CLAUDE.md                  ← 给 AI 接手者的项目约定（含跨仓库禁令）
├── README.md
├── pyproject.toml             ← 包名 qbg，requires-python >=3.11,<3.13
├── environment.yml            ← conda env qbg
├── .env / .env.example
├── .gitignore                 ← 必须覆盖 .env data/portfolio/ tools/screenshots/ reports/ logs/ qlib/ TradingAgents/
│
├── src\qbg\
│   ├── __init__.py
│   ├── config.py              # pydantic-settings 单一配置源
│   ├── llm.py                 # 单一 LLM 配置点，默认 DeepSeek
│   │
│   ├── market\                # 【全新】A股规则层
│   │   ├── rules.py           #   板块识别、涨跌停、一手、tick
│   │   ├── calendar.py        #   交易日历 + 交易时段
│   │   └── t1.py              #   T+1 可卖量
│   │
│   ├── data\
│   │   ├── sources\
│   │   │   ├── base.py        #   DailyBarSource 协议
│   │   │   ├── baostock.py    #   主源
│   │   │   ├── akshare.py     #   补充源
│   │   │   ├── mootdx.py      #   备胎
│   │   │   └── tushare.py     #   预留，本期不启用
│   │   ├── cache.py           #   增量 parquet 读写
│   │   ├── universe.py        #   沪深300 成分 + 过滤 + 带日期快照
│   │   ├── industry.py        #   申万一级行业
│   │   └── qlib_dump.py       #   parquet → qlib bin
│   │
│   ├── model\
│   │   ├── train.py           # 移植：LightGBM + Alpha158，region=CN，N seed 集成
│   │   └── handlers.py
│   │
│   ├── strategy\
│   │   ├── predict.py         # 移植：从 mlruns 读最新预测
│   │   ├── topk_weights.py    # 移植 + 改 affordable_scores 为整手判据
│   │   └── regime.py          # 改写：沪深300 vs N日均线
│   │
│   ├── agents\                # 【P7】
│   │   ├── ashare_vendor.py   #   注册到 TradingAgents VENDOR_METHODS
│   │   ├── ticker_map.py      #   600519.SH ↔ TA symbol
│   │   ├── review.py          #   移植（几乎原样）
│   │   └── token_tracker.py   #   移植
│   │
│   ├── portfolio\
│   │   ├── base.py            #   PositionSource 协议 + Portfolio/Position dataclass
│   │   ├── ocr_source.py      #   读 tools/ 产出的 CSV
│   │   ├── manual.py          #   同一份 CSV 的手工编辑路径
│   │   ├── easytrader_src.py  #   【P9】opt-in 只读
│   │   └── reconcile.py       #   清单 vs 实际持仓 差异
│   │
│   ├── risk\
│   │   └── gates.py           # 移植闸链结构，8 道闸
│   │
│   ├── execution\
│   │   ├── base.py            #   ExecutionAdapter 协议
│   │   ├── advisory.py        #   默认：写下单清单
│   │   ├── easytrader_adapter.py  # 【P9】
│   │   ├── order_planner.py   #   移植 + 整手 + 涨跌停夹逼
│   │   └── fees.py            #   【改写】A股不对称费率
│   │
│   ├── backtest\
│   │   ├── engine.py          # 移植 + 四条 A股约束
│   │   ├── metrics.py         # 移植（纯函数，原样）
│   │   ├── regime_stress.py   # 移植
│   │   └── report.py
│   │
│   ├── store\                 # 移植 quant-agent
│   │   ├── schema.sql  db.py  etl.py  queries.py  backfill.py
│   ├── analysis\              # 移植 quant-agent（零 LLM 事实层）
│   ├── tuning\                # 移植 quant-agent（白名单 + 回测把关 + 回滚）
│   ├── agent\                 # 移植 quant-agent（Claude Agent SDK）
│   │
│   ├── report\
│   │   ├── daily_report.py    #   中文日报
│   │   ├── order_sheet.py     #   下单清单（md + csv）
│   │   └── snapshot.py
│   │
│   ├── orchestrator\
│   │   ├── daily_cycle.py     #   编排主干
│   │   └── run_marker.py      #   幂等标记
│   │
│   └── utils\
│       └── logging.py         # 移植：JSONL 结构化日志
│
├── tools\                     # 【全新】独立工具，不 import 主流水线
│   ├── ocr_positions.py       #   截图 → 多模态 LLM → 校验 → positions.csv
│   └── screenshots\           #   (gitignored) 临时截图
│
├── scripts\                   # 编号 CLI 入口，沿用参考仓库的编号习惯
│   ├── 00_market_check.py     #   今天是不是交易日
│   ├── 01_ingest.py           #   拉数据 → parquet → qlib bin
│   ├── 02_train.py            #   训练 LightGBM
│   ├── 03_predict.py          #   打分
│   ├── 04_plan_orders.py      #   选股 + 复核 + 风控 + 出清单（--dry-run）
│   ├── 05_report.py           #   日报
│   ├── 06_backtest.py         #   回测
│   ├── 07_regime_stress.py    #   择时参数压力测试
│   ├── 14_backfill_store.py   #   重建 SQLite
│   ├── 20_daily_review.py     #   【P8】agent 复盘
│   ├── 21_analyze.py          #   【P8】零 LLM 分析
│   ├── 22_params.py           #   【P8】参数应用/回滚
│   └── patch_tradingagents.py #   【P7】注册 ashare vendor（幂等）
│
├── configs\
│   ├── universe_hs300.txt     #   股票池
│   ├── risk_limits.yaml       #   风控参数（带中文注释）
│   ├── fee_profile.yaml       #   佣金/印花税/过户费
│   ├── tunable_params.yaml    #   【P8】参数白名单分层
│   └── workflow_cn_lgb.yaml   #   qlib workflow
│
├── data\
│   ├── parquet\               #   行情缓存（每股一个文件）
│   ├── qlib_bin\cn_data\      #   qlib 列存
│   ├── portfolio\
│   │   ├── positions.csv      #   当前持仓（OCR 或手工产出）
│   │   └── history\           #   持仓历史快照
│   ├── snapshots\             #   universe 快照、行业缓存、代码名称表
│   └── runs.db                #   SQLite 派生库
│
├── qlib\                      #   vendored clone（gitignored, pip install -e）
├── TradingAgents\             #   vendored clone（gitignored, pip install -e）
├── tests\                     #   pytest，全部离线
├── docs\                      #   架构、策略、运维、上线检查单
├── reports\
│   ├── YYYY-MM-DD.md          #   日报
│   └── orders\YYYY-MM-DD.{md,csv}  #   下单清单
├── logs\qbg.jsonl
├── mlruns\
└── run_daily.bat / setup_schedule.bat
```

### 4.3 关键接口定义

这些协议是各层之间的契约，**接手者应先定义它们再写实现**。

```python
# qbg/data/sources/base.py
class DailyBarSource(Protocol):
    name: str
    def fetch(self, code: str, start: str, end: str, adjust: str = "hfq") -> pd.DataFrame:
        """返回列: date, open, high, low, close, volume, amount, factor,
                  is_st(bool), is_suspended(bool)
           date 为 datetime64；缺数据返回空 DataFrame 而不是抛异常。"""
    def trade_dates(self, start: str, end: str) -> list[str]: ...

# qbg/portfolio/base.py
@dataclass
class Position:
    code: str            # 600519.SH
    name: str            # 贵州茅台
    qty: int             # 持仓股数
    sellable_qty: int    # 可卖股数（T+1 后才等于 qty）
    cost_price: float
    last_price: float
    market_value: float
    buy_date: date | None = None

@dataclass
class Portfolio:
    asof: date
    cash: float          # 可用资金
    total_assets: float
    positions: list[Position]
    source: str          # ocr | manual | easytrader

class PositionSource(Protocol):
    def snapshot(self) -> Portfolio: ...

# qbg/execution/base.py
@dataclass
class Order:
    code: str
    name: str
    side: Literal["BUY", "SELL"]
    quantity: int        # 必须是 100 的整数倍（SELL 清仓零股除外）
    limit_price: float   # 已按涨跌停夹逼
    ref_price: float     # 决策价
    reason: str

class ExecutionAdapter(Protocol):
    mode: str            # ADVISORY | PAPER | LIVE
    def get_portfolio(self) -> Portfolio: ...
    def submit(self, orders: list[Order]) -> list[dict]: ...
    def cancel_open_orders(self) -> int: ...
```

### 4.4 配置项清单

**`.env` / `.env.example`**（沿用 quant-trading 的"每项带注释写清为什么"的习惯）：

```bash
# --- 运行模式 ---
QBG_MODE=ADVISORY            # ADVISORY | PAPER | LIVE
I_CONFIRM_REAL=0             # LIVE 需要这个 =1 且 risk_limits.yaml 的 allow_live_mode=true

# --- 数据源 ---
QBG_DATA_SOURCES=baostock,akshare,mootdx   # 降级链顺序
QBG_INGEST_MAX_WORKERS=4
TUSHARE_TOKEN=                             # 预留，本期不用

# --- 股票池与策略 ---
QBG_TOP_K=3                  # 10万账户：单槽约3万
QBG_KEEP_RANK=15             # 迟滞：持仓股在前15名内就保留
QBG_REBALANCE_EVERY_DAYS=10  # 约两周。成本是小账户第一杀手
QBG_REBALANCE_CASH_TRIGGER=0.50
QBG_MARKET_SMA=100           # 待 07_regime_stress 压测后确定
QBG_ENSEMBLE_SEEDS=3
QBG_INDUSTRY_NEUTRAL=1
QBG_MIN_LIST_DAYS=60         # 次新股过滤，同时避开新股涨跌幅特殊规则
QBG_EXCLUDE_ST=1

# --- 持仓来源 ---
QBG_PORTFOLIO_SOURCE=ocr     # ocr | manual | easytrader

# --- TradingAgents 复核层（P7）---
QBG_AGENTS_ENABLED=0         # 默认关，P7 完成后打开
QBG_AGENTS_MIN_RATING=Hold   # 10万账户 k=3，放宽到 Hold 才够填满槽位
QBG_AGENTS_FAIL_OPEN=1       # LLM 挂了不阻塞 qlib 信号
QBG_AGENTS_RENORMALIZE=1
QBG_AGENTS_CANDIDATES=9      # 复核 top 9 而不是只复核 k=3 个
TRADINGAGENTS_LLM_PROVIDER=openai        # DeepSeek 走 OpenAI 兼容surface
TRADINGAGENTS_DEEP_THINK_LLM=deepseek-chat
TRADINGAGENTS_QUICK_THINK_LLM=deepseek-chat
TRADINGAGENTS_LLM_BACKEND_URL=https://api.deepseek.com
TRADINGAGENTS_MAX_DEBATE_ROUNDS=1
TRADINGAGENTS_MAX_RISK_ROUNDS=1
TRADINGAGENTS_OUTPUT_LANGUAGE=中文
DEEPSEEK_API_KEY=

# --- OCR 读图 ---
QBG_VISION_MODEL=            # 多模态模型名
QBG_VISION_BASE_URL=
QBG_VISION_API_KEY=

# --- 复盘调参 agent（P8）---
QBG_AGENT_ENABLED=0
QBG_AGENT_AUTOAPPLY=0
ANTHROPIC_API_KEY=
```

**`configs/risk_limits.yaml`**：

```yaml
allow_live_mode: false        # 第二把锁；改 true 之外还需 .env 的 I_CONFIRM_REAL=1
require_trading_session: false  # 顾问模式盘后跑，默认关
max_position_pct: 0.34        # 单票上限
min_cash_buffer_pct: 0.05     # 现金下限
max_daily_loss_pct: 0.03      # 单日止损熔断
stop_loss_pct: 0.08           # 个股止损
rebalance_drift_band: 0.03    # 权重偏离小于此值不动，省成本
order_slippage_pct: 0.002     # 限价相对现价的让价幅度
max_stale_days: 1             # data_freshness_guard：数据最多允许落后几个交易日
```

**`configs/fee_profile.yaml`**（⚠️ 待用户填实际费率）：

```yaml
commission_rate: 0.00025      # 万2.5，待用户确认南京证券实际费率
commission_min: 5.0           # 元，双边
stamp_tax_rate: 0.0005        # 0.05%，仅卖出
transfer_fee_rate: 0.00001    # 0.001%，双边
```

### 4.5 数据库 schema

移植 `E:\codes\quant-agent\src\qtf\store\schema.sql`（19 表 + 2 视图），做以下改动：

| 改动 | 说明 |
|---|---|
| `trd_env` → `mode` | 取值 `ADVISORY`/`PAPER`/`LIVE`。**保留它在每张账户级表主键里的位置** —— 顾问模式的假想净值和将来实盘净值绝不能被加总 |
| 新增表 `plans` | 每日下单清单（计划下了什么） |
| 新增表 `executions` | 用户实际成交（由 reconcile 从持仓差异推断，或手工回填） |
| `verdicts` 表 | 保留，P7 的 TradingAgents 评级写这里 |
| `prices` 表 | 增加 `is_st` / `is_suspended` / `limit_up` / `limit_down` 列 |
| `v_decisions` 视图 | join 里加上 `plans` 和 `executions`，回答"计划买的和实际买的差多少" |

**铁律**：这个库**永远是派生的**。所有行都能从 `logs/qbg.jsonl` + `data/parquet/` + `data/portfolio/history/` + `mlruns/` 用 `scripts/14_backfill_store.py --rebuild` 重建。交易路径不读它，删掉不影响下单。

---

## 5. A股特有规则详解

> **这一节是本项目的核心增量。两个参考仓库完全没有这些东西，且每一条写错都会导致下出无法成交的单或产生假的回测收益。**

### 5.1 板块识别与涨跌停

```
沪主板:    600 601 603 605        ±10%
深主板:    000 001                ±10%
原中小板:  002 003（已并入深主板） ±10%
创业板:    300 301                ±20%
科创板:    688 689(CDR)           ±20%
北交所:    430 83x 87x 88x 920    ±30%   ← 本期直接排除（沪深300 本就不含；且开户需50万+2年经验）

ST / *ST:  主板 ±5%
           创业板/科创板的 ST 股仍是 ±20%
```

涨跌停价 = `round(prev_close × (1 ± limit_pct), 2)`（四舍五入到分）。

**新股例外**：上市首日及前几日涨跌幅规则特殊（创业板/科创板前 5 日不设涨跌幅）。
→ **处理方式：`QBG_MIN_LIST_DAYS=60` 直接过滤次新股**，一次性规避这整类复杂性。

### 5.2 T+1

- 当日买入的股票**当日不可卖出**，次一交易日才可卖。
- `sellable_qty = qty - today_bought_qty`
- **必须同时进两个地方**：
  1. 风控闸 `t1_guard`（SELL 数量 ≤ `sellable_qty`）
  2. **回测引擎**（当日建仓的名字不能同日卖出）
- ⚠️ 只做第 1 处而漏掉第 2 处，回测收益会系统性偏高。

### 5.3 最小交易单位

- **买入必须是 100 股（一手）的整数倍**
- 卖出可以有零股，但**零股必须一次性全部卖出**（不能拆）
- 科创板例外：买入最少 200 股，之后可按 1 股递增 —— 本期沪深300 里科创板票很少，实现时按 100 处理并在 `rules.py` 留 TODO

**对 10 万账户的直接后果**：`qbg_top_k=3` → 单槽预算 ≈ 3 万 → **只能买单价 300 元以下的票**。这必须体现在 `affordable_scores` 里，判据从美股版的 `price ≤ budget` 改成 `price × 100 ≤ budget`。

### 5.4 费用（不对称）

| 项 | 费率 | 方向 | 说明 |
|---|---|---|---|
| 佣金 | 万 2.5（待确认南京证券实际） | 双边 | **最低 5 元** |
| 印花税 | 0.05% | **仅卖出** | 2023-08-28 由 0.1% 减半 |
| 过户费 | 0.001% | 双边 | 2022-04 起沪深统一 |

**往返总成本 ≈ 10 bp**（不含滑点）。

回测里 `cost_per_turnover` 必须拆成 `buy_cost` 和 `sell_cost` 两个数，不能用一个对称值。

**为什么这条很关键**：quant-trading 的 roadmap 记录了一个实测教训 —— 3000 美元账户日频调仓，固定 $1.99/单的费用 + 滑点让年化变成 −30%，改成月频才勉强打平。10 万人民币账户 + 10 bp 往返，同样的逻辑成立。**所以 `QBG_REBALANCE_EVERY_DAYS` 默认 10（约两周），不给日频。**

### 5.5 停牌

- BaoStock 的 `tradestatus` 字段：`1` = 正常交易，`0` = 停牌
- 停牌股：不下单；回测中不参与调仓，**权重冻结**（既不能买也不能卖）
- A股停牌可能持续数月，长期停牌股应从 universe 剔除

### 5.6 复权口径

| 用途 | 口径 |
|---|---|
| 模型训练、回测 | **后复权 (hfq)** —— 价格序列连续，收益率计算正确，不会因除权产生假跌 |
| 下单清单的限价和金额 | **不复权** —— 用户在 APP 里看到的是这个价 |

两套价格都要在 parquet 缓存里保留（或保留 `factor` 列供换算）。

### 5.7 执行时点与标签口径

**半自动模式的真实时序**：T 日盘后（17:30 后数据可用）跑流水线出清单 → **T+1 开盘后用户手动执行**。

→ **回测的成交假设必须是"次日开盘价"，不是收盘价。** 中间隔一个隔夜跳空，用收盘价会系统性高估。

→ **qlib 的标签也要相应改**。qlib CN 默认 label 是 `Ref($close,-2)/Ref($close,-1)-1`（T+1 收盘买、T+2 收盘卖）。本项目应改成 open-to-open：`Ref($open,-2)/Ref($open,-1)-1`，与执行现实对齐。

---

## 6. 复用与重写清单

### 6.1 直接复用（逐文件）

| 源文件 | 复用方式 |
|---|---|
| `quant-trading\src\qtf\config.py` | **模式照搬**：`PROJECT_ROOT` + pydantic-settings + 每个参数带注释写清"为什么是这个值 / 回测依据"。**这个注释习惯必须保持**，它是这套系统可维护性的关键 |
| `quant-trading\src\qtf\strategy\topk_weights.py` | `select_with_hysteresis` **原样复制**（纯泛型，与市场无关）；`topk_equal_weight` / `renormalize_weights` 只需改掉 `code_from_symbol(market="US")`；**`affordable_scores` 必须改判据为整手**（见 §5.3）；`tilt_by_rating` 保留备用 |
| `quant-trading\src\qtf\risk\gates.py` | **闸链两级设计原样保留**（硬闸 vs 只砍BUY的订单闸）；`max_position_guard` / `min_cash_guard` / `daily_loss_kill_switch` 逻辑原样；`GateResult` dataclass 原样 |
| `quant-trading\src\qtf\agents\review.py` | **几乎原样移植**：五档评级 + `_rank` + 429 指数退避(30/60/120s) + fail-open + `TokenTracker`，全部与市场无关。只换 `moomoo_to_yf` → A股 ticker_map |
| `quant-trading\src\qtf\agents\token_tracker.py` | 原样 |
| `quant-trading\scripts\patch_tradingagents.py` | **幂等 patch 框架照搬**（`MARKER` 检测 + 找不到预期代码就警告而非静默失败），patch 内容换成注册 `ashare` vendor |
| `quant-trading\src\qtf\backtest\engine.py` | 向量化 top-K 框架整体移植；`_holding_weights` / `forward_returns_from_close` / `_zscore_by_day` / IC 计算原样；`slippage_curve` 机制保留 |
| `quant-trading\src\qtf\backtest\metrics.py` | **原样**（纯函数：Sharpe / maxDD / 年化 / IC / RankIC / equity curve） |
| `quant-trading\src\qtf\backtest\regime_stress.py` | 移植，指数换沪深300 |
| `quant-trading\src\qtf\execution\order_planner.py` | 移植，加整手取整和涨跌停夹逼 |
| `quant-trading\src\qtf\orchestrator\run_marker.py` | 原样（幂等标记与市场无关） |
| `quant-trading\src\qtf\utils\logging.py` | **原样**（JSONL 结构化日志） |
| `quant-agent\src\qtf\store\*` | 表结构 + ETL + queries + backfill 整体移植，`trd_env`→`mode` |
| `quant-agent\src\qtf\analysis\*` | 零 LLM 事实层整体移植 |
| `quant-agent\src\qtf\tuning\*` | 白名单分层 + 8 项回测把关 + overlay + 回滚，整体移植 |
| `quant-agent\src\qtf\agent\*` | Claude Agent SDK 层整体移植，工具 SQL 换新表 |
| `quant-agent\src\qtf\llm.py` | 单一 LLM 配置点，默认指向 DeepSeek |

### 6.2 明确不复用（美股/moomoo 专属）

`utils/subprocess_runner.py`、`data/moomoo_kline.py`、`data/schema.py`、`data/earnings.py`、`execution/moomoo_executor.py`、`execution/fees.py`、`risk/market_hours.py`、`strategy/regime.py`（逻辑思路保留，实现重写）、`research/av_sentiment.py`、`research/sec_fundamentals.py`、`research/sectors.py`、`notify/`（本期不做邮件）。

### 6.3 全新编写

`market/`（全部）、`data/sources/`（全部）、`data/cache.py`、`data/universe.py`、`data/industry.py`、`portfolio/`（全部）、`execution/base.py`、`execution/advisory.py`、`execution/fees.py`、`agents/ashare_vendor.py`、`agents/ticker_map.py`、`tools/ocr_positions.py`、`report/order_sheet.py`。

---

## 7. 分阶段实施

### P0 — 项目骨架

**目标**：能 `import qbg`，能 `pytest`。

**任务**：
1. **把本文档复制为 `E:\codes\quant-biga\plan.md`**
2. `C:\ProgramData\miniconda3\Scripts\conda.exe create -n qbg python=3.11 -y`
3. `git init`；写 `.gitignore`（必须覆盖 `.env`、`data/portfolio/`、`tools/screenshots/`、`reports/`、`logs/`、`qlib/`、`TradingAgents/`、`mlruns/`、`data/`）
4. `pyproject.toml`（包名 `qbg`，`requires-python = ">=3.11,<3.13"`，ruff line-length 110 / select `E,F,I,B,UP`，pytest `testpaths=["tests"]`）
5. `environment.yml`
6. 目录骨架 + `src/qbg/config.py` + `src/qbg/llm.py` + `src/qbg/utils/logging.py`
7. `.env.example`（§4.4 的完整清单，每项带中文注释）
8. `CLAUDE.md`：项目约定 + **跨仓库禁令**（不得写入 quant-trading / quant-agent，不得动它们的计划任务和 conda 环境）
9. 项目级 `.claude/settings.json` 加常用只读命令允许项，减少权限提示

**验收**：`conda run -n qbg python -c "import qbg; print(qbg.__file__)"` 成功；`pytest` 通过（哪怕只有一个占位测试）。

---

### P1 — 数据层

**目标**：一条命令把沪深300 五年日K 落到本地，且断网重跑走缓存。

**任务**：
1. `data/sources/base.py` 定义 `DailyBarSource` 协议（§4.3）
2. `baostock.py`：登录/登出管理、`query_history_k_data_plus`、字段映射（`tradestatus`→`is_suspended`、`isST`→`is_st`）、复权 flag
3. `akshare.py`：`stock_zh_a_hist`（adjust `""`/`"qfq"`/`"hfq"`）+ `stock_info_a_code_name`（全市场代码名称表，OCR 反查要用）
4. `mootdx.py`：备胎
5. `cache.py`：每股一个 parquet，**增量 top-up**（读 last_date，只拉之后的），支持强制全量重拉
6. 降级链：主源失败/返回空 → 下一个源；**所有元数据调用 fail-soft 到上次缓存**
7. `market/calendar.py`：交易日历（BaoStock `query_trade_dates` 为主，AKShare 为备），带本地缓存
8. `data/universe.py`：沪深300 成分（AKShare）+ 过滤（ST / 停牌 / 上市 < `QBG_MIN_LIST_DAYS` / 北交所）+ **带日期的快照落盘**（缓解生存者偏差，见 §8.1）
9. `data/industry.py`：申万一级行业，带缓存
10. `data/qlib_dump.py`：parquet → qlib bin（`region=cn`），沿用 quant-trading `ingest.py` 调 `dump_bin.py` 的做法
11. `scripts/01_ingest.py`

**验收**：
- `python scripts/01_ingest.py` 拉完 300 只 5 年日K，产出 `data/parquet/*.parquet` 和 `data/qlib_bin/cn_data/`
- 再跑一次只增量、秒级完成
- 离线单测：复权口径、停牌/ST 标志解析、降级链（mock 主源抛异常验证走备源）、缓存增量边界

---

### P2 — A股规则层 + 回测引擎

**目标**：回测数字可信。

**任务**：
1. `market/rules.py`：`board_of(code)`、`price_limit(code, prev_close, is_st)`、`LOT_SIZE`、`round_lot(qty)`、`is_st_name(name)`
2. `market/t1.py`：`sellable_qty(position, today_buys)`
3. `execution/fees.py`：A股不对称费率（§5.4），读 `configs/fee_profile.yaml`
4. `backtest/metrics.py`：原样移植
5. `backtest/engine.py`：移植向量化框架，**加四条 A股约束**：
   - T+1（当日建仓不可同日卖）
   - 涨跌停（次日开盘涨停 → 买单不成交、信号作废；跌停 → 卖单不成交、被迫持有）
   - 停牌（不参与调仓，权重冻结）
   - 不对称费率（`buy_cost` / `sell_cost` 分离）
   - **成交价用次日开盘价**（§5.7）
6. 保留 `slippage_curve`：在 10 bp 基础成本上再叠 0/10/20/30 bp
7. `backtest/report.py`：中文回测报告
8. `scripts/06_backtest.py`

**验收**：
- 手造已知答案的小样本，逐条验证四条约束确实生效（这是本阶段最重要的验收）
- 固定 seed 跑两遍，指标逐位一致
- 回测报告含：夏普、最大回撤、年化、IC、RankIC、平均换手、滑点敏感曲线

---

### P3 — 模型与选股

**目标**：产出有正向 IC 的日度分数。

**任务**：
1. `configs/workflow_cn_lgb.yaml`：qlib init（`provider_uri ./data/qlib_bin/cn_data`，`region: cn`），Alpha158 handler，LGBModel，**label 改 open-to-open**（§5.7），train/valid/test 分段
2. `model/train.py`：移植（**注意 quant-trading 刻意绕开 qlib 的 `workflow()`/`PortAnaRecord`，因为 Windows 上 joblib 拆卸会崩** —— 照做），N seed 集成
3. `strategy/predict.py`：从 mlruns 读最新 `pred.pkl`
4. `strategy/topk_weights.py`：移植 + `affordable_scores` 改整手判据
5. `strategy/regime.py`：沪深300 vs N 日均线
6. `scripts/07_regime_stress.py`：N ∈ {20,50,100,200} 跑 5 年 model-free 压力测试，**用结果定 N，不拍脑袋**
7. 行业中性化（申万一级组内 demean）
8. `scripts/02_train.py` / `03_predict.py`

**验收**：Rank IC > 0 且三个 seed 方向一致；regime 的 N 有压测依据并写进 `config.py` 注释；行业中性化前后的回测对比记录在 `docs/`。

---

### P4 — 风控闸链 + 下单清单

**目标**：产出一份可以直接照着敲的清单。

**任务**：
1. `risk/gates.py`：移植闸链结构，实现 8 道闸
   - 硬闸：`mode_guard`（双锁）、`session_guard`（顾问模式默认关）、`data_freshness_guard`
   - 订单闸：`price_limit_guard`、`suspension_guard`、`st_guard`、`t1_guard`、`lot_guard`、`max_position_guard`、`min_cash_guard`、`daily_loss_kill_switch`
2. `execution/base.py`：`ExecutionAdapter` 协议 + `Order` dataclass
3. `execution/order_planner.py`：目标权重 → 订单
   - 整手取整 `floor(budget / price / 100) × 100`
   - 限价按次日涨跌停区间夹逼
   - 漂移带 3%
4. `execution/advisory.py`：写 `reports/orders/YYYY-MM-DD.{md,csv}`
5. `report/order_sheet.py`：清单格式 —— 代码 / 名称 / 方向 / 股数 / 限价 / 预估金额 / 预估费用 / 理由
6. `scripts/04_plan_orders.py`（含 `--dry-run`）

**验收**：
- 用手造 `positions.csv` 跑通，人工核对：股数是 100 整数倍、限价在涨跌停内、总金额不超可用现金
- **每道闸都有单测**（通过 + 拦截两种情形）
- 特别验证"SELL 永远放行"：构造一个所有订单闸都失败的场景，确认 SELL 仍在输出里

---

### P5 — 持仓 OCR 工具

> **本阶段不依赖 P2–P4，P1 之后随时可以提前做。** 如果想早点用上，可以插到 P2 之前。

**目标**：截图进，可信的 `positions.csv` 出。

**任务** —— `tools/ocr_positions.py`：

1. **CLI**：`python tools/ocr_positions.py 截图1.png [截图2.png ...] [--yes] [--dry-run]`
2. **读图**：多模态 LLM，输出结构化 JSON
   ```json
   {"asof": "2026-08-10", "总资产": 98765.43, "可用资金": 12345.67,
    "positions": [{"名称": "贵州茅台", "代码": null, "股数": 100,
                   "可用股数": 100, "成本价": 1450.0, "现价": 1480.0,
                   "市值": 148000.0, "盈亏": 3000.0}]}
   ```
   支持多张截图合并（持仓列表要滚动时分屏截，需去重）
3. **名称→代码反查**：⚠️ **手机 APP 持仓页经常只显示股票名不显示代码**，这是个真实的坑。用 P1 缓存的 `stock_info_a_code_name` 全市场表做精确匹配；**歧义时报错，绝不猜**
4. **校验层（强制，比 OCR 本身更重要）**：
   - 名称在全市场表里唯一匹配
   - 股数是 100 整数倍（零股单独标记不报错）
   - `|市值 − 股数 × 现价| / 市值 < 1%`
   - `|总资产 − (可用资金 + Σ市值)| / 总资产 < 1%`
   - 成本价 > 0
   - 现价落在当日涨跌停区间内
   - **任一项不过 → 打印出问题行并拒绝写入**
5. **人工确认**：解析结果打印成对齐表格，`y/N` 确认后才落盘（`--yes` 可跳过但默认不跳）
6. **落盘**：`data/portfolio/positions.csv`（当前）+ `data/portfolio/history/positions_YYYY-MM-DD.csv`（历史）
7. **隐私**：截图不入库，处理后可选删除；`.gitignore` 覆盖 `data/portfolio/` 和 `tools/screenshots/`

**同时完成**：
- `portfolio/base.py`（协议 + dataclass）、`ocr_source.py`、`manual.py`
- `portfolio/reconcile.py`：昨日清单 vs 今日持仓 → 实际成交 / 未成交 / 价差。**半自动模式下这是唯一能量化执行质量的东西，也是 P8 复盘 agent 的关键事实来源**

**验收**：
- 用真实截图跑通
- **对抗测试：故意改坏一位数字、删掉一行、改掉一个股票名 → 校验层必须拦下，不能静默通过**

---

### P6 — 编排 + 日报 + store

**目标**：一条命令跑完全流程，当天记录完整可查。

**任务**：
1. `orchestrator/daily_cycle.py`：编排主干（是否交易日 → 是否调仓日 → 拉数 → 打分 → 选股 → 风控 → 出清单 → 日报 → 写 store）
2. `orchestrator/run_marker.py`：移植（幂等）
3. `report/daily_report.py`：中文 Markdown 日报
4. `store/`：移植 schema + ETL + queries + backfill，`trd_env`→`mode`，加 `plans` / `executions` 表
5. `scripts/00_market_check.py` / `05_report.py` / `14_backfill_store.py`
6. `run_daily.bat` + `setup_schedule.bat`（Windows 计划任务，建议 17:45 之后跑，等 BaoStock 数据齐）

**验收**：`run_daily.bat` 一键跑完；`runs.db` 有当天完整记录；`14_backfill_store.py --rebuild` 能从日志重建出一致的库。

---

### P7 — TradingAgents 逐票复核

**目标**：候选股拿到带中文理由的五档评级。

**任务**：
1. `git clone https://github.com/TauricResearch/TradingAgents` 到 `TradingAgents/`（gitignored），`pip install -e`
2. `qbg/agents/ashare_vendor.py`：实现四类方法（§2.4 映射表），**行情直接读 quant-biga 自己的 parquet 缓存**
3. `scripts/patch_tradingagents.py`：幂等 patch，往 `VENDOR_METHODS` 注册 `ashare` vendor，并把 `default_config` 的 `*_apis` 指过去
4. `qbg/agents/ticker_map.py`：`600519.SH` ↔ TA symbol（用 6 位码作 symbol，vendor 内部判交易所）
5. `qbg/agents/review.py` + `token_tracker.py`：移植
6. 接进 `daily_cycle`：qlib 出 top `QBG_AGENTS_CANDIDATES`(默认 9) → 复核 → 保留 ≥ `min_rating` → `renormalize_weights` → 取前 k=3
7. DeepSeek 配置（§4.4）

**⚠️ 10 万账户的设计要点**：k=3 时若 LLM 砍掉 2 个只剩 1 个，`renormalize_weights` 会把权重放大到超过 `max_position_pct` 而被 cap 截断，导致大量现金闲置。所以：
- **复核 top 8–10 个候选而不是只复核 3 个**（`QBG_AGENTS_CANDIDATES=9`）
- `QBG_AGENTS_MIN_RATING` 默认放宽到 `Hold`（quant-trading 实盘就是 Hold）

**验收**：
- 对一只真实 A股跑出带中文理由的五档评级
- `TokenTracker` 报出真实 token 和成本
- 情绪面取不到数据时，分析师**照实说"无数据"而不是编造**（人工抽查转录）
- 同一批候选跑三次看评级稳定性（quant-agent 有 `verdict_stability` 分析可移植）

---

### P8 — 自动复盘调参 agent

**目标**：agent 每天自动复盘，并在回测把关下提出/应用/回滚参数改进。

**任务**：
1. 移植 `quant-agent` 的 `analysis/`（零 LLM 事实层）
2. 移植 `tuning/`：`configs/tunable_params.yaml` 白名单分层（`A / A_risk / A_slow / B / frozen`）+ 8 项回测把关 + overlay 应用 + 审计 + 回滚
3. 移植 `agent/`：Claude Agent SDK、MCP 工具（SQL 换新表）、3 级权限、PreToolUse 熔断、转录脱敏
4. `scripts/20_daily_review.py` / `21_analyze.py` / `22_params.py`
5. `.claude/` 表面：subagent + 斜杠命令（参考 quant-agent 的 8 个）

**熔断要求**（照搬 quant-agent 的思路）：agent **禁止**修改 `QBG_MODE`、`I_CONFIRM_REAL`、`allow_live_mode`，禁止读 `.env`，禁止写 `data/portfolio/`。

**验收**：
- 手工制造已知异常（改坏某天持仓）→ `analysis/` 能诊断出来
- agent 能开出带 `discriminator` 的 hypothesis
- **参数改动被回测把关拦下时不会被应用**（这是最关键的负向测试）
- 回滚能一键还原

---

### P9（可选）— easytrader 半自动执行

**前提**：P0–P8 稳定运行，且用户明确要求。

**任务**：`portfolio/easytrader_src.py`（只读持仓）+ `execution/easytrader_adapter.py`（下单）。

**纪律**：先**只读持仓跑两周无异常**，再考虑开下单。任何异常降级到 OCR/manual 源，**不允许让主流水线崩掉**。

---

### 阶段依赖图

```
P0 ──► P1 ──┬──► P2 ──► P3 ──► P4 ──┬──► P6 ──► P7 ──► P8
            │                        │
            └──► P5 ─────────────────┘        P9（可选，任意时间）
```

P1–P2 是工作量和风险的大头。**P0–P6 打通即可每天产出可执行的下单清单**（系统已经有用）；P7 提升选股质量；P8 加自我改进回路。

---

## 8. 风险与限制

### 8.1 生存者偏差（数据层，中等严重）

AKShare 只提供**当前**指数成分股。用今天的沪深300 回测过去 5 年会系统性高估收益（今天在 300 里的公司，是过去 5 年活得好的那批）。

**缓解**：universe 落成**带日期的快照**，从第一天开始积累；回测报告里**明确标注这一偏差**；**早期回测数字不能当真实预期**。真正的历史成分股需要付费数据源（Tushare 高积分档有 `index_weight`）。

### 8.2 AKShare 限频 / 封 IP（数据层，高频发生）

已用"BaoStock 主源 + 增量缓存 + 降级链"大幅缓解，但元数据和 P7 复核层的新闻/财务仍走 AKShare。

**缓解**：所有 AKShare 调用 **fail-soft 到上次缓存**；加请求间隔；绝不并发轰炸。

### 8.3 OCR 读错数字（持仓层，最直接的资金风险）

读错持仓 → 下错单。这是本系统里**最短的错误传导路径**。

**缓解**：§7-P5 的六条校验 + 人工确认是**强制的**，任一项不过就拒绝写入。P5 的验收明确包含对抗测试。

### 8.4 南京证券无 API（执行层，已知且已规避）

QMT/NXT 门槛约 100 万。**建议用户打电话给开户营业部亲自确认一次**（网上门槛表不一定准）。有确切答复后再决定是否实现 `QmtAdapter`。

### 8.5 成本吃掉 alpha（策略层，小账户第一杀手）

10 bp 往返 + 滑点。

**缓解**：默认两周调仓（`QBG_REBALANCE_EVERY_DAYS=10`）；漂移带 3%；迟滞选股降换手；**回测只看净成本后的曲线，不看毛收益**。

### 8.6 执行延迟与跳空（策略层）

盘后出信号、次日开盘后人工执行，中间隔一个隔夜跳空。

**缓解**：回测用**次日开盘价**成交假设；qlib label 改 open-to-open（§5.7）。

### 8.7 easytrader 久未维护（P9）

同花顺客户端升级即失效。所以是可选项，不是依赖。

### 8.8 LLM 幻觉（P7）

情绪面在 A股无 Reddit/StockTwits 对应物，取不到数据时若让 LLM 自由发挥，会编造舆情。

**缓解**：vendor 取不到就**明确返回"无数据"**，并在 prompt 里要求分析师照实说明；人工抽查转录。

---

## 9. 验证策略

### 9.1 离线单测（铁律：不联网、不碰券商、不调 LLM）

两个参考仓库的所有测试都是离线的，本项目沿用。覆盖：

- 涨跌停计算，**逐板块**（主板/创业板/科创板/ST）
- 整手取整（含边界：预算刚好不够一手、刚好够一手）
- T+1 可卖量
- **8 道闸各自的通过与拦截**，特别是"SELL 永远放行"
- 数据源降级链（mock 主源异常）
- 复权口径（后复权序列在除权日连续）
- 费率不对称（买卖成本不等、最低 5 元生效）
- OCR 校验层**每一条规则**
- `ashare` vendor 的返回结构
- 回测四条 A股约束（用手造的已知答案样本）

### 9.2 回测复现性

固定 seed 跑两遍 `scripts/06_backtest.py`，**指标逐位一致**。不一致说明有未受控的随机性或状态泄漏。

### 9.3 端到端 dry-run

用手造 `positions.csv` 跑 `04_plan_orders.py --dry-run`，人工核对：股数 100 整数倍、限价在涨跌停内、总金额不超可用现金、SELL 数量不超可卖量。

### 9.4 OCR 对抗测试

真实截图跑通后，**故意改坏一位数字 / 删一行 / 改一个股票名**，确认校验层拦下而非静默通过。

### 9.5 纸上跟踪（上线前必做）

正式照单执行前**空跑 4 周**：每天出清单但不执行，用次日实际开盘价回填，看假想净值曲线是否和回测同量级。

**差太多说明回测有偏差** —— 回头查 §5 的四条约束和 §8.1 的生存者偏差。

### 9.6 复核层验证（P7）

同一批候选跑三次看评级稳定性；`TokenTracker` 实测成本对照 §3-D7 的估算，超预算就调 `min_rating` 或减候选数。

### 9.7 agent 层验证（P8）

手工制造已知异常 → 确认 `analysis/` 能诊断、agent 能开 hypothesis、**参数改动被回测把关拦下时不会被应用**、回滚能还原。

---

## 10. 待补充的输入

| # | 需要的东西 | 用途 | 何时需要 |
|---|---|---|---|
| 1 | **一张手机 APP 持仓截图** | 确定 vision prompt 和字段映射；**特别是确认持仓页到底显不显示股票代码** | P5 |
| 2 | **南京证券实际佣金费率**（万几、是否最低 5 元） | 填 `configs/fee_profile.yaml`，回测成本模型直接用 | P2 |
| 3 | （可选）**打电话确认南京证券 NXT/QMT 开通门槛** | 决定是否实现 `QmtAdapter` | P9 之前 |

**这三样都不阻塞 P0–P4 开工。**

---

## 11. 附录

### 11.1 术语表

| 术语 | 含义 |
|---|---|
| T+1 | A股当日买入次日才可卖出 |
| 一手 | 100 股，买入的最小单位 |
| 前复权 (qfq) / 后复权 (hfq) | 除权除息的价格调整方向。后复权保持历史价不变、调整近期价；前复权反之 |
| 涨跌停 | 单日价格波动上限，触及后只能单向成交 |
| ST / *ST | 特别处理 / 退市风险警示，涨跌幅限制更严 |
| 申万一级行业 | 申银万国行业分类第一级，A股最常用的行业标准 |
| Rank IC | 预测排名与实际收益排名的相关系数，横截面选股的核心指标 |
| 迟滞选股 (hysteresis) | 持仓股只要还在前 N 名就保留，降低换手 |
| QMT / miniQMT | 迅投的量化交易终端 / 其 Python API (xtquant) |
| Ptrade | 恒生的量化交易终端，另一条券商路线 |
| Alpha158 | qlib 内置的 158 个手工因子库 |
| fail-open | 组件失败时放行而非阻塞（本项目 LLM 复核层的策略） |
| fail-soft | 组件失败时降级到缓存/默认值（本项目元数据调用的策略） |

### 11.2 参考链接

- Tushare 积分与频次权限对应表 — https://tushare.pro/document/1?doc_id=290
- AKShare 股票数据文档 — https://akshare.akfamily.xyz/data/stock/stock.html
- BaoStock 知识库 — https://www.baostock.com/mainContent
- 迅投 QMT 社区·各券商支持情况及资金门槛 — https://www.xuntou.net/forum.php?mod=viewthread&tid=232
- 南京证券 NXT 极速策略交易系统 — https://www.njzq.com.cn/njzq/business/nxt.html
- 南京证券软件下载 — https://www.njzq.com.cn/njzq/software/index.jsp
- RapidOCR 文档 — https://rapidai.github.io/RapidOCRDocs/main/
- TradingAgents（上游） — https://github.com/TauricResearch/TradingAgents
- TradingAgents-CN（中文分支，本期未选用） — https://github.com/hsliuping/TradingAgents-CN
- Ptrade API 文档 — https://ptradeapi.com/

### 11.3 本地参考路径

```
E:\codes\quant-trading\          美股实盘骨架（只读参考，禁止修改）
E:\codes\quant-agent\            agent 复盘调参层（只读参考，禁止修改）
E:\codes\quant-trading\TradingAgents\   已 vendored 的上游 TradingAgents，可读源码
C:\ProgramData\miniconda3\Scripts\conda.exe   conda（不在 PATH）
C:\Users\gjq00\.conda\envs\      现有环境
C:\Users\gjq00\.claude\settings.json   全局权限配置
```

### 11.4 关键源码位置速查

| 要找什么 | 去哪看 |
|---|---|
| 闸链两级设计 | `quant-trading\src\qtf\risk\gates.py::run_all_gates` |
| 可负担性过滤 | `quant-trading\src\qtf\strategy\topk_weights.py::affordable_scores` |
| 迟滞选股 | 同上 `::select_with_hysteresis` |
| TradingAgents 复核封装 | `quant-trading\src\qtf\agents\review.py::review_candidates` |
| 幂等 patch 框架 | `quant-trading\scripts\patch_tradingagents.py` |
| TradingAgents 供应商注册表 | `quant-trading\TradingAgents\tradingagents\dataflows\interface.py:134` |
| 向量化回测 | `quant-trading\src\qtf\backtest\engine.py` |
| store schema | `quant-agent\src\qtf\store\schema.sql` |
| T+1 结算感知下单 | `quant-trading\src\qtf\orchestrator\daily_cycle.py::_submit_settled` |
| 参数配置注释风格 | `quant-trading\src\qtf\config.py`（全文都是范例） |
