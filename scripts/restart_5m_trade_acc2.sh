#!/bin/bash
# ============================================================================
#  restart_5m_trade_acc2.sh — 第二账号 5m 交易策略实例
#
#  与主账号完全隔离运行：独立的 Polymarket 账号、PG 数据库、日志文件。
#  进程管理使用 PID 文件，不会影响主账号实例。
#
#  必需环境变量（在 .env 或运行前 export）：
#    ACC2_KEY              — 第二账号 Polymarket API Key
#    ACC2_WALLET           — 第二账号钱包地址
#    ACC2_PG_DSN           — 第二账号 PostgreSQL 连接字符串
#
#  可选：
#    ACC2_BUILDER_API_KEY, ACC2_BUILDER_SECRET,
#    ACC2_BUILDER_PASSPHRASE, ACC2_BUILDER_ADDRESS
#      — 如果第二账号使用 Builder Relayer（gasless），需设置这些
#
#  参数覆盖文件: config/5m_trade_acc2_params.env
#  日志文件:     logs/5m_trade_acc2.log, logs/5m_trade_acc2_diag.log
#  PID 文件:     logs/5m_trade_acc2.pid
# ============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# --- 第二账号凭证 -----------------------------------------------------------
# 从 .env 加载（如果尚未设置）
# 从 .env 提取 ACC2_ 变量（兼容 python-dotenv 格式，不直接 source）
if [ -f "$PROJECT_ROOT/.env" ]; then
  _extract_env() {
    local key="$1"
    grep -E "^${key}=" "$PROJECT_ROOT/.env" | head -1 | sed "s/^${key}=//" | sed 's/^"//;s/"$//'
  }
  [ -z "${ACC2_KEY:-}" ]     && ACC2_KEY="$(_extract_env ACC2_KEY)"
  [ -z "${ACC2_WALLET:-}" ]  && ACC2_WALLET="$(_extract_env ACC2_WALLET)"
  [ -z "${ACC2_PG_DSN:-}" ]  && ACC2_PG_DSN="$(_extract_env ACC2_PG_DSN)"
  [ -z "${ACC2_BUILDER_API_KEY:-}" ]    && ACC2_BUILDER_API_KEY="$(_extract_env ACC2_BUILDER_API_KEY)"
  [ -z "${ACC2_BUILDER_SECRET:-}" ]     && ACC2_BUILDER_SECRET="$(_extract_env ACC2_BUILDER_SECRET)"
  [ -z "${ACC2_BUILDER_PASSPHRASE:-}" ] && ACC2_BUILDER_PASSPHRASE="$(_extract_env ACC2_BUILDER_PASSPHRASE)"
  [ -z "${ACC2_BUILDER_ADDRESS:-}" ]    && ACC2_BUILDER_ADDRESS="$(_extract_env ACC2_BUILDER_ADDRESS)"
fi

if [ -z "${ACC2_KEY:-}" ]; then
  echo "❌ 必须设置 ACC2_KEY 环境变量（第二账号 Polymarket API Key）"
  exit 1
fi
if [ -z "${ACC2_WALLET:-}" ]; then
  echo "❌ 必须设置 ACC2_WALLET 环境变量（第二账号钱包地址）"
  exit 1
fi
if [ -z "${ACC2_PG_DSN:-}" ]; then
  echo "❌ 必须设置 ACC2_PG_DSN 环境变量（第二账号 PostgreSQL DSN）"
  exit 1
fi

# 将第二账号凭证映射为代码期望的标准环境变量
export FIVE_M_ACCOUNT_KEY="$ACC2_KEY"
export FIVE_M_ACCOUNT_WALLET_ADDRESS="$ACC2_WALLET"
export PG_DSN="$ACC2_PG_DSN"
export POLYMARKET_PROFILE="trade"

