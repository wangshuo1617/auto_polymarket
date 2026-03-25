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
                    },
                    "挂单结论": {
                        "type": "string",
                        "description": "针对该合约现有挂单的一句话结论：明确说明是否需要调整、以及如何调整（如「无需调整」「建议撤销 45¢ 卖单改挂 42¢」「建议新增 40¢ 买单」等）。禁止仅写「见下方」或「对照第二部分」。"
                    },
                    "补仓建议": {
                        "type": "string",
                        "description": "针对该合约（原有单）是否需要补仓的一句话结论：如需补仓则写价位/条件与建议补仓量；如不需要则写「无需补仓」或「暂不补仓」及理由。"
                    }
                },
                "required": ["事件或合约", "仓位简述", "挂单建议", "挂单结论", "补仓建议"]
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
        },
        "报告解读附录": {
            "type": "array",
            "description": "把关键建议翻译成可快速执行的一句话",
            "items": {
                "type": "object",
                "properties": {
                    "标的": {"type": "string"},
                    "执行优先级": {"type": "string", "enum": ["立即执行", "挂单等待", "仅观察"]},
                    "一句话结论": {"type": "string"},
                    "执行要点": {"type": "string"}
                },
                "required": ["标的", "执行优先级", "一句话结论", "执行要点"]
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

# 新兴市场可进入性分析（基于市场消息，非简单价格之和）
ENTERABILITY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "opportunities": {
            "type": "array",
            "description": "对每个事件的「可进入性」判定，必须基于近期市场消息/新闻，而非仅看 Yes+No 价格之和",
            "items": {
                "type": "object",
                "properties": {
                    "event_title": {"type": "string", "description": "事件标题"},
                    "event_slug": {"type": "string", "description": "事件 slug，用于链接"},
                    "可进入性": {
                        "type": "string",
                        "enum": ["可考虑进入", "观望", "不建议参与"],
                        "description": "根据市场消息与事件性质判定的可进入性"
                    },
                    "理由_基于市场消息": {
                        "type": "string",
                        "description": "必须基于近期新闻、事件进展、政策或市场情绪等「市场消息」给出理由，禁止仅写「价格之和」或纯数据结论"
                    },
                    "建议方向或观望说明": {
                        "type": "string",
                        "description": "若可考虑进入：建议 Yes/No 及大致价位或区间；若观望/不建议：说明原因或观察要点"
                    },
                    "风险提示": {"type": "string", "description": "该事件或标的的主要风险"},
                    "参考消息摘要": {
                        "type": "string",
                        "description": "简要列出依据的新闻/消息来源或关键词（可用 Google 检索结果）"
                    }
                },
                "required": ["event_title", "event_slug", "可进入性", "理由_基于市场消息", "建议方向或观望说明", "风险提示", "参考消息摘要"]
            }
        }
    },
    "required": ["opportunities"]
}

EVENT_SELECTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "selected_events": {
            "type": "array",
            "description": "从输入事件中筛选出的“可考虑进入且具有获利潜力”的事件",
            "items": {
                "type": "object",
                "properties": {
                    "event_title": {"type": "string"},
                    "event_slug": {"type": "string"},
                    "selection_reason": {"type": "string"},
                    "enterability": {
                        "type": "string",
                        "enum": ["可考虑进入"],
                        "description": "仅允许可考虑进入；观望/不建议参与不应出现在 selected_events 中",
                    },
                    "profit_potential_score": {
                        "type": "integer",
                        "description": "1-100，预期获利潜力评分（越高越有潜力）",
                    },
                    "priority_score": {
                        "type": "integer",
                        "description": "1-100，分数越高优先级越高",
                    },
                },
                "required": [
                    "event_title",
                    "event_slug",
                    "selection_reason",
                    "enterability",
                    "profit_potential_score",
                    "priority_score",
                ],
            },
        }
    },
    "required": ["selected_events"],
}

EVENT_SELECTION_SYSTEM_INSTRUCTION = """# Role
你是 Polymarket 事件筛选器（Event Selector），目标是从大量活跃事件中挑出最值得后续深度分析的一小部分。

# Goal
从输入事件中挑选最合理的候选（例如 20 个），用于后续可进入性深度分析。

# Selection Principles
- 优先考虑：新兴事件、突发事件、与现实新闻进展相关性高的事件。
- 同时考虑：事件描述是否清晰、结算标准是否可验证、市场活跃度（volume24hr/liquidity）是否可交易。
- 尽量覆盖多主题，不要全部集中在同一类题材。
- 只从输入中选，不要杜撰新事件。
- 只选择“可考虑进入且有获利潜力”的事件；排除观望和不建议参与的事件。
- 对获利潜力低、信息噪声大、结算不清晰、流动性差的事件应排除。

# Output Constraints
- 输出必须符合 JSON Schema。
- 仅输出你选中的事件列表及简要选择理由与优先级分。
"""

ENTERABILITY_SYSTEM_INSTRUCTION = """# Role
你是预测市场（Prediction Market）与 Polymarket 的分析师，擅长根据**市场消息、新闻与事件进展**判断某个市场是否值得参与。

# Goal
对给定的 Polymarket 事件列表，**基于近期新闻、政策、事件动态等「市场消息」**判定每个事件的「可进入性」，并给出理由与建议。  
**禁止**仅根据「Yes/No 价格之和」或纯数学套利下结论；必须结合真实世界信息（谁说了什么、发生了什么、时间线、可信度等）判断是否可考虑进入、观望或不建议参与。

# Context
- 输入中会提供每个事件的标题、描述、结算来源、以及各子市场的 outcome 名称与价格（含 Yes/No 或多种结果）。
- 输入还会提供事件元数据（如 `new`、`featured`、`volume24hr`、`updatedAt`），用于识别新兴或突发事件优先级。
- 价格数据仅作参考，用于了解市场当前定价；**判定可进入性的核心依据必须是市场消息**。
- 请使用 Google 搜索（已启用）检索与事件相关的近期新闻或进展，在「理由_基于市场消息」和「参考消息摘要」中体现。
- 请优先分析：突发事件、短时新上线事件、近期更新频繁且成交快速放大的事件；并明确指出“市场叙事”与“现实进展”之间是否存在偏差。

# Output
对每个事件输出：可进入性（可考虑进入/观望/不建议参与）、理由_基于市场消息、建议方向或观望说明、风险提示、参考消息摘要。  
必须覆盖全部输入事件，不允许漏项。若证据不足，必须输出「观望」并写明缺失的关键信息。
"""


def get_enterability_user_prompt(events_summary: list, current_date: str) -> str:
    """生成可进入性分析的用户 prompt，events_summary 每项含 title, slug, description, resolutionSource, markets(含 question, outcomes, outcomePrices)。"""
    parts = [
        f"当前日期：{current_date}。请根据**市场消息与新闻**（非仅价格数据）对以下 Polymarket 事件判定可进入性，并输出结构化 JSON。",
        "",
        "事件列表（含描述与各市场价格，价格仅作参考）：",
        ""
    ]
    total = len(events_summary)
    for i, ev in enumerate(events_summary, 1):
        title = ev.get("title") or "—"
        slug = ev.get("slug") or "—"
        desc = (ev.get("description") or "")[:800]
        resolution = ev.get("resolutionSource") or "—"
        is_new = ev.get("new")
        featured = ev.get("featured")
        vol24 = ev.get("volume24hr")
        vol = ev.get("volume")
        liq = ev.get("liquidity")
        created_at = ev.get("createdAt") or "—"
        updated_at = ev.get("updatedAt") or "—"
        start_at = ev.get("startDate") or "—"
        parts.append(f"--- 事件 {i}: {title} (slug: {slug}) ---")
        if desc:
            parts.append(f"描述: {desc}")
        parts.append(f"结算依据: {resolution}")
        parts.append(
            f"事件元数据: new={is_new}, featured={featured}, volume24hr={vol24}, volume={vol}, liquidity={liq}, createdAt={created_at}, updatedAt={updated_at}, startDate={start_at}"
        )
        for m in ev.get("markets") or []:
            q = m.get("question") or "—"
            outcomes = m.get("outcomes") or []
            prices = m.get("outcomePrices") or []
            price_str = " / ".join(f"{o}: {p}" for o, p in zip(outcomes, prices)) if outcomes and prices else str(prices)
            parts.append(f"  - {q} 价格: {price_str}")
        parts.append("")
    parts.append("")
    parts.append("严格输出要求：")
    parts.append(f"1) 必须覆盖全部输入事件，共 {total} 条；每条事件输出一条机会判断。")
    parts.append("2) 任何事件证据不足时，不可省略，必须输出“可进入性=观望”，并说明缺失证据。")
    parts.append("3) 事件标题和 slug 请与输入保持一致，避免改写。")
    parts.append("4) 优先对新兴/突发事件给出更详细理由，但其余事件也必须给结论。")
    return "\n".join(parts)


