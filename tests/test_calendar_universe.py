"""交易日历与股票池过滤。全离线。

A股有调休（国庆前后的周末可能开市），所以日历必须来自数据源，
"周一到周五减节假日"是算不出来的——这里用真实的调休日期做测试。
"""

from __future__ import annotations

import datetime as dt
import json

import pandas as pd
import pytest

from qbg.data import cache, universe
from qbg.market import calendar
from tests.test_sources_chain import FakeSource, chain_with, make_bars

# 2026 年 8 月的真实交易日（含周末休市），来自 BaoStock 实测。
AUG = ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07",
       "2026-08-10"]


# ----------------------------------------------------------------------
# 日历
# ----------------------------------------------------------------------


def test_calendar_roundtrip(tmp_path):
    calendar.save_cached(AUG, root=tmp_path)
    assert calendar.load_cached(root=tmp_path) == AUG


def test_is_trading_day(tmp_path):
    calendar.save_cached(AUG, root=tmp_path)
    assert calendar.is_trading_day("2026-08-05", root=tmp_path) is True
    assert calendar.is_trading_day("2026-08-08", root=tmp_path) is False   # 周六
    assert calendar.is_trading_day(dt.date(2026, 8, 10), root=tmp_path) is True


def test_is_trading_day_without_cache_is_false_not_crash(tmp_path):
    """日历缺失时不跑是安全的一侧，但必须留下日志（见实现）。"""
    assert calendar.is_trading_day("2026-08-05", root=tmp_path) is False


def test_next_and_previous_trading_day(tmp_path):
    calendar.save_cached(AUG, root=tmp_path)
    # 半自动模式下 next_trading_day 就是"清单要在哪天执行"
    assert calendar.next_trading_day("2026-08-07", root=tmp_path) == "2026-08-10"
    assert calendar.next_trading_day("2026-08-08", root=tmp_path) == "2026-08-10"
    assert calendar.previous_trading_day("2026-08-10", root=tmp_path) == "2026-08-07"
    assert calendar.next_trading_day("2026-08-10", root=tmp_path) is None


def test_trading_days_between_excludes_start_includes_end(tmp_path):
    calendar.save_cached(AUG, root=tmp_path)
    assert calendar.trading_days_between("2026-08-03", "2026-08-07", root=tmp_path) == 4
    assert calendar.trading_days_between("2026-08-07", "2026-08-07", root=tmp_path) == 0


def test_trading_days_range(tmp_path):
    calendar.save_cached(AUG, root=tmp_path)
    assert calendar.trading_days("2026-08-04", "2026-08-06", root=tmp_path) == [
        "2026-08-04", "2026-08-05", "2026-08-06",
    ]


def test_refresh_falls_back_to_cache_when_source_fails(tmp_path):
    """空日历会让 is_trading_day 对所有日期都说 False——看起来像永远休市。"""
    calendar.save_cached(AUG, root=tmp_path)
    dead = FakeSource("baostock", bars=None)   # trade_dates 返回空
    assert calendar.refresh(chain_with(dead), root=tmp_path) == AUG


def test_refresh_saves_fresh_dates(tmp_path):
    src = FakeSource("baostock", bars=make_bars(["2026-08-03"]))
    got = calendar.refresh(chain_with(src), root=tmp_path)
    assert got == ["2026-08-03", "2026-08-04"]
    assert calendar.load_cached(root=tmp_path) == got


@pytest.mark.parametrize(
    ("hhmm", "expected"),
    [
        ((9, 29), False),   # 开盘前
        ((9, 30), True),    # 开盘
        ((11, 30), True),   # 上午收盘
        ((12, 0), False),   # 午休 —— A股特有，美股没有
        ((13, 0), True),    # 下午开盘
        ((15, 0), True),    # 收盘
        ((15, 1), False),
    ],
)
def test_in_session_handles_lunch_break(hhmm, expected):
    now = dt.datetime(2026, 8, 10, *hhmm, tzinfo=calendar.TZ)
    assert calendar.in_session(now) is expected


# ----------------------------------------------------------------------
# 股票池过滤
# ----------------------------------------------------------------------


def _seed(tmp_path, code, *, days=400, is_st=False, is_suspended=False):
    """造一只有 `days` 天历史的票，最后一天带指定状态。"""
    dates = [
        (dt.date(2026, 8, 10) - dt.timedelta(days=days - i)).isoformat()
        for i in range(days)
    ]
    df = make_bars(dates)
    df["source"] = "baostock"
    df.loc[df.index[-1], "is_st"] = is_st
    df.loc[df.index[-1], "is_suspended"] = is_suspended
    cache.write(code, df, root=tmp_path)


def test_filter_keeps_healthy_stock(tmp_path):
    _seed(tmp_path, "600519.SH")
    rep = universe.filter_members(["600519.SH"], asof="2026-08-10",
                                  parquet_root=tmp_path, name_map={})
    assert rep.kept == ["600519.SH"]


def test_filter_drops_st_by_daily_flag(tmp_path):
    """用 BaoStock 的当日 isST，不是"今天查到的当前状态"——避免前视偏差。"""
    _seed(tmp_path, "600518.SH", is_st=True)
    rep = universe.filter_members(["600518.SH"], asof="2026-08-10",
                                  parquet_root=tmp_path, name_map={})
    assert rep.st == ["600518.SH"]
    assert rep.kept == []


