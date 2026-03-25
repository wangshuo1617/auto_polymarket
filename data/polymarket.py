"""
Polymarket 持仓与订单 API
"""
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 添加项目根目录到 sys.path，以便可以导入 config
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import requests
import httpx
import py_clob_client.http_helpers.helpers as clob_http_helpers
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    OrderArgs,
    CreateOrderOptions,
    BalanceAllowanceParams,
    AssetType,
    MarketOrderArgs,
    PartialCreateOrderOptions,
    OrderType,
    PostOrdersArgs,
)
from py_clob_client.order_builder.builder import ROUNDING_CONFIG
from py_clob_client.order_builder.helpers import round_down
from py_clob_client.order_builder.constants import BUY, SELL
from datetime import datetime, timezone
from config import POLYMARKET_KEY, WALLET_ADDRESS


host = "https://clob.polymarket.com"
chain_id = 137
private_key = POLYMARKET_KEY
funder_address = WALLET_ADDRESS

_temp_client = ClobClient(
    host,
    key=private_key,
    chain_id=chain_id,
    signature_type=1,
    funder=funder_address,
)
creds = _temp_client.create_or_derive_api_creds()

client = ClobClient(
    host,
    key=private_key,
    chain_id=chain_id,
    signature_type=1,
    funder=funder_address,
    creds=creds,
)

_market_meta_cache: Dict[str, Dict[str, Any]] = {}
_token_order_meta_cache: Dict[str, Dict[str, Any]] = {}
_http_keepalive_started = False
_http_keepalive_lock = threading.Lock()


def _configure_clob_http_client() -> None:
    limits = httpx.Limits(
        max_connections=100,
        max_keepalive_connections=32,
        keepalive_expiry=300.0,
    )
    timeout = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
    old_client = getattr(clob_http_helpers, "_http_client", None)
    clob_http_helpers._http_client = httpx.Client(
        http2=True,
        limits=limits,
        timeout=timeout,
    )
    if old_client is not None:
        try:
            old_client.close()
        except Exception:
            pass


def _get_clob_cache(attr_name: str) -> Dict[str, Any]:
    cache = getattr(client, attr_name, None)
    if isinstance(cache, dict):
        return cache
    cache = {}
    setattr(client, attr_name, cache)
    return cache


def _cache_token_order_metadata(
    token_id: str,
    minimum_tick_size: Optional[Any] = None,
    neg_risk: Optional[bool] = None,
    fee_rate_bps: Optional[int] = None,
) -> Dict[str, Any]:
    token = str(token_id or "")
    if not token:
        return {}

    record = _token_order_meta_cache.setdefault(token, {})
    tick_cache = _get_clob_cache("_ClobClient__tick_sizes")
    neg_risk_cache = _get_clob_cache("_ClobClient__neg_risk")
    fee_rate_cache = _get_clob_cache("_ClobClient__fee_rates")

    if minimum_tick_size is not None:
        tick_size_str = str(minimum_tick_size)
        record["minimum_tick_size"] = tick_size_str
        tick_cache[token] = tick_size_str

    if neg_risk is not None:
        neg_risk_bool = bool(neg_risk)
        record["neg_risk"] = neg_risk_bool
        neg_risk_cache[token] = neg_risk_bool

    if fee_rate_bps is not None:
        fee_int = int(fee_rate_bps)
        record["fee_rate_bps"] = fee_int
        fee_rate_cache[token] = fee_int

    return record


def _get_cached_fee_rate_bps(token_id: str) -> int:
    token = str(token_id or "")
    if not token:
        return 0
    cached = _token_order_meta_cache.get(token) or {}
    if "fee_rate_bps" in cached:
        try:
            return int(cached.get("fee_rate_bps") or 0)
        except Exception:
            return 0

    fee_rate_cache = _get_clob_cache("_ClobClient__fee_rates")
    if token in fee_rate_cache:
        try:
            return int(fee_rate_cache.get(token) or 0)
        except Exception:
            return 0
    return 0


def _safe_positive_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
        if parsed <= 0:
            return None
        return parsed
    except Exception:
        return None


def _extract_execution_price_from_order(order_payload: Dict[str, Any]) -> Optional[float]:
    if not isinstance(order_payload, dict):
        return None

    for key in ("avgPrice", "avg_price", "average_price"):
        price = _safe_positive_float(order_payload.get(key))
        if price is not None:
            return price

    # 优先用逐笔成交计算真实均价，避免把挂单 price 当作成交价。
    trades = order_payload.get("associate_trades")
    if not isinstance(trades, list):
        trades = order_payload.get("associated_trades")
    if isinstance(trades, list):
        total_size = 0.0
        total_notional = 0.0
        for trade in trades:
            if not isinstance(trade, dict):
                continue

            trade_price = None
            for price_key in ("price", "avg_price", "avgPrice", "trade_price"):
                trade_price = _safe_positive_float(trade.get(price_key))
                if trade_price is not None:
                    break

            trade_size = None
            for size_key in (
                "match_size",
                "matched_size",
                "size_matched",
                "size",
                "amount",
                "maker_amount",
                "makerAmount",
            ):
                trade_size = _safe_positive_float(trade.get(size_key))
                if trade_size is not None:
                    break

            if trade_price is None or trade_size is None:
                continue
            total_size += trade_size
            total_notional += trade_price * trade_size

        if total_size > 0:
            return total_notional / total_size

    taker = _safe_positive_float(
        order_payload.get("takerAmount")
        if order_payload.get("takerAmount") is not None
        else order_payload.get("taker_amount")
    )
    maker = _safe_positive_float(
        order_payload.get("makerAmount")
        if order_payload.get("makerAmount") is not None
        else order_payload.get("maker_amount")
    )
    if taker is not None and maker is not None and maker > 0:
        ratio_price = taker / maker
        if ratio_price > 0:
            return ratio_price
    return None


