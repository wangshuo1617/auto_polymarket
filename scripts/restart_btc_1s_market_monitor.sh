#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

DB_PATH="${1:-logs/btc_poly_1s.duckdb}"
SYMBOL="${2:-btcusdt}"
LOG_FILE="logs/btc_1s_market_monitor.log"
PID_FILE="logs/btc_1s_market_monitor.pid"

mkdir -p logs

echo "=========================================="
echo "重启 btc_1s_market_monitor 服务"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
echo "数据库文件: $DB_PATH"
echo "交易对: $SYMBOL"
echo "=========================================="

echo "[1/3] 停止已有 btc_1s_market_monitor 进程..."
pkill -f "btc_1s_market_monitor.py" || true
sleep 1

REMAINING=$(pgrep -f "btc_1s_market_monitor.py" || true)
if [ -n "$REMAINING" ]; then
  echo "检测到残留进程，强制停止: $REMAINING"
  pkill -9 -f "btc_1s_market_monitor.py" || true
  sleep 1
fi

echo "[2/3] 启动新进程..."
nohup uv run btc_1s_market_monitor.py --db-path "$DB_PATH" --symbol "$SYMBOL" > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 2

echo "[3/3] 校验进程状态..."
if ps -p "$NEW_PID" > /dev/null 2>&1; then
  echo "✅ btc_1s_market_monitor 启动成功"
  echo "PID: $NEW_PID"
  echo "PID 文件: $PID_FILE"
  echo "日志文件: $LOG_FILE"
  echo "查看日志: tail -f $LOG_FILE"
else
  echo "❌ btc_1s_market_monitor 启动失败，请检查日志: $LOG_FILE"
  exit 1
fi
