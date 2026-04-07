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


def get_binance_derivatives_data() -> dict:
    """获取币安衍生品数据：资金费率、持仓量、多空比"""
    base_url = "https://fapi.binance.com"
    params = {"symbol": "BTCUSDT"}

    fr_resp = requests.get(base_url + "/fapi/v1/premiumIndex", params=params)
    fr_resp.raise_for_status()
    fr_data = fr_resp.json()

    oi_resp = requests.get(base_url + "/fapi/v1/openInterest", params=params)
    oi_resp.raise_for_status()
    oi_data = oi_resp.json()

    ls_resp = requests.get(
        base_url + "/futures/data/topLongShortPositionRatio",
        params={"symbol": "BTCUSDT", "period": "5m", "limit": 1},
    )
    ls_resp.raise_for_status()
    ls_data = ls_resp.json()

    return {
        "funding_rate": float(fr_data["lastFundingRate"]),
        "open_interest_usdt": float(oi_data["openInterest"]) * float(fr_data["markPrice"]),
        "long_short_ratio": float(ls_data[0]["longShortRatio"]),
        "next_funding_time": fr_data["nextFundingTime"],
    }