def _wait_order_execution_price(
    order_id: str,
    max_attempts: int = 8,
    sleep_sec: float = 0.25,
) -> Optional[float]:
    if not order_id:
        return None

    for attempt in range(max(1, int(max_attempts))):
        try:
            detail = client.get_order(order_id)
            price = _extract_execution_price_from_order(detail)
            if price is not None:
                return price
        except Exception as e:
            logger.debug(
                "wait_order_execution_price failed: order_id=%s attempt=%s error=%s",
                order_id,
                attempt + 1,
                e,
            )

        if attempt < max_attempts - 1:
            time.sleep(max(0.05, float(sleep_sec)))

    return None


def normalize_order_size(size: float, tick_size: Any) -> float:
    try:
        tick_key = str(tick_size)
        round_config = ROUNDING_CONFIG.get(tick_key)
        size_digits = int(round_config.size) if round_config is not None else 2
        normalized = float(round_down(float(size), size_digits))
        return max(0.0, normalized)
    except Exception:
        normalized = int(float(size) * 100) / 100
        return max(0.0, float(normalized))


def _clamp_order_price(price: float, tick_size: Any) -> float:
    tick = _safe_positive_float(tick_size)
    if tick is None:
        tick = 0.01

    min_price = tick
    max_price = max(tick, 1.0 - tick)
    clamped = min(max(float(price), min_price), max_price)

    tick_key = str(tick_size)
    round_config = ROUNDING_CONFIG.get(tick_key)
    if round_config is not None:
        try:
            price_digits = int(round_config.price)
            clamped = float(round_down(clamped, price_digits))
        except Exception:
            pass

    clamped = min(max(clamped, min_price), max_price)
    return float(clamped)


def get_conditional_token_balance(token_id: str) -> float:
    try:
        response = client.get_balance_allowance(
            BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=str(token_id),
            )
        )
        if not isinstance(response, dict):
            return 0.0
        raw_balance = int(response.get("balance", 0) or 0)
        return max(0.0, raw_balance / 10**6)
    except Exception as e:
        logger.warning(
            "get_conditional_token_balance failed: token_id=%s error=%s",
            _token_id_short(str(token_id)),
            e,
        )
        return 0.0


def prefetch_order_metadata_for_tokens(
    token_ids: List[str],
    market_meta: Optional[Dict[str, Any]] = None,
    refresh_fee_rate: bool = False,
) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    minimum_tick_size = None
    neg_risk = None
    if isinstance(market_meta, dict):
        minimum_tick_size = market_meta.get("minimum_tick_size")
        neg_risk = market_meta.get("neg_risk")

    for token_id in token_ids:
        token = str(token_id or "")
        if not token:
            continue

        _cache_token_order_metadata(
            token,
            minimum_tick_size=minimum_tick_size,
            neg_risk=neg_risk,
        )

        fee_rate_bps: Optional[int] = None
        if refresh_fee_rate:
            try:
                fee_rate_bps = int(client.get_fee_rate_bps(token) or 0)
            except Exception as e:
                logger.warning(
                    "prefetch fee_rate failed, fallback to cached/default: token_id=%s error=%s",
                    _token_id_short(token),
                    e,
                )
                fee_rate_bps = _get_cached_fee_rate_bps(token)
        else:
            fee_rate_bps = _get_cached_fee_rate_bps(token)

        # 缓存未命中时常为 0；CLOB 下单要求与 market 的 taker fee 一致，否则会 400：
        # invalid fee rate (0), current market's taker fee: 1000
        if fee_rate_bps == 0:
            try:
                fetched = int(client.get_fee_rate_bps(token) or 0)
                if fetched > 0:
                    fee_rate_bps = fetched
            except Exception as e:
                logger.warning(
                    "get_fee_rate_bps when cached fee was 0: token_id=%s error=%s",
                    _token_id_short(token),
                    e,
                )

        meta = _cache_token_order_metadata(
            token,
            minimum_tick_size=minimum_tick_size,
            neg_risk=neg_risk,
            fee_rate_bps=fee_rate_bps,
        )
        result[token] = dict(meta)

    return result


