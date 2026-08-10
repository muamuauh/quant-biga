"""南京证券持仓截图 → 经强校验的 positions.csv。"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.portfolio.ocr_source import (  # noqa: E402
    PortfolioValidationError,
    read_images,
    require_valid,
    save_snapshot,
)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="持仓截图 OCR（截图会发送到配置的 LLM）")
    parser.add_argument("screenshots", nargs="+", type=Path)
    parser.add_argument("--yes", action="store_true", help="跳过 y/N 确认")
    parser.add_argument("--dry-run", action="store_true", help="只解析校验，不写文件")
    parser.add_argument("--asof", type=date.fromisoformat,
                        help="截图日期 YYYY-MM-DD；截图不显示日期时应显式提供")
    parser.add_argument("--json", action="store_true",
                        help="机器模式：只输出一个 JSON；必须配 --dry-run 或 --yes")
    args = parser.parse_args(argv)
    if args.json:
        logging.disable(logging.CRITICAL)
    if args.json and not (args.dry_run or args.yes):
        print(json.dumps({"ok": False, "error": "json_requires_dry_run_or_yes"},
                         ensure_ascii=False))
        return 2
    missing = [str(path) for path in args.screenshots if not path.is_file()]
    if missing:
        if args.json:
            print(json.dumps({"ok": False, "error": "screenshots_missing", "missing": missing},
                             ensure_ascii=False))
        else:
            print("截图不存在：" + "、".join(missing), file=sys.stderr)
        return 2
    payload = read_images(args.screenshots)
    if args.asof:
        payload["asof"] = args.asof.isoformat()
    try:
        snapshot, warnings = require_valid(payload)
    except PortfolioValidationError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": "validation_failed",
                              "issues": [issue.__dict__ for issue in exc.issues]},
                             ensure_ascii=False))
        else:
            print("校验失败，拒绝写入：", file=sys.stderr)
            for issue in exc.issues:
                print(f"- {issue.code} {issue.field}: {issue.message}", file=sys.stderr)
        return 3
    frame = __import__("pandas").DataFrame([p.as_dict() for p in snapshot.positions])
    result = {"ok": True, "dry_run": args.dry_run, "asof": snapshot.asof,
              "total_equity": snapshot.total_equity,
              "available_cash": snapshot.available_cash,
              "positions": [p.as_dict() for p in snapshot.positions],
              "warnings": [warning.__dict__ for warning in warnings], "written": False}
    if not args.json:
        print(frame.to_string(index=False))
        print(f"总资产={snapshot.total_equity:.2f} 可用资金={snapshot.available_cash:.2f}")
        for warning in warnings:
            print(f"警告：{warning.message}")
    if args.dry_run:
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        return 0
    if not args.yes and input("确认写入持仓 CSV？[y/N] ").strip().lower() != "y":
        print("已取消，未写入任何文件。")
        return 1
    current, history = save_snapshot(snapshot)
    result.update(written=True, current=str(current), history=str(history))
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"已写入 {current} 和 {history}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
