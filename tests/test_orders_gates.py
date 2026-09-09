from __future__ import annotations

import pytest

from qbg.execution.base import Order
from qbg.execution.order_planner import plan_orders
from qbg.risk import gates

LIMITS = {
    "allow_live_mode": False, "require_trading_session": False, "max_stale_days": 1,
    "max_position_pct": 0.34, "min_cash_buffer_pct": 0.05, "max_daily_loss_pct": 0.03,
}


def order(code="600519.SH", side="BUY", qty=100, price=10.0):
    return Order(code, side, qty, price, "test", ref_price=price)


def test_planner_rounds_buy_to_lot_and_clamps_limit():
    out = plan_orders({"600519.SH": 0.3}, {}, {"600519.SH": 10.99}, {"600519.SH": 10.0},
                      10_000, slippage=0.1, drift_band=0)
    assert len(out) == 1 and out[0].quantity == 200
    assert out[0].price == 11.0


def test_planner_sells_odd_lot_in_full_and_sell_first():
    out = plan_orders({"000858.SZ": 0.3}, {"600519.SH": 137},
                      {"600519.SH": 10, "000858.SZ": 10},
                      {"600519.SH": 10, "000858.SZ": 10}, 10_000, drift_band=0)
    assert out[0].side == "SELL" and out[0].quantity == 137
    assert out[1].side == "BUY" and out[1].quantity == 300


@pytest.mark.parametrize("mode,confirm,allow,passed", [
    ("ADVISORY", 0, False, True), ("LIVE", 0, True, False), ("LIVE", 1, False, False),
    ("LIVE", 1, True, True),
])
def test_mode_guard(mode, confirm, allow, passed):
    assert gates.mode_guard({"allow_live_mode": allow}, mode, confirm).passed is passed


def test_session_guard_pass_and_block():
    assert gates.session_guard({"require_trading_session": False}, False).passed
    assert not gates.session_guard({"require_trading_session": True}, False).passed
    assert gates.session_guard({"require_trading_session": True}, True).passed


def test_data_freshness_guard_pass_and_block():
    assert gates.data_freshness_guard("2026-08-07", "2026-08-10", LIMITS).passed
    assert not gates.data_freshness_guard("2026-08-05", "2026-08-10", LIMITS).passed


def test_price_limit_guard_pass_and_block():
    assert gates.price_limit_guard([order()], {"600519.SH": 10}, {}, {"600519.SH": 10}).passed
    assert not gates.price_limit_guard([order()], {"600519.SH": 10}, {}, {"600519.SH": 11}).passed


def test_suspension_st_t1_and_lot_guards():
    buy, sell = order(), order(side="SELL", qty=200)
    assert not gates.suspension_guard([buy], {buy.code: True}).passed
    assert not gates.st_guard([buy], {buy.code: True}).passed
    assert not gates.t1_guard([sell], {sell.code: 100}).passed
    assert not gates.lot_guard([order(qty=50)], {}).passed
    assert gates.lot_guard([buy], {}).passed


def test_position_cash_and_loss_guards_pass_and_block():
    assert not gates.max_position_guard({"600519.SH": 0.5}, LIMITS).passed
    assert gates.max_position_guard({"600519.SH": 0.3}, LIMITS).passed
    assert not gates.min_cash_guard([order(price=100)], 10_000, 10_000, LIMITS).passed
    assert gates.min_cash_guard([order(price=10)], 10_000, 10_000, LIMITS).passed
    assert not gates.daily_loss_kill_switch([order()], -4_000, 100_000, LIMITS).passed
    assert gates.daily_loss_kill_switch([order()], -2_000, 100_000, LIMITS).passed


def test_hard_gate_failure_drops_everything():
    hard_ok, allowed, _ = gates.run_all_gates(
        target_weights={}, orders=[order(side="SELL")], current_cash=0, total_equity=100_000,
        today_pnl=0, latest_data_date=None, asof="2026-08-10", prev_close={}, market_price={},
        is_st={}, suspended={}, sellable_qty={}, current_qty={}, limits=LIMITS,
    )
    assert not hard_ok and allowed == []


def test_all_order_gates_fail_but_sell_is_preserved():
    sell = order(side="SELL", qty=200, price=9)
    bad = dict(LIMITS, min_cash_buffer_pct=2.0, max_daily_loss_pct=0.0)
    hard_ok, allowed, results = gates.run_all_gates(
        target_weights={sell.code: 0.9}, orders=[sell], current_cash=0, total_equity=100_000,
        today_pnl=-10_000, latest_data_date="2026-08-08", asof="2026-08-10",
        prev_close={sell.code: 10}, market_price={sell.code: 9}, is_st={sell.code: True},
        suspended={sell.code: True}, sellable_qty={sell.code: 0}, current_qty={sell.code: 100},
        limits=bad,
        # 这条测的是「所有订单闸都失败时 SELL 仍然放行」，不是新鲜度。
        # 但 prediction_freshness_guard 是**硬闸**且 None 不放行，所以必须给
        # 一个新鲜的日期，否则流程在硬闸阶段就 return 了，压根走不到订单闸。
        prediction_date="2026-08-10",
    )
    assert hard_ok and allowed == [sell]
    assert sum(not result.passed for result in results) >= 4


def test_advisory_writes_utf8_markdown_and_excel_friendly_csv(tmp_path):
    from qbg.execution.advisory import AdvisoryAdapter

    result = AdvisoryAdapter(tmp_path).submit([order()], "2026-08-10", [])
    assert result.ok and result.submitted == 1
    assert (tmp_path / "2026-08-10.md").read_text(encoding="utf-8").startswith("# A股下单清单")
    assert (tmp_path / "2026-08-10.csv").read_bytes().startswith(b"\xef\xbb\xbf")
