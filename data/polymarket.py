"""
Polymarket 持仓与订单 API
"""
import sys
from pathlib import Path

# 添加项目根目录到 sys.path，以便可以导入 config
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, CreateOrderOptions
from py_clob_client.order_builder.constants import BUY, SELL
from datetime import datetime
from config import POLYMARKET_KEY, WALLET_ADDRESS

host = "https://clob.polymarket.com"
chain_id = 137
private_key = POLYMARKET_KEY
funder_address = WALLET_ADDRESS

_temp_client = ClobClient(
    host,
    key=private_key,
    chain_id=chain_id,
    signature_type=2,
    funder=funder_address,
)
creds = _temp_client.create_or_derive_api_creds()

client = ClobClient(
    host,
    key=private_key,
    chain_id=chain_id,
    signature_type=2,
    funder=funder_address,
    creds=creds,
)


def get_positions() -> list:
    """获取 Polymarket 持仓"""
    position_url = "https://data-api.polymarket.com/positions"
    params = {"user": WALLET_ADDRESS}
    response = requests.get(position_url, params=params)
    response.raise_for_status()
    result = response.json()
    return [i for i in result if i["curPrice"] != 0]


def get_open_orders() -> list:
    """获取未成交挂单"""
    response = client.get_orders()
    return response


def buy_order(market_id: str, token_id: str, price: float, size: int = 5):
    market = client.get_market(market_id)
    try:
        response = client.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=BUY),
            options=CreateOrderOptions(
                tick_size=str(market["minimum_tick_size"]),
                neg_risk=market["neg_risk"],
            ),
        )
        return response["orderID"]
    except Exception as e:
        print(f"下单失败：{e}")
        return None


def sell_order(market_id: str, token_id: str, price: float, size: int = 5):
    market = client.get_market(market_id)
    try:
        response = client.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=SELL),
            options=CreateOrderOptions(
                tick_size=str(market["minimum_tick_size"]),
                neg_risk=market["neg_risk"],
            ),
        )
        return response["orderID"]
    except Exception as e:
        print(f"下单失败：{e}")
        return None

def cancel_order(order_id: str):
    return client.cancel(order_id)

def get_event_situation(market_slug:str=None):
    current_month_year = datetime.now().strftime("%B-%Y").lower()  # e.g. february-2026
    if not market_slug:
        market_slug = f"what-price-will-bitcoin-hit-in-{current_month_year}"
    url = f"https://gamma-api.polymarket.com/events/slug/{market_slug}"
    response = requests.get(url)
    response.raise_for_status()
    result = response.json()
    polymarket_event_situation = {}
    polymarket_event_situation["event_name"] = result["title"]
    polymarket_event_situation["markets"] = [{"question": i["question"], "outcomes": i["outcomes"], "outcomePrices": i["outcomePrices"]} for i in result["markets"]]
    return polymarket_event_situation

def get_order_book(token_id: str):
    return client.get_order_book(token_id)

if __name__ == "__main__":
    print(get_event_situation())