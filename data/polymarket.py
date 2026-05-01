"""
Polymarket 持仓与订单 API
"""
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 线程本地存储：用于 buy_order/sell_order 在返回 None 时把底层错误信息暴露给上层调用方
# (例如 dashboard 需要把 'service not ready' / 'not enough balance' 等真实原因展示给用户)
_last_order_error = threading.local()


def _set_last_order_error(msg: Optional[str]) -> None:
    _last_order_error.value = msg


def get_last_order_error() -> Optional[str]:
    return getattr(_last_order_error, "value", None)


def clear_last_order_error() -> None:
    _last_order_error.value = None


def _friendly_clob_error(exc: BaseException) -> str:
    """把 PolyApiException / 普通 Exception 转成更适合给前端展示的中文友好提示。
    保留底层原始信息以便排查。
    """
    raw = str(exc) if exc is not None else ""
    low = raw.lower()
    # 常见错误模式 -> 友好提示
    if "service not ready" in low or "status_code=425" in low:
        return f"Polymarket 服务暂时不可用 (HTTP 425),请稍后重试。原始: {raw}"
    if "not enough balance / allowance" in low:
        return f"余额或授权不足,无法成交。原始: {raw}"
    if "order_version_mismatch" in low:
        return f"订单版本不匹配,可能 SDK 与 CLOB 协议版本不一致。原始: {raw}"
    if "minimum_tick_size" in low or "tick size" in low:
        return f"价格未对齐到最小 tick size。原始: {raw}"
    if "status_code=429" in low or "rate limit" in low:
        return f"请求过于频繁 (HTTP 429),请稍后重试。原始: {raw}"
    if "status_code=502" in low or "status_code=503" in low or "status_code=504" in low:
        return f"Polymarket 网关临时错误,请稍后重试。原始: {raw}"
    return raw or exc.__class__.__name__


# 添加项目根目录到 sys.path，以便可以导入 config
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import requests
import httpx
import py_clob_client_v2.http_helpers.helpers as clob_http_helpers
from py_clob_client_v2.client import ClobClient
from py_clob_client_v2.clob_types import (
    OrderArgs,
    CreateOrderOptions,
    BalanceAllowanceParams,
    AssetType,
    MarketOrderArgs,
    PartialCreateOrderOptions,
    OrderType,
    PostOrdersArgs,
    BookParams,
)
from py_clob_client_v2.order_builder.builder import ROUNDING_CONFIG
from py_clob_client_v2.order_builder.helpers import round_down
from py_clob_client_v2.order_builder.constants import BUY, SELL
from datetime import datetime, timezone
from config import (
    POLYMARKET_KEY,
    WALLET_ADDRESS,
    PM_TRADE_KEY,
    PM_TRADE_WALLET_ADDRESS,
    PM_ANALYZE_KEY,
    PM_ANALYZE_WALLET_ADDRESS,
    POLYMARKET_PROFILE,
)


host = "https://clob.polymarket.com"
chain_id = 137


@dataclass(frozen=True)
class PolymarketContext:
    profile: str
    private_key: str
    wallet_address: str
    client: ClobClient


_PROFILE_ENV_KEY = "POLYMARKET_PROFILE"
_context_cache: Dict[str, PolymarketContext] = {}
_context_lock = threading.Lock()


def _resolve_profile(profile: Optional[str] = None) -> str:
    resolved = (profile or os.getenv(_PROFILE_ENV_KEY) or POLYMARKET_PROFILE or "trade").strip().lower()
    if resolved not in {"trade", "analyze"}:
        raise ValueError(f"Unsupported polymarket profile: {resolved}")
    return resolved


def _resolve_credentials(profile: str) -> tuple[str, str]:
    resolved = _resolve_profile(profile)
    if resolved == "trade":
        private_key = (PM_TRADE_KEY or "").strip()
        wallet = (PM_TRADE_WALLET_ADDRESS or "").strip()
    else:
        private_key = (PM_ANALYZE_KEY or "").strip()
        wallet = (PM_ANALYZE_WALLET_ADDRESS or "").strip()

    if not private_key:
        private_key = (POLYMARKET_KEY or "").strip()
    if not wallet:
        wallet = (WALLET_ADDRESS or "").strip()

    if not private_key:
        raise ValueError(f"Missing private key for polymarket profile={resolved}")
    if not wallet:
        raise ValueError(f"Missing wallet address for polymarket profile={resolved}")
    return private_key, wallet