# Builder Relayer 凭证（可选）
if [ -n "${ACC2_BUILDER_API_KEY:-}" ]; then
  export BUILDER_API_KEY="$ACC2_BUILDER_API_KEY"
  export BUILDER_SECRET="${ACC2_BUILDER_SECRET:-}"
  export BUILDER_PASSPHRASE="${ACC2_BUILDER_PASSPHRASE:-}"
  export BUILDER_ADDRESS="${ACC2_BUILDER_ADDRESS:-}"
fi

# --- 日志与 PID 文件 --------------------------------------------------------
LOG_PREFIX="5m_trade_acc2"
LOG_FILE="logs/${LOG_PREFIX}.stdout.log"
PID_FILE="logs/${LOG_PREFIX}.pid"

# --- 加载参数覆盖文件 --------------------------------------------------------
_OVERRIDE_FILE="$PROJECT_ROOT/config/5m_trade_acc2_params.env"
if [ -f "$_OVERRIDE_FILE" ]; then
  echo "[config] 加载参数覆盖文件: $_OVERRIDE_FILE"
  source "$_OVERRIDE_FILE"
fi

# ============================================================================
#  策略参数（默认值 = 当前主实盘优化参数，可通过覆盖文件调整）
# ============================================================================

# 基础运行模式
MODE="${MODE:-${1:---live}}"
STAKE_USD="${STAKE_USD:-5.0}"
EXIT_MODE="${EXIT_MODE:-hold}"

# 入场控制
ENTRY_MINUTE="${ENTRY_MINUTE:-3}"
ENTRY_PRECLOSE_SEC="${ENTRY_PRECLOSE_SEC:-5}"
MIN_DIRECTION_DIFF="${MIN_DIRECTION_DIFF:-10}"
MAX_ENTRY_PRICE="${MAX_ENTRY_PRICE:-0.80}"
TOXIC_UTC_HOURS="${TOXIC_UTC_HOURS-}"
MAX_BTC_CROSS_COUNT="${MAX_BTC_CROSS_COUNT:-5}"
MIN_ENTRY_UPDOWN_DIFF="${MIN_ENTRY_UPDOWN_DIFF:-0.30}"
MAX_AVG_BTC_DELTA="${MAX_AVG_BTC_DELTA:-3.0}"
MINUTE_CONSISTENCY="${MINUTE_CONSISTENCY-}"

# tpsl 模式相关
MIN_HOLD_BEFORE_CLOSE_SEC="${MIN_HOLD_BEFORE_CLOSE_SEC:-5}"
TP_PRICE_CAP="${TP_PRICE_CAP:-0.95}"
TP_VALUE_CAP="${TP_VALUE_CAP:-0.15}"
SL_TO_TP_RATIO="${SL_TO_TP_RATIO:-1.3333333333333333}"

# 风险仓位管理
ENABLE_RISK_SIZING="${ENABLE_RISK_SIZING:-true}"
RISK_MIN_STAKE_RATIO="${RISK_MIN_STAKE_RATIO:-0.30}"
RISK_MAX_STAKE_RATIO="${RISK_MAX_STAKE_RATIO:-1.5}"
CONFIDENCE_BOOST="${CONFIDENCE_BOOST:-true}"
CONFIDENCE_BOOST_GE_095="${CONFIDENCE_BOOST_GE_095:-1.3}"
STAKE_CAP_VERY_HIGH="${STAKE_CAP_VERY_HIGH:-0.20}"
STAKE_CAP_HIGH="${STAKE_CAP_HIGH:-0.50}"
STAKE_CAP_MEDIUM_HIGH="${STAKE_CAP_MEDIUM_HIGH:-0.70}"
MEDIUM_HIGH_THRESHOLD="${MEDIUM_HIGH_THRESHOLD:-0.40}"
RISK_W_PRICE="${RISK_W_PRICE:-0.15}"
RISK_W_DIRECTION="${RISK_W_DIRECTION:-0.35}"
RISK_W_STABILITY="${RISK_W_STABILITY:-0.50}"
RISK_DIFF_BOOST_THRESHOLD="${RISK_DIFF_BOOST_THRESHOLD:-0.44}"
RISK_DIFF_BOOST_MULTIPLIER="${RISK_DIFF_BOOST_MULTIPLIER:-1.40}"
CROSS_BORDERLINE_DIFF_MULTIPLIER="${CROSS_BORDERLINE_DIFF_MULTIPLIER:-0.0}"

