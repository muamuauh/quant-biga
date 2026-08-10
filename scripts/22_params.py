from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.tuning.overlay import read, rollback  # noqa: E402
from qbg.tuning.whitelist import load  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    rb = sub.add_parser("rollback")
    rb.add_argument("snapshot", type=Path)
    args = parser.parse_args(argv)
    if args.action == "rollback":
        print(rollback(args.snapshot))
    else:
        specs, frozen, policy = load()
        print(json.dumps({"values": read(), "params": {k: v.__dict__ for k, v in specs.items()},
                          "frozen": sorted(frozen), "policy": policy}, ensure_ascii=False,
                         indent=2, default=list))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

