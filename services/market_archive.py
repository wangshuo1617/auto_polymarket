"""月度市场归档服务。

两张表 + 三个核心函数:

1. `market_position_snapshots` — 每日定期把当前 Polymarket 持仓 (含 curPrice=0 的)
   写一行,用作归档时回溯成本/持仓的兜底数据源。

2. `monthly_market_archive` — 已结算市场的最终归档:每个 (market_id, token_id) 一行,
   含成交量、加权成本、最终结算价、实现盈亏。归档来源是 Polymarket
   `data-api.polymarket.com/activity` 的 buy/sell 流水,精确重建 cost basis。

公开函数:
- `snapshot_positions(positions)` —  daily 持仓快照
- `find_archivable_markets()` — 列出"我们曾持仓 + 当前已结算 + 未归档"的市场
- `archive_market(market_id)` — 用 activity_history 完整重建该 market 的归档行
- `run_archive_cycle()` — 一次性跑完: snapshot → 检测 → 归档,适合定时任务调用
"""
from __future__ import annotations

import logging
from typing import Any, Iterable

from data.database import get_conn, get_cursor
from data import polymarket as _pm

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS market_position_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_date   DATE        NOT NULL,
    market_id       TEXT        NOT NULL,
    condition_id    TEXT,
    token_id        TEXT        NOT NULL,
    outcome         TEXT,
    market_question TEXT,
    event_slug      TEXT,
    shares          DOUBLE PRECISION,
    avg_price       DOUBLE PRECISION,
    cur_price       DOUBLE PRECISION,
    market_value    DOUBLE PRECISION,
    end_date        TIMESTAMPTZ,
    closed          BOOLEAN,
    raw_payload     JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (snapshot_date, market_id, token_id)
);

CREATE INDEX IF NOT EXISTS idx_mps_market_id ON market_position_snapshots(market_id);
CREATE INDEX IF NOT EXISTS idx_mps_snapshot_date ON market_position_snapshots(snapshot_date DESC);

CREATE TABLE IF NOT EXISTS monthly_market_archive (
    id                  BIGSERIAL PRIMARY KEY,
    market_id           TEXT        NOT NULL,
    token_id            TEXT        NOT NULL,
    condition_id        TEXT,
    market_question     TEXT,
    outcome             TEXT,
    event_slug          TEXT,
    month_label         TEXT,
    resolved_at         TIMESTAMPTZ,
    end_date            TIMESTAMPTZ,
    won                 BOOLEAN,
    final_outcome_value DOUBLE PRECISION,  -- 0 or 1 per share
    -- 重建自 activity history:
    buy_shares          DOUBLE PRECISION DEFAULT 0,
    sell_shares         DOUBLE PRECISION DEFAULT 0,
    buy_usdc            DOUBLE PRECISION DEFAULT 0,  -- 实付 USDC (正)
    sell_usdc           DOUBLE PRECISION DEFAULT 0,  -- 实收 USDC (正)
    avg_entry_price     DOUBLE PRECISION,
    avg_exit_price      DOUBLE PRECISION,
    leftover_shares     DOUBLE PRECISION,
    -- realized_pnl = sell_usdc - buy_usdc + leftover_shares * final_outcome_value
    realized_pnl        DOUBLE PRECISION,
    trade_count         INTEGER,
    activity_payload    JSONB,
    archived_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (market_id, token_id)
);