# 最后一分钟接近度风控
ENABLE_LAST_MIN_PROXIMITY_CLOSE="${ENABLE_LAST_MIN_PROXIMITY_CLOSE:-true}"
LAST_MIN_PROXIMITY_THRESHOLD="${LAST_MIN_PROXIMITY_THRESHOLD:-5.0}"

# 最后一分钟 Token bid 急跌止损
ENABLE_LAST_MIN_BID_DROP_CLOSE="${ENABLE_LAST_MIN_BID_DROP_CLOSE:-true}"
LAST_MIN_BID_DROP_THRESHOLD="${LAST_MIN_BID_DROP_THRESHOLD:-0.30}"
LAST_MIN_BID_DROP_LOOKBACK_SEC="${LAST_MIN_BID_DROP_LOOKBACK_SEC:-1.0}"
LAST_MIN_BID_DROP_START_SEC="${LAST_MIN_BID_DROP_START_SEC:-240.0}"
LAST_MIN_BID_DROP_FLOOR="${LAST_MIN_BID_DROP_FLOOR:-0.10}"

# Binance 前哨止损
ENABLE_BINANCE_EARLY_SL="${ENABLE_BINANCE_EARLY_SL:-false}"
BINANCE_SL_START_SEC="${BINANCE_SL_START_SEC:-240.0}"
BINANCE_SL_PROXIMITY="${BINANCE_SL_PROXIMITY:-3.0}"
ENABLE_BINANCE_TRADE_IMBALANCE_SL="${ENABLE_BINANCE_TRADE_IMBALANCE_SL:-true}"
BINANCE_SL_IMBALANCE_RATIO="${BINANCE_SL_IMBALANCE_RATIO:-0.80}"
BINANCE_SL_IMBALANCE_START_SEC="${BINANCE_SL_IMBALANCE_START_SEC:-270.0}"
BINANCE_SL_IMBALANCE_WINDOW_SEC="${BINANCE_SL_IMBALANCE_WINDOW_SEC:-3.0}"
BINANCE_SL_IMBALANCE_MIN_PROXIMITY="${BINANCE_SL_IMBALANCE_MIN_PROXIMITY:-10.0}"

# 系统控制
REPORT_INTERVAL_SEC="${REPORT_INTERVAL_SEC:-3600}"
ENABLE_DB_TICK_VALIDATION="${ENABLE_DB_TICK_VALIDATION:-false}"

# 偏离入场模式
ENABLE_DEVIATION_ENTRY="${ENABLE_DEVIATION_ENTRY:-true}"
DEVIATION_ENTRY_THRESHOLD="${DEVIATION_ENTRY_THRESHOLD:-40.0}"
DEVIATION_ENTRY_START_SEC="${DEVIATION_ENTRY_START_SEC:-60.0}"
DEVIATION_ENTRY_END_SEC="${DEVIATION_ENTRY_END_SEC:-240.0}"
ENABLE_EARLY_PROBE="${ENABLE_EARLY_PROBE:-true}"
EARLY_PROBE_START_SEC="${EARLY_PROBE_START_SEC:-0.0}"
EARLY_PROBE_END_SEC="${EARLY_PROBE_END_SEC:-60.0}"
EARLY_PROBE_MIN_ABS_DIFF="${EARLY_PROBE_MIN_ABS_DIFF:-10.0}"
EARLY_PROBE_STAKE_RATIO="${EARLY_PROBE_STAKE_RATIO:-0.20}"
EARLY_PROBE_MAX_ENTRY_PRICE="${EARLY_PROBE_MAX_ENTRY_PRICE:-0.60}"

