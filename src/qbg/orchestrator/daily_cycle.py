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
from qbg.portfolio.source import load_portfolio
from qbg.report.daily_report import generate
from qbg.risk.gates import load_limits, run_all_gates
from qbg.store.etl import backfill
from qbg.strategy.predict import (
    latest_date_scores,
    load_production_predictions,
    prediction_asof,
)
from qbg.strategy.regime import market_risk_on
from qbg.strategy.topk_weights import affordable_scores, renormalize_weights, topk_equal_weight
from qbg.utils.logging import get_logger, log_event

log = get_logger("qbg.orchestrator.daily_cycle")

# 早退的宽限窗口。
#
# 判据不是"现在离开盘还有多久"，而是**"等我们跑到判闸那一刻，市场开没开"**——
# 从启动到 run_all_gates 要走完拉数、打分、LLM 逐票复核，实测 5-8 分钟。
# 所以开盘在 5 分钟以内的就照常往下跑，等判闸时它早开了。
#
# 别把这个值调大。设成 15 会让 09:16 启动的那次跑完整条流程（含 LLM），
# 到 09:24 判闸时市场还没开 —— 钱花了，结果扔了，正是这个早退要避免的事。
#
# 被早退跳过也不会丢掉一天：早退**不写当日幂等标记**（save_marker 只在
# hard_ok 分支里调），09:30 那个日触发器照样会正常跑一遍。
SESSION_GRACE_MINUTES = 5


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def run_daily(*, today: str | None = None, skip_ingest: bool = False,
              retrain: bool = False, dry_run: bool = False, force: bool = False,
              default_equity: float = 100_000.0) -> dict:
    today = today or date.today().isoformat()
    result: dict = {"date": today, "mode": settings.qbg_mode.upper(), "started_ts": _now(),
                    "run_kind": "dry_run" if dry_run else "rebalance", "hard_ok": True,
                    "submitted": False, "market_risk_on": False, "orders": [],
                    "prediction_source": None, "prediction_asof": None,
                    "allowed_orders": [], "targets": {}, "scores": [], "gates": []}
    if not force and not calendar.is_trading_day(today):
        result["skipped_reason"] = "not_trading_day"
        return _finish(result, dry_run)
    if not force and already_completed_today(today):
        result["skipped_reason"] = "already_completed_today"
        return _finish(result, dry_run)

    # 交易时段早退。
    #
    # `session_guard` 是硬闸，但它在流程**跑到一半**才判 —— 拉数、打分、
    # LLM 逐票复核都做完了才发现"不在交易时段"，然后整天作废。那几毛钱和
    # 五到八分钟是白花的，而且每次都会发一封「⚠ 硬闸中止」的邮件。
    #
    # 2026-08-26 实测触发这条：计划任务的"登录后 3 分钟"触发器在 08:40
    # 跑了一次（盘前 50 分钟），全程跑完才被闸拦下。那天恰好 risk-off 没调
    # LLM，算是运气好。
    #
    # 用「多久以后开盘」而不是「现在开没开」：09:30:00 触发的任务可能因为
    # 几秒时钟偏差落在 09:29:5x，那时 in_session() 还是 False，
    # 按它早退会把**一整个交易日**跳过去。
    limits = load_limits()
    if not force and limits.get("require_trading_session", False):
        wait = calendar.minutes_until_session()
        if wait is None or wait > SESSION_GRACE_MINUTES:
            result["skipped_reason"] = "not_trading_session"
            log_event(log, "cycle.skipped.not_trading_session",
                      minutes_until_session=wait)
            return _finish(result, dry_run)
    if not skip_ingest:
        subprocess.run([sys.executable, str(PROJECT_ROOT / "scripts" / "01_ingest.py"),
                        "--skip-meta"], check=True)
    if retrain:
        train(live=True)

    portfolio_source, degraded = settings.qbg_portfolio_source, None
    try:
        loaded = load_portfolio()
        snapshot = loaded.snapshot
        portfolio_source = loaded.source
        if loaded.degraded:
            degraded = {"from": loaded.degraded_from, "reason": loaded.degraded_reason}
        equity, cash = snapshot.total_equity, snapshot.available_cash
        positions = [p.as_dict() for p in snapshot.positions]
        asof = snapshot.asof
    except Exception as exc:  # noqa: BLE001 —— 任何读取失败都要留下痕迹，不能只认两种
        # **别把失败原因丢掉。** 2026-08-25 实测：easytrader 读失败 → 降级到 ocr →
        # CSV 也不存在 → 落到这里。原来这里只写 source="default"，日报便只说
        # 「无持仓记录，按默认初始资金假设」—— 完全看不出「本来能读到真实持仓，
        # 是校验失败才退到这里的」。
        equity, cash, positions, asof = default_equity, default_equity, [], ""
        degraded = {"from": settings.qbg_portfolio_source,
                    "reason": f"{type(exc).__name__}: {exc}"}
        portfolio_source = "default"
        log_event(log, "portfolio.fallback_to_default", **degraded)
    result["account"] = {"total_equity": equity, "available_cash": cash}
    result["positions"] = positions
    # 来源与 asof 必须进日报：降级后用的是**过期持仓**，而那样出的清单
    # 和正常清单长得一模一样，不标出来没人会发现。
    result["portfolio"] = {"source": portfolio_source, "asof": asof, "degraded": degraded}
    if not is_rebalance_day(settings.qbg_rebalance_every_days, today,
                            cash_fraction=cash / equity if equity else 0,
                            cash_trigger=settings.qbg_rebalance_cash_trigger):
        result["skipped_reason"] = "not_rebalance_day"
        result["run_kind"] = "monitoring"
        return _finish(result, dry_run)

    current = {p["code"]: int(p["qty"]) for p in positions}
    sellable = {p["code"]: int(p["sellable_qty"]) for p in positions}
    # **优先读滚动重训的 live，回退静态的 cn_lgb。** 2026-09-09 之前这里
    # 直接读静态那份，而它的 test 段止于 2026-08-10 —— 于是日流程连着一个月
    # 每天选出完全相同的三只票。predictions 的日期同时喂给
    # prediction_freshness_guard，让这件事下次不可能再静默发生。
    raw_pred, pred_source = load_production_predictions()
    pred_asof = prediction_asof(raw_pred)
    log_event(log, "cycle.predictions.loaded", source=pred_source, asof=pred_asof)
    result["prediction_source"] = pred_source
    result["prediction_asof"] = pred_asof
    scores = latest_date_scores(raw_pred, neutralize=bool(settings.qbg_industry_neutral))
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
    filtered = affordable_scores(scores, last, equity, settings.qbg_top_k,
                                 cap=float(limits["max_position_pct"]))
    risk_on = market_risk_on(list(last), settings.qbg_market_sma,
                             band=settings.qbg_market_sma_band)
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
        # 预测日期。行情闸看不见它 —— 行情每天都在更新，陈旧的是预测。
        prediction_date=pred_asof,
        prev_close=previous, market_price=last, is_st=st, suspended=suspended,
        sellable_qty=sellable, current_qty=current, limits=limits,
        # **这个参数以前没传。** 它默认 None，而 session_guard 对 None 返回
        # False —— 于是 require_trading_session 一改成 true，这个硬闸就在
        # 任何时间都必然失败，系统永远不下单。和此前写死 AdvisoryAdapter、
        # 写死 ManualSource 是同一类问题：配置项存在，但没人读。
        session_open=calendar.in_session())
    result.update(market_risk_on=risk_on, targets=targets, hard_ok=hard_ok,
                  agent_verdicts=review_verdicts, agent_usage=review_usage,
                  orders=[o.as_dict() for o in orders], allowed_orders=[o.as_dict() for o in allowed],
                  gates=[g.__dict__ for g in gate_results])
    if hard_ok and not dry_run:
        # 顾问清单**永远写**，即使真的下了单。理由：清单是这一天「我们打算做
        # 什么」的书面记录，和「实际成交了什么」是两回事，出问题时要能对账。
        execution = AdvisoryAdapter().submit(allowed, today, gate_results)
        result["submitted"] = True
        result["order_artifacts"] = list(execution.artifacts)
        result["execution_mode"] = "ADVISORY"

        # P9c：非顾问模式再走券商。这里此前**写死了 AdvisoryAdapter**，
        # 和 P9a 之前写死 ManualSource 是同一类问题 —— 配置项存在但没人读。
        if str(settings.qbg_mode).upper() != "ADVISORY":
            refusal = broker_refusal(portfolio_source, degraded)
            if refusal:
                log_event(log, "cycle.broker.refused", reason=refusal)
                result.update({"execution_mode": str(settings.qbg_mode).upper(),
                               "broker": {"ok": False, "submitted": 0,
                                          "message": refusal, "outcomes": []}})
            else:
                result.update(_submit_to_broker(allowed, today, gate_results))

        save_marker(list(execution.artifacts), today)
        save_rebalance_date(today)
    return _finish(result, dry_run)


