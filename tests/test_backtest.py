"""回测引擎与指标。全离线，用**手造的已知答案样本**。

这是 P2 最重要的验收：四条 A股约束每一条都必须能被单独证明生效。
少任何一条，回测收益都会凭空多出一截，而且不会有任何报错。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from qbg.backtest import engine, metrics
from qbg.backtest.engine import Panel
from qbg.execution.fees import FeeProfile

PROFILE = FeeProfile()
DATES = pd.date_range("2026-08-03", periods=6, freq="D")
INSTS = ["600519.SH", "000858.SZ", "300750.SZ"]


def make_panel(open_px, suspended=None, limit_up=None, limit_down=None) -> Panel:
    idx, cols = DATES[: len(open_px)], INSTS
    op = pd.DataFrame(open_px, index=idx, columns=cols, dtype=float)

    def flags(v):
        if v is None:
            return pd.DataFrame(False, index=idx, columns=cols)
        return pd.DataFrame(v, index=idx, columns=cols)

    return Panel(
        open_px=op,
        close_px=op.copy(),
        suspended=flags(suspended),
        limit_up_open=flags(limit_up),
        limit_down_open=flags(limit_down),
    )


def flat_scores(values, n_days) -> pd.DataFrame:
    return pd.DataFrame([values] * n_days, index=DATES[:n_days], columns=INSTS, dtype=float)


# ----------------------------------------------------------------------
# 指标
# ----------------------------------------------------------------------


def test_trading_days_is_a_share_not_us():
    """A股一年约 244 个交易日。抄美股的 252 会让年化高估约 3%。"""
    assert metrics.TRADING_DAYS == 244


def test_equity_curve_compounds():
    r = pd.Series([0.1, 0.1])
    assert metrics.equity_curve(r).tolist() == pytest.approx([1.1, 1.21])


def test_max_drawdown():
    curve = pd.Series([1.0, 1.5, 0.75, 1.2])
    assert metrics.max_drawdown(curve) == pytest.approx(-0.5)


def test_max_drawdown_of_monotone_rise_is_zero():
    assert metrics.max_drawdown(pd.Series([1.0, 1.1, 1.2])) == 0.0


def test_compute_metrics_on_constant_return():
    r = pd.Series([0.001] * metrics.TRADING_DAYS)
    m = metrics.compute_metrics(r)
    assert m.n_days == metrics.TRADING_DAYS
    assert m.annual_return == pytest.approx(1.001**244 - 1, rel=1e-6)
    assert m.max_drawdown == 0.0
    assert m.win_rate == 1.0
    assert m.sharpe == 0.0        # 零波动 → 夏普无定义，报 0


def test_compute_metrics_empty():
    m = metrics.compute_metrics(pd.Series(dtype=float))
    assert m.n_days == 0 and m.sharpe == 0.0


def test_turnover_halves_the_weight_delta():
    """一次调仓同时产生买和卖，不除以 2 会把换手算成两倍。"""
    w = pd.DataFrame({"a": [1.0, 0.0], "b": [0.0, 1.0]})
    assert metrics.turnover(w).tolist() == pytest.approx([0.0, 1.0])


def test_information_coefficient_perfect_and_inverted():
    idx = pd.MultiIndex.from_product(
        [pd.to_datetime(["2026-08-03"]), ["a", "b", "c"]],
        names=["datetime", "instrument"])
    pred = pd.Series([1.0, 2.0, 3.0], index=idx)
    ic, ric = metrics.information_coefficient(pred, pd.Series([1.0, 2.0, 3.0], index=idx))
    assert ic == pytest.approx(1.0) and ric == pytest.approx(1.0)
    ic, ric = metrics.information_coefficient(pred, pd.Series([3.0, 2.0, 1.0], index=idx))
    assert ic == pytest.approx(-1.0) and ric == pytest.approx(-1.0)


# ----------------------------------------------------------------------
# 目标权重：前视偏差
# ----------------------------------------------------------------------


def test_target_weights_are_shifted_by_one_day():
    """t 日收盘后的分数决定 t+1 日开盘的持仓。不 shift 就是前视偏差。"""
    scores = pd.DataFrame(
        [[3.0, 1.0, 2.0], [1.0, 3.0, 2.0]],
        index=DATES[:2], columns=INSTS)
    w = engine.target_weights_from_scores(scores, k=1, total_weight=1.0)
    # 第一天没有前一天的分数 → 空仓
    assert w.iloc[0].sum() == 0.0
    # 第二天持有的是**第一天**分数最高的票
    assert w.iloc[1]["600519.SH"] == pytest.approx(1.0)


def test_target_weights_equal_weight_top_k():
    scores = flat_scores([3.0, 2.0, 1.0], 3)
    w = engine.target_weights_from_scores(scores, k=2, total_weight=0.9)
    row = w.iloc[-1]
    assert row["600519.SH"] == pytest.approx(0.45)
    assert row["000858.SZ"] == pytest.approx(0.45)
    assert row["300750.SZ"] == 0.0


def test_fixed_slots_leaves_unfilled_slots_in_cash():
    """阈值型打分：合格的票不足 k 只时，剩下的槽位是**现金**，不是加仓。

    默认（排序型）按实际选中数归一，一只票就吃满仓位；阈值型必须按 k 归一。
    这是两条完全不同的曲线，混用等于给回测偷偷加杠杆。
    """
    scores = flat_scores([3.0, np.nan, np.nan], 3)

    spread = engine.target_weights_from_scores(scores, k=3, total_weight=0.9)
    assert spread.iloc[-1]["600519.SH"] == pytest.approx(0.9)

    slots = engine.target_weights_from_scores(scores, k=3, total_weight=0.9, fixed_slots=True)
    assert slots.iloc[-1]["600519.SH"] == pytest.approx(0.3)
    assert slots.iloc[-1].sum() == pytest.approx(0.3)


def test_fixed_slots_is_a_noop_when_all_slots_fill():
    """候选够 k 只时两种归一方式必须逐位一致 —— 否则模型那条线也会被改动。"""
    scores = flat_scores([3.0, 2.0, 1.0], 3)
    a = engine.target_weights_from_scores(scores, k=2, total_weight=0.9)
    b = engine.target_weights_from_scores(scores, k=2, total_weight=0.9, fixed_slots=True)
    pd.testing.assert_frame_equal(a, b)


def test_hysteresis_keeps_a_holding_that_slipped_out_of_top_k():
    """迟滞的全部意义：掉到第 k 名之外但还在 keep_rank 内，不换人。

    没有它，排名每天抖一下就换一次仓，而换手是这套系统里最贵的东西。
    """
    # 分数在第3天翻转：600519 从第1名掉到第3名，仍在 keep_rank=3 内。
    # 信号 shift 一天，所以这个翻转要到第4天开盘才可能被执行。
    scores = pd.DataFrame(
        [[3.0, 2.0, 1.0],
         [3.0, 2.0, 1.0],
         [1.0, 3.0, 2.0],
         [1.0, 3.0, 2.0],
         [1.0, 3.0, 2.0]],
        index=DATES[:5], columns=INSTS)
    panel = make_panel([[10.0, 10.0, 10.0]] * 5)

    plain = engine.run_backtest(scores, panel, k=1, total_weight=1.0,
                                slippage_grid=())
    kept = engine.run_backtest(scores, panel, k=1, total_weight=1.0, keep_rank=3,
                               slippage_grid=())
    # 无迟滞：换成当日第1名 000858；有迟滞：600519 还在前3名内，不动
    assert plain.weights.iloc[-1]["000858.SZ"] == pytest.approx(1.0)
    assert kept.weights.iloc[-1]["600519.SH"] == pytest.approx(1.0)
    assert kept.avg_turnover < plain.avg_turnover


def test_hysteresis_with_keep_rank_equal_k_matches_plain_topk():
    """keep_rank <= k 时迟滞不该改变任何东西——否则它悄悄换了一套选股逻辑。"""
    scores = pd.DataFrame(
        [[3.0, 2.0, 1.0], [1.0, 3.0, 2.0], [2.0, 1.0, 3.0], [3.0, 1.0, 2.0],
         [1.0, 2.0, 3.0]],
        index=DATES[:5], columns=INSTS)
    panel = make_panel([[10.0, 11.0, 12.0]] * 5)
    plain = engine.run_backtest(scores, panel, k=2, slippage_grid=())
    kept = engine.run_backtest(scores, panel, k=2, keep_rank=2, slippage_grid=())
    pd.testing.assert_frame_equal(plain.weights, kept.weights)


def test_rebalance_phase_shifts_which_days_are_traded():
    """相位决定在哪些天调仓。固定相位会把"持有期"和"碰巧哪天下单"混在一起。"""
    scores = pd.DataFrame(
        [[3.0, 2.0, 1.0], [1.0, 3.0, 2.0], [2.0, 1.0, 3.0], [3.0, 1.0, 2.0],
         [1.0, 2.0, 3.0], [2.0, 3.0, 1.0]],
        index=DATES[:6], columns=INSTS)
    panel = make_panel([[10.0, 11.0, 12.0]] * 6)
    a = engine.run_backtest(scores, panel, k=1, rebalance_every=2,
                            rebalance_phase=0, slippage_grid=())
    b = engine.run_backtest(scores, panel, k=1, rebalance_every=2,
                            rebalance_phase=1, slippage_grid=())
    assert not a.weights.equals(b.weights)


def test_rebalance_phase_is_a_noop_at_daily_rebalance():
    """每日调仓时每天都调，相位无从谈起 —— 不该悄悄改变基准臂。"""
    scores = flat_scores([3.0, 2.0, 1.0], 4)
    panel = make_panel([[10.0, 11.0, 12.0]] * 4)
    a = engine.run_backtest(scores, panel, k=2, rebalance_phase=0, slippage_grid=())
    b = engine.run_backtest(scores, panel, k=2, rebalance_phase=3, slippage_grid=())
    pd.testing.assert_frame_equal(a.weights, b.weights)


def test_target_weights_skip_untradable():
    """停牌股排进 top-K 只会白占一个槽位。"""
    scores = flat_scores([3.0, 2.0, 1.0], 3)
    tradable = pd.DataFrame(True, index=DATES[:3], columns=INSTS)
    tradable["600519.SH"] = False
    w = engine.target_weights_from_scores(scores, k=1, total_weight=1.0, tradable=tradable)
    assert w.iloc[-1]["000858.SZ"] == pytest.approx(1.0)
    assert w.iloc[-1]["600519.SH"] == 0.0


# ----------------------------------------------------------------------
# 四条 A股约束 —— 每条单独证明
# ----------------------------------------------------------------------


def test_constraint_limit_up_blocks_buying():
    """约束 2a：开盘一字涨停买不进，钱留在现金里。"""
    w_prev = pd.Series({"a": 0.0, "b": 0.5})
    w_target = pd.Series({"a": 0.5, "b": 0.5})
    limit_up = pd.Series({"a": True, "b": False})
    false = pd.Series({"a": False, "b": False})

    w_new, blocked = engine._step_weights(w_prev, w_target, false, limit_up, false)
    assert w_new["a"] == 0.0                      # 没买进
    assert blocked["limit_up_cannot_buy"] == 1
    # 买不进的钱不会被重新分配给 b —— 真实情况就是那笔钱没投出去
    assert w_new["b"] == 0.5


def test_constraint_limit_up_does_not_block_selling():
    """涨停只挡买入。想卖的时候涨停反而是好事，必须放行。"""
    w_prev = pd.Series({"a": 0.5})
    w_target = pd.Series({"a": 0.0})
    w_new, blocked = engine._step_weights(
        w_prev, w_target, pd.Series({"a": False}),
        pd.Series({"a": True}), pd.Series({"a": False}))
    assert w_new["a"] == 0.0
    assert blocked["limit_up_cannot_buy"] == 0


def test_constraint_limit_down_blocks_selling():
    """约束 2b：开盘跌停卖不出，被迫继续持有。"""
    w_prev = pd.Series({"a": 0.5})
    w_target = pd.Series({"a": 0.0})
    w_new, blocked = engine._step_weights(
        w_prev, w_target, pd.Series({"a": False}),
        pd.Series({"a": False}), pd.Series({"a": True}))
    assert w_new["a"] == 0.5                      # 卖不掉
    assert blocked["limit_down_cannot_sell"] == 1


def test_constraint_suspension_freezes_weight():
    """约束 3：停牌不能交易，权重冻结。"""
    w_prev = pd.Series({"a": 0.3})
    w_target = pd.Series({"a": 0.9})
    w_new, blocked = engine._step_weights(
        w_prev, w_target, pd.Series({"a": True}),
        pd.Series({"a": False}), pd.Series({"a": False}))
    assert w_new["a"] == 0.3
    assert blocked["suspended"] == 1


def test_constraint_suspension_zeroes_return():
    """停牌期间价格不动。不置零的话，复牌的跳空会被摊到停牌那天，
    凭空造出一根大阳线。"""
    op = [[10.0, 10.0, 10.0], [10.0, 10.0, 10.0], [20.0, 10.0, 10.0]]
    susp = [[False, False, False], [True, False, False], [False, False, False]]
    panel = make_panel(op, suspended=susp)
    ret = panel.open_to_open_returns()
    assert ret.iloc[1]["600519.SH"] == 0.0        # 停牌日收益 0
    assert ret.iloc[0]["600519.SH"] == 0.0        # 10 → 10


def test_constraint_t1_is_structural_in_open_to_open_model():
    """约束 1：每日开盘调一次仓的模型里，T+1 是结构性满足的。

    t 日开盘买入的票，最早也要 t+1 日开盘才可能卖出——因为这个模型里
    根本不存在"同一天的第二次交易"。这个测试把这个不变式钉住：将来若
    加入日内止损，它会失败，提醒实现者必须显式处理 T+1。
    """
    panel = make_panel([[10.0] * 3] * 4)
    scores = flat_scores([3.0, 2.0, 1.0], 4)
    res = engine.run_backtest(scores, panel, k=1, fee_profile=PROFILE)
    # 每个持有期恰好是一个交易日，不存在日内的买后即卖
    assert len(res.weights) == len(panel.dates) - 1


def test_constraint_asymmetric_fees_sell_costs_more():
    """约束 4：卖出多 5bp 印花税。对称成本会系统性低估卖出成本。"""
    assert engine.fees.cost_rate("SELL", PROFILE) > engine.fees.cost_rate("BUY", PROFILE)
    # 建仓再清仓：成本应等于 买入费率 + 卖出费率，而不是 2×买入费率
    op = [[10.0] * 3] * 4
    panel = make_panel(op)
    scores = pd.DataFrame(
        [[3.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 1.0], [1.0, 1.0, 1.0]],
        index=DATES[:4], columns=INSTS)
    res = engine.run_backtest(scores, panel, k=1, total_weight=1.0, fee_profile=PROFILE)
    assert res.buy_cost_rate == pytest.approx(0.00026)
    assert res.sell_cost_rate == pytest.approx(0.00076)


# ----------------------------------------------------------------------
# 成交假设：开盘价而非收盘价
# ----------------------------------------------------------------------


def test_returns_are_open_to_open():
    """盘后出信号、次日开盘执行，中间隔一个跳空。用收盘价会白送这段收益。"""
    op = [[10.0, 1.0, 1.0], [11.0, 1.0, 1.0], [12.1, 1.0, 1.0]]
    panel = make_panel(op)
    ret = panel.open_to_open_returns()
    assert ret.iloc[0]["600519.SH"] == pytest.approx(0.1)   # 10 → 11
    assert ret.iloc[1]["600519.SH"] == pytest.approx(0.1)   # 11 → 12.1
    assert np.isnan(ret.iloc[2]["600519.SH"])               # 没有下一天


def test_known_answer_single_stock_no_costs():
    """已知答案：满仓单只票，涨 10% 两期，零成本 → 净值 1.21。"""
    op = [[10.0, 1.0, 1.0], [11.0, 1.0, 1.0], [12.1, 1.0, 1.0], [13.31, 1.0, 1.0]]
    panel = make_panel(op)
    scores = flat_scores([3.0, 1.0, 2.0], 4)
    zero_fee = FeeProfile(commission_rate=0, commission_min=0,
                          stamp_tax_rate=0, transfer_fee_rate=0)
    res = engine.run_backtest(scores, panel, k=1, total_weight=1.0,
                              fee_profile=zero_fee, slippage_grid=())
    # 第一期空仓（没有前一天的分数），后两期各涨 10%
    assert res.daily_returns.iloc[0] == pytest.approx(0.0)
    assert res.daily_returns.iloc[1] == pytest.approx(0.1)
    assert res.daily_returns.iloc[2] == pytest.approx(0.1)
    assert res.strategy_curve.iloc[-1] == pytest.approx(1.21)


def test_costs_reduce_returns_by_turnover_times_rate():
    """建仓那天的成本应精确等于 换手 × 买入费率。"""
    op = [[10.0] * 3] * 3
    panel = make_panel(op)
    scores = flat_scores([3.0, 1.0, 2.0], 3)
    res = engine.run_backtest(scores, panel, k=1, total_weight=1.0,
                              fee_profile=PROFILE, slippage_grid=())
    # 第二期建仓，权重 0 → 1，买入换手 1.0
    assert res.daily_returns.iloc[1] == pytest.approx(-engine.fees.cost_rate("BUY", PROFILE))


# ----------------------------------------------------------------------
# 滑点敏感性 / 基准 / 汇总
# ----------------------------------------------------------------------


def test_slippage_curve_is_monotonically_worse():
    op = [[10.0, 11.0, 9.0], [11.0, 10.0, 10.0], [12.0, 9.0, 11.0], [11.0, 12.0, 10.0]]
    panel = make_panel(op)
    scores = pd.DataFrame(np.random.RandomState(0).randn(4, 3),
                          index=DATES[:4], columns=INSTS)
    res = engine.run_backtest(scores, panel, k=1, fee_profile=PROFILE)
    returns = [res.slippage_curve[s].total_return for s in (0.0, 0.001, 0.002, 0.003)]
    assert returns == sorted(returns, reverse=True)


def test_benchmark_is_equal_weight_buy_and_hold():
    op = [[10.0, 10.0, 10.0], [11.0, 11.0, 11.0], [12.1, 12.1, 12.1]]
    panel = make_panel(op)
    scores = flat_scores([3.0, 2.0, 1.0], 3)
    res = engine.run_backtest(scores, panel, k=1, fee_profile=PROFILE, slippage_grid=())
    assert res.benchmark.total_return == pytest.approx(0.21)   # 等权买入持有


def test_blocked_counts_are_reported():
    """被拦下的调仓次数大 = 策略想做的事有很大一部分在真实市场做不到，
    回测结论要打折看。所以必须报出来。"""
    op = [[10.0] * 3] * 4
    limit_up = [[False] * 3, [True, False, False], [False] * 3, [False] * 3]
    panel = make_panel(op, limit_up=limit_up)
    scores = flat_scores([3.0, 1.0, 2.0], 4)
    res = engine.run_backtest(scores, panel, k=1, fee_profile=PROFILE, slippage_grid=())
    assert res.blocked["limit_up_cannot_buy"] >= 1


def test_result_as_dict_is_json_friendly():
    op = [[10.0] * 3] * 3
    panel = make_panel(op)
    res = engine.run_backtest(flat_scores([3.0, 2.0, 1.0], 3), panel,
                              k=1, fee_profile=PROFILE, slippage_grid=(0.0,))
    d = res.as_dict()
    assert set(d) >= {"strategy", "benchmark", "rank_ic", "blocked", "avg_turnover"}
    assert isinstance(d["blocked"], dict)


def test_reproducible_across_runs():
    """固定输入跑两遍，指标必须逐位一致——不一致说明有未受控的状态。"""
    op = [[10.0, 11.0, 9.0], [11.0, 10.0, 10.0], [12.0, 9.0, 11.0], [11.0, 12.0, 10.0]]
    panel = make_panel(op)
    scores = flat_scores([3.0, 2.0, 1.0], 4)
    a = engine.run_backtest(scores, panel, k=2, fee_profile=PROFILE)
    b = engine.run_backtest(scores, panel, k=2, fee_profile=PROFILE)
    assert a.strategy.as_dict() == b.strategy.as_dict()
    assert a.rank_ic == b.rank_ic
