"""
Advisory metrics + alerts (A6).

无副作用纯查询脚本; 输出 advisory pipeline 的关键健康指标 + 阈值告警。
适合接入 systemd timer / cron 周期跑, 配合 --check --json 让外层告警系统
按 exit code 决定是否触发告警。

指标 (plan-advisory §7 A6):
- settlement_coverage         — 最新 settlement_feed_version 的 refresh_status,
                                 missing_condition_count, refreshed_condition_count
- batch_freshness_seconds     — now - 最新 complete batch 的 batch_completed_at
                                 (高于阈值 → dashboard 应显示 "数据陈旧" 横幅)
- batch_failure_rate          — 最近 N 小时内 status='failed' / 总 batch 数
- disputed_count              — 最新 settlement_feed_version 中 settlement_state='disputed'
                                 的 condition_id 数 (人工需关注)
- settlement_state_flip_count — 最近 24h 内同一 condition_id 跨版本发生
                                 settlement_state 翻转的次数 (e.g. settled→disputed)
- manual_trades_24h           — 最近 24h 用户手动下单数 + 总 size_usdc

用法:
    LD_PRELOAD="" uv run scripts/advisory_metrics.py
    LD_PRELOAD="" uv run scripts/advisory_metrics.py --json
    LD_PRELOAD="" uv run scripts/advisory_metrics.py --check
        # 任一阈值超限则 exit code 2; --json 模式同时输出 alerts 数组
    LD_PRELOAD="" uv run scripts/advisory_metrics.py --check --alert-email
        # 同上, 且若有告警则向 config.TO_EMAIL 发送一封 SMTP 邮件
        # (带去重 + 冷却, 状态文件 logs/.advisory_alert_state.json)

阈值 (CLI 可覆盖):
- --max-batch-freshness-sec      默认 600 (10 min, dashboard staleness 5 min 的 2x)
- --max-batch-failure-rate       默认 0.10 (10%)
- --max-settlement-missing       默认 0  (任何缺失立即告警)
- --max-disputed                 默认 0  (任何 disputed 立即告警, 人工 review)
- --max-state-flips-24h          默认 0  (任何翻转都需要人工 review)
- --batch-window-hours           默认 6  (failure rate 统计窗口)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

# allow running as `uv run scripts/advisory_metrics.py`
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data.database import get_conn  # noqa: E402

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
#  Email alert bridge (R4) — dedup + cooldown
# ---------------------------------------------------------------------------

ALERT_STATE_PATH = os.path.join("logs", ".advisory_alert_state.json")


def _alert_fingerprint(alerts: list[dict]) -> str:
    key = sorted((a.get("metric"), a.get("severity")) for a in alerts)
    return json.dumps(key, sort_keys=True)


def _load_alert_state() -> dict:
    try:
        with open(ALERT_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_alert_state(state: dict) -> None:
    os.makedirs(os.path.dirname(ALERT_STATE_PATH) or ".", exist_ok=True)
    tmp = ALERT_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, ALERT_STATE_PATH)


def _format_alert_email(alerts: list[dict], metrics: dict) -> tuple[str, str]:
    """Return (subject, plain_text_body) for the alert email."""
    severities = sorted({a.get("severity", "warn").upper() for a in alerts})
    subject = f"[advisory-alert] {len(alerts)} alert(s) " + "/".join(severities)

    lines = [
        "Advisory pipeline alerts fired:",
        "",
    ]
    for a in alerts:
        lines.append(f"  [{a.get('severity', 'warn').upper():<8}] "
                     f"{a.get('metric')}: {a.get('message')}")
    lines.append("")
    lines.append("--- Snapshot metrics ---")
    bf = metrics.get("batch_freshness", {})
    sc = metrics.get("settlement_coverage", {})
    bfr = metrics.get("batch_failure_rate", {})
    lines.append(
        f"batch_freshness: id={bf.get('latest_batch_id')} "
        f"age={bf.get('age_seconds')}s status={bf.get('status')}"
    )
    lines.append(
        f"settlement_coverage: version={sc.get('version')} "
        f"status={sc.get('refresh_status')} missing={sc.get('missing_condition_count')}"
    )
    lines.append(
        f"batch_failure_rate: window={bfr.get('window_hours')}h "
        f"rate={bfr.get('failure_rate')} total={bfr.get('total_count')}"
    )
    lines.append("")
    lines.append(f"generated_at_utc: {datetime.now(timezone.utc).isoformat()}")
    return subject, "\n".join(lines)


def maybe_send_alert_email(alerts: list[dict], metrics: dict,
                           cooldown_seconds: float = 3600.0,
                           dry_run: bool = False) -> dict:
    """If alerts exist and (fingerprint changed OR cooldown elapsed), send email.

    Returns a status dict describing what happened (sent / skipped / failed).
    Persists state in `logs/.advisory_alert_state.json` so repeat cron runs
    don't spam the inbox.
    """
    if not alerts:
        # clear last fingerprint so next alert (even same as before) is treated as fresh
        state = _load_alert_state()
        if state.get("last_fingerprint"):
            state["last_clear_at_utc"] = datetime.now(timezone.utc).isoformat()
            state["last_fingerprint"] = None
            _save_alert_state(state)
        return {"action": "none", "reason": "no alerts"}

    state = _load_alert_state()
    fp = _alert_fingerprint(alerts)
    last_fp = state.get("last_fingerprint")
    last_sent_iso = state.get("last_sent_at_utc")

    if last_fp == fp and last_sent_iso:
        try:
            last_sent = datetime.fromisoformat(last_sent_iso)
            age = (datetime.now(timezone.utc) - last_sent).total_seconds()
            if age < cooldown_seconds:
                return {
                    "action": "skipped",
                    "reason": f"same fingerprint within cooldown ({age:.0f}s < {cooldown_seconds:.0f}s)",
                    "fingerprint": fp,
                }
        except ValueError:
            pass

    subject, body = _format_alert_email(alerts, metrics)

    if dry_run:
        return {"action": "dry_run", "subject": subject, "body_preview": body[:200]}

    try:
        from notifications.email import EmailSender  # local import: avoid hard dep when unused
        from config import TO_EMAIL
        if not TO_EMAIL:
            return {"action": "failed", "reason": "TO_EMAIL not configured"}
        sender = EmailSender()
        ok = sender.send_email(TO_EMAIL, subject, body, content_type="plain")
    except Exception as exc:
        logger.exception("alert email send raised")
        return {"action": "failed", "reason": f"exception: {exc}"}

    if not ok:
        return {"action": "failed", "reason": "EmailSender.send_email returned False"}

    state["last_fingerprint"] = fp
    state["last_sent_at_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_alert_count"] = len(alerts)
    _save_alert_state(state)
    return {"action": "sent", "subject": subject, "fingerprint": fp}


# ---------------------------------------------------------------------------
#  Metric collectors
# ---------------------------------------------------------------------------

def collect_settlement_coverage() -> dict:
    """最新 settlement_feed_version 的 refresh_status + 缺失统计."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT settlement_feed_version, refreshed_at_utc, refresh_status,
                   rows_upserted, refreshed_condition_ids, missing_condition_ids
            FROM settlement_feed_versions
            ORDER BY settlement_feed_version DESC
            LIMIT 1
        """)
        row = cur.fetchone()
    if not row:
        return {"version": None, "status": "no_versions"}
    version, refreshed_at, status, upserted, refreshed_ids, missing_ids = row
    refreshed_ids = refreshed_ids or []
    missing_ids = missing_ids or []
    return {
        "version": version,
        "refreshed_at_utc": refreshed_at.astimezone(timezone.utc).isoformat()
            if isinstance(refreshed_at, datetime) else None,
        "refresh_status": status,
        "rows_upserted": upserted,
        "refreshed_condition_count": len(refreshed_ids),
        "missing_condition_count": len(missing_ids),
        "missing_condition_ids": missing_ids[:20],  # 截断, 防 log 爆炸
    }


def collect_batch_freshness() -> dict:
    """最新 complete batch + 距今秒数."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT id, batch_sequence, batch_completed_at, token_count
            FROM market_view_batches
            WHERE status = 'complete'
            ORDER BY batch_sequence DESC
            LIMIT 1
        """)
        row = cur.fetchone()
    if not row:
        return {"latest_batch_id": None, "freshness_seconds": None}
    batch_id, seq, completed_at, tok_count = row
    if isinstance(completed_at, datetime):
        age = (datetime.now(timezone.utc) - completed_at.astimezone(timezone.utc))
        age_sec = age.total_seconds()
        completed_iso = completed_at.astimezone(timezone.utc).isoformat()
    else:
        age_sec = None
        completed_iso = None
    return {
        "latest_batch_id": batch_id,
        "batch_sequence": seq,
        "batch_completed_at": completed_iso,
        "token_count": tok_count,
        "freshness_seconds": age_sec,
    }


def collect_batch_failure_rate(window_hours: int) -> dict:
    """最近 N 小时内 batch 完成情况按 status 聚合 + failure_rate."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT status, COUNT(*)
            FROM market_view_batches
            WHERE generated_at >= %s
            GROUP BY status
        """, (cutoff,))
        counts: dict[str, int] = defaultdict(int)
        for status, n in cur.fetchall():
            counts[status] = int(n)
    total = sum(counts.values())
    failed = counts.get("failed", 0)
    started = counts.get("started", 0)  # 长时间 started 也是异常
    failure_rate = (failed / total) if total > 0 else 0.0
    return {
        "window_hours": window_hours,
        "total_batches": total,
        "by_status": dict(counts),
        "failure_rate": round(failure_rate, 4),
        "stuck_started_count": started,
    }


def collect_disputed_count() -> dict:
    """最新 settlement_feed_version 中 settlement_state='disputed' 的 condition 数."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT condition_id, market_slug
            FROM settlement_feed_records
            WHERE settlement_feed_version = (
                SELECT MAX(settlement_feed_version) FROM settlement_feed_versions
            )
            AND settlement_state = 'disputed'
            ORDER BY condition_id
        """)
        rows = cur.fetchall()
    return {
        "disputed_count": len(rows),
        "disputed_conditions": [
            {"condition_id": cid, "market_slug": slug} for cid, slug in rows[:20]
        ],
    }


def collect_state_flips(window_hours: int = 24) -> dict:
    """
    最近 N 小时内同一 condition_id 跨版本发生 settlement_state 翻转的次数.
    翻转定义: 同一 condition_id 的两个相邻版本 settlement_state 不同.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            WITH recent AS (
                SELECT r.condition_id, r.settlement_state, r.settlement_feed_version
                FROM settlement_feed_records r
                JOIN settlement_feed_versions v
                  ON v.settlement_feed_version = r.settlement_feed_version
                WHERE v.refreshed_at_utc >= %s
            ),
            with_prev AS (
                SELECT condition_id, settlement_state, settlement_feed_version,
                       LAG(settlement_state) OVER (
                         PARTITION BY condition_id
                         ORDER BY settlement_feed_version
                       ) AS prev_state
                FROM recent
            )
            SELECT condition_id, prev_state, settlement_state, settlement_feed_version
            FROM with_prev
            WHERE prev_state IS NOT NULL AND prev_state <> settlement_state
            ORDER BY settlement_feed_version DESC, condition_id
        """, (cutoff,))
        flips = cur.fetchall()
    return {
        "window_hours": window_hours,
        "flip_count": len(flips),
        "flips": [
            {"condition_id": cid, "from": prev, "to": cur_state, "version": ver}
            for cid, prev, cur_state, ver in flips[:20]
        ],
    }


def collect_manual_trades_24h() -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT side, COUNT(*), COALESCE(SUM(size_usdc), 0)
            FROM manual_trades
            WHERE executed_at_utc >= %s
            GROUP BY side
        """, (cutoff,))
        rows = cur.fetchall()
    by_side = {side: {"count": int(n), "total_size_usdc": float(s)} for side, n, s in rows}
    total = sum(v["count"] for v in by_side.values())
    return {
        "window_hours": 24,
        "total_trades": total,
        "by_side": by_side,
    }


