"""parquet 缓存 → qlib 列存（`.bin`）。

qlib 只认它自己的二进制格式，转换分两步：
  1. 每只票导出一个 CSV（qlib `dump_bin.py` 的输入格式）
  2. 调 qlib 自带的 `scripts/dump_bin.py` 把 CSV 转成 `.bin`

第 2 步需要本地 clone 的 qlib（`qlib/`，gitignored）。**没装 qlib 时第 1 步
照样能跑完并明确告诉你差什么**，而不是抛一个 ImportError 让人以为数据层
坏了——P1/P2 阶段本来就还没到需要 qlib 的时候。

## 复权口径：为什么要把因子重新归一到"最后一天 = 1"

我们缓存里存的是**后复权因子**（锚在上市首日，茅台约 7.67），而 qlib 的
CN 数据约定是**前复权风格**：`$close` 是复权价、`$factor` 是因子、
`raw = $close / $factor`，且最后一天的 factor = 1，于是 `$close` 在最后
一天就等于真实成交价。

两种口径对模型完全等价（因子对某只票是常数倍，收益率序列一样），但归一到
qlib 的约定有个实际好处：**打印出来的价格是人能认的数**。调试时看到
`$close = 1309` 而不是 `$close = 10039`，能立刻和 APP 里的价格对上。

## 停牌日

停牌日的行**保留**，不删。qlib 的日历是全市场共用的，删掉个股的停牌行会
让它在对齐时用前值填充——那正是我们想要的（停牌期间持仓价值不变）。
删行反而会让不同股票的行数不一致，dump_bin 处理起来更容易出问题。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd

from qbg.config import PROJECT_ROOT, settings
from qbg.data import cache
from qbg.market import codes
from qbg.utils.logging import get_logger, log_event

log = get_logger(__name__)

# qlib dump_bin.py 期望的列。`symbol` 用 qlib 的 instrument 命名（SH600519）。
QLIB_COLUMNS = ("date", "symbol", "open", "high", "low", "close", "volume", "factor")

_CSV_SUBDIR = "qlib_csv"


def qlib_root() -> Path:
    """本地 clone 的 qlib 目录。"""
    return PROJECT_ROOT / "qlib"


def dump_bin_script() -> Path:
    return qlib_root() / "scripts" / "dump_bin.py"


def to_qlib_frame(df: pd.DataFrame, code: str) -> pd.DataFrame:
    """一只票的缓存 → qlib CSV 结构。

    因子重新归一到最后一天 = 1（见模块说明），价格随之变成"以今天为基准的
    前复权价"。
    """
    if df.empty:
        return pd.DataFrame(columns=list(QLIB_COLUMNS))

    out = df.copy()
    f = out["factor"].astype(float)
    last = f.iloc[-1]
    # 因子恒为正（cache.verify 会拦住非正值），但历史数据全缺时可能是 NaN。
    norm = f / last if last and last > 0 else pd.Series(1.0, index=f.index)

    res = pd.DataFrame({
        "date": pd.to_datetime(out["date"]).dt.strftime("%Y-%m-%d"),
        "symbol": codes.to_qlib(code),
        "open": out["open"] * norm,
        "high": out["high"] * norm,
        "low": out["low"] * norm,
        "close": out["close"] * norm,
        "volume": out["volume"],
        "factor": norm,
    })
    return res[list(QLIB_COLUMNS)]


def export_csv(members: list[str], csv_dir: Path | None = None,
               parquet_root: Path | None = None) -> dict:
    """把股票池导出成 qlib CSV。返回摘要。

    空数据的票**跳过而不是写空文件**：dump_bin 遇到空 CSV 会报错中断整批。
    """
    csv_dir = csv_dir or (settings.qlib_provider_uri.parent / _CSV_SUBDIR)
    csv_dir.mkdir(parents=True, exist_ok=True)

    written, skipped = 0, []
    for code in members:
        df = cache.read(code, parquet_root)
        frame = to_qlib_frame(df, code)
        if frame.empty:
            skipped.append(code)
            continue
        frame.to_csv(csv_dir / f"{codes.to_qlib(code)}.csv", index=False)
        written += 1

    summary = {"csv_dir": str(csv_dir), "written": written,
               "skipped": len(skipped), "skipped_codes": skipped[:20]}
    log_event(log, "qlib_dump.export_csv", **summary)
    return summary


def write_instruments(members: list[str], provider_uri: Path | None = None,
                      name: str = "all", parquet_root: Path | None = None) -> Path:
    """写 qlib 的 instruments 清单：`symbol<TAB>start<TAB>end`。

    起止日期取每只票在缓存里的实际首末交易日，而不是统一的区间——
    qlib 用它判断某只票在某天是否"在池子里"，写宽了会让退市/次新股在
    它们根本没上市的日期参与截面排序。
    """
    provider_uri = provider_uri or settings.qlib_provider_uri
    d = provider_uri / "instruments"
    d.mkdir(parents=True, exist_ok=True)

    lines = []
    for code in members:
        df = cache.read(code, parquet_root)
        if df.empty:
            continue
        start = pd.Timestamp(df["date"].iloc[0]).strftime("%Y-%m-%d")
        end = pd.Timestamp(df["date"].iloc[-1]).strftime("%Y-%m-%d")
        lines.append(f"{codes.to_qlib(code)}\t{start}\t{end}")

    p = d / f"{name}.txt"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log_event(log, "qlib_dump.instruments", path=str(p), count=len(lines))
    return p


def run_dump_bin(csv_dir: Path | None = None,
                 provider_uri: Path | None = None) -> dict:
    """调 qlib 的 `dump_bin.py`。qlib 没 clone 时返回明确的未完成状态。

    不抛异常是有意的：P1/P2 阶段还用不到 qlib，数据层本身是完好的，
    没必要因为"还没到那一步"就让 ingest 以非零码退出。
    """
    csv_dir = csv_dir or (settings.qlib_provider_uri.parent / _CSV_SUBDIR)
    provider_uri = provider_uri or settings.qlib_provider_uri
    script = dump_bin_script()

    if not script.exists():
        msg = (f"未找到 {script}。qlib 还没 clone —— P3 训练模型前执行：\n"
               f"  git clone https://github.com/microsoft/qlib qlib && pip install -e qlib")
        log_event(log, "qlib_dump.skipped", reason="qlib_not_cloned", script=str(script))
        return {"ok": False, "reason": "qlib_not_cloned", "hint": msg}

    provider_uri.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(script), "dump_all",
        # qlib 0.9.x used ``--csv_path``；当前主线改名为 ``--data_path``。
        # 本项目 vendored clone 跟随主线，因此使用新参数名。若未来固定回旧版，
        # 这里会明确返回 CLI 错误，而不会静默生成空数据。
        "--data_path", str(csv_dir),
        "--qlib_dir", str(provider_uri),
        "--include_fields", "open,high,low,close,volume,factor",
        "--date_field_name", "date",
        "--symbol_field_name", "symbol",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    ok = proc.returncode == 0
    log_event(log, "qlib_dump.dump_bin",
              ok=ok, returncode=proc.returncode,
              stderr=(proc.stderr or "")[-800:])
    return {"ok": ok, "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-2000:],
            "stderr": (proc.stderr or "")[-2000:]}


def dump(members: list[str], parquet_root: Path | None = None) -> dict:
    """完整流程：导出 CSV → 写 instruments → 转 bin。"""
    csv_summary = export_csv(members, parquet_root=parquet_root)
    inst = write_instruments(members, parquet_root=parquet_root)
    bin_result = run_dump_bin()
    return {"csv": csv_summary, "instruments": str(inst), "bin": bin_result}
