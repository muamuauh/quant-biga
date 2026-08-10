"""第三方 OpenAI-compatible 中转站驱动的每日复盘 agent。

安全边界刻意做窄：模型只看到本地代码整理好的事实 JSON，只能返回结构化文本；
它没有 shell、文件、数据库或参数写入工具。报告与假设由本模块校验后代写，参数建议
只进入报告，绝不会绕过白名单和八项回测闸直接应用。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from qbg.agent.safety import assert_param_allowed, assert_write_allowed
from qbg.analysis.health import diagnose
from qbg.analysis.hypotheses import open_hypothesis
from qbg.config import settings
from qbg.llm import relay_client, resolve_relay
from qbg.store.etl import connect
from qbg.tuning.overlay import read as read_overlay
from qbg.tuning.whitelist import load as load_whitelist
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)
VALID_STATUS = {"normal", "attention", "manual_action"}


@dataclass
class AgentReviewResult:
    date: str
    ok: bool = False
    status: str | None = None
    summary: str | None = None
    report_path: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    hypotheses_opened: list[str] = field(default_factory=list)
    error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _fetch_rows(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    cursor = db.execute(sql, params)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def collect_facts(review_date: str | None = None, *, mode: str = "ADVISORY",
                  path: Path | None = None) -> tuple[str, dict]:
    """从派生库收集最小充分事实；不调用 LLM，也不做因果推断。"""
    db_path = path or settings.db_path
    when = review_date or date_cls.today().isoformat()
    if db_path.exists():
        # 老版本 runs.db 可能只有交易表。connect() 会幂等执行最新 schema，
        # 相当于轻量迁移，不删除也不重建已有记录。
        connect(db_path).close()
        with sqlite3.connect(db_path) as db:
            latest = db.execute(
                "SELECT MAX(date) FROM runs WHERE mode=?", (mode.upper(),)
            ).fetchone()[0]
            when = review_date or latest or when

    findings = [item.as_dict() for item in diagnose(db_path, mode.upper())]
    facts: dict[str, Any] = {
        "date": when,
        "mode": mode.upper(),
        "findings": findings,
        "limitations": [
            "数据库是日志派生索引，不是交易真相源",
            "没有新闻证据时禁止解释涨跌原因",
            "样本不足时禁止据此建议调参",
            "所有参数建议都必须另行通过八项回测闸",
        ],
    }
    if not db_path.exists():
        return when, facts

    with sqlite3.connect(db_path) as db:
        key = (when, mode.upper())
        facts.update(
            run=_fetch_rows(
                db,
                "SELECT date,mode,started_ts,finished_ts,run_kind,skipped_reason,"
                "market_risk_on,submitted,hard_ok,report_path "
                "FROM runs WHERE date=? AND mode=?",
                key,
            ),
            top_scores=_fetch_rows(
                db,
                "SELECT code,score,rank FROM scores WHERE date=? AND mode=? "
                "ORDER BY rank LIMIT 10",
                key,
            ),
            equity=_fetch_rows(db, "SELECT * FROM equity WHERE date=? AND mode=?", key),
            positions=_fetch_rows(
                db,
                "SELECT code,name,qty,sellable_qty,cost_price,last_price,market_value,pnl "
                "FROM positions WHERE date=? AND mode=? ORDER BY market_value DESC",
                key,
            ),
            executions=_fetch_rows(
                db,
                "SELECT code,side,quantity,limit_price,estimated_fee,allowed,reason "
                "FROM executions WHERE date=? AND mode=? ORDER BY code,side",
                key,
            ),
            gates=_fetch_rows(
                db, "SELECT name,passed,reason FROM gates WHERE date=? AND mode=? ORDER BY name", key
            ),
            open_hypotheses=_fetch_rows(
                db,
                "SELECT id,opened_date,topic,statement,discriminator,n_observations "
                "FROM hypotheses WHERE mode=? AND status='open' ORDER BY opened_date,id LIMIT 20",
                (mode.upper(),),
            ),
        )
    return when, facts


SYSTEM_PROMPT = """\
你是 A 股半自动量化系统的复盘分析师，位于交易链路下游，不参与下单。
你只能依据用户给出的 facts JSON 写复盘，不得补写新闻、行情、成交或因果解释。
findings 为空可以结论为正常；数据缺失必须明确写“数据不足”，不能猜测。
参数建议一次最多一个参数，只是待回测提议，绝不能声称已经应用。

