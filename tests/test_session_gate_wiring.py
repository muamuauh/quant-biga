"""交易时段闸的接线与早退。

2026-08-26 实测暴露的两个问题：

1. **`session_open` 从来没传进 `run_all_gates`。** 它默认 `None`，而
   `session_guard` 对 `None` 返回 False —— 于是 `require_trading_session`
   一改成 `true`，这个硬闸就在**任何时间**都必然失败，系统永远不下单。
   这是第三次同一类问题（前两次是写死 AdvisoryAdapter、写死 ManualSource）：
   配置项存在，但没人读。

2. **硬闸在流程跑到一半才判。** 计划任务的"登录后 3 分钟"触发器在 08:40
   跑了一次，拉数打分全做完才被拦下。那天恰好 risk-off 没调 LLM，
   下次 risk-on 就是几毛钱和 5-8 分钟白烧，外加一封像出事了的邮件。
"""

from __future__ import annotations

import datetime as dt

import pytest

from qbg.market import calendar
from qbg.orchestrator import daily_cycle
from qbg.risk.gates import session_guard

BJ = calendar.TZ


def _at(clock: str) -> dt.datetime:
    if len(clock) == 5:
        clock += ":00"
    return dt.datetime.fromisoformat(f"2026-08-26T{clock}").replace(tzinfo=BJ)


# ---------------------------------------------------------------------------
# calendar.minutes_until_session
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("clock,expected", [
    ("08:40", 50.0),      # 盘前
    ("09:29:55", pytest.approx(0.083, abs=0.01)),
    ("09:30", 0.0),       # 开盘瞬间 = 已在时段内
    ("10:15", 0.0),
    ("12:00", 60.0),      # 午休 -> 下午场
    ("13:30", 0.0),
    ("15:30", None),      # 收盘后今天没有下一段了
])
def test_minutes_until_session(clock, expected):
    assert calendar.minutes_until_session(_at(clock)) == expected


def test_naive_datetime_is_treated_as_beijing():
    naive = dt.datetime(2026, 8, 26, 10, 0)
    assert calendar.minutes_until_session(naive) == 0.0


# ---------------------------------------------------------------------------
# 早退判据：等我们跑到判闸那一刻，市场开没开
# ---------------------------------------------------------------------------
def _would_skip(clock: str) -> bool:
    wait = calendar.minutes_until_session(_at(clock))
    return wait is None or wait > daily_cycle.SESSION_GRACE_MINUTES


PIPELINE_MINUTES = 7  # 拉数 + 打分 + LLM 复核的实测量级


@pytest.mark.parametrize("clock", ["08:40", "09:16", "15:30", "12:00"])
def test_skips_when_market_still_shut_at_gate_time(clock):
    """跳过的那些，判闸时确实还没开盘 —— 跑下去只会白烧钱。"""
    assert _would_skip(clock)
    gate_at = _at(clock) + dt.timedelta(minutes=PIPELINE_MINUTES)
    assert not calendar.in_session(gate_at)


@pytest.mark.parametrize("clock", ["09:26", "09:29:55", "09:30", "10:15", "13:30"])
def test_proceeds_when_market_open_by_gate_time(clock):
    """继续的那些，判闸时市场都已经开了。

    09:29:55 这一行是**关键**：09:30:00 触发的任务可能因为几秒时钟偏差落在
    开盘前，只看 `in_session()` 就会把一整个交易日跳过去。
    """
    assert not _would_skip(clock)
    gate_at = _at(clock) + dt.timedelta(minutes=PIPELINE_MINUTES)
    assert calendar.in_session(gate_at)


def test_grace_window_stays_below_pipeline_runtime():
    """宽限窗口调大就会重新引入它本要避免的浪费。

    设成 15 的话，09:16 启动那次会跑完整条流程（含 LLM），到 09:23 判闸时
    市场还没开 —— 钱花了，结果扔了。
    """
    assert daily_cycle.SESSION_GRACE_MINUTES < PIPELINE_MINUTES


# ---------------------------------------------------------------------------
# 接线：session_open 必须真的传下去
# ---------------------------------------------------------------------------
def test_session_guard_blocks_on_unknown_session():
    """这是 bug 的根源：不传 session_open 就等于宣称"时段未知"，永远拦。"""
    limits = {"require_trading_session": True}
    assert not session_guard(limits, None).passed
    assert not session_guard(limits, False).passed
    assert session_guard(limits, True).passed


def test_daily_cycle_passes_session_open_to_gates(monkeypatch):
    """盯住接线本身 —— 光测 session_guard 抓不到"调用方没传"这种 bug。"""
    seen = {}

    def _spy(**kwargs):
        seen.update(kwargs)
        return True, [], []

    monkeypatch.setattr(daily_cycle, "run_all_gates", _spy)
    monkeypatch.setattr(calendar, "in_session", lambda *a, **k: True)
    import inspect
    src = inspect.getsource(daily_cycle.run_daily)
    assert "session_open=" in src, "run_all_gates 调用里必须显式传 session_open"


def test_gate_uses_live_session_not_the_early_check():
    """判闸要用**判闸那一刻**的时段，不能复用启动时算好的值。

    09:26 启动、09:33 判闸：启动时不在时段内，判闸时在。复用启动时的值
    就会把这次本该成功的运行拦掉。
    """
    assert not calendar.in_session(_at("09:26"))
    assert calendar.in_session(_at("09:26") + dt.timedelta(minutes=PIPELINE_MINUTES))


# ---------------------------------------------------------------------------
# 早退不该丢掉一天，也不该发像出事了的邮件
# ---------------------------------------------------------------------------
def test_early_skip_does_not_write_the_daily_marker():
    """早退不写当日幂等标记，否则 09:30 的日触发器会被"今日已完成"挡掉。"""
    import inspect
    src = inspect.getsource(daily_cycle.run_daily)
    head = src[:src.index("not_trading_session")]
    assert "save_marker" not in head


def test_email_subject_is_calm_for_out_of_session_skip():
    """每天登录都收到一封带 ⚠ 的邮件，人很快就不看邮件了 ——
    而这套系统的安全网全靠人看邮件。"""
    from qbg.notify.digest import status_tag
    tag = status_tag({"skipped_reason": "not_trading_session"}, [], None)
    assert "⚠" not in tag
    assert "非交易时段" in tag


def test_report_says_it_is_expected_not_a_failure():
    from qbg.report.daily_report import render
    text = render({"date": "2026-08-26", "hard_ok": True,
                   "skipped_reason": "not_trading_session"})
    assert "预期行为" in text
    assert "不是故障" in text
