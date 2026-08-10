"""零 LLM 运行健康诊断。"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from qbg.config import settings


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    title: str
    detail: str

    def as_dict(self) -> dict:
        return asdict(self)


def diagnose(path: Path | None = None, mode: str = "ADVISORY") -> list[Finding]:
    db_path = path or settings.db_path
    if not db_path.exists():
        return [Finding("STORE_MISSING", "error", "派生库缺失", "运行 14_backfill_store.py --rebuild")]
    findings = []
    with sqlite3.connect(db_path) as db:
        failed = db.execute("SELECT name, reason FROM gates WHERE mode=? AND passed=0", (mode,)).fetchall()
        if failed:
            findings.append(Finding("GATE_FAILURE", "warn", "存在失败风控闸", str(failed[-10:])))
        runs = db.execute("SELECT COUNT(*) FROM runs WHERE mode=?", (mode,)).fetchone()[0]
        if not runs:
            findings.append(Finding("NO_RUNS", "error", "没有运行记录", "检查 daily_cycle 和日志 ETL"))
        invalid = db.execute("SELECT COUNT(*) FROM positions WHERE mode=? AND (qty<0 OR sellable_qty>qty)",
                             (mode,)).fetchone()[0]
        if invalid:
            findings.append(Finding("POSITION_INVALID", "error", "持仓数量不自洽", f"{invalid} 行"))
    return findings

