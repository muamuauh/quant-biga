"""手工 CSV 持仓源，也是 OCR 失败时的零依赖降级路径。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from qbg.config import settings
from qbg.portfolio.base import PortfolioSnapshot, Position


class ManualSource:
    def __init__(self, path: Path | None = None):
        self.path = path or settings.positions_csv

    def load(self) -> PortfolioSnapshot:
        frame = pd.read_csv(self.path)
        positions = tuple(Position(
            code=str(row.code), name=str(getattr(row, "name", "")), qty=int(row.qty),
            sellable_qty=int(getattr(row, "sellable_qty", row.qty)),
            cost_price=float(getattr(row, "cost_price", 0)),
            last_price=float(getattr(row, "last_price", 0)),
            market_value=float(getattr(row, "market_value", 0)),
            pnl=float(getattr(row, "pnl", 0)),
        ) for row in frame.itertuples())
        first = frame.iloc[0] if len(frame) else {}
        return PortfolioSnapshot(str(first.get("asof", "")), float(first.get("total_equity", 0)),
                                 float(first.get("available_cash", 0)), positions)

