"""P9a 同花顺持仓源的离线测试。

**不联网、不碰券商、不调 LLM。** `read_tables` 整个被替换掉，
所以这些测试在 Linux/CI 上也能跑 —— 那正是它们该有的样子：
一套需要开着同花顺才能跑的测试等于没有测试。

表头和取值都取自 2026-08-21 在同花顺 9.60.61 上的真实实测。
"""

from __future__ import annotations

import pytest

from qbg.portfolio import source as source_mod
from qbg.portfolio.easytrader_source import (
    EasytraderSource,
    _check_columns,
    to_payload,
)
from qbg.portfolio.ocr_source import PortfolioValidationError
from qbg.portfolio.ths_client import ThsReadError

# 实测的 18 列表头
REAL_COLUMNS = ["操作", "序号", "证券代码", "证券名称", "股票余额", "可用余额", "冻结数量",
                "成本价", "市价", "盈亏", "盈亏比例(%)", "当日盈亏", "当日盈亏比(%)",
                "市值", "仓位占比(%)", "当日买入", "当日卖出", "交易市场"]

BALANCE = {"资金余额": 100000.0, "可用金额": 40000.0, "可取金额": 0.0, "总资产": 100000.0}


def _row(code="600519", name="贵州茅台", qty=100, sellable=100,
         cost=500.0, last=600.0, value=60000.0, pnl=10000.0):
    return {"操作": "", "序号": 1, "证券代码": code, "证券名称": name,
            "股票余额": qty, "可用余额": sellable, "冻结数量": 0,
            "成本价": cost, "市价": last, "盈亏": pnl, "盈亏比例(%)": 20.0,
            "当日盈亏": 0.0, "当日盈亏比(%)": 0.0, "市值": value,
            "仓位占比(%)": 60.0, "当日买入": 0, "当日卖出": 0, "交易市场": "上海A股"}


def _tables(rows=None, balance=None, columns=None):
    return {"balance": dict(balance or BALANCE), "position": list(rows or []),
            "columns": list(columns if columns is not None else REAL_COLUMNS)}


def _reader(tables):
    def _call(**_kwargs):
        return tables
    return _call


@pytest.fixture
def no_price_check(monkeypatch):
    """跳过涨跌停校验：它要读 parquet 缓存，那是 P1 的事，与本模块无关。"""
    monkeypatch.setattr("qbg.portfolio.ocr_source._latest_limit_context",
                        lambda code: (600.0, False))
    monkeypatch.setattr("qbg.data.meta.load_cached", lambda: {"600519.SH": "贵州茅台"})


# ---------------------------------------------------------------------------
# 表头 → payload 映射
# ---------------------------------------------------------------------------
def test_payload_maps_real_column_names():
    payload = to_payload(_tables([_row()]), asof="2026-08-21")
    assert payload["总资产"] == 100000.0
    # 用「可用金额」而不是「资金余额」：前者才是真正能下单的钱
    assert payload["可用资金"] == 40000.0
    assert payload["asof"] == "2026-08-21"
    position = payload["positions"][0]
    assert position == {"代码": "600519", "名称": "贵州茅台", "股数": 100,
                        "可用股数": 100, "成本价": 500.0, "现价": 600.0,
                        "市值": 60000.0, "盈亏": 10000.0}


def test_payload_uses_available_not_balance():
    """资金余额 ≠ 可用金额。拿错会把未交收资金当成能用的钱。"""
    balance = {**BALANCE, "资金余额": 99999.0, "可用金额": 40000.0}
    assert to_payload(_tables([], balance))["可用资金"] == 40000.0


def test_payload_drops_blank_placeholder_rows():
    """同花顺空仓时给的是整行空白占位行，不是持仓。"""
    blank = {**_row(code="", name="", qty=0, sellable=0, cost=0.0,
                    last=0.0, value=0.0, pnl=0.0)}
    payload = to_payload(_tables([blank, _row()]))
    assert len(payload["positions"]) == 1
    assert payload["positions"][0]["代码"] == "600519"


# ---------------------------------------------------------------------------
# 表头漂移检测
# ---------------------------------------------------------------------------
def test_check_columns_accepts_real_header():
    assert _check_columns(REAL_COLUMNS) == []


def test_check_columns_flags_renamed_column():
    """'市值' 被改名成 '最新市值' 是真实存在的版本差异，必须报出来。"""
    drifted = [c if c != "市值" else "最新市值" for c in REAL_COLUMNS]
    assert _check_columns(drifted) == ["市值"]


def test_missing_column_raises_instead_of_parsing():
    """表头对不上说明多半在读另一张表 —— 这时解析出来的持仓比没有更危险。"""
    tables = _tables([_row()], columns=[c for c in REAL_COLUMNS if c != "可用余额"])
    with pytest.raises(ThsReadError, match="可用余额"):
        EasytraderSource(reader=_reader(tables)).load()


# ---------------------------------------------------------------------------
# 复用 P5 校验
# ---------------------------------------------------------------------------
def test_load_returns_snapshot(no_price_check):
    snapshot = EasytraderSource(reader=_reader(_tables([_row()]))).load()
    assert snapshot.total_equity == 100000.0
    assert snapshot.available_cash == 40000.0
    assert len(snapshot.positions) == 1
    position = snapshot.positions[0]
    assert position.code == "600519.SH"       # 裸 6 位被规范化
    assert position.qty == 100
    assert position.sellable_qty == 100


