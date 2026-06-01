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


def calculate_monthly_goal_tier_allocations(
    target_pct: float,
    target_position_overrides: dict[str, Any] | None = None,
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
        return sum(
            positions[idx] * float(tier["return_pct"]) / 100.0
            for idx, tier in enumerate(MONTHLY_GOAL_TIERS)
        )

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

    normalized_position_overrides = _normalize_target_position_overrides(target_position_overrides or {})
    if normalized_position_overrides:
        for idx, tier in enumerate(MONTHLY_GOAL_TIERS):
            tier_key = str(tier["key"])
            if tier_key in normalized_position_overrides:
                positions[idx] = normalized_position_overrides[tier_key]

    allocations = []
    planned = planned_return()
    for idx, tier in enumerate(MONTHLY_GOAL_TIERS):
        position_pct = max(0.0, positions[idx])
        contribution_pct = position_pct * float(tier["return_pct"]) / 100.0
        allocations.append({
            "tier_key": tier["key"],
            "target_position_pct": position_pct,
            "target_contribution_pct": contribution_pct,
            "target_profit_share_pct": (
                contribution_pct / planned * 100.0 if planned > 0 else 0.0
            ),
        })
    return {
        "target_pct": target,
        "planned_return_pct": planned,
        "allocation_source": "dashboard_custom_positions" if normalized_position_overrides else "target_model",
        "target_position_overrides": normalized_position_overrides,
        "target_feasible_without_leverage": sum(item["target_position_pct"] for item in allocations) <= 100.000001,
        "target_return_matches_goal": abs(planned - target) <= 1e-6,
        "total_position_pct": sum(item["target_position_pct"] for item in allocations),
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
        "as_of_utc": as_of_utc.astimezone(timezone.utc).isoformat(),
        "end_utc": end_utc.astimezone(timezone.utc).isoformat() if end_utc else None,
        "tier_key": None,
        "tier_label": None,
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
    snapshot.update({
        "strike": strike,
        "direction": direction,
        "yes_price": round(yes_price, 6),
        "no_price": round(no_price, 6),
        "distance_pct": round(distance_pct, 6),
        "days_left": round(days_left, 6),
        "phase_key": phase_key,
        "phase_label": phase_label,
        "tier_key": tier_key,
        "tier_label": ACTIONABLE_TIER_LABELS.get(tier_key),
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
              AND event_slug LIKE 'what-price-will-bitcoin-hit-in%%'
            ORDER BY fill_timestamp ASC, id ASC
            """,
            (fill_profile, month_end_utc),
        )
        rows = list(cur.fetchall())

    state: dict[str, list[dict]] = {}
    by_token: dict[str, dict] = {}
    by_tier: dict[str, dict] = {
        key: {"tier_key": key, "tier_label": label, "realized_pnl": 0.0, "sell_shares": 0.0}
        for key, label in ACTIONABLE_TIER_LABELS.items()
    }
    total_realized = 0.0
    classified_realized = 0.0
    unclassified_realized = 0.0
    untracked_sell_usdc = 0.0
    untracked_sell_shares = 0.0
    data_start = None
    data_end = None

    def _token_item(row: dict, token_id: str) -> dict:
        return by_token.setdefault(token_id, {
            "token_id": token_id,
            "realized_pnl": 0.0,
            "classified_realized_pnl": 0.0,
            "unclassified_realized_pnl": 0.0,
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
        nonlocal total_realized, classified_realized, unclassified_realized
        item = _token_item(row, token_id)
        item["sell_shares"] += shares
        item["sell_usdc"] += proceeds
        item["matched_sell_shares"] += shares
        item["matched_sell_usdc"] += proceeds
        item["realized_pnl"] += realized
        total_realized += realized
        if tier_key in by_tier:
            bucket = by_tier[tier_key]
            bucket["realized_pnl"] += realized
            bucket["sell_shares"] += shares
            token_bucket = item["by_tier"].setdefault(tier_key, {
                "tier_key": tier_key,
                "tier_label": tier_label or ACTIONABLE_TIER_LABELS.get(tier_key),
                "realized_pnl": 0.0,
                "sell_shares": 0.0,
            })
            token_bucket["realized_pnl"] += realized
            token_bucket["sell_shares"] += shares
            item["classified_realized_pnl"] += realized
            classified_realized += realized
        else:
            item["unclassified_realized_pnl"] += realized
            unclassified_realized += realized

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
            "realized_pnl", "classified_realized_pnl", "unclassified_realized_pnl",
            "sell_shares", "sell_usdc", "matched_sell_shares", "matched_sell_usdc",
            "untracked_sell_shares", "untracked_sell_usdc",
        ):
            item[key] = round(float(item[key] or 0.0), 6)
        for tier_item in item.get("by_tier", {}).values():
            tier_item["realized_pnl"] = round(float(tier_item["realized_pnl"] or 0.0), 6)
            tier_item["sell_shares"] = round(float(tier_item["sell_shares"] or 0.0), 6)
    for tier_item in by_tier.values():
        tier_item["realized_pnl"] = round(float(tier_item["realized_pnl"] or 0.0), 6)
        tier_item["sell_shares"] = round(float(tier_item["sell_shares"] or 0.0), 6)

    return {
        "month_label": month_label,
        "month_start_et": month_start_et.isoformat(),
        "month_end_et": month_end_et.isoformat(),
        "source": "advisory_chain_fills",
        "profile": fill_profile,
        "attribution": "entry_tier_fifo",
        "data_start": data_start.isoformat() if data_start else None,
        "data_end": data_end.isoformat() if data_end else None,
        "total_realized": round(total_realized, 6),
        "classified_realized": round(classified_realized, 6),
        "unclassified_realized": round(unclassified_realized, 6),
        "untracked_sell_usdc": round(untracked_sell_usdc, 6),
        "untracked_sell_shares": round(untracked_sell_shares, 6),
        "by_tier": by_tier,
        "by_token": by_token,
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
    profile: str = "analyze",
) -> dict[str, Any]:
    """构建供 AI 使用的本月目标分层上下文。"""
    realized_summary = realized_summary or get_monthly_goal_realized_summary(profile=profile)
    base = max(0.0, _to_float(base_value, 0.0) or 0.0)
    pct = max(0.0, _to_float(target_pct, MONTHLY_GOAL_DEFAULT_TARGET_PCT) or 0.0)
    total_target_profit = base * pct / 100.0 if base > 0 and pct > 0 else None
    phase_key, phase_label = _classify_time_phase(days_left_in_month)
    thresholds = _current_phase_thresholds(phase_key)
    by_token, by_question_outcome = _build_position_indexes(positions)
    normalized_realized_overrides = _normalize_realized_overrides(realized_overrides or {})

    candidates_by_tier: dict[str, list[dict]] = {tier["key"]: [] for tier in MONTHLY_GOAL_TIERS}
    markets = polymarket_event_situation.get("markets", []) if isinstance(polymarket_event_situation, dict) else []
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
            outcome_index = next(
                (idx for idx, name in enumerate(outcomes)
                 if str(name or "").strip().lower() == str(tier["outcome"]).lower()),
                -1,
            )
            if outcome_index < 0:
                continue
            token_id = str(token_ids[outcome_index]) if outcome_index < len(token_ids) else ""
            held = by_token.get(token_id) if token_id else None
            if held is None:
                held = by_question_outcome.get(_position_match_key(question, tier["outcome"]), {"shares": 0.0, "value": 0.0})
            price = _entry_price_for_outcome(market, outcome_index)
            candidates_by_tier[tier["key"]].append({
                "question": question,
                "outcome": tier["outcome"],
                "token_id": token_id or None,
                "price": round(price, 6) if price is not None else None,
                "strike": round(float(strike), 2),
                "direction_in_question": direction,
                "distance_pct": round(distance_pct, 2),
                "held_shares": round(float(held.get("shares") or 0.0), 6),
                "held_value_usdc": round(float(held.get("value") or 0.0), 2),
            })
    for values in candidates_by_tier.values():
        values.sort(key=lambda item: item.get("strike") or 0.0, reverse=True)

    realized_by_tier = realized_summary.get("by_tier") or {}
    tier_contexts: list[dict[str, Any]] = []
    total_planned_position = 0.0
    total_tier_remaining = 0.0
    total_target_shares = 0.0
    allocation_plan = calculate_monthly_goal_tier_allocations(pct, target_position_overrides)
    plan_expected_return_pct = float(allocation_plan["planned_return_pct"])
    total_target_profit = (
        base * plan_expected_return_pct / 100.0
        if base > 0 and plan_expected_return_pct > 0 else None
    )
    allocations_by_tier = {
        item["tier_key"]: item
        for item in allocation_plan["allocations"]
    }
    for tier in MONTHLY_GOAL_TIERS:
        tier_key = tier["key"]
        allocation = allocations_by_tier.get(tier_key) or {}
        target_position_pct = float(allocation.get("target_position_pct") or 0.0)
        target_contribution_pct = float(allocation.get("target_contribution_pct") or 0.0)
        target_profit_share_pct = float(allocation.get("target_profit_share_pct") or 0.0)
        target_position = (
            base * target_position_pct / 100.0
            if base > 0 and target_position_pct >= 0 else None
        )
        tier_target_profit = (
            base * target_contribution_pct / 100.0
            if base > 0 and target_contribution_pct >= 0 else None
        )
        candidates = candidates_by_tier[tier_key]
        per_candidate_target = target_position / len(candidates) if target_position is not None and candidates else None
        target_shares = None
        enriched_candidates = []
        if per_candidate_target is not None:
            target_shares = 0.0
        for candidate in candidates:
            price = candidate.get("price")
            candidate_target_shares = None
            if per_candidate_target is not None and price:
                candidate_target_shares = per_candidate_target / float(price)
                target_shares += candidate_target_shares
            enriched = dict(candidate)
            enriched["target_position_usdc"] = round(per_candidate_target, 2) if per_candidate_target is not None else None
            enriched["target_shares"] = round(candidate_target_shares, 6) if candidate_target_shares is not None else None
            enriched_candidates.append(enriched)
        realized_row = realized_by_tier.get(tier_key) if isinstance(realized_by_tier, dict) else None
        auto_realized = float((realized_row or {}).get("realized_pnl") or 0.0)
        has_realized_override = tier_key in normalized_realized_overrides
        realized = normalized_realized_overrides[tier_key] if has_realized_override else auto_realized
        remaining = max(0.0, (tier_target_profit or 0.0) - realized) if tier_target_profit is not None else None
        held_value = sum(float(c.get("held_value_usdc") or 0.0) for c in candidates)
        held_shares = sum(float(c.get("held_shares") or 0.0) for c in candidates)
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
            "target_contribution_pct": round(target_contribution_pct, 4),
            "target_profit_share_pct": round(target_profit_share_pct, 4),
            "target_profit_usdc": round(tier_target_profit, 2) if tier_target_profit is not None else None,
            "target_position_usdc": round(target_position, 2) if target_position is not None else None,
            "current_held_value_usdc": round(held_value, 2),
            "current_held_shares": round(held_shares, 6),
            "target_position_gap_usdc": round(max(0.0, (target_position or 0.0) - held_value), 2) if target_position is not None else None,
            "target_shares": round(target_shares, 6) if target_shares is not None else None,
            "realized_pnl_usdc": round(realized, 2),
            "auto_realized_pnl_usdc": round(auto_realized, 2),
            "realized_override_applied": has_realized_override,
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
        "allocation_source": allocation_plan["allocation_source"],
        "target_return_matches_goal": bool(allocation_plan["target_return_matches_goal"]),
        "target_position_overrides": allocation_plan["target_position_overrides"],
        "custom_target_positions_included": bool(allocation_plan["target_position_overrides"]),
        "dashboard_target_pct_included": target_pct_source != "backend_default",
        "manual_ui_realized_overrides_included": bool(normalized_realized_overrides),
        "realized_overrides": normalized_realized_overrides,
        "manual_ui_override_note": "Dashboard 保存的目标百分比、目标仓位占比和手动 realized 覆盖会进入 AI；未覆盖的 realized 档位继续使用自动 FIFO realized。",
        "target_base_value_usdc": round(base, 2),
        "target_base_value_source": "monthly_progress.baseline_net_value_or_current_net_value",
        "requested_target_profit_usdc": round(base * pct / 100.0, 2) if base > 0 and pct > 0 else None,
        "total_target_profit_usdc": round(total_target_profit, 2) if total_target_profit is not None else None,
        "total_planned_position_usdc": round(total_planned_position, 2),
        "total_planned_position_pct": round(float(allocation_plan["total_position_pct"]), 4),
        "planned_return_pct": round(float(allocation_plan["planned_return_pct"]), 4),
        "target_feasible_without_leverage": bool(allocation_plan["target_feasible_without_leverage"]),
        "total_target_shares": round(total_target_shares, 6),
        "total_realized_pnl_usdc": round(effective_total_realized, 2),
        "auto_total_realized_pnl_usdc": round(total_realized, 2),
        "classified_realized_pnl_usdc": round(classified_realized, 2),
        "unclassified_realized_pnl_usdc": round(unclassified_realized, 2),
        "untracked_sell_usdc": round(float(realized_summary.get("untracked_sell_usdc") or 0.0), 2),
        "total_remaining_profit_usdc": round(total_remaining, 2) if total_remaining is not None else None,
        "sum_tier_remaining_profit_usdc": round(total_tier_remaining, 2),
        "month_label": realized_summary.get("month_label"),
        "phase_key": phase_key,
        "phase_label": phase_label,
        "days_left_in_month": round(float(days_left_in_month or 0.0), 4),
        "current_phase_thresholds": thresholds,
        "tiers": tier_contexts,
        "discipline_notes": [
            "remaining_profit_usdc 只用于排序和选择机会，不能作为放宽止损、追价或突破仓位上限的理由。",
            "不得为了补某档缺口而买入不在 current_phase_threshold 内的 token。",
            "若某档已超额完成，应优先保护利润或转入观察，除非出现更高质量且符合风控的机会。",
            "低价 Yes 缺口不能通过无催化彩票仓补；下方 dip No 缺口不能通过第一次逼近 barrier 逆势抄底补。",
        ],
    }
