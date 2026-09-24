"""零 LLM 运行健康诊断。"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

from qbg.config import settings

# 「几乎满仓现金」的判据。持仓清空时现金占比就是 100%，0.95 留给零股和费用尾差。
IDLE_CASH_RATIO = 0.95
# 连续几个运行日满仓现金才算异常。
#
# 1 天是正常的：卖出当天钱还没投出去。**2 天就不正常了** —— `QBG_REBALANCE_CASH_TRIGGER`
# 是 0.50，现金超过一半的那天就会被当成调仓日，也就是说系统每天都在尝试建仓却建不起来。
# 2026-09-15~17 连续三天满仓现金（复核 0/5 全拦），而当时的诊断只有 4 条检查、
# 一条都不覆盖这种情况，于是复盘 agent 每天都写"运行健康：全部正常"。
IDLE_CASH_DAYS = 2


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    """老库可能没有新表。诊断不该因为缺一张表就整个抛出去。"""
    return bool(db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone())


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
        findings.extend(_idle_cash(db, mode))
        findings.extend(_review_blocked(db, mode))
    return findings


def _idle_cash(db: sqlite3.Connection, mode: str) -> list[Finding]:
    """连续几个运行日几乎满仓现金 —— 钱没在工作，而且没人会注意到。

    从最近一天往回数，断了就停：关心的是"现在还空着吗"，不是历史上空过几天。
    """
    rows = db.execute(
        "SELECT date,total_equity,available_cash FROM equity WHERE mode=? "
        "ORDER BY date DESC LIMIT 30", (mode,)).fetchall()
    streak, since = 0, None
    for day, total, cash in rows:
        if not total or cash is None or cash / total < IDLE_CASH_RATIO:
            break
        streak, since = streak + 1, day
    if streak < IDLE_CASH_DAYS:
        return []
    # **建仓当天不算闲置。** 权益快照是开盘前从券商读的，当天的买单还没成交，
    # 现金占比看着仍是 100%。2026-09-18 就是这样：复核恢复到 3/5、真下了 3 笔买单，
    # 却照样被算进连续天数。明天的快照见到持仓，streak 自己会断 —— 也就是说这条
    # 告警只会在"问题正在解决的那一天"多响一次，而那一响足够让复盘 agent 开一条
    # 无谓的假设（它每天独立跑，看不出这是昨天那件事的尾巴）。
    if rows and _bought_on(db, mode, rows[0][0]):
        return []
    return [Finding("IDLE_CASH", "warn", f"连续 {streak} 个运行日几乎满仓现金",
                    f"自 {since} 起现金占比 ≥ {IDLE_CASH_RATIO:.0%}；"
                    "现金触发阈值是 50%，说明系统每天都在尝试建仓但没买成 —— "
                    "查逐票复核是不是把候选全拦了，或可负担性过滤是不是把票筛空了")]


def _bought_on(db: sqlite3.Connection, mode: str, day: str) -> bool:
    """当天有没有放行的买单 —— 有的话现金是正在出去，不是闲着。"""
    return bool(db.execute(
        "SELECT 1 FROM executions WHERE date=? AND mode=? AND side='BUY' AND allowed=1 LIMIT 1",
        (day, mode)).fetchone())


def _review_blocked(db: sqlite3.Connection, mode: str) -> list[Finding]:
    """逐票复核把当天候选**全部**拦下 —— 那天一定不会有任何买入。

    **影子模式下这条不成立**：全拦也照样按模型排名买，报出来就是一条
    "当天不会有任何买入"的假告警。
    """
    if settings.qbg_agents_shadow or not _has_table(db, "verdicts"):
        return []
    rows = db.execute(
        "SELECT date, COUNT(*), COALESCE(SUM(kept),0) FROM verdicts WHERE mode=? "
        "GROUP BY date ORDER BY date DESC LIMIT 30", (mode,)).fetchall()
    streak, total, since = 0, 0, None
    for day, n, kept in rows:
        if not n or kept:
            break
        streak, total, since = streak + 1, n, day
    if not streak:
        return []
    detail = (f"{since} 复核 0/{total} 保留" if streak == 1 else
              f"连续 {streak} 天全部拦下（最近一天 0/{total}，自 {since}）")
    return [Finding("REVIEW_BLOCKED_ALL", "warn" if streak == 1 else "error",
                    "逐票复核拦下全部候选", detail +
                    "；当天不会有任何买入。连续出现要看 QBG_AGENTS_MIN_RATING 是否过严")]

