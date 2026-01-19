import requests
import sys
import os
import json
from datetime import datetime
from pathlib import Path
from order_func import get_open_orders
from gemini_researcher import analyze_market_with_grounding
from email_alert import EmailSender
from config import TO_EMAIL,WALLET_ADDRESS
from html_generator import generate_html_template
from defiLlama import StablecoinMonitor
from etf_data import ETFScraper

# 添加项目根目录到 sys.path，以便可以直接运行此文件
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

def get_positions():
    position_url = "https://data-api.polymarket.com/positions"
    params = {"user":WALLET_ADDRESS}
    response = requests.get(position_url,params=params)
    response.raise_for_status()
    result = response.json()
    result = [i for i in result if i["curPrice"] != 0]
    return result

def get_4h_klines_data():
    url= "https://data-api.binance.vision"
    klines_endpoint = "/api/v3/klines"
    params = {"symbol":"BTCUSDT","interval":"4h","limit":10}
    response = requests.get(url + klines_endpoint,params=params)
    response.raise_for_status()
    result = response.json()
    return result

def match_orders_with_positions(orders: list, positions: list) -> list:
    """
    将挂单和仓位匹配起来
    
    Args:
        orders: 挂单列表，每个订单包含 asset_id, market, outcome, side, price 等字段
        positions: 仓位列表，每个仓位包含 asset, conditionId, outcome, curPrice 等字段
    
    Returns:
        匹配后的列表，每个元素包含仓位信息和相关的挂单列表
    """
    # 创建索引以快速查找
    # 按 asset_id 索引仓位
    positions_by_asset = {pos["asset"]: pos for pos in positions}
    # 按 conditionId 和 outcome 索引仓位（用于备用匹配）
    positions_by_market = {}
    for pos in positions:
        key = (pos["conditionId"], pos["outcome"])
        if key not in positions_by_market:
            positions_by_market[key] = []
        positions_by_market[key].append(pos)
    
    # 为每个仓位匹配挂单
    matched_results = []
    processed_positions = set()
    
    for order in orders:
        order_asset_id = order.get("asset_id")
        order_market = order.get("market")
        order_outcome = order.get("outcome")
        
        matched_position = None
        
        # 方法1: 通过 asset_id 精确匹配（最准确）
        if order_asset_id and order_asset_id in positions_by_asset:
            matched_position = positions_by_asset[order_asset_id]
        
        # 方法2: 通过 market + outcome 匹配（同一市场的同一方向）
        elif order_market and order_outcome:
            market_key = (order_market, order_outcome)
            if market_key in positions_by_market:
                # 如果有多个仓位，选择第一个（或可以根据其他条件选择）
                matched_position = positions_by_market[market_key][0]
        
        # 如果找到匹配的仓位
        if matched_position:
            position_key = matched_position["asset"]
            
            # 检查这个仓位是否已经处理过
            if position_key not in processed_positions:
                # 创建新的匹配结果
                matched_result = {
                    "position": matched_position.copy(),
                    "orders": []
                }
                matched_results.append(matched_result)
                processed_positions.add(position_key)
            
            # 找到对应的匹配结果并添加订单
            for result in matched_results:
                if result["position"]["asset"] == matched_position["asset"]:
                    result["orders"].append(order)
                    break
    
    # 添加没有挂单的仓位
    for pos in positions:
        if pos["asset"] not in processed_positions:
            matched_results.append({
                "position": pos.copy(),
                "orders": []
            })
    
    return matched_results

def format_matched_data(matched_results: list) -> list:
    """
    格式化匹配后的数据，便于查看和分析
    
    Args:
        matched_results: match_orders_with_positions 返回的结果
    
    Returns:
        格式化后的列表
    """
    formatted = []
    for item in matched_results:
        position = item["position"]
        orders = item["orders"]
        
        formatted_item = {
            "合约内容": position.get("title", "未知"),
            "仓位信息": {
                "猜测结果": position.get("outcome", "未知"),
                "持仓量": position.get("size", 0),
                "平均成本": position.get("avgPrice", 0),
                "当前价格": position.get("curPrice", 0),
                "盈亏百分比": position.get("percentPnl", 0),
                "到期日": position.get("endDate", "未知")
            },
            "相关挂单": []
        }
        
        for order in orders:
            formatted_item["相关挂单"].append({
                "猜测结果": order.get("side", "未知"),
                "挂单方向": order.get("outcome", "未知"),
                "挂单价格": order.get("price", 0),
                "挂单数量": order.get("original_size", 0),
                "已成交数量": order.get("size_matched", 0)
            })
        
        formatted.append(formatted_item)
    
    return formatted

def get_btc_price():
    url= "https://data-api.binance.vision"
    endpoint = "/api/v3/avgPrice"
    params = {"symbol":"BTCUSDT"}
    response = requests.get(url + endpoint,params=params)
    response.raise_for_status()
    result = response.json().get("price", 0)
    float_result = float(result)
    return float_result

