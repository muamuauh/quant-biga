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
- [x] **P5** 持仓 OCR 工具（离线对抗测试完成；真实截图待用户提供）
- [x] **P6** 编排 + 日报 + store
- [x] **P7** TradingAgents 逐票复核（vendor/离线测试完成；真实调用待 API key）
- [ ] **P8** 自动复盘调参 agent（事实层/白名单/八闸/回滚已完成；SDK 在线层待补）
- [ ] **P9** easytrader（可选）

阶段目标、任务清单和验收标准见 `plan.md` §7。

---

## 六、待补充的外部输入

1. **一张手机 APP 持仓截图** —— P5 需要，用来确定读图 prompt 和字段映射，
   特别是确认持仓页**显不显示股票代码**
2. **南京证券实际佣金费率** —— P2 需要，填进 `configs/fee_profile.yaml`
   （当前是行业常见值万2.5，非实测）
3. （可选）打电话确认南京证券 NXT/QMT 开通门槛 —— 决定是否实现 `QmtAdapter`
