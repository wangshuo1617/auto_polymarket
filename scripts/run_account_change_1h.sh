#!/bin/bash
# 启动「每 1 小时执行 account_change_report.py」的调度器（后台运行）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

LOG_FILE="logs/account_change_1h.log"
PID_FILE="logs/account_change_1h.pid"

mkdir -p logs

if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    echo "账户变化调度器已在运行 (PID: $OLD_PID)，如需重启请先执行: kill $OLD_PID"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

echo "启动 account_change_report 每 1 小时调度器..."
nohup uv run scripts/run_account_change_every_1h.py >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 1

if kill -0 "$NEW_PID" 2>/dev/null; then
  echo "✅ 账户变化调度器已启动 (PID: $NEW_PID)"
  echo "日志: $LOG_FILE"
  echo "停止: kill $NEW_PID"
else
  echo "❌ 启动失败，请查看: $LOG_FILE"
  rm -f "$PID_FILE"
  exit 1
fi
