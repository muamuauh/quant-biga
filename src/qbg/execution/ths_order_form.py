"""同花顺下单表单的写入层（P9b）。

和 `qbg.portfolio.ths_client`（只读取表层）分开放，是为了让那个模块
「没有任何下单代码路径」这句承诺继续成立 —— 读和写的风险等级完全不同。

这一层只负责**把一张单子正确地填进去并提交**，不管选股、不管风控、不管三把锁，
那些是 `easytrader_adapter` 的事。

------------------------------------------------------------------------
2026-08-21 阶段一实测出来的五颗雷，本模块逐条对应
------------------------------------------------------------------------
1. `set_edit_text` 不被认账      → 一律 `type_keys`
2. `^a{DEL}` 清不掉输入框        → `{HOME}+{END}{DEL}`
3. 丢前台焦点则输入静默失败      → 每笔单前 `force_foreground`
4. `user.main` 可能是隐藏窗口    → `bind_main_window`（由调用方在 connect 后做）
5. 按 control id 认弹窗按钮会点错 → `find_dialog_button`，只认可见按钮 + 白名单
6. 买入页/卖出页有两套同 id 控件 → `_one_visible`，按可见性消歧（联调时在卖出页复现）

**五个的症状全都是「看起来没反应」或「看起来成功了」**，没有一个会给出指向
根因的报错。所以本模块的成功判据永远不是「没抛异常」，而是两道肯定性校验：

* **提交前**：截图 + OCR 回读三个输入框，任一不符立即中止，**不点提交**
* **提交后**：由调用方回读 `today_entrusts` 比对，见 `easytrader_adapter`
"""

from __future__ import annotations

import ctypes
import io
import re
import time
from dataclasses import dataclass, field

from qbg.portfolio.ths_client import (
    _ocr,
    cleanup_dialogs,
    enum_dialogs,
    find_dialog_button,
    force_foreground,
)
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

# 买入/卖出页控件（2026-08-21 实测，同花顺 9.60.61；卖出页已核对）。
#
# 两个页面**各有一套同 id 的控件**，都加载过之后按 id 直接找会 ElementAmbiguous，
# 所以一律走 `_one_visible` 按可见性消歧。
# 另外提交前用 `_assert_side` 校验提交按钮的文字，防止菜单没切过去就下单 ——
# 卖出页实测提交按钮文字确为「卖出」，标签是「卖出价格/卖出数量/可用余额」。
CODE_ID = 1032        # Edit   证券代码
NAME_ID = 1036        # Static 证券名称（客户端自动回填，用作「输入被接收」的预言机）
PRICE_ID = 1033       # Edit   委托价格
AMOUNT_ID = 1034      # Edit   委托数量
SUBMIT_ID = 1006      # Button 买入 / 卖出

MENU = {"BUY": ["买入[F1]"], "SELL": ["卖出[F2]"]}
SUBMIT_LABEL = {"BUY": "买入", "SELL": "卖出"}

# 见 ths_client 里的说明：这个 Edit 不支持 Ctrl+A，必须 Shift+End 选中再删。
CLEAR_KEYS = "{HOME}+{END}{DEL}"

CAPTCHA_IMAGE_ID = 2405
CAPTCHA_INPUT_ID = 2404
_WM_CLOSE = 0x0010

# 出现这些字样就**中止**，绝不点「是」继续。
#
# 「小数部分应为 N 位」这条尤其重要：OCR 回读看不见小数点（ddddocr 不输出标点），
# 所以小数位错误只能靠客户端这句提示兜底。easytrader 的默认处理是发 Alt+Y
# 一路点「是」—— 那等于带着一个可疑的价格继续下单。
FATAL_DIALOG_PATTERNS = (
    "小数部分应为",
    "价格超出",
    "涨跌幅",
    "可用资金不足",
    "可用股份不足",
    "非交易",
    "超出委托",
)


class OrderFormError(RuntimeError):
    """填单/提交失败。调用方必须中止后续订单，不要重试到别的单子上。"""


@dataclass
class PlaceResult:
    ok: bool
    message: str = ""
    dialogs: list[str] = field(default_factory=list)
    verified_fields: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 表单操作
