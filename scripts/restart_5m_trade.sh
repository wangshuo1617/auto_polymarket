#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 强制 5m 交易路径默认使用 trade 账号 profile（可由外部环境覆盖）
export POLYMARKET_PROFILE="${POLYMARKET_PROFILE:-trade}"

# 加载参数覆盖文件（由 Dashboard 编辑保存）
_OVERRIDE_FILE="$PROJECT_ROOT/config/5m_trade_params.env"
if [ -f "$_OVERRIDE_FILE" ]; then
  echo "[config] 加载参数覆盖文件: $_OVERRIDE_FILE"
  source "$_OVERRIDE_FILE"
fi

# 基础运行模式
MODE="${MODE:-${1:---live}}"                                  # 运行模式：--live / --dry-run
STAKE_USD="${STAKE_USD:-${5:-10.0}}"                          # 单笔基础仓位（USDC）
EXIT_MODE="${EXIT_MODE:-${16:-hold}}"                         # 平仓模式：hold / tpsl

# 入场控制
ENTRY_MINUTE="${ENTRY_MINUTE:-${2:-4}}"                       # 入场决策分钟（1-4）
ENTRY_PRECLOSE_SEC="${ENTRY_PRECLOSE_SEC:-${3:-3}}"           # 入场分钟收盘前秒数
MIN_DIRECTION_DIFF="${MIN_DIRECTION_DIFF:-${4:-39}}"          # 最小方向差值（BTC 与开盘价）
MAX_ENTRY_PRICE="${MAX_ENTRY_PRICE:-${7:-0.98}}"              # 最大允许入场价格
TOXIC_UTC_HOURS="${TOXIC_UTC_HOURS-${13-"0,5,7,16,19"}}"     # 跳过交易的 UTC 小时列表；空字符串=不跳过
MAX_BTC_CROSS_COUNT="${MAX_BTC_CROSS_COUNT:-${14:-4}}"        # BTC 跨越开盘价次数上限
MIN_ENTRY_UPDOWN_DIFF="${MIN_ENTRY_UPDOWN_DIFF:-${15:-0.38}}" # Polymarket UP/DOWN token的最小价差
MAX_AVG_BTC_DELTA="${MAX_AVG_BTC_DELTA:-${17:-3.0}}"          # ATR 波动率阈值
MINUTE_CONSISTENCY="${MINUTE_CONSISTENCY-${18:-3}}"            # 分钟一致性检查分钟列表，逗号分隔，如1,2,3；空字符串=禁用

# tpsl模式平仓相关控制
MIN_HOLD_BEFORE_CLOSE_SEC="${MIN_HOLD_BEFORE_CLOSE_SEC:-${8:-60}}"  # 最短持仓保护秒数
TP_PRICE_CAP="${TP_PRICE_CAP:-${10:-0.97}}"                   # TP 价格上限
TP_VALUE_CAP="${TP_VALUE_CAP:-${11:-0.15}}"                   # TP 收益值上限
SL_TO_TP_RATIO="${SL_TO_TP_RATIO:-${12:-0.9}}"               # SL/TP 比例

# 风险仓位管理
ENABLE_RISK_SIZING="${ENABLE_RISK_SIZING:-${19:-true}}"       # 是否启用动态仓位
RISK_MIN_STAKE_RATIO="${RISK_MIN_STAKE_RATIO:-${20:-0.30}}"   # 动态仓位最小倍率
RISK_MAX_STAKE_RATIO="${RISK_MAX_STAKE_RATIO:-${21:-1.5}}"    # 动态仓位最大倍率
CONFIDENCE_BOOST="${CONFIDENCE_BOOST:-${22:-true}}"           # 是否启用高置信加仓
CONFIDENCE_BOOST_GE_095="${CONFIDENCE_BOOST_GE_095:-${23:-1.3}}" # 置信度>=0.95 加仓倍率
STAKE_CAP_VERY_HIGH="${STAKE_CAP_VERY_HIGH:-${24:-0.20}}"     # very_high 风险仓位上限
STAKE_CAP_HIGH="${STAKE_CAP_HIGH:-${25:-0.50}}"               # high 风险仓位上限
STAKE_CAP_MEDIUM_HIGH="${STAKE_CAP_MEDIUM_HIGH:-${26:-0.70}}" # medium_high 风险仓位上限
MEDIUM_HIGH_THRESHOLD="${MEDIUM_HIGH_THRESHOLD:-${27:-0.45}}"  # medium_high 阈值
RISK_W_PRICE="${RISK_W_PRICE:-${28:-0.15}}"                   # 风险评分：价格权重
RISK_W_DIRECTION="${RISK_W_DIRECTION:-${29:-0.35}}"           # 风险评分：方向权重
RISK_W_STABILITY="${RISK_W_STABILITY:-${30:-0.50}}"           # 风险评分：稳定性权重
RISK_DIFF_BOOST_THRESHOLD="${RISK_DIFF_BOOST_THRESHOLD:-${31:-0.44}}"     # risk_diff boost 启动阈值
RISK_DIFF_BOOST_MULTIPLIER="${RISK_DIFF_BOOST_MULTIPLIER:-${32:-1.40}}"   # risk_diff boost 倍率
CROSS_BORDERLINE_DIFF_MULTIPLIER="${CROSS_BORDERLINE_DIFF_MULTIPLIER:-${33:-0.0}}" # cross_count 临界倍增系数

