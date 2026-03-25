#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p logs

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

STARTED=0
FAILED=0

echo ""
echo "=========================================="
echo "  一键启动定时分析 + 账户报告"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  工作目录: $PROJECT_ROOT"
echo "=========================================="
echo ""

# ── 通用函数 ──
stop_by_pid_file() {
  local pid_file="$1"
  local name="$2"
  if [ -f "$pid_file" ]; then
    local old_pid
    old_pid=$(cat "$pid_file")
    if ps -p "$old_pid" > /dev/null 2>&1; then
      kill "$old_pid" 2>/dev/null || true
      sleep 1
      if ps -p "$old_pid" > /dev/null 2>&1; then
        kill -9 "$old_pid" 2>/dev/null || true
        sleep 1
      fi
      echo -e "  ${YELLOW}已停止旧 $name (PID=$old_pid)${NC}"
    fi
    rm -f "$pid_file"
  fi
}

stop_by_pattern() {
  local pattern="$1"
  pkill -f "$pattern" 2>/dev/null || true
  sleep 1
}

start_service() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"
  shift 3
  local cmd=("$@")

  nohup "${cmd[@]}" >> "$log_file" 2>&1 &
  local new_pid=$!
  echo "$new_pid" > "$pid_file"
  sleep 2

  if ps -p "$new_pid" > /dev/null 2>&1; then
    echo -e "  ${GREEN}✅ $name${NC}  PID=$new_pid  日志=$log_file"
    STARTED=$((STARTED + 1))
  else
    echo -e "  ${RED}❌ $name 启动失败${NC}  请查看 $log_file"
    FAILED=$((FAILED + 1))
  fi
}

# ================================================================
# 1. BTC 持仓分析（每 4 小时）
# ================================================================
echo -e "${CYAN}[1/4] BTC 持仓分析 (每 4h)${NC}"
stop_by_pid_file "logs/position_analyze_4h.pid" "position_analyze"
stop_by_pattern "run_position_analyze_every_4h"
start_service "position_analyze_btc (4h)" \
  "logs/position_analyze_4h.pid" \
  "logs/position_analyze_4h.log" \
  uv run scripts/run_position_analyze_every_4h.py

# ================================================================
# 2. 原油持仓分析（每 4 小时）
# ================================================================
echo -e "${CYAN}[2/4] 原油持仓分析 (每 4h)${NC}"
stop_by_pid_file "logs/position_analyze_oil_4h.pid" "position_analyze_oil"
stop_by_pattern "run_position_analyze_oil_every"
stop_by_pattern "_pa_oil_4h.py"
start_service "position_analyze_oil (4h)" \
  "logs/position_analyze_oil_4h.pid" \
  "logs/position_analyze_oil_4h.log" \
  uv run scripts/run_position_analyze_oil_every_2h.py

# ================================================================
# 3. 黄金持仓分析（每 4 小时）
# ================================================================
echo -e "${CYAN}[3/4] 黄金持仓分析 (每 4h)${NC}"
stop_by_pid_file "logs/position_analyze_gold_4h.pid" "position_analyze_gold"
stop_by_pattern "run_position_analyze_gold_every"
stop_by_pattern "_pa_gold_4h.py"
start_service "position_analyze_gold (4h)" \
  "logs/position_analyze_gold_4h.pid" \
  "logs/position_analyze_gold_4h.log" \
  uv run scripts/run_position_analyze_gold_every_2h.py

# ================================================================
# 4. 账户变动报告
# ================================================================
echo -e "${CYAN}[4/4] 账户变动报告${NC}"
if pgrep -f "account_change_report" > /dev/null 2>&1; then
  echo -e "  ${YELLOW}已在运行，跳过${NC}"
else
  stop_by_pid_file "logs/account_change_1h.pid" "account_change_report"
  start_service "account_change_report" \
    "logs/account_change_1h.pid" \
    "logs/account_change_1h.log" \
    uv run account_change_report.py
fi

# ================================================================
# 汇总
# ================================================================
echo ""
echo "=========================================="
echo -e "  启动完成: ${GREEN}${STARTED} 成功${NC}  ${RED}${FAILED} 失败${NC}"
echo "=========================================="
echo ""
echo "  Mispricing 交易相关服务请单独启动:"
echo "    bash scripts/start_mp_trade.sh"
echo ""
echo "  停止所有服务:"
echo "    bash scripts/stop_all.sh"
echo ""
