"""实盘下单：仅调用项目内 data.polymarket，不修改其他包。"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

ENTRY_SLIPPAGE = 0.02
MIN_ORDER_SIZE = 5.0


def _level_price(lvl: Any) -> Any:
    """py-clob 可能返回 dict 档位或带 .price 的对象；dict 不能用 getattr。"""
    if isinstance(lvl, dict):
        return lvl.get("price")
    return getattr(lvl, "price", None)


def _best_ask_from_book(book: Any) -> Optional[float]:
    asks = getattr(book, "asks", None) or []
    if not asks:
        return None
    prices = []
    for lvl in asks:
        p = _level_price(lvl)
        if p is None:
            continue
        try:
            prices.append(float(p))
        except (TypeError, ValueError):
            continue
    return min(prices) if prices else None


def place_buy_hold_to_settlement(
    market_slug: str,
    direction: str,
    stake_usd: float,
    *,
    slippage: float = ENTRY_SLIPPAGE,
    dry_run: bool = True,
    tick_fallback_ask: Optional[float] = None,
) -> Optional[str]:
    """
    按方向买入 stake_usd 名义金额的 outcome token，持有至结算（不在此模块平仓）。

    tick_fallback_ask: 策略侧来自 SQLite 快照的 ask；当 CLOB 瞬时无卖单或解析失败时用作
    best_ask 回退（尾盘薄流动性常见）。dry-run 优先用其完成模拟；实盘也会回退并打 WARNING。

    Returns order_id or \"dry-run\" or None on failure.
    """
    if direction not in ("up", "down"):
        logger.error("invalid direction=%s", direction)
        return None

    from data.polymarket import (
        buy_order,
        get_event_token_id,
        get_market_metadata,
        get_order_book,
    )

    info = get_event_token_id(market_slug)
    markets = info.get("markets") or []
    if not markets:
        logger.error("no markets for slug=%s", market_slug)
        return None
    m = markets[0]
    outcomes = [str(o).lower() for o in (m.get("outcomes") or [])]
    token_ids = m.get("token_id") or []
    if len(outcomes) != len(token_ids) or len(token_ids) < 2:
        logger.error("bad market structure slug=%s", market_slug)
        return None
    up_idx = down_idx = None
    for i, o in enumerate(outcomes):
        if "up" in o:
            up_idx = i
        if "down" in o:
            down_idx = i
    if up_idx is None or down_idx is None:
        up_idx, down_idx = 0, 1

    token_id = str(token_ids[up_idx] if direction == "up" else token_ids[down_idx])
    market_id = m.get("market_id") or m.get("conditionId")
    if not market_id:
        logger.error("missing market_id slug=%s", market_slug)
        return None

    meta = get_market_metadata(str(market_id))
    book = get_order_book(token_id)
    best_ask: Optional[float] = None
    if book is not None:
        best_ask = _best_ask_from_book(book)

    def _fallback_ok(a: Optional[float]) -> bool:
        return a is not None and 0 < float(a) < 1.0

    if best_ask is None or best_ask <= 0:
        if _fallback_ok(tick_fallback_ask):
            fb = float(tick_fallback_ask)
            if dry_run:
                logger.info(
                    "CLOB no usable ask; dry-run using tick snapshot ask=%.4f token=%s",
                    fb,
                    token_id[:16],
                )
            else:
                logger.warning(
                    "CLOB no usable ask; live order using tick snapshot ask=%.4f (may be stale) token=%s",
                    fb,
                    token_id[:16],
                )
            best_ask = fb
        else:
            if book is None:
                logger.error("empty order book token=%s", token_id[:16])
            else:
                logger.error("no ask token=%s", token_id[:16])
            return None

    rough_size = stake_usd / best_ask
    if rough_size < MIN_ORDER_SIZE:
        logger.info(
            "tail_vol size raised to minimum: original=%.4f min=%.4f (stake=%.4f ask=%.4f)",
            rough_size,
            MIN_ORDER_SIZE,
            stake_usd,
            best_ask,
        )
        rough_size = MIN_ORDER_SIZE
    sweep_price = min(0.99, best_ask + slippage)

    logger.info(
        "tail_vol buy: slug=%s dir=%s token=%s best_ask=%.4f sweep=%.4f size~=%.4f dry_run=%s",
        market_slug,
        direction,
        token_id[:16],
        best_ask,
        sweep_price,
        rough_size,
        dry_run,
    )

    if dry_run:
        return "dry-run"

    oid = buy_order(
        str(market_id),
        token_id,
        sweep_price,
        rough_size,
        market_meta=meta,
    )
    if not oid:
        logger.error("buy_order returned empty")
        return None
    return str(oid)
