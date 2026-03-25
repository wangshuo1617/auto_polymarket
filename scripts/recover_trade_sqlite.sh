#!/usr/bin/env bash
# 将损坏的 SQLite 用 .recover 导出到新文件（需系统 sqlite3 命令，3.29+）。
#
# 用法（仓库根）:
#   bash scripts/stop_mp_trade.sh
#   bash scripts/recover_trade_sqlite.sh              # 默认: tmp/trade.sqlite3 或 $SQLITE_DB_PATH
#   bash scripts/recover_trade_sqlite.sh path/to.db
#
# 成功后: 自行 pragma integrity_check，再 mv 覆盖原文件或改 .env 里 SQLITE_DB_PATH。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

if ! command -v sqlite3 >/dev/null 2>&1; then
  echo "错误: 未找到 sqlite3 命令。请安装 SQLite CLI（含 .recover），例如: sudo apt install sqlite3"
  exit 1
fi

REL="${1:-}"
if [ -z "$REL" ]; then
  if [ -n "${SQLITE_DB_PATH:-}" ]; then
    REL="$SQLITE_DB_PATH"
  else
    REL="tmp/trade.sqlite3"
  fi
fi

if [[ "$REL" = /* ]]; then
  SRC="$REL"
else
  SRC="$PROJECT_ROOT/$REL"
fi
if [ ! -f "$SRC" ]; then
  echo "错误: 文件不存在: $SRC"
  exit 1
fi

TS="$(date +%Y%m%d_%H%M%S)"
BAK="${SRC}.corrupt_backup_${TS}"
DST="${SRC}.recovered_${TS}"

echo "备份损坏库 -> $BAK"
cp -a "$SRC" "$BAK"

echo "运行 .recover -> $DST （大库可能较慢）"
set +e
sqlite3 "$SRC" ".recover" | sqlite3 "$DST"
r0=${PIPESTATUS[0]:-1}
r1=${PIPESTATUS[1]:-1}
set -e
if [ "$r0" -ne 0 ] || [ "$r1" -ne 0 ]; then
  echo "错误: recover 管道失败 (.recover=$r0, 写入新库=$r1)。可手动:"
  echo "  sqlite3 \"$SRC\" \".recover\" | sqlite3 \"$DST\""
  exit 1
fi

echo ""
echo "请验证:"
echo "  sqlite3 \"$DST\" \"pragma integrity_check;\""
echo "确认 ok 后，在停掉所有写入进程的前提下替换:"
echo "  mv \"$DST\" \"$SRC\""
echo "或保留新路径并修改 .env 中 SQLITE_DB_PATH="
