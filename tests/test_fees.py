"""A股费用模型。全离线。

重点是**不对称性**：印花税只在卖出时收。用对称成本回测会系统性低估
卖出成本 5bp，在一年 25 次调仓的节奏下累积约 1.25 个百分点——足以把
一个真实是负的策略算成正的。
"""

from __future__ import annotations

import pytest

from qbg.execution import fees
from qbg.execution.fees import FeeProfile

PROFILE = FeeProfile()   # 万2.5 / 最低5元 / 印花5bp / 过户0.1bp


def test_buy_has_no_stamp_tax():
    """A股成本不对称的全部来源。"""
    b = fees.estimate(30_000.0, "BUY", PROFILE)
    assert b.stamp_tax == 0.0
    assert b.commission == pytest.approx(7.5)
    assert b.transfer_fee == pytest.approx(0.3)


def test_sell_pays_stamp_tax():
    s = fees.estimate(30_000.0, "SELL", PROFILE)
    assert s.stamp_tax == pytest.approx(15.0)    # 30000 × 0.05%
    assert s.commission == pytest.approx(7.5)


def test_sell_costs_more_than_buy_by_exactly_the_stamp_tax():
    n = 30_000.0
    b = fees.estimate(n, "BUY", PROFILE)
    s = fees.estimate(n, "SELL", PROFILE)
    assert s.total - b.total == pytest.approx(n * PROFILE.stamp_tax_rate)


def test_commission_minimum_kicks_in_on_small_notional():
    """top_k 调大让单笔变小，最低佣金会让实际费率翻倍——这是隐藏代价。"""
    small = fees.estimate(10_000.0, "BUY", PROFILE)
    assert small.commission == pytest.approx(5.0)     # 而不是 2.5
    effective_rate = small.commission / 10_000.0
    assert effective_rate == pytest.approx(0.0005)    # 万5，是名义万2.5 的两倍


def test_commission_minimum_not_triggered_at_typical_size():
    """10万账户 k=3，单笔约3万 → 佣金 7.5 元 > 5 元，最低值不生效。"""
    assert fees.estimate(30_000.0, "BUY", PROFILE).commission == pytest.approx(7.5)


def test_min_notional_for_rate():
    """低于这个成交额，向量化回测的费率近似就会低估成本。"""
    assert fees.min_notional_for_rate(PROFILE) == pytest.approx(20_000.0)


def test_cost_rate_is_asymmetric():
    buy = fees.cost_rate("BUY", PROFILE)
    sell = fees.cost_rate("SELL", PROFILE)
    assert buy == pytest.approx(0.00026)             # 万2.5 + 0.1bp
    assert sell == pytest.approx(0.00076)            # 再加 5bp 印花税
    assert sell > buy


def test_round_trip_rate_is_about_10bp():
    """往返约 10bp —— 这是 QBG_REBALANCE_EVERY_DAYS 默认 10 而不是 1 的全部理由。"""
    rt = fees.round_trip_rate(PROFILE)
    assert rt == pytest.approx(0.00102)
    assert 0.0009 < rt < 0.0012


def test_zero_and_negative_notional_returns_zero_not_error():
    """整手取整可能算出 0 股，上游不该因此崩掉。"""
    for n in (0, -100, None):
        assert fees.estimate(n, "BUY", PROFILE).total == 0.0


def test_invalid_side_rejected():
    with pytest.raises(ValueError, match="BUY 或 SELL"):
        fees.estimate(1000.0, "HOLD", PROFILE)


def test_breakdown_as_dict_rounds_to_cent():
    d = fees.estimate(30_000.0, "SELL", PROFILE).as_dict()
    assert d == {"commission": 7.5, "stamp_tax": 15.0,
                 "transfer_fee": 0.3, "total": 22.8}


def test_profile_loads_from_yaml(tmp_path):
    p = tmp_path / "fee.yaml"
    p.write_text("commission_rate: 0.0001\ncommission_min: 1.0\n", encoding="utf-8")
    prof = FeeProfile.load(p)
    assert prof.commission_rate == 0.0001
    assert prof.commission_min == 1.0
    # 未指定的项保留默认
    assert prof.stamp_tax_rate == 0.0005


def test_profile_load_missing_file_uses_defaults(tmp_path):
    prof = FeeProfile.load(tmp_path / "nope.yaml")
    assert prof.commission_rate == FeeProfile().commission_rate


def test_profile_ignores_unknown_keys(tmp_path):
    """配置文件里的注释性字段不该让加载崩掉。"""
    p = tmp_path / "fee.yaml"
    p.write_text("commission_rate: 0.0003\nsome_note: 0.5\n", encoding="utf-8")
    assert FeeProfile.load(p).commission_rate == 0.0003


def test_shipped_config_matches_documented_rates():
    """configs/fee_profile.yaml 里的值应当和文档一致，改一处忘另一处会很难查。"""
    prof = FeeProfile.load()
    assert prof.stamp_tax_rate == 0.0005      # 2023-08 减半后
    assert prof.transfer_fee_rate == 0.00001  # 2022-04 沪深统一后
    assert prof.commission_min == 5.0
