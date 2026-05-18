"""Advisory fair-value calibration drift monitor.

每天 cron 跑一次. 计算指标:
  M1 (terminal):  仅用已结算 market 的 winning_token_id 作 label.
                  反馈周期 = 月末. 月度报告用.
  M2 (intraday):  在 M1 基础上, 对未结算但 strike 已被 BTC 触发的 snapshot
                  也打 label (path-locked partial label). 提供日度漂移信号.

输出:
  - 写入 advisory_calibration_runs (每模式一行 + 一个 trades_json 占位)
  - stdout 打印 reliability table
  - ECE > THRESHOLD 时返回非 0 退出码 (供 cron / alert 钩子)

Usage:
  uv run python scripts/advisory_calibration_monitor.py [--mode M1|M2|both]
                                                        [--since-days 28]
                                                        [--ece-threshold 0.05]
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data.database import get_conn, get_cursor  # noqa: E402

logger = logging.getLogger("advisory.calibration")

DEFAULT_SINCE_DAYS = 28
DEFAULT_ECE_THRESHOLD = 0.05
N_BUCKETS = 10  # [0.0,0.1), [0.1,0.2), ..., [0.9,1.0]


@dataclass
class Bucket:
    lo: float
    hi: float
    n: int = 0
    sum_p: float = 0.0
    sum_y: float = 0.0

    @property
    def mean_p(self) -> float:
        return self.sum_p / self.n if self.n else 0.0

    @property
    def mean_y(self) -> float:
        return self.sum_y / self.n if self.n else 0.0


@dataclass
class CalibResult:
    mode: str
    n_total: int
    n_labelled: int
    ece: float
    brier: float
    log_loss: float
    buckets: list[Bucket] = field(default_factory=list)


def _make_buckets() -> list[Bucket]:
    edges = [i / N_BUCKETS for i in range(N_BUCKETS + 1)]
    return [Bucket(edges[i], edges[i + 1]) for i in range(N_BUCKETS)]


def _bucket_for(p: float, buckets: list[Bucket]) -> Bucket:
    for b in buckets:
        if b.lo <= p < b.hi:
            return b
    return buckets[-1]  # p == 1.0


def _compute_metrics(samples: list[tuple[float, float]], mode: str) -> CalibResult:
    """samples: list of (p, y∈{0,1})."""
    buckets = _make_buckets()
    brier_sum = 0.0
    logloss_sum = 0.0
    eps = 1e-9
    for p, y in samples:
        b = _bucket_for(p, buckets)
        b.n += 1
        b.sum_p += p
        b.sum_y += y
        brier_sum += (p - y) ** 2
        p_clip = min(max(p, eps), 1.0 - eps)
        logloss_sum += -(y * math.log(p_clip) + (1 - y) * math.log(1 - p_clip))
    n = len(samples)
    if n == 0:
        return CalibResult(mode=mode, n_total=0, n_labelled=0,
                           ece=0.0, brier=0.0, log_loss=0.0, buckets=buckets)
    ece = sum(abs(b.mean_p - b.mean_y) * b.n / n for b in buckets if b.n)
    return CalibResult(
        mode=mode,
        n_total=n,
        n_labelled=n,
        ece=ece,
        brier=brier_sum / n,
        log_loss=logloss_sum / n,
        buckets=buckets,
    )


def _fetch_settled_labels(since: datetime) -> list[tuple[float, float]]:
    """M1 terminal labels: snapshots where condition_id is in settled records.

    label = 1 if snapshot.token_id == settlement.winning_token_id else 0
    """
    sql = """
        SELECT s.fair_value_for_edge AS p,
               (s.token_id = sf.winning_token_id)::int AS y
          FROM market_view_snapshots s
          JOIN settlement_feed_records sf
            ON sf.condition_id = s.condition_id
           AND sf.settlement_state = 'settled'
         WHERE s.generated_at >= %s
           AND s.fair_value_for_edge IS NOT NULL
    """
    out: list[tuple[float, float]] = []
    with get_cursor() as cur:
        cur.execute(sql, (since,))
        for r in cur.fetchall():
            out.append((float(r["p"]), float(r["y"])))
    return out


def _fetch_path_locked_labels(since: datetime) -> list[tuple[float, float]]:
    """M2 partial labels: snapshots where strike has been touched in BTC kline
    history between snapshot time and NOW. Excludes already-settled (handled by M1).

    A "touch" is determined by:
      - For side_above=true (yes-up / no-up): touched if max(btc_price) ≥ strike
      - For side_above=false (yes-down / no-down): touched if min(btc_price) ≤ strike

    yes outcome (oi=0) wins on touch; no outcome (oi=1) loses on touch.

    NOTE: we only label the PROVEN side (touched markets); markets that have
    NOT yet touched are still uncertain at "now" and excluded.

    数据源已从本地 btc_poly_1s_ticks 切到 Binance 5m kline。
    """
    from datetime import timezone as _tz
    from data.binance import get_path_extrema

    sql = """
        SELECT s.id, s.market_slug, s.fair_value_for_edge AS p,
               (s.view_payload->>'strike_usd')::float AS strike,
               (s.view_payload->>'outcome_index') AS outcome_index,
               (s.view_payload->>'side_above')::bool AS side_above
          FROM market_view_snapshots s
     LEFT JOIN settlement_feed_records sf
            ON sf.condition_id = s.condition_id
           AND sf.settlement_state = 'settled'
         WHERE s.generated_at >= %s
           AND s.fair_value_for_edge IS NOT NULL
           AND sf.condition_id IS NULL
           AND (s.view_payload->>'strike_usd') IS NOT NULL
    """
    rows: list[dict] = []
    with get_cursor() as cur:
        cur.execute(sql, (since,))
        for r in cur.fetchall():
            rows.append(dict(r))

    if not rows:
        return []

    # BTC price is global, so one path-extrema fetch covers all snapshots.
    now = datetime.now(_tz.utc)
    try:
        pmax, pmin, _cov, n = get_path_extrema(since, now, interval="5m")
    except Exception:
        logger.exception("get_path_extrema failed")
        return []
    if n == 0 or pmax <= 0:
        return []

    out: list[tuple[float, float]] = []
    for r in rows:
        strike = float(r["strike"])
        side_above = bool(r["side_above"])
        touched = (pmax >= strike) if side_above else (pmin > 0 and pmin <= strike)
        if not touched:
            continue
        # outcome_index '0' = yes (wins on touch), '1' = no (loses on touch)
        y = 1.0 if r["outcome_index"] == "0" else 0.0
        out.append((float(r["p"]), y))
    return out


def _format_table(res: CalibResult) -> str:
    lines = [
        f"  bucket          n   mean_p   mean_y   |Δ|",
        f"  --------------------------------------------",
    ]
    for b in res.buckets:
        if not b.n:
            continue
        diff = abs(b.mean_p - b.mean_y)
        lines.append(
            f"  [{b.lo:.1f},{b.hi:.1f})  {b.n:>5d}   {b.mean_p:.3f}    {b.mean_y:.3f}   {diff:.3f}"
        )
    lines.append(f"  --------------------------------------------")
    lines.append(f"  total n={res.n_total}  ECE={res.ece:.4f}  Brier={res.brier:.4f}  LogLoss={res.log_loss:.4f}")
    return "\n".join(lines)


def _persist(res: CalibResult, since: datetime) -> int:
    payload = {
        "mode": res.mode,
        "ece": round(res.ece, 6),
        "brier": round(res.brier, 6),
        "log_loss": round(res.log_loss, 6),
        "n_buckets_nonempty": sum(1 for b in res.buckets if b.n),
        "buckets": [
            {"lo": b.lo, "hi": b.hi, "n": b.n,
             "mean_p": round(b.mean_p, 6), "mean_y": round(b.mean_y, 6)}
            for b in res.buckets if b.n
        ],
    }
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO advisory_calibration_runs
                   (since_utc, n_snapshots, brier, calibration_json, trades_json)
            VALUES (%s, %s, %s, %s::jsonb, %s::jsonb)
            RETURNING id
            """,
            (since, res.n_total, res.brier, json.dumps(payload), json.dumps([])),
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return int(new_id)


