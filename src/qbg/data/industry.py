"""申万一级行业分类。

用于行业中性化：选股前在行业内 demean，避免一次押注单一行业。

## 为什么用申万而不是 LLM 打标

`quant-agent` 在美股上是用 LLM 给每只票分 GICS 行业的——因为免费的美股
行业数据不好拿。A股不需要这么做：**申万一级行业是现成的公开数据**，
31 个行业，权威、免费、可复现。省掉 LLM 调用，结果也不会每次跑都不一样。

## 为什么不用东财行业

实测（2026-08-10，本机）东财的 `stock_board_industry_name_em` 走
`push2.eastmoney.com`，该主机不可达（连接被重置）。而申万的
`sw_index_first_info` / `index_component_sw` 走的是另一条路径，可用。

## 成本

一级行业 31 个，每个拉一次成分股 = 31 次请求。行业归属是很低频的数据
（调整一年几次），所以缓存下来按天复用，不必每次 ingest 都拉。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pandas as pd

from qbg.config import settings
from qbg.market import codes
from qbg.utils.logging import get_logger, log_event
from qbg.utils.net import net_timeout

log = get_logger(__name__)

_CACHE_NAME = "industry_sw.json"

# 申万接口之间的请求间隔。31 次请求打太快容易被限流，而这份数据
# 一年才变几次，慢一点完全无所谓。
_SLEEP_SEC = 0.3


def _cache_path(root: Path | None = None) -> Path:
    return (root or settings.snapshot_dir) / _CACHE_NAME


def load_cached(root: Path | None = None) -> dict[str, str]:
    """`{'600519.SH': '食品饮料', ...}`。没缓存返回空 dict。"""
    p = _cache_path(root)
    if not p.exists():
        return {}
    try:
        return dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        log_event(log, "industry.cache_read_error", path=str(p), error=str(e)[:200])
        return {}


def save_cached(mapping: dict[str, str], root: Path | None = None) -> Path:
    root = root or settings.snapshot_dir
    root.mkdir(parents=True, exist_ok=True)
    p = _cache_path(root)
    p.write_text(json.dumps(mapping, ensure_ascii=False, indent=0, sort_keys=True),
                 encoding="utf-8")
    return p


def refresh(root: Path | None = None, sleep_sec: float = _SLEEP_SEC) -> dict[str, str]:
    """拉全部 31 个申万一级行业及其成分股。

    **fail-soft**：整体拉不到就退回缓存；单个行业拉失败就跳过那一个
    （其余 30 个的归属仍然有用，行业中性化对缺失值的处理是"自成一组"）。
    """
    try:
        import akshare as ak

        with net_timeout():
            industries = ak.sw_index_first_info()
    except Exception as e:  # noqa: BLE001
        cached = load_cached(root)
        log_event(log, "industry.refresh.failed_using_cache",
                  error=str(e)[:200], cached_count=len(cached))
        return cached

    if industries is None or industries.empty:
        return load_cached(root)

    mapping: dict[str, str] = {}
    failed: list[str] = []
    for ind_code, ind_name in zip(
        industries["行业代码"].astype(str), industries["行业名称"].astype(str), strict=False
    ):
        # `801010.SI` → `801010`
        symbol = ind_code.split(".")[0]
        try:
            with net_timeout():
                cons = ak.index_component_sw(symbol=symbol)
        except Exception as e:  # noqa: BLE001
            failed.append(ind_name)
            log_event(log, "industry.component.error",
                      industry=ind_name, symbol=symbol, error=str(e)[:160])
            continue
        if cons is None or cons.empty or "证券代码" not in cons.columns:
            failed.append(ind_name)
            continue
        for raw in cons["证券代码"].astype(str):
            try:
                mapping[codes.normalize(raw)] = ind_name
            except codes.UnknownCodeError:
                continue
        time.sleep(sleep_sec)

    if not mapping:
        return load_cached(root)

    save_cached(mapping, root)
    log_event(log, "industry.refresh.ok", stocks=len(mapping),
              industries=len(set(mapping.values())), failed=failed)
    return mapping


def industry_of(code: str, root: Path | None = None,
                mapping: dict[str, str] | None = None) -> str:
    """代码 → 申万一级行业名。查不到返回 `"未分类"`。

    返回一个具体的组名而不是空串/NaN，是为了让中性化时这些票**自成一组**
    互相 demean，而不是被丢掉或混进某个真实行业里。
    """
    mapping = load_cached(root) if mapping is None else mapping
    return mapping.get(codes.normalize(code), "未分类")


def neutralize(scores: pd.Series, mapping: dict[str, str] | None = None,
               root: Path | None = None) -> pd.Series:
    """行业内 demean。`scores` 以规范代码为索引。

    减去组均值而不是做完整的截面回归：一级行业只有 31 组，demean 已经
    足够去掉行业整体的 beta，而且它是无参数的——没有可以过拟合的东西。

    **单只票自成一组时 demean 会把它变成 0**，等于把它排到中间。这是
    有意的保守处理：一个没有同行可比的票，我们对它的相对强弱没有信息。
    """
    if scores.empty:
        return scores
    mapping = load_cached(root) if mapping is None else mapping
    groups = pd.Series(
        [mapping.get(str(c), "未分类") for c in scores.index], index=scores.index
    )
    return scores - scores.groupby(groups).transform("mean")


def coverage(members: list[str], root: Path | None = None) -> dict:
    """行业表对给定股票池的覆盖情况。进日报，用来发现"行业数据过期了"。"""
    mapping = load_cached(root)
    hit = [c for c in members if codes.normalize(c) in mapping]
    return {
        "total": len(members),
        "classified": len(hit),
        "unclassified": len(members) - len(hit),
        "industries": len({mapping[codes.normalize(c)] for c in hit}),
    }
