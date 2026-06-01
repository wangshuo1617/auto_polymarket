"""
AI prompt 长度观测指标持久化。
"""

from __future__ import annotations

import json
from typing import Any

from psycopg2.extras import Json

from data.database import get_conn

_DDL_AI_PROMPT_METRICS = """
CREATE TABLE IF NOT EXISTS ai_prompt_metrics (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    asset TEXT NOT NULL DEFAULT 'btc',
    analysis_kind TEXT NOT NULL DEFAULT 'position_analyze',
    profile TEXT NOT NULL DEFAULT 'analyze',
    source_run_id INTEGER,
    model_id TEXT,
    prompt_family TEXT,
    prompt_version TEXT,
    system_prompt_hash TEXT,
    schema_hash TEXT,
    measurement_source TEXT,
    system_chars INTEGER,
    user_chars INTEGER,
    schema_chars INTEGER,
    system_user_chars INTEGER,
    total_chars_with_schema INTEGER,
    estimated_tokens INTEGER,
    estimated_tokens_method TEXT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_ai_prompt_metrics_created_at ON ai_prompt_metrics(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ai_prompt_metrics_source_run_id ON ai_prompt_metrics(source_run_id);
"""


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def ensure_ai_prompt_metrics_table() -> None:
    """确保 prompt metrics 表存在。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_DDL_AI_PROMPT_METRICS)


def persist_ai_prompt_metrics(
    *,
    asset: str,
    analysis_kind: str,
    profile: str,
    source_run_id: int | None,
    model_id: str | None,
    prompt_family: str | None,
    prompt_version: str | None,
    system_prompt_hash: str | None,
    schema_hash: str | None,
    metrics: dict[str, Any],
) -> int | None:
    """写入一次 AI prompt 长度观测。"""
    if not isinstance(metrics, dict) or not metrics:
        return None
    ensure_ai_prompt_metrics_table()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ai_prompt_metrics (
                    asset, analysis_kind, profile, source_run_id,
                    model_id, prompt_family, prompt_version,
                    system_prompt_hash, schema_hash, measurement_source,
                    system_chars, user_chars, schema_chars, system_user_chars,
                    total_chars_with_schema, estimated_tokens,
                    estimated_tokens_method, metrics
                ) VALUES (
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s,
                    %s, %s
                )
                RETURNING id
                """,
                (
                    asset,
                    analysis_kind,
                    profile,
                    source_run_id,
                    model_id,
                    prompt_family,
                    prompt_version,
                    system_prompt_hash,
                    schema_hash,
                    metrics.get("measurement_source"),
                    metrics.get("system_chars"),
                    metrics.get("user_chars"),
                    metrics.get("schema_chars"),
                    metrics.get("system_user_chars"),
                    metrics.get("total_chars_with_schema"),
                    metrics.get("estimated_tokens_chars_div_4"),
                    metrics.get("estimated_tokens_method"),
                    Json(metrics, dumps=_json_dumps),
                ),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
