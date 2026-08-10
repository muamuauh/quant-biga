"""受白名单约束的参数 overlay、快照与回滚。"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from qbg.config import PROJECT_ROOT
from qbg.tuning.whitelist import load

OVERLAY = PROJECT_ROOT / "configs" / "tuned_params.yaml"
HISTORY = PROJECT_ROOT / "data" / "params" / "history"


def read(path: Path | None = None) -> dict:
    target = path or OVERLAY
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        return {"settings": dict(raw.get("settings") or {}),
                "risk_limits": dict(raw.get("risk_limits") or {})}
    except (OSError, yaml.YAMLError, TypeError):
        return {"settings": {}, "risk_limits": {}}


def apply_change(name: str, value: Any, *, gate_passed: bool, proposal_id: str,
                 autoapply: bool, path: Path | None = None,
                 history: Path | None = None) -> tuple[Path, Path]:
    specs, frozen, _ = load()
    if name in frozen or name not in specs:
        raise PermissionError(f"参数 {name} 已冻结或不在白名单")
    spec = specs[name]
    if not spec.auto_applicable:
        raise PermissionError(f"参数 {name} 属于 {spec.tier}，只能提案")
    if not gate_passed:
        raise PermissionError("八项回测把关未通过，拒绝应用")
    if not autoapply:
        raise PermissionError("QBG_AGENT_AUTOAPPLY 未授权")
    value = spec.validate(value)
    target, history_dir = path or OVERLAY, history or HISTORY
    history_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    snapshot = history_dir / f"{stamp}.yaml"
    if target.exists():
        shutil.copy2(target, snapshot)
    else:
        snapshot.write_text("settings: {}\nrisk_limits: {}\n", encoding="utf-8")
    data = read(target)
    data[spec.scope][name] = value
    data["last_proposal"] = proposal_id
    target.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return target, snapshot


def rollback(snapshot: Path, path: Path | None = None) -> Path:
    target = path or OVERLAY
    if not snapshot.is_file():
        raise FileNotFoundError(snapshot)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(snapshot, target)
    return target

