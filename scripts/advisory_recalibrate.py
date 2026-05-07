"""Advisory P1b — Recalibrate _CALIBRATION_CONTROL_POINTS.

Replays historical settled monthly BTC barrier markets using the
*current* fair-value model (post P0+P1a) and fits new bias-pp control
points by binning predictions vs realized binary outcomes.

Approach:
  1. Iterate slugs `what-price-will-bitcoin-hit-in-<month>-<year>` from
     Nov 2025 up to (current month - 1).
  2. For each slug, fetch the gamma event payload; for each market with
     a clean binary `outcomePrices` (yes/no won), parse strike/direction.
  3. Sample N prediction time points (default: 25/20/15/10/5/2 days
     before settlement). At each:
       - BTC spot = the close of the 1d kline immediately before T
       - σ = EWMA(λ=0.94) over previous 60 daily log returns
       - μ = shrunk mean log return (60d window)
       - path-to-date [month_start, T] using daily kline highs/lows
       - p_event = Step 0 (1 if path-to-date touched) else GBM
                   reflection from T → month_end
  4. Bin predictions by `distance_pct` at prediction time (buckets
     0-3, 3-8, 8-15, 15-30, 30+). Compute mean p_pred vs mean realized
     event outcome → bias_pp = (predicted - realized) * 100.
  5. Print suggested new `_CALIBRATION_CONTROL_POINTS`.

Run:
  LD_PRELOAD="" uv run scripts/advisory_recalibrate.py
"""

from __future__ import annotations

import calendar
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import requests  # noqa: E402

from services.advisory.path_metrics import (  # noqa: E402
    parse_slug_to_month_window,
    klines_to_log_returns,
    compute_drift_daily,
    compute_ewma_sigma,
)
from services.profit_optimizer import (  # noqa: E402
    _barrier_touch_prob, _extract_strike_and_direction,
)
from data.polymarket import get_event_token_id  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("recalibrate")

_BUCKETS = [(0, 3), (3, 8), (8, 15), (15, 30), (30, 100)]
_PREDICTION_OFFSETS_DAYS = [2, 5, 10, 15, 20, 25]
_MONTH_NAMES = [n.lower() for n in calendar.month_name if n]