def run(mode: str, since_days: int, ece_threshold: float, persist: bool) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    modes = ["M1", "M2"] if mode == "both" else [mode]
    worst_ece = 0.0
    for m in modes:
        if m == "M1":
            samples = _fetch_settled_labels(since)
        elif m == "M2":
            samples = _fetch_settled_labels(since) + _fetch_path_locked_labels(since)
        else:
            print(f"unknown mode: {m}", file=sys.stderr)
            return 2
        res = _compute_metrics(samples, mode=m)
        print(f"\n=== mode={m} since={since.isoformat()} ===")
        if res.n_total == 0:
            print("  (no labelled samples — nothing to report)")
            continue
        print(_format_table(res))
        if persist:
            new_id = _persist(res, since)
            print(f"  → persisted advisory_calibration_runs.id={new_id}")
        worst_ece = max(worst_ece, res.ece)
    if worst_ece > ece_threshold:
        print(f"\n!! ECE {worst_ece:.4f} exceeds threshold {ece_threshold:.4f}", file=sys.stderr)
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["M1", "M2", "both"], default="both")
    p.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    p.add_argument("--ece-threshold", type=float, default=DEFAULT_ECE_THRESHOLD)
    p.add_argument("--no-persist", action="store_true",
                   help="dry run: compute + print only, skip DB write")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return run(args.mode, args.since_days, args.ece_threshold, persist=not args.no_persist)


if __name__ == "__main__":
    sys.exit(main())
