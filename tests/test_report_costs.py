"""日报的「今日成本」段。

`plan.md` §8.5 把「成本吃掉 alpha」列为小账户的第一杀手 —— quant-trading
实测 3000 美元账户日频调仓被费用和滑点打到年化 −30%。成本不摆在日报上，
这件事就只能靠回测发现，而回测发现得太晚。
"""

from __future__ import annotations

from qbg.report.daily_report import _trading_costs, render

BASE = {"date": "2026-08-25", "mode": "PAPER", "submitted": True,
        "account": {"total_equity": 200000.0, "available_cash": 190000.0},
        "positions": [], "orders": [], "gates": []}


def _o(code="601398.SH", side="BUY", notional=30000.0, qty=3800, price=7.89):
    return {"code": code, "name": "x", "side": side, "quantity": qty,
            "price": price, "notional": notional, "reason": "test"}


# ---------------------------------------------------------------------------
# 成本计算
# ---------------------------------------------------------------------------
def test_stamp_tax_only_on_sell():
    """A股成本的**全部不对称性**来自这里：印花税只在卖出时收。"""
    buy = _trading_costs([_o(side="BUY", notional=100_000.0)])
    sell = _trading_costs([_o(side="SELL", notional=100_000.0)])
    assert buy["stamp_tax"] == 0.0
    assert sell["stamp_tax"] > 0.0
    # 同样金额，卖出比买入贵大约 5bp（印花税 0.05%）
    assert abs((sell["total"] - buy["total"]) / 100_000.0 * 1e4 - 5.0) < 0.1


def test_round_trip_is_about_10bp():
    """买入再卖出往返约 10bp —— plan.md §5.4 的那个数。"""
    both = _trading_costs([_o(side="BUY", notional=100_000.0),
                           _o(side="SELL", notional=100_000.0)])
    assert 9.0 < both["bp"] * 2 < 11.5


def test_min_commission_hit_is_counted():
    """小额单会触及最低佣金 5 元，实际费率翻倍 —— 这是小账户的隐藏代价。"""
    small = _trading_costs([_o(notional=10_000.0)])     # 万2.5 = 2.5 元 < 5 元
    assert small["min_commission_hits"] == 1
    assert small["commission"] == 5.0


def test_large_order_does_not_hit_min_commission():
    big = _trading_costs([_o(notional=30_000.0)])       # 万2.5 = 7.5 元 > 5 元
    assert big["min_commission_hits"] == 0


def test_buy_and_sell_notional_split():
    totals = _trading_costs([_o(side="BUY", notional=30_000.0),
                             _o(side="SELL", notional=50_000.0)])
    assert totals["buy_notional"] == 30_000.0
    assert totals["sell_notional"] == 50_000.0
    assert totals["notional"] == 80_000.0


def test_zero_and_invalid_orders_ignored():
    """整手取整可能算出 0 股，不能让它污染成本统计或除零。"""
    totals = _trading_costs([_o(notional=0.0), _o(side="", notional=1000.0)])
    assert totals["notional"] == 0.0 and totals["total"] == 0.0
    assert totals["bp"] == 0.0


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def test_section_lists_each_cost_item():
    text = render({**BASE, "allowed_orders": [_o(side="BUY", notional=30_000.0),
                                              _o(side="SELL", notional=50_000.0)]})
    assert "## 今日成本" in text
    for item in ("佣金", "印花税", "过户费", "交易成本合计"):
        assert item in text, item
    assert "bp" in text


def test_llm_cost_is_reported():
    text = render({**BASE, "allowed_orders": [_o()],
                   "agent_usage": {"calls": 60, "total_tokens": 285000,
                                   "cost_usd": 0.2137}})
    assert "LLM 逐票复核" in text
    assert "$0.2137" in text
    assert "285,000" in text


def test_review_tokens_reported_when_present():
    text = render({**BASE, "allowed_orders": [_o()],
                   "daily_review": {"ok": True, "usage": {"total_tokens": 3000}}})
    assert "LLM 自动复盘" in text


def test_no_section_when_nothing_to_report():
    """没订单也没 LLM 调用时不要留一张空表。"""
    assert "## 今日成本" not in render(BASE)


