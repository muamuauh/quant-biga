"""盘前风控闸链。

硬闸失败时不输出任何订单；订单闸失败只移除 BUY。SELL 始终保留在顾问清单中，
因为它降低风险。无法成交的 SELL（T+1、停牌、跌停）会得到失败闸记录，供清单
醒目标注和人工处理，而不会被系统悄悄吞掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import yaml

from qbg.config import settings
from qbg.execution.base import Order
from qbg.market import calendar, rules


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    reason: str
    affected: tuple[str, ...] = ()


def load_limits(path: Path | None = None) -> dict:
    limits = yaml.safe_load((path or settings.risk_limits_yaml).read_text(encoding="utf-8")) or {}
    # Overlay 只能覆盖白名单 risk 参数，实盘第三把锁永远只读原始 YAML。
    try:
        from qbg.tuning.overlay import read
        from qbg.tuning.whitelist import load

        specs, frozen, _ = load()
        for name, value in read().get("risk_limits", {}).items():
            spec = specs.get(name)
            if spec and spec.scope == "risk_limits" and name not in frozen:
                limits[name] = spec.validate(value)
    except Exception:  # noqa: BLE001 — overlay 损坏时安全降级到基线 YAML
        pass
    return limits


def mode_guard(limits: dict, mode: str | None = None, confirm_real: int | None = None) -> GateResult:
    current = (mode or settings.qbg_mode).upper()
    confirm = settings.i_confirm_real if confirm_real is None else confirm_real
    if current in {"ADVISORY", "PAPER"}:
        return GateResult("mode_guard", True, f"mode={current}")
    if current != "LIVE":
        return GateResult("mode_guard", False, f"未知 mode={current}")
    if confirm != 1 or not limits.get("allow_live_mode", False):
        return GateResult("mode_guard", False, "LIVE 三把锁未全部确认")
    return GateResult("mode_guard", True, "LIVE 已人工三重确认")


def session_guard(limits: dict, session_open: bool | None = None) -> GateResult:
    if not limits.get("require_trading_session", False):
        return GateResult("session_guard", True, "顾问模式关闭时段检查")
    if session_open is True:
        return GateResult("session_guard", True, "交易时段内")
    return GateResult("session_guard", False, "不在交易时段或时段未知")


def _stale_days(latest: pd.Timestamp, asof: pd.Timestamp) -> tuple[int, str]:
    """行情落后了几个**交易日**，以及用的是哪种口径。

    必须按交易日数，不能按工作日（`pd.bdate_range` 只排除周末）。差别在长假：
    春节后第一个交易日，上一根 K 线是 7 个工作日之前，但它就是**最近一个交易
    日**的收盘，数据一点都不旧。按工作日算会得出 stale=7，直接把硬闸打翻。

    2026 年实算，按工作日口径会误伤 6 天（春节/清明/劳动/端午/中秋/国庆之后
    的第一个交易日）。而 `max_stale_days` 默认是 1，在 07:30 盘前跑的排程下
    余量本来就是 0（最新数据必然是上一交易日），所以任何长于周末的假期都会翻车。

    日历缺失时退回工作日口径：它只会**高估**落后天数，对硬闸而言宁可多拦不可
    少拦。口径会写进闸的 reason，便于事后判断走的是哪条路。
    """
    if latest >= asof:
        return 0, "交易日"
    start, end = latest.date().isoformat(), asof.date().isoformat()
    days = calendar.load_cached()
    if days and days[0] <= start and end <= days[-1]:
        return max(0, calendar.trading_days_between(start, end)), "交易日"
    return max(0, len(pd.bdate_range(latest, asof)) - 1), "工作日（交易日历未覆盖）"


def data_freshness_guard(latest_date: date | str | None, asof: date | str,
                         limits: dict) -> GateResult:
    if latest_date is None:
        return GateResult("data_freshness_guard", False, "没有行情日期")
    latest, today = pd.Timestamp(latest_date).normalize(), pd.Timestamp(asof).normalize()
    stale, unit = _stale_days(latest, today)
    allowed = int(limits.get("max_stale_days", 1))
    return GateResult("data_freshness_guard", stale <= allowed,
                      f"行情落后 {stale} 个{unit}，允许 {allowed}")


def prediction_freshness_guard(pred_date: date | str | None, asof: date | str,
                              limits: dict) -> GateResult:
    """模型预测有多旧。**硬闸** —— 陈旧的预测比没有预测危险得多。

    ## 为什么这道闸必须存在

    2026-09-09 实测：日流程连着一个月每天选出**完全相同的三只票**。
    根因是 `latest_date_scores` 取的是「预测里最后一天」，而生产模型
    （静态 experiment `cn_lgb`）的 test 段止于 2026-08-10 —— 于是那一天的
    分数被反复使用了 30 天。

    **行情闸看不见这件事**：`data_freshness_guard` 守的是 K 线，而 K 线每天
    都在更新（那时 `last_date` 是昨天，新鲜得很）。陈旧的是**预测**，
    而当时没有任何一道闸在看它的日期。系统本该为此尖叫，却一声不吭地
    按一个月前的信号交易了一个月。

    ## 为什么是硬闸而不是订单闸

    订单闸只砍 BUY、放行 SELL —— 那个语义假设"信号是对的，只是某些订单不该
    执行"。预测过期时这个前提就不成立了：**买卖两个方向都建立在同一份过期
    信号上**，凭什么相信它的卖出judgment？此时唯一安全的动作是全盘停手，
    让人来看一眼。

    复用 `_stale_days`，因此长假不会误伤（见那个函数的说明）。
    """
    if pred_date is None:
        # 拿不到预测日期**不放行**。这里最容易犯的错是"未知就当没问题" ——
        # 而这道闸存在的全部理由就是"没人在看这个数"。
        return GateResult("prediction_freshness_guard", False, "没有模型预测日期")
    latest = pd.Timestamp(pred_date).normalize()
    today = pd.Timestamp(asof).normalize()
    stale, unit = _stale_days(latest, today)
    allowed = int(limits.get("max_prediction_stale_days", 3))
    return GateResult(
        "prediction_freshness_guard", stale <= allowed,
        f"模型预测落后 {stale} 个{unit}（{latest.date()}），允许 {allowed}")


def price_limit_guard(orders: list[Order], prev_close: dict[str, float],
                      is_st: dict[str, bool], market_price: dict[str, float]) -> GateResult:
    hit = []
    for order in orders:
        previous, current = prev_close.get(order.code), market_price.get(order.code)
        if not previous or not current:
            continue
        blocked = (order.side == "BUY" and rules.at_limit_up(current, order.code, previous,
                                                              is_st.get(order.code, False)))
        blocked |= (order.side == "SELL" and rules.at_limit_down(current, order.code, previous,
                                                                  is_st.get(order.code, False)))
        if blocked:
            hit.append(order.code)
    return GateResult("price_limit_guard", not hit,
                      "未触及阻塞方向涨跌停" if not hit else f"涨跌停阻塞: {sorted(set(hit))}",
                      tuple(sorted(set(hit))))


def suspension_guard(orders: list[Order], suspended: dict[str, bool]) -> GateResult:
    hit = sorted({o.code for o in orders if suspended.get(o.code, False)})
    return GateResult("suspension_guard", not hit,
                      "无停牌订单" if not hit else f"停牌: {hit}", tuple(hit))


def st_guard(orders: list[Order], is_st: dict[str, bool]) -> GateResult:
    hit = sorted({o.code for o in orders if o.side == "BUY" and is_st.get(o.code, False)})
    return GateResult("st_guard", not hit, "无 ST 买单" if not hit else f"禁止买入 ST: {hit}",
                      tuple(hit))


def t1_guard(orders: list[Order], sellable_qty: dict[str, int]) -> GateResult:
    hit = sorted({o.code for o in orders
                  if o.side == "SELL" and o.quantity > sellable_qty.get(o.code, 0)})
    return GateResult("t1_guard", not hit, "卖出量均不超过可卖量" if not hit else f"T+1 冻结: {hit}",
                      tuple(hit))


def lot_guard(orders: list[Order], current_qty: dict[str, int]) -> GateResult:
    hit = []
    for order in orders:
        if order.side == "BUY" and not rules.is_valid_buy_qty(order.quantity):
            hit.append(order.code)
        if order.side == "SELL":
            holding = current_qty.get(order.code, 0)
            if order.quantity <= 0 or order.quantity > holding:
                hit.append(order.code)
    hit = sorted(set(hit))
    return GateResult("lot_guard", not hit, "数量合法" if not hit else f"数量非法: {hit}", tuple(hit))


def max_position_guard(target_weights: dict[str, float], limits: dict) -> GateResult:
    cap = float(limits.get("max_position_pct", 1.0))
    hit = sorted(c for c, weight in target_weights.items() if weight > cap + 1e-9)
    return GateResult("max_position_guard", not hit,
                      f"单票权重不超过 {cap:.1%}" if not hit else f"超单票上限: {hit}", tuple(hit))


def min_cash_guard(orders: list[Order], current_cash: float, total_equity: float,
                   limits: dict) -> GateResult:
    post_cash = current_cash
    for order in orders:
        cashflow = order.notional + order.estimated_fee
        post_cash += (order.notional - order.estimated_fee) if order.side == "SELL" else -cashflow
    floor = total_equity * float(limits.get("min_cash_buffer_pct", 0.0))
    return GateResult("min_cash_guard", post_cash >= floor,
                      f"交易后现金 {post_cash:.2f}，下限 {floor:.2f}")


def daily_loss_kill_switch(orders: list[Order], today_pnl: float, total_equity: float,
                           limits: dict) -> GateResult:
    loss = -today_pnl / total_equity if total_equity > 0 else 0.0
    cap = float(limits.get("max_daily_loss_pct", 1.0))
    failed = loss > cap and any(o.side == "BUY" for o in orders)
    return GateResult("daily_loss_kill_switch", not failed,
                      f"当日亏损 {loss:.2%}，上限 {cap:.2%}")


def run_all_gates(*, target_weights: dict[str, float], orders: list[Order],
                  current_cash: float, total_equity: float, today_pnl: float,
                  latest_data_date: date | str | None, asof: date | str,
                  prediction_date: date | str | None = None,
                  prev_close: dict[str, float], market_price: dict[str, float],
                  is_st: dict[str, bool], suspended: dict[str, bool],
                  sellable_qty: dict[str, int], current_qty: dict[str, int],
                  limits: dict | None = None, session_open: bool | None = None,
                  mode: str | None = None, confirm_real: int | None = None,
                  ) -> tuple[bool, list[Order], list[GateResult]]:
    limits = limits or load_limits()
    hard = [mode_guard(limits, mode, confirm_real), session_guard(limits, session_open),
            data_freshness_guard(latest_data_date, asof, limits),
            prediction_freshness_guard(prediction_date, asof, limits)]
    if not all(result.passed for result in hard):
        return False, [], hard

    results = hard
    results.extend([
        price_limit_guard(orders, prev_close, is_st, market_price),
        suspension_guard(orders, suspended),
        st_guard(orders, is_st),
        t1_guard(orders, sellable_qty),
        lot_guard(orders, current_qty),
        max_position_guard(target_weights, limits),
        daily_loss_kill_switch(orders, today_pnl, total_equity, limits),
    ])
    sells = [order for order in orders if order.side == "SELL"]
    buys = [order for order in orders if order.side == "BUY"]
    # 切片起点 = 硬闸个数。加硬闸时**必须**同步改这里 —— 少改一位，
    # 新加的硬闸会被当成订单闸参与"只砍 BUY"，而它已经在上面 return 过了，
    # 走到这里说明它是通过的，于是这个错误完全不可见。
    for result in results[len(hard):]:
        if not result.passed and result.name != "t1_guard":
            affected = set(result.affected)
            buys = [] if not affected else [o for o in buys if o.code not in affected]

    cash_result = min_cash_guard(sells + buys, current_cash, total_equity, limits)
    if not cash_result.passed:
        for order in sorted(buys, key=lambda item: item.notional, reverse=True):
            buys.remove(order)
            cash_result = min_cash_guard(sells + buys, current_cash, total_equity, limits)
            if cash_result.passed:
                break
    results.append(cash_result)
    return True, sells + buys, results