# ---------------------------------------------------------------------------
def _one_visible(user, control_id: int, class_name: str):
    """按 control_id + **可见性**取唯一控件。

    买入页和卖出页各有一套同 id 的控件（1032/1033/1034/1006 都是），两页都加载过
    之后 `child_window(control_id=...)` 会 `ElementAmbiguousError` ——
    和 grid 的 6 个 `1047` 是同一个坑。2026-08-21 联调时在卖出页复现。

    只有当前页那套是可见的，所以用可见性消歧。**恰好一个才返回**，
    多于一个就抛错而不是随便挑 —— 挑错了就是往另一张单子里填数字。
    """
    found = [ctrl for ctrl in user.main.descendants(control_id=control_id,
                                                    class_name=class_name)
             if ctrl.is_visible()]
    if len(found) != 1:
        raise OrderFormError(
            f"control_id={control_id} 可见的有 {len(found)} 个，无法确定是哪一个")
    return found[0]


def _edit(user, control_id: int):
    return _one_visible(user, control_id, "Edit")


def _static(user, control_id: int) -> str:
    try:
        return _one_visible(user, control_id, "Static").window_text()
    except Exception:  # noqa: BLE001
        return ""


def _keys_for(text: str) -> str:
    """小数点用 `{VK_DECIMAL}` 直接发虚拟键，绕开中文输入法可能的全角转换。"""
    return "{VK_DECIMAL}".join(str(text).split("."))


def fill_field(user, control_id: int, text: str) -> None:
    edit = _edit(user, control_id)
    edit.set_focus()
    time.sleep(0.1)
    edit.type_keys(CLEAR_KEYS, set_foreground=False, pause=0.05)
    edit.type_keys(_keys_for(text), set_foreground=False, pause=0.08)


def ocr_digits(user, control_id: int) -> str:
    """截图输入框再 OCR，归一成数字序列。

    这是本客户端上**唯一可靠**的回读手段 —— `window_text()` 在这些框上恒为空。
    ddddocr 常把 0 认成 o、1 认成 l/i，边框还会带出杂字符，所以只留数字；
    它也**不输出标点**，因此比对的是数字序列，小数点位置由客户端的
    「小数部分应为 N 位」提示兜底（见 FATAL_DIALOG_PATTERNS）。
    """
    try:
        buf = io.BytesIO()
        _edit(user, control_id).capture_as_image().save(buf, format="PNG")
        raw = _ocr().classification(buf.getvalue()).lower()
        raw = raw.replace("o", "0").replace("l", "1").replace("i", "1")
        return "".join(ch for ch in raw if ch.isdigit())
    except Exception as exc:  # noqa: BLE001
        raise OrderFormError(f"OCR 回读失败，无法确认输入内容：{exc}") from exc


def _digits(text) -> str:
    return "".join(ch for ch in str(text) if ch.isdigit())


def _assert_side(user, side: str) -> None:
    """确认当前真的在目标页面上。

    菜单导航是模拟点击，可能静默失败。如果没切过去就填单提交，就会
    **在买入页下出一张卖单的参数** —— 这是最不能接受的一类故障。
    提交按钮的文字（买入/卖出）是最直接的证据。
    """
    try:
        label = _one_visible(user, SUBMIT_ID, "Button").window_text()
    except OrderFormError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise OrderFormError(f"读不到提交按钮，无法确认当前页面：{exc}") from exc
    want = SUBMIT_LABEL[side]
    if want not in label:
        raise OrderFormError(f"当前页面的提交按钮是 {label!r}，不是 {want!r} —— 菜单没切过去，中止")


# ---------------------------------------------------------------------------
# 弹窗
# ---------------------------------------------------------------------------
def _visible_text(kids) -> str:
    """只取 visible=True 的 Static。

    这个 `#32770` 是**通用提示框模板**，里面藏着一堆别的消息的隐藏控件
    （`非交易用户只提供查询功能`、`前往普通下单`、`不再提醒`…）。
    读全部子控件文本会把它们当成当前消息，判断全错。
    """
    return " ".join(text for _cid, cls, text, visible in kids
                    if visible and cls == "Static" and text.strip())


def _is_captcha(kids) -> bool:
    ids = {cid for cid, _c, _t, _v in kids}
    return CAPTCHA_IMAGE_ID in ids and CAPTCHA_INPUT_ID in ids


