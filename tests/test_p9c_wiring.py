"""P9c：daily_cycle 按模式选执行适配器 + 日报的「计划 vs 实际委托」对账。

在这之前 `daily_cycle` **写死 `AdvisoryAdapter()`** —— 和 P9a 之前写死
`ManualSource()` 是同一类问题：配置项存在但没有任何代码读它。
"""

from __future__ import annotations

import pytest

from qbg.execution.base import ExecutionResult, Order
from qbg.orchestrator import daily_cycle
from qbg.report.daily_report import render


def _order(code="601398.SH", side="BUY", qty=100, price=7.99):
    return Order(code=code, side=side, quantity=qty, price=price, reason="test")


def _outcome(ok=True, entrust="6219942124", message="已提交且回读校验通过"):
    return {"code": "601398.SH", "side": "BUY", "quantity": 100, "price": 7.99,
            "ok": ok, "entrust_no": entrust, "message": message, "dialogs": []}


# ---------------------------------------------------------------------------
# 适配器选择
# ---------------------------------------------------------------------------
def test_broker_submit_returns_outcomes(monkeypatch):
    class _Fake:
        mode = "PAPER"

        def submit(self, orders, asof, gates=None):
            return ExecutionResult(True, "PAPER", 1, (), "全部提交并回读校验通过",
                                   (_outcome(),))

    monkeypatch.setattr("qbg.execution.easytrader_adapter.EasytraderAdapter", _Fake)
    monkeypatch.setattr(daily_cycle.settings, "qbg_mode", "PAPER")
    out = daily_cycle._submit_to_broker([_order()], "2026-08-25", [])
    assert out["execution_mode"] == "PAPER"
    assert out["broker"]["ok"] and out["broker"]["submitted"] == 1
    assert out["broker"]["outcomes"][0]["entrust_no"] == "6219942124"


def test_broker_failure_does_not_raise(monkeypatch):
    """下单失败必须被记录，**不能掀翻日流程** —— 崩了就连日报和邮件都没有。"""
    class _Boom:
        def submit(self, *a, **k):
            raise RuntimeError("读不到客户端的资金账号")

    monkeypatch.setattr("qbg.execution.easytrader_adapter.EasytraderAdapter", _Boom)
    monkeypatch.setattr(daily_cycle.settings, "qbg_mode", "PAPER")
    out = daily_cycle._submit_to_broker([_order()], "2026-08-25", [])
    assert out["broker"]["ok"] is False
    assert "资金账号" in out["broker"]["message"]


def test_constructor_failure_is_also_caught(monkeypatch):
    """连 adapter 都构造不出来（比如三把锁没开）同样不能抛出去。"""
    def _boom():
        raise RuntimeError("LIVE 三把锁未全部人工确认")

    monkeypatch.setattr("qbg.execution.easytrader_adapter.EasytraderAdapter", _boom)
    monkeypatch.setattr(daily_cycle.settings, "qbg_mode", "LIVE")
    out = daily_cycle._submit_to_broker([_order()], "2026-08-25", [])
    assert out["broker"]["ok"] is False
    assert "三把锁" in out["broker"]["message"]


# ---------------------------------------------------------------------------
# 日报对账段
# ---------------------------------------------------------------------------
BASE = {"date": "2026-08-25", "mode": "PAPER", "submitted": True,
        "account": {"total_equity": 200000.0, "available_cash": 199217.0},
        "positions": [], "orders": [], "allowed_orders": [], "gates": []}


def test_report_shows_entrust_numbers():
    text = render({**BASE, "execution_mode": "PAPER",
                   "broker": {"ok": True, "submitted": 1, "message": "全部提交并回读校验通过",
                              "outcomes": [_outcome()]}})
    assert "## 计划 vs 实际委托" in text
    assert "6219942124" in text
    assert "1/1" in text


