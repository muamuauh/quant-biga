"""同花顺不可用时的告警邮件。全离线。

2026-10-08 预检 07:45 拉起了同花顺，但客户端进了精简模式，主窗口被隐藏。预检只往
日志里记了一条 [WARN]，直到 09:33 日报才有人知道 —— 而 07:45 → 09:30 这段时间本来
就是留给人工处理同花顺的。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from qbg.config import settings
from qbg.notify import broker_alert

HINTS = {"网上股票交易系统5.0 窗口": "· 「精简模式」 —— 在小条上右键\n     切回完整窗口",
         "xiadan.exe 已启动": "先手动打开并**登录**"}
ADVISORY = {"Python 与客户端位数一致"}

COMPACT = [  # 2026-10-08 那天的真实检查结果
    ("xiadan.exe 已启动", True, "pid=[21896]"),
    ("网上股票交易系统5.0 窗口", False, "hwnd=67666 被程序隐藏 —— **精简模式**，主窗口整个藏起来了"),
    ("Python 与客户端位数一致", False, "python=64bit / xiadan=32-bit"),
]
NOT_RUNNING = [
    ("xiadan.exe 已启动", False, "未运行，请先手动打开并登录"),
    ("网上股票交易系统5.0 窗口", False, "窗口不存在，多半是没登录交易"),
]
HEALTHY = [("xiadan.exe 已启动", True, "pid=[9044]"),
           ("Python 与客户端位数一致", False, "python=64bit / xiadan=32-bit")]


def test_compact_mode_is_named_in_the_subject():
    """标题要一眼说清是哪种：精简模式是右键小条，没登录是去登录 —— 修法完全不同。"""
    subject, body = broker_alert.build_alert(COMPACT, HINTS, ADVISORY, "2026-10-08", "PAPER")
    assert "精简模式" in subject and "2026-10-08" in subject
    assert "右键" in body, "得带上怎么修"
    assert "拒绝下单" in body


def test_not_running_says_so():
    subject, _ = broker_alert.build_alert(NOT_RUNNING, HINTS, ADVISORY, "2026-10-08", "PAPER")
    assert "没登录" in subject or "没启动" in subject


def test_advisory_items_alone_do_not_alert():
    """位数不一致是已知无害的提醒。为它天天发邮件就是狼来了。"""
    assert broker_alert.build_alert(HEALTHY, HINTS, ADVISORY, "2026-10-08", "PAPER") is None


def test_advisory_mode_does_not_threaten_refused_orders():
    """顾问模式本来就不下单，别写"拒绝下单"吓人。"""
    _, body = broker_alert.build_alert(COMPACT, HINTS, ADVISORY, "2026-10-08", "ADVISORY")
    assert "拒绝下单" not in body


def test_hints_are_flattened_for_email():
    """HINTS 是给终端排版的（续行缩进），邮件里收成一行。"""
    _, body = broker_alert.build_alert(COMPACT, HINTS, ADVISORY, "2026-10-08", "PAPER")
    assert "     切回" not in body and "右键 切回完整窗口" in body


# ----------------------------------------------------------------------
# 一天一封
# ----------------------------------------------------------------------

@pytest.fixture
def mailbox(monkeypatch):
    sent = []

    def fake(subject, body):
        sent.append(subject)
        return {"sent": True}
    monkeypatch.setattr(broker_alert, "notify_owner", fake)
    monkeypatch.setattr(settings, "qbg_mode", "PAPER")
    return sent


def test_only_one_alert_per_day(mailbox, tmp_path: Path):
    """预检一天跑两遍（07:45 的任务 + run_daily.ps1 里再跑一遍），还可能手动重跑。"""
    for _ in range(3):
        broker_alert.send_once(COMPACT, HINTS, ADVISORY, day="2026-10-08", root=tmp_path)
    assert len(mailbox) == 1


def test_a_new_day_alerts_again(mailbox, tmp_path: Path):
    broker_alert.send_once(COMPACT, HINTS, ADVISORY, day="2026-10-08", root=tmp_path)
    broker_alert.send_once(COMPACT, HINTS, ADVISORY, day="2026-10-09", root=tmp_path)
    assert len(mailbox) == 2


def test_a_failed_send_is_retried_by_the_next_preflight(monkeypatch, tmp_path: Path):
    """SMTP 那次失败了就别写去重标记 —— 09:30 那遍预检还能再试。"""
    results = iter([{"sent": False, "error": "timeout"}, {"sent": True}])
    calls = []
    monkeypatch.setattr(broker_alert, "notify_owner",
                        lambda s, b: calls.append(s) or next(results))
    broker_alert.send_once(COMPACT, HINTS, ADVISORY, day="2026-10-08", root=tmp_path)
    broker_alert.send_once(COMPACT, HINTS, ADVISORY, day="2026-10-08", root=tmp_path)
    assert len(calls) == 2


def test_a_healthy_client_sends_nothing(mailbox, tmp_path: Path):
    result = broker_alert.send_once(HEALTHY, HINTS, ADVISORY, day="2026-10-08", root=tmp_path)
    assert mailbox == [] and result["skipped"] == "没有阻塞项"


def test_the_alert_never_raises(monkeypatch, tmp_path: Path):
    """告警是旁路，不能反过来把预检弄坏。"""
    def boom(*_):
        raise RuntimeError("smtp exploded")
    monkeypatch.setattr(broker_alert, "notify_owner", boom)
    result = broker_alert.send_once(COMPACT, HINTS, ADVISORY, day="2026-10-08", root=tmp_path)
    assert result["sent"] is False and "smtp exploded" in result["error"]


# ----------------------------------------------------------------------
# 接线
# ----------------------------------------------------------------------

def test_preflight_asks_the_probe_to_alert():
    text = Path("scripts/preflight.ps1").read_text(encoding="utf-8-sig")
    assert "probe_ths.py\") --preflight-only --alert" in text


def test_a_manual_probe_does_not_email():
    """手工跑探针不该发邮件 —— 只有带 --alert 才发，而只有预检带它。"""
    text = Path("tools/probe_ths.py").read_text(encoding="utf-8")
    assert "if args.alert and not ok:" in text
