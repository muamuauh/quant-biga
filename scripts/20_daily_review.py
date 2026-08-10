from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.agent.review import collect_facts, daily_review  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="确定性事实层 + 第三方中转站每日复盘")
    parser.add_argument("--date", default=None)
    parser.add_argument("--mode", default="ADVISORY")
    parser.add_argument("--facts-only", action="store_true", help="不调用 LLM，只输出本地事实")
    parser.add_argument("--synthetic", action="store_true",
                        help="用临时空库测试完整在线链路，不发送真实账户/订单数据")
    args = parser.parse_args(argv)
    if args.facts_only:
        _, facts = collect_facts(args.date, mode=args.mode)
        print(json.dumps(facts, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.synthetic:
        temp_parent = Path(__file__).resolve().parents[1] / "reports"
        temp_parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="qbg-review-", dir=temp_parent) as tmp:
            root = Path(tmp)
            result = daily_review(args.date or "2099-01-01", mode=args.mode,
                                  db_path=root / "synthetic.db", report_dir=root / "review")
            payload = result.as_dict()
            payload["report_exists_before_cleanup"] = bool(
                result.report_path and Path(result.report_path).exists()
            )
            payload["report_path"] = "<temporary>" if result.report_path else None
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0 if result.ok else 2
    result = daily_review(args.date, mode=args.mode)
    print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
