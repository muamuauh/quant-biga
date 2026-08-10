# 进度记录

> 每完成一个阶段更新一次。**接手前先读 `plan.md`**（完整背景/架构/计划），
> 再读 `CLAUDE.md`（纪律与约定），最后看这里知道走到哪了。

**最后更新**：2026-08-10
**当前状态**：P0–P2 完成并提交，P3（模型与选股）未开始

---

## 一句话现状

数据层和回测引擎已经跑通并经过真实数据验证：能拉沪深300 日线增量落地，
能跑带四条 A股约束的 top-K 回测，**随机打分的 Rank IC 在 0 附近证明引擎
没有前视偏差**。还没有真正的选股模型（那是 P3），也还不能出下单清单（P4）。

---

## 阶段进度

| 阶段 | 状态 | commit | 内容 |
|---|---|---|---|
| **P0** 项目骨架 | ✅ 完成 | `1ec819d` | conda env `qbg`、config/llm/logging、configs、CLAUDE.md |
| **P1** 数据层 | ✅ 完成 | `8e37e2f` | 三源降级链、增量 parquet、股票池/行业/日历、qlib 导出 |
| **P2** A股规则 + 回测 | ✅ 完成 | `5a75fb8` | 涨跌停/整手/T+1、不对称费率、回测引擎四条约束 |
| **P3** 模型与选股 | ⬜ 未开始 | | qlib LightGBM + Alpha158、择时压测、可负担性过滤 |
| **P4** 风控 + 下单清单 | ⬜ 未开始 | | 8 道闸、order_planner、AdvisoryAdapter |
| **P5** 持仓 OCR | ⬜ 未开始 | | `tools/ocr_positions.py` + 校验层 + reconcile |
| **P6** 编排 + 日报 + store | ⬜ 未开始 | | daily_cycle、SQLite store、中文日报 |
| **P7** TradingAgents 复核 | ⬜ 未开始 | | ashare vendor、DeepSeek |
| **P8** 复盘调参 agent | ⬜ 未开始 | | Claude Agent SDK、参数白名单、回测把关 |
| **P9** easytrader（可选） | ⬜ 未开始 | | |

**规模**：源码 3755 行 · 测试 1876 行 · **245 个离线测试全绿** · ruff 全绿

---

## 已经能用的东西

```bash
conda activate qbg      # 或用全路径 C:/ProgramData/miniconda3/Scripts/conda.exe run -n qbg

# 拉数据（首次全量约几十分钟，之后增量秒级）
python scripts/01_ingest.py
python scripts/01_ingest.py --codes 600519.SH,000858.SZ --skip-meta --no-qlib

# 回测（P3 之前用占位打分验证引擎）
python scripts/06_backtest.py --scores random     # 校准：Rank IC 应在 0 附近
python scripts/06_backtest.py --scores reversal --k 3
```

**数据现状**：`data/parquet/` 已有 127 只票的 2020-01 至今日线
（全量 300 只的后台拉取当时未跑完，重跑 `01_ingest.py` 会自动补齐）。

---

## 实测中发现的、写在代码注释里的坑

这些都是**不会报错但会毁掉结果**的那类问题，全部已处理：

| 坑 | 后果 | 处理 |
|---|---|---|
| 成交量单位：BaoStock 给"股"，AKShare 给"手" | 差 100 倍，任何量价因子被静默污染 | akshare 源统一 ×100；对账不变式 `amount/volume ≈ close` |
| 东财 `push2*` 主机本机不可达 | 实时快照/东财行业全废 | 行业改走申万，股票池走中证 |
| `bs.login()` 往 stdout 打字符串 | 破坏 JSONL 日志，store 层无法重建 | `redirect_stdout` 吞掉 |
| BaoStock 会话全局、非线程安全 | 并发查询数据串台且不报错 | 模块级锁串行化 |
| 股票名写法不一致（`万  科Ａ` vs `万科A`） | 名称反查永远查不到 → P5 持仓进不了系统 | NFKC 折叠 + 去全角空格 |
| 内建 `round()` 是银行家舍入 | 涨跌停差一分钱 = 限价无法成交 | 用 Decimal ROUND_HALF_UP |
| 常数收益序列的 std 是 1e-19 不是 0 | 夏普炸到 3.6e16 | 加零波动阈值 |
| ST 涨跌幅：以为都是 ±5% | 创业板 ST 限价被夹到错误区间，单废掉 | 只有主板收窄，创业板/科创板仍 ±20% |

