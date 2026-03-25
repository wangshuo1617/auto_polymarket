#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

DEFAULT_STRATEGY="m=4,pre=9,diff=30,max=0.95,stake=20,hold=60,tp_cap=0.97,tp_val_cap=0.15,sl_ratio=0.9,cross=3,ud_diff=0.05"
NOW_TS="$(date +%s)"
DEFAULT_SINCE_TS="1773825462"
DEFAULT_UNTIL_TS=""

FAST_MODE="false"
if [[ "${1:-}" == "--fast-live-latest" ]]; then
  FAST_MODE="true"
fi

if [[ "$FAST_MODE" == "true" ]]; then
  STRATEGY=""
  SINCE_TS=""
  DB_PATH="${2:-tmp/trade.sqlite3}"
  UNTIL_TS="${3:-$DEFAULT_UNTIL_TS}"
  REPORT_JSON="${4:-output/5m_backtest_live_diff_report.json}"
  BACKTEST_SUMMARY_CSV="${5:-output/5m_backtest_diff_summary.csv}"
  BACKTEST_EVENTS_CSV="${6:-output/5m_backtest_diff_trade_events.csv}"
  PRINT_TOP_N="${7:-10}"
  EXISTING_BACKTEST_EVENTS_CSV="${8:-}"
  TS_MODE="${9:---disable-output-timestamp}"
  TRADE_COMPARE_CSV="${10:-output/5m_backtest_live_trade_compare.csv}"
else
  STRATEGY="${1:-$DEFAULT_STRATEGY}"
  SINCE_TS="${2:-$DEFAULT_SINCE_TS}"
  UNTIL_TS="${3:-$DEFAULT_UNTIL_TS}"
  DB_PATH="${4:-tmp/trade.sqlite3}"
  REPORT_JSON="${5:-output/5m_backtest_live_diff_report.json}"
  BACKTEST_SUMMARY_CSV="${6:-output/5m_backtest_diff_summary.csv}"
  BACKTEST_EVENTS_CSV="${7:-output/5m_backtest_diff_trade_events.csv}"
  PRINT_TOP_N="${8:-10}"
  EXISTING_BACKTEST_EVENTS_CSV="${9:-}"
  TS_MODE="${10:---disable-output-timestamp}"
  TRADE_COMPARE_CSV="${11:-output/5m_backtest_live_trade_compare.csv}"
fi

USAGE="./scripts/5m_trade_diff.sh [strategy] [since_ts] [until_ts] [db_path] [report_json] [backtest_summary_csv] [backtest_events_csv] [print_top_n] [existing_backtest_events_csv] [--disable-output-timestamp|--with-timestamp] [trade_compare_csv][--fast-live-latest]"

