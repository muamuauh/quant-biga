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
        "not_trading_session": "非交易时段",
        "already_completed_today": "今日已完成",
    }
    # 监控日**如果移动止盈动了手**，状态必须由下单结果决定，不能停在"监控日"。
    # 否则一笔卖失败的止盈单，副标题和邮件主题都会写成风平浪静的"监控日" ——
    # 和 2026-08-25 "报喜不报忧"是同一个毛病。
    exits_acted = bool((result.get("exits") or {}).get("orders"))
    if skipped and not (skipped == "not_rebalance_day" and exits_acted):
        return labels.get(str(skipped), str(skipped))
    if not result.get("hard_ok", True):
        return "⚠ 硬闸中止"
    review = result.get("daily_review") or {}
    if review and not review.get("ok"):
        return "⚠ 复盘失败"
    # 券商回执优先于"清单已生成"。副标题和主题、一句话结论是同一个毛病：
    # submitted 只说明顾问清单落了盘，不代表券商收到了单。
    portfolio = result.get("portfolio") or {}
    if str(portfolio.get("source") or "") == "default" and portfolio.get("degraded"):
        return "⚠ 持仓读不到"
    broker = result.get("broker")
    if broker is not None:
        planned = len(result.get("allowed_orders") or [])
        ok = sum(1 for o in (broker.get("outcomes") or []) if o.get("ok"))
        if planned == 0:
            return "正常 · 无订单"      # 没打算下单，不是失败
        if ok == 0:
            return f"⚠ 下单失败 0/{planned} 笔"
        return f"{'⚠ ' if ok < planned else ''}券商接单 {ok}/{planned} 笔"
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
        if planned == 0:
            pass                        # 没打算下单，没什么可汇报的
        elif broker.get("ok") and ok:
            facts.append(f"券商已接单 {ok}/{planned} 笔并通过回读校验")
        elif ok:
            facts.append(f"券商只接了 {ok}/{planned} 笔，其余失败或未尝试")
        elif any(o.get("verified") is False for o in outcomes):
            facts.append("🔴 下单结果**无法确认**（回读不可信）——"
                         "可能已成交，先人工核对再决定，别补单")
        else:
            facts.append(f"⚠ 下单全部未成功（0/{planned} 笔）")
    return facts


def _headline(result: dict) -> str:
    """一句话结论：先说结果，再说过程。"""
    facts = _outcome_facts(result)
    review = result.get("daily_review") or {}
    review_text = review.get("summary") or review.get("error")

    skipped = result.get("skipped_reason")
    exits = result.get("exits") or {}
    what = _exit_kinds(exits.get("hits") or [])
    if skipped == "not_rebalance_day" and exits.get("orders"):
        n = len(exits["orders"])
        base = f"今日为监控日：{what}触发，生成 {n} 笔卖单。不换股，也不重置调仓周期。"
        if not result.get("hard_ok", True):
            base = f"今日为监控日：{what}触发 {n} 笔，但硬风控闸未通过，**没有卖出**。"
    elif skipped == "not_rebalance_day" and exits.get("refused"):
        base = f"今日为监控日：{what}触发，但未下单 —— {exits['refused']}。"
    elif skipped == "not_rebalance_day":
        base = "今日为监控日：已更新账户与持仓事实，不生成新的调仓订单。"
        if exits.get("stop_enabled") or exits.get("trailing_enabled"):
            base += "止损与移动止盈均无持仓触发。"
    elif skipped == "not_trading_session":
        # 这不是故障。自动下单要求在盘中，盘前/盘后触发就安静跳过 ——
        # 说清楚它是**预期行为**，否则每次登录都收到一封像出事了的邮件。
        base = ("当前不在交易时段，已提前退出（自动下单必须在盘中）。"
                "这是预期行为，不是故障；下一个交易时段内的计划任务会正常执行。")
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


# 失控兜底。真实理由 400~600 字（本仓库 19 条历史记录：均值 459、最长 564），
# 所以这个上限**平时永远不会生效** —— 它防的是 LLM 死循环那种病态输出，
# 那种东西会把日报和邮件一起撑爆。和 quant-trading / quant-agent 取同一个值。
RATIONALE_MAX_CHARS = 8000

