#!/bin/bash
# 初始化第二账号的 PostgreSQL 数据库表
# 用法: ACC2_PG_DSN="postgresql://..." ./scripts/init_acc2_db.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 从 .env 提取 ACC2_PG_DSN（兼容 python-dotenv 格式，不直接 source）
if [ -z "${ACC2_PG_DSN:-}" ] && [ -f "$PROJECT_ROOT/.env" ]; then
  _val=$(grep -E '^ACC2_PG_DSN=' "$PROJECT_ROOT/.env" | head -1 | sed 's/^ACC2_PG_DSN=//' | sed 's/^"//;s/"$//')
  if [ -n "$_val" ]; then
    export ACC2_PG_DSN="$_val"
  fi
fi

if [ -z "${ACC2_PG_DSN:-}" ]; then
  echo "❌ 必须设置 ACC2_PG_DSN 环境变量"
  exit 1
fi

echo "正在初始化第二账号数据库..."
echo "DSN: ${ACC2_PG_DSN%%@*}@***"

PG_DSN="$ACC2_PG_DSN" uv run python -c "
from data.database import init_db
init_db()
print('✅ 数据库表初始化完成')
"
