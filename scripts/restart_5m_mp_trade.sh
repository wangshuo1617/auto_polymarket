#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# ── 参数（均可通过环境变量或位置参数覆盖） ──
STAKE_USD="${1:-2.0}"
TREND_TH="${2:-0.02}"
MP_MAX="${3:-0.25}"
ROLLING_DAYS="${4:-5}"
DRY_RUN="${5:-}"

LOG_FILE="logs/5m_mp_trade.stdout.log"
PID_FILE="logs/5m_mp_trade.pid"
BIZ_LOG="logs/5m_trade.log"

mkdir -p logs

echo "=========================================="
echo "重启 Mispricing 5m_trade 策略"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
echo "──────────────────────────────────────────"
echo "基础下注金额: ${STAKE_USD} USDC"
echo "trend 阈值: ${TREND_TH}"
echo "mispricing 上限: ${MP_MAX}"
echo "滚动窗口天数: ${ROLLING_DAYS}"
echo "模式: $([ -n "$DRY_RUN" ] && echo '模拟(dry-run)' || echo '实盘(live)')"
echo "=========================================="

# ── 停止旧进程 ──
echo "[1/3] 停止已有 mispricing 交易进程..."
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if ps -p "$OLD_PID" > /dev/null 2>&1; then
    kill "$OLD_PID" 2>/dev/null || true
    sleep 1
    if ps -p "$OLD_PID" > /dev/null 2>&1; then
      kill -9 "$OLD_PID" 2>/dev/null || true
      sleep 1
    fi
    echo "  已停止旧进程 PID=$OLD_PID"
  else
    echo "  旧进程 PID=$OLD_PID 已不存在"
  fi
fi

# 兜底：按进程名匹配
pkill -f "5m_trade_mispricing.py" 2>/dev/null || true
sleep 1

# ── 构建启动命令 ──
echo "[2/3] 启动新进程..."
CMD=(
  uv run new_trade/5m_trade_mispricing.py
  --stake-usd "$STAKE_USD"
  --trend-th "$TREND_TH"
  --mp-max "$MP_MAX"
  --rolling-window-days "$ROLLING_DAYS"
)

if [ -n "$DRY_RUN" ]; then
  CMD+=(--dry-run)
fi

nohup "${CMD[@]}" >> "$LOG_FILE" 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"
sleep 2

# ── 校验 ──
echo "[3/3] 校验进程状态..."
if ps -p "$NEW_PID" > /dev/null 2>&1; then
  echo "✅ Mispricing 5m_trade 启动成功"
  echo "PID: $NEW_PID"
  echo "PID 文件: $PID_FILE"
  echo "业务日志: $BIZ_LOG"
  echo "进程输出: $LOG_FILE"
  echo "查看日志: tail -f $BIZ_LOG"
  echo ""
  echo "停止命令: kill $NEW_PID"
else
  echo "❌ 启动失败，请检查日志: $LOG_FILE"
  tail -20 "$LOG_FILE" 2>/dev/null || true
  exit 1
fi
