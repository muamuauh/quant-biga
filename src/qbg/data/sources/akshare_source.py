"""AKShare 日线源 —— 备源。

只在 BaoStock 拿不到时兜底，**不适合当主源**：底层爬东方财富，批量拉会
限频、封 IP、跳验证页。它在本项目的主要价值是元数据（股票池、行业、
代码名称表），那些在 `qbg.data.meta` / `universe` / `industry` 里。

## 两个必须处理的差异

**1. 成交量单位是"手"，不是"股"。**
东财返回的 `成交量` 以手（100 股）计，而 BaoStock 以股计。实测对账：

    BaoStock  600519  volume=4268859 股 × ~1311 元 ≈ amount 5.6e9   ✓
    AKShare   600519  volume=106151 手 × 100 × ~1309 ≈ amount 1.39e10 ✓

不换算的话，同一只票在两个源之间成交量差 100 倍，而且**不会报任何错**——
任何量价因子（换手率、量比、成交额加权）都会被静默污染。所以这里统一
乘 100 转成股。

**2. 没有停牌和 ST 标志。**
东财的历史接口不返回 `tradestatus` / `isST`，本源只能把两者填 False。
这是 BaoStock 必须当主源的核心原因——降级到 AKShare 时，停牌/ST 过滤
会失效，`cache.py` 会把这件事记进日志而不是让它悄悄过去。

## 环境注意

实测（2026-08-10 本机）：`push2his.eastmoney.com`（历史行情，本模块用的）
可达，但 `push2*.eastmoney.com`（实时快照、东财行业、个股信息）被重置。
所以本项目的任何设计都不依赖东财的实时接口。
"""

from __future__ import annotations

import pandas as pd

from qbg.data.sources.base import SourceUnavailable, empty_bars, normalize_bars
from qbg.market import codes
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

# 东财历史接口的中文列名 → 本项目的列名。
_RENAME = {
    "日期": "date",
    "开盘": "open",
    "最高": "high",
    "最低": "low",
    "收盘": "close",
    "成交量": "volume",   # 单位是手，下面要 ×100
    "成交额": "amount",
}

# 东财成交量的单位是"手"，1 手 = 100 股。
_LOT = 100


def _ak():
    try:
        import akshare as ak
    except ImportError as e:  # pragma: no cover
        raise SourceUnavailable("akshare 未安装：pip install -e '.[data]'") from e
    return ak


def _fmt(d: str) -> str:
    """`2026-08-10` → `20260810`（东财要这个格式）。"""
    return str(d).replace("-", "")


def _hist(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    ak = _ak()
    try:
        return ak.stock_zh_a_hist(
            symbol=symbol, period="daily",
            start_date=_fmt(start), end_date=_fmt(end),
            adjust=adjust,
        )
    except Exception as e:  # noqa: BLE001 — 网络/限频/改版都在这里兜住
        log_event(log, "source.akshare.hist.error",
                  symbol=symbol, adjust=adjust or "raw", error=str(e)[:200])
        return pd.DataFrame()


class AkshareSource:
    """`DailyBarSource` 实现。"""

    name = "akshare"

    def fetch(self, code: str, start: str, end: str) -> pd.DataFrame:
        symbol = codes.digits(code)
        raw = _hist(symbol, start, end, adjust="")
        if raw is None or raw.empty:
            return empty_bars()
        hfq = _hist(symbol, start, end, adjust="hfq")

        df = raw.rename(columns=_RENAME)[list(_RENAME.values())].copy()
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce") * _LOT
        df["factor"] = _derive_factor(raw, hfq)
        # 东财历史接口不给这两个标志。填 False 意味着降级到本源时停牌/ST
        # 过滤会失效——由调用方记录，不在这里假装有数据。
        df["is_st"] = False
        df["is_suspended"] = False
        return normalize_bars(df)

    def trade_dates(self, start: str, end: str) -> list[str]:
        ak = _ak()
        try:
            df = ak.tool_trade_date_hist_sina()
        except Exception as e:  # noqa: BLE001
            log_event(log, "source.akshare.trade_dates.error", error=str(e)[:200])
            return []
        if df is None or df.empty:
            return []
        s = pd.to_datetime(df.iloc[:, 0], errors="coerce").dropna()
        s = s[(s >= pd.Timestamp(start)) & (s <= pd.Timestamp(end))]
        return sorted(s.dt.strftime("%Y-%m-%d").tolist())


def _derive_factor(raw: pd.DataFrame, hfq: pd.DataFrame) -> pd.Series:
    """factor = 后复权收盘 / 不复权收盘，按日期对齐。

    注意各源的后复权**锚点不同**（AKShare 锚在上市首日，BaoStock 也是，
    但具体口径未必一致），所以同一只票在两个源上的 factor 数值可以不同。
    这不影响正确性：factor 对某只票是个常数倍，收益率序列完全一样。
    """
    if hfq is None or hfq.empty or "收盘" not in hfq.columns:
        return pd.Series(1.0, index=raw.index)

    raw_close = pd.to_numeric(raw["收盘"], errors="coerce")
    hfq_close = pd.to_numeric(
        hfq.set_index(hfq["日期"].astype(str))["收盘"], errors="coerce"
    )
    factor = raw["日期"].astype(str).map(hfq_close) / raw_close
    return factor.replace([float("inf"), float("-inf")], pd.NA).fillna(1.0)
