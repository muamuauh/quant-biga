"""回测：读 parquet 缓存 → 构建面板 → 跑 top-K → 出中文报告。

    python scripts/06_backtest.py                       # 用当前股票池
    python scripts/06_backtest.py --k 5                 # 换持仓只数
    python scripts/06_backtest.py --scores momentum     # 换打分方式
    python scripts/06_backtest.py --start 2024-01-01

## 关于 --scores

P3 接上 qlib 模型之前，这里提供两种**占位打分**用来验证回测引擎本身：

    momentum  20 日动量（经典、有正 IC 但很弱）
    reversal  5 日反转（A股上历史更强）
    random    固定 seed 的随机数 —— **它的作用是校准**：随机分数的
              Rank IC 应当在 0 附近。如果它也显著为正，说明引擎里有
              前视偏差，任何"模型有效"的结论都不成立。

**这些不是策略**，不要拿它们的回测数字当预期收益。真正的信号在 P3。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qbg.backtest import engine  # noqa: E402
from qbg.backtest import panel as panel_mod
from qbg.config import load_universe, settings  # noqa: E402
from qbg.execution import fees  # noqa: E402
from qbg.utils.logging import get_logger, log_event  # noqa: E402

log = get_logger("qbg.scripts.backtest")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="A股 top-K 日频回测")
    p.add_argument("--k", type=int, default=settings.qbg_top_k, help="持仓只数")
    p.add_argument("--scores", default="reversal",
                   choices=["model", "momentum", "reversal", "random"],
                   help="打分方式；model 读取最近一次 cn_lgb 预测")
    p.add_argument("--no-neutralize", action="store_true",
                   help="model 模式下关闭申万一级行业中性化，用于对照")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--total-weight", type=float, default=0.95,
                   help="投资总仓位，其余留现金")
    p.add_argument("--codes", default="", help="只用这些票（逗号分隔）")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args(argv)


def build_scores(kind: str, panel: engine.Panel, seed: int) -> pd.DataFrame:
    """占位打分。全部只用**截至当日收盘**的信息，不含未来数据。"""
    close = panel.close_px
    if kind == "model":
        from qbg.strategy.predict import load_latest_predictions, predictions_to_frame

        frame = predictions_to_frame(load_latest_predictions())
        return frame
    if kind == "momentum":
        return close.pct_change(20)
    if kind == "reversal":
        return -close.pct_change(5)
    rng = np.random.RandomState(seed)
    return pd.DataFrame(rng.randn(*close.shape), index=close.index, columns=close.columns)


def main(argv=None) -> int:
    args = parse_args(argv)

    members = ([c.strip() for c in args.codes.split(",") if c.strip()]
               if args.codes else load_universe())
    if not members:
        members = _codes_from_cache()
    if not members:
        print("没有可用数据。先跑 python scripts/01_ingest.py", file=sys.stderr)
        return 1

    pnl = panel_mod.build_panel(members, start=args.start, end=args.end)
    if len(pnl.dates) < 30:
        print(f"数据太少（{len(pnl.dates)} 个交易日），无法回测。先跑 01_ingest.py",
              file=sys.stderr)
        return 1

    scores = build_scores(args.scores, pnl, args.seed)
    if args.scores == "model" and not args.no_neutralize and settings.qbg_industry_neutral:
        from qbg.strategy.predict import neutralize_frame

        scores = neutralize_frame(scores)
    result = engine.run_backtest(
        scores, pnl, k=args.k, total_weight=args.total_weight)

    _print_report(result, pnl, args)
    return 0


def _codes_from_cache() -> list[str]:
    """股票池文件还没生成时，退回"缓存里有什么就用什么"。"""
    d = settings.parquet_dir
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.parquet"))


def _pct(x: float) -> str:
    return f"{x * 100:+.2f}%"


def _print_report(res: engine.BacktestResult, pnl: engine.Panel, args) -> None:
    profile = fees.FeeProfile.load()
    print("\n" + "=" * 62)
    print(f"回测报告  ({args.scores} 打分 · top-{res.k})")
    print("=" * 62)
    print(f"区间      {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}  "
          f"({len(res.daily_returns)} 个持有期)")
    print(f"股票池    {len(pnl.instruments)} 只")

    s, b = res.strategy, res.benchmark
    print(f"\n{'':10}{'策略':>12}{'基准(等权买入持有)':>22}")
    print(f"{'累计收益':10}{_pct(s.total_return):>12}{_pct(b.total_return):>18}")
    print(f"{'年化收益':10}{_pct(s.annual_return):>12}{_pct(b.annual_return):>18}")
    print(f"{'年化波动':10}{_pct(s.annual_vol):>12}{_pct(b.annual_vol):>18}")
    print(f"{'夏普':10}{s.sharpe:>12.2f}{b.sharpe:>18.2f}")
    print(f"{'最大回撤':10}{_pct(s.max_drawdown):>12}{_pct(b.max_drawdown):>18}")
    print(f"{'胜率':10}{_pct(s.win_rate):>12}{_pct(b.win_rate):>18}")

    print(f"\nIC        {res.ic:+.4f}      Rank IC  {res.rank_ic:+.4f}")
    print(f"平均换手  {res.avg_turnover:.3f} /期")

    print(f"\n成本      买 {res.buy_cost_rate * 1e4:.1f}bp · "
          f"卖 {res.sell_cost_rate * 1e4:.1f}bp（含印花税）· "
          f"往返 {fees.round_trip_rate(profile) * 1e4:.1f}bp")

    if res.slippage_curve:
        print("\n滑点敏感性（在基础费率之上再叠）:")
        for slip in sorted(res.slippage_curve):
            m = res.slippage_curve[slip]
            print(f"  +{slip * 1e4:>4.0f}bp   年化 {_pct(m.annual_return):>9}   "
                  f"夏普 {m.sharpe:>6.2f}   回撤 {_pct(m.max_drawdown):>9}")

    blocked = res.blocked
    total_blocked = sum(blocked.values())
    if total_blocked:
        print(f"\n受阻调仓  {total_blocked} 次 "
              f"(停牌 {blocked['suspended']} · "
              f"涨停买不进 {blocked['limit_up_cannot_buy']} · "
              f"跌停卖不出 {blocked['limit_down_cannot_sell']})")

    _print_caveats(res, args, profile)
    log_event(log, "backtest.report", **res.as_dict()["strategy"])


def _print_caveats(res: engine.BacktestResult, args, profile) -> None:
    """把回测**不可信的地方**明确列出来。

    一份不说自己局限的回测报告比没有报告更危险——它会让人按一个自己
    并不理解其成立条件的数字去下注。
    """
    print("\n" + "-" * 62)
    print("这份回测不能说明什么")
    print("-" * 62)

    if args.scores == "random":
        print("  · 随机打分：Rank IC 应当在 0 附近。显著为正说明引擎有前视偏差")
    elif args.scores != "model":
        print(f"  · {args.scores} 是**占位打分**，不是策略。真正的信号在 P3(qlib 模型)")
    else:
        print("  · 模型测试段仍使用当前沪深300成分，存在显著生存者偏差")

    print("  · 生存者偏差：股票池是**当前**沪深300 成分，回测过去等于只测了")
    print("    活到今天的那批公司。收益被系统性高估，幅度无法估计")

    equity_guess = 100_000 * args.total_weight / max(res.k, 1)
    min_notional = fees.min_notional_for_rate(profile)
    if equity_guess < min_notional:
        print(f"  · 单笔约 {equity_guess:,.0f} 元 < {min_notional:,.0f} 元，")
        print("    最低 5 元佣金会让实际费率高于回测用的费率 → 成本被低估")

    total_blocked = sum(res.blocked.values())
    if total_blocked > len(res.daily_returns) * 0.05:
        print(f"  · 受阻调仓 {total_blocked} 次占比偏高，说明策略想做的事有相当")
        print("    一部分在真实市场里做不到，结论要打折")

    print("  · 成交假设是**次日开盘价**。真实执行是你手动敲单，会有额外偏差")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
