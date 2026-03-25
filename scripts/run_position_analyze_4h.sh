#!/bin/bash
# 启动「每 4 小时执行 position_analyze.py」的调度器（后台运行）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

LOG_FILE="logs/position_analyze_4h.log"
PID_FILE="logs/position_analyze_4h.pid"

mkdir -p logs

if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "正在停止原有持仓分析调度器 (PID: $OLD_PID)..."
    kill "$OLD_PID" 2>/dev/null || true
    sleep 2
    kill -9 "$OLD_PID" 2>/dev/null || true
    sleep 1
  fi
  rm -f "$PID_FILE"
fi

echo "启动 position_analyze 每 4 小时调度器..."
nohup uv run scripts/run_position_analyze_every_4h.py >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 1

if kill -0 "$NEW_PID" 2>/dev/null; then
  echo "✅ 调度器已启动 (PID: $NEW_PID)"
  echo "日志: $LOG_FILE"
  echo "停止: kill $NEW_PID"
else
  echo "❌ 启动失败，请查看: $LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi
