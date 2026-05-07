"""L2 Computer + market_view_snapshots writer + 4-step batch orchestrator.

实现 plan-advisory.md §1.6 MarketView 字段计算 + §4.2 step 0/0a/0b/1/2/3/4 写入流程.

L2 输入 (per-token):
- merged_state: A2 输出的 MergedState (resolution_state / halt_reason / settlement_state / ...)
- path_view_fair: A2.5 PathView 中本 token 的 TokenFair (含 fair_calibrated / p_touch_to_date)
- quote: {best_bid, best_ask}
- position: 当前持仓 USDC (可 None)
- target: 目标持仓 USDC (可 None — 由调用方风险配置决定, advisory MVP 不强制)

L2 输出: MarketView (即 market_view_snapshots row 内容):
- fair_value_for_edge: halt 时为 None, 其他用 path_view.fair_calibrated
- edge_buy_active: fair - best_ask (正值=可买入有利可图; halt/无报价时 None)
- expected_apr_by_intent: 简化版 = edge_buy_active / best_ask * 365 / days_left (NULL 时跳过)
- ranking_score: 简化为 expected_apr_by_intent 或 edge_buy_active (halt 行强制 None, dashboard 不参与排序)
- target/current/delta_usdc: 透传

dashboard 展示 (plan §3) 由前端按 halt_reason / resolution_state 分组渲染, 后端不预选。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from data.database import get_conn

from .market_state_adapter import MergedState, BtcTickState, TokenContext, snapshot as state_snapshot
from .path_view import PathView, TokenFair, compute_and_persist_path_view
from .settlement_adapter import RefreshResult, refresh_settlement_feed, ConditionDescriptor

logger = logging.getLogger(__name__)


@dataclass
class TokenQuote:
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None


@dataclass
class TokenPosition:
    """调用方提供的当前/目标持仓 USDC. 任何字段可 None."""
    current_usdc: Optional[float] = None
    target_usdc: Optional[float] = None


@dataclass
class MarketView:
    """对应 market_view_snapshots 一行 + view_payload JSONB."""
    token_id: str
    market_slug: str
    condition_id: str
    resolution_state: str
    halt_reason: Optional[str]
    fair_value_status: str
    settlement_state: Optional[str]
    fair_value_for_edge: Optional[float]
    edge_buy_active: Optional[float]
    expected_apr_by_intent: Optional[float]
    ranking_score: Optional[float]
    target_position_usdc: Optional[float]
    current_position_usdc: Optional[float]
    delta_usdc: Optional[float]
    view_payload: dict
    inputs_hash: str


def _compute_one_view(
    token: TokenContext,
    state: MergedState,
    fair: Optional[TokenFair],
    quote: TokenQuote,
    position: TokenPosition,
    days_left: float,
    inputs_hash: str,
    total_net_value_usdc: Optional[float] = None,
) -> MarketView:
    halt = state.halt_reason
    fvs = state.fair_value_status
    fair_value: Optional[float] = None
    edge_buy: Optional[float] = None
    apr: Optional[float] = None
    ranking: Optional[float] = None

    # settled token: fair = 1 if winner, 0 if loser
    if state.resolution_state == "settled":
        if state.winning_token_id == token.token_id:
            fair_value = 1.0
        else:
            fair_value = 0.0
        if fvs is None:
            fvs = "settled"
    elif halt is not None:
        # halted: 强制 unavailable, 不算 edge / apr / ranking
        if fvs is None:
            fvs = "unavailable"
    else:
        # open + 未 halt: 用 PathView fair
        if fair is not None and fair.fair_calibrated is not None:
            fair_value = float(fair.fair_calibrated)
            if fvs is None:
                fvs = "available"
        else:
            if fvs is None:
                fvs = "placeholder"

    # edge / apr — 仅在 fair_value 可用 + 有 ask 报价 + 未 halt 时计算
    # fair_value=1.0 (path-touched) 仍允许: ask<1 时 buy 是接近无风险套利
    if (
        halt is None
        and fair_value is not None
        and 0.0 < fair_value <= 1.0
        and quote.best_ask is not None
        and 0.0 < quote.best_ask < 1.0
    ):
        edge_buy = fair_value - quote.best_ask
        if edge_buy > 0 and days_left > 0:
            # expected_apr = (fair / ask - 1) annualized
            apr = (fair_value / quote.best_ask - 1.0) * 365.0 / days_left
        ranking = edge_buy

    # Kelly target (quarter-Kelly + 20% net-value cap).
    # 仅当 fair / ask 都可用 + 未 halt + edge > 0 + total_net_value 可知时计算.
    # 不会写到 position.target_usdc (避免污染 input dataclass), 直接进入 payload.
    target_usdc: Optional[float] = position.target_usdc
    if (
        target_usdc is None
        and total_net_value_usdc is not None and total_net_value_usdc > 0
        and fair_value is not None and 0.0 < fair_value < 1.0
        and quote.best_ask is not None and 0.0 < quote.best_ask < 1.0
        and edge_buy is not None and edge_buy > 0
    ):
        # Kelly = (p − price) / (1 − price); quarter-Kelly + 20% cap
        kelly = max(0.0, (fair_value - quote.best_ask) / max(1e-6, 1.0 - quote.best_ask))
        fractional = 0.25 * kelly
        target_usdc = round(min(total_net_value_usdc * 0.20,
                                total_net_value_usdc * fractional), 2)

    # 减仓 / 清仓: 持有 + 按 bid 重算 edge_bid<0 (卖出价已低于公允) → target=0 全清.
    # 仅 ask-edge 翻负但 bid-edge 仍 >=0 时只是不该加仓, 不强制平.
    # 锁利: 持有 + bid_edge >= +10% (市场超付) → target=0 全清拿大头.
    if (
        position.current_usdc is not None and position.current_usdc > 0
        and fair_value is not None and 0.0 < fair_value < 1.0
        and quote.best_bid is not None and quote.best_bid > 0
        and halt is None
    ):
        edge_bid = (quote.best_bid - fair_value) / fair_value
        if edge_bid < 0 or edge_bid >= 0.10:
            target_usdc = 0.0

    delta_usdc = None
    if target_usdc is not None and position.current_usdc is not None:
        delta_usdc = target_usdc - position.current_usdc

    payload = {
        "token_id": token.token_id,
        "market_slug": state.market_slug,
        "condition_id": state.condition_id,
        "outcome_index": token.outcome_index,
        "strike_usd": token.strike_usd,
        "side_above": token.side_above,
        "resolution_state": state.resolution_state,
        "halt_reason": halt,
        "fair_value_status": fvs,
        "settlement_state": state.settlement_state,
        "winning_token_id": state.winning_token_id,
        "fair_value_for_edge": fair_value,
        "best_bid": quote.best_bid,
        "best_ask": quote.best_ask,
        "edge_buy_active": edge_buy,
        "expected_apr_by_intent": apr,
        "ranking_score": ranking,
        "target_position_usdc": target_usdc,
        "current_position_usdc": position.current_usdc,
        "delta_usdc": delta_usdc,
        "path_view_fair_raw": fair.fair_raw if fair else None,
        "path_view_p_touch_to_date": fair.p_touch_to_date if fair else None,
        "path_view_distance_pct": fair.distance_pct if fair else None,
    }

    return MarketView(
        token_id=token.token_id,
        market_slug=state.market_slug,
        condition_id=state.condition_id,
        resolution_state=state.resolution_state,
        halt_reason=halt,
        fair_value_status=fvs,
        settlement_state=state.settlement_state,
        fair_value_for_edge=fair_value,
        edge_buy_active=edge_buy,
        expected_apr_by_intent=apr,
        ranking_score=ranking,
        target_position_usdc=target_usdc,
        current_position_usdc=position.current_usdc,
        delta_usdc=delta_usdc,
        view_payload=payload,
        inputs_hash=inputs_hash,
    )


def _canonical_inputs_hash(
    *, path_view_id: int, input_quote_snapshot_id: Optional[int],
    path_observation_inputs_hash: str, settlement_feed_version: Optional[int],
    settlement_refresh_effect_hash: str, risk_config_version: str,
    user_thesis_id: Optional[int] = None,
) -> str:
    payload = {
        "path_view_id": path_view_id,
        "input_quote_snapshot_id": input_quote_snapshot_id,
        "path_observation_inputs_hash": path_observation_inputs_hash,
        "settlement_feed_version": settlement_feed_version,
        "settlement_refresh_effect_hash": settlement_refresh_effect_hash,
        "risk_config_version": risk_config_version,
        "user_thesis_id": user_thesis_id,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------
#  Writers (step 2 + step 3)
# ---------------------------------------------------------------------------

def _write_snapshots(
    cur, batch_id: int, path_view_id: int, path_observation_snapshot_id: int,
    views: list[MarketView],
) -> dict[str, int]:
    """step 2: 批量插入 market_view_snapshots, 返回 {token_id: snapshot_id}."""
    out: dict[str, int] = {}
    for v in views:
        cur.execute(
            """
            INSERT INTO market_view_snapshots
              (batch_id, token_id, path_view_id, path_observation_snapshot_id,
               resolution_state, halt_reason, fair_value_status, settlement_state,
               market_slug, condition_id,
               fair_value_for_edge, edge_buy_active, expected_apr_by_intent,
               ranking_score, target_position_usdc, current_position_usdc, delta_usdc,
               view_payload, inputs_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id
            """,
            (
                batch_id, v.token_id, path_view_id, path_observation_snapshot_id,
                v.resolution_state, v.halt_reason, v.fair_value_status, v.settlement_state,
                v.market_slug, v.condition_id,
                v.fair_value_for_edge, v.edge_buy_active, v.expected_apr_by_intent,
                v.ranking_score, v.target_position_usdc, v.current_position_usdc, v.delta_usdc,
                json.dumps(v.view_payload), v.inputs_hash,
            ),
        )
        out[v.token_id] = cur.fetchone()[0]
    return out


def _upsert_latest(cur, batch_id: int, batch_sequence: int, snapshot_ids: dict[str, int]) -> int:
    """step 3: atomic upsert market_view_latest with WHERE excluded.batch_sequence > ...

    旧 batch 永不覆盖新 batch (单 PG 语句保证原子性, 无 race condition).
    返回实际更新行数 (insert 或 update 满足 WHERE 条件的行).
    """
    if not snapshot_ids:
        return 0
    rows = [(tid, batch_id, batch_sequence, sid) for tid, sid in snapshot_ids.items()]
    cur.executemany(
        """
        INSERT INTO market_view_latest (token_id, batch_id, batch_sequence, snapshot_id)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (token_id) DO UPDATE
          SET batch_id = EXCLUDED.batch_id,
              batch_sequence = EXCLUDED.batch_sequence,
              snapshot_id = EXCLUDED.snapshot_id,
              updated_at = NOW()
          WHERE EXCLUDED.batch_sequence > market_view_latest.batch_sequence
        """,
        rows,
    )
    return len(rows)


# ---------------------------------------------------------------------------
#  4-step batch orchestrator
# ---------------------------------------------------------------------------

@dataclass
class BatchResult:
    batch_id: int
    batch_sequence: int
    status: str
    failure_step: Optional[str]
    failure_error: Optional[str]
    settlement_feed_version: Optional[int]
    path_observation_snapshot_id: Optional[int]
    path_view_id: Optional[int]
    snapshot_ids: dict[str, int] = field(default_factory=dict)
    inputs_hash: Optional[str] = None
    refresh_status: Optional[str] = None
    missing_condition_ids: list[str] = field(default_factory=list)


def run_advisory_batch(
    *,
    token_universe: Iterable[TokenContext],
    descriptors: Iterable[ConditionDescriptor],
    btc_tick: BtcTickState,
    current_btc_price: float,
    path_max_btc: float,
    path_min_btc: float,
    sigma_daily: float,
    sigma_source: str,
    sigma_is_iv: bool,
    days_left: float,
    quotes: dict[str, TokenQuote],
    positions: dict[str, TokenPosition],
    total_net_value_usdc: Optional[float] = None,
    risk_config_version: str = "advisory-mvp-v1",
    drift_daily: float = 0.0,
    input_quote_snapshot_id: Optional[int] = None,
    as_of_utc: Optional[datetime] = None,
    user_thesis_id: Optional[int] = None,
) -> BatchResult:
    """完整 4-step batch (plan-advisory §4.2):
      step 0:  create batch row status=started
      step 0a: refresh settlement feed (carry-forward + bootstrap + coverage 守护)
      step 0b: market state snapshot (path_observation_snapshots 写入 + state machine)
      step 0c: PathView L1 fair (path_views 写入)
      step 1:  update batch with refs + inputs_hash
      step 2:  write market_view_snapshots (one per token)
      step 3:  atomic upsert market_view_latest
      step 4:  mark batch complete (atomic SET status='complete', batch_completed_at=NOW())

    coverage gap 退化路径 (plan v1.1): refresh_status='partial'/'failed' + 全 universe halt
    时仍走完 step 0b/1/2/3/4 写出 degraded complete batch (而非 mark batch failed),
    让 dashboard 看到最新 halt_reason 隐藏推荐, 不卡上一个 complete batch 的旧 fair.

    硬失败 (mark status=failed) 仅当 step 0/0a/0b/0c/1/2/3/4 自身崩溃 (e.g. PG 断连).
    """
    universe = list(token_universe)
    desc_list = list(descriptors)
    as_of = as_of_utc or datetime.now(timezone.utc)

    # step 0
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO market_view_batches (as_of_utc, status, token_count)
            VALUES (%s, 'started', %s)
            RETURNING id, batch_sequence
            """,
            (as_of, len(universe)),
        )
        batch_id, batch_sequence = cur.fetchone()
        conn.commit()

    failure_step: Optional[str] = None
    failure_error: Optional[str] = None
    refresh_result: Optional[RefreshResult] = None
    snap_result = None
    path_view: Optional[PathView] = None

    try:
        # step 0a
        refresh_result = refresh_settlement_feed(desc_list)

        # step 0b
        snap_result = state_snapshot(
            universe, btc_tick,
            settlement_feed_version=refresh_result.settlement_feed_version,
            settlement_missing_set=refresh_result.missing_condition_ids,
            settlement_effect_hash=refresh_result.effect_hash,
            as_of_utc=as_of,
        )

        # step 0c (advisory: PathView)
        path_view = compute_and_persist_path_view(
            universe,
            path_observation_snapshot_id=snap_result.path_observation_snapshot_id,
            current_btc_price=current_btc_price,
            path_max_btc=path_max_btc, path_min_btc=path_min_btc,
            sigma_daily=sigma_daily, sigma_source=sigma_source, sigma_is_iv=sigma_is_iv,
            days_left=days_left, drift_daily=drift_daily,
            input_quote_snapshot_id=input_quote_snapshot_id,
            as_of_utc=as_of,
        )

        # step 1: update batch refs + inputs_hash
        inputs_hash = _canonical_inputs_hash(
            path_view_id=path_view.path_view_id,
            input_quote_snapshot_id=input_quote_snapshot_id,
            path_observation_inputs_hash=snap_result.inputs_hash,
            settlement_feed_version=refresh_result.settlement_feed_version,
            settlement_refresh_effect_hash=refresh_result.effect_hash,
            risk_config_version=risk_config_version,
            user_thesis_id=user_thesis_id,
        )

        # step 2: build views per token
        views: list[MarketView] = []
        for tok in universe:
            state = snap_result.per_token_state[tok.token_id]
            fair = path_view.per_token_fair.get(tok.token_id)
            quote = quotes.get(tok.token_id, TokenQuote())
            pos = positions.get(tok.token_id, TokenPosition())
            views.append(_compute_one_view(tok, state, fair, quote, pos, days_left, inputs_hash,
                                           total_net_value_usdc=total_net_value_usdc))

        # step 1+2+3+4 in single transaction
        with get_conn() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    UPDATE market_view_batches SET
                      path_view_id = %s,
                      input_quote_snapshot_id = %s,
                      path_observation_snapshot_id = %s,
                      settlement_feed_version = %s,
                      settlement_refresh_state = %s::jsonb,
                      settlement_refresh_effect_hash = %s,
                      inputs_hash = %s
                    WHERE id = %s
                    """,
                    (
                        path_view.path_view_id,
                        input_quote_snapshot_id,
                        snap_result.path_observation_snapshot_id,
                        refresh_result.settlement_feed_version,
                        json.dumps({
                            "refresh_status": refresh_result.refresh_status,
                            "refreshed": refresh_result.refreshed_condition_ids,
                            "missing": refresh_result.missing_condition_ids,
                        }),
                        refresh_result.effect_hash,
                        inputs_hash,
                        batch_id,
                    ),
                )
                snapshot_ids = _write_snapshots(
                    cur, batch_id, path_view.path_view_id,
                    snap_result.path_observation_snapshot_id, views,
                )
                _upsert_latest(cur, batch_id, batch_sequence, snapshot_ids)
                cur.execute(
                    """
                    UPDATE market_view_batches
                       SET status='complete', batch_completed_at = NOW()
                     WHERE id = %s AND status = 'started'
                    """,
                    (batch_id,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        logger.info(
            "advisory batch: id=%s seq=%s tokens=%d refresh=%s missing=%d inputs_hash=%s",
            batch_id, batch_sequence, len(views), refresh_result.refresh_status,
            len(refresh_result.missing_condition_ids), inputs_hash[:12],
        )

        return BatchResult(
            batch_id=batch_id, batch_sequence=batch_sequence,
            status="complete", failure_step=None, failure_error=None,
            settlement_feed_version=refresh_result.settlement_feed_version,
            path_observation_snapshot_id=snap_result.path_observation_snapshot_id,
            path_view_id=path_view.path_view_id, snapshot_ids=snapshot_ids,
            inputs_hash=inputs_hash, refresh_status=refresh_result.refresh_status,
            missing_condition_ids=list(refresh_result.missing_condition_ids),
        )

    except Exception as exc:
        # 硬失败 — 标 batch failed + failure_step + failure_error
        if refresh_result is None:
            failure_step = "0a_settlement_refresh"
        elif snap_result is None:
            failure_step = "0b_market_state_snapshot"
        elif path_view is None:
            failure_step = "0c_path_view"
        else:
            failure_step = "1-4_batch_finalize"
        failure_error = f"{type(exc).__name__}: {exc}"
        logger.exception("advisory batch failed at %s: %s", failure_step, failure_error)
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE market_view_batches
                   SET status='failed', failure_step=%s, failure_error=%s
                 WHERE id=%s
                """,
                (failure_step, failure_error, batch_id),
            )
            conn.commit()
        return BatchResult(
            batch_id=batch_id, batch_sequence=batch_sequence,
            status="failed", failure_step=failure_step, failure_error=failure_error,
            settlement_feed_version=(refresh_result.settlement_feed_version if refresh_result else None),
            path_observation_snapshot_id=(snap_result.path_observation_snapshot_id if snap_result else None),
            path_view_id=(path_view.path_view_id if path_view else None),
            refresh_status=(refresh_result.refresh_status if refresh_result else None),
            missing_condition_ids=(list(refresh_result.missing_condition_ids) if refresh_result else []),
        )


