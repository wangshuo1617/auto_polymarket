import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict

from data.polymarket import (
    buy_order,
    get_event_token_id,
    get_market_metadata,
    normalize_order_size,
    prefetch_order_metadata_for_tokens,
)

from .models import OpenPosition
from .risk_sizing import RiskAssessment, assess_risk
from .watchers import PolymarketAssetPriceWatcher

logger = logging.getLogger(__name__)
TRADE_PROFILE = "trade"


def select_market_and_tokens(trader: Any, market_slug: str) -> Dict[str, Any]:
    self = trader
    cached = self._market_cache.get(market_slug)
    if cached is not None:
        return cached

    info_t0 = time.perf_counter()
    info = get_event_token_id(market_slug)
    info_ms = (time.perf_counter() - info_t0) * 1000
    self._record_latency("market_event_fetch", info_ms)
    markets = info.get("markets") or []
    if not markets:
        raise RuntimeError(f"未找到市场: {market_slug}")

    m = markets[0]
    outcomes = [str(o).lower() for o in (m.get("outcomes") or [])]
    token_ids = m.get("token_id") or []
    if len(outcomes) != len(token_ids) or len(token_ids) < 2:
        raise RuntimeError(f"市场结构异常: {market_slug}")

    up_index = None
    down_index = None
    for idx, o in enumerate(outcomes):
        if "up" in o:
            up_index = idx
        if "down" in o:
            down_index = idx

    if up_index is None or down_index is None:
        up_index, down_index = 0, 1

    result = {
        "market_id": m.get("market_id") or m.get("conditionId"),
        "up_token": token_ids[up_index],
        "down_token": token_ids[down_index],
        "market_meta": None,
    }
    market_id = result["market_id"]
    if market_id:
        meta_t0 = time.perf_counter()
        result["market_meta"] = get_market_metadata(market_id, profile=TRADE_PROFILE)
        meta_ms = (time.perf_counter() - meta_t0) * 1000
        self._record_latency("market_meta_fetch", meta_ms)

        prefetch_t0 = time.perf_counter()
        prefetch_order_metadata_for_tokens(
            token_ids=[str(result["up_token"]), str(result["down_token"])],
            profile=TRADE_PROFILE,
            market_meta=result["market_meta"],
            refresh_fee_rate=True,
        )
        prefetch_ms = (time.perf_counter() - prefetch_t0) * 1000
        self._record_latency("order_meta_prefetch", prefetch_ms)

        logger.info(
            "市场信息拉取耗时: slug=%s event=%.2fms market_meta=%.2fms order_meta_prefetch=%.2fms",
            market_slug,
            info_ms,
            meta_ms,
            prefetch_ms,
        )
    self._market_cache[market_slug] = result
    return result


