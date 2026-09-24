"""止损与移动止盈的实盘一侧：峰值库、触发判据、卖单、日报。全离线。

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
    src = inspect.getsource(daily_cycle._run_forced_exits)
    assert "save_rebalance_date" not in src
    assert "save_marker" in src, "应当写当日完成标记，防同一天重跑重复卖"


def test_trailing_goes_through_the_gates():
    """硬闸语义不许为某一类订单开口子（CLAUDE.md §三）。"""
    src = inspect.getsource(daily_cycle._run_forced_exits)
    assert "run_all_gates" in src
    assert src.index("run_all_gates") < src.index("_submit_to_broker")


def test_degraded_portfolio_does_not_touch_peaks():
    """降级时不刷新峰值：落到默认账户时持仓是空的，刷新会把整个峰值库清空。"""
    src = inspect.getsource(daily_cycle.run_daily)
    assert 'exits_trusted = degraded is None and portfolio_source != "default"' in src
    guard = src[src.index("trailing.update_peaks") - 200: src.index("trailing.update_peaks")]
    assert "exits_trusted" in guard


# ----------------------------------------------------------------------
# 日报
# ----------------------------------------------------------------------

def _exits(**over):
    base = {"stop_enabled": False, "stop_pct": 0.0, "trailing_enabled": False,
            "arm_pct": 0.0, "trail_pct": 0.0, "trusted": True, "watch": [], "hits": [],
            "orders": [], "skipped": [], "refused": None, "excluded_on_rebalance": []}
    base.update(over)
    return base


def _monitoring_result(**over):
    base = {"date": "2026-09-15", "mode": "PAPER", "skipped_reason": "not_rebalance_day",
            "run_kind": "monitoring", "hard_ok": True, "orders": [], "allowed_orders": [],
            "rebalance": {"every_days": 10, "last": "2026-09-11", "days_since": 2,
                          "is_today": False, "next": "2026-09-25", "next_after_today": None,
                          "cash_fraction": 0.21, "cash_trigger": 0.5, "cash_triggered": False},
            "exits": _exits(trailing_enabled=True, arm_pct=0.15, trail_pct=0.05)}
    base.update(over)
    return base


def test_failed_trailing_sell_is_not_reported_as_a_quiet_monitoring_day():
    """**止盈卖失败了，副标题不能还写"监控日"。** 和 2026-08-25 报喜不报忧同一个毛病。"""
    order = {"code": "600000.SH", "side": "SELL", "quantity": 600, "price": 11.24}
    result = _monitoring_result(
        orders=[order], allowed_orders=[order], submitted=True,
        exits=_exits(trailing_enabled=True, arm_pct=0.15, trail_pct=0.05,
                     hits=[{"kind": "trailing"}], orders=[order]),
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
        exits=_exits(trailing_enabled=True, arm_pct=0.15, trail_pct=0.05, watch=watch)))
    assert "已上膛，再跌" in text
    assert "未上膛，再涨" in text


def test_disabled_trailing_is_one_line():
    text = daily_report.render(_monitoring_result(exits=_exits()))
    assert text.count("未启用") == 2, "止损和移动止盈各一行"


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
        exits=_exits(trailing_enabled=True, arm_pct=0.10, trail_pct=0.10, watch=watch)))
    assert text.index("|NEAR|") < text.index("|FAR|"), "离触发最近的应当排最前"


# ----------------------------------------------------------------------
# 止损
# ----------------------------------------------------------------------

def test_stop_loss_hits_at_the_line():
    hits = trailing.stop_loss_hits([_pos(cost=10, last=9.2)], 0.08)
    assert [(h.code, h.kind) for h in hits] == [("600000.SH", "stop_loss")]
    assert "止损" in hits[0].reason()


def test_stop_loss_uses_cost_not_peak():
    """止损问"这笔买卖亏了多少"。先涨 30% 再跌回成本的票，止损不管 —— 那是止盈的事。"""
    assert trailing.stop_loss_hits([_pos(cost=10, last=10.0)], 0.08) == []


def test_stop_loss_off_and_small_loss():
    assert trailing.stop_loss_hits([_pos(cost=10, last=5.0)], 0.0) == []
    assert trailing.stop_loss_hits([_pos(cost=10, last=9.5)], 0.08) == []


def test_forced_exits_never_sells_the_same_code_twice():
    """同时满足止损和移动止盈（冲高后暴跌）的票只卖一次，归到止损。"""
    positions = [_pos(cost=10, last=9.0)]
    hits = trailing.forced_exits(positions, {"600000.SH": 14.0}, 0.08, 0.15, 0.05)
    assert [h.kind for h in hits] == ["stop_loss"]


def test_watchlist_shows_distance_to_stop():
    rows = trailing.watchlist([_pos(cost=10, last=9.5)], {}, 0.0, 0.0, 0.08)
    assert rows[0]["stop_price"] == 9.2
    assert rows[0]["to_stop"] < 0 and not rows[0]["stopped"]


def test_rebalance_day_excludes_stopped_codes_from_selection():
    """调仓日也止损：从分数里剔除，**并且**从迟滞的"已持有"集合里剔除 ——
    只剔分数不剔集合的话，迟滞会因为"还在前 keep_rank 名"把它留下。"""
    src = inspect.getsource(daily_cycle.run_daily)
    assert "reviewed_scores.drop(" in src
    assert "set(current) - stopped_today" in src
    assert src.index("stopped_today") < src.index("topk_equal_weight(")


def test_stop_loss_is_read_from_risk_limits():
    """止损参数在 risk_limits.yaml，此前**零引用** —— 钉住它真的被读了。"""
    src = inspect.getsource(daily_cycle.run_daily)
    assert 'limits.get("stop_loss_pct"' in src


def test_report_shows_stop_exclusion_on_rebalance_day():
    result = _monitoring_result(
        skipped_reason=None, run_kind="rebalance",
        rebalance={"every_days": 10, "last": "2026-09-11", "days_since": 10, "is_today": True,
                   "next": "2026-09-25", "next_after_today": "2026-10-09",
                   "cash_fraction": 0.05, "cash_trigger": 0.5, "cash_triggered": False},
        exits=_exits(stop_enabled=True, stop_pct=0.08, excluded_on_rebalance=["600482.SH"],
                     hits=[{"kind": "stop_loss", "code": "600482.SH"}]))
    text = daily_report.render(result)
    assert "600482.SH" in text and "已从选股里剔除" in text


def test_stop_loss_headline_says_stop_loss():
    order = {"code": "600000.SH", "side": "SELL", "quantity": 1000, "price": 9.15}
    result = _monitoring_result(orders=[order], allowed_orders=[order], submitted=True,
                                exits=_exits(stop_enabled=True, stop_pct=0.08,
                                             hits=[{"kind": "stop_loss"}], orders=[order]))
    assert "止损触发" in daily_report._headline(result)


# ----------------------------------------------------------------------
# 按昨收判（2026-09-24）
# ----------------------------------------------------------------------
# 回测的止损是收盘 t 判、开盘 t+1 卖。8% 止损保留的理由（回撤少 5.1 点）是在这条
# 规则上量出来的；实盘此前拿 09:32 的盘中价判，跑的是另一条没被验证的规则。

GOLD = "600547.SH"


def _gold(live):
    """山东黄金，2026-09-18 以 33.111 建仓。8% 止损线 = 30.462。"""
    return _pos(code=GOLD, name="山东黄金", qty=1800, sellable=1800, cost=33.111, last=live)


def test_shandong_gold_2026_09_24_is_not_stopped_on_close():
    """**真实那一天。** 09-23 收盘 30.93（−6.6%，没到线），09-24 09:32 跳空到
    30.23（−8.7%）。盘中规则当场卖了；按回测的规则那天不该卖。"""
    live = [_gold(30.23)]
    assert trailing.stop_loss_hits(live, 0.08), "前提：按盘中价确实会触发"

    judged = trailing.judged_on_close(live, {GOLD: 30.93})
    assert trailing.stop_loss_hits(judged, 0.08) == []


def test_a_close_below_the_line_still_stops_even_if_it_opens_higher():
    """反方向也要成立 —— 这条改动不是"少卖"，是"按收盘判"。
    昨收跌破线、今天开盘拉回来了，收盘规则照样卖。"""
    judged = trailing.judged_on_close([_gold(30.60)], {GOLD: 30.40})
    assert [h.kind for h in trailing.stop_loss_hits(judged, 0.08)] == ["stop_loss"]


def test_judged_positions_keep_the_live_price_for_the_record():
    judged = trailing.judged_on_close([_gold(30.23)], {GOLD: 30.93})[0]
    assert judged["last_price"] == 30.93 and judged["live_price"] == 30.23
    assert judged["judged_on"] == "close"


def test_missing_close_falls_back_to_live_and_says_so():
    """缺日线时退回现价 —— 止损是保护性的，缺数据时宁可按现价判也不能整只不判。
    但得标出来，否则就是两条规则混着跑而没人知道。"""
    judged = trailing.judged_on_close([_gold(30.23)], {})[0]
    assert judged["last_price"] == 30.23 and judged["judged_on"] == "live"
    assert trailing.stop_loss_hits([judged], 0.08), "缺数据也得能止损"


def test_the_order_price_still_uses_the_live_reference():
    """**只换"判不判"用的价，不换"挂多少"用的价。** 拿昨收去挂限价就是
    37.7% 的卖单挂到市价错误一侧的那个老坑。"""
    judged = trailing.judged_on_close([_gold(30.23)], {GOLD: 30.40})
    hits = trailing.stop_loss_hits(judged, 0.08)
    orders, _ = trailing.sell_orders(hits, {GOLD: 30.23}, {GOLD: 30.40}, {GOLD: False}, 0.005)
    assert orders[0].ref_price == 30.23, "限价参考应当是实时价，不是判止损用的昨收"


def test_daily_cycle_judges_on_close_everywhere():
    """观察表、调仓日止损、监控日强卖 —— 三处都要用换过价的持仓。
    漏一处就是两条规则混着跑。"""
    src = inspect.getsource(daily_cycle.run_daily)
    assert "judged_on_close(" in src
    first_use = min(src.index(k) for k in ("watchlist(", "stop_loss_hits(", "_run_forced_exits("))
    assert src.index("judged_on_close(") < first_use, "换价必须在第一次判之前"
    for call in ("trailing.watchlist(positions", "trailing.stop_loss_hits(positions",
                 "trailing.update_peaks(positions", "_run_forced_exits(result, positions"):
        assert call not in src, f"{call} —— 这里还在用 09:32 的盘中价判"


def test_report_says_it_judges_on_close():
    """和「账户与持仓」表里 09:32 的现价不是同一个数 —— 列名得说清楚。"""
    result = _monitoring_result(exits=_exits(
        stop_enabled=True, stop_pct=0.08, judged_on="close", close_date="2026-09-23",
        watch=trailing.watchlist(trailing.judged_on_close([_gold(30.23)], {GOLD: 30.93}),
                                 {}, 0.0, 0.0, 0.08)))
    text = daily_report.render(result)
    assert "按**昨收**判断" in text and "2026-09-23" in text
    assert "|代码|名称|成本|昨收|" in text
