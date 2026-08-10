"""A股交易费用。**注意这是不对称的：印花税只在卖出时收。**

| 项 | 费率 | 方向 |
|---|---|---|
| 佣金 | 万 2.5（可配），**最低 5 元** | 双边 |
| 印花税 | 0.05%（2023-08-28 由 0.1% 减半） | **仅卖出** |
| 过户费 | 0.001%（2022-04 起沪深统一） | 双边 |

往返总成本 ≈ **10 bp**（不含滑点）。

## 为什么必须建模成不对称

回测里用一个对称的 `cost_per_turnover` 会系统性低估卖出成本 5bp。
在 `QBG_REBALANCE_EVERY_DAYS=10`（约一年 25 次调仓）的节奏下，
这个误差一年累积约 1.25 个百分点——足以把一个真实是负的策略算成正的。

所以 `buy_cost_rate` 和 `sell_cost_rate` 是两个数，`backtest/engine.py`
分别使用。

## 最低佣金什么时候咬人

10 万账户 `top_k=3`，单笔约 3 万，佣金 7.5 元 > 5 元，最低值不生效。
但如果 `top_k` 调大到 10（单笔 1 万），佣金变成 2.5 元 → 被拉到 5 元，
**实际费率翻倍到万 5**。这是"多分散一点"在小账户上要付的隐藏代价，
`estimate` 会如实反映出来。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from qbg.config import settings


@dataclass(frozen=True)
class FeeProfile:
    """费率表。默认值是行业常见水平，**不是南京证券的实测值**。

    TODO(用户提供)：确认南京证券实际佣金费率后改 configs/fee_profile.yaml。
    """

    commission_rate: float = 0.00025   # 万 2.5
    commission_min: float = 5.0        # 元
    stamp_tax_rate: float = 0.0005     # 0.05%，仅卖出
    transfer_fee_rate: float = 0.00001  # 0.001%，双边

    @classmethod
    def load(cls, path: Path | None = None) -> FeeProfile:
        """从 `configs/fee_profile.yaml` 读。文件不存在就用默认值。"""
        p = path or settings.fee_profile_yaml
        if not p.exists():
            return cls()
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: float(v) for k, v in data.items() if k in known})


@dataclass(frozen=True)
class FeeBreakdown:
    """一笔交易的费用拆解。进日报和下单清单——你要能看出钱花在哪。"""

    commission: float
    stamp_tax: float
    transfer_fee: float

    @property
    def total(self) -> float:
        return self.commission + self.stamp_tax + self.transfer_fee

    def as_dict(self) -> dict:
        return {
            "commission": round(self.commission, 2),
            "stamp_tax": round(self.stamp_tax, 2),
            "transfer_fee": round(self.transfer_fee, 2),
            "total": round(self.total, 2),
        }


def estimate(notional: float, side: str,
             profile: FeeProfile | None = None) -> FeeBreakdown:
    """估算一笔交易的费用。`side` 是 `"BUY"` 或 `"SELL"`。

    `notional` = 价格 × 股数（元）。零或负的成交额返回全 0，
    不抛异常——上游可能因为整手取整算出 0 股。
    """
    profile = profile or FeeProfile.load()
    if notional is None or notional <= 0:
        return FeeBreakdown(0.0, 0.0, 0.0)

    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side 必须是 BUY 或 SELL，收到 {side!r}")

    commission = max(notional * profile.commission_rate, profile.commission_min)
    # 印花税只在卖出时收 —— 这是 A股成本不对称的全部来源。
    stamp_tax = notional * profile.stamp_tax_rate if side == "SELL" else 0.0
    transfer_fee = notional * profile.transfer_fee_rate
    return FeeBreakdown(commission, stamp_tax, transfer_fee)


def cost_rate(side: str, profile: FeeProfile | None = None) -> float:
    """单边成本率（小数），**忽略最低佣金**。给回测的向量化成本模型用。

    忽略最低佣金是有意的近似：向量化回测按换手率算成本，没有"单笔"的
    概念。这个近似在单笔金额远大于 `commission_min / commission_rate`
    （本项目约 2 万元）时误差可忽略；单笔更小时会**低估**成本，
    所以 `min_notional_for_rate` 给出这个近似成立的下界，回测报告应该
    检查它。
    """
    profile = profile or FeeProfile.load()
    side = side.upper()
    rate = profile.commission_rate + profile.transfer_fee_rate
    if side == "SELL":
        rate += profile.stamp_tax_rate
    return rate


def min_notional_for_rate(profile: FeeProfile | None = None) -> float:
    """低于这个成交额，最低佣金就会让实际费率高于 `cost_rate`。

    回测报告应当报出"平均单笔成交额"并和它对比：低于它就说明回测低估了
    成本，结论要打折看。
    """
    profile = profile or FeeProfile.load()
    if profile.commission_rate <= 0:
        return 0.0
    return profile.commission_min / profile.commission_rate


def round_trip_rate(profile: FeeProfile | None = None) -> float:
    """买入 + 卖出的往返成本率。用来一眼判断换手率能不能承受。"""
    return cost_rate("BUY", profile) + cost_rate("SELL", profile)
