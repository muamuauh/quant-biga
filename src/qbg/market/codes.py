"""A股代码格式转换。

每个数据源用的代码格式都不一样，这是 A股工具链最烦人的一处摩擦：

    规范形式（本项目内部统一用这个）  600519.SH   000858.SZ
    BaoStock                          sh.600519   sz.000858
    AKShare（stock_zh_a_hist）        600519      000858      ← 只要 6 位
    Mootdx                            market=1 + 600519       ← 市场用数字
    qlib（CN region）                 SH600519    SZ000858

规范形式选 `600519.SH` 而不是别的，是因为它是 tushare/Wind 的约定，
在中文量化圈里最通用，用户看到也最容易认。

**交易所推断只看代码前缀**，不查任何接口——离线可判、可测试。
板块识别和涨跌停在 `rules.py`（P2），那是另一件事。
"""

from __future__ import annotations

import re

# 交易所前缀 → 该交易所的股票代码开头。按最长前缀优先匹配。
# 只列**股票**，不含 B 股 / 基金 / 债券（本项目不碰它们）。
_SH_PREFIXES = ("600", "601", "603", "605", "688", "689")
_SZ_PREFIXES = ("000", "001", "002", "003", "300", "301")
# 北交所：老代码段 43/83/87/88，2024 年起新增 920。
_BJ_PREFIXES = ("430", "830", "831", "832", "833", "834", "835", "836", "837",
                "838", "839", "870", "871", "872", "873", "874", "875", "876",
                "877", "878", "879", "880", "889", "920")

_CANONICAL_RE = re.compile(r"^(\d{6})\.(SH|SZ|BJ)$")
_BAOSTOCK_RE = re.compile(r"^(sh|sz|bj)\.(\d{6})$", re.IGNORECASE)
_QLIB_RE = re.compile(r"^(SH|SZ|BJ)(\d{6})$")


class UnknownCodeError(ValueError):
    """代码既推断不出交易所、也不是任何已知格式。

    刻意抛异常而不是猜：猜错交易所会让整条链路拿到错误的行情，
    而且错得很安静（另一个交易所可能真有同号的票）。
    """


def exchange_of(digits: str) -> str:
    """6 位数字码 → 'SH' | 'SZ' | 'BJ'。推断不出就抛 UnknownCodeError。"""
    if not (len(digits) == 6 and digits.isdigit()):
        raise UnknownCodeError(f"不是 6 位数字代码: {digits!r}")
    if digits.startswith(_SH_PREFIXES):
        return "SH"
    if digits.startswith(_SZ_PREFIXES):
        return "SZ"
    if digits.startswith(_BJ_PREFIXES):
        return "BJ"
    raise UnknownCodeError(
        f"无法推断交易所: {digits!r}（本项目只支持 A股股票，不含 B股/基金/债券）"
    )


def normalize(code: str) -> str:
    """任意已知格式 → 规范形式 `600519.SH`。

    接受：`600519.SH` / `sh.600519` / `SH600519` / `600519`（裸 6 位）。
    """
    s = str(code).strip()
    if not s:
        raise UnknownCodeError("空代码")

    m = _CANONICAL_RE.match(s.upper())
    if m:
        return f"{m.group(1)}.{m.group(2)}"

    m = _BAOSTOCK_RE.match(s)
    if m:
        return f"{m.group(2)}.{m.group(1).upper()}"

    m = _QLIB_RE.match(s.upper())
    if m:
        return f"{m.group(2)}.{m.group(1)}"

    if len(s) == 6 and s.isdigit():
        return f"{s}.{exchange_of(s)}"

    raise UnknownCodeError(f"无法识别的代码格式: {code!r}")


def digits(code: str) -> str:
    """规范形式 → 裸 6 位数字（AKShare 的 stock_zh_a_hist 要这个）。"""
    return normalize(code).split(".")[0]


def exchange(code: str) -> str:
    """规范形式 → 'SH' | 'SZ' | 'BJ'。"""
    return normalize(code).split(".")[1]


def to_baostock(code: str) -> str:
    """规范形式 → `sh.600519`。"""
    d, ex = normalize(code).split(".")
    return f"{ex.lower()}.{d}"


def to_qlib(code: str) -> str:
    """规范形式 → `SH600519`（qlib CN region 的 instrument 名）。"""
    d, ex = normalize(code).split(".")
    return f"{ex}{d}"


def to_mootdx(code: str) -> tuple[int, str]:
    """规范形式 → `(market, '600519')`。

    通达信的 market 编码：1 = 上交所，0 = 深交所，2 = 北交所。
    """
    d, ex = normalize(code).split(".")
    market = {"SH": 1, "SZ": 0, "BJ": 2}[ex]
    return market, d


def is_bse(code: str) -> bool:
    """是不是北交所。

    北交所本项目默认排除：涨跌幅 ±30%、流动性差、开户还需 50 万+2 年经验，
    和沪深主板完全不是一类标的。
    """
    return exchange(code) == "BJ"
