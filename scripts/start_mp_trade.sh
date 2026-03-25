#!/bin/bash
set -euo pipefail

# 可选环境变量：
#   MP_RUN_TICK_HEALTH=1      — 启用启动前 tick 库健康检查（默认关闭）
#   MP_HEALTH_SLEEP=15        — 首次检查前等待秒数（给 monitor 建库/首写，默认 15）
#   MP_HEALTH_POLL_MAX=180    — 若未通过，最长再轮询多少秒（默认 180，约可攒满 80+ 行）
#   MP_HEALTH_POLL_INTERVAL=10— 轮询间隔秒数（默认 10）
#   MP_HEALTH_MAX_LAG=300     — 传给 check_tick_db_health.py --max-lag-sec
#   MP_HEALTH_MIN_ROWS=80     — 近 10 分钟最少行数（默认 80；更严可调高）
#   MP_HEALTH_SKIP_RECENT=1   — 不传行数阈值，仅 integrity + 延迟
#   MP_HEALTH_DB_PATH=...     — 指定 SQLite 路径（否则用项目 config / TICK_DB_HEALTH_PATH）

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

# ── 交易参数（可通过环境变量覆盖） ──
MP_STAKE="${MP_STAKE:-2.0}"
MP_TREND_TH="${MP_TREND_TH:-0.02}"
MP_MAX="${MP_MAX:-0.25}"
MP_ROLLING_DAYS="${MP_ROLLING_DAYS:-5}"
MP_ROLLING_WINDOWS="${MP_ROLLING_WINDOWS:-500}"
MP_UPDATE_INTERVAL="${MP_UPDATE_INTERVAL:-300}"
MP_ENABLE_REGIME_STATE_MACHINE="${MP_ENABLE_REGIME_STATE_MACHINE:-0}"
MP_REGIME_VOL_ENTER_Q="${MP_REGIME_VOL_ENTER_Q:-0.65}"
MP_REGIME_VOL_EXIT_Q="${MP_REGIME_VOL_EXIT_Q:-0.55}"
MP_REGIME_VOL_MIN_SAMPLES="${MP_REGIME_VOL_MIN_SAMPLES:-100}"
MP_REGIME_HIGH_MULT="${MP_REGIME_HIGH_MULT:-0.60}"
MP_REGIME_LOW_MULT="${MP_REGIME_LOW_MULT:-1.00}"
MP_ENABLE_REGIME_WHITELIST_FILTER="${MP_ENABLE_REGIME_WHITELIST_FILTER:-1}"

echo ""
echo "=========================================="
echo "  启动 Mispricing 5m 交易全套服务"
echo "  时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "  工作目录: $PROJECT_ROOT"
echo "──────────────────────────────────────────"
echo "  基础下注: ${MP_STAKE} USDC"
echo "  trend 阈值: ${MP_TREND_TH}"
echo "  mp 上限: ${MP_MAX}"
echo "  滚动窗口: 最近 ${MP_ROLLING_WINDOWS} 个（兜底 ${MP_ROLLING_DAYS} 天）"
echo "  独立状态机: $([ \"$MP_ENABLE_REGIME_STATE_MACHINE\" = \"1\" ] && echo '开启' || echo '关闭(默认)')"
echo "  状态机白名单过滤: $([ \"$MP_ENABLE_REGIME_WHITELIST_FILTER\" = \"1\" ] && echo '开启' || echo '关闭(默认)')"
echo "  指标更新间隔: ${MP_UPDATE_INTERVAL}s"
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
# 1. BTC 1s Market Monitor（tick 数据采集）
# ================================================================
echo -e "${CYAN}[1/3] BTC 1s Market Monitor${NC}"
stop_by_pid_file "logs/btc_1s_market_monitor.pid" "btc_1s_market_monitor"
stop_by_pattern "btc_1s_market_monitor.py"
start_service "btc_1s_market_monitor" \
  "logs/btc_1s_market_monitor.pid" \
  "logs/btc_1s_market_monitor.log" \
  uv run btc_1s_market_monitor.py --symbol btcusdt

# ================================================================
# 2. Mispricing 指标定时更新
# ================================================================
echo -e "${CYAN}[2/3] Mispricing 指标更新 (每 ${MP_UPDATE_INTERVAL}s)${NC}"
stop_by_pid_file "logs/update_mispricing.pid" "update_mispricing_db"
stop_by_pattern "update_mispricing_db.py"
start_service "update_mispricing_db" \
  "logs/update_mispricing.pid" \
  "logs/update_mispricing.log" \
  uv run new_trade/update_mispricing_db.py --interval "$MP_UPDATE_INTERVAL"

# ================================================================
# 3. Mispricing 5m 交易策略（可选：启动前 tick 库健康检查）
# ================================================================
echo -e "${CYAN}[3/3] Mispricing 5m 交易${NC}"

