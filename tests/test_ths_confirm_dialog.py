"""委托确认框的解析与核对 —— 用 2026-08-21 实测抓到的原文。

这道校验比提交前的 OCR 预检强得多：OCR 是**猜**控件里有什么，
委托确认框是**客户端自己声明**它即将提交什么。所以点「是」之前必须核对它。
"""

from __future__ import annotations

from qbg.execution.ths_order_form import check_confirm, parse_confirm

# 实测原文（含 HTML 标签，客户端就是这么给的）
REAL = (
    "<font color=0x888888>资金帐号：</font>模拟炒股-****\n"
    "<font color=0x888888>证券代码：</font>600519(贵州茅台)\n"
    "<font color=0x888888>买入价格：</font><font  color=0x323232>1209.260</font>\n"
    "<font color=0x888888>买入数量：</font>100\n"
    "<font color=0x888888>预估金额：</font>120962.278\n\n"
    "您是否确定以上买入委托？"
)

EXPECT = {"code": "600519", "price": 1209.26, "quantity": 100}


def test_parses_real_dialog():
    got = parse_confirm(REAL)
    assert got["code"] == "600519"
    assert got["quantity"] == 100
    # 客户端显示 3 位小数，我们发的是 2 位
    assert abs(got["price"] - 1209.26) < 1e-6


def test_matching_order_passes():
    assert check_confirm(REAL, EXPECT) is None


def test_price_display_precision_is_tolerated():
    """客户端显示 1209.260，我们发 1209.26 —— 不能因为这个就中止。"""
    assert check_confirm(REAL, {**EXPECT, "price": 1209.26}) is None


def test_wrong_code_is_caught():
    problem = check_confirm(REAL, {**EXPECT, "code": "000001"})
    assert problem and "代码" in problem


def test_wrong_quantity_is_caught():
    """数量对不上是最该拦的一类 —— 清空失败会让数量变成拼接后的数字。"""
    problem = check_confirm(REAL, {**EXPECT, "quantity": 200})
    assert problem and "数量" in problem


def test_wrong_price_is_caught():
    problem = check_confirm(REAL, {**EXPECT, "price": 1272.90})
    assert problem and "价格" in problem


def test_appended_price_is_caught():
    """`^a` 清空失败时价格会变成 `1272.901209.26` 这类拼接值。"""
    broken = REAL.replace("1209.260", "1272.901209")
    problem = check_confirm(broken, EXPECT)
    assert problem and "价格" in problem


def test_sell_dialog_wording_also_parses():
    """卖出页的文案是「卖出价格/卖出数量」。"""
    sell = REAL.replace("买入", "卖出")
    assert check_confirm(sell, EXPECT) is None


def test_unparseable_text_is_refused_not_passed():
    """读不出内容时必须报不符，**不能**当成通过 —— 那等于没有校验。"""
    problem = check_confirm("某个我们没见过的提示", EXPECT)
    assert problem and "读不出" in problem


def test_missing_field_is_reported():
    partial = "证券代码：600519(贵州茅台)\n您是否确定以上买入委托？"
    problem = check_confirm(partial, EXPECT)
    assert problem and "缺字段" in problem
