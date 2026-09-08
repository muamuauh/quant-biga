"""多 horizon 标签实验 —— 模型的 alpha 到底有几天？

## 为什么要做

现行标签是 `Ref($open,-2)/Ref($open,-1)-1`，也就是**持有一天**。这个选择
从来没被验证过：它是 §5.7 为了对齐"盘后出信号、次日开盘执行"而定的成交
口径，顺手也定了预测期限 —— 这两件事其实是独立的。成交在次日开盘，不代表
第三天开盘就非卖不可。

`12_rebalance_gate.py` 曾经测出"每日调仓最好"，据此保留了 `REBALANCE_EVERY=1`。
**那次测试有个盲点**：它拿一个用 1 日标签训出来的模型去做 5 日、10 日调仓。
模型学的是明天的收益，你却让它管未来两周 —— 输是必然的，那不能说明
"日频更优"，只能说明**信号和持有期不匹配**。

`15_turnover_sweep.py` 给了一条旁证：`bias20` 的**毛**年化（@0bp，已排除
成本因素）从每日调仓的 +37.45% 涨到「每 3 日 + 迟滞10」的 +48.23%。
少交易不只是省了钱，信号本身也变好了 —— 说明那个信号的期限本来就比一天长。

## 这个实验怎么判

每个 horizon **训一个自己的模型**，然后**用匹配的持有期回测**。判据是
净年化和夏普，**不是 Rank IC** —— 引擎的 `_compute_ic` 永远对齐 1 日前向
收益，所以那一列问的是"对明天准不准"，h 日模型在上面机械地偏低。横着比
等于用 1 日模型的考卷考所有人。

同时把每个模型放到**别的**持有期上跑（`--every` 交叉网格），用来把两件事
分开：**是标签配对了，还是只是少交易了。** 只看匹配行分不清这两者。

用法：
    python scripts/16_horizon_sweep.py                 # 训 1/3/5/10 日四个模型
    python scripts/16_horizon_sweep.py --reuse         # 已训过就直接读
    python scripts/16_horizon_sweep.py --horizons 1,5 --seeds 1   # 快速试跑
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

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

HEADLINE_SLIPPAGE = 0.001   # 与 13/15 同一口径
# 实验用的 MLflow experiment 前缀。**不写进 `cn_lgb`** —— 那是实盘每天读的
# 那一个，被这里的实验模型覆盖会静默地换掉生产信号。
EXPERIMENT_PREFIX = "cn_lgb_h"


def label_for(horizon: int) -> str:
    """持有 `horizon` 个交易日的开盘到开盘收益。

    h=1 时退化成现行标签。分子的 `-(1 + h)` 不能写成 `-h`：买入发生在
    `Ref($open,-1)`（次日开盘），所以持有 h 天后的卖出点是第 `1+h` 个开盘。
    差一格就会把 h 日模型训成 h-1 日模型，而且没有任何报错。
    """
    return f"Ref($open,-{1 + horizon})/Ref($open,-1)-1"


def train_horizon(horizon: int, seeds: int | None, workdir: Path) -> str:
    """按给定 horizon 改标签后训练，返回 experiment 名。"""
    cfg = yaml.safe_load(settings.workflow_yaml.read_text(encoding="utf-8"))
    cfg["data_handler_config"]["label"] = [label_for(horizon)]
    path = workdir / f"workflow_h{horizon}.yaml"
    path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                    encoding="utf-8")
    experiment = f"{EXPERIMENT_PREFIX}{horizon}"
    train_mod.train(workflow_yaml=path, experiment_name=experiment, seed_count=seeds)
    return experiment


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="多 horizon 标签实验")
    p.add_argument("--horizons", default="1,3,5,10")
    p.add_argument("--seeds", type=int, default=None, help="集成 seed 数，默认用配置值")
    p.add_argument("--k", type=int, default=settings.qbg_top_k)
    p.add_argument("--keep-rank", type=int, default=0, help="回测时的迟滞名次")
    p.add_argument("--every", default="",
                   help="持有期网格，逗号分隔；总是自动补上每个 h 自己。"
                        "留空 = 只跑 {h, 1}")
    p.add_argument("--reuse", action="store_true", help="experiment 已存在就不重训")
    p.add_argument("--no-neutralize", action="store_true")
    args = p.parse_args(argv)

    horizons = [int(x) for x in args.horizons.split(",") if x.strip()]
    every_grid = [int(x) for x in args.every.split(",") if x.strip()]
    workdir = Path(settings.mlruns_dir).parent / "data" / "horizon_sweep"
    workdir.mkdir(parents=True, exist_ok=True)

    frames = {}
    for h in horizons:
        experiment = f"{EXPERIMENT_PREFIX}{h}"
        if args.reuse:
            try:
                pred = load_latest_predictions(experiment)
                print(f"[h={h}] 复用已有 experiment {experiment}", flush=True)
            except Exception:  # noqa: BLE001 — 没训过就训
                pred = None
        else:
            pred = None
        if pred is None:
            print(f"[h={h}] 训练中，标签 = {label_for(h)}", flush=True)
            t0 = time.time()
            train_horizon(h, args.seeds, workdir)
            print(f"[h={h}] 训练完成 ({time.time() - t0:.0f}s)", flush=True)
            pred = load_latest_predictions(experiment)
        frame = predictions_to_frame(pred)
        if not args.no_neutralize and settings.qbg_industry_neutral:
            frame = neutralize_frame(frame)
        frames[h] = frame

    # 所有 horizon 共用同一个面板和同一段区间，否则比较无效。
    start = min(str(f.index.min().date()) for f in frames.values())
    end = max(str(f.index.max().date()) for f in frames.values())
    pnl = panel_mod.build_panel(load_universe(), start=start, end=end)
    print(f"\n回测区间 {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   "
          f"{len(pnl.dates)} 日   k={args.k}   迟滞={args.keep_rank}\n", flush=True)

    rows, bench = [], None
    for h, frame in frames.items():
        scores = frame.reindex(index=pnl.dates, columns=pnl.instruments)
        # 持有期网格总是包含 h 自己（匹配行）和 1（每日基准）。
        # **交叉跑是必须的**：只看匹配行分不清 h 日模型赢在"标签对了"还是
        # "换手低了" —— 要拿 1 日模型也做 h 日调仓才能把两者分开。
        for every in sorted(set(every_grid) | {h, 1}):
            res = engine.run_backtest(
                scores, pnl, k=args.k, rebalance_every=every,
                keep_rank=args.keep_rank, extra_slippage=HEADLINE_SLIPPAGE,
                slippage_grid=(0.0, HEADLINE_SLIPPAGE))
            rows.append({
                "h": h, "every": every, "matched": every == h,
                "annual": res.strategy.annual_return,
                "annual_nofric": res.slippage_curve[0.0].annual_return,
                "sharpe": res.strategy.sharpe,
                "mdd": res.strategy.max_drawdown,
                "turnover": res.avg_turnover,
                "rank_ic": res.rank_ic,
            })
            bench = bench or res.benchmark
            print(f"  h={h:<2} 持有={every:<2} → 净年化 {res.strategy.annual_return:+8.2%}",
                  flush=True)

    _report(rows, bench, pnl, args)
    return 0


def _report(rows, bench, pnl, args) -> None:
    print("\n" + "=" * 92)
    print(f"多 horizon 标签实验   {pnl.dates[0].date()} ~ {pnl.dates[-1].date()}   "
          f"净年化已扣 基础费率 + {HEADLINE_SLIPPAGE * 1e4:.0f}bp 滑点")
    print("=" * 92)
    print(f"等权买入持有（不换手）: 年化 {bench.annual_return:+.2%}   "
          f"夏普 {bench.sharpe:.2f}   回撤 {bench.max_drawdown:.2%}")

    print(f"\n{'标签':>6}{'持有':>6}{'':>4}{'净年化':>10}{'@0bp':>10}{'夏普':>8}"
          f"{'回撤':>10}{'换手':>8}{'RankIC':>10}")
    print("-" * 74)
    for r in rows:
        mark = " ←匹配" if r["matched"] else "     "
        star = "*" if r["annual"] > bench.annual_return else " "
        print(f"{star}{r['h']:>5}日{r['every']:>5}日{mark}{r['annual']:>10.2%}"
              f"{r['annual_nofric']:>10.2%}{r['sharpe']:>8.2f}{r['mdd']:>10.2%}"
              f"{r['turnover']:>8.3f}{r['rank_ic']:>+10.4f}")

    print("\n" + "-" * 92)
    print("怎么读")
    print("-" * 92)
    print("  · 只比「←匹配」那几行 —— 每个模型配它自己的持有期，这才是公平比较。")
    print("  · **不要横向比 Rank IC。** 引擎的 `_compute_ic` 永远拿**1 日**前向收益")
    print("    对齐（`asset_ret.shift(-1)`），所以这一列问的是「这个模型对明天准不准」。")
    print("    h 日模型本来就不为明天优化，在这一列上机械地偏低 —— 横着比等于")
    print("    用 1 日模型的考卷去考所有人。它只在同一 horizon 内部有意义。")
    print("  · 同一个 h 的「匹配」和「持有=1」两行之差，回答的是：拉长持有期的")
    print("    好处里，多少来自模型本身更准，多少只是来自少交易。")
    print("  · 若 h>1 的匹配行明显更好 → `12_rebalance_gate.py` 的「日频最优」")
    print("    结论要推翻，因为那次是拿 1 日模型去做低频调仓，信号和持有期错配。")
    print("  · 测试段只有约 1.5 年，且股票池是**当前**沪深300 成分（生存者偏差）。")
    print("    这里的绝对收益不可当预期，horizon 之间的相对比较才是结论。")
    print(f"\n实验模型写在 experiment `{EXPERIMENT_PREFIX}*`，**没有碰生产用的 "
          f"`cn_lgb`**。要换生产标签得手动改 configs/workflow_cn_lgb.yaml 并重训。")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
