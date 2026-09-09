"""预测新鲜度硬闸。全离线。

## 它要挡的那件事

2026-09-09 实测：日流程连着一个月每天选出**完全相同的三只票**。
`latest_date_scores` 取的是「预测里最后一天」，而生产模型的 test 段止于
2026-08-10 —— 那一天的分数被反复用了 30 天。

**行情闸看不见这件事**：`data_freshness_guard` 守的是 K 线，K 线每天都在更新
（`last_date` 是昨天，一路绿灯）。陈旧的是**预测**，而当时没有任何一道闸
在看它的日期。
"""

from __future__ import annotations

import pytest

from qbg.execution.base import Order
from qbg.risk import gates

LIMITS = {
    "allow_live_mode": True, "require_trading_session": False,
    "max_stale_days": 1, "max_prediction_stale_days": 3,
    "max_position_pct": 0.34, "min_cash_buffer_pct": 0.05,
    "max_daily_loss_pct": 0.03, "order_slippage_pct": 0.002,
    "rebalance_drift_band": 0.03,
}


def order(side="BUY", qty=100, price=10.0):
    return Order("600519.SH", side, qty, price, "test", ref_price=price)


# ----------------------------------------------------------------------
# 闸本身
# ----------------------------------------------------------------------

def test_fresh_prediction_passes():
    r = gates.prediction_freshness_guard("2026-08-10", "2026-08-10", LIMITS)
    assert r.passed


def test_stale_prediction_fails():
    """就是那次故障的形状：预测停在 8-10，今天是 9-09。"""
    r = gates.prediction_freshness_guard("2026-08-10", "2026-09-09", LIMITS)
    assert not r.passed
    assert "2026-08-10" in r.reason


def test_missing_prediction_date_fails_closed():
    """拿不到日期**不放行**。

    最容易犯的错是"未知就当没问题" —— 而这道闸存在的全部理由就是
    "没人在看这个数"。fail-open 等于把闸删掉。
    """
    assert not gates.prediction_freshness_guard(None, "2026-09-09", LIMITS).passed


def test_threshold_is_configurable_and_inclusive():
    # 08-06 → 08-10 隔 2 个**交易日**（08-08/09 是周末，不算）。
    # 按日历日估会得出 4，这正是 `_stale_days` 存在的理由 —— 见它的说明。
    limits = dict(LIMITS, max_prediction_stale_days=1)
    assert gates.prediction_freshness_guard("2026-08-06", "2026-08-10", limits).passed is False
    # 正好等于阈值要放行
    assert gates.prediction_freshness_guard("2026-08-07", "2026-08-10", limits).passed is True
    loose = dict(LIMITS, max_prediction_stale_days=30)
    assert gates.prediction_freshness_guard("2026-08-10", "2026-09-09", loose).passed


def test_future_prediction_is_not_stale():
    """预测日期不早于今天时 stale=0，不该因为"未来"而被拦。"""
    assert gates.prediction_freshness_guard("2026-09-10", "2026-09-09", LIMITS).passed


# ----------------------------------------------------------------------
# 接线：它必须是**硬闸**，而不是订单闸
# ----------------------------------------------------------------------

def test_stale_prediction_blocks_everything_including_sells():
    """硬闸语义：全盘不交易，**SELL 也不放行**。

    订单闸的"SELL 永远放行"假设「信号是对的，只是某些订单不该执行」。
    预测过期时这个前提不成立 —— 买卖两个方向建立在同一份过期信号上，
    凭什么相信它的卖出 judgment？此时唯一安全的动作是全盘停手。
    """
    sell = order(side="SELL")
    hard_ok, allowed, results = gates.run_all_gates(
        target_weights={}, orders=[sell], current_cash=50_000, total_equity=100_000,
        today_pnl=0, latest_data_date="2026-09-08", asof="2026-09-09",
        prev_close={sell.code: 10}, market_price={sell.code: 10},
        is_st={sell.code: False}, suspended={sell.code: False},
        sellable_qty={sell.code: 100}, current_qty={sell.code: 100},
        limits=LIMITS, prediction_date="2026-08-10",
    )
    assert hard_ok is False
    assert allowed == []
    failed = [r.name for r in results if not r.passed]
    assert "prediction_freshness_guard" in failed


def test_market_data_fresh_but_prediction_stale_is_caught():
    """**这正是那次故障**：行情新鲜、预测陈旧，旧代码里没有任何东西看得见。"""
    hard_ok, _allowed, results = gates.run_all_gates(
        target_weights={}, orders=[], current_cash=50_000, total_equity=100_000,
        today_pnl=0, latest_data_date="2026-09-08", asof="2026-09-09",
        prev_close={}, market_price={}, is_st={}, suspended={},
        sellable_qty={}, current_qty={}, limits=LIMITS,
        prediction_date="2026-08-10",
    )
    by_name = {r.name: r for r in results}
    assert by_name["data_freshness_guard"].passed, "行情本来就是新鲜的"
    assert not by_name["prediction_freshness_guard"].passed
    assert hard_ok is False


def test_hard_gate_slice_kept_in_sync():
    """加硬闸时 `results[len(hard):]` 那个切片必须跟着改。

    写死成 `results[3:]` 的话，第 4 道硬闸会被当成订单闸参与"只砍 BUY" ——
    而它在上面已经 return 过了，走到这里说明它是通过的，于是这个错误
    **完全不可见**。这条测试就是那道保险。
    """
    import inspect

    src = inspect.getsource(gates.run_all_gates)
    assert "results[len(hard):]" in src, \
        "硬闸切片写死了个数 —— 加硬闸时会静默错位"


@pytest.mark.parametrize("path", [
    "src/qbg/orchestrator/daily_cycle.py",
    "scripts/04_plan_orders.py",
])
def test_both_callers_pass_the_prediction_date(path):
    """两个调用方都要传。漏一个，那条路径的闸就等于不存在。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    src = (root / path).read_text(encoding="utf-8")
    assert "prediction_date=" in src, f"{path} 没给 run_all_gates 传预测日期"


def test_daily_cycle_prefers_the_live_experiment():
    """`train(live=True)` 写 cn_lgb_live，而 2026-09-09 之前没人读它 ——
    加了 --retrain 也白搭。这条钉住日流程走的是 live 优先那条路。"""
    import inspect

    from qbg.orchestrator import daily_cycle

    src = inspect.getsource(daily_cycle.run_daily)
    assert "load_production_predictions" in src, \
        "daily_cycle 仍在直接读静态 experiment —— 重训的结果没人用"
