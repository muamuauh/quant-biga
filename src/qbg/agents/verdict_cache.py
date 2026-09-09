"""把逐票复核的结论落盘，让「复核」和「下单」可以分开跑。

## 为什么要拆

2026-09-09 实测：TradingAgents 复核 5 只票花了 **58 分钟**（每只 12 次 LLM
调用，中转站单次 30 秒~2 分钟）。而 `require_trading_session` 是硬闸 ——
09:30 开跑，复核跑到 10:30，勉强赶上上午盘尾；慢一点就过 11:30，
**整天作废，而且 LLM 的钱已经花掉了**。

拆开之后：

    08:30  premarket   拉数 → 重训 → 打分 → 复核 → 写这份缓存   （慢，约 1 小时）
    09:30  daily       读缓存 → 风控 → 下单                      （快，约 2 分钟）

它把「LLM 慢」和「必须在盘中下单」这两个约束**解耦**了 —— 这才是问题的根，
减少候选数只是把症状往后推一点。

## 缓存的匹配判据：候选名单必须**逐只相同**

不是"日期对上就用"。盘前和盘中之间，如果分数变了（重训、数据修正、
可负担性过滤因资金变化而改变），候选名单就会不同 —— 这时拿旧结论去套新名单，
等于**给没复核过的票安一个别人的评级**。

所以判据是集合相等。不等就整份作废、退回盘中现场复核（慢，但正确）。
宁可慢，不可张冠李戴。

## 它不是「跳过复核」的开关

缓存缺失、过期、名单不符，一律退回现场复核。这个模块只能让复核**提前发生**，
不能让它**不发生** —— `QBG_AGENTS_ENABLED` 才是那个开关。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from qbg.config import settings
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

_SUBDIR = "reviews"


def cache_path(day: str, root: Path | None = None) -> Path:
    return (root or settings.data_dir) / _SUBDIR / f"{day}.json"


def save_verdicts(day: str, candidates: list[str], verdicts: list[dict],
                  kept: dict[str, float], usage: dict,
                  root: Path | None = None) -> Path:
    """写一份当日复核结论。`candidates` 的**顺序无关**，比对时按集合。"""
    path = cache_path(day, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": day,
        "created_ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "candidates": sorted(candidates),
        "kept": {k: float(v) for k, v in kept.items()},
        "verdicts": verdicts,
        "usage": usage,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event(log, "review.cache.saved", day=day, candidates=len(candidates),
              kept=len(kept), path=str(path))
    return path


def load_verdicts(day: str, candidates: list[str],
                  root: Path | None = None) -> tuple[dict[str, float], list[dict], dict] | None:
    """读当日复核结论；**候选名单不完全一致就返回 `None`**。

    返回 `(kept, verdicts, usage)`，和 `review_candidates` 的返回形状一致，
    这样调用方两条路径可以共用后续代码。
    """
    path = cache_path(day, root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 —— 坏文件不该让日流程崩
        log_event(log, "review.cache.unreadable", path=str(path),
                  error=f"{type(exc).__name__}: {exc}"[:160])
        return None

    if payload.get("date") != day:
        # 文件名和内容对不上：多半是手工改过或半截写入。不猜，直接作废。
        log_event(log, "review.cache.date_mismatch", path=str(path),
                  inside=payload.get("date"), expected=day)
        return None

    cached = set(payload.get("candidates") or [])
    wanted = set(candidates)
    if cached != wanted:
        log_event(log, "review.cache.candidates_changed",
                  day=day, only_in_cache=sorted(cached - wanted),
                  only_in_today=sorted(wanted - cached))
        return None

    kept = {k: float(v) for k, v in (payload.get("kept") or {}).items()}
    log_event(log, "review.cache.hit", day=day, candidates=len(cached),
              kept=len(kept), created_ts=payload.get("created_ts"))
    return kept, list(payload.get("verdicts") or []), dict(payload.get("usage") or {})
