"""同花顺客户端只读取表层（P9a）。

**这个模块没有任何下单代码路径。** 它只做四件只读查询：资金、持仓、当日委托、
当日成交。下单是 P9b，走 execution 层，需要三把锁。不要往这里加下单函数。

为什么不直接用 easytrader 自带的 grid 策略 —— 2026-08-21 在同花顺 9.60.61 上
实测，三个策略没有一个能用，而且原因各不相同（详见 plan.md §2.2.5）：

    Xls    不继承 Copy，**零验证码处理**。Ctrl+S 之后弹的是反爬验证码框，
           它当成保存框，把临时文件路径打进验证码输入框，文件从未生成。
    Copy   有验证码分支，但用 title_re="验证码" 找控件，而 pywinauto 的
           title_re 是 **re.match（锚定开头）**；本版文案是「先输入验证码：」，
           匹配不上 → 分支不触发 → 读到陈旧剪贴板 → **静默返回空列表**。
    WMCopy 继承 Copy，同一个缺陷。

所以本模块自己实现，四个关键点每一条都是踩出来的：

1. **按可见性消歧 grid** —— control_id=1047 在本机有 6 个 CVirtualGridCtrl，
   恰好只有当前查询页那个可见。easytrader 原生写法直接 ElementAmbiguousError。
2. **找验证码框不能用 app.windows()** —— 实测枚举不到，改裸 EnumWindows，
   并按**控件结构**认（图片 2405 + 输入框 2404），不按文案。
3. **填验证码必须 type_keys** —— set_edit_text 走 WM_SETTEXT，输入框收下了却
   不认账。而且两种方式 window_text() 回读**都是空**，不能拿回读当校验。
4. **哨兵校验** —— 取表前往剪贴板写哨兵，取完确认真被覆盖了才算成功。
   这是区分「表是空的」和「复制根本没发生」的唯一可靠判据。

本模块只在 Windows 上可用，且所有第三方依赖都是**函数内延迟导入**，
这样 Linux/CI 上 import 本模块不会失败，离线测试也能 mock 掉。
"""

from __future__ import annotations

import ctypes
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 仅供类型检查，运行时不导入 Windows 专属模块
    import ctypes.wintypes as wintypes
else:  # 运行时同样需要，但放在这里避免非 Windows 平台的静态分析噪音
    import ctypes.wintypes as wintypes

from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

TRADE_WINDOW_TITLE = "网上股票交易系统5.0"

# 交易窗口「可用」的最小尺寸。精简模式下窗口标题不变、也算可见，但整个缩成
# 一条约 158x26 的下单小条；完整窗口实测约 1118x637。取这个阈值只是为了把
# 两种状态分开，不必精确。
MIN_USABLE_WIDTH = 400
MIN_USABLE_HEIGHT = 200
GRID_CONTROL_ID = 1047

# 反爬验证码框的控件 id。**按结构认框就靠这两个**，别改成按文案匹配。
CAPTCHA_IMAGE_ID = 2405
CAPTCHA_INPUT_ID = 2404
CAPTCHA_OK_ID = 1

_SENTINEL = "__QBG_CLIPBOARD_SENTINEL__"
_WM_CLOSE = 0x0010
_OCR = None


class ThsReadError(RuntimeError):
    """取表失败。调用方据此降级到 CSV，不要吞掉。"""


# ---------------------------------------------------------------------------
# Win32 小工具
# ---------------------------------------------------------------------------
def _ocr():
    """ddddocr 实例。首次加载 ONNX 模型约 1 秒，之后复用。"""
    global _OCR
    if _OCR is None:
        import ddddocr

        _OCR = ddddocr.DdddOcr(show_ad=False)
    return _OCR


def set_clipboard(text: str) -> None:
    """写剪贴板。

    64 位下**必须显式声明 restype**：GlobalAlloc/GlobalLock 默认按 c_int 返回，
    句柄和指针会被截断成 32 位，然后 memmove 直接 access violation。
    """
    u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
    k32.GlobalAlloc.restype = wintypes.HGLOBAL
    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    u32.SetClipboardData.restype = wintypes.HANDLE
    u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not u32.OpenClipboard(None):
        raise ThsReadError("OpenClipboard 失败（剪贴板被其他进程占用）")
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(0x2002, len(data))    # GMEM_MOVEABLE|GMEM_ZEROINIT
        pointer = k32.GlobalLock(handle)
        ctypes.memmove(pointer, data, len(data))
        k32.GlobalUnlock(handle)
        u32.SetClipboardData(13, handle)               # CF_UNICODETEXT
    finally:
        u32.CloseClipboard()


