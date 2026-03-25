#!/usr/bin/env bash
# 执行 tail_vol_trade 实盘循环，并把终端输出追加到日志（前台时同时打印到控制台）。
#
# 日志文件（默认）：
#   logs/tail_vol_execute.log     — 本脚本捕获的 stdout/stderr（含启动横幅）
# Python 模块另写：
#   logs/tail_vol_live.log        — logging 文件（见 tail_vol_trade/live.py）
#   logs/tail_vol_live_signals.jsonl — 每笔信号一行 JSON
#
# 用法：
#   ./scripts/run_tail_vol_execute.sh
#   ./scripts/run_tail_vol_execute.sh --execute --stake-usd 5
#   TAIL_VOL_EXECUTE_LOG=logs/my_tail.log ./scripts/run_tail_vol_execute.sh --poll-interval 1
#
# 后台运行（关闭终端也不断；只往日志写，不 tee 到控制台）：
#   TAIL_VOL_BACKGROUND=1 ./scripts/run_tail_vol_execute.sh --clob
#   TAIL_VOL_BACKGROUND=1 TAIL_VOL_EXECUTE_LOG=logs/my_tail.log ./scripts/run_tail_vol_execute.sh --execute
#   PID 默认写入 logs/tail_vol_execute.pid，可覆盖：TAIL_VOL_PID_FILE=...
#   停止：kill "$(cat logs/tail_vol_execute.pid)" 或再跑一次后台（会先尝试停旧进程）
#
# 需：btc_1s_market_monitor 写入与 tail_vol **相同**的 tick 库；真下单需 .env 密钥。
# 库路径：.env 的 SQLITE_DB_PATH，或一次性的 TAIL_VOL_DB=...，或：
#   ./scripts/run_tail_vol_execute.sh --db /home/you/data/trade.sqlite3 --execute
#   ./scripts/run_tail_vol_execute.sh --clob --execute
# （勿让 Windows 与 WSL 同时打开同一物理文件；优先 WSL 内路径如 ~/data/。）

set -euo pipefail
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p logs

LOG_FILE="${TAIL_VOL_EXECUTE_LOG:-logs/tail_vol_execute.log}"
PID_FILE="${TAIL_VOL_PID_FILE:-logs/tail_vol_execute.pid}"
RUN_TS="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"

resolve_python() {
  if [ -n "${PYTHON:-}" ]; then
    echo "$PYTHON"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    echo python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    echo python
    return
  fi
  echo "error: no python3/python in PATH; set PYTHON=" >&2
  exit 1
}

run_live() {
  if [ "${TAIL_VOL_NO_UV:-}" != "1" ] && command -v uv >/dev/null 2>&1; then
    uv run python -m tail_vol_trade.live "$@"
  else
    PY="$(resolve_python)"
    "$PY" -m tail_vol_trade.live "$@"
  fi
}

if [ "${TAIL_VOL_BACKGROUND:-}" = "1" ]; then
  if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "Stopping previous tail_vol_execute (PID $OLD_PID)..." >&2
      kill "$OLD_PID" 2>/dev/null || true
      sleep 2
      kill -9 "$OLD_PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
  fi

  {
    echo ""
    echo "================================================================"
    echo "[$RUN_TS] tail_vol_trade.live BACKGROUND start parent=$$ cwd=$PROJECT_ROOT"
    echo "args: $*"
    echo "execute_log: $LOG_FILE"
    echo "pid_file: $PID_FILE"
    echo "================================================================"
  } >> "$LOG_FILE"

  nohup env TAIL_VOL_NO_UV="${TAIL_VOL_NO_UV:-}" PYTHON="${PYTHON:-}" bash -c '
    cd "$1" || exit 1
    shift
    if [ "${TAIL_VOL_NO_UV:-}" != "1" ] && command -v uv >/dev/null 2>&1; then
      exec uv run python -m tail_vol_trade.live "$@"
    fi
    if [ -n "${PYTHON:-}" ]; then PY="$PYTHON"
    elif command -v python3 >/dev/null 2>&1; then PY=python3
    elif command -v python >/dev/null 2>&1; then PY=python
    else
      echo "error: no python3/python in PATH" >&2
      exit 1
    fi
    exec "$PY" -m tail_vol_trade.live "$@"
  ' _ "$PROJECT_ROOT" "$@" >> "$LOG_FILE" 2>&1 &

  NEW_PID=$!
  echo "$NEW_PID" > "$PID_FILE"
  sleep 1

  if kill -0 "$NEW_PID" 2>/dev/null; then
    echo "tail_vol_trade.live running in background pid=$NEW_PID"
    echo "  shell+log: $LOG_FILE"
    echo "  python:    logs/tail_vol_live.log"
    echo "  pid file:  $PID_FILE"
    echo "  stop:      kill $NEW_PID"
  else
    echo "Background start failed; see $LOG_FILE" >&2
    rm -f "$PID_FILE"
    exit 1
  fi
  exit 0
fi

{
  echo ""
  echo "================================================================"
  echo "[$RUN_TS] tail_vol_trade.live start pid=$$ cwd=$PROJECT_ROOT"
  echo "args: $*"
  echo "execute_log: $LOG_FILE"
  echo "================================================================"
} | tee -a "$LOG_FILE"

run_live "$@" 2>&1 | tee -a "$LOG_FILE"
EXIT_CODE=${PIPESTATUS[0]}

{
  echo "----------------------------------------------------------------"
  echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] tail_vol_trade.live exit_code=$EXIT_CODE"
  echo "================================================================"
} | tee -a "$LOG_FILE"

exit "$EXIT_CODE"
