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

import time
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

# 提交后回读校验的轮询参数。
#
# **必须轮询。** 2026-08-25 实测：一笔卖单实际「全部成交」了，但提交后立刻
# 读到的当日委托表里还没有它，于是被判成「没下出去」并中止了整批订单。
# 客户端把委托刷出来需要一点时间（要走一次服务器往返）。
#
# 这个方向的误判特别危险 —— 我们以为没下成、实际下成了。上层若据此重试，
# 就会**重复下单**。取表本身约 10 秒，3 次 × 4 秒间隔足以覆盖实测的延迟。
VERIFY_ATTEMPTS = 3
VERIFY_INTERVAL_SEC = 4.0


class LiveLockError(RuntimeError):
    """三把锁没开齐。这不是可恢复的错误，也**绝不允许由程序自动解开**。"""


@dataclass
class OrderOutcome:
    order: Order
    ok: bool
    message: str
    entrust_no: str = ""
    # 回读是否**可信**。区分三种状态，别塌缩成 ok/not ok 两种：
    #   ok=True                成功，券商记录里确实有这一笔
    #   ok=False verified=True 确实被拒（读到了别的委托，就是没有这笔）
    #   ok=False verified=False **确认不了**（读不到/读到空表）——
    #                           可能已成交，绝不能据此重试
    verified: bool = True
    dialogs: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"code": self.order.code, "side": self.order.side,
                "quantity": self.order.quantity, "price": self.order.price,
                "ok": self.ok, "message": self.message, "verified": self.verified,
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