# 委托确认框：客户端在这里把**它即将提交的东西**完整列出来（2026-08-21 实测）：
#
#     <font color=0x888888>资金帐号：</font>模拟炒股-****
#     <font color=0x888888>证券代码：</font>600519(贵州茅台)
#     <font color=0x888888>买入价格：</font><font color=0x323232>1209.260</font>
#     <font color=0x888888>买入数量：</font>100
#     <font color=0x888888>预估金额：</font>120962.278
#     您是否确定以上买入委托？
#
# **这是比 OCR 预检强得多的校验**：OCR 是猜控件里有什么，这是客户端的正式声明。
# 所以点「是」之前必须核对它，对不上就点「否」。
CONFIRM_MARKERS = ("您是否确定", "委托确认")
_TAG_RE = re.compile(r"<[^>]+>")
_FIELD_RE = {
    "code": re.compile(r"证券代码[：:]\s*(\d{6})"),
    "price": re.compile(r"(?:买入|卖出|委托)价格[：:]\s*([\d.]+)"),
    "quantity": re.compile(r"(?:买入|卖出|委托)数量[：:]\s*(\d+)"),
}
CONFIRM_PRICE_TOLERANCE = 0.005


def parse_confirm(text: str) -> dict:
    """从委托确认框里抠出代码/价格/数量。抠不到的键就不在返回值里。"""
    plain = _TAG_RE.sub("", str(text))
    out: dict = {}
    for key, pattern in _FIELD_RE.items():
        match = pattern.search(plain)
        if not match:
            continue
        raw = match.group(1)
        out[key] = raw if key == "code" else float(raw)
    return out


def check_confirm(text: str, expect: dict) -> str | None:
    """核对委托确认框与我们要下的单。返回不符原因，一致则返回 None。

    价格用容差比：客户端显示的是 `1209.260`（3 位小数），我们发的是 `1209.26`。
    """
    got = parse_confirm(text)
    if not got:
        return "委托确认框里读不出代码/价格/数量，无法核对"
    problems = []
    if "code" in got and got["code"] != expect["code"]:
        problems.append(f"代码 期望 {expect['code']} 实际 {got['code']}")
    if "quantity" in got and int(got["quantity"]) != int(expect["quantity"]):
        problems.append(f"数量 期望 {expect['quantity']} 实际 {int(got['quantity'])}")
    if "price" in got and abs(got["price"] - float(expect["price"])) > CONFIRM_PRICE_TOLERANCE:
        problems.append(f"价格 期望 {expect['price']} 实际 {got['price']}")
    missing = [k for k in ("code", "price", "quantity") if k not in got]
    if missing:
        problems.append(f"确认框里缺字段 {missing}")
    return "；".join(problems) if problems else None


