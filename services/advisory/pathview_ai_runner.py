"""Phase B3 — AI PathView shadow runner (NEVER drives production).

Pulls current batch context (path_views + per-token universe + baseline GBM
fair) from PG, calls Gemini with PATHVIEW_AI_SCHEMA, validates the response,
and persists into advisory_pathview_shadow_runs (source='ai') +
advisory_pathview_shadow_views.

Default DISABLED via env. Set ADVISORY_PATHVIEW_AI_ENABLED=1 to flip on.
On any failure (timeout / validation_failed / quota), logs warning and
records a 'failed'/'fatal' shadow row — production GBM advisory + email
notification path is not affected.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from data.database import get_conn
from services.advisory.pathview_shadow import _persist_shadow_run
from services.advisory.pathview_validator import validate_pathview_payload

logger = logging.getLogger(__name__)


def _safe_call(label: str, fn, *args, default=None, **kwargs):
    """Best-effort context fetch — log & return default on failure so the
    AI run still proceeds with partial context."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        logger.warning("pathview_ai context fetch %s failed: %s", label, exc)
        return default


def _fetch_market_context() -> dict:
    """Pull the same BTC + sentiment context production analyze_market sees,
    plus a BTC spot L2 depth summary so AI can spot key support/resistance walls.

    NOTE: We deliberately do NOT pass Polymarket token orderbooks — those would
    anchor the AI to current market consensus and defeat the shadow comparison.
    """
    from data.binance import (
        get_4h_klines_data, get_1d_klines_data, get_btc_spot_depth_summary,
    )
    from services.volatility import build_daily_volatility_profile
    from services.market_sentiment import get_market_sentiment_and_funding

    btc_4h = _safe_call("btc_4h", get_4h_klines_data, limit=12, default=[]) or []
    btc_1d = _safe_call("btc_1d", get_1d_klines_data, limit=14, default=[]) or []
    dvp = _safe_call("dvol_profile", build_daily_volatility_profile, btc_1d, default={}) or {}
    sentiment = _safe_call("sentiment", get_market_sentiment_and_funding, default={}) or {}
    btc_depth = _safe_call("btc_depth", get_btc_spot_depth_summary, default={}) or {}
    return {
        "btc_4h_k_data": btc_4h,
        "btc_1d_k_data": btc_1d,
        "daily_volatility_profile": dvp,
        "market_sentiment_and_funding": sentiment,
        "btc_spot_depth_summary": btc_depth,
    }


def ai_enabled() -> bool:
    raw = os.environ.get("ADVISORY_PATHVIEW_AI_ENABLED", "0").strip()
    return raw not in ("", "0", "false", "no", "off")


def _ai_min_interval_hours() -> float:
    """两次 AI shadow run 之间的最小间隔 (小时)。默认 6h。"""
    try:
        return float(os.environ.get("ADVISORY_PATHVIEW_AI_MIN_INTERVAL_HOURS", "6"))
    except Exception:
        return 6.0


def _ai_focus_tokens() -> int:
    """每次 AI 调用只分析最接近现价的 N 个 *未触发* token, 默认 8。"""
    try:
        return max(1, int(os.environ.get("ADVISORY_PATHVIEW_AI_FOCUS_TOKENS", "8")))
    except Exception:
        return 8


