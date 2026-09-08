"""带鉴权的入站邮件命令（重跑 / 关机 / 状态）。

一封命令邮件只有在**全部**满足时才被执行：

  1. 发件人在白名单里（`EMAIL_COMMAND_ALLOWLIST`）；
  2. 主题回显了当前的一次性令牌（`#T=...`，见 `tokens.py`）；
  3. 正文第一行里**恰好**出现一个已知命令词。

其余一律拒绝，fail-closed。拒绝本身也要安全地处理：

  * 白名单地址 + 令牌错 → 回信告诉他为什么没执行；
  * **非白名单地址 → 绝不回信**。回了就等于确认这个地址存在，
    还会把我们变成一个垃圾邮件反射器。改为通知机主。

本模块只做解析与校验，不执行任何动作 —— 派发在 `scripts/email_listener.py`。

**这里没有任何一条路能碰三把锁。** 「重跑」只是重新触发那条本来就带全部风控闸
的日流程；`QBG_MODE` / `I_CONFIRM_REAL` / `allow_live_mode` 一个都不会被改。
"""

from __future__ import annotations

import email
import email.header
import imaplib
import json
import re
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path

from qbg.config import settings
from qbg.notify.tokens import (
    FOOTER_SENTINEL,
    SELF_HEADER,
    consume_token,
    current_token,
    extract_token,
)
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

RERUN = "RERUN"
SHUTDOWN = "SHUTDOWN"
STATUS = "STATUS"

# 命令词 → 命令。中英文都收，因为手机上打中文更快，而客户端有时会吞掉标点。
KEYWORDS = {
    "重新运行": RERUN, "重跑": RERUN, "rerun": RERUN,
    "关机": SHUTDOWN, "shutdown": SHUTDOWN,
    "状态": STATUS, "status": STATUS,
}

# 引用标记 —— 一道预过滤，免得回复里第一行本身就是被引用的原文（「> 关机」）
# 被读成意图。这是**黑名单**，所以刻意不单独依赖它：`command_text` 同时还
# 限制只读第一行。各家客户端的引用格式差异极大
# （163 用 `---- 回复的原邮件 ----` 加一堆表头行）。
_QUOTE_MARKERS = (
    re.compile(r"^\s*>", re.MULTILINE),
    re.compile(r"^\s*On .{0,120}\bwrote:\s*$", re.MULTILINE),
    re.compile(r"^\s*(在|于).{0,80}(写道|寫道)[：:]\s*$", re.MULTILINE),
    re.compile(r"^\s*-+\s*[^\n]{0,12}(原邮件|原始邮件)\s*-+", re.MULTILINE),
    re.compile(r"^\s*-+\s*(Original Message|Forwarded message)\s*-+", re.MULTILINE),
    re.compile(r"^\s*\|?\s*(发件人|發件人|收件人|主题|主題|日期|From|To|Subject|Date)\s*[|：:]",
               re.MULTILINE),
)


def command_text(body: str) -> str:
    """操作者真正打的那一行 —— 第一行非空行，**而且只有那一行**。

    为什么不能扫全文：回复会把原报告整个引用进来，而那封报告的标题和页脚里
    列着每一个命令词。扫全文会同时看到好几个，然后判成「命令不明确」拒绝掉 ——
    鉴权完美通过，然后拒绝执行任何命令。

    页脚的约定本来就写着「第一行整行只写一个词」，所以就读那一行。
    在此之前先按页脚哨兵和引用标记截断，免得第一行本身是引用文字。
    """
    text = body or ""
    if FOOTER_SENTINEL in text:
        text = text.split(FOOTER_SENTINEL, 1)[0]
    cut = len(text)
    for pattern in _QUOTE_MARKERS:
        match = pattern.search(text)
        if match and match.start() < cut:
            cut = match.start()
    for line in text[:cut].splitlines():
        if line.strip():
            return line.strip()
    return ""


def allowlist() -> set[str]:
    """允许下命令的地址集合。

    留空时回退到收件人/发件人 —— 但那只是为了让没配这一项的部署不至于把
    自己锁在门外，**生产环境应当显式写死**。
    """
    raw = (settings.email_command_allowlist or "").strip()
    if not raw:
        raw = (settings.notify_email_to or settings.smtp_user or "")
    return {a.strip().lower() for a in raw.replace(";", ",").split(",") if a.strip()}


