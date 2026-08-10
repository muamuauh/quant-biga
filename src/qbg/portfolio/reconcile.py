"""昨日顾问清单与今日持仓差分，量化半自动执行质量。"""

from __future__ import annotations

import pandas as pd


def reconcile_orders(orders: pd.DataFrame, before: pd.DataFrame,
                     after: pd.DataFrame) -> pd.DataFrame:
    before_qty = before.set_index("code")["qty"].astype(int).to_dict() if len(before) else {}
    after_qty = after.set_index("code")["qty"].astype(int).to_dict() if len(after) else {}
    rows = []
    for row in orders.itertuples():
        code, side = str(row.code), str(row.side).upper()
        expected = int(row.quantity) * (1 if side == "BUY" else -1)
        actual = after_qty.get(code, 0) - before_qty.get(code, 0)
        same_direction = max(0, actual) if expected > 0 else max(0, -actual)
        filled = min(abs(expected), same_direction)
        status = "已成交" if filled == abs(expected) else ("部分成交" if filled else "未成交")
        rows.append({"code": code, "side": side, "planned_qty": abs(expected),
                     "filled_qty": filled, "status": status,
                     "quantity_gap": abs(expected) - filled})
    return pd.DataFrame(rows)

