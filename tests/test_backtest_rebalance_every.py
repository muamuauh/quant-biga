"""回测引擎的调仓间隔（`rebalance_every`）。

引擎此前**根本表达不了这件事**：实盘有 `QBG_REBALANCE_EVERY_DAYS`，
而回测只会每天调仓，于是任何关于调仓频率的结论都无从验证 ——
包括「每日调仓是不是太贵了」这个直接关系到成本的问题。

难点在于引擎会建模漂移（`w_prev = w_new * (1 + r)`）：光把分数 ffill 不够，
目标不变但持仓每天漂，引擎照样会把它拉回去 —— 那是"每天调仓到一个过期目标"，
恰恰是这个参数要避免的事。所以非调仓日的目标必须是**当前持仓本身**。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from qbg.backtest import engine


def _panel(days=20, names=("A", "B", "C", "D")):
    dates = pd.bdate_range("2026-01-01", periods=days)
    rng = np.random.RandomState(7)
    frame = lambda v: pd.DataFrame(v, index=dates, columns=list(names))  # noqa: E731
    close = frame(100 * (1 + rng.randn(days, len(names)) * 0.01).cumprod(axis=0))
    return engine.Panel(
        open_px=close.shift(1).bfill(), close_px=close,
        suspended=frame(False), limit_up_open=frame(False), limit_down_open=frame(False),
    )


def _alternating_scores(panel):
    """每天都把排名整个翻过来 —— 每日调仓会疯狂换手，低频则几乎不动。"""
    rows = []
    for i in range(len(panel.dates)):
        order = list(range(len(panel.instruments)))
        if i % 2:
            order.reverse()
        rows.append(order)
    return pd.DataFrame(rows, index=panel.dates, columns=panel.instruments, dtype=float)


def test_daily_is_the_default():
    panel = _panel()
    scores = _alternating_scores(panel)
    assert (engine.run_backtest(scores, panel, k=2).avg_turnover
            == engine.run_backtest(scores, panel, k=2, rebalance_every=1).avg_turnover)


def test_reported_turnover_counts_trades_not_drift():
    """换手率必须只算**真正成交的量**。

    `w_prev` 含当天的价格漂移，所以 `weights.diff()` 度量的是「漂移 + 交易」。
    日频调仓时漂移占比小、看不出来；调仓间隔一拉长，漂移就成了主要成分 ——
    于是低频策略会被报出一个它根本没付的换手，正好在比较调仓频率时误导最大。
    """
    panel = _panel(days=21)
    scores = _alternating_scores(panel)
    # 间隔比样本长 → 全程一次交易都没有（第 0 天目标为 0，见下一条测试）。
    res = engine.run_backtest(scores, panel, k=2, rebalance_every=100)
    assert res.avg_turnover == 0.0

    # 而按权重差算的话，只要有持仓就会因为漂移而非零。
    held = engine.run_backtest(scores, panel, k=2, rebalance_every=5)
    from qbg.backtest.metrics import turnover as weight_diff_turnover
    drifty = float(weight_diff_turnover(held.weights).mean())
    assert drifty > held.avg_turnover, "权重差口径应当高于真实成交口径"


def test_longer_interval_really_cuts_traded_turnover():
    """这是这个参数存在的全部理由：换手 = 成本。"""
    panel = _panel(days=60)
    scores = _alternating_scores(panel)
    daily = engine.run_backtest(scores, panel, k=2, rebalance_every=1)
    slow = engine.run_backtest(scores, panel, k=2, rebalance_every=10)
    assert slow.avg_turnover < daily.avg_turnover * 0.35


def test_first_interval_is_flat_because_of_the_signal_shift():
    """第 0 天的目标恒为 0（`target_weights_from_scores` 里那个 shift(1)：
    t-1 的分数决定 t 的持仓，而第 0 天没有前一天）。

    所以长间隔下**第一个调仓区间是空仓的** —— 这不是 bug，是信号对齐的
    必然结果。写下来是因为它看起来很像 bug。
    """
    panel = _panel(days=12)
    scores = _alternating_scores(panel)
    res = engine.run_backtest(scores, panel, k=2, rebalance_every=100)
    assert res.avg_turnover == 0.0
    assert float(res.weights.abs().to_numpy().sum()) == 0.0


def test_interval_does_not_break_the_gross_return_path():
    """低频不该凭空改变收益的量级 —— 它省的是成本，不是制造 alpha。"""
    panel = _panel(days=40)
    scores = _alternating_scores(panel)
    for every in (1, 5, 10):
        res = engine.run_backtest(scores, panel, k=2, rebalance_every=every)
        assert np.isfinite(res.strategy.annual_return)
        assert len(res.daily_returns) == len(panel.dates) - 1
