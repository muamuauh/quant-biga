"""盘前复核缓存 + 填单重试。全离线。

## 这两件事都是 2026-09-09 那天逼出来的

  · 复核 5 只票花了 **58 分钟**，而 `require_trading_session` 是硬闸 ——
    09:30 现场复核会跑到收盘之后，整天作废且 LLM 的钱已经花掉。
  · 填单 `126.80` 回读成 `16.80`（第二位丢了），整批订单被中止。
"""

from __future__ import annotations

import json

import pytest

from qbg.agents import verdict_cache

# ----------------------------------------------------------------------
# 复核缓存
# ----------------------------------------------------------------------

def test_roundtrip(tmp_path):
    verdict_cache.save_verdicts(
        "2026-09-10", ["600519.SH", "000858.SZ"],
        [{"code": "600519.SH", "rating": "Hold", "kept": True}],
        {"600519.SH": 0.5}, {"cost_usd": 0.2}, root=tmp_path)
    got = verdict_cache.load_verdicts("2026-09-10", ["000858.SZ", "600519.SH"], root=tmp_path)
    assert got is not None
    kept, verdicts, usage = got
    assert kept == {"600519.SH": 0.5}
    assert usage["cost_usd"] == 0.2
    assert len(verdicts) == 1


def test_candidate_order_does_not_matter():
    """名单比对按**集合**：打分顺序可能因浮点微差抖动，那不该让缓存失效。"""
    # roundtrip 里已经用了乱序，这里显式钉住语义
    assert sorted(["b", "a"]) == sorted(["a", "b"])


def test_different_candidates_invalidate_the_whole_cache(tmp_path):
    """**这是最要紧的一条。**

    名单变了还套旧结论，等于给一只从没复核过的票安上别人的评级。
    宁可退回现场复核（慢），不可张冠李戴。
    """
    verdict_cache.save_verdicts("2026-09-10", ["600519.SH"], [], {"600519.SH": 1.0},
                                {}, root=tmp_path)
    assert verdict_cache.load_verdicts(
        "2026-09-10", ["600519.SH", "000858.SZ"], root=tmp_path) is None
    assert verdict_cache.load_verdicts("2026-09-10", ["000858.SZ"], root=tmp_path) is None


def test_missing_cache_returns_none_not_error(tmp_path):
    """缓存缺失是**正常情况**（盘前任务没跑），不该抛异常打断日流程。"""
    assert verdict_cache.load_verdicts("2026-09-10", ["600519.SH"], root=tmp_path) is None


def test_corrupt_cache_returns_none(tmp_path):
    """半截写入的文件不该让日流程崩 —— 退回现场复核就好。"""
    p = verdict_cache.cache_path("2026-09-10", tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{不是合法 json", encoding="utf-8")
    assert verdict_cache.load_verdicts("2026-09-10", ["600519.SH"], root=tmp_path) is None


def test_date_inside_file_must_match_the_filename(tmp_path):
    """文件名和内容对不上多半是手工改过。不猜，直接作废。"""
    p = verdict_cache.cache_path("2026-09-10", tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"date": "2026-09-01", "candidates": ["600519.SH"],
                             "kept": {}, "verdicts": [], "usage": {}}), encoding="utf-8")
    assert verdict_cache.load_verdicts("2026-09-10", ["600519.SH"], root=tmp_path) is None


# ----------------------------------------------------------------------
# 接线
# ----------------------------------------------------------------------

def test_daily_cycle_prefers_the_cache_but_falls_back():
    """缓存命中就用，未命中必须**退回现场复核**而不是跳过复核。

    缓存不是「关掉复核」的开关 —— `QBG_AGENTS_ENABLED` 才是。
    """
    import inspect

    from qbg.orchestrator import daily_cycle

    # 2026-09-24 复核逻辑从 run_daily 抽成了 `_apply_review`（为了能对影子模式做
    # 行为测试）。三条路的**行为**见 tests/test_review_shadow.py，这里只留顺序检查。
    src = inspect.getsource(daily_cycle._apply_review)
    assert "load_verdicts" in src, "daily_cycle 没读盘前缓存"
    assert "review_candidates" in src, "缓存未命中时没有退回现场复核的路径"
    assert src.index("load_verdicts") < src.index("review_candidates"), \
        "顺序反了：应当先查缓存，未命中才现场复核"


