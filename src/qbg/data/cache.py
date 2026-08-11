"""每股一个 parquet 的增量缓存。

**这是整个数据层能在限频环境下活下来的关键。** 首次落地 300 只 × 5 年要
拉很久，之后每天只拉新增的那几行——秒级完成，而且几乎不会触发任何源的
频率限制。

## 增量的边界怎么定

从 `last_date + 1 天` 开始拉，不是从 `last_date` 开始。看起来只差一天，
但从 `last_date` 重拉会让最后一行被"更新"——而最后一行恰恰是最可能被
源事后修正的那行（收盘数据当晚出、盘后可能重算）。所以：

  * 常规增量：`start = last_date + 1`
  * `refresh_tail_days > 0` 时：`start = last_date - refresh_tail_days + 1`，
    显式地重拉尾部若干天并覆盖。默认 2，因为 A股的成交额/复权因子偶尔
    会在 T+1 被修正，只拉"严格更新"的话这类修正永远进不来。

## 为什么记 `source` 列

降级链退到 AKShare 时拿不到停牌/ST 标志，退到 Mootdx 时连复权因子都没有。
把每一行是从哪个源来的记下来，才能在事后问"哪些行的停牌标志是假的"，
然后定向重拉。不记的话这些洞是不可见的。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from qbg.config import settings
from qbg.data.sources.base import BAR_COLUMNS, normalize_bars
from qbg.data.sources.chain import SourceChain
from qbg.market import codes
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

# parquet 里比 BAR_COLUMNS 多一列：这一行是哪个源给的。
STORED_COLUMNS: tuple[str, ...] = (*BAR_COLUMNS, "source")

# 默认重拉尾部天数。见模块说明——不为 0 是因为 A股数据当晚出、T+1 可能修正。
DEFAULT_REFRESH_TAIL_DAYS = 2


def path_for(code: str, root: Path | None = None) -> Path:
    """`600519.SH` → `data/parquet/600519.SH.parquet`。

    用规范代码当文件名（而不是裸 6 位）：SH 和 SZ 理论上不会同号，但
    文件名里带交易所让人 `ls` 一眼能看出是哪个市场，也避免将来加港股时
    需要迁移。
    """
    root = root or settings.parquet_dir
    return root / f"{codes.normalize(code)}.parquet"


def read(code: str, root: Path | None = None, repair: bool = True) -> pd.DataFrame:
    """读缓存。没有文件就返回空 DataFrame（不是异常——首次运行本来就没有）。

    `repair=True`（默认）会修掉数据源里的伪造因子下降（见 `repair_factor`）。
    默认开是因为**没有任何下游想要一个错的因子**；`repair=False` 保留源的
    原样，供对账和 `verify()` 用。

    落盘的永远是源的原样，修复只发生在读出来之后——这样源的问题始终可见、
    可审计，不会被我们的修复悄悄掩盖掉。
    """
    p = path_for(code, root)
    if not p.exists():
        return _empty_stored()
    try:
        df = pd.read_parquet(p)
    except Exception as e:  # noqa: BLE001 — 坏文件不该让整批 ingest 崩
        log_event(log, "cache.read_error", code=code, path=str(p), error=str(e)[:200])
        return _empty_stored()
    out = _conform(df)
    if repair and not out.empty:
        fixed, repairs = repair_factor(out["factor"])
        if repairs:
            out = out.copy()
            out["factor"] = fixed
            log_event(log, "cache.factor_repaired", code=code,
                      n_repairs=len(repairs), repairs=repairs[:5])
    return out


def last_date(code: str, root: Path | None = None) -> pd.Timestamp | None:
    """缓存里最后一个交易日，没有缓存返回 None。"""
    df = read(code, root)
    return None if df.empty else pd.Timestamp(df["date"].iloc[-1])


def write(code: str, df: pd.DataFrame, root: Path | None = None) -> Path:
    """整表覆盖写。调用方应先 merge，不要用它做追加。"""
    root = root or settings.parquet_dir
    root.mkdir(parents=True, exist_ok=True)
    p = path_for(code, root)
    _conform(df).to_parquet(p, index=False)
    return p


def merge(old: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    """合并新旧数据，**同一天以新数据为准**。

    新数据优先是有意的：重拉尾部就是为了让事后修正能覆盖旧值。
    如果反过来以旧为准，`refresh_tail_days` 就完全没有意义了。
    """
    if len(new) == 0:
        return _conform(old)
    if len(old) == 0:
        return _conform(new)
    both = pd.concat([_conform(old), _conform(new)], ignore_index=True)
    both = both.drop_duplicates(subset=["date"], keep="last")
    return both.sort_values("date").reset_index(drop=True)


def update(
    code: str,
    chain: SourceChain,
    *,
    start: str | None = None,
    end: str | None = None,
    refresh_tail_days: int = DEFAULT_REFRESH_TAIL_DAYS,
    force_full: bool = False,
    root: Path | None = None,
) -> dict:
    """增量更新一只票，返回一份可直接进日志的摘要。

    `force_full=True` 时忽略缓存从 `start` 全量重拉——源改口径、发现历史
    数据有问题时用。
    """
    start = start or settings.qbg_history_start
    end = end or pd.Timestamp.today().strftime("%Y-%m-%d")

    cached = _empty_stored() if force_full else read(code, root)
    fetch_start = start
    if not cached.empty:
        last = pd.Timestamp(cached["date"].iloc[-1])
        # 见模块说明：从 last - refresh_tail_days + 1 开始，让尾部修正能进来。
        fetch_start = (last - pd.Timedelta(days=max(refresh_tail_days, 1) - 1)).strftime("%Y-%m-%d")
        if fetch_start > end:
            # 缓存已经比请求的区间还新，什么都不用做。
            return _summary(code, cached, added=0, source="cache", skipped=True)

    result = chain.fetch(code, fetch_start, end)
    if result.empty:
        return _summary(code, cached, added=0, source=result.source or "none",
                        skipped=False, empty_fetch=True)

    fresh = result.bars.copy()
    fresh["source"] = result.source
    merged = merge(cached, fresh)
    write(code, merged, root)

    added = len(merged) - len(cached)
    summary = _summary(code, merged, added=added, source=result.source,
                       skipped=False, degraded=result.degraded)
    log_event(log, "cache.update", **summary)
    return summary


def repair_factor(factor: pd.Series) -> tuple[pd.Series, list[dict]]:
    """修掉后复权因子里的伪造下降。返回 `(修好的因子, 修复记录)`。

    ## 为什么需要它

    后复权因子只会在除权除息日**向上**跳（分红送股不断累积），它的存在
    本身就是为了抵消原始价的除权缺口。所以**因子在原始价正常波动的日子
    发生变化，这个变化必定是假的**。

    实测（2026-08-10，沪深300 全量 304 只）有 4 只中招，而且
    **BaoStock 自己的 `query_adjust_factor` 权威表里就带着这个下降**——
    换那个接口拿不到更好的数据，只能修。

        000001.SZ  平安银行  2020-12-31  119.96 → 99.79   伪造单日收益 +16.94%
        000002.SZ  万科A     2020-11-19  115.09 → 112.15  伪造 +2.57%（次日原样恢复）
        600372.SH            2020-11-19                    伪造 +0.41%
        601607.SH            2020-11-27                    伪造 +0.78%

    平安银行那一天原始价是 19.20 → 19.34（+0.73%），复权后却变成 −16.2%——
    一根凭空造出来的假阴线。不修的话它会直接进模型训练集。

    ## 两种故障，两种修法

    **一日凹陷**（次日恢复到原值）——例如万科：因子掉一天又原样弹回来，
    没有任何公司行为是这个形状。修法：把那一天的异常值换成前一天的值。

    **持久平移**（掉下去就不回来）——例如平安银行：多半是数据源在某个
    时点换了复权基准，把两段不同锚点的序列拼在了一起。修法：把**断点之后
    的整段**按比例放大回断点之前的水平。这样段内的相对变化完全不变
    （因子对某只票是常数倍，收益率序列不受影响），而断点当天的比值变成
    1.0，伪造收益随之消失。

    两种修法都只改因子、不碰价格，且都保证修完的序列单调不减。
    """
    f = pd.to_numeric(factor, errors="coerce").astype(float)
    if len(f) < 2:
        return f, []

    vals = f.to_numpy(copy=True)
    repairs: list[dict] = []

    for i in range(1, len(vals)):
        prev = vals[i - 1]
        if not (prev > 0) or not (vals[i] > 0):
            continue
        # 相对容差：因子量级从 1 到 130 都有，绝对容差没法统一。
        if vals[i] >= prev * (1 - 1e-9):
            continue

        recovers = i + 1 < len(vals) and vals[i + 1] >= prev * (1 - 1e-9)
        if recovers:
            # 一日凹陷：只有这一天是坏的。
            repairs.append({"kind": "outlier", "pos": i,
                            "from": round(vals[i], 6), "to": round(prev, 6)})
            vals[i] = prev
        else:
            # 持久平移：整条尾巴按比例抬回去。
            scale = prev / vals[i]
            repairs.append({"kind": "rescale", "pos": i,
                            "from": round(vals[i], 6), "to": round(prev, 6),
                            "scale": round(scale, 6)})
            vals[i:] *= scale

    return pd.Series(vals, index=f.index, name=f.name), repairs


def verify(df: pd.DataFrame) -> list[str]:
    """对一只票的缓存做自洽检查，返回问题列表（空 = 没问题）。

    这些检查针对的是**静默错误**——不会抛异常但会毁掉下游的那类：

      · 日期非严格递增 → 合并逻辑出了问题
      · 复权因子非正 / 递减 → 后复权因子应当单调不减（除权只会累积）
      · 价格非正 → 停牌日的 0 价没被清理掉
      · high < low → 源的数据本身有问题

    ⚠ 要检出因子递减，必须传**未修复**的数据：`cache.read(code, repair=False)`。
    `read()` 默认已经把伪造下降修掉了，直接把它的结果喂进来，这条检查
    永远不会触发——那就等于把源的数据质量问题藏起来了。
    """
    problems: list[str] = []
    if df.empty:
        return problems

    if not df["date"].is_monotonic_increasing:
        problems.append("date 非递增")
    if df["date"].duplicated().any():
        problems.append("date 有重复")

    f = df["factor"].dropna()
    if len(f) and (f <= 0).any():
        problems.append("factor 有非正值")
    if len(f) > 1 and (f.diff().dropna() < -1e-9).any():
        # 后复权因子只会因分红送股累积上升，下降说明源的口径中途变了。
        problems.append("factor 递减（后复权因子应单调不减）")

    tradable = df[~df["is_suspended"]]
    for col in ("open", "high", "low", "close"):
        s = tradable[col].dropna()
        if len(s) and (s <= 0).any():
            problems.append(f"{col} 在非停牌日有非正值")

    bad_hl = tradable.dropna(subset=["high", "low"])
    if len(bad_hl) and (bad_hl["high"] < bad_hl["low"]).any():
        problems.append("存在 high < low 的行")

    return problems


def hfq(df: pd.DataFrame) -> pd.DataFrame:
    """返回后复权价格视图：OHLC 乘以 factor，其余列不动。

    模型训练和回测用这个；下单清单用原始 df（不复权，和 APP 里看到的一致）。
    分成两个函数而不是存两套价格，是为了让"哪个是复权的"在调用点一目了然。
    """
    out = df.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = out[col] * out["factor"]
    return out


# ----------------------------------------------------------------------
# 内部
# ----------------------------------------------------------------------


def _empty_stored() -> pd.DataFrame:
    df = normalize_bars(None)
    df["source"] = pd.Series(dtype="object")
    return df[list(STORED_COLUMNS)]


def _conform(df: pd.DataFrame) -> pd.DataFrame:
    """规整到 `STORED_COLUMNS`。"""
    if df is None or len(df) == 0:
        return _empty_stored()
    src = df["source"] if "source" in df.columns else pd.Series("", index=df.index)
    out = normalize_bars(df)
    # normalize_bars 会重排序，source 要按 date 对齐回去而不是按位置贴。
    if "source" in df.columns:
        mapping = dict(zip(pd.to_datetime(df["date"], errors="coerce"), src, strict=False))
        out["source"] = out["date"].map(mapping).fillna("")
    else:
        out["source"] = ""
    return out[list(STORED_COLUMNS)]


def _summary(code: str, df: pd.DataFrame, *, added: int, source: str,
             skipped: bool, degraded: bool = False,
             empty_fetch: bool = False) -> dict:
    return {
        "code": code,
        "rows": len(df),
        "added": added,
        "source": source,
        "skipped": skipped,
        "degraded": degraded,
        "empty_fetch": empty_fetch,
        "last_date": None if df.empty else str(pd.Timestamp(df["date"].iloc[-1]).date()),
    }
