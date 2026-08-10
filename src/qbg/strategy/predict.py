"""从 qlib/MLflow 读取最近一次模型预测并规整成项目代码格式。"""

from __future__ import annotations

import os

import pandas as pd
import yaml

from qbg.config import settings
from qbg.data import industry
from qbg.market import codes
from qbg.model.train import STATIC_EXPERIMENT, import_qlib


def _latest_recorder(recorders: dict):
    if not recorders:
        raise RuntimeError("没有训练记录；先运行 python scripts/02_train.py")
    return max(recorders.values(), key=lambda rec: getattr(rec, "end_time", "") or "")


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