def test_llm_only_run_still_reports():
    """有复核但风控砍光了订单 —— LLM 的钱照样花了，得报出来。"""
    text = render({**BASE, "agent_usage": {"calls": 60, "total_tokens": 285000,
                                           "cost_usd": 0.2137}})
    assert "## 今日成本" in text
    assert "LLM 逐票复核" in text
    assert "交易成本合计" not in text


def test_breakeven_line_present():
    """把成本换算成「要涨多少才回本」比一个绝对数更能说明问题。"""
    text = render({**BASE, "allowed_orders": [_o(side="SELL", notional=100_000.0)]})
    assert "才能打平" in text


# ---------------------------------------------------------------------------
# 2026-08-25 实测那次日报的四个毛病，逐条钉住。
#
# 那天：持仓降级到虚构账户、3 笔单一笔都没进券商。而日报和邮件说的是
# 「已生成下单清单」「已提交3笔」，还列了 ¥17.50 佣金和 ¥66,743 成交额。
# 全是报喜，全是假的。
# ---------------------------------------------------------------------------
FAILED_RUN = {
    "date": "2026-08-25", "mode": "PAPER", "execution_mode": "PAPER", "hard_ok": True,
    "submitted": True,
    "targets": {"002384.SZ": 0.3167, "300274.SZ": 0.3167, "300394.SZ": 0.3167},
    "allowed_orders": [
        {"code": "002384.SZ", "side": "BUY", "quantity": 100, "notional": 19399.0},
        {"code": "300274.SZ", "side": "BUY", "quantity": 200, "notional": 22344.0},
        {"code": "300394.SZ", "side": "BUY", "quantity": 100, "notional": 25000.0}],
    "broker": {"ok": False, "submitted": 0, "message": "提交后当日委托里没有出现这一笔",
               "outcomes": [{"code": "002384.SZ", "side": "BUY", "quantity": 100,
                             "price": 193.99, "ok": False, "entrust_no": None,
                             "message": "提交后当日委托里没有出现这一笔"}]},
    "agent_usage": {"calls": 80, "total_tokens": 341651, "cost_usd": 0.2852},
}


def test_costs_are_zero_when_nothing_reached_the_broker():
    """一笔都没进券商 → 交易成本必须是 0，不能按计划的订单收费。"""
    text = render(FAILED_RUN)
    assert "已委托 0/3 笔" in text
    assert "不产生费用" in text
    assert "66,743" not in text, "成交额是凭空的：那 3 笔单根本没成交"
    assert "¥17.50" not in text


def test_costs_are_labelled_as_estimate_in_advisory_mode():
    advisory = {k: v for k, v in FAILED_RUN.items() if k not in ("broker", "execution_mode")}
    text = render(advisory)
    assert "预估，实际以成交为准" in text


def test_headline_leads_with_the_failure():
    """一句话结论必须先说失败，而不是说「已生成下单清单」。"""
    text = render(FAILED_RUN)
    headline = text.split("## 一句话结论", 1)[1].split("##", 1)[0]
    assert "下单全部未成功" in headline
    assert "0/3" in headline
    assert "已生成下单清单" not in headline


def test_headline_shouts_when_portfolio_is_fictional():
    run = {**FAILED_RUN, "portfolio": {"source": "default", "asof": "",
                                       "degraded": {"from": "easytrader", "reason": "对不上账"}}}
    headline = render(run).split("## 一句话结论", 1)[1].split("##", 1)[0]
    assert "持仓完全读不到" in headline
    assert "不可照做" in headline


def test_headline_reports_success_plainly():
    ok_run = {**FAILED_RUN,
              "broker": {"ok": True, "submitted": 3, "message": "",
                         "outcomes": [{"code": c, "side": "BUY", "ok": True,
                                       "entrust_no": "62211154"} for c in
                                      ("002384.SZ", "300274.SZ", "300394.SZ")]}}
    headline = render(ok_run).split("## 一句话结论", 1)[1].split("##", 1)[0]
    assert "券商已接单 3/3 笔" in headline


