"""同花顺官方数据 API 客户端。全离线 —— 所有响应都是注入的假数据。

这个模块存在的理由只有一个：**下单时拿到当日真实参考价。**

缓存里最新一根日线是昨天的，拿它当 09:30 的限价参考等于忽略整个隔夜跳空。
2026-09-11 实测踩中：688041 昨收 232.37、卖单限价 231.91，而当天集合竞价定在
228.02、全天区间 225.37~231.50 —— 挂在市价上方，一整天没成交。
"""

from __future__ import annotations

import json
import socket
from io import BytesIO

import pytest

from qbg.config import settings
from qbg.data import fuyao

# ----------------------------------------------------------------------
# 离线守卫本身 —— 它要是不灵，下面所有"离线"测试都是自欺
# ----------------------------------------------------------------------

def test_the_offline_guard_actually_blocks():
    """conftest 的出站网络守卫必须真的拦得住。

    2026-09-11：fuyao 接进 daily_cycle 后，测试会读真实 .env 里的 key 并**真的
    发请求出去** —— 而测试照样全绿，只是慢了几秒。这条断言就是防止守卫哪天被
    改坏之后，这类问题重新变得不可见。
    """
    with pytest.raises(AssertionError, match="测试必须离线"):
        socket.socket().connect(("fuyao.aicubes.cn", 443))


def test_optional_service_is_off_under_tests():
    """测试一律跑在"没配 key"那条路径上，不依赖本机 .env 配了什么。"""
    assert not fuyao.enabled()


# ----------------------------------------------------------------------
# 取价
# ----------------------------------------------------------------------

def _fake_urlopen(monkeypatch, payloads: dict):
    """按 URL 里的路径片段返回假响应。记录请求头，供鉴权断言用。"""
    seen = []

    class _Response:
        def __init__(self, body):
            self._buffer = BytesIO(json.dumps(body).encode("utf-8"))

        def read(self):
            return self._buffer.read()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def fake(request, timeout=None):
        seen.append({"url": request.full_url, "headers": dict(request.headers),
                     "timeout": timeout})
        for fragment, body in payloads.items():
            if fragment in request.full_url:
                return _Response(body)
        raise AssertionError(f"没有为 {request.full_url} 准备假响应")

    monkeypatch.setattr(fuyao.urllib.request, "urlopen", fake)
    monkeypatch.setattr(settings, "fuyao_api_key", "sk-test")
    return seen


def _envelope(items):
    return {"code": 0, "message": "success", "data": {"total": len(items), "item": items}}


def test_live_snapshot_is_preferred(monkeypatch):
    """盘中快照的 last_price 最贴近下单那一瞬间，优先用它。"""
    _fake_urlopen(monkeypatch, {
        "/prices/snapshot": _envelope([
            {"thscode": "688041.SH", "last_price": 230.89, "open_price": 228.02,
             "prev_price": 232.37},
        ]),
    })
    assert fuyao.reference_prices(["688041.SH"]) == {"688041.SH": 230.89}


def test_auction_fills_in_before_continuous_trading(monkeypatch):
    """09:25~09:30 连续竞价还没开始，快照没有当日价，竞价已经定出开盘价。

    那一段时间集合竞价是**唯一**的当日价来源 —— 而那正是日流程下单的时刻。
    """
    _fake_urlopen(monkeypatch, {
        "/prices/snapshot": _envelope([
            {"thscode": "688041.SH", "last_price": 0, "open_price": 0,
             "prev_price": 232.37},
        ]),
        "/auction/snapshot": _envelope([
            {"thscode": "688041.SH", "auction_price": 228.02, "pre_close_price": 232.37},
        ]),
    })
    assert fuyao.reference_prices(["688041.SH"]) == {"688041.SH": 228.02}


