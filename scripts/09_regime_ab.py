"""择时闸在**真实策略**（top_k 模型选股）上到底值不值 —— 开/关的 A/B 对照。

关于 `QBG_MARKET_SMA` 的其它证据（`07_regime_stress.py`）全部来自 model-free
等权全池。但实盘跑的是 k=3 的集中持仓，波动和回撤完全是另一个量级，
所以「择时能削多少回撤」在等权全池上得到的答案未必成立。

做法：同一套模型分数、同一个回测引擎（T+1 / 涨跌停 / 停牌 / 不对称费率 /
次日开盘成交都在引擎里），两个分支只差一件事 —— risk-off 那天把当日分数整行
置 NaN，于是 `target_weights_from_scores` 给出零权重、组合转全现金。
**不改引擎**，所以两个分支之间没有任何实现差异。

中性化按 `QBG_INDUSTRY_NEUTRAL` 走，和 `06_backtest.py --scores model`
以及实盘 `daily_cycle` 保持一致 —— 拿未中性化的分数做 A/B 会得出一组
和实盘无关的数字。

用法：
    python scripts/09_regime_ab.py
    python scripts/09_regime_ab.py --windows 0,20,50,100,200 --k 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.backtest import engine  # noqa: E402
from qbg.backtest import panel as panel_mod  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.strategy.predict import (  # noqa: E402
    load_latest_predictions,
    neutralize_frame,
    predictions_to_frame,
)
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402


def main(argv=None) -> int:
    make_output_safe()
    parser = argparse.ArgumentParser(description="择时开/关在真实 top-K 策略上的 A/B")
    parser.add_argument("--k", type=int, default=settings.qbg_top_k)
    parser.add_argument("--windows", default="0,20,50,100,200",
                        help="0 表示关闭择时")
    parser.add_argument("--no-neutralize", action="store_true")
    args = parser.parse_args(argv)

    members = load_universe()
    scores = predictions_to_frame(load_latest_predictions())
    if not args.no_neutralize and settings.qbg_industry_neutral:
        scores = neutralize_frame(scores)

    panel = panel_mod.build_panel(members, start=str(scores.index.min().date()),
                                  end=str(scores.index.max().date()))
    scores = scores.reindex(index=panel.dates, columns=panel.instruments)
    # **信号在完整历史上算，不能先 reindex 到模型窗再算 SMA。**
    # `risk_on_series` 的 min_periods=sma_window：388 天的窗口里 SMA100 前 100 天
    # 没有信号（默认 risk-on）、SMA200 只剩 188 天、更长的干脆整段没有 —— 长窗口
    # 会静默退化成「不择时」，看起来像「长 SMA 更差」。生产的 `market_risk_on()`
    # 读的是完整 parquet 历史。2026-09-08 修，和 19/20 号是同一个坑。
    level = equal_weight_index(members)

    print(f"窗口 {panel.dates[0].date()} ~ {panel.dates[-1].date()}   "
          f"{len(panel.dates)} 个交易日   k={args.k}   "
          f"中性化={'否' if args.no_neutralize else bool(settings.qbg_industry_neutral)}\n")
    print(f"{'择时':<12}{'年化':>10}{'Sharpe':>9}{'最大回撤':>10}"
          f"{'换手':>8}{'在场':>8}{'@10bp年化':>11}{'@10bp回撤':>11}")
    print("-" * 80)

    for w in [int(x) for x in args.windows.split(",") if x.strip()]:
        if w <= 0:
            masked, label, invested = scores, "关闭择时", 1.0
        else:
            on = risk_on_series(level, w).reindex(panel.dates).ffill().fillna(True)
            masked = scores.where(on)
            label, invested = f"SMA={w}", float(on.mean())
        res = engine.run_backtest(masked, panel, k=args.k)
        m, slip = res.strategy, res.slippage_curve.get(0.001)
        print(f"{label:<12}{m.annual_return:>+9.2%}{m.sharpe:>9.4f}{m.max_drawdown:>10.2%}"
              f"{res.avg_turnover:>8.3f}{invested:>8.1%}"
              f"{slip.annual_return if slip else float('nan'):>+10.2%}"
              f"{slip.max_drawdown if slip else float('nan'):>11.2%}")

    print("\n⚠ 三条限制，读结论前必须记住：")
    print("  1. 只覆盖模型的 test 段（约 1.5 年），择时状态翻转次数很少 ——")
    print("     这点样本量不足以区分 50/100/200，只够看出「开/关」的方向。")
    print("  2. 绝对收益不可当预期：股票池有生存者偏差，且 +30bp 滑点下策略本身转负。")
    print("  3. 两个分支共用同一套分数，所以**相对**比较比绝对数字可信得多。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
from qbg.utils.console import make_output_safe  # noqa: E402
