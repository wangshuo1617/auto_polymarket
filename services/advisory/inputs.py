"""
Advisory batch input assembly (R1 dependency).

Pure functions that fetch real-data inputs needed by
`services.advisory.computer.run_advisory_batch`. Extracted from V1 driver
(`scripts/advisory_run_batch_e2e.py`) so the long-running batch runner
(`scripts/advisory_batch_runner.py`) can reuse the same logic.

Public API:
    BatchInputs (dataclass)
    assemble_batch_inputs(slug, max_strikes, btc_now=None) -> BatchInputs
    fetch_descriptors(slug, max_strikes) -> list[ConditionDescriptor]
    parse_slug_to_month_end(slug) -> datetime
    select_current_month_slug() -> str
    select_active_month_slug(now=None, lookahead_months=2) -> str
"""

from __future__ import annotations

import calendar
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from data.binance import get_btc_price, get_1d_klines_data
from data.polymarket import get_event_token_id, get_best_prices
from services.volatility import build_daily_volatility_profile
from services.profit_optimizer import _extract_strike_and_direction
from services.advisory.computer import TokenQuote, TokenPosition
from services.advisory.market_state_adapter import TokenContext, BtcTickState
from services.advisory.settlement_adapter import ConditionDescriptor
from services.advisory.path_metrics import (
    parse_slug_to_month_window,
    compute_path_extrema_with_fallback,
    klines_to_log_returns,
    compute_drift_daily,
    compute_ewma_sigma,
)

logger = logging.getLogger(__name__)


@dataclass
class BatchInputs:
    slug: str
    btc_price: float
    days_left: float
    sigma_daily: float
    sigma_source: str
    drift_daily: float
    drift_source: str
    path_max_btc: float
    path_min_btc: float
    path_source: str
    universe: list[TokenContext]
    descriptors: list[ConditionDescriptor]
    quotes: dict[str, TokenQuote]
    positions: dict[str, TokenPosition] = field(default_factory=dict)
    btc_tick: Optional[BtcTickState] = None
    as_of_utc: Optional[datetime] = None
    user_thesis_text: Optional[str] = None
    user_thesis_id: Optional[int] = None


def select_current_month_slug() -> str:
    return f"what-price-will-bitcoin-hit-in-{datetime.now(timezone.utc).strftime('%B-%Y').lower()}"


def _slug_for_month(year: int, month: int) -> str:
    month_name = calendar.month_name[month].lower()
    return f"what-price-will-bitcoin-hit-in-{month_name}-{year}"


def _advance_month(year: int, month: int, delta: int) -> tuple[int, int]:
    idx = (month - 1) + delta
    return year + idx // 12, (idx % 12) + 1


def _slug_has_live_markets(slug: str) -> bool:
    """Probe gamma: True iff event resolves and has at least one market.

    Any exception (404, network, parse) → False so caller can advance month.
    Note: a slug with markets that have all already settled still returns True
    here; settlement_adapter handles those rows correctly. We only skip slugs
    that gamma cannot resolve at all (e.g., next month not yet listed).
    """
    try:
        event = get_event_token_id(slug)
    except Exception as exc:
        logger.info("slug probe failed slug=%s err=%s", slug, exc)
        return False
    markets = event.get("markets") or []
    if not markets:
        logger.info("slug probe: %s has zero markets", slug)
        return False
    return True


def select_active_month_slug(now: Optional[datetime] = None,
                             lookahead_months: int = 2) -> str:
    """Pick the slug for the active BTC month-end event with month-rollover handoff.

    Strategy:
    1. Start from `now`'s calendar month (UTC).
    2. If month-end has already passed (rare — `now` is past the 23:59 of last
       day), advance to next month immediately.
    3. Probe gamma: if event resolves with ≥1 market → use it.
    4. Otherwise advance 1 month and re-probe; cap at `lookahead_months`
       advances (default 2 = current/next/next+1).
    5. If nothing works, fall back to current-month slug (downstream will
       surface a clear error rather than silently picking a stale slug).
    """
    now = now or datetime.now(timezone.utc)
    year, month = now.year, now.month

    current_end = parse_slug_to_month_end(_slug_for_month(year, month))
    if current_end is not None and now > current_end:
        year, month = _advance_month(year, month, 1)

    for offset in range(lookahead_months + 1):
        y, m = _advance_month(year, month, offset)
        slug = _slug_for_month(y, m)
        if _slug_has_live_markets(slug):
            if offset > 0:
                logger.info("active month slug rolled forward by %d month(s) -> %s", offset, slug)
            return slug

    fallback = _slug_for_month(year, month)
    logger.warning("no live slug found within +%d months; falling back to %s",
                   lookahead_months, fallback)
    return fallback


