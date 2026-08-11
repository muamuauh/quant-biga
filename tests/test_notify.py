from __future__ import annotations

import smtplib

import pytest

from qbg.notify import mailer


@pytest.fixture
def mail_config():
    return mailer.MailConfig(
        enabled=True,
        host="smtp.example.com",
        port=465,
        user="sender@example.com",
        password="app-password",
        to="owner@example.com",
    )


class _FakeSMTP:
    def __init__(self, captured: dict):
        self.captured = captured

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def starttls(self, **kwargs):
        self.captured["starttls"] = True

    def login(self, user, password):
        self.captured["login"] = (user, password)

    def send_message(self, message):
        self.captured["message"] = message


def test_unconfigured_mail_is_a_noop():
    result = mailer.send("subject", "body", cfg=mailer.MailConfig())
    assert result["sent"] is False
    assert "NOTIFY_EMAIL_ENABLED=1" in result["skipped"]
    assert "SMTP_HOST" in result["skipped"]


def test_smtp_failure_is_swallowed(monkeypatch, mail_config):
    def fail(*args, **kwargs):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(smtplib, "SMTP_SSL", fail)
    result = mailer.send("subject", "body", cfg=mail_config)
    assert result["sent"] is False
    assert "SMTPAuthenticationError" in result["error"]


def test_port_465_uses_implicit_tls_and_multipart(monkeypatch, mail_config):
    captured = {}
    monkeypatch.setattr(smtplib, "SMTP_SSL", lambda *args, **kwargs: _FakeSMTP(captured))
    monkeypatch.setattr(
        smtplib,
        "SMTP",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("STARTTLS path used")),
    )
    result = mailer.send("subject", "body", html="<html>body</html>", cfg=mail_config)
    assert result["sent"] is True
    types = [part.get_content_type() for part in captured["message"].walk()]
    assert "text/plain" in types and "text/html" in types


def test_port_587_uses_starttls(monkeypatch, mail_config):
    captured = {}
    cfg = mailer.MailConfig(**{**mail_config.__dict__, "port": 587})
    monkeypatch.setattr(smtplib, "SMTP", lambda *args, **kwargs: _FakeSMTP(captured))
    monkeypatch.setattr(
        smtplib,
        "SMTP_SSL",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("SMTPS path used")),
    )
    assert mailer.send("subject", "body", cfg=cfg)["sent"] is True
    assert captured["starttls"] is True


def test_markdown_html_contains_responsive_table():
    html = mailer.markdown_to_html("# 日报\n\n|代码|评级|\n|---|---|\n|600519.SH|Hold|\n")
    assert html is not None
    assert "<table>" in html and "<style>" in html and 'class="bar"' in html
    assert "@media only screen" in html
    assert "quant-biga 自动通知" in html


def test_build_message_reads_exact_report(tmp_path):
    report = tmp_path / "2026-08-10.md"
    report.write_text("# 最终日报\n\n完整正文。", encoding="utf-8")
    subject, body = mailer.build_daily_message({
        "date": "2026-08-10",
        "mode": "ADVISORY",
        "submitted": True,
        "allowed_orders": [{}, {}],
        "report_path": str(report),
    })
    assert subject == "[量化-ADVISORY] 每日报告 2026-08-10 — 清单2笔"
    assert body == "# 最终日报\n\n完整正文。"


def test_dry_run_subject_reports_allowed_advice_not_no_orders(tmp_path):
    report = tmp_path / "2026-08-10.md"
    report.write_text("# dry-run 日报", encoding="utf-8")
    subject, _ = mailer.build_daily_message({
        "date": "2026-08-10",
        "mode": "ADVISORY",
        "submitted": False,
        "orders": [{}, {}],
        "allowed_orders": [{}, {}],
        "report_path": str(report),
    })
    assert subject.endswith("订单建议2笔·未执行")


def test_quiet_skip_is_not_sent(monkeypatch, mail_config):
    monkeypatch.setattr(mailer, "load_config", lambda: mail_config)
    monkeypatch.setattr(
        mailer,
        "send",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("mail sent")),
    )
    result = mailer.notify_daily_report({"skipped_reason": "not_trading_day"})
    assert result == {"sent": False, "skipped": "not_trading_day"}


def test_monitoring_day_is_mailed(monkeypatch, mail_config, tmp_path):
    report = tmp_path / "daily.md"
    report.write_text("# 监控日报", encoding="utf-8")
    monkeypatch.setattr(mailer, "load_config", lambda: mail_config)
    monkeypatch.setattr(
        mailer,
        "send",
        lambda subject, body, **kwargs: {"sent": True, "subject": subject, "body": body},
    )
    result = mailer.notify_daily_report({
        "date": "2026-08-10",
        "mode": "ADVISORY",
        "skipped_reason": "not_rebalance_day",
        "report_path": str(report),
    })
    assert result["sent"] is True
    assert "监控日" in result["subject"]
