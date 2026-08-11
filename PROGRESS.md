# 进度记录

> 接手前先读 `plan.md` 和 `CLAUDE.md`。最后更新：2026-08-10。

## 一句话现状

P3–P6 已完成并用本机真实行情跑通：290 只股票训练的三 seed Rank IC 均为正，
顾问模式可一键生成下单清单、日报和可从 JSONL 完整重建的 SQLite 索引。P5 真实
截图与 P7/P8 中转在线验收均完成；P7 作为 fail-open 复核闸启用。P8 复盘模型无
文件、shell、数据库或下单权限；出站邮件通知与本机 SMTP 在线验收均已完成。

## 阶段进度

| 阶段 | 状态 | 结果 |
|---|---|---|
| P0 骨架 | ✅ | `qbg` 环境、配置、日志、三把锁 |
| P1 数据 | ✅ | BaoStock/AKShare 降级链、parquet、qlib dump；mootdx 因 httpx 冲突拆为独立可选源 |
| P2 规则/回测 | ✅ | T+1、整手、涨跌停、停牌、不对称费用 |
| P3 模型/选股 | ✅ | Alpha158 + LightGBM 三 seed；集成 Rank IC **+0.04234** |
| P4 风控/清单 | ✅ | 11 个闸结果（3 硬闸 + 订单闸）、SELL 保留、真实模型 dry-run |
| P5 OCR | ✅ | 真实南京证券截图 5 只持仓通过；当前/历史 CSV 已写入且哈希一致 |
| P6 编排/store | ✅ | 一键日循环；重建前后表计数一致 |
| P7 TA 复核 | ✅ | 全链路 9 候选在线通过；144 calls / 599,555 tokens，当前部署已启用且异常 fail-open |
| P8 复盘调参 | ✅ | ZenMux 真实 facts 复盘通过；报告/store 正常，未自动改参 |
| P9 easytrader | ⬜ 可选 | 需 P0–P8 稳定并由用户明确要求 |

## 2026-08-10 实测

- qlib/LightGBM seeds 42/43/44 Rank IC：`+0.04208 / +0.04181 / +0.04184`；集成 `+0.04234`。
- 行业中性化：年化 `30.78% → 31.43%`，Sharpe `1.174 → 1.190`，换手 `0.196 → 0.191`。
- model-free SMA 压测选择 20 日：Sharpe `0.849`，最大回撤 `-14.32%`。
- 顾问清单 dry-run：3 笔订单，股数 `100/200/500`，现金闸后剩余 `28,527.08` 元。
- store 重建前后：runs `1`、scores `290`、plans `3`、executions `3`、gates `11`、events `789`，完全一致。
- 中转在线：文本探测、Vision 合成图、P8 合成事实均通过；P7 三次共 216,330 tokens，原始评级不稳定但复核闸结论一致。
- 全链路 dry-run：290 只打分 → 9 只 TA 复核全部通过 → 3 只目标 → 8 条订单建议 → 11 道风控全过 → P8 复盘 normal → 邮件发送成功；未提交订单。
- 邮件：163 SMTP 465 隐式 TLS 在线通过；完整账户日报已发送，主题明确标注“订单建议8笔·未执行”。
- 邮件模板按参考 PDF 改为单列摘要卡：概览、一句话结论、持仓、复核、订单意见、运行健康和自动复盘，并支持移动端表格。
- 实测修复：positions ETL 占位符错误（重建后 5 行、invalid=0）及 dry-run 邮件主题误判，均有回归测试。
- 全量离线测试：**315 passed**；ruff 全绿。

## 2026-08-10 修复：后复权因子伪造下降

沪深300 全量 304 只里有 4 只的 BaoStock 后复权因子会**下降**。后复权因子只会
随分红送股向上累积，下降必定是假的。最严重的是平安银行 2020-12-31：原始价
19.20→19.34（**+0.73%**），复权后变成 **−16.21%** —— 一根凭空造出来的假阴线，
不处理就直接进模型训练集。

换 `query_adjust_factor` 解决不了：BaoStock 自己的权威因子表里就带着这个下降
（已用独立窄区间查询交叉验证）。

`cache.repair_factor()` 按两种形态分别修：**一日凹陷**（次日原样恢复，如万科）
换成前一日值；**持久平移**（掉下去不回来，如平安银行，多半是源换了复权基准）
把断点之后整段按比例抬回去——段内相对变化不变，断点当天比值变成 1.0。
`read()` 默认修复，`read(repair=False)` 取原样；**落盘永远是源的原样**，
所以 `verify()` 仍能报出源的问题（`01_ingest.py` 已改用 `repair=False` 去验）。

验证：4 只全部恢复单调、全量扫描 0 只残留、平安银行那天复权收益回到 +0.73%；
新增 24 个离线单测，全量 **339 passed**，ruff 全绿。详见 `docs/data-sources.md`。

**重建 qlib bin（299 只）并重训三 seed**：集成 Rank IC `+0.042343 → +0.041358`
（`-0.000985`，小于 seed 间离散度 0.00196，属噪声范围；修复后的数字是在正确
数据上测出来的，应采信这一个）。三 seed 仍全正、方向一致。

⚠️ **行业中性化的结论翻转了**：现在原始分数在年化/Sharpe/回撤/换手/Rank IC
上全面优于中性化（36.37% vs 28.82%，Sharpe 1.37 vs 1.14），与旧结论相反。
本次同时变了三样东西（因子修复、股票池 290→299、重训），**不能归因于单一
原因**；行业表已排除损坏（299/299 有归属）。`QBG_INDUSTRY_NEUTRAL` 仍保持 1
未动 —— 翻一个已部署参数需要走 `tuning/` 的八项回测闸做受控评估，单点观测
不够。详见 `docs/p3-model-validation.md`。

上述收益数字有当前成分股生存者偏差，不是未来收益承诺。详见
`docs/p3-model-validation.md`。

## 常用命令

```bash
python scripts/01_ingest.py
python scripts/02_train.py --seeds 3
python scripts/06_backtest.py --scores model
python scripts/04_plan_orders.py --dry-run
run_daily.bat
python scripts/14_backfill_store.py --rebuild
python scripts/20_daily_review.py
python scripts/24_llm_check.py --probe
python scripts/22_params.py list
```

## 仍需用户输入

1. 南京证券实际佣金费率。

详细在线验收步骤见 `docs/remaining-online-validation.md`。
