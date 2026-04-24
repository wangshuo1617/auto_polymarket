"""Recommendation auto-trigger engine。

设计要点(吸收第 6 轮 rubber-duck 反馈):

1. **单线程消费 + 队列**:watcher 回调只把价格/事件 enqueue,engine 单 worker thread
   消费,杜绝多 watcher 线程共享内存索引导致的 race。
2. **fail-closed atomic claim**:`claim_auto_plan_for_execution()` 在一条 SQL 内
   同时校验 status / auto_execute_enabled / parse_status / state / expires_at,
   关 auto 之后即便内存索引未 refresh 也无法触发。
3. **frozen execution payload**:enable_auto_execute 时在 trigger_spec.execution_payload
   冻结 market_id/token_id/limit_price/size_shares/order_type,fire 时不再 re-derive。
4. **health gate**:price source 60s 内无更新 → 暂停所有 dwell 累计 + 拒绝 immediate。
5. **dwell 仅内存,重启后保守重置**:不持久化 dwell_started_at,进程重启后从 0 开始
   累计(更保守,不会因为旧脏数据立即触发);auto_executor_state(fired/expired)持久化。
6. **限速**:进程内 token bucket(全局每分钟 N 笔)。多实例靠 PG advisory lock 保证只有
   一个 executor 在跑(acquire_singleton_lock)。
7. **共享 preflight**:fire 前调用 `app._build_execution_preflight` + 绑定校验,与
   人工路径完全一致(余额 / 单标的上限 / correlation cap / 持仓数量)。

v1 显式不支持(留给 v2):
  - poly_bid/poly_ask 类型(parser 能识别但 engine 暂不订阅 PolymarketAssetPriceWatcher)
  - cancel 类(状态机不同,需要专门实现)
  - composite trigger
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from services.recommendation_trigger import auto_trigger_db as atdb
from services.recommendation_trigger.parser import (
    PARSE_STATUS_PARSED,
    ParsedTrigger,
    TRIGGER_TYPE_BTC_PRICE,
    TRIGGER_TYPE_IMMEDIATE,
    TRIGGER_TYPE_POLY_ASK,
    TRIGGER_TYPE_POLY_BID,
)

logger = logging.getLogger(__name__)


# ---------------------- 全局开关 ----------------------

def _killswitch_enabled() -> bool:
    return os.getenv("AUTO_EXECUTE_KILLSWITCH", "0").strip() == "1"


# ---------------------- 数据结构 ----------------------

@dataclass
class _DwellState:
    in_window_since: Optional[float] = None  # epoch seconds


@dataclass
class _PlanEntry:
    plan_id: int
    item_id: int
    action_type: str
    trigger: ParsedTrigger
    frozen_payload: dict[str, Any]
    semantic_key: str = ""  # 用于 refresh 时检测 enable 重置(同 semantic_key 复用 dwell)
    dwell: _DwellState = field(default_factory=_DwellState)


# 旧名兼容(只用作类型别名,不再创建实例)
_ItemEntry = _PlanEntry


@dataclass
class _PriceEvent:
    source: str        # "btc"
    price: float
    received_at: float


# ---------------------- 限速桶 ----------------------

class _TokenBucket:
    """简单的进程内速率限制:每 window_seconds 最多 capacity 次。
    不是 PG-backed,部署多实例需配合 advisory lock。
    """

    def __init__(self, capacity: int, window_seconds: int = 60) -> None:
        self.capacity = capacity
        self.window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def try_consume(self) -> bool:
        now = time.time()
        with self._lock:
            while self._timestamps and now - self._timestamps[0] > self.window:
                self._timestamps.popleft()
            if len(self._timestamps) >= self.capacity:
                return False
            self._timestamps.append(now)
            return True


# ---------------------- 引擎 ----------------------

class TriggerEngine:
    """单线程 worker + 队列消费 price events。

    用法:
      engine = TriggerEngine(execute_callback=..., refresh_callback=atdb.list_active_auto_plans, ...)
      engine.start()
      # 在 watcher callback 里:
      engine.enqueue_btc_price(price, received_at)
      engine.stop()
    """

    QUEUE_MAXSIZE = 4096
    REFRESH_INTERVAL_SECONDS = 30
    SOURCE_STALE_SECONDS = 60  # 价格源超过此秒数无更新视为不健康

    def __init__(
        self,
        *,
        execute_fn: Callable[["_PlanEntry", dict[str, Any]], None],
        rate_capacity_per_minute: int = 6,
    ) -> None:
        self._execute_fn = execute_fn
        self._rate_bucket = _TokenBucket(capacity=rate_capacity_per_minute, window_seconds=60)
        self._queue: queue.Queue[_PriceEvent] = queue.Queue(maxsize=self.QUEUE_MAXSIZE)
        self._plans: dict[int, _PlanEntry] = {}
        self._items_lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._refresher: Optional[threading.Thread] = None
        self._last_btc_update_at: float = 0.0
        self._fire_count = 0
        self._skip_count = 0
        self._last_refresh_at: float = 0.0

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._worker:
            return
        self._stop_evt.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="trigger-engine-worker", daemon=True)
        self._worker.start()
        self._refresher = threading.Thread(target=self._refresh_loop, name="trigger-engine-refresh", daemon=True)
        self._refresher.start()
        logger.info("TriggerEngine started")

    def stop(self) -> None:
        self._stop_evt.set()
        try:
            self._queue.put_nowait(_PriceEvent(source="__stop__", price=0.0, received_at=0.0))
        except queue.Full:
            pass
        if self._worker:
            self._worker.join(timeout=5)
        if self._refresher:
            self._refresher.join(timeout=5)
        logger.info("TriggerEngine stopped (fired=%d skipped=%d)", self._fire_count, self._skip_count)

    # ---------- 外部接口(线程安全) ----------

    def enqueue_btc_price(self, price: float, received_at: Optional[float] = None) -> None:
        try:
            evt = _PriceEvent(source="btc", price=float(price), received_at=received_at or time.time())
        except (TypeError, ValueError):
            return
        try:
            self._queue.put_nowait(evt)
        except queue.Full:
            logger.warning("TriggerEngine queue full,丢弃一个 BTC 价格事件 (price=%s)", price)

    def stats(self) -> dict[str, Any]:
        with self._items_lock:
            tracked = len(self._plans)
        return {
            "tracked": tracked,
            "fired": self._fire_count,
            "skipped": self._skip_count,
            "queue_size": self._queue.qsize(),
            "last_btc_update_at": self._last_btc_update_at,
            "last_refresh_at": self._last_refresh_at,
            "killswitch": _killswitch_enabled(),
        }

    # ---------- 内部:刷新循环 ----------

    def _refresh_loop(self) -> None:
        # 第一次立即刷,再按 interval 循环
        while not self._stop_evt.is_set():
            try:
                self._refresh_from_db()
            except Exception:  # noqa: BLE001
                logger.exception("TriggerEngine refresh 异常")
            self._stop_evt.wait(self.REFRESH_INTERVAL_SECONDS)

    def _refresh_from_db(self) -> None:
        # 过期清理
        try:
            n_expired = atdb.expire_overdue_plans()
            if n_expired:
                logger.info("auto trigger 过期清理(plans): %d 条", n_expired)
        except Exception:  # noqa: BLE001
            logger.exception("expire_overdue_plans 失败")

        rows = atdb.list_active_auto_plans()
        new_plans: dict[int, _PlanEntry] = {}
        for row in rows:
            try:
                entry = self._build_entry(row)
            except Exception as exc:  # noqa: BLE001
                logger.warning("跳过 plan %s: %s", row.get("plan_id"), exc)
                continue
            if entry is None:
                continue
            new_plans[entry.plan_id] = entry

        with self._items_lock:
            # 仅当 semantic_key 一致时复用旧的 dwell;否则重新累计
            for pid, entry in new_plans.items():
                old = self._plans.get(pid)
                if old is not None and old.semantic_key == entry.semantic_key and entry.semantic_key:
                    entry.dwell = old.dwell
            self._plans = new_plans
        self._last_refresh_at = time.time()
        logger.debug("TriggerEngine refreshed: tracked=%d", len(new_plans))

    def _build_entry(self, row: dict[str, Any]) -> Optional[_PlanEntry]:
        spec = row.get("trigger_spec") or {}
        if not isinstance(spec, dict):
            return None
        execution_payload = row.get("armed_execution_payload")
        if not isinstance(execution_payload, dict) or not execution_payload:
            logger.warning("plan %s 缺少 armed_execution_payload,跳过", row.get("plan_id"))
            return None
        ttype = spec.get("type") or (TRIGGER_TYPE_IMMEDIATE if spec.get("immediate") else None)
        if ttype not in {TRIGGER_TYPE_BTC_PRICE, TRIGGER_TYPE_IMMEDIATE}:
            if ttype in {TRIGGER_TYPE_POLY_BID, TRIGGER_TYPE_POLY_ASK}:
                logger.info("plan %s 是 poly 类触发,v1 引擎暂不支持,跳过", row.get("plan_id"))
            return None
        operator = spec.get("operator") or "=="
        try:
            value = float(spec.get("value") or 0.0)
        except (TypeError, ValueError):
            return None
        expires_at_raw = row.get("expires_at") or spec.get("expires_at")
        expires_at: Optional[datetime] = None
        if expires_at_raw:
            try:
                if isinstance(expires_at_raw, datetime):
                    expires_at = expires_at_raw
                else:
                    expires_at = datetime.fromisoformat(str(expires_at_raw).replace("Z", "+00:00"))
            except ValueError:
                pass
        trigger = ParsedTrigger(
            type=ttype,
            operator=operator,
            value=value,
            asset_token_id=spec.get("asset_token_id"),
            expires_at=expires_at,
            min_dwell_seconds=int(spec.get("min_dwell_seconds") or 5),
            cooldown_seconds=int(spec.get("cooldown_seconds") or 30),
            max_fires=int(spec.get("max_fires") or 1),
            source=str(spec.get("source") or "ai"),
            raw=spec,
        )
        action = str(row.get("action_type") or "").strip().lower()
        if action not in {"buy", "sell"}:
            return None
        return _PlanEntry(
            plan_id=int(row["plan_id"]),
            item_id=int(row["item_id"]),
            action_type=action,
            trigger=trigger,
            frozen_payload=execution_payload,
            semantic_key=str(row.get("semantic_key") or ""),
        )

    # ---------- 内部:消费循环 ----------

    def _worker_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                evt = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if evt.source == "__stop__":
                break
            if evt.source == "btc":
                # 价格源中断保护:若上一次 tick 距今超过 SOURCE_STALE_SECONDS,
                # 在更新 _last_btc_update_at 前把所有正在累计 dwell 的 entry 重置,
                # 避免离线期间被算进 dwell 而触发误 fire。
                if (
                    self._last_btc_update_at > 0
                    and (evt.received_at - self._last_btc_update_at) > self.SOURCE_STALE_SECONDS
                ):
                    self._reset_all_dwell(reason="btc source stale")
                self._last_btc_update_at = evt.received_at
                self._handle_btc_price(evt.price, evt.received_at)

    def _reset_all_dwell(self, *, reason: str) -> None:
        with self._items_lock:
            for entry in self._plans.values():
                if entry.dwell.in_window_since is not None:
                    entry.dwell.in_window_since = None
        logger.warning("已重置所有 dwell: %s", reason)

    def _is_btc_source_healthy(self, now: float) -> bool:
        if self._last_btc_update_at <= 0:
            return False
        return (now - self._last_btc_update_at) <= self.SOURCE_STALE_SECONDS

    def _handle_btc_price(self, price: float, now: float) -> None:
        if _killswitch_enabled():
            return
        with self._items_lock:
            entries = [e for e in self._plans.values() if e.trigger.type == TRIGGER_TYPE_BTC_PRICE]
            immediate = [e for e in self._plans.values() if e.trigger.type == TRIGGER_TYPE_IMMEDIATE]

        for entry in immediate:
            self._try_fire(entry, reason="immediate", price=price)

        for entry in entries:
            hit = self._evaluate(price, entry.trigger.operator, entry.trigger.value)
            dwell = entry.dwell
            if hit:
                if dwell.in_window_since is None:
                    dwell.in_window_since = now
                elapsed = now - dwell.in_window_since
                if elapsed >= entry.trigger.min_dwell_seconds:
                    self._try_fire(
                        entry,
                        reason=f"btc {entry.trigger.operator} {entry.trigger.value} dwell={elapsed:.1f}s",
                        price=price,
                    )
                    dwell.in_window_since = None
            else:
                if dwell.in_window_since is not None:
                    dwell.in_window_since = None

    @staticmethod
    def _evaluate(price: float, operator: str, threshold: float) -> bool:
        if operator == ">=":
            return price >= threshold
        if operator == ">":
            return price > threshold
        if operator == "<=":
            return price <= threshold
        if operator == "<":
            return price < threshold
        return False

    def _try_fire(self, entry: _PlanEntry, *, reason: str, price: float) -> None:
        now = time.time()
        if entry.trigger.type == TRIGGER_TYPE_BTC_PRICE and not self._is_btc_source_healthy(now):
            self._skip_count += 1
            return
        if not self._rate_bucket.try_consume():
            self._skip_count += 1
            logger.warning("plan %s 被限速桶拒,延后", entry.plan_id)
            return
        try:
            self._execute_fn(entry, {"reason": reason, "price": price})
            self._fire_count += 1
        except Exception:  # noqa: BLE001
            logger.exception("execute_fn 异常 plan=%s,从内存索引移除避免每 tick 重试", entry.plan_id)
        finally:
            # 不论成功还是失败,都从内存索引剔除;若 plan 仍处于 armed,下一次 refresh(30s) 会重新加入。
            # 这避免了 immediate 类 plan 每 tick 都重试,以及 btc_price 类 plan 在执行 callback 抛错后无限重入。
            with self._items_lock:
                self._plans.pop(entry.plan_id, None)
