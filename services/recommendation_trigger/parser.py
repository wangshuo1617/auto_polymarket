"""recommendation trigger 解析器。

把 recommendation_items 的 trigger_spec(JSONB,AI 输出) 或 fallback 解析中文 trigger_condition
变成内部统一的 ParsedTrigger 结构,供 engine 评估。

设计原则:
  - 解析失败一律不自动触发(`unparseable`),只走人工执行路径,fail-closed。
  - 不接受任何"无阈值/无方向"的兜底,避免误触发。
  - 所有阈值类型必须能映射到现有 watcher 的实时数据源(BTC Binance aggTrade / Polymarket bid/ask)。
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)
ET_TIMEZONE = ZoneInfo("America/New_York")

# 解析状态常量,与 DB 列 trigger_parse_status 对应。
PARSE_STATUS_PARSED = "parsed"
PARSE_STATUS_UNPARSEABLE = "unparseable"
PARSE_STATUS_MANUAL_ONLY = "manual_only"  # 显式不自动触发(如 review/alert)

# 触发类型枚举。engine 按 type 分发到对应价格源；BTC 阈值用 1m K 线收盘价确认。
TRIGGER_TYPE_BTC_PRICE = "btc_price_threshold"
TRIGGER_TYPE_POLY_BID = "poly_bid_threshold"
TRIGGER_TYPE_POLY_ASK = "poly_ask_threshold"
TRIGGER_TYPE_IMMEDIATE = "immediate"  # 立即触发(用于"挂单等待"类的 limit order)

_VALID_TYPES = {
    TRIGGER_TYPE_BTC_PRICE,
    TRIGGER_TYPE_POLY_BID,
    TRIGGER_TYPE_POLY_ASK,
    TRIGGER_TYPE_IMMEDIATE,
}
_VALID_OPERATORS = {">=", "<=", ">", "<"}

# 安全默认值
DEFAULT_MIN_DWELL_SECONDS = 5
DEFAULT_COOLDOWN_SECONDS = 30
DEFAULT_MAX_FIRES = 1


@dataclass
class ParsedTrigger:
    """engine 内部统一的触发条件表示。"""
    type: str
    operator: str  # 对 immediate 类型无意义,占位 "=="
    value: float   # 对 immediate 类型无意义,占位 0.0
    asset_token_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    min_dwell_seconds: int = DEFAULT_MIN_DWELL_SECONDS
    cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS
    max_fires: int = DEFAULT_MAX_FIRES
    source: str = "ai"  # ai / heuristic
    raw: dict[str, Any] = field(default_factory=dict)

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "operator": self.operator,
            "value": self.value,
            "asset_token_id": self.asset_token_id,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "min_dwell_seconds": self.min_dwell_seconds,
            "cooldown_seconds": self.cooldown_seconds,
            "max_fires": self.max_fires,
            "source": self.source,
            "raw": self.raw,
        }


@dataclass
class ParseResult:
    status: str  # PARSE_STATUS_*
    trigger: Optional[ParsedTrigger] = None
    reason: str = ""

    @property
    def parsed(self) -> bool:
        return self.status == PARSE_STATUS_PARSED and self.trigger is not None


# ---------------------- 主入口 ----------------------

# 不可自动执行的 action_type(纯 audit / 决策记录)
_NON_EXECUTABLE_ACTIONS = {"review", "alert"}


def parse_trigger(item: dict[str, Any]) -> ParseResult:
    """解析 recommendation item -> ParsedTrigger。

    item 至少应包含: action_type, item_kind, trigger_condition, trigger_spec(可选),
                   asset_token_id(可选,从 raw_payload 衍生)。
    """
    action_type = str(item.get("action_type") or "").strip().lower()
    if action_type in _NON_EXECUTABLE_ACTIONS:
        # 阶段4 之前:整个 item 拒绝。阶段4 起,每个 plan 自带 buy/sell/cancel,
        # 这条分支仅在 plan_id=NULL 的"item 维度兼容调用"中触发。
        return ParseResult(status=PARSE_STATUS_MANUAL_ONLY, reason="action_type 不可执行")
    if action_type not in {"buy", "sell", "cancel"}:
        return ParseResult(status=PARSE_STATUS_MANUAL_ONLY, reason=f"未知 action_type={action_type}")

    # 1. 优先吃 AI 输出的结构化字段
    raw_spec = item.get("trigger_spec")
    if isinstance(raw_spec, dict) and raw_spec:
        result = _from_structured(raw_spec, item)
        if result is not None:
            return result

    # 2. fallback: 中文 trigger_condition 启发式
    text = str(item.get("trigger_condition") or "").strip()
    if text:
        result = _from_text_heuristic(text, item)
        if result is not None:
            return result

    return ParseResult(
        status=PARSE_STATUS_UNPARSEABLE,
        reason="缺少结构化 trigger_spec,且文本无法识别阈值",
    )


# ---------------------- 结构化解析 ----------------------

def _from_structured(spec: dict[str, Any], item: dict[str, Any]) -> Optional[ParseResult]:
    """从 AI 输出的 trigger_spec(JSONB) 构造 ParsedTrigger。

    支持两种 key 风格(中英文混排,因为 AI prompt 是中文):
      - 英文: type/operator/value/expires_at/min_dwell_seconds/...
      - 中文: 类型/比较/阈值/过期/...
    任何关键字段缺失或非法 → 返回 UNPARSEABLE(而不是 None),阻止 fallback 启发式覆盖
    AI 显式给出但写错的 spec(避免 silently 走文本兜底)。
    """
    ttype = _pick(spec, ["type", "类型"])
    operator = _pick(spec, ["operator", "比较", "方向"]) or ">="
    value = _pick(spec, ["value", "阈值", "价格"])
    expires_at_raw = _pick(spec, ["expires_at", "过期"])
    immediate_flag = _pick(spec, ["immediate", "立即"])

    # 立即触发(通常用于"挂单等待"类 — 一旦 approve 就立即下 limit 单)
    if (isinstance(immediate_flag, bool) and immediate_flag) or ttype == TRIGGER_TYPE_IMMEDIATE:
        ttype = TRIGGER_TYPE_IMMEDIATE
        operator = "=="
        value = 0.0

    if ttype not in _VALID_TYPES:
        return ParseResult(
            status=PARSE_STATUS_UNPARSEABLE,
            reason=f"trigger_spec.type 非法: {ttype!r}",
        )

    operator = _normalize_operator(operator)
    if operator not in _VALID_OPERATORS:
        return ParseResult(
            status=PARSE_STATUS_UNPARSEABLE,
            reason=f"trigger_spec.operator 非法: {operator!r}",
        )

    if ttype == TRIGGER_TYPE_IMMEDIATE:
        numeric_value = 0.0
    else:
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return ParseResult(
                status=PARSE_STATUS_UNPARSEABLE,
                reason=f"trigger_spec.value 非数字: {value!r}",
            )
        if not math.isfinite(numeric_value) or numeric_value < 0:
            return ParseResult(
                status=PARSE_STATUS_UNPARSEABLE,
                reason=f"trigger_spec.value 非有限非负数: {numeric_value}",
            )

    asset_token_id = _pick(spec, ["asset_token_id", "token_id"]) or item.get("asset_token_id")
    if ttype in {TRIGGER_TYPE_POLY_BID, TRIGGER_TYPE_POLY_ASK} and not asset_token_id:
        return ParseResult(
            status=PARSE_STATUS_UNPARSEABLE,
            reason=f"poly_* 触发缺少 asset_token_id",
        )

    try:
        expires_at = _parse_expires_at(expires_at_raw)
    except ValueError as exc:
        return ParseResult(
            status=PARSE_STATUS_UNPARSEABLE,
            reason=f"expires_at 无法解析: {exc}",
        )

    trigger = ParsedTrigger(
        type=ttype,
        operator=operator,
        value=numeric_value,
        asset_token_id=str(asset_token_id) if asset_token_id else None,
        expires_at=expires_at,
        min_dwell_seconds=_safe_int(spec.get("min_dwell_seconds"), DEFAULT_MIN_DWELL_SECONDS, lo=0, hi=300),
        cooldown_seconds=_safe_int(spec.get("cooldown_seconds"), DEFAULT_COOLDOWN_SECONDS, lo=0, hi=3600),
        max_fires=_safe_int(spec.get("max_fires"), DEFAULT_MAX_FIRES, lo=1, hi=10),
        source="ai",
        raw=spec,
    )
    return ParseResult(status=PARSE_STATUS_PARSED, trigger=trigger)


# ---------------------- 中文文本启发式 ----------------------

# BTC 阈值: "BTC 跌破 65000" / "BTC 上破 8万" / "BTC 价格 >= 70000" / "BTC ≥ $73,500" / "BTC > $80k"
# 单位规范化: "万"/"w" -> *10000; "k"/"千" -> *1000
# 数字部分允许 $ 前缀和千分位逗号
_BTC_THRESHOLD_RE = re.compile(
    r"BTC[^0-9]{0,16}"
    r"(?P<dir>跌破|跌至|跌到|低于|下破|向下击穿|回调至|回调到|回踩至|回踩到|↓|<=|<|≤|"
    r"涨破|涨至|涨到|高于|上破|向上突破|突破|反弹至|反弹到|↑|>=|>|≥)"
    r"[^0-9$]{0,8}\$?"
    r"(?P<val>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>万|w|W|k|K|千)?",
    re.IGNORECASE,
)
# 不要触发的关键字(只是描述性,不是真触发条件)
_NON_TRIGGER_HINTS = {
    "挂单等待", "默认持有", "持有到期", "保留", "无减仓必要",
    "彩票", "保险", "止盈止损", "止损规则",
}


def _from_text_heuristic(text: str, item: dict[str, Any]) -> Optional[ParseResult]:
    """从中文 trigger_condition 文本启发式抽取 BTC 阈值。

    覆盖率有限,但任何模糊文本都返回 UNPARSEABLE(safe default)。
    poly bid/ask 类阈值靠 AI 显式 trigger_spec,不做文本启发(歧义太大)。
    """
    # 先排除明显是描述性的文本
    for hint in _NON_TRIGGER_HINTS:
        if hint in text:
            return ParseResult(
                status=PARSE_STATUS_UNPARSEABLE,
                reason=f"文本是描述性 ({hint}),不是机器可评估阈值",
            )

    match = _BTC_THRESHOLD_RE.search(text)
    if not match:
        return ParseResult(
            status=PARSE_STATUS_UNPARSEABLE,
            reason="BTC 阈值正则未命中",
        )

    direction = match.group("dir")
    raw_val_str = match.group("val").replace(",", "")
    raw_val = float(raw_val_str)
    unit = (match.group("unit") or "").lower()
    if unit in {"万", "w"}:
        raw_val *= 10000.0
    elif unit in {"k", "千"}:
        raw_val *= 1000.0

    if not math.isfinite(raw_val) or raw_val <= 0:
        return ParseResult(status=PARSE_STATUS_UNPARSEABLE, reason="解析出的 BTC 阈值非法")

    # 启发式 sanity 范围(BTC 应在合理价格区间)
    if raw_val < 1000 or raw_val > 1_000_000:
        return ParseResult(
            status=PARSE_STATUS_UNPARSEABLE,
            reason=f"BTC 阈值 {raw_val} 超出合理范围,可能是误识别",
        )

    down_keywords = ("跌", "低", "下", "↓", "<", "≤", "回调", "回踩")
    up_keywords = ("涨", "上", "突破", "高", "↑", ">", "≥", "反弹")
    if any(k in direction for k in down_keywords):
        operator = "<="
    elif any(k in direction for k in up_keywords):
        operator = ">="
    else:
        return ParseResult(status=PARSE_STATUS_UNPARSEABLE, reason="无法识别比较方向")

    trigger = ParsedTrigger(
        type=TRIGGER_TYPE_BTC_PRICE,
        operator=operator,
        value=raw_val,
        source="heuristic",
        raw={"text": text, "matched": match.group(0)},
    )
    return ParseResult(status=PARSE_STATUS_PARSED, trigger=trigger)


# ---------------------- 工具 ----------------------

def _pick(d: dict[str, Any], keys: list[str]) -> Any:
    for k in keys:
        if k in d and d[k] is not None and d[k] != "":
            return d[k]
    return None


def _normalize_operator(op: Any) -> str:
    if op is None:
        return ">="
    s = str(op).strip()
    mapping = {
        "上破": ">=", "突破": ">=", "高于": ">=", "上": ">=", ">=": ">=", ">": ">",
        "跌破": "<=", "低于": "<=", "下破": "<=", "下": "<=", "<=": "<=", "<": "<",
        "==": ">=",
    }
    return mapping.get(s, s)


def _parse_expires_at(value: Any) -> Optional[datetime]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=ET_TIMEZONE)
    s = str(value).strip()
    # 支持 "2026-05-31" / "2026-05-31T23:59:59Z" / "2026-05-31T23:59:59+08:00"；naive 按 ET。
    fmts = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(s.replace("Z", "+0000") if fmt.endswith("%z") else s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ET_TIMEZONE)
            return dt
        except ValueError:
            continue
    logger.warning("trigger expires_at 格式无法解析: %r", s)
    raise ValueError(f"expires_at 无法识别: {s!r}")


def _safe_int(value: Any, default: int, *, lo: int, hi: int) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n
