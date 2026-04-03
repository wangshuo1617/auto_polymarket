"""5m Trade 策略参数注册表。

所有策略参数的 **唯一定义来源**。argparse、create_trader_from_args、startup_params、
strategy_signature、Dashboard 参数面板等均从此注册表自动派生，新增参数只需在此添加一条记录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# UI 分组名常量
# ---------------------------------------------------------------------------
GRP_ENTRY = "入场控制"
GRP_TPSL = "TPSL 平仓控制"
GRP_RISK = "风险仓位管理"
GRP_DIR_CONFIRM = "方向确认风控"
GRP_ENDGAME = "终盘风控"
GRP_SYSTEM = "系统"

# 分组显示顺序
GROUP_ORDER: list[str] = [
    GRP_ENTRY,
    GRP_TPSL,
    GRP_RISK,
    GRP_DIR_CONFIRM,
    GRP_ENDGAME,
    GRP_SYSTEM,
]


@dataclass(frozen=True)
class ParamDef:
    """一个策略参数的完整元数据。"""

    key: str
    """Python 标准名（同时也是 startup_params / 前端 JSON key），如 ``"entry_minute"``。"""

    param_type: str
    """``"int"`` | ``"float"`` | ``"str"`` | ``"bool"``。"""

    default: Any
    """argparse 默认值。"""

    description: str
    """中文描述，用于 CLI --help 和前端参数弹窗。"""

    group: str
    """UI 分组名，取值为 ``GRP_*`` 常量。"""

    shell_var: str
    """Shell 环境变量名，如 ``"ENTRY_MINUTE"``。"""

    sig_key: str
    """策略签名缩写，如 ``"m"``。空字符串表示不纳入签名。"""

    cli_flag: str | None = None
    """CLI flag（不含 ``--``）。``None`` 时自动从 ``key.replace('_', '-')`` 生成。"""

    constructor_name: str | None = None
    """Trader 构造函数形参名。``None`` 时默认等于 ``key``。"""

    choices: list[Any] | None = None
    """argparse choices 约束。"""

    bool_inverted: bool = False
    """仅 ``param_type="bool"`` 有效。``True`` 表示 CLI 使用 ``--disable-xxx``
    且 argparse 存储 ``disable_xxx=True/False``，传给构造函数时需取反。"""

    startup_key: str | None = None
    """写入 ``startup_params`` (DB ``params_json``) 时使用的 key。
    ``None`` 时默认等于 ``key``。主要用于布尔反转参数
    （如 key="confidence_boost" 但构造函数中是 confidence_boost_enabled）。"""

    # 内部 helper
    # ------------------------------------------------------------------

    @property
    def cli_flag_resolved(self) -> str:
        """解析后的 CLI flag 名（不含 ``--``）。"""
        return self.cli_flag or self.key.replace("_", "-")

    @property
    def constructor_name_resolved(self) -> str:
        """解析后的构造函数形参名。"""
        return self.constructor_name or self.key

    @property
    def startup_key_resolved(self) -> str:
        """写入 startup_params 的 key。"""
        return self.startup_key or self.key

    @property
    def args_attr(self) -> str:
        """argparse 解析后的属性名。

        普通参数：``cli_flag_resolved.replace('-', '_')``
        反转布尔：``"disable_" + key``（argparse dest）
        """
        if self.param_type == "bool" and self.bool_inverted:
            return "disable_" + self.key.replace("-", "_")
        if self.param_type == "bool":
            return self.key  # enable-style dest
        return self.cli_flag_resolved.replace("-", "_")

    def resolve_value(self, args: Any) -> Any:
        """从 argparse Namespace 中提取最终值（处理布尔反转）。"""
        raw = getattr(args, self.args_attr)
        if self.param_type == "bool" and self.bool_inverted:
            return not raw
        return raw


# ---------------------------------------------------------------------------
# 参数注册表
# 注意：列表顺序决定 argparse flag 顺序和策略签名（signature）顺序，
#       需与历史版本保持一致。UI 显示顺序由 DISPLAY_ORDER 按 group 重排。
# ---------------------------------------------------------------------------

PARAM_REGISTRY: list[ParamDef] = [
    # ==================== 入场控制（第一批） ====================
    ParamDef(
        key="entry_minute",
        param_type="int",
        default=3,
        description="入场决策分钟（1-4）",
        group=GRP_ENTRY,
        shell_var="ENTRY_MINUTE",
        sig_key="m",
        constructor_name="entry_decision_minute",
        choices=[1, 2, 3, 4],
    ),
    ParamDef(
        key="entry_preclose_sec",
        param_type="int",
        default=5,
        description="入场分钟收盘前秒数",
        group=GRP_ENTRY,
        shell_var="ENTRY_PRECLOSE_SEC",
        sig_key="pre",
        constructor_name="entry_preclose_seconds",
    ),
    ParamDef(
        key="min_direction_diff",
        param_type="float",
        default=10.0,
        description="最小方向差值（BTC vs 开盘价）",
        group=GRP_ENTRY,
        shell_var="MIN_DIRECTION_DIFF",
        sig_key="diff",
    ),
    ParamDef(
        key="max_entry_price",
        param_type="float",
        default=0.80,
        description="最大允许入场价格",
        group=GRP_ENTRY,
        shell_var="MAX_ENTRY_PRICE",
        sig_key="max",
    ),
    ParamDef(
        key="stake_usd",
        param_type="float",
        default=5.0,
        description="单笔基础仓位（USDC）",
        group=GRP_ENTRY,
        shell_var="STAKE_USD",
        sig_key="stake",
    ),

    # ==================== TPSL 平仓控制 ====================
    ParamDef(
        key="min_hold_before_close_sec",
        param_type="int",
        default=5,
        description="最短持仓保护秒数",
        group=GRP_TPSL,
        shell_var="MIN_HOLD_BEFORE_CLOSE_SEC",
        sig_key="hold",
    ),
    ParamDef(
        key="tp_price_cap",
        param_type="float",
        default=0.95,
        description="TP 价格上限",
        group=GRP_TPSL,
        shell_var="TP_PRICE_CAP",
        sig_key="tp_cap",
    ),
    ParamDef(
        key="tp_value_cap",
        param_type="float",
        default=0.15,
        description="TP 收益值上限",
        group=GRP_TPSL,
        shell_var="TP_VALUE_CAP",
        sig_key="tp_val_cap",
    ),
    ParamDef(
        key="sl_to_tp_ratio",
        param_type="float",
        default=4.0 / 3.0,
        description="SL/TP 比例",
        group=GRP_TPSL,
        shell_var="SL_TO_TP_RATIO",
        sig_key="sl_ratio",
    ),
    # 兼容保留参数（不在前端/shell 中暴露，但 argparse 和构造函数中需要）
    ParamDef(
        key="take_profit_spread",
        param_type="float",
        default=0.15,
        description="兼容保留参数：当前使用动态止盈",
        group=GRP_TPSL,
        shell_var="",
        sig_key="",
    ),
    ParamDef(
        key="stop_loss_spread",
        param_type="float",
        default=-0.20,
        description="兼容保留参数：当前使用动态止损",
        group=GRP_TPSL,
        shell_var="",
        sig_key="",
    ),

    # ==================== 入场控制（第二批） ====================
    ParamDef(
        key="max_btc_cross_count",
        param_type="int",
        default=5,
        description="BTC 跨越开盘价次数上限",
        group=GRP_ENTRY,
        shell_var="MAX_BTC_CROSS_COUNT",
        sig_key="cross",
    ),
    ParamDef(
        key="min_entry_updown_diff",
        param_type="float",
        default=0.30,
        description="UP/DOWN token 最小价差",
        group=GRP_ENTRY,
        shell_var="MIN_ENTRY_UPDOWN_DIFF",
        sig_key="ud_diff",
    ),
    ParamDef(
        key="max_avg_btc_delta",
        param_type="float",
        default=3.0,
        description="ATR 波动率阈值",
        group=GRP_ENTRY,
        shell_var="MAX_AVG_BTC_DELTA",
        sig_key="atr",
    ),
    ParamDef(
        key="minute_consistency",
        param_type="str",
        default="1,2,3",
        description="分钟一致性检查列表",
        group=GRP_ENTRY,
        shell_var="MINUTE_CONSISTENCY",
        sig_key="mc",
    ),
    ParamDef(
        key="exit_mode",
        param_type="str",
        default="tpsl",
        description="平仓模式：hold / tpsl",
        group=GRP_ENTRY,
        shell_var="EXIT_MODE",
        sig_key="exit",
        choices=["tpsl", "hold"],
    ),

    # ==================== 系统 ====================
    ParamDef(
        key="report_interval_sec",
        param_type="int",
        default=3600,
        description="报告输出间隔（秒）",
        group=GRP_SYSTEM,
        shell_var="REPORT_INTERVAL_SEC",
        sig_key="",
    ),
    ParamDef(
        key="toxic_utc_hours",
        param_type="str",
        default="16,19,20",
        description="跳过交易的 UTC 小时列表",
        group=GRP_ENTRY,
        shell_var="TOXIC_UTC_HOURS",
        sig_key="",
    ),

    # ==================== 风险仓位管理 ====================
    ParamDef(
        key="enable_risk_sizing",
        param_type="bool",
        default=True,
        description="是否启用动态仓位",
        group=GRP_RISK,
        shell_var="ENABLE_RISK_SIZING",
        sig_key="risk",
    ),
    ParamDef(
        key="risk_min_stake_ratio",
        param_type="float",
        default=0.20,
        description="动态仓位最小倍率",
        group=GRP_RISK,
        shell_var="RISK_MIN_STAKE_RATIO",
        sig_key="rmin",
    ),
    ParamDef(
        key="risk_max_stake_ratio",
        param_type="float",
        default=1.2,
        description="动态仓位最大倍率",
        group=GRP_RISK,
        shell_var="RISK_MAX_STAKE_RATIO",
        sig_key="rmax",
    ),
    ParamDef(
        key="risk_diff_boost_threshold",
        param_type="float",
        default=0.44,
        description="风险评分高于此值时要求更大价差",
        group=GRP_RISK,
        shell_var="RISK_DIFF_BOOST_THRESHOLD",
        sig_key="rdb",
    ),
    ParamDef(
        key="risk_diff_boost_multiplier",
        param_type="float",
        default=1.40,
        description="高风险时价差倍率提升",
        group=GRP_RISK,
        shell_var="RISK_DIFF_BOOST_MULTIPLIER",
        sig_key="rdbm",
    ),
    ParamDef(
        key="cross_borderline_diff_multiplier",
        param_type="float",
        default=0.0,
        description="cross接近上限时价差倍增系数",
        group=GRP_RISK,
        shell_var="CROSS_BORDERLINE_DIFF_MULTIPLIER",
        sig_key="cbdm",
    ),
    ParamDef(
        key="stake_cap_very_high",
        param_type="float",
        default=0.0,
        description="very_high 风险仓位上限",
        group=GRP_RISK,
        shell_var="STAKE_CAP_VERY_HIGH",
        sig_key="",
    ),
    ParamDef(
        key="stake_cap_high",
        param_type="float",
        default=0.50,
        description="high 风险仓位上限",
        group=GRP_RISK,
        shell_var="STAKE_CAP_HIGH",
        sig_key="",
    ),
    ParamDef(
        key="stake_cap_medium_high",
        param_type="float",
        default=0.35,
        description="medium_high 风险仓位上限",
        group=GRP_RISK,
        shell_var="STAKE_CAP_MEDIUM_HIGH",
        sig_key="",
    ),
    ParamDef(
        key="medium_high_threshold",
        param_type="float",
        default=0.40,
        description="medium_high 阈值",
        group=GRP_RISK,
        shell_var="MEDIUM_HIGH_THRESHOLD",
        sig_key="mht",
    ),
    ParamDef(
        key="confidence_boost",
        param_type="bool",
        default=True,
        description="是否启用高置信加仓",
        group=GRP_RISK,
        shell_var="CONFIDENCE_BOOST",
        sig_key="",
        bool_inverted=True,
        constructor_name="confidence_boost_enabled",
    ),
    ParamDef(
        key="confidence_boost_ge_095",
        param_type="float",
        default=1.5,
        description="置信度≥0.95 加仓倍率",
        group=GRP_RISK,
        shell_var="CONFIDENCE_BOOST_GE_095",
        sig_key="",
    ),
    ParamDef(
        key="risk_w_price",
        param_type="float",
        default=0.50,
        description="风险评分：价格权重",
        group=GRP_RISK,
        shell_var="RISK_W_PRICE",
        sig_key="",
    ),
    ParamDef(
        key="risk_w_direction",
        param_type="float",
        default=0.15,
        description="风险评分：方向权重",
        group=GRP_RISK,
        shell_var="RISK_W_DIRECTION",
        sig_key="",
    ),
    ParamDef(
        key="risk_w_stability",
        param_type="float",
        default=0.35,
        description="风险评分：稳定性权重",
        group=GRP_RISK,
        shell_var="RISK_W_STABILITY",
        sig_key="",
    ),

    # ==================== 方向确认风控 ====================
    ParamDef(
        key="direction_confirm_close",
        param_type="bool",
        default=True,
        description="是否启用方向不一致平仓",
        group=GRP_DIR_CONFIRM,
        shell_var="ENABLE_DIRECTION_CONFIRM_CLOSE",
        sig_key="dc",
        cli_flag="disable-direction-confirm-close",
        bool_inverted=True,
        constructor_name="enable_direction_confirm_close",
        startup_key="enable_direction_confirm_close",
    ),
    ParamDef(
        key="direction_confirm_preclose_sec",
        param_type="int",
        default=15,
        description="方向确认触发秒（距5m结束）",
        group=GRP_DIR_CONFIRM,
        shell_var="DIRECTION_CONFIRM_PRECLOSE_SEC",
        sig_key="dcp",
    ),
    ParamDef(
        key="direction_confirm_min_abs_diff",
        param_type="float",
        default=0.0,
        description="不一致平仓最小绝对价差",
        group=GRP_DIR_CONFIRM,
        shell_var="DIRECTION_CONFIRM_MIN_ABS_DIFF",
        sig_key="dcd",
    ),
    ParamDef(
        key="enable_direction_confirm_low_diff_close",
        param_type="bool",
        default=True,
        description="是否启用低价差强平",
        group=GRP_DIR_CONFIRM,
        shell_var="ENABLE_DIRECTION_CONFIRM_LOW_DIFF_CLOSE",
        sig_key="dcl",
    ),
    ParamDef(
        key="direction_confirm_low_diff_threshold",
        param_type="float",
        default=10.0,
        description="低价差强平阈值",
        group=GRP_DIR_CONFIRM,
        shell_var="DIRECTION_CONFIRM_LOW_DIFF_THRESHOLD",
        sig_key="dclt",
    ),

    # ==================== 终盘风控 ====================
    ParamDef(
        key="enable_last_seconds_reverse_guard",
        param_type="bool",
        default=True,
        description="是否启用终盘加速反向风控",
        group=GRP_ENDGAME,
        shell_var="ENABLE_LAST_SECONDS_REVERSE_GUARD",
        sig_key="lrg",
    ),
    ParamDef(
        key="reverse_guard_start_sec",
        param_type="int",
        default=295,
        description="终盘反向风控起始秒",
        group=GRP_ENDGAME,
        shell_var="REVERSE_GUARD_START_SEC",
        sig_key="lrgs",
    ),
    ParamDef(
        key="reverse_guard_lookback_sec",
        param_type="int",
        default=3,
        description="终盘反向风控回看秒数",
        group=GRP_ENDGAME,
        shell_var="REVERSE_GUARD_LOOKBACK_SEC",
        sig_key="lrgl",
    ),
    ParamDef(
        key="reverse_guard_btc_move",
        param_type="float",
        default=15.0,
        description="终盘反向BTC移动阈值",
        group=GRP_ENDGAME,
        shell_var="REVERSE_GUARD_BTC_MOVE",
        sig_key="lrgm",
    ),
    ParamDef(
        key="reverse_guard_require_cross_open",
        param_type="bool",
        default=True,
        description="是否要求穿越开盘价才平仓",
        group=GRP_ENDGAME,
        shell_var="REVERSE_GUARD_REQUIRE_CROSS_OPEN",
        sig_key="lrgx",
        cli_flag="disable-reverse-guard-require-cross-open",
        bool_inverted=True,
    ),
    ParamDef(
        key="enable_last_seconds_position_guard",
        param_type="bool",
        default=True,
        description="是否启用终盘位置风控",
        group=GRP_ENDGAME,
        shell_var="ENABLE_LAST_SECONDS_POSITION_GUARD",
        sig_key="lpg",
    ),
    ParamDef(
        key="position_guard_start_sec",
        param_type="int",
        default=298,
        description="终盘位置风控起始秒",
        group=GRP_ENDGAME,
        shell_var="POSITION_GUARD_START_SEC",
        sig_key="lpgs",
    ),
    ParamDef(
        key="position_guard_min_consecutive_sec",
        param_type="int",
        default=2,
        description="终盘位置反向连续秒数",
        group=GRP_ENDGAME,
        shell_var="POSITION_GUARD_MIN_CONSECUTIVE_SEC",
        sig_key="lpgn",
    ),

    # ==================== 入场控制（末尾追加） ====================
    ParamDef(
        key="enable_db_tick_validation",
        param_type="bool",
        default=True,
        description="是否启用DB tick交叉验证",
        group=GRP_ENTRY,
        shell_var="ENABLE_DB_TICK_VALIDATION",
        sig_key="dbtv",
    ),
]


# ---------------------------------------------------------------------------
# 派生数据结构（运行时自动计算，勿手动编辑）
# ---------------------------------------------------------------------------

_REGISTRY_BY_KEY: dict[str, ParamDef] = {p.key: p for p in PARAM_REGISTRY}

# 按 GROUP_ORDER 排列的 key 列表（用于 STRATEGY_PARAM_DISPLAY_ORDER 等）
DISPLAY_ORDER: list[str] = [
    p.key for g in GROUP_ORDER for p in PARAM_REGISTRY if p.group == g
]

# Python key → Shell 变量名映射（排除无 shell_var 的兼容参数）
PARAM_SHELL_MAP: dict[str, str] = {
    p.key: p.shell_var for p in PARAM_REGISTRY if p.shell_var
}

# 布尔参数 key 集合
BOOLEAN_PARAM_KEYS: set[str] = {
    p.key for p in PARAM_REGISTRY if p.param_type == "bool"
}

# 参数默认值映射
PARAM_DEFAULTS: dict[str, Any] = {p.key: p.default for p in PARAM_REGISTRY}


def get_param_def(key: str) -> ParamDef | None:
    """按 key 查找参数定义。"""
    return _REGISTRY_BY_KEY.get(key)


def build_startup_params(args: Any) -> dict[str, Any]:
    """从 argparse Namespace 自动构建 startup_params 字典（写入 DB params_json）。

    跳过 shell_var 为空的兼容保留参数（如 take_profit_spread）。
    """
    result: dict[str, Any] = {}
    for p in PARAM_REGISTRY:
        if not p.shell_var:
            continue
        result[p.startup_key_resolved] = p.resolve_value(args)
    return result


def _sig_format_value(p: ParamDef, value: Any) -> str:
    """将参数值格式化为签名字符串片段。"""
    if p.param_type == "bool":
        return str(int(value))
    if p.param_type == "float":
        return f"{value:g}"
    return str(value)


def build_strategy_signature(args: Any) -> str:
    """从 argparse Namespace 自动构建策略签名字符串。

    仅包含 sig_key 非空的参数，按 PARAM_REGISTRY 定义顺序排列。
    """
    parts: list[str] = []
    for p in PARAM_REGISTRY:
        if not p.sig_key:
            continue
        value = p.resolve_value(args)
        parts.append(f"{p.sig_key}={_sig_format_value(p, value)}")
    return ",".join(parts)


def get_param_schema() -> list[dict]:
    """生成前端可用的分组参数 schema（供 API 下发）。"""
    groups: dict[str, list[dict]] = {}
    for p in PARAM_REGISTRY:
        if not p.shell_var:
            continue  # 兼容保留参数不暴露给前端
        entry = {
            "key": p.key,
            "type": p.param_type,
            "default": p.default,
            "description": p.description,
        }
        groups.setdefault(p.group, []).append(entry)
    return [
        {"label": g, "params": groups.get(g, [])}
        for g in GROUP_ORDER
        if g in groups
    ]
