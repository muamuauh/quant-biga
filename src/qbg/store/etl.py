"""JSONL 真相源 → SQLite 派生索引。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from qbg.config import settings

# 给已经存在的表补新列。`CREATE TABLE IF NOT EXISTS` **不会**改动已有的表，
# 所以只改 schema.sql 对现网那份 runs.db 毫无作用 —— 而位置式 INSERT 会因为
# 列数对不上直接报错。（重建整个库不是选项：`hypotheses`/`proposals`/`reviews`
# 是 agent 自己写的，日志里重建不出来。）
_ADDED_COLUMNS = (
    ("positions", "day_pnl", "REAL"),   # 2026-09-21
)


def connect(path: Path | None = None) -> sqlite3.Connection:
    db = sqlite3.connect(path or settings.db_path)
    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
    db.executescript(schema)
    for table, column, decl in _ADDED_COLUMNS:
        existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}  # noqa: S608
        if existing and column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")  # noqa: S608
    db.commit()
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
    elif msg in ("agents.review.verdict", "agents.review.error") and event.get("mode"):
        _ingest_verdict(db, event, run_date, mode)


def _ingest_verdict(db: sqlite3.Connection, event: dict, run_date: str, mode: str) -> None:
    """复核结论**在产出时**就入库，不等日流程读缓存。

    2026-09-20（周日）暴露的口子：盘前复核照跑了（$1.69、5 只候选、保留 3 只），
    但日流程因为非交易日跳过，从没走到读缓存那步 —— 于是 `verdicts` 表当天 0 行，
    复盘 agent 拿到的事实是「candidates=0 kept=0」，**把真花了钱的一次复核报成
    没发生过**。结论只在「日流程读了缓存」这一条路径上入库，那条路径断了就全丢。

    `INSERT OR IGNORE`：日流程随后的 `_ingest_cycle` 带着 `review_source`
    （如 `premarket_cache`）重写同一行，那个值信息量更大，别让这里盖回去 ——
    而重放顺序里复核事件总在 `cycle.completed` 之前。

    **调用点要求事件自带 `mode`，猜不得。** 2026-09-18 之前记的复核事件没有这个
    字段，`ingest_event` 会回退成 `ADVISORY`，而这套部署跑在 PAPER —— 重放会凭空
    造出一整套模式标错的行，和真行一天一天并排躺着（09-18 甚至保留数都不一样：
    ADVISORY 2 只 vs PAPER 3 只）。**缺行是看得见的，标错的行看着像真的。**
    代价是修复前那几天的盘前结论进不来，从下一次复核起自动补齐。
    """
    db.execute("INSERT OR IGNORE INTO verdicts VALUES (?,?,?,?,?,?,?)", (
        run_date, mode, event.get("code"), event.get("rating") or "Error",
        int(bool(event.get("kept"))), event.get("error"), "agents.review"))


def _ingest_cycle(db: sqlite3.Connection, result: dict, run_date: str, mode: str) -> None:
    db.execute("""INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (
        run_date, mode, result.get("started_ts"), result.get("finished_ts"),
        result.get("run_kind"), result.get("skipped_reason"),
        # None（今天没判过择时）存 NULL，别塌成 0 —— 0 会被读成 risk-off。
        None if result.get("market_risk_on") is None else int(bool(result["market_risk_on"])),
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
    # 复核结论。**rationale 不入库**：一条理由 400~600 字，进了派生库既撑大体积，
    # 又会被复盘 agent 原样读进 prompt。要的是"拦了谁、什么评级"，不是长文。
    # **只在日流程真的带着复核结论时才重写。** 「日流程没有结论」不等于
    # 「当天没有结论」：跳过的日子（非交易日 / risk_off）`agent_verdicts` 是空的，
    # 而盘前可能已经花钱复核过、结论由 `_ingest_verdict` 从事件写进来了。
    # 无条件 DELETE 会把它们清掉 —— 重放顺序里复核事件在 `cycle.completed` 之前。
    verdict_rows = result.get("agent_verdicts") or []
    if verdict_rows:
        db.execute("DELETE FROM verdicts WHERE date=? AND mode=?", (run_date, mode))
    source = result.get("review_source")
    for verdict in verdict_rows:
        db.execute("INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?,?,?,?)", (
            run_date, mode, verdict.get("code"), verdict.get("rating"),
            int(bool(verdict.get("kept"))), verdict.get("error"), source))
    db.execute("DELETE FROM gates WHERE date=? AND mode=?", (run_date, mode))
    for gate in result.get("gates", []):
        db.execute("INSERT INTO gates VALUES (?,?,?,?,?)",
                   (run_date, mode, gate["name"], int(gate["passed"]), gate["reason"]))
    account = result.get("account", {})
    db.execute("INSERT OR REPLACE INTO equity VALUES (?,?,?,?)",
               (run_date, mode, account.get("total_equity"), account.get("available_cash")))
    db.execute("DELETE FROM positions WHERE date=? AND mode=?", (run_date, mode))
    for position in result.get("positions", []):
        db.execute(
            "INSERT INTO positions (date,mode,code,name,qty,sellable_qty,"
            "cost_price,last_price,market_value,pnl,day_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                run_date, mode, position.get("code"), position.get("name"), position.get("qty"),
                position.get("sellable_qty"), position.get("cost_price"),
                position.get("last_price"), position.get("market_value"),
                position.get("pnl"), position.get("day_pnl")))


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
              for table in ("runs", "scores", "plans", "executions", "gates", "equity",
                            "positions", "events")}
    db.close()
    return {"read": read, "invalid": invalid, "counts": counts}
