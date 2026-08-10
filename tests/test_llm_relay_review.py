from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

from qbg import llm
from qbg.agent import review
from qbg.config import settings


def _configure_relay(monkeypatch):
    monkeypatch.setattr(settings, "qbg_llm_base_url", "https://relay.example/v1/")
    monkeypatch.setattr(settings, "qbg_llm_api_key", "test-key-not-a-secret")
    monkeypatch.setattr(settings, "qbg_llm_model", "gpt-4o")
    monkeypatch.setattr(settings, "qbg_llm_model_quick", "gpt-4o-mini")


def test_unified_relay_drives_vision_and_tradingagents(tmp_path, monkeypatch):
    _configure_relay(monkeypatch)
    monkeypatch.setattr(llm, "ENV_FILE", tmp_path / "missing.env")
    for key in (*llm.TA_PIN_KEYS, llm.RELAY_TA_KEY_ENV):
        monkeypatch.delenv(key, raising=False)

    relay = llm.resolve_relay()
    vision = llm.resolve_vision()
    env = llm.tradingagents_env(relay)

    assert relay.base_url == "https://relay.example/v1"
    assert vision.model == "gpt-4o-mini" and vision.api_key == relay.api_key
    assert env["TRADINGAGENTS_LLM_PROVIDER"] == "openai_compatible"
    assert env["TRADINGAGENTS_DEEP_THINK_LLM"] == "gpt-4o"
    assert env["TRADINGAGENTS_QUICK_THINK_LLM"] == "gpt-4o-mini"
    assert env["OPENAI_COMPATIBLE_API_KEY"] == relay.api_key


class _FakeCompletions:
    def __init__(self, payload):
        self.payload = payload
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=json.dumps(self.payload, ensure_ascii=False)))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150),
        )


class _FakeClient:
    def __init__(self, payload):
        self.completions = _FakeCompletions(payload)
        self.chat = SimpleNamespace(completions=self.completions)


def test_review_agent_writes_validated_report_and_hypothesis(tmp_path, monkeypatch):
    _configure_relay(monkeypatch)
    monkeypatch.setattr(settings, "qbg_agent_enabled", 1)
    monkeypatch.setattr(review, "assert_write_allowed", lambda path: None)
    client = _FakeClient({
        "status": "attention",
        "summary": "派生库缺少运行记录，需要先重建。",
        "evidence": ["facts.findings 包含 STORE_MISSING"],
        "hypotheses": [{
            "topic": "ETL",
            "statement": "完成回填后运行记录将出现",
            "discriminator": "回填后 runs 表记录数大于 0",
        }],
        "parameter_suggestions": [{
            "parameter": "qbg_top_k",
            "proposed_value": 4,
            "rationale": "仅作为测试，必须另行回测",
        }],
        "actions": ["运行派生库重建"],
    })
    db_path = tmp_path / "runs.db"
    result = review.daily_review("2026-08-10", client=client, db_path=db_path,
                                 report_dir=tmp_path / "review")

    assert result.ok and result.usage["total_tokens"] == 150
    assert result.hypotheses_opened == ["HYP-2026-08-10-01"]
    assert "八项回测闸" in (tmp_path / "review" / "2026-08-10.md").read_text(encoding="utf-8")
    assert client.completions.kwargs["response_format"] == {"type": "json_object"}
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
        assert db.execute("SELECT discriminator FROM hypotheses").fetchone()[0]


def test_review_agent_rejects_non_json_without_writing(tmp_path, monkeypatch):
    _configure_relay(monkeypatch)
    monkeypatch.setattr(settings, "qbg_agent_enabled", 1)
    monkeypatch.setattr(review, "assert_write_allowed", lambda path: None)
    client = _FakeClient({"status": "not-valid", "summary": "x"})
    result = review.daily_review("2026-08-10", client=client, db_path=tmp_path / "runs.db",
                                 report_dir=tmp_path / "review")
    assert not result.ok and "非法复盘状态" in (result.error or "")
    assert not (tmp_path / "review" / "2026-08-10.md").exists()
