"""把 OOS 窗口拉长：测试段从 1.5 年变 2.6 / 3.6 年，看噪声底降多少。

## 为什么这是当前最该做的一件事

最近这一整轮实验，几乎每个结论都被"测试段只有 388 天"卡住：

    调仓频率      相位噪声 142 个百分点年化 → 测不出来
    多 horizon    同上
    混合权重      三个 k 打架，只能说"在噪声内"
    SMA200        k=3 交叉臂明确标注"不作判据"

**先有能分辨的尺子，再去测新东西。** 拉长测试段不需要任何新数据，只要把
train/valid 的分界往前挪、重训一次。

## 代价也要一起量

训练段会跟着变短（3.6 年方案只剩 2 年训练数据），模型可能变弱。所以这个
脚本同时报**模型质量**（Rank IC、k=3 净表现）和**尺子精度**（相位极差），
让人看清这笔交换：

    更长的测试段  →  结论更可信
    更短的训练段  →  模型可能更差

如果 Rank IC 掉得比噪声降得多，那就不值得。

## `fit_end_time` 必须跟着 train 段一起改

Alpha158 的预处理器（去极值/标准化）在 `fit_*` 区间上拟合。只改 segments
不改 `fit_end_time`，处理器就会在**测试段的数据上**拟合过 —— 一个安静的
信息泄漏，表现是测试指标虚高。

用法：
    python scripts/23_oos_window.py
    python scripts/23_oos_window.py --splits cur,long --seeds 1   # 快速试跑
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import yaml  # noqa: E402

from qbg.backtest import engine  # noqa: E402
from qbg.backtest import panel as panel_mod  # noqa: E402
from qbg.config import load_universe, settings  # noqa: E402
from qbg.model import train as train_mod  # noqa: E402
from qbg.strategy.predict import (  # noqa: E402
    load_latest_predictions,
    neutralize_frame,
    predictions_to_frame,
)
from qbg.strategy.regime import equal_weight_index, risk_on_series  # noqa: E402

SLIPPAGE = 0.001
EXPERIMENT_PREFIX = "cn_lgb_oos_"
# 相位极差用这两个间隔量：日频没有相位可言，间隔越长相位越多、
# 越能暴露"碰巧在哪些天下单"的那部分方差。
PHASE_EVERY = (3, 5)

# (名字, train 起, train 止, valid 止, test 止)。test 起 = valid 止 + 1 天。
SPLITS = {
    "cur":  ("2020-01-01", "2023-12-31", "2024-12-31", "2026-08-10"),
    "mid":  ("2020-01-01", "2022-12-31", "2023-12-31", "2026-08-10"),
    "long": ("2020-01-01", "2021-12-31", "2022-12-31", "2026-08-10"),
}


def _next_day(day: str) -> str:
    import datetime as dt

    return (dt.date.fromisoformat(day) + dt.timedelta(days=1)).isoformat()


def train_split(name: str, seeds: int | None, workdir: Path) -> str:
    tr_start, tr_end, va_end, te_end = SPLITS[name]
    cfg = yaml.safe_load(settings.workflow_yaml.read_text(encoding="utf-8"))
    dh = cfg["data_handler_config"]
    dh["fit_start_time"] = tr_start
    # **必须跟着 train 段走**，否则预处理器会在测试段上拟合（安静的泄漏）。
    dh["fit_end_time"] = tr_end
    cfg["task"]["dataset"]["kwargs"]["segments"] = {
        "train": [tr_start, tr_end],
        "valid": [_next_day(tr_end), va_end],
        "test": [_next_day(va_end), te_end],
    }
    path = workdir / f"workflow_{name}.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    experiment = f"{EXPERIMENT_PREFIX}{name}"
    train_mod.train(workflow_yaml=path, experiment_name=experiment, seed_count=seeds)
    return experiment


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="OOS 窗口长度实验")
    p.add_argument("--splits", default="cur,mid,long")
    p.add_argument("--seeds", type=int, default=None)
    p.add_argument("--k", type=int, default=settings.qbg_top_k)
    p.add_argument("--reuse", action="store_true")
    args = p.parse_args(argv)

    names = [x.strip() for x in args.splits.split(",") if x.strip()]
    workdir = ROOT / "data" / "oos_window"
    workdir.mkdir(parents=True, exist_ok=True)
    members = load_universe()

    # 择时按实盘配置叠上去。**信号在完整历史上算**，不能先截到测试段
    # （min_periods=sma_window，截断会让长 SMA 静默退化成"不择时"）。
    full_level = equal_weight_index(
        members, use_inclusion=bool(int(settings.qbg_market_index_eligible)))

    rows = []
    for name in names:
        experiment = f"{EXPERIMENT_PREFIX}{name}"
        pred = None
        if args.reuse:
            try:
                pred = load_latest_predictions(experiment)
                print(f"[{name}] 复用 {experiment}", flush=True)
            except Exception:  # noqa: BLE001
                pred = None
        if pred is None:
            tr_start, tr_end, va_end, te_end = SPLITS[name]
            print(f"[{name}] 训练中：train {tr_start}~{tr_end}  "
                  f"valid ~{va_end}  test ~{te_end}", flush=True)
            t0 = time.time()
            train_split(name, args.seeds, workdir)
            print(f"[{name}] 完成 ({time.time() - t0:.0f}s)", flush=True)
            pred = load_latest_predictions(experiment)

        frame = predictions_to_frame(pred)
        if settings.qbg_industry_neutral:
            frame = neutralize_frame(frame)
        pnl = panel_mod.build_panel(members, start=str(frame.index.min().date()),
                                    end=str(frame.index.max().date()))
        scores = frame.reindex(index=pnl.dates, columns=pnl.instruments)
        if settings.qbg_market_sma:
            on = (risk_on_series(full_level, settings.qbg_market_sma,
                                 float(settings.qbg_market_sma_band))
                  .reindex(pnl.dates).ffill().fillna(True))
            scores = scores.where(on)

        base = engine.run_backtest(scores, pnl, k=args.k,
                                   extra_slippage=SLIPPAGE, slippage_grid=())
        spreads = {}
        for every in PHASE_EVERY:
            annuals = [engine.run_backtest(
                scores, pnl, k=args.k, rebalance_every=every, rebalance_phase=ph,
                extra_slippage=SLIPPAGE, slippage_grid=()).strategy.annual_return
                for ph in range(every)]
            spreads[every] = max(annuals) - min(annuals)

        rows.append({"name": name, "days": len(pnl.dates),
                     "start": pnl.dates[0].date(), "end": pnl.dates[-1].date(),
                     "annual": base.strategy.annual_return, "sharpe": base.strategy.sharpe,
                     "mdd": base.strategy.max_drawdown, "rank_ic": base.rank_ic,
                     "spreads": spreads})
        print(f"  {name}: {len(pnl.dates)} 日  年化 {base.strategy.annual_return:+.2%}  "
              f"夏普 {base.strategy.sharpe:.2f}  RankIC {base.rank_ic:+.4f}  "
              f"相位极差 " + " / ".join(f"每{e}日 {s:.1%}" for e, s in spreads.items()),
              flush=True)

    _report(rows, args)
    return 0


def _report(rows, args) -> None:
    print("\n" + "=" * 94)
    print(f"OOS 窗口长度实验   k={args.k}   滑点 {SLIPPAGE * 1e4:.0f}bp   "
          f"择时 SMA{settings.qbg_market_sma} 缓冲{settings.qbg_market_sma_band:.0%}")
    print("=" * 94)
    print(f"{'方案':>6}{'训练段':>14}{'测试段':>24}{'天数':>7}"
          f"{'净年化':>11}{'夏普':>8}{'RankIC':>10}"
          + "".join(f"{f'每{e}日极差':>11}" for e in PHASE_EVERY))
    print("-" * 94)
    for r in rows:
        tr_start, tr_end, _, _ = SPLITS[r["name"]]
        tag = "  ← 现行" if r["name"] == "cur" else ""
        print(f"{r['name']:>6}{tr_start[:4] + '~' + tr_end[:4]:>14}"
              f"{str(r['start']) + '~' + str(r['end']):>24}{r['days']:>7}"
              f"{r['annual']:>11.2%}{r['sharpe']:>8.2f}{r['rank_ic']:>+10.4f}"
              + "".join(f"{r['spreads'][e]:>11.1%}" for e in PHASE_EVERY) + tag)

    print("\n" + "-" * 94)
    print("怎么读")
    print("-" * 94)
    print("  · **「极差」越小，这把尺子越能分辨东西。** 那两列才是这个实验的产出，")
    print("    净年化和夏普只是用来确认模型没被更短的训练段毁掉。")
    print("  · 净年化在不同测试段之间**不可直接比**：区间不同、行情不同。")
    print("    要看的是 Rank IC 掉了多少 —— 那是模型质量的同口径度量。")
    print("  · 若 Rank IC 掉幅小而极差降幅大 → 换。反之训练数据不够，别换。")
    print(f"\n实验模型写在 `{EXPERIMENT_PREFIX}*`，**没有碰生产用的 `cn_lgb`**。")
    print("要换生产分段得手动改 configs/workflow_cn_lgb.yaml 的 segments +")
    print("fit_end_time（两处必须一起改），再重训。\n")


if __name__ == "__main__":
    raise SystemExit(main())
