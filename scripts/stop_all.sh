#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

echo ""
echo "=========================================="
echo "  停止所有服务（分析 + 交易）"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
echo ""

STOPPED=0

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
    kill "$pid" 2>/dev/null || true
    sleep 1
    if ps -p "$pid" > /dev/null 2>&1; then
      kill -9 "$pid" 2>/dev/null || true
      sleep 1
    fi
    echo -e "  ${RED}■${NC} $name (PID=$pid) 已停止"
    STOPPED=$((STOPPED + 1))
  else
    echo "  $name (PID=$pid) 已不在运行"
  fi
  rm -f "$pid_file"
}

# ── 交易服务 ──
stop_service "btc_1s_market_monitor"     "logs/btc_1s_market_monitor.pid"
stop_service "update_mispricing_db"      "logs/update_mispricing.pid"
stop_service "5m_trade_mispricing"       "logs/5m_mp_trade.pid"

# ── 分析服务 ──
stop_service "position_analyze_btc"      "logs/position_analyze_4h.pid"
stop_service "position_analyze_oil"      "logs/position_analyze_oil_4h.pid"
stop_service "position_analyze_gold"     "logs/position_analyze_gold_4h.pid"
stop_service "account_change_report"     "logs/account_change_1h.pid"

# 兜底清理
pkill -f "5m_trade_mispricing.py" 2>/dev/null || true
pkill -f "update_mispricing_db.py" 2>/dev/null || true
pkill -f "btc_1s_market_monitor.py" 2>/dev/null || true
pkill -f "run_position_analyze_every_4h.py" 2>/dev/null || true
pkill -f "run_position_analyze_oil_every" 2>/dev/null || true
pkill -f "run_position_analyze_gold_every" 2>/dev/null || true

echo ""
echo -e "=========================================="
echo -e "  已停止 ${STOPPED} 个服务"
echo -e "=========================================="
echo ""