if [ "${MP_RUN_TICK_HEALTH:-0}" = "1" ]; then
  echo -e "  ${CYAN}预检: 主 tick 库健康检查 (scripts/check_tick_db_health.py)${NC}"
  _hsleep="${MP_HEALTH_SLEEP:-15}"
  echo "  等待 ${_hsleep}s 以便 monitor 建库并开始写入…"
  sleep "$_hsleep"

  if command -v uv >/dev/null 2>&1; then
    HEALTH_CMD=(uv run python "$PROJECT_ROOT/scripts/check_tick_db_health.py")
  else
    echo -e "  ${YELLOW}未检测到 uv，预检改用 python（请确保已安装依赖）${NC}"
    HEALTH_CMD=(python "$PROJECT_ROOT/scripts/check_tick_db_health.py")
  fi
  if [ -n "${MP_HEALTH_DB_PATH:-}" ]; then
    HEALTH_CMD+=(--db-path "$MP_HEALTH_DB_PATH")
  fi
  HEALTH_CMD+=(--max-lag-sec "${MP_HEALTH_MAX_LAG:-300}")
  if [ "${MP_HEALTH_SKIP_RECENT:-0}" = "1" ]; then
    HEALTH_CMD+=(--skip-recent-count)
  else
    HEALTH_CMD+=(--min-rows-recent "${MP_HEALTH_MIN_ROWS:-80}")
  fi

  # 冷启动时 monitor 约 1Hz 写入，8s 内不可能达到默认 80 行 /10min；轮询直至达标或超时。
  _poll_max="${MP_HEALTH_POLL_MAX:-180}"
  _poll_iv="${MP_HEALTH_POLL_INTERVAL:-10}"
  _deadline=$(( $(date +%s) + _poll_max ))
  _health_ok=0
  while true; do
    set +e
    "${HEALTH_CMD[@]}"
    _hrc=$?
    set -e
    if [ "$_hrc" -eq 0 ]; then
      _health_ok=1
      break
    fi
    # exit 2: 库损坏/缺文件等，轮询无效（见 check_tick_db_health.py）
    if [ "$_hrc" -eq 2 ]; then
      echo -e "  ${RED}❌ Tick 库致命错误 (exit 2)，已立即中止（非延迟/行数问题）。${NC}"
      echo -e "  ${YELLOW}若为 integrity 损坏: 先 bash scripts/stop_mp_trade.sh，再 bash scripts/recover_trade_sqlite.sh${NC}"
      exit 1
    fi
    _now=$(date +%s)
    if [ "$_now" -ge "$_deadline" ]; then
      break
    fi
    echo -e "  ${YELLOW}Tick 库尚未满足阈值（冷启动需约 80s+ 攒行），${_poll_iv}s 后重试… (剩余约 $((_deadline - _now))s)${NC}"
    sleep "$_poll_iv"
  done

  if [ "$_health_ok" != "1" ]; then
    echo -e "  ${RED}❌ Tick 库检查未通过（已轮询 ${_poll_max}s），已中止启动 Mispricing 交易。${NC}"
    echo -e "  ${YELLOW}提示: 查看 monitor 日志 logs/btc_1s_market_monitor.log；手动诊断:${NC}"
    echo "    uv run python scripts/check_tick_db_health.py"
    echo -e "  ${YELLOW}可调宽: MP_HEALTH_MIN_ROWS=30 MP_HEALTH_POLL_MAX=300 … 或先不设 MP_RUN_TICK_HEALTH 跳过预检${NC}"
    exit 1
  fi
  echo -e "  ${GREEN}✅ Tick 库检查通过${NC}"
else
  echo -e "  ${YELLOW}已跳过 tick 库预检（默认关闭；需要时: MP_RUN_TICK_HEALTH=1）${NC}"
fi

stop_by_pid_file "logs/5m_mp_trade.pid" "5m_trade_mispricing"
stop_by_pattern "5m_trade_mispricing.py"
start_service "5m_trade_mispricing" \
  "logs/5m_mp_trade.pid" \
  "logs/5m_mp_trade.stdout.log" \
  uv run new_trade/5m_trade_mispricing.py \
    --stake-usd "$MP_STAKE" \
    --trend-th "$MP_TREND_TH" \
    --mp-max "$MP_MAX" \
    --rolling-window-count "$MP_ROLLING_WINDOWS" \
    --rolling-window-days "$MP_ROLLING_DAYS" \
    --regime-vol-enter-q "$MP_REGIME_VOL_ENTER_Q" \
    --regime-vol-exit-q "$MP_REGIME_VOL_EXIT_Q" \
    --regime-vol-min-samples "$MP_REGIME_VOL_MIN_SAMPLES" \
    --regime-high-vol-stake-multiplier "$MP_REGIME_HIGH_MULT" \
    --regime-low-vol-stake-multiplier "$MP_REGIME_LOW_MULT" \
    $([ "$MP_ENABLE_REGIME_WHITELIST_FILTER" = "1" ] && echo "--enable-regime-whitelist-filter") \
    $([ "$MP_ENABLE_REGIME_STATE_MACHINE" = "1" ] && echo "--enable-regime-state-machine")

# ================================================================
# 汇总
# ================================================================
echo ""
echo "=========================================="
echo -e "  启动完成: ${GREEN}${STARTED} 成功${NC}  ${RED}${FAILED} 失败${NC}"
echo "=========================================="
echo ""
echo "  查看交易日志: tail -f logs/5m_trade.log"
echo "  停止交易服务: bash scripts/stop_mp_trade.sh"
echo ""