# ---------------------------------------------------------------------------
#  Alerting
# ---------------------------------------------------------------------------

def evaluate_alerts(metrics: dict, thresholds: dict) -> list[dict]:
    """对收集到的 metrics 应用阈值, 输出 alert 列表 (空 = healthy)."""
    alerts: list[dict] = []

    # 1. batch freshness
    bf = metrics["batch_freshness"]
    fresh_sec = bf.get("freshness_seconds")
    max_fresh = thresholds["max_batch_freshness_sec"]
    if fresh_sec is None:
        alerts.append({
            "metric": "batch_freshness",
            "severity": "critical",
            "message": "no complete batch found in DB",
        })
    elif fresh_sec > max_fresh:
        alerts.append({
            "metric": "batch_freshness",
            "severity": "high",
            "message": f"latest complete batch is {fresh_sec:.0f}s old "
                       f"(threshold {max_fresh}s)",
            "value": fresh_sec,
        })

    # 2. settlement coverage
    sc = metrics["settlement_coverage"]
    miss_count = sc.get("missing_condition_count")
    refresh_status = sc.get("refresh_status")
    max_missing = thresholds["max_settlement_missing"]
    if sc.get("status") == "no_versions":
        alerts.append({
            "metric": "settlement_coverage",
            "severity": "critical",
            "message": "no settlement_feed_versions row exists",
        })
    elif refresh_status == "failed":
        alerts.append({
            "metric": "settlement_coverage",
            "severity": "critical",
            "message": "latest settlement refresh status='failed'",
        })
    elif refresh_status == "partial" or (miss_count is not None and miss_count > max_missing):
        alerts.append({
            "metric": "settlement_coverage",
            "severity": "high",
            "message": f"{miss_count} condition(s) missing from latest refresh "
                       f"(status={refresh_status}, threshold {max_missing})",
            "value": miss_count,
        })

    # 3. batch failure rate
    bfr = metrics["batch_failure_rate"]
    rate = bfr.get("failure_rate", 0.0)
    max_rate = thresholds["max_batch_failure_rate"]
    if rate > max_rate:
        alerts.append({
            "metric": "batch_failure_rate",
            "severity": "high",
            "message": f"batch failure rate {rate:.2%} > {max_rate:.2%} "
                       f"in last {bfr['window_hours']}h "
                       f"({bfr['by_status'].get('failed', 0)}/{bfr['total_batches']})",
            "value": rate,
        })

    # 4. disputed
    dp = metrics["disputed_count"]
    n_disputed = dp.get("disputed_count", 0)
    if n_disputed > thresholds["max_disputed"]:
        alerts.append({
            "metric": "disputed_count",
            "severity": "high",
            "message": f"{n_disputed} condition(s) currently disputed",
            "value": n_disputed,
        })

    # 5. state flips
    sf = metrics["settlement_state_flips"]
    n_flips = sf.get("flip_count", 0)
    if n_flips > thresholds["max_state_flips_24h"]:
        alerts.append({
            "metric": "settlement_state_flips",
            "severity": "medium",
            "message": f"{n_flips} settlement state flip(s) in last 24h",
            "value": n_flips,
        })

    return alerts


