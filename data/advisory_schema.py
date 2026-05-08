"""Advisory-only fair-value rebalancer schema.

落库 7 张表, 沿用 data/database.py 的 idempotent CREATE TABLE IF NOT EXISTS 模式。
对应 plan-advisory.md v1.3 §4.1。

Enum 集中定义, 与 §1.5.5 状态机/§1.6 MarketView 字段对齐。A1-lint 会校验本文件
与 plan-advisory.md 文本中所有出现位置完全一致, 不允许漂移。
"""

from __future__ import annotations

import logging

from data.database import get_conn

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Enum 字面量 (与 plan-advisory.md / plan.md v2.27 freeze 对齐)
#  A1-lint 必须验证以下集合在 schema CHECK / Python enum / plan 文本一致
# ---------------------------------------------------------------------------

RESOLUTION_STATES = (
    "open",
    "locked_event_occurred",
    "locked_event_missed",
    "settled",
    "unknown",
)

HALT_REASONS = (
    "stale_btc",
    "stale_view",
    "divergence_high",
    "risk_cap",
    "liquidity_thin",
    "low_apr_better_alternative",
    "wick_risk_high",
    "awaiting_resolution",
    "local_touch_unconfirmed",
    "settlement_lag",
    "price_source_mismatch",
    "settlement_local_conflict",
    "settlement_baseline_missing",
    "settlement_disputed",
)

FAIR_VALUE_STATUSES = (
    "available",
    "placeholder",
    "unavailable",
    "locked_event_occurred",
    "locked_event_missed",
    "settled",
)

SETTLEMENT_STATES = ("pending", "settled", "disputed")

REFRESH_STATUSES = ("ok", "partial", "failed")

BATCH_STATUSES = ("started", "complete", "failed")


