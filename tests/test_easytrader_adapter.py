"""P9b 下单适配器的离线测试。

**不联网、不碰券商。** `place_order` 和当日委托回读整个被替换掉，
所以这些测试在没有同花顺的机器上也能跑。

委托表的字段和取值全部来自 2026-08-21 模拟账户的真实实单。
"""

from __future__ import annotations

import pytest

from qbg.execution import easytrader_adapter as mod
from qbg.execution.base import Order
from qbg.execution.easytrader_adapter import (
    EasytraderAdapter,
    LiveLockError,
    assert_live_allowed,
    find_entrust,
)
from qbg.execution.ths_order_form import OrderFormError, PlaceResult


def _order(code="600519.SH", side="BUY", qty=100, price=1213.97):
    return Order(code=code, side=side, quantity=qty, price=price, reason="test")


def _entrust(code="600519", side="买入", qty=100, price=1213.97, no="6216979694", note="未成交"):
    """2026-08-21 实测的真实委托行结构。"""
    return {"委托时间": "21:31:30", "证券代码": code, "证券名称": "贵州茅台",
            "操作": side, "备注": note, "委托数量": qty, "成交数量": 0,
            "委托价格": price, "成交均价": 0.0, "撤消数量": 0,
            "合同编号": no, "交易市场": "上海Ａ股"}


PAPER_ACCOUNT = "模拟炒股-****"
REAL_ACCOUNT = "南京证券-1234567890"

BLANK_ROW = {"委托时间": "", "证券代码": "", "证券名称": "", "操作": "", "备注": "",
             "委托数量": 0, "成交数量": 0, "委托价格": 0.0, "成交均价": 0.0,
             "撤消数量": 0, "合同编号": "", "交易市场": ""}


@pytest.fixture(autouse=True)
def paper_mode(monkeypatch):
    """默认跑在 PAPER 下 —— 模拟盘不需要三把锁，和 risk.gates.mode_guard 同语义。"""
    monkeypatch.setattr(mod.settings, "qbg_mode", "PAPER")


# ---------------------------------------------------------------------------
# 回读匹配
# ---------------------------------------------------------------------------
def test_find_entrust_matches_real_row():
    assert find_entrust([_entrust()], _order())["合同编号"] == "6216979694"


def test_blank_placeholder_rows_are_ignored():
    """实测撤单后 4 行里 3 行是空占位行，不滤会找不到目标、误判成没下成。"""
    rows = [BLANK_ROW, BLANK_ROW, _entrust(), BLANK_ROW]
    assert find_entrust(rows, _order()) is not None


@pytest.mark.parametrize("kwargs", [
    {"code": "000001"},          # 代码不同
    {"side": "卖出"},             # 方向不同
    {"qty": 200},                # 数量不同
    {"price": 1300.00},          # 价格不同
])
def test_find_entrust_requires_all_four_fields(kwargs):
    """四个字段少比一个，就可能匹配到同一天的另一笔单。"""
    assert find_entrust([_entrust(**kwargs)], _order()) is None


def test_price_tolerance_absorbs_float_noise():
    assert find_entrust([_entrust(price=1213.9701)], _order()) is not None
    assert find_entrust([_entrust(price=1213.99)], _order()) is None


def test_code_matches_across_formats():
    """订单里是 600519.SH，委托表里是裸 6 位。"""
    assert find_entrust([_entrust(code="600519")], _order(code="600519.SH")) is not None


# ---------------------------------------------------------------------------
# 三把锁
# ---------------------------------------------------------------------------
def test_advisory_mode_refuses(monkeypatch):
    monkeypatch.setattr(mod.settings, "qbg_mode", "ADVISORY")
    with pytest.raises(LiveLockError, match="ADVISORY"):
        assert_live_allowed({})


def test_paper_mode_needs_no_locks(monkeypatch):
    monkeypatch.setattr(mod.settings, "qbg_mode", "PAPER")
    assert_live_allowed({})          # 不抛异常即通过


def test_live_needs_all_three_locks(monkeypatch):
    monkeypatch.setattr(mod.settings, "qbg_mode", "LIVE")
    monkeypatch.setattr(mod.settings, "i_confirm_real", 0)
    with pytest.raises(LiveLockError):
        assert_live_allowed({"allow_live_mode": True})
    monkeypatch.setattr(mod.settings, "i_confirm_real", 1)
    with pytest.raises(LiveLockError):
        assert_live_allowed({"allow_live_mode": False})
    assert_live_allowed({"allow_live_mode": True})   # 三把锁齐了才放行


