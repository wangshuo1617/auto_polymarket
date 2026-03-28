#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 基础运行模式
MODE="${1:---live}"                                  # 运行模式：--live / --dry-run
STAKE_USD="${5:-10.0}"                              # 单笔基础仓位（USDC）
EXIT_MODE="${16:-hold}"                             # 平仓模式：hold / tpsl

# 入场控制
ENTRY_MINUTE="${2:-4}"                              # 入场决策分钟（1-4）
ENTRY_PRECLOSE_SEC="${3:-3}"                        # 入场分钟收盘前秒数
MIN_DIRECTION_DIFF="${4:-39}"                       # 最小方向差值（BTC 与开盘价）
MAX_ENTRY_PRICE="${7:-0.98}"                        # 最大允许入场价格
TOXIC_UTC_HOURS="${13-"0,5,7,16,19"}"              # 跳过交易的 UTC 小时列表
MAX_BTC_CROSS_COUNT="${14:-4}"                      # BTC 跨越开盘价次数上限
MIN_ENTRY_UPDOWN_DIFF="${15:-0.38}"                 # Polymarket UP/DOWN token的最小价差
MAX_AVG_BTC_DELTA="${17:-3.0}"                      # ATR 波动率阈值
MINUTE_CONSISTENCY="${18:-3}"                       # 分钟一致性检查分钟列表，逗号分隔，如1,2,3

# tpsl模式平仓相关控制
MIN_HOLD_BEFORE_CLOSE_SEC="${8:-60}"                # 最短持仓保护秒数
TP_PRICE_CAP="${10:-0.97}"                          # TP 价格上限
TP_VALUE_CAP="${11:-0.15}"                          # TP 收益值上限
SL_TO_TP_RATIO="${12:-0.9}"                         # SL/TP 比例

# 风险仓位管理
ENABLE_RISK_SIZING="${19:-true}"                    # 是否启用动态仓位
RISK_MIN_STAKE_RATIO="${20:-0.20}"                  # 动态仓位最小倍率
RISK_MAX_STAKE_RATIO="${21:-1.2}"                   # 动态仓位最大倍率
CONFIDENCE_BOOST="${22:-true}"                      # 是否启用高置信加仓
CONFIDENCE_BOOST_GE_095="${23:-1.5}"                # 置信度>=0.95 加仓倍率
STAKE_CAP_VERY_HIGH="${24:-0.0}"                    # very_high 风险仓位上限
STAKE_CAP_HIGH="${25:-0.50}"                        # high 风险仓位上限
STAKE_CAP_MEDIUM_HIGH="${26:-0.50}"                 # medium_high 风险仓位上限
MEDIUM_HIGH_THRESHOLD="${27:-0.45}"                 # medium_high 阈值
RISK_W_PRICE="${28:-0.30}"                          # 风险评分：价格权重
RISK_W_DIRECTION="${29:-0.15}"                      # 风险评分：方向权重
RISK_W_STABILITY="${30:-0.55}"                      # 风险评分：稳定性权重
RISK_DIFF_BOOST_THRESHOLD="${31:-0.44}"             # risk_diff boost 启动阈值，当入场风险评分大于该值时，要求更大价差
RISK_DIFF_BOOST_MULTIPLIER="${32:-1.40}"            # risk_diff boost 倍率
CROSS_BORDERLINE_DIFF_MULTIPLIER="${33:-0.0}"       # cross_count 临界倍增系数，当BTC跨越开盘价次数接近上限时，要求更大价差

# 方向确认风控
ENABLE_DIRECTION_CONFIRM_CLOSE="${35:-true}"        # 是否启用方向不一致平仓
DIRECTION_CONFIRM_PRECLOSE_SEC="${34:-15}"          # 方向确认触发秒（距5m结束）
DIRECTION_CONFIRM_MIN_ABS_DIFF="${41:-0.0}"         # 不一致平仓最小绝对价差，确认时价格方向与持仓方向不一致，且与开盘价价差大于该值时，平仓
ENABLE_DIRECTION_CONFIRM_LOW_DIFF_CLOSE="${45:-true}"   # 是否启用方向确认低价差强平
DIRECTION_CONFIRM_LOW_DIFF_THRESHOLD="${46:-10.0}"      # 低价差强平阈值，确认时BTC价格与开盘价差值小于该值时，平仓

