"""A股交易规则：涨跌停、整手、T+1。全离线。

这三条是本项目相对美股参考仓库的核心增量，写错的后果分别是：
下出无法成交的单、静默不下单让槽位空着、回测收益凭空多出一截。
"""

from __future__ import annotations

import datetime as dt

import pytest

from qbg.market import rules, t1

# ----------------------------------------------------------------------
# 板块识别
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "board"),
    [
        ("600519.SH", "主板"), ("601318.SH", "主板"),
        ("603259.SH", "主板"), ("605499.SH", "主板"),
        ("000001.SZ", "主板"), ("001979.SZ", "主板"),
        ("002594.SZ", "主板"), ("003816.SZ", "主板"),
        ("300750.SZ", "创业板"), ("301269.SZ", "创业板"),
        ("688981.SH", "科创板"), ("689009.SH", "科创板"),
        ("430047.BJ", "北交所"), ("920008.BJ", "北交所"),
    ],
)
def test_board_of(code, board):
    assert rules.board_of(code) == board


# ----------------------------------------------------------------------
# 涨跌停
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("code", "is_st", "pct"),
    [
        ("600519.SH", False, 0.10),
        ("000001.SZ", False, 0.10),
        ("300750.SZ", False, 0.20),
        ("688981.SH", False, 0.20),
        ("430047.BJ", False, 0.30),
        # ST 只在主板收窄到 5%
        ("600519.SH", True, 0.05),
        ("000001.SZ", True, 0.05),
    ],
)
def test_limit_pct(code, is_st, pct):
    assert rules.limit_pct(code, is_st) == pct


def test_st_on_chinext_and_star_is_still_20pct():
    """常见误解：以为所有 ST 都是 ±5%。

    写错会把创业板 ST 股的限价夹到根本不该有的窄区间里，单直接废掉。
    """
    assert rules.limit_pct("300750.SZ", is_st=True) == 0.20
    assert rules.limit_pct("688981.SH", is_st=True) == 0.20


def test_price_limits_main_board():
    lo, hi = rules.price_limits("600519.SH", prev_close=100.0)
    assert (lo, hi) == (90.0, 110.0)


def test_price_limits_chinext():
    lo, hi = rules.price_limits("300750.SZ", prev_close=100.0)
    assert (lo, hi) == (80.0, 120.0)


def test_price_limits_st_main_board():
    lo, hi = rules.price_limits("600518.SH", prev_close=100.0, is_st=True)
    assert (lo, hi) == (95.0, 105.0)


def test_round_price_uses_half_up_not_bankers_rounding():
    """Python 内建 round() 是银行家舍入：round(2.675, 2) == 2.67。

    涨跌停价差一分钱就是"这个限价根本无法成交"，所以必须用四舍五入。
    """
    assert rules.round_price(2.675) == 2.68
    assert rules.round_price(2.665) == 2.67
    assert round(2.675, 2) == 2.67          # 对照：内建的行为不同


def test_price_limits_rounds_to_cent():
    # 13.09 × 1.1 = 14.399，四舍五入到 14.40
    lo, hi = rules.price_limits("600519.SH", prev_close=13.09)
    assert hi == 14.40
    assert lo == 11.78


def test_price_limits_rejects_nonpositive_prev_close():
    for bad in (0, -1, None):
        with pytest.raises(ValueError):
            rules.price_limits("600519.SH", prev_close=bad)


def test_clamp_to_limits():
    """让价后的限价可能越过涨跌停，交易所会直接废单。"""
    # 想按 115 买，但涨停是 110
    assert rules.clamp_to_limits(115.0, "600519.SH", prev_close=100.0) == 110.0
    # 想按 85 卖，但跌停是 90
    assert rules.clamp_to_limits(85.0, "600519.SH", prev_close=100.0) == 90.0
    # 区间内不动
    assert rules.clamp_to_limits(105.0, "600519.SH", prev_close=100.0) == 105.0


def test_at_limit_up_and_down():
    assert rules.at_limit_up(110.0, "600519.SH", prev_close=100.0) is True
    assert rules.at_limit_up(109.99, "600519.SH", prev_close=100.0) is False
    assert rules.at_limit_down(90.0, "600519.SH", prev_close=100.0) is True
    assert rules.at_limit_down(90.01, "600519.SH", prev_close=100.0) is False


# ----------------------------------------------------------------------
# 整手
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("shares", "expected"),
    [(0, 0), (99, 0), (100, 100), (199, 100), (250, 200), (1000, 1000), (-5, 0)],
)
def test_round_lot_down(shares, expected):
    assert rules.round_lot_down(shares) == expected


