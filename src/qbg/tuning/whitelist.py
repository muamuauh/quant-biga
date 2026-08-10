"""参数白名单：范围和冻结规则由代码执行，不依赖 agent 自觉。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from qbg.config import PROJECT_ROOT


@dataclass(frozen=True)
class ParamSpec:
    name: str
    scope: str
    tier: str
    kind: str
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    allowed: tuple[Any, ...] = ()

    @property
    def auto_applicable(self) -> bool:
        return self.tier in {"A", "A_risk", "A_slow"}

    def validate(self, value: Any) -> Any:
        converted = {"int": int, "float": float, "str": str, "bool": bool}[self.kind](value)
        if self.allowed and converted not in self.allowed:
            raise ValueError(f"{self.name} 只能取 {self.allowed}")
        if self.minimum is not None and converted < self.minimum:
            raise ValueError(f"{self.name} 小于下限 {self.minimum}")
        if self.maximum is not None and converted > self.maximum:
            raise ValueError(f"{self.name} 大于上限 {self.maximum}")
        return converted


def load(path: Path | None = None) -> tuple[dict[str, ParamSpec], set[str], dict]:
    raw = yaml.safe_load((path or PROJECT_ROOT / "configs" / "tunable_params.yaml").read_text(
        encoding="utf-8"))
    specs = {name: ParamSpec(name, item["scope"], item["tier"], item["type"],
                             item.get("min"), item.get("max"), item.get("step"),
                             tuple(item.get("allowed") or ()))
             for name, item in raw["params"].items()}
    return specs, set(raw.get("frozen") or ()), dict(raw.get("policy") or {})

