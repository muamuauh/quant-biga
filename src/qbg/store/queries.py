"""派生 store 的只读查询；默认按 mode 隔离。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd

from qbg.config import settings


def table(name: str, *, mode: str | None = None, path: Path | None = None) -> pd.DataFrame:
    allowed = {"runs", "scores", "plans", "executions", "gates", "equity", "positions", "events"}
    if name not in allowed:
        raise ValueError(f"不允许的表: {name}")
    with sqlite3.connect(path or settings.db_path) as db:
        if mode is None:
            return pd.read_sql_query(f"SELECT * FROM {name}", db)  # noqa: S608
        return pd.read_sql_query(f"SELECT * FROM {name} WHERE mode=?", db,  # noqa: S608
                                 params=[mode.upper()])

