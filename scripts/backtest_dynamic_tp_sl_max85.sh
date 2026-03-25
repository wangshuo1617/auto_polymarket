#!/bin/bash
# 仅测试 dynamic 模式、max_entry_price=0.85 下 sl_ratio 与 tp_value_cap 的最优组合。
# 其它维度收窄以突出 tp_val_cap × sl_ratio 的对比。
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

DB_PATH="${1:-tmp/trade.sqlite3}"

echo "=========================================="
echo "Dynamic TP/SL 回测 (max_entry=0.85)"
echo "DB: $DB_PATH"
echo "=========================================="

python scripts/backtest_5m_trade_params.py \
  --db-path "$DB_PATH" \
  --tp-sl-mode-grid dynamic \
  --max-entry-price-grid 0.85 \
  --entry-minute-grid "2,3" \
  --entry-preclose-sec-grid 5 \
  --min-direction-diff-grid "30,50" \
  --min-hold-before-close-sec-grid 40 \
  --tp-price-cap-grid "0.95,0.99" \
  --tp-value-cap-grid "0.1,0.12,0.15,0.18,0.2" \
  --sl-to-tp-ratio-grid "1.0,1.2,1.333333,1.5,1.8" \
  --stake-usd-grid 10 \
  --sort-by total_pnl \
  --top-k 30 \
  --output-csv "output/5m_backtest_dynamic_max85.csv" \
  --disable-output-timestamp

echo ""
echo "结果已写入 output/5m_backtest_dynamic_max85.csv，按 total_pnl 排序的前 30 为 sl_ratio × tp_val_cap 在 max=0.85 下的较优组合。"
