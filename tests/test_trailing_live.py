"""移动止盈的实盘一侧：峰值库、触发判据、卖单、日报。全离线。

引擎一侧（回测）见 test_trailing_take_profit.py。
"""

from __future__ import annotations

import inspect

from qbg.orchestrator import daily_cycle
from qbg.report import daily_report
from qbg.risk import trailing


def _pos(code="600000.SH", qty=1000, sellable=1000, cost=10.0, last=11.0, name="浦发银行"):
    return {"code": code, "name": name, "qty": qty, "sellable_qty": sellable,
            "cost_price": cost, "last_price": last, "market_value": qty * last}


# ----------------------------------------------------------------------
# 峰值库
# ----------------------------------------------------------------------

def test_peak_rises_with_price_and_never_falls():
    peaks = trailing.update_peaks([_pos(last=12.0)], {})
    assert peaks["600000.SH"] == 12.0
    peaks = trailing.update_peaks([_pos(last=11.0)], peaks)
    assert peaks["600000.SH"] == 12.0, "峰值只升不降"


def test_peak_is_dropped_when_no_longer_held():
    """卖掉再买回，峰值必须从新建仓重算。沿用旧峰值 = 买进来立刻被判成大幅回撤。"""
    peaks = trailing.update_peaks([_pos(code="A", last=20.0), _pos(code="B", last=5.0)], {})
    peaks = trailing.update_peaks([_pos(code="B", last=5.0)], peaks)
    assert "A" not in peaks


def test_missing_price_keeps_the_old_peak():
    """现价暂时读不到时保留旧峰值。归零 = 下次从头算 = 漏掉一次该有的触发。"""
    peaks = trailing.update_peaks([_pos(last=0.0)], {"600000.SH": 13.0})
    assert peaks == {"600000.SH": 13.0}


def test_peak_is_at_least_cost():
    """买进来就跌的票，峰值不该低于成本 —— 否则"峰值浮盈"是负数，日报读着很怪。"""
    peaks = trailing.update_peaks([_pos(cost=10.0, last=9.0)], {})
    assert peaks["600000.SH"] == 10.0


def test_peaks_roundtrip_and_corrupt_file(tmp_path):
    path = tmp_path / "peaks.json"
    trailing.save_peaks({"600000.SH": 12.5}, path)
    assert trailing.load_peaks(path) == {"600000.SH": 12.5}
    path.write_text("{半截", encoding="utf-8")
    assert trailing.load_peaks(path) == {}, "坏文件应当当作没有，不能打断日流程"


# ----------------------------------------------------------------------
# 触发判据
# ----------------------------------------------------------------------

def test_armed_and_retraced_triggers():
    # 成本 10，峰值 12（+20% 上膛），现价 11.3（从峰值回撤 5.8%）
    hits = trailing.triggered([_pos(cost=10, last=11.3)], {"600000.SH": 12.0}, 0.15, 0.05)
    assert [h.code for h in hits] == ["600000.SH"]
    assert "移动止盈" in hits[0].reason()


def test_not_armed_never_triggers():
    """峰值只到 +10%，回撤再多也不管 —— 那是止损的事。"""
    hits = trailing.triggered([_pos(cost=10, last=8.0)], {"600000.SH": 11.0}, 0.15, 0.05)
    assert hits == []


def test_small_retrace_does_not_trigger():
    hits = trailing.triggered([_pos(cost=10, last=11.8)], {"600000.SH": 12.0}, 0.15, 0.05)
    assert hits == []


def test_zero_params_mean_off():
    assert trailing.triggered([_pos(cost=10, last=11.3)], {"600000.SH": 12.0}, 0, 0.05) == []
    assert trailing.triggered([_pos(cost=10, last=11.3)], {"600000.SH": 12.0}, 0.15, 0) == []


# ----------------------------------------------------------------------
# 卖单
# ----------------------------------------------------------------------

def _hit(qty=1000, sellable=1000):
    return trailing.triggered([_pos(qty=qty, sellable=sellable, cost=10, last=11.3)],
                              {"600000.SH": 12.0}, 0.15, 0.05)


def test_sell_quantity_is_capped_to_sellable():
    """**按可卖量下，不按持有量。** t1_guard 失败时卖单照样放行，超出的部分会被
    券商 T+1 静默拒绝 —— 所以必须在这里就地截断，不能指望闸。"""
    orders, skipped = trailing.sell_orders(_hit(qty=1000, sellable=600),
                                           {"600000.SH": 11.3}, {"600000.SH": 11.5}, {}, 0.005)
    assert skipped == []
    assert orders[0].quantity == 600
    assert orders[0].side == "SELL"


def test_zero_sellable_is_skipped_with_a_reason():
    """同一天重跑的第二道保险：挂单会冻结可卖量，这时不该再下一笔。"""
    orders, skipped = trailing.sell_orders(_hit(sellable=0),
                                           {"600000.SH": 11.3}, {"600000.SH": 11.5}, {}, 0.005)
    assert orders == []
    assert "T+1" in skipped[0]["reason"]


def test_limit_price_concedes_and_stays_inside_limits():
    orders, _ = trailing.sell_orders(_hit(), {"600000.SH": 11.30}, {"600000.SH": 11.50},
                                     {}, 0.005)
    assert orders[0].price < 11.30, "卖单要往下让价"
    assert orders[0].price >= round(11.50 * 0.9, 2), "不能低于跌停价"


# ----------------------------------------------------------------------
# 接线：顺序和"不许做的事"
# ----------------------------------------------------------------------

