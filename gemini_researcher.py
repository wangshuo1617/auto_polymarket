"""
Gemini Researcher Module for Polymarket Analysis
Uses Google Gemini API with Google Search Grounding for market research.
"""
import sys
import os
from pathlib import Path

# 添加项目根目录到 sys.path，以便可以直接运行此文件
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import json
from datetime import datetime
from typing import Dict, Any, Optional
from google import genai
from google.genai import types

from config import GOOGLE_API_KEY, GEMINI_MODEL_ID
from gemini_prompts import (
    RESPONSE_SCHEMA,
    get_system_instruction,
    get_user_prompt,
)


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
    market_sentiment_and_funding: dict,
) -> Dict[str, Any]:
    """
    Analyze the polymarket positions, orders, and btc 4h k data.
    """
    
    # Get current date for temporal context
    current_date = datetime.now().strftime("%Y-%m-%d")
    
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
        polymarket_status, btc_4h_k_data, market_sentiment_and_funding
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