def _http_keepalive_loop(interval_sec: int) -> None:
    ping_count = 0
    while True:
        try:
            ping_t0 = time.perf_counter()
            client.get_server_time()
            ping_ms = (time.perf_counter() - ping_t0) * 1000
            ping_count += 1
            if ping_count % 30 == 1:
                logger.debug(
                    "clob_http_keepalive ping ok: latency=%.2fms sample=%s/30",
                    ping_ms,
                    ping_count % 30,
                )
        except Exception as e:
            logger.warning("clob_http_keepalive ping failed: %s", e)
        time.sleep(max(5, interval_sec))


def ensure_http_keepalive(interval_sec: int = 20) -> None:
    global _http_keepalive_started
    with _http_keepalive_lock:
        if _http_keepalive_started:
            return
        thread = threading.Thread(
            target=_http_keepalive_loop,
            args=(interval_sec,),
            daemon=True,
            name="polymarket-http-keepalive",
        )
        thread.start()
        _http_keepalive_started = True


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


def _safe_optional_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None


def _is_timeout_like_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    timeout_signals = (
        "readtimeout",
        "read timeout",
        "the read operation timed out",
        "request exception",
        "timed out",
    )
    return any(sig in msg for sig in timeout_signals)


def _extract_order_id_from_activity_item(item: Dict[str, Any]) -> Optional[str]:
    for k in ("orderID", "orderId", "order_id", "clobOrderId", "clob_order_id"):
        v = item.get(k)
        if v:
            return str(v)
    return None


def _extract_token_id_from_item(item: Dict[str, Any]) -> Optional[str]:
    for k in (
        "token_id",
        "tokenId",
        "asset_id",
        "assetId",
        "outcomeTokenId",
        "clobTokenId",
        "clob_token_id",
    ):
        v = item.get(k)
        if v:
            return str(v)
    return None


def _recover_timeout_buy_order(
    market_id: str,
    token_id: str,
    submitted_price: float,
    submitted_size: float,
    timeout_ts: int,
) -> Optional[str]:
    """
    当下单超时时，尝试回查是否已被服务端接收，避免“假失败后重复下单”。
    返回真实 order_id；若仅能确认有成交但无 order_id，返回 tx:<hash> 占位。
    """
    # 1) 先查 open orders（若订单仍挂着，通常可拿到 order_id）
    try:
        open_orders = get_open_orders()
        if isinstance(open_orders, list):
            for od in reversed(open_orders):
                if not isinstance(od, dict):
                    continue
                od_token = _extract_token_id_from_item(od)
                if od_token and od_token != str(token_id):
                    continue
                side = str(od.get("side") or "").upper()
                if side and side != "BUY":
                    continue
                p = _safe_optional_float(od.get("price"))
                s = _safe_optional_float(od.get("size"))
                if p is None or s is None:
                    continue
                price_ok = abs(p - float(submitted_price)) <= 0.03
                size_ok = abs(s - float(submitted_size)) <= max(0.2, float(submitted_size) * 0.12)
                if not (price_ok and size_ok):
                    continue
                recovered_oid = (
                    str(od.get("id") or "")
                    or str(od.get("orderID") or "")
                    or str(od.get("orderId") or "")
                    or str(od.get("order_id") or "")
                )
                if recovered_oid:
                    logger.warning(
                        "buy_order 超时后在 open_orders 找到匹配订单: market_id=%s token_id=%s order_id=%s",
                        market_id,
                        _token_id_short(token_id),
                        recovered_oid,
                    )
                    return recovered_oid
    except Exception as e:
        logger.warning("buy_order 超时回查 open_orders 失败: %s", e)

    # 2) 再查 activity（可能已快速成交，不在 open_orders）
    try:
        activity = get_activity_history(market_id)
        if isinstance(activity, list):
            now_ts = int(time.time())
            for item in sorted(
                [x for x in activity if isinstance(x, dict)],
                key=lambda x: int(x.get("timestamp") or 0),
                reverse=True,
            ):
                ts = int(item.get("timestamp") or 0)
                if ts and ts < timeout_ts - 10:
                    break
                if ts and ts > now_ts + 10:
                    continue
                event_type = str(item.get("type") or "").upper()
                side = str(item.get("side") or "").upper()
                if event_type != "TRADE" or side != "BUY":
                    continue
                item_token = _extract_token_id_from_item(item)
                if item_token and item_token != str(token_id):
                    continue
                p = _safe_optional_float(item.get("price"))
                s = _safe_optional_float(item.get("size"))
                if p is None or s is None:
                    continue
                price_ok = abs(p - float(submitted_price)) <= 0.03
                size_ok = abs(s - float(submitted_size)) <= max(0.2, float(submitted_size) * 0.2)
                if not (price_ok and size_ok):
                    continue

                oid = _extract_order_id_from_activity_item(item)
                if oid:
                    logger.warning(
                        "buy_order 超时后在 activity 找到匹配订单ID: market_id=%s token_id=%s order_id=%s",
                        market_id,
                        _token_id_short(token_id),
                        oid,
                    )
                    return oid

                tx_hash = str(item.get("transactionHash") or "").strip()
                if tx_hash:
                    pseudo = f"tx:{tx_hash}"
                    logger.warning(
                        "buy_order 超时后在 activity 确认成交(无order_id)，返回占位ID: %s",
                        pseudo,
                    )
                    return pseudo
    except Exception as e:
        logger.warning("buy_order 超时回查 activity 失败: %s", e)

    return None


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
    normalized_size = normalize_order_size(size=size, tick_size=meta["minimum_tick_size"])
    if normalized_size <= 0:
        logger.error(
            "buy_order normalized_size is zero: market_id=%s token_id=%s original_size=%s",
            market_id,
            _token_id_short(token_id),
            size,
        )
        return None
    if abs(normalized_size - float(size)) > 1e-12:
        logger.info(
            "buy_order size normalized by SDK rule: token_id=%s original=%.6f normalized=%.6f tick_size=%s",
            _token_id_short(token_id),
            float(size),
            normalized_size,
            meta["minimum_tick_size"],
        )
    prefetch_order_metadata_for_tokens(
        token_ids=[token_id],
        market_meta=meta,
        refresh_fee_rate=False,
    )
    fee_rate_bps = _get_cached_fee_rate_bps(token_id)
    submit_t0 = time.perf_counter()
    try:
        response = client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=normalized_size,
                side=BUY,
                fee_rate_bps=fee_rate_bps,
            ),
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
        logger.exception(
            "buy_order create_and_post_order failed: market_id=%s token_id=%s price=%s size=%s error=%s",
            market_id,
            _token_id_short(token_id),
            price,
            size,
            e,
        )
        if _is_timeout_like_error(e):
            recovered = _recover_timeout_buy_order(
                market_id=market_id,
                token_id=token_id,
                submitted_price=float(price),
                submitted_size=float(normalized_size),
                timeout_ts=int(time.time()),
            )
            if recovered:
                return recovered
        return None


