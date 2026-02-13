"""
Polymarket 持仓分析主入口
获取持仓、挂单、K线、市场情绪，经 AI 分析后发送邮件
"""
import json
from pathlib import Path
from datetime import datetime

from config import TO_EMAIL
from data.polymarket import get_positions, get_open_orders, get_event_situation, get_balance_allowance
from data.binance import get_btc_price, get_4h_klines_data
from ai.researcher import analyze_market_with_grounding
from notifications.email import EmailSender
from notifications.html import generate_html_template
from services.position import match_orders_with_positions, format_matched_data
from services.market_sentiment import get_market_sentiment_and_funding

LAST_REPORT_PATH = Path(__file__).resolve().parent / "last_report.json"


def _load_previous_report() -> dict | None:
    """加载上一时间段的报告，供本次 AI 参考。"""
    if not LAST_REPORT_PATH.exists():
        return None
    try:
        with open(LAST_REPORT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _save_report(data: dict) -> None:
    """将本次报告保存为下一轮的「上一时间段报告」。"""
    try:
        with open(LAST_REPORT_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


if __name__ == "__main__":
    email_sender = EmailSender()
    time_now = datetime.now().strftime("%m-%d %H:%M")

    positions = get_positions()
    orders = get_open_orders()
    matched_results = match_orders_with_positions(orders, positions)
    formatted = format_matched_data(matched_results)
    print(f"{time_now} Polymarket持仓情况格式化完成")

    klines_data = get_4h_klines_data()
    print(f"{time_now} 比特币4h K线数据获取完成")

    market_sentiment_and_funding = get_market_sentiment_and_funding()
    print(f"{time_now} 市场情绪与资金面获取完成")

    event_situation = get_event_situation()
    usdc_balance = get_balance_allowance()
    previous_report = _load_previous_report()
    if previous_report:
        print(f"{time_now} 已加载上一时间段报告作为参考")
    print(f"{time_now} Polymarket 事件/市场现价与 USDC 余额获取完成,开始进行AI分析")

    analyze_result = analyze_market_with_grounding(
        formatted,
        klines_data,
        market_sentiment_and_funding,
        event_situation,
        usdc_balance,
        previous_report=previous_report,
    )
    warn_prices = analyze_result["预警信号"]
    for warn_price in warn_prices:
        warn_price["alert_status"] = False
    with open("price_warn_config.py", "w") as f:
        f.write(f"WARN_PRICE = {warn_prices}")
    print(f"{time_now} AI分析完成,开始发送邮件")

    email_subject = f"{time_now} Polymarket持仓情况分析,当前BTC价格: {get_btc_price():,.2f}"
    email_content = generate_html_template(analyze_result)
    with open(f"output/{time_now}_email.html", "w") as f:
        f.write(email_content)
    email_sender.send_html_email(TO_EMAIL, email_subject, email_content)
    _save_report(analyze_result)
    print(f"{time_now} 邮件发送完成")
