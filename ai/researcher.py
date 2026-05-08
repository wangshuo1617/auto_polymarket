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
    GOLD_RESPONSE_SCHEMA,
    get_gold_system_instruction,
    get_gold_user_prompt,
    PATHVIEW_AI_SCHEMA,
    get_pathview_ai_system_instruction,
    get_pathview_ai_user_prompt,
)

ET_TIMEZONE = ZoneInfo("America/New_York")


def _dedupe_grounding_sources(sources: list[dict]) -> list[dict]:
    """按 URL 去重，保留前序来源。"""
    seen: set[str] = set()
    deduped: list[dict] = []
    for src in sources:
        url = str(src.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append(src)
    return deduped


def _ensure_news_factors_from_grounding(result: Dict[str, Any], sources: list[dict]) -> None:
    """
    保证 BTC短期预测.新闻驱动因子 引用外部检索来源。
    若模型未给出来源URL，则基于 grounding sources 进行兜底填充。
    """
    btc_prediction = result.get("BTC短期预测")
    if not isinstance(btc_prediction, dict):
        return

    news_factors = btc_prediction.get("新闻驱动因子")
    if not isinstance(news_factors, list):
        news_factors = []

    deduped_sources = _dedupe_grounding_sources(sources)
    if not deduped_sources:
        return
    grounded_urls = {str(src.get("url") or "").strip() for src in deduped_sources}
    news_urls = {
        str(item.get("来源") or "").strip()
        for item in news_factors
        if isinstance(item, dict)
    }
    if grounded_urls.intersection(news_urls):
        return

    fallback_items = []
    for src in deduped_sources[:3]:
        title = str(src.get("title") or "").strip() or "外部检索新闻"
        url = str(src.get("url") or "").strip()
        fallback_items.append(
            {
                "事件": title,
                "方向偏置": "偏震荡",
                "影响说明": "该条目来自外部检索来源，请结合K线与成交量确认其方向强度。",
                "发布时间": "未知",
                "来源": url,
            }
        )
    if fallback_items:
        btc_prediction["新闻驱动因子"] = fallback_items


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
    intraday_volatility_hint: dict,
    future_possibility_context: dict,
    profit_optimization_context: dict,
    market_sentiment_and_funding: dict,
    polymarket_event_situation: dict,
    usdc_balance: str,
    recommendation_memory_context: dict | None = None,
    previous_report: dict | None = None,
    operator_intent: str | None = None,
    monthly_target: str = "月度净值翻倍（+100%）",
) -> Dict[str, Any]:
    """
    Analyze the polymarket positions, orders, event situation, USDC balance, and btc 4h k data.
    previous_report: 上一时间段的报告内容，供本次输出参考与延续。
    operator_intent: 本次分析的操作员意图，如持仓偏好、当前判断等，优先于默认策略。
    monthly_target: 本月收益目标，用于调整策略激进度。
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
        system_instruction=get_system_instruction(current_date, monthly_target=monthly_target),
        tools=[grounding_tool],  # Enable Google Search Grounding
        response_schema=RESPONSE_SCHEMA,  # Structured output
        temperature=0.4,  # 金融分析需要一致性，低温优于高温
        max_output_tokens=16384,
    )
    
    user_prompt = get_user_prompt(
        polymarket_status,
        btc_4h_k_data,
        btc_1d_k_data,
        daily_volatility_profile,
        intraday_volatility_hint,
        future_possibility_context,
        profit_optimization_context,
        market_sentiment_and_funding,
        polymarket_event_situation,
        usdc_balance,
        recommendation_memory_context=recommendation_memory_context,
        previous_report=previous_report,
        operator_intent=operator_intent,
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
        
        _ensure_news_factors_from_grounding(result, sources)
        return result
        
    except Exception as e:
        raise Exception(f"Error calling Gemini API: {str(e)}") from e


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
        temperature=0.5,
        max_output_tokens=8192,
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

def analyze_gold_market_with_grounding(
    polymarket_status: list,
    gold_4h_k_data: list,
    gold_1d_k_data: list,
    daily_volatility_profile: dict,
    future_possibility_context: dict,
    profit_optimization_context: dict,
    gold_market_context: dict,
    polymarket_event_situation: dict,
    usdc_balance: str,
    previous_report: dict | None = None,
) -> Dict[str, Any]:
    """
    针对 Polymarket 黄金类 event 的持仓与市场分析（Gold K 线、波动率、收益优化等）。
    """
    current_date = datetime.now(ET_TIMEZONE).strftime("%Y-%m-%d")
    client = _get_client()
    model = GEMINI_MODEL_ID
    grounding_tool = types.Tool(google_search=types.GoogleSearch())
    config = types.GenerateContentConfig(
        system_instruction=get_gold_system_instruction(current_date),
        tools=[grounding_tool],
        response_schema=GOLD_RESPONSE_SCHEMA,
        temperature=0.4,
        max_output_tokens=8192,
    )
    user_prompt = get_gold_user_prompt(
        polymarket_status,
        gold_4h_k_data,
        gold_1d_k_data,
        daily_volatility_profile,
        future_possibility_context,
        profit_optimization_context,
        gold_market_context,
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
        raise Exception(f"Error calling Gemini API (gold): {str(e)}") from e


def analyze_pathview_for_advisory(
    *,
    batch_id: int,
    batch_as_of_utc: str,
    current_btc_price: float,
    days_left: float,
    gbm_sigma_daily: float,
    gbm_drift_daily: float,
    sigma_source: str,
    btc_panels: dict,
    tokens: list,
    baseline_fair_by_token: dict,
    temperature: float = 0.3,
    max_output_tokens: int = 8192,
) -> Dict[str, Any]:
    """B3: AI PathView shadow estimator. Returns parsed JSON dict matching
    PATHVIEW_AI_SCHEMA. Caller must validate via pathview_validator before
    persisting. NEVER drives production trading."""
    client = _get_client()
    config = types.GenerateContentConfig(
        system_instruction=get_pathview_ai_system_instruction(),
        response_mime_type="application/json",
        response_schema=PATHVIEW_AI_SCHEMA,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        thinking_config=types.ThinkingConfig(thinking_budget=2048),
    )
    user_prompt = get_pathview_ai_user_prompt(
        batch_id=batch_id,
        batch_as_of_utc=batch_as_of_utc,
        current_btc_price=current_btc_price,
        days_left=days_left,
        gbm_sigma=gbm_sigma_daily,
        gbm_drift=gbm_drift_daily,
        sigma_source=sigma_source,
        btc_panels=btc_panels,
        tokens=tokens,
        baseline_fair_by_token=baseline_fair_by_token,
    )
    response = client.models.generate_content(
        model=GEMINI_MODEL_ID,
        contents=user_prompt,
        config=config,
    )
    text = response.text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        if "```json" in text:
            s = text.find("```json") + 7
            e = text.find("```", s)
            text = text[s:e].strip()
        elif "```" in text:
            s = text.find("```") + 3
            e = text.find("```", s)
            text = text[s:e].strip()
        return json.loads(text)
