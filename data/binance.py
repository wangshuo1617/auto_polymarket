"""
Binance 现货与衍生品数据
"""
from __future__ import annotations

from datetime import datetime, timezone

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


def get_1m_kline_close_at(ts_utc) -> float | None:
    """获取指定 UTC 时间所在 1m K 线的 BTC close，用于成交时点归因。"""
    if isinstance(ts_utc, datetime):
        ts_ms = int(ts_utc.astimezone(timezone.utc).timestamp() * 1000)
    else:
        ts_ms = int(ts_utc)
    minute_open_ms = ts_ms - (ts_ms % 60_000)
    url = "https://data-api.binance.vision"
    params = {
        "symbol": "BTCUSDT",
        "interval": "1m",
        "startTime": minute_open_ms,
        "endTime": minute_open_ms + 60_000,
        "limit": 1,
    }
    response = requests.get(url + "/api/v3/klines", params=params, timeout=10)
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return None
    return float(rows[0][4])


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


def _to_ms(value) -> int:
    if isinstance(value, datetime):
        return int(value.astimezone(timezone.utc).timestamp() * 1000)
    return int(value)


def _fetch_klines_range(
    base_url: str,
    endpoint: str,
    *,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    timeout: int = 15,
) -> list:
    out: list = []
    cursor = int(start_ms)
    interval_ms = {
        "1m": 60_000,
        "3m": 180_000,
        "5m": 300_000,
        "15m": 900_000,
        "30m": 1_800_000,
        "1h": 3_600_000,
    }.get(interval)
    if interval_ms is None:
        raise ValueError(f"unsupported interval: {interval}")

    while cursor < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1000,
        }
        response = requests.get(base_url + endpoint, params=params, timeout=timeout)
        response.raise_for_status()
        chunk = response.json()
        if not chunk:
            break
        out.extend(chunk)
        last_open = int(chunk[-1][0])
        next_cursor = last_open + interval_ms
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(chunk) < 1000:
            break
    return out


def _kline_pressure(klines: list) -> dict:
    if not klines:
        return {
            "open_price": 0.0,
            "close_price": 0.0,
            "ret_pct": 0.0,
            "quote_volume_usd_m": 0.0,
            "taker_buy_quote_usd_m": 0.0,
            "taker_sell_quote_usd_m": 0.0,
            "net_taker_quote_usd_m": 0.0,
            "taker_buy_ratio": 0.0,
            "net_taker_ratio": 0.0,
            "trade_count": 0,
            "kline_count": 0,
        }

    open_price = float(klines[0][1])
    close_price = float(klines[-1][4])
    quote_volume = 0.0
    taker_buy_quote = 0.0
    trade_count = 0
    for k in klines:
        quote_volume += float(k[7])
        trade_count += int(k[8])
        taker_buy_quote += float(k[10])
    taker_sell_quote = max(quote_volume - taker_buy_quote, 0.0)
    net_taker_quote = taker_buy_quote - taker_sell_quote
    taker_buy_ratio = taker_buy_quote / quote_volume if quote_volume > 0 else 0.0
    net_taker_ratio = net_taker_quote / quote_volume if quote_volume > 0 else 0.0
    ret_pct = ((close_price / open_price) - 1.0) * 100 if open_price > 0 else 0.0

    return {
        "open_price": round(open_price, 2),
        "close_price": round(close_price, 2),
        "ret_pct": round(ret_pct, 3),
        "quote_volume_usd_m": round(quote_volume / 1_000_000, 2),
        "taker_buy_quote_usd_m": round(taker_buy_quote / 1_000_000, 2),
        "taker_sell_quote_usd_m": round(taker_sell_quote / 1_000_000, 2),
        "net_taker_quote_usd_m": round(net_taker_quote / 1_000_000, 2),
        "taker_buy_ratio": round(taker_buy_ratio, 4),
        "net_taker_ratio": round(net_taker_ratio, 4),
        "trade_count": trade_count,
        "kline_count": len(klines),
    }


def _classify_spot_pressure(spot: dict) -> tuple[str, str]:
    ratio = float(spot.get("net_taker_ratio") or 0.0)
    net_usd_m = abs(float(spot.get("net_taker_quote_usd_m") or 0.0))
    ret_pct = float(spot.get("ret_pct") or 0.0)

    direction = "NEUTRAL"
    if ratio >= 0.08:
        direction = "BUY_PRESSURE"
    elif ratio <= -0.08:
        direction = "SELL_PRESSURE"

    confidence = "LOW"
    if direction != "NEUTRAL":
        if abs(ratio) >= 0.15 and net_usd_m >= 100:
            confidence = "HIGH"
        elif abs(ratio) >= 0.10 and net_usd_m >= 50:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"
        # 价格和主动成交同向时，置信度上调；背离时不判 high。
        if direction == "BUY_PRESSURE" and ret_pct > 0.15 and confidence == "MEDIUM":
            confidence = "HIGH"
        elif direction == "SELL_PRESSURE" and ret_pct < -0.15 and confidence == "MEDIUM":
            confidence = "HIGH"
        elif direction == "BUY_PRESSURE" and ret_pct < -0.20 and confidence == "HIGH":
            confidence = "MEDIUM"
        elif direction == "SELL_PRESSURE" and ret_pct > 0.20 and confidence == "HIGH":
            confidence = "MEDIUM"
    return direction, confidence


def _fetch_open_interest_change(start_ms: int, end_ms: int) -> dict:
    params = {
        "symbol": "BTCUSDT",
        "period": "5m",
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 500,
    }
    response = requests.get(
        "https://fapi.binance.com/futures/data/openInterestHist",
        params=params,
        timeout=15,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows:
        return {"oi_value_change_pct": None, "start_oi_value": None, "end_oi_value": None}
    start_val = float(rows[0].get("sumOpenInterestValue") or 0.0)
    end_val = float(rows[-1].get("sumOpenInterestValue") or 0.0)
    change_pct = ((end_val / start_val) - 1.0) * 100 if start_val > 0 else None
    return {
        "oi_value_change_pct": round(change_pct, 3) if change_pct is not None else None,
        "start_oi_value": round(start_val, 2),
        "end_oi_value": round(end_val, 2),
    }


def get_btc_session_market_pressure(start_utc, end_utc, interval: str = "5m") -> dict:
    """获取同一时间窗内 Binance BTC 现货/期货资金压力。

    主要用于验证 ETF 盘中异常是否传导到 BTC 市场:
    - spot taker net quote > 0: 现货主动买盘占优
    - spot taker net quote < 0: 现货主动卖盘占优
    - futures/OI 用于判断是否更像杠杆驱动而非现货驱动
    """
    start_ms = _to_ms(start_utc)
    end_ms = min(_to_ms(end_utc), int(datetime.now(timezone.utc).timestamp() * 1000))
    if end_ms <= start_ms:
        raise ValueError("end_utc must be later than start_utc")

    spot_klines = _fetch_klines_range(
        "https://data-api.binance.vision",
        "/api/v3/klines",
        symbol="BTCUSDT",
        interval=interval,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    spot = _kline_pressure(spot_klines)
    spot_direction, spot_confidence = _classify_spot_pressure(spot)

    futures = {"available": False}
    try:
        futures_klines = _fetch_klines_range(
            "https://fapi.binance.com",
            "/fapi/v1/klines",
            symbol="BTCUSDT",
            interval=interval,
            start_ms=start_ms,
            end_ms=end_ms,
        )
        futures = _kline_pressure(futures_klines)
        futures["available"] = True
        futures["open_interest"] = _fetch_open_interest_change(start_ms, end_ms)
    except Exception as exc:
        futures = {"available": False, "error": str(exc)[:200]}

    transmission = "NEUTRAL"
    if spot_direction != "NEUTRAL" and spot_confidence in {"MEDIUM", "HIGH"}:
        transmission = spot_direction

    leverage_note = "UNKNOWN"
    oi_change = None
    if futures.get("available"):
        oi_change = (futures.get("open_interest") or {}).get("oi_value_change_pct")
        fut_ratio = float(futures.get("net_taker_ratio") or 0.0)
        spot_ratio = float(spot.get("net_taker_ratio") or 0.0)
        if oi_change is not None and oi_change > 1.0 and abs(fut_ratio) > abs(spot_ratio) * 1.3:
            leverage_note = "FUTURES_LED"
        elif abs(spot_ratio) >= abs(fut_ratio) * 0.8:
            leverage_note = "SPOT_CONFIRMED"
        else:
            leverage_note = "MIXED"

    return {
        "window": {
            "start_utc": datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).isoformat(),
            "end_utc": datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).isoformat(),
            "interval": interval,
        },
        "spot": {
            **spot,
            "direction": spot_direction,
            "confidence": spot_confidence,
        },
        "futures": futures,
        "combined": {
            "direction": transmission,
            "confidence": spot_confidence if transmission != "NEUTRAL" else "LOW",
            "spot_confirmed": transmission != "NEUTRAL",
            "leverage_note": leverage_note,
            "oi_value_change_pct": oi_change,
        },
    }
