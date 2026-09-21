"""非交易日那条路径上的四个洞。全离线。

2026-09-20（周日）一次真实运行暴露的：日流程正确跳过了，但

  · 盘前复核照跑 36 分钟、123 万 tokens、**$1.69** —— 它根本没有交易日闸；
  · 那次复核的结论**一行都没进库**（只有「日流程读缓存」这一条入库路径，
    而日流程跳过了），复盘 agent 于是把它报成 candidates=0；
  · 日报写着「PAPER 模式已直接向券商下单」，当天 0 笔。
"""

from __future__ import annotations

import sqlite3

import pytest

from qbg.report.daily_report import render
from qbg.store.etl import _ingest_cycle, connect, ingest_event

MODE = "PAPER"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "runs.db"
    connect(path).close()
    return path


def _verdict_event(code, rating, kept, *, date="2026-09-20", mode=MODE, **extra):
    event = {"ts": f"{date}T00:13:49.000+00:00", "logger": "qbg.agents.review",
             "msg": "agents.review.verdict", "level": "INFO",
             "code": code, "rating": rating, "kept": kept}
    if date is not None:
        event["date"] = date
    if mode is not None:
        event["mode"] = mode
    event.update(extra)
    return event


def _replay(path, events):
    db = connect(path)
    for event in events:
        ingest_event(db, event)
    db.commit()
    db.close()


# ------------------------------------------------------------------
# 盘前的交易日闸
# ------------------------------------------------------------------