def open_position(
    trader: Any,
    market_slug: str,
    direction: str,
    abs_btc_diff: float = 0.0,
    btc_cross_count: int = 0,
) -> None:
    self = trader

    if self.position is not None:
        if self.position.market_slug != market_slug:
            logger.warning(
                "检测到历史持仓，清空本地持仓后继续开仓: local_market=%s target_market=%s",
                self.position.market_slug,
                market_slug,
            )
            self.position = None
        elif self.position.balance_confirmed and self.position.size <= 0.02:
            logger.warning(
                "检测到零仓位残留，清理后继续开仓: %s",
                self.position,
            )
            self.position = None
        else:
            logger.warning("已有持仓，跳过开仓: %s", self.position)
            return

    if direction not in {"up", "down"}:
        raise RuntimeError(f"非法方向 direction={direction}")

    open_t0 = time.perf_counter()
    market_info = self._select_market_and_tokens(market_slug)
    market_id = market_info["market_id"]
    market_meta = market_info.get("market_meta")
    up_token = str(market_info["up_token"])
    down_token = str(market_info["down_token"])
    token_id = up_token if direction == "up" else down_token

    logger.info(
        "建仓token映射: market=%s direction=%s up_token=%s down_token=%s selected_token=%s",
        market_slug,
        direction,
        up_token,
        down_token,
        token_id,
    )

    # Polymarket 报价新鲜度检查（对齐回测 max_quote_age_ms / stale_entry_ask）
    ws_snapshot = self._ws_book_cache.get(token_id)
    if ws_snapshot is not None:
        now_ms = int(time.time() * 1000)
        snapshot_received_ms = int(ws_snapshot.get("received_ms") or 0)
        quote_age_ms = now_ms - snapshot_received_ms
        if quote_age_ms > self.WS_BOOK_MAX_AGE_MS:
            reason = "放弃开仓：Polymarket报价过期 (age=%dms > %dms) token=%s" % (
                quote_age_ms,
                self.WS_BOOK_MAX_AGE_MS,
                token_id,
            )
            logger.warning("%s", reason)
            self._record_skip_window(
                reason=reason,
                market_slug=market_slug,
                market_id=market_id,
                token_id=token_id,
                direction=direction,
            )
            return
    else:
        reason = "放弃开仓：无Polymarket WS报价数据 token=%s" % (token_id,)
        logger.warning("%s", reason)
        self._record_skip_window(
            reason=reason,
            market_slug=market_slug,
            market_id=market_id,
            token_id=token_id,
            direction=direction,
        )
        return

    entry_levels_payload = self._fetch_orderbook_levels(token_id=token_id, side="buy")
    entry_levels = entry_levels_payload.get("levels") or []
    if not entry_levels:
        raise RuntimeError("订单簿无卖单，流动性不足")
    best_ask_price = self._to_positive_float(entry_levels_payload.get("best_ask"))
    if best_ask_price is None:
        best_ask_price = float(entry_levels[0]["price"])
    rough_entry_price = best_ask_price

    # --- Risk-based position sizing ---
    risk_assessment = None
    effective_stake = self.stake_usd
    if getattr(self, "enable_risk_sizing", False):
        risk_assessment = assess_risk(
            entry_price=rough_entry_price,
            abs_btc_diff=abs_btc_diff,
            min_direction_diff=self.min_direction_diff,
            btc_cross_count=btc_cross_count,
            max_btc_cross_count=self.max_btc_cross_count,
            base_stake=self.stake_usd,
            min_stake_ratio=getattr(self, "risk_min_stake_ratio", 0.20),
            max_stake_ratio=getattr(self, "risk_max_stake_ratio", 1.50),
            confidence_boost_enabled=getattr(self, "confidence_boost_enabled", True),
            w_price=getattr(self, "risk_w_price", 0.50),
            w_direction=getattr(self, "risk_w_direction", 0.15),
            w_stability=getattr(self, "risk_w_stability", 0.35),
            stake_cap_very_high=getattr(self, "stake_cap_very_high", 0.0),
            stake_cap_high=getattr(self, "stake_cap_high", 0.50),
            stake_cap_medium_high=getattr(self, "stake_cap_medium_high", 0.35),
            medium_high_threshold=getattr(self, "medium_high_threshold", 0.40),
            confidence_boost_ge_095=getattr(self, "confidence_boost_ge_095", 1.5),
        )
        effective_stake = risk_assessment.adjusted_stake
        logger.info(
            "风险评估: entry_price=%.4f risk_score=%.3f risk_level=%s "
            "base_stake=%.2f adjusted_stake=%.2f "
            "(price_risk=%.2f dir_risk=%.2f stab_risk=%.2f)",
            rough_entry_price,
            risk_assessment.risk_score,
            risk_assessment.risk_level,
            self.stake_usd,
            effective_stake,
            risk_assessment.entry_price_risk,
            risk_assessment.direction_risk,
            risk_assessment.stability_risk,
        )
        if effective_stake <= 0:
            reason = "放弃开仓：风险等级=%s，仓位削减为0" % (risk_assessment.risk_level,)
            logger.info("%s", reason)
            self._record_skip_window(
                reason=reason,
                market_slug=market_slug,
                market_id=market_id,
                token_id=token_id,
                direction=direction,
            )
            return

    size = round(effective_stake / rough_entry_price, 6)
    normalized_size = normalize_order_size(
        size=size,
        tick_size=(market_meta or {}).get("minimum_tick_size", "0.01"),
    )
    if normalized_size <= 0:
        reason = "放弃开仓：归一化后下单数量为0，original=%.6f price=%.4f" % (
            size,
            rough_entry_price,
        )
        logger.warning("%s", reason)
        self._record_skip_window(
            reason=reason,
            market_slug=market_slug,
            market_id=market_id,
            token_id=token_id,
            direction=direction,
        )
        return
    if abs(normalized_size - size) > 1e-12:
        logger.info(
            "建仓size按SDK规则归一化: original=%.6f normalized=%.6f",
            size,
            normalized_size,
        )
    size = normalized_size

    plan = self._build_execution_plan(
        token_id=token_id,
        side="buy",
        target_size=size,
        levels_payload=entry_levels_payload,
    )
    self._log_execution_plan(stage="建仓", market_slug=market_slug, token_id=token_id, plan=plan)
    open_book_source = str(plan.get("book_source", "unknown"))
    logger.info(
        "建仓价格观测: market=%s token=%s source=%s best_from_levels=%.4f worst_fill=%.4f",
        market_slug,
        token_id,
        open_book_source,
        float(plan["best_price"]),
        float(plan["worst_price"]),
    )

    if plan["fill_ratio"] < self.MIN_ENTRY_LIQUIDITY_FILL_RATIO:
        reason = "放弃开仓：流动性不足，fill_ratio=%.2f%% 低于阈值 %.2f%%" % (
            plan["fill_ratio"] * 100,
            self.MIN_ENTRY_LIQUIDITY_FILL_RATIO * 100,
        )
        logger.warning("%s", reason)
        self._record_skip_window(
            reason=reason,
            market_slug=market_slug,
            market_id=market_id,
            token_id=token_id,
            direction=direction,
        )
        return

    if plan["slippage_bps"] > self.MAX_ENTRY_SLIPPAGE_BPS:
        reason = "放弃开仓：预估滑点过大 slippage=%.2fbps 超过阈值 %.2fbps" % (
            plan["slippage_bps"],
            self.MAX_ENTRY_SLIPPAGE_BPS,
        )
        logger.warning("%s", reason)
        self._record_skip_window(
            reason=reason,
            market_slug=market_slug,
            market_id=market_id,
            token_id=token_id,
            direction=direction,
        )
        return

    entry_price = float(plan["worst_price"])
    if best_ask_price > self.max_entry_price:
        reason = "放弃开仓：best_ask=%.4f 高于 MAX_ENTRY_PRICE=%.4f (worst_fill=%.4f)" % (
            best_ask_price,
            self.max_entry_price,
            entry_price,
        )
        logger.info("%s", reason)
        self._record_skip_window(
            reason=reason,
            market_slug=market_slug,
            market_id=market_id,
            token_id=token_id,
            direction=direction,
        )
        return

    logger.info(
        "建仓价格判定: best_ask=%.4f worst_fill=%.4f max_entry=%.4f",
        best_ask_price,
        entry_price,
        self.max_entry_price,
    )

    # 动态风控（由启动参数注入）:
    # 1) 止盈值 = min(tp_value_cap, tp_price_cap - entry_price)
    # 2) 止损值 = 止盈值 * sl_to_tp_ratio
    take_profit_value = min(self.tp_value_cap, max(0.0, self.tp_price_cap - entry_price))
    take_profit_price = min(self.tp_price_cap, entry_price + take_profit_value)
    stop_loss_value = take_profit_value * self.sl_to_tp_ratio
    stop_loss_price = max(0.001, entry_price - stop_loss_value)

    logger.info(
        "开仓: 市场=%s 方向=%s token=%s 价格=%.4f 数量=%.4f TP值=%.4f SL值=%.4f SL=%.4f TP=%.4f",
        market_slug,
        direction,
        token_id,
        entry_price,
        size,
        take_profit_value,
        stop_loss_value,
        stop_loss_price,
        take_profit_price,
    )

    if self.dry_run:
        logger.info("dry-run 模式：不实际下单，仅模拟持仓与盈亏")
        order_id = None
    else:
        # 应用建仓滑点：在 worst_price 基础上加价，确保尽快抢到仓位
        sweep_entry_price = min(0.99, entry_price + self.ENTRY_SWEEP_SLIPPAGE)
        logger.info("应用建仓滑点: 预估价=%.4f 实际建仓挂单价(sweep)=%.4f", entry_price, sweep_entry_price)
        submit_t0 = time.perf_counter()
        order_id = buy_order(
            market_id,
            token_id,
            sweep_entry_price,
            size,
            profile=TRADE_PROFILE,
            market_meta=market_meta,
        )
        submit_ms = (time.perf_counter() - submit_t0) * 1000
        self._record_latency("buy_submit", submit_ms)
        if not order_id:
            raise RuntimeError("Polymarket 买单下单失败，order_id 为空")
        logger.info("买单已提交，order_id=%s submit_latency=%.2fms", order_id, submit_ms)
    self.position = OpenPosition(
        market_slug=market_slug,
        market_id=market_id,
        token_id=token_id,
        direction=direction,
        size=size,
        entry_price=entry_price,
        entry_time=datetime.now(timezone.utc),
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        entry_best_ask=best_ask_price,
        entry_avg_fill_price=float(plan["vwap_price"]),
        entry_full_fill=bool(plan.get("full_fill", False)),
        actual_entry_price=float(plan["vwap_price"]),
        actual_entry_size=size,
        total_invested_usdc=float(plan["vwap_price"]) * size,
        risk_score=risk_assessment.risk_score if risk_assessment else None,
        risk_level=risk_assessment.risk_level if risk_assessment else None,
        risk_adjusted_stake=risk_assessment.adjusted_stake if risk_assessment else None,
        btc_cross_count=btc_cross_count,
        abs_btc_diff=abs_btc_diff,
        entry_price_risk=risk_assessment.entry_price_risk if risk_assessment else None,
        direction_risk=risk_assessment.direction_risk if risk_assessment else None,
        stability_risk=risk_assessment.stability_risk if risk_assessment else None,
        window_open_btc_price=getattr(self, 'window_open_price', None),
    )
    self._persist_entry_event(position=self.position, order_id=order_id)

    if not self.dry_run:
        self._schedule_position_balance_confirmation(
            market_slug=market_slug,
            token_id=token_id,
            order_id=order_id,
        )

    if self._poly_watcher:
        self._poly_watcher.stop()
    self._poly_watcher = PolymarketAssetPriceWatcher(
        asset_id=token_id,
        on_price=self._on_polymarket_price,
        on_book=self._on_polymarket_book,
    )
    self._poly_watcher.start()
    open_ms = (time.perf_counter() - open_t0) * 1000
    self._record_latency("open_total", open_ms)
    logger.info(
        "开仓链路总耗时: market=%s token=%s source=%s latency=%.2fms",
        market_slug,
        token_id,
        open_book_source,
        open_ms,
    )
