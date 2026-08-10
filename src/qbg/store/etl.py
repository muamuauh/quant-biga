"""JSONL 真相源 → SQLite 派生索引。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from qbg.config import settings


def connect(path: Path | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or settings.db_path)
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    db.executescript(schema)
    return db


def ingest_event(db: sqlite3.Connection, event: dict) -> None:
    ts, logger, msg = str(event.get("ts", "")), str(event.get("logger", "")), str(event.get("msg", ""))
    if not logger.startswith("qbg."):
        return
    run_date = str(event.get("date") or ts[:10])
    mode = str(event.get("mode") or "ADVISORY").upper()
    payload_json = json.dumps(event, ensure_ascii=False, sort_keys=True)
    db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)",
               (ts, logger, msg, event.get("level"), run_date, mode, payload_json))
    if msg == "cycle.completed":
        _ingest_cycle(db, event, run_date, mode)


def _ingest_cycle(db: sqlite3.Connection, result: dict, run_date: str, mode: str) -> None:
    db.execute("""INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
        run_date, mode, result.get("started_ts"), result.get("finished_ts"),
        result.get("run_kind"), result.get("skipped_reason"), int(bool(result.get("market_risk_on"))),
        int(bool(result.get("submitted"))), int(bool(result.get("hard_ok"))),
        result.get("report_path"), json.dumps(result, ensure_ascii=False, sort_keys=True)))
    db.execute("DELETE FROM scores WHERE date=? AND mode=?", (run_date, mode))
    for rank, item in enumerate(result.get("scores", []), 1):
        db.execute("INSERT INTO scores VALUES (?,?,?,?,?)",
                   (run_date, mode, item["code"], item["score"], rank))
    db.execute("DELETE FROM plans WHERE date=? AND mode=?", (run_date, mode))
    for code, weight in result.get("targets", {}).items():
        db.execute("INSERT INTO plans VALUES (?,?,?,?)", (run_date, mode, code, weight))
    db.execute("DELETE FROM executions WHERE date=? AND mode=?", (run_date, mode))
    allowed = {(o["code"], o["side"]) for o in result.get("allowed_orders", [])}
    for order in result.get("orders", []):
        db.execute("INSERT INTO executions VALUES (?,?,?,?,?,?,?,?,?,?)", (
            run_date, mode, order["code"], order["side"], order["quantity"], order["price"],
            order.get("ref_price"), order.get("estimated_fee"),
            int((order["code"], order["side"]) in allowed), order.get("reason")))
    db.execute("DELETE FROM gates WHERE date=? AND mode=?", (run_date, mode))
    for gate in result.get("gates", []):
        db.execute("INSERT INTO gates VALUES (?,?,?,?,?)",
                   (run_date, mode, gate["name"], int(gate["passed"]), gate["reason"]))
    account = result.get("account", {})
    db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?)",
               (run_date, mode, account.get("total_equity"), account.get("available_cash")))
    db.execute("DELETE FROM positions WHERE date=? AND mode=?", (run_date, mode))
    for position in result.get("positions", []):
        db.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
            run_date, mode, position.get("code"), position.get("name"), position.get("qty"),
            position.get("sellable_qty"), position.get("cost_price"), position.get("last_price"),
            position.get("market_value"), position.get("pnl")))


def backfill(log_path: Path | None = None, db_path: Path | None = None) -> dict:
    source = log_path or settings.log_dir / "qbg.jsonl"
    db = connect(db_path)
    read, invalid = 0, 0
    if source.exists():
        for line in source.read_text(encoding="utf-8").splitlines():
            try:
                ingest_event(db, json.loads(line))
                read += 1
            except (json.JSONDecodeError, KeyError, TypeError, sqlite3.Error):
                invalid += 1
    db.commit()
    counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
              for table in ("runs", "scores", "plans", "executions", "gates", "events")}
    db.close()
    return {"read": read, "invalid": invalid, "counts": counts}