def force_foreground(hwnd: int) -> bool:
    """把窗口拉到前台。

    后台进程直接调 SetForegroundWindow 会被 Windows 的前台锁**静默**拒绝，
    必须先 AttachThreadInput 把输入队列挂到当前前台线程上。
    """
    u32 = ctypes.windll.user32
    current = u32.GetForegroundWindow()
    tid_from = u32.GetWindowThreadProcessId(current, None)
    tid_to = u32.GetWindowThreadProcessId(hwnd, None)
    u32.AttachThreadInput(tid_from, tid_to, True)
    try:
        u32.ShowWindow(hwnd, 9)                        # SW_RESTORE
        u32.SetForegroundWindow(hwnd)
    finally:
        u32.AttachThreadInput(tid_from, tid_to, False)
    time.sleep(0.3)
    return u32.GetForegroundWindow() == hwnd


def _xiadan_pids() -> set[int]:
    import subprocess

    out = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq xiadan.exe", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="gbk", errors="replace", check=False,
    ).stdout
    pids: set[int] = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == "xiadan.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def _enum_dialogs(pids: set[int]) -> list[tuple[int, list[tuple[int, str, str, bool]]]]:
    """列出这些进程的可见对话框及其**全部**子控件。

    每个子控件是 `(control_id, class_name, text, visible)` 四元组。

    两条不能省的细节：

    * **必须收全部子控件，不能只收有文本的。** 验证码图片控件（2405）文本是空的，
      漏掉它 `_is_captcha` 就认不出验证码框。
    * **必须带上 visible。** 这个 `#32770` 是通用提示框模板，藏着一堆别的消息的
      隐藏控件；判断「这个框在说什么」只能看 visible=True 的 Static。
    """
    u32 = ctypes.windll.user32
    found: list[tuple[int, list[tuple[int, str, str, bool]]]] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids or not u32.IsWindowVisible(hwnd):
            return True
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "#32770":
            return True
        rect = wintypes.RECT()
        u32.GetWindowRect(hwnd, ctypes.byref(rect))
        if rect.right - rect.left < 100:      # 约 238x29 的精简模式下单条，不是弹窗
            return True
        kids: list[tuple[int, str, str, bool]] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        def _child(child, __):
            length = u32.GetWindowTextLengthW(child)
            buf = ctypes.create_unicode_buffer(length + 1)
            u32.GetWindowTextW(child, buf, length + 1)
            child_cls = ctypes.create_unicode_buffer(256)
            u32.GetClassNameW(child, child_cls, 256)
            kids.append((u32.GetDlgCtrlID(child), child_cls.value, buf.value,
                         bool(u32.IsWindowVisible(child))))
            return True

        u32.EnumChildWindows(hwnd, _child, 0)
        found.append((hwnd, kids))
        return True

    u32.EnumWindows(_cb, 0)
    return found


def enum_dialogs() -> list[tuple[int, list[tuple[int, str, str, bool]]]]:
    """列出交易客户端当前所有可见对话框：[(hwnd, [(id, class, text, visible), ...]), ...]。

    用裸 EnumWindows 而不是 pywinauto 的 `app.windows()` —— 后者在这个客户端上
    枚举不到部分对话框（反爬验证码框实测就看不见）。
    """
    pids = _xiadan_pids()
    return [] if not pids else _enum_dialogs(pids)


def cleanup_dialogs() -> list[str]:
    """关掉客户端上遗留的模态框，返回被关掉的描述。

    模态框留着会卡住后面所有查询，而且会一直挂在客户端上等人去点 ——
    自动化的副作用不该让人回头手动收拾。

    **关的方式一律 WM_CLOSE，绝不点按钮。** 同花顺的营销提示框上挂着
    「立即重启」「前往普通下单」「点击此处前去开通~」，语义都不安全，
    而且 id 分配还和验证码框相反（验证码框 id=2 是『取消』，营销框 id=2 是
    『确定』）。认不出来的框就别猜它的按钮 —— 这是在券商客户端上操作，
    猜错一次的代价和省下的几行代码不成比例。
    """
    u32 = ctypes.windll.user32
    pids = _xiadan_pids()
    if not pids:
        return []
    closed = []
    for hwnd, kids in _enum_dialogs(pids):
        labels = [text for _cid, _cls, text, _vis in kids if text.strip()]
        kind = "验证码框" if any("验证码" in t for t in labels) else "其他弹窗"
        u32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        closed.append(f"{kind} {labels[:3]}")
    return closed


