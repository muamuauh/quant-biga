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
                       TradingAgents 逐票复核（DeepSeek，P7）
                                          │
                          沪深300 择时 → 订单规划（整手 + 涨跌停夹逼）
                                          │
                                    8 道风控闸
                                          │
                        reports/orders/YYYY-MM-DD.{md,csv}
                                          │
                     你在南京证券 APP 照单执行 → 截图 → OCR → CSV
                                          │
                       reconcile → SQLite store → 中文日报 → 复盘 agent（P8）
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
pip install -e .[data,model,llm,dev]

# 2. 配置
cp .env.example .env                     # 然后填 API key

# 3. 拉数据（P1 完成后可用）
python scripts/01_ingest.py

# 4. 回测（P2 完成后可用）
python scripts/06_backtest.py

# 5. 出下单清单（P4 完成后可用）
python scripts/04_plan_orders.py --dry-run
```

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

---

## 安全

1. `.env` 含 API key —— 已 gitignored，**永远不要提交**
2. `data/portfolio/`、`tools/screenshots/`、`reports/`、`logs/` 含账户金额 —— 同样已 gitignored
3. 实盘需要**三把锁同时开**：`QBG_MODE=LIVE` + `I_CONFIRM_REAL=1` +
   `risk_limits.yaml` 的 `allow_live_mode: true`。任何自动化流程都不得修改它们
4. 启用 OCR 读图意味着**账户持仓截图会上传到 LLM 提供商**。不接受就用
   `QBG_PORTFOLIO_SOURCE=manual` 手工维护 CSV

## License

MIT