# 终盘风控
ENABLE_LAST_SECONDS_REVERSE_GUARD="${36:-true}"     # 是否启用终盘加速反向风控
REVERSE_GUARD_START_SEC="${37:-295}"                # 终盘加速反向风控起始秒
REVERSE_GUARD_LOOKBACK_SEC="${38:-2}"               # 终盘加速反向风控回看秒数
REVERSE_GUARD_BTC_MOVE="${39:-15.0}"                # 终盘加速反向BTC移动阈值
REVERSE_GUARD_REQUIRE_CROSS_OPEN="${40:-true}"      # 是否要求穿越开盘价才平仓
ENABLE_LAST_SECONDS_POSITION_GUARD="${42:-true}"    # 是否启用终盘位置风控
POSITION_GUARD_START_SEC="${43:-295}"               # 终盘位置风控起始秒
POSITION_GUARD_MIN_CONSECUTIVE_SEC="${44:-2}"       # 终盘位置反向连续秒数

# 系统控制
REPORT_INTERVAL_SEC="${6:-3600}"                    # 报告输出间隔（秒）
TRADE_DB_PATH="${9:-}"                              # SQLite 路径，空则走默认配置

# 日志文件
LOG_FILE="logs/5m_trade.stdout.log"
PID_FILE="logs/5m_trade.pid"
USAGE="./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [min_hold_before_close_sec] [trade_db_path] [tp_price_cap] [tp_value_cap] [sl_to_tp_ratio] [toxic_utc_hours_csv] [max_btc_cross_count] [min_entry_updown_diff] [exit_mode] [max_avg_btc_delta] [minute_consistency] ... [direction_confirm_low_diff_threshold]"

print_usage() {
  echo "用法: $USAGE"
  echo "提示: 详细参数说明见脚本顶部各变量旁注释。"
}

if [ "$MODE" = "-h" ] || [ "$MODE" = "--help" ]; then
  print_usage
  exit 0
fi

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

if ! [[ "$DIRECTION_CONFIRM_PRECLOSE_SEC" =~ ^[0-9]+$ ]] || [ "$DIRECTION_CONFIRM_PRECLOSE_SEC" -lt 1 ] || [ "$DIRECTION_CONFIRM_PRECLOSE_SEC" -gt 299 ]; then
  echo "❌ direction_confirm_preclose_sec 必须是 1-299 的整数"
  print_usage
  exit 1
fi

if [ "$ENABLE_DIRECTION_CONFIRM_CLOSE" != "true" ] && [ "$ENABLE_DIRECTION_CONFIRM_CLOSE" != "false" ]; then
  echo "❌ enable_direction_confirm_close 必须是 true 或 false"
  print_usage
  exit 1
fi

if [ "$ENABLE_LAST_SECONDS_REVERSE_GUARD" != "true" ] && [ "$ENABLE_LAST_SECONDS_REVERSE_GUARD" != "false" ]; then
  echo "❌ enable_last_seconds_reverse_guard 必须是 true 或 false"
  print_usage
  exit 1
fi

if ! [[ "$REVERSE_GUARD_START_SEC" =~ ^[0-9]+$ ]] || [ "$REVERSE_GUARD_START_SEC" -lt 1 ] || [ "$REVERSE_GUARD_START_SEC" -gt 299 ]; then
  echo "❌ reverse_guard_start_sec 必须是 1-299 的整数"
  print_usage
  exit 1
fi

if ! [[ "$REVERSE_GUARD_LOOKBACK_SEC" =~ ^[0-9]+$ ]] || [ "$REVERSE_GUARD_LOOKBACK_SEC" -lt 1 ] || [ "$REVERSE_GUARD_LOOKBACK_SEC" -gt 30 ]; then
  echo "❌ reverse_guard_lookback_sec 必须是 1-30 的整数"
  print_usage
  exit 1
fi

