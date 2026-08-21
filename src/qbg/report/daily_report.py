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


def _status(result: dict) -> str:
    skipped = result.get("skipped_reason")
    labels = {
        "not_rebalance_day": "监控日",
        "not_trading_day": "非交易日",
        "already_completed_today": "今日已完成",
    }
    if skipped:
        return labels.get(str(skipped), str(skipped))
    if not result.get("hard_ok", True):
        return "⚠ 硬闸中止"
    review = result.get("daily_review") or {}
    if review and not review.get("ok"):
        return "⚠ 复盘失败"
    if result.get("submitted"):
        return f"已生成清单 {len(result.get('allowed_orders') or [])} 笔"
    if result.get("allowed_orders"):
        return f"有订单建议 {len(result.get('allowed_orders') or [])} 笔 · 未执行"
    if result.get("orders"):
        return "无允许订单"
    return "正常 · 无订单"


def _headline(result: dict) -> str:
    review = result.get("daily_review") or {}
    review_text = review.get("summary") or review.get("error")
    if review_text:
        if result.get("orders"):
            allowed = len(result.get("allowed_orders") or [])
            execution = "已生成下单清单" if result.get("submitted") else "仅供参考，未执行"
            review_text = f"{review_text} 风控允许 {allowed} 笔订单；{execution}。"
        return _cell(review_text, 260)
    skipped = result.get("skipped_reason")
    if skipped == "not_rebalance_day":
        return "今日为监控日：已更新账户与持仓事实，不生成新的调仓订单。"
    if skipped:
        return f"本次流程未执行：{_status(result)}。"
    if not result.get("hard_ok", True):
        return "硬风控闸未通过，本次不生成可执行清单。"
    targets = len(result.get("targets") or {})
    allowed = len(result.get("allowed_orders") or [])
    execution = "已生成下单清单" if result.get("submitted") else "仅供决策参考，未执行"
    return f"量化模型给出 {targets} 个目标，风控允许 {allowed} 笔订单；{execution}。"


_SOURCE_LABELS = {
    "easytrader": "同花顺客户端直读",
    "ocr": "持仓截图 OCR 产出的 CSV",
    "manual": "手工维护的 CSV",
    "default": "无持仓记录，按默认初始资金假设",
}


def _portfolio_provenance(portfolio: dict) -> list[str]:
    """持仓从哪来、截止到哪天、有没有降级。

    这段必须显眼：easytrader 读失败会退回 CSV，而**用过期持仓出的清单
    和正常清单长得一模一样**。不写出来，没人会发现自己在照着几天前的持仓下单。
    """
    if not portfolio:
        return []
    source = str(portfolio.get("source") or "")
    label = _SOURCE_LABELS.get(source, source or "未知")
    asof = str(portfolio.get("asof") or "").strip()
    line = f"**持仓来源**：{label}"
    if asof:
        line += f"（截止 {asof}）"
    lines = [line, ""]
    degraded = portfolio.get("degraded")
    if degraded:
        lines = [
            f"> ⚠️ **持仓来源已降级**：`{degraded.get('from')}` 读取失败，"
            f"改用{label}"
            + (f"（截止 {asof}）" if asof else "")
            + "。",
            f"> 失败原因：`{_cell(str(degraded.get('reason') or ''))}`",
            "> **下面的持仓可能是过期的，据此产生的订单请人工复核后再执行。**",
            "",
        ]
    return lines


