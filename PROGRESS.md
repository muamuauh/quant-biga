# 进度记录

> 接手前先读 `plan.md` 和 `CLAUDE.md`。最后更新：2026-08-10。

## 一句话现状

P3–P6 已完成并用本机真实行情跑通：290 只股票训练的三 seed Rank IC 均为正，
顾问模式可一键生成下单清单、日报和可从 JSONL 完整重建的 SQLite 索引。P5/P7
的代码与离线安全测试完成，但真实截图/LLM 调用缺密钥；P8 的参数治理核心完成，
Claude Agent SDK 在线运行层尚未移植。

## 阶段进度

| 阶段 | 状态 | 结果 |
|---|---|---|
| P0 骨架 | ✅ | `qbg` 环境、配置、日志、三把锁 |
| P1 数据 | ✅ | BaoStock/AKShare 降级链、parquet、qlib dump；mootdx 因 httpx 冲突拆为独立可选源 |
| P2 规则/回测 | ✅ | T+1、整手、涨跌停、停牌、不对称费用 |
| P3 模型/选股 | ✅ | Alpha158 + LightGBM 三 seed；集成 Rank IC **+0.04234** |
| P4 风控/清单 | ✅ | 11 个闸结果（3 硬闸 + 订单闸）、SELL 保留、真实模型 dry-run |
| P5 OCR | 🟨 | 多图、强校验、人工确认、历史 CSV、对账均完成；待真实截图在线验收 |
| P6 编排/store | ✅ | 一键日循环；重建前后表计数一致 |
| P7 TA 复核 | 🟨 | ashare vendor、幂等 patch、fail-open、token 计价完成；待 DeepSeek key 在线验收 |
| P8 复盘调参 | 🟨 | 事实诊断、白名单、八闸、overlay、回滚、熔断完成；Claude SDK/MCP 运行层待补 |
| P9 easytrader | ⬜ 可选 | 需 P0–P8 稳定并由用户明确要求 |

## 2026-08-10 实测

- qlib/LightGBM seeds 42/43/44 Rank IC：`+0.04208 / +0.04181 / +0.04184`；集成 `+0.04234`。
- 行业中性化：年化 `30.78% → 31.43%`，Sharpe `1.174 → 1.190`，换手 `0.196 → 0.191`。
- model-free SMA 压测选择 20 日：Sharpe `0.849`，最大回撤 `-14.32%`。
- 顾问清单 dry-run：3 笔订单，股数 `100/200/500`，现金闸后剩余 `28,527.08` 元。
- store 重建前后：runs `1`、scores `290`、plans `3`、executions `3`、gates `11`、events `789`，完全一致。
- 全量离线测试：**292 passed**；ruff 全绿。

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
python scripts/22_params.py list
```

## 仍需用户输入

1. 南京证券实际佣金费率。
2. 一张南京证券 APP 持仓截图。
3. 若启用 P7/P8：DeepSeek 与 Anthropic API 配置。

详细在线验收步骤见 `docs/remaining-online-validation.md`。
