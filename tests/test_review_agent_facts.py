"""复盘 agent 拿到的事实、确定性健康检查、假设去重。全离线。

这三件事是 2026-09-18 一次实际复盘失灵查出来的：

  · 09-16/17 复盘 agent 连着两天报"submitted=1 但 executions 为空，需人工核实"，
    而那两天真实是 **0 目标 0 订单** —— `runs.submitted` 是布尔值，被读成了条数。
  · 同期账户**连续三天满仓现金**（逐票复核 0/5 全拦），agent 写的是"全部正常"：
    复核结论既没进派生库、也没进事实包，它根本看不见。
  · 那条误报还每天新开一条同名假设，未决列表开始被同一个噪声灌满。
"""

from __future__ import annotations

import sqlite3

import pytest

from qbg.agent.review import collect_facts
from qbg.analysis.health import IDLE_CASH_DAYS, diagnose
from qbg.analysis.hypotheses import open_hypothesis
from qbg.store.etl import _ingest_cycle, connect

MODE = "PAPER"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "runs.db"
    connect(path).close()
    return path


def _cycle(path, date, **over):
    result = {"date": date, "mode": MODE, "run_kind": "rebalance", "hard_ok": True,
              "submitted": True, "market_risk_on": True, "orders": [], "allowed_orders": [],
              "targets": {}, "scores": [], "gates": [], "positions": [],
              "account": {"total_equity": 100_000.0, "available_cash": 100_000.0},
              "agent_verdicts": [], "review_source": "premarket_cache"}
    result.update(over)
    db = connect(path)
    _ingest_cycle(db, result, date, MODE)
    db.commit()
    db.close()
    return result


def _verdict(code, rating, kept):
    return {"code": code, "rating": rating, "kept": kept, "rationale": "x" * 500, "error": None}


# ----------------------------------------------------------------------
# 复核结论进库
# ----------------------------------------------------------------------

def test_verdicts_are_stored(db_path):
    _cycle(db_path, "2026-09-16", agent_verdicts=[
        _verdict("600000.SH", "Underweight", False), _verdict("600519.SH", "Buy", True)])
    with sqlite3.connect(db_path) as db:
        rows = db.execute("SELECT code,rating,kept,source FROM verdicts ORDER BY code").fetchall()
    assert rows == [("600000.SH", "Underweight", 0, "premarket_cache"),
                    ("600519.SH", "Buy", 1, "premarket_cache")]


def test_rationale_is_not_stored(db_path):
    """理由正文 400~600 字/条。进了库就会被原样读进 prompt，撑爆上下文。"""
    _cycle(db_path, "2026-09-16", agent_verdicts=[_verdict("600000.SH", "Sell", False)])
    with sqlite3.connect(db_path) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(verdicts)")}
    assert "rationale" not in columns


def test_facts_expose_the_review_gate(db_path):
    """**这是 agent 此前完全看不见的那一项。**"""
    _cycle(db_path, "2026-09-16", agent_verdicts=[
        _verdict("A.SH", "Underweight", False), _verdict("B.SH", "Sell", False)])
    _, facts = collect_facts("2026-09-16", mode=MODE, path=db_path)
    assert facts["review"]["candidates"] == 2
    assert facts["review"]["kept"] == 0
    assert facts["review"]["blocked_all"] is True


# ----------------------------------------------------------------------
# submitted 的歧义
# ----------------------------------------------------------------------

def test_submitted_is_not_presented_as_a_count(db_path):
    """`submitted` 是"顾问清单已写盘"的布尔值。留着这个名字给 LLM，它会读成
    "提交了 1 笔" —— 09-16/17 的误报就是这么来的。"""
    _cycle(db_path, "2026-09-16")
    _, facts = collect_facts("2026-09-16", mode=MODE, path=db_path)
    run = facts["run"][0]
    assert "submitted" not in run, "含糊的字段名不该出现在喂给模型的事实里"
    assert run["advisory_sheet_written"] is True
    assert run["n_orders"] == 0 and run["n_orders_allowed"] == 0 and run["n_targets"] == 0