def test_affordable_shares_10w_account():
    """10 万账户 k=3 → 单槽 3 万 → 一手买得起的上限约 300 元/股。"""
    budget = 30_000.0
    assert rules.affordable_shares(budget, 100.0) == 300   # 3 手
    assert rules.affordable_shares(budget, 299.0) == 100   # 刚好 1 手
    assert rules.affordable_shares(budget, 301.0) == 0     # 一手都买不起


def test_affordable_shares_boundary_exactly_one_lot():
    assert rules.affordable_shares(30_000.0, 300.0) == 100
    assert rules.affordable_shares(29_999.0, 300.0) == 0


def test_affordable_shares_guards():
    assert rules.affordable_shares(0, 10.0) == 0
    assert rules.affordable_shares(1000, 0) == 0
    assert rules.affordable_shares(None, 10.0) == 0


def test_is_valid_buy_qty():
    assert rules.is_valid_buy_qty(100) is True
    assert rules.is_valid_buy_qty(250) is False
    assert rules.is_valid_buy_qty(0) is False
    assert rules.is_valid_buy_qty(-100) is False


def test_normalize_sell_qty_full_lots():
    assert rules.normalize_sell_qty(200, holding=500) == 200


def test_normalize_sell_qty_avoids_leaving_odd_lot():
    """留一个 37 股的尾巴，下次清它还要单独付一次最低 5 元佣金。"""
    # 持仓 537，想卖 500 → 会留 37 股零股 → 干脆全卖
    assert rules.normalize_sell_qty(500, holding=537) == 537


def test_normalize_sell_qty_allows_clearing_odd_lot_holding():
    """持仓本身就是零股时，清仓是合法的（零股必须一次性卖完）。"""
    assert rules.normalize_sell_qty(37, holding=37) == 37


def test_normalize_sell_qty_rounds_partial_sell_to_lots():
    # 持仓 1000，想卖 250 → 取整到 200，剩 800 仍是整手
    assert rules.normalize_sell_qty(250, holding=1000) == 200


def test_normalize_sell_qty_guards():
    assert rules.normalize_sell_qty(100, holding=0) == 0
    assert rules.normalize_sell_qty(0, holding=100) == 0
    assert rules.normalize_sell_qty(999, holding=100) == 100   # 不能超卖


# ----------------------------------------------------------------------
# T+1
# ----------------------------------------------------------------------


def test_sellable_from_trades():
    assert t1.sellable_from_trades(holding=1000, bought_today=0) == 1000
    assert t1.sellable_from_trades(holding=1000, bought_today=300) == 700
    assert t1.sellable_from_trades(holding=300, bought_today=300) == 0


def test_sellable_from_trades_clamps_to_zero():
    """当天买了又卖导致的负可卖量没有意义，按 0 处理是安全的一侧。"""
    assert t1.sellable_from_trades(holding=100, bought_today=500) == 0


def test_is_sellable_by_buy_date():
    assert t1.is_sellable("2026-08-10", asof="2026-08-10") is False   # 当日买入
    assert t1.is_sellable("2026-08-07", asof="2026-08-10") is True
    assert t1.is_sellable(None, asof="2026-08-10") is True            # 历史持仓


class _Pos:
    def __init__(self, code, qty, sellable_qty=None, buy_date=None):
        self.code, self.qty = code, qty
        self.sellable_qty, self.buy_date = sellable_qty, buy_date


def test_apply_to_positions_prefers_broker_sellable():
    """券商给的可卖量是权威值，优先于按日期推算。"""
    pos = [_Pos("600519.SH", 1000, sellable_qty=700, buy_date="2026-08-10")]
    assert t1.apply_to_positions(pos, "2026-08-10") == {"600519.SH": 700}


def test_apply_to_positions_falls_back_to_buy_date():
    pos = [
        _Pos("600519.SH", 1000, buy_date="2026-08-10"),   # 当日买入 → 0
        _Pos("000858.SZ", 500, buy_date="2026-08-07"),    # 之前买的 → 全可卖
    ]
    assert t1.apply_to_positions(pos, "2026-08-10") == {
        "600519.SH": 0, "000858.SZ": 500,
    }


def test_apply_to_positions_clamps_broker_value_to_holding():
    pos = [_Pos("600519.SH", 100, sellable_qty=999)]
    assert t1.apply_to_positions(pos, "2026-08-10") == {"600519.SH": 100}


def test_frozen_shares_only_lists_actually_frozen():
    pos = [
        _Pos("600519.SH", 1000, sellable_qty=700),
        _Pos("000858.SZ", 500, sellable_qty=500),
    ]
    assert t1.frozen_shares(pos, dt.date(2026, 8, 10)) == {"600519.SH": 300}
