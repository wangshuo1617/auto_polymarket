"""MarketStateAdapter — settlement merge + path_observation_snapshots writer.

实现 plan-advisory.md §1.5.5 状态机 (advisory v1.3) + §4.2 step 0b:

输入:
- token_universe: list[TokenContext] — 每个 token 的 condition_id / market_slug / strike 等元数据
- btc_tick_state: 调用方提供的最近 BTC tick (source/version/latest_ts)
- settlement_version + missing_condition_ids: 来自上一步 PolymarketSettlementAdapter

输出:
- per_token_state: dict[token_id, MergedState]  — 包含 resolution_state / halt_reason /
  fair_value_status / settlement_state / winning_token_id / final_price 等字段, 供 L2 Computer
  消费写入 market_view_snapshots。
- path_observation_snapshot_id: 对应 path_observation_snapshots 行 id。
- effect_hash: settlement_refresh_effect_hash (透传给 batch.inputs_hash)。

状态机核心 (与 plan-advisory.md §1.5.5 一致):
  - condition 在 settlement_feed_records 缺失 → resolution_state='unknown',
    halt_reason='settlement_baseline_missing', fair_value_status='unavailable'
  - settlement_state == 'disputed' → 保持原 resolution_state, 强制
    halt_reason='settlement_disputed', fair_value_status='unavailable'
  - settlement_state == 'settled' → resolution_state='settled',
    fair_value_status='settled' (winning side) / 否则失效
  - 否则 → resolution_state='open', halt_reason=None, fair_value_status 由下游 (A2.5/A3) 决定

"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from data.database import get_conn

from .settlement_adapter import latest_records_by_condition

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TokenContext:
    """L2/Computer 上游需要的最小 token 元数据."""

    token_id: str
    condition_id: str
    market_slug: str
    outcome_index: int  # 0 or 1, gamma outcomes 中的位置
    # path observation 输入: 暂留 strike + side 占位, 由 PathView (A2.5) 完整使用
    strike_usd: Optional[float] = None
    side_above: Optional[bool] = None  # True = "Bitcoin reach $X" 类 yes-token


@dataclass
class MergedState:
    token_id: str
    condition_id: str
    market_slug: str
    resolution_state: str
    halt_reason: Optional[str]
    fair_value_status: Optional[str]  # 仅在 settled / 强制 unavailable 时定值; 否则 None 留 L2 决定
    settlement_state: Optional[str]
    winning_token_id: Optional[str]
    final_price: Optional[float]


@dataclass
class BtcTickState:
    source: str
    version: str
    latest_tick_ts_utc: datetime


@dataclass
class SnapshotResult:
    path_observation_snapshot_id: int
    settlement_feed_version: Optional[int]
    settlement_refresh_effect_hash: str
    per_token_state: dict[str, MergedState]
    missing_set: set[str]
    inputs_hash: str


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _merge_one(
    token: TokenContext,
    settlement_record: Optional[dict],
    *,
    in_missing_set: bool,
) -> MergedState:
    base = MergedState(
        token_id=token.token_id,
        condition_id=token.condition_id,
        market_slug=token.market_slug,
        resolution_state="open",
        halt_reason=None,
        fair_value_status=None,
        settlement_state=None,
        winning_token_id=None,
        final_price=None,
    )

    if in_missing_set or settlement_record is None:
        # coverage gap: degraded complete batch (plan v1.1)
        return MergedState(
            **{
                **asdict(base),
                "resolution_state": "unknown",
                "halt_reason": "settlement_baseline_missing",
                "fair_value_status": "unavailable",
            }
        )

    state = settlement_record["settlement_state"]
    base.settlement_state = state
    base.winning_token_id = settlement_record.get("winning_token_id")
    base.final_price = settlement_record.get("final_price")

    if state == "disputed":
        return MergedState(
            **{
                **asdict(base),
                "halt_reason": "settlement_disputed",
                "fair_value_status": "unavailable",
            }
        )

    if state == "settled":
        # 胜负双方都标 fair_value_status='settled'; 调用方根据 winning_token_id 区分
        # 是否为胜方 (胜方 final price=1, 败方=0)。无 halt_reason — 已结算市场不再交易,
        # 由 resolution_state='settled' 兜底。
        return MergedState(
            **{
                **asdict(base),
                "resolution_state": "settled",
                "halt_reason": None,
                "fair_value_status": "settled",
            }
        )

    # state == 'pending' (含 carry-forward 后仍未变化)
    return base


def _compute_inputs_hash(
    btc_tick: BtcTickState,
    settlement_effect_hash: str,
    per_token: dict[str, MergedState],
) -> str:
    payload = {
        "btc_source": btc_tick.source,
        "btc_version": btc_tick.version,
        "btc_latest": btc_tick.latest_tick_ts_utc.isoformat(),
        "settlement_effect": settlement_effect_hash,
        "per_token": sorted(
            (
                tid,
                s.resolution_state,
                s.halt_reason or "",
                s.settlement_state or "",
                s.winning_token_id or "",
            )
            for tid, s in per_token.items()
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def snapshot(
    token_universe: Iterable[TokenContext],
    btc_tick: BtcTickState,
    settlement_feed_version: Optional[int],
    settlement_missing_set: Iterable[str],
    settlement_effect_hash: str,
    *,
    as_of_utc: Optional[datetime] = None,
) -> SnapshotResult:
    """主入口: 状态机 merge + 写 path_observation_snapshots 行.

    bootstrap 守护: 调用方应在 settlement_adapter.is_bootstrap_pending() == True 时
    直接 mark batch failed, 不进入本函数。本函数假定上游已写好 settlement_feed_versions。
    """
    universe = list(token_universe)
    missing_set = set(settlement_missing_set)
    latest_records = latest_records_by_condition()

    per_token: dict[str, MergedState] = {}
    for tok in universe:
        record = latest_records.get(tok.condition_id)
        in_missing = tok.condition_id in missing_set
        per_token[tok.token_id] = _merge_one(tok, record, in_missing_set=in_missing)

    as_of = as_of_utc or _utc_now()
    inputs_hash = _compute_inputs_hash(btc_tick, settlement_effect_hash, per_token)
    per_token_observations = {
        tid: {
            "condition_id": s.condition_id,
            "market_slug": s.market_slug,
            "resolution_state": s.resolution_state,
            "halt_reason": s.halt_reason,
            "fair_value_status": s.fair_value_status,
            "settlement_state": s.settlement_state,
            "winning_token_id": s.winning_token_id,
            "final_price": s.final_price,
        }
        for tid, s in per_token.items()
    }

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO path_observation_snapshots
              (as_of_utc, btc_tick_feed_source, btc_tick_feed_version, latest_tick_ts_utc,
               settlement_feed_version, settlement_refresh_effect_hash,
               per_token_observations, inputs_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id
            """,
            (
                as_of,
                btc_tick.source,
                btc_tick.version,
                btc_tick.latest_tick_ts_utc,
                settlement_feed_version,
                settlement_effect_hash,
                json.dumps(per_token_observations),
                inputs_hash,
            ),
        )
        snapshot_id = cur.fetchone()[0]
        conn.commit()

    logger.info(
        "market_state_adapter: snapshot_id=%s tokens=%d missing=%d settlement_version=%s",
        snapshot_id,
        len(universe),
        len(missing_set),
        settlement_feed_version,
    )
    return SnapshotResult(
        path_observation_snapshot_id=snapshot_id,
        settlement_feed_version=settlement_feed_version,
        settlement_refresh_effect_hash=settlement_effect_hash,
        per_token_state=per_token,
        missing_set=missing_set,
        inputs_hash=inputs_hash,
    )
