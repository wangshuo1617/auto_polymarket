#!/bin/bash
# 安装/更新 systemd 服务文件并启用开机自启
# 用法: bash scripts/install_systemd.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="$SCRIPT_DIR/systemd"
TARGET_DIR="/etc/systemd/system"

SERVICES=(
  auto-poly-btc-monitor
  auto-poly-5m-trade
  auto-poly-app
  auto-poly-usdc-monitor
)

echo "=========================================="
echo "安装 systemd 服务"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 先停止通过 nohup 启动的旧进程
echo "[1/4] 停止旧的 nohup 进程..."
pkill -f "5m_trade.py" 2>/dev/null || true
pkill -f "app.py" 2>/dev/null || true
pkill -f "btc_1s_market_monitor.py" 2>/dev/null || true
pkill -f "usdc_balance_monitor.py" 2>/dev/null || true
sleep 2

echo "[2/4] 复制 unit 文件到 $TARGET_DIR ..."
for svc in "${SERVICES[@]}"; do
  cp "$SYSTEMD_DIR/$svc.service" "$TARGET_DIR/$svc.service"
  echo "  ✓ $svc.service"
done

echo "[3/4] 重新加载 systemd 配置..."
systemctl daemon-reload

echo "[4/4] 启用并启动服务..."
for svc in "${SERVICES[@]}"; do
  systemctl enable "$svc"
  systemctl restart "$svc"
  sleep 3
  if systemctl is-active --quiet "$svc"; then
    echo "  ✅ $svc 已启动"
  else
    echo "  ❌ $svc 启动失败"
    systemctl status "$svc" --no-pager -l || true
  fi
done

echo ""
echo "=========================================="
echo "安装完成！常用命令："
echo "  systemctl status auto-poly-*          # 查看所有服务状态"
echo "  systemctl restart auto-poly-5m-trade  # 重启某个服务"
echo "  journalctl -u auto-poly-5m-trade -f   # 查看日志"
echo "  systemctl stop auto-poly-5m-trade     # 停止某个服务"
echo "=========================================="
