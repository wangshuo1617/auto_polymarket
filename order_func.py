import sys
import os
from pathlib import Path

# 添加项目根目录到 sys.path，以便可以直接运行此文件
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, CreateOrderOptions
from py_clob_client.order_builder.constants import BUY, SELL
from config import POLYMARKET_KEY, WALLET_ADDRESS

host = "https://clob.polymarket.com"
chain_id = 137
private_key = POLYMARKET_KEY
funder_address = WALLET_ADDRESS

temp_client = ClobClient(
    host,
    key=private_key,
    chain_id=chain_id,
    signature_type=2,
    funder=funder_address
)

print("正在获取 API 凭证...")
creds = temp_client.create_or_derive_api_creds()
print("API 凭证获取成功！")

client = ClobClient(
    host,
    key=private_key,
    chain_id=chain_id,
    signature_type=2,
    funder=funder_address,
    creds=creds  
)

def buy_order(market_id: str, token_id: str, price: float, size: int=5):
    market = client.get_market(market_id)
    
    print("正在下单...")
    try:
        response = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=BUY,
            ),
            options=CreateOrderOptions(
                # 这里必须加 str()，修复 KeyError: 0.001 问题
                tick_size=str(market["minimum_tick_size"]),
                neg_risk=market["neg_risk"],
            )
        )
        print("下单成功！")
        return response["orderID"]
    except Exception as e:
        print(f"下单失败：{e}")
        return None     

def sell_order(market_id: str, token_id: str, price: float, size: int=5):
    market = client.get_market(market_id)
    
    print("正在下单...")
    try:
        response = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=size,
                side=SELL,
            ),
            options=CreateOrderOptions(
                tick_size=str(market["minimum_tick_size"]),
                neg_risk=market["neg_risk"],
            )
        )
        print("下单成功！")
        return response["orderID"]
    except Exception as e:
        print(f"下单失败：{e}")
        return None

def cancel_order(order_id: str):
    response = client.cancel(order_id)
    print("取消订单成功！")
    return response

def get_open_orders():
    response = client.get_orders()
    print("获取订单成功！")
    return response

def get_order_history():
    response = client.get_trades()
    print("获取订单历史成功！")
    return response

if __name__ == "__main__":
    print(get_open_orders()[0])
