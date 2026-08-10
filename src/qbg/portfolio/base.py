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

