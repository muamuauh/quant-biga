"""代码名称表、名称归一化、行业分类。全离线。

名称归一化是 P5 (OCR 读图) 的关键路径：手机 APP 持仓页只显示中文名，
名字对不上就查不到代码，持仓就进不了系统。
"""

from __future__ import annotations

import pandas as pd
import pytest

from qbg.data import industry, meta
from qbg.data.meta import AmbiguousNameError, UnknownNameError

# ----------------------------------------------------------------------
# 名称归一化
# ----------------------------------------------------------------------


def test_norm_name_folds_fullwidth_and_spaces():
    """实测的真实差异：交易所官网给 '万  科Ａ'，中证给 '万科A'。

    直接字符串比较的话这只票永远查不到。
    """
    assert meta.norm_name("万  科Ａ") == meta.norm_name("万科A") == "万科A"


def test_norm_name_handles_ideographic_space():
    """U+3000 全角空格不是普通空格，str.strip() 抓不到它。"""
    assert meta.norm_name("平安　银行") == "平安银行"


def test_norm_name_uppercases():
    """ST 有时写成小写 st。"""
    assert meta.norm_name("st康美") == meta.norm_name("ST康美") == "ST康美"


def test_norm_name_on_none_and_empty():
    assert meta.norm_name(None) == ""
    assert meta.norm_name("   ") == ""


def test_norm_name_folds_fullwidth_digits():
    assert meta.norm_name("Ｔ２") == "T2"


# ----------------------------------------------------------------------
# 名称 ↔ 代码
# ----------------------------------------------------------------------


MAPPING = {
    "600519.SH": "贵州茅台",
    "000002.SZ": "万  科Ａ",
    "000001.SZ": "平安银行",
    "600000.SH": "浦发银行",
}


def test_code_of_matches_through_normalization():
    """APP 里显示 '万科A'，表里存的是 '万  科Ａ'，必须能对上。"""
    assert meta.code_of("万科A", mapping=MAPPING) == "000002.SZ"
    assert meta.code_of("万  科Ａ", mapping=MAPPING) == "000002.SZ"
    assert meta.code_of("万科Ａ", mapping=MAPPING) == "000002.SZ"


def test_code_of_unknown_raises_not_guesses():
    """猜错名字会把持仓算到别的票上，且一路到下单清单都没有提示。"""
    with pytest.raises(UnknownNameError):
        meta.code_of("不存在的公司", mapping=MAPPING)


def test_code_of_empty_raises():
    with pytest.raises(UnknownNameError):
        meta.code_of("  ", mapping=MAPPING)


def test_code_of_ambiguous_raises():
    dup = {"600001.SH": "同名股份", "000009.SZ": "同名股份"}
    with pytest.raises(AmbiguousNameError, match="匹配到多只票"):
        meta.code_of("同名股份", mapping=dup)


def test_is_st_name():
    assert meta.is_st_name("ST康美") is True
    assert meta.is_st_name("*ST康美") is True
    assert meta.is_st_name("st康美") is True
    assert meta.is_st_name("退市海润") is True
    assert meta.is_st_name("贵州茅台") is False


def test_st_detection_survives_fullwidth():
    assert meta.is_st_name("ＳＴ康美") is True


# ----------------------------------------------------------------------
# 缓存往返
# ----------------------------------------------------------------------


def test_meta_cache_roundtrip(tmp_path):
    meta.save_cached(MAPPING, root=tmp_path)
    assert meta.load_cached(root=tmp_path) == MAPPING
    assert meta.name_of("600519.SH", root=tmp_path) == "贵州茅台"
    assert meta.name_of("sh.600519", root=tmp_path) == "贵州茅台"


def test_name_of_missing_returns_empty_not_error(tmp_path):
    """清单里显示代码即可，不该因为查不到名字就崩掉。"""
    meta.save_cached(MAPPING, root=tmp_path)
    assert meta.name_of("300750.SZ", root=tmp_path) == ""


def test_meta_cache_corrupt_returns_empty(tmp_path):
    (tmp_path / "code_name.json").write_text("{not json", encoding="utf-8")
    assert meta.load_cached(root=tmp_path) == {}


def test_to_frame_adds_norm_and_st_columns(tmp_path):
    meta.save_cached({**MAPPING, "600518.SH": "ST康美"}, root=tmp_path)
    df = meta.to_frame(root=tmp_path)
    assert set(df.columns) == {"code", "name", "name_norm", "is_st"}
    assert df.loc[df["code"] == "000002.SZ", "name_norm"].iloc[0] == "万科A"
    assert df.loc[df["code"] == "600518.SH", "is_st"].iloc[0]


def test_to_frame_empty(tmp_path):
    assert meta.to_frame(root=tmp_path).empty


# ----------------------------------------------------------------------
# 行业
# ----------------------------------------------------------------------


IND = {
    "600519.SH": "食品饮料",
    "000858.SZ": "食品饮料",
    "601318.SH": "非银金融",
    "000001.SZ": "银行",
}


def test_industry_of_falls_back_to_unclassified():
    """返回具体组名而不是 NaN，这样未分类的票能自成一组互相 demean。"""
    assert industry.industry_of("600519.SH", mapping=IND) == "食品饮料"
    assert industry.industry_of("300750.SZ", mapping=IND) == "未分类"


def test_neutralize_demeans_within_industry():
    scores = pd.Series({"600519.SH": 1.0, "000858.SZ": 3.0, "601318.SH": 5.0})
    out = industry.neutralize(scores, mapping=IND)
    # 食品饮料组均值 2.0
    assert out["600519.SH"] == pytest.approx(-1.0)
    assert out["000858.SZ"] == pytest.approx(1.0)
    # 非银金融只有一只，自成一组 → demean 后为 0（我们对它的相对强弱没信息）
    assert out["601318.SH"] == pytest.approx(0.0)


def test_neutralize_removes_industry_level_bias():
    """中性化的本意：一个整体很强的行业不该让它的成员全部排在前面。"""
    scores = pd.Series({
        "600519.SH": 10.0, "000858.SZ": 11.0,   # 食品饮料，整体很高
        "601318.SH": 0.0, "000001.SZ": 1.0,     # 另两个行业，整体很低
    })
    ind = {**IND, "000001.SZ": "非银金融"}
    out = industry.neutralize(scores, mapping=ind)
    # 中性化后，两组内部的相对次序保留，但组间的绝对水平差被消掉
    assert out["000858.SZ"] == pytest.approx(out["000001.SZ"])
    assert out["600519.SH"] == pytest.approx(out["601318.SH"])


def test_neutralize_groups_unclassified_together():
    scores = pd.Series({"300750.SZ": 1.0, "300760.SZ": 3.0})
    out = industry.neutralize(scores, mapping={})
    assert out["300750.SZ"] == pytest.approx(-1.0)
    assert out["300760.SZ"] == pytest.approx(1.0)


def test_neutralize_empty():
    assert industry.neutralize(pd.Series(dtype=float), mapping=IND).empty


def test_industry_cache_roundtrip(tmp_path):
    industry.save_cached(IND, root=tmp_path)
    assert industry.load_cached(root=tmp_path) == IND


def test_coverage_reports_gaps(tmp_path):
    industry.save_cached(IND, root=tmp_path)
    cov = industry.coverage(["600519.SH", "000858.SZ", "300750.SZ"], root=tmp_path)
    assert cov == {"total": 3, "classified": 2, "unclassified": 1, "industries": 1}
