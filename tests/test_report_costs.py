"""日报的「今日成本」段。

`plan.md` §8.5 把「成本吃掉 alpha」列为小账户的第一杀手 —— quant-trading
实测 3000 美元账户日频调仓被费用和滑点打到年化 −30%。成本不摆在日报上，
这件事就只能靠回测发现，而回测发现得太晚。
"""

from __future__ import annotations

from qbg.report.daily_report import _trading_costs, render

BASE = {"date": "2026-08-25", "mode": "PAPER", "submitted": True,
        "account": {"total_equity": 200000.0, "available_cash": 190000.0},
        "positions": [], "orders": [], "gates": []}


def _o(code="601398.SH", side="BUY", notional=30000.0, qty=3800, price=7.89):
    return {"code": code, "name": "x", "side": side, "quantity": qty,
            "price": price, "notional": notional, "reason": "test"}


# ---------------------------------------------------------------------------
# 成本计算
# ---------------------------------------------------------------------------
def test_stamp_tax_only_on_sell():
    """A股成本的**全部不对称性**来自这里：印花税只在卖出时收。"""
    buy = _trading_costs([_o(side="BUY", notional=100_000.0)])
    sell = _trading_costs([_o(side="SELL", notional=100_000.0)])
    assert buy["stamp_tax"] == 0.0
    assert sell["stamp_tax"] > 0.0
    # 同样金额，卖出比买入贵大约 5bp（印花税 0.05%）
    assert abs((sell["total"] - buy["total"]) / 100_000.0 * 1e4 - 5.0) < 0.1


def test_round_trip_is_about_10bp():
    """买入再卖出往返约 10bp —— plan.md §5.4 的那个数。"""
    both = _trading_costs([_o(side="BUY", notional=100_000.0),
                           _o(side="SELL", notional=100_000.0)])
    assert 9.0 < both["bp"] * 2 < 11.5


def test_min_commission_hit_is_counted():
    """小额单会触及最低佣金 5 元，实际费率翻倍 —— 这是小账户的隐藏代价。"""
    small = _trading_costs([_o(notional=10_000.0)])     # 万2.5 = 2.5 元 < 5 元
    assert small["min_commission_hits"] == 1
    assert small["commission"] == 5.0


def test_large_order_does_not_hit_min_commission():
    big = _trading_costs([_o(notional=30_000.0)])       # 万2.5 = 7.5 元 > 5 元
    assert big["min_commission_hits"] == 0


def test_buy_and_sell_notional_split():
    totals = _trading_costs([_o(side="BUY", notional=30_000.0),
                             _o(side="SELL", notional=50_000.0)])
    assert totals["buy_notional"] == 30_000.0
    assert totals["sell_notional"] == 50_000.0
    assert totals["notional"] == 80_000.0


def test_zero_and_invalid_orders_ignored():
    """整手取整可能算出 0 股，不能让它污染成本统计或除零。"""
    totals = _trading_costs([_o(notional=0.0), _o(side="", notional=1000.0)])
    assert totals["notional"] == 0.0 and totals["total"] == 0.0
    assert totals["bp"] == 0.0


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def test_section_lists_each_cost_item():
    text = render({**BASE, "allowed_orders": [_o(side="BUY", notional=30_000.0),
                                              _o(side="SELL", notional=50_000.0)]})
    assert "## 今日成本" in text
    for item in ("佣金", "印花税", "过户费", "交易成本合计"):
        assert item in text, item
    assert "bp" in text


def test_llm_cost_is_reported():
    text = render({**BASE, "allowed_orders": [_o()],
                   "agent_usage": {"calls": 60, "total_tokens": 285000,
                                   "cost_usd": 0.2137}})
    assert "LLM 逐票复核" in text
    assert "$0.2137" in text
    assert "285,000" in text


def test_review_tokens_reported_when_present():
    text = render({**BASE, "allowed_orders": [_o()],
                   "daily_review": {"ok": True, "usage": {"total_tokens": 3000}}})
    assert "LLM 自动复盘" in text


def test_no_section_when_nothing_to_report():
    """没订单也没 LLM 调用时不要留一张空表。"""
    assert "## 今日成本" not in render(BASE)


def test_llm_only_run_still_reports():
    """有复核但风控砍光了订单 —— LLM 的钱照样花了，得报出来。"""
    text = render({**BASE, "agent_usage": {"calls": 60, "total_tokens": 285000,
                                           "cost_usd": 0.2137}})
    assert "## 今日成本" in text
    assert "LLM 逐票复核" in text
    assert "交易成本合计" not in text


def test_breakeven_line_present():
    """把成本换算成「要涨多少才回本」比一个绝对数更能说明问题。"""
    text = render({**BASE, "allowed_orders": [_o(side="SELL", notional=100_000.0)]})
    assert "才能打平" in text
