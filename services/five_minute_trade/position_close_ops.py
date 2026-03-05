import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from data.polymarket import (
    get_conditional_token_balance,
    get_market_metadata,
    get_order_book,
    get_order_detail,
    normalize_order_size,
    sell_order,
)

from .models import OpenPosition
from .watchers import PolymarketAssetPriceWatcher

logger = logging.getLogger(__name__)


def schedule_position_balance_confirmation(
    trader: Any,
    market_slug: str,
    token_id: str,
    order_id: Optional[str] = None,
    match_check_delay_sec: int = 3,
    first_balance_delay_sec: int = 5,
    retry_balance_delay_sec: int = 7,
) -> None:
    self = trader

    def _run() -> None:
        start_ts = time.monotonic()

        def _sleep_until(offset_sec: int) -> None:
            remain = float(offset_sec) - (time.monotonic() - start_ts)
            if remain > 0:
                time.sleep(remain)

        matched_size = 0.0
        order_status = ""
        matched_price: Optional[float] = None
        if order_id:
            _sleep_until(match_check_delay_sec)
            try:
                detail = get_order_detail(order_id)
                if isinstance(detail, dict):
                    matched_size = self._parse_order_matched_size(detail)
                    matched_price = self._extract_execution_price_from_order(detail)
                    order_status = str(detail.get("status") or "").upper()
                    logger.info(
                        "建仓快通道检查: order_id=%s status=%s matched=%.6f avg_price=%s",
                        order_id,
                        order_status,
                        matched_size,
                        f"{matched_price:.6f}" if matched_price is not None else "N/A",
                    )
            except Exception as e:
                logger.warning("建仓快通道查询订单状态失败，继续余额确认: order_id=%s error=%s", order_id, e)

        _sleep_until(first_balance_delay_sec)

        with self._lock:
            pos = self.position
            if (
                pos is None
                or pos.market_slug != market_slug
                or pos.token_id != token_id
            ):
                return
            market_info = self._market_cache.get(market_slug) or {}
            market_meta = market_info.get("market_meta") or {}
            tick_size = market_meta.get("minimum_tick_size", "0.01")

        raw_balance = get_conditional_token_balance(token_id)
        confirmed_size = normalize_order_size(raw_balance, tick_size=tick_size)

        if confirmed_size <= 0 and matched_size > 0:
            extra_wait = max(0, retry_balance_delay_sec - first_balance_delay_sec)
            if extra_wait > 0:
                logger.warning(
                    "建仓后%ss余额为0但订单已有成交，%ss 后执行二次确认: market=%s token=%s order_id=%s status=%s matched=%.6f",
                    first_balance_delay_sec,
                    extra_wait,
                    market_slug,
                    token_id,
                    order_id,
                    order_status,
                    matched_size,
                )
                _sleep_until(retry_balance_delay_sec)
            raw_balance_retry = get_conditional_token_balance(token_id)
            confirmed_size_retry = normalize_order_size(raw_balance_retry, tick_size=tick_size)
            logger.info(
                "建仓后余额二次确认: market=%s token=%s first=%.6f retry=%.6f raw_retry=%.6f order_id=%s retry_delay=%ss",
                market_slug,
                token_id,
                confirmed_size,
                confirmed_size_retry,
                raw_balance_retry,
                order_id,
                retry_balance_delay_sec,
            )
            raw_balance = raw_balance_retry
            confirmed_size = confirmed_size_retry

        with self._lock:
            pos = self.position
            if (
                pos is None
                or pos.market_slug != market_slug
                or pos.token_id != token_id
            ):
                return

            old_size = float(pos.size)
            pos.size = confirmed_size
            pos.balance_confirmed = True
            if matched_size > 0:
                pos.actual_entry_size = matched_size
            elif pos.actual_entry_size is None and confirmed_size > 0:
                pos.actual_entry_size = confirmed_size

            if matched_price is not None:
                pos.actual_entry_price = matched_price
            elif pos.actual_entry_price is None and pos.entry_avg_fill_price is not None:
                pos.actual_entry_price = pos.entry_avg_fill_price

            if (
                pos.actual_entry_price is not None
                and pos.actual_entry_size is not None
                and pos.actual_entry_size > 0
            ):
                pos.total_invested_usdc = pos.actual_entry_price * pos.actual_entry_size
            logger.info(
                "建仓后余额确认: market=%s token=%s old_size=%.6f confirmed_size=%.6f raw_balance=%.6f entry_size=%.6f entry_price=%s invested=%s delay=%ss",
                market_slug,
                token_id,
                old_size,
                confirmed_size,
                raw_balance,
                pos.actual_entry_size or 0.0,
                f"{pos.actual_entry_price:.6f}" if pos.actual_entry_price is not None else "N/A",
                f"{pos.total_invested_usdc:.6f}" if pos.total_invested_usdc is not None else "N/A",
                first_balance_delay_sec,
            )

            if confirmed_size <= 0:
                if matched_size > 0:
                    logger.error("重大延迟: 引擎已成交 %.6f 但 API 余额为 0，强制保留持仓以维持风控保护！", matched_size)
                    pos.size = normalize_order_size(matched_size * 0.985, tick_size=tick_size)
                    if pos.actual_entry_size is None:
                        pos.actual_entry_size = matched_size
                    if pos.actual_entry_price is None and matched_price is not None:
                        pos.actual_entry_price = matched_price
                    if (
                        pos.total_invested_usdc is None
                        and pos.actual_entry_price is not None
                        and pos.actual_entry_size is not None
                        and pos.actual_entry_size > 0
                    ):
                        pos.total_invested_usdc = pos.actual_entry_price * pos.actual_entry_size
                    pos.balance_confirmed = True
                else:
                    logger.info("建仓后余额确认为0且无撮合记录，清理本地持仓避免阻塞后续开仓: market=%s token=%s", market_slug, token_id)
                    self.position = None
                    if self._poly_watcher:
                        self._poly_watcher.stop()
                        self._poly_watcher = None

    threading.Thread(
        target=_run,
        daemon=True,
        name="position-balance-confirm",
    ).start()


