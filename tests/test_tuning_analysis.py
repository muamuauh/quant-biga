from __future__ import annotations

from pathlib import Path

import pytest

from qbg.agent.safety import assert_param_allowed, assert_write_allowed
from qbg.analysis.hypotheses import open_hypothesis
from qbg.tuning.gates import evaluate
from qbg.tuning.overlay import apply_change, read, rollback
from qbg.tuning.whitelist import load

BASE = {"annual_return": .20, "sharpe": 1.0, "max_drawdown": -.18,
        "avg_turnover": .2, "rank_ic": .03}


def good_report():
    candidate = dict(BASE, annual_return=.22, sharpe=1.05, max_drawdown=-.17,
                     avg_turnover=.19, rank_ic=.035)
    return evaluate(BASE, candidate, subperiod_excess=[.01, -.01, .02],
                    neighbor_sharpes=[.95, 1.0], high_cost={"annual_return": .05, "sharpe": .3})


def test_eight_gates_pass_good_candidate():
    report = good_report()
    assert len(report.checks) == 8 and report.passed


def test_bad_parameter_is_blocked_and_never_applied(tmp_path):
    bad = dict(BASE, annual_return=-.5, sharpe=-1, max_drawdown=-.7,
               avg_turnover=1, rank_ic=-.1)
    report = evaluate(BASE, bad, subperiod_excess=[-.1] * 3,
                      neighbor_sharpes=[-1, -1], high_cost={"annual_return": -.5, "sharpe": -1})
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("settings: {}\nrisk_limits: {}\n", encoding="utf-8")
    before = overlay.read_text(encoding="utf-8")
    with pytest.raises(PermissionError, match="未通过"):
        apply_change("qbg_top_k", 4, gate_passed=report.passed, proposal_id="P1",
                     autoapply=True, path=overlay, history=tmp_path / "history")
    assert overlay.read_text(encoding="utf-8") == before


def test_apply_and_one_click_rollback(tmp_path):
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("settings:\n  qbg_top_k: 3\nrisk_limits: {}\n", encoding="utf-8")
    _, snapshot = apply_change("qbg_top_k", 4, gate_passed=True, proposal_id="P2",
                               autoapply=True, path=overlay, history=tmp_path / "history")
    assert read(overlay)["settings"]["qbg_top_k"] == 4
    rollback(snapshot, overlay)
    assert read(overlay)["settings"]["qbg_top_k"] == 3


def test_frozen_and_out_of_range_rejected(tmp_path):
    specs, frozen, _ = load()
    assert {"qbg_mode", "i_confirm_real", "allow_live_mode"} <= frozen
    with pytest.raises(ValueError):
        specs["qbg_top_k"].validate(99)
    with pytest.raises(PermissionError):
        apply_change("qbg_mode", "LIVE", gate_passed=True, proposal_id="P",
                     autoapply=True, path=tmp_path / "o", history=tmp_path / "h")


def test_hypothesis_requires_discriminator(tmp_path):
    with pytest.raises(ValueError, match="discriminator"):
        open_hypothesis("模型", "IC 下降源于漂移", "", opened_date="2026-08-10", path=tmp_path / "db")
    item = open_hypothesis("模型", "IC 下降源于漂移", "未来20日IC恢复则证伪",
                           opened_date="2026-08-10", path=tmp_path / "db")
    assert item.discriminator and item.status == "open"


def test_agent_safety_denies_secrets_portfolio_and_locks(tmp_path):
    with pytest.raises(PermissionError):
        assert_write_allowed(Path(__file__).parents[1] / ".env")
    with pytest.raises(PermissionError):
        assert_write_allowed(Path(__file__).parents[1] / "data" / "portfolio" / "x.csv")
    with pytest.raises(PermissionError):
        assert_param_allowed("allow_live_mode")
