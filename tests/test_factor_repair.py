"""后复权因子的伪造下降修复。全离线。

背景见 `cache.repair_factor` 的 docstring：实测沪深300 全量 304 只里有 4 只
的因子会下降，最严重的（平安银行 2020-12-31）会造出一根 −16.2% 的假阴线，
而原始价那天其实是 +0.73%。不修的话它直接进模型训练集。

**修复的正确性判据只有一条：修完之后，复权收益率必须等于原始收益率
（除权日除外）。** 下面的测试都是围绕这条展开的。
"""

from __future__ import annotations

import pandas as pd
import pytest

from qbg.data import cache
from qbg.data.cache import repair_factor


def _s(values) -> pd.Series:
    return pd.Series(values, dtype=float)


# ----------------------------------------------------------------------
# 不该动的情况
# ----------------------------------------------------------------------


def test_monotone_factor_is_untouched():
    """正常的因子只会向上跳，一个字节都不该改。"""
    f = _s([1.0, 1.0, 1.2, 1.2, 1.5])
    out, repairs = repair_factor(f)
    assert repairs == []
    assert out.tolist() == f.tolist()


def test_constant_factor_untouched():
    f = _s([7.669] * 5)
    out, repairs = repair_factor(f)
    assert repairs == []
    assert out.tolist() == f.tolist()


def test_short_series_untouched():
    assert repair_factor(_s([1.0]))[1] == []
    assert repair_factor(_s([]))[1] == []


def test_nonpositive_values_are_skipped_not_crash():
    """0 或负因子是另一类问题（verify 会报），修复函数不该在这里炸掉。"""
    out, _ = repair_factor(_s([1.0, 0.0, 1.0]))
    assert len(out) == 3


# ----------------------------------------------------------------------
# 故障模式一：一日凹陷（万科 2020-11-19 的形状）
# ----------------------------------------------------------------------


def test_one_day_dip_is_replaced_with_previous():
    """因子掉一天又原样弹回来——没有任何公司行为是这个形状。"""
    f = _s([115.094135, 115.094135, 112.152874, 115.094135, 115.094135])
    out, repairs = repair_factor(f)
    assert len(repairs) == 1
    assert repairs[0]["kind"] == "outlier"
    assert out.tolist() == pytest.approx([115.094135] * 5)


def test_one_day_dip_removes_the_fake_return():
    """核心判据：修完之后复权收益 == 原始收益。"""
    close = _s([29.95, 30.76, 30.91, 30.87, 31.04])
    f = _s([115.094135, 115.094135, 112.152874, 115.094135, 115.094135])

    fake = (close * f).pct_change()
    assert fake.iloc[2] == pytest.approx(-0.0208, abs=1e-3)   # 伪造的 -2%

    fixed, _ = repair_factor(f)
    real = (close * fixed).pct_change()
    assert real.tolist()[1:] == pytest.approx(close.pct_change().tolist()[1:])


def test_dip_followed_by_legitimate_ex_div_keeps_both_right():
    """凹陷次日恰好碰上真实除权：异常值被修掉，除权跳升要保留。"""
    f = _s([100.0, 98.0, 110.0, 110.0])
    out, repairs = repair_factor(f)
    assert repairs[0]["kind"] == "outlier"
    assert out.tolist() == pytest.approx([100.0, 100.0, 110.0, 110.0])


# ----------------------------------------------------------------------
# 故障模式二：持久平移（平安银行 2020-12-31 的形状）
# ----------------------------------------------------------------------


def test_persistent_shift_rescales_the_tail():
    """掉下去不回来 = 数据源换了复权基准，把两段不同锚点的序列拼在了一起。

    修法是把断点之后整段按比例抬回去，而不是简单地钳住——钳住会把好几年
    的分红累积压平成一个平台，然后再冒出一个假跳升。
    """
    f = _s([119.960317, 119.960317, 99.787353, 100.572054, 102.382020])
    out, repairs = repair_factor(f)

    assert len(repairs) == 1
    assert repairs[0]["kind"] == "rescale"
    # 断点当天被抬回断点之前的水平
    assert out.iloc[2] == pytest.approx(119.960317)
    # 尾巴按同一比例放大，段内相对关系不变
    scale = 119.960317 / 99.787353
    assert out.iloc[3] == pytest.approx(100.572054 * scale)
    assert out.iloc[4] == pytest.approx(102.382020 * scale)


def test_persistent_shift_preserves_within_segment_ratios():
    """段内的相对变化必须原样保留——因子是常数倍，收益率序列不受影响。"""
    f = _s([120.0, 100.0, 101.0, 103.0])
    out, _ = repair_factor(f)
    assert (out.iloc[2] / out.iloc[1]) == pytest.approx(101.0 / 100.0)
    assert (out.iloc[3] / out.iloc[2]) == pytest.approx(103.0 / 101.0)


