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


# Initialize the Gemini client
def _get_client():
    """Initialize and return the Gemini client."""
    if not GOOGLE_API_KEY:
        raise ValueError("GOOGLE_API_KEY is not set. Please check your .env file.")
    
    # Initialize client with API key directly
    client = genai.Client(api_key=GOOGLE_API_KEY)
    return client


# Define the response schema for structured output
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "防守端分析": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "合约": {
                        "type": "string",
                        "description": "合约名称"
                    },
                    "状态": {
                        "type": "string",
                        "description": "状态"
                    },
                    "操作建议": {
                        "type": "string",
                        "description": "操作建议"
                    },
                    "逻辑": {
                        "type": "string",
                        "description": "逻辑"
                    }
                }
            }
        },
        "进攻端分析": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "合约": {
                        "type": "string",
                        "description": "合约名称"
                    },
                    "状态": {
                        "type": "string",
                        "description": "状态"
                    },
                    "操作建议": {
                        "type": "string",
                        "description": "操作建议"
                    },
                    "阶梯挂单建议": {
                        "type": "object",
                        "properties": {
                            "安全阀": {
                                "type": "object",
                                "properties": {
                                    "价格": {
                                        "type": "integer",
                                        "description": "价格"
                                    },
                                    "逻辑": {
                                        "type": "string",
                                        "description": "逻辑"
                                    }
                                }
                            },
                            "目标位": {
                                "type": "object",
                                "properties": {
                                    "价格": {
                                        "type": "integer",
                                        "description": "价格"
                                    },
                                    "逻辑": {
                                        "type": "string",
                                        "description": "逻辑"
                                    }
                                }
                            },
                            "梦想单": {
                                "type": "object",
                                "properties": {
                                    "价格": {
                                        "type": "integer",
                                        "description": "价格"
                                    },
                                    "逻辑": {
                                        "type": "string",
                                        "description": "逻辑"
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "预警信号": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "预警方向": {
                        "type": "string",
                        "description": "预警方向",
                        "enum": ["up_to", "down_to"]
                    },
                    "价格": {
                        "type": "integer",
                        "description": "价格"
                    },
                    "操作建议": {
                        "type": "string",
                        "description": "操作建议"
                    }
                }
            }
        }
    },
    "required": ["防守端分析", "进攻端分析", "预警信号"]
}

def _get_system_prompt(current_date: str) -> str:
    """Generate the Superforecaster system prompt."""
    return f"""# Role
你是一名资深的加密货币衍生品交易员和预测市场（Prediction Market）分析师。你精通期权定价逻辑（特别是Theta衰减和Delta对冲）、市场大众心理学以及Polymarket的特有机制。

# Goal
根据我提供的【Polymarket持仓情况】、【Polymarket挂单情况】、【过去24小时的比特币4h K数据】以及【当前时间】，分析我的风险敞口，并给出具体的挂单（Limit Order）管理、操作建议。

# Input Data
我会提供以下信息：
1. **持仓情况**：包含合约主题、合约类型（side：Yes/No）、平均买入价（Avg）、当前市场价、持仓数量、初始价值、当前价值、结算日期。
2. **挂单情况**：我当前的止盈/止损挂单。
3. **市场背景**：比特币过去24小时4h K线数据。
4. **当前时间**：{current_date}。

# Analysis Framework (必须严格遵循的分析逻辑)

## 1. 核心数据解析
* **提取持仓**：列出每个合约的名称、方向（Yes/No）、成本价 vs 现价、浮盈/浮亏状态。
* **计算安全垫 (Safety Margin)**：计算当前BTC价格距离合约触发价（Strike Price）的百分比距离。

## 2. 风险与时间价值评估 (关键步骤)
* **时间因子 (Theta Analysis)**：结合【当前日期】和【结算日期】，判断时间流逝对该合约是有利还是有害。
    * *规则：对于虚值（OTM）的"Yes"合约，时间是敌人；对于深虚值的"No"合约，时间是朋友。*
* **概率偏差**：对比Polymarket的定价（例如 20¢ 代表 20% 概率）与实际市场技术面概率，寻找错误定价。

## 3. 策略建议 (Actionable Advice)
基于上述分析，将仓位分为两类并给出建议：
* **防守型仓位 (Defensive)**：通常是 "Dip to No" 或深实值合约。
    * *建议方向*：是否由于安全垫足够厚而应该 "Hold to Maturity"（持有到期）吃满利润？还是应该挂单释放保证金？
* **进攻型仓位 (Offensive)**：通常是 "Reach Yes" 或博弈型合约。
    * *建议方向*：必须给出**阶梯止盈 (Laddering)** 建议。根据阻力位，给出 3 个具体的挂单价格建议（保守/中性/激进）。

# Output Format
请用以下结构输出分析结果：

## 📊 市场与持仓快照
简述当前市场环境对我的持仓是顺风还是逆风。

## 🛡️ 防守端分析 (Dip/Floor bets)
* **合约**: [名称]
* **状态**: [安全/危险]
* **操作建议**: [具体价格/保持不动]
* **逻辑**: [解释Theta或支撑位逻辑]

## ⚔️ 进攻端分析 (Reach/Ceiling bets)
* **合约**: [名称]
* **状态**: [博弈中/需止损]
* **阶梯挂单建议**:
    1.  **安全阀**: [价格¢] - 逻辑：[例如：防假突破]
    2.  **目标位**: [价格¢] - 逻辑：[例如：关键阻力位]
    3.  **梦想单**: [价格¢] - 逻辑：[例如：FOMO情绪溢价]

## 🚨 预警信号 (Watchlist)
* 如果 BTC 跌破/突破 $[价格]，立即执行 [具体操作]。

---"""


def analyze_market_with_grounding(
    polymarket_status: list,
    btc_4h_k_data: list,
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
        tools=[grounding_tool],  # Enable Google Search Grounding
        response_schema=RESPONSE_SCHEMA,  # Structured output
        temperature=0.7,  # Balanced creativity and consistency
    )
    
    # Build the prompt
    system_prompt = _get_system_prompt(current_date)
    user_prompt = f"""
    Polymarket持仓情况和挂单情况: {polymarket_status}
    比特币过去24小时4h K线数据: {btc_4h_k_data}
    """

    try:
        # Combine system prompt and user prompt
        full_prompt = f"{system_prompt}\n\n{user_prompt}"
        
        # Generate content with grounding
        response = client.models.generate_content(
            model=model,
            contents=full_prompt,
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