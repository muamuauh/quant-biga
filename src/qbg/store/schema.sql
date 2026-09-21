PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 全部是从 JSONL/CSV/MLflow 可重建的派生索引，交易路径禁止读取本库。
CREATE TABLE IF NOT EXISTS runs (
  date TEXT NOT NULL, mode TEXT NOT NULL, started_ts TEXT, finished_ts TEXT,
  run_kind TEXT, skipped_reason TEXT, market_risk_on INTEGER, submitted INTEGER,
  hard_ok INTEGER, report_path TEXT, payload_json TEXT,
  PRIMARY KEY (date, mode)
);
CREATE TABLE IF NOT EXISTS scores (
  date TEXT NOT NULL, mode TEXT NOT NULL, code TEXT NOT NULL, score REAL, rank INTEGER,
  PRIMARY KEY (date, mode, code)
);
CREATE TABLE IF NOT EXISTS plans (
  date TEXT NOT NULL, mode TEXT NOT NULL, code TEXT NOT NULL, target_weight REAL,
  PRIMARY KEY (date, mode, code)
);
CREATE TABLE IF NOT EXISTS executions (
  date TEXT NOT NULL, mode TEXT NOT NULL, code TEXT NOT NULL, side TEXT NOT NULL,
  quantity INTEGER, limit_price REAL, ref_price REAL, estimated_fee REAL, allowed INTEGER,
  reason TEXT, PRIMARY KEY (date, mode, code, side)
);
-- TradingAgents 逐票复核的结论。**2026-09-18 才建** —— `plan.md` 从一开始就写着
-- "P7 的评级写这里"，但 schema 里一直没有它，ETL 也不写。后果是复核结论只活在
-- data/reviews/*.json 和日报正文里，派生库看不到，**复盘 agent 也就看不到**：
-- 2026-09-15~17 连续三天复核把 5 只候选全拦下、账户满仓现金，而复盘 agent
-- 写的是"运行健康：全部正常"。它不是判断失误，是压根没拿到这项事实。
CREATE TABLE IF NOT EXISTS verdicts (
  date TEXT NOT NULL, mode TEXT NOT NULL, code TEXT NOT NULL, rating TEXT,
  kept INTEGER NOT NULL DEFAULT 0, error TEXT, source TEXT,
  PRIMARY KEY (date, mode, code)
);
CREATE TABLE IF NOT EXISTS gates (
  date TEXT NOT NULL, mode TEXT NOT NULL, name TEXT NOT NULL, passed INTEGER, reason TEXT,
  PRIMARY KEY (date, mode, name)
);
CREATE TABLE IF NOT EXISTS equity (
  date TEXT NOT NULL, mode TEXT NOT NULL, total_equity REAL, available_cash REAL,
  PRIMARY KEY (date, mode)
);
CREATE TABLE IF NOT EXISTS positions (
  date TEXT NOT NULL, mode TEXT NOT NULL, code TEXT NOT NULL, name TEXT, qty INTEGER,
  sellable_qty INTEGER, cost_price REAL, last_price REAL, market_value REAL, pnl REAL,
  day_pnl REAL,
  PRIMARY KEY (date, mode, code)
);
CREATE TABLE IF NOT EXISTS events (
  ts TEXT NOT NULL, logger TEXT NOT NULL, msg TEXT NOT NULL, level TEXT, date TEXT,
  mode TEXT, payload_json TEXT NOT NULL,
  PRIMARY KEY (ts, logger, msg, payload_json)
);
CREATE INDEX IF NOT EXISTS ix_events_date_msg ON events(date, msg);

CREATE TABLE IF NOT EXISTS findings (
  date TEXT NOT NULL, mode TEXT NOT NULL, code TEXT NOT NULL, severity TEXT,
  title TEXT, detail TEXT, evidence_json TEXT,
  PRIMARY KEY (date, mode, code)
);
CREATE TABLE IF NOT EXISTS hypotheses (
  id TEXT PRIMARY KEY, mode TEXT NOT NULL, opened_date TEXT NOT NULL, topic TEXT NOT NULL,
  statement TEXT NOT NULL, discriminator TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
  check_after TEXT, resolution TEXT, resolved_date TEXT, n_observations INTEGER DEFAULT 1,
  evidence_json TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
  id TEXT PRIMARY KEY, created_ts TEXT, param TEXT, scope TEXT, tier TEXT, old_value TEXT,
  new_value TEXT, rationale TEXT, gate_json TEXT, gate_passed INTEGER, status TEXT
);
CREATE TABLE IF NOT EXISTS param_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT, applied_ts TEXT, param TEXT, old_value TEXT,
  new_value TEXT, proposal_id TEXT, actor TEXT, overlay_snapshot TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
  date TEXT NOT NULL, mode TEXT NOT NULL, kind TEXT NOT NULL, path TEXT, created_ts TEXT,
  n_findings INTEGER, max_severity TEXT, cost_usd REAL,
  PRIMARY KEY (date, mode, kind)
);
