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
