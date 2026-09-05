"""换手压缩扫描 —— 因子筛完之后唯一还有意义的那一维。

`13_factor_zoo.py` 的结论：27 个手写因子里，换手率单独解释了净年化差异的
**58%**（相关系数 −0.764），拟合斜率 −73.6%/单位换手，而这个斜率就等于
成本恒等式本身（`0.1 × (买2.6 + 卖7.6 + 双边滑点20) bp × 252 ≈ 7.6%`）。
换句话说：在这个池子、这个频率上，**因子之间的差异主要是交易量的差异**。

所以下一步不是再写因子，是把已经有毛边际的因子的换手压下去。两个杠杆，
都已经在实盘配置里、此前回测却验不了：

    rebalance_every  调仓间隔（`QBG_REBALANCE_EVERY_DAYS`）—— 少调仓
    keep_rank        迟滞保留名次（`QBG_KEEP_RANK`）—— 调仓时少换人

两者压换手的**机制不同**，所以要交叉扫而不是各扫各的：拉长间隔是"整段时间
不动"，迟滞是"每次都看但只在掉出去时才换"。后者对信号衰减快的因子更友好。

    python scripts/15_turnover_sweep.py                    # bias20 全网格
    python scripts/15_turnover_sweep.py --factor ret3_th5
    python scripts/15_turnover_sweep.py --thresholds       # 追涨阈值那一族
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from qbg.backtest import engine  # noqa: E402
from qbg.backtest import panel as panel_mod  # noqa: E402
from qbg.config import load_universe  # noqa: E402

HEADLINE_SLIPPAGE = 0.001   # 和 13_factor_zoo 同一口径：散户限价单额外滑点
TRADING_DAYS_PER_YEAR = 252


def _annual(returns: pd.Series) -> float:
    if len(returns) == 0:
        return 0.0
    return float((1 + returns).prod()) ** (TRADING_DAYS_PER_YEAR / len(returns)) - 1


def build_signal(name: str, close: pd.DataFrame) -> tuple[pd.DataFrame, bool]:
    """返回 `(分数, 是否阈值型)`。阈值型走 `fixed_slots`，空槽留现金。"""
    if name == "bias20":
        ma20 = close.rolling(20, min_periods=10).mean()
        return -(close / ma20 - 1.0), False
    if name == "rev20":
        return -close.pct_change(20, fill_method=None), False
    if name == "rev5":
        return -close.pct_change(5, fill_method=None), False
    if name == "ret3":
        return close.pct_change(3, fill_method=None), False
    if name.startswith("ret3_th"):
        pct = float(name.removeprefix("ret3_th")) / 100.0
        r3 = close.pct_change(3, fill_method=None)
        return r3.where(r3 >= pct), True
    raise SystemExit(f"未知因子：{name}")


def run_grid(scores, is_th, pnl, k, every_grid, keep_grid, phases: bool) -> list[dict]:
    """扫网格。`phases=True` 时每个 `every` 跑遍全部相位再取平均。

    **相位不是细节。** `rebalance_every=n` 只在 `index % n == phase` 那些天调仓，
    不同相位用的是几乎不重叠的决策日；k 小、窗口短的时候，光换个相位就能
    差上百个百分点的年化（16_horizon_sweep 实测：h=3 的三个相位是
    +1.98% / +39% / +144.65%）。固定相位 0 报出来的数字，分不清是持有期的
    功劳还是"碰巧在那些天下单"的运气。
    """
    rows = []
    for every in every_grid:
        for keep in keep_grid:
            runs = []
            for phase in (range(every) if phases else (0,)):
                runs.append(engine.run_backtest(
                    scores, pnl, k=k, rebalance_every=every, rebalance_phase=phase,
                    keep_rank=keep, extra_slippage=HEADLINE_SLIPPAGE,
                    fixed_slots=is_th, slippage_grid=(0.0, HEADLINE_SLIPPAGE)))
            annuals = [r.strategy.annual_return for r in runs]
            halves = []
            for r in runs:
                half = len(r.daily_returns) // 2
                halves.append((_annual(r.daily_returns.iloc[:half]),
                               _annual(r.daily_returns.iloc[half:])))
            rows.append({
                "k": k, "every": every, "keep": keep, "n_phase": len(runs),
                "annual": sum(annuals) / len(annuals),
                "spread": max(annuals) - min(annuals),
                "annual_nofric": sum(r.slippage_curve[0.0].annual_return
                                     for r in runs) / len(runs),
                "sharpe": sum(r.strategy.sharpe for r in runs) / len(runs),
                "mdd": sum(r.strategy.max_drawdown for r in runs) / len(runs),
                "turnover": sum(r.avg_turnover for r in runs) / len(runs),
                "h1": sum(h[0] for h in halves) / len(halves),
                "h2": sum(h[1] for h in halves) / len(halves),
            })
            print(f"    k={k} 间隔={every:<2} 迟滞={keep:<2} × {len(runs)} 相位 → "
                  f"换手 {rows[-1]['turnover']:.3f}  净年化 {rows[-1]['annual']:+.2%}"
                  f"  相位极差 {rows[-1]['spread']:.2%}", flush=True)
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="调仓间隔 × 迟滞 的换手压缩扫描")
    p.add_argument("--factor", default="bias20")
    p.add_argument("--k", default="3,5")
    p.add_argument("--every", default="1,3,5,10")
    p.add_argument("--keep", default="0,10,20,30")
    p.add_argument("--thresholds", action="store_true",
                   help="改跑「近3日涨幅>X%%」那一族，X 取 2/3/5/8/12/15")
    p.add_argument("--phases", action="store_true",
                   help="每个调仓间隔扫遍全部相位再平均（强烈建议开）")
    p.add_argument("--start", default=None)
    args = p.parse_args(argv)

    members = load_universe()
    t0 = time.time()
    pnl = panel_mod.build_panel(members, start=args.start)
    close = pnl.close_px
    print(f"面板 {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   "
          f"{len(pnl.dates)} 日   {len(pnl.instruments)} 只   ({time.time() - t0:.0f}s)\n",
          flush=True)

    bench = None
    ks = [int(x) for x in args.k.split(",") if x.strip()]
    every_grid = [int(x) for x in args.every.split(",") if x.strip()]
    keep_grid = [int(x) for x in args.keep.split(",") if x.strip()]

    factors = ([f"ret3_th{x}" for x in (2, 3, 5, 8, 12, 15)]
               if args.thresholds else [args.factor])

    tables = {}
    for name in factors:
        scores, is_th = build_signal(name, close)
        scores = scores.reindex(index=pnl.dates, columns=pnl.instruments)
        if is_th:
            print(f"  {name}: 日均 {scores.notna().sum(axis=1).mean():.1f} 只合格", flush=True)
        print(f"  --- {name} ---", flush=True)
        rows = []
        for k in ks:
            rows += run_grid(scores, is_th, pnl, k, every_grid, keep_grid, args.phases)
        tables[name] = rows
        if bench is None:
            bench = engine.run_backtest(scores, pnl, k=ks[0], slippage_grid=()).benchmark

    _report(tables, bench, pnl)
    return 0


def _report(tables, bench, pnl) -> None:
    print("\n" + "=" * 96)
    print(f"换手压缩扫描   {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   "
          f"净年化已扣 基础费率 + {HEADLINE_SLIPPAGE * 1e4:.0f}bp 滑点")
    print("=" * 96)
    print(f"等权买入持有（不换手）: 年化 {bench.annual_return:+.2%}   "
          f"夏普 {bench.sharpe:.2f}   回撤 {bench.max_drawdown:.2%}")
    print("行首 * = 净年化跑赢这条线。这是唯一的及格标准。")

    winners = []
    for name, rows in tables.items():
        print(f"\n--- {name} ---")
        print(f"{'k':>3}{'间隔':>5}{'迟滞':>5}{'相位':>5}{'换手':>8}{'净年化':>10}"
              f"{'相位极差':>10}{'@0bp':>10}{'夏普':>7}{'回撤':>9}{'前半':>10}{'后半':>10}")
        print("-" * 92)
        for r in sorted(rows, key=lambda r: r["annual"], reverse=True):
            beat = r["annual"] > bench.annual_return
            if beat:
                winners.append((name, r))
            print(f"{'*' if beat else ' '}{r['k']:>2}{r['every']:>5}{r['keep']:>5}"
                  f"{r['n_phase']:>5}{r['turnover']:>8.3f}{r['annual']:>+10.2%}"
                  f"{r['spread']:>10.2%}{r['annual_nofric']:>+10.2%}"
                  f"{r['sharpe']:>7.2f}{r['mdd']:>9.2%}{r['h1']:>+10.2%}{r['h2']:>+10.2%}")

    print("\n" + "-" * 96)
    if not winners:
        print("没有任何组合跑赢等权买入持有。**压换手不足以救这个信号。**")
    else:
        print(f"{len(winners)} 个组合跑赢基准。但跑赢不等于可用，还要过三关：")
        print("  1. 前半/后半必须同号 —— 只在一段行情里成立的不是规律")
        print("  2. 夏普要真的高于基准，不能只是靠加大波动把年化撑上去")
        print("  3. 邻居要在同一量级 —— 网格上孤立的一个尖峰是运气，不是高原")
        print("  4. **相位极差要远小于它跑赢基准的幅度** —— 极差比优势还大，")
        print("     说明这一格的数字主要由「碰巧哪天下单」决定，换个相位就没了")
        print("  过了这三关再进 tuning/ 的八项闸，不要直接改 .env。")
    print("\n生存者偏差贯穿全表（股票池是**当前**沪深300 成分），绝对收益被系统性")
    print("高估；网格内部的相对比较仍然成立。")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
