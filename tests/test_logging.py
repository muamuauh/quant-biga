"""JSONL 日志的冒烟测试。

logs/qbg.jsonl 是 store 层的真相源（runs.db 每行都能从它重建），
所以"每行都是合法 JSON 且带 ts/level/logger/msg"是个硬约束。
"""

from __future__ import annotations

import json
import logging

from qbg.utils.logging import JsonFormatter


def _format(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def _record(msg: str, **extras) -> logging.LogRecord:
    rec = logging.LogRecord(
        name="qbg.test", level=logging.INFO, pathname=__file__,
        lineno=1, msg=msg, args=(), exc_info=None,
    )
    if extras:
        rec.extras = extras
    return rec


def test_base_fields_present():
    out = _format(_record("ingest.fetch.ok"))
    assert set(out) >= {"ts", "level", "logger", "msg"}
    assert out["msg"] == "ingest.fetch.ok"
    assert out["logger"] == "qbg.test"
    assert out["level"] == "INFO"


def test_timestamp_is_iso_utc():
    out = _format(_record("x"))
    assert out["ts"].endswith("+00:00") or out["ts"].endswith("Z")


def test_extras_are_merged_flat():
    """extras 平铺到顶层，这样 store ETL 可以直接按列取，不用再解一层。"""
    out = _format(_record("ingest.fetch.ok", code="600519.SH", rows=1234))
    assert out["code"] == "600519.SH"
    assert out["rows"] == 1234


def test_chinese_not_escaped():
    """ensure_ascii=False —— 日报和理由都是中文，转义了就没法直接看。"""
    formatted = JsonFormatter().format(_record("gates.st.blocked", reason="ST 股不建仓"))
    assert "ST 股不建仓" in formatted