def get_polymarket_context(profile: Optional[str] = None) -> PolymarketContext:
    resolved = _resolve_profile(profile)
    with _context_lock:
        cached = _context_cache.get(resolved)
        if cached is not None:
            return cached

        private_key, wallet = _resolve_credentials(resolved)
        temp_client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=2,
            funder=wallet,
        )
        creds = temp_client.create_or_derive_api_key()
        profile_client = ClobClient(
            host,
            key=private_key,
            chain_id=chain_id,
            signature_type=2,
            funder=wallet,
            creds=creds,
        )
        ctx = PolymarketContext(
            profile=resolved,
            private_key=private_key,
            wallet_address=wallet,
            client=profile_client,
        )
        _context_cache[resolved] = ctx
        return ctx


def get_client(profile: Optional[str] = None) -> ClobClient:
    return get_polymarket_context(profile).client


# Backward-compatible default client/wallet for legacy call sites
_default_ctx = get_polymarket_context()
client = _default_ctx.client
funder_address = _default_ctx.wallet_address
private_key = _default_ctx.private_key

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


def _get_clob_cache(clob_client: ClobClient, attr_name: str) -> Dict[str, Any]:
    cache = getattr(clob_client, attr_name, None)
    if isinstance(cache, dict):
        return cache
    cache = {}
    setattr(clob_client, attr_name, cache)
    return cache


def _cache_token_order_metadata(
    clob_client: ClobClient,
    token_id: str,
    minimum_tick_size: Optional[Any] = None,
    neg_risk: Optional[bool] = None,
    fee_rate_bps: Optional[int] = None,
) -> Dict[str, Any]:
    token = str(token_id or "")
    if not token:
        return {}

    record = _token_order_meta_cache.setdefault(token, {})
    tick_cache = _get_clob_cache(clob_client, "_ClobClient__tick_sizes")
    neg_risk_cache = _get_clob_cache(clob_client, "_ClobClient__neg_risk")
    fee_rate_cache = _get_clob_cache(clob_client, "_ClobClient__fee_rates")

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


def _get_cached_fee_rate_bps(clob_client: ClobClient, token_id: str) -> int:
    token = str(token_id or "")
    if not token:
        return 0
    cached = _token_order_meta_cache.get(token) or {}
    if "fee_rate_bps" in cached:
        try:
            return int(cached.get("fee_rate_bps") or 0)
        except Exception:
            return 0

    fee_rate_cache = _get_clob_cache(clob_client, "_ClobClient__fee_rates")
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
    profile: Optional[str] = None,
    max_attempts: int = 8,
    sleep_sec: float = 0.25,
) -> Optional[float]:
    if not order_id:
        return None

    clob_client = get_client(profile)
    for attempt in range(max(1, int(max_attempts))):
        try:
            detail = clob_client.get_order(order_id)
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


