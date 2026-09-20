"""TradingAgents 对候选 A股逐票给出五档中文评级，默认 fail-open。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

from qbg.agents.ticker_map import to_ta_symbol
from qbg.agents.token_tracker import TokenTracker
from qbg.config import settings
from qbg.llm import apply_tradingagents_env
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)
RATINGS = ["Buy", "Overweight", "Hold", "Underweight", "Sell"]


def rating_rank(value: str) -> int:
    normalized = (value or "").strip().title()
    return RATINGS.index(normalized) if normalized in RATINGS else len(RATINGS)


def _is_rate_limit(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in (
        "too many requests", "rate limit", "rate-limit", "429", "ratelimit", "quota", "请求过于频繁",
    ))


def _propagate_with_retry(graph, ticker: str, trade_date: str,
                          max_retries: int = 3, base_backoff: float = 30.0):
    """仅对 429 类瞬时限流做指数退避；其他错误立即交给 fail-open 处理。"""
    import time

    for attempt in range(max_retries + 1):
        try:
            return graph.propagate(ticker, trade_date)
        except Exception as exc:  # noqa: BLE001
            if not _is_rate_limit(exc) or attempt == max_retries:
                raise
            wait = base_backoff * (2 ** attempt)
            log_event(log, "agents.review.rate_limited", ticker=ticker,
                      attempt=attempt + 1, max_retries=max_retries, wait_seconds=wait)
            time.sleep(wait)
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class ReviewVerdict:
    code: str
    rating: str
    rationale: str
    kept: bool
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _build_graph(callbacks=None):
    # 必须在首次导入 DEFAULT_CONFIG 前完成；TradingAgents 在模块导入时读取 env。
    apply_tradingagents_env()
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph import trading_graph
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    from qbg.agents import ashare_vendor

    config = DEFAULT_CONFIG.copy()
    config["benchmark_ticker"] = "000300"
    config["temperature"] = 0.0
    config["results_dir"] = str(settings.report_dir / "tradingagents")
    config["data_cache_dir"] = str(settings.snapshot_dir / "tradingagents" / "cache")
    config["memory_log_path"] = str(settings.snapshot_dir / "tradingagents" / "memory.md")
    # 上游 identity resolver 绕过 vendor registry 直连 Yahoo；A 股改用本地名称表。
    trading_graph.resolve_instrument_identity = ashare_vendor.resolve_instrument_identity
    # StockTwits/Reddit 对六位 A 股代码没有有效覆盖，还会触发 404/429。市场、新闻、
    # 基本面三路已经覆盖本策略所需事实，移除 social 能降低噪声、延迟和 token。
    analysts = ("market", "news", "fundamentals")
    graph = TradingAgentsGraph(selected_analysts=analysts, debug=False, config=config,
                               callbacks=callbacks)
    # 上游历史反思也绕过 vendor registry 直连 yfinance，并用美股 benchmark。
    # A 股同口径收益归因由 qbg 自己的回测/store 完成，这里禁用该重复且错误的路径。
    graph._resolve_pending_entries = lambda ticker: None  # noqa: SLF001
    return graph


def review_candidates(candidate_weights: dict[str, float], trade_date: date | None = None,
                      min_rating: str | None = None,
                      fail_open: bool | None = None) -> tuple[dict[str, float], list[ReviewVerdict], dict]:
    if not candidate_weights:
        return {}, [], {}
    cutoff = rating_rank(min_rating or settings.qbg_agents_min_rating)
    fail_open = bool(settings.qbg_agents_fail_open) if fail_open is None else fail_open
    tracker = TokenTracker()
    trade_day = (trade_date or date.today()).isoformat()
    log_event(log, "agents.review.start", candidates=list(candidate_weights),
              trade_date=trade_day, min_rating=min_rating or settings.qbg_agents_min_rating,
              fail_open=fail_open)
    try:
        graph = _build_graph([tracker])
    except Exception as exc:  # noqa: BLE001
        log_event(log, "agents.review.graph_init_error", error=str(exc))
        verdicts = [ReviewVerdict(code, "Error", "复核图初始化失败", fail_open, str(exc))
                    for code in candidate_weights]
        return (dict(candidate_weights) if fail_open else {}), verdicts, {}
    kept, verdicts = {}, []
    for code, weight in candidate_weights.items():
        try:
            state, rating = _propagate_with_retry(graph, to_ta_symbol(code), trade_day)
            rationale = state.get("final_trade_decision", "") if isinstance(state, dict) else ""
            passed = rating_rank(rating) <= cutoff
            verdicts.append(ReviewVerdict(code, rating, rationale, passed))
            # **date/mode 必须显式带上。** ETL 靠它们把结论写进 `verdicts` 表；
            # 缺了就只能退回拿 `ts[:10]` 猜，而盘前 08:00（北京）正好是 UTC 零点
             # 前后 —— 早跑十分钟日期就差一天。
            log_event(log, "agents.review.verdict", code=code, rating=rating, kept=passed,
                      date=trade_day, mode=settings.qbg_mode.upper())
            if passed:
                kept[code] = weight
        except Exception as exc:  # noqa: BLE001
            verdicts.append(ReviewVerdict(code, "Error", "", fail_open, str(exc)))
            if fail_open:
                kept[code] = weight
            log_event(log, "agents.review.error", code=code, error=str(exc),
                      kept=fail_open, date=trade_day, mode=settings.qbg_mode.upper())
    usage = tracker.summary()
    log_event(log, "agents.review.done", kept=list(kept),
              dropped=[verdict.code for verdict in verdicts if not verdict.kept],
              tokens=usage.get("total_tokens", 0), cost_usd=usage.get("cost_usd", 0))
    return kept, verdicts, usage
