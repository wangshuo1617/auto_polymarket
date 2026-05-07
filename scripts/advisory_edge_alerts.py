"""
Advisory edge-flip alerts (position-scoped, 5min cadence).

业务级通知 (区别于 advisory_metrics.py 的健康告警):
仅针对当前**有持仓**的 token, 用最新 fair_value (来自 hourly path_views)
+ 实时 CLOB best_ask 计算 edge = fair − ask.
当 edge 从 ≥ MIN_PREV_EDGE 翻转到 ≤ 0 时, 触发邮件汇总.

特点:
- 5min 触发, 但 fair 仍来自 hourly batch (中间 5min 的 edge 变化主要
  由 ask 价波动驱动, 这正是要监控的)
- 仅监控持仓 token (analyze profile wallet 的 Polymarket positions)
- 同一 token 在 COOLDOWN_SECONDS 内只发一次 (默认 6h)
- 状态文件: logs/.advisory_edge_alert_state.json

用法:
    LD_PRELOAD="" uv run scripts/advisory_edge_alerts.py
    LD_PRELOAD="" uv run scripts/advisory_edge_alerts.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Optional

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data.database import get_conn  # noqa: E402
from data.polymarket import get_positions, get_best_prices  # noqa: E402

logger = logging.getLogger(__name__)
ALERT_STATE_PATH = os.path.join("logs", ".advisory_edge_alert_state.json")


def _load_state() -> dict:
    try:
        with open(ALERT_STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(ALERT_STATE_PATH) or ".", exist_ok=True)
    tmp = ALERT_STATE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, default=str)
    os.replace(tmp, ALERT_STATE_PATH)


def _fetch_latest_fair_for_tokens(token_ids: list[str]) -> dict[str, dict]:
    """token_id -> {fair, slug, outcome_index, days_left, batch_id}"""
    out: dict[str, dict] = {}
    if not token_ids:
        return out
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT ON (token_id)
                   token_id, batch_id, market_slug, fair_value_for_edge,
                   view_payload->>'outcome_index',
                   view_payload->>'days_left'
            FROM market_view_snapshots
            WHERE token_id = ANY(%s)
            ORDER BY token_id, batch_id DESC
            """,
            (list(token_ids),),
        )
        for tid, bid, slug, fair, oi, dl in cur.fetchall():
            out[tid] = {
                "fair": float(fair) if fair is not None else None,
                "slug": slug,
                "outcome_index": int(oi) if oi is not None else None,
                "days_left": float(dl) if dl is not None else None,
                "batch_id": bid,
            }
    return out


def _label(slug: str, oi: Optional[int]) -> str:
    side = "yes" if oi == 0 else ("no" if oi == 1 else "?")
    return f"{slug}::{side}"


def collect_position_token_ids(profile: str = "analyze") -> list[str]:
    raw = get_positions(profile=profile)
    out: list[str] = []
    for p in raw:
        tid = p.get("asset") or p.get("token_id")
        size = p.get("size")
        try:
            sz = float(size) if size is not None else 0.0
        except (TypeError, ValueError):
            sz = 0.0
        if tid and sz > 0:
            out.append(str(tid))
    return out


def detect_flips(token_ids: list[str], min_prev_edge: float):
    fairs = _fetch_latest_fair_for_tokens(token_ids)
    quotes = get_best_prices(token_ids)
    state = _load_state()

    flips: list[dict] = []
    observations: list[dict] = []
    for tid in token_ids:
        f = fairs.get(tid)
        q = quotes.get(tid) or {}
        ask = q.get("best_ask")
        if not f or f.get("fair") is None or ask is None or ask <= 0 or ask >= 1:
            continue
        fair = f["fair"]
        edge = fair - ask
        last = state.get(tid, {})
        prev_edge = last.get("last_edge")

        observations.append({
            "token_id": tid, "edge": edge, "fair": fair, "ask": ask,
            "slug": f["slug"], "outcome_index": f["outcome_index"],
            "days_left": f.get("days_left"),
        })

        if prev_edge is None:
            continue
        if prev_edge >= min_prev_edge and edge <= 0:
            flips.append({
                "token_id": tid, "slug": f["slug"],
                "outcome_index": f["outcome_index"],
                "prev_edge": prev_edge, "curr_edge": edge,
                "fair": fair, "ask": ask,
                "prev_ask": last.get("last_ask"),
                "days_left": f.get("days_left"),
            })
    flips.sort(key=lambda x: x["prev_edge"], reverse=True)
    return flips, observations


