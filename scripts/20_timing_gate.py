"""关掉市场择时（`QBG_MARKET_SMA=0`）的评估。

## 先说结论会长什么样

**这个闸注定过不了 `drawdown` 那一项。** 关掉择时就是用回撤换收益，而八项闸
里明写着"回撤不显著恶化"。所以不要指望看到"通过"两个字 —— 这个脚本的用处
不是判通过，是**把那笔交换的价码算准**，让人在知情的前提下做选择。

这一点必须写死在这里，否则下一个人会去调闸的阈值来"让它过"，那就本末倒置了。
八项闸是给**参数微调**设计的（"这个改动是不是白赚"）；关掉一整个风控组件是
**风险偏好变更**，闸给不出答案，只能给价码。

## 两项闸不适用，如实标注而不是硬凑

  · `protection`（相对满仓仍有回撤保护）—— 候选**就是**满仓，恒等式为假。
  · `plateau`（相邻取值稳定）—— 开关型改动没有"相邻取值"。
    先例见 `10_neutralize_gate.py` 里"参数高原这一闸不适用"的处理。

## 两条臂

  · 全样本 1620 日 model-free（等权全池）：统计上强
  · 模型窗 388 日 k=3：口径对，但噪声可达 142 个百分点年化，**不作判据**

第二条在这里格外重要：**关掉择时对回撤的影响会被集中度放大**，而 k=3 才是
实盘跑的东西。等权池的 -22.86% 不等于账户会看到的回撤。

用法：
    python scripts/20_timing_gate.py
"""

from __future__ import annotations

import argparse
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qbg.backtest import engine  # noqa: E402
from qbg.backtest.metrics import compute_metrics  # noqa: E402
from qbg.backtest.panel import build_panel  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.data import universe as universe_mod  # noqa: E402
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402
from qbg.tuning.gates import Check, GateReport  # noqa: E402

switch_costs = import_module("07_regime_stress").switch_costs

SUBPERIODS = 4
HIGH_COST_MULT = 1.75
SLIPPAGE_BP = 10.0
TRADING_DAYS_PER_YEAR = 252
# risk_limits.yaml 的风险档上限。关掉择时后它是唯一还在管总回撤的东西。
RISK_TIER_MAX_DD = -0.25


def _annual(returns: pd.Series) -> float:
    if len(returns) == 0:
        return 0.0
    return float((1 + returns).prod()) ** (TRADING_DAYS_PER_YEAR / len(returns)) - 1


