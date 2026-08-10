# 进度记录

> 接手前先读 `plan.md` 和 `CLAUDE.md`。最后更新：2026-08-10。

## 一句话现状

P3–P6 已完成并用本机真实行情跑通：290 只股票训练的三 seed Rank IC 均为正，
顾问模式可一键生成下单清单、日报和可从 JSONL 完整重建的 SQLite 索引。P5 真实
截图与 P7/P8 中转在线验收均完成；P7 作为 fail-open 复核闸启用。P8 复盘模型无
文件、shell、数据库或下单权限，出站邮件通知已接入但尚待本机 SMTP 凭据。

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
- 实测修复：positions ETL 占位符错误（重建后 5 行、invalid=0）及 dry-run 邮件主题误判，均有回归测试。
- 全量离线测试：**314 passed**；ruff 全绿。

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
