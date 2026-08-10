"""结构化 JSON 日志，同时写 stdout 和 logs/qbg.jsonl。

移植自 quant-trading/src/qtf/utils/logging.py（原样，与市场无关）。

为什么是 JSONL 而不是普通文本：这个文件是 store 层的**真相源**——
data/runs.db 里的每一行都能从这里重建（见 scripts/14_backfill_store.py）。
可解析比可读更重要。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from qbg.config import settings


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extras = getattr(record, "extras", None)
        if extras:
            payload.update(extras)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


_configured = False


def get_logger(name: str) -> logging.Logger:
    """取一个 logger，首次调用时配置 root handler。

    注意 `root.handlers.clear()`：第三方库（akshare/baostock/openai/httpx）会往
    root 上挂自己的 handler，不清掉的话日志文件里会混入大量噪声，让后续的
    store ETL 很难过滤。代价是这些库的日志也只会以 JSON 形式出现。
    """
    global _configured
    logger = logging.getLogger(name)
    if _configured:
        return logger

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    log_path: Path = settings.log_dir / "qbg.jsonl"

    handler_stream = logging.StreamHandler(sys.stdout)
    handler_stream.setFormatter(JsonFormatter())

    handler_file = logging.FileHandler(log_path, encoding="utf-8")
    handler_file.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler_stream)
    root.addHandler(handler_file)
    root.setLevel(logging.INFO)

    _configured = True
    return logger


def log_event(logger: logging.Logger, msg: str, **extras: Any) -> None:
    """发一条带结构化字段的 INFO 日志。

    `msg` 用 `阶段.步骤[.状态]` 的点分命名（如 `ingest.fetch.ok`、
    `gates.price_limit.blocked`），这样 store 层可以按前缀聚合。
    """
    logger.info(msg, extra={"extras": extras})
