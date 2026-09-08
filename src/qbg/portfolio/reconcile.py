"""昨日顾问清单与今日持仓差分，量化半自动执行质量。

除了「成交了几股」，这里还量**成交价对不对得上回测的假设** ——
见 `fill_slippage`。那是整套回测里唯一一个从没被实测过的输入。
"""

from __future__ import annotations

import pandas as pd

# 成交表的列名在不同券商/版本间会变，所以按语义找列而不是按字面名。
# 顺序即优先级：先找最精确的，再退到宽泛的。
_CODE_KEYS = ("证券代码", "股票代码", "代码")
_SIDE_KEYS = ("操作", "买卖标志", "委托方向")
_QTY_KEYS = ("成交数量", "成交量")
_PRICE_KEYS = ("成交均价", "成交价格", "成交价")


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


def _pick(row: dict, keys) -> object:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _num(value) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def fill_slippage(trades, reference_price: dict[str, float]) -> pd.DataFrame:
    """实际成交价 vs 回测假设的成交价，单位 bp。

    ## 为什么参照物是**当日开盘价**

    回测引擎的成交假设是「次日开盘价」（`plan.md` §5.7）：盘后出信号、
    次日开盘执行。所以要校准的就是这一条 —— 实际敲进去的价格，比开盘价
    贵了还是便宜了多少。

    这是整套回测里**唯一一个从没被实测过的输入**。费率有 fee_profile、
    滑点有敏感性曲线，而"开盘价成交"这个假设一直是白拿的。它不是一个可调
    参数，是一个乘在**所有**收益上的系统性偏移。

    ## 符号约定：正 = 吃亏

    买入成交价高于开盘价 → 多花钱 → 正。
    卖出成交价低于开盘价 → 少收钱 → 正。
    统一成"正数就是成本"之后，这一列可以直接和 `fee_profile` 的费率相加，
    也可以直接喂给回测的 `extra_slippage`。

    `reference_price` 是 `{code: 当日开盘价}`，**必须用不复权价** ——
    成交回报里的价格是原始价，拿复权价去比会算出一个和真实盘口无关的数。
    """
    rows = []
    for raw in (trades or []):
        row = dict(raw)
        code = _pick(row, _CODE_KEYS)
        price = _num(_pick(row, _PRICE_KEYS))
        qty = _num(_pick(row, _QTY_KEYS))
        side_raw = str(_pick(row, _SIDE_KEYS) or "")
        if code is None or price <= 0 or qty <= 0:
            continue
        code = str(code).strip()
        ref = reference_price.get(code)
        if not ref or ref <= 0:
            # 参照价拿不到就**跳过**而不是记 0：记 0 会把"不知道"混进平均值里，
            # 把真实滑点往下拉，而这正是我们要量的那个数。
            continue
        side = "BUY" if "买" in side_raw else ("SELL" if "卖" in side_raw else "")
        if not side:
            continue
        sign = 1.0 if side == "BUY" else -1.0
        rows.append({"code": code, "side": side, "qty": int(qty),
                     "fill_price": price, "ref_price": float(ref),
                     "slippage_bp": sign * (price - ref) / ref * 1e4,
                     "notional": price * qty})
    return pd.DataFrame(rows)


def slippage_summary(frame: pd.DataFrame) -> dict:
    """把逐笔滑点汇总成能直接填进回测假设的一个数。

    **按成交金额加权**，不是按笔数：回测里的 `extra_slippage` 乘的是换手金额，
    等权平均会让一笔 3000 元的小单和一笔 4 万元的大单说话一样响。
    """
    if frame is None or frame.empty:
        return {"n": 0, "weighted_bp": None, "median_bp": None,
                "buy_bp": None, "sell_bp": None, "notional": 0.0}
    w = frame["notional"]
    out = {"n": int(len(frame)),
           "weighted_bp": float((frame["slippage_bp"] * w).sum() / w.sum()),
           "median_bp": float(frame["slippage_bp"].median()),
           "notional": float(w.sum())}
    for side in ("BUY", "SELL"):
        part = frame[frame["side"] == side]
        key = "buy_bp" if side == "BUY" else "sell_bp"
        out[key] = (float((part["slippage_bp"] * part["notional"]).sum()
                          / part["notional"].sum()) if len(part) else None)
    return out
