"""沪深300（或股票池等权代理）相对移动均线的市场状态过滤。"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from qbg.config import settings
from qbg.data import cache


def risk_on_series(level: pd.Series, sma_window: int, band: float = 0.0) -> pd.Series:
    """返回每个收盘日可得到的风险状态；历史不足时默认 risk-on。

    `band` 是**迟滞缓冲**（小数，0 = 关闭，等同旧行为）：

        当前 risk-on  → 跌破 `sma × (1 - band)` 才转 off
        当前 risk-off → 升过 `sma × (1 + band)` 才转 on

    ## 为什么需要它

    裸的 `level >= sma` 在指数贴着均线走的时候会疯狂抖动。本机 2026-09-08
    实测：1620 个交易日里有 **27 段** risk-off，中位只有 **5 天** —— 那不是
    在躲熊市，那是在均线上蹭来蹭去。每翻一次要全仓清掉再买回来，一个来回
    约 10bp 基础费率 + 双边滑点，而且清仓期间的上涨全部错过。

    典型片段（2026-08-27 ~ 09-02，偏离 = 指数/SMA100 - 1）：

        -0.11% off → -0.25% off → +0.02% ON → +0.10% ON → -1.23% off

    两天的 risk-on 窗口，付两次翻转成本，什么也没躲掉。

    ## 为什么是路径依赖的迟滞，不是一条固定的偏移线

    把阈值整体下移（`level >= sma * 0.99`）只是把抖动搬到低 1% 的位置，
    抖动次数不变，还顺手削掉了保护。迟滞是**开关两个方向用不同的线**，
    中间那条带子里维持现状 —— 这才是真正减少翻转次数的东西。
    和选股那边的 `keep_rank` 是同一个思路。
    """
    level = level.astype(float).sort_index()
    if sma_window <= 0:
        return pd.Series(True, index=level.index, dtype=bool)
    sma = level.rolling(sma_window, min_periods=sma_window).mean()
    if band <= 0:
        return (level >= sma).where(sma.notna(), True).astype(bool)

    # 迟滞是路径依赖的：今天的状态取决于昨天的状态，没法向量化成一次比较。
    upper = sma * (1.0 + band)
    lower = sma * (1.0 - band)
    state = True          # 历史不足时默认 risk-on，和无 band 分支一致
    out = []
    for lv, up, lo, has_sma in zip(level, upper, lower, sma.notna(), strict=False):
        if not has_sma:
            out.append(True)
            continue
        if state and lv < lo:
            state = False
        elif not state and lv > up:
            state = True
        out.append(state)
    return pd.Series(out, index=level.index, dtype=bool)


def is_risk_on(level: pd.Series, sma_window: int, band: float = 0.0) -> bool:
    """最新收盘的状态；数据不足时不误清仓。

    `band` > 0 时结果**依赖整段历史**（迟滞是路径依赖的），所以这里必须把
    完整序列算完再取最后一个，不能只比最新一天和均线。
    """
    clean = level.dropna().sort_index()
    return True if clean.empty else bool(risk_on_series(clean, sma_window, band).iloc[-1])


def equal_weight_index(codes_list: list[str], root: Path | None = None,
                       price_column: str = "close",
                       eligible: pd.DataFrame | None = None,
                       use_inclusion: bool = False) -> pd.Series:
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

    `eligible`（`date × code` 布尔表，见 `data.universe.eligibility_mask`）传进来
    时，只有当天**确实在指数里**的票参与平均。`use_inclusion=True` 是它的便利
    形式：自己去查纳入日期建表（生产链路用这条，因为那里只有代码表没有面板）。
    两者都给时 `eligible` 优先。不传就是老口径：拿今天的成分
    回看历史，于是 2020 年的曲线里混着 2026 年才被纳入的票 —— 而它们被纳入
    恰恰因为这几年涨得好。**择时信号会因此看到一条比真实市场更强的曲线**，
    SMA 的穿越点也就跟着偏。QBG_MARKET_SMA 那张压测表是在老口径上选出来的。
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
    if eligible is None and use_inclusion:
        # 生产链路用这条：只知道 codes_list，日期索引要读完缓存才有，所以
        # 在这里就地建表，而不是让调用方先算一遍指数拿索引（那要多读一遍
        # 299 个 parquet，日流程里是几十秒）。
        from qbg.data.universe import eligibility_mask

        eligible = eligibility_mask(prices.index, prices.columns)
    changes = prices.pct_change()
    if eligible is not None:
        # 先算收益再屏蔽，不能先屏蔽价格 —— 后者会让"刚被纳入"那天冒出一个
        # 从 NaN 到真实价的假跳变。
        changes = changes.where(eligible.reindex(index=changes.index,
                                                 columns=changes.columns).fillna(False))
    # skipna=True：当天还没上市的票不拉低平均。全是 NaN 的日子记 0。
    daily = changes.mean(axis=1, skipna=True).fillna(0.0)
    return (1.0 + daily).cumprod().rename("market")


def market_risk_on(codes_list: list[str], sma_window: int | None = None,
                   root: Path | None = None, band: float | None = None,
                   use_inclusion: bool | None = None) -> bool:
    """读取缓存并报告当前市场状态。"""
    window = settings.qbg_market_sma if sma_window is None else sma_window
    width = settings.qbg_market_sma_band if band is None else band
    incl = (bool(settings.qbg_market_index_eligible) if use_inclusion is None
            else use_inclusion)
    level = equal_weight_index(codes_list, root, use_inclusion=incl)
    return is_risk_on(level, window, width)

