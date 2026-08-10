"""项目规范代码与 TradingAgents symbol 的转换。"""

from qbg.market import codes


def to_ta_symbol(code: str) -> str:
    """`600519.SH` → `600519`；交易所由 ashare vendor 按前缀判定。"""
    return codes.digits(code)


def from_ta_symbol(symbol: str) -> str:
    """裸六位码、qlib 码或规范码 → 项目规范代码。"""
    return codes.normalize(symbol)

