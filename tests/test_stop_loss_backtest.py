"""回测引擎的止损 + 现金触发提前调仓。全离线，手造已知答案的价格路径。

止损判据：建仓以来亏损达到 `stop_loss` 就卖。和移动止盈不同，**调仓日也生效** ——
调仓日把止损的票从当天选股里剔除、槽位让给下一名；监控日直接卖成现金。

现金触发：现金占比 ≥ `cash_trigger` 时监控日当作调仓日（实盘
`QBG_REBALANCE_CASH_TRIGGER`）。此前引擎**完全不模拟**它，0.50 这个值没有依据。
"""

from __future__ import annotations

import pandas as pd

from qbg.backtest import engine
from qbg.execution.fees import FeeProfile

FREE = FeeProfile(commission_rate=0.0, commission_min=0.0,
                  stamp_tax_rate=0.0, transfer_fee_rate=0.0)


def _panel(a, b=None, down_a=None):
    n = len(a)
    dates = pd.bdate_range("2026-01-05", periods=n)
    open_px = pd.DataFrame({"A": a, "B": b or [100.0] * n}, index=dates, dtype=float)
    flags = pd.DataFrame(False, index=dates, columns=["A", "B"])
    down = flags.copy()
    if down_a is not None:
        down["A"] = down_a
    return engine.Panel(open_px=open_px, close_px=open_px.copy(), suspended=flags,
                        limit_up_open=flags.copy(), limit_down_open=down)


def _scores(panel):
    """模型永远更看好 A，B 是替补。"""
    return pd.DataFrame({"A": 1.0, "B": 0.5}, index=panel.dates)


def _run(panel, **kw):
    kw.setdefault("rebalance_phase", 1)
    kw.setdefault("keep_rank", 2)
    return engine.run_backtest(_scores(panel), panel, k=1, total_weight=1.0,
                               fee_profile=FREE, slippage_grid=(), **kw)


# 第 1 天 100 建仓，之后 95 -> 90：第 3 天建仓以来 -10%，越过 8% 止损线。
FALLING = [100, 100, 95, 90, 88, 86, 85, 84, 83, 82, 81, 80]


def test_defaults_change_nothing():
    panel = _panel(FALLING)
    base = _run(panel, rebalance_every=20)
    same = _run(panel, rebalance_every=20, stop_loss=0.0, cash_trigger=0.0)
    assert base.weights["A"].max() > 0, "A 没建仓 —— 下面的相等是在空仓上成立的"
    pd.testing.assert_series_equal(base.daily_returns, same.daily_returns)
    assert same.stop_exits == 0 and same.cash_rebalances == 0


def test_monitoring_day_stop_sells_to_cash():
    panel = _panel(FALLING)
    res = _run(panel, rebalance_every=20, stop_loss=0.08)
    assert res.stop_exits == 1
    assert res.weights["A"].iloc[-1] == 0.0
    assert res.weights["B"].iloc[-1] == 0.0, "监控日不该顺手买替补 —— 那是调仓的事"


def test_stop_limits_the_loss_versus_holding():
    panel = _panel(FALLING)
    hold = _run(panel, rebalance_every=20)
    stop = _run(panel, rebalance_every=20, stop_loss=0.08)
    assert stop.strategy.total_return > hold.strategy.total_return


def test_small_loss_is_not_stopped():
    panel = _panel([100, 100, 97, 95, 94, 95, 96, 97, 98, 99, 100, 101])
    res = _run(panel, rebalance_every=20, stop_loss=0.08)
    assert res.stop_exits == 0
    assert res.weights["A"].iloc[-1] > 0


def test_limit_down_blocks_the_stop():
    """A股约束：一字跌停开盘卖不出，止损也救不了。不计入执行次数。"""
    n = len(FALLING)
    res = _run(_panel(FALLING, down_a=[False] * 3 + [True] * (n - 3)),
               rebalance_every=20, stop_loss=0.08)
    assert res.stop_exits == 0
    assert res.weights["A"].iloc[-1] > 0
    assert res.blocked["limit_down_cannot_sell"] > 0


