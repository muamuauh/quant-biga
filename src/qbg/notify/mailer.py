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
from qbg.notify.digest import build_digest
from qbg.notify.tokens import (
    FOOTER_SENTINEL,
    consume_token,
    issue_token,
    tag_subject,
)
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

IMPLICIT_TLS_PORTS = (465, 8465)
# 这两种状态没有执行任何日流程，发信只会制造周末/重复运行噪音。监控日确实读取了
# 账户并生成报告，所以 deliberately 不在此集合中。
# 这些跳过**不发邮件**。共同点：它们都不是"今天本该发生什么但没发生"，
# 而是"今天本来就不该发生什么"。
#
# not_trading_session 是 2026-08-26 加的：计划任务的"登录后 3 分钟"触发器会在
# 盘前跑一次（那天是 08:40），而自动下单必须在盘中。每天登录都收到一封邮件，
# 人很快就不看邮件了 —— 而这套系统的安全网全靠人看邮件。
# 当天真正的那次运行在 09:30，它会照常发信。
QUIET_SKIPS = frozenset({"not_trading_day", "already_completed_today",
                         "not_trading_session"})

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


def normalize_recipients(raw: str) -> str:
    """把收件人列表规范成 `a@x.com, b@y.com`。

    `smtplib.send_message` 从 To 头解析收件人，逗号分隔本来就支持（含空格、
    尾随逗号都能正确解析）。但**分号分隔会静默丢掉除第一个以外的所有人** ——
    只发给第一个，不报错、不告警。而 `a@x.com;b@y.com` 是很常见的写法
    （Outlook 习惯）。

    所以这里两种分隔符都接受，顺手去重去空。多写一行，换掉一整类
    「以为通知了两个人、其实只通知了一个」的静默故障。
    """
    parts = [p.strip() for chunk in str(raw or "").split(";") for p in chunk.split(",")]
    seen: list[str] = []
    for part in parts:
        if part and part not in seen:
            seen.append(part)
    return ", ".join(seen)


def load_config() -> MailConfig:
    user = str(settings.smtp_user or "").strip()
    recipient = normalize_recipients(settings.notify_email_to) or user
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
         cfg: MailConfig | None = None, to: str | None = None) -> dict:
    """发送一封邮件；任何失败都转成结果字典，永不抛异常。

    `to` 覆盖默认收件人 —— 只用于回复**白名单内**的命令发件人。
    调用方负责先确认地址在白名单里；我们绝不回复不可信地址。
    """
    cfg = cfg or load_config()
    if not cfg.configured:
        return {"sent": False, "skipped": f"未配置邮件通知（缺 {', '.join(cfg.missing())}）"}

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = cfg.user
    message["To"] = to or cfg.to
    # 这个头就是入站监听器用来认出「这是我们自己发的」的标记（tokens.SELF_HEADER）。
    # 收件人通常就是发信邮箱，服务商会把副本投回 INBOX，而正文页脚里列着全部
    # 命令词 —— 不打这个标记的话，监听器会把自己的报告永远读成一条命令。
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


def notify_daily_report(result: dict | None = None, *, when: str | None = None,
                        mode: str | None = None, db_path: Path | None = None) -> dict:
    """发送当天日报。

    正文由 `digest.build_digest` 从 **store** 组装（每项独立降级），而不是拼
    当次运行的内存 `result`——邮件最需要看的那一晚，恰恰是流程半路挂掉、
    `result` 残缺不全的那一晚。`result` 现在只用来取日期/模式和判断安静跳过；
    不传它也能为任意历史日期补发。
    """
    result = result or {}
    cfg = load_config()
    if not cfg.configured:
        return {"sent": False, "skipped": f"未配置邮件通知（缺 {', '.join(cfg.missing())}）"}

    day = str(when or result.get("date") or date.today().isoformat())
    env = str(mode or result.get("mode") or settings.qbg_mode).upper()

    # 安静跳过必须在**查 store 之前**。周末 run_daily.bat 在市场检查处就退出、
    # 根本没写 runs 行，先查 store 的话会把周六报成「无运行记录」——一周两次
    # 假警报，更糟的是它让真正的漏跑和周末长得一模一样。
    if result.get("skipped_reason") in QUIET_SKIPS:
        reason = str(result["skipped_reason"])
        log_event(log, "notify.email.quiet_skip", date=day, skipped=reason)
        return {"sent": False, "skipped": reason}
    if not result and not _is_trading_day(day):
        log_event(log, "notify.email.quiet_skip", date=day, reason="not_trading_day")
        return {"sent": False, "skipped": f"{day} 不是交易日，不发"}

    try:
        subject, body = build_digest(day, env, db_path=db_path,
                                     fallback_run=result or None)
    except Exception as exc:  # noqa: BLE001 — 摘要失败本身就是需要人看的事
        subject = f"[量化-{env}] 每日报告 {day} — ⚠ 摘要生成失败"
        body = (f"# 摘要生成失败\n\n`build_digest` 抛出 "
                f"`{type(exc).__name__}: {exc}`\n\n这本身就是需要人看的事。")
    # 命令通道开着时改走 send_with_token：令牌必须由**出站日报**送出去，
    # 因为那封信正好只落进那个唯一能用它的邮箱。
    if commands_enabled():
        return send_with_token(subject, body)
    return send(subject, body, html=markdown_to_html(body), cfg=cfg)


