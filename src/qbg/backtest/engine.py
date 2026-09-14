"""向量化 top-K 日频回测，带四条 A股约束。

框架移植自 quant-trading/src/qtf/backtest/engine.py，但**成交假设和交易
约束全部重写**——美股版直接套到 A股上，回测数字会是假的。

## 时序：为什么用开盘价而不是收盘价

半自动模式的真实时序是：

    T 日盘后（17:30 数据可用）跑流水线出清单
      → T+1 开盘后你在 APP 里照单手动执行
        → 持有到 T+2 开盘再调

中间隔着一个**隔夜跳空**。用 T 日收盘价成交等于假设你能在信号产生的
同一刻交易，会把跳空那段收益白送给策略。所以：

    w[t]   = 由 t-1 日收盘后的分数决定的目标权重，在 t 日**开盘**建仓
    ret[t] = open[t+1] / open[t] - 1     ← 开盘到开盘

## 四条 A股约束

**1. T+1** —— 在"每日开盘调一次仓"的模型里是**结构性满足**的：t 日开盘
买入的票，最早也是 t+1 日开盘才可能卖出。（A股的 T+1 只约束股票，卖出
资金当日可继续买入，所以现金侧无需建模。）引擎里有断言把这个不变式钉住，
将来若加入日内止损，约束必须显式实现。

**2. 涨跌停** —— 这条需要**区分"目标持仓"和"实际持仓"**，是引擎里最实质
的改动。开盘一字涨停就买不进，开盘跌停就卖不出。买不进的钱留在现金里
（不重新分配给别的票——真实情况就是那笔钱没投出去）。

**3. 停牌** —— 不能交易，权重冻结，当日收益按 0 计。

**4. 不对称费率** —— 卖出多 5bp 印花税。买卖换手分开乘各自的费率，
不能用一个对称的 `cost_per_turnover`。

## 保留的东西

滑点敏感性曲线（`slippage_curve`）：在基础费率之上再叠 0/10/20/30bp，
看策略还剩多少。A股散户限价单的滑点大致在这个量级，头条数字只报无滑点
版本会严重误导。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from qbg.backtest.metrics import (
    BacktestMetrics,
    compute_metrics,
    equity_curve,
    information_coefficient,
)
from qbg.execution import fees
from qbg.strategy import topk_weights
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)


@dataclass
class Panel:
    """回测需要的全部面板数据。全部是 `date × instrument` 的宽表。

    `open_px` / `close_px` 用**后复权价**（价格连续，收益率正确）。
    `prev_close_raw` 用**不复权**收盘价——涨跌停是交易所按原始价算的，
    用复权价算出来的涨跌停会和真实盘口对不上，而且错得很安静。
    """

    open_px: pd.DataFrame
    close_px: pd.DataFrame
    suspended: pd.DataFrame        # bool
    limit_up_open: pd.DataFrame    # bool：开盘即涨停 → 买不进
    limit_down_open: pd.DataFrame  # bool：开盘即跌停 → 卖不出

    @property
    def dates(self) -> pd.Index:
        return self.open_px.index

    @property
    def instruments(self) -> pd.Index:
        return self.open_px.columns

    def open_to_open_returns(self) -> pd.DataFrame:
        """`open[t+1] / open[t] - 1`，最后一行是 NaN（没有下一天）。

        停牌日强制为 0：停牌期间价格不动，持仓价值不变。不处理的话，
        复牌当天的巨大跳空会被摊到停牌那一天，凭空造出一根大阳线。
        """
        ret = self.open_px.shift(-1) / self.open_px - 1.0
        return ret.mask(self.suspended, 0.0)


@dataclass
class BacktestResult:
    strategy: BacktestMetrics
    benchmark: BacktestMetrics
    ic: float
    rank_ic: float
    k: int
    strategy_curve: pd.Series
    benchmark_curve: pd.Series
    daily_returns: pd.Series
    weights: pd.DataFrame
    avg_turnover: float
    # 因涨跌停/停牌而没能执行的调仓次数，按类型分。
    # **这个数大说明回测结论不可信**——它意味着策略想做的事有很大一部分
    # 在真实市场里做不到。
    blocked: dict[str, int] = field(default_factory=dict)
    # 额外滑点（小数）→ 指标。头条数字是无额外滑点的版本。
    slippage_curve: dict[float, BacktestMetrics] = field(default_factory=dict)
    buy_cost_rate: float = 0.0
    sell_cost_rate: float = 0.0
    # 移动止盈**实际执行了**的清仓次数（被跌停拦下的不算）。
    # 这个数必须和收益一起看：quant-trading 2026-08-04 那次回测里，"不变差"的
    # 档位全都是**零触发** —— 它们没有变好，只是什么都没做。
    trailing_exits: int = 0

    def as_dict(self) -> dict:
        return {
            "strategy": self.strategy.as_dict(),
            "benchmark": self.benchmark.as_dict(),
            "ic": self.ic,
            "rank_ic": self.rank_ic,
            "k": self.k,
            "avg_turnover": self.avg_turnover,
            "blocked": self.blocked,
            "buy_cost_rate": self.buy_cost_rate,
            "sell_cost_rate": self.sell_cost_rate,
            "slippage_curve": {str(k): v.as_dict() for k, v in self.slippage_curve.items()},
        }


def target_weights_from_scores(
    scores: pd.DataFrame,
    k: int,
    total_weight: float = 0.95,
    tradable: pd.DataFrame | None = None,
    fixed_slots: bool = False,
) -> pd.DataFrame:
    """每日分数 → 等权 top-K 目标权重。

    `scores` 是 `date × instrument`。**信号在 t 日收盘后产生，t+1 日开盘
    执行**，所以这里对结果 shift(1)——不 shift 就是用当天的信息在当天开盘
    交易，一个典型的前视偏差。

    `tradable` 为 False 的票不参与选择：停牌股排进 top-K 只会白占一个槽位。

    `fixed_slots` 决定**候选不足 k 只时那笔钱去哪**：

      * `False`（默认，排序型打分）：按实际选中数归一，仓位摊满。模型分数
        每天都有几百个非空值，选中数恒等于 k，这一分支的行为和固定槽位一样。
      * `True`（阈值型打分）：按 k 归一，没填满的槽位**留成现金**。
        「3日涨幅>5%」这类条件在某些日子只有一只票合格，按实际数归一会把
        95% 仓位压到那一只上 —— 而真实下单流程（`execution/order_planner`）
        是按固定槽位预算下的单，槽位空着就是现金，没有任何环节会做那件事。
        不区分的话，阈值型策略的回测收益里会混进一个它实盘拿不到的杠杆。
    """
    if scores.empty or k <= 0:
        return pd.DataFrame(index=scores.index, columns=scores.columns, dtype=float)

    usable = scores.where(tradable) if tradable is not None else scores
    # rank(ascending=False) → 1 是最好的
    ranks = usable.rank(axis=1, ascending=False, method="first")
    picked = (ranks <= k) & usable.notna()
    n_picked = picked.sum(axis=1).replace(0, np.nan)
    denom = float(k) if fixed_slots else n_picked
    weights = picked.astype(float).div(denom, axis=0) * total_weight
    # 关键的一步 shift：t 日的分数决定 t+1 日开盘的持仓。
    return weights.shift(1).fillna(0.0)


def _hysteresis_target(
    row: pd.Series,
    held: set,
    k: int,
    keep_rank: int,
    total_weight: float,
    fixed_slots: bool,
) -> pd.Series:
    """迟滞选股的当日目标权重：持仓股还在前 `keep_rank` 名内就留着。

    这一步**必须在循环里**做，因为选谁取决于当天持有谁——路径依赖，没法
    像普通 top-K 那样预先算成一张表。引擎此前表达不了 `QBG_KEEP_RANK`，
    于是实盘有这个参数、回测却验不了它，和 `rebalance_every` 当初是同一个洞。

    排序用 `rank(method="first")` 而不是 `sort_values`：要和非迟滞路径的
    并列处理**逐位一致**，否则两条路径在有并列分数时会选出不同的票，
    而这种差异只在特定数据上出现，最难查。
    """
    ranks = row.rank(ascending=False, method="first")
    ordered = ranks.dropna().sort_values().index.tolist()
    selected = topk_weights.select_with_hysteresis(ordered, held, k, keep_rank)

    w = pd.Series(0.0, index=row.index)
    if selected:
        denom = float(k) if fixed_slots else float(len(selected))
        w.loc[selected] = total_weight / denom
    return w


def _step_weights(
    w_prev: pd.Series,
    w_target: pd.Series,
    suspended: pd.Series,
    limit_up: pd.Series,
    limit_down: pd.Series,
) -> tuple[pd.Series, dict[str, int]]:
    """一天的调仓，受 A股可交易性约束。

    返回 `(实际权重, 被拦下的次数)`。被拦下的仓位**保持原样**，腾不出来
    的钱留在现金里——不重新分配给别的票，因为真实情况就是那笔钱没投出去。
    """
    w_new = w_target.copy()
    blocked = {"suspended": 0, "limit_up_cannot_buy": 0, "limit_down_cannot_sell": 0}

    want_buy = w_target > w_prev + 1e-12
    want_sell = w_target < w_prev - 1e-12

    # 停牌：什么都做不了
    hit = suspended & (want_buy | want_sell)
    blocked["suspended"] = int(hit.sum())
    w_new = w_new.mask(suspended, w_prev)

    # 一字涨停：买不进
    hit = (~suspended) & want_buy & limit_up
    blocked["limit_up_cannot_buy"] = int(hit.sum())
    w_new = w_new.mask(hit, w_prev)

    # 一字跌停：卖不出，被迫持有
    hit = (~suspended) & want_sell & limit_down
    blocked["limit_down_cannot_sell"] = int(hit.sum())
    w_new = w_new.mask(hit, w_prev)

    return w_new, blocked


def run_backtest(
    scores: pd.DataFrame,
    panel: Panel,
    *,
    k: int = 3,
    total_weight: float = 0.95,
    rebalance_every: int = 1,
    rebalance_phase: int = 0,
    keep_rank: int = 0,
    extra_slippage: float = 0.0,
    fee_profile: fees.FeeProfile | None = None,
    fixed_slots: bool = False,
    slippage_grid: tuple[float, ...] = (0.0, 0.001, 0.002, 0.003),
    trail_arm: float = 0.0,
    trail_pct: float = 0.0,
) -> BacktestResult:
    """跑一次 top-K 回测。

    `rebalance_every` 是**调仓间隔（交易日）**，对应实盘的
    `QBG_REBALANCE_EVERY_DAYS`。默认 1 = 每日调仓。非调仓日完全不碰组合 ——
    这是引擎此前根本表达不了的一件事：实盘有这个参数，而回测只会每天调，
    于是任何关于调仓频率的结论都无从验证。

    `rebalance_phase` 决定**在哪些天调仓**：`every=3` 时相位 0 用第 0/3/6… 天，
    相位 1 用第 1/4/7… 天。这几组日子几乎不重叠，所以固定相位会把"持有期"
    和"碰巧在哪些天下的单"混在一起 —— 在 k 小、窗口短的时候，后者能贡献
    上百个百分点的年化差异。**比较调仓频率时必须扫遍 0..every-1 全部相位
    再取平均**，相位之间的离散度本身就是结论：散得厉害说明单相位的数字
    没有信息量。

    `keep_rank` 是**迟滞选股**的保留名次，对应实盘的 `QBG_KEEP_RANK`：
    持仓股只要还在当日前 `keep_rank` 名内就不换人。`0` = 关闭。这条同样是
    实盘有、回测原先验不了的参数；而它恰恰是**唯一能在不改信号的前提下压
    换手**的杠杆（quant-trading 实测把日均换手 0.745 压到 0.524）。

    基准是**股票池等权买入持有**——回答"这个模型比无脑等权持有强在哪"，
    比拿沪深300 指数当基准更能隔离出选股能力（指数是市值加权，混进了
    市值因子的影响）。
    """
    profile = fee_profile or fees.FeeProfile.load()
    buy_rate = fees.cost_rate("BUY", profile)
    sell_rate = fees.cost_rate("SELL", profile)

    scores = scores.reindex(index=panel.dates, columns=panel.instruments)
    tradable = ~panel.suspended
    w_target = target_weights_from_scores(scores, k, total_weight, tradable, fixed_slots)
    # 迟滞要在循环里逐日选股（见 `_hysteresis_target`）。这里预先把"当日可交易 +
    # 尚未 shift"的分数备好，和 `target_weights_from_scores` 里的口径完全一致：
    # 按**信号日**的可交易性过滤，再由次日开盘执行。
    ranking_scores = scores.where(tradable) if keep_rank > 0 else None
    asset_ret = panel.open_to_open_returns()

    daily_returns, weight_rows, traded_turnover = [], [], []
    blocked_total = {"suspended": 0, "limit_up_cannot_buy": 0, "limit_down_cannot_sell": 0}
    w_prev = pd.Series(0.0, index=panel.instruments)

    # --- 移动止盈 ---------------------------------------------------------
    # 和实盘 `qbg.risk.trailing` 同一套判据：浮盈峰值先达到 `trail_arm` 才"上膛"，
    # 之后从峰值回撤 `trail_pct` 就整仓卖出。**只在非调仓日触发** —— 调仓日交给
    # 新一轮选股决定，不去砍一只模型仍排在前 k 的票。
    #
    # 用"建仓以来累计净值"代替成本价：新建仓从 1.0 起算，每天乘 (1+r)。
    # 近似之处：调仓日同一只票加减仓时，实盘的成本价会被摊薄，这里不摊薄、
    # 继续从最初建仓算。影响只在"持有期里被加过仓"的票上。
    trailing_on = trail_arm > 0 and trail_pct > 0
    grow = pd.Series(0.0, index=panel.instruments)
    peak = pd.Series(0.0, index=panel.instruments)
    trailing_exits = 0

    # 最后一天没有 open[t+1]，无法形成一个完整的持有期，所以不进循环。
    for index, day in enumerate(panel.dates[:-1]):
        # 非调仓日**什么都不做**：目标就是当前（已漂移的）持仓，delta 为零。
        #
        # 光把分数 ffill 是不够的 —— 目标权重不变，但 `w_prev` 每天按涨跌漂移
        # （见循环末尾），引擎会把它拉回目标，于是变成"每天调仓到一个过期目标"，
        # 恰恰是 rebalance_every 要避免的那件事。真实行为是这几天完全不碰组合。
        #
        # 注意这里没有实现实盘的 `rebalance_drift_band`（漂移小于 3% 就不动）：
        # 调仓日这里会走满到目标。差别只在调仓日的换手上，方向是**高估**换手，
        # 也就是对低频那一侧不利 —— 结论若仍偏向低频，那是保守的。
        is_rebalance_day = (rebalance_every <= 1
                            or index % rebalance_every == rebalance_phase % rebalance_every)
        if not is_rebalance_day:
            target = w_prev
            if trailing_on:
                armed = (w_prev > 0) & (peak - 1.0 >= trail_arm)
                fired = armed & (grow <= peak * (1.0 - trail_pct))
                if fired.any():
                    target = w_prev.mask(fired, 0.0)
        elif ranking_scores is None:
            target = w_target.loc[day]
        elif index == 0:
            target = pd.Series(0.0, index=panel.instruments)  # 没有前一日分数
        else:
            target = _hysteresis_target(
                ranking_scores.iloc[index - 1], set(w_prev.index[w_prev > 0]),
                k, keep_rank, total_weight, fixed_slots)
        w_new, blocked = _step_weights(
            w_prev, target,
            panel.suspended.loc[day],
            panel.limit_up_open.loc[day],
            panel.limit_down_open.loc[day],
        )
        for key, val in blocked.items():
            blocked_total[key] += val
        if trailing_on and not is_rebalance_day:
            # 非调仓日唯一能让持仓归零的就是移动止盈；被跌停拦下的仍 > 0，不计。
            trailing_exits += int(((w_prev > 0) & (w_new <= 0)).sum())

        delta = w_new - w_prev
        buy_turnover = float(delta.clip(lower=0).sum())
        sell_turnover = float((-delta).clip(lower=0).sum())
        # **换手要按真正成交的量算，不能用相邻两天的权重差。**
        # `w_prev` 已经含了当天的价格漂移（见循环末尾），所以
        # `weights.diff()` 度量的是「漂移 + 交易」。日频调仓时漂移占比小、
        # 看不出来；但调仓间隔一拉长，漂移就成了主要成分 —— 于是低频策略
        # 会被报出一个它根本没付的换手，正好在比较调仓频率时误导最大。
        traded_turnover.append((buy_turnover + sell_turnover) / 2.0)
        # 费率不对称：卖出多 5bp 印花税。滑点两边都吃。
        cost = (buy_turnover * (buy_rate + extra_slippage)
                + sell_turnover * (sell_rate + extra_slippage))

        r = asset_ret.loc[day].fillna(0.0)
        gross = float((w_new * r).sum())
        daily_returns.append(gross - cost)
        weight_rows.append(w_new.rename(day))

        # 权重按各自涨跌漂移到次日开盘。现金部分收益为 0。
        w_prev = w_new * (1.0 + r)

        if trailing_on:
            held_now = w_new > 0
            entered = held_now & (grow <= 0)
            grow = grow.mask(entered, 1.0).where(held_now, 0.0) * (1.0 + r)
            peak = peak.mask(entered, 1.0).where(held_now, 0.0).clip(lower=grow)

    dates = panel.dates[:-1]
    ret_s = pd.Series(daily_returns, index=dates, name="strategy")
    weights = pd.DataFrame(weight_rows) if weight_rows else pd.DataFrame()

    # 基准：股票池等权买入持有（停牌收益已在 open_to_open_returns 里置 0）
    bench_s = asset_ret.loc[dates].mean(axis=1).fillna(0.0).rename("benchmark")

    ic, rank_ic = _compute_ic(scores, asset_ret)
    turn = pd.Series(traded_turnover, index=dates, name="turnover")

    slippage_curve = {}
    for slip in slippage_grid:
        if slip == extra_slippage:
            slippage_curve[slip] = compute_metrics(ret_s, bench_s)
        else:
            slippage_curve[slip] = _rerun_with_slippage(
                weights, asset_ret, dates, buy_rate, sell_rate, slip, bench_s
            )

    result = BacktestResult(
        strategy=compute_metrics(ret_s, bench_s),
        benchmark=compute_metrics(bench_s),
        ic=ic, rank_ic=rank_ic, k=k,
        strategy_curve=equity_curve(ret_s),
        benchmark_curve=equity_curve(bench_s),
        daily_returns=ret_s,
        weights=weights,
        avg_turnover=float(turn.mean()) if len(turn) else 0.0,
        blocked=blocked_total,
        slippage_curve=slippage_curve,
        buy_cost_rate=buy_rate,
        sell_cost_rate=sell_rate,
        trailing_exits=trailing_exits,
    )
    log_event(log, "backtest.done", k=k, keep_rank=keep_rank, n_days=len(ret_s),
              sharpe=round(result.strategy.sharpe, 3),
              rank_ic=round(rank_ic, 4),
              avg_turnover=round(result.avg_turnover, 3),
              blocked=blocked_total, trailing_exits=trailing_exits)
    return result


def _rerun_with_slippage(weights, asset_ret, dates, buy_rate, sell_rate,
                         slip, bench) -> BacktestMetrics:
    """用已有的权重路径重算不同滑点下的收益。

    权重路径不随滑点变化（策略不会因为成本高就改主意——那需要重新优化，
    是另一回事），所以这里可以纯向量化，不必重跑整个循环。
    """
    if weights.empty:
        return compute_metrics(pd.Series(dtype=float))
    delta = weights.diff()
    delta.iloc[0] = weights.iloc[0]
    buy_t = delta.clip(lower=0).sum(axis=1)
    sell_t = (-delta).clip(lower=0).sum(axis=1)
    cost = buy_t * (buy_rate + slip) + sell_t * (sell_rate + slip)
    gross = (weights * asset_ret.loc[dates].fillna(0.0)).sum(axis=1)
    return compute_metrics(gross - cost, bench)


def _compute_ic(scores: pd.DataFrame, asset_ret: pd.DataFrame) -> tuple[float, float]:
    """IC / Rank IC。分数在 t 日产生，对应的收益是 t+1 开盘到 t+2 开盘。

    `asset_ret[t]` 已经是 `open[t+1]/open[t]-1`，所以要再 shift(-1) 才对齐
    到"t 日的分数预测的那段收益"。差一格就会算出一个看起来还不错但完全
    错位的 IC。
    """
    fwd = asset_ret.shift(-1)
    s = scores.stack(future_stack=True).rename("pred")
    f = fwd.stack(future_stack=True).rename("fwd")
    s.index.names = ["datetime", "instrument"]
    f.index.names = ["datetime", "instrument"]
    return information_coefficient(s, f)
