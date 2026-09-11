"""同花顺官方数据 API（fuyao.aicubes.cn）的只读客户端。

## 它解决的是一个具体问题：限价用的参考价是隔夜之前的

系统 09:30 下单，而 parquet 缓存里最新一根日线是**昨天**的。限价因此建立在
一个隔夜跳空之前的价格上。沪深300 成分股 46 万个交易日实测：

    跳空绝对值   P50 0.35%   P75 0.78%   P90 1.51%   P95 2.20%   P99 4.79%

0.2% 的让价下 **37.7% 的卖单**会挂到市价错误的一侧（完整表见
`configs/risk_limits.yaml`）。2026-09-11 实测踩中：688041 昨收 232.37、
卖单限价 231.91，而当天集合竞价定在 **228.02**、全天区间 225.37~231.50 ——
挂在市价上方，一整天没成交，回款没到账，连带一笔买单被券商静默拒绝。
若当时读到 228.02，按 0.2% 让价得 227.56，必成交。

## 它**没有**下单能力

官方 README 写明只做数据。下单仍然走 `qbg/execution/` 那套 easytrader +
pywinauto 的 UI 自动化。**不要往这个模块里加下单代码**，理由同
`ths_client.py`（CLAUDE.md §七）。

## 整条链路是可选增强，失败一律退回缓存收盘价

没配 key、网络不通、对端限流、返回里没有这只票 —— 全部退回老行为（用
`cache` 里的昨收）。这和 `fetch_index_constituents` 的 fail-soft 是同一条纪律：

**新增一个外部依赖，不能让它有权让整条下单链停摆。**

所以这里没有任何 `raise`：拿不到就少一个改进，而不是多一个故障点。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from qbg.config import settings
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

SNAPSHOT_PATH = "/api/a-share/prices/snapshot"
AUCTION_PATH = "/api/a-share/auction/snapshot"

# thscode 和本项目内部代码格式**完全一致**（600519.SH / 000001.SZ），
# 所以这里没有代码映射层。哪天对不上了，映射要加在这个模块里，
# 不要去动 qbg.market.codes —— 那是全项目的规范形式。


def enabled() -> bool:
    """配了 key 才算开。留空 = 这条增强整个不存在。"""
    return bool((settings.fuyao_api_key or "").strip())


def _get(path: str, params: dict) -> dict | None:
    """一次 GET。**任何失败都返回 None**，不抛异常。"""
    if not enabled():
        return None
    url = f"{settings.qbg_fuyao_base_url.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"X-api-key": settings.fuyao_api_key})
    try:
        # 用 settings 的统一超时。没有超时的降级链是死代码 —— 见 utils/net.py。
        with urllib.request.urlopen(request, timeout=settings.qbg_net_timeout_sec) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log_event(log, "fuyao.request.failed", path=path, error=f"{type(exc).__name__}: {exc}")
        return None
    # code=4001 或 HTTP 429 是限流。官方说法是"降低并发与频率、稍后重试"，
    # 而我们这里没有重试的余地（09:30 下单不等人），所以直接退回缓存价。
    if payload.get("code") != 0:
        log_event(log, "fuyao.request.rejected", path=path,
                  code=payload.get("code"), message=payload.get("message"))
        return None
    return payload.get("data") or {}


def _batched(codes: list[str]) -> list[list[str]]:
    size = max(1, int(settings.qbg_fuyao_batch))
    return [codes[i:i + size] for i in range(0, len(codes), size)]


def snapshot(codes: list[str]) -> dict[str, dict]:
    """盘中快照，按 thscode 索引。取不到的票直接不在返回里。"""
    out: dict[str, dict] = {}
    for batch in _batched(list(codes)):
        data = _get(SNAPSHOT_PATH, {"thscodes": ",".join(batch)})
        for row in (data or {}).get("item") or []:
            code = row.get("thscode")
            if code:
                out[code] = row
    return out


def auction_snapshot(codes: list[str], stage: str = "final") -> dict[str, dict]:
    """集合竞价快照。`stage`：`live`（9:15~9:25 撮合中）或 `final`（已定价）。"""
    out: dict[str, dict] = {}
    for batch in _batched(list(codes)):
        data = _get(AUCTION_PATH, {"thscodes": ",".join(batch), "stage": stage})
        for row in (data or {}).get("item") or []:
            code = row.get("thscode")
            if code:
                out[code] = row
    return out


def _positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def reference_prices(codes: list[str]) -> dict[str, float]:
    """下单该用的**当日**参考价，按 thscode 索引。拿不到的票不在返回里。

    取价顺序，以及为什么是这个顺序：

    1. **盘中快照的 `last_price`** —— 此刻真实成交价，最贴近下单那一瞬间。
    2. **集合竞价的 `auction_price`** —— 09:25~09:30 之间连续竞价还没开始，
       快照没有当日价，而竞价已经定出了开盘价。这一段时间它是唯一的来源。
    3. **快照的 `open_price`** —— 盘中快照偶尔缺 `last_price`（停牌、
       尚未成交）时的兜底，仍然是当日价。

    **昨收（`prev_price`）故意不在这条链里。** 那正是我们要绕开的东西 ——
    拿不到当日价就该返回"没有"，让调用方明确退回缓存收盘价并按更宽的让价
    下单，而不是在这里悄悄还回一个昨收、让上层以为拿到了新价。
    """
    codes = list(codes)
    if not codes or not enabled():
        return {}
    prices: dict[str, float] = {}

    live = snapshot(codes)
    for code, row in live.items():
        price = _positive(row.get("last_price")) or _positive(row.get("open_price"))
        if price is not None:
            prices[code] = price

    missing = [code for code in codes if code not in prices]
    if missing:
        for code, row in auction_snapshot(missing).items():
            price = _positive(row.get("auction_price")) or _positive(row.get("open_price"))
            if price is not None:
                prices[code] = price

    log_event(log, "fuyao.reference_prices", wanted=len(codes), got=len(prices))
    return prices
