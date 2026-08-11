"""把每日 Markdown 日报和崩溃告警发送给账户所有者。

设计参考 quant-agent/quant-trading 的成熟实现：Markdown 同时发送纯文本与 HTML，
主题直接写运行结论，465/8465 使用隐式 TLS，其余端口使用 STARTTLS。通知是旁路：
未配置时静默跳过，渲染或 SMTP 失败只记日志，绝不阻断清单生成和风控流程。
"""

from __future__ import annotations

import smtplib
import ssl
import traceback
from dataclasses import dataclass
from datetime import date
from email.message import EmailMessage
from pathlib import Path

from qbg.config import settings
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

IMPLICIT_TLS_PORTS = (465, 8465)
# 这两种状态没有执行任何日流程，发信只会制造周末/重复运行噪音。监控日确实读取了
# 账户并生成报告，所以 deliberately 不在此集合中。
QUIET_SKIPS = frozenset({"not_trading_day", "already_completed_today"})

EMAIL_CSS = """
body { background:#f4f6f8; margin:0; padding:24px 12px;
       font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,
       'PingFang SC','Microsoft YaHei',sans-serif; color:#1f2328;
       line-height:1.5; overflow-wrap:anywhere; word-break:break-word; }
.wrap { max-width:760px; margin:0 auto; background:#ffffff; border-radius:12px;
        padding:0 0 20px; border:1px solid #dfe3e8; overflow:hidden;
        box-shadow:0 5px 20px rgba(31,35,40,.06); }
.wrap > :not(.bar):not(.foot) { margin-left:24px; margin-right:24px; }
.bar { background:#ffffff; padding:21px 24px 17px; margin:0 0 4px;
       border-top:5px solid #1677ff; border-bottom:1px solid #edf0f2; }
h1 { font-size:22px; line-height:1.3; margin:0; color:#20252b; letter-spacing:-.2px; }
h1 + p { margin-top:5px; }
.bar em { color:#69717a; font-style:normal; font-size:12.5px; }
em { color:#69717a; }
h2 { font-size:15.5px; margin:23px 0 9px; padding-bottom:6px; color:#1677ff;
     border-bottom:1px solid #dbe8f8; }
h3 { font-size:13.5px; margin:14px 0 6px; }
p { margin:6px 0; }
table { display:block; max-width:100%; overflow-x:auto; -webkit-overflow-scrolling:touch;
        border-collapse:collapse; margin:10px 0 13px; font-size:12.5px; white-space:nowrap; }
th,td { border:1px solid #e1e5e9; padding:7px 10px; text-align:right;
        font-variant-numeric:tabular-nums; }
th:first-child, td:first-child { text-align:left; }
th { background:#f3f5f7; font-weight:600; color:#3a4149; }
tr:nth-child(even) td { background:#fafbfc; }
ul { margin:6px 0; padding-left:20px; }
li { margin:3px 0; }
blockquote { margin:10px 0 14px; padding:10px 13px; background:#f2f7ff;
             border-left:4px solid #1677ff; border-radius:5px; color:#28384d; }
blockquote p { margin:0; }
pre { background:#f6f8fa; border:1px solid #e3e6ea; border-radius:6px;
      padding:10px; overflow-x:auto; font-size:12px; white-space:pre-wrap;
      word-break:break-word; line-height:1.45; }
code { font-family:'SFMono-Regular',Consolas,monospace; }
hr { border:0; border-top:1px solid #eaecef; margin:16px 0; }
sub, .foot { color:#8a9199; font-size:11px; }
.foot { margin:22px 24px 0; padding-top:12px; border-top:1px solid #edf0f2; }
@media only screen and (max-width:600px) {
  body { padding:0; background:#fff; }
  .wrap { border:0; border-radius:0; box-shadow:none; }
  .wrap > :not(.bar):not(.foot) { margin-left:15px; margin-right:15px; }
  .bar { padding:17px 15px 14px; }
  h1 { font-size:20px; }
  .foot { margin-left:15px; margin-right:15px; }
}
"""


@dataclass(frozen=True)
class MailConfig:
    enabled: bool = False
    host: str = ""
    port: int = 587
    user: str = ""
    password: str = ""
    to: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.enabled and self.host and self.user and self.password and self.to)

    def missing(self) -> list[str]:
        missing = []
        if not self.enabled:
            missing.append("NOTIFY_EMAIL_ENABLED=1")
        if not self.host:
            missing.append("SMTP_HOST")
        if not self.user:
            missing.append("SMTP_USER")
        if not self.password:
            missing.append("SMTP_PASSWORD")
        if not self.to:
            missing.append("NOTIFY_EMAIL_TO/SMTP_USER")
        return missing


def load_config() -> MailConfig:
    user = str(settings.smtp_user or "").strip()
    recipient = str(settings.notify_email_to or "").strip() or user
    return MailConfig(
        enabled=bool(int(settings.notify_email_enabled or 0)),
        host=str(settings.smtp_host or "").strip(),
        port=int(settings.smtp_port or 587),
        user=user,
        password=str(settings.smtp_password or ""),
        to=recipient,
    )


def markdown_to_html(md_text: str) -> str | None:
    """渲染为自包含 HTML；依赖不可用或渲染失败时退回纯文本。"""
    try:
        import re

        from markdown_it import MarkdownIt

        body = MarkdownIt("commonmark").enable("table").render(md_text)
        body = re.sub(
            r"^\s*(<h1>.*?</h1>)\s*(<p>.*?</p>)?",
            lambda match: f'<div class="bar">{match.group(1)}{match.group(2) or ""}</div>',
            body,
            count=1,
            flags=re.DOTALL,
        )
        return (
            '<!doctype html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<style>{EMAIL_CSS}</style></head>"
            f'<body><div class="wrap">{body}'
            '<div class="foot">quant-biga 自动通知 · 数据与订单意见仅供决策参考</div>'
            '</div></body></html>'
        )
    except Exception as exc:  # noqa: BLE001 — 纯文本仍是一封完整通知
        log_event(log, "notify.email.render_error", error=f"{type(exc).__name__}: {exc}")
        return None


