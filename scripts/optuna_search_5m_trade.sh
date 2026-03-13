#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# Time range (required)
START_TS_SEC="${START_TS_SEC:-}"
END_TS_SEC="${END_TS_SEC:-}"

# Optuna controls
TRIALS="${TRIALS:-300}"
TIMEOUT_SEC="${TIMEOUT_SEC:-0}"
N_JOBS="${N_JOBS:-1}"
MIN_TRADES="${MIN_TRADES:-20}"
SCORE_MODE="${SCORE_MODE:-pnl_pf_over_mdd}"
SEED="${SEED:-42}"
ENFORCE_MULTI_OBJECTIVE="${ENFORCE_MULTI_OBJECTIVE:-1}"
MIN_WIN_RATE="${MIN_WIN_RATE:-0.70}"
MIN_PROFIT_FACTOR="${MIN_PROFIT_FACTOR:-1.30}"
MAX_MAX_DRAWDOWN="${MAX_MAX_DRAWDOWN:-30}"
PLATEAU_CHECK="${PLATEAU_CHECK:-1}"
PLATEAU_DIFF_DELTA="${PLATEAU_DIFF_DELTA:-5}"
PLATEAU_HOLD_DELTA="${PLATEAU_HOLD_DELTA:-10}"
PLATEAU_WEIGHT="${PLATEAU_WEIGHT:-0.35}"
WALK_FORWARD="${WALK_FORWARD:-0}"
WF_TRAIN_DAYS="${WF_TRAIN_DAYS:-7}"
WF_TEST_DAYS="${WF_TEST_DAYS:-3}"
WF_STEP_DAYS="${WF_STEP_DAYS:-3}"
WF_MAX_FOLDS="${WF_MAX_FOLDS:-0}"

# Search space
ENTRY_MINUTE_MIN="${ENTRY_MINUTE_MIN:-2}"
ENTRY_MINUTE_MAX="${ENTRY_MINUTE_MAX:-4}"
PRECLOSE_SEC_MIN="${PRECLOSE_SEC_MIN:-2}"
PRECLOSE_SEC_MAX="${PRECLOSE_SEC_MAX:-15}"
DIFF_MIN="${DIFF_MIN:-30}"
DIFF_MAX="${DIFF_MAX:-100}"
DIFF_STEP="${DIFF_STEP:-5}"
MAX_ENTRY_MIN="${MAX_ENTRY_MIN:-0.70}"
MAX_ENTRY_MAX="${MAX_ENTRY_MAX:-0.95}"
MAX_ENTRY_STEP="${MAX_ENTRY_STEP:-0.05}"
HOLD_MIN="${HOLD_MIN:-0}"
HOLD_MAX="${HOLD_MAX:-120}"
HOLD_STEP="${HOLD_STEP:-10}"
TP_CAP_MIN="${TP_CAP_MIN:-0.85}"
TP_CAP_MAX="${TP_CAP_MAX:-0.99}"
TP_CAP_STEP="${TP_CAP_STEP:-0.02}"
TP_VAL_MIN="${TP_VAL_MIN:-0.05}"
TP_VAL_MAX="${TP_VAL_MAX:-0.30}"
TP_VAL_STEP="${TP_VAL_STEP:-0.05}"
SL_RATIO_MIN="${SL_RATIO_MIN:-0.8}"
SL_RATIO_MAX="${SL_RATIO_MAX:-2.5}"
SL_RATIO_STEP="${SL_RATIO_STEP:-0.1}"

# Fixed strategy/runtime knobs
STAKE_USD="${STAKE_USD:-10.0}"
DB_PATH="${DB_PATH:-${SQLITE_DB_PATH:-logs/trade.sqlite3}}"
TOXIC_UTC_HOURS="${TOXIC_UTC_HOURS:-}"
LIVE_LIKE="${LIVE_LIKE:-1}"

OUTPUT_JSON="${OUTPUT_JSON:-output/5m_optuna_best.json}"
TRIALS_CSV="${TRIALS_CSV:-output/5m_optuna_trials.csv}"
CANDIDATES_CSV="${CANDIDATES_CSV:-output/5m_optuna_candidates.csv}"
MIN_PLATEAU_PASS_RATE="${MIN_PLATEAU_PASS_RATE:-0.70}"
TOP_CANDIDATES="${TOP_CANDIDATES:-50}"
DISABLE_OUTPUT_TIMESTAMP="${DISABLE_OUTPUT_TIMESTAMP:-0}"

if [ -z "$START_TS_SEC" ] || [ -z "$END_TS_SEC" ]; then
  echo "❌ START_TS_SEC 和 END_TS_SEC 是必填环境变量"
  echo "示例: START_TS_SEC=1773282445 END_TS_SEC=1773368672 bash scripts/optuna_search_5m_trade.sh"
  exit 1
fi

