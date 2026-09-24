"""逐票复核的影子模式 + 监控日不复核。全离线。

2026-09-24 实盘归因（20 个交易日、60 个复核样本）：复核**拦掉**的票比**留下**的
涨得多 —— 1 日超额 +0.66% vs −0.75%，5 日 +4.57% vs +1.61%。同期模型前 3 名
本身跑赢市场（5 日超额 +1.79%，Rank IC +0.029 不比回测差）。亏损出在复核这一层。

两件事：

- **影子模式**：照常复核、照常记录，但选股直接用模型排名。继续积累"留下 vs 拦掉"
  的对照，停掉可能的损害。
- **监控日不复核**：调仓改成每 10 日后，9/10 的盘前复核结论没人读。
  09-22/23/24 三个监控日各花约 $1.6，一分没用上。
"""

from __future__ import annotations

import pandas as pd
import pytest

from qbg.config import settings
from qbg.orchestrator import daily_cycle

# 模型排名：A 最好 … H 第 8。候选取前 5（A~E）。
FILTERED = pd.Series([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2],
                     index=list("ABCDEFGH"), name="score")
# 复核只留下 D、E —— 模型的第 1~3 名全被拦。形状照 2026-09-18 那天：
# 实际买进的是模型第 2/4/5 名，丢掉了第 1 和第 3。
CACHED = ({"D": 0.2, "E": 0.2},
          [{"code": c, "rating": r, "kept": c in "DE"} for c, r in
           zip("ABCDE", ["Underweight", "Underweight", "Sell", "Overweight", "Hold"],
               strict=True)],
          {"total_tokens": 1})


@pytest.fixture
def review_env(monkeypatch):
    """装好复核的依赖。`calls` 记下现场复核有没有被叫起来。"""
    calls = []
    monkeypatch.setattr(settings, "qbg_agents_candidates", 5)

    def fake_review(weights, **_):
        calls.append(list(weights))
        raise AssertionError("现场复核被叫起来了 —— 那要 58 分钟")

    import qbg.agents.review as review_mod
    monkeypatch.setattr(review_mod, "review_candidates", fake_review)
    return calls


def _cache(monkeypatch, value):
    import qbg.agents.verdict_cache as vc
    monkeypatch.setattr(vc, "load_verdicts", lambda today, candidates: value)


# ----------------------------------------------------------------------
# 影子模式
# ----------------------------------------------------------------------

def test_shadow_selects_from_the_full_ranking(monkeypatch, review_env):
    """**核心。** 复核把第 1~3 名全拦了，影子模式下选股照样看得见它们。"""
    monkeypatch.setattr(settings, "qbg_agents_shadow", 1)
    _cache(monkeypatch, CACHED)

    reviewed, verdicts, _, source = daily_cycle._apply_review(FILTERED, "2026-09-18")

    assert list(reviewed.index) == list("ABCDEFGH"), "影子模式不该裁剪选股范围"
    assert source == "shadow_cache"
    assert len(verdicts) == 5, "结论要留着 —— 那是'留下 vs 拦掉'对照的数据"


def test_shadow_keeps_hysteresis_alive(monkeypatch, review_env):
    """复核开着时只在 5 个候选里挑，排第 8 的持仓（H）根本不在集合里 ——
    `QBG_KEEP_RANK=15` 实际上失效，它会被直接卖掉。回测是在全排名上跑迟滞的。"""
    _cache(monkeypatch, CACHED)

    monkeypatch.setattr(settings, "qbg_agents_shadow", 0)
    gated, *_ = daily_cycle._apply_review(FILTERED, "2026-09-18")
    assert "H" not in gated.index, "前提：非影子模式下第 8 名确实被裁掉了"

    monkeypatch.setattr(settings, "qbg_agents_shadow", 1)
    shadow, *_ = daily_cycle._apply_review(FILTERED, "2026-09-18")
    assert "H" in shadow.index


def test_shadow_never_runs_the_live_review(monkeypatch, review_env):
    """缓存没有也**不现场复核**：58 分钟，而结论反正不影响下单。"""
    monkeypatch.setattr(settings, "qbg_agents_shadow", 1)
    _cache(monkeypatch, None)

    reviewed, verdicts, _, source = daily_cycle._apply_review(FILTERED, "2026-09-18")

    assert review_env == [], "影子模式叫起了现场复核"
    assert source == "shadow_missing"
    assert verdicts == []
    assert list(reviewed.index) == list("ABCDEFGH")


# ----------------------------------------------------------------------
# 非影子模式：原行为不许变
# ----------------------------------------------------------------------

def test_gated_mode_uses_the_cache_and_filters(monkeypatch, review_env):
    monkeypatch.setattr(settings, "qbg_agents_shadow", 0)
    _cache(monkeypatch, CACHED)

    reviewed, _, _, source = daily_cycle._apply_review(FILTERED, "2026-09-18")

    assert list(reviewed.index) == ["D", "E"]
    assert source == "premarket_cache"


def test_gated_mode_falls_back_to_live_review(monkeypatch, review_env):
    """缓存不是"关掉复核"的开关 —— `QBG_AGENTS_ENABLED` 才是。"""
    monkeypatch.setattr(settings, "qbg_agents_shadow", 0)
    _cache(monkeypatch, None)

    with pytest.raises(AssertionError, match="现场复核"):
        daily_cycle._apply_review(FILTERED, "2026-09-18")
    assert review_env == [list("ABCDE")]


