"""Advisory chain fills historical backfill (v2 B3).

一次性脚本: 把过去 N 天的 advisory-universe TRADE activity 全部拉进
advisory_chain_fills (UNIQUE 去重保证幂等)。后续 60s poller 接手增量。

执行:
    LD_PRELOAD="" uv run python scripts/advisory_fills_backfill.py --days 30
    LD_PRELOAD="" uv run python scripts/advisory_fills_backfill.py --days 7 --profile analyze

注意: data-api 不强制限速但页大 500 时建议每窗口 sleep 一下避免被 ban。
默认每 24h 一窗口, 每窗口间 sleep 1s。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data.database import get_conn  # noqa: E402
from data.polymarket import get_polymarket_context  # noqa: E402
from services.advisory.chain_fills_poller import (  # noqa: E402
    _fetch_activity,
    _insert_fill,
    PROFILES,
)

logger = logging.getLogger("advisory.backfill")

WINDOW_SECONDS = 24 * 3600
SLEEP_BETWEEN_WINDOWS = 1.0


def backfill_profile(profile: str, days: int, dry_run: bool) -> dict:
    ctx = get_polymarket_context(profile)
    wallet = ctx.wallet_address
    now_ts = int(time.time())
    earliest_ts = now_ts - days * 24 * 3600
    totals = {"profile": profile, "fetched": 0, "inserted": 0,
              "duplicate": 0, "filtered": 0, "windows": 0, "errors": 0}

    cursor = now_ts
    while cursor > earliest_ts:
        win_end = cursor
        win_start = max(earliest_ts, cursor - WINDOW_SECONDS)
        try:
            items = _fetch_activity(wallet, win_start, win_end)
        except Exception as exc:
            logger.warning("backfill %s window=[%d,%d] fetch failed: %s",
                           profile, win_start, win_end, exc)
            totals["errors"] += 1
            cursor = win_start
            time.sleep(SLEEP_BETWEEN_WINDOWS)
            continue
        totals["fetched"] += len(items)
        totals["windows"] += 1
        if items and not dry_run:
            with get_conn() as conn:
                cur = conn.cursor()
                for it in items:
                    outcome = _insert_fill(cur, it, wallet, profile)
                    if outcome == "inserted":
                        totals["inserted"] += 1
                    elif outcome == "duplicate":
                        totals["duplicate"] += 1
                    else:
                        totals["filtered"] += 1
                conn.commit()
        elif items and dry_run:
            for it in items:
                slug = str(it.get("eventSlug") or "")
                if slug.startswith("what-price-will-bitcoin-hit-in"):
                    totals["inserted"] += 1
                else:
                    totals["filtered"] += 1
        logger.info(
            "backfill %s window=[%s,%s] fetched=%d inserted=%d dup=%d filtered=%d",
            profile,
            datetime.fromtimestamp(win_start, tz=timezone.utc).isoformat(timespec="minutes"),
            datetime.fromtimestamp(win_end, tz=timezone.utc).isoformat(timespec="minutes"),
            len(items), totals["inserted"], totals["duplicate"], totals["filtered"],
        )
        cursor = win_start
        time.sleep(SLEEP_BETWEEN_WINDOWS)
    return totals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--profile", default="all", choices=["all", *PROFILES])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    profiles = [args.profile] if args.profile != "all" else list(PROFILES)
    out = {"days": args.days, "dry_run": args.dry_run, "results": []}
    for p in profiles:
        out["results"].append(backfill_profile(p, args.days, args.dry_run))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
