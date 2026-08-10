"""多模态 OCR 结果解析、强制校验与原子落盘。"""

from __future__ import annotations

import base64
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from qbg.config import settings
from qbg.data import cache, meta
from qbg.llm import resolve_vision, vision_client
from qbg.market import codes, rules
from qbg.portfolio.base import PortfolioSnapshot, Position

PROMPT = """你是券商持仓表格转录器。逐字读取全部截图并合并滚动列表的重叠行。
只输出 JSON，不解释，不猜缺失数字。格式：
{"asof":"YYYY-MM-DD","总资产":0,"可用资金":0,"positions":[
{"名称":"","代码":null,"股数":0,"可用股数":0,"成本价":0,"现价":0,"市值":0,"盈亏":0}]}
同一股票出现在多张截图时只保留一行。看不清的必填数字填 null，禁止臆测。"""


@dataclass(frozen=True)
class ValidationIssue:
    field: str
    message: str
    code: str = ""
    fatal: bool = True


class PortfolioValidationError(ValueError):
    def __init__(self, issues: list[ValidationIssue]):
        self.issues = issues
        super().__init__("；".join(issue.message for issue in issues))


def _image_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def read_images(paths: list[Path]) -> dict:
    """一次请求发送所有滚动截图，让模型在同一上下文内去重。"""
    cfg = resolve_vision()
    content: list[dict] = [{"type": "text", "text": PROMPT}]
    content.extend({"type": "image_url", "image_url": {"url": _image_url(path)}} for path in paths)
    response = vision_client(cfg).chat.completions.create(
        model=cfg.model, messages=[{"role": "user", "content": content}],
        response_format={"type": "json_object"}, temperature=0,
    )
    text = response.choices[0].message.content or "{}"
    return json.loads(text)


def _number(value, field: str, issues: list[ValidationIssue], code: str = "") -> float:
    try:
        number = float(value)
        if pd.isna(number):
            raise ValueError
        return number
    except (TypeError, ValueError):
        issues.append(ValidationIssue(field, f"{code or '账户'} 的 {field} 缺失或不是数字", code))
        return 0.0


def validate_payload(payload: dict, *, name_map: dict[str, str] | None = None,
                     price_history: dict[str, tuple[float, bool]] | None = None,
                     tolerance: float = 0.01) -> tuple[PortfolioSnapshot, list[ValidationIssue]]:
    """验证 OCR JSON；任一 fatal issue 由调用方拒绝落盘。"""
    name_map = meta.load_cached() if name_map is None else name_map
    issues: list[ValidationIssue] = []
    total = _number(payload.get("总资产"), "总资产", issues)
    cash = _number(payload.get("可用资金"), "可用资金", issues)
    positions: list[Position] = []
    seen: dict[str, Position] = {}
    for raw in payload.get("positions") or []:
        name = str(raw.get("名称") or "").strip()
        raw_code = raw.get("代码")
        try:
            code = codes.normalize(raw_code) if raw_code else meta.code_of(name, mapping=name_map)
            expected_name = name_map.get(code, "")
            if expected_name and meta.norm_name(expected_name) != meta.norm_name(name):
                raise meta.UnknownNameError(f"代码 {code} 对应 {expected_name!r}，不是 {name!r}")
        except (codes.UnknownCodeError, meta.UnknownNameError, meta.AmbiguousNameError) as exc:
            issues.append(ValidationIssue("名称/代码", str(exc), str(raw_code or name)))
            continue
        qty = int(_number(raw.get("股数"), "股数", issues, code))
        sellable = int(_number(raw.get("可用股数"), "可用股数", issues, code))
        cost = _number(raw.get("成本价"), "成本价", issues, code)
        last = _number(raw.get("现价"), "现价", issues, code)
        value = _number(raw.get("市值"), "市值", issues, code)
        pnl = _number(raw.get("盈亏", 0), "盈亏", issues, code)
        if qty <= 0 or sellable < 0 or sellable > qty:
            issues.append(ValidationIssue("股数", f"{code} 股数/可用股数不合理", code))
        if qty % rules.LOT_SIZE:
            issues.append(ValidationIssue("股数", f"{code} 有 {qty % 100} 股零股，请人工确认", code,
                                          fatal=False))
        if cost <= 0:
            issues.append(ValidationIssue("成本价", f"{code} 成本价必须 > 0", code))
        if value <= 0 or abs(value - qty * last) / value >= tolerance:
            issues.append(ValidationIssue("市值", f"{code} 市值与股数×现价偏差达到 1%", code))
        history = price_history.get(code) if price_history is not None else _latest_limit_context(code)
        if history is None:
            issues.append(ValidationIssue("行情", f"{code} 无前收盘行情，无法校验涨跌停", code))
        else:
            previous, is_st = history
            low, high = rules.price_limits(code, previous, is_st)
            if not low <= last <= high:
                issues.append(ValidationIssue("现价", f"{code} 现价 {last} 不在 [{low}, {high}]", code))
        position = Position(code, name, qty, sellable, cost, last, value, pnl)
        if code in seen and seen[code] != position:
            issues.append(ValidationIssue("重复", f"{code} 在多图中出现不一致的重复行", code))
        else:
            seen[code] = position
    positions = list(seen.values())
    if total <= 0 or abs(total - (cash + sum(p.market_value for p in positions))) / total >= tolerance:
        issues.append(ValidationIssue("总资产", "总资产与可用资金+持仓市值合计偏差达到 1%"))
    snapshot = PortfolioSnapshot(str(payload.get("asof") or ""), total, cash, tuple(positions))
    return snapshot, issues


def _latest_limit_context(code: str) -> tuple[float, bool] | None:
    frame = cache.read(code)
    if len(frame) < 2:
        return None
    return float(frame.iloc[-2]["close"]), bool(frame.iloc[-1]["is_st"])


def require_valid(payload: dict, **kwargs) -> tuple[PortfolioSnapshot, list[ValidationIssue]]:
    snapshot, issues = validate_payload(payload, **kwargs)
    fatal = [issue for issue in issues if issue.fatal]
    if fatal:
        raise PortfolioValidationError(fatal)
    return snapshot, issues


def save_snapshot(snapshot: PortfolioSnapshot, root: Path | None = None) -> tuple[Path, Path]:
    root = root or settings.portfolio_dir
    current, history = root / "positions.csv", root / "history" / f"positions_{snapshot.asof}.csv"
    frame = pd.DataFrame([p.as_dict() for p in snapshot.positions])
    frame.insert(0, "asof", snapshot.asof)
    frame["total_equity"], frame["available_cash"] = snapshot.total_equity, snapshot.available_cash
    current.parent.mkdir(parents=True, exist_ok=True)
    history.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(current, index=False, encoding="utf-8-sig")
    frame.to_csv(history, index=False, encoding="utf-8-sig")
    return current, history

