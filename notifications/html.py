import json

# --- 1. 生成内部 HTML 片段的辅助函数 ---


def generate_position_analysis_section(data: dict) -> str:
    """最前章节：仓位分析。突出原有单是否需要补仓，以及挂单是否需调整（不仅是挂单调整）。"""
    positions = data.get("当前持仓与挂单分析与建议", []) or []
    if not positions:
        return ""

    cards_html = ""
    for block in positions:
        event_name = (block.get("事件或合约", "") or "").strip()
        summary = (block.get("仓位简述", "") or "").strip()
        # 补仓结论：优先用报告给出的「补仓建议」或「是否需要补仓」等字段
        add_pos_raw = (
            (block.get("补仓建议", "")
             or block.get("是否需要补仓", "")
             or block.get("补仓结论", "")
             or "")
            .strip()
        )
        if add_pos_raw:
            add_pos_tip = add_pos_raw
        else:
            add_pos_tip = "【须报告给出】本合约是否需要补仓暂无结论，请在报告数据中为该条持仓补充「补仓建议」字段。"
        # 挂单结论
        order_raw = (
            (block.get("挂单结论", "")
             or block.get("挂单需调整", "")
             or block.get("挂单调整建议", "")
             or block.get("是否需要调整挂单", "")
             or "")
            .strip()
        )
        if order_raw:
            order_tip = order_raw
        else:
            order_tip = "【须报告给出结论】本合约挂单是否需调整暂无具体结论，请补充「挂单结论」字段。"

        lines = []
        if event_name:
            lines.append(f'<div class="position-analysis-contract-name">{event_name}</div>')
        if summary:
            lines.append(f'<div class="position-analysis-detail">仓位简述：{summary}</div>')
        lines.append(f'<div class="position-analysis-add">📦 是否需要补仓：{add_pos_tip}</div>')
        lines.append(f'<div class="position-analysis-order">📌 挂单是否需调整：{order_tip}</div>')
        cards_html += f'<div class="position-analysis-card">{("".join(lines))}</div>'

    return f"""
    <section class="position-analysis-section">
        <h2 class="position-analysis-title">仓位分析（原有单：补仓与挂单）</h2>
        <div class="position-analysis-body">
            {cards_html}
        </div>
    </section>
    """


def generate_action_priority_section(data: dict) -> str:
    """最前章节：当前行动要求优先级。当前持有单与建仓调整用大字体与着重色突出。"""
    positions = data.get("当前持仓与挂单分析与建议", []) or []
    entries = data.get("建仓建议", []) or []
    if not positions and not entries:
        return ""

    items_html = ""
    # 当前持有单：每个合约名单独一行，并提示对照下方内容看挂单是否需调整
    if positions:
        pos_cards = []
        for block in positions:
            event_name = (block.get("事件或合约", "") or "").strip()
            summary = (block.get("仓位简述", "") or "").strip()
            orders = block.get("挂单建议", []) or []
            hold = (block.get("持有条件", "") or "").strip()
            # 必须展示具体结论：优先用报告给出的「挂单结论」或「挂单需调整」等字段；若无则明确提示须由报告补充
            raw_conclusion = (
                (block.get("挂单结论", "")
                 or block.get("挂单需调整", "")
                 or block.get("挂单调整建议", "")
                 or block.get("是否需要调整挂单", "")
                 or "")
                .strip()
            )
            if raw_conclusion:
                adjust_tip = raw_conclusion
            else:
                adjust_tip = "【须报告给出结论】本合约挂单是否需调整暂无具体结论，请在报告数据中为该条持仓补充「挂单结论」或「挂单需调整」字段。"
            lines = []
            if event_name:
                lines.append(f'<div class="action-priority-contract-name">{event_name}</div>')
            if summary:
                lines.append(f'<div class="action-priority-detail">仓位简述：{summary}</div>')
            if hold:
                lines.append(f'<div class="action-priority-detail">持有条件：{hold}</div>')
            lines.append(f'<div class="action-priority-adjust">📌 {adjust_tip}</div>')
            pos_cards.append("".join(lines))
        if pos_cards:
            items_html += """
            <div class="action-priority-block action-priority-hold">
                <div class="action-priority-label">当前持有单</div>
                <div class="action-priority-hold-list">
            """
            for card in pos_cards:
                items_html += f'<div class="action-priority-hold-item">{card}</div>'
            items_html += """
                </div>
            </div>
            """

    # 建仓/调整要求：大字体、着重色
    if entries:
        entry_lines = []
        for item in entries:
            event_or_q = (item.get("事件或问题", "") or "").strip()
            direction = (item.get("建议方向", "") or "").strip()
            price_range = (item.get("建议价格区间", "") or "").strip()
            amount = (item.get("建议投入金额或比例", "") or "").strip()
            line = event_or_q
            if direction:
                line += f" — {direction}"
            if price_range:
                line += f"，区间 {price_range}"
            if amount:
                line += f"，投入 {amount}"
            if line:
                entry_lines.append(line)
        if entry_lines:
            items_html += """
            <div class="action-priority-block action-priority-entry">
                <div class="action-priority-label">建仓与调整要求</div>
                <ul class="action-priority-list">
            """
            for line in entry_lines:
                items_html += f"<li>{line}</li>"
            items_html += """
                </ul>
            </div>
            """

    if not items_html:
        return ""
    return f"""
    <section class="action-priority-section">
        <h2 class="action-priority-title">当前行动要求优先级</h2>
        <div class="action-priority-body">
            {items_html}
        </div>
    </section>
    """


