# quant-biga — A股半自动量化系统

数据 → 因子模型 → LLM 复核 → 风控闸 → **下单清单** → 人工执行 → 复盘。

针对 **10 万以内的 A股账户**，券商南京证券（无公开 API，所以走半自动）。
架构沿用 `quant-trading` / `quant-agent` 两套美股系统，数据层、执行层和
市场规则层针对 A股重写。

> ⚠️ **这是研究/教学骨架，不构成投资建议。** 正式照单执行前请先空跑 4 周
> 纸上跟踪（见 `plan.md` §9.5）。

---

## 30 秒架构

```
BaoStock/AKShare ─► parquet 增量缓存 ─► qlib bin
                                          │
                          qlib LightGBM + Alpha158（3 seed 集成）
                                          │
                    申万行业中性 → 一手可负担性过滤 → 迟滞 top-K
                                          │
                TradingAgents 逐票复核闸（第三方中转，P7；失败放行）
                                          │
                          沪深300 择时 → 订单规划（整手 + 涨跌停夹逼）
                                          │
                                    8 道风控闸
                                          │
                        reports/orders/YYYY-MM-DD.{md,csv}
                                          │
                     你在南京证券 APP 照单执行 → 截图 → OCR → CSV
                                          │
                 reconcile → SQLite store → 中文日报 → 复盘 agent（P8）→ 邮件
```

---

## 和两个兄弟仓库的区别

| | quant-trading | quant-agent | **quant-biga** |
|---|---|---|---|
| 市场 | 美股 | 美股 | **A股** |
| 数据源 | moomoo OpenD | moomoo OpenD | **BaoStock + AKShare** |
| 执行 | moomoo 实盘下单 | moomoo 模拟盘 | **下单清单，人工执行** |
| 持仓 | broker API | broker API | **APP 截图 → 多模态 LLM** |
| 包名 | `qtf` | `qtf` | `qbg` |
| conda env | `qtf` | `qtagent` | `qbg` |

那两个仓库是**只读参考**，本项目绝不写入它们（详见 `CLAUDE.md`）。

---

## 快速开始

```bash
# 1. 环境
conda env create -f environment.yml      # 或 conda create -n qbg python=3.11
conda activate qbg
pip install -e ".[data,model,llm,dev]"

# P3 / P7 的 vendored 依赖
git clone https://github.com/microsoft/qlib qlib
pip install -e qlib
git clone https://github.com/TauricResearch/TradingAgents TradingAgents
pip install -e TradingAgents
python scripts/patch_tradingagents.py

# 2. 配置（仓库已生成 gitignored 的 .env；首次克隆时执行下一行）
cp .env.example .env
# 在 .env 填 QBG_LLM_BASE_URL / QBG_LLM_API_KEY / 两档模型 id
python scripts/24_llm_check.py            # 只检查配置，不联网
python scripts/24_llm_check.py --probe    # 最小在线连通性测试
python scripts/24_llm_check.py --probe-vision  # 无隐私纯色图多模态测试

# 3. 拉数据（P1 完成后可用）
python scripts/01_ingest.py

# 4. 训练与模型回测
python scripts/02_train.py --seeds 3
python scripts/06_backtest.py --scores model

# 5. 出下单清单（P4 完成后可用）
python scripts/04_plan_orders.py --dry-run

# 6. 一键日循环（增量拉数 → 选股 → 风控 → 清单 → 日报 → store）
run_daily.bat

# 7. 持仓截图（默认人工确认；截图会发送到配置的 Vision 端点）
python tools/ocr_positions.py screenshot.png --dry-run

# 8. 验证派生库可重建
python scripts/14_backfill_store.py --rebuild

# 9. 每日复盘（连通性通过后先把 .env 的 QBG_AGENT_ENABLED 改为 1）
python scripts/20_daily_review.py

# 10. 邮件通知（先在 .env 填 SMTP_*，再设 NOTIFY_EMAIL_ENABLED=1）
python scripts/27_notify.py --test
python scripts/27_notify.py --date 2026-08-10 --dry-run
```

## 第三方中转站与模型

P5 OCR、P7 TradingAgents 和 P8 每日复盘共用 `QBG_LLM_*`。中转站必须兼容
`POST /v1/chat/completions`；`QBG_LLM_BASE_URL` 通常需要包含末尾 `/v1`。
TradingAgents 会自动使用 `openai_compatible` provider，避免误走多数中转站没有的
`/v1/responses`。

