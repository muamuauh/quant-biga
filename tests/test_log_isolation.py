"""钉住「测试不许写真相源」这道闸本身。

`logs/qbg.jsonl` 是 store 层的真相源：`backfill()` 逐行重放它来重建 `runs.db`。
测试往里写 = 测试构造的假运行变成真实历史，而且**没有任何报错**。

2026-09-18 实测：加这道闸之前，跑一次完整测试往真实日志追加 218 行，
其中 3 行带真实格式的 `date`。当时派生表还是干净的，只因为没有测试产出
`cycle.completed` —— 那是运气，不是设计。

和 `test_fuyao.py::test_the_offline_guard_actually_blocks` 同一个道理：
**闸本身也要有测试**，否则哪天它静默失效了没人知道。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from qbg.config import settings
from qbg.utils.logging import get_logger, log_event

REAL_LOG = (settings.log_dir / "qbg.jsonl").resolve()


def test_no_handler_points_at_the_real_log():
    """收集阶段模块级的 `get_logger` 已经跑过了 —— 那时就不该挂上真实文件。"""
    for handler in logging.getLogger().handlers:
        target = getattr(handler, "baseFilename", None)
        assert target is None or Path(target).resolve() != REAL_LOG, (
            f"root logger 上挂着指向真相源的 handler：{target}")


def test_logging_an_event_does_not_touch_the_real_log(capfd):
    """真正的验收：发一条事件，真实文件不许变，而事件仍然看得到。"""
    before = REAL_LOG.stat().st_size if REAL_LOG.exists() else None

    log_event(get_logger("qbg.test.isolation"), "test.isolation.probe", marker="ZZ-probe")

    after = REAL_LOG.stat().st_size if REAL_LOG.exists() else None
    assert after == before, "事件被写进了真相源"

    # 事件本身没丢 —— 走标准流，测试失败时照样看得到。
    out, err = capfd.readouterr()
    events = [json.loads(line) for line in (out + err).splitlines()
              if line.startswith("{")]
    assert any(e.get("marker") == "ZZ-probe" for e in events), (
        "事件既没进文件也没进标准流 —— 那是把日志弄丢了，不是隔离")


def test_the_guard_actually_blocks_a_filehandler():
    """绕过 `get_logger` 直接造 FileHandler 也要被拦。"""
    with pytest.raises(AssertionError, match="真相源"):
        logging.FileHandler(REAL_LOG, encoding="utf-8")


def test_the_guard_still_allows_other_files(tmp_path):
    """别拦过头 —— 写 tmp_path 是正当的。"""
    target = tmp_path / "qbg.jsonl"
    handler = logging.FileHandler(target, encoding="utf-8")
    handler.close()
    assert target.exists()


def test_get_logger_stays_early_exiting():
    """`_configured` 是隔离的支点。有人把它重置了，这条会红。"""
    from qbg.utils import logging as qbg_logging
    assert qbg_logging._configured is True
