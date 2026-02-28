#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

MODE="${1:---dry-run}"
ENTRY_MINUTE="${2:-2}"
LOG_FILE="logs/5m_trade.log"
PID_FILE="logs/5m_trade.pid"

mkdir -p logs

if [ "$MODE" != "--dry-run" ] && [ "$MODE" != "--live" ]; then
  echo "❌ 模式参数错误：仅支持 --dry-run 或 --live"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute]"
  exit 1
fi

if ! [[ "$ENTRY_MINUTE" =~ ^[1-4]$ ]]; then
  echo "❌ entry_minute 必须是 1-4"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute]"
  exit 1
fi

echo "=========================================="
echo "重启 5m_trade 服务"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
echo "模式参数: $MODE"
echo "建仓分钟: $ENTRY_MINUTE"
echo "=========================================="

echo "[1/3] 停止已有 5m_trade 进程..."
pkill -f "5m_trade.py" || true
sleep 1

REMAINING=$(pgrep -f "5m_trade.py" || true)
if [ -n "$REMAINING" ]; then
  echo "检测到残留进程，强制停止: $REMAINING"
  pkill -9 -f "5m_trade.py" || true
  sleep 1
fi

echo "[2/3] 启动新进程..."
if [ "$MODE" = "--live" ]; then
  nohup uv run 5m_trade.py --entry-minute "$ENTRY_MINUTE" > "$LOG_FILE" 2>&1 &
else
  nohup uv run 5m_trade.py --dry-run --entry-minute "$ENTRY_MINUTE" > "$LOG_FILE" 2>&1 &
fi

NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 2

echo "[3/3] 校验进程状态..."
if ps -p "$NEW_PID" > /dev/null 2>&1; then
  echo "✅ 5m_trade 启动成功"
  echo "PID: $NEW_PID"
  echo "PID 文件: $PID_FILE"
  echo "日志文件: $LOG_FILE"
  echo "查看日志: tail -f $LOG_FILE"
else
  echo "❌ 5m_trade 启动失败，请检查日志: $LOG_FILE"
  exit 1
fi