# DCA 加仓
ENABLE_DCA="${ENABLE_DCA:-true}"
DCA_MAX_ADDS="${DCA_MAX_ADDS:-4}"
DCA_INTERVAL_SEC="${DCA_INTERVAL_SEC:-15.0}"
DCA_DEVIATION_STEP="${DCA_DEVIATION_STEP:-20.0}"
DCA_END_SEC="${DCA_END_SEC:-270.0}"
DCA_MIN_CONFIDENCE="${DCA_MIN_CONFIDENCE:-0.3}"
DCA_MAX_ENTRY_PRICE="${DCA_MAX_ENTRY_PRICE:-0.95}"
DCA_W_DEVIATION="${DCA_W_DEVIATION:-0.25}"
DCA_W_ATR="${DCA_W_ATR:-0.20}"
DCA_W_CROSS="${DCA_W_CROSS:-0.20}"
DCA_W_PRICE="${DCA_W_PRICE:-0.15}"
DCA_W_TIME="${DCA_W_TIME:-0.10}"
DCA_W_POSITION="${DCA_W_POSITION:-0.10}"
DCA_ALLOW_PULLBACK_ADD="${DCA_ALLOW_PULLBACK_ADD:-true}"
DCA_PULLBACK_RATIO_MIN="${DCA_PULLBACK_RATIO_MIN:-0.03}"
DCA_MAX_AVG_PRICE="${DCA_MAX_AVG_PRICE:-0.0}"

# 方向修正
ENABLE_DIRECTION_REVERSAL="${ENABLE_DIRECTION_REVERSAL:-true}"
REVERSAL_THRESHOLD="${REVERSAL_THRESHOLD:-50.0}"
REVERSAL_START_SEC="${REVERSAL_START_SEC:-120.0}"
REVERSAL_END_SEC="${REVERSAL_END_SEC:-240.0}"
REVERSAL_SIZE_MULTIPLIER="${REVERSAL_SIZE_MULTIPLIER:-1.2}"
REVERSAL_MIN_POSITION_USDC="${REVERSAL_MIN_POSITION_USDC:-0.0}"

# 连败缩仓
ENABLE_STREAK_SIZING="${ENABLE_STREAK_SIZING:-true}"
STREAK_LOSS_THRESHOLD="${STREAK_LOSS_THRESHOLD:-3}"
STREAK_SHRINK_FACTOR="${STREAK_SHRINK_FACTOR:-0.5}"
STREAK_MAX_SHRINKS="${STREAK_MAX_SHRINKS:-3}"

# ============================================================================
#  参数校验（精简版：仅校验核心参数和模式）
# ============================================================================

if [ "$MODE" = "-h" ] || [ "$MODE" = "--help" ]; then
  echo "用法: ACC2_KEY=xxx ACC2_WALLET=xxx ACC2_PG_DSN=xxx ./scripts/restart_5m_trade_acc2.sh [--dry-run|--live]"
  echo "参数通过 config/5m_trade_acc2_params.env 覆盖，格式同主账号。"
  exit 0
fi

mkdir -p logs

if [ "$MODE" != "--dry-run" ] && [ "$MODE" != "--live" ]; then
  echo "❌ 模式参数错误：仅支持 --dry-run 或 --live"
  exit 1
fi

if ! [[ "$ENTRY_MINUTE" =~ ^[1-4]$ ]]; then
  echo "❌ entry_minute 必须是 1-4"; exit 1
fi
if ! [[ "$STAKE_USD" =~ ^[0-9]+([.][0-9]+)?$ ]] || ! awk "BEGIN{exit !($STAKE_USD > 0)}"; then
  echo "❌ stake_usd 必须是大于 0 的数字"; exit 1
fi
if [ "$EXIT_MODE" != "tpsl" ] && [ "$EXIT_MODE" != "hold" ]; then
  echo "❌ exit_mode 必须是 tpsl 或 hold"; exit 1
fi

# ============================================================================
#  输出启动信息
# ============================================================================

