import logging
import time
from typing import Any, Dict, List, Optional

from data.polymarket import get_order_book

logger = logging.getLogger(__name__)


def fetch_orderbook_levels(trader: Any, token_id: str, side: str) -> Dict[str, Any]:
    self = trader
    ws_snapshot = self._ws_book_cache.get(token_id)
    levels: List[Dict[str, float]] = []
    source = "http"

    if ws_snapshot is not None:
        now_ms = int(time.time() * 1000)
        snapshot_ts = int(ws_snapshot.get("received_ms") or now_ms)
        age_ms = now_ms - snapshot_ts
        if age_ms <= self.WS_BOOK_MAX_AGE_MS:
            if side == "buy":
                levels = list(ws_snapshot.get("asks") or [])
            elif side == "sell":
                levels = list(reversed(ws_snapshot.get("bids") or []))
            else:
                raise RuntimeError(f"未知 side: {side}")

            if levels:
                source = "ws"
                self._record_latency(f"orderbook_{side}_ws", float(age_ms))
                source_key = f"{side}_ws"
                self._book_source_counts[source_key] = (
                    self._book_source_counts.get(source_key, 0) + 1
                )
                logger.debug(
                    "订单簿来源: side=%s token=%s source=ws_book snapshot_age=%.2fms",
                    side,
                    token_id,
                    float(age_ms),
                )
        else:
            logger.debug(
                "订单簿WS快照过期，回退HTTP: side=%s token=%s snapshot_age=%.2fms threshold=%.2fms",
                side,
                token_id,
                float(age_ms),
                float(self.WS_BOOK_MAX_AGE_MS),
            )
    else:
        logger.debug(
            "订单簿无WS快照，回退HTTP: side=%s token=%s",
            side,
            token_id,
        )

    if source != "ws":
        book_t0 = time.perf_counter()
        book = get_order_book(token_id)
        book_ms = (time.perf_counter() - book_t0) * 1000
        self._record_latency(f"orderbook_{side}", book_ms)
        source_key = f"{side}_http"
        self._book_source_counts[source_key] = (
            self._book_source_counts.get(source_key, 0) + 1
        )
        if book is None:
            raise RuntimeError("订单簿为空")
        logger.debug(
            "订单簿获取耗时: side=%s token=%s latency=%.2fms source=http",
            side,
            token_id,
            book_ms,
        )

        if side == "buy":
            raw_levels = getattr(book, "asks", None) or []
            sorted_levels = sorted(
                raw_levels,
                key=lambda lvl: float(getattr(lvl, "price")),
            )
        elif side == "sell":
            raw_levels = getattr(book, "bids", None) or []
            sorted_levels = sorted(
                raw_levels,
                key=lambda lvl: float(getattr(lvl, "price")),
                reverse=True,
            )
        else:
            raise RuntimeError(f"未知 side: {side}")

        levels = []
        for lvl in sorted_levels:
            lvl_price = self._to_positive_float(getattr(lvl, "price", None))
            lvl_size = self._to_positive_float(getattr(lvl, "size", None))
            if lvl_price is None or lvl_size is None:
                continue
            levels.append({"price": lvl_price, "size": lvl_size})

    normalized_levels: List[Dict[str, float]] = []
    for lvl in levels:
        if not isinstance(lvl, dict):
            continue
        lvl_price = self._to_positive_float(lvl.get("price"))
        lvl_size = self._to_positive_float(lvl.get("size"))
        if lvl_price is None or lvl_size is None:
            continue
        normalized_levels.append({"price": lvl_price, "size": lvl_size})

    if side == "buy":
        normalized_levels = sorted(normalized_levels, key=lambda lvl: float(lvl["price"]))
    elif side == "sell":
        normalized_levels = sorted(normalized_levels, key=lambda lvl: float(lvl["price"]), reverse=True)
    else:
        raise RuntimeError(f"未知 side: {side}")

    if not normalized_levels:
        raise RuntimeError(f"订单簿无可用{'卖' if side == 'buy' else '买'}单")

    best_price_from_levels = self._to_positive_float(normalized_levels[0].get("price"))

    return {
        "source": source,
        "levels": normalized_levels,
        "best_ask": best_price_from_levels if side == "buy" else None,
        "best_bid": best_price_from_levels if side == "sell" else None,
    }


