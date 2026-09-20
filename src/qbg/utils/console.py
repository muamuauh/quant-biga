"""让脚本的标准输出在 GBK 控制台上不会掀桌子。

本机（以及 Windows 计划任务）的代码页是 GBK/cp936，而**重定向到文件时
Python 用的也是这个 locale 编码**。脚本报告里有一批 GBK 编不出的字符 ——
`⚠ − ✅ ❌ ¥ ✗`，12 个脚本里共 25 处 —— 撞上就直接抛 `UnicodeEncodeError`。

2026-09-20 实测有多难看：`12_rebalance_gate.py > out.txt` 跑完 90 秒回测、
打完指标表，崩在打印子区间那一行的 `−` 上，**八项闸的结论一个字都没写出来**。
表现是「脚本失败了」，而实际上计算全做完了，只差往外写。

**修法不是把那些字符换成 ASCII** —— `✅/❌` 正是这些报告一眼能读的原因。
让编不出的字符降级成 `?`，别让它掀掉整次运行的结果。

调用点在每个会打这些字符的脚本顶部。`tests/test_console_safety.py`
钉住「谁打了 GBK 编不出的字符，谁就得调这个」。
"""

from __future__ import annotations

import sys


def make_output_safe() -> None:
    """让编码问题不再吞掉运行结果。按"输出到哪里"分两种处理：

    **重定向到文件/管道时改用 UTF-8。** 那时根本没有控制台代码页这个约束，
    而单纯把 `errors` 设成 `replace` 会把 `✅/❌` 一起打成 `?` ——
    八项闸那张表每行都变成 `?`，看不出哪项过哪项没过。等于崩溃换成了失明。

    **真的是控制台时只放宽 `errors`，不动 `encoding`。** 中文 GBK 编得出，
    强行改成 UTF-8 反而让整片中文变乱码；这时只要保证那几个符号降级成 `?`
    而不是抛异常就够了。

    stdout 被换掉过（pytest 捕获、管道包装）时可能没有 `reconfigure`，
    那种情况直接跳过：这是个便利设施，不该成为新的崩溃点。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError, AttributeError):  # 已关闭 / 不是真的文本流
            continue