# 水平分割线：整行只由 - * _ 组成且至少三个。模型有时会用它分节，
# 但在日报里它会把那一票的内容割成几块，视觉上像是好几条独立记录。
_HR_CHARS = {"-", "*", "_"}


def _rationale(text) -> str:
    """把 LLM 的复核理由铺开，只做最低限度的规整。

    **不按字数截断。** 这段文字是 P7 每票十几次 LLM 调用换来的唯一产物，
    截掉就等于把钱花了却看不到结论 —— 而且截断总是从后面开始，
    砍掉的恰好是 Price Target / Time Horizon 这些最具体的部分。
    （quant-trading 和 quant-agent 2026-08-31 各自修掉了 600 / 400 字的截断，
    本仓库 08-25 已经先一步改成不截断。）

    只做四件事：

    1. 去掉重复的 `**Rating**` 行 —— 评级已经在小标题里了。
    2. **把模型输出的 `#` 标题降级成粗体。** 不降级的话它们会变成真正的
       markdown 标题，混进日报自己的大纲里 —— 一条复核理由能让"### 复核明细"
       下面凭空冒出几个和报告结构平级的章节。
    3. **丢掉模型输出的水平分割线。** 它会把一票的内容割成几块，
       看起来像好几条独立记录。
    4. 只在**超长**时截断（见 RATIONALE_MAX_CHARS），并明确标注。

    2、3 两条在本仓库的历史数据里一次都没触发过（19 条全都没有标题和分割线），
    加它们是因为**两个兄弟仓库都实际遇到了** —— 同一个上游 TradingAgents，
    只是换了模型，输出格式随时可能变成那样。
    """
    body = str(text or "").strip()
    if not body:
        return "_无复核理由_"
    if len(body) > RATIONALE_MAX_CHARS:
        body = body[:RATIONALE_MAX_CHARS] + "…（过长已截断）"

    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if line.startswith("**Rating**"):
            continue
        stripped = line.replace(" ", "")
        if len(stripped) >= 3 and set(stripped) <= _HR_CHARS:
            continue                      # 模型自己的分割线
        if line.startswith("#"):
            line = line.lstrip("#").strip()
            line = f"**{line}**" if line else ""
        if not line and lines and not lines[-1]:
            continue                      # 连续空行压成一个
        lines.append(line)
    while lines and not lines[-1]:
        lines.pop()
    # 每个非空段落之间留一个空行，markdown→HTML 才会分段。
    out = "\n\n".join(line for line in lines if line)
    return out or "_无复核理由_"


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


def _regime_section(result: dict) -> list[str]:
    """市场择时状态，以及它**此刻正在做什么决定**。

    为什么值得单开一段：概览表里那个 `risk-off` 是全篇最有后果的一个词 ——
    它一旦成立，目标持仓直接清零，当天所有订单都是卖出。而表格里孤零零一个
    英文词完全传达不出这件事，看日报的人会以为"清仓"是选股模型挑出来的结论。
    不是。选股分数照常在算，只是**一个都不会被采用**。
    """
    from qbg.config import settings as _s

    window = int(_s.qbg_market_sma or 0)
    if not window or result.get("market_risk_on") is None:
        return []   # 择时关掉了，或者今天压根没判（监控日）
    risk_on = bool(result["market_risk_on"])
    lines = ["## 市场择时", ""]
    if risk_on:
        lines += [f"> 🟢 **risk-on**：股票池等权指数在 {window} 日均线**之上**，"
                  "正常按模型分数选股建仓。", ""]
        return lines
    lines += [
        f"> 🔴 **risk-off**：股票池等权指数跌破 {window} 日均线 —— "
        "**目标持仓直接清零，当天全部订单都是卖出**。",
        "",
        "这不是选股模型的结论。分数照常在算（见下方「量化选择」），"
        "只是 risk-off 时一个都不会被采用 —— 择时闸在选股之后、下单之前，"
        "一票否决。",
        "",
        f"择时的作用是**削回撤，不是增收益**。含成本压测（2020–2026，10bp 滑点）："
        f"不择时年化 16.85% / 最大回撤 −22.86%，SMA={window} 年化 11.19% / "
        "回撤 −18.61%。用大约 5 个点的年化换约 4 个点的回撤。",
        "",
        "**指数重新站上均线之前不会自动买回。** 这是设计如此：择时的全部价值就在于"
        "下跌段不在场，中途抢反弹会把这个价值抵消掉。",
        "",
    ]
    return lines


