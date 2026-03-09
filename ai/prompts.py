"""
Gemini Researcher 的 RESPONSE_SCHEMA、system_instruction、user_prompt 定义。
与 gemini_researcher.py 配合使用。
"""
import json

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "整体分析": {
            "type": "string",
            "description": "一段话概括市场环境：例如 ETF 流出配合 K 线破位、散户多空比等。简述当前环境对持仓是顺风还是逆风，预测未来 24 小时 BTC 与 Polymarket 走势，以及预期月内 BTC 波动范围。"
        },
        "当前持仓与挂单分析与建议": {
            "type": "array",
            "description": "针对已参与的 event（有持仓或挂单的），按 event/合约逐条给出仓位简述与挂单建议",
            "items": {
                "type": "object",
                "properties": {
                    "事件或合约": {
                        "type": "string",
                        "description": "事件名或合约/问题描述"
                    },
                    "仓位简述": {
                        "type": "string",
                        "description": "当前持仓与挂单的简要分析（盈亏、风险、是否值得持有等）"
                    },
                    "持有条件": {
                        "type": "string",
                        "description": "在什么条件下可以继续持有，避免过早离场"
                    },
                    "分层离场计划": {
                        "type": "string",
                        "description": "分批减仓/止盈/止损规则，避免一次性全平"
                    },
                    "挂单建议": {
                        "type": "array",
                        "description": "针对该合约的挂买单、挂卖单建议",
                        "items": {
                            "type": "object",
                            "properties": {
                                "操作类型": {"type": "string", "enum": ["挂买单", "挂卖单"]},
                                "方向": {"type": "string", "enum": ["Yes", "No"]},
                                "建议价格": {"type": "number", "description": "美分 1-99"},
                                "建议数量或比例": {"type": "string"},
                                "触发条件": {"type": "string", "description": "例如 BTC 上破/下破某价位后执行"},
                                "理由": {"type": "string"}
                            },
                            "required": ["操作类型", "方向", "建议价格", "理由"]
                        }
                    },
                    "离场风控": {
                        "type": "object",
                        "description": "基于1d ATR的自适应离场参数",
                        "properties": {
                            "市场状态": {"type": "string", "enum": ["trend_up", "trend_down", "range", "unknown"]},
                            "ATR百分比": {"type": "number"},
                            "波动分位": {"type": "number", "description": "近30天日线TR百分位(0-100)"},
                            "止盈阈值": {"type": "string", "description": "例如 +3.2% 或 +1.8xATR"},
                            "止损阈值": {"type": "string", "description": "例如 -1.8% 或 -1.0xATR"}
                        }
                    }
                },
                "required": ["事件或合约", "仓位简述", "挂单建议"]
            }
        },
        "建仓建议": {
            "type": "array",
            "description": "针对当前事件中用户尚未参与的 market/问题，结合 USDC 余额、市场情况与各 outcome 现价，给出是否建仓及如何建仓",
            "items": {
                "type": "object",
                "properties": {
                    "事件或问题": {"type": "string"},
                    "建议方向": {"type": "string", "enum": ["Yes", "No"]},
                    "建议价格区间": {"type": "string", "description": "例如 25-35¢"},
                    "建议投入金额或比例": {"type": "string", "description": "结合可用 USDC 给出，如 50 张、或 10% 余额"},
                    "预估优势": {"type": "string", "description": "例如 模型概率-隐含概率=+6.2%"},
                    "建议仓位上限": {"type": "string", "description": "例如 不超过总资金12%（分数Kelly后）"},
                    "理由": {"type": "string"}
                },
                "required": ["事件或问题", "建议方向", "建议价格区间", "建议投入金额或比例", "理由"]
            }
        },
        "预警信号": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "预警方向": {"type": "string", "enum": ["up_to", "down_to"]},
                    "价格": {"type": "integer", "description": "BTC 价格"},
                    "操作建议": {"type": "string"},
                    "关联止盈止损": {"type": "string", "description": "说明该预警对应止盈/止损动作"}
                }
            }
        }
    },
    "required": ["整体分析", "当前持仓与挂单分析与建议", "建仓建议", "预警信号"]
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
3. **Polymarket 事件与市场现价**：当前事件下各问题的题目、选项（outcomes）及对应实时价格（outcomePrices）。
4. **当前可用 USDC 余额**：用于建仓建议时考虑可投入金额。
5. **市场背景**：比特币过去7天4h K线数据 + 过去30天1d K线数据。
6. **市场情绪与资金面**：包括衍生品情绪、流动性陷阱、机构资金流入流出情况、恐惧贪婪指数。
7. **日线波动率画像**：包含 ATR%、近30天日线TR波动分位、市场状态(trend/range)、以及自适应止盈止损模板。
8. **未来可能性上下文**：包含当月高低点、动态回补目标与其空间、从月高点回撤、月内剩余交易日等。
9. **收益优化上下文**：包含情景概率、分布假设、每个市场的 edge（模型概率-隐含概率）、分数Kelly仓位上限与总风险预算。
10. **上一时间段报告**（若有）：上一轮输出的完整报告。可据此延续判断、调整建议（如上次挂单是否仍有效、建仓建议是否更新等），使本期报告与上期衔接。