def get_event_selection_user_prompt(
    events_summary: list,
    current_date: str,
    target_count: int,
) -> str:
    parts = [
        f"当前日期：{current_date}。请从以下事件中筛选出最多 {target_count} 个“可考虑进入且具备获利潜力”的事件，并输出结构化 JSON。",
        "",
        "筛选时优先考虑：新兴/突发、现实世界新闻相关性、结算清晰度、活跃度与可交易性。",
        "必须排除：观望、不建议参与、获利潜力不足、信息不确定性过高的事件。",
        "",
    ]
    for i, ev in enumerate(events_summary, 1):
        title = ev.get("title") or "—"
        slug = ev.get("slug") or "—"
        desc = (ev.get("description") or "")[:400]
        resolution = ev.get("resolutionSource") or "—"
        parts.append(
            f"[{i}] title={title} | slug={slug} | new={ev.get('new')} | featured={ev.get('featured')} | "
            f"volume24hr={ev.get('volume24hr')} | volume={ev.get('volume')} | liquidity={ev.get('liquidity')}"
        )
        if desc:
            parts.append(f"    desc={desc}")
        parts.append(f"    resolution={resolution}")
    parts.append("")
    parts.append(f"请输出不超过 {target_count} 个 selected_events，且只能从以上输入中选择。")
    parts.append("若满足条件的事件不足，允许少于目标数量（宁缺毋滥，禁止凑数）。")
    return "\n".join(parts)


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
**每条必须给出 `补仓建议`**：明确该合约（原有单）**是否需要补仓**；如需补仓则写价位/条件与建议补仓量；如不需要则写「无需补仓」或「暂不补仓」及理由。不得省略。
**每条必须给出 `挂单结论`**：一句话明确说明该合约现有挂单**是否需要调整**以及**如何调整**（例如「无需调整」「建议撤销 45¢ 卖单改挂 42¢」「建议新增 40¢ 买单」）。禁止仅写「见下方」或「对照第二部分」。
其中 `离场风控` 需包含：市场状态、ATR百分比、波动分位、止盈阈值、止损阈值。
并补充 `持有条件` 与 `分层离场计划`，避免全有或全无式建议。

**Part 3 - 建仓建议**：针对**未参与的 event/问题**（事件现价中有但持仓与挂单中未出现的）。结合**当前 USDC 余额**、市场判断与各 outcome 现价，给出是否建仓、方向(Yes/No)、建议价格区间、建议投入金额或比例、理由。投入金额需在余额可承受范围内。
每条建仓建议尽量包含 `预估优势` 和 `建议仓位上限`。

**Part 4 - 预警信号**：BTC 价格向上/向下触及某价位时的操作建议（与现有格式一致）。