echo "=========================================="
echo "重启 5m_trade 服务 [第二账号]"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
echo "模式参数: $MODE"
echo "账号: ACC2 (wallet=${ACC2_WALLET:0:10}...)"
echo "数据库: ACC2_PG_DSN"
echo "日志前缀: $LOG_PREFIX"
echo "单笔仓位金额(USDC): $STAKE_USD"
echo "建仓分钟: $ENTRY_MINUTE"
echo "收盘前抢跑秒数: $ENTRY_PRECLOSE_SEC"
echo "最小方向差值: $MIN_DIRECTION_DIFF"
echo "允许最高开仓价: $MAX_ENTRY_PRICE"
echo "UP/DOWN最小价差: $MIN_ENTRY_UPDOWN_DIFF"
echo "BTC越过开盘价最大次数: $MAX_BTC_CROSS_COUNT"
echo "ATR波动率上限: $MAX_AVG_BTC_DELTA"
echo "平仓模式: $EXIT_MODE"
echo "风险仓位管理: $ENABLE_RISK_SIZING (min=$RISK_MIN_STAKE_RATIO max=$RISK_MAX_STAKE_RATIO)"
echo "最后一分钟接近度风控: enable=$ENABLE_LAST_MIN_PROXIMITY_CLOSE threshold=$LAST_MIN_PROXIMITY_THRESHOLD"
echo "Token bid急跌止损: enable=$ENABLE_LAST_MIN_BID_DROP_CLOSE threshold=$LAST_MIN_BID_DROP_THRESHOLD"
echo "Binance前哨止损: enable=$ENABLE_BINANCE_EARLY_SL proximity=\$${BINANCE_SL_PROXIMITY}"
echo "Binance成交流止损: enable=$ENABLE_BINANCE_TRADE_IMBALANCE_SL ratio=$BINANCE_SL_IMBALANCE_RATIO"
echo "偏离入场: enable=$ENABLE_DEVIATION_ENTRY threshold=\$${DEVIATION_ENTRY_THRESHOLD}"
echo "DCA加仓: enable=$ENABLE_DCA"
echo "方向修正: enable=$ENABLE_DIRECTION_REVERSAL"
echo "连败缩仓: enable=$ENABLE_STREAK_SIZING threshold=$STREAK_LOSS_THRESHOLD"
echo "=========================================="

# ============================================================================
#  构建命令
# ============================================================================

