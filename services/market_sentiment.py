"""
市场情绪与资金面数据聚合
"""
import os
import json
import requests
from datetime import datetime, timezone

from data.binance import get_btc_price, get_binance_derivatives_data
from data.etf import ETFScraper
from data.rsi import last_24h_rsi
from data.defillama import StablecoinMonitor

SENTIMENT_CACHE_FILE = "last_market_sentiment_and_funding.json"


def get_market_sentiment_and_funding() -> dict:
    """聚合恐惧贪婪指数、BTC价格、衍生品数据、RSI、ETF、稳定币流动性等"""
    current_timestamp = datetime.now(timezone.utc)
    timestamp_readable = current_timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")

    # 1. 恐惧贪婪指数
    fear_greed_url = "https://api.alternative.me/fng/"
    response = requests.get(fear_greed_url)
    response.raise_for_status()
    fng_data = response.json()["data"][0]
    fng_value = int(fng_data["value"])
    fng_status = fng_data["value_classification"]
    fng_interpretations = {
        "Extreme Fear": "市场极度恐慌，可能是买入机会",
        "Fear": "市场情绪偏弱，恐慌正在蔓延",
        "Neutral": "市场情绪中性，观望为主",
        "Greed": "市场情绪偏强，贪婪情绪上升",
        "Extreme Greed": "市场极度贪婪，注意回调风险"
    }
    fng_interpretation = fng_interpretations.get(fng_status, "市场情绪未知")

    # 2. BTC 价格
    current_price = get_btc_price()

    # 3. 上次结果用于计算变化
    last_result = None
    if os.path.exists(SENTIMENT_CACHE_FILE):
        with open(SENTIMENT_CACHE_FILE, "r") as f:
            last_result = json.load(f)
    price_change = None
    if last_result and "btc_current_price" in last_result:
        price_change = round(current_price - last_result["btc_current_price"], 2)

    # 4. 币安衍生品数据
    binance_data = get_binance_derivatives_data()
    funding_rate = binance_data["funding_rate"]
    open_interest_usdt = binance_data["open_interest_usdt"]
    long_short_ratio = binance_data["long_short_ratio"]
    funding_rate_pct = f"{funding_rate * 100:.4f}%"
    if funding_rate > 0:
        funding_rate_pct = f"+{funding_rate_pct}"

    if open_interest_usdt >= 1e9:
        oi_formatted = f"${open_interest_usdt / 1e9:.2f}B"
    elif open_interest_usdt >= 1e6:
        oi_formatted = f"${open_interest_usdt / 1e6:.2f}M"
    else:
        oi_formatted = f"${open_interest_usdt / 1e3:.2f}K"

    oi_change_trend = "Unknown"
    if last_result and "open_interest_usdt" in last_result:
        last_oi = last_result["open_interest_usdt"]
        if open_interest_usdt > last_oi * 1.01:
            oi_change_trend = "Increasing"
        elif open_interest_usdt < last_oi * 0.99:
            oi_change_trend = "Decreasing"
        else:
            oi_change_trend = "Stable"

    # 5. 多空比解释
    if long_short_ratio > 2.0:
        ls_interpretation = f"警告：散户大比例做多 ({long_short_ratio:.2f})，通常是反向指标"
    elif long_short_ratio > 1.5:
        ls_interpretation = f"注意：多头占优 ({long_short_ratio:.2f})，市场情绪偏多"
    elif long_short_ratio < 0.67:
        ls_interpretation = f"注意：空头占优 ({long_short_ratio:.2f})，市场情绪偏空"
    elif long_short_ratio < 0.5:
        ls_interpretation = f"警告：散户大比例做空 ({long_short_ratio:.2f})，可能是反向指标"
    else:
        ls_interpretation = f"多空比相对均衡 ({long_short_ratio:.2f})"

    # 6. RSI 24h
    rsi_24h: list | None = None
    rsi_interpretation: str = "N/A"
    try:
        rsi_24h = last_24h_rsi()
        if rsi_24h is not None:
            rsi_24h = [round(x, 2) if x == x else None for x in rsi_24h]
            valid_rsi = [x for x in rsi_24h if x is not None]
            if valid_rsi:
                latest_rsi = valid_rsi[-1]
                if latest_rsi >= 70:
                    rsi_interpretation = f"RSI {latest_rsi:.1f} 超买，注意回调风险"
                elif latest_rsi >= 50:
                    rsi_interpretation = f"RSI {latest_rsi:.1f} 偏多"
                elif latest_rsi >= 30:
                    rsi_interpretation = f"RSI {latest_rsi:.1f} 偏空"
                else:
                    rsi_interpretation = f"RSI {latest_rsi:.1f} 超卖，可能反弹机会"
                if len(valid_rsi) >= 2:
                    prev_rsi = valid_rsi[-2]
                    if latest_rsi > prev_rsi + 2:
                        rsi_interpretation += "；24h内RSI上升"
                    elif latest_rsi < prev_rsi - 2:
                        rsi_interpretation += "；24h内RSI下降"
                    else:
                        rsi_interpretation += "；24h内RSI震荡"
    except Exception as e:
        print(f"RSI获取失败: {e}")

    # 7. ETF 流入
    etf_flow_2w: list[dict[str, str]] = []
    try:
        etf_data = ETFScraper().get_etf_inflow()
        if etf_data:
            etf_flow_2w = etf_data.get("etf_flow_2w") or []
            print(f"ETF最近两周流动数据条目: {len(etf_flow_2w)}")
    except Exception as e:
        print(f"ETF抓取失败: {e}")

    # 8. 稳定币流动性
    stablecoin_mcap = 0
    try:
        stablecoin_liquidity = StablecoinMonitor().get_macro_liquidity()
        stablecoin_mcap = stablecoin_liquidity.get("total_mcap_usd", 0) if stablecoin_liquidity else 0
    except Exception:
        pass

    stablecoin_macro_liquidity = "N/A"
    if stablecoin_mcap > 0:
        if stablecoin_mcap >= 1e12:
            stablecoin_macro_liquidity = f"${stablecoin_mcap / 1e12:.2f}T"
        elif stablecoin_mcap >= 1e9:
            stablecoin_macro_liquidity = f"${stablecoin_mcap / 1e9:.2f}B"
        elif stablecoin_mcap >= 1e6:
            stablecoin_macro_liquidity = f"${stablecoin_mcap / 1e6:.2f}M"
        elif stablecoin_mcap >= 1e3:
            stablecoin_macro_liquidity = f"${stablecoin_mcap / 1e3:.2f}K"
        else:
            stablecoin_macro_liquidity = f"${stablecoin_mcap:.4f}"

    result = {
        "market_context": {
            "timestamp_readable": timestamp_readable,
            "btc_price": round(current_price, 2),
            "price_change": price_change
        },
        "sentiment_data": {
            "fear_greed": {
                "value": fng_value,
                "status": fng_status,
                "interpretation": fng_interpretation
            },
            "long_short_ratio": round(long_short_ratio, 2),
            "ls_interpretation": ls_interpretation,
            "rsi_24h": rsi_24h,
            "rsi_interpretation": rsi_interpretation,
        },
        "liquidity_data": {
            "funding_rate_pct": funding_rate_pct,
            "open_interest": oi_formatted,
            "oi_change_trend": oi_change_trend,
            "etf_net_inflow_2w": etf_flow_2w,
            "stablecoin_macro_liquidity": stablecoin_macro_liquidity
        }
    }
    print(result)

    save_data = {
        "btc_current_price": current_price,
        "open_interest_usdt": open_interest_usdt,
        "etf_flow_2w": etf_flow_2w,
        "timestamp": current_timestamp.timestamp()
    }
    with open(SENTIMENT_CACHE_FILE, "w") as f:
        json.dump(save_data, f, indent=4)

    return result