if ! [[ "$REVERSE_GUARD_BTC_MOVE" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($REVERSE_GUARD_BTC_MOVE > 0)}"; then
  echo "❌ reverse_guard_btc_move 必须是大于 0 的数字"
  print_usage
  exit 1
fi

if [ "$REVERSE_GUARD_REQUIRE_CROSS_OPEN" != "true" ] && [ "$REVERSE_GUARD_REQUIRE_CROSS_OPEN" != "false" ]; then
  echo "❌ reverse_guard_require_cross_open 必须是 true 或 false"
  print_usage
  exit 1
fi

if ! [[ "$DIRECTION_CONFIRM_MIN_ABS_DIFF" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ direction_confirm_min_abs_diff 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if [ "$ENABLE_DIRECTION_CONFIRM_LOW_DIFF_CLOSE" != "true" ] && [ "$ENABLE_DIRECTION_CONFIRM_LOW_DIFF_CLOSE" != "false" ]; then
  echo "❌ enable_direction_confirm_low_diff_close 必须是 true 或 false"
  print_usage
  exit 1
fi

if ! [[ "$DIRECTION_CONFIRM_LOW_DIFF_THRESHOLD" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ direction_confirm_low_diff_threshold 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if [ "$ENABLE_LAST_SECONDS_POSITION_GUARD" != "true" ] && [ "$ENABLE_LAST_SECONDS_POSITION_GUARD" != "false" ]; then
  echo "❌ enable_last_seconds_position_guard 必须是 true 或 false"
  print_usage
  exit 1
fi

if ! [[ "$POSITION_GUARD_START_SEC" =~ ^[0-9]+$ ]] || [ "$POSITION_GUARD_START_SEC" -lt 1 ] || [ "$POSITION_GUARD_START_SEC" -gt 299 ]; then
  echo "❌ position_guard_start_sec 必须是 1-299 的整数"
  print_usage
  exit 1
fi

if ! [[ "$POSITION_GUARD_MIN_CONSECUTIVE_SEC" =~ ^[0-9]+$ ]] || [ "$POSITION_GUARD_MIN_CONSECUTIVE_SEC" -lt 1 ] || [ "$POSITION_GUARD_MIN_CONSECUTIVE_SEC" -gt 10 ]; then
  echo "❌ position_guard_min_consecutive_sec 必须是 1-10 的整数"
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
echo "信心加仓: $CONFIDENCE_BOOST (>=0.95倍率=$CONFIDENCE_BOOST_GE_095)"
echo "风险等级stake上限: very_high=$STAKE_CAP_VERY_HIGH high=$STAKE_CAP_HIGH medium_high=$STAKE_CAP_MEDIUM_HIGH (阈值=$MEDIUM_HIGH_THRESHOLD)"
echo "风险权重: price=$RISK_W_PRICE direction=$RISK_W_DIRECTION stability=$RISK_W_STABILITY"
echo "risk_diff_boost: threshold=$RISK_DIFF_BOOST_THRESHOLD multiplier=$RISK_DIFF_BOOST_MULTIPLIER"
echo "cross_borderline: diff_multiplier=$CROSS_BORDERLINE_DIFF_MULTIPLIER"
echo "方向一致性确认: enable=$ENABLE_DIRECTION_CONFIRM_CLOSE preclose_sec=$DIRECTION_CONFIRM_PRECLOSE_SEC"
echo "方向一致性确认最小偏离阈值: min_abs_diff=$DIRECTION_CONFIRM_MIN_ABS_DIFF"
echo "方向确认低价差强平: enable=$ENABLE_DIRECTION_CONFIRM_LOW_DIFF_CLOSE low_diff_threshold=$DIRECTION_CONFIRM_LOW_DIFF_THRESHOLD"
echo "终盘反向风控: enable=$ENABLE_LAST_SECONDS_REVERSE_GUARD start_sec=$REVERSE_GUARD_START_SEC lookback_sec=$REVERSE_GUARD_LOOKBACK_SEC btc_move=$REVERSE_GUARD_BTC_MOVE require_cross_open=$REVERSE_GUARD_REQUIRE_CROSS_OPEN"
echo "终盘位置风控: enable=$ENABLE_LAST_SECONDS_POSITION_GUARD start_sec=$POSITION_GUARD_START_SEC min_consecutive_sec=$POSITION_GUARD_MIN_CONSECUTIVE_SEC"
if [ -n "$TRADE_DB_PATH" ]; then
  echo "交易数据库路径: $TRADE_DB_PATH"
else
  echo "交易数据库路径: 使用 config.SQLITE_DB_PATH"
fi
echo "=========================================="

_build_cmd() {
  CMD=(
    uv run 5m_trade.py
    --entry-minute "$ENTRY_MINUTE"
    --entry-preclose-sec "$ENTRY_PRECLOSE_SEC"
    --min-direction-diff "$MIN_DIRECTION_DIFF"
    --stake-usd "$STAKE_USD"
    --report-interval-sec "$REPORT_INTERVAL_SEC"
    --max-entry-price "$MAX_ENTRY_PRICE"
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
    --confidence-boost-ge-095 "$CONFIDENCE_BOOST_GE_095"
    --stake-cap-very-high "$STAKE_CAP_VERY_HIGH"
    --stake-cap-high "$STAKE_CAP_HIGH"
    --stake-cap-medium-high "$STAKE_CAP_MEDIUM_HIGH"
    --medium-high-threshold "$MEDIUM_HIGH_THRESHOLD"
    --risk-w-price "$RISK_W_PRICE"
    --risk-w-direction "$RISK_W_DIRECTION"
    --risk-w-stability "$RISK_W_STABILITY"
    --risk-diff-boost-threshold "$RISK_DIFF_BOOST_THRESHOLD"
    --risk-diff-boost-multiplier "$RISK_DIFF_BOOST_MULTIPLIER"
    --cross-borderline-diff-multiplier "$CROSS_BORDERLINE_DIFF_MULTIPLIER"
    --direction-confirm-preclose-sec "$DIRECTION_CONFIRM_PRECLOSE_SEC"
    --direction-confirm-min-abs-diff "$DIRECTION_CONFIRM_MIN_ABS_DIFF"
    --direction-confirm-low-diff-threshold "$DIRECTION_CONFIRM_LOW_DIFF_THRESHOLD"
    --reverse-guard-start-sec "$REVERSE_GUARD_START_SEC"
    --reverse-guard-lookback-sec "$REVERSE_GUARD_LOOKBACK_SEC"
    --reverse-guard-btc-move "$REVERSE_GUARD_BTC_MOVE"
    --position-guard-start-sec "$POSITION_GUARD_START_SEC"
    --position-guard-min-consecutive-sec "$POSITION_GUARD_MIN_CONSECUTIVE_SEC"
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

  if [ "$ENABLE_DIRECTION_CONFIRM_CLOSE" = "false" ]; then
    CMD+=(--disable-direction-confirm-close)
  fi

  if [ "$ENABLE_DIRECTION_CONFIRM_LOW_DIFF_CLOSE" = "false" ]; then
    CMD+=(--disable-direction-confirm-low-diff-close)
  fi

  if [ "$ENABLE_LAST_SECONDS_REVERSE_GUARD" = "false" ]; then
    CMD+=(--disable-last-seconds-reverse-guard)
  fi

  if [ "$REVERSE_GUARD_REQUIRE_CROSS_OPEN" = "false" ]; then
    CMD+=(--disable-reverse-guard-require-cross-open)
  fi

  if [ "$ENABLE_LAST_SECONDS_POSITION_GUARD" = "false" ]; then
    CMD+=(--disable-last-seconds-position-guard)
  fi

  if [ "$MODE" != "--live" ]; then
    CMD+=(--dry-run)
  fi
}

# --foreground 模式：前台运行，供 systemd 调用（通过环境变量 FOREGROUND=1 激活）
if [ "${FOREGROUND:-}" = "1" ]; then
  echo "[foreground] 前台启动 5m_trade ..."
  _build_cmd
  exec "${CMD[@]}"
fi

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
_build_cmd
nohup "${CMD[@]}" >> "$LOG_FILE" 2>&1 &

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
