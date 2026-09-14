"""移动止盈：回测引擎 + 实盘判据。全离线，手造已知答案的价格路径。

判据（和 quant-trading `risk/trailing.py` 同一套）：
  · 浮盈峰值 ≥ arm 才"上膛" —— 没赚够的票不管
  · 上膛后从峰值回撤 ≥ trail 就整仓卖出
  · **只在非调仓日触发** —— 调仓日交给新一轮选股，不砍模型仍看好的票

quant-trading 2026-08-04 在几乎相同的配置（k=3、每 10 日、行业中性）上回测，
部署档 arm15/trail5 夏普 1.65→1.55、回撤反而恶化；"不变差"的档位全是零触发。
所以这里特别钉住"触发次数"这个数 —— 不看它就分不清"变好了"和"没做事"。
"""

from __future__ import annotations

import pandas as pd

from qbg.backtest import engine
from qbg.execution.fees import FeeProfile

FREE = FeeProfile(commission_rate=0.0, commission_min=0.0,
                  stamp_tax_rate=0.0, transfer_fee_rate=0.0)


def _panel(prices_a, prices_b=None, limit_down_a=None):
    """两只票。A 走给定路径，B 平着不动。价格是开盘价序列。"""
    n = len(prices_a)
    dates = pd.bdate_range("2026-01-05", periods=n)
    b = prices_b or [100.0] * n
    open_px = pd.DataFrame({"A": prices_a, "B": b}, index=dates, dtype=float)
    flags = pd.DataFrame(False, index=dates, columns=["A", "B"])
    down = flags.copy()
    if limit_down_a is not None:
        down["A"] = limit_down_a
    return engine.Panel(open_px=open_px, close_px=open_px.copy(), suspended=flags,
                        limit_up_open=flags.copy(), limit_down_open=down)


def _hold_a(panel):
    """模型永远只看好 A —— 所以任何对 A 的卖出都只能来自移动止盈。"""
    return pd.DataFrame({"A": 1.0, "B": 0.0}, index=panel.dates)


def _run(panel, **kw):
    # 相位 1：信号要 shift 一天才执行，第 0 天没有前一日信号可用。相位 0 的话
    # 唯一的调仓日落在第 0 天，A 根本买不进来，所有断言都在空仓上"通过"。
    kw.setdefault("rebalance_phase", 1)
    return engine.run_backtest(_hold_a(panel), panel, k=1, total_weight=1.0,
                               fee_profile=FREE, slippage_grid=(), **kw)


# A：+20% 到 120，然后回撤到 113（从峰值 −5.8%）。
RUN_UP_THEN_GIVE_BACK = [100, 100, 110, 120, 116, 113, 112, 111, 110, 109, 108, 107]


def test_default_is_off_and_changes_nothing():
    """默认 0 = 关闭。**所有现有调用方的结果必须逐位不变。**"""
    panel = _panel(RUN_UP_THEN_GIVE_BACK)
    base = _run(panel, rebalance_every=20)
    same = _run(panel, rebalance_every=20, trail_arm=0.0, trail_pct=0.0)
    assert base.weights["A"].max() > 0, "A 没建仓 —— 下面的相等是在两个空仓上成立的"
    pd.testing.assert_series_equal(base.daily_returns, same.daily_returns)
    assert same.trailing_exits == 0


def test_armed_winner_is_sold_after_giving_back():
    """涨过 15%、从峰值回撤超过 5% —— 卖掉，而且之后不再持有 A。"""
    panel = _panel(RUN_UP_THEN_GIVE_BACK)
    res = _run(panel, rebalance_every=20, trail_arm=0.15, trail_pct=0.05)
    assert res.trailing_exits == 1
    assert res.weights["A"].iloc[-1] == 0.0, "触发之后应当空仓等下一个调仓日"


def test_selling_locks_in_the_gain_versus_riding_it_down():
    """这条路径上止盈应当赢：峰值之后一路跌到 107。"""
    panel = _panel(RUN_UP_THEN_GIVE_BACK)
    ride = _run(panel, rebalance_every=20)
    trail = _run(panel, rebalance_every=20, trail_arm=0.15, trail_pct=0.05)
    assert trail.strategy.total_return > ride.strategy.total_return


def test_never_armed_is_never_sold():
    """只涨了 10%，没到 15% 的上膛线。回撤再大也不管 —— 那是止损的事，不是止盈。"""
    panel = _panel([100, 100, 105, 110, 104, 100, 96, 94, 92, 90, 89, 88])
    res = _run(panel, rebalance_every=20, trail_arm=0.15, trail_pct=0.05)
    assert res.trailing_exits == 0
    assert res.weights["A"].iloc[-1] > 0


def test_small_give_back_is_not_enough():
    """涨过 15% 但只回撤 3%，没到 5%。继续拿着 —— 让赢家跑。"""
    panel = _panel([100, 100, 110, 120, 118, 117, 117, 118, 119, 120, 121, 122])
    res = _run(panel, rebalance_every=20, trail_arm=0.15, trail_pct=0.05)
    assert res.trailing_exits == 0


def test_rebalance_day_does_not_trigger():
    """调仓日交给选股。模型仍然只看好 A，所以 A 应该**留着**。

    每日调仓 = 每天都是调仓日 = 移动止盈永远不触发。这不是 bug：参考实现就是
    这么设计的，理由是不去砍一只模型仍排在前 k 的票。
    """
    panel = _panel(RUN_UP_THEN_GIVE_BACK)
    res = _run(panel, rebalance_every=1, trail_arm=0.15, trail_pct=0.05)
    assert res.trailing_exits == 0


def test_limit_down_open_blocks_the_sell():
    """A股约束：一字跌停开盘卖不出去。触发了也得被迫持有，而且**不计入**触发次数。"""
    n = len(RUN_UP_THEN_GIVE_BACK)
    down = [False] * n
    down[4:] = [True] * (n - 4)            # 从回撤开始那天起一直跌停开盘
    panel = _panel(RUN_UP_THEN_GIVE_BACK, limit_down_a=down)
    res = _run(panel, rebalance_every=20, trail_arm=0.15, trail_pct=0.05)
    assert res.trailing_exits == 0, "跌停卖不出的不该算作执行了止盈"
    assert res.weights["A"].iloc[-1] > 0
    assert res.blocked["limit_down_cannot_sell"] > 0


def test_peak_resets_after_exit():
    """卖掉之后重新买回，峰值从新的建仓价重算，不能沿用上一段的峰值。

    沿用的话，重新建仓那一刻就会被判成"已从峰值大幅回撤"，立刻又被卖掉。
    """
    # A 先涨到 130 回撤触发；第 10 天调仓时重新买回（价格 115），之后平着走
    path = [100, 100, 115, 130, 122, 121, 120, 119, 118, 117, 115, 115, 115, 115, 115]
    panel = _panel(path)
    res = _run(panel, rebalance_every=10, trail_arm=0.15, trail_pct=0.05)
    assert res.trailing_exits == 1, "重新建仓后不该被旧峰值立刻再卖一次"
    assert res.weights["A"].iloc[-1] > 0