ACCOUNT_COMBO_ID = 2322


def read_account_name(user) -> str:
    """读客户端上当前登录的资金账号标识（如 `模拟炒股-****`）。

    读不到返回空串 —— 调用方必须把「读不到」当作**不能确认**来处理，
    而不是当作通过。判断在哪个账户上下单这件事，没有第二个信源。
    """
    try:
        combo = user.main.child_window(control_id=ACCOUNT_COMBO_ID, class_name="ComboBox")
        return str(combo.window_text() or "").strip()
    except Exception:  # noqa: BLE001 —— 控件不在/改版都归为「读不到」
        return ""


def find_trade_window() -> int | None:
    """按标题找交易主窗口的 hwnd（含被隐藏的）。"""
    u32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        length = u32.GetWindowTextLengthW(hwnd)
        if length:
            buf = ctypes.create_unicode_buffer(length + 1)
            u32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value == TRADE_WINDOW_TITLE:
                found.append(hwnd)
        return True

    u32.EnumWindows(_cb, 0)
    return found[0] if found else None


def bind_main_window(user) -> int:
    """把 easytrader 的 `_main` 钉到**真正的**交易窗口上。

    **必须做。** easytrader 的 `connect()` 最后一行是：

        self._main = self._app.top_window()

    `top_window()` 返回的是 Z 序最上面那个窗口 —— 而这个客户端里有一个
    **0x0 大小、却算「可见」的 `Internet Explorer_Hidden` 窗口**会抢到它。
    一旦抢到，后续所有 `child_window(...)` 都在错误的父窗口下查找，
    报 `ElementNotFoundError`，而错误信息里只写「找不到 control_id=xxxx」，
    完全看不出根因是主窗口认错了。

    这个故障是**间歇性**的（取决于 connect 那一刻的 Z 序），所以更该钉死 ——
    偶尔失败比一直失败难查得多。2026-08-21 实测复现。
    """
    hwnd = find_trade_window()
    if hwnd is None:
        raise ThsReadError(f"找不到「{TRADE_WINDOW_TITLE}」窗口，客户端是否已登录？")

    # 光「存在」不够，还得「能用」。精简模式下窗口标题不变、也算可见，
    # 但整个缩成一条约 158x26 的下单小条，里面没有可操作的控件树 ——
    # 不拦的话后面每一个 child_window 都会 ElementNotFound，
    # 而报错只会说「找不到 control_id=xxxx」，看不出根因。
    u32 = ctypes.windll.user32
    rect = wintypes.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    if not u32.IsWindowVisible(hwnd) or width < MIN_USABLE_WIDTH or height < MIN_USABLE_HEIGHT:
        raise ThsReadError(
            f"交易窗口不可用（{width}x{height}，visible={bool(u32.IsWindowVisible(hwnd))}）—— "
            "多半处于**精简模式**或被隐藏。请在客户端上切回「专业」模式并还原窗口。")

    user._main = user.app.window(handle=hwnd)
    return hwnd


# 弹窗按钮白名单/黑名单。
#
# **绝不能按 control id 认按钮** —— id 的语义在不同弹窗里是相反的，
# 2026-08-21 实测：
#
#     反爬验证码框：  id=1 '确定'      id=2 '取消'
#     下单提示框：    id=1 '立即重启'  id=2 '确定'      ← 正好反过来
#
# 探测脚本里就因为「先试 id=6，再试 id=1」而点到了「立即重启」，
# 只因它恰好 visible=False 才没出事。
#
# 也不能只按文本模糊匹配 —— 撤单确认框里有个 `改单`，语义是
# 「撤单并以新的价格委托」，误点会直接下出一张**新单**。
#
# 所以：只看 visible=True 的按钮，文本去掉 & 助记符后走**白名单**，
# 并把已知的危险按钮显式拉黑，认不出就一律 WM_CLOSE。
CONFIRM_LABELS = ("确定", "是", "yes", "ok")
DANGEROUS_LABELS = ("改单", "立即重启", "前往普通下单", "前去开通", "不再提示", "重下")


