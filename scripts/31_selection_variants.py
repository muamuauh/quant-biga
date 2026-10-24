"""选股的风险结构：同一个模型，只改"怎么从排名里挑"。探索性比较，不是八项闸。

## 问的是什么

2026-10-24 复盘：09-22 → 10-23 回撤 −20.2%，2/3 来自选股（8 只里 7 只跑输等权）。
模型本身没坏（最近两周 Rank IC +0.023，和回测一个水平），但它最近明显偏向**高波动、
成交火爆**的票，而这两个因子在 10 月刚好回撤；k=3 把整个账户押在排名最前的 3 只上，
一次风格回撤就直接变成账户回撤。

坏月份之后最容易冒出来的念头是"以后避开高波动票"。这个脚本就是在问：真这么做会怎样？

  A 基线 k=3  /  B k=5  /  C 剔除波动率最高的 20%  /  D 每个行业最多 1 只  /  E C+D

## 结论（2026-10-24，两个窗口、跑遍相位）

- **C 剔除高波动会砍掉约 2/3 的收益**（cur +61.9% → +20.4%，mid +43.2% → +18.1%）。
  模型的超额正好来自那些高波动票 —— 这个月的亏损是拿那份收益要付的代价。
  **"这个月亏在高波动票上"≠"以后该避开高波动票"**，坏月份之后它看起来最有道理。
- **B k=5 是唯一在两个窗口上都一致改善风险的**：夏普 1.25→1.40 / 0.99→1.03，最大回撤
  少 4~5 个点，坏月份没那么坏，相位极差大降；代价是年化少 2~3 个点。
  **但这里没算资金约束** —— 实仓 4 万时的结论见 `30_topk_gate.py`。
- D 行业上限：cur 窗口有一点帮助，mid 几乎没用。不稳定。

剔除高波动、行业上限都是对分数做掩码再交给引擎；被掩掉的已持有票也失去迟滞保护，
所以换手会略高于真实实现。作为"值不值得做"的判断够用，真要上线得走八项闸。

用法：
    python scripts/31_selection_variants.py
    python scripts/31_selection_variants.py --vol-cut 0.9     # 只剔最高的 10%
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qbg.backtest import panel as panel_mod  # noqa: E402
from qbg.backtest import phases  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.data import cache, industry  # noqa: E402
from qbg.strategy.predict import (  # noqa: E402
    load_latest_predictions,
    neutralize_frame,
    predictions_to_frame,
)

WIN = 22   # "坏月份"：22 个交易日滚动收益的 5% 分位


def vol_frame(codes, dates) -> pd.DataFrame:
    closes = {}
    for code in codes:
        frame = cache.read(code)
        if not frame.empty:
            closes[code] = frame.set_index(pd.to_datetime(frame["date"]))["close"]
    close = pd.DataFrame(closes).sort_index()
    # 截至 t 日收盘的 20 日波动 —— 和 t 日的打分用的是同一时点的信息，不偷看
    return close.pct_change().rolling(20).std().reindex(index=dates, columns=codes)


def drop_high_vol(scores: pd.DataFrame, vol: pd.DataFrame, cut: float) -> pd.DataFrame:
    return scores.where(~(vol.rank(axis=1, pct=True) > cut))


def one_per_industry(scores: pd.DataFrame, mapping: dict[str, str]) -> pd.DataFrame:
    out = scores.copy()
    labels = pd.Series({code: mapping.get(code, code) for code in scores.columns})
    for day, row in scores.iterrows():
        s = row.dropna()
        if s.empty:
            continue
        keep = set(s.groupby(labels.reindex(s.index)).idxmax().values)
        out.loc[day, [code for code in s.index if code not in keep]] = np.nan
    return out


def summarize(res) -> tuple:
    windows = [((1 + run.daily_returns).rolling(WIN).apply(np.prod, raw=True) - 1).dropna()
               for run in res.runs]
    w = pd.concat(windows)
    return (res.annual_return, res.sharpe, res.max_drawdown, w.quantile(0.05),
            (w <= -0.15).mean(), res.phase_spread)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="选股风险结构的探索性比较（不是八项闸）")
    parser.add_argument("--vol-cut", type=float, default=0.80,
                        help="剔除波动率分位高于它的票。默认 0.8 = 剔除最高的 20%%")
    parser.add_argument("--keep-rank", type=int, default=settings.qbg_keep_rank)
    parser.add_argument("--every", type=int, default=settings.qbg_rebalance_every_days)
    parser.add_argument("--stop", type=float, default=0.08)
    parser.add_argument("--slippage", type=float, default=0.0020)
    args = parser.parse_args(argv)

    mapping = industry.load_cached()
    members = load_universe()
    for exp in (None, "cn_lgb_mid"):
        raw = predictions_to_frame(load_latest_predictions(exp) if exp else load_latest_predictions())
        if settings.qbg_industry_neutral:
            raw = neutralize_frame(raw)
        panel = panel_mod.build_panel(members, start=str(raw.index.min().date()),
                                      end=str(raw.index.max().date()))
        base = raw.reindex(index=panel.dates, columns=panel.instruments)
        vol = vol_frame(list(panel.instruments), panel.dates)
        variants = {
            "A 基线 k=3": (base, 3),
            "B k=5": (base, 5),
            "C 剔除高波动 k=3": (drop_high_vol(base, vol, args.vol_cut), 3),
            "D 行业上限 k=3": (one_per_industry(base, mapping), 3),
            "E C+D k=3": (one_per_industry(drop_high_vol(base, vol, args.vol_cut), mapping), 3),
        }
        print(f"\n===== {exp or 'cn_lgb（生产，cur）'}  {panel.dates[0].date()} ~ "
              f"{panel.dates[-1].date()} =====")
        print(f"{'方案':<16}{'净年化':>9}{'夏普':>7}{'最大回撤':>9}{'坏月份(5%)':>11}"
              f"{'月亏≥15%':>9}{'相位极差':>9}")
        for name, (sc, k) in variants.items():
            res = phases.run_phases(sc, panel, rebalance_every=args.every, k=k,
                                    keep_rank=args.keep_rank, extra_slippage=args.slippage,
                                    stop_loss=args.stop)
            a, s, dd, q5, p15, spread = summarize(res)
            print(f"{name:<16}{a:>+9.1%}{s:>7.2f}{dd:>9.1%}{q5:>+11.1%}{p15:>9.1%}{spread:>9.1%}",
                  flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
