#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

DB_PATH="${1:-logs/btc_poly_1s.duckdb}"
CMD="${2:-latest}"
ARG3="${3:-}"

if [ ! -f "$DB_PATH" ]; then
  echo "❌ 数据库文件不存在: $DB_PATH"
  echo "请先启动监控服务或传入正确 DB 路径。"
  exit 1
fi

run_sql() {
  local sql="$1"
  DB_PATH_ENV="$DB_PATH" SQL_ENV="$sql" uv run python - <<'PY'
import os
import duckdb

conn = duckdb.connect(os.environ["DB_PATH_ENV"])
try:
    result = conn.execute(os.environ["SQL_ENV"]).fetchdf()
    print(result)
finally:
    conn.close()
PY
}

usage() {
  cat <<EOF
用法:
  ./scripts/query_btc_poly_1s.sh [db_path] [command] [arg]

command:
  tables                     查看所有表
  schema                     查看 btc_poly_1s_ticks 表结构
  latest [N]                 查看最新 N 条 (默认 20)
  market <market_slug>       查看指定 5m 市场窗口
  last_hour                  查看最近 1 小时样本
  corr                       计算 BTC 1s 变化与 up/down 中间价 1s 变化相关性
  stale [age_ms]             查看价格延迟样本 (默认 age_ms >= 3000)
  sql "<SQL>"                执行自定义 SQL

示例:
  ./scripts/query_btc_poly_1s.sh
  ./scripts/query_btc_poly_1s.sh logs/btc_poly_1s.duckdb latest 50
  ./scripts/query_btc_poly_1s.sh logs/btc_poly_1s.duckdb market btc-updown-5m-1741032000
  ./scripts/query_btc_poly_1s.sh logs/btc_poly_1s.duckdb corr
  ./scripts/query_btc_poly_1s.sh logs/btc_poly_1s.duckdb sql "SELECT count(*) FROM btc_poly_1s_ticks"
EOF
}

case "$CMD" in
  tables)
    run_sql "SHOW TABLES"
    ;;
  schema)
    run_sql "DESCRIBE btc_poly_1s_ticks"
    ;;
  latest)
    N="${ARG3:-20}"
    if ! [[ "$N" =~ ^[0-9]+$ ]]; then
      echo "❌ latest 的 N 必须是整数"
      exit 1
    fi
    run_sql "SELECT ts_utc, market_slug, btc_price, up_best_bid, up_best_ask, down_best_bid, down_best_ask, btc_age_ms, up_age_ms, down_age_ms FROM btc_poly_1s_ticks ORDER BY ts_sec DESC LIMIT ${N}"
    ;;
  market)
    MARKET_SLUG="${ARG3:-}"
    if [ -z "$MARKET_SLUG" ]; then
      echo "❌ market 需要 market_slug 参数"
      exit 1
    fi
    DB_PATH_ENV="$DB_PATH" MARKET_SLUG_ENV="$MARKET_SLUG" uv run python - <<'PY'
import os
import duckdb

conn = duckdb.connect(os.environ["DB_PATH_ENV"])
try:
    sql = """
    SELECT ts_utc, market_slug, btc_price,
           up_best_bid, up_best_ask,
           down_best_bid, down_best_ask,
           btc_age_ms, up_age_ms, down_age_ms
    FROM btc_poly_1s_ticks
    WHERE market_slug = ?
    ORDER BY ts_sec
    """
    result = conn.execute(sql, [os.environ["MARKET_SLUG_ENV"]]).fetchdf()
    print(result)
finally:
    conn.close()
PY
    ;;
  last_hour)
    run_sql "SELECT ts_utc, market_slug, btc_price, up_best_ask, down_best_ask FROM btc_poly_1s_ticks WHERE ts_sec >= epoch(now()) - 3600 ORDER BY ts_sec"
    ;;
  corr)
    run_sql "WITH x AS ( SELECT ts_sec, btc_price - lag(btc_price) OVER (ORDER BY ts_sec) AS btc_ret_1s, ((up_best_bid + up_best_ask)/2.0) - lag((up_best_bid + up_best_ask)/2.0) OVER (ORDER BY ts_sec) AS up_mid_chg_1s, ((down_best_bid + down_best_ask)/2.0) - lag((down_best_bid + down_best_ask)/2.0) OVER (ORDER BY ts_sec) AS down_mid_chg_1s FROM btc_poly_1s_ticks ) SELECT corr(btc_ret_1s, up_mid_chg_1s) AS corr_btc_up, corr(btc_ret_1s, down_mid_chg_1s) AS corr_btc_down, count(*) AS samples FROM x WHERE btc_ret_1s IS NOT NULL"
    ;;
  stale)
    AGE_MS="${ARG3:-3000}"
    if ! [[ "$AGE_MS" =~ ^[0-9]+$ ]]; then
      echo "❌ stale 的 age_ms 必须是整数"
      exit 1
    fi
    run_sql "SELECT ts_utc, market_slug, btc_age_ms, up_age_ms, down_age_ms, btc_price, up_best_ask, down_best_ask FROM btc_poly_1s_ticks WHERE coalesce(btc_age_ms,0) >= ${AGE_MS} OR coalesce(up_age_ms,0) >= ${AGE_MS} OR coalesce(down_age_ms,0) >= ${AGE_MS} ORDER BY ts_sec DESC LIMIT 200"
    ;;
  sql)
    CUSTOM_SQL="${ARG3:-}"
    if [ -z "$CUSTOM_SQL" ]; then
      echo "❌ sql 需要 SQL 字符串参数"
      exit 1
    fi
    run_sql "$CUSTOM_SQL"
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    echo "❌ 未知命令: $CMD"
    usage
    exit 1
    ;;
esac