_build_cmd() {
  CMD=(
    uv run 5m_trade.py
    --log-prefix "$LOG_PREFIX"
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
    --early-probe-start-sec "$EARLY_PROBE_START_SEC"
    --early-probe-end-sec "$EARLY_PROBE_END_SEC"
    --early-probe-min-abs-diff "$EARLY_PROBE_MIN_ABS_DIFF"
    --early-probe-stake-ratio "$EARLY_PROBE_STAKE_RATIO"
    --early-probe-max-entry-price "$EARLY_PROBE_MAX_ENTRY_PRICE"
    --reversal-threshold "$REVERSAL_THRESHOLD"
    --reversal-start-sec "$REVERSAL_START_SEC"
    --reversal-end-sec "$REVERSAL_END_SEC"
    --reversal-size-multiplier "$REVERSAL_SIZE_MULTIPLIER"
    --reversal-min-position-usdc "$REVERSAL_MIN_POSITION_USDC"
    --dca-max-adds "$DCA_MAX_ADDS"
    --dca-interval-sec "$DCA_INTERVAL_SEC"
    --dca-deviation-step "$DCA_DEVIATION_STEP"
    --dca-end-sec "$DCA_END_SEC"
    --dca-min-confidence "$DCA_MIN_CONFIDENCE"
    --dca-max-entry-price "$DCA_MAX_ENTRY_PRICE"
    --dca-pullback-ratio-min "$DCA_PULLBACK_RATIO_MIN"
    --dca-max-avg-price "$DCA_MAX_AVG_PRICE"
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

  # 布尔开关
  if [ "$ENABLE_RISK_SIZING" = "false" ]; then CMD+=(--disable-risk-sizing); fi
  if [ "$CONFIDENCE_BOOST" = "false" ]; then CMD+=(--disable-confidence-boost); fi
  if [ "$ENABLE_LAST_MIN_PROXIMITY_CLOSE" = "false" ]; then CMD+=(--disable-last-min-proximity-close); fi
  if [ "$ENABLE_LAST_MIN_BID_DROP_CLOSE" = "false" ]; then CMD+=(--disable-last-min-bid-drop-close); fi
  if [ "$ENABLE_BINANCE_EARLY_SL" = "false" ]; then CMD+=(--disable-binance-early-sl); fi
  if [ "$ENABLE_BINANCE_TRADE_IMBALANCE_SL" = "false" ]; then CMD+=(--disable-binance-trade-imbalance-sl); fi
  if [ "$ENABLE_DB_TICK_VALIDATION" = "false" ]; then CMD+=(--disable-db-tick-validation); fi
  if [ "$ENABLE_DEVIATION_ENTRY" = "true" ]; then CMD+=(--enable-deviation-entry); fi
  if [ "$ENABLE_EARLY_PROBE" = "true" ]; then CMD+=(--enable-early-probe); fi
  if [ "$ENABLE_DIRECTION_REVERSAL" = "true" ]; then CMD+=(--enable-direction-reversal); fi
  if [ "$ENABLE_DCA" = "true" ]; then CMD+=(--enable-dca); fi
  if [ "$DCA_ALLOW_PULLBACK_ADD" = "true" ]; then CMD+=(--enable-dca-pullback-add); fi
  if [ "$ENABLE_STREAK_SIZING" = "true" ]; then CMD+=(--enable-streak-sizing); fi

  if [ "$MODE" != "--live" ]; then CMD+=(--dry-run); fi
}

# ============================================================================
#  进程管理（PID 文件方式，不影响其他 5m_trade 实例）
# ============================================================================

# --foreground 模式：前台运行，供 systemd 调用
if [ "${FOREGROUND:-}" = "1" ]; then
  echo "[foreground] 前台启动 5m_trade [ACC2] ..."
  _build_cmd
  exec "${CMD[@]}"
fi

# 停止已有的 ACC2 进程（仅通过 PID 文件，不使用 pkill）
echo "[1/3] 停止已有 ACC2 进程..."
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
  if [ -n "$OLD_PID" ] && ps -p "$OLD_PID" > /dev/null 2>&1; then
    echo "  终止旧进程 PID=$OLD_PID"
    kill "$OLD_PID" 2>/dev/null || true
    # 等待最多 5 秒
    for i in $(seq 1 5); do
      if ! ps -p "$OLD_PID" > /dev/null 2>&1; then break; fi
      sleep 1
    done
    # 仍在运行则强制杀
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
      echo "  旧进程未退出，强制终止..."
      kill -9 "$OLD_PID" 2>/dev/null || true
      sleep 1
    fi
  fi
  rm -f "$PID_FILE"
else
  echo "  无 PID 文件，跳过"
fi

echo "[2/3] 启动 ACC2 新进程..."
_build_cmd
nohup "${CMD[@]}" >> "$LOG_FILE" 2>&1 &

NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 2

echo "[3/3] 校验进程状态..."
if ps -p "$NEW_PID" > /dev/null 2>&1; then
  echo "✅ 5m_trade [ACC2] 启动成功"
  echo "PID: $NEW_PID"
  echo "PID 文件: $PID_FILE"
  echo "业务日志(轮转): logs/${LOG_PREFIX}.log"
  echo "诊断日志(轮转): logs/${LOG_PREFIX}_diag.log"
  echo "进程输出日志: $LOG_FILE"
  echo "查看业务日志: tail -f logs/${LOG_PREFIX}.log"
else
  echo "❌ 5m_trade [ACC2] 启动失败，请检查日志: $LOG_FILE"
  exit 1
fi