def test_sellable_is_carried_through(no_price_check):
    """可用余额是本方案相对截图 OCR 的真正增量，T+1 闸靠它。

    场景就是当日买入 200 股：股票余额 300、可用余额 100，差额当日不可卖。
    """
    rows = [_row(qty=300, sellable=100, value=180000.0)]
    balance = {**BALANCE, "总资产": 220000.0, "可用金额": 40000.0}
    snapshot = EasytraderSource(reader=_reader(_tables(rows, balance))).load()
    assert (snapshot.positions[0].qty, snapshot.positions[0].sellable_qty) == (300, 100)


def test_market_value_mismatch_is_fatal(no_price_check):
    """市值与股数×现价对不上 —— 复用的正是 P5 那条校验。"""
    with pytest.raises(PortfolioValidationError):
        EasytraderSource(reader=_reader(_tables([_row(value=1.0)]))).load()


def test_totals_mismatch_is_fatal(no_price_check):
    """总资产 ≠ 可用资金 + 持仓市值。"""
    balance = {**BALANCE, "总资产": 100000.0, "可用金额": 1.0}
    with pytest.raises(PortfolioValidationError):
        EasytraderSource(reader=_reader(_tables([_row()], balance))).load()


def test_read_error_propagates():
    def _boom(**_kwargs):
        raise ThsReadError("剪贴板未被更新")

    with pytest.raises(ThsReadError):
        EasytraderSource(reader=_boom).load()


# ---------------------------------------------------------------------------
# 来源选择与降级
# ---------------------------------------------------------------------------
def test_factory_picks_manual_by_default(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(source_mod, "ManualSource",
                        lambda: type("S", (), {"load": lambda self: sentinel})())
    loaded = source_mod.load_portfolio("ocr")
    assert loaded.snapshot is sentinel
    assert loaded.source == "ocr"
    assert not loaded.degraded


def test_easytrader_failure_degrades_to_csv(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(source_mod, "ManualSource",
                        lambda: type("S", (), {"load": lambda self: sentinel})())
    monkeypatch.setattr(source_mod.settings, "qbg_ths_fallback_to_csv", 1)

    def _boom():
        raise ThsReadError("连接同花顺失败")

    monkeypatch.setattr(source_mod, "_build", lambda name: type(
        "S", (), {"load": lambda self: _boom()})())
    loaded = source_mod.load_portfolio("easytrader")
    assert loaded.snapshot is sentinel
    assert loaded.degraded
    assert loaded.degraded_from == "easytrader"
    assert "连接同花顺失败" in loaded.degraded_reason


def test_degradation_can_be_disabled(monkeypatch):
    """关掉降级时必须**抛出去**，不能悄悄用旧 CSV。"""
    monkeypatch.setattr(source_mod.settings, "qbg_ths_fallback_to_csv", 0)
    monkeypatch.setattr(source_mod, "_build", lambda name: type(
        "S", (), {"load": lambda self: (_ for _ in ()).throw(ThsReadError("x"))})())
    with pytest.raises(ThsReadError):
        source_mod.load_portfolio("easytrader")


def test_csv_source_failure_never_degrades(monkeypatch):
    """ocr/manual 自己失败时不该被降级逻辑吞掉 —— 没有第三条路。"""
    monkeypatch.setattr(source_mod, "_build", lambda name: type(
        "S", (), {"load": lambda self: (_ for _ in ()).throw(FileNotFoundError("no csv"))})())
    with pytest.raises(FileNotFoundError):
        source_mod.load_portfolio("ocr")


# ---------------------------------------------------------------------------
# 清仓后的残留行
#
# 2026-08-25 实测：卖光 601398 之后，持仓表**仍保留一行**，代码和名称都在，
# 但股数/可用/成本价/市值全归零，只剩市价和当日盈亏。它表示「今天持有过、
# 现在没了」，不是持仓。不滤掉会连报三条 FATAL，整个 load() 抛异常 ——
# 清仓当天持仓源直接不可用。
# ---------------------------------------------------------------------------
CLOSED_OUT = {"操作": "", "序号": 1, "证券代码": "601398", "证券名称": "工商银行",
              "股票余额": 0, "可用余额": 0, "冻结数量": 0, "成本价": 0.0,
              "市价": 7.89, "盈亏": 4.1, "盈亏比例(%)": 0.0, "当日盈亏": 0.0,
              "当日盈亏比(%)": 0.0, "市值": 0.0, "仓位占比(%)": 0.0,
              "当日买入": 0, "当日卖出": 100, "交易市场": "上海Ａ股"}


def test_closed_out_row_is_dropped():
    payload = to_payload(_tables([CLOSED_OUT]))
    assert payload["positions"] == []


def test_closed_out_row_does_not_break_load(no_price_check):
    """清仓当天必须还能正常读出「空仓」，而不是抛异常。"""
    balance = {**BALANCE, "总资产": 200004.1, "可用金额": 200004.1}
    snapshot = EasytraderSource(reader=_reader(_tables([CLOSED_OUT], balance))).load()
    assert snapshot.positions == ()
    assert snapshot.available_cash == 200004.1


def test_real_position_alongside_closed_out(no_price_check):
    """清掉的那只被滤掉，还持有的那只要留下。"""
    payload = to_payload(_tables([CLOSED_OUT, _row()]))
    assert [p["代码"] for p in payload["positions"]] == ["600519"]


def test_zero_qty_with_nonzero_value_is_not_dropped():
    """股数 0 但市值非 0 = 取表出错，该报 FATAL 让人看见，不能当清仓静静丢掉。"""
    broken = {**CLOSED_OUT, "股票余额": 0, "市值": 783.0}
    assert len(to_payload(_tables([broken]))["positions"]) == 1