def _clean_label(text: str) -> str:
    """去掉 `&` 助记符和尾部的 `(Y)` 之类，留下可比对的文本。

    实测按钮文本是 `是(&Y)` / `否(&N)`，按 `"是"` 精确匹配会落空 ——
    第一轮探测就栽在这里，导致弹窗被 WM_CLOSE 当成取消，订单没提交。
    """
    import re

    return re.sub(r"[（(][^）)]*[）)]|&|\s", "", str(text)).strip()


def find_dialog_button(kids, labels=CONFIRM_LABELS) -> int | None:
    """在子控件列表里找一个**可见且安全**的按钮，返回它的 control_id。

    `kids` 形如 [(control_id, class_name, text, visible), ...]。
    """
    for control_id, class_name, text, visible in kids:
        if not visible or class_name != "Button":
            continue
        clean = _clean_label(text)
        if not clean or any(bad in clean for bad in DANGEROUS_LABELS):
            continue
        if any(clean.lower() == want or clean == want for want in labels):
            return control_id
    return None


def _find_captcha_dialog():
    """找反爬验证码框，返回 pywinauto 包装好的窗口（没有则 None）。

    **不能用 pywinauto 的 app.windows() 找** —— 实测它枚举不到这个框（返回的
    列表里只有主窗口），于是「等验证码」等多久都是白等。裸 EnumWindows 一直
    稳定看得见它。按控件结构认（图片 2405 + 输入框 2404），不按文案 ——
    文案随版本变，easytrader 就是栽在这上面。
    """
    from pywinauto.controls.hwndwrapper import HwndWrapper
    from pywinauto.win32_element_info import HwndElementInfo

    u32 = ctypes.windll.user32
    pids = _xiadan_pids()
    found: list[int] = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        pid = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in pids or not u32.IsWindowVisible(hwnd):
            return True
        if u32.GetDlgItem(hwnd, CAPTCHA_IMAGE_ID) and u32.GetDlgItem(hwnd, CAPTCHA_INPUT_ID):
            found.append(hwnd)
        return True

    u32.EnumWindows(_cb, 0)
    return HwndWrapper(HwndElementInfo(found[0])) if found else None


def _dlg_item(dialog, control_id: int):
    """取对话框子控件。用 GetDlgItem 而不是 child_window —— 后者在这个客户端上
    经常 ElementAmbiguousError，而我们已经知道确切的 control_id。"""
    from pywinauto.controls.hwndwrapper import HwndWrapper
    from pywinauto.win32_element_info import HwndElementInfo

    handle = ctypes.windll.user32.GetDlgItem(dialog.handle, control_id)
    return HwndWrapper(HwndElementInfo(handle)) if handle else None


def solve_captcha(dialog, attempts: int = 5) -> bool:
    """认验证码并提交。成功（框消失）返回 True。"""
    import io

    u32 = ctypes.windll.user32
    for _ in range(attempts):
        image_ctl = _dlg_item(dialog, CAPTCHA_IMAGE_ID)
        edit = _dlg_item(dialog, CAPTCHA_INPUT_ID)
        ok_btn = _dlg_item(dialog, CAPTCHA_OK_ID)
        if not (image_ctl and edit and ok_btn):
            return False
        try:
            buf = io.BytesIO()
            image_ctl.capture_as_image().save(buf, format="PNG")
            code = "".join(ch for ch in _ocr().classification(buf.getvalue()) if ch.isdigit())
        except Exception:  # noqa: BLE001 —— 截图/识别失败就当这一轮没认出来
            code = ""
        if len(code) == 4:
            # 必须 type_keys，不能 set_edit_text：后者走 WM_SETTEXT，输入框收下了
            # 却不认账，点确定弹窗不关。两种方式 window_text() 回读都是空，
            # 所以**唯一可靠的成功判据是弹窗消失**。
            edit.set_focus()
            time.sleep(0.15)
            edit.type_keys(code, set_foreground=False, pause=0.08)
            time.sleep(0.15)
            ok_btn.click()
            time.sleep(0.6)
            if not u32.IsWindow(dialog.handle) or not u32.IsWindowVisible(dialog.handle):
                return True
        else:
            try:
                image_ctl.click()          # 认不出就点一下图换一张
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.4)
    return False


