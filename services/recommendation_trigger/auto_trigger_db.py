"""自动触发执行所需的 DB 操作。

把 trigger 状态机相关的 SQL 集中在这里,避免污染 recommendation_db.py。
所有方法都强制单 SQL 原子条件检查,杜绝 TOCTOU。

执行单元为 plan(每个 recommendation_action_plans 行有自己的 status):
    proposed   — AI 生成,待用户启用
    armed      — 已 enable,等待 trigger 条件
    executing  — 正在下单
    fired      — 已下单(终态)
    expired    — TTL 过期(终态)
    disarmed   — 用户撤销 / fire 永久失败(终态)
    superseded — 被同 item 内更新的 plan 取代

任何 fire 路径都必须走 `claim_auto_plan_for_execution()`,它在一条 SQL 内同时校验:
  plan.status='armed' AND trigger_parse_status='parsed'
  AND (expires_at IS NULL OR expires_at > NOW())
然后把 plan.status 推到 'executing'。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg2.extras import Json, RealDictCursor

from data.database import get_conn
from config import PG_DSN
import psycopg2 as _psycopg2

logger = logging.getLogger(__name__)


# 历史 item 维度状态(只保留常量, 兼容 recommendation_items.auto_executor_state 列):
AUTO_STATE_ARMED = "armed"
AUTO_STATE_IN_WINDOW = "in_window"
AUTO_STATE_FIRED = "fired"
AUTO_STATE_EXPIRED = "expired"
AUTO_STATE_DISARMED = "disarmed"
AUTO_STATE_RATE_LIMITED = "rate_limited"


class AutoTriggerClaimError(Exception):
    """fire 路径上的原子占用失败。code 用于审计。"""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


# ============================================================================
# 阶段 4 plan 维度的状态机:每个 recommendation_action_plans 行有自己的 status
# (proposed/armed/executing/fired/expired/disarmed/superseded)。
# enable 把 proposed→armed 并冻结 armed_execution_payload;disable 把
# proposed/armed→disarmed;claim 在单 SQL 内 armed→executing;fire 完成后
# executor 调 mark_plan_fired/mark_plan_failed。
# ============================================================================

PLAN_STATUS_PROPOSED = "proposed"
PLAN_STATUS_ARMED = "armed"
PLAN_STATUS_EXECUTING = "executing"
PLAN_STATUS_FIRED = "fired"
PLAN_STATUS_EXPIRED = "expired"
PLAN_STATUS_DISARMED = "disarmed"
PLAN_STATUS_SUPERSEDED = "superseded"

_PLAN_FIREABLE_STATES = (PLAN_STATUS_ARMED,)


def promote_plan_to_immediate(*, plan_id: int) -> bool:
    """把无 trigger / unparseable 的 proposed plan 升级为 immediate trigger,
    使其满足 enable_auto_execute_plan 的 parse_status='parsed' 校验,从而能进入 armed→engine 的
    immediate 批次,在下一个 tick 立即下单。

    仅对 proposed 状态生效;armed 之后再升级会与已有冻结 payload 的语义冲突,所以禁止。
    """
    immediate_spec = {
        "type": "immediate",
        "source": "user_promote",
        "max_fires": 1,
        "cooldown_seconds": 0,
        "min_dwell_seconds": 0,
        "asset_token_id": None,
        "expires_at": None,
    }
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE recommendation_action_plans
               SET trigger_spec = %s,
                   trigger_parse_status = 'parsed',
                   trigger_summary = '立即可执行',
                   updated_at = NOW()
             WHERE id = %s AND status = 'proposed'
            """,
            (Json(immediate_spec), int(plan_id)),
        )
        ok = (cur.rowcount or 0) > 0
        conn.commit()
        return ok


def enable_auto_execute_plan(
    *,
    plan_id: int,
    frozen_payload: dict[str, Any],
    operator_label: str,
) -> dict[str, Any]:
    """proposed→armed,冻结 armed_execution_payload。
    校验:item.status='approved' AND plan.status='proposed' AND parse_status='parsed' AND not expired。
    """
    if not isinstance(frozen_payload, dict) or not frozen_payload:
        raise ValueError("frozen_payload 不能为空")
    enriched = dict(frozen_payload)
    enriched.setdefault("frozen_at", datetime.now(timezone.utc).isoformat())
    enriched.setdefault("frozen_by", (operator_label or "dashboard").strip()[:64])

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            UPDATE recommendation_action_plans p
               SET status = %s,
                   armed_execution_payload = %s,
                   updated_at = NOW()
              FROM recommendation_items i
             WHERE p.id = %s
               AND p.item_id = i.id
               AND i.status = 'approved'
               AND p.status = %s
               AND p.trigger_parse_status = 'parsed'
               AND (p.expires_at IS NULL OR p.expires_at > NOW())
            RETURNING p.id, p.item_id, p.action_type, p.status, p.trigger_spec
            """,
            (
                PLAN_STATUS_ARMED,
                Json(enriched),
                int(plan_id),
                PLAN_STATUS_PROPOSED,
            ),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise AutoTriggerClaimError(
                f"plan {plan_id} 无法启用(item 未批准 / plan 状态非 proposed / 未解析 / 已过期)",
                code="enable_blocked",
            )
        conn.commit()
        return dict(row)


def disable_auto_execute_plan(
    *,
    plan_id: int,
    reason: str,
) -> dict[str, Any]:
    """proposed/armed→disarmed(终态)。fired/expired/superseded 不再回退。"""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            UPDATE recommendation_action_plans
               SET status = %s,
                   armed_execution_payload = COALESCE(armed_execution_payload, '{}'::jsonb)
                       || %s::jsonb,
                   updated_at = NOW()
             WHERE id = %s
               AND status IN (%s, %s)
            RETURNING id, item_id, status
            """,
            (
                PLAN_STATUS_DISARMED,
                Json({"disarmed_reason": (reason or "")[:200]}),
                int(plan_id),
                PLAN_STATUS_PROPOSED,
                PLAN_STATUS_ARMED,
            ),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise AutoTriggerClaimError(
                f"plan {plan_id} 不存在或已是终态",
                code="disable_blocked",
            )
        conn.commit()
        return dict(row)


def cascade_cancel_plans_for_item(*, item_id: int, reason: str) -> int:
    """item 被拒绝/延后/已撤销时,把所有非终态 plan 一并 disarmed。返回受影响行数。
    调用方应在同一事务里调用(此函数自带 commit,所以仅做最终步)。
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE recommendation_action_plans
               SET status = %s,
                   armed_execution_payload = COALESCE(armed_execution_payload, '{}'::jsonb)
                       || %s::jsonb,
                   updated_at = NOW()
             WHERE item_id = %s
               AND status IN (%s, %s)
            """,
            (
                PLAN_STATUS_DISARMED,
                Json({"disarmed_reason": f"cascade:{(reason or '')[:180]}"}),
                int(item_id),
                PLAN_STATUS_PROPOSED,
                PLAN_STATUS_ARMED,
            ),
        )
        n = cur.rowcount or 0
        conn.commit()
        return n


def claim_auto_plan_for_execution(*, plan_id: int) -> dict[str, Any]:
    """auto fire 路径的原子占用:armed→executing。
    单 SQL 同时校验:plan.status='armed' AND parse='parsed' AND not expired
    AND action_type IN buy/sell AND item 仍处于"自动可执行"状态。

    注意: item.status 不能再硬要 'approved' —— 因为同一 item 内一个 plan fired 之后
    `record_action()` 会把 item.status 推到 order_submitted/order_failed,
    若硬要 approved 则后续 plan 全部失活,Phase 4 的 1:N 语义被破坏。
    所以允许 item 处于 approved 或下单尝试后的非终结态;反馈拒绝/暂缓的 plan 走 cascade_cancel,
    won/lost/expired 等市场终态显式排除。
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            UPDATE recommendation_action_plans p
               SET status = %s,
                   updated_at = NOW()
              FROM recommendation_items i
             WHERE p.id = %s
               AND p.item_id = i.id
               AND i.status IN ('approved','order_submitted','order_failed','cancel_submitted','cancel_failed')
               AND p.status = %s
               AND p.trigger_parse_status = 'parsed'
               AND p.action_type IN ('buy','sell')
               AND (p.expires_at IS NULL OR p.expires_at > NOW())
            RETURNING p.id AS plan_id, p.item_id, p.action_type, p.armed_execution_payload,
                      p.trigger_spec, p.semantic_key, i.title, i.subject_key, i.item_kind
            """,
            (PLAN_STATUS_EXECUTING, int(plan_id), PLAN_STATUS_ARMED),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            cur.execute(
                """
                SELECT p.status AS plan_status, p.trigger_parse_status,
                       p.action_type, p.expires_at,
                       i.status AS item_status
                  FROM recommendation_action_plans p
             LEFT JOIN recommendation_items i ON i.id = p.item_id
                 WHERE p.id = %s
                """,
                (int(plan_id),),
            )
            probe = cur.fetchone()
            if not probe:
                raise AutoTriggerClaimError(f"plan {plan_id} 不存在", code="plan_not_found")
            raise AutoTriggerClaimError(
                f"auto plan claim 拒绝: plan_status={probe['plan_status']} "
                f"item_status={probe['item_status']} parse={probe['trigger_parse_status']} "
                f"action={probe['action_type']} expires={probe['expires_at']}",
                code="auto_plan_claim_blocked",
            )
        conn.commit()
        return dict(row)


def mark_plan_fired(*, plan_id: int, order_id: str | None) -> bool:
    """executor 成功完成下单后调用:executing→fired。"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE recommendation_action_plans
               SET status = %s, fired_at = NOW(), fired_order_id = %s, updated_at = NOW()
             WHERE id = %s AND status = %s
            """,
            (PLAN_STATUS_FIRED, order_id, int(plan_id), PLAN_STATUS_EXECUTING),
        )
        ok = (cur.rowcount or 0) > 0
        conn.commit()
        return ok


def mark_plan_failed(*, plan_id: int, reason: str) -> bool:
    """executor 永久失败时调用:executing→disarmed。"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE recommendation_action_plans
               SET status = %s,
                   armed_execution_payload = COALESCE(armed_execution_payload, '{}'::jsonb)
                       || %s::jsonb,
                   updated_at = NOW()
             WHERE id = %s AND status = %s
            """,
            (
                PLAN_STATUS_DISARMED,
                Json({"failed_reason": (reason or "")[:200]}),
                int(plan_id),
                PLAN_STATUS_EXECUTING,
            ),
        )
        ok = (cur.rowcount or 0) > 0
        conn.commit()
        return ok


def list_active_auto_plans() -> list[dict[str, Any]]:
    """polling refresh:所有当前应被 watcher 监控的 armed plan(只挑能 fire 的)。"""
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT p.id AS plan_id, p.item_id, p.action_type, p.trigger_spec,
                   p.trigger_parse_status, p.armed_execution_payload, p.expires_at,
                   p.semantic_key,
                   i.title, i.subject_key, i.item_kind,
                   i.suggested_price_low_cents, i.suggested_price_high_cents
              FROM recommendation_action_plans p
              JOIN recommendation_items i ON i.id = p.item_id
             WHERE p.status = %s
               AND p.trigger_parse_status = 'parsed'
               AND i.status IN ('approved','order_submitted','order_failed','cancel_submitted','cancel_failed')
               AND (p.expires_at IS NULL OR p.expires_at > NOW())
             ORDER BY p.id
            """,
            (PLAN_STATUS_ARMED,),
        )
        return [dict(r) for r in cur.fetchall()]


def expire_overdue_plans() -> int:
    """polling 把过期 armed/proposed plan 推到 expired。"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE recommendation_action_plans
               SET status = %s, updated_at = NOW()
             WHERE status IN (%s, %s)
               AND expires_at IS NOT NULL
               AND expires_at <= NOW()
            """,
            (PLAN_STATUS_EXPIRED, PLAN_STATUS_PROPOSED, PLAN_STATUS_ARMED),
        )
        n = cur.rowcount or 0
        conn.commit()
        return n


def list_stale_executing_plans(*, timeout_minutes: int = 15) -> list[dict[str, Any]]:
    """返回卡在 executing 超过 timeout_minutes 的 plan,用于 watchdog 巡检。
    通常发生在 record_action 与 mark_plan_fired 之间进程崩溃。
    走 idx_rap_executing_watchdog 索引。
    """
    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            """
            SELECT p.id AS plan_id, p.item_id, p.action_type, p.armed_execution_payload,
                   p.updated_at, p.semantic_key, i.title, i.subject_key,
                   EXTRACT(EPOCH FROM (NOW() - p.updated_at)) AS stale_seconds,
                   (SELECT a.order_id
                      FROM recommendation_actions a
                     WHERE a.plan_id = p.id AND a.status = 'submitted'
                     ORDER BY a.id DESC LIMIT 1) AS submitted_order_id
              FROM recommendation_action_plans p
              JOIN recommendation_items i ON i.id = p.item_id
             WHERE p.status = %s
               AND p.updated_at <= NOW() - (%s || ' minutes')::interval
             ORDER BY p.updated_at ASC
            """,
            (PLAN_STATUS_EXECUTING, str(int(timeout_minutes))),
        )
        out = []
        for r in cur.fetchall():
            d = dict(r)
            try:
                d["stale_seconds"] = float(d.get("stale_seconds") or 0.0)
            except Exception:  # noqa: BLE001
                d["stale_seconds"] = 0.0
            out.append(d)
        return out


def repair_stale_executing_plan(
    *,
    plan_id: int,
    target_status: str,
    reason: str,
    released_by: str,
    order_id: str | None = None,
) -> dict[str, Any]:
    """把卡在 executing 的 plan 释放到 fired/disarmed/armed。仅允许 executing→指定终态。
    - target_status='fired': 必须传 order_id(代表已确认订单挂出)
    - target_status='disarmed': 代表确认未挂单或永久放弃
    - target_status='armed': 代表知道 claim 之后没有任何下单动作,可重新尝试
    返回更新后的 plan 行;失败抛 AutoTriggerClaimError。
    """
    target = str(target_status or "").strip().lower()
    if target not in {PLAN_STATUS_FIRED, PLAN_STATUS_DISARMED, PLAN_STATUS_ARMED}:
        raise ValueError(f"target_status 非法: {target}")
    if target == PLAN_STATUS_FIRED and not (order_id and str(order_id).strip()):
        raise ValueError("repair→fired 必须提供 order_id")

    audit = {
        "repaired_at": datetime.now(timezone.utc).isoformat(),
        "repaired_by": (released_by or "watchdog").strip()[:64],
        "repair_reason": (reason or "")[:200],
        "repair_from": PLAN_STATUS_EXECUTING,
        "repair_to": target,
    }
    if order_id:
        audit["repair_order_id"] = str(order_id).strip()

    with get_conn() as conn:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if target == PLAN_STATUS_FIRED:
            cur.execute(
                """
                UPDATE recommendation_action_plans
                   SET status = %s,
                       fired_at = COALESCE(fired_at, NOW()),
                       fired_order_id = COALESCE(fired_order_id, %s),
                       armed_execution_payload = COALESCE(armed_execution_payload, '{}'::jsonb)
                           || %s::jsonb,
                       updated_at = NOW()
                 WHERE id = %s AND status = %s
                RETURNING id, item_id, status, fired_order_id, fired_at
                """,
                (PLAN_STATUS_FIRED, str(order_id).strip(), Json(audit), int(plan_id), PLAN_STATUS_EXECUTING),
            )
        else:
            cur.execute(
                """
                UPDATE recommendation_action_plans
                   SET status = %s,
                       armed_execution_payload = COALESCE(armed_execution_payload, '{}'::jsonb)
                           || %s::jsonb,
                       updated_at = NOW()
                 WHERE id = %s AND status = %s
                RETURNING id, item_id, status
                """,
                (target, Json(audit), int(plan_id), PLAN_STATUS_EXECUTING),
            )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            raise AutoTriggerClaimError(
                f"plan {plan_id} 不在 executing 状态,无需修复",
                code="plan_not_executing",
            )
        conn.commit()
        return dict(row)


def auto_repair_stale_executing_plans(*, timeout_minutes: int = 15) -> dict[str, int]:
    """启动时自动巡检:扫所有 stale executing plan,
    根据 recommendation_actions 里有无 plan_id+submitted+order_id 决定补到 fired 或 disarmed。
    返回 {scanned, fired, disarmed, skipped}。
    """
    rows = list_stale_executing_plans(timeout_minutes=timeout_minutes)
    fired = disarmed = skipped = 0
    for r in rows:
        plan_id = int(r["plan_id"])
        order_id = r.get("submitted_order_id")
        try:
            if order_id:
                repair_stale_executing_plan(
                    plan_id=plan_id, target_status=PLAN_STATUS_FIRED,
                    reason="auto-repair: found submitted action with order_id",
                    released_by="auto-repair", order_id=str(order_id),
                )
                fired += 1
            else:
                repair_stale_executing_plan(
                    plan_id=plan_id, target_status=PLAN_STATUS_DISARMED,
                    reason="auto-repair: no submitted order action found",
                    released_by="auto-repair",
                )
                disarmed += 1
        except Exception:  # noqa: BLE001
            skipped += 1
    return {"scanned": len(rows), "fired": fired, "disarmed": disarmed, "skipped": skipped}


def acquire_singleton_lock(*, lock_key: int = 0x4131_5503) -> bool:
    """PG advisory lock,确保整个集群只跑一个 auto_executor 实例。
    返回 True 表示拿到锁,False 表示已有实例在跑。

    实现细节: 必须用一个**独立的、不归还连接池**的连接持有锁。
    之前用 `get_conn().__enter__()` 取连接,但 contextmanager 在 GC 时会 putconn 归还,
    锁连接被池复用后 advisory lock 语义就失效了。改为直接 psycopg2.connect 拿一个
    专属连接,断开自动释放锁。
    """
    try:
        conn = _psycopg2.connect(PG_DSN)
    except Exception:  # noqa: BLE001
        return False
    conn.autocommit = True
    cur = conn.cursor()
    try:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
        got = bool(cur.fetchone()[0])
    except Exception:  # noqa: BLE001
        got = False
    if not got:
        try:
            cur.close()
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        return False
    _SINGLETON_HOLDER["conn"] = conn
    _SINGLETON_HOLDER["cursor"] = cur
    return True


_SINGLETON_HOLDER: dict[str, Any] = {}


def release_singleton_lock() -> None:
    cur = _SINGLETON_HOLDER.pop("cursor", None)
    conn = _SINGLETON_HOLDER.pop("conn", None)
    if cur is not None:
        try:
            cur.close()
        except Exception:  # noqa: BLE001
            pass
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