def test_yesterday_close_is_never_returned(monkeypatch):
    """**拿不到当日价就返回"没有"，绝不退回昨收。**

    昨收正是这个模块要绕开的东西。在这里悄悄还一个昨收，上层会以为拿到了
    新价并按更窄的让价下单 —— 比没接这个 API 更糟：它会让人放心地挂错价。
    """
    _fake_urlopen(monkeypatch, {
        "/prices/snapshot": _envelope([
            {"thscode": "688041.SH", "last_price": None, "open_price": None,
             "prev_price": 232.37},
        ]),
        "/auction/snapshot": _envelope([]),
    })
    assert fuyao.reference_prices(["688041.SH"]) == {}


def test_api_key_goes_in_the_header_and_timeout_is_set(monkeypatch):
    """鉴权走 X-api-key 头；**必须带超时** —— 没有超时的降级链是死代码。"""
    monkeypatch.setattr(settings, "qbg_net_timeout_sec", 12.5)
    seen = _fake_urlopen(monkeypatch, {
        "/prices/snapshot": _envelope([{"thscode": "600519.SH", "last_price": 1421.0}]),
    })
    fuyao.reference_prices(["600519.SH"])
    assert seen[0]["timeout"] == 12.5
    assert seen[0]["headers"].get("X-api-key") == "sk-test"


# ----------------------------------------------------------------------
# 失败一律退回，绝不抛
# ----------------------------------------------------------------------

@pytest.mark.parametrize("body", [
    {"code": 4001, "message": "rate limited"},          # 限流
    {"code": 0, "data": {}},                            # 没有 item
    {"code": 0, "data": {"item": [{"last_price": 1.0}]}},  # 行里没有 thscode
])
def test_bad_responses_degrade_quietly(monkeypatch, body):
    """限流、空返回、字段缺失 —— 全部退回空字典，让调用方用缓存收盘价。

    **不能抛。** 新接一个外部依赖，不该让它有权让整条下单链停摆。
    """
    _fake_urlopen(monkeypatch, {"snapshot": body})
    assert fuyao.reference_prices(["600519.SH"]) == {}


def test_network_failure_degrades_quietly(monkeypatch):
    def boom(request, timeout=None):
        raise OSError("connection reset")

    monkeypatch.setattr(fuyao.urllib.request, "urlopen", boom)
    monkeypatch.setattr(settings, "fuyao_api_key", "sk-test")
    assert fuyao.reference_prices(["600519.SH"]) == {}


def test_no_key_means_no_request_at_all(monkeypatch):
    """留空 key = 这条增强整个不存在，一个请求都不该发。"""
    def boom(request, timeout=None):
        raise AssertionError("没配 key 却还是发了请求")

    monkeypatch.setattr(fuyao.urllib.request, "urlopen", boom)
    monkeypatch.setattr(settings, "fuyao_api_key", "")
    assert fuyao.reference_prices(["600519.SH"]) == {}


def test_codes_are_batched(monkeypatch):
    """官方限制单次最多 100 个 thscode，超了要分批。"""
    monkeypatch.setattr(settings, "qbg_fuyao_batch", 2)
    codes = [f"60000{i}.SH" for i in range(5)]
    seen = _fake_urlopen(monkeypatch, {
        "/prices/snapshot": _envelope([{"thscode": c, "last_price": 10.0} for c in codes]),
        "/auction/snapshot": _envelope([]),
    })
    fuyao.reference_prices(codes)
    snapshot_calls = [s for s in seen if "/prices/snapshot" in s["url"]]
    assert len(snapshot_calls) == 3, "5 个代码、每批 2 个，应当发 3 次"


def test_module_has_no_order_path():
    """**这个模块永远不下单。** 和 ths_client.py 同一条纪律（CLAUDE.md §七）。

    官方 API 本来就只有数据，但"顺手加一个"是真实存在的诱惑：下单必须走
    execution 层，那里有三把锁和整条风控闸。
    """
    import inspect

    source = inspect.getsource(fuyao)
    for forbidden in ("place_order", "submit", "buy(", "sell(", "/trade"):
        assert forbidden not in source, f"fuyao.py 里出现了 {forbidden}"
