"""带强制 discriminator 的可证伪假设账本。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from qbg.store.etl import connect


@dataclass(frozen=True)
class Hypothesis:
    id: str
    mode: str
    opened_date: str
    topic: str
    statement: str
    discriminator: str
    status: str = "open"

    def as_dict(self) -> dict:
        return asdict(self)


def open_hypothesis(topic: str, statement: str, discriminator: str, *, opened_date: str,
                    mode: str = "ADVISORY", path: Path | None = None) -> Hypothesis:
    if not statement.strip():
        raise ValueError("statement 不能为空")
    if not discriminator.strip():
        raise ValueError("必须给出 discriminator；无法证伪的信念不是假设")
    db = connect(path)
    # **同一个主题只留一条未决假设。** 复盘 agent 每天独立跑一次，同一个现象会被
    # 反复"发现"：2026-09-17 和 09-18 各开了一条"执行记录一致性"，内容一样、
    # 都 open、观测次数都是 1 —— 看起来像两个独立证据，其实是同一件事记了两遍，
    # 而且那件事本身还是误报。再多几天，未决列表就被同一条噪声灌满了。
    #
    # 命中就加一次观测、把最新一次的陈述覆盖上去，返回原来那条。
    existing = db.execute(
        "SELECT id,mode,opened_date,topic,statement,discriminator,status,n_observations "
        "FROM hypotheses WHERE mode=? AND topic=? AND status='open' ORDER BY opened_date LIMIT 1",
        (mode, topic)).fetchone()
    if existing:
        db.execute("UPDATE hypotheses SET n_observations=n_observations+1,statement=?,"
                   "discriminator=? WHERE id=?",
                   (statement.strip(), discriminator.strip(), existing[0]))
        db.commit()
        db.close()
        return Hypothesis(existing[0], existing[1], existing[2], existing[3],
                          statement.strip(), discriminator.strip())
    prefix = "HYP-" + opened_date.replace("-", "-")
    count = db.execute("SELECT COUNT(*) FROM hypotheses WHERE opened_date=?", (opened_date,)).fetchone()[0]
    item = Hypothesis(f"{prefix}-{count + 1:02d}", mode, opened_date, topic,
                      statement.strip(), discriminator.strip())
    db.execute("INSERT INTO hypotheses (id,mode,opened_date,topic,statement,discriminator,status,"
               "evidence_json) VALUES (?,?,?,?,?,?,?,?)",
               (item.id, item.mode, item.opened_date, item.topic, item.statement,
                item.discriminator, item.status, json.dumps({}, ensure_ascii=False)))
    db.commit()
    db.close()
    return item


def resolve(hypothesis_id: str, status: str, resolution: str,
            *, path: Path | None = None, resolved_date: str) -> None:
    if status not in {"confirmed", "refuted", "abandoned"} or not resolution.strip():
        raise ValueError("关闭假设必须给出合法状态和证据结论")
    db = connect(path)
    db.execute("UPDATE hypotheses SET status=?,resolution=?,resolved_date=? WHERE id=?",
               (status, resolution.strip(), resolved_date, hypothesis_id))
    db.commit()
    db.close()

