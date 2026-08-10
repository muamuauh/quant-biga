"""沪深300（或股票池等权代理）相对移动均线的市场状态过滤。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from qbg.config import settings
from qbg.data import cache


def risk_on_series(level: pd.Series, sma_window: int) -> pd.Series:
    """返回每个收盘日可得到的风险状态；历史不足时默认 risk-on。"""
    level = level.astype(float).sort_index()
    if sma_window <= 0:
        return pd.Series(True, index=level.index, dtype=bool)
    sma = level.rolling(sma_window, min_periods=sma_window).mean()
    return (level >= sma).where(sma.notna(), True).astype(bool)


def is_risk_on(level: pd.Series, sma_window: int) -> bool:
    """最新收盘是否不低于 N 日均线；数据不足时不误清仓。"""
    clean = level.dropna().sort_index()
    return True if clean.empty else bool(risk_on_series(clean, sma_window).iloc[-1])


def equal_weight_index(codes_list: list[str], root: Path | None = None,
                       price_column: str = "close") -> pd.Series:
    """从本项目 parquet 构造后复权等权指数，作为沪深300状态代理。"""
    columns: dict[str, pd.Series] = {}
    for code in codes_list:
        df = cache.read(code, root)
        if df.empty or price_column not in df:
            continue
        adjusted = df[price_column].astype(float) * df["factor"].astype(float)
        columns[code] = pd.Series(adjusted.to_numpy(), index=pd.to_datetime(df["date"]))
    if not columns:
        return pd.Series(dtype=float)
    prices = pd.DataFrame(columns).sort_index().ffill()
    normalized = prices.div(prices.apply(lambda col: col.dropna().iloc[0] if col.notna().any() else 1.0))
    return normalized.mean(axis=1, skipna=True).rename("market")


def market_risk_on(codes_list: list[str], sma_window: int | None = None,
                   root: Path | None = None) -> bool:
    """读取缓存并报告当前市场状态。"""
    window = settings.qbg_market_sma if sma_window is None else sma_window
    return is_risk_on(equal_weight_index(codes_list, root), window)

