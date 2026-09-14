"""移动止盈的八项闸：现行配置（不止盈）vs 加上 arm/trail。

## 为什么要单独过闸，而不是照搬兄弟仓库的参数

quant-trading 2026-08-04 在几乎相同的配置（k=3、每 10 日、行业中性）上回测，
部署档 arm15/trail5 **夏普 1.65→1.55、年化 −5.6 点、回撤反而恶化**；"不变差"的
档位全是零触发。结论是它对动量型 top-K 会砍掉仍在跑的赢家。

但那是美股，50 只大盘股。A股有 T+1、涨跌停（跌停开盘卖不出）、更高的散户换手，
截面结构不一样。**所以同一个机制在这里要重新量，不能照搬结论，也不能照搬参数。**

## 这道闸比的是什么

基线 = 现行配置原样（`QBG_REBALANCE_EVERY_DAYS` / `QBG_TOP_K` / `QBG_KEEP_RANK`，
20bp 滑点，**不止盈**）。候选 = 同样的配置加上 arm/trail。两边共用同一份预测、
同一个股票池、同一个引擎，只差止盈参数。

**必须相位平均**（见 `qbg.backtest.phases`）：每 10 日调仓的相位极差实测 77~86 个
百分点年化，只跑一个相位比的是运气。

**触发次数必须和收益一起看。** 每相位触发 0 次的"不变差"，是什么都没做，不是变好了。

## 高原闸的邻居

默认取 arm × {2/3, 3/2} 和 trail ± 2 个百分点四个点。两个参数都连续，
候选应该站在平台上，不是一根尖峰。

用法：
    python scripts/29_trailing_gate.py --arm 0.10 --trail 0.10
    python scripts/29_trailing_gate.py --arm 0.10 --trail 0.10 --experiment cn_lgb_mid
    python scripts/29_trailing_gate.py --arm 0.15 --trail 0.05 --neighbors 0.10:0.05,0.20:0.05
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
from qbg.tuning.gates import evaluate  # noqa: E402

SUBPERIODS = 4
HIGH_COST_MULT = 1.75


def _neighbors(arm: float, trail: float, raw: str) -> list[tuple[float, float]]:
    if raw.strip():
        return [(float(a), float(t)) for a, t in
                (item.split(":") for item in raw.split(",") if item.strip())]
    return [(round(arm * 2 / 3, 4), trail), (round(arm * 1.5, 4), trail),
            (arm, round(max(0.01, trail - 0.02), 4)), (arm, round(trail + 0.02, 4))]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="移动止盈的八项闸")
    parser.add_argument("--arm", type=float, required=True, help="上膛线，如 0.10")
    parser.add_argument("--trail", type=float, required=True, help="从峰值回撤，如 0.10")
    parser.add_argument("--neighbors", default="", help="高原邻居 arm:trail,arm:trail")
    parser.add_argument("--experiment", default=None,
                        help="读哪个 MLflow 实验的预测。cn_lgb_mid 的测试段比生产长 65%%")
    parser.add_argument("--k", type=int, default=settings.qbg_top_k)
    parser.add_argument("--every", type=int, default=settings.qbg_rebalance_every_days)
    parser.add_argument("--keep-rank", type=int, default=settings.qbg_keep_rank)
    parser.add_argument("--slippage", type=float, default=0.0020)
    args = parser.parse_args(argv)

    members = load_universe()
    pred = (load_latest_predictions(args.experiment) if args.experiment
            else load_latest_predictions())
    scores = predictions_to_frame(pred)
    if settings.qbg_industry_neutral:
        scores = neutralize_frame(scores)
    panel = panel_mod.build_panel(members, start=str(scores.index.min().date()),
                                  end=str(scores.index.max().date()))
    scores = scores.reindex(index=panel.dates, columns=panel.instruments)
    if settings.qbg_market_sma:
        print("⚠ 择时开着，但这道闸没有叠择时信号 —— 结论只对不择时的配置有效。")

    def run(arm: float, trail: float, profile=None) -> phases.PhaseResult:
        return phases.run_phases(scores, panel, rebalance_every=args.every, k=args.k,
                                 keep_rank=args.keep_rank, extra_slippage=args.slippage,
                                 fee_profile=profile, trail_arm=arm, trail_pct=trail)

    print(f"{args.experiment or 'cn_lgb（生产）'}  {panel.dates[0].date()} ~ "
          f"{panel.dates[-1].date()}（{len(panel.dates)} 日）  k={args.k}  "
          f"每 {args.every} 日  keep_rank={args.keep_rank}  滑点 {args.slippage * 1e4:.0f}bp")
    print(f"基线 = 不止盈    候选 = arm {args.arm:.0%} / trail {args.trail:.0%}\n")

    base, cand = run(0.0, 0.0), run(args.arm, args.trail)
    baseline, candidate = base.facts(), cand.facts()
    print(f"{'指标':<16}{'基线':>12}{'候选':>12}{'差':>12}")
    print("-" * 52)
    for key, fmt in (("annual_return", "{:+.2%}"), ("sharpe", "{:.4f}"),
                     ("max_drawdown", "{:.2%}"), ("avg_turnover", "{:.4f}"),
                     ("rank_ic", "{:+.4f}")):
        b, c = baseline[key], candidate[key]
        print(f"{key:<16}{fmt.format(b):>12}{fmt.format(c):>12}{fmt.format(c - b):>12}")
    print(f"{'止盈触发/相位':<14}{base.trailing_exits:>12.1f}{cand.trailing_exits:>12.1f}")
    if cand.trailing_exits < 1:
        print("  ⚠ 候选几乎不触发 —— 就算指标没变差，也只是什么都没做。")
    print(f"相位极差（年化） 基线 {base.phase_spread:+.1%}  候选 {cand.phase_spread:+.1%}")

    profile = fees.FeeProfile.load()
    high = fees.FeeProfile(
        commission_rate=profile.commission_rate * HIGH_COST_MULT,
        commission_min=profile.commission_min * HIGH_COST_MULT,
        stamp_tax_rate=profile.stamp_tax_rate * HIGH_COST_MULT,
        transfer_fee_rate=profile.transfer_fee_rate * HIGH_COST_MULT)
    high_cost = run(args.arm, args.trail, profile=high).facts()

    base_sub, cand_sub = base.subperiod_annual(SUBPERIODS), cand.subperiod_annual(SUBPERIODS)
    excess = [c - b for b, c in zip(base_sub, cand_sub, strict=True)]
    print(f"\n子区间超额（相位平均后的年化），切成 {SUBPERIODS} 段：")
    for i, ((start, end), b, c) in enumerate(
            zip(base.subperiod_spans(SUBPERIODS), base_sub, cand_sub, strict=True), 1):
        print(f"  第{i}段 {start.date()}~{end.date()}  基线 {b:+9.2%}  候选 {c:+9.2%}  "
              f"超额 {c - b:+9.2%}")

    neighbor_sharpes = []
    print("\n相邻取值（高原闸）：")
    for arm, trail in _neighbors(args.arm, args.trail, args.neighbors):
        res = run(arm, trail)
        neighbor_sharpes.append(res.sharpe)
        print(f"  arm {arm:.1%} / trail {trail:.1%} → Sharpe {res.sharpe:.4f}"
              f"  触发/相位 {res.trailing_exits:.1f}")

    report = evaluate(baseline, candidate, subperiod_excess=excess,
                      neighbor_sharpes=neighbor_sharpes, high_cost=high_cost)
    print("\n八项闸：")
    for check in report.checks:
        print(f"  {'✅' if check.passed else '❌'} {check.name:<16} {check.detail}")
    failed = [c.name for c in report.checks if not c.passed]
    print(f"\n结论：{'通过' if report.passed else '**未通过**'}")
    if report.passed:
        print(f"  → 可以在 .env 设 QBG_TRAIL_ARM_PCT={args.arm} QBG_TRAIL_PCT={args.trail}")
    else:
        print(f"  → 保持不止盈。未过：{'、'.join(failed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