print_usage() {
  cat <<EOF
用法: $USAGE

示例1（全部默认，比较最近6小时）:
  ./scripts/5m_trade_diff.sh

示例2（指定核心参数）:
  ./scripts/5m_trade_diff.sh "m=3,pre=4,diff=50,max=0.9,stake=10,hold=60,tp_cap=0.99,tp_val_cap=0.2,sl_ratio=1.5" 1773282300 1773294476

示例3（跳过回测，直接对比已有回测逐单CSV）:
  ./scripts/5m_trade_diff.sh "m=3,pre=4,diff=50,max=0.9,stake=10,hold=60,tp_cap=0.99,tp_val_cap=0.2,sl_ratio=1.5" 1773282300 1773294476 tmp/trade.sqlite3 output/report.json output/summary.csv output/events.csv 10 output/existing_backtest_events.csv

示例4（自定义逐笔逐市场对比CSV输出）:
  ./scripts/5m_trade_diff.sh "m=3,pre=4,diff=50,max=0.9,stake=10,hold=60,tp_cap=0.99,tp_val_cap=0.2,sl_ratio=1.5" 1773282300 1773294476 tmp/trade.sqlite3 output/report.json output/summary.csv output/events.csv 10 "" --disable-output-timestamp output/my_trade_compare.csv

示例5（快速模式：自动读取最近一次 live 启动策略和启动时间）:
  ./scripts/5m_trade_diff.sh --fast-live-latest

示例6（快速模式 + 自定义数据库和截止时间）:
  ./scripts/5m_trade_diff.sh --fast-live-latest tmp/trade.sqlite3 1773629750
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  print_usage
  echo
  .venv/bin/python -m services.five_minute_trade.trade_diff_service --help
  exit 0
fi

if [[ "$FAST_MODE" == "true" ]]; then
  if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "❌ 快速模式依赖 sqlite3 命令，但当前环境未安装"
    exit 1
  fi

  set +e
  LATEST_LIVE_ROW="$(sqlite3 -noheader -separator $'\t' "$DB_PATH" "SELECT strategy_signature, start_ts_sec FROM trade_startups WHERE mode='live' ORDER BY start_ts_sec DESC, id DESC LIMIT 1;")"
  SQLITE_EXIT=$?
  set -e

  if (( SQLITE_EXIT != 0 )); then
    echo "❌ 快速模式读取数据库失败: $DB_PATH"
    echo "请确认 trade_startups 表存在且数据库可读"
    exit 1
  fi

  if [[ -z "$LATEST_LIVE_ROW" ]]; then
    echo "❌ 快速模式未找到 live 启动记录（trade_startups.mode='live'）"
    exit 1
  fi

  IFS=$'\t' read -r STRATEGY SINCE_TS <<< "$LATEST_LIVE_ROW"

  if [[ -z "$STRATEGY" || -z "$SINCE_TS" ]]; then
    echo "❌ 快速模式解析最新 live 启动记录失败"
    exit 1
  fi
fi

if ! [[ "$SINCE_TS" =~ ^[0-9]+$ ]]; then
  echo "❌ since_ts 必须是整数时间戳"
  print_usage
  exit 1
fi

# until_ts 允许不传：为空时默认取当前时间。
if [[ -z "$UNTIL_TS" ]]; then
  UNTIL_TS="$NOW_TS"
fi

if ! [[ "$UNTIL_TS" =~ ^[0-9]+$ ]]; then
  echo "❌ until_ts 必须是整数时间戳"
  print_usage
  exit 1
fi

if (( SINCE_TS > UNTIL_TS )); then
  echo "❌ since_ts 不能大于 until_ts"
  print_usage
  exit 1
fi

if ! [[ "$PRINT_TOP_N" =~ ^[0-9]+$ ]]; then
  echo "❌ print_top_n 必须是大于等于0的整数"
  print_usage
  exit 1
fi

if [[ "$TS_MODE" != "--disable-output-timestamp" && "$TS_MODE" != "--with-timestamp" ]]; then
  echo "❌ 第10个参数仅支持 --disable-output-timestamp 或 --with-timestamp"
  print_usage
  exit 1
fi

check_db_health() {
  local db_path="$1"
  if [[ ! -f "$db_path" ]]; then
    return 1
  fi
  set +e
  .venv/bin/python - "$db_path" <<'PY'
import sqlite3
import sys

db_path = sys.argv[1]
try:
    conn = sqlite3.connect(db_path)
    cur = conn.execute("PRAGMA integrity_check;")
    row = cur.fetchone()
    conn.close()
    ok = bool(row and str(row[0]).lower() == "ok")
    sys.exit(0 if ok else 2)
except Exception:
    sys.exit(1)
PY
  local rc=$?
  set -e
  return $rc
}

if ! check_db_health "$DB_PATH"; then
  ALT_DB_PATH=""
  if [[ "$DB_PATH" == logs/* ]]; then
    ALT_DB_PATH="tmp/trade.sqlite3"
  elif [[ "$DB_PATH" == output/* ]]; then
    ALT_DB_PATH="tmp/trade.sqlite3"
  elif [[ "$DB_PATH" == tmp/* ]]; then
    ALT_DB_PATH="output/trade.sqlite3"
  fi

  if [[ -n "$ALT_DB_PATH" ]] && check_db_health "$ALT_DB_PATH"; then
    echo "⚠️ 检测到数据库不可用或损坏: $DB_PATH"
    echo "✅ 自动切换到可用数据库: $ALT_DB_PATH"
    DB_PATH="$ALT_DB_PATH"
  else
    echo "❌ 数据库不可用或损坏: $DB_PATH"
    if [[ -n "$ALT_DB_PATH" ]]; then
      echo "❌ 备用数据库也不可用: $ALT_DB_PATH"
    fi
    echo "建议手动检查:"
    echo "  sqlite3 \"$DB_PATH\" \"PRAGMA integrity_check;\""
    exit 1
  fi
fi

echo "=========================================="
echo "运行 5m_trade_diff"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
if [[ "$FAST_MODE" == "true" ]]; then
  echo "模式: 快速模式（最近一次 live 启动）"
fi
echo "策略签名: $STRATEGY"
echo "比较区间: $SINCE_TS -> $UNTIL_TS"
echo "数据库: $DB_PATH"
echo "报告输出: $REPORT_JSON"
echo "回测汇总CSV: $BACKTEST_SUMMARY_CSV"
echo "回测逐单CSV: $BACKTEST_EVENTS_CSV"
echo "逐笔逐市场对比CSV: $TRADE_COMPARE_CSV"
echo "打印TOP N: $PRINT_TOP_N"
if [[ -n "$EXISTING_BACKTEST_EVENTS_CSV" ]]; then
  echo "已有回测逐单CSV: $EXISTING_BACKTEST_EVENTS_CSV (将跳过回测)"
else
  echo "已有回测逐单CSV: 无 (将自动跑回测)"
fi
echo "时间戳后缀模式: $TS_MODE"
echo "=========================================="

CMD=(
  .venv/bin/python -m services.five_minute_trade.trade_diff_service
  --strategy "$STRATEGY"
  --since-ts "$SINCE_TS"
  --until-ts "$UNTIL_TS"
  --db-path "$DB_PATH"
  --report-json "$REPORT_JSON"
  --backtest-summary-csv "$BACKTEST_SUMMARY_CSV"
  --backtest-generated-events-csv "$BACKTEST_EVENTS_CSV"
  --trade-compare-csv "$TRADE_COMPARE_CSV"
  --print-top-n "$PRINT_TOP_N"
)

if [[ "$TS_MODE" == "--disable-output-timestamp" ]]; then
  CMD+=(--disable-output-timestamp)
fi

if [[ -n "$EXISTING_BACKTEST_EVENTS_CSV" ]]; then
  CMD+=(--backtest-events-csv "$EXISTING_BACKTEST_EVENTS_CSV")
fi

"${CMD[@]}"
