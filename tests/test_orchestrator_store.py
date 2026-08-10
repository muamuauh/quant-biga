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
    assert "订单摘要" in text and "mode_guard" in text and "生存者偏差" in text


def test_backfill_rebuild_is_idempotent_and_mode_isolated(tmp_path):
    log = tmp_path / "qbg.jsonl"
    events = []
    for mode in ("ADVISORY", "PAPER"):
        events.append({"ts": f"2026-08-10T10:00:0{len(events)}+00:00", "level": "INFO",
                       "logger": "qbg.orchestrator.daily_cycle", "msg": "cycle.completed",
                       "date": "2026-08-10", "mode": mode, "hard_ok": True,
                       "market_risk_on": True, "targets": {"600519.SH": .3},
                       "scores": [{"code": "600519.SH", "score": .1}], "orders": [],
                       "allowed_orders": [], "gates": [], "account": {}, "positions": []})
    log.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")
    db = tmp_path / "runs.db"
    first, second = backfill(log, db), backfill(log, db)
    assert first["counts"] == second["counts"]
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM plans").fetchone()[0] == 2
