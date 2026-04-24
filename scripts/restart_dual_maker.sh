#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 强制使用 trade 账号 profile
export POLYMARKET_PROFILE="${POLYMARKET_PROFILE:-trade}"

# 参数配置（可通过环境变量覆盖）
MODE="${MODE:-${1:---dry-run}}"                       # --dry-run / --live
BID_PRICE="${BID_PRICE:-0.38}"                        # 两侧挂单价格
SHARES="${SHARES:-15}"                                # 每侧股数
CANCEL_AT_SEC="${CANCEL_AT_SEC:-270}"                 # 结算时间点（秒）
QUEUE_HAIRCUT="${QUEUE_HAIRCUT:-10}"                   # 干运行成交判定 tick 数
LOG_PREFIX="${LOG_PREFIX:-dual_maker}"                 # 日志文件前缀

# 参数校验
if [[ "$MODE" != "--dry-run" && "$MODE" != "--live" ]]; then
    echo "❌ MODE 必须是 --dry-run 或 --live，当前值: $MODE"
    exit 1
fi

if ! echo "$BID_PRICE" | grep -qE '^[0-9]+\.?[0-9]*$'; then
    echo "❌ BID_PRICE 必须是数字: $BID_PRICE"
    exit 1
fi

echo "─── 双边低价挂单策略配置 ───"
echo "  MODE:           $MODE"
echo "  BID_PRICE:      $BID_PRICE"
echo "  SHARES:         $SHARES"
echo "  CANCEL_AT_SEC:  $CANCEL_AT_SEC"
echo "  QUEUE_HAIRCUT:  $QUEUE_HAIRCUT"
echo "  LOG_PREFIX:     $LOG_PREFIX"
echo "  PROFILE:        $POLYMARKET_PROFILE"
echo "────────────────────────────"

# 停止旧进程
_PID_FILE="$PROJECT_ROOT/logs/${LOG_PREFIX}.pid"
if [ -f "$_PID_FILE" ]; then
    _OLD_PID=$(cat "$_PID_FILE")
    if kill -0 "$_OLD_PID" 2>/dev/null; then
        echo "停止旧进程: PID=$_OLD_PID"
        kill "$_OLD_PID" 2>/dev/null || true
        sleep 2
        kill -0 "$_OLD_PID" 2>/dev/null && kill -9 "$_OLD_PID" 2>/dev/null || true
    fi
    rm -f "$_PID_FILE"
fi

# 构建命令
CMD=(
    uv run dual_maker_trade.py
    "$MODE"
    --bid-price "$BID_PRICE"
    --shares "$SHARES"
    --cancel-at-sec "$CANCEL_AT_SEC"
    --queue-haircut "$QUEUE_HAIRCUT"
    --log-prefix "$LOG_PREFIX"
)

echo "启动命令: ${CMD[*]}"

# 后台启动
nohup "${CMD[@]}" >> "logs/${LOG_PREFIX}_nohup.log" 2>&1 &
_NEW_PID=$!
echo "$_NEW_PID" > "$_PID_FILE"

echo "✅ 双边低价挂单策略已启动: PID=$_NEW_PID, 日志=logs/${LOG_PREFIX}.log"