def get_binance_derivatives_data():
    base_url = "https://fapi.binance.com"
    
    # 1. 获取实时资金费率 (Premium Index)
    # 包含了当前资金费率和预测下期费率
    fr_endpoint = "/fapi/v1/premiumIndex"
    params = {'symbol': 'BTCUSDT'}
    fr_data = requests.get(base_url + fr_endpoint, params=params).json()
    
    # 2. 获取持仓量 (Open Interest)
    # 注意：Binance 返回的是“张数”或“币数”，通常我们需要转化为 USDT 价值
    oi_endpoint = "/fapi/v1/openInterest"
    oi_data = requests.get(base_url + oi_endpoint, params=params).json()
    
    # 3. 获取多空人数比 (Top Trader Long/Short Ratio)
    # 这是一个非常有用的反向指标，散户做多时，通常要跌
    ls_endpoint = "/futures/data/topLongShortPositionRatio"
    ls_params = {'symbol': 'BTCUSDT', 'period': '5m', 'limit': 1}
    ls_data = requests.get(base_url + ls_endpoint, params=ls_params).json()
    
    result = {
        "funding_rate": float(fr_data['lastFundingRate']),
        "open_interest_usdt": float(oi_data['openInterest']) * float(fr_data['markPrice']), # 估算美元价值
        "long_short_ratio": float(ls_data[0]['longShortRatio']),
        "next_funding_time": fr_data['nextFundingTime']
    }
    
    return result