def _is_trading_day(day: str) -> bool:
    """交易日历不可用时返回 True——宁可多发一封，也不要静默漏掉真实的运行。"""
    try:
        from qbg.market import calendar

        return bool(calendar.is_trading_day(day))
    except Exception:  # noqa: BLE001
        return True


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


# ---------------------------------------------------------------------------
# 入站命令通道（可选，默认关闭）
# ---------------------------------------------------------------------------
def commands_enabled() -> bool:
    return bool(int(settings.email_commands_enabled or 0))


def _command_footer(token: str) -> str:
    """告诉操作者怎么用回复驱动监听器。

    **以哨兵开头**：命令解析器只读它**上面**的内容，所以下面列出的这些命令词
    （以及回复时被引用的整份副本）永远不会被当成用户的意图。
    """
    lines = [
        "",
        "",
        FOOTER_SENTINEL,
        "",
        "**远程命令**（直接回复本邮件，**请勿修改主题** —— 令牌在主题里）。"
        "指令写在正文**第一行**，整行只写这一个词：",
        "",
        "- `重跑`：重新跑一遍今天的流程（风控闸一道不少，**不解锁任何东西**）",
        "- `状态`：回一封当前运行状态，不做任何动作",
        "- `关机`：关闭这台机器",
        "",
        f"<sub>命令令牌 `{token}`（一次性，用后失效）。命令必须来自白名单地址"
        "并带本令牌。系统绝不会因为一封邮件而改动三把实盘锁。</sub>",
        "",
    ]
    return "\n".join(lines)


def send_with_token(subject: str, body: str) -> dict:
    """发信，并在命令通道开着时附上一个新的一次性令牌。

    令牌在这里生成，因为**正是这封出站邮件**把它送进那个唯一能用它的邮箱。

    发送失败要把令牌回滚 —— 否则一个谁都没收到的新令牌会把操作者手里那个
    仍然有效的旧令牌顶掉，等于把人锁在门外。
    """
    if not commands_enabled():
        return send(subject, body, html=markdown_to_html(body))
    token = issue_token()
    body = body + _command_footer(token)
    result = send(tag_subject(subject, token), body, html=markdown_to_html(body))
    if not result.get("sent"):
        consume_token()
        log_event(log, "notify.email.token_rolled_back", subject=subject)
    return result


def notify_owner(subject: str, body: str) -> dict:
    """给机主发一条运维通知。

    监听器用它来报告「拒绝了一条非白名单命令」——**绝不回复那个不可信地址**
    （回了就是垃圾邮件反射器，还等于确认地址有效）。
    """
    mode = str(settings.qbg_mode).upper()
    return send(f"[量化-{mode}] {subject}", body, html=markdown_to_html(body))


def reply_to_sender(to_addr: str, subject: str, body: str) -> dict:
    """回复一个**白名单内**的发件人（例如令牌过期）。

    调用方负责先确认 `to_addr` 在白名单里 —— 这个函数不自己检查，
    因为它也被用在已经校验过的路径上。**不要**拿它回复任意地址。
    """
    return send(subject, body, html=markdown_to_html(body), to=to_addr)