def _iter_slugs(start_year: int, start_month: int,
                end_year: int, end_month: int) -> list[str]:
    out = []
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        out.append(f"what-price-will-bitcoin-hit-in-{_MONTH_NAMES[m-1]}-{y}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _fetch_klines_until(end_ms: int, total_days: int) -> list:
    """Fetch up to `total_days` daily klines ending <= end_ms."""
    out: list = []
    fetched = 0
    cur_end = end_ms
    while fetched < total_days:
        chunk = 1000 if (total_days - fetched) > 1000 else (total_days - fetched)
        r = requests.get(
            "https://data-api.binance.vision/api/v3/klines",
            params={"symbol": "BTCUSDT", "interval": "1d", "limit": chunk, "endTime": cur_end},
            timeout=15,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        out = rows + out  # prepend older
        fetched += len(rows)
        cur_end = rows[0][0] - 1
        if len(rows) < chunk:
            break
    return out


@dataclass
class _Sample:
    slug: str
    strike: float
    direction: str
    prediction_t_utc: datetime
    btc_spot: float
    distance_pct: float
    p_predicted_event: float
    p_predicted_calibrated: float  # currently same as raw (we bypass calibration to refit)
    realized_event: int  # 0/1


def _yes_won_for_market(market: dict) -> Optional[bool]:
    outcomes = market.get("outcomes") or []
    prices = market.get("outcomePrices") or []
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    try:
        nums = [float(x) for x in prices]
    except (TypeError, ValueError):
        return None
    if not (abs(nums[0] + nums[1] - 1.0) < 1e-6 and {nums[0], nums[1]} == {0.0, 1.0}):
        return None
    yes_idx = next((i for i, o in enumerate(outcomes) if str(o).lower() == "yes"), None)
    if yes_idx is None:
        return None
    return nums[yes_idx] == 1.0


def _build_samples_for_slug(slug: str, all_klines: list) -> list[_Sample]:
    window = parse_slug_to_month_window(slug)
    if window is None:
        return []
    month_start, month_end = window

    try:
        event = get_event_token_id(slug)
    except Exception as exc:
        logger.warning("slug %s: gamma fetch failed: %s", slug, exc)
        return []
    markets = event.get("markets") or []
    if not markets:
        return []

    # Index klines by open_time_ms (start of UTC day)
    by_day: dict[int, list] = {}
    for k in all_klines:
        by_day[int(k[0])] = k

    samples: list[_Sample] = []
    month_start_ms = int(month_start.timestamp() * 1000)
    month_end_ms = int(month_end.timestamp() * 1000)

    for m in markets:
        strike, direction = _extract_strike_and_direction(m.get("question") or "")
        if strike is None or direction not in ("above", "below"):
            continue
        yes_won = _yes_won_for_market(m)
        if yes_won is None:
            continue
        realized = 1 if yes_won else 0  # yes wins iff barrier touched

        for off_days in _PREDICTION_OFFSETS_DAYS:
            pred_t = month_end - timedelta(days=off_days)
            if pred_t <= month_start:
                continue
            pred_t_ms = int(pred_t.timestamp() * 1000)
            # daily kline that contains pred_t (open_time = day start UTC)
            day_open_ms = (pred_t_ms // 86400000) * 86400000
            kline = by_day.get(day_open_ms)
            if kline is None:
                continue
            try:
                btc_spot = float(kline[4])  # close of that day
            except (TypeError, ValueError):
                continue
            if btc_spot <= 0:
                continue

            # past 60 daily klines BEFORE pred_t (use day_open_ms - 1ms as cutoff)
            past = [k for k in all_klines if int(k[0]) < day_open_ms]
            past_60 = past[-60:]
            rets = klines_to_log_returns(past_60, n=60)
            if len(rets) < 20:
                continue
            sigma, _ = compute_ewma_sigma(rets, lam=0.94)
            mu, _ = compute_drift_daily(rets, shrink_full_n=60)

            # path-to-date [month_start, day_open_ms-1ms]
            in_window = [k for k in all_klines
                         if month_start_ms <= int(k[0]) < day_open_ms]
            highs = [float(k[2]) for k in in_window if k[2]]
            lows = [float(k[3]) for k in in_window if k[3]]
            path_max = max(highs) if highs else btc_spot
            path_min = min(lows) if lows else btc_spot

            # Step 0
            yes_touched = (
                (direction == "above" and path_max >= strike)
                or (direction == "below" and path_min <= strike)
            )
            if yes_touched:
                p_pred = 1.0
            else:
                days_left = max(0.001, (month_end - pred_t).total_seconds() / 86400.0)
                p_pred = _barrier_touch_prob(
                    btc_spot, strike, direction, mu, sigma, days_left, sigma_is_iv=False,
                )

            distance_pct = abs(strike - btc_spot) / btc_spot * 100.0
            samples.append(_Sample(
                slug=slug, strike=strike, direction=direction,
                prediction_t_utc=pred_t, btc_spot=btc_spot,
                distance_pct=distance_pct,
                p_predicted_event=p_pred, p_predicted_calibrated=p_pred,
                realized_event=realized,
            ))

    return samples


def _bucketize(samples: list[_Sample]) -> list[tuple[float, float, int]]:
    """Return list of (distance_midpoint, bias_pp, n) per bucket."""
    out: list[tuple[float, float, int]] = []
    for lo, hi in _BUCKETS:
        bucket = [s for s in samples if lo <= s.distance_pct < hi]
        n = len(bucket)
        if n == 0:
            mid = (lo + hi) / 2.0
            out.append((mid, 0.0, 0))
            continue
        mean_pred = sum(s.p_predicted_event for s in bucket) / n
        mean_real = sum(s.realized_event for s in bucket) / n
        bias_pp = (mean_pred - mean_real) * 100.0
        mid = (lo + hi) / 2.0
        logger.info("bucket [%g, %g): n=%d  pred=%.3f  real=%.3f  bias_pp=%+.1f",
                    lo, hi, n, mean_pred, mean_real, bias_pp)
        out.append((mid, round(bias_pp, 1), n))
    return out


def main(start: tuple[int, int] = (2025, 11),
         end: Optional[tuple[int, int]] = None) -> None:
    now = datetime.now(timezone.utc)
    if end is None:
        # exclude current month (not all settled yet)
        prev_y, prev_m = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
        end = (prev_y, prev_m)
    logger.info("recalibration window: %s → %s", start, end)

    slugs = _iter_slugs(start[0], start[1], end[0], end[1])
    logger.info("slugs: %s", slugs)

    end_window = parse_slug_to_month_window(slugs[-1])
    if end_window is None:
        logger.error("could not parse end slug")
        sys.exit(2)
    fetch_end_ms = int(end_window[1].timestamp() * 1000) + 86400000
    # need ~60 days BEFORE start month + full span between start and end
    span_days = (end_window[1] - parse_slug_to_month_window(slugs[0])[0]).days + 75
    logger.info("fetching %d daily klines ending %s", span_days, end_window[1])
    all_klines = _fetch_klines_until(fetch_end_ms, span_days)
    logger.info("fetched %d klines [%s .. %s]", len(all_klines),
                datetime.fromtimestamp(all_klines[0][0]/1000, timezone.utc).date(),
                datetime.fromtimestamp(all_klines[-1][0]/1000, timezone.utc).date())

    all_samples: list[_Sample] = []
    for slug in slugs:
        s = _build_samples_for_slug(slug, all_klines)
        logger.info("slug %s → %d samples", slug, len(s))
        all_samples.extend(s)

    if not all_samples:
        logger.error("no samples — aborting")
        sys.exit(1)

    logger.info("total samples: %d", len(all_samples))
    pts = _bucketize(all_samples)

    # Suggested control points (anchor 0.0 + tail 80.0 → 0.0)
    suggested = [(0.0, 0.0, 0)] + pts + [(80.0, 0.0, 0)]
    print("\n=== Suggested _CALIBRATION_CONTROL_POINTS ===")
    print("_CALIBRATION_CONTROL_POINTS: list[tuple[float, float, int]] = [")
    for d, b, n in suggested:
        print(f"    ({d:>5.1f}, {b:>+6.1f}, {n:>3d}),")
    print("]")


if __name__ == "__main__":
    main()
