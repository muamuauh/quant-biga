"""k 的八项闸：回测里的资金约束必须和实盘一字不差。全离线。

闸的全部价值在于它测的是**实盘会跑的那条规则**。一手约束要是另写一份、和
`affordable_scores` 有一点出入，闸就是在给一个不存在的策略打分。
"""

from __future__ import annotations

from importlib import util

import pandas as pd
import pytest

from qbg.execution import fees
from qbg.strategy.topk_weights import affordable_scores


@pytest.fixture(scope="module")
def gate():
    spec = util.spec_from_file_location("topk_gate", "scripts/30_topk_gate.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PRICES = {"600000.SH": 9.8, "600026.SH": 20.56, "300274.SZ": 98.0,
          "688041.SH": 226.8, "300661.SZ": 120.0}


@pytest.mark.parametrize("k", [3, 4, 5, 6])
@pytest.mark.parametrize("equity", [40_000.0, 160_000.0])
def test_backtest_mask_matches_live_filter(gate, k, equity):
    """**核心。** 同一天、同一组价，回测的掩码和实盘的过滤必须挑出同一批票。"""
    cap = 0.34
    live = set(affordable_scores(pd.Series(1.0, index=list(PRICES)), PRICES, equity, k, cap=cap).index)
    close = pd.DataFrame([PRICES], index=[pd.Timestamp("2026-10-20")])
    mask = gate.affordable_mask(close, gate.slot_budget(equity, k, cap))
    backtest = {code for code, ok in mask.iloc[0].items() if ok}
    assert backtest == live


def test_k5_at_40k_cannot_buy_the_expensive_picks(gate):
    """4 万、k=5：每槽 7,600，海光信息（226）、圣邦股份（120）、阳光电源（98）都买不起。"""
    budget = gate.slot_budget(40_000.0, 5, 0.34)
    assert round(budget) == 7600
    close = pd.DataFrame([PRICES], index=[pd.Timestamp("2026-10-20")])
    row = gate.affordable_mask(close, budget).iloc[0]
    assert not row["688041.SH"] and not row["300661.SZ"] and not row["300274.SZ"]
    assert row["600026.SH"] and row["600000.SH"]


def test_cap_binds_before_equal_weight(gate):
    """k=2 时 0.95/2 = 47.5%，但单票上限 34% 先卡住。"""
    assert gate.slot_budget(100_000.0, 2, 0.34) == pytest.approx(34_000.0)


def test_minimum_commission_raises_the_effective_rate(gate):
    """`fees.min_notional_for_rate`：成交额低于 5/万2.5 = 2 万时费率近似会低估。"""
    base = fees.FeeProfile()
    assert gate.account_fees(base, 7_600.0).commission_rate == pytest.approx(5 / 7_600)
    assert gate.account_fees(base, 12_667.0).commission_rate > base.commission_rate
    assert gate.account_fees(base, 50_000.0).commission_rate == base.commission_rate, \
        "单槽够大时最低佣金不起作用"


def test_bigger_k_pays_more_per_trade_on_a_small_account(gate):
    """这正是不加这一条闸就会偏向大 k 的原因。"""
    rate = {k: gate.account_fees(fees.FeeProfile(), gate.slot_budget(40_000.0, k, 0.34)).commission_rate
            for k in (3, 4, 5)}
    assert rate[3] < rate[4] < rate[5]


def test_missing_price_is_not_affordable(gate):
    """价格缺失视为买不起 —— 和实盘一致（交给数据新鲜度闸报告）。"""
    close = pd.DataFrame([{"600000.SH": float("nan")}], index=[pd.Timestamp("2026-10-20")])
    assert not gate.affordable_mask(close, 50_000.0).iloc[0]["600000.SH"]
