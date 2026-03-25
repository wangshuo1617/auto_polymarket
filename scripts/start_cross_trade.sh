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

# 可通过环境变量覆盖（统一映射到 cross_trade/cross_trade_5m.py 参数）
#
# cross 策略特有参数：
#   CROSS_TREND_TH           -> --trend-th
#   CROSS_OPEN_MAX_TH        -> --cross-open-max-th
#   CROSS_MAX_END_BID        -> --max-end-bid
#   CROSS_MAX_BTC_DELTA      -> --max-btc-delta
#   CROSS_TICK_DB_PATH       -> --tick-db-path (可选)
#
# 继承自 build_trade_arg_parser 的通用参数：
#   CROSS_DRY_RUN            -> --dry-run (1 启用)
#   CROSS_STAKE              -> --stake-usd
#   CROSS_REPORT_INTERVAL_SEC-> --report-interval-sec
#   CROSS_ENTRY_MINUTE       -> --entry-minute
#   CROSS_ENTRY_PRECLOSE_SEC -> --entry-preclose-sec
#   CROSS_MIN_DIRECTION_DIFF -> --min-direction-diff
#   CROSS_MAX_ENTRY_PRICE    -> --max-entry-price
#   CROSS_TP_PRICE_CAP       -> --tp-price-cap
#   CROSS_TP_VALUE_CAP       -> --tp-value-cap
#   CROSS_SL_TO_TP_RATIO     -> --sl-to-tp-ratio
#   CROSS_MIN_HOLD_SEC       -> --min-hold-before-close-sec
#   CROSS_TOXIC_UTC_HOURS    -> --toxic-utc-hours
#   CROSS_TRADE_DB_PATH      -> --trade-db-path (可选)
#
# 兼容保留参数（通常无需改）：
#   CROSS_TAKE_PROFIT_SPREAD -> --take-profit-spread
#   CROSS_STOP_LOSS_SPREAD   -> --stop-loss-spread
#
# 额外透传：
#   CROSS_EXTRA_ARGS
CROSS_DRY_RUN="${CROSS_DRY_RUN:-0}"
CROSS_STAKE="${CROSS_STAKE:-5.0}"
CROSS_REPORT_INTERVAL_SEC="${CROSS_REPORT_INTERVAL_SEC:-3600}"
CROSS_ENTRY_MINUTE="${CROSS_ENTRY_MINUTE:-4}"
CROSS_ENTRY_PRECLOSE_SEC="${CROSS_ENTRY_PRECLOSE_SEC:-5}"
CROSS_MIN_DIRECTION_DIFF="${CROSS_MIN_DIRECTION_DIFF:-0.01}"
CROSS_MAX_ENTRY_PRICE="${CROSS_MAX_ENTRY_PRICE:-0.98}"
CROSS_TAKE_PROFIT_SPREAD="${CROSS_TAKE_PROFIT_SPREAD:-0.15}"
CROSS_STOP_LOSS_SPREAD="${CROSS_STOP_LOSS_SPREAD:--0.20}"
CROSS_TP_PRICE_CAP="${CROSS_TP_PRICE_CAP:-0.95}"
CROSS_TP_VALUE_CAP="${CROSS_TP_VALUE_CAP:-0.15}"
CROSS_SL_TO_TP_RATIO="${CROSS_SL_TO_TP_RATIO:-1.3333333333333333}"
CROSS_MIN_HOLD_SEC="${CROSS_MIN_HOLD_SEC:-5}"
CROSS_TOXIC_UTC_HOURS="${CROSS_TOXIC_UTC_HOURS:-}"
CROSS_TRADE_DB_PATH="${CROSS_TRADE_DB_PATH:-}"

CROSS_TREND_TH="${CROSS_TREND_TH:-0.04}"
CROSS_OPEN_MAX_TH="${CROSS_OPEN_MAX_TH:-6}"
CROSS_MAX_END_BID="${CROSS_MAX_END_BID:-0.99}"
CROSS_MAX_BTC_DELTA="${CROSS_MAX_BTC_DELTA:-80}"
CROSS_TICK_DB_PATH="${CROSS_TICK_DB_PATH:-}"

CROSS_EXTRA_ARGS="${CROSS_EXTRA_ARGS:-}"

