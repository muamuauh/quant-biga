"""顾问清单幂等标记与按交易日计数的调仓节奏。"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from qbg.config import settings
from qbg.market import calendar


def marker_path() -> Path:
    return settings.portfolio_dir / "last_run.json"


def rebalance_path() -> Path:
    return settings.portfolio_dir / "last_rebalance.json"


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def already_completed_today(today: str | None = None, path: Path | None = None) -> bool:
    marker = _load(path or marker_path())
    return bool(marker and marker.get("date") == (today or date.today().isoformat())
                and marker.get("artifacts"))


def save_marker(artifacts: list[str], today: str | None = None,
                path: Path | None = None) -> Path:
    target = path or marker_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"date": today or date.today().isoformat(),
                                  "artifacts": artifacts}, ensure_ascii=False), encoding="utf-8")
    return target


def load_rebalance_date(path: Path | None = None) -> str | None:
    value = _load(path or rebalance_path())
    return value.get("date") if value else None


def save_rebalance_date(today: str | None = None, path: Path | None = None) -> Path:
    target = path or rebalance_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"date": today or date.today().isoformat()}), encoding="utf-8")
    return target


def is_rebalance_day(every_days: int, today: str, *, path: Path | None = None,
                     cash_fraction: float | None = None, cash_trigger: float = 0.0,
                     calendar_root: Path | None = None) -> bool:
    if cash_trigger > 0 and cash_fraction is not None and cash_fraction >= cash_trigger:
        return True
    if every_days <= 1:
        return True
    last = load_rebalance_date(path)
    return not last or calendar.trading_days_between(last, today, calendar_root) >= every_days