# ---------------------------------------------------------------------------
# 提交流程
# ---------------------------------------------------------------------------
def _adapter(monkeypatch, *, placed=None, entrusts=None, baseline=None, max_orders=8):
    """`entrusts` 是**提交后**能读到的委托；`baseline` 是提交前的（默认空）。

    分开两者是必须的：adapter 先读基线，之后只认**新出现**的合同编号。
    如果 reader 每次都返回同一份数据，新下的单永远不算「新增」。
    """
    calls = []
    reads = {"n": 0}

    def fake_place(_user, *, code, side, quantity, price):
        calls.append({"code": code, "side": side, "quantity": quantity, "price": price})
        if placed is not None:
            return placed(len(calls) - 1)
        return PlaceResult(True, "ok")

    def fake_read(_user):
        reads["n"] += 1
        return list(baseline or []) if reads["n"] == 1 else list(entrusts or [])

    monkeypatch.setattr(mod, "place_order", fake_place)
    adapter = EasytraderAdapter(connect=lambda: object(), reader=fake_read,
                                max_orders=max_orders,
                                # 默认跑在 PAPER 下，所以要给一个模拟盘账户，
                                # 否则会被账户守卫拦下（那是另一组测试的事）。
                                account_reader=lambda _u: PAPER_ACCOUNT)
    return adapter, calls


def test_sell_orders_go_first(monkeypatch):
    """先卖后买 —— 卖出释放的资金当日可用于买入。"""
    buy, sell = _order(side="BUY"), _order(code="000001.SZ", side="SELL")
    rows = [_entrust(), _entrust(code="000001", side="卖出")]
    adapter, calls = _adapter(monkeypatch, entrusts=rows)
    adapter.submit([buy, sell], "2026-08-21")
    assert [c["side"] for c in calls] == ["SELL", "BUY"]


def test_missing_entrust_halts_remaining_orders(monkeypatch):
    """回读找不到 —— 这笔可能根本没下出去，必须停掉后面所有单。"""
    orders = [_order(), _order(code="000001.SZ"), _order(code="000002.SZ")]
    adapter, calls = _adapter(monkeypatch, entrusts=[])      # 委托表空
    result = adapter.submit(orders, "2026-08-21")
    assert not result.ok
    assert result.submitted == 0
    assert len(calls) == 1, "第一笔就该停，不该继续下第二笔"
    assert "没有出现这一笔" in result.message


def test_form_error_halts_remaining_orders(monkeypatch):
    def boom(_index):
        raise OrderFormError("提交前回读不符，未提交：price 期望 121397 实际 12729")

    orders = [_order(), _order(code="000001.SZ")]
    adapter, calls = _adapter(monkeypatch, placed=boom, entrusts=[_entrust()])
    result = adapter.submit(orders, "2026-08-21")
    assert not result.ok
    assert len(calls) == 1
    assert "提交前回读不符" in result.message


def test_rejected_by_client_halts(monkeypatch):
    """客户端弹「小数部分应为 2 位」这类致命提示时，place_order 返回 ok=False。"""
    def rejected(_index):
        return PlaceResult(False, "客户端提示「委托价格的小数部分应为 2 位」，已中止")

    adapter, _calls = _adapter(monkeypatch, placed=rejected, entrusts=[_entrust()])
    result = adapter.submit([_order()], "2026-08-21")
    assert not result.ok
    assert "小数部分应为" in result.message


def test_all_verified_reports_ok(monkeypatch):
    adapter, calls = _adapter(monkeypatch, entrusts=[_entrust()])
    result = adapter.submit([_order()], "2026-08-21")
    assert result.ok
    assert result.submitted == 1
    assert len(calls) == 1


def test_max_orders_rejects_whole_batch(monkeypatch):
    """超上限整批拒绝 —— 半批执行比不执行更难收拾。"""
    orders = [_order(code=f"60000{i}.SH") for i in range(5)]
    adapter, calls = _adapter(monkeypatch, entrusts=[], max_orders=3)
    result = adapter.submit(orders, "2026-08-21")
    assert not result.ok
    assert calls == [], "超上限时一笔都不该下"
    assert "超过单次上限" in result.message


def test_empty_orders_is_ok(monkeypatch):
    adapter, calls = _adapter(monkeypatch)
    result = adapter.submit([], "2026-08-21")
    assert result.ok and result.submitted == 0 and calls == []


def test_entrust_read_failure_is_not_success(monkeypatch):
    """读不到当日委托 = 无法确认，必须当失败处理，不能当成功。"""
    def boom(_user):
        raise RuntimeError("剪贴板未被更新")

    monkeypatch.setattr(mod, "place_order",
                        lambda _u, **_k: PlaceResult(True, "ok"))
    adapter = EasytraderAdapter(connect=lambda: object(), reader=boom,
                                account_reader=lambda _u: PAPER_ACCOUNT)
    result = adapter.submit([_order()], "2026-08-21")
    assert not result.ok
    # 基线那一步就读不到，连单都不会下
    assert "基线" in result.message


# ---------------------------------------------------------------------------
# 新增合同编号才算数
#
# 2026-08-21 联调暴露的缺陷：当日委托里出现了三行代码/方向/数量/价格完全相同的
# 记录（同一只票反复试单）。只比四个字段会匹配到早就撤掉的那笔，
# 然后报告成功并带回**错误的合同编号**。
# ---------------------------------------------------------------------------
def test_duplicate_params_would_fool_plain_matching():
    """先证明缺陷真实存在：四字段相同的旧记录会被 find_entrust 匹配上。"""
    stale = _entrust(no="OLD-1", note="全部撤单")
    assert find_entrust([stale], _order())["合同编号"] == "OLD-1"