def _regime_brief(result: dict) -> str:
    """概览表的"市场状态"。

    **监控日根本不判择时**，`market_risk_on` 停在初始值 False。以前直接拿它渲染，
    于是每个监控日都显示 risk-off —— 调仓改成 10 日之后，十天里有九天会显示一个
    假的 risk-off。择时关闭时同理，那个词没有意义。
    """
    if not int(settings.qbg_market_sma or 0):
        return "择时已关闭"
    if result.get("skipped_reason") == "not_rebalance_day":
        return "—（监控日不判）"
    return "risk-on" if result.get("market_risk_on") else "risk-off"


def _rebalance_brief(result: dict) -> str:
    """概览表里那一格。"""
    info = result.get("rebalance") or {}
    if not info:
        return "—"
    if int(info.get("every_days") or 1) <= 1:
        return "每日调仓"
    if info.get("is_today"):
        return "**调仓日**（现金触发提前）" if info.get("cash_triggered") else "**调仓日**"
    tail = f" · 下次 {info['next']}" if info.get("next") else ""
    return f"监控日{tail}"


def _rebalance_section(result: dict) -> list[str]:
    """调仓周期：今天是什么日子、上次什么时候、下次什么时候。

    调仓间隔拉长到 10 日之后，**十天里有九天是监控日**。那九天日报没有订单、
    没有目标，看起来像"系统没干活"。这一节把"为什么今天不换股、哪天会换"
    讲清楚，免得操作者以为出了故障去手动补跑。
    """
    info = result.get("rebalance") or {}
    every = int(info.get("every_days") or 0)
    if not info or every <= 1:
        return []
    since = info.get("days_since")
    cash_fraction = float(info.get("cash_fraction") or 0.0)
    trigger = float(info.get("cash_trigger") or 0.0)

    if info.get("is_today"):
        today = "🔁 **调仓日** —— 按模型分数重新选股"
        if info.get("cash_triggered"):
            today += (f"（**提前调仓**：现金占比 {cash_fraction:.0%} 超过 {trigger:.0%}，"
                      "不等周期到期）")
    else:
        today = "👀 **监控日** —— 不换股，只读账户、检查移动止盈"

    last = info.get("last")
    last_text = (f"{last}（已过 {since} 个交易日）" if last and since is not None
                 else "尚无记录 —— 首次运行当天即调仓")
    if info.get("is_today"):
        nxt = info.get("next_after_today")
        next_text = (f"若今天调仓成功，下一次约在 **{nxt}**（{every} 个交易日后）"
                     if nxt else "若今天调仓成功，下一次在 10 个交易日后（交易日历不够远，算不出日期）")
    elif info.get("next"):
        remaining = every - int(since or 0)
        next_text = f"**{info['next']}**（还有 {remaining} 个交易日）"
    else:
        next_text = "算不出（交易日历缓存不够远）"

    lines = ["## 调仓周期", "", "| 项目 | 值 |", "|---|---|",
             f"| 调仓间隔 | 每 {every} 个交易日 |",
             f"| 今天 | {today} |",
             f"| 上次调仓 | {last_text} |",
             f"| 下次调仓 | {next_text} |"]
    if trigger > 0:
        lines.append(f"| 现金占比 | {cash_fraction:.1%}（超过 {trigger:.0%} 会提前调仓）|")
    lines += ["", "下次调仓日是**估算**：现金占比一旦超过阈值（比如移动止盈卖掉两只之后），"
              "会提前到第二天调仓。", ""]
    return lines