def overlay(asset_returns, level, sma, band, slippage_bp) -> tuple[pd.Series, dict]:
    """择时仓位叠到等权全池收益上。`sma<=0` 时恒为满仓（= 关掉择时）。"""
    if sma <= 0:
        aligned = pd.Series(1.0, index=asset_returns.index)
    else:
        shifted = risk_on_series(level, sma, band).shift(1)
        exposure = shifted.where(shifted.notna(), True).astype(bool)
        aligned = exposure.reindex(asset_returns.index).fillna(True).astype(float)
    costs = switch_costs(aligned, slippage_bp).reindex(asset_returns.index).fillna(0.0)
    net = asset_returns * aligned - costs
    m = compute_metrics(net, asset_returns)
    return net, {"annual_return": m.annual_return, "sharpe": m.sharpe,
                 "max_drawdown": m.max_drawdown,
                 "switch_cost_pa": float(costs.sum() / len(costs) * TRADING_DAYS_PER_YEAR),
                 "flips": int((aligned.diff().fillna(0) != 0).sum()),
                 "invested": float(aligned.mean())}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="关掉市场择时的评估")
    p.add_argument("--k", type=int, default=settings.qbg_top_k)
    args = p.parse_args(argv)

    base_sma = int(settings.qbg_market_sma)
    base_band = float(settings.qbg_market_sma_band)

    members = load_universe()
    pnl = build_panel(members)
    asset_returns = pnl.open_to_open_returns().mean(axis=1).dropna()

    raw = equal_weight_index(members).reindex(pnl.dates).ffill()
    mapping = universe_mod.inclusion_dates()
    elig = universe_mod.eligibility_mask(pnl.dates, pnl.instruments, mapping)
    fixed = equal_weight_index(members, eligible=elig).reindex(pnl.dates).ffill()

    print(f"窗口 {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   {len(pnl.dates)} 日   "
          f"翻仓滑点 {SLIPPAGE_BP:.0f}bp")
    # 基线必须是**生产此刻真正在跑的那个配置**，包括指数口径开关。写死 raw
    # 的话，用户一旦打开 QBG_MARKET_INDEX_ELIGIBLE，闸就在和一个不存在的配置
    # 作比较 —— 正是本项目反复栽的那个跟头。
    use_fixed = bool(int(settings.qbg_market_index_eligible))
    base_level = fixed if use_fixed else raw
    base_index_name = "修正口径" if use_fixed else "老口径"
    print(f"基线 = SMA{base_sma} 缓冲{base_band:.0%} {base_index_name}"
          f"（现行生效值）   候选 = 关掉择时\n")

    base_net, baseline = overlay(asset_returns, base_level, base_sma, base_band,
                                 SLIPPAGE_BP)
    cand_net, candidate = overlay(asset_returns, raw, 0, 0.0, SLIPPAGE_BP)

    # 决策表：把可选项一起摆出来，光看"开/关"两点会漏掉中间那些更好的选择。
    print("全样本 model-free 决策表（等权全池）")
    print(f"{'配置':<28}{'净年化':>10}{'夏普':>8}{'回撤':>10}{'在场%':>8}"
          f"{'翻转':>7}{'翻仓成本/年':>12}")
    print("-" * 83)
    options = [("关掉择时（候选）", None, 0, 0.0),
               (f"SMA{base_sma} 缓冲{base_band:.0%} {base_index_name}（现行）",
                base_level, base_sma, base_band),
               ("SMA100 缓冲0% · 老口径（改之前）", raw, 100, 0.0),
               ("SMA100 缓冲3% · 老口径", raw, 100, 0.03),
               ("SMA100 缓冲3% · 修正口径", fixed, 100, 0.03),
               ("SMA200 缓冲3% · 修正口径", fixed, 200, 0.03)]
    for label, lvl, sma, band in options:
        _, f = overlay(asset_returns, lvl, sma, band, SLIPPAGE_BP)
        print(f"{label:<28}{f['annual_return']:>10.2%}{f['sharpe']:>8.2f}"
              f"{f['max_drawdown']:>10.2%}{f['invested']:>8.1%}{f['flips']:>7}"
              f"{f['switch_cost_pa']:>12.2%}")

    print(f"\n候选 − 基线：年化 {candidate['annual_return'] - baseline['annual_return']:+.2%}"
          f"   夏普 {candidate['sharpe'] - baseline['sharpe']:+.2f}"
          f"   回撤 {candidate['max_drawdown'] - baseline['max_drawdown']:+.2%}")

    chunks = np.array_split(np.arange(len(base_net)), SUBPERIODS)
    excess = []
    print(f"\n子区间超额（候选 − 基线，年化），切成 {SUBPERIODS} 段：")
    for i, idx in enumerate(chunks, 1):
        b, c = _annual(base_net.iloc[idx]), _annual(cand_net.iloc[idx])
        excess.append(c - b)
        span = base_net.index[idx]
        print(f"  第{i}段 {span[0].date()}~{span[-1].date()}  "
              f"基线 {b:+9.2%}  候选 {c:+9.2%}  超额 {c - b:+9.2%}")

    _, high_cost = overlay(asset_returns, raw, 0, 0.0, SLIPPAGE_BP * HIGH_COST_MULT)

    # k=3 交叉检查 —— 关掉择时对回撤的影响会被集中度放大，而 k=3 才是实盘。
    print("\n模型窗 k=3 交叉检查（**388 日，噪声可达 142 个百分点年化，不作判据**）:")
    topk_dd = None
    try:
        from qbg.strategy.predict import (
            load_latest_predictions,
            neutralize_frame,
            predictions_to_frame,
        )

        frame = predictions_to_frame(load_latest_predictions())
        if settings.qbg_industry_neutral:
            frame = neutralize_frame(frame)
        mp = build_panel(members, start=str(frame.index.min().date()),
                         end=str(frame.index.max().date()))
        sc = frame.reindex(index=mp.dates, columns=mp.instruments)
        # **不 reindex 到模型窗**：`risk_on_series` 的 min_periods=sma_window，
        # 先截到 388 天的话 SMA100 前 100 天没有信号、长 SMA 干脆整段没信号，
        # 长窗口会退化成"不择时"。生产读的是完整 parquet 历史。
        lvl = equal_weight_index(members)
        for label, sma in ((f"基线 SMA{base_sma}", base_sma), ("候选 关掉择时", 0)):
            scores = sc if sma <= 0 else sc.where(
                risk_on_series(lvl, sma, base_band).reindex(mp.dates).ffill().fillna(True))
            r = engine.run_backtest(scores, mp, k=args.k,
                                    extra_slippage=SLIPPAGE_BP / 1e4, slippage_grid=())
            print(f"  {label:<14} 年化 {r.strategy.annual_return:+9.2%}  "
                  f"夏普 {r.strategy.sharpe:>6.2f}  回撤 {r.strategy.max_drawdown:>8.2%}")
            if sma <= 0:
                topk_dd = r.strategy.max_drawdown
    except Exception as exc:  # noqa: BLE001
        print(f"  跳过（{type(exc).__name__}: {str(exc)[:90]}）")

    checks = _evaluate(baseline, candidate, excess, high_cost)
    report = GateReport(tuple(checks))
    print("\n八项闸：")
    for c in report.checks:
        icon = "➖" if c.detail.startswith("【不适用】") else ("✅" if c.passed else "❌")
        print(f"  {icon} {c.name:<16} {c.detail}")

    print("\n" + "-" * 83)
    print("这个闸的结论怎么用")
    print("-" * 83)
    print("  · `drawdown` 失败是**设计使然**，不是候选有问题：关掉择时就是拿回撤")
    print("    换收益，而八项闸明写着回撤不许恶化。**不要去调闸的阈值让它过。**")
    print("  · 八项闸是给参数微调设计的（\"这个改动是不是白赚\"）。关掉一整个风控")
    print("    组件是**风险偏好变更**，闸给不出答案，只能把价码算准。")
    print(f"  · 价码（全样本等权池）：年化 "
          f"{candidate['annual_return'] - baseline['annual_return']:+.2%}，"
          f"回撤 {candidate['max_drawdown'] - baseline['max_drawdown']:+.2%}。")
    if topk_dd is not None:
        print(f"  · **但实盘是 k=3。** 模型窗上关掉择时的回撤是 {topk_dd:.2%} —— "
              f"集中度会把回撤放大，")
        print("    等权池那个数字不是账户会看到的。这一条比上面那行更接近你的体验。")
    print("  · 关掉之后，`risk_limits.yaml` 里的 `max_daily_loss_pct`(3%) 和")
    print("    `stop_loss_pct`(8%) 就是仅剩的下行保护，而它们管的是单日/单票，")
    print("    （⚠ 2026-09-14 更正：stop_loss_pct 从未接线，实际只有 max_daily_loss_pct，")
    print("     而它只砍当天的 BUY、不卖任何东西）")
    print("    **管不了慢慢阴跌出来的总回撤**。这是关掉择时真正丢掉的东西。")
    print("  · 中间选项在决策表里：捆绑改动（缓冲带 + 修正口径）把择时从"
          "\"最差版本\"")
    print("    变成\"合理版本\"，代价比全关小得多。全关是端点，不是唯一的替代方案。\n")
    return 0


