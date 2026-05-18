#!/usr/bin/env bash
# systemd 前台启动入口（auto-poly-usdc-monitor.service ExecStart 调用）
set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo "=========================================="
echo "启动 usdc_balance_monitor.py"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
echo "=========================================="

exec uv run scripts/usdc_balance_monitor.py
