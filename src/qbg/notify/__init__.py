"""只出站的邮件通知；SMTP 故障永远不能影响交易流程。"""

from qbg.notify.mailer import (
    IMPLICIT_TLS_PORTS,
    MailConfig,
    build_daily_message,
    load_config,
    markdown_to_html,
    notify_daily_report,
    notify_failure,
    send,
)

__all__ = [
    "IMPLICIT_TLS_PORTS",
    "MailConfig",
    "build_daily_message",
    "load_config",
    "markdown_to_html",
    "notify_daily_report",
    "notify_failure",
    "send",
]