def _premarket(monkeypatch):
    """把 26_premarket.py 作为模块载入，并把每一个副作用都换成「一碰就炸」。

    闸的全部价值就在于**它排在副作用之前**。直接断言源码里的行号太脆，
    所以这里让拉数/重训/复核只要被调到就失败。
    """
    from importlib import util
    spec = util.spec_from_file_location("premarket_mod", "scripts/26_premarket.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def boom(*args, **kwargs):
        raise AssertionError("闸没拦住 —— 副作用已经发生了")

    monkeypatch.setattr(module.subprocess, "run", boom)
    monkeypatch.setattr(module, "train", boom)
    monkeypatch.setattr(module, "load_production_predictions", boom)
    return module


def test_premarket_skips_a_non_trading_day(monkeypatch, capsys):
    """**这次真的省下了 $1.69。**"""
    module = _premarket(monkeypatch)
    monkeypatch.setattr(module.calendar, "is_trading_day", lambda *a, **k: False)

    assert module.main(["--date", "2026-09-20"]) == 0
    assert "不是交易日" in capsys.readouterr().out


def test_premarket_runs_on_a_trading_day(monkeypatch):
    """别拦过头 —— 交易日必须往下走（往下第一步就是被换掉的拉数，所以会炸）。"""
    module = _premarket(monkeypatch)
    monkeypatch.setattr(module.calendar, "is_trading_day", lambda *a, **k: True)

    with pytest.raises(AssertionError, match="闸没拦住"):
        module.main(["--date", "2026-09-21"])


def test_premarket_force_overrides_the_gate(monkeypatch):
    """手工在周末看候选是正当用法。"""
    module = _premarket(monkeypatch)
    monkeypatch.setattr(module.calendar, "is_trading_day", lambda *a, **k: False)

    with pytest.raises(AssertionError, match="闸没拦住"):
        module.main(["--date", "2026-09-20", "--force"])


# ------------------------------------------------------------------
# 复核结论在产出时就入库
# ------------------------------------------------------------------

def test_verdicts_land_without_any_cycle(db_path):
    """**核心修复。** 日流程压根没跑，结论也要留下痕迹。"""
    _replay(db_path, [_verdict_event("600183.SH", "Overweight", True),
                      _verdict_event("601689.SH", "Underweight", False)])
    with sqlite3.connect(db_path) as db:
        rows = db.execute("SELECT date,mode,code,kept,source FROM verdicts "
                          "ORDER BY code").fetchall()
    assert rows == [("2026-09-20", MODE, "600183.SH", 1, "agents.review"),
                    ("2026-09-20", MODE, "601689.SH", 0, "agents.review")]


def test_a_verdict_without_mode_is_not_guessed(db_path):
    """修复前记的事件没有 mode，`ingest_event` 会回退成 ADVISORY —— 而部署跑在
    PAPER。凭空造出的模式标签会和真行并排躺着，**缺行看得见，标错的行看着像真的**。"""
    _replay(db_path, [_verdict_event("600183.SH", "Overweight", True, mode=None)])
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 0


def test_an_errored_verdict_is_recorded(db_path):
    event = _verdict_event("600183.SH", None, False)
    event["msg"] = "agents.review.error"
    event["error"] = "timeout"
    _replay(db_path, [event])
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT rating,error FROM verdicts").fetchone() == ("Error", "timeout")


def test_a_skipped_cycle_does_not_wipe_them(db_path):
    """**重放顺序是复核事件在前、`cycle.completed` 在后。** 跳过的日子
    `agent_verdicts` 是空的，无条件 DELETE 会把盘前花钱买来的结论清掉。"""
    _replay(db_path, [_verdict_event("600183.SH", "Overweight", True)])
    db = connect(db_path)
    _ingest_cycle(db, {"date": "2026-09-20", "mode": MODE, "run_kind": "rebalance",
                       "skipped_reason": "not_trading_day", "agent_verdicts": []},
                  "2026-09-20", MODE)
    db.commit()
    db.close()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT COUNT(*) FROM verdicts").fetchone()[0] == 1


def test_a_real_cycle_still_replaces_them(db_path):
    """日流程带着结论回来时，它的 `review_source` 信息量更大，该覆盖。"""
    _replay(db_path, [_verdict_event("600183.SH", "Overweight", True)])
    db = connect(db_path)
    _ingest_cycle(db, {"date": "2026-09-20", "mode": MODE, "review_source": "premarket_cache",
                       "agent_verdicts": [{"code": "600183.SH", "rating": "Overweight",
                                           "kept": True}]},
                  "2026-09-20", MODE)
    db.commit()
    db.close()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT source FROM verdicts").fetchone()[0] == "premarket_cache"


# ------------------------------------------------------------------
# 日报不许说自己下过单
# ------------------------------------------------------------------

def _report(**over):
    result = {"date": "2026-09-20", "mode": MODE, "run_kind": "rebalance",
              "skipped_reason": "not_trading_day", "orders": [], "allowed_orders": [],
              "targets": {}, "scores": [], "gates": [], "positions": [], "account": {}}
    result.update(over)
    return render(result)


def test_an_empty_run_does_not_claim_to_have_ordered():
    text = _report()
    assert "已直接向券商下单" not in text, "0 笔订单的日报说自己下过单了"
    assert "本次没有订单" in text


def test_a_run_with_orders_still_says_so():
    """别拦过头 —— 真下了单必须说，那句话是给人对账用的。"""
    order = {"code": "600183.SH", "side": "BUY", "quantity": 100, "price": 10.0,
             "ref_price": 10.0, "estimated_fee": 1.0, "reason": "test"}
    text = _report(skipped_reason=None, orders=[order], allowed_orders=[order])
    assert "已直接向券商下单" in text


def test_advisory_mode_is_untouched():
    text = _report(mode="ADVISORY")
    assert "顾问模式不接触券商" in text


# ------------------------------------------------------------------
# 老库补列
# ------------------------------------------------------------------

def test_an_old_db_gets_the_new_column(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` **不会**改动已有的表。只改 schema.sql 对
    现网那份 runs.db 毫无作用，而带新列的 INSERT 会直接报错。
    重建整个库不是选项 —— hypotheses/proposals/reviews 是 agent 自己写的，
    日志里重建不出来。"""
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE positions (date TEXT, mode TEXT, code TEXT, name TEXT,"
                   " qty INTEGER, sellable_qty INTEGER, cost_price REAL, last_price REAL,"
                   " market_value REAL, pnl REAL, PRIMARY KEY (date, mode, code))")
        db.execute("INSERT INTO positions VALUES ('2026-09-18','PAPER','600000.SH','X',"
                   "100,100,10.0,11.0,1100.0,100.0)")

    connect(path).close()

    with sqlite3.connect(path) as db:
        assert "day_pnl" in {r[1] for r in db.execute("PRAGMA table_info(positions)")}
        assert db.execute("SELECT COUNT(*) FROM positions").fetchone()[0] == 1, "老数据不许丢"
        assert db.execute("SELECT day_pnl FROM positions").fetchone()[0] is None


def test_migrating_twice_is_harmless(tmp_path):
    """每次 connect 都会跑一遍。第二次必须是空操作，不能抛 duplicate column。"""
    path = tmp_path / "runs.db"
    for _ in range(3):
        connect(path).close()
    with sqlite3.connect(path) as db:
        columns = [r[1] for r in db.execute("PRAGMA table_info(positions)")]
    assert columns.count("day_pnl") == 1


def test_day_pnl_reaches_the_store(db_path):
    position = {"code": "600547.SH", "name": "山东黄金", "qty": 1800, "sellable_qty": 1800,
                "cost_price": 33.111, "last_price": 32.710, "market_value": 58878.0,
                "pnl": -721.06, "day_pnl": -108.0}
    db = connect(db_path)
    _ingest_cycle(db, {"date": "2026-09-21", "mode": MODE, "positions": [position],
                       "account": {}}, "2026-09-21", MODE)
    db.commit()
    db.close()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT pnl,day_pnl FROM positions").fetchone() == (-721.06, -108.0)
