"""
Binance 现货与衍生品数据
"""
from __future__ import annotations

import requests


def get_btc_price() -> float:
    """获取 BTC 当前价格"""
    url = "https://data-api.binance.vision"
    endpoint = "/api/v3/avgPrice"
    params = {"symbol": "BTCUSDT"}
    response = requests.get(url + endpoint, params=params)
    response.raise_for_status()
    return float(response.json().get("price", 0))


def get_1h_klines_data(limit: int = 1) -> list:
    """获取 BTC 1h K 线数据。每根 K 线: [open_time, open, high, low, close, ...]"""
    url = "https://data-api.binance.vision"
    params = {"symbol": "BTCUSDT", "interval": "1h", "limit": limit}
    response = requests.get(url + "/api/v3/klines", params=params)
    response.raise_for_status()
    return response.json()


def get_1h_klines_data_range(start_time_ms: int, end_time_ms: int) -> list:
    """
    获取 BTC 1h K 线区间数据（分页拉取）。

    返回格式与 Binance klines 一致：
    [open_time, open, high, low, close, ...]
    """
    base_url = "https://data-api.binance.vision"
    endpoint = "/api/v3/klines"
    all_klines: list = []
    cursor = int(start_time_ms)
    end_ms = int(end_time_ms)

    while cursor < end_ms:
        params = {
            "symbol": "BTCUSDT",
            "interval": "1h",
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        response = requests.get(base_url + endpoint, params=params)
        response.raise_for_status()
        chunk = response.json()
        if not chunk:
            break

        all_klines.extend(chunk)

        # EAFP: 直接推进游标，若结构异常则由异常显式暴露。
        last_open_time = int(chunk[-1][0])
        next_cursor = last_open_time + 3600 * 1000
        if next_cursor <= cursor:
            break
        cursor = next_cursor

    return all_klines


def get_4h_klines_data(limit: int = 10) -> list:
    """获取 BTC 4h K 线数据"""
    url = "https://data-api.binance.vision"
    klines_endpoint = "/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "4h", "limit": limit}
    response = requests.get(url + klines_endpoint, params=params)
    response.raise_for_status()
    return response.json()


def get_1d_klines_data(limit: int = 30) -> list:
    """获取 BTC 1d K 线数据，默认近30天。"""
    url = "https://data-api.binance.vision"
    klines_endpoint = "/api/v3/klines"
    params = {"symbol": "BTCUSDT", "interval": "1d", "limit": limit}
    response = requests.get(url + klines_endpoint, params=params)
    response.raise_for_status()
    return response.json()


def get_path_extrema(
    start_utc,
    end_utc,
    interval: str = "1m",
    symbol: str = "BTCUSDT",
) -> tuple[float, float, float, int]:
    """获取 [start_utc, end_utc] 期间的 BTC path high/low（Binance 现货 kline）。

    Polymarket 月度 BTC barrier 市场使用 Binance 现货 OHLC 结算,这里直接读源数据。

    返回: (path_max, path_min, coverage_ratio, kline_count)
        coverage_ratio = 实际拿到的 kline 根数 / 期望根数
        若 end_utc 早于 start_utc 或拿不到任何 kline 返回 (0, 0, 0, 0)
    """
    from datetime import datetime, timezone

    if isinstance(start_utc, datetime):
        start_ms = int(start_utc.timestamp() * 1000)
    else:
        start_ms = int(start_utc)
    if isinstance(end_utc, datetime):
        end_ms = int(end_utc.timestamp() * 1000)
    else:
        end_ms = int(end_utc)

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    end_ms = min(end_ms, now_ms)
    if end_ms <= start_ms:
        return 0.0, 0.0, 0.0, 0

    interval_ms = {
        "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
        "30m": 1_800_000, "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000,
        "1d": 86_400_000,
    }.get(interval)
    if interval_ms is None:
        raise ValueError(f"unsupported interval: {interval}")

    base_url = "https://data-api.binance.vision"
    endpoint = "/api/v3/klines"
    cursor = start_ms
    path_max = float("-inf")
    path_min = float("inf")
    count = 0

    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        resp = requests.get(base_url + endpoint, params=params, timeout=15)
        resp.raise_for_status()
        chunk = resp.json()
        if not chunk:
            break
        for k in chunk:
            try:
                high = float(k[2]); low = float(k[3])
            except (TypeError, ValueError, IndexError):
                continue
            if high > path_max:
                path_max = high
            if low < path_min and low > 0:
                path_min = low
            count += 1
        last_open = int(chunk[-1][0])
        next_cursor = last_open + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor

    if count == 0 or path_max == float("-inf"):
        return 0.0, 0.0, 0.0, 0

    expected = max(1, (end_ms - start_ms) // interval_ms)
    coverage = min(1.0, count / float(expected))
    return path_max, (path_min if path_min != float("inf") else 0.0), coverage, count


def get_binance_derivatives_data() -> dict:
    """获取币安衍生品数据：资金费率、持仓量、多空比"""
    base_url = "https://fapi.binance.com"
    params = {"symbol": "BTCUSDT"}

    fr_data = requests.get(base_url + "/fapi/v1/premiumIndex", params=params).json()
    oi_data = requests.get(base_url + "/fapi/v1/openInterest", params=params).json()
    ls_data = requests.get(
        base_url + "/futures/data/topLongShortPositionRatio",
        params={"symbol": "BTCUSDT", "period": "5m", "limit": 1},
    ).json()

    return {
        "funding_rate": float(fr_data["lastFundingRate"]),
        "open_interest_usdt": float(oi_data["openInterest"]) * float(fr_data["markPrice"]),
        "long_short_ratio": float(ls_data[0]["longShortRatio"]),
        "next_funding_time": fr_data["nextFundingTime"],
    }
