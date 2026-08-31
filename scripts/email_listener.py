"""入站邮件命令监听器（重跑 / 关机 / 状态）。

每 N 秒查一次收件箱，最多活 M 小时，对通过鉴权的命令执行动作
（鉴权见 `qbg.notify.inbox`）。日流程跑完后拉起，这样你人不在机器旁边也能
用一封回复重新触发流程或把机器关掉。

**单实例**：文件锁保证同时只有一个监听器在跑，重跑不会把监听器叠起来。
收到「关机」或活满时长后自行退出。

**这里没有任何一条路能碰三把锁。**「重跑」只是重新触发那条本来就带全部风控闸
的日流程 —— 该拦的照拦，`QBG_MODE` / `I_CONFIRM_REAL` / `allow_live_mode`
一个都不会被改。

用法：
    python scripts/email_listener.py                 # 真实模式
    python scripts/email_listener.py --dry-run       # 只鉴权+回执，不执行动作
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qbg.config import settings  # noqa: E402
from qbg.notify.inbox import (  # noqa: E402
    RERUN,
    SHUTDOWN,
    STATUS,
    last_poll_error,
    poll_once,
)
from qbg.utils.logging import get_logger, log_event  # noqa: E402

log = get_logger("qbg.email_listener")
_LOCK = ROOT / "data" / "snapshots" / "email_listener.lock"

# 关机前给在跑的流程留的时间。`shutdown /t` 的秒数，不是我们自己 sleep ——
# 这样 `shutdown /a` 还能在这段时间里取消掉，是一条真实的后悔药。
SHUTDOWN_DELAY_SEC = 60

# 连续轮询失败多少次之后放弃并告警。
#
# **不能无限重试。** 2026-08-31 实测：163 需要先发 IMAP ID 命令，我们没发，
# 于是 SELECT 一直失败，同一条错误每 60 秒刷一次、刷了三个小时 —— 而且
# 没有任何人知道命令通道其实是死的。会自愈的故障（网络抖动）几次之内就会好；
# 好不了的都是配置问题，重试一万次也一样。
MAX_CONSECUTIVE_FAILURES = 5


def _acquire_lock():
    """Windows 单实例锁。拿不到返回 None（说明已经有一个在跑）。"""
    try:
        import msvcrt

        _LOCK.parent.mkdir(parents=True, exist_ok=True)
        handle = open(_LOCK, "a+")  # noqa: SIM115 —— 锁要活到进程结束
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            handle.close()
            return None
        return handle
    except Exception as exc:  # noqa: BLE001 —— 非 Windows / 锁不可用：照跑
        log_event(log, "listener.lock.skipped", error=str(exc))
        return open(_LOCK, "a+")  # noqa: SIM115


def _ack(command: str, extra: str = "") -> None:
    """回一封确认信，并**发一个新令牌**，好让下一条命令还能用。

    没有这一步，测试模式下第一条命令消费掉令牌之后就没法再测第二条了。
    """
    from qbg.notify.mailer import send_with_token

    send_with_token(f"[量化] ✅ 已收到命令：{command}",
                    f"# 入站命令已鉴权\n\n"
                    f"监听器**收到并通过鉴权**（白名单 + 一次性令牌）识别出命令："
                    f"**{command}**。\n\n{extra}")


def _run_rerun() -> None:
    """重新触发日流程。

    **不加 `--force`。** force 会绕过交易日/时段/当日幂等这些检查，而那正是
    我们希望它遵守的东西 —— 一封邮件不该有比计划任务更大的权限。
    所以：当天已经成功跑完的话，重跑就是一次几秒的空转；
    上次是硬闸中止（没写幂等标记）的话，它才会真的重来一遍。
    """
    log_event(log, "listener.dispatch", command=RERUN)
    subprocess.Popen([sys.executable, "-m", "qbg.orchestrator.daily_cycle"],
                     cwd=str(ROOT))


def _run_status() -> None:
    """只读：回一封当前状态，不做任何动作。"""
    from qbg.market import calendar
    from qbg.notify.mailer import send_with_token
    from qbg.orchestrator.run_marker import already_completed_today

    today = __import__("datetime").date.today().isoformat()
    report = settings.report_dir / "daily" / f"{today}.md"
    lines = [
        f"# 运行状态 · {today}", "",
        f"- 模式：**{str(settings.qbg_mode).upper()}**",
        f"- 持仓源：`{settings.qbg_portfolio_source}`",
        f"- 今天是交易日：{'是' if calendar.is_trading_day(today) else '否'}",
        f"- 当前在交易时段：{'是' if calendar.in_session() else '否'}",
        f"- 今日已完成标记：{'有' if already_completed_today(today) else '无'}",
        f"- 今日日报：{'已生成' if report.exists() else '尚未生成'}",
        "",
        "本命令是**只读**的，没有触发任何动作。",
    ]
    log_event(log, "listener.dispatch", command=STATUS)
    send_with_token("[量化] 运行状态", "\n".join(lines))


def _run_shutdown() -> None:
    """关机。留 60 秒缓冲，期间 `shutdown /a` 可以取消。"""
    log_event(log, "listener.dispatch", command=SHUTDOWN, delay=SHUTDOWN_DELAY_SEC)
    from qbg.notify.mailer import notify_owner

    notify_owner("⏻ 收到关机命令",
                 f"将在 **{SHUTDOWN_DELAY_SEC} 秒**后关机。\n\n"
                 f"反悔的话在这台机器上执行 `shutdown /a` 取消。")
    subprocess.Popen(["shutdown", "/s", "/t", str(SHUTDOWN_DELAY_SEC),
                      "/c", "quant-biga: shutdown by email command"])


def _report_dead_channel(detail: str, failures: int) -> None:
    """连续失败到放弃时，**告诉人一声再退出**。

    安静地死掉是最糟的结局：你以为命令通道在，其实它三小时前就废了。
    """
    log_event(log, "listener.exit", reason="poll failures", failures=failures,
              error=detail)
    try:
        from qbg.notify.mailer import notify_owner

        body = [
            f"监听器连续 {failures} 次轮询失败，已退出。"
            "**回复邮件不再能触发任何命令。**",
            "",
            "最后一次的错误：",
            "",
            "```",
            detail,
            "```",
            "",
            "常见原因：`IMAP_HOST` 和 SMTP 不是同一家；163/126 没在邮箱设置里"
            "开启 IMAP；`SMTP_PASSWORD` 用了登录密码而不是授权码。",
            "",
            "日流程和下单**不受影响** —— 命令通道是旁路。",
        ]
        notify_owner("⚠ 入站命令通道已停止", "\n".join(body))
    except Exception as exc:  # noqa: BLE001
        log_event(log, "listener.report_failed", error=str(exc))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="quant-biga 入站邮件命令监听器")
    parser.add_argument("--poll-sec", type=int, default=settings.email_listener_poll_sec)
    parser.add_argument("--max-hours", type=float, default=settings.email_listener_max_hours)
    parser.add_argument("--dry-run", action="store_true",
                        help="测试模式：鉴权并回执，但**不执行**动作（不下单、不关机）")
    args = parser.parse_args(argv)

    if not (settings.email_commands_enabled and settings.smtp_user
            and settings.smtp_password and settings.imap_host):
        log_event(log, "listener.disabled",
                  reason="EMAIL_COMMANDS_ENABLED=0 或缺 SMTP/IMAP 配置")
        return 0

    lock = _acquire_lock()
    if lock is None:
        log_event(log, "listener.already_running")
        return 0

    deadline = time.time() + args.max_hours * 3600
    log_event(log, "listener.start", poll_sec=args.poll_sec,
              max_hours=args.max_hours, dry_run=bool(args.dry_run))
    try:
        failures = 0
        while time.time() < deadline:
            commands = poll_once()
            if last_poll_error["detail"]:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    _report_dead_channel(last_poll_error["detail"], failures)
                    return 0
            else:
                failures = 0
            for cmd in commands:
                if args.dry_run:
                    _ack(cmd, "当前是**测试模式**，因此未执行任何动作。"
                              "可以再回复本邮件测试下一条命令。")
                    continue
                if cmd == RERUN:
                    _run_rerun()
                elif cmd == STATUS:
                    _run_status()
                elif cmd == SHUTDOWN:
                    _run_shutdown()
                    log_event(log, "listener.exit", reason="shutdown command")
                    return 0
            time.sleep(max(5, args.poll_sec))
        log_event(log, "listener.exit", reason="max lifetime reached")
    except KeyboardInterrupt:
        log_event(log, "listener.exit", reason="interrupted")
    finally:
        try:
            lock.close()
            _LOCK.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
