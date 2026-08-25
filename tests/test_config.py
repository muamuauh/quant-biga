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
    """默认必须是只出清单的顾问模式，绝不能默认碰实盘。

    **测代码默认值，不测全局 `settings`**（理由同 test_small_account_defaults）：
    部署的 `.env` 里 QBG_MODE 是使用者的选择，改成 PAPER 天经地义，
    不该让测试变红。这条测试守的是**代码契约** —— 一个没有 `.env` 的新环境
    起来必须是 ADVISORY，绝不能默认就能下单。
    """
    defaults = Settings(_env_file=None)
    assert defaults.qbg_mode.upper() == "ADVISORY"
    assert defaults.i_confirm_real == 0
    assert defaults.is_live is False


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

    这些不是随便的默认值 —— 调低 rebalance_every_days 或调高 top_k 之前
    先读 config.py 里的理由（成本会吃掉 alpha）。

    **必须用 `_env_file=None` 测代码默认值，不能测全局 `settings`。**
    全局 settings 会读部署用的 `.env`，那是使用者的选择，不是代码的契约。
    拿它当断言对象，测试就会因为别人合理的配置而变红 —— 而一个因部署选择
    变红的测试，只会逼人去改测试或长期忍受红灯，两条路都让它失去意义。
    调仓频率是否合适由日报的「今日成本」段和回测来回答，不由单元测试把关。
    """
    defaults = Settings(_env_file=None)
    assert defaults.qbg_top_k == 3
    assert defaults.qbg_rebalance_every_days >= 5
    assert defaults.qbg_keep_rank > defaults.qbg_top_k


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
