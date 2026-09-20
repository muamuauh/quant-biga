"""脚本的输出不许因为一个字符就把整次运行的结果吞掉。

2026-09-20 实测：`12_rebalance_gate.py > out.txt` 跑完 90 秒回测、打完指标表，
崩在打印子区间那一行的 `−`（U+2212）上 —— **八项闸的结论一个字都没写出来**。
本机代码页是 GBK，而重定向到文件时 Python 用的也是这个 locale 编码。

报告里 GBK 编不出的字符有 6 种：`⚠ − ✅ ❌ ¥ ✗`。**不能把它们换成 ASCII** ——
`✅/❌` 正是这些报告一眼能读的原因。所以让编不出的字符降级成 `?`。
"""

from __future__ import annotations

import io
import pathlib
import sys

import pytest

from qbg.utils.console import make_output_safe

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
# 真实在用的那几个。GBK 编得出中文，所以只有这类符号会出事。
UNENCODABLE = "⚠−✅❌¥✗"


class _GbkStream(io.TextIOWrapper):
    """假装是个 GBK 控制台。`isatty` 决定守卫走哪条路。"""

    def __init__(self, tty: bool):
        super().__init__(io.BytesIO(), encoding="gbk", newline="")
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _gbk_stream(tty: bool = True):
    return _GbkStream(tty)


def _prints_unencodable(text: str) -> bool:
    for line in text.splitlines():
        if "print(" not in line:
            continue
        for ch in line:
            try:
                ch.encode("gbk")
            except UnicodeEncodeError:
                return True
    return False


# ----------------------------------------------------------------------
# 守卫本身
# ----------------------------------------------------------------------

def test_a_gbk_stream_really_does_explode():
    """先确认这个坑是真的 —— 否则下面那条测的是空气。"""
    stream = _gbk_stream()
    with pytest.raises(UnicodeEncodeError):
        stream.write("子区间超额（候选 − 基线）")
        stream.flush()


def test_make_output_safe_degrades_instead_of_raising(monkeypatch):
    stream = _gbk_stream()
    monkeypatch.setattr(sys, "stdout", stream)
    make_output_safe()

    print("子区间超额（候选 − 基线）", file=sys.stdout)
    sys.stdout.flush()

    written = stream.buffer.getvalue().decode("gbk")
    assert "子区间超额" in written, "中文不该受影响 —— GBK 编得出"
    assert "−" not in written and "?" in written, "编不出的字符应降级成 ?"


def test_a_real_console_keeps_gbk(monkeypatch):
    """控制台上只改 errors，**不改 encoding** —— 改成 UTF-8 会让中文整片乱码。"""
    stream = _gbk_stream(tty=True)
    monkeypatch.setattr(sys, "stdout", stream)
    make_output_safe()
    assert sys.stdout.encoding.lower() in ("gbk", "cp936")


def test_redirected_output_switches_to_utf8(monkeypatch):
    """**重定向时不该牺牲 ✅/❌。** 只放宽 errors 的话八项闸那张表每行都是 `?`，
    看不出哪项过哪项没过 —— 崩溃换成了失明。管道里没有代码页约束，用 UTF-8。"""
    stream = _gbk_stream(tty=False)
    monkeypatch.setattr(sys, "stdout", stream)
    make_output_safe()

    print("  ✅ net_return   ✗ sharpe   候选 − 基线", file=sys.stdout)
    sys.stdout.flush()

    written = stream.buffer.getvalue().decode("utf-8")
    assert "✅" in written and "✗" in written and "−" in written, (
        "重定向到文件时这些符号应当原样保留")


def test_it_survives_a_stream_without_reconfigure(monkeypatch):
    """pytest 捕获、管道包装都可能换掉 stdout。便利设施不该变成新的崩溃点。"""
    class Bare:
        def write(self, _):
            return 0

    monkeypatch.setattr(sys, "stdout", Bare())
    make_output_safe()  # 不抛就算过


# ----------------------------------------------------------------------
# 谁打了那些字符，谁就得调它
# ----------------------------------------------------------------------

def test_every_script_that_prints_those_chars_calls_the_guard():
    """**这条才是防复发的那一条。** 新脚本里写个 `✅` 就会被这里抓住。"""
    missing = []
    for path in sorted(SCRIPTS.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _prints_unencodable(text) and "make_output_safe()" not in text:
            missing.append(path.name)
    assert not missing, (
        f"这些脚本会打 GBK 编不出的字符（{UNENCODABLE}）却没调 make_output_safe()："
        f"{missing}。重定向输出时它们会在打到那个字符的瞬间崩掉，"
        "而前面算了多久就白算多久。")


def test_the_guard_is_called_before_anything_prints():
    """装在 main() 第一行。装在末尾等于没装。"""
    for path in sorted(SCRIPTS.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "make_output_safe()" not in text:
            continue
        body = text[text.index("def main("):]
        guard = body.index("make_output_safe()")
        first_print = body.find("print(")
        if first_print >= 0:
            assert guard < first_print, (
                f"{path.name}：make_output_safe() 排在第一个 print 后面了")
