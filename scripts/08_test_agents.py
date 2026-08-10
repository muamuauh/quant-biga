from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.agents.review import review_candidates  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="P7 单票在线复核与评级稳定性测试")
    parser.add_argument("code", help="A 股代码，如 600519.SH")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 4))
    args = parser.parse_args(argv)
    trade_date = date.fromisoformat(args.date)
    runs = []
    for index in range(args.repeat):
        kept, verdicts, usage = review_candidates({args.code: 1.0}, trade_date=trade_date)
        verdict = verdicts[0]
        runs.append({
            "run": index + 1,
            "code": verdict.code,
            "rating": verdict.rating,
            "kept": verdict.kept,
            "error": verdict.error,
            "rationale_preview": verdict.rationale[:500],
            "usage": usage,
            "kept_codes": sorted(kept),
        })
    ratings = Counter(item["rating"] for item in runs)
    result = {"runs": runs, "rating_counts": dict(ratings),
              "stable": len(ratings) == 1 and "Error" not in ratings}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if all(item["error"] is None for item in runs) else 2


if __name__ == "__main__":
    raise SystemExit(main())
