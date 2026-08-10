"""预览、测试或补发 quant-biga 每日报告邮件。

用法：
  python scripts/27_notify.py --test
  python scripts/27_notify.py --date 2026-08-10 --dry-run
  python scripts/27_notify.py --date 2026-08-10

脚本永远以 0 退出：邮件是旁路通知，不能改变日流程的成功/失败结论。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from qbg.config import settings  # noqa: E402
from qbg.notify import (  # noqa: E402
    IMPLICIT_TLS_PORTS,
    build_daily_message,
    load_config,
    markdown_to_html,
    send,
)


def _load_result(when: str) -> dict:
    """优先从派生库读取完整 payload；库不可用时仍可按报告路径补发。"""
    mode = settings.qbg_mode.upper()
    if settings.db_path.exists():
        try:
            with sqlite3.connect(settings.db_path) as connection:
                row = connection.execute(
                    "SELECT payload_json FROM runs WHERE date=? AND mode=?",
                    (when, mode),
                ).fetchone()
            if row and row[0]:
                result = json.loads(row[0])
                result.setdefault("date", when)
                result.setdefault("mode", mode)
                result.setdefault(
                    "report_path", str(settings.report_dir / "daily" / f"{when}.md")
                )
                return result
        except (OSError, sqlite3.Error, json.JSONDecodeError):
            pass
    return {
        "date": when,
        "mode": mode,
        "report_path": str(settings.report_dir / "daily" / f"{when}.md"),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="YYYY-MM-DD；默认今天")
    parser.add_argument("--dry-run", action="store_true", help="打印主题和正文，不发送")
    parser.add_argument("--test", action="store_true", help="发送一封 SMTP/HTML 配置测试邮件")
    args = parser.parse_args(argv)

    cfg = load_config()
    if not cfg.configured:
        print(f"邮件通知未配置（缺 {', '.join(cfg.missing())}），跳过。")
        print("请在 .env 填 NOTIFY_EMAIL_ENABLED / SMTP_* / NOTIFY_EMAIL_TO。")
        return 0

    tls = "隐式 TLS" if cfg.port in IMPLICIT_TLS_PORTS else "STARTTLS"
    print(f"收件人：{cfg.to}")
    print(f"服务器：{cfg.host}:{cfg.port}（{tls}）")

    if args.test:
        body = (
            "# quant-biga 邮件配置测试\n\n"
            f"*{settings.qbg_mode.upper()} · SMTP 与 HTML 渲染自检*\n\n"
            "| 项 | 值 |\n|---|---|\n"
            f"| 服务器 | {cfg.host}:{cfg.port} |\n"
            f"| 收件人 | {cfg.to} |\n\n"
            "收到这封邮件且表格正常显示，说明邮件通知已经可用。"
        )
        result = send(
            f"[量化-{settings.qbg_mode.upper()}] 邮件配置测试",
            body,
            html=markdown_to_html(body),
            cfg=cfg,
        )
        print(result)
        return 0

    when = args.date or __import__("datetime").date.today().isoformat()
    subject, body = build_daily_message(_load_result(when))
    if args.dry_run:
        print(f"\n--- 主题 ---\n{subject}\n\n--- 正文 ---\n{body}")
        print("\n--dry-run：没有发送。")
        return 0

    print(send(subject, body, html=markdown_to_html(body), cfg=cfg))
    return 0


if __name__ == "__main__":
    main()
    raise SystemExit(0)
