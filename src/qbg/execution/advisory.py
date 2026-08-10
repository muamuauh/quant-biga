"""零券商接触的顾问执行适配器：仅写 Markdown/CSV 清单。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from qbg.config import settings
from qbg.execution.base import ExecutionResult, Order
from qbg.report.order_sheet import render_markdown, write_csv
from qbg.risk.gates import GateResult


class AdvisoryAdapter:
    def __init__(self, root: Path | None = None):
        self.root = root or settings.report_dir / "orders"

    def submit(self, orders: list[Order], asof: date | str,
               gates: list[GateResult] | None = None) -> ExecutionResult:
        stamp = str(date.fromisoformat(asof) if isinstance(asof, str) else asof)
        md_path, csv_path = self.root / f"{stamp}.md", self.root / f"{stamp}.csv"
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render_markdown(orders, gates), encoding="utf-8")
        write_csv(csv_path, orders)
        return ExecutionResult(True, "ADVISORY", len(orders),
                               (str(md_path), str(csv_path)), "仅生成清单，未接触券商")

