"""同花顺客户端只读探针 —— 验证 easytrader 能否驱动本机的 xiadan.exe。

**这个脚本没有任何下单代码路径。** 它只做四件只读查询：
资金、持仓、当日委托、当日成交。要下单请走 execution 层，不要往这里加。

    python tools/probe_ths.py                      # 预检 + 只读查询
    python tools/probe_ths.py --preflight-only     # 只做环境预检，不碰客户端
    python tools/probe_ths.py --grid copy          # 换成 easytrader 原生策略做对照
    python tools/probe_ths.py --save               # 结果写 data/portfolio/（gitignored）

为什么要预检：pywinauto 驱动失败时的表现是"连得上、发得出、没反应"，
几乎无法从异常信息倒推原因。下面几项是最常见的失败原因，先一次性排掉。
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wintypes
import json
import os
import platform
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qbg.portfolio.ths_client import (  # noqa: E402
    CaptchaAwareCopy,
    bind_main_window,
    cleanup_dialogs,
)

# 本机实测路径（同花顺 9.60.61）。别的机器用 --exe 覆盖。
DEFAULT_EXE = r"D:\tonghuashun\同花顺\xiadan.exe"

# 同花顺官网通用客户端用 universal_client；券商自己发的改版包用 ths。
# D:\tonghuashun\同花顺\ 是官网通用版的默认安装位置，所以默认 universal。
DEFAULT_CLIENT = "universal_client"


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------
def _integrity_level(pid: int) -> str:
    """进程的完整性级别。UIPI 禁止低完整性进程向高完整性窗口发模拟输入。"""
    k32, a32 = ctypes.windll.kernel32, ctypes.windll.advapi32
    a32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
    a32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
    a32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)
    a32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE

    handle = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return f"?(OpenProcess {k32.GetLastError()})"
    token = wintypes.HANDLE()
    if not a32.OpenProcessToken(handle, 0x0008, ctypes.byref(token)):  # TOKEN_QUERY
        return f"?(OpenProcessToken {k32.GetLastError()})"
    size = wintypes.DWORD()
    a32.GetTokenInformation(token, 25, None, 0, ctypes.byref(size))  # TokenIntegrityLevel
    buf = ctypes.create_string_buffer(size.value)
    if not a32.GetTokenInformation(token, 25, buf, size, ctypes.byref(size)):
        return f"?(GetTokenInformation {k32.GetLastError()})"
    sid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
    count = a32.GetSidSubAuthorityCount(sid)[0]
    rid = a32.GetSidSubAuthority(sid, count - 1)[0]
    return {0x1000: "Low", 0x2000: "Medium", 0x2100: "Medium+",
            0x3000: "High", 0x4000: "System"}.get(rid, hex(rid))


def _pids_named(name: str) -> list[int]:
    """按可执行文件名找进程，不依赖 psutil。"""
    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="gbk", errors="replace", check=False,
    ).stdout
    pids: list[int] = []
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == name.lower():
            try:
                pids.append(int(parts[1]))
            except ValueError:
                pass
    return pids


TRADE_WINDOW_TITLE = "网上股票交易系统5.0"


class _WINDOWPLACEMENT(ctypes.Structure):
    _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                ("showCmd", ctypes.c_uint),
                ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                ("rcNormalPosition", wintypes.RECT)]


def _find_trade_window() -> tuple[int, str] | None:
    """按标题找交易主窗口，返回 (hwnd, 状态)。状态取值见下。

    不用 pywinauto 的 find_windows：它默认只返回**可见**窗口，而交易窗被隐藏时
    窗口仍然存在只是 visible=False —— 那种情况下 pywinauto 报"找不到窗口"，
    会把人引向完全错误的方向。这里用裸 EnumWindows 自己找。

    三种"不可见"必须分开，因为修法完全不同：
      * 最小化       —— 还原一下就行
      * 被程序隐藏   —— 是**精简模式**：showCmd 仍是 SW_NORMAL，却 ShowWindow(SW_HIDE)
                        了。同花顺精简模式只留一个约 238x29 的下单小条，
                        主窗口整个藏起来，UI 自动化没有任何可操作的控件树
      * 窗口不存在   —— 没登录交易
    """
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
    if not found:
        return None

    hwnd = found[0]
    if u32.IsWindowVisible(hwnd):
        return hwnd, "visible"
    if u32.IsIconic(hwnd):
        return hwnd, "minimized"
    placement = _WINDOWPLACEMENT()
    placement.length = ctypes.sizeof(placement)
    u32.GetWindowPlacement(hwnd, ctypes.byref(placement))
    # showCmd 说"正常"但窗口不可见 = 程序主动 ShowWindow(SW_HIDE) 了 = 精简模式
    return hwnd, "hidden_by_app" if placement.showCmd == 1 else f"showCmd={placement.showCmd}"


def _pe_machine(path: Path) -> str:
    """读 PE 头判断 32/64 位。pywinauto 跨位数驱动是已知的踩坑来源。"""
    try:
        with path.open("rb") as fh:
            fh.seek(0x3C)
            fh.seek(struct.unpack("<I", fh.read(4))[0] + 4)
            machine = struct.unpack("<H", fh.read(2))[0]
        return {0x14C: "32-bit", 0x8664: "64-bit"}.get(machine, hex(machine))
    except OSError as exc:
        return f"?({exc})"


def preflight(exe: Path) -> list[tuple[str, bool, str]]:
    """返回 [(检查项, 是否通过, 说明)]。不通过不代表一定跑不了，但要先看一眼。"""
    checks: list[tuple[str, bool, str]] = []

    checks.append(("平台是 Windows", sys.platform == "win32", sys.platform))

    exists = exe.is_file()
    exe_bits = _pe_machine(exe) if exists else "找不到"
    checks.append(("客户端存在", exists, f"{exe} {exe_bits}"))

    pids = _pids_named("xiadan.exe")
    checks.append(("xiadan.exe 已启动", bool(pids),
                   f"pid={pids}" if pids else "未运行，请先手动打开并登录"))

    # 交易窗口存在性与可见性 —— 和"进程在跑"是两回事
    win = _find_trade_window()
    if win is None:
        checks.append((f"{TRADE_WINDOW_TITLE} 窗口", False, "窗口不存在，多半是没登录交易"))
    else:
        hwnd, state = win
        note = {
            "visible": "hwnd={} 正常显示",
            "minimized": "hwnd={} 最小化了，还原一下",
            "hidden_by_app": "hwnd={} 被程序隐藏 —— **精简模式**，主窗口整个藏起来了",
        }.get(state, "hwnd={} 状态异常：" + state).format(hwnd)
        checks.append((f"{TRADE_WINDOW_TITLE} 窗口", state == "visible", note))

    # 完整性级别对比 —— 本机实测最容易卡住的一条。
    # 2026-08-21 实测结论：跨级别**读**是通的（WM_GETTEXT、控件文本、枚举都正常），
    # 被挡住的只有**写**（模拟点击、按键）。而 easytrader 取任何数据都要先用
    # 菜单导航到对应查询页，那一步就是写 —— 所以读得通并不代表能用。
    me = _integrity_level(os.getpid())
    if pids:
        theirs = _integrity_level(pids[0])
        ok = not (me.startswith("Medium") and theirs in ("High", "System"))
        note = f"python={me} / xiadan={theirs}"
        if not ok:
            note += " —— 读得到、写不了"
        checks.append(("完整性级别不低于客户端", ok, note))
    else:
        checks.append(("完整性级别不低于客户端", False, f"python={me} / xiadan=(未运行)"))

    py_bits = platform.architecture()[0]
    checks.append(("Python 与客户端位数一致", py_bits == exe_bits,
                   f"python={py_bits} / xiadan={exe_bits}"))

    try:
        import easytrader  # noqa: F401
        import pywinauto
        checks.append(("easytrader / pywinauto 已装", True, f"pywinauto {pywinauto.__version__}"))
    except ImportError as exc:
        checks.append(("easytrader / pywinauto 已装", False,
                       f"{exc.name} 缺失 —— pip install easytrader"))

    # 验证码识别 —— 不是可选项。
    # 同花顺对**拷贝数据**这个动作本身有反爬：一取表就弹
    # 「检测到您正在拷贝数据，为保护您的账号数据安全，请先输入验证码」。
    # 这个框是模态的，不认掉它 Ctrl+A/Ctrl+C 根本进不了 grid，
    # 于是 easytrader 读到陈旧剪贴板、静默返回空列表。
    checks.append(("验证码识别可用", *_captcha_backend()))

    return checks


def _captcha_backend() -> tuple[bool, str]:
    """有没有能识别验证码的后端。

    优先 ddddocr：纯 pip、不需要往系统里装二进制，而且它就是为这类四位扭曲
    验证码训练的，比 tesseract 准得多。tesseract 作为备选 —— 注意它需要
    **二进制**，光 pip install pytesseract 不够用。
    """
    try:
        import ddddocr  # noqa: F401
        return True, f"ddddocr {getattr(ddddocr, '__version__', '?')}"
    except ImportError:
        pass
    try:
        import pytesseract
        return True, f"tesseract {pytesseract.get_tesseract_version()}"
    except ImportError:
        return False, "ddddocr / pytesseract 都没装"
    except Exception:  # noqa: BLE001 —— pytesseract 找不到二进制时抛自定义异常
        return False, "pytesseract 已装但找不到 tesseract.exe；建议改用 ddddocr"


HINTS = {
    "完整性级别不低于客户端":
        "同花顺以管理员身份在跑。UIPI 允许低完整性进程**读**高完整性窗口，\n"
        "     但**禁止写**（模拟点击、按键）。而 easytrader 取任何数据都要先用菜单\n"
        "     导航到查询页，那一步就是写 —— 所以「读得到资金数字」不代表能用。\n"
        "     pywinauto 自己也会警告 no rights to make changes in the target GUI。\n"
        "     二选一：\n"
        "     A. 退出同花顺，用普通权限重开（推荐：长期挂定时任务时权限更小）\n"
        "     B. 以管理员身份打开终端再跑本脚本",
    "网上股票交易系统5.0 窗口":
        "注意：要显示的是**独立下单程序 xiadan.exe** 的窗口，不是同花顺行情主界面\n"
        "     （那是另一个进程 hexin.exe，最大化它没有用）。\n"
        "     · 「精简模式」 —— 屏幕角落那个约 238x29 的下单小条就是。在小条上右键\n"
        "       或点还原按钮，切回完整的「网上股票交易系统5.0」窗口。精简模式下主窗口\n"
        "       被 ShowWindow(SW_HIDE) 整个藏起来，控件树无法操作\n"
        "     · 「最小化」   —— 还原一下即可\n"
        "     · 「窗口不存在」—— 还没登录交易，先登录",
    "Python 与客户端位数一致":
        "xiadan.exe 是 32 位而 qbg 是 64 位。读控件文本（如资金）能跨位数工作，但凡是\n"
        "     需要往目标进程注入远程内存的操作 —— 读 SysTreeView32 表项、取 grid 行 ——\n"
        "     跨位数就不可靠。pywinauto 自己也会警告。\n"
        "     ⚠ conda 已经没有 win-32 的 python 3.11（Anaconda 砍了 32 位 Windows），\n"
        "     `CONDA_FORCE_32BIT` 那条老办法**行不通**。要建 32 位环境只能：\n"
        "       1. 装 python.org 的 3.11 Windows **x86**(32-bit) 安装包\n"
        "       2. <32位python>\\python.exe -m venv .venv32\n"
        "       3. .venv32\\Scripts\\pip install easytrader",
    "xiadan.exe 已启动":
        r"先手动打开 D:\tonghuashun\同花顺\xiadan.exe 并**登录**，且不要进精简模式。",
    "easytrader / pywinauto 已装":
        "conda activate qbg && pip install easytrader",
    "验证码识别可用":
        "同花顺对**拷贝数据**本身有反爬：一取表就弹「检测到您正在拷贝数据……\n"
        "     请先输入验证码」。这个框是模态的，不认掉它，Ctrl+A/Ctrl+C 进不了 grid，\n"
        "     easytrader 于是读到陈旧剪贴板、**静默返回空列表**（不抛异常）。\n"
        "     所以这一项是硬阻塞，不是可选项。三条路：\n"
        "     A. 装 tesseract 二进制 + pip install pytesseract（easytrader 原生走这条）\n"
        "     B. pip install ddddocr，再把 easytrader.utils.captcha.captcha_recognize\n"
        "        换成 ddddocr —— 它就是为这类四位扭曲验证码做的，比 tesseract 准得多\n"
        "     C. 在同花顺客户端设置里关掉「拷贝数据需验证码」（若该版本有此开关，最省事）",
}


# 验证码处理与取表策略统一放在 qbg.portfolio.ths_client，探针只是它的调用方。
# 这两份代码曾经各存一份，但那意味着探针"测通了"不代表生产路径也通 ——
# 探针的全部价值就在于它跑的和日流程跑的是同一段代码。
CAPTCHA_DIALOG_HINT = "验证码"


def print_preflight(checks: list[tuple[str, bool, str]]) -> bool:
    print("\n=== 预检 ===")
    for name, ok, note in checks:
        print(f"  [{'OK ' if ok else 'FAIL'}] {name:<24} {note}")
    failed = [name for name, ok, _ in checks if not ok]
    if not failed:
        print("  全部通过。")
        return True
    print("\n--- 怎么修 ---")
    for name in failed:
        print(f"  * {name}: {HINTS.get(name, '见上方说明')}")
    return False


READ_ONLY_FIELDS = ("balance", "position", "today_entrusts", "today_trades")


def probe(exe: Path, client: str, grid: str) -> dict:
    import easytrader
    from easytrader import grid_strategies

    user = easytrader.use(client)
    user.grid_strategy = {
        # auto: 本文件自带的策略 —— 可见消歧 + ddddocr 认验证码 + 哨兵校验。
        #       easytrader 原生三个在本机都不可用，原因见 CaptchaAwareCopy 的注释。
        "auto": CaptchaAwareCopy,
        # 下面三个是 easytrader 原生的，留着做对照实验
        "xls": grid_strategies.Xls,
        "copy": grid_strategies.Copy,
        "wmcopy": grid_strategies.WMCopy,
    }[grid]
    user.connect(str(exe))
    bind_main_window(user)      # connect() 认主窗口不可靠，见 ths_client 里的说明

    result: dict[str, object] = {
        "probed_at": datetime.now().isoformat(timespec="seconds"),
        "client": client, "grid": grid, "exe": str(exe),
    }
    # 逐项独立 try：某一项挂掉不该让其他三项也没结果，
    # 因为「哪几项能用」正是这次探测要回答的问题。
    dialogs: list[str] = []
    for field in READ_ONLY_FIELDS:
        try:
            result[field] = getattr(user, field)
        except Exception as exc:  # noqa: BLE001 —— 探针要如实记录任意失败
            result[field] = {"__error__": f"{type(exc).__name__}: {exc}"}
        # 每取完一张表就清一次：模态框留着会卡住后面所有查询，
        # 而且会一直挂在客户端上等人去点。
        dialogs += cleanup_dialogs()
        columns = getattr(user.grid_strategy_instance, "last_columns", None)
        if columns:
            result.setdefault("__columns__", {})[field] = list(columns)

    # 收尾轮询：验证码框是**异步**弹的，实测能在最后一次查询结束好几秒后才冒出来
    # （同花顺像是按累计拷贝次数触发反爬，不是每次拷贝都弹）。
    # 扫一次不够，这里盯 6 秒；不兜住的话探针就会把模态框留在你的交易客户端上。
    for _ in range(12):
        time.sleep(0.5)
        dialogs += cleanup_dialogs()
    result["__dialogs__"] = dialogs
    return result


# qbg.portfolio.base.Position 需要的字段 → 同花顺表头
# 2026-08-21 实测的真实表头（同花顺 9.60.61「查询[F4] → 资金股票」，18 列）：
#   操作 序号 证券代码 证券名称 股票余额 可用余额 冻结数量 成本价 市价 盈亏
#   盈亏比例(%) 当日盈亏 当日盈亏比(%) 市值 仓位占比(%) 当日买入 当日卖出 交易市场
# 注意是「市值」不是「最新市值」—— 别照抄 easytrader 文档或别的券商版本的列名。
POSITION_FIELD_MAP = {
    "证券代码": "code",
    "证券名称": "name",
    "股票余额": "qty",
    "可用余额": "sellable_qty",     # T+1 闸要的就是它，截图 OCR 常常拿不到
    "成本价": "cost_price",
    "市价": "last_price",
    "市值": "market_value",
}


def summarize(data: dict) -> None:
    print("\n=== 只读查询结果 ===")
    empty: list[str] = []
    for field in READ_ONLY_FIELDS:
        value = data.get(field)
        if isinstance(value, dict) and "__error__" in value:
            print(f"\n[{field}] 失败: {value['__error__']}")
            continue
        rows = value if isinstance(value, list) else [value]
        print(f"\n[{field}] {len(rows)} 行")
        if rows and isinstance(rows[0], dict):
            print("  字段: " + "、".join(rows[0].keys()))
        elif field != "balance":
            empty.append(field)

    dialogs = data.get("__dialogs__") or []
    if dialogs:
        print(f"\n⚠ 取表过程中弹出并被关掉了 {len(dialogs)} 个模态框：")
        for item in dialogs:
            print(f"   - {item}")
        print("  同花顺对『拷贝数据』本身有反爬验证码；营销提示框也会不定时插进来。")
        print("  两者都是模态的，不清掉会卡住后面所有查询。")

    if empty:
        print("\n· 以下表返回 0 行：" + "、".join(empty))
        if data.get("grid") == "auto":
            # auto 策略过了哨兵校验才会返回，所以 0 行是**可信的空**。
            print("  auto 策略经过哨兵校验（确认剪贴板真被更新过）才返回，")
            print("  所以这里的 0 行是可信的「表确实空」，不是静默失败。")
        else:
            # easytrader 原生策略没有这层校验，0 行可能是假的。
            print("  ⚠ 你用的是 easytrader 原生策略，它取表失败时**不抛异常**、")
            print("  直接返回空列表（实测 Ctrl+A/Ctrl+C 被验证码框挡住时就是这样）。")
            print("  这里的 0 行不可信，换 --grid auto 再跑一次。")

    print("\n=== 对照 qbg 的 Position 需要的字段 ===")
    positions = data.get("position")
    have = set(positions[0]) if isinstance(positions, list) and positions else set()
    if not have:
        # 空仓时行是空的，但**表头还在** —— 字段映射靠表头就够了，
        # 不必等到账户里真有持仓。
        have = set((data.get("__columns__") or {}).get("position") or [])
        if have:
            print("  （账户空仓，下面用表头对照；有持仓后再跑一次可验证取值）")
    if not have:
        print("  持仓表头也没取到，无法对照。")
        return
    for cn, en in POSITION_FIELD_MAP.items():
        print(f"  [{'OK ' if cn in have else '缺 '}] {cn:<6} -> Position.{en}")
    extra = have - set(POSITION_FIELD_MAP)
    if extra:
        print("  客户端多给的字段: " + "、".join(sorted(extra)))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="同花顺客户端只读探针（不下单）")
    parser.add_argument("--exe", type=Path, default=Path(DEFAULT_EXE))
    parser.add_argument("--client", default=DEFAULT_CLIENT, choices=["universal_client", "ths"])
    parser.add_argument("--grid", default="auto",
                        choices=["auto", "xls", "copy", "wmcopy"],
                        help="auto=本文件自带的带验证码策略（默认）；其余三个是 easytrader 原生的")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--force", action="store_true", help="预检不过也继续")
    parser.add_argument("--save", action="store_true",
                        help="结果写 data/portfolio/probe_ths_*.json（含真实金额，已 gitignore）")
    args = parser.parse_args(argv)

    ok = print_preflight(preflight(args.exe))
    if args.preflight_only:
        return 0 if ok else 1
    if not ok and not args.force:
        print("\n预检未通过，已停止。确认要硬试就加 --force。")
        return 1

    try:
        data = probe(args.exe, args.client, args.grid)
    except Exception as exc:  # noqa: BLE001
        print(f"\n连接失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("客户端已登录、且没进精简模式吗？换 --grid copy 再试一次。", file=sys.stderr)
        return 2

    summarize(data)

    if args.save:
        out = REPO / "data" / "portfolio" / f"probe_ths_{datetime.now():%Y%m%d_%H%M%S}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                       encoding="utf-8")
        print(f"\n已写入 {out}（含真实账户金额，不要提交、不要外发）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
