"""P9a 同花顺持仓源的离线测试。

**不联网、不碰券商、不调 LLM。** `read_tables` 整个被替换掉，
所以这些测试在 Linux/CI 上也能跑 —— 那正是它们该有的样子：
一套需要开着同花顺才能跑的测试等于没有测试。

表头和取值都取自 2026-08-21 在同花顺 9.60.61 上的真实实测。
"""

from __future__ import annotations

import pytest

from qbg.portfolio import easytrader_source, ocr_source
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

# 基准场景：**无挂单冻结**。总资产 10 万 = 可用 4 万 + 持仓市值 6 万（`_row()` 默认值）。
# 「资金余额」实测不是现金（见 easytrader_source.BALANCE_COLUMNS 的注释），这里
# 保留一个和它无关的值，正是为了证明没有任何代码去读它。
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
    """总资产 ≠ 可用资金 + 冻结资金 + 持仓市值 → FATAL。

    这里冻结资金**已知为 0**（委托表读到了，但没有未成交买单），所以缺口
    没有任何合理解释，必须拦下。

    对比 `test_unknown_frozen_tolerates_gap_without_failing`：读不到委托表时
    同样的缺口只报警告 —— 差别在于"知不知道冻了多少"，不在缺口本身。
    """
    balance = {**BALANCE, "总资产": 100000.0, "可用金额": 1.0}
    tables = {**_tables([_row()], balance), "entrusts": []}
    with pytest.raises(PortfolioValidationError):
        EasytraderSource(reader=_reader(tables)).load()


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


# ---------------------------------------------------------------------------
# 挂单冻结资金
#
# 2026-08-25 全链路实测的故障：一笔未成交买单把现金冻住，而恒等式当时写的是
# `总资产 ≈ 可用资金 + 市值` —— 冻结的钱两边都不在，校验必然失败，
# 持仓读取整个降级到虚构的默认账户，系统照着它下了单。
#
# 下面这组数字**全部来自本机模拟账户实测**（同花顺 9.60.61），不是编的。
# ---------------------------------------------------------------------------
MEASURED = {"总资产": 199_470.46, "可用金额": 13_570.46, "资金余额": 199_217.75,
            "可取金额": 0.0}
# 挂单：300394 天孚通信 买入 200 股 @250.00，未成交 → 冻结 50,016.00（含预冻手续费）
PENDING_BUY = {"操作": "买入", "委托数量": 200, "成交数量": 0, "撤消数量": 0,
               "委托价格": 250.0, "证券代码": "300394"}


def test_frozen_cash_from_pending_buy():
    assert easytrader_source.frozen_cash([PENDING_BUY]) == 50_000.0


def test_frozen_cash_ignores_settled_and_cancelled():
    """已成交/已撤单的委托不再冻结资金。

    判据用「委托数量 − 成交数量 − 撤消数量」，不解析「备注」里的中文状态串 ——
    那个更容易随客户端版本变。
    """
    rows = [{**PENDING_BUY, "成交数量": 200},              # 全部成交
            {**PENDING_BUY, "撤消数量": 200},              # 全部撤单
            {**PENDING_BUY, "成交数量": 50, "撤消数量": 150}]  # 部成部撤
    assert easytrader_source.frozen_cash(rows) == 0.0


def test_frozen_cash_counts_partial_fill():
    assert easytrader_source.frozen_cash([{**PENDING_BUY, "成交数量": 50}]) == 150 * 250.0


def test_sell_orders_do_not_freeze_cash():
    """卖单冻结的是股份，不是钱。"""
    assert easytrader_source.frozen_cash([{**PENDING_BUY, "操作": "卖出"}]) == 0.0


def test_frozen_cash_unknown_when_entrusts_unreadable():
    """读不到委托表 → None（未知），不能当成 0。"""
    assert easytrader_source.frozen_cash(None) is None


def test_measured_account_validates_with_frozen_cash():
    """把实测的那一组数字整体跑一遍恒等式：13,570.46 + 50,016 + 135,884 = 199,470.46"""
    rows = [_row(code="002384", name="东山精密", qty=100, sellable=0, cost=190.0,
                 last=190.84, value=19_084.0),
            _row(code="300274", name="阳光电源", qty=700, sellable=0, cost=109.0,
                 last=109.59, value=76_713.0),
            _row(code="300502", name="新易盛", qty=100, sellable=0, cost=400.0,
                 last=400.87, value=40_087.0)]
    tables = {**_tables(rows, MEASURED), "entrusts": [PENDING_BUY]}
    payload = easytrader_source.to_payload(tables)
    assert payload["可用资金"] == 13_570.46
    assert payload["冻结资金"] == 50_000.0
    names = {"002384.SZ": "东山精密", "300274.SZ": "阳光电源", "300502.SZ": "新易盛"}
    _snapshot, issues = ocr_source.validate_payload(
        payload, name_map=names,
        price_history={c: (v, False) for c, v in
                       (("002384.SZ", 190.0), ("300274.SZ", 109.0), ("300502.SZ", 400.0))})
    assert not [i for i in issues if i.fatal], [i.message for i in issues]


