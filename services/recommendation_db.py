"""
月度建议系统的 DB 持久化。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any

from psycopg2.extras import Json, execute_values

from data.database import get_conn, get_cursor

logger = logging.getLogger(__name__)

_FEEDBACK_DECISION_TO_STATUS = {
    "execute": "approved",
    "reject": "rejected",
    "defer": "deferred",
    "read": "read",
}

# 已经进入"已下单 / 已撤单 / 结算结果"等终态的 item，不允许再被反馈/执行覆盖
_TERMINAL_ITEM_STATUSES = {
    "order_submitted",
    "cancel_submitted",
    "won",
    "lost",
    "expired",
}

# 允许从 execution gate 真实下单/撤单的 item 状态（用于"原子占用"前的合法源状态集合）
_EXECUTABLE_ITEM_STATUSES = {"approved", "order_failed", "cancel_failed"}

# Outcome 只允许对真实成功提交过的 item 登记，避免把 order_failed 包装成 won
_OUTCOME_ELIGIBLE_STATUSES = {
    "order_submitted",
    "cancel_submitted",
    "won",
    "lost",
    "expired",
    "breakeven",
}


class RecommendationGateError(Exception):
    """Execution gate 校验失败时抛出，调用方应作为 409 返回。"""

    def __init__(self, message: str, *, code: str = "gate_violation") -> None:
        super().__init__(message)
        self.code = code

_DDL_RECOMMENDATION_RUNS = """
CREATE TABLE IF NOT EXISTS recommendation_runs (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    asset TEXT NOT NULL DEFAULT 'btc',
    analysis_kind TEXT NOT NULL DEFAULT 'position_analyze',
    profile TEXT NOT NULL DEFAULT 'analyze',
    status TEXT NOT NULL DEFAULT 'completed',
    trigger_type TEXT NOT NULL DEFAULT 'scheduled',
    trigger_reason TEXT,
    operator_intent TEXT,
    model_id TEXT,
    prompt_family TEXT,
    prompt_version TEXT,
    system_prompt_hash TEXT,
    schema_hash TEXT,
    btc_price DOUBLE PRECISION,
    days_left_in_month DOUBLE PRECISION,
    recommendation_count INTEGER NOT NULL DEFAULT 0,
    input_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    analysis_output JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary_text TEXT
);
"""

_DDL_RECOMMENDATION_RUNS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_recommendation_runs_created_at ON recommendation_runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_recommendation_runs_asset ON recommendation_runs(asset);
CREATE INDEX IF NOT EXISTS idx_recommendation_runs_trigger_type ON recommendation_runs(trigger_type);
"""

_DDL_RECOMMENDATION_ITEMS = """
CREATE TABLE IF NOT EXISTS recommendation_items (
    id SERIAL PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source_section TEXT NOT NULL,
    item_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    action_type TEXT NOT NULL,
    direction TEXT,
    strategy_type TEXT,
    suggested_price_text TEXT,
    suggested_price_low_cents DOUBLE PRECISION,
    suggested_price_high_cents DOUBLE PRECISION,
    size_text TEXT,
    trigger_condition TEXT,
    reason TEXT,
    edge_text TEXT,
    confidence_text TEXT,
    correlation_group TEXT,
    priority_hint TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    executing_started_at TIMESTAMPTZ
);
ALTER TABLE recommendation_items ADD COLUMN IF NOT EXISTS executing_started_at TIMESTAMPTZ;
"""

