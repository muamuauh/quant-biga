from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qbg.market.calendar import is_trading_day, next_trading_day, previous_trading_day  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().isoformat())
    args = parser.parse_args(argv)
    opened = is_trading_day(args.date)
    print({"date": args.date, "trading_day": opened,
           "previous": previous_trading_day(args.date), "next": next_trading_day(args.date)})
    return 0 if opened else 1


if __name__ == "__main__":
    raise SystemExit(main())

