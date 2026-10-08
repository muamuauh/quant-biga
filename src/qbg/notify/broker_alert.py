"""同花顺下单端不可用时，**在 07:45 预检那一刻**就告诉操作者。

2026-10-08 国庆后第一个交易日：预检 07:45 拉起了同花顺，但客户端进了**精简模式**
（主窗口被程序整个隐藏，只剩屏幕角落一个下单小条），easytrader 找不到可操作的窗口。
预检只往日志里记了一条 `[WARN]`，以退出码 1 结束 —— 没有任何人看到。于是：

- 08:00 盘前读不到持仓，不知道现金占比，保守地照跑了复核（$1.58）
- 09:30 日流程落到 10 万假资金 → 假的 100% 现金触发调仓 → 券商层拒单
- **真实持仓的止损检查当天没跑**
- 操作者 09:33 收到日报里的「持仓读不到」才知道出了事

07:45 → 09:30 这一个多小时本来就是留给**人工处理同花顺**的（登录脚本做不到）。
一条只进日志的告警，等于把这段时间白白浪费掉。

**一天只发一封。** 预检一天跑两遍（07:45 的任务 + `run_daily.ps1` 里自己再跑一遍），
操作者还可能手动重跑。去重标记写在 `logs/`，只有真的发出去了才写 ——
SMTP 那次失败的话，下一次预检会再试。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from qbg.config import settings
from qbg.notify.mailer import notify_owner

# 精简模式、最小化这种"进程在、窗口不对"的情况，标题要一眼说清是哪一种 ——
# 修法完全不同：一个是右键小条，一个是登录。
_HEADLINES = (
    ("精简模式", "精简模式（主窗口被隐藏）"),
    ("最小化", "交易窗口最小化了"),
    ("窗口不存在", "没登录交易"),
    ("未运行", "下单程序没启动"),
    ("写不了", "以管理员身份在跑"),
)


def build_alert(checks: list[tuple[str, bool, str]], hints: dict[str, str],
                advisory: set[str], day: str, mode: str) -> tuple[str, str] | None:
    """阻塞项 → (主题, 正文)。没有阻塞项返回 None。提醒项（位数不一致）不算。"""
    blocking = [(name, note) for name, ok, note in checks if not ok and name not in advisory]
    if not blocking:
        return None

    notes = " ".join(note for _, note in blocking)
    headline = next((label for key, label in _HEADLINES if key in notes), blocking[0][0])
    subject = f"⚠ 同花顺不可用 {day} — {headline}"

    trading = str(mode).upper() != "ADVISORY"
    lines = [
        "同花顺下单端现在不可用，**09:30 的日流程会读不到持仓**。",
        "",
        "不修的后果：",
        "- 持仓降级到 CSV / 默认值，**止损和移动止盈不检查**",
    ]
    if trading:
        lines.append(f"- {mode.upper()} 模式下**拒绝下单**（daily_cycle.broker_refusal）")
    lines += [
        "- 日报会显示「持仓读不到·结果不可用」",
        "",
        "## 哪里不对",
        "",
    ]
    lines += [f"- **{name}**：{note}" for name, note in blocking]
    lines += ["", "## 怎么修", ""]
    for name, _ in blocking:
        hint = hints.get(name)
        if hint:
            # HINTS 是给终端排版的（续行缩进 5 格），邮件里收成一段
            lines.append(f"- **{name}**：" + " ".join(part.strip() for part in hint.splitlines()))
    lines += [
        "",
        "## 修好之后",
        "",
        "- **09:30 之前**：什么都不用做，日流程会自己读。",
        "- **09:30 之后**（上午盘到 11:30）：",
        "  1. 确认能读了：`python tools/probe_ths.py --preflight-only`",
        "  2. 重跑日流程：`powershell -ExecutionPolicy Bypass -File run_daily.ps1`",
        "     持仓读不到的那次运行**不会写当日完成标记**，所以不用加 `--force`。",
        "",
        "在任务计划程序里右键「运行」预检任务是没用的：过了 09:30 的窗口它会被跳过，"
        "而且照样返回 0x0。",
    ]
    return subject, "\n".join(lines)


def flag_path(day: str, root: Path | None = None) -> Path:
    return (root or settings.log_dir) / f"broker_alert_{day}.sent"


def send_once(checks: list[tuple[str, bool, str]], hints: dict[str, str], advisory: set[str],
              *, day: str | None = None, root: Path | None = None) -> dict:
    """有阻塞项就发一封告警；同一天只发一次。永不抛异常 —— 告警不能反过来弄坏预检。"""
    day = day or date.today().isoformat()
    built = build_alert(checks, hints, advisory, day, settings.qbg_mode)
    if built is None:
        return {"sent": False, "skipped": "没有阻塞项"}
    flag = flag_path(day, root)
    if flag.exists():
        return {"sent": False, "skipped": "今天已经告警过"}
    try:
        result = notify_owner(*built)
    except Exception as exc:  # noqa: BLE001
        return {"sent": False, "error": f"{type(exc).__name__}: {exc}"}
    if result.get("sent"):
        try:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text(built[0], encoding="utf-8")
        except OSError:
            pass  # 去重标记写不了，最坏是明天之前多收一封
    return result