def test_order_counts_are_real(db_path):
    order = {"code": "600000.SH", "side": "BUY", "quantity": 100, "price": 10.0,
             "ref_price": 10.0, "estimated_fee": 1.0, "reason": "test"}
    _cycle(db_path, "2026-09-18", orders=[order], allowed_orders=[order],
           targets={"600000.SH": 0.33})
    _, facts = collect_facts("2026-09-18", mode=MODE, path=db_path)
    run = facts["run"][0]
    assert (run["n_orders"], run["n_orders_allowed"], run["n_targets"]) == (1, 1, 1)


# ----------------------------------------------------------------------
# 确定性健康检查
# ----------------------------------------------------------------------

def test_review_blocking_everything_is_a_finding(db_path):
    _cycle(db_path, "2026-09-16", agent_verdicts=[
        _verdict("A.SH", "Underweight", False), _verdict("B.SH", "Sell", False)])
    codes = {f.code for f in diagnose(db_path, MODE)}
    assert "REVIEW_BLOCKED_ALL" in codes


def test_review_keeping_something_is_not_a_finding(db_path):
    _cycle(db_path, "2026-09-18", agent_verdicts=[
        _verdict("A.SH", "Underweight", False), _verdict("B.SH", "Buy", True)])
    codes = {f.code for f in diagnose(db_path, MODE)}
    assert "REVIEW_BLOCKED_ALL" not in codes


def test_consecutive_blocking_is_an_error_not_a_warning(db_path):
    for day in ("2026-09-15", "2026-09-16", "2026-09-17"):
        _cycle(db_path, day, agent_verdicts=[_verdict("A.SH", "Sell", False)])
    hit = next(f for f in diagnose(db_path, MODE) if f.code == "REVIEW_BLOCKED_ALL")
    assert hit.severity == "error"
    assert "连续 3 天" in hit.detail


def test_idle_cash_needs_more_than_one_day(db_path):
    """卖出当天钱还没投出去是正常的。"""
    _cycle(db_path, "2026-09-15")
    assert "IDLE_CASH" not in {f.code for f in diagnose(db_path, MODE)}


def test_consecutive_idle_cash_is_a_finding(db_path):
    """**2026-09-15~17 那三天。** 当时四条检查一条都不覆盖，于是复盘写"全部正常"。"""
    for day in ("2026-09-15", "2026-09-16", "2026-09-17"):
        _cycle(db_path, day)
    hit = next(f for f in diagnose(db_path, MODE) if f.code == "IDLE_CASH")
    assert "连续 3" in hit.title
    assert "2026-09-15" in hit.detail, "要说清从哪天起空着"


def test_idle_cash_streak_counts_back_from_today_only(db_path):
    """关心的是"现在还空着吗"，不是历史上空过几天。"""
    for day in ("2026-09-14", "2026-09-15"):
        _cycle(db_path, day)
    _cycle(db_path, "2026-09-18",
           account={"total_equity": 100_000.0, "available_cash": 5_000.0})
    assert "IDLE_CASH" not in {f.code for f in diagnose(db_path, MODE)}


def test_a_day_that_bought_is_not_idle(db_path):
    """**2026-09-18 实测。** 权益快照是开盘前读的，当天的买单还没成交，现金看着仍是
    100% —— 复核明明已经恢复、钱正在出去，却照样被算进"连续满仓现金"。"""
    order = {"code": "600000.SH", "side": "BUY", "quantity": 100, "price": 10.0,
             "ref_price": 10.0, "estimated_fee": 1.0, "reason": "test"}
    for day in ("2026-09-16", "2026-09-17"):
        _cycle(db_path, day)
    _cycle(db_path, "2026-09-18", orders=[order], allowed_orders=[order],
           targets={"600000.SH": 0.33})
    assert "IDLE_CASH" not in {f.code for f in diagnose(db_path, MODE)}


