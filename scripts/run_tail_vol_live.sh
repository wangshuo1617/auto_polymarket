#!/usr/bin/env bash
# 实盘：轮询 SQLite tick，尾盘满足策略则下单（默认 dry-run）。
# 需先运行 btc_1s_market_monitor 写入 btc_poly_1s_ticks；需 .env 中 Polymarket 密钥（真下单时）。
#
#   ./scripts/run_tail_vol_live.sh
#   ./scripts/run_tail_vol_live.sh --execute --stake-usd 5
#   ./scripts/run_tail_vol_live.sh --db ~/data/trade.sqlite3
#   TAIL_VOL_NO_UV=1 python -m tail_vol_trade.live

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p logs

resolve_python() {
  if [ -n "${PYTHON:-}" ]; then
    echo "$PYTHON"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    echo python3
    return
  fi
  if command -v python >/dev/null 2>&1; then
    echo python
    return
  fi
  echo "error: no python3/python in PATH" >&2
  exit 1
}

if [ "${TAIL_VOL_NO_UV:-}" != "1" ] && command -v uv >/dev/null 2>&1; then
  exec uv run python -m tail_vol_trade.live "$@"
fi

PY="$(resolve_python)"
exec "$PY" -m tail_vol_trade.live "$@"
