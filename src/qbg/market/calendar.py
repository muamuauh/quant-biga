"""A股交易日历与交易时段。

替代 quant-trading 的 `risk/market_hours.py`（那个用 pandas_market_calendars
查 NYSE）。A股没有夏令时，但有午休，还有调休——国庆前后的周末可能是交易日，
所以**日历必须来自数据源，不能靠"周一到周五减法定节假日"算**。

日历本地缓存成 JSON。它一年才变一次（年底公布次年安排），但每次 ingest
都去查一遍既慢又给源添堵；`fail-soft` 到上次缓存也让临时断网不至于让
整条流水线判断不出"今天是不是交易日"。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from qbg.config import settings
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

TZ = ZoneInfo("Asia/Shanghai")

# 连续竞价时段。集合竞价（09:15–09:25、14:57–15:00）不单独建模：
# 本项目是盘后出清单、次日人工执行，不需要区分到那个粒度。
MORNING = (dt.time(9, 30), dt.time(11, 30))
AFTERNOON = (dt.time(13, 0), dt.time(15, 0))

_CACHE_NAME = "trade_calendar.json"


def _cache_path(root: Path | None = None) -> Path:
    return (root or settings.snapshot_dir) / _CACHE_NAME


def load_cached(root: Path | None = None) -> list[str]:
    p = _cache_path(root)
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        log_event(log, "calendar.cache_read_error", path=str(p), error=str(e)[:200])
        return []


def save_cached(dates: list[str], root: Path | None = None) -> Path:
    root = root or settings.snapshot_dir
    root.mkdir(parents=True, exist_ok=True)
    p = _cache_path(root)
    p.write_text(json.dumps(sorted(set(dates)), ensure_ascii=False), encoding="utf-8")
    return p


def refresh(chain, start: str | None = None, end: str | None = None,
            root: Path | None = None) -> list[str]:
    """从数据源刷新日历并落缓存。

    **fail-soft**：源拿不到就退回上次缓存，而不是返回空——一个空日历会让
    `is_trading_day` 对所有日期都说 False，整条流水线看起来像"永远休市"。
    """
    start = start or settings.qbg_history_start
    # 往后多要一年：次年的安排一公布就能拿到，省得跨年时判断不出来。
    end = end or (dt.date.today() + dt.timedelta(days=365)).isoformat()

    dates = chain.trade_dates(start, end)
    if not dates:
        cached = load_cached(root)
        log_event(log, "calendar.refresh.failed_using_cache", cached_days=len(cached))
        return cached
    save_cached(dates, root)
    log_event(log, "calendar.refresh.ok", days=len(dates),
              first=dates[0], last=dates[-1])
    return dates


def trading_days(start: str, end: str, root: Path | None = None) -> list[str]:
    """缓存里 [start, end] 区间的交易日。"""
    lo, hi = str(start), str(end)
    return [d for d in load_cached(root) if lo <= d <= hi]


def is_trading_day(day: dt.date | str | None = None, root: Path | None = None) -> bool:
    """`day` 是不是交易日。缓存为空时返回 False 并记日志。

    返回 False 而不是抛异常，是因为调用点是 `00_market_check.py` —— 它的
    职责就是"今天要不要跑"，日历缺失时不跑是安全的一侧。但必须留下日志，
    否则会表现为"系统连续几周什么都不做"却没人知道为什么。
    """
    d = _as_date_str(day)
    cal = load_cached(root)
    if not cal:
        log_event(log, "calendar.empty", asked=d)
        return False
    return d in set(cal)


def previous_trading_day(day: dt.date | str | None = None,
                         root: Path | None = None) -> str | None:
    """`day` 之前最近的一个交易日（不含 day 本身）。"""
    d = _as_date_str(day)
    earlier = [x for x in load_cached(root) if x < d]
    return earlier[-1] if earlier else None


def next_trading_day(day: dt.date | str | None = None,
                     root: Path | None = None) -> str | None:
    """`day` 之后最近的一个交易日（不含 day 本身）。

    半自动模式下这就是"清单要在哪天执行"。
    """
    d = _as_date_str(day)
    later = [x for x in load_cached(root) if x > d]
    return later[0] if later else None


def trading_days_between(start: str, end: str, root: Path | None = None) -> int:
    """(start, end] 之间的交易日数。用于判断调仓周期。"""
    return len([d for d in load_cached(root) if start < d <= end])


def in_session(now: dt.datetime | None = None) -> bool:
    """当前是否在连续竞价时段内（含午休判断）。

    只看时段，**不看是不是交易日**——两件事分开判断，调用方通常两个都要
    （`is_trading_day() and in_session()`），合在一起反而容易在测试里
    互相干扰。
    """
    now = now or dt.datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    t = now.astimezone(TZ).time()
    return (MORNING[0] <= t <= MORNING[1]) or (AFTERNOON[0] <= t <= AFTERNOON[1])


def minutes_until_session(now: dt.datetime | None = None) -> float | None:
    """距离下一个连续竞价时段开始还有几分钟。

    已经在时段内返回 ``0.0``；今天两段都收了返回 ``None``。
    和 :func:`in_session` 一样**不判交易日**，两件事分开。

    为什么需要它：调用方想在"明知会被时段闸拦下"时提前退出，省掉拉数和
    LLM 复核的开销。但只看 `in_session()` 是不够的 —— 09:30:00 触发的任务
    可能因为几秒的时钟偏差落在 09:29:5x，那时 `in_session()` 还是 False，
    早退就会把**一整个交易日**跳过去。所以要问的是"多久以后开盘"，
    而不是"现在开没开"。
    """
    now = now or dt.datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    now = now.astimezone(TZ)
    if in_session(now):
        return 0.0
    for start, _end in (MORNING, AFTERNOON):
        opening = now.replace(hour=start.hour, minute=start.minute,
                              second=0, microsecond=0)
        if now < opening:
            return (opening - now).total_seconds() / 60.0
    return None


def _as_date_str(day: dt.date | str | None) -> str:
    if day is None:
        return dt.datetime.now(TZ).date().isoformat()
    if isinstance(day, str):
        return str(pd.Timestamp(day).date())
    if isinstance(day, dt.datetime):
        return day.date().isoformat()
    return day.isoformat()
