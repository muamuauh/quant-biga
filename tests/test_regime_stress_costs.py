"""择时压测的翻转成本模型。

为什么单开一个文件：定 `QBG_MARKET_SMA` 的那次压测（2026-08-10）**没有成本
模型** —— `returns = asset_returns * exposure`，状态翻转是免费的。于是翻转最勤
的 SMA=20 拿到最高 Sharpe，而它在 2020–2026 翻转了 179 次，SMA=100 只有 63 次。
不把成本加进去，这个参数就是在一个不存在的世界里选出来的。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
_stress = __import__("07_regime_stress")
switch_costs = _stress.switch_costs

from qbg.execution.fees import FeeProfile  # noqa: E402


@pytest.fixture
def profile():
    return FeeProfile.load()


def _exposure(*values) -> pd.Series:
    return pd.Series([float(v) for v in values])


def test_holding_still_costs_nothing(profile):
    """一直满仓或一直空仓都不该产生费用。"""
    assert switch_costs(_exposure(1, 1, 1, 1)).sum() == 0.0
    assert switch_costs(_exposure(0, 0, 0, 0)).sum() == 0.0


def test_exit_costs_more_than_entry(profile):
    """**A股费用不对称**：卖出多一道印花税 5bp，两个方向不能用同一个数。"""
    enter = switch_costs(_exposure(0, 1)).iloc[1]
    exit_ = switch_costs(_exposure(1, 0)).iloc[1]
    assert exit_ > enter
    assert exit_ - enter == pytest.approx(profile.stamp_tax_rate)


def test_cost_lands_on_the_day_of_the_switch(profile):
    costs = switch_costs(_exposure(1, 1, 0, 0, 1))
    assert costs.iloc[0] == 0.0 and costs.iloc[1] == 0.0
    assert costs.iloc[2] > 0, "离场那天要记卖出成本"
    assert costs.iloc[3] == 0.0
    assert costs.iloc[4] > 0, "回场那天要记买入成本"


def test_round_trip_is_about_ten_bp(profile):
    """一次完整往返约 10bp —— 和 fee_profile.yaml 末尾的参考值对得上。"""
    round_trip = switch_costs(_exposure(0, 1, 0)).sum()
    assert round_trip == pytest.approx(10.2e-4, rel=0.05)


def test_slippage_is_charged_both_ways(profile):
    """滑点买卖都收，20bp 往返就是 40bp。"""
    base = switch_costs(_exposure(0, 1, 0)).sum()
    with_slip = switch_costs(_exposure(0, 1, 0), slippage_bp=20).sum()
    assert with_slip - base == pytest.approx(40e-4)


def test_more_switches_cost_more(profile):
    """把结论钉死：翻转越勤成本越高 —— 这正是不计成本的压测看不见的东西。"""
    calm = switch_costs(_exposure(0, 1, 1, 1, 1, 1, 1, 1)).sum()
    choppy = switch_costs(_exposure(0, 1, 0, 1, 0, 1, 0, 1)).sum()
    assert choppy > calm * 3
