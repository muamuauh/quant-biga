"""股票池：沪深300 成分股 + 过滤 + **带日期的快照**。

## 生存者偏差，以及为什么要存快照

AKShare（和几乎所有免费源）只给**当前**的指数成分股。用今天的沪深300 去
回测过去 5 年，等于只回测了"过去 5 年活得好、最后进了 300 的那批公司"——
收益会被系统性高估，而且高估幅度无法估计。

免费源解不了这个问题（真正的历史成分股要付费数据，比如 Tushare 高积分档
的 `index_weight`）。能做的是**从今天开始积累**：每次刷新都把当天的成分
存成一份带日期的快照，一年后就有了一年的真实历史。

所以：
  · `configs/universe_hs300.txt` 是"当前用哪批票"，给日常流水线用
  · `data/snapshots/universe/YYYY-MM-DD.json` 是历史，给将来的无偏回测用
  · **回测报告必须标注这一偏差**（P2 的事）

## 过滤

  · **ST/*ST/退** —— 涨跌幅只有 ±5%，退市风险高，不建仓
  · **次新股**（上市 < `QBG_MIN_LIST_DAYS` 天）—— 新股前几日涨跌幅规则
    特殊（创业板/科创板前 5 日不设涨跌幅），一刀切规避这整类复杂性
  · **北交所** —— 涨跌幅 ±30%、流动性差、开户门槛不同。沪深300 本来
    不含北交所，这条是防止将来换池子时忘了排除
  · **长期停牌** —— 停牌可能持续数月，留在池子里只会污染排序
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from qbg.config import settings
from qbg.data import cache, meta
from qbg.market import codes
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

_SNAPSHOT_DIR = "universe"


@dataclass
class FilterReport:
    """过滤掉了谁、为什么。进日报，也进 store。

    刻意保留每一类被剔除的代码而不只是计数：某天池子突然少了 30 只票时，
    要能立刻看出是"ST 变多了"还是"数据源少给了一批"。
    """

    kept: list[str] = field(default_factory=list)
    st: list[str] = field(default_factory=list)
    too_new: list[str] = field(default_factory=list)
    bse: list[str] = field(default_factory=list)
    suspended: list[str] = field(default_factory=list)
    no_data: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "kept": len(self.kept),
            "dropped_st": len(self.st),
            "dropped_too_new": len(self.too_new),
            "dropped_bse": len(self.bse),
            "dropped_suspended": len(self.suspended),
            "dropped_no_data": len(self.no_data),
        }


# ----------------------------------------------------------------------
# 成分股获取
# ----------------------------------------------------------------------


def fetch_index_constituents(index_code: str | None = None) -> list[str]:
    """拉指数成分股（规范代码）。

    用中证指数官方接口 `index_stock_cons_csindex` 而不是东财的：实测
    东财的 `push2*` 主机在本机不可达，中证的可用。而且中证是指数编制方
    自己的数据，比第三方转手的更权威。
    """
    index_code = index_code or settings.qbg_index_code
    try:
        import akshare as ak

        df = ak.index_stock_cons_csindex(symbol=index_code)
    except Exception as e:  # noqa: BLE001
        log_event(log, "universe.fetch.error", index=index_code, error=str(e)[:200])
        return []

    if df is None or df.empty or "成分券代码" not in df.columns:
        log_event(log, "universe.fetch.unexpected_schema", index=index_code,
                  columns=[str(c) for c in (df.columns if df is not None else [])])
        return []

    out: list[str] = []
    for raw in df["成分券代码"].astype(str):
        try:
            out.append(codes.normalize(raw))
        except codes.UnknownCodeError:
            continue
    log_event(log, "universe.fetch.ok", index=index_code, count=len(out))
    return sorted(set(out))


# ----------------------------------------------------------------------
# 快照
# ----------------------------------------------------------------------


def snapshot_path(day: str | date, root: Path | None = None) -> Path:
    root = root or settings.snapshot_dir
    return root / _SNAPSHOT_DIR / f"{day}.json"


def save_snapshot(members: list[str], day: str | date | None = None,
                  root: Path | None = None) -> Path:
    """存一份带日期的成分快照。见模块说明的生存者偏差。"""
    day = day or date.today().isoformat()
    p = snapshot_path(day, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(sorted(members), ensure_ascii=False), encoding="utf-8")
    log_event(log, "universe.snapshot.saved", day=str(day), count=len(members))
    return p


def load_snapshot(day: str | date, root: Path | None = None) -> list[str]:
    p = snapshot_path(day, root)
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        log_event(log, "universe.snapshot.read_error", path=str(p), error=str(e)[:200])
        return []


# ----------------------------------------------------------------------
# 纳入日期 —— 生存者偏差里**能修的那一半**
# ----------------------------------------------------------------------

_INCLUSION_FILE = "inclusion_dates.json"


def inclusion_dates(index_code: str = "000300", root: Path | None = None,
                    refresh: bool = False) -> dict[str, str]:
    """当前成分股各自的**纳入日期**，`{"600519.SH": "2010-01-04", ...}`。

    ## 这修的是哪一半偏差

    "股票池是当前沪深300 成分"其实混着两个不同的偏差：

      * **纳入前视** —— 2026 年才进指数的票，在 2020 年的回测里就已经在池子
        里了。而它进得去，恰恰是因为这几年涨得好。等于提前知道谁会赢。
      * **幸存者** —— 2020 年在指数里、后来被剔除的票，样本里根本没有。

    纳入日期只能修**第一个**。实测（2026-09-05）当前 300 只里有 141 只
    （47%）是 2020-01-01 之后才纳入的 —— 这一半的量级不小。
    第二个要有历史剔除名单才行，本地拿不到。

    ## 一个已知的保守之处

    接口给的是**最近一次**纳入日期。一只票若曾在 2015–2018 年进过指数、
    中间被剔、2026 年又回来，这里只会看到 2026 那次，于是 2015–2018 那段
    会被当成"还没进来"而排除掉。方向是**少算而不是多算**，偏保守 ——
    宁可漏掉真实的历史成员，也不要把未来的赢家提前放进池子。

    取不到就 fail-soft 回上次缓存（和本模块其它元数据调用一致）。
    """
    path = (root or settings.snapshot_dir) / _SNAPSHOT_DIR / _INCLUSION_FILE
    if not refresh and path.exists():
        try:
            return dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as e:  # noqa: BLE001
            log_event(log, "universe.inclusion.read_error", path=str(path),
                      error=str(e)[:200])

    try:
        import akshare as ak

        df = ak.index_stock_cons(symbol=index_code)
    except Exception as e:  # noqa: BLE001 — 网络/接口变更都不该炸掉调用方
        log_event(log, "universe.inclusion.fetch_failed", error=str(e)[:200])
        if path.exists():
            return dict(json.loads(path.read_text(encoding="utf-8")))
        return {}

    # 接口列名是中文且改过不止一次，所以按语义找列而不是按字面名。
    code_col = next((c for c in df.columns if "代码" in c), df.columns[0])
    date_col = next((c for c in df.columns if "日期" in c), df.columns[-1])
    out: dict[str, str] = {}
    for raw, day in zip(df[code_col].astype(str), df[date_col].astype(str), strict=False):
        try:
            out[codes.normalize(raw)] = day[:10]
        except codes.UnknownCodeError:
            continue

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=0, sort_keys=True),
                    encoding="utf-8")
    log_event(log, "universe.inclusion.saved", index=index_code, count=len(out))
    return out


def eligibility_mask(dates, instruments, dates_by_code: dict[str, str] | None = None):
    """`date × instrument` 的布尔表：这一天这只票**已经在指数里**了吗。

    喂给回测时用 `scores.where(mask)` —— 未纳入的票不参与选择。
    **基准也必须用同一个 mask**（`asset_ret.where(mask).mean(axis=1)`），
    否则等于拿一个带偏差的基准去比一个修过偏差的策略，差值没有意义。

    查不到纳入日期的票**放行**（视为一直在池子里）。取不到接口时整张表全
    放行，退化成修之前的行为 —— 缺数据不该让回测变成另一套口径而不吭声。
    """
    mapping = inclusion_dates() if dates_by_code is None else dates_by_code
    mask = pd.DataFrame(True, index=dates, columns=instruments)
    for code in instruments:
        day = mapping.get(code)
        if day:
            mask[code] = dates >= pd.Timestamp(day)
    return mask


def list_snapshots(root: Path | None = None) -> list[str]:
    """已积累的快照日期，升序。"""
    d = (root or settings.snapshot_dir) / _SNAPSHOT_DIR
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


# ----------------------------------------------------------------------
# 过滤
# ----------------------------------------------------------------------


def filter_members(
    members: list[str],
    *,
    asof: str | None = None,
    min_list_days: int | None = None,
    exclude_st: bool | None = None,
    exclude_bse: bool | None = None,
    parquet_root: Path | None = None,
    name_map: dict[str, str] | None = None,
) -> FilterReport:
    """按可交易性过滤成分股。

    ST 和上市天数都从**本地 parquet 缓存**判断，不再发网络请求：
      · ST：用缓存里最后一行的 `is_st`（BaoStock 给的**当日**状态）
      · 上市天数：用缓存里第一行的日期。这是个下界估计——如果历史只拉了
        5 年，一只上市 20 年的票也只能看到 5 年。所以判据是"缓存里的
        历史长度不足 min_list_days"，对老票永远为真，正是想要的语义。
    """
    asof = asof or date.today().isoformat()
    min_list_days = settings.qbg_min_list_days if min_list_days is None else min_list_days
    exclude_st = bool(settings.qbg_exclude_st) if exclude_st is None else exclude_st
    exclude_bse = bool(settings.qbg_exclude_bse) if exclude_bse is None else exclude_bse
    name_map = meta.load_cached() if name_map is None else name_map

    rep = FilterReport()
    asof_ts = pd.Timestamp(asof)

    for code in members:
        if exclude_bse and codes.is_bse(code):
            rep.bse.append(code)
            continue

        df = cache.read(code, parquet_root)
        if df.empty:
            rep.no_data.append(code)
            continue

        first = pd.Timestamp(df["date"].iloc[0])
        if (asof_ts - first).days < min_list_days:
            rep.too_new.append(code)
            continue

        last = df.iloc[-1]
        if exclude_st and (bool(last["is_st"]) or meta.is_st_name(name_map.get(code, ""))):
            rep.st.append(code)
            continue

        if bool(last["is_suspended"]):
            rep.suspended.append(code)
            continue

        rep.kept.append(code)

    log_event(log, "universe.filter", asof=asof, total=len(members), **rep.as_dict())
    return rep


# ----------------------------------------------------------------------
# 落盘 / 读取
# ----------------------------------------------------------------------


def write_universe_file(members: list[str], path: Path | None = None) -> Path:
    """写 `configs/universe_hs300.txt`（日常流水线读这个）。"""
    path = path or settings.universe_file
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"# 由 scripts/01_ingest.py 生成 —— 请勿手工编辑\n"
        f"# 指数 {settings.qbg_index_code} 成分，已过滤 ST / 次新 / 停牌 / 北交所\n"
        f"# 生成于 {date.today().isoformat()}，共 {len(members)} 只\n"
        f"# 历史快照见 data/snapshots/universe/（用于缓解生存者偏差）\n"
    )
    path.write_text(header + "\n".join(sorted(members)) + "\n", encoding="utf-8")
    return path
