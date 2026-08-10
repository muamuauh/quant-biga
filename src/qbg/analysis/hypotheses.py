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

