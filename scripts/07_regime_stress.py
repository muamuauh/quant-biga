"""对 {20,50,100,200} 日均线做 model-free 五年压力测试。

**择时不是免费的。** 每次状态翻转都要清空一次再买回一次，A股往返约 10bp
（佣金双边 + 印花税卖出单边 + 过户费双边）再叠滑点。短均线翻转更勤，
而在不计成本的测试里这一点完全看不出来 —— 2026-08-10 那次定下 SMA=20 的
压测就没有成本模型，而 SMA=20 在 2020–2026 翻转了 179 次，SMA=100 只有 63 次。

所以这里按翻转逐笔扣费，并沿用 backtest/engine.py 的习惯给出滑点敏感曲线。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from qbg.backtest.metrics import compute_metrics  # noqa: E402
from qbg.backtest.panel import build_panel  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.execution.fees import FeeProfile  # noqa: E402
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402


def switch_costs(exposure: pd.Series, slippage_bp: float = 0.0) -> pd.Series:
    """把每次仓位翻转的成本摊到当天的收益上。

    进场是买入（佣金 + 过户费），离场是卖出（多一道印花税 5bp）——
    **A股费用不对称，两个方向不能用同一个数**。滑点两个方向都收。
    """
    profile = FeeProfile.load()
    buy_rate = profile.commission_rate + profile.transfer_fee_rate + slippage_bp / 1e4
    sell_rate = (profile.commission_rate + profile.stamp_tax_rate
                 + profile.transfer_fee_rate + slippage_bp / 1e4)
    change = exposure.astype(float).diff().fillna(0.0)
    return change.clip(lower=0) * buy_rate + (-change).clip(lower=0) * sell_rate


def evaluate(codes_list: list[str], windows: list[int], start: str | None = None,
             slippage_bp: float = 0.0) -> pd.DataFrame:
    """等权股票池收益乘以前一日收盘可知的 SMA 状态，再扣掉翻转成本。"""
    pnl = build_panel(codes_list, start=start)
    asset_returns = pnl.open_to_open_returns().mean(axis=1).dropna()
    level = equal_weight_index(codes_list).reindex(pnl.dates).ffill()
    rows = []
    for window in windows:
        # t-1 收盘信号决定 t 开盘仓位；shift 是避免前视的关键。
        shifted = risk_on_series(level, window).shift(1)
        exposure = shifted.where(shifted.notna(), True).astype(bool)
        aligned = exposure.reindex(asset_returns.index).fillna(True).astype(float)
        gross = asset_returns * aligned
        costs = switch_costs(aligned, slippage_bp).reindex(gross.index).fillna(0.0)
        metrics = compute_metrics(gross - costs, asset_returns)
        rows.append({"sma": window, **metrics.as_dict(),
                     "invested_pct": float(aligned.mean()),
                     "switches": int((aligned.diff().fillna(0) != 0).sum()),
                     "cost_drag_pa": float(costs.sum() / len(costs) * 252)})
    return pd.DataFrame(rows).set_index("sma")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A股市场择时均线压力测试")
    parser.add_argument("--start", default=settings.qbg_history_start)
    parser.add_argument("--windows", default="20,50,100,200")
    parser.add_argument("--slippage-bp", default="0,10,20",
                        help="滑点敏感曲线，逗号分隔（bp）。散户限价单实测约 10~20bp")
    args = parser.parse_args(argv)
    members = load_universe() or sorted(p.stem for p in settings.parquet_dir.glob("*.parquet"))
    if not members:
        print("没有行情缓存，先运行 scripts/01_ingest.py", file=sys.stderr)
        return 1
    windows = [int(item) for item in args.windows.split(",") if item.strip()]
    columns = ["annual_return", "sharpe", "max_drawdown", "invested_pct",
               "switches", "cost_drag_pa"]
    for slippage in [float(x) for x in args.slippage_bp.split(",") if x.strip()]:
        result = evaluate(members, windows, args.start, slippage)
        print(f"\n=== 滑点 {slippage:.0f} bp ===")
        print(result[columns].to_string(
            formatters={c: "{:.4f}".format for c in columns if c != "switches"}))
        print(f"按样本内 Sharpe 最优：SMA={int(result['sharpe'].idxmax())}")
    print("\n注意：这里只算**择时翻转**的成本，不含日常调仓换手。"
          "请同时比较最大回撤，避免只追单一指标。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
