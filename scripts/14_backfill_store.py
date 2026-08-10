from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.config import settings  # noqa: E402
from qbg.store.etl import backfill  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rebuild", action="store_true")
    args = parser.parse_args(argv)
    if args.rebuild and settings.db_path.exists():
        settings.db_path.unlink()
    print(backfill())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

