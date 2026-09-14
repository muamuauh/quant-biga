"""`QBG_REBALANCE_EVERY_DAYS` 的受控评估 —— 过 `tuning/` 的八项回测闸。

现行部署是 **1（每日调仓）**，而代码默认是 10。`fee_profile.yaml` 末尾算过账：
往返约 10bp + 滑点，一次完整调仓 20~30bp；`plan.md` §8.5 把「成本吃掉 alpha」
列为小账户第一杀手。但那些都是**推理**，不是这套策略上的实测 —— 这个脚本补上。

和中性化那次（`10_neutralize_gate.py`）不同，**这次「参数高原」那一闸真的适用**：
调仓间隔是连续取值，候选左右都有邻居，可以检查它是不是站在一个平坦的区域上，
而不是一根靠运气立住的尖峰。

基线 = 现行配置，候选 = 命令行给的那个值。两边共用同一份预测、同一个股票池、
同一个引擎，只差 `rebalance_every`。择时按现行配置加上，以反映实盘。

用法：
    python scripts/12_rebalance_gate.py --candidate 5
    python scripts/12_rebalance_gate.py --candidate 10 --neighbors 5,15
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


from qbg.backtest import panel as panel_mod  # noqa: E402
from qbg.backtest import phases  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.execution import fees  # noqa: E402
from qbg.strategy.predict import (  # noqa: E402
    load_latest_predictions,
    neutralize_frame,
    predictions_to_frame,
)
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402
from qbg.tuning.gates import Check, GateReport  # noqa: E402

SUBPERIODS = 4
HIGH_COST_MULT = 1.75      # 八项闸要求 +75% 成本下仍为正
# 每次 LLM 逐票复核的实测成本。只有调仓日才复核，所以这一项和调仓频率
# 成正比 —— 而它不在回测里，得单独算出来摆在旁边。
#
# 0.29 是 2026-08 的值（80 次调用、约 34 万 tokens）。**已经过期 4 倍多**：
# 2026-09-11 同一天跑了两轮，分别是 $1.13（92 次调用、126 万 tokens）和
# $1.40（90 次、116 万）。中转站换成了 deepseek-v4-flash / v4-pro / flash
# 三个模型混合计费，和当初不是一个档次。取 1.25 为当前估计。
LLM_COST_PER_REBALANCE_USD = 1.25
TRADING_DAYS_PER_YEAR = 252        # 只用于 LLM 账单的"次/年"估算


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="调仓间隔的八项闸评估")
    parser.add_argument("--candidate", type=int, required=True, help="候选调仓间隔")
    parser.add_argument("--baseline", type=int, default=None,
                        help="基线调仓间隔。默认取现行配置 —— 配置已经改成候选值时，"
                             "默认会变成候选比候选，所以复核历史决定要显式传")
    parser.add_argument("--neighbors", default="", help="高原闸用的相邻取值，逗号分隔")
    parser.add_argument("--k", type=int, default=settings.qbg_top_k)
    parser.add_argument("--experiment", default=None,
                        help="读哪个 MLflow 实验的预测。cn_lgb_mid 的测试段比生产长 65%%。"
                             "**评估任何参数都要跑两个窗口** —— 移动止盈就是短窗口全过、长窗口全翻")
    parser.add_argument("--keep-rank", type=int, default=settings.qbg_keep_rank,
                        help="迟滞。默认取生产值 —— 用 0 是在评估一个没在跑的配置")
    parser.add_argument("--slippage", type=float, default=0.0020,
                        help="单边额外滑点。默认 20bp，和项目的成本恒等式一致")
    args = parser.parse_args(argv)

    baseline_every = int(args.baseline if args.baseline is not None
                         else settings.qbg_rebalance_every_days)
    if baseline_every == args.candidate:
        print(f"⚠ 基线和候选都是每 {baseline_every} 日 —— 这是自己比自己，结论没有意义。"
              "复核历史决定请传 --baseline。")
    candidate_every = args.candidate
    neighbors = ([int(x) for x in args.neighbors.split(",") if x.strip()]
                 or sorted({max(1, candidate_every - 5), candidate_every + 5}))

    members = load_universe()
    scores = predictions_to_frame(load_latest_predictions(args.experiment) if args.experiment
                                  else load_latest_predictions())
    if settings.qbg_industry_neutral:
        scores = neutralize_frame(scores)
    panel = panel_mod.build_panel(members, start=str(scores.index.min().date()),
                                  end=str(scores.index.max().date()))
    scores = scores.reindex(index=panel.dates, columns=panel.instruments)

    regime = "关闭"
    if settings.qbg_market_sma:
        # **信号在完整历史上算，不能先 reindex 到模型窗再算 SMA。**
        # `risk_on_series` 的 min_periods=sma_window：388 天的窗口里 SMA100 前 100 天
        # 没有信号（默认 risk-on）、SMA200 只剩 188 天、更长的干脆整段没有 —— 长窗口
        # 会静默退化成「不择时」，看起来像「长 SMA 更差」。生产的 `market_risk_on()`
        # 读的是完整 parquet 历史。2026-09-08 修，和 19/20 号是同一个坑。
        level = equal_weight_index(members)
        on = risk_on_series(level, settings.qbg_market_sma).reindex(panel.dates).ffill().fillna(True)
        scores = scores.where(on)
        regime = f"SMA={settings.qbg_market_sma}"

    print(f"窗口 {panel.dates[0].date()} ~ {panel.dates[-1].date()}   "
          f"{len(panel.dates)} 个交易日   k={args.k}   择时={regime}")
    print(f"基线 = 每 {baseline_every} 日调仓（现行配置）   "
          f"候选 = 每 {candidate_every} 日   高原邻居 = {neighbors}\n")

    def run(every, profile=None):
        """跑一个调仓间隔。**三处此前漏掉的东西，每一处都单向偏向日频。**

        1. **相位平均。** `every=5` 时相位 0 用第 0/5/10… 天、相位 1 用第
           1/6/11… 天，几乎不重叠。实测 k=3 每 5 日的相位极差是 **37 个百分点
           年化**，每 10 日 86 个 —— 只跑相位 0，比的是"哪几天交易"的运气。
           日频只有一个相位，从来不受影响：**偏差专打候选臂。**

        2. **`keep_rank`（迟滞）。** 生产是 15，漏掉它等于评估一个没在跑的配置
           （带上之后日频换手 0.807 -> 0.579）。

        3. **`extra_slippage`。** `fee_profile` 只有佣金和印花税；项目自己的
           成本恒等式是"买 2.6 + 卖 7.6 + **双边滑点 20**bp"。漏掉滑点 = 漏掉
           往返成本的三分之二，**而那正是拉长调仓间隔唯一能省下来的东西**。
           一道回答"少交易值不值"的闸，不能不算少交易省下的钱。
        """
        return phases.run_phases(scores, panel, rebalance_every=every, k=args.k,
                                 keep_rank=args.keep_rank, extra_slippage=args.slippage,
                                 fee_profile=profile)

    base_res, cand_res = run(baseline_every), run(candidate_every)
    baseline, candidate = base_res.facts(), cand_res.facts()

    profile = fees.FeeProfile.load()
    high = fees.FeeProfile(
        commission_rate=profile.commission_rate * HIGH_COST_MULT,
        commission_min=profile.commission_min * HIGH_COST_MULT,
        stamp_tax_rate=profile.stamp_tax_rate * HIGH_COST_MULT,
        transfer_fee_rate=profile.transfer_fee_rate * HIGH_COST_MULT)
    high_cost = run(candidate_every, profile=high).facts()

    print(f"{'指标':<16}{f'基线(每{baseline_every}日)':>16}"
          f"{f'候选(每{candidate_every}日)':>16}{'差':>12}")
    print("-" * 62)
    for key, fmt in (("annual_return", "{:+.2%}"), ("sharpe", "{:.4f}"),
                     ("max_drawdown", "{:.2%}"), ("avg_turnover", "{:.4f}"),
                     ("rank_ic", "{:+.4f}")):
        b, c = baseline[key], candidate[key]
        print(f"{key:<16}{fmt.format(b):>16}{fmt.format(c):>16}{fmt.format(c - b):>12}")

    # LLM 账单：只有调仓日才做逐票复核，所以它和频率成正比，而回测看不到这一项。
    print(f"\nLLM 逐票复核年账单（每次 ${LLM_COST_PER_REBALANCE_USD:.2f}）：")
    for label, every in (("基线", baseline_every), ("候选", candidate_every)):
        per_year = TRADING_DAYS_PER_YEAR / every
        print(f"  {label} 每 {every:>2} 日 → 约 {per_year:5.0f} 次/年 = "
              f"${per_year * LLM_COST_PER_REBALANCE_USD:,.0f}")

    for label, res, every in (("基线", base_res, baseline_every),
                              ("候选", cand_res, candidate_every)):
        if every > 1:
            print(f"{label}每 {every} 日的相位极差 {res.phase_spread:+.1%} 年化"
                  "（最好与最差相位之差；实盘只能落在其中一个上）")

    # **子区间也按相位平均。** 2026-09-14 之前这里读的是 `res.daily_returns`，
    # 而它通过 __getattr__ 落到了第 0 个相位 —— 头条指标平均了、子区间闸没有。
    base_sub = base_res.subperiod_annual(SUBPERIODS)
    cand_sub = cand_res.subperiod_annual(SUBPERIODS)
    excess = [c - b for b, c in zip(base_sub, cand_sub, strict=True)]
    print(f"\n子区间超额（候选 − 基线，相位平均后的年化），切成 {SUBPERIODS} 段：")
    for i, ((start_day, end_day), b, c) in enumerate(
            zip(base_res.subperiod_spans(SUBPERIODS), base_sub, cand_sub, strict=True), 1):
        print(f"  第{i}段 {start_day.date()}~{end_day.date()}  "
              f"基线 {b:+9.2%}  候选 {c:+9.2%}  超额 {c - b:+9.2%}")

    neighbor_sharpes = []
    print("\n相邻取值（高原闸）：")
    for every in neighbors:
        sharpe = run(every).sharpe
        neighbor_sharpes.append(sharpe)
        print(f"  每 {every:>2} 日 → Sharpe {sharpe:.4f}")

    report = GateReport(tuple(
        _evaluate(baseline, candidate, excess, neighbor_sharpes, high_cost)))
    print("\n八项闸：")
    for check in report.checks:
        print(f"  {'✅' if check.passed else '❌'} {check.name:<16} {check.detail}")
    failed = [c.name for c in report.checks if not c.passed]
    print(f"\n结论：{'通过' if not failed else '**未通过**'}")
    print(f"  → {'可以把 QBG_REBALANCE_EVERY_DAYS 改成 ' + str(candidate_every)}"
          if not failed else f"  → 维持每 {baseline_every} 日。未过：{'、'.join(failed)}")
    return 0


def _evaluate(baseline, candidate, subperiod_excess, neighbor_sharpes, high_cost):
    passed_sub = sum(v >= 0 for v in subperiod_excess)
    return [
        Check("net_return", candidate["annual_return"] >= baseline["annual_return"],
              "净年化不低于基线"),
        Check("sharpe", candidate["sharpe"] >= baseline["sharpe"] - 0.05,
              "Sharpe 允许最多回落 0.05"),
        Check("drawdown", candidate["max_drawdown"] >= baseline["max_drawdown"] - 0.02,
              "回撤不显著恶化"),
        Check("turnover", candidate["avg_turnover"] <= baseline["avg_turnover"] * 1.20,
              "换手不增加超过 20%"),
        Check("rank_ic", candidate["rank_ic"] > 0, "Rank IC 必须为正"),
        Check("subperiod", bool(subperiod_excess) and
              passed_sub >= (len(subperiod_excess) + 1) // 2,
              f"至少半数子区间不劣于基线（{passed_sub}/{len(subperiod_excess)}）"),
        Check("plateau", len(neighbor_sharpes) >= 2 and
              min(neighbor_sharpes) >= candidate["sharpe"] - 0.20,
              "相邻取值处于稳定高原（不是靠运气立住的尖峰）"),
        Check("cost_robustness",
              high_cost["annual_return"] > 0 and high_cost["sharpe"] > 0,
              f"+{int((HIGH_COST_MULT - 1) * 100)}% 成本下仍为正"),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
