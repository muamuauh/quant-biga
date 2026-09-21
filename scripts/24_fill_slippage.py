"""实测成交滑点：真实成交价 vs 回测假设的「次日开盘价」。

## 为什么这件事比再调一个参数值钱

回测引擎的成交假设是**次日开盘价**（`plan.md` §5.7）。整套结论都建在它上面，
而它**从来没有被实测过** —— 费率有 `fee_profile.yaml`，滑点有敏感性曲线，
只有"能按开盘价成交"这一条是白拿的。

它也不是一个可调参数：**它是一个乘在所有收益上的系统性偏移**。
`13_factor_zoo.py` 测到换手率单独解释了因子间净收益差异的 58%，而那条成本
公式里的滑点项现在填的是 10bp —— 一个猜的数。这个脚本把它换成实测值。

## 符号约定：正 = 吃亏

买入成交价高于开盘价、卖出低于开盘价，都记正。这样这个数可以直接填进回测的
`extra_slippage`，也可以直接和 `fee_profile` 的费率相加。

## 参照价必须用**不复权**开盘价

成交回报里的价格是原始盘口价。拿后复权价去比会算出一个和真实成交无关的数
（复权因子把历史价整体缩放过）。这里读的是 parquet 的 `open` 原始列。

## 取数

`--live` 连同花顺读今日成交（要求客户端开着、已登录）。这是目前唯一的取数
方式 —— `ths_client.read_tables` 从 2026-09-08 起会顺带读「今日成交」表，
所以数据会随日流程慢慢攒起来，但把历史成交从 `logs/qbg.jsonl` 回溯出来的
那条路还没做。

**样本少的时候这个数字没有意义** —— 单日的加权值会被一两笔大单主导。
至少攒够 20 笔再看，脚本会明说。

用法：
    python scripts/24_fill_slippage.py --live
    python scripts/24_fill_slippage.py --live --date 2026-09-08
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from qbg.config import settings  # noqa: E402
from qbg.data import cache  # noqa: E402
from qbg.market import codes as codes_mod  # noqa: E402
from qbg.portfolio.reconcile import fill_slippage, slippage_summary  # noqa: E402
from qbg.utils.console import make_output_safe  # noqa: E402

MIN_SAMPLE = 20


def reference_opens(codes: list[str], day: str) -> dict[str, float]:
    """`{6位代码: 当日不复权开盘价}`。

    键用**裸 6 位**：成交回报里的代码是 6 位的，而 parquet 用 `600519.SH`。
    在这里统一，免得比对时静默对不上（对不上会被当成"参照价拿不到"跳过，
    表现是样本莫名其妙地少）。
    """
    out: dict[str, float] = {}
    ts = pd.Timestamp(day)
    for code in codes:
        try:
            canon = codes_mod.normalize(code)
        except codes_mod.UnknownCodeError:
            continue
        df = cache.read(canon)
        if df.empty:
            continue
        hit = df[df["date"] == ts]
        if hit.empty:
            continue
        out[codes_mod.digits(canon)] = float(hit.iloc[0]["open"])
    return out


def main(argv=None) -> int:
    make_output_safe()
    p = argparse.ArgumentParser(description="实测成交滑点")
    p.add_argument("--live", action="store_true", help="连同花顺读今日成交")
    p.add_argument("--date", default=date.today().isoformat(), help="参照开盘价取哪天")
    args = p.parse_args(argv)

    if not args.live:
        print("目前只实现了 --live（连同花顺读今日成交）。", file=sys.stderr)
        print("要求：同花顺下单端开着并已登录，且今天有成交。", file=sys.stderr)
        return 2

    from qbg.portfolio.ths_client import read_tables

    tables = read_tables(exe=settings.qbg_ths_exe)
    trades = tables.get("trades")
    if trades is None:
        print("读不到「今日成交」表。同花顺版本可能不支持，或今天没有成交。",
              file=sys.stderr)
        return 1
    if not trades:
        print("今日成交为空 —— 今天没有成交，没什么可量的。")
        return 0

    codes = sorted({str(r.get("证券代码") or r.get("股票代码") or "").strip()
                    for r in trades} - {""})
    refs = reference_opens(codes, args.date)
    frame = fill_slippage(trades, refs)

    print(f"今日成交 {len(trades)} 笔，参照开盘价（{args.date}，不复权）"
          f"覆盖 {len(refs)}/{len(codes)} 只")
    if frame.empty:
        print("没有一笔能对上参照价 —— 检查 --date 是不是交易日、"
              "以及 parquet 里有没有当天的行情。", file=sys.stderr)
        return 1

    print(f"\n{'代码':<10}{'方向':>6}{'股数':>8}{'成交价':>10}{'开盘价':>10}"
          f"{'滑点bp':>10}{'金额':>12}")
    print("-" * 66)
    for r in frame.itertuples():
        print(f"{r.code:<10}{r.side:>6}{r.qty:>8}{r.fill_price:>10.3f}"
              f"{r.ref_price:>10.3f}{r.slippage_bp:>+10.1f}{r.notional:>12,.0f}")

    s = slippage_summary(frame)
    print(f"\n{'-' * 66}")
    print(f"按成交金额加权：{s['weighted_bp']:+.2f} bp   "
          f"中位数 {s['median_bp']:+.2f} bp   总金额 {s['notional']:,.0f} 元")
    for label, key in (("买入", "buy_bp"), ("卖出", "sell_bp")):
        if s[key] is not None:
            print(f"  {label} {s[key]:+.2f} bp")

    print(f"\n{'-' * 66}")
    if s["n"] < MIN_SAMPLE:
        print(f"⚠ 只有 {s['n']} 笔，**不足以下任何结论**（至少要 {MIN_SAMPLE} 笔）。")
        print("  每个交易日跑一次，攒够了再看这个数。单日的数被一两笔大单主导。")
    else:
        print(f"样本 {s['n']} 笔。可以拿加权值去替换回测里那个猜的 10bp：")
        print(f"  scripts/13_factor_zoo.py 等的 HEADLINE_SLIPPAGE = "
              f"{abs(s['weighted_bp']) / 1e4:.5f}")
    print("\n注意这个数**只覆盖你实际下过单的那些票**。冷门票、大单、封板附近的")
    print("成交都可能系统性更差，而它们恰恰是策略最想买的那一类。")
    print("所以实测值仍然是**下界**，不是保守估计。\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
