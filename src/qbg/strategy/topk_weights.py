"""把模型分数转换成适合 10 万以内 A股账户的 top-K 权重。"""

from __future__ import annotations

from typing import TypeVar

import pandas as pd

from qbg.market import codes, rules

T = TypeVar("T")


def select_with_hysteresis(ranked: list[T], held: set[T], k: int, keep_rank: int) -> list[T]:
    """持仓仍在 keep_rank 内就保留，再用当日最高排名补足 k 个。"""
    if k <= 0:
        return []
    keep_rank = max(k, keep_rank)
    selected = [item for item in ranked[:keep_rank] if item in held][:k]
    for item in ranked:
        if len(selected) >= k:
            break
        if item not in selected:
            selected.append(item)
    return selected


def affordable_scores(scores: pd.Series, price_by_code: dict[str, float], equity: float,
                      k: int, total_weight: float = 0.95,
                      cap: float | None = None) -> pd.Series:
    """剔除单槽预算买不起一手（100 股）的票。

    若价格表完整但没有任何票可买，返回空 Series；不能退回原分数，否则后续会
    重新选中明知买不起的票。价格缺失同样视为不可负担，交给数据新鲜度闸报告。
    """
    if scores.empty or equity <= 0 or k <= 0:
        return scores.iloc[0:0]
    slot_weight = total_weight / k
    if cap is not None:
        slot_weight = min(slot_weight, cap)
    budget = equity * slot_weight
    keep = []
    for raw in scores.index:
        code = codes.normalize(str(raw))
        price = float(price_by_code.get(code, 0.0) or 0.0)
        if rules.affordable_shares(budget, price) >= rules.LOT_SIZE:
            keep.append(raw)
    return scores.loc[keep]


def topk_equal_weight(scores: pd.Series, k: int = 3, total_weight: float = 0.95,
                      held_codes: set[str] | None = None,
                      keep_rank: int | None = None) -> dict[str, float]:
    """分数（任意已知代码格式）→ 规范代码等权目标。"""
    if scores.empty or k <= 0:
        return {}
    ranked = [codes.normalize(str(item)) for item in scores.sort_values(ascending=False).index]
    chosen = (select_with_hysteresis(ranked, held_codes or set(), k, keep_rank)
              if held_codes and keep_rank else ranked[:k])
    if not chosen:
        return {}
    weight = total_weight / len(chosen)
    return {code: weight for code in chosen}


def renormalize_weights(weights: dict[str, float], target_total: float,
                        cap: float | None = None) -> dict[str, float]:
    """按比例放大幸存权重，并以单票上限截断；截断余量留作现金。"""
    current = sum(max(0.0, value) for value in weights.values())
    if not weights or current <= 0:
        return dict(weights)
    scale = target_total / current
    return {code: min(value * scale, cap) if cap is not None else value * scale
            for code, value in weights.items()}