def sell_order(
    market_id: str,
    token_id: str,
    price: float,
    size: float = 5.0,
    market_meta: Optional[Dict[str, Any]] = None,
    order_type: OrderType = OrderType.FAK,  # <--- 核心新增：默认使用 FAK 拒绝挂单被套
):
    logger.info("sell_order called: market_id=%s token_id=%s price=%s size=%s type=%s", market_id, _token_id_short(token_id), price, size, order_type)
    meta = market_meta or get_market_metadata(market_id)
    if not meta or meta.get("minimum_tick_size") is None:
        return None

    normalized_size = normalize_order_size(size=size, tick_size=meta["minimum_tick_size"])
    if normalized_size <= 0:
        return None

    normalized_price = _clamp_order_price(price=float(price), tick_size=meta["minimum_tick_size"])
    
    prefetch_order_metadata_for_tokens(token_ids=[token_id], market_meta=meta, refresh_fee_rate=False)
    fee_rate_bps = _get_cached_fee_rate_bps(token_id)

    def _submit_once(submit_size: float):
        # 核心修改：拆分 create 和 post，强行注入 order_type
        signed_order = client.create_order(
            OrderArgs(
                token_id=token_id,
                price=normalized_price,
                size=submit_size,
                side=SELL,
                fee_rate_bps=fee_rate_bps,
            ),
            options=PartialCreateOrderOptions(
                tick_size=str(meta["minimum_tick_size"]),
                neg_risk=bool(meta.get("neg_risk", False)),
            ),
        )
        return client.post_order(signed_order, orderType=order_type)

    submit_t0 = time.perf_counter()
    try:
        response = _submit_once(normalized_size)
        order_id = response.get("orderID") if isinstance(response, dict) else None
        submit_ms = (time.perf_counter() - submit_t0) * 1000
        logger.info("sell_order success: order_id=%s submit_latency=%.2fms", order_id, submit_ms)
        return order_id
    except Exception as e:
        err_msg = str(e).lower()
        if "not enough balance / allowance" not in err_msg:
            logger.exception("sell_order failed: %s", e)
            return None

        # --- 救回那段死代码：自动降量重试机制 ---
        logger.warning("sell_order 触发余额不足，尝试去链上核实真实余额并重试...")
        available_balance = get_conditional_token_balance(token_id)
        retry_size = normalize_order_size(
            size=min(normalized_size, available_balance),
            tick_size=meta["minimum_tick_size"],
        )
        
        if retry_size <= 0 or retry_size + 1e-12 >= normalized_size:
            logger.warning("sell_order 真实余额验证失败: 目标=%.6f, 实际=%.6f", normalized_size, available_balance)
            return None

        try:
            logger.info("sell_order 使用真实余额重试: size=%.6f", retry_size)
            retry_resp = _submit_once(retry_size)
            return retry_resp.get("orderID") if isinstance(retry_resp, dict) else None
        except Exception as retry_err:
            logger.exception("sell_order 降量重试依然失败: %s", retry_err)
            return None

def cancel_order(order_id: str):
    return client.cancel(order_id)


def get_order_detail(order_id: str) -> Optional[Dict[str, Any]]:
    if not order_id:
        return None
    if str(order_id).startswith("tx:"):
        # 占位ID：来自超时后 activity 回查，无法通过 clob get_order 查询。
        return {
            "id": order_id,
            "status": "MATCHED_BY_ACTIVITY",
            "matched_size": 0,
        }
    try:
        detail = client.get_order(order_id)
        if isinstance(detail, dict):
            return detail
        return None
    except Exception as e:
        logger.warning("get_order_detail failed: order_id=%s error=%s", order_id, e)
        return None

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

