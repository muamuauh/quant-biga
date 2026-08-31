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


# ---------------------------------------------------------------------------
# equal_weight_index 必须是**等权组合的净值曲线**，不是归一化价格的平均。
#
# 2026-08-31 查出来的口径 bug：原实现把每只股票按首值归一后取平均，于是
# 涨了 5 倍的票权重就是没涨的 5 倍 —— 那不是任何真实组合的净值。
# 后果是**用组合 B 的信号去择时组合 A**：本轮 risk-off 窗口里，等权组合
# +1.96% 而那条"指数"是 -2.16%，方向相反。而 QBG_MARKET_SMA 那张压测表
# 本身就是在这个错信号上选出来的。
# ---------------------------------------------------------------------------
def _fake_cache(frames):
    def _read(code, root=None):
        return frames[code]
    return _read


def _bars(prices):
    return pd.DataFrame({"date": pd.date_range("2026-01-01", periods=len(prices)),
                         "close": prices, "factor": [1.0] * len(prices)})


def test_index_is_the_equal_weight_portfolio_nav(monkeypatch):
    """一只翻倍、一只腰斩 —— 等权组合应基本持平，而不是被大涨的那只带飞。"""
    from qbg.strategy import regime

    frames = {"A": _bars([1.0, 2.0]), "B": _bars([1.0, 0.5])}
    monkeypatch.setattr(regime.cache, "read", _fake_cache(frames))
    nav = regime.equal_weight_index(["A", "B"])
    # 等权：一只 +100%、一只 -50%，平均 +25%
    assert nav.iloc[-1] / nav.iloc[0] - 1 == pytest.approx(0.25)


def test_high_growth_names_do_not_dominate(monkeypatch):
    """旧实现的病灶：某只票历史涨幅越大，它对当日信号的影响越大。

    A 从 1 涨到 100 再到 101（+1%），B 从 1 到 1 再到 1.5（+50%）。
    等权组合当日应是 +25.5%；旧的"归一化取平均"会被 A 的绝对水平压住。
    """
    from qbg.strategy import regime

    frames = {"A": _bars([1.0, 100.0, 101.0]), "B": _bars([1.0, 1.0, 1.5])}
    monkeypatch.setattr(regime.cache, "read", _fake_cache(frames))
    nav = regime.equal_weight_index(["A", "B"])
    last_day = nav.iloc[-1] / nav.iloc[-2] - 1
    assert last_day == pytest.approx((0.01 + 0.50) / 2)


def test_unlisted_names_do_not_drag_the_average(monkeypatch):
    """还没上市的票当天不参与平均，成分变化不该在曲线上留下跳变。"""
    from qbg.strategy import regime

    late = _bars([float("nan"), 1.0, 1.2])
    frames = {"A": _bars([1.0, 1.1, 1.21]), "B": late}
    monkeypatch.setattr(regime.cache, "read", _fake_cache(frames))
    nav = regime.equal_weight_index(["A", "B"])
    assert nav.notna().all()
    assert (nav > 0).all()


def test_empty_universe_is_empty_not_a_crash(monkeypatch):
    from qbg.strategy import regime

    monkeypatch.setattr(regime.cache, "read", lambda *a, **k: pd.DataFrame())
    assert regime.equal_weight_index(["A"]).empty
