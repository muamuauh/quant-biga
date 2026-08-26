"""多收件人解析。

`smtplib.send_message` 从 To 头解析收件人，逗号分隔本来就支持。
但**分号分隔会静默丢掉除第一个以外的所有人**（实测），而那是很常见的写法。
「以为通知了两个人、其实只通知了一个」是不会有人发现的那种故障。
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import getaddresses

import pytest

from qbg.notify.mailer import normalize_recipients


def _recipients(header: str) -> list[str]:
    """走 smtplib 实际使用的那条解析路径。"""
    message = EmailMessage()
    message["To"] = header
    return [addr for _name, addr in getaddresses(message.get_all("To", []))]


@pytest.mark.parametrize("raw", [
    "a@x.com,b@y.com",
    "a@x.com, b@y.com",
    " a@x.com ,b@y.com ",
    "a@x.com,b@y.com,",
    "a@x.com;b@y.com",          # ← 不规范化的话这个只会发给 a
    "a@x.com; b@y.com",
    "a@x.com,,b@y.com",
])
def test_two_recipients_survive(raw):
    assert _recipients(normalize_recipients(raw)) == ["a@x.com", "b@y.com"]


def test_semicolon_would_silently_drop_without_normalization():
    """锁住动机：不规范化时分号确实只解析出一个地址，而且不报错。"""
    assert _recipients("a@x.com;b@y.com") == ["a@x.com"]


def test_single_recipient_unchanged():
    assert normalize_recipients("a@x.com") == "a@x.com"


def test_duplicates_removed():
    assert normalize_recipients("a@x.com, a@x.com, b@y.com") == "a@x.com, b@y.com"


def test_order_preserved():
    """去重不能打乱顺序 —— 第一个通常是主要收件人。"""
    assert normalize_recipients("b@y.com, a@x.com") == "b@y.com, a@x.com"


@pytest.mark.parametrize("raw", ["", "   ", ",", ";", " , ; "])
def test_empty_yields_empty(raw):
    """空值要落回 SMTP_USER（由 load_config 处理），这里只保证返回空串。"""
    assert normalize_recipients(raw) == ""


def test_three_recipients():
    assert normalize_recipients("a@x.com;b@y.com,c@z.com") == "a@x.com, b@y.com, c@z.com"


# ---------------------------------------------------------------------------
# 邮件主题必须反映**券商那边实际发生了什么**。
# 2026-08-25 实测：3 笔单一笔都没进券商，主题却是「已提交3笔」——
# `submitted` 指的是"顾问清单已落盘"，不是"券商收到了单"。人只看主题。
# ---------------------------------------------------------------------------
def _tag(**over):
    from qbg.notify.digest import status_tag
    return status_tag({"submitted": True, "allowed_orders": [{}, {}, {}], **over}, [], None)


def test_subject_says_failed_when_no_order_reached_broker():
    tag = _tag(broker={"ok": False, "outcomes": [{"ok": False}]})
    assert "下单失败" in tag and "0/3" in tag
    assert "已提交" not in tag


def test_subject_flags_partial_fill():
    assert "部分成交 2/3" in _tag(
        broker={"ok": False, "outcomes": [{"ok": True}, {"ok": True}, {"ok": False}]})


def test_subject_reports_broker_success():
    assert _tag(broker={"ok": True, "outcomes": [{"ok": True}] * 3}) == "券商已接单3笔"


def test_subject_leads_with_unreadable_portfolio():
    """持仓读不到是最严重的 —— 盖过其他一切状态。"""
    tag = _tag(portfolio={"source": "default", "degraded": {"reason": "对不上账"}},
               broker={"ok": True, "outcomes": [{"ok": True}] * 3})
    assert "持仓读不到" in tag


def test_advisory_subject_no_longer_claims_submission():
    """顾问模式只是生成清单，说「已提交」会让人以为单子下出去了。"""
    tag = _tag()
    assert "已生成清单3笔" == tag


def test_subject_distinguishes_unverified_from_failed():
    """「确认不了」比「失败」严重：失败可以补单，确认不了不能碰。"""
    tag = _tag(broker={"ok": False, "outcomes": [
        {"ok": False, "verified": False}]})
    assert "待人工核对" in tag
    assert "下单失败" not in tag
