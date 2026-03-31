#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

PG_DSN="${PG_DSN:-}"
MAX_STALE_SEC="${BTC_1S_MAX_STALE_SEC:-20}"
RECHECK_SEC="${BTC_1S_RECHECK_SEC:-45}"
RESTART_TARGET="${BTC_1S_RESTART_TARGET:-auto-poly-btc-monitor}"

if ! [[ "$MAX_STALE_SEC" =~ ^[0-9]+$ ]] || [ "$MAX_STALE_SEC" -lt 5 ]; then
  echo "[btc1s-freshness] invalid BTC_1S_MAX_STALE_SEC=$MAX_STALE_SEC, fallback to 20"
  MAX_STALE_SEC=20
fi
if ! [[ "$RECHECK_SEC" =~ ^[0-9]+$ ]] || [ "$RECHECK_SEC" -lt 5 ]; then
  echo "[btc1s-freshness] invalid BTC_1S_RECHECK_SEC=$RECHECK_SEC, fallback to 45"
  RECHECK_SEC=45
fi

if [ -z "$PG_DSN" ]; then
  echo "[btc1s-freshness] PG_DSN not set, skip"
  exit 0
fi

last_ts_sec="$(psql "$PG_DSN" -tAc "SELECT COALESCE(EXTRACT(EPOCH FROM MAX(ts_utc))::bigint, 0) FROM btc_poly_1s_ticks;" 2>/dev/null || echo 0)"
if ! [[ "$last_ts_sec" =~ ^[0-9]+$ ]]; then
  last_ts_sec=0
fi

now_ts_sec="$(date -u +%s)"
stale_sec=$((now_ts_sec - last_ts_sec))

if [ "$last_ts_sec" -gt 0 ] && [ "$stale_sec" -le "$MAX_STALE_SEC" ]; then
  echo "[btc1s-freshness] healthy: lag=${stale_sec}s threshold=${MAX_STALE_SEC}s"
  exit 0
fi

echo "[btc1s-freshness] stale detected: lag=${stale_sec}s threshold=${MAX_STALE_SEC}s, restarting $RESTART_TARGET"
systemctl restart "$RESTART_TARGET"

sleep "$RECHECK_SEC"
last_ts_after="$(psql "$PG_DSN" -tAc "SELECT COALESCE(EXTRACT(EPOCH FROM MAX(ts_utc))::bigint, 0) FROM btc_poly_1s_ticks;" 2>/dev/null || echo 0)"
if ! [[ "$last_ts_after" =~ ^[0-9]+$ ]]; then
  last_ts_after=0
fi

now_after_sec="$(date -u +%s)"
lag_after=$((now_after_sec - last_ts_after))
if [ "$last_ts_after" -gt "$last_ts_sec" ]; then
  echo "[btc1s-freshness] recovered after restart: advanced_from=${last_ts_sec} to=${last_ts_after}, lag=${lag_after}s"
  exit 0
fi

echo "[btc1s-freshness] still stale after restart: last_before=${last_ts_sec} last_after=${last_ts_after} lag=${lag_after}s"
exit 1
