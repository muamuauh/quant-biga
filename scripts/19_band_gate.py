"""`QBG_MARKET_SMA_BAND`（择时迟滞缓冲带）的八项闸。

## 为什么闸要跑在**老口径**指数上

生产链路的 `market_risk_on()` 读的是不带资格表的等权指数 —— 纳入前视那个修
还没有接到生产上（那是另一个改动，要自己的闸）。**闸必须测将要运行的那个
东西**，所以主判据用老口径。修正口径并列报出来当旁证：两边结论一致才敢改。

## 为什么两条臂都要跑

  · **全样本 model-free**（1620 日，等权全池 × 仓位）：统计上强，但和实盘的
    k=3 集中组合不是一回事。
  · **模型窗 k=3**（388 日）：口径对，但**噪声压倒一切** —— 2026-09-05 实测
    这个窗口单配置的相位噪声可达 142 个百分点年化。

所以主判据取前者，后者只作交叉检查并明确标注不可信。只跑后者会得出随机结论，
只跑前者会测错东西。

## 八项闸为什么换了两项

`tuning/gates.py` 的通用八项是给**选股**参数设计的。缓冲带不碰选股，两项要换：

  · `turnover` → `switch_cost`：缓冲带不改变持仓选择，改变的是**翻仓次数**。
    换手在 model-free 臂里恒等于仓位翻转，直接测翻仓成本更贴切。
  · `rank_ic` → `protection`：缓冲带对 Rank IC 的影响**恒为零**（同一批分数），
    留着就是一项永远通过的空闸。换成"相对一直满仓仍有回撤保护" —— 如果连这
    都没有，那该做的是把择时关掉（`QBG_MARKET_SMA=0`），不是调缓冲带。

用法：
    python scripts/19_band_gate.py --candidate 0.03            # 改缓冲带
    python scripts/19_band_gate.py --candidate-sma 200         # 改 SMA
    python scripts/19_band_gate.py --candidate-sma 200 --neighbors 150,250
"""

from __future__ import annotations

import argparse
import sys
from importlib import import_module
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qbg.backtest import engine  # noqa: E402
from qbg.backtest.metrics import compute_metrics  # noqa: E402
from qbg.backtest.panel import build_panel  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.data import universe as universe_mod  # noqa: E402
from qbg.execution import fees  # noqa: E402
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402
from qbg.tuning.gates import Check, GateReport  # noqa: E402

switch_costs = import_module("07_regime_stress").switch_costs

SUBPERIODS = 4
HIGH_COST_MULT = 1.75
SLIPPAGE_BP = 10.0          # 和 13/15/18 同一口径
TRADING_DAYS_PER_YEAR = 252


def _annual(returns: pd.Series) -> float:
    if len(returns) == 0:
        return 0.0
    return float((1 + returns).prod()) ** (TRADING_DAYS_PER_YEAR / len(returns)) - 1


def overlay(asset_returns, level, sma, band, slippage_bp) -> tuple[pd.Series, dict]:
    """把择时仓位叠到等权全池收益上，返回 `(净日收益, 事实)`。"""
    shifted = risk_on_series(level, sma, band).shift(1)
    exposure = shifted.where(shifted.notna(), True).astype(bool)
    aligned = exposure.reindex(asset_returns.index).fillna(True).astype(float)
    costs = switch_costs(aligned, slippage_bp).reindex(asset_returns.index).fillna(0.0)
    net = asset_returns * aligned - costs
    m = compute_metrics(net, asset_returns)
    return net, {"annual_return": m.annual_return, "sharpe": m.sharpe,
                 "max_drawdown": m.max_drawdown,
                 "switch_cost_pa": float(costs.sum() / len(costs) * TRADING_DAYS_PER_YEAR),
                 "flips": int((aligned.diff().fillna(0) != 0).sum()),
                 "invested": float(aligned.mean())}


