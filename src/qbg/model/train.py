"""训练 qlib Alpha158 + LightGBM 多 seed 集成模型。

只运行本项目需要的 fit/predict，并把产物直接写入 MLflow recorder。刻意不跑
qlib 的 ``workflow``、``SigAnaRecord`` 或 ``PortAnaRecord``：Windows 上后者
的 joblib/loky 拆卸会在模型已经训练成功后把进程硬杀掉。
"""

from __future__ import annotations

import importlib
import os
import sys
from copy import deepcopy
from pathlib import Path

import pandas as pd
import yaml

from qbg.config import PROJECT_ROOT, settings
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

STATIC_EXPERIMENT = "cn_lgb"
LIVE_EXPERIMENT = "cn_lgb_live"


def import_qlib():
    """导入 vendored qlib，避开仓库根目录形成的同名 namespace 阴影。"""
    module = importlib.import_module("qlib")
    if hasattr(module, "init"):
        return module
    sys.modules.pop("qlib", None)
    vendored_root = str(PROJECT_ROOT / "qlib")
    if vendored_root not in sys.path:
        sys.path.insert(0, vendored_root)
    module = importlib.import_module("qlib")
    if not hasattr(module, "init"):
        raise RuntimeError("qlib 安装或 vendored 路径损坏：导入模块没有 init()")
    return module


def ensemble_seeds(base: int = 42, count: int | None = None) -> list[int]:
    """返回稳定、连续的 seed 列表，至少训练一个模型。"""
    n = settings.qbg_ensemble_seeds if count is None else count
    return [base + i for i in range(max(1, int(n)))]


def cross_sectional_rank_ic(pred: pd.Series, label: pd.Series) -> float:
    """按日算 Spearman IC 后取均值；无有效截面时返回 0。"""
    joined = pd.concat([pred.rename("pred"), label.rename("label")], axis=1).dropna()
    if joined.empty:
        return 0.0
    date_level = "datetime" if "datetime" in joined.index.names else joined.index.names[0]
    daily = joined.groupby(level=date_level, sort=True).apply(
        lambda x: x["pred"].corr(x["label"], method="spearman")
    )
    daily = daily.dropna()
    return float(daily.mean()) if len(daily) else 0.0


def _roll_windows_to_latest(cfg: dict, valid_days: int = 63, label_buffer: int = 3,
                            infer_days: int = 10) -> dict:
    """把静态可复现分段滚动到 qlib 日历的最新一天，供每日预测使用。"""
    from qlib.data import D

    out = deepcopy(cfg)
    dhc = out["data_handler_config"]
    cal = list(D.calendar(start_time=dhc["start_time"], end_time="2099-01-01"))
    need = infer_days + valid_days + label_buffer + 30
    if len(cal) < need:
        raise RuntimeError(f"qlib 日历太短：需要至少 {need} 日，实际 {len(cal)} 日")

    def day(index: int) -> str:
        return pd.Timestamp(cal[index]).strftime("%Y-%m-%d")

    train_end = day(-(infer_days + valid_days + label_buffer))
    valid_start = day(-(infer_days + valid_days))
    valid_end = day(-(infer_days + label_buffer))
    test_start = day(-infer_days)
    latest = day(-1)
    dhc.update(end_time=latest, fit_end_time=train_end)
    handler = out["task"]["dataset"]["kwargs"]["handler"]["kwargs"]
    handler.update(end_time=latest, fit_end_time=train_end)
    out["task"]["dataset"]["kwargs"]["segments"] = {
        "train": [dhc["start_time"], train_end],
        "valid": [valid_start, valid_end],
        "test": [test_start, latest],
    }
    log_event(log, "train.live.windows", train_end=train_end,
              valid=[valid_start, valid_end], test=[test_start, latest])
    return out


def train(workflow_yaml: Path | None = None, experiment_name: str | None = None,
          *, live: bool = False, seed_count: int | None = None) -> str:
    """训练多 seed 模型并保存平均预测，返回 MLflow recorder id。"""
    # MLflow 3.15 起 file store 进入维护模式并要求显式 opt-in。本项目的 mlruns
    # 是可再生模型产物且已 gitignore，继续用它比再引入一套状态数据库更符合
    # “本地文件是真相源”的现有架构。只影响当前及子进程，不修改用户环境。
    os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
    try:
        qlib = import_qlib()
        from qlib.utils import init_instance_by_config
        from qlib.workflow import R
    except ImportError as exc:
        raise RuntimeError(
            "P3 训练依赖未安装：先 clone qlib 并执行 pip install -e qlib，"
            "再安装项目的 model extra"
        ) from exc

    cfg_path = workflow_yaml or settings.workflow_yaml
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    experiment = experiment_name or (LIVE_EXPERIMENT if live else STATIC_EXPERIMENT)
    uri = "file:" + str(settings.mlruns_dir.resolve()).replace("\\", "/")
    qlib.init(**cfg["qlib_init"], exp_manager={
        "class": "MLflowExpManager",
        "module_path": "qlib.workflow.expm",
        "kwargs": {"uri": uri, "default_exp_name": experiment},
    })
    if live:
        cfg = _roll_windows_to_latest(cfg)

    task = cfg["task"]
    dataset = init_instance_by_config(task["dataset"])
    label_frame = dataset.prepare("test", col_set="label")
    label = label_frame.iloc[:, 0] if isinstance(label_frame, pd.DataFrame) else label_frame
    seeds = ensemble_seeds(count=seed_count)
    log_event(log, "train.start", experiment=experiment, live=live, seeds=seeds)

    with R.start(experiment_name=experiment):
        predictions: list[pd.Series] = []
        seed_ics: dict[str, float] = {}
        models: dict[str, object] = {}
        for seed in seeds:
            model_cfg = deepcopy(task["model"])
            model_cfg.setdefault("kwargs", {})["seed"] = seed
            model = init_instance_by_config(model_cfg)
            model.fit(dataset)
            raw = model.predict(dataset, segment="test")
            pred = raw.iloc[:, 0] if isinstance(raw, pd.DataFrame) else raw
            pred = pred.astype(float).rename(f"seed_{seed}")
            predictions.append(pred)
            models[str(seed)] = model
            seed_ics[str(seed)] = cross_sectional_rank_ic(pred, label)

        seed_frame = pd.concat(predictions, axis=1)
        average = seed_frame.mean(axis=1).rename("score")
        R.save_objects(**{
            "params.pkl": models,
            "pred.pkl": average,
            "seed_predictions.pkl": seed_frame,
            "seed_rank_ic.pkl": seed_ics,
        })
        recorder_id = R.get_recorder().id

    log_event(log, "train.done", experiment=experiment, recorder_id=recorder_id,
              seed_rank_ic=seed_ics, ensemble_rank_ic=cross_sectional_rank_ic(average, label))
    return recorder_id
