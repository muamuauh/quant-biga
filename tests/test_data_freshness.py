"""数据新鲜度硬闸必须按**交易日**计数，不能按工作日。

背景：2026-08-22 把定时任务从「当天 18:15」改成「次日 07:30」之后，
最新数据必然是**上一交易日**的收盘，`max_stale_days=1` 的余量就归零了。
此时若按 `pd.bdate_range`（只排除周末）计数，2026 年会有 6 天被误伤：
春节 / 清明 / 劳动 / 端午 / 中秋 / 国庆之后的第一个交易日。

误伤的代价不只是「当天没清单」—— 硬闸中止的退出码是 2，语义是
「风控拒绝交易，需要人看」。一年 6 次为非问题拉最高级警报，
会让人以后不再认真看它。
"""

from __future__ import annotations

import pytest

from qbg.market import calendar
from qbg.risk import gates

LIMITS = {"max_stale_days": 1}


def _ok(latest, asof):
    return gates.data_freshness_guard(latest, asof, LIMITS).passed


# 2026 年真实的长假边界：(最后一个交易日, 假期后第一个交易日)
HOLIDAY_BOUNDARIES = [
    pytest.param("2026-02-13", "2026-02-24", id="春节"),
    pytest.param("2026-04-03", "2026-04-07", id="清明"),
    pytest.param("2026-04-30", "2026-05-06", id="劳动节"),
    pytest.param("2026-06-18", "2026-06-22", id="端午"),
    pytest.param("2026-09-24", "2026-09-28", id="中秋"),
    pytest.param("2026-09-30", "2026-10-08", id="国庆"),
]


@pytest.mark.parametrize(("latest", "asof"), HOLIDAY_BOUNDARIES)
def test_holiday_gap_is_not_stale(latest, asof):
    """假期后第一个交易日：上一根 K 线就是最近一个交易日的收盘，数据不旧。"""
    if not calendar.load_cached():
        pytest.skip("本机没有交易日历缓存")
    assert _ok(latest, asof), f"{latest} → {asof} 被误判为过期"


@pytest.mark.parametrize(("latest", "asof"), HOLIDAY_BOUNDARIES)
def test_holiday_gap_would_fail_under_business_day_counting(latest, asof):
    """反过来锁住：这些日期在**工作日口径**下确实会失败。

    没有这条，上面那组测试可能只是因为日期选得太近而恒过 —— 那就锁不住任何东西。
    """
    import pandas as pd

    business = len(pd.bdate_range(pd.Timestamp(latest), pd.Timestamp(asof))) - 1
    assert business > 1, f"{latest} → {asof} 工作日只差 {business} 天，选不出对比度"


def test_normal_overnight_is_one_trading_day():
    """07:30 盘前跑的常态：最新数据是上一交易日，stale 恰好等于 1。"""
    if not calendar.load_cached():
        pytest.skip("本机没有交易日历缓存")
    assert _ok("2026-08-21", "2026-08-24")     # 周五 → 周一
    assert _ok("2026-08-18", "2026-08-19")     # 周二 → 周三


def test_genuinely_stale_is_still_blocked():
    """该拦的还得拦：真落后两个及以上交易日就是数据没更新。"""
    if not calendar.load_cached():
        pytest.skip("本机没有交易日历缓存")
    assert not _ok("2026-08-19", "2026-08-24")   # 落后 3 个交易日
    assert not _ok("2026-08-18", "2026-08-24")   # 落后 4 个交易日


def test_same_day_is_fresh():
    """盘后跑（旧的 18:15 排程）：最新数据就是当天，stale=0。"""
    assert _ok("2026-08-24", "2026-08-24")


def test_future_data_is_not_stale():
    """latest 比 asof 还新（补跑历史日期）不该被算成过期。"""
    assert _ok("2026-08-24", "2026-08-21")


def test_missing_date_blocks():
    assert not gates.data_freshness_guard(None, "2026-08-24", LIMITS).passed


def test_falls_back_to_business_days_when_calendar_missing(monkeypatch):
    """日历缺失时退回工作日口径 —— 只会高估，对硬闸而言宁可多拦。"""
    monkeypatch.setattr(calendar, "load_cached", lambda *a, **k: [])
    result = gates.data_freshness_guard("2026-02-13", "2026-02-24", LIMITS)
    assert not result.passed
    assert "工作日" in result.reason, "退回工作日口径时必须在 reason 里说明"


def test_reason_states_which_unit_was_used():
    """口径要写进 reason —— 事后排查时得知道走的是哪条路。"""
    if not calendar.load_cached():
        pytest.skip("本机没有交易日历缓存")
    assert "交易日" in gates.data_freshness_guard("2026-08-21", "2026-08-24", LIMITS).reason
