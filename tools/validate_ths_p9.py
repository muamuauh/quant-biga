"""P9a 取值验证 + P9b 卖出路径验证 —— 需要**交易时段**运行。

🔴 **这个脚本会真的下单。** 和 `probe_ths.py` / `probe_ths_order.py` 不同 ——
   那两个只读/只填不交，本脚本会**实际提交买单并等待成交**。

   三道硬闸，缺一不可，全在 `main()` 开头：
     1. `QBG_MODE` 必须是 `PAPER`（脚本自己设环境变量，不动你的 .env）
     2. 账户名必须含「模拟」二字
     3. 必须在交易时段内（`--force` 可绕过，但买单不会成交）

   **不要把它接进 daily_cycle，也不要去掉这三道闸。** 真实下单走
   `execution/easytrader_adapter.py`，那里有三把锁。

⚠️ **必须在交易时段跑**（北京时间 09:30–11:30 / 13:00–15:00 的交易日）。
   收盘后买单不会成交，A 段会超时并撤单退出。

四段：

  A. 建仓 —— 买入并**等待真正成交**（限价挂在市价上方以确保成交）
  B. P9a 取值验证 —— 用 `load_portfolio("easytrader")` 读回持仓，逐字段核对
  C. 卖出路径验证 —— 卖单**挂在市价上方 2%**，挂得住、不成交。
     当天建的仓（可用=0）会被 T+1 拒绝，验证拒绝路径；
     隔夜的仓（可用=qty）会被接受，验证**真正的下单 + 回读路径**。
  D. 清理 —— 撤掉任何遗留委托

**持仓会留着不卖。** 卖单挂在市价上方就是为了这个 —— 我们要验的是「委托能不能
正确下出去并被回读到」，成交是券商撮合的事，不归这条链路管。

第二次跑（隔夜持仓，可用余额已解冻）用：

    python tools/validate_ths_p9.py --skip-build
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
import warnings

warnings.filterwarnings("ignore")

# 必须在 import qbg.config 之前：环境变量优先级高于 .env，不会动你的配置文件
os.environ["QBG_MODE"] = "PAPER"
REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import easytrader  # noqa: E402

from qbg.config import settings  # noqa: E402
from qbg.execution.base import Order  # noqa: E402
from qbg.execution.easytrader_adapter import EasytraderAdapter  # noqa: E402
from qbg.execution.ths_order_form import (  # noqa: E402
    CODE_ID,
    NAME_ID,
    PRICE_ID,
    fill_field,
    ocr_digits,
)
from qbg.market import calendar  # noqa: E402
from qbg.portfolio.easytrader_source import EasytraderSource  # noqa: E402
from qbg.portfolio.ths_client import (  # noqa: E402
    CaptchaAwareCopy,
    bind_main_window,
    cleanup_dialogs,
    force_foreground,
)

EXE = settings.qbg_ths_exe        # 换机器改 QBG_THS_EXE 配置，别改代码


def connect():
    user = easytrader.use("universal_client")
    # 类默认是 grid_strategy=Xls，在这个客户端上零验证码处理、必然失败
    user.grid_strategy = CaptchaAwareCopy
    user.connect(EXE)
    bind_main_window(user)
    user.enable_type_keys_for_editor()
    return user


def reference_price(user, code: str) -> tuple[str, float]:
    """读客户端自动回填的市价。OCR 只给数字，但 A 股价格恒为 2 位小数。"""
    force_foreground(user.main.wrapper_object().handle)
    user._switch_left_menus(["买入[F1]"])
    time.sleep(0.5)
    force_foreground(user.main.wrapper_object().handle)
    fill_field(user, CODE_ID, code)
    time.sleep(1.3)
    name = user.main.child_window(control_id=NAME_ID, class_name="Static").window_text()
    digits = ocr_digits(user, PRICE_ID)
    if not name.strip() or not digits:
        raise SystemExit(f"读不到参考价（名称={name!r} 数字={digits!r}），中止")
    return name, int(digits) / 100


def read_positions(source: EasytraderSource):
    return source.load()


# ---------------------------------------------------------------------------
def phase_a_build(user, code: str, qty: int, wait: int) -> bool:
    print("\n=== A. 建仓（买入并等待成交）===")
    name, ref = reference_price(user, code.split(".")[0])
    # 挂在市价上方 2% 以确保成交：限价单成交在对手价，不会真按这个价成交。
    # 仍在 ±10% 涨跌停内。
    price = round(ref * 1.02, 2)
    print(f"  {name} 参考价 {ref:.2f} → 买单挂 {price:.2f}（+2%，确保成交）")

    order = Order(code=code, side="BUY", quantity=qty, price=price,
                  reason="P9 建仓验证", name=name)
    result = EasytraderAdapter(connect=lambda: user).submit([order], "validation")
    print(f"  提交结果：ok={result.ok} {result.message}")
    if not result.ok:
        return False

    print(f"  等待成交（最多 {wait}s）…")
    source = EasytraderSource()
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(10)
        try:
            snapshot = read_positions(source)
        except Exception as exc:  # noqa: BLE001
            print(f"    读持仓失败（继续等）：{type(exc).__name__}: {exc}")
            continue
        if snapshot.positions:
            print(f"    ✅ 已有持仓 {len(snapshot.positions)} 只")
            return True
        print("    还没成交…")
    print("  ❌ 超时未成交 —— 现在是交易时段吗？")
    return False


def phase_b_verify(qty: int) -> bool:
    print("\n=== B. P9a 取值验证 ===")
    try:
        snapshot = EasytraderSource().load()
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 读持仓失败：{type(exc).__name__}: {exc}")
        return False

    print(f"  asof={snapshot.asof} 总资产={snapshot.total_equity} 可用={snapshot.available_cash}")
    if not snapshot.positions:
        print("  ❌ 持仓为空")
        return False

    ok = True
    for position in snapshot.positions:
        print(f"\n  {position.code} {position.name}")
        checks = [
            ("qty 是正整数且为一手整数倍",
             position.qty > 0 and position.qty % 100 == 0, position.qty),
            ("cost_price > 0", position.cost_price > 0, position.cost_price),
            ("last_price > 0", position.last_price > 0, position.last_price),
            ("market_value ≈ qty × last_price",
             abs(position.market_value - position.qty * position.last_price)
             / max(position.market_value, 1) < 0.01, position.market_value),
            # T+1 的核心：当天买入的部分当天不可卖。
            # sellable_qty 正是 easytrader 相对截图 OCR 的真正增量。
            ("sellable_qty ≤ qty", position.sellable_qty <= position.qty,
             position.sellable_qty),
        ]
        for label, passed, value in checks:
            print(f"    [{'OK ' if passed else '差异'}] {label:<28} = {value}")
            ok = ok and passed
        if position.sellable_qty == 0:
            print("    ★ sellable_qty=0 —— 当天买入当天不可卖，T+1 语义正确")
        elif position.sellable_qty == position.qty:
            print("    ★ sellable_qty=qty —— 全部可卖（持仓是之前交易日建的）")
    return ok


def phase_c_sell_path(user, code: str, qty: int) -> bool:
    """卖出路径验证。

    两种场景都能跑：
      * 当天刚建的仓（`可用余额=0`）→ 被 T+1 拒绝，验证的是拒绝路径
      * 隔夜的仓（`可用余额=qty`）→ 卖单被接受，验证的是**真正的下单路径**

    **卖单必须挂在市价上方**（这里 +2%）。买单挂下方不会成交，卖单同理要挂
    上方才不会成交 —— 挂下方等于市价甩卖，会**真的把持仓卖掉**。
    我们要验证的是「委托能不能正确下出去并被回读到」，不是让它成交；
    成交是券商撮合的事，不归这条链路管。D 段会把这张挂单撤掉，持仓保住。
    """
    print("\n=== C. 卖出路径验证 ===")
    name, ref = reference_price(user, code.split(".")[0])
    price = round(ref * 1.02, 2)          # 高于市价 → 挂得住、不成交
    print(f"  卖单挂 {price:.2f}（参考价 {ref:.2f} 的 +2%，挂得住不成交），数量 {qty}")

    order = Order(code=code, side="SELL", quantity=qty, price=price,
                  reason="P9b 卖出路径验证", name=name)
    result = EasytraderAdapter(connect=lambda: user).submit([order], "validation")
    print(f"  提交结果：ok={result.ok}")
    print(f"  message: {result.message}")

    # 三种结果含义完全不同，必须分开报 —— 早先这里无条件打印「✅ 卖单被拒绝」，
    # 把「我们不知道发生了什么」说成了「我们正确识别了拒绝」。
    if result.ok:
        print("  ⚠ 卖单**被接受**了 —— 说明这批持仓当天可卖（不是今天买的）。")
        print("     卖出链路打通，但 T+1 的负向场景没测到。D 段会撤掉它。")
    elif "客户端提示" in result.message:
        print("  ✅ 客户端给出了明确的拒绝理由，且被我们的致命词表抓住 —— 识别正确。")
    elif "没有出现这一笔" in result.message:
        print("  ✅ **静默拒绝**：客户端不建委托、不报错、不给理由。")
        print("     这正是回读校验存在的意义 —— 没有它，这笔单会被当成成功。")
        print("     卖出链路本身是通的（填单 → OCR 校验 → 委托确认框核对都过了）。")
    else:
        print("  ❌ 卖出链路本身出了问题（填单或回读失败），要查上面的 message。")
    return True


def phase_d_cleanup(user) -> None:
    print("\n=== D. 清理：撤掉遗留委托 ===")
    force_foreground(user.main.wrapper_object().handle)
    user._switch_left_menus(["撤单[F3]"])
    time.sleep(0.8)
    try:
        btn = user.main.child_window(control_id=30001, class_name="Button")
        if not btn.is_enabled():
            print("  没有可撤的委托")
        else:
            btn.click()
            time.sleep(1.0)
            import ctypes

            from qbg.portfolio.ths_client import enum_dialogs, find_dialog_button
            u32 = ctypes.windll.user32
            for hwnd, kids in enum_dialogs():
                bid = find_dialog_button(kids)
                if bid:
                    u32.SendMessageW(u32.GetDlgItem(hwnd, bid), 0x00F5, 0, 0)
                else:
                    u32.PostMessageW(hwnd, 0x0010, 0, 0)
                time.sleep(0.8)
            print("  已全撤")
    except Exception as exc:  # noqa: BLE001
        print(f"  撤单失败：{type(exc).__name__}: {exc}")
    print("  清理弹窗:", cleanup_dialogs())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="P9a 取值 + P9b 卖出路径验证（需交易时段）")
    parser.add_argument("--code", default="601398.SH",
                        help="用来建仓的股票，默认工商银行（便宜、流动性好）")
    parser.add_argument("--qty", type=int, default=100)
    parser.add_argument("--wait", type=int, default=90, help="等待成交的秒数")
    parser.add_argument("--force", action="store_true",
                        help="非交易时段也硬跑（买单不会成交，A 段必然超时）")
    parser.add_argument("--skip-build", action="store_true",
                        help="已经有持仓了，跳过 A 段直接验证")
    args = parser.parse_args(argv)

    print(f"QBG_MODE = {settings.qbg_mode}")
    if settings.qbg_mode.upper() != "PAPER":
        print("不是 PAPER 模式，中止。")
        return 1

    # 交易时段硬闸：A 段要真成交才有意义。收盘跑只会白等满 --wait 秒，
    # 然后给出一个「超时未成交」的假阴性结论 —— 那比直接拒绝更浪费时间。
    # `calendar.in_session()` 是 P2 就建好的，直接复用。
    import datetime as _dt
    now = _dt.datetime.now()
    trading = calendar.is_trading_day(now.date().isoformat())
    session = calendar.in_session(now)
    print(f"现在 {now:%Y-%m-%d %H:%M} 交易日={trading} 时段内={session}")
    if not (trading and session) and not args.skip_build and not args.force:
        nxt = calendar.next_trading_day(now.date().isoformat())
        print(f"不在交易时段，买单不会成交。下一个交易日：{nxt}（09:30–11:30 / 13:00–15:00）")
        print("已有持仓想只跑验证，用 --skip-build；要硬跑用 --force。")
        return 3

    user = connect()
    account = user.main.child_window(control_id=2322, class_name="ComboBox").window_text()
    print(f"账户：{account!r}")
    if "模拟" not in account:
        print("不是模拟账户，中止。")
        return 1

    try:
        if not args.skip_build and not phase_a_build(user, args.code, args.qty, args.wait):
            print("\n建仓未成功，跳过后续验证。")
            phase_d_cleanup(user)
            return 2
        phase_b_verify(args.qty)
        phase_c_sell_path(user, args.code, args.qty)
    finally:
        phase_d_cleanup(user)

    print("\n完成。卖单挂在市价上方且已由 D 段撤掉，持仓保留。")
    print("当天建的仓 sellable_qty=0（T+1 冻结）；隔一个交易日再跑会变成 =qty。")
    print("两种场景都跑过，才算把 T+1 语义验完 —— 第二次用 --skip-build。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
