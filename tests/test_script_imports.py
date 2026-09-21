"""脚本的 import 不许排在 `if __name__ == "__main__"` 后面。

2026-09-21 早上 09:30 的日流程整个失败（`CalledProcessError`，当天没有日报、
没有下单）。根因是前一天给 12 个脚本装 `make_output_safe()` 的补丁，正则里
`(?:\\n\\s+.*)*` 的 `\\s` 包含换行 —— "续行"模式把整个文件吞掉，import 被插到了
**文件末尾**，也就是 `if __name__ == "__main__": raise SystemExit(main())` 之后。
于是 `main()` 先跑，`make_output_safe()` 抛 `NameError`。
4 个脚本中招，其中 `01_ingest.py` 是盘前和日流程用 subprocess 调的那个。

**当时 841 条测试全绿。** 昨天写的那两条守卫测试是纯文本匹配 ——
「字符串在不在」「在不在第一个 print 之前」，从没真正执行过脚本。
而单纯 import 也抓不到：模块级那行 import 本身执行得好好的，
只是 `main()` 已经先跑完了。

所以这里用 AST 判一条硬规则：**顶层 import 必须全部排在 `__main__` 守卫之前。**
这条比"检查某个具体名字"通用 —— 任何"import 被塞到文件末尾"都会被抓住。

**ruff 的 F821 补不上这个缺口**（它本来就开着）：它抓得到"根本没 import"，
但抓不到"import 在文件末尾" —— 对 linter 来说模块作用域里那个名字就是有定义的，
它不管顺序。只有 `__main__` 守卫让 `main()` 提前跑这件事，才把顺序变成了运行时问题。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = sorted((ROOT / "scripts").rglob("*.py"))


def _main_guard_line(tree: ast.Module) -> int | None:
    """`if __name__ == "__main__":` 那一行的行号。"""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                and test.left.id == "__name__"):
            return node.lineno
    return None


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_imports_come_before_the_main_guard(path: pathlib.Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guard = _main_guard_line(tree)
    if guard is None:
        return

    late = [f"第 {node.lineno} 行" for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom)) and node.lineno > guard]
    assert not late, (
        f"{path.name}：{'、'.join(late)} 的 import 排在 `if __name__` 之后（第 {guard} 行）。"
        "`main()` 在守卫里就跑掉了，那行 import 永远执行不到 —— "
        "脚本会在用到那个名字的瞬间抛 NameError。")


# ----------------------------------------------------------------------
# 无人值守跑的那几个：真的启动一次
# ----------------------------------------------------------------------

# 上面那条 AST 规则只挡「import 放错位置」这一类。**真正启动一次**才挡得住
# 任意的启动期崩溃（模块级笔误、import 路径写错、循环 import…）。
#
# 只覆盖自动化链路，不是全部 35 个脚本：全量要 36 秒，而手工跑的实验脚本
# 崩了当场就看见 traceback。**无人值守的这几个不一样 —— 崩一个就是一整天**：
# 2026-09-21 `01_ingest.py` 的 NameError 让日流程直接 CalledProcessError，
# 当天没有日报、没有下单，而唯一的痕迹是一封失败邮件。
AUTOMATED = [
    ["-m", "qbg.orchestrator.daily_cycle"],   # run_daily.ps1（09:30）
    ["scripts/26_premarket.py"],              # run_premarket.ps1（08:00）
    ["scripts/00_market_check.py"],           # 两个 .ps1 都用它判交易日
    ["scripts/01_ingest.py"],                 # daily_cycle 和盘前都 subprocess 调
    ["scripts/email_listener.py"],            # daily_cycle 拉起的常驻监听
]


@pytest.mark.parametrize("argv", AUTOMATED, ids=lambda a: a[-1])
def test_automated_entrypoints_start(argv):
    """`--help` 走完 import + 建解析器就退出，不碰网络、券商和 LLM。"""
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, *argv, "--help"], cwd=ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert "Traceback" not in (done.stdout + done.stderr), (
        f"{argv[-1]} 启动就崩了 —— 它跑在无人值守的链路上，"
        f"崩了就是一整天没有日报也没有下单：\n{done.stderr[-1500:]}")
    assert done.returncode == 0, f"{argv[-1]} --help 退出码 {done.returncode}"
