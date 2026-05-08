"""Phase B2.5 — shadow entry-intent state machine (no production effect).

For each gbm_baseline_replay shadow run, this module:
  1. Pulls per-token edge / target_size from production market_view_snapshots.
  2. Computes correlation buckets (expiry_date × direction) and caps total
     allocated USDC per bucket.
  3. Runs a hysteresis state machine across recent shadow runs:
       waiting → armed → executable → expired/cancelled
       hysteresis: require N consecutive batches above arm threshold to
       advance armed→executable; symmetric drop band to step back.
       cooldown: if last 'cancelled' state for this token was within
       COOLDOWN_MIN minutes, force 'waiting'.
  4. Persists into advisory_pathview_shadow_intents.

NEVER mutates advisory_intents (production state machine for actual orders).

env config:
  ADVISORY_SHADOW_INTENT_ARM_EDGE_PCT (default 3.0) — arm threshold (pp of fair)
  ADVISORY_SHADOW_INTENT_DISARM_EDGE_PCT (default 1.0) — drop-back threshold
  ADVISORY_SHADOW_INTENT_HYSTERESIS_BATCHES (default 3)
  ADVISORY_SHADOW_INTENT_COOLDOWN_MIN (default 30)
  ADVISORY_SHADOW_INTENT_BUCKET_MAX_USDC (default 200) — per correlation bucket
  ADVISORY_SHADOW_INTENT_GLOBAL_MAX_USDC (default 600) — across all buckets
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from data.database import get_conn

logger = logging.getLogger(__name__)


# --- env helpers ---

def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    try:
        return float(raw) if raw is not None and raw.strip() else default
    except ValueError:
        return default


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    try:
        return int(raw) if raw is not None and raw.strip() else default
    except ValueError:
        return default


def _arm_threshold() -> float:
    return _env_float("ADVISORY_SHADOW_INTENT_ARM_EDGE_PCT", 3.0) / 100.0


def _disarm_threshold() -> float:
    return _env_float("ADVISORY_SHADOW_INTENT_DISARM_EDGE_PCT", 1.0) / 100.0


def _hysteresis_batches() -> int:
    return max(1, _env_int("ADVISORY_SHADOW_INTENT_HYSTERESIS_BATCHES", 3))


def _cooldown_min() -> int:
    return max(0, _env_int("ADVISORY_SHADOW_INTENT_COOLDOWN_MIN", 30))


def _bucket_max() -> float:
    return max(0.0, _env_float("ADVISORY_SHADOW_INTENT_BUCKET_MAX_USDC", 200.0))


def _global_max() -> float:
    return max(0.0, _env_float("ADVISORY_SHADOW_INTENT_GLOBAL_MAX_USDC", 600.0))


# --- core records ---

@dataclass
class _TokenSnap:
    token_id: str
    fair_event: Optional[float]
    best_ask: Optional[float]
    best_bid: Optional[float]
    target_position_usdc: Optional[float]
    side_above: Optional[bool]
    expiry_iso: Optional[str]


@dataclass
class _History:
    last_state: Optional[str] = None
    consecutive_above_arm: int = 0
    last_cancelled_at: Optional[datetime] = None


def _fetch_token_snaps(conn, batch_id: int) -> tuple[list[_TokenSnap], Optional[str]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT pv.as_of_utc, pv.days_left
        FROM market_view_batches b
        LEFT JOIN path_views pv ON pv.id = b.path_view_id
        WHERE b.id = %s
        """,
        (batch_id,),
    )
    row = cur.fetchone()
    expiry_iso: Optional[str] = None
    if row and row[0] is not None and row[1] is not None:
        try:
            as_of = row[0]
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=timezone.utc)
            expiry_dt = as_of + timedelta(days=float(row[1]))
            expiry_iso = expiry_dt.isoformat()
        except (TypeError, ValueError):
            expiry_iso = None

    cur.execute(
        """
        SELECT s.token_id, s.view_payload, s.target_position_usdc
        FROM market_view_snapshots s
        WHERE s.batch_id = %s
        """,
        (batch_id,),
    )
    out: list[_TokenSnap] = []
    for tok, vp, tgt in cur.fetchall():
        vp = vp or {}
        out.append(_TokenSnap(
            token_id=tok,
            fair_event=vp.get("fair_value_for_edge"),
            best_ask=vp.get("best_ask"),
            best_bid=vp.get("best_bid"),
            target_position_usdc=tgt,
            side_above=vp.get("side_above"),
            expiry_iso=expiry_iso,
        ))
    return out, expiry_iso


def _fetch_history(conn, token_id: str, lookback_batches: int) -> _History:
    """Get last N shadow_intents rows for token, build hysteresis counter."""
    cur = conn.cursor()
    cur.execute(
        """
        SELECT state, edge_to_fair, generated_at
        FROM advisory_pathview_shadow_intents
        WHERE token_id = %s
        ORDER BY generated_at DESC
        LIMIT %s
        """,
        (token_id, lookback_batches),
    )
    rows = cur.fetchall()
    if not rows:
        return _History()

    last_state = rows[0][0]

    # consecutive_above_arm: count from most recent backward while edge >= arm
    arm = _arm_threshold()
    consec = 0
    for state, edge, _ in rows:
        if edge is not None and edge >= arm and state != "cancelled":
            consec += 1
        else:
            break

    cur.execute(
        """
        SELECT max(generated_at)
        FROM advisory_pathview_shadow_intents
        WHERE token_id = %s AND state = 'cancelled'
        """,
        (token_id,),
    )
    last_cancel = cur.fetchone()[0]

    return _History(
        last_state=last_state,
        consecutive_above_arm=consec,
        last_cancelled_at=last_cancel,
    )