# 最后一分钟接近度风控
ENABLE_LAST_MIN_PROXIMITY_CLOSE="${ENABLE_LAST_MIN_PROXIMITY_CLOSE:-${35:-true}}"  # 最后一分钟触及开盘价附近时平仓
LAST_MIN_PROXIMITY_THRESHOLD="${LAST_MIN_PROXIMITY_THRESHOLD:-${34:-10.0}}"        # 平仓阈值（距开盘价$）

# 最后一分钟 Token bid 急跌止损
ENABLE_LAST_MIN_BID_DROP_CLOSE="${ENABLE_LAST_MIN_BID_DROP_CLOSE:-true}"           # 最后一分钟 Token bid 急跌时平仓
LAST_MIN_BID_DROP_THRESHOLD="${LAST_MIN_BID_DROP_THRESHOLD:-0.30}"                 # Bid/entry 比率跌幅阈值
LAST_MIN_BID_DROP_LOOKBACK_SEC="${LAST_MIN_BID_DROP_LOOKBACK_SEC:-1.0}"            # 急跌回看秒数
LAST_MIN_BID_DROP_START_SEC="${LAST_MIN_BID_DROP_START_SEC:-240.0}"                # 急跌检测启用时刻（窗口内秒数）
LAST_MIN_BID_DROP_FLOOR="${LAST_MIN_BID_DROP_FLOOR:-0.10}"                         # Bid/entry 比率下限（低于此不卖）

# Binance 前哨止损
ENABLE_BINANCE_EARLY_SL="${ENABLE_BINANCE_EARLY_SL:-true}"                        # 启用Binance实时价格前哨止损
BINANCE_SL_START_SEC="${BINANCE_SL_START_SEC:-240.0}"                             # Binance前哨止损启用时刻（窗口内秒数）
BINANCE_SL_PROXIMITY="${BINANCE_SL_PROXIMITY:-3.0}"                               # Binance价格距开盘价阈值（$）
ENABLE_BINANCE_TRADE_IMBALANCE_SL="${ENABLE_BINANCE_TRADE_IMBALANCE_SL:-true}"    # 启用Binance成交流不平衡止损
BINANCE_SL_IMBALANCE_RATIO="${BINANCE_SL_IMBALANCE_RATIO:-0.80}"                  # 成交流卖方占比阈值（0-1）
BINANCE_SL_IMBALANCE_START_SEC="${BINANCE_SL_IMBALANCE_START_SEC:-270.0}"         # 成交流不平衡止损启用时刻（窗口内秒数）
BINANCE_SL_IMBALANCE_WINDOW_SEC="${BINANCE_SL_IMBALANCE_WINDOW_SEC:-3.0}"         # 成交流不平衡计算回看秒数
BINANCE_SL_IMBALANCE_MIN_PROXIMITY="${BINANCE_SL_IMBALANCE_MIN_PROXIMITY:-15.0}"  # 成交流止损需价格距开盘<此值($)

# 系统控制
REPORT_INTERVAL_SEC="${REPORT_INTERVAL_SEC:-${6:-3600}}"      # 报告输出间隔（秒）
ENABLE_DB_TICK_VALIDATION="${ENABLE_DB_TICK_VALIDATION:-${47:-true}}"  # 是否启用DB tick交叉验证

# 偏离入场模式
ENABLE_DEVIATION_ENTRY="${ENABLE_DEVIATION_ENTRY:-false}"                     # 启用偏离入场模式
DEVIATION_ENTRY_THRESHOLD="${DEVIATION_ENTRY_THRESHOLD:-40.0}"                # BTC偏离开盘价$阈值
DEVIATION_ENTRY_START_SEC="${DEVIATION_ENTRY_START_SEC:-60.0}"                # 偏离入场最早生效时间(窗口内秒)
DEVIATION_ENTRY_END_SEC="${DEVIATION_ENTRY_END_SEC:-240.0}"                   # 偏离入场最晚截止时间

