"""同花顺**下单表单输入路径**探针 —— 只填不交。

⚠️ **这个脚本不会提交任何订单。** 它只做一件事：把值填进买入页的输入框，
然后判断客户端**有没有真的收到**。提交按钮（control 1006）在本文件里
不存在任何点击路径，不要往里加 —— 下单是 P9b，走 execution 层。

    python tools/probe_ths_order.py                 # 默认用 600519 试
    python tools/probe_ths_order.py --code 000001

为什么必须先做这个探测
----------------------
easytrader 的 `_type_edit_control_keys` 默认走 `set_edit_text`（WM_SETTEXT）：

    def _type_edit_control_keys(self, control_id, text):
        if not self._editor_need_type_keys:
            self._main.child_window(control_id=control_id, class_name="Edit").set_edit_text(text)

而 2026-08-21 实测已经证明：**这个客户端上 `set_edit_text` 会「收下但不认账」**
（在反爬验证码输入框上确证 —— 填完点确定，弹窗纹丝不动；换 `type_keys` 立刻通过）。

如果买入页的价格框（1033）/数量框（1034）也是这样，那么 easytrader 会
「填」完价格数量、然后**照样点提交** —— 这不是失败，是**下出错误的单**。
所以在写 P9b 的 adapter 之前必须先把这件事钉死。

怎么判断输入真的进去了
----------------------
**不能靠回读控件文本**：实测验证码输入框在 type_keys 成功的情况下
`window_text()` 依然返回空字符串，回读会给出假阴性。

改用**客户端的副作用**做预言机 —— 证券代码填对后，客户端会自动回填：

    1036 Static  证券名称
    1018 Static  可买(股)

这两个字段一旦出现，就说明客户端真的处理了输入，而不只是控件里躺了段文本。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qbg.portfolio.ths_client import (  # noqa: E402
    bind_main_window,
    cleanup_dialogs,
    force_foreground,
)

# 买入页控件（2026-08-21 实测，同花顺 9.60.61）
CODE_ID = 1032        # Edit  证券代码
NAME_ID = 1036        # Static 证券名称   —— 客户端自动回填，用作预言机
PRICE_ID = 1033       # Edit  买入价格
AVAILABLE_ID = 1018   # Static 可买(股)   —— 客户端自动回填，用作预言机
AMOUNT_ID = 1034      # Edit  买入数量
# 1006 是「买入」提交按钮。**本文件不点它**，只在这里留个名字提醒后来者。


def _edit(user, control_id):
    return user.main.child_window(control_id=control_id, class_name="Edit")


def _static_text(user, control_id: str) -> str:
    try:
        return user.main.child_window(control_id=control_id, class_name="Static").window_text()
    except Exception:  # noqa: BLE001
        return "<读不到>"


# 清空输入框的按键序列。
#
# **不能用 `^a{DEL}`（Ctrl+A 全选）** —— 这个 Edit 控件不支持 Ctrl+A，DEL 只会
# 往后删一个字符，残留内容原样留着。2026-08-21 实测后果：填完证券代码后客户端
# 会**自动回填市价**（1272.90），清不掉的话我们输入的 1213.97 被**追加**在后面，
# 变成 1272.901213.97 —— 客户端于是弹「委托价格的小数部分应为 2 位，是否继续？」，
# 而这条提示很容易被误读成「价格格式写错了」，实际是没清干净。
#
# {HOME} 回到开头，+{END} 是 Shift+End 选到结尾，{DEL} 删掉选区。
CLEAR_KEYS = "{HOME}+{END}{DEL}"


def _clear_form(user) -> None:
    """把三个输入框清空，别在客户端上留下半张单子。"""
    for control_id in (CODE_ID, PRICE_ID, AMOUNT_ID):
        try:
            edit = _edit(user, control_id)
            edit.set_focus()
            edit.type_keys(CLEAR_KEYS, set_foreground=False, pause=0.05)
        except Exception:  # noqa: BLE001
            pass
    time.sleep(0.3)


def _snapshot(user, *, ocr: bool = False) -> dict:
    snap = {
        "代码框回读": _readback(user, CODE_ID),
        "价格框回读": _readback(user, PRICE_ID),
        "数量框回读": _readback(user, AMOUNT_ID),
        "证券名称(客户端回填)": _static_text(user, NAME_ID),
        "可买股数(客户端回填)": _static_text(user, AVAILABLE_ID),
    }
    if ocr:
        snap["代码框OCR"] = _ocr_readback(user, CODE_ID)
        snap["价格框OCR"] = _ocr_readback(user, PRICE_ID)
        snap["数量框OCR"] = _ocr_readback(user, AMOUNT_ID)
    return snap


def _readback(user, control_id: int) -> str:
    try:
        return _edit(user, control_id).window_text()
    except Exception:  # noqa: BLE001
        return "<读不到>"


def _ocr_readback(user, control_id: int) -> str:
    """截图输入框再 OCR —— 这是本客户端上**唯一可靠**的回读手段。

    `window_text()` 在这些框上恒为空（实测），所以拿不到实际内容。但我们为了
    认反爬验证码已经装了 ddddocr，正好拿来读输入框里到底是什么。

    这条很重要：2026-08-21 用 `type_keys` 打 `1213.97`，客户端却弹
    「委托价格的小数部分应为 2 位，是否继续？」—— 说明它收到的**不是**
    我们以为的那个数。没有这个回读手段，就只能靠猜。
    """
    import io

    try:
        from qbg.portfolio.ths_client import _ocr

        buf = io.BytesIO()
        _edit(user, control_id).capture_as_image().save(buf, format="PNG")
        return _ocr().classification(buf.getvalue())
    except Exception as exc:  # noqa: BLE001
        return f"<OCR失败 {type(exc).__name__}>"


def _keys_for(text: str) -> str:
    """把要输入的文本翻成 type_keys 的按键序列。

    小数点写成 `{VK_DECIMAL}`（小键盘的点）。**这不是必需的** —— 截图确认过，
    直接打 "." 同样能得到 `1213.97`。用 VK_DECIMAL 只是为了绕开输入法：
    中文输入法在半角/全角状态下可能把 "." 变成 "。"，而 `{VK_DECIMAL}` 是
    直接发虚拟键，不经过输入法。

    ⚠️ 别被 OCR 骗了：ddddocr 不输出标点，`1213.97` 一律读成 `121397`。
    我一度据此以为小数点没打进去，截图看过才发现输入本来就是对的。
    """
    return "{VK_DECIMAL}".join(text.split("."))


def _fill(user, control_id: int, text: str, method: str) -> None:
    edit = _edit(user, control_id)
    if method == "set_edit_text":
        edit.set_edit_text(text)
    else:
        edit.set_focus()
        time.sleep(0.1)
        edit.type_keys(CLEAR_KEYS, set_foreground=False, pause=0.05)
        edit.type_keys(_keys_for(text), set_foreground=False, pause=0.08)


def try_method(user, method: str, code: str, price: str, amount: str) -> dict:
    """用一种填值方式填完整张单子，返回观察结果。**不提交。**"""
    # 每轮都重新抢前台。后台进程调 set_focus()/SetForegroundWindow 会被 Windows
    # 的前台锁**静默**拒绝，表现就是「填了但客户端毫无反应」——和它真的不认账
    # 长得一模一样，极易误判。
    force_foreground(user.main.wrapper_object().handle)
    _clear_form(user)
    before = _snapshot(user)

    _fill(user, CODE_ID, code, method)
    # 客户端要花点时间去查名称和可买量，别急着读
    time.sleep(1.2)
    after_code = _snapshot(user)

    _fill(user, PRICE_ID, price, method)
    _fill(user, AMOUNT_ID, amount, method)
    time.sleep(0.6)
    after_all = _snapshot(user, ocr=True)

    name_filled = bool(after_code["证券名称(客户端回填)"].strip()) and \
        not before["证券名称(客户端回填)"].strip()
    available_filled = bool(after_code["可买股数(客户端回填)"].strip()) and \
        not before["可买股数(客户端回填)"].strip()
    return {"method": method, "before": before, "after_code": after_code,
            "after_all": after_all,
            "expected": {"代码": code, "价格": price, "数量": amount},
            "客户端回填了证券名称": name_filled,
            "客户端回填了可买股数": available_filled}


def _report(result: dict) -> bool:
    method = result["method"]
    print(f"\n=== 填值方式：{method} ===")
    print("  填完代码后：")
    for key in ("代码框回读", "证券名称(客户端回填)", "可买股数(客户端回填)"):
        print(f"    {key:<22} {result['after_code'][key]!r}")
    print("  填完价格数量后（OCR 才是可信回读，window_text 恒为空）：")
    for key in ("价格框回读", "价格框OCR", "数量框回读", "数量框OCR", "代码框OCR"):
        if key in result["after_all"]:
            print(f"    {key:<22} {result['after_all'][key]!r}")
    # 「输入被接收」和「值是正确的」是两件事，必须分开报。
    # 混在一起的话，一个丢了小数点的价格会被算成「输入失败」，
    # 而它其实是更危险的那种：进去了，但不是你要的数。
    landed = result["客户端回填了证券名称"] or result["客户端回填了可买股数"]
    print(f"  输入是否被客户端接收：{'✅ 是' if landed else '❌ 否'}")
    values_ok = None
    if landed and result.get("expected"):
        values_ok = True
        print("  OCR 回读 vs 期望：")
        for label, key in (("代码", "代码框OCR"), ("价格", "价格框OCR"), ("数量", "数量框OCR")):
            got = _normalize_ocr(result["after_all"].get(key, ""))
            want = _normalize_ocr(result["expected"][label])
            same = got == want
            print(f"    [{'OK ' if same else '差异'}] {label}: 期望 {want!r}  实际 {got!r}")
            values_ok = values_ok and same
        print(f"  三个框的值是否都正确：{'✅ 是' if values_ok else '❌ 否'}")
    return {"landed": landed, "values_ok": values_ok}


def _normalize_ocr(text: str) -> str:
    """把 OCR 结果归一成可比对的**数字序列**。

    两条实测出来的边界：

    1. ddddocr 是为字母数字验证码训练的，常把 `0` 认成 `o`、`1` 认成 `l/i`，
       控件边框还会多出一两个杂字符（代码框读出 `600519x`）。
    2. **它不输出标点。** `1213.97` 一律读成 `121397` —— 我一度据此断定
       小数点没打进去，截图看过才发现是 OCR 的局限，输入本身是对的。

    所以两边都去掉小数点，只比数字序列。这个校验**能**抓住数字错了、多了、
    少了（比如清空失败导致旧值残留）；**抓不住**小数点位置错误。
    小数点由客户端自己兜底 —— 它会弹「委托价格的小数部分应为 2 位，是否继续？」，
    P9b 应当把这个提示当作**致命信号直接中止**，而不是点「是」继续。
    """
    swapped = str(text).lower().replace("o", "0").replace("l", "1").replace("i", "1")
    return "".join(ch for ch in swapped if ch.isdigit())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="同花顺下单表单输入路径探针（只填不交，不会提交任何订单）")
    parser.add_argument("--code", default="600519", help="用来试填的股票代码")
    parser.add_argument("--price", default="1.00", help="试填价格；不会提交，填多少都无所谓")
    parser.add_argument("--amount", default="100", help="试填数量；不会提交")
    parser.add_argument("--exe", default=r"D:\tonghuashun\同花顺\xiadan.exe")
    args = parser.parse_args(argv)

    import easytrader

    user = easytrader.use("universal_client")
    user.connect(args.exe)
    # connect() 用 top_window() 认主窗口，会被 0x0 的 Internet Explorer_Hidden
    # 抢走，之后所有控件查找都报 ElementNotFoundError。按标题重新钉死。
    bind_main_window(user)
    account = ""
    try:
        account = user.main.child_window(control_id=2322,
                                         class_name="ComboBox").window_text()
    except Exception:  # noqa: BLE001
        pass
    print(f"当前账户：{account!r}")
    if "模拟" not in account:
        # 这是一个只填不交的脚本，理论上对真实账户也无害。但既然它是为
        # 「P9b 之前的写入路径验证」而生，跑在模拟账户上才是本意 —— 提醒一下。
        print("  ⚠ 看起来不是模拟账户。本脚本不会提交订单，但建议在模拟账户上验证。")

    user._switch_left_menus(["买入[F1]"])
    time.sleep(0.5)

    results = []
    try:
        for method in ("set_edit_text", "type_keys"):
            results.append(try_method(user, method, args.code, args.price, args.amount))
    finally:
        _clear_form(user)
        leftovers = cleanup_dialogs()
        if leftovers:
            print(f"\n清理了 {len(leftovers)} 个弹窗：{leftovers}")

    verdicts = {r["method"]: _report(r) for r in results}

    print("\n=== 结论 ===")
    set_edit = verdicts.get("set_edit_text") or {}
    typed = verdicts.get("type_keys") or {}
    if set_edit.get("landed"):
        print("  set_edit_text 可用 —— easytrader 默认路径没问题。")
    elif typed.get("landed"):
        print("  ⚠ set_edit_text **不被认账**，但 type_keys 可以。")
        print("  → P9b 的 adapter 必须调用 user.enable_type_keys_for_editor()，")
        print("    否则 easytrader 会「填」完价格数量然后照样点提交 —— 下出错误的单。")
    else:
        print("  ❌ 两种方式客户端都没反应。先别写 P9b，回头查焦点/权限/控件 id。")
        print("\n（本次运行没有提交任何订单。）")
        return 1

    if typed.get("values_ok"):
        print("  ✅ 三个输入框的值与期望完全一致 —— 输入路径可用于 P9b。")
    elif typed.get("values_ok") is False:
        print("  ❌ **输入进去了但值不对** —— 这是最危险的一类故障：不报错，直接下错单。")
        print("     P9b 在点提交之前必须 OCR 回读比对三个框，不一致就中止。")
    print("\n（本次运行没有提交任何订单。）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
