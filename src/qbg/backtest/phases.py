"""相位平均：调仓间隔 > 1 的回测必须把每个相位都跑一遍再平均。

## 为什么

`rebalance_every=10` 时，相位 0 在第 0/10/20… 天调仓，相位 1 在第 1/11/21… 天，
十组日子几乎不重叠。实测 k=3、每 10 日的**相位极差 86 个百分点年化** —— 只跑
一个相位，比的是"哪几天交易"的运气，不是参数。日频只有一个相位，从不受影响，
所以这个偏差**单向打在低频那一侧**。

## 为什么"每个指标各自取平均"，而不是"先把日收益平均再算指标"

后者是十个相位组合的等权混合，混合本身会分散掉波动、压低回撤 —— 等于凭空
给低频策略加了一层实盘里不存在的分散化。实盘只会落在**某一个**相位上，所以
正确的问题是"随机落在一个相位上，期望表现是多少"，也就是各相位指标的均值。

子区间同理：每个相位各自切段算年化，再按段取平均。

## 2026-09-14 的教训

此前 `12_rebalance_gate.py` 里的平均只覆盖了头条指标，`daily_returns` 通过
`__getattr__` 落到了第 0 个相位 —— 于是子区间那一闸**仍然只看相位 0**。
这个模块把"所有要喂给闸的数"都显式按相位平均，不留兜底属性。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from qbg.backtest import engine
from qbg.backtest.metrics import TRADING_DAYS


def _annual(returns) -> float:
    returns = np.asarray(returns, dtype=float)
    if returns.size == 0:
        return 0.0
    growth = float(np.prod(1.0 + returns))
    return growth ** (TRADING_DAYS / returns.size) - 1.0 if growth > 0 else -1.0


@dataclass
class PhaseResult:
    runs: list

    def _mean(self, getter) -> float:
        return float(np.mean([getter(r) for r in self.runs]))

    @property
    def annual_return(self) -> float:
        return self._mean(lambda r: r.strategy.annual_return)

    @property
    def sharpe(self) -> float:
        return self._mean(lambda r: r.strategy.sharpe)

    @property
    def max_drawdown(self) -> float:
        return self._mean(lambda r: r.strategy.max_drawdown)

    @property
    def avg_turnover(self) -> float:
        return self._mean(lambda r: r.avg_turnover)

    @property
    def rank_ic(self) -> float:
        return self._mean(lambda r: r.rank_ic)

    @property
    def trailing_exits(self) -> float:
        return self._mean(lambda r: r.trailing_exits)

    @property
    def phase_spread(self) -> float:
        """最好和最差相位的年化之差。大 = 结果主要由"从哪天开始"决定。"""
        values = [r.strategy.annual_return for r in self.runs]
        return float(max(values) - min(values))

    def facts(self) -> dict:
        """喂给 `tuning.gates.evaluate` 的那组数。"""
        return {"annual_return": self.annual_return, "sharpe": self.sharpe,
                "max_drawdown": self.max_drawdown, "avg_turnover": self.avg_turnover,
                "rank_ic": self.rank_ic}

    def subperiod_annual(self, n: int) -> list[float]:
        """切 n 段，每段在每个相位上各算年化，再按段取相位均值。"""
        per_phase = []
        for run in self.runs:
            daily = run.daily_returns.to_numpy()
            per_phase.append([_annual(chunk) for chunk in np.array_split(daily, n)])
        return [float(v) for v in np.mean(np.array(per_phase), axis=0)]

    def subperiod_spans(self, n: int) -> list[tuple]:
        index = self.runs[0].daily_returns.index
        return [(index[idx[0]], index[idx[-1]])
                for idx in np.array_split(np.arange(len(index)), n) if len(idx)]


def run_phases(scores, panel, *, rebalance_every: int, **kwargs) -> PhaseResult:
    """每个相位跑一次。`rebalance_every <= 1` 时只有一个相位。"""
    every = max(1, int(rebalance_every))
    kwargs.setdefault("slippage_grid", ())
    return PhaseResult([engine.run_backtest(scores, panel, rebalance_every=every,
                                            rebalance_phase=phase, **kwargs)
                        for phase in range(every)])
