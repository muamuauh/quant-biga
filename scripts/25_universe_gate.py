"""扩股票池的八项闸：沪深300 vs 沪深300+中证500。

## 这个比较有三层变化，不能混为一谈

换股票池不是换一个参数，它同时改了三件事：

  1. **训练池**（模型见过哪些票）—— qlib 的 `instruments: all` 意味着 bin 里
     有什么就训什么，所以要另建一份 bin（`data/qlib_bin/cn_data_ext`）。
  2. **候选池**（从哪些票里选 top-k）
  3. **成交成本**（中小盘流动性差，10bp 的滑点假设站不住）

所以这里**同时报 10bp 和 20bp** 两档滑点。只报 10bp 等于假设中证500 的票和
茅台一样好成交 —— 那是自欺欺人，而换手率单独解释了因子间净收益差异的 58%。

## 纳入前视在中小盘上严重得多

中证500 有 **78%** 的当前成分是 2020 年后才纳入的（沪深300 只有 47%）。
资格表能修，但修正之后 2020 年的中证500 只剩约 110 只可用 —— **早期样本很薄**。
脚本会把各年的有效池子大小打出来，那一行比任何收益数字都重要。

## 判据仍然是同口径基准

扩池后的等权买入持有**不是**沪深300 的等权持有 —— 池子不一样，基准也不一样。
拿扩池策略去比沪深300 的基准是在偷换尺子。两边各自和自己池子的等权持有比。

用法：
    python scripts/25_universe_gate.py                 # 训练 + 评估
    python scripts/25_universe_gate.py --reuse         # 已训过就直接读
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from qbg.backtest import engine  # noqa: E402
from qbg.backtest import panel as panel_mod  # noqa: E402
from qbg.backtest.metrics import compute_metrics  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.data import universe as universe_mod  # noqa: E402
from qbg.model import train as train_mod  # noqa: E402
from qbg.strategy.predict import (  # noqa: E402
    load_latest_predictions,
    neutralize_frame,
    predictions_to_frame,
)
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402
from qbg.tuning.gates import Check, GateReport  # noqa: E402

EXT_EXPERIMENT = "cn_lgb_ext"
from qbg.utils.console import make_output_safe  # noqa: E402

EXT_PROVIDER = "cn_data_ext"

# OOS 分段，和 scripts/23_oos_window.py 逐字相同。
# (train 起, train 止, valid 止, test 止)；test 起 = valid 止 + 1 天。
#
# **默认 `cur` 是生产分段，测试段只有约 388 个交易日** —— 首版扩池结论就跑在
# 这个窗口上，子区间摆动 −443%，所以那张表当时只能标"幅度不可信"。
# `mid` 把 train/valid 各往前挪一年，测试段变成 2024-01-01 起，约 640 日，多 65%。
# `23_oos_window.py` 实测 `mid` 最优：噪声底砍 56~70%，Rank IC 只掉 18%。
SPLITS = {
    "cur":  ("2020-01-01", "2023-12-31", "2024-12-31", "2026-08-10"),
    "mid":  ("2020-01-01", "2022-12-31", "2023-12-31", "2026-08-10"),
    "long": ("2020-01-01", "2021-12-31", "2022-12-31", "2026-08-10"),
}
ZZ500_FILE = ROOT / "configs" / "universe_000905.txt"
SUBPERIODS = 4
SLIPPAGES = (0.001, 0.002)     # 10bp 和 20bp —— 中小盘不能只看 10bp
HIGH_COST_MULT = 1.75


def read_universe_file(path: Path) -> list[str]:
    return sorted({ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.startswith("#")})


def _annual(returns: pd.Series) -> float:
    if len(returns) == 0:
        return 0.0
    return float((1 + returns).prod()) ** (252 / len(returns)) - 1


def _next_day(day: str) -> str:
    import datetime as dt

    return (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()


def train_arm(provider: str, experiment: str, split: str, seeds: int | None,
              workdir: Path) -> None:
    """训练一条臂。**除了 provider_uri 和分段，其余和生产逐字相同** ——
    否则测出来的差异分不清是"池子变了"还是"顺手也改了别的"。

    **换分段时两条臂都要重训。** 只重训扩池那条，等于拿 640 日的扩池去比
    388 日的基准，差异里混着"窗口不同"这第三个变量 —— 那正是本文件开头警告的
    "三层变化不能混为一谈"。
    """
    cfg = yaml.safe_load(settings.workflow_yaml.read_text(encoding="utf-8"))
    cfg["qlib_init"]["provider_uri"] = f"./data/qlib_bin/{provider}"
    tr_start, tr_end, va_end, te_end = SPLITS[split]
    dh = cfg["data_handler_config"]
    dh["fit_start_time"] = tr_start
    # **必须跟着 train 段走**，否则预处理器会在测试段上拟合（安静的泄漏）。
    dh["fit_end_time"] = tr_end
    cfg["task"]["dataset"]["kwargs"]["segments"] = {
        "train": [tr_start, tr_end],
        "valid": [_next_day(tr_end), va_end],
        "test": [_next_day(va_end), te_end],
    }
    path = workdir / f"workflow_{experiment}.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    train_mod.train(workflow_yaml=path, experiment_name=experiment, seed_count=seeds)


def evaluate(members, frame, k, slippage, index_code):
    """一个池子的完整评估：策略 + **同池子的**等权买入持有基准。"""
    pnl = panel_mod.build_panel(members, start=str(frame.index.min().date()),
                                end=str(frame.index.max().date()))
    scores = frame.reindex(index=pnl.dates, columns=pnl.instruments)
    if settings.qbg_market_sma:
        # 择时信号在**完整历史**上算，不能先截到测试段（min_periods 会让长 SMA
        # 静默退化成"不择时"）。用各自池子的等权净值，和实盘口径一致。
        lvl = equal_weight_index(
            members, use_inclusion=bool(int(settings.qbg_market_index_eligible)))
        on = (risk_on_series(lvl, settings.qbg_market_sma,
                             float(settings.qbg_market_sma_band))
              .reindex(pnl.dates).ffill().fillna(True))
        scores = scores.where(on)
    res = engine.run_backtest(scores, pnl, k=k, extra_slippage=slippage,
                              slippage_grid=())
    return pnl, res


def main(argv=None) -> int:
    make_output_safe()
    p = argparse.ArgumentParser(description="扩股票池的八项闸")
    p.add_argument("--k", type=int, default=settings.qbg_top_k)
    p.add_argument("--seeds", type=int, default=None)
    p.add_argument("--reuse", action="store_true")
    p.add_argument("--split", default="cur", choices=sorted(SPLITS),
                   help="OOS 分段。cur = 生产（测试段约 388 日）；"
                        "mid = 训练段前挪一年，测试段约 640 日")
    args = p.parse_args(argv)

    if not ZZ500_FILE.exists():
        print(f"缺 {ZZ500_FILE}。先跑 scripts/22_expand_universe.py --index 000905",
              file=sys.stderr)
        return 1
    hs300 = load_universe()
    ext_members = sorted(set(hs300) | set(read_universe_file(ZZ500_FILE)))

    workdir = ROOT / "data" / "universe_gate"
    workdir.mkdir(parents=True, exist_ok=True)
    suffix = "" if args.split == "cur" else f"_{args.split}"
    # cur 的基准臂就是生产模型本身，不重训（那正是"现行部署"的定义）。
    base_exp = f"cn_lgb{suffix}" if suffix else None
    ext_exp = f"{EXT_EXPERIMENT}{suffix}"

    def obtain(provider, experiment, label, members):
        if args.reuse:
            try:
                pred = (load_latest_predictions(experiment) if experiment
                        else load_latest_predictions())
                print(f"复用 {experiment or 'cn_lgb（生产）'}", flush=True)
                return pred
            except Exception:  # noqa: BLE001
                pass
        if experiment is None:
            return load_latest_predictions()
        print(f"训练 {label}（{len(members)} 只，分段 {args.split}）…", flush=True)
        t0 = time.time()
        train_arm(provider, experiment, args.split, args.seeds, workdir)
        print(f"  完成 ({time.time() - t0:.0f}s)", flush=True)
        return load_latest_predictions(experiment)

    base_pred = obtain("cn_data", base_exp, "沪深300 臂", hs300)
    ext_pred = obtain(EXT_PROVIDER, ext_exp, "扩池臂", ext_members)

    def prep(pred):
        f = predictions_to_frame(pred)
        return neutralize_frame(f) if settings.qbg_industry_neutral else f

    base_frame = prep(base_pred)
    ext_frame = prep(ext_pred)

    print(f"\n沪深300 {len(hs300)} 只   扩池 {len(ext_members)} 只   "
          f"k={args.k}   择时 SMA{settings.qbg_market_sma} "
          f"缓冲{settings.qbg_market_sma_band:.0%}")
    tr_start, tr_end, va_end, te_end = SPLITS[args.split]
    print(f"分段 {args.split}：train {tr_start}~{tr_end}  valid ~{va_end}  "
          f"test {_next_day(va_end)}~{te_end}")

    # 各年真正在指数里的只数 —— 这一行比任何收益数字都重要。
    ext_pnl_probe = panel_mod.build_panel(
        ext_members, start=str(ext_frame.index.min().date()),
        end=str(ext_frame.index.max().date()))
    mapping = dict(universe_mod.inclusion_dates("000300"))
    mapping.update(universe_mod.inclusion_dates("000905"))
    elig = universe_mod.eligibility_mask(ext_pnl_probe.dates,
                                         ext_pnl_probe.instruments, mapping)
    per_year = elig.sum(axis=1)
    print("\n各年真正在指数里的只数（扩池后）：")
    for year, n in per_year.groupby(per_year.index.year).mean().items():
        print(f"  {year}  {n:5.0f} / {len(ext_pnl_probe.instruments)} 只")

    results = {}
    for slip in SLIPPAGES:
        print(f"\n{'=' * 84}\n滑点 {slip * 1e4:.0f}bp\n{'=' * 84}")
        print(f"{'池子':<16}{'净年化':>11}{'夏普':>8}{'回撤':>10}{'换手':>8}"
              f"{'RankIC':>10}{'同池等权持有':>14}")
        print("-" * 84)
        for label, members, frame, idx in (
                ("沪深300（现行）", hs300, base_frame, "000300"),
                ("+中证500", ext_members, ext_frame, "000905")):
            pnl, res = evaluate(members, frame, args.k, slip, idx)
            hold = compute_metrics(
                pnl.open_to_open_returns().mean(axis=1).dropna())
            results[(label, slip)] = (res, hold, pnl)
            print(f"{label:<16}{res.strategy.annual_return:>11.2%}"
                  f"{res.strategy.sharpe:>8.2f}{res.strategy.max_drawdown:>10.2%}"
                  f"{res.avg_turnover:>8.3f}{res.rank_ic:>+10.4f}"
                  f"{hold.annual_return:>14.2%}")

    _gate(results, args)
    return 0


def _gate(results, args) -> None:
    slip = SLIPPAGES[0]
    base_res, base_hold, _ = results[("沪深300（现行）", slip)]
    cand_res, cand_hold, _ = results[("+中证500", slip)]
    hi_res, _, _ = results[("+中证500", SLIPPAGES[-1])]

    b = base_res.daily_returns
    c = cand_res.daily_returns
    n = min(len(b), len(c))
    excess = []
    print(f"\n子区间超额（候选 − 基线，年化，{slip * 1e4:.0f}bp），切成 {SUBPERIODS} 段：")
    for i, idx in enumerate(np.array_split(np.arange(n), SUBPERIODS), 1):
        bb, cc = _annual(b.iloc[idx]), _annual(c.iloc[idx])
        excess.append(cc - bb)
        print(f"  第{i}段  基线 {bb:+9.2%}  候选 {cc:+9.2%}  超额 {cc - bb:+9.2%}")

    passed_sub = sum(v >= 0 for v in excess)
    checks = [
        Check("net_return", cand_res.strategy.annual_return >= base_res.strategy.annual_return,
              "净年化不低于基线"),
        Check("sharpe", cand_res.strategy.sharpe >= base_res.strategy.sharpe - 0.05,
              "Sharpe 允许最多回落 0.05"),
        Check("drawdown",
              cand_res.strategy.max_drawdown >= base_res.strategy.max_drawdown - 0.02,
              "回撤不显著恶化"),
        Check("turnover", cand_res.avg_turnover <= base_res.avg_turnover * 1.20,
              "换手不增加超过 20%"),
        Check("rank_ic", cand_res.rank_ic > 0, "Rank IC 必须为正"),
        Check("subperiod", passed_sub >= (SUBPERIODS + 1) // 2,
              f"至少半数子区间不劣于基线（{passed_sub}/{SUBPERIODS}）"),
        # 高原闸不适用：股票池不是连续取值，"相邻池子"不存在。
        # 硬凑一个（比如中证500 的一半）测的是另一件事。先例：10_neutralize_gate.py
        Check("plateau", True, "【不适用】股票池不是连续取值，没有相邻取值可测"),
        # 扩池的成本鲁棒性不是"再加 75%"，而是**中小盘本来就该用更高的滑点**。
        # 20bp 下还赢，才叫这个池子真的更好。
        Check("cost_robustness",
              hi_res.strategy.annual_return >= base_res.strategy.annual_return,
              f"{SLIPPAGES[-1] * 1e4:.0f}bp 滑点下仍不低于基线的 "
              f"{SLIPPAGES[0] * 1e4:.0f}bp 表现（中小盘该按更高滑点算）"),
    ]
    report = GateReport(tuple(checks))
    print("\n八项闸：")
    for ck in report.checks:
        icon = "➖" if ck.detail.startswith("【不适用】") else ("✅" if ck.passed else "❌")
        print(f"  {icon} {ck.name:<16} {ck.detail}")
    failed = [ck.name for ck in report.checks if not ck.passed]
    print(f"\n结论：{'通过' if not failed else '**未通过**'}")
    print(f"  → {'可以考虑切换股票池' if not failed else '维持沪深300。未过：' + '、'.join(failed)}")

    print("\n" + "-" * 84)
    print("  · **两边各自和自己池子的等权持有比。** 扩池后的基准不是沪深300 的")
    print("    基准 —— 拿扩池策略去比沪深300 的基准是在偷换尺子。")
    print("  · 中证500 有 78% 的成分是 2020 年后才纳入的，纳入前视比沪深300 重得多；")
    print("    上面各年的有效只数那张表比任何收益数字都重要。")
    print("  · 切换股票池要改 `QBG_INDEX_CODE` **并重建生产的 qlib bin**，")
    print("    那会换掉生产模型的训练池。不是改一行配置的事。\n")


if __name__ == "__main__":
    raise SystemExit(main())
