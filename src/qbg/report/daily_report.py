"""每日运行结果的中文 Markdown 日报。"""

from __future__ import annotations

from pathlib import Path

from qbg.config import settings


def render(result: dict) -> str:
    status = result.get("skipped_reason") or ("完成" if result.get("hard_ok", True) else "硬闸中止")
    lines = [f"# quant-biga 日报 · {result.get('date', '')}", "", f"- 状态：{status}",
             f"- 模式：{result.get('mode', settings.qbg_mode)}",
             f"- 市场状态：{'risk-on' if result.get('market_risk_on') else 'risk-off'}",
             f"- 目标：{len(result.get('targets', {}))} 只",
             f"- 计划订单：{len(result.get('orders', []))} 笔",
             f"- 允许订单：{len(result.get('allowed_orders', []))} 笔", ""]
    if result.get("orders"):
        lines += ["## 订单摘要", "", "|代码|方向|股数|限价|原因|", "|---|---:|---:|---:|---|"]
        for order in result["orders"]:
            lines.append(f"|{order['code']}|{order['side']}|{order['quantity']}|"
                         f"{order['price']:.2f}|{order['reason']}|")
        lines.append("")
    if result.get("gates"):
        lines += ["## 风控", ""]
        lines += [f"- {'✅' if gate['passed'] else '⚠️'} {gate['name']}：{gate['reason']}"
                  for gate in result["gates"]]
    lines += ["", "## 重要限制", "",
              "- 当前历史股票池有生存者偏差，回测收益不能视为未来预期。",
              "- 顾问模式不接触券商；实际成交必须由次日持仓对账确认。", ""]
    return "\n".join(lines)


def generate(result: dict, root: Path | None = None) -> Path:
    root = root or settings.report_dir / "daily"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{result.get('date', 'unknown')}.md"
    path.write_text(render(result), encoding="utf-8")
    return path

