"""月度目标的成交等级归因工具。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg2.extras

from data.database import get_conn, get_cursor
from services.profit_optimizer import _extract_strike_and_direction, _parse_market_prices

logger = logging.getLogger(__name__)

ET_TIMEZONE = ZoneInfo("America/New_York")

ACTIONABLE_TIER_LABELS = {
    "comfy_high_no": "舒服高价No",
    "high_no": "高价No",
    "mid_no": "中价No",
    "low_yes": "低价Yes",
}

MONTHLY_GOAL_DEFAULT_TARGET_PCT = 20.0
MONTHLY_GOAL_TIERS = [
    {
        "key": "comfy_high_no",
        "label": "舒服高价No",
        "outcome": "No",
        "return_pct": 5.0,
        "contribution_pct": 1.0,
    },
    {
        "key": "high_no",
        "label": "标准高价No",
        "outcome": "No",
        "return_pct": 10.0,
        "contribution_pct": 3.0,
    },
    {
        "key": "mid_no",
        "label": "中价No",
        "outcome": "No",
        "return_pct": 20.0,
        "contribution_pct": 9.0,
    },
    {
        "key": "low_yes",
        "label": "低价Yes",
        "outcome": "Yes",
        "return_pct": 40.0,
        "contribution_pct": 2.0,
    },
]

_ATTRIBUTION_COLUMNS_READY = False
_MONTHLY_GOAL_SETTINGS_READY = False
MONTHLY_GOAL_MIN_POSITION_PCT_BY_TIER = {
    "comfy_high_no": 10.0,
}
MONTHLY_GOAL_OVERALL_LOSS_BUDGET_PCT = 5.0
MONTHLY_GOAL_TIER_LOSS_BUDGET_PCT_BY_TIER = {
    "comfy_high_no": 0.5,
    "high_no": 1.0,
    "mid_no": 2.5,
    "low_yes": 1.0,
}
MONTHLY_GOAL_PHASE_POSITION_CAPS_BY_TIER = {
    "month_start": {
        "comfy_high_no": 20.0,
        "high_no": 30.0,
        "mid_no": 50.0,
        "low_yes": 5.0,
    },
    "month_mid": {
        "comfy_high_no": 30.0,
        "high_no": 35.0,
        "mid_no": 35.0,
        "low_yes": 3.0,
    },
    "month_end": {
        "comfy_high_no": 50.0,
        "high_no": 35.0,
        "mid_no": 20.0,
        "low_yes": 2.0,
    },
}
MONTHLY_GOAL_MID_NO_ENTRY_RULES = {
    "month_start": {"allow_distance_pct": 8.0, "block_distance_pct": 6.0},
    "month_mid": {"allow_distance_pct": 7.0, "block_distance_pct": 5.0},
    "month_end": {"allow_distance_pct": 6.0, "block_distance_pct": 4.5},
}
MONTHLY_REVIEW_SNAPSHOT_VERSION = 1
MONTHLY_REVIEW_DISTANCE_BUCKET_LABELS = {
    "lt_3": "<3%",
    "3_5": "3%-5%",
    "5_8": "5%-8%",
    "8_12": "8%-12%",
    "gte_12": ">=12%",
    "unknown": "未知距离",
}
MONTHLY_REVIEW_GATE_LABELS = {
    "allow": "允许",
    "caution": "谨慎",
    "block": "暂停",
    "unknown": "未知",
}
MONTHLY_REVIEW_SAMPLE_QUALITY_LABELS = {
    "insufficient": "样本不足",
    "limited": "样本有限",
    "reliable": "样本较可靠",
}


def _loss_budget_status(used_usdc: float, budget_usdc: float) -> tuple[str, float | None]:
    if budget_usdc <= 0:
        return ("stop_new_entries" if used_usdc > 0 else "ok", None)
    usage_pct = used_usdc / budget_usdc * 100.0
    if usage_pct >= 100.0:
        return "stop_new_entries", usage_pct
    if usage_pct >= 70.0:
        return "caution", usage_pct
    return "ok", usage_pct


def _pending_order_buy_notional(order: dict[str, Any]) -> float:
    """估算 active buy pending 的最大占用；缺价格时保守返回 0 并在 unmatched 中暴露。"""
    if not isinstance(order, dict):
        return 0.0
    try:
        estimated = _to_float(order.get("estimated_buy_notional_usdc"), None)
        if estimated is not None and estimated > 0:
            return float(estimated)
        size_spec = order.get("size_spec") if isinstance(order.get("size_spec"), dict) else {}
        price_spec = order.get("price_spec") if isinstance(order.get("price_spec"), dict) else {}
        size_type = str(size_spec.get("type") or "").lower()
        if size_type == "usdc":
            return max(0.0, float(size_spec.get("value") or 0.0))
        if size_type == "shares":
            price = _to_float(price_spec.get("value"), None)
            if price is None or price <= 0:
                price = _to_float(order.get("current_token_price"), None)
            if price is None or price <= 0:
                return 0.0
            return max(0.0, float(size_spec.get("value") or 0.0) * float(price))
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def _active_pending_buys_by_token(active_manual_pending_orders: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    by_token: dict[str, dict[str, Any]] = {}
    if not isinstance(active_manual_pending_orders, dict):
        return by_token
    orders = active_manual_pending_orders.get("orders") or []
    if not isinstance(orders, list):
        return by_token
    for order in orders:
        if not isinstance(order, dict):
            continue
        if str(order.get("action") or "").strip().lower() != "buy":
            continue
        if str(order.get("status") or "pending").strip().lower() not in {"pending", "executing"}:
            continue
        token_id = str(order.get("token_id") or "").strip()
        if not token_id:
            continue
        notional = _pending_order_buy_notional(order)
        row = by_token.setdefault(token_id, {
            "pending_buy_notional_usdc": 0.0,
            "pending_order_ids": [],
            "pending_plan_ids": [],
        })
        row["pending_buy_notional_usdc"] += notional
        if order.get("id") is not None:
            row["pending_order_ids"].append(order.get("id"))
        if order.get("plan_id") is not None and order.get("plan_id") not in row["pending_plan_ids"]:
            row["pending_plan_ids"].append(order.get("plan_id"))
    return by_token


def _combine_entry_status(*statuses: str | None) -> str:
    clean = {str(s or "ok") for s in statuses}
    if "block" in clean or "stop_new_entries" in clean:
        return "block"
    if "caution" in clean:
        return "caution"
    return "allow"


def _review_bias_by_tier(trade_review_context: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """把复盘结论压成按 tier 的轻量偏置，供候选质量评分使用。"""
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(trade_review_context, dict):
        return out
    for item in trade_review_context.get("conclusions") or []:
        if not isinstance(item, dict):
            continue
        group_key = str(item.get("group_key") or "")
        tier_key = group_key.split("|")[0] if group_key else ""
        if tier_key not in ACTIONABLE_TIER_LABELS:
            continue
        if str(item.get("sample_quality") or "").lower() == "insufficient":
            continue
        severity = str(item.get("severity") or "").lower()
        current = out.setdefault(tier_key, {"add": 0, "reduce": 0, "block": 0, "labels": []})
        if severity in {"add", "reduce", "block"}:
            current[severity] += 1
            title = str(item.get("title") or "")
            if title:
                current["labels"].append(title[:80])
    return out


def _candidate_quality(
    *,
    candidate: dict[str, Any],
    tier_key: str,
    phase_key: str,
    entry_gate: str,
    review_bias: dict[str, dict[str, Any]],
    tier_headroom: float | None,
) -> dict[str, Any]:
    """启发式候选质量分；只用于排序和提示，不是校准概率。"""
    score = 50.0
    reasons: list[str] = ["heuristic_not_calibrated"]
    gate = str(entry_gate or "allow")
    if gate == "allow":
        score += 20
        reasons.append("entry_gate_allow")
    elif gate == "caution":
        score -= 10
        reasons.append("entry_gate_caution")
    else:
        score -= 45
        reasons.append("entry_gate_block")

    distance = _to_float(candidate.get("distance_pct"), None)
    price = _to_float(candidate.get("price"), None)
    if distance is not None:
        if tier_key in {"comfy_high_no", "high_no"}:
            if distance >= (12 if phase_key == "month_early" else 8):
                score += 10
                reasons.append("wide_distance")
            elif distance < 5:
                score -= 15
                reasons.append("thin_distance")
        elif tier_key == "mid_no":
            if 4 <= distance <= 10:
                score += 8
                reasons.append("mid_no_distance_fit")
            elif distance < 3:
                score -= 18
                reasons.append("too_close_to_barrier")
        elif tier_key == "low_yes":
            if 2 <= distance <= 8:
                score += 8
                reasons.append("low_yes_distance_fit")
            elif distance > 12:
                score -= 15
                reasons.append("too_far_for_low_yes")
    if price is not None:
        if tier_key in {"comfy_high_no", "high_no"} and price >= 0.82:
            score += 8
            reasons.append("defensive_no_price_fit")
        elif tier_key == "mid_no" and 0.58 <= price < 0.82:
            score += 8
            reasons.append("mid_no_price_fit")
        elif tier_key == "low_yes" and 0.08 <= price <= 0.30:
            score += 8
            reasons.append("low_yes_price_fit")
        else:
            score -= 8
            reasons.append("price_band_mismatch")

    held_value = _to_float(candidate.get("held_value_usdc"), 0.0) or 0.0
    pending_value = _to_float(candidate.get("pending_buy_notional_usdc"), 0.0) or 0.0
    if pending_value > 0:
        score -= 10
        reasons.append("active_pending_uses_headroom")
    if tier_headroom is not None and tier_headroom <= 0:
        score -= 25
        reasons.append("no_tier_headroom")
    elif held_value > 0:
        score -= 4
        reasons.append("already_held")

    bias = review_bias.get(tier_key) or {}
    if bias.get("block"):
        score -= 18
        reasons.append("review_block_bias")
    elif bias.get("reduce"):
        score -= 10
        reasons.append("review_reduce_bias")
    elif bias.get("add"):
        score += 8
        reasons.append("review_add_bias")

    score = max(0.0, min(100.0, score))
    label = "excellent" if score >= 80 else ("good" if score >= 65 else ("watch" if score >= 45 else "avoid"))
    return {
        "quality_score": round(score, 1),
        "quality_label": label,
        "quality_reasons": reasons[:8],
    }


def _distance_bucket(distance_pct: Any) -> str:
    distance = _to_float(distance_pct, None)
    if distance is None:
        return "unknown"
    if distance < 3.0:
        return "lt_3"
    if distance < 5.0:
        return "3_5"
    if distance < 8.0:
        return "5_8"
    if distance < 12.0:
        return "8_12"
    return "gte_12"


def _phase_position_caps(phase_key: str | None) -> dict[str, float]:
    return dict(MONTHLY_GOAL_PHASE_POSITION_CAPS_BY_TIER.get(
        str(phase_key or "month_mid"),
        MONTHLY_GOAL_PHASE_POSITION_CAPS_BY_TIER["month_mid"],
    ))


def _planned_return_for_positions(positions: list[float]) -> float:
    return sum(
        positions[idx] * float(tier["return_pct"]) / 100.0
        for idx, tier in enumerate(MONTHLY_GOAL_TIERS)
    )


def _apply_phase_position_caps(positions: list[float], phase_key: str | None) -> list[float]:
    """按月内阶段限制进攻档，并把超额优先挪到更防守的 No 档。"""
    caps = _phase_position_caps(phase_key)
    adjusted = [max(0.0, float(p or 0.0)) for p in positions]
    excess = 0.0
    for idx, tier in enumerate(MONTHLY_GOAL_TIERS):
        cap = float(caps.get(str(tier["key"]), 100.0))
        if adjusted[idx] > cap:
            excess += adjusted[idx] - cap
            adjusted[idx] = cap

    for idx in (0, 1, 2, 3):
        if excess <= 1e-9:
            break
        tier_key = str(MONTHLY_GOAL_TIERS[idx]["key"])
        cap = float(caps.get(tier_key, 100.0))
        room = max(0.0, cap - adjusted[idx])
        move = min(room, excess)
        adjusted[idx] += move
        excess -= move
    return adjusted


def calculate_monthly_goal_tier_allocations(
    target_pct: float,
    target_position_overrides: dict[str, Any] | None = None,
    phase_key: str | None = None,
) -> dict[str, Any]:
    """把月度目标换算成不超过 100% 净值的分层目标仓位。"""
    target = max(0.0, float(target_pct or 0.0))
    positions = [
        float(tier["contribution_pct"]) / float(tier["return_pct"]) * 100.0
        if float(tier["return_pct"]) > 0 else 0.0
        for tier in MONTHLY_GOAL_TIERS
    ]
    min_positions = [
        float(MONTHLY_GOAL_MIN_POSITION_PCT_BY_TIER.get(str(tier["key"]), 0.0))
        for tier in MONTHLY_GOAL_TIERS
    ]

    def planned_return() -> float:
        return _planned_return_for_positions(positions)

    current = planned_return()

    def shift_position(from_idx: int, to_idx: int, remaining_delta: float) -> float:
        if remaining_delta <= 0:
            return 0.0
        from_return = float(MONTHLY_GOAL_TIERS[from_idx]["return_pct"])
        to_return = float(MONTHLY_GOAL_TIERS[to_idx]["return_pct"])
        effect_per_pct = abs(to_return - from_return) / 100.0
        if effect_per_pct <= 0:
            return 0.0
        available_pct = max(0.0, positions[from_idx] - min_positions[from_idx])
        move_pct = min(available_pct, remaining_delta / effect_per_pct)
        positions[from_idx] -= move_pct
        positions[to_idx] += move_pct
        return move_pct * effect_per_pct

    if target > current:
        remaining = target - current
        for from_idx, to_idx in ((0, 1), (1, 2), (2, 3)):
            remaining -= shift_position(from_idx, to_idx, remaining)
            if remaining <= 1e-9:
                break
    elif target < current:
        remaining = current - target
        for from_idx, to_idx in ((3, 2), (2, 1), (1, 0)):
            remaining -= shift_position(from_idx, to_idx, remaining)
            if remaining <= 1e-9:
                break
        current = planned_return()
        if target < current and positions[0] > 0 and float(MONTHLY_GOAL_TIERS[0]["return_pct"]) > 0:
            # 目标低于最低全仓收益时，保留现金，仅配置一部分舒服高价 No。
            positions[0] *= target / current if current > 0 else 0.0

    target_model_positions = [max(0.0, float(p or 0.0)) for p in positions]
    phase_suggested_positions = _apply_phase_position_caps(target_model_positions, phase_key)
    normalized_position_overrides = _normalize_target_position_overrides(target_position_overrides or {})
    if normalized_position_overrides:
        for idx, tier in enumerate(MONTHLY_GOAL_TIERS):
            tier_key = str(tier["key"])
            if tier_key in normalized_position_overrides:
                positions[idx] = normalized_position_overrides[tier_key]
    else:
        positions = list(phase_suggested_positions)

    allocations = []
    planned = _planned_return_for_positions(positions)
    effective_positions = [
        min(max(0.0, float(positions[idx] or 0.0)), max(0.0, float(phase_suggested_positions[idx] or 0.0)))
        for idx in range(len(MONTHLY_GOAL_TIERS))
    ]
    effective_planned = _planned_return_for_positions(effective_positions)
    for idx, tier in enumerate(MONTHLY_GOAL_TIERS):
        position_pct = max(0.0, positions[idx])
        contribution_pct = position_pct * float(tier["return_pct"]) / 100.0
        phase_position_pct = max(0.0, phase_suggested_positions[idx])
        effective_position_pct = max(0.0, effective_positions[idx])
        effective_contribution_pct = effective_position_pct * float(tier["return_pct"]) / 100.0
        allocations.append({
            "tier_key": tier["key"],
            "target_position_pct": position_pct,
            "target_contribution_pct": contribution_pct,
            "target_profit_share_pct": (
                contribution_pct / planned * 100.0 if planned > 0 else 0.0
            ),
            "target_model_position_pct": target_model_positions[idx],
            "phase_suggested_position_pct": phase_position_pct,
            "phase_position_cap_pct": float(_phase_position_caps(phase_key).get(str(tier["key"]), 100.0)),
            "effective_position_cap_pct": effective_position_pct,
            "effective_target_contribution_pct": effective_contribution_pct,
            "effective_target_profit_share_pct": (
                effective_contribution_pct / effective_planned * 100.0 if effective_planned > 0 else 0.0
            ),
            "exceeds_phase_suggestion": position_pct > phase_position_pct + 1e-9,
        })
    return {
        "target_pct": target,
        "planned_return_pct": planned,
        "effective_planned_return_pct": effective_planned,
        "phase_key": phase_key or "month_mid",
        "phase_position_caps": _phase_position_caps(phase_key),
        "allocation_source": "dashboard_custom_positions" if normalized_position_overrides else "target_model",
        "target_position_overrides": normalized_position_overrides,
        "target_feasible_without_leverage": sum(item["target_position_pct"] for item in allocations) <= 100.000001,
        "target_return_matches_goal": abs(effective_planned - target) <= 1e-6,
        "total_position_pct": sum(item["target_position_pct"] for item in allocations),
        "effective_total_position_pct": sum(item["effective_position_cap_pct"] for item in allocations),
        "allocations": allocations,
    }


def ensure_monthly_goal_settings_table() -> None:
    """保存 Dashboard 本月目标设置，供跨浏览器和离线 AI 分析读取。"""
    global _MONTHLY_GOAL_SETTINGS_READY
    if _MONTHLY_GOAL_SETTINGS_READY:
        return
    with get_conn(autocommit=True) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS monthly_goal_settings (
                profile TEXT NOT NULL,
                month_label TEXT NOT NULL,
                target_pct DOUBLE PRECISION NOT NULL,
                realized_overrides JSONB NOT NULL DEFAULT '{}'::jsonb,
                source TEXT NOT NULL DEFAULT 'dashboard',
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (profile, month_label)
            )
            """
        )
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS monthly_goal_settings_updated_idx
            ON monthly_goal_settings(updated_at DESC)
            """
        )
        cur.execute(
            """
            ALTER TABLE monthly_goal_settings
            ADD COLUMN IF NOT EXISTS realized_overrides JSONB NOT NULL DEFAULT '{}'::jsonb
            """
        )
        cur.execute(
            """
            ALTER TABLE monthly_goal_settings
            ADD COLUMN IF NOT EXISTS target_position_overrides JSONB NOT NULL DEFAULT '{}'::jsonb
            """
        )
    _MONTHLY_GOAL_SETTINGS_READY = True


def _normalize_realized_overrides(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for key in ACTIONABLE_TIER_LABELS:
        if key not in value:
            continue
        number = _to_float(value.get(key), None)
        if number is not None:
            out[key] = float(number)
    return out


def _normalize_target_position_overrides(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for tier in MONTHLY_GOAL_TIERS:
        key = str(tier["key"])
        if key not in value:
            continue
        number = _to_float(value.get(key), None)
        if number is None:
            continue
        out[key] = min(100.0, max(0.0, float(number)))
    return out


def get_monthly_goal_target_pct(
    *,
    profile: str = "analyze",
    month_label: str | None = None,
    default: float = MONTHLY_GOAL_DEFAULT_TARGET_PCT,
) -> dict[str, Any]:
    """读取本月目标设置；未保存时返回默认目标。"""
    ensure_monthly_goal_settings_table()
    _, _, current_month_label = _current_et_month_bounds()
    label = month_label or current_month_label
    fill_profile = str(profile or "analyze").strip() or "analyze"
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT target_pct, realized_overrides, target_position_overrides, source, updated_at
            FROM monthly_goal_settings
            WHERE profile = %s AND month_label = %s
            """,
            (fill_profile, label),
        )
        row = cur.fetchone()
    if row:
        position_overrides = _normalize_target_position_overrides(row.get("target_position_overrides") or {})
        return {
            "profile": fill_profile,
            "month_label": label,
            "target_pct": float(row["target_pct"]),
            "realized_overrides": _normalize_realized_overrides(row.get("realized_overrides") or {}),
            "has_realized_overrides": bool(_normalize_realized_overrides(row.get("realized_overrides") or {})),
            "target_position_overrides": position_overrides,
            "has_target_position_overrides": bool(position_overrides),
            "source": row.get("source") or "dashboard",
            "saved": True,
            "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
        }
    return {
        "profile": fill_profile,
        "month_label": label,
        "target_pct": float(default),
        "realized_overrides": {},
        "has_realized_overrides": False,
        "target_position_overrides": {},
        "has_target_position_overrides": False,
        "source": "backend_default",
        "saved": False,
        "updated_at": None,
    }


