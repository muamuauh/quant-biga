"""A股交易规则：板块识别、涨跌停、最小交易单位。

**这是本项目相对两个美股参考仓库的核心增量。** 这里每一条写错，要么下出
根本不可能成交的单，要么让回测收益凭空多出来一截。

## 涨跌停

    主板（沪 600/601/603/605，深 000/001/002/003）  ±10%
    创业板（300/301）、科创板（688/689）             ±20%
    北交所（430/83x/87x/88x/920）                    ±30%
    ST / *ST：主板 ±5%，创业板/科创板仍是 ±20%

涨跌停价 = `round(prev_close × (1 ± pct), 2)`，四舍五入到分。

**新股例外**：上市首日及前几日规则特殊（创业板/科创板前 5 日不设涨跌幅）。
本项目用 `QBG_MIN_LIST_DAYS=60` 直接过滤次新股，一次性规避这整类复杂性——
所以这里不处理新股，也不该有人来这里加。

## 最小交易单位

买入必须是 100 股（一手）的整数倍。卖出可以有零股，但**零股必须一次性
全部卖出**（不能拆）。

对 10 万账户的直接后果：`top_k=3` → 单槽预算约 3 万 → 一手买得起的上限
是 300 元/股。这个约束必须体现在选股的可负担性过滤里（P3），否则规划器
会静默地不下单，槽位空着变成现金拖累。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from qbg.market import codes

# 一手 = 100 股。买入必须是它的整数倍。
LOT_SIZE = 100

# 科创板买入最少 200 股，之后可按 1 股递增。
# TODO: 沪深300 里科创板票很少，本期按 100 处理。将来若换成全市场股票池，
#       这里要按板块返回不同的最小买入量。
STAR_MIN_BUY = 200

# 最小价格变动单位（股票）。ETF 是 0.001，本项目不碰 ETF。
TICK = Decimal("0.01")

# 板块 → 涨跌幅（非 ST）
_MAIN = "主板"
_CHINEXT = "创业板"
_STAR = "科创板"
_BSE = "北交所"

_LIMIT_PCT: dict[str, float] = {
    _MAIN: 0.10,
    _CHINEXT: 0.20,
    _STAR: 0.20,
    _BSE: 0.30,
}

# ST 股的涨跌幅。**只有主板收窄到 5%**——创业板/科创板的 ST 股仍是 20%，
# 这是个常见的误解，写错会让创业板 ST 股的限价被夹到根本不该有的窄区间里。
_ST_LIMIT_PCT: dict[str, float] = {
    _MAIN: 0.05,
    _CHINEXT: 0.20,
    _STAR: 0.20,
    _BSE: 0.30,
}

_SH_MAIN = ("600", "601", "603", "605")
_SZ_MAIN = ("000", "001", "002", "003")
_CHINEXT_PREFIX = ("300", "301")
_STAR_PREFIX = ("688", "689")


def board_of(code: str) -> str:
    """代码 → 板块名（`'主板'` / `'创业板'` / `'科创板'` / `'北交所'`）。"""
    d = codes.digits(code)
    if d.startswith(_STAR_PREFIX):
        return _STAR
    if d.startswith(_CHINEXT_PREFIX):
        return _CHINEXT
    if d.startswith(_SH_MAIN) or d.startswith(_SZ_MAIN):
        return _MAIN
    # exchange_of 已经保证代码合法，走到这里只可能是北交所。
    return _BSE


def limit_pct(code: str, is_st: bool = False) -> float:
    """该票的单日涨跌幅限制（小数）。"""
    board = board_of(code)
    table = _ST_LIMIT_PCT if is_st else _LIMIT_PCT
    return table[board]


def round_price(value: float) -> float:
    """按最小变动单位取整到分，用**四舍五入**（交易所口径）。

    不能用 Python 的 `round()`：它是银行家舍入，`round(2.675, 2)` 给 2.67。
    涨跌停价差一分钱就是"这个限价根本无法成交"，所以走 Decimal。
    """
    return float(Decimal(str(value)).quantize(TICK, rounding=ROUND_HALF_UP))


def price_limits(code: str, prev_close: float, is_st: bool = False) -> tuple[float, float]:
    """返回 `(跌停价, 涨停价)`。

    `prev_close` 必须是**前收盘价**（不复权，交易所口径）。用复权价算出来的
    涨跌停会和真实盘口对不上——这是个很隐蔽的错误，因为数值看起来总是"合理"的。
    """
    if prev_close is None or prev_close <= 0:
        raise ValueError(f"prev_close 必须为正，收到 {prev_close!r}")
    pct = limit_pct(code, is_st)
    return round_price(prev_close * (1 - pct)), round_price(prev_close * (1 + pct))


def clamp_to_limits(price: float, code: str, prev_close: float,
                    is_st: bool = False) -> float:
    """把限价夹到涨跌停区间内。

    下单清单里的限价会在参考价上加/减一个让价幅度以提高成交概率，
    但让出去的价格可能越过涨跌停——交易所会直接废单。
    """
    lo, hi = price_limits(code, prev_close, is_st)
    return min(max(round_price(price), lo), hi)


def at_limit_up(price: float, code: str, prev_close: float,
                is_st: bool = False) -> bool:
    """当前价是否已封涨停。**涨停时买不进**，风控闸据此砍掉 BUY。"""
    _, hi = price_limits(code, prev_close, is_st)
    return round_price(price) >= hi


def at_limit_down(price: float, code: str, prev_close: float,
                  is_st: bool = False) -> bool:
    """当前价是否已封跌停。**跌停时卖不出**，清单里要标注"可能不成交"。"""
    lo, _ = price_limits(code, prev_close, is_st)
    return round_price(price) <= lo


# ----------------------------------------------------------------------
# 最小交易单位
# ----------------------------------------------------------------------


def round_lot_down(shares: float) -> int:
    """向下取整到整手。买入用这个——多买一手可能就超预算了。"""
    if shares is None or shares <= 0:
        return 0
    return int(shares // LOT_SIZE) * LOT_SIZE


def affordable_shares(budget: float, price: float) -> int:
    """给定预算和价格，最多能买多少股（整手）。

    买不起一手就返回 0。调用方**必须**处理这个 0：静默下一个 0 股的单
    等于槽位空着，钱躺在现金里拖累收益。选股阶段的可负担性过滤就是为了
    让这种票根本不进候选。
    """
    if budget is None or price is None or budget <= 0 or price <= 0:
        return 0
    return round_lot_down(budget / price)


def is_valid_buy_qty(shares: int) -> bool:
    """买入数量是否合法：正数且整手。"""
    return isinstance(shares, int) and shares > 0 and shares % LOT_SIZE == 0


def normalize_sell_qty(shares: int, holding: int) -> int:
    """把卖出数量规整成交易所能接受的量。

    规则：整手部分随便卖，但**零股必须一次性清完**。所以如果想卖的数量
    会在账上留下不足一手的余数，就把整个持仓一起卖掉——留一个 37 股的
    尾巴，下次要清它还得单独下一笔单，白付一次最低 5 元佣金。
    """
    if holding <= 0 or shares <= 0:
        return 0
    shares = int(min(shares, holding))
    remainder = holding - shares
    if 0 < remainder < LOT_SIZE:
        return holding
    if shares % LOT_SIZE != 0 and shares != holding:
        # 想卖的量本身不是整手，且不是清仓 → 向下取整到整手
        return round_lot_down(shares)
    return shares


def min_buy_shares(code: str) -> int:
    """该票买入的最小股数。见 STAR_MIN_BUY 的 TODO。"""
    return LOT_SIZE