def test_persistent_shift_removes_the_fake_return():
    """平安银行那一天：原始 +0.73%，复权后 -16.2%。修完必须变回 +0.73%。"""
    close = _s([19.17, 19.20, 19.34, 18.60, 18.17])
    f = _s([119.960317, 119.960317, 99.787353, 99.787353, 99.787353])

    fake = (close * f).pct_change()
    assert fake.iloc[2] == pytest.approx(-0.1621, abs=1e-3)

    fixed, _ = repair_factor(f)
    real = (close * fixed).pct_change()
    assert real.iloc[2] == pytest.approx(0.00729, abs=1e-4)   # == 原始收益
    assert real.tolist()[1:] == pytest.approx(close.pct_change().tolist()[1:])


# ----------------------------------------------------------------------
# 通用不变式
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        [115.09, 115.09, 112.15, 115.09, 115.09],              # 凹陷
        [119.96, 119.96, 99.79, 100.57, 102.38],               # 平移
        [100.0, 98.0, 96.0, 97.0, 99.0],                       # 连续下降
        [10.0, 9.0, 12.0, 11.0, 15.0],                         # 混合
    ],
)
def test_repaired_factor_is_always_monotone_nondecreasing(raw):
    """修完必须单调不减——这是"后复权因子只会累积"的直接推论。"""
    out, _ = repair_factor(_s(raw))
    assert (out.diff().dropna() >= -1e-9).all()


@pytest.mark.parametrize(
    "raw",
    [
        [115.09, 115.09, 112.15, 115.09, 115.09],
        [119.96, 119.96, 99.79, 100.57, 102.38],
        [10.0, 9.0, 12.0, 11.0, 15.0],
    ],
)
def test_repair_never_creates_a_downward_return(raw):
    """修复只能消除伪造的下跌，不能反过来制造新的。"""
    close = _s([10.0] * len(raw))          # 原始价不动
    out, _ = repair_factor(_s(raw))
    ret = (close * out).pct_change().dropna()
    assert (ret >= -1e-9).all()


def test_repair_is_idempotent():
    """修过的数据再修一遍不该有任何变化。"""
    f = _s([119.96, 119.96, 99.79, 100.57, 102.38])
    once, _ = repair_factor(f)
    twice, repairs = repair_factor(once)
    assert repairs == []
    assert twice.tolist() == pytest.approx(once.tolist())


def test_repair_preserves_index():
    idx = pd.date_range("2026-08-03", periods=4)
    f = pd.Series([120.0, 100.0, 101.0, 103.0], index=idx)
    out, _ = repair_factor(f)
    assert out.index.equals(idx)


# ----------------------------------------------------------------------
# 与 cache.read / verify 的集成
# ----------------------------------------------------------------------


def _write_bad(tmp_path, code="000001.SZ"):
    from tests.test_sources_chain import make_bars

    df = make_bars(["2026-08-03", "2026-08-04", "2026-08-05"])
    df["factor"] = [119.960317, 99.787353, 99.787353]
    df["source"] = "baostock"
    cache.write(code, df, root=tmp_path)
    return code


def test_read_repairs_by_default(tmp_path):
    """默认修好——没有任何下游想要一个错的因子。"""
    code = _write_bad(tmp_path)
    f = cache.read(code, root=tmp_path)["factor"]
    assert (f.diff().dropna() >= -1e-9).all()


def test_read_with_repair_false_returns_source_as_is(tmp_path):
    """原样可取，供对账用。"""
    code = _write_bad(tmp_path)
    f = cache.read(code, root=tmp_path, repair=False)["factor"]
    assert f.iloc[1] == pytest.approx(99.787353)


def test_stored_file_is_never_mutated_by_repair(tmp_path):
    """落盘的永远是源的原样，修复只发生在读出来之后。

    这样源的问题始终可见、可审计，不会被我们的修复悄悄掩盖掉。
    """
    code = _write_bad(tmp_path)
    cache.read(code, root=tmp_path)                      # 触发修复
    raw = pd.read_parquet(cache.path_for(code, tmp_path))
    assert raw["factor"].iloc[1] == pytest.approx(99.787353)


def test_verify_still_flags_the_source_problem(tmp_path):
    """数据质量告警不能被修复掩盖——ingest 必须用 repair=False 去验。"""
    code = _write_bad(tmp_path)
    problems = cache.verify(cache.read(code, root=tmp_path, repair=False))
    assert any("factor 递减" in p for p in problems)
    # 反面：喂修好的数据进去就检不出来了（这正是 ingest 要注意的点）
    assert cache.verify(cache.read(code, root=tmp_path)) == []


def test_hfq_uses_repaired_factor(tmp_path):
    """后复权价格视图必须用修好的因子，否则假阴线照样进模型。"""
    code = _write_bad(tmp_path)
    df = cache.read(code, root=tmp_path)
    ret = cache.hfq(df)["close"].pct_change().dropna()
    assert (ret.abs() < 1e-9).all()      # 原始价不动 → 复权价也不该动