def generate_overview_section(text: str) -> str:
    """Part 1: 整体分析"""
    if not text:
        return ""
    return f"""
        <h2 style="color: #3b82f6; margin-top: 20px;">📈 一、整体分析</h2>
        <div class="market-snapshot">
            <div class="market-snapshot-content">{text}</div>
        </div>
    """


def generate_position_and_orders_section(items: list) -> str:
    """Part 2: 当前持仓、当前挂单分析与建议（已参与的 event）"""
    if not items:
        return '<div class="card"><div class="card-body muted">暂无已参与仓位</div></div>'
    html = ""
    for block in items:
        event_name = block.get("事件或合约", "")
        summary = block.get("仓位简述", "")
        hold_condition = block.get("持有条件", "")
        layered_exit_plan = block.get("分层离场计划", "")
        orders = block.get("挂单建议", [])
        exit_risk = block.get("离场风控", {})
        order_rows = ""
        for o in orders:
            op_type = o.get("操作类型", "")
            icon = "🟢" if op_type == "挂买单" else "🔴"
            price = o.get("建议价格")
            price_str = f" <strong>{price}¢</strong>" if price is not None else ""
            direction = o.get("方向", "")
            size = o.get("建议数量或比例", "")
            reason = o.get("理由", "")
            order_rows += f"""
            <div class="ladder-step">
                <div class="step-price">{icon} {op_type} {direction}{price_str} · {size or '-'}</div>
                <div class="step-logic">触发条件：{o.get('触发条件', '-')}</div>
                <div class="step-logic">{reason}</div>
            </div>
            """

        exit_risk_html = ""
        if exit_risk:
            exit_risk_html = f"""
                <div class="action-box" style="margin-top: 10px; border-left: 3px solid #f59e0b;">
                    🛡️ 离场风控：状态={exit_risk.get('市场状态', '-')}, ATR={exit_risk.get('ATR百分比', '-') }%,
                    波动分位={exit_risk.get('波动分位', '-')},
                    止盈={exit_risk.get('止盈阈值', '-')}, 止损={exit_risk.get('止损阈值', '-')}
                </div>
            """

        hold_plan_html = ""
        if hold_condition or layered_exit_plan:
            hold_plan_html = f"""
                <div class="action-box" style="margin-top: 10px; border-left: 3px solid #3b82f6;">
                    ⏳ 持有条件：{hold_condition or '-'}<br>
                    📚 分层离场计划：{layered_exit_plan or '-'}
                </div>
            """
        html += f"""
        <div class="card action-card">
            <div class="card-header" style="border-left: 4px solid #10b981;">
                <div class="contract-title">{event_name}</div>
            </div>
            <div class="card-body">
                <div class="action-box" style="margin-bottom: 10px;">📋 仓位简述：{summary}</div>
                <div class="muted" style="margin-bottom: 6px;">挂单建议：</div>
                <div class="ladder-container">{order_rows}</div>
                {exit_risk_html}
                {hold_plan_html}
            </div>
        </div>
        """
    return html


