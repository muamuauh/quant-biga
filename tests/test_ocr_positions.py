from __future__ import annotations

import copy

import pandas as pd
import pytest

from qbg.portfolio.ocr_source import PortfolioValidationError, require_valid, save_snapshot
from qbg.portfolio.reconcile import reconcile_orders

NAMES = {"600519.SH": "贵州茅台", "000858.SZ": "五粮液"}
HISTORY = {"600519.SH": (1480.0, False), "000858.SZ": (150.0, False)}
PAYLOAD = {
    "asof": "2026-08-10", "总资产": 168000.0, "可用资金": 20000.0,
    "positions": [{"名称": "贵州茅台", "代码": None, "股数": 100, "可用股数": 100,
                   "成本价": 1450.0, "现价": 1480.0, "市值": 148000.0, "盈亏": 3000.0}],
}


def test_valid_payload_resolves_name_and_saves_both_files(tmp_path):
    snapshot, warnings = require_valid(PAYLOAD, name_map=NAMES, price_history=HISTORY)
    assert snapshot.positions[0].code == "600519.SH" and warnings == []
    current, history = save_snapshot(snapshot, tmp_path)
    assert current.exists() and history.exists()


def test_adversarial_changed_digit_is_rejected():
    bad = copy.deepcopy(PAYLOAD)
    bad["positions"][0]["市值"] = 138000.0
    with pytest.raises(PortfolioValidationError, match="市值"):
        require_valid(bad, name_map=NAMES, price_history=HISTORY)


def test_adversarial_deleted_row_is_caught_by_asset_reconciliation():
    bad = copy.deepcopy(PAYLOAD)
    bad["positions"] = []
    with pytest.raises(PortfolioValidationError, match="总资产"):
        require_valid(bad, name_map=NAMES, price_history=HISTORY)


def test_adversarial_changed_name_is_rejected():
    bad = copy.deepcopy(PAYLOAD)
    bad["positions"][0]["名称"] = "不存在公司"
    with pytest.raises(PortfolioValidationError):
        require_valid(bad, name_map=NAMES, price_history=HISTORY)


@pytest.mark.parametrize("field,value", [("成本价", 0), ("现价", 2000), ("可用股数", 200)])
def test_cost_limit_and_sellable_checks(field, value):
    bad = copy.deepcopy(PAYLOAD)
    bad["positions"][0][field] = value
    with pytest.raises(PortfolioValidationError):
        require_valid(bad, name_map=NAMES, price_history=HISTORY)


def test_odd_lot_is_warning_not_fatal():
    data = copy.deepcopy(PAYLOAD)
    row = data["positions"][0]
    row.update(股数=137, 可用股数=137, 市值=202760)
    data["总资产"] = 222760
    snapshot, warnings = require_valid(data, name_map=NAMES, price_history=HISTORY)
    assert snapshot.positions[0].qty == 137
    assert len(warnings) == 1 and not warnings[0].fatal


def test_conflicting_duplicate_rows_are_rejected():
    data = copy.deepcopy(PAYLOAD)
    duplicate = copy.deepcopy(data["positions"][0])
    duplicate["股数"] = 200
    data["positions"].append(duplicate)
    with pytest.raises(PortfolioValidationError, match="重复"):
        require_valid(data, name_map=NAMES, price_history=HISTORY)


def test_reconcile_filled_partial_and_missing():
    orders = pd.DataFrame([
        {"code": "a", "side": "BUY", "quantity": 100},
        {"code": "b", "side": "SELL", "quantity": 200},
        {"code": "c", "side": "BUY", "quantity": 100},
    ])
    before = pd.DataFrame({"code": ["a", "b"], "qty": [0, 300]})
    after = pd.DataFrame({"code": ["a", "b"], "qty": [100, 200]})
    result = reconcile_orders(orders, before, after)
    assert result["status"].tolist() == ["已成交", "部分成交", "未成交"]
