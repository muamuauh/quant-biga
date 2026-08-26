"""多收件人解析。

`smtplib.send_message` 从 To 头解析收件人，逗号分隔本来就支持。
但**分号分隔会静默丢掉除第一个以外的所有人**（实测），而那是很常见的写法。
「以为通知了两个人、其实只通知了一个」是不会有人发现的那种故障。
"""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import getaddresses

import pytest

from qbg.notify.mailer import normalize_recipients


def _recipients(header: str) -> list[str]:
    """走 smtplib 实际使用的那条解析路径。"""
    message = EmailMessage()
    message["To"] = header
    return [addr for _name, addr in getaddresses(message.get_all("To", []))]


@pytest.mark.parametrize("raw", [
    "a@x.com,b@y.com",
    "a@x.com, b@y.com",
    " a@x.com ,b@y.com ",
    "a@x.com,b@y.com,",
    "a@x.com;b@y.com",          # ← 不规范化的话这个只会发给 a
    "a@x.com; b@y.com",
    "a@x.com,,b@y.com",
])
def test_two_recipients_survive(raw):
    assert _recipients(normalize_recipients(raw)) == ["a@x.com", "b@y.com"]


def test_semicolon_would_silently_drop_without_normalization():
    """锁住动机：不规范化时分号确实只解析出一个地址，而且不报错。"""
    assert _recipients("a@x.com;b@y.com") == ["a@x.com"]


def test_single_recipient_unchanged():
    assert normalize_recipients("a@x.com") == "a@x.com"


def test_duplicates_removed():
    assert normalize_recipients("a@x.com, a@x.com, b@y.com") == "a@x.com, b@y.com"


def test_order_preserved():
    """去重不能打乱顺序 —— 第一个通常是主要收件人。"""
    assert normalize_recipients("b@y.com, a@x.com") == "b@y.com, a@x.com"


@pytest.mark.parametrize("raw", ["", "   ", ",", ";", " , ; "])
def test_empty_yields_empty(raw):
    """空值要落回 SMTP_USER（由 load_config 处理），这里只保证返回空串。"""
    assert normalize_recipients(raw) == ""


def test_three_recipients():
    assert normalize_recipients("a@x.com;b@y.com,c@z.com") == "a@x.com, b@y.com, c@z.com"


# ---------------------------------------------------------------------------
# 邮件主题必须反映**券商那边实际发生了什么**。
# 2026-08-25 实测：3 笔单一笔都没进券商，主题却是「已提交3笔」——
# `submitted` 指的是"顾问清单已落盘"，不是"券商收到了单"。人只看主题。
# ---------------------------------------------------------------------------
def _tag(**over):
    from qbg.notify.digest import status_tag
    return status_tag({"submitted": True, "allowed_orders": [{}, {}, {}], **over}, [], None)


def test_subject_says_failed_when_no_order_reached_broker():
    tag = _tag(broker={"ok": False, "outcomes": [{"ok": False}]})
    assert "下单失败" in tag and "0/3" in tag
    assert "已提交" not in tag


def test_subject_flags_partial_fill():
    assert "部分成交 2/3" in _tag(
        broker={"ok": False, "outcomes": [{"ok": True}, {"ok": True}, {"ok": False}]})


def test_subject_reports_broker_success():
    assert _tag(broker={"ok": True, "outcomes": [{"ok": True}] * 3}) == "券商已接单3笔"


def test_subject_leads_with_unreadable_portfolio():
    """持仓读不到是最严重的 —— 盖过其他一切状态。"""
    tag = _tag(portfolio={"source": "default", "degraded": {"reason": "对不上账"}},
               broker={"ok": True, "outcomes": [{"ok": True}] * 3})
    assert "持仓读不到" in tag


def test_advisory_subject_no_longer_claims_submission():
    """顾问模式只是生成清单，说「已提交」会让人以为单子下出去了。"""
    tag = _tag()
    assert "已生成清单3笔" == tag


def test_subject_distinguishes_unverified_from_failed():
    """「确认不了」比「失败」严重：失败可以补单，确认不了不能碰。"""
    tag = _tag(broker={"ok": False, "outcomes": [
        {"ok": False, "verified": False}]})
    assert "待人工核对" in tag
    assert "下单失败" not in tag


# ---------------------------------------------------------------------------
# store 的 runs 表没有 broker / portfolio 两列，而主题最要紧的信息恰恰在那里。
# 2026-08-26 实测：两笔单真的进了券商并回读通过，主题却是「已生成清单2笔」；
# 而 08:43 那次一笔都没成功，主题也是同一句式 —— 完全区分不出来。
# ---------------------------------------------------------------------------
def test_digest_merges_broker_facts_from_the_run_result():
    from qbg.notify.digest import build_digest
    store_row = {"submitted": 1, "hard_ok": 1}          # store 里有的那些列
    result = {"date": "2026-08-26", "mode": "PAPER",
              "allowed_orders": [{}, {}],
              "broker": {"ok": True, "outcomes": [{"ok": True}, {"ok": True}]}}
    subject, _body = build_digest("2026-08-26", "PAPER",
                                  db_path=None, fallback_run=result)
    assert "已生成清单" not in subject, "store 行盖住了券商回执"
    assert "券商已接单2笔" in subject


def test_digest_merges_portfolio_degradation():
    from qbg.notify.digest import build_digest
    result = {"date": "2026-08-26", "mode": "PAPER", "allowed_orders": [{}],
              "portfolio": {"source": "default", "degraded": {"reason": "对不上账"}},
              "broker": {"ok": True, "outcomes": [{"ok": True}]}}
    subject, _body = build_digest("2026-08-26", "PAPER",
                                  db_path=None, fallback_run=result)
    assert "持仓读不到" in subject


def test_out_of_session_skip_sends_no_email_at_all():
    """盘前触发的跳过**不发信**。

    计划任务的"登录后 3 分钟"触发器每天早上都会跑一次（实测 08:40），
    而自动下单必须在盘中。每天登录都收到一封邮件，人很快就不看邮件了 ——
    而这套系统的安全网全靠人看邮件。当天真正那次在 09:30，它会照常发。
    """
    from qbg.notify.mailer import QUIET_SKIPS
    assert "not_trading_session" in QUIET_SKIPS


def test_store_row_still_wins_for_skipped_reason():
    """`runs` 表自己有 skipped_reason 这一列 —— 不能被调用方的值盖过。

    只有 store **根本没有的列**（broker / portfolio / allowed_orders）
    才从 result 叠加。
    """
    from qbg.notify.digest import build_digest
    _subject, _body = build_digest("2026-08-26", "PAPER", db_path=None,
                                   fallback_run={"skipped_reason": "not_rebalance_day"})
    import inspect

    from qbg.notify import digest
    src = inspect.getsource(digest.build_digest)
    assert '"skipped_reason"' not in src.split("_FROM_RESULT")[1].split(")")[0]