def generate_new_position_section(items: list) -> str:
    """Part 3: 建仓建议（未参与的 event）"""
    if not items:
        return '<div class="card"><div class="card-body muted">暂无建仓建议</div></div>'
    html = ""
    for item in items:
        event_or_question = item.get("事件或问题", "")
        direction = item.get("建议方向", "")
        price_range = item.get("建议价格区间", "")
        amount = item.get("建议投入金额或比例", "")
        edge_hint = item.get("预估优势", "")
        cap_hint = item.get("建议仓位上限", "")
        reason = item.get("理由", "")

        extra_lines = ""
        if edge_hint:
            extra_lines += f"<div class='logic-text'>预估优势：{edge_hint}</div>"
        if cap_hint:
            extra_lines += f"<div class='logic-text'>建议仓位上限：{cap_hint}</div>"

        html += f"""
        <div class="card action-card">
            <div class="card-header" style="border-left: 4px solid #f59e0b;">
                <div class="contract-title">{event_or_question}</div>
                <div class="status-badge" style="background: #f59e0b20; color: #f59e0b;">{direction}</div>
            </div>
            <div class="card-body">
                <div class="action-box">建议价格区间：<strong>{price_range}</strong> · 建议投入：{amount}</div>
                {extra_lines}
                <div class="logic-text">理由：{reason}</div>
            </div>
        </div>
        """
    return html

def generate_alert_rows(items):
    rows = ""
    for item in items:
        # 判断方向颜色
        is_up = item['预警方向'] == 'up_to'
        color = "#10b981" if is_up else "#ef4444"
        icon = "📈" if is_up else "📉"
        direction_text = "向上突破" if is_up else "向下触及"
        
        rows += f"""
        <div class="alert-item" style="border-color: {color};">
            <div class="alert-price" style="color: {color};">
                {icon} BTC {direction_text} <strong>${item['价格']}</strong>
            </div>
            <div class="alert-action">
                {item['操作建议']}
            </div>
            <div class="alert-action muted" style="margin-top: 4px;">
                {item.get('关联止盈止损', '')}
            </div>
        </div>
        """
    return rows

def generate_market_snapshot(snapshot_text):
    """生成市场快照部分的 HTML（兼容旧 key 市场与持仓快照）"""
    if not snapshot_text:
        return ""
    return generate_overview_section(snapshot_text)


def generate_interpretation_appendix(items: list) -> str:
    """Part 5: 报告解读附录（快速执行版）"""
    if not items:
        return ""

    rows = ""
    for item in items:
        target = item.get("标的", "")
        priority = item.get("执行优先级", "")
        summary = item.get("一句话结论", "")
        action = item.get("执行要点", "")

        priority_bg = "#334155"
        priority_color = "#e2e8f0"
        if priority == "立即执行":
            priority_bg = "#ef444420"
            priority_color = "#f87171"
        elif priority == "挂单等待":
            priority_bg = "#f59e0b20"
            priority_color = "#fbbf24"
        elif priority == "仅观察":
            priority_bg = "#3b82f620"
            priority_color = "#60a5fa"

        rows += f"""
        <div class="card action-card">
            <div class="card-header" style="border-left: 4px solid #3b82f6;">
                <div class="contract-title">{target}</div>
                <div class="status-badge" style="background: {priority_bg}; color: {priority_color};">{priority or '未分类'}</div>
            </div>
            <div class="card-body">
                <div class="action-box">{summary}</div>
                <div class="logic-text">执行要点：{action}</div>
            </div>
        </div>
        """
    return rows


