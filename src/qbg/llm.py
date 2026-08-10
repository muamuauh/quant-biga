"""LLM 配置的唯一入口。三个消费者，两种 API 表面。

本项目从三个地方调 LLM，它们说的**不是同一套协议**：

  1. `qbg.portfolio` / `tools/ocr_positions.py` —— 多模态读图，OpenAI SDK 的
     `/v1/chat/completions`（带 image_url）。
  2. `qbg.agents.review`（P7，TradingAgents）—— 走 TradingAgents 自己的
     provider 注册表，最终也是 chat completions。
  3. `qbg.agent.client`（P8，Claude Agent SDK）—— `claude` CLI，说的是
     **Anthropic Messages API**（`/v1/messages`）。

要点：**一个只实现 `/v1/chat/completions` 的中转站驱动不了消费者 3。**
所以 Anthropic 那一侧有独立的配置项，不复用 OpenAI 侧的 base_url。

关于 TradingAgents 的 provider 选择（这是个容易踩的坑）：
  · TradingAgents **原生支持 deepseek**，内置 base_url `https://api.deepseek.com`
    并从 `DEEPSEEK_API_KEY` 读 key
    （见 TradingAgents/tradingagents/llm_clients/openai_client.py 的
    `_PROVIDER_BASE_URL` 和 api_key_env.py 的 `PROVIDER_API_KEY_ENV`）。
    所以本项目直接用 `TRADINGAGENTS_LLM_PROVIDER=deepseek`，不要绕道。
  · **不要用 `provider="openai"` 去接中转站**：那条路会设 `use_responses_api=True`
    即 POST `/v1/responses`，几乎没有中转站实现这个端点。
  · 如果将来真要接中转站，用 `provider="openrouter"`——它走普通 chat
    completions，且 `validators.py` 对它跳过模型名校验（中转站的模型 id 形如
    `qwen/qwen3-max`，不在任何 catalogue 里）。

优先级规则（和参数 overlay 一致）：**环境里已显式设置的 provider 专用变量
胜过本模块推导出来的值。** 已经手动 pin 了 `TRADINGAGENTS_*` 的配置不会被
悄悄覆盖——静默覆盖 pin 正是最难排查的一类故障。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from qbg.config import PROJECT_ROOT, settings

ENV_FILE = PROJECT_ROOT / ".env"

# 会盖过推导值的 pin，按关注度排序。
TA_PIN_KEYS = (
    "TRADINGAGENTS_LLM_PROVIDER",
    "TRADINGAGENTS_LLM_BACKEND_URL",
    "TRADINGAGENTS_DEEP_THINK_LLM",
    "TRADINGAGENTS_QUICK_THINK_LLM",
)


def redact(secret: str) -> str:
    """`sk-abc123...ef01` —— 够区分两把 key，不够拿去用。

    每条日志、每个 CLI 输出都走这里。key 已经躺在 .env 里了，
    这一层至少不要再把暴露面扩大。
    """
    if not secret:
        return "<unset>"
    return f"{secret[:10]}...{secret[-4:]}" if len(secret) > 18 else "<set>"


def load_env_file() -> None:
    """把 .env 灌进 os.environ，**不覆盖**已有值。

    必要性：TradingAgents 直接从 `os.environ` 读 `TRADINGAGENTS_*` 和各
    provider 的 key，而 pydantic-settings 只填充 settings 对象、不碰进程环境。
    quant-trading 踩过这个坑——.env 里的 `DEEPSEEK_API_KEY` 从没到达 LLM
    client，graph 构造时报 "API key is not set"，复核层 fail-open 放行，
    那一天看起来"全部通过"实际上根本没复核过。

    `override=False` 保证优先级正确：shell 里 export 的值仍然赢过文件，
    这符合"临时用另一把 key 跑一次"的直觉。
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(ENV_FILE, override=False)
    except Exception:  # noqa: BLE001 —— 坏掉的 .env 不该中断整个流程
        pass


# ----------------------------------------------------------------------
# 消费者 1：多模态读图（P5）
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class VisionConfig:
    base_url: str = ""
    api_key: str = ""
    model: str = ""

    @property
    def ready(self) -> bool:
        # base_url 可空（用官方端点），但 key 和 model 必须有。
        return bool(self.api_key and self.model)


def resolve_vision() -> VisionConfig:
    return VisionConfig(
        base_url=str(settings.qbg_vision_base_url or "").strip(),
        api_key=str(settings.qbg_vision_api_key or "").strip(),
        model=str(settings.qbg_vision_model or "").strip(),
    )


def vision_client(cfg: VisionConfig | None = None):
    """一个指向多模态模型的 `openai.OpenAI`。

    ⚠ 隐私提醒：调用方会把**账户持仓截图**发给这个端点。不接受的话，
    改用 `QBG_PORTFOLIO_SOURCE=manual` 手工维护 CSV。
    """
    from openai import OpenAI

    cfg = cfg or resolve_vision()
    if not cfg.ready:
        raise RuntimeError(
            "多模态读图未配置：请在 .env 里设 QBG_VISION_API_KEY 和 QBG_VISION_MODEL"
            "（可选 QBG_VISION_BASE_URL）。或改用 QBG_PORTFOLIO_SOURCE=manual。"
        )
    kwargs: dict = {"api_key": cfg.api_key}
    if cfg.base_url:
        kwargs["base_url"] = cfg.base_url
    return OpenAI(**kwargs)


# ----------------------------------------------------------------------
# 消费者 2：TradingAgents 逐票复核（P7）
# ----------------------------------------------------------------------

# DeepSeek 在 TradingAgents 的 model catalogue 里的合法 id。provider 非
# ollama/openrouter 时 validators.py 会校验模型名，写错会在 graph 构造时炸。
DEEPSEEK_MODELS = frozenset(
    {"deepseek-chat", "deepseek-reasoner", "deepseek-v4-flash", "deepseek-v4-pro"}
)


def tradingagents_conflicts() -> list[str]:
    """当前正盖过推导值的 pin。没有就返回空。

    存在的意义是**把静默变成可见**：复核层 fail-open，一个 pin 把模型指错
    地方的症状不是报错，而是"这一天看起来复核过了，其实没有"。
    由 P7 的 graph 构造处记录，也由日报呈现。
    """
    load_env_file()
    return [f"{k}={os.environ[k]}" for k in TA_PIN_KEYS if os.environ.get(k)]


def describe() -> dict:
    """脱敏后的配置摘要，给日志和排查脚本用。"""
    v = resolve_vision()
    load_env_file()
    return {
        "vision_base_url": v.base_url or "<provider default>",
        "vision_model": v.model or "<unset>",
        "vision_api_key": redact(v.api_key),
        "vision_ready": v.ready,
        "ta_provider": os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "<unset>"),
        "ta_deep_model": os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM", "<unset>"),
        "ta_quick_model": os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM", "<unset>"),
        "deepseek_api_key": redact(os.environ.get("DEEPSEEK_API_KEY", "")),
        "anthropic_api_key": redact(os.environ.get("ANTHROPIC_API_KEY", "")),
    }