def test_only_new_entrust_id_counts(monkeypatch):
    """基线里已有的同参数记录不算数，必须出现新编号才判成功。"""
    stale = _entrust(no="OLD-1", note="全部撤单")
    fresh = _entrust(no="NEW-1")
    reads = iter([[stale], [stale, fresh]])     # 提交前基线 / 提交后
    monkeypatch.setattr(mod, "place_order", lambda _u, **_k: PlaceResult(True, "ok"))
    adapter = EasytraderAdapter(connect=lambda: object(),
                                reader=lambda _u: next(reads),
                                account_reader=lambda _u: PAPER_ACCOUNT)
    result = adapter.submit([_order()], "2026-08-21")
    assert result.ok
    assert "NEW-1" in result.message or result.submitted == 1


def test_no_new_entrust_is_failure_even_if_stale_matches(monkeypatch):
    """委托表没变化 = 这笔没下出去，哪怕表里有个长得一模一样的旧记录。"""
    stale = _entrust(no="OLD-1", note="全部撤单")
    monkeypatch.setattr(mod, "place_order", lambda _u, **_k: PlaceResult(True, "ok"))
    adapter = EasytraderAdapter(connect=lambda: object(), reader=lambda _u: [stale],
                                account_reader=lambda _u: PAPER_ACCOUNT)
    result = adapter.submit([_order()], "2026-08-21")
    assert not result.ok
    assert "没有出现这一笔" in result.message


def test_baseline_read_failure_refuses_to_trade(monkeypatch):
    """建不起基线就不许下单 —— 没有基线，回读校验形同虚设。"""
    def boom(_user):
        raise RuntimeError("剪贴板未被更新")

    monkeypatch.setattr(mod, "place_order", lambda _u, **_k: PlaceResult(True, "ok"))
    adapter = EasytraderAdapter(connect=lambda: object(), reader=boom,
                                account_reader=lambda _u: PAPER_ACCOUNT)
    result = adapter.submit([_order()], "2026-08-21")
    assert not result.ok
    assert "基线" in result.message and result.submitted == 0


# ---------------------------------------------------------------------------
# 账户守卫：声明的 mode 必须和客户端里真正登录的账户对得上
#
# QBG_MODE=PAPER 在 assert_live_allowed 里是直接放行的（模拟盘不需要三把锁），
# 但 PAPER 只是 .env 里的一句声明。客户端登着真钱账户 + .env 写 PAPER，
# 就会在真钱上下单且一道锁都不查。客户端的资金账号是唯一的独立信源。
# ---------------------------------------------------------------------------

def test_paper_mode_accepts_simulated_account():
    mod.assert_account_matches_mode(PAPER_ACCOUNT, "PAPER", "模拟")


def test_paper_mode_refuses_real_account():
    """最要紧的一条：声明 PAPER 却登着真钱账户，必须拒绝。"""
    with pytest.raises(LiveLockError, match="真钱"):
        mod.assert_account_matches_mode(REAL_ACCOUNT, "PAPER", "模拟")


def test_live_mode_refuses_simulated_account():
    """反向也要拦：三把锁都开了却在模拟盘上跑，多半是配错了。"""
    with pytest.raises(LiveLockError, match="模拟盘"):
        mod.assert_account_matches_mode(PAPER_ACCOUNT, "LIVE", "模拟")


def test_live_mode_accepts_real_account():
    mod.assert_account_matches_mode(REAL_ACCOUNT, "LIVE", "模拟")


@pytest.mark.parametrize("mode", ["PAPER", "LIVE"])
def test_unreadable_account_refuses_to_trade(mode):
    """读不到账户 = 无法确认在哪个账户上交易 = 不交易。"""
    with pytest.raises(LiveLockError, match="读不到"):
        mod.assert_account_matches_mode("", mode, "模拟")


def test_advisory_mode_skips_account_check():
    """ADVISORY 不碰券商，账户是什么都无所谓。"""
    mod.assert_account_matches_mode("", "ADVISORY", "模拟")


def test_submit_enforces_account_guard(monkeypatch):
    """守卫要真的接在 submit 上，不能只是个没人调的函数。"""
    monkeypatch.setattr(mod.settings, "qbg_mode", "PAPER")
    monkeypatch.setattr(mod, "place_order",
                        lambda _u, **_k: PlaceResult(True, "ok"))
    adapter = EasytraderAdapter(connect=lambda: object(),
                                reader=lambda _u: [],
                                account_reader=lambda _u: REAL_ACCOUNT)
    with pytest.raises(LiveLockError, match="真钱"):
        adapter.submit([_order()], "2026-08-25")
