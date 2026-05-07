"""PathViewProvider — L1 fair value computation (GBM + path-to-date + wick).

实现 plan-advisory.md §A2.5: 沿用 services/profit_optimizer.py 的现成 GBM barrier-touch
+ 校准修正 (avoid 重新发明轮子)。

PathView 表示当前 batch 内, 每个 token 的"独立 fair value 估计"。后续 L2 Computer (A3)
会根据 settlement merge / halt_reason 决定是否真正写入 market_view_snapshots.fair_value_for_edge。

核心步骤 (plan §1.5.5 step 0/1/2/3):
- Step 0 (path-to-date 短路): 若 token 是 "above strike" 且 path_max_btc >= strike → fair=1
                                若 token 是 "below strike" 且 path_min_btc <= strike → fair=1
                                (Polymarket 月度市场任意时刻触及即算 Yes)
- Step 1 (GBM barrier): 调用 _barrier_touch_prob() 算 P(剩余时间内首次触及)
- Step 2 (path scenario): 沿用 profit_optimizer 已有逻辑, 但本 advisory MVP 暂只用 step 1 +
  step 3, 不做 scenario-weighted 平均 (留给 A3 risk_config)
- Step 3 (wick adjustment): 配置 wick_buffer_pct, 在 strike 上下加/减一个 buffer 后再算
  barrier — 避免被 1 秒 wick 行情打穿后又回归而错估 fair (advisory MVP 默认 0)
- 最终 fair_calibrated = _calibrate_p_yes(fair_raw, distance_pct)

输入:
- token_universe: list[TokenContext] — A2 已定义, 含 strike_usd / side_above
- current_btc_price + path_max + path_min: 来自 BTC tick reader (调用方提供)
- sigma_daily / sigma_is_iv: 上游 volatility profile (Deribit IV / realized / ATR fallback)
- days_left: 距月末剩余天数
- input_quote_snapshot_id: 关联的 quote 快照 (可 None)

输出:
- PathView 对象 + 已 persist 的 path_view_id
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional

from data.database import get_conn
from services.profit_optimizer import _barrier_touch_prob, _calibrate_p_yes

from .market_state_adapter import TokenContext

logger = logging.getLogger(__name__)

# wick buffer 默认关闭 (advisory MVP) — 上调到 0.001~0.005 可对 1s wick 做风险扣减
DEFAULT_WICK_BUFFER_PCT = 0.0


@dataclass
class TokenFair:
    token_id: str
    strike_usd: Optional[float]
    side_above: Optional[bool]
    distance_pct: Optional[float]
    p_touch_to_date: bool
    fair_raw: Optional[float]
    fair_wick_adjusted: Optional[float]
    fair_calibrated: Optional[float]
    note: str = ""


@dataclass
class PathView:
    path_view_id: int
    path_observation_snapshot_id: int
    input_quote_snapshot_id: Optional[int]
    current_btc_price: float
    sigma_daily: float
    sigma_source: str
    sigma_is_iv: bool
    drift_daily: float
    days_left: float
    per_token_fair: dict[str, TokenFair]
    inputs_hash: str


def _compute_one(
    token: TokenContext,
    current_price: float,
    path_max: float,
    path_min: float,
    drift_daily: float,
    sigma_daily: float,
    sigma_is_iv: bool,
    days_left: float,
    wick_buffer_pct: float,
) -> TokenFair:
    base = TokenFair(
        token_id=token.token_id,
        strike_usd=token.strike_usd,
        side_above=token.side_above,
        distance_pct=None,
        p_touch_to_date=False,
        fair_raw=None,
        fair_wick_adjusted=None,
        fair_calibrated=None,
    )

    if token.strike_usd is None or token.side_above is None or current_price <= 0:
        base.note = "missing_strike_or_direction_or_price"
        return base

    strike = float(token.strike_usd)
    base.distance_pct = abs(strike - current_price) / current_price * 100.0

    # 关键: yes/no 是同一个 barrier 事件的互补结果.
    # market_above = 该 condition 的 yes 视角下的方向 (i.e. yes token 的 side_above).
    # 当前 token 若是 yes (outcome_index=0) → side_above 即为 market 方向;
    # 若是 no (outcome_index=1) → side_above 已被 inputs.py 反向, 还原为 market 方向需取 not.
    is_no_token = (token.outcome_index == 1)
    market_above = (not token.side_above) if is_no_token else bool(token.side_above)
    direction = "above" if market_above else "below"

    # Step 0: path-to-date 短路 — yes 已触及 barrier, yes=1, no=0
    yes_touched = (
        (direction == "above" and path_max >= strike)
        or (direction == "below" and path_min <= strike)
    )
    if yes_touched:
        base.p_touch_to_date = True
        p_yes = 1.0
        fair = 0.0 if is_no_token else 1.0
        base.fair_raw = fair
        base.fair_wick_adjusted = fair
        base.fair_calibrated = fair
        base.note = "touched_to_date" + ("_no_loser" if is_no_token else "")
        return base

    # Step 1: GBM barrier touch (yes 视角)
    p_yes_raw = _barrier_touch_prob(
        current_price, strike, direction, drift_daily, sigma_daily, days_left,
        sigma_is_iv=sigma_is_iv,
    )
    base.fair_raw = (1.0 - p_yes_raw) if is_no_token else p_yes_raw

    # Step 3: wick adjustment — yes 视角下的保守值
    if wick_buffer_pct > 0:
        if direction == "above":
            adj_strike = strike * (1.0 + wick_buffer_pct)
        else:
            adj_strike = strike * (1.0 - wick_buffer_pct)
        p_yes_wick = _barrier_touch_prob(
            current_price, adj_strike, direction, drift_daily, sigma_daily, days_left,
            sigma_is_iv=sigma_is_iv,
        )
    else:
        p_yes_wick = p_yes_raw
    base.fair_wick_adjusted = (1.0 - p_yes_wick) if is_no_token else p_yes_wick

    # 校准 (calibration bias correction) — calibrator 输入是 p_yes, 然后再翻转
    p_yes_cal = _calibrate_p_yes(p_yes_wick, base.distance_pct)
    base.fair_calibrated = (1.0 - p_yes_cal) if is_no_token else p_yes_cal
    return base


def _compute_inputs_hash(
    path_observation_snapshot_id: int,
    input_quote_snapshot_id: Optional[int],
    current_price: float,
    sigma_daily: float,
    sigma_is_iv: bool,
    drift_daily: float,
    days_left: float,
    wick_buffer_pct: float,
    per_token: dict[str, TokenFair],
) -> str:
    payload = {
        "path_observation_snapshot_id": path_observation_snapshot_id,
        "input_quote_snapshot_id": input_quote_snapshot_id,
        "current_price": round(current_price, 2),
        "sigma_daily": round(sigma_daily, 8),
        "sigma_is_iv": sigma_is_iv,
        "drift_daily": round(drift_daily, 8),
        "days_left": round(days_left, 6),
        "wick_buffer_pct": round(wick_buffer_pct, 6),
        "per_token": sorted(
            (
                tid,
                f.strike_usd,
                f.side_above,
                round(f.fair_calibrated, 8) if f.fair_calibrated is not None else None,
            )
            for tid, f in per_token.items()
        ),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def compute_and_persist_path_view(
    token_universe: Iterable[TokenContext],
    *,
    path_observation_snapshot_id: int,
    current_btc_price: float,
    path_max_btc: float,
    path_min_btc: float,
    sigma_daily: float,
    sigma_source: str,
    sigma_is_iv: bool,
    days_left: float,
    drift_daily: float = 0.0,
    input_quote_snapshot_id: Optional[int] = None,
    wick_buffer_pct: float = DEFAULT_WICK_BUFFER_PCT,
    as_of_utc: Optional[datetime] = None,
) -> PathView:
    """主入口: 计算所有 token 的 L1 fair, 写入 path_views, 返回 PathView."""
    universe = list(token_universe)
    per_token: dict[str, TokenFair] = {}
    for tok in universe:
        per_token[tok.token_id] = _compute_one(
            tok, current_btc_price, path_max_btc, path_min_btc,
            drift_daily, sigma_daily, sigma_is_iv, days_left, wick_buffer_pct,
        )

    as_of = as_of_utc or datetime.now(timezone.utc)
    inputs_hash = _compute_inputs_hash(
        path_observation_snapshot_id, input_quote_snapshot_id,
        current_btc_price, sigma_daily, sigma_is_iv, drift_daily, days_left,
        wick_buffer_pct, per_token,
    )

    per_token_payload = {
        tid: {
            "strike_usd": f.strike_usd,
            "side_above": f.side_above,
            "distance_pct": f.distance_pct,
            "p_touch_to_date": f.p_touch_to_date,
            "fair_raw": f.fair_raw,
            "fair_wick_adjusted": f.fair_wick_adjusted,
            "fair_calibrated": f.fair_calibrated,
            "note": f.note,
        }
        for tid, f in per_token.items()
    }

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO path_views
              (as_of_utc, path_observation_snapshot_id, input_quote_snapshot_id,
               current_btc_price, sigma_daily, sigma_source, sigma_is_iv,
               drift_daily, days_left, per_token_fair, inputs_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            RETURNING id
            """,
            (
                as_of, path_observation_snapshot_id, input_quote_snapshot_id,
                current_btc_price, sigma_daily, sigma_source, sigma_is_iv,
                drift_daily, days_left, json.dumps(per_token_payload), inputs_hash,
            ),
        )
        path_view_id = cur.fetchone()[0]
        conn.commit()

    logger.info(
        "path_view: id=%s tokens=%d touched_to_date=%d sigma=%.5f source=%s",
        path_view_id, len(universe),
        sum(1 for f in per_token.values() if f.p_touch_to_date),
        sigma_daily, sigma_source,
    )
    return PathView(
        path_view_id=path_view_id,
        path_observation_snapshot_id=path_observation_snapshot_id,
        input_quote_snapshot_id=input_quote_snapshot_id,
        current_btc_price=current_btc_price,
        sigma_daily=sigma_daily,
        sigma_source=sigma_source,
        sigma_is_iv=sigma_is_iv,
        drift_daily=drift_daily,
        days_left=days_left,
        per_token_fair=per_token,
        inputs_hash=inputs_hash,
    )
