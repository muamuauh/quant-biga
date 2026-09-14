"""一条命令完成交易日检查、选股、风控、顾问清单、日报和 store。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import UTC, date, datetime

import pandas as pd

from qbg.config import PROJECT_ROOT, settings
from qbg.data import cache, fuyao, meta
from qbg.execution.advisory import AdvisoryAdapter
from qbg.execution.order_planner import plan_orders
from qbg.market import calendar
from qbg.model.train import train
from qbg.orchestrator.run_marker import (
    already_completed_today,
    is_rebalance_day,
    load_rebalance_date,
    save_marker,
    save_rebalance_date,
)
from qbg.portfolio.source import load_portfolio
from qbg.report.daily_report import generate
from qbg.risk import trailing
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
                    "review_source": None,
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

    # --- 移动止盈：峰值每次运行都刷新（调仓日也刷），只在非调仓日强卖 ------
    # 刷新必须在判调仓日**之前**：调仓日虽然不强卖，但峰值不刷就会过期 ——
    # 一只调仓日创了新高的票，下一个监控日会拿一个偏低的旧峰值去算回撤。
    trail_arm = float(settings.qbg_trail_arm_pct)
    trail_pct = float(settings.qbg_trail_pct)
    trailing_on = trail_arm > 0 and trail_pct > 0
    peaks: dict[str, float] = {}
    result["trailing"] = {"enabled": trailing_on, "arm_pct": trail_arm, "trail_pct": trail_pct,
                          "hits": [], "orders": [], "skipped": [], "refused": None}
    # 降级时**不刷新也不保存**：落到默认账户时持仓是空的，update_peaks 会把
    # 整个峰值库清空；那之后恢复正常，所有持仓的峰值都得从现价重新起算。
    if trailing_on and degraded is None and portfolio_source != "default":
        peaks = trailing.update_peaks(positions, trailing.load_peaks())
        if not dry_run:
            trailing.save_peaks(peaks)
        result["trailing"]["watch"] = trailing.watchlist(positions, peaks, trail_arm, trail_pct)

    due = is_rebalance_day(settings.qbg_rebalance_every_days, today,
                           cash_fraction=cash / equity if equity else 0,
                           cash_trigger=settings.qbg_rebalance_cash_trigger)
    result["rebalance"] = _rebalance_status(today, due, cash, equity)
    if not due:
        result["skipped_reason"] = "not_rebalance_day"
        result["run_kind"] = "monitoring"
        if trailing_on:
            _run_trailing(result, positions, peaks, trail_arm, trail_pct, limits, today,
                          dry_run, portfolio_source, degraded, cash, equity)
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
    last, previous, st, suspended, dates = _bars(set(scores.index) | set(current))
    filtered = affordable_scores(scores, last, equity, settings.qbg_top_k,
                                 cap=float(limits["max_position_pct"]))
    risk_on = market_risk_on(list(last), settings.qbg_market_sma,
                             band=settings.qbg_market_sma_band)
    review_verdicts, review_usage = [], {}
    review_source = None
    reviewed_scores = filtered
    if risk_on and settings.qbg_agents_enabled:
        from qbg.agents.verdict_cache import load_verdicts

        candidate_count = min(settings.qbg_agents_candidates, len(filtered))
        candidate_scores = filtered.iloc[:candidate_count]
        candidates = list(candidate_scores.index)
        candidate_weights = {code: 1.0 / candidate_count for code in candidates}

        # **优先用盘前跑好的结论。** 复核实测 58 分钟（5 只票 × 12 次 LLM 调用），
        # 而 require_trading_session 是硬闸 —— 09:30 现场复核会跑到上午盘尾甚至
        # 收盘之后，整天作废且钱已经花掉。盘前跑完、盘中只读，把「LLM 慢」和
        # 「必须盘中下单」这两个约束解耦。
        #
        # 缓存**只在候选名单逐只相同时**才用（见 verdict_cache 的说明）：
        # 名单变了还套旧结论，等于给没复核过的票安一个别人的评级。
        cached = load_verdicts(today, candidates)
        if cached is not None:
            kept, review_verdicts, review_usage = cached
            review_source = "premarket_cache"
        else:
            from qbg.agents.review import review_candidates

            kept, verdicts, review_usage = review_candidates(candidate_weights)
            review_verdicts = [verdict.as_dict() for verdict in verdicts]
            review_source = "inline"
        reviewed_scores = candidate_scores.loc[[code for code in candidates if code in kept]]
        log_event(log, "cycle.review.source", source=review_source,
                  candidates=len(candidates), kept=len(kept))
    result["review_source"] = review_source
    targets = topk_equal_weight(reviewed_scores, settings.qbg_top_k, 0.95, set(current),
                                settings.qbg_keep_rank) if risk_on else {}
    targets = renormalize_weights(targets, 0.95, float(limits["max_position_pct"]))
    reference, limit_base, slippage, reference_source = _reference_prices(
        set(targets) | set(current), last, previous, limits)
    result["reference_source"] = reference_source

    orders = plan_orders(targets, current, reference, limit_base, equity, names=names, is_st=st,
                         slippage=slippage,
                         drift_band=float(limits["rebalance_drift_band"]))
    hard_ok, allowed, gate_results = run_all_gates(
        target_weights=targets, orders=orders, current_cash=cash, total_equity=equity,
        today_pnl=0, latest_data_date=max(dates) if dates else None, asof=today,
        # 预测日期。行情闸看不见它 —— 行情每天都在更新，陈旧的是预测。
        prediction_date=pred_asof,
        prev_close=limit_base, market_price=reference, is_st=st, suspended=suspended,
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


def _bars(codes) -> tuple[dict, dict, dict, dict, list]:
    """缓存里每只票最新两根日线：昨收、前收、ST、停牌、行情日期。"""
    last, previous, st, suspended, dates = {}, {}, {}, {}, []
    for code in codes:
        frame = cache.read(code)
        if frame.empty:
            continue
        row = frame.iloc[-1]
        last[code] = float(row.close)
        previous[code] = float(frame.iloc[-2].close if len(frame) > 1 else row.close)
        st[code], suspended[code] = bool(row.is_st), bool(row.is_suspended)
        dates.append(pd.Timestamp(row.date))
    return last, previous, st, suspended, dates


def _reference_prices(codes, last: dict, previous: dict, limits: dict
                      ) -> tuple[dict, dict, float, str]:
    """下单参考价 + 涨跌停基准 + 让价 + 来源。调仓日和监控日（移动止盈）共用。

    缓存里最新一根日线是**昨天**的，拿它当 09:30 的限价参考等于忽略整个
    隔夜跳空。实测 0.2% 让价下 37.7% 的卖单会挂到市价错误一侧；2026-09-11
    就踩中（688041 昨收 232.37、卖单挂 231.91，当日区间 225.37~231.50）。

    两个字典必须**成对**换，不能只换一个：
      reference  = 算仓位和限价用的"当前价"
      limit_base = 算涨跌停区间用的"上一个收盘"
    用实时价当 reference 时，上一个收盘就是缓存里最新那根（昨收 = last）；
    退回缓存价时，上一个收盘才是再往前一根（previous）。
    只换 reference 会把涨跌停区间锚在**前天**，实测会让 1.65% 的单被夹到
    （正确锚定只有 0.99%），而且永远往收紧的方向错。

    让价按**参考价的新鲜度**取，不是按偏好取：实时价只需覆盖几秒的波动，
    昨收要覆盖一整个隔夜跳空。只要有一只票没拿到实时价，整批就按 stale 走 ——
    宁可宽不可窄（窄了挂不上）。
    """
    codes = set(codes)
    reference, limit_base = dict(last), dict(previous)
    live_prices: dict[str, float] = {}
    if fuyao.enabled():
        try:
            live_prices = fuyao.reference_prices(sorted(codes))
        except Exception as exc:  # noqa: BLE001
            # 这条链路是**可选增强**，不能让它有权让下单停摆。
            log_event(log, "cycle.reference.failed", error=f"{type(exc).__name__}: {exc}")
            live_prices = {}
    for code, price in live_prices.items():
        reference[code] = price
        limit_base[code] = last.get(code, previous.get(code, price))
    fresh = bool(codes) and codes.issubset(live_prices)
    slippage = float(limits["order_slippage_live_pct" if fresh else "order_slippage_stale_pct"])
    source = "live" if fresh else "cached_close"
    log_event(log, "cycle.reference.prices", source=source,
              wanted=len(codes), live=len(live_prices), slippage=slippage)
    return reference, limit_base, slippage, source


def _rebalance_status(today: str, due: bool, cash: float, equity: float) -> dict:
    """给日报用的调仓周期状态：上次、已过几天、下次、今天是不是。

    **下次调仓日是估算。** 现金占比超过 `QBG_REBALANCE_CASH_TRIGGER` 会提前调仓
    —— 比如移动止盈卖掉两只之后。所以日报上写的"下次"是"不出意外的话"。
    """
    every = int(settings.qbg_rebalance_every_days)
    last = load_rebalance_date()
    since = calendar.trading_days_between(last, today) if last else None
    cash_fraction = cash / equity if equity else 0.0
    trigger = float(settings.qbg_rebalance_cash_trigger)
    cash_over = trigger > 0 and cash_fraction >= trigger
    ahead = [d for d in calendar.load_cached() if d > today]
    next_day = today if due else None
    if not due and since is not None:
        remaining = every - since
        if 0 < remaining <= len(ahead):
            next_day = ahead[remaining - 1]
    # 今天调仓**成功**之后的下一次。调仓日风控闸没过的话不会写调仓日期，
    # 明天仍然到期 —— 所以这是个条件句，日报要照这个语气写。
    next_after_today = ahead[every - 1] if due and every <= len(ahead) else None
    # 调仓日是不是被现金触发的：到期了就不算"触发"，没到期却调了才算。
    by_cash = bool(due and cash_over and (since is not None and since < every))
    return {"every_days": every, "last": last, "days_since": since, "is_today": due,
            "next": next_day, "next_after_today": next_after_today,
            "cash_fraction": round(cash_fraction, 4),
            "cash_trigger": trigger, "cash_triggered": by_cash}


def _run_trailing(result: dict, positions: list[dict], peaks: dict, arm: float,
                  trail: float, limits: dict, today: str, dry_run: bool,
                  portfolio_source: str, degraded: dict | None,
                  cash: float, equity: float) -> None:
    """监控日的移动止盈：查触发 → 卖单 → 风控闸 → 下单。结果写进 `result["trailing"]`。

    **四条不许简化的地方：**

    1. **持仓降级就不强卖。** 读失败落到默认账户时持仓是空的；落到过期 CSV 时，
       可能卖一只早就不在账上的票。止盈是"卖掉我手上的"，不知道手上有什么就不卖。
    2. **照走整条风控闸。** 包括预测新鲜度这道硬闸 —— 止盈卖出其实不依赖预测，
       但硬闸"失败就全盘不交易"的语义不许为某一类订单开口子（CLAUDE.md §三）。
       盘前每天重训，正常日子不会被它拦；真被拦了日报会写出来，退出码 2 要人看。
    3. **写当日完成标记，不写调仓日。** 前者防同一天重跑重复卖（挂单会冻结可卖量，
       是第二道保险）；后者要是写了，会把调仓周期重置掉。
    4. **卖单按可卖量下。** `t1_guard` 失败时卖单照样放行，超出可卖量的部分会被
       券商 T+1 静默拒绝，所以在 `trailing.sell_orders` 里就地截断。
    """
    info = result["trailing"]
    hits = trailing.triggered(positions, peaks, arm, trail)
    info["hits"] = [hit.as_dict() for hit in hits]
    log_event(log, "cycle.trailing.checked", held=len(positions), hits=len(hits))
    if not hits:
        return
    if degraded is not None or portfolio_source == "default":
        info["refused"] = "持仓来自降级数据源 —— 不按可能过期的持仓强卖"
        log_event(log, "cycle.trailing.refused", reason=info["refused"])
        return

    codes = {hit.code for hit in hits}
    last, previous, st, suspended, dates = _bars({p["code"] for p in positions})
    reference, limit_base, slippage, source = _reference_prices(codes, last, previous, limits)
    orders, skipped = trailing.sell_orders(hits, reference, limit_base, st, slippage)
    info.update(orders=[o.as_dict() for o in orders], skipped=skipped,
                reference_source=source)
    if not orders:
        return

    raw_pred, _ = load_production_predictions()
    current = {p["code"]: int(p["qty"]) for p in positions}
    sellable = {p["code"]: int(p["sellable_qty"]) for p in positions}
    remaining = {p["code"]: float(p.get("market_value", 0.0) or 0.0) / equity
                 for p in positions if equity and p["code"] not in codes}
    hard_ok, allowed, gate_results = run_all_gates(
        target_weights=remaining, orders=orders, current_cash=cash, total_equity=equity,
        today_pnl=0, latest_data_date=max(dates) if dates else None, asof=today,
        prediction_date=prediction_asof(raw_pred),
        prev_close=limit_base, market_price=reference, is_st=st, suspended=suspended,
        sellable_qty=sellable, current_qty=current, limits=limits,
        session_open=calendar.in_session())
    info.update(hard_ok=hard_ok, allowed_orders=[o.as_dict() for o in allowed],
                gates=[g.__dict__ for g in gate_results])
    # 让日报的「订单意见 / 风控闸门 / 实际委托」几节照常显示这几笔。
    result.update(hard_ok=hard_ok, orders=info["orders"],
                  allowed_orders=info["allowed_orders"], gates=info["gates"])
    if not hard_ok or dry_run:
        return

    execution = AdvisoryAdapter().submit(allowed, today, gate_results)
    result["submitted"] = True
    result["order_artifacts"] = list(execution.artifacts)
    result["execution_mode"] = "ADVISORY"
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
    log_event(log, "cycle.trailing.submitted", orders=len(allowed))


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
