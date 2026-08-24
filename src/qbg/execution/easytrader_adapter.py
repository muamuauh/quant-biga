"""同花顺下单适配器（P9b）—— 实现 `ExecutionAdapter`。

在 `ths_order_form`（把一张单填对并提交）之上，负责整批订单的**纪律**：

* **先卖后买**，且卖出成交前不下买单
* **每笔提交后回读 `today_entrusts` 校验**，不一致立即停掉后续所有订单
* 单次运行订单数上限
* 三把锁：`QBG_MODE` + `I_CONFIRM_REAL` + `allow_live_mode`

为什么回读校验不能省
--------------------
control_id 是硬编码的，同花顺重排控件就会失效，**而且失效往往是静默的**。
2026-08-21 阶段一实测的五颗雷里，没有一颗会给出指向根因的报错 ——
症状全是「看起来没反应」或「看起来成功了」。所以这一层的成功判据
永远不是「没抛异常」，而是**券商自己的委托记录里确实有这一笔，且四个字段都对**。

为什么先卖后买
--------------
移植自 quant-trading 的 `_submit_settled`。那边加这个逻辑是因为纸面账户现金
曾被打到 −$24,322：买单按「计划中的现金」下，而卖出还没到账。
A 股 T+1 下卖出资金当日可用于买入，但**必须等卖单真的成交**，
所以这里轮询持仓而不是简单地 sleep。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from qbg.config import settings
from qbg.execution.base import ExecutionResult, Order
from qbg.execution.ths_order_form import OrderFormError, place_order
from qbg.risk.gates import GateResult, load_limits
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

# 回读时用来定位「就是这一笔」的字段，以及各自的比对方式。
ENTRUST_CODE = "证券代码"
ENTRUST_SIDE = "操作"
ENTRUST_QTY = "委托数量"
ENTRUST_PRICE = "委托价格"
ENTRUST_ID = "合同编号"
SIDE_LABEL = {"BUY": "买入", "SELL": "卖出"}

# 价格比对容差。委托价格回读是浮点数，直接 == 比会被表示误差咬到；
# A 股最小价位是 0.01，半个价位足够区分「同一笔」和「另一笔」。
PRICE_TOLERANCE = 0.005


class LiveLockError(RuntimeError):
    """三把锁没开齐。这不是可恢复的错误，也**绝不允许由程序自动解开**。"""


@dataclass
class OrderOutcome:
    order: Order
    ok: bool
    message: str
    entrust_no: str = ""
    dialogs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"code": self.order.code, "side": self.order.side,
                "quantity": self.order.quantity, "price": self.order.price,
                "ok": self.ok, "message": self.message,
                "entrust_no": self.entrust_no, "dialogs": self.dialogs}


def _is_blank_row(row: dict) -> bool:
    """同花顺的委托表里混着整行空白的占位行，必须先滤掉。

    实测撤单后 4 行里有 3 行是这种：字段全空、数字全 0。
    不滤的话回读校验会在一堆空行里找不到目标，误判成「没下成」。
    """
    return not str(row.get(ENTRUST_CODE) or "").strip()


def entrust_ids(entrusts: list[dict]) -> set[str]:
    """当日委托里已有的合同编号集合。用于「提交前快照 → 提交后取差集」。"""
    return {str(row.get(ENTRUST_ID) or "").strip()
            for row in entrusts if not _is_blank_row(row)} - {""}


def find_entrust(entrusts: list[dict], order: Order) -> dict | None:
    """在当日委托里找出与这笔订单匹配的那一条。

    四个字段全部要对上：代码、方向、数量、价格。

    ⚠️ **四个字段是必要条件，但不是充分条件。** 2026-08-21 联调时当日委托里
    出现了三行代码/方向/数量/价格完全相同的记录（同一只票反复试单），
    此函数返回第一个匹配项 —— 换个顺序就会匹配到早已撤掉的那笔，
    然后报告成功并带回错误的合同编号。

    所以**调用方必须先用 `entrust_ids()` 快照，再只在新出现的合同编号里找**，
    见 `EasytraderAdapter._submit_one`。这个函数单独用是不安全的。
    """
    want_code = "".join(ch for ch in str(order.code) if ch.isdigit())[-6:]
    want_side = SIDE_LABEL.get(order.side, order.side)
    for row in entrusts:
        if _is_blank_row(row):
            continue
        code = "".join(ch for ch in str(row.get(ENTRUST_CODE) or "") if ch.isdigit())[-6:]
        if code != want_code:
            continue
        if want_side not in str(row.get(ENTRUST_SIDE) or ""):
            continue
        try:
            if int(float(row.get(ENTRUST_QTY) or 0)) != int(order.quantity):
                continue
            if abs(float(row.get(ENTRUST_PRICE) or 0) - float(order.price)) > PRICE_TOLERANCE:
                continue
        except (TypeError, ValueError):
            continue
        return row
    return None


def assert_live_allowed(limits: dict | None = None) -> None:
    """三把锁。**任何自动化流程都不得修改它们**，这里只读、只校验。"""
    limits = load_limits() if limits is None else limits
    mode = str(settings.qbg_mode).upper()
    if mode in {"ADVISORY"}:
        raise LiveLockError("ADVISORY 模式不下单，请用 AdvisoryAdapter")
    if mode == "PAPER":
        return                      # 模拟盘不需要三把锁，risk.gates.mode_guard 同此语义
    if mode != "LIVE":
        raise LiveLockError(f"未知 QBG_MODE={mode}")
    if settings.i_confirm_real != 1 or not limits.get("allow_live_mode", False):
        raise LiveLockError("LIVE 三把锁未全部人工确认，拒绝下单")


class EasytraderAdapter:
    """把订单提交到同花顺客户端。实现 `ExecutionAdapter` 协议。"""

    mode = "LIVE"

    def __init__(self, *, exe: str | None = None, client: str | None = None,
                 max_orders: int | None = None, connect=None, reader=None):
        self.exe = exe or settings.qbg_ths_exe
        self.client = client or settings.qbg_ths_client
        self.max_orders = max_orders if max_orders is not None else settings.qbg_ths_max_orders
        # 注入点，离线测试用；生产环境不传。
        self._connect = connect or self._default_connect
        self._read_entrusts = reader

    # -- 连接 -------------------------------------------------------------
    def _default_connect(self):
        import easytrader

        from qbg.portfolio.ths_client import CaptchaAwareCopy, bind_main_window

        user = easytrader.use(self.client)
        # **必须显式覆盖取表策略。** UniversalClientTrader 的类默认是
        # `grid_strategy = grid_strategies.Xls`，而 Xls 在这个客户端上零验证码
        # 处理、必然失败（详见 ths_client 的模块注释）。不覆盖的话
        # `today_entrusts` 每次都读不到 —— 也就意味着**回读校验永远失败**，
        # 每一笔单都会被判成「可能没下出去」而中止。
        user.grid_strategy = CaptchaAwareCopy
        user.connect(self.exe)
        # connect() 用 top_window() 认主窗口，会被 0x0 的 Internet Explorer_Hidden
        # 抢走（雷四）。必须按标题重新钉死，否则控件全找不到。
        bind_main_window(user)
        # set_edit_text 在这个客户端上不被认账（雷一），全程走 type_keys。
        user.enable_type_keys_for_editor()
        return user

    def _entrusts(self, user) -> list[dict]:
        if self._read_entrusts is not None:
            return self._read_entrusts(user)
        return list(user.today_entrusts or [])

    # -- 主流程 -----------------------------------------------------------
    def submit(self, orders: list[Order], asof: date | str,
               gates: list[GateResult] | None = None) -> ExecutionResult:
        assert_live_allowed()
        stamp = str(asof)
        if not orders:
            return ExecutionResult(True, self.mode, 0, (), "没有可提交的订单")
        if len(orders) > self.max_orders:
            # 上限是为了兜住上游的逻辑错误。一次要下十几笔单，多半是
            # 选股或规划出了问题，而不是真的需要那么多笔。
            return ExecutionResult(False, self.mode, 0, (),
                                   f"订单数 {len(orders)} 超过单次上限 {self.max_orders}，拒绝提交")

        # 先卖后买：卖出释放的资金当日可用于买入，但必须等它真的落到委托里。
        ordered = ([o for o in orders if o.side == "SELL"]
                   + [o for o in orders if o.side != "SELL"])

        user = self._connect()
        # 提交前先快照已有的合同编号。回读校验只在**新出现**的编号里找 ——
        # 光比代码/方向/数量/价格会匹配到当天早些时候的同参数委托（含已撤单的）。
        try:
            known_ids = entrust_ids(self._entrusts(user))
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(False, self.mode, 0, (),
                                   f"提交前读不到当日委托，无法建立基线，拒绝下单：{exc}")

        outcomes: list[OrderOutcome] = []
        for order in ordered:
            outcome = self._submit_one(user, order, known_ids)
            outcomes.append(outcome)
            if outcome.entrust_no:
                known_ids.add(outcome.entrust_no)
            if not outcome.ok:
                # 一笔出问题就停掉后续全部。控件漂移是**系统性**故障，
                # 不是偶发 —— 继续下只会把同一个错误重复施加到更多订单上。
                log_event(log, "ths.order.halt", code=order.code,
                          reason=outcome.message, remaining=len(ordered) - len(outcomes))
                break

        submitted = sum(1 for o in outcomes if o.ok)
        ok = submitted == len(ordered)
        message = "全部提交并回读校验通过" if ok else (
            f"{submitted}/{len(ordered)} 笔成功后中止：" + next(
                (o.message for o in outcomes if not o.ok), "")
        )
        log_event(log, "ths.submit.done", asof=stamp, ok=ok, submitted=submitted,
                  total=len(ordered), outcomes=[o.as_dict() for o in outcomes])
        return ExecutionResult(ok, self.mode, submitted, (), message)

    def _submit_one(self, user, order: Order, known_ids: set[str]) -> OrderOutcome:
        try:
            result = place_order(user, code=order.code, side=order.side,
                                 quantity=order.quantity, price=order.price)
        except OrderFormError as exc:
            return OrderOutcome(order, False, f"填单失败：{exc}")
        if not result.ok:
            return OrderOutcome(order, False, result.message, dialogs=result.dialogs)

        # 回读校验：券商自己的委托记录才是唯一可信的成功判据。
        try:
            entrusts = self._entrusts(user)
        except Exception as exc:  # noqa: BLE001
            return OrderOutcome(order, False, f"提交后读不到当日委托，无法确认：{exc}",
                                dialogs=result.dialogs)
        # 只在**新出现**的合同编号里找。当日委托里可能已经躺着同参数的旧记录
        # （含已撤单的），在全表里找会匹配到它们，然后报告成功并带回错误的编号。
        fresh = [row for row in entrusts
                 if str(row.get(ENTRUST_ID) or "").strip() not in known_ids]
        row = find_entrust(fresh, order)
        if row is None:
            # 2026-08-24 实测：同花顺对「T+1 不可卖的卖单」是**静默拒绝** ——
            # 让你走完委托确认、点了「是」，然后什么都不做：不建委托、不报错、
            # 不弹任何拒绝提示（只弹了个无关的营销框）。
            # 所以这里既不能报「成功」，也没法说出拒绝理由，只能如实描述。
            return OrderOutcome(
                order, False,
                "提交后当日委托里没有出现这一笔（代码/方向/数量/价格四项全对的新记录）。"
                "客户端也没有给出拒绝理由 —— 同花顺对这类拒绝是静默的（实测 T+1 不可卖时如此）。"
                "已停止后续订单。常见原因：可用股份/资金不足、超出涨跌停、非交易时段",
                dialogs=result.dialogs)
        entrust_no = str(row.get(ENTRUST_ID) or "")
        log_event(log, "ths.order.verified", code=order.code, side=order.side,
                  quantity=order.quantity, price=order.price, entrust_no=entrust_no)
        return OrderOutcome(order, True, "已提交且回读校验通过", entrust_no, result.dialogs)
