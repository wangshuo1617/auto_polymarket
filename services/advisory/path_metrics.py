"""Path-to-date extrema + dynamic drift/EWMA σ helpers (Fair-Value P0+P1).

All functions are pure / side-effect free except `compute_path_extrema_from_1s`
which queries Postgres `btc_poly_1s_ticks`. They are extracted so that
`scripts/advisory_recalibrate.py` can replay them on historical windows
without going through the live batch runner.
"""

from __future__ import annotations

import calendar
import logging
import math
import re
from datetime import datetime, timezone
from typing import Optional

from data.database import get_conn

logger = logging.getLogger(__name__)


# --- slug → window helpers --------------------------------------------------

_MONTH_RE = re.compile(r"in-([a-z]+)-(\d{4})$")
_MONTH_MAP = {n.lower(): i for i, n in enumerate(calendar.month_name) if n}


def parse_slug_to_month_window(slug: str) -> Optional[tuple[datetime, datetime]]:
    """Return (start_utc, end_utc) for a `*-in-<month>-<year>` slug.

    Window is the **resolution window** for the monthly BTC barrier markets:
        start = first day of month 00:00:00 UTC (inclusive)
        end   = last day of month 23:59:59 UTC (inclusive)
    """
    m = _MONTH_RE.search(slug)
    if not m:
        return None
    month = _MONTH_MAP.get(m.group(1))
    if not month:
        return None
    year = int(m.group(2))
    last_day = calendar.monthrange(year, month)[1]
    start = datetime(year, month, 1, 0, 0, 0, tzinfo=timezone.utc)
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return start, end


# --- path-to-date extrema ---------------------------------------------------

# If 1s data covers < this fraction of the elapsed window we fall back to
# daily klines (gaps from monitor restarts could understate path-min).
_MIN_1S_COVERAGE = 0.5


def compute_path_extrema_from_1s(
    start_utc: datetime,
    end_utc: datetime,
) -> tuple[float, float, float, str]:
    """Return (path_max, path_min, coverage_ratio, source).

    Queries `btc_poly_1s_ticks` for max/min btc_price between
    [start_utc, end_utc] (clamped to now if end is in the future).
    coverage_ratio = distinct_seconds / window_seconds.
    Returns (0, 0, 0, "no_data") on empty / error.
    """
    now = datetime.now(timezone.utc)
    effective_end = min(end_utc, now)
    if effective_end <= start_utc:
        return 0.0, 0.0, 0.0, "empty_window"

    start_sec = int(start_utc.timestamp())
    end_sec = int(effective_end.timestamp())
    window_sec = max(1, end_sec - start_sec)

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT MAX(btc_price), MIN(btc_price), COUNT(DISTINCT ts_sec)
                FROM btc_poly_1s_ticks
                WHERE ts_sec BETWEEN %s AND %s
                  AND btc_price IS NOT NULL
                """,
                (start_sec, end_sec),
            )
            row = cur.fetchone()
    except Exception:
        logger.exception("btc_poly_1s_ticks query failed")
        return 0.0, 0.0, 0.0, "query_error"

    if not row or row[0] is None:
        return 0.0, 0.0, 0.0, "no_data"

    pmax = float(row[0])
    pmin = float(row[1])
    coverage = float(row[2] or 0) / window_sec
    source = f"btc_poly_1s({start_utc.date()}..{effective_end.date()},cov={coverage:.2%})"
    return pmax, pmin, coverage, source


def compute_path_extrema_with_fallback(
    start_utc: datetime,
    end_utc: datetime,
    fallback_high: float,
    fallback_low: float,
    fallback_source: str,
) -> tuple[float, float, str]:
    """Prefer 1s data; fall back to caller-provided 7d kline extrema.

    Fallback triggers on: empty window, no rows, query error, or
    coverage < _MIN_1S_COVERAGE.
    """
    pmax, pmin, cov, src = compute_path_extrema_from_1s(start_utc, end_utc)
    if pmax <= 0 or cov < _MIN_1S_COVERAGE:
        return fallback_high, fallback_low, f"kline_fallback({fallback_source};1s_cov={cov:.2%})"
    return pmax, pmin, src


# --- log returns + dynamic drift / EWMA σ ----------------------------------

def klines_to_log_returns(klines: list, n: Optional[int] = None) -> list[float]:
    """Convert Binance 1d klines to daily log returns (close-to-close).

    klines[i][4] is the close price (string). Optionally slice last `n`.
    Returns at most len(klines)-1 returns.
    """
    closes: list[float] = []
    for k in klines or []:
        try:
            closes.append(float(k[4]))
        except (TypeError, ValueError, IndexError):
            continue
    if len(closes) < 2:
        return []
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] > 0]
    if n is not None and len(rets) > n:
        rets = rets[-n:]
    return rets


def compute_drift_daily(returns: list[float], shrink_full_n: int = 60) -> tuple[float, str]:
    """Sample mean log return with sample-size shrinkage to 0.

    weight = min(1, n / shrink_full_n); shrunk_drift = mean(returns) * weight.
    Returns (drift_daily, source_str).
    """
    n = len(returns)
    if n == 0:
        return 0.0, "drift=0(no_returns)"
    mean = sum(returns) / n
    weight = min(1.0, n / float(shrink_full_n))
    drift = mean * weight
    return drift, f"drift_mean_log_ret_n{n}_shrink{weight:.2f}={drift:.6f}"


def compute_ewma_sigma(returns: list[float], lam: float = 0.94) -> tuple[float, str]:
    """RiskMetrics EWMA daily σ.

    σ²_t = λ·σ²_{t-1} + (1−λ)·r²_t  (initialized with first squared return).
    Returns (sigma_daily, source_str). Falls back to 0 for empty input.
    """
    if not returns:
        return 0.0, "ewma_sigma=0(no_returns)"
    var = returns[0] ** 2
    for r in returns[1:]:
        var = lam * var + (1.0 - lam) * (r * r)
    sigma = math.sqrt(var)
    return sigma, f"ewma_lam{lam}_n{len(returns)}({sigma * 100:.2f}%)"