def schedule_post_close_balance_check(
    trader: Any,
    closed_position: OpenPosition,
    reason: str,
    target_close_size: float,
    expected_exit_price: Optional[float] = None,
    exit_best_bid: Optional[float] = None,
    exit_avg_fill_price: Optional[float] = None,
    exit_full_fill: Optional[bool] = None,
    order_id: Optional[str] = None,
    match_check_delay_sec: int = 3,
    balance_check_delay_sec: int = 5,
) -> None:
    self = trader

    def _run() -> None:
        start_ts = time.monotonic()

        def _sleep_until(offset_sec: int) -> None:
            remain = float(offset_sec) - (time.monotonic() - start_ts)
            if remain > 0:
                time.sleep(remain)

        order_detail: Optional[Dict[str, Any]] = None
        matched_raw = 0.0
        actual_exit_price = None
        order_status = ""

        if order_id:
            _sleep_until(match_check_delay_sec)
            try:
                order_detail = get_order_detail(order_id)
                if isinstance(order_detail, dict):
                    order_status = str(order_detail.get("status") or "").upper()
                    matched_raw = self._parse_order_matched_size(order_detail)
                    actual_exit_price = self._extract_execution_price_from_order(order_detail)

                    logger.info(
                        "平仓快通道检查: order_id=%s status=%s matched=%.6f target=%.6f avg_price=%s",
                        order_id,
                        order_status,
                        matched_raw,
                        target_close_size,
                        f"{actual_exit_price:.6f}" if actual_exit_price is not None else "N/A",
                    )

                    if order_status == "MATCHED" or matched_raw >= target_close_size * 0.999:
                        realized_size = min(max(matched_raw, 0.0), target_close_size)
                        final_exit_price = (
                            actual_exit_price
                            if actual_exit_price is not None and actual_exit_price > 0
                            else (
                                expected_exit_price
                                if expected_exit_price is not None and expected_exit_price > 0
                                else closed_position.entry_price
                            )
                        )
                        self._append_realized_trade(
                            pos=closed_position,
                            reason=reason,
                            matched_size=realized_size,
                            actual_exit_price=final_exit_price,
                            expected_exit_price=expected_exit_price,
                            exit_best_bid=exit_best_bid,
                            exit_avg_fill_price=(
                                actual_exit_price
                                if actual_exit_price is not None
                                else exit_avg_fill_price
                            ),
                            exit_full_fill=True,
                        )
                        logger.info("⚡ 快通道确认: 订单已完全成交，已按真实成交价记账")
                        return
            except Exception as e:
                logger.warning("快通道查询订单状态失败，降级到慢通道: %s", e)

        logger.info(
            "快通道未确认完全成交 (可能发生部分成交/撤单)，将在下单后第 %ss 启动慢通道余额复核...",
            balance_check_delay_sec,
        )
        _sleep_until(balance_check_delay_sec)

        market_info = self._market_cache.get(closed_position.market_slug) or {}
        market_meta = market_info.get("market_meta") or {}
        tick_size = market_meta.get("minimum_tick_size", "0.01")

        raw_balance = get_conditional_token_balance(closed_position.token_id)
        remaining_size = normalize_order_size(raw_balance, tick_size=tick_size)
        sold_by_balance = max(0.0, target_close_size - remaining_size)

        if order_id:
            try:
                refreshed_detail = get_order_detail(order_id)
                if isinstance(refreshed_detail, dict):
                    order_detail = refreshed_detail
                    matched_raw = max(matched_raw, self._parse_order_matched_size(refreshed_detail))
                    refreshed_exit_price = self._extract_execution_price_from_order(refreshed_detail)
                    if refreshed_exit_price is not None:
                        actual_exit_price = refreshed_exit_price
            except Exception as e:
                logger.warning("慢通道刷新订单详情失败: order_id=%s error=%s", order_id, e)

        realized_size = min(target_close_size, max(matched_raw, sold_by_balance))
        final_exit_price = (
            actual_exit_price
            if actual_exit_price is not None and actual_exit_price > 0
            else (
                expected_exit_price
                if expected_exit_price is not None and expected_exit_price > 0
                else closed_position.entry_price
            )
        )

        should_retry = False
        with self._lock:
            logger.info(
                "平仓慢通道余额确认: market=%s token=%s remaining_size=%.6f sold_by_balance=%.6f matched=%.6f raw_balance=%.6f delay=%ss reason=%s",
                closed_position.market_slug,
                closed_position.token_id,
                remaining_size,
                sold_by_balance,
                matched_raw,
                raw_balance,
                balance_check_delay_sec,
                reason,
            )

            if remaining_size <= 0.02:
                if realized_size > 0:
                    self._append_realized_trade(
                        pos=closed_position,
                        reason=reason,
                        matched_size=realized_size,
                        actual_exit_price=final_exit_price,
                        expected_exit_price=expected_exit_price,
                        exit_best_bid=exit_best_bid,
                        exit_avg_fill_price=(
                            actual_exit_price
                            if actual_exit_price is not None
                            else exit_avg_fill_price
                        ),
                        exit_full_fill=True,
                    )
                logger.info("慢通道确认: 残余份额不足 0.05 (实余 %.6f)，视为粉尘忽略，平仓彻底完成。", remaining_size)
                return

            if realized_size > 0:
                self._append_realized_trade(
                    pos=closed_position,
                    reason=f"{reason}_partial",
                    matched_size=realized_size,
                    actual_exit_price=final_exit_price,
                    expected_exit_price=expected_exit_price,
                    exit_best_bid=exit_best_bid,
                    exit_avg_fill_price=(
                        actual_exit_price
                        if actual_exit_price is not None
                        else exit_avg_fill_price
                    ),
                    exit_full_fill=False,
                )

            existing = self.position
            if (
                existing is not None
                and existing.market_slug == closed_position.market_slug
                and existing.token_id == closed_position.token_id
            ):
                if remaining_size > existing.size:
                    existing.size = remaining_size
                existing.balance_confirmed = True
                should_retry = True
            elif existing is None:
                self.position = OpenPosition(
                    market_slug=closed_position.market_slug,
                    market_id=closed_position.market_id,
                    token_id=closed_position.token_id,
                    direction=closed_position.direction,
                    size=remaining_size,
                    entry_price=closed_position.entry_price,
                    entry_time=closed_position.entry_time,
                    stop_loss_price=closed_position.stop_loss_price,
                    take_profit_price=closed_position.take_profit_price,
                    last_best_bid=closed_position.last_best_bid,
                    balance_confirmed=True,
                    entry_best_ask=closed_position.entry_best_ask,
                    entry_avg_fill_price=closed_position.entry_avg_fill_price,
                    entry_full_fill=closed_position.entry_full_fill,
                    actual_entry_price=closed_position.actual_entry_price,
                    actual_entry_size=remaining_size,
                    total_invested_usdc=self._compute_allocated_entry_cost(
                        closed_position,
                        remaining_size,
                    ),
                )
                should_retry = True
                if self._poly_watcher:
                    self._poly_watcher.stop()
                self._poly_watcher = PolymarketAssetPriceWatcher(
                    asset_id=closed_position.token_id,
                    on_price=self._on_polymarket_price,
                    on_book=self._on_polymarket_book,
                )
                self._poly_watcher.start()
                logger.warning(
                    "平仓慢通道发现真实残仓，已恢复持仓并准备重试平仓: market=%s token=%s size=%.6f",
                    closed_position.market_slug,
                    closed_position.token_id,
                    remaining_size,
                )

        if should_retry:
            residual_reason = f"{reason}_residual" if not reason.endswith("_residual") else reason
            self._force_close_position(reason=residual_reason)

    threading.Thread(
        target=_run,
        daemon=True,
        name="position-post-close-confirm",
    ).start()