**Part 5 - 报告解读附录（新增）**：输出 3-8 条“可执行一句话”，每条包含：`标的`、`执行优先级`、`一句话结论`、`执行要点`。
`执行优先级` 只能使用：`立即执行`、`挂单等待`、`仅观察`。
优先覆盖：edge最高的建仓建议、风险最高的持仓、最关键的预警。

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


# ---------- 原油 (WTI) Polymarket 分析 ----------
OIL_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "整体分析": {
            "type": "string",
            "description": "一段话概括原油市场环境：供需、地缘、WTI K 线等。简述对持仓影响、未来 24h 与月内 WTI 波动预期。"
        },
        "当前持仓与挂单分析与建议": {
            "type": "array",
            "description": "针对已参与的原油 event（有持仓或挂单的），按 event/合约逐条给出仓位简述与挂单建议",
            "items": {
                "type": "object",
                "properties": {
                    "事件或合约": {"type": "string"},
                    "仓位简述": {"type": "string"},
                    "持有条件": {"type": "string"},
                    "分层离场计划": {"type": "string"},
                    "挂单建议": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "操作类型": {"type": "string", "enum": ["挂买单", "挂卖单"]},
                                "方向": {"type": "string", "enum": ["Yes", "No"]},
                                "建议价格": {"type": "number"},
                                "建议数量或比例": {"type": "string"},
                                "触发条件": {"type": "string", "description": "例如 WTI 上破/下破某价位后执行"},
                                "理由": {"type": "string"}
                            },
                            "required": ["操作类型", "方向", "建议价格", "理由"]
                        }
                    },
                    "离场风控": {
                        "type": "object",
                        "properties": {
                            "市场状态": {"type": "string", "enum": ["trend_up", "trend_down", "range", "unknown"]},
                            "ATR百分比": {"type": "number"},
                            "波动分位": {"type": "number"},
                            "止盈阈值": {"type": "string"},
                            "止损阈值": {"type": "string"}
                        }
                    },
                    "挂单结论": {"type": "string"},
                    "补仓建议": {
                        "type": "string",
                        "description": "针对该合约（原有单）是否需要补仓的一句话结论：如需补仓则写价位/条件与建议补仓量；如不需要则写「无需补仓」或「暂不补仓」及理由。"
                    }
                },
                "required": ["事件或合约", "仓位简述", "挂单建议", "挂单结论", "补仓建议"]
            }
        },
        "建仓建议": {
            "type": "array",
            "description": "针对当前原油事件中用户尚未参与的 market/问题，结合 USDC 余额与各 outcome 现价给出建仓建议",
            "items": {
                "type": "object",
                "properties": {
                    "事件或问题": {"type": "string"},
                    "建议方向": {"type": "string", "enum": ["Yes", "No"]},
                    "建议价格区间": {"type": "string"},
                    "建议投入金额或比例": {"type": "string"},
                    "预估优势": {"type": "string"},
                    "建议仓位上限": {"type": "string"},
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
                    "价格": {"type": "number", "description": "WTI 原油价格，美元/桶"},
                    "操作建议": {"type": "string"},
                    "关联止盈止损": {"type": "string"}
                }
            }
        },
        "报告解读附录": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "标的": {"type": "string"},
                    "执行优先级": {"type": "string", "enum": ["立即执行", "挂单等待", "仅观察"]},
                    "一句话结论": {"type": "string"},
                    "执行要点": {"type": "string"}
                },
                "required": ["标的", "执行优先级", "一句话结论", "执行要点"]
            }
        }
    },
    "required": ["整体分析", "当前持仓与挂单分析与建议", "建仓建议", "预警信号"]
}