# DCA 加仓
ENABLE_DCA="${ENABLE_DCA:-false}"                                            # 启用DCA加仓
DCA_MAX_ADDS="${DCA_MAX_ADDS:-4}"                                            # 最大追加次数
DCA_INTERVAL_SEC="${DCA_INTERVAL_SEC:-15.0}"                                 # 两次DCA最小间隔(秒)
DCA_DEVIATION_STEP="${DCA_DEVIATION_STEP:-20.0}"                             # 每次追加需额外偏离增量($)
DCA_END_SEC="${DCA_END_SEC:-270.0}"                                          # DCA最晚截止时间
DCA_MIN_CONFIDENCE="${DCA_MIN_CONFIDENCE:-0.3}"                              # DCA最低信心分
DCA_MAX_ENTRY_PRICE="${DCA_MAX_ENTRY_PRICE:-0.95}"                           # DCA加仓最高token价格
DCA_W_DEVIATION="${DCA_W_DEVIATION:-0.25}"                                   # 信心权重：偏离强度
DCA_W_ATR="${DCA_W_ATR:-0.20}"                                               # 信心权重：ATR
DCA_W_CROSS="${DCA_W_CROSS:-0.20}"                                           # 信心权重：cross
DCA_W_PRICE="${DCA_W_PRICE:-0.15}"                                           # 信心权重：token价格
DCA_W_TIME="${DCA_W_TIME:-0.10}"                                             # 信心权重：剩余时间
DCA_W_POSITION="${DCA_W_POSITION:-0.10}"                                     # 信心权重：已持仓量

# 方向修正
ENABLE_DIRECTION_REVERSAL="${ENABLE_DIRECTION_REVERSAL:-false}"              # 启用方向修正
REVERSAL_THRESHOLD="${REVERSAL_THRESHOLD:-50.0}"                             # BTC反向偏离$阈值
REVERSAL_START_SEC="${REVERSAL_START_SEC:-120.0}"                            # 方向修正最早生效时间
REVERSAL_END_SEC="${REVERSAL_END_SEC:-240.0}"                                # 方向修正最晚截止时间
REVERSAL_SIZE_MULTIPLIER="${REVERSAL_SIZE_MULTIPLIER:-1.2}"                  # 修正仓位倍数

# 连败缩仓
ENABLE_STREAK_SIZING="${ENABLE_STREAK_SIZING:-false}"                        # 启用连败缩仓
STREAK_LOSS_THRESHOLD="${STREAK_LOSS_THRESHOLD:-3}"                          # 连败N次后开始缩仓
STREAK_SHRINK_FACTOR="${STREAK_SHRINK_FACTOR:-0.5}"                          # 缩仓比例
STREAK_MAX_SHRINKS="${STREAK_MAX_SHRINKS:-3}"                                # 最大连续缩减次数

# 日志文件
LOG_FILE="logs/5m_trade.stdout.log"
PID_FILE="logs/5m_trade.pid"
USAGE="./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [min_hold_before_close_sec] [trade_db_path] [tp_price_cap] [tp_value_cap] [sl_to_tp_ratio] [toxic_utc_hours_csv] [max_btc_cross_count] [min_entry_updown_diff] [exit_mode] [max_avg_btc_delta] [minute_consistency] ... [last_min_proximity_threshold]"

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

if [ "$ENABLE_LAST_MIN_PROXIMITY_CLOSE" != "true" ] && [ "$ENABLE_LAST_MIN_PROXIMITY_CLOSE" != "false" ]; then
  echo "❌ enable_last_min_proximity_close 必须是 true 或 false"
  print_usage
  exit 1
fi

