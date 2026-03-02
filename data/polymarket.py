"""
Polymarket 持仓与订单 API
"""
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 添加项目根目录到 sys.path，以便可以导入 config
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import requests
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, CreateOrderOptions, BalanceAllowanceParams, AssetType
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

_market_meta_cache: Dict[str, Dict[str, Any]] = {}


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
    logger.info("get_open_orders called")
    try:
        response = client.get_orders()
        count = len(response) if isinstance(response, list) else "non-list"
        logger.info("get_open_orders success: count=%s", count)
        return response
    except Exception as e:
        logger.exception("get_open_orders failed: error=%s", e)
        raise


def _token_id_short(tid: str) -> str:
    """Shorten token_id for logging (first 8 + ... + last 4)."""
    if not tid or len(tid) <= 16:
        return tid or ""
    return f"{tid[:8]}...{tid[-4:]}"


def get_market_metadata(market_id: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
    """
    获取并缓存 market 下单所需元信息，减少下单时延。
    返回字段：minimum_tick_size, neg_risk
    """
    if not market_id:
        return None

    if not force_refresh:
        cached = _market_meta_cache.get(market_id)
        if cached is not None:
            logger.info("get_market_metadata cache_hit: market_id=%s", market_id)
            return cached

    t0 = time.perf_counter()
    try:
        market = client.get_market(market_id)
        meta = {
            "minimum_tick_size": market.get("minimum_tick_size"),
            "neg_risk": market.get("neg_risk", False),
        }
        _market_meta_cache[market_id] = meta
        latency_ms = (time.perf_counter() - t0) * 1000
        logger.info("get_market_metadata fetched: market_id=%s latency=%.2fms", market_id, latency_ms)
        return meta
    except Exception as e:
        logger.exception("get_market_metadata failed: market_id=%s error=%s", market_id, e)
        return None


def buy_order(
    market_id: str,
    token_id: str,
    price: float,
    size: float = 5.0,
    market_meta: Optional[Dict[str, Any]] = None,
):
    logger.info("buy_order called: market_id=%s token_id=%s price=%s size=%s", market_id, _token_id_short(token_id), price, size)
    meta = market_meta or get_market_metadata(market_id)
    if not meta or meta.get("minimum_tick_size") is None:
        logger.error("buy_order missing market metadata: market_id=%s", market_id)
        return None
    submit_t0 = time.perf_counter()
    try:
        response = client.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=BUY),
            options=CreateOrderOptions(
                tick_size=str(meta["minimum_tick_size"]),
                neg_risk=bool(meta.get("neg_risk", False)),
            ),
        )
        order_id = response.get("orderID") if isinstance(response, dict) else None
        submit_ms = (time.perf_counter() - submit_t0) * 1000
        logger.info("buy_order success: market_id=%s order_id=%s submit_latency=%.2fms", market_id, order_id, submit_ms)
        return order_id
    except Exception as e:
        logger.exception("buy_order create_and_post_order failed: market_id=%s token_id=%s price=%s size=%s error=%s", market_id, _token_id_short(token_id), price, size, e)
        return None


def sell_order(
    market_id: str,
    token_id: str,
    price: float,
    size: float = 5.0,
    market_meta: Optional[Dict[str, Any]] = None,
):
    logger.info("sell_order called: market_id=%s token_id=%s price=%s size=%s", market_id, _token_id_short(token_id), price, size)
    meta = market_meta or get_market_metadata(market_id)
    if not meta or meta.get("minimum_tick_size") is None:
        logger.error("sell_order missing market metadata: market_id=%s", market_id)
        return None
    submit_t0 = time.perf_counter()
    try:
        response = client.create_and_post_order(
            OrderArgs(token_id=token_id, price=price, size=size, side=SELL),
            options=CreateOrderOptions(
                tick_size=str(meta["minimum_tick_size"]),
                neg_risk=bool(meta.get("neg_risk", False)),
            ),
        )
        order_id = response.get("orderID") if isinstance(response, dict) else None
        submit_ms = (time.perf_counter() - submit_t0) * 1000
        logger.info("sell_order success: market_id=%s order_id=%s submit_latency=%.2fms", market_id, order_id, submit_ms)
        return order_id
    except Exception as e:
        logger.exception("sell_order create_and_post_order failed: market_id=%s token_id=%s price=%s size=%s error=%s", market_id, _token_id_short(token_id), price, size, e)
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

def get_event_token_id(market_slug:str=None):
    current_month_year = datetime.now().strftime("%B-%Y").lower()  # e.g. february-2026
    if not market_slug:
        market_slug = f"what-price-will-bitcoin-hit-in-{current_month_year}"
    url = f"https://gamma-api.polymarket.com/events/slug/{market_slug}"
    response = requests.get(url)
    response.raise_for_status()
    result = response.json()

    def parse_json_list(value):
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                return []
        return []

    polymarket_event_situation = {}
    polymarket_event_situation["event_name"] = result["title"]
    polymarket_event_situation["markets"] = [
        {
            "question": i["question"],
            "market_id": i["conditionId"],
            "outcomes": parse_json_list(i.get("outcomes")),
            "outcomePrices": parse_json_list(i.get("outcomePrices")),
            "token_id": parse_json_list(i.get("clobTokenIds")),
        }
        for i in result["markets"]
    ]
    return polymarket_event_situation

def get_order_book(token_id: str):
    return client.get_order_book(token_id)

def get_balance_allowance() -> str:
    """返回当前可用 USDC 余额，如 $123.45"""
    response = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    balance = int(response.get("balance", 0)) / 10**6
    return f"${balance:.2f}"

if __name__ == "__main__":
    print(get_event_token_id("btc-updown-5m-1772096400"))