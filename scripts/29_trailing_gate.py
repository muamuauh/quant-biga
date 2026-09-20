"""退出规则的八项闸：止损 / 移动止盈 / 现金触发提前调仓。

## 基线和候选

基线 = 现行配置原样：`QBG_REBALANCE_EVERY_DAYS` / `QBG_TOP_K` / `QBG_KEEP_RANK`，
20bp 滑点，**不止损、不止盈**，现金触发阈值取 `QBG_REBALANCE_CASH_TRIGGER`
（2026-09-14 之前引擎完全不模拟现金触发，所以此前所有回测里它等于关着）。
候选 = 同样的配置加上命令行给的规则。两边共用同一份预测、股票池、引擎。

## 两件必须一起看的事

**相位平均**（`qbg.backtest.phases`）：每 10 日调仓的相位极差实测 77~86 个百分点。

**跑两个窗口**（`--experiment cn_lgb_mid`）：移动止盈在 388 日窗口上 16 组参数全部变好，
在 630 日窗口上两组候选全翻。**只跑一个窗口的结论不能用。**

**执行次数必须和收益一起看**：每相位触发 0 次的"不变差"是什么都没做。

## 历史

2026-09-14 首版只评估移动止盈，结论是不开（见 config.py）。同一天发现引擎在多日持仓时
权重没有按组合收益归一化（连涨两个 10% 算成 +22.10%），修掉之后重跑。

用法：
    python scripts/29_trailing_gate.py --stop 0.08
    python scripts/29_trailing_gate.py --stop 0.08 --cash-trigger 0.3 --experiment cn_lgb_mid
    python scripts/29_trailing_gate.py --arm 0.10 --trail 0.10
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
from qbg.utils.console import make_output_safe  # noqa: E402

HIGH_COST_MULT = 1.75


def _neighbors(rule: dict, base_cash: float) -> list[dict]:
    """高原闸的邻居：每个打开了的参数往两边各挪一步。"""
    out = []
    if rule["stop"] > 0:
        out += [rule | {"stop": round(max(0.01, rule["stop"] - 0.03), 4)},
                rule | {"stop": round(rule["stop"] + 0.03, 4)}]
    if rule["arm"] > 0 and rule["trail"] > 0:
        out += [rule | {"arm": round(rule["arm"] * 2 / 3, 4)},
                rule | {"arm": round(rule["arm"] * 1.5, 4)},
                rule | {"trail": round(max(0.01, rule["trail"] - 0.02), 4)},
                rule | {"trail": round(rule["trail"] + 0.02, 4)}]
    if rule["cash"] != base_cash and rule["cash"] > 0:
        out += [rule | {"cash": round(max(0.05, rule["cash"] - 0.1), 4)},
                rule | {"cash": round(min(0.95, rule["cash"] + 0.1), 4)}]
    return out


def _label(rule: dict) -> str:
    parts = []
    if rule["stop"] > 0:
        parts.append(f"止损 {rule['stop']:.0%}")
    if rule["arm"] > 0 and rule["trail"] > 0:
        parts.append(f"止盈 {rule['arm']:.0%}/{rule['trail']:.0%}")
    parts.append(f"现金触发 {rule['cash']:.0%}" if rule["cash"] > 0 else "现金触发关")
    return " · ".join(parts)


def main(argv=None) -> int:
    make_output_safe()
    parser = argparse.ArgumentParser(description="退出规则的八项闸")
    parser.add_argument("--stop", type=float, default=0.0, help="止损线，如 0.08")
    parser.add_argument("--arm", type=float, default=0.0, help="移动止盈上膛线，如 0.10")
    parser.add_argument("--trail", type=float, default=0.0, help="移动止盈回撤，如 0.10")
    parser.add_argument("--cash-trigger", type=float,
                        default=settings.qbg_rebalance_cash_trigger,
                        help="候选的现金触发阈值。默认 = 现行配置")
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

    base_cash = float(settings.qbg_rebalance_cash_trigger)
    baseline_rule = {"stop": 0.0, "arm": 0.0, "trail": 0.0, "cash": base_cash}
    candidate_rule = {"stop": args.stop, "arm": args.arm, "trail": args.trail,
                      "cash": args.cash_trigger}

    def run(rule: dict, profile=None) -> phases.PhaseResult:
        return phases.run_phases(scores, panel, rebalance_every=args.every, k=args.k,
                                 keep_rank=args.keep_rank, extra_slippage=args.slippage,
                                 fee_profile=profile, trail_arm=rule["arm"],
                                 trail_pct=rule["trail"], stop_loss=rule["stop"],
                                 cash_trigger=rule["cash"])

    print(f"{args.experiment or 'cn_lgb（生产）'}  {panel.dates[0].date()} ~ "
          f"{panel.dates[-1].date()}（{len(panel.dates)} 日）  k={args.k}  "
          f"每 {args.every} 日  keep_rank={args.keep_rank}  滑点 {args.slippage * 1e4:.0f}bp")
    print(f"基线 = {_label(baseline_rule)}（现行配置）\n候选 = {_label(candidate_rule)}\n")

    base, cand = run(baseline_rule), run(candidate_rule)
    baseline, candidate = base.facts(), cand.facts()
    print(f"{'指标':<16}{'基线':>12}{'候选':>12}{'差':>12}")
    print("-" * 52)
    for key, fmt in (("annual_return", "{:+.2%}"), ("sharpe", "{:.4f}"),
                     ("max_drawdown", "{:.2%}"), ("avg_turnover", "{:.4f}"),
                     ("rank_ic", "{:+.4f}")):
        b, c = baseline[key], candidate[key]
        print(f"{key:<16}{fmt.format(b):>12}{fmt.format(c):>12}{fmt.format(c - b):>12}")
    stop_exits = lambda res: res._mean(lambda r: r.stop_exits)  # noqa: E731
    cash_rebs = lambda res: res._mean(lambda r: r.cash_rebalances)  # noqa: E731
    print(f"{'止损执行/相位':<14}{stop_exits(base):>12.1f}{stop_exits(cand):>12.1f}")
    print(f"{'止盈执行/相位':<14}{base.trailing_exits:>12.1f}{cand.trailing_exits:>12.1f}")
    print(f"{'现金提前调仓/相位':<12}{cash_rebs(base):>12.1f}{cash_rebs(cand):>12.1f}")
    if stop_exits(cand) + cand.trailing_exits < 1:
        print("  ⚠ 候选几乎不触发 —— 就算指标没变差，也只是什么都没做。")
    print(f"相位极差（年化） 基线 {base.phase_spread:+.1%}  候选 {cand.phase_spread:+.1%}")

    profile = fees.FeeProfile.load()
    high = fees.FeeProfile(
        commission_rate=profile.commission_rate * HIGH_COST_MULT,
        commission_min=profile.commission_min * HIGH_COST_MULT,
        stamp_tax_rate=profile.stamp_tax_rate * HIGH_COST_MULT,
        transfer_fee_rate=profile.transfer_fee_rate * HIGH_COST_MULT)
    high_cost = run(candidate_rule, profile=high).facts()

    base_sub, cand_sub = base.subperiod_annual(SUBPERIODS), cand.subperiod_annual(SUBPERIODS)
    excess = [c - b for b, c in zip(base_sub, cand_sub, strict=True)]
    print(f"\n子区间超额（相位平均后的年化），切成 {SUBPERIODS} 段：")
    for i, ((start, end), b, c) in enumerate(
            zip(base.subperiod_spans(SUBPERIODS), base_sub, cand_sub, strict=True), 1):
        print(f"  第{i}段 {start.date()}~{end.date()}  基线 {b:+9.2%}  候选 {c:+9.2%}  "
              f"超额 {c - b:+9.2%}")

    neighbor_sharpes = []
    print("\n相邻取值（高原闸）：")
    for rule in _neighbors(candidate_rule, base_cash):
        res = run(rule)
        neighbor_sharpes.append(res.sharpe)
        print(f"  {_label(rule):<32} → Sharpe {res.sharpe:.4f}")

    report = evaluate(baseline, candidate, subperiod_excess=excess,
                      neighbor_sharpes=neighbor_sharpes, high_cost=high_cost)
    print("\n八项闸：")
    for check in report.checks:
        print(f"  {'✅' if check.passed else '❌'} {check.name:<16} {check.detail}")
    failed = [c.name for c in report.checks if not c.passed]
    print(f"\n结论：{'通过' if report.passed else '**未通过**'}")
    if report.passed:
        print(f"  → 可以部署：{_label(candidate_rule)}")
    else:
        print(f"  → 维持现行配置。未过：{'、'.join(failed)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