def detect_command(text: str) -> str | None:
    """恰好一个命令词 → 那个命令；一个都没有 → None；两个及以上 → `AMBIGUOUS`。

    **两个词绝不猜。** 猜错的代价是关掉一台正在交易的机器。
    """
    low = (text or "").lower()
    found = {cmd for keyword, cmd in KEYWORDS.items() if keyword in low}
    if not found:
        return None
    if len(found) > 1:
        return "AMBIGUOUS"
    return next(iter(found))


@dataclass(frozen=True)
class ParsedCommand:
    from_addr: str
    command: str | None       # RERUN / SHUTDOWN / STATUS / "AMBIGUOUS" / None
    status: str               # ok / bad_token / not_allowed / ambiguous / none
    token: str | None


def parse_command(from_addr: str, subject: str, body: str,
                  allow: set[str], token_now: str | None) -> ParsedCommand:
    """单封邮件的纯校验 —— 没有 I/O，可离线单测。

    检查顺序是刻意的：先看有没有命令（没有就根本不是给我们的信，
    不该产生任何告警噪音），再看地址，最后看令牌。
    """
    addr = parseaddr(from_addr or "")[1].strip().lower()
    # 命令只从操作者打的第一行取。**不从主题取** —— 主题里带着令牌，
    # 而且回复主题会原样带上原报告的标题，里面可能含命令词。
    cmd = detect_command(command_text(body))
    token = extract_token(subject or "")

    if cmd is None:
        return ParsedCommand(addr, None, "none", token)
    if cmd == "AMBIGUOUS":
        return ParsedCommand(addr, cmd, "ambiguous", token)
    if addr not in allow:
        return ParsedCommand(addr, cmd, "not_allowed", token)
    if not token_now or token != token_now:
        return ParsedCommand(addr, cmd, "bad_token", token)
    return ParsedCommand(addr, cmd, "ok", token)


def _handle_rejection(pc: ParsedCommand) -> None:
    """按安全策略回信/通知。**只会发给白名单地址。**"""
    from qbg.notify.mailer import notify_owner, reply_to_sender

    if pc.status == "not_allowed":
        # 绝不回复不可信的发件人 —— 改为通知机主。
        notify_owner(
            "⛔ 已拒绝非白名单命令",
            f"收到来自非白名单地址的命令：\n\n"
            f"- 发件人：`{pc.from_addr}`\n- 命令：`{pc.command}`\n\n"
            f"已按策略拒绝，**且没有回复该地址**（回复等于确认地址存在）。\n\n"
            f"如果这是你本人的新地址，把它加进 `EMAIL_COMMAND_ALLOWLIST`。")
    elif pc.status == "bad_token":
        reply_to_sender(
            pc.from_addr, "Re: 命令令牌无效",
            "你的命令**未执行**：令牌错误或已过期（令牌是一次性的，用一次就作废）。\n\n"
            "请回复**最新一封**报告邮件，并且**不要修改主题** —— 令牌在主题里。")
    elif pc.status == "ambiguous":
        reply_to_sender(
            pc.from_addr, "Re: 命令不明确",
            "你的邮件第一行里同时出现了多个命令词，为安全起见**未执行**。\n\n"
            "请只写一个：`重跑`、`关机` 或 `状态`。")


def _text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(
                        part.get_content_charset() or "utf-8", "replace")
                except Exception:  # noqa: BLE001
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(
            msg.get_content_charset() or "utf-8", "replace")
    except Exception:  # noqa: BLE001
        return ""


def _seen_path() -> Path:
    return settings.snapshot_dir / "processed_email_uids.json"


def _load_seen() -> set[str]:
    p = _seen_path()
    if not p.exists():
        return set()
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError):
        return set()


def _save_seen(seen: set[str]) -> None:
    p = _seen_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(seen)[-500:]), encoding="utf-8")   # 有界


class ImapSelectError(RuntimeError):
    """选不中收件箱。单独一个类型，好让调用方把它和网络抖动区分开。"""