def test_filter_drops_st_by_name_when_flag_missing(tmp_path):
    """降级到 AKShare 时 is_st 恒为 False，名称是唯一的兜底。"""
    _seed(tmp_path, "600518.SH", is_st=False)
    rep = universe.filter_members(["600518.SH"], asof="2026-08-10",
                                  parquet_root=tmp_path,
                                  name_map={"600518.SH": "*ST康美"})
    assert rep.st == ["600518.SH"]


def test_filter_drops_too_new(tmp_path):
    """次新股一刀切，顺带规避新股前几日的特殊涨跌幅规则。"""
    _seed(tmp_path, "301269.SZ", days=30)
    rep = universe.filter_members(["301269.SZ"], asof="2026-08-10",
                                  min_list_days=60, parquet_root=tmp_path, name_map={})
    assert rep.too_new == ["301269.SZ"]


def test_filter_drops_suspended(tmp_path):
    _seed(tmp_path, "600519.SH", is_suspended=True)
    rep = universe.filter_members(["600519.SH"], asof="2026-08-10",
                                  parquet_root=tmp_path, name_map={})
    assert rep.suspended == ["600519.SH"]


def test_filter_drops_bse_without_needing_data(tmp_path):
    """北交所不该因为"没缓存"被算进 no_data——它是被规则排除的。"""
    rep = universe.filter_members(["430047.BJ"], asof="2026-08-10",
                                  parquet_root=tmp_path, name_map={})
    assert rep.bse == ["430047.BJ"]
    assert rep.no_data == []


def test_filter_reports_no_data(tmp_path):
    rep = universe.filter_members(["600519.SH"], asof="2026-08-10",
                                  parquet_root=tmp_path, name_map={})
    assert rep.no_data == ["600519.SH"]


def test_filter_report_counts(tmp_path):
    _seed(tmp_path, "600519.SH")
    _seed(tmp_path, "600518.SH", is_st=True)
    rep = universe.filter_members(["600519.SH", "600518.SH", "430047.BJ"],
                                  asof="2026-08-10", parquet_root=tmp_path, name_map={})
    assert rep.as_dict() == {
        "kept": 1, "dropped_st": 1, "dropped_too_new": 0,
        "dropped_bse": 1, "dropped_suspended": 0, "dropped_no_data": 0,
    }


# ----------------------------------------------------------------------
# 快照（缓解生存者偏差）
# ----------------------------------------------------------------------


def test_snapshot_roundtrip(tmp_path):
    universe.save_snapshot(["600519.SH", "000858.SZ"], day="2026-08-10", root=tmp_path)
    assert universe.load_snapshot("2026-08-10", root=tmp_path) == ["000858.SZ", "600519.SH"]


def test_list_snapshots_sorted(tmp_path):
    for d in ("2026-08-10", "2026-07-01", "2026-08-03"):
        universe.save_snapshot(["600519.SH"], day=d, root=tmp_path)
    assert universe.list_snapshots(root=tmp_path) == [
        "2026-07-01", "2026-08-03", "2026-08-10",
    ]


def test_load_missing_snapshot_returns_empty(tmp_path):
    assert universe.load_snapshot("1999-01-01", root=tmp_path) == []


def test_write_universe_file_has_provenance_header(tmp_path):
    """文件头必须说明它是生成物、过滤了什么、快照在哪——否则半年后没人敢动它。"""
    p = universe.write_universe_file(["600519.SH", "000858.SZ"],
                                     path=tmp_path / "u.txt")
    text = p.read_text(encoding="utf-8")
    assert "请勿手工编辑" in text
    assert "生存者偏差" in text
    assert "600519.SH" in text and "000858.SZ" in text


# ----------------------------------------------------------------------
# 纳入日期 —— 生存者偏差里能修的那一半（全部离线：给字典或给缓存文件）
# ----------------------------------------------------------------------

def _write_inclusion(tmp_path, mapping):
    p = tmp_path / "universe" / "inclusion_dates.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(mapping), encoding="utf-8")
    return p


def test_inclusion_dates_reads_cache_without_network(tmp_path):
    """有缓存就不碰网络。测试铁律：不联网。"""
    _write_inclusion(tmp_path, {"600519.SH": "2010-01-04"})
    assert universe.inclusion_dates(root=tmp_path) == {"600519.SH": "2010-01-04"}


def test_eligibility_mask_excludes_days_before_inclusion():
    """纳入前的日子必须是 False —— 这正是「提前知道谁会赢」那个偏差的来源。"""
    dates = pd.date_range("2026-08-03", periods=4, freq="D")
    insts = ["600519.SH", "000858.SZ"]
    mask = universe.eligibility_mask(
        dates, insts, {"600519.SH": "2026-08-05"})
    assert list(mask["600519.SH"]) == [False, False, True, True]
    # 查不到纳入日期的票放行，而不是被静默排除
    assert mask["000858.SZ"].all()


def test_eligibility_mask_all_true_when_mapping_empty():
    """拿不到纳入日期时退化成修之前的行为，不能悄悄换一套口径。"""
    dates = pd.date_range("2026-08-03", periods=3, freq="D")
    mask = universe.eligibility_mask(dates, ["600519.SH"], {})
    assert mask.to_numpy().all()
