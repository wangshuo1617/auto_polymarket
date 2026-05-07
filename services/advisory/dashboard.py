"""
Advisory dashboard blueprint (A5).

提供 advisory-only fair-value rebalancer 的 dashboard 接入:
- GET /recommendations            — 推荐表 HTML
- GET /api/advisory/recommendations — 最新 complete batch 的 token MarketView (JSON)
- POST /api/advisory/manual_trades — 记录用户手动下单
- GET /api/advisory/manual_trades  — 列出最近 manual trades

数据契约 (plan-advisory §5):
- 路由数据源严格走 `get_latest_complete_batch_views()`, 不读 market_view_latest
  (latest 是 per-token 累加, universe 收缩后会污染推荐)
- Staleness 全局兜底: latest_complete.batch_completed_at 距今超过 STALENESS_THRESHOLD
  → 整个推荐区块替换为 "数据陈旧" 横幅, 不展示任何 token 行
- manual_trades 写入必须透传 dashboard 当前展示的 snapshot_id; PG trigger 强制
  (a) batch.status='complete' (b) snapshot.token_id == manual_trades.token_id
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from flask import Blueprint, jsonify, render_template, request

from data.database import get_conn
from services.advisory.computer import get_latest_complete_batch_views

logger = logging.getLogger(__name__)

advisory_bp = Blueprint("advisory", __name__)

# 推荐数据 staleness 阈值 (5 分钟内的 batch 才展示, 超过显示 "数据陈旧" 横幅)
STALENESS_THRESHOLD = timedelta(minutes=5)


# ---------------------------------------------------------------------------
#  Page
# ---------------------------------------------------------------------------

@advisory_bp.route("/recommendations")
def recommendations_page():
    """渲染建议表页面 (实际数据由前端调用 /api/advisory/recommendations 加载)."""
    return render_template("advisory_recommendations.html")


# ---------------------------------------------------------------------------
#  API
# ---------------------------------------------------------------------------

def _serialize_snapshot(s: dict) -> dict:
    """把 DB row dict 转成前端友好 JSON; 时间统一 ISO 8601 UTC."""
    out = dict(s)
    if isinstance(out.get("generated_at"), datetime):
        out["generated_at"] = out["generated_at"].astimezone(timezone.utc).isoformat()
    if isinstance(out.get("view_payload"), str):
        # JSONB 在 psycopg2 通常自动反序列化, 但 fallback 保险
        try:
            import json
            out["view_payload"] = json.loads(out["view_payload"])
        except Exception:
            pass
    return out


@advisory_bp.route("/api/advisory/recommendations", methods=["GET"])
def api_recommendations():
    """返回最新 complete batch 的 MarketView 列表, 按 ranking_score DESC 排序."""
    try:
        result = get_latest_complete_batch_views()
    except Exception as exc:
        logger.exception("get_latest_complete_batch_views failed")
        return jsonify({"error": f"failed to load latest batch: {exc}"}), 500

    if result is None:
        result = (None, [])
    batch, snapshots = result

    if batch is None:
        return jsonify({
            "status": "no_batch",
            "stale": False,
            "batch": None,
            "snapshots": [],
        })

    completed_at = batch.get("batch_completed_at")
    if isinstance(completed_at, datetime):
        age = datetime.now(timezone.utc) - completed_at.astimezone(timezone.utc)
        is_stale = age > STALENESS_THRESHOLD
        completed_iso = completed_at.astimezone(timezone.utc).isoformat()
    else:
        is_stale = True
        completed_iso = None

    # plan §5.1 staleness 兜底: 不展示任何 token 行, 由前端渲染横幅
    return jsonify({
        "status": "stale" if is_stale else "ok",
        "stale": is_stale,
        "staleness_threshold_seconds": int(STALENESS_THRESHOLD.total_seconds()),
        "batch": {
            "id": batch.get("id"),
            "batch_sequence": batch.get("batch_sequence"),
            "batch_completed_at": completed_iso,
            "token_count": batch.get("token_count"),
        },
        "snapshots": [] if is_stale else [_serialize_snapshot(s) for s in snapshots],
    })


@advisory_bp.route("/api/advisory/manual_trades", methods=["POST"])
def api_manual_trade_create():
    """
    记录用户手动下单. 必填 (token_id, side, price_usdc, size_usdc, snapshot_id);
    可选 (executed_at_utc, user_note). 后端依赖 PG trigger 校验 snapshot 一致性
    (token_id 匹配 + batch.status='complete'); trigger 异常会以 409 返回.
    """
    payload = request.get_json(silent=True) or {}
    required = ("token_id", "side", "price_usdc", "size_usdc",
                "market_view_snapshot_id_at_decision")
    missing = [k for k in required if payload.get(k) in (None, "")]
    if missing:
        return jsonify({"error": f"missing required fields: {missing}"}), 400

    side = str(payload["side"]).lower()
    if side not in ("buy", "sell"):
        return jsonify({"error": f"side must be buy or sell, got {side!r}"}), 400

    try:
        price = float(payload["price_usdc"])
        size = float(payload["size_usdc"])
        snap_id = int(payload["market_view_snapshot_id_at_decision"])
    except (TypeError, ValueError) as exc:
        return jsonify({"error": f"numeric field parse error: {exc}"}), 400

    if not (0.0 < price < 1.0):
        return jsonify({"error": "price_usdc must be in (0, 1)"}), 400
    if size <= 0:
        return jsonify({"error": "size_usdc must be > 0"}), 400

    executed_at_raw = payload.get("executed_at_utc")
    if executed_at_raw:
        try:
            executed_at = datetime.fromisoformat(str(executed_at_raw).replace("Z", "+00:00"))
            if executed_at.tzinfo is None:
                executed_at = executed_at.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            return jsonify({"error": f"executed_at_utc invalid ISO 8601: {exc}"}), 400
    else:
        executed_at = datetime.now(timezone.utc)

    note: Optional[str] = payload.get("user_note") or None
    if note and len(note) > 1024:
        note = note[:1024]

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO manual_trades
                    (executed_at_utc, token_id, side, price_usdc, size_usdc,
                     market_view_snapshot_id_at_decision, user_note)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id, created_at
                """,
                (executed_at, str(payload["token_id"]), side, price, size, snap_id, note),
            )
            new_id, created_at = cur.fetchone()
            conn.commit()
    except Exception as exc:
        # PG trigger raise EXCEPTION 会落到这里 (psycopg2.errors.RaiseException)
        msg = str(exc)
        logger.warning("manual_trade insert rejected: %s", msg)
        # snapshot 不存在 / token_id 不匹配 / batch 未 complete → 409 Conflict
        if "snapshot" in msg.lower() or "manual_trades" in msg.lower():
            return jsonify({"error": msg}), 409
        return jsonify({"error": msg}), 500

    return jsonify({
        "id": new_id,
        "created_at": created_at.astimezone(timezone.utc).isoformat()
            if isinstance(created_at, datetime) else None,
        "executed_at_utc": executed_at.astimezone(timezone.utc).isoformat(),
    }), 201