def test_report_makes_failure_loud():
    """下单失败时清单照样存在 —— 不写清楚，看日报的人会以为单子都下出去了。"""
    text = render({**BASE, "execution_mode": "PAPER",
                   "broker": {"ok": False, "submitted": 0,
                              "message": "0/1 笔成功后中止：当日委托里没有出现这一笔",
                              "outcomes": [_outcome(ok=False, entrust="",
                                                    message="没有出现这一笔")]}})
    assert "下单未全部成功" in text
    assert "❌" in text
    assert "顾问清单仍然有效" in text


def test_advisory_run_says_not_submitted_to_broker():
    """ADVISORY 下 submitted=True 只代表清单写好了，很容易被读成「单子下出去了」。"""
    row = _order().as_dict()
    text = render({**BASE, "mode": "ADVISORY", "execution_mode": "ADVISORY",
                   "orders": [row], "allowed_orders": [row]})
    assert "未向券商提交" in text
    assert "## 计划 vs 实际委托" not in text


def test_no_broker_section_without_broker_data():
    assert "## 计划 vs 实际委托" not in render(BASE)


@pytest.mark.parametrize("mode", ["PAPER", "LIVE"])
def test_execution_mode_is_reported(mode):
    """模式必须写进日报：PAPER 的成绩不能被读成实盘成绩。"""
    text = render({**BASE, "mode": mode, "execution_mode": mode,
                   "broker": {"ok": True, "submitted": 1, "message": "ok",
                              "outcomes": [_outcome()]}})
    assert f"执行模式 **{mode}**" in text


# ---------------------------------------------------------------------------
# 持仓读不到 → 拒绝下单
#
# 2026-08-25 全链路实测的真实故障链：挂单冻结资金 → 总资产恒等式失败 →
# easytrader 降级到 ocr → CSV 不存在 → 落到 default_equity=100,000 空仓假设。
# 系统于是照着一个**虚构账户**规划并真的把单发了出去（券商静默拒绝）。
#
# 顾问模式出一份基于假设的清单无所谓 —— 人会看。真下单不行：
# 不知道自己现在持有什么，就可能重复买入、或者卖出根本不存在的股票。
# ---------------------------------------------------------------------------
def test_default_portfolio_blocks_broker_submission():
    refusal = daily_cycle.broker_refusal(
        "default", {"from": "easytrader", "reason": "PortfolioValidationError: 总资产对不上"})
    assert refusal is not None, "持仓落到默认假设账户时绝不能放行下单"
    assert "拒绝下单" in refusal
    assert "PortfolioValidationError" in refusal, "拒绝理由必须带上根因，否则没法排查"


def test_refusal_survives_missing_degraded_info():
    """降级信息丢了也要拒绝 —— 不能因为不知道原因就当没事发生。"""
    assert daily_cycle.broker_refusal("default", None) is not None


@pytest.mark.parametrize("source", ["easytrader", "ocr", "manual"])
def test_real_portfolio_sources_are_allowed(source):
    assert daily_cycle.broker_refusal(source, None) is None


def test_refusal_is_visible_in_report():
    """拒绝下单必须在日报上显眼 —— 否则看日报的人以为清单都下出去了。"""
    text = render({"date": "2026-08-25", "hard_ok": True, "execution_mode": "PAPER",
                   "broker": {"ok": False, "submitted": 0, "outcomes": [],
                              "message": "持仓读取失败、已落到默认假设账户 —— 拒绝下单"}})
    assert "下单未全部成功" in text
    assert "拒绝下单" in text


def test_report_shouts_when_portfolio_fell_back_to_default():
    text = render({"date": "2026-08-25", "hard_ok": True,
                   "portfolio": {"source": "default", "asof": "",
                                 "degraded": {"from": "easytrader",
                                              "reason": "PortfolioValidationError: 总资产对不上"}}})
    assert "持仓完全读不到" in text
    assert "不要照着执行" in text
    assert "PortfolioValidationError" in text
