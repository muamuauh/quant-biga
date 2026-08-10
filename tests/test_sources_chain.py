"""数据源契约与降级链。全离线——所有源都是假的。

重点验证降级链最容易写错的那条区分：
**"源挂了"要换源，"这只票没数据"不换源。**
"""

from __future__ import annotations

import pandas as pd
import pytest

from qbg.data.sources.base import (
    BAR_COLUMNS,
    SourceUnavailable,
    empty_bars,
    normalize_bars,
)
from qbg.data.sources.chain import SourceChain

# ----------------------------------------------------------------------
# 假源
# ----------------------------------------------------------------------


def make_bars(dates: list[str], close: float = 10.0, source_extra: dict | None = None):
    df = pd.DataFrame({
        "date": pd.to_datetime(dates),
        "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
        "volume": 1000.0, "amount": close * 1000.0, "factor": 1.0,
        "is_st": False, "is_suspended": False,
    })
    if source_extra:
        for k, v in source_extra.items():
            df[k] = v
    return df


class FakeSource:
    def __init__(self, name, bars=None, raises=None, calls=None):
        self.name = name
        self._bars = bars
        self._raises = raises
        self.calls = calls if calls is not None else []

    def fetch(self, code, start, end):
        self.calls.append(("fetch", code))
        if self._raises:
            raise self._raises
        return self._bars if self._bars is not None else empty_bars()

    def trade_dates(self, start, end):
        self.calls.append(("trade_dates", start))
        if self._raises:
            raise self._raises
        return ["2026-08-03", "2026-08-04"] if self._bars is not None else []


def chain_with(*sources) -> SourceChain:
    """构造一个跳过真实源加载的 SourceChain。"""
    c = SourceChain(names=[s.name for s in sources])
    c._sources = {s.name: s for s in sources}
    return c


# ----------------------------------------------------------------------
# base 契约
# ----------------------------------------------------------------------


def test_empty_bars_has_full_schema():
    df = empty_bars()
    assert list(df.columns) == list(BAR_COLUMNS)
    assert len(df) == 0
    # 能直接和真实数据 concat 而不产生 object 列。
    merged = pd.concat([df, make_bars(["2026-08-03"])], ignore_index=True)
    assert len(merged) == 1


def test_normalize_fills_missing_columns_with_nan_not_zero():
    """0 是个合法价格。用它当'没有'会让下游算出 -100% 收益却毫无察觉。"""
    df = pd.DataFrame({"date": ["2026-08-03"], "close": [10.0]})
    out = normalize_bars(df)
    assert pd.isna(out.loc[0, "open"])
    assert out.loc[0, "close"] == 10.0


def test_normalize_defaults_factor_to_one():
    """没有除权记录的票，因子本来就该是 1，不是 NaN。"""
    out = normalize_bars(pd.DataFrame({"date": ["2026-08-03"], "close": [10.0]}))
    assert out.loc[0, "factor"] == 1.0


def test_normalize_sorts_and_dedups_keeping_last():
    df = pd.DataFrame({
        "date": ["2026-08-05", "2026-08-03", "2026-08-05"],
        "close": [1.0, 2.0, 99.0],
    })
    out = normalize_bars(df)
    assert out["date"].is_monotonic_increasing
    assert len(out) == 2
    # 同一天重复只可能是源的毛病，保留后到的。
    assert out.loc[out["date"] == pd.Timestamp("2026-08-05"), "close"].iloc[0] == 99.0


def test_normalize_drops_unparseable_dates():
    df = pd.DataFrame({"date": ["2026-08-03", "not-a-date"], "close": [1.0, 2.0]})
    assert len(normalize_bars(df)) == 1


def test_normalize_handles_none_and_empty():
    assert len(normalize_bars(None)) == 0
    assert list(normalize_bars(pd.DataFrame()).columns) == list(BAR_COLUMNS)


