"""增量 parquet 缓存。全离线——用假源。

重点是增量边界和合并优先级：这两处错了不会报错，只会让数据悄悄不对。
"""

from __future__ import annotations

import pandas as pd

from qbg.data import cache
from qbg.data.sources.base import SourceUnavailable, empty_bars
from tests.test_sources_chain import FakeSource, chain_with, make_bars


def _write(tmp_path, code, dates, close=10.0, source="baostock"):
    df = make_bars(dates, close=close)
    df["source"] = source
    return cache.write(code, df, root=tmp_path)


# ----------------------------------------------------------------------
# 读写往返
# ----------------------------------------------------------------------


def test_read_missing_file_returns_empty_not_error():
    """首次运行本来就没有缓存，这不是异常情况。"""
    df = cache.read("600519.SH", root=cache.settings.parquet_dir / "does-not-exist")
    assert df.empty
    assert "source" in df.columns


def test_write_read_roundtrip(tmp_path):
    _write(tmp_path, "600519.SH", ["2026-08-03", "2026-08-04"])
    out = cache.read("600519.SH", root=tmp_path)
    assert len(out) == 2
    assert list(out.columns) == list(cache.STORED_COLUMNS)
    assert out["source"].tolist() == ["baostock", "baostock"]


def test_filename_uses_canonical_code(tmp_path):
    p = _write(tmp_path, "sh.600519", ["2026-08-03"])
    assert p.name == "600519.SH.parquet"


def test_last_date(tmp_path):
    _write(tmp_path, "600519.SH", ["2026-08-03", "2026-08-05"])
    assert cache.last_date("600519.SH", root=tmp_path) == pd.Timestamp("2026-08-05")
    assert cache.last_date("000858.SZ", root=tmp_path) is None


def test_corrupt_file_returns_empty_not_crash(tmp_path):
    """一个坏文件不该让整批 300 只的 ingest 崩掉。"""
    tmp_path.mkdir(parents=True, exist_ok=True)
    cache.path_for("600519.SH", tmp_path).write_bytes(b"not a parquet file")
    assert cache.read("600519.SH", root=tmp_path).empty


# ----------------------------------------------------------------------
# 合并优先级
# ----------------------------------------------------------------------


def test_merge_prefers_new_on_same_date():
    """新数据优先是有意的——重拉尾部就是为了让事后修正能覆盖旧值。"""
    old = make_bars(["2026-08-03"], close=10.0)
    new = make_bars(["2026-08-03"], close=99.0)
    out = cache.merge(old, new)
    assert len(out) == 1
    assert out.loc[0, "close"] == 99.0


def test_merge_with_empty_sides():
    bars = make_bars(["2026-08-03"])
    assert len(cache.merge(empty_bars(), bars)) == 1
    assert len(cache.merge(bars, empty_bars())) == 1
    assert len(cache.merge(empty_bars(), empty_bars())) == 0


def test_merge_keeps_sorted_and_unique():
    old = make_bars(["2026-08-03", "2026-08-04"])
    new = make_bars(["2026-08-04", "2026-08-05"])
    out = cache.merge(old, new)
    assert out["date"].is_monotonic_increasing
    assert len(out) == 3


def test_merge_preserves_source_per_row():
    """降级发生在哪几天必须可追溯，否则停牌标志的洞是不可见的。"""
    old = make_bars(["2026-08-03"])
    old["source"] = "baostock"
    new = make_bars(["2026-08-04"])
    new["source"] = "akshare"
    out = cache.merge(old, new)
    assert out.set_index(out["date"].dt.strftime("%Y-%m-%d"))["source"].to_dict() == {
        "2026-08-03": "baostock", "2026-08-04": "akshare",
    }


# ----------------------------------------------------------------------
# 增量边界
# ----------------------------------------------------------------------


def test_update_from_scratch_fetches_full_range(tmp_path):
    src = FakeSource("baostock", bars=make_bars(["2026-08-03", "2026-08-04"]))
    summary = cache.update("600519.SH", chain_with(src),
                           start="2026-08-01", end="2026-08-05", root=tmp_path)
    assert summary["added"] == 2
    assert summary["rows"] == 2
    assert summary["source"] == "baostock"


def test_update_is_incremental_and_refreshes_tail(tmp_path, monkeypatch):
    """增量应从 last_date - (refresh_tail_days - 1) 开始，让 T+1 修正能进来。"""
    _write(tmp_path, "600519.SH", ["2026-08-03", "2026-08-04"])

    seen = {}

    class Recorder(FakeSource):
        def fetch(self, code, start, end):
            seen["start"] = start
            return make_bars(["2026-08-04", "2026-08-05"], close=99.0)

    cache.update("600519.SH", chain_with(Recorder("baostock", bars=1)),
                 start="2026-08-01", end="2026-08-06",
                 refresh_tail_days=2, root=tmp_path)

    # last_date = 08-04，refresh_tail_days=2 → 从 08-03 开始重拉
    assert seen["start"] == "2026-08-03"
    out = cache.read("600519.SH", root=tmp_path)
    assert len(out) == 3
    # 08-04 被新数据覆盖了
    assert out.loc[out["date"] == pd.Timestamp("2026-08-04"), "close"].iloc[0] == 99.0