def _correlation_bucket(snap: _TokenSnap) -> str:
    """Group tokens by (expiry_date, direction). expiry_date = YYYY-MM-DD."""
    if snap.side_above is True:
        direction = "above"
    elif snap.side_above is False:
        direction = "below"
    else:
        direction = "unknown"
    expiry = "unknown"
    if snap.expiry_iso:
        try:
            dt = datetime.fromisoformat(str(snap.expiry_iso).replace("Z", "+00:00"))
            expiry = dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            expiry = str(snap.expiry_iso)[:10]
    return f"{expiry}|{direction}"


def _decide_state(
    edge: Optional[float],
    history: _History,
    now: datetime,
) -> tuple[str, str]:
    """Returns (new_state, transition_reason)."""
    arm = _arm_threshold()
    disarm = _disarm_threshold()
    hyst_n = _hysteresis_batches()
    cooldown = timedelta(minutes=_cooldown_min())

    if edge is None:
        return "skipped", "no_edge_signal"

    if (history.last_cancelled_at is not None
            and (now - history.last_cancelled_at) < cooldown):
        return "waiting", f"cooldown_{_cooldown_min()}m_active"

    last = history.last_state

    # promotion path
    if edge >= arm:
        # this batch counted in history.consecutive_above_arm only AFTER persist;
        # current candidate count = consec + 1
        candidate = history.consecutive_above_arm + 1
        if candidate >= hyst_n:
            if last in ("executable",):
                return "executable", "edge_sustained"
            return "executable", f"promoted_after_{candidate}_batches"
        return "armed", f"edge_above_arm_count={candidate}/{hyst_n}"

    # demotion / waiting
    if edge < disarm:
        if last in ("armed", "executable"):
            return "expired", f"edge_dropped_below_{disarm:.4f}"
        return "waiting", "edge_below_disarm"

    # in hysteresis band: hold prior state if it was active, else waiting
    if last in ("armed", "executable"):
        return last, "edge_in_hysteresis_band_hold"
    return "waiting", "edge_in_hysteresis_band_no_prior"


def project_shadow_intents(run_id: int, batch_id: int) -> int:
    """Project shadow entry intents for one shadow run. Returns rows written."""
    now = datetime.now(timezone.utc)
    bucket_cap = _bucket_max()
    global_cap = _global_max()

    with get_conn() as conn:
        snaps, _expiry_iso = _fetch_token_snaps(conn, batch_id)
        if not snaps:
            return 0

        # First pass: compute edge + state per token
        records: list[dict] = []
        for s in snaps:
            edge = None
            if (s.fair_event is not None and s.best_ask is not None
                    and 0.0 < s.best_ask < 1.0):
                edge = float(s.fair_event) - float(s.best_ask)
            hist = _fetch_history(conn, s.token_id, _hysteresis_batches())
            state, reason = _decide_state(edge, hist, now)
            records.append({
                "snap": s,
                "edge": edge,
                "state": state,
                "reason": reason,
                "prev_state": hist.last_state,
                "consec": (hist.consecutive_above_arm + 1
                           if (edge is not None and edge >= _arm_threshold()
                               and state != "cancelled")
                           else 0),
                "bucket": _correlation_bucket(s),
            })

        # Second pass: rank executable/armed by edge desc, allocate budget
        active = [r for r in records if r["state"] in ("armed", "executable")]
        active.sort(key=lambda r: (r["edge"] or 0.0), reverse=True)

        bucket_used: dict[str, float] = {}
        global_used = 0.0
        for r in active:
            raw_target = r["snap"].target_position_usdc or 0.0
            # cap by remaining bucket and global budget
            bkt = r["bucket"]
            bkt_remaining = max(0.0, bucket_cap - bucket_used.get(bkt, 0.0))
            global_remaining = max(0.0, global_cap - global_used)
            capped = min(max(0.0, raw_target), bkt_remaining, global_remaining)
            r["target_raw"] = raw_target
            r["target_capped"] = capped
            r["bucket_used"] = bucket_used.get(bkt, 0.0) + capped
            bucket_used[bkt] = r["bucket_used"]
            global_used += capped

        # Inactive tokens get raw=target, capped=0
        for r in records:
            if "target_capped" not in r:
                r["target_raw"] = r["snap"].target_position_usdc or 0.0
                r["target_capped"] = 0.0
                r["bucket_used"] = bucket_used.get(r["bucket"], 0.0)

        # Persist
        cur = conn.cursor()
        rows = [
            (
                run_id, batch_id, r["snap"].token_id, r["state"],
                r["prev_state"], r["consec"], r["edge"],
                r["snap"].fair_event, r["snap"].best_ask, r["snap"].best_bid,
                r["target_raw"], r["target_capped"],
                r["bucket"], r["bucket_used"], r["reason"],
                json.dumps({
                    "arm_threshold": _arm_threshold(),
                    "disarm_threshold": _disarm_threshold(),
                    "hysteresis_batches": _hysteresis_batches(),
                    "cooldown_min": _cooldown_min(),
                    "bucket_max_usdc": bucket_cap,
                    "global_max_usdc": global_cap,
                    "side_above": r["snap"].side_above,
                }),
            )
            for r in records
        ]
        cur.executemany(
            """
            INSERT INTO advisory_pathview_shadow_intents
              (run_id, batch_id, token_id, state, prev_state,
               consecutive_above_arm, edge_to_fair, fair_event,
               best_ask, best_bid, target_size_usdc_raw, target_size_usdc_capped,
               correlation_bucket, bucket_size_used_usdc,
               transition_reason, components)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (run_id, token_id) DO NOTHING
            """,
            rows,
        )
        conn.commit()
    return len(records)