OIL_SYSTEM_INSTRUCTION_TEMPLATE = """# Role
你是资深原油与预测市场（Prediction Market）分析师，熟悉 WTI 期货、CME 结算价与 Polymarket 原油类事件的规则。

# Goal
根据【Polymarket 原油持仓】、【挂单】、【WTI K 线及价格数据】，对账户进行风险敞口分析，并给出可执行的限价单管理策略。

# Context
* 当前时间：{current_date}
* K 线格式与 Binance 一致： [open_time(ms), Open, High, Low, Close, Volume, ...]。

# Resolution Rules（必须严格按此规则理解「Hit」与结算）
本类市场采用以下 CME 官方规则，分析时须据此判断 Yes/No 概率与建议：

1. **结算条件**：若在到期月（如 March 2026）的**最后一个交易日之前**的**任意一个交易日**，CME 公布的 **Active Month（ front month）原油 CL 期货的官方结算价** 达到或超过该市场所标价格，则结算为 **Yes**；否则为 **No**。即：只要月内有一天结算价 ≥ 目标价即 Yes，不必等到月末。

2. **Active Month 定义**：Active Month 为 CME 所列合约月份中**最近月**。在 spot month 到期日**前两个交易日**起，下一列出的合约视为新的 Active Month（例如 spot 周五到期，则周三起下一合约为 Active Month）。

3. **仅计官方结算价**：只采用 CME Group 公布的 **Active Month 官方日结算价（Settlement）**。盘中成交价、最高/最低价、买卖盘、中间价或任何指示性价格**均不计入**。结算价可能与当日最后一笔成交价不同；CME 的结算价方法论因品种/合约可能不同。

4. **计日规则**：仅当 CME 当日发布了该 Active Month 的官方结算价时，该日才计入。周末、节假日或市场休市日（无结算价发布）**不计算**。

5. **裁决来源**：以 CME Group 官网该交易日**首次发布**的 Active Month 原油 CL 期货「Settlement」价格为准；之后任何修正或更新不影响本市场结算。

分析时请据此理解：例如「Will CL hit $80 by end of March?」表示三月内**任一日**官方结算价 ≥ 80 即 Yes，因此概率评估应基于「月内是否会出现至少一天结算 ≥ 目标价」，而非仅看月末一日。

# Input Data
1. 持仓与挂单（仅原油相关 event）。
2. 原油市场简要背景：`wti_price_usd_per_bbl` 来自 Yahoo Finance (CL=F)。
3. Polymarket 原油事件与各市场当前价格。
4. 当前可用 USDC 余额。
5. WTI 过去 7 天 4h K 线 + 过去 30 天 1d K 线。
6. 日线波动率画像（ATR%、波动分位、自适应止盈止损模板）。
7. 未来可能性上下文（月高/月低、回撤、关键价位）。
8. 收益优化上下文（edge、Kelly 等）。
9. 上一时间段报告（若有）。

# 实时新闻与地缘政治（必须检索并纳入分析）
* **必须使用检索工具** 查询近期伊朗与原油相关的实时新闻与动态（如：伊朗石油产量/出口、制裁与豁免、中东局势、OPEC+ 与伊朗表态等），并将检索结果纳入整体分析与建议。
* 在 **Part 1 整体分析** 中须包含一段「地缘与新闻」：概括当前伊朗/中东等地缘与原油供需相关的最新动态，以及对 WTI 与 Polymarket 原油市场的影响（上行/下行/震荡风险）。
* 建仓与持仓建议须结合上述新闻与地缘变化，避免仅依赖技术面。

# Analysis
* 结合 WTI 趋势与波动率，评估各 outcome 的隐含概率与模型概率偏差。
* 离场与挂单建议需参考 daily_volatility_profile，避免固定百分比止盈止损。
* 预警信号中的「价格」为 WTI 美元/桶。

# Output
Part 1 整体分析（须含地缘与新闻小结）；Part 2 持仓与挂单分析与建议（每条须含**补仓建议**：该合约是否需要补仓、价位/条件与建议补仓量或「无需补仓」及理由；含挂单结论、离场风控）；Part 3 建仓建议；Part 4 预警信号（WTI 价格）；Part 5 报告解读附录。输出必须符合 JSON Schema。

额外约束（去锚定）：
1) 在整体分析开头先写「本轮新证据摘要」（先说本轮新增信息，再给结论）。
2) 若与上一轮建议存在方向或优先级冲突，必须明确给出变化原因（如新闻变化、价格结构变化、波动率变化）。
"""

