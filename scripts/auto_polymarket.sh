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

# 运行持仓分析脚本
echo ""
echo "运行持仓分析脚本 (position_analyze.py)..."
uv run /root/auto_polymarket/position_analyze.py

if [ $? -eq 0 ]; then
    echo "✓ 持仓分析脚本执行成功"
else
    echo "✗ 持仓分析脚本执行失败，退出码: $?"
    exit 1
fi

echo ""
echo "=========================================="
echo "脚本执行完成"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
