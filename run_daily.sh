#!/usr/bin/env bash
#
# quant-biga 每日编排（Linux/macOS，对应 Windows 的 run_daily.bat）
#
#   ./run_daily.sh                 # 正常日跑
#   ./run_daily.sh --dry-run       # 走完全流程但不提交
#   ./run_daily.sh --date 2026-08-07 --force   # 补跑某一天
#   ./run_daily.sh --retrain       # 顺带重训（内存峰值约 4.4G，见 docs/deployment.md）
#
# 退出码沿用 daily_cycle 的语义，**不要吞掉**：
#   0  正常（含"非交易日/今日已跑"这类安静跳过）
#   2  硬闸中止 —— 风控拒绝交易，需要人看
#   其他  异常退出，Python 侧已尽力发过崩溃告警邮件
#
# cron 只在非零退出时才发本地邮件，所以上面的语义必须原样传出去。

set -euo pipefail

# ---------------------------------------------------------------------------
# 时区：必须显式设定
#
# 交易日历、交易时段、date.today() 全依赖它，而云服务器默认多是 UTC。
# 不设的话「今天」会差 8 小时——晚上跑的任务会算成前一天，直接对错日期出清单，
# 而且不会有任何报错。系统时区已经是上海时也无妨，这里只是把它钉死。
# ---------------------------------------------------------------------------
export TZ="${TZ:-Asia/Shanghai}"

# 中文日报和 JSONL 日志都要 UTF-8。cron 的 locale 通常是 POSIX，不设会乱码。
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
BOOT_LOG="$LOG_DIR/run_daily.log"

# log  = 只进文件；warn = 文件 + stderr
#
# 分开是因为 cron 的约定是「有输出 = 出了值得告诉你的事」。如果每天正常跑完也
# 往 stderr 写 START/DONE，配了 MAILTO 就会天天收到一封什么事都没有的信——
# 收上十天你就再也不看它了，真出事那封也一起被忽略。
log()  { printf '%s %s\n' "$(date '+%F %T %Z')" "$*" >>"$BOOT_LOG"; }
warn() { printf '%s %s\n' "$(date '+%F %T %Z')" "$*" | tee -a "$BOOT_LOG" >&2; }

# ---------------------------------------------------------------------------
# 找 Python
#
# 优先用 env 的 python 绝对路径，而不是 `conda activate`：cron 不加载任何 shell
# profile，conda 的 shell 函数根本不存在，`conda activate` 在 cron 里是最常见的
# 失败点。直接指向解释器则完全不依赖 shell 初始化。
#
# 覆盖方式：QBG_PYTHON=/path/to/python ./run_daily.sh
# ---------------------------------------------------------------------------
find_python() {
  if [[ -n "${QBG_PYTHON:-}" ]]; then
    [[ -x "$QBG_PYTHON" ]] && { echo "$QBG_PYTHON"; return 0; }
    log "ERROR QBG_PYTHON 指向的不是可执行文件: $QBG_PYTHON"
    return 1
  fi
  local candidate
  for candidate in \
      "$HOME/miniconda3/envs/qbg/bin/python" \
      "$HOME/anaconda3/envs/qbg/bin/python" \
      "$HOME/.conda/envs/qbg/bin/python" \
      "/opt/conda/envs/qbg/bin/python"; do
    [[ -x "$candidate" ]] && { echo "$candidate"; return 0; }
  done
  # 兜底：conda 在 PATH 里时问它要环境路径
  if command -v conda >/dev/null 2>&1; then
    candidate="$(conda run -n qbg python -c 'import sys; print(sys.executable)' 2>/dev/null || true)"
    [[ -n "$candidate" && -x "$candidate" ]] && { echo "$candidate"; return 0; }
  fi
  return 1
}

# ---------------------------------------------------------------------------
# 启动失败必须吵
#
# Python 一旦跑起来，崩溃有 notify_failure 兜底发邮件。但如果连解释器都找不到
# （环境没建好、盘没挂上、目录被删），Python 根本没机会运行，通知链是断的——
# 表现就是「今天没收到邮件」，而这和周末、和真的没交易，长得一模一样。
# 所以这里把启动失败写进日志并以非零码退出，让 cron 的 MAILTO 接住。
# ---------------------------------------------------------------------------
if ! PYTHON="$(find_python)"; then
  warn "ERROR 找不到 qbg 环境的 Python。设 QBG_PYTHON 指向解释器，或先建环境："
  warn "      conda env create -f environment.yml && conda activate qbg && pip install -e '.[data,model,llm]'"
  exit 127
fi

# ---------------------------------------------------------------------------
# 时区自检
#
# 上面 export 了 TZ，但要确认它**真的到达了 Python**——真正决定"今天是哪天"的
# 是 Python 看到的本地时区，不是 shell 里的变量。
#
# 只告警不中止：18:15 北京时 = 10:15 UTC，同一天，所以即使服务器是 UTC，
# 按计划任务跑也不会错日期。真正会出问题的是**北京时 00:00–08:00 之间**的
# 手动补跑——那时 UTC 还停在前一天，date.today() 会差一天，而且不会报任何错。
# 所以这里把它变成一条可见的记录，而不是直接拒跑一个多半能正常工作的系统。
# ---------------------------------------------------------------------------
tz_offset="$("$PYTHON" -c \
  'import datetime as d;print(d.datetime.now().astimezone().strftime("%z"))' 2>/dev/null || echo "?")"
if [[ "$tz_offset" != "+0800" ]]; then
  warn "WARN  Python 本地时区是 UTC$tz_offset，不是 +0800。"
  warn "      北京时 00:00-08:00 之间补跑会算错日期。修：timedatectl set-timezone Asia/Shanghai"
fi

# ---------------------------------------------------------------------------
# 互斥锁
#
# 防止 cron 与手动执行撞车。流水线内部有当日幂等标记，但那是在跑到一半才生效的；
# 两个进程同时写 parquet / runs.db / 报告文件才是真正的麻烦。
# 拿不到锁就直接退出 0：说明另一个实例正在跑，这不是错误，不该惊动 cron。
# ---------------------------------------------------------------------------
LOCK_FILE="$LOG_DIR/run_daily.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$LOCK_FILE"
  if ! flock -n 9; then
    log "SKIP 已有实例在运行（$LOCK_FILE），本次退出"
    exit 0
  fi
else
  warn "WARN  系统没有 flock，本次不加锁；cron 与手动执行同时跑可能写坏 parquet/runs.db"
fi

log "START $PYTHON -m qbg.orchestrator.daily_cycle $*"

set +e
"$PYTHON" -m qbg.orchestrator.daily_cycle "$@"
rc=$?
set -e

case "$rc" in
  0) log  "DONE  正常结束" ;;
  2) warn "WARN  硬闸中止（exit 2）——风控拒绝交易，需要人看" ;;
  *) warn "ERROR 异常退出 exit=$rc（Python 侧应已尝试发送崩溃告警）" ;;
esac

exit "$rc"
