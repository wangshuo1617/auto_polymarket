#!/usr/bin/env python3
"""
BTC ETF 盘中买压/卖压代理监控服务。

信号说明：
1. ETF 活跃度：成交节奏 + ETF 相对 BTC 的同一美股时段强弱。
2. Binance 传导：BTCUSDT 现货 taker flow + 期货/OI，用于验证是否真的传导到 BTC 市场。
3. 告警：避开开盘初期噪音，要求连续多次同方向确认后才发邮件。
"""

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import TO_EMAIL
from data.etf import get_etf_combined_signal, get_us_market_monitor_window, get_us_market_session_window
from notifications.email import EmailSender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("etf_volume_monitor")

# ── 配置 ──────────────────────────────────────────────────────────────────────
POLL_INTERVAL = int(os.getenv("ETF_MONITOR_INTERVAL", "1800"))   # 默认 30 分钟
MAX_ALERTS_PER_DAY = int(os.getenv("ETF_MONITOR_MAX_ALERTS", "3"))
SINGLE_THRESHOLD = float(os.getenv("ETF_SINGLE_THRESHOLD", "1.8"))
COMBINED_THRESHOLD = float(os.getenv("ETF_COMBINED_THRESHOLD", "1.5"))
OPEN_GUARD_MINUTES = int(os.getenv("ETF_MONITOR_OPEN_GUARD_MINUTES", "60"))
CONFIRMATIONS_REQUIRED = int(os.getenv("ETF_MONITOR_CONFIRMATIONS", "2"))
ALERT_COOLDOWN_SECONDS = int(os.getenv("ETF_MONITOR_ALERT_COOLDOWN_SECONDS", str(3 * 3600)))
STATE_FILE = Path(os.getenv("ETF_MONITOR_STATE_FILE", "logs/etf_monitor_state.json"))
KEY_ALERT_WINDOWS = [
    {"key": "post_open", "label": "开盘后确认", "start_min": 60, "end_min": 90},
    {"key": "midday", "label": "午盘再平衡", "start_min": 210, "end_min": 240},
    {"key": "last_hour", "label": "尾盘一小时", "start_min": 330, "end_min": 360},
    {"key": "pre_close", "label": "收盘前确认", "start_min": 360, "end_min": 390},
]


def _in_us_market_hours() -> bool:
    now = datetime.now(timezone.utc)
    if now.astimezone().weekday() >= 5:
        return False
    open_utc, close_utc = get_us_market_monitor_window(now)
    return open_utc <= now <= close_utc


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {}
    try:
        with STATE_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:
        logger.warning("读取 ETF 监控状态失败，使用空状态: %s", exc)
        return {}


