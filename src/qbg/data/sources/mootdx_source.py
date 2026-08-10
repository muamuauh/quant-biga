"""Mootdx（通达信）日线源 —— 第三备胎。

BaoStock 和 AKShare 都拿不到时的最后兜底。它直连通达信行情服务器，
不经过任何网页，所以在东财被限流、BaoStock 服务器抽风时还能出数。

## 局限（所以它排最后，且默认不在降级链里）

  · **不给复权因子** —— `factor` 一律 1.0，等于不复权。用它填出来的数据
    不能直接拿去训练模型（除权日会出现假跌）。
  · **不给停牌/ST 标志** —— 和 AKShare 一样填 False。
  · **回溯深度有限** —— 通达信服务器按 offset 取最近 N 根，拉不到很久以前
    的历史。

综合起来：它只适合"补最近几天的洞"，不适合建立历史库。
`cache.py` 会把用它填的行记进日志，这样后续能识别并重拉。

## 状态

代码按 mootdx 0.11 的 API 写成，但**未在真实网络下验证过**（写的时候
BaoStock 主源正常，没有触发降级的场景）。第一次真正走到这条分支时
请核对返回结构。
"""

from __future__ import annotations

import pandas as pd

from qbg.data.sources.base import SourceUnavailable, empty_bars, normalize_bars
from qbg.market import codes
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

# 通达信的 frequency 编码：9 = 日线。
_FREQ_DAILY = 9
# 单次最多回溯的 K 线根数。通达信服务器本身有上限，取大了会被截断。
_MAX_BARS = 800

_RENAME = {
    "datetime": "date",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "vol": "volume",
    "amount": "amount",
}


def _client():
    try:
        from mootdx.quotes import Quotes
    except ImportError as e:  # pragma: no cover
        raise SourceUnavailable("mootdx 未安装：pip install -e '.[data]'") from e
    try:
        return Quotes.factory(market="std")
    except Exception as e:  # noqa: BLE001 — 连不上行情服务器
        raise SourceUnavailable(f"mootdx 无法连接行情服务器: {e}") from e


class MootdxSource:
    """`DailyBarSource` 实现。见模块说明的局限。"""

    name = "mootdx"

    def fetch(self, code: str, start: str, end: str) -> pd.DataFrame:
        _, symbol = codes.to_mootdx(code)
        client = _client()
        try:
            df = client.bars(symbol=symbol, frequency=_FREQ_DAILY, offset=_MAX_BARS)
        except Exception as e:  # noqa: BLE001
            log_event(log, "source.mootdx.bars.error", symbol=symbol, error=str(e)[:200])
            return empty_bars()

        if df is None or len(df) == 0:
            return empty_bars()

        out = df.reset_index()
        # mootdx 不同版本把日期放在 index 或 'datetime' 列，两种都兜住。
        if "datetime" not in out.columns:
            date_col = next((c for c in out.columns if "date" in str(c).lower()), None)
            if date_col is None:
                log_event(log, "source.mootdx.bars.no_date_column",
                          symbol=symbol, columns=[str(c) for c in out.columns])
                return empty_bars()
            out = out.rename(columns={date_col: "datetime"})

        out = out.rename(columns=_RENAME)
        keep = [c for c in ("date", "open", "high", "low", "close", "volume", "amount")
                if c in out.columns]
        out = out[keep].copy()

        # 服务器只按根数回溯，日期范围要自己裁。
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out = out[(out["date"] >= pd.Timestamp(start)) & (out["date"] <= pd.Timestamp(end))]

        # 见模块说明：这三个字段本源给不出，填成"无复权、非停牌、非ST"。
        out["factor"] = 1.0
        out["is_st"] = False
        out["is_suspended"] = False
        return normalize_bars(out)

    def trade_dates(self, start: str, end: str) -> list[str]:
        """不支持。交易日历由 BaoStock / AKShare 提供。"""
        return []