def get_market_sentiment_and_funding():
    from datetime import datetime, timezone
    
    # 获取当前时间戳
    current_timestamp = datetime.now(timezone.utc)
    timestamp_readable = current_timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # 1. 获取恐惧贪婪指数
    fear_greed_url = "https://api.alternative.me/fng/"
    response = requests.get(fear_greed_url)
    response.raise_for_status()
    fng_data = response.json()["data"][0]
    fng_value = int(fng_data["value"])
    fng_status = fng_data["value_classification"]
    
    # 恐惧贪婪指数解释
    fng_interpretations = {
        "Extreme Fear": "市场极度恐慌，可能是买入机会",
        "Fear": "市场情绪偏弱，恐慌正在蔓延",
        "Neutral": "市场情绪中性，观望为主",
        "Greed": "市场情绪偏强，贪婪情绪上升",
        "Extreme Greed": "市场极度贪婪，注意回调风险"
    }
    fng_interpretation = fng_interpretations.get(fng_status, "市场情绪未知")
    
    # 2. 获取BTC当前价格
    current_price = get_btc_price()
    
    # 3. 获取上次结果用于计算变化
    last_result = None
    if os.path.exists("last_market_sentiment_and_funding.json"):
        with open("last_market_sentiment_and_funding.json", "r") as f:
            last_result = json.load(f)
    
    # 计算价格变化（如果有上次结果）
    price_change = None
    if last_result and "btc_current_price" in last_result:
        last_price = last_result["btc_current_price"]
        price_change = round(current_price - last_price, 2)
    
    # 4. 获取币安衍生品数据
    binance_data = get_binance_derivatives_data()
    funding_rate = binance_data["funding_rate"]
    open_interest_usdt = binance_data["open_interest_usdt"]
    long_short_ratio = binance_data["long_short_ratio"]
    
    # 格式化资金费率为百分比
    funding_rate_pct = f"{funding_rate * 100:.4f}%"
    if funding_rate > 0:
        funding_rate_pct = f"+{funding_rate_pct}"
    
    # 格式化持仓量
    if open_interest_usdt >= 1e9:
        oi_formatted = f"${open_interest_usdt / 1e9:.2f}B"
    elif open_interest_usdt >= 1e6:
        oi_formatted = f"${open_interest_usdt / 1e6:.2f}M"
    else:
        oi_formatted = f"${open_interest_usdt / 1e3:.2f}K"
    
    # 判断持仓量趋势
    oi_change_trend = "Unknown"
    if last_result and "open_interest_usdt" in last_result:
        last_oi = last_result["open_interest_usdt"]
        if open_interest_usdt > last_oi * 1.01:  # 增加超过1%
            oi_change_trend = "Increasing"
        elif open_interest_usdt < last_oi * 0.99:  # 减少超过1%
            oi_change_trend = "Decreasing"
        else:
            oi_change_trend = "Stable"
    
    # 5. 多空比解释逻辑
    ls_interpretation = ""
    if long_short_ratio > 2.0:
        ls_interpretation = f"警告：散户大比例做多 ({long_short_ratio:.2f})，通常是反向指标"
    elif long_short_ratio > 1.5:
        ls_interpretation = f"注意：多头占优 ({long_short_ratio:.2f})，市场情绪偏多"
    elif long_short_ratio < 0.67:  # 1/1.5
        ls_interpretation = f"注意：空头占优 ({long_short_ratio:.2f})，市场情绪偏空"
    elif long_short_ratio < 0.5:
        ls_interpretation = f"警告：散户大比例做空 ({long_short_ratio:.2f})，可能是反向指标"
    else:
        ls_interpretation = f"多空比相对均衡 ({long_short_ratio:.2f})"
    
    # 6. 获取ETF流入数据
    etf_inflow_num = None
    scraper = ETFScraper()
    try:
        etf_data = scraper.get_etf_inflow()
        if etf_data:
            etf_inflow_num = etf_data.get("net_inflow_num")
    except Exception as e:
        print(f"ETF抓取失败: {e}")
    
    # 格式化ETF流入
    etf_net_inflow = "N/A"
    if etf_inflow_num is not None:
        if abs(etf_inflow_num) >= 1e9:
            etf_net_inflow = f"${etf_inflow_num / 1e9:.2f}B"
        elif abs(etf_inflow_num) >= 1e6:
            etf_net_inflow = f"${etf_inflow_num / 1e6:.2f}M"
        elif abs(etf_inflow_num) >= 1e3:
            etf_net_inflow = f"${etf_inflow_num / 1e3:.2f}K"
        else:
            etf_net_inflow = f"${etf_inflow_num:.2f}"
    
    # 7. 获取稳定币流动性（可选，如果需要的话）
    monitor = StablecoinMonitor()
    stablecoin_liquidity = monitor.get_macro_liquidity()
    stablecoin_mcap = stablecoin_liquidity.get("total_mcap_usd", 0) if stablecoin_liquidity else 0
    
    # 格式化稳定币流动性
    stablecoin_macro_liquidity = "N/A"
    if stablecoin_mcap > 0:
        if stablecoin_mcap >= 1e12:  # 万亿级别
            stablecoin_macro_liquidity = f"${stablecoin_mcap / 1e12:.2f}T"
        elif stablecoin_mcap >= 1e9:  # 十亿级别
            stablecoin_macro_liquidity = f"${stablecoin_mcap / 1e9:.2f}B"
        elif stablecoin_mcap >= 1e6:  # 百万级别
            stablecoin_macro_liquidity = f"${stablecoin_mcap / 1e6:.2f}M"
        elif stablecoin_mcap >= 1e3:  # 千级别
            stablecoin_macro_liquidity = f"${stablecoin_mcap / 1e3:.2f}K"
        else:
            stablecoin_macro_liquidity = f"${stablecoin_mcap:.4f}"
    
    # 构建结构化结果
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
            "ls_interpretation": ls_interpretation
        },
        "liquidity_data": {
            "funding_rate_pct": funding_rate_pct,
            "open_interest": oi_formatted,
            "oi_change_trend": oi_change_trend,
            "etf_net_inflow": etf_net_inflow,
            "stablecoin_macro_liquidity": stablecoin_macro_liquidity
        }
    }
    
    # 保存当前结果供下次使用
    save_data = {
        "btc_current_price": current_price,
        "open_interest_usdt": open_interest_usdt,
        "etf_inflow_num": etf_inflow_num,
        "timestamp": current_timestamp.timestamp()
    }
    json.dump(save_data, open("last_market_sentiment_and_funding.json", "w"), indent=4)
    
    return result

if __name__ == "__main__":
    email_sender = EmailSender()
    time_now = datetime.now().strftime("%m-%d %H:%M")
    # 获取数据
    positions = get_positions()
    orders = get_open_orders()
    
    # 匹配挂单和仓位
    matched_results = match_orders_with_positions(orders, positions)
    
    # 格式化输出
    formatted = format_matched_data(matched_results)
    print(f"{time_now} Polymarket持仓情况格式化完成")
    #print(formatted)
    klines_data = get_4h_klines_data()
    print(f"{time_now} 比特币4h K线数据获取完成")
    
    market_sentiment_and_funding = get_market_sentiment_and_funding()
    print(f"{time_now} 市场情绪与资金面获取完成,开始进行AI分析")
    
    analyze_result = analyze_market_with_grounding(formatted,klines_data,market_sentiment_and_funding)
    warn_prices = analyze_result["预警信号"]
    for warn_price in warn_prices:
        warn_price["alert_status"] = False
    with open("price_warn_config.py","w") as f:
        f.write(f"WARN_PRICE = {warn_prices}")
    print(f"{time_now} AI分析完成,开始发送邮件")
    
    email_subject = f"{time_now} Polymarket持仓情况分析,当前BTC价格: {get_btc_price():,.2f}"
    email_content = generate_html_template(analyze_result)
    email_sender.send_html_email(TO_EMAIL, email_subject, email_content)
    print(f"{time_now} 邮件发送完成")