"""数据源降级链。

按 `QBG_DATA_SOURCES` 的顺序试，第一个拿到数据的赢。

## "拿不到"分两种，处理方式完全不同

  · **源不可用**（`SourceUnavailable`：没装、登录失败、连不上）
    → 换下一个源，并把这个源**在本次进程里标记为死的**，后面 299 只票
      不必再每只都等它超时一次。

  · **这只票没数据**（返回空 DataFrame）
    → **不换源**。退市股、代码写错、日期区间里根本没交易日，在任何源上
      都查不到，换源只是白等三次。

把这两种混为一谈是降级链最常见的写法错误：要么一只退市股拖着整批去轮询
所有源，要么主源挂了却因为"返回空"被当成正常结果，整批数据静默变空。

## 降级不是免费的

不同源的字段完备度不一样（见各源模块的说明）：AKShare 和 Mootdx 都
**给不出停牌/ST 标志**，Mootdx 连复权因子都没有。所以降级发生时这里会
记一条 `chain.degraded` 日志，标明哪只票、退到了哪个源、缺哪些字段。
`cache.py` 把这个信息落进 parquet 的 `source` 列，后续可以按源重拉。
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from qbg.config import settings
from qbg.data.sources.base import DailyBarSource, SourceUnavailable, empty_bars
from qbg.utils.logging import get_logger, log_event
from qbg.utils.net import net_timeout

log = get_logger(__name__)

# 各源缺失的字段，用于降级时的告警。见各源模块的说明。
_MISSING_FIELDS: dict[str, tuple[str, ...]] = {
    "baostock": (),
    "akshare": ("is_st", "is_suspended"),
    "mootdx": ("is_st", "is_suspended", "factor"),
}


_KNOWN_SOURCES = frozenset(_MISSING_FIELDS)


def _build(name: str) -> DailyBarSource:
    if name == "baostock":
        from qbg.data.sources.baostock_source import BaostockSource

        return BaostockSource()
    if name == "akshare":
        from qbg.data.sources.akshare_source import AkshareSource

        return AkshareSource()
    if name == "mootdx":
        from qbg.data.sources.mootdx_source import MootdxSource

        return MootdxSource()
    raise ValueError(f"未知数据源: {name!r}（可选: baostock / akshare / mootdx）")


@dataclass
class FetchResult:
    """带来源标注的取数结果。

    `source` 会被写进 parquet：知道某段数据是从哪个源来的，才能在发现
    停牌标志缺失时定位到"那几天降级到了 akshare"。
    """

    bars: pd.DataFrame
    source: str          # 实际出数的源名；全失败时是 ""
    degraded: bool       # 是否不是首选源出的数

    @property
    def empty(self) -> bool:
        return len(self.bars) == 0


class SourceChain:
    """按顺序尝试多个源，失败降级。

    实例内会缓存"哪些源已经死了"，所以整批 ingest 里一个挂掉的源
    只会拖慢第一只票。
    """

    def __init__(self, names: list[str] | None = None):
        # 用 `is None` 而不是 `or`：显式传进来的空列表是调用方的 bug，
        # 静默退回全局默认会让"我明明只配了一个源"变成"它偷偷试了三个"。
        self.names = list(settings.data_sources) if names is None else list(names)
        if not self.names:
            raise ValueError("数据源列表为空，至少要配一个（QBG_DATA_SOURCES）")
        # 源名拼错是**配置错误**，不是运行时降级——在构造时就炸掉，
        # 而不是等到第一次 fetch 才发现，更不能降级成"全失败"静默返回空数据。
        unknown = [n for n in self.names if n not in _KNOWN_SOURCES]
        if unknown:
            raise ValueError(
                f"未知数据源 {unknown}，可选: {sorted(_KNOWN_SOURCES)}"
            )
        self._sources: dict[str, DailyBarSource] = {}
        self._dead: set[str] = set()

    def _get(self, name: str) -> DailyBarSource | None:
        """懒加载。构造失败（没装、连不上）等同于源不可用，走降级。"""
        if name in self._dead:
            return None
        if name not in self._sources:
            try:
                self._sources[name] = _build(name)
            except (SourceUnavailable, ImportError) as e:
                log_event(log, "chain.source_dead", source=name, reason=str(e)[:200])
                self._dead.add(name)
                return None
        return self._sources[name]

    def fetch(self, code: str, start: str, end: str) -> FetchResult:
        for i, name in enumerate(self.names):
            src = self._get(name)
            if src is None:
                continue
            try:
                # 超时套在**链条层**而不是每个源里：源的实现各不相同
                # （baostock 走自己的协议、akshare 走 requests），但都从这里进。
                # 没有它的话「换下一个源」这条降级路径只挡得住"立刻报错"，
                # 挡不住"永远不回" —— 见 utils/net.py 的模块说明。
                with net_timeout():
                    bars = src.fetch(code, start, end)
            except SourceUnavailable as e:
                # 源整体不可用 → 拉黑，换下一个。
                log_event(log, "chain.source_dead", source=name, reason=str(e)[:200])
                self._dead.add(name)
                continue
            except Exception as e:  # noqa: BLE001 — 单源的意外错误不该中断整批
                log_event(log, "chain.fetch_error", source=name, code=code,
                          error=str(e)[:200])
                continue

            if len(bars) == 0:
                # 这只票在这个源没数据。**不换源**——见模块说明。
                log_event(log, "chain.empty", source=name, code=code,
                          start=start, end=end)
                return FetchResult(empty_bars(), name, degraded=i > 0)

            if i > 0:
                log_event(log, "chain.degraded", code=code, source=name,
                          preferred=self.names[0],
                          missing_fields=list(_MISSING_FIELDS.get(name, ())))
            return FetchResult(bars, name, degraded=i > 0)

        log_event(log, "chain.all_failed", code=code, tried=self.names)
        return FetchResult(empty_bars(), "", degraded=True)

    def trade_dates(self, start: str, end: str) -> list[str]:
        """交易日历：同样按顺序试，第一个给出非空结果的赢。

        Mootdx 不支持日历（返回空列表），所以链条会自然跳过它。
        """
        for name in self.names:
            src = self._get(name)
            if src is None:
                continue
            try:
                dates = src.trade_dates(start, end)
            except SourceUnavailable as e:
                log_event(log, "chain.source_dead", source=name, reason=str(e)[:200])
                self._dead.add(name)
                continue
            except Exception as e:  # noqa: BLE001
                log_event(log, "chain.trade_dates_error", source=name, error=str(e)[:200])
                continue
            if dates:
                return dates
        log_event(log, "chain.trade_dates_all_failed", tried=self.names)
        return []
