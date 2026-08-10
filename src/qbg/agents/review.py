"""TradingAgents 对候选 A股逐票给出五档中文评级，默认 fail-open。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date

from qbg.agents.ticker_map import to_ta_symbol
from qbg.agents.token_tracker import TokenTracker
from qbg.config import settings
from qbg.llm import load_env_file
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)
RATINGS = ["Buy", "Overweight", "Hold", "Underweight", "Sell"]


def rating_rank(value: str) -> int:
    normalized = (value or "").strip().title()
    return RATINGS.index(normalized) if normalized in RATINGS else len(RATINGS)


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
    load_env_file()
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = DEFAULT_CONFIG.copy()
    config["benchmark_ticker"] = "000300"
    return TradingAgentsGraph(debug=False, config=config, callbacks=callbacks)


def review_candidates(candidate_weights: dict[str, float], trade_date: date | None = None,
                      min_rating: str | None = None,
                      fail_open: bool | None = None) -> tuple[dict[str, float], list[ReviewVerdict], dict]:
    if not candidate_weights:
        return {}, [], {}
    cutoff = rating_rank(min_rating or settings.qbg_agents_min_rating)
    fail_open = bool(settings.qbg_agents_fail_open) if fail_open is None else fail_open
    tracker = TokenTracker()
    try:
        graph = _build_graph([tracker])
    except Exception as exc:  # noqa: BLE001
        verdicts = [ReviewVerdict(code, "Error", "复核图初始化失败", fail_open, str(exc))
                    for code in candidate_weights]
        return (dict(candidate_weights) if fail_open else {}), verdicts, {}
    kept, verdicts = {}, []
    for code, weight in candidate_weights.items():
        try:
            state, rating = graph.propagate(to_ta_symbol(code), (trade_date or date.today()).isoformat())
            rationale = state.get("final_trade_decision", "") if isinstance(state, dict) else ""
            passed = rating_rank(rating) <= cutoff
            verdicts.append(ReviewVerdict(code, rating, rationale, passed))
            if passed:
                kept[code] = weight
        except Exception as exc:  # noqa: BLE001
            verdicts.append(ReviewVerdict(code, "Error", "", fail_open, str(exc)))
            if fail_open:
                kept[code] = weight
            log_event(log, "agents.review.error", code=code, error=str(exc))
    return kept, verdicts, tracker.summary()

