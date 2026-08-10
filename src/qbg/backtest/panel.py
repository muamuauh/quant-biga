"""从 parquet 缓存构建回测面板。

把"每股一个 parquet"转成回测要的 `date × instrument` 宽表，并**在这里
算出涨跌停标志**——这是唯一同时拿得到不复权价和板块信息的地方。

## 涨跌停必须用不复权价算

交易所的涨跌停是按**前收盘价（不复权）**算的。用后复权价算出来的涨跌停
数值看起来总是"合理"的，但和真实盘口对不上——这是个很隐蔽的错误，因为
它不会产生任何异常值，只是把某些日子的可交易性判断反了。

所以这里的分工是：
  · `open_px` / `close_px` → **后复权**（价格连续，收益率正确）
  · 涨跌停判断 → **不复权**（交易所口径）
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from qbg.backtest.engine import Panel
from qbg.data import cache
from qbg.market import rules
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

# 判断"开盘即涨停"的容差。交易所按分定价，用 0.5 分作阈值可以吸收
# 浮点误差，又不会把差一分钱的情况误判成涨停。
_TOL = 0.005


def build_panel(
    codes_list: list[str],
    start: str | None = None,
    end: str | None = None,
    parquet_root: Path | None = None,
) -> Panel:
    """读缓存 → 拼成 `Panel`。

    数据不足的票会被剔除并记日志，而不是留一列全 NaN——一列 NaN 会让
    截面排序悄悄少一只票，而不会有任何提示。
    """
    opens, closes, susp, lim_up, lim_dn = {}, {}, {}, {}, {}
    skipped = []

    for code in codes_list:
        df = cache.read(code, parquet_root)
        if df.empty:
            skipped.append(code)
            continue
        if start is not None:
            df = df[df["date"] >= pd.Timestamp(start)]
        if end is not None:
            df = df[df["date"] <= pd.Timestamp(end)]
        if df.empty:
            skipped.append(code)
            continue

        df = df.set_index("date")
        factor = df["factor"]

        # 后复权：给收益率计算用
        opens[code] = df["open"] * factor
        closes[code] = df["close"] * factor

        susp[code] = df["is_suspended"]
        up, dn = _limit_flags(df, code)
        lim_up[code] = up
        lim_dn[code] = dn

    if skipped:
        log_event(log, "panel.skipped_codes", count=len(skipped), sample=skipped[:10])

    if not opens:
        empty = pd.DataFrame()
        return Panel(empty, empty, empty, empty, empty)

    panel = Panel(
        open_px=pd.DataFrame(opens).sort_index(),
        close_px=pd.DataFrame(closes).sort_index(),
        # 缺失的日子按"停牌"处理：某只票在某天没有行情行，最可能就是它
        # 当时还没上市或已停牌，按不可交易处理是安全的一侧。
        suspended=_bool_frame(susp, default=True),
        limit_up_open=_bool_frame(lim_up, default=False),
        limit_down_open=_bool_frame(lim_dn, default=False),
    )
    log_event(log, "panel.built",
              instruments=len(panel.instruments), days=len(panel.dates),
              start=str(panel.dates[0].date()), end=str(panel.dates[-1].date()))
    return panel


def _bool_frame(cols: dict[str, pd.Series], default: bool) -> pd.DataFrame:
    """把各股的布尔序列拼成宽表，缺失填 `default`。

    不用 `.fillna(d).astype(bool)`：不同股票的日期索引不一致，对齐后产生
    的 NaN 会让整列变成 object dtype，pandas 3 对 object 列的 fillna 会
    发降级警告。逐列显式转换更啰嗦但没有歧义。
    """
    df = pd.DataFrame(cols).sort_index()
    for c in df.columns:
        df[c] = df[c].map(lambda v: default if pd.isna(v) else bool(v)).astype(bool)
    return df


def _limit_flags(df: pd.DataFrame, code: str) -> tuple[pd.Series, pd.Series]:
    """按**不复权**价判断开盘是否一字涨停/跌停。

    首日没有前收盘价，一律判为不涨跌停——保守的一侧（宁可让回测以为
    能交易，也不要凭空造出一个"买不进"的限制）。
    """
    prev_close = df["close"].shift(1)
    open_raw = df["open"]
    is_st = df["is_st"].astype(bool)

    up = pd.Series(False, index=df.index)
    dn = pd.Series(False, index=df.index)

    valid = prev_close.notna() & (prev_close > 0) & open_raw.notna() & (open_raw > 0)
    for ts in df.index[valid]:
        pc = float(prev_close.loc[ts])
        lo, hi = rules.price_limits(code, pc, bool(is_st.loc[ts]))
        op = float(open_raw.loc[ts])
        up.loc[ts] = op >= hi - _TOL
        dn.loc[ts] = op <= lo + _TOL

    return up, dn


def scores_from_frame(df: pd.DataFrame) -> pd.DataFrame:
    """把 `date × instrument` 的任意打分表规整成回测能用的形状。

    P3 接上 qlib 后，模型预测会经过这里；在那之前它让手造的分数也能直接
    喂进回测，用来验证引擎本身。
    """
    return df.sort_index().astype(float)
