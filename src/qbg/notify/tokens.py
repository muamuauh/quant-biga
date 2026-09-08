"""出站邮件与入站命令共享的一次性令牌。

每封出站报告/告警的**主题**里带一个随机令牌（`#T=<token>`）。入站命令只有在
主题回显了**当前**令牌时才被执行 —— 而令牌只出现在发往你自己邮箱的那封信里，
外人拿不到。

**令牌才是真正的鉴权，白名单不是。** 发件地址可以伪造，令牌不能。白名单是
第二道，用来在令牌万一泄漏时缩小可用范围，两道都要。

令牌是一次性的：命令执行后立即作废，下一封出站邮件再发一个新的。这挡掉重放 ——
否则一封旧邮件可以被反复转发来重复触发。

这是一个能触发真实交易流程的开关，所以整套判断刻意写得简单且 **fail-closed**：
令牌缺失、不匹配、过期、读不出来 —— 一律拒绝。
"""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

from qbg.config import settings

SUBJECT_TAG = "#T="  # 主题里的令牌标记

# 回复协议的两个常量。发信方（mailer）和解析方（inbox）都要用，所以放这里：
#
#   FOOTER_SENTINEL —— 操作者写的内容到此为止，下面是我们自己的说明文字。
#       命令解析器**只读它上面的部分**，这样页脚里列出的那些命令词
#       （「重跑」「关机」……）就不会被当成用户的意图。
#   SELF_HEADER —— 我们发出去的每封信都盖这个头。收件人通常就是发信邮箱本身，
#       服务商会把这封信也投进 INBOX —— 而它的页脚里写着全部命令词，
#       不排除的话会被永远读成一条命令。监听器见到这个头就跳过。
FOOTER_SENTINEL = "-- qbg-cmd --"
SELF_HEADER = "X-Quant-Biga"

# 兜底过期时间。真正的上限是 EMAIL_LISTENER_MAX_HOURS（默认 3 小时），
# 这一条是防止第二天手工起一个监听器时，昨天那封邮件的令牌还能用。
TOKEN_TTL_HOURS = 24


def _token_path() -> Path:
    return settings.snapshot_dir / "command_token.json"


def issue_token() -> str:
    """生成并落盘一个新令牌（覆盖旧的），返回它。"""
    token = secrets.token_hex(4)  # 8 个十六进制字符：熵够用，放主题里又不占地方
    p = _token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"token": token,
                             "issued": datetime.now(UTC).isoformat()}),
                 encoding="utf-8")
    return token


def current_token() -> str | None:
    """当前有效且未过期的令牌；没有 / 过期 / 读不出来都返回 None。"""
    p = _token_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    token = data.get("token") or None
    if not token:
        return None
    try:
        issued = datetime.fromisoformat(data["issued"])
        if datetime.now(UTC) - issued > timedelta(hours=TOKEN_TTL_HOURS):
            return None
    except (KeyError, TypeError, ValueError):
        # 时间戳读不懂**不是**放行的理由 —— fail-closed。
        return None
    return token


def consume_token() -> None:
    """作废当前令牌（命令执行后调用），挡掉同一封邮件的重放。"""
    try:
        _token_path().unlink(missing_ok=True)
    except OSError:
        pass


def tag_subject(subject: str, token: str) -> str:
    return f"{subject} {SUBJECT_TAG}{token}"


def extract_token(subject: str) -> str | None:
    """从（可能带 `Re:` 前缀的）回复主题里取出令牌。"""
    if not subject or SUBJECT_TAG not in subject:
        return None
    tail = subject.split(SUBJECT_TAG, 1)[1].strip()
    token = tail.split()[0] if tail else ""
    # 只保留十六进制字符：有些客户端会在后面粘上标点。
    token = "".join(c for c in token if c in "0123456789abcdefABCDEF")
    return token or None
