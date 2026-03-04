import hmac
import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path

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
    client,
    get_event_situation,
    get_open_orders,
    get_positions,
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
        positions = get_positions()
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
        orders = get_open_orders()

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
                    market = client.get_market(market_id)
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
        order_id = buy_order(market_id, token_id, float(price), float(size))
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
        order_id = sell_order(market_id, token_id, float(price), float(size), order_type= OrderType.GTC)
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
        result = cancel_order(order_id)
        logger.info("api_cancel success: order_id=%s result=%s", order_id, result)
        return jsonify({'result': result})
    except Exception as e:
        logger.exception("api_cancel failed: order_id=%s error=%s", order_id, e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/balance')
def api_balance():
    try:
        balance = get_balance_allowance()
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


@app.route('/api/balance_summary')
def api_balance_summary():
    """Return cash balance, total position value, and profile value (cash + positions)."""
    try:
        balance_str = get_balance_allowance()
        cash = _parse_cash_balance(balance_str)
        positions = get_positions()
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
        subprocess.Popen(
            [sys.executable, "position_analyze.py"],
            cwd=_project_root,
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