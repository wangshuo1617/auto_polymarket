#!/bin/bash
# 启动「每 2 小时执行 position_analyze_oil.py」的调度器（后台运行）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

LOG_FILE="logs/position_analyze_oil_2h.log"
PID_FILE="logs/position_analyze_oil_2h.pid"

mkdir -p logs

if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "原油分析调度器已在运行 (PID: $OLD_PID)，如需重启请先执行: kill $OLD_PID"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

echo "启动 position_analyze_oil 每 2 小时调度器..."
nohup uv run scripts/run_position_analyze_oil_every_2h.py >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 1

if kill -0 "$NEW_PID" 2>/dev/null; then
  echo "✅ 原油分析调度器已启动 (PID: $NEW_PID)"
  echo "日志: $LOG_FILE"
  echo "停止: kill $NEW_PID"
else
  echo "❌ 启动失败，请查看: $LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi
