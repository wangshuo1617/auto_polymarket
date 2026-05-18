SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=========================================="
echo "重启所有服务"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $(pwd)"
echo "=========================================="
echo "正在重启 app.py..."
bash "$SCRIPT_DIR/restart_app.sh"
echo "正在重启 usdc_balance_monitor.py..."
bash "$SCRIPT_DIR/restart_usdc_balance_monitor.sh"

