"""
Gemini Researcher 的 RESPONSE_SCHEMA、system_instruction、user_prompt 定义。
与 gemini_researcher.py 配合使用。
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "市场与持仓快照": {
            "type": "string",
            "description": "一段话概括，例如：ETF流出配合K线破位，且散户逆势做多，下跌未结束。分析K线和新闻，简述当前市场环境对我的持仓是顺风还是逆风。预测未来24小时的btc市场走势和ploymarket市场走势。给出预期月内btc的波动范围"
        },
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
                    "希腊值分析": {
                        "type": "string",
                        "description": "希腊值分析"
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
                    "希腊值分析": {
                        "type": "string",
                        "description": "希腊值分析"
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
                                        "description": "挂单价格"
                                    },
                                    "逻辑": {
                                        "type": "string",
                                        "description": "逻辑"
                                    },
                                    "仓位百分比": {
                                        "type": "integer",
                                        "description": "仓位百分比"
                                    }
                                }
                            },
                            "目标位": {
                                "type": "object",
                                "properties": {
                                    "价格": {
                                        "type": "integer",
                                        "description": "挂单价格"
                                    },
                                    "逻辑": {
                                        "type": "string",
                                        "description": "逻辑"
                                    },
                                    "仓位百分比": {
                                        "type": "integer",
                                        "description": "仓位百分比"
                                    }
                                }
                            },
                            "梦想单": {
                                "type": "object",
                                "properties": {
                                    "价格": {
                                        "type": "integer",
                                        "description": "挂单价格"
                                    },
                                    "逻辑": {
                                        "type": "string",
                                        "description": "逻辑"
                                    },
                                    "仓位百分比": {
                                        "type": "integer",
                                        "description": "仓位百分比"
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
                        "description": "btc价格"
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

MONTHLY_STRATEGY_SCHEMA = {
    "type": "object",
    "properties": {
        "月份趋势判断": {
            "type": "string",
            "description": "对新一月BTC整体趋势的判断（偏多/偏空/震荡），以及核心依据。"
        },
        "月内BTC变动区间": {
            "type": "object",
            "properties": {
                "下限": {"type": "number", "description": "月内BTC价格区间下限"},
                "上限": {"type": "number", "description": "月内BTC价格区间上限"},
                "逻辑": {"type": "string", "description": "区间判断依据"}
            },
            "required": ["下限", "上限", "逻辑"]
        },
        "策略方案": {
            "type": "object",
            "properties": {
                "总体建议": {"type": "string"},
                "建仓方向": {"type": "string", "enum": ["偏多", "偏空", "震荡"]},
                "仓位建议": {"type": "string"},
                "分批建仓": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "触发条件": {"type": "string"},
                            "建议价格区间": {"type": "string"},
                            "仓位比例": {"type": "string"},
                            "逻辑": {"type": "string"}
                        },
                        "required": ["触发条件", "建议价格区间", "仓位比例", "逻辑"]
                    }
                },
                "风险控制": {"type": "array", "items": {"type": "string"}},
                "关键观察指标": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["总体建议", "建仓方向", "仓位建议", "分批建仓", "风险控制", "关键观察指标"]
        },
        "风险提示": {"type": "array", "items": {"type": "string"}},
        "参考数据摘要": {
            "type": "object",
            "properties": {
                "btc现价": {"type": "number"},
                "4h趋势": {"type": "string"},
                "24h RSI": {"type": "string"},
                "资金费率": {"type": "string"},
                "OI": {"type": "string"},
                "ETF净流入": {"type": "string"},
                "稳定币流动性": {"type": "string"},
                "恐惧贪婪": {"type": "string"},
                "多空比": {"type": "string"}
            },
            "required": [
                "btc现价",
                "4h趋势",
                "24h RSI",
                "资金费率",
                "OI",
                "ETF净流入",
                "稳定币流动性",
                "恐惧贪婪",
                "多空比"
            ]
        }
    },
    "required": ["月份趋势判断", "月内BTC变动区间", "策略方案", "风险提示", "参考数据摘要"]
}


SYSTEM_INSTRUCTION_TEMPLATE = """# Role
你是一名资深的加密货币衍生品交易员和预测市场（Prediction Market）专家。你精通二元期权（Binary Options）的定价模型、Theta衰减特性、Delta对冲策略，并对Polymarket的流动性陷阱有深刻理解。

# Goal
根据我提供的【Polymarket持仓】、【挂单】、【BTC K线及市场数据】，对我的账户进行风险敞口分析（Exposure Analysis）。
**核心任务**：基于当前BTC价格与到期日的距离，计算每个合约的胜率赔率比，并给出具体的、可执行的限价单（Limit Order）管理策略。

# Context & Constraints
* **当前时间**：{current_date}。
* **K线数据格式**: List of `[Kline open time(ms), Open price, High price, Low price, Close price, Volume, Kline Close time(ms), Quote asset volume, Number of trades, Taker buy base asset volume, Taker buy quote asset volume, Ignore]`。请重点关注 Close price 和 Volume。