def test_broker_denominator_counts_planned_not_attempted():
    """下单遇错即停：outcomes 只有 1 笔，但计划是 3 笔。

    写成「0/1」会让人以为只计划了 1 笔，看不出还有 2 笔根本没试过。
    """
    text = render(FAILED_RUN)
    assert "0/3** 笔通过回读校验" in text
    assert "2 笔因中止未尝试" in text


def test_limits_section_admits_it_touched_the_broker():
    """PAPER 模式真的下了单，就不能再说「顾问模式不接触券商」。"""
    text = render(FAILED_RUN)
    assert "顾问模式不接触券商" not in text
    assert "PAPER 模式已直接向券商下单" in text
    advisory = {k: v for k, v in FAILED_RUN.items()
                if k not in ("broker", "execution_mode", "mode")}
    assert "顾问模式不接触券商" in render(advisory)


# ---------------------------------------------------------------------------
# 复核明细：LLM 的理由是 400–600 字的结构化文本，塞进表格会被砍掉三分之二
# 并且横向溢出，邮件里根本拉不动 —— 等于那几毛钱 token 白花了。
# ---------------------------------------------------------------------------
RATIONALE = (
    "**Rating**: Overweight\n\n"
    "**Executive Summary**: 建议逐步增加对阳光电源（300274.SZ）的持仓，"
    "初始仓位可设定为投资组合的5%-7%，并在股价回调至10日EMA附近时进一步加仓。\n\n"
    "**Investment Thesis**: 阳光电源在光伏发电和储能市场展现了显著的增长潜力，"
    "其2026年第二季度的营业收入同比增长133.06%，净利润增长383.55%。"
    "然而，公司高达66.34%的资产负债率确实是一个需要关注的风险。\n\n"
    "**Time Horizon**: 6-12个月")

REVIEWED = {"date": "2026-08-25", "hard_ok": True,
            "agent_verdicts": [{"code": "300274.SZ", "rating": "Overweight",
                                "rationale": RATIONALE, "kept": True, "error": None}]}


def test_full_rationale_survives_into_the_report():
    text = render(REVIEWED)
    assert "Investment Thesis" in text
    assert "Time Horizon" in text
    assert "6-12个月" in text, "理由被截断了 —— 最后一节没进日报"
    assert "…" not in text.split("#### 300274.SZ", 1)[1]


def test_rationale_is_not_crammed_into_a_table_cell():
    """理由必须在表格**外面**。表格行里出现长文本就是老毛病复发。"""
    text = render(REVIEWED)
    detail = text.split("### 复核明细", 1)[1]
    table_rows = [ln for ln in detail.splitlines() if ln.startswith("|")]
    assert table_rows, "概览表还在"
    assert all(len(row) < 60 for row in table_rows), f"表格行过长，理由又被塞回去了：{table_rows}"
    assert "#### 300274.SZ · Overweight · 通过" in detail


def test_duplicate_rating_line_is_dropped():
    """评级已经在小标题里了，正文再重复一遍 **Rating** 是噪音。"""
    detail = render(REVIEWED).split("#### 300274.SZ", 1)[1]
    assert "**Rating**" not in detail


def test_review_error_is_shown_instead_of_rationale():
    text = render({"date": "2026-08-25", "hard_ok": True,
                   "agent_verdicts": [{"code": "600519.SH", "rating": "未复核",
                                       "rationale": "", "kept": True,
                                       "error": "429 Too Many Requests"}]})
    assert "复核异常" in text and "429" in text


def test_subtitle_does_not_claim_success_either():
    """副标题和主题、一句话结论是同一个毛病，三处都要改到。"""
    from qbg.report.daily_report import _status
    assert _status(FAILED_RUN) == "⚠ 下单失败 0/3 笔"
    assert "已生成清单" not in render(FAILED_RUN).split("## 概览")[0]


# ---------------------------------------------------------------------------
# 「确认不了」和「确实被拒」必须分开显示 —— 该做的事完全相反。
# 被拒 -> 可以补单；确认不了 -> 可能已成交，补单就是重复下单。
#
# 2026-08-26 实测：一笔卖单全部成交（合同 6222104175），回读读到空表被判失败。
# 当时日报只说「请人工核对后决定是否补单」，而正确提示是「先看在不在，别急着补」。
# ---------------------------------------------------------------------------
UNVERIFIED_RUN = {
    "date": "2026-08-26", "hard_ok": True, "execution_mode": "PAPER", "submitted": True,
    "allowed_orders": [{"code": "002384.SZ", "side": "SELL", "quantity": 100,
                        "notional": 19046.0}],
    "broker": {"ok": False, "submitted": 0, "message": "⚠ 无法确认：读到 0 行真实记录",
               "outcomes": [{"code": "002384.SZ", "side": "SELL", "quantity": 100,
                             "price": 190.46, "ok": False, "verified": False,
                             "entrust_no": "", "message": "⚠ 无法确认"}]},
}