# ---------------------------------------------------------------------------
# grid 策略
# ---------------------------------------------------------------------------
class CaptchaAwareCopy:
    """easytrader 的 IGridStrategy 实现：可见消歧 + ddddocr 认验证码 + 哨兵校验。"""

    #: 每张表重试几次。由 ThsClient 按配置覆盖。
    attempts = 3

    def __init__(self):
        self._trader = None
        # 空表也有表头，而**表头正是字段映射唯一需要的东西**。
        # easytrader 的 to_dict("records") 在 0 行时把它丢了，这里单独留一份。
        self.last_columns: list[str] = []

    def set_trader(self, trader):
        self._trader = trader

    def get(self, control_id: int) -> list[dict]:
        """取一张表。失败会重试 —— 复制这一步在实测中不稳定。

        有哨兵校验兜底，重试是安全的：不会把陈旧剪贴板当成新数据。
        """
        last: Exception | None = None
        for _ in range(max(1, self.attempts)):
            try:
                return self._get_once(control_id)
            except ThsReadError as exc:
                last = exc
                time.sleep(0.8)
        raise last if last else ThsReadError("取表失败")

    def _get_once(self, control_id: int) -> list[dict]:
        import io

        import pandas as pd
        import pywinauto.clipboard

        trader = self._trader
        # 本机 1047 有 6 个同 id 的 grid，只有当前查询页那个可见 —— 用可见性消歧。
        # easytrader 原生的 child_window(control_id=...) 在这里 ElementAmbiguousError。
        #
        # ⚠️ `descendants()` **不接受 control_id 过滤**，传了会被静默忽略
        # （2026-08-24 实测），所以必须自己比对 `control_id()`。这里恰好因为
        # CVirtualGridCtrl 同时只有一个可见而没出事，但那是运气，不是设计。
        grids = [e for e in trader.main.descendants(class_name="CVirtualGridCtrl")
                 if e.control_id() == control_id and e.is_visible()]
        if len(grids) != 1:
            raise ThsReadError(f"可见的 grid 有 {len(grids)} 个，无法消歧")

        set_clipboard(_SENTINEL)
        force_foreground(trader.main.wrapper_object().handle)
        # 光把窗口拉到前台不够，键盘焦点还得在表格上，否则 ^A^C 打到别的控件上，
        # 表现就是「剪贴板纹丝不动」。
        grids[0].click_input()
        time.sleep(0.2)
        grids[0].type_keys("^A^C", set_foreground=False, pause=0.2)

        # 反爬验证码是**异步**弹的，3 秒窗口会漏掉，然后它在调用返回后才冒出来
        # 卡在客户端上。8 秒是实测下来够用的值。
        captcha_seen = False
        deadline = time.time() + 8
        while time.time() < deadline:
            dialog = _find_captcha_dialog()
            if dialog is not None:
                captcha_seen = True
                if not solve_captcha(dialog):
                    ctypes.windll.user32.PostMessageW(dialog.handle, _WM_CLOSE, 0, 0)
                    raise ThsReadError("验证码识别失败（已关掉弹窗）")
                break
            time.sleep(0.2)

        # 肯定性校验：剪贴板必须真的变了才算取到。少了这一步就会把陈旧内容
        # 当成新数据解析出 0 行，而且全程不抛异常。
        deadline = time.time() + 5
        content = None
        while time.time() < deadline:
            try:
                content = pywinauto.clipboard.GetData()
            except Exception:  # noqa: BLE001 —— 剪贴板被别的进程短暂占用
                content = None
            if content and content != _SENTINEL:
                break
            time.sleep(0.2)
        if not content or content == _SENTINEL:
            # 两种情况必须分开报，否则又回到「看不出是空表还是坏了」。
            # 判据是**验证码有没有弹**：反爬只在真有数据可拷时触发，
            # 空 grid 上 Ctrl+A 什么都选不中，自然不会惊动它。
            if captcha_seen:
                raise ThsReadError("验证码已认掉但剪贴板仍未更新 —— 取表机制有问题")
            raise ThsReadError("剪贴板未被更新，且全程没弹验证码 —— 大概率是这张表本来就空")

        frame = pd.read_csv(io.StringIO(content), delimiter="\t",
                            dtype=trader.config.GRID_DTYPE, na_filter=False)
        self.last_columns = [c for c in frame.columns if not str(c).startswith("Unnamed")]
        return frame.to_dict("records")


