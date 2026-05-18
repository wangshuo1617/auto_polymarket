#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

LOG_FILE="logs/recommendation_auto_executor.log"
PID_FILE="logs/recommendation_auto_executor.pid"

mkdir -p logs

echo "=========================================="
echo "重启 recommendation_auto_executor 服务"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
echo "PM profile: ${AUTO_EXECUTOR_PM_PROFILE:-analyze}"
echo "限速:       ${AUTO_EXECUTE_RATE_PER_MINUTE:-6}/min"
echo "killswitch: ${AUTO_EXECUTE_KILLSWITCH:-0}"
echo "=========================================="

if [ "${FOREGROUND:-}" = "1" ]; then
  echo "[foreground] 前台启动 recommendation_auto_executor ..."
  exec uv run recommendation_auto_executor.py
fi

echo "[1/3] 停止已有 recommendation_auto_executor 进程..."
pkill -f "recommendation_auto_executor.py" || true
sleep 1

REMAINING=$(pgrep -f "recommendation_auto_executor.py" || true)
if [ -n "$REMAINING" ]; then
  echo "检测到残留进程,强制停止: $REMAINING"
  pkill -9 -f "recommendation_auto_executor.py" || true
  sleep 1
fi

echo "[2/3] 启动新进程..."
nohup uv run recommendation_auto_executor.py > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 2

echo "[3/3] 校验进程状态..."
if ps -p "$NEW_PID" > /dev/null 2>&1; then
  echo "✅ recommendation_auto_executor 启动成功"
  echo "PID: $NEW_PID"
  echo "日志: tail -f $LOG_FILE"
else
  echo "❌ recommendation_auto_executor 启动失败,请检查日志: $LOG_FILE"
  exit 1
fi