def broker_refusal(portfolio_source: str, degraded: dict | None) -> str | None:
    """要不要**拒绝**把订单发给券商？返回拒绝理由，`None` 表示放行。

    **读不到持仓就不许下单。** 2026-08-25 全链路实测的教训：一笔挂单冻结了
    资金 → 总资产恒等式失败 → easytrader 降级到 ocr → CSV 也不存在 →
    落到 `default_equity` 的 10 万空仓假设。系统于是照着一个**虚构账户**
    做规划，并真的把单发了出去（真实账户有 20 万和 3 只持仓）。

    顾问模式下出一份基于假设的清单无所谓 —— 人会看着执行。真下单不行：
    不知道自己现在持有什么，就可能重复买入、或者卖出根本不存在的股票。

    注意这和风控闸不同：闸是在「持仓已知」的前提下判断订单合不合规，
    这里判断的是**前提本身成不成立**，所以必须挡在闸之外、更靠前。
    """
    if portfolio_source != "default":
        return None
    return ("持仓读取失败、已落到默认假设账户 —— "
            f"拒绝下单（{(degraded or {}).get('reason', '原因未知')}）")


def _submit_to_broker(allowed, today: str, gate_results) -> dict:
    """PAPER / LIVE 模式下把订单提交给券商。

    **失败不抛给上层。** 到这一步风控已经放行、顾问清单也已经落盘，
    下单失败应该被如实记录进日报并告警，而不是让整个日流程崩掉 ——
    崩掉会连日报和邮件都没有，那才是真的什么都不知道。

    三把锁和账户守卫都在 `EasytraderAdapter` 里，这里不重复实现。
    """
    from qbg.execution.easytrader_adapter import EasytraderAdapter

    try:
        broker = EasytraderAdapter()
        outcome = broker.submit(allowed, today, gate_results)
    except Exception as exc:  # noqa: BLE001 —— 券商链路的任何异常都不该掀翻日流程
        log_event(log, "cycle.broker.failed", error=f"{type(exc).__name__}: {exc}")
        return {"execution_mode": str(settings.qbg_mode).upper(),
                "broker": {"ok": False, "submitted": 0,
                           "message": f"{type(exc).__name__}: {exc}", "outcomes": []}}
    log_event(log, "cycle.broker.done", ok=outcome.ok, submitted=outcome.submitted,
              mode=outcome.mode)
    return {"execution_mode": outcome.mode,
            "broker": {"ok": outcome.ok, "submitted": outcome.submitted,
                       "message": outcome.message, "outcomes": list(outcome.outcomes)}}