def _exit_kinds(hits: list[dict]) -> str:
    kinds = {h.get("kind") for h in hits}
    if kinds == {"stop_loss"}:
        return "止损"
    if kinds == {"trailing"}:
        return "移动止盈"
    return "止损 / 移动止盈"


def _exits_section(result: dict) -> list[str]:
    """止损与移动止盈：参数、每只持仓离两条线各多远、今天动没动手。"""
    info = result.get("exits") or {}
    if not info:
        return []
    stop_on, trail_on = bool(info.get("stop_enabled")), bool(info.get("trailing_enabled"))
    lines = ["## 止损与移动止盈", ""]
    stop = float(info.get("stop_pct") or 0)
    arm, trail = float(info.get("arm_pct") or 0), float(info.get("trail_pct") or 0)
    lines.append(f"- **止损**：建仓以来亏损达到 **{stop:.0%}** 就卖；**调仓日也生效**，"
                 "槽位当天让给下一名。" if stop_on else
                 "- **止损**：未启用（`risk_limits.yaml` 的 `stop_loss_pct` 为 0）。")
    lines.append(f"- **移动止盈**：浮盈峰值达到 **+{arm:.0%}** 上膛，从峰值回撤 **{trail:.0%}** "
                 "卖出；**只在监控日执行**。" if trail_on else
                 "- **移动止盈**：未启用（`QBG_TRAIL_ARM_PCT` / `QBG_TRAIL_PCT` 为 0）。"
                 "八项闸结论是不开，见 `config.py`。")
    lines.append("")
    if not (stop_on or trail_on):
        return lines

    watch = info.get("watch") or []
    if watch:
        lines += ["|代码|名称|成本|现价|浮盈|止损|移动止盈|",
                  "|---|---|---:|---:|---:|---|---|"]

        # 离任一条线最近的排最前。to_* 都是"还要再动多少"，取绝对值比较。
        def _closeness(r):
            gaps = []
            if stop_on and "to_stop" in r:
                gaps.append(0.0 if r.get("stopped") else abs(float(r["to_stop"])))
            if trail_on:
                if r.get("fired"):
                    gaps.append(0.0)
                elif r.get("armed"):
                    gaps.append(abs(float(r["to_trigger"])))
            return min(gaps) if gaps else 9.0

        for row in sorted(watch, key=_closeness):
            if not stop_on:
                stop_state = "—"
            elif row.get("stopped"):
                stop_state = "🔔 **触发**"
            else:
                stop_state = f"再跌 {abs(float(row['to_stop'])):.1%}（{row['stop_price']}）"
            if not trail_on:
                trail_state = "—"
            elif row.get("fired"):
                trail_state = "🔔 **触发**"
            elif row.get("armed"):
                trail_state = (f"已上膛，再跌 {abs(float(row['to_trigger'])):.1%} "
                               f"（{row['trigger_price']}）")
            else:
                trail_state = f"未上膛，再涨 {float(row['to_arm']):.1%}"
            lines.append(
                f"|{_cell(row['code'])}|{_cell(row.get('name'))}|{_number(row['cost'], 3)}|"
                f"{_number(row['last'], 3)}|{float(row.get('pnl', 0)):+.1%}|"
                f"{stop_state}|{trail_state}|")
        lines.append("")
    elif not info.get("trusted", True):
        lines += ["> ⚠ 持仓来自降级数据源，本次**不判止损、不更新峰值、不强卖**。", ""]

    rebalance = result.get("rebalance") or {}
    if rebalance.get("is_today"):
        excluded = info.get("excluded_on_rebalance") or []
        if excluded:
            lines += [f"今天是调仓日：**{'、'.join(excluded)}** 触发止损，已从选股里剔除、"
                      "目标清零，槽位让给下一名。", ""]
        if trail_on:
            lines += ["移动止盈调仓日**不强卖**，峰值照常更新。", ""]
        return lines

    hits = info.get("hits") or []
    if info.get("refused"):
        lines += [f"> ⚠ 触发 {len(hits)} 只，但**没有下单**：{info['refused']}", ""]
    elif hits:
        orders = info.get("orders") or []
        lines += [f"{_exit_kinds(hits)}触发 **{len(hits)}** 只，生成 **{len(orders)}** 笔卖单"
                  "（明细见「订单意见」与「计划 vs 实际委托」）。", ""]
        for item in info.get("skipped") or []:
            lines.append(f"- {_cell(item.get('code'))} 未下单：{_cell(item.get('reason'))}")
        if info.get("skipped"):
            lines.append("")
        lines += ["强制卖出**不重置调仓周期**。卖出的钱闲置到下一个调仓日，"
                  "除非现金占比超过阈值、触发提前调仓。", ""]
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
    # **「确认不了」和「确实被拒」要分开说，因为该做的事完全相反。**
    # 被拒 -> 可以补单；确认不了 -> 可能已经成交，补单就是重复下单。
    # 2026-08-26 实测：一笔卖单全部成交（合同 6222104175），回读却读到空表，
    # 被报成失败。当时日报只说「请人工核对后决定是否补单」，
    # 而正确的提示应该是「先去客户端看这笔在不在，别急着补」。
    unverified = [o for o in outcomes if not o.get("ok") and o.get("verified") is False]
    if unverified:
        lines += ["> 🔴 **有订单无法确认状态**（回读不可信，不是券商拒单的证据）："
                  f"{'、'.join(str(o.get('code')) for o in unverified)}",
                  "> **这些单可能已经成交。** 请先到同花顺「今日委托」核对，"
                  "**不要直接补单** —— 补单会变成重复下单。", ""]
    if not broker.get("ok"):
        lines += [f"> ⚠️ **下单未全部成功**：{_cell(str(broker.get('message') or ''))}"]
        if not unverified:
            lines.append("> 顾问清单仍然有效，请人工核对后决定是否补单。")
        lines.append("")
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