OIL_USER_PROMPT_TEMPLATE = """
以下是当前要分析的具体信息（Polymarket 原油类 event）：

Polymarket 持仓情况和挂单情况: {polymarket_status}

Polymarket 原油事件与各市场当前价格: {polymarket_event_situation}

当前可用 USDC 余额: {usdc_balance}

WTI 过去7天 4h K线数据: {oil_4h_k_data}

WTI 过去30天 1d K线数据: {oil_1d_k_data}

原油市场简要背景: {oil_market_context}

日线波动率画像: {daily_volatility_profile}

未来可能性上下文: {future_possibility_context}

收益优化上下文: {profit_optimization_context}

上一时间段报告:
{previous_report}
"""


def get_oil_system_instruction(current_date: str) -> str:
    return OIL_SYSTEM_INSTRUCTION_TEMPLATE.format(current_date=current_date)


def get_oil_user_prompt(
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
) -> str:
    event_situation_str = json.dumps(polymarket_event_situation, ensure_ascii=False, indent=2)
    previous_report_str = json.dumps(previous_report, ensure_ascii=False, indent=2) if previous_report else "（无）"
    return OIL_USER_PROMPT_TEMPLATE.format(
        polymarket_status=polymarket_status,
        polymarket_event_situation=event_situation_str,
        usdc_balance=usdc_balance,
        oil_4h_k_data=oil_4h_k_data,
        oil_1d_k_data=oil_1d_k_data,
        daily_volatility_profile=daily_volatility_profile,
        future_possibility_context=future_possibility_context,
        profit_optimization_context=profit_optimization_context,
        oil_market_context=oil_market_context,
        previous_report=previous_report_str,
    )


# ---------- 黄金 (GC) Polymarket 分析 ----------
GOLD_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "整体分析": {
            "type": "string",
            "description": "一段话概括黄金市场环境：美联储利率预期、美元、地缘、GC K 线等。简述对持仓影响、未来 24h 与月内 GC 波动预期。"
        },
        "当前持仓与挂单分析与建议": {
            "type": "array",
            "description": "针对已参与的黄金 event（有持仓或挂单的），按 event/合约逐条给出仓位简述与挂单建议",
            "items": {
                "type": "object",
                "properties": {
                    "事件或合约": {"type": "string"},
                    "仓位简述": {"type": "string"},
                    "持有条件": {"type": "string"},
                    "分层离场计划": {"type": "string"},
                    "挂单建议": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "操作类型": {"type": "string", "enum": ["挂买单", "挂卖单"]},
                                "方向": {"type": "string", "enum": ["Yes", "No"]},
                                "建议价格": {"type": "number"},
                                "建议数量或比例": {"type": "string"},
                                "触发条件": {"type": "string", "description": "例如 GC 上破/下破某价位后执行"},
                                "理由": {"type": "string"}
                            },
                            "required": ["操作类型", "方向", "建议价格", "理由"]
                        }
                    },
                    "离场风控": {
                        "type": "object",
                        "properties": {
                            "市场状态": {"type": "string", "enum": ["trend_up", "trend_down", "range", "unknown"]},
                            "ATR百分比": {"type": "number"},
                            "波动分位": {"type": "number"},
                            "止盈阈值": {"type": "string"},
                            "止损阈值": {"type": "string"}
                        }
                    },
                    "挂单结论": {"type": "string"},
                    "补仓建议": {
                        "type": "string",
                        "description": "针对该合约（原有单）是否需要补仓的一句话结论：如需补仓则写价位/条件与建议补仓量；如不需要则写「无需补仓」或「暂不补仓」及理由。"
                    }
                },
                "required": ["事件或合约", "仓位简述", "挂单建议", "挂单结论", "补仓建议"]
            }
        },
        "建仓建议": {
            "type": "array",
            "description": "针对当前黄金事件中用户尚未参与的 market/问题，结合 USDC 余额与各 outcome 现价给出建仓建议",
            "items": {
                "type": "object",
                "properties": {
                    "事件或问题": {"type": "string"},
                    "建议方向": {"type": "string", "enum": ["Yes", "No"]},
                    "建议价格区间": {"type": "string"},
                    "建议投入金额或比例": {"type": "string"},
                    "预估优势": {"type": "string"},
                    "建议仓位上限": {"type": "string"},
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
                    "价格": {"type": "number", "description": "COMEX 黄金 GC 价格，美元/盎司"},
                    "操作建议": {"type": "string"},
                    "关联止盈止损": {"type": "string"}
                }
            }
        },
        "报告解读附录": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "标的": {"type": "string"},
                    "执行优先级": {"type": "string", "enum": ["立即执行", "挂单等待", "仅观察"]},
                    "一句话结论": {"type": "string"},
                    "执行要点": {"type": "string"}
                },
                "required": ["标的", "执行优先级", "一句话结论", "执行要点"]
            }
        }
    },
    "required": ["整体分析", "当前持仓与挂单分析与建议", "建仓建议", "预警信号"]
}