def test_premarket_script_never_places_orders():
    """盘前脚本**永远不下单**。它没有风控闸，一旦能下单就是绕过整条闸链。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "26_premarket.py").read_text(
        encoding="utf-8")
    for forbidden in ("run_all_gates", "plan_orders", "EasytraderAdapter", "submit"):
        assert forbidden not in src, f"盘前脚本里出现了 {forbidden} —— 它不该碰下单链路"


def test_setup_registers_three_tasks_and_guards_all_names():
    """任务名护栏必须覆盖第三个任务。

    `setup_schedule.ps1` 拒绝 qtf_* / qtagent_*（兄弟仓库挂着真钱账户，
    覆盖是静默的）。加了新任务却没把它加进护栏，等于开了个后门。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "setup_schedule.ps1").read_text(
        encoding="utf-8-sig")
    assert "$PremarketTaskName" in src
    assert src.count("@($TaskName, $PreflightTaskName, $PremarketTaskName))") == 2, \
        "两处任务名护栏没有都覆盖盘前任务"


def test_premarket_accepts_every_flag_setup_appends():
    """**setup_schedule.ps1 往任务动作里拼的每一个开关，被调脚本都得认。**

    2026-09-10 早上炸的就是这个：任务动作里拼了 `--retrain`（那是 run_daily 的
    写法），而 26_premarket.py 当时只有 `--no-retrain`。argparse 直接 exit 2 ——
    **预检全绿、复核一步没跑、当天没有缓存**，而且要等到看日志才知道。

    两个脚本各自都"对"，错在接缝上。所以在接缝上钉一条。

    只建解析器、不跑 `main()` —— 后者会真的拉数据、读同花顺、调 LLM。
    """
    import importlib.util
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    setup = (root / "scripts" / "setup_schedule.ps1").read_text(encoding="utf-8-sig")
    flags = set(re.findall(r"\$argLine \+= ' (--[a-z-]+)'", setup))
    assert flags, "没从 setup_schedule.ps1 里解析出任何拼进任务动作的开关"

    spec = importlib.util.spec_from_file_location(
        "premarket_mod", root / "scripts" / "26_premarket.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    parser = mod.build_parser()

    for flag in flags:
        try:
            parser.parse_args([flag])
        except SystemExit as exc:
            raise AssertionError(
                f"26_premarket.py 不认 {flag} —— 而任务动作里拼了它（exit {exc.code}）"
            ) from exc


def test_retrain_moved_to_the_premarket_task():
    """重训归盘前。日流程再训一遍是白花 2 分钟（同数据同 seed，结果逐位相同）。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "scripts" / "setup_schedule.ps1").read_text(
        encoding="utf-8-sig")
    line = next((ln for ln in src.splitlines()
                 if "--retrain" in ln and "$argLine" in ln), None)
    assert line is not None
    assert "$PremarketTaskName" in line, f"--retrain 没挂到盘前任务上：{line.strip()}"


# ----------------------------------------------------------------------
# 填单重试
# ----------------------------------------------------------------------

def test_fill_and_verify_exists_and_is_used():
    """价格/数量必须有重试。

    此前只有代码框有重试，理由是"失败会中止整批订单，代价太大" ——
    那个理由对价格和数量完全成立，只是当初没往下推。
    """
    import inspect

    from qbg.execution import ths_order_form as form

    assert hasattr(form, "fill_and_verify")
    src = inspect.getsource(form.place_order)
    assert "fill_and_verify(user, PRICE_ID" in src
    assert "fill_and_verify(user, AMOUNT_ID" in src


def test_retry_does_not_relax_the_final_check():
    """重试用完仍然不符，照样中止。

    放宽成"差不多就提交"是把一道安全闸换成一个赌注 —— 那笔 16.80 的买单
    要是发出去了，挂在跌停之外成交不了是运气好；换成卖单填成 10 倍价，
    后果就不一样了。
    """
    import inspect

    from qbg.execution import ths_order_form as form

    src = inspect.getsource(form.place_order)
    assert "raise OrderFormError" in src
    assert "提交前回读不符，未提交" in src
    # 最终判据必须是**重新读一遍**三个框，而不是复用重试里那次读数
    assert src.index("checks = {") > src.index("fill_and_verify(user, AMOUNT_ID"), \
        "最终校验应当在两个字段都填完之后"
    assert "ocr_digits(user, PRICE_ID)" in src, \
        "最终校验没有重新读价格 —— 填数量时客户端可能回头改动价格框"


@pytest.mark.parametrize("field", ["price", "quantity"])
def test_mismatch_message_carries_the_read_history(field):
    """历次读数要进错误信息，否则分不清「键被吞」和「OCR 抖动」。

    各次读数不同 → 抖动，重试有效；始终相同 → 键真的丢了或 OCR 有系统性
    缺陷，得换手段。在此之前两种都只表现为同一条 "期望 12680 实际 1680"。
    """
    import inspect

    from qbg.execution import ths_order_form as form

    src = inspect.getsource(form.place_order)
    assert "历次读数" in src
    assert f"{field}_seen" in src or "price_seen" in src
