#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

MODE="${1:---live}"
ENTRY_MINUTE="${2:-4}"
ENTRY_PRECLOSE_SEC="${3:-9}"
MIN_DIRECTION_DIFF="${4:-30}"
MAX_ENTRY_PRICE="${7:-0.95}"
STAKE_USD="${5:-20.0}"
MIN_HOLD_BEFORE_CLOSE_SEC="${10:-70}"
TP_PRICE_CAP="${12:-0.99}"
TP_VALUE_CAP="${13:-0.15}"
SL_TO_TP_RATIO="${14:-0.9}"
MAX_BTC_CROSS_COUNT="${16:-4}"
MIN_ENTRY_UPDOWN_DIFF="${17:-0.38}"
EXIT_MODE="${18:-hold}"
MAX_AVG_BTC_DELTA="${19:-3.0}"
MINUTE_CONSISTENCY="${20:-3}"
ENABLE_RISK_SIZING="${21:-true}"
RISK_MIN_STAKE_RATIO="${22:-0.20}"
RISK_MAX_STAKE_RATIO="${23:-1.2}"
CONFIDENCE_BOOST="${24:-true}"

REPORT_INTERVAL_SEC="${6:-3600}"
TAKE_PROFIT_SPREAD="${8:-0.15}"
STOP_LOSS_SPREAD="${9:--0.20}"
TRADE_DB_PATH="${11:-}"
TOXIC_UTC_HOURS="${15-"0,5,7,16,19"}"
LOG_FILE="logs/5m_trade.stdout.log"
PID_FILE="logs/5m_trade.pid"
USAGE="./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path] [tp_price_cap] [tp_value_cap] [sl_to_tp_ratio] [toxic_utc_hours_csv] [max_btc_cross_count] [min_entry_updown_diff]"

print_usage() {
  echo "用法: $USAGE"
}

mkdir -p logs

if [ "$MODE" != "--dry-run" ] && [ "$MODE" != "--live" ]; then
  echo "❌ 模式参数错误：仅支持 --dry-run 或 --live"
  print_usage
  exit 1
fi

if ! [[ "$ENTRY_MINUTE" =~ ^[1-4]$ ]]; then
  echo "❌ entry_minute 必须是 1-4"
  print_usage
  exit 1
fi

if ! [[ "$ENTRY_PRECLOSE_SEC" =~ ^[0-9]+$ ]] || [ "$ENTRY_PRECLOSE_SEC" -lt 1 ] || [ "$ENTRY_PRECLOSE_SEC" -gt 59 ]; then
  echo "❌ entry_preclose_sec 必须是 1-59 的整数"
  print_usage
  exit 1
fi

