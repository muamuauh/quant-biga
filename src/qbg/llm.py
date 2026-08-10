"""LLM 配置的唯一入口：一个中转三元组，三个受控消费者。

本项目统一使用第三方中转站常见的 OpenAI-compatible
``/v1/chat/completions`` surface：

1. P5 OCR：图片 + 文本，默认使用 quick 模型，可由 ``QBG_VISION_*`` 覆盖；
2. P7 TradingAgents：映射为其 ``openai_compatible`` provider；
3. P8 每日复盘：本地事实层先准备数据，LLM 只返回严格 JSON，不获得文件或 shell 工具。

这样复盘 agent 不再依赖 Anthropic/Claude CLI，也不会因为中转站只实现 Chat
Completions 而在夜间任务中 404。显式的消费者专用变量始终优先于统一配置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from qbg.config import PROJECT_ROOT, settings

ENV_FILE = PROJECT_ROOT / ".env"
RELAY_TA_PROVIDER = "openai_compatible"
RELAY_TA_KEY_ENV = "OPENAI_COMPATIBLE_API_KEY"
TA_PIN_KEYS = (
    "TRADINGAGENTS_LLM_PROVIDER",
    "TRADINGAGENTS_LLM_BACKEND_URL",
    "TRADINGAGENTS_DEEP_THINK_LLM",
    "TRADINGAGENTS_QUICK_THINK_LLM",
)


def redact(secret: str) -> str:
    """返回只适合排障、不能拿去使用的 key 摘要。"""
    if not secret:
        return "<unset>"
    return f"{secret[:10]}...{secret[-4:]}" if len(secret) > 18 else "<set>"


def load_env_file() -> None:
    """把项目 ``.env`` 注入进程环境，不覆盖 shell 中的显式设置。"""
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE, override=False)
    except Exception:  # noqa: BLE001 -- 坏掉的 .env 不应中断交易主流程
        pass


@dataclass(frozen=True)
class RelayConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    quick_model: str = ""
    timeout: float = 90.0

    @property
    def effective_quick_model(self) -> str:
        return self.quick_model or self.model

    @property
    def ready(self) -> bool:
        # P7/P8 明确要求走第三方中转，所以 base_url 也是就绪条件。
        return bool(self.base_url and self.api_key and self.model)


def resolve_relay() -> RelayConfig:
    return RelayConfig(
        base_url=str(settings.qbg_llm_base_url or "").strip().rstrip("/"),
        api_key=str(settings.qbg_llm_api_key or "").strip(),
        model=str(settings.qbg_llm_model or "").strip(),
        quick_model=str(settings.qbg_llm_model_quick or "").strip(),
        timeout=float(settings.qbg_llm_timeout_seconds),
    )


def relay_client(cfg: RelayConfig | None = None):
    """构造面向第三方中转站的 OpenAI SDK 客户端。"""
    from openai import OpenAI

    cfg = cfg or resolve_relay()
    if not cfg.ready:
        raise RuntimeError(
            "第三方中转站未配置完整：请设置 QBG_LLM_BASE_URL、"
            "QBG_LLM_API_KEY 和 QBG_LLM_MODEL"
        )
    return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, timeout=cfg.timeout)


@dataclass(frozen=True)
class VisionConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout: float = 90.0

    @property
    def ready(self) -> bool:
        # 允许专用 Vision 配置省略 base_url，从而使用 OpenAI SDK 官方默认端点。
        return bool(self.api_key and self.model)


def resolve_vision() -> VisionConfig:
    relay = resolve_relay()
    return VisionConfig(
        base_url=str(settings.qbg_vision_base_url or relay.base_url).strip().rstrip("/"),
        api_key=str(settings.qbg_vision_api_key or relay.api_key).strip(),
        model=str(settings.qbg_vision_model or relay.effective_quick_model).strip(),
        timeout=relay.timeout,
    )


def vision_client(cfg: VisionConfig | None = None):
    """构造多模态客户端；调用方会把账户截图上传到该端点。"""
    from openai import OpenAI

    cfg = cfg or resolve_vision()
    if not cfg.ready:
        raise RuntimeError(
            "多模态读图未配置：请设置 QBG_VISION_API_KEY/QBG_VISION_MODEL，"
            "或配置统一的 QBG_LLM_* 三元组"
        )
    kwargs: dict = {"api_key": cfg.api_key, "timeout": cfg.timeout}
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return OpenAI(**kwargs)


def tradingagents_env(cfg: RelayConfig | None = None) -> dict[str, str]:
    """推导 TradingAgents 的中转站环境变量，不覆盖人工 pin。"""
    cfg = cfg or resolve_relay()
    load_env_file()
    if not cfg.ready:
        return {}
    wanted = {
        "TRADINGAGENTS_LLM_PROVIDER": RELAY_TA_PROVIDER,
        "TRADINGAGENTS_LLM_BACKEND_URL": cfg.base_url,
        "TRADINGAGENTS_DEEP_THINK_LLM": cfg.model,
        "TRADINGAGENTS_QUICK_THINK_LLM": cfg.effective_quick_model,
        RELAY_TA_KEY_ENV: cfg.api_key,
    }
    return {key: value for key, value in wanted.items() if not os.environ.get(key)}


def apply_tradingagents_env(cfg: RelayConfig | None = None) -> dict[str, str]:
    """在导入 TradingAgents 默认配置前应用推导值。"""
    applied = tradingagents_env(cfg)
    os.environ.update(applied)
    return applied


def tradingagents_conflicts(cfg: RelayConfig | None = None) -> list[str]:
    """返回会覆盖统一三元组的显式 TradingAgents pin。"""
    cfg = cfg or resolve_relay()
    if not cfg.ready:
        return []
    load_env_file()
    expected = {
        "TRADINGAGENTS_LLM_PROVIDER": RELAY_TA_PROVIDER,
        "TRADINGAGENTS_LLM_BACKEND_URL": cfg.base_url,
        "TRADINGAGENTS_DEEP_THINK_LLM": cfg.model,
        "TRADINGAGENTS_QUICK_THINK_LLM": cfg.effective_quick_model,
    }
    return [f"{key}={os.environ[key]}" for key in TA_PIN_KEYS
            if os.environ.get(key) and os.environ[key] != expected[key]]


def describe() -> dict:
    """脱敏配置摘要，供自检脚本与日志使用。"""
    relay = resolve_relay()
    vision = resolve_vision()
    return {
        "relay_base_url": relay.base_url or "<unset>",
        "relay_model": relay.model or "<unset>",
        "relay_quick_model": relay.effective_quick_model or "<unset>",
        "relay_api_key": redact(relay.api_key),
        "relay_ready": relay.ready,
        "vision_base_url": vision.base_url or "<provider default>",
        "vision_model": vision.model or "<unset>",
        "vision_api_key": redact(vision.api_key),
        "vision_ready": vision.ready,
        "tradingagents_provider": RELAY_TA_PROVIDER if relay.ready else "<not configured>",
        "tradingagents_pins": tradingagents_conflicts(relay) or "<none>",
    }
