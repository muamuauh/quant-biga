"""全市场代码-名称表，以及**股票名称的归一化**。

这张表有两个用途：
  1. 下单清单要显示中文名（你在 APP 里认的是名字不是代码）
  2. **P5 的 OCR 读图靠它把名称反查成代码** —— 手机 APP 持仓页经常只显示
     股票名不显示代码

## 名称归一化：一个真实的坑

同一只票在不同接口里的名字**写法不一样**。实测（2026-08-10）：

    ak.stock_info_a_code_name()        →  '万  科Ａ'   ← 两个空格 + 全角Ａ
    ak.index_stock_cons_csindex()      →  '万科A'      ← 无空格 + 半角A

直接用字符串相等去匹配，这只票永远查不到。所以所有比对都走 `norm_name()`：
去空白（含全角空格）、全角转半角、统一大写。

**歧义时抛异常，绝不猜。** 猜错名字会让系统把持仓算到另一只票上，
而这个错误一路传到下单清单都不会有任何提示。
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path

import pandas as pd

from qbg.config import settings
from qbg.market import codes
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

_CACHE_NAME = "code_name.json"


class AmbiguousNameError(ValueError):
    """一个名字匹配到多只票。宁可报错也不猜——猜错会把持仓记到别的票上。"""


class UnknownNameError(ValueError):
    """名字在全市场表里找不到。"""


def norm_name(name: str) -> str:
    """股票名归一化，用于跨接口比对。

    `'万  科Ａ'` 和 `'万科A'` 都 → `'万科A'`。

    NFKC 把全角字符折叠成半角（Ａ→A、２→2），再去掉所有空白字符
    （含全角空格 U+3000）。最后统一大写，因为 ST 有时写成 st。
    """
    if name is None:
        return ""
    s = unicodedata.normalize("NFKC", str(name))
    s = "".join(ch for ch in s if not ch.isspace())
    return s.upper()


def _cache_path(root: Path | None = None) -> Path:
    return (root or settings.snapshot_dir) / _CACHE_NAME


def load_cached(root: Path | None = None) -> dict[str, str]:
    """`{'600519.SH': '贵州茅台', ...}`，没缓存返回空 dict。"""
    p = _cache_path(root)
    if not p.exists():
        return {}
    try:
        return dict(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        log_event(log, "meta.cache_read_error", path=str(p), error=str(e)[:200])
        return {}


def save_cached(mapping: dict[str, str], root: Path | None = None) -> Path:
    root = root or settings.snapshot_dir
    root.mkdir(parents=True, exist_ok=True)
    p = _cache_path(root)
    p.write_text(json.dumps(mapping, ensure_ascii=False, indent=0, sort_keys=True),
                 encoding="utf-8")
    return p


def refresh(root: Path | None = None) -> dict[str, str]:
    """从 AKShare 拉全市场代码名称表。

    `stock_info_a_code_name` 走的是交易所官网（不是东财 push2），所以在
    东财被限流时仍然可用——这点很重要，因为 P5 的 OCR 反查完全依赖它。

    **fail-soft 到缓存**：拉不到就用上次的。名称变更（改名、戴帽摘帽）
    是低频事件，用一份几天前的表远好过完全没有表。
    """
    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
    except Exception as e:  # noqa: BLE001
        cached = load_cached(root)
        log_event(log, "meta.refresh.failed_using_cache",
                  error=str(e)[:200], cached_count=len(cached))
        return cached

    if df is None or df.empty:
        return load_cached(root)

    mapping: dict[str, str] = {}
    skipped = 0
    for raw_code, name in zip(df["code"].astype(str), df["name"].astype(str), strict=False):
        try:
            mapping[codes.normalize(raw_code)] = name.strip()
        except codes.UnknownCodeError:
            # B股/基金/债券等不在支持范围内，静默跳过但计数。
            skipped += 1
    save_cached(mapping, root)
    log_event(log, "meta.refresh.ok", count=len(mapping), skipped=skipped)
    return mapping


def name_of(code: str, root: Path | None = None) -> str:
    """代码 → 名称。查不到返回空字符串（清单里显示代码即可，不该因此崩掉）。"""
    return load_cached(root).get(codes.normalize(code), "")


def code_of(name: str, root: Path | None = None,
            mapping: dict[str, str] | None = None) -> str:
    """名称 → 代码。**这是 OCR 读图的关键路径。**

    走归一化比对，所以 `'万  科Ａ'`、`'万科A'`、`'万科Ａ'` 都能命中同一只票。
    找不到抛 `UnknownNameError`，多于一只抛 `AmbiguousNameError`——
    两种都不返回"最像的那个"，因为猜错的代价是把持仓算到别的票上。
    """
    mapping = load_cached(root) if mapping is None else mapping
    target = norm_name(name)
    if not target:
        raise UnknownNameError("空的股票名称")

    hits = [c for c, n in mapping.items() if norm_name(n) == target]
    if not hits:
        raise UnknownNameError(f"全市场代码表里找不到股票名: {name!r}")
    if len(hits) > 1:
        raise AmbiguousNameError(f"股票名 {name!r} 匹配到多只票: {sorted(hits)}")
    return hits[0]


def is_st_name(name: str) -> bool:
    """名字里带 ST / *ST / 退，说明是风险警示股。

    这是**名称层面**的判断，用于 universe 过滤和 OCR 结果的合理性提示。
    交易时点的权威判断用 BaoStock 的 `isST` 字段（那是当日状态，无前视偏差）。
    """
    n = norm_name(name)
    return "ST" in n or "退" in n


def to_frame(root: Path | None = None) -> pd.DataFrame:
    """代码名称表的 DataFrame 视图，带归一化列，方便批量 join。"""
    mapping = load_cached(root)
    df = pd.DataFrame({"code": list(mapping), "name": list(mapping.values())})
    if df.empty:
        return pd.DataFrame(columns=["code", "name", "name_norm", "is_st"])
    df["name_norm"] = df["name"].map(norm_name)
    df["is_st"] = df["name"].map(is_st_name)
    return df.sort_values("code").reset_index(drop=True)
