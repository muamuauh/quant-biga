"""顾问模式下单清单的 CSV/Markdown 渲染。"""

from __future__ import annotations

import csv
from pathlib import Path

from qbg.execution.base import Order
from qbg.risk.gates import GateResult

HEADERS = ["代码", "名称", "方向", "股数", "限价", "预估金额", "预估费用", "理由"]


def rows(orders: list[Order]) -> list[list[str]]:
    return [[o.code, o.name, o.side, str(o.quantity), f"{o.price:.2f}", f"{o.notional:.2f}",
             f"{o.estimated_fee:.2f}", o.reason] for o in orders]


def render_markdown(orders: list[Order], gates: list[GateResult] | None = None) -> str:
    lines = ["# A股下单清单", "", "| " + " | ".join(HEADERS) + " |",
             "|" + "|".join(["---"] * len(HEADERS)) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows(orders)]
    if not orders:
        lines.append("| - | - | - | - | - | - | - | 无可执行订单 |")
    if gates:
        lines += ["", "## 风控闸", ""]
        lines += [f"- {'通过' if gate.passed else '拦截/警告'} `{gate.name}`：{gate.reason}"
                  for gate in gates]
    lines += ["", "> 顾问模式：请在南京证券 APP 人工核对代码、方向、数量和限价后执行。", ""]
    return "\n".join(lines)


def write_csv(path: Path, orders: list[Order]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADERS)
        writer.writerows(rows(orders))
    return path

