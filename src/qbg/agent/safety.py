"""复盘 agent 的不可绕过写入边界。"""

from __future__ import annotations

from pathlib import Path

from qbg.config import PROJECT_ROOT

DENIED_NAMES = {".env", "risk_limits.yaml"}
DENIED_PARTS = {"portfolio", "screenshots"}
DENIED_PARAMS = {"qbg_mode", "i_confirm_real", "allow_live_mode",
                 "qbg_agent_enabled", "qbg_agent_autoapply"}


def assert_write_allowed(path: Path) -> None:
    resolved = path.resolve()
    if PROJECT_ROOT not in resolved.parents:
        raise PermissionError("禁止写项目目录外")
    relative = resolved.relative_to(PROJECT_ROOT)
    if relative.name in DENIED_NAMES or any(part.lower() in DENIED_PARTS for part in relative.parts):
        raise PermissionError(f"agent 禁止写入 {relative}")


def assert_param_allowed(name: str) -> None:
    if name.lower() in DENIED_PARAMS:
        raise PermissionError(f"agent 禁止修改安全/授权参数 {name}")

