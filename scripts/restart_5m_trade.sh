#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

MODE="${1:---live}"
ENTRY_MINUTE="${2:-2}"
ENTRY_PRECLOSE_SEC="${3:-6}"
MIN_DIRECTION_DIFF="${4:-20}"
STAKE_USD="${5:-5.0}"
REPORT_INTERVAL_SEC="${6:-3600}"
MAX_ENTRY_PRICE="${7:-0.80}"
TAKE_PROFIT_SPREAD="${8:-0.15}"
STOP_LOSS_SPREAD="${9:--0.20}"
MIN_HOLD_BEFORE_CLOSE_SEC="${10:-60}"
TRADE_DB_PATH="${11:-}"
LOG_FILE="logs/5m_trade.stdout.log"
PID_FILE="logs/5m_trade.pid"

mkdir -p logs

if [ "$MODE" != "--dry-run" ] && [ "$MODE" != "--live" ]; then
  echo "❌ 模式参数错误：仅支持 --dry-run 或 --live"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path]"
  exit 1
fi

if ! [[ "$ENTRY_MINUTE" =~ ^[1-4]$ ]]; then
  echo "❌ entry_minute 必须是 1-4"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path]"
  exit 1
fi

if ! [[ "$ENTRY_PRECLOSE_SEC" =~ ^[0-9]+$ ]] || [ "$ENTRY_PRECLOSE_SEC" -lt 1 ] || [ "$ENTRY_PRECLOSE_SEC" -gt 59 ]; then
  echo "❌ entry_preclose_sec 必须是 1-59 的整数"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path]"
  exit 1
fi

if ! [[ "$MIN_DIRECTION_DIFF" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($MIN_DIRECTION_DIFF > 0)}"; then
  echo "❌ min_direction_diff 必须是大于 0 的数字"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path]"
  exit 1
fi

if ! [[ "$STAKE_USD" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($STAKE_USD > 0)}"; then
  echo "❌ stake_usd 必须是大于 0 的数字"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path]"
  exit 1
fi

if ! [[ "$REPORT_INTERVAL_SEC" =~ ^[0-9]+$ ]] || [ "$REPORT_INTERVAL_SEC" -le 0 ]; then
  echo "❌ report_interval_sec 必须是大于 0 的整数"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path]"
  exit 1
fi

if ! [[ "$MAX_ENTRY_PRICE" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($MAX_ENTRY_PRICE > 0)}"; then
  echo "❌ max_entry_price 必须是大于 0 的数字"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path]"
  exit 1
fi

if ! [[ "$TAKE_PROFIT_SPREAD" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ take_profit_spread 必须是数字"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path]"
  exit 1
fi

if ! [[ "$STOP_LOSS_SPREAD" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ stop_loss_spread 必须是数字"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path]"
  exit 1
fi

if ! [[ "$MIN_HOLD_BEFORE_CLOSE_SEC" =~ ^[0-9]+$ ]]; then
  echo "❌ min_hold_before_close_sec 必须是大于等于 0 的整数"
  echo "用法: ./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path]"
  exit 1
fi

echo "=========================================="
echo "重启 5m_trade 服务"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
echo "模式参数: $MODE"
echo "建仓分钟: $ENTRY_MINUTE"
echo "收盘前抢跑秒数: $ENTRY_PRECLOSE_SEC"
echo "最小方向差值: $MIN_DIRECTION_DIFF"
echo "单笔仓位金额(USDC): $STAKE_USD"
echo "报告间隔(秒): $REPORT_INTERVAL_SEC"
echo "允许最高开仓价: $MAX_ENTRY_PRICE"
echo "止盈价差: $TAKE_PROFIT_SPREAD"
echo "止损价差: $STOP_LOSS_SPREAD"
echo "最短持仓保护秒数: $MIN_HOLD_BEFORE_CLOSE_SEC"
if [ -n "$TRADE_DB_PATH" ]; then
  echo "交易数据库路径: $TRADE_DB_PATH"
else
  echo "交易数据库路径: 使用 config.SQLITE_DB_PATH"
fi
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
CMD=(
  uv run 5m_trade.py
  --entry-minute "$ENTRY_MINUTE"
  --entry-preclose-sec "$ENTRY_PRECLOSE_SEC"
  --min-direction-diff "$MIN_DIRECTION_DIFF"
  --stake-usd "$STAKE_USD"
  --report-interval-sec "$REPORT_INTERVAL_SEC"
  --max-entry-price "$MAX_ENTRY_PRICE"
  --take-profit-spread "$TAKE_PROFIT_SPREAD"
  --stop-loss-spread "$STOP_LOSS_SPREAD"
  --min-hold-before-close-sec "$MIN_HOLD_BEFORE_CLOSE_SEC"
)

if [ -n "$TRADE_DB_PATH" ]; then
  CMD+=(--trade-db-path "$TRADE_DB_PATH")
fi

if [ "$MODE" = "--live" ]; then
  nohup "${CMD[@]}" >> "$LOG_FILE" 2>&1 &
else
  nohup "${CMD[@]}" --dry-run >> "$LOG_FILE" 2>&1 &
fi

NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 2

echo "[3/3] 校验进程状态..."
if ps -p "$NEW_PID" > /dev/null 2>&1; then
  echo "✅ 5m_trade 启动成功"
  echo "PID: $NEW_PID"
  echo "PID 文件: $PID_FILE"
  echo "业务日志(轮转): logs/5m_trade.log"
  echo "诊断日志(轮转): logs/5m_trade_diag.log"
  echo "进程输出日志: $LOG_FILE"
  echo "查看业务日志: tail -f logs/5m_trade.log"
else
  echo "❌ 5m_trade 启动失败，请检查日志: $LOG_FILE"
  exit 1
fi
