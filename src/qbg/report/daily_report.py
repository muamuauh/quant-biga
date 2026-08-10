"""每日运行结果的中文 Markdown 日报。"""

from __future__ import annotations

from pathlib import Path

from qbg.config import settings


def _number(value, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "-"


def _cell(value, limit: int = 180) -> str:
    text = " ".join(str(value or "").split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(result: dict) -> str:
    status = result.get("skipped_reason") or ("完成" if result.get("hard_ok", True) else "硬闸中止")
    mode = result.get("mode", settings.qbg_mode)
    account = result.get("account") or {}
    positions = result.get("positions") or []
    lines = [f"# quant-biga 日报 · {result.get('date', '')}", "", f"*{mode} · {status}*", "",
             "## 概览", "", "| 项 | 值 |", "|---|---:|",
             f"| 市场状态 | {'risk-on' if result.get('market_risk_on') else 'risk-off'} |",
             f"| 总资产 | ¥{_number(account.get('total_equity'))} |",
             f"| 可用资金 | ¥{_number(account.get('available_cash'))} |",
             f"| 当前持仓 | {len(positions)} 只 |",
             f"| 目标 | {len(result.get('targets', {}))} 只 |",
             f"| 计划订单 | {len(result.get('orders', []))} 笔 |",
             f"| 允许订单 | {len(result.get('allowed_orders', []))} 笔 |", ""]

    if positions:
        lines += ["## 当前持仓", "",
                  "|代码|名称|股数|可卖|成本|现价|市值|盈亏|收益率|",
                  "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
        for position in sorted(positions, key=lambda item: float(item.get("market_value", 0)),
                               reverse=True):
            qty = float(position.get("qty", 0) or 0)
            cost = float(position.get("cost_price", 0) or 0)
            pnl = float(position.get("pnl", 0) or 0)
            basis = qty * cost
            pnl_ratio = pnl / basis if basis else 0.0
            lines.append(
                f"|{_cell(position.get('code'))}|{_cell(position.get('name'))}|"
                f"{int(qty)}|{int(position.get('sellable_qty', 0) or 0)}|"
                f"{_number(cost, 3)}|{_number(position.get('last_price'), 3)}|"
                f"{_number(position.get('market_value'))}|{pnl:+,.2f}|{pnl_ratio:+.2%}|"
            )
        lines.append("")

    verdicts = result.get("agent_verdicts") or []
    if verdicts:
        lines += ["## TradingAgents 复核闸", "",
                  "TradingAgents 只过滤量化候选，不生成主信号；调用异常按 fail-open 放行。", "",
                  "|代码|评级|闸门|理由/异常|", "|---|---|---:|---|"]
        for verdict in verdicts:
            detail = verdict.get("error") or verdict.get("rationale") or "无"
            lines.append(
                f"|{_cell(verdict.get('code'))}|{_cell(verdict.get('rating'))}|"
                f"{'通过' if verdict.get('kept') else '拦截'}|{_cell(detail)}|"
            )
        usage = result.get("agent_usage") or {}
        if usage:
            lines += ["", f"- 调用：{int(usage.get('calls', 0) or 0)} 次",
                      f"- tokens：{int(usage.get('total_tokens', 0) or 0):,}",
                      f"- 估算成本：${float(usage.get('cost_usd', 0) or 0):.4f}"]
        lines.append("")

    if result.get("orders"):
        lines += ["## 订单摘要", "", "|代码|方向|股数|限价|原因|", "|---|---:|---:|---:|---|"]
        for order in result["orders"]:
            lines.append(f"|{order['code']}|{order['side']}|{order['quantity']}|"
                         f"{order['price']:.2f}|{_cell(order['reason'])}|")
        lines.append("")
    if result.get("gates"):
        lines += ["## 风控", ""]
        lines += [f"- {'✅' if gate['passed'] else '⚠️'} {gate['name']}：{gate['reason']}"
                  for gate in result["gates"]]
    if result.get("daily_review"):
        review = result["daily_review"]
        lines += ["", "## 自动复盘", "",
                  f"- 状态：{'完成' if review.get('ok') else '失败'}",
                  f"- 分级：{review.get('status') or '无'}",
                  f"- 结论：{review.get('summary') or review.get('error') or '无'}"]
        if review.get("report_path"):
            lines.append(f"- 报告：`{review['report_path']}`")
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
