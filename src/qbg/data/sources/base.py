"""日线数据源的统一契约。

每个源实现 `DailyBarSource`，上层只认这个协议，不认具体是 BaoStock 还是
AKShare。这样降级链（见 `chain.py`）可以在主源限频/挂掉时无缝换人。

## 列约定

`fetch()` 返回的 DataFrame 必须有 `BAR_COLUMNS` 这些列，且：

  * **open/high/low/close 一律是"不复权"价**——也就是你在券商 APP 里看到
    的价格。下单清单的限价、金额都用它。
  * **`factor` 是后复权因子**，`hfq_price = price * factor`。模型训练和回测
    用后复权（价格序列连续，除权日不会出现假跌）。

    为什么存"原始价 + 因子"而不是直接存两套价格：这是 qlib 的存储约定，
    `qlib_dump.py` 可以直接落盘不用换算；而且因子本身是可校验的
    （单调、最新一天应为 1.0），存两套价格反而容易出现两边不一致却发现不了。

  * `is_st` / `is_suspended` 是**当日**状态，不是当前状态。回测要按当天的
    状态判断能不能交易，用"今天查到的 ST 状态"去过滤三年前的数据就是
    前视偏差。

## 错误约定

**取不到数据返回空 DataFrame，不抛异常。** 单只票拉失败不该中断整批
300 只的 ingest。真正的异常（配置错、代码格式错）才抛。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

# fetch() 必须返回的列，顺序即约定顺序。
BAR_COLUMNS: tuple[str, ...] = (
    "date",          # datetime64[ns]，交易日
    "open",          # 不复权
    "high",
    "low",
    "close",
    "volume",        # 股
    "amount",        # 元
    "factor",        # 后复权因子：hfq = price * factor
    "is_st",         # bool，当日是否 ST/*ST
    "is_suspended",  # bool，当日是否停牌
)

_FLOAT_COLS = ("open", "high", "low", "close", "volume", "amount", "factor")
_BOOL_COLS = ("is_st", "is_suspended")


class SourceUnavailable(RuntimeError):
    """源整体不可用（未安装、登录失败、被封）。降级链据此换下一个源。

    和"这只票没数据"区分开：后者返回空 DataFrame，链条不会因此换源——
    退市股在任何源上都查不到，换源只是白等。
    """


@runtime_checkable
class DailyBarSource(Protocol):
    """日线源。实现类要能在没网的测试里被 mock 掉。"""

    name: str

    def fetch(self, code: str, start: str, end: str) -> pd.DataFrame:
        """拉 [start, end] 的日线，返回 `BAR_COLUMNS` 结构。

        `code` 是规范形式（`600519.SH`），`start`/`end` 是 `YYYY-MM-DD`。
        无数据返回空 DataFrame；源不可用抛 `SourceUnavailable`。
        """
        ...

    def trade_dates(self, start: str, end: str) -> list[str]:
        """交易日列表（`YYYY-MM-DD`）。不支持的源返回空列表。"""
        ...


def empty_bars() -> pd.DataFrame:
    """标准的空返回值，列和 dtype 都对，可以直接 concat。"""
    df = pd.DataFrame({c: pd.Series(dtype="float64") for c in _FLOAT_COLS})
    df.insert(0, "date", pd.Series(dtype="datetime64[ns]"))
    for c in _BOOL_COLS:
        df[c] = pd.Series(dtype="bool")
    return df[list(BAR_COLUMNS)]


def normalize_bars(df: pd.DataFrame) -> pd.DataFrame:
    """把源的原始输出规整成 `BAR_COLUMNS`：补缺列、转类型、按日期排序去重。

    缺失的数值列填 NaN 而不是 0——0 是个合法价格，用它当"没有"会让下游
    算出 -100% 的收益率却毫无察觉。`factor` 是唯一的例外，缺失时填 1.0
    （等价于不复权），因为一个没有除权记录的票因子本来就该是 1。
    """
    if df is None or len(df) == 0:
        return empty_bars()

    out = df.copy()
    for col in BAR_COLUMNS:
        if col not in out.columns:
            out[col] = pd.NA

    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    for c in _FLOAT_COLS:
        out[c] = pd.to_numeric(out[c], errors="coerce")
    out["factor"] = out["factor"].fillna(1.0)
    for c in _BOOL_COLS:
        # 先 astype(object) 再比较，避开 pandas 3 对 object 列 fillna 的
        # 隐式降级警告；缺失一律当 False（"不知道是否停牌" → 按可交易处理，
        # 真正的停牌过滤靠 BaoStock 的 tradestatus，见 chain 的降级告警）。
        out[c] = out[c].map(lambda v: bool(v) if v is not None and v is not pd.NA else False)
        out[c] = out[c].astype(bool)

    out = out.dropna(subset=["date"])
    # 同一天出现两行只可能是源的毛病，保留后到的那份。
    out = out.drop_duplicates(subset=["date"], keep="last")
    return out.sort_values("date").reset_index(drop=True)[list(BAR_COLUMNS)]