def render(result: dict) -> str:
    mode = result.get("mode", settings.qbg_mode)
    account = result.get("account") or {}
    positions = result.get("positions") or []
    targets = result.get("targets") or {}
    orders = result.get("orders") or []
    allowed_orders = result.get("allowed_orders") or []
    gates = result.get("gates") or []
    passed_gates = sum(bool(gate.get("passed")) for gate in gates)
    lines = [f"# 复盘 · {result.get('date', '')}", "", f"*{str(mode).upper()} · {_status(result)}*", "",
             "## 概览", "", "| 项目 | 结果 |", "|---|---:|",
             f"| 总资产 | ¥{_number(account.get('total_equity'))} |",
             f"| 可用资金 | ¥{_number(account.get('available_cash'))} |",
             f"| 运行类型 | {_cell(str(mode).upper())} |",
             f"| 市场状态 | {'risk-on' if result.get('market_risk_on') else 'risk-off'} |",
             f"| 当前持仓 | {len(positions)} 只 |",
             f"| 量化目标 | {len(targets)} 只 |",
             f"| 订单意见 | {len(allowed_orders)}/{len(orders)} 笔允许 |",
             f"| 风控闸门 | {passed_gates}/{len(gates)} 项通过 |", "",
             "## 一句话结论", "", f"> {_headline(result)}", "",
             "## 账户与持仓", ""]

    lines += _portfolio_provenance(result.get("portfolio") or {})

    unrealized = sum(float(position.get("pnl", 0) or 0) for position in positions)
    if positions:
        lines += [
            f"账户当前持有 **{len(positions)}** 只股票，持仓浮动盈亏合计 "
            f"**¥{unrealized:+,.2f}**。以下均为本次流程读取的账户事实。",
            "",
        ]
    else:
        lines += ["当前没有可展示的持仓记录。", ""]

    if positions:
        lines += ["|代码|名称|股数|可卖|成本|现价|市值|盈亏|收益率|",
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
    if targets or verdicts:
        lines += ["## 量化选择与 TradingAgents 复核", "",
                  "TradingAgents 复核闸只过滤量化候选，不生成主信号；调用异常按 fail-open 放行。", ""]
        if targets:
            verdict_by_code = {str(item.get("code")): item for item in verdicts}
            lines += ["|代码|目标权重|复核评级|结果|", "|---|---:|---|---:|"]
            for code, weight in sorted(targets.items(), key=lambda item: float(item[1]), reverse=True):
                verdict = verdict_by_code.get(str(code), {})
                gate_result = (
                    "通过" if verdict.get("kept") else "拦截"
                ) if verdict else "未复核"
                lines.append(
                    f"|{_cell(code)}|{float(weight):.2%}|{_cell(verdict.get('rating') or '未复核')}|"
                    f"{gate_result}|"
                )
            lines.append("")
    if verdicts:
        lines += ["### 复核明细", "",
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

    lines += ["## 订单意见", ""]
    if orders:
        allowed_keys = {
            (str(order.get("code")), str(order.get("side"))) for order in allowed_orders
            if order.get("code") is not None and order.get("side") is not None
        }
        lines += ["|代码|名称|方向|股数|限价|风控|原因|",
                  "|---|---|---:|---:|---:|---:|---|"]
        for order in orders:
            key = (str(order.get("code")), str(order.get("side")))
            lines.append(
                f"|{_cell(order.get('code'))}|{_cell(order.get('name'))}|"
                f"{_cell(order.get('side'))}|{int(order.get('quantity', 0) or 0)}|"
                f"{_number(order.get('price'))}|{'允许' if key in allowed_keys else '拦截'}|"
                f"{_cell(order.get('reason'))}|"
            )
        lines.append("")
        if not result.get("submitted"):
            lines += ["> 当前为顾问/演练流程：以上是订单意见，**没有向券商提交订单**。", ""]
    else:
        lines += ["本次没有生成订单意见。", ""]

    lines += ["## 运行健康", ""]
    if gates:
        lines += [f"风控闸门共 **{len(gates)}** 项，通过 **{passed_gates}** 项。", "",
                  "|检查项|结果|说明|", "|---|---:|---|"]
        lines += [
            f"|{_cell(gate.get('name'))}|{'✅ 通过' if gate.get('passed') else '⚠️ 未通过'}|"
            f"{_cell(gate.get('reason'))}|" for gate in gates
        ]
        lines.append("")
    else:
        lines += ["本次结果中没有风控闸门明细。", ""]

    if result.get("daily_review"):
        review = result["daily_review"]
        review_status = {
            "normal": "正常",
            "attention": "需关注",
            "manual_action": "需人工处理",
        }.get(review.get("status"), review.get("status") or "无")
        lines += ["## 自动复盘", "",
                  f"- 状态：{'完成' if review.get('ok') else '失败'}",
                  f"- 分级：{review_status}",
                  f"- 结论：{review.get('summary') or review.get('error') or '无'}"]
        usage = review.get("usage") or {}
        if usage.get("total_tokens"):
            lines.append(f"- tokens：{int(usage['total_tokens']):,}")
        if review.get("report_path"):
            lines.append(f"- 报告：`{review['report_path']}`")
        lines.append("")
    lines += ["## 建议与限制", "",
              "- 当前历史股票池有生存者偏差，回测收益不能视为未来预期。",
              "- 顾问模式不接触券商；实际成交必须由次日持仓对账确认。", ""]
    return "\n".join(lines)


def generate(result: dict, root: Path | None = None) -> Path:
    root = root or settings.report_dir / "daily"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{result.get('date', 'unknown')}.md"
    path.write_text(render(result), encoding="utf-8")
    return path