# Input Data
我会提供以下信息：
1. **持仓情况**：包含合约主题、合约类型（side：Yes/No）、平均买入价（Avg）、当前市场价、持仓数量、初始价值、当前价值、结算日期。
2. **挂单情况**：未成交的 Limit Orders。
3. **市场背景**：比特币过去24小时4h K线数据。
4. **市场情绪与资金面**：包括衍生品情绪、流动性陷阱、机构资金流入流出情况、恐惧贪婪指数。

# Analysis Framework (COT - Chain of Thought)

请按以下步骤进行深呼吸并思考，不要跳过步骤：

## Step 1: 市场环境与定价偏差 (Market Context)
* 分析 K 线趋势：BTC 是处于上升/下降通道还是震荡？
* **趋势判断**: 结合 K 线和 资金面。判断当前是"下跌中继"、"底部反转"还是"崩盘开始"？K线是放量下跌，缩量下跌，放量上涨，缩量上涨还是其他情况？
* **波动率测算**: 基于 K 线的高低点，估算 BTC 未来的潜在波动范围。它是否有能力会在月内波动 10% 以上？
* **定价偏差检查**：计算当前合约价格隐含的概率（例如 30¢ = 30%）与基于 K 线技术面判断的概率是否存在显著偏差？
    * *Edge Case*: 如果 BTC 价格只差 1% 就要触发 Strike Price，但合约价格只有 40¢，这是低估还是因为时间不够了？

## Step 2: 持仓诊断与流动性检查 (Position Diagnosis)
* **安全垫 (Safety Margin)**：(Strike Price - Current BTC Price) / Current BTC Price。
* **Theta (时间价值)**：
    * 对于 OTM (虚值) 的 Yes 合约，时间流逝是致命的 -> 建议尽早止损或轮动。
    * 对于 ITM (实值) 的 Yes 合约，时间流逝是朋友 -> 建议 Hold。。

## Step 3: 策略生成 (Strategy Generation)
* **防守型 (Defensive)**：针对已获利需保护利润，或深套需止损的仓位。
    * 决策：Hold to Maturity (吃满 100¢) vs. Sell Now (释放资金)。
* **进攻型 (Offensive)**：针对博弈型仓位。
    * **阶梯挂单 (Laddering)**：不要单点止盈。根据 BTC 的阻力位/支撑位，反推合约价格，给出 3 档挂单建议。
"""

MONTHLY_SYSTEM_INSTRUCTION_TEMPLATE = """# Role
你是一名资深的加密货币策略分析师和预测市场（Prediction Market）交易员，擅长将宏观与技术面信号转化为可执行的月度建仓方案。

# Goal
根据【BTC价格趋势与K线】、【资金面与情绪数据】和【宏观流动性】为“新一月 Polymarket BTC 价格预测市场”制定建仓建议。
输出明确的趋势判断、月内价格上下限区间、分批建仓方案和风险控制要点。

# Context
* 当前时间：{current_date}
* 目标月份：{target_month}

# Output Constraints
* 输出必须严格符合 JSON Schema。
* 使用简明、可执行、可复用的策略语言。
* 不要包含与实际交易执行无关的内容。
"""


USER_PROMPT_TEMPLATE = """
以下是当前要分析的具体信息：

Polymarket持仓情况和挂单情况: {polymarket_status}
比特币过去24小时4h K线数据: {btc_4h_k_data}
市场情绪与资金面: {market_sentiment_and_funding}
"""

MONTHLY_USER_PROMPT_TEMPLATE = """
以下是月初建仓建议所需的输入数据：

BTC 4h K线数据: {btc_4h_k_data}
市场情绪与资金面: {market_sentiment_and_funding}
衍生摘要: {derived_summary}

请生成新一月 Polymarket BTC 价格预测市场的趋势判断与建仓方案。
"""


def get_system_instruction(current_date: str) -> str:
    """根据当前日期生成 system instruction。"""
    return SYSTEM_INSTRUCTION_TEMPLATE.format(current_date=current_date)


def get_user_prompt(
    polymarket_status: list,
    btc_4h_k_data: list,
    market_sentiment_and_funding: dict,
) -> str:
    """根据输入数据生成 user prompt。"""
    return USER_PROMPT_TEMPLATE.format(
        polymarket_status=polymarket_status,
        btc_4h_k_data=btc_4h_k_data,
        market_sentiment_and_funding=market_sentiment_and_funding,
    )


def get_monthly_system_instruction(current_date: str, target_month: str) -> str:
    return MONTHLY_SYSTEM_INSTRUCTION_TEMPLATE.format(
        current_date=current_date,
        target_month=target_month,
    )


def get_monthly_user_prompt(
    btc_4h_k_data: list,
    market_sentiment_and_funding: dict,
    derived_summary: dict,
) -> str:
    return MONTHLY_USER_PROMPT_TEMPLATE.format(
        btc_4h_k_data=btc_4h_k_data,
        market_sentiment_and_funding=market_sentiment_and_funding,
        derived_summary=derived_summary,
    )