def get_activity_history(market_id: str) -> List[Dict[str, Any]]:
    url = "https://data-api.polymarket.com/activity"
    params = {"market": str(market_id), "user": WALLET_ADDRESS}
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        result = response.json()
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.warning("get_activity_history failed: market_id=%s error=%s", market_id, e)
        return []

def get_5m_updown_activity_history(since_ts: Optional[int] = None, until_ts: Optional[int] = None) -> List[Dict[str, Any]]:
    """拉取用户在时间窗内的 activity，并只保留 BTC 5m up/down 相关 slug。

    Data API 对单页条数有限制；单页拉满时继续用 offset 翻页，避免累计盈亏只统计前 N 条。
    """
    url = "https://data-api.polymarket.com/activity"
    page_limit = 500
    max_pages = 80  # 最多约 4 万条原始 activity，防失控
    filtered: List[Dict[str, Any]] = []
    offset = 0
    try:
        for _ in range(max_pages):
            params: Dict[str, Any] = {
                "user": WALLET_ADDRESS,
                "limit": page_limit,
                "offset": offset,
            }
            if since_ts is not None:
                params["start"] = int(since_ts)
            if until_ts is not None:
                params["end"] = int(until_ts)
            response = requests.get(url, params=params, timeout=60)
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, list) or not result:
                break
            for item in result:
                if not isinstance(item, dict):
                    continue
                slug = str(item.get("eventSlug") or "").lower()
                if "btc-updown-5m" in slug:
                    filtered.append(item)
            if len(result) < page_limit:
                break
            offset += page_limit
        return filtered
    except Exception as e:
        logger.warning("get_5m_updown_activity_history failed: error=%s", e)
    return []

def _event_time_to_epoch(event_time: str) -> int:
    raw = str(event_time or "").strip()
    if not raw:
        return 0
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return 0


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except Exception:
        return 0.0


def _round2(value: float) -> float:
    return round(float(value), 2)


def _activity_trade_size(item: Dict[str, Any]) -> float:
    """从 activity 条目推断成交份额（shares）。"""
    s = _safe_float(item.get("size"))
    if s > 0:
        return s
    p = _safe_float(item.get("price"))
    u = _safe_float(item.get("usdcSize"))
    if p > 0 and u > 0:
        return u / p
    return 0.0


def _activity_outcome_up_down(item: Dict[str, Any]) -> Optional[str]:
    o = str(item.get("outcome") or "").lower()
    if "up" in o:
        return "up"
    if "down" in o:
        return "down"
    idx = item.get("outcomeIndex")
    try:
        idx_int = int(idx)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        idx_int = None
    if idx_int == 0:
        return "up"
    if idx_int == 1:
        return "down"
    return None


def _build_slug_net_shares_from_trades(
    batch: List[Any],
    since_ts: int,
    until_ts_int: Optional[int],
    *,
    include_redeem: bool = True,
) -> Dict[str, Dict[str, float]]:
    """用 TRADE 推导每个 slug 的 Up/Down 净份额（仅本时间窗内流水）。

    include_redeem=False 时忽略 REDEEM，用于「按最终结算价」反推盈亏（不把赎回当作头寸变化）。
    """
    out: Dict[str, Dict[str, float]] = {}
    for item in batch:
        if not isinstance(item, dict):
            continue
        ts = int(item.get("timestamp") or 0)
        if ts < since_ts:
            continue
        if until_ts_int is not None and ts > until_ts_int:
            continue
        event_slug = str(item.get("eventSlug") or item.get("slug") or "unknown").strip().lower() or "unknown"
        if event_slug not in out:
            out[event_slug] = {"up": 0.0, "down": 0.0}
        bucket = out[event_slug]
        event_type = str(item.get("type") or "").upper()
        if event_type == "TRADE":
            side = str(item.get("side") or "").upper()
            if side not in {"BUY", "SELL"}:
                continue
            od = _activity_outcome_up_down(item)
            if od is None:
                continue
            sz = _activity_trade_size(item)
            if sz <= 0:
                continue
            sign = 1.0 if side == "BUY" else -1.0
            bucket[od] += sign * sz
        elif include_redeem and event_type == "REDEEM":
            od = _activity_outcome_up_down(item)
            sz = _activity_trade_size(item)
            if sz <= 0 or od is None:
                continue
            bucket[od] -= sz
    return out


