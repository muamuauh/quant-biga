"""量化生存者偏差里**能修的那一半**：纳入前视。

## 两个偏差，别混为一谈

"股票池是当前沪深300 成分"其实混着两件事：

  * **纳入前视** —— 2026 年才进指数的票，在 2020 年的回测里就已经在池子里。
    而它进得去恰恰因为这几年涨得好。**等于提前知道谁会赢。**
  * **幸存者** —— 2020 年在指数里、后来被剔除的票，样本里根本没有。

第一个能修：`ak.index_stock_cons` 给得出每只当前成分的**纳入日期**。
实测（2026-09-05）287 只里有 136 只（47%）是 2020-01-01 之后才纳入的。
第二个修不了，本地拿不到历史剔除名单 —— 这个脚本不假装能修它。

## 基准必须用同一个 mask

这是最容易漏的一步。等权买入持有的基准**本身就带纳入前视** —— 它也在
2020 年就持有了 2026 年才进指数的票。拿一个修过的策略去比一个没修的基准，
差值没有任何意义。所以下面策略和基准走同一张 `eligible` 表。

## 这个脚本报什么

同一个配置跑两遍（原口径 / 纳入日期修正），**差值就是纳入前视的量级**。
它不会给出"真实收益"，只会给出"我们此前高估了多少"。

用法：
    python scripts/17_survivorship.py
    python scripts/17_survivorship.py --phases      # 调仓间隔扫遍相位
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
from qbg.backtest.metrics import compute_metrics  # noqa: E402
from qbg.config import load_universe  # noqa: E402
from qbg.data import universe as universe_mod  # noqa: E402

HEADLINE_SLIPPAGE = 0.001   # 与 13/15/16 同一口径

# 配置表：(名字, 因子, k, 调仓间隔, 迟滞)。
# bias20 那两格是 15_turnover_sweep 相位平均后的最稳一格和它的日频对照；
# 其余是 13_factor_zoo 里有代表性的几个，用来看偏差是不是只打击某一类因子。
CONFIGS = [
    ("bias20 间隔5",  "bias20", 3, 5, 0),
    ("bias20 日频",   "bias20", 3, 1, 0),
    ("rev20 日频",    "rev20",  3, 1, 0),
    ("rev5 日频",     "rev5",   3, 1, 0),
    ("ret3 日频",     "ret3",   3, 1, 0),
    ("highvol 日频",  "highvol", 3, 1, 0),
]


def build_signal(name: str, close: pd.DataFrame) -> pd.DataFrame:
    if name == "bias20":
        return -(close / close.rolling(20, min_periods=10).mean() - 1.0)
    if name == "rev20":
        return -close.pct_change(20, fill_method=None)
    if name == "rev5":
        return -close.pct_change(5, fill_method=None)
    if name == "ret3":
        return close.pct_change(3, fill_method=None)
    if name == "highvol":
        return close.pct_change(fill_method=None).rolling(20, min_periods=10).std()
    raise SystemExit(f"未知因子：{name}")


def run(scores, pnl, k, every, keep, phases: bool) -> tuple[float, float, float]:
    """返回 `(净年化, 夏普, 换手)`，`phases=True` 时对相位取平均。"""
    runs = []
    for phase in (range(every) if phases else (0,)):
        runs.append(engine.run_backtest(
            scores, pnl, k=k, rebalance_every=every, rebalance_phase=phase,
            keep_rank=keep, extra_slippage=HEADLINE_SLIPPAGE, slippage_grid=()))
    n = len(runs)
    return (sum(r.strategy.annual_return for r in runs) / n,
            sum(r.strategy.sharpe for r in runs) / n,
            sum(r.avg_turnover for r in runs) / n)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="量化纳入前视偏差")
    p.add_argument("--phases", action="store_true", help="调仓间隔扫遍相位再平均")
    p.add_argument("--start", default=None)
    args = p.parse_args(argv)

    members = load_universe()
    t0 = time.time()
    pnl = panel_mod.build_panel(members, start=args.start)
    close = pnl.close_px
    print(f"面板 {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   "
          f"{len(pnl.dates)} 日   {len(pnl.instruments)} 只   ({time.time() - t0:.0f}s)")

    mapping = universe_mod.inclusion_dates()
    if not mapping:
        print("拿不到纳入日期（接口失败且无缓存）。先联网跑一次。", file=sys.stderr)
        return 1
    eligible = universe_mod.eligibility_mask(pnl.dates, pnl.instruments, mapping)

    covered = sum(1 for c in pnl.instruments if c in mapping)
    print(f"纳入日期覆盖 {covered}/{len(pnl.instruments)} 只"
          f"（查不到的按'一直在池子里'放行）")
    per_day = eligible.sum(axis=1)
    print("\n各年实际在池子里的只数（这才是当年真正能选的范围）：")
    for year, n in per_day.groupby(per_day.index.year).mean().items():
        print(f"  {year}  {n:5.0f} 只   （原口径一律按 {len(pnl.instruments)} 只算）")

    asset_ret = pnl.open_to_open_returns()
    dates = pnl.dates[:-1]
    bench_raw = compute_metrics(
        asset_ret.loc[dates].mean(axis=1).fillna(0.0))
    # **基准也要修**：不修的话等于拿带偏差的尺子去量修过偏差的策略。
    bench_fix = compute_metrics(
        asset_ret.where(eligible).loc[dates].mean(axis=1).fillna(0.0))

    print(f"\n{'':16}{'原口径':>22}{'纳入日期修正':>22}{'差':>10}")
    print(f"{'':16}{'年化':>11}{'夏普':>11}{'年化':>11}{'夏普':>11}{'年化':>10}")
    print("-" * 70)
    print(f"{'等权买入持有':16}{bench_raw.annual_return:>11.2%}{bench_raw.sharpe:>11.2f}"
          f"{bench_fix.annual_return:>11.2%}{bench_fix.sharpe:>11.2f}"
          f"{bench_fix.annual_return - bench_raw.annual_return:>10.2%}")

    rows = []
    for label, factor, k, every, keep in CONFIGS:
        raw_scores = build_signal(factor, close).reindex(
            index=pnl.dates, columns=pnl.instruments)
        a0, s0, t_0 = run(raw_scores, pnl, k, every, keep, args.phases)
        a1, s1, t_1 = run(raw_scores.where(eligible), pnl, k, every, keep, args.phases)
        rows.append((label, a0, s0, a1, s1, a1 - a0, t_0, t_1))
        print(f"{label:16}{a0:>11.2%}{s0:>11.2f}{a1:>11.2%}{s1:>11.2f}{a1 - a0:>10.2%}",
              flush=True)

    _verdict(rows, bench_raw, bench_fix)
    return 0


def _verdict(rows, bench_raw, bench_fix) -> None:
    print("\n" + "-" * 70)
    print("怎么读")
    print("-" * 70)
    bench_gap = bench_fix.annual_return - bench_raw.annual_return
    print(f"  · 基准自己就差了 {bench_gap:+.2%} —— **偏差不是策略独有的**，")
    print("    它先污染了尺子。所以判据永远是「策略 vs 同口径基准」。")

    print("\n修正后仍跑赢基准的配置：")
    winners = [r for r in rows if r[3] > bench_fix.annual_return]
    if not winners:
        print("    （没有）—— 修掉纳入前视之后，没有一个手写因子还站得住。")
    else:
        for label, _a0, _s0, a1, s1, _d, _t0, _t1 in winners:
            edge = a1 - bench_fix.annual_return
            sharpe_edge = s1 - bench_fix.sharpe
            flag = "" if sharpe_edge > 0.05 else "   ← 但夏普没有实质改善"
            print(f"    {label:16} 年化 {a1:+.2%}（领先 {edge:+.2%}）"
                  f"  夏普 {s1:.2f}（{sharpe_edge:+.2f}）{flag}")

    print("\n**这个脚本没修的那一半**：历史上被剔出沪深300 的票仍然不在样本里。")
    print("方向明确（继续高估），幅度仍然未知 —— 要修得有历史剔除名单。")
    print("所以修正后的数字**仍然是上界，不是预期收益**。")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