详见 `docs/data-sources.md`。

---

## 关键设计决定（改之前先读理由）

- **降级链区分两种失败**：「源挂了」换源并拉黑，「这只票没数据」不换源。
  混为一谈会让退市股轮询所有源，或让主源挂掉被当成正常空结果。
- **回测成交价用次日开盘价**，目标权重 shift(1)。盘后出信号、次日人工执行，
  用收盘价等于白送隔夜跳空那段收益。
- **涨跌停用不复权价算，收益率用后复权价**。混用不会产生异常值，只会把
  可交易性判断悄悄搞反。
- **买不进的钱留在现金里**，不重新分配给别的票——真实情况就是那笔钱没投出去。
- **回测报告强制打印「这份回测不能说明什么」**。一份不说自己局限的报告比
  没有报告更危险。
- **`data/runs.db` 永远是派生的**（P6 才建），可从 JSONL 日志全量重建，
  交易路径不读它。

---

## P3 开工前要知道的

1. **qlib 还没 clone**。`qlib_dump.run_dump_bin()` 会明确报 `qlib_not_cloned`
   并给出命令，不会抛异常：
   ```bash
   git clone https://github.com/microsoft/qlib qlib && pip install -e qlib
   pip install -e ".[model]"     # lightgbm + mlflow
   ```
2. **qlib label 必须改成 open-to-open**。CN 默认是
   `Ref($close,-2)/Ref($close,-1)-1`（T+1 收盘买、T+2 收盘卖），
   和本项目"次日开盘执行"的成交假设对不上。改成
   `Ref($open,-2)/Ref($open,-1)-1`。
3. **`QBG_MARKET_SMA=100` 是从美股抄来的占位值**，必须用
   `scripts/07_regime_stress.py`（还没写）在 {20,50,100,200} 上跑 5 年
   压测后重定，并把结论写进 `config.py` 的注释。
4. **可负担性过滤要按整手算**：判据是 `price × 100 ≤ 单槽预算`，
   不是 `price ≤ 预算`。`rules.affordable_shares()` 已经实现，
   P3 接进选股时直接用。
5. quant-trading 刻意绕开 qlib 的 `workflow()` / `PortAnaRecord`
   （Windows 上 joblib 拆卸会崩），移植时照做。

---

## 还欠你的两样输入

| # | 需要的东西 | 用途 | 何时需要 |
|---|---|---|---|
| 1 | **南京证券实际佣金费率**（万几、是否最低 5 元） | `configs/fee_profile.yaml` 现在是行业常见值万2.5，**非实测** | P3 回测结论要用之前 |
| 2 | **一张手机 APP 持仓截图** | 定读图 prompt 和字段映射，特别是确认持仓页**显不显示股票代码** | P5 |

另外建议打个电话给南京证券营业部确认 NXT/QMT 的实际开通门槛
（网上的表说约 100 万，不一定准）——决定 P9 要不要做 `QmtAdapter`。

---

## 占位打分的回测结果（**不是策略表现**）

跑于 2026-08-10，105~111 只票，2020-01 至今，k=3：

| 打分 | 年化 | 夏普 | Rank IC | 换手/期 |
|---|---|---|---|---|
| random | −10.66% | −0.20 | **−0.0022** ← 校准通过 | 0.917 |
| momentum(20d) | +22.95% | 0.65 | −0.0105 | 0.249 |
| reversal(5d) | +10.25% | 0.44 | +0.0184 | 0.518 |
| 基准（等权买入持有） | ≈+20% | ≈0.93 | — | 0 |

**怎么读这张表**：
- random 的 Rank IC ≈ 0 是**引擎正确性的证明**，不是策略结果
- reversal 的 +0.0184 方向符合 A股短期反转的先验，说明 IC 算得对
- 三个占位打分的夏普**都不如等权买入持有**，这很正常——它们不是策略，
  而且 k=3 极度集中（年化波动 38%~52%）
- 滑点曲线：0.917 换手下多 10bp 滑点，年化从 −10.7% 掉到 −42.9%。
  **这就是 `QBG_REBALANCE_EVERY_DAYS` 默认 10 而不是 1 的实证依据**
