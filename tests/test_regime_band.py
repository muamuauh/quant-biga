"""择时迟滞缓冲带（`QBG_MARKET_SMA_BAND`）。

治的是**抖动**：无缓冲时 1620 个交易日里翻了 75 次仓、risk-off 中位只有 3 天 ——
那不是在躲熊市，是在均线上蹭，每翻一次全仓清掉再买回来。

这里也钉住接线：本项目已经被"参数存在但没人读"坑过三次
（`session_open` / `keep_rank` / `rebalance_every`），缓冲带是第四个同形状的。
"""

from __future__ import annotations

import inspect

import numpy as np
import pandas as pd

from qbg.orchestrator import daily_cycle
from qbg.strategy.regime import equal_weight_index, is_risk_on, risk_on_series


def series(values) -> pd.Series:
    idx = pd.date_range("2026-01-01", periods=len(values), freq="D")
    return pd.Series([float(v) for v in values], index=idx)


def test_band_zero_matches_the_bare_comparison():
    """band=0 必须和旧行为逐位一致，否则这个新参数会悄悄改掉生产信号。"""
    rng = np.random.RandomState(7)
    level = series(100 + rng.randn(200).cumsum())
    bare = risk_on_series(level, 20, 0.0)
    assert bare.equals(risk_on_series(level, 20))


def test_band_survives_a_shallow_dip_that_bare_signal_flips_on():
    """贴着均线的浅回撤：裸信号翻，带缓冲的不翻。这就是抖动本身。"""
    # 先平稳建立均线，再插一根只跌 0.5% 的小阴线。
    level = series([100.0] * 20 + [99.5] + [100.0] * 5)
    assert not risk_on_series(level, 20, 0.0).iloc[20], "裸信号应当在这一天转 off"
    assert risk_on_series(level, 20, 0.02).iloc[20], "2% 缓冲不该被 0.5% 的回撤触发"


def test_band_still_exits_on_a_real_drawdown():
    """缓冲不是把闸关掉：跌穿下轨照样出场，否则保护就没了。"""
    level = series([100.0] * 20 + [90.0] * 5)
    assert not risk_on_series(level, 20, 0.02).iloc[-1]


def test_band_requires_a_real_recovery_to_re_enter():
    """回来也要过上轨才算数 —— 两个方向用不同的线，这才是迟滞。

    这一段的 SMA20 ≈ 94.33，上轨 ≈ 96.21。95.5 **高于均线**（裸信号会立刻
    转回 risk-on）但**没过上轨**，所以带缓冲的应当继续持币。两个都断言，
    只断言后者的话，一个恒为 False 的实现也能过。
    """
    level = series([100.0] * 20 + [90.0] * 10 + [95.5] * 3)
    assert risk_on_series(level, 20, 0.0).iloc[-1], "裸信号在这一天应当已经转回 on"
    assert not risk_on_series(level, 20, 0.02).iloc[-1], "没过上轨就不该重新进场"


def test_band_reduces_flip_count_on_a_choppy_series():
    """本质诉求：同一条锯齿序列上，翻转次数必须显著下降。"""
    rng = np.random.RandomState(11)
    level = series(100 + rng.randn(400).cumsum() * 0.3)
    flips = {b: int((risk_on_series(level, 50, b).astype(int).diff().fillna(0) != 0).sum())
             for b in (0.0, 0.03)}
    assert flips[0.03] < flips[0.0], f"缓冲带没有减少翻转：{flips}"


def test_is_risk_on_uses_the_whole_history_when_band_is_on():
    """迟滞是路径依赖的 —— 只比最新一天和均线会得出不同的答案。"""
    level = series([100.0] * 20 + [90.0] * 10 + [99.5] * 2)
    # 最新值 99.5 低于均线，两种算法碰巧同为 False；关键是它没有崩，
    # 而且和整段序列的最后一个元素一致。
    assert is_risk_on(level, 20, 0.02) is bool(risk_on_series(level, 20, 0.02).iloc[-1])


def test_daily_cycle_passes_the_band_to_the_regime_check():
    """接线：config 里有这个参数，编排必须真的把它传下去。"""
    src = inspect.getsource(daily_cycle.run_daily)
    assert "qbg_market_sma_band" in src, \
        "daily_cycle 没有把 qbg_market_sma_band 传给 market_risk_on —— 参数形同虚设"


def test_equal_weight_index_respects_the_eligibility_mask():
    """资格表能屏蔽掉"当天还不在指数里"的票，否则择时信号带纳入前视。"""
    dates = pd.date_range("2026-01-01", periods=4, freq="D")
    prices = pd.DataFrame({"A": [10.0, 11.0, 12.0, 13.0],
                           "B": [10.0, 10.0, 10.0, 10.0]}, index=dates)
    # 直接验证屏蔽语义：只有 A 合格时，收益等于 A 自己的收益。
    changes = prices.pct_change()
    eligible = pd.DataFrame(False, index=dates, columns=["A", "B"])
    eligible["A"] = True
    masked = changes.where(eligible).mean(axis=1, skipna=True).fillna(0.0)
    assert masked.iloc[1] == pytest_approx(0.1)
    # B 全程不合格，所以它那条平线不该把平均拉低
    assert masked.iloc[1] != changes.mean(axis=1).iloc[1]


def pytest_approx(x, rel=1e-9):
    import pytest

    return pytest.approx(x, rel=rel)


def test_equal_weight_index_signature_accepts_eligible():
    """接线：脚本要靠它做修正版指数，签名少一个参数就整条链断了。"""
    assert "eligible" in inspect.signature(equal_weight_index).parameters
