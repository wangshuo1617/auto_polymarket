#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

DEFAULT_STRATEGY="m=4,pre=7,diff=45,max=0.95,stake=5,hold=70,tp_cap=0.97,tp_val_cap=0.15,sl_ratio=2.5"
NOW_TS="$(date +%s)"
DEFAULT_SINCE_TS="1773450376"
DEFAULT_UNTIL_TS=""

STRATEGY="${1:-$DEFAULT_STRATEGY}"
SINCE_TS="${2:-$DEFAULT_SINCE_TS}"
UNTIL_TS="${3:-$DEFAULT_UNTIL_TS}"
DB_PATH="${4:-logs/trade.sqlite3}"
REPORT_JSON="${5:-output/5m_backtest_live_diff_report.json}"
BACKTEST_SUMMARY_CSV="${6:-output/5m_backtest_diff_summary.csv}"
BACKTEST_EVENTS_CSV="${7:-output/5m_backtest_diff_trade_events.csv}"
PRINT_TOP_N="${8:-10}"
EXISTING_BACKTEST_EVENTS_CSV="${9:-}"
TS_MODE="${10:---disable-output-timestamp}"
TRADE_COMPARE_CSV="${11:-output/5m_backtest_live_trade_compare.csv}"

USAGE="./scripts/5m_trade_diff.sh [strategy] [since_ts] [until_ts] [db_path] [report_json] [backtest_summary_csv] [backtest_events_csv] [print_top_n] [existing_backtest_events_csv] [--disable-output-timestamp|--with-timestamp] [trade_compare_csv]"

print_usage() {
  cat <<EOF
用法: $USAGE

示例1（全部默认，比较最近6小时）:
  ./scripts/5m_trade_diff.sh

示例2（指定核心参数）:
  ./scripts/5m_trade_diff.sh "m=3,pre=4,diff=50,max=0.9,stake=10,hold=60,tp_cap=0.99,tp_val_cap=0.2,sl_ratio=1.5" 1773282300 1773294476

示例3（跳过回测，直接对比已有回测逐单CSV）:
  ./scripts/5m_trade_diff.sh "m=3,pre=4,diff=50,max=0.9,stake=10,hold=60,tp_cap=0.99,tp_val_cap=0.2,sl_ratio=1.5" 1773282300 1773294476 logs/trade.sqlite3 output/report.json output/summary.csv output/events.csv 10 output/existing_backtest_events.csv

示例4（自定义逐笔逐市场对比CSV输出）:
  ./scripts/5m_trade_diff.sh "m=3,pre=4,diff=50,max=0.9,stake=10,hold=60,tp_cap=0.99,tp_val_cap=0.2,sl_ratio=1.5" 1773282300 1773294476 logs/trade.sqlite3 output/report.json output/summary.csv output/events.csv 10 "" --disable-output-timestamp output/my_trade_compare.csv
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  print_usage
  echo
  /root/auto_polymarket/.venv/bin/python -m services.five_minute_trade.trade_diff_service --help
  exit 0
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

echo "=========================================="
echo "运行 5m_trade_diff"
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "工作目录: $PROJECT_ROOT"
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
  /root/auto_polymarket/.venv/bin/python -m services.five_minute_trade.trade_diff_service
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
