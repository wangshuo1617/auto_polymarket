"""
Gemini Researcher Module for Polymarket Analysis
Uses Google Gemini API with Google Search Grounding for market research.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any
from google import genai
from google.genai import types

from config import GOOGLE_API_KEY, GEMINI_MODEL_ID
from ai.prompts import (
    RESPONSE_SCHEMA,
    get_system_instruction,
    get_user_prompt,
    MONTHLY_STRATEGY_SCHEMA,
    get_monthly_system_instruction,
    get_monthly_user_prompt,
    OIL_RESPONSE_SCHEMA,
    get_oil_system_instruction,
    get_oil_user_prompt,
    ENTERABILITY_RESPONSE_SCHEMA,
    ENTERABILITY_SYSTEM_INSTRUCTION,
    get_enterability_user_prompt,
    EVENT_SELECTION_RESPONSE_SCHEMA,
    EVENT_SELECTION_SYSTEM_INSTRUCTION,
    get_event_selection_user_prompt,
)

ET_TIMEZONE = ZoneInfo("America/New_York")


def _parse_json_response_text(response_text: str) -> Dict[str, Any]:
    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        if "```json" in response_text:
            json_start = response_text.find("```json") + 7
            json_end = response_text.find("```", json_start)
            response_text = response_text[json_start:json_end].strip()
        elif "```" in response_text:
            json_start = response_text.find("```") + 3
            json_end = response_text.find("```", json_start)
            response_text = response_text[json_start:json_end].strip()
        return json.loads(response_text)


# Initialize the Gemini client
def _get_client():
    """Initialize and return the Gemini client."""
    if not GOOGLE_API_KEY:
        raise ValueError("GOOGLE_API_KEY is not set. Please check your .env file.")
    
    # Initialize client with API key directly
    client = genai.Client(api_key=GOOGLE_API_KEY)
    return client

def analyze_market_with_grounding(
    polymarket_status: list,
    btc_4h_k_data: list,
    btc_1d_k_data: list,
    daily_volatility_profile: dict,
    future_possibility_context: dict,
    profit_optimization_context: dict,
    market_sentiment_and_funding: dict,
    polymarket_event_situation: dict,
    usdc_balance: str,
    previous_report: dict | None = None,
) -> Dict[str, Any]:
    """
    Analyze the polymarket positions, orders, event situation, USDC balance, and btc 4h k data.
    previous_report: 上一时间段的报告内容，供本次输出参考与延续。
    """
    
    # Get current date for temporal context
    current_date = datetime.now(ET_TIMEZONE).strftime("%Y-%m-%d")
    
    # Initialize client
    client = _get_client()
    
    # Use provided model_id or default from config
    model = GEMINI_MODEL_ID
    
    # Configure generation with Google Search Grounding
    # Enable Google Search Grounding tool
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    
    config = types.GenerateContentConfig(
        system_instruction=get_system_instruction(current_date),
        tools=[grounding_tool],  # Enable Google Search Grounding
        response_schema=RESPONSE_SCHEMA,  # Structured output
        temperature=0.7,  # Balanced creativity and consistency
    )
    
    user_prompt = get_user_prompt(
        polymarket_status,
        btc_4h_k_data,
        btc_1d_k_data,
        daily_volatility_profile,
        future_possibility_context,
        profit_optimization_context,
        market_sentiment_and_funding,
        polymarket_event_situation,
        usdc_balance,
        previous_report=previous_report,
    )

    try:        
        # Generate content with grounding
        response = client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=config
        )
        
        # Extract the text response
        response_text = response.text
        
        # Extract grounding sources if available
        sources = []
        try:
            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                if hasattr(candidate, 'grounding_metadata'):
                    grounding_metadata = candidate.grounding_metadata
                    if hasattr(grounding_metadata, 'grounding_chunks'):
                        for chunk in grounding_metadata.grounding_chunks:
                            if hasattr(chunk, 'web'):
                                web = chunk.web
                                if hasattr(web, 'uri'):
                                    sources.append({
                                        "url": web.uri,
                                        "title": getattr(web, 'title', ''),
                                    })
        except Exception as e:
            # If source extraction fails, continue without sources
            pass
        
        # Parse JSON response
        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            # If response is not valid JSON, try to extract JSON from markdown code blocks
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            
            result = json.loads(response_text)
        
        return result
        
    except Exception as e:
        raise Exception(f"Error calling Gemini API: {str(e)}") from e


def analyze_oil_market_with_grounding(
    polymarket_status: list,
    oil_4h_k_data: list,
    oil_1d_k_data: list,
    daily_volatility_profile: dict,
    future_possibility_context: dict,
    profit_optimization_context: dict,
    oil_market_context: dict,
    polymarket_event_situation: dict,
    usdc_balance: str,
    previous_report: dict | None = None,
) -> Dict[str, Any]:
    """
    针对 Polymarket 原油类 event 的持仓与市场分析（WTI K 线、波动率、收益优化等）。
    """
    current_date = datetime.now(ET_TIMEZONE).strftime("%Y-%m-%d")
    client = _get_client()
    model = GEMINI_MODEL_ID
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        system_instruction=get_oil_system_instruction(current_date),
        tools=[grounding_tool],
        response_schema=OIL_RESPONSE_SCHEMA,
        temperature=0.7,
    )
    user_prompt = get_oil_user_prompt(
        polymarket_status,
        oil_4h_k_data,
        oil_1d_k_data,
        daily_volatility_profile,
        future_possibility_context,
        profit_optimization_context,
        oil_market_context,
        polymarket_event_situation,
        usdc_balance,
        previous_report=previous_report,
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=config,
        )
        response_text = response.text
        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            result = json.loads(response_text)
        return result
    except Exception as e:
        raise Exception(f"Error calling Gemini API (oil): {str(e)}") from e


def analyze_monthly_strategy_with_grounding(
    btc_4h_k_data: list,
    btc_1d_k_data: list,
    market_sentiment_and_funding: dict,
    derived_summary: dict,
    target_month: str,
) -> Dict[str, Any]:
    """
    Analyze month-start strategy for Polymarket BTC monthly market.
    """
    current_date = datetime.now(ET_TIMEZONE).strftime("%Y-%m-%d")
    client = _get_client()
    model = GEMINI_MODEL_ID

    grounding_tool = types.Tool(google_search=types.GoogleSearch())

    config = types.GenerateContentConfig(
        system_instruction=get_monthly_system_instruction(current_date, target_month),
        tools=[grounding_tool],
        response_schema=MONTHLY_STRATEGY_SCHEMA,
        temperature=0.6,
    )

    user_prompt = get_monthly_user_prompt(
        btc_4h_k_data,
        btc_1d_k_data,
        market_sentiment_and_funding,
        derived_summary,
    )

    try:
        response = client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=config,
        )

        response_text = response.text

        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            if "```json" in response_text:
                json_start = response_text.find("```json") + 7
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()
            elif "```" in response_text:
                json_start = response_text.find("```") + 3
                json_end = response_text.find("```", json_start)
                response_text = response_text[json_start:json_end].strip()

            result = json.loads(response_text)

        return result

    except Exception as e:
        raise Exception(f"Error calling Gemini API: {str(e)}") from e


def assess_enterability_with_grounding(events_summary: list) -> Dict[str, Any]:
    """
    基于市场消息（新闻、事件进展）判定各事件的「可进入性」，使用 Google Search Grounding。
    events_summary 每项需含 title, slug, description, resolutionSource, markets（含 question, outcomes, outcomePrices）。
    返回 { "opportunities": [ { event_title, event_slug, 可进入性, 理由_基于市场消息, ... } ] }。

    为规避 grounding 单次 remote calls 的上限，函数会自动分批调用模型。
    并对每一批次执行“结果补齐”：任何缺失事件都会自动补一条「观望」。
    """
    current_date = datetime.now(ET_TIMEZONE).strftime("%Y-%m-%d")
    client = _get_client()
    model = GEMINI_MODEL_ID
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        system_instruction=ENTERABILITY_SYSTEM_INSTRUCTION,
        tools=[grounding_tool],
        response_schema=ENTERABILITY_RESPONSE_SCHEMA,
        temperature=0.5,
    )
    def _default_watch(event: Dict[str, Any], reason: str) -> Dict[str, Any]:
        title = str(event.get("title") or "—")
        slug = str(event.get("slug") or "—")
        return {
            "event_title": title,
            "event_slug": slug,
            "可进入性": "观望",
            "理由_基于市场消息": f"证据不足或模型未返回该事件结论：{reason}",
            "建议方向或观望说明": "暂不入场，等待更明确的现实世界进展与可信来源。",
            "风险提示": "信息不对称导致误判风险较高。",
            "参考消息摘要": "待补充：需进一步检索权威新闻与官方来源。",
        }

    if not events_summary:
        return {"opportunities": []}

    # 分批：每批控制在较小规模，降低单次 grounding remote calls 压力
    batch_size = 8
    all_out: list[Dict[str, Any]] = []

    for i in range(0, len(events_summary), batch_size):
        batch = events_summary[i : i + batch_size]
        try:
            user_prompt = get_enterability_user_prompt(batch, current_date)
            response = client.models.generate_content(
                model=model,
                contents=user_prompt,
                config=config,
            )
            parsed = _parse_json_response_text(response.text or "{}")
            returned = parsed.get("opportunities") if isinstance(parsed, dict) else []
            returned = returned if isinstance(returned, list) else []

            # 将返回结果按 slug/title 建索引
            by_slug: Dict[str, Dict[str, Any]] = {}
            by_title: Dict[str, Dict[str, Any]] = {}
            for item in returned:
                if not isinstance(item, dict):
                    continue
                slug = str(item.get("event_slug") or "").strip()
                title = str(item.get("event_title") or "").strip()
                if slug:
                    by_slug[slug] = item
                if title:
                    by_title[title] = item

            # 严格补齐本批次：保证每个输入事件至少一条输出
            for ev in batch:
                slug = str(ev.get("slug") or "").strip()
                title = str(ev.get("title") or "").strip()
                item = None
                if slug and slug in by_slug:
                    item = by_slug[slug]
                elif title and title in by_title:
                    item = by_title[title]
                if isinstance(item, dict):
                    all_out.append(item)
                else:
                    all_out.append(_default_watch(ev, "未命中返回结果"))
        except Exception as batch_error:
            # 单批次失败时不抛出，直接将该批次补齐为“观望”，保证全覆盖输出
            for ev in batch:
                all_out.append(_default_watch(ev, f"批次调用失败: {batch_error}"))

    return {"opportunities": all_out}


def select_events_with_gemini(
    events_summary: list[dict],
    target_count: int = 20,
    chunk_size: int = 120,
) -> list[dict]:
    """
    先由 Gemini 在大量事件中筛选出更合理的候选事件，再用于后续深度分析。
    为控制 token，使用分块初筛 + 二次合并筛选。
    """
    if not events_summary:
        return []

    target_count = max(1, int(target_count))
    chunk_size = max(20, int(chunk_size))
    current_date = datetime.now(ET_TIMEZONE).strftime("%Y-%m-%d")
    client = _get_client()
    model = GEMINI_MODEL_ID

    # 事件池索引（用于根据 slug/title 回填原始事件）
    by_slug: Dict[str, dict] = {}
    by_title: Dict[str, dict] = {}
    for ev in events_summary:
        slug = str(ev.get("slug") or "").strip()
        title = str(ev.get("title") or "").strip()
        if slug:
            by_slug[slug] = ev
        if title:
            by_title[title] = ev

    select_config = types.GenerateContentConfig(
        system_instruction=EVENT_SELECTION_SYSTEM_INSTRUCTION,
        response_schema=EVENT_SELECTION_RESPONSE_SCHEMA,
        temperature=0.2,
        # 选事件阶段不使用 grounding/工具调用，避免 AFC remote call 预算影响
        automaticFunctionCalling=types.AutomaticFunctionCallingConfig(
            disable=True
        ),
    )

    def _extract_selected(raw: Dict[str, Any], source_events: list[dict]) -> list[dict]:
        items = raw.get("selected_events") if isinstance(raw, dict) else []
        items = items if isinstance(items, list) else []
        out: list[dict] = []
        seen: set[str] = set()
        local_by_slug = {str(e.get("slug") or "").strip(): e for e in source_events}
        local_by_title = {str(e.get("title") or "").strip(): e for e in source_events}
        for it in items:
            if not isinstance(it, dict):
                continue
            # 严格只保留“可考虑进入”事件
            if str(it.get("enterability") or "").strip() != "可考虑进入":
                continue
            slug = str(it.get("event_slug") or "").strip()
            title = str(it.get("event_title") or "").strip()
            ev = None
            if slug and slug in local_by_slug:
                ev = local_by_slug[slug]
            elif title and title in local_by_title:
                ev = local_by_title[title]
            if ev is None:
                continue
            key = str(ev.get("slug") or ev.get("title") or "")
            if key in seen:
                continue
            seen.add(key)
            out.append(ev)
        return out

    # 第一阶段：分块初筛
    chunks = [events_summary[i : i + chunk_size] for i in range(0, len(events_summary), chunk_size)]
    shortlist: list[dict] = []
    shortlist_seen: set[str] = set()
    per_chunk_pick = max(3, min(target_count, (target_count // max(1, len(chunks))) + 2))

    for chunk in chunks:
        prompt = get_event_selection_user_prompt(chunk, current_date, per_chunk_pick)
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=select_config)
            parsed = _parse_json_response_text(resp.text or "{}")
            chosen = _extract_selected(parsed, chunk)
        except Exception:
            # 该块失败则跳过，避免误选不具备进入价值的事件
            chosen = []
        for ev in chosen:
            key = str(ev.get("slug") or ev.get("title") or "")
            if key in shortlist_seen:
                continue
            shortlist_seen.add(key)
            shortlist.append(ev)

    # 第二阶段：若初筛过多，再让 Gemini 合并到 target_count
    if len(shortlist) > target_count:
        prompt = get_event_selection_user_prompt(shortlist, current_date, target_count)
        try:
            resp = client.models.generate_content(model=model, contents=prompt, config=select_config)
            parsed = _parse_json_response_text(resp.text or "{}")
            final_selected = _extract_selected(parsed, shortlist)
        except Exception:
            final_selected = shortlist[:target_count]
    else:
        final_selected = shortlist

    # 宁缺毋滥：不再随机补齐；若不足 target_count，按实际可进入数量返回
    return final_selected[:target_count]