def generate_monthly_strategy_html(data: dict) -> str:
    """生成月初建仓建议的 HTML 邮件内容"""
    strategy = data.get("策略方案", {})
    ladder = strategy.get("分批建仓", [])
    risk_tips = data.get("风险提示", [])
    risk_controls = strategy.get("风险控制", [])
    indicators = strategy.get("关键观察指标", [])
    summary = data.get("参考数据摘要", {})

    ladder_rows = ""
    for step in ladder:
        ladder_rows += f"""
        <div class="ladder-step">
            <div class="step-price">触发条件: <strong>{step.get('触发条件','')}</strong></div>
            <div class="step-logic">建议价格区间: {step.get('建议价格区间','')} | 仓位比例: {step.get('仓位比例','')}</div>
            <div class="step-logic">逻辑: {step.get('逻辑','')}</div>
        </div>
        """

    risk_items = "".join([f"<li>{item}</li>" for item in risk_tips])
    control_items = "".join([f"<li>{item}</li>" for item in risk_controls])
    indicator_items = "".join([f"<li>{item}</li>" for item in indicators])

    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        :root {{
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-main: #e2e8f0;
            --text-muted: #94a3b8;
            --accent-green: #10b981;
            --accent-orange: #f59e0b;
            --accent-blue: #3b82f6;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            margin: 0;
            padding: 20px;
            line-height: 1.6;
        }}
        .container {{ max-width: 800px; margin: 0 auto; }}
        h1 {{ text-align: center; color: #fff; margin-bottom: 30px; font-size: 24px; }}
        h2 {{
            font-size: 18px;
            border-bottom: 2px solid #334155;
            padding-bottom: 10px;
            margin-top: 30px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .card {{
            background-color: var(--card-bg);
            border-radius: 8px;
            margin-bottom: 16px;
            padding: 16px;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .muted {{ color: var(--text-muted); font-size: 14px; }}
        .ladder-container {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 10px;
            margin-top: 10px;
        }}
        .ladder-step {{
            background: #0f172a;
            border: 1px solid #334155;
            padding: 10px;
            border-radius: 6px;
        }}
        ul {{ margin: 0; padding-left: 20px; }}
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 10px;
        }}
        .summary-item {{ background: #0f172a; border: 1px solid #334155; padding: 10px; border-radius: 6px; }}
        .summary-item strong {{ color: var(--accent-blue); }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📅 月初 BTC 建仓建议 (Polymarket)</h1>

        <div class="card">
            <h2 style="color: var(--accent-blue);">🧭 月份趋势判断</h2>
            <div>{data.get("月份趋势判断", "")}</div>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-blue);">📏 月内BTC变动区间</h2>
            <div><strong>下限：</strong>{data.get("月内BTC变动区间", {}).get("下限", "")}</div>
            <div><strong>上限：</strong>{data.get("月内BTC变动区间", {}).get("上限", "")}</div>
            <div class="muted" style="margin-top: 6px;">{data.get("月内BTC变动区间", {}).get("逻辑", "")}</div>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-green);">🎯 策略方案</h2>
            <div><strong>总体建议：</strong>{strategy.get("总体建议", "")}</div>
            <div><strong>建仓方向：</strong>{strategy.get("建仓方向", "")}</div>
            <div><strong>仓位建议：</strong>{strategy.get("仓位建议", "")}</div>
            <div class="muted" style="margin-top: 8px;">分批建仓：</div>
            <div class="ladder-container">
                {ladder_rows}
            </div>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-orange);">🛡️ 风险控制</h2>
            <ul>{control_items}</ul>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-blue);">👀 关键观察指标</h2>
            <ul>{indicator_items}</ul>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-blue);">📌 参考数据摘要</h2>
            <div class="summary-grid">
                <div class="summary-item"><strong>BTC现价</strong><br>{summary.get("btc现价", "")}</div>
                <div class="summary-item"><strong>4h趋势</strong><br>{summary.get("4h趋势", "")}</div>
                <div class="summary-item"><strong>24h RSI</strong><br>{summary.get("24h RSI", "")}</div>
                <div class="summary-item"><strong>资金费率</strong><br>{summary.get("资金费率", "")}</div>
                <div class="summary-item"><strong>OI</strong><br>{summary.get("OI", "")}</div>
                <div class="summary-item"><strong>ETF净流入</strong><br>{summary.get("ETF净流入", "")}</div>
                <div class="summary-item"><strong>稳定币流动性</strong><br>{summary.get("稳定币流动性", "")}</div>
                <div class="summary-item"><strong>恐惧贪婪</strong><br>{summary.get("恐惧贪婪", "")}</div>
                <div class="summary-item"><strong>多空比</strong><br>{summary.get("多空比", "")}</div>
            </div>
        </div>

        <div class="card">
            <h2 style="color: var(--accent-orange);">⚠️ 风险提示</h2>
            <ul>{risk_items}</ul>
        </div>

        <div style="text-align: center; margin-top: 30px; color: #475569; font-size: 12px;">
            Generated by AI Monthly Strategy Engine
        </div>
    </div>
</body>
</html>
"""

# --- 2. 主 HTML 模板 (使用 f-string) ---
def generate_html_template(data):
    return f"""
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        :root {{
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-main: #e2e8f0;
            --text-muted: #94a3b8;
            --accent-green: #10b981;
            --accent-orange: #f59e0b;
            --accent-red: #ef4444;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            margin: 0;
            padding: 20px;
            line-height: 1.5;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
        }}
        h1 {{ text-align: center; color: #fff; margin-bottom: 30px; font-size: 24px; }}
        h2 {{ 
            font-size: 18px; 
            border-bottom: 2px solid #334155; 
            padding-bottom: 10px; 
            margin-top: 40px; 
            display: flex; 
            align-items: center; 
            gap: 10px;
        }}
        
        /* 通用卡片样式 */
        .card {{
            background-color: var(--card-bg);
            border-radius: 8px;
            margin-bottom: 16px;
            overflow: hidden;
            box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1);
        }}
        .card-header {{
            padding: 12px 16px;
            background: rgba(255,255,255,0.03);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .contract-title {{ font-weight: bold; font-size: 15px; }}
        .status-badge {{
            font-size: 12px;
            padding: 2px 8px;
            border-radius: 12px;
            font-weight: 600;
        }}
        .card-body {{ padding: 16px; }}
        .action-box {{
            background: rgba(255,255,255,0.05);
            padding: 10px;
            border-radius: 4px;
            margin-bottom: 10px;
            font-size: 14px;
        }}
        .action-box.warning {{ background: rgba(245, 158, 11, 0.1); color: #fbbf24; }}
        .logic-text {{ color: var(--text-muted); font-size: 13px; font-style: italic; }}
        .muted {{ color: var(--text-muted); font-size: 14px; }}

        /* 阶梯挂单样式 */
        .ladder-container {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 10px;
            margin-top: 15px;
        }}
        .ladder-step {{
            background: #0f172a;
            border: 1px solid #334155;
            padding: 10px;
            border-radius: 6px;
        }}
        .step-price {{ font-size: 14px; margin-bottom: 4px; color: #fff; }}
        .step-logic {{ font-size: 12px; color: #64748b; line-height: 1.3; }}
        .direction-badge {{ font-size: 11px; margin-left: 4px; opacity: 0.9; }}

        /* 预警样式 */
        .alert-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            background: var(--card-bg);
            border-left: 4px solid #ccc;
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 4px;
        }}
        .alert-price {{ font-weight: bold; font-size: 16px; }}
        .alert-action {{ font-size: 14px; color: var(--text-muted); max-width: 60%; text-align: right; }}

        /* 市场快照样式 */
        .market-snapshot {{
            background: linear-gradient(135deg, rgba(16, 185, 129, 0.1) 0%, rgba(59, 130, 246, 0.1) 100%);
            border: 1px solid rgba(59, 130, 246, 0.3);
            border-radius: 8px;
            padding: 20px;
            margin-bottom: 30px;
            line-height: 1.8;
        }}
        .market-snapshot-content {{
            color: var(--text-main);
            font-size: 15px;
            white-space: pre-wrap;
            word-wrap: break-word;
        }}

        /* 仓位分析：原有单补仓与挂单，放在最前；深绿+红色 */
        .position-analysis-section {{
            margin-bottom: 28px;
            border: 2px solid #047857;
            border-radius: 10px;
            background: linear-gradient(135deg, rgba(4, 120, 87, 0.15) 0%, rgba(220, 38, 38, 0.06) 100%);
            padding: 18px 20px;
        }}
        .position-analysis-title {{
            font-size: 20px;
            font-weight: 700;
            color: #fff;
            margin: 0 0 14px 0;
            padding-bottom: 10px;
            border-bottom: 2px solid #047857;
        }}
        .position-analysis-body {{
            display: flex;
            flex-direction: column;
            gap: 14px;
        }}
        .position-analysis-card {{
            padding: 14px 16px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 8px;
            border-left: 4px solid #047857;
        }}
        .position-analysis-contract-name {{
            font-size: 16px;
            font-weight: 700;
            color: #059669;
            margin-bottom: 8px;
            display: block;
        }}
        .position-analysis-detail {{
            font-size: 14px;
            color: var(--text-main);
            margin-bottom: 8px;
        }}
        .position-analysis-add {{
            font-size: 14px;
            font-weight: 600;
            color: #047857;
            margin-bottom: 6px;
        }}
        .position-analysis-order {{
            font-size: 14px;
            color: #ef4444;
        }}

        /* 当前行动要求优先级：大字体与着重色 */
        .action-priority-section {{
            margin-bottom: 28px;
            border: 2px solid var(--accent-orange);
            border-radius: 10px;
            background: linear-gradient(135deg, rgba(245, 158, 11, 0.12) 0%, rgba(239, 68, 68, 0.06) 100%);
            padding: 18px 20px;
        }}
        .action-priority-title {{
            font-size: 20px;
            font-weight: 700;
            color: #fff;
            margin: 0 0 14px 0;
            padding-bottom: 10px;
            border-bottom: 2px solid rgba(245, 158, 11, 0.5);
        }}
        .action-priority-body {{
            display: flex;
            flex-direction: column;
            gap: 16px;
        }}
        .action-priority-block {{
            padding: 14px 16px;
            border-radius: 8px;
            font-size: 16px;
            font-weight: 600;
            line-height: 1.5;
        }}
        .action-priority-hold {{
            background: rgba(239, 68, 68, 0.15);
            border-left: 4px solid var(--accent-red);
        }}
        .action-priority-hold .action-priority-label {{
            color: #f87171;
            font-size: 17px;
        }}
        .action-priority-hold-list {{
            display: flex;
            flex-direction: column;
            gap: 14px;
        }}
        .action-priority-hold-item {{
            padding: 10px 12px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 6px;
            border-left: 3px solid rgba(239, 68, 68, 0.6);
        }}
        .action-priority-contract-name {{
            font-size: 16px;
            font-weight: 700;
            color: #fca5a5;
            margin-bottom: 6px;
            display: block;
        }}
        .action-priority-detail {{
            font-size: 14px;
            color: var(--text-main);
            margin-bottom: 4px;
        }}
        .action-priority-adjust {{
            font-size: 14px;
            color: #fbbf24;
            margin-top: 8px;
        }}
        .action-priority-entry {{
            background: rgba(245, 158, 11, 0.2);
            border-left: 4px solid var(--accent-orange);
        }}
        .action-priority-entry .action-priority-label {{
            color: #fbbf24;
            font-size: 17px;
        }}
        .action-priority-label {{
            font-weight: 700;
            margin-bottom: 8px;
        }}
        .action-priority-list {{
            margin: 0;
            padding-left: 22px;
        }}
        .action-priority-list li {{
            margin-bottom: 6px;
        }}

    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Polymarket 仓位分析报告</h1>

        {generate_position_analysis_section(data)}
        {generate_action_priority_section(data)}

        {generate_overview_section(data.get("整体分析", "") or data.get("市场与持仓快照", ""))}

        <h2 style="color: #10b981;">📋 二、当前持仓、当前挂单分析与建议</h2>
        {generate_position_and_orders_section(data.get('当前持仓与挂单分析与建议', []))}

        <h2 style="color: #f59e0b;">🆕 三、建仓建议</h2>
        {generate_new_position_section(data.get('建仓建议', []))}

        <h2 style="color: var(--accent-red);">🚨 四、预警价格及操作</h2>
        {generate_alert_rows(data.get('预警信号', []))}

        <h2 style="color: #3b82f6;">🧠 五、报告解读附录</h2>
        {generate_interpretation_appendix(data.get('报告解读附录', []))}
        
        <div style="text-align: center; margin-top: 40px; color: #475569; font-size: 12px;">
            Generated by AI Agent Strategy Analysis
        </div>
    </div>
</body>
</html>
"""