def test_balance_field_is_never_read():
    """「资金余额」实测不是现金（199,217.75 ≈ 总资产而非现金），任何代码都不该用它。"""
    assert "资金余额" not in easytrader_source.BALANCE_COLUMNS
    payload = easytrader_source.to_payload(_tables([], MEASURED))
    assert 199_217.75 not in payload.values()


def test_unknown_frozen_tolerates_gap_without_failing():
    """同花顺委托表读失败（冻结资金=None）：缺口报成**非致命警告**，持仓照样能用。

    宁可让一份带警告的真实持仓通过，也不要退回虚构的默认账户 —— 后者更危险。
    注意这和"键不存在"（截图 OCR）不是一回事：那种情况仍然严格校验，
    见 test_ocr_payload_without_frozen_key_stays_strict。
    """
    _snapshot, issues = ocr_source.validate_payload(
        {"asof": "2026-08-25", "冻结资金": None,
         "总资产": 199_470.46, "可用资金": 13_570.46,
         "positions": [{"名称": "新易盛", "代码": "300502", "股数": 100, "可用股数": 0,
                        "成本价": 400.0, "现价": 400.87, "市值": 40_087.0, "盈亏": 87.0}]},
        name_map={"300502.SZ": "新易盛"}, price_history={"300502.SZ": (400.0, False)})
    gap = [i for i in issues if i.field == "总资产"]
    assert gap and not gap[0].fatal
    assert "说不清的资金" in gap[0].message


def test_available_plus_holdings_exceeding_total_is_fatal():
    """冻结资金不可能为负 —— 可用+市值 超过总资产就是真读错了，必须 FATAL。"""
    _snapshot, issues = ocr_source.validate_payload(
        {"asof": "2026-08-25", "冻结资金": None,
         "总资产": 50_000.0, "可用资金": 40_000.0,
         "positions": [{"名称": "新易盛", "代码": "300502", "股数": 100, "可用股数": 0,
                        "成本价": 400.0, "现价": 400.87, "市值": 40_087.0, "盈亏": 87.0}]},
        name_map={"300502.SZ": "新易盛"}, price_history={"300502.SZ": (400.0, False)})
    assert any(i.fatal and i.field == "总资产" for i in issues)


def test_known_frozen_still_catches_real_mismatch():
    """修的是漏报，不是把校验关掉：冻结额已知时对不上账仍须 FATAL。"""
    tables = {**_tables([_row()], BALANCE), "entrusts": [PENDING_BUY]}  # 声称冻了 5 万，实际没冻
    with pytest.raises(PortfolioValidationError):
        EasytraderSource(reader=_reader(tables)).load()


def test_ocr_payload_without_frozen_key_stays_strict():
    """截图 OCR 的 payload 没有「冻结资金」这个键 —— 必须走两项严格恒等式。

    P5 的对抗测试（故意删掉一行持仓）靠的就是这一条。放宽它等于把
    「OCR 漏读一整只票」这种最危险的故障变成一条警告。
    """
    _snapshot, issues = ocr_source.validate_payload(
        {"asof": "2026-08-25", "总资产": 199_470.46, "可用资金": 13_570.46,
         "positions": [{"名称": "新易盛", "代码": "300502", "股数": 100, "可用股数": 0,
                        "成本价": 400.0, "现价": 400.87, "市值": 40_087.0, "盈亏": 87.0}]},
        name_map={"300502.SZ": "新易盛"}, price_history={"300502.SZ": (400.0, False)})
    assert any(i.fatal and i.field == "总资产" for i in issues)


def test_column_check_uses_position_header_not_entrusts():
    """列头必须是**持仓表**的，不能被后读的委托表覆盖。

    2026-08-25 加读委托表时踩到：`last_columns` 记的是最近一次读到的表头，
    读完委托表后它变成 12 列，缺列检查便拿它去比 18 列的必需列，
    报出「同花顺可能改了界面」的假警报 —— 而真正的界面漂移检查因此失灵。
    """
    entrust_header = ["委托时间", "证券代码", "证券名称", "操作", "备注"]
    assert easytrader_source._check_columns(entrust_header), "委托表列头本就不该通过持仓表的检查"
    # 正常路径：列头取自持仓表，检查通过
    assert easytrader_source._check_columns(list(easytrader_source.POSITION_COLUMNS)) == []