def test_update_skips_when_cache_newer_than_requested_end(tmp_path):
    _write(tmp_path, "600519.SH", ["2026-08-03", "2026-08-04"])
    src = FakeSource("baostock", bars=make_bars(["2026-08-09"]))
    summary = cache.update("600519.SH", chain_with(src),
                           start="2026-08-01", end="2026-08-01", root=tmp_path)
    assert summary["skipped"] is True
    assert summary["added"] == 0
    assert src.calls == []   # 根本没发请求


def test_force_full_ignores_cache(tmp_path):
    _write(tmp_path, "600519.SH", ["2026-08-03"], close=10.0)
    src = FakeSource("baostock", bars=make_bars(["2026-08-03"], close=77.0))
    cache.update("600519.SH", chain_with(src), start="2026-08-01",
                 end="2026-08-05", force_full=True, root=tmp_path)
    out = cache.read("600519.SH", root=tmp_path)
    assert out.loc[0, "close"] == 77.0


def test_update_with_empty_fetch_keeps_cache(tmp_path):
    """源没数据时不该把已有缓存清掉。"""
    _write(tmp_path, "600519.SH", ["2026-08-03"])
    src = FakeSource("baostock", bars=None)
    summary = cache.update("600519.SH", chain_with(src),
                           start="2026-08-01", end="2026-08-09", root=tmp_path)
    assert summary["empty_fetch"] is True
    assert len(cache.read("600519.SH", root=tmp_path)) == 1


def test_update_records_source_and_degradation(tmp_path):
    # 注意用"主源挂了"而不是"主源返回空"来触发降级：返回空按 chain 的
    # 语义是"这只票没数据"，不会换源。
    primary = FakeSource("baostock", raises=SourceUnavailable("挂了"))
    backup = FakeSource("akshare", bars=make_bars(["2026-08-03"]))
    summary = cache.update("600519.SH", chain_with(primary, backup),
                           start="2026-08-01", end="2026-08-05", root=tmp_path)
    assert summary["degraded"] is True
    assert cache.read("600519.SH", root=tmp_path)["source"].iloc[0] == "akshare"


# ----------------------------------------------------------------------
# 自洽校验
# ----------------------------------------------------------------------


def test_verify_clean_data_has_no_problems():
    assert cache.verify(make_bars(["2026-08-03", "2026-08-04"])) == []


def test_verify_catches_decreasing_factor():
    """后复权因子只会因分红送股累积上升，递减说明源的口径中途变了。"""
    df = make_bars(["2026-08-03", "2026-08-04"])
    df["factor"] = [2.0, 1.0]
    assert any("factor 递减" in p for p in cache.verify(df))


def test_verify_catches_nonpositive_factor():
    df = make_bars(["2026-08-03"])
    df["factor"] = [0.0]
    assert any("factor 有非正值" in p for p in cache.verify(df))


def test_verify_catches_nonpositive_price_on_trading_day():
    df = make_bars(["2026-08-03"])
    df["close"] = [0.0]
    assert any("close" in p for p in cache.verify(df))


def test_verify_allows_zero_price_on_suspended_day():
    """停牌日没有成交，0 价是正常的，不该报警。"""
    df = make_bars(["2026-08-03"])
    df["close"] = [0.0]
    df["is_suspended"] = [True]
    assert cache.verify(df) == []


def test_verify_catches_high_below_low():
    df = make_bars(["2026-08-03"])
    df["high"], df["low"] = [1.0], [9.0]
    assert any("high < low" in p for p in cache.verify(df))


def test_verify_empty_is_clean():
    assert cache.verify(empty_bars()) == []


# ----------------------------------------------------------------------
# 复权视图
# ----------------------------------------------------------------------


def test_hfq_multiplies_ohlc_only():
    """成交量/成交额不该被复权因子缩放——只有价格该。"""
    df = make_bars(["2026-08-03"], close=10.0)
    df["factor"] = 2.0
    out = cache.hfq(df)
    assert out.loc[0, "close"] == 20.0
    assert out.loc[0, "open"] == 20.0
    assert out.loc[0, "volume"] == df.loc[0, "volume"]
    assert out.loc[0, "amount"] == df.loc[0, "amount"]


def test_hfq_does_not_mutate_input():
    df = make_bars(["2026-08-03"], close=10.0)
    df["factor"] = 2.0
    cache.hfq(df)
    assert df.loc[0, "close"] == 10.0