def parse_slug_to_month_end(slug: str) -> Optional[datetime]:
    m = re.search(r"in-([a-z]+)-(\d{4})$", slug)
    if not m:
        return None
    month_name, year = m.group(1), int(m.group(2))
    month_map = {n.lower(): i for i, n in enumerate(calendar.month_name) if n}
    month = month_map.get(month_name)
    if not month:
        return None
    last_day = calendar.monthrange(year, month)[1]
    return datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)


def _fetch_universe(slug: str, max_strikes: int, btc_now: float
                    ) -> tuple[list[TokenContext], list[ConditionDescriptor], list[str]]:
    event = get_event_token_id(slug)
    descriptors_dict: dict[str, ConditionDescriptor] = {}
    universe: list[TokenContext] = []
    token_ids: list[str] = []

    parsed = []
    for m in event.get("markets", []):
        strike, direction = _extract_strike_and_direction(m.get("question") or "")
        if strike is None:
            continue
        parsed.append({**m, "_strike": strike, "_direction": direction})

    parsed.sort(key=lambda x: abs(x["_strike"] - btc_now))
    parsed = parsed[:max_strikes]

    for m in parsed:
        cond_id = m["market_id"]
        outcomes = m.get("outcomes") or []
        token_id_pair = m.get("token_id") or []
        if len(token_id_pair) != 2 or len(outcomes) != 2:
            logger.warning("skip market %s outcomes=%s tokens=%s", cond_id, outcomes, token_id_pair)
            continue

        descriptors_dict[cond_id] = ConditionDescriptor(
            condition_id=cond_id,
            market_slug=slug,
            clob_token_ids=tuple(str(t) for t in token_id_pair),
        )

        for idx, (outcome, tok) in enumerate(zip(outcomes, token_id_pair)):
            tid = str(tok)
            side_above = (m["_direction"] == "above") if outcome.lower() == "yes" else \
                         (m["_direction"] != "above")
            universe.append(TokenContext(
                token_id=tid,
                condition_id=cond_id,
                market_slug=slug,
                outcome_index=idx,
                strike_usd=float(m["_strike"]),
                side_above=side_above,
            ))
            token_ids.append(tid)

    return universe, list(descriptors_dict.values()), token_ids


def fetch_descriptors(slug: str, max_strikes: int = 6,
                      btc_now: Optional[float] = None) -> list[ConditionDescriptor]:
    """Lightweight helper for settlement_refresher: fetch descriptors only.

    Reuses the same nearest-strike selection as the batch runner so refresh
    coverage matches what the batch will actually consume.
    """
    if btc_now is None:
        btc_now = float(get_btc_price() or 0.0)
        if btc_now <= 0:
            raise RuntimeError("Failed to fetch BTC price for descriptor selection")
    _, descriptors, _ = _fetch_universe(slug, max_strikes, btc_now)
    return descriptors


def _fetch_quotes(token_ids: list[str]) -> dict[str, TokenQuote]:
    raw = get_best_prices(token_ids, profile="analyze")
    return {tid: TokenQuote(best_bid=v.get("best_bid"), best_ask=v.get("best_ask"))
            for tid, v in raw.items()}