def get_conditional_token_balance(token_id: str, profile: Optional[str] = None) -> float:
    clob_client = get_client(profile)
    try:
        response = clob_client.get_balance_allowance(
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
    profile: Optional[str] = None,
    market_meta: Optional[Dict[str, Any]] = None,
    refresh_fee_rate: bool = False,
) -> Dict[str, Dict[str, Any]]:
    clob_client = get_client(profile)
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
            clob_client=clob_client,
            token_id=token,
            minimum_tick_size=minimum_tick_size,
            neg_risk=neg_risk,
        )

        fee_rate_bps: Optional[int] = None
        if refresh_fee_rate:
            try:
                fee_rate_bps = int(clob_client.get_fee_rate_bps(token) or 0)
            except Exception as e:
                logger.warning(
                    "prefetch fee_rate failed, fallback to cached/default: token_id=%s error=%s",
                    _token_id_short(token),
                    e,
                )
                fee_rate_bps = _get_cached_fee_rate_bps(clob_client, token)
        else:
            fee_rate_bps = _get_cached_fee_rate_bps(clob_client, token)
            # 缓存未命中返回 0 时不写入，避免污染 ClobClient 内部缓存
            if fee_rate_bps == 0:
                fee_rate_bps = None

        meta = _cache_token_order_metadata(
            clob_client=clob_client,
            token_id=token,
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


def get_positions(profile: Optional[str] = None) -> list:
    """获取 Polymarket 持仓"""
    wallet_address = get_polymarket_context(profile).wallet_address
    position_url = "https://data-api.polymarket.com/positions"
    params = {"user": wallet_address}
    response = requests.get(position_url, params=params)
    response.raise_for_status()
    result = response.json()
    return [i for i in result if i["curPrice"] != 0]


def get_open_orders(profile: Optional[str] = None) -> list:
    """获取未成交挂单"""
    clob_client = get_client(profile)
    logger.info("get_open_orders called")
    try:
        response = clob_client.get_open_orders()
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


def get_market_metadata(
    market_id: str,
    force_refresh: bool = False,
    profile: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    获取并缓存 market 下单所需元信息，减少下单时延。
    返回字段：minimum_tick_size, neg_risk
    """
    clob_client = get_client(profile)
    if not market_id:
        return None

    if not force_refresh:
        cached = _market_meta_cache.get(market_id)
        if cached is not None:
            logger.info("get_market_metadata cache_hit: market_id=%s", market_id)
            return cached

    t0 = time.perf_counter()
    try:
        market = clob_client.get_market(market_id)
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
    profile: Optional[str] = None,
    market_meta: Optional[Dict[str, Any]] = None,
):
    clear_last_order_error()
    clob_client = get_client(profile)
    logger.info("buy_order called: market_id=%s token_id=%s price=%s size=%s", market_id, _token_id_short(token_id), price, size)
    meta = market_meta or get_market_metadata(market_id, profile=profile)
    if not meta or meta.get("minimum_tick_size") is None:
        logger.error("buy_order missing market metadata: market_id=%s", market_id)
        _set_last_order_error("无法获取市场元数据 (minimum_tick_size)")
        return None
    normalized_size = normalize_order_size(size=size, tick_size=meta["minimum_tick_size"])
    if normalized_size <= 0:
        logger.error(
            "buy_order normalized_size is zero: market_id=%s token_id=%s original_size=%s",
            market_id,
            _token_id_short(token_id),
            size,
        )
        _set_last_order_error(f"size 归一化后 <= 0 (原始 size={size}, tick_size={meta['minimum_tick_size']})")
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
        profile=profile,
        market_meta=meta,
        refresh_fee_rate=False,
    )
    fee_rate_bps = _get_cached_fee_rate_bps(clob_client, token_id)
    # 缓存未命中时主动查询 fee_rate，避免传 0 被 API 拒绝
    if fee_rate_bps == 0:
        try:
            fee_rate_bps = int(clob_client.get_fee_rate_bps(token_id) or 0)
            if fee_rate_bps > 0:
                _cache_token_order_metadata(clob_client, token_id, fee_rate_bps=fee_rate_bps)
                logger.info("buy_order fee_rate_bps fetched on cache miss: token_id=%s fee_rate_bps=%d", _token_id_short(token_id), fee_rate_bps)
        except Exception as e:
            logger.warning("buy_order get_fee_rate_bps failed: token_id=%s error=%s", _token_id_short(token_id), e)
    submit_t0 = time.perf_counter()
    try:
        response = clob_client.create_and_post_order(
            OrderArgs(
                token_id=token_id,
                price=price,
                size=normalized_size,
                side=BUY,
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
        logger.exception("buy_order create_and_post_order failed: market_id=%s token_id=%s price=%s size=%s error=%s", market_id, _token_id_short(token_id), price, size, e)
        _set_last_order_error(_friendly_clob_error(e))
        return None


def sell_order(
    market_id: str,
    token_id: str,
    price: float,
    size: float = 5.0,
    profile: Optional[str] = None,
    market_meta: Optional[Dict[str, Any]] = None,
    order_type: OrderType = OrderType.FAK,  # <--- 核心新增：默认使用 FAK 拒绝挂单被套
):
    clear_last_order_error()
    clob_client = get_client(profile)
    logger.info("sell_order called: market_id=%s token_id=%s price=%s size=%s type=%s", market_id, _token_id_short(token_id), price, size, order_type)
    meta = market_meta or get_market_metadata(market_id, profile=profile)
    if not meta or meta.get("minimum_tick_size") is None:
        _set_last_order_error("无法获取市场元数据 (minimum_tick_size)")
        return None

    normalized_size = normalize_order_size(size=size, tick_size=meta["minimum_tick_size"])
    if normalized_size <= 0:
        _set_last_order_error(f"size 归一化后 <= 0 (原始 size={size}, tick_size={meta['minimum_tick_size']})")
        return None

    normalized_price = _clamp_order_price(price=float(price), tick_size=meta["minimum_tick_size"])
    
    prefetch_order_metadata_for_tokens(token_ids=[token_id], profile=profile, market_meta=meta, refresh_fee_rate=False)
    fee_rate_bps = _get_cached_fee_rate_bps(clob_client, token_id)

    def _submit_once(submit_size: float):
        # 核心修改：拆分 create 和 post，强行注入 order_type
        signed_order = clob_client.create_order(
            OrderArgs(
                token_id=token_id,
                price=normalized_price,
                size=submit_size,
                side=SELL,
            ),
            options=PartialCreateOrderOptions(
                tick_size=str(meta["minimum_tick_size"]),
                neg_risk=bool(meta.get("neg_risk", False)),
            ),
        )
        return clob_client.post_order(signed_order, order_type=order_type)

    def _is_retryable_server_error(err_msg: str) -> bool:
        text = str(err_msg or "").lower()
        return (
            "status_code=500" in text
            or "status_code=502" in text
            or "status_code=503" in text
            or "status_code=504" in text
            or "could not run the execution" in text
            or "internal server error" in text
            or "bad gateway" in text
            or "service unavailable" in text
            or "gateway timeout" in text
        )

    submit_t0 = time.perf_counter()
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = _submit_once(normalized_size)
            order_id = response.get("orderID") if isinstance(response, dict) else None
            submit_ms = (time.perf_counter() - submit_t0) * 1000
            logger.info("sell_order success: order_id=%s submit_latency=%.2fms", order_id, submit_ms)
            return order_id
        except Exception as e:
            err_msg = str(e).lower()
            is_balance_err = "not enough balance / allowance" in err_msg
            if _is_retryable_server_error(err_msg) and attempt < max_attempts:
                backoff_sec = 0.15 * attempt
                logger.warning(
                    "sell_order 交易所临时错误，准备重试: attempt=%d/%d wait=%.2fs error=%s",
                    attempt,
                    max_attempts,
                    backoff_sec,
                    e,
                )
                time.sleep(backoff_sec)
                continue
            if not is_balance_err:
                logger.exception("sell_order failed: %s", e)
                _set_last_order_error(_friendly_clob_error(e))
                return None

            # --- 救回那段死代码：自动降量重试机制 ---
            logger.warning("sell_order 触发余额不足，尝试去链上核实真实余额并重试...")
            available_balance = get_conditional_token_balance(token_id, profile=profile)
            retry_size = normalize_order_size(
                size=min(normalized_size, available_balance),
                tick_size=meta["minimum_tick_size"],
            )
            
            if retry_size <= 0 or retry_size + 1e-12 >= normalized_size:
                logger.warning("sell_order 真实余额验证失败: 目标=%.6f, 实际=%.6f", normalized_size, available_balance)
                _set_last_order_error(
                    f"余额不足且无法降量重试: 目标 size={normalized_size:.6f}, 链上可用={available_balance:.6f}"
                )
                return None

            try:
                logger.info("sell_order 使用真实余额重试: size=%.6f", retry_size)
                retry_resp = _submit_once(retry_size)
                return retry_resp.get("orderID") if isinstance(retry_resp, dict) else None
            except Exception as retry_err:
                logger.exception("sell_order 降量重试依然失败: %s", retry_err)
                _set_last_order_error(_friendly_clob_error(retry_err))
                return None
    return None

def cancel_order(order_id: str, profile: Optional[str] = None):
    clob_client = get_client(profile)
    from py_clob_client_v2.clob_types import OrderPayload
    return clob_client.cancel_order(OrderPayload(orderID=order_id))


def get_order_detail(order_id: str, profile: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not order_id:
        return None
    clob_client = get_client(profile)
    try:
        detail = clob_client.get_order(order_id)
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
    polymarket_event_situation["markets"] = [
        {
            "question": i.get("question"),
            "outcomes": i.get("outcomes"),
            "outcomePrices": i.get("outcomePrices"),
            # 透传状态字段供上层过滤已结算市场（如存在）
            "active": i.get("active"),
            "status": i.get("status"),
            "closed": i.get("closed"),
            "resolved": i.get("resolved"),
            "isResolved": i.get("isResolved"),
            "endDate": i.get("endDate"),
        }
        for i in result["markets"]
    ]
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

def get_order_book(token_id: str, profile: Optional[str] = None):
    clob_client = get_client(profile)
    return clob_client.get_order_book(token_id)


def get_best_prices(token_ids: List[str], profile: Optional[str] = None) -> Dict[str, Dict[str, Optional[float]]]:
    """批量获取一组 token 的 best bid / best ask。

    返回 {token_id: {"best_bid": float|None, "best_ask": float|None}}。
    Polymarket /prices 接口约定：side=BUY 返回 best bid（买方愿付最高价），
    side=SELL 返回 best ask（卖方愿收最低价）。失败的 token 用 None 占位，避免拖垮整体。
    """
    result: Dict[str, Dict[str, Optional[float]]] = {tid: {"best_bid": None, "best_ask": None} for tid in token_ids}
    if not token_ids:
        return result
    try:
        clob_client = get_client(profile)
        params = []
        for tid in token_ids:
            params.append({"token_id": str(tid), "side": "BUY"})
            params.append({"token_id": str(tid), "side": "SELL"})
        prices = clob_client.get_prices(params)  # {token_id: {"BUY": "0.12", "SELL": "0.13"}}
    except Exception as exc:
        logger.warning("get_best_prices batch failed: error=%s", exc)
        return result

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for tid in token_ids:
        entry = prices.get(str(tid)) or {}
        result[tid] = {
            "best_bid": _f(entry.get("BUY")),
            "best_ask": _f(entry.get("SELL")),
        }
    return result

def get_balance_allowance(profile: Optional[str] = None) -> str:
    """返回当前可用 USDC 余额，如 $123.45"""
    clob_client = get_client(profile)
    response = clob_client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    balance = int(response.get("balance", 0)) / 10**6
    return f"${balance:.2f}"

def get_activity_history(market_id: str, profile: Optional[str] = None) -> List[Dict[str, Any]]:
    wallet_address = get_polymarket_context(profile).wallet_address
    url = "https://data-api.polymarket.com/activity"
    params = {"market": str(market_id), "user": wallet_address}
    
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        result = response.json()
        return result if isinstance(result, list) else []
    except Exception as e:
        logger.warning("get_activity_history failed: market_id=%s error=%s", market_id, e)
        return []

def get_5m_updown_activity_history(
    since_ts: Optional[int] = None,
    until_ts: Optional[int] = None,
    profile: Optional[str] = None,
) -> List[Dict[str, Any]]:
    wallet_address = get_polymarket_context(profile).wallet_address
    url = "https://data-api.polymarket.com/activity"
    params = {"user": wallet_address, "limit": 1000, "start": since_ts, "end": until_ts}
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        result = response.json()
        if isinstance(result, list):
            filtered = []
            for item in result:
                slug = str(item.get("eventSlug") or "").lower()
                if "btc-updown-5m" in slug:
                    filtered.append(item)
            return filtered
        return []
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


def calculate_activity_pnl_from_trade_events(
    since_ts: int,
    until_ts: Optional[int] = None,
    profile: Optional[str] = None,
) -> Dict[str, Any]:
    """统计给定时间区间内 5m up/down activity 的 USDC 收益。

    规则：
    - 收入: type=TRADE 且 side=SELL，以及 type=REDEEM
    - 支出: type=TRADE 且 side=BUY
    - 仅统计 usdcSize

    说明：
    - 直接使用 get_5m_updown_activity_history 的返回数据，不再依赖 SQLite 中的 market_id。
    - db_path/sleep_sec 参数保留用于兼容旧调用。
    - 新增 slug 维度聚合：返回每个 slug 的净盈亏，以及盈利/亏损/持平次数统计。
    """
    since_ts = int(since_ts)
    until_ts_int = int(until_ts) if until_ts is not None else None
    batch = get_5m_updown_activity_history(
        since_ts=since_ts,
        until_ts=until_ts_int,
        profile=profile,
    )
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
    }

if __name__ == "__main__":
    result = calculate_activity_pnl_from_trade_events(since_ts=1773390285)
    print(result)
