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


def _outcome_facts(result: dict) -> list[str]:
    """把「这一趟到底成没成」拆成几句人话。

    2026-08-25 的教训：那天持仓读取降级到虚构账户、3 笔单一笔都没进券商，
    而一句话结论写的是「已生成下单清单」，邮件主题写的是「已提交3笔」。
    **两处都在报喜。** 真正出事的两件事一个字都没提。

    所以结论必须由**实际发生的事**倒推，而不是由"我们打算做什么"正推。
    """
    facts: list[str] = []
    portfolio = result.get("portfolio") or {}
    if str(portfolio.get("source") or "") == "default" and portfolio.get("degraded"):
        facts.append("⚠ 持仓完全读不到，本次用的是默认假设账户，结论不可照做")

    broker = result.get("broker")
    if broker is not None:
        outcomes = broker.get("outcomes") or []
        ok = sum(1 for o in outcomes if o.get("ok"))
        planned = len(result.get("allowed_orders") or [])
        if broker.get("ok") and ok:
            facts.append(f"券商已接单 {ok}/{planned} 笔并通过回读校验")
        elif ok:
            facts.append(f"券商只接了 {ok}/{planned} 笔，其余失败或未尝试")
        else:
            facts.append(f"⚠ 下单全部未成功（0/{planned} 笔）")
    return facts


def _headline(result: dict) -> str:
    """一句话结论：先说结果，再说过程。"""
    facts = _outcome_facts(result)
    review = result.get("daily_review") or {}
    review_text = review.get("summary") or review.get("error")

    skipped = result.get("skipped_reason")
    if skipped == "not_rebalance_day":
        base = "今日为监控日：已更新账户与持仓事实，不生成新的调仓订单。"
    elif skipped:
        base = f"本次流程未执行：{_status(result)}。"
    elif not result.get("hard_ok", True):
        base = "硬风控闸未通过，本次不生成可执行清单。"
    else:
        targets = len(result.get("targets") or {})
        allowed = len(result.get("allowed_orders") or [])
        # 只有在**没有**券商回执时才谈"清单"；有回执时结果由 facts 说了算，
        # 再说一遍"已生成下单清单"只会掩盖失败。
        tail = ("" if result.get("broker") is not None else
                ("已生成下单清单。" if result.get("submitted") else "仅供决策参考，未执行。"))
        base = f"量化模型给出 {targets} 个目标，风控允许 {allowed} 笔订单；{tail}".rstrip("；")
        if not base.endswith("。"):
            base += "。"

    parts = list(facts)
    if review_text:
        parts.append(_cell(review_text, 200))
    parts.append(base)
    return _cell(" ".join(parts), 400)


def _rationale(text) -> str:
    """把 LLM 的复核理由原样铺开，只做最低限度的规整。

    **不截断。** 这段文字是 P7 每票十几次 LLM 调用换来的唯一产物，
    截掉就等于把钱花了却看不到结论。

    只做两件事：去掉重复的 `**Rating**:` 行（评级已经在标题里了），
    以及把各小节之间补上空行，让 markdown→HTML 转换时能正确分段。
    """
    body = str(text or "").strip()
    if not body:
        return "_无复核理由_"
    kept = [ln.strip() for ln in body.splitlines()
            if ln.strip() and not ln.strip().startswith("**Rating**")]
    return "\n\n".join(kept)


def _trading_costs(orders: list[dict]) -> dict:
    """按 A股规则把交易成本拆开算。

    **不能只报一个总数。** A股成本是**不对称**的 —— 印花税 0.05% 只在卖出时
    收，所以同样金额的买单和卖单成本差 5bp。把它们加成一个数，就看不出
    「今天的成本主要来自卖出」这种事实，也就没法判断调仓频率是不是太高了。

    最低佣金也要单独盯：10 万账户 k=3 时单笔约 3 万，佣金 7.5 元 > 5 元不生效；
    但 k 调大到 10（单笔 1 万）就会被拉到 5 元，**实际费率翻倍到万 5**。
    这是"多分散一点"在小账户上的隐藏代价，报出来才看得见。
    """
    from qbg.execution import fees

    profile = fees.FeeProfile.load()
    totals = {"notional": 0.0, "commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0,
              "buy_notional": 0.0, "sell_notional": 0.0, "min_commission_hits": 0}
    for order in orders:
        notional = float(order.get("notional") or 0.0)
        side = str(order.get("side") or "").upper()
        if notional <= 0 or side not in ("BUY", "SELL"):
            continue
        fee = fees.estimate(notional, side, profile)
        totals["notional"] += notional
        totals["buy_notional" if side == "BUY" else "sell_notional"] += notional
        totals["commission"] += fee.commission
        totals["stamp_tax"] += fee.stamp_tax
        totals["transfer_fee"] += fee.transfer_fee
        if notional * profile.commission_rate < profile.commission_min:
            totals["min_commission_hits"] += 1
    totals["total"] = totals["commission"] + totals["stamp_tax"] + totals["transfer_fee"]
    totals["bp"] = (totals["total"] / totals["notional"] * 1e4) if totals["notional"] else 0.0
    return totals


