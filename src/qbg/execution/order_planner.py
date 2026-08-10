"""目标权重与当前持仓做差，生成符合 A股整手和涨跌停规则的限价单。"""

from __future__ import annotations

from qbg.execution import fees
from qbg.execution.base import Order
from qbg.market import codes, rules


def plan_orders(target_weights: dict[str, float], current_qty: dict[str, int],
                last_close: dict[str, float], prev_close: dict[str, float],
                total_equity: float, *, names: dict[str, str] | None = None,
                is_st: dict[str, bool] | None = None, slippage: float = 0.002,
                drift_band: float = 0.03,
                fee_profile: fees.FeeProfile | None = None) -> list[Order]:
    """生成 SELL/BUY 单；卖单排前，便于先释放现金再买。"""
    names, is_st = names or {}, is_st or {}
    normalized_targets = {codes.normalize(c): float(w) for c, w in target_weights.items()}
    normalized_qty = {codes.normalize(c): int(q) for c, q in current_qty.items()}
    universe = set(normalized_targets) | set(normalized_qty)
    orders: list[Order] = []
    for code in sorted(universe):
        target_w = max(0.0, normalized_targets.get(code, 0.0))
        held = max(0, normalized_qty.get(code, 0))
        px = float(last_close.get(code, 0.0) or 0.0)
        previous = float(prev_close.get(code, 0.0) or 0.0)
        if px <= 0 or previous <= 0:
            continue
        current_w = held * px / total_equity if total_equity > 0 else 0.0
        if target_w > 0 and held > 0 and abs(target_w - current_w) < drift_band:
            continue

        target_qty = rules.affordable_shares(target_w * total_equity, px) if target_w else 0
        diff = target_qty - held
        if diff > 0:
            quantity = rules.round_lot_down(diff)
            side = "BUY"
        elif diff < 0:
            quantity = rules.normalize_sell_qty(-diff, held)
            side = "SELL"
        else:
            continue
        if quantity <= 0:
            continue

        raw_limit = px * (1 + slippage if side == "BUY" else 1 - slippage)
        limit = rules.clamp_to_limits(raw_limit, code, previous, is_st.get(code, False))
        reason = f"目标权重 {target_w:.1%}，持仓 {held}→{target_qty} 股"
        order = Order(code=code, name=names.get(code, ""), side=side, quantity=quantity,
                      price=limit, ref_price=px, reason=reason)
        order.estimated_fee = fees.estimate(order.notional, side, fee_profile).total
        orders.append(order)
    return sorted(orders, key=lambda order: (order.side != "SELL", order.code))

