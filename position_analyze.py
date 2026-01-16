import requests
import sys
import os
from datetime import datetime
from pathlib import Path
from order_func import get_open_orders
from gemini_researcher import analyze_market_with_grounding
from email_alert import EmailSender
from config import TO_EMAIL,WALLET_ADDRESS
from html_generator import generate_html_template

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
    return f"{float_result:,.2f}"


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
    print(f"{time_now} Polymarket持仓情况格式化完成,开始分析")
    #print(formatted)
    klines_data = get_4h_klines_data()
    print(f"{time_now} 比特币4h K线数据获取完成,开始进行AI分析")
    analyze_result = analyze_market_with_grounding(formatted,klines_data)
    warn_prices = analyze_result["预警信号"]
    for warn_price in warn_prices:
        warn_price["alert_status"] = False
    with open("price_warn_config.py","w") as f:
        f.write(f"WARN_PRICE = {warn_prices}")
    print(f"{time_now} AI分析完成,开始发送邮件")
    
    email_subject = f"{time_now} Polymarket持仓情况分析,当前BTC价格: {get_btc_price()}"
    email_content = generate_html_template(analyze_result)
    email_sender.send_html_email(TO_EMAIL, email_subject, email_content)
    print(f"{time_now} 邮件发送完成")