if ! [[ "$MIN_DIRECTION_DIFF" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($MIN_DIRECTION_DIFF > 0)}"; then
  echo "❌ min_direction_diff 必须是大于 0 的数字"
  print_usage
  exit 1
fi

if ! [[ "$STAKE_USD" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($STAKE_USD > 0)}"; then
  echo "❌ stake_usd 必须是大于 0 的数字"
  print_usage
  exit 1
fi

if ! [[ "$REPORT_INTERVAL_SEC" =~ ^[0-9]+$ ]] || [ "$REPORT_INTERVAL_SEC" -le 0 ]; then
  echo "❌ report_interval_sec 必须是大于 0 的整数"
  print_usage
  exit 1
fi

if ! [[ "$MAX_ENTRY_PRICE" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($MAX_ENTRY_PRICE > 0)}"; then
  echo "❌ max_entry_price 必须是大于 0 的数字"
  print_usage
  exit 1
fi

if ! [[ "$TAKE_PROFIT_SPREAD" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ take_profit_spread 必须是数字"
  print_usage
  exit 1
fi

if ! [[ "$STOP_LOSS_SPREAD" =~ ^-?[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ stop_loss_spread 必须是数字"
  print_usage
  exit 1
fi

if ! [[ "$MIN_HOLD_BEFORE_CLOSE_SEC" =~ ^[0-9]+$ ]]; then
  echo "❌ min_hold_before_close_sec 必须是大于等于 0 的整数"
  print_usage
  exit 1
fi

if ! [[ "$TP_PRICE_CAP" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($TP_PRICE_CAP > 0)}"; then
  echo "❌ tp_price_cap 必须是大于 0 的数字"
  print_usage
  exit 1
fi

if ! [[ "$TP_VALUE_CAP" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($TP_VALUE_CAP >= 0)}"; then
  echo "❌ tp_value_cap 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if ! [[ "$SL_TO_TP_RATIO" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($SL_TO_TP_RATIO > 0)}"; then
  echo "❌ sl_to_tp_ratio 必须是大于 0 的数字"
  print_usage
  exit 1
fi

if ! [[ "$MAX_BTC_CROSS_COUNT" =~ ^[0-9]+$ ]]; then
  echo "❌ max_btc_cross_count 必须是大于等于 0 的整数"
  print_usage
  exit 1
fi

if ! [[ "$MIN_ENTRY_UPDOWN_DIFF" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ min_entry_updown_diff 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if [ "$EXIT_MODE" != "tpsl" ] && [ "$EXIT_MODE" != "hold" ]; then
  echo "❌ exit_mode 必须是 tpsl 或 hold"
  print_usage
  exit 1
fi

if ! [[ "$MAX_AVG_BTC_DELTA" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ max_avg_btc_delta 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

# minute_consistency: 逗号分隔的分钟列表 (如 "1,2,3") 或空字符串
if [ -n "$MINUTE_CONSISTENCY" ]; then
  IFS=',' read -r -a MC_PARTS <<< "$MINUTE_CONSISTENCY"
  for mc_part in "${MC_PARTS[@]}"; do
    mc_trimmed="$(echo "$mc_part" | tr -d '[:space:]')"
    if [ -z "$mc_trimmed" ]; then continue; fi
    if ! [[ "$mc_trimmed" =~ ^[1-4]$ ]]; then
      echo "❌ minute_consistency 每项必须是 1-4 的整数，当前值: $mc_trimmed"
      print_usage
      exit 1
    fi
  done
fi

if [ "$ENABLE_RISK_SIZING" != "true" ] && [ "$ENABLE_RISK_SIZING" != "false" ]; then
  echo "❌ enable_risk_sizing 必须是 true 或 false"
  print_usage
  exit 1
fi

if ! [[ "$RISK_MIN_STAKE_RATIO" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ risk_min_stake_ratio 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if ! [[ "$RISK_MAX_STAKE_RATIO" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ risk_max_stake_ratio 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if [ "$CONFIDENCE_BOOST" != "true" ] && [ "$CONFIDENCE_BOOST" != "false" ]; then
  echo "❌ confidence_boost 必须是 true 或 false"
  print_usage
  exit 1
fi

if [ -n "$TOXIC_UTC_HOURS" ]; then
  IFS=',' read -r -a TOXIC_HOUR_PARTS <<< "$TOXIC_UTC_HOURS"
  for hour_part in "${TOXIC_HOUR_PARTS[@]}"; do
    hour_trimmed="$(echo "$hour_part" | tr -d '[:space:]')"
    if [ -z "$hour_trimmed" ]; then
      continue
    fi
    if ! [[ "$hour_trimmed" =~ ^[0-9]+$ ]] || [ "$hour_trimmed" -lt 0 ] || [ "$hour_trimmed" -gt 23 ]; then
      echo "❌ toxic_utc_hours 必须是 0-23 的逗号分隔整数，或空字符串"
      print_usage
      exit 1
    fi
  done
fi

if [ -n "$TOXIC_UTC_HOURS" ]; then
  # 规范化：去掉空白和空项，避免传入如 "16, 19,20" 的格式噪音。
  IFS=',' read -r -a TOXIC_HOUR_PARTS <<< "$TOXIC_UTC_HOURS"
  NORMALIZED_TOXIC_HOURS=()
  for hour_part in "${TOXIC_HOUR_PARTS[@]}"; do
    hour_trimmed="$(echo "$hour_part" | tr -d '[:space:]')"
    if [ -n "$hour_trimmed" ]; then
      NORMALIZED_TOXIC_HOURS+=("$hour_trimmed")
    fi
  done
  if [ "${#NORMALIZED_TOXIC_HOURS[@]}" -gt 0 ]; then
    TOXIC_UTC_HOURS="$(IFS=,; echo "${NORMALIZED_TOXIC_HOURS[*]}")"
  else
    TOXIC_UTC_HOURS=""
  fi
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
echo "动态止盈参数: tp_price_cap=$TP_PRICE_CAP tp_value_cap=$TP_VALUE_CAP"
echo "动态止损参数: sl_to_tp_ratio=$SL_TO_TP_RATIO (SL值=TP值*sl_to_tp_ratio)"
if [ -n "$TOXIC_UTC_HOURS" ]; then
  echo "有毒时间段(UTC小时): $TOXIC_UTC_HOURS"
else
  echo "有毒时间段(UTC小时): 无（不跳过任何时段）"
fi
echo "兼容参数(当前策略未使用): take_profit_spread=$TAKE_PROFIT_SPREAD stop_loss_spread=$STOP_LOSS_SPREAD"
echo "最短持仓保护秒数: $MIN_HOLD_BEFORE_CLOSE_SEC"
echo "BTC越过开盘价最大次数: $MAX_BTC_CROSS_COUNT"
echo "UP/DOWN最小价差: $MIN_ENTRY_UPDOWN_DIFF"
echo "ATR波动率上限: $MAX_AVG_BTC_DELTA"
if [ -n "$MINUTE_CONSISTENCY" ]; then
  echo "分钟一致性检查: 第 $MINUTE_CONSISTENCY 分钟"
else
  echo "分钟一致性检查: 已禁用"
fi
echo "平仓模式: $EXIT_MODE"
echo "风险仓位管理: $ENABLE_RISK_SIZING (min=$RISK_MIN_STAKE_RATIO max=$RISK_MAX_STAKE_RATIO)"
echo "信心加仓: $CONFIDENCE_BOOST"
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
  --tp-price-cap "$TP_PRICE_CAP"
  --tp-value-cap "$TP_VALUE_CAP"
  --sl-to-tp-ratio "$SL_TO_TP_RATIO"
  --toxic-utc-hours "$TOXIC_UTC_HOURS"
  --min-hold-before-close-sec "$MIN_HOLD_BEFORE_CLOSE_SEC"
  --max-btc-cross-count "$MAX_BTC_CROSS_COUNT"
  --min-entry-updown-diff "$MIN_ENTRY_UPDOWN_DIFF"
  --max-avg-btc-delta "$MAX_AVG_BTC_DELTA"
  --exit-mode "$EXIT_MODE"
  --risk-min-stake-ratio "$RISK_MIN_STAKE_RATIO"
  --risk-max-stake-ratio "$RISK_MAX_STAKE_RATIO"
)

if [ -n "$TRADE_DB_PATH" ]; then
  CMD+=(--trade-db-path "$TRADE_DB_PATH")
fi

CMD+=(--minute-consistency "$MINUTE_CONSISTENCY")

if [ "$ENABLE_RISK_SIZING" = "false" ]; then
  CMD+=(--disable-risk-sizing)
fi

if [ "$CONFIDENCE_BOOST" = "false" ]; then
  CMD+=(--disable-confidence-boost)
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
