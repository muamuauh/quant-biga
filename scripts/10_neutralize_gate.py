"""`QBG_INDUSTRY_NEUTRAL` 的受控评估 —— 过 `tuning/` 的八项回测闸。

`docs/p3-model-validation.md` 从 2026-08-10 起挂着一条未决告警：复权因子修复
后重训，行业中性化在**每一项指标上都更差**，和之前据以部署 `=1` 的结论完全
相反。但那次只有**单点观测**，而同时还变了三样东西（复权修复、股票池 290→299、
重训），所以文档自己的建议是：走八项闸做受控评估再决定，不要凭单点翻参数。

这个脚本就是那次评估。基线 = 现行配置（中性化开），候选 = 关掉。
两边共用同一份预测、同一个股票池、同一个回测引擎，**只切换中性化这一个开关**。

关于「参数高原」这一项：`qbg_industry_neutral` 是 0/1 二元开关，**没有相邻
取值**，所以那一项在这里天然不适用。脚本如实报告 `n/a` 而**不伪造邻居**
让它通过 —— 把不适用的闸糊弄成通过，等于把八项闸变成七项闸，
而且是在没人注意的地方变。

用法：
    python scripts/10_neutralize_gate.py
    python scripts/10_neutralize_gate.py --no-regime   # 不加择时闸的对照
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402

from qbg.backtest import engine  # noqa: E402
from qbg.backtest import panel as panel_mod  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.execution import fees  # noqa: E402
from qbg.strategy.predict import (  # noqa: E402
    load_latest_predictions,
    neutralize_frame,
    predictions_to_frame,
)
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402
from qbg.tuning.gates import Check, GateReport  # noqa: E402

SUBPERIODS = 4          # 把测试段切成几块看稳定性
HIGH_COST_MULT = 1.75   # 八项闸要求 +75% 成本下仍为正


def _run(scores, panel, k, profile=None):
    return engine.run_backtest(scores, panel, k=k, fee_profile=profile)


def _annual(returns) -> float:
    """按日收益算年化。子区间太短时用几何年化，和引擎口径一致。"""
    if len(returns) == 0:
        return 0.0
    total = float((1 + returns).prod())
    return total ** (252 / len(returns)) - 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="QBG_INDUSTRY_NEUTRAL 的八项闸评估")
    parser.add_argument("--k", type=int, default=settings.qbg_top_k)
    parser.add_argument("--no-regime", action="store_true",
                        help="不加择时闸；默认按现行配置加上，以反映实盘")
    args = parser.parse_args(argv)

    members = load_universe()
    raw = predictions_to_frame(load_latest_predictions())
    panel = panel_mod.build_panel(members, start=str(raw.index.min().date()),
                                  end=str(raw.index.max().date()))
    raw = raw.reindex(index=panel.dates, columns=panel.instruments)
    neutral = neutralize_frame(raw).reindex(index=panel.dates, columns=panel.instruments)

    regime_note = "关闭"
    if not args.no_regime and settings.qbg_market_sma:
        level = equal_weight_index(members).reindex(panel.dates).ffill()
        on = risk_on_series(level, settings.qbg_market_sma).reindex(panel.dates).fillna(True)
        raw, neutral = raw.where(on), neutral.where(on)
        regime_note = f"SMA={settings.qbg_market_sma}"

    print(f"窗口 {panel.dates[0].date()} ~ {panel.dates[-1].date()}   "
          f"{len(panel.dates)} 个交易日   k={args.k}   择时={regime_note}\n")

    base_res = _run(neutral, panel, args.k)      # 基线 = 现行配置（中性化开）
    cand_res = _run(raw, panel, args.k)          # 候选 = 关掉中性化

    profile = fees.FeeProfile.load()
    high = fees.FeeProfile(
        commission_rate=profile.commission_rate * HIGH_COST_MULT,
        commission_min=profile.commission_min * HIGH_COST_MULT,
        stamp_tax_rate=profile.stamp_tax_rate * HIGH_COST_MULT,
        transfer_fee_rate=profile.transfer_fee_rate * HIGH_COST_MULT,
    )
    high_res = _run(raw, panel, args.k, profile=high)

    def facts(res):
        m = res.strategy
        return {"annual_return": m.annual_return, "sharpe": m.sharpe,
                "max_drawdown": m.max_drawdown, "avg_turnover": res.avg_turnover,
                "rank_ic": res.rank_ic}

    baseline, candidate = facts(base_res), facts(cand_res)
    print(f"{'指标':<14}{'基线(中性化=1)':>16}{'候选(中性化=0)':>16}{'差':>12}")
    print("-" * 60)
    for key, fmt in (("annual_return", "{:+.2%}"), ("sharpe", "{:.4f}"),
                     ("max_drawdown", "{:.2%}"), ("avg_turnover", "{:.3f}"),
                     ("rank_ic", "{:+.4f}")):
        b, c = baseline[key], candidate[key]
        print(f"{key:<14}{fmt.format(b):>16}{fmt.format(c):>16}{fmt.format(c - b):>12}")

    # --- 子区间稳定性 ---
    chunks = np.array_split(np.arange(len(base_res.daily_returns)), SUBPERIODS)
    excess = []
    print(f"\n子区间超额（候选 − 基线，年化），切成 {SUBPERIODS} 段：")
    for i, idx in enumerate(chunks, 1):
        b = _annual(base_res.daily_returns.iloc[idx])
        c = _annual(cand_res.daily_returns.iloc[idx])
        excess.append(c - b)
        span = base_res.daily_returns.index[idx]
        print(f"  第{i}段 {span[0].date()}~{span[-1].date()}  "
              f"基线 {b:+8.2%}  候选 {c:+8.2%}  超额 {c-b:+8.2%}")

    # --- 八项闸 ---
    report = _evaluate_binary(baseline, candidate, excess, facts(high_res))
    print("\n八项闸：")
    for check in report.checks:
        mark = {True: "✅", False: "❌", None: "➖"}[check.passed]
        print(f"  {mark} {check.name:<16} {check.detail}")
    # **不能直接用 `report.passed`**：它是 `all(check.passed ...)`，而标了 n/a
    # 的那项 `passed` 是 None（falsy），会把"不适用"算成"不通过"。
    # 这次恰好有真失败所以看不出来，但只要哪天七项全过，它照样会报未通过。
    applicable = [c for c in report.checks if c.passed is not None]
    failed = [c.name for c in applicable if not c.passed]
    print(f"\n结论：{'通过' if not failed else '**未通过**'}"
          f"（{len(applicable)} 项适用，1 项 n/a）")
    if failed:
        print(f"  → 维持现行配置。未过：{'、'.join(failed)}")
    else:
        print("  → 可以把 QBG_INDUSTRY_NEUTRAL 改成 0")
    return 0


def _evaluate_binary(baseline, candidate, subperiod_excess, high_cost) -> GateReport:
    """八项闸，但「参数高原」对二元开关标 n/a 而不是伪造邻居让它通过。

    `tuning.gates.evaluate` 要求传 `neighbor_sharpes`；0/1 开关没有邻居，
    传空列表会让那一项直接判失败 —— 那是"不适用"被误报成"不通过"，
    同样是错的。所以这里显式复刻其余七项，把高原那项标成 n/a。
    """
    checks = [
        Check("net_return", candidate["annual_return"] >= baseline["annual_return"],
              "净年化不低于基线"),
        Check("sharpe", candidate["sharpe"] >= baseline["sharpe"] - 0.05,
              "Sharpe 允许最多回落 0.05"),
        Check("drawdown", candidate["max_drawdown"] >= baseline["max_drawdown"] - 0.02,
              "回撤不显著恶化"),
        Check("turnover", candidate["avg_turnover"] <= baseline["avg_turnover"] * 1.20,
              "换手不增加超过 20%"),
        Check("rank_ic", candidate["rank_ic"] > 0, "Rank IC 必须为正"),
        Check("subperiod",
              bool(subperiod_excess) and
              sum(v >= 0 for v in subperiod_excess) >= (len(subperiod_excess) + 1) // 2,
              f"至少半数子区间不劣于基线（{sum(v >= 0 for v in subperiod_excess)}"
              f"/{len(subperiod_excess)}）"),
        Check("plateau", None, "n/a —— 0/1 开关没有相邻取值，此闸不适用"),
        Check("cost_robustness",
              high_cost["annual_return"] > 0 and high_cost["sharpe"] > 0,
              f"+{int((HIGH_COST_MULT - 1) * 100)}% 成本下仍为正"),
    ]
    return GateReport(tuple(checks))


if __name__ == "__main__":
    raise SystemExit(main())
