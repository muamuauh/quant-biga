"""手写短线因子的批量检验 —— 回答"我自己想的这个规则有用吗"。

起因是「选最近三日涨幅超过一定幅度的股」这类想法。想法本身很容易写，
难的是**在同一套约束下和别的想法、和无脑等权持有放在一起比**。这个脚本
就是那把统一的尺子。

## 三条判据，缺一不可

1. **要和"等权买入持有"比，不是和 0 比。** 股票池是沪深300，2020 年以来
   等权持有本身年化就有 +16.8%。一个策略年化 +9% 不叫有效，叫跑输。

2. **头条数字必须是含滑点的。** 这套框架此前踩过的最大的坑：短线因子换手
   动辄 0.5~0.9/日，一年 130~230 次往返。基础费率约 10bp 往返，散户限价单
   滑点再叠 10bp —— 光成本就吃掉年化 15%~30%。所以本脚本**默认把 +10bp
   滑点算进头条收益**，`@0bp` 那一列只作参考。

3. **Rank IC 在 k=3 上是个很差的代理。** 实测过：5日反转的 Rank IC 比模型
   高，收益只有模型的 1/20。Rank IC 度量的是全截面排序质量，而 top-3 只用
   到排序的最顶端那三个位置。所以这里报它，但**不拿它做判据**。

## 阈值型 vs 排序型

「3日涨幅 > 5%」是**阈值**，不是排序。合格的票某些天不足 k 只，这时按实际
数归一会把仓位压到一两只上 —— 真实下单是按固定槽位预算走的，槽位空着就是
现金。所以阈值型一律走 `fixed_slots=True`（见 `engine.target_weights_from_scores`），
并额外报出"平均每天几只合格"。

用法：
    python scripts/13_factor_zoo.py                          # 全样本，k=3
    python scripts/13_factor_zoo.py --k 3,10                 # 同时看集中度
    python scripts/13_factor_zoo.py --start 2025-01-01 --with-model
    python scripts/13_factor_zoo.py --only ret3,rev3,rev3_th5
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qbg.backtest import engine  # noqa: E402
from qbg.backtest import panel as panel_mod  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.data import cache  # noqa: E402

# 散户限价单的额外滑点。头条收益按这个算 —— 见模块说明第 2 条。
HEADLINE_SLIPPAGE = 0.001
TRADING_DAYS_PER_YEAR = 252
SUBPERIODS = 2


# --------------------------------------------------------------------------
# 因子定义
# --------------------------------------------------------------------------
# 每个因子是 `frames -> DataFrame(date x instrument)`，**只能用截至当日收盘
# 的信息**。引擎会再 shift(1)，所以这里不要自己 shift。
#
# 分数越大越优先。反转类因子记得取负号 —— 这是最容易写反的地方，写反了
# 会得到一个"完美的负 alpha"，看起来像发现了什么，其实只是符号错了。

def _z(df: pd.DataFrame) -> pd.DataFrame:
    """按天做横截面 z-score，供组合因子用。不同量纲的因子不 z 化就没法相加。"""
    return df.sub(df.mean(axis=1), axis=0).div(df.std(axis=1).replace(0, np.nan), axis=0)


def _ret(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close.pct_change(n, fill_method=None)


def _threshold(signal: pd.DataFrame, gate: pd.DataFrame, lo: float) -> pd.DataFrame:
    """只保留 `gate >= lo` 的票，其余置 NaN（= 不参与选择）。"""
    return signal.where(gate >= lo)


def build_factors() -> dict:
    """名字 → (说明, 是否阈值型, 计算函数)。"""
    f: dict = {}

    def add(name, desc, fn, threshold=False):
        f[name] = (desc, threshold, fn)

    # --- A. 用户提的那一类：短线动量 / 突破 ------------------------------
    add("ret1", "买昨日涨幅最大的", lambda d: _ret(d["close"], 1))
    add("ret3", "买近3日涨幅最大的", lambda d: _ret(d["close"], 3))
    add("ret5", "买近5日涨幅最大的", lambda d: _ret(d["close"], 5))
    add("mom20", "买近20日涨幅最大的", lambda d: _ret(d["close"], 20))
    add("mom60", "买近60日涨幅最大的", lambda d: _ret(d["close"], 60))
    add("breakout20", "离20日最高价最近", lambda d: d["close"] / d["high20"] - 1.0)

    # 阈值版：用户的原话「近三日涨幅超过一定幅度」
    for pct in (3, 5, 8):
        add(f"ret3_th{pct}", f"近3日涨幅>{pct}%，其中选最强",
            lambda d, p=pct / 100: _threshold(_ret(d["close"], 3), _ret(d["close"], 3), p),
            threshold=True)

    # --- B. 反转 ----------------------------------------------------------
    add("rev1", "买昨日跌幅最大的", lambda d: -_ret(d["close"], 1))
    add("rev3", "买近3日跌幅最大的", lambda d: -_ret(d["close"], 3))
    add("rev5", "买近5日跌幅最大的", lambda d: -_ret(d["close"], 5))
    add("rev10", "买近10日跌幅最大的", lambda d: -_ret(d["close"], 10))
    add("rev20", "买近20日跌幅最大的", lambda d: -_ret(d["close"], 20))
    add("bias20", "20日乖离率最低（超跌）", lambda d: -(d["close"] / d["ma20"] - 1.0))

    # 阈值版：用户那条规则的镜像 —— 近3日**跌**超过一定幅度
    for pct in (3, 5, 8):
        add(f"rev3_th{pct}", f"近3日跌幅>{pct}%，其中选最惨",
            lambda d, p=pct / 100: _threshold(-_ret(d["close"], 3), -_ret(d["close"], 3), p),
            threshold=True)

    # --- C. 量能 ----------------------------------------------------------
    add("vol_surge", "放量（量比最高）", lambda d: d["volume"] / d["vol_ma20"])
    add("vol_dry", "缩量（量比最低）", lambda d: -(d["volume"] / d["vol_ma20"]))

    # --- D. 波动 ----------------------------------------------------------
    add("lowvol", "20日波动率最低", lambda d: -d["vol20"])
    add("highvol", "20日波动率最高", lambda d: d["vol20"])

    # --- E. 组合 ----------------------------------------------------------
    # 「超跌 + 缩量」：跌下来但没人恐慌抛售。A股散户市里这一组最常被提到。
    add("rev5_voldry", "超跌 且 缩量",
        lambda d: _z(-_ret(d["close"], 5)) + _z(-(d["volume"] / d["vol_ma20"])))
    # 「超跌 + 放量」：跌下来且成交放大 —— 相反的假设，用来证伪上一条。
    add("rev5_volsurge", "超跌 且 放量",
        lambda d: _z(-_ret(d["close"], 5)) + _z(d["volume"] / d["vol_ma20"]))
    # 「超跌 + 低波」：把反转限制在平时不折腾的票上。
    add("rev5_lowvol", "超跌 且 低波动",
        lambda d: _z(-_ret(d["close"], 5)) + _z(-d["vol20"]))
    # 「动量 + 低波」：趋势票里挑不颠的。
    add("mom20_lowvol", "中期动量 且 低波动",
        lambda d: _z(_ret(d["close"], 20)) + _z(-d["vol20"]))

    # --- 校准锚 -----------------------------------------------------------
    # 随机分数的收益就是"什么都不知道但照样天天换手"的下场。任何因子跑不赢
    # 它，说明那个因子的信息量还不如骰子。
    add("random", "随机（校准锚）",
        lambda d: pd.DataFrame(np.random.RandomState(42).randn(*d["close"].shape),
                               index=d["close"].index, columns=d["close"].columns))
    return f


# --------------------------------------------------------------------------
# 数据
# --------------------------------------------------------------------------

def build_frames(codes_list, pnl, start, end) -> dict:
    """构建因子要用的宽表。

    `Panel` 只带 open/close/停牌/涨跌停，量价因子还要 high 和 volume，
    所以这里重读一次缓存。价格一律**后复权**（和 Panel 一致），成交量用原始
    值 —— 量比是同一只票自己跟自己比，不需要复权。
    """
    highs, vols = {}, {}
    for code in codes_list:
        df = cache.read(code)
        if df.empty:
            continue
        if start:
            df = df[df["date"] >= pd.Timestamp(start)]
        if end:
            df = df[df["date"] <= pd.Timestamp(end)]
        if df.empty:
            continue
        df = df.set_index("date")
        highs[code] = df["high"] * df["factor"]
        vols[code] = df["volume"]

    idx, cols = pnl.dates, pnl.instruments
    high = pd.DataFrame(highs).reindex(index=idx, columns=cols)
    volume = pd.DataFrame(vols).reindex(index=idx, columns=cols)
    close = pnl.close_px

    return {
        "close": close,
        "high": high,
        "volume": volume,
        "high20": high.rolling(20, min_periods=10).max(),
        "ma20": close.rolling(20, min_periods=10).mean(),
        "vol_ma20": volume.rolling(20, min_periods=10).mean().replace(0, np.nan),
        "vol20": close.pct_change(fill_method=None).rolling(20, min_periods=10).std(),
    }


# --------------------------------------------------------------------------
# 评估
# --------------------------------------------------------------------------

def _annual(returns: pd.Series) -> float:
    if len(returns) == 0:
        return 0.0
    return float((1 + returns).prod()) ** (TRADING_DAYS_PER_YEAR / len(returns)) - 1


def evaluate(name, desc, is_threshold, scores, pnl, k) -> dict:
    """跑一个因子，返回一行记录。头条指标已含 `HEADLINE_SLIPPAGE`。"""
    res = engine.run_backtest(
        scores, pnl, k=k,
        extra_slippage=HEADLINE_SLIPPAGE,
        fixed_slots=is_threshold,
        slippage_grid=(0.0, HEADLINE_SLIPPAGE),
    )
    halves = np.array_split(np.arange(len(res.daily_returns)), SUBPERIODS)
    sub = [_annual(res.daily_returns.iloc[i]) for i in halves]

    # 阈值型：平均每天几只票合格。远小于 k 说明大部分时间在空仓，
    # 收益低不代表信号差，只代表没上场 —— 这两件事必须能分开看。
    filled = float("nan")
    if is_threshold:
        filled = float(scores.notna().sum(axis=1).mean())

    return {
        "name": name, "desc": desc, "k": k,
        "annual": res.strategy.annual_return,
        "annual_nofric": res.slippage_curve[0.0].annual_return,
        "sharpe": res.strategy.sharpe,
        "mdd": res.strategy.max_drawdown,
        "turnover": res.avg_turnover,
        "rank_ic": res.rank_ic,
        "sub": sub,
        "filled": filled,
        # 「涨停买不进」对短线动量因子是**要害**：近3日涨最多的票，正是次日
        # 开盘一字涨停概率最高的票。这一列大，说明回测里那些收益有相当一部分
        # 在真实盘口上根本拿不到。
        "no_buy": res.blocked["limit_up_cannot_buy"],
        "blocked": sum(res.blocked.values()),
    }, res


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="手写短线因子批量检验")
    p.add_argument("--k", default="3", help="持仓只数，逗号分隔可给多个")
    p.add_argument("--start", default=None)
    p.add_argument("--end", default=None)
    p.add_argument("--only", default="", help="只跑这些因子，逗号分隔")
    p.add_argument("--with-model", action="store_true",
                   help="把 qlib 模型分数一起放进来比（要求窗口在模型测试段内）")
    args = p.parse_args(argv)

    ks = [int(x) for x in args.k.split(",") if x.strip()]
    members = load_universe()
    print(f"构建面板（{len(members)} 只）…", flush=True)
    t0 = time.time()
    pnl = panel_mod.build_panel(members, start=args.start, end=args.end)
    frames = build_frames(members, pnl, args.start, args.end)
    print(f"  {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   "
          f"{len(pnl.dates)} 个交易日   {len(pnl.instruments)} 只   "
          f"({time.time() - t0:.0f}s)\n", flush=True)

    factors = build_factors()
    if args.with_model:
        from qbg.strategy.predict import (
            load_latest_predictions,
            neutralize_frame,
            predictions_to_frame,
        )

        model = predictions_to_frame(load_latest_predictions())
        if settings.qbg_industry_neutral:
            model = neutralize_frame(model)
        factors["model"] = ("qlib LightGBM（现行）", False, lambda d, m=model: m)

    if args.only:
        want = {x.strip() for x in args.only.split(",") if x.strip()}
        factors = {n: v for n, v in factors.items() if n in want}

    rows, bench = [], None
    for k in ks:
        for name, (desc, is_th, fn) in factors.items():
            scores = fn(frames).reindex(index=pnl.dates, columns=pnl.instruments)
            row, res = evaluate(name, desc, is_th, scores, pnl, k)
            rows.append(row)
            # 基准是"股票池等权买入持有"，由引擎顺带算出，和用哪个因子无关。
            # 它不换手，所以不受滑点影响 —— 这正是它难打败的原因。
            bench = bench or res.benchmark
            print(f"  {name:<16} k={k}  年化(含滑点) {row['annual']:+8.2%}", flush=True)

    _report(rows, ks, bench, pnl)
    return 0


def _report(rows, ks, bench, pnl) -> None:
    print("\n" + "=" * 100)
    print(f"因子对照   {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   "
          f"{len(pnl.dates)} 个交易日   头条收益已扣 基础费率 + {HEADLINE_SLIPPAGE * 1e4:.0f}bp 滑点")
    print("=" * 100)
    print(f"等权买入持有（不换手，因此不受滑点影响）: "
          f"年化 {bench.annual_return:+.2%}   夏普 {bench.sharpe:.2f}   "
          f"回撤 {bench.max_drawdown:.2%}")

    for k in ks:
        sub = sorted([r for r in rows if r["k"] == k],
                     key=lambda r: r["annual"], reverse=True)
        print(f"\n--- k={k} ---")
        print(f"{'因子':<16}{'年化':>9}{'@0bp':>9}{'夏普':>7}{'回撤':>9}"
              f"{'换手':>7}{'RankIC':>9}{'前半':>9}{'后半':>9}"
              f"{'合格数':>7}{'买不进':>7}  说明")
        print("-" * 124)
        for r in sub:
            beats = "*" if r["annual"] > bench.annual_return else " "
            filled = f"{r['filled']:.1f}" if r["filled"] == r["filled"] else "—"
            print(f"{beats}{r['name']:<15}{r['annual']:>+9.2%}{r['annual_nofric']:>+9.2%}"
                  f"{r['sharpe']:>7.2f}{r['mdd']:>9.2%}{r['turnover']:>7.2f}"
                  f"{r['rank_ic']:>+9.4f}{r['sub'][0]:>+9.2%}{r['sub'][-1]:>+9.2%}"
                  f"{filled:>7}{r['no_buy']:>7}  {r['desc']}")

    print("\n" + "-" * 100)
    print("怎么读这张表")
    print("-" * 100)
    print("  · 行首 * = 净收益跑赢等权买入持有。**没有星号的一律是坏策略**，")
    print("    哪怕年化为正 —— 一个不换手、不用想、不花 LLM 钱的组合就能拿到更多。")
    print("  · 「@0bp」是只扣基础费率、不扣滑点的版本。它和「年化」的差距就是")
    print("    换手的代价；差距大的因子在实盘里最容易变成另一个样子。")
    print("  · 「前半/后半」是把区间对半切的年化。**符号翻转的因子不要信** ——")
    print("    那是一段行情的产物，不是一条规律。")
    print("  · 「合格数」只对阈值型有意义：平均每天几只票满足条件。远小于 k")
    print("    说明大部分时间空仓，收益低是因为没上场，不是因为信号差。")
    print("  · 「买不进」= 因次日开盘一字涨停而没能成交的次数。短线动量因子")
    print("    在这一列上天然吃亏：近3日涨最多的票正是最可能开盘封板的票。")
    print("  · Rank IC 只作参考，不作判据：它度量全截面排序，而 top-k 只用到")
    print("    最顶端 k 个位置。实测过 Rank IC 更高但收益低 20 倍的例子。")
    print("  · 生存者偏差贯穿全表：股票池是**当前**沪深300 成分。所有绝对收益")
    print("    都被系统性高估，但因子之间的**相对**比较仍然成立。")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