# ---------------------------------------------------------------------------
#  CLI
# ---------------------------------------------------------------------------

def collect_all(window_hours: int) -> dict:
    return {
        "settlement_coverage": collect_settlement_coverage(),
        "batch_freshness": collect_batch_freshness(),
        "batch_failure_rate": collect_batch_failure_rate(window_hours),
        "disputed_count": collect_disputed_count(),
        "settlement_state_flips": collect_state_flips(),
        "manual_trades_24h": collect_manual_trades_24h(),
    }


def _print_text(metrics: dict, alerts: list) -> None:
    print("=" * 72)
    print("ADVISORY METRICS")
    print("=" * 72)

    sc = metrics["settlement_coverage"]
    print(f"settlement_coverage: version={sc.get('version')} "
          f"status={sc.get('refresh_status')} "
          f"refreshed={sc.get('refreshed_condition_count')} "
          f"missing={sc.get('missing_condition_count')}")
    if sc.get("missing_condition_ids"):
        print(f"  missing IDs: {sc['missing_condition_ids']}")

    bf = metrics["batch_freshness"]
    fresh_str = f"{bf['freshness_seconds']:.0f}s" if bf.get("freshness_seconds") else "N/A"
    print(f"batch_freshness: id={bf.get('latest_batch_id')} "
          f"seq={bf.get('batch_sequence')} age={fresh_str} "
          f"completed_at={bf.get('batch_completed_at')}")

    bfr = metrics["batch_failure_rate"]
    print(f"batch_failure_rate: window={bfr['window_hours']}h "
          f"total={bfr['total_batches']} by_status={dict(bfr['by_status'])} "
          f"rate={bfr['failure_rate']:.2%}")

    dp = metrics["disputed_count"]
    print(f"disputed_count: n={dp['disputed_count']}")
    for d in dp.get("disputed_conditions", [])[:5]:
        print(f"  - {d['condition_id']} ({d['market_slug']})")

    sf = metrics["settlement_state_flips"]
    print(f"state_flips_24h: n={sf['flip_count']}")
    for f in sf.get("flips", [])[:5]:
        print(f"  - {f['condition_id']}: {f['from']} -> {f['to']} (v{f['version']})")

    mt = metrics["manual_trades_24h"]
    print(f"manual_trades_24h: total={mt['total_trades']} by_side={mt['by_side']}")

    print()
    print("=" * 72)
    if alerts:
        print(f"ALERTS ({len(alerts)})")
        print("=" * 72)
        for a in alerts:
            print(f"  [{a['severity'].upper():<8}] {a['metric']}: {a['message']}")
    else:
        print("ALERTS: none — healthy ✓")


