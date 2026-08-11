from __future__ import annotations

import json
import sqlite3

from qbg.orchestrator import run_marker
from qbg.report.daily_report import render
from qbg.store.etl import backfill


def test_marker_and_rebalance_schedule(tmp_path):
    marker = tmp_path / "marker.json"
    assert not run_marker.already_completed_today("2026-08-10", marker)
    run_marker.save_marker(["a.csv"], "2026-08-10", marker)
    assert run_marker.already_completed_today("2026-08-10", marker)
    assert not run_marker.already_completed_today("2026-08-11", marker)


def test_daily_report_contains_orders_and_risk():
    text = render({"date": "2026-08-10", "mode": "ADVISORY", "market_risk_on": True,
                   "targets": {"a": .3}, "allowed_orders": [{}],
                   "orders": [{"code": "a", "side": "BUY", "quantity": 100,
                               "price": 10.0, "reason": "test"}],
                   "gates": [{"name": "mode_guard", "passed": True, "reason": "ok"}]})
    assert "订单意见" in text and "mode_guard" in text and "生存者偏差" in text
    assert "没有向券商提交订单" in text


def test_daily_report_follows_mail_summary_order():
    text = render({
        "date": "2026-08-10",
        "mode": "ADVISORY",
        "market_risk_on": True,
        "account": {"total_equity": 100_000, "available_cash": 80_000},
        "positions": [],
        "targets": {"600519.SH": .3},
        "orders": [],
        "allowed_orders": [],
        "gates": [{"name": "mode_guard", "passed": True, "reason": "ok"}],
    })
    headings = [
        "## 概览",
        "## 一句话结论",
        "## 账户与持仓",
        "## 量化选择与 TradingAgents 复核",
        "## 订单意见",
        "## 运行健康",
        "## 建议与限制",
    ]
    assert text.startswith("# 复盘 · 2026-08-10")
    assert [text.index(heading) for heading in headings] == sorted(
        text.index(heading) for heading in headings
    )
    assert "1/1 项通过" in text


def test_daily_report_contains_agent_review_status():
    text = render({"date": "2026-08-10", "daily_review": {
        "ok": True, "summary": "运行正常", "report_path": "reports/review/2026-08-10.md",
    }})
    assert "自动复盘" in text and "运行正常" in text


def test_daily_report_contains_account_positions_and_tradingagents_gate():
    text = render({
        "date": "2026-08-10",
        "mode": "ADVISORY",
        "account": {"total_equity": 40_507.37, "available_cash": 15_587.77},
        "positions": [{
            "code": "600586.SH", "name": "金晶科技", "qty": 400, "sellable_qty": 400,
            "cost_price": 11.578, "last_price": 4.21, "market_value": 1684, "pnl": -2952.96,
        }],
        "agent_verdicts": [{
            "code": "600519.SH", "rating": "Hold", "kept": True,
            "rationale": "量化候选达到最低评级。", "error": None,
        }],
        "agent_usage": {"calls": 16, "total_tokens": 70_429, "cost_usd": 0.0792},
    })
    assert "¥40,507.37" in text and "金晶科技" in text
    assert "TradingAgents 复核闸" in text and "600519.SH" in text and "70,429" in text


def test_backfill_rebuild_is_idempotent_and_mode_isolated(tmp_path):
    log = tmp_path / "qbg.jsonl"
    events = []
    for mode in ("ADVISORY", "PAPER"):
        events.append({"ts": f"2026-08-10T10:00:0{len(events)}+00:00", "level": "INFO",
                       "logger": "qbg.orchestrator.daily_cycle", "msg": "cycle.completed",
                       "date": "2026-08-10", "mode": mode, "hard_ok": True,
                       "market_risk_on": True, "targets": {"600519.SH": .3},
                       "scores": [{"code": "600519.SH", "score": .1}], "orders": [],
                       "allowed_orders": [], "gates": [],
                       "account": {"total_equity": 100_000, "available_cash": 80_000},
                       "positions": [{
                           "code": "600519.SH", "name": "贵州茅台", "qty": 100,
                           "sellable_qty": 100, "cost_price": 1500, "last_price": 1600,
                           "market_value": 160_000, "pnl": 10_000,
                       }]})
    log.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    db = tmp_path / "runs.db"
    first, second = backfill(log, db), backfill(log, db)
    assert first["counts"] == second["counts"]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM equity").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 2