CMD=(
  /root/auto_polymarket/.venv/bin/python -m services.five_minute_trade.optuna_search_service
  --db-path "$DB_PATH"
  --start-ts-sec "$START_TS_SEC"
  --end-ts-sec "$END_TS_SEC"
  --trials "$TRIALS"
  --timeout-sec "$TIMEOUT_SEC"
  --n-jobs "$N_JOBS"
  --min-trades "$MIN_TRADES"
  --score-mode "$SCORE_MODE"
  --seed "$SEED"
  --min-win-rate "$MIN_WIN_RATE"
  --min-profit-factor "$MIN_PROFIT_FACTOR"
  --max-max-drawdown "$MAX_MAX_DRAWDOWN"
  --plateau-diff-delta "$PLATEAU_DIFF_DELTA"
  --plateau-hold-delta "$PLATEAU_HOLD_DELTA"
  --plateau-weight "$PLATEAU_WEIGHT"
  --min-plateau-pass-rate "$MIN_PLATEAU_PASS_RATE"
  --top-candidates "$TOP_CANDIDATES"
  --entry-minute-min "$ENTRY_MINUTE_MIN"
  --entry-minute-max "$ENTRY_MINUTE_MAX"
  --preclose-sec-min "$PRECLOSE_SEC_MIN"
  --preclose-sec-max "$PRECLOSE_SEC_MAX"
  --diff-min "$DIFF_MIN"
  --diff-max "$DIFF_MAX"
  --diff-step "$DIFF_STEP"
  --max-entry-min "$MAX_ENTRY_MIN"
  --max-entry-max "$MAX_ENTRY_MAX"
  --max-entry-step "$MAX_ENTRY_STEP"
  --stake-usd "$STAKE_USD"
  --hold-min "$HOLD_MIN"
  --hold-max "$HOLD_MAX"
  --hold-step "$HOLD_STEP"
  --tp-cap-min "$TP_CAP_MIN"
  --tp-cap-max "$TP_CAP_MAX"
  --tp-cap-step "$TP_CAP_STEP"
  --tp-val-min "$TP_VAL_MIN"
  --tp-val-max "$TP_VAL_MAX"
  --tp-val-step "$TP_VAL_STEP"
  --sl-ratio-min "$SL_RATIO_MIN"
  --sl-ratio-max "$SL_RATIO_MAX"
  --sl-ratio-step "$SL_RATIO_STEP"
  --toxic-utc-hours "$TOXIC_UTC_HOURS"
  --output-json "$OUTPUT_JSON"
  --trials-csv "$TRIALS_CSV"
  --candidates-csv "$CANDIDATES_CSV"
)

if [ "$ENFORCE_MULTI_OBJECTIVE" = "1" ]; then
  CMD+=(--enforce-multi-objective)
fi

if [ "$PLATEAU_CHECK" = "1" ]; then
  CMD+=(--plateau-check)
fi

if [ "$WALK_FORWARD" = "1" ]; then
  CMD+=(
    --walk-forward
    --wf-train-days "$WF_TRAIN_DAYS"
    --wf-test-days "$WF_TEST_DAYS"
    --wf-step-days "$WF_STEP_DAYS"
    --wf-max-folds "$WF_MAX_FOLDS"
  )
fi

if [ "$LIVE_LIKE" = "1" ]; then
  CMD+=(--live-like)
fi

if [ "$DISABLE_OUTPUT_TIMESTAMP" = "1" ]; then
  CMD+=(--disable-output-timestamp)
fi

echo "=========================================="
echo "Optuna 搜索 5m 参数"
echo "时间范围: $START_TS_SEC -> $END_TS_SEC"
echo "试验次数: $TRIALS"
echo "最小交易数: $MIN_TRADES"
echo "评分模式: $SCORE_MODE"
if [ "$ENFORCE_MULTI_OBJECTIVE" = "1" ]; then
  echo "多目标约束: 开启 (win_rate>=${MIN_WIN_RATE}, pf>=${MIN_PROFIT_FACTOR}, mdd<=${MAX_MAX_DRAWDOWN})"
else
  echo "多目标约束: 关闭"
fi
if [ "$PLATEAU_CHECK" = "1" ]; then
  echo "高原稳健性: 开启 (diff±${PLATEAU_DIFF_DELTA}, hold±${PLATEAU_HOLD_DELTA}, weight=${PLATEAU_WEIGHT})"
else
  echo "高原稳健性: 关闭"
fi
echo "稳健候选导出: min_plateau_pass_rate=${MIN_PLATEAU_PASS_RATE}, top=${TOP_CANDIDATES}"
if [ "$WALK_FORWARD" = "1" ]; then
  echo "Walk-forward: 开启 (train=${WF_TRAIN_DAYS}d test=${WF_TEST_DAYS}d step=${WF_STEP_DAYS}d max_folds=${WF_MAX_FOLDS})"
else
  echo "Walk-forward: 关闭"
fi
echo "DB: $DB_PATH"
echo "=========================================="

"${CMD[@]}"