def main():
    parser = argparse.ArgumentParser(description="Advisory metrics + alerts (A6)")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON instead of text table.")
    parser.add_argument("--check", action="store_true",
                        help="Exit 2 if any alert fires (else 0); useful for cron/timer.")
    parser.add_argument("--batch-window-hours", type=int, default=6,
                        help="Window for batch_failure_rate aggregation (default 6).")
    parser.add_argument("--max-batch-freshness-sec", type=float, default=600,
                        help="Threshold (sec) for batch_freshness alert (default 600).")
    parser.add_argument("--max-batch-failure-rate", type=float, default=0.10,
                        help="Threshold for batch_failure_rate alert (default 0.10).")
    parser.add_argument("--max-settlement-missing", type=int, default=0,
                        help="Threshold for missing_condition_count (default 0).")
    parser.add_argument("--max-disputed", type=int, default=0,
                        help="Threshold for disputed_count alert (default 0).")
    parser.add_argument("--max-state-flips-24h", type=int, default=0,
                        help="Threshold for settlement_state_flips alert (default 0).")
    parser.add_argument("--alert-email", action="store_true",
                        help="Send SMTP email to config.TO_EMAIL when alerts fire "
                             "(R4). Dedup + cooldown via logs/.advisory_alert_state.json.")
    parser.add_argument("--alert-cooldown-sec", type=float, default=3600.0,
                        help="Suppress repeat emails for the same alert fingerprint "
                             "within this window (default 3600s = 1h).")
    parser.add_argument("--alert-email-dry-run", action="store_true",
                        help="With --alert-email, format the email but don't send.")
    args = parser.parse_args()

    thresholds = {
        "max_batch_freshness_sec": args.max_batch_freshness_sec,
        "max_batch_failure_rate": args.max_batch_failure_rate,
        "max_settlement_missing": args.max_settlement_missing,
        "max_disputed": args.max_disputed,
        "max_state_flips_24h": args.max_state_flips_24h,
    }

    metrics = collect_all(args.batch_window_hours)
    alerts = evaluate_alerts(metrics, thresholds)

    if args.json:
        out = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "thresholds": thresholds,
            "metrics": metrics,
            "alerts": alerts,
        }
    else:
        _print_text(metrics, alerts)
        out = None

    if args.alert_email:
        email_status = maybe_send_alert_email(
            alerts, metrics,
            cooldown_seconds=args.alert_cooldown_sec,
            dry_run=args.alert_email_dry_run,
        )
        if args.json:
            out["alert_email"] = email_status  # type: ignore[index]
        else:
            print(f"alert_email: {email_status['action']} "
                  f"({email_status.get('reason') or email_status.get('subject') or ''})")

    if args.json:
        print(json.dumps(out, indent=2, default=str))

    return 2 if (args.check and alerts) else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    sys.exit(main())
