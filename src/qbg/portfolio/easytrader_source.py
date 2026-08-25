"""同花顺客户端持仓源（P9a）——**只读**。

设计要点：**不新写一套校验，而是把同花顺的表头翻译成 P5 的 OCR payload 形状，
复用 `ocr_source.validate_payload` 的那六条校验。** 数据来源换了，校验不能省 ——
UI 自动化读错数字和 OCR 读错数字是同一类故障，而且 §8.3 说得很清楚：
持仓读错是本系统里最短的资金错误传导路径。

这条链路上没有任何下单代码。下单是 P9b。
"""

from __future__ import annotations

from datetime import date

from qbg.config import settings
from qbg.portfolio import ocr_source
from qbg.portfolio.base import PortfolioSnapshot
from qbg.portfolio.ths_client import ThsReadError, read_tables
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

# 2026-08-21 实测表头（同花顺 9.60.61「查询[F4] → 资金股票」，18 列）：
#   操作 序号 证券代码 证券名称 股票余额 可用余额 冻结数量 成本价 市价 盈亏
#   盈亏比例(%) 当日盈亏 当日盈亏比(%) 市值 仓位占比(%) 当日买入 当日卖出 交易市场
#
# ⚠ 是「市值」不是「最新市值」—— 别照抄 easytrader 文档或别的券商版本的列名。
# ⚠ 「可用余额」是本方案相对截图 OCR 的**真正增量**：T+1 闸需要它，
#    而手机持仓截图里经常根本没有这一列。
POSITION_COLUMNS = {
    "证券代码": "代码",
    "证券名称": "名称",
    "股票余额": "股数",
    "可用余额": "可用股数",
    "成本价": "成本价",
    "市价": "现价",
    "市值": "市值",
    "盈亏": "盈亏",
}

# 资金表头：资金余额 / 可用金额 / 可取金额 / 总资产。
#
# **两个现金字段都要，用途不同，混用会出事**：
#
#   可用金额 = 真正能下单的钱（= 资金余额 − 挂单冻结）。可负担性过滤用它，
#              用资金余额会算出买不起的单。
#   资金余额 = 账户现金总额，**含被挂单冻结的部分**。总资产恒等式用它：
#              总资产 = 资金余额 + 持仓市值
#
# 2026-08-25 全链路实测踩到：一笔 5 万的挂单未成交，冻结的钱既不在可用金额里
# 也不在持仓市值里，于是 `总资产 ≈ 可用资金 + 市值` 这条校验必然失败 ——
# **任何未成交挂单都会让持仓读取整个失败**，然后静默降级到 CSV。
BALANCE_COLUMNS = {"总资产": "总资产", "可用金额": "可用资金", "资金余额": "现金总额"}

_REQUIRED = ("证券代码", "证券名称", "股票余额", "可用余额", "成本价", "市价", "市值")


def _as_number(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _is_closed_out(mapped: dict) -> bool:
    """这一行是不是「今天清掉的仓」的残留。

    判据要同时看**股数和市值都为 0** —— 只看股数会误伤一种真实情况：
    股数 > 0 但市值被读成 0（取表出错）。那种情况应该报 FATAL 让人看见，
    而不是被当成清仓静静丢掉。
    """
    return _as_number(mapped.get("股数")) <= 0 and _as_number(mapped.get("市值")) <= 0


def to_payload(tables: dict, *, asof: str | None = None) -> dict:
    """同花顺的表 → P5 的 OCR payload 形状，以便复用同一套校验。"""
    balance = tables.get("balance") or {}
    payload: dict = {"asof": asof or date.today().isoformat()}
    for source_key, target_key in BALANCE_COLUMNS.items():
        payload[target_key] = balance.get(source_key)

    positions = []
    for row in tables.get("position") or []:
        mapped = {target: row.get(source) for source, target in POSITION_COLUMNS.items()}
        # 同花顺在无持仓时会给出整行空白的占位行（字段全空、数字全 0），
        # 它们不是持仓，直接丢掉；留着会被校验层当成「代码缺失」报一堆假错误。
        if not str(mapped.get("代码") or "").strip() and not str(mapped.get("名称") or "").strip():
            continue
        # **当天清仓后的残留行**：代码和名称都还在，但股数/成本价/市值全归零，
        # 只剩市价和当日盈亏。2026-08-25 实测卖光 601398 之后就是这样：
        #     股票余额=0 可用余额=0 成本价=0.0 市值=0.0 市价=7.89
        # 它表示「今天持有过、现在没了」，不是持仓。不滤掉的话会连报三条
        # FATAL（股数不合理 / 成本价必须>0 / 市值与股数×现价偏差），
        # 于是整个 load() 抛异常 —— 清仓当天持仓源直接不可用。
        if _is_closed_out(mapped):
            continue
        positions.append(mapped)
    payload["positions"] = positions
    return payload


def _check_columns(columns: list[str]) -> list[str]:
    """表头缺列检查。

    control_id 是硬编码的，同花顺重排控件时**失效往往是静默的** —— 读到空表，
    或者读到另一张表。缺列是能捕捉到这种漂移的最早信号，所以单独报出来，
    不要等到校验层去猜为什么字段全空。
    """
    return [name for name in _REQUIRED if columns and name not in columns]


class EasytraderSource:
    """从同花顺客户端读持仓。实现 `PortfolioSource` 协议。"""

    def __init__(self, *, exe: str | None = None, client: str | None = None,
                 strategy: str | None = None, attempts: int | None = None,
                 reader=read_tables):
        self.exe = exe or settings.qbg_ths_exe
        self.client = client or settings.qbg_ths_client
        self.strategy = strategy or settings.qbg_ths_grid_strategy
        self.attempts = attempts if attempts is not None else settings.qbg_ths_retries
        # 注入点，离线测试用；生产环境不传。
        self._reader = reader

    def load(self) -> PortfolioSnapshot:
        tables = self._reader(exe=self.exe, client=self.client,
                              strategy=self.strategy, attempts=self.attempts)
        missing = _check_columns(tables.get("columns") or [])
        if missing:
            # 硬失败而不是继续解析：表头对不上说明我们多半在读另一张表，
            # 这时候「解析出来的持仓」比没有持仓更危险。
            raise ThsReadError(f"持仓表缺少必需列 {missing}；同花顺可能改了界面，请重跑 tools/probe_ths.py")
        snapshot, warnings = ocr_source.require_valid(to_payload(tables))
        if warnings:
            log_event(log, "ths.load.warnings",
                      issues=[f"{w.code}:{w.field}:{w.message}" for w in warnings])
        log_event(log, "ths.load.ok", positions=len(snapshot.positions),
                  total_equity=snapshot.total_equity)
        return snapshot
