#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

LOG_FILE="logs/update_mispricing.log"
PID_FILE="logs/update_mispricing.pid"

mkdir -p logs

INTERVAL="${1:-300}"
BACKFILL="${2:-}"

echo "=========================================="
echo "启动 Mispricing 指标定时更新服务"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
echo "更新间隔: ${INTERVAL}s"
echo "=========================================="

echo "[1/3] 停止已有进程..."
pkill -f "update_mispricing_db.py" || true
sleep 1

REMAINING=$(pgrep -f "update_mispricing_db.py" || true)
if [ -n "$REMAINING" ]; then
  echo "检测到残留进程，强制停止: $REMAINING"
  pkill -9 -f "update_mispricing_db.py" || true
  sleep 1
fi

echo "[2/3] 启动新进程..."
EXTRA_ARGS=""
if [ -n "$BACKFILL" ]; then
  EXTRA_ARGS="--backfill-days $BACKFILL"
fi

nohup uv run new_trade/update_mispricing_db.py \
  --interval "$INTERVAL" \
  $EXTRA_ARGS \
  >> "$LOG_FILE" 2>&1 &

NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 2

echo "[3/3] 校验进程状态..."
if ps -p "$NEW_PID" > /dev/null 2>&1; then
  echo "✅ update_mispricing_db 启动成功"
  echo "PID: $NEW_PID"
  echo "PID 文件: $PID_FILE"
  echo "日志文件: $LOG_FILE"
  echo "查看日志: tail -f $LOG_FILE"
else
  echo "❌ update_mispricing_db 启动失败，请检查日志: $LOG_FILE"
  exit 1
fi
