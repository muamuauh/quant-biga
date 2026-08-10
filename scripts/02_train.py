"""训练 Alpha158 + LightGBM 多 seed 集成。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.model.train import train  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="训练 A股 qlib 模型")
    parser.add_argument("--live", action="store_true", help="滚动窗口到最新交易日")
    parser.add_argument("--seeds", type=int, default=None, help="覆盖集成 seed 数")
    args = parser.parse_args(argv)
    recorder_id = train(live=args.live, seed_count=args.seeds)
    print(f"训练完成，recorder_id={recorder_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