# ----------------------------------------------------------------------
# 日报和健康检查不许在影子模式下说假话
# ----------------------------------------------------------------------

def test_report_does_not_say_blocked_in_shadow_mode():
    """影子模式下那只票没被拦，照样可能被买进了。写"拦截"是假话。"""
    from qbg.report.daily_report import render

    result = {"date": "2026-09-25", "mode": "PAPER", "run_kind": "rebalance",
              "orders": [], "allowed_orders": [], "targets": {"A": 0.3},
              "scores": [], "gates": [], "positions": [], "account": {},
              "agent_verdicts": CACHED[1], "review_shadow": True}
    text = render(result)
    assert "影子模式" in text
    assert "会拦截（未执行）" in text
    assert "|拦截|" not in text


def test_health_does_not_alarm_on_blocked_all_in_shadow_mode(monkeypatch, tmp_path):
    """全拦也照样按模型排名买。"当天不会有任何买入"在影子模式下是假告警。"""
    from qbg.analysis.health import diagnose
    from qbg.store.etl import _ingest_cycle, connect

    path = tmp_path / "runs.db"
    db = connect(path)
    _ingest_cycle(db, {"date": "2026-09-16", "mode": "PAPER", "account": {},
                       "agent_verdicts": [{"code": "A", "rating": "Sell", "kept": False}]},
                  "2026-09-16", "PAPER")
    db.commit()
    db.close()

    monkeypatch.setattr(settings, "qbg_agents_shadow", 0)
    assert "REVIEW_BLOCKED_ALL" in {f.code for f in diagnose(path, "PAPER")}, "前提"
    monkeypatch.setattr(settings, "qbg_agents_shadow", 1)
    assert "REVIEW_BLOCKED_ALL" not in {f.code for f in diagnose(path, "PAPER")}


# ----------------------------------------------------------------------
# 监控日不复核
# ----------------------------------------------------------------------

def _premarket():
    from importlib import util
    spec = util.spec_from_file_location("premarket_mod", "scripts/26_premarket.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("cash, scheduled, expected", [
    (None, False, True),    # 现金读不到：可能是现金触发日，照跑
    (0.13, True, True),     # 到期的调仓日
    (0.13, False, False),   # 普通监控日 —— 这是省钱的那一格
    (0.60, False, True),    # 现金过半：日流程会提前调仓，得复核
])
def test_review_needed(monkeypatch, cash, scheduled, expected):
    module = _premarket()
    monkeypatch.setattr(settings, "qbg_rebalance_every_days", 10)
    monkeypatch.setattr(settings, "qbg_rebalance_cash_trigger", 0.5)
    import qbg.orchestrator.run_marker as rm
    monkeypatch.setattr(rm, "load_rebalance_date", lambda *a, **k: "2026-09-18")
    monkeypatch.setattr(rm.calendar, "trading_days_between",
                        lambda a, b, *_: 10 if scheduled else 3)
    assert module._review_needed("2026-09-24", cash) is expected


def test_monitoring_day_still_retrains_but_skips_review(monkeypatch, capsys):
    """**闸必须排在重训之后、复核之前。**

    重训不能省：监控日的止损单也要过预测新鲜度那道硬闸，重训跳了止损就会被拦下。
    省的只是复核那 $1.6。
    """
    module = _premarket()
    did = []

    class _Done:
        returncode = 0

    class _Snap:
        total_equity, available_cash = 188_812.14, 26_104.14

    class _Loaded:
        snapshot, source, degraded = _Snap(), "easytrader", False

    monkeypatch.setattr(module.calendar, "is_trading_day", lambda *a, **k: True)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: did.append("ingest") or _Done())
    monkeypatch.setattr(module, "train", lambda **k: did.append("train"))
    monkeypatch.setattr(module, "load_production_predictions",
                        lambda: (pd.DataFrame(), "cn_lgb_live"))
    monkeypatch.setattr(module, "prediction_asof", lambda _: "2026-09-23")
    monkeypatch.setattr(module, "load_portfolio", lambda: _Loaded())
    monkeypatch.setattr(module, "latest_date_scores", lambda *a, **k: FILTERED)
    monkeypatch.setattr(module.bar_cache, "read", lambda code: pd.DataFrame())
    monkeypatch.setattr(module, "load_limits", lambda: {"max_position_pct": 0.35})
    monkeypatch.setattr(module, "affordable_scores", lambda s, *a, **k: s)
    monkeypatch.setattr(module, "market_risk_on", lambda *a, **k: True)
    monkeypatch.setattr(settings, "qbg_agents_enabled", 1)
    monkeypatch.setattr(module, "_review_needed", lambda today, cash: False)

    import qbg.agents.review as review_mod

    def boom(*a, **k):
        raise AssertionError("监控日还在复核")
    monkeypatch.setattr(review_mod, "review_candidates", boom)

    assert module.main(["--date", "2026-09-24"]) == 0
    assert did == ["ingest", "train"], "拉数和重训不许省"
    assert "监控日" in capsys.readouterr().out