def save_monthly_goal_target_pct(
    *,
    target_pct: float,
    realized_overrides: dict[str, Any] | None = None,
    target_position_overrides: dict[str, Any] | None = None,
    profile: str = "analyze",
    month_label: str | None = None,
    source: str = "dashboard",
) -> dict[str, Any]:
    """保存本月目标设置。"""
    value = float(target_pct)
    if value <= 0:
        raise ValueError("target_pct must be positive")
    ensure_monthly_goal_settings_table()
    _, _, current_month_label = _current_et_month_bounds()
    label = month_label or current_month_label
    fill_profile = str(profile or "analyze").strip() or "analyze"
    setting_source = str(source or "dashboard").strip()[:64] or "dashboard"
    normalized_overrides = _normalize_realized_overrides(realized_overrides or {})
    normalized_position_overrides = _normalize_target_position_overrides(target_position_overrides or {})
    with get_cursor() as cur:
        cur.execute(
            """
            INSERT INTO monthly_goal_settings (
                profile, month_label, target_pct, realized_overrides, target_position_overrides, source, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
            ON CONFLICT (profile, month_label)
            DO UPDATE SET
                target_pct = EXCLUDED.target_pct,
                realized_overrides = EXCLUDED.realized_overrides,
                target_position_overrides = EXCLUDED.target_position_overrides,
                source = EXCLUDED.source,
                updated_at = NOW()
            RETURNING target_pct, realized_overrides, target_position_overrides, source, updated_at
            """,
            (
                fill_profile,
                label,
                value,
                psycopg2.extras.Json(normalized_overrides),
                psycopg2.extras.Json(normalized_position_overrides),
                setting_source,
            ),
        )
        row = cur.fetchone()
    saved_overrides = _normalize_realized_overrides(row.get("realized_overrides") or {})
    saved_position_overrides = _normalize_target_position_overrides(row.get("target_position_overrides") or {})
    return {
        "profile": fill_profile,
        "month_label": label,
        "target_pct": float(row["target_pct"]),
        "realized_overrides": saved_overrides,
        "has_realized_overrides": bool(saved_overrides),
        "target_position_overrides": saved_position_overrides,
        "has_target_position_overrides": bool(saved_position_overrides),
        "source": row.get("source") or setting_source,
        "saved": True,
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


def ensure_fill_attribution_columns() -> None:
    """补齐 advisory_chain_fills 的等级归因列，允许老库幂等升级。"""
    global _ATTRIBUTION_COLUMNS_READY
    if _ATTRIBUTION_COLUMNS_READY:
        return
    with get_conn(autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            """
            ALTER TABLE advisory_chain_fills
                ADD COLUMN IF NOT EXISTS entry_tier_key TEXT,
                ADD COLUMN IF NOT EXISTS entry_tier_label TEXT,
                ADD COLUMN IF NOT EXISTS tier_snapshot JSONB;
            CREATE INDEX IF NOT EXISTS advisory_chain_fills_entry_tier_idx
                ON advisory_chain_fills (entry_tier_key, fill_timestamp DESC);
            """
        )
    _ATTRIBUTION_COLUMNS_READY = True


def _classify_time_phase(days_left_in_month: float) -> tuple[str, str]:
    if days_left_in_month <= 7.0:
        return "month_end", "月末"
    if days_left_in_month <= 16.0:
        return "month_mid", "月中"
    return "month_start", "月初"


def _month_end_for(dt_utc: datetime) -> datetime:
    dt_et = dt_utc.astimezone(ET_TIMEZONE)
    if dt_et.month == 12:
        return dt_et.replace(year=dt_et.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return dt_et.replace(month=dt_et.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def _parse_datetime(raw: Any) -> datetime | None:
    if raw in (None, ""):
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        text = str(raw).strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET_TIMEZONE)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _days_left(as_of_utc: datetime, end_utc: datetime | None) -> float:
    end_et = (end_utc or _month_end_for(as_of_utc)).astimezone(ET_TIMEZONE)
    as_of_et = as_of_utc.astimezone(ET_TIMEZONE)
    return max(0.0, (end_et - as_of_et).total_seconds() / 86400.0)


def _tier_key_for(
    *,
    outcome: str,
    yes_price: float | None,
    no_price: float | None,
    distance_pct: float,
    days_left: float,
) -> str | None:
    phase_key, _phase_label = _classify_time_phase(days_left)
    d = distance_pct

    if phase_key == "month_start":
        comfy_high_no = no_price is not None and no_price >= 0.90 and d >= 12.0
        high_no = no_price is not None and no_price >= 0.82 and d >= 10.0
        mid_no = no_price is not None and 0.65 <= no_price < 0.82 and 5.0 <= d <= 12.0
        low_yes = yes_price is not None and 0.10 <= yes_price <= 0.25 and 5.0 <= d <= 10.0
    elif phase_key == "month_mid":
        comfy_high_no = no_price is not None and no_price >= 0.88 and d >= 10.0
        high_no = no_price is not None and no_price >= 0.82 and d >= 8.0
        mid_no = no_price is not None and 0.62 <= no_price < 0.82 and 4.0 <= d <= 10.0
        low_yes = yes_price is not None and 0.10 <= yes_price <= 0.30 and 3.0 <= d <= 8.0
    else:
        comfy_high_no = no_price is not None and no_price >= 0.85 and d >= 8.0
        high_no = no_price is not None and no_price >= 0.75 and d >= 6.0
        mid_no = no_price is not None and 0.58 <= no_price < 0.78 and 3.0 <= d <= 8.0
        low_yes = yes_price is not None and 0.08 <= yes_price <= 0.22 and 2.0 <= d <= 5.0

    normalized = outcome.strip().lower()
    if normalized == "no":
        if comfy_high_no:
            return "comfy_high_no"
        if high_no:
            return "high_no"
        if mid_no:
            return "mid_no"
    if normalized == "yes" and low_yes:
        return "low_yes"
    return None


def _current_et_month_bounds(now: datetime | None = None) -> tuple[datetime, datetime, str]:
    now_et = (now or datetime.now(ET_TIMEZONE)).astimezone(ET_TIMEZONE)
    start_et = now_et.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_et.month == 12:
        end_et = start_et.replace(year=start_et.year + 1, month=1)
    else:
        end_et = start_et.replace(month=start_et.month + 1)
    return start_et, end_et, start_et.strftime("%Y-%m")


def _monthly_btc_event_slug(month_start_et: datetime) -> str:
    month_name = month_start_et.strftime("%B").lower()
    return f"what-price-will-bitcoin-hit-in-{month_name}-{month_start_et.year}"


def _to_float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        if value is None:
            return default
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _ensure_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _position_value(position: dict) -> tuple[float, float]:
    size = _to_float(position.get("size"), 0.0) or 0.0
    current_value = _to_float(position.get("currentValue"), None)
    if current_value is None:
        cur_price = _to_float(position.get("curPrice"), 0.0) or 0.0
        current_value = size * cur_price
    return size, current_value


def _position_match_key(question: Any, outcome: Any) -> tuple[str, str]:
    return (str(question or "").strip().lower(), str(outcome or "").strip().lower())


def _build_position_indexes(positions: list | None) -> tuple[dict[str, dict], dict[tuple[str, str], dict]]:
    by_token: dict[str, dict] = {}
    by_question_outcome: dict[tuple[str, str], dict] = {}
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        size, value = _position_value(pos)
        entry = {"shares": size, "value": value}
        token_id = str(pos.get("asset") or pos.get("token_id") or pos.get("tokenId") or "").strip()
        if token_id:
            current = by_token.setdefault(token_id, {"shares": 0.0, "value": 0.0})
            current["shares"] += size
            current["value"] += value
        question = pos.get("title") or pos.get("question") or pos.get("market") or pos.get("condition")
        outcome = pos.get("outcome") or pos.get("side")
        key = _position_match_key(question, outcome)
        if key[0] and key[1]:
            current = by_question_outcome.setdefault(key, {"shares": 0.0, "value": 0.0})
            current["shares"] += entry["shares"]
            current["value"] += entry["value"]
    return by_token, by_question_outcome


def _market_is_settled(market: dict) -> bool:
    for key in ("resolved", "isResolved", "redeemed", "isRedeemed", "closed", "isClosed"):
        if bool(market.get(key)):
            return True
    status_text = str(market.get("status") or "").strip().lower()
    if status_text in {"resolved", "redeemed", "closed", "settled", "expired"}:
        return True
    if market.get("active") is False:
        return True
    prices = _ensure_list(market.get("outcomePrices"))
    for raw in prices:
        price = _to_float(raw, None)
        if price is not None and abs(price - 1.0) <= 1e-9:
            return True
    return False


def _entry_price_for_outcome(market: dict, outcome_index: int) -> float | None:
    for field in ("bestAsks", "outcomePrices", "bestBids"):
        values = _ensure_list(market.get(field))
        raw = values[outcome_index] if outcome_index < len(values) else None
        price = _to_float(raw, None)
        if price is not None and 0 < price < 1:
            return price
    return None


def _market_entries_by_token(
    *,
    markets: list,
    current_btc_price: float | None,
    days_left_in_month: float,
) -> dict[str, dict[str, Any]]:
    """按当前 event 快照给 token 补充当前价格/距离/四档归属。"""
    out: dict[str, dict[str, Any]] = {}
    if current_btc_price is None or current_btc_price <= 0:
        return out
    for market in markets or []:
        if not isinstance(market, dict) or _market_is_settled(market):
            continue
        question = str(market.get("question") or "")
        strike, direction = _extract_strike_and_direction(question)
        yes_price, no_price = _parse_market_prices(market)
        if strike is None or direction == "unknown" or yes_price is None or no_price is None:
            continue
        outcomes = _ensure_list(market.get("outcomes"))
        token_ids = _ensure_list(market.get("token_id") or market.get("clobTokenIds"))
        distance_pct = abs(strike - float(current_btc_price)) / float(current_btc_price) * 100.0
        for idx, token_id_raw in enumerate(token_ids):
            token_id = str(token_id_raw or "").strip()
            if not token_id:
                continue
            outcome = str(outcomes[idx]) if idx < len(outcomes) else ""
            price = _entry_price_for_outcome(market, idx)
            current_tier_key = _tier_key_for(
                outcome=outcome,
                yes_price=yes_price,
                no_price=no_price,
                distance_pct=distance_pct,
                days_left=days_left_in_month,
            )
            out[token_id] = {
                "question": question,
                "outcome": outcome,
                "token_id": token_id,
                "price": round(price, 6) if price is not None else None,
                "strike": round(float(strike), 2),
                "direction_in_question": direction,
                "distance_pct": round(distance_pct, 2),
                "yes_price": round(yes_price, 6),
                "no_price": round(no_price, 6),
                "current_tier_key": current_tier_key,
                "current_tier_label": ACTIONABLE_TIER_LABELS.get(current_tier_key),
            }
    return out


def _intent_from_pending_orders(active_manual_pending_orders: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    by_token: dict[str, dict[str, Any]] = {}
    if not isinstance(active_manual_pending_orders, dict):
        return by_token
    for order in active_manual_pending_orders.get("orders") or []:
        if not isinstance(order, dict):
            continue
        token_id = str(order.get("token_id") or "").strip()
        tier_key = str(order.get("intent_tier_key") or "").strip()
        if not token_id or tier_key not in ACTIONABLE_TIER_LABELS:
            continue
        current = by_token.setdefault(token_id, {
            "intent_tier_key": tier_key,
            "intent_tier_label": order.get("intent_tier_label") or ACTIONABLE_TIER_LABELS.get(tier_key),
            "source": "manual_pending_intent",
            "pending_order_ids": [],
            "pending_plan_ids": [],
        })
        if order.get("id") is not None:
            current["pending_order_ids"].append(order.get("id"))
        if order.get("plan_id") is not None and order.get("plan_id") not in current["pending_plan_ids"]:
            current["pending_plan_ids"].append(order.get("plan_id"))
    return by_token


def _intent_from_open_lots(trade_review_summary: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    by_token: dict[str, dict[str, Any]] = {}
    if not isinstance(trade_review_summary, dict):
        return by_token
    for lot in trade_review_summary.get("open_lots") or []:
        if not isinstance(lot, dict):
            continue
        token_id = str(lot.get("token_id") or "").strip()
        tier_key = str(lot.get("tier_key") or "").strip()
        if not token_id or tier_key not in ACTIONABLE_TIER_LABELS:
            continue
        cost = _to_float(lot.get("remaining_cost"), 0.0) or 0.0
        current = by_token.get(token_id)
        if current and float(current.get("remaining_cost_usdc") or 0.0) >= cost:
            continue
        by_token[token_id] = {
            "intent_tier_key": tier_key,
            "intent_tier_label": lot.get("tier_label") or ACTIONABLE_TIER_LABELS.get(tier_key),
            "source": "open_lot_entry_tier",
            "remaining_cost_usdc": round(cost, 2),
            "buy_timestamp": lot.get("buy_timestamp"),
        }
    return by_token


def _open_lot_intents_by_token(trade_review_summary: dict[str, Any] | None) -> dict[str, list[dict[str, Any]]]:
    by_token: dict[str, dict[str, dict[str, Any]]] = {}
    if not isinstance(trade_review_summary, dict):
        return {}
    for lot in trade_review_summary.get("open_lots") or []:
        if not isinstance(lot, dict):
            continue
        token_id = str(lot.get("token_id") or "").strip()
        tier_key = str(lot.get("tier_key") or "").strip()
        if not token_id or tier_key not in ACTIONABLE_TIER_LABELS:
            continue
        cost = _to_float(lot.get("remaining_cost"), 0.0) or 0.0
        if cost <= 0:
            continue
        bucket = by_token.setdefault(token_id, {})
        row = bucket.setdefault(tier_key, {
            "intent_tier_key": tier_key,
            "intent_tier_label": lot.get("tier_label") or ACTIONABLE_TIER_LABELS.get(tier_key),
            "source": "open_lot_entry_tier",
            "remaining_cost_usdc": 0.0,
            "buy_timestamps": [],
        })
        row["remaining_cost_usdc"] += cost
        if lot.get("buy_timestamp"):
            row["buy_timestamps"].append(lot.get("buy_timestamp"))
    return {
        token_id: sorted(
            (
                {**row, "remaining_cost_usdc": round(float(row.get("remaining_cost_usdc") or 0.0), 2)}
                for row in tiers.values()
            ),
            key=lambda item: float(item.get("remaining_cost_usdc") or 0.0),
            reverse=True,
        )
        for token_id, tiers in by_token.items()
    }


def _pending_buy_intents_by_token_tier(active_manual_pending_orders: dict[str, Any] | None) -> dict[str, dict[str, dict[str, Any]]]:
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if not isinstance(active_manual_pending_orders, dict):
        return out
    for order in active_manual_pending_orders.get("orders") or []:
        if not isinstance(order, dict):
            continue
        if str(order.get("action") or "").strip().lower() != "buy":
            continue
        if str(order.get("status") or "pending").strip().lower() not in {"pending", "executing"}:
            continue
        token_id = str(order.get("token_id") or "").strip()
        tier_key = str(order.get("intent_tier_key") or "").strip()
        if not token_id or tier_key not in ACTIONABLE_TIER_LABELS:
            continue
        row = out.setdefault(token_id, {}).setdefault(tier_key, {
            "pending_buy_notional_usdc": 0.0,
            "pending_order_ids": [],
            "pending_plan_ids": [],
        })
        row["pending_buy_notional_usdc"] += _pending_order_buy_notional(order)
        if order.get("id") is not None:
            row["pending_order_ids"].append(order.get("id"))
        if order.get("plan_id") is not None and order.get("plan_id") not in row["pending_plan_ids"]:
            row["pending_plan_ids"].append(order.get("plan_id"))
    return out


def _held_allocations_by_token_tier(
    *,
    positions: list | None,
    open_lot_intents: dict[str, list[dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    by_token, _by_question = _build_position_indexes(positions)
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for token_id, held in by_token.items():
        held_value = float(held.get("value") or 0.0)
        held_shares = float(held.get("shares") or 0.0)
        lots = open_lot_intents.get(token_id) or []
        total_cost = sum(float(lot.get("remaining_cost_usdc") or 0.0) for lot in lots)
        if held_value <= 0 and held_shares <= 0:
            continue
        if total_cost <= 0:
            continue
        for lot in lots:
            tier_key = str(lot.get("intent_tier_key") or "")
            if tier_key not in ACTIONABLE_TIER_LABELS:
                continue
            ratio = float(lot.get("remaining_cost_usdc") or 0.0) / total_cost
            row = out.setdefault(token_id, {}).setdefault(tier_key, {
                "held_value_usdc": 0.0,
                "held_shares": 0.0,
                "remaining_cost_usdc": 0.0,
                "intent_tier_label": lot.get("intent_tier_label") or ACTIONABLE_TIER_LABELS.get(tier_key),
                "intent_source": lot.get("source") or "open_lot_entry_tier",
            })
            row["held_value_usdc"] += held_value * ratio
            row["held_shares"] += held_shares * ratio
            row["remaining_cost_usdc"] += float(lot.get("remaining_cost_usdc") or 0.0)
    for tiers in out.values():
        for row in tiers.values():
            row["held_value_usdc"] = round(float(row.get("held_value_usdc") or 0.0), 2)
            row["held_shares"] = round(float(row.get("held_shares") or 0.0), 6)
            row["remaining_cost_usdc"] = round(float(row.get("remaining_cost_usdc") or 0.0), 2)
    return out


def _auxiliary_group_for(entry: dict[str, Any] | None) -> tuple[str, str, str] | None:
    if not isinstance(entry, dict):
        return ("unknown", "未识别", "当前 event 中未找到该 token，无法计算距离/价格")
    outcome = str(entry.get("outcome") or "").strip().lower()
    question = str(entry.get("question") or "").strip().lower()
    distance = _to_float(entry.get("distance_pct"), None)
    price = _to_float(entry.get("price"), None)
    direction = str(entry.get("direction_in_question") or "").strip().lower()
    if outcome == "yes" and direction == "below" and distance is not None and distance >= 10.0:
        return ("tail_hedge", "尾部下跌保险", "远距离 dip Yes，作为尾部保险/凸性仓，不参与四档补仓目标")
    if distance is not None and distance <= 3.0:
        return ("near_barrier_tactical", "近障碍战术仓", "距离 barrier 很近，属于高波动战术/退出管理，不参与稳定目标补仓")
    if outcome == "no" and direction == "above" and price is not None and price < 0.58:
        return ("near_barrier_tactical", "近障碍战术仓", "No 价格低于主战区下沿，更偏战术交易或风险处理")
    if outcome == "yes" and direction == "below" and price is not None and price > 0.30:
        return ("near_barrier_tactical", "近障碍战术仓", "dip Yes 价格高于低价Yes区间，更偏风险处理")
    return ("other_auxiliary", "其他辅助仓", "不属于当前四档主战区，但可保留为观察/人工管理对象")


def _build_exposure_classification(
    *,
    positions: list | None,
    markets: list,
    current_btc_price: float | None,
    days_left_in_month: float,
    active_manual_pending_orders: dict[str, Any] | None,
    trade_review_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    market_by_token = _market_entries_by_token(
        markets=markets,
        current_btc_price=current_btc_price,
        days_left_in_month=days_left_in_month,
    )
    pending_intents = _intent_from_pending_orders(active_manual_pending_orders)
    open_lot_intents = _open_lot_intents_by_token(trade_review_summary)
    held_by_token_tier = _held_allocations_by_token_tier(
        positions=positions,
        open_lot_intents=open_lot_intents,
    )
    core_items: list[dict[str, Any]] = []
    auxiliary_items: list[dict[str, Any]] = []
    grouped_aux: dict[str, dict[str, Any]] = {}
    by_token, _by_question = _build_position_indexes(positions)

    for token_id, held in by_token.items():
        held_value = float(held.get("value") or 0.0)
        held_shares = float(held.get("shares") or 0.0)
        if held_value < 0.01 and held_shares < 1e-9:
            continue
        entry = market_by_token.get(token_id)
        tier_allocations = held_by_token_tier.get(token_id) or {}
        if tier_allocations:
            for intent_key, allocation in tier_allocations.items():
                if intent_key not in ACTIONABLE_TIER_LABELS:
                    continue
                current_key = entry.get("current_tier_key") if entry else None
                if current_key == intent_key:
                    band_status = "in_band"
                    band_label = "当前仍在意图档位区间"
                elif current_key:
                    band_status = "shifted_band"
                    band_label = f"当前落入 {ACTIONABLE_TIER_LABELS.get(current_key, current_key)}"
                elif entry:
                    band_status = "out_of_band"
                    band_label = "当前已滑出四档主战区"
                else:
                    band_status = "unknown"
                    band_label = "当前 event 未匹配，无法判断是否出带"
                core_items.append({
                    "token_id": token_id,
                    "question": (entry or {}).get("question"),
                    "outcome": (entry or {}).get("outcome"),
                    "intent_tier_key": intent_key,
                    "intent_tier_label": allocation.get("intent_tier_label") or ACTIONABLE_TIER_LABELS.get(intent_key),
                    "intent_source": allocation.get("intent_source"),
                    "current_tier_key": current_key,
                    "current_tier_label": ACTIONABLE_TIER_LABELS.get(current_key),
                    "band_status": band_status,
                    "band_label": band_label,
                    "held_shares": round(float(allocation.get("held_shares") or 0.0), 6),
                    "held_value_usdc": round(float(allocation.get("held_value_usdc") or 0.0), 2),
                    "remaining_cost_usdc": round(float(allocation.get("remaining_cost_usdc") or 0.0), 2),
                    "price": (entry or {}).get("price"),
                    "strike": (entry or {}).get("strike"),
                    "distance_pct": (entry or {}).get("distance_pct"),
                    "pending_order_ids": [],
                    "pending_plan_ids": [],
                })
            continue
        intent = pending_intents.get(token_id)
        if intent and intent.get("intent_tier_key") in ACTIONABLE_TIER_LABELS:
            intent_key = str(intent["intent_tier_key"])
            current_key = entry.get("current_tier_key") if entry else None
            if current_key == intent_key:
                band_status = "in_band"
                band_label = "当前仍在意图档位区间"
            elif current_key:
                band_status = "shifted_band"
                band_label = f"当前落入 {ACTIONABLE_TIER_LABELS.get(current_key, current_key)}"
            elif entry:
                band_status = "out_of_band"
                band_label = "当前已滑出四档主战区"
            else:
                band_status = "unknown"
                band_label = "当前 event 未匹配，无法判断是否出带"
            core_items.append({
                "token_id": token_id,
                "question": (entry or {}).get("question"),
                "outcome": (entry or {}).get("outcome"),
                "intent_tier_key": intent_key,
                "intent_tier_label": intent.get("intent_tier_label") or ACTIONABLE_TIER_LABELS.get(intent_key),
                "intent_source": intent.get("source"),
                "current_tier_key": current_key,
                "current_tier_label": ACTIONABLE_TIER_LABELS.get(current_key),
                "band_status": band_status,
                "band_label": band_label,
                "held_shares": round(held_shares, 6),
                "held_value_usdc": round(held_value, 2),
                "remaining_cost_usdc": None,
                "price": (entry or {}).get("price"),
                "strike": (entry or {}).get("strike"),
                "distance_pct": (entry or {}).get("distance_pct"),
                "pending_order_ids": list(intent.get("pending_order_ids") or [])[:8],
                "pending_plan_ids": list(intent.get("pending_plan_ids") or [])[:8],
            })
            continue

        aux = _auxiliary_group_for(entry)
        if aux is None:
            continue
        group_key, group_label, reason = aux
        item = {
            "token_id": token_id,
            "question": (entry or {}).get("question"),
            "outcome": (entry or {}).get("outcome"),
            "group_key": group_key,
            "group_label": group_label,
            "reason": reason,
            "held_shares": round(held_shares, 6),
            "held_value_usdc": round(held_value, 2),
            "price": (entry or {}).get("price"),
            "strike": (entry or {}).get("strike"),
            "distance_pct": (entry or {}).get("distance_pct"),
        }
        auxiliary_items.append(item)
        group = grouped_aux.setdefault(group_key, {
            "group_key": group_key,
            "group_label": group_label,
            "held_value_usdc": 0.0,
            "held_shares": 0.0,
            "position_count": 0,
            "items": [],
        })
        group["held_value_usdc"] += held_value
        group["held_shares"] += held_shares
        group["position_count"] += 1
        group["items"].append(item)

    core_items.sort(key=lambda item: float(item.get("held_value_usdc") or 0.0), reverse=True)
    auxiliary_items.sort(key=lambda item: float(item.get("held_value_usdc") or 0.0), reverse=True)
    groups = []
    for group in grouped_aux.values():
        group["held_value_usdc"] = round(float(group.get("held_value_usdc") or 0.0), 2)
        group["held_shares"] = round(float(group.get("held_shares") or 0.0), 6)
        group["items"] = sorted(
            group["items"],
            key=lambda item: float(item.get("held_value_usdc") or 0.0),
            reverse=True,
        )[:8]
        groups.append(group)
    groups.sort(key=lambda item: float(item.get("held_value_usdc") or 0.0), reverse=True)
    return {
        "source": "entry_intent_plus_current_band",
        "basis_note": (
            "主分类优先使用挂单/成交时 intent_tier；当前行情只作为 in_band/out_of_band 状态。"
            "辅助仓位不参与四档目标收益和新增补仓余量。"
        ),
        "core_positions": core_items[:20],
        "core_position_count": len(core_items),
        "out_of_band_count": sum(1 for item in core_items if item.get("band_status") in {"out_of_band", "shifted_band"}),
        "auxiliary_positions": auxiliary_items[:20],
        "auxiliary_position_count": len(auxiliary_items),
        "auxiliary_groups": groups,
    }


def _current_phase_thresholds(phase_key: str) -> dict[str, str]:
    if phase_key == "month_start":
        return {
            "comfy_high_no": "No>=0.90 且 distance>=12%",
            "high_no": "No>=0.82 且 distance>=10%",
            "mid_no": "0.65<=No<0.82 且 5%<=distance<=12%",
            "low_yes": "0.10<=Yes<=0.25 且 5%<=distance<=10%",
        }
    if phase_key == "month_mid":
        return {
            "comfy_high_no": "No>=0.88 且 distance>=10%",
            "high_no": "No>=0.82 且 distance>=8%",
            "mid_no": "0.62<=No<0.82 且 4%<=distance<=10%",
            "low_yes": "0.10<=Yes<=0.30 且 3%<=distance<=8%",
        }
    return {
        "comfy_high_no": "No>=0.85 且 distance>=8%",
        "high_no": "No>=0.75 且 distance>=6%",
        "mid_no": "0.58<=No<0.78 且 3%<=distance<=8%",
        "low_yes": "0.08<=Yes<=0.22 且 2%<=distance<=5%",
    }


def _btc_momentum_direction(btc_momentum_context: dict[str, Any] | None) -> tuple[str, bool]:
    if not isinstance(btc_momentum_context, dict):
        return "neutral", False
    direction = str(btc_momentum_context.get("direction") or "neutral").lower()
    if direction not in {"up", "down", "neutral"}:
        direction = "neutral"
    fast = bool(btc_momentum_context.get("fast_move_toward_barrier") or btc_momentum_context.get("fast_move"))
    return direction, fast


def _mid_no_entry_gate(
    *,
    direction_in_question: str,
    distance_pct: float,
    phase_key: str,
    btc_momentum_context: dict[str, Any] | None,
) -> dict[str, Any]:
    rules = MONTHLY_GOAL_MID_NO_ENTRY_RULES.get(phase_key, MONTHLY_GOAL_MID_NO_ENTRY_RULES["month_mid"])
    allow_distance = float(rules["allow_distance_pct"])
    block_distance = float(rules["block_distance_pct"])
    status = "allow"
    reasons: list[str] = []

    if distance_pct < block_distance:
        status = "block"
        reasons.append(f"distance {distance_pct:.1f}% 已低于中价No硬门槛 {block_distance:.1f}%")
    elif distance_pct < allow_distance:
        status = "caution"
        reasons.append(f"distance {distance_pct:.1f}% 未达到中价No正常门槛 {allow_distance:.1f}%")

    momentum_direction, fast_move = _btc_momentum_direction(btc_momentum_context)
    approaching = (
        (direction_in_question == "above" and momentum_direction == "up")
        or (direction_in_question == "below" and momentum_direction == "down")
    )
    if approaching and fast_move and distance_pct <= allow_distance + 2.0:
        status = "block"
        reasons.append("BTC 正快速朝 barrier 方向移动，需要冷却确认后再考虑中价No")
    elif approaching:
        status = "caution" if status == "allow" else status
        reasons.append("BTC 短线方向朝 barrier 移动，中价No只能小仓位/挂更保守价格")

    if not reasons:
        reasons.append("distance 和短线方向均满足中价No确认")
    return {
        "status": status,
        "distance_allow_pct": allow_distance,
        "distance_block_pct": block_distance,
        "btc_momentum_direction": momentum_direction,
        "fast_move_toward_barrier": bool(approaching and fast_move),
        "reasons": reasons,
    }


def classify_entry_tier(
    *,
    question: str,
    outcome: str,
    fill_price: float,
    btc_price: float | None,
    as_of_utc: datetime,
    end_utc: datetime | None = None,
) -> dict[str, Any]:
    """按成交时点锁定买入 lot 的策略等级；非主战区返回 tier_key=None。"""
    snapshot: dict[str, Any] = {
        "question": question,
        "outcome": outcome,
        "fill_price": fill_price,
        "btc_price": btc_price,
        "attribution_basis": "fill_price_outcome_distance_days_left",
        "review_snapshot_version": MONTHLY_REVIEW_SNAPSHOT_VERSION,
        "as_of_utc": as_of_utc.astimezone(timezone.utc).isoformat(),
        "end_utc": end_utc.astimezone(timezone.utc).isoformat() if end_utc else None,
        "tier_key": None,
        "tier_label": None,
        "distance_bucket": "unknown",
        "entry_gate": "unknown",
        "entry_gate_reasons": [],
        "reason": None,
    }
    try:
        price = float(fill_price)
    except (TypeError, ValueError):
        snapshot["reason"] = "invalid_fill_price"
        return snapshot
    if not (0 < price < 1):
        snapshot["reason"] = "invalid_fill_price"
        return snapshot
    if btc_price is None or btc_price <= 0:
        snapshot["reason"] = "missing_btc_price"
        return snapshot

    strike, direction = _extract_strike_and_direction(question or "")
    if strike is None or direction == "unknown":
        snapshot["reason"] = "unknown_barrier"
        return snapshot

    normalized = str(outcome or "").strip().lower()
    if normalized == "yes":
        yes_price, no_price = price, 1.0 - price
    elif normalized == "no":
        yes_price, no_price = 1.0 - price, price
    else:
        snapshot["reason"] = "unknown_outcome"
        return snapshot

    distance_pct = abs(strike - float(btc_price)) / float(btc_price) * 100.0
    days_left = _days_left(as_of_utc, end_utc)
    phase_key, phase_label = _classify_time_phase(days_left)
    tier_key = _tier_key_for(
        outcome=outcome,
        yes_price=yes_price,
        no_price=no_price,
        distance_pct=distance_pct,
        days_left=days_left,
    )
    if tier_key == "mid_no":
        gate = _mid_no_entry_gate(
            direction_in_question=direction,
            distance_pct=distance_pct,
            phase_key=phase_key,
            btc_momentum_context=None,
        )
        entry_gate = str(gate.get("status") or "unknown")
        entry_gate_reasons = list(gate.get("reasons") or [])
    elif tier_key:
        entry_gate = "allow"
        entry_gate_reasons = ["成交时符合该档价格/距离/阶段规则"]
    else:
        entry_gate = "unknown"
        entry_gate_reasons = ["成交时不属于本月目标四档主战区"]
    snapshot.update({
        "strike": strike,
        "direction": direction,
        "yes_price": round(yes_price, 6),
        "no_price": round(no_price, 6),
        "distance_pct": round(distance_pct, 6),
        "distance_bucket": _distance_bucket(distance_pct),
        "days_left": round(days_left, 6),
        "phase_key": phase_key,
        "phase_label": phase_label,
        "tier_key": tier_key,
        "tier_label": ACTIONABLE_TIER_LABELS.get(tier_key),
        "entry_gate": entry_gate,
        "entry_gate_reasons": entry_gate_reasons,
        "reason": None if tier_key else "not_actionable_tier",
    })
    return snapshot


def classify_activity_buy_tier(item: dict, *, fill_dt: datetime, price: float, btc_price: float | None) -> dict[str, Any]:
    """从 Polymarket activity buy 记录生成 entry_tier snapshot。"""
    end_utc = (
        _parse_datetime(item.get("endDate"))
        or _parse_datetime(item.get("endDateIso"))
        or _parse_datetime(item.get("marketEndDate"))
    )
    question = (
        item.get("title")
        or item.get("question")
        or item.get("marketTitle")
        or item.get("eventTitle")
        or ""
    )
    return classify_entry_tier(
        question=str(question),
        outcome=str(item.get("outcome") or ""),
        fill_price=float(price),
        btc_price=btc_price,
        as_of_utc=fill_dt.astimezone(timezone.utc),
        end_utc=end_utc,
    )


def json_param(value: dict | None):
    return psycopg2.extras.Json(value) if value is not None else None


def _compact_market_sentiment_context(input_snapshot: dict[str, Any]) -> dict[str, Any]:
    sentiment = input_snapshot.get("market_sentiment_and_funding") or {}
    sentiment_data = sentiment.get("sentiment_data") or {}
    liquidity_data = sentiment.get("liquidity_data") or {}
    market_context = sentiment.get("market_context") or {}
    volatility = input_snapshot.get("daily_volatility_profile") or {}
    future = input_snapshot.get("future_possibility_context") or {}
    fear_greed = sentiment_data.get("fear_greed") or {}
    return {
        "btc_price": market_context.get("btc_price"),
        "btc_price_change": market_context.get("price_change"),
        "fear_greed_value": fear_greed.get("value"),
        "fear_greed_status": fear_greed.get("status"),
        "funding_rate_pct": liquidity_data.get("funding_rate_pct"),
        "open_interest": liquidity_data.get("open_interest"),
        "oi_change_trend": liquidity_data.get("oi_change_trend"),
        "long_short_ratio": sentiment_data.get("long_short_ratio"),
        "rsi_interpretation": sentiment_data.get("rsi_interpretation"),
        "market_regime": volatility.get("market_regime"),
        "atr_pct": volatility.get("atr_pct"),
        "tr_percentile_30d": volatility.get("tr_percentile_30d"),
        "scenario_bias": future.get("scenario_bias"),
        "drawdown_from_month_high_pct": future.get("drawdown_from_month_high_pct"),
        "space_to_reclaim_target_pct": future.get("space_to_reclaim_target_pct"),
    }


def build_decision_context_snapshot(cur, *, profile: str, fill_dt: datetime) -> dict[str, Any] | None:
    """为成交复盘锁定最近一次 AI 分析的轻量上下文；不复制完整 prompt/输出。"""
    try:
        cur.execute(
            """
            SELECT id, created_at, btc_price, days_left_in_month, input_snapshot
            FROM recommendation_runs
            WHERE profile = %s
              AND status IN ('completed', 'partial')
              AND created_at <= %s
              AND created_at >= %s - INTERVAL '72 hours'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (str(profile or "analyze"), fill_dt, fill_dt),
        )
        row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("decision context lookup failed: %s", exc)
        return None
    if not row:
        return None
    input_snapshot = row.get("input_snapshot") or {}
    if not isinstance(input_snapshot, dict):
        input_snapshot = {}
    created_at = row.get("created_at")
    if isinstance(created_at, datetime):
        created_at_utc = created_at.astimezone(timezone.utc)
        age_hours = max((fill_dt.astimezone(timezone.utc) - created_at_utc).total_seconds() / 3600.0, 0.0)
        created_at_value = created_at_utc.isoformat()
    else:
        age_hours = None
        created_at_value = None
    profit_context = input_snapshot.get("profit_optimization_context") or {}
    monthly_progress = profit_context.get("monthly_progress") or {}
    market_sentiment = _compact_market_sentiment_context(input_snapshot)
    return {
        "source": "latest_recommendation_run_before_fill",
        "recommendation_run_id": int(row["id"]),
        "recommendation_created_at": created_at_value,
        "recommendation_age_hours": round(age_hours, 4) if age_hours is not None else None,
        "run_btc_price": row.get("btc_price"),
        "run_days_left_in_month": row.get("days_left_in_month"),
        "monthly_progress": {
            "month": monthly_progress.get("month"),
            "monthly_pnl_pct": monthly_progress.get("monthly_pnl_pct"),
            "monthly_pnl_usdc": monthly_progress.get("monthly_pnl_usdc"),
            "current_net_value": monthly_progress.get("current_net_value"),
            "baseline_net_value": monthly_progress.get("baseline_net_value"),
        },
        "market_sentiment": market_sentiment,
    }


def get_monthly_goal_realized_summary(
    *,
    profile: str = "analyze",
    now: datetime | None = None,
) -> dict[str, Any]:
    """按 ET 本月 sell 成交估算已实现盈亏，并按买入 lot 的 entry_tier 归因。

    查询保留本月之前的历史成交，以便当前月卖出能正确消耗跨月遗留 buy lot。
    """
    ensure_fill_attribution_columns()
    month_start_et, month_end_et, month_label = _current_et_month_bounds(now)
    month_start_utc = month_start_et.astimezone(timezone.utc)
    month_end_utc = month_end_et.astimezone(timezone.utc)
    monthly_event_slug = _monthly_btc_event_slug(month_start_et)
    fill_profile = str(profile or "analyze").strip() or "analyze"
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT fill_timestamp, token_id, side, price, size_shares, size_usdc,
                   market_slug, event_slug, raw_json,
                   entry_tier_key, entry_tier_label, tier_snapshot
            FROM advisory_chain_fills
            WHERE profile = %s
              AND fill_timestamp < %s
              AND event_slug = %s
            ORDER BY fill_timestamp ASC, id ASC
            """,
            (fill_profile, month_end_utc, monthly_event_slug),
        )
        rows = list(cur.fetchall())

    state: dict[str, list[dict]] = {}
    by_token: dict[str, dict] = {}
    by_tier: dict[str, dict] = {
        key: {
            "tier_key": key,
            "tier_label": label,
            "realized_pnl": 0.0,
            "gross_realized_loss": 0.0,
            "loss_trade_count": 0,
            "sell_shares": 0.0,
        }
        for key, label in ACTIONABLE_TIER_LABELS.items()
    }
    total_realized = 0.0
    total_realized_loss = 0.0
    classified_realized = 0.0
    classified_realized_loss = 0.0
    unclassified_realized = 0.0
    unclassified_realized_loss = 0.0
    untracked_sell_usdc = 0.0
    untracked_sell_shares = 0.0
    data_start = None
    data_end = None

    def _token_item(row: dict, token_id: str) -> dict:
        return by_token.setdefault(token_id, {
            "token_id": token_id,
            "realized_pnl": 0.0,
            "gross_realized_loss": 0.0,
            "loss_trade_count": 0,
            "classified_realized_pnl": 0.0,
            "classified_realized_loss": 0.0,
            "unclassified_realized_pnl": 0.0,
            "unclassified_realized_loss": 0.0,
            "sell_shares": 0.0,
            "sell_usdc": 0.0,
            "matched_sell_shares": 0.0,
            "matched_sell_usdc": 0.0,
            "untracked_sell_shares": 0.0,
            "untracked_sell_usdc": 0.0,
            "market_slug": row.get("market_slug"),
            "event_slug": row.get("event_slug"),
            "outcome": (row.get("raw_json") or {}).get("outcome"),
            "by_tier": {},
        })

    def _add_realized(row: dict, token_id: str, tier_key: str | None, tier_label: str | None,
                      shares: float, proceeds: float, realized: float) -> None:
        nonlocal total_realized, total_realized_loss
        nonlocal classified_realized, classified_realized_loss
        nonlocal unclassified_realized, unclassified_realized_loss
        item = _token_item(row, token_id)
        realized_loss = max(0.0, -realized)
        item["sell_shares"] += shares
        item["sell_usdc"] += proceeds
        item["matched_sell_shares"] += shares
        item["matched_sell_usdc"] += proceeds
        item["realized_pnl"] += realized
        item["gross_realized_loss"] += realized_loss
        if realized_loss > 0:
            item["loss_trade_count"] += 1
        total_realized += realized
        total_realized_loss += realized_loss
        if tier_key in by_tier:
            bucket = by_tier[tier_key]
            bucket["realized_pnl"] += realized
            bucket["gross_realized_loss"] += realized_loss
            if realized_loss > 0:
                bucket["loss_trade_count"] += 1
            bucket["sell_shares"] += shares
            token_bucket = item["by_tier"].setdefault(tier_key, {
                "tier_key": tier_key,
                "tier_label": tier_label or ACTIONABLE_TIER_LABELS.get(tier_key),
                "realized_pnl": 0.0,
                "gross_realized_loss": 0.0,
                "loss_trade_count": 0,
                "sell_shares": 0.0,
            })
            token_bucket["realized_pnl"] += realized
            token_bucket["gross_realized_loss"] += realized_loss
            if realized_loss > 0:
                token_bucket["loss_trade_count"] += 1
            token_bucket["sell_shares"] += shares
            item["classified_realized_pnl"] += realized
            item["classified_realized_loss"] += realized_loss
            classified_realized += realized
            classified_realized_loss += realized_loss
        else:
            item["unclassified_realized_pnl"] += realized
            item["unclassified_realized_loss"] += realized_loss
            unclassified_realized += realized
            unclassified_realized_loss += realized_loss

    for row in rows:
        ts = row["fill_timestamp"]
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if data_start is None or ts < data_start:
            data_start = ts
        if data_end is None or ts > data_end:
            data_end = ts
        token_id = str(row["token_id"] or "")
        if not token_id:
            continue
        side = str(row["side"] or "").lower()
        shares = float(row["size_shares"] or 0.0)
        usdc = float(row["size_usdc"] or 0.0)
        if shares <= 0 or usdc <= 0 or side not in {"buy", "sell"}:
            continue

        lots = state.setdefault(token_id, [])
        in_month = month_start_utc <= ts < month_end_utc
        if side == "buy":
            lots.append({
                "shares": shares,
                "cost": usdc,
                "entry_tier_key": row.get("entry_tier_key"),
                "entry_tier_label": row.get("entry_tier_label"),
                "tier_snapshot": row.get("tier_snapshot"),
            })
            continue

        if side == "sell":
            remaining = shares
            while remaining > 1e-9 and lots:
                lot = lots[0]
                lot_shares = max(0.0, float(lot.get("shares") or 0.0))
                lot_cost = max(0.0, float(lot.get("cost") or 0.0))
                if lot_shares <= 1e-9:
                    lots.pop(0)
                    continue
                matched_shares = min(remaining, lot_shares)
                proceeds = usdc * (matched_shares / shares)
                cost_basis = lot_cost * (matched_shares / lot_shares)
                realized = proceeds - cost_basis
                if in_month:
                    _add_realized(
                        row,
                        token_id,
                        str(lot.get("entry_tier_key") or "") or None,
                        lot.get("entry_tier_label"),
                        matched_shares,
                        proceeds,
                        realized,
                    )
                lot["shares"] = max(0.0, lot_shares - matched_shares)
                lot["cost"] = max(0.0, lot_cost - cost_basis)
                remaining -= matched_shares
                if lot["shares"] <= 1e-9:
                    lots.pop(0)
            if in_month and remaining > 1e-9:
                unknown_ratio = remaining / shares
                unknown_usdc = usdc * unknown_ratio
                item = _token_item(row, token_id)
                item["sell_shares"] += remaining
                item["sell_usdc"] += unknown_usdc
                item["untracked_sell_shares"] += remaining
                item["untracked_sell_usdc"] += unknown_usdc
                untracked_sell_shares += remaining
                untracked_sell_usdc += unknown_usdc

    for item in by_token.values():
        for key in (
            "realized_pnl", "gross_realized_loss",
            "classified_realized_pnl", "classified_realized_loss",
            "unclassified_realized_pnl", "unclassified_realized_loss",
            "sell_shares", "sell_usdc", "matched_sell_shares", "matched_sell_usdc",
            "untracked_sell_shares", "untracked_sell_usdc",
        ):
            item[key] = round(float(item[key] or 0.0), 6)
        for tier_item in item.get("by_tier", {}).values():
            tier_item["realized_pnl"] = round(float(tier_item["realized_pnl"] or 0.0), 6)
            tier_item["gross_realized_loss"] = round(float(tier_item["gross_realized_loss"] or 0.0), 6)
            tier_item["sell_shares"] = round(float(tier_item["sell_shares"] or 0.0), 6)
    for tier_item in by_tier.values():
        tier_item["realized_pnl"] = round(float(tier_item["realized_pnl"] or 0.0), 6)
        tier_item["gross_realized_loss"] = round(float(tier_item["gross_realized_loss"] or 0.0), 6)
        tier_item["sell_shares"] = round(float(tier_item["sell_shares"] or 0.0), 6)

    return {
        "month_label": month_label,
        "event_slug": monthly_event_slug,
        "month_start_et": month_start_et.isoformat(),
        "month_end_et": month_end_et.isoformat(),
        "source": "advisory_chain_fills",
        "profile": fill_profile,
        "attribution": "entry_tier_fifo",
        "data_start": data_start.isoformat() if data_start else None,
        "data_end": data_end.isoformat() if data_end else None,
        "total_realized": round(total_realized, 6),
        "total_realized_loss": round(total_realized_loss, 6),
        "classified_realized": round(classified_realized, 6),
        "classified_realized_loss": round(classified_realized_loss, 6),
        "unclassified_realized": round(unclassified_realized, 6),
        "unclassified_realized_loss": round(unclassified_realized_loss, 6),
        "untracked_sell_usdc": round(untracked_sell_usdc, 6),
        "untracked_sell_shares": round(untracked_sell_shares, 6),
        "by_tier": by_tier,
        "by_token": by_token,
    }


def _new_review_group(key: str, label: str) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "match_count": 0,
        "win_count": 0,
        "loss_count": 0,
        "matched_buy_cost": 0.0,
        "sell_proceeds": 0.0,
        "realized_pnl": 0.0,
        "gross_profit": 0.0,
        "gross_loss": 0.0,
        "worst_match_pnl": None,
        "matched_shares": 0.0,
        "token_count": 0,
        "_tokens": set(),
    }


def _add_review_match(group: dict[str, Any], *, token_id: str, shares: float, cost: float,
                      proceeds: float, realized: float) -> None:
    group["match_count"] += 1
    group["matched_shares"] += shares
    group["matched_buy_cost"] += cost
    group["sell_proceeds"] += proceeds
    group["realized_pnl"] += realized
    if realized >= 0:
        group["win_count"] += 1
        group["gross_profit"] += realized
    else:
        group["loss_count"] += 1
        group["gross_loss"] += -realized
    worst = group.get("worst_match_pnl")
    group["worst_match_pnl"] = realized if worst is None else min(float(worst), realized)
    group["_tokens"].add(token_id)


def _finalize_review_group(group: dict[str, Any]) -> dict[str, Any]:
    out = dict(group)
    tokens = out.pop("_tokens", set())
    cost = float(out.get("matched_buy_cost") or 0.0)
    count = int(out.get("match_count") or 0)
    wins = int(out.get("win_count") or 0)
    token_count = len(tokens)
    out["token_count"] = token_count
    if count < 3 or token_count < 2:
        sample_quality = "insufficient"
    elif count < 5 or token_count < 3:
        sample_quality = "limited"
    else:
        sample_quality = "reliable"
    out["sample_quality"] = sample_quality
    out["sample_quality_label"] = MONTHLY_REVIEW_SAMPLE_QUALITY_LABELS.get(sample_quality, sample_quality)
    out["return_pct"] = round(float(out.get("realized_pnl") or 0.0) / cost * 100.0, 2) if cost > 0 else None
    out["win_rate_pct"] = round(wins / count * 100.0, 2) if count > 0 else None
    for key in ("matched_buy_cost", "sell_proceeds", "realized_pnl", "gross_profit", "gross_loss", "matched_shares", "worst_match_pnl"):
        if out.get(key) is not None:
            out[key] = round(float(out.get(key) or 0.0), 6)
    return out


def _review_group(groups: dict[str, dict], key: str | None, label: str | None) -> dict[str, Any]:
    group_key = str(key or "unknown")
    if group_key not in groups:
        groups[group_key] = _new_review_group(group_key, str(label or group_key))
    return groups[group_key]


def _snapshot_gate(snapshot: dict[str, Any]) -> str:
    gate = str(snapshot.get("entry_gate") or "").strip().lower()
    if gate in {"allow", "caution", "block"}:
        return gate
    mid_gate = snapshot.get("mid_no_entry_gate") or {}
    gate = str(mid_gate.get("status") or "").strip().lower() if isinstance(mid_gate, dict) else ""
    return gate if gate in {"allow", "caution", "block"} else "unknown"


def _combo_key_label(*, tier_key: str | None, tier_label: str | None, phase_key: str | None,
                     phase_label: str | None, distance_bucket: str, entry_gate: str) -> tuple[str, str]:
    tier_part = str(tier_key or "unclassified")
    phase_part = str(phase_key or "unknown")
    gate_part = str(entry_gate or "unknown")
    key = "|".join([tier_part, phase_part, distance_bucket, gate_part])
    label = " / ".join([
        str(tier_label or ACTIONABLE_TIER_LABELS.get(tier_key or "") or "未归类"),
        str(phase_label or phase_part),
        MONTHLY_REVIEW_DISTANCE_BUCKET_LABELS.get(distance_bucket, distance_bucket),
        MONTHLY_REVIEW_GATE_LABELS.get(gate_part, gate_part),
    ])
    return key, label


def _build_review_conclusions(combos: list[dict[str, Any]], coverage: dict[str, Any]) -> list[dict[str, Any]]:
    conclusions: list[dict[str, Any]] = []
    if coverage.get("buy_snapshot_coverage_pct") is not None and coverage["buy_snapshot_coverage_pct"] < 70:
        conclusions.append({
            "severity": "info",
            "title": "历史买入快照覆盖不足",
            "detail": "早期成交缺少买入时距离/阶段/情绪快照，当前结论更适合用于之后的纪律校准。",
        })
    losing = [c for c in combos if float(c.get("realized_pnl") or 0.0) < -0.01]
    winning = [c for c in combos if float(c.get("realized_pnl") or 0.0) > 0.01]
    for item in sorted(losing, key=lambda x: float(x.get("gross_loss") or 0.0), reverse=True)[:3]:
        sample_quality = str(item.get("sample_quality") or "insufficient")
        if sample_quality == "insufficient":
            severity = "monitor"
        else:
            severity = "block" if item.get("entry_gate") == "block" or float(item.get("return_pct") or 0.0) <= -20.0 else "reduce"
        conclusions.append({
            "severity": severity,
            "title": f"降权/复核：{item.get('label')}",
            "detail": (
                f"已实现 {item.get('realized_pnl'):+.2f} USDC，"
                f"回报 {item.get('return_pct') if item.get('return_pct') is not None else '—'}%，"
                f"亏损 {item.get('gross_loss'):.2f} USDC，"
                f"最差单次 {item.get('worst_match_pnl') if item.get('worst_match_pnl') is not None else '—'} USDC，"
                f"样本质量 {item.get('sample_quality_label')}。后续同类入场需要更强确认或更小仓位。"
            ),
            "group_key": item.get("key"),
            "match_count": item.get("match_count"),
            "token_count": item.get("token_count"),
            "worst_match_pnl": item.get("worst_match_pnl"),
            "sample_quality": sample_quality,
            "sample_quality_label": item.get("sample_quality_label"),
        })
    for item in sorted(winning, key=lambda x: float(x.get("realized_pnl") or 0.0), reverse=True)[:2]:
        sample_quality = str(item.get("sample_quality") or "insufficient")
        severity = "add" if sample_quality != "insufficient" else "info"
        conclusions.append({
            "severity": severity,
            "title": f"可保留：{item.get('label')}",
            "detail": (
                f"已实现 {item.get('realized_pnl'):+.2f} USDC，"
                f"回报 {item.get('return_pct') if item.get('return_pct') is not None else '—'}%，"
                f"胜率 {item.get('win_rate_pct') if item.get('win_rate_pct') is not None else '—'}%，"
                f"样本质量 {item.get('sample_quality_label')}。"
            ),
            "group_key": item.get("key"),
            "match_count": item.get("match_count"),
            "token_count": item.get("token_count"),
            "worst_match_pnl": item.get("worst_match_pnl"),
            "sample_quality": sample_quality,
            "sample_quality_label": item.get("sample_quality_label"),
        })
    if not conclusions:
        conclusions.append({
            "severity": "info",
            "title": "本月已平仓样本不足",
            "detail": "当前复盘主要用于沉淀买入时上下文；等出现更多 sell 成交后会自动生成可加权/降权组合。",
        })
    return conclusions[:6]


def get_monthly_trade_review_summary(
    *,
    profile: str = "analyze",
    now: datetime | None = None,
) -> dict[str, Any]:
    """按买入 lot 的决策快照复盘本月已实现收益来源。"""
    ensure_fill_attribution_columns()
    month_start_et, month_end_et, month_label = _current_et_month_bounds(now)
    month_start_utc = month_start_et.astimezone(timezone.utc)
    month_end_utc = month_end_et.astimezone(timezone.utc)
    monthly_event_slug = _monthly_btc_event_slug(month_start_et)
    fill_profile = str(profile or "analyze").strip() or "analyze"
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, fill_timestamp, token_id, side, price, size_shares, size_usdc,
                   market_slug, event_slug, raw_json,
                   entry_tier_key, entry_tier_label, tier_snapshot
            FROM advisory_chain_fills
            WHERE profile = %s
              AND fill_timestamp < %s
              AND event_slug = %s
            ORDER BY fill_timestamp ASC, id ASC
            """,
            (fill_profile, month_end_utc, monthly_event_slug),
        )
        rows = list(cur.fetchall())

    lots_by_token: dict[str, list[dict[str, Any]]] = {}
    groups = {
        "by_tier": {},
        "by_phase": {},
        "by_distance_bucket": {},
        "by_entry_gate": {},
        "by_combo": {},
    }
    matched_trades: list[dict[str, Any]] = []
    open_lots: list[dict[str, Any]] = []
    buy_lot_count = 0
    buy_lots_with_snapshot = 0
    buy_lots_with_ai_context = 0
    current_month_buy_lot_count = 0
    untracked_sell_usdc = 0.0
    untracked_sell_shares = 0.0
    total_realized = 0.0
    total_cost = 0.0
    total_proceeds = 0.0

    for row in rows:
        ts = row["fill_timestamp"]
        if isinstance(ts, datetime) and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        token_id = str(row.get("token_id") or "")
        side = str(row.get("side") or "").lower()
        shares = float(row.get("size_shares") or 0.0)
        usdc = float(row.get("size_usdc") or 0.0)
        if not token_id or side not in {"buy", "sell"} or shares <= 0 or usdc <= 0:
            continue
        raw = row.get("raw_json") or {}
        in_month = month_start_utc <= ts < month_end_utc
        if side == "buy":
            snapshot = row.get("tier_snapshot") or {}
            buy_lot_count += 1
            if snapshot:
                buy_lots_with_snapshot += 1
            if snapshot.get("decision_context"):
                buy_lots_with_ai_context += 1
            if in_month:
                current_month_buy_lot_count += 1
            lots_by_token.setdefault(token_id, []).append({
                "fill_id": row.get("id"),
                "token_id": token_id,
                "buy_timestamp": ts,
                "shares": shares,
                "cost": usdc,
                "buy_price": float(row.get("price") or 0.0),
                "entry_tier_key": row.get("entry_tier_key"),
                "entry_tier_label": row.get("entry_tier_label"),
                "tier_snapshot": snapshot,
                "question": snapshot.get("question") or raw.get("title") or raw.get("question") or raw.get("marketTitle"),
                "outcome": snapshot.get("outcome") or raw.get("outcome"),
            })
            continue

        lots = lots_by_token.setdefault(token_id, [])
        remaining = shares
        while remaining > 1e-9 and lots:
            lot = lots[0]
            lot_shares = max(0.0, float(lot.get("shares") or 0.0))
            lot_cost = max(0.0, float(lot.get("cost") or 0.0))
            if lot_shares <= 1e-9:
                lots.pop(0)
                continue
            matched_shares = min(remaining, lot_shares)
            proceeds = usdc * (matched_shares / shares)
            cost_basis = lot_cost * (matched_shares / lot_shares)
            realized = proceeds - cost_basis
            if in_month:
                snapshot = lot.get("tier_snapshot") or {}
                tier_key = str(lot.get("entry_tier_key") or snapshot.get("tier_key") or "") or None
                tier_label = lot.get("entry_tier_label") or snapshot.get("tier_label") or ACTIONABLE_TIER_LABELS.get(tier_key or "")
                phase_key = str(snapshot.get("phase_key") or "unknown")
                phase_label = snapshot.get("phase_label") or phase_key
                distance_bucket = str(snapshot.get("distance_bucket") or _distance_bucket(snapshot.get("distance_pct")))
                entry_gate = _snapshot_gate(snapshot)
                combo_key, combo_label = _combo_key_label(
                    tier_key=tier_key,
                    tier_label=tier_label,
                    phase_key=phase_key,
                    phase_label=phase_label,
                    distance_bucket=distance_bucket,
                    entry_gate=entry_gate,
                )
                for bucket, key, label in (
                    ("by_tier", tier_key or "unclassified", tier_label or "未归类"),
                    ("by_phase", phase_key, phase_label),
                    ("by_distance_bucket", distance_bucket, MONTHLY_REVIEW_DISTANCE_BUCKET_LABELS.get(distance_bucket, distance_bucket)),
                    ("by_entry_gate", entry_gate, MONTHLY_REVIEW_GATE_LABELS.get(entry_gate, entry_gate)),
                    ("by_combo", combo_key, combo_label),
                ):
                    _add_review_match(
                        _review_group(groups[bucket], key, label),
                        token_id=token_id,
                        shares=matched_shares,
                        cost=cost_basis,
                        proceeds=proceeds,
                        realized=realized,
                    )
                total_realized += realized
                total_cost += cost_basis
                total_proceeds += proceeds
                trade = {
                    "token_id": token_id,
                    "question": lot.get("question"),
                    "outcome": lot.get("outcome"),
                    "tier_key": tier_key,
                    "tier_label": tier_label,
                    "phase_key": phase_key,
                    "phase_label": phase_label,
                    "distance_bucket": distance_bucket,
                    "distance_bucket_label": MONTHLY_REVIEW_DISTANCE_BUCKET_LABELS.get(distance_bucket, distance_bucket),
                    "distance_pct": snapshot.get("distance_pct"),
                    "entry_gate": entry_gate,
                    "entry_gate_label": MONTHLY_REVIEW_GATE_LABELS.get(entry_gate, entry_gate),
                    "buy_timestamp": lot.get("buy_timestamp").isoformat() if isinstance(lot.get("buy_timestamp"), datetime) else None,
                    "sell_timestamp": ts.isoformat() if isinstance(ts, datetime) else None,
                    "buy_price": round(float(lot.get("buy_price") or 0.0), 6),
                    "sell_price": round(float(row.get("price") or 0.0), 6),
                    "matched_shares": round(matched_shares, 6),
                    "matched_buy_cost": round(cost_basis, 6),
                    "sell_proceeds": round(proceeds, 6),
                    "realized_pnl": round(realized, 6),
                    "return_pct": round(realized / cost_basis * 100.0, 2) if cost_basis > 0 else None,
                    "decision_context": snapshot.get("decision_context"),
                }
                matched_trades.append(trade)
            lot["shares"] = max(0.0, lot_shares - matched_shares)
            lot["cost"] = max(0.0, lot_cost - cost_basis)
            remaining -= matched_shares
            if lot["shares"] <= 1e-9:
                lots.pop(0)
        if in_month and remaining > 1e-9:
            unknown_ratio = remaining / shares
            untracked_sell_usdc += usdc * unknown_ratio
            untracked_sell_shares += remaining

    for lots in lots_by_token.values():
        for lot in lots:
            if lot.get("buy_timestamp") and lot["buy_timestamp"] < month_end_utc and float(lot.get("shares") or 0.0) > 1e-9:
                snapshot = lot.get("tier_snapshot") or {}
                open_lots.append({
                    "token_id": lot.get("token_id"),
                    "question": lot.get("question"),
                    "outcome": lot.get("outcome"),
                    "tier_key": lot.get("entry_tier_key") or snapshot.get("tier_key"),
                    "tier_label": lot.get("entry_tier_label") or snapshot.get("tier_label"),
                    "phase_key": snapshot.get("phase_key"),
                    "phase_label": snapshot.get("phase_label"),
                    "distance_bucket": snapshot.get("distance_bucket") or _distance_bucket(snapshot.get("distance_pct")),
                    "entry_gate": _snapshot_gate(snapshot),
                    "remaining_shares": round(float(lot.get("shares") or 0.0), 6),
                    "remaining_cost": round(float(lot.get("cost") or 0.0), 6),
                    "buy_timestamp": lot.get("buy_timestamp").isoformat() if isinstance(lot.get("buy_timestamp"), datetime) else None,
                })

    finalized_groups = {
        name: sorted(
            (_finalize_review_group(group) for group in bucket.values()),
            key=lambda item: abs(float(item.get("realized_pnl") or 0.0)),
            reverse=True,
        )
        for name, bucket in groups.items()
    }
    combo_groups = finalized_groups["by_combo"]
    for item in combo_groups:
        parts = str(item.get("key") or "").split("|")
        item["tier_key"] = parts[0] if len(parts) > 0 else None
        item["phase_key"] = parts[1] if len(parts) > 1 else None
        item["distance_bucket"] = parts[2] if len(parts) > 2 else None
        item["entry_gate"] = parts[3] if len(parts) > 3 else None
    best_combinations = sorted(
        [item for item in combo_groups if float(item.get("realized_pnl") or 0.0) > 0],
        key=lambda item: float(item.get("realized_pnl") or 0.0),
        reverse=True,
    )[:5]
    worst_combinations = sorted(
        [item for item in combo_groups if float(item.get("realized_pnl") or 0.0) < 0],
        key=lambda item: float(item.get("realized_pnl") or 0.0),
    )[:5]
    coverage = {
        "buy_lot_count": buy_lot_count,
        "current_month_buy_lot_count": current_month_buy_lot_count,
        "buy_lots_with_snapshot": buy_lots_with_snapshot,
        "buy_lots_with_ai_context": buy_lots_with_ai_context,
        "buy_snapshot_coverage_pct": round(buy_lots_with_snapshot / buy_lot_count * 100.0, 2) if buy_lot_count else None,
        "ai_context_coverage_pct": round(buy_lots_with_ai_context / buy_lot_count * 100.0, 2) if buy_lot_count else None,
        "matched_trade_count": len(matched_trades),
        "open_lot_count": len(open_lots),
        "untracked_sell_usdc": round(untracked_sell_usdc, 6),
        "untracked_sell_shares": round(untracked_sell_shares, 6),
    }
    conclusions = _build_review_conclusions(combo_groups, coverage)
    matched_trades.sort(key=lambda item: abs(float(item.get("realized_pnl") or 0.0)), reverse=True)
    return {
        "source": "advisory_chain_fills_decision_snapshot_fifo",
        "profile": fill_profile,
        "month_label": month_label,
        "event_slug": monthly_event_slug,
        "month_start_et": month_start_et.isoformat(),
        "month_end_et": month_end_et.isoformat(),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "用买入时快照解释收益来源：哪类档位/阶段/距离/gate 组合赚钱，哪类组合需要降权或暂停。",
        "summary": {
            "matched_buy_cost": round(total_cost, 6),
            "sell_proceeds": round(total_proceeds, 6),
            "realized_pnl": round(total_realized, 6),
            "return_pct": round(total_realized / total_cost * 100.0, 2) if total_cost > 0 else None,
        },
        "coverage": coverage,
        "groups": finalized_groups,
        "best_combinations": best_combinations,
        "worst_combinations": worst_combinations,
        "conclusions": conclusions,
        "sample_trades": matched_trades[:20],
        "open_lots": sorted(open_lots, key=lambda item: float(item.get("remaining_cost") or 0.0), reverse=True)[:200],
    }


def compact_monthly_trade_review_for_ai(review_summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(review_summary, dict):
        return None
    return {
        "source": review_summary.get("source"),
        "purpose": review_summary.get("purpose"),
        "month_label": review_summary.get("month_label"),
        "summary": review_summary.get("summary"),
        "coverage": review_summary.get("coverage"),
        "conclusions": review_summary.get("conclusions"),
        "best_combinations": review_summary.get("best_combinations", [])[:3],
        "worst_combinations": review_summary.get("worst_combinations", [])[:3],
    }


def build_monthly_goal_context(
    *,
    polymarket_event_situation: dict,
    positions: list | None,
    base_value: float,
    current_btc_price: float | None,
    days_left_in_month: float,
    target_pct: float = MONTHLY_GOAL_DEFAULT_TARGET_PCT,
    target_pct_source: str = "backend_default",
    realized_overrides: dict[str, Any] | None = None,
    target_position_overrides: dict[str, Any] | None = None,
    realized_summary: dict | None = None,
    btc_momentum_context: dict[str, Any] | None = None,
    trade_review_summary: dict[str, Any] | None = None,
    active_manual_pending_orders: dict[str, Any] | None = None,
    profile: str = "analyze",
) -> dict[str, Any]:
    """构建供 AI 使用的本月目标分层上下文。"""
    realized_summary = realized_summary or get_monthly_goal_realized_summary(profile=profile)
    if trade_review_summary is None:
        try:
            trade_review_summary = get_monthly_trade_review_summary(profile=profile)
        except Exception as exc:  # noqa: BLE001
            logger.warning("monthly trade review summary unavailable: %s", exc)
            trade_review_summary = None
    trade_review_context = compact_monthly_trade_review_for_ai(trade_review_summary)
    review_bias = _review_bias_by_tier(trade_review_context)
    base = max(0.0, _to_float(base_value, 0.0) or 0.0)
    pct = max(0.0, _to_float(target_pct, MONTHLY_GOAL_DEFAULT_TARGET_PCT) or 0.0)
    total_target_profit = base * pct / 100.0 if base > 0 and pct > 0 else None
    phase_key, phase_label = _classify_time_phase(days_left_in_month)
    thresholds = _current_phase_thresholds(phase_key)
    pending_buys_by_token = _active_pending_buys_by_token(active_manual_pending_orders)
    open_lot_intents = _open_lot_intents_by_token(trade_review_summary)
    held_by_token_tier = _held_allocations_by_token_tier(
        positions=positions,
        open_lot_intents=open_lot_intents,
    )
    pending_buys_by_token_tier = _pending_buy_intents_by_token_tier(active_manual_pending_orders)
    pending_tokens_seen: set[str] = set()
    pending_token_tiers_seen: set[tuple[str, str]] = set()
    normalized_realized_overrides = _normalize_realized_overrides(realized_overrides or {})

    candidates_by_tier: dict[str, list[dict]] = {tier["key"]: [] for tier in MONTHLY_GOAL_TIERS}
    markets = polymarket_event_situation.get("markets", []) if isinstance(polymarket_event_situation, dict) else []
    market_entries_by_token = _market_entries_by_token(
        markets=markets,
        current_btc_price=current_btc_price,
        days_left_in_month=days_left_in_month,
    )
    exposure_classification = _build_exposure_classification(
        positions=positions,
        markets=markets,
        current_btc_price=current_btc_price,
        days_left_in_month=days_left_in_month,
        active_manual_pending_orders=active_manual_pending_orders,
        trade_review_summary=trade_review_summary,
    )
    candidate_token_tiers_seen: set[tuple[str, str]] = set()
    for market in markets:
        if not isinstance(market, dict) or _market_is_settled(market):
            continue
        question = str(market.get("question") or "")
        strike, direction = _extract_strike_and_direction(question)
        yes_price, no_price = _parse_market_prices(market)
        if current_btc_price is None or current_btc_price <= 0 or strike is None or direction == "unknown":
            continue
        if yes_price is None or no_price is None:
            continue
        distance_pct = abs(strike - float(current_btc_price)) / float(current_btc_price) * 100.0
        outcomes = _ensure_list(market.get("outcomes"))
        token_ids = _ensure_list(market.get("token_id") or market.get("clobTokenIds"))
        for tier in MONTHLY_GOAL_TIERS:
            tier_key = _tier_key_for(
                outcome=tier["outcome"],
                yes_price=yes_price,
                no_price=no_price,
                distance_pct=distance_pct,
                days_left=days_left_in_month,
            )
            if tier_key != tier["key"]:
                continue
            tier_key = str(tier["key"])
            outcome_index = next(
                (idx for idx, name in enumerate(outcomes)
                 if str(name or "").strip().lower() == str(tier["outcome"]).lower()),
                -1,
            )
            if outcome_index < 0:
                continue
            token_id = str(token_ids[outcome_index]) if outcome_index < len(token_ids) else ""
            held = (held_by_token_tier.get(token_id) or {}).get(tier_key) if token_id else None
            if held is None:
                held = {"held_shares": 0.0, "held_value_usdc": 0.0}
            pending_row = (pending_buys_by_token_tier.get(token_id) or {}).get(tier_key) if token_id else None
            pending_buy_notional = float((pending_row or {}).get("pending_buy_notional_usdc") or 0.0)
            if pending_buy_notional > 0 and token_id:
                pending_tokens_seen.add(token_id)
                pending_token_tiers_seen.add((token_id, tier_key))
            price = _entry_price_for_outcome(market, outcome_index)
            candidate = {
                "question": question,
                "outcome": tier["outcome"],
                "token_id": token_id or None,
                "price": round(price, 6) if price is not None else None,
                "strike": round(float(strike), 2),
                "direction_in_question": direction,
                "distance_pct": round(distance_pct, 2),
                "held_shares": round(float(held.get("held_shares") or 0.0), 6),
                "held_value_usdc": round(float(held.get("held_value_usdc") or 0.0), 2),
                "held_intent_tier_key": tier_key if float(held.get("held_value_usdc") or 0.0) > 0 else None,
                "current_tier_key": tier_key,
                "band_status": "in_band" if float(held.get("held_value_usdc") or 0.0) > 0 else None,
                "allocation_candidate": True,
                "pending_buy_notional_usdc": round(pending_buy_notional, 2),
                "pending_order_ids": list((pending_row or {}).get("pending_order_ids") or [])[:8],
                "pending_plan_ids": list((pending_row or {}).get("pending_plan_ids") or [])[:8],
            }
            if token_id:
                candidate_token_tiers_seen.add((token_id, tier_key))
            if tier["key"] == "mid_no":
                candidate["mid_no_entry_gate"] = _mid_no_entry_gate(
                    direction_in_question=direction,
                    distance_pct=distance_pct,
                    phase_key=phase_key,
                    btc_momentum_context=btc_momentum_context,
                )
            candidates_by_tier[tier["key"]].append(candidate)
    for token_id in sorted(set(held_by_token_tier) | set(pending_buys_by_token_tier)):
        entry = market_entries_by_token.get(token_id) or {}
        tier_keys = set((held_by_token_tier.get(token_id) or {}).keys()) | set((pending_buys_by_token_tier.get(token_id) or {}).keys())
        for tier_key in tier_keys:
            if tier_key not in candidates_by_tier or (token_id, tier_key) in candidate_token_tiers_seen:
                continue
            held = (held_by_token_tier.get(token_id) or {}).get(tier_key) or {}
            pending_row = (pending_buys_by_token_tier.get(token_id) or {}).get(tier_key) or {}
            pending_buy_notional = float(pending_row.get("pending_buy_notional_usdc") or 0.0)
            held_value = float(held.get("held_value_usdc") or 0.0)
            held_shares = float(held.get("held_shares") or 0.0)
            if held_value <= 0 and pending_buy_notional <= 0:
                continue
            if pending_buy_notional > 0:
                pending_tokens_seen.add(token_id)
                pending_token_tiers_seen.add((token_id, tier_key))
            current_tier_key = entry.get("current_tier_key")
            if current_tier_key == tier_key:
                band_status = "in_band"
            elif current_tier_key:
                band_status = "shifted_band"
            elif entry:
                band_status = "out_of_band"
            else:
                band_status = "unknown"
            candidate = {
                "question": entry.get("question") or None,
                "outcome": entry.get("outcome") or None,
                "token_id": token_id,
                "price": entry.get("price"),
                "strike": entry.get("strike"),
                "direction_in_question": entry.get("direction_in_question"),
                "distance_pct": entry.get("distance_pct"),
                "held_shares": round(held_shares, 6),
                "held_value_usdc": round(held_value, 2),
                "held_intent_tier_key": tier_key if held_value > 0 else None,
                "current_tier_key": current_tier_key,
                "current_tier_label": ACTIONABLE_TIER_LABELS.get(current_tier_key),
                "band_status": band_status,
                "allocation_candidate": False,
                "intent_only_note": "按入场/挂单意图占用该档；当前不作为新增候选分配余量",
                "pending_buy_notional_usdc": round(pending_buy_notional, 2),
                "pending_order_ids": list(pending_row.get("pending_order_ids") or [])[:8],
                "pending_plan_ids": list(pending_row.get("pending_plan_ids") or [])[:8],
            }
            candidates_by_tier[tier_key].append(candidate)
    for values in candidates_by_tier.values():
        values.sort(key=lambda item: (0 if item.get("allocation_candidate") is False else 1, item.get("strike") or 0.0), reverse=True)

    realized_by_tier = realized_summary.get("by_tier") or {}
    tier_contexts: list[dict[str, Any]] = []
    total_planned_position = 0.0
    total_tier_remaining = 0.0
    total_target_shares = 0.0
    total_pending_buy_notional = sum(
        float(item.get("pending_buy_notional_usdc") or 0.0)
        for item in pending_buys_by_token.values()
    )
    unattributed_pending_tokens = []
    for token_id, row in pending_buys_by_token.items():
        tier_rows = pending_buys_by_token_tier.get(token_id) or {}
        if not tier_rows:
            unattributed_pending_tokens.append(token_id)
            continue
        if any((token_id, tier_key) not in pending_token_tiers_seen for tier_key in tier_rows):
            unattributed_pending_tokens.append(token_id)
    unattributed_pending_buy_notional = sum(
        float(pending_buys_by_token[token_id].get("pending_buy_notional_usdc") or 0.0)
        for token_id in unattributed_pending_tokens
    )
    allocation_plan = calculate_monthly_goal_tier_allocations(pct, target_position_overrides, phase_key=phase_key)
    plan_expected_return_pct = float(allocation_plan["planned_return_pct"])
    effective_plan_expected_return_pct = float(allocation_plan.get("effective_planned_return_pct") or plan_expected_return_pct)
    total_target_profit = (
        base * effective_plan_expected_return_pct / 100.0
        if base > 0 and effective_plan_expected_return_pct > 0 else None
    )
    allocations_by_tier = {
        item["tier_key"]: item
        for item in allocation_plan["allocations"]
    }
    total_realized_loss = float(realized_summary.get("total_realized_loss") or 0.0)
    classified_realized_loss = float(realized_summary.get("classified_realized_loss") or 0.0)
    unclassified_realized_loss = float(realized_summary.get("unclassified_realized_loss") or 0.0)
    overall_loss_budget_usdc = base * MONTHLY_GOAL_OVERALL_LOSS_BUDGET_PCT / 100.0 if base > 0 else 0.0
    overall_loss_status, overall_loss_usage_pct = _loss_budget_status(total_realized_loss, overall_loss_budget_usdc)
    for tier in MONTHLY_GOAL_TIERS:
        tier_key = tier["key"]
        allocation = allocations_by_tier.get(tier_key) or {}
        target_position_pct = float(allocation.get("target_position_pct") or 0.0)
        target_contribution_pct = float(allocation.get("target_contribution_pct") or 0.0)
        target_profit_share_pct = float(allocation.get("target_profit_share_pct") or 0.0)
        phase_suggested_position_pct = float(allocation.get("phase_suggested_position_pct") or target_position_pct)
        effective_position_cap_pct = float(allocation.get("effective_position_cap_pct") or 0.0)
        target_position = (
            base * target_position_pct / 100.0
            if base > 0 and target_position_pct >= 0 else None
        )
        effective_position_cap = (
            base * effective_position_cap_pct / 100.0
            if base > 0 and effective_position_cap_pct >= 0 else None
        )
        tier_target_profit = (
            base * float(allocation.get("effective_target_contribution_pct") or target_contribution_pct) / 100.0
            if base > 0 and target_contribution_pct >= 0 else None
        )
        candidates = candidates_by_tier[tier_key]
        realized_row = realized_by_tier.get(tier_key) if isinstance(realized_by_tier, dict) else None
        auto_realized = float((realized_row or {}).get("realized_pnl") or 0.0)
        auto_realized_loss = float((realized_row or {}).get("gross_realized_loss") or max(0.0, -auto_realized))
        loss_budget_pct = float(MONTHLY_GOAL_TIER_LOSS_BUDGET_PCT_BY_TIER.get(tier_key, 0.0))
        loss_budget_usdc = base * loss_budget_pct / 100.0 if base > 0 else 0.0
        loss_budget_remaining_usdc = max(0.0, loss_budget_usdc - auto_realized_loss)
        loss_budget_status, loss_budget_usage_pct = _loss_budget_status(auto_realized_loss, loss_budget_usdc)
        if overall_loss_status == "stop_new_entries" or loss_budget_status == "stop_new_entries":
            risk_entry_gate = "block"
        elif overall_loss_status == "caution" or loss_budget_status == "caution":
            risk_entry_gate = "caution"
        else:
            risk_entry_gate = "allow"
        has_realized_override = tier_key in normalized_realized_overrides
        realized = normalized_realized_overrides[tier_key] if has_realized_override else auto_realized
        remaining = max(0.0, (tier_target_profit or 0.0) - realized) if tier_target_profit is not None else None
        held_value = sum(float(c.get("held_value_usdc") or 0.0) for c in candidates)
        held_shares = sum(float(c.get("held_shares") or 0.0) for c in candidates)
        pending_buy_notional = sum(float(c.get("pending_buy_notional_usdc") or 0.0) for c in candidates)
        committed_value = held_value + pending_buy_notional

        candidate_gate_statuses: list[str] = []
        eligible_candidate_count = 0
        enriched_candidates = []
        for candidate in candidates:
            market_gate = "allow"
            gate_reasons: list[str] = []
            if candidate.get("allocation_candidate") is False:
                market_gate = "allow"
                gate_reasons = ["该项按原入场/挂单意图占用本档，不作为新增候选分配余量"]
            elif tier_key == "mid_no":
                mid_gate = candidate.get("mid_no_entry_gate") or {}
                market_gate = str(mid_gate.get("status") or "allow")
                gate_reasons = list(mid_gate.get("reasons") or [])
            candidate_gate = _combine_entry_status(risk_entry_gate, market_gate)
            if candidate.get("allocation_candidate") is not False:
                candidate_gate_statuses.append(candidate_gate)
            if candidate_gate != "block" and candidate.get("allocation_candidate") is not False:
                eligible_candidate_count += 1
            enriched = dict(candidate)
            enriched["entry_gate"] = candidate_gate
            enriched["entry_gate_reasons"] = gate_reasons
            enriched_candidates.append(enriched)

        market_entry_gate = "allow"
        if tier_key == "mid_no" and candidate_gate_statuses:
            if any(status == "allow" for status in candidate_gate_statuses):
                market_entry_gate = "allow"
            elif any(status == "caution" for status in candidate_gate_statuses):
                market_entry_gate = "caution"
            else:
                market_entry_gate = "block"
        entry_gate = _combine_entry_status(risk_entry_gate, market_entry_gate)
        if entry_gate == "block":
            risk_adjusted_position_cap = 0.0
        elif entry_gate == "caution":
            risk_adjusted_position_cap = (effective_position_cap or 0.0) * 0.5
        else:
            risk_adjusted_position_cap = effective_position_cap
        risk_adjusted_headroom = (
            max(0.0, (risk_adjusted_position_cap or 0.0) - committed_value)
            if risk_adjusted_position_cap is not None else None
        )
        for enriched in enriched_candidates:
            enriched.update(_candidate_quality(
                candidate=enriched,
                tier_key=tier_key,
                phase_key=phase_key,
                entry_gate=str(enriched.get("entry_gate") or entry_gate),
                review_bias=review_bias,
                tier_headroom=risk_adjusted_headroom,
            ))
        eligible_candidates_for_allocation = [
            c for c in enriched_candidates
            if c.get("entry_gate") != "block" and c.get("allocation_candidate") is not False
        ]
        per_candidate_headroom = (
            (risk_adjusted_headroom or 0.0) / len(eligible_candidates_for_allocation)
            if risk_adjusted_headroom is not None and eligible_candidates_for_allocation else None
        )
        allocatable_candidate_headroom = 0.0 if per_candidate_headroom is not None else None
        target_shares = 0.0 if per_candidate_headroom is not None else None
        for enriched in enriched_candidates:
            price = enriched.get("price")
            candidate_target_shares = None
            if enriched.get("allocation_candidate") is False:
                candidate_target = None
            elif enriched.get("entry_gate") == "block":
                candidate_target = 0.0
            elif per_candidate_headroom is not None:
                candidate_target = max(0.0, per_candidate_headroom)
                if allocatable_candidate_headroom is not None:
                    allocatable_candidate_headroom += candidate_target
            else:
                candidate_target = None
            if candidate_target is not None and price:
                candidate_target_shares = candidate_target / float(price)
                if target_shares is not None:
                    target_shares += candidate_target_shares
            enriched["target_position_usdc"] = round(candidate_target, 2) if candidate_target is not None else None
            enriched["target_shares"] = round(candidate_target_shares, 6) if candidate_target_shares is not None else None
        if target_position is not None:
            total_planned_position += target_position
        if remaining is not None:
            total_tier_remaining += remaining
        if target_shares is not None:
            total_target_shares += target_shares
        tier_contexts.append({
            "tier_key": tier_key,
            "tier_label": tier["label"],
            "outcome": tier["outcome"],
            "expected_return_pct": tier["return_pct"],
            "target_position_pct": round(target_position_pct, 4),
            "phase_suggested_position_pct": round(phase_suggested_position_pct, 4),
            "effective_position_cap_pct": round(effective_position_cap_pct, 4),
            "risk_adjusted_position_cap_pct": round(
                (risk_adjusted_position_cap or 0.0) / base * 100.0, 4
            ) if base > 0 and risk_adjusted_position_cap is not None else None,
            "exceeds_phase_suggestion": bool(allocation.get("exceeds_phase_suggestion")),
            "target_contribution_pct": round(target_contribution_pct, 4),
            "effective_target_contribution_pct": round(float(allocation.get("effective_target_contribution_pct") or target_contribution_pct), 4),
            "target_profit_share_pct": round(target_profit_share_pct, 4),
            "target_profit_usdc": round(tier_target_profit, 2) if tier_target_profit is not None else None,
            "target_position_usdc": round(target_position, 2) if target_position is not None else None,
            "effective_position_cap_usdc": round(effective_position_cap, 2) if effective_position_cap is not None else None,
            "risk_adjusted_position_cap_usdc": round(risk_adjusted_position_cap, 2) if risk_adjusted_position_cap is not None else None,
            "current_held_value_usdc": round(held_value, 2),
            "current_held_shares": round(held_shares, 6),
            "current_pending_buy_notional_usdc": round(pending_buy_notional, 2),
            "current_committed_value_usdc": round(committed_value, 2),
            "target_position_gap_usdc": round(max(0.0, (target_position or 0.0) - committed_value), 2) if target_position is not None else None,
            "headroom_to_position_limit_usdc": round(max(0.0, (target_position or 0.0) - committed_value), 2) if target_position is not None else None,
            "headroom_to_effective_cap_usdc": round(max(0.0, (effective_position_cap or 0.0) - committed_value), 2) if effective_position_cap is not None else None,
            "headroom_to_risk_adjusted_cap_usdc": round(risk_adjusted_headroom, 2) if risk_adjusted_headroom is not None else None,
            "allocatable_candidate_headroom_usdc": round(allocatable_candidate_headroom, 2) if allocatable_candidate_headroom is not None else None,
            "target_shares": round(target_shares, 6) if target_shares is not None else None,
            "realized_pnl_usdc": round(realized, 2),
            "auto_realized_pnl_usdc": round(auto_realized, 2),
            "realized_override_applied": has_realized_override,
            "loss_budget_pct": round(loss_budget_pct, 4),
            "loss_budget_usdc": round(loss_budget_usdc, 2),
            "gross_realized_loss_usdc": round(auto_realized_loss, 2),
            "loss_budget_remaining_usdc": round(loss_budget_remaining_usdc, 2),
            "loss_budget_usage_pct": round(loss_budget_usage_pct, 2) if loss_budget_usage_pct is not None else None,
            "loss_budget_status": loss_budget_status,
            "risk_entry_gate": risk_entry_gate,
            "market_entry_gate": market_entry_gate,
            "entry_gate": entry_gate,
            "remaining_profit_usdc": round(remaining, 2) if remaining is not None else None,
            "realized_minus_target_usdc": round(realized - (tier_target_profit or 0.0), 2) if tier_target_profit is not None else None,
            "candidate_count": len(candidates),
            "candidates": enriched_candidates,
            "current_phase_threshold": thresholds.get(tier_key),
        })

    total_realized = float(realized_summary.get("total_realized") or 0.0)
    classified_realized = float(realized_summary.get("classified_realized") or 0.0)
    unclassified_realized = float(realized_summary.get("unclassified_realized") or 0.0)
    effective_total_realized = sum(float(t.get("realized_pnl_usdc") or 0.0) for t in tier_contexts) + unclassified_realized
    total_remaining = (
        max(0.0, (total_target_profit or 0.0) - effective_total_realized)
        if total_target_profit is not None else None
    )
    return {
        "source": "backend_monthly_goal_context",
        "target_pct": pct,
        "target_pct_source": target_pct_source,
        "plan_expected_return_pct": round(plan_expected_return_pct, 4),
        "effective_plan_expected_return_pct": round(effective_plan_expected_return_pct, 4),
        "allocation_source": allocation_plan["allocation_source"],
        "target_return_matches_goal": bool(allocation_plan["target_return_matches_goal"]),
        "target_position_overrides": allocation_plan["target_position_overrides"],
        "custom_target_positions_included": bool(allocation_plan["target_position_overrides"]),
        "phase_position_caps": allocation_plan.get("phase_position_caps") or {},
        "phase_dynamic_allocation_note": "无手动比例时自动采用月内阶段组合；有手动比例时保留手动计划显示，但新增建议按 min(手动上限, 阶段建议上限, 风险预算上限) 计算。",
        "dashboard_target_pct_included": target_pct_source != "backend_default",
        "manual_ui_realized_overrides_included": bool(normalized_realized_overrides),
        "realized_overrides": normalized_realized_overrides,
        "manual_ui_override_note": "Dashboard 保存的目标百分比、目标仓位占比和手动 realized 覆盖会进入 AI；未覆盖的 realized 档位继续使用自动 FIFO realized。防错预算始终使用自动 FIFO 统计的 gross realized loss，手动 realized 覆盖不能重置预算消耗。",
        "target_base_value_usdc": round(base, 2),
        "target_base_value_source": "monthly_progress.baseline_net_value_or_current_net_value",
        "requested_target_profit_usdc": round(base * pct / 100.0, 2) if base > 0 and pct > 0 else None,
        "total_target_profit_usdc": round(total_target_profit, 2) if total_target_profit is not None else None,
        "total_planned_position_usdc": round(total_planned_position, 2),
        "total_pending_buy_notional_usdc": round(total_pending_buy_notional, 2),
        "attributed_pending_buy_notional_usdc": round(total_pending_buy_notional - unattributed_pending_buy_notional, 2),
        "unattributed_pending_buy_notional_usdc": round(unattributed_pending_buy_notional, 2),
        "unattributed_pending_token_count": len(unattributed_pending_tokens),
        "unattributed_pending_token_ids": unattributed_pending_tokens[:20],
        "total_planned_position_pct": round(float(allocation_plan["total_position_pct"]), 4),
        "effective_total_position_pct": round(float(allocation_plan.get("effective_total_position_pct") or allocation_plan["total_position_pct"]), 4),
        "planned_return_pct": round(float(allocation_plan["planned_return_pct"]), 4),
        "effective_planned_return_pct": round(float(allocation_plan.get("effective_planned_return_pct") or allocation_plan["planned_return_pct"]), 4),
        "target_feasible_without_leverage": bool(allocation_plan["target_feasible_without_leverage"]),
        "total_target_shares": round(total_target_shares, 6),
        "total_realized_pnl_usdc": round(effective_total_realized, 2),
        "auto_total_realized_pnl_usdc": round(total_realized, 2),
        "classified_realized_pnl_usdc": round(classified_realized, 2),
        "unclassified_realized_pnl_usdc": round(unclassified_realized, 2),
        "total_gross_realized_loss_usdc": round(total_realized_loss, 2),
        "classified_gross_realized_loss_usdc": round(classified_realized_loss, 2),
        "unclassified_gross_realized_loss_usdc": round(unclassified_realized_loss, 2),
        "overall_loss_budget_pct": MONTHLY_GOAL_OVERALL_LOSS_BUDGET_PCT,
        "overall_loss_budget_usdc": round(overall_loss_budget_usdc, 2),
        "overall_loss_budget_remaining_usdc": round(max(0.0, overall_loss_budget_usdc - total_realized_loss), 2),
        "overall_loss_budget_usage_pct": round(overall_loss_usage_pct, 2) if overall_loss_usage_pct is not None else None,
        "overall_loss_budget_status": overall_loss_status,
        "untracked_sell_usdc": round(float(realized_summary.get("untracked_sell_usdc") or 0.0), 2),
        "total_remaining_profit_usdc": round(total_remaining, 2) if total_remaining is not None else None,
        "sum_tier_remaining_profit_usdc": round(total_tier_remaining, 2),
        "month_label": realized_summary.get("month_label"),
        "phase_key": phase_key,
        "phase_label": phase_label,
        "days_left_in_month": round(float(days_left_in_month or 0.0), 4),
        "current_phase_thresholds": thresholds,
        "tiers": tier_contexts,
        "core_exposure_status": exposure_classification,
        "auxiliary_exposure_groups": exposure_classification.get("auxiliary_groups") or [],
        "trade_review_context": trade_review_context,
        "discipline_notes": [
            "remaining_profit_usdc 只用于排序和选择机会，不能作为放宽止损、追价或突破仓位上限的理由。",
            "target_position_gap_usdc/headroom_to_position_limit_usdc 是仓位上限余量，不是必须补满的任务。",
            "新增建议必须优先使用 headroom_to_risk_adjusted_cap_usdc；若手动目标超过阶段建议，不得按手动上限补仓。",
            "active manual pending buy 已计入 current_committed_value_usdc 并扣减新增余量；unattributed pending 需要人工复核。",
            "entry_gate 为 block 时，不应新增该档买入；entry_gate 为 caution 时，只能小仓位且必须有更强确认。",
            "中价No需要同时满足价格区间、barrier distance、BTC未朝barrier快速移动；快速逼近后需要冷却确认。",
            "trade_review_context 的结论用于校准加权/降权；sample_quality=insufficient 只能提示观察，不得作为加仓依据。",
            "不得为了补某档缺口而买入不在 current_phase_threshold 内的 token。",
            "core_exposure_status 中主分类按入场/挂单意图锁定；out_of_band 只表示当前滑出区间，归因仍属于原 intent_tier。",
            "auxiliary_exposure_groups（尾部保险、近障碍战术等）只用于风险/退出管理，不参与四档目标收益、缺口补仓或 headroom 计算。",
            "若某档已超额完成，应优先保护利润或转入观察，除非出现更高质量且符合风控的机会。",
            "低价 Yes 缺口不能通过无催化彩票仓补；下方 dip No 缺口不能通过第一次逼近 barrier 逆势抄底补。",
        ],
    }