CREATE INDEX IF NOT EXISTS idx_mma_month_label ON monthly_market_archive(month_label);
CREATE INDEX IF NOT EXISTS idx_mma_resolved_at ON monthly_market_archive(resolved_at DESC);
"""

_initialized = False


def ensure_tables() -> None:
    global _initialized
    if _initialized:
        return
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(_DDL)
    _initialized = True


def _safe_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def snapshot_positions(positions: Iterable[dict]) -> int:
    """把当前持仓写入 daily snapshot 表 (UPSERT 到 (snapshot_date, market_id, token_id))。

    positions 接受 polymarket data-api /positions 原始 row (含 curPrice/avgPrice/size 等)。
    建议传入**未经 curPrice=0 过滤**的版本,以便归档损失档。
    """
    ensure_tables()
    rows = list(positions or [])
    if not rows:
        return 0
    import json
    inserted = 0
    with get_conn() as conn, conn.cursor() as cur:
        for p in rows:
            market_id = str(p.get("conditionId") or p.get("market") or p.get("market_id") or "")
            token_id = str(p.get("asset") or p.get("token_id") or "")
            if not market_id or not token_id:
                continue
            cur.execute(
                """
                INSERT INTO market_position_snapshots
                  (snapshot_date, market_id, condition_id, token_id, outcome,
                   market_question, event_slug, shares, avg_price, cur_price,
                   market_value, end_date, closed, raw_payload)
                VALUES (CURRENT_DATE, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (snapshot_date, market_id, token_id) DO UPDATE SET
                  outcome = EXCLUDED.outcome,
                  market_question = EXCLUDED.market_question,
                  event_slug = EXCLUDED.event_slug,
                  shares = EXCLUDED.shares,
                  avg_price = EXCLUDED.avg_price,
                  cur_price = EXCLUDED.cur_price,
                  market_value = EXCLUDED.market_value,
                  end_date = EXCLUDED.end_date,
                  closed = EXCLUDED.closed,
                  raw_payload = EXCLUDED.raw_payload
                """,
                (
                    market_id,
                    p.get("conditionId"),
                    token_id,
                    p.get("outcome"),
                    p.get("title") or p.get("question"),
                    p.get("eventSlug") or p.get("slug"),
                    _safe_float(p.get("size")),
                    _safe_float(p.get("avgPrice")),
                    _safe_float(p.get("curPrice")),
                    _safe_float(p.get("currentValue") or p.get("value")),
                    p.get("endDate"),
                    p.get("closed"),
                    json.dumps(p, default=str),
                ),
            )
            inserted += 1
    logger.info("market_archive snapshot: %d rows", inserted)
    return inserted


def find_archivable_markets(*, max_age_days: int = 60) -> list[dict]:
    """找出"我们曾在最近 max_age_days 持仓 + 还未归档"的 market_id 列表。

    后续每个 market_id 调用 archive_market() 处理。
    """
    ensure_tables()
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT s.market_id, s.market_question, s.event_slug
            FROM market_position_snapshots s
            LEFT JOIN monthly_market_archive a ON a.market_id = s.market_id
            WHERE s.snapshot_date >= CURRENT_DATE - make_interval(days => %s)
              AND a.market_id IS NULL
            ORDER BY s.market_id
            """,
            (max_age_days,),
        )
        return [dict(r) for r in cur.fetchall()]


def _fetch_market_resolution(market_id: str) -> dict | None:
    """用 clob_client.get_market 取 closed + 各 outcome 价格。

    Polymarket 已结算的 market: closed=True, 且各 token 的 'winner' / 'price'
    字段标记最终值 (1 or 0)。
    """
    try:
        clob = _pm.get_client()
        meta = clob.get_market(market_id)
        return meta if isinstance(meta, dict) else None
    except Exception:  # noqa: BLE001
        logger.exception("fetch_market_resolution failed: market_id=%s", market_id)
        return None


def _resolve_token_final_value(market_meta: dict, token_id: str) -> float | None:
    """从 market metadata 抽出 token_id 对应的结算价 (0 / 1)。

    polymarket get_market 返回的 tokens 列表里, 每个 token 有 'winner' (bool) 字段。
    赢的一方 final_value=1, 输的一方 final_value=0。市场未结算返回 None。
    """
    tokens = market_meta.get("tokens") or []
    for t in tokens:
        if str(t.get("token_id") or "") == str(token_id):
            if t.get("winner") is True:
                return 1.0
            if t.get("winner") is False:
                return 0.0
            return None
    return None