def handle_dialogs(user, *, expect: dict | None = None,
                   rounds: int = 6, timeout: float = 8.0) -> tuple[list[str], str | None]:
    """处理提交后弹出的对话框。返回 (弹窗文本列表, 致命原因或 None)。

    `expect` 给的是这笔单的 code/price/quantity；遇到**委托确认框**时会拿它
    和框里客户端自己列出的内容逐项核对，对不上就点「否」中止。

    分类顺序有讲究：先认反爬验证码（按控件结构），再判致命文案，
    然后核对委托确认框，最后才泛用地找确认按钮。反过来的话，
    验证码框里凑巧有个「确定」就会被点掉，委托确认框也会不经核对被点掉。
    """
    u32 = ctypes.windll.user32
    seen: list[str] = []
    for _round in range(rounds):
        dialogs = []
        deadline = time.time() + timeout
        while time.time() < deadline:
            dialogs = enum_dialogs()
            if dialogs:
                break
            time.sleep(0.3)
        if not dialogs:
            return seen, None

        hwnd, kids = dialogs[0]
        text = _visible_text(kids)

        if _is_captcha(kids):
            from qbg.portfolio.ths_client import _find_captcha_dialog, solve_captcha

            seen.append(f"验证码框：{text}")
            dialog = _find_captcha_dialog()
            if dialog is None or not solve_captcha(dialog):
                u32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
                return seen, "反爬验证码识别失败"
            time.sleep(0.6)
            continue

        seen.append(text)
        fatal = next((p for p in FATAL_DIALOG_PATTERNS if p in text), None)
        if fatal:
            # 关掉而不是点「是」。带着一个被客户端质疑的价格继续下单，
            # 比这一单不成交危险得多。
            u32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            time.sleep(0.5)
            return seen, f"客户端提示「{text}」（命中 {fatal}），已中止"

        if expect and any(marker in text for marker in CONFIRM_MARKERS):
            # 委托确认框 —— 客户端的正式声明，点「是」之前必须核对。
            mismatch = check_confirm(text, expect)
            if mismatch:
                no_id = find_dialog_button(kids, labels=("否", "取消", "no"))
                if no_id is not None:
                    u32.SendMessageW(u32.GetDlgItem(hwnd, no_id), 0x00F5, 0, 0)
                else:
                    u32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
                time.sleep(0.5)
                log_event(log, "ths.order.confirm_mismatch", detail=mismatch)
                return seen, f"委托确认框与计划不符，已拒绝：{mismatch}"
            log_event(log, "ths.order.confirm_ok", **expect)

        button_id = find_dialog_button(kids)
        if button_id is None:
            u32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
            time.sleep(0.5)
            return seen, f"认不出弹窗的确认按钮，已关闭：{text}"
        btn = u32.GetDlgItem(hwnd, button_id)
        u32.SendMessageW(btn, 0x00F5, 0, 0)      # BM_CLICK
        time.sleep(0.8)
    return seen, "弹窗处理超过轮数上限，可能陷入循环"


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def place_order(user, *, code: str, side: str, quantity: int, price: float) -> PlaceResult:
    """填一张单并提交。**不做回读校验** —— 那是调用方的事（见 adapter）。

    `code` 接受 `600519.SH` 或裸 6 位，内部只取 6 位数字。
    任何一步不确定就抛 `OrderFormError`，绝不「尽力而为」地继续。
    """
    if side not in MENU:
        raise OrderFormError(f"未知方向 {side!r}")
    if quantity <= 0:
        raise OrderFormError(f"数量必须为正，得到 {quantity}")

    six = re.sub(r"\D", "", str(code))[-6:]
    if len(six) != 6:
        raise OrderFormError(f"无法从 {code!r} 取出 6 位证券代码")
    price_text = f"{float(price):.2f}"
    amount_text = str(int(quantity))

    force_foreground(user.main.wrapper_object().handle)
    user._switch_left_menus(MENU[side])
    time.sleep(0.5)
    force_foreground(user.main.wrapper_object().handle)
    _assert_side(user, side)

    fill_field(user, CODE_ID, six)
    time.sleep(1.2)
    if not _static(user, NAME_ID).strip():
        raise OrderFormError(f"填入 {six} 后客户端没有回填证券名称 —— 输入没被接收，中止")

    fill_field(user, PRICE_ID, price_text)
    fill_field(user, AMOUNT_ID, amount_text)
    time.sleep(0.6)

    # 提交前肯定性校验。不通过就**不点提交**。
    checks = {"code": (ocr_digits(user, CODE_ID), six),
              "price": (ocr_digits(user, PRICE_ID), _digits(price_text)),
              "quantity": (ocr_digits(user, AMOUNT_ID), _digits(amount_text))}
    mismatched = {k: v for k, v in checks.items() if v[0] != v[1]}
    if mismatched:
        detail = "；".join(f"{k} 期望 {want} 实际 {got}" for k, (got, want) in mismatched.items())
        raise OrderFormError(f"提交前回读不符，未提交：{detail}")

    log_event(log, "ths.order.submit", code=six, side=side,
              quantity=quantity, price=price_text)
    _one_visible(user, SUBMIT_ID, "Button").click()

    dialogs, fatal = handle_dialogs(
        user, expect={"code": six, "price": float(price_text), "quantity": int(quantity)})
    cleanup_dialogs()
    if fatal:
        log_event(log, "ths.order.rejected", code=six, side=side, reason=fatal)
        return PlaceResult(False, fatal, dialogs,
                           {k: v[0] for k, v in checks.items()})
    return PlaceResult(True, "已提交，待回读校验", dialogs,
                       {k: v[0] for k, v in checks.items()})