def test_rebalance_day_stop_excludes_and_hands_the_slot_to_the_next_name():
    """**调仓日也止损**：模型再看好 A 也不留，而且槽位当天就让给 B，不空着。"""
    # 每 3 日调仓、相位 1 -> 第 1/4/7 天调仓。A 在第 4 天才跌破 -8%。
    path = [100, 100, 97, 95, 90, 89, 88, 87, 86, 85]
    res = _run(_panel(path), rebalance_every=3, stop_loss=0.08)
    day4 = res.weights.iloc[4]
    assert res.stop_exits == 1
    assert day4["A"] == 0.0, "止损的票当天被选回来了"
    assert day4["B"] > 0, "槽位没有让给下一名"


def test_cash_trigger_redeploys_after_a_stop():
    """止损卖成现金之后，现金 100% ≥ 阈值 -> 第二天提前调仓，钱不闲置到周期末。"""
    panel = _panel(FALLING)
    idle = _run(panel, rebalance_every=20, stop_loss=0.08)
    redeploy = _run(panel, rebalance_every=20, stop_loss=0.08, cash_trigger=0.5)
    assert idle.cash_rebalances == 0
    assert redeploy.cash_rebalances >= 1
    assert redeploy.weights.iloc[-1].sum() > 0, "提前调仓之后应当重新持仓"


def test_cash_trigger_does_not_collapse_phases():
    """首次建仓之前不触发。否则空仓的 100% 现金会让所有相位都在第 1 天调仓，
    相位平均就失效了。"""
    panel = _panel([100.0 + i for i in range(30)])
    kw = dict(rebalance_every=10, cash_trigger=0.5)
    first_buy = [ _run(panel, rebalance_phase=p, **kw).weights["A"].gt(0).idxmax()
                  for p in (2, 5) ]
    assert first_buy[0] != first_buy[1], "现金触发把不同相位的首次建仓日抹成一样了"


def test_real_cash_fraction_not_fooled_by_a_drawdown():
    """组合整体跌了但一笔没卖，现金占比没变 —— 不该触发提前调仓。

    这条最初失败过：引擎当时的权重只按收益漂移、不按组合收益归一化，
    `1 - sum(w)` 在下跌时会虚高（跌 40% 显示 40% "现金"）。顺着查下去才发现
    那是一个影响所有多日持仓回测的 bug（见 test_multi_day_hold_compounds_correctly）。
    """
    crash = [100, 100, 90, 80, 70, 65, 60, 60, 60, 60, 60, 60]
    res = _run(_panel(crash), rebalance_every=20, cash_trigger=0.3)
    assert res.cash_rebalances == 0, "没有任何卖出，却被当成高现金提前调仓了"


def test_multi_day_hold_compounds_correctly():
    """**2026-09-14 修掉的引擎 bug。** 持有不调时权重必须按组合收益归一化。

    修之前 `w_prev = w_new * (1 + r)` 不除组合收益：连涨两个 10% 算成 +22.10%
    （真实 +21%），连跌算成 −18.10%（真实 −19%）。日频调仓每天重置目标所以一直是对的，
    **只有持有超过一天才错** —— 正好是调仓间隔、止盈、止损这几件事的回测。
    """
    for path, expected in (([100, 100, 110, 121, 121], 0.21),
                           ([100, 100, 90, 81, 81], -0.19),
                           ([100, 100, 110, 99, 99], -0.01)):
        res = engine.run_backtest(
            pd.DataFrame({"A": 1.0, "B": 0.0}, index=_panel(path).dates), _panel(path),
            k=1, total_weight=1.0, rebalance_every=20, rebalance_phase=1,
            fee_profile=FREE, slippage_grid=())
        assert abs(res.strategy.total_return - expected) < 1e-9, (path, res.strategy.total_return)
