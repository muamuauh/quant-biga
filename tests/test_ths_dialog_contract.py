"""锁住 `enum_dialogs` 的返回形状。

写这组测试是因为踩了一次：`_enum_dialogs` 当时返回的是**文本字符串列表**，
而我加的公共包装 `enum_dialogs()` 却按 `(id, class, text, visible)` 四元组
声明契约，`ths_order_form` 信了那份契约，联调时炸在
`ValueError: too many values to unpack (expected 4)`。

`find_dialog_button` 的测试没能拦住它 —— 那些测试拿的是**手工构造**的四元组，
从没验证过真实数据源产出的是同一个形状。所以这里专门测**消费方能不能吃下
生产方给的东西**，而不是各自单独测。
"""

from __future__ import annotations

import pytest

from qbg.execution import ths_order_form as form
from qbg.portfolio import ths_client

# 一个验证码框的真实结构（2026-08-21 实测）。
# 注意 2405 是验证码图片，**文本为空** —— 早先只收「有文本的子控件」，
# 它会被整个丢掉，于是 _is_captcha 认不出验证码框。
CAPTCHA_KIDS = [
    (1, "Button", "确定", True),
    (2, "Button", "取消", True),
    (2393, "Static", "检测到您正在拷贝数据，为保护您的账号数据安全，请", True),
    (2394, "Static", "先输入验证码：", True),
    (2405, "Static", "", True),          # 验证码图片，无文本
    (2404, "Edit", "", True),            # 输入框，无文本
    (2406, "Static", "验证码错误!!", False),
]

SUBMIT_TIP_KIDS = [
    (2, "Button", "确定", True),
    (1, "Button", "立即重启", False),
    (1365, "Static", "提示", True),
    (1806, "Static", "非交易用户只提供查询功能", False),
]


@pytest.fixture
def fake_dialogs(monkeypatch):
    def _install(rows):
        monkeypatch.setattr(ths_client, "_xiadan_pids", lambda: {123})
        monkeypatch.setattr(ths_client, "_enum_dialogs", lambda _pids: rows)
    return _install


def test_enum_dialogs_rows_unpack_as_four_tuples(fake_dialogs):
    """消费方按 4 元组解包 —— 生产方必须给 4 元组。"""
    fake_dialogs([(999, CAPTCHA_KIDS)])
    for _hwnd, kids in ths_client.enum_dialogs():
        for cid, cls, text, visible in kids:      # 解包不炸就是通过
            assert isinstance(cid, int)
            assert isinstance(cls, str)
            assert isinstance(text, str)
            assert isinstance(visible, bool)


def test_visible_text_consumes_enum_dialogs_output(fake_dialogs):
    """就是这条路径在联调时炸的：handle_dialogs → _visible_text(kids)。"""
    fake_dialogs([(999, SUBMIT_TIP_KIDS)])
    _hwnd, kids = ths_client.enum_dialogs()[0]
    text = form._visible_text(kids)
    assert "提示" in text
    # 隐藏控件里那句「非交易用户只提供查询功能」会命中致命词表，
    # 绝不能被当成当前消息读出来。
    assert "非交易" not in text


def test_is_captcha_needs_textless_controls(fake_dialogs):
    """2405/2404 都没有文本；只收有文本的子控件就认不出验证码框。"""
    fake_dialogs([(999, CAPTCHA_KIDS)])
    _hwnd, kids = ths_client.enum_dialogs()[0]
    assert form._is_captcha(kids)
    assert not form._is_captcha(SUBMIT_TIP_KIDS)


def test_cleanup_dialogs_consumes_same_shape(fake_dialogs, monkeypatch):
    """cleanup_dialogs 也吃同一份数据，别只修一个消费方。"""
    posted = []
    monkeypatch.setattr(ths_client, "_xiadan_pids", lambda: {123})
    monkeypatch.setattr(ths_client, "_enum_dialogs", lambda _pids: [(999, CAPTCHA_KIDS)])

    class _FakeUser32:
        @staticmethod
        def PostMessageW(hwnd, msg, _w, _l):
            posted.append((hwnd, msg))

    monkeypatch.setattr(ths_client.ctypes, "windll",
                        type("W", (), {"user32": _FakeUser32})())
    closed = ths_client.cleanup_dialogs()
    assert posted == [(999, 0x0010)]
    assert closed and "验证码框" in closed[0]


def test_enum_dialogs_empty_when_client_not_running(monkeypatch):
    monkeypatch.setattr(ths_client, "_xiadan_pids", lambda: set())
    assert ths_client.enum_dialogs() == []
