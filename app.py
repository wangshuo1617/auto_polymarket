import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, render_template, request, jsonify, session, redirect, url_for
from py_clob_client.clob_types import (
    OrderArgs,
    CreateOrderOptions,
    BalanceAllowanceParams,
    AssetType,
    MarketOrderArgs,
    PartialCreateOrderOptions,
    OrderType,
    PostOrdersArgs,
)

logger = logging.getLogger(__name__)

# 添加项目根目录到 sys.path
_project_root = Path(__file__).resolve().parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from data.binance import get_btc_price, get_1h_klines_data
from data.polymarket import (
    get_client,
    get_event_situation,
    get_open_orders,
    get_positions,
    get_5m_updown_activity_history,
    buy_order,
    sell_order,
    cancel_order,
    get_balance_allowance,
    get_event_token_id,
)

app = Flask(__name__)
app.secret_key = os.getenv("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)
app.config["SESSION_COOKIE_NAME"] = "pm_dashboard_session"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("DASHBOARD_HTTPS_ONLY", "false").lower() == "true"
APP_PM_PROFILE = (os.getenv("POLYMARKET_PROFILE", "analyze") or "analyze").strip().lower()
TRADE_PM_PROFILE = "trade"
ET_TIMEZONE = ZoneInfo("America/New_York")
UTC8_TIMEZONE = ZoneInfo("Asia/Shanghai")
STRATEGY_PARAM_DISPLAY_ORDER = [
    "entry_minute",
    "entry_preclose_sec",
    "min_direction_diff",
    "max_entry_price",
    "stake_usd",
    "report_interval_sec",
    "min_hold_before_close_sec",
    "exit_mode",
    "tp_price_cap",
    "tp_value_cap",
    "sl_to_tp_ratio",
    "toxic_utc_hours",
    "trade_db_path",
    "enable_risk_sizing",
    "risk_min_stake_ratio",
    "risk_max_stake_ratio",
    "risk_diff_boost_threshold",
    "risk_diff_boost_multiplier",
    "cross_borderline_diff_multiplier",
    "stake_cap_very_high",
    "stake_cap_high",
    "stake_cap_medium_high",
    "medium_high_threshold",
    "confidence_boost_ge_095",
    "risk_w_price",
    "risk_w_direction",
    "risk_w_stability",
    "enable_direction_confirm_close",
    "direction_confirm_preclose_sec",
    "direction_confirm_min_abs_diff",
    "enable_direction_confirm_low_diff_close",
    "direction_confirm_low_diff_threshold",
    "enable_last_seconds_reverse_guard",
    "reverse_guard_start_sec",
    "reverse_guard_lookback_sec",
    "reverse_guard_btc_move",
    "reverse_guard_require_cross_open",
    "enable_last_seconds_position_guard",
    "position_guard_start_sec",
    "position_guard_min_consecutive_sec",
]


def _is_authenticated() -> bool:
    return session.get("dashboard_authed") is True


def _is_api_request() -> bool:
    return request.path.startswith("/api/")


def _login_redirect():
    return redirect(url_for("login", next=request.path))


@app.before_request
def require_authentication():
    if request.endpoint in {"login", "logout", "static"}:
        return None
    if _is_authenticated():
        return None
    if _is_api_request():
        return jsonify({"error": "Unauthorized"}), 401
    return _login_redirect()


@app.route('/login', methods=['GET', 'POST'])
def login():
    next_url = request.args.get('next') or request.form.get('next') or url_for('index')
    if not next_url.startswith('/'):
        next_url = url_for('index')

    password_config = os.getenv("DASHBOARD_PASSWORD", "").strip()
    if request.method == 'POST':
        if not password_config:
            return render_template('login.html', error='Server not configured: set DASHBOARD_PASSWORD in .env', next_url=next_url), 500

        input_password = (request.form.get('password') or '').strip()
        if hmac.compare_digest(input_password, password_config):
            session['dashboard_authed'] = True
            return redirect(next_url)
        return render_template('login.html', error='Invalid password', next_url=next_url), 401

    return render_template('login.html', error=None, next_url=next_url)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/events')
def api_events():
    try:
        data = get_event_token_id()
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/positions')
def api_positions():
    try:
        positions = get_positions(profile=APP_PM_PROFILE)
        # Normalize and add display fields; use conditionId as market_id for sell
        def _to_float(v, default=0.0):
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        result = []
        for p in positions:
            size = _to_float(p.get("size"))
            cur_price = _to_float(p.get("curPrice"))
            avg_price = _to_float(p.get("avgPrice"))
            current_value = p.get("currentValue")
            if current_value is None and size and cur_price is not None:
                current_value = size * cur_price
            initial_value = p.get("initialValue")
            if initial_value is None and size and avg_price is not None:
                initial_value = size * avg_price
            result.append({
                "asset": p.get("asset"),
                "conditionId": p.get("conditionId"),
                "title": p.get("title") or "—",
                "outcome": p.get("outcome") or "—",
                "size": size,
                "avgPrice": avg_price,
                "curPrice": cur_price,
                "initialValue": initial_value,
                "currentValue": current_value,
                "percentPnl": p.get("percentPnl"),
                "endDate": p.get("endDate"),
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/open_orders')
def api_open_orders():
    logger.info("api_open_orders requested")
    try:
        orders = get_open_orders(profile=APP_PM_PROFILE)
        market_client = get_client(APP_PM_PROFILE)

        def _to_float(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        def _format_size(value):
            return str(int(value)) if float(value).is_integer() else str(round(value, 6))

        market_cache = {}
        for order in orders:
            original_size = _to_float(order.get("original_size"))
            matched_size = _to_float(order.get("size_matched"))
            remaining_size = max(original_size - matched_size, 0.0)
            order["remaining_size"] = _format_size(remaining_size)
            order["size"] = f"{_format_size(matched_size)}/{_format_size(original_size)}"

            market_id = (
                order.get("market_id")
                or order.get("market")
                or order.get("condition_id")
                or order.get("conditionId")
            )
            if not market_id:
                order["event_name"] = ""
                continue
            if market_id not in market_cache:
                try:
                    market = market_client.get_market(market_id)
                    market_cache[market_id] = market.get("question") or market.get("title") or ""
                except Exception:
                    market_cache[market_id] = ""
            order["event_name"] = market_cache.get(market_id, "")
        logger.info("api_open_orders success: orders_count=%s", len(orders))
        return jsonify(orders)
    except Exception as e:
        logger.exception("api_open_orders failed: error=%s", e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/buy', methods=['POST'])
def api_buy():
    data = request.json or {}
    market_id = data.get('market_id')
    token_id = data.get('token_id')
    price = data.get('price')
    size = data.get('size')
    logger.info("api_buy requested: market_id=%s price=%s size=%s", market_id, price, size)
    if not all([market_id, token_id, price, size]):
        logger.warning("api_buy missing parameters: has market_id=%s token_id=%s price=%s size=%s", bool(market_id), bool(token_id), price, size)
        return jsonify({'error': 'Missing parameters'}), 400
    try:
        order_id = buy_order(
            market_id,
            token_id,
            float(price),
            float(size),
            profile=APP_PM_PROFILE,
        )
        if order_id is None:
            logger.warning("api_buy returned null order_id: market_id=%s price=%s size=%s", market_id, price, size)
            return jsonify({'error': 'Order placement failed (null order_id)', 'order_id': None}), 500
        logger.info("api_buy success: order_id=%s", order_id)
        return jsonify({'order_id': order_id})
    except Exception as e:
        logger.exception("api_buy exception: market_id=%s error=%s", market_id, e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/sell', methods=['POST'])
def api_sell():
    data = request.json or {}
    market_id = data.get('market_id')
    token_id = data.get('token_id')
    price = data.get('price')
    size = data.get('size')
    logger.info("api_sell requested: market_id=%s price=%s size=%s", market_id, price, size)
    if not all([market_id, token_id, price, size]):
        logger.warning("api_sell missing parameters: has market_id=%s token_id=%s price=%s size=%s", bool(market_id), bool(token_id), price, size)
        return jsonify({'error': 'Missing parameters'}), 400
    try:
        order_id = sell_order(
            market_id,
            token_id,
            float(price),
            float(size),
            profile=APP_PM_PROFILE,
            order_type=OrderType.GTC,
        )
        if order_id is None:
            logger.warning("api_sell returned null order_id: market_id=%s price=%s size=%s", market_id, price, size)
            return jsonify({'error': 'Order placement failed (null order_id)', 'order_id': None}), 500
        logger.info("api_sell success: order_id=%s", order_id)
        return jsonify({'order_id': order_id})
    except Exception as e:
        logger.exception("api_sell exception: market_id=%s error=%s", market_id, e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/cancel', methods=['POST'])
def api_cancel():
    data = request.json or {}
    order_id = data.get('order_id')
    logger.info("api_cancel requested: order_id=%s", order_id)
    if not order_id:
        logger.warning("api_cancel missing order_id")
        return jsonify({'error': 'Missing order_id'}), 400
    try:
        result = cancel_order(order_id, profile=APP_PM_PROFILE)
        logger.info("api_cancel success: order_id=%s result=%s", order_id, result)
        return jsonify({'result': result})
    except Exception as e:
        logger.exception("api_cancel failed: order_id=%s error=%s", order_id, e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/balance')
def api_balance():
    try:
        balance = get_balance_allowance(profile=APP_PM_PROFILE)
        return jsonify({'balance': balance})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/btc_1h_kline')
def api_btc_1h_kline():
    """Return current BTC price and latest 1h kline OHLC."""
    try:
        price = get_btc_price()
        klines = get_1h_klines_data(limit=1)
        if not klines:
            return jsonify({"price": price, "open": None, "high": None, "low": None, "close": None})
        candle = klines[0]
        return jsonify({
            "price": price,
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _parse_cash_balance(balance_str: str) -> float:
    """Parse '$123.45' or similar to float."""
    s = (balance_str or "").replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _format_et_time(iso_text: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return dt.astimezone(ET_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _format_utc_time(iso_text: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso_text).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        return dt.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(iso_text or "")[:19]


def _load_activity_exit_by_slug(profile: str) -> tuple[dict[str, float], dict[str, int]]:
    exit_usdc_by_slug: dict[str, float] = {}
    exit_count_by_slug: dict[str, int] = {}
    activity = get_5m_updown_activity_history(profile=profile)
    for item in activity:
        if not isinstance(item, dict):
            continue
        slug = str(item.get("eventSlug") or item.get("slug") or "").strip().lower()
        if not slug:
            continue
        event_type = str(item.get("type") or "").upper()
        side = str(item.get("side") or "").upper()
        if not ((event_type == "TRADE" and side == "SELL") or event_type == "REDEEM"):
            continue
        try:
            usdc_size = float(item.get("usdcSize") or 0.0)
        except Exception:
            usdc_size = 0.0
        if usdc_size <= 0:
            continue
        exit_usdc_by_slug[slug] = exit_usdc_by_slug.get(slug, 0.0) + usdc_size
        exit_count_by_slug[slug] = exit_count_by_slug.get(slug, 0) + 1
    return exit_usdc_by_slug, exit_count_by_slug


def _load_trade_balance_series(conn: sqlite3.Connection, limit: int = 240) -> list[dict]:
    rows = conn.execute(
        """
        SELECT ts_utc, balance
        FROM usdc_balance_snapshots
        WHERE profile = ?
        ORDER BY ts_utc DESC
        LIMIT ?
        """,
        (TRADE_PM_PROFILE, int(limit)),
    ).fetchall()
    rows = list(reversed(rows))
    result = []
    for row in rows:
        result.append({
            "ts": str(row["ts_utc"]),
            "balance": round(float(row["balance"]), 2),
        })
    return result


def _load_latest_trade_strategy_params(conn: sqlite3.Connection) -> dict:
    try:
        row = conn.execute(
            """
            SELECT start_ts_sec, params_json, strategy_signature, created_at
            FROM trade_startups
            WHERE mode='live'
              AND COALESCE(dry_run, 0)=0
            ORDER BY start_ts_sec DESC, id DESC
            LIMIT 1
            """
        ).fetchone()
    except Exception:
        return {}

    if row is None:
        return {}

    params = {}
    try:
        params = json.loads(str(row["params_json"] or "{}"))
        if not isinstance(params, dict):
            params = {}
    except Exception:
        params = {}

    if "exit_mode" not in params:
        signature = str(row["strategy_signature"] or "")
        m = re.search(r"(?:^|,)exit=([^,]+)", signature)
        if m:
            params["exit_mode"] = str(m.group(1) or "").strip()

    ordered_params: dict = {}
    for key in STRATEGY_PARAM_DISPLAY_ORDER:
        if key in params:
            ordered_params[key] = params.get(key)
    for key, value in params.items():
        if key not in ordered_params:
            ordered_params[key] = value
    start_ts_sec = int(row["start_ts_sec"] or 0)
    start_time_utc8 = ""
    if start_ts_sec > 0:
        try:
            start_time_utc8 = datetime.fromtimestamp(start_ts_sec, tz=timezone.utc).astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            start_time_utc8 = ""
    params_items = [{"name": k, "value": ordered_params[k]} for k in ordered_params.keys()]
    return {
        "start_ts_sec": start_ts_sec,
        "start_time_utc8": start_time_utc8,
        "created_at": str(row["created_at"] or ""),
        "params": ordered_params,
        "params_items": params_items,
    }


def _load_skipped_windows(conn: sqlite3.Connection, limit: int = 80) -> list[dict]:
    rows = conn.execute(
        """
        SELECT event_time, market_slug, reason
        FROM trade_events
        WHERE mode='live'
          AND side='skip'
          AND market_slug LIKE 'btc-updown-5m-%'
        ORDER BY event_time DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    result = []
    for row in rows:
        raw_time = str(row["event_time"] or "")
        utc_time = _format_utc_time(raw_time)
        result.append({
            "window_slug": str(row["market_slug"] or ""),
            "utc_time": utc_time,
            "et_time": _format_et_time(raw_time),
            "reason": str(row["reason"] or ""),
        })
    return result


def _build_trade_history_rows(rows: list[sqlite3.Row]) -> list[dict]:
    trade_exit_by_slug, trade_exit_count_by_slug = _load_activity_exit_by_slug(TRADE_PM_PROFILE)
    analyze_exit_by_slug, analyze_exit_count_by_slug = _load_activity_exit_by_slug("analyze")
    history_rows: list[dict] = []
    for row in rows:
        slug = str(row["market_slug"] or "").strip().lower()
        analyze_entry_usdc = float(row["analyze_entry_usdc"] or 0.0)
        db_analyze_exit_usdc = float(row["analyze_exit_usdc"] or 0.0)
        analyze_entry_size = float(row["analyze_entry_size"] or 0.0)
        analyze_exit_size = float(row["analyze_exit_size"] or 0.0)
        analyze_buy_count = int(row["analyze_buy_count"] or 0)
        db_analyze_exit_count = int(row["analyze_exit_count"] or 0)

        trade_entry_usdc = float(row["trade_entry_usdc"] or 0.0)
        db_trade_exit_usdc = float(row["trade_exit_usdc"] or 0.0)
        trade_entry_size = float(row["trade_entry_size"] or 0.0)
        trade_exit_size = float(row["trade_exit_size"] or 0.0)
        trade_buy_count = int(row["trade_buy_count"] or 0)
        db_trade_exit_count = int(row["trade_exit_count"] or 0)

        api_trade_exit_usdc = float(trade_exit_by_slug.get(slug, 0.0))
        api_trade_exit_count = int(trade_exit_count_by_slug.get(slug, 0))
        api_analyze_exit_usdc = float(analyze_exit_by_slug.get(slug, 0.0))
        api_analyze_exit_count = int(analyze_exit_count_by_slug.get(slug, 0))

        resolved_trade_exit_usdc = max(db_trade_exit_usdc, api_trade_exit_usdc)
        resolved_trade_exit_count = max(db_trade_exit_count, api_trade_exit_count)
        resolved_analyze_exit_usdc = max(db_analyze_exit_usdc, api_analyze_exit_usdc)
        resolved_analyze_exit_count = max(db_analyze_exit_count, api_analyze_exit_count)

        # 规则：
        # - 分离前窗口（有 analyze_backfill 入场）只按 analyze 账号结算。
        # - 分离后窗口（无 analyze_backfill 入场）按 trade 账号正常结算。
        if analyze_buy_count > 0:
            entry_usdc = analyze_entry_usdc
            exit_usdc = resolved_analyze_exit_usdc
            entry_size = analyze_entry_size
            exit_size = analyze_exit_size
            unresolved = resolved_analyze_exit_count <= 0
        else:
            entry_usdc = trade_entry_usdc if trade_entry_usdc > 0 else float(row["entry_usdc"] or 0.0)
            exit_usdc = resolved_trade_exit_usdc
            entry_size = trade_entry_size if trade_entry_size > 0 else float(row["entry_size"] or 0.0)
            exit_size = trade_exit_size
            unresolved = trade_buy_count > 0 and resolved_trade_exit_count <= 0

        pnl = None if unresolved else round(float(exit_usdc) - float(entry_usdc), 4)
        entry_price = None if entry_size <= 0 else round(float(entry_usdc) / float(entry_size), 4)
        exit_price = None if unresolved or exit_size <= 0 else round(float(exit_usdc) / float(exit_size), 4)
        if unresolved:
            result = "未定"
        elif pnl > 0:
            result = "盈利"
        elif pnl < 0:
            result = "亏损"
        else:
            result = "持平"

        history_rows.append({
            "window_slug": row["market_slug"],
            "utc_time": _format_utc_time(str(row["first_event_time"] or "")),
            "result": result,
            "entry_price": entry_price,
            "entry_size": round(entry_size, 4),
            "entry_usdc": round(entry_usdc, 4),
            "exit_price": exit_price,
            "exit_usdc": None if unresolved else round(exit_usdc, 4),
            "pnl": pnl,  # unresolved 不计利润
        })
    return history_rows


def _parse_utc8_datetime(raw: str) -> datetime:
    dt = datetime.fromisoformat(str(raw or "").strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC8_TIMEZONE)
    else:
        dt = dt.astimezone(UTC8_TIMEZONE)
    return dt


def _count_5m_windows(start_dt_utc8: datetime, end_dt_utc8: datetime) -> int:
    span_sec = (end_dt_utc8 - start_dt_utc8).total_seconds()
    if span_sec < 0:
        return 0
    return int(span_sec // 300) + 1


def _categorize_skip_reason(reason: str) -> str:
    text = str(reason or "").strip()
    lower = text.lower()
    if not text:
        return "其他"
    if "toxic time regime" in lower:
        return "有毒时段"
    if "crossed open price" in lower or "方向不稳定" in text:
        return "方向不稳定"
    if "spread too narrow" in lower:
        return "盘口价差过窄"
    if "预判价差不足" in text:
        return "预判价差不足"
    if "risk_diff_boost" in lower:
        return "风险阈值提升拦截"
    if "窗口波动过大" in text or "avg |δbtc|/s" in lower:
        return "窗口波动过大"
    if "仓位削减为0" in text:
        return "风险降仓到0"
    if "db交叉验证未通过" in text:
        return "DB交叉验证失败"
    if "best_ask=" in lower and "max_entry_price" in lower:
        return "入场价格超阈值"
    if "best_ask 缺失" in text:
        return "盘口缺失"
    if "订单簿缓存不完整" in text:
        return "订单簿缓存不完整"
    if "market cache 缺失" in text:
        return "市场缓存缺失"
    if re.search(r"skip entry", lower):
        return "其他策略拦截"
    return "其他"


@app.route('/api/5m_trade_summary')
def api_5m_trade_summary():
    try:
        balance_str = get_balance_allowance(profile=TRADE_PM_PROFILE)
    except Exception as e:
        return jsonify({"error": f"获取trade余额失败: {e}"}), 500

    db_path = os.getenv("SQLITE_DB_PATH", "logs/trade.sqlite3")
    if not os.path.isabs(db_path):
        db_path = str((_project_root / db_path).resolve())

    log_series = []
    history_rows = []
    skipped_windows = []
    strategy_params = {}
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            log_series = _load_trade_balance_series(conn=conn, limit=240)
            skipped_windows = _load_skipped_windows(conn=conn, limit=80)
            strategy_params = _load_latest_trade_strategy_params(conn=conn)
            query = """
                SELECT
                    market_slug,
                    MIN(event_time) AS first_event_time,
                    SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) AS buy_event_count,
                    SUM(CASE WHEN side='buy' THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS entry_usdc,
                    SUM(CASE WHEN side='buy' THEN COALESCE(trade_size, 0) ELSE 0 END) AS entry_size,
                    SUM(CASE WHEN side='buy' THEN COALESCE(notional_usdc, 0) ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN side='buy' THEN COALESCE(trade_size, 0) ELSE 0 END), 0) AS entry_price,
                    SUM(CASE WHEN side IN ('sell','redeem') THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS exit_usdc,
                    SUM(CASE WHEN side IN ('sell','redeem') THEN COALESCE(trade_size, 0) ELSE 0 END) AS exit_size,
                    SUM(CASE WHEN side IN ('sell','redeem') THEN COALESCE(notional_usdc, 0) ELSE 0 END) /
                        NULLIF(SUM(CASE WHEN side IN ('sell','redeem') THEN COALESCE(trade_size, 0) ELSE 0 END), 0) AS exit_price,
                    SUM(CASE WHEN side IN ('sell','redeem') THEN 1 ELSE 0 END) AS exit_event_count,
                    SUM(CASE WHEN side='buy' AND reason='analyze_backfill' THEN 1 ELSE 0 END) AS analyze_buy_count,
                    SUM(CASE WHEN side IN ('sell','redeem') AND reason='analyze_backfill' THEN 1 ELSE 0 END) AS analyze_exit_count,
                    SUM(CASE WHEN side='buy' AND reason='analyze_backfill' THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS analyze_entry_usdc,
                    SUM(CASE WHEN side IN ('sell','redeem') AND reason='analyze_backfill' THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS analyze_exit_usdc,
                    SUM(CASE WHEN side='buy' AND reason='analyze_backfill' THEN COALESCE(trade_size, 0) ELSE 0 END) AS analyze_entry_size,
                    SUM(CASE WHEN side IN ('sell','redeem') AND reason='analyze_backfill' THEN COALESCE(trade_size, 0) ELSE 0 END) AS analyze_exit_size,
                    SUM(CASE WHEN side='buy' AND (reason IS NULL OR reason!='analyze_backfill') THEN 1 ELSE 0 END) AS trade_buy_count,
                    SUM(CASE WHEN side IN ('sell','redeem') AND (reason IS NULL OR reason!='analyze_backfill') THEN 1 ELSE 0 END) AS trade_exit_count,
                    SUM(CASE WHEN side='buy' AND (reason IS NULL OR reason!='analyze_backfill') THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS trade_entry_usdc,
                    SUM(CASE WHEN side IN ('sell','redeem') AND (reason IS NULL OR reason!='analyze_backfill') THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS trade_exit_usdc,
                    SUM(CASE WHEN side='buy' AND (reason IS NULL OR reason!='analyze_backfill') THEN COALESCE(trade_size, 0) ELSE 0 END) AS trade_entry_size,
                    SUM(CASE WHEN side IN ('sell','redeem') AND (reason IS NULL OR reason!='analyze_backfill') THEN COALESCE(trade_size, 0) ELSE 0 END) AS trade_exit_size
                FROM trade_events
                WHERE mode='live'
                  AND market_slug LIKE 'btc-updown-5m-%'
                  AND side IN ('buy', 'sell', 'redeem')
                GROUP BY market_slug
                HAVING buy_event_count > 0
                ORDER BY first_event_time DESC
                LIMIT 240
            """
            rows = conn.execute(query).fetchall()
            conn.close()
            history_rows = _build_trade_history_rows(rows)
        except Exception as e:
            return jsonify({"error": f"读取trade_events失败: {e}"}), 500

    return jsonify({
        "current_balance": balance_str,
        "balance_series": log_series,
        "history": history_rows,
        "skipped_windows": skipped_windows,
        "strategy_params": strategy_params,
    })


@app.route('/api/5m_trade_stats', methods=['POST'])
def api_5m_trade_stats():
    payload = request.get_json(silent=True) or {}
    start_raw = str(payload.get("start_time") or "").strip()
    end_raw = str(payload.get("end_time") or "").strip()
    stat_type = str(payload.get("stat_type") or "").strip().lower()
    if stat_type not in {"history", "skip"}:
        return jsonify({"error": "stat_type 必须是 history 或 skip"}), 400
    if not start_raw or not end_raw:
        return jsonify({"error": "请提供 start_time 和 end_time"}), 400

    try:
        start_dt_utc8 = _parse_utc8_datetime(start_raw)
        end_dt_utc8 = _parse_utc8_datetime(end_raw)
    except Exception:
        return jsonify({"error": "时间格式错误，请使用 YYYY-MM-DDTHH:MM 或 YYYY-MM-DDTHH:MM:SS"}), 400

    if end_dt_utc8 < start_dt_utc8:
        return jsonify({"error": "结束时间不能早于开始时间"}), 400

    total_windows = _count_5m_windows(start_dt_utc8, end_dt_utc8)
    start_utc_iso = start_dt_utc8.astimezone(timezone.utc).isoformat()
    end_utc_iso = end_dt_utc8.astimezone(timezone.utc).isoformat()

    db_path = os.getenv("SQLITE_DB_PATH", "logs/trade.sqlite3")
    if not os.path.isabs(db_path):
        db_path = str((_project_root / db_path).resolve())
    if not os.path.exists(db_path):
        return jsonify({"error": f"数据库不存在: {db_path}"}), 500

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if stat_type == "history":
            rows = conn.execute(
                """
                SELECT
                    market_slug,
                    MIN(event_time) AS first_event_time,
                    SUM(CASE WHEN side='buy' THEN 1 ELSE 0 END) AS buy_event_count,
                    SUM(CASE WHEN side='buy' THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS entry_usdc,
                    SUM(CASE WHEN side='buy' THEN COALESCE(trade_size, 0) ELSE 0 END) AS entry_size,
                    SUM(CASE WHEN side IN ('sell','redeem') THEN 1 ELSE 0 END) AS exit_event_count,
                    SUM(CASE WHEN side='buy' AND reason='analyze_backfill' THEN 1 ELSE 0 END) AS analyze_buy_count,
                    SUM(CASE WHEN side IN ('sell','redeem') AND reason='analyze_backfill' THEN 1 ELSE 0 END) AS analyze_exit_count,
                    SUM(CASE WHEN side='buy' AND reason='analyze_backfill' THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS analyze_entry_usdc,
                    SUM(CASE WHEN side IN ('sell','redeem') AND reason='analyze_backfill' THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS analyze_exit_usdc,
                    SUM(CASE WHEN side='buy' AND reason='analyze_backfill' THEN COALESCE(trade_size, 0) ELSE 0 END) AS analyze_entry_size,
                    SUM(CASE WHEN side IN ('sell','redeem') AND reason='analyze_backfill' THEN COALESCE(trade_size, 0) ELSE 0 END) AS analyze_exit_size,
                    SUM(CASE WHEN side='buy' AND (reason IS NULL OR reason!='analyze_backfill') THEN 1 ELSE 0 END) AS trade_buy_count,
                    SUM(CASE WHEN side IN ('sell','redeem') AND (reason IS NULL OR reason!='analyze_backfill') THEN 1 ELSE 0 END) AS trade_exit_count,
                    SUM(CASE WHEN side='buy' AND (reason IS NULL OR reason!='analyze_backfill') THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS trade_entry_usdc,
                    SUM(CASE WHEN side IN ('sell','redeem') AND (reason IS NULL OR reason!='analyze_backfill') THEN COALESCE(notional_usdc, 0) ELSE 0 END) AS trade_exit_usdc,
                    SUM(CASE WHEN side='buy' AND (reason IS NULL OR reason!='analyze_backfill') THEN COALESCE(trade_size, 0) ELSE 0 END) AS trade_entry_size,
                    SUM(CASE WHEN side IN ('sell','redeem') AND (reason IS NULL OR reason!='analyze_backfill') THEN COALESCE(trade_size, 0) ELSE 0 END) AS trade_exit_size
                FROM trade_events
                WHERE mode='live'
                  AND market_slug LIKE 'btc-updown-5m-%'
                  AND side IN ('buy', 'sell', 'redeem')
                GROUP BY market_slug
                HAVING buy_event_count > 0
                   AND first_event_time >= ?
                   AND first_event_time <= ?
                ORDER BY first_event_time DESC
                """,
                (start_utc_iso, end_utc_iso),
            ).fetchall()

            history_rows = _build_trade_history_rows(rows)
            trade_window_count = len(history_rows)
            settled = [r for r in history_rows if r.get("pnl") is not None]
            profit_rows = [r for r in settled if float(r["pnl"]) > 0]
            loss_rows = [r for r in settled if float(r["pnl"]) < 0]
            profit_amount = round(sum(float(r["pnl"]) for r in profit_rows), 4)
            loss_amount = round(sum(float(r["pnl"]) for r in loss_rows), 4)
            net_pnl = round(sum(float(r["pnl"]) for r in settled), 4)
            decided_count = len(profit_rows) + len(loss_rows)
            win_rate = (len(profit_rows) / decided_count) if decided_count > 0 else None

            return jsonify({
                "stat_type": "history",
                "total_windows": total_windows,
                "trade_window_count": trade_window_count,
                "trade_window_ratio": (trade_window_count / total_windows) if total_windows > 0 else None,
                "net_pnl": net_pnl,
                "profit_window_count": len(profit_rows),
                "loss_window_count": len(loss_rows),
                "win_rate": win_rate,
                "profit_amount": profit_amount,
                "loss_amount": loss_amount,
                "avg_profit": (round(profit_amount / len(profit_rows), 4) if profit_rows else None),
                "avg_loss": (round(loss_amount / len(loss_rows), 4) if loss_rows else None),
            })

        skip_rows = conn.execute(
            """
            SELECT reason, COUNT(*) AS cnt
            FROM trade_events
            WHERE mode='live'
              AND side='skip'
              AND market_slug LIKE 'btc-updown-5m-%'
              AND event_time >= ?
              AND event_time <= ?
            GROUP BY reason
            ORDER BY cnt DESC, reason ASC
            """,
            (start_utc_iso, end_utc_iso),
        ).fetchall()
        skip_count = int(sum(int(r["cnt"] or 0) for r in skip_rows))
        category_count: dict[str, int] = {}
        category_examples: dict[str, list[str]] = {}
        for row in skip_rows:
            cnt = int(row["cnt"] or 0)
            reason = str(row["reason"] or "--")
            category = _categorize_skip_reason(reason)
            category_count[category] = category_count.get(category, 0) + cnt
            if category not in category_examples:
                category_examples[category] = []
            if reason not in category_examples[category] and len(category_examples[category]) < 3:
                category_examples[category].append(reason)

        reason_stats = []
        for category, cnt in sorted(category_count.items(), key=lambda x: (-x[1], x[0])):
            reason_stats.append({
                "reason": category,
                "count": cnt,
                "ratio": (cnt / skip_count) if skip_count > 0 else None,
                "examples": category_examples.get(category, []),
            })
        return jsonify({
            "stat_type": "skip",
            "total_windows": total_windows,
            "skip_window_count": skip_count,
            "skip_window_ratio": (skip_count / total_windows) if total_windows > 0 else None,
            "reasons": reason_stats,
        })
    except Exception as e:
        return jsonify({"error": f"统计失败: {e}"}), 500
    finally:
        conn.close()


@app.route('/api/balance_summary')
def api_balance_summary():
    """Return cash balance, total position value, and profile value (cash + positions)."""
    try:
        balance_str = get_balance_allowance(profile=APP_PM_PROFILE)
        cash = _parse_cash_balance(balance_str)
        positions = get_positions(profile=APP_PM_PROFILE)
        position_value = 0.0
        for p in positions:
            cv = p.get("currentValue")
            if cv is not None:
                try:
                    position_value += float(cv)
                except (TypeError, ValueError):
                    pass
            else:
                try:
                    size = float(p.get("size") or 0)
                    cur = float(p.get("curPrice") or 0)
                    position_value += size * cur
                except (TypeError, ValueError):
                    pass
        profile_value = cash + position_value
        return jsonify({
            "cash_balance": balance_str,
            "position_value": round(position_value, 2),
            "profile_value": round(profile_value, 2),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/run_position_analyze', methods=['POST'])
def api_run_position_analyze():
    """Run position_analyze.py in the background."""
    try:
        sub_env = os.environ.copy()
        sub_env["POLYMARKET_PROFILE"] = "analyze"
        subprocess.Popen(
            [sys.executable, "position_analyze.py"],
            cwd=_project_root,
            env=sub_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return jsonify({"status": "started"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _latest_report_path():
    """Return path to the most recently modified *_email.html in output/, or None."""
    output_dir = _project_root / "output"
    if not output_dir.is_dir():
        return None
    candidates = list(output_dir.glob("*_email.html"))
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@app.route('/report/latest')
def report_latest():
    """Serve the latest position analysis report HTML."""
    path = _latest_report_path()
    if path is None:
        return "No report found.", 404
    try:
        html = path.read_text(encoding="utf-8")
        return Response(html, mimetype="text/html; charset=utf-8")
    except OSError as e:
        return str(e), 500


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.ERROR,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(_project_root / "logs" / "app.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    bind_host = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    bind_port = int(os.getenv("DASHBOARD_PORT", "5000"))
    app.run(host=bind_host, port=bind_port, debug=False)