"""执行层公共协议；上层不依赖具体券商或顾问清单实现。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Protocol


@dataclass
class Order:
    code: str
    side: str
    quantity: int
    price: float
    reason: str
    name: str = ""
    ref_price: float = 0.0
    estimated_fee: float = 0.0

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    def as_dict(self) -> dict:
        return asdict(self) | {"notional": round(self.notional, 2)}


@dataclass(frozen=True)
class ExecutionResult:
    ok: bool
    mode: str
    submitted: int
    artifacts: tuple[str, ...] = ()
    message: str = ""


class ExecutionAdapter(Protocol):
    def submit(self, orders: list[Order], asof: date | str) -> ExecutionResult: ...

