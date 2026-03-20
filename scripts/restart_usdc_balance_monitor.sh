echo "[1/3] 停止已有  usdc_balance_monitor.py 进程..."
pkill -f "usdc_balance_monitor.py" || true
sleep 1

echo "[2/3] 启动 usdc_balance_monitor.py..."
nohup uv run /root/auto_polymarket/scripts/usdc_balance_monitor.py > /dev/null 2>&1 &
sleep 2

echo "[3/3] 启动完成。"