TradingAgents 在本项目中只作**复核闸**：qlib 仍是唯一主信号，评级低于 `Hold`
才剔除候选；中转站限流、超时或返回异常时按 `QBG_AGENTS_FAIL_OPEN=1` 放行，
不会因为 LLM 不可用而阻断整天的量化结果。原始评级允许波动，真正需要稳定的是
“是否跨过 Hold 阈值”的闸门结论。

- `QBG_LLM_MODEL=gpt-4o`：复盘和最终判断，优先质量。
- `QBG_LLM_MODEL_QUICK=gpt-4o-mini`：批量分析和 OCR，优先成本与延迟。
- 若中转站使用 `openai/gpt-4o` 之类命名空间，必须按其控制台模型 id 修改。
- OCR 会上传持仓截图；不信任中转站时设 `QBG_PORTFOLIO_SOURCE=manual`。

复盘模型只接收本地整理的 facts JSON，只返回结构化结论；没有 shell、文件、数据库
或下单权限。参数建议只写入报告，仍需人工运行八项回测闸，默认
`QBG_AGENT_AUTOAPPLY=0`。

## 邮件通知

邮件机制参考 `quant-agent`：日流程结束后读取最终落盘的 Markdown 日报，同时发送
纯文本和适配手机表格的 HTML；主题直接包含模式和运行结论。HTML 采用单列摘要卡，
正文依次呈现概览、一句话结论、账户与持仓、量化选择与 TradingAgents 复核、订单
意见、运行健康和自动复盘；订单会明确标记风控是否允许以及是否实际提交。465/8465
使用隐式 TLS，其他端口使用 STARTTLS。未配置时是 no-op，SMTP 失败只记日志，绝不
改变订单或退出状态。

当前只实现安全边界更窄的**出站通知**，没有移植 quant-agent 的入站邮件命令通道；
邮件回复不能重跑、调参或进入交易路径。配置和测试命令见 `.env.example` 与
`scripts/27_notify.py --help`。

---

## A股特有的坑

这些是两个美股仓库完全没有、且写错就会下出无法成交的单的地方：

- **T+1** —— 当日买入当日不可卖。风控闸和回测引擎**两处**都要有
- **一手 = 100 股** —— 10万账户 k=3 → 单槽3万 → 只能买 300 元以下的票
- **涨跌停** —— 主板 ±10%，创业板/科创板 ±20%，主板 ST ±5%
- **费用不对称** —— 印花税 0.05% 只在卖出时收，往返总成本约 10bp
- **成交价用次日开盘价** —— 盘后出信号、次日人工执行，中间隔一个跳空

完整说明见 `plan.md` §5。

---

## 文档

| 文件 | 内容 |
|---|---|
| `plan.md` | **完整的背景、调研结论、决策记录、架构、分阶段计划**。接手先读这个 |
| `CLAUDE.md` | 纪律与操作细节（跨仓库禁令、代码约定、当前进度） |
| `src/qbg/config.py` | 每个策略参数及其"为什么是这个值" |
| `configs/*.yaml` | 风控参数、费率，同样带理由注释 |
| `docs/p3-model-validation.md` | 三 seed IC、行业中性化对照和 SMA 压测实测记录 |
| `docs/online-llm-validation.md` | 中转站、Vision、P7 稳定性、P8 与真实 OCR 在线验收 |
| `tools/README.md` | OCR 人工/机器调用、JSON 契约与退出码 |
| `PROGRESS.md` | 当前阶段完成度、在线验收缺口和最近一次验证结果 |

---

## 安全

1. `.env` 含 API key —— 已 gitignored，**永远不要提交**
2. `data/portfolio/`、`tools/screenshots/`、`reports/`、`logs/` 含账户金额 —— 同样已 gitignored
3. 实盘需要**三把锁同时开**：`QBG_MODE=LIVE` + `I_CONFIRM_REAL=1` +
   `risk_limits.yaml` 的 `allow_live_mode: true`。任何自动化流程都不得修改它们
4. 启用 OCR 读图意味着**账户持仓截图会上传到 LLM 提供商**。不接受就用
   `QBG_PORTFOLIO_SOURCE=manual` 手工维护 CSV
5. 第三方中转站会看到发送给 P7/P8 的行情、持仓金额和复盘事实；应选择可信服务商，
   并限制 key 额度/IP。真实 key 只写本机 `.env`
6. 邮件日报包含账户余额、持仓和订单；只应发送到自己控制的邮箱，并使用应用专用密码

## License

MIT
