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
    """从本项目 parquet 构造**等权组合的净值曲线**，作为市场状态代理。

    实现是「每日等权收益累乘」，不是「归一化价格取平均」。这个区别至关重要，
    而且原来就写错了：

        错：把每只股票按首日价归一，然后对这些水平取平均
            → 涨了 5 倍的票在曲线里的权重就是没涨的票的 5 倍，
              得到的是一条**增长加权**的曲线，不是任何真实组合的净值。
        对：每天先算等权平均收益，再累乘
            → 就是一个每日再平衡的等权组合，和压测里
              `open_to_open_returns().mean(axis=1)` 度量的是同一个东西。

    2026-08-31 实测这个差别有多大（本轮 risk-off，07-16 起 31 个交易日）：
        等权组合  +1.96%      ← 账户实际会赚的
        旧的"指数" −2.16%      ← 择时信号看到的
    方向相反。两者日收益相关系数只有 0.71，近一年累计差 12 个百分点。

    也就是说**旧代码在用组合 B 的信号去择时组合 A** —— 而 QBG_MARKET_SMA
    那张压测表，本身就是在这个错信号上选出来的。

    停牌日 ffill 后当日收益记 0；尚未上市的票当日不参与平均（NaN 跳过），
    所以成分随时间变化不会在曲线上留下跳变。
    """
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
    # skipna=True：当天还没上市的票不拉低平均。全是 NaN 的日子记 0。
    daily = prices.pct_change().mean(axis=1, skipna=True).fillna(0.0)
    return (1.0 + daily).cumprod().rename("market")


def market_risk_on(codes_list: list[str], sma_window: int | None = None,
                   root: Path | None = None) -> bool:
    """读取缓存并报告当前市场状态。"""
    window = settings.qbg_market_sma if sma_window is None else sma_window
    return is_risk_on(equal_weight_index(codes_list, root), window)

