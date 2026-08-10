"""向 vendored TradingAgents 注册 qbg 的 ashare vendor；可重复执行。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "TradingAgents" / "tradingagents"
MARKER = "# [qbg ashare vendor]"


def main() -> int:
    interface = ROOT / "dataflows" / "interface.py"
    config = ROOT / "default_config.py"
    if not interface.exists() or not config.exists():
        print("TradingAgents 未 clone")
        return 1
    text = interface.read_text(encoding="utf-8")
    if MARKER not in text:
        anchor = "import logging"
        if anchor not in text:
            raise RuntimeError("TradingAgents 接口已变化，找不到 import logging 锚点")
        text = text.replace('VENDOR_LIST = [', 'VENDOR_LIST = [\n    "ashare",')
        mapping = {
            '"get_stock_data": {': '"get_stock_data": {\n        "ashare": ashare_vendor.get_stock_data,',
            '"get_indicators": {': '"get_indicators": {\n        "ashare": ashare_vendor.get_indicators,',
            '"get_fundamentals": {': (
                '"get_fundamentals": {\n        "ashare": ashare_vendor.get_fundamentals,'),
            '"get_balance_sheet": {': (
                '"get_balance_sheet": {\n        "ashare": ashare_vendor.get_balance_sheet,'),
            '"get_cashflow": {': '"get_cashflow": {\n        "ashare": ashare_vendor.get_cashflow,',
            '"get_income_statement": {': (
                '"get_income_statement": {\n        "ashare": ashare_vendor.get_income_statement,'),
            '"get_news": {': '"get_news": {\n        "ashare": ashare_vendor.get_news,',
            '"get_global_news": {': '"get_global_news": {\n        "ashare": ashare_vendor.get_global_news,',
            '"get_insider_transactions": {': ('"get_insider_transactions": {\n'
                                                '        "ashare": ashare_vendor.get_insider_transactions,'),
        }
        for old in mapping:
            if old not in text:
                raise RuntimeError(f"TradingAgents 接口已变化，找不到 {old}")
        text = text.replace(anchor, f"{anchor}\n\n{MARKER}\nfrom qbg.agents import ashare_vendor", 1)
        for old, new in mapping.items():
            text = text.replace(old, new, 1)
        interface.write_text(text, encoding="utf-8")
        print("已注册 ashare vendor")
    else:
        print("ashare vendor 已注册，跳过")
    cfg = config.read_text(encoding="utf-8")
    for category in ("core_stock_apis", "technical_indicators", "fundamental_data", "news_data"):
        cfg = cfg.replace(f'"{category}": "yfinance"', f'"{category}": "ashare"')
    config.write_text(cfg, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