_DDL_RECOMMENDATION_ITEMS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_recommendation_items_run_id ON recommendation_items(run_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_items_status ON recommendation_items(status);
CREATE INDEX IF NOT EXISTS idx_recommendation_items_subject_key ON recommendation_items(subject_key);
CREATE INDEX IF NOT EXISTS idx_recommendation_items_kind ON recommendation_items(item_kind);
"""

_DDL_RECOMMENDATION_FEEDBACK = """
CREATE TABLE IF NOT EXISTS recommendation_feedback (
    id SERIAL PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES recommendation_items(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    decision TEXT NOT NULL,
    reason_tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    feedback_text TEXT,
    allow_model_learning BOOLEAN NOT NULL DEFAULT TRUE,
    raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb
);
"""

_DDL_RECOMMENDATION_FEEDBACK_INDICES = """
CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_item_id ON recommendation_feedback(item_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_feedback_decision ON recommendation_feedback(decision);
"""

_DDL_RECOMMENDATION_ACTIONS = """
CREATE TABLE IF NOT EXISTS recommendation_actions (
    id SERIAL PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES recommendation_items(id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    action_type TEXT NOT NULL,
    status TEXT NOT NULL,
    order_id TEXT,
    request_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    response_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_text TEXT,
    triggered_by TEXT
);
ALTER TABLE recommendation_actions ADD COLUMN IF NOT EXISTS triggered_by TEXT;
"""

_DDL_RECOMMENDATION_ACTIONS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_recommendation_actions_item_id ON recommendation_actions(item_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_actions_status ON recommendation_actions(status);
CREATE INDEX IF NOT EXISTS idx_recommendation_actions_triggered_by ON recommendation_actions(triggered_by);
"""

# 阶段3 自动触发：trigger_spec / trigger_parse_status / auto_execute_enabled
_DDL_RECOMMENDATION_ITEMS_TRIGGER = """
ALTER TABLE recommendation_items ADD COLUMN IF NOT EXISTS trigger_spec JSONB;
ALTER TABLE recommendation_items ADD COLUMN IF NOT EXISTS trigger_parse_status TEXT;
ALTER TABLE recommendation_items ADD COLUMN IF NOT EXISTS auto_execute_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE recommendation_items ADD COLUMN IF NOT EXISTS auto_executor_state TEXT;
ALTER TABLE recommendation_items ADD COLUMN IF NOT EXISTS auto_executor_state_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_recommendation_items_auto_exec
    ON recommendation_items(status, auto_execute_enabled)
    WHERE status = 'approved' AND auto_execute_enabled = TRUE;
"""

_DDL_RECOMMENDATION_OUTCOMES = """
CREATE TABLE IF NOT EXISTS recommendation_outcomes (
    id SERIAL PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES recommendation_items(id) ON DELETE CASCADE,
    evaluated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    outcome_label TEXT,
    hit BOOLEAN,
    pnl DOUBLE PRECISION,
    notes TEXT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_by TEXT,
    supersedes_outcome_id INTEGER REFERENCES recommendation_outcomes(id) ON DELETE SET NULL,
    is_final BOOLEAN NOT NULL DEFAULT FALSE,
    revision_reason TEXT
);
ALTER TABLE recommendation_outcomes ADD COLUMN IF NOT EXISTS recorded_by TEXT;
ALTER TABLE recommendation_outcomes ADD COLUMN IF NOT EXISTS supersedes_outcome_id INTEGER REFERENCES recommendation_outcomes(id) ON DELETE SET NULL;
ALTER TABLE recommendation_outcomes ADD COLUMN IF NOT EXISTS is_final BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE recommendation_outcomes ADD COLUMN IF NOT EXISTS revision_reason TEXT;
"""

_DDL_RECOMMENDATION_OUTCOMES_INDICES = """
CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_item_id ON recommendation_outcomes(item_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_hit ON recommendation_outcomes(hit);
"""

# 阶段4：action_plans 表 — 把 item 的单 action 重构为 1:N 可执行计划
_DDL_RECOMMENDATION_ACTION_PLANS = """
CREATE TABLE IF NOT EXISTS recommendation_action_plans (
    id BIGSERIAL PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES recommendation_items(id) ON DELETE CASCADE,
    run_id INTEGER NOT NULL REFERENCES recommendation_runs(id) ON DELETE CASCADE,
    ordinal SMALLINT NOT NULL,
    semantic_key TEXT NOT NULL,
    action_type TEXT NOT NULL,
    trigger_spec JSONB NOT NULL,
    trigger_parse_status TEXT NOT NULL,
    trigger_summary TEXT,
    suggested_execution_payload JSONB,
    armed_execution_payload JSONB,
    status TEXT NOT NULL DEFAULT 'proposed',
    fired_at TIMESTAMPTZ,
    fired_order_id TEXT,
    expires_at TIMESTAMPTZ,
    reason_text TEXT,
    superseded_by BIGINT REFERENCES recommendation_action_plans(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (item_id, ordinal)
);
"""

_DDL_RECOMMENDATION_ACTION_PLANS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_rap_active ON recommendation_action_plans (status, expires_at, item_id)
    WHERE status IN ('proposed','armed') AND trigger_parse_status='parsed';
CREATE INDEX IF NOT EXISTS idx_rap_item_status ON recommendation_action_plans (item_id, status);
CREATE INDEX IF NOT EXISTS idx_rap_semantic_key ON recommendation_action_plans (semantic_key)
    WHERE status IN ('proposed','armed');
CREATE INDEX IF NOT EXISTS idx_rap_executing_watchdog ON recommendation_action_plans (status, updated_at)
    WHERE status='executing';
CREATE INDEX IF NOT EXISTS idx_rap_run_id ON recommendation_action_plans (run_id);
"""

# actions/outcomes 加 plan_id FK(向后兼容,允许 NULL)
_DDL_ACTIONS_OUTCOMES_PLAN_LINK = """
ALTER TABLE recommendation_actions
    ADD COLUMN IF NOT EXISTS plan_id BIGINT REFERENCES recommendation_action_plans(id) ON DELETE SET NULL;
ALTER TABLE recommendation_outcomes
    ADD COLUMN IF NOT EXISTS plan_id BIGINT REFERENCES recommendation_action_plans(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_recommendation_actions_plan_id ON recommendation_actions(plan_id);
CREATE INDEX IF NOT EXISTS idx_recommendation_outcomes_plan_id ON recommendation_outcomes(plan_id);
"""


_DDL_MODEL_CHANGE_PROPOSALS = """
CREATE TABLE IF NOT EXISTS model_change_proposals (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'proposed',
    proposal_type TEXT NOT NULL,
    target_scope TEXT NOT NULL DEFAULT 'monthly_recommendation',
    title TEXT NOT NULL,
    rationale TEXT NOT NULL,
    change_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    proposed_by TEXT NOT NULL DEFAULT 'agent',
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ,
    decision_notes TEXT
);
"""

_DDL_MODEL_CHANGE_PROPOSALS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_model_change_proposals_status ON model_change_proposals(status);
CREATE INDEX IF NOT EXISTS idx_model_change_proposals_scope ON model_change_proposals(target_scope);
"""

_DDL_MODEL_SHADOW_EVALS = """
CREATE TABLE IF NOT EXISTS model_shadow_evals (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    proposal_id INTEGER REFERENCES model_change_proposals(id) ON DELETE SET NULL,
    target_scope TEXT NOT NULL DEFAULT 'monthly_recommendation',
    baseline_version TEXT,
    candidate_version TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    notes TEXT
);
"""

_DDL_MODEL_SHADOW_EVALS_INDICES = """
CREATE INDEX IF NOT EXISTS idx_model_shadow_evals_proposal_id ON model_shadow_evals(proposal_id);
CREATE INDEX IF NOT EXISTS idx_model_shadow_evals_status ON model_shadow_evals(status);
"""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _summarize_trigger(spec: dict | None, parse_status: str) -> str | None:
    if not spec:
        return None
    t = spec.get("type")
    if t == "immediate":
        return "立即执行"
    op = spec.get("operator") or ""
    val = spec.get("value")
    if t == "btc_price_threshold":
        return f"BTC 1m close {op} {val}" if val is not None else None
    if t in ("poly_bid_threshold", "poly_ask_threshold"):
        side = "bid" if "bid" in (t or "") else "ask"
        return f"poly {side} {op} {val}"
    if parse_status == "unparseable":
        return None
    return f"{t} {op} {val}".strip()


def _extract_expires_at(spec: dict | None):
    """从 trigger_spec 提取 expires_at(只接受标准字段名)。
    阶段4 后续修复:统一只承认 `expires_at`,删除 `ttl` 别名 —— 历史上多处对 TTL 字段
    的语义不一致(prompt 写 TTL、parser 接大写 TTL、这里只接小写 ttl)会让 plan 在不同
    模块拿到不同的过期时间。统一后,AI 必须输出 `expires_at`(ISO8601);其它 key 一律忽略。
    """
    if not spec:
        return None
    raw = spec.get("expires_at")
    if not raw:
        return None
    try:
        from datetime import datetime
        if isinstance(raw, datetime):
            return raw
        s = str(raw).replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _normalize_text_key(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).lower()


def _strip_outcome_suffix(value: str) -> str:
    return re.sub(r"\s*\((yes|no)\)\s*$", "", (value or "").strip(), flags=re.IGNORECASE)


def _infer_correlation_group(question: str) -> str | None:
    lower = (question or "").strip().lower()
    if not lower:
        return None
    if "dip to" in lower or "below" in lower:
        return "btc_below"
    if "reach" in lower or "above" in lower:
        return "btc_above"
    return None


def _parse_price_range_cents(value: Any) -> tuple[float | None, float | None]:
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        cents = float(value)
        return cents, cents
    text = str(value)
    numbers = re.findall(r"\d+(?:\.\d+)?", text.replace(",", ""))
    if not numbers:
        return None, None
    parsed = [float(num) for num in numbers]
    return min(parsed), max(parsed)


def _extract_title_direction(title: str) -> tuple[str, str | None]:
    text = (title or "").strip()
    matched = re.search(r"\((Yes|No)\)\s*$", text, flags=re.IGNORECASE)
    if not matched:
        return text, None
    direction = matched.group(1).title()
    base_title = re.sub(r"\s*\((Yes|No)\)\s*$", "", text, flags=re.IGNORECASE)
    return base_title.strip(), direction


def build_recommendation_items(
    analysis_output: dict,
    profit_optimization_context: dict | None = None,
) -> list[dict]:
    """把 AI 结构化输出规范化为 recommendation_items。"""
    output = analysis_output if isinstance(analysis_output, dict) else {}
    profit_ctx = profit_optimization_context if isinstance(profit_optimization_context, dict) else {}

    edge_map: dict[str, dict] = {}
    for item in profit_ctx.get("top_edge_opportunities", []):
        if not isinstance(item, dict):
            continue
        key = _normalize_text_key(item.get("question") or "")
        if key:
            edge_map[key] = item

    swing_map: dict[str, dict] = {}
    for item in profit_ctx.get("swing_opportunities", []):
        if not isinstance(item, dict):
            continue
        key = _normalize_text_key(item.get("question") or "")
        if key:
            swing_map[key] = item

    position_map: dict[str, dict] = {}
    for item in profit_ctx.get("position_safety_assessment", []):
        if not isinstance(item, dict):
            continue
        base_title, _ = _extract_title_direction(str(item.get("title") or ""))
        key = _normalize_text_key(base_title)
        if key:
            position_map[key] = item

    items: list[dict] = []

    def _meta_for_question(question: str) -> tuple[str | None, str | None]:
        key = _normalize_text_key(question)
        edge_meta = edge_map.get(key)
        if edge_meta:
            return (
                str(edge_meta.get("calibration_confidence") or "").strip() or None,
                str(edge_meta.get("correlation_group") or "").strip() or None,
            )
        swing_meta = swing_map.get(key)
        if swing_meta:
            return (
                str(swing_meta.get("calibration_confidence") or "").strip() or None,
                _infer_correlation_group(question),
            )
        return None, _infer_correlation_group(question)

    # 注: 旧版"预警信号"item 用到的 btc_price_question_map / _resolve_warning_target 已随
    # "预警信号" schema 一并移除 (操作清单 已统一表达 BTC 触发位的操作)。

    _ACTION_MAP = {
        "买入": "buy",
        "卖出": "sell",
        "撤单": "cancel",
        "持有观察": "review",
    }
    for item in output.get("操作清单", []):
        if not isinstance(item, dict):
            continue
        op_text = str(item.get("操作") or "").strip()
        action_type = _ACTION_MAP.get(op_text, "review")
        if action_type == "review":
            continue
        title = str(item.get("标的") or "").strip() or op_text or "操作清单条目"
        base_title, fallback_direction = _extract_title_direction(title)
        direction = str(item.get("方向") or "").strip() or fallback_direction
        priority = str(item.get("优先级") or "").strip() or None
        confidence_text, correlation_group = _meta_for_question(base_title)
        low_cents, high_cents = _parse_price_range_cents(item.get("价格"))
        item_kind = "entry" if action_type == "buy" else ("position_order_cancel" if action_type == "cancel" else "exit")
        items.append({
            "source_section": "操作清单",
            "item_kind": item_kind,
            "title": title,
            "subject_key": _normalize_text_key(base_title),
            "action_type": action_type,
            "direction": direction,
            "strategy_type": str(item.get("策略类型") or "").strip() or None,
            "suggested_price_text": str(item.get("价格") or "").strip() or None,
            "suggested_price_low_cents": low_cents,
            "suggested_price_high_cents": high_cents,
            "size_text": str(item.get("金额或数量") or "").strip() or None,
            "trigger_condition": str(item.get("触发条件") or "").strip() or None,
            "reason": str(item.get("理由") or "").strip() or None,
            "edge_text": None,
            "confidence_text": confidence_text,
            "correlation_group": correlation_group,
            "priority_hint": priority,
            "raw_payload": item,
        })

    return items


class RecommendationDB:
    """recommendation_* 与 model_* 表的初始化与持久化。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def init_tables(self) -> None:
        with get_conn(autocommit=True) as conn:
            cur = conn.cursor()
            cur.execute(_DDL_RECOMMENDATION_RUNS)
            cur.execute(_DDL_RECOMMENDATION_RUNS_INDICES)
            cur.execute(_DDL_RECOMMENDATION_ITEMS)
            cur.execute(_DDL_RECOMMENDATION_ITEMS_INDICES)
            cur.execute(_DDL_RECOMMENDATION_FEEDBACK)
            cur.execute(_DDL_RECOMMENDATION_FEEDBACK_INDICES)
            cur.execute(_DDL_RECOMMENDATION_ACTIONS)
            cur.execute(_DDL_RECOMMENDATION_ACTIONS_INDICES)
            cur.execute(_DDL_RECOMMENDATION_ITEMS_TRIGGER)
            cur.execute(_DDL_RECOMMENDATION_OUTCOMES)
            cur.execute(_DDL_RECOMMENDATION_OUTCOMES_INDICES)
            # 阶段4：action_plans 表 + actions/outcomes 链接列
            cur.execute(_DDL_RECOMMENDATION_ACTION_PLANS)
            cur.execute(_DDL_RECOMMENDATION_ACTION_PLANS_INDICES)
            cur.execute(_DDL_ACTIONS_OUTCOMES_PLAN_LINK)
            cur.execute(_DDL_MODEL_CHANGE_PROPOSALS)
            cur.execute(_DDL_MODEL_CHANGE_PROPOSALS_INDICES)
            cur.execute(_DDL_MODEL_SHADOW_EVALS)
            cur.execute(_DDL_MODEL_SHADOW_EVALS_INDICES)

            # 第四轮加固 #2（中）：is_final 一次性 backfill。
            # ALTER TABLE 加列默认 FALSE，旧 outcome 全部不是 final，会让
            # build_memory_context 的 hit_rate 统计为 0、且 record_outcome 的"已有 final 必须修订"
            # 保护被绕过。这里用幂等迁移：对每个 item，把"最新一条 outcome"标记为 is_final=TRUE，
            # 仅当该 item 当前一条 final 都没有时执行（多次启动不会反复改写）。
            cur.execute(
                """
                WITH latest AS (
                    SELECT DISTINCT ON (item_id) id, item_id
                    FROM recommendation_outcomes
                    ORDER BY item_id, evaluated_at DESC, id DESC
                ),
                items_without_final AS (
                    SELECT item_id FROM recommendation_outcomes
                    GROUP BY item_id
                    HAVING SUM(CASE WHEN is_final IS TRUE THEN 1 ELSE 0 END) = 0
                )
                UPDATE recommendation_outcomes
                SET is_final = TRUE
                WHERE id IN (
                    SELECT l.id FROM latest l
                    JOIN items_without_final w ON w.item_id = l.item_id
                )
                """
            )
            backfilled = cur.rowcount or 0
            if backfilled > 0:
                logger.warning(
                    "recommendation_outcomes.is_final 回填: 把 %d 条最新历史 outcome 置为 is_final=TRUE",
                    backfilled,
                )

            # 第四轮加固 #6（低）：把历史 'rejected_terminal' 统一迁移到 'rejected'，
            # 否则它既不在 _TERMINAL_ITEM_STATUSES，也不在 _EXECUTABLE_ITEM_STATUSES，
            # 在 UI 上会变成"可再次反馈改写"的怪状态。
            cur.execute(
                """
                UPDATE recommendation_items
                SET status = 'rejected'
                WHERE status = 'rejected_terminal'
                """
            )
            migrated_rejected = cur.rowcount or 0
            if migrated_rejected > 0:
                logger.warning(
                    "recommendation_items: 把 %d 条历史 'rejected_terminal' 状态迁移到 'rejected'",
                    migrated_rejected,
                )

            logger.info("recommendation/model 数据表初始化完成")

    def persist_analysis_run(
        self,
        *,
        asset: str,
        analysis_kind: str,
        profile: str,
        trigger_type: str,
        trigger_reason: str | None,
        operator_intent: str | None,
        model_id: str | None,
        prompt_family: str | None,
        prompt_version: str | None,
        system_prompt_hash: str | None,
        schema_hash: str | None,
        btc_price: float | None,
        days_left_in_month: float | None,
        input_snapshot: dict,
        analysis_output: dict,
        items: list[dict],
        status: str = "completed",
    ) -> int:
        normalized_status = str(status or "completed").strip().lower()
        if normalized_status not in {"completed", "partial", "failed"}:
            raise ValueError(f"unsupported run status: {status}")
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO recommendation_runs (
                    asset, analysis_kind, profile, status, trigger_type, trigger_reason,
                    operator_intent, model_id, prompt_family, prompt_version,
                    system_prompt_hash, schema_hash, btc_price, days_left_in_month,
                    recommendation_count, input_snapshot, analysis_output, summary_text
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s, %s
                )
                RETURNING id
                """,
                (
                    asset,
                    analysis_kind,
                    profile,
                    normalized_status,
                    trigger_type,
                    trigger_reason,
                    operator_intent,
                    model_id,
                    prompt_family,
                    prompt_version,
                    system_prompt_hash,
                    schema_hash,
                    btc_price,
                    days_left_in_month,
                    len(items),
                    Json(input_snapshot, dumps=_json_dumps),
                    Json(analysis_output, dumps=_json_dumps),
                    str(analysis_output.get("整体分析") or "").strip() or None,
                ),
            )
            run_id = cur.fetchone()[0]

            if items:
                from services.recommendation_trigger.parser import (
                    parse_trigger,
                    PARSE_STATUS_PARSED,
                    PARSE_STATUS_UNPARSEABLE,
                )

                for item in items:
                    raw_payload = item.get("raw_payload") or {}
                    legacy_trigger_spec = raw_payload.get("trigger_spec")
                    item_action_type = item.get("action_type")
                    item_subject_key = item["subject_key"]

                    # 阶段4 之前的兼容:item 维度也跑 parser,继续填 trigger_spec/trigger_parse_status 列(只读用途)
                    parse_input = {
                        "action_type": item_action_type,
                        "item_kind": item.get("item_kind"),
                        "trigger_condition": item.get("trigger_condition"),
                        "trigger_spec": legacy_trigger_spec,
                    }
                    try:
                        parsed_item = parse_trigger(parse_input)
                        item_parse_status = parsed_item.status
                        item_spec_dict = parsed_item.trigger.to_jsonable() if parsed_item.trigger else None
                    except Exception:  # noqa: BLE001
                        logger.exception("trigger parse 异常 title=%s", item.get("title"))
                        item_parse_status = PARSE_STATUS_UNPARSEABLE
                        item_spec_dict = None

                    cur.execute(
                        """
                        INSERT INTO recommendation_items (
                            run_id, source_section, item_kind, title, subject_key, action_type,
                            direction, strategy_type, suggested_price_text, suggested_price_low_cents,
                            suggested_price_high_cents, size_text, trigger_condition, reason,
                            edge_text, confidence_text, correlation_group, priority_hint, raw_payload,
                            trigger_spec, trigger_parse_status
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, %s, %s, %s, %s,
                            %s, %s
                        ) RETURNING id
                        """,
                        (
                            run_id,
                            item["source_section"],
                            item["item_kind"],
                            item["title"],
                            item_subject_key,
                            item_action_type,
                            item.get("direction"),
                            item.get("strategy_type"),
                            item.get("suggested_price_text"),
                            item.get("suggested_price_low_cents"),
                            item.get("suggested_price_high_cents"),
                            item.get("size_text"),
                            item.get("trigger_condition"),
                            item.get("reason"),
                            item.get("edge_text"),
                            item.get("confidence_text"),
                            item.get("correlation_group"),
                            item.get("priority_hint"),
                            Json(raw_payload, dumps=_json_dumps),
                            Json(item_spec_dict, dumps=_json_dumps) if item_spec_dict else None,
                            item_parse_status,
                        ),
                    )
                    item_id = cur.fetchone()[0]

                    # 阶段4 主路径:写 action_plans。AI 输出在 raw_payload['action_plans'] 数组,
                    # 兼容旧路径:若 AI 没给 action_plans 但给了 trigger_spec + 顶层 action_type,
                    # 自动合成单条 plan。
                    raw_plans = raw_payload.get("action_plans")
                    if not isinstance(raw_plans, list) or not raw_plans:
                        if item_action_type in {"buy", "sell", "cancel"}:
                            raw_plans = [{
                                "action_type": item_action_type,
                                "side": item.get("direction"),
                                "price_cents": item.get("suggested_price_low_cents") or item.get("suggested_price_high_cents"),
                                "size_text": item.get("size_text"),
                                "target_order_id": raw_payload.get("目标挂单ID") or raw_payload.get("target_order_id"),
                                "trigger_spec": legacy_trigger_spec,
                                "reason": item.get("reason"),
                            }]
                        else:
                            raw_plans = []

                    for ordinal_zero, raw_plan in enumerate(raw_plans):
                        if not isinstance(raw_plan, dict):
                            continue
                        plan_action = str(raw_plan.get("action_type") or "").strip().lower()
                        if plan_action not in {"buy", "sell", "cancel"}:
                            logger.debug("跳过非法 action_plan action_type=%r item=%s", plan_action, item_id)
                            continue
                        plan_trigger_spec = raw_plan.get("trigger_spec") if isinstance(raw_plan.get("trigger_spec"), dict) else None
                        # item-level trigger_condition 仅在以下两种情况可作 fallback:
                        # ① 仅有 1 条 plan 且其 action_type 与 item.action_type 一致 (兼容旧路径,见上面 raw_plans 兜底);
                        # ② plan 自己显式带了 trigger_condition 字符串。
                        # 否则不要回退——多 plan 时 item 级文案只属于其中一条具体动作,套用到其他 plan 会产生
                        # 例如 "sell BTC>=77500" 这种与 buy plan 冲突的错误触发。
                        plan_trigger_condition = raw_plan.get("trigger_condition")
                        if not plan_trigger_condition and not plan_trigger_spec:
                            single_plan_match = (
                                len(raw_plans) == 1
                                and plan_action == str(item_action_type or "").lower()
                            )
                            if single_plan_match:
                                plan_trigger_condition = item.get("trigger_condition")
                        plan_parse_input = {
                            "action_type": plan_action,
                            "item_kind": item.get("item_kind"),
                            "trigger_condition": plan_trigger_condition,
                            "trigger_spec": plan_trigger_spec,
                        }
                        try:
                            plan_parsed = parse_trigger(plan_parse_input)
                            plan_parse_status = plan_parsed.status
                            plan_spec_dict = plan_parsed.trigger.to_jsonable() if plan_parsed.trigger else (plan_trigger_spec or {})
                        except Exception:  # noqa: BLE001
                            logger.exception("plan parse 异常 item=%s ordinal=%s", item_id, ordinal_zero)
                            plan_parse_status = PARSE_STATUS_UNPARSEABLE
                            plan_spec_dict = plan_trigger_spec or {}
                        # plan_spec_dict 始终是 dict(供 NOT NULL 列);unparseable 时为 raw 或空
                        if plan_spec_dict is None:
                            plan_spec_dict = {}

                        suggested_payload = self._build_suggested_payload(item, raw_plan)
                        semantic_key = self._compute_semantic_key(
                            subject_key=item_subject_key,
                            action_type=plan_action,
                            trigger_spec=plan_spec_dict,
                            payload=suggested_payload,
                        )
                        trigger_summary = _summarize_trigger(plan_spec_dict, plan_parse_status)
                        expires_at = _extract_expires_at(plan_spec_dict)

                        # supersede 检查:同 semantic_key 且 status='proposed' 的旧 plan 标 superseded;
                        # status='armed' 的旧 plan 不动(用户已显式启用,不能被静默替换)
                        cur.execute(
                            """
                            UPDATE recommendation_action_plans
                               SET status='superseded', updated_at=NOW()
                             WHERE semantic_key=%s AND status='proposed'
                            """,
                            (semantic_key,),
                        )

                        cur.execute(
                            """
                            INSERT INTO recommendation_action_plans (
                                item_id, run_id, ordinal, semantic_key, action_type,
                                trigger_spec, trigger_parse_status, trigger_summary,
                                suggested_execution_payload, status, expires_at, reason_text
                            ) VALUES (
                                %s, %s, %s, %s, %s,
                                %s, %s, %s,
                                %s, 'proposed', %s, %s
                            )
                            """,
                            (
                                item_id,
                                run_id,
                                ordinal_zero + 1,
                                semantic_key,
                                plan_action,
                                Json(plan_spec_dict, dumps=_json_dumps),
                                plan_parse_status,
                                trigger_summary,
                                Json(suggested_payload, dumps=_json_dumps) if suggested_payload else None,
                                expires_at,
                                str(raw_plan.get("reason") or "")[:1000] or None,
                            ),
                        )

            conn.commit()
            logger.info("recommendation_run 已保存: run_id=%s items=%s", run_id, len(items))
            return run_id

    @staticmethod
    def _build_suggested_payload(item: dict, raw_plan: dict) -> dict:
        """从 AI 给的 plan 字段 + item 上下文构造 suggested_execution_payload。
        engine fire 时若 plan 已 armed,会用 armed_execution_payload(冻结快照),
        手动 preview/execute 用这个 suggested_*。
        """
        action = str(raw_plan.get("action_type") or "").strip().lower()
        side = raw_plan.get("side") or item.get("direction") or item.get("default_target_side")
        price = raw_plan.get("price_cents") or item.get("suggested_price_low_cents") or item.get("suggested_price_high_cents")
        size_text = raw_plan.get("size_text") or item.get("size_text")
        size_spec_raw = raw_plan.get("size_spec")
        size_spec = None
        if isinstance(size_spec_raw, dict):
            mode = str(size_spec_raw.get("mode") or "").strip().lower()
            try:
                value = float(size_spec_raw.get("value"))
            except (TypeError, ValueError):
                value = None
            if mode in {"amount_usdc", "shares", "portion_position", "portion_equity", "portion_cash"} and value is not None and value > 0:
                size_spec = {"mode": mode, "value": value}
        target_order_id = raw_plan.get("target_order_id")
        # warning/复盘 等 item 的 title 不是真实 market question;
        # ① 优先 AI 在 plan 里给的 target_question;② 否则回退到 build_recommendation_items 预解析的 default_target_question。
        target_question = (
            (raw_plan.get("target_question") or "").strip()
            or (item.get("default_target_question") or "").strip()
            or None
        )
        return {
            "action_type": action,
            "side": side,
            "price_cents": price,
            "size_text": size_text,
            "size_spec": size_spec,
            "target_order_id": target_order_id,
            "target_question": target_question,
            "subject_key": item.get("subject_key"),
        }

    @staticmethod
    def _compute_semantic_key(*, subject_key: str, action_type: str, trigger_spec: dict, payload: dict) -> str:
        """跨 run 标识"同一个建议"的稳定 hash。
        包含:subject_key + action_type + trigger 关键字段 + payload 关键字段。
        不含 expires_at / dwell / cooldown 等运行参数,允许 AI 微调而不被识别为新计划。
        """
        import hashlib
        sig = {
            "subject": subject_key or "",
            "action": action_type or "",
            "trigger": {
                "type": (trigger_spec or {}).get("type"),
                "operator": (trigger_spec or {}).get("operator"),
                "value": (trigger_spec or {}).get("value"),
                "asset_token_id": (trigger_spec or {}).get("asset_token_id"),
            },
            "payload": {
                "side": (payload or {}).get("side"),
                "price_cents": (payload or {}).get("price_cents"),
                "target_order_id": (payload or {}).get("target_order_id"),
            },
        }
        s = json.dumps(sig, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.md5(s.encode("utf-8")).hexdigest()

    def claim_item_for_execution(
        self,
        *,
        item_id: int,
        expected_action_type: str,
    ) -> dict:
        """Execution gate（**原子占用版**）：

        - 用一条 UPDATE ... WHERE status IN (...) AND action_type=? RETURNING ... 同时
          完成"校验合法源状态 + 校验 action_type + 占用为 executing"三件事；
        - 占用成功后，调用方必须**保证**最终调用 `record_action()`（成功或失败）
          来把 item 推到 order_submitted/order_failed/cancel_submitted/cancel_failed，
          否则 item 会停留在 executing 状态（仍然安全：不会被并发再次占用，但需要人工干预）；
        - 校验失败抛 RecommendationGateError，调用方应返回 409。

        这把"先校验后下单"的窗口期（rubber-duck 第 2 轮发现的并发双重下单漏洞）
        收敛到一条原子 UPDATE。
        """
        normalized_expected = str(expected_action_type or "").strip().lower()
        if normalized_expected not in {"buy", "sell", "cancel"}:
            raise ValueError(f"unsupported expected_action_type: {expected_action_type}")

        with self._lock, get_conn() as conn:
            import psycopg2.extras
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            # 先把 item 拿出来（带行锁），便于区分"不存在 / 状态不对 / action 不匹配"几种失败原因
            cur.execute(
                """
                SELECT id, action_type, status, item_kind, title
                FROM recommendation_items
                WHERE id = %s
                FOR UPDATE
                """,
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise RecommendationGateError(
                    f"recommendation item {item_id} 不存在",
                    code="item_not_found",
                )
            item_action = str(row["action_type"] or "").strip().lower()
            item_status = str(row["status"] or "").strip().lower()
            if item_action != normalized_expected:
                conn.rollback()
                raise RecommendationGateError(
                    f"item {item_id} 的 action_type={item_action}，与本次请求 {normalized_expected} 不匹配",
                    code="action_type_mismatch",
                )
            if item_status not in _EXECUTABLE_ITEM_STATUSES:
                conn.rollback()
                raise RecommendationGateError(
                    f"item {item_id} 当前状态 {item_status} 不允许执行（需要批准或上次失败可重试）",
                    code="item_not_approved",
                )
            cur.execute(
                """
                UPDATE recommendation_items
                SET status = 'executing', executing_started_at = NOW()
                WHERE id = %s
                """,
                (item_id,),
            )
            conn.commit()
            return {
                "item_id": int(row["id"]),
                "action_type": item_action,
                "previous_status": item_status,
                "status": "executing",
                "item_kind": row["item_kind"],
                "title": row["title"],
            }

    # 兼容保留：旧名 assert_item_executable，但内部走原子占用语义。
    # 任何调用方拿到成功返回后，必须保证最终调用 record_action()。
    def assert_item_executable(
        self,
        *,
        item_id: int,
        expected_action_type: str,
    ) -> dict:
        return self.claim_item_for_execution(
            item_id=item_id,
            expected_action_type=expected_action_type,
        )

    def list_stale_executing(self, *, timeout_minutes: int = 15) -> list[dict]:
        """返回 status='executing' 且 executing_started_at 早于 timeout_minutes 的 item 列表，
        用于 watchdog 巡检：rubber-duck 第 3 轮发现 record_action 失败时 item 会卡在 executing
        且无任何巡检/告警，导致后续 buy/sell 永远 409。"""
        try:
            timeout = max(1, int(timeout_minutes))
        except (TypeError, ValueError):
            timeout = 15
        with self._lock, get_conn() as conn:
            import psycopg2.extras
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT ri.id, ri.title, ri.action_type, ri.item_kind,
                       ri.executing_started_at,
                       EXTRACT(EPOCH FROM (NOW() - COALESCE(ri.executing_started_at, ri.created_at))) AS stale_seconds,
                       rr.asset, rr.created_at AS run_created_at
                FROM recommendation_items ri
                JOIN recommendation_runs rr ON rr.id = ri.run_id
                WHERE ri.status = 'executing'
                  AND COALESCE(ri.executing_started_at, ri.created_at)
                      < NOW() - (%s || ' minutes')::interval
                ORDER BY COALESCE(ri.executing_started_at, ri.created_at) ASC
                """,
                (str(timeout),),
            )
            rows = cur.fetchall()
            result: list[dict] = []
            for row in rows:
                d = dict(row)
                d["stale_seconds"] = float(d.get("stale_seconds") or 0.0)
                result.append(d)
            return result

    def force_release_executing(
        self,
        *,
        item_id: int,
        reason: str,
        released_by: str,
        new_status: str = "order_failed",
        acknowledge_possible_duplicate: bool = False,
    ) -> dict:
        """人工把卡在 executing 的 item 解除回 approved/order_failed/cancel_failed。

        - 仅允许从 executing 释放，避免把已成交单退回；
        - 必须传 reason+released_by，写入 recommendation_actions 留痕。
        - 第四轮加固 #1（严重 / 资金风险）：fail-closed reconciliation。
          如果该 item 已经存在 status='submitted' 的真实下单/撤单 action（即"订单可能已发出，
          只是回写失败"），默认拒绝释放到 approved——直接 release 到 approved 会让 claim_item_for_execution
          再次允许下单，造成**重复下单**。调用方必须：
            (a) 先去交易所对账（open orders / order_id 查询）；
            (b) 若确认订单实际已成功，应当通过 record_outcome 推到终态而非 release；
            (c) 若确认订单确实没发出，传 acknowledge_possible_duplicate=True 显式承担风险，
                且 new_status 必须是 order_failed/cancel_failed（仍允许重试，但留痕已 ack）。
          new_status 默认从 'approved' 改为 'order_failed'，让 GUI/默认路径走"重试需新一轮 approve"。
        """
        reason_norm = str(reason or "").strip()
        if not reason_norm:
            raise ValueError("reason 不能为空")
        released_by_norm = str(released_by or "").strip()
        if not released_by_norm:
            raise ValueError("released_by 不能为空")
        new_status_norm = str(new_status or "").strip().lower()
        if new_status_norm not in _EXECUTABLE_ITEM_STATUSES:
            raise ValueError(
                f"new_status 必须是 {sorted(_EXECUTABLE_ITEM_STATUSES)} 之一，得到 {new_status_norm}"
            )

        with self._lock, get_conn() as conn:
            import psycopg2.extras
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(
                """
                SELECT id, status, action_type, title
                FROM recommendation_items
                WHERE id = %s
                FOR UPDATE
                """,
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.rollback()
                raise RecommendationGateError(
                    f"recommendation item {item_id} 不存在",
                    code="item_not_found",
                )
            current_status = str(row["status"] or "").strip().lower()
            if current_status != "executing":
                conn.rollback()
                raise RecommendationGateError(
                    f"item {item_id} 当前状态 {current_status} 不是 executing，无需释放",
                    code="item_not_executing",
                )

            # 第四轮加固 #1：检查是否已有 submitted action（订单可能已发出但回写失败）
            cur.execute(
                """
                SELECT id, action_type, status, order_id, created_at
                FROM recommendation_actions
                WHERE item_id = %s
                  AND action_type IN ('buy', 'sell', 'cancel')
                  AND status = 'submitted'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (item_id,),
            )
            submitted_action = cur.fetchone()
            if submitted_action and not acknowledge_possible_duplicate:
                conn.rollback()
                raise RecommendationGateError(
                    f"item {item_id} 已有 submitted 的 {submitted_action['action_type']} action "
                    f"(order_id={submitted_action.get('order_id')})，订单可能已成功只是回写失败。"
                    f"请先到交易所对账：若已成交请用 record_outcome 推到终态；"
                    f"若确认未发出，请显式传 acknowledge_possible_duplicate=true 释放。",
                    code="release_blocked_by_submitted_action",
                )
            if submitted_action and new_status_norm == "approved":
                conn.rollback()
                raise RecommendationGateError(
                    f"item {item_id} 已有 submitted action，即便 ack 也不允许释放到 approved "
                    f"（会绕过下一轮人工审核），请释放到 order_failed/cancel_failed",
                    code="release_to_approved_blocked",
                )

            cur.execute(
                """
                UPDATE recommendation_items
                SET status = %s, executing_started_at = NULL
                WHERE id = %s
                """,
                (new_status_norm, item_id),
            )
            cur.execute(
                """
                INSERT INTO recommendation_actions (
                    item_id, action_type, status, request_payload,
                    response_payload, order_id, error_text
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    item_id,
                    "force_release",
                    "released",
                    Json({
                        "released_by": released_by_norm,
                        "new_status": new_status_norm,
                        "reason": reason_norm,
                        "acknowledge_possible_duplicate": bool(acknowledge_possible_duplicate),
                        "had_submitted_action": bool(submitted_action),
                        "submitted_action_id": (submitted_action["id"] if submitted_action else None),
                        "submitted_order_id": (submitted_action.get("order_id") if submitted_action else None),
                    }, dumps=_json_dumps),
                    Json({}, dumps=_json_dumps),
                    None,
                    reason_norm,
                ),
            )
            conn.commit()
            return {
                "item_id": item_id,
                "previous_status": current_status,
                "new_status": new_status_norm,
                "released_by": released_by_norm,
                "reason": reason_norm,
                "acknowledge_possible_duplicate": bool(acknowledge_possible_duplicate),
                "had_submitted_action": bool(submitted_action),
            }

    def submit_feedback(
        self,
        *,
        item_id: int,
        decision: str,
        reason_tags: list[str] | None = None,
        feedback_text: str | None = None,
        allow_model_learning: bool = True,
        raw_payload: dict | None = None,
    ) -> dict:
        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision not in _FEEDBACK_DECISION_TO_STATUS:
            raise ValueError(f"unsupported decision: {decision}")

        cleaned_tags: list[str] = []
        for tag in reason_tags or []:
            text = str(tag or "").strip()
            if text and text not in cleaned_tags:
                cleaned_tags.append(text)

        item_status = _FEEDBACK_DECISION_TO_STATUS[normalized_decision]

        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, run_id, title, status
                FROM recommendation_items
                WHERE id = %s
                """,
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"recommendation item not found: {item_id}")

            current_status = row[3] if isinstance(row, (list, tuple)) else row["status"]
            if current_status in _TERMINAL_ITEM_STATUSES:
                # 已下过单 / 已撤单 / 已结算的 item 不允许通过反馈再次改写状态，
                # 防止"再点一次批准执行 → 重新进 execution gate → 重复下单"。
                raise RecommendationGateError(
                    f"item {item_id} 已处于终态 {current_status}，不允许再次反馈以改写状态",
                    code="item_terminal",
                )
            if current_status == "executing":
                # 正在执行中（已被 gate 占用尚未回写最终状态），同样不允许 feedback 改写
                raise RecommendationGateError(
                    f"item {item_id} 正在执行中，请等待执行结果回写后再操作",
                    code="item_executing",
                )

            cur.execute(
                """
                INSERT INTO recommendation_feedback (
                    item_id, decision, reason_tags, feedback_text,
                    allow_model_learning, raw_payload
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (
                    item_id,
                    normalized_decision,
                    Json(cleaned_tags, dumps=_json_dumps),
                    (feedback_text or "").strip() or None,
                    bool(allow_model_learning),
                    Json(raw_payload or {}, dumps=_json_dumps),
                ),
            )
            feedback_id, created_at = cur.fetchone()

            cur.execute(
                """
                UPDATE recommendation_items
                SET status = %s
                WHERE id = %s
                """,
                (item_status, item_id),
            )
            conn.commit()
            return {
                "feedback_id": feedback_id,
                "item_id": item_id,
                "run_id": row[1] if isinstance(row, (list, tuple)) else row["run_id"],
                "title": row[2] if isinstance(row, (list, tuple)) else row["title"],
                "decision": normalized_decision,
                "status": item_status,
                "reason_tags": cleaned_tags,
                "feedback_text": (feedback_text or "").strip() or None,
                "allow_model_learning": bool(allow_model_learning),
                "created_at": created_at,
            }

    def record_action(
        self,
        *,
        item_id: int,
        action_type: str,
        status: str,
        order_id: str | None = None,
        request_payload: dict | None = None,
        response_payload: dict | None = None,
        error_text: str | None = None,
        triggered_by: str | None = None,
        plan_id: int | None = None,
    ) -> dict:
        normalized_action_type = str(action_type or "").strip().lower()
        normalized_status = str(status or "").strip().lower()
        if normalized_action_type not in {"buy", "sell", "cancel"}:
            raise ValueError(f"unsupported action_type: {action_type}")
        if normalized_status not in {"submitted", "failed"}:
            raise ValueError(f"unsupported action status: {status}")

        normalized_triggered_by = (triggered_by or "manual").strip().lower() or "manual"
        if normalized_triggered_by not in {"manual", "auto", "cron"}:
            raise ValueError(f"unsupported triggered_by: {triggered_by}")

        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, title
                FROM recommendation_items
                WHERE id = %s
                """,
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"recommendation item not found: {item_id}")

            cur.execute(
                """
                INSERT INTO recommendation_actions (
                    item_id, plan_id, action_type, status, order_id,
                    request_payload, response_payload, error_text, triggered_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (
                    item_id,
                    int(plan_id) if plan_id is not None else None,
                    normalized_action_type,
                    normalized_status,
                    (order_id or "").strip() or None,
                    Json(request_payload or {}, dumps=_json_dumps),
                    Json(response_payload or {}, dumps=_json_dumps),
                    (error_text or "").strip() or None,
                    normalized_triggered_by,
                ),
            )
            action_id, created_at = cur.fetchone()

            # 阶段4: 仅在"item 级 manual" 路径下推动 item.status
            # plan 驱动的 auto 动作只更新 plan 状态(由 atdb.mark_plan_fired/_failed 处理),
            # 否则一个 item 的首条 plan fire 后整 item 被推到 order_submitted/order_failed,
            # 后续 plan 会被 list_active/claim 的 item.status 守卫挡掉,1:N 语义就废了。
            should_advance_item = (plan_id is None)
            if should_advance_item:
                if normalized_action_type == "cancel":
                    item_status = "cancel_submitted" if normalized_status == "submitted" else "cancel_failed"
                else:
                    item_status = "order_submitted" if normalized_status == "submitted" else "order_failed"
                cur.execute(
                    """
                    UPDATE recommendation_items
                    SET status = %s
                    WHERE id = %s
                    """,
                    (item_status, item_id),
                )
            else:
                # plan-driven: item.status 不变;仅在返回里给前端展示,供 latest_action 渲染
                if normalized_action_type == "cancel":
                    item_status = "cancel_submitted" if normalized_status == "submitted" else "cancel_failed"
                else:
                    item_status = "order_submitted" if normalized_status == "submitted" else "order_failed"
            conn.commit()
            return {
                "action_id": action_id,
                "item_id": item_id,
                "title": row[1] if isinstance(row, (list, tuple)) else row["title"],
                "action_type": normalized_action_type,
                "status": item_status,
                "order_id": (order_id or "").strip() or None,
                "error_text": (error_text or "").strip() or None,
                "created_at": created_at,
            }

    def record_outcome(
        self,
        *,
        item_id: int,
        outcome_label: str,
        hit: bool | None = None,
        pnl: float | None = None,
        notes: str | None = None,
        metrics: dict | None = None,
        recorded_by: str | None = None,
        revision_reason: str | None = None,
    ) -> dict:
        """人工/外部为已执行的 recommendation item 登记最终结果。

        - outcome_label: 'won' / 'lost' / 'breakeven' / 'expired' / 'unknown' 等自由标签；
          若是 'won'/'lost'/'expired' 会同步把 item.status 改写到对应终态，
          以便后续统计和 memory_context 摘要可见。
        - 仅允许对已经下过单或撤过单的 item 登记结果。
        - 第三轮审查 #3：必须传 recorded_by；若该 item 已存在 is_final=TRUE 的 outcome，
          再次登记必须传 revision_reason，并把旧的 is_final 置 FALSE，新行写 supersedes_outcome_id+is_final=TRUE。
          这样 build_memory_context 取 latest is_final 时不会被无凭据覆盖污染。
        """
        normalized_label = str(outcome_label or "").strip().lower()
        if not normalized_label:
            raise ValueError("outcome_label 不能为空")
        allowed_labels = {"won", "lost", "breakeven", "expired", "unknown"}
        if normalized_label not in allowed_labels:
            raise ValueError(f"outcome_label 必须是 {sorted(allowed_labels)} 之一")

        recorded_by_norm = str(recorded_by or "").strip()
        if not recorded_by_norm:
            raise ValueError("recorded_by 不能为空（用于审计）")

        try:
            pnl_value = float(pnl) if pnl is not None else None
        except (TypeError, ValueError) as exc:
            raise ValueError(f"pnl 非法: {exc}") from exc

        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, status, title
                FROM recommendation_items
                WHERE id = %s
                FOR UPDATE
                """,
                (item_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"recommendation item not found: {item_id}")
            current_status = row["status"] if not isinstance(row, (list, tuple)) else row[1]
            if current_status not in _OUTCOME_ELIGIBLE_STATUSES:
                # 严格收口：只允许对真实成功提交过的 item 登记结果，
                # 排除 order_failed/cancel_failed，避免把"根本没成交"的失败单包装成 won。
                raise RecommendationGateError(
                    f"item {item_id} 当前状态 {current_status} 不允许登记 outcome（必须先成功提交订单/撤单）",
                    code="item_not_executed",
                )

            cur.execute(
                """
                SELECT id FROM recommendation_outcomes
                WHERE item_id = %s AND is_final IS TRUE
                ORDER BY evaluated_at DESC, id DESC
                LIMIT 1
                """,
                (item_id,),
            )
            existing_final = cur.fetchone()
            existing_final_id = (existing_final[0] if isinstance(existing_final, (list, tuple))
                                  else (existing_final["id"] if existing_final else None)) if existing_final else None
            revision_reason_norm = str(revision_reason or "").strip()
            if existing_final_id is not None and not revision_reason_norm:
                # 已存在 final outcome，必须显式传修订理由才能覆盖（防止任意覆盖污染统计）
                raise RecommendationGateError(
                    f"item {item_id} 已有最终 outcome（#{existing_final_id}），如需修订必须传 revision_reason",
                    code="outcome_already_final",
                )
            if existing_final_id is not None:
                cur.execute(
                    """
                    UPDATE recommendation_outcomes
                    SET is_final = FALSE
                    WHERE id = %s
                    """,
                    (existing_final_id,),
                )

            cur.execute(
                """
                INSERT INTO recommendation_outcomes (
                    item_id, outcome_label, hit, pnl, notes, metrics,
                    recorded_by, supersedes_outcome_id, is_final, revision_reason
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)
                RETURNING id, evaluated_at
                """,
                (
                    item_id,
                    normalized_label,
                    bool(hit) if hit is not None else None,
                    pnl_value,
                    (notes or "").strip() or None,
                    Json(metrics or {}, dumps=_json_dumps),
                    recorded_by_norm,
                    existing_final_id,
                    revision_reason_norm or None,
                ),
            )
            outcome_id, evaluated_at = cur.fetchone()

            new_status_map = {"won": "won", "lost": "lost", "expired": "expired"}
            if normalized_label in new_status_map:
                cur.execute(
                    """
                    UPDATE recommendation_items
                    SET status = %s
                    WHERE id = %s
                    """,
                    (new_status_map[normalized_label], item_id),
                )

            conn.commit()
            return {
                "outcome_id": outcome_id,
                "item_id": item_id,
                "outcome_label": normalized_label,
                "hit": bool(hit) if hit is not None else None,
                "pnl": pnl_value,
                "evaluated_at": evaluated_at,
                "recorded_by": recorded_by_norm,
                "supersedes_outcome_id": existing_final_id,
                "is_final": True,
            }

    def build_memory_context(
        self,
        *,
        asset: str = "btc",
        feedback_days: int = 7,
        outcome_days: int = 30,
        pending_limit: int = 8,
        pending_days: int = 14,
    ) -> dict:
        feedback_rows: list[dict] = []
        pending_rows: list[dict] = []
        action_stats: dict[str, int] = {}
        outcome_summary = {
            "evaluated_count": 0,
            "hit_count": 0,
            "hit_rate": None,
            "total_pnl": 0.0,
        }

        with get_cursor() as cur:
            cur.execute(
                """
                SELECT rf.decision, rf.reason_tags, rf.feedback_text, rf.allow_model_learning,
                       rf.created_at, ri.title, ri.item_kind, ri.source_section
                FROM recommendation_feedback rf
                JOIN recommendation_items ri ON ri.id = rf.item_id
                JOIN recommendation_runs rr ON rr.id = ri.run_id
                WHERE rr.asset = %s
                  AND rf.created_at >= NOW() - (%s || ' days')::interval
                ORDER BY rf.created_at DESC
                LIMIT 200
                """,
                (asset, str(feedback_days)),
            )
            feedback_rows = list(cur.fetchall())

            cur.execute(
                """
                SELECT ra.status, COUNT(*) AS cnt
                FROM recommendation_actions ra
                JOIN recommendation_items ri ON ri.id = ra.item_id
                JOIN recommendation_runs rr ON rr.id = ri.run_id
                WHERE rr.asset = %s
                  AND ra.created_at >= NOW() - (%s || ' days')::interval
                GROUP BY ra.status
                """,
                (asset, str(outcome_days)),
            )
            for row in cur.fetchall():
                action_stats[str(row["status"])] = int(row["cnt"])

            cur.execute(
                """
                WITH latest_outcome AS (
                    SELECT DISTINCT ON (ro.item_id)
                        ro.item_id, ro.hit, ro.pnl, ro.outcome_label, ro.evaluated_at
                    FROM recommendation_outcomes ro
                    JOIN recommendation_items ri ON ri.id = ro.item_id
                    JOIN recommendation_runs rr ON rr.id = ri.run_id
                    WHERE rr.asset = %s
                      AND ro.evaluated_at >= NOW() - (%s || ' days')::interval
                      AND ro.is_final IS TRUE
                    ORDER BY ro.item_id, ro.evaluated_at DESC, ro.id DESC
                )
                SELECT
                    COUNT(*) FILTER (WHERE hit IS NOT NULL) AS evaluated_count,
                    SUM(CASE WHEN hit IS TRUE THEN 1 ELSE 0 END) AS hit_count,
                    SUM(COALESCE(pnl, 0)) AS total_pnl,
                    COUNT(*) AS total_outcome_items
                FROM latest_outcome
                """,
                (asset, str(outcome_days)),
            )
            outcome_row = cur.fetchone()
            if outcome_row:
                evaluated_count = int(outcome_row["evaluated_count"] or 0)
                hit_count = int(outcome_row["hit_count"] or 0)
                total_pnl = float(outcome_row["total_pnl"] or 0.0)
                total_outcome_items = int(outcome_row["total_outcome_items"] or 0)
                outcome_summary = {
                    # 仅按"每 item 最新 outcome"统计；hit_rate 分母排除 hit IS NULL 的中性结果
                    "evaluated_count": evaluated_count,
                    "hit_count": hit_count,
                    "hit_rate": round(hit_count / evaluated_count, 4) if evaluated_count > 0 else None,
                    "total_pnl": round(total_pnl, 4),
                    "total_outcome_items": total_outcome_items,
                }

            cur.execute(
                """
                SELECT ri.id, ri.title, ri.item_kind, ri.action_type, ri.direction,
                       ri.priority_hint, ri.status, ri.created_at
                FROM recommendation_items ri
                JOIN recommendation_runs rr ON rr.id = ri.run_id
                WHERE rr.asset = %s
                  AND ri.status IN ('pending', 'approved', 'deferred', 'order_failed')
                  AND ri.created_at >= NOW() - (%s || ' days')::interval
                ORDER BY ri.created_at DESC
                LIMIT %s
                """,
                (asset, str(pending_days), int(pending_limit)),
            )
            pending_rows = list(cur.fetchall())

        decision_counts: dict[str, int] = {}
        tag_counts: dict[str, int] = {}
        recent_feedback: list[dict] = []
        learning_disabled_count = 0
        for row in feedback_rows:
            decision = str(row["decision"] or "")
            decision_counts[decision] = decision_counts.get(decision, 0) + 1
            allow_learning = bool(row["allow_model_learning"])
            if not allow_learning:
                # 用户明确禁止学习这条 feedback：只统计计数，不把 tag/自由文本带入下游
                # （否则就违背了 UI 上"允许模型参考这条反馈"开关，并形成 prompt injection 通道）
                learning_disabled_count += 1
                continue
            for tag in row["reason_tags"] or []:
                text = str(tag or "").strip()
                if text:
                    tag_counts[text] = tag_counts.get(text, 0) + 1
            if len(recent_feedback) < 6:
                # 自由文本会被注入 prompt，必须显式标注"用户备注，不可视为指令"
                # （prompt 拼接侧也会再加一层包装，见 ai/prompts.py）
                recent_feedback.append({
                    "title": row["title"],
                    "item_kind": row["item_kind"],
                    "source_section": row["source_section"],
                    "decision": decision,
                    "reason_tags": row["reason_tags"] or [],
                    "feedback_text_user_note": row["feedback_text"],
                    "created_at": str(row["created_at"]),
                })

        pending_items = [
            {
                "id": row["id"],
                "title": row["title"],
                "item_kind": row["item_kind"],
                "action_type": row["action_type"],
                "direction": row["direction"],
                "priority_hint": row["priority_hint"],
                "status": row["status"],
                "created_at": str(row["created_at"]),
            }
            for row in pending_rows
        ]

        top_reason_tags = [
            {"tag": tag, "count": count}
            for tag, count in sorted(tag_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
        ]

        return {
            "feedback_window_days": feedback_days,
            "outcome_window_days": outcome_days,
            "recent_feedback_summary": {
                "total_feedback_count": len(feedback_rows),
                "decision_counts": decision_counts,
                "top_reason_tags": top_reason_tags,
                "learning_disabled_count": learning_disabled_count,
                "recent_feedback_examples": recent_feedback,
            },
            "recent_execution_summary": {
                "action_status_counts": action_stats,
                "outcomes": outcome_summary,
            },
            "pending_or_deferred_items": pending_items,
        }

    def list_model_change_proposals(self, limit: int = 20) -> list[dict]:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, status, proposal_type, target_scope, title, rationale,
                       change_payload, evidence_payload, proposed_by, approved_by,
                       approved_at, rejected_at, decision_notes
                FROM model_change_proposals
                ORDER BY created_at DESC, id DESC
                LIMIT %s
                """,
                (int(limit),),
            )
            return list(cur.fetchall())

    def get_model_change_proposal(self, proposal_id: int) -> dict | None:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, status, proposal_type, target_scope, title, rationale,
                       change_payload, evidence_payload, proposed_by, approved_by,
                       approved_at, rejected_at, decision_notes
                FROM model_change_proposals
                WHERE id = %s
                """,
                (int(proposal_id),),
            )
            return cur.fetchone()

    def create_model_change_proposal(
        self,
        *,
        proposal_type: str,
        title: str,
        rationale: str,
        change_payload: dict | None = None,
        evidence_payload: dict | None = None,
        proposed_by: str = "agent",
        target_scope: str = "monthly_recommendation",
    ) -> dict:
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO model_change_proposals (
                    proposal_type, target_scope, title, rationale,
                    change_payload, evidence_payload, proposed_by
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at, status
                """,
                (
                    str(proposal_type or "").strip(),
                    str(target_scope or "monthly_recommendation").strip(),
                    str(title or "").strip(),
                    str(rationale or "").strip(),
                    Json(change_payload or {}, dumps=_json_dumps),
                    Json(evidence_payload or {}, dumps=_json_dumps),
                    str(proposed_by or "agent").strip(),
                ),
            )
            row = cur.fetchone()
            conn.commit()
            proposal_id = row[0] if isinstance(row, (list, tuple)) else row["id"]
            created_at = row[1] if isinstance(row, (list, tuple)) else row["created_at"]
            status = row[2] if isinstance(row, (list, tuple)) else row["status"]
            return {
                "id": proposal_id,
                "created_at": created_at,
                "status": status,
                "title": str(title or "").strip(),
            }

    def review_model_change_proposal(
        self,
        *,
        proposal_id: int,
        decision: str,
        reviewer: str,
        decision_notes: str | None = None,
    ) -> dict:
        normalized_decision = str(decision or "").strip().lower()
        if normalized_decision not in {"approve", "reject"}:
            raise ValueError(f"unsupported proposal decision: {decision}")
        new_status = "approved" if normalized_decision == "approve" else "rejected"
        approved_by = str(reviewer or "").strip() or "dashboard"
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE model_change_proposals
                SET status = %s,
                    approved_by = CASE WHEN %s = 'approved' THEN %s ELSE approved_by END,
                    approved_at = CASE WHEN %s = 'approved' THEN NOW() ELSE approved_at END,
                    rejected_at = CASE WHEN %s = 'rejected' THEN NOW() ELSE rejected_at END,
                    decision_notes = %s
                WHERE id = %s
                RETURNING id, status, title, approved_by, approved_at, rejected_at, decision_notes
                """,
                (
                    new_status,
                    new_status,
                    approved_by,
                    new_status,
                    new_status,
                    (decision_notes or "").strip() or None,
                    int(proposal_id),
                ),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f"proposal not found: {proposal_id}")
            conn.commit()
            return {
                "id": row[0] if isinstance(row, (list, tuple)) else row["id"],
                "status": row[1] if isinstance(row, (list, tuple)) else row["status"],
                "title": row[2] if isinstance(row, (list, tuple)) else row["title"],
                "approved_by": row[3] if isinstance(row, (list, tuple)) else row["approved_by"],
                "approved_at": row[4] if isinstance(row, (list, tuple)) else row["approved_at"],
                "rejected_at": row[5] if isinstance(row, (list, tuple)) else row["rejected_at"],
                "decision_notes": row[6] if isinstance(row, (list, tuple)) else row["decision_notes"],
            }

    def list_model_shadow_evals(
        self,
        *,
        proposal_id: int | None = None,
        limit: int = 50,
    ) -> list[dict]:
        with get_cursor() as cur:
            if proposal_id is None:
                cur.execute(
                    """
                    SELECT id, created_at, proposal_id, target_scope, baseline_version,
                           candidate_version, status, metrics, notes
                    FROM model_shadow_evals
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (int(limit),),
                )
            else:
                cur.execute(
                    """
                    SELECT id, created_at, proposal_id, target_scope, baseline_version,
                           candidate_version, status, metrics, notes
                    FROM model_shadow_evals
                    WHERE proposal_id = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    (int(proposal_id), int(limit)),
                )
            return list(cur.fetchall())

    def create_model_shadow_eval(
        self,
        *,
        proposal_id: int | None,
        target_scope: str = "monthly_recommendation",
        baseline_version: str | None = None,
        candidate_version: str | None = None,
        status: str = "completed",
        metrics: dict | None = None,
        notes: str | None = None,
    ) -> dict:
        with self._lock, get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO model_shadow_evals (
                    proposal_id, target_scope, baseline_version, candidate_version,
                    status, metrics, notes
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at, proposal_id, target_scope, baseline_version,
                          candidate_version, status, metrics, notes
                """,
                (
                    int(proposal_id) if proposal_id is not None else None,
                    str(target_scope or "monthly_recommendation").strip(),
                    (baseline_version or "").strip() or None,
                    (candidate_version or "").strip() or None,
                    str(status or "completed").strip(),
                    Json(metrics or {}, dumps=_json_dumps),
                    (notes or "").strip() or None,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            if isinstance(row, (list, tuple)):
                return {
                    "id": row[0],
                    "created_at": row[1],
                    "proposal_id": row[2],
                    "target_scope": row[3],
                    "baseline_version": row[4],
                    "candidate_version": row[5],
                    "status": row[6],
                    "metrics": row[7] or {},
                    "notes": row[8],
                }
            return dict(row)
