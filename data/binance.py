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


def get_btc_spot_depth_summary(
    band_pcts: list[float] | None = None,
    raw_limit: int = 1000,
) -> dict:
    """获取 BTC 现货 (BTCUSDT) L2 盘口, 并按相对现价的 ±band_pcts 聚合成
    买卖墙摘要。给 AI 用作判断关键阻力 / 支撑的依据。

    返回 schema:
      {
        "mid_price": float,
        "best_bid": float, "best_ask": float, "spread_bps": float,
        "bands": [
          {"band_pct": 0.005, "buy_wall_usd": 1234567.0, "sell_wall_usd": ...,
           "imbalance": 0.42},  # imbalance = (buy - sell) / (buy + sell)
          ...
        ],
        "top_buy_walls": [{"price": ..., "size_btc": ..., "size_usd": ...}, x5],
        "top_sell_walls": [...x5],
        "snapshot_time_ms": int,
      }
    """
    if band_pcts is None:
        band_pcts = [0.005, 0.01, 0.02, 0.03, 0.05]

    url = "https://data-api.binance.vision/api/v3/depth"
    resp = requests.get(url, params={"symbol": "BTCUSDT", "limit": raw_limit}, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    bids = [(float(p), float(s)) for p, s in data.get("bids", [])]
    asks = [(float(p), float(s)) for p, s in data.get("asks", [])]
    if not bids or not asks:
        return {"error": "empty_book"}

    best_bid = bids[0][0]
    best_ask = asks[0][0]
    mid = (best_bid + best_ask) / 2.0
    spread_bps = (best_ask - best_bid) / mid * 1e4 if mid > 0 else None

    bands = []
    for pct in band_pcts:
        lo = mid * (1 - pct)
        hi = mid * (1 + pct)
        buy_usd = sum(p * s for p, s in bids if p >= lo)
        sell_usd = sum(p * s for p, s in asks if p <= hi)
        denom = buy_usd + sell_usd
        bands.append({
            "band_pct": pct,
            "buy_wall_usd": round(buy_usd, 0),
            "sell_wall_usd": round(sell_usd, 0),
            "imbalance": round((buy_usd - sell_usd) / denom, 4) if denom > 0 else None,
        })

    def _top_walls(side):
        return [
            {"price": p, "size_btc": round(s, 4), "size_usd": round(p * s, 0)}
            for p, s in sorted(side, key=lambda x: x[0] * x[1], reverse=True)[:5]
        ]

    return {
        "mid_price": mid,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "spread_bps": round(spread_bps, 3) if spread_bps is not None else None,
        "bands": bands,
        "top_buy_walls": _top_walls(bids),
        "top_sell_walls": _top_walls(asks),
        "snapshot_time_ms": data.get("E") or data.get("lastUpdateId"),
    }


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
