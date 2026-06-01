import hmac
import json
import logging
import math
import os
import re
import secrets
import subprocess
import sys
import time
import calendar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, List
from zoneinfo import ZoneInfo

import psycopg2.extras
from data.database import get_conn, get_cursor
from services.recommendation_db import RecommendationDB, RecommendationGateError
from services.profit_optimizer import get_or_set_monthly_baseline
from services.profit_optimizer import _extract_strike_and_direction, _parse_market_prices
from services.monthly_goal_attribution import (
    classify_entry_tier,
    get_monthly_goal_target_pct,
    get_monthly_goal_realized_summary,
    get_monthly_trade_review_summary,
    save_monthly_goal_target_pct,
)
from services.entry_review import (
    complete_entry_review_task,
    list_entry_review_tasks,
)

from flask import Flask, Response, render_template, request, jsonify, session, redirect, url_for
from py_clob_client_v2.clob_types import (
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
    buy_order,
    sell_order,
    cancel_order,
    get_order_detail,
    get_balance_allowance,
    get_event_token_id,
    get_best_prices,
    get_last_order_error,
)

app = Flask(__name__)
app.secret_key = os.getenv("DASHBOARD_SECRET_KEY") or secrets.token_hex(32)

# Advisory blueprint (fair-value rebalancer recommendations + manual trade log).
# 受 before_request 全局认证保护; 失败导入不应阻断 dashboard 主功能.
try:
    from services.advisory.dashboard import advisory_bp
    app.register_blueprint(advisory_bp)
    from services.advisory.intent_writer import (
        record_place_intent as _advisory_record_place,
        record_cancel_intent as _advisory_record_cancel,
    )
except Exception as _adv_exc:  # pragma: no cover - defensive
    logging.getLogger(__name__).warning(
        "advisory blueprint not registered: %s", _adv_exc
    )
    def _advisory_record_place(**_kwargs):  # type: ignore[no-redef]
        return None
    def _advisory_record_cancel(**_kwargs):  # type: ignore[no-redef]
        return None

# 第五轮加固 #1：默认不信任 X-Forwarded-For（任何外部用户都能伪造该 header 污染 audit 字段）。
# 部署在可信反向代理后时，运维需显式设置 DASHBOARD_TRUST_PROXY=1。
_DASHBOARD_TRUST_PROXY = (os.getenv("DASHBOARD_TRUST_PROXY", "0").strip() == "1")
_AUDIT_FIELD_SAFE_RE = re.compile(r"[^\w\.\-:@]")


def _sanitize_audit_field(value: object, max_len: int = 64) -> str:
    """把任意来源（环境变量、HTTP header、hostname）规范化成短的、可入审计/cookie 的字符串。

    - 删掉所有控制字符与可能撑大 cookie / 注入 SQL 上下文的字符
    - 截断到 max_len，防止 cookie session 因为超长 forwarded 链路膨胀
    """
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    cleaned = _AUDIT_FIELD_SAFE_RE.sub("", text)
    return cleaned[:max_len]

app.config["SESSION_COOKIE_NAME"] = "pm_dashboard_session"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("DASHBOARD_HTTPS_ONLY", "false").lower() == "true"
APP_PM_PROFILE = (os.getenv("POLYMARKET_PROFILE", "analyze") or "analyze").strip().lower()
ET_TIMEZONE = ZoneInfo("America/New_York")
UTC8_TIMEZONE = ZoneInfo("Asia/Shanghai")
_recommendation_db = RecommendationDB()
try:
    # 保证空库 / 新部署场景下 /api/recommendations/* 等接口不会因表不存在直接 500。
    _recommendation_db.init_tables()
except Exception:
    logger.exception("recommendation 数据表初始化失败（启动继续，但相关接口可能不可用）")
RECOMMENDATION_SINGLE_MARKET_CAP_RATIO = 0.20
RECOMMENDATION_CORRELATION_CAP_RATIO = 0.40
RECOMMENDATION_DEFAULT_MAX_AGE_HOURS = 12.0
RECOMMENDATION_EVENT_MAX_AGE_HOURS = 4.0
RECOMMENDATION_PRICE_WARN_TOLERANCE_CENTS = 3.0


def _normalize_utc_iso(raw: str, default: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return default
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat()
    except Exception:
        return default


def _parse_dashboard_datetime(raw: object) -> datetime:
    """解析 Dashboard 人工输入时间；naive 默认按北京时间。"""
    dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC8_TIMEZONE)
    return dt



def _is_authenticated() -> bool:
    return session.get("dashboard_authed") is True


def _is_api_request() -> bool:
    return request.path.startswith("/api/")


def _login_redirect():
    return redirect(url_for("login", next=request.path))


def _make_zone_badge(label: str, tone: str, tooltip: str) -> dict:
    return {"label": label, "tone": tone, "tooltip": tooltip}


def _classify_time_phase(days_left_in_month: float) -> tuple[str, str]:
    if days_left_in_month <= 7.0:
        return "month_end", "月末"
    if days_left_in_month <= 16.0:
        return "month_mid", "月中"
    return "month_start", "月初"


def _market_zone_badges(
    yes_price: float | None,
    no_price: float | None,
    *,
    distance_pct: float | None,
    days_left_in_month: float,
) -> list[dict]:
    badges: list[dict] = []
    d = distance_pct or 0.0
    phase_key, phase_label = _classify_time_phase(days_left_in_month)

    if phase_key == "month_start":
        comfy_high_no = no_price is not None and no_price >= 0.90 and d >= 12.0
        high_no = no_price is not None and no_price >= 0.82 and d >= 10.0
        mid_no = no_price is not None and 0.65 <= no_price < 0.82 and 5.0 <= d <= 12.0
        low_yes = yes_price is not None and 0.10 <= yes_price <= 0.25 and 5.0 <= d <= 10.0
    elif phase_key == "month_mid":
        comfy_high_no = no_price is not None and no_price >= 0.88 and d >= 10.0
        high_no = no_price is not None and no_price >= 0.82 and d >= 8.0
        mid_no = no_price is not None and 0.62 <= no_price < 0.82 and 4.0 <= d <= 10.0
        low_yes = yes_price is not None and 0.10 <= yes_price <= 0.30 and 3.0 <= d <= 8.0
    else:
        comfy_high_no = no_price is not None and no_price >= 0.85 and d >= 8.0
        high_no = no_price is not None and no_price >= 0.75 and d >= 6.0
        mid_no = no_price is not None and 0.58 <= no_price < 0.78 and 3.0 <= d <= 8.0
        low_yes = yes_price is not None and 0.08 <= yes_price <= 0.22 and 2.0 <= d <= 5.0

    if phase_key == "month_mid":
        mid_no_tip = (
            f"{phase_label}谨慎收益区：结合你的历史，月中 No 不是自动主战区。"
            "只有在第一次试探 barrier 失败后的确认位，或出现明确错配时才参与。"
        )
        low_yes_tip = (
            f"{phase_label}弹性机会区：结合你的历史，月中 Yes 的弹性更值得关注。"
            "但仍只在突破/催化确认后小仓位参与，不能无信号抄底。"
        )
    elif phase_key == "month_end":
        mid_no_tip = (
            f"{phase_label}近端收益区：月末盈利更偏向 No，"
            "可接受更近的中价 No，但仍应优先做更确定的 No 收割。"
        )
        low_yes_tip = (
            f"{phase_label}克制进攻区：只有非常近、非常明确的催化/错配才参与 Yes，"
            "默认应把注意力放回 No。"
        )
    else:
        mid_no_tip = (
            f"{phase_label}收益候选区：月初不要预设没 edge。"
            "中价 No 可做，但依然优先等第一次试探 barrier 失败后再进。"
        )
        low_yes_tip = (
            f"{phase_label}进攻弹性仓：月初允许更积极寻找低价 Yes，"
            "但仍需突破/催化确认，不能因为便宜就直接买。"
        )

    if comfy_high_no:
        badges.append(
            _make_zone_badge(
                "舒服高价No",
                "danger",
                f"{phase_label}舒服防守仓：当前按剩余 {days_left_in_month:.1f} 天判断，需同时满足更舒服的 No 价格与更远的 barrier 距离。适合稳定收 Theta、做组合底盘，但仍不是主利润引擎。",
            )
        )
    elif high_no:
        badges.append(
            _make_zone_badge(
                "高价No",
                "danger",
                f"{phase_label}防守仓：已进入高价No区，但舒适度弱于更远 barrier 的舒服高价No。适合偏防守生息，避免把它当主收益仓追大仓。",
            )
        )
    elif mid_no:
        badges.append(
            _make_zone_badge(
                "中价No",
                "warning",
                mid_no_tip,
            )
        )
    else:
        badges.append(
            _make_zone_badge(
                "No观察",
                "secondary",
                f"{phase_label}当前 No 不在舒服高价No / 高价No / 中价No主战区，优先观察，避免硬做或追不舒服的价格结构。",
            )
        )

    if low_yes:
        badges.append(
            _make_zone_badge(
                "低价Yes",
                "success",
                low_yes_tip,
            )
        )
    else:
        badges.append(
            _make_zone_badge(
                "Yes观察",
                "secondary",
                f"{phase_label}当前 Yes 不属于低价Yes进攻区；若没有趋势突破或明确催化，保持观察，不主动抄底。",
            )
        )

    return badges


def _build_market_badges(market: dict, current_btc_price: float, days_left_in_month: float) -> dict:
    strike, direction = _extract_strike_and_direction(market.get("question") or "")
    yes_price, no_price = _parse_market_prices(market)
    phase_key, phase_label = _classify_time_phase(days_left_in_month)
    if current_btc_price <= 0 or strike is None or direction == "unknown":
        return {
            "distance_pct": None,
            "distance_label": "距 barrier —",
            "phase_label": phase_label,
            "zone_badges": [
                _make_zone_badge("No观察", "secondary", "当前无法可靠识别 No 分类，先观察。"),
                _make_zone_badge("Yes观察", "secondary", "当前无法可靠识别 Yes 分类，先观察。"),
            ],
        }

    distance_pct = round(abs(strike - current_btc_price) / current_btc_price * 100.0, 1)
    action_word = "上破" if direction == "above" else "下破"
    return {
        "distance_pct": distance_pct,
        "distance_label": f"距{action_word} barrier {distance_pct:.1f}%",
        "phase_label": phase_label,
        "zone_badges": _market_zone_badges(
            yes_price,
            no_price,
            distance_pct=distance_pct,
            days_left_in_month=days_left_in_month,
        ),
    }


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
            # 第四轮加固 #4 + 第五轮加固 #1：写入稳定操作者标识。
            # X-Forwarded-For 默认不信任（DASHBOARD_TRUST_PROXY=1 才取），全部字段做白名单清洗 + 截断，
            # 避免攻击者通过伪造 header 污染 outcome/release 的 recorded_by/released_by 字段，
            # 也避免超长字符串撑大 Flask 的客户端 session cookie。
            try:
                import socket as _socket
                operator_name = _sanitize_audit_field(
                    os.getenv("DASHBOARD_OPERATOR") or _socket.gethostname() or "dashboard"
                ) or "dashboard"
            except Exception:  # noqa: BLE001
                operator_name = "dashboard"
            if _DASHBOARD_TRUST_PROXY:
                forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0]
                remote_ip = _sanitize_audit_field(forwarded) or _sanitize_audit_field(request.remote_addr) or "?"
            else:
                remote_ip = _sanitize_audit_field(request.remote_addr) or "?"
            session_token = secrets.token_hex(4)
            user_label = f"{operator_name}@{remote_ip}#{session_token}"
            session['user'] = user_label[:128]
            session['operator_name'] = operator_name
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
        current_btc_price = float(get_btc_price())
        now_et = datetime.now(ET_TIMEZONE)
        days_in_month = calendar.monthrange(now_et.year, now_et.month)[1]
        days_left_in_month = max(0, days_in_month - now_et.day) + (24 - now_et.hour) / 24.0
        phase_label = _classify_time_phase(days_left_in_month)[1]
        token_ids: List[str] = []
        for market in data.get("markets") or []:
            for tid in (market.get("token_id") or []):
                if tid:
                    token_ids.append(str(tid))
        # 批量取 best bid/ask；失败用 None 占位，前端会显示 —
        price_map = get_best_prices(token_ids, profile=APP_PM_PROFILE) if token_ids else {}
        for market in data.get("markets") or []:
            best_bids = []
            best_asks = []
            for tid in (market.get("token_id") or []):
                entry = price_map.get(str(tid)) or {}
                best_bids.append(entry.get("best_bid"))
                best_asks.append(entry.get("best_ask"))
            market["bestBids"] = best_bids
            market["bestAsks"] = best_asks
            market["barrierMeta"] = _build_market_badges(
                market=market,
                current_btc_price=current_btc_price,
                days_left_in_month=days_left_in_month,
            )
        data["current_btc_price"] = current_btc_price
        data["phase_key"] = phase_key
        data["phase_label"] = phase_label
        return jsonify(data)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _current_et_month_bounds() -> tuple[datetime, datetime, str]:
    now_et = datetime.now(ET_TIMEZONE)
    start_et = now_et.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start_et.month == 12:
        end_et = start_et.replace(year=start_et.year + 1, month=1)
    else:
        end_et = start_et.replace(month=start_et.month + 1)
    return start_et, end_et, start_et.strftime("%Y-%m")


def _build_current_intent_tier_map() -> dict[str, dict]:
    """按当前行情给 token 生成 intent_tier 快照；失败时返回空 map。"""
    try:
        data = get_event_token_id()
        btc_price = float(get_btc_price())
    except Exception as exc:  # noqa: BLE001
        logger.warning("build current intent tier map failed: %s", exc)
        return {}

    now_utc = datetime.now(timezone.utc)
    out: dict[str, dict] = {}
    for market in data.get("markets") or []:
        question = str(market.get("question") or "")
        outcomes = market.get("outcomes") or []
        prices = market.get("outcomePrices") or []
        token_ids = market.get("token_id") or []
        for idx, token_id in enumerate(token_ids):
            if not token_id or idx >= len(outcomes):
                continue
            try:
                price = float(prices[idx]) if idx < len(prices) else None
            except (TypeError, ValueError):
                price = None
            if price is None or not (0 < price < 1):
                continue
            snapshot = classify_entry_tier(
                question=question,
                outcome=str(outcomes[idx] or ""),
                fill_price=price,
                btc_price=btc_price,
                as_of_utc=now_utc,
            )
            snapshot["source"] = "current_event_at_order_creation"
            out[str(token_id)] = snapshot
    return out


def _apply_intent_tier_snapshot(extra: dict, token_id: object, tier_map: dict[str, dict] | None) -> dict:
    snapshot = (tier_map or {}).get(str(token_id or ""))
    if not snapshot:
        return extra
    extra["intent_tier_snapshot"] = snapshot
    if snapshot.get("tier_key"):
        extra["intent_tier_key"] = snapshot.get("tier_key")
        extra["intent_tier_label"] = snapshot.get("tier_label")
    return extra


def _apply_post_entry_review_extra(extra: dict, source: dict) -> dict:
    """把显式配置的入场后复查窗口带入 pending extra。"""
    raw_hours = source.get("post_entry_review_hours") if isinstance(source, dict) else None
    if not isinstance(raw_hours, list):
        return extra
    hours: list[float] = []
    for value in raw_hours[:6]:
        try:
            hour = float(value)
        except (TypeError, ValueError):
            continue
        if 0 < hour <= 720 and hour not in hours:
            hours.append(hour)
    if not hours:
        return extra
    extra["post_entry_review_hours"] = hours
    note = str(source.get("post_entry_review_note") or "").strip()
    if note:
        extra["post_entry_review_note"] = note[:500]
    return extra


