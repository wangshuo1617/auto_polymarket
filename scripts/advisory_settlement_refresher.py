"""
Advisory settlement refresher — long-running process for systemd (R2).

Periodically calls `services.advisory.settlement_adapter.refresh_settlement_feed()`
to write a fresh `settlement_feed_versions` row from Polymarket gamma. The
batch runner (R1) consumes the latest version on every iteration; running
the refresher on a faster cadence keeps settlement state fresh without
blowing up the batch loop's wall time.

Cadence guidance:
- BTC up/down markets settle at month end (one-shot per condition); during
  the month settlement state is mostly stable, so default 600s (10min) is
  plenty. Near month-end (last few days) consider lowering to 60-120s via
  `--interval`.

Health behavior (mirrors R1):
- Each iteration wrapped in try/except; exception → exponential backoff
  60→600s before next iteration. Reset on success.
- A `RefreshResult` with `refresh_status='failed'` (universe non-empty but
  zero records returned) also counts as failure and triggers backoff.
- `partial` (some descriptors missing) counts as success — feed is usable.

Signal handling:
- SIGTERM / SIGINT → finish current iteration then exit cleanly (0).

Usage:
    LD_PRELOAD="" uv run scripts/advisory_settlement_refresher.py
    LD_PRELOAD="" uv run scripts/advisory_settlement_refresher.py \
        --interval 600 --max-strikes 8 --once
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from services.advisory.inputs import (  # noqa: E402
    fetch_descriptors,
    select_active_month_slug,
)
from services.advisory.settlement_adapter import refresh_settlement_feed  # noqa: E402

logger = logging.getLogger("advisory_settlement_refresher")


# ---------------------------------------------------------------------------
#  Logging — mirrors R1 / five_minute_trade pattern
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    fh = RotatingFileHandler(
        filename="logs/advisory_settlement_refresher.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setLevel(logging.INFO)
    fh.setFormatter(formatter)

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(formatter)

    root.addHandler(fh)
    root.addHandler(sh)

    for noisy in ("urllib3", "httpx", "httpcore", "py_clob_client_v2",
                  "py_clob_client_v2.http_helpers.helpers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
#  Runner
# ---------------------------------------------------------------------------

@dataclass
class RunnerState:
    stop: bool = False
    consecutive_errors: int = 0
    last_success_at: datetime = None  # type: ignore[assignment]
    iterations: int = 0


def _install_signal_handlers(state: RunnerState) -> None:
    def _handler(signum, frame):
        logger.info("received signal %s, will exit after current iteration", signum)
        state.stop = True
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _run_one(slug: str, max_strikes: int) -> dict:
    descriptors = fetch_descriptors(slug, max_strikes=max_strikes)
    result = refresh_settlement_feed(descriptors)
    return {
        "settlement_feed_version": result.settlement_feed_version,
        "refresh_status": result.refresh_status,
        "rows_upserted": result.rows_upserted,
        "missing_count": len(result.missing_condition_ids or []),
        "descriptors": len(descriptors),
        "effect_hash": result.effect_hash[:12],
    }


def _backoff_seconds(consecutive_errors: int, base: float = 60.0, cap: float = 600.0) -> float:
    if consecutive_errors <= 0:
        return 0.0
    return min(cap, base * (2 ** (consecutive_errors - 1)))


def _interruptible_sleep(seconds: float, state: RunnerState) -> None:
    end = time.monotonic() + seconds
    while not state.stop and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def main():
    parser = argparse.ArgumentParser(
        description="Advisory settlement refresher (R2, systemd-friendly)")
    parser.add_argument("--slug", default=None,
                        help="Polymarket event slug; default = active month BTC event "
                             "(auto-rolls forward at month end via select_active_month_slug)")
    parser.add_argument("--interval", type=float, default=600.0,
                        help="Seconds between successful iterations (default 600 = 10min)")
    parser.add_argument("--max-strikes", type=int, default=8,
                        help="Number of strikes to refresh (default 8; superset of batch's 6)")
    parser.add_argument("--once", action="store_true",
                        help="Run a single iteration then exit (smoke test)")
    parser.add_argument("--error-backoff-base", type=float, default=60.0)
    parser.add_argument("--error-backoff-cap", type=float, default=600.0)
    args = parser.parse_args()

    _configure_logging()

    state = RunnerState()
    _install_signal_handlers(state)

    logger.info("advisory settlement refresher starting interval=%ss max_strikes=%d once=%s",
                args.interval, args.max_strikes, args.once)

    while not state.stop:
        slug = args.slug or select_active_month_slug()
        state.iterations += 1
        t0 = time.monotonic()
        try:
            summary = _run_one(slug, args.max_strikes)
        except Exception as exc:
            state.consecutive_errors += 1
            backoff = _backoff_seconds(state.consecutive_errors,
                                       base=args.error_backoff_base,
                                       cap=args.error_backoff_cap)
            logger.error("iteration %d FAILED slug=%s err=%s; backoff=%.0fs (consec=%d)\n%s",
                         state.iterations, slug, exc, backoff, state.consecutive_errors,
                         traceback.format_exc())
            if args.once:
                return 1
            _interruptible_sleep(backoff, state)
            continue

        elapsed = time.monotonic() - t0
        if summary["refresh_status"] in ("ok", "partial"):
            state.consecutive_errors = 0
            state.last_success_at = datetime.now(timezone.utc)
            logger.info(
                "iteration %d ok version=%s status=%s descriptors=%d rows=%d missing=%d "
                "hash=%s elapsed=%.1fs",
                state.iterations, summary["settlement_feed_version"],
                summary["refresh_status"], summary["descriptors"],
                summary["rows_upserted"], summary["missing_count"],
                summary["effect_hash"], elapsed)
            if args.once:
                return 0
            _interruptible_sleep(args.interval, state)
        else:
            # status='failed' — universe non-empty but zero records
            state.consecutive_errors += 1
            backoff = _backoff_seconds(state.consecutive_errors,
                                       base=args.error_backoff_base,
                                       cap=args.error_backoff_cap)
            logger.error(
                "iteration %d refresh FAILED version=%s descriptors=%d missing=%d; "
                "backoff=%.0fs (consec=%d)",
                state.iterations, summary["settlement_feed_version"],
                summary["descriptors"], summary["missing_count"],
                backoff, state.consecutive_errors)
            if args.once:
                return 1
            _interruptible_sleep(backoff, state)

    logger.info("advisory settlement refresher exiting cleanly after %d iterations",
                state.iterations)
    return 0


if __name__ == "__main__":
    sys.exit(main())
