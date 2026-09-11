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
"""

from __future__ import annotations

import socket

import pytest

_LOCAL = {"127.0.0.1", "::1", "localhost", "0.0.0.0"}


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """堵死出站网络 + 关掉可选外部服务。"""
    from qbg.config import settings

    # 可选增强一律按"没配"跑。真实 .env 里配了什么，测试不该知道也不该依赖。
    monkeypatch.setattr(settings, "fuyao_api_key", "", raising=False)

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