def test_a_day_whose_buys_were_all_blocked_is_still_idle(db_path):
    """买单被风控闸砍光 ≠ 钱在出去。这种日子恰恰是最该报的。"""
    order = {"code": "600000.SH", "side": "BUY", "quantity": 100, "price": 10.0,
             "ref_price": 10.0, "estimated_fee": 1.0, "reason": "test"}
    for day in ("2026-09-16", "2026-09-17"):
        _cycle(db_path, day)
    _cycle(db_path, "2026-09-18", orders=[order], allowed_orders=[],
           targets={"600000.SH": 0.33})
    assert "IDLE_CASH" in {f.code for f in diagnose(db_path, MODE)}


def test_a_day_that_only_sold_is_still_idle(db_path):
    """只卖不买，现金只会更多。"""
    order = {"code": "600000.SH", "side": "SELL", "quantity": 100, "price": 10.0,
             "ref_price": 10.0, "estimated_fee": 1.0, "reason": "test"}
    for day in ("2026-09-16", "2026-09-17"):
        _cycle(db_path, day)
    _cycle(db_path, "2026-09-18", orders=[order], allowed_orders=[order])
    assert "IDLE_CASH" in {f.code for f in diagnose(db_path, MODE)}


def test_idle_cash_threshold_is_at_least_two_days():
    assert IDLE_CASH_DAYS >= 2


def test_diagnose_survives_a_db_without_verdicts(tmp_path):
    """老库没有新表。诊断不该因为缺一张表就整个抛出去。"""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE runs (date TEXT, mode TEXT)")
        db.execute("CREATE TABLE gates (name TEXT, mode TEXT, passed INT, reason TEXT)")
        db.execute("CREATE TABLE positions (mode TEXT, qty INT, sellable_qty INT)")
        db.execute("CREATE TABLE equity (date TEXT, mode TEXT, total_equity REAL, "
                   "available_cash REAL)")
    assert isinstance(diagnose(path, MODE), list)


# ----------------------------------------------------------------------
# 假设去重
# ----------------------------------------------------------------------

def test_same_topic_increments_instead_of_piling_up(db_path):
    """09-17 和 09-18 各开了一条"执行记录一致性" —— 同一件事记了两遍，
    看起来像两个独立证据。"""
    first = open_hypothesis("执行记录一致性", "订单已成交但未写入派生索引",
                            "核对券商执行日志", opened_date="2026-09-17",
                            mode=MODE, path=db_path)
    again = open_hypothesis("执行记录一致性", "订单已成交但未写入派生索引（第二次观测）",
                            "核对券商执行日志", opened_date="2026-09-18",
                            mode=MODE, path=db_path)
    assert again.id == first.id
    with sqlite3.connect(db_path) as db:
        rows = db.execute("SELECT id,n_observations,statement FROM hypotheses").fetchall()
    assert len(rows) == 1
    assert rows[0][1] == 2, "应当加观测次数"
    assert "第二次观测" in rows[0][2], "陈述应当更新到最新一次"


def test_a_different_topic_still_opens_a_new_one(db_path):
    open_hypothesis("执行记录一致性", "a", "b", opened_date="2026-09-17",
                    mode=MODE, path=db_path)
    other = open_hypothesis("复核过严", "min_rating 过严导致空仓", "统计保留率",
                            opened_date="2026-09-18", mode=MODE, path=db_path)
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0] == 2
    assert other.topic == "复核过严"


def test_a_resolved_hypothesis_does_not_block_a_new_one(db_path):
    """已经结掉的假设不该挡住同主题的新假设 —— 那是新一轮观测。"""
    from qbg.analysis.hypotheses import resolve

    first = open_hypothesis("复核过严", "a", "b", opened_date="2026-09-17",
                           mode=MODE, path=db_path)
    resolve(first.id, "refuted", "复核保留率回到 3/5", path=db_path,
            resolved_date="2026-09-18")
    again = open_hypothesis("复核过严", "a2", "b2", opened_date="2026-09-19",
                            mode=MODE, path=db_path)
    assert again.id != first.id