def build_execution_plan(
    trader: Any,
    token_id: str,
    side: str,
    target_size: float,
    levels_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    self = trader
    if target_size <= 0:
        raise RuntimeError("target_size 必须大于 0")

    payload = levels_payload or self._fetch_orderbook_levels(token_id=token_id, side=side)
    sorted_levels = payload.get("levels") or []
    book_source = str(payload.get("source") or "unknown")

    total_available = 0.0
    for lvl in sorted_levels:
        lvl_size = self._to_positive_float(lvl.get("size")) if isinstance(lvl, dict) else None
        if lvl_size is not None:
            total_available += lvl_size

    remaining = target_size
    consumed_levels: List[Dict[str, float]] = []
    executed_size = 0.0
    executed_notional = 0.0

    for lvl in sorted_levels:
        lvl_price = self._to_positive_float(lvl.get("price")) if isinstance(lvl, dict) else None
        lvl_size = self._to_positive_float(lvl.get("size")) if isinstance(lvl, dict) else None
        if lvl_price is None or lvl_size is None:
            continue
        if remaining <= 1e-9:
            break
        take_size = min(remaining, lvl_size)
        consumed_levels.append({
            "price": lvl_price,
            "size": take_size,
        })
        executed_size += take_size
        executed_notional += take_size * lvl_price
        remaining -= take_size

    if executed_size <= 0:
        raise RuntimeError("订单簿深度不足，无法成交")

    best_price = consumed_levels[0]["price"]
    worst_price = consumed_levels[-1]["price"]
    vwap_price = executed_notional / executed_size
    level_prices_preview = [
        float(lvl["price"])
        for lvl in sorted_levels[:10]
        if isinstance(lvl, dict) and lvl.get("price") is not None
    ]

    if side == "buy":
        slippage_abs = max(0.0, vwap_price - best_price)
    else:
        slippage_abs = max(0.0, best_price - vwap_price)

    slippage_bps = (slippage_abs / best_price * 10000.0) if best_price > 0 else 0.0
    fill_ratio = executed_size / target_size
    full_fill = fill_ratio >= 0.999999

    return {
        "side": side,
        "book_source": book_source,
        "target_size": target_size,
        "available_size": total_available,
        "executed_size": executed_size,
        "executed_notional": executed_notional,
        "fill_ratio": fill_ratio,
        "full_fill": full_fill,
        "best_price": best_price,
        "worst_price": worst_price,
        "vwap_price": vwap_price,
        "slippage_abs": slippage_abs,
        "slippage_bps": slippage_bps,
        "consumed_levels": consumed_levels,
        "level_prices_preview": level_prices_preview,
    }


def log_execution_plan(trader: Any, stage: str, market_slug: str, token_id: str, plan: Dict[str, Any]) -> None:
    _ = trader
    side = str(plan.get("side", ""))
    target_size = float(plan.get("target_size", 0.0))
    executed_size = float(plan.get("executed_size", 0.0))
    fill_ratio = float(plan.get("fill_ratio", 0.0))
    best_price = float(plan.get("best_price", 0.0))
    worst_price = float(plan.get("worst_price", 0.0))
    vwap_price = float(plan.get("vwap_price", 0.0))
    slippage_abs = float(plan.get("slippage_abs", 0.0))
    slippage_bps = float(plan.get("slippage_bps", 0.0))
    levels = plan.get("consumed_levels") or []
    book_source = str(plan.get("book_source", "unknown"))

    if fill_ratio >= 0.999999 and len(levels) == 1:
        logger.info(
            "%s 流动性评估: 市场=%s token=%s side=%s 完整在单档成交 price=%.4f size=%.4f",
            stage,
            market_slug,
            token_id,
            side,
            best_price,
            executed_size,
        )
        logger.info(
            "%s 订单簿路径: market=%s token=%s side=%s source=%s",
            stage,
            market_slug,
            token_id,
            side,
            book_source,
        )
        return

    if fill_ratio >= 0.999999:
        logger.info(
            "%s 流动性评估: 市场=%s token=%s side=%s 完整分阶成交 target=%.4f levels=%s best=%.4f worst=%.4f avg=%.4f slippage=%.4f(%.2fbps)",
            stage,
            market_slug,
            token_id,
            side,
            target_size,
            len(levels),
            best_price,
            worst_price,
            vwap_price,
            slippage_abs,
            slippage_bps,
        )
        logger.info(
            "%s 订单簿路径: market=%s token=%s side=%s source=%s",
            stage,
            market_slug,
            token_id,
            side,
            book_source,
        )
        return

    logger.warning(
        "%s 流动性评估: 市场=%s token=%s side=%s 未完整成交 target=%.4f 可成交=%.4f(%.2f%%) levels=%s best=%.4f worst=%.4f avg=%.4f slippage=%.4f(%.2fbps)",
        stage,
        market_slug,
        token_id,
        side,
        target_size,
        executed_size,
        fill_ratio * 100,
        len(levels),
        best_price,
        worst_price,
        vwap_price,
        slippage_abs,
        slippage_bps,
    )
    logger.info(
        "%s 订单簿路径: market=%s token=%s side=%s source=%s",
        stage,
        market_slug,
        token_id,
        side,
        book_source,
    )
