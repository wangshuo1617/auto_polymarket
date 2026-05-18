"""
Advisory dashboard blueprint (A5).

提供 advisory-only fair-value rebalancer 的 dashboard 接入:
- GET /recommendations            — 推荐表 HTML
- GET /api/advisory/recommendations — 最新 complete batch 的 token MarketView (JSON)
- GET /api/advisory/portfolio      — advisory_chain_fills 派生持仓 + 最新快照对照 (v2 C2)
- GET /api/advisory/user_thesis    — 当前 active 自由文本判断 (P2)
- POST /api/advisory/user_thesis   — 设置/替换 active 自由文本判断
- DELETE /api/advisory/user_thesis — 清除 active 自由文本判断

数据契约 (plan-advisory §5):
- 路由数据源严格走 `get_latest_complete_batch_views()`, 不读 market_view_latest
  (latest 是 per-token 累加, universe 收缩后会污染推荐)
- Staleness 全局兜底: latest_complete.batch_completed_at 距今超过 STALENESS_THRESHOLD
  → 整个推荐区块替换为 "数据陈旧" 横幅, 不展示任何 token 行
- v2 重构后, dashboard 下单写 advisory_intents (intent_writer), 链上事实由
  advisory_chain_fills (poller + worker 关联) 派生.
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

# 推荐数据 staleness 阈值 (advisory batch 每小时跑一次, 留 10min slack → 70min)
STALENESS_THRESHOLD = timedelta(minutes=70)


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


def _fetch_latest_ai_shadow() -> dict:
    """返回最近一次 source='ai' (任意 batch) 的 PathView shadow run 数据.
    不与当前 batch 绑定 —— AI 每 6h 跑一次, 非 AI batch 不应清空显示.
    结构: {"run": {...}, "by_token": {token_id: {fair_event, p_event_yes, fair_value_status, rationale_short}}}
    若无任何 AI run, 返回 {"run": None, "by_token": {}}.
    """
    out = {"run": None, "by_token": {}}
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT id, batch_id, generated_at, model_id, prompt_version,
                       validation_status, request_latency_ms, raw_payload
                FROM advisory_pathview_shadow_runs
                WHERE source = 'ai'
                ORDER BY generated_at DESC
                LIMIT 1
                """,
            )
            row = cur.fetchone()
            if not row:
                return out
            run_id, batch_id, gen_at, model_id, prompt_v, status, latency, payload = row
            if isinstance(payload, str):
                import json
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            payload = payload or {}
            out["run"] = {
                "id": run_id,
                "batch_id": batch_id,
                "generated_at": gen_at.astimezone(timezone.utc).isoformat() if isinstance(gen_at, datetime) else None,
                "model_id": model_id,
                "prompt_version": prompt_v,
                "validation_status": status,
                "latency_ms": latency,
                "summary": payload.get("market_view_summary"),
                "ai_notes": payload.get("ai_notes"),
                "key_levels": payload.get("key_levels"),
            }

            cur.execute(
                """
                SELECT token_id, fair_event, p_event_yes, fair_value_status, components
                FROM advisory_pathview_shadow_views
                WHERE run_id = %s
                """,
                (run_id,),
            )
            for tk, fair, p_yes, fv_status, comp in cur.fetchall():
                if isinstance(comp, str):
                    import json
                    try:
                        comp = json.loads(comp)
                    except Exception:
                        comp = {}
                comp = comp or {}
                out["by_token"][tk] = {
                    "fair_event": fair,
                    "p_event_yes": p_yes,
                    "fair_value_status": fv_status,
                    "rationale_short": comp.get("rationale_short"),
                }
    except Exception:
        logger.exception("_fetch_latest_ai_shadow failed")
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
    snapshots_out = []
    ai_shadow = {"run": None, "by_token": {}}
    if not is_stale:
        ai_shadow = _fetch_latest_ai_shadow()
        ai_by_token = ai_shadow.get("by_token") or {}
        for s in snapshots:
            row = _serialize_snapshot(s)
            ai_row = ai_by_token.get(row.get("token_id"))
            row["ai_shadow"] = ai_row  # None 表示当前 token 不在 AI focus 子集
            snapshots_out.append(row)

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
        "ai_shadow_run": ai_shadow.get("run"),
        "snapshots": snapshots_out,
    })


