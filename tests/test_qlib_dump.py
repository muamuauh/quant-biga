"""parquet → qlib CSV 的转换。全离线（不需要 clone qlib）。

复权口径的归一化是这里唯一容易错的地方：错了不会报错，只会让模型
拿到不连续的价格序列。
"""

from __future__ import annotations

import pytest

from qbg.data import cache, qlib_dump
from tests.test_sources_chain import make_bars


def _bars(dates, closes, factors):
    df = make_bars(dates)
    df["close"] = closes
    df["open"] = closes
    df["high"] = [c * 1.01 for c in closes]
    df["low"] = [c * 0.99 for c in closes]
    df["factor"] = factors
    df["source"] = "baostock"
    return df


def test_qlib_frame_schema():
    df = _bars(["2026-08-03", "2026-08-04"], [10.0, 11.0], [1.0, 1.0])
    out = qlib_dump.to_qlib_frame(df, "600519.SH")
    assert list(out.columns) == list(qlib_dump.QLIB_COLUMNS)
    assert out["symbol"].unique().tolist() == ["SH600519"]
    assert out["date"].tolist() == ["2026-08-03", "2026-08-04"]


def test_factor_normalized_to_one_on_last_day():
    """qlib CN 约定：最后一天 factor=1，于是 $close 等于真实成交价。

    调试时看到 1309 而不是 10039，能立刻和 APP 里的价格对上。
    """
    df = _bars(["2026-08-03", "2026-08-04"], [100.0, 110.0], [7.0, 7.669])
    out = qlib_dump.to_qlib_frame(df, "600519.SH")
    assert out["factor"].iloc[-1] == pytest.approx(1.0)
    assert out["close"].iloc[-1] == pytest.approx(110.0)   # 最后一天 = 原始价


def test_qlib_close_over_factor_recovers_raw_price():
    """qlib 的不变式：raw = $close / $factor。破了它，任何按原始价的计算都会错。"""
    raw = [100.0, 110.0, 120.0]
    df = _bars(["2026-08-03", "2026-08-04", "2026-08-05"], raw, [7.0, 7.0, 7.669])
    out = qlib_dump.to_qlib_frame(df, "600519.SH")
    recovered = (out["close"] / out["factor"]).tolist()
    assert recovered == pytest.approx(raw)


def test_returns_are_preserved_through_normalization():
    """归一化是常数倍缩放，收益率序列必须完全不变——这是它安全的前提。"""
    df = _bars(["2026-08-03", "2026-08-04", "2026-08-05"],
               [100.0, 110.0, 121.0], [2.0, 2.0, 2.0])
    out = qlib_dump.to_qlib_frame(df, "600519.SH")
    hfq_ret = (df["close"] * df["factor"]).pct_change().dropna()
    qlib_ret = out["close"].pct_change().dropna()
    assert qlib_ret.tolist() == pytest.approx(hfq_ret.tolist())


def test_ex_dividend_day_is_continuous_in_qlib_close():
    """除权日：原始价跳水，复权价应当连续。这正是要用复权价训练的原因。"""
    # 10 送 10：原始收盘从 20 变成 10，但因子翻倍
    df = _bars(["2026-08-03", "2026-08-04"], [20.0, 10.0], [1.0, 2.0])
    out = qlib_dump.to_qlib_frame(df, "600519.SH")
    ret = out["close"].pct_change().iloc[-1]
    assert ret == pytest.approx(0.0)          # 复权后没有假跌
    raw_ret = df["close"].pct_change().iloc[-1]
    assert raw_ret == pytest.approx(-0.5)     # 原始价看起来腰斩


def test_volume_is_not_scaled_by_factor():
    df = _bars(["2026-08-03"], [10.0], [1.0])
    df["factor"] = 5.0
    out = qlib_dump.to_qlib_frame(df, "600519.SH")
    assert out["volume"].iloc[0] == df["volume"].iloc[0]


def test_empty_input_returns_empty_frame_with_schema():
    out = qlib_dump.to_qlib_frame(cache._empty_stored(), "600519.SH")
    assert out.empty
    assert list(out.columns) == list(qlib_dump.QLIB_COLUMNS)


