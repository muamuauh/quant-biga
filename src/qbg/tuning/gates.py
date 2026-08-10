"""候选参数的八项回测把关；任何一项失败都不得应用。"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class GateReport:
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def as_dict(self) -> dict:
        return {"passed": self.passed, "checks": [asdict(check) for check in self.checks]}


def evaluate(baseline: dict, candidate: dict, *, subperiod_excess: list[float],
             neighbor_sharpes: list[float], high_cost: dict,
             risk_tier: bool = False) -> GateReport:
    """输入均为确定性回测事实，零 LLM。"""
    checks = (
        Check("net_return", candidate["annual_return"] >= baseline["annual_return"],
              "净年化不低于基线"),
        Check("sharpe", candidate["sharpe"] >= baseline["sharpe"] - 0.05,
              "Sharpe 允许最多回落 0.05"),
        Check("drawdown", candidate["max_drawdown"] >= baseline["max_drawdown"] - 0.02 and
              (not risk_tier or candidate["max_drawdown"] >= -0.25), "回撤不显著恶化且风险档≤25%"),
        Check("turnover", candidate["avg_turnover"] <= baseline["avg_turnover"] * 1.20,
              "换手不增加超过 20%"),
        Check("rank_ic", candidate["rank_ic"] > 0, "Rank IC 必须为正"),
        Check("subperiod", bool(subperiod_excess) and
              sum(value >= 0 for value in subperiod_excess) >= (len(subperiod_excess) + 1) // 2,
              "至少半数子区间不劣于基线"),
        Check("plateau", len(neighbor_sharpes) >= 2 and
              min(neighbor_sharpes) >= candidate["sharpe"] - 0.20, "相邻参数处于稳定高原"),
        Check("cost_robustness", high_cost["annual_return"] > 0 and high_cost["sharpe"] > 0,
              "+75% 成本下仍为正"),
    )
    return GateReport(checks)