def _infer_up_down_winner_from_market_first(market: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """根据 Gamma 盘口末价（接近 0/1）推断 Up/Down 谁赢；未分胜负则 unresolved。"""
    import json

    outcomes = market.get("outcomes") or []
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes) if outcomes else []
        except json.JSONDecodeError:
            outcomes = []
    if not isinstance(outcomes, list):
        outcomes = []
    prices_raw = market.get("outcomePrices") or []
    plist: List[float] = []
    if isinstance(prices_raw, str):
        try:
            parsed = json.loads(prices_raw)
            prices_raw = parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            prices_raw = []
    if isinstance(prices_raw, list):
        for p in prices_raw:
            try:
                plist.append(float(p))
            except (TypeError, ValueError):
                plist.append(0.0)
    if len(plist) < 2:
        return None, "bad_prices"
    n = min(len(outcomes), len(plist))
    if n < 2:
        plist = plist[:2]
        outcomes = outcomes[:2] if len(outcomes) >= 2 else outcomes
        n = min(len(outcomes), len(plist))
    if n < 2:
        return None, "bad_prices"
    plist = plist[:n]
    outcomes = outcomes[:n]
    pmax = max(plist)
    pmin = min(plist)
    # 避免盘中价(≈0.5)误判为已结算：要求胜侧足够接近 1 或价差足够大
    if pmax < 0.80 and (pmax - pmin) < 0.50:
        return None, "unresolved"
    win_i = max(range(len(plist)), key=lambda i: plist[i])
    o = str(outcomes[win_i]).lower()
    if "up" in o:
        return "up", "ok"
    if "down" in o:
        return "down", "ok"
    return None, "unknown_outcome"


def _payout_usdc_at_settlement(nu: float, nd: float, winner: str) -> float:
    """二元结算：胜方每股 1 USDC，败方 0。"""
    if winner == "up":
        return float(nu)
    if winner == "down":
        return float(nd)
    return 0.0


def _apply_slug_settlement_estimates(
    slug_rows: List[Dict[str, Any]],
    *,
    max_slugs: int = 40,
    sleep_sec: float = 0.06,
    trade_only_positions: Optional[Dict[str, Dict[str, float]]] = None,
    prefer_settlement_final: bool = False,
) -> None:
    """填充每盘的 settlement 反推与展示用盈亏。

    prefer_settlement_final=False（默认）:
      已赎回：展示用 Activity net_pnl；未赎回：Gamma 胜负 + 净头寸反推。

    prefer_settlement_final=True:
      一律用 Gamma 已分胜负时的「卖出收入 + 胜方净份额×1 − 买入成本」，
      净份额来自 trade_only_positions（仅 TRADE，不含 REDEEM），不依赖赎回流水。
    """
    from data.gamma_api import fetch_event_by_slug

    rows = sorted(
        slug_rows,
        key=lambda r: (abs(float(r.get("net_pnl", 0) or 0)), int(r.get("activity_count", 0) or 0)),
        reverse=True,
    )[: max(0, int(max_slugs))]

    for row in rows:
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        nu = float(row.get("net_shares_up") or 0.0)
        nd = float(row.get("net_shares_down") or 0.0)
        if trade_only_positions is not None:
            pos_to = trade_only_positions.get(slug) or {"up": 0.0, "down": 0.0}
            nu = float(pos_to.get("up", 0.0))
            nd = float(pos_to.get("down", 0.0))
        buy_cost = float(row.get("expense_trade_buy") or 0.0)
        sell_inc = float(row.get("income_trade_sell") or 0.0)
        inc_redeem = float(row.get("income_redeem") or 0.0)
        cnt_redeem = int(row.get("count_redeem") or 0)
        net_pnl = float(row.get("net_pnl") or 0.0)

        redeemed = cnt_redeem > 0 or inc_redeem > 1e-6
        row["redeemed"] = redeemed
        row["settlement_est_pnl_usdc"] = None
        row["resolution_winner"] = None
        row["settlement_note"] = ""
        row["gamma_settlement_prices"] = None

        if not prefer_settlement_final and redeemed:
            row["pnl_display_mode"] = "actual_redeem"
            row["display_round_pnl_usdc"] = _round2(net_pnl)
            time.sleep(sleep_sec)
            continue

        ev = fetch_event_by_slug(slug)
        if ev is None:
            row["settlement_note"] = "gamma_fetch_fail"
            if prefer_settlement_final:
                row["pnl_display_mode"] = "settlement_final_pending"
                row["display_round_pnl_usdc"] = None
            time.sleep(sleep_sec)
            continue

        mkts = list(ev.get("markets") or [])
        if not mkts:
            row["settlement_note"] = "no_markets"
            if prefer_settlement_final:
                row["pnl_display_mode"] = "settlement_final_pending"
                row["display_round_pnl_usdc"] = None
            time.sleep(sleep_sec)
            continue

        m0 = mkts[0]
        plist = m0.get("outcomePrices") or []
        row["gamma_settlement_prices"] = plist if isinstance(plist, list) else str(plist)[:120]

        winner, note = _infer_up_down_winner_from_market_first(m0)
        row["settlement_note"] = note
        if winner is None:
            if prefer_settlement_final:
                row["pnl_display_mode"] = "settlement_final_pending"
                row["display_round_pnl_usdc"] = None
            time.sleep(sleep_sec)
            continue

        row["resolution_winner"] = winner
        payout = _payout_usdc_at_settlement(nu, nd, winner)
        if prefer_settlement_final:
            est = float(payout) + sell_inc - buy_cost
            row["settlement_est_pnl_usdc"] = _round2(est)
            row["pnl_display_mode"] = "settlement_final"
            row["display_round_pnl_usdc"] = _round2(est)
        else:
            est = float(payout) - buy_cost
            row["settlement_est_pnl_usdc"] = _round2(est)
            row["pnl_display_mode"] = "settlement_reverse"
            row["display_round_pnl_usdc"] = _round2(est)
        time.sleep(sleep_sec)