_STRATEGIES = {"auto": CaptchaAwareCopy}


def _strategy(name: str, attempts: int):
    """按名字取 grid 策略。auto 之外的是 easytrader 原生的，只用于对照实验。"""
    if name == "auto":
        cls = type("_ConfiguredCopy", (CaptchaAwareCopy,), {"attempts": attempts})
        return cls
    from easytrader import grid_strategies

    native = {"xls": grid_strategies.Xls, "copy": grid_strategies.Copy,
              "wmcopy": grid_strategies.WMCopy}
    if name not in native:
        raise ValueError(f"未知的取表策略 {name!r}，可选：auto/{'/'.join(native)}")
    return native[name]


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------
def read_tables(*, exe: str, client: str = "universal_client",
                strategy: str = "auto", attempts: int = 3) -> dict:
    """连上同花顺，读回资金与持仓。**只读，没有下单路径。**

    返回 ``{"balance": {...}, "position": [...], "columns": [...]}``。
    任何失败都抛 :class:`ThsReadError`，由调用方决定是否降级。
    """
    import easytrader

    try:
        user = easytrader.use(client)
        user.grid_strategy = _strategy(strategy, attempts)
        user.connect(exe)
        # connect() 用 top_window() 认主窗口，会被 0x0 的 Internet Explorer_Hidden
        # 抢走。必须按标题重新钉一次，否则后面所有控件查找都在错误的父窗口下进行。
        hwnd = bind_main_window(user)
        log_event(log, "ths.connect.ok", main_hwnd=hwnd)
    except ThsReadError:
        raise
    except Exception as exc:  # noqa: BLE001 —— easytrader 抛的异常类型不稳定
        raise ThsReadError(f"连接同花顺失败：{type(exc).__name__}: {exc}") from exc

    entrusts, trades, columns = None, None, []
    try:
        balance = user.balance
        positions = user.position
        # **列头必须在这里抓，不能等到最后。** `last_columns` 记的是
        # grid_strategy 最近一次读到的表头，下面再读委托表就会把它覆盖成
        # 委托表的 12 列，于是持仓表的缺列检查拿 12 列去比 18 列的必需列，
        # 报一个「同花顺改了界面」的假警报。2026-08-25 加读委托表时踩到。
        columns = list(getattr(user.grid_strategy_instance, "last_columns", []) or [])
        # 今日委托是**尽力而为**的第三张表：只用来推算挂单冻结了多少现金
        # （资金表没有「冻结金额」这一列）。读不到就留 None 表示"未知"，
        # 绝不能让它把资金和持仓这两张真正要紧的表一起拖垮。
        try:
            entrusts = user.today_entrusts
        except Exception as exc:  # noqa: BLE001
            log_event(log, "ths.read.entrusts_failed", error=f"{type(exc).__name__}: {exc}")
        # 今日成交是**第四张尽力而为的表**，只服务一件事：量实际成交价和
        # 回测假设（次日开盘价）差多少。那是整套回测里唯一没被实测过的输入。
        # 和委托表同样的纪律 —— 读不到留 None，绝不拖垮资金和持仓。
        try:
            trades = user.today_trades
        except Exception as exc:  # noqa: BLE001
            log_event(log, "ths.read.trades_failed", error=f"{type(exc).__name__}: {exc}")
    except ThsReadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ThsReadError(f"读取失败：{type(exc).__name__}: {exc}") from exc
    finally:
        # 无论成败都收拾弹窗。验证码框是异步弹的，可能在调用返回后才冒出来，
        # 所以这里多扫几轮而不是只扫一次。
        leftovers: list[str] = []
        for _ in range(8):
            time.sleep(0.5)
            leftovers += cleanup_dialogs()
        if leftovers:
            log_event(log, "ths.dialogs_closed", count=len(leftovers), detail=leftovers[:5])

    log_event(log, "ths.read.ok", positions=len(positions), columns=len(columns),
              entrusts=None if entrusts is None else len(entrusts),
              trades=None if trades is None else len(trades))
    return {"balance": dict(balance or {}), "position": list(positions or []),
            "columns": columns,
            "entrusts": None if entrusts is None else list(entrusts),
            "trades": None if trades is None else list(trades)}
