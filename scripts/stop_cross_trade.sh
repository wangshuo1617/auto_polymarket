#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

RED='\033[0;31m'
YELLOW='\033[0;33m'
GREEN='\033[0;32m'
NC='\033[0m'

echo ""
echo "=========================================="
echo "  停止 Cross 交易服务"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
echo ""

STOPPED=0

# 可通过环境变量覆盖
#   CROSS_STOP_GRACE_SEC      首次 SIGTERM 后最大等待秒数（默认 15）
#   CROSS_STOP_POLL_INTERVAL  轮询间隔秒数（默认 1）
CROSS_STOP_GRACE_SEC="${CROSS_STOP_GRACE_SEC:-15}"
CROSS_STOP_POLL_INTERVAL="${CROSS_STOP_POLL_INTERVAL:-1}"

wait_pid_exit() {
  local pid="$1"
  local grace_sec="$2"
  local poll_interval="$3"
  local elapsed=0

  while ps -p "$pid" > /dev/null 2>&1; do
    if [ "$elapsed" -ge "$grace_sec" ]; then
      return 1
    fi
    sleep "$poll_interval"
    elapsed=$((elapsed + poll_interval))
  done
  return 0
}

stop_service() {
  local name="$1"
  local pid_file="$2"

  if [ ! -f "$pid_file" ]; then
    echo "  $name: PID 文件不存在，跳过"
    return
  fi

  local pid
  pid=$(cat "$pid_file")
  if ps -p "$pid" > /dev/null 2>&1; then
    echo -e "  ${YELLOW}■${NC} 正在优雅停止 $name (PID=$pid)，等待最多 ${CROSS_STOP_GRACE_SEC}s..."
    kill "$pid" 2>/dev/null || true
    if wait_pid_exit "$pid" "$CROSS_STOP_GRACE_SEC" "$CROSS_STOP_POLL_INTERVAL"; then
      echo -e "  ${GREEN}■${NC} $name (PID=$pid) 已优雅停止"
      STOPPED=$((STOPPED + 1))
    else
      echo -e "  ${YELLOW}■${NC} $name 超时未退出，升级强制停止 (kill -9)"
      kill -9 "$pid" 2>/dev/null || true
      sleep 1
      if ps -p "$pid" > /dev/null 2>&1; then
        echo -e "  ${RED}■${NC} $name (PID=$pid) 强制停止失败，请手动检查"
      else
        echo -e "  ${RED}■${NC} $name (PID=$pid) 已强制停止"
        STOPPED=$((STOPPED + 1))
      fi
    fi
  else
    echo "  $name (PID=$pid) 已不在运行"
  fi
  rm -f "$pid_file"
}

stop_service "cross_trade_5m" "logs/cross_trade_5m.pid"
stop_service "btc_1s_market_monitor" "logs/btc_1s_market_monitor.pid"

pkill -f "cross_trade/cross_trade_5m.py" 2>/dev/null || true
pkill -f "python -m cross_trade" 2>/dev/null || true
pkill -f "btc_1s_market_monitor.py" 2>/dev/null || true

echo ""
echo "=========================================="
echo "  已停止 ${STOPPED} 个服务"
echo "=========================================="
echo ""