def _cost_section(result: dict) -> list[str]:
    """今日成本明细：交易费用 + LLM 调用。

    为什么值得单开一段：`plan.md` §8.5 把「成本吃掉 alpha」列为小账户的第一
    杀手 —— quant-trading 的实测是 3000 美元账户日频调仓被费用和滑点打到
    年化 −30%。成本不摆在日报上，这件事就只能靠回测发现，而回测发现得太晚。
    """
    # **成本要按实际发生的算，不能按计划的算。**
    # 2026-08-25 实测那次：3 笔单一笔都没进券商，日报却照样列出 ¥17.50 佣金和
    # 「成交额 ¥66,743」—— 全是凭空的。走了券商就以券商接单为准；
    # 顾问模式下如实标明这是**预估**。
    planned = result.get("allowed_orders") or []
    broker = result.get("broker")
    if broker is not None:
        done = {(str(o.get("code")), str(o.get("side")).upper())
                for o in (broker.get("outcomes") or []) if o.get("ok")}
        orders = [o for o in planned
                  if (str(o.get("code")), str(o.get("side")).upper()) in done]
        basis = f"已委托 {len(orders)}/{len(planned)} 笔"
    else:
        orders = planned
        basis = f"计划 {len(orders)} 笔（预估，实际以成交为准）"
    trade = _trading_costs(orders)
    llm = result.get("agent_usage") or {}
    review_usage = (result.get("daily_review") or {}).get("usage") or {}
    llm_cost = float(llm.get("cost_usd") or 0.0)

    if not planned and not llm and not review_usage:
        return []

    lines = ["## 今日成本", "", f"交易成本口径：**{basis}**。", "",
             "| 项目 | 金额 | 说明 |", "|---|---:|---|"]

    if trade["notional"] <= 0 and planned:
        lines.append("| 交易成本 | ¥0.00 | 没有订单真正进入券商，不产生费用 |")
    if trade["notional"] > 0:
        lines += [
            f"| 佣金 | ¥{_number(trade['commission'])} | 双边，"
            + (f"其中 {trade['min_commission_hits']} 笔触及最低佣金"
               if trade["min_commission_hits"] else "未触及最低佣金") + " |",
            f"| 印花税 | ¥{_number(trade['stamp_tax'])} | **仅卖出**，卖出额 "
            f"¥{_number(trade['sell_notional'])} |",
            f"| 过户费 | ¥{_number(trade['transfer_fee'])} | 双边 |",
            f"| **交易成本合计** | **¥{_number(trade['total'])}** | "
            f"成交额 ¥{_number(trade['notional'])} 的 **{trade['bp']:.1f} bp** |",
        ]
    if llm:
        lines.append(
            f"| LLM 逐票复核 | ${llm_cost:.4f} | {int(llm.get('calls', 0) or 0)} 次调用 · "
            f"{int(llm.get('total_tokens', 0) or 0):,} tokens |")
    if review_usage.get("total_tokens"):
        lines.append(
            f"| LLM 自动复盘 | — | {int(review_usage['total_tokens']):,} tokens"
            "（该链路未计价） |")
    lines.append("")

    if trade["notional"] > 0:
        # 把成本换算成"要涨多少才回本"，比一个绝对数更能说明问题。
        lines += [f"> 本次调仓的交易成本相当于持仓需上涨 **{trade['bp'] / 1e4:.4%}** 才能打平。"
                  "往返（买入再卖出）约为其两倍。", ""]
    return lines