def force_close_position(trader: Any, reason: str) -> None:
    self = trader

    if not self.position:
        return

    hold_seconds = (datetime.now(timezone.utc) - self.position.entry_time).total_seconds()
    min_hold = float(self.min_hold_before_close_sec)
    # 给边界比较留出极小浮点余量，避免日志显示 60.00s 仍被判定未达保护期。
    if (reason == "sl") and (hold_seconds + 1e-6 < min_hold):
        if self._should_emit_log(
            key=f"close_protection:{self.position.market_slug}:{self.position.token_id}:{reason}",
            interval_sec=10.0,
        ):
            logger.info(
                "平仓保护期生效，暂不平仓: reason=%s hold=%.2fs need>=%.2fs",
                reason,
                hold_seconds,
                min_hold,
            )
        return

    close_t0 = time.perf_counter()
    pos = self.position
    self.position = None

    if self._poly_watcher:
        self._poly_watcher.stop()
        self._poly_watcher = None

    market_meta = None
    market_info = self._market_cache.get(pos.market_slug)
    if market_info:
        market_meta = market_info.get("market_meta")
    if market_meta is None:
        market_meta = get_market_metadata(pos.market_id)

    exit_price = pos.last_best_bid
    if exit_price is None or exit_price <= 0:
        try:
            book = get_order_book(pos.token_id)
            if book is not None:
                bids = getattr(book, "bids", None) or []
                if bids:
                    best_bid_level = max(
                        bids, key=lambda lvl: float(getattr(lvl, "price"))
                    )
                    exit_price = float(getattr(best_bid_level, "price"))
        except Exception as e:
            logger.warning("获取平仓价格失败，将使用入场价: %s", e)
    if exit_price is None or exit_price <= 0:
        exit_price = pos.entry_price

    sell_plan: Optional[Dict[str, Any]] = None
    exit_best_bid: Optional[float] = None
    exit_avg_fill_price: Optional[float] = None
    exit_full_fill: Optional[bool] = None
    try:
        sell_plan = self._build_execution_plan(
            token_id=pos.token_id,
            side="sell",
            target_size=pos.size,
        )
        emit_close_detail_log = self._should_emit_log(
            key=f"close_detail:{pos.market_slug}:{pos.token_id}:{reason}",
            interval_sec=10.0,
        )
        bid_prices = sell_plan.get("level_prices_preview") or []
        if bid_prices and emit_close_detail_log:
            logger.info(
                "平仓买单价格(按高到低, 前10档): %s",
                ",".join(f"{float(price):.4f}" for price in bid_prices),
            )
        if emit_close_detail_log:
            self._log_execution_plan(
                stage=f"平仓[{reason}]",
                market_slug=pos.market_slug,
                token_id=pos.token_id,
                plan=sell_plan,
            )
            logger.info(
                "平仓价格观测: market=%s token=%s reason=%s source=%s best_from_levels=%.4f worst_fill=%.4f",
                pos.market_slug,
                pos.token_id,
                reason,
                str(sell_plan.get("book_source", "unknown")),
                float(sell_plan["best_price"]),
                float(sell_plan["worst_price"]),
            )
        exit_best_bid = float(sell_plan["best_price"])
        exit_avg_fill_price = float(sell_plan["vwap_price"])
        exit_full_fill = bool(sell_plan.get("full_fill", False))
        exit_price = float(sell_plan["worst_price"])

        if sell_plan["slippage_bps"] > self.MAX_EXIT_SLIPPAGE_BPS_WARN:
            logger.warning(
                "平仓预估滑点偏大: slippage=%.2fbps (>%.2fbps)",
                sell_plan["slippage_bps"],
                self.MAX_EXIT_SLIPPAGE_BPS_WARN,
            )
    except Exception as e:
        logger.warning("平仓深度评估失败，使用回退价格: %s", e)

    close_book_source = (
        str(sell_plan.get("book_source", "unknown"))
        if sell_plan is not None
        else "unknown"
    )
    sweep_price = exit_price
    target_close_size = pos.size
    if target_close_size > 0 and target_close_size < 0.02:
        logger.info("平仓拦截: 当前仓位(%.6f)极小，视为粉尘忽略，直接清理本地持仓", target_close_size)
        return
    if target_close_size <= 0:
        if pos.balance_confirmed:
            logger.info(
                "平仓时发现已确认零仓位，视为已平仓并清理本地持仓: market=%s token=%s reason=%s",
                pos.market_slug,
                pos.token_id,
                reason,
            )
            return

        logger.warning(
            "平仓跳过：持仓数量为0但尚未确认，恢复持仓等待后续确认 market=%s token=%s reason=%s confirmed=%s",
            pos.market_slug,
            pos.token_id,
            reason,
            pos.balance_confirmed,
        )
        self.position = pos
        return

    if not self.dry_run:
        if reason in {"sl", "sl_direction_change", "sl_residual"}:
            current_bid = min(
                pos.last_best_bid if pos.last_best_bid else exit_price,
                exit_price,
            )
            sweep_price = max(0.01, float(current_bid) - 0.05)
        else:
            current_bid = exit_price
            sweep_price = max(0.01, float(current_bid) - 0.01)

        logger.info("应用强平滑点: 预估价=%.4f 实际强平挂单价(sweep)=%.4f", exit_price, sweep_price)

        submit_t0 = time.perf_counter()
        order_id = sell_order(
            pos.market_id,
            pos.token_id,
            sweep_price,
            target_close_size,
            market_meta=market_meta,
        )
        submit_ms = (time.perf_counter() - submit_t0) * 1000
        self._record_latency("sell_submit", submit_ms)
        if not order_id:
            logger.warning(
                "平仓卖单提交失败，转入慢通道余额复核: market=%s token=%s price=%.4f size=%.4f",
                pos.market_id,
                pos.token_id,
                sweep_price,
                target_close_size,
            )
            self._schedule_post_close_balance_check(
                closed_position=pos,
                reason=f"{reason}_submit_fail",
                target_close_size=target_close_size,
                expected_exit_price=exit_price,
                exit_best_bid=exit_best_bid,
                exit_avg_fill_price=exit_avg_fill_price,
                exit_full_fill=exit_full_fill,
                order_id=None,
                match_check_delay_sec=3,
                balance_check_delay_sec=5,
            )
            close_ms = (time.perf_counter() - close_t0) * 1000
            self._record_latency("close_total", close_ms)
            logger.info(
                "平仓链路总耗时(失败): market=%s token=%s reason=%s source=%s latency=%.2fms",
                pos.market_slug,
                pos.token_id,
                reason,
                close_book_source,
                close_ms,
            )
            return
        else:
            logger.info("平仓卖单已提交，order_id=%s submit_latency=%.2fms", order_id, submit_ms)

            self._schedule_post_close_balance_check(
                closed_position=pos,
                reason=reason,
                target_close_size=target_close_size,
                expected_exit_price=exit_price,
                exit_best_bid=exit_best_bid,
                exit_avg_fill_price=exit_avg_fill_price,
                exit_full_fill=exit_full_fill,
                order_id=order_id,
                match_check_delay_sec=3,
                balance_check_delay_sec=5,
            )
    elif self.dry_run:
        dry_run_exit = exit_price
        self._append_realized_trade(
            pos=pos,
            reason=reason,
            matched_size=target_close_size,
            actual_exit_price=dry_run_exit,
            expected_exit_price=exit_price,
            exit_best_bid=exit_best_bid,
            exit_avg_fill_price=exit_avg_fill_price,
            exit_full_fill=exit_full_fill,
        )

    close_ms = (time.perf_counter() - close_t0) * 1000
    self._record_latency("close_total", close_ms)
    logger.info(
        "平仓链路总耗时: market=%s token=%s reason=%s source=%s latency=%.2fms",
        pos.market_slug,
        pos.token_id,
        reason,
        close_book_source,
        close_ms,
    )
