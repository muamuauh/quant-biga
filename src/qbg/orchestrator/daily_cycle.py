"""一条命令完成交易日检查、选股、风控、顾问清单、日报和 store。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, date, datetime

import pandas as pd

from qbg.config import PROJECT_ROOT, settings
from qbg.data import cache, meta
from qbg.execution.advisory import AdvisoryAdapter
from qbg.execution.order_planner import plan_orders
from qbg.market import calendar
from qbg.model.train import train
from qbg.orchestrator.run_marker import (
    already_completed_today,
    is_rebalance_day,
    save_marker,
    save_rebalance_date,
)
from qbg.portfolio.manual import ManualSource
from qbg.report.daily_report import generate
from qbg.risk.gates import load_limits, run_all_gates
from qbg.store.etl import backfill
from qbg.strategy.predict import latest_date_scores, load_latest_predictions
from qbg.strategy.regime import market_risk_on
from qbg.strategy.topk_weights import affordable_scores, renormalize_weights, topk_equal_weight
from qbg.utils.logging import get_logger, log_event

log = get_logger("qbg.orchestrator.daily_cycle")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def run_daily(*, today: str | None = None, skip_ingest: bool = False,
              retrain: bool = False, dry_run: bool = False, force: bool = False,
              default_equity: float = 100_000.0) -> dict:
    today = today or date.today().isoformat()
    result: dict = {"date": today, "mode": settings.qbg_mode.upper(), "started_ts": _now(),
                    "run_kind": "dry_run" if dry_run else "rebalance", "hard_ok": True,
                    "submitted": False, "market_risk_on": False, "orders": [],
                    "allowed_orders": [], "targets": {}, "scores": [], "gates": []}
    if not force and not calendar.is_trading_day(today):
        result["skipped_reason"] = "not_trading_day"
        return _finish(result, dry_run)
    if not force and already_completed_today(today):
        result["skipped_reason"] = "already_completed_today"
        return _finish(result, dry_run)
    if not skip_ingest:
        subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "01_ingest.py"),
                        "--skip-meta"], check=True)
    if retrain:
        train(live=True)

    try:
        snapshot = ManualSource().load()
        equity, cash = snapshot.total_equity, snapshot.available_cash
        positions = [p.as_dict() for p in snapshot.positions]
    except (FileNotFoundError, pd.errors.EmptyDataError):
        equity, cash, positions = default_equity, default_equity, []
    result["account"] = {"total_equity": equity, "available_cash": cash}
    result["positions"] = positions
    if not is_rebalance_day(settings.qbg_rebalance_every_days, today,
                            cash_fraction=cash / equity if equity else 0,
                            cash_trigger=settings.qbg_rebalance_cash_trigger):
        result["skipped_reason"] = "not_rebalance_day"
        result["run_kind"] = "monitoring"
        return _finish(result, dry_run)

    current = {p["code"]: int(p["qty"]) for p in positions}
    sellable = {p["code"]: int(p["sellable_qty"]) for p in positions}
    scores = latest_date_scores(load_latest_predictions(), neutralize=bool(settings.qbg_industry_neutral))
    result["scores"] = [{"code": code, "score": float(score)} for code, score in scores.items()]
    names = meta.load_cached()
    last, previous, st, suspended, dates = {}, {}, {}, {}, []
    for code in set(scores.index) | set(current):
        frame = cache.read(code)
        if frame.empty:
            continue
        row = frame.iloc[-1]
        last[code] = float(row.close)
        previous[code] = float(frame.iloc[-2].close if len(frame) > 1 else row.close)
        st[code], suspended[code] = bool(row.is_st), bool(row.is_suspended)
        dates.append(pd.Timestamp(row.date))
    limits = load_limits()
    filtered = affordable_scores(scores, last, equity, settings.qbg_top_k,
                                 cap=float(limits["max_position_pct"]))
    risk_on = market_risk_on(list(last), settings.qbg_market_sma)
    review_verdicts, review_usage = [], {}
    reviewed_scores = filtered
    if risk_on and settings.qbg_agents_enabled:
        from qbg.agents.review import review_candidates

        candidate_count = min(settings.qbg_agents_candidates, len(filtered))
        candidate_scores = filtered.iloc[:candidate_count]
        candidate_weights = {code: 1.0 / candidate_count for code in candidate_scores.index}
        kept, verdicts, review_usage = review_candidates(candidate_weights)
        reviewed_scores = candidate_scores.loc[[code for code in candidate_scores.index if code in kept]]
        review_verdicts = [verdict.as_dict() for verdict in verdicts]
    targets = topk_equal_weight(reviewed_scores, settings.qbg_top_k, 0.95, set(current),
                                settings.qbg_keep_rank) if risk_on else {}
    targets = renormalize_weights(targets, 0.95, float(limits["max_position_pct"]))
    orders = plan_orders(targets, current, last, previous, equity, names=names, is_st=st,
                         slippage=float(limits["order_slippage_pct"]),
                         drift_band=float(limits["rebalance_drift_band"]))
    hard_ok, allowed, gate_results = run_all_gates(
        target_weights=targets, orders=orders, current_cash=cash, total_equity=equity,
        today_pnl=0, latest_data_date=max(dates) if dates else None, asof=today,
        prev_close=previous, market_price=last, is_st=st, suspended=suspended,
        sellable_qty=sellable, current_qty=current, limits=limits)
    result.update(market_risk_on=risk_on, targets=targets, hard_ok=hard_ok,
                  agent_verdicts=review_verdicts, agent_usage=review_usage,
                  orders=[o.as_dict() for o in orders], allowed_orders=[o.as_dict() for o in allowed],
                  gates=[g.__dict__ for g in gate_results])
    if hard_ok and not dry_run:
        execution = AdvisoryAdapter().submit(allowed, today, gate_results)
        result["submitted"] = True
        result["order_artifacts"] = list(execution.artifacts)
        save_marker(list(execution.artifacts), today)
        save_rebalance_date(today)
    return _finish(result, dry_run)


def _finish(result: dict, dry_run: bool) -> dict:
    result["finished_ts"] = _now()
    report = generate(result)
    result["report_path"] = str(report)
    log_event(log, "cycle.completed", **result)
    if not dry_run:
        backfill()
        if settings.qbg_agent_enabled:
            # 复盘位于交易链路下游：先写 cycle 日志并回填事实库，再调用中转站。
            # 它失败只记录结果，绝不改变当日订单或 hard_ok。
            from qbg.agent.review import daily_review

            result["daily_review"] = daily_review(
                result.get("date"), mode=result.get("mode", "ADVISORY")
            ).as_dict()
            generate(result)  # 把复盘状态/链接补回已经生成的中文日报。
        # 邮件位于所有交易、日志、store 和复盘之后，只读取最终日报。通知模块保证
        # SMTP/HTML 异常不抛出，因此它永远不能改变订单或 hard_ok。
        from qbg.notify import notify_daily_report

        result["email_notification"] = notify_daily_report(result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="quant-biga 每日编排")
    parser.add_argument("--date", default=None)
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--retrain", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="忽略交易日和当日幂等检查")
    args = parser.parse_args(argv)
    try:
        result = run_daily(today=args.date, skip_ingest=args.skip_ingest, retrain=args.retrain,
                           dry_run=args.dry_run, force=args.force)
    except Exception as exc:
        # Windows 计划任务常吞掉控制台输出；尽力发崩溃告警后仍保留原异常和退出码。
        from qbg.notify import notify_failure

        notify_failure(exc, when=args.date, context="qbg.orchestrator.daily_cycle")
        raise
    print({key: result.get(key) for key in
           ("date", "mode", "run_kind", "skipped_reason", "market_risk_on", "hard_ok",
            "submitted", "report_path")})
    return 0 if result.get("hard_ok", True) else 2


if __name__ == "__main__":
    raise SystemExit(main())