def _start_email_listener(notification: dict | None) -> None:
    """日报发完之后拉起入站命令监听器（如果开着）。

    标志位和 `quant-trading/scripts/04_execute.py::_launch_listener` 保持一致：
    只用 `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`。

    **不要加 `CREATE_BREAKAWAY_FROM_JOB`。** 计划任务的 job object 没有设
    `JOB_OBJECT_LIMIT_BREAKAWAY_OK`，带这个标志的 CreateProcess 会直接返回
    `ERROR_ACCESS_DENIED`（2026-08-31 实测 `PermissionError: [WinError 5]`，
    监听器一次都没起来）。代价是监听器留在 job 里，任务在它活着的这几小时里
    一直显示「正在运行」—— 参考实现也是这样，可以接受。

    **只有真的发出了日报才起。** 令牌是由那封日报送出去的 —— 没发信就没有新
    令牌，监听器起来也只能拿着一个作废的旧令牌空转。

    这一条是 quant-trading 没有的，因为它把监听器起在**交易那一步**
    （`04_execute.py`），非交易日根本走不到。而这里是 `_finish`，所有路径的
    汇合点，包括安静跳过 —— 2026-08-31 就是这么出的事：09:16 那次什么都没做，
    却起了 3 小时的监听器，把任务钉住，09:30 真正那次被 `IgnoreNew` 拒绝，
    当天一笔单都没下。判据用「发没发出日报」而不是「跑到哪一步」，
    因为它同时也正好是「有没有新令牌」。

    这里**吞掉所有异常**：命令通道是旁路，它起不来不能反过来影响已经完成的
    交易流程和日报 —— 和整个 notify 层是同一条纪律。
    """
    if not settings.email_commands_enabled:
        return
    if not (notification or {}).get("sent"):
        log_event(log, "listener.not_spawned",
                  reason="本次没有发出日报，也就没有新令牌，监听器起了也没用")
        return
    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    try:
        subprocess.Popen(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "email_listener.py")],
            cwd=str(PROJECT_ROOT), creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log_event(log, "listener.spawned")
    except Exception as exc:  # noqa: BLE001
        log_event(log, "listener.spawn_failed", error=f"{type(exc).__name__}: {exc}")


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
        _start_email_listener(result["email_notification"])
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