def send(subject: str, body: str, *, html: str | None = None,
         cfg: MailConfig | None = None) -> dict:
    """发送一封邮件；任何失败都转成结果字典，永不抛异常。"""
    cfg = cfg or load_config()
    if not cfg.configured:
        return {"sent": False, "skipped": f"未配置邮件通知（缺 {', '.join(cfg.missing())}）"}

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg.user
    message["To"] = cfg.to
    message["X-Quant-Biga"] = "notification"
    message.set_content(body)
    if html:
        message.add_alternative(html, subtype="html")

    try:
        context = ssl.create_default_context()
        if cfg.port in IMPLICIT_TLS_PORTS:
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=30, context=context) as smtp:
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=30) as smtp:
                smtp.starttls(context=context)
                smtp.login(cfg.user, cfg.password)
                smtp.send_message(message)
    except Exception as exc:  # noqa: BLE001 — 通知失败不能反向破坏日流程
        error = f"{type(exc).__name__}: {exc}"
        log_event(log, "notify.email.error", error=error, host=cfg.host,
                  port=cfg.port, subject=subject)
        return {"sent": False, "error": error}

    log_event(log, "notify.email.sent", to=cfg.to, subject=subject)
    return {"sent": True, "to": cfg.to, "subject": subject}


def _status_tag(result: dict) -> str:
    skipped = result.get("skipped_reason")
    if skipped == "not_rebalance_day":
        return "监控日"
    if skipped:
        return str(skipped)
    if not result.get("hard_ok", True):
        return "⚠ 硬闸中止"
    review = result.get("daily_review") or {}
    if review and not review.get("ok"):
        return "⚠ 复盘失败"
    if result.get("submitted"):
        return f"清单{len(result.get('allowed_orders') or [])}笔"
    if result.get("allowed_orders"):
        # dry-run 会完整走完风控但刻意不提交；主题应表达“有建议、未执行”，不能把
        # submitted=False 错译成没有允许订单。
        return f"订单建议{len(result.get('allowed_orders') or [])}笔·未执行"
    if result.get("orders"):
        return "无允许订单"
    return "正常·无订单"


def build_daily_message(result: dict) -> tuple[str, str]:
    """从已经落盘的日报构造主题和正文，保证邮件与本地报告是一份内容。"""
    when = str(result.get("date") or date.today().isoformat())
    mode = str(result.get("mode") or settings.qbg_mode).upper()
    subject = f"[量化-{mode}] 每日报告 {when} — {_status_tag(result)}"
    path_value = result.get("report_path")
    path = Path(path_value) if path_value else settings.report_dir / "daily" / f"{when}.md"
    try:
        return subject, path.read_text(encoding="utf-8")
    except OSError as exc:
        # 报告缺失本身就是需要通知的异常；保留最小运行事实，不因一个文件问题静默。
        body = (
            f"# quant-biga 日报读取失败 · {when}\n\n"
            f"- 模式：{mode}\n"
            f"- 运行结论：{_status_tag(result)}\n"
            f"- 报告路径：`{path}`\n"
            f"- 异常：`{type(exc).__name__}: {exc}`\n"
        )
        return f"[量化-{mode}] 每日报告 {when} — ⚠ 报告读取失败", body


def notify_daily_report(result: dict) -> dict:
    """发送当天日报；无运行的安静跳过日不发信。"""
    cfg = load_config()
    if not cfg.configured:
        return {"sent": False, "skipped": f"未配置邮件通知（缺 {', '.join(cfg.missing())}）"}
    if result.get("skipped_reason") in QUIET_SKIPS:
        reason = str(result["skipped_reason"])
        log_event(log, "notify.email.quiet_skip", skipped=reason)
        return {"sent": False, "skipped": reason}
    try:
        subject, body = build_daily_message(result)
    except Exception as exc:  # noqa: BLE001 — 摘要失败也要尽力发出告警
        mode = str(result.get("mode") or settings.qbg_mode).upper()
        subject = f"[量化-{mode}] 每日报告 — ⚠ 摘要生成失败"
        body = f"# 摘要生成失败\n\n`{type(exc).__name__}: {exc}`"
    return send(subject, body, html=markdown_to_html(body), cfg=cfg)


def notify_failure(exc: BaseException, *, when: str | None = None,
                   context: str = "daily_cycle") -> dict:
    """发送带 traceback 的崩溃告警；自身永不抛异常。"""
    cfg = load_config()
    if not cfg.configured:
        return {"sent": False, "skipped": f"未配置邮件通知（缺 {', '.join(cfg.missing())}）"}
    day = when or date.today().isoformat()
    mode = settings.qbg_mode.upper()
    trace = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    subject = f"[量化-{mode}] ⚠ 运行失败 {day} — {type(exc).__name__}"
    body = (
        f"quant-biga 运行失败\n日期：{day}\n模式：{mode}\n位置：{context}\n"
        f"异常：{type(exc).__name__}: {exc}\n\n--- Traceback ---\n{trace}\n\n"
        "提示：本次下单清单可能未生成，请检查 logs/qbg.jsonl 和任务计划日志。"
    )
    return send(subject, body, cfg=cfg)