def _send_imap_id(conn) -> None:
    """向服务器自报家门（RFC 2971 的 `ID` 命令）。

    **163/126 必须发这个**，否则 `SELECT` 会被拒（服务器认为是"不安全登录"），
    而 imaplib 对 `NO` 不抛异常 —— 表现就是后面每一条命令都报
    "illegal in state AUTH"。

    `imaplib` 没有内建 ID，所以要先把它登记进命令表再走 `_simple_command`。
    发不出去只记日志：别家服务器不需要它，不该因此连不上。
    """
    try:
        imaplib.Commands.setdefault("ID", ("AUTH", "SELECTED"))
        conn._simple_command(
            "ID", '("name" "quant-biga" "version" "1.0" "vendor" "quant-biga")')
    except Exception as exc:  # noqa: BLE001
        log_event(log, "inbox.imap_id.skipped", error=f"{type(exc).__name__}: {exc}")


# 最近一次轮询的失败原因。监听器据此判断"连续失败"并升级上报。
# 用可变字典而不是全局变量：模块级 `global` 在测试里更难隔离。
last_poll_error: dict[str, str | None] = {"detail": None}


def poll_once() -> list[str]:
    """查一次收件箱；拒绝在内部处理掉；返回**待派发**的合法命令，最新的在最后。

    **永不抛异常** —— 一次 IMAP 抖动只记日志并返回空列表，不能让监听器死掉。
    """
    if not (settings.email_commands_enabled and settings.smtp_user
            and settings.smtp_password and settings.imap_host):
        return []
    last_poll_error["detail"] = None
    allow = allowlist()
    token_now = current_token()
    seen = _load_seen()
    commands: list[str] = []
    conn = None
    try:
        conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port, timeout=30)
        conn.login(settings.smtp_user, settings.smtp_password)
        _send_imap_id(conn)
        # **必须检查 select 的返回值。** imaplib 对服务器的 `NO` 不抛异常，
        # 只是返回 ('NO', ...)。不检查的话我们会在 AUTH 态里继续发 SEARCH，
        # 服务器回一句 "command SEARCH illegal in state AUTH"，
        # 然后每 60 秒重复一次 —— 2026-08-31 实测刷了三小时。
        typ, _ = conn.select("INBOX")
        if typ != "OK":
            raise ImapSelectError(
                f"select INBOX 失败（{typ}）—— 163/126 需要登录后先发 IMAP ID 命令，"
                f"且必须在邮箱设置里开启 IMAP、用「授权码」而不是登录密码")
        typ, data = conn.uid("SEARCH", None, "UNSEEN")
        if typ != "OK":
            return []
        for uid in (data[0].split() if data and data[0] else []):
            uid_s = uid.decode()
            if uid_s in seen:
                continue
            typ, raw = conn.uid("FETCH", uid, "(RFC822)")
            if typ != "OK" or not raw or not raw[0]:
                continue

            def _mark_read(_uid=uid):
                try:
                    conn.uid("STORE", _uid, "+FLAGS", "(\\Seen)")
                except Exception:  # noqa: BLE001
                    pass

            msg = email.message_from_bytes(raw[0][1])
            seen.add(uid_s)      # 无论如何都记下，避免反复 FETCH
            # 我们自己发出去的信被投回同一个邮箱（收件人通常就是发信邮箱）。
            # 它的页脚里写着全部命令词 —— 不跳过就会被永远读成一条命令。
            if msg.get(SELF_HEADER):
                _mark_read()
                continue
            subject = str(email.header.make_header(
                email.header.decode_header(msg.get("Subject", ""))))
            pc = parse_command(msg.get("From", ""), subject, _text_body(msg),
                               allow, token_now)
            log_event(log, "inbox.command.parsed", frm=pc.from_addr,
                      command=pc.command, status=pc.status)
            if pc.status == "none":
                # 不是给我们的信 —— **保持未读**，别动别人的邮件状态。
                continue
            _mark_read()
            if pc.status == "ok":
                consume_token()      # 一次性：挡重放
                token_now = None     # 本轮后面的邮件需要新令牌才算数
                commands.append(pc.command)
            else:
                _handle_rejection(pc)
        _save_seen(seen)
        return commands
    except Exception as exc:  # noqa: BLE001 —— 轮询失败绝不能杀掉监听器
        detail = f"{type(exc).__name__}: {exc}"
        log_event(log, "inbox.poll.error", error=detail)
        # 把失败原因留给调用方：连续失败要能升级成"告诉人"，而不是
        # 每 60 秒刷一条一模一样的日志刷三小时（2026-08-31 实测）。
        last_poll_error["detail"] = detail
        return []
    finally:
        if conn is not None:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001
                pass
