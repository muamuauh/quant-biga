"""对 {20,50,100,200} 日均线做 model-free 五年压力测试。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from qbg.backtest.metrics import compute_metrics  # noqa: E402
from qbg.backtest.panel import build_panel  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402


def evaluate(codes_list: list[str], windows: list[int], start: str | None = None) -> pd.DataFrame:
    """等权股票池收益乘以前一日收盘可知的 SMA 状态。"""
    pnl = build_panel(codes_list, start=start)
    asset_returns = pnl.open_to_open_returns().mean(axis=1).dropna()
    level = equal_weight_index(codes_list).reindex(pnl.dates).ffill()
    rows = []
    for window in windows:
        # t-1 收盘信号决定 t 开盘仓位；shift 是避免前视的关键。
        shifted = risk_on_series(level, window).shift(1)
        exposure = shifted.where(shifted.notna(), True).astype(bool)
        returns = asset_returns * exposure.reindex(asset_returns.index).fillna(True).astype(float)
        metrics = compute_metrics(returns, asset_returns)
        rows.append({"sma": window, **metrics.as_dict(),
                     "invested_pct": float(exposure.mean())})
    return pd.DataFrame(rows).set_index("sma")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A股市场择时均线压力测试")
    parser.add_argument("--start", default=settings.qbg_history_start)
    parser.add_argument("--windows", default="20,50,100,200")
    args = parser.parse_args(argv)
    members = load_universe() or sorted(p.stem for p in settings.parquet_dir.glob("*.parquet"))
    if not members:
        print("没有行情缓存，先运行 scripts/01_ingest.py", file=sys.stderr)
        return 1
    windows = [int(item) for item in args.windows.split(",") if item.strip()]
    result = evaluate(members, windows, args.start)
    print(result[["annual_return", "sharpe", "max_drawdown", "invested_pct"]].to_string(
        formatters={c: "{:.4f}".format for c in result.columns}
    ))
    best = int(result["sharpe"].idxmax())
    print(f"\n按样本内 Sharpe 最优：SMA={best}。请同时比较最大回撤，避免只追单一指标。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
