"""代码格式转换。全离线。

这一层错了会让整条链路拿到**另一只票**的行情，而且错得很安静，
所以每种格式和每个边界都要有测试。
"""

from __future__ import annotations

import pytest

from qbg.market import codes
from qbg.market.codes import UnknownCodeError


@pytest.mark.parametrize(
    ("digits", "exchange"),
    [
        ("600519", "SH"), ("601318", "SH"), ("603259", "SH"), ("605499", "SH"),
        ("688981", "SH"), ("689009", "SH"),
        ("000001", "SZ"), ("001979", "SZ"), ("002594", "SZ"), ("003816", "SZ"),
        ("300750", "SZ"), ("301269", "SZ"),
        ("430047", "BJ"), ("830799", "BJ"), ("870508", "BJ"), ("920008", "BJ"),
    ],
)
def test_exchange_inference_by_prefix(digits, exchange):
    assert codes.exchange_of(digits) == exchange


@pytest.mark.parametrize("bad", ["", "60051", "6005199", "abcdef", "12345a"])
def test_exchange_of_rejects_malformed(bad):
    with pytest.raises(UnknownCodeError):
        codes.exchange_of(bad)


def test_exchange_of_rejects_unknown_prefix():
    """B股/基金/债券不在支持范围内——宁可报错也不猜。"""
    for d in ("900001", "200011", "510300", "110059"):
        with pytest.raises(UnknownCodeError):
            codes.exchange_of(d)


@pytest.mark.parametrize(
    "raw",
    ["600519.SH", "600519.sh", "sh.600519", "SH.600519", "SH600519", "600519"],
)
def test_normalize_accepts_all_known_formats(raw):
    assert codes.normalize(raw) == "600519.SH"


def test_normalize_strips_whitespace():
    assert codes.normalize("  000858.SZ  ") == "000858.SZ"


@pytest.mark.parametrize("bad", ["", "   ", "600519.XX", "hk.00700", "AAPL"])
def test_normalize_rejects_unknown(bad):
    with pytest.raises(UnknownCodeError):
        codes.normalize(bad)


def test_per_source_formats():
    assert codes.to_baostock("600519.SH") == "sh.600519"
    assert codes.to_baostock("000858.SZ") == "sz.000858"
    assert codes.to_qlib("600519.SH") == "SH600519"
    assert codes.digits("sh.600519") == "600519"
    assert codes.exchange("SH600519") == "SH"


def test_mootdx_market_encoding():
    """通达信 market：1=上交所 0=深交所 2=北交所。写反了会拿到别的市场的票。"""
    assert codes.to_mootdx("600519.SH") == (1, "600519")
    assert codes.to_mootdx("000858.SZ") == (0, "000858")
    assert codes.to_mootdx("430047.BJ") == (2, "430047")


def test_roundtrip_through_every_format():
    for canonical in ("600519.SH", "000858.SZ", "300750.SZ", "688981.SH"):
        assert codes.normalize(codes.to_baostock(canonical)) == canonical
        assert codes.normalize(codes.to_qlib(canonical)) == canonical
        assert codes.normalize(codes.digits(canonical)) == canonical


def test_is_bse():
    """北交所默认排除：涨跌幅 ±30%、流动性差、开户门槛不同。"""
    assert codes.is_bse("430047.BJ") is True
    assert codes.is_bse("600519.SH") is False
