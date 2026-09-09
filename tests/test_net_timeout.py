"""网络超时。全离线：用本地 socket 造一个"接受连接但永不回复"的服务端。

这一层要挡的是 2026-09-09 那次卡死：日流程停了 10 分钟没有任何进展，
进程只有一条 TCP 连接、CPU 累计 1.7 秒 —— 请求发出去了对端不回，而整条
调用链上**一个超时都没有**。

**没有超时的降级链是死代码。** `fetch_index_constituents` 的 fail-soft 分支、
`SourceChain` 的"换下一个源"，都只挡得住"立刻报错"，挡不住"永远不回"。
"""

from __future__ import annotations

import socket
import threading

import pytest

from qbg.utils.net import guarded, net_timeout


def _silent_server():
    """接受连接然后什么都不发。模拟"对端不回"这个真实故障。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    held = []

    def accept_and_hold():
        try:
            conn, _ = srv.accept()
            held.append(conn)      # 握住不放，也不发数据
        except OSError:
            pass

    t = threading.Thread(target=accept_and_hold, daemon=True)
    t.start()
    return srv, held


def test_timeout_actually_fires_on_a_silent_peer():
    """核心断言：对端不回时必须抛出来，而不是永远等下去。"""
    srv, _held = _silent_server()
    try:
        with net_timeout(0.3):
            s = socket.create_connection(srv.getsockname())
            with pytest.raises((TimeoutError, socket.timeout, OSError)):
                s.recv(1)
            s.close()
    finally:
        srv.close()


def test_previous_timeout_is_restored_even_on_exception():
    """块内抛异常也要还原。

    不还原的话，一次数据源失败会把超时**留给整个进程后续的网络代码** ——
    一个安静的、跨模块的行为改变。
    """
    socket.setdefaulttimeout(None)
    with pytest.raises(ValueError):
        with net_timeout(1.0):
            assert socket.getdefaulttimeout() == 1.0
            raise ValueError("boom")
    assert socket.getdefaulttimeout() is None


def test_nested_blocks_restore_the_outer_value():
    socket.setdefaulttimeout(None)
    with net_timeout(5.0):
        with net_timeout(1.0):
            assert socket.getdefaulttimeout() == 1.0
        assert socket.getdefaulttimeout() == 5.0
    assert socket.getdefaulttimeout() is None


@pytest.mark.parametrize("value", [0, -1, 0.0])
def test_non_positive_means_no_limit(value):
    """0 或负数 = 退回旧行为（不设限），留给"我就是要等"的场景。"""
    socket.setdefaulttimeout(None)
    with net_timeout(value):
        assert socket.getdefaulttimeout() is None


def test_guarded_returns_default_and_does_not_raise():
    """`guarded` 是给"拿不到就降级"的元数据调用用的，不该把异常漏出去。"""
    def boom():
        raise ConnectionError("对端不回")

    assert guarded(boom, default=["fallback"], what="test") == ["fallback"]


def test_guarded_passes_through_the_return_value():
    assert guarded(lambda a, b=0: a + b, 1, b=2, default=None) == 3


# ----------------------------------------------------------------------
# 接线：光有模块没用，调用点得真的用上
# ----------------------------------------------------------------------

@pytest.mark.parametrize("module_path", [
    "src/qbg/data/universe.py",
    "src/qbg/data/sources/chain.py",
    "src/qbg/data/industry.py",
    "src/qbg/data/meta.py",
])
def test_network_call_sites_use_the_timeout(module_path):
    """本项目已经被"参数存在但没人读"坑过五次，这是第六个同形状的。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / module_path).read_text(encoding="utf-8")
    assert "net_timeout" in src, f"{module_path} 没有用 net_timeout —— 降级链仍是死代码"


def test_chain_wraps_fetch_not_just_imports_it():
    """`SourceChain.fetch` 是行情主链路的单一收口点，必须真的包住那一句。"""
    import inspect

    from qbg.data.sources.chain import SourceChain

    src = inspect.getsource(SourceChain.fetch)
    assert "net_timeout" in src, "SourceChain.fetch 没有套超时"
    assert src.index("net_timeout") < src.index("src.fetch("), \
        "超时必须包在 src.fetch 外面，包在后面等于没包"