GOLD_SYSTEM_INSTRUCTION_TEMPLATE = """# Role
你是资深黄金与预测市场（Prediction Market）分析师，熟悉 COMEX 黄金期货 GC、CME 结算价与 Polymarket 黄金类事件的规则。

# Goal
根据【Polymarket 黄金持仓】、【挂单】、【GC K 线及价格数据】，对账户进行风险敞口分析，并给出可执行的限价单管理策略。

# Context
* 当前时间：{current_date}
* K 线格式与 Binance 一致： [open_time(ms), Open, High, Low, Close, Volume, ...]。

# Resolution Rules（必须严格按此规则理解「Hit」与结算）
本类市场采用以下 CME 官方规则，分析时须据此判断 Yes/No 概率与建议。市场有两种类型：

**↑ (above) 型市场**：若在 March 2026 的**最后一个交易日之前**的**任意一个交易日**，CME 公布的 **Active Month 黄金 GC 期货的官方结算价** 达到或**超过**该市场所标价格，则结算为 **Yes**；否则为 **No**。即：月内任一日结算价 ≥ 目标价即 Yes。

**↓ (below) 型市场**：若在 March 2026 的**最后一个交易日之前**的**任意一个交易日**，CME 公布的 **Active Month 黄金 GC 期货的官方结算价** 达到或**低于**该市场所标价格，则结算为 **Yes**；否则为 **No**。即：月内任一日结算价 ≤ 目标价即 Yes。

1. **Active Month 定义**：Active Month 为 CME  designated delivery-cycle 月份（Feb, Apr, Jun, Aug, Oct, Dec）中**最近月**且**非 spot month**。在 First Position Date 自动切换为下一 eligible 合约月。

2. **仅计官方结算价**：只采用 CME Group 公布的 **Active Month 官方日结算价（Settlement）**。盘中成交价、最高/最低价、买卖盘、中间价或任何指示性价格**均不计入**。结算价可能与当日最后一笔成交价不同。

3. **计日规则**：仅当 CME 当日发布了该 Active Month 的官方结算价时，该日才计入。周末、节假日或市场休市日（无结算价发布）**不计算**。

4. **裁决来源**：以 CME Group 官网该交易日**首次发布**的 Active Month 黄金 GC 期货「Settlement」价格为准；之后任何修正或更新不影响本市场结算。

分析时请根据 question 中的 ↑/↓ 或 above/below 判断该市场类型，正确评估 Yes 概率。

# Input Data
1. 持仓与挂单（仅黄金相关 event）。
2. 黄金市场简要背景：`gc_price_usd_per_oz` 来自 Yahoo Finance (GC=F)。
3. Polymarket 黄金事件与各市场当前价格。
4. 当前可用 USDC 余额。
5. GC 过去 7 天 4h K 线 + 过去 30 天 1d K 线。
6. 日线波动率画像（ATR%、波动分位、自适应止盈止损模板）。
7. 未来可能性上下文（月高/月低、回撤、关键价位）。
8. 收益优化上下文（edge、Kelly 等）。
9. 上一时间段报告（若有）。

# 实时新闻与宏观（必须检索并纳入分析）
* **必须使用检索工具** 查询近期黄金、美联储、美元、地缘相关的实时新闻与动态（如：FOMC 决议、CPI、非农、央行购金、地缘风险等），并将检索结果纳入整体分析与建议。
* 在 **Part 1 整体分析** 中须包含一段「宏观与新闻」：概括当前利率预期、美元、地缘、央行购金等对 GC 与 Polymarket 黄金市场的影响。
* 建仓与持仓建议须结合上述新闻与宏观变化，避免仅依赖技术面。

# Analysis
* 结合 GC 趋势与波动率，评估各 outcome 的隐含概率与模型概率偏差。
* 离场与挂单建议需参考 daily_volatility_profile，避免固定百分比止盈止损。
* 预警信号中的「价格」为 GC 美元/盎司。

# Output
Part 1 整体分析（须含宏观与新闻小结）；Part 2 持仓与挂单分析与建议（每条须含**补仓建议**：该合约是否需要补仓、价位/条件与建议补仓量或「无需补仓」及理由；含挂单结论、离场风控）；Part 3 建仓建议；Part 4 预警信号（GC 价格）；Part 5 报告解读附录。输出必须符合 JSON Schema。

额外约束（去锚定）：
1) 在整体分析开头先写「本轮新证据摘要」（先说本轮新增信息，再给结论）。
2) 若与上一轮建议存在方向或优先级冲突，必须明确给出变化原因（如新闻变化、价格结构变化、波动率变化）。
"""

