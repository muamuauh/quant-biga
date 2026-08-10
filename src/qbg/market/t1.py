"""T+1：当日买入的股票当日不可卖出。

**这条规则必须同时出现在两个地方：**

  1. 风控闸 `t1_guard` —— 卖出数量 ≤ 可卖量（P4）
  2. **回测引擎** —— 当日建仓的名字不能在同一天卖出（P2）

只做第 1 处而漏掉第 2 处，是 A股回测里最常见也最难发现的一类偏差：
回测会以为可以当天买入、当天获利了结，于是把日内波动也算成收益。
收益曲线看起来只是"好一点"，不会有任何报错。

## 可卖量从哪来

券商 APP 直接显示"可用/可卖"数量，所以 `Position.sellable_qty` 通常是
读出来的，不用算。`sellable_from_trades` 是给回测和对账用的：从持仓量
减去当日买入量。
"""

from __future__ import annotations

from datetime import date

import pandas as pd


def sellable_from_trades(holding: int, bought_today: int) -> int:
    """可卖量 = 持仓 − 当日买入。

    结果夹在 `[0, holding]`：`bought_today` 若因数据问题超过 `holding`
    （比如当天买了又卖），负的可卖量没有意义，按 0 处理是安全的一侧。
    """
    return int(max(0, min(holding, holding - max(bought_today, 0))))


def is_sellable(buy_date: date | str | None, asof: date | str) -> bool:
    """`buy_date` 买入的仓位，在 `asof` 这天能不能卖。

    `buy_date` 为 None 时返回 True：不知道买入日期的仓位，只可能是历史
    持仓（新买的一定知道日期），按可卖处理。
    """
    if buy_date is None:
        return True
    return pd.Timestamp(buy_date).date() < pd.Timestamp(asof).date()


def apply_to_positions(positions, asof: date | str) -> dict[str, int]:
    """`{code: 可卖股数}`。

    优先用券商给的 `sellable_qty`（那是权威值），没有时才按买入日期推算。
    """
    out: dict[str, int] = {}
    for p in positions:
        if getattr(p, "sellable_qty", None) is not None:
            out[p.code] = int(max(0, min(p.qty, p.sellable_qty)))
        else:
            out[p.code] = int(p.qty) if is_sellable(getattr(p, "buy_date", None), asof) else 0
    return out


def frozen_shares(positions, asof: date | str) -> dict[str, int]:
    """`{code: 因 T+1 冻结的股数}`。只含真正被冻结的票，给日报用。"""
    sellable = apply_to_positions(positions, asof)
    return {
        p.code: int(p.qty) - sellable[p.code]
        for p in positions
        if int(p.qty) - sellable[p.code] > 0
    }