@app.route('/api/monthly_goal/realized')
def api_monthly_goal_realized():
    """按 ET 本月 sell 成交估算已实现盈亏，并按买入 lot 的 entry_tier 归因。

    数据源使用 advisory_chain_fills（由 Polymarket activity poller 增量写入）。
    新成交买入会锁定 entry_tier；历史缺失 entry_tier 的 lot 归入未归类。
    """
    try:
        profile = request.args.get("profile", "analyze")
        return jsonify(get_monthly_goal_realized_summary(profile=profile))
    except Exception as e:
        logger.exception("api_monthly_goal_realized error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/monthly_goal/review')
def api_monthly_goal_review():
    """按买入时快照复盘本月已实现收益来源与应加权/降权组合。"""
    try:
        profile = request.args.get("profile", "analyze")
        return jsonify(get_monthly_trade_review_summary(profile=profile))
    except Exception as e:
        logger.exception("api_monthly_goal_review error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/entry_reviews')
def api_entry_reviews():
    """买入成交后的复查任务列表。"""
    try:
        profile = request.args.get("profile", APP_PM_PROFILE)
        status = request.args.get("status") or None
        return jsonify({"tasks": list_entry_review_tasks(profile=profile, status=status)})
    except Exception as e:
        logger.exception("api_entry_reviews error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/entry_reviews/<int:task_id>/complete', methods=['POST'])
def api_entry_review_complete(task_id: int):
    """人工标记复查任务完成或忽略。"""
    data = request.get_json(silent=True) or {}
    try:
        row = complete_entry_review_task(
            task_id,
            status=str(data.get("status") or "done"),
            result_payload=data.get("result_payload") if isinstance(data.get("result_payload"), dict) else data,
        )
        if not row:
            return jsonify({"error": "复查任务不存在或已不是 pending 状态"}), 404
        return jsonify({"ok": True, "task": row})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        logger.exception("api_entry_review_complete error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/monthly_goal/settings', methods=['GET', 'POST'])
def api_monthly_goal_settings():
    """Dashboard 本月目标设置；AI 分析脚本从同一处读取。"""
    try:
        if request.method == "GET":
            profile = request.args.get("profile", "analyze")
            return jsonify(get_monthly_goal_target_pct(profile=profile))

        payload = request.get_json(silent=True) or {}
        target_pct = float(payload.get("target_pct"))
        if target_pct <= 0:
            return jsonify({"error": "target_pct must be positive"}), 400
        profile = str(payload.get("profile") or "analyze").strip() or "analyze"
        return jsonify(save_monthly_goal_target_pct(
            target_pct=target_pct,
            realized_overrides=payload.get("realized_overrides") or {},
            target_position_overrides=payload.get("target_position_overrides") or {},
            profile=profile,
            source="dashboard",
        ))
    except (TypeError, ValueError):
        return jsonify({"error": "invalid target_pct"}), 400
    except Exception as e:
        logger.exception("api_monthly_goal_settings error")
        return jsonify({"error": str(e)}), 500


# ---------- 交易讨论室 (Chat with AI) ----------
# 复用 position_analyze 用的同一套 Gemini Pro + Search Grounding，
# 让用户可以拿"上一份完整 AI 报告 + 实时持仓/挂单/余额 + 当前 events 价格"作为上下文
# 跟 AI 自由提问。前端把多轮历史存 localStorage,服务端是无状态的。
_CHAT_MAX_HISTORY_TURNS = 20  # 每边最多带 20 条进 prompt,防 token 爆炸
_CHAT_MAX_USER_MESSAGE_LEN = 4000  # 单条用户消息上限,防止粘贴超长内容刷爆 API
_CHAT_DEFAULT_SESSION_ID = "default"  # 当前 dashboard 单用户,固定一个 session
_CHAT_HISTORY_LOAD_LIMIT = 200  # GET /api/chat/history 一次最多回多少条
_chat_table_ready = False
_chat_table_lock = __import__("threading").Lock()


def _ensure_chat_table() -> None:
    """首次访问时创建 chat_messages 表(幂等)。"""
    global _chat_table_ready
    if _chat_table_ready:
        return
    with _chat_table_lock:
        if _chat_table_ready:
            return
        with get_conn(autocommit=True) as conn, conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id          BIGSERIAL PRIMARY KEY,
                    session_id  TEXT NOT NULL DEFAULT 'default',
                    role        TEXT NOT NULL CHECK (role IN ('user','assistant')),
                    content     TEXT NOT NULL,
                    sources     JSONB,
                    latency_ms  INTEGER,
                    model       TEXT,
                    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_chat_messages_session_created
                    ON chat_messages (session_id, created_at);
                """
            )
        _chat_table_ready = True


def _save_chat_message(role: str, content: str, *, sources=None, latency_ms=None, model=None,
                       session_id: str = _CHAT_DEFAULT_SESSION_ID) -> None:
    _ensure_chat_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chat_messages (session_id, role, content, sources, latency_ms, model)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                session_id,
                role,
                content,
                psycopg2.extras.Json(sources) if sources else None,
                latency_ms,
                model,
            ),
        )


def _load_chat_history(session_id: str = _CHAT_DEFAULT_SESSION_ID, limit: int = _CHAT_HISTORY_LOAD_LIMIT) -> list:
    _ensure_chat_table()
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT id, role, content, sources, latency_ms, model,
                   EXTRACT(EPOCH FROM created_at) * 1000 AS ts
            FROM chat_messages
            WHERE session_id = %s
            ORDER BY id DESC
            LIMIT %s
            """,
            (session_id, limit),
        )
        rows = cur.fetchall()
    rows.reverse()
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "sources": r["sources"] or [],
            "latency_ms": r["latency_ms"],
            "model": r["model"],
            "ts": int(r["ts"]) if r["ts"] is not None else None,
        })
    return out


def _clear_chat_history(session_id: str = _CHAT_DEFAULT_SESSION_ID) -> int:
    _ensure_chat_table()
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM chat_messages WHERE session_id = %s", (session_id,))
        return cur.rowcount


def _load_chat_context_blob() -> dict:
    """收集供 AI 参考的实时上下文。失败的子项不阻塞,用 None 占位。"""
    blob: dict = {"generated_at": datetime.now(timezone.utc).isoformat()}

    # 上一份完整 AI 报告(JSON)
    try:
        report_path = Path(__file__).resolve().parent / "last_report.json"
        if report_path.exists():
            with report_path.open("r", encoding="utf-8") as f:
                blob["last_ai_report"] = json.load(f)
        else:
            blob["last_ai_report"] = None
    except Exception as exc:
        logger.warning("chat: load last_report.json failed: %s", exc)
        blob["last_ai_report"] = {"error": str(exc)}

    # 持仓
    try:
        positions = get_positions(profile=APP_PM_PROFILE) or []
        blob["positions"] = [
            {
                "title": p.get("title"),
                "outcome": p.get("outcome"),
                "size": p.get("size"),
                "avgPrice": p.get("avgPrice"),
                "curPrice": p.get("curPrice"),
                "currentValue": p.get("currentValue"),
                "percentPnl": p.get("percentPnl"),
                "endDate": p.get("endDate"),
                "conditionId": p.get("conditionId"),
            }
            for p in positions
        ]
    except Exception as exc:
        logger.warning("chat: get_positions failed: %s", exc)
        blob["positions"] = {"error": str(exc)}

    # 挂单
    try:
        orders = get_open_orders(profile=APP_PM_PROFILE) or []
        blob["open_orders"] = [
            {
                "market_id": o.get("market") or o.get("market_id") or o.get("condition_id"),
                "side": o.get("side"),
                "price": o.get("price"),
                "original_size": o.get("original_size"),
                "size_matched": o.get("size_matched"),
                "outcome": o.get("outcome"),
                "asset_id": o.get("asset_id"),
                "created_at": o.get("created_at"),
            }
            for o in orders
        ]
    except Exception as exc:
        logger.warning("chat: get_open_orders failed: %s", exc)
        blob["open_orders"] = {"error": str(exc)}

    # USDC 余额(gross)
    try:
        blob["usdc_balance"] = get_balance_allowance(profile=APP_PM_PROFILE)
    except Exception as exc:
        logger.warning("chat: get_balance_allowance failed: %s", exc)
        blob["usdc_balance"] = None

    # 本月 event 各 strike 的最新 best bid/ask + mid
    try:
        ev = get_event_token_id()
        token_ids = []
        for m in (ev.get("markets") or []):
            for tid in (m.get("token_id") or []):
                if tid:
                    token_ids.append(str(tid))
        prices = get_best_prices(token_ids, profile=APP_PM_PROFILE) if token_ids else {}
        markets_brief = []
        for m in (ev.get("markets") or []):
            outs = m.get("outcomes") or []
            mids = m.get("outcomePrices") or []
            tids = m.get("token_id") or []
            rows = []
            for i, oc in enumerate(outs):
                tid = str(tids[i]) if i < len(tids) else None
                px = prices.get(tid) if tid else None
                rows.append({
                    "outcome": oc,
                    "mid": mids[i] if i < len(mids) else None,
                    "best_bid": (px or {}).get("best_bid"),
                    "best_ask": (px or {}).get("best_ask"),
                })
            markets_brief.append({"question": m.get("question"), "outcomes": rows})
        blob["polymarket_event"] = {"event_name": ev.get("event_name"), "markets": markets_brief}
    except Exception as exc:
        logger.warning("chat: load polymarket event failed: %s", exc)
        blob["polymarket_event"] = {"error": str(exc)}

    # 当前 BTC 现价
    try:
        blob["btc_spot_price"] = get_btc_price()
    except Exception as exc:
        logger.warning("chat: get_btc_price failed: %s", exc)
        blob["btc_spot_price"] = None

    return blob


def _build_chat_system_instruction(context_blob: dict) -> str:
    """系统 prompt: 角色定义 + 实时上下文 JSON。"""
    context_json = json.dumps(context_blob, ensure_ascii=False, default=str, indent=2)
    return (
        "你是这个 Polymarket 月度 BTC 价格事件交易系统的内置策略助手。\n"
        "用户会基于自己的交易想法问你问题,你的任务是结合下面提供的实时上下文(持仓、挂单、"
        "USDC 余额、本月 event 各 strike 的 best bid/ask、上一份完整 AI 报告)与你掌握的市场/链上/"
        "宏观信息,给出**具体、可操作、考虑了已有仓位与资金限制**的建议。\n"
        "\n"
        "**回答风格要求:**\n"
        "- 中文为主,涉及代码/symbol/价格用英文/数字保持精确\n"
        "- 直接给观点,不要客套\n"
        "- 如果用户问的方向你不认同,**坦诚反驳并说理由**;不要无条件附和\n"
        "- 引用具体数字时直接从上下文里取,不要瞎猜;不确定就说不确定\n"
        "- 如果建议下单,给出**方向(Yes/No)、目标价区间(美分)、size(USDC 或 share 数)、止盈止损位**;"
        "  但**不要替用户执行**,告诉他去 events tab 自己挂单\n"
        "- 必要时使用 Google Search 查最新新闻/ETF 流向/美股盘前等市场状态\n"
        "\n"
        "**实时上下文 (system-injected, 用户不可见):**\n"
        "```json\n"
        f"{context_json}\n"
        "```\n"
    )


@app.route('/api/chat/history', methods=['GET'])
def api_chat_history():
    try:
        history = _load_chat_history()
        return jsonify({"messages": history})
    except Exception as e:
        logger.exception("api_chat_history error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/chat/history', methods=['DELETE'])
def api_chat_history_clear():
    try:
        n = _clear_chat_history()
        return jsonify({"deleted": n})
    except Exception as e:
        logger.exception("api_chat_history_clear error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/chat', methods=['POST'])
def api_chat():
    """对话接口。前端只需发"新一条 user 消息";服务端从 PG 读历史拼上下文,并把 user+assistant 写回 PG。

    兼容旧契约: 若 body 带 messages 数组,取最后一条 user;若带 message 字段,作为单条 user。
    """
    try:
        body = request.get_json(silent=True) or {}
        messages = body.get("messages")
        single_msg = body.get("message")

        if isinstance(single_msg, str) and single_msg.strip():
            user_text = single_msg.strip()
        elif isinstance(messages, list) and messages:
            last = messages[-1]
            if not isinstance(last, dict) or last.get("role") != "user":
                return jsonify({"error": "messages 末尾必须是 user 消息"}), 400
            user_text = str(last.get("content") or "").strip()
        else:
            return jsonify({"error": "缺少 message 或 messages"}), 400

        if not user_text:
            return jsonify({"error": "消息内容为空"}), 400
        if len(user_text) > _CHAT_MAX_USER_MESSAGE_LEN:
            return jsonify({"error": f"单条消息超过 {_CHAT_MAX_USER_MESSAGE_LEN} 字符上限"}), 400

        # 1) 先把 user 消息持久化
        _save_chat_message("user", user_text)

        # 2) 从 PG 加载历史(已含刚写入的 user),取最近 N 轮喂给 Gemini
        full_history = _load_chat_history()
        recent = full_history[-_CHAT_MAX_HISTORY_TURNS * 2:]

        # 构造上下文
        context_blob = _load_chat_context_blob()
        system_instruction = _build_chat_system_instruction(context_blob)

        # 调 Gemini Pro + Search Grounding
        from google import genai as _genai
        from google.genai import types as _gtypes
        from config import GOOGLE_API_KEY, GEMINI_MODEL_ID

        if not GOOGLE_API_KEY:
            return jsonify({"error": "GOOGLE_API_KEY 未设置"}), 500

        client = _genai.Client(api_key=GOOGLE_API_KEY)
        contents = []
        for m in recent:
            text = str(m.get("content") or "")
            if not text:
                continue
            mapped_role = "user" if m.get("role") == "user" else "model"
            contents.append(_gtypes.Content(role=mapped_role, parts=[_gtypes.Part(text=text)]))

        config = _gtypes.GenerateContentConfig(
            system_instruction=system_instruction,
            tools=[_gtypes.Tool(google_search=_gtypes.GoogleSearch())],
            temperature=0.5,
            max_output_tokens=4096,
        )

        t0 = time.monotonic()
        response = client.models.generate_content(
            model=GEMINI_MODEL_ID,
            contents=contents,
            config=config,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        reply_text = (getattr(response, "text", None) or "").strip()
        if not reply_text:
            return jsonify({"error": "Gemini 返回空响应,可能触发 safety/上下文过长"}), 502

        sources = []
        try:
            cand = (response.candidates or [None])[0]
            gm = getattr(cand, "grounding_metadata", None) if cand else None
            chunks = getattr(gm, "grounding_chunks", None) if gm else None
            if chunks:
                seen = set()
                for c in chunks:
                    web = getattr(c, "web", None)
                    if not web:
                        continue
                    url = getattr(web, "uri", None)
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    sources.append({"url": url, "title": getattr(web, "title", None) or url})
        except Exception:
            pass

        # 3) 持久化 assistant 回复
        _save_chat_message("assistant", reply_text, sources=sources,
                           latency_ms=latency_ms, model=GEMINI_MODEL_ID)

        logger.info("chat: reply ok latency=%dms in_msgs=%d out_chars=%d sources=%d",
                    latency_ms, len(contents), len(reply_text), len(sources))
        return jsonify({
            "reply": reply_text,
            "sources": sources,
            "model": GEMINI_MODEL_ID,
            "latency_ms": latency_ms,
            "context_summary": {
                "positions_count": len(context_blob.get("positions") or []) if isinstance(context_blob.get("positions"), list) else 0,
                "open_orders_count": len(context_blob.get("open_orders") or []) if isinstance(context_blob.get("open_orders"), list) else 0,
                "usdc_balance": context_blob.get("usdc_balance"),
                "btc_spot_price": context_blob.get("btc_spot_price"),
                "has_last_ai_report": bool(context_blob.get("last_ai_report")),
            },
        })
    except Exception as e:
        logger.exception("api_chat error")
        return jsonify({"error": str(e)}), 500


# ============================================================================
# 手动挂单的"BTC 1m K 线收盘触达"延迟触发
# ============================================================================
from services.manual_pending_orders import (
    insert_pending_order as _mpo_insert,
    insert_plan as _mpo_insert_plan,
    list_pending_orders as _mpo_list,
    cancel_pending_order as _mpo_cancel,
    update_pending_order as _mpo_update,
    VALID_OPS as _MPO_VALID_OPS,
    VALID_TRIGGER_KINDS as _MPO_VALID_KINDS,
    VALID_SIZE_TYPES as _MPO_VALID_SIZE_TYPES,
    VALID_PRICE_TYPES as _MPO_VALID_PRICE_TYPES,
)


def _maybe_queue_manual_pending(action: str, data: dict, recommendation_item_id):
    """若 request 带 trigger_op + trigger_btc_price,把订单写入 manual_pending_orders 并返回 jsonify 响应。
    实际触发用 BTC 1m K 线收盘价确认,避免 tick 插针误触发。
    否则返回 None,让上层走立即下单路径。
    """
    op = (data.get('trigger_op') or '').strip()
    raw_price = data.get('trigger_btc_price')
    if not op and raw_price in (None, ''):
        return None  # 走立即下单
    if recommendation_item_id is not None and str(recommendation_item_id).strip() != "":
        return jsonify({'error': '推荐执行不支持延迟触发,请立即下单或在 Recommendations 页签操作'}), 400
    if op not in _MPO_VALID_OPS:
        return jsonify({'error': f'trigger_op 必须是 {_MPO_VALID_OPS}'}), 400
    try:
        trigger_btc_price = float(raw_price)
    except (TypeError, ValueError):
        return jsonify({'error': 'trigger_btc_price 必须是数字'}), 400

    expires_at = None
    raw_expiry_hours = data.get('trigger_expiry_hours')
    if raw_expiry_hours is not None and raw_expiry_hours != '':
        try:
            hours = float(raw_expiry_hours)
            if hours <= 0 or hours > 24 * 30:
                raise ValueError("hours out of range")
            expires_at = datetime.now(timezone.utc) + timedelta(hours=hours)
        except (TypeError, ValueError):
            return jsonify({'error': 'trigger_expiry_hours 必须是 (0, 720] 的数字(小时)'}), 400

    extra: dict = {'profile': APP_PM_PROFILE}
    _apply_intent_tier_snapshot(extra, data.get('token_id'), _build_current_intent_tier_map())
    raw_offset = data.get('trigger_market_offset')
    if raw_offset is not None and raw_offset != '':
        if action != 'sell':
            return jsonify({'error': 'trigger_market_offset 当前仅支持 sell'}), 400
        try:
            offset = float(raw_offset)
        except (TypeError, ValueError):
            return jsonify({'error': 'trigger_market_offset 必须是数字'}), 400
        if offset < -0.5 or offset > 0.5:
            return jsonify({'error': 'trigger_market_offset 必须在 [-0.5, 0.5]'}), 400
        extra['trigger_market_offset'] = offset

    try:
        row = _mpo_insert(
            action=action,
            market_id=str(data['market_id']),
            token_id=str(data['token_id']),
            price=float(data['price']),
            size=float(data['size']),
            trigger_op=op,
            trigger_btc_price=trigger_btc_price,
            expires_at=expires_at,
            notes=str(data.get('trigger_notes') or '')[:500] or None,
            requested_by=session.get('user') or 'dashboard',
            extra=extra,
        )
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        logger.exception("queue manual pending order failed")
        return jsonify({'error': f'排队失败: {e}'}), 500

    logger.info("manual pending order queued: id=%s action=%s op=%s threshold=%s",
                row.get('id'), action, op, trigger_btc_price)
    return jsonify({
        'queued': True,
        'pending_id': row.get('id'),
        'pending': row,
        'message': f"已排队: BTC 1m收盘 {op} {trigger_btc_price} 时下 {action} 单 (有效期至 {row.get('expires_at')})",
    })


@app.route('/api/manual_pending', methods=['GET'])
def api_manual_pending_list():
    try:
        include_finished = (request.args.get('include_finished') or '').lower() in ('1', 'true', 'yes')
        rows = _mpo_list(include_finished=include_finished)
        # Enrich with event_name + outcome label for UI display
        try:
            market_client = get_client(APP_PM_PROFILE)
        except Exception:
            market_client = None
        market_cache: dict[str, dict] = {}
        for r in rows:
            mid = r.get('market_id')
            tid = str(r.get('token_id') or '')
            if not mid:
                continue
            if mid not in market_cache:
                try:
                    m = market_client.get_market(mid) if market_client else {}
                    market_cache[mid] = m or {}
                except Exception:
                    market_cache[mid] = {}
            m = market_cache[mid]
            r['event_name'] = m.get('question') or m.get('title') or ''
            outcome_label = ''
            for tk in (m.get('tokens') or []):
                if str(tk.get('token_id') or '') == tid:
                    outcome_label = str(tk.get('outcome') or '')
                    break
            if not outcome_label:
                outcomes = m.get('outcomes') or []
                token_ids = m.get('token_id') or m.get('clobTokenIds') or []
                for idx, t in enumerate(token_ids):
                    if str(t) == tid and idx < len(outcomes):
                        outcome_label = str(outcomes[idx])
                        break
            r['outcome'] = outcome_label
        return jsonify({'orders': rows})
    except Exception as e:
        logger.exception("api_manual_pending_list error")
        return jsonify({'error': str(e)}), 500


@app.route('/api/recommendation_plans/pending', methods=['GET'])
def api_recommendation_plans_pending():
    """仅把 approved 且可转入 manual_pending_orders 的 recommendation plans 暴露给 pending 面板。"""
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT p.id AS plan_id,
                       p.item_id,
                       p.ordinal,
                       p.action_type,
                       p.status,
                       p.trigger_parse_status,
                       p.trigger_summary,
                       p.trigger_spec,
                       p.suggested_execution_payload,
                       p.armed_execution_payload,
                       p.expires_at,
                       p.reason_text,
                       i.title,
                       i.direction,
                       i.status AS item_status
                  FROM recommendation_action_plans p
                  JOIN recommendation_items i ON i.id = p.item_id
                 WHERE p.status = 'proposed'
                   AND i.status = 'approved'
                   AND COALESCE((p.suggested_execution_payload->>'manual_pending_only')::boolean, false) = true
                 ORDER BY p.id DESC
                 LIMIT 200
                """
            )
            rows = cur.fetchall()
        out = []
        for row in rows:
            d = dict(row)
            expires_at = d.get("expires_at")
            if isinstance(expires_at, datetime):
                d["expires_at"] = expires_at.isoformat()
                d["expires_at_bjt"] = expires_at.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
            payload = d.get("armed_execution_payload") or d.get("suggested_execution_payload") or {}
            d["side"] = payload.get("direction") or payload.get("side") or d.get("direction")
            d["price_cents"] = payload.get("price_cents")
            if d["price_cents"] is None and payload.get("price") is not None:
                try:
                    d["price_cents"] = float(payload.get("price")) * 100.0
                except (TypeError, ValueError):
                    d["price_cents"] = None
            d["size_text"] = payload.get("size_text")
            if not d["size_text"] and payload.get("size_shares") is not None:
                try:
                    d["size_text"] = f'{float(payload.get("size_shares")):.2f} 张'
                except (TypeError, ValueError):
                    d["size_text"] = str(payload.get("size_shares"))
            d["target_question"] = payload.get("target_question") or d.get("title")
            d["pending_order"] = payload.get("pending_order") if isinstance(payload.get("pending_order"), dict) else None
            d["manual_pending_only"] = bool(payload.get("manual_pending_only"))
            out.append(d)
        return jsonify({"plans": out})
    except Exception as e:
        logger.exception("api_recommendation_plans_pending error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/manual_pending/<int:order_id>', methods=['DELETE'])
def api_manual_pending_cancel(order_id: int):
    try:
        row = _mpo_cancel(order_id)
        if not row:
            return jsonify({'error': '订单不存在或已不是 pending 状态'}), 404
        return jsonify({'cancelled': True, 'order': row})
    except Exception as e:
        logger.exception("api_manual_pending_cancel error")
        return jsonify({'error': str(e)}), 500


@app.route('/api/manual_pending/<int:order_id>', methods=['PATCH'])
def api_manual_pending_update(order_id: int):
    data = request.get_json(silent=True) or {}
    updates: dict = {}
    allowed = {"trigger_kind", "trigger_op", "trigger_threshold", "trigger_pct", "size_spec", "price_spec", "notes"}
    for key in allowed:
        if key in data:
            updates[key] = data[key]
    try:
        if "expires_hours" in data and data["expires_hours"] not in (None, ""):
            hours = float(data["expires_hours"])
            if hours <= 0 or hours > 720:
                return jsonify({"error": "expires_hours 必须在 (0,720]"}), 400
            updates["expires_at"] = datetime.now(timezone.utc) + timedelta(hours=hours)
        elif "expires_at" in data and data["expires_at"]:
            raw = str(data["expires_at"]).strip()
            updates["expires_at"] = _parse_dashboard_datetime(raw)
        row = _mpo_update(order_id, updates)
        if not row:
            return jsonify({"error": "订单不存在或已不是 pending 状态"}), 404
        return jsonify({"updated": True, "order": row})
    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as e:
        logger.exception("api_manual_pending_update error")
        return jsonify({"error": str(e)}), 500


@app.route('/api/manual_pending/plan', methods=['POST'])
def api_manual_pending_plan():
    """提交联动计划: { items: [...] }

    每个 item 字段:
      action               buy / sell
      market_id, token_id  必填
      trigger_kind         immediate | btc_abs | share_abs | share_cost_pct
      trigger_op           >= / <=
      trigger_threshold    btc_abs/share_abs 用 (BTC 1m收盘价 或 share 绝对价 0~1); immediate 不需要
      trigger_pct          share_cost_pct 用 (-100~+1000)
      size_spec            {type, value} type ∈ shares/usdc/pct_balance/pct_position
      price_spec           {type, value?/offset?} type ∈ absolute/market/cost_pct
      expires_hours        可选 (默认 24)
      parent_index         可选 (引用 items 前序索引;首项不能有)
      notes                可选
    """
    data = request.get_json(silent=True) or {}
    items = data.get('items')
    if not isinstance(items, list) or not items:
        return jsonify({'error': 'items 必须是非空数组'}), 400
    if len(items) > 10:
        return jsonify({'error': 'items 单次最多 10 项'}), 400

    now = datetime.now(timezone.utc)
    intent_tier_map = _build_current_intent_tier_map()
    parsed_items: list[dict] = []
    for idx, raw in enumerate(items):
        if not isinstance(raw, dict):
            return jsonify({'error': f'items[{idx}] 必须是对象'}), 400
        try:
            hours = float(raw.get('expires_hours') or 24)
            if hours <= 0 or hours > 720:
                raise ValueError('hours out of range')
        except (TypeError, ValueError):
            return jsonify({'error': f'items[{idx}].expires_hours 必须在 (0, 720]'}), 400
        extra = {'profile': APP_PM_PROFILE}
        _apply_intent_tier_snapshot(extra, raw.get('token_id'), intent_tier_map)
        _apply_post_entry_review_extra(extra, raw)
        item = {
            'action': str(raw.get('action') or '').strip().lower(),
            'market_id': str(raw.get('market_id') or '').strip(),
            'token_id': str(raw.get('token_id') or '').strip(),
            'trigger_kind': str(raw.get('trigger_kind') or 'btc_abs').strip().lower(),
            'trigger_op': str(raw.get('trigger_op') or '>=').strip(),
            'trigger_threshold': raw.get('trigger_threshold'),
            'trigger_pct': raw.get('trigger_pct'),
            'size_spec': raw.get('size_spec') or {},
            'price_spec': raw.get('price_spec') or {},
            'expires_at': now + timedelta(hours=hours),
            'notes': raw.get('notes'),
            'extra': extra,
            'parent_index': raw.get('parent_index'),
        }
        parsed_items.append(item)
    try:
        rows = _mpo_insert_plan(
            parsed_items,
            requested_by=session.get('user') or 'dashboard',
        )
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    except Exception as e:
        logger.exception("create manual pending plan failed")
        return jsonify({'error': f'写入失败: {e}'}), 500
    logger.info("manual pending plan created: plan_id=%s items=%s",
                rows[0].get('plan_id'), len(rows))
    return jsonify({
        'queued': True,
        'plan_id': rows[0].get('plan_id'),
        'items': rows,
        'message': f'已排队联动计划 plan_id={rows[0].get("plan_id")}, 共 {len(rows)} 档',
    })


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

        def _to_float_or_none(v):
            try:
                if v is None:
                    return None
                return float(v)
            except (TypeError, ValueError):
                return None

        token_ids = [str(p.get("asset")) for p in positions if p.get("asset")]
        best_prices = {}
        if token_ids:
            try:
                best_prices = get_best_prices(token_ids, profile=APP_PM_PROFILE)
            except Exception as exc:
                logger.warning("api_positions get_best_prices failed: %s", exc)

        result = []
        for p in positions:
            asset = p.get("asset")
            px = best_prices.get(str(asset)) if asset else None
            best_bid = _to_float_or_none((px or {}).get("best_bid"))
            best_ask = _to_float_or_none((px or {}).get("best_ask"))
            size = _to_float(p.get("size"))
            source_cur_price = _to_float_or_none(p.get("curPrice"))
            valuation_price = best_bid if best_bid is not None else source_cur_price
            valuation_source = "best_bid" if best_bid is not None else "polymarket"
            avg_price = _to_float_or_none(p.get("avgPrice"))
            current_value = size * valuation_price if valuation_price is not None else p.get("currentValue")
            initial_value = p.get("initialValue")
            if initial_value is None and size and avg_price is not None:
                initial_value = size * avg_price
            percent_pnl = p.get("percentPnl")
            if best_bid is not None and avg_price and avg_price > 0:
                percent_pnl = ((best_bid / avg_price) - 1.0) * 100
            result.append({
                "asset": asset,
                "conditionId": p.get("conditionId"),
                "title": p.get("title") or "—",
                "outcome": p.get("outcome") or "—",
                "size": size,
                "avgPrice": avg_price,
                "curPrice": valuation_price,
                "curPriceSource": valuation_source,
                "sourceCurPrice": source_cur_price,
                "bestBid": best_bid,
                "bestAsk": best_ask,
                "initialValue": initial_value,
                "currentValue": current_value,
                "percentPnl": percent_pnl,
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


@app.route('/api/open_orders/edit', methods=['POST'])
def api_open_order_edit():
    """编辑交易所 open order：先撤旧单，再按剩余数量重挂新限价单。"""
    data = request.json or {}
    order_id = str(data.get("order_id") or "").strip()
    if not order_id:
        return jsonify({"error": "Missing order_id"}), 400
    try:
        new_price = float(data.get("price"))
        new_size = float(data.get("size"))
    except (TypeError, ValueError):
        return jsonify({"error": "price/size 必须是数字"}), 400
    if not (0 < new_price < 1):
        return jsonify({"error": "price 必须在 (0,1)"}), 400
    if new_size <= 0:
        return jsonify({"error": "size 必须 > 0"}), 400

    try:
        orders = get_open_orders(profile=APP_PM_PROFILE)
        order = next((o for o in orders if str(o.get("id")) == order_id), None)
        if order is None:
            detail = get_order_detail(order_id, profile=APP_PM_PROFILE)
            if isinstance(detail, dict) and str(detail.get("id") or detail.get("orderID") or "") == order_id:
                order = detail
        if not order:
            return jsonify({"error": "订单不存在或已不在 open orders 中"}), 404

        market_id = str(order.get("market") or order.get("market_id") or "").strip()
        token_id = str(order.get("asset_id") or order.get("token_id") or "").strip()
        side = str(order.get("side") or "").strip().upper()
        try:
            original_size = float(order.get("original_size") or order.get("size") or 0)
            matched_size = float(order.get("size_matched") or order.get("matched_size") or 0)
        except (TypeError, ValueError):
            original_size = 0.0
            matched_size = 0.0
        remaining_size = max(original_size - matched_size, 0.0)

        if not market_id or not token_id or side not in {"BUY", "SELL"}:
            return jsonify({"error": "原订单缺少 market/token/side，无法安全编辑"}), 400
        if remaining_size <= 0:
            return jsonify({"error": "原订单已无剩余可编辑数量"}), 409
        if new_size > remaining_size + 1e-9:
            return jsonify({"error": f"新 size 不能超过当前剩余数量 {remaining_size:.6f}"}), 400

        logger.info(
            "api_open_order_edit requested: order_id=%s side=%s market=%s token=%s old_price=%s remaining=%s new_price=%s new_size=%s",
            order_id, side, market_id, token_id, order.get("price"), remaining_size, new_price, new_size,
        )

        cancel_result = cancel_order(order_id, profile=APP_PM_PROFILE)
        if isinstance(cancel_result, dict):
            not_canceled = cancel_result.get("not_canceled") or cancel_result.get("notCanceled") or {}
            canceled = cancel_result.get("canceled")
            if (
                (isinstance(not_canceled, dict) and order_id in not_canceled)
                or (isinstance(canceled, list) and order_id not in [str(x) for x in canceled])
            ):
                return jsonify({"error": f"撤销原订单失败: {cancel_result}", "cancel_result": cancel_result}), 409
        _advisory_record_cancel(
            order_id=order_id,
            user_note="dashboard-edit",
            submission_payload={"edit_to": {"price": new_price, "size": new_size}, "cancel_result": cancel_result},
        )

        if side == "BUY":
            new_order_id = buy_order(
                market_id=market_id,
                token_id=token_id,
                price=new_price,
                size=new_size,
                profile=APP_PM_PROFILE,
            )
            action_side = "buy"
        else:
            new_order_id = sell_order(
                market_id=market_id,
                token_id=token_id,
                price=new_price,
                size=new_size,
                profile=APP_PM_PROFILE,
                order_type=OrderType.GTC,
            )
            action_side = "sell"

        if not new_order_id:
            error_detail = get_last_order_error() or "替换订单下单失败；原订单已撤销"
            logger.warning("api_open_order_edit replacement failed: old_order_id=%s reason=%s", order_id, error_detail)
            return jsonify({
                "error": error_detail,
                "cancelled_order_id": order_id,
                "cancel_result": cancel_result,
            }), 500

        _advisory_record_place(
            token_id=token_id,
            side=action_side,
            price=new_price,
            size_shares=new_size,
            polymarket_order_id=str(new_order_id),
            user_note="dashboard-edit",
            submission_payload={
                "replaces_order_id": order_id,
                "old_price": order.get("price"),
                "old_remaining_size": remaining_size,
                "cancel_result": cancel_result,
            },
        )
        logger.info("api_open_order_edit success: old_order_id=%s new_order_id=%s", order_id, new_order_id)
        return jsonify({
            "old_order_id": order_id,
            "new_order_id": str(new_order_id),
            "cancel_result": cancel_result,
            "price": new_price,
            "size": new_size,
        })
    except Exception as e:
        logger.exception("api_open_order_edit failed: order_id=%s error=%s", order_id, e)
        return jsonify({"error": str(e)}), 500


@app.route('/api/buy', methods=['POST'])
def api_buy():
    data = request.json or {}
    market_id = data.get('market_id')
    token_id = data.get('token_id')
    price = data.get('price')
    size = data.get('size')
    recommendation_item_id = data.get('recommendation_item_id')
    logger.info("api_buy requested: market_id=%s price=%s size=%s", market_id, price, size)
    if not all([market_id, token_id, price, size]):
        logger.warning("api_buy missing parameters: has market_id=%s token_id=%s price=%s size=%s", bool(market_id), bool(token_id), price, size)
        return jsonify({'error': 'Missing parameters'}), 400
    queued = _maybe_queue_manual_pending('buy', data, recommendation_item_id)
    if queued is not None:
        return queued
    if recommendation_item_id is not None and str(recommendation_item_id).strip() != "":
        # 第三轮审查 #1：服务端硬绑定校验，防止用 item_id 越权下别的市场/超大 size
        try:
            _assert_recommendation_request_matches_item(
                item_id=int(recommendation_item_id),
                expected_action_type="buy",
                request_market_id=market_id,
                request_token_id=token_id,
                request_price=float(price),
                request_size=float(size),
            )
        except RecommendationBindingError as be:
            logger.warning("api_buy binding check failed: item_id=%s code=%s msg=%s",
                           recommendation_item_id, be.code, be)
            return jsonify({'error': str(be), 'code': be.code}), be.http_status
        except (TypeError, ValueError) as ve:
            return jsonify({'error': f'recommendation_item_id 非法: {ve}'}), 400
        try:
            _recommendation_db.assert_item_executable(
                item_id=int(recommendation_item_id),
                expected_action_type="buy",
            )
        except RecommendationGateError as ge:
            logger.warning("api_buy blocked by execution gate: item_id=%s error=%s", recommendation_item_id, ge)
            return jsonify({'error': str(ge), 'code': ge.code}), 409
        except (TypeError, ValueError) as ve:
            return jsonify({'error': f'recommendation_item_id 非法: {ve}'}), 400
    try:
        order_id = buy_order(
            market_id,
            token_id,
            float(price),
            float(size),
            profile=APP_PM_PROFILE,
        )
        if order_id is None:
            error_detail = get_last_order_error() or "Order placement failed (null order_id)"
            if recommendation_item_id:
                try:
                    _recommendation_db.record_action(
                        item_id=int(recommendation_item_id),
                        action_type="buy",
                        status="failed",
                        request_payload=data,
                        response_payload={"order_id": None},
                        error_text=error_detail,
                    )
                except Exception:
                    logger.exception("record buy action failed")
            logger.warning("api_buy returned null order_id: market_id=%s price=%s size=%s reason=%s", market_id, price, size, error_detail)
            return jsonify({'error': error_detail, 'order_id': None}), 500
        recommendation_sync_error = None
        if recommendation_item_id:
            try:
                _recommendation_db.record_action(
                    item_id=int(recommendation_item_id),
                    action_type="buy",
                    status="submitted",
                    order_id=order_id,
                    request_payload=data,
                    response_payload={"order_id": order_id},
                )
            except Exception as rec_err:
                logger.exception("record buy action failed (order already placed): item_id=%s order_id=%s",
                                 recommendation_item_id, order_id)
                recommendation_sync_error = str(rec_err)
        logger.info("api_buy success: order_id=%s", order_id)
        # Advisory v2 A3: write place_buy intent (captures fair/edge snapshot).
        _advisory_record_place(
            token_id=token_id, side="buy",
            price=float(price), size_shares=float(size),
            polymarket_order_id=order_id,
            user_note="dashboard",
            submission_payload=data,
        )
        resp = {'order_id': order_id}
        if recommendation_sync_error:
            resp['recommendation_sync_error'] = recommendation_sync_error
        return jsonify(resp)
    except Exception as e:
        if recommendation_item_id:
            try:
                _recommendation_db.record_action(
                    item_id=int(recommendation_item_id),
                    action_type="buy",
                    status="failed",
                    request_payload=data,
                    response_payload={},
                    error_text=str(e),
                )
            except Exception:
                logger.exception("record buy action failed")
        logger.exception("api_buy exception: market_id=%s error=%s", market_id, e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/sell', methods=['POST'])
def api_sell():
    data = request.json or {}
    market_id = data.get('market_id')
    token_id = data.get('token_id')
    price = data.get('price')
    size = data.get('size')
    recommendation_item_id = data.get('recommendation_item_id')
    logger.info("api_sell requested: market_id=%s price=%s size=%s", market_id, price, size)
    if not all([market_id, token_id, price, size]):
        logger.warning("api_sell missing parameters: has market_id=%s token_id=%s price=%s size=%s", bool(market_id), bool(token_id), price, size)
        return jsonify({'error': 'Missing parameters'}), 400
    queued = _maybe_queue_manual_pending('sell', data, recommendation_item_id)
    if queued is not None:
        return queued
    if recommendation_item_id is not None and str(recommendation_item_id).strip() != "":
        # 第三轮审查 #1：服务端硬绑定校验
        try:
            _assert_recommendation_request_matches_item(
                item_id=int(recommendation_item_id),
                expected_action_type="sell",
                request_market_id=market_id,
                request_token_id=token_id,
                request_price=float(price),
                request_size=float(size),
            )
        except RecommendationBindingError as be:
            logger.warning("api_sell binding check failed: item_id=%s code=%s msg=%s",
                           recommendation_item_id, be.code, be)
            return jsonify({'error': str(be), 'code': be.code}), be.http_status
        except (TypeError, ValueError) as ve:
            return jsonify({'error': f'recommendation_item_id 非法: {ve}'}), 400
        try:
            _recommendation_db.assert_item_executable(
                item_id=int(recommendation_item_id),
                expected_action_type="sell",
            )
        except RecommendationGateError as ge:
            logger.warning("api_sell blocked by execution gate: item_id=%s error=%s", recommendation_item_id, ge)
            return jsonify({'error': str(ge), 'code': ge.code}), 409
        except (TypeError, ValueError) as ve:
            return jsonify({'error': f'recommendation_item_id 非法: {ve}'}), 400
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
            error_detail = get_last_order_error() or "Order placement failed (null order_id)"
            if recommendation_item_id:
                try:
                    _recommendation_db.record_action(
                        item_id=int(recommendation_item_id),
                        action_type="sell",
                        status="failed",
                        request_payload=data,
                        response_payload={"order_id": None},
                        error_text=error_detail,
                    )
                except Exception:
                    logger.exception("record sell action failed")
            logger.warning("api_sell returned null order_id: market_id=%s price=%s size=%s reason=%s", market_id, price, size, error_detail)
            return jsonify({'error': error_detail, 'order_id': None}), 500
        recommendation_sync_error = None
        if recommendation_item_id:
            try:
                _recommendation_db.record_action(
                    item_id=int(recommendation_item_id),
                    action_type="sell",
                    status="submitted",
                    order_id=order_id,
                    request_payload=data,
                    response_payload={"order_id": order_id},
                )
            except Exception as rec_err:
                logger.exception("record sell action failed (order already placed): item_id=%s order_id=%s",
                                 recommendation_item_id, order_id)
                recommendation_sync_error = str(rec_err)
        logger.info("api_sell success: order_id=%s", order_id)
        _advisory_record_place(
            token_id=token_id, side="sell",
            price=float(price), size_shares=float(size),
            polymarket_order_id=order_id,
            user_note="dashboard",
            submission_payload=data,
        )
        resp = {'order_id': order_id}
        if recommendation_sync_error:
            resp['recommendation_sync_error'] = recommendation_sync_error
        return jsonify(resp)
    except Exception as e:
        if recommendation_item_id:
            try:
                _recommendation_db.record_action(
                    item_id=int(recommendation_item_id),
                    action_type="sell",
                    status="failed",
                    request_payload=data,
                    response_payload={},
                    error_text=str(e),
                )
            except Exception:
                logger.exception("record sell action failed")
        logger.exception("api_sell exception: market_id=%s error=%s", market_id, e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/cancel', methods=['POST'])
def api_cancel():
    data = request.json or {}
    order_id = data.get('order_id')
    recommendation_item_id = data.get('recommendation_item_id')
    logger.info("api_cancel requested: order_id=%s", order_id)
    if not order_id:
        logger.warning("api_cancel missing order_id")
        return jsonify({'error': 'Missing order_id'}), 400
    if recommendation_item_id is not None and str(recommendation_item_id).strip() != "":
        # 第三轮审查 #1：服务端硬绑定，避免用 cancel 建议去撤别人的单
        try:
            _assert_recommendation_request_matches_item(
                item_id=int(recommendation_item_id),
                expected_action_type="cancel",
                request_order_id=str(order_id),
            )
        except RecommendationBindingError as be:
            logger.warning("api_cancel binding check failed: item_id=%s code=%s msg=%s",
                           recommendation_item_id, be.code, be)
            return jsonify({'error': str(be), 'code': be.code}), be.http_status
        except (TypeError, ValueError) as ve:
            return jsonify({'error': f'recommendation_item_id 非法: {ve}'}), 400
        try:
            _recommendation_db.assert_item_executable(
                item_id=int(recommendation_item_id),
                expected_action_type="cancel",
            )
        except RecommendationGateError as ge:
            logger.warning("api_cancel blocked by execution gate: item_id=%s error=%s", recommendation_item_id, ge)
            return jsonify({'error': str(ge), 'code': ge.code}), 409
        except (TypeError, ValueError) as ve:
            return jsonify({'error': f'recommendation_item_id 非法: {ve}'}), 400
    try:
        result = cancel_order(order_id, profile=APP_PM_PROFILE)
        recommendation_sync_error = None
        if recommendation_item_id:
            try:
                _recommendation_db.record_action(
                    item_id=int(recommendation_item_id),
                    action_type="cancel",
                    status="submitted",
                    order_id=str(order_id),
                    request_payload=data,
                    response_payload={"result": result},
                )
            except Exception as rec_err:
                logger.exception("record cancel action failed (cancel already submitted): item_id=%s order_id=%s",
                                 recommendation_item_id, order_id)
                recommendation_sync_error = str(rec_err)
        logger.info("api_cancel success: order_id=%s result=%s", order_id, result)
        # Advisory v2 A4: mirror cancel as cancel intent.
        _advisory_record_cancel(
            order_id=str(order_id),
            user_note="dashboard",
            submission_payload=data,
        )
        resp = {'result': result}
        if recommendation_sync_error:
            resp['recommendation_sync_error'] = recommendation_sync_error
        return jsonify(resp)
    except Exception as e:
        if recommendation_item_id:
            try:
                _recommendation_db.record_action(
                    item_id=int(recommendation_item_id),
                    action_type="cancel",
                    status="failed",
                    order_id=str(order_id),
                    request_payload=data,
                    response_payload={},
                    error_text=str(e),
                )
            except Exception:
                logger.exception("record cancel action failed")
        logger.exception("api_cancel failed: order_id=%s error=%s", order_id, e)
        return jsonify({'error': str(e)}), 500

@app.route('/api/balance')
def api_balance():
    try:
        balance = get_balance_allowance(profile=APP_PM_PROFILE)
        return jsonify({'balance': balance})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# 钱包充值/提现 (MetaMask EOA <-> Polymarket Proxy/Safe)
# ---------------------------------------------------------------------------
@app.route('/api/wallet/info', methods=['GET'])
def api_wallet_info():
    try:
        from services.wallet_transfer import get_addresses, query_balances
        info = get_addresses(profile=APP_PM_PROFILE)
        info.update(query_balances(profile=APP_PM_PROFILE))
        return jsonify(info)
    except Exception as e:
        logger.exception("api_wallet_info failed: %s", e)
        return jsonify({'error': str(e)}), 500


def _parse_wallet_amount(payload: dict) -> float:
    raw = payload.get('amount')
    if raw is None:
        raise ValueError('缺少参数 amount')
    try:
        amount = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f'非法 amount: {raw!r}')
    if amount <= 0:
        raise ValueError('amount 必须大于 0')
    return amount


@app.route('/api/wallet/deposit', methods=['POST'])
def api_wallet_deposit():
    """从 MetaMask EOA 经 Bridge API 充值到 Polymarket proxy (自动 wrap pUSD)。"""
    data = request.json or {}
    try:
        amount = _parse_wallet_amount(data)
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    source_token = (data.get('source_token') or 'USDC').strip()

    operator = (session.get('operator_name') or 'dashboard') if session else 'dashboard'
    logger.info("api_wallet_deposit requested: amount=%s src=%s operator=%s",
                amount, source_token, operator)

    try:
        from services.wallet_transfer import deposit_via_bridge
        result = deposit_via_bridge(amount, profile=APP_PM_PROFILE, source_token=source_token)
        logger.info("api_wallet_deposit success: tx=%s amount=%s", result.tx_hash, amount)
        return jsonify({
            'tx_hash': result.tx_hash,
            'amount_usdc': result.amount_usdc,
            'from_address': result.from_address,
            'bridge_address': result.bridge_address,
            'proxy_address': result.proxy_address,
            'source_token': result.source_token,
            'explorer_url': f"https://polygonscan.com/tx/{result.tx_hash}",
            'note': '资金已发往 Bridge, 通常 1-2 分钟后自动入账 Polymarket (pUSD)。',
        })
    except Exception as e:
        logger.exception("api_wallet_deposit failed: amount=%s error=%s", amount, e)
        return jsonify({'error': str(e)}), 500


@app.route('/api/wallet/withdraw', methods=['POST'])
def api_wallet_withdraw():
    """从 Polymarket proxy 经 Bridge API 提现到 MetaMask (自动 swap 成原生 USDC)。"""
    data = request.json or {}
    try:
        amount = _parse_wallet_amount(data)
    except ValueError as ve:
        return jsonify({'error': str(ve)}), 400
    dest_token = (data.get('dest_token') or 'USDC').strip()

    operator = (session.get('operator_name') or 'dashboard') if session else 'dashboard'
    logger.info("api_wallet_withdraw requested: amount=%s dest=%s operator=%s",
                amount, dest_token, operator)

    try:
        from services.wallet_transfer import withdraw_via_bridge
        result = withdraw_via_bridge(amount, profile=APP_PM_PROFILE, dest_token=dest_token)
        logger.info(
            "api_wallet_withdraw success: relayer_id=%s tx=%s amount=%s",
            result.relayer_transaction_id, result.tx_hash, amount,
        )
        return jsonify({
            'relayer_transaction_id': result.relayer_transaction_id,
            'tx_hash': result.tx_hash,
            'state': result.state,
            'amount_usdc': result.amount_usdc,
            'from_address': result.from_address,
            'bridge_address': result.bridge_address,
            'recipient_address': result.recipient_address,
            'source_token': result.source_token,
            'dest_token': result.dest_token,
            'explorer_url': (
                f"https://polygonscan.com/tx/{result.tx_hash}" if result.tx_hash else None
            ),
            'note': '资金已发往 Bridge, 通常 1-2 分钟后自动到账 MetaMask (原生 USDC)。',
        })
    except Exception as e:
        logger.exception("api_wallet_withdraw failed: amount=%s error=%s", amount, e)
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


@app.route('/api/btc_fast_move_signal')
def api_btc_fast_move_signal():
    """Return Binance WS fast up/down pressure signal for BTCUSDT."""
    try:
        from services.fast_move_signal import get_default_signal_service

        service = get_default_signal_service(auto_start=True)
        return jsonify(service.get_snapshot())
    except Exception as e:
        logger.exception("api_btc_fast_move_signal failed")
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


def _strip_recommendation_direction_suffix(title: str) -> str:
    return re.sub(r"\s*\((yes|no)\)\s*$", "", str(title or "").strip(), flags=re.IGNORECASE).strip()


def _resolve_recommendation_market(question_title: str, direction: str) -> tuple[str, str]:
    event_data = get_event_token_id()
    markets = event_data.get("markets", []) if isinstance(event_data, dict) else []
    normalized_title = _strip_recommendation_direction_suffix(question_title).lower()
    normalized_direction = str(direction or "").strip().lower()
    for market in markets:
        if not isinstance(market, dict):
            continue
        question = str(market.get("question") or "").strip()
        if question.lower() != normalized_title:
            continue
        outcomes = market.get("outcomes") or []
        token_ids = market.get("token_id") or []
        for idx, outcome in enumerate(outcomes):
            if str(outcome or "").strip().lower() == normalized_direction and idx < len(token_ids):
                return str(market.get("market_id") or ""), str(token_ids[idx] or "")
        raise ValueError(f"未找到方向 {direction} 对应的 token_id")
    raise ValueError(f"未找到 recommendation 对应的市场: {question_title}")


def _parse_recommendation_amount_usdc(size_text: str) -> float | None:
    text = str(size_text or "")
    matched = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", text.replace(",", ""))
    if not matched:
        return None
    try:
        return float(matched.group(1))
    except ValueError:
        return None


def _parse_recommendation_shares(size_text: str) -> float | None:
    text = str(size_text or "").strip()
    if not text or "$" in text:
        return None
    matched = re.search(r"([0-9]+(?:\.[0-9]+)?)", text.replace(",", ""))
    if not matched:
        return None
    try:
        return float(matched.group(1))
    except ValueError:
        return None


def _recommendation_default_price(action_type: str, low_cents: float | None, high_cents: float | None) -> float | None:
    if low_cents is None and high_cents is None:
        return None
    low = low_cents if low_cents is not None else high_cents
    high = high_cents if high_cents is not None else low_cents
    if low is None or high is None:
        return None
    selected_cents = high if action_type == "buy" else low
    return round(float(selected_cents) / 100.0, 4)


def _infer_recommendation_correlation_group(title: str) -> str | None:
    lower = _strip_recommendation_direction_suffix(title).lower()
    if not lower:
        return None
    if "dip to" in lower or "below" in lower:
        return "btc_below"
    if "reach" in lower or "above" in lower:
        return "btc_above"
    return None


def _resolve_recommendation_market_snapshot(question_title: str, direction: str) -> dict:
    event_data = get_event_token_id()
    markets = event_data.get("markets", []) if isinstance(event_data, dict) else []
    normalized_title = _strip_recommendation_direction_suffix(question_title).lower()
    normalized_direction = str(direction or "").strip().lower()
    for market in markets:
        if not isinstance(market, dict):
            continue
        question = str(market.get("question") or "").strip()
        if question.lower() != normalized_title:
            continue
        outcomes = market.get("outcomes") or []
        token_ids = market.get("token_id") or []
        prices = market.get("outcomePrices") or []
        for idx, outcome in enumerate(outcomes):
            if str(outcome or "").strip().lower() != normalized_direction:
                continue
            token_id = str(token_ids[idx] or "") if idx < len(token_ids) else ""
            try:
                current_price = float(prices[idx]) if idx < len(prices) else None
            except (TypeError, ValueError):
                current_price = None
            return {
                "question": question,
                "market_id": str(market.get("market_id") or ""),
                "token_id": token_id,
                "current_price": current_price,
            }
        raise ValueError(f"未找到方向 {direction} 对应的 token_id")
    raise ValueError(f"未找到 recommendation 对应的市场: {question_title}")


def _position_current_value(position: dict) -> float:
    try:
        current_value = position.get("currentValue")
        if current_value is not None:
            return float(current_value)
    except (TypeError, ValueError):
        pass
    try:
        return float(position.get("size") or 0.0) * float(position.get("curPrice") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _build_profile_snapshot(profile: str) -> dict:
    balance_str = get_balance_allowance(profile=profile)
    cash_balance = _parse_cash_balance(balance_str)
    positions = get_positions(profile=profile)
    position_value = sum(_position_current_value(position) for position in positions if isinstance(position, dict))
    return {
        "cash_balance": cash_balance,
        "position_value": position_value,
        "profile_value": cash_balance + position_value,
        "positions": positions,
    }


def _estimate_recommendation_order_notional(price: float, size_text: str) -> dict:
    amount_usdc = _parse_recommendation_amount_usdc(size_text)
    shares = _parse_recommendation_shares(size_text)
    if amount_usdc is not None:
        return {
            "input_mode": "amount",
            "input_value": amount_usdc,
            "estimated_notional": amount_usdc,
            "estimated_shares": round(amount_usdc / max(price, 1e-9), 6),
        }
    if shares is not None:
        return {
            "input_mode": "quantity",
            "input_value": shares,
            "estimated_notional": round(shares * price, 6),
            "estimated_shares": shares,
        }
    return {
        "input_mode": "quantity",
        "input_value": None,
        "estimated_notional": None,
        "estimated_shares": None,
    }


def _build_execution_preflight(
    *,
    item: dict,
    market_snapshot: dict,
    run_created_at: datetime | None,
    trigger_type: str,
    profile_snapshot: dict,
    order_estimate: dict,
) -> tuple[bool, list[dict]]:
    checks: list[dict] = []
    title_base = _strip_recommendation_direction_suffix(item["title"])
    direction = str(item.get("direction") or "").strip().lower()
    correlation_group = str(item.get("correlation_group") or "").strip() or _infer_recommendation_correlation_group(title_base)
    profile_value = float(profile_snapshot.get("profile_value") or 0.0)
    cash_balance = float(profile_snapshot.get("cash_balance") or 0.0)
    estimated_notional = order_estimate.get("estimated_notional")
    estimated_shares = order_estimate.get("estimated_shares")
    positions = profile_snapshot.get("positions") or []

    max_age_hours = (
        RECOMMENDATION_EVENT_MAX_AGE_HOURS
        if str(trigger_type or "").strip().lower() in {"event", "event_driven", "triggered"}
        else RECOMMENDATION_DEFAULT_MAX_AGE_HOURS
    )
    if isinstance(run_created_at, datetime):
        age_hours = max((datetime.now(timezone.utc) - run_created_at.astimezone(timezone.utc)).total_seconds() / 3600.0, 0.0)
        if age_hours > max_age_hours:
            checks.append({
                "name": "recommendation_age",
                "status": "fail",
                "message": f"建议已过期：距生成已 {age_hours:.1f}h，超过 {max_age_hours:.1f}h 上限。",
            })
        else:
            checks.append({
                "name": "recommendation_age",
                "status": "pass",
                "message": f"建议时效正常：{age_hours:.1f}h / {max_age_hours:.1f}h。",
            })
    else:
        checks.append({
            "name": "recommendation_age",
            "status": "warn",
            "message": "无法解析建议生成时间，建议人工确认时效。",
        })

    current_price = market_snapshot.get("current_price")
    low_cents = item.get("suggested_price_low_cents")
    high_cents = item.get("suggested_price_high_cents")
    if current_price is None or (low_cents is None and high_cents is None):
        checks.append({
            "name": "market_price_alignment",
            "status": "warn",
            "message": "无法完成当前市场价校验，将使用建议价格开单。",
        })
    else:
        current_cents = float(current_price) * 100.0
        low = low_cents if low_cents is not None else high_cents
        high = high_cents if high_cents is not None else low_cents
        if low is None or high is None:
            checks.append({
                "name": "market_price_alignment",
                "status": "warn",
                "message": "建议价格区间不完整，跳过当前市场价校验。",
            })
        elif item["action_type"] == "buy":
            if current_cents > float(high) + RECOMMENDATION_PRICE_WARN_TOLERANCE_CENTS:
                checks.append({
                    "name": "market_price_alignment",
                    "status": "warn",
                    "message": f"当前价 {current_cents:.1f}¢ 高于建议区间 {low:.1f}-{high:.1f}¢，挂单可能较难成交。",
                })
            elif current_cents < float(low) - RECOMMENDATION_PRICE_WARN_TOLERANCE_CENTS:
                checks.append({
                    "name": "market_price_alignment",
                    "status": "warn",
                    "message": f"当前价 {current_cents:.1f}¢ 低于建议区间 {low:.1f}-{high:.1f}¢，建议确认是否仍需该挂单。",
                })
            else:
                checks.append({
                    "name": "market_price_alignment",
                    "status": "pass",
                    "message": f"当前价 {current_cents:.1f}¢ 与建议区间 {low:.1f}-{high:.1f}¢ 基本一致。",
                })
        else:
            if current_cents < float(low) - 10.0:
                checks.append({
                    "name": "market_price_alignment",
                    "status": "warn",
                    "message": f"当前价 {current_cents:.1f}¢ 明显低于建议卖价 {low:.1f}¢，该卖单可能长期挂在盘口上。",
                })
            else:
                checks.append({
                    "name": "market_price_alignment",
                    "status": "pass",
                    "message": f"当前价 {current_cents:.1f}¢ 已完成卖单价格检查。",
                })

    if estimated_notional is None:
        checks.append({
            "name": "size_parse",
            "status": "warn",
            "message": "无法从建议中精确解析金额/数量；下单前请在弹窗中人工确认。",
        })
    else:
        checks.append({
            "name": "size_parse",
            "status": "pass",
            "message": f"预计订单名义金额约 ${estimated_notional:.2f}。",
        })

    current_market_value = 0.0
    current_correlation_value = 0.0
    current_position_shares = 0.0
    for position in positions:
        if not isinstance(position, dict):
            continue
        position_title = _strip_recommendation_direction_suffix(str(position.get("title") or ""))
        position_outcome = str(position.get("outcome") or "").strip().lower()
        position_value = _position_current_value(position)
        if position_title.lower() == title_base.lower():
            current_market_value += position_value
            if position_outcome == direction:
                current_position_shares += float(position.get("size") or 0.0)
        if correlation_group and _infer_recommendation_correlation_group(position_title) == correlation_group:
            current_correlation_value += position_value

    if profile_value > 0 and estimated_notional is not None and item["action_type"] == "buy":
        single_cap = profile_value * RECOMMENDATION_SINGLE_MARKET_CAP_RATIO
        projected_market_value = current_market_value + estimated_notional
        if projected_market_value > single_cap + 1e-6:
            checks.append({
                "name": "single_market_cap",
                "status": "fail",
                "message": f"单标的上限超标：当前 ${current_market_value:.2f}，下单后 ${projected_market_value:.2f} > cap ${single_cap:.2f}。",
            })
        else:
            checks.append({
                "name": "single_market_cap",
                "status": "pass",
                "message": f"单标的敞口安全：下单后 ${projected_market_value:.2f} / cap ${single_cap:.2f}。",
            })

        if correlation_group:
            corr_cap = profile_value * RECOMMENDATION_CORRELATION_CAP_RATIO
            projected_corr_value = current_correlation_value + estimated_notional
            if projected_corr_value > corr_cap + 1e-6:
                checks.append({
                    "name": "correlation_group_cap",
                    "status": "fail",
                    "message": f"{correlation_group} 聚合敞口超标：下单后 ${projected_corr_value:.2f} > cap ${corr_cap:.2f}。",
                })
            else:
                checks.append({
                    "name": "correlation_group_cap",
                    "status": "pass",
                    "message": f"{correlation_group} 聚合敞口安全：下单后 ${projected_corr_value:.2f} / cap ${corr_cap:.2f}。",
                })
    else:
        checks.append({
            "name": "single_market_cap",
            "status": "warn",
            "message": "无法完成单标的上限校验；请人工确认当前仓位。",
        })
        if correlation_group:
            checks.append({
                "name": "correlation_group_cap",
                "status": "warn",
                "message": f"无法完成 {correlation_group} 聚合敞口校验；请人工确认相关性风险。",
            })

    if item["action_type"] == "buy":
        if estimated_notional is None:
            checks.append({
                "name": "balance_check",
                "status": "warn",
                "message": "无法估算买入金额，跳过余额校验。",
            })
        elif estimated_notional > cash_balance + 1e-6:
            checks.append({
                "name": "balance_check",
                "status": "fail",
                "message": f"余额不足：需要 ${estimated_notional:.2f}，当前可用 ${cash_balance:.2f}。",
            })
        else:
            checks.append({
                "name": "balance_check",
                "status": "pass",
                "message": f"余额充足：需要 ${estimated_notional:.2f}，当前可用 ${cash_balance:.2f}。",
            })
    else:
        if estimated_shares is None:
            checks.append({
                "name": "position_size_check",
                "status": "warn",
                "message": "无法估算卖出数量，跳过持仓数量校验。",
            })
        elif current_position_shares + 1e-6 < estimated_shares:
            checks.append({
                "name": "position_size_check",
                "status": "fail",
                "message": f"持仓不足：建议卖出 {estimated_shares:.2f} 股，当前同方向持仓仅 {current_position_shares:.2f} 股。",
            })
        else:
            checks.append({
                "name": "position_size_check",
                "status": "pass",
                "message": f"持仓充足：当前同方向持仓 {current_position_shares:.2f} 股。",
            })

    allow_execute = not any(check["status"] == "fail" for check in checks)
    return allow_execute, checks


def _extract_recommendation_target_order_id(raw_payload: dict | None) -> str | None:
    if not isinstance(raw_payload, dict):
        return None
    for key in ("目标挂单ID", "target_order_id", "order_id"):
        value = str(raw_payload.get(key) or "").strip()
        if value:
            return value
    return None


def _extract_pending_management(raw_payload: dict | None) -> dict | None:
    if not isinstance(raw_payload, dict):
        return None
    value = raw_payload.get("pending_management") or raw_payload.get("待触发订单管理")
    if not isinstance(value, dict):
        return None
    target_id = value.get("target_pending_order_id") or value.get("目标pending订单ID")
    action = str(value.get("action") or value.get("处理动作") or "").strip().lower()
    if action not in {"keep", "modify", "cancel", "replace"}:
        action = "modify" if value.get("suggested_updates") else "keep"
    return {
        "target_pending_order_id": target_id,
        "target_plan_id": value.get("target_plan_id") or value.get("目标plan_id"),
        "action": action,
        "reason": value.get("reason") or value.get("理由"),
        "suggested_updates": value.get("suggested_updates") if isinstance(value.get("suggested_updates"), dict) else {},
    }


def _find_open_order_by_id(order_id: str) -> dict | None:
    target_order_id = str(order_id or "").strip()
    if not target_order_id:
        return None
    for order in get_open_orders(profile=APP_PM_PROFILE):
        if str(order.get("id") or "").strip() == target_order_id:
            return order
    return None


def _build_cancel_preflight(target_order_id: str | None, target_order: dict | None) -> tuple[bool, list[dict]]:
    checks: list[dict] = []
    if not str(target_order_id or "").strip():
        checks.append({
            "status": "fail",
            "code": "missing_target_order_id",
            "message": "该撤单建议缺少目标挂单ID，无法执行。",
        })
        return False, checks
    if not isinstance(target_order, dict):
        checks.append({
            "status": "fail",
            "code": "target_order_not_found",
            "message": "目标挂单已不存在于当前 open orders，可能已成交或已被撤销。",
        })
        return False, checks

    try:
        original_size = float(target_order.get("original_size") or 0.0)
    except (TypeError, ValueError):
        original_size = 0.0
    try:
        matched_size = float(target_order.get("size_matched") or 0.0)
    except (TypeError, ValueError):
        matched_size = 0.0
    remaining_size = max(original_size - matched_size, 0.0)
    if remaining_size <= 0:
        checks.append({
            "status": "fail",
            "code": "no_remaining_size",
            "message": "目标挂单剩余数量为 0，当前无需再撤单。",
        })
    elif matched_size > 0:
        checks.append({
            "status": "warn",
            "code": "partially_matched",
            "message": f"该挂单已部分成交 {matched_size:.4f}，撤单仅会取消剩余 {remaining_size:.4f}。",
        })
    else:
        checks.append({
            "status": "pass",
            "code": "order_open",
            "message": "目标挂单当前仍在 open orders 中，可以执行撤单。",
        })
    allow_execute = not any(check["status"] == "fail" for check in checks)
    return allow_execute, checks


# 服务端硬绑定：当请求带 recommendation_item_id 时，强制校验
#   buy/sell：market_id / token_id / price / size 必须与 item 解析出的目标一致或在合法范围内
#   cancel：order_id 必须等于 item.raw_payload 指定的 target_order_id，且仍在 open orders
# 这是第三轮审查 #1 的修复点：避免“批准一条建议 → 任意改 market_id/size/order_id 越权下单/撤单”。
class RecommendationBindingError(Exception):
    def __init__(self, message: str, code: str = "binding_failed", http_status: int = 409):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


_RECOMMENDATION_PRICE_BINDING_TOLERANCE_CENTS = 5.0  # 价格允许在建议区间外 ±5¢
_RECOMMENDATION_SIZE_BINDING_MAX_RATIO = 1.10  # 实际下单 size 不得超过建议估算 size 的 110%


def _assert_recommendation_request_matches_item(
    *,
    item_id: int,
    expected_action_type: str,
    request_market_id: str | None = None,
    request_token_id: str | None = None,
    request_price: float | None = None,
    request_size: float | None = None,
    request_order_id: str | None = None,
) -> dict:
    """在 execution gate 之前，强制核对请求体中的目标参数与 recommendation item 绑定关系。

    返回解析后的 item dict（含 raw_payload / suggested_price / size_text 等）。
    任何不匹配抛 RecommendationBindingError，调用方应返回 409（资金边界拒绝）。
    """
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT ri.id, ri.title, ri.action_type, ri.direction, ri.item_kind,
                   ri.size_text, ri.suggested_price_text,
                   ri.suggested_price_low_cents, ri.suggested_price_high_cents,
                   ri.status, ri.raw_payload
            FROM recommendation_items ri
            WHERE ri.id = %s
            """,
            (int(item_id),),
        )
        item = cur.fetchone()
    if not item:
        raise RecommendationBindingError(f"recommendation item {item_id} 不存在", code="item_not_found", http_status=404)
    item = dict(item)
    if str(item["action_type"] or "").strip().lower() != expected_action_type:
        raise RecommendationBindingError(
            f"item {item_id} 的 action_type={item['action_type']}，与本次请求 {expected_action_type} 不一致",
            code="action_type_mismatch",
        )

    if expected_action_type in {"buy", "sell"}:
        if not item.get("direction"):
            raise RecommendationBindingError("该建议缺少方向信息，无法核对目标 token", code="missing_direction")
        try:
            market_snapshot = _resolve_recommendation_market_snapshot(item["title"], item["direction"])
        except Exception as exc:  # noqa: BLE001
            raise RecommendationBindingError(
                f"无法解析建议对应的市场快照：{exc}",
                code="market_resolution_failed",
                http_status=400,
            )
        expected_market_id = str(market_snapshot.get("market_id") or "").strip()
        expected_token_id = str(market_snapshot.get("token_id") or "").strip()
        if not expected_market_id or not expected_token_id:
            raise RecommendationBindingError("建议对应的 market_id/token_id 未能解析", code="market_resolution_empty")

        actual_market_id = str(request_market_id or "").strip()
        actual_token_id = str(request_token_id or "").strip()
        if actual_market_id != expected_market_id:
            raise RecommendationBindingError(
                f"market_id 与建议不一致：请求 {actual_market_id}，建议绑定 {expected_market_id}",
                code="market_id_mismatch",
            )
        if actual_token_id != expected_token_id:
            raise RecommendationBindingError(
                f"token_id 与建议不一致：请求 {actual_token_id}，建议绑定 {expected_token_id}",
                code="token_id_mismatch",
            )

        # 价格边界：必须在建议区间 ±tolerance 内
        # 第四轮加固 #3（中）：fail-closed —— 若 recommendation 缺少价格区间或 size 估算失败，
        # 不允许降级（之前直接 skip 校验导致绑定退化成只认 market/token）。
        low_cents = item.get("suggested_price_low_cents")
        high_cents = item.get("suggested_price_high_cents")
        if request_price is None:
            raise RecommendationBindingError("缺少价格参数", code="missing_price", http_status=400)
        try:
            price_cents = float(request_price) * 100.0
        except (TypeError, ValueError):
            raise RecommendationBindingError("价格非法", code="invalid_price", http_status=400)
        if not math.isfinite(price_cents) or price_cents < 0:
            raise RecommendationBindingError(
                f"价格非有限正数: {request_price}", code="invalid_price", http_status=400,
            )
        if low_cents is None or high_cents is None:
            raise RecommendationBindingError(
                "建议未给出价格区间，无法做绑定校验，拒绝执行（请补建议或走非 recommendation 路径）",
                code="missing_suggested_price_band",
            )
        try:
            lo_raw = float(low_cents)
            hi_raw = float(high_cents)
        except (TypeError, ValueError):
            raise RecommendationBindingError(
                f"建议价格区间非数字: {low_cents}-{high_cents}", code="invalid_suggested_price_band",
            )
        if not (math.isfinite(lo_raw) and math.isfinite(hi_raw)) or lo_raw < 0 or hi_raw < 0 or hi_raw < lo_raw:
            raise RecommendationBindingError(
                f"建议价格区间非法: {lo_raw}-{hi_raw}", code="invalid_suggested_price_band",
            )
        lo = lo_raw - _RECOMMENDATION_PRICE_BINDING_TOLERANCE_CENTS
        hi = hi_raw + _RECOMMENDATION_PRICE_BINDING_TOLERANCE_CENTS
        if not (lo <= price_cents <= hi):
            raise RecommendationBindingError(
                f"价格 {price_cents:.1f}¢ 超出建议区间 {lo_raw:.1f}-{hi_raw:.1f}¢ ±{_RECOMMENDATION_PRICE_BINDING_TOLERANCE_CENTS:.0f}¢",
                code="price_out_of_band",
            )

        # Size 上限：基于建议 size_text 估算，硬上限 110% 防止越权放大
        if request_size is None:
            raise RecommendationBindingError("缺少 size 参数", code="missing_size", http_status=400)
        try:
            actual_size = float(request_size)
        except (TypeError, ValueError):
            raise RecommendationBindingError("size 非法", code="invalid_size", http_status=400)
        if not math.isfinite(actual_size) or actual_size <= 0:
            raise RecommendationBindingError(
                f"size 必须为有限正数: {request_size}", code="invalid_size", http_status=400,
            )
        try:
            est = _estimate_recommendation_order_notional(float(request_price), str(item.get("size_text") or ""))
            estimated_shares = est.get("estimated_shares")
        except Exception:  # noqa: BLE001
            estimated_shares = None
        if estimated_shares is None or not math.isfinite(float(estimated_shares)) or float(estimated_shares) <= 0:
            raise RecommendationBindingError(
                f"建议 size_text 无法解析为有效股数（size_text={item.get('size_text')!r}），"
                f"拒绝执行以防越权放大",
                code="missing_size_estimate",
            )
        cap = float(estimated_shares) * _RECOMMENDATION_SIZE_BINDING_MAX_RATIO
        if actual_size > cap:
            raise RecommendationBindingError(
                f"size {actual_size} 超过建议估算 {float(estimated_shares):.4f} 的 {_RECOMMENDATION_SIZE_BINDING_MAX_RATIO:.0%} 上限",
                code="size_over_cap",
            )
        return item

    if expected_action_type == "cancel":
        expected_order_id = _extract_recommendation_target_order_id(item.get("raw_payload"))
        if not expected_order_id:
            raise RecommendationBindingError(
                "该撤单建议未指定 target_order_id，无法绑定校验",
                code="missing_target_order_id",
            )
        actual_order_id = str(request_order_id or "").strip()
        if actual_order_id != str(expected_order_id).strip():
            raise RecommendationBindingError(
                f"order_id 与建议不一致：请求 {actual_order_id}，建议绑定 {expected_order_id}",
                code="order_id_mismatch",
            )
        # 还要确认该订单仍属于本账户的 open orders（防止跨账户/已成交订单）
        target_order = _find_open_order_by_id(actual_order_id)
        if not target_order:
            raise RecommendationBindingError(
                "目标挂单已不在当前账户的 open orders 中（可能已成交或已撤销）",
                code="target_order_not_open",
            )
        return item

    raise RecommendationBindingError(f"不支持的 action_type: {expected_action_type}", code="unsupported_action", http_status=400)


def _build_auto_iteration_proposals(memory_context: dict) -> list[dict]:
    feedback_summary = memory_context.get("recent_feedback_summary", {}) if isinstance(memory_context, dict) else {}
    execution_summary = memory_context.get("recent_execution_summary", {}) if isinstance(memory_context, dict) else {}
    top_reason_tags = feedback_summary.get("top_reason_tags", []) or []
    action_status_counts = execution_summary.get("action_status_counts", {}) or {}
    decision_counts = feedback_summary.get("decision_counts", {}) or {}
    learning_disabled_count = int(feedback_summary.get("learning_disabled_count") or 0)
    tag_count_map = {
        str(item.get("tag") or ""): int(item.get("count") or 0)
        for item in top_reason_tags
        if isinstance(item, dict)
    }

    proposals: list[dict] = []
    if tag_count_map.get("价格不合适", 0) >= 2:
        proposals.append({
            "proposal_type": "prompt_tweak",
            "title": "强化价格敏感度与建议价格调整说明",
            "rationale": "最近反馈中“价格不合适”出现频繁，说明当前建议价格区间与操作员可接受价格存在偏差。建议强化 prompt 中对未执行建议的价格复盘和更优挂单价调整说明。",
            "change_payload": {
                "target": "ai.prompts",
                "suggested_change": "在 Step 0/建仓建议部分增加对未执行建议的价格回顾与调整要求。",
            },
            "evidence_payload": {
                "tag": "价格不合适",
                "count": tag_count_map.get("价格不合适", 0),
            },
        })
    if tag_count_map.get("仓位重复", 0) >= 2 or tag_count_map.get("相关性过高", 0) >= 2:
        proposals.append({
            "proposal_type": "sizing_rule",
            "title": "收紧同方向相关性与重复仓位建议",
            "rationale": "最近反馈提示同方向/重复仓位过多，说明建议系统在相关性或已有持仓复用上仍偏激进。建议收紧相关性提示，并在建仓建议中更明确扣减已有同组仓位预算。",
            "change_payload": {
                "target": "execution_and_prompt",
                "suggested_change": "提高 correlation_group 风险提示权重，并在建议文本中明确剩余可用风险预算。",
            },
            "evidence_payload": {
                "仓位重复": tag_count_map.get("仓位重复", 0),
                "相关性过高": tag_count_map.get("相关性过高", 0),
            },
        })
    if int(action_status_counts.get("failed", 0) or 0) >= 2:
        proposals.append({
            "proposal_type": "execution_rule",
            "title": "加强执行前校验与失败原因分解",
            "rationale": "最近 recommendation_actions 中失败次数偏多，说明 execution gate 或下单参数映射仍需增强。建议增加失败原因归因与更明确的前置拦截规则。",
            "change_payload": {
                "target": "execution_gate",
                "suggested_change": "细化失败分类，增加价格偏离/持仓不足/余额不足等分项统计与提案。",
            },
            "evidence_payload": {
                "failed_action_count": int(action_status_counts.get("failed", 0) or 0),
            },
        })
    if int(decision_counts.get("defer", 0) or 0) >= 3 or tag_count_map.get("稍后处理", 0) >= 2:
        proposals.append({
            "proposal_type": "cadence_rule",
            "title": "收紧补充建议频率与重复提醒阈值",
            "rationale": "最近出现较多 defer/稍后处理，说明当前补充建议节奏偏密或重复提醒过多。建议提高去重阈值，并对低优先级建议增加冷却时间。",
            "change_payload": {
                "target": "cadence_and_dedupe",
                "suggested_change": "对非高优先级建议增加 cooldown，并在已有 pending/deferred 建议时减少相近新建议生成。",
            },
            "evidence_payload": {
                "defer_count": int(decision_counts.get("defer", 0) or 0),
                "稍后处理": tag_count_map.get("稍后处理", 0),
            },
        })
    if int(decision_counts.get("reject", 0) or 0) >= 4 or learning_disabled_count >= 2:
        proposals.append({
            "proposal_type": "feedback_policy",
            "title": "加强人工意图约束并降低重复建议",
            "rationale": "近期 reject 与禁止模型学习反馈偏多，说明系统部分建议与操作员稳定判断边界存在冲突。建议更强地吸收人工边界，减少与既有人工计划相冲突的建议。",
            "change_payload": {
                "target": "memory_and_prompt",
                "suggested_change": "提高人工已有计划/allow_model_learning=false 信号权重，并在重复 reject 后降低相近建议优先级。",
            },
            "evidence_payload": {
                "reject_count": int(decision_counts.get("reject", 0) or 0),
                "learning_disabled_count": learning_disabled_count,
                "人工已有计划": tag_count_map.get("人工已有计划", 0),
            },
        })
    return proposals


def _get_latest_recommendation_prompt_version(asset: str = "btc") -> str | None:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT prompt_version
            FROM recommendation_runs
            WHERE asset = %s
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (asset,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return row[0] if isinstance(row, (list, tuple)) else row["prompt_version"]


def _format_shadow_eval_row(row: dict) -> dict:
    created_at = row[1] if isinstance(row, (list, tuple)) else row["created_at"]
    return {
        "id": row["id"] if not isinstance(row, (list, tuple)) else row[0],
        "proposal_id": row["proposal_id"] if not isinstance(row, (list, tuple)) else row[2],
        "target_scope": row["target_scope"] if not isinstance(row, (list, tuple)) else row[3],
        "baseline_version": row["baseline_version"] if not isinstance(row, (list, tuple)) else row[4],
        "candidate_version": row["candidate_version"] if not isinstance(row, (list, tuple)) else row[5],
        "status": row["status"] if not isinstance(row, (list, tuple)) else row[6],
        "metrics": (row["metrics"] if not isinstance(row, (list, tuple)) else row[7]) or {},
        "notes": row["notes"] if not isinstance(row, (list, tuple)) else row[8],
        "created_at_utc8": created_at.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S") if isinstance(created_at, datetime) else str(created_at or ""),
    }


def _build_shadow_eval_payload(proposal: dict, memory_context: dict) -> tuple[dict, str]:
    proposal_type = str(proposal.get("proposal_type") or "").strip().lower()
    title = str(proposal.get("title") or "").strip()
    evidence = proposal.get("evidence_payload") or {}
    feedback_summary = memory_context.get("recent_feedback_summary", {}) if isinstance(memory_context, dict) else {}
    execution_summary = memory_context.get("recent_execution_summary", {}) if isinstance(memory_context, dict) else {}
    total_feedback_count = int(feedback_summary.get("total_feedback_count") or 0)
    learning_disabled_count = int(feedback_summary.get("learning_disabled_count") or 0)
    action_status_counts = execution_summary.get("action_status_counts", {}) or {}

    baseline_label = "recent_feedback"
    baseline_count = total_feedback_count
    candidate_label = "estimated_issue_count_after_change"
    improvement_ratio = 0.20
    confidence = "low"

    if proposal_type == "prompt_tweak":
        baseline_label = str(evidence.get("tag") or "价格不合适")
        baseline_count = int(evidence.get("count") or 0)
        improvement_ratio = 0.35
        confidence = "medium" if baseline_count >= 3 else "low"
    elif proposal_type == "sizing_rule":
        repeated_count = int(evidence.get("仓位重复") or 0)
        correlation_count = int(evidence.get("相关性过高") or 0)
        baseline_label = "仓位重复/相关性过高"
        baseline_count = repeated_count + correlation_count
        improvement_ratio = 0.30
        confidence = "medium" if baseline_count >= 4 else "low"
    elif proposal_type == "execution_rule":
        baseline_label = "execution_failures"
        baseline_count = int(evidence.get("failed_action_count") or action_status_counts.get("failed") or 0)
        improvement_ratio = 0.40
        confidence = "medium" if baseline_count >= 3 else "low"

    estimated_after = max(baseline_count - max(1, round(baseline_count * improvement_ratio)), 0) if baseline_count > 0 else 0
    absolute_reduction = max(baseline_count - estimated_after, 0)
    reduction_pct = round((absolute_reduction / baseline_count) * 100.0, 1) if baseline_count > 0 else 0.0
    decision_hint = "worth_review" if baseline_count >= 2 and reduction_pct >= 20.0 else "weak_signal"

    metrics = {
        "evaluation_type": "heuristic_offline",
        "proposal_title": title,
        "baseline_signal": {
            "label": baseline_label,
            "count": baseline_count,
        },
        "candidate_estimate": {
            "label": candidate_label,
            "count": estimated_after,
        },
        "estimated_absolute_reduction": absolute_reduction,
        "estimated_relative_reduction_pct": reduction_pct,
        "confidence": confidence,
        "decision_hint": decision_hint,
        "feedback_window_days": memory_context.get("feedback_window_days"),
        "outcome_window_days": memory_context.get("outcome_window_days"),
        "learning_disabled_count": learning_disabled_count,
        "action_status_counts": action_status_counts,
    }
    notes = (
        "heuristic_offline shadow eval：基于最近 feedback/action 汇总估计该提案可能减少的问题数量；"
        "仅供审批前参考，不代表真实线上 A/B 结果。"
    )
    return metrics, notes


def _create_shadow_eval_for_proposal(proposal: dict, memory_context: dict | None = None) -> dict:
    current_memory_context = memory_context or _recommendation_db.build_memory_context(asset="btc")
    proposal_id = proposal["id"] if not isinstance(proposal, (list, tuple)) else proposal[0]
    target_scope = str(
        (proposal["target_scope"] if not isinstance(proposal, (list, tuple)) else proposal[4]) or "monthly_recommendation"
    ).strip() or "monthly_recommendation"
    baseline_version = _get_latest_recommendation_prompt_version(asset="btc") or "current"
    candidate_version = f"{baseline_version}+proposal-{proposal_id}"
    metrics, notes = _build_shadow_eval_payload(proposal, current_memory_context)
    created = _recommendation_db.create_model_shadow_eval(
        proposal_id=int(proposal_id),
        target_scope=target_scope,
        baseline_version=baseline_version,
        candidate_version=candidate_version,
        status="completed",
        metrics=metrics,
        notes=notes,
    )
    return _format_shadow_eval_row(created)


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
        # SELL 单只锁仓位不锁 USDC,故只需扣减 BUY 单的剩余 notional 即得真实可用余额。
        buy_locked = 0.0
        try:
            for o in get_open_orders(profile=APP_PM_PROFILE):
                if str(o.get("side", "")).upper() != "BUY":
                    continue
                try:
                    rem = float(o.get("original_size") or 0) - float(o.get("size_matched") or 0)
                    px = float(o.get("price") or 0)
                    if rem > 0 and px > 0:
                        buy_locked += rem * px
                except (TypeError, ValueError):
                    pass
        except Exception:
            logger.warning("balance_summary: get_open_orders failed", exc_info=True)
        available = max(0.0, cash - buy_locked)
        monthly_progress = get_or_set_monthly_baseline(profile_value)
        return jsonify({
            "cash_balance": balance_str,
            "position_value": round(position_value, 2),
            "profile_value": round(profile_value, 2),
            "month_start_profile_value": monthly_progress.get("baseline_net_value"),
            "monthly_return_pct": monthly_progress.get("monthly_pnl_pct"),
            "monthly_pnl_usdc": monthly_progress.get("monthly_pnl_usdc"),
            "buy_locked": round(buy_locked, 2),
            "available_balance": round(available, 2),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/run_position_analyze', methods=['POST'])
def api_run_position_analyze():
    """Run position_analyze.py in the background."""
    try:
        body = request.get_json(silent=True) or {}
        operator_intent = (body.get("operator_intent") or "").strip()
        trigger_reason = (body.get("trigger_reason") or "").strip()
        sub_env = os.environ.copy()
        sub_env["POLYMARKET_PROFILE"] = "analyze"
        sub_env["ANALYZE_TRIGGER_TYPE"] = "manual"
        if operator_intent:
            sub_env["OPERATOR_INTENT"] = operator_intent
        if trigger_reason:
            sub_env["ANALYZE_TRIGGER_REASON"] = trigger_reason
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


@app.route('/api/recommendations/latest')
def api_recommendations_latest():
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, created_at, asset, analysis_kind, profile, trigger_type, trigger_reason,
                       operator_intent, model_id, prompt_family, prompt_version,
                       btc_price, days_left_in_month, recommendation_count, summary_text,
                       status
                FROM recommendation_runs
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            run = cur.fetchone()
            if not run:
                return jsonify({"run": None, "items": [], "summary": {"total_items": 0}})

            run_id = run["id"]
            cur.execute(
                """
                SELECT ri.id, ri.source_section, ri.item_kind, ri.title, ri.action_type, ri.direction, ri.strategy_type,
                       ri.suggested_price_text, ri.suggested_price_low_cents, ri.suggested_price_high_cents,
                       ri.size_text, ri.trigger_condition, ri.reason, ri.edge_text, ri.confidence_text,
                       ri.correlation_group, ri.priority_hint, ri.status,
                       ri.raw_payload,
                       ri.trigger_spec, ri.trigger_parse_status,
                       ri.auto_execute_enabled, ri.auto_executor_state, ri.auto_executor_state_at,
                       fb.decision AS latest_decision,
                       fb.reason_tags AS latest_reason_tags,
                       fb.feedback_text AS latest_feedback_text,
                       fb.allow_model_learning AS latest_allow_model_learning,
                       fb.created_at AS latest_feedback_at,
                       ra.action_type AS latest_action_type,
                       ra.status AS latest_action_status,
                       ra.order_id AS latest_action_order_id,
                       ra.error_text AS latest_action_error_text,
                       ra.created_at AS latest_action_at,
                       plans.plans AS plans,
                       mpo.queued_pending_order_count,
                       mpo.active_queued_pending_order_count
                FROM recommendation_items ri
                LEFT JOIN LATERAL (
                    SELECT decision, reason_tags, feedback_text, allow_model_learning, created_at
                    FROM recommendation_feedback rf
                    WHERE rf.item_id = ri.id
                    ORDER BY rf.created_at DESC, rf.id DESC
                    LIMIT 1
                ) fb ON TRUE
                LEFT JOIN LATERAL (
                    SELECT action_type, status, order_id, error_text, created_at
                    FROM recommendation_actions ra
                    WHERE ra.item_id = ri.id
                    ORDER BY ra.created_at DESC, ra.id DESC
                    LIMIT 1
                ) ra ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COALESCE(json_agg(plan_obj ORDER BY ordinal), '[]'::json) AS plans
                    FROM (
                        SELECT p.ordinal,
                               json_build_object(
                                   'id', p.id,
                                   'ordinal', p.ordinal,
                                   'action_type', p.action_type,
                                   'status', p.status,
                                   'trigger_spec', p.trigger_spec,
                                   'trigger_summary', p.trigger_summary,
                                   'trigger_parse_status', p.trigger_parse_status,
                                   'expires_at', p.expires_at,
                                   'suggested_execution_payload', p.suggested_execution_payload,
                                   'armed_execution_payload', p.armed_execution_payload,
                                   'reason_text', p.reason_text,
                                   'fired_at', p.fired_at,
                                   'fired_order_id', p.fired_order_id
                               ) AS plan_obj
                        FROM recommendation_action_plans p
                        WHERE p.item_id = ri.id
                    ) sub
                ) plans ON TRUE
                LEFT JOIN LATERAL (
                    SELECT COUNT(*)::int AS queued_pending_order_count,
                           COUNT(*) FILTER (WHERE mpo.status IN ('pending','executing'))::int AS active_queued_pending_order_count
                    FROM manual_pending_orders mpo
                    WHERE mpo.extra->>'source' = 'recommendation_pending_order'
                      AND mpo.extra->>'recommendation_item_id' = ri.id::text
                ) mpo ON TRUE
                WHERE ri.run_id = %s
                ORDER BY
                    CASE ri.priority_hint
                        WHEN '立即执行' THEN 1
                        WHEN '挂单等待' THEN 2
                        WHEN '仅观察' THEN 3
                        ELSE 9
                    END,
                    id ASC
                """,
                (run_id,),
            )
            items = cur.fetchall()

        created_at = run["created_at"]
        if isinstance(created_at, datetime):
            created_at_utc8 = created_at.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        else:
            created_at_utc8 = _format_utc_time(str(created_at or ""))

        response_run = {
            "id": run_id,
            "created_at_utc8": created_at_utc8,
            "asset": run["asset"],
            "analysis_kind": run["analysis_kind"],
            "profile": run["profile"],
            "trigger_type": run["trigger_type"],
            "trigger_reason": run["trigger_reason"],
            "operator_intent": run["operator_intent"],
            "model_id": run["model_id"],
            "prompt_family": run["prompt_family"],
            "prompt_version": run["prompt_version"],
            "btc_price": run["btc_price"],
            "days_left_in_month": run["days_left_in_month"],
            "recommendation_count": run["recommendation_count"],
            "summary_text": run["summary_text"],
            "status": run["status"],
        }
        response_items = [
            {
                "target_order_id": _extract_recommendation_target_order_id(item["raw_payload"]),
                "pending_management": _extract_pending_management(item["raw_payload"]),
                "id": item["id"],
                "source_section": item["source_section"],
                "item_kind": item["item_kind"],
                "title": item["title"],
                "action_type": item["action_type"],
                "direction": item["direction"],
                "strategy_type": item["strategy_type"],
                "suggested_price_text": item["suggested_price_text"],
                "suggested_price_low_cents": item["suggested_price_low_cents"],
                "suggested_price_high_cents": item["suggested_price_high_cents"],
                "size_text": item["size_text"],
                "trigger_condition": item["trigger_condition"],
                "reason": item["reason"],
                "edge_text": item["edge_text"],
                "confidence_text": item["confidence_text"],
                "correlation_group": item["correlation_group"],
                "priority_hint": item["priority_hint"],
                "status": item["status"],
                "trigger_spec": item["trigger_spec"],
                "trigger_parse_status": item["trigger_parse_status"],
                "auto_execute_enabled": bool(item["auto_execute_enabled"]),
                "auto_executor_state": item["auto_executor_state"],
                "auto_executor_state_at_utc8": (
                    item["auto_executor_state_at"].astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
                    if isinstance(item["auto_executor_state_at"], datetime)
                    else None
                ),
                "latest_feedback": (
                    {
                        "decision": item["latest_decision"],
                        "reason_tags": item["latest_reason_tags"] or [],
                        "feedback_text": item["latest_feedback_text"],
                        "allow_model_learning": item["latest_allow_model_learning"],
                        "created_at_utc8": (
                            item["latest_feedback_at"].astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
                            if isinstance(item["latest_feedback_at"], datetime)
                            else None
                        ),
                    }
                    if item["latest_decision"]
                    else None
                ),
                "latest_action": (
                    {
                        "action_type": item["latest_action_type"],
                        "status": item["latest_action_status"],
                        "order_id": item["latest_action_order_id"],
                        "error_text": item["latest_action_error_text"],
                        "created_at_utc8": (
                            item["latest_action_at"].astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
                            if isinstance(item["latest_action_at"], datetime)
                            else None
                        ),
                    }
                    if item["latest_action_status"]
                    else None
                ),
                "queued_pending_order_count": int(item["queued_pending_order_count"] or 0),
                "active_queued_pending_order_count": int(item["active_queued_pending_order_count"] or 0),
                "action_plans": item.get("plans") or [],
                "take_profit": (item.get("raw_payload") or {}).get("止盈目标") or (item.get("raw_payload") or {}).get("止盈阈值"),
                "stop_loss": (item.get("raw_payload") or {}).get("止损规则") or (item.get("raw_payload") or {}).get("止损阈值") or (item.get("raw_payload") or {}).get("认错止损价"),
                "max_hold": (item.get("raw_payload") or {}).get("最长持仓"),
            }
            for item in items
        ]
        summary = {
            "total_items": len(response_items),
            "by_kind": {
                kind: sum(1 for item in response_items if item["item_kind"] == kind)
                for kind in sorted({item["item_kind"] for item in response_items})
            },
        }
        return jsonify({"run": response_run, "items": response_items, "summary": summary})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/recommendations/<int:item_id>/outcome', methods=['POST'])
def api_recommendation_outcome(item_id: int):
    """人工为已执行的 recommendation item 登记最终结果（命中/未命中/PnL/备注）。

    - 写入 recommendation_outcomes，build_memory_context 会自动把最近 30 天 outcomes 摘要
      回流给模型，实现 Phase 5 的 outcomes 闭环。
    - 仅允许 order_submitted/cancel_submitted/order_failed/cancel_failed/won/lost/expired 的 item 登记。
    """
    try:
        body = request.get_json(silent=True) or {}
        outcome_label = body.get("outcome_label")
        hit_raw = body.get("hit")
        if hit_raw is None:
            hit = None
        elif isinstance(hit_raw, bool):
            hit = hit_raw
        elif isinstance(hit_raw, str):
            hit = hit_raw.strip().lower() in {"1", "true", "yes", "y"}
        else:
            hit = bool(hit_raw)

        pnl = body.get("pnl")
        notes = body.get("notes")
        metrics = body.get("metrics") or {}
        if not isinstance(metrics, dict):
            return jsonify({"error": "metrics 必须是对象"}), 400

        result = _recommendation_db.record_outcome(
            item_id=item_id,
            outcome_label=str(outcome_label or ""),
            hit=hit,
            pnl=pnl,
            notes=notes,
            metrics=metrics,
            recorded_by=str(session.get("user") or session.get("username") or "dashboard"),
            revision_reason=body.get("revision_reason"),
        )
        evaluated_at = result.get("evaluated_at")
        if isinstance(evaluated_at, datetime):
            result["evaluated_at_utc8"] = evaluated_at.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        result.pop("evaluated_at", None)
        return jsonify(result)
    except RecommendationGateError as ge:
        return jsonify({"error": str(ge), "code": ge.code}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("api_recommendation_outcome failed: item_id=%s error=%s", item_id, e)
        return jsonify({"error": str(e)}), 500


@app.route('/api/recommendations/stale_executing')
def api_recommendations_stale_executing():
    """巡检：返回卡在 executing 状态超过阈值的 item，供 Dashboard 顶部告警条使用。"""
    try:
        timeout_minutes_raw = request.args.get("timeout_minutes", "15")
        try:
            timeout_minutes = max(1, int(timeout_minutes_raw))
        except (TypeError, ValueError):
            timeout_minutes = 15
        rows = _recommendation_db.list_stale_executing(timeout_minutes=timeout_minutes)
        for r in rows:
            ts = r.get("executing_started_at")
            if isinstance(ts, datetime):
                r["executing_started_at_utc8"] = ts.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
            r.pop("executing_started_at", None)
            run_ts = r.get("run_created_at")
            if isinstance(run_ts, datetime):
                r["run_created_at_utc8"] = run_ts.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
            r.pop("run_created_at", None)
        return jsonify({
            "items": rows,
            "count": len(rows),
            "timeout_minutes": timeout_minutes,
        })
    except Exception as e:
        logger.exception("api_recommendations_stale_executing failed: %s", e)
        return jsonify({"error": str(e)}), 500


@app.route('/api/recommendations/<int:item_id>/release_executing', methods=['POST'])
def api_recommendations_release_executing(item_id: int):
    """人工解除卡在 executing 的 item，回到 approved/order_failed/cancel_failed 之一。

    第四轮加固 #1：若该 item 已存在 submitted action（订单可能已发出但回写失败），
    后端默认拒绝释放；调用方必须显式传 acknowledge_possible_duplicate=true 才能继续，
    且不允许释放到 approved（强制走 order_failed/cancel_failed → 下一轮人工 approve）。
    """
    try:
        body = request.get_json(silent=True) or {}
        reason = body.get("reason")
        new_status = body.get("new_status") or "order_failed"
        ack = bool(body.get("acknowledge_possible_duplicate") or False)
        result = _recommendation_db.force_release_executing(
            item_id=item_id,
            reason=str(reason or ""),
            released_by=str(session.get("user") or session.get("username") or "dashboard"),
            new_status=str(new_status),
            acknowledge_possible_duplicate=ack,
        )
        return jsonify(result)
    except RecommendationGateError as ge:
        return jsonify({"error": str(ge), "code": ge.code}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("api_recommendations_release_executing failed: item_id=%s error=%s", item_id, e)
        return jsonify({"error": str(e)}), 500


@app.route('/api/recommendations/feedback', methods=['POST'])
def api_recommendations_feedback():
    try:
        body = request.get_json(silent=True) or {}
        item_id_raw = body.get("item_id")
        decision = str(body.get("decision") or "").strip().lower()
        try:
            item_id = int(item_id_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "item_id 非法"}), 400

        if decision not in {"execute", "reject", "defer", "read"}:
            return jsonify({"error": "decision 非法"}), 400

        reason_tags = body.get("reason_tags") or []
        if not isinstance(reason_tags, list):
            return jsonify({"error": "reason_tags 必须是数组"}), 400

        feedback_text = str(body.get("feedback_text") or "").strip()
        allow_model_learning = bool(body.get("allow_model_learning", True))

        result = _recommendation_db.submit_feedback(
            item_id=item_id,
            decision=decision,
            reason_tags=reason_tags,
            feedback_text=feedback_text,
            allow_model_learning=allow_model_learning,
            raw_payload=body,
        )
        # 阶段4:reject/defer 时把该 item 所有非终态 plan 一并 disarmed,避免"item rejected 但 plan 仍 armed→fire"
        if decision in {"reject", "defer"}:
            try:
                from services.recommendation_trigger import auto_trigger_db as atdb
                n = atdb.cascade_cancel_plans_for_item(
                    item_id=item_id,
                    reason=f"feedback:{decision}",
                )
                if n:
                    logger.info("feedback cascade-cancel: item=%s decision=%s plans=%d", item_id, decision, n)
            except Exception:  # noqa: BLE001
                logger.exception("feedback cascade-cancel 异常 item=%s", item_id)
        created_at = result.get("created_at")
        if isinstance(created_at, datetime):
            result["created_at_utc8"] = created_at.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        result.pop("created_at", None)
        return jsonify(result)
    except RecommendationGateError as ge:
        return jsonify({"error": str(ge), "code": ge.code}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/recommendations/<int:item_id>/execution_preview')
def api_recommendation_execution_preview(item_id: int):
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT ri.id, ri.title, ri.action_type, ri.direction, ri.item_kind, ri.size_text,
                       suggested_price_text, suggested_price_low_cents, suggested_price_high_cents,
                       ri.status, ri.correlation_group, ri.raw_payload,
                       rr.created_at AS run_created_at,
                       rr.trigger_type AS run_trigger_type
                FROM recommendation_items ri
                JOIN recommendation_runs rr ON rr.id = ri.run_id
                WHERE ri.id = %s
                """,
                (item_id,),
            )
            item = cur.fetchone()
        if not item:
            return jsonify({"error": "recommendation item 不存在"}), 404
        if item["status"] not in {"approved", "order_failed", "cancel_failed"}:
            return jsonify({"error": "仅已批准执行或下单失败的建议可以进入 execution gate"}), 400
        if item["action_type"] not in {"buy", "sell", "cancel"}:
            return jsonify({"error": "该建议不是可下单类型"}), 400
        if item["action_type"] == "cancel":
            target_order_id = _extract_recommendation_target_order_id(item["raw_payload"])
            target_order = _find_open_order_by_id(target_order_id or "")
            allow_execute, checks = _build_cancel_preflight(target_order_id, target_order)
            # 第五轮加固 #3：preview 必须复用真实 submit 路径的服务端绑定校验。
            # 之前 preview 只跑 preflight，submit 才跑绑定 → 出现 preview 通过但 submit 409 的 UX 假象。
            try:
                _assert_recommendation_request_matches_item(
                    item_id=int(item["id"]),
                    expected_action_type="cancel",
                    request_order_id=target_order_id,
                )
            except RecommendationBindingError as bind_exc:
                allow_execute = False
                checks.append({
                    "name": "服务端绑定校验",
                    "ok": False,
                    "detail": f"[{bind_exc.code}] {bind_exc}",
                })
            return jsonify({
                "item_id": item["id"],
                "title": item["title"],
                "action_type": item["action_type"],
                "direction": item["direction"],
                "order_id": target_order_id,
                "status": item["status"],
                "allow_execute": allow_execute,
                "checks": checks,
                "cancel_target": {
                    "order_id": str((target_order or {}).get("id") or target_order_id or ""),
                    "side": str((target_order or {}).get("side") or ""),
                    "outcome": str((target_order or {}).get("outcome") or ""),
                    "price": (target_order or {}).get("price"),
                    "original_size": (target_order or {}).get("original_size"),
                    "matched_size": (target_order or {}).get("size_matched"),
                },
                "note": "撤单执行会直接调用现有 cancel API，并把结果写回 recommendation_actions。",
            })
        if not item["direction"]:
            return jsonify({"error": "该建议缺少方向信息"}), 400

        market_snapshot = _resolve_recommendation_market_snapshot(item["title"], item["direction"])
        default_price = _recommendation_default_price(
            item["action_type"],
            item["suggested_price_low_cents"],
            item["suggested_price_high_cents"],
        )
        if default_price is None:
            return jsonify({"error": "该建议缺少建议价格，无法生成下单预览"}), 400

        order_estimate = _estimate_recommendation_order_notional(default_price, item["size_text"] or "")
        profile_snapshot = _build_profile_snapshot(APP_PM_PROFILE)
        allow_execute, checks = _build_execution_preflight(
            item=item,
            market_snapshot=market_snapshot,
            run_created_at=item.get("run_created_at"),
            trigger_type=str(item.get("run_trigger_type") or ""),
            profile_snapshot=profile_snapshot,
            order_estimate=order_estimate,
        )

        # 第五轮加固 #3：把 submit 时的服务端绑定校验提前到 preview，
        # 避免 preview 显示 allow_execute=true 但实际下单被 409 binding_failed 拒。
        # 用 preview 默认值（market_id/token_id/price/估算 size）跑同一份校验函数，失败就强制禁掉执行。
        binding_size_for_check = None
        try:
            est_shares = order_estimate.get("estimated_shares") if isinstance(order_estimate, dict) else None
            if est_shares is not None:
                binding_size_for_check = float(est_shares)
        except (TypeError, ValueError):
            binding_size_for_check = None
        try:
            _assert_recommendation_request_matches_item(
                item_id=int(item["id"]),
                expected_action_type=str(item["action_type"]),
                request_market_id=market_snapshot.get("market_id"),
                request_token_id=market_snapshot.get("token_id"),
                request_price=float(default_price),
                request_size=binding_size_for_check,
            )
        except RecommendationBindingError as bind_exc:
            allow_execute = False
            checks.append({
                "name": "服务端绑定校验",
                "ok": False,
                "detail": f"[{bind_exc.code}] {bind_exc}",
            })

        return jsonify({
            "item_id": item["id"],
            "title": item["title"],
            "action_type": item["action_type"],
            "direction": item["direction"],
            "market_id": market_snapshot["market_id"],
            "token_id": market_snapshot["token_id"],
            "price": default_price,
            "suggested_price_text": item["suggested_price_text"],
            "current_market_price": market_snapshot.get("current_price"),
            "input_mode": order_estimate["input_mode"],
            "input_value": order_estimate["input_value"],
            "size_text": item["size_text"],
            "status": item["status"],
            "allow_execute": allow_execute,
            "checks": checks,
            "note": "预览参数已根据建议自动填充；正式下单前仍可在弹窗内手动调整。",
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _assert_plan_request_binding(
    *,
    plan: dict,
    request_market_id: str | None,
    request_token_id: str | None,
    request_price: float | None,
    request_size: float | None,
    precomputed_estimated_shares: float | None = None,
) -> None:
    """阶段4 plan 级绑定校验:类似 item 级,但全部基于 plan 自己的字段。
    - action_type: plan.action_type 必须 in {buy, sell}
    - market/token: 由 item.title + (plan.suggested_execution_payload.side or item.direction) 解析
    - 价格区间: 优先 plan.suggested_execution_payload.price_cents,fallback 到 item.suggested_price_*
    - size: plan.suggested_execution_payload.size_text fallback item.size_text
    """
    sugg = plan.get("suggested_execution_payload") or {}
    plan_action = str(plan.get("action_type") or "").strip().lower()
    if plan_action not in {"buy", "sell"}:
        raise RecommendationBindingError(
            f"plan action_type={plan_action} 不支持自动执行", code="action_type_mismatch",
        )
    direction = sugg.get("side") or plan.get("direction")
    if not direction:
        raise RecommendationBindingError("plan 缺少方向", code="missing_direction")
    try:
        target_question = (sugg.get("target_question") or "").strip() or plan["title"]
        market_snapshot = _resolve_recommendation_market_snapshot(target_question, direction)
    except Exception as exc:  # noqa: BLE001
        raise RecommendationBindingError(
            f"无法解析 market: {exc}", code="market_resolution_failed", http_status=400,
        )
    expected_mid = str(market_snapshot.get("market_id") or "").strip()
    expected_tid = str(market_snapshot.get("token_id") or "").strip()
    if not expected_mid or not expected_tid:
        raise RecommendationBindingError("market_id/token_id 解析为空", code="market_resolution_empty")
    if str(request_market_id or "").strip() != expected_mid:
        raise RecommendationBindingError(
            f"market_id 不一致: 请求 {request_market_id} 期望 {expected_mid}",
            code="market_id_mismatch",
        )
    if str(request_token_id or "").strip() != expected_tid:
        raise RecommendationBindingError(
            f"token_id 不一致: 请求 {request_token_id} 期望 {expected_tid}",
            code="token_id_mismatch",
        )

    # 价格区间: 优先 plan suggested price_cents 单点±tolerance;否则 fallback item 的 low/high
    if request_price is None:
        raise RecommendationBindingError("缺少价格", code="missing_price", http_status=400)
    try:
        price_cents = float(request_price) * 100.0
    except (TypeError, ValueError):
        raise RecommendationBindingError("价格非法", code="invalid_price", http_status=400)
    if not math.isfinite(price_cents) or price_cents < 0:
        raise RecommendationBindingError("价格非有限", code="invalid_price", http_status=400)
    plan_pc = sugg.get("price_cents")
    lo_raw = hi_raw = None
    if plan_pc is not None:
        try:
            pc = float(plan_pc)
            lo_raw = hi_raw = pc
        except (TypeError, ValueError):
            lo_raw = hi_raw = None
    if lo_raw is None:
        lo_raw = plan.get("suggested_price_low_cents")
        hi_raw = plan.get("suggested_price_high_cents")
    if lo_raw is None or hi_raw is None:
        raise RecommendationBindingError(
            "缺少价格区间,无法绑定校验", code="missing_suggested_price_band",
        )
    try:
        lo_raw = float(lo_raw); hi_raw = float(hi_raw)
    except (TypeError, ValueError):
        raise RecommendationBindingError(
            f"价格区间非数: {lo_raw}-{hi_raw}", code="invalid_suggested_price_band",
        )
    if not (math.isfinite(lo_raw) and math.isfinite(hi_raw)) or lo_raw < 0 or hi_raw < lo_raw:
        raise RecommendationBindingError(
            f"价格区间非法: {lo_raw}-{hi_raw}", code="invalid_suggested_price_band",
        )
    lo = lo_raw - _RECOMMENDATION_PRICE_BINDING_TOLERANCE_CENTS
    hi = hi_raw + _RECOMMENDATION_PRICE_BINDING_TOLERANCE_CENTS
    if not (lo <= price_cents <= hi):
        raise RecommendationBindingError(
            f"价格 {price_cents:.1f}¢ 超出 {lo_raw:.1f}-{hi_raw:.1f}¢ ±{_RECOMMENDATION_PRICE_BINDING_TOLERANCE_CENTS:.0f}¢",
            code="price_out_of_band",
        )

    # size 上限
    if request_size is None:
        raise RecommendationBindingError("缺少 size", code="missing_size", http_status=400)
    try:
        actual_size = float(request_size)
    except (TypeError, ValueError):
        raise RecommendationBindingError("size 非法", code="invalid_size", http_status=400)
    if not math.isfinite(actual_size) or actual_size <= 0:
        raise RecommendationBindingError("size 必须正数", code="invalid_size", http_status=400)
    size_text = sugg.get("size_text") or plan.get("size_text") or ""
    # 若 caller 已经计算过(如 enable 路由先调 _build_plan_frozen_payload), 直接复用避免再走一次 API。
    if precomputed_estimated_shares is not None and float(precomputed_estimated_shares) > 0:
        est_shares = float(precomputed_estimated_shares)
    else:
        try:
            est_shares = _resolve_plan_size_shares(plan, str(size_text), float(request_price), request_token_id)
        except Exception:  # noqa: BLE001
            est_shares = None
    if est_shares is None or not math.isfinite(float(est_shares)) or float(est_shares) <= 0:
        raise RecommendationBindingError(
            f"size_text 无法解析: {size_text!r}", code="missing_size_estimate",
        )
    cap = float(est_shares) * _RECOMMENDATION_SIZE_BINDING_MAX_RATIO
    if actual_size > cap:
        raise RecommendationBindingError(
            f"size {actual_size} 超过估算 {float(est_shares):.4f} 的 {_RECOMMENDATION_SIZE_BINDING_MAX_RATIO:.0%} 上限",
            code="size_over_cap",
        )


def _shares_from_size_spec(spec: dict, price_dollars: float, token_id: str | None, plan: dict) -> float | None:
    """根据结构化 size_spec 计算 share 数。返回 None 表示无法解析。"""
    mode = str(spec.get("mode") or "").strip().lower()
    try:
        value = float(spec.get("value"))
    except (TypeError, ValueError):
        return None
    if value <= 0 or not math.isfinite(value):
        return None
    if mode == "shares":
        return value
    if mode == "amount_usdc":
        return value / max(price_dollars, 1e-9)
    if mode == "portion_position":
        if not token_id:
            return None
        positions = _build_profile_snapshot(APP_PM_PROFILE).get("positions") or []
        for pos in positions:
            if str(pos.get("asset") or pos.get("token_id") or "") == str(token_id):
                cur_shares = float(pos.get("size") or pos.get("shares") or 0)
                if cur_shares > 0:
                    return cur_shares * min(value, 100.0) / 100.0
        return None
    if mode in {"portion_equity", "portion_cash"}:
        snap = _build_profile_snapshot(APP_PM_PROFILE)
        base = float(snap.get("profile_value") or 0.0) if mode == "portion_equity" else float(snap.get("cash_balance") or 0.0)
        if base <= 0:
            return None
        notional = base * min(value, 100.0) / 100.0
        return notional / max(price_dollars, 1e-9)
    return None


def _resolve_plan_size_shares(plan: dict, size_text: str, price_dollars: float, token_id: str | None) -> float | None:
    """统一的 plan size 解析逻辑,供 frozen_payload 构造和绑定校验复用。
    顺序:
      ① 优先 suggested_execution_payload.size_spec(AI 给的结构化字段);
      ② 直接 parse size_text(数字/$amount);
      ③ "全部/全平/清仓/all" 兜底:a) 当前账户在该 token 上的持仓 share;
                                  b) 同 item 内 buy sibling plan 的 size。
    """
    sugg = plan.get("suggested_execution_payload") or {}
    spec = sugg.get("size_spec") if isinstance(sugg, dict) else None
    if isinstance(spec, dict):
        try:
            size_spec_shares = _shares_from_size_spec(spec, price_dollars, token_id, plan)
        except Exception:  # noqa: BLE001
            logger.exception("plan %s size_spec 解析失败", plan.get("plan_id"))
            size_spec_shares = None
        if size_spec_shares and size_spec_shares > 0:
            return float(size_spec_shares)
    estimate = _estimate_recommendation_order_notional(price_dollars, size_text)
    try:
        size = float(estimate.get("estimated_shares")) if estimate else None
    except (TypeError, ValueError):
        size = None
    if size and size > 0:
        return size
    if not re.search(r"全部|全平|all\b|清仓", str(size_text or ""), re.IGNORECASE):
        return None
    if str(plan.get("action_type") or "").lower() != "sell":
        return None
    # ① 当前持仓
    if token_id:
        try:
            positions = _build_profile_snapshot(APP_PM_PROFILE).get("positions") or []
            for pos in positions:
                if str(pos.get("asset") or pos.get("token_id") or "") == str(token_id):
                    cur_shares = float(pos.get("size") or pos.get("shares") or 0)
                    if cur_shares > 0:
                        return cur_shares
        except Exception:  # noqa: BLE001
            logger.exception("plan %s 查持仓失败", plan.get("plan_id"))
    # ② sibling buy plan
    try:
        with get_cursor() as _cur:
            _cur.execute(
                """
                SELECT armed_execution_payload, suggested_execution_payload
                  FROM recommendation_action_plans
                 WHERE item_id = %s AND action_type = 'buy' AND id <> %s
                 ORDER BY ordinal LIMIT 1
                """,
                (int(plan["item_id"]), int(plan["plan_id"])),
            )
            sib = _cur.fetchone()
        if sib:
            sib_armed = sib.get("armed_execution_payload") or {}
            sib_sugg = sib.get("suggested_execution_payload") or {}
            sib_size = sib_armed.get("size_shares") or sib_armed.get("size")
            if not sib_size:
                sib_size_text = sib_sugg.get("size_text") or ""
                sib_est = _estimate_recommendation_order_notional(price_dollars, sib_size_text)
                sib_size = sib_est.get("estimated_shares") if sib_est else None
            if sib_size and float(sib_size) > 0:
                return float(sib_size)
    except Exception:  # noqa: BLE001
        logger.exception("plan %s 查 sibling buy 失败", plan.get("plan_id"))
    return None


def _fetch_plan_with_item(plan_id: int) -> dict | None:
    with get_cursor() as cur:
        cur.execute(
            """
            SELECT p.id AS plan_id, p.item_id, p.action_type, p.status AS plan_status,
                   p.trigger_spec, p.trigger_parse_status, p.trigger_summary, p.expires_at,
                   p.suggested_execution_payload, p.armed_execution_payload, p.semantic_key,
                   p.reason_text,
                   i.title, i.direction, i.item_kind, i.size_text,
                   i.suggested_price_low_cents, i.suggested_price_high_cents,
                   i.status AS item_status, i.raw_payload
              FROM recommendation_action_plans p
              JOIN recommendation_items i ON i.id = p.item_id
             WHERE p.id = %s
            """,
            (plan_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _build_plan_frozen_payload(plan: dict) -> tuple[dict | None, str | None]:
    """从 plan + item 构造 frozen_payload(market_id/token_id/price/size/order_type/...)。
    返回 (payload, error_msg)。
    - price 单位:美元(0~1),与 buy_order/sell_order 一致
    - size:估算的 share 数(>0)
    """
    sugg = plan.get("suggested_execution_payload") or {}
    direction = sugg.get("side") or plan.get("direction")
    if not direction:
        return None, "缺少方向(side)"
    # warning/复盘 等 item 的 title 是 'up_to:79220' 等合成键, 必须靠 AI 在 plan 内提供 target_question
    target_question = (sugg.get("target_question") or "").strip() or plan["title"]
    try:
        market_snapshot = _resolve_recommendation_market_snapshot(target_question, direction)
    except ValueError as exc:
        msg = str(exc)
        if "未找到 recommendation 对应的市场" in msg and not (sugg.get("target_question") or "").strip():
            return None, (
                f"无法定位目标市场 '{target_question}'。warning/复盘 类提案需要 AI 在 action_plan 中显式给出 "
                "target_question(目标市场 question 全文)。请等待下一轮 AI 分析重新生成此类提案,"
                "或改为手动执行。"
            )
        return None, msg
    market_id = market_snapshot.get("market_id")
    token_id = market_snapshot.get("token_id")
    if not market_id or not token_id:
        return None, "无法解析 market_id/token_id"

    # AI plan 的 price_cents 是美分(0~100);_recommendation_default_price 返回美元(0~1)
    raw_cents = sugg.get("price_cents")
    price_dollars: float | None = None
    try:
        if raw_cents is not None:
            price_dollars = float(raw_cents) / 100.0
    except (TypeError, ValueError):
        price_dollars = None
    if price_dollars is None:
        price_dollars = _recommendation_default_price(
            plan["action_type"], plan["suggested_price_low_cents"], plan["suggested_price_high_cents"],
        )
    if price_dollars is None or price_dollars <= 0:
        return None, "缺少建议价格"

    size_text = sugg.get("size_text") or plan.get("size_text") or ""
    size = _resolve_plan_size_shares(plan, size_text, price_dollars, token_id)
    if not size or size <= 0:
        if re.search(r"全部|全平|all\b|清仓", str(size_text), re.IGNORECASE):
            return None, (
                f"无法估算下单 size:size_text='{size_text}' 表示全部仓位,但当前账户未持有该 token,"
                "且同 item 内也没有可继承 size 的 buy plan。请等买入 plan 成交后再启用此卖出 plan。"
            )
        return None, f"无法估算下单 size(size_text='{size_text}')"

    return {
        "market_id": market_id,
        "token_id": token_id,
        "price": price_dollars,
        "size": size,
        "limit_price": price_dollars,
        "size_shares": size,
        "direction": direction,
        "action_type": plan["action_type"],
        "order_type": str(sugg.get("order_type") or "GTC").upper(),
        "plan_id": int(plan["plan_id"]),
        "item_id": int(plan["item_id"]),
    }, None


def _normalize_ai_pending_price_spec(raw: object, plan: dict) -> dict:
    spec = raw if isinstance(raw, dict) else {}
    ptype = str(spec.get("type") or "").strip().lower()
    if ptype in _MPO_VALID_PRICE_TYPES:
        if ptype == "absolute":
            value = spec.get("value")
            if value is None:
                raise ValueError("price_spec.absolute 需要 value")
            return {"type": "absolute", "value": float(value)}
        if ptype == "market":
            return {"type": "market", "offset": float(spec.get("offset") or 0.0)}
        return {"type": "cost_pct", "value": float(spec.get("value") or 0.0)}

    sugg = plan.get("suggested_execution_payload") or {}
    raw_cents = spec.get("price_cents") or sugg.get("price_cents")
    if raw_cents is None:
        raw_cents = _recommendation_default_price(
            plan["action_type"],
            plan.get("suggested_price_low_cents"),
            plan.get("suggested_price_high_cents"),
        )
        if raw_cents is not None:
            return {"type": "absolute", "value": float(raw_cents)}
    if raw_cents is None:
        raise ValueError("pending_order 缺少 price_spec")
    return {"type": "absolute", "value": float(raw_cents) / 100.0}


def _normalize_ai_pending_size_spec(raw: object, plan: dict) -> dict:
    spec = raw if isinstance(raw, dict) else {}
    stype = str(spec.get("type") or "").strip().lower()
    if stype in _MPO_VALID_SIZE_TYPES:
        value = spec.get("value")
        if value is None:
            raise ValueError("size_spec 需要 value")
        return {"type": stype, "value": float(value)}

    # 兼容旧 recommendation size_spec.mode,转换为 manual_pending_orders 的 size_spec.type。
    sugg = plan.get("suggested_execution_payload") or {}
    legacy = spec if str(spec.get("mode") or "").strip() else (sugg.get("size_spec") if isinstance(sugg.get("size_spec"), dict) else {})
    mode = str((legacy or {}).get("mode") or "").strip().lower()
    value = (legacy or {}).get("value")
    if value is None:
        raise ValueError("pending_order 缺少可转换的 size_spec.value")
    if mode == "amount_usdc":
        return {"type": "usdc", "value": float(value)}
    if mode == "shares":
        return {"type": "shares", "value": float(value)}
    if mode == "portion_cash":
        return {"type": "pct_balance", "value": float(value)}
    if mode == "portion_position":
        return {"type": "pct_position", "value": float(value)}
    raise ValueError("pending_order 缺少可转换的 size_spec")


def _parse_ai_pending_expires_at(raw: object, expires_hours: object) -> datetime:
    if raw not in (None, ""):
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET_TIMEZONE)
        return dt.astimezone(timezone.utc)
    try:
        hours = float(expires_hours or 24)
    except (TypeError, ValueError):
        raise ValueError("expires_hours 必须是数字") from None
    if hours <= 0 or hours > 720:
        raise ValueError("expires_hours 必须在 (0,720]")
    return datetime.now(timezone.utc) + timedelta(hours=hours)


def _convert_recommendation_plan_to_manual_pending_item(
    plan: dict,
    *,
    index: int,
    intent_tier_map: dict[str, dict] | None = None,
) -> dict:
    sugg = plan.get("suggested_execution_payload") or {}
    pending_order = sugg.get("pending_order")
    if not isinstance(pending_order, dict):
        raise ValueError(f"plan #{plan.get('plan_id')} 缺少 pending_order")

    action = str(plan.get("action_type") or "").strip().lower()
    if action not in {"buy", "sell"}:
        raise ValueError(f"plan #{plan.get('plan_id')} action_type={action} 不支持转入 manual pending")
    direction = str(sugg.get("side") or plan.get("direction") or "").strip()
    if not direction:
        raise ValueError(f"plan #{plan.get('plan_id')} 缺少方向(side)")
    target_question = (sugg.get("target_question") or "").strip() or str(plan.get("title") or "").strip()
    market_snapshot = _resolve_recommendation_market_snapshot(target_question, direction)
    market_id = str(market_snapshot.get("market_id") or "").strip()
    token_id = str(market_snapshot.get("token_id") or "").strip()
    if not market_id or not token_id:
        raise ValueError(f"plan #{plan.get('plan_id')} 无法解析 market_id/token_id")

    trigger_kind = str(pending_order.get("trigger_kind") or "").strip().lower()
    trigger_op = str(pending_order.get("trigger_op") or ">=").strip()
    if trigger_kind == "share_cost_pct" and pending_order.get("trigger_pct") is None and pending_order.get("trigger_threshold") is not None:
        trigger_pct = pending_order.get("trigger_threshold")
        trigger_threshold = None
    else:
        trigger_pct = pending_order.get("trigger_pct")
        trigger_threshold = pending_order.get("trigger_threshold")

    parent_index = pending_order.get("parent_index")
    if parent_index in ("", None):
        parent_index = None
    else:
        try:
            parent_index = int(parent_index)
        except (TypeError, ValueError):
            raise ValueError(f"plan #{plan.get('plan_id')} parent_index 必须是整数") from None
        if parent_index < 0 or parent_index >= index:
            raise ValueError(f"plan #{plan.get('plan_id')} parent_index={parent_index} 必须引用前序 plan")

    price_spec = _normalize_ai_pending_price_spec(pending_order.get("price_spec"), plan)
    size_spec = _normalize_ai_pending_size_spec(pending_order.get("size_spec"), plan)
    expires_at = _parse_ai_pending_expires_at(pending_order.get("expires_at"), pending_order.get("expires_hours"))
    notes = (
        pending_order.get("notes")
        or plan.get("reason_text")
        or f"AI recommendation #{plan.get('item_id')} plan #{plan.get('plan_id')}"
    )
    extra = {
        "profile": APP_PM_PROFILE,
        "source": "recommendation_pending_order",
        "recommendation_item_id": int(plan["item_id"]),
        "recommendation_plan_id": int(plan["plan_id"]),
        "recommendation_plan_ordinal": int(plan.get("ordinal") or index + 1),
        "plan_role": sugg.get("plan_role"),
    }
    review_source = {}
    if isinstance(sugg, dict):
        review_source.update({
            "post_entry_review_hours": sugg.get("post_entry_review_hours"),
            "post_entry_review_note": sugg.get("post_entry_review_note"),
        })
    if isinstance(pending_order, dict):
        review_source.update({
            "post_entry_review_hours": pending_order.get("post_entry_review_hours", review_source.get("post_entry_review_hours")),
            "post_entry_review_note": pending_order.get("post_entry_review_note", review_source.get("post_entry_review_note")),
        })
    _apply_post_entry_review_extra(extra, review_source)
    _apply_intent_tier_snapshot(extra, token_id, intent_tier_map)
    return {
        "action": action,
        "market_id": market_id,
        "token_id": token_id,
        "trigger_kind": trigger_kind,
        "trigger_op": trigger_op,
        "trigger_threshold": trigger_threshold,
        "trigger_pct": trigger_pct,
        "size_spec": size_spec,
        "price_spec": price_spec,
        "expires_at": expires_at,
        "notes": notes,
        "extra": extra,
        "parent_index": parent_index,
    }


@app.route('/api/recommendation_plans/<int:plan_id>/enable', methods=['POST'])
def api_recommendation_plan_enable(plan_id: int):
    """旧 recommendation action_plan 独立自动执行入口已下线。"""
    return jsonify({
        "error": "旧 recommendation action_plan 独立自动执行已下线；请使用 recommendation 的 pending_order 一键转入统一 pending 队列。",
        "code": "legacy_action_plan_disabled",
    }), 410


@app.route('/api/recommendations/<int:item_id>/enable_all_plans', methods=['POST'])
def api_recommendation_enable_all_plans(item_id: int):
    """旧 parsed plans 批量启用入口已下线。"""
    return jsonify({
        "error": "旧 parsed plans 批量启用已下线；请使用 pending_order 一键转入统一 pending 队列。",
        "code": "legacy_action_plan_disabled",
    }), 410


@app.route('/api/recommendations/<int:item_id>/queue_pending_plan', methods=['POST'])
def api_recommendation_queue_pending_plan(item_id: int):
    """把 AI 明确输出的 linked plan 转入 manual_pending_orders 统一队列。

    与 enable_all_plans 不同,这里要求每条 buy/sell action_plan 都带 pending_order,
    并一次性写入 manual_pending_orders.insert_plan,保留 parent_index 联动关系。
    """
    from services.recommendation_trigger import auto_trigger_db as atdb
    data = request.get_json(silent=True) or {}
    try:
        with get_cursor() as cur:
            cur.execute(
                """
                SELECT id, status, title
                  FROM recommendation_items
                 WHERE id = %s
                """,
                (int(item_id),),
            )
            item = cur.fetchone()
        if not item:
            return jsonify({"error": "recommendation item 不存在"}), 404

        item_status = str(item["status"] or "")
        feedback_result = None
        if item_status != "approved":
            if item_status not in {"pending", "read", "deferred", "order_failed", "cancel_failed"}:
                return jsonify({"error": f"item 当前状态 {item_status} 不允许转入 pending", "code": "item_not_queueable"}), 409
            feedback_result = _recommendation_db.submit_feedback(
                item_id=int(item_id),
                decision="execute",
                reason_tags=["queue_pending_plan"],
                feedback_text=str(data.get("feedback_text") or "批准并转入统一 pending 计划")[:500],
                allow_model_learning=bool(data.get("allow_model_learning", True)),
                raw_payload={"source": "queue_pending_plan", **data},
            )

        with get_cursor() as cur:
            cur.execute(
                """
                SELECT p.id AS plan_id, p.item_id, p.ordinal, p.action_type, p.status AS plan_status,
                       p.trigger_spec, p.trigger_parse_status, p.trigger_summary, p.expires_at,
                       p.suggested_execution_payload, p.armed_execution_payload, p.semantic_key,
                       p.reason_text,
                       i.title, i.direction, i.item_kind, i.size_text,
                       i.suggested_price_low_cents, i.suggested_price_high_cents,
                       i.status AS item_status, i.raw_payload
                  FROM recommendation_action_plans p
                  JOIN recommendation_items i ON i.id = p.item_id
                 WHERE p.item_id = %s
                   AND p.action_type IN ('buy','sell')
                    AND p.status IN ('proposed','armed','executing')
                  ORDER BY p.ordinal, p.id
                """,
                (int(item_id),),
            )
            plans = [dict(r) for r in cur.fetchall()]

        if not plans:
            return jsonify({"ok": False, "error": "没有 buy/sell action plans 可转入 pending", "feedback": feedback_result}), 400
        blocking = [p for p in plans if str(p.get("plan_status") or "") not in {"proposed"}]
        if blocking:
            return jsonify({
                "ok": False,
                "error": "存在非 proposed 的 action plan,为避免重复触发已取消转入",
                "blocking": [{"plan_id": p.get("plan_id"), "status": p.get("plan_status")} for p in blocking],
                "feedback": feedback_result,
            }), 409
        missing = [
            p.get("plan_id") for p in plans
            if not isinstance((p.get("suggested_execution_payload") or {}).get("pending_order"), dict)
        ]
        if missing:
            return jsonify({
                "ok": False,
                "error": "该 recommendation 仍是旧式独立 plan,缺少 pending_order 结构,不能安全转入统一 pending",
                "missing_plan_ids": missing,
                "feedback": feedback_result,
            }), 400

        parsed_items: list[dict] = []
        errors: list[dict] = []
        intent_tier_map = _build_current_intent_tier_map()
        for idx, plan in enumerate(plans):
            try:
                parsed_items.append(_convert_recommendation_plan_to_manual_pending_item(
                    plan,
                    index=idx,
                    intent_tier_map=intent_tier_map,
                ))
            except Exception as exc:  # noqa: BLE001
                errors.append({"plan_id": plan.get("plan_id"), "error": str(exc)})
        if errors:
            return jsonify({
                "ok": False,
                "error": "部分 plan 无法转换为 manual pending,已取消本次转入",
                "errors": errors,
                "feedback": feedback_result,
            }), 409

        rows = _mpo_insert_plan(
            parsed_items,
            requested_by=session.get('user') or 'dashboard',
        )
        try:
            disarmed_count = atdb.cascade_cancel_plans_for_item(
                item_id=int(item_id),
                reason="queued_to_manual_pending",
            )
        except Exception:
            for row in rows:
                try:
                    _mpo_cancel(int(row["id"]))
                except Exception:
                    logger.exception("rollback queued manual pending failed: %s", row)
            raise
        return jsonify({
            "ok": True,
            "item_id": int(item_id),
            "feedback": feedback_result,
            "queued": True,
            "plan_id": rows[0].get("plan_id") if rows else None,
            "items": rows,
            "disarmed_recommendation_plan_count": disarmed_count,
            "note": "已转入 Positions / Orders 的统一 pending 队列；原 recommendation action plans 已关闭,避免重复触发。",
        })
    except RecommendationGateError as ge:
        return jsonify({"error": str(ge), "code": ge.code}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        logger.exception("api_recommendation_queue_pending_plan failed item_id=%s", item_id)
        return jsonify({"error": str(e)}), 500


@app.route('/api/recommendation_plans/<int:plan_id>/disable', methods=['POST'])
def api_recommendation_plan_disable(plan_id: int):
    from services.recommendation_trigger import auto_trigger_db as atdb
    data = request.get_json(silent=True) or {}
    reason = str(data.get("reason") or "user_disabled")[:200]
    try:
        row = atdb.disable_auto_execute_plan(plan_id=int(plan_id), reason=reason)
        return jsonify({"ok": True, "plan": row})
    except atdb.AutoTriggerClaimError as exc:
        return jsonify({"error": str(exc), "code": exc.code}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/recommendation_plans/stale_executing', methods=['GET'])
def api_recommendation_plans_stale_executing():
    """列出卡在 executing 超过 timeout_minutes 分钟的 plan,用于 dashboard 巡检面板。"""
    from services.recommendation_trigger import auto_trigger_db as atdb
    try:
        timeout_minutes = int(request.args.get("timeout_minutes", "5"))
    except (TypeError, ValueError):
        timeout_minutes = 5
    timeout_minutes = max(1, min(240, timeout_minutes))
    try:
        rows = atdb.list_stale_executing_plans(timeout_minutes=timeout_minutes)
        for r in rows:
            for k in ("updated_at",):
                v = r.get(k)
                if isinstance(v, datetime):
                    r[k] = v.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({"timeout_minutes": timeout_minutes, "rows": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/recommendation_plans/<int:plan_id>/repair_executing', methods=['POST'])
def api_recommendation_plan_repair_executing(plan_id: int):
    """人工把卡 executing 的 plan 释放到 fired/disarmed/armed。
    body: { target_status: 'fired'|'disarmed'|'armed', reason: str, order_id?: str }
    """
    from services.recommendation_trigger import auto_trigger_db as atdb
    data = request.get_json(silent=True) or {}
    target = str(data.get("target_status") or "").strip().lower()
    if target not in {"fired", "disarmed", "armed"}:
        return jsonify({"error": "target_status 必须 in fired/disarmed/armed"}), 400
    reason = str(data.get("reason") or "")[:200]
    if not reason:
        return jsonify({"error": "reason 必填"}), 400
    order_id = (data.get("order_id") or "").strip() or None
    if target == "fired" and not order_id:
        return jsonify({"error": "target=fired 必须给 order_id"}), 400
    operator = (session.get('user') or session.get('username') or 'dashboard')[:64]
    try:
        row = atdb.repair_stale_executing_plan(
            plan_id=int(plan_id),
            target_status=target,
            reason=reason,
            released_by=operator,
            order_id=order_id,
        )
        return jsonify({"ok": True, "plan": row})
    except atdb.AutoTriggerClaimError as exc:
        return jsonify({"error": str(exc), "code": exc.code}), 409
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route('/api/model_change_proposals')
def api_model_change_proposals():
    try:
        proposals = _recommendation_db.list_model_change_proposals(limit=20)
        shadow_rows = _recommendation_db.list_model_shadow_evals(limit=100)
        shadow_by_proposal: dict[int, list[dict]] = {}
        for row in shadow_rows:
            formatted = _format_shadow_eval_row(row)
            proposal_id = formatted.get("proposal_id")
            if proposal_id is None:
                continue
            shadow_by_proposal.setdefault(int(proposal_id), []).append(formatted)
        result = []
        for proposal in proposals:
            created_at = proposal["created_at"]
            approved_at = proposal.get("approved_at")
            rejected_at = proposal.get("rejected_at")
            result.append({
                "id": proposal["id"],
                "status": proposal["status"],
                "proposal_type": proposal["proposal_type"],
                "target_scope": proposal["target_scope"],
                "title": proposal["title"],
                "rationale": proposal["rationale"],
                "change_payload": proposal["change_payload"] or {},
                "evidence_payload": proposal["evidence_payload"] or {},
                "proposed_by": proposal["proposed_by"],
                "approved_by": proposal["approved_by"],
                "decision_notes": proposal["decision_notes"],
                "shadow_evals": shadow_by_proposal.get(int(proposal["id"]), []),
                "created_at_utc8": created_at.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S") if isinstance(created_at, datetime) else str(created_at or ""),
                "approved_at_utc8": approved_at.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S") if isinstance(approved_at, datetime) else None,
                "rejected_at_utc8": rejected_at.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S") if isinstance(rejected_at, datetime) else None,
            })
        return jsonify({"items": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/model_change_proposals/autogenerate', methods=['POST'])
def api_model_change_proposals_autogenerate():
    try:
        memory_context = _recommendation_db.build_memory_context(asset="btc")
        candidates = _build_auto_iteration_proposals(memory_context)
        existing_keys = {
            (
                str(item.get("proposal_type") or ""),
                str(item.get("target_scope") or ""),
                str(item["title"]),
            )
            for item in _recommendation_db.list_model_change_proposals(limit=200)
            if str(item.get("status") or "") in {"proposed", "approved"}
        }
        created: list[dict] = []
        created_shadow_evals: list[dict] = []
        shadow_eval_errors: list[dict] = []
        for candidate in candidates:
            cand_key = (
                str(candidate.get("proposal_type") or ""),
                str(candidate.get("target_scope") or ""),
                str(candidate["title"]),
            )
            if cand_key in existing_keys:
                continue
            created_proposal = _recommendation_db.create_model_change_proposal(**candidate)
            created.append(created_proposal)
            proposal_row = _recommendation_db.get_model_change_proposal(int(created_proposal["id"]))
            if not proposal_row:
                continue
            try:
                created_shadow_evals.append(_create_shadow_eval_for_proposal(proposal_row, memory_context))
            except Exception as eval_err:
                # 单条 shadow eval 失败不应该让整个 autogenerate 接口 500，导致 proposal 已落库但响应是错误。
                logger.exception("shadow_eval auto-create failed: proposal_id=%s", proposal_row.get("id"))
                shadow_eval_errors.append({
                    "proposal_id": proposal_row.get("id"),
                    "error": str(eval_err),
                })
        return jsonify({
            "created_count": len(created),
            "shadow_eval_count": len(created_shadow_evals),
            "shadow_eval_errors": shadow_eval_errors,
            "created": [
                {
                    "id": item["id"],
                    "status": item["status"],
                    "title": item["title"],
                }
                for item in created
            ],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/model_change_proposals/<int:proposal_id>/shadow_eval', methods=['POST'])
def api_model_change_proposal_shadow_eval(proposal_id: int):
    try:
        proposal = _recommendation_db.get_model_change_proposal(proposal_id)
        if not proposal:
            return jsonify({"error": "proposal not found"}), 404
        result = _create_shadow_eval_for_proposal(proposal)
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/model_change_proposals/<int:proposal_id>/review', methods=['POST'])
def api_model_change_proposal_review(proposal_id: int):
    try:
        body = request.get_json(silent=True) or {}
        decision = str(body.get("decision") or "").strip().lower()
        decision_notes = str(body.get("decision_notes") or "").strip()
        result = _recommendation_db.review_model_change_proposal(
            proposal_id=proposal_id,
            decision=decision,
            reviewer="dashboard",
            decision_notes=decision_notes,
        )
        approved_at = result.get("approved_at")
        rejected_at = result.get("rejected_at")
        return jsonify({
            "id": result["id"],
            "status": result["status"],
            "title": result["title"],
            "approved_by": result["approved_by"],
            "decision_notes": result["decision_notes"],
            "approved_at_utc8": approved_at.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S") if isinstance(approved_at, datetime) else None,
            "rejected_at_utc8": rejected_at.astimezone(UTC8_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S") if isinstance(rejected_at, datetime) else None,
        })
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
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
