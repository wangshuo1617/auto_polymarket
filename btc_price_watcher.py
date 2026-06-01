#!/usr/bin/env python3
"""BTC 价格自适应邮件播报。

目标: 平时低频保底,价格异动或接近月度 barrier 时自动缩短邮件间隔;
北京时间睡觉时段静默,避免打扰。
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import requests

from config import ENABLE_BTC_HOURLY_EMAIL, TO_EMAIL
from data.binance import get_btc_price
from data.polymarket import get_event_situation, get_positions
from notifications.email import EmailSender
from services.profit_optimizer import _extract_strike_and_direction

logger = logging.getLogger(__name__)
BJ_TZ = ZoneInfo("Asia/Shanghai")
STATE_PATH = Path("logs/btc_price_watcher_state.json")


@dataclass
class WatchConfig:
    poll_seconds: int
    quiet_start_hour: int
    quiet_start_minute: int
    quiet_end_hour: int
    quiet_end_minute: int
    normal_interval: int
    watch_interval: int
    alert_interval: int
    urgent_interval: int
    startup_send: bool
    dry_run: bool


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        logger.warning("环境变量 %s=%r 不是整数,使用默认 %s", name, raw, default)
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _setup_logging() -> None:
    Path("logs").mkdir(exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.handlers.RotatingFileHandler(
        "logs/btc_price_watcher.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(fmt)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def _parse_hhmm(text: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        h, m = str(text).strip().split(":", 1)
        hh = int(h)
        mm = int(m)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
        return hh, mm
    except Exception:
        return default


def load_config(args: argparse.Namespace) -> WatchConfig:
    qs = _parse_hhmm(os.getenv("BTC_WATCHER_QUIET_START", "22:00"), (22, 0))
    qe = _parse_hhmm(os.getenv("BTC_WATCHER_QUIET_END", "06:00"), (6, 0))
    return WatchConfig(
        poll_seconds=max(15, int(args.poll_seconds or _env_int("BTC_WATCHER_POLL_SECONDS", 60))),
        quiet_start_hour=qs[0],
        quiet_start_minute=qs[1],
        quiet_end_hour=qe[0],
        quiet_end_minute=qe[1],
        normal_interval=max(300, _env_int("BTC_WATCHER_NORMAL_INTERVAL", 3600)),
        watch_interval=max(300, _env_int("BTC_WATCHER_WATCH_INTERVAL", 1800)),
        alert_interval=max(300, _env_int("BTC_WATCHER_ALERT_INTERVAL", 900)),
        urgent_interval=max(300, _env_int("BTC_WATCHER_URGENT_INTERVAL", 300)),
        startup_send=not args.no_startup_send and _env_bool("BTC_WATCHER_STARTUP_SEND", True),
        dry_run=bool(args.dry_run),
    )


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def _is_quiet_time(now: datetime, cfg: WatchConfig) -> bool:
    local = now.astimezone(BJ_TZ)
    cur_min = local.hour * 60 + local.minute
    start = cfg.quiet_start_hour * 60 + cfg.quiet_start_minute
    end = cfg.quiet_end_hour * 60 + cfg.quiet_end_minute
    if start <= end:
        return start <= cur_min < end
    return cur_min >= start or cur_min < end


def _fetch_1m_klines(limit: int = 65) -> list:
    resp = requests.get(
        "https://data-api.binance.vision/api/v3/klines",
        params={"symbol": "BTCUSDT", "interval": "1m", "limit": limit},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _pct_change(current: float, previous: Optional[float]) -> Optional[float]:
    if previous is None or previous <= 0:
        return None
    return (current / previous - 1.0) * 100.0


def _recent_moves(current_price: float) -> dict[str, Optional[float]]:
    try:
        klines = _fetch_1m_klines(limit=65)
    except Exception as exc:
        logger.warning("获取 Binance 1m K 线失败: %s", exc)
        return {"move_5m_pct": None, "move_15m_pct": None, "move_60m_pct": None}
    closes: list[float] = []
    for k in klines:
        try:
            closes.append(float(k[4]))
        except (TypeError, ValueError, IndexError):
            continue
    if not closes:
        return {"move_5m_pct": None, "move_15m_pct": None, "move_60m_pct": None}
    return {
        "move_5m_pct": _pct_change(current_price, closes[-6] if len(closes) >= 6 else None),
        "move_15m_pct": _pct_change(current_price, closes[-16] if len(closes) >= 16 else None),
        "move_60m_pct": _pct_change(current_price, closes[-61] if len(closes) >= 61 else None),
    }


def _position_barrier_index() -> dict[tuple[str, int], dict[str, Any]]:
    """按 (direction, strike) 汇总当前 BTC 月度持仓方向。

    outcome=Yes 表示 barrier 逼近对持仓通常是有利方向,不需要因此提频；
    outcome=No 表示 barrier 逼近是风险方向,维持原提频规则。
    """
    try:
        positions = get_positions(profile="analyze") or []
    except Exception as exc:
        logger.warning("获取 Polymarket 持仓失败: %s", exc)
        return {}
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for pos in positions:
        title = str(pos.get("title") or "")
        event_slug = str(pos.get("eventSlug") or "")
        if "what-price-will-bitcoin-hit-in" not in event_slug and "Bitcoin" not in title:
            continue
        strike, direction = _extract_strike_and_direction(title)
        outcome = str(pos.get("outcome") or "").strip().title()
        if strike is None or strike <= 0 or outcome not in {"Yes", "No"}:
            continue
        key = (direction, int(round(strike)))
        item = out.setdefault(
            key,
            {
                "outcome": outcome,
                "size": 0.0,
                "current_value": 0.0,
                "initial_value": 0.0,
                "title": title,
            },
        )
        try:
            item["size"] += float(pos.get("size") or 0.0)
            item["current_value"] += float(pos.get("currentValue") or 0.0)
            item["initial_value"] += float(pos.get("initialValue") or 0.0)
        except (TypeError, ValueError):
            pass
        # 同一 strike 理论上不会同时有 Yes/No；若有,保守标为 No。
        if outcome == "No":
            item["outcome"] = "No"
    return out


def _nearest_barrier(current_price: float) -> dict[str, Any]:
    try:
        event = get_event_situation()
    except Exception as exc:
        logger.warning("获取 Polymarket barrier 失败: %s", exc)
        return {}
    position_index = _position_barrier_index()
    nearest: Optional[dict[str, Any]] = None
    for market in event.get("markets") or []:
        question = market.get("question") or ""
        strike, direction = _extract_strike_and_direction(question)
        if strike is None or strike <= 0:
            continue
        active = market.get("active")
        closed = market.get("closed") or market.get("resolved") or market.get("isResolved")
        if active is False or closed:
            continue
        if direction == "above" and strike < current_price:
            continue
        if direction == "below" and strike > current_price:
            continue
        distance_pct = abs(strike / current_price - 1.0) * 100.0
        pos = position_index.get((direction, int(round(strike)))) or {}
        item = {
            "question": question,
            "strike": strike,
            "direction": direction,
            "distance_pct": distance_pct,
            "position_outcome": pos.get("outcome"),
            "position_size": pos.get("size"),
            "position_current_value": pos.get("current_value"),
            "position_title": pos.get("title"),
        }
        if nearest is None or distance_pct < nearest["distance_pct"]:
            nearest = item
    return nearest or {}


def _risk_level(
    *,
    moves: dict[str, Optional[float]],
    barrier: dict[str, Any],
) -> tuple[str, list[str]]:
    reasons: list[str] = []
    m5 = abs(moves.get("move_5m_pct") or 0.0)
    m15 = abs(moves.get("move_15m_pct") or 0.0)
    m60 = abs(moves.get("move_60m_pct") or 0.0)
    dist = float(barrier.get("distance_pct") or 999.0)
    position_outcome = str(barrier.get("position_outcome") or "").title()

    level = "normal"
    if position_outcome == "No":
        if dist <= 0.5:
            level = "urgent"; reasons.append(f"No 持仓 barrier 距离 {dist:.2f}%")
        elif dist <= 1.0:
            level = "alert"; reasons.append(f"No 持仓 barrier 距离 {dist:.2f}%")
        elif dist <= 2.0:
            level = "watch"; reasons.append(f"No 持仓 barrier 距离 {dist:.2f}%")
    elif position_outcome == "Yes" and dist <= 2.0:
        reasons.append(f"Yes 持仓 barrier 距离 {dist:.2f}%,不因逼近提频")
    elif barrier and dist <= 2.0:
        reasons.append(f"最近 barrier 距离 {dist:.2f}%,当前无匹配持仓不提频")

    if m5 >= 0.35:
        level = "urgent"; reasons.append(f"5m 变动 {moves['move_5m_pct']:+.2f}%")
    elif m15 >= 0.90:
        level = "urgent"; reasons.append(f"15m 变动 {moves['move_15m_pct']:+.2f}%")
    elif m15 >= 0.60 or m60 >= 1.20:
        if level not in {"urgent"}:
            level = "alert"
        if m15 >= 0.60:
            reasons.append(f"15m 变动 {moves['move_15m_pct']:+.2f}%")
        if m60 >= 1.20:
            reasons.append(f"60m 变动 {moves['move_60m_pct']:+.2f}%")
    elif m15 >= 0.35 or m60 >= 0.80:
        if level == "normal":
            level = "watch"
        if m15 >= 0.35:
            reasons.append(f"15m 变动 {moves['move_15m_pct']:+.2f}%")
        if m60 >= 0.80:
            reasons.append(f"60m 变动 {moves['move_60m_pct']:+.2f}%")

    if not reasons:
        reasons.append("无明显异动,按小时保底播报")
    return level, reasons


def _interval_for_level(level: str, cfg: WatchConfig) -> int:
    return {
        "urgent": cfg.urgent_interval,
        "alert": cfg.alert_interval,
        "watch": cfg.watch_interval,
    }.get(level, cfg.normal_interval)


def _fmt_move(value: Optional[float]) -> str:
    return "N/A" if value is None else f"{value:+.2f}%"


def _build_email(
    *,
    price: float,
    moves: dict[str, Optional[float]],
    barrier: dict[str, Any],
    level: str,
    reasons: list[str],
    interval_sec: int,
    quiet: bool,
) -> tuple[str, str]:
    now_bj = datetime.now(timezone.utc).astimezone(BJ_TZ)
    level_label = {"normal": "常规", "watch": "关注", "alert": "警戒", "urgent": "紧急"}[level]
    subject = f"BTC价格播报 {level_label}: ${price:,.0f}"
    if barrier:
        subject += f" | 最近barrier {barrier['distance_pct']:.2f}%"
    body_lines = [
        f"时间(BJ): {now_bj:%Y-%m-%d %H:%M:%S}",
        f"BTC: ${price:,.2f}",
        "",
        "近期变化:",
        f"- 5m:  {_fmt_move(moves.get('move_5m_pct'))}",
        f"- 15m: {_fmt_move(moves.get('move_15m_pct'))}",
        f"- 60m: {_fmt_move(moves.get('move_60m_pct'))}",
        "",
        f"风险等级: {level_label} ({level})",
        "触发原因: " + "；".join(reasons),
        f"当前播报间隔: {interval_sec // 60} 分钟",
    ]
    if barrier:
        body_lines.extend([
            "",
            "最近月度 barrier:",
            f"- {barrier.get('question')}",
            f"- strike: ${barrier.get('strike'):,.0f}",
            f"- direction: {barrier.get('direction')}",
            f"- distance: {barrier.get('distance_pct'):.2f}%",
            f"- position: {barrier.get('position_outcome') or '无匹配持仓'}"
            + (
                f" / value ${float(barrier.get('position_current_value') or 0.0):,.0f}"
                if barrier.get("position_outcome") else ""
            ),
        ])
    if quiet:
        body_lines.append("\n注意: 当前处于北京时间静默时段,本邮件通常不会发送。")
    body_lines.extend([
        "",
        "节奏规则:",
        "- 常规: 60 分钟",
        "- 关注: 30 分钟 (No持仓barrier≤2% 或中等波动)",
        "- 警戒: 15 分钟 (No持仓barrier≤1% 或明显波动)",
        "- 紧急: 5 分钟 (No持仓barrier≤0.5% 或快速异动)",
        "- Yes持仓barrier逼近只展示,不单独提频",
        "- 北京时间睡觉时段不发送邮件,只记录日志",
    ])
    return subject, "\n".join(body_lines)


def _should_send(
    *,
    now_ts: float,
    state: dict[str, Any],
    level: str,
    interval_sec: int,
    startup: bool,
) -> bool:
    last_sent = float(state.get("last_sent_ts") or 0.0)
    last_level = str(state.get("last_level") or "normal")
    if last_sent <= 0:
        return startup
    if now_ts - last_sent >= interval_sec:
        return True
    # 风险升级时,只要距离上一封超过 5 分钟就立即提醒。
    rank = {"normal": 0, "watch": 1, "alert": 2, "urgent": 3}
    if rank.get(level, 0) > rank.get(last_level, 0) and now_ts - last_sent >= 300:
        return True
    return False


def run_once(cfg: WatchConfig, state: dict[str, Any], *, startup: bool = False) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    now_ts = now.timestamp()
    price = get_btc_price()
    moves = _recent_moves(price)
    barrier = _nearest_barrier(price)
    level, reasons = _risk_level(moves=moves, barrier=barrier)
    interval_sec = _interval_for_level(level, cfg)
    quiet = _is_quiet_time(now, cfg)
    email_enabled = _env_bool("BTC_WATCHER_ENABLE_EMAIL", ENABLE_BTC_HOURLY_EMAIL)
    should_send = (
        email_enabled
        and not quiet
        and _should_send(now_ts=now_ts, state=state, level=level, interval_sec=interval_sec, startup=startup)
    )
    subject, body = _build_email(
        price=price,
        moves=moves,
        barrier=barrier,
        level=level,
        reasons=reasons,
        interval_sec=interval_sec,
        quiet=quiet,
    )
    logger.info(
        "BTC watcher: price=%.2f level=%s interval=%ss quiet=%s send=%s reasons=%s",
        price, level, interval_sec, quiet, should_send, ";".join(reasons)
    )
    if should_send:
        if cfg.dry_run:
            logger.info("DRY-RUN 邮件: %s\n%s", subject, body)
            ok = True
        else:
            ok = EmailSender().send_email(TO_EMAIL, subject, body, content_type="plain")
        if ok and not cfg.dry_run:
            state["last_sent_ts"] = now_ts
            state["last_subject"] = subject
    state["last_level"] = level
    state["last_price"] = price
    state["last_checked_ts"] = now_ts
    state["last_quiet"] = quiet
    _save_state(state)
    return {
        "price": price,
        "moves": moves,
        "barrier": barrier,
        "level": level,
        "interval_sec": interval_sec,
        "quiet": quiet,
        "sent": should_send,
        "subject": subject,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="BTC 自适应邮件播报")
    parser.add_argument("--once", action="store_true", help="只运行一轮")
    parser.add_argument("--dry-run", action="store_true", help="不真实发送邮件")
    parser.add_argument("--poll-seconds", type=int, default=None, help="检查间隔秒数")
    parser.add_argument("--no-startup-send", action="store_true", help="启动时不立即发送首封")
    args = parser.parse_args()

    _setup_logging()
    cfg = load_config(args)
    state = _load_state()
    stopping = False

    def _handle_signal(signum, _frame):
        nonlocal stopping
        logger.info("收到信号 %s,准备退出", signum)
        stopping = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    logger.info(
        "BTC watcher 启动: enabled=%s dry_run=%s poll=%ss quiet=%02d:%02d-%02d:%02d(BJ)",
        _env_bool("BTC_WATCHER_ENABLE_EMAIL", ENABLE_BTC_HOURLY_EMAIL),
        cfg.dry_run,
        cfg.poll_seconds,
        cfg.quiet_start_hour,
        cfg.quiet_start_minute,
        cfg.quiet_end_hour,
        cfg.quiet_end_minute,
    )

    first = True
    while not stopping:
        try:
            run_once(cfg, state, startup=first and cfg.startup_send)
        except Exception:
            logger.exception("BTC watcher 本轮失败")
        if args.once:
            break
        first = False
        for _ in range(cfg.poll_seconds):
            if stopping:
                break
            time.sleep(1)
    logger.info("BTC watcher 已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
