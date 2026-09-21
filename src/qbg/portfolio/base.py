"""持仓来源公共模型与协议。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Protocol


@dataclass(frozen=True)
class Position:
    code: str
    name: str
    qty: int
    sellable_qty: int
    cost_price: float
    last_price: float
    market_value: float
    pnl: float = 0.0
    # 当日盈亏。**默认 None 而不是 0.0** —— OCR/CSV 那两个来源根本没有这一列，
    # 写 0 等于断言"今天平盘"，而真相是"不知道"。和 `market_risk_on` 同一条规矩：
    # 缺失要看得出来是缺失。
    day_pnl: float | None = None

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class PortfolioSnapshot:
    asof: str
    total_equity: float
    available_cash: float
    positions: tuple[Position, ...]


class PortfolioSource(Protocol):
    def load(self) -> PortfolioSnapshot: ...

