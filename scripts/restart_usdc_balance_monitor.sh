SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"


echo "=========================================="
echo "重启 usdc_balance_monitor.py 服务"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
echo "=========================================="


# --foreground 模式：前台运行，供 systemd 调用（通过环境变量 FOREGROUND=1 激活）
if [ "${FOREGROUND:-}" = "1" ]; then
  echo "[foreground] 前台启动 usdc_balance_monitor ..."
  exec uv run scripts/usdc_balance_monitor.py
fi

echo "[1/3] 停止已有  usdc_balance_monitor.py 进程..."
pkill -f "usdc_balance_monitor.py" || true
sleep 1

echo "[2/3] 启动 usdc_balance_monitor.py..."
nohup uv run scripts/usdc_balance_monitor.py > /dev/null 2>&1 &
sleep 2

echo "[3/3] 启动完成。"
echo "✅ usdc_balance_monitor 启动成功"