def get_latest_complete_batch_views() -> tuple[Optional[dict], list[dict]]:
    """plan-advisory §5.1: dashboard 数据源.

    取最新 status='complete' batch + 该 batch 的全部 snapshots.
    *不*走 market_view_latest 直查 — universe 收缩后旧 token 残留会绕过 staleness 检查.
    """
    sql_batch = """
        SELECT id, batch_sequence, batch_completed_at, as_of_utc, settlement_feed_version,
               settlement_refresh_state, inputs_hash, token_count
          FROM market_view_batches
         WHERE status='complete'
         ORDER BY batch_sequence DESC
         LIMIT 1
    """
    sql_snaps = """
        SELECT id, token_id, market_slug, condition_id,
               resolution_state, halt_reason, fair_value_status, settlement_state,
               fair_value_for_edge, edge_buy_active, expected_apr_by_intent,
               ranking_score, target_position_usdc, current_position_usdc, delta_usdc,
               view_payload
          FROM market_view_snapshots
         WHERE batch_id = %s
         ORDER BY ranking_score DESC NULLS LAST
    """
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql_batch)
        row = cur.fetchone()
        if not row:
            return None, []
        cols = [d[0] for d in cur.description]
        batch = dict(zip(cols, row))
        cur.execute(sql_snaps, (batch["id"],))
        scols = [d[0] for d in cur.description]
        snaps = [dict(zip(scols, r)) for r in cur.fetchall()]
    return batch, snaps