def test_report_separates_unverified_from_rejected():
    text = render(UNVERIFIED_RUN)
    assert "无法确认状态" in text
    assert "可能已经成交" in text
    assert "不要直接补单" in text
    assert "请人工核对后决定是否补单" not in text, "确认不了时不该建议补单"


def test_rejected_order_still_suggests_manual_topup():
    rejected = {**UNVERIFIED_RUN,
                "broker": {**UNVERIFIED_RUN["broker"],
                           "outcomes": [{**UNVERIFIED_RUN["broker"]["outcomes"][0],
                                         "verified": True}]}}
    text = render(rejected)
    assert "无法确认状态" not in text
    assert "请人工核对后决定是否补单" in text


def test_headline_flags_unverified_loudly():
    headline = render(UNVERIFIED_RUN).split("## 一句话结论", 1)[1].split("##", 1)[0]
    assert "无法确认" in headline
    assert "别补单" in headline
    assert "下单全部未成功" not in headline, "说成「失败」会诱导补单"


def test_rerender_from_log_restores_truncated_rationale(tmp_path):
    """日报是渲染产物，`logs/qbg.jsonl` 才是真相源 —— 渲染代码修好后，
    旧日报能从日志重建，什么都不会丢。

    2026-08-10 那份的复核理由被砍到 180 字加省略号（当时用表格渲染），
    而日志里存的是完整的 455~564 字。这条测试钉住"重渲染确实能恢复"。
    """
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    rerender = __import__("11_rerender_reports")

    # 用一个模板里不会出现的字符 —— 「细」会和「复核明细」撞上，
    # 全文计数就多出两个，得出一个和被测行为无关的失败。
    long_rationale = "**Executive Summary**: " + "囧" * 300 + "\n\n**Time Horizon**: 6-12个月"
    record = {"msg": "cycle.completed", "date": "2026-08-10", "hard_ok": True,
              "agent_verdicts": [{"code": "600549.SH", "rating": "Hold",
                                  "rationale": long_rationale, "kept": True,
                                  "error": None}]}
    log = tmp_path / "qbg.jsonl"
    log.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    runs = rerender.load_runs(log)
    assert set(runs) == {"2026-08-10"}
    text = render(runs["2026-08-10"])
    assert "…" not in text, "理由不该再被截断"
    assert "Time Horizon" in text, "最后一节必须还在"
    block = text.split("#### 600549.SH", 1)[1]
    assert block.count("囧") == 300, "理由必须一字不差地展开"


def test_rerender_keeps_the_last_run_of_a_day(tmp_path):
    """同一天可能跑多次（补跑/手工重跑）。取最后一次 —— 和当初落盘的一致，
    因为 generate 每次都覆盖同名文件。"""
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    rerender = __import__("11_rerender_reports")

    log = tmp_path / "qbg.jsonl"
    log.write_text("\n".join(
        json.dumps({"msg": "cycle.completed", "date": "2026-08-10", "run": n})
        for n in (1, 2, 3)) + "\n", encoding="utf-8")
    assert rerender.load_runs(log)["2026-08-10"]["run"] == 3


def test_rerender_survives_a_corrupt_log_line(tmp_path):
    """日志被写坏一行不该让整个重渲染失败 —— 它是追加写的运行日志，
    不是事务性存储。"""
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    rerender = __import__("11_rerender_reports")

    log = tmp_path / "qbg.jsonl"
    log.write_text(
        "{坏掉的一行\n"
        + json.dumps({"msg": "cycle.completed", "date": "2026-08-11"}) + "\n",
        encoding="utf-8")
    assert set(rerender.load_runs(log)) == {"2026-08-11"}
