"""相位平均。全离线。

2026-09-14 的教训：`12_rebalance_gate.py` 的平均只覆盖了头条指标，子区间闸用的
`daily_returns` 通过 `__getattr__` 落到了第 0 个相位。头条是平均的、子区间是运气
—— 而且表面上完全看不出来。这里钉住"每一个喂给闸的数都按相位平均"。
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pandas as pd

from qbg.backtest import phases


def _fake_run(daily, annual, sharpe=1.0, dd=-0.1, turnover=0.1, exits=0):
    index = pd.bdate_range("2026-01-05", periods=len(daily))
    return SimpleNamespace(
        daily_returns=pd.Series(daily, index=index),
        strategy=SimpleNamespace(annual_return=annual, sharpe=sharpe, max_drawdown=dd),
        avg_turnover=turnover, rank_ic=0.02, trailing_exits=exits)


def test_headline_metrics_are_phase_means():
    res = phases.PhaseResult([_fake_run([0.0] * 8, 0.10, sharpe=1.0, exits=2),
                              _fake_run([0.0] * 8, 0.30, sharpe=2.0, exits=4)])
    assert np.isclose(res.annual_return, 0.20)
    assert np.isclose(res.sharpe, 1.5)
    assert np.isclose(res.trailing_exits, 3.0)
    assert np.isclose(res.phase_spread, 0.20)


def test_subperiods_average_every_phase_not_just_phase_zero():
    """**这就是那个 bug。** 相位 0 前半段大涨、相位 1 前半段大跌 ——
    只看相位 0 会把前半段报成大赚。"""
    up, down = [0.02] * 4 + [0.0] * 4, [-0.02] * 4 + [0.0] * 4
    res = phases.PhaseResult([_fake_run(up, 0.0), _fake_run(down, 0.0)])
    first_half = res.subperiod_annual(2)[0]
    phase0_only = phases._annual(np.array(up[:4]))
    assert first_half < phase0_only / 2, "子区间仍然只看了相位 0"
    both = (phases._annual(np.array(up[:4])) + phases._annual(np.array(down[:4]))) / 2
    assert np.isclose(first_half, both)


def test_facts_has_exactly_what_the_gate_reads():
    res = phases.PhaseResult([_fake_run([0.0] * 4, 0.1)])
    assert set(res.facts()) == {"annual_return", "sharpe", "max_drawdown",
                                "avg_turnover", "rank_ic"}


def test_annualizes_with_a_share_trading_days():
    """A股一年约 244 个交易日，不是美股的 252。"""
    daily = np.full(244, 0.001)
    assert np.isclose(phases._annual(daily), 1.001 ** 244 - 1)
