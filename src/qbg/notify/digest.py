"""从 store 组装每日邮件摘要。

## 为什么从 store 读，而不是把当次运行的 result 拼一拼

`build_daily_message(result)` 只能用**这一次运行还活着时**攒在内存里的东西。
但邮件最需要看的那一晚，恰恰是流程半路挂掉、`result` 残缺不全的那一晚。
store 是从 JSONL 日志重建出来的派生索引，它记着**已经发生过**的事实，所以：

  · 每一项查询**独立降级**（`_safe`）——净值表坏了不该连带假设一起丢
  · 可以为**任意历史日期**补发（`build_digest("2026-08-07")`）
  · 跨天的东西（未决假设）本来就只存在于 store 里，内存 result 里没有

## 一份产物，两个视图

日报和复盘报告都**整篇内联**，不再二次摘要。摘要会失真，而且一旦摘要和原文
不一致，你没法知道该信哪个。邮件正文是 Markdown，同时渲染成 HTML 和纯文本
兜底——同一份内容，两种呈现。

## 安静跳过必须在查 store 之前

周末 `run_daily.bat` 在市场检查处就退出，**根本没写 runs 行**。如果先查 store
再判断，周末会被报成「无运行记录」——一周两次假警报，更糟的是它让真正的漏跑
和周末长得一模一样。这条是从 quant-agent 的实测教训直接移植过来的。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date as _date
from pathlib import Path
from typing import Any, TypeVar

from qbg.config import settings
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

T = TypeVar("T")

# 失败步骤里附多少行日志尾巴。够定位问题，又不至于把邮件撑爆。
FAILURE_TAIL_LINES = 15

# 严重度排序，用于挑出最高级别。
_SEVERITY_ORDER = {"info": 0, "warn": 1, "error": 2, "critical": 3}


def _safe(fn: Callable[[], T], what: str) -> T | None:
    """跑 `fn`，任何异常都吞掉并记日志。

    这是本模块的核心纪律：**一项查询失败不能带走整封邮件**。宁可发一封缺了
    某个章节的信，也不要因为净值表读不出来就什么都不发——那正是最需要收到
    邮件的时候。
    """
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 —— 通知是旁路，绝不向上抛
        log_event(log, "digest.lookup_failed", what=what,
                  error=f"{type(exc).__name__}: {exc}"[:200])
        return None


def _rows(sql: str, params: tuple = (), db_path: Path | None = None) -> list[dict]:
    path = db_path or settings.db_path
    if not Path(path).exists():
        return []
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql, params).fetchall()]


def _money(value: Any) -> str:
    try:
        return f"{float(value):+,.2f}"
    except (TypeError, ValueError):
        return "—"


def _amount(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "—"


# ----------------------------------------------------------------------
# 各项事实
# ----------------------------------------------------------------------


def run_row(when: str, mode: str, db_path: Path | None = None) -> dict | None:
    """运行行，并补上当天的目标持仓数（`runs` 表本身不存这个）。"""
    rows = _rows("SELECT * FROM runs WHERE date=? AND mode=?", (when, mode), db_path)
    if not rows:
        return None
    row = dict(rows[0])
    n = _rows("SELECT COUNT(*) AS n FROM plans WHERE date=? AND mode=?",
              (when, mode), db_path)
    row["n_plans"] = n[0]["n"] if n else 0
    return row


def equity_row(when: str, mode: str, db_path: Path | None = None) -> dict | None:
    """净值行，并**派生**当日盈亏与持仓数。

    `equity` 表只存 `total_equity` / `available_cash`，没有盈亏字段。当日盈亏
    在这里算成「今日净值 − 上一有记录日的净值」，而不是去读券商的当日盈亏——
    顾问模式下根本没有券商可读，而且这个口径和净值曲线天然自洽（曲线上相邻
    两点之差就是它）。第一天没有前值，返回 None 而不是 0：`—` 是诚实的，
    `+0.00` 会被误读成"今天平盘"。
    """
    rows = _rows("SELECT * FROM equity WHERE date=? AND mode=?", (when, mode), db_path)
    if not rows:
        return None
    row = dict(rows[0])

    prev = _rows("SELECT total_equity FROM equity WHERE mode=? AND date<? "
                 "ORDER BY date DESC LIMIT 1", (mode, when), db_path)
    try:
        row["day_pnl"] = (float(row["total_equity"]) - float(prev[0]["total_equity"])
                          if prev and prev[0].get("total_equity") is not None else None)
    except (TypeError, ValueError):
        row["day_pnl"] = None

    n = _rows("SELECT COUNT(*) AS n FROM positions WHERE date=? AND mode=?",
              (when, mode), db_path)
    row["n_positions"] = n[0]["n"] if n else None
    return row


def findings(when: str, mode: str, db_path: Path | None = None) -> list[dict]:
    return _rows(
        "SELECT * FROM findings WHERE date=? AND mode=? ORDER BY severity DESC",
        (when, mode), db_path)


def due_hypotheses(when: str, mode: str, db_path: Path | None = None) -> list[dict]:
    """到期该复查的开放假设。

    `check_after IS NULL` 的也算到期：一个没写复查日期的开放假设，不主动
    提醒就等于永远沉在库里。宁可多提醒一次。
    """
    return _rows(
        "SELECT * FROM hypotheses WHERE mode=? AND status='open' "
        "AND (check_after IS NULL OR check_after<=?) ORDER BY opened_date",
        (mode, when), db_path)


def last_failure(when: str, mode: str, db_path: Path | None = None) -> dict | None:
    """当天最后一条 error 级事件，附日志尾巴。

    出事时不用登机器翻 JSONL——最需要的那 15 行直接送到眼前。
    """
    rows = _rows(
        "SELECT * FROM events WHERE date=? AND level IN ('ERROR','CRITICAL') "
        "ORDER BY ts DESC LIMIT 1", (when,), db_path)
    if not rows:
        return None
    row = rows[0]
    payload = _safe(lambda: json.loads(row.get("payload_json") or "{}"),
                    "failure_payload") or {}
    return {
        "msg": row.get("msg"),
        "ts": row.get("ts"),
        "detail": payload.get("error") or payload.get("reason") or "",
        "tail": _log_tail(when),
    }


def _log_tail(when: str) -> list[str]:
    """当天日志的最后若干行（只取 ERROR/CRITICAL 附近的）。"""
    path = settings.log_dir / "qbg.jsonl"
    if not path.exists():
        return []
    lines: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if when not in raw:
                continue
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("level") in ("ERROR", "CRITICAL"):
                lines.append(f"{rec.get('ts', '')[:19]} {rec.get('msg')} "
                             f"{rec.get('error') or rec.get('reason') or ''}".strip())
    return lines[-FAILURE_TAIL_LINES:]


def review_path(when: str, mode: str, db_path: Path | None = None) -> Path | None:
    """复盘报告路径。优先从 store 的 reviews 表取，退回约定目录。"""
    rows = _rows("SELECT path FROM reviews WHERE date=? AND mode=? AND kind='daily'",
                 (when, mode), db_path)
    if rows and rows[0].get("path"):
        p = Path(rows[0]["path"])
        if p.exists():
            return p
    fallback = settings.report_dir / "review" / f"{when}.md"
    return fallback if fallback.exists() else None


def daily_report_path(when: str) -> Path | None:
    p = settings.report_dir / "daily" / f"{when}.md"
    return p if p.exists() else None


# ----------------------------------------------------------------------
# 状态与主题
# ----------------------------------------------------------------------


# 跳过原因 → 给人看的说法。直接把 `not_rebalance_day` 写进主题没人看得懂，
# 而且「监控日」和「出事了」是两件完全不同的事，主题必须让人一眼分开。
SKIP_LABELS = {
    "not_rebalance_day": "监控日",
    "not_trading_day": "非交易日",
    # 盘前/盘后触发的安静跳过**不是故障**，主题不带 ⚠。每天登录都收到一封
    # 像出事了的邮件，人很快就不看邮件了 —— 而这套系统的安全网全靠人看邮件。
    "not_trading_session": "非交易时段·已跳过",
    "already_completed_today": "今日已运行",
}


def status_tag(run: dict | None, finds: list[dict], failure: dict | None) -> str:
    """一句话结论，直接进主题——不打开邮件就知道发生了什么。"""
    if failure:
        return f"✗ 失败于 {failure.get('msg') or '未知步骤'}"
    if not run:
        return "✗ 无运行记录"
    if run.get("skipped_reason"):
        reason = str(run["skipped_reason"])
        return SKIP_LABELS.get(reason, reason)
    if not run.get("hard_ok", 1):
        return "⚠ 硬闸中止"
    worst = max((_SEVERITY_ORDER.get(str(f.get("severity")), -1) for f in finds),
                default=-1)
    if worst >= _SEVERITY_ORDER["critical"]:
        return "⚠ critical"
    if worst >= _SEVERITY_ORDER["error"]:
        return "⚠ 有错误"
    if worst >= _SEVERITY_ORDER["warn"]:
        return "⚠ 有告警"
    # 订单数优先用调用方给的**实际允许订单**。store 里没有订单表：`plans` 存的是
    # 目标持仓（几只票），和订单笔数是两回事——3 只目标可能对应 8 笔买卖单。
    # 早期版本拿 plans 的行数当订单数，主题写"订单建议3笔"而日报写 8 笔，
    # 两个数字互相打架。
    orders = run.get("allowed_orders")
    n_orders = len(orders) if isinstance(orders, (list, tuple)) else None
    n_plans = run.get("n_plans")

    # **主题必须反映券商那边实际发生了什么。** 2026-08-25 实测那次：
    # 3 笔单一笔都没进券商，主题却是「已提交3笔」—— `submitted` 指的是
    # "顾问清单已落盘"，不是"券商收到了单"。人只看主题，报喜的主题最误导。
    portfolio = run.get("portfolio") or {}
    if str(portfolio.get("source") or "") == "default" and portfolio.get("degraded"):
        return "⚠ 持仓读不到·结果不可用"
    broker = run.get("broker")
    if broker is not None:
        ok = sum(1 for o in (broker.get("outcomes") or []) if o.get("ok"))
        planned = n_orders if n_orders is not None else len(broker.get("outcomes") or [])
        if ok == 0:
            return f"⚠ 下单失败 0/{planned}笔"
        if ok < planned:
            return f"⚠ 部分成交 {ok}/{planned}笔"
        return f"券商已接单{ok}笔"

    if run.get("submitted"):
        return f"已生成清单{n_orders if n_orders is not None else n_plans or 0}笔"
    if n_orders:
        return f"订单建议{n_orders}笔·未执行"
    if n_orders == 0 and n_plans:
        return f"目标{n_plans}只·无需调仓"
    if n_plans:
        # 拿不到订单笔数时只说目标持仓，不假装知道要下几笔单。
        return f"目标{n_plans}只"
    return "正常·无订单"


# ----------------------------------------------------------------------
# 组装
# ----------------------------------------------------------------------


def build_digest(when: str | None = None, mode: str | None = None,
                 db_path: Path | None = None,
                 fallback_run: dict | None = None) -> tuple[str, str]:
    """返回 `(主题, Markdown 正文)`。**任何单项失败都不会让它抛异常。**

    `fallback_run` 是 store 里查不到运行行时的兜底（通常是日循环手里的
    `result`）。store 仍然是真相源——只有它**没有**记录时才用兜底。这不是
    可有可无的：ETL 自己失败的那天，store 是空的，但那恰恰是最需要发信说
    清楚状况的一天，而调用方手里明明还攥着运行事实。
    """
    when = str(when or _date.today().isoformat())
    mode = str(mode or settings.qbg_mode).upper()

    run = _safe(lambda: run_row(when, mode, db_path), "runs") or fallback_run
    # 订单列表 store 里**根本没有**（只有 plans 存目标持仓），所以从调用方那里
    # 叠加过来不构成"真相源打架"——它是纯附加信息。没有它主题只能说"目标3只"，
    # 而人真正要知道的是"今天要敲 8 笔单"。
    # TODO: 更彻底的做法是给 store 加一张 orders 表，这样补发历史邮件也有单数。
    if run is not None and fallback_run and "allowed_orders" in fallback_run:
        run = {**run, "allowed_orders": fallback_run["allowed_orders"]}
    equity = _safe(lambda: equity_row(when, mode, db_path), "equity")
    finds = _safe(lambda: findings(when, mode, db_path), "findings") or []
    due = _safe(lambda: due_hypotheses(when, mode, db_path), "hypotheses") or []
    failure = _safe(lambda: last_failure(when, mode, db_path), "failure")

    status = status_tag(run, finds, failure)
    subject = f"[量化-{mode}] 每日报告 {when} — {status}"
    if equity and equity.get("day_pnl") is not None:
        subject += f" {_money(equity.get('day_pnl'))}"

    report = daily_report_path(when)

    md: list[str] = [f"# 每日报告 · {when}", "", f"*{mode} · {status}*", ""]

    # --- 当日盈亏：日报没有这一项 ---
    # 它是**跨天**派生的（今日净值 − 上一有记录日），日报只看当天一天的事实，
    # 拿不到这个数。所以无论日报在不在，这一行都要有。
    if equity and equity.get("day_pnl") is not None:
        md += [f"**当日盈亏 {_money(equity['day_pnl'])}**"
               f"（总资产 {_amount(equity.get('total_equity'))}）", ""]

    # --- 概览：只在日报缺失时重建 ---
    # 日报正文自己就有一份更全的概览。日报在的时候再放一份，读者会看到两个
    # 「概览」互相打架；日报不在的时候，这是唯一的账户事实来源。
    if report is None and (equity or run):
        md += ["## 概览（日报缺失时的重建）", "", "| 项目 | 值 |", "|---|---:|"]
        if equity:
            md.append(f"| 总资产 | {_amount(equity.get('total_equity'))} |")
            md.append(f"| 可用资金 | {_amount(equity.get('available_cash'))} |")
            n_pos = equity.get("n_positions")
            md.append(f"| 持仓数 | {'—' if n_pos is None else n_pos} |")
        if run:
            md.append(f"| 硬闸 | {'通过' if run.get('hard_ok') else '中止'} |")
            md.append(f"| 已提交 | {'是' if run.get('submitted') else '否'} |")
        md.append("")

    # --- 到期的未决假设：复盘 agent 的跨天记忆 ---
    if due:
        md += ["## 到期的未决假设", "",
               "> 这些是复盘 agent 之前提出、约定要复查的问题。"
               "每条都带 discriminator（什么观测能把它证实或证伪）。", ""]
        for h in due:
            md.append(f"- `{h.get('id')}` **{h.get('topic')}** — {h.get('statement')}")
            md.append(f"  - 判据：{h.get('discriminator')}")
            md.append(f"  - 开于 {h.get('opened_date')}，已携带 "
                      f"{h.get('n_observations', 1)} 次")
        md.append("")

    # --- 运行健康：同样只在日报缺失时重建（日报和复盘各自都有一节） ---
    if report is None and finds:
        md += ["## 运行健康（日报缺失时的重建）", ""]
        for f in finds:
            md.append(f"- **[{f.get('severity')}]** `{f.get('code')}` "
                      f"{f.get('title') or ''} — {f.get('detail') or ''}")
        md.append("")

    # --- 失败步骤 ---
    if failure:
        md += ["## 失败步骤", "",
               f"`{failure.get('msg')}` @ {str(failure.get('ts'))[:19]}", ""]
        if failure.get("detail"):
            md += [f"> {failure['detail']}", ""]
        tail = failure.get("tail") or []
        if tail:
            md += ["```"] + list(tail) + ["```", ""]

    # --- 整篇内联：日报 + 复盘 ---
    # 日报的「自动复盘」一节只有三行摘要（状态/分级/结论），所以把复盘全文
    # 再内联一遍是**补充**不是重复。
    md += _inline(report, "日报正文",
                  f"没有日报文件：`{settings.report_dir / 'daily' / (when + '.md')}`")
    md += _inline(_safe(lambda: review_path(when, mode, db_path), "review_path"),
                  "自动复盘",
                  "今天没有复盘报告（P8 未启用或未运行）。")

    return subject, "\n".join(md)


def _inline(path: Path | None, title: str, missing_note: str) -> list[str]:
    """整篇内联一个 Markdown 文件；缺失或读不出就留一句说明。

    刻意不二次摘要：摘要会失真，而且一旦摘要和原文不一致，你没法知道
    该信哪个。
    """
    out = ["---", "", f"## {title}", ""]
    if path is None:
        return out + [f"> {missing_note}", ""]
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError as exc:
        return out + [f"> 读取失败 `{path}`：`{type(exc).__name__}: {exc}`", ""]
    if not text:
        return out + [f"> 文件是空的：`{path}`", ""]
    # 去掉被内联文件自己的一级标题，避免邮件里出现两个 h1。
    lines = text.splitlines()
    if lines and lines[0].lstrip().startswith("# "):
        lines = lines[1:]
    return out + lines + [""]
