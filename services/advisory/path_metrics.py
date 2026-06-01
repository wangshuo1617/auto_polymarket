"""Path-to-date extrema + dynamic drift/EWMA σ helpers (Fair-Value P0+P1).

All functions are pure / side-effect free except `compute_path_extrema_from_binance`
which hits Binance kline REST API (with a small TTL cache). They are extracted so
that `scripts/advisory_recalibrate.py` can replay them on historical windows
without going through the live batch runner.
"""

from __future__ import annotations

import calendar
import logging
import math
import re
import time
import threading
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from data.binance import get_path_extrema

logger = logging.getLogger(__name__)
ET_TIMEZONE = ZoneInfo("America/New_York")


# --- slug → window helpers --------------------------------------------------

_MONTH_RE = re.compile(r"in-([a-z]+)-(\d{4})$")
_MONTH_MAP = {n.lower(): i for i, n in enumerate(calendar.month_name) if n}


def parse_slug_to_month_window(slug: str) -> Optional[tuple[datetime, datetime]]:
    """Return (start_utc, end_utc) for a `*-in-<month>-<year>` slug.

    Window is the **resolution window** for the monthly BTC barrier markets:
        start = first day of month 00:00:00 ET (inclusive), converted to UTC
        end   = last day of month 23:59:59 ET (inclusive), converted to UTC
    """
    m = _MONTH_RE.search(slug)
    if not m:
        return None
    month = _MONTH_MAP.get(m.group(1))
    if not month:
        return None
    year = int(m.group(2))
    last_day = calendar.monthrange(year, month)[1]
    start_et = datetime(year, month, 1, 0, 0, 0, tzinfo=ET_TIMEZONE)
    end_et = datetime(year, month, last_day, 23, 59, 59, tzinfo=ET_TIMEZONE)
    return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)


# --- path-to-date extrema ---------------------------------------------------

# If 1m data covers < this fraction of the elapsed window we fall back to
# daily klines (Binance API failure could leave gaps).
_MIN_1S_COVERAGE = 0.5

# TTL cache：advisory batch 每 5min 跑一次，60s 缓存可让同一批次内多次调用复用，
# 但不会让陈旧结果延续到下一批次。Key=(start_sec, end_sec_floor_60s)。
_CACHE_TTL_SEC = 60
_CACHE: dict[tuple[int, int], tuple[float, tuple[float, float, float, str]]] = {}
_CACHE_LOCK = threading.Lock()


def compute_path_extrema_from_binance(
    start_utc: datetime,
    end_utc: datetime,
    interval: str = "5m",
) -> tuple[float, float, float, str]:
    """Return (path_max, path_min, coverage_ratio, source).

    用 Binance kline 在 [start_utc, end_utc] (end 截到 now) 算 path 最高/最低。
    Polymarket 月度 BTC barrier 用 Binance 现货 OHLC 结算，与此处源一致。
    barrier touch 用 high/low 即可，1m 与 5m 结果一致（5m.high = max of 5×1m.high），
    默认 5m 在精度无损的前提下减少 5× 请求量与延迟。
    coverage = 实际拿到的 bar 数 / 期望 bar 数。
    空窗口/拉取异常 → (0, 0, 0, error_source)。
    """
    now = datetime.now(timezone.utc)
    effective_end = min(end_utc, now)
    if effective_end <= start_utc:
        return 0.0, 0.0, 0.0, "empty_window"

    start_sec = int(start_utc.timestamp())
    # 把 end 向下取整到 5min，对齐 advisory_batch 5min 节奏，确保同批次内 cache 命中
    end_sec = int(effective_end.timestamp()) // 300 * 300

    key = (start_sec, end_sec, interval)
    now_t = time.time()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and now_t - cached[0] <= _CACHE_TTL_SEC:
            return cached[1]

    try:
        pmax, pmin, coverage, n = get_path_extrema(
            start_utc, effective_end, interval=interval
        )
    except Exception:
        logger.exception("Binance get_path_extrema failed")
        return 0.0, 0.0, 0.0, "query_error"

    if pmax <= 0 or n == 0:
        result = (0.0, 0.0, 0.0, "no_data")
    else:
        source = (
            f"binance_{interval}_kline({start_utc.date()}..{effective_end.date()},"
            f"bars={n},cov={coverage:.2%})"
        )
        result = (pmax, pmin, coverage, source)

    with _CACHE_LOCK:
        _CACHE[key] = (now_t, result)
        if len(_CACHE) > 32:
            oldest = min(_CACHE.items(), key=lambda kv: kv[1][0])[0]
            _CACHE.pop(oldest, None)
    return result


# 兼容别名（原 1s 实现已迁出到 Binance）
compute_path_extrema_from_1s = compute_path_extrema_from_binance


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
    pmax, pmin, cov, src = compute_path_extrema_from_binance(start_utc, end_utc)
    if pmax <= 0 or cov < _MIN_1S_COVERAGE:
        return fallback_high, fallback_low, f"kline_fallback({fallback_source};1m_cov={cov:.2%})"
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


def compute_realized_sigma(returns: list[float], n: Optional[int] = None) -> float:
    """Sample stdev of log returns (daily σ).

    Uses last `n` returns if specified. Returns 0 for <2 samples.
    """
    rets = returns[-n:] if (n is not None and len(returns) > n) else list(returns)
    if len(rets) < 2:
        return 0.0
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)


def compute_sigma_panel(returns: list[float],
                        windows: tuple[int, ...] = (7, 14, 30, 60),
                        ewma_lam: float = 0.94) -> dict:
    """Compute multi-window σ panel for diagnostics / regime detection.

    Returns dict with realized_{n}d for each window + ewma_lam{λ}.
    All values are daily σ (decimal, not percent). 0.0 when insufficient data.
    """
    panel: dict = {}
    for w in windows:
        panel[f"realized_{w}d"] = round(compute_realized_sigma(returns, n=w), 8)
    ewma_sigma, _ = compute_ewma_sigma(returns, lam=ewma_lam)
    panel[f"ewma_lam{ewma_lam}"] = round(ewma_sigma, 8)
    panel["n_returns"] = len(returns)
    return panel
