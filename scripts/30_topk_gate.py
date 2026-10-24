"""每次调仓选几只（k）该不该改 —— 按**实仓资金**、带一手约束和最低佣金过八项闸。

## 为什么要单独一道闸

2026-10-24 复盘：09-22 → 10-23 回撤 −20.2%，2/3 来自选股（8 只里 7 只跑输等权）。
模型本身没坏（最近两周 Rank IC +0.023，和回测一个水平），这是 k=3 的集中度在一个
风格回撤月里被放大。探索性比较（`31_selection_variants.py`）里，只有加大 k 在两个
窗口上都一致地改善了风险：k=5 夏普 1.25→1.40、最大回撤 −29.3%→−24.8%。

**但那次比较没考虑资金规模。**实仓只有 4 万：

- **一手约束**：单槽 = 资金 × min(0.95/k, 单票上限)。k=5 时每槽约 7,600，只买得起
  股价 76 元以下的票 —— 排名靠前的高价股（海光信息、阳光电源、圣邦股份）全被挤掉，
  选股实际在往排名后面退。这里照搬实盘的 `affordable_scores` 规则逐日做掩码。
- **最低佣金 5 元**：引擎按费率算佣金，`fees.min_notional_for_rate` 写明成交额低于
  5/万2.5 = 20,000 元时会低估。4 万账户**每个 k 都在这条线以下**，k 越大低估越多
  （k=3 有效佣金约 3.9bp，k=5 约 6.6bp）。这里按每个 k 的单槽金额换算有效费率。

不考虑这两条，闸会系统性地偏向大 k。

## 预期

八项闸的 `net_return` 要求"净年化不低于基线"，而加大 k 是**拿收益换风险**（探索里
k=5 年化少 2~3 个点）—— 它几乎注定过不了这一项。那是闸的设计决定的，和关择时过不了
`drawdown` 是同一回事：**不要去调闸的阈值让它过**。闸负责把价码算准，改不改是操作者的
风险偏好决定。

## 简化

资金按固定的 `--equity` 算，不随回测净值增长。策略一年 +60% 的话，第二年其实买得起
更多票 —— 所以这里对大 k 偏保守。手数取整造成的零头现金没模拟（权重仍是连续的）。

用法：
    python scripts/30_topk_gate.py --candidate 5                      # 两个窗口都要跑
    python scripts/30_topk_gate.py --candidate 5 --experiment cn_lgb_mid
    python scripts/30_topk_gate.py --candidate 5 --equity 160000      # 模拟盘的资金量
    python scripts/30_topk_gate.py --candidate 5 --no-account         # 不加资金约束（对照）
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qbg.backtest import panel as panel_mod  # noqa: E402
from qbg.backtest import phases  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.data import cache  # noqa: E402
from qbg.execution import fees  # noqa: E402
from qbg.market import rules  # noqa: E402
from qbg.risk.gates import load_limits  # noqa: E402
from qbg.strategy.predict import (  # noqa: E402
    load_latest_predictions,
    neutralize_frame,
    predictions_to_frame,
)
from qbg.tuning.gates import evaluate  # noqa: E402
from qbg.utils.console import make_output_safe  # noqa: E402

SUBPERIODS = 4
HIGH_COST_MULT = 1.75      # 八项闸要求 +75% 成本下仍为正
TOTAL_WEIGHT = 0.95        # 和 topk_equal_weight / affordable_scores 的默认一致
BAD_MONTH_WINDOW = 22      # "坏月份"：22 个交易日滚动收益的 5% 分位


def slot_budget(equity: float, k: int, cap: float) -> float:
    """实盘 `affordable_scores` 的单槽预算，一字不差。"""
    return equity * min(TOTAL_WEIGHT / k, cap)


def affordable_mask(close: pd.DataFrame, budget: float) -> pd.DataFrame:
    """买得起一手的格子为 True。规则用实盘那个函数本身，不另写一份。"""
    def one(px) -> bool:
        if px is None or not np.isfinite(px) or px <= 0:
            return False          # 价格缺失视为买不起，和实盘一致
        return rules.affordable_shares(budget, float(px)) >= rules.LOT_SIZE
    return close.apply(lambda col: col.map(one)).astype(bool)


def account_fees(base: fees.FeeProfile, budget: float) -> fees.FeeProfile:
    """单槽金额低于 min/rate 时，最低佣金把有效费率抬上去。"""
    effective = max(base.commission_rate, base.commission_min / budget) if budget > 0 \
        else base.commission_rate
    return dataclasses.replace(base, commission_rate=effective)


def high_cost(profile: fees.FeeProfile) -> fees.FeeProfile:
    return dataclasses.replace(
        profile,
        commission_rate=profile.commission_rate * HIGH_COST_MULT,
        commission_min=profile.commission_min * HIGH_COST_MULT,
        stamp_tax_rate=profile.stamp_tax_rate * HIGH_COST_MULT,
        transfer_fee_rate=profile.transfer_fee_rate * HIGH_COST_MULT)


def bad_month(res: phases.PhaseResult) -> float:
    windows = [((1 + run.daily_returns).rolling(BAD_MONTH_WINDOW).apply(np.prod, raw=True) - 1).dropna()
               for run in res.runs]
    return float(pd.concat(windows).quantile(0.05))


def main(argv=None) -> int:
    make_output_safe()
    parser = argparse.ArgumentParser(description="k（每次调仓选几只）的八项闸，带资金约束")
    parser.add_argument("--candidate", type=int, required=True, help="候选 k")
    parser.add_argument("--baseline", type=int, default=settings.qbg_top_k,
                        help="基线 k，默认取现行配置")
    parser.add_argument("--neighbors", default="",
                        help="高原闸用的相邻 k，逗号分隔；默认候选 ±1（去掉基线）")
    parser.add_argument("--equity", type=float, default=40_000.0,
                        help="按多少资金算一手约束和最低佣金。默认 4 万 = 实仓")
    parser.add_argument("--no-account", action="store_true",
                        help="不加资金约束（一手 + 最低佣金），作对照")
    parser.add_argument("--experiment", default=None,
                        help="cn_lgb_mid 的测试段比生产长 65%%。**两个窗口都要跑**")
    parser.add_argument("--keep-rank", type=int, default=settings.qbg_keep_rank)
    parser.add_argument("--every", type=int, default=settings.qbg_rebalance_every_days)
    parser.add_argument("--slippage", type=float, default=0.0020,
                        help="单边额外滑点。默认 20bp，和项目的成本恒等式一致")
    args = parser.parse_args(argv)

    base_k, cand_k = args.baseline, args.candidate
    if base_k == cand_k:
        print(f"⚠ 基线和候选都是 k={base_k} —— 自己比自己，结论没有意义。")
    neighbors = ([int(x) for x in args.neighbors.split(",") if x.strip()]
                 or [n for n in (cand_k - 1, cand_k + 1) if n >= 1 and n != base_k])
    limits = load_limits()
    cap = float(limits["max_position_pct"])
    stop = float(limits.get("stop_loss_pct", 0.0) or 0.0)
    base_fees = fees.FeeProfile.load()

    members = load_universe()
    scores = predictions_to_frame(load_latest_predictions(args.experiment) if args.experiment
                                  else load_latest_predictions())
    if settings.qbg_industry_neutral:
        scores = neutralize_frame(scores)
    panel = panel_mod.build_panel(members, start=str(scores.index.min().date()),
                                  end=str(scores.index.max().date()))
    scores = scores.reindex(index=panel.dates, columns=panel.instruments)
    # 一手约束看的是**不复权**的价 —— 缓存里的 close 就是实盘下单用的那个价
    closes = {}
    for code in panel.instruments:
        frame = cache.read(code)
        if not frame.empty:
            closes[code] = frame.set_index(pd.to_datetime(frame["date"]))["close"]
    close = pd.DataFrame(closes).reindex(index=panel.dates, columns=panel.instruments)

    def setup(k: int):
        if args.no_account:
            return scores, base_fees
        budget = slot_budget(args.equity, k, cap)
        return scores.where(affordable_mask(close, budget)), account_fees(base_fees, budget)

    def run(k: int, *, costly: bool = False) -> phases.PhaseResult:
        sc, profile = setup(k)
        return phases.run_phases(sc, panel, rebalance_every=args.every, k=k,
                                 keep_rank=args.keep_rank, extra_slippage=args.slippage,
                                 stop_loss=stop, fee_profile=high_cost(profile) if costly else profile)

    label = "不加资金约束" if args.no_account else f"资金 {args.equity:,.0f}（一手 + 最低佣金）"
    print(f"窗口 {panel.dates[0].date()} ~ {panel.dates[-1].date()}   {len(panel.dates)} 日   "
          f"每 {args.every} 日调仓   迟滞 {args.keep_rank}   止损 {stop:.0%}   {label}")

    if not args.no_account:
        print(f"\n{'k':>3}{'单槽':>10}{'有效佣金':>10}{'模型前 k 名里买不起的':>22}")
        for k in sorted({base_k, cand_k, *neighbors}):
            budget = slot_budget(args.equity, k, cap)
            mask = affordable_mask(close, budget)
            top = scores.rank(axis=1, ascending=False) <= k
            missing = (top & ~mask).sum(axis=1) / k
            print(f"{k:>3}{budget:>10,.0f}{account_fees(base_fees, budget).commission_rate * 1e4:>9.1f}bp"
                  f"{missing[top.sum(axis=1) > 0].mean():>21.0%}")

    base_res, cand_res = run(base_k), run(cand_k)
    baseline, candidate = base_res.facts(), cand_res.facts()
    print(f"\n{'指标':<16}{f'基线 k={base_k}':>14}{f'候选 k={cand_k}':>14}{'差':>12}")
    print("-" * 56)
    for key, fmt in (("annual_return", "{:+.2%}"), ("sharpe", "{:.4f}"),
                     ("max_drawdown", "{:.2%}"), ("avg_turnover", "{:.4f}"),
                     ("rank_ic", "{:+.4f}")):
        b, c = baseline[key], candidate[key]
        print(f"{key:<16}{fmt.format(b):>14}{fmt.format(c):>14}{fmt.format(c - b):>12}")
    b_bad, c_bad = bad_month(base_res), bad_month(cand_res)
    print(f"{'坏月份(5%分位)':<14}{b_bad:>+14.2%}{c_bad:>+14.2%}{c_bad - b_bad:>+12.2%}")
    print(f"{'相位极差':<14}{base_res.phase_spread:>14.2%}{cand_res.phase_spread:>14.2%}"
          f"{cand_res.phase_spread - base_res.phase_spread:>+12.2%}")

    base_sub = base_res.subperiod_annual(SUBPERIODS)
    cand_sub = cand_res.subperiod_annual(SUBPERIODS)
    excess = [c - b for b, c in zip(base_sub, cand_sub, strict=True)]
    print(f"\n子区间（相位平均后的年化），切成 {SUBPERIODS} 段：")
    for (start, end), b, c in zip(base_res.subperiod_spans(SUBPERIODS), base_sub, cand_sub, strict=True):
        print(f"  {start.date()}~{end.date()}  基线 {b:+9.2%}  候选 {c:+9.2%}  超额 {c - b:+9.2%}")

    neighbor_sharpes = []
    print("\n相邻 k（高原闸）：")
    for k in neighbors:
        sharpe = run(k).sharpe
        neighbor_sharpes.append(sharpe)
        print(f"  k={k} → Sharpe {sharpe:.4f}")

    costly = run(cand_k, costly=True).facts()
    report = evaluate(baseline, candidate, subperiod_excess=excess,
                      neighbor_sharpes=neighbor_sharpes, high_cost=costly)
    print("\n八项闸：")
    for check in report.checks:
        print(f"  {'✅' if check.passed else '❌'} {check.name:<16} {check.detail}")
    failed = [c.name for c in report.checks if not c.passed]
    print(f"\n结论：{'通过' if not failed else '**未通过**'}"
          + (f"（未过：{'、'.join(failed)}）" if failed else ""))
    if failed == ["net_return"]:
        print("  只挂在 net_return：这是拿收益换风险，闸**注定**过不了这一项（见文件头）。"
              "改不改是风险偏好决定，不是闸能替你做的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
