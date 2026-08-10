from __future__ import annotations

from types import SimpleNamespace

from qbg.agents import ashare_vendor
from qbg.agents.review import _propagate_with_retry, rating_rank
from qbg.agents.ticker_map import from_ta_symbol, to_ta_symbol
from qbg.agents.token_tracker import TokenTracker
from qbg.data import cache, meta
from tests.test_sources_chain import make_bars


def test_ticker_mapping_round_trip():
    assert to_ta_symbol("600519.SH") == "600519"
    assert from_ta_symbol("600519") == "600519.SH"


def test_instrument_identity_uses_local_name(tmp_path, monkeypatch):
    meta.save_cached({"600519.SH": "贵州茅台"}, tmp_path)
    monkeypatch.setattr(meta.settings, "snapshot_dir", tmp_path)
    identity = ashare_vendor.resolve_instrument_identity("600519")
    assert identity == {"exchange": "SSE", "quote_type": "EQUITY", "company_name": "贵州茅台"}


def test_ashare_stock_data_reads_local_cache(tmp_path, monkeypatch):
    frame = make_bars(["2026-08-03", "2026-08-04"])
    frame["source"] = "baostock"
    cache.write("600519.SH", frame, tmp_path)
    monkeypatch.setattr(cache.settings, "parquet_dir", tmp_path)
    text = ashare_vendor.get_stock_data("600519", "2026-08-03", "2026-08-04")
    assert "本地同口径行情" in text and "2026-08-04" in text


def test_no_data_is_explicit_and_forbids_invention(tmp_path, monkeypatch):
    monkeypatch.setattr(cache.settings, "parquet_dir", tmp_path)
    text = ashare_vendor.get_stock_data("600519", "2026-08-03", "2026-08-04")
    assert "无数据" in text and "禁止推测或编造" in text
    assert "无数据" in ashare_vendor.get_global_news("2026-08-04")


def test_verified_snapshot_uses_local_cache(tmp_path, monkeypatch):
    frame = make_bars([f"2026-07-{day:02d}" for day in range(1, 31)])
    frame["source"] = "baostock"
    cache.write("600519.SH", frame, tmp_path)
    monkeypatch.setattr(cache.settings, "parquet_dir", tmp_path)
    text = ashare_vendor.get_verified_market_snapshot("600519", "2026-07-30")
    assert "本地后复权确定性快照" in text and '"sma20"' in text


def test_five_tier_rating_order():
    assert [rating_rank(x) for x in ("Buy", "Overweight", "Hold", "Underweight", "Sell")] == list(range(5))
    assert rating_rank("unknown") == 5


def test_token_tracker_counts_and_prices():
    tracker = TokenTracker()
    response = SimpleNamespace(llm_output={"model_name": "deepseek-chat", "token_usage": {
        "prompt_tokens": 1000, "completion_tokens": 500,
        "prompt_tokens_details": {"cached_tokens": 200}}})
    tracker.on_llm_end(response)
    summary = tracker.summary()
    assert summary["calls"] == 1 and summary["total_tokens"] == 1500
    assert summary["cost_usd"] > 0


def test_review_retries_rate_limit_then_succeeds(monkeypatch):
    class Graph:
        calls = 0

        def propagate(self, ticker, trade_date):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("HTTP 429 rate limit")
            return {"final_trade_decision": "ok"}, "Hold"

    waits = []
    monkeypatch.setattr("time.sleep", waits.append)
    graph = Graph()
    assert _propagate_with_retry(graph, "600519", "2026-08-10", base_backoff=0.1)[1] == "Hold"
    assert graph.calls == 3
    assert waits == [0.1, 0.2]


def test_review_does_not_retry_non_rate_limit(monkeypatch):
    class Graph:
        def propagate(self, ticker, trade_date):
            raise RuntimeError("invalid response")

    monkeypatch.setattr(
        "time.sleep",
        lambda seconds: (_ for _ in ()).throw(AssertionError("unexpected retry")),
    )
    import pytest

    with pytest.raises(RuntimeError, match="invalid response"):
        _propagate_with_retry(Graph(), "600519", "2026-08-10")
