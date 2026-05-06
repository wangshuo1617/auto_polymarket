"""PolymarketSettlementAdapter — refresh settlement feed from gamma API.

实现 plan-advisory.md §4.2 step 0a:
- 输入 condition universe (condition_id → market_slug + clob_token_ids)
- 调用 gamma /events/slug/<slug> 拉取 closed/outcomePrices/umaResolutionStatus
- 派生 settlement_state ∈ {pending, settled, disputed}
- 写入 settlement_feed_versions (一行) + settlement_feed_records (carry-forward: 每个
  tradable condition 都写一行, 即使值未变)
- bootstrap 守护: 若 condition_universe 非空但全部刷新失败 → refresh_status='failed'
- coverage 守护: 若部分 condition 取不到数据 → refresh_status='partial' + missing_set 落库
- 全部成功 → refresh_status='ok'

settlement_state 派生规则:
- gamma.umaResolutionStatus == 'disputed' → 'disputed'
- gamma.closed == True 且 outcomePrices 为二元 [0/1] → 'settled'
   winning_token_id = clob_token_ids[outcomePrices.index('1')]
- 其他 → 'pending'
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Iterable, Optional

import requests

from data.database import get_conn

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
SOURCE_NAME = "polymarket-gamma-events-slug"


@dataclass(frozen=True)
class ConditionDescriptor:
    condition_id: str
    market_slug: str
    clob_token_ids: tuple[str, ...]  # 顺序与 outcomes 对应


@dataclass
class SettlementRecord:
    condition_id: str
    market_slug: str
    settlement_state: str
    settlement_outcome_event_bool: Optional[bool]
    winning_token_id: Optional[str]
    final_price: Optional[float]
    settled_at_utc: Optional[str]
    raw_payload: dict


@dataclass
class RefreshResult:
    settlement_feed_version: int
    refresh_status: str
    refreshed_condition_ids: list[str]
    missing_condition_ids: list[str]
    rows_upserted: int
    effect_hash: str


def _parse_outcome_prices(raw) -> Optional[list[str]]:
    """Gamma returns outcomePrices as a JSON-encoded string list."""
    if isinstance(raw, list):
        return [str(x) for x in raw]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except json.JSONDecodeError:
            return None
    return None


def _derive_settlement(
    market_payload: dict, clob_token_ids: tuple[str, ...]
) -> tuple[str, Optional[bool], Optional[str], Optional[float]]:
    """根据 gamma market 字段派生 (settlement_state, outcome_bool, winning_token, final_price).

    final_price 暂存 gamma 给的 'Yes' 价格 (outcomePrices[0]) — settled 时为 0/1, 反映胜方。
    """
    uma_status = (market_payload.get("umaResolutionStatus") or "").strip().lower()
    if uma_status == "disputed":
        return ("disputed", None, None, None)

    closed = bool(market_payload.get("closed"))
    prices = _parse_outcome_prices(market_payload.get("outcomePrices"))
    if not closed or not prices or len(prices) != 2:
        return ("pending", None, None, None)

    try:
        nums = [float(p) for p in prices]
    except (TypeError, ValueError):
        return ("pending", None, None, None)

    # 已结算: 胜方价格==1, 败方==0 (二元 binary outcome 约定)
    if not (
        (abs(nums[0] - 1.0) < 1e-9 and abs(nums[1]) < 1e-9)
        or (abs(nums[1] - 1.0) < 1e-9 and abs(nums[0]) < 1e-9)
    ):
        # 价格不是干净的 0/1 → 视为 pending (可能是 closed=true 但还在 dispute window)
        return ("pending", None, None, None)

    win_idx = 0 if nums[0] >= nums[1] else 1
    outcome_bool = win_idx == 0  # 约定 outcomes[0]='Yes' → True
    winning_token = clob_token_ids[win_idx] if win_idx < len(clob_token_ids) else None
    return ("settled", outcome_bool, winning_token, nums[0])


def _fetch_event_markets(slug: str, *, timeout: float = 10.0) -> Optional[list[dict]]:
    url = f"{GAMMA_BASE}/events/slug/{slug}"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
        return body.get("markets") or []
    except (requests.RequestException, ValueError) as exc:
        logger.warning("settlement_adapter: gamma fetch failed slug=%s err=%s", slug, exc)
        return None


def _fetch_records(
    descriptors: list[ConditionDescriptor],
) -> tuple[list[SettlementRecord], list[str]]:
    """为 universe 拉取 gamma, 按 condition_id 派生 record. 返回 (records, missing_condition_ids)."""
    by_slug: dict[str, list[ConditionDescriptor]] = {}
    for d in descriptors:
        by_slug.setdefault(d.market_slug, []).append(d)

    records: list[SettlementRecord] = []
    missing: list[str] = []
    for slug, descs in by_slug.items():
        markets = _fetch_event_markets(slug)
        if markets is None:
            # 整 slug 拉失败 → 该 slug 下所有 condition 都视为 missing (carry-forward
            # 的责任在调用方 — 这里只报告本次刷新缺失了哪些)
            missing.extend(d.condition_id for d in descs)
            continue
        by_cid = {m.get("conditionId"): m for m in markets if m.get("conditionId")}
        for d in descs:
            mp = by_cid.get(d.condition_id)
            if mp is None:
                missing.append(d.condition_id)
                continue
            state, ob, win_tok, final_price = _derive_settlement(mp, d.clob_token_ids)
            records.append(
                SettlementRecord(
                    condition_id=d.condition_id,
                    market_slug=d.market_slug,
                    settlement_state=state,
                    settlement_outcome_event_bool=ob,
                    winning_token_id=win_tok,
                    final_price=final_price,
                    settled_at_utc=None,  # gamma 不直接给 settle 时间, 留 NULL
                    raw_payload={
                        "closed": mp.get("closed"),
                        "outcomePrices": mp.get("outcomePrices"),
                        "umaResolutionStatus": mp.get("umaResolutionStatus"),
                        "endDate": mp.get("endDate"),
                    },
                )
            )
    return records, missing


def _compute_effect_hash(records: list[SettlementRecord]) -> str:
    """对全部 record 的关键字段做规范化哈希, 用于 inputs_hash 链路."""
    key = sorted(
        (
            r.condition_id,
            r.settlement_state,
            r.winning_token_id or "",
            r.final_price if r.final_price is not None else "",
        )
        for r in records
    )
    h = hashlib.sha256()
    h.update(json.dumps(key, sort_keys=True, default=str).encode())
    return h.hexdigest()


def refresh_settlement_feed(
    descriptors: Iterable[ConditionDescriptor],
) -> RefreshResult:
    """主入口: 拉取 → 写 settlement_feed_versions + settlement_feed_records.

    bootstrap 检查由调用方 (MarketStateAdapter) 负责 — 本函数即使 universe 非空且
    全部 missing, 也会写一行 refresh_status='failed' 的 version, 以便上层判断。
    """
    desc_list = list(descriptors)
    if not desc_list:
        # 空 universe: 写一个 ok 空版本, 让调用方区分 "未刷新" 和 "刷新过但是空"
        records: list[SettlementRecord] = []
        missing: list[str] = []
    else:
        records, missing = _fetch_records(desc_list)

    if desc_list and not records:
        status = "failed"
    elif missing:
        status = "partial"
    else:
        status = "ok"

    refreshed_ids = [r.condition_id for r in records]
    effect_hash = _compute_effect_hash(records)

    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO settlement_feed_versions
              (refresh_status, rows_upserted, refreshed_condition_ids, missing_condition_ids, source_etag)
            VALUES (%s, %s, %s::jsonb, %s::jsonb, %s)
            RETURNING settlement_feed_version, refreshed_at_utc
            """,
            (
                status,
                len(records),
                json.dumps(refreshed_ids),
                json.dumps(missing),
                SOURCE_NAME,
            ),
        )
        version, _refreshed_at = cur.fetchone()

        if records:
            cur.executemany(
                """
                INSERT INTO settlement_feed_records
                  (settlement_feed_version, condition_id, market_slug, settlement_state,
                   settlement_outcome_event_bool, winning_token_id, final_price,
                   settled_at_utc, settlement_source, raw_payload)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                [
                    (
                        version,
                        r.condition_id,
                        r.market_slug,
                        r.settlement_state,
                        r.settlement_outcome_event_bool,
                        r.winning_token_id,
                        r.final_price,
                        r.settled_at_utc,
                        SOURCE_NAME,
                        json.dumps(r.raw_payload),
                    )
                    for r in records
                ],
            )
        conn.commit()

    logger.info(
        "settlement_adapter: version=%s status=%s refreshed=%d missing=%d effect_hash=%s",
        version,
        status,
        len(refreshed_ids),
        len(missing),
        effect_hash[:12],
    )
    return RefreshResult(
        settlement_feed_version=version,
        refresh_status=status,
        refreshed_condition_ids=refreshed_ids,
        missing_condition_ids=missing,
        rows_upserted=len(records),
        effect_hash=effect_hash,
    )


def is_bootstrap_pending() -> bool:
    """settlement_feed_versions 表为空 → bootstrap 未完成, 调用方应 mark batch failed."""
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM settlement_feed_versions LIMIT 1")
        return cur.fetchone() is None


def latest_records_by_condition() -> dict[str, dict]:
    """读取每个 condition_id 最近一条 settlement_feed_records (carry-forward 视图).

    返回 {condition_id: {settlement_state, winning_token_id, final_price,
    settlement_feed_version, settlement_outcome_event_bool}}.
    """
    sql = """
        SELECT DISTINCT ON (condition_id)
            condition_id,
            settlement_state,
            winning_token_id,
            final_price,
            settlement_feed_version,
            settlement_outcome_event_bool,
            settled_at_utc
        FROM settlement_feed_records
        ORDER BY condition_id, settlement_feed_version DESC
    """
    out: dict[str, dict] = {}
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            out[d["condition_id"]] = d
    return out
