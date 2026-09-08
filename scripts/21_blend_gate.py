"""把短线信号和 qlib 分数混合：权重该是正的还是负的，由数据说。

## 为什么这个实验的重点是**符号**

诉求是"选最近 3 日涨幅超过 5% 的票，和 qlib 的结果做加权"。但
`13_factor_zoo.py`（全样本 1620 日）测到的是：

    ret3 单独用   净年化 -25.45%   **@0bp -0.55%**   Rank IC **-0.0159**
    买不进 106 次（全表最高档 —— 涨最多的票正是次日最可能封板的）

`@0bp` 是扣费之前的毛收益，它已经是负的 —— 也就是说这条信号**在付任何成本
之前就没有边际**。而 Rank IC 为负意味着：近 3 日涨得多的票，次日倾向于**跑输**。

把一个负 IC 的信号以**正权重**加进模型分数，是在机械地往下拽。所以这个脚本
不去论证"该不该加"，而是**把权重扫过零点**，让曲线自己指出最优符号：

  · 若最优 w > 0 → 直觉对，全样本那个负 IC 在模型窗上不成立
  · 若最优 w < 0 → **反着用**：不追高。同一条信息，符号一翻就有价值
  · 若最优 w ≈ 0 → 这条信息在模型之外没有增量（模型可能已经学到了）

## 三种混合形式

  · `ret3_z`     连续型：z(ret3) 直接加权
  · `ret3_gate5` 阈值型：ret3 ≥ 5% 记 1、否则 0（**诉求的字面形式**）
  · `veto_top`   否决型：ret3 排在全池前 X% 的票直接剔出候选
                 —— 这一种**不加权、不改变持仓只数、成本增量约等于零**

第三种是前几轮反复提到的"短线信号当否决器而非选择器"。它在这里第一次被
量化对照，而不是只作为建议。

## 噪声警告

模型分数只在测试段存在（388 日），而这个窗口 k=3 的噪声可达 142 个百分点
年化（`docs/p3-model-validation.md`）。所以：**同时扫 k=3/5/10，看结论是否
跨 k 一致**。单个 k 上的最优点没有意义；三个 k 指向同一个符号才是证据。

用法：
    python scripts/21_blend_gate.py
    python scripts/21_blend_gate.py --k 3,5,10 --weights -0.6,-0.3,0,0.3,0.6
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from qbg.backtest import engine  # noqa: E402
from qbg.backtest import panel as panel_mod  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.strategy.predict import (  # noqa: E402
    load_latest_predictions,
    neutralize_frame,
    predictions_to_frame,
)
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402

SLIPPAGE = 0.001            # 与 13/15/18/19 同一口径
VETO_PCTS = (0.02, 0.05, 0.10, 0.20)


def zscore(df: pd.DataFrame) -> pd.DataFrame:
    """按天做横截面 z-score。不 z 化就没法和模型分数相加（量纲完全不同）。"""
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1).replace(0, pd.NA), axis=0)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="短线信号 × qlib 分数的混合权重扫描")
    p.add_argument("--k", default="3,5,10")
    p.add_argument("--weights", default="-0.6,-0.4,-0.2,-0.1,0,0.1,0.2,0.4,0.6")
    p.add_argument("--threshold", type=float, default=0.05, help="阈值型的门限")
    args = p.parse_args(argv)

    ks = [int(x) for x in args.k.split(",") if x.strip()]
    weights = [float(x) for x in args.weights.split(",") if x.strip()]

    members = load_universe()
    frame = predictions_to_frame(load_latest_predictions())
    if settings.qbg_industry_neutral:
        frame = neutralize_frame(frame)
    pnl = panel_mod.build_panel(members, start=str(frame.index.min().date()),
                                end=str(frame.index.max().date()))
    model = frame.reindex(index=pnl.dates, columns=pnl.instruments)
    close = pnl.close_px
    ret3 = close.pct_change(3, fill_method=None)

    # 择时闸按实盘配置叠上去，否则测的是一个不存在的策略。
    # **信号在完整历史上算**，不能先截到模型窗（min_periods=sma_window，
    # 截断会让长 SMA 静默退化成"不择时" —— 19/20 号脚本踩过这个坑）。
    on = None
    if settings.qbg_market_sma:
        full = equal_weight_index(
            members, use_inclusion=bool(int(settings.qbg_market_index_eligible)))
        on = (risk_on_series(full, settings.qbg_market_sma,
                             float(settings.qbg_market_sma_band))
              .reindex(pnl.dates).ffill().fillna(True))

    zm, zr = zscore(model), zscore(ret3)
    gate = (ret3 >= args.threshold).astype(float).where(ret3.notna())

    def run(scores, k):
        s = scores.where(on) if on is not None else scores
        r = engine.run_backtest(s, pnl, k=k, extra_slippage=SLIPPAGE, slippage_grid=())
        return {"annual": r.strategy.annual_return, "sharpe": r.strategy.sharpe,
                "mdd": r.strategy.max_drawdown, "turnover": r.avg_turnover,
                "rank_ic": r.rank_ic, "no_buy": r.blocked["limit_up_cannot_buy"]}

    print(f"模型窗 {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   {len(pnl.dates)} 日   "
          f"{len(pnl.instruments)} 只   滑点 {SLIPPAGE * 1e4:.0f}bp   "
          f"择时 SMA{settings.qbg_market_sma} 缓冲{settings.qbg_market_sma_band:.0%}")
    print(f"阈值型门限 = 近3日涨幅 ≥ {args.threshold:.0%}   "
          f"日均合格 {gate.sum(axis=1).mean():.1f} 只 / {len(pnl.instruments)}")
    print("\n**这个窗口只有 388 日、k=3 噪声可达 142 个百分点年化。**")
    print("单个 k 的最优点没有意义 —— 只看**三个 k 是否指向同一个符号**。\n")

    base = {k: run(model, k) for k in ks}
    print("基线（纯 qlib 分数）")
    print(f"{'k':>4}{'净年化':>11}{'夏普':>8}{'回撤':>10}{'换手':>8}{'RankIC':>10}{'买不进':>8}")
    print("-" * 59)
    for k in ks:
        b = base[k]
        print(f"{k:>4}{b['annual']:>11.2%}{b['sharpe']:>8.2f}{b['mdd']:>10.2%}"
              f"{b['turnover']:>8.3f}{b['rank_ic']:>+10.4f}{b['no_buy']:>8}")

    for label, signal in (("ret3_z（连续型）", zr), ("ret3_gate5（阈值型·诉求形式）", gate)):
        print(f"\n{'=' * 78}\n混合：z(qlib) + w × {label}\n{'=' * 78}")
        print(f"{'w':>7}" + "".join(f"{f'k={k} 夏普':>12}" for k in ks)
              + "".join(f"{f'k={k} 年化':>12}" for k in ks))
        print("-" * (7 + 24 * len(ks)))
        for w in weights:
            blended = zm + w * signal.fillna(0.0)
            rows = {k: run(blended, k) for k in ks}
            tag = "  ← 纯模型" if w == 0 else ""
            print(f"{w:>7.2f}" + "".join(f"{rows[k]['sharpe']:>12.2f}" for k in ks)
                  + "".join(f"{rows[k]['annual']:>12.1%}" for k in ks) + tag)

    print(f"\n{'=' * 78}\n否决型：ret3 排在全池前 X% 的票剔出候选（不改持仓只数）\n{'=' * 78}")
    print(f"{'剔除':>7}" + "".join(f"{f'k={k} 夏普':>12}" for k in ks)
          + "".join(f"{f'k={k} 年化':>12}" for k in ks) + f"{'换手Δ':>10}")
    print("-" * (17 + 24 * len(ks)))
    rank_pct = ret3.rank(axis=1, ascending=False, pct=True)
    for pct in VETO_PCTS:
        vetoed = model.where(rank_pct > pct)
        rows = {k: run(vetoed, k) for k in ks}
        d_turn = rows[ks[0]]["turnover"] - base[ks[0]]["turnover"]
        print(f"{pct:>7.0%}" + "".join(f"{rows[k]['sharpe']:>12.2f}" for k in ks)
              + "".join(f"{rows[k]['annual']:>12.1%}" for k in ks) + f"{d_turn:>+10.3f}")

    print(f"\n{'-' * 78}\n怎么读\n{'-' * 78}")
    print("  · **先看符号，再看数值。** 三个 k 一致地偏向 w<0 或 w>0 才算证据；")
    print("    只有一个 k 冒尖那是噪声（这个窗口的噪声比多数效应大）。")
    print("  · 否决型那张表的「换手Δ」是关键：接近 0 说明它**几乎不花钱** ——")
    print("    同一条信息当选择器要付换手税，当否决器不用。")
    print("  · 全样本（1620 日）上 ret3 的 Rank IC 是 **-0.0159**、@0bp 毛收益")
    print("    **-0.55%**。若这里最优 w>0，那是模型窗和全样本打架，以样本长的为准。")
    print("  · TradingAgents 在打分**之后**跑，混合只改变送去复核的候选名单，")
    print("    不需要改架构；复核成本按候选数计，也不变。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