def _save_state(state: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        with STATE_FILE.open("w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("保存 ETF 监控状态失败: %s", exc)


def _market_open_guard_active(now: datetime) -> tuple[bool, int]:
    session_open_utc, _ = get_us_market_session_window(now)
    elapsed_minutes = max(int((now - session_open_utc).total_seconds() // 60), 0)
    return elapsed_minutes < OPEN_GUARD_MINUTES, elapsed_minutes


def _elapsed_session_minutes(now: datetime) -> int:
    session_open_utc, _ = get_us_market_session_window(now)
    return max(int((now - session_open_utc).total_seconds() // 60), 0)


def _pressure_labels(direction_code: str) -> tuple[str, str, str]:
    mapping = {
        "BUY_PRESSURE": ("📈", "买压增强", "买压↑"),
        "SELL_PRESSURE": ("📉", "卖压增强", "卖压↓"),
        "NEUTRAL": ("➡️", "方向不明", "中性→"),
    }
    return mapping[direction_code]


def _signal_pressure(signal: dict) -> dict:
    return signal.get("pressure_direction") or signal.get("direction") or {
        "direction": "NEUTRAL",
        "confidence": "LOW",
    }


def _build_email_body(signal: dict) -> str:
    vol = signal["volume"]
    direction = signal["direction"]
    binance = signal.get("binance_pressure") or {}
    pressure = _signal_pressure(signal)
    strength = signal["signal_strength"]
    streak_count = signal.get("streak_count", 0)
    confirmations_required = signal.get("confirmations_required", CONFIRMATIONS_REQUIRED)
    _, dir_cn, _ = _pressure_labels(pressure["direction"])
    alert_kind = signal.get("alert_kind", "regular")
    key_window_label = signal.get("key_window_label")

    title = "🚨 BTC ETF 盘中代理信号"
    if alert_kind == "key_time":
        title = f"🕒 BTC ETF 关键时点快照 — {key_window_label}"

    lines = [
        f"{title} — 强度: {strength}",
        "=" * 45,
        f"检测时间 (UTC): {signal['fetched_at']}",
        f"对齐到的行情时间 (UTC): {direction['aligned_end_utc']}",
        "",
        "【综合判断（ETF 异常 + Binance 传导）】",
        f"  方向判断: {dir_cn}",
        f"  置信度: {pressure.get('confidence', 'LOW')}",
        f"  来源: {pressure.get('source', 'UNKNOWN')}",
        f"  ETF 与 Binance 同向: {pressure.get('etf_binance_aligned', False)}",
        "",
        "【ETF 价格/成交代理】",
        f"  BTC 同时段涨跌:     {direction['btc_ret_today_pct']:+.3f}%",
        f"  ETF 加权超额收益:   {direction['composite_excess_pct']:+.3f}%",
        f"  ETF相对强弱: {direction['direction']} / {direction['confidence']}",
        f"  breadth: {direction['same_direction_count']}/5 同向确认",
        f"  连续确认: {streak_count}/{confirmations_required}",
        "",
        "  各 ETF 超额明细:",
    ]
    for ticker, d in direction["tickers"].items():
        excess = d.get("excess_vs_btc_pct")
        excess_str = f"{excess:+.3f}%" if excess is not None else "N/A"
        lines.append(
            f"    {ticker}: 今日 {d['ret_today_pct']:+.3f}%  超额 {excess_str}"
        )

    spot = binance.get("spot") or {}
    futures = binance.get("futures") or {}
    combined = binance.get("combined") or {}
    lines += [
        "",
        "【Binance BTC 市场传导】",
        f"  现货方向: {spot.get('direction', 'N/A')} / {spot.get('confidence', 'N/A')}",
        f"  现货涨跌: {spot.get('ret_pct', 0):+.3f}%",
        f"  现货主动净额: {spot.get('net_taker_quote_usd_m', 0):+.2f}M USDT",
        f"  现货 taker buy ratio: {spot.get('taker_buy_ratio', 0):.2%}",
        f"  现货成交额: ${spot.get('quote_volume_usd_m', 0):.0f}M",
        f"  传导判断: {combined.get('direction', 'N/A')} / {combined.get('confidence', 'N/A')}",
        f"  杠杆备注: {combined.get('leverage_note', 'N/A')}",
    ]
    if futures.get("available"):
        oi = futures.get("open_interest") or {}
        lines += [
            f"  期货主动净额: {futures.get('net_taker_quote_usd_m', 0):+.2f}M USDT",
            f"  期货 taker buy ratio: {futures.get('taker_buy_ratio', 0):.2%}",
            f"  OI 变化: {oi.get('oi_value_change_pct')}%",
        ]
    elif futures:
        lines.append(f"  期货数据: unavailable ({futures.get('error', 'unknown')})")

    lines += [
        "",
        "【成交节奏（仅作辅助确认）】",
        f"  当前时段进度: {vol['session_progress_pct']:.1f}%",
        f"  合计原始成交比: {vol['combined_ratio']:.2f}x",
        f"  合计节奏比:     {vol['combined_pace_ratio']:.2f}x  总成交额: ${vol['total_vol_usd_m']:.0f}M",
    ]
    for ticker, d in vol["etfs"].items():
        flag = "  ⚠️ 节奏异常" if d["pace_ratio"] >= SINGLE_THRESHOLD else ""
        lines.append(
            f"  {ticker} ({d['name']}): 原始 {d['raw_ratio']:.2f}x / 节奏 {d['pace_ratio']:.2f}x  ${d['vol_usd_m']:.0f}M{flag}"
        )

    lines += [
        "",
        "【说明】",
        "这不是官方实时净流入数据，而是 ETF 活跃度 + BTC 市场传导的实时压力模型。",
        "普通告警只有在 ETF 活跃度和/或 Binance 现货传导连续确认后才会触发。",
        "关键时点快照不会占用普通告警额度，用于保底同步盘中状态。",
        "精确净流量金额仍需等待盘后 Farside / 官方口径确认。",
    ]
    return "\n".join(lines)


def _update_streak(state: dict, signal: dict, today: date) -> tuple[int, str | None]:
    if state.get("streak_day") != today.isoformat():
        state["streak_day"] = today.isoformat()
        state["streak_label"] = None
        state["streak_count"] = 0

    direction_code = _signal_pressure(signal)["direction"]
    strength = signal["signal_strength"]
    candidate_label = direction_code if strength in ("STRONG", "MODERATE") else None

    if not candidate_label:
        state["streak_label"] = None
        state["streak_count"] = 0
        return 0, None

    if candidate_label == state.get("streak_label"):
        state["streak_count"] = int(state.get("streak_count", 0)) + 1
    else:
        state["streak_label"] = candidate_label
        state["streak_count"] = 1
    return int(state["streak_count"]), candidate_label


def _within_cooldown(state: dict, now: datetime, label: str) -> bool:
    if state.get("last_alert_label") != label:
        return False
    raw = state.get("last_alert_at_utc")
    if not raw:
        return False
    try:
        last_alert = datetime.fromisoformat(raw)
    except ValueError:
        return False
    return (now - last_alert).total_seconds() < ALERT_COOLDOWN_SECONDS


def _should_alert(state: dict, signal: dict, now: datetime, today: date) -> tuple[bool, str]:
    guard_active, elapsed_minutes = _market_open_guard_active(now)
    if guard_active:
        return False, f"opening_guard_{elapsed_minutes}m"

    streak_count, label = _update_streak(state, signal, today)
    signal["streak_count"] = streak_count
    signal["confirmations_required"] = CONFIRMATIONS_REQUIRED
    if not label:
        return False, "strength_not_high_enough"

    if streak_count < CONFIRMATIONS_REQUIRED:
        return False, f"awaiting_confirmation_{streak_count}/{CONFIRMATIONS_REQUIRED}"

    alerts_sent_today = int(state.get("alerts_sent_today", 0))
    if alerts_sent_today >= MAX_ALERTS_PER_DAY:
        return False, "daily_limit_reached"

    if _within_cooldown(state, now, label):
        return False, f"same_direction_within_cooldown_{ALERT_COOLDOWN_SECONDS}s"

    return True, "confirmed"


def _get_due_key_window(state: dict, now: datetime, today: date) -> dict | None:
    key_alerts_sent = state.setdefault("key_alerts_sent", {})
    day_key = today.isoformat()
    if not isinstance(key_alerts_sent.get(day_key), dict):
        key_alerts_sent[day_key] = {}

    elapsed_minutes = _elapsed_session_minutes(now)
    for window in KEY_ALERT_WINDOWS:
        if not (window["start_min"] <= elapsed_minutes < window["end_min"]):
            continue
        if key_alerts_sent[day_key].get(window["key"]):
            continue
        return window
    return None


def _mark_key_window_sent(state: dict, today: date, window_key: str) -> None:
    key_alerts_sent = state.setdefault("key_alerts_sent", {})
    day_key = today.isoformat()
    if not isinstance(key_alerts_sent.get(day_key), dict):
        key_alerts_sent[day_key] = {}
    key_alerts_sent[day_key][window_key] = datetime.now(timezone.utc).isoformat()


def _send_alert(sender: EmailSender, signal: dict, today: date) -> bool:
    if not TO_EMAIL:
        logger.warning("TO_EMAIL 未配置，跳过邮件发送")
        return False
    direction = signal["direction"]
    pressure = _signal_pressure(signal)
    vol = signal["volume"]
    _, _, dir_short = _pressure_labels(pressure["direction"])
    if signal.get("alert_kind") == "key_time":
        subject = (
            f"[ETF关键时点] {signal.get('key_window_label')} | {dir_short}"
            f" ETF超额{direction['composite_excess_pct']:+.2f}%"
            f" | Binance {pressure.get('source', 'N/A')}"
            f" | breadth {direction['same_direction_count']}/5"
            f" | 节奏{vol['combined_pace_ratio']:.2f}x"
            f" | {today.strftime('%Y-%m-%d')}"
        )
    else:
        subject = (
            f"[ETF传导代理] {dir_short} ETF超额{direction['composite_excess_pct']:+.2f}%"
            f" | Binance {pressure.get('confidence', 'LOW')}"
            f" | breadth {direction['same_direction_count']}/5"
            f" | 节奏{vol['combined_pace_ratio']:.2f}x"
            f" | 连续{signal.get('streak_count', 0)}次"
            f" | {today.strftime('%Y-%m-%d')}"
        )
    body = _build_email_body(signal)
    ok = sender.send_email(TO_EMAIL, subject, body, content_type="plain")
    if ok:
        logger.info("ETF 邮件已发送: kind=%s", signal.get("alert_kind", "regular"))
    else:
        logger.error("ETF 邮件发送失败: kind=%s", signal.get("alert_kind", "regular"))
    return ok


def main() -> None:
    logger.info("ETF 盘中买压/卖压代理监控服务启动")
    logger.info(
        f"配置: 轮询间隔={POLL_INTERVAL}s  单只量能阈值={SINGLE_THRESHOLD}x"
        f"  合计量能阈值={COMBINED_THRESHOLD}x  开盘保护={OPEN_GUARD_MINUTES}m"
        f"  连续确认={CONFIRMATIONS_REQUIRED} 次  每日最多告警={MAX_ALERTS_PER_DAY}"
    )

    sender = EmailSender()
    state = _load_state()

    while True:
        now = datetime.now(timezone.utc)
        today = datetime.now(timezone.utc).date()

        if state.get("day") != today.isoformat():
            state["day"] = today.isoformat()
            state["alerts_sent_today"] = 0
            state["last_alert_label"] = None
            state["last_alert_at_utc"] = None
            state["key_alerts_sent"] = {}

        if not _in_us_market_hours():
            logger.debug("美股休市，暂停轮询，60 秒后再检查")
            _save_state(state)
            time.sleep(60)
            continue

        try:
            logger.info("检查 ETF 盘中代理...")
            signal = get_etf_combined_signal(
                volume_single_threshold=SINGLE_THRESHOLD,
                volume_combined_threshold=COMBINED_THRESHOLD,
            )
            logger.info(f"  {signal['summary']}")
            should_alert, reason = _should_alert(state, signal, now, today)
            due_key_window = _get_due_key_window(state, now, today)

            if due_key_window:
                key_signal = dict(signal)
                key_signal["alert_kind"] = "key_time"
                key_signal["key_window_label"] = due_key_window["label"]
                logger.warning(
                    "发送 ETF 关键时点快照: window=%s strength=%s",
                    due_key_window["key"],
                    key_signal["signal_strength"],
                )
                if _send_alert(sender, key_signal, today):
                    _mark_key_window_sent(state, today, due_key_window["key"])
                    state["last_alert_label"] = signal["direction"]["direction"]
                    state["last_alert_at_utc"] = now.isoformat()
            elif should_alert:
                signal["alert_kind"] = "regular"
                logger.warning(
                    "触发 ETF 盘中代理预警: strength=%s streak=%s/%s",
                    signal["signal_strength"],
                    signal.get("streak_count", 0),
                    CONFIRMATIONS_REQUIRED,
                )
                if _send_alert(sender, signal, today):
                    state["alerts_sent_today"] = int(state.get("alerts_sent_today", 0)) + 1
                    state["last_alert_label"] = signal["direction"]["direction"]
                    state["last_alert_at_utc"] = now.isoformat()
            else:
                logger.info("  本轮不发邮件: %s", reason)
        except Exception as e:
            logger.exception(f"ETF 信号检查失败: {e}")

        _save_state(state)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
