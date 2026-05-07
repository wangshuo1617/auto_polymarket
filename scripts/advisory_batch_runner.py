"""
Advisory batch runner — long-running process for systemd (R1).

Loops `services.advisory.computer.run_advisory_batch()` every N seconds
with real-data inputs from `services.advisory.inputs.assemble_batch_inputs()`.

Health behavior:
- Each iteration is wrapped in try/except so a single failure (network /
  Polymarket / PG hiccup) doesn't crash the process.
- On exception, logs traceback and applies an **error backoff** (default
  60s → 60/120/240... capped at 600s) before the next iteration; resets
  on success.
- On `run_advisory_batch` returning `status='failed'` (hard step crash),
  uses the same backoff path.
- `status='complete'` (incl. degraded path with refresh_status='partial'/
  'failed') counts as success — the system has written a usable batch
  row that dashboard can display + metrics can pick up.

Signal handling:
- SIGTERM / SIGINT → finish current iteration then exit cleanly with 0.
  systemd ExecStop = systemd default (TERM then KILL after timeout).

Usage:
    LD_PRELOAD="" uv run scripts/advisory_batch_runner.py
    LD_PRELOAD="" uv run scripts/advisory_batch_runner.py \
        --interval 300 --max-strikes 6 --once
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

from services.advisory.computer import run_advisory_batch  # noqa: E402
from services.advisory.inputs import (  # noqa: E402
    assemble_batch_inputs,
    select_active_month_slug,
)

logger = logging.getLogger("advisory_batch_runner")


# ---------------------------------------------------------------------------
#  Logging — mirror project pattern (services/five_minute_trade/bootstrap.py)
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
        filename="logs/advisory_batch_runner.log",
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

    for noisy in ("urllib3", "httpx", "httpcore", "py_clob_client_v2", "py_clob_client_v2.http_helpers.helpers"):
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
    """Single iteration: assemble inputs + run batch + return summary dict."""
    inputs = assemble_batch_inputs(slug, max_strikes=max_strikes)
    result = run_advisory_batch(
        token_universe=inputs.universe,
        descriptors=inputs.descriptors,
        btc_tick=inputs.btc_tick,
        current_btc_price=inputs.btc_price,
        path_max_btc=inputs.path_max_btc,
        path_min_btc=inputs.path_min_btc,
        sigma_daily=inputs.sigma_daily,
        sigma_source=inputs.sigma_source,
        sigma_is_iv=False,
        drift_daily=inputs.drift_daily,
        days_left=inputs.days_left,
        quotes=inputs.quotes,
        positions=inputs.positions,
        as_of_utc=inputs.as_of_utc,
        user_thesis_id=inputs.user_thesis_id,
    )
    return {
        "batch_id": result.batch_id,
        "batch_sequence": result.batch_sequence,
        "status": result.status,
        "failure_step": result.failure_step,
        "failure_error": result.failure_error,
        "refresh_status": result.refresh_status,
        "missing_count": len(result.missing_condition_ids or []),
        "btc_price": inputs.btc_price,
        "days_left": inputs.days_left,
        "tokens": len(inputs.universe),
        "quoted": sum(1 for q in inputs.quotes.values()
                      if q.best_bid is not None or q.best_ask is not None),
    }


def _backoff_seconds(consecutive_errors: int, base: float = 60.0, cap: float = 600.0) -> float:
    """Exponential backoff: base * 2^(n-1), capped at `cap`."""
    if consecutive_errors <= 0:
        return 0.0
    return min(cap, base * (2 ** (consecutive_errors - 1)))


def _interruptible_sleep(seconds: float, state: RunnerState) -> None:
    """Sleep but check stop flag every 1s."""
    end = time.monotonic() + seconds
    while not state.stop and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def main():
    parser = argparse.ArgumentParser(description="Advisory batch runner (R1, systemd-friendly)")
    parser.add_argument("--slug", default=None,
                        help="Polymarket event slug; default = active month BTC event "
                             "(auto-rolls forward at month end via select_active_month_slug)")
    parser.add_argument("--interval", type=float, default=300.0,
                        help="Seconds between successful iterations (default 300 = 5min)")
    parser.add_argument("--max-strikes", type=int, default=6,
                        help="Number of strikes per batch (default 6)")
    parser.add_argument("--once", action="store_true",
                        help="Run a single iteration then exit (smoke test)")
    parser.add_argument("--error-backoff-base", type=float, default=60.0)
    parser.add_argument("--error-backoff-cap", type=float, default=600.0)
    args = parser.parse_args()

    _configure_logging()

    state = RunnerState()
    _install_signal_handlers(state)

    logger.info("advisory batch runner starting interval=%ss max_strikes=%d once=%s",
                args.interval, args.max_strikes, args.once)

    while not state.stop:
        slug = args.slug or select_active_month_slug()
        state.iterations += 1
        t0 = time.monotonic()
        try:
            summary = _run_one(slug, args.max_strikes)
        except Exception as exc:  # broad: network / PG / parse
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
        if summary["status"] == "complete":
            state.consecutive_errors = 0
            state.last_success_at = datetime.now(timezone.utc)
            logger.info(
                "iteration %d ok batch_id=%s seq=%s status=%s refresh=%s tokens=%d quoted=%d "
                "btc=$%.2f days_left=%.2f elapsed=%.1fs",
                state.iterations, summary["batch_id"], summary["batch_sequence"],
                summary["status"], summary["refresh_status"], summary["tokens"],
                summary["quoted"], summary["btc_price"], summary["days_left"], elapsed)
            if args.once:
                return 0
            _interruptible_sleep(args.interval, state)
        else:
            # batch row was created but status='failed' (hard step crash)
            state.consecutive_errors += 1
            backoff = _backoff_seconds(state.consecutive_errors,
                                       base=args.error_backoff_base,
                                       cap=args.error_backoff_cap)
            logger.error(
                "iteration %d batch FAILED batch_id=%s step=%s error=%s; backoff=%.0fs (consec=%d)",
                state.iterations, summary["batch_id"], summary["failure_step"],
                summary["failure_error"], backoff, state.consecutive_errors)
            if args.once:
                return 1
            _interruptible_sleep(backoff, state)

    logger.info("advisory batch runner exiting cleanly after %d iterations", state.iterations)
    return 0


if __name__ == "__main__":
    sys.exit(main())
