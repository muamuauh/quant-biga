"""TradingAgents 全图 token 与成本汇总。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

PRICING = {
    "deepseek-v4-pro": {"in_miss": 0.417, "in_hit": 0.0035, "out": 0.833},
    "deepseek-v4-flash": {"in_miss": 0.139, "in_hit": 0.0028, "out": 0.278},
    "deepseek-chat": {"in_miss": 0.28, "in_hit": 0.028, "out": 0.42},
    "deepseek-reasoner": {"in_miss": 0.55, "in_hit": 0.14, "out": 2.19},
}
_DEFAULT = {"in_miss": 1.0, "in_hit": 0.1, "out": 3.0}


@dataclass
class TokenTracker(BaseCallbackHandler):
    per_model: dict[str, dict[str, int]] = field(default_factory=dict)

    def _bump(self, model: str, incoming: int, cached: int, outgoing: int) -> None:
        usage = self.per_model.setdefault(model, {"calls": 0, "input": 0, "cached": 0, "output": 0})
        usage["calls"] += 1
        usage["input"] += incoming
        usage["cached"] += cached
        usage["output"] += outgoing

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        output = getattr(response, "llm_output", None) or {}
        model = output.get("model_name") or "unknown"
        tokens = output.get("token_usage") or {}
        incoming = int(tokens.get("prompt_tokens", 0) or 0)
        outgoing = int(tokens.get("completion_tokens", 0) or 0)
        details = tokens.get("prompt_tokens_details") or {}
        cached = int(tokens.get("prompt_cache_hit_tokens", 0) or details.get("cached_tokens", 0) or 0)
        if incoming or outgoing:
            self._bump(model, incoming, cached, outgoing)

    def _cost(self, model: str, usage: dict[str, int]) -> float:
        price = PRICING.get(model, _DEFAULT)
        return ((usage["input"] - usage["cached"]) * price["in_miss"] +
                usage["cached"] * price["in_hit"] + usage["output"] * price["out"]) / 1e6

    def summary(self) -> dict[str, Any]:
        return {"calls": sum(x["calls"] for x in self.per_model.values()),
                "input_tokens": sum(x["input"] for x in self.per_model.values()),
                "cached_input_tokens": sum(x["cached"] for x in self.per_model.values()),
                "output_tokens": sum(x["output"] for x in self.per_model.values()),
                "total_tokens": sum(x["input"] + x["output"] for x in self.per_model.values()),
                "cost_usd": round(sum(self._cost(m, x) for m, x in self.per_model.items()), 4),
                "per_model": self.per_model}

