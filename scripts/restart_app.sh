#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Dashboard 固定使用 analyze 账号视角，避免被 .env 默认值覆盖
export POLYMARKET_PROFILE="analyze"

LOG_FILE="logs/app.log"
PID_FILE="logs/app.pid"

mkdir -p logs

echo "=========================================="
echo "重启 app.py 服务"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
echo "账号 profile: $POLYMARKET_PROFILE"
echo "=========================================="

# --foreground 模式：前台运行，供 systemd 调用（通过环境变量 FOREGROUND=1 激活）
if [ "${FOREGROUND:-}" = "1" ]; then
  echo "[foreground] 前台启动 app.py ..."
  exec uv run app.py
fi

echo "[1/3] 停止已有 app.py 进程..."
pkill -f "app.py" || true
sleep 1

REMAINING=$(pgrep -f "app.py" || true)
if [ -n "$REMAINING" ]; then
  echo "检测到残留进程，强制停止: $REMAINING"
  pkill -9 -f "app.py" || true
  sleep 1
fi

echo "[2/3] 启动新进程..."
nohup uv run app.py > "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 2

echo "[3/3] 校验进程状态..."
if ps -p "$NEW_PID" > /dev/null 2>&1; then
  echo "✅ app.py 启动成功"
  echo "PID: $NEW_PID"
  echo "PID 文件: $PID_FILE"
  echo "日志文件: $LOG_FILE"
  echo "查看日志: tail -f $LOG_FILE"
else
  echo "❌ app.py 启动失败，请检查日志: $LOG_FILE"
  exit 1
fi
