"""拉取 A股日线并落地：数据源 → parquet 增量缓存 → 股票池 → qlib bin。

    python scripts/01_ingest.py                 # 日常增量（秒级）
    python scripts/01_ingest.py --full          # 忽略缓存全量重拉
    python scripts/01_ingest.py --codes 600519.SH,000858.SZ   # 只拉指定的票
    python scripts/01_ingest.py --skip-meta     # 跳过元数据刷新（日历/名称/行业）
    python scripts/01_ingest.py --no-qlib       # 只更新 parquet，不导 qlib

首次运行会拉沪深300 × 5 年，取决于网络约几十分钟；之后每天只拉新增的
几行，秒级完成。这就是增量缓存存在的全部意义。

## 并发

默认串行。**BaoStock 是主源，而它的会话是全局的、不是线程安全的**
（并发查询会让响应交错、数据互相串台且不报错）。`--workers N` 只在
主源不是 baostock 时才有意义，所以默认关掉。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# 允许直接 `python scripts/01_ingest.py` 而不必先装包
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.config import load_universe, settings  # noqa: E402
from qbg.data import cache, industry, meta, qlib_dump, universe  # noqa: E402
from qbg.data.sources.chain import SourceChain  # noqa: E402
from qbg.market import calendar  # noqa: E402
from qbg.utils.logging import get_logger, log_event  # noqa: E402

log = get_logger("qbg.scripts.ingest")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="拉取 A股日线并落地")
    p.add_argument("--full", action="store_true", help="忽略缓存，全量重拉")
    p.add_argument("--codes", default="", help="只拉这些票（逗号分隔），默认全池")
    p.add_argument("--start", default=None, help="起始日期，默认 QBG_HISTORY_START")
    p.add_argument("--end", default=None, help="结束日期，默认今天")
    p.add_argument("--skip-meta", action="store_true",
                   help="跳过日历/代码名称/行业的刷新（它们变得很慢）")
    p.add_argument("--no-qlib", action="store_true", help="不导出 qlib bin")
    p.add_argument("--refresh-tail-days", type=int,
                   default=cache.DEFAULT_REFRESH_TAIL_DAYS,
                   help="重拉尾部多少天，让 T+1 的数据修正能进来")
    return p.parse_args(argv)


def refresh_metadata(chain: SourceChain) -> dict:
    """刷新交易日历、代码名称表、申万行业。全部 fail-soft 到缓存。"""
    cal = calendar.refresh(chain)
    names = meta.refresh()
    inds = industry.refresh()
    return {"calendar_days": len(cal), "names": len(names), "industries": len(inds)}


def resolve_members(args) -> tuple[list[str], bool]:
    """决定这次要拉哪些票，以及是否需要重建股票池文件。

    首次运行（还没有 universe 文件）时先去拉指数成分——否则没有票可拉，
    整个 ingest 会空转，而且不会有任何提示。
    """
    if args.codes:
        return [c.strip() for c in args.codes.split(",") if c.strip()], False

    existing = load_universe()
    if existing:
        # 已有池子：拉它，同时也拉当前指数成分（可能有新进的票），
        # 这样过滤时新成分已经有数据可判断。
        fresh = universe.fetch_index_constituents()
        return sorted(set(existing) | set(fresh)), True

    fresh = universe.fetch_index_constituents()
    if not fresh:
        log_event(log, "ingest.no_universe",
                  hint="拉不到指数成分且本地无 universe 文件，检查网络或用 --codes")
    return fresh, True


def ingest_bars(members: list[str], chain: SourceChain, args) -> dict:
    """逐只更新 parquet 缓存，返回统计。"""
    stats = {"ok": 0, "empty": 0, "degraded": 0, "added_rows": 0, "problems": {}}
    t0 = time.time()

    for i, code in enumerate(members, 1):
        try:
            summary = cache.update(
                code, chain,
                start=args.start, end=args.end,
                refresh_tail_days=args.refresh_tail_days,
                force_full=args.full,
            )
        except Exception as e:  # noqa: BLE001 — 一只票的意外不该中断整批
            log_event(log, "ingest.code_error", code=code, error=str(e)[:200])
            stats["empty"] += 1
            continue

        if summary["empty_fetch"]:
            stats["empty"] += 1
        else:
            stats["ok"] += 1
        stats["degraded"] += int(summary["degraded"])
        stats["added_rows"] += max(summary["added"], 0)

        # 自洽校验：抓那些不抛异常但会毁掉下游的静默错误。
        # repair=False：要看源的原样，否则因子递减这条永远检不出来
        # （read() 默认已经把它修掉了，见 cache.repair_factor）。
        problems = cache.verify(cache.read(code, repair=False))
        if problems:
            stats["problems"][code] = problems
            log_event(log, "ingest.verify_problems", code=code, problems=problems)

        if i % 25 == 0 or i == len(members):
            log_event(log, "ingest.progress", done=i, total=len(members),
                      elapsed_sec=round(time.time() - t0, 1))

    stats["elapsed_sec"] = round(time.time() - t0, 1)
    return stats


def main(argv=None) -> int:
    args = parse_args(argv)
    chain = SourceChain()

    log_event(log, "ingest.start", sources=chain.names, full=args.full,
              start=args.start or settings.qbg_history_start)

    meta_stats = {}
    if not args.skip_meta:
        meta_stats = refresh_metadata(chain)

    members, rebuild_universe = resolve_members(args)
    if not members:
        log_event(log, "ingest.abort", reason="没有可拉取的股票")
        print("没有可拉取的股票。检查网络，或用 --codes 指定。", file=sys.stderr)
        return 1

    bar_stats = ingest_bars(members, chain, args)

    filtered = None
    if rebuild_universe:
        rep = universe.filter_members(members)
        universe.save_snapshot(rep.kept)          # 带日期，缓解生存者偏差
        universe.write_universe_file(rep.kept)
        filtered = rep

    qlib_result = None
    if not args.no_qlib and filtered is not None and filtered.kept:
        qlib_result = qlib_dump.dump(filtered.kept)

    log_event(log, "ingest.done", meta=meta_stats, bars=bar_stats,
              universe=filtered.as_dict() if filtered else None)

    _print_report(meta_stats, bar_stats, filtered, qlib_result)
    return 0


def _print_report(meta_stats, bar_stats, filtered, qlib_result) -> None:
    """给人看的收尾摘要。日志是给机器看的，这个是给你看的。"""
    print("\n" + "=" * 58)
    print("ingest 完成")
    print("=" * 58)
    if meta_stats:
        print(f"元数据    交易日 {meta_stats['calendar_days']} · "
              f"代码名称 {meta_stats['names']} · 行业 {meta_stats['industries']}")
    print(f"行情      成功 {bar_stats['ok']} · 无数据 {bar_stats['empty']} · "
          f"降级 {bar_stats['degraded']} · 新增 {bar_stats['added_rows']} 行 · "
          f"{bar_stats['elapsed_sec']}s")

    if bar_stats["problems"]:
        print(f"\n⚠ {len(bar_stats['problems'])} 只票的数据未通过自洽校验：")
        for code, probs in list(bar_stats["problems"].items())[:10]:
            print(f"    {code}: {'; '.join(probs)}")

    if filtered:
        d = filtered.as_dict()
        print(f"\n股票池    保留 {d['kept']} 只")
        print(f"          剔除 ST {d['dropped_st']} · 次新 {d['dropped_too_new']} · "
              f"停牌 {d['dropped_suspended']} · 北交所 {d['dropped_bse']} · "
              f"无数据 {d['dropped_no_data']}")
        print(f"          → {settings.universe_file}")

    if qlib_result:
        b = qlib_result["bin"]
        if b.get("ok"):
            print(f"\nqlib      已导出 → {settings.qlib_provider_uri}")
        else:
            print(f"\nqlib      未导出（{b.get('reason', 'dump_bin 失败')}）")
            if b.get("hint"):
                print("          " + b["hint"].replace("\n", "\n          "))
    print()


if __name__ == "__main__":
    raise SystemExit(main())