def assert_account_matches_mode(account: str, mode: str, pattern: str) -> None:
    """声明的模式必须和客户端里**真正登录的账户**对得上。

    为什么必须查这个：`QBG_MODE=PAPER` 在 `assert_live_allowed` 里是**直接放行**
    的（模拟盘不需要三把锁）。但 PAPER 本身只是 `.env` 里的一句声明 ——
    代码无从知道同花顺客户端里登录的到底是模拟盘还是真钱账户。

    于是有一个很实在的洞：客户端登着真实券商账户 + `.env` 写着 PAPER，
    就会在真钱上下单，而且**三把锁一道都不检查**。声明和现实之间唯一的纽带
    是「你记得切了账户」—— 这正是这个项目一路在消灭的那种「没有独立预言机」
    的假设。

    客户端的资金账号标识就是那个预言机，而且我们已经会读它了。

    读不到账户名时**拒绝下单**：无法确认要在哪个账户上交易，就不该交易。
    """
    mode = mode.upper()
    if mode not in {"PAPER", "LIVE"}:
        return
    if not account:
        raise LiveLockError(
            "读不到客户端的资金账号，无法确认这是模拟盘还是真钱账户，拒绝下单。"
            "（同花顺改版可能挪了账号控件，跑 tools/probe_ths.py 看看）")
    looks_paper = pattern in account
    if mode == "PAPER" and not looks_paper:
        raise LiveLockError(
            f"QBG_MODE=PAPER 但客户端登录的是 {account!r}，不含 {pattern!r} —— "
            "这看起来是真钱账户。要在真钱上下单请改 QBG_MODE=LIVE 并开齐三把锁。")
    if mode == "LIVE" and looks_paper:
        raise LiveLockError(
            f"QBG_MODE=LIVE 但客户端登录的是 {account!r}，看起来是模拟盘。"
            "三把锁都开了却跑在模拟盘上，多半是配错了，先确认。")


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

    @property
    def mode(self) -> str:
        """报告真实的运行模式，别写死。

        日报和 store 都按这个字段区分 PAPER 与 LIVE 的历史 —— 写死会把
        模拟盘的成绩混进实盘曲线里，那正是 store schema 用 mode 做主键要防的事。
        """
        return str(settings.qbg_mode).upper()

    def __init__(self, *, exe: str | None = None, client: str | None = None,
                 max_orders: int | None = None, connect=None, reader=None,
                 account_reader=None):
        self.exe = exe or settings.qbg_ths_exe
        self.client = client or settings.qbg_ths_client
        self.max_orders = max_orders if max_orders is not None else settings.qbg_ths_max_orders
        # 注入点，离线测试用；生产环境不传。
        self._connect = connect or self._default_connect
        self._read_entrusts = reader
        self._account_reader = account_reader

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

    def _read_account(self, user) -> str:
        if self._account_reader is not None:
            return self._account_reader(user)
        from qbg.portfolio.ths_client import read_account_name

        return read_account_name(user)

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
        # 账户守卫：声明的 mode 必须和客户端里真正登录的账户对得上。
        # 放在连接之后、下单之前 —— 这是唯一能拿到「现实」的时点。
        assert_account_matches_mode(self._read_account(user), settings.qbg_mode,
                                    settings.qbg_paper_account_pattern)
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
        return ExecutionResult(ok, self.mode, submitted, (), message,
                               tuple(o.as_dict() for o in outcomes))

    def _submit_one(self, user, order: Order, known_ids: set[str]) -> OrderOutcome:
        # 局部导入：ths_client 会拉起 pywinauto/ddddocr，离线测试里那条路
        # 整个被 mock 掉，模块级导入会把测试也拖进 UI 依赖。
        from qbg.portfolio.ths_client import cleanup_dialogs

        try:
            result = place_order(user, code=order.code, side=order.side,
                                 quantity=order.quantity, price=order.price)
        except OrderFormError as exc:
            return OrderOutcome(order, False, f"填单失败：{exc}")
        if not result.ok:
            return OrderOutcome(order, False, result.message, dialogs=result.dialogs)

        # 回读校验：券商自己的委托记录才是唯一可信的成功判据。
        #
        # **必须轮询，不能读一次就下结论。** 2026-08-25 实测：一笔卖单实际
        # 「全部成交」了，但提交后立刻读到的委托表里还没有它，于是被判成
        # 「没下出去」并中止了整批订单。这个方向的误判特别危险 ——
        # 我们以为没下成、实际下成了，上层若据此重试就会**重复下单**。
        row, read_ok, last_error = None, False, None
        real_rows = 0            # 读到的**非空白**委托行数
        for attempt in range(VERIFY_ATTEMPTS):
            if attempt:
                time.sleep(VERIFY_INTERVAL_SEC)
            # **每次读之前都先清弹窗。** `today_entrusts` 要先切左侧菜单
            # 「查询[F4] → 当日委托」才能让那张表变成可见的那个 1047 网格
            # （同 control_id 的网格有三个，买卖页上那个是另一块）。
            # 模态框会挡住菜单切换，于是我们读到的还是买卖页那块 —— 空表。
            #
            # 提交后的「提示」框是**异步**弹的，place_order 里那次 cleanup
            # 可能赶在它出现之前。所以这里每一轮都扫一遍，而不是只扫一次。
            leftovers = cleanup_dialogs()
            if leftovers:
                log_event(log, "ths.order.dialogs_cleared_before_verify",
                          attempt=attempt, detail=leftovers[:3])
                time.sleep(0.5)
            try:
                entrusts = self._entrusts(user)
            except Exception as exc:  # noqa: BLE001 —— 取表本身可能瞬时失败，重试
                last_error = exc
                continue
            read_ok = True
            real_rows = max(real_rows, sum(1 for r in entrusts if not _is_blank_row(r)))
            # 只在**新出现**的合同编号里找。当日委托里可能已经躺着同参数的旧
            # 记录（含已撤单的），在全表里找会匹配到它们，然后报告成功并带回
            # 错误的编号。
            fresh = [r for r in entrusts
                     if str(r.get(ENTRUST_ID) or "").strip() not in known_ids]
            row = find_entrust(fresh, order)
            if row is not None:
                break
            # 没匹配上就把**实际看到的**记下来。不记的话，事后完全无法区分
            # 「委托表是空的」和「表里有别的单但都对不上」—— 2026-08-26 那次
            # 就是因为没有这行日志，只能靠事后手工重读客户端才发现单其实成交了。
            log_event(log, "ths.order.verify_miss", attempt=attempt, code=order.code,
                      rows=len(entrusts), real_rows=real_rows, fresh=len(fresh),
                      seen=[{"code": r.get(ENTRUST_CODE), "side": r.get(ENTRUST_SIDE),
                             "qty": r.get(ENTRUST_QTY), "price": r.get(ENTRUST_PRICE),
                             "id": r.get(ENTRUST_ID)}
                            for r in fresh if not _is_blank_row(r)][:5])
        if row is None and not read_ok:
            # 一次都没读成 —— 这和「读成了但没有这笔」是两回事，别混为一谈。
            return OrderOutcome(order, False, f"提交后读不到当日委托，无法确认：{last_error}",
                                dialogs=result.dialogs, verified=False)
        if row is None and real_rows == 0:
            # **读不到 ≠ 没下成。**
            #
            # 我们刚刚走完了委托确认框，当日委托表却一行真实记录都没有 ——
            # 这不可能是"券商拒绝了"的样子（拒绝了也该看得到别人的单，
            # 何况今天早些时候的单也会在），只可能是这次取表不可信：
            # 剪贴板没被覆盖、弹窗挡住了表格、或者读到了别的网格。
            #
            # 2026-08-26 实测：一笔卖单**全部成交**（合同 6222104175），
            # 而三次回读都读到空表，于是被报成「没下出去」并中止了整批订单。
            # 那个方向的误判最危险 —— 上层若据此重试就是**重复卖出**。
            #
            # 所以这里既不说成功也不说失败，如实说"确认不了"，
            # 并且明确叫人**不要重试**。
            log_event(log, "ths.order.unverified", code=order.code, side=order.side,
                      quantity=order.quantity, price=order.price)
            return OrderOutcome(
                order, False,
                "⚠ 无法确认：提交后当日委托表读到 0 行真实记录 —— 这是取表不可信的表现，"
                "**不是**券商拒单的证据。这笔单可能已经成交。"
                "请到同花顺「今日委托」人工核对后再决定，**不要直接重试**（会重复下单）。"
                "已停止后续订单。",
                dialogs=result.dialogs, verified=False)
        if row is None:
            # 读到了别人的委托、就是没有这一笔 —— 这才是拒单的样子。
            # 2026-08-24 实测：同花顺对「T+1 不可卖的卖单」是**静默拒绝** ——
            # 让你走完委托确认、点了「是」，然后什么都不做：不建委托、不报错、
            # 不弹任何拒绝提示（只弹了个无关的营销框）。
            return OrderOutcome(
                order, False,
                f"提交后当日委托里没有出现这一笔（表里有 {real_rows} 行其他记录，"
                "代码/方向/数量/价格四项全对的新记录一条都没有）。"
                "客户端也没有给出拒绝理由 —— 同花顺对这类拒绝是静默的（实测 T+1 不可卖时如此）。"
                "已停止后续订单。常见原因：可用股份/资金不足、超出涨跌停、非交易时段",
                dialogs=result.dialogs, verified=True)
        entrust_no = str(row.get(ENTRUST_ID) or "")
        log_event(log, "ths.order.verified", code=order.code, side=order.side,
                  quantity=order.quantity, price=order.price, entrust_no=entrust_no)
        return OrderOutcome(order, True, "已提交且回读校验通过", entrust_no, result.dialogs)
