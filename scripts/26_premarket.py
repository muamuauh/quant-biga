"""盘前把慢活干完：拉数 → 重训 → 打分 → 逐票复核 → 写结论缓存。

## 为什么要有这个脚本

2026-09-09 实测：TradingAgents 复核 5 只票花了 **58 分钟**（每只 12 次 LLM
调用，中转站单次响应 30 秒~2 分钟）。而 `require_trading_session` 是**硬闸**，
09:30 现场复核会跑到上午盘尾甚至收盘之后 —— **整天作废，而 LLM 的钱已经
花掉了**（当天 $0.23）。

减少候选数只是把症状往后推，而且和 `config.py` 里那条设计冲突
（"候选池太小就填不满槽位"）。真正的解法是**把慢活挪到盘前**：

    08:30  这个脚本        拉数 + 重训 + 复核     约 1 小时
    09:30  run_daily.ps1   读缓存 + 风控 + 下单   约 2 分钟

它把「LLM 慢」和「必须盘中下单」这两个约束解耦了。

## 这个脚本**永远不下单**

没有风控闸、没有 execution、不碰三把锁。它的唯一产物是
`data/reviews/<date>.json`。就算它跑飞了，日流程也只是退回盘中现场复核 ——
慢，但正确。

## 为什么不读同花顺

`affordable_scores` 需要一个总资产来判断"这只票买得起一手吗"。盘前 08:30
同花顺多半还没登录（预检 09:15 才拉起它，那 15 分钟是留给人工登录的）。

所以这里走 `load_portfolio()` 的**降级链**：读得到就用真实持仓，读不到就退到
CSV/默认值。资金量只影响可负担性过滤的边界，而**判据是候选名单是否逐只相同**
—— 名单一致就用缓存，不一致自动退回现场复核。所以这里估得粗一点是安全的：
最坏情况是白跑一次盘前复核，不会下错单。

用法：
    python scripts/26_premarket.py                 # 完整：拉数 + 重训 + 复核
    python scripts/26_premarket.py --skip-ingest   # 数据已经新的时候
    python scripts/26_premarket.py --dry-run       # 只打印候选，不调 LLM
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

from qbg.agents.verdict_cache import save_verdicts  # noqa: E402
from qbg.config import settings  # noqa: E402
from qbg.data import cache as bar_cache  # noqa: E402
from qbg.model.train import train  # noqa: E402
from qbg.portfolio.source import load_portfolio  # noqa: E402
from qbg.risk.gates import load_limits  # noqa: E402
from qbg.strategy.predict import (  # noqa: E402
    latest_date_scores,
    load_production_predictions,
    prediction_asof,
)
from qbg.strategy.regime import market_risk_on  # noqa: E402
from qbg.strategy.topk_weights import affordable_scores  # noqa: E402
from qbg.utils.logging import get_logger, log_event  # noqa: E402

log = get_logger("qbg.scripts.premarket")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="盘前：重训 + 逐票复核，写结论缓存")
    p.add_argument("--date", default=date.today().isoformat())
    p.add_argument("--skip-ingest", action="store_true")
    p.add_argument("--no-retrain", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只打印候选，不调 LLM")
    p.add_argument("--equity", type=float, default=None,
                   help="覆盖总资产（可负担性过滤用）。默认走 load_portfolio 的降级链")
    args = p.parse_args(argv)
    today = args.date

    if not args.skip_ingest:
        rc = subprocess.run([sys.executable, str(ROOT / "scripts" / "01_ingest.py"),
                             "--skip-meta"], cwd=ROOT).returncode
        if rc != 0:
            print(f"拉数失败 rc={rc}，中止。", file=sys.stderr)
            return rc
    if not args.no_retrain:
        t0 = time.time()
        train(live=True)
        print(f"滚动重训完成（{time.time() - t0:.0f}s）", flush=True)

    raw_pred, pred_source = load_production_predictions()
    pred_asof = prediction_asof(raw_pred)
    print(f"预测来源 {pred_source}   最新 {pred_asof}")

    # 资金量：读得到就用真实的，读不到走降级链。见模块说明 —— 估粗了最坏
    # 只是白跑一次盘前复核，不会下错单。
    equity = args.equity
    if equity is None:
        try:
            loaded = load_portfolio()
            equity = float(loaded.snapshot.total_equity)
            print(f"持仓来源 {loaded.source}   总资产 {equity:,.2f}")
        except Exception as exc:  # noqa: BLE001
            equity = 100_000.0
            print(f"读不到持仓（{type(exc).__name__}），可负担性过滤按 {equity:,.0f} 估")

    scores = latest_date_scores(raw_pred, neutralize=bool(settings.qbg_industry_neutral))
    last = {}
    for code in scores.index:
        frame = bar_cache.read(code)
        if not frame.empty:
            last[code] = float(frame.iloc[-1]["close"])

    limits = load_limits()
    filtered = affordable_scores(scores, last, equity, settings.qbg_top_k,
                                 cap=float(limits["max_position_pct"]))
    # 择时关掉时（QBG_MARKET_SMA=0）它恒为 True，不读缓存也不拖时间。
    risk_on = market_risk_on(list(last), settings.qbg_market_sma,
                             band=settings.qbg_market_sma_band)
    if not risk_on:
        print("risk-off —— 今天不会有持仓目标，跳过复核（省一小时和一笔 LLM 账单）")
        log_event(log, "premarket.skipped", reason="risk_off", date=today)
        return 0
    if not settings.qbg_agents_enabled:
        print("QBG_AGENTS_ENABLED=0，复核关闭，无需盘前缓存。")
        return 0

    count = min(settings.qbg_agents_candidates, len(filtered))
    candidate_scores = filtered.iloc[:count]
    candidates = list(candidate_scores.index)
    weights = {code: 1.0 / count for code in candidates}
    print(f"候选 {count} 只：{candidates}")

    if args.dry_run:
        print("\n--dry-run：不调 LLM，不写缓存。")
        return 0

    from qbg.agents.review import review_candidates

    t0 = time.time()
    kept, verdicts, usage = review_candidates(weights, trade_date=pd.Timestamp(today).date())
    elapsed = time.time() - t0

    path = save_verdicts(today, candidates, [v.as_dict() for v in verdicts], kept, usage)
    print(f"\n复核完成（{elapsed / 60:.1f} 分钟）  保留 {len(kept)}/{len(candidates)} 只")
    for v in verdicts:
        mark = "保留" if v.kept else "剔除"
        print(f"  {mark}  {v.code:<12}{v.rating:<12}{(v.error or v.rationale or '')[:40]}")
    print(f"\n缓存写入 {path}")
    print("09:30 的日流程会直接读它 —— **候选名单逐只相同**才会用，"
          "不一致会自动退回现场复核。")
    log_event(log, "premarket.done", date=today, candidates=len(candidates),
              kept=len(kept), elapsed_sec=round(elapsed, 1),
              cost_usd=usage.get("cost_usd"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
