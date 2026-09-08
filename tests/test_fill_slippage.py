"""实测成交滑点。全部离线：给手造的成交表和参照价，不碰券商。

这一层量的是**回测里唯一没被实测过的输入** —— "能按次日开盘价成交"。
它不是可调参数，是一个乘在所有收益上的系统性偏移。
"""

from __future__ import annotations

import pandas as pd
import pytest

from qbg.portfolio.reconcile import fill_slippage, slippage_summary

REF = {"600519": 100.0, "000858": 50.0}


def trade(code="600519", side="买入", qty=100, price=100.0, key="成交均价"):
    return {"证券代码": code, "操作": side, "成交数量": qty, key: price}


def test_buying_above_open_is_positive_cost():
    """符号约定：买贵了记正。正数就是成本，可以直接和费率相加。"""
    out = fill_slippage([trade(price=100.5)], REF)
    assert out.iloc[0]["slippage_bp"] == pytest.approx(50.0)


def test_selling_below_open_is_also_positive_cost():
    """卖便宜了同样是吃亏 —— 两个方向都要归一到"正 = 成本"。"""
    out = fill_slippage([trade(side="卖出", price=99.5)], REF)
    assert out.iloc[0]["slippage_bp"] == pytest.approx(50.0)


def test_selling_above_open_is_negative_cost():
    """卖贵了是占便宜，必须记负，否则平均值会被系统性抬高。"""
    out = fill_slippage([trade(side="卖出", price=100.5)], REF)
    assert out.iloc[0]["slippage_bp"] == pytest.approx(-50.0)


def test_missing_reference_price_is_skipped_not_zeroed():
    """参照价拿不到要**跳过**。记 0 会把"不知道"混进平均值，把滑点拉低 ——
    而那正是要量的那个数。"""
    out = fill_slippage([trade(code="999999", price=100.5)], REF)
    assert out.empty


def test_price_column_name_variants_are_accepted():
    """成交表列名在券商/版本间会变，按语义找列而不是按字面名。"""
    for key in ("成交均价", "成交价格", "成交价"):
        out = fill_slippage([trade(price=101.0, key=key)], REF)
        assert len(out) == 1, key
        assert out.iloc[0]["slippage_bp"] == pytest.approx(100.0)


def test_rows_without_a_usable_side_are_dropped():
    """认不出买卖方向就没法定符号，宁可丢掉也不要猜。"""
    assert fill_slippage([trade(side="其他")], REF).empty


def test_summary_is_weighted_by_notional_not_by_count():
    """回测的 extra_slippage 乘的是**换手金额**，等权平均会让小单说话太响。

    一笔 100 股 @100（1 万元）滑点 +100bp，一笔 100 股 @50（5 千元）滑点 0：
    等权是 +50bp，按金额加权是 +66.7bp。要的是后者。
    """
    frame = fill_slippage(
        [trade(price=101.0), trade(code="000858", price=50.0)], REF)
    s = slippage_summary(frame)
    assert s["n"] == 2
    assert s["median_bp"] == pytest.approx(50.0)
    assert s["weighted_bp"] == pytest.approx(100.0 * 10100 / (10100 + 5000))
    assert s["weighted_bp"] > s["median_bp"]


def test_summary_of_empty_frame_says_unknown_not_zero():
    """没有样本时必须报 None。返回 0 会被下游当成"实测滑点为零"。"""
    s = slippage_summary(pd.DataFrame())
    assert s["n"] == 0 and s["weighted_bp"] is None and s["buy_bp"] is None


def test_buy_and_sell_are_reported_separately():
    """A股费率不对称（卖出多印花税），滑点也没理由对称 —— 分开报。"""
    s = slippage_summary(fill_slippage(
        [trade(price=101.0), trade(side="卖出", price=99.0)], REF))
    assert s["buy_bp"] == pytest.approx(100.0)
    assert s["sell_bp"] == pytest.approx(100.0)
