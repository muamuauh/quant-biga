"""BaoStock 日线源 —— 本项目的主源。

选它当主源的唯一理由：**它是唯一不靠爬网页的免费 A股数据源。**
AKShare 覆盖更全但底层爬东方财富，批量拉必然限频/封 IP/跳验证页；
BaoStock 是自建服务器 + SDK 直连，量大也不会被判定成爬虫。

它还直接给出两个别处很难拿到的字段：
  · `tradestatus` —— 当日是否停牌
  · `isST`        —— 当日是否 ST（**当日**，不是当前，没有前视偏差）

## 两个必须知道的坑

**1. 全局会话，不是线程安全的。**
baostock 用一个模块级的 socket 连接，多线程并发调
`query_history_k_data_plus` 会让响应交错、返回互相串台的数据——而且不报错，
只是数据是错的。所以这里用一把模块级锁把所有查询串行化。
真正省时间的是增量缓存（每天只拉新增的几行），不是并发。

**2. 复权因子要靠两次查询相除得到。**
`query_history_k_data_plus` 只能给"某一种复权口径的价格"，不给因子。
`query_adjust_factor` 给的是除权除息事件，要自己前向填充到每个交易日，
边界情况很多。这里改用更笨但更可靠的办法：拉一次不复权、拉一次后复权，
`factor = hfq_close / raw_close`。多一次查询，换来一个自洽、可校验的因子。
"""

from __future__ import annotations

import io
import threading
from contextlib import redirect_stdout

import pandas as pd

from qbg.data.sources.base import SourceUnavailable, empty_bars, normalize_bars
from qbg.market import codes
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

# 见模块说明的坑 1：baostock 的会话是全局的，所有查询必须串行。
_LOCK = threading.RLock()
_logged_in = False

_FIELDS = "date,open,high,low,close,volume,amount,tradestatus,isST"

# adjustflag: 1=后复权 2=前复权 3=不复权
_ADJ_RAW = "3"
_ADJ_HFQ = "1"


def _bs():
    try:
        import baostock as bs
    except ImportError as e:  # pragma: no cover - 环境问题，测试里 mock 掉
        raise SourceUnavailable("baostock 未安装：pip install -e '.[data]'") from e
    return bs


def _ensure_login() -> None:
    """登录一次，进程内复用。

    `bs.login()` 会往 stdout 打一行 "login success!"，而本项目的 stdout 是
    JSONL 日志流——一行裸文本会让日志文件不再是合法的 JSONL，直接破坏
    store 层的重建。所以把它的输出吞掉。
    """
    global _logged_in
    if _logged_in:
        return
    bs = _bs()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rs = bs.login()
    if rs.error_code != "0":
        raise SourceUnavailable(f"baostock 登录失败: {rs.error_code} {rs.error_msg}")
    _logged_in = True
    log_event(log, "source.baostock.login.ok")


def _rs_to_df(rs) -> pd.DataFrame:
    """把 baostock 的游标结果读成 DataFrame。

    它的迭代协议很别扭：先判 error_code 再 next()，且每行是字符串列表。
    """
    rows = []
    while rs.error_code == "0" and rs.next():
        rows.append(rs.get_row_data())
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows, columns=rs.fields)


def _query(bs_code: str, start: str, end: str, adjustflag: str) -> pd.DataFrame:
    bs = _bs()
    rs = bs.query_history_k_data_plus(
        bs_code, _FIELDS,
        start_date=start, end_date=end,
        frequency="d", adjustflag=adjustflag,
    )
    if rs.error_code != "0":
        # 单只票查询失败不该拖垮整批：记录后返回空，由上层决定是否降级。
        log_event(log, "source.baostock.query.error",
                  code=bs_code, adjustflag=adjustflag,
                  error_code=rs.error_code, error_msg=rs.error_msg)
        return pd.DataFrame()
    return _rs_to_df(rs)


class BaostockSource:
    """`DailyBarSource` 实现。"""

    name = "baostock"

    def fetch(self, code: str, start: str, end: str) -> pd.DataFrame:
        bs_code = codes.to_baostock(code)
        with _LOCK:
            _ensure_login()
            raw = _query(bs_code, start, end, _ADJ_RAW)
            if raw.empty:
                return empty_bars()
            hfq = _query(bs_code, start, end, _ADJ_HFQ)

        df = raw.rename(columns={"isST": "is_st"})
        # tradestatus: 1=正常交易, 0=停牌。转成"是否停牌"。
        df["is_suspended"] = pd.to_numeric(df["tradestatus"], errors="coerce").fillna(1) == 0
        df["is_st"] = pd.to_numeric(df["is_st"], errors="coerce").fillna(0) == 1
        df["factor"] = _derive_factor(raw, hfq)
        return normalize_bars(df)

    def trade_dates(self, start: str, end: str) -> list[str]:
        with _LOCK:
            _ensure_login()
            bs = _bs()
            rs = bs.query_trade_dates(start_date=start, end_date=end)
            if rs.error_code != "0":
                log_event(log, "source.baostock.trade_dates.error",
                          error_code=rs.error_code, error_msg=rs.error_msg)
                return []
            df = _rs_to_df(rs)
        if df.empty:
            return []
        # is_trading_day: '1' = 交易日
        open_days = df[df["is_trading_day"].astype(str) == "1"]
        return sorted(open_days["calendar_date"].astype(str).tolist())


def _derive_factor(raw: pd.DataFrame, hfq: pd.DataFrame) -> pd.Series:
    """factor = 后复权收盘 / 不复权收盘，按日期对齐。

    拿不到后复权数据时退回 1.0（等价于不复权）而不是报错：因子只影响
    模型和回测的价格序列连续性，缺了它下单清单照样是对的。缺失会被
    `qbg.data.cache` 的校验记下来，不会悄悄溜走。
    """
    if hfq.empty or "close" not in hfq.columns:
        return pd.Series(1.0, index=raw.index)

    raw_close = pd.to_numeric(raw["close"], errors="coerce")
    hfq_close = pd.to_numeric(
        hfq.set_index(hfq["date"].astype(str))["close"], errors="coerce"
    )
    aligned = raw["date"].astype(str).map(hfq_close)
    factor = aligned / raw_close
    # 停牌日 close 可能是 0，相除会得到 inf/NaN——填 1.0 交给下游的连续性校验。
    return factor.replace([float("inf"), float("-inf")], pd.NA).fillna(1.0)
