"""TradingAgents 的 A股数据 vendor。

行情和技术指标只读 quant-biga parquet，确保 LLM 与训练/回测同口径；财务与新闻
低频调用 AKShare 并 fail-soft 到文本缓存。取不到时固定返回“无数据”，让 prompt
没有可供幻觉填空的含糊空间。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from qbg.config import settings
from qbg.data import cache
from qbg.market import codes

_NO_DATA = "无数据：数据源未返回可核验内容，禁止推测或编造。"


def _code(symbol: str) -> str:
    raw = re.sub(r"\D", "", str(symbol))
    return codes.normalize(raw)


def get_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    code = _code(symbol)
    frame = cache.read(code)
    frame = frame[(frame["date"] >= pd.Timestamp(start_date)) &
                  (frame["date"] <= pd.Timestamp(end_date))]
    if frame.empty:
        return f"{code} {start_date} 至 {end_date} {_NO_DATA}"
    wanted = ["date", "open", "high", "low", "close", "volume", "amount", "turn"]
    out = frame[[column for column in wanted if column in frame]].copy()
    out["date"] = pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d")
    return f"# {code} 本地同口径行情，共 {len(out)} 行\n" + out.to_csv(index=False)


def get_indicators(symbol: str, indicator: str, curr_date: str, look_back_days: int) -> str:
    from stockstats import wrap

    code = _code(symbol)
    frame = cache.hfq(cache.read(code))
    start = pd.Timestamp(curr_date) - pd.Timedelta(days=look_back_days)
    frame = frame[(frame["date"] >= start) & (frame["date"] <= pd.Timestamp(curr_date))]
    if frame.empty:
        return f"{code} {indicator} {_NO_DATA}"
    prepared = frame.rename(columns={c: c.capitalize() for c in ("date", "open", "high", "low",
                                                                  "close", "volume")})
    try:
        stats = wrap(prepared[["Date", "Open", "High", "Low", "Close", "Volume"]].copy())
        values = stats[indicator]
    except Exception as exc:  # noqa: BLE001
        return f"{code} 指标 {indicator} 无数据：{exc}。禁止推测或编造。"
    lines = [f"{pd.Timestamp(day).date()}: {value:.6g}" for day, value in
             zip(prepared["Date"], values, strict=False) if pd.notna(value)]
    return f"## {code} {indicator}（本地后复权行情）\n" + "\n".join(lines[-look_back_days:])


def _cache_dir() -> Path:
    path = settings.snapshot_dir / "agents"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _cached(key: str, fetch: Callable[[], str]) -> str:
    path = _cache_dir() / f"{key}.txt"
    try:
        text = fetch().strip()
        if text:
            path.write_text(text, encoding="utf-8")
            return text
    except Exception:  # noqa: BLE001
        pass
    return path.read_text(encoding="utf-8") if path.exists() else _NO_DATA


def _financial_table(symbol: str, statement: str) -> str:
    code, digits = _code(symbol), codes.digits(_code(symbol))

    def fetch() -> str:
        import akshare as ak

        if statement == "摘要":
            fn = getattr(ak, "stock_financial_abstract_ths", None) or ak.stock_financial_abstract
            frame = fn(symbol=digits)
        else:
            frame = ak.stock_financial_report_sina(stock=digits, symbol=statement)
        return f"# {code} {statement}\n" + frame.tail(12).to_csv(index=False)

    return _cached(f"financial_{digits}_{statement}", fetch)


def get_fundamentals(ticker: str, curr_date: str | None = None) -> str:
    return _financial_table(ticker, "摘要")


def get_balance_sheet(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    return _financial_table(ticker, "资产负债表")


def get_cashflow(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    return _financial_table(ticker, "现金流量表")


def get_income_statement(ticker: str, freq: str = "quarterly", curr_date: str | None = None) -> str:
    return _financial_table(ticker, "利润表")


def get_news(ticker: str, start_date: str, end_date: str) -> str:
    code, digits = _code(ticker), codes.digits(_code(ticker))

    def fetch() -> str:
        import akshare as ak

        sections = []
        for title, call in (
            ("个股新闻", lambda: ak.stock_news_em(symbol=digits)),
            ("公告", lambda: ak.stock_notice_report(symbol=digits)),
            ("券商研报", lambda: ak.stock_research_report_em(symbol=digits)),
        ):
            try:
                frame = call()
                if frame is not None and not frame.empty:
                    sections.append(f"## {title}\n{frame.head(20).to_csv(index=False)}")
            except Exception:  # noqa: BLE001
                continue
        return f"# {code} 可核验资讯\n" + "\n".join(sections)

    return _cached(f"news_{digits}_{end_date}", fetch)


def get_global_news(curr_date: str, look_back_days: int | None = None,
                    limit: int | None = None) -> str:
    return "A股宏观新闻无数据：本期未配置稳定的可核验宏观源，禁止推测或编造。"


def get_insider_transactions(ticker: str, curr_date: str | None = None) -> str:
    return "A股内部人交易无数据：免费源没有可靠同口径接口，禁止推测或编造。"