def _gate_word(kept, shadow: bool) -> str:
    """影子模式下不能写"拦截" —— 那只票没被拦，照样可能被买进了。"""
    if shadow:
        return "会通过" if kept else "会拦截（未执行）"
    return "通过" if kept else "拦截"


def _read_moment(read_ts: str) -> str:
    """ISO 时刻 -> `2026-09-21 09:48`。解析不了就原样回显，别为了好看丢信息。"""
    from datetime import datetime as _dt
    try:
        return _dt.fromisoformat(read_ts).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return read_ts


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
    read_ts = str(portfolio.get("read_ts") or "").strip()
    line = f"**持仓来源**：{label}"
    if read_ts:
        # 券商直读是**某一刻的盘中快照**，不是当天的最终状态。只标日期的话，
        # 操作者过几分钟拿客户端一对就会以为我们算错了 —— 2026-09-21 真发生过，
        # 差额 657 元全部来自读完之后的价格变动（生益电子一只就占 535）。
        line += f"（**{_read_moment(read_ts)} 读取的盘中快照**，之后价格还会变）"
    elif asof:
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
             f"| 市场状态 | {_regime_brief(result)} |",
             f"| 调仓 | {_rebalance_brief(result)} |",
             f"| 当前持仓 | {len(positions)} 只 |",
             f"| 量化目标 | {len(targets)} 只 |",
             f"| 订单意见 | {len(allowed_orders)}/{len(orders)} 笔允许 |",
             f"| 风控闸门 | {passed_gates}/{len(gates)} 项通过 |", "",
             "## 一句话结论", "", f"> {_headline(result)}", "",
             "## 账户与持仓", ""]

    lines += _portfolio_provenance(result.get("portfolio") or {})

    unrealized = sum(float(position.get("pnl", 0) or 0) for position in positions)
    # 当日盈亏只有券商直读有。**一只都没有就整列不显示** —— 与其摆一列问号，
    # 不如别摆；但只要有一只有，就得显示，缺的那几只标 `—`。
    day_values = [position.get("day_pnl") for position in positions]
    has_day = any(value is not None for value in day_values)
    day_total = sum(float(value) for value in day_values if value is not None)
    if positions:
        summary = (f"账户当前持有 **{len(positions)}** 只股票，"
                   f"持仓以来浮动盈亏 **¥{unrealized:+,.2f}**")
        if has_day:
            # 这两个数经常一正一负（今天涨了但还没回本，或反过来），
            # 分开说清楚，别让人以为是同一个口径。
            summary += f"，**当日盈亏 ¥{day_total:+,.2f}**"
        lines += [summary + "。以下均为本次流程读取的账户事实。", ""]
    else:
        lines += ["当前没有可展示的持仓记录。", ""]

    if positions:
        header = "|代码|名称|股数|可卖|成本|现价|市值|持仓盈亏|收益率|"
        align = "|---|---|---:|---:|---:|---:|---:|---:|---:|"
        if has_day:
            header += "当日盈亏|"
            align += "---:|"
        lines += [header, align]
        for position in sorted(positions, key=lambda item: float(item.get("market_value", 0)),
                               reverse=True):
            qty = float(position.get("qty", 0) or 0)
            cost = float(position.get("cost_price", 0) or 0)
            pnl = float(position.get("pnl", 0) or 0)
            basis = qty * cost
            pnl_ratio = pnl / basis if basis else 0.0
            row = (
                f"|{_cell(position.get('code'))}|{_cell(position.get('name'))}|"
                f"{int(qty)}|{int(position.get('sellable_qty', 0) or 0)}|"
                f"{_number(cost, 3)}|{_number(position.get('last_price'), 3)}|"
                f"{_number(position.get('market_value'))}|{pnl:+,.2f}|{pnl_ratio:+.2%}|"
            )
            if has_day:
                day = position.get("day_pnl")
                row += ("—|" if day is None else f"{float(day):+,.2f}|")
            lines.append(row)
        lines.append("")

    verdicts = result.get("agent_verdicts") or []
    # 择时段放在**这个 if 之外**：risk-off 时恰好没有 targets，
    # 放进去就正好在它最该出现的时候不出现。
    lines += _rebalance_section(result)
    lines += _exits_section(result)
    lines += _regime_section(result)
    shadow = bool(result.get("review_shadow"))
    if targets or verdicts:
        intro = ("**复核处于影子模式：只记录评级，不影响选股。** 目标持仓直接取模型排名；"
                 "下面的「会通过 / 会拦截」是复核**本来**会怎么判，用来积累对照数据。"
                 if shadow else
                 "TradingAgents 复核闸只过滤量化候选，不生成主信号；调用异常按 fail-open 放行。")
        lines += ["## 量化选择与 TradingAgents 复核", "", intro, ""]
        if targets:
            verdict_by_code = {str(item.get("code")): item for item in verdicts}
            lines += ["|代码|目标权重|复核评级|结果|", "|---|---:|---|---:|"]
            for code, weight in sorted(targets.items(), key=lambda item: float(item[1]), reverse=True):
                verdict = verdict_by_code.get(str(code), {})
                gate_result = (
                    _gate_word(verdict.get("kept"), shadow)
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
                f"{_gate_word(verdict.get('kept'), shadow)}|"
            )
        lines.append("")
        for verdict in verdicts:
            code, rating = verdict.get("code"), verdict.get("rating") or "未复核"
            gate = _gate_word(verdict.get("kept"), shadow)
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
    # 但「模式会下单」不等于「这次下了单」。2026-09-20（周日）日流程跳过、0 笔订单，
    # 日报照样写着"PAPER 模式已直接向券商下单" —— 上一版只看 mode 不看结果。
    # 一份说自己下过单的空日报，比不说更坏。
    if mode == "ADVISORY":
        lines.append("- 顾问模式不接触券商；实际成交必须由次日持仓对账确认。")
    elif result.get("allowed_orders"):
        lines.append(f"- **{mode} 模式已直接向券商下单**；成交结果以「计划 vs 实际委托」"
                     "一节的合同编号为准，并由次日持仓复核。")
    else:
        lines.append(f"- {mode} 模式会直接向券商下单，但**本次没有订单**，未接触券商。")
    lines.append("")
    return "\n".join(lines)


def generate(result: dict, root: Path | None = None) -> Path:
    root = root or settings.report_dir / "daily"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{result.get('date', 'unknown')}.md"
    path.write_text(render(result), encoding="utf-8")
    return path
