"""PathView shadow runner (Phase B1 — no production effect).

NEVER mutates `path_views` / `market_view_snapshots` / `advisory_intents`.
Writes only to `advisory_pathview_shadow_runs` + `advisory_pathview_shadow_views`.

Usage:
  - B1 (now): `record_baseline_replay(batch_id)` 把当前 batch 的 GBM PathView
    包成 shadow payload, 跑 validator, 写 shadow 表。用作 B4 baseline 对照
    + validator 回归测试样本。
  - B3 (future): `record_ai_run(batch_id, ai_payload)` 同样写 shadow 表,
    但 source='ai'。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from data.database import get_conn
from services.advisory.pathview_validator import (
    ValidationResult,
    validate_pathview_payload,
)

logger = logging.getLogger(__name__)


def _fetch_batch_baseline(batch_id: int) -> Optional[dict]:
    """Pull current batch's GBM PathView + market_view_snapshots, repack as
    shadow payload (acts as gbm_baseline_replay source for B4 comparison)."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT b.as_of_utc, pv.sigma_daily, pv.per_token_fair
            FROM market_view_batches b
            LEFT JOIN path_views pv ON pv.id = b.path_view_id
            WHERE b.id = %s
            """,
            (batch_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        as_of, sigma, per_token_fair_json = row

        cur.execute(
            """
            SELECT token_id, view_payload
            FROM market_view_snapshots
            WHERE batch_id = %s
            """,
            (batch_id,),
        )
        snaps = cur.fetchall()

    fair_map = per_token_fair_json or {}
    per_token: list[dict] = []
    for tok_id, vp in snaps:
        vp = vp or {}
        f = fair_map.get(tok_id, {})
        fair_event = f.get("fair_calibrated")
        if fair_event is None:
            fair_event = vp.get("fair_event")
        fair_non_event = (1.0 - fair_event) if isinstance(fair_event, (int, float)) else None
        # baseline 用 fair_event 当 p_event_yes 的代理 (yes 视角已合并 no 翻转)
        p_event_yes = fair_event
        per_token.append({
            "token_id": tok_id,
            "p_event_yes": p_event_yes,
            "fair_event": fair_event,
            "fair_non_event": fair_non_event,
            "fair_value_status": vp.get("fair_value_status") or "available",
            "strike_usd": f.get("strike_usd"),
            "side_above": f.get("side_above"),
        })

    return {
        "as_of_utc": (as_of.isoformat() if isinstance(as_of, datetime) else str(as_of)),
        "sigma_daily": float(sigma) if sigma is not None else None,
        "per_token": per_token,
        "key_levels": [],
    }


def _persist_shadow_run(
    batch_id: int,
    source: str,
    payload: dict,
    validation: ValidationResult,
    *,
    model_id: Optional[str] = None,
    model_version: Optional[str] = None,
    prompt_version: Optional[str] = None,
    request_latency_ms: Optional[int] = None,
    inputs_hash: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO advisory_pathview_shadow_runs
              (batch_id, source, model_id, model_version, prompt_version,
               request_latency_ms, inputs_hash, raw_payload,
               validation_status, validation_errors, validation_warnings, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s)
            RETURNING id
            """,
            (
                batch_id, source, model_id, model_version, prompt_version,
                request_latency_ms, inputs_hash, json.dumps(payload),
                validation.status,
                json.dumps(validation.errors) if validation.errors else None,
                json.dumps(validation.warnings) if validation.warnings else None,
                notes,
            ),
        )
        run_id = cur.fetchone()[0]

        rows = []
        for tok in payload.get("per_token") or []:
            rows.append((
                run_id,
                tok.get("token_id"),
                tok.get("p_event_yes"),
                tok.get("fair_event"),
                tok.get("fair_non_event"),
                tok.get("fair_value_status"),
                json.dumps({k: v for k, v in tok.items()
                            if k not in ("token_id",)}),
            ))
        if rows:
            cur.executemany(
                """
                INSERT INTO advisory_pathview_shadow_views
                  (run_id, token_id, p_event_yes, fair_event, fair_non_event,
                   fair_value_status, components)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (run_id, token_id) DO NOTHING
                """,
                rows,
            )
        conn.commit()
    return run_id


def record_baseline_replay(batch_id: int) -> Optional[int]:
    """Self-test: 把当前 batch 的 GBM PathView 当 shadow payload 落库,
    用以验证 validator + shadow schema, 同时为 B4 提供 baseline 对照行。
    Returns shadow_run_id 或 None (batch 不存在)。"""
    payload = _fetch_batch_baseline(batch_id)
    if payload is None:
        logger.warning("baseline replay: batch %s not found", batch_id)
        return None

    try:
        batch_as_of = datetime.fromisoformat(
            str(payload["as_of_utc"]).replace("Z", "+00:00"))
        if batch_as_of.tzinfo is None:
            batch_as_of = batch_as_of.replace(tzinfo=timezone.utc)
    except Exception:
        batch_as_of = datetime.now(timezone.utc)

    baseline_map = {
        t["token_id"]: t["fair_event"]
        for t in payload["per_token"]
        if t.get("fair_event") is not None
    }

    validation = validate_pathview_payload(
        payload,
        batch_as_of_utc=batch_as_of,
        baseline_fair_by_token=baseline_map,
    )

    run_id = _persist_shadow_run(
        batch_id, source="gbm_baseline_replay",
        payload=payload, validation=validation,
        model_id="gbm_v1", model_version="phase_a", prompt_version=None,
        notes="self_test_baseline_replay",
    )
    logger.info(
        "shadow baseline replay: batch=%s run_id=%s status=%s errors=%d warnings=%d",
        batch_id, run_id, validation.status,
        len(validation.errors), len(validation.warnings),
    )
    return run_id


def record_ai_run(
    batch_id: int,
    ai_payload: dict,
    *,
    model_id: str,
    model_version: str,
    prompt_version: str,
    request_latency_ms: Optional[int] = None,
    baseline_fair_by_token: Optional[dict[str, float]] = None,
) -> int:
    """Stub: B3 接 AI 后调用. 此处仅校验 + 落 shadow 表, 完全不影响生产."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT as_of_utc FROM market_view_batches WHERE id=%s", (batch_id,))
        row = cur.fetchone()
    batch_as_of = row[0] if row and isinstance(row[0], datetime) \
                  else datetime.now(timezone.utc)
    if batch_as_of.tzinfo is None:
        batch_as_of = batch_as_of.replace(tzinfo=timezone.utc)

    validation = validate_pathview_payload(
        ai_payload,
        batch_as_of_utc=batch_as_of,
        baseline_fair_by_token=baseline_fair_by_token,
    )
    run_id = _persist_shadow_run(
        batch_id, source="ai",
        payload=ai_payload, validation=validation,
        model_id=model_id, model_version=model_version,
        prompt_version=prompt_version,
        request_latency_ms=request_latency_ms,
    )
    logger.info(
        "shadow ai run: batch=%s run_id=%s status=%s errors=%d warnings=%d",
        batch_id, run_id, validation.status,
        len(validation.errors), len(validation.warnings),
    )
    return run_id