只返回一个 JSON 对象，不要 Markdown 或代码围栏，结构必须为：
{
  "status": "normal|attention|manual_action",
  "summary": "一句话结论",
  "evidence": ["直接来自 facts 的证据，最多5条"],
  "hypotheses": [
    {"topic":"主题", "statement":"可证伪陈述", "discriminator":"什么观测能确认或证伪"}
  ],
  "parameter_suggestions": [
    {"parameter":"白名单参数名", "proposed_value":3, "rationale":"对应哪条事实，且需回测"}
  ],
  "actions": ["人工动作，最多3条"]
}
没有内容的数组返回 []。hypothesis 没有 discriminator 就不要输出。
"""


def _extract_json(text: str) -> dict:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`").removeprefix("json").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("中转站没有返回 JSON 对象") from None
        payload = json.loads(raw[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("复盘响应必须是 JSON 对象")
    return payload


def _validate_output(payload: dict) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    status = str(payload.get("status") or "").strip()
    summary = str(payload.get("summary") or "").strip()
    if status not in VALID_STATUS:
        raise ValueError(f"非法复盘状态: {status!r}")
    if not summary:
        raise ValueError("复盘 summary 不能为空")

    evidence = [str(item).strip() for item in (payload.get("evidence") or []) if str(item).strip()][:5]
    actions = [str(item).strip() for item in (payload.get("actions") or []) if str(item).strip()][:3]
    hypotheses = []
    for item in (payload.get("hypotheses") or [])[:3]:
        if not isinstance(item, dict):
            continue
        normalized = {key: str(item.get(key) or "").strip()
                      for key in ("topic", "statement", "discriminator")}
        if all(normalized.values()):
            hypotheses.append(normalized)
        else:
            warnings.append("丢弃缺少 topic/statement/discriminator 的假设")

    specs, frozen, _ = load_whitelist()
    suggestions = []
    for item in (payload.get("parameter_suggestions") or [])[:1]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("parameter") or "").strip()
        try:
            assert_param_allowed(name)
            if name in frozen or name not in specs:
                raise PermissionError("不在可调白名单")
            value = specs[name].validate(item.get("proposed_value"))
        except (PermissionError, ValueError, TypeError) as exc:
            warnings.append(f"丢弃参数建议 {name or '<empty>'}: {exc}")
            continue
        suggestions.append({
            "parameter": name,
            "proposed_value": value,
            "rationale": str(item.get("rationale") or "").strip(),
            "requires_backtest": True,
        })
    return {
        "status": status,
        "summary": summary,
        "evidence": evidence,
        "hypotheses": hypotheses,
        "parameter_suggestions": suggestions,
        "actions": actions,
    }, warnings


def _usage(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    result = {}
    for source, target in (("prompt_tokens", "prompt_tokens"),
                           ("completion_tokens", "completion_tokens"),
                           ("total_tokens", "total_tokens")):
        value = getattr(usage, source, None)
        if value is not None:
            result[target] = int(value)
    return result


def _render(when: str, output: dict, facts: dict, warnings: list[str]) -> str:
    lines = [f"# {when} 自动复盘", "", "## 一句话结论", "", output["summary"], "",
             "## 事实证据", ""]
    lines.extend(f"- {item}" for item in output["evidence"] or ["无"])
    lines.extend(["", "## 运行健康", ""])
    findings = facts.get("findings") or []
    lines.extend(
        f"- [{item['severity']}] {item['title']}：{item['detail']}" for item in findings
    )
    if not findings:
        lines.append("- 全部正常")
    lines.extend(["", "## 可证伪假设", ""])
    for item in output["hypotheses"]:
        lines.append(f"- **{item['topic']}**：{item['statement']}；判据：{item['discriminator']}")
    if not output["hypotheses"]:
        lines.append("- 无")
    lines.extend(["", "## 参数建议", ""])
    for item in output["parameter_suggestions"]:
        lines.append(
            f"- `{item['parameter']}={item['proposed_value']}`：{item['rationale']}"
            "（仅提议，必须另行通过八项回测闸）"
        )
    if not output["parameter_suggestions"]:
        lines.append("- 无")
    lines.extend(["", "## 人工动作", ""])
    lines.extend(f"- {item}" for item in output["actions"] or ["无"])
    if warnings:
        lines.extend(["", "## 本地校验告警", ""])
        lines.extend(f"- {item}" for item in warnings)
    lines.extend(["", "---", "本报告由第三方中转站模型生成；模型无下单和参数写入权限。", ""])
    return "\n".join(lines)


def daily_review(review_date: str | None = None, *, mode: str = "ADVISORY",
                 client: Any | None = None, db_path: Path | None = None,
                 report_dir: Path | None = None) -> AgentReviewResult:
    """生成一次复盘；任何 LLM 错误都转为结果，不影响交易主流程。"""
    when, facts = collect_facts(review_date, mode=mode, path=db_path)
    result = AgentReviewResult(date=when)
    if not int(settings.qbg_agent_enabled):
        result.error = "QBG_AGENT_ENABLED=0，复盘 agent 已关闭"
        return result

    cfg = resolve_relay()
    if client is None and not cfg.ready:
        result.error = "第三方中转站配置不完整"
        return result

    try:
        api = client or relay_client(cfg)
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps({
                    "facts": facts,
                    "current_overlay": read_overlay(),
                }, ensure_ascii=False, default=str)},
            ],
            "temperature": float(settings.qbg_agent_temperature),
            "max_tokens": int(settings.qbg_agent_max_tokens),
        }
        if int(settings.qbg_agent_json_mode):
            kwargs["response_format"] = {"type": "json_object"}
        response = api.chat.completions.create(**kwargs)
        text = response.choices[0].message.content
        output, warnings = _validate_output(_extract_json(text))

        target_dir = report_dir or settings.report_dir / "review"
        target = target_dir / f"{when}.md"
        assert_write_allowed(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render(when, output, facts, warnings), encoding="utf-8")

        db = connect(db_path)
        for finding in facts.get("findings") or []:
            db.execute(
                "INSERT OR REPLACE INTO findings VALUES (?,?,?,?,?,?,?)",
                (when, mode.upper(), finding["code"], finding["severity"], finding["title"],
                 finding["detail"], json.dumps(finding, ensure_ascii=False)),
            )
        severity_order = {"info": 0, "warn": 1, "error": 2, "critical": 3}
        max_severity = max(
            (item["severity"] for item in facts.get("findings") or []),
            key=lambda value: severity_order.get(value, -1),
            default=None,
        )
        db.execute(
            "INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?,?,?)",
            (when, mode.upper(), "daily", str(target), None,
             len(facts.get("findings") or []),
             max_severity, None),
        )
        db.commit()
        db.close()

        opened = []
        for item in output["hypotheses"]:
            hypothesis = open_hypothesis(**item, opened_date=when, mode=mode.upper(), path=db_path)
            opened.append(hypothesis.id)
        result.ok = True
        result.status = output["status"]
        result.summary = output["summary"]
        result.report_path = str(target)
        result.usage = _usage(response)
        result.hypotheses_opened = opened
        log_event(log, "agent.review.done", date=when, mode=mode.upper(), ok=True,
                  model=cfg.model, usage=result.usage, report_path=str(target))
    except Exception as exc:  # noqa: BLE001 -- 复盘失败不得影响交易路径
        safe_error = str(exc).replace(cfg.api_key, "<redacted>") if cfg.api_key else str(exc)
        result.error = f"{type(exc).__name__}: {safe_error}"
        log_event(log, "agent.review.error", date=when, mode=mode.upper(), error=result.error)
    return result
