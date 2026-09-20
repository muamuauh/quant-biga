"""反事实：某段区间里，各择时窗口分别会让这个账户变成多少钱。

回答的是一个会**反复被问**的问题：「这轮空仓到底值不值？」

压测（`07_regime_stress.py`）给的是六年半的平均表现，它无法回答"眼下这 31 天
我是不是白等了"。而后者才是人在盘前真正想知道的事 —— 也正是最容易凭感觉
乱调参数的时刻。所以把它做成一个能随时跑的工具，用数字代替感觉。

口径和 `07_regime_stress.py` 完全一致（等权全池、次日开盘成交、按翻转逐笔
扣费），因此两边的数字可以直接对照。

**这不是 top-k 组合的回测。** 择时参数本来就是在等权全池上选的，所以反事实
也用同一口径；换成 3 只集中持仓，波动会明显更大。

用法：
    python scripts/08_regime_counterfactual.py --start 2026-07-16
    python scripts/08_regime_counterfactual.py --start 2026-07-16 --capital 200000
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import pandas as pd  # noqa: E402

from qbg.backtest.panel import build_panel  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402

_stress = __import__("07_regime_stress")
from qbg.utils.console import make_output_safe  # noqa: E402

switch_costs = _stress.switch_costs


def evaluate(start: str, capital: float, windows: list[int],
             slippage_bp: float) -> pd.DataFrame:
    members = load_universe() or sorted(p.stem for p in settings.parquet_dir.glob("*.parquet"))
    panel = build_panel(members, start=settings.qbg_history_start)
    returns = panel.open_to_open_returns().mean(axis=1).dropna()
    level = equal_weight_index(members).reindex(panel.dates).ffill()
    window = returns.index[returns.index >= pd.Timestamp(start)]
    if len(window) == 0:
        raise SystemExit(f"{start} 之后没有行情数据")

    rows = []
    for w in windows:
        if w <= 0:
            exposure = pd.Series(1.0, index=returns.index)
            label = "不择时（满仓）"
        else:
            # t-1 收盘信号决定 t 开盘仓位；shift 是避免前视的关键。
            shifted = risk_on_series(level, w).shift(1)
            exposure = shifted.where(shifted.notna(), True).astype(float)
            exposure = exposure.reindex(returns.index).fillna(1.0)
            label = f"SMA={w}"
        costs = switch_costs(exposure, slippage_bp).reindex(returns.index).fillna(0.0)
        net = (returns * exposure - costs).loc[window]
        curve = (1 + net).cumprod()
        rows.append({
            "策略": label,
            "期末资金": capital * float(curve.iloc[-1]),
            "收益": float(curve.iloc[-1]) - 1,
            "最大回撤": float((curve / curve.cummax() - 1).min()),
            "在场天数": int(exposure.loc[window].sum()),
            "翻转": int((exposure.loc[window].diff().fillna(0) != 0).sum()),
            "成本": capital * float(costs.loc[window].sum()),
        })
    frame = pd.DataFrame(rows).set_index("策略")
    frame.attrs["window"] = (window[0].date(), window[-1].date(), len(window))
    return frame


def main(argv=None) -> int:
    make_output_safe()
    parser = argparse.ArgumentParser(description="择时窗口的区间反事实")
    parser.add_argument("--start", required=True, help="区间起点 YYYY-MM-DD")
    parser.add_argument("--capital", type=float, default=200_000.0)
    parser.add_argument("--windows", default="0,10,20,50,100,150,200")
    parser.add_argument("--slippage-bp", type=float, default=10.0,
                        help="散户限价单实测约 10~20 bp")
    args = parser.parse_args(argv)

    windows = [int(x) for x in args.windows.split(",") if x.strip()]
    frame = evaluate(args.start, args.capital, windows, args.slippage_bp)
    first, last, count = frame.attrs["window"]
    print(f"窗口 {first} ~ {last}   共 {count} 个交易日")
    print(f"起始资金 ¥{args.capital:,.2f}   滑点 {args.slippage_bp:.0f} bp\n")
    print(frame.to_string(formatters={
        "期末资金": "{:,.2f}".format, "收益": "{:+.2%}".format,
        "最大回撤": "{:.2%}".format, "成本": "{:,.0f}".format}))

    if "不择时（满仓）" in frame.index:
        hold = frame.loc["不择时（满仓）"]
        current = f"SMA={settings.qbg_market_sma}"
        if current in frame.index:
            row = frame.loc[current]
            print(f"\n当前配置 {current} vs 满仓不择时："
                  f"{row['期末资金'] - hold['期末资金']:+,.2f} 元"
                  f"（{row['收益'] - hold['收益']:+.2%}）")
            print(f"最大回撤差：{row['最大回撤'] - hold['最大回撤']:+.2%}"
                  "（负数 = 当前配置回撤更小）")
    print("\n⚠ 单个区间证明不了参数好坏 —— 依据是 07_regime_stress.py 的六年半压测。"
          "这个工具是用来看清「这次到底发生了什么」，不是用来据此调参的。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
