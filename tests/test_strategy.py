from __future__ import annotations

import pandas as pd
import pytest

from qbg.data.industry import neutralize
from qbg.model.train import cross_sectional_rank_ic, ensemble_seeds
from qbg.strategy.predict import latest_date_scores, neutralize_frame, predictions_to_frame
from qbg.strategy.regime import is_risk_on, risk_on_series
from qbg.strategy.topk_weights import (
    affordable_scores,
    renormalize_weights,
    select_with_hysteresis,
    topk_equal_weight,
)


def test_ensemble_seed_count_is_deterministic():
    assert ensemble_seeds(42, 3) == [42, 43, 44]
    assert ensemble_seeds(7, 0) == [7]


def test_rank_ic_is_cross_sectional_daily_average():
    idx = pd.MultiIndex.from_product(
        [pd.to_datetime(["2026-08-03", "2026-08-04"]), ["a", "b", "c"]],
        names=["datetime", "instrument"],
    )
    pred = pd.Series([1, 2, 3, 1, 2, 3], index=idx, dtype=float)
    label = pd.Series([2, 4, 6, 6, 4, 2], index=idx, dtype=float)
    assert cross_sectional_rank_ic(pred, label) == pytest.approx(0.0)


def test_affordability_uses_one_lot_not_one_share():
    scores = pd.Series([3.0, 2.0, 1.0], index=["600519.SH", "000858.SZ", "300750.SZ"])
    prices = {"600519.SH": 301.0, "000858.SZ": 299.0, "300750.SZ": 0.0}
    out = affordable_scores(scores, prices, equity=100_000, k=3, total_weight=0.9)
    assert out.index.tolist() == ["000858.SZ"]


def test_hysteresis_keeps_competitive_holding():
    ranked = ["a", "b", "c", "d"]
    assert select_with_hysteresis(ranked, {"c"}, k=2, keep_rank=3) == ["c", "a"]
    assert select_with_hysteresis(ranked, {"d"}, k=2, keep_rank=3) == ["a", "b"]


def test_topk_normalizes_codes_and_weights():
    scores = pd.Series([2.0, 1.0], index=["SH600519", "000858.SZ"])
    assert topk_equal_weight(scores, k=2, total_weight=0.9) == {
        "600519.SH": 0.45, "000858.SZ": 0.45,
    }


def test_renormalize_respects_cap_and_leaves_excess_cash():
    out = renormalize_weights({"a": 0.2, "b": 0.1}, 0.95, cap=0.34)
    assert out == pytest.approx({"a": 0.34, "b": 0.3166666667})
    assert sum(out.values()) < 0.95


def test_industry_neutralization_is_per_group():
    scores = pd.Series([3.0, 1.0, 10.0, 6.0], index=["a", "b", "c", "d"])
    mapping = {"a": "甲", "b": "甲", "c": "乙", "d": "乙"}
    out = neutralize(scores, mapping=mapping)
    assert out.to_dict() == pytest.approx({"a": 1, "b": -1, "c": 2, "d": -2})


def test_prediction_frame_and_latest_neutralization():
    idx = pd.MultiIndex.from_product(
        [pd.to_datetime(["2026-08-03", "2026-08-04"]), ["600519.SH", "000858.SZ"]],
        names=["datetime", "instrument"],
    )
    pred = pd.Series([1.0, 3.0, 4.0, 2.0], index=idx)
    frame = predictions_to_frame(pred)
    neutral = neutralize_frame(frame, {"600519.SH": "同组", "000858.SZ": "同组"})
    assert neutral.loc[pd.Timestamp("2026-08-04"), "600519.SH"] == pytest.approx(1.0)
    latest = latest_date_scores(pred, neutralize=True,
                                mapping={"600519.SH": "同组", "000858.SZ": "同组"})
    assert latest.index[0] == "600519.SH"


def test_regime_defaults_on_for_short_history_and_turns_off_below_sma():
    level = pd.Series([10.0, 11.0, 9.0], index=pd.date_range("2026-08-01", periods=3))
    signal = risk_on_series(level, 2)
    assert signal.tolist() == [True, True, False]
    assert not is_risk_on(level, 2)
    assert is_risk_on(level, 20)
