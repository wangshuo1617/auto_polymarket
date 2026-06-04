#!/bin/bash
# 安装/更新 systemd 服务文件并启用开机自启
# 用法: bash scripts/install_systemd.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="$SCRIPT_DIR/systemd"
TARGET_DIR="/etc/systemd/system"

SERVICES=(
  auto-poly-app
  auto-poly-usdc-monitor
  auto-poly-btc-price-watcher
  auto-poly-advisory-batch
  auto-poly-advisory-settlement
  auto-poly-etf-volume-monitor
  auto-poly-recommendation-executor
)

EXTRA_UNITS=(
  auto-poly-advisory-calibration.service
  auto-poly-advisory-edge-alerts.service
  auto-poly-advisory-fills-poller.service
  auto-poly-advisory-intent-filler.service
  auto-poly-advisory-metrics.service
)

TIMERS=(
  auto-poly-advisory-calibration.timer
  auto-poly-advisory-edge-alerts.timer
  auto-poly-advisory-fills-poller.timer
  auto-poly-advisory-intent-filler.timer
  auto-poly-advisory-metrics.timer
)

echo "=========================================="
echo "安装 systemd 服务"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 先停止通过 nohup 启动的旧进程
echo "[1/4] 停止旧的 nohup 进程..."
pkill -f "app.py" 2>/dev/null || true
pkill -f "usdc_balance_monitor.py" 2>/dev/null || true
sleep 2

echo "[2/4] 复制 unit/timer 文件到 $TARGET_DIR ..."
for svc in "${SERVICES[@]}"; do
  cp "$SYSTEMD_DIR/$svc.service" "$TARGET_DIR/$svc.service"
  echo "  ✓ $svc.service"
done
for unit in "${EXTRA_UNITS[@]}"; do
  cp "$SYSTEMD_DIR/$unit" "$TARGET_DIR/$unit"
  echo "  ✓ $unit"
done
for timer in "${TIMERS[@]}"; do
  cp "$SYSTEMD_DIR/$timer" "$TARGET_DIR/$timer"
  echo "  ✓ $timer"
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
for timer in "${TIMERS[@]}"; do
  systemctl enable --now "$timer"
  sleep 1
  if systemctl is-active --quiet "$timer"; then
    echo "  ✅ $timer 已启动"
  else
    echo "  ❌ $timer 启动失败"
    systemctl status "$timer" --no-pager -l || true
  fi
done

echo ""
echo "=========================================="
echo "安装完成！常用命令："
echo "  systemctl status auto-poly-*                       # 查看所有服务状态"
echo "  systemctl restart auto-poly-app                    # 重启 Dashboard"
echo "  systemctl restart auto-poly-recommendation-executor  # 重启自动执行器"
echo "  journalctl -u auto-poly-app -f                     # 查看日志"
echo "  systemctl stop auto-poly-app                       # 停止某个服务"
echo "=========================================="
