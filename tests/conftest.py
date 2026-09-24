"""全局测试夹具。**这里强制「测试必须离线」这条铁律。**

CLAUDE.md 写着"不联网、不碰券商、不调 LLM"，但在 2026-09-11 之前那只是一条
约定 —— 没有任何东西拦得住一次意外的真实请求。

而它真的发生了：同花顺官方数据 API 接进 `daily_cycle` 的那天，
`fuyao.enabled()` 读的是真实 `.env` 里的 key，于是**跑 `run_daily` 的测试会
真的发 HTTP 请求出去**。测试照样全绿，只是慢了几秒 —— 这正是这类问题最难
发现的样子。

所以这里做两件事，两件都是 autouse：

1. **把可选外部服务关掉**，让测试跑在"没配 key"那条路径上。想测已配 key 的
   行为，测试自己 monkeypatch 打开，那是显式的。
2. **把出站网络整个堵死**。任何一次真实连接直接报错，而不是安静地变慢。
   本地回环放行 —— 那是子进程通信，不是"联网"。

外加第三件（2026-09-18 加）：**不许往真相源里写**。见下面 `pytest_configure`。
"""

from __future__ import annotations

import logging
import socket

import pytest

_LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


def _real_log_path():
    from qbg.config import settings
    return (settings.log_dir / "qbg.jsonl").resolve()


def pytest_configure(config):
    """把测试期间的日志从 `logs/qbg.jsonl` 引开。

    那个文件是 store 层的**真相源** —— `backfill()` 逐行重放它来重建 `runs.db`，
    而 `ingest_event` 把每一条 `qbg.*` 事件都写进 `events` 表，`cycle.completed`
    更是直接重建 runs/scores/plans/executions/verdicts。

    2026-09-18 实测：跑一次完整测试往真实日志里追加 **218 行**，其中带
    `date` 字段的有 3 行（`2026-08-10` ×2、`2026-09-18` ×1）。这些是测试构造的
    假数据，长得和真实运行一模一样，回填时分不出来。目前还没有测试产出
    `cycle.completed`，所以派生表侥幸是干净的 —— **但那只是运气**：哪天有个
    测试跑完整的 `daily_cycle`，假数据就直接进 `runs` 了，而且没有任何报错。

    时机必须在 `pytest_configure`：模块级的 `log = get_logger(__name__)` 在**收集
    阶段**就跑了，比任何 fixture 都早。这里抢先把 root handler 配好并置上
    `_configured`，`get_logger` 之后一律早退，真实文件从头到尾不会被打开。

    只挂 stdout，不落文件 —— pytest 会捕获 stdout，测试失败时照样看得到事件。
    """
    from qbg.utils import logging as qbg_logging

    # `_configured` 改名的话这里会静默失效（真实 handler 又挂回去）。宁可炸。
    assert hasattr(qbg_logging, "_configured"), (
        "qbg.utils.logging._configured 不见了 —— 日志隔离依赖它让 get_logger 早退，"
        "改名后必须同步改这里"
    )
    handler = logging.StreamHandler()
    handler.setFormatter(qbg_logging.JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    qbg_logging._configured = True


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """堵死出站网络 + 关掉可选外部服务。"""
    from qbg.config import settings

    # 可选增强一律按"没配"跑。真实 .env 里配了什么，测试不该知道也不该依赖。
    monkeypatch.setattr(settings, "fuyao_api_key", "", raising=False)

    # **行为开关按代码默认值跑。** `settings` 读的是真实 `.env`，所以操作者每改一个
    # 开关，整套测试的前提就跟着变了 —— 2026-09-24 在 `.env` 里打开影子模式的那一刻，
    # 两条"复核全拦应当报警"的测试就红了：它们没做错，是开关替它们改了前提。
    # 想测开着的行为，测试自己 monkeypatch 打开，那是显式的。
    # 新加行为开关时一并加到这里。
    monkeypatch.setattr(settings, "qbg_agents_shadow", 0, raising=False)

    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) and address else address
        if isinstance(host, str) and host not in _LOCAL:
            raise AssertionError(
                f"测试试图连接外网：{host}。测试必须离线（CLAUDE.md §三）——"
                "把数据源 mock 掉，或用 monkeypatch 注入假响应。"
            )
        return real_connect(self, address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def _no_writing_to_the_truth_source(monkeypatch):
    """兜底：任何人试图打开真实的 `logs/qbg.jsonl` 写入，直接报错。

    `pytest_configure` 已经让 `get_logger` 不会打开它了，但那道防线依赖一个私有
    全局 —— 测试要是自己把 `_configured` 重置回 False，或者干脆自己造一个
    `FileHandler`，就绕过去了。和出站网络那道闸一样：**要么拦住，要么响亮地炸**，
    不能安静地写进去。
    """
    real_init = logging.FileHandler.__init__

    def guarded(self, filename, *args, **kwargs):
        from pathlib import Path
        if Path(filename).resolve() == _real_log_path():
            raise AssertionError(
                f"测试试图写入真相源 {filename}。`backfill()` 会把它逐行重放进 "
                "runs.db —— 测试构造的假运行会变成真实历史。写 tmp_path 去。"
            )
        return real_init(self, filename, *args, **kwargs)

    monkeypatch.setattr(logging.FileHandler, "__init__", guarded)
