"""短线研究：涨停/连板/炸板 与 龙虎榜，在全市场 10 年数据上的次日表现。

## 这个脚本回答什么

"打板"和"跟龙虎榜"是 A股最流行的两类短线玩法。它们能不能做？

结论先写在这里：**都不能，而且方向一致地负。** 详见 `docs/shortterm-pools.md`。

## 数据从哪来（这是这个研究能做成的前提）

本地 parquet 缓存只有沪深300 那 299 只，而龙虎榜的票 **99% 不在里面**
（实测 5 天 189 只票，只有 1 只重合）。所以这个研究必须有全市场数据。

同花顺官方 API 的批量导出提供**全市场 10 年日线**（5,562 只 × 2,428 交易日
× 1028 万行，180 MB）。脚本会把它下到 `--work` 目录，默认是系统临时目录 ——
**不落进 `data/`**，那是生产数据目录，一份 180 MB 的研究快照不该混进去。

## 三个必须知道的数据坑

1. **`date_ms` 是 UTC，必须 +8 小时。** 不偏移的话每一根 K 线都会被记到
   前一天，而且不报错 —— 校准后 688041.SH 的 2026-09-10 开 232.00 收 232.37，
   和本地 BaoStock 缓存逐位一致；不偏移则整体错开一天。

2. **dump 是不复权的（`adjusted: none`），必须自己复权。** 这是我第一版踩的坑：
   除权日只占 0.34% 的行，但那些行的日收益均值是 **−5.27%**（非除权日 +0.05%），
   而**单日跌超 25% 的 3,398 行里 94.9% 是除权日** —— 全是假暴跌。不复权的话
   等权全池十年是 −3.29%，复权后是 +1.58%。反转类因子会**优先挑中**刚除权的
   票，污染最重。复权后单日跌超 25% 的行从 3,398 降到 173。

3. **涨停池/跌停池/炸板池的 `date` 参数是静默失效的。** 传 2026-09-10、
   2025-09-10、2023-09-11 返回的内容**指纹完全相同**（都是当天的池子）。
   照文档写回测会得到彻头彻尾的假结果。所以这里的涨停/连板**全部从日线
   自己重算**（`close >= prev_close × (1+涨跌幅上限)`），这样反而拿到 10 年
   而不是 1 天。龙虎榜的 `date` 是真生效的（可回溯一年）。

## 口径

- 成交价用**次日开盘买入、再次日开盘卖出**，和项目的 qlib label 口径一致
  （`Ref($open,-2)/Ref($open,-1)-1`）。
- **次日开盘一字涨停的样本必须剔除** —— 那种票买不进去。不剔除的话连板
  策略会凭空多出一大截收益，而那部分收益在现实里拿不到。这是打板回测
  最常见的作假点：4 连板有 **52%** 次日一字封死。
- 北交所（8xx/920/430）整体排除：开户门槛 50 万 + 2 年经验。

用法：
    python scripts/28_shortterm_pools.py                 # 全部
    python scripts/28_shortterm_pools.py --skip-lhb      # 只做涨停部分（不联网拉龙虎榜）
    python scripts/28_shortterm_pools.py --work D:/tmp   # 换工作目录
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from qbg.config import settings  # noqa: E402

BASE = "https://fuyao.aicubes.cn"
from qbg.utils.console import make_output_safe  # noqa: E402

# 北交所 + 老三板：开户门槛 50 万 + 2 年经验，本项目不交易。
EXCLUDE_PREFIX = {"430", "400", "920"} | {f"8{n}" for n in range(30, 80)}
WIDE_CAP_PREFIX = {"300", "301", "688", "689"}   # 创业板 / 科创板 ±20%


def _api(path: str, **params) -> dict:
    key = (settings.fuyao_api_key or "").strip()
    if not key:
        raise SystemExit("没有 FUYAO_API_KEY —— 这个研究需要全市场数据，"
                         "见 .env.example")
    query = "&".join(f"{k}={v}" for k, v in params.items())
    request = urllib.request.Request(f"{BASE}{path}" + (f"?{query}" if query else ""),
                                     headers={"X-api-key": key})
    with urllib.request.urlopen(request, timeout=settings.qbg_net_timeout_sec) as resp:
        return json.loads(resp.read())


def _download(kind: str, dest: Path, label: str) -> None:
    if dest.exists():
        return
    url = _api(f"/api/dump/market-dumps/{kind}/download-url")["data"]["presigned_url"]
    print(f"下载{label} -> {dest}", flush=True)
    urllib.request.urlretrieve(url, dest)


def _back_adjust(df: pd.DataFrame, work: Path) -> pd.DataFrame:
    """用除权事件流做后复权。**不做的话整个研究都是错的。**

    dump 是 `adjusted: none`。除权日只占 0.34% 的行，但那些行的日收益均值是
    −5.27%，而单日跌超 25% 的行里 **94.9% 是除权日** —— 全是假暴跌。
    反转类因子会优先挑中刚除权的票，污染最重。

    持有 1 股，除权后变成 (1 + 送股 + 配股) 股，另收现金红利、付配股款。
    所以 1 股除权前的价值，用除权后价格为单位表示，是：

        ratio = [(1+送股+配股) × 收盘 + 红利 − 配股比例 × 配股价] / 收盘

    后复权 = 从除权日起，之后所有价格乘以累计 ratio。
    """
    events = work / "adj_factors.parquet"
    _download("adjustment-factors", events, "除权事件流")
    adj = pd.read_parquet(events)
    adj["date"] = (pd.to_datetime(adj["ex_date_ms"], unit="ms")
                   + pd.Timedelta(hours=8)).dt.normalize()
    adj = adj[["thscode", "date", "dividend_per_share", "per_share_bonus",
               "allotment_ratio", "allotment_price"]].fillna(0.0)

    merged = df.merge(adj, on=["thscode", "date"], how="left").fillna(
        {"dividend_per_share": 0.0, "per_share_bonus": 0.0,
         "allotment_ratio": 0.0, "allotment_price": 0.0})
    shares = 1.0 + merged.per_share_bonus + merged.allotment_ratio
    cash = merged.dividend_per_share - merged.allotment_ratio * merged.allotment_price
    ratio = np.where(merged.close_price > 0,
                     (shares * merged.close_price + cash) / merged.close_price, 1.0)
    merged["ratio"] = np.where(np.isfinite(ratio) & (ratio > 0), ratio, 1.0)
    cumulative = merged.groupby("thscode", sort=False)["ratio"].cumprod()
    for column in ("open_price", "high_price", "close_price"):
        merged[column] = merged[column] * cumulative
    return merged.drop(columns=["dividend_per_share", "per_share_bonus",
                                "allotment_ratio", "allotment_price", "ratio"])


def load_prices(work: Path) -> pd.DataFrame:
    """全市场 10 年**后复权**日线。已按北交所过滤、修正时区、算好涨停标记。"""
    dest = work / "daily_k_10y.parquet"
    _download("daily-k", dest, "全市场 10 年日线（约 180 MB，几分钟）")
    df = pd.read_parquet(dest, columns=["thscode", "date_ms", "open_price", "high_price",
                                        "close_price", "volume", "turnover"])
    # **必须 +8 小时**：date_ms 是 UTC，不偏移会让每根 K 线错记到前一天。
    df["date"] = (pd.to_datetime(df["date_ms"], unit="ms")
                  + pd.Timedelta(hours=8)).dt.normalize()
    df = df.drop(columns="date_ms").sort_values(["thscode", "date"])
    df = _back_adjust(df, work)

    prefix = df["thscode"].str.slice(0, 3)
    df = df[~prefix.isin(EXCLUDE_PREFIX)].copy()
    prefix = df["thscode"].str.slice(0, 3)
    df["cap"] = np.where(prefix.isin(WIDE_CAP_PREFIX), 0.20, 0.10)
    df["board"] = np.where(prefix.isin({"688", "689"}), "科创板",
                           np.where(prefix.isin({"300", "301"}), "创业板", "主板"))

    df = df.sort_values(["thscode", "date"])
    by_code = df.groupby("thscode", sort=False)
    df["prev_close"] = by_code["close_price"].shift(1)
    df["next_open"] = by_code["open_price"].shift(-1)
    df["nn_open"] = by_code["open_price"].shift(-2)
    # 判据用**比率**不用绝对分差：复权之后价格被缩放过，1 分钱的容差没有意义。
    # 千分之一的容差兜住浮点和四舍五入。ST 股上限更严会被容差放进来一些 ——
    # 影响方向是让样本更脏，不是更好看，所以不做 ST 过滤，结论只会偏保守。
    tol = 0.001
    df["limit_up"] = df.close_price / df.prev_close - 1.0 >= df.cap - tol
    df["broke"] = ((df.high_price / df.prev_close - 1.0 >= df.cap - tol) & (~df.limit_up))
    df["limit_down"] = df.close_price / df.prev_close - 1.0 <= -df.cap + tol
    # **买不进的样本**：次日开盘就一字涨停。不剔除它，连板策略会凭空多出
    # 一大截现实里拿不到的收益。
    df["next_open_limit"] = df.next_open / df.close_price - 1.0 >= df.cap - tol
    df["ret_oo"] = df.nn_open / df.next_open - 1.0
    streak = df.limit_up.astype(int)
    df["streak"] = streak.groupby(df.thscode, sort=False).transform(
        lambda s: s * (s.groupby((s != s.shift()).cumsum()).cumcount() + 1))
    return df


def fetch_dragon_tiger(work: Path, days: list, pause: float = 0.4) -> pd.DataFrame:
    """龙虎榜。**只有一年**（更早返回 code=1003），且没有批量导出，只能逐日拉。"""
    dest = work / "dragon_tiger.parquet"
    if dest.exists():
        return pd.read_parquet(dest)
    rows, missing = [], []
    for i, day in enumerate(days):
        payload = None
        for attempt in range(3):
            try:
                payload = _api("/api/a-share/special-data/dragon-tiger-list",
                               date=day, board_type="all")
                break
            except Exception as exc:  # noqa: BLE001 —— 限流/瞬断，退避重试
                if attempt == 2:
                    payload = {"code": -1, "message": type(exc).__name__}
                time.sleep(3 * (attempt + 1))
        if (payload or {}).get("code") != 0:
            missing.append((day, (payload or {}).get("message")))
            continue
        for item in payload["data"].get("stock_items") or []:
            item["trade_date"] = day
            rows.append(item)
        if (i + 1) % 40 == 0:
            print(f"  龙虎榜 {i + 1}/{len(days)}，累计 {len(rows):,} 行", flush=True)
        time.sleep(pause)
    frame = pd.DataFrame(rows)
    frame.to_parquet(dest, index=False)
    if missing:
        print(f"  缺 {len(missing)} 天（示例 {missing[:3]}）")
    return frame


def _line(label: str, sample: pd.Series, base: float, extra: str = "") -> None:
    if sample.empty:
        print(f"  {label:<16} 无样本")
        return
    print(f"  {label:<16} n={len(sample):>7,}  均值 {sample.mean() * 100:+6.2f}%"
          f"  超额 {(sample.mean() - base) * 100:+6.2f}%"
          f"  胜率 {(sample > 0).mean() * 100:5.1f}%{extra}")


def report_limit_up(df: pd.DataFrame) -> None:
    ok = df.ret_oo.notna() & df.next_open.gt(0)
    base = df.loc[ok, "ret_oo"].mean()
    print(f"\n样本 {ok.sum():,} 行，{df.thscode.nunique():,} 只票，"
          f"{df.date.min().date()} ~ {df.date.max().date()}")
    print(f"全样本基准（次日开 -> 再次日开）{base * 100:+.3f}%")

    print("\n== 涨停之后 ==")
    for label, mask in (
        ("首板", df.limit_up & (df.streak == 1)),
        ("2 连板", df.limit_up & (df.streak == 2)),
        ("3 连板", df.limit_up & (df.streak == 3)),
        ("4 连板及以上", df.limit_up & (df.streak >= 4)),
        ("炸板", df.broke),
        ("跌停", df.limit_down),
    ):
        hit = df[mask & ok]
        blocked = hit.next_open_limit.mean() * 100 if len(hit) else 0.0
        _line(label, hit.loc[~hit.next_open_limit, "ret_oo"], base,
              f"  次日一字买不进 {blocked:5.1f}%")

    print("\n== 涨停后持有 N 天（可买样本）==")
    by_code = df.groupby("thscode", sort=False)
    for n in (1, 2, 3, 5, 10, 20):
        ret = by_code["open_price"].shift(-(n + 1)) / df.next_open - 1.0
        hit = df.limit_up & (~df.next_open_limit) & ret.notna() & df.next_open.gt(0)
        _line(f"持有 {n} 日", ret[hit], ret[ret.notna()].mean())

    print("\n== 稳健性：分年度超额（一年翻车就不能当规则用）==")
    sig = df[df.limit_up & ~df.next_open_limit & ok]
    positives = 0
    for year, part in sig.groupby(sig.date.dt.year):
        year_base = df.loc[ok & (df.date.dt.year == year), "ret_oo"].mean()
        excess = part.ret_oo.mean() - year_base
        positives += excess > 0
        print(f"  {year}  n={len(part):>6,}  超额 {excess * 100:+6.2f}%"
              f"  胜率 {(part.ret_oo > 0).mean() * 100:5.1f}%")
    print(f"  -> 超额为正的年度：{positives}/{sig.date.dt.year.nunique()}")

    print("\n== 分板块 / 分流动性 ==")
    for board, part in sig.groupby("board"):
        board_base = df.loc[ok & (df.board == board), "ret_oo"].mean()
        _line(board, part.ret_oo, board_base)
    amt20 = df.groupby("thscode", sort=False)["turnover"].transform(
        lambda s: s.rolling(20, min_periods=10).mean())
    part = sig.assign(amt=amt20.reindex(sig.index)).dropna(subset=["amt"])
    part = part.assign(bucket=pd.qcut(part.amt, 5, labels=["Q1 最小", "Q2", "Q3", "Q4", "Q5 最大"]))
    for bucket, sub in part.groupby("bucket", observed=True):
        _line(f"成交额 {bucket}", sub.ret_oo, base)


def report_factors(df: pd.DataFrame) -> None:
    """短线因子：截面 IC 是真的，但**极端尾部符号翻转**，而 k=3 正好活在尾部。

    这一节回答"能不能拿短线因子做选股、和 qlib 加权"。答案是不能，
    原因不是"没有信号"，而是信号在**中段**为正、在**尾部**为负。
    """
    by_code = df.groupby("thscode", sort=False)
    df = df.assign(
        age=by_code.cumcount(),
        amt20=by_code["turnover"].transform(lambda s: s.rolling(20, min_periods=15).mean()),
    )
    df["rev5"] = -(df.close_price / by_code["close_price"].shift(5) - 1.0)
    df["rev20"] = -(df.close_price / by_code["close_price"].shift(20) - 1.0)
    df["ovn"] = df.open_price / df.prev_close - 1.0          # 隔夜跳空
    df["intra"] = -(df.close_price / df.open_price - 1.0)    # 日内反转（取负）
    df["amp"] = (df.high_price - df.prev_close).abs() / df.prev_close

    # **流动性门槛 5000 万**：单槽 3 万的单不冲击盘口。不设这个门槛，
    # 结论会建立在一批根本买不到那么多的小票上。
    live = df[df.ret_oo.notna() & df.next_open.gt(0) & df.age.ge(60)
              & df.amt20.gt(5e7)].copy()
    base = live.ret_oo.mean()
    print(f"\n\n可交易子样本 {len(live):,} 行（20 日均成交额 > 5000 万）"
          f"  基准 {base * 100:+.4f}%")

    print("\n== 截面分位 + IC ==")
    print(f"{'因子':<8}{'Q1':>9}{'Q5':>9}{'Q5−Q1':>9}{'IC':>9}")
    factors = ["rev5", "rev20", "ovn", "intra", "amp"]
    for name in factors:
        part = live[[name, "ret_oo", "date"]].dropna()
        part = part.assign(q=part.groupby("date")[name].transform(
            lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")))
        means = part.groupby("q")["ret_oo"].mean()
        # lambda 里用默认参数绑住 name：循环变量是后期绑定的，
        # 虽然这里 apply 立刻消费不会出错，但留着就是给下一个改动埋坑。
        ic = part.groupby("date").apply(
            lambda x, col=name: x[col].corr(x.ret_oo, method="spearman"),
            include_groups=False).mean()
        print(f"{name:<8}{means[0] * 100:+8.3f}%{means[4] * 100:+8.3f}%"
              f"{(means[4] - means[0]) * 100:+8.3f}%{ic:+9.4f}")

    print("\n== **非单调性**：分位是正的，极端尾部是负的 ==")
    print("  （本项目跑 k=3，**恰好活在尾部**，所以分位数那张表用不上）")
    print(f"{'因子':<8}{'Q5 前20%':>11}{'前 2.5%':>11}{'top-20':>10}{'top-3':>10}")
    for name in factors:
        part = live[[name, "ret_oo", "date"]].dropna()
        quintile = part.assign(q=part.groupby("date")[name].transform(
            lambda x: pd.qcut(x, 5, labels=False, duplicates="drop")))
        row = [quintile.loc[quintile.q == 4, "ret_oo"].mean()]
        cut = part.groupby("date")[name].transform(lambda x: x.quantile(0.975))
        row.append(part.loc[part[name] >= cut, "ret_oo"].mean())
        for k in (20, 3):
            idx = part.groupby("date")[name].nlargest(k).index.get_level_values(1)
            row.append(part.loc[idx, "ret_oo"].mean())
        print(f"{name:<8}" + "".join(f"{v * 100:+10.3f}%" for v in row))


def report_dragon_tiger(df: pd.DataFrame, lhb: pd.DataFrame) -> None:
    if lhb.empty:
        print("\n龙虎榜：没有数据")
        return
    lhb = lhb.assign(date=pd.to_datetime(lhb.trade_date))
    merged = lhb.merge(
        df[["thscode", "date", "ret_oo", "next_open_limit", "limit_up", "next_open"]],
        on=["thscode", "date"], how="left")
    window = df[(df.date >= lhb.date.min()) & (df.date <= lhb.date.max()) & df.ret_oo.notna()]
    base = window.ret_oo.mean()
    # merge 之后这两列是 object（有 NaN），直接取反会 TypeError。
    # 对不上价格的行按"没封死/没涨停"处理，反正下一行就被 ret_oo 的 notna 滤掉了。
    for column in ("next_open_limit", "limit_up"):
        merged[column] = merged[column].astype("boolean").fillna(False).astype(bool)
    merged = merged[merged.ret_oo.notna() & ~merged.next_open_limit]

    print(f"\n\n龙虎榜 {len(lhb):,} 行 / {lhb.date.nunique()} 天"
          f"（**只有一年** —— 接口不给更早的）")
    print(f"可用 {len(merged):,} 行；同期全市场基准 {base * 100:+.3f}%")

    for title, column in (("净买入额", "net_value"), ("净买入占比", "net_rate"),
                          ("机构席位净买", "org_net_value"),
                          ("游资席位净买", "hot_money_net_value")):
        part = merged.dropna(subset=[column])
        if part.empty:
            continue
        print(f"\n== {title} ==")
        part = part.assign(bucket=pd.qcut(part[column], 5, duplicates="drop",
                                          labels=["Q1 最低", "Q2", "Q3", "Q4", "Q5 最高"]))
        for bucket, sub in part.groupby("bucket", observed=True):
            _line(str(bucket), sub.ret_oo, base)

    print("\n== 上榜当天是否涨停 ==")
    for flag, sub in merged.groupby("limit_up"):
        _line("涨停" if flag else "未涨停", sub.ret_oo, base)
    print("\n== 净买为正 且 当天没涨停（避开涨停负漂移）==")
    _line("组合", merged[(merged.net_value > 0) & (~merged.limit_up)].ret_oo, base)


def main(argv=None) -> int:
    make_output_safe()
    parser = argparse.ArgumentParser(description="短线：涨停/连板/龙虎榜")
    parser.add_argument("--work", default=None, help="放大文件的目录（默认系统临时目录）")
    parser.add_argument("--skip-lhb", action="store_true", help="不拉龙虎榜（省 240 次请求）")
    parser.add_argument("--lhb-days", type=int, default=240)
    args = parser.parse_args(argv)

    work = Path(args.work or os.path.join(tempfile.gettempdir(), "qbg_shortterm"))
    work.mkdir(parents=True, exist_ok=True)
    df = load_prices(work)
    report_limit_up(df)
    report_factors(df)

    if not args.skip_lhb:
        days = sorted(df.date.unique())[-args.lhb_days:]
        lhb = fetch_dragon_tiger(work, [str(pd.Timestamp(d).date()) for d in days])
        report_dragon_tiger(df, lhb)

    print("\n" + "=" * 72)
    print("结论见 docs/shortterm-pools.md。一句话：两类玩法都是负的，"
          "而且成本还没算进去。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
