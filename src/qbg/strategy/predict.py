"""从 qlib/MLflow 读取最近一次模型预测并规整成项目代码格式。"""

from __future__ import annotations

import os

import pandas as pd
import yaml

from qbg.config import settings
from qbg.data import industry
from qbg.market import codes
from qbg.model.train import LIVE_EXPERIMENT, STATIC_EXPERIMENT, import_qlib
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)


def _latest_recorder(recorders: dict):
    if not recorders:
        raise RuntimeError("没有训练记录；先运行 python scripts/02_train.py")
    return max(recorders.values(), key=lambda rec: getattr(rec, "end_time", "") or "")


def load_production_predictions() -> tuple[pd.Series, str]:
    """日流程用的预测：**优先滚动重训的 live，回退静态的 cn_lgb**，返回 `(预测, 来源)`。

    ## 为什么需要这个函数

    `train(live=True)` 把滚动重训的结果写进 `cn_lgb_live`，而在 2026-09-09
    之前**没有任何代码读它** —— `load_latest_predictions()` 默认读静态的
    `cn_lgb`。于是即使加了 `--retrain`，日流程照样去读那份止于 2026-08-10 的
    旧预测。重训完全白做，而且没有任何迹象。

    这是本项目第六次出现同一个形状：**东西存在，但没人读**。

    回退到静态是刻意的：live 还没训过（首次部署）或这次重训失败时，用旧预测
    继续跑好过整天不跑 —— 而"旧到什么程度算不能用"由
    `prediction_freshness_guard` 判，不在这里判。**两件事分开**：
    这里负责"拿哪一份"，闸负责"这一份还能不能用"。
    """
    try:
        pred = load_latest_predictions(LIVE_EXPERIMENT)
        if pred is not None and len(pred):
            return pred, LIVE_EXPERIMENT
    except Exception as exc:  # noqa: BLE001 —— live 没训过是正常情况，不是错误
        log_event(log, "predict.live_unavailable", experiment=LIVE_EXPERIMENT,
                  error=f"{type(exc).__name__}: {exc}"[:160])
    return load_latest_predictions(STATIC_EXPERIMENT), STATIC_EXPERIMENT


def prediction_asof(pred: pd.Series) -> str | None:
    """预测里最新的那一天。闸要拿它和今天比。"""
    if pred is None or not len(pred) or not isinstance(pred.index, pd.MultiIndex):
        return None
    return str(pred.index.get_level_values("datetime").max().date())


def load_latest_predictions(experiment_name: str = STATIC_EXPERIMENT) -> pd.Series:
    """读取最新 ``pred.pkl``，返回 (datetime, canonical instrument) Series。"""
    os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
    try:
        qlib = import_qlib()
        from qlib.workflow import R
    except ImportError as exc:
        raise RuntimeError("qlib 未安装，无法读取模型预测") from exc

    cfg = yaml.safe_load(settings.workflow_yaml.read_text(encoding="utf-8"))
    uri = "file:" + str(settings.mlruns_dir.resolve()).replace("\\", "/")
    qlib.init(**cfg["qlib_init"], exp_manager={
        "class": "MLflowExpManager",
        "module_path": "qlib.workflow.expm",
        "kwargs": {"uri": uri, "default_exp_name": experiment_name},
    })
    exp = R.get_exp(experiment_name=experiment_name, create=False)
    rec = _latest_recorder(exp.list_recorders()).load_object("pred.pkl")
    pred = rec.iloc[:, 0] if isinstance(rec, pd.DataFrame) else rec
    pred = pred.astype(float).copy()
    if isinstance(pred.index, pd.MultiIndex) and "instrument" in pred.index.names:
        arrays = []
        for name in pred.index.names:
            values = pred.index.get_level_values(name)
            arrays.append([codes.normalize(v) for v in values] if name == "instrument" else values)
        pred.index = pd.MultiIndex.from_arrays(arrays, names=pred.index.names)
    return pred.sort_index()


def predictions_to_frame(pred: pd.Series) -> pd.DataFrame:
    """MultiIndex prediction → date × canonical instrument。"""
    if not isinstance(pred.index, pd.MultiIndex):
        raise ValueError("预测索引必须是 (datetime, instrument) MultiIndex")
    frame = pred.unstack("instrument")
    frame.index = pd.to_datetime(frame.index)
    return frame.sort_index().sort_index(axis=1)


def neutralize_frame(frame: pd.DataFrame, mapping: dict[str, str] | None = None) -> pd.DataFrame:
    """逐日执行申万一级行业内 demean，不跨日期泄漏信息。"""
    return frame.apply(lambda row: industry.neutralize(row, mapping=mapping), axis=1)


def latest_date_scores(pred: pd.Series, *, neutralize: bool = False,
                       mapping: dict[str, str] | None = None) -> pd.Series:
    """取最新日期的降序分数，可选行业中性化。"""
    if not isinstance(pred.index, pd.MultiIndex):
        raise ValueError("预测索引必须是 MultiIndex")
    latest = pred.index.get_level_values("datetime").max()
    scores = pred.xs(latest, level="datetime")
    if neutralize:
        scores = industry.neutralize(scores, mapping=mapping)
    return scores.dropna().sort_values(ascending=False)