def archive_market(market_id: str) -> dict | None:
    """对一个 market_id 抓 activity history 重建 cost/收益, 写入归档表。

    返回 None 表示市场尚未结算或没有交易记录, 跳过。
    """
    ensure_tables()
    meta = _fetch_market_resolution(market_id)
    if not meta:
        return None
    if not (meta.get("closed") or meta.get("archived")):
        return None

    activities = _pm.get_activity_history(market_id) or []
    if not activities:
        logger.info("archive_market: market=%s 无 activity 记录, 跳过", market_id)
        return None

    # 按 token_id 聚合 buy/sell
    import json
    per_token: dict[str, dict[str, Any]] = {}
    for ev in activities:
        side = (ev.get("side") or "").upper()  # BUY / SELL
        if side not in ("BUY", "SELL"):
            continue
        token_id = str(ev.get("asset") or "")
        if not token_id:
            continue
        shares = _safe_float(ev.get("size")) or 0.0
        price = _safe_float(ev.get("price")) or 0.0
        usdc = shares * price
        bucket = per_token.setdefault(token_id, {
            "buy_shares": 0.0, "sell_shares": 0.0,
            "buy_usdc": 0.0, "sell_usdc": 0.0,
            "outcome": ev.get("outcome"),
            "title": ev.get("title") or ev.get("eventSlug"),
            "event_slug": ev.get("eventSlug") or ev.get("slug"),
            "trade_count": 0,
        })
        if side == "BUY":
            bucket["buy_shares"] += shares
            bucket["buy_usdc"] += usdc
        else:
            bucket["sell_shares"] += shares
            bucket["sell_usdc"] += usdc
        bucket["trade_count"] += 1

    end_date = meta.get("end_date_iso") or meta.get("end_date")
    month_label = None
    if end_date:
        try:
            month_label = str(end_date)[:7]  # YYYY-MM
        except Exception:  # noqa: BLE001
            month_label = None

    written = 0
    summary = {"market_id": market_id, "tokens": []}
    with get_conn() as conn, conn.cursor() as cur:
        for token_id, b in per_token.items():
            final_value = _resolve_token_final_value(meta, token_id)
            leftover = b["buy_shares"] - b["sell_shares"]
            avg_entry = (b["buy_usdc"] / b["buy_shares"]) if b["buy_shares"] else None
            avg_exit = (b["sell_usdc"] / b["sell_shares"]) if b["sell_shares"] else None
            realized = b["sell_usdc"] - b["buy_usdc"] + (leftover * (final_value or 0.0))
            won = final_value is not None and final_value >= 0.5

            cur.execute(
                """
                INSERT INTO monthly_market_archive
                  (market_id, token_id, condition_id, market_question, outcome, event_slug,
                   month_label, resolved_at, end_date, won, final_outcome_value,
                   buy_shares, sell_shares, buy_usdc, sell_usdc,
                   avg_entry_price, avg_exit_price, leftover_shares, realized_pnl,
                   trade_count, activity_payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), %s, %s, %s,
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (market_id, token_id) DO UPDATE SET
                  market_question = EXCLUDED.market_question,
                  outcome = EXCLUDED.outcome,
                  event_slug = EXCLUDED.event_slug,
                  month_label = EXCLUDED.month_label,
                  resolved_at = EXCLUDED.resolved_at,
                  end_date = EXCLUDED.end_date,
                  won = EXCLUDED.won,
                  final_outcome_value = EXCLUDED.final_outcome_value,
                  buy_shares = EXCLUDED.buy_shares,
                  sell_shares = EXCLUDED.sell_shares,
                  buy_usdc = EXCLUDED.buy_usdc,
                  sell_usdc = EXCLUDED.sell_usdc,
                  avg_entry_price = EXCLUDED.avg_entry_price,
                  avg_exit_price = EXCLUDED.avg_exit_price,
                  leftover_shares = EXCLUDED.leftover_shares,
                  realized_pnl = EXCLUDED.realized_pnl,
                  trade_count = EXCLUDED.trade_count,
                  activity_payload = EXCLUDED.activity_payload,
                  archived_at = NOW()
                """,
                (
                    market_id, token_id, meta.get("condition_id"),
                    meta.get("question") or b.get("title"),
                    b.get("outcome"),
                    b.get("event_slug"),
                    month_label, end_date, won, final_value,
                    b["buy_shares"], b["sell_shares"],
                    b["buy_usdc"], b["sell_usdc"],
                    avg_entry, avg_exit, leftover, realized,
                    b["trade_count"],
                    json.dumps({"meta": meta, "events": activities}, default=str)[:5_000_000],
                ),
            )
            written += 1
            summary["tokens"].append({
                "token_id": token_id, "won": won, "realized_pnl": realized,
                "buy_shares": b["buy_shares"], "sell_shares": b["sell_shares"],
            })
    logger.info("archive_market: market=%s tokens=%d realized_total=%.3f",
                market_id, written, sum(t["realized_pnl"] or 0 for t in summary["tokens"]))
    return summary


def run_archive_cycle(positions: Iterable[dict] | None = None) -> dict:
    """一次完整循环: snapshot + 检测 + 归档。供 cron / position_analyze.py 收尾调用。

    positions: 当前持仓列表。若 None, 自动调 get_positions(); 但建议外部传入未过滤的版本。
    """
    ensure_tables()
    if positions is None:
        positions = _pm.get_positions()
    snap_count = snapshot_positions(positions)

    candidates = find_archivable_markets()
    archived = []
    for c in candidates:
        try:
            res = archive_market(c["market_id"])
            if res:
                archived.append(res)
        except Exception:  # noqa: BLE001
            logger.exception("archive_market 失败 market_id=%s", c.get("market_id"))

    return {
        "snapshot_count": snap_count,
        "candidate_count": len(candidates),
        "archived_count": len(archived),
        "archived": archived,
    }