def calculate_activity_pnl_from_trade_events(
    since_ts: int,
    until_ts: Optional[int] = None,
    *,
    include_settlement_estimate: bool = False,
    max_settlement_slugs: int = 40,
    include_gamma_mtm: Optional[bool] = None,
    max_gamma_mtm_slugs: Optional[int] = None,
    prefer_settlement_final: bool = False,
) -> Dict[str, Any]:
    """统计给定时间区间内 5m up/down activity 的 USDC 收益。

    规则：
    - 收入: type=TRADE 且 side=SELL，以及 type=REDEEM
    - 支出: type=TRADE 且 side=BUY
    - 仅统计 usdcSize

    说明：
    - slug 维度：每条含 net_shares_up/down（由 TRADE/REDEEM 推导）。
    - include_settlement_estimate=True（或兼容参数 include_gamma_mtm=True）时：
      默认：已赎回用 Activity net_pnl；未赎回用 Gamma 反推。
    - prefer_settlement_final=True（需同时 include_settlement_estimate）时：
      一律用 Gamma 已分胜负 + 仅 TRADE 净头寸，公式「胜方份额×1 + 卖出收入 − 买入成本」，
      不把 REDEEM 当作头寸或现金流依据（与链上 Activity 净值对照用）。
    """
    if include_gamma_mtm is not None:
        include_settlement_estimate = bool(include_gamma_mtm)
    if max_gamma_mtm_slugs is not None:
        max_settlement_slugs = int(max_gamma_mtm_slugs)
    if prefer_settlement_final:
        include_settlement_estimate = True
    since_ts = int(since_ts)
    until_ts_int = int(until_ts) if until_ts is not None else None
    batch = get_5m_updown_activity_history(since_ts=since_ts, until_ts=until_ts_int)
    if not isinstance(batch, list):
        batch = []

    income_trade_sell = 0.0
    income_redeem = 0.0
    expense_trade_buy = 0.0
    count_trade_sell = 0
    count_redeem = 0
    count_trade_buy = 0
    activity_count = 0
    slug_stats: Dict[str, Dict[str, Any]] = {}

    def _get_slug_stats(slug_value: str) -> Dict[str, Any]:
        if slug_value not in slug_stats:
            slug_stats[slug_value] = {
                "slug": slug_value,
                "activity_count": 0,
                "income_trade_sell": 0.0,
                "income_redeem": 0.0,
                "expense_trade_buy": 0.0,
                "count_trade_sell": 0,
                "count_redeem": 0,
                "count_trade_buy": 0,
            }
        return slug_stats[slug_value]

    for item in batch:
        if not isinstance(item, dict):
            continue

        ts = int(item.get("timestamp") or 0)
        if ts < since_ts:
            continue
        if until_ts_int is not None and ts > until_ts_int:
            continue

        usdc_size = _safe_float(item.get("usdcSize"))
        if usdc_size <= 0:
            continue

        event_type = str(item.get("type") or "").upper()
        side = str(item.get("side") or "").upper()
        event_slug = str(item.get("eventSlug") or item.get("slug") or "unknown").strip().lower() or "unknown"
        slug_bucket = _get_slug_stats(event_slug)

        if event_type == "TRADE" and side == "SELL":
            income_trade_sell += usdc_size
            count_trade_sell += 1
            activity_count += 1
            slug_bucket["income_trade_sell"] += usdc_size
            slug_bucket["count_trade_sell"] += 1
            slug_bucket["activity_count"] += 1
        elif event_type == "REDEEM":
            income_redeem += usdc_size
            count_redeem += 1
            activity_count += 1
            slug_bucket["income_redeem"] += usdc_size
            slug_bucket["count_redeem"] += 1
            slug_bucket["activity_count"] += 1
        elif event_type == "TRADE" and side == "BUY":
            expense_trade_buy += usdc_size
            count_trade_buy += 1
            activity_count += 1
            slug_bucket["expense_trade_buy"] += usdc_size
            slug_bucket["count_trade_buy"] += 1
            slug_bucket["activity_count"] += 1

    total_income = income_trade_sell + income_redeem
    net_pnl = total_income - expense_trade_buy

    slug_pnl_summary: List[Dict[str, Any]] = []
    slug_profit_count = 0
    slug_loss_count = 0
    slug_flat_count = 0

    for stats in slug_stats.values():
        slug_total_income = float(stats["income_trade_sell"]) + float(stats["income_redeem"])
        slug_net_pnl = slug_total_income - float(stats["expense_trade_buy"])

        if slug_net_pnl > 0:
            slug_profit_count += 1
        elif slug_net_pnl < 0:
            slug_loss_count += 1
        else:
            slug_flat_count += 1

        slug_pnl_summary.append(
            {
                "slug": stats["slug"],
                "activity_count": int(stats["activity_count"]),
                "income_trade_sell": _round2(stats["income_trade_sell"]),
                "income_redeem": _round2(stats["income_redeem"]),
                "expense_trade_buy": _round2(stats["expense_trade_buy"]),
                "total_income": _round2(slug_total_income),
                "net_pnl": _round2(slug_net_pnl),
                "count_trade_sell": int(stats["count_trade_sell"]),
                "count_redeem": int(stats["count_redeem"]),
                "count_trade_buy": int(stats["count_trade_buy"]),
            }
        )

    slug_pnl_summary.sort(key=lambda x: (x["net_pnl"], x["slug"]), reverse=True)

    slug_positions = _build_slug_net_shares_from_trades(batch, since_ts, until_ts_int, include_redeem=True)
    slug_positions_trade_only = _build_slug_net_shares_from_trades(
        batch, since_ts, until_ts_int, include_redeem=False
    )
    for row in slug_pnl_summary:
        skey = str(row.get("slug") or "").strip()
        pos = slug_positions.get(skey) or {"up": 0.0, "down": 0.0}
        row["net_shares_up"] = _round2(float(pos.get("up", 0.0)))
        row["net_shares_down"] = _round2(float(pos.get("down", 0.0)))
        pos_to = slug_positions_trade_only.get(skey) or {"up": 0.0, "down": 0.0}
        row["net_shares_up_trade_only"] = _round2(float(pos_to.get("up", 0.0)))
        row["net_shares_down_trade_only"] = _round2(float(pos_to.get("down", 0.0)))
        cnt_redeem = int(row.get("count_redeem") or 0)
        inc_redeem = float(row.get("income_redeem") or 0.0)
        redeemed = cnt_redeem > 0 or inc_redeem > 1e-6
        row["redeemed"] = redeemed
        row["settlement_est_pnl_usdc"] = None
        row["resolution_winner"] = None
        row["settlement_note"] = ""
        row["pnl_display_mode"] = "actual_redeem" if redeemed else "pending"
        row["display_round_pnl_usdc"] = _round2(float(row.get("net_pnl") or 0.0)) if redeemed else None
        row["gamma_settlement_prices"] = None

    slug_blend_pnl_total_usdc: Optional[float] = None
    if include_settlement_estimate and slug_pnl_summary:
        _apply_slug_settlement_estimates(
            slug_pnl_summary,
            max_slugs=max_settlement_slugs,
            trade_only_positions=slug_positions_trade_only if prefer_settlement_final else None,
            prefer_settlement_final=bool(prefer_settlement_final),
        )

    blend_parts: List[float] = []
    if prefer_settlement_final:
        for r in slug_pnl_summary:
            if r.get("pnl_display_mode") == "settlement_final" and r.get("display_round_pnl_usdc") is not None:
                blend_parts.append(float(r["display_round_pnl_usdc"]))
    else:
        for r in slug_pnl_summary:
            if r.get("redeemed"):
                blend_parts.append(float(r.get("net_pnl") or 0.0))
            elif r.get("settlement_est_pnl_usdc") is not None:
                blend_parts.append(float(r.get("settlement_est_pnl_usdc") or 0.0))
    if blend_parts:
        slug_blend_pnl_total_usdc = _round2(sum(blend_parts))
    elif prefer_settlement_final:
        # 本窗口无已结算盘时仍返回 0，避免邮件主题误用 Activity 主数字
        slug_blend_pnl_total_usdc = 0.0

    if prefer_settlement_final and slug_pnl_summary:
        pf = lo = z = 0
        for r in slug_pnl_summary:
            d = r.get("display_round_pnl_usdc")
            if d is None or r.get("pnl_display_mode") != "settlement_final":
                continue
            v = float(d)
            if v > 0:
                pf += 1
            elif v < 0:
                lo += 1
            else:
                z += 1
        slug_profit_count = pf
        slug_loss_count = lo
        slug_flat_count = z

    return {
        "since_ts": since_ts,
        "until_ts": until_ts_int,
        "activity_count": activity_count,
        "income_trade_sell": _round2(income_trade_sell),
        "income_redeem": _round2(income_redeem),
        "expense_trade_buy": _round2(expense_trade_buy),
        "total_income": _round2(total_income),
        "net_pnl": _round2(net_pnl),
        "count_trade_sell": count_trade_sell,
        "count_redeem": count_redeem,
        "count_trade_buy": count_trade_buy,
        "slug_summary": slug_pnl_summary,
        "slug_profit_count": slug_profit_count,
        "slug_loss_count": slug_loss_count,
        "slug_flat_count": slug_flat_count,
        "slug_total_count": len(slug_pnl_summary),
        "slug_blend_pnl_total_usdc": slug_blend_pnl_total_usdc,
        "slug_mtm_total_usdc": slug_blend_pnl_total_usdc,
        "include_settlement_estimate": bool(include_settlement_estimate),
        "include_gamma_mtm": bool(include_settlement_estimate),
        "prefer_settlement_final": bool(prefer_settlement_final),
    }

if __name__ == "__main__":
    result = calculate_activity_pnl_from_trade_events(since_ts=1773801000)
    print(result)