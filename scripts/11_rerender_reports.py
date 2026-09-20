"""用当前的渲染代码，从 `logs/qbg.jsonl` 重新生成历史日报。

为什么需要它：日报是**渲染产物**，而 `logs/qbg.jsonl` 才是真相源
（CLAUDE.md §三）。渲染代码修好之后，已经落盘的旧日报不会自己更新 ——
它们停留在生成那天的格式里。

具体触发这个工具的场景（2026-08-31）：2026-08-10 的日报里 TradingAgents 的
复核理由被砍到 180 字加个省略号，因为当时是用表格渲染的。08-25 改成了
「概览表 + 每票一块、理由完整展开」，但 08-10 那份文件还是旧的。
日志里存的是完整的 455~564 字，所以重渲染就能恢复 —— 什么都没丢。

**只重渲染，不重算。** 输入是当时那次运行的 `cycle.completed` 记录原样，
所以数字、评级、订单一个都不会变，变的只有排版。

用法：
    python scripts/11_rerender_reports.py --date 2026-08-10
    python scripts/11_rerender_reports.py --all
    python scripts/11_rerender_reports.py --all --dry-run    # 只看会改什么
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from qbg.config import settings  # noqa: E402
from qbg.report.daily_report import generate, render  # noqa: E402

TRUNCATION_MARK = "…"


def load_runs(log_path: Path) -> dict[str, dict]:
    """日期 → 那一天**最后一次** `cycle.completed`。

    同一天可能跑多次（补跑、手工重跑）。取最后一次，和当初落盘的日报一致 ——
    `generate` 每次都覆盖同名文件，所以最后写进去的就是最后一次的结果。
    """
    runs: dict[str, dict] = {}
    if not log_path.exists():
        return runs
    with log_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue        # 日志被截断/串行写坏的那一行，跳过就好
            if record.get("msg") == "cycle.completed" and record.get("date"):
                runs[str(record["date"])] = record
    return runs


def main(argv=None) -> int:
    make_output_safe()
    parser = argparse.ArgumentParser(description="从 JSONL 日志重渲染历史日报")
    parser.add_argument("--date", help="只重渲染这一天（YYYY-MM-DD）")
    parser.add_argument("--all", action="store_true", help="重渲染日志里的每一天")
    parser.add_argument("--dry-run", action="store_true", help="只报告差异，不写文件")
    parser.add_argument("--log", default=str(settings.log_dir / "qbg.jsonl"))
    args = parser.parse_args(argv)

    if not args.date and not args.all:
        parser.error("要么给 --date，要么给 --all")

    runs = load_runs(Path(args.log))
    if not runs:
        print(f"{args.log} 里没有 cycle.completed 记录", file=sys.stderr)
        return 1

    targets = sorted(runs) if args.all else [args.date]
    root = settings.report_dir / "daily"
    changed = unchanged = missing = 0

    for day in targets:
        record = runs.get(day)
        if record is None:
            print(f"  {day}  ✗ 日志里没有这一天的运行记录")
            missing += 1
            continue
        path = root / f"{day}.md"
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        after = render(record)
        if after == before:
            unchanged += 1
            continue
        changed += 1
        # 报告最有意义的那一项差异：省略号少了几个 = 恢复了多少处被截断的文本。
        cut_before, cut_after = before.count(TRUNCATION_MARK), after.count(TRUNCATION_MARK)
        note = f"{len(before):,} → {len(after):,} 字符"
        if cut_before > cut_after:
            note += f"；恢复 {cut_before - cut_after} 处被截断的文本"
        print(f"  {day}  {'(dry-run) ' if args.dry_run else ''}{note}")
        if not args.dry_run:
            generate(record, root)

    print(f"\n{'将改写' if args.dry_run else '已改写'} {changed} 份，"
          f"无变化 {unchanged} 份" + (f"，缺记录 {missing} 份" if missing else ""))
    if args.dry_run and changed:
        print("加 --all（去掉 --dry-run）真正写入。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
from qbg.utils.console import make_output_safe  # noqa: E402