echo ""
echo "=========================================="
echo "  启动 Cross 5m 交易服务"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  工作目录: $PROJECT_ROOT"
echo "──────────────────────────────────────────"
echo "  基础下注: ${CROSS_STAKE} USDC"
echo "  trend 阈值: ${CROSS_TREND_TH}"
echo "  cross_open_max 上限: ${CROSS_OPEN_MAX_TH}"
echo "  4m末 bid 上限: ${CROSS_MAX_END_BID}"
echo "  BTC delta 上限: ${CROSS_MAX_BTC_DELTA}"
echo "  entry_minute/preclose: ${CROSS_ENTRY_MINUTE} / ${CROSS_ENTRY_PRECLOSE_SEC}s"
echo "  max_entry_price: ${CROSS_MAX_ENTRY_PRICE}"
echo "  TP价格/价差上限: ${CROSS_TP_PRICE_CAP} / ${CROSS_TP_VALUE_CAP}"
echo "  SL:TP 倍率: ${CROSS_SL_TO_TP_RATIO}"
echo "  min_hold_sec: ${CROSS_MIN_HOLD_SEC}"
echo "  dry_run: $([ \"$CROSS_DRY_RUN\" = \"1\" ] && echo '开启' || echo '关闭')"
echo "  toxic_utc_hours: ${CROSS_TOXIC_UTC_HOURS:-<empty>}"
if [ -n "$CROSS_TRADE_DB_PATH" ]; then
  echo "  trade_db_path: ${CROSS_TRADE_DB_PATH}"
fi
if [ -n "$CROSS_TICK_DB_PATH" ]; then
  echo "  tick_db_path: ${CROSS_TICK_DB_PATH}"
fi
echo "=========================================="
echo ""

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

echo -e "${CYAN}[1/2] BTC 1s Market Monitor${NC}"
stop_by_pid_file "logs/btc_1s_market_monitor.pid" "btc_1s_market_monitor"
stop_by_pattern "btc_1s_market_monitor.py"
start_service "btc_1s_market_monitor" \
  "logs/btc_1s_market_monitor.pid" \
  "logs/btc_1s_market_monitor.log" \
  uv run btc_1s_market_monitor.py --symbol btcusdt

echo -e "${CYAN}[2/2] Cross 5m 交易${NC}"
stop_by_pid_file "logs/cross_trade_5m.pid" "cross_trade_5m"
stop_by_pattern "cross_trade/cross_trade_5m.py"
stop_by_pattern "python -m cross_trade"

CMD=(
  uv run -m cross_trade
  --stake-usd "$CROSS_STAKE"
  --report-interval-sec "$CROSS_REPORT_INTERVAL_SEC"
  --entry-minute "$CROSS_ENTRY_MINUTE"
  --entry-preclose-sec "$CROSS_ENTRY_PRECLOSE_SEC"
  --min-direction-diff "$CROSS_MIN_DIRECTION_DIFF"
  --max-entry-price "$CROSS_MAX_ENTRY_PRICE"
  --take-profit-spread "$CROSS_TAKE_PROFIT_SPREAD"
  --stop-loss-spread "$CROSS_STOP_LOSS_SPREAD"
  --tp-price-cap "$CROSS_TP_PRICE_CAP"
  --tp-value-cap "$CROSS_TP_VALUE_CAP"
  --sl-to-tp-ratio "$CROSS_SL_TO_TP_RATIO"
  --min-hold-before-close-sec "$CROSS_MIN_HOLD_SEC"
  --toxic-utc-hours "$CROSS_TOXIC_UTC_HOURS"
  --trend-th "$CROSS_TREND_TH"
  --cross-open-max-th "$CROSS_OPEN_MAX_TH"
  --max-end-bid "$CROSS_MAX_END_BID"
  --max-btc-delta "$CROSS_MAX_BTC_DELTA"
)

if [ "$CROSS_DRY_RUN" = "1" ]; then
  CMD+=(--dry-run)
fi

if [ -n "$CROSS_TRADE_DB_PATH" ]; then
  CMD+=(--trade-db-path "$CROSS_TRADE_DB_PATH")
fi

if [ -n "$CROSS_TICK_DB_PATH" ]; then
  CMD+=(--tick-db-path "$CROSS_TICK_DB_PATH")
fi

if [ -n "$CROSS_EXTRA_ARGS" ]; then
  # shellcheck disable=SC2206
  EXTRA_ARR=($CROSS_EXTRA_ARGS)
  CMD+=("${EXTRA_ARR[@]}")
fi

start_service "cross_trade_5m" \
  "logs/cross_trade_5m.pid" \
  "logs/cross_trade_5m.stdout.log" \
  "${CMD[@]}"

echo ""
echo "=========================================="
echo -e "  启动完成: ${GREEN}${STARTED} 成功${NC}  ${RED}${FAILED} 失败${NC}"
echo "=========================================="
echo ""
echo "  查看业务日志: tail -f logs/5m_trade.log"
echo "  查看策略输出: tail -f logs/cross_trade_5m.stdout.log"
echo "  停止服务: bash scripts/stop_cross_trade.sh"
echo ""