@advisory_bp.route("/api/advisory/manual_trades", methods=["GET"])
def api_manual_trade_list():
    """列出最近 manual trades; 默认 50 条, 可 ?limit=N (max 200)."""
    try:
        limit = min(int(request.args.get("limit", 50)), 200)
    except (TypeError, ValueError):
        limit = 50

    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT mt.id, mt.created_at, mt.executed_at_utc, mt.token_id,
                       mt.side, mt.price_usdc, mt.size_usdc,
                       mt.market_view_snapshot_id_at_decision, mt.user_note,
                       s.market_slug, s.condition_id, s.fair_value_for_edge
                FROM manual_trades mt
                LEFT JOIN market_view_snapshots s
                    ON s.id = mt.market_view_snapshot_id_at_decision
                ORDER BY mt.executed_at_utc DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.exception("manual_trades list failed")
        return jsonify({"error": str(exc)}), 500

    out = []
    for r in rows:
        (tid, created, exec_at, token, side, price, size, snap_id, note,
         slug, cid, fair) = r
        out.append({
            "id": tid,
            "created_at": created.astimezone(timezone.utc).isoformat()
                if isinstance(created, datetime) else None,
            "executed_at_utc": exec_at.astimezone(timezone.utc).isoformat()
                if isinstance(exec_at, datetime) else None,
            "token_id": token,
            "side": side,
            "price_usdc": float(price) if price is not None else None,
            "size_usdc": float(size) if size is not None else None,
            "snapshot_id": snap_id,
            "market_slug": slug,
            "condition_id": cid,
            "fair_at_decision": float(fair) if fair is not None else None,
            "user_note": note,
        })
    return jsonify({"trades": out, "count": len(out)})
