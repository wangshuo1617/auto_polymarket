#!/usr/bin/env bash
# 运行 tail_vol_trade 回测（默认 logs/trade.sqlite3，可用环境变量覆盖）。
# 每次运行会在 logs/tail_vol_trade.log 追加一行 JSONL（可用 --no-log-file 关闭）。
#
#   ./scripts/run_tail_vol_trade.sh
#   ./scripts/run_tail_vol_trade.sh --stake-usd 2
#   TAIL_VOL_DB=tmp/trade.sqlite3 ./scripts/run_tail_vol_trade.sh --vol-threshold 0.45
#   TAIL_VOL_LOG=logs/tail_vol_custom.log ./scripts/run_tail_vol_trade.sh
#   TAIL_VOL_NO_UV=1 ./scripts/run_tail_vol_trade.sh
#   PYTHON=/usr/bin/python3 TAIL_VOL_NO_UV=1 ./scripts/run_tail_vol_trade.sh
#
# 若报 $'\r': command not found，说明脚本是 CRLF，请转 LF 或 git checkout（见 .gitattributes）。
#
# 日志：本脚本跑的是「回测」，写入 logs/tail_vol_trade.log（JSONL），不是 tail_vol_live*.log。
#       实盘轮询请用：bash scripts/run_tail_vol_live.sh 或 bash scripts/run_tail_vol_execute.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

mkdir -p logs

DB="${TAIL_VOL_DB:-logs/trade.sqlite3}"

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
  echo "error: no python3/python in PATH; set PYTHON to the interpreter path" >&2
  exit 1
}

if [ "${TAIL_VOL_NO_UV:-}" != "1" ] && command -v uv >/dev/null 2>&1; then
  exec uv run python -m tail_vol_trade --db "$DB" "$@"
fi

PY="$(resolve_python)"
exec "$PY" -m tail_vol_trade --db "$DB" "$@"
