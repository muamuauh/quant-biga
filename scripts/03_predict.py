"""读取最新模型预测，输出可供 P4 下单规划使用的候选 CSV。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.config import settings  # noqa: E402
from qbg.strategy.predict import latest_date_scores, load_latest_predictions  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="导出最新 A股模型分数")
    parser.add_argument("--experiment", default="cn_lgb")
    parser.add_argument("--no-neutralize", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    pred = load_latest_predictions(args.experiment)
    scores = latest_date_scores(
        pred, neutralize=bool(settings.qbg_industry_neutral and not args.no_neutralize)
    )
    latest = pred.index.get_level_values("datetime").max()
    output = args.output or settings.snapshot_dir / f"scores_{latest:%Y%m%d}.csv"
    output.parent.mkdir(parents=True, exist_ok=True)
    scores.rename("score").to_csv(output, index_label="code")
    print(scores.head(settings.qbg_agents_candidates).to_string())
    print(f"\n已写入 {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

