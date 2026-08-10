"""最新模型分数 → 风控 → 南京证券 APP 人工下单清单。"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from qbg.config import settings  # noqa: E402
from qbg.data import cache, meta  # noqa: E402
from qbg.execution.advisory import AdvisoryAdapter  # noqa: E402
from qbg.execution.order_planner import plan_orders  # noqa: E402
from qbg.report.order_sheet import render_markdown  # noqa: E402
from qbg.risk.gates import load_limits, run_all_gates  # noqa: E402
from qbg.strategy.predict import latest_date_scores, load_latest_predictions  # noqa: E402
from qbg.strategy.regime import market_risk_on  # noqa: E402
from qbg.strategy.topk_weights import affordable_scores, topk_equal_weight  # noqa: E402


def _positions(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["code", "qty", "sellable_qty"])
    frame = pd.read_csv(path)
    for column in ("qty", "sellable_qty"):
        if column not in frame:
            frame[column] = frame.get("qty", 0)
    return frame


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="生成 A股顾问模式下单清单")
    parser.add_argument("--positions", type=Path, default=settings.positions_csv)
    parser.add_argument("--equity", type=float, default=100_000)
    parser.add_argument("--cash", type=float, default=100_000)
    parser.add_argument("--today-pnl", type=float, default=0.0)
    parser.add_argument("--asof", default=date.today().isoformat())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    positions = _positions(args.positions)
    current = {str(row.code): int(row.qty) for row in positions.itertuples()}
    sellable = {str(row.code): int(row.sellable_qty) for row in positions.itertuples()}
    candidates = latest_date_scores(load_latest_predictions(), neutralize=bool(settings.qbg_industry_neutral))
    codes_list = list(dict.fromkeys(list(candidates.index) + list(current)))
    names = meta.load_cached()
    last, previous, st, suspended, latest_dates = {}, {}, {}, {}, []
    for code in codes_list:
        frame = cache.read(code)
        if frame.empty:
            continue
        row = frame.iloc[-1]
        last[code] = float(row["close"])
        previous[code] = float(frame.iloc[-2]["close"] if len(frame) > 1 else row["close"])
        st[code], suspended[code] = bool(row["is_st"]), bool(row["is_suspended"])
        latest_dates.append(pd.Timestamp(row["date"]))

    limits = load_limits()
    filtered = affordable_scores(candidates, last, args.equity, settings.qbg_top_k,
                                 cap=float(limits["max_position_pct"]))
    risk_on = market_risk_on(list(last), settings.qbg_market_sma)
    targets = topk_equal_weight(filtered, settings.qbg_top_k, 0.95,
                                set(current), settings.qbg_keep_rank) if risk_on else {}
    orders = plan_orders(targets, current, last, previous, args.equity, names=names, is_st=st,
                         slippage=float(limits["order_slippage_pct"]),
                         drift_band=float(limits["rebalance_drift_band"]))
    hard_ok, allowed, results = run_all_gates(
        target_weights=targets, orders=orders, current_cash=args.cash, total_equity=args.equity,
        today_pnl=args.today_pnl, latest_data_date=max(latest_dates) if latest_dates else None,
        asof=args.asof, prev_close=previous, market_price=last, is_st=st, suspended=suspended,
        sellable_qty=sellable, current_qty=current, limits=limits,
    )
    print(render_markdown(allowed, results))
    if not hard_ok:
        return 2
    if not args.dry_run:
        result = AdvisoryAdapter().submit(allowed, args.asof, results)
        print("\n" + result.message + "：" + "、".join(result.artifacts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