def test_peaks_refresh_before_the_rebalance_check():
    """峰值刷新必须在判调仓日**之前**。调仓日不强卖，但不刷峰值就会过期。"""
    src = inspect.getsource(daily_cycle.run_daily)
    assert src.index("trailing.update_peaks") < src.index("is_rebalance_day(")


def test_trailing_never_resets_the_rebalance_clock():
    """止盈卖出**不是调仓**。写了调仓日期会把 10 日周期重置掉。"""
    src = inspect.getsource(daily_cycle._run_trailing)
    assert "save_rebalance_date" not in src
    assert "save_marker" in src, "应当写当日完成标记，防同一天重跑重复卖"


def test_trailing_goes_through_the_gates():
    """硬闸语义不许为某一类订单开口子（CLAUDE.md §三）。"""
    src = inspect.getsource(daily_cycle._run_trailing)
    assert "run_all_gates" in src
    assert src.index("run_all_gates") < src.index("_submit_to_broker")


def test_degraded_portfolio_does_not_touch_peaks():
    """降级时不刷新峰值：落到默认账户时持仓是空的，刷新会把整个峰值库清空。"""
    src = inspect.getsource(daily_cycle.run_daily)
    guard = src[src.index("trailing.update_peaks") - 200: src.index("trailing.update_peaks")]
    assert "degraded is None" in guard


# ----------------------------------------------------------------------
# 日报
# ----------------------------------------------------------------------

def _monitoring_result(**over):
    base = {"date": "2026-09-15", "mode": "PAPER", "skipped_reason": "not_rebalance_day",
            "run_kind": "monitoring", "hard_ok": True, "orders": [], "allowed_orders": [],
            "rebalance": {"every_days": 10, "last": "2026-09-11", "days_since": 2,
                          "is_today": False, "next": "2026-09-25", "next_after_today": None,
                          "cash_fraction": 0.21, "cash_trigger": 0.5, "cash_triggered": False},
            "trailing": {"enabled": True, "arm_pct": 0.15, "trail_pct": 0.05,
                         "hits": [], "orders": [], "skipped": [], "refused": None, "watch": []}}
    base.update(over)
    return base


def test_failed_trailing_sell_is_not_reported_as_a_quiet_monitoring_day():
    """**止盈卖失败了，副标题不能还写"监控日"。** 和 2026-08-25 报喜不报忧同一个毛病。"""
    order = {"code": "600000.SH", "side": "SELL", "quantity": 600, "price": 11.24}
    result = _monitoring_result(
        orders=[order], allowed_orders=[order], submitted=True,
        trailing={"enabled": True, "arm_pct": 0.15, "trail_pct": 0.05, "hits": [{}],
                  "orders": [order], "skipped": [], "refused": None, "watch": []},
        broker={"ok": False, "submitted": 0, "outcomes": [{"ok": False}]})
    assert daily_report._status(result) != "监控日"
    assert "下单失败" in daily_report._status(result)
    assert "移动止盈" in daily_report._headline(result)


def test_quiet_monitoring_day_says_so():
    result = _monitoring_result()
    assert daily_report._status(result) == "监控日"
    assert "无持仓触发" in daily_report._headline(result)


def test_report_shows_rebalance_schedule():
    text = daily_report.render(_monitoring_result())
    assert "## 调仓周期" in text
    assert "2026-09-25" in text and "还有 8 个交易日" in text
    assert "监控日 · 下次 2026-09-25" in text, "概览表里要一眼看到"


def test_report_shows_distance_to_trigger():
    """没触发的日子，日报要能看出"离得很远"还是"再跌一点就卖"。"""
    watch = trailing.watchlist([_pos(cost=10, last=11.8), _pos(code="B", cost=10, last=10.5)],
                               {"600000.SH": 12.0, "B": 10.5}, 0.15, 0.05)
    text = daily_report.render(_monitoring_result(
        trailing={"enabled": True, "arm_pct": 0.15, "trail_pct": 0.05, "hits": [],
                  "orders": [], "skipped": [], "refused": None, "watch": watch}))
    assert "已上膛，再跌" in text
    assert "未上膛，再涨" in text


def test_disabled_trailing_is_one_line():
    text = daily_report.render(_monitoring_result(trailing={"enabled": False}))
    assert "未启用" in text


def test_monitoring_day_does_not_show_a_fake_risk_off(monkeypatch):
    """监控日不判择时，`market_risk_on` 停在初始值 False。直接渲染会让十天里九天
    显示一个假的 risk-off。"""
    from qbg.config import settings

    monkeypatch.setattr(settings, "qbg_market_sma", 100)
    text = daily_report.render(_monitoring_result(market_risk_on=False))
    assert "| 市场状态 | risk-off |" not in text
    assert "监控日不判" in text
    monkeypatch.setattr(settings, "qbg_market_sma", 0)
    assert "择时已关闭" in daily_report.render(_monitoring_result())


def test_closest_to_trigger_is_listed_first():
    watch = trailing.watchlist(
        # NEAR：峰值 13、触发价 11.7、现价 12.0 -> 再跌 2.5% 就卖
        # FAR ：峰值 14、触发价 12.6、现价 13.9 -> 还要再跌 9.4%
        [_pos(code="FAR", cost=10, last=13.9), _pos(code="NEAR", cost=10, last=12.0)],
        {"FAR": 14.0, "NEAR": 13.0}, 0.10, 0.10)
    text = daily_report.render(_monitoring_result(
        trailing={"enabled": True, "arm_pct": 0.10, "trail_pct": 0.10, "hits": [],
                  "orders": [], "skipped": [], "refused": None, "watch": watch}))
    assert text.index("|NEAR|") < text.index("|FAR|"), "离触发最近的应当排最前"