# ----------------------------------------------------------------------
# 降级链
# ----------------------------------------------------------------------


def test_primary_wins_when_it_has_data():
    primary = FakeSource("baostock", bars=make_bars(["2026-08-03"]))
    backup = FakeSource("akshare", bars=make_bars(["2026-08-03"]))
    res = chain_with(primary, backup).fetch("600519.SH", "2026-08-01", "2026-08-05")

    assert res.source == "baostock"
    assert res.degraded is False
    assert backup.calls == []  # 备源根本没被碰


def test_falls_back_when_primary_unavailable():
    primary = FakeSource("baostock", raises=SourceUnavailable("登录失败"))
    backup = FakeSource("akshare", bars=make_bars(["2026-08-03"]))
    res = chain_with(primary, backup).fetch("600519.SH", "2026-08-01", "2026-08-05")

    assert res.source == "akshare"
    assert res.degraded is True
    assert not res.empty


def test_falls_back_on_unexpected_exception():
    """源抛了意料之外的错，也不该让整批 ingest 崩掉。"""
    primary = FakeSource("baostock", raises=ValueError("源改版了"))
    backup = FakeSource("akshare", bars=make_bars(["2026-08-03"]))
    res = chain_with(primary, backup).fetch("600519.SH", "2026-08-01", "2026-08-05")
    assert res.source == "akshare"


def test_empty_result_does_NOT_fall_back():
    """核心区分：这只票没数据 ≠ 源挂了。

    退市股在任何源上都查不到，换源只是白等三次。
    """
    primary = FakeSource("baostock", bars=None)   # 返回空
    backup = FakeSource("akshare", bars=make_bars(["2026-08-03"]))
    res = chain_with(primary, backup).fetch("000004.SZ", "2026-08-01", "2026-08-05")

    assert res.empty
    assert res.source == "baostock"
    assert backup.calls == []  # 关键：没有去试备源


def test_dead_source_is_not_retried_for_every_code():
    """主源挂了只该拖慢第一只票，不该每只都等一次超时。"""
    primary = FakeSource("baostock", raises=SourceUnavailable("连不上"))
    backup = FakeSource("akshare", bars=make_bars(["2026-08-03"]))
    chain = chain_with(primary, backup)

    for code in ("600519.SH", "000858.SZ", "300750.SZ"):
        chain.fetch(code, "2026-08-01", "2026-08-05")

    assert len(primary.calls) == 1   # 只被试过一次
    assert len(backup.calls) == 3


def test_all_sources_failed_returns_empty_with_no_source():
    a = FakeSource("baostock", raises=SourceUnavailable("x"))
    b = FakeSource("akshare", raises=SourceUnavailable("y"))
    res = chain_with(a, b).fetch("600519.SH", "2026-08-01", "2026-08-05")

    assert res.empty
    assert res.source == ""
    assert res.degraded is True


def test_trade_dates_skips_sources_returning_empty():
    """Mootdx 不支持日历（返回空），链条应自然跳过它。"""
    nodates = FakeSource("mootdx", bars=None)
    hasdates = FakeSource("baostock", bars=make_bars(["2026-08-03"]))
    dates = chain_with(nodates, hasdates).trade_dates("2026-08-01", "2026-08-05")
    assert dates == ["2026-08-03", "2026-08-04"]


def test_empty_source_list_rejected():
    """显式传空列表是调用方的 bug，不该静默退回全局默认。"""
    with pytest.raises(ValueError, match="至少要配一个"):
        SourceChain(names=[])


def test_unknown_source_name_fails_loudly_at_construction():
    """源名拼错是配置错误，不是运行时降级。

    必须在构造时就炸——降级成"全失败静默返回空数据"会让一个 .env 的
    typo 表现为"今天没有任何行情"，而那看起来和休市一模一样。
    """
    with pytest.raises(ValueError, match="未知数据源"):
        SourceChain(names=["baostock", "nope"])
