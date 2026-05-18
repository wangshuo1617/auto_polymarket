#!/bin/bash
# Restart wrapper for advisory settlement refresher (R2).
# - FOREGROUND=1 -> exec for systemd (auto-poly-advisory-settlement.service)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

export LD_PRELOAD=""

INTERVAL="${ADVISORY_SETTLEMENT_INTERVAL:-600}"
MAX_STRIKES="${ADVISORY_SETTLEMENT_MAX_STRIKES:-8}"
SLUG="${ADVISORY_SETTLEMENT_SLUG:-}"

CMD=(uv run scripts/advisory_settlement_refresher.py
     --interval "$INTERVAL"
     --max-strikes "$MAX_STRIKES")
if [ -n "$SLUG" ]; then
  CMD+=(--slug "$SLUG")
fi

LOG_FILE="logs/advisory_settlement_refresher.stdout.log"
PID_FILE="logs/advisory_settlement_refresher.pid"

mkdir -p logs

echo "=========================================="
echo "重启 advisory_settlement_refresher"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "interval=${INTERVAL}s max_strikes=${MAX_STRIKES} slug=${SLUG:-<auto>}"
echo "=========================================="

if [ "${FOREGROUND:-}" = "1" ]; then
  echo "[foreground] exec ${CMD[*]}"
  exec "${CMD[@]}"
fi

if [ -f "$PID_FILE" ]; then
  OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "[1/2] 停止旧进程 PID=$OLD_PID"
    kill "$OLD_PID" || true
    sleep 2
  fi
  rm -f "$PID_FILE"
fi

echo "[2/2] 启动 advisory_settlement_refresher (后台)"
nohup "${CMD[@]}" >>"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
echo "PID=$(cat "$PID_FILE") 日志: $LOG_FILE"
