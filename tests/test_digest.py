"""store 驱动的邮件摘要。全离线，用临时 sqlite。

核心纪律是**每项独立降级**：宁可发一封缺了某个章节的信，也不要因为一张表
读不出来就什么都不发——那正是最需要收到邮件的时候。下面一半的测试都在验
这条。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from qbg.notify import digest


@pytest.fixture(autouse=True)
def isolate_reports(tmp_path, monkeypatch):
    """把 `report_dir` 指到临时目录。

    不隔离的话，`daily_report_path()` 会读到本机 `reports/` 里真实存在的日报，
    「日报缺失」的那些分支永远测不到——而且测试结果会随开发机上有没有跑过
    日循环而变。日志目录同理。
    """
    monkeypatch.setattr(digest.settings, "report_dir", tmp_path / "reports")
    monkeypatch.setattr(digest.settings, "log_dir", tmp_path / "logs")


@pytest.fixture
def db(tmp_path) -> Path:
    """建一个带完整 schema 的空库。"""
    path = tmp_path / "runs.db"
    schema = Path("src/qbg/store/schema.sql").read_text(encoding="utf-8")
    with sqlite3.connect(path) as con:
        con.executescript(schema)
    return path


def write_report(tmp_path, when="2026-08-10", text="# 日报\n\n## 概览\n\n真正的概览在这里"):
    p = tmp_path / "reports" / "daily" / f"{when}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _insert(db: Path, sql: str, params: tuple) -> None:
    with sqlite3.connect(db) as con:
        con.execute(sql, params)


def add_run(db, when="2026-08-10", mode="ADVISORY", *, skipped=None,
            submitted=0, hard_ok=1):
    _insert(db, "INSERT INTO runs (date,mode,run_kind,skipped_reason,submitted,hard_ok) "
                "VALUES (?,?,?,?,?,?)",
            (when, mode, "rebalance", skipped, submitted, hard_ok))


def add_equity(db, when, total, cash=1000.0, mode="ADVISORY"):
    _insert(db, "INSERT INTO equity (date,mode,total_equity,available_cash) VALUES (?,?,?,?)",
            (when, mode, total, cash))


def add_position(db, when, *, day_pnl, code="600519.SH", mode="ADVISORY"):
    _insert(db, "INSERT INTO positions (date,mode,code,qty,pnl,day_pnl) VALUES (?,?,?,?,?,?)",
            (when, mode, code, 100, 0.0, day_pnl))


# ----------------------------------------------------------------------
# 主题与状态
# ----------------------------------------------------------------------


def test_subject_carries_mode_date_and_status(db):
    add_run(db)
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "[量化-ADVISORY]" in subject
    assert "2026-08-10" in subject


def test_subject_falls_back_to_equity_change_with_its_date(db):
    """拿不到券商当日盈亏时退回净值变化，**必须带上对比日期**。

    那个口径会静默跨越好几天（周末、停机、读不到账户），
    不写清楚就又是一个顶着"当日"名字的跨天数字。
    """
    add_equity(db, "2026-08-07", 100_000.0)
    add_equity(db, "2026-08-10", 101_234.5)
    add_run(db)
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "+1,234.50" in subject
    assert "2026-08-07" in subject, "跨了 3 天，得让人看见是跟哪天比"
    assert "当日" not in subject, "净值变化不是当日盈亏，别用那个名字"


def test_subject_prefers_the_broker_figure(db):
    """**标题要放操作者能对上账的那个数。**

    2026-09-23 实测：净值变化 −4,688.00、券商当日盈亏 +210.00、持仓盈亏
    −1,879.06。当时标题显示 −4,688.00 还管它叫「当日盈亏」 —— 恰好是操作者
    在同花顺上唯一看不到的那一个。
    """
    add_equity(db, "2026-09-22", 198_113.14)
    add_equity(db, "2026-09-23", 193_425.14)
    add_position(db, "2026-09-23", day_pnl=210.0)
    add_run(db, when="2026-09-23")
    subject, body = digest.build_digest("2026-09-23", "ADVISORY", db_path=db)
    assert "+210.00" in subject
    assert "-4,688.00" not in subject, "标题别放那个对不上账的数"
    # 但正文两个都要有 —— 净值曲线看的是后者。
    assert "+210.00" in body and "-4,688.00" in body
    assert "2026-09-22" in body, "净值变化要写明跟哪天比"


def test_the_two_measures_stay_separate(db):
    add_equity(db, "2026-09-22", 198_113.14)
    add_equity(db, "2026-09-23", 193_425.14)
    add_position(db, "2026-09-23", day_pnl=210.0)
    row = digest.equity_row("2026-09-23", "ADVISORY", db)
    assert row["broker_day_pnl"] == 210.0
    assert row["equity_change"] == -4688.0
    assert row["prev_date"] == "2026-09-22"
    assert "day_pnl" not in row, "含糊的旧名字不该留着 —— 它同时像这两个数"


def test_no_broker_column_means_none_not_zero(db):
    """顾问模式 / 老客户端 / OCR 来源都没有这一列。0 是在替券商断言"今天平盘"。"""
    add_equity(db, "2026-09-22", 198_113.14)
    add_equity(db, "2026-09-23", 193_425.14)
    add_position(db, "2026-09-23", day_pnl=None)
    row = digest.equity_row("2026-09-23", "ADVISORY", db)
    assert row["broker_day_pnl"] is None


def test_equity_change_is_none_on_first_day_not_zero(db):
    """第一天没有前值，就什么都不说。

    绝不能填 `+0.00` —— 那会被读成"今天平盘"，而事实是"不知道"。
    """
    add_equity(db, "2026-08-10", 100_000.0)
    row = digest.equity_row("2026-08-10", "ADVISORY", db)
    assert row["equity_change"] is None
    subject, body = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "当日盈亏" not in body
    assert "+0.00" not in subject


def test_skip_reason_is_translated_for_humans(db):
    """`not_rebalance_day` 进主题没人看得懂，而且「监控日」和「出事了」
    是两件完全不同的事。"""
    add_run(db, skipped="not_rebalance_day")
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "监控日" in subject
    assert "not_rebalance_day" not in subject


def test_no_run_row_is_flagged(db):
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "无运行记录" in subject


def test_fallback_run_used_only_when_store_has_nothing(db):
    """store 是真相源；只有它没记录时才用调用方给的兜底。

    ETL 自己失败的那天 store 是空的，但那恰恰最需要发信说清楚状况。
    """
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db,
                                     fallback_run={"skipped_reason": "not_rebalance_day"})
    assert "监控日" in subject

    add_run(db, submitted=1)
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db,
                                     fallback_run={"skipped_reason": "not_rebalance_day"})
    assert "监控日" not in subject          # store 赢


def test_hard_gate_abort_beats_plan_count(db):
    add_run(db, hard_ok=0)
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "硬闸中止" in subject


def test_order_count_comes_from_caller_not_from_plans(db):
    """`plans` 存的是**目标持仓**，不是订单笔数——3 只目标可能对应 8 笔买卖单。

    早期版本拿 plans 行数当订单数，主题写"订单建议3笔"而日报写 8 笔，
    两个数字互相打架。订单列表 store 里根本没有，只能由调用方叠加。
    """
    add_run(db, submitted=0)
    for code in ("600519.SH", "000858.SZ", "300750.SZ"):
        _insert(db, "INSERT INTO plans VALUES (?,?,?,?)",
                ("2026-08-10", "ADVISORY", code, 0.3))

    # 日循环调用：带上真实的允许订单
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db,
                                     fallback_run={"allowed_orders": [{}] * 8})
    assert "订单建议8笔·未执行" in subject

    # 补发历史：拿不到订单笔数就只说目标持仓，不假装知道要下几笔单
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "目标3只" in subject
    assert "8笔" not in subject


def test_no_orders_but_has_targets_says_no_rebalance_needed(db):
    add_run(db, submitted=0)
    _insert(db, "INSERT INTO plans VALUES (?,?,?,?)",
            ("2026-08-10", "ADVISORY", "600519.SH", 0.3))
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db,
                                     fallback_run={"allowed_orders": []})
    assert "无需调仓" in subject


def test_severity_escalates_subject(db):
    add_run(db)
    _insert(db, "INSERT INTO findings VALUES (?,?,?,?,?,?,?)",
            ("2026-08-10", "ADVISORY", "X", "critical", "标题", "细节", "{}"))
    subject, _ = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "critical" in subject


# ----------------------------------------------------------------------
# 到期的未决假设 —— 复盘 agent 的跨天记忆
# ----------------------------------------------------------------------


def _add_hyp(db, hid, check_after, status="open", mode="ADVISORY"):
    _insert(db, "INSERT INTO hypotheses "
                "(id,mode,opened_date,topic,statement,discriminator,status,check_after,n_observations) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
            (hid, mode, "2026-08-01", "换手过高", "调仓太频繁吃掉了 alpha",
             "把 rebalance_every_days 提到 20 后成本占比应下降一半", status, check_after, 3))


def test_due_hypothesis_appears_with_discriminator(db):
    """不进邮件的话你永远不会主动去查它。判据必须一起带出来——
    一个没有判据的信念不是假设。"""
    add_run(db)
    _add_hyp(db, "HYP-1", "2026-08-05")
    _, body = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "到期的未决假设" in body
    assert "HYP-1" in body
    assert "判据：" in body
    assert "已携带 3 次" in body


def test_future_check_after_is_not_due(db):
    add_run(db)
    _add_hyp(db, "HYP-1", "2026-09-01")
    _, body = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "到期的未决假设" not in body


def test_null_check_after_counts_as_due(db):
    """没写复查日期的开放假设，不提醒就等于永远沉在库里。"""
    add_run(db)
    _add_hyp(db, "HYP-1", None)
    _, body = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "HYP-1" in body


def test_resolved_hypothesis_is_not_due(db):
    add_run(db)
    _add_hyp(db, "HYP-1", "2026-08-05", status="confirmed")
    _, body = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "HYP-1" not in body


def test_hypotheses_are_mode_isolated(db):
    """顾问模式的假设不该出现在实盘的邮件里。"""
    add_run(db, mode="LIVE")
    _add_hyp(db, "HYP-ADV", "2026-08-05", mode="ADVISORY")
    _, body = digest.build_digest("2026-08-10", "LIVE", db_path=db)
    assert "HYP-ADV" not in body


# ----------------------------------------------------------------------
# 内联与降级
# ----------------------------------------------------------------------


def test_inline_strips_duplicate_h1(tmp_path):
    """被内联的文件自带一级标题，不去掉邮件里会出现两个 h1。"""
    f = tmp_path / "r.md"
    f.write_text("# 原标题\n\n正文内容", encoding="utf-8")
    out = "\n".join(digest._inline(f, "日报正文", "缺失"))
    assert out.count("# ") == 1          # 只剩章节自己的 "## 日报正文"
    assert "正文内容" in out
    assert "原标题" not in out


def test_inline_missing_file_leaves_a_note(tmp_path):
    out = "\n".join(digest._inline(None, "自动复盘", "今天没有复盘报告"))
    assert "今天没有复盘报告" in out


def test_inline_unreadable_file_does_not_raise(tmp_path):
    out = "\n".join(digest._inline(tmp_path / "nope.md", "日报正文", "缺失"))
    assert "读取失败" in out or "缺失" in out


def test_inline_empty_file_is_reported(tmp_path):
    f = tmp_path / "empty.md"
    f.write_text("   ", encoding="utf-8")
    assert "文件是空的" in "\n".join(digest._inline(f, "日报正文", "缺失"))


def test_missing_database_still_produces_a_digest(tmp_path):
    """库整个不存在也要发得出信——这正是最该收到邮件的情况。"""
    subject, body = digest.build_digest("2026-08-10", "ADVISORY",
                                        db_path=tmp_path / "nope.db")
    assert "2026-08-10" in subject
    assert body.strip()


def test_corrupt_database_degrades_per_lookup(tmp_path):
    """一张表读不出来不能带走整封邮件。"""
    bad = tmp_path / "bad.db"
    bad.write_bytes(b"not a sqlite file")
    subject, body = digest.build_digest("2026-08-10", "ADVISORY", db_path=bad)
    assert subject and body.strip()


def test_safe_swallows_and_logs():
    assert digest._safe(lambda: 1 / 0, "boom") is None
    assert digest._safe(lambda: 42, "ok") == 42


def test_body_is_markdown_with_expected_sections(db):
    add_run(db)
    add_equity(db, "2026-08-10", 100_000.0)
    _, body = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert body.startswith("# 每日报告")
    assert "| 总资产 |" in body        # 日报缺失 → 重建概览


def test_overview_is_rebuilt_only_when_daily_report_missing(db, tmp_path):
    """日报正文自己就有一份更全的概览。两份并列会让读者看到"两个概览打架"。"""
    add_run(db)
    add_equity(db, "2026-08-10", 100_000.0)

    # 日报缺失 → 重建（这是唯一的账户事实来源）
    _, body = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "日报缺失时的重建" in body

    # 日报存在 → 不重建，交给内联的日报去讲
    write_report(tmp_path)
    _, body = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "日报缺失时的重建" not in body
    assert body.count("## 概览") == 1
    assert "真正的概览在这里" in body


def test_pnl_lines_show_even_when_daily_report_exists(db, tmp_path):
    """净值变化是**跨天**派生的，日报只看当天一天的事实，拿不到这个数。
    所以无论日报在不在，这一段都要有。"""
    add_equity(db, "2026-08-07", 100_000.0)
    add_equity(db, "2026-08-10", 101_000.0)
    add_position(db, "2026-08-10", day_pnl=210.0)
    add_run(db)
    write_report(tmp_path, text="# 日报\n\n内容")
    _, body = digest.build_digest("2026-08-10", "ADVISORY", db_path=db)
    assert "当日盈亏 +210.00" in body
    assert "净值较 2026-08-07 +1,000.00" in body