def topk_arm(scores, pnl, level, sma, band, k) -> dict | None:
    """模型窗 k=3 的交叉检查。风控口径和实盘一致：risk-off 当天分数置空。

    **`level` 必须是完整历史（2020 起）的等权净值，不能先截到模型窗再算 SMA。**
    `risk_on_series` 的 `min_periods=sma_window`：模型窗只有 388 天，先截断的话
    SMA200 前 200 天没有信号（默认 risk-on），SMA250 干脆整段都没信号 ——
    于是长 SMA 会退化成"不择时"，看起来像"长 SMA 更差"，其实是**这个窗口
    测不了它**。生产链路的 `market_risk_on()` 读的是完整 parquet 历史，
    所以先算信号、再 reindex 才和实盘同口径。
    """
    on = risk_on_series(level, sma, band).reindex(pnl.dates).ffill().fillna(True)
    res = engine.run_backtest(scores.where(on), pnl, k=k,
                              extra_slippage=SLIPPAGE_BP / 1e4, slippage_grid=())
    return {"annual_return": res.strategy.annual_return, "sharpe": res.strategy.sharpe,
            "max_drawdown": res.strategy.max_drawdown, "turnover": res.avg_turnover}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="择时参数（缓冲带 / SMA）的八项闸")
    p.add_argument("--candidate", type=float, default=None, help="候选缓冲带，小数")
    p.add_argument("--candidate-sma", type=int, default=None, help="候选 SMA 窗口")
    p.add_argument("--neighbors", default="",
                   help="高原闸用的相邻取值，逗号分隔。**按变动的那一维解释** —— "
                        "改 SMA 就填 SMA，改缓冲带就填缓冲带")
    p.add_argument("--sma", type=int, default=None, help="基线 SMA，默认取生效配置")
    p.add_argument("--k", type=int, default=settings.qbg_top_k)
    # 默认跟随生效配置。写死 raw 的话，用户一旦打开 QBG_MARKET_INDEX_ELIGIBLE，
    # 闸就在和一个不存在的配置作比较 —— 20_timing_gate.py 犯过这个错。
    p.add_argument("--index", choices=("raw", "fixed"),
                   default=("fixed" if int(settings.qbg_market_index_eligible) else "raw"),
                   help="判据用哪条等权指数。默认跟随 QBG_MARKET_INDEX_ELIGIBLE；"
                        "手动选 fixed 等于假设指数那个修也一起上线")
    args = p.parse_args(argv)

    base_sma = int(settings.qbg_market_sma if args.sma is None else args.sma)
    base_band = float(settings.qbg_market_sma_band)
    cand_sma = base_sma if args.candidate_sma is None else int(args.candidate_sma)
    cand_band = base_band if args.candidate is None else float(args.candidate)
    if (cand_sma, cand_band) == (base_sma, base_band):
        p.error("候选和基线完全相同 —— 至少给 --candidate 或 --candidate-sma 之一")

    # 高原闸只在**变动的那一维**上取邻居。两维一起变时没有一维意义上的"邻居"，
    # 必须显式给，否则闸会在一条错误的轴上宣布"高原稳定"。
    sma_moved, band_moved = cand_sma != base_sma, cand_band != base_band
    explicit = [float(x) for x in args.neighbors.split(",") if x.strip()]
    if sma_moved and band_moved and not explicit:
        p.error("同时改了 SMA 和缓冲带 —— 高原闸没有单一维度可测，请显式给 --neighbors")
    if sma_moved:
        vals = explicit or [max(20.0, cand_sma - 50), cand_sma + 50]
        neighbors = [(int(v), cand_band) for v in sorted(vals)]
        fmt_n = [f"SMA{int(v)}" for v, _ in neighbors]
    else:
        vals = explicit or sorted({max(0.0, round(cand_band - 0.01, 4)),
                                   round(cand_band + 0.02, 4)})
        neighbors = [(cand_sma, float(v)) for v in sorted(vals)]
        fmt_n = [f"{b:.1%}" for _, b in neighbors]

    members = load_universe()
    pnl = build_panel(members)
    asset_returns = pnl.open_to_open_returns().mean(axis=1).dropna()

    raw = equal_weight_index(members).reindex(pnl.dates).ffill()
    mapping = universe_mod.inclusion_dates()
    fixed = equal_weight_index(
        members, eligible=universe_mod.eligibility_mask(
            pnl.dates, pnl.instruments, mapping)).reindex(pnl.dates).ffill()

    hold = compute_metrics(asset_returns, asset_returns)
    print(f"窗口 {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   {len(pnl.dates)} 日   "
          f"翻仓滑点 {SLIPPAGE_BP:.0f}bp")
    print(f"基线 = SMA{base_sma} 缓冲{base_band:.1%}（现行生效值）   "
          f"候选 = SMA{cand_sma} 缓冲{cand_band:.1%}   高原邻居 = {fmt_n}")
    print(f"一直满仓（保护闸的参照）: 年化 {hold.annual_return:+.2%}   "
          f"夏普 {hold.sharpe:.2f}   回撤 {hold.max_drawdown:.2%}\n")

    # 判据指数和旁证指数。默认判据 = 生产在读的那条。
    judge, witness = ((raw, fixed) if args.index == "raw" else (fixed, raw))
    judge_name = "老口径（生产在读）" if args.index == "raw" else "修正纳入前视"
    witness_name = "修正纳入前视" if args.index == "raw" else "老口径（生产在读）"
    print(f"判据指数 = {judge_name}" + "\n")

    base_net, baseline = overlay(asset_returns, judge, base_sma, base_band, SLIPPAGE_BP)
    cand_net, candidate = overlay(asset_returns, judge, cand_sma, cand_band, SLIPPAGE_BP)

    print(f"{'指标':<18}{f'基线(SMA{base_sma})':>14}{f'候选(SMA{cand_sma})':>14}{'差':>12}")
    print("-" * 58)
    for key, fmt in (("annual_return", "{:+.2%}"), ("sharpe", "{:.4f}"),
                     ("max_drawdown", "{:.2%}"), ("switch_cost_pa", "{:.2%}"),
                     ("invested", "{:.1%}")):
        b, c = baseline[key], candidate[key]
        print(f"{key:<18}{fmt.format(b):>14}{fmt.format(c):>14}{fmt.format(c - b):>12}")
    print(f"{'flips':<18}{baseline['flips']:>14}{candidate['flips']:>14}"
          f"{candidate['flips'] - baseline['flips']:>12}")

    # 旁证：同一组参数在**另一条**指数上。两边不一致就不要改 —— 那说明这个
    # 参数的好坏取决于指数口径，而指数口径本身还是个未决的改动。
    _, base_fx = overlay(asset_returns, witness, base_sma, base_band, SLIPPAGE_BP)
    _, cand_fx = overlay(asset_returns, witness, cand_sma, cand_band, SLIPPAGE_BP)
    print(f"\n旁证 · {witness_name}（**非判据**）:")
    print(f"  基线 年化 {base_fx['annual_return']:+.2%} 夏普 {base_fx['sharpe']:.2f} "
          f"翻转 {base_fx['flips']}   →   "
          f"候选 年化 {cand_fx['annual_return']:+.2%} 夏普 {cand_fx['sharpe']:.2f} "
          f"翻转 {cand_fx['flips']}")
    agree = (cand_fx["sharpe"] >= base_fx["sharpe"]) == (candidate["sharpe"] >= baseline["sharpe"])
    print(f"  两个口径结论{'一致 ✅' if agree else '**不一致** ⚠ —— 不要改'}")

    chunks = np.array_split(np.arange(len(base_net)), SUBPERIODS)
    excess = []
    print(f"\n子区间超额（候选 − 基线，年化），切成 {SUBPERIODS} 段：")
    for i, idx in enumerate(chunks, 1):
        b, c = _annual(base_net.iloc[idx]), _annual(cand_net.iloc[idx])
        excess.append(c - b)
        span = base_net.index[idx]
        print(f"  第{i}段 {span[0].date()}~{span[-1].date()}  "
              f"基线 {b:+9.2%}  候选 {c:+9.2%}  超额 {c - b:+9.2%}")

    neighbor_sharpes = []
    print("\n相邻取值（高原闸）：")
    for nb_sma, nb_band in neighbors:
        _, f = overlay(asset_returns, judge, nb_sma, nb_band, SLIPPAGE_BP)
        neighbor_sharpes.append(f["sharpe"])
        print(f"  SMA{nb_sma:<4} 缓冲{nb_band:>5.1%} → 夏普 {f['sharpe']:.4f}  翻转 {f['flips']}")

    # +75% 成本：缓冲带的全部价值就是省成本，成本涨了它只会更有价值 ——
    # 这一闸对它天然友好，所以**同时**要求候选在高成本下仍不劣于基线，
    # 否则等于白送一项。
    profile = fees.FeeProfile.load()
    hi_slip = SLIPPAGE_BP * HIGH_COST_MULT
    _, high_cost = overlay(asset_returns, judge, cand_sma, cand_band, hi_slip)
    _, high_base = overlay(asset_returns, judge, base_sma, base_band, hi_slip)
    print(f"\n+{int((HIGH_COST_MULT - 1) * 100)}% 成本（滑点 {hi_slip:.0f}bp，"
          f"基础费率往返 {fees.round_trip_rate(profile) * 1e4:.1f}bp）:")
    print(f"  基线 年化 {high_base['annual_return']:+.2%}   "
          f"候选 年化 {high_cost['annual_return']:+.2%}")

    print("\n模型窗 k=3 交叉检查（**388 日，噪声可达 142 个百分点年化，不作判据**）:")
    try:
        from qbg.strategy.predict import (
            load_latest_predictions,
            neutralize_frame,
            predictions_to_frame,
        )

        frame = predictions_to_frame(load_latest_predictions())
        if settings.qbg_industry_neutral:
            frame = neutralize_frame(frame)
        mp = build_panel(members, start=str(frame.index.min().date()),
                         end=str(frame.index.max().date()))
        sc = frame.reindex(index=mp.dates, columns=mp.instruments)
        # **不 reindex 到模型窗**：SMA 要在完整历史上算（见 topk_arm 的说明）。
        # 资格表也因此要覆盖完整历史，不能只给模型窗那 388 天。
        full_eligible = (None if args.index == "raw" else
                         universe_mod.eligibility_mask(pnl.dates, pnl.instruments, mapping))
        lvl = equal_weight_index(members, eligible=full_eligible)
        for label, sma, band in ((f"基线 SMA{base_sma}", base_sma, base_band),
                                 (f"候选 SMA{cand_sma}", cand_sma, cand_band)):
            r = topk_arm(sc, mp, lvl, sma, band, args.k)
            print(f"  {label:<10} 年化 {r['annual_return']:+9.2%}  "
                  f"夏普 {r['sharpe']:>6.2f}  回撤 {r['max_drawdown']:>8.2%}")
    except Exception as exc:  # noqa: BLE001 —— 交叉检查失败不该挡住主判据
        print(f"  跳过（{type(exc).__name__}: {str(exc)[:90]}）")

    report = GateReport(tuple(_evaluate(
        baseline, candidate, excess, neighbor_sharpes, high_cost, high_base, hold)))
    print("\n八项闸：")
    for c in report.checks:
        print(f"  {'✅' if c.passed else '❌'} {c.name:<16} {c.detail}")
    failed = [c.name for c in report.checks if not c.passed]
    print(f"\n结论：{'通过' if not failed else '**未通过**'}")
    moved = ("QBG_MARKET_SMA 改成 " + str(cand_sma) if sma_moved
             else "QBG_MARKET_SMA_BAND 改成 " + str(cand_band))
    keep = (f"SMA{base_sma}" if sma_moved else f"缓冲带 {base_band}")
    print(f"  → 可以把 {moved}" if not failed
          else f"  → 维持 {keep}。未过：{'、'.join(failed)}")
    return 0 if not failed else 1