def _format_email(flips: list[dict]) -> tuple[str, str]:
    subject = f"[advisory-edge] 持仓 {len(flips)} 个 token 的 edge → 0/负"
    lines = [
        "以下持仓 token 的 edge 从 ≥阈值 翻转到 ≤0 (实时 ask 触发):",
        "",
        f"{'market::side':<55} {'prev_edge':>10} {'curr_edge':>10} "
        f"{'prev_ask':>9} {'curr_ask':>9} {'fair':>9} {'days':>6}",
        "-" * 115,
    ]
    for f in flips:
        lines.append(
            f"{_label(f['slug'], f['outcome_index'])[:55]:<55} "
            f"{f['prev_edge']:>+10.4f} {f['curr_edge']:>+10.4f} "
            f"{(f.get('prev_ask') or 0):>9.4f} {f['ask']:>9.4f} "
            f"{f['fair']:>9.4f} {(f.get('days_left') or 0):>6.2f}"
        )
    lines.append("")
    lines.append(f"generated_at_utc: {datetime.now(timezone.utc).isoformat()}")
    return subject, "\n".join(lines)


def maybe_send_email(flips: list[dict], cooldown_seconds: float,
                     dry_run: bool) -> dict:
    if not flips:
        return {"action": "none", "reason": "no flips"}
    state = _load_state()
    now = datetime.now(timezone.utc)
    fresh: list[dict] = []
    for f in flips:
        last = state.get(f["token_id"], {}).get("last_alert_at_utc")
        if last:
            try:
                age = (now - datetime.fromisoformat(last)).total_seconds()
                if age < cooldown_seconds:
                    continue
            except ValueError:
                pass
        fresh.append(f)
    if not fresh:
        return {"action": "skipped", "reason": "all flips within cooldown",
                "total": len(flips)}

    subject, body = _format_email(fresh)
    if dry_run:
        return {"action": "dry_run", "subject": subject,
                "body_preview": body[:400], "fresh_count": len(fresh)}

    try:
        from notifications.email import EmailSender
        from config import TO_EMAIL
        if not TO_EMAIL:
            return {"action": "failed", "reason": "TO_EMAIL not configured"}
        ok = EmailSender().send_email(TO_EMAIL, subject, body, content_type="plain")
    except Exception as exc:
        logger.exception("edge-alert email send raised")
        return {"action": "failed", "reason": f"exception: {exc}"}
    if not ok:
        return {"action": "failed", "reason": "EmailSender.send_email returned False"}

    iso = now.isoformat()
    for f in fresh:
        cur = state.get(f["token_id"], {})
        cur["last_alert_at_utc"] = iso
        cur["last_alert_prev_edge"] = f["prev_edge"]
        cur["last_alert_curr_edge"] = f["curr_edge"]
        state[f["token_id"]] = cur
    _save_state(state)
    return {"action": "sent", "subject": subject, "fresh_count": len(fresh),
            "total": len(flips)}


def update_observations(observations: list[dict]) -> None:
    state = _load_state()
    iso = datetime.now(timezone.utc).isoformat()
    seen = set()
    for o in observations:
        tid = o["token_id"]
        seen.add(tid)
        cur = state.get(tid, {})
        cur["last_edge"] = o["edge"]
        cur["last_ask"] = o["ask"]
        cur["last_fair"] = o["fair"]
        cur["last_observed_at_utc"] = iso
        cur["slug"] = o["slug"]
        cur["outcome_index"] = o["outcome_index"]
        state[tid] = cur
    # 不再持仓的 token: 清掉 last_edge/ask 防止下次进场误判 prev_edge
    for tid in list(state.keys()):
        if tid not in seen:
            cur = state.get(tid, {})
            cur.pop("last_edge", None)
            cur.pop("last_ask", None)
            cur.pop("last_fair", None)
            state[tid] = cur
    _save_state(state)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-prev-edge", type=float, default=0.02,
                        help="只在前次 edge >= 该值时才视为'原本有 edge' (默认 0.02)")
    parser.add_argument("--cooldown-sec", type=float, default=6 * 3600,
                        help="同 token 冷却秒数 (默认 6h)")
    parser.add_argument("--profile", default="analyze")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    held = collect_position_token_ids(profile=args.profile)
    logger.info("held tokens (size>0, profile=%s): %d", args.profile, len(held))
    if not held:
        msg = {"action": "none", "reason": "no held tokens"}
        print(json.dumps(msg) if args.json else msg)
        return 0

    flips, observations = detect_flips(held, args.min_prev_edge)
    logger.info("flips=%d (out of %d observations)",
                len(flips), len(observations))
    result = maybe_send_email(flips, cooldown_seconds=args.cooldown_sec,
                              dry_run=args.dry_run)
    update_observations(observations)

    if args.json:
        print(json.dumps({"flips": flips, "observations": observations,
                          "result": result}, default=str))
    else:
        print(f"held={len(held)} obs={len(observations)} "
              f"flips={len(flips)} result={result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