if ! [[ "$LAST_MIN_PROXIMITY_THRESHOLD" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ last_min_proximity_threshold 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if [ "$ENABLE_LAST_MIN_BID_DROP_CLOSE" != "true" ] && [ "$ENABLE_LAST_MIN_BID_DROP_CLOSE" != "false" ]; then
  echo "❌ enable_last_min_bid_drop_close 必须是 true 或 false"
  print_usage
  exit 1
fi

if ! [[ "$LAST_MIN_BID_DROP_THRESHOLD" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ last_min_bid_drop_threshold 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if ! [[ "$LAST_MIN_BID_DROP_LOOKBACK_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ last_min_bid_drop_lookback_sec 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if ! [[ "$LAST_MIN_BID_DROP_START_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ last_min_bid_drop_start_sec 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if ! [[ "$LAST_MIN_BID_DROP_FLOOR" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ last_min_bid_drop_floor 必须是大于等于 0 的数字"
  print_usage
  exit 1
fi

if [ "$ENABLE_BINANCE_EARLY_SL" != "true" ] && [ "$ENABLE_BINANCE_EARLY_SL" != "false" ]; then
  echo "❌ enable_binance_early_sl 必须是 true 或 false"; print_usage; exit 1
fi
if ! [[ "$BINANCE_SL_START_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ binance_sl_start_sec 必须是数字"; print_usage; exit 1
fi
if ! [[ "$BINANCE_SL_PROXIMITY" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ binance_sl_proximity 必须是数字"; print_usage; exit 1
fi
if [ "$ENABLE_BINANCE_TRADE_IMBALANCE_SL" != "true" ] && [ "$ENABLE_BINANCE_TRADE_IMBALANCE_SL" != "false" ]; then
  echo "❌ enable_binance_trade_imbalance_sl 必须是 true 或 false"; print_usage; exit 1
fi
if ! [[ "$BINANCE_SL_IMBALANCE_RATIO" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ binance_sl_imbalance_ratio 必须是数字"; print_usage; exit 1
fi
if ! [[ "$BINANCE_SL_IMBALANCE_START_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ binance_sl_imbalance_start_sec 必须是数字"; print_usage; exit 1
fi
if ! [[ "$BINANCE_SL_IMBALANCE_WINDOW_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ binance_sl_imbalance_window_sec 必须是数字"; print_usage; exit 1
fi
if ! [[ "$BINANCE_SL_IMBALANCE_MIN_PROXIMITY" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
  echo "❌ binance_sl_imbalance_min_proximity 必须是数字"; print_usage; exit 1
fi

if [ "$ENABLE_DB_TICK_VALIDATION" != "true" ] && [ "$ENABLE_DB_TICK_VALIDATION" != "false" ]; then
  echo "❌ enable_db_tick_validation 必须是 true 或 false"
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
echo "账号 profile: $POLYMARKET_PROFILE"
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
echo "最后一分钟接近度风控: enable=$ENABLE_LAST_MIN_PROXIMITY_CLOSE threshold=$LAST_MIN_PROXIMITY_THRESHOLD"
echo "Token bid急跌止损: enable=$ENABLE_LAST_MIN_BID_DROP_CLOSE threshold=$LAST_MIN_BID_DROP_THRESHOLD lookback=${LAST_MIN_BID_DROP_LOOKBACK_SEC}s start=${LAST_MIN_BID_DROP_START_SEC}s floor=$LAST_MIN_BID_DROP_FLOOR"
echo "Binance前哨止损: enable=$ENABLE_BINANCE_EARLY_SL proximity=\$${BINANCE_SL_PROXIMITY} start=${BINANCE_SL_START_SEC}s"
echo "Binance成交流止损: enable=$ENABLE_BINANCE_TRADE_IMBALANCE_SL ratio=$BINANCE_SL_IMBALANCE_RATIO window=${BINANCE_SL_IMBALANCE_WINDOW_SEC}s start=${BINANCE_SL_IMBALANCE_START_SEC}s min_proximity=\$${BINANCE_SL_IMBALANCE_MIN_PROXIMITY}"
echo "DB tick交叉验证: enable=$ENABLE_DB_TICK_VALIDATION"
echo "偏离入场: enable=$ENABLE_DEVIATION_ENTRY threshold=\$${DEVIATION_ENTRY_THRESHOLD} start=${DEVIATION_ENTRY_START_SEC}s end=${DEVIATION_ENTRY_END_SEC}s"
echo "DCA加仓: enable=$ENABLE_DCA max_adds=$DCA_MAX_ADDS interval=${DCA_INTERVAL_SEC}s step=\$${DCA_DEVIATION_STEP} end=${DCA_END_SEC}s min_conf=$DCA_MIN_CONFIDENCE max_price=$DCA_MAX_ENTRY_PRICE"
echo "DCA权重: deviation=$DCA_W_DEVIATION atr=$DCA_W_ATR cross=$DCA_W_CROSS price=$DCA_W_PRICE time=$DCA_W_TIME position=$DCA_W_POSITION"
echo "方向修正: enable=$ENABLE_DIRECTION_REVERSAL threshold=\$${REVERSAL_THRESHOLD} start=${REVERSAL_START_SEC}s end=${REVERSAL_END_SEC}s size_mult=$REVERSAL_SIZE_MULTIPLIER"
echo "连败缩仓: enable=$ENABLE_STREAK_SIZING threshold=$STREAK_LOSS_THRESHOLD factor=$STREAK_SHRINK_FACTOR max_shrinks=$STREAK_MAX_SHRINKS"
echo "数据库: PG_DSN 环境变量"
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
    --last-min-proximity-threshold "$LAST_MIN_PROXIMITY_THRESHOLD"
    --last-min-bid-drop-threshold "$LAST_MIN_BID_DROP_THRESHOLD"
    --last-min-bid-drop-lookback-sec "$LAST_MIN_BID_DROP_LOOKBACK_SEC"
    --last-min-bid-drop-start-sec "$LAST_MIN_BID_DROP_START_SEC"
    --last-min-bid-drop-floor "$LAST_MIN_BID_DROP_FLOOR"
    --binance-sl-start-sec "$BINANCE_SL_START_SEC"
    --binance-sl-proximity "$BINANCE_SL_PROXIMITY"
    --binance-sl-imbalance-ratio "$BINANCE_SL_IMBALANCE_RATIO"
    --binance-sl-imbalance-start-sec "$BINANCE_SL_IMBALANCE_START_SEC"
    --binance-sl-imbalance-window-sec "$BINANCE_SL_IMBALANCE_WINDOW_SEC"
    --binance-sl-imbalance-min-proximity "$BINANCE_SL_IMBALANCE_MIN_PROXIMITY"
    --deviation-entry-threshold "$DEVIATION_ENTRY_THRESHOLD"
    --deviation-entry-start-sec "$DEVIATION_ENTRY_START_SEC"
    --deviation-entry-end-sec "$DEVIATION_ENTRY_END_SEC"
    --reversal-threshold "$REVERSAL_THRESHOLD"
    --reversal-start-sec "$REVERSAL_START_SEC"
    --reversal-end-sec "$REVERSAL_END_SEC"
    --reversal-size-multiplier "$REVERSAL_SIZE_MULTIPLIER"
    --dca-max-adds "$DCA_MAX_ADDS"
    --dca-interval-sec "$DCA_INTERVAL_SEC"
    --dca-deviation-step "$DCA_DEVIATION_STEP"
    --dca-end-sec "$DCA_END_SEC"
    --dca-min-confidence "$DCA_MIN_CONFIDENCE"
    --dca-max-entry-price "$DCA_MAX_ENTRY_PRICE"
    --dca-w-deviation "$DCA_W_DEVIATION"
    --dca-w-atr "$DCA_W_ATR"
    --dca-w-cross "$DCA_W_CROSS"
    --dca-w-price "$DCA_W_PRICE"
    --dca-w-time "$DCA_W_TIME"
    --dca-w-position "$DCA_W_POSITION"
    --streak-loss-threshold "$STREAK_LOSS_THRESHOLD"
    --streak-shrink-factor "$STREAK_SHRINK_FACTOR"
    --streak-max-shrinks "$STREAK_MAX_SHRINKS"
  )

  CMD+=(--minute-consistency "$MINUTE_CONSISTENCY")

  if [ "$ENABLE_RISK_SIZING" = "false" ]; then
    CMD+=(--disable-risk-sizing)
  fi

  if [ "$CONFIDENCE_BOOST" = "false" ]; then
    CMD+=(--disable-confidence-boost)
  fi

  if [ "$ENABLE_LAST_MIN_PROXIMITY_CLOSE" = "false" ]; then
    CMD+=(--disable-last-min-proximity-close)
  fi

  if [ "$ENABLE_LAST_MIN_BID_DROP_CLOSE" = "false" ]; then
    CMD+=(--disable-last-min-bid-drop-close)
  fi

  if [ "$ENABLE_BINANCE_EARLY_SL" = "false" ]; then
    CMD+=(--disable-binance-early-sl)
  fi

  if [ "$ENABLE_BINANCE_TRADE_IMBALANCE_SL" = "false" ]; then
    CMD+=(--disable-binance-trade-imbalance-sl)
  fi

  if [ "$ENABLE_DB_TICK_VALIDATION" = "false" ]; then
    CMD+=(--disable-db-tick-validation)
  fi

  if [ "$ENABLE_DEVIATION_ENTRY" = "true" ]; then
    CMD+=(--enable-deviation-entry)
  fi

  if [ "$ENABLE_DIRECTION_REVERSAL" = "true" ]; then
    CMD+=(--enable-direction-reversal)
  fi

  if [ "$ENABLE_DCA" = "true" ]; then
    CMD+=(--enable-dca)
  fi

  if [ "$ENABLE_STREAK_SIZING" = "true" ]; then
    CMD+=(--enable-streak-sizing)
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
