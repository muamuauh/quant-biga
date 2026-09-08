"""重新体检市场择时：抖动、缓冲带，以及纳入前视对 SMA 选择的影响。

## 为什么要重跑

两件事让 `QBG_MARKET_SMA=100` 这个结论需要复核：

1. **纳入前视**（2026-09-05 量化）。择时信号读的是股票池等权净值，而池子是
   **当前**沪深300 成分 —— 2020 年那段曲线里混着 2026 年才被纳入的票，而它们
   被纳入恰恰因为涨得好。信号看到的市场比真实市场强，SMA 的穿越点跟着偏。
   `07_regime_stress.py` 那张选出 SMA=100 的表就是在这条曲线上跑的。

2. **抖动**（2026-09-08 实测）。1620 个交易日里有 **27 段** risk-off，中位只有
   **5 天**。那不是在躲熊市，是在均线上蹭 —— 每翻一次全仓清掉再买回来，一个
   来回约 10bp 基础费率 + 双边滑点，还错过清仓期间的上涨。

## 这个脚本回答什么

**不是**"该不该调低阈值"，而是三个可测的问题：

  · 修掉纳入前视之后，SMA 的最优取值还是 100 吗
  · 加迟滞缓冲带能不能在**不牺牲保护**的前提下减少翻转
  · 择时整体到底还值不值得开（对手是"一直满仓"，不是 0）

判据是**净年化 + 夏普 + 回撤三项一起看**，并且和同口径的"一直满仓"比。
只看年化会把"少赚但也少亏"的择时误判成失败。

用法：
    python scripts/18_regime_recheck.py
    python scripts/18_regime_recheck.py --slippage-bp 10
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from qbg.backtest.metrics import compute_metrics  # noqa: E402
from qbg.backtest.panel import build_panel  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.data import universe as universe_mod  # noqa: E402
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from importlib import import_module  # noqa: E402

switch_costs = import_module("07_regime_stress").switch_costs

SMA_GRID = (20, 50, 100, 150, 200)
BAND_GRID = (0.0, 0.01, 0.02, 0.03)


def arm(asset_returns, level, sma, band, slippage_bp) -> dict:
    """一个 (SMA, 缓冲带) 组合的净表现。"""
    # t-1 收盘的信号决定 t 开盘的仓位。shift 是避免前视的关键一步。
    shifted = risk_on_series(level, sma, band).shift(1)
    exposure = shifted.where(shifted.notna(), True).astype(bool)
    aligned = exposure.reindex(asset_returns.index).fillna(True).astype(float)
    gross = asset_returns * aligned
    costs = switch_costs(aligned, slippage_bp).reindex(gross.index).fillna(0.0)
    m = compute_metrics(gross - costs, asset_returns)
    flips = int((aligned.diff().fillna(0) != 0).sum())
    return {"sma": sma, "band": band, "annual": m.annual_return, "sharpe": m.sharpe,
            "mdd": m.max_drawdown, "invested": float(aligned.mean()), "flips": flips,
            "cost_pa": float(costs.sum() / len(costs) * 252)}


def spell_stats(on: pd.Series) -> tuple[int, float]:
    """risk-off 的段数和中位长度 —— 抖动程度的直接度量。"""
    s = on.dropna().astype(bool)
    if s.empty:
        return 0, 0.0
    sizes = s.groupby((s != s.shift()).cumsum()).agg(["first", "size"])
    off = sizes[~sizes["first"]]["size"]
    return len(off), float(off.median()) if len(off) else 0.0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="市场择时复核")
    p.add_argument("--slippage-bp", type=float, default=10.0,
                   help="翻仓的额外滑点（bp，双边）。默认 10，和因子实验同口径")
    p.add_argument("--sma", default=",".join(str(x) for x in SMA_GRID))
    p.add_argument("--band", default=",".join(str(x) for x in BAND_GRID),
                   help="缓冲带，小数。网格最优若顶在边角上，说明该往外扩")
    args = p.parse_args(argv)
    sma_grid = [int(x) for x in args.sma.split(",") if x.strip()]
    band_grid = [float(x) for x in args.band.split(",") if x.strip()]

    members = load_universe()
    t0 = time.time()
    pnl = build_panel(members)
    asset_returns = pnl.open_to_open_returns().mean(axis=1).dropna()

    mapping = universe_mod.inclusion_dates()
    eligible = universe_mod.eligibility_mask(pnl.dates, pnl.instruments, mapping)

    variants = {
        "老口径（带纳入前视）": equal_weight_index(members).reindex(pnl.dates).ffill(),
        "修正（只算当天在指数里的）":
            equal_weight_index(members, eligible=eligible).reindex(pnl.dates).ffill(),
    }
    print(f"面板 {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   {len(pnl.dates)} 日   "
          f"{len(pnl.instruments)} 只   翻仓滑点 {args.slippage_bp:.0f}bp   "
          f"({time.time() - t0:.0f}s)")

    hold = compute_metrics(asset_returns, asset_returns)
    print(f"\n一直满仓（等权持有，不择时）: 年化 {hold.annual_return:+.2%}   "
          f"夏普 {hold.sharpe:.2f}   回撤 {hold.max_drawdown:.2%}")
    print("**这是唯一的及格线。** 择时要么提高夏普、要么显著削回撤，"
          "两样都没有就是在白付成本。")

    best = {}
    for label, level in variants.items():
        print(f"\n{'=' * 96}\n{label}\n{'=' * 96}")
        print(f"{'SMA':>5}{'缓冲带':>8}{'净年化':>10}{'夏普':>8}{'回撤':>10}"
              f"{'在场%':>8}{'翻转次数':>10}{'成本/年':>9}{'off段数':>9}{'off中位':>9}")
        print("-" * 96)
        rows = []
        for sma in sma_grid:
            for band in band_grid:
                r = arm(asset_returns, level, sma, band, args.slippage_bp)
                n_off, med_off = spell_stats(risk_on_series(level, sma, band))
                r["n_off"], r["med_off"] = n_off, med_off
                rows.append(r)
                mark = "*" if r["sharpe"] > hold.sharpe else " "
                cur = " ←现行" if (sma == settings.qbg_market_sma and band == 0.0) else ""
                print(f"{mark}{sma:>4}{band:>8.1%}{r['annual']:>10.2%}{r['sharpe']:>8.2f}"
                      f"{r['mdd']:>10.2%}{r['invested']:>8.1%}{r['flips']:>10}"
                      f"{r['cost_pa']:>9.2%}{n_off:>9}{med_off:>9.0f}{cur}")
        best[label] = max(rows, key=lambda r: r["sharpe"])

    print(f"\n{'-' * 96}\n怎么读\n{'-' * 96}")
    print("  · 行首 * = 夏普高于「一直满仓」。**只看净年化会误判** —— 择时天然")
    print("    少赚（在场时间 < 100%），它的价值在削回撤，要三项一起看。")
    print("  · 「翻转次数」和「off段数」是抖动的直接度量。缓冲带如果能在夏普")
    print("    不降的前提下把翻转砍掉一半，那就是纯赚 —— 省下的全是成本。")
    print("  · 两张表的差 = 纳入前视对**择时参数选择**的影响。若修正后最优 SMA")
    print("    变了，说明现行的 100 是在一条比真实市场更强的曲线上选出来的。")
    for label, r in best.items():
        print(f"\n  {label} 最优: SMA={r['sma']} 缓冲带={r['band']:.1%}  "
              f"夏普 {r['sharpe']:.2f}（满仓 {hold.sharpe:.2f}）  "
              f"年化 {r['annual']:+.2%}  回撤 {r['mdd']:.2%}  翻转 {r['flips']} 次")
    print("\n生存者偏差只修掉了纳入前视那一半；历史被剔除的成分仍不在样本里，")
    print("方向同样是高估。绝对数字是上界，网格内部的相对比较才是结论。")
    print("**任何改动都要先过 tuning/ 的八项闸，不要直接改 .env。**\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