# v2 D1: POST/GET /api/advisory/manual_trades 已移除. 写入 → intent_writer
# (透过 /api/buy /api/sell /api/cancel 自动捕获); 读取 → advisory_intents 视图.


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
            "outcome_index": (vp.get("outcome_index") if isinstance(vp, dict) else None),
            "strike_usd": (vp.get("strike_usd") if isinstance(vp, dict) else None),
            "side_above": (vp.get("side_above") if isinstance(vp, dict) else None),
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
#  Manual batch trigger — 立即跑一次 advisory_batch_runner --once
# ---------------------------------------------------------------------------

@advisory_bp.route("/api/advisory/run_batch_now", methods=["POST"])
def api_run_batch_now():
    """Spawn a single advisory batch iteration synchronously and return new batch_id.

    Reads ADVISORY_BATCH_MAX_STRIKES env (matching the systemd service config).
    Blocks up to 60s waiting for the subprocess; returns the latest batch_id +
    completion_at + duration so the frontend can decide whether to refresh.
    """
    import os
    import subprocess
    import time

    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    max_strikes = int(os.environ.get("ADVISORY_BATCH_MAX_STRIKES", "12"))

    # snapshot current latest batch_id to detect a new one was written
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(id) FROM market_view_batches WHERE status='complete'")
        prev_id = cur.fetchone()[0] or 0

    cmd = [
        "uv", "run", "scripts/advisory_batch_runner.py",
        "--once", "--max-strikes", str(max_strikes),
    ]
    env = os.environ.copy()
    env.setdefault("LD_PRELOAD", "")
    env.setdefault("POLYMARKET_PROFILE", "analyze")

    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=project_root, env=env,
            capture_output=True, text=True, timeout=60.0,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "batch runner timed out (>60s)"}), 504
    elapsed = time.monotonic() - t0

    if proc.returncode != 0:
        logger.error("manual run_batch_now failed rc=%d stderr=%s",
                     proc.returncode, proc.stderr[-2000:])
        return jsonify({
            "error": "batch runner exited non-zero",
            "returncode": proc.returncode,
            "stderr_tail": proc.stderr[-1000:],
        }), 500

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("""SELECT id, status, batch_completed_at, token_count
                       FROM market_view_batches
                       WHERE status='complete'
                       ORDER BY id DESC LIMIT 1""")
        row = cur.fetchone()
    if not row or row[0] <= prev_id:
        return jsonify({
            "error": "no new complete batch written",
            "previous_batch_id": prev_id,
            "stdout_tail": proc.stdout[-1000:],
        }), 500

    return jsonify({
        "batch_id": row[0],
        "status": row[1],
        "batch_completed_at": row[2].isoformat() if row[2] else None,
        "token_count": row[3],
        "previous_batch_id": prev_id,
        "elapsed_seconds": round(elapsed, 2),
    })


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


# v2 D1: GET /api/advisory/reconcile (manual_trades vs activity API) 已移除.
# 由 /api/advisory/reconcile_v2 取代.


# ---------------------------------------------------------------------------
#  Reconcile v2 (C3) — advisory_intents ⇄ advisory_chain_fills, 6 类视图
# ---------------------------------------------------------------------------
#  与 v1 的差异:
#   - 数据源: intents (决策) + chain_fills (链上事实) + worker 维护的关联,
#     不再从 manual_trades+activity API 当场撮合;
#   - 输出 6 个独立 tab: matched / partial / orphan /
#     cancelled_clean / cancelled_with_fills / chain_fills_no_intent;
#   - 每条 row 自带 fills[] 详情和 slippage_cents (C4 用), 便于前端展开.

@advisory_bp.route("/api/advisory/reconcile_v2", methods=["GET"])
def api_reconcile_v2():
    try:
        hours = int(float(request.args.get("hours", 72)))
    except (TypeError, ValueError):
        hours = 72
    hours = max(1, min(hours, 720))
    try:
        limit = int(request.args.get("limit", 200))
    except (TypeError, ValueError):
        limit = 200
    limit = max(1, min(limit, 1000))
    try:
        from services.advisory.reconcile_v2 import reconcile_v2
        rep = reconcile_v2(hours=hours, limit_per_class=limit).to_dict()
    except Exception as exc:
        logger.exception("reconcile_v2 failed")
        return jsonify({"error": str(exc)}), 500
    return jsonify(rep)
