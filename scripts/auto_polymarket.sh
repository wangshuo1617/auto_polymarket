#!/bin/bash

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 设置错误处理：如果任何命令失败，脚本将退出
set -e

echo "=========================================="
echo "开始运行 Polymarket 自动化脚本"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 第一步：运行持仓分析脚本
echo ""
echo "步骤 1/2: 运行持仓分析脚本 (position_analyze.py)..."
uv run /root/auto_polymarket/position_analyze.py

if [ $? -eq 0 ]; then
    echo "✓ 持仓分析脚本执行成功"
else
    echo "✗ 持仓分析脚本执行失败，退出码: $?"
    exit 1
fi

# 第二步：运行比特币价格监控脚本
echo ""
echo "步骤 2/2: 运行比特币价格监控脚本 (btc_price_watcher.py)..."

# 检查并终止现有的 btc_price_watcher.py 进程
EXISTING_PIDS=$(pgrep -f "btc_price_watcher.py" || true)
if [ -n "$EXISTING_PIDS" ]; then
    echo "发现正在运行的 btc_price_watcher.py 进程: $EXISTING_PIDS"
    echo "正在终止现有进程..."
    pkill -f "btc_price_watcher.py" || true
    sleep 2
    # 再次检查，如果还有进程则强制终止
    REMAINING_PIDS=$(pgrep -f "btc_price_watcher.py" || true)
    if [ -n "$REMAINING_PIDS" ]; then
        echo "强制终止剩余进程..."
        pkill -9 -f "btc_price_watcher.py" || true
        sleep 1
    fi
    echo "✓ 已终止现有进程"
else
    echo "未发现正在运行的 btc_price_watcher.py 进程"
fi

# 使用 nohup 在后台运行
echo "正在启动新的 btc_price_watcher.py 进程（后台运行）..."
LOG_FILE="/root/auto_polymarket/logs/btc_watcher.log"
nohup uv run /root/auto_polymarket/btc_price_watcher.py > "$LOG_FILE" 2>&1 &
WATCHER_PID=$!

# 等待一下确保进程启动成功
sleep 2

# 检查进程是否还在运行
if ps -p $WATCHER_PID > /dev/null 2>&1; then
    echo "✓ btc_price_watcher.py 已成功启动（PID: $WATCHER_PID）"
    echo "  日志文件: $LOG_FILE"
    echo "  查看日志: tail -f $LOG_FILE"
    echo "  停止进程: kill $WATCHER_PID"
else
    echo "✗ btc_price_watcher.py 启动失败，请检查日志: $LOG_FILE"
    exit 1
fi

echo ""
echo "=========================================="
echo "脚本执行完成"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
