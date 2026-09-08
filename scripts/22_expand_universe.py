"""扩股票池：把另一个指数的成分拉进 parquet 缓存，写独立的 universe 文件。

## 为什么写独立文件而不是改 `QBG_INDEX_CODE`

改配置会**立刻改变日流程实际交易的股票池**，而扩池的效果还没测过。
这里的产物是 `configs/universe_<code>.txt`，回测脚本用 `--codes` 或
`load_universe` 的替代路径读它，生产读的 `universe_hs300.txt` 一个字不动。
测完、过闸、人工确认之后再谈切换。

**parquet 缓存是按股票分文件的**，所以往里加票是纯增量操作：
沪深300 的那 299 个文件不受任何影响，任何只读 `universe_hs300.txt` 的
流程也看不到变化。

## 顺序不能反

`filter_members`（ST / 次新 / 停牌 / 北交所）**从本地缓存判断**，所以必须
先拉数再过滤。反过来做的话新票全都没有缓存，会被"上市天数不足"一刀切光。

## 中小盘的两个已知代价

  · **纳入前视严重得多**：中证500 有 78% 的当前成分是 2020 年后才纳入的
    （中证1000 是 76%，沪深300 只有 47%）。资格表机制能修，但修正后 2020 年
    的中证500 只剩约 110 只可用 —— 早期样本很薄。
  · **滑点假设要重估**：全套结论建在 10bp 上，而换手率解释了因子间 58% 的
    收益差异。用 10bp 测中证1000 是自欺欺人，起码 20~30bp 重跑一遍。

用法：
    python scripts/22_expand_universe.py --index 000905          # 中证500
    python scripts/22_expand_universe.py --index 000905 --dry-run
    python scripts/22_expand_universe.py --index 000852 --limit 200
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qbg.data import cache  # noqa: E402
from qbg.data import universe as universe_mod  # noqa: E402
from qbg.utils.logging import get_logger, log_event  # noqa: E402

log = get_logger("qbg.scripts.expand")

INDEX_NAMES = {"000300": "沪深300", "000905": "中证500", "000852": "中证1000"}


def universe_path(index_code: str) -> Path:
    return ROOT / "configs" / f"universe_{index_code}.txt"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="扩股票池：拉另一个指数的成分")
    p.add_argument("--index", required=True, help="指数代码，如 000905 / 000852")
    p.add_argument("--limit", type=int, default=0, help="只拉前 N 只（试跑用）")
    p.add_argument("--dry-run", action="store_true", help="只报告要拉多少，不拉")
    p.add_argument("--batch", type=int, default=60,
                   help="每批多少只。BaoStock 会话是全局的，分批是为了让失败"
                        "只影响一批而不是整次运行")
    args = p.parse_args(argv)

    name = INDEX_NAMES.get(args.index, args.index)
    members = universe_mod.fetch_index_constituents(args.index)
    if not members:
        print(f"拿不到 {name}({args.index}) 的成分股。", file=sys.stderr)
        return 1

    # 纳入日期单独缓存一份（文件名带指数代码，见 universe._inclusion_file）。
    # 它是资格表的输入，扩池之后修生存者偏差全靠它。
    incl = universe_mod.inclusion_dates(args.index)
    cached = {c for c in members if not cache.read(c).empty}
    todo = [c for c in members if c not in cached]
    if args.limit:
        todo = todo[: args.limit]

    print(f"{name}({args.index})：成分 {len(members)} 只，"
          f"已有缓存 {len(cached)} 只，待拉 {len(todo)} 只")
    print(f"纳入日期覆盖 {len(incl)}/{len(members)} 只")
    if incl:
        after = sum(1 for v in incl.values() if v >= "2020-01-01")
        print(f"  其中 2020-01-01 之后才纳入的：{after} 只（{after / len(incl):.0%}）"
              f" ← 纳入前视的量级")
    if args.dry_run:
        print("\n--dry-run：不拉数。")
        return 0

    ok = 0
    for i in range(0, len(todo), args.batch):
        batch = todo[i: i + args.batch]
        t0 = time.time()
        # 走 01_ingest.py 而不是直接调 SourceChain：降级链、增量边界、复权因子
        # 修复那一整套逻辑都在它里面，绕过去等于维护第二份实现。
        # --skip-meta / --no-qlib：元数据和 qlib bin 最后统一做一次。
        rc = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "01_ingest.py"),
             "--codes", ",".join(batch), "--skip-meta", "--no-qlib"],
            cwd=ROOT).returncode
        got = sum(1 for c in batch if not cache.read(c).empty)
        ok += got
        print(f"  批 {i // args.batch + 1}/{-(-len(todo) // args.batch)}："
              f"{got}/{len(batch)} 只落地  rc={rc}  ({time.time() - t0:.0f}s)",
              flush=True)

    # **过滤必须在拉数之后**：ST / 上市天数都从本地缓存判断。
    report = universe_mod.filter_members(members)
    kept = report.kept if hasattr(report, "kept") else report.members
    path = universe_path(args.index)
    header = (
        f"# 由 scripts/22_expand_universe.py 生成 —— 请勿手工编辑\n"
        f"# 指数 {args.index}（{name}）成分，已过滤 ST / 次新 / 停牌 / 北交所\n"
        f"# 生成于 {date.today().isoformat()}，共 {len(kept)} 只\n"
        f"# **生产读的是 configs/universe_hs300.txt，这个文件只给回测用。**\n"
        f"# 纳入日期见 data/snapshots/universe/inclusion_dates_{args.index}.json\n"
    )
    path.write_text(header + "\n".join(sorted(kept)) + "\n", encoding="utf-8")

    print(f"\n落地 {ok}/{len(todo)} 只；过滤后保留 {len(kept)}/{len(members)} 只")
    print(f"写入 {path.relative_to(ROOT)}")
    print("\n下一步：qlib bin 要重建才能训练 —— "
          "python scripts/01_ingest.py --skip-meta")
    log_event(log, "expand.done", index=args.index, fetched=ok,
              kept=len(kept), total=len(members))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