def _last_ai_run_at(batch_id: int) -> Optional[datetime]:
    """返回上一次 AI shadow run 的 generated_at (任一状态, 含 failed)。"""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT max(generated_at)
            FROM advisory_pathview_shadow_runs
            WHERE source = 'ai'
            """,
        )
        row = cur.fetchone()
    if not row or row[0] is None:
        return None
    ts = row[0]
    if isinstance(ts, datetime) and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _should_run_ai_now() -> tuple[bool, str]:
    """节流: 距上次 AI 调用不足 min_interval 则跳过。"""
    min_h = _ai_min_interval_hours()
    if min_h <= 0:
        return True, "no_throttle"
    last = _last_ai_run_at(0)
    if last is None:
        return True, "first_run"
    age_h = (datetime.now(timezone.utc) - last).total_seconds() / 3600.0
    if age_h >= min_h:
        return True, f"age={age_h:.2f}h>={min_h}h"
    return False, f"throttled age={age_h:.2f}h<{min_h}h"


def _select_focus_tokens(tokens: list, spot: Optional[float], n: int) -> list:
    """挑选最接近现价的 n 个 *未触发* token (按 |strike - spot|).
    fair_value_status != 'available' 的 token (locked_event_occurred /
    locked_event_missed / settled) 已经触发或锁定, 不再进入 AI 分析子集."""
    if not tokens:
        return []
    pool = [t for t in tokens if (t.get("fair_value_status") or "available") == "available"]
    if not pool:
        return []
    if spot is None or spot <= 0 or n >= len(pool):
        return list(pool)

    def _dist(t):
        s = t.get("strike_usd")
        try:
            return abs(float(s) - float(spot))
        except Exception:
            return float("inf")

    return sorted(pool, key=_dist)[:n]



def _ai_model_version() -> str:
    return os.environ.get("ADVISORY_PATHVIEW_AI_MODEL_VERSION", "gemini_v1")


def _ai_prompt_version() -> str:
    return os.environ.get("ADVISORY_PATHVIEW_AI_PROMPT_VERSION", "b3_v6")


def _format_strike_short(strike: float | int | None) -> str:
    if strike is None:
        return "?"
    try:
        k = float(strike) / 1000.0
    except Exception:
        return "?"
    if k.is_integer():
        return f"{int(k)}k"
    txt = f"{k:.1f}".rstrip("0").rstrip(".")
    return f"{txt}k"


def _month_from_slug(slug: str | None) -> str:
    if not slug:
        return "?"
    import re
    m = re.search(r"-in-([a-z]+)(?:-([0-9]{4}))?", slug, re.I)
    if not m:
        return "?"
    mo = m.group(1).lower()
    yr = m.group(2)
    return f"{mo}{yr[-2:]}" if yr else mo


def _build_token_label(slug: str | None, vp: dict, fair_meta: dict) -> str:
    """与前端 formatTokenLabel 同口径: 'may26-↑85k-yes' / 'may26-↓75k-no'.

    side_above ⊕ outcome_index 决定 token 实际 pay-off 方向; outcome_index=1 (no)
    时 market 方向取反."""
    strike = vp.get("strike_usd") or fair_meta.get("strike_usd")
    side_above = vp.get("side_above")
    if side_above is None:
        side_above = fair_meta.get("side_above")
    oi = vp.get("outcome_index")
    if oi == 0:
        market_above = bool(side_above)
    elif oi == 1:
        market_above = not bool(side_above)
    else:
        market_above = bool(side_above)
    arrow = "↑" if market_above else "↓"
    yn = "yes" if oi == 0 else ("no" if oi == 1 else "?")
    month = _month_from_slug(slug)
    return f"{month}-{arrow}{_format_strike_short(strike)}-{yn}"


def _fetch_batch_context(batch_id: int) -> Optional[dict]:
    """Pull everything needed to build the AI prompt."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT b.as_of_utc, pv.current_btc_price, pv.sigma_daily,
                   pv.sigma_source, pv.drift_daily, pv.days_left,
                   pv.per_token_fair
            FROM market_view_batches b
            LEFT JOIN path_views pv ON pv.id = b.path_view_id
            WHERE b.id = %s
            """,
            (batch_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        as_of, spot, sigma, sigma_src, mu, days, per_token_fair = row

        cur.execute(
            """
            SELECT s.token_id, s.market_slug, s.condition_id,
                   s.view_payload
            FROM market_view_snapshots s
            WHERE s.batch_id = %s
            ORDER BY s.token_id
            """,
            (batch_id,),
        )
        snaps = cur.fetchall()

    tokens = []
    baseline_fair = {}
    fair_map = per_token_fair or {}
    for tok, slug, cid, vp in snaps:
        vp = vp or {}
        f = fair_map.get(tok, {})
        strike = vp.get("strike_usd") or f.get("strike_usd")
        side_above = vp.get("side_above") if vp.get("side_above") is not None else f.get("side_above")
        # market_direction follows side_above only — outcome_index (yes/no) is handled
        # by fair_event vs fair_non_event polarity in the AI output, not by direction grouping.
        market_dir = "above" if side_above else "below"
        tokens.append({
            "token_id": tok,
            "label": _build_token_label(slug, vp, f),
            "market_slug": slug,
            "strike_usd": strike,
            "side_above": side_above,
            "market_direction": market_dir,
            "fair_value_status": vp.get("fair_value_status") or "available",
        })
        if f.get("fair_calibrated") is not None:
            baseline_fair[tok] = float(f["fair_calibrated"])

    return {
        "as_of_utc": as_of.isoformat() if isinstance(as_of, datetime) else str(as_of),
        "current_btc_price": float(spot) if spot is not None else None,
        "sigma_daily": float(sigma) if sigma is not None else None,
        "sigma_source": sigma_src,
        "drift_daily": float(mu) if mu is not None else 0.0,
        "days_left": float(days) if days is not None else None,
        "tokens": tokens,
        "baseline_fair": baseline_fair,
    }


def _record_shadow_failure(
    batch_id: int, *, status: str, errors: list, notes: str,
    latency_ms: Optional[int] = None,
) -> int:
    """Persist a shadow row representing a failed AI call so B4 metrics
    can track AI availability without blowing up the batch."""
    from services.advisory.pathview_validator import ValidationResult
    res = ValidationResult(status=status, errors=errors, warnings=[])
    return _persist_shadow_run(
        batch_id, source="ai",
        payload={"per_token": [], "ai_call_failed": True},
        validation=res,
        model_id="gemini",
        model_version=_ai_model_version(),
        prompt_version=_ai_prompt_version(),
        request_latency_ms=latency_ms,
        notes=notes,
    )


def run_ai_pathview_for_batch(batch_id: int) -> Optional[int]:
    """Main B3 entry. Returns shadow_run_id or None when disabled/throttled."""
    if not ai_enabled():
        logger.debug("pathview_ai disabled (set ADVISORY_PATHVIEW_AI_ENABLED=1)")
        return None

    ok, reason = _should_run_ai_now()
    if not ok:
        logger.info("pathview_ai: skip batch=%s (%s)", batch_id, reason)
        return None
    logger.info("pathview_ai: proceed batch=%s (%s)", batch_id, reason)

    ctx = _fetch_batch_context(batch_id)
    if ctx is None:
        logger.warning("pathview_ai: batch %s not found", batch_id)
        return None

    focus_n = _ai_focus_tokens()
    spot = ctx.get("current_btc_price")
    focus_tokens = _select_focus_tokens(ctx["tokens"], spot, focus_n)
    focus_ids = {t["token_id"] for t in focus_tokens}
    focus_baseline = {k: v for k, v in ctx["baseline_fair"].items() if k in focus_ids}

    # Compact tid mapping (e.g. t1..t8) to slash 8 × 78-char token_ids in the
    # prompt. AI echoes back the short tid; we restore the full token_id
    # before validation/persistence so downstream code is unaffected.
    tid_to_token: dict[str, str] = {}
    token_to_tid: dict[str, str] = {}
    compact_focus_tokens: list[dict] = []
    for idx, tok in enumerate(focus_tokens, start=1):
        tid = f"t{idx}"
        tid_to_token[tid] = tok["token_id"]
        token_to_tid[tok["token_id"]] = tid
        new_tok = dict(tok)
        new_tok["token_id"] = tid
        compact_focus_tokens.append(new_tok)
    compact_focus_baseline = {
        token_to_tid[t]: v for t, v in focus_baseline.items() if t in token_to_tid
    }

    logger.info(
        "pathview_ai: focus %d/%d tokens (spot=%s)",
        len(focus_tokens), len(ctx["tokens"]), spot,
    )

    try:
        from ai.researcher import analyze_pathview_for_advisory
    except Exception as exc:
        logger.warning("pathview_ai: cannot import researcher: %s", exc)
        return _record_shadow_failure(
            batch_id, status="fatal",
            errors=[{"code": "import_failed", "detail": str(exc)}],
            notes="researcher_unavailable",
        )

    market_ctx = _fetch_market_context()

    panels = {
        "sigma_source": ctx.get("sigma_source"),
        "gbm_sigma_daily": ctx.get("sigma_daily"),
        "gbm_drift_daily": ctx.get("drift_daily"),
        "current_btc_price": ctx.get("current_btc_price"),
        "days_left": ctx.get("days_left"),
    }

    t0 = time.time()
    payload = None
    try:
        payload = analyze_pathview_for_advisory(
            batch_id=batch_id,
            batch_as_of_utc=ctx["as_of_utc"],
            current_btc_price=ctx["current_btc_price"] or 0.0,
            days_left=ctx["days_left"] or 0.0,
            gbm_sigma_daily=ctx["sigma_daily"] or 0.0,
            gbm_drift_daily=ctx["drift_daily"] or 0.0,
            sigma_source=ctx.get("sigma_source") or "unknown",
            btc_panels=panels,
            tokens=compact_focus_tokens,
            baseline_fair_by_token=compact_focus_baseline,
            market_context=market_ctx,
        )
    except Exception as exc:
        latency = int((time.time() - t0) * 1000)
        logger.warning("pathview_ai: API call failed batch=%s err=%s", batch_id, exc)
        return _record_shadow_failure(
            batch_id, status="failed",
            errors=[{"code": "api_call_failed", "detail": str(exc)[:500]}],
            notes="ai_api_exception", latency_ms=latency,
        )

    latency = int((time.time() - t0) * 1000)

    # Restore long token_ids: AI sees t1..tN, downstream expects full ids.
    for item in (payload.get("per_token") or []):
        short = item.get("token_id")
        if isinstance(short, str) and short in tid_to_token:
            item["token_id"] = tid_to_token[short]

    try:
        batch_as_of = datetime.fromisoformat(ctx["as_of_utc"].replace("Z", "+00:00"))
        if batch_as_of.tzinfo is None:
            batch_as_of = batch_as_of.replace(tzinfo=timezone.utc)
    except Exception:
        batch_as_of = datetime.now(timezone.utc)

    validation = validate_pathview_payload(
        payload,
        batch_as_of_utc=batch_as_of,
        baseline_fair_by_token=focus_baseline,
    )

    run_id = _persist_shadow_run(
        batch_id, source="ai",
        payload=payload, validation=validation,
        model_id="gemini",
        model_version=_ai_model_version(),
        prompt_version=_ai_prompt_version(),
        request_latency_ms=latency,
        notes=("validation_passed" if validation.status == "passed"
               else f"validation_{validation.status}"),
    )
    logger.info(
        "pathview_ai: batch=%s run_id=%s status=%s errors=%d warnings=%d "
        "latency=%dms n_tokens=%d",
        batch_id, run_id, validation.status,
        len(validation.errors), len(validation.warnings),
        latency, len(payload.get("per_token") or []),
    )
    return run_id
