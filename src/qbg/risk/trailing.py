"""止损与移动止盈：两条强制退出规则，共用峰值库、卖单和日流程里的同一条强卖链路。

## 止损（`risk_limits.yaml` 的 `stop_loss_pct`）

建仓以来亏损达到 `stop_loss_pct` 就卖。**调仓日也生效**：模型再看好也不留，
调仓日把止损的票从当天选股里剔除、槽位让给下一名；监控日直接卖成现金。

这个参数 2026-09-14 之前**从来没有接线**（yaml 里有、代码零引用），而文档一直写着
"单票由 8% 止损管"。

## 移动止盈

## 它管什么、不管什么

止损管下行，这个管**回吐**：一只票浮盈曾经摸到 `arm_pct` 以上（真赚到了），
之后从它的峰值回撤 `trail_pct`，就卖掉 —— 把大部分涨幅落袋，而不是在两个
调仓日之间眼看它跌回去。

和固定止盈（"+20% 就卖"）不同：峰值随赢家一起抬高，只有真的掉头才出场，
所以不会机械地截断右尾。

**它不管从没赚够的票。** 买进来就跌、从没到过 `arm_pct` 的票这里一概不碰 ——
那是止损的事（`risk_limits.yaml` 的 `stop_loss_pct`，2026-09-14 查明**至今没有
接线**，见 CLAUDE.md）。

## 只在非调仓日强卖

调仓日交给新一轮选股：模型仍然把一只票排在前 k，就不去砍它 —— 那等于
用一条机械规则否决模型。峰值每天都刷新（调仓日也刷），所以不会过期。

## 回测上它未必加分

quant-trading 2026-08-04 在几乎相同的配置（k=3、每 10 日、行业中性）上回测，
部署档 arm15/trail5 **夏普 1.65→1.55、年化 −5.6 点、回撤反而从 −17.9% 恶化到
−19.7%**；凡触发过的档位都变差，4 次触发 3 次卖早了。对动量型 top-K，它倾向于
砍掉仍在跑的赢家。那边操作者看过之后为了"浮盈落袋"的纪律**知情保留**。

**本项目 A股上的结论也是不开**（2026-09-14，`scripts/29_trailing_gate.py`）：
10/10 在两个窗口上收益都为正（+11.1 / +13.7 点），但 630 日窗口上回撤变差 2.4 点、
候选站在尖峰上，没过闸。详见 `config.py`。（当天早些时候的一版数字来自有复利 bug 的
引擎，已替换。）

## 峰值只在每次运行时采样

一天一次，取 09:30 那一刻的券商现价。盘中冲高又回落的那部分峰值看不见 ——
所以实盘触发会比"盯盘价"晚、比回测（按开盘价）略不同。这是日频系统的固有限制。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from qbg.config import settings
from qbg.execution import fees
from qbg.execution.base import Order
from qbg.market import rules


def peaks_path() -> Path:
    # 放 data/portfolio/：那是账户数据目录，已 gitignore。峰值价格本身就能反推持仓。
    return settings.data_dir / "portfolio" / "position_peaks.json"


def load_peaks(path: Path | None = None) -> dict[str, float]:
    """记录的峰值价，读不到就当没有。**读不到不是故障** —— 首次运行本来就没有。"""
    target = path or peaks_path()
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return {str(code): float(price) for code, price in raw.items() if float(price) > 0}
    except (OSError, ValueError, TypeError, AttributeError):
        # 半截写入或手工改坏：宁可让峰值从现价重新起算（晚触发），
        # 也不要抛异常打断日流程。
        return {}


def save_peaks(peaks: dict[str, float], path: Path | None = None) -> Path:
    target = path or peaks_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再替换：写到一半断电，读到的是旧文件而不是半截 JSON。
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(peaks, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


def update_peaks(positions: list[dict], peaks: dict[str, float]) -> dict[str, float]:
    """把每只持仓的峰值抬到现价；**不再持有的票删掉**。

    删掉是必须的：一只卖了又买回来的票，峰值要从新的建仓重新算。沿用旧峰值的话，
    重新买进的那一刻就会被判成"已从峰值大幅回撤"，立刻又被卖掉。

    成本价也参与：峰值至少是成本价。否则一只买进来就跌的票，峰值会是某个低于
    成本的价，"浮盈峰值"就成了负数 —— 判据上不会出错，但日报上会很怪。
    """
    out: dict[str, float] = {}
    for p in positions:
        code = p.get("code")
        if not code or int(p.get("qty", 0) or 0) <= 0:
            continue
        last = float(p.get("last_price", 0.0) or 0.0)
        cost = float(p.get("cost_price", 0.0) or 0.0)
        prior = float(peaks.get(code, 0.0) or 0.0)
        if last <= 0:
            # 现价暂时读不到：保留旧峰值，别让它归零（归零 = 下次从头算 = 漏触发）
            if prior > 0:
                out[code] = prior
            continue
        out[code] = max(prior, last, cost)
    return out


@dataclass(frozen=True)
class ExitHit:
    code: str
    name: str
    cost: float
    peak: float
    last: float
    sellable_qty: int
    kind: str = "trailing"          # "stop_loss" | "trailing"

    @property
    def peak_gain(self) -> float:
        return self.peak / self.cost - 1.0

    @property
    def pnl(self) -> float:
        return self.last / self.cost - 1.0

    @property
    def drawdown(self) -> float:
        return 1.0 - self.last / self.peak

    def reason(self) -> str:
        if self.kind == "stop_loss":
            return f"止损：建仓以来 {self.pnl:+.1%}"
        return (f"移动止盈：峰值浮盈 {self.peak_gain:+.1%}，现 {self.pnl:+.1%}，"
                f"从峰值回撤 {self.drawdown:.1%}")

    def as_dict(self) -> dict:
        return {"code": self.code, "name": self.name, "kind": self.kind, "cost": self.cost,
                "peak": self.peak, "last": self.last, "sellable_qty": self.sellable_qty,
                "peak_gain": round(self.peak_gain, 4), "pnl": round(self.pnl, 4),
                "drawdown": round(self.drawdown, 4), "reason": self.reason()}


# 旧名字。移动止盈最先写，测试和调用方里还在用。
TrailingHit = ExitHit


def stop_loss_hits(positions: list[dict], stop_pct: float) -> list[ExitHit]:
    """哪些持仓亏损到了止损线。`stop_pct` ≤ 0 = 关闭。

    按**成本价**算，不按峰值：止损问的是"这笔买卖亏了多少"，不是"从高点回吐了多少"
    —— 后者是移动止盈的事。一只先涨 30% 再跌回成本的票，止损不管，止盈管。
    """
    if stop_pct <= 0:
        return []
    hits = []
    for p in positions:
        code = p.get("code")
        cost = float(p.get("cost_price", 0.0) or 0.0)
        last = float(p.get("last_price", 0.0) or 0.0)
        if not code or int(p.get("qty", 0) or 0) <= 0 or cost <= 0 or last <= 0:
            continue
        if last / cost - 1.0 <= -stop_pct:
            hits.append(ExitHit(code=code, name=str(p.get("name", "")), cost=cost,
                                peak=max(cost, last), last=last, kind="stop_loss",
                                sellable_qty=int(p.get("sellable_qty", 0) or 0)))
    return hits


def forced_exits(positions: list[dict], peaks: dict[str, float], stop_pct: float,
                 arm_pct: float, trail_pct: float) -> list[ExitHit]:
    """止损 ∪ 移动止盈，同一只票只出现一次、**止损优先**（理由写得更直接）。"""
    stops = stop_loss_hits(positions, stop_pct)
    stopped = {hit.code for hit in stops}
    return stops + [hit for hit in triggered(positions, peaks, arm_pct, trail_pct)
                    if hit.code not in stopped]


def triggered(positions: list[dict], peaks: dict[str, float],
              arm_pct: float, trail_pct: float) -> list[TrailingHit]:
    """哪些持仓该止盈了。`peaks` 必须已经用 `update_peaks` 刷新过当天。

    任一参数 ≤ 0 = 关闭，返回空。
    """
    if arm_pct <= 0 or trail_pct <= 0:
        return []
    hits = []
    for p in positions:
        code = p.get("code")
        if not code or int(p.get("qty", 0) or 0) <= 0:
            continue
        cost = float(p.get("cost_price", 0.0) or 0.0)
        last = float(p.get("last_price", 0.0) or 0.0)
        peak = float(peaks.get(code, 0.0) or 0.0)
        if cost <= 0 or last <= 0 or peak <= 0:
            continue
        if peak / cost - 1.0 < arm_pct:
            continue                                  # 没赚够，没上膛
        if 1.0 - last / peak < trail_pct:
            continue                                  # 回撤还不够
        hits.append(TrailingHit(code=code, name=str(p.get("name", "")), cost=cost,
                                peak=peak, last=last,
                                sellable_qty=int(p.get("sellable_qty", 0) or 0)))
    return hits


def watchlist(positions: list[dict], peaks: dict[str, float],
              arm_pct: float, trail_pct: float, stop_pct: float = 0.0) -> list[dict]:
    """每只持仓离触发还差多少。

    移动止盈绝大多数日子什么都不做。没有这张表，日报在那些日子里只能写
    "未触发" —— 看不出是"离得很远"还是"再跌 0.3% 就卖"，而后者恰恰是
    操作者最想提前知道的。
    """
    rows = []
    for p in positions:
        code = p.get("code")
        cost = float(p.get("cost_price", 0.0) or 0.0)
        last = float(p.get("last_price", 0.0) or 0.0)
        # 只开了止损、没开止盈时不维护峰值库，峰值退回 max(成本, 现价)。
        peak = float(peaks.get(code, 0.0) or 0.0) if code else 0.0
        peak = peak if peak > 0 else max(cost, last)
        if not code or int(p.get("qty", 0) or 0) <= 0 or cost <= 0 or last <= 0:
            continue
        peak_gain = peak / cost - 1.0
        drawdown = 1.0 - last / peak
        row = {"code": code, "name": str(p.get("name", "")), "cost": cost, "peak": peak,
               "last": last, "peak_gain": round(peak_gain, 4), "drawdown": round(drawdown, 4),
               "pnl": round(last / cost - 1.0, 4),
               "armed": arm_pct > 0 and trail_pct > 0 and peak_gain >= arm_pct}
        if stop_pct > 0:
            stop_price = cost * (1.0 - stop_pct)
            row["stop_price"] = round(stop_price, 3)
            row["to_stop"] = round(stop_price / last - 1.0, 4)            # 负 = 还要再跌这么多
            row["stopped"] = last / cost - 1.0 <= -stop_pct
        if row["armed"]:
            trigger_price = peak * (1.0 - trail_pct)
            row["trigger_price"] = round(trigger_price, 3)
            row["to_trigger"] = round(trigger_price / last - 1.0, 4)   # 负 = 还要再跌这么多
            row["fired"] = drawdown >= trail_pct
        else:
            arm_price = cost * (1.0 + arm_pct)
            row["arm_price"] = round(arm_price, 3)
            row["to_arm"] = round(arm_price / last - 1.0, 4)           # 正 = 还要再涨这么多
            row["fired"] = False
        rows.append(row)
    return rows


def sell_orders(hits: list[TrailingHit], reference: dict[str, float],
                limit_base: dict[str, float], is_st: dict[str, bool],
                slippage: float, fee_profile: fees.FeeProfile | None = None
                ) -> tuple[list[Order], list[dict]]:
    """把触发的持仓变成卖单。返回 `(订单, 下不了单的及原因)`。

    **数量按可卖量，不按持有量。** 风控闸的 `t1_guard` 失败时卖单照样放行
    （"SELL 永远放行"），所以超出可卖量的那部分会被发到券商、被 T+1 静默拒绝。
    这里就地截到可卖量，而不是指望闸拦住。
    """
    orders, skipped = [], []
    for hit in hits:
        if hit.sellable_qty <= 0:
            skipped.append({"code": hit.code, "reason": "可卖量为 0（T+1 冻结）"})
            continue
        px = float(reference.get(hit.code) or hit.last)
        base = float(limit_base.get(hit.code) or 0.0)
        if px <= 0 or base <= 0:
            skipped.append({"code": hit.code, "reason": "没有参考价或昨收，算不出限价"})
            continue
        quantity = rules.normalize_sell_qty(hit.sellable_qty, hit.sellable_qty)
        limit = rules.clamp_to_limits(px * (1 - slippage), hit.code, base,
                                      is_st.get(hit.code, False))
        order = Order(code=hit.code, side="SELL", quantity=quantity, price=limit,
                      reason=hit.reason(), name=hit.name, ref_price=px)
        order.estimated_fee = fees.estimate(order.notional, "SELL", fee_profile).total
        orders.append(order)
    return orders, skipped
