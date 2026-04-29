#!/usr/bin/env python3
"""recommendation 自动触发执行器 (长跑进程)。

启动顺序:
  1. 拿 PG advisory lock,确保整个集群只有一个 executor 在跑
  2. 启动 ChainlinkBTCPriceWatcher,把价格事件 enqueue 进 TriggerEngine
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
from typing import Any

# 项目根加入 sys.path,与其他 root entry 对齐
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from py_clob_client_v2.clob_types import OrderType  # noqa: E402

from data.polymarket import buy_order, sell_order, get_best_prices  # noqa: E402
from services.five_minute_trade.watchers import ChainlinkBTCPriceWatcher  # noqa: E402
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

    # 手动延迟挂单表幂等建表
    try:
        _mpo.ensure_table()
    except Exception:  # noqa: BLE001
        logger.exception("manual_pending_orders ensure_table 失败,继续")

    _last_expiry_sweep = [0.0]

    def _process_manual_pending(price: float) -> None:
        try:
            triggered = _mpo.fetch_triggered_orders(price)
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
            limit_price = float(claimed['price'])
            extra = claimed.get('extra') or {}
            offset = extra.get('trigger_market_offset') if isinstance(extra, dict) else None
            if offset is not None:
                try:
                    quotes = get_best_prices([claimed['token_id']], profile=AUTO_EXECUTOR_PM_PROFILE)
                    best_bid = (quotes.get(claimed['token_id']) or {}).get('best_bid')
                except Exception:  # noqa: BLE001
                    logger.exception("[manual_pending %s] 获取 best_bid 失败", order_id)
                    best_bid = None
                if not best_bid or best_bid <= 0:
                    _mpo.mark_order_failed(order_id, error_message="无法获取 best_bid,放弃市价挂单")
                    continue
                computed = round(best_bid + float(offset), 3)
                computed = max(0.01, min(0.99, computed))
                logger.info("[manual_pending %s] market mode: best_bid=%s offset=%s -> price=%s",
                            order_id, best_bid, offset, computed)
                limit_price = computed

            logger.info("[manual_pending %s] FIRE action=%s op=%s thr=%s btc=%s market=%s price=%s size=%s",
                        order_id, action, claimed['trigger_op'], claimed['trigger_btc_price'],
                        price, claimed['market_id'], limit_price, claimed['size'])
            if DRY_RUN:
                logger.info("[manual_pending %s] DRY-RUN 不真实下单", order_id)
                _mpo.mark_order_failed(order_id, error_message="dry-run, not executed")
                continue
            try:
                if action == 'buy':
                    fired_id = buy_order(
                        market_id=claimed['market_id'],
                        token_id=claimed['token_id'],
                        price=limit_price,
                        size=float(claimed['size']),
                        profile=AUTO_EXECUTOR_PM_PROFILE,
                    )
                else:
                    fired_id = sell_order(
                        market_id=claimed['market_id'],
                        token_id=claimed['token_id'],
                        price=limit_price,
                        size=float(claimed['size']),
                        profile=AUTO_EXECUTOR_PM_PROFILE,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.exception("[manual_pending %s] 下单异常", order_id)
                _mpo.mark_order_failed(order_id, error_message=f"{type(exc).__name__}: {exc}")
                continue
            if not fired_id:
                _mpo.mark_order_failed(order_id, error_message=f"{action}_order returned empty id")
                continue
            _mpo.mark_order_fired(order_id, fired_order_id=str(fired_id))
            logger.info("[manual_pending %s] DONE order_id=%s", order_id, fired_id)

    def _on_btc(payload: dict[str, Any]) -> None:
        try:
            price = float(payload.get("last_price"))
        except (TypeError, ValueError):
            return
        try:
            engine.enqueue_btc_price(price, payload.get("update_time"))
        except (TypeError, ValueError):
            pass
        _process_manual_pending(price)
        # 每 60s 扫一次过期 pending
        now_ts = time.time()
        if now_ts - _last_expiry_sweep[0] > 60:
            _last_expiry_sweep[0] = now_ts
            try:
                n = _mpo.expire_overdue_orders()
                if n:
                    logger.info("manual_pending 过期清理: %s", n)
            except Exception:  # noqa: BLE001
                logger.exception("manual_pending expire_overdue_orders 失败")

    watcher = ChainlinkBTCPriceWatcher(symbol="btcusdt", callback=_on_btc)
    watcher.start()

    stop = {"flag": False}

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