def _fetch_positions_from_chain_fills(
    token_ids: list[str],
    quotes: dict[str, TokenQuote],
) -> dict[str, TokenPosition]:
    """聚合 advisory_chain_fills 的净 shares, 用 best_bid 估值得到 current_usdc.

    - 只覆盖当前 universe 中的 token_id (历史持仓不污染本批次)
    - shares ≤ 1e-9 视为已清仓, 不写入 (保持与 portfolio API 一致)
    - 缺 best_bid 时用 0.5 兜底, 防止整列消失 (会标注 quote_missing)
    """
    if not token_ids:
        return {}
    from data.database import get_conn  # 避免顶层循环导入
    out: dict[str, TokenPosition] = {}
    try:
        with get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT token_id,
                       SUM(CASE WHEN side='buy'  THEN size_shares ELSE 0 END) -
                       SUM(CASE WHEN side='sell' THEN size_shares ELSE 0 END) AS net_shares
                FROM advisory_chain_fills
                WHERE token_id = ANY(%s)
                GROUP BY token_id
                """,
                (list(token_ids),),
            )
            rows = cur.fetchall()
    except Exception:
        logger.exception("advisory_chain_fills aggregation failed; positions empty")
        return {}
    for tid, net in rows:
        shares = float(net or 0.0)
        if shares <= 1e-9:
            continue
        q = quotes.get(tid)
        bid = (q.best_bid if q else None)
        mark = bid if (bid is not None and bid > 0) else 0.5
        out[tid] = TokenPosition(current_usdc=shares * mark, target_usdc=None)
    logger.info("positions derived from chain_fills: %d (out of %d tokens)",
                len(out), len(token_ids))
    return out


def _estimate_volatility_and_drift() -> tuple[float, str, float, str, float, float, str]:
    """Return (sigma_daily, sigma_source, drift_daily, drift_source,
    fallback_high_7d, fallback_low_7d, fallback_source).

    σ source: EWMA (λ=0.94) over up to 60 daily log returns. Falls back to
    realized 30d profile / ATR if klines are unavailable.
    Drift: shrunk sample mean log return over up to 60 days.
    """
    klines = get_1d_klines_data(limit=60) or []
    profile = build_daily_volatility_profile(klines, atr_period=14)
    realized_pct = float(profile.get("realized_vol_daily_pct") or 0.0)

    returns = klines_to_log_returns(klines, n=60)
    sigma_ewma, ewma_src = compute_ewma_sigma(returns, lam=0.94)
    drift, drift_src = compute_drift_daily(returns, shrink_full_n=60)

    if sigma_ewma > 0:
        sigma_daily = sigma_ewma
        sigma_source = ewma_src
    elif realized_pct > 0:
        sigma_daily = realized_pct / 100.0
        sigma_source = f"realized_vol_30d({realized_pct}%)"
    else:
        atr_pct = float(profile.get("atr_pct") or 1.8)
        sigma_daily = max(0.008, atr_pct / 100.0 * 0.8)
        sigma_source = f"atr_fallback({atr_pct}%)"

    recent7 = klines[-7:] if len(klines) >= 7 else klines
    highs = [float(k[2]) for k in recent7]
    lows = [float(k[3]) for k in recent7]
    fb_high = max(highs) if highs else 0.0
    fb_low = min(lows) if lows else 0.0
    fb_src = f"7d_klines(n={len(recent7)})"
    return sigma_daily, sigma_source, drift, drift_src, fb_high, fb_low, fb_src


def assemble_batch_inputs(slug: str, max_strikes: int = 6,
                          btc_now: Optional[float] = None) -> BatchInputs:
    """全部真实数据采集 + 装配, 返回 BatchInputs.

    抛出 RuntimeError 如果 BTC 价无法获取或 universe 为空。
    """
    now_utc = datetime.now(timezone.utc)
    month_end = parse_slug_to_month_end(slug)
    if month_end is None:
        raise ValueError(f"cannot parse month-end from slug: {slug}")
    days_left = max(0.0, (month_end - now_utc).total_seconds() / 86400.0)

    if btc_now is None:
        btc_now = float(get_btc_price() or 0.0)
    if btc_now <= 0:
        raise RuntimeError("Binance returned 0 BTC price")

    sigma_daily, sigma_source, drift_daily, drift_source, \
        fb_high, fb_low, fb_src = _estimate_volatility_and_drift()

    month_window = parse_slug_to_month_window(slug)
    if month_window is None:
        path_max, path_min, path_source = fb_high, fb_low, fb_src
    else:
        m_start, m_end = month_window
        path_max, path_min, path_source = compute_path_extrema_with_fallback(
            m_start, m_end, fb_high, fb_low, fb_src,
        )

    universe, descriptors, token_ids = _fetch_universe(slug, max_strikes, btc_now)
    if not universe:
        raise RuntimeError(f"no tokens parsed from event slug={slug}")

    quotes = _fetch_quotes(token_ids)

    # 链上派生持仓 (advisory_chain_fills 净 shares × best_bid 估值)
    positions = _fetch_positions_from_chain_fills(token_ids, quotes)

    # P2: pull active user thesis (if any) so it propagates into BatchInputs
    # and inputs_hash. Best-effort: any failure → no thesis, batch continues.
    thesis_text: Optional[str] = None
    thesis_id: Optional[int] = None
    try:
        from services.advisory.user_thesis import get_active_thesis
        active = get_active_thesis()
        if active:
            thesis_text = active.thesis_text
            thesis_id = active.id
    except Exception:
        logger.exception("user_thesis lookup failed; proceeding without")

    return BatchInputs(
        slug=slug,
        btc_price=btc_now,
        days_left=days_left,
        sigma_daily=sigma_daily,
        sigma_source=sigma_source,
        drift_daily=drift_daily,
        drift_source=drift_source,
        path_max_btc=max(path_max, btc_now),
        path_min_btc=min(path_min, btc_now) if path_min > 0 else btc_now,
        path_source=path_source,
        universe=universe,
        descriptors=descriptors,
        quotes=quotes,
        positions=positions,
        btc_tick=BtcTickState(
            source="binance_avgPrice",
            version="advisory-runner-v1",
            latest_tick_ts_utc=now_utc,
        ),
        as_of_utc=now_utc,
        user_thesis_text=thesis_text,
        user_thesis_id=thesis_id,
    )
