"""计划任务「过期不补跑」窗口闸的接线测试。

## 为什么是源码断言而不是行为测试

闸本身是 PowerShell（`scripts/window_guard.ps1`），本仓库的测试是 pytest 且
必须离线、不依赖 Windows 特性，所以这里不去跑 powershell.exe。**这些测试要挡的
不是逻辑错，是接线错** —— 本项目已经被"参数存在但没人读"坑过三次：

  · `require_trading_session` 写进了 yaml，`session_open` 却从没传给 `run_all_gates`
  · `QBG_KEEP_RANK` 在 config 里，回测引擎却表达不了它
  · `QBG_REBALANCE_EVERY_DAYS` 同上

窗口闸有一模一样的形状：`setup_schedule.ps1` 把 `-ScheduledAt` 写进任务动作，
两个被调度的脚本必须**都**声明并读取它。少接一处，闸就静默失效 —— 而症状
（晚上开机它自己跑起来）要等到下一次早上没开机才看得见。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "window_guard.ps1"
# 会被计划任务直接叫起来、因而必须自己判断窗口的脚本。
SCHEDULED = [ROOT / "run_daily.ps1", ROOT / "scripts" / "preflight.ps1"]
SETUP = ROOT / "scripts" / "setup_schedule.ps1"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def test_guard_script_exists():
    """两个被调度的脚本都 dot-source 它；文件没了闸就整个消失。"""
    assert GUARD.exists(), f"{GUARD} 不存在 —— 过期不补跑的闸没有实现"


@pytest.mark.parametrize("path", SCHEDULED, ids=lambda p: p.name)
def test_scheduled_scripts_declare_the_window_parameters(path):
    """光有 window_guard.ps1 没用，脚本得先能接住这两个参数。"""
    src = read(path)
    assert "$ScheduledAt" in src, f"{path.name} 没有声明 -ScheduledAt"
    assert "$WindowMinutes" in src, f"{path.name} 没有声明 -WindowMinutes"


@pytest.mark.parametrize("path", SCHEDULED, ids=lambda p: p.name)
def test_scheduled_scripts_actually_call_the_guard(path):
    """声明了还得真的调用 —— 这正是"参数存在但没人读"那三次的犯错形状。"""
    src = read(path)
    assert "window_guard.ps1" in src, f"{path.name} 没有 dot-source window_guard.ps1"
    assert "Get-QbgWindowSkipReason" in src, f"{path.name} 声明了参数却从不调用闸函数"


@pytest.mark.parametrize("path", SCHEDULED, ids=lambda p: p.name)
def test_scheduled_scripts_offer_an_override(path):
    """必须留一个明知过期也要跑的出口，否则补跑昨天的就只能改代码。"""
    assert "$IgnoreWindow" in read(path), f"{path.name} 没有 -IgnoreWindow 出口"


def test_setup_writes_the_window_into_the_task_action():
    """任务要自描述：从任务计划程序看动作那一行就知道窗口是多少。"""
    src = read(SETUP)
    assert "-ScheduledAt" in src and "-WindowMinutes" in src, \
        "setup_schedule.ps1 没有把窗口参数写进任务动作 —— 计划任务不会带上它们，闸永远放行"


def test_setup_keeps_start_when_available():
    """闸和 StartWhenAvailable 是**配套**的，不是二选一。

    只留调度器的补跑 → 晚上开机乱跑（这次要修的问题）；
    只留脚本的闸、去掉 StartWhenAvailable → 09:35 才开机那天根本不会被唤起，
    等于把功能修没了。所以这一条防的是"顺手把 StartWhenAvailable 删掉"。
    """
    assert "-StartWhenAvailable" in read(SETUP), \
        "去掉 StartWhenAvailable 会让迟到但仍有效的启动完全不被唤起"


def _first_call_offset(src: str, name: str) -> int | None:
    """`name` 第一次被**调用**的位置。跳过 `function name` 那一处定义。

    直接找字符串会撞上函数定义 —— 定义当然排在调用之前，那样断言永远失败。
    """
    start = 0
    while (i := src.find(name, start)) != -1:
        if not src[:i].rstrip().endswith("function"):
            return i
        start = i + len(name)
    return None


def test_preflight_guard_runs_before_any_side_effect():
    """预检的闸必须在开代理/开 TUN/拉同花顺之前。

    顺序错了闸就白设：晚上开机时那三件事已经做完了，再退出也收不回来 ——
    而那正是这次要修的症状本身（晚上开机，代理和同花顺被自己打开了）。
    """
    src = read(ROOT / "scripts" / "preflight.ps1")
    guard_at = _first_call_offset(src, "Get-QbgWindowSkipReason")
    assert guard_at is not None, "预检里找不到窗口闸的调用"
    for marker in ("Enable-ByHotkey", "Enable-ProxyByRegistry", "Start-Process"):
        call_at = _first_call_offset(src, marker)
        if call_at is not None:
            assert guard_at < call_at, \
                f"预检的窗口闸出现在 {marker} 的调用之后 —— 副作用已经发生了才判断"
