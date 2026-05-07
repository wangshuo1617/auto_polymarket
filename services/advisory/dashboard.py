"""
Advisory dashboard blueprint (A5).

提供 advisory-only fair-value rebalancer 的 dashboard 接入:
- GET /recommendations            — 推荐表 HTML
- GET /api/advisory/recommendations — 最新 complete batch 的 token MarketView (JSON)
- POST /api/advisory/manual_trades — 记录用户手动下单
- GET /api/advisory/manual_trades  — 列出最近 manual trades
- GET /api/advisory/portfolio      — manual_trades 派生持仓 + 最新快照对照 (P1)
- GET /api/advisory/user_thesis    — 当前 active 自由文本判断 (P2)
- POST /api/advisory/user_thesis   — 设置/替换 active 自由文本判断
- DELETE /api/advisory/user_thesis — 清除 active 自由文本判断

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


# ---------------------------------------------------------------------------
#  Portfolio (P1) — advisory_chain_fills 派生当前持仓 + 最新 advisory 快照对照
# ---------------------------------------------------------------------------
#  v2 重构: 数据源由 manual_trades 切换为 advisory_chain_fills (链上事实).
#       好处:
#       (a) 不再需要"用户主动记录" — dashboard 之外的下单(直链/Builder/外部钱包)
#           只要钱包在 advisory universe 内, 持仓自动反映;
#       (b) 取消的订单不会污染持仓 (取消未成交, 链上不出现 fill);
#       (c) shares 直接来自 transferLog, 不再 size_usdc / price 间接派生 → 更准.
#  返回 schema 与 v1 兼容; 仅增加 fill_count = buy_count + sell_count 在 totals 中.
#  on-chain 实际持仓对账由 P4 处理; 这里仅用 advisory_chain_fills 自报数据.

@advisory_bp.route("/api/advisory/portfolio", methods=["GET"])
def api_portfolio():
    """
    返回 advisory_chain_fills 聚合得到的每个 token 净持仓, 配上最新 advisory 快照行情.
    """
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                WITH agg AS (
                    SELECT
                        token_id,
                        SUM(CASE WHEN side='buy'  THEN size_shares ELSE 0 END) AS shares_buy,
                        SUM(CASE WHEN side='sell' THEN size_shares ELSE 0 END) AS shares_sell,
                        SUM(CASE WHEN side='buy'  THEN size_usdc ELSE 0 END) AS usdc_buy,
                        SUM(CASE WHEN side='sell' THEN size_usdc ELSE 0 END) AS usdc_sell,
                        SUM(CASE WHEN side='buy'  THEN 1 ELSE 0 END) AS buy_count,
                        SUM(CASE WHEN side='sell' THEN 1 ELSE 0 END) AS sell_count,
                        MIN(fill_timestamp) AS first_trade_at,
                        MAX(fill_timestamp) AS last_trade_at
                    FROM advisory_chain_fills
                    GROUP BY token_id
                ),
                latest AS (
                    SELECT DISTINCT ON (s.token_id)
                        s.token_id, s.id AS snapshot_id, s.generated_at,
                        s.market_slug, s.condition_id,
                        s.fair_value_for_edge, s.edge_buy_active,
                        s.halt_reason, s.view_payload
                    FROM market_view_snapshots s
                    ORDER BY s.token_id, s.id DESC
                )
                SELECT
                    a.token_id,
                    a.shares_buy, a.shares_sell, a.usdc_buy, a.usdc_sell,
                    a.buy_count, a.sell_count,
                    a.first_trade_at, a.last_trade_at,
                    l.snapshot_id, l.generated_at, l.market_slug, l.condition_id,
                    l.fair_value_for_edge, l.edge_buy_active, l.halt_reason,
                    l.view_payload
                FROM agg a
                LEFT JOIN latest l ON l.token_id = a.token_id
                ORDER BY (a.usdc_buy - a.usdc_sell) DESC
                """,
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.exception("portfolio aggregation failed")
        return jsonify({"error": str(exc)}), 500

    positions = []
    total_invested = 0.0
    total_mark = 0.0
    total_pnl = 0.0
    open_count = 0

    for r in rows:
        (token_id, sh_buy, sh_sell, u_buy, u_sell, n_buy, n_sell,
         first_at, last_at, snap_id, snap_at, slug, cid,
         fair, edge_buy, halt, vp) = r

        shares = float((sh_buy or 0) - (sh_sell or 0))
        net_invested = float((u_buy or 0) - (u_sell or 0))
        avg_entry = (net_invested / shares) if shares > 1e-9 else None

        # view_payload JSONB 取 best_bid/best_ask
        best_bid = best_ask = None
        if isinstance(vp, dict):
            best_bid = vp.get("best_bid")
            best_ask = vp.get("best_ask")
        elif isinstance(vp, str):
            try:
                import json as _json
                _vp = _json.loads(vp)
                best_bid = _vp.get("best_bid")
                best_ask = _vp.get("best_ask")
            except Exception:
                pass

        mark_value = unrealized = None
        if shares > 1e-9 and best_bid is not None:
            mark_value = shares * float(best_bid)
            unrealized = mark_value - net_invested
            total_mark += mark_value
            total_pnl += unrealized
            open_count += 1
        if shares > 1e-9:
            total_invested += net_invested

        positions.append({
            "token_id": token_id,
            "market_slug": slug,
            "condition_id": cid,
            "shares": shares,
            "avg_entry_price": avg_entry,
            "net_invested_usdc": net_invested,
            "buy_count": int(n_buy or 0),
            "sell_count": int(n_sell or 0),
            "first_trade_at": first_at.astimezone(timezone.utc).isoformat()
                if isinstance(first_at, datetime) else None,
            "last_trade_at": last_at.astimezone(timezone.utc).isoformat()
                if isinstance(last_at, datetime) else None,
            "latest_snapshot_id": snap_id,
            "latest_snapshot_at": snap_at.astimezone(timezone.utc).isoformat()
                if isinstance(snap_at, datetime) else None,
            "fair_value_for_edge": float(fair) if fair is not None else None,
            "best_bid": float(best_bid) if best_bid is not None else None,
            "best_ask": float(best_ask) if best_ask is not None else None,
            "edge_buy_active": bool(edge_buy) if edge_buy is not None else None,
            "halt_reason": halt,
            "mark_value_usdc": mark_value,
            "unrealized_pnl_usdc": unrealized,
        })

    return jsonify({
        "as_of": datetime.now(timezone.utc).isoformat(),
        "positions": positions,
        "totals": {
            "open_position_count": open_count,
            "net_invested_usdc": total_invested,
            "mark_value_usdc": total_mark,
            "unrealized_pnl_usdc": total_pnl,
        },
    })


# ---------------------------------------------------------------------------
#  User thesis (P2) — 自由文本判断, 写入 BatchInputs / inputs_hash
# ---------------------------------------------------------------------------

@advisory_bp.route("/api/advisory/user_thesis", methods=["GET"])
def api_user_thesis_get():
    try:
        from services.advisory.user_thesis import get_active_thesis
        active = get_active_thesis()
    except Exception as exc:
        logger.exception("get_active_thesis failed")
        return jsonify({"error": str(exc)}), 500
    if active is None:
        return jsonify({"active": None})
    return jsonify({"active": active.to_dict()})


@advisory_bp.route("/api/advisory/user_thesis", methods=["POST"])
def api_user_thesis_set():
    payload = request.get_json(silent=True) or {}
    text = payload.get("thesis_text")
    ttl = payload.get("ttl_hours", 6)
    try:
        from services.advisory.user_thesis import set_thesis
        active = set_thesis(text, ttl_hours=ttl)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        logger.exception("set_thesis failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"active": active.to_dict()}), 201


@advisory_bp.route("/api/advisory/user_thesis", methods=["DELETE"])
def api_user_thesis_clear():
    try:
        from services.advisory.user_thesis import clear_active_thesis
        n = clear_active_thesis()
    except Exception as exc:
        logger.exception("clear_active_thesis failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify({"cleared": n})


# ---------------------------------------------------------------------------
#  Calibration history (P3) — 由 systemd timer 每 6h 写入 advisory_calibration_runs
# ---------------------------------------------------------------------------

@advisory_bp.route("/api/advisory/calibration", methods=["GET"])
def api_calibration_runs():
    try:
        limit = int(request.args.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    limit = max(1, min(limit, 200))
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, run_at, since_utc, n_snapshots, brier,
                       n_trades, n_trades_settled, total_pnl_usdc,
                       calibration_json, trades_json
                FROM advisory_calibration_runs
                ORDER BY run_at DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
    except Exception as exc:
        logger.exception("calibration runs query failed")
        return jsonify({"error": str(exc)}), 500
    runs = [{
        "id": int(r[0]),
        "run_at": r[1].astimezone(timezone.utc).isoformat() if r[1] else None,
        "since_utc": r[2].astimezone(timezone.utc).isoformat() if r[2] else None,
        "n_snapshots": int(r[3] or 0),
        "brier": float(r[4]) if r[4] is not None else None,
        "n_trades": int(r[5] or 0),
        "n_trades_settled": int(r[6] or 0),
        "total_pnl_usdc": float(r[7]) if r[7] is not None else None,
        "calibration": r[8],
        "trades": r[9],
    } for r in rows]
    return jsonify({"runs": runs, "count": len(runs)})


# ---------------------------------------------------------------------------
#  Reconcile (P4) — manual_trades vs on-chain activity diff
# ---------------------------------------------------------------------------

@advisory_bp.route("/api/advisory/reconcile", methods=["GET"])
def api_reconcile():
    try:
        hours = float(request.args.get("hours", 24))
    except (TypeError, ValueError):
        hours = 24.0
    hours = max(0.5, min(hours, 168.0))
    profile = request.args.get("profile", "analyze")
    if profile not in ("analyze", "trade"):
        return jsonify({"error": "profile must be analyze or trade"}), 400
    try:
        from services.advisory.reconcile import reconcile
        rep = reconcile(hours=hours, profile=profile).to_dict()
    except Exception as exc:
        logger.exception("reconcile failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(rep)
