"""回测指标。纯函数、无 IO、完全可单测。

移植自 quant-trading/src/qtf/backtest/metrics.py（与市场无关，几乎原样）。

所有收益率序列都是**日简单收益**（0.012 = 当天 +1.2%），按日期索引。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

# A股一年约 242~245 个交易日，取 244 作为年化基数。
# 美股版用 252，直接抄过来会让年化收益高估约 3%——夏普和 Calmar 也跟着错。
TRADING_DAYS = 244

# 低于这个日波动就当作零波动（见 compute_metrics 里的说明）。
_VOL_EPS = 1e-12


@dataclass
class BacktestMetrics:
    n_days: int
    total_return: float          # 累计收益，小数（0.25 = +25%）
    annual_return: float         # 年化（CAGR）
    annual_vol: float            # 年化波动率
    sharpe: float                # 年化夏普，rf=0
    max_drawdown: float          # 最大回撤，负数
    calmar: float                # annual_return / |max_drawdown|
    win_rate: float              # 上涨日占比
    best_day: float
    worst_day: float
    excess_annual_return: float  # 相对基准的超额年化

    def as_dict(self) -> dict:
        return asdict(self)


def equity_curve(daily_returns: pd.Series) -> pd.Series:
    """日收益复利成净值曲线，起点 1.0。"""
    return (1.0 + daily_returns.fillna(0.0)).cumprod()


def max_drawdown(curve: pd.Series) -> float:
    """净值曲线的最大峰谷回撤。返回 <= 0。"""
    if curve.empty:
        return 0.0
    running_peak = curve.cummax()
    return float((curve / running_peak - 1.0).min())


def _annualize(curve: pd.Series, n_days: int) -> float:
    if n_days <= 0:
        return 0.0
    years = n_days / TRADING_DAYS
    final = float(curve.iloc[-1])
    if years <= 0 or final <= 0:
        # 净值归零时 CAGR 数学上是 -100%，直接报出来而不是留个 NaN。
        return -1.0 if final <= 0 else 0.0
    return float(final ** (1.0 / years) - 1.0)


def compute_metrics(
    daily_returns: pd.Series,
    benchmark_returns: pd.Series | None = None,
) -> BacktestMetrics:
    """从日收益序列算出标准指标集。"""
    r = daily_returns.dropna()
    n = len(r)
    if n == 0:
        return BacktestMetrics(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    curve = equity_curve(r)
    total = float(curve.iloc[-1] - 1.0)
    annual = _annualize(curve, n)
    std = float(r.std(ddof=1)) if n > 1 else 0.0
    vol = std * np.sqrt(TRADING_DAYS)
    # 阈值而不是 `std > 0`：常数序列的样本标准差是 1e-19 级的浮点残渣，
    # 不是 0，除下去会得到 3.6e16 这种荒谬的夏普。日收益量级在 1e-2~1e-3，
    # 1e-12 安全地低于任何真实波动。
    sharpe = float(r.mean() / std * np.sqrt(TRADING_DAYS)) if std > _VOL_EPS else 0.0
    mdd = max_drawdown(curve)
    calmar = float(annual / abs(mdd)) if mdd < 0 else 0.0
    up, down = int((r > 0).sum()), int((r < 0).sum())
    win = up / (up + down) if (up + down) else 0.0

    excess = 0.0
    if benchmark_returns is not None:
        b = benchmark_returns.dropna()
        if len(b) > 0:
            excess = annual - _annualize(equity_curve(b), len(b))

    return BacktestMetrics(
        n_days=n,
        total_return=total,
        annual_return=annual,
        annual_vol=vol,
        sharpe=sharpe,
        max_drawdown=mdd,
        calmar=calmar,
        win_rate=win,
        best_day=float(r.max()),
        worst_day=float(r.min()),
        excess_annual_return=excess,
    )


def _pearson(x: pd.Series, y: pd.Series) -> float:
    """手写皮尔逊相关。

    绕开 `Series.corr` → `numpy.corrcoef`：quant-trading 在同类 conda
    numpy/MKL 构建上遇到过 Windows 原生崩溃（0xc06d007f）。手写版没有
    这个风险，代价只有几行代码。
    """
    xc, yc = x - x.mean(), y - y.mean()
    denom = float(np.sqrt((xc**2).sum()) * np.sqrt((yc**2).sum()))
    if denom == 0.0:
        return float("nan")
    return float((xc * yc).sum() / denom)


def information_coefficient(
    predictions: pd.Series,
    forward_returns: pd.Series,
) -> tuple[float, float]:
    """日频截面 IC（皮尔逊）和 Rank IC（斯皮尔曼）的均值。

    两个序列共享 `(datetime, instrument)` 的 MultiIndex。每天在截面上把
    预测和实际前瞻收益做相关，再对每日相关系数取平均。

    **Rank IC 是横截面选股的核心指标**：它只关心排序对不对，不受个别
    极端值影响，而我们的策略本来就只用排序（top-K）。
    """
    df = pd.DataFrame({"pred": predictions, "fwd": forward_returns}).dropna()
    if df.empty:
        return 0.0, 0.0

    ics, rank_ics = [], []
    for _, day in df.groupby(level="datetime"):
        # 截面上少于 3 只票算不出有意义的相关，跳过。
        if len(day) < 3:
            continue
        ic = _pearson(day["pred"], day["fwd"])
        ric = _pearson(day["pred"].rank(), day["fwd"].rank())   # 秩相关
        if pd.notna(ic):
            ics.append(ic)
        if pd.notna(ric):
            rank_ics.append(ric)

    return (float(np.mean(ics)) if ics else 0.0,
            float(np.mean(rank_ics)) if rank_ics else 0.0)


def turnover(weights: pd.DataFrame) -> pd.Series:
    """每日换手率：权重变动绝对值之和的一半。

    除以 2 是因为一次调仓同时产生买和卖，不除会把换手算成两倍。
    这个数直接乘以往返成本率就是当天的成本，所以口径必须和费率模型对齐。
    """
    if weights.empty:
        return pd.Series(dtype=float)
    return weights.diff().abs().sum(axis=1).fillna(0.0) / 2.0
