"""P0 骨架的冒烟测试：配置能读、路径自洽、默认值符合 10 万账户的设计前提。

全部离线——不联网、不碰券商、不调 LLM。这是本项目所有测试的铁律。
"""

from __future__ import annotations

from pathlib import Path

from qbg.config import PROJECT_ROOT, Settings, load_universe, settings


def test_project_root_is_repo_root():
    """PROJECT_ROOT 应指向仓库根，而不是 src/ 或包目录。"""
    assert (PROJECT_ROOT / "pyproject.toml").exists()
    assert (PROJECT_ROOT / "src" / "qbg" / "config.py").exists()


def test_paths_are_under_project_root():
    """所有派生路径都应落在仓库内，避免误写到别处（尤其是兄弟仓库）。"""
    for p in (
        settings.parquet_dir,
        settings.qlib_provider_uri,
        settings.snapshot_dir,
        settings.portfolio_dir,
        settings.report_dir,
        settings.log_dir,
        settings.db_path,
    ):
        assert isinstance(p, Path)
        assert PROJECT_ROOT in p.parents or p == PROJECT_ROOT


def test_default_mode_is_advisory():
    """默认必须是只出清单的顾问模式，绝不能默认碰实盘。"""
    assert settings.qbg_mode.upper() == "ADVISORY"
    assert settings.i_confirm_real == 0
    assert settings.is_live is False


def test_is_live_needs_both_env_locks():
    """.env 的两把锁必须同时开；第三把锁在 risk_limits.yaml，不由 config 判断。"""
    live_only = settings.model_copy(update={"qbg_mode": "LIVE", "i_confirm_real": 0})
    confirm_only = settings.model_copy(update={"qbg_mode": "ADVISORY", "i_confirm_real": 1})
    both = settings.model_copy(update={"qbg_mode": "LIVE", "i_confirm_real": 1})

    assert live_only.is_live is False
    assert confirm_only.is_live is False
    assert both.is_live is True


def test_data_sources_parsed_in_order():
    """降级链顺序有意义：baostock 是唯一不靠爬网页的源，必须排第一。"""
    sources = settings.data_sources
    assert sources[0] == "baostock"
    assert "akshare" in sources


def test_small_account_defaults():
    """10 万账户的设计前提：k 小、调仓慢、有迟滞。

    这些不是随便的默认值——调低 rebalance_every_days 或调高 top_k 之前
    先读 config.py 里的理由（成本会吃掉 alpha）。
    """
    assert settings.qbg_top_k == 3
    assert settings.qbg_rebalance_every_days >= 5
    assert settings.qbg_keep_rank > settings.qbg_top_k


def test_agents_default_off():
    """新安装默认关闭；本机 .env 可在在线验收后显式打开复核闸。"""
    defaults = Settings(_env_file=None)
    assert defaults.qbg_agents_enabled == 0
    assert defaults.qbg_agent_enabled == 0
    assert defaults.qbg_agent_autoapply == 0


def test_load_universe_missing_file_returns_empty(tmp_path, monkeypatch):
    """P1 之前 universe 文件不存在，应返回空列表而不是抛 FileNotFoundError。"""
    monkeypatch.setattr(settings, "universe_file", tmp_path / "nope.txt")
    assert load_universe() == []


def test_load_universe_skips_comments_and_blanks(tmp_path, monkeypatch):
    f = tmp_path / "universe.txt"
    f.write_text("# 沪深300\n600519.SH\n\n  000858.SZ  \n# 注释\n", encoding="utf-8")
    monkeypatch.setattr(settings, "universe_file", f)
    assert load_universe() == ["600519.SH", "000858.SZ"]
