"""
Polymarket 持仓与订单 API
"""
import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, CreateOrderOptions
from py_clob_client.order_builder.constants import BUY, SELL

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
