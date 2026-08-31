"""入站邮件命令的鉴权与解析。

**全部离线**：不连 IMAP、不发信。`poll_once` 那层的网络部分不在这里测，
这里测的是它依赖的纯函数 —— 而安全性恰好全在那些纯函数里。

这条通道能触发这台机器上的交易流程，所以每一条**拒绝**路径都要有测试。
放行路径只有一条，拒绝路径有五条。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from qbg.notify import tokens
from qbg.notify.inbox import (
    RERUN,
    SHUTDOWN,
    STATUS,
    command_text,
    detect_command,
    parse_command,
)

ALLOW = {"owner@163.com"}
TOKEN = "a1b2c3d4"
SUBJECT = f"Re: [量化-PAPER] 每日报告 2026-08-29 {tokens.SUBJECT_TAG}{TOKEN}"


def _parse(body, *, frm="owner@163.com", subject=SUBJECT, token=TOKEN):
    return parse_command(frm, subject, body, ALLOW, token)


# ---------------------------------------------------------------------------
# 放行：唯一的一条
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("body,expected", [
    ("重跑", RERUN),
    ("重新运行", RERUN),
    ("rerun", RERUN),
    ("关机", SHUTDOWN),
    ("shutdown", SHUTDOWN),
    ("状态", STATUS),
    ("STATUS", STATUS),
])
def test_valid_command_is_accepted(body, expected):
    pc = _parse(body)
    assert (pc.status, pc.command) == ("ok", expected)


def test_sender_display_name_is_stripped():
    """`张三 <owner@163.com>` 这种格式要能对上白名单。"""
    assert _parse("重跑", frm="张三 <owner@163.com>").status == "ok"


def test_allowlist_is_case_insensitive():
    assert _parse("重跑", frm="OWNER@163.COM").status == "ok"


# ---------------------------------------------------------------------------
# 拒绝路径
# ---------------------------------------------------------------------------
def test_non_allowlisted_sender_is_rejected():
    pc = _parse("关机", frm="attacker@evil.com")
    assert pc.status == "not_allowed"


def test_wrong_token_is_rejected():
    pc = _parse("关机", subject=f"Re: 报告 {tokens.SUBJECT_TAG}deadbeef")
    assert pc.status == "bad_token"


def test_missing_token_is_rejected():
    """主题被改掉、令牌丢了 —— 这正是最常见的误用，必须拒绝。"""
    assert _parse("关机", subject="Re: 报告").status == "bad_token"


def test_no_live_token_rejects_everything():
    """令牌已被消费/过期时，**任何**命令都不放行（fail-closed）。"""
    assert _parse("重跑", token=None).status == "bad_token"


def test_two_command_words_are_never_guessed():
    """两个词就拒绝。猜错的代价是关掉一台正在交易的机器。"""
    pc = _parse("重跑还是关机？")
    assert pc.status == "ambiguous"
    assert pc.command == "AMBIGUOUS"


def test_no_command_word_is_silent():
    """不是给我们的信 —— 状态是 none，不该产生任何告警噪音。"""
    pc = _parse("今天收益怎么样？")
    assert (pc.status, pc.command) == ("none", None)


def test_command_is_checked_before_identity():
    """先判有没有命令，再判身份。

    否则任何一封路过的普通邮件都会被记成「非白名单命令」，
    机主每天收一堆假警报，然后就不看了。
    """
    pc = _parse("随便聊两句", frm="stranger@example.com")
    assert pc.status == "none"


# ---------------------------------------------------------------------------
# 正文解析：这三条是实测踩出来的坑
# ---------------------------------------------------------------------------
def test_only_the_first_line_counts():
    """回复会把原报告整个引用进来，而报告页脚里列着**每一个**命令词。

    扫全文会同时看到好几个 → 判成"不明确" → 鉴权完美通过然后拒绝执行任何命令。
    """
    body = "重跑\n\n下面是原文，里面写着 关机 和 状态 两个词"
    assert detect_command(command_text(body)) == RERUN


def test_footer_sentinel_cuts_our_own_boilerplate():
    body = f"关机\n\n{tokens.FOOTER_SENTINEL}\n\n- `重跑`：...\n- `状态`：..."
    assert detect_command(command_text(body)) == SHUTDOWN


@pytest.mark.parametrize("quoted", [
    "> 关机",
    "在 2026-08-29，muamuauh 写道：\n关机",
    "---- 回复的原邮件 ----\n关机",
    "发件人: muamuauh@163.com\n关机",
    "On Fri, Aug 29, 2026 at 9:30 AM someone wrote:\n关机",
])
def test_quoted_text_is_not_intent(quoted):
    """回复里第一行本身就是引用原文时，不能读成命令。

    各家客户端引用格式差异极大（163 用 `---- 回复的原邮件 ----` 加表头行），
    所以引用标记只是预过滤，真正兜底的是"只读第一行"。
    """
    assert detect_command(command_text(quoted)) is None


def test_empty_body_yields_nothing():
    assert command_text("") == ""
    assert detect_command("") is None


# ---------------------------------------------------------------------------
# 令牌
# ---------------------------------------------------------------------------
def test_token_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(tokens.settings, "snapshot_dir", tmp_path, raising=False)
    tok = tokens.issue_token()
    assert tokens.current_token() == tok
    subject = tokens.tag_subject("[量化-PAPER] 每日报告 2026-08-29", tok)
    assert tokens.extract_token(f"Re: {subject}") == tok


def test_token_is_single_use(tmp_path, monkeypatch):
    """一次性 —— 否则一封旧邮件可以被反复转发来重复触发。"""
    monkeypatch.setattr(tokens.settings, "snapshot_dir", tmp_path, raising=False)
    tokens.issue_token()
    tokens.consume_token()
    assert tokens.current_token() is None


def test_expired_token_is_not_honoured(tmp_path, monkeypatch):
    """兜底过期：第二天手工起监听器时，昨天那封信的令牌不能还能用。"""
    monkeypatch.setattr(tokens.settings, "snapshot_dir", tmp_path, raising=False)
    stale = datetime.now(UTC) - timedelta(hours=tokens.TOKEN_TTL_HOURS + 1)
    (tmp_path / "command_token.json").write_text(
        json.dumps({"token": "aabbccdd", "issued": stale.isoformat()}), encoding="utf-8")
    assert tokens.current_token() is None


@pytest.mark.parametrize("payload", [
    "{}",                                    # 没有 token 字段
    '{"token": "aabbccdd"}',                 # 没有时间戳
    '{"token": "aabbccdd", "issued": "看不懂"}',
    "not json at all",
])
def test_unreadable_token_file_fails_closed(tmp_path, monkeypatch, payload):
    """读不懂**不是**放行的理由。"""
    monkeypatch.setattr(tokens.settings, "snapshot_dir", tmp_path, raising=False)
    (tmp_path / "command_token.json").write_text(payload, encoding="utf-8")
    assert tokens.current_token() is None


def test_extract_token_tolerates_client_punctuation():
    assert tokens.extract_token(f"Re: 报告 {tokens.SUBJECT_TAG}a1b2c3d4>") == "a1b2c3d4"
    assert tokens.extract_token("Re: 报告（没有令牌）") is None


# ---------------------------------------------------------------------------
# 2026-08-31 实测的三个故障，逐条钉住。
# ---------------------------------------------------------------------------
def test_listener_not_spawned_without_a_fresh_token(monkeypatch):
    """没发出日报就不该起监听器 —— 没发信 = 没发新令牌 = 它起来也只能空转。

    实测代价远不止空转：盘前那次安静跳过后照样起了 3 小时的监听器，
    而监听器是计划任务的子进程，任务因此一直算「正在运行」，
    09:30 真正那次被 IgnoreNew 拒绝，**当天一笔单都没下**。
    """
    from qbg.orchestrator import daily_cycle

    spawned = []
    monkeypatch.setattr(daily_cycle.settings, "email_commands_enabled", 1, raising=False)
    monkeypatch.setattr(daily_cycle.subprocess, "Popen",
                        lambda *a, **k: spawned.append(a))

    daily_cycle._start_email_listener({"sent": False, "skipped": "not_trading_session"})
    assert spawned == [], "静默跳过的运行不该起监听器"

    daily_cycle._start_email_listener(None)
    assert spawned == [], "拿不到通知结果时同样不起"

    daily_cycle._start_email_listener({"sent": True})
    assert len(spawned) == 1, "真的发了日报（也就发了新令牌）才起"


def test_listener_flags_match_quant_trading(monkeypatch):
    """标志位和 quant-trading 的 `_launch_listener` 保持一致。

    **绝不能加 `CREATE_BREAKAWAY_FROM_JOB`。** 计划任务的 job object 没有设
    `JOB_OBJECT_LIMIT_BREAKAWAY_OK`，带它的 CreateProcess 直接返回
    `ERROR_ACCESS_DENIED` —— 2026-08-31 实测 `PermissionError: [WinError 5]`，
    监听器一次都没起来。参考实现从来没请求过脱离，所以从来没遇到这个问题。
    """
    import subprocess as sp
    import sys as _sys

    from qbg.orchestrator import daily_cycle

    captured = {}
    monkeypatch.setattr(daily_cycle.settings, "email_commands_enabled", 1, raising=False)
    monkeypatch.setattr(daily_cycle.subprocess, "Popen",
                        lambda *a, **k: captured.update(k))
    daily_cycle._start_email_listener({"sent": True})
    flags = captured.get("creationflags", 0)
    if _sys.platform == "win32":
        assert flags & sp.DETACHED_PROCESS
        assert flags & sp.CREATE_NEW_PROCESS_GROUP
        assert not (flags & sp.CREATE_BREAKAWAY_FROM_JOB), (
            "别再加 breakaway —— job object 不允许时会 WinError 5，监听器起不来")


def test_poll_error_is_reported_to_the_caller(monkeypatch):
    """轮询失败要留下痕迹，好让监听器把「连续失败」升级成告警。

    没有这个的话，同一条错误会每 60 秒刷一次刷三小时，
    而没有任何人知道命令通道其实已经死了。
    """
    from qbg.notify import inbox

    monkeypatch.setattr(inbox.settings, "email_commands_enabled", 1, raising=False)
    monkeypatch.setattr(inbox.settings, "smtp_user", "u@163.com", raising=False)
    monkeypatch.setattr(inbox.settings, "smtp_password", "pw", raising=False)
    monkeypatch.setattr(inbox.settings, "imap_host", "imap.163.com", raising=False)

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", _boom)
    assert inbox.poll_once() == []
    assert "connection refused" in (inbox.last_poll_error["detail"] or "")


def test_successful_poll_clears_the_error(monkeypatch):
    """一次成功要把连续失败计数清零 —— 否则偶发抖动会累积成误报。"""
    from qbg.notify import inbox

    inbox.last_poll_error["detail"] = "旧的错误"
    monkeypatch.setattr(inbox.settings, "email_commands_enabled", 0, raising=False)
    inbox.poll_once()          # 通道关着：直接返回，不该动错误状态
    assert inbox.last_poll_error["detail"] == "旧的错误"


def test_imap_id_is_registered_for_163():
    """163/126 登录后必须先发 IMAP ID，否则 SELECT 被拒、
    后续每条命令都报 "illegal in state AUTH"。"""
    import imaplib

    from qbg.notify.inbox import _send_imap_id

    class _Conn:
        def __init__(self):
            self.sent = []

        def _simple_command(self, name, arg):
            self.sent.append((name, arg))
            return "OK", []

    conn = _Conn()
    _send_imap_id(conn)
    assert conn.sent and conn.sent[0][0] == "ID"
    assert "ID" in imaplib.Commands


def test_imap_id_failure_does_not_break_other_providers():
    """别家服务器不需要 ID，发不出去不该让连接失败。"""
    from qbg.notify.inbox import _send_imap_id

    class _Conn:
        def _simple_command(self, *a):
            raise RuntimeError("ID not supported")

    _send_imap_id(_Conn())      # 不抛异常即通过


def test_no_orders_is_not_a_failure():
    """一笔都没打算下 ≠ 下单失败。

    2026-08-31 实测：risk-off + 空仓，本来就无事可做，主题却写
    「⚠ 下单失败 0/0笔」。假警报和漏报一样有害 —— 它会训练人忽略这个前缀，
    而真出事时也是同一个前缀。
    """
    from qbg.notify.digest import status_tag
    from qbg.report.daily_report import _status, render

    run = {"submitted": True, "allowed_orders": [],
           "broker": {"ok": True, "submitted": 0, "message": "没有可提交的订单",
                      "outcomes": []}}
    assert "失败" not in status_tag(run, [], None)
    assert "失败" not in _status({**run, "hard_ok": True})
    text = render({**run, "date": "2026-08-31", "hard_ok": True})
    assert "下单全部未成功" not in text