def _broker_section(result: dict) -> list[str]:
    """计划 vs 实际委托对账（P9c）。

    只有真的走了券商才有这一段。它回答的是「清单上写的和券商那边实际收到的
    是不是同一回事」—— 这也是 P9b 那套三道校验的最终产物：
    每一笔都带着券商给的合同编号，或者带着失败原因。

    **失败必须显眼。** 下单失败时顾问清单照样存在、日报照样生成，
    如果这里不写清楚，看日报的人会以为清单上的单子都下出去了。
    """
    broker = result.get("broker")
    if not broker:
        return []
    mode = result.get("execution_mode", "")
    outcomes = broker.get("outcomes") or []
    ok_count = sum(1 for o in outcomes if o.get("ok"))
    # 分母用**计划笔数**而不是 outcomes 的长度。下单是遇错即停的，失败时
    # outcomes 里往往只有尝试过的那一两笔，写成「0/1」会让人以为只计划了 1 笔，
    # 而实际上有 3 笔（其中 2 笔根本没试）。
    planned = len(result.get("allowed_orders") or []) or len(outcomes)
    untried = max(planned - len(outcomes), 0)
    header = f"执行模式 **{mode}** · 券商回执 **{ok_count}/{planned}** 笔通过回读校验"
    if untried:
        header += f"（其中 {untried} 笔因中止未尝试）"
    lines = ["## 计划 vs 实际委托", "", header, ""]
    if not broker.get("ok"):
        lines += [f"> ⚠️ **下单未全部成功**：{_cell(str(broker.get('message') or ''))}",
                  "> 顾问清单仍然有效，请人工核对后决定是否补单。", ""]
    if outcomes:
        lines += ["|代码|方向|股数|委托价|结果|合同编号|说明|", "|---|---|---:|---:|---|---|---|"]
        for item in outcomes:
            lines.append(
                f"|{item.get('code','')}|{item.get('side','')}|{item.get('quantity',0)}|"
                f"{_number(item.get('price'))}|{'✅' if item.get('ok') else '❌'}|"
                f"{item.get('entrust_no') or '—'}|{_cell(str(item.get('message') or ''), 60)}|")
        lines.append("")
    return lines


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
    if degraded and source == "default":
        # 一路降到默认假设账户 —— 这是最危险的一种：清单是照着一个**虚构账户**
        # 算出来的。必须比普通降级更响。
        return [
            f"> 🔴 **持仓完全读不到**：`{degraded.get('from')}` 失败后连降级路径也没成功，"
            "本次使用的是**默认假设账户**，不是你的真实持仓。",
            f"> 失败原因：`{_cell(str(degraded.get('reason') or ''))}`",
            "> **下面的一切（持仓、目标、订单、成本）都建立在这个假设之上，不要照着执行。**",
            "> PAPER/LIVE 模式下已自动拒绝向券商下单。",
            "",
        ]
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
        # **理由不能塞进表格单元格。** LLM 的复核理由是 400–600 字的结构化文本
        # （Executive Summary / Investment Thesis / Time Horizon）。塞进表格会被
        # `_cell` 砍到 180 字并把换行压平，剩下的还要横向溢出 —— 邮件里根本
        # 拉不动，等于白花了那几毛钱的 token。
        #
        # 所以拆成两段：先用表格给一眼看完的结论，再每票一块展开完整理由。
        lines += ["### 复核明细", "", "|代码|评级|闸门|", "|---|---|---:|"]
        for verdict in verdicts:
            lines.append(
                f"|{_cell(verdict.get('code'))}|{_cell(verdict.get('rating'))}|"
                f"{'通过' if verdict.get('kept') else '拦截'}|"
            )
        lines.append("")
        for verdict in verdicts:
            code, rating = verdict.get("code"), verdict.get("rating") or "未复核"
            gate = "通过" if verdict.get("kept") else "拦截"
            lines += [f"#### {_cell(code)} · {_cell(rating)} · {gate}", ""]
            if verdict.get("error"):
                lines += [f"> ⚠️ 复核异常：{_cell(str(verdict['error']), 400)}", ""]
            else:
                lines += [_rationale(verdict.get("rationale")), ""]
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
        elif not result.get("broker"):
            # 提交了但没走券商 = ADVISORY 模式，只落了清单。写清楚，
            # 否则「submitted=True」很容易被读成「单子下出去了」。
            lines += ["> ADVISORY 模式：已生成下单清单，**未向券商提交**，请人工执行。", ""]
    else:
        lines += ["本次没有生成订单意见。", ""]

    lines += _broker_section(result)
    lines += _cost_section(result)

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
    # 这段以前把「顾问模式不接触券商」写死了，于是 PAPER 模式真的下了单
    # 之后，日报还在告诉人"本系统不接触券商"。按实际模式说话。
    mode = str(result.get("execution_mode") or result.get("mode") or "ADVISORY").upper()
    lines += ["## 建议与限制", "",
              "- 当前历史股票池有生存者偏差，回测收益不能视为未来预期。"]
    if mode == "ADVISORY":
        lines.append("- 顾问模式不接触券商；实际成交必须由次日持仓对账确认。")
    else:
        lines.append(f"- **{mode} 模式已直接向券商下单**；成交结果以「计划 vs 实际委托」"
                     "一节的合同编号为准，并由次日持仓复核。")
    lines.append("")
    return "\n".join(lines)


def generate(result: dict, root: Path | None = None) -> Path:
    root = root or settings.report_dir / "daily"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{result.get('date', 'unknown')}.md"
    path.write_text(render(result), encoding="utf-8")
    return path
