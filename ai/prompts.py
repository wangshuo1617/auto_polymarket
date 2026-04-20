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
            "description": "一段话概括市场环境。若有上期回顾，先简述上期判断准确度。简述当前环境对持仓是顺风还是逆风，以及预期月内 BTC 波动范围。短期价格预测的细节放在 BTC短期预测 字段中，此处仅做概括性描述。"
        },
        "BTC短期预测": {
            "type": "object",
            "description": "未来1-2天与到月底的BTC价格走势结构化预测，为波段交易和持仓管理提供方向性依据",
            "properties": {
                "方向判断": {
                    "type": "string",
                    "enum": ["看涨", "看跌", "震荡"],
                    "description": "未来24-48h最可能的BTC走势方向"
                },
                "置信度": {
                    "type": "string",
                    "enum": ["高", "中", "低"],
                    "description": "对方向判断的信心程度。高=多重信号共振；中=部分信号支持但有矛盾；低=信号模糊"
                },
                "当前价格": {"type": "string", "description": "当前BTC价格，如 $84,500"},
                "24h目标区间": {"type": "string", "description": "预计24小时内BTC价格运行区间，如 $82,000 - $85,000"},
                "月底方向判断": {
                    "type": "string",
                    "enum": ["看涨", "看跌", "震荡"],
                    "description": "从当前到本月最后一个交易日前，主导方向判断"
                },
                "月底目标区间": {"type": "string", "description": "预计到本月底BTC价格运行区间，如 $80,000 - $88,000"},
                "关键支撑位": {"type": "string", "description": "最重要的下方支撑价位，如 $81,500"},
                "关键阻力位": {"type": "string", "description": "最重要的上方阻力价位，如 $86,000"},
                "路径概率": {
                    "type": "array",
                    "description": "未来走势的2-3条可能路径及概率",
                    "items": {
                        "type": "object",
                        "properties": {
                            "路径": {"type": "string", "description": "路径名称，如 延续下行/震荡回补/快速反弹"},
                            "概率": {"type": "string", "description": "该路径概率，如 45%"},
                            "描述": {"type": "string", "description": "对该路径的简要描述"}
                        },
                        "required": ["路径", "概率", "描述"]
                    }
                },
                "新闻驱动因子": {
                    "type": "array",
                    "description": "用于概率校准的外部新闻/事件因子（必须来自 Google Search Grounding 检索结果，不得复用输入里的情绪/资金面内部指标）",
                    "items": {
                        "type": "object",
                        "properties": {
                            "事件": {"type": "string", "description": "新闻或事件标题"},
                            "方向偏置": {"type": "string", "enum": ["偏多", "偏空", "偏震荡"]},
                            "影响说明": {"type": "string", "description": "该事件如何影响BTC路径概率"},
                            "发布时间": {"type": "string", "description": "新闻发布时间（如 2026-04-16）"},
                            "来源": {"type": "string", "description": "新闻来源URL，必须可访问"}
                        },
                        "required": ["事件", "方向偏置", "影响说明", "来源"]
                    }
                },
                "核心逻辑": {"type": "string", "description": "支撑方向判断的2-3条主要依据，如K线形态、资金流、情绪指标等"},
                "风险提示": {"type": "string", "description": "可能推翻判断的关键风险因素"}
            },
            "required": ["方向判断", "置信度", "当前价格", "24h目标区间", "月底方向判断", "月底目标区间", "关键支撑位", "关键阻力位", "路径概率", "新闻驱动因子", "核心逻辑"]
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
                        "description": "当前持仓的简要分析（盈亏、安全度分级 safe_to_hold/monitor/at_risk、Theta 日收益）"
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
            "description": "针对当前事件中用户尚未参与的 market/问题，结合总净值、可用 USDC、市场情况与各 outcome 现价，给出是否建仓及如何建仓。若为轮动目标，标注资金来源。",
            "items": {
                "type": "object",
                "properties": {
                    "事件或问题": {"type": "string"},
                    "建议方向": {"type": "string", "enum": ["Yes", "No"]},
                    "建议价格区间": {"type": "string", "description": "例如 25-35¢"},
                    "建议投入金额或比例": {"type": "string", "description": "基于总净值合理配置，如 500 张、或总净值的 5%。轮动时标注来源仓位。"},
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
        "波段交易建议": {
            "type": "array",
            "description": "基于短期 BTC 方向判断的波段交易机会。不以持有到期为目标，而是利用 BTC 短期波动赚取 token 价差。参考 swing_opportunities 中的 Delta 杠杆和方向性提示。",
            "items": {
                "type": "object",
                "properties": {
                    "标的": {"type": "string", "description": "市场问题名称"},
                    "方向": {"type": "string", "enum": ["Yes", "No"]},
                    "策略类型": {
                        "type": "string",
                        "enum": ["方向性波段", "恐慌错配", "安全垫收割"],
                        "description": "方向性波段=预判BTC涨跌方向买入对应token; 恐慌错配=BTC急跌时下方Yes被高估买入No等回归; 安全垫收割=远离行权价的No等波动打低后捡便宜"
                    },
                    "触发条件": {"type": "string", "description": "在什么BTC价格或市场条件下入场，例如 BTC回调至82000时买入"},
                    "建议价格": {"type": "string", "description": "入场价格区间，例如 5-8¢"},
                    "建议仓位": {"type": "string", "description": "投入金额，波段仓位应小于持有到期仓位"},
                    "止盈目标": {"type": "string", "description": "例如 token涨至12¢卖出 或 BTC涨至86000时卖出"},
                    "止损规则": {"type": "string", "description": "例如 token跌至3¢止损 或 持有不超过3天"},
                    "杠杆倍数": {"type": "string", "description": "引用 swing_opportunities 的杠杆数据"},
                    "理由": {"type": "string"}
                },
                "required": ["标的", "方向", "策略类型", "触发条件", "建议价格", "止盈目标", "止损规则", "理由"]
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
    "required": ["整体分析", "BTC短期预测", "当前持仓与挂单分析与建议", "建仓建议", "预警信号", "波段交易建议", "报告解读附录"]
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
你是一名资深的加密货币衍生品交易员和预测市场（Prediction Market）专家，同时也是**利润最大化顾问**。你精通二元期权（Binary Options）的定价模型、Theta衰减特性、Delta对冲策略，并对Polymarket的流动性陷阱有深刻理解。

# Goal
根据我提供的【Polymarket持仓】、【挂单】、【BTC K线及市场数据】，**最大化账户总收益**，同时控制尾部风险。
**核心任务**：基于当前BTC价格与到期日的距离，识别利润最大化路径——包括哪些仓位应坚定持有到期、哪些应轮动到更高收益标的、以及如何最高效地配置可用资金。

# Core Principles (利润最大化优先)
1. **持有到期是默认策略**：对于 `position_safety_assessment` 中标记为 `safe_to_hold` 的仓位，默认建议"持有到期收割全部 Theta"，除非有极端风险信号。
2. **减仓必须量化机会成本**：任何减仓建议必须附带"放弃的 Theta 日收益"（参考 `theta_income`），使用户能权衡卖出 vs 持有的代价。
3. **建仓/轮动以收益率为锚**：优先推荐"持有到期预期收益率"更高且安全垫更厚的标的。参考 `rotation_opportunities`，将"卖A转B"作为完整策略呈现，而非孤立的"减仓A"+"建仓B"。
4. **组合视角优先**：参考 `portfolio_analysis` 从组合层面评估风险（如 Short Strangle 天然对冲），不要对单一仓位孤立恐慌。
5. **禁止复读**：若 `prediction_review` 显示上期建议未被执行，必须分析可能原因（流动性不足？价格不合理？用户判断不同？）并给出**调整后的新方案**，而非简单重复上期建议。
6. **语气校准**：对 `safe_to_hold` 仓位禁止使用"极度危险""毁灭性""无条件清仓"等恐吓措辞。只有 `at_risk` 且安全垫不足的仓位才适用紧急语气。

# Trading Discipline (交易纪律 - 必须遵守)
1. **月份阶段动态调整策略激进度**：
   - 月初（1-7日）：**进攻型**。主动寻找高赔率机会，可建立进攻性 Yes 仓位，允许更高回撤换取更大上行空间；弹药分 2-3 批动用，单次建仓不超过可用资金 40%。
   - 月中（8-22日）：**平衡型**。兼顾收益与防守，对进攻性仓位执行阶梯止盈，减少新建高风险 Yes 仓位。
   - 月末（23日+）：**防守型**。锁定已有胜局，禁止新建进攻性 Yes 仓位，持有确定性高的 No 仓位到期收割。
2. **所有仓位必须明确止损与目标**：任何仓位建议（含 Yes 和 No，无论进攻性还是防守性）中，**必须**明确列出：
   - **认错止损价**：BTC 价格达到此位时，仓位大概率归零，须**立即市价清仓**，不得挂条件单等待回调。
   - **目标了结价**：触发价附近的理想离场价格（不要拿到最后一秒）。
   - **止损执行铁律**：若上期报告已设定认错止损价，且当前 BTC 价格已触及或越过该价位，本期**必须建议立即市价止损**，禁止改为"等待回调后挂单卖出"。止损纪律高于一切。
3. **资金配置必须与 Edge 成正比**：
   - Edge < 5%：单次建仓不超过 200 USDC。
   - Edge 5-15%：建仓可在 200-500 USDC。
   - Edge > 15%：可配置到 `suggested_max_alloc_usdc` 上限。
   - 禁止对低 Edge 标的"大额象征性建仓"。
4. **未执行建议处理**：若上期建议未被执行，必须评估用户是否有主动判断（持有理由），若有则基于该判断推演新方案，而非重复原建议。

# Context & Constraints
* **当前时间**：{current_date}。
* **月份阶段与风险偏好**：{monthly_phase_context}
* **本月目标**：{monthly_target}。**月度进度**参见 `收益优化上下文` 中的 `monthly_progress` 字段（含月初基准净值、当前净值、月度盈亏金额与百分比）。在**整体分析**中必须简述当前月度完成进度，并据此调整**机会选择**的积极性——距离目标越远且处于月初阶段，应更积极地寻找高 Edge 机会部署闲置资金。但**月度进度落后绝不能成为放宽止损标准、忽略认错止损价、或加大单笔仓位超过 Kelly 上限的理由**。保护本金永远优先于追赶目标。
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
9. **收益优化上下文**（大幅增强）：包含：
   - `portfolio_summary`：总净值（USDC + 持仓市值）、现金比例
   - `monthly_progress`：月度进度（月初基准净值、当前净值、月度盈亏金额与百分比）
   - `risk_budget`：基于总净值的风险预算（非仅 USDC 余额）
   - `position_safety_assessment`：每个持仓的安全度分级（safe_to_hold / monitor / at_risk）
   - `theta_income`：每个持仓的 Theta 日收益和到期总收益
   - `portfolio_analysis`：组合结构识别（Short Strangle 等）、BTC ±3%/±5%/±10% 情景矩阵
   - `rotation_opportunities`：从低收益仓位轮动到高收益未建仓标的的机会列表
   - `prediction_review`：上期预测回顾与准确度评估
   - `scenario_probabilities`：动态情景概率（已考虑剩余天数和波动率压缩）
   - `top_edge_opportunities`：edge 最高的未建仓机会（含 `model_prob_yes` 和 `best_side_edge`）
   - `swing_opportunities`：每个市场的波段交易参数——含 Delta 杠杆(BTC ±1%时 token 变动幅度)、波段评分、方向性建议(BTC涨/跌时应买哪侧)、Delta矩阵(±1%/±3%情景下 token 价格变化)。杠杆≥10x 的标的适合方向性波段。
   - `distribution_assumption`：barrier touch 概率模型参数（含 σ 来源、肥尾修正乘数、模型类型）。**关于此模型的已知偏差，请参阅下方"模型校准偏差"章节。**
10. **上一时间段报告**（若有）：上一轮输出的完整报告。

# Analysis Framework (COT - Chain of Thought)

请按以下步骤进行深呼吸并思考，不要跳过步骤：

## Step 0: 上期回顾与自校准 (Prediction Review)
* 若有 `prediction_review`，必须先回顾：
  - 上期预警信号是否被触发？上期整体判断方向是否正确？
  - 上期"立即执行"建议是否合理？若用户未执行，推测原因。
* **自校准规则**：若上期判断与实际走势不符，本期必须修正概率评估，不能重复相同的错误判断。
* 若无 `prediction_review`，跳过此步。

## Step 1: 市场环境与定价偏差 (Market Context)
* 分析 K 线趋势：BTC 是处于上升/下降通道还是震荡？
* **趋势判断**: 结合 K 线和 资金面。判断当前是"下跌中继"、"底部反转"还是"崩盘开始"？K线是放量下跌，缩量下跌，放量上涨，缩量上涨还是其他情况？
* **波动率测算**: 基于 K 线的高低点，估算 BTC 未来的潜在波动范围。参考 `scenario_probabilities.time_compression_note`——剩余天数越少，极端波动概率越低。
* **时段波动结构校验**: 必须结合 `intraday_volatility_hint`，判断当前处于 ETF 交易时段/工作日非交易时段/周末时，波动风险应上调还是下调。
* **外部新闻检索（强制）**: 必须使用 Google Search Grounding 检索近48小时 BTC 相关外部新闻（至少2条，建议2-3条），并在 `新闻驱动因子` 字段输出标题、方向偏置、影响说明和来源URL。`新闻驱动因子` 不得使用输入数据中的内部指标直接充当新闻（如恐惧贪婪、资金费率、ETF净流入统计值本身）。
* **未来路径评估**: 必须参考 `scenario_probabilities`（已内置时间压缩与波动率调节）给出三条路径概率，不要自行硬编码概率：
    * 路径A：延续下行
    * 路径B：震荡后回补
    * 路径C：快速反弹
  - **禁止无信息均分**：禁止输出“30%/30%/30%”或近似均分概率。若出现“最高概率<45%”或“最高与最低差值<15%”的模糊状态，必须使用检索工具补充近48小时外部新闻（宏观、监管、ETF资金、地缘事件），用新闻因子重新校准后再给出概率分布，并明确更可能的主方向（最高概率需明显高于次高概率）。
* **定价偏差检查**：计算当前合约价格隐含的概率与 K 线技术面判断的概率是否存在显著偏差？
* **EV 优先级**：必须参考 `top_edge_opportunities`，优先推荐 edge 为正的标的。

### 模型校准偏差（Model Calibration Bias — 已自动校正）
`top_edge_opportunities` 中包含两套概率/edge：
* **原始值** (`model_prob_yes`, `edge_yes_raw`, `edge_no_raw`)：GBM barrier touch 模型的直接输出。
* **校准值** (`prob_yes_calibrated`, `edge_yes_calibrated`, `edge_no_calibrated`, `best_side_edge`)：已根据下表自动施加分段线性校正。

校准基于 85 个已结算月度市场的回测偏差：

| 行权距离 | 样本数 | 模型偏差 | 校正效果 |
|---------|-------|---------|---------|
| 0-3% (近距离) | 6 | **高估 +9pp** | 已自动下调（小样本收缩后约 -5pp） |
| 3-8% (中近距离) | 7 | **高估 +23pp** | 已自动下调（收缩后约 -14pp），但 `calibration_confidence=low`，**仍需额外谨慎** |
| 8-15% (中距离) | 13 | 低估 -6pp | 已自动上调约 +5pp，`calibration_confidence=medium` |
| 15-30% (中远距离) | 19 | **几乎完美** | ✅ 无显著校正，`calibration_confidence=high`，最可信赖 |
| 30%+ (远距离) | 40 | 高估 +5pp | 已自动下调，`calibration_confidence=medium` |

**使用指引**：
1. 优先使用 `best_side_edge`（校准后）和 `prob_yes_calibrated` 做决策，而非原始值。
2. `calibration_confidence=low` 的标的，即使校准后 edge 仍为正，也应降低仓位或跳过。
3. `distance_pct` 字段可直接获知行权距离，无需手动计算。

### 同方向相关性警告（Correlation Risk）
`correlation_group` 字段标识同方向市场组（如 `btc_above`、`btc_below`）。
**同方向标的高度相关**：如果 BTC 涨到 $110k，则 $100k/$105k/$108k above 全部获胜。
* 同一 `correlation_group` 内的标的不要合计超过单市场上限的 2 倍（即总仓位 ≤ 40% risk_budget）。
* 跨方向（above + below）组合可以分散风险。

**关键风险：波动率政体变化 (Vol Regime Shift)**
* 模型使用过去 30 天已实现波动率（或 Deribit IV），但未来波动率可能剧烈变化。
* 历史案例：2026年2月 BTC 从 $78k 暴跌至 $60k，实际 σ 从 2.3%/天翻倍至 4.4%/天，模型未能预见。
* **校正规则**：当你在 Step 1 中判断市场正在或即将进入新的波动率政体时（例如重大监管事件、宏观冲击），应对模型概率施加额外折扣/溢价——上行波动率增大时上方 Yes 概率上调、下方 dip 概率也上调；波动率收缩时则相反。

## Step 2: 持仓安全度分类与利润最大化 (Position Diagnosis)
* **首先按安全度分类**：参考 `position_safety_assessment` 将每个持仓标记为 safe_to_hold / monitor / at_risk。
* **safe_to_hold 仓位**（安全垫远超预期波动）：
    - 默认建议：**持有到期**，收割全部 Theta。
    - 关注点：如何在到期前通过挂单榨取最后几分钱的时间价值。
    - 禁止对此类仓位建议大规模减仓，除非出现 `at_risk` 级别的市场异变。
* **monitor 仓位**：
    - 给出持有条件和阶梯止盈计划。
* **at_risk 仓位**：
    - 可以使用紧急语气，给出明确的止损/减仓建议。
* **ATR临界提醒（必须强调）**：若 `position_safety_assessment` 中出现 `within_one_atr_warning=true` 或 `atr_distance < 1`，
  必须在“当前持仓与挂单分析与建议”中单独标注为高优先级风险点，并给出明确的触发价位与应急动作。
* **Theta 成本量化**：任何减仓建议必须引用 `theta_income` 中的数据，说明"卖出 X 张将放弃每天 $Y 的 Theta 收益"。**但当 `atr_distance < 1` 时，Theta 收益是以仓位存活为前提的条件收益，此时禁止将 Theta 作为继续持有的理由**——应优先执行止损，保护本金。
* **组合级风险评估**：参考 `portfolio_analysis`：
    - 识别组合结构（Short Strangle = 天然对冲），避免对单一仓位孤立恐慌。
    - 引用情景矩阵：BTC ±5% 时组合净值变化多少？如果组合层面风险可控，不要因为单仓"占比高"就恐慌。
* **自适应离场规则**：离场阈值必须参考 `daily_volatility_profile`，禁止使用固定阈值。
* **仓位约束**：遵守 `risk_budget`（注意：现在基于**总净值**而非仅 USDC 余额）。

## Step 2.5: 波段交易策略 (Swing Trading)
* **核心思路**：不以持有到期为目标，而是利用 BTC 短期价格波动（1-3天）赚取 token 价差收益。参考 `swing_opportunities` 中的 Delta 杠杆和方向性提示。
* **必须覆盖的范围**：
  - **已持仓标的**（`is_held=true`）：分析短期波动下已持仓 token 的价差机会——是否应趁 BTC 短暂有利波动部分止盈？是否可以在 BTC 回调时加仓摊低成本？已持仓标的必须至少给一条波段建议。
  - **未持仓的高杠杆标的**（`swing_score ≥ 1.0`）：从 `swing_opportunities` 中选出 swing_score 最高的 3-5 个，结合 Step 1 方向判断给出波段建议。
* **三种波段策略类型**：
  1. **方向性波段**：结合 Step 1 的短期趋势判断，若预判 BTC 未来 1-2 天上涨/下跌，买入对应方向的高杠杆标的（参考 `btc_up_action`/`btc_down_action`）。优先选择杠杆≥10x、swing_score≥1.5 的标的。
  2. **恐慌错配**：当 BTC 急跌时，下方 dip 标的的 Yes 价格可能被恐慌推高（超过模型公允价），此时买入其 No 等待价格回归。判断依据：token 当前价 vs `model_yes_prob`，偏差≥20% 时有错配机会。
  3. **安全垫收割**：远离行权价的 No 在 BTC 短暂波动时价格下跌，此时低价买入等反弹卖出。适合 `current_no_price` 在 0.85-0.96 之间、杠杆 3-8x 的标的。
* **仓位控制**（强制规则）：
  - 单笔波段仓位 ≤ 总净值的 5%（波段交易是高换手策略，单笔控小）
  - 所有波段仓位合计 ≤ 总净值的 15%
  - 与持有到期仓位分开管理，波段亏损不能用持有到期的安全垫来抵
* **止盈止损**（强制规则）：
  - 方向性波段：止盈 = 入场价 × (1 + 杠杆×1%的BTC目标涨幅)，止损 = token 价格跌 30% 或 BTC 反向 2%
  - 恐慌错配：止盈 = token 回归模型公允价，止损 = 持有不超过 2 天（错配未修复则认错）
  - 安全垫收割：止盈 = 买入价 +3¢~+5¢，止损 = 买入价 -3¢
  - 任何波段仓位最长持有不超过 3 天
* **月份阶段适配**：
  - 月初（>20天）：波段机会多，可积极参与方向性波段
  - 月中（10-20天）：以恐慌错配和安全垫收割为主，方向性波段仅在强信号时参与
  - 月末（<10天）：Theta 衰减快，波段机会缩减，仅参与明确的错配修复
* **输出数量要求**：必须输出 **3-8 条**波段交易建议。不要保守只给 1 条——`swing_opportunities` 已提供充足的数据支撑。对于每个策略类型至少尝试覆盖一条（若有合适标的）。

## Step 3: 轮动与建仓 (Rotation & New Positions)
* **轮动优先**：若 `rotation_opportunities` 非空，优先将轮动作为完整策略呈现：
    - "卖出 A（收益率 X%）→ 转投 B（收益率 Y%），收益率提升 Z 个百分点"
    - 轮动建议必须同时出现在 Part 2（卖出侧）和 Part 3（买入侧），形成闭环。
* **独立建仓**：对于没有轮动对应的高 edge 标的，基于**总净值**给出合理的建仓金额（非仅 USDC 余额）。
* **禁止无意义金额**：建仓金额应占总净值的合理比例（参考 `suggested_max_alloc_usdc`），不要给出总净值 0.2% 以下的"象征性"建议。

## Step 4: 报告输出结构

**Part 1 - 整体分析**：一段话概括市场环境、对持仓的影响、月内波动预期。若有上期回顾，先简述上期判断准确度。短期价格预测细节放在 Part 1.5 中。

**Part 1.5 - BTC短期预测**：结构化输出未来24-48h与到月底的BTC走势预测。必须包含：
- 方向判断（看涨/看跌/震荡）和置信度（高/中/低）
- 当前价格、24h目标区间、月底方向判断、月底目标区间
- 关键支撑位和阻力位
- 2-3条路径概率（必须参考 `scenario_probabilities`，概率之和应约为100%）
- 新闻驱动因子（2-3条，必须来自 Google Search Grounding 检索结果；写明事件、方向偏置、影响说明、来源URL；当路径概率接近时必须提供并据此打破均分）
- 核心逻辑（引用具体K线形态、资金流向、情绪指标等数据支撑）
- 风险提示（什么事件会推翻判断）

**Part 2 - 当前持仓与挂单分析与建议**：仅针对**已参与的 event**（有持仓或挂单的）。按事件/合约逐条：
- 先写仓位简述（盈亏、安全度分级、Theta 日收益）
- `持有条件` 与 `分层离场计划`
- 具体挂单建议（方向、价格¢、数量或比例、理由）
- `离场风控`（市场状态、ATR百分比、波动分位、止盈阈值、止损阈值）
- 若存在轮动机会，在此处说明"建议部分仓位轮动至 XX，详见建仓建议"

**Part 3 - 建仓建议**：针对**未参与的 event/问题**。结合**总净值与可用 USDC**、市场判断与各 outcome 现价：
- 若为轮动目标，标注"资金来源：轮动自 XX 仓位"
- 给出方向(Yes/No)、建议价格区间、建议投入金额或比例、预估优势、建议仓位上限
- 投入金额基于总净值合理配置

**Part 4 - 预警信号**：BTC 价格向上/向下触及某价位时的操作建议。

**Part 5 - 波段交易建议**：基于 `swing_opportunities` 和 Step 2.5 分析输出 **3-8 条**建议。必须覆盖：(1) 已持仓标的的波段操作机会 (2) 未持仓的高杠杆标的。每个建议必须包含标的、方向、策略类型、触发条件、建议价格、止盈目标、止损规则、理由。不要给模糊的"可以关注"，要给可执行的条件单。

**Part 6 - 报告解读附录**：输出 3-8 条"可执行一句话"，每条包含：`标的`、`执行优先级`、`一句话结论`、`执行要点`。
`执行优先级` 只能使用：`立即执行`、`挂单等待`、`仅观察`。
优先覆盖：轮动建议、edge最高的建仓建议、波段交易机会、需关注的持仓。
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

【操作员指示】（优先于默认策略，请在整体分析中明确响应）:
{operator_intent}

Polymarket 持仓情况和挂单情况: {polymarket_status}

Polymarket 事件与各市场当前价格: {polymarket_event_situation}

当前可用 USDC 余额: {usdc_balance}

比特币过去7天4h K线数据: {btc_4h_k_data}

比特币过去30天1d K线数据: {btc_1d_k_data}

市场情绪与资金面: {market_sentiment_and_funding}

日线波动率画像(用于自适应离场): {daily_volatility_profile}

时段波动提示(经验规则): {intraday_volatility_hint}

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


def _get_monthly_phase_context(day: int) -> str:
    """根据日期返回月份阶段和对应风险偏好描述。"""
    if day <= 7:
        return (
            f"当前为**月初（第{day}天）**，风险偏好：**进攻型**。"
            "应主动寻找高赔率机会，允许建立进攻性 Yes 仓位，弹药分批动用，"
            "单次建仓不超过可用资金 40%。有充足时间纠错，策略应积极。"
        )
    elif day <= 22:
        return (
            f"当前为**月中（第{day}天）**，风险偏好：**平衡型**。"
            "兼顾收益与防守，对已有进攻性 Yes 仓位执行阶梯止盈，"
            "减少新建高风险 Yes 仓位，优先强化确定性 Theta 收益。"
        )
    else:
        return (
            f"当前为**月末（第{day}天）**，风险偏好：**防守型**。"
            "优先锁定已有胜局，禁止新建进攻性 Yes 仓位，"
            "持有高胜率 No 仓位到期，不要为追求额外收益承担不必要风险。"
        )


def get_system_instruction(
    current_date: str,
    monthly_target: str = "月度净值翻倍（+100%）",
) -> str:
    """根据当前日期生成 system instruction，自动注入月份阶段与目标。"""
    day = int(current_date.split("-")[2])
    monthly_phase_context = _get_monthly_phase_context(day)
    return SYSTEM_INSTRUCTION_TEMPLATE.format(
        current_date=current_date,
        monthly_phase_context=monthly_phase_context,
        monthly_target=monthly_target,
    )


def get_user_prompt(
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
    previous_report: dict | None = None,
    operator_intent: str | None = None,
) -> str:
    """根据输入数据生成 user prompt。
    operator_intent: 本次分析的操作员意图，如持仓偏好、当前判断等，优先于默认策略。
    """
    event_situation_str = json.dumps(
        polymarket_event_situation, ensure_ascii=False, indent=2
    )
    if previous_report:
        previous_report_str = json.dumps(
            previous_report, ensure_ascii=False, indent=2
        )
    else:
        previous_report_str = "（无）"
    operator_intent_str = operator_intent if operator_intent else "（无特别指示，按默认策略执行）"
    return USER_PROMPT_TEMPLATE.format(
        operator_intent=operator_intent_str,
        polymarket_status=polymarket_status,
        polymarket_event_situation=event_situation_str,
        usdc_balance=usdc_balance,
        btc_4h_k_data=btc_4h_k_data,
        btc_1d_k_data=btc_1d_k_data,
        daily_volatility_profile=daily_volatility_profile,
        intraday_volatility_hint=intraday_volatility_hint,
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

# ---------- 黄金 (Gold) Polymarket 分析 ----------

GOLD_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "整体分析": {
            "type": "string",
            "description": "一段话概括黄金市场环境（美元/利率/地缘/央行购金等）。若有上期回顾先简述准确度。简述对持仓影响、未来24h与月内金价波动预期。"
        },
        "当前持仓与挂单分析与建议": {
            "type": "array",
            "description": "针对已参与的黄金 event（有持仓或挂单的），按 event/合约逐条分析",
            "items": {
                "type": "object",
                "properties": {
                    "事件或合约": {"type": "string"},
                    "仓位简述": {
                        "type": "string",
                        "description": "盈亏、安全度分级 safe_to_hold/monitor/at_risk、Theta 日收益"
                    },
                    "持有条件": {"type": "string"},
                    "分层离场计划": {"type": "string"},
                    "挂单建议": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "操作类型": {"type": "string", "enum": ["挂买单", "挂卖单"]},
                                "方向": {"type": "string", "enum": ["Yes", "No"]},
                                "建议价格": {"type": "number", "description": "美分 1-99"},
                                "建议数量或比例": {"type": "string"},
                                "触发条件": {"type": "string"},
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
                    }
                },
                "required": ["事件或合约", "仓位简述", "挂单建议"]
            }
        },
        "建仓建议": {
            "type": "array",
            "description": "针对黄金事件中用户尚未参与的 market/问题，结合总净值、可用 USDC 给出建仓建议",
            "items": {
                "type": "object",
                "properties": {
                    "事件或问题": {"type": "string"},
                    "建议方向": {"type": "string", "enum": ["Yes", "No"]},
                    "建议价格区间": {"type": "string"},
                    "建议投入金额或比例": {"type": "string", "description": "基于总净值合理配置"},
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
                    "价格": {"type": "number", "description": "黄金价格，美元/盎司"},
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
    "required": ["整体分析", "当前持仓与挂单分析与建议", "建仓建议", "预警信号", "报告解读附录"]
}

GOLD_SYSTEM_INSTRUCTION_TEMPLATE = """# Role
你是资深贵金属与预测市场（Prediction Market）分析师，同时也是**利润最大化顾问**。你精通黄金期货定价、CME 结算机制、央行购金趋势，并深入理解 Polymarket 黄金类二元期权的结算规则与流动性特性。

# Goal
根据我提供的【Polymarket 黄金持仓】、【挂单】、【黄金 K 线及价格数据】，**最大化账户总收益**，同时控制尾部风险。
**核心任务**：基于当前金价与到期日的距离，识别利润最大化路径——包括哪些仓位应坚定持有到期、哪些应轮动到更高收益标的、以及如何最高效地配置可用资金。

# Core Principles (利润最大化优先)
1. **持有到期是默认策略**：对于 `position_safety_assessment` 中标记为 `safe_to_hold` 的仓位，默认建议"持有到期收割全部 Theta"，除非有极端风险信号。
2. **减仓必须量化机会成本**：任何减仓建议必须附带"放弃的 Theta 日收益"（参考 `theta_income`）。**但当 `atr_distance < 1` 时，Theta 收益是以仓位存活为前提的条件收益，此时禁止将 Theta 作为继续持有的理由**——应优先执行止损，保护本金。
3. **所有仓位必须明确止损**：任何仓位建议（含 Yes 和 No）中，必须明确认错止损价。若上期已设定止损价且当前价格已触及或越过，本期**必须建议立即市价止损**，禁止改为"等待回调后挂单卖出"。
4. **建仓/轮动以收益率为锚**：优先推荐"持有到期预期收益率"更高且安全垫更厚的标的。参考 `rotation_opportunities`。
5. **组合视角优先**：参考 `portfolio_analysis` 从组合层面评估风险，不要对单一仓位孤立恐慌。
6. **禁止复读**：若 `prediction_review` 显示上期建议未被执行，必须分析原因并给出调整后的新方案。
7. **语气校准**：对 `safe_to_hold` 仓位禁止使用恐吓措辞。只有 `at_risk` 仓位才适用紧急语气。

# 黄金 Polymarket 结算规则
1. **结算条件**：若在到期月的**最后一个交易日之前**的**任意一个交易日**，CME 公布的 **Active Month 黄金 GC 期货的官方结算价 (Settlement)** 达到或超过该市场所标价格，则结算为 **Yes**；否则为 **No**。即：只要月内有一天结算价 ≥ 目标价即 Yes。
2. **结算价 ≠ 盘中高点**：CME Settlement 价格通常在 CME 收盘后公布，盘中最高价不算。
3. **"hit" = ≥ Settlement**：到达即算，不要求收盘价、不要求连续多天。
4. **裁决来源**：以 CME Group 官网该交易日**首次发布**的 Active Month 黄金 GC 期货「Settlement」价格为准。

# Context & Constraints
* **当前时间**：{current_date}
* **K线数据格式**: `[open_time_ms, Open, High, Low, Close, Volume, ...]`，重点关注 Close 和 Volume。

# Input Data
1. **持仓与挂单**（仅黄金相关 event）
2. **黄金市场背景**：`gold_price_usd_per_oz` 来自 Yahoo Finance (GC=F)
3. **Polymarket 黄金事件与各市场当前价格**
4. **USDC 余额**
5. **黄金 4h K线（近7天）+ 1d K线（近30天）**
6. **日线波动率画像**
7. **未来可能性上下文**
8. **收益优化上下文**（含 portfolio_summary, risk_budget, position_safety_assessment, theta_income, portfolio_analysis, rotation_opportunities, prediction_review, scenario_probabilities, top_edge_opportunities）
9. **上一时间段报告**（若有）

# Analysis Framework (COT)

## Step 0: 上期回顾与自校准
* 若有 `prediction_review`，先回顾上期判断准确度，自校准概率评估。

## Step 1: 市场环境与定价偏差
* 分析黄金 K 线趋势：上升/下降通道还是震荡？
* **驱动因素**：美元指数 (DXY)、实际利率、央行购金、地缘风险（中东、制裁）、ETF 资金流。
* **必须使用检索工具** 查询近期与黄金相关的实时新闻（如：美联储利率决议、央行购金数据、中东局势、关税政策等），并纳入分析。
* **波动率测算**：基于 K 线高低点估算未来波动范围。参考 `scenario_probabilities.time_compression_note`。
* **定价偏差**：计算当前合约隐含概率与技术面判断概率的偏差。
* **EV 优先级**：参考 `top_edge_opportunities`，推荐 edge 为正的标的。
* **模型校准提醒**：`model_prob_yes` 基于 GBM barrier touch 模型 + 肥尾修正。该模型在 BTC 月度市场中的回测显示近距离(0-8%)有高估倾向、中远距离(15-30%)校准最佳。黄金市场的校准数据不足，对模型概率的采信度应更保守，优先依赖你自身的基本面和技术面判断。

## Step 2: 持仓安全度分类与利润最大化
* 参考 `position_safety_assessment` 按安全度分类。
* safe_to_hold → 持有到期；monitor → 给出条件；at_risk → 紧急止损。
* 减仓必须引用 theta_income 数据。
* 参考 portfolio_analysis 情景矩阵评估组合风险。

## Step 3: 轮动与建仓
* 轮动优先：参考 rotation_opportunities。
* 建仓金额基于总净值（非仅 USDC）。

## Step 4: 报告输出
**Part 1 - 整体分析**：含「地缘与新闻」段落。
**Part 2 - 当前持仓与挂单分析与建议**
**Part 3 - 建仓建议**
**Part 4 - 预警信号**（金价触及某价位时的操作建议）
**Part 5 - 报告解读附录**：3-8 条可执行一句话。
"""

GOLD_USER_PROMPT_TEMPLATE = """
以下是当前要分析的具体信息（Polymarket 黄金类 event）：

Polymarket 黄金持仓情况和挂单情况: {polymarket_status}

Polymarket 黄金事件与各市场当前价格: {polymarket_event_situation}

当前可用 USDC 余额: {usdc_balance}

黄金市场背景: {gold_market_context}

黄金过去7天 4h K线数据: {gold_4h_k_data}

黄金过去30天 1d K线数据: {gold_1d_k_data}

日线波动率画像(用于自适应离场): {daily_volatility_profile}

未来可能性上下文(用于评估是否过早离场): {future_possibility_context}

收益优化上下文(用于最大化期望收益并控制回撤): {profit_optimization_context}

上一时间段报告（仅供参考，可延续或调整其判断与建议）:
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
    event_situation_str = json.dumps(
        polymarket_event_situation, ensure_ascii=False, indent=2
    )
    if previous_report:
        previous_report_str = json.dumps(
            previous_report, ensure_ascii=False, indent=2
        )
    else:
        previous_report_str = "无上期报告"

    return GOLD_USER_PROMPT_TEMPLATE.format(
        polymarket_status=polymarket_status,
        polymarket_event_situation=event_situation_str,
        usdc_balance=usdc_balance,
        gold_market_context=json.dumps(gold_market_context, ensure_ascii=False, indent=2),
        gold_4h_k_data=gold_4h_k_data,
        gold_1d_k_data=gold_1d_k_data,
        daily_volatility_profile=json.dumps(daily_volatility_profile, ensure_ascii=False, indent=2),
        future_possibility_context=json.dumps(future_possibility_context, ensure_ascii=False, indent=2),
        profit_optimization_context=json.dumps(profit_optimization_context, ensure_ascii=False, indent=2),
        previous_report=previous_report_str,
    )
