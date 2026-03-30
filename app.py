import hmac
import logging
import os
import secrets
import sqlite3
import subprocess
import sys
from datetime import datetime
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
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            log_series = _load_trade_balance_series(conn=conn, limit=240)
            skipped_windows = _load_skipped_windows(conn=conn, limit=80)
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

            trade_exit_by_slug, trade_exit_count_by_slug = _load_activity_exit_by_slug(TRADE_PM_PROFILE)
            analyze_exit_by_slug, analyze_exit_count_by_slug = _load_activity_exit_by_slug("analyze")

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
                    "et_time": _format_et_time(str(row["first_event_time"] or "")),
                    "result": result,
                    "entry_price": entry_price,
                    "entry_size": round(entry_size, 4),
                    "entry_usdc": round(entry_usdc, 4),
                    "exit_price": exit_price,
                    "exit_usdc": None if unresolved else round(exit_usdc, 4),
                    "pnl": pnl,  # unresolved 不计利润
                })
        except Exception as e:
            return jsonify({"error": f"读取trade_events失败: {e}"}), 500

    return jsonify({
        "current_balance": balance_str,
        "balance_series": log_series,
        "history": history_rows,
        "skipped_windows": skipped_windows,
    })


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