GOLD_USER_PROMPT_TEMPLATE = """
以下是当前要分析的具体信息（Polymarket 黄金类 event）：

Polymarket 持仓情况和挂单情况: {polymarket_status}

Polymarket 黄金事件与各市场当前价格: {polymarket_event_situation}

当前可用 USDC 余额: {usdc_balance}

GC 过去7天 4h K线数据: {gold_4h_k_data}

GC 过去30天 1d K线数据: {gold_1d_k_data}

黄金市场简要背景: {gold_market_context}

日线波动率画像: {daily_volatility_profile}

未来可能性上下文: {future_possibility_context}

收益优化上下文: {profit_optimization_context}

上一时间段报告:
{previous_report}
"""


def get_gold_system_instruction(current_date: str) -> str:
    return GOLD_SYSTEM_INSTRUCTION_TEMPLATE.format(current_date=current_date)


def get_gold_user_prompt(
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
) -> str:
    event_situation_str = json.dumps(polymarket_event_situation, ensure_ascii=False, indent=2)
    previous_report_str = json.dumps(previous_report, ensure_ascii=False, indent=2) if previous_report else "（无）"
    return GOLD_USER_PROMPT_TEMPLATE.format(
        polymarket_status=polymarket_status,
        polymarket_event_situation=event_situation_str,
        usdc_balance=usdc_balance,
        gold_4h_k_data=gold_4h_k_data,
        gold_1d_k_data=gold_1d_k_data,
        daily_volatility_profile=daily_volatility_profile,
        future_possibility_context=future_possibility_context,
        profit_optimization_context=profit_optimization_context,
        gold_market_context=gold_market_context,
        previous_report=previous_report_str,
    )