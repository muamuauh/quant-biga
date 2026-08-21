"""弹窗按钮选择的离线测试 —— 用 2026-08-21 实测到的真实弹窗结构。

这层是安全关键的：选错按钮不是「操作失败」，而是**执行了另一个动作**。
撤单确认框里的「改单」意思是「撤单并以新的价格委托」，误点会下出一张新单。
"""

from __future__ import annotations

from qbg.portfolio.ths_client import _clean_label, find_dialog_button

# ---- 实测弹窗结构：(control_id, class_name, text, visible) ----

# 提交订单后的第一个提示框。注意 id=1 是「立即重启」而不是「确定」。
SUBMIT_TIP = [
    (2, "Button", "确定", True),
    (1, "Button", "立即重启", False),
    (1008, "Button", "查询当前委托", True),
    (1365, "Static", "提示", True),
    (1009, "Button", "", True),
    (2392, "Button", "点击此处不再提示", True),
    (2393, "Static", "现有成交价格预警服务，", False),
    (5200, "Button", "前往普通下单", False),
]

# 撤单确认框。id=1754「改单」= 撤单并以新的价格委托，绝不能碰。
CANCEL_CONFIRM = [
    (6, "Button", "是(&Y)", True),
    (7, "Button", "否(&N)", True),
    (1754, "Button", "改单", True),
    (1365, "Static", "撤单确认", True),
    (1008, "Button", "", True),
    (1041, "Static", "(撤单并以新的价格委托)", True),
]

# 反爬验证码框。这里 id=1 才是「确定」—— 和 SUBMIT_TIP 正好相反。
CAPTCHA = [
    (1, "Button", "确定", True),
    (2, "Button", "取消", True),
    (2393, "Static", "检测到您正在拷贝数据，为保护您的账号数据安全，请", True),
    (2394, "Static", "先输入验证码：", True),
]

# 价格格式警告：「委托价格的小数部分应为 2 位，是否继续？」
PRICE_WARNING = [
    (1040, "Static", "委托价格的小数部分应为 2 位，是否继续？", True),
    (6, "Button", "是(&Y)", True),
    (7, "Button", "否(&N)", True),
    (1365, "Static", "提示信息", True),
    (130, "Button", "前往普通下单", False),
]


def test_clean_label_strips_mnemonic():
    """按钮文本是 `是(&Y)`，按 '是' 精确匹配会落空 —— 第一轮探测就栽在这。"""
    assert _clean_label("是(&Y)") == "是"
    assert _clean_label("否(&N)") == "否"
    assert _clean_label("确定") == "确定"
    assert _clean_label("全撤(Z /)") == "全撤"


def test_submit_tip_picks_visible_confirm_not_restart():
    """id=1 是不可见的「立即重启」，正确答案是 id=2「确定」。

    探测脚本按 id 猜，点中了「立即重启」，只因它 visible=False 才没出事。
    """
    assert find_dialog_button(SUBMIT_TIP) == 2


def test_cancel_confirm_picks_yes_not_modify():
    """撤单确认框选「是」，绝不能选「改单」—— 那会下出一张新单。"""
    assert find_dialog_button(CANCEL_CONFIRM) == 6


def test_modify_button_is_blacklisted():
    """就算「是」不在了，也不许退而求其次点「改单」。"""
    without_yes = [row for row in CANCEL_CONFIRM if row[0] != 6]
    assert find_dialog_button(without_yes) is None


def test_captcha_dialog_id_is_opposite_of_submit_tip():
    """同一个 id 在不同弹窗里语义相反 —— 这正是不能按 id 认按钮的理由。"""
    assert find_dialog_button(CAPTCHA) == 1
    assert find_dialog_button(SUBMIT_TIP) == 2


def test_invisible_buttons_are_ignored():
    hidden_only = [(2, "Button", "确定", False)]
    assert find_dialog_button(hidden_only) is None


def test_dangerous_labels_never_selected():
    for label in ("改单", "立即重启", "前往普通下单", "点击此处不再提示"):
        assert find_dialog_button([(99, "Button", label, True)]) is None, label


def test_price_warning_yes_is_findable_but_caller_should_abort():
    """「是」找得到 —— 但价格格式警告应当由调用方判定为致命，而不是点「是」继续。

    这个测试锁住的是**能力**；用不用是 adapter 的决策，见 P9b。
    """
    assert find_dialog_button(PRICE_WARNING) == 6


def test_static_controls_are_not_buttons():
    assert find_dialog_button([(1365, "Static", "确定", True)]) is None
