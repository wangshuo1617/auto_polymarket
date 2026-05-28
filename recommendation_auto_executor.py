#!/usr/bin/env python3
"""recommendation 自动触发执行器 (长跑进程)。

启动顺序:
  1. 拿 PG advisory lock,确保整个集群只有一个 executor 在跑
  2. 启动 BinanceBTCPriceWatcher,把价格事件 enqueue 进 TriggerEngine
  3. TriggerEngine.start() — worker thread 从 queue 消费 + refresh thread 每 30s 刷 DB
  4. SIGTERM/SIGINT 优雅退出

非目标(v1):
  - 不订阅 Polymarket 价格(poly_bid/poly_ask 类 trigger 暂不支持)
  - 不处理 cancel(状态机不同)
  - 不做跨实例限速(只靠 advisory lock 强制单实例)
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any, Optional

# 项目根加入 sys.path,与其他 root entry 对齐
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from py_clob_client_v2.clob_types import OrderType  # noqa: E402

from data.polymarket import buy_order, sell_order, get_best_prices, get_balance_allowance, get_positions  # noqa: E402
from services.shared.watchers import BinanceBTCPriceWatcher  # noqa: E402
from services.recommendation_db import RecommendationDB  # noqa: E402
from services.recommendation_trigger import auto_trigger_db as atdb  # noqa: E402
from services.recommendation_trigger.engine import TriggerEngine, _PlanEntry  # noqa: E402
from services import manual_pending_orders as _mpo  # noqa: E402

logger = logging.getLogger("recommendation_auto_executor")

# 与 dashboard manual 路径(app.APP_PM_PROFILE)以及 position_analyze 的 ANALYZE_PROFILE
# 使用同一份 profile,避免 AI 看 A 账号持仓/净值,executor 却下单到 B 账号导致 size 错配。
# 优先级: AUTO_EXECUTOR_PM_PROFILE → POLYMARKET_PROFILE → "analyze"
# 注意:.env 中 POLYMARKET_PROFILE=trade 是给 5m_trade 用的,这里必须显式覆盖。
AUTO_EXECUTOR_PM_PROFILE = (
    os.getenv("AUTO_EXECUTOR_PM_PROFILE")
    or os.getenv("POLYMARKET_PROFILE")
    or "analyze"
)
RATE_LIMIT_PER_MINUTE = int(os.getenv("AUTO_EXECUTE_RATE_PER_MINUTE", "6"))
DRY_RUN = os.getenv("AUTO_EXECUTE_DRY_RUN", "0").strip() in {"1", "true", "TRUE", "yes"}
KILLSWITCH = os.getenv("AUTO_EXECUTE_KILLSWITCH", "0").strip() in {"1", "true", "TRUE", "yes"}


_db = RecommendationDB()


class _BTCOneMinuteCloseBuilder:
    """用 aggTrade tick 聚合出已收盘的 1m BTC K 线 close。"""

    def __init__(self) -> None:
        self._minute_start_ms: Optional[int] = None
        self._open: Optional[float] = None
        self._high: Optional[float] = None
        self._low: Optional[float] = None
        self._close: Optional[float] = None

    def update(self, *, price: float, event_ms: int) -> Optional[dict[str, Any]]:
        minute_start_ms = int(event_ms // 60_000 * 60_000)
        if self._minute_start_ms is None:
            self._start_new(minute_start_ms, price)
            return None

        if minute_start_ms == self._minute_start_ms:
            self._high = max(float(self._high or price), price)
            self._low = min(float(self._low or price), price)
            self._close = price
            return None

        closed = {
            "open_time_ms": self._minute_start_ms,
            "close_time_ms": self._minute_start_ms + 60_000,
            "open": float(self._open or price),
            "high": float(self._high or price),
            "low": float(self._low or price),
            "close": float(self._close or price),
        }
        self._start_new(minute_start_ms, price)
        return closed

    def _start_new(self, minute_start_ms: int, price: float) -> None:
        self._minute_start_ms = minute_start_ms
        self._open = price
        self._high = price
        self._low = price
        self._close = price


def _executor_label() -> str:
    try:
        host = socket.gethostname() or "executor"
    except Exception:  # noqa: BLE001
        host = "executor"
    safe = "".join(c for c in host if c.isalnum() or c in {"-", "_", "."})[:48]
    return f"auto_executor@{safe}"


def _execute(entry: _PlanEntry, ctx: dict[str, Any]) -> None:
    """engine 调到这里:对该 plan 执行原子 claim + 真实下单 + record_action + mark_plan_*。

    关键约束:
      - DRY_RUN 模式下**不**消费 plan(不调 claim,只 log)
      - 否则必须先 atdb.claim_auto_plan_for_execution(),失败直接放弃
      - frozen_payload 是 enable 时锁定的执行参数,fire 时不再 re-derive
      - record_action 带 triggered_by='auto' + plan_id
      - 成功:mark_plan_fired;失败:mark_plan_failed
    """
    plan_id = entry.plan_id
    item_id = entry.item_id
    action = entry.action_type
    payload = entry.frozen_payload

    market_id = str(payload.get("market_id") or "").strip()
    token_id = str(payload.get("token_id") or "").strip()
    try:
        # 兼容历史字段名:limit_price/size_shares 与 price/size 都接受
        price = float(payload.get("limit_price") or payload.get("price") or 0.0)
        size = float(payload.get("size_shares") or payload.get("size") or 0.0)
    except (TypeError, ValueError):
        price = 0.0
        size = 0.0

    if not market_id or not token_id or price <= 0 or size <= 0:
        logger.error(
            "[plan %s item %s] frozen_payload 字段非法,放弃: market_id=%s token_id=%s price=%s size=%s",
            plan_id, item_id, market_id, token_id, price, size,
        )
        return

    if DRY_RUN:
        logger.info("[plan %s item %s] DRY-RUN: 命中触发条件但不执行真实下单 ctx=%s", plan_id, item_id, ctx)
        # 仅日志, 不写 actions / 不改 plan / 不改 item, 避免污染真实状态(参考 rubber-duck #7)
        return

    try:
        claim = atdb.claim_auto_plan_for_execution(plan_id=plan_id)
    except atdb.AutoTriggerClaimError as exc:
        logger.warning("[plan %s] claim 拒: %s", plan_id, exc)
        return

    logger.info(
        "[plan %s item %s] AUTO FIRE action=%s market=%s price=%s size=%s ctx=%s claim=%s",
        plan_id, item_id, action, market_id, price, size, ctx, claim,
    )

    request_payload = {
        "market_id": market_id,
        "token_id": token_id,
        "price": price,
        "size": size,
        "frozen": payload,
        "trigger_ctx": ctx,
        "plan_id": plan_id,
    }

    raw_order_type = str(payload.get("order_type") or "GTC").strip().upper()
    sell_order_type = OrderType.GTC if raw_order_type == "GTC" else OrderType.FAK

    try:
        if action == "buy":
            order_id = buy_order(
                market_id=market_id,
                token_id=token_id,
                price=price,
                size=size,
                profile=AUTO_EXECUTOR_PM_PROFILE,
            )
        elif action == "sell":
            order_id = sell_order(
                market_id=market_id,
                token_id=token_id,
                price=price,
                size=size,
                profile=AUTO_EXECUTOR_PM_PROFILE,
                order_type=sell_order_type,
            )
        else:
            order_id = None
    except Exception as exc:  # noqa: BLE001
        logger.exception("[plan %s] %s_order 抛异常", plan_id, action)
        _db.record_action(
            item_id=item_id,
            action_type=action,
            status="failed",
            request_payload=request_payload,
            error_text=f"{type(exc).__name__}: {exc}",
            triggered_by="auto",
            plan_id=plan_id,
        )
        atdb.mark_plan_failed(plan_id=plan_id, reason=f"{type(exc).__name__}: {exc}"[:200])
        return

    if not order_id:
        _db.record_action(
            item_id=item_id,
            action_type=action,
            status="failed",
            request_payload=request_payload,
            error_text=f"{action}_order 返回空 order_id",
            triggered_by="auto",
            plan_id=plan_id,
        )
        atdb.mark_plan_failed(plan_id=plan_id, reason="order returned empty id")
        return

    _db.record_action(
        item_id=item_id,
        action_type=action,
        status="submitted",
        order_id=str(order_id),
        request_payload=request_payload,
        triggered_by="auto",
        plan_id=plan_id,
    )
    atdb.mark_plan_fired(plan_id=plan_id, order_id=str(order_id))
    logger.info("[plan %s item %s] AUTO order submitted: order_id=%s", plan_id, item_id, order_id)


def _setup_logging() -> None:
    logs_dir = _PROJECT_ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    fh = logging.handlers.RotatingFileHandler(
        logs_dir / "recommendation_auto_executor.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
    )
    fh.setFormatter(formatter)
    root.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    root.addHandler(sh)


def main() -> int:
    _setup_logging()
    logger.info("===== recommendation_auto_executor 启动 =====")
    logger.info("executor=%s profile=%s rate_limit=%s/min dry_run=%s killswitch=%s",
                _executor_label(), AUTO_EXECUTOR_PM_PROFILE, RATE_LIMIT_PER_MINUTE, DRY_RUN, KILLSWITCH)
    if KILLSWITCH:
        logger.warning("KILLSWITCH 已激活,executor 不会执行任何下单(连价格事件也会被 engine 内部限速 / 健康度门拦下)")

    if not atdb.acquire_singleton_lock():
        logger.error("已有另一个 auto_executor 实例在跑(advisory lock 被占),退出")
        return 1

    # 确保表/列存在(幂等)
    try:
        _db.init_tables()
    except Exception:  # noqa: BLE001
        logger.exception("init_tables 失败,继续")

    # 启动巡检:修复上次进程崩溃留下的 stale executing plan
    # (record_action 与 mark_plan_fired 之间的两段式回写若被打断,plan 会卡 executing)
    try:
        repair_summary = atdb.auto_repair_stale_executing_plans(timeout_minutes=5)
        if repair_summary["scanned"] > 0:
            logger.warning("启动 stale-executing 自动修复: %s", repair_summary)
        else:
            logger.info("启动 stale-executing 巡检: 无积压")
    except Exception:  # noqa: BLE001
        logger.exception("启动 stale-executing 自动修复失败,继续")

    engine = TriggerEngine(execute_fn=_execute, rate_capacity_per_minute=RATE_LIMIT_PER_MINUTE)
    engine.start()
    btc_1m_builder = _BTCOneMinuteCloseBuilder()

    # 手动延迟挂单表幂等建表
    try:
        _mpo.ensure_table()
    except Exception:  # noqa: BLE001
        logger.exception("manual_pending_orders ensure_table 失败,继续")

    _last_expiry_sweep = [0.0]

    def _resolve_size_at_fire(
        *,
        action: str,
        token_id: str,
        size_spec: dict,
        limit_price: float,
    ) -> tuple[float, str]:
        """根据 size_spec 在 fire 瞬间计算实际下单 shares,返回 (size, debug_info)。
        失败抛 ValueError(error_message)。
        """
        st = (size_spec or {}).get("type") or "shares"
        sv = float(size_spec.get("value") or 0)
        if sv <= 0:
            raise ValueError(f"size_spec.value={sv} 非法")
        if st == "shares":
            return round(sv, 2), f"shares={sv}"
        if st == "usdc":
            if limit_price <= 0:
                raise ValueError("usdc sizing 需要正 limit_price")
            return round(sv / limit_price, 2), f"usdc={sv}/price={limit_price}"
        if st == "pct_balance":
            try:
                bal_str = get_balance_allowance(profile=AUTO_EXECUTOR_PM_PROFILE)
                balance = float(bal_str.lstrip("$"))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"取 USDC 余额失败: {exc}") from exc
            usdc_to_use = balance * sv / 100.0
            if usdc_to_use <= 0 or limit_price <= 0:
                raise ValueError(f"pct_balance 计算结果非法: balance={balance} pct={sv} price={limit_price}")
            return round(usdc_to_use / limit_price, 2), f"balance={balance}*{sv}%/price={limit_price}"
        if st == "pct_position":
            try:
                positions = get_positions(profile=AUTO_EXECUTOR_PM_PROFILE)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"取持仓失败: {exc}") from exc
            pos_shares = 0.0
            for p in positions or []:
                if str(p.get("asset") or p.get("token_id") or "") == token_id:
                    try:
                        pos_shares = float(p.get("size") or 0)
                    except (TypeError, ValueError):
                        pos_shares = 0.0
                    break
            if pos_shares <= 0:
                raise ValueError(f"无可卖持仓 (token_id={token_id[:10]}...)")
            return round(pos_shares * sv / 100.0, 2), f"position={pos_shares}*{sv}%"
        raise ValueError(f"未知 size_spec.type={st}")

    def _resolve_price_at_fire(
        *,
        action: str,
        token_id: str,
        price_spec: dict,
        parent_fill_price: Optional[float] = None,
        fallback_price: float = 0.5,
    ) -> tuple[float, str]:
        """根据 price_spec 在 fire 瞬间计算 limit price,返回 (price, debug_info)。"""
        pt = (price_spec or {}).get("type") or "absolute"
        if pt == "absolute":
            v = float(price_spec.get("value") or fallback_price)
            return round(max(0.01, min(0.99, v)), 3), f"abs={v}"
        if pt == "market":
            offset = float(price_spec.get("offset") or 0.0)
            try:
                quotes = get_best_prices([token_id], profile=AUTO_EXECUTOR_PM_PROFILE)
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"取 best_prices 失败: {exc}") from exc
            q = quotes.get(token_id) or {}
            ref = q.get("best_ask") if action == "buy" else q.get("best_bid")
            if not ref or ref <= 0:
                raise ValueError(f"market mode 无法取到 best_{'ask' if action=='buy' else 'bid'}")
            computed = round(float(ref) + offset, 3)
            return max(0.01, min(0.99, computed)), f"market:{'ask' if action=='buy' else 'bid'}={ref}+{offset}"
        if pt == "cost_pct":
            if parent_fill_price is None or parent_fill_price <= 0:
                raise ValueError("cost_pct price 需要 parent_fill_price")
            pct = float(price_spec.get("value") or 0)
            computed = round(parent_fill_price * (1.0 + pct / 100.0), 3)
            return max(0.01, min(0.99, computed)), f"cost={parent_fill_price}*(1+{pct}%)"
        raise ValueError(f"未知 price_spec.type={pt}")

    def _process_manual_pending(
        btc_price: Optional[float] = None,
        share_prices: Optional[dict[str, float]] = None,
    ) -> None:
        try:
            triggered = _mpo.fetch_triggered_orders(btc_price, share_prices=share_prices)
        except Exception:  # noqa: BLE001
            logger.exception("manual_pending fetch_triggered 失败")
            return
        for order in triggered:
            order_id = order.get('id')
            try:
                claimed = _mpo.try_claim_order(order_id)
            except Exception:  # noqa: BLE001
                logger.exception("manual_pending claim 失败 id=%s", order_id)
                continue
            if not claimed:
                continue  # 已被别人/前一次 tick claim

            action = claimed['action']
            token_id = claimed['token_id']
            market_id = claimed['market_id']
            size_spec = claimed.get('size_spec') or {"type": "shares", "value": claimed.get('size') or 0}
            price_spec = claimed.get('price_spec')
            if not price_spec:
                # 兼容老路径:trigger_market_offset 走 sell 市价;否则用 absolute price
                extra = claimed.get('extra') or {}
                legacy_offset = extra.get('trigger_market_offset') if isinstance(extra, dict) else None
                if legacy_offset is not None:
                    price_spec = {"type": "market", "offset": float(legacy_offset)}
                else:
                    price_spec = {"type": "absolute", "value": float(claimed.get('price') or 0.5)}

            parent_fill_price = order.get('_parent_fill_price')

            # 1. 先算 limit price (usdc/pct_balance sizing 需要)
            try:
                limit_price, price_dbg = _resolve_price_at_fire(
                    action=action,
                    token_id=token_id,
                    price_spec=price_spec,
                    parent_fill_price=parent_fill_price,
                    fallback_price=float(claimed.get('price') or 0.5),
                )
            except ValueError as ve:
                logger.warning("[manual_pending %s] price 解析失败: %s", order_id, ve)
                _mpo.mark_order_failed(order_id, error_message=f"price spec: {ve}")
                continue

            # 2. 再算 size
            try:
                size, size_dbg = _resolve_size_at_fire(
                    action=action,
                    token_id=token_id,
                    size_spec=size_spec,
                    limit_price=limit_price,
                )
            except ValueError as ve:
                logger.warning("[manual_pending %s] size 解析失败: %s", order_id, ve)
                _mpo.mark_order_failed(order_id, error_message=f"size spec: {ve}")
                continue

            if size <= 0:
                _mpo.mark_order_failed(order_id, error_message=f"resolved size={size}")
                continue

            logger.info(
                "[manual_pending %s] FIRE action=%s kind=%s btc=%s plan=%s parent=%s price=%s (%s) size=%s (%s)",
                order_id, action, claimed.get('trigger_kind'), btc_price,
                claimed.get('plan_id'), claimed.get('parent_pending_id'),
                limit_price, price_dbg, size, size_dbg,
            )

            if DRY_RUN:
                logger.info("[manual_pending %s] DRY-RUN 不真实下单", order_id)
                _mpo.mark_order_failed(order_id, error_message="dry-run, not executed")
                continue
            try:
                if action == 'buy':
                    fired_id = buy_order(
                        market_id=market_id,
                        token_id=token_id,
                        price=limit_price,
                        size=size,
                        profile=AUTO_EXECUTOR_PM_PROFILE,
                    )
                else:
                    fired_id = sell_order(
                        market_id=market_id,
                        token_id=token_id,
                        price=limit_price,
                        size=size,
                        profile=AUTO_EXECUTOR_PM_PROFILE,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("[manual_pending %s] 下单异常", order_id)
                _mpo.mark_order_failed(order_id, error_message=f"{type(exc).__name__}: {exc}")
                continue
            if not fired_id:
                _mpo.mark_order_failed(order_id, error_message=f"{action}_order returned empty id")
                continue
            # fill_price = limit price (Polymarket limit 单成交不会差于限价,作为子档 cost basis)
            _mpo.mark_order_fired(order_id, fired_order_id=str(fired_id), fill_price=limit_price)
            logger.info("[manual_pending %s] DONE order_id=%s fill_price=%s", order_id, fired_id, limit_price)

    def _on_btc(payload: dict[str, Any]) -> None:
        try:
            price = float(payload.get("last_price"))
        except (TypeError, ValueError):
            return
        try:
            event_ms = int(payload.get("timestamp") or int(time.time() * 1000))
        except (TypeError, ValueError):
            event_ms = int(time.time() * 1000)
        try:
            # tick 只用于 executor 健康度与 immediate 计划；BTC 阈值单改用 1m close 确认。
            engine.enqueue_btc_tick(price, payload.get("update_time"))
        except (TypeError, ValueError):
            pass
        closed = btc_1m_builder.update(price=price, event_ms=event_ms)
        if closed:
            close_price = float(closed["close"])
            closed_at = float(closed["close_time_ms"]) / 1000.0
            engine.enqueue_btc_1m_close(close_price, closed_at)
            logger.info(
                "BTC 1m close 确认: close=%s high=%s low=%s close_time=%s",
                close_price,
                closed["high"],
                closed["low"],
                int(closed["close_time_ms"]),
            )
            _process_manual_pending(btc_price=close_price)
        # 每 60s 扫一次过期 pending + 卡死 executing
        now_ts = time.time()
        if now_ts - _last_expiry_sweep[0] > 60:
            _last_expiry_sweep[0] = now_ts
            try:
                n = _mpo.expire_overdue_orders()
                if n:
                    logger.info("manual_pending 过期清理: %s", n)
            except Exception:  # noqa: BLE001
                logger.exception("manual_pending expire_overdue_orders 失败")
            try:
                stale_report = _mpo.repair_stale_executing_orders(
                    timeout_minutes=int(os.getenv("MANUAL_PENDING_STALE_EXECUTING_MIN", "10"))
                )
                if stale_report.get("marked_failed"):
                    logger.warning(
                        "manual_pending stale-executing 告警: scanned=%s marked_failed=%s ids=%s",
                        stale_report.get("scanned"),
                        stale_report.get("marked_failed"),
                        stale_report.get("ids"),
                    )
            except Exception:  # noqa: BLE001
                logger.exception("manual_pending repair_stale_executing_orders 失败")

    # ============ share 价轮询线程 ============
    # 5s 一轮:收集 share_abs / share_cost_pct 类挂单的 token_id,批量取 best_bid/ask,
    # 取中间价作为 share price 输入,触发后走与 BTC 相同的 _process_manual_pending 路径。
    SHARE_POLL_INTERVAL = float(os.getenv("MANUAL_PENDING_SHARE_POLL_SEC", "5"))

    def _share_poll_loop() -> None:
        logger.info("manual_pending share-price 轮询启动 interval=%ss", SHARE_POLL_INTERVAL)
        while not stop["flag"]:
            try:
                token_ids = _mpo.collect_active_share_token_ids()
                share_prices: dict[str, float] = {}
                if token_ids:
                    quotes = get_best_prices(token_ids, profile=AUTO_EXECUTOR_PM_PROFILE)
                    for tid, q in (quotes or {}).items():
                        bid = (q or {}).get("best_bid")
                        ask = (q or {}).get("best_ask")
                        if bid and ask and bid > 0 and ask > 0:
                            share_prices[tid] = (float(bid) + float(ask)) / 2.0
                        elif bid:
                            share_prices[tid] = float(bid)
                        elif ask:
                            share_prices[tid] = float(ask)
                # 始终调用 _process_manual_pending:
                #  - 有 share_prices: 评估 share_abs / share_cost_pct
                #  - 无 share_prices: 仍可触发 time_after_parent_fill (纯时间,不依赖价格)
                _process_manual_pending(share_prices=share_prices if share_prices else None)
            except Exception:  # noqa: BLE001
                logger.exception("share-price 轮询异常")
            # 用 sleep 而不是 Event.wait 简化(stop 时最多多等 SHARE_POLL_INTERVAL)
            for _ in range(int(max(1, SHARE_POLL_INTERVAL))):
                if stop["flag"]:
                    break
                time.sleep(1)
        logger.info("manual_pending share-price 轮询退出")

    watcher = BinanceBTCPriceWatcher(symbol="btcusdt", callback=_on_btc)
    watcher.start()

    stop = {"flag": False}

    share_thread = __import__("threading").Thread(target=_share_poll_loop, name="mpo-share-poll", daemon=True)
    share_thread.start()

    def _handle_signal(signum, frame):  # noqa: ARG001
        logger.info("收到信号 %s,准备退出", signum)
        stop["flag"] = True

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    try:
        while not stop["flag"]:
            time.sleep(2)
            if int(time.time()) % 60 == 0:
                logger.info("heartbeat: %s", engine.stats())
    finally:
        try:
            watcher.stop()
        except Exception:  # noqa: BLE001
            logger.exception("watcher.stop 异常")
        try:
            engine.stop()
        except Exception:  # noqa: BLE001
            logger.exception("engine.stop 异常")
        atdb.release_singleton_lock()
        logger.info("===== recommendation_auto_executor 已退出 =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