def _evaluate(baseline, candidate, subperiod_excess, high_cost) -> list[Check]:
    passed_sub = sum(v >= 0 for v in subperiod_excess)
    return [
        Check("net_return", candidate["annual_return"] >= baseline["annual_return"],
              "净年化不低于基线"),
        Check("sharpe", candidate["sharpe"] >= baseline["sharpe"] - 0.05,
              "Sharpe 允许最多回落 0.05"),
        Check("drawdown", candidate["max_drawdown"] >= baseline["max_drawdown"] - 0.02,
              "回撤不显著恶化 —— **关掉择时必然过不了这一项，见下方说明**"),
        Check("switch_cost", candidate["switch_cost_pa"] <= baseline["switch_cost_pa"],
              f"翻仓成本不增加（基线 {baseline['switch_cost_pa']:.2%}/年 → "
              f"候选 {candidate['switch_cost_pa']:.2%}/年）"),
        Check("risk_tier", candidate["max_drawdown"] >= RISK_TIER_MAX_DD,
              f"回撤仍在风险档上限内（≥ {RISK_TIER_MAX_DD:.0%}，"
              f"候选 {candidate['max_drawdown']:.2%}）"),
        Check("subperiod", bool(subperiod_excess) and
              passed_sub >= (len(subperiod_excess) + 1) // 2,
              f"至少半数子区间不劣于基线（{passed_sub}/{len(subperiod_excess)}）"),
        # 开关型改动没有"相邻取值"。硬凑一个（比如 SMA=20）测的是另一件事。
        Check("plateau", True,
              "【不适用】开关型改动没有相邻取值可测；"
              "中间选项另见决策表，先例：10_neutralize_gate.py"),
        Check("cost_robustness",
              high_cost["annual_return"] > 0 and high_cost["sharpe"] > 0,
              f"+{int((HIGH_COST_MULT - 1) * 100)}% 成本下仍为正"),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