# Analysis Framework (COT - Chain of Thought)

请按以下步骤进行深呼吸并思考，不要跳过步骤：

## Step 1: 市场环境与定价偏差 (Market Context)
* 分析 K 线趋势：BTC 是处于上升/下降通道还是震荡？
* **趋势判断**: 结合 K 线和 资金面。判断当前是"下跌中继"、"底部反转"还是"崩盘开始"？K线是放量下跌，缩量下跌，放量上涨，缩量上涨还是其他情况？
* **波动率测算**: 基于 K 线的高低点，估算 BTC 未来的潜在波动范围。它是否有能力会在月内波动 10% 以上？
* **未来路径评估**: 必须同时评估至少三条路径，并给出主观概率区间(合计约100%)：
    * 路径A：延续下行
    * 路径B：震荡后回补（例如回补到当月动态关键阻力）
    * 路径C：快速反弹
* **定价偏差检查**：计算当前合约价格隐含的概率（例如 30¢ = 30%）与基于 K 线技术面判断的概率是否存在显著偏差？
    * *Edge Case*: 如果 BTC 价格只差 1% 就要触发 Strike Price，但合约价格只有 40¢，这是低估还是因为时间不够了？
* **EV 优先级**：必须参考 `profit_optimization_context.top_edge_opportunities`。
    * 优先推荐 edge 为正且明显高于交易摩擦的标的。
    * 对 edge 不明显或为负的标的，明确给出“不参与/减仓”。

## Step 2: 持仓诊断与流动性检查 (Position Diagnosis)
* **安全垫 (Safety Margin)**：(Strike Price - Current BTC Price) / Current BTC Price。
* **Theta (时间价值)**：
    * 对于 OTM (虚值) 的 Yes 合约，时间流逝是致命的 -> 建议尽早止损或轮动。
    * 对于 ITM (实值) 的 Yes 合约，时间流逝是朋友 -> 建议 Hold。。
* **自适应离场规则**：离场阈值必须参考输入中的 `daily_volatility_profile`。
    * 禁止使用固定止盈止损阈值（例如固定 +2%/-2%）。
    * 必须根据 market_regime 与 ATR% 给出动态止盈/止损（可用 xATR 或百分比表达）。
    * 在 Part 2 的每个已参与合约中，补充 `离场风控` 字段。
* **避免过早离场**：
    * 若 `future_possibility_context` 显示距关键位回补空间有限、且月内剩余时间充足，则优先给“继续持有 + 分层离场”，而不是一次性全平。
    * 只有在路径A概率显著占优或关键风控位失守时，才建议快速离场。
    * 禁止把固定整数价位作为默认锚点；关键价位必须来自当月动态结构（如 month_high/month_low/dynamic_reclaim_target/dynamic_key_levels）。
* **仓位约束**：
    * 必须遵守 `profit_optimization_context.risk_budget`（总风险预算、单市场上限、分数Kelly）。
    * 禁止把可用 USDC 一次性重仓到单一方向。

## Step 3: 报告输出结构（四部分）

**Part 1 - 整体分析**：一段话概括市场环境、对持仓的影响、未来 24h 与月内波动预期。

**Part 2 - 当前持仓与挂单分析与建议**：仅针对**已参与的 event**（有持仓或挂单的）。按事件/合约逐条：先写仓位简述（盈亏、风险、是否持有），再给出该合约的**挂买单、挂卖单**具体建议（方向、价格¢、数量或比例、理由）。价格要具体可执行。
其中 `离场风控` 需包含：市场状态、ATR百分比、波动分位、止盈阈值、止损阈值。
并补充 `持有条件` 与 `分层离场计划`，避免全有或全无式建议。

**Part 3 - 建仓建议**：针对**未参与的 event/问题**（事件现价中有但持仓与挂单中未出现的）。结合**当前 USDC 余额**、市场判断与各 outcome 现价，给出是否建仓、方向(Yes/No)、建议价格区间、建议投入金额或比例、理由。投入金额需在余额可承受范围内。
每条建仓建议尽量包含 `预估优势` 和 `建议仓位上限`。

**Part 4 - 预警信号**：BTC 价格向上/向下触及某价位时的操作建议（与现有格式一致）。

若提供了**上一时间段报告**，可在本报告中延续或修正其判断与建议（例如：上期建议 35¢ 挂卖单若未成交可继续持有、或根据最新行情更新建仓区间）。
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

Polymarket 持仓情况和挂单情况: {polymarket_status}

Polymarket 事件与各市场当前价格: {polymarket_event_situation}

当前可用 USDC 余额: {usdc_balance}

比特币过去7天4h K线数据: {btc_4h_k_data}

比特币过去30天1d K线数据: {btc_1d_k_data}

市场情绪与资金面: {market_sentiment_and_funding}

日线波动率画像(用于自适应离场): {daily_volatility_profile}

未来可能性上下文(用于评估是否过早离场): {future_possibility_context}

收益优化上下文(用于最大化期望收益并控制回撤): {profit_optimization_context}

上一时间段报告（仅供参考，可在本报告中延续或调整其判断与建议）:
{previous_report}
"""

MONTHLY_USER_PROMPT_TEMPLATE = """
以下是月初建仓建议所需的输入数据：

BTC 4h K线数据(近7天): {btc_4h_k_data}
BTC 1d K线数据(近30天): {btc_1d_k_data}
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
    btc_1d_k_data: list,
    daily_volatility_profile: dict,
    future_possibility_context: dict,
    profit_optimization_context: dict,
    market_sentiment_and_funding: dict,
    polymarket_event_situation: dict,
    usdc_balance: str,
    previous_report: dict | None = None,
) -> str:
    """根据输入数据生成 user prompt。"""
    event_situation_str = json.dumps(
        polymarket_event_situation, ensure_ascii=False, indent=2
    )
    if previous_report:
        previous_report_str = json.dumps(
            previous_report, ensure_ascii=False, indent=2
        )
    else:
        previous_report_str = "（无）"
    return USER_PROMPT_TEMPLATE.format(
        polymarket_status=polymarket_status,
        polymarket_event_situation=event_situation_str,
        usdc_balance=usdc_balance,
        btc_4h_k_data=btc_4h_k_data,
        btc_1d_k_data=btc_1d_k_data,
        daily_volatility_profile=daily_volatility_profile,
        future_possibility_context=future_possibility_context,
        profit_optimization_context=profit_optimization_context,
        market_sentiment_and_funding=market_sentiment_and_funding,
        previous_report=previous_report_str,
    )


def get_monthly_system_instruction(current_date: str, target_month: str) -> str:
    return MONTHLY_SYSTEM_INSTRUCTION_TEMPLATE.format(
        current_date=current_date,
        target_month=target_month,
    )


def get_monthly_user_prompt(
    btc_4h_k_data: list,
    btc_1d_k_data: list,
    market_sentiment_and_funding: dict,
    derived_summary: dict,
) -> str:
    return MONTHLY_USER_PROMPT_TEMPLATE.format(
        btc_4h_k_data=btc_4h_k_data,
        btc_1d_k_data=btc_1d_k_data,
        market_sentiment_and_funding=market_sentiment_and_funding,
        derived_summary=derived_summary,
    )