def _evaluate(baseline, candidate, subperiod_excess, neighbor_sharpes,
              high_cost, high_base, hold) -> list[Check]:
    passed_sub = sum(v >= 0 for v in subperiod_excess)
    return [
        Check("net_return", candidate["annual_return"] >= baseline["annual_return"],
              "净年化不低于基线"),
        Check("sharpe", candidate["sharpe"] >= baseline["sharpe"] - 0.05,
              "Sharpe 允许最多回落 0.05"),
        Check("drawdown", candidate["max_drawdown"] >= baseline["max_drawdown"] - 0.02,
              "回撤不显著恶化"),
        # 替代通用闸的 turnover：缓冲带不碰选股，它改变的是翻仓次数。
        Check("switch_cost", candidate["switch_cost_pa"] <= baseline["switch_cost_pa"],
              f"翻仓成本不增加（基线 {baseline['switch_cost_pa']:.2%}/年 → "
              f"候选 {candidate['switch_cost_pa']:.2%}/年）"),
        # 替代通用闸的 rank_ic：缓冲带对 Rank IC 的影响恒为零，那是一项空闸。
        # 换成"择时还配不配存在" —— 没有回撤保护就该关掉择时，不是调缓冲带。
        Check("protection", candidate["max_drawdown"] > hold.max_drawdown,
              f"相对一直满仓仍有回撤保护（满仓 {hold.max_drawdown:.2%} → "
              f"候选 {candidate['max_drawdown']:.2%}）"),
        Check("subperiod", bool(subperiod_excess) and
              passed_sub >= (len(subperiod_excess) + 1) // 2,
              f"至少半数子区间不劣于基线（{passed_sub}/{len(subperiod_excess)}）"),
        Check("plateau", len(neighbor_sharpes) >= 2 and
              min(neighbor_sharpes) >= candidate["sharpe"] - 0.20,
              "相邻取值处于稳定高原（不是靠运气立住的尖峰）"),
        Check("cost_robustness",
              high_cost["annual_return"] > 0 and high_cost["sharpe"] > 0
              and high_cost["annual_return"] >= high_base["annual_return"],
              f"+{int((HIGH_COST_MULT - 1) * 100)}% 成本下仍为正**且**不劣于基线"),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