def _enum_check(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"CHECK ({column} IN ({quoted}))"


# ---------------------------------------------------------------------------
#  DDL
# ---------------------------------------------------------------------------

_DDL_PATH_OBSERVATION_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS path_observation_snapshots (
    id                              BIGSERIAL PRIMARY KEY,
    generated_at_utc                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    as_of_utc                       TIMESTAMPTZ NOT NULL,
    btc_tick_feed_source            TEXT NOT NULL,
    btc_tick_feed_version           TEXT NOT NULL,
    latest_tick_ts_utc              TIMESTAMPTZ NOT NULL,
    settlement_feed_version         BIGINT,
    settlement_refresh_effect_hash  TEXT,
    per_token_observations          JSONB NOT NULL,
    inputs_hash                     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS path_observation_snapshots_gen_idx
    ON path_observation_snapshots (generated_at_utc DESC);
"""

_DDL_SETTLEMENT_FEED_VERSIONS = """
CREATE TABLE IF NOT EXISTS settlement_feed_versions (
    settlement_feed_version  BIGSERIAL PRIMARY KEY,
    refreshed_at_utc         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    refresh_status           TEXT NOT NULL {refresh_check},
    rows_upserted            INTEGER NOT NULL DEFAULT 0,
    refreshed_condition_ids  JSONB NOT NULL DEFAULT '[]'::jsonb,
    missing_condition_ids    JSONB NOT NULL DEFAULT '[]'::jsonb,
    source_etag              TEXT
);
CREATE INDEX IF NOT EXISTS settlement_feed_versions_status_idx
    ON settlement_feed_versions (refresh_status, refreshed_at_utc DESC);
""".format(refresh_check=_enum_check("refresh_status", REFRESH_STATUSES))

_DDL_SETTLEMENT_FEED_RECORDS = """
CREATE TABLE IF NOT EXISTS settlement_feed_records (
    settlement_feed_version        BIGINT NOT NULL
        REFERENCES settlement_feed_versions(settlement_feed_version),
    condition_id                   TEXT NOT NULL,
    market_slug                    TEXT,
    settlement_state               TEXT NOT NULL {settlement_check},
    settlement_outcome_event_bool  BOOLEAN,
    winning_token_id               TEXT,
    final_price                    DOUBLE PRECISION,
    settled_at_utc                 TIMESTAMPTZ,
    settlement_source              TEXT,
    raw_payload                    JSONB,
    ingested_at_utc                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (settlement_feed_version, condition_id)
);
CREATE INDEX IF NOT EXISTS settlement_feed_records_market_idx
    ON settlement_feed_records (market_slug);
CREATE INDEX IF NOT EXISTS settlement_feed_records_state_idx
    ON settlement_feed_records (settlement_state);
CREATE INDEX IF NOT EXISTS settlement_feed_records_cond_ver_idx
    ON settlement_feed_records (condition_id, settlement_feed_version DESC);
""".format(settlement_check=_enum_check("settlement_state", SETTLEMENT_STATES))

_DDL_MARKET_VIEW_BATCHES = """
CREATE TABLE IF NOT EXISTS market_view_batches (
    id                              BIGSERIAL PRIMARY KEY,
    batch_sequence                  BIGSERIAL UNIQUE NOT NULL,
    generated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    as_of_utc                       TIMESTAMPTZ NOT NULL,
    path_view_id                    BIGINT,
    input_quote_snapshot_id         BIGINT,
    path_observation_snapshot_id    BIGINT
        REFERENCES path_observation_snapshots(id),
    settlement_feed_version         BIGINT
        REFERENCES settlement_feed_versions(settlement_feed_version),
    settlement_refresh_state        JSONB,
    settlement_refresh_effect_hash  TEXT,
    inputs_hash                     TEXT,
    token_count                     INTEGER,
    status                          TEXT NOT NULL DEFAULT 'started' {status_check},
    batch_completed_at              TIMESTAMPTZ,
    failure_step                    TEXT,
    failure_error                   TEXT,
    sigma_panel                     JSONB
);
ALTER TABLE market_view_batches
    ADD COLUMN IF NOT EXISTS sigma_panel JSONB;
CREATE INDEX IF NOT EXISTS market_view_batches_status_seq_idx
    ON market_view_batches (status, batch_sequence DESC);
CREATE INDEX IF NOT EXISTS market_view_batches_completed_idx
    ON market_view_batches (batch_completed_at DESC)
    WHERE status = 'complete';
""".format(status_check=_enum_check("status", BATCH_STATUSES))

# market_view_snapshots: 1.6 全部字段以 JSONB view_payload 存 + 索引/查询常用列提取出来
_DDL_MARKET_VIEW_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS market_view_snapshots (
    id                              BIGSERIAL PRIMARY KEY,
    batch_id                        BIGINT NOT NULL REFERENCES market_view_batches(id),
    token_id                        TEXT NOT NULL,
    path_view_id                    BIGINT,
    path_observation_snapshot_id    BIGINT REFERENCES path_observation_snapshots(id),
    generated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- 索引/排序常用列 (与 view_payload 一致, 写入时由 L2 Computer 同步)
    resolution_state                TEXT NOT NULL {resolution_check},
    halt_reason                     TEXT {halt_check},
    fair_value_status               TEXT NOT NULL {fair_check},
    settlement_state                TEXT {settlement_check},
    market_slug                     TEXT,
    condition_id                    TEXT,
    fair_value_for_edge             DOUBLE PRECISION,
    edge_buy_active                 DOUBLE PRECISION,
    expected_apr_by_intent          DOUBLE PRECISION,
    ranking_score                   DOUBLE PRECISION,
    target_position_usdc            DOUBLE PRECISION,
    current_position_usdc           DOUBLE PRECISION,
    delta_usdc                      DOUBLE PRECISION,

    view_payload                    JSONB NOT NULL,
    inputs_hash                     TEXT NOT NULL,

    UNIQUE (batch_id, token_id)
);
CREATE INDEX IF NOT EXISTS market_view_snapshots_batch_idx
    ON market_view_snapshots (batch_id);
CREATE INDEX IF NOT EXISTS market_view_snapshots_token_gen_idx
    ON market_view_snapshots (token_id, generated_at DESC);
CREATE INDEX IF NOT EXISTS market_view_snapshots_ranking_idx
    ON market_view_snapshots (batch_id, ranking_score DESC NULLS LAST);
""".format(
    resolution_check=_enum_check("resolution_state", RESOLUTION_STATES),
    halt_check=_enum_check("halt_reason", HALT_REASONS).replace("CHECK (", "CHECK (halt_reason IS NULL OR "),
    fair_check=_enum_check("fair_value_status", FAIR_VALUE_STATUSES),
    settlement_check=_enum_check("settlement_state", SETTLEMENT_STATES).replace(
        "CHECK (", "CHECK (settlement_state IS NULL OR "
    ),
)

_DDL_MARKET_VIEW_LATEST = """
CREATE TABLE IF NOT EXISTS market_view_latest (
    token_id          TEXT PRIMARY KEY,
    batch_id          BIGINT NOT NULL REFERENCES market_view_batches(id),
    batch_sequence    BIGINT NOT NULL,
    snapshot_id       BIGINT NOT NULL REFERENCES market_view_snapshots(id),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS market_view_latest_seq_idx
    ON market_view_latest (batch_sequence DESC);
"""

# NOTE (v2 D1 cleanup, 2026-05-07): manual_trades 已退役, 由 advisory_intents
# (决策意图) + advisory_chain_fills (链上事实) 取代. 历史数据归档为
# manual_trades_archived_2026_05_07 (PG 中保留 90 天).


_DDL_INPUT_QUOTE_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS input_quote_snapshots (
    id                  BIGSERIAL PRIMARY KEY,
    captured_at_utc     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    source              TEXT NOT NULL,
    per_token_quote     JSONB NOT NULL,
    inputs_hash         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS input_quote_snapshots_captured_idx
    ON input_quote_snapshots (captured_at_utc DESC);
"""

_DDL_PATH_VIEWS = """
CREATE TABLE IF NOT EXISTS path_views (
    id                              BIGSERIAL PRIMARY KEY,
    generated_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    as_of_utc                       TIMESTAMPTZ NOT NULL,
    path_observation_snapshot_id    BIGINT NOT NULL
        REFERENCES path_observation_snapshots(id),
    input_quote_snapshot_id         BIGINT
        REFERENCES input_quote_snapshots(id),
    current_btc_price               DOUBLE PRECISION NOT NULL,
    sigma_daily                     DOUBLE PRECISION NOT NULL,
    sigma_source                    TEXT NOT NULL,
    sigma_is_iv                     BOOLEAN NOT NULL DEFAULT FALSE,
    drift_daily                     DOUBLE PRECISION NOT NULL DEFAULT 0,
    days_left                       DOUBLE PRECISION NOT NULL,
    per_token_fair                  JSONB NOT NULL,
    inputs_hash                     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS path_views_gen_idx
    ON path_views (generated_at DESC);
CREATE INDEX IF NOT EXISTS path_views_path_obs_idx
    ON path_views (path_observation_snapshot_id);
"""


# advisory_user_theses (P2): user 自由文本判断 (e.g. "我觉得 BTC 接下来会冲 90k").
# 持久化到 PG, 由 inputs.assemble_batch_inputs 注入 BatchInputs.user_thesis_text;
# 改变文本 → inputs_hash 改变 → 触发新 batch (cache invalidation).
# 当前未连入 AI prompt (advisory pipeline 暂无 AI 调用); 文本随 batch 一起记录,
# 后续 AI 上线后可直接消费.
_DDL_USER_THESES = """
CREATE TABLE IF NOT EXISTS advisory_user_theses (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at      TIMESTAMPTZ NOT NULL,
    thesis_text     TEXT NOT NULL CHECK (length(thesis_text) BETWEEN 1 AND 4000),
    cleared_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS advisory_user_theses_active_idx
    ON advisory_user_theses (expires_at DESC)
    WHERE cleared_at IS NULL;
"""


_DDL_CALIBRATION_RUNS = """
CREATE TABLE IF NOT EXISTS advisory_calibration_runs (
    id              BIGSERIAL PRIMARY KEY,
    run_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    since_utc       TIMESTAMPTZ,
    n_snapshots     INTEGER NOT NULL DEFAULT 0,
    brier           DOUBLE PRECISION,
    n_trades        INTEGER NOT NULL DEFAULT 0,
    n_trades_settled INTEGER NOT NULL DEFAULT 0,
    total_pnl_usdc  DOUBLE PRECISION,
    calibration_json JSONB NOT NULL,
    trades_json     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS advisory_calibration_runs_run_at_idx
    ON advisory_calibration_runs (run_at DESC);
"""


# ---------------------------------------------------------------------------
#  V2: Intent / Fill 双源拆分 (plan-advisory-v2.md)
#  - advisory_intents: dashboard 三类意图 (place_buy / place_sell / cancel)
#  - advisory_chain_fills: 链上 activity 事实 cache (60s poller 写入)
#  - advisory_chain_fills_poller_state: poller 增量游标
# ---------------------------------------------------------------------------

INTENT_KINDS = ("place_buy", "place_sell", "cancel")
INTENT_STATUSES = (
    "open",
    "filled",
    "partial",
    "cancelled_clean",
    "cancelled_with_fills",
    "rejected",
    "orphan",
)
INTENT_SUBMISSION_STATUSES = ("submitted", "rejected", "unknown")

_DDL_ADVISORY_INTENTS = """
CREATE TABLE IF NOT EXISTS advisory_intents (
    id                                       BIGSERIAL PRIMARY KEY,
    created_at                               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind                                     TEXT NOT NULL {kind_check},
    token_id                                 TEXT,
    intended_side                            TEXT CHECK (intended_side IS NULL OR intended_side IN ('buy','sell')),
    intended_price                           DOUBLE PRECISION
        CHECK (intended_price IS NULL OR (intended_price > 0 AND intended_price < 1)),
    intended_size_shares                     DOUBLE PRECISION
        CHECK (intended_size_shares IS NULL OR intended_size_shares > 0),
    intended_size_usdc                       DOUBLE PRECISION
        CHECK (intended_size_usdc IS NULL OR intended_size_usdc > 0),
    fair_at_decision                         DOUBLE PRECISION,
    edge_at_decision                         DOUBLE PRECISION,
    market_view_snapshot_id_at_decision      BIGINT REFERENCES market_view_snapshots(id),
    cancel_target_order_id                   TEXT,
    cancel_target_intent_id                  BIGINT REFERENCES advisory_intents(id),
    user_note                                TEXT,
    polymarket_order_id                      TEXT,
    submission_status                        TEXT NOT NULL DEFAULT 'unknown' {submission_check},
    submission_payload_json                  JSONB,
    intent_status                            TEXT NOT NULL DEFAULT 'open' {status_check},
    linked_fill_ids                          BIGINT[] NOT NULL DEFAULT '{{}}',
    last_status_check_at                     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS advisory_intents_open_idx
    ON advisory_intents (intent_status)
    WHERE intent_status IN ('open', 'partial');
CREATE INDEX IF NOT EXISTS advisory_intents_token_idx
    ON advisory_intents (token_id, created_at DESC);
CREATE INDEX IF NOT EXISTS advisory_intents_order_idx
    ON advisory_intents (polymarket_order_id)
    WHERE polymarket_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS advisory_intents_kind_created_idx
    ON advisory_intents (kind, created_at DESC);
""".format(
    kind_check=_enum_check("kind", INTENT_KINDS),
    submission_check=_enum_check("submission_status", INTENT_SUBMISSION_STATUSES),
    status_check=_enum_check("intent_status", INTENT_STATUSES),
)

# advisory_intents 一致性 trigger:
# - place_buy / place_sell 必须有 token_id + intended_side + intended_price + intended_size_shares
# - cancel 必须有 cancel_target_order_id 或 cancel_target_intent_id 至少一个
# - intended_side 必须与 kind 后缀一致 (place_buy → buy, place_sell → sell)
_DDL_ADVISORY_INTENTS_TRIGGER = """
CREATE OR REPLACE FUNCTION advisory_intents_validate()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.kind IN ('place_buy', 'place_sell') THEN
        IF NEW.token_id IS NULL OR NEW.intended_side IS NULL
           OR NEW.intended_price IS NULL OR NEW.intended_size_shares IS NULL THEN
            RAISE EXCEPTION
                'advisory_intents: place_* requires token_id + intended_side + intended_price + intended_size_shares';
        END IF;
        IF (NEW.kind = 'place_buy' AND NEW.intended_side <> 'buy')
           OR (NEW.kind = 'place_sell' AND NEW.intended_side <> 'sell') THEN
            RAISE EXCEPTION
                'advisory_intents: kind=% inconsistent with intended_side=%',
                NEW.kind, NEW.intended_side;
        END IF;
    ELSIF NEW.kind = 'cancel' THEN
        IF NEW.cancel_target_order_id IS NULL AND NEW.cancel_target_intent_id IS NULL THEN
            RAISE EXCEPTION
                'advisory_intents: cancel requires cancel_target_order_id or cancel_target_intent_id';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS advisory_intents_validate_trg ON advisory_intents;
CREATE TRIGGER advisory_intents_validate_trg
BEFORE INSERT OR UPDATE ON advisory_intents
FOR EACH ROW EXECUTE FUNCTION advisory_intents_validate();
"""

CHAIN_FILL_PROFILES = ("analyze", "trade")

_DDL_ADVISORY_CHAIN_FILLS = """
CREATE TABLE IF NOT EXISTS advisory_chain_fills (
    id                  BIGSERIAL PRIMARY KEY,
    tx_hash             TEXT NOT NULL,
    log_index           INTEGER NOT NULL DEFAULT 0,
    fill_timestamp      TIMESTAMPTZ NOT NULL,
    token_id            TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('buy','sell')),
    price               DOUBLE PRECISION NOT NULL CHECK (price > 0 AND price < 1),
    size_shares         DOUBLE PRECISION NOT NULL CHECK (size_shares > 0),
    size_usdc           DOUBLE PRECISION NOT NULL CHECK (size_usdc > 0),
    wallet_address      TEXT NOT NULL,
    profile             TEXT NOT NULL {profile_check},
    market_slug         TEXT,
    event_slug          TEXT,
    raw_json            JSONB NOT NULL,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (tx_hash, log_index, token_id)
);
CREATE INDEX IF NOT EXISTS advisory_chain_fills_ts_idx
    ON advisory_chain_fills (fill_timestamp DESC);
CREATE INDEX IF NOT EXISTS advisory_chain_fills_token_ts_idx
    ON advisory_chain_fills (token_id, fill_timestamp DESC);
CREATE INDEX IF NOT EXISTS advisory_chain_fills_wallet_ts_idx
    ON advisory_chain_fills (wallet_address, fill_timestamp DESC);
CREATE INDEX IF NOT EXISTS advisory_chain_fills_profile_ts_idx
    ON advisory_chain_fills (profile, fill_timestamp DESC);
""".format(profile_check=_enum_check("profile", CHAIN_FILL_PROFILES))

_DDL_ADVISORY_CHAIN_FILLS_POLLER_STATE = """
CREATE TABLE IF NOT EXISTS advisory_chain_fills_poller_state (
    profile             TEXT PRIMARY KEY {profile_check},
    last_success_at     TIMESTAMPTZ,
    last_window_end_ts  BIGINT,
    last_error          TEXT,
    last_error_at       TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
""".format(profile_check=_enum_check("profile", CHAIN_FILL_PROFILES))


_DDL_STATEMENTS: tuple[tuple[str, str], ...] = (
    ("path_observation_snapshots", _DDL_PATH_OBSERVATION_SNAPSHOTS),
    ("settlement_feed_versions", _DDL_SETTLEMENT_FEED_VERSIONS),
    ("settlement_feed_records", _DDL_SETTLEMENT_FEED_RECORDS),
    ("input_quote_snapshots", _DDL_INPUT_QUOTE_SNAPSHOTS),
    ("path_views", _DDL_PATH_VIEWS),
    ("market_view_batches", _DDL_MARKET_VIEW_BATCHES),
    ("market_view_snapshots", _DDL_MARKET_VIEW_SNAPSHOTS),
    ("market_view_latest", _DDL_MARKET_VIEW_LATEST),
    ("advisory_user_theses", _DDL_USER_THESES),
    ("advisory_calibration_runs", _DDL_CALIBRATION_RUNS),
    ("advisory_intents", _DDL_ADVISORY_INTENTS),
    ("advisory_intents_trigger", _DDL_ADVISORY_INTENTS_TRIGGER),
    ("advisory_chain_fills", _DDL_ADVISORY_CHAIN_FILLS),
    ("advisory_chain_fills_poller_state", _DDL_ADVISORY_CHAIN_FILLS_POLLER_STATE),
)


def init_advisory_schema() -> None:
    """创建 advisory plan v1.3 §4.1 中全部 7 张表 + 一致性触发器。幂等。"""
    with get_conn(autocommit=True) as conn:
        cur = conn.cursor()
        for name, ddl in _DDL_STATEMENTS:
            logger.info("advisory schema: applying %s", name)
            cur.execute(ddl)
    logger.info("advisory schema: 7 张表 + trigger 已就绪")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_advisory_schema()
