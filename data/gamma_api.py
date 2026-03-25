"""
Polymarket Gamma API 只读接口（仅 requests，无 CLOB 依赖）
用于套利扫描等场景，无需 py_clob_client。
"""
import json
import logging
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"


def _parse_outcome_prices(value: Any) -> List[float]:
    """将 API 返回的 outcomePrices（可能为 JSON 字符串或列表）解析为 float 列表。"""
    if isinstance(value, list):
        out = []
        for x in value:
            try:
                out.append(float(x))
            except (TypeError, ValueError):
                pass
        return out
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return _parse_outcome_prices(parsed) if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def _normalize_event(raw_event: Dict[str, Any]) -> Dict[str, Any] | None:
    if not isinstance(raw_event, dict):
        return None
    markets_raw = raw_event.get("markets") or []
    markets: List[Dict[str, Any]] = []
    for m in markets_raw:
        if not isinstance(m, dict):
            continue
        prices = _parse_outcome_prices(m.get("outcomePrices"))
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes) if outcomes else []
            except json.JSONDecodeError:
                outcomes = []
        if not isinstance(outcomes, list):
            outcomes = []
        markets.append(
            {
                "question": m.get("question") or "",
                "outcomes": outcomes,
                "outcomePrices": prices,
                "conditionId": m.get("conditionId"),
                "clobTokenIds": m.get("clobTokenIds"),
            }
        )
    if not markets:
        return None
    return {
        "id": raw_event.get("id"),
        "title": raw_event.get("title") or "",
        "slug": raw_event.get("slug") or "",
        "description": (raw_event.get("description") or "")[:2000],
        "resolutionSource": raw_event.get("resolutionSource") or "",
        "endDate": raw_event.get("endDate"),
        "startDate": raw_event.get("startDate"),
        "createdAt": raw_event.get("createdAt"),
        "updatedAt": raw_event.get("updatedAt"),
        "new": bool(raw_event.get("new")),
        "featured": bool(raw_event.get("featured")),
        "volume": raw_event.get("volume"),
        "volume24hr": raw_event.get("volume24hr"),
        "liquidity": raw_event.get("liquidity"),
        "tags": raw_event.get("tags") if isinstance(raw_event.get("tags"), list) else [],
        "markets": markets,
    }


def fetch_active_events(limit: int = 200, order: str | None = None) -> List[Dict[str, Any]]:
    """
    从 Gamma API 获取活跃、未关闭的事件列表（含 markets 及 outcomePrices），用于套利扫描等。
    返回列表每项含: title, slug, markets ([question, outcomes, outcomePrices, conditionId, clobTokenIds]), 等。
    """
    url = f"{GAMMA_BASE}/events"
    params: Dict[str, Any] = {
        "active": "true",
        "closed": "false",
        "limit": max(1, min(500, limit)),
    }
    if order:
        params["order"] = order
        params["ascending"] = "false"
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, list):
            return []
        result: List[Dict[str, Any]] = []
        for ev in raw:
            normalized = _normalize_event(ev)
            if normalized is not None:
                result.append(normalized)
        return result
    except Exception as e:
        logger.warning("fetch_active_events failed: %s", e)
        return []


def fetch_active_events_paginated(
    total_limit: int = 1000,
    page_size: int = 200,
    max_pages: int = 10,
) -> List[Dict[str, Any]]:
    """
    分页获取活跃且未关闭事件，尽量覆盖全量新兴市场扫描所需数据。
    """
    url = f"{GAMMA_BASE}/events"
    page_size = max(20, min(500, int(page_size)))
    total_limit = max(1, int(total_limit))
    max_pages = max(1, int(max_pages))

    offset = 0
    pages = 0
    out: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()

    while len(out) < total_limit and pages < max_pages:
        params: Dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": min(page_size, total_limit - len(out)),
            "offset": offset,
        }
        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            raw = response.json()
            if not isinstance(raw, list) or not raw:
                break
            added_this_page = 0
            for ev in raw:
                normalized = _normalize_event(ev)
                if normalized is None:
                    continue
                event_id = str(normalized.get("id") or normalized.get("slug") or "")
                if event_id and event_id in seen_ids:
                    continue
                if event_id:
                    seen_ids.add(event_id)
                out.append(normalized)
                added_this_page += 1
                if len(out) >= total_limit:
                    break
            pages += 1
            offset += len(raw)
            if len(raw) < params["limit"] or added_this_page == 0:
                break
        except Exception as e:
            logger.warning("fetch_active_events_paginated failed: page=%s offset=%s error=%s", pages + 1, offset, e)
            break

    return out


def fetch_event_by_slug(slug: str) -> Optional[Dict[str, Any]]:
    """GET /events/slug/{slug}，返回与 _normalize_event 一致的结构（含 markets outcomePrices）。"""
    raw_slug = str(slug or "").strip()
    if not raw_slug:
        return None
    url = f"{GAMMA_BASE}/events/slug/{raw_slug}"
    try:
        response = requests.get(url, timeout=25)
        response.raise_for_status()
        raw = response.json()
        if not isinstance(raw, dict):
            return None
        return _normalize_event(raw)
    except Exception as e:
        logger.warning("fetch_event_by_slug failed: slug=%s error=%s", raw_slug, e)
        return None
