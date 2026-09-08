"""按 `QBG_PORTFOLIO_SOURCE` 选持仓源，并处理降级。

在 P9a 之前 `daily_cycle` 是**硬编码** `ManualSource()` 的 —— 配置项存在但没人读。
当时没出问题只是因为 ocr 和 manual 恰好写同一份 CSV；接入 easytrader 后这个
洞就必须补上。
"""

from __future__ import annotations

from dataclasses import dataclass

from qbg.config import settings
from qbg.portfolio.base import PortfolioSnapshot
from qbg.portfolio.manual import ManualSource
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)


@dataclass(frozen=True)
class LoadedPortfolio:
    """持仓 + **它是怎么来的**。

    来源和降级原因必须一路带到日报里。静默降级比读失败更危险：用过期持仓
    出的清单看上去和正常清单一模一样，没人会发现。
    """

    snapshot: PortfolioSnapshot
    source: str
    degraded_from: str | None = None
    degraded_reason: str | None = None

    @property
    def degraded(self) -> bool:
        return self.degraded_from is not None


def _build(name: str):
    if name == "easytrader":
        from qbg.portfolio.easytrader_source import EasytraderSource

        return EasytraderSource()
    # ocr 与 manual 读的是同一份 CSV：ocr 是写入方（tools/ocr_positions.py），
    # 读取方两者一致，所以这里共用 ManualSource。
    return ManualSource()


def load_portfolio(source: str | None = None) -> LoadedPortfolio:
    """按配置读持仓；easytrader 失败时按配置降级到 CSV。"""
    name = (source or settings.qbg_portfolio_source or "ocr").lower()
    try:
        return LoadedPortfolio(_build(name).load(), name)
    except Exception as exc:  # noqa: BLE001 —— 降级判断只看「有没有失败」
        if name != "easytrader" or not settings.qbg_ths_fallback_to_csv:
            raise
        reason = f"{type(exc).__name__}: {exc}"
        log_event(log, "portfolio.degraded", source=name, fallback="ocr", reason=reason)
        snapshot = ManualSource().load()      # 这一步再失败就该抛出去，没有第三条路
        return LoadedPortfolio(snapshot, "ocr", degraded_from=name, degraded_reason=reason)