def test_export_csv_skips_empty_stocks(tmp_path):
    """空 CSV 会让 dump_bin 报错中断整批，所以跳过而不是写空文件。"""
    cache.write("600519.SH", _bars(["2026-08-03"], [10.0], [1.0]), root=tmp_path)
    csv_dir = tmp_path / "csv"
    summary = qlib_dump.export_csv(["600519.SH", "000858.SZ"],
                                   csv_dir=csv_dir, parquet_root=tmp_path)
    assert summary["written"] == 1
    assert summary["skipped"] == 1
    assert (csv_dir / "SH600519.csv").exists()
    assert not (csv_dir / "SZ000858.csv").exists()


def test_instruments_uses_actual_first_last_dates(tmp_path):
    """写宽了会让次新股在它还没上市的日期参与截面排序。"""
    cache.write("600519.SH",
                _bars(["2026-08-03", "2026-08-04"], [10.0, 11.0], [1.0, 1.0]),
                root=tmp_path)
    p = qlib_dump.write_instruments(["600519.SH"], provider_uri=tmp_path / "q",
                                    parquet_root=tmp_path)
    assert p.read_text(encoding="utf-8").strip() == "SH600519\t2026-08-03\t2026-08-04"


def test_run_dump_bin_without_qlib_reports_clearly(monkeypatch, tmp_path):
    """qlib 没 clone 时给出可执行的提示，而不是抛 ImportError。"""
    monkeypatch.setattr(qlib_dump, "qlib_root", lambda: tmp_path / "no-qlib")
    result = qlib_dump.run_dump_bin(csv_dir=tmp_path, provider_uri=tmp_path / "q")
    assert result["ok"] is False
    assert result["reason"] == "qlib_not_cloned"
    assert "git clone" in result["hint"]


def test_suspended_rows_are_kept():
    """停牌行保留：qlib 用前值填充，正好等价于"持仓价值不变"。"""
    df = _bars(["2026-08-03", "2026-08-04"], [10.0, 10.0], [1.0, 1.0])
    df["is_suspended"] = [False, True]
    out = qlib_dump.to_qlib_frame(df, "600519.SH")
    assert len(out) == 2


def test_dump_uses_a_separate_csv_dir_for_a_non_default_provider(tmp_path, monkeypatch):
    """另辟 bin 时 CSV 中转目录必须分开。

    `dump_bin.py` 把目录里的**每一个** CSV 都塞进 bin。两个池子共用一个 CSV
    目录的话，小池子那份 bin 也会被灌进大池子的票 —— 而 instruments 清单看
    起来还是对的，很难发现。
    """
    from qbg.data import qlib_dump

    seen = {}

    def fake_export(members, csv_dir=None, parquet_root=None):
        seen["csv_dir"] = csv_dir
        return {"exported": 0}

    def fake_instruments(members, provider_uri=None, name="all", parquet_root=None):
        seen["instruments_uri"] = provider_uri
        return tmp_path / "instruments.txt"

    def fake_dump_bin(csv_dir=None, provider_uri=None):
        seen["bin_uri"] = provider_uri
        return {"status": "ok"}

    monkeypatch.setattr(qlib_dump, "export_csv", fake_export)
    monkeypatch.setattr(qlib_dump, "write_instruments", fake_instruments)
    monkeypatch.setattr(qlib_dump, "run_dump_bin", fake_dump_bin)

    ext = tmp_path / "qlib_bin" / "cn_data_ext"
    qlib_dump.dump(["600519.SH"], provider_uri=ext)
    assert seen["instruments_uri"] == ext
    assert seen["bin_uri"] == ext
    assert seen["csv_dir"].name.endswith("cn_data_ext")


def test_dump_keeps_the_production_csv_dir_unchanged(tmp_path, monkeypatch):
    """不传 provider_uri 时路径必须和以前逐字一致，否则会白搬一次目录。"""
    from qbg.config import settings
    from qbg.data import qlib_dump

    seen = {}
    monkeypatch.setattr(qlib_dump, "export_csv",
                        lambda m, csv_dir=None, parquet_root=None:
                        seen.update(csv_dir=csv_dir) or {"exported": 0})
    monkeypatch.setattr(qlib_dump, "write_instruments",
                        lambda m, provider_uri=None, name="all", parquet_root=None:
                        tmp_path / "i.txt")
    monkeypatch.setattr(qlib_dump, "run_dump_bin",
                        lambda csv_dir=None, provider_uri=None: {"status": "ok"})

    qlib_dump.dump(["600519.SH"])
    assert seen["csv_dir"] == settings.qlib_provider_uri.parent / "qlib_csv"
