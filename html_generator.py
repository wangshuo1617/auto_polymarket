import json

# --- 1. 生成内部 HTML 片段的辅助函数 ---

def generate_defensive_rows(items):
    rows = ""
    for item in items:
        # 根据状态定义颜色
        status_color = "#10b981" if "安全" in item['状态'] else "#6b7280"
        
        rows += f"""
        <div class="card defensive-card">
            <div class="card-header" style="border-left: 4px solid {status_color};">
                <div class="contract-title">{item['合约']}</div>
                <div class="status-badge" style="background: {status_color}20; color: {status_color};">{item['状态']}</div>
            </div>
            <div class="card-body">
                <div class="action-box" style="margin-bottom: 8px;">🧮 <strong>希腊值分析:</strong> {item.get('希腊值分析','')}</div>
                <div class="action-box">👉 <strong>建议:</strong> {item['操作建议']}</div>
                <div class="logic-text">{item['逻辑']}</div>
            </div>
        </div>
        """
    return rows

def generate_offensive_rows(items):
    rows = ""
    for item in items:
        # 解析阶梯数据
        ladders = item['阶梯挂单建议']
        ladder_html = ""
        for key, val in ladders.items():
            # 定义不同阶梯的图标
            icon = "🛡️" if key == "安全阀" else ("🎯" if key == "目标位" else "🚀")
            position_pct = val.get("仓位百分比")
            position_pct_text = f"{position_pct}%" if position_pct is not None else ""
            ladder_html += f"""
            <div class="ladder-step">
                <div class="step-price">
                    <span class="step-icon">{icon}</span> {key}: <strong>{val['价格']}¢</strong>
                    <span style="color: #94a3b8; font-weight: 500; margin-left: 8px;">仓位: {position_pct_text}</span>
                </div>
                <div class="step-logic">{val['逻辑']}</div>
            </div>
            """

        rows += f"""
        <div class="card offensive-card">
            <div class="card-header" style="border-left: 4px solid #f59e0b;">
                <div class="contract-title">{item['合约']}</div>
                <div class="status-badge" style="background: #f59e0b20; color: #f59e0b;">{item['状态']}</div>
            </div>
            <div class="card-body">
                <div class="action-box" style="margin-bottom: 8px;">🧮 <strong>希腊值分析:</strong> {item.get('希腊值分析','')}</div>
                <div class="action-box warning">👉 <strong>建议:</strong> {item['操作建议']}</div>
                <div class="ladder-container">
                    {ladder_html}
                </div>
            </div>
        </div>
        """
    return rows

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
        </div>
        """
    return rows

def generate_market_snapshot(snapshot_text):
    """生成市场快照部分的 HTML"""
    if not snapshot_text:
        return ""
    
    return f"""
        <h2 style="color: #3b82f6; margin-top: 20px;">📈 市场与持仓快照 (Market Snapshot)</h2>
        <div class="market-snapshot">
            <div class="market-snapshot-content">{snapshot_text}</div>
        </div>
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

    </style>
</head>
<body>
    <div class="container">
        <h1>📊 Polymarket 仓位分析报告</h1>

        {generate_market_snapshot(data.get("市场与持仓快照", ""))}

        <h2 style="color: var(--accent-green);">🛡️ 防守端分析 (Defensive)</h2>
        {generate_defensive_rows(data['防守端分析'])}

        <h2 style="color: var(--accent-orange);">⚔️ 进攻端分析 (Offensive)</h2>
        {generate_offensive_rows(data['进攻端分析'])}

        <h2 style="color: var(--accent-red);">🚨 市场预警 (Signals)</h2>
        {generate_alert_rows(data['预警信号'])}
        
        <div style="text-align: center; margin-top: 40px; color: #475569; font-size: 12px;">
            Generated by AI Agent Strategy Analysis
        </div>
    </div>
</body>
</html>
"""
