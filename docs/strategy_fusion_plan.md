# 5m Trade 策略融合实现计划

## 背景

将 marketing101 的核心策略思路融合到现有 5m_trade.py 策略中。核心变化：从"固定时间入场、单笔下注、固定仓位"改为"偏离触发入场、DCA加仓、动态缩仓"，同时保留现有所有止损保护机制。

## 现状 vs 目标

| 维度 | 现状 | 目标 |
|------|------|------|
| 入场时机 | 固定第N分钟末尾 | BTC偏离≥阈值时随时触发 |
| 入场价格 | ~0.96（方向已定，价格已高）| ~0.60-0.75（更早入场）|
| 买入次数 | 单笔 | DCA多笔，偏离加大时追加 |
| 仓位控制 | 固定/risk_sizing | 连败缩仓 + DCA信心调节 |
| 过滤条件 | 硬门槛（跳窗口）| 软调节（调DCA激进度）|
| 止损 | imbalance/proximity/bid_drop | 全部保留 |

## 实现方案

### 第1阶段：偏离触发入场（替换固定时间入场）

**改动核心**：入场判定从 `_handle_entry_minute()` 的"固定时间窗口"模式，改为在 `_clock_tick()` 主循环中持续监控 BTC 偏离。

#### 新参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `enable_deviation_entry` | bool | False | 启用偏离入场模式（False=保持现有固定时间模式） |
| `deviation_entry_threshold` | float | 40.0 | BTC偏离开盘价$阈值，触发首次入场 |
| `deviation_entry_start_sec` | float | 60.0 | 偏离入场最早生效时间（窗口内秒） |
| `deviation_entry_end_sec` | float | 240.0 | 偏离入场最晚截止时间（之后不再首次入场） |

#### 逻辑

1. `_clock_tick()` 中新增分支：若 `enable_deviation_entry=True` 且 `deviation_entry_start_sec <= rel_sec < deviation_entry_end_sec`：
   - 计算 `abs_diff = abs(btc_price - window_open_price)`
   - 若 `abs_diff >= deviation_entry_threshold` 且 `window_traded == False`：
     - 确定方向 → 调用 `_open_position()` 首次建仓
     - 设置 `window_traded = True`
2. 原有 `entry_trigger_sec..entry_deadline_sec` 的判定逻辑仅在 `enable_deviation_entry=False` 时生效
3. 保留 `toxic_utc_hours` 检查（在偏离入场前也检查）

### 第2阶段：DCA 加仓

**改动核心**：首次入场后，在窗口内继续监控 BTC 偏离，满足条件时追加买入。加仓金额由信心评分函数 `compute_dca_add_size()` 动态决定，综合多个市场因子。

#### 新参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `enable_dca` | bool | False | 启用DCA加仓 |
| `dca_max_adds` | int | 4 | 最大追加次数 |
| `dca_interval_sec` | float | 15.0 | 两次DCA之间最小间隔（秒） |
| `dca_deviation_step` | float | 20.0 | 每次追加需要BTC额外偏离的增量（$） |
| `dca_end_sec` | float | 270.0 | DCA最晚截止时间（留30s给止损逻辑） |
| `dca_min_confidence` | float | 0.3 | DCA信心分低于此值不加仓 |
| `dca_w_deviation` | float | 0.25 | 信心权重：BTC偏离强度 |
| `dca_w_atr` | float | 0.20 | 信心权重：ATR稳定度（ATR越低信心越高） |
| `dca_w_cross` | float | 0.20 | 信心权重：cross稳定度（cross越少信心越高） |
| `dca_w_price` | float | 0.15 | 信心权重：token价格（越低盈亏比越好） |
| `dca_w_time` | float | 0.10 | 信心权重：窗口剩余时间（越早越好） |
| `dca_w_position` | float | 0.10 | 信心权重：已持仓量（已加仓越少越好） |

#### DCA 信心评分函数 `compute_dca_add_size()`

位置：`services/five_minute_trade/dca_sizing.py`（新文件）

**输入**：

| 因子 | 含义 | 高值 → 信心 |
|------|------|-------------|
| BTC 偏离增量 | 相比入场时又偏离了多少 | ↑ 趋势加强 → 加大 |
| ATR | 窗口内每秒波动均值 | ↑ 方向不确定 → **降低** |
| Cross count | BTC穿越开盘价次数 | ↑ 反复横跳 → **降低/停止** |
| Token 当前价格 | 越低盈亏比越好 | ↑ 盈亏比差 → 降低 |
| 窗口内已用时间 | 离结算越近风险越大 | ↑ 时间不够 → 降低 |
| 已持仓量 | 已经加了几次 | ↑ 集中度风险 → 降低 |

**输出**：

```python
@dataclass
class DCADecision:
    should_add: bool        # 是否加仓
    add_size_usdc: float    # 本次加仓金额
    confidence: float       # 0-1 综合信心分（日志/诊断用）
    reason: str             # 决策理由
    # 各分项分数（诊断用）
    deviation_score: float
    atr_score: float
    cross_score: float
    price_score: float
    time_score: float
    position_score: float
```

**计算逻辑**：

```python
base_add = effective_stake  # 基础加仓额 = 经连败缩仓调整后的stake

# 各因子独立产出 0.0-1.0 的信心子分数
deviation_score  = clamp((current_deviation - entry_deviation) / dca_deviation_step, 0, 1)
atr_score        = clamp(1.0 - atr / atr_ceiling, 0, 1)           # atr_ceiling 取自历史P90
cross_score      = clamp(1.0 - cross_count / cross_ceiling, 0, 1) # cross_ceiling = dca_max_adds+2
price_score      = clamp(1.0 - token_price / 0.85, 0, 1)          # token>0.85时score→0
time_score       = clamp((dca_end_sec - rel_sec) / (dca_end_sec - deviation_entry_start_sec), 0, 1)
position_score   = clamp(1.0 - dca_count / dca_max_adds, 0, 1)

confidence = (dca_w_deviation * deviation_score
            + dca_w_atr * atr_score
            + dca_w_cross * cross_score
            + dca_w_price * price_score
            + dca_w_time * time_score
            + dca_w_position * position_score)

if confidence < dca_min_confidence:
    return DCADecision(should_add=False, ...)

add_size_usdc = base_add * confidence
```

#### 触发逻辑

1. 在 `_clock_tick()` 中，若 `enable_dca=True` 且 `position is not None` 且 `rel_sec < dca_end_sec`：
   - 检查时间间隔：距上次DCA >= `dca_interval_sec`
   - 检查偏离增量：当前 `abs_diff >= deviation_entry_threshold + dca_add_count * dca_deviation_step`
   - 方向一致性：DCA方向必须与持仓方向一致
   - 调用 `compute_dca_add_size()` 获取 DCADecision
   - `should_add=True` → 追加买入（调用 `_dca_add_position()`）
2. 新方法 `_dca_add_position(add_size_usdc)`：
   - 复用 `entry_ops` 的订单簿/下单/流动性检查逻辑
   - 更新现有 `self.position` 的 size/total_invested_usdc（加权平均入场价）
   - 更新 DB 中 `trade_window_summary` 的 entry_size/entry_usdc/entry_price
   - 记录本次 DCA 的诊断信息到 `dca_history`
3. OpenPosition 新增字段：`dca_count: int = 0`, `dca_history: list = []`

注意：ATR 和 cross count 不再作为硬门槛拦截 DCA，而是通过信心分数**连续调节**加仓金额——ATR 高或 cross 多时信心分低，加仓金额自动缩小甚至归零。

### 第3阶段：连败缩仓

**改动核心**：跟踪连续亏损次数，动态缩减 stake_usd。

#### 新参数

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `enable_streak_sizing` | bool | False | 启用连败缩仓 |
| `streak_loss_threshold` | int | 3 | 连败N次后开始缩仓 |
| `streak_shrink_factor` | float | 0.5 | 缩仓比例（乘数） |
| `streak_max_shrinks` | int | 3 | 最大连续缩减次数（防止仓位过小） |

#### 逻辑

1. 新增实例变量 `_consecutive_losses: int = 0`
2. 窗口结算时（`settle_window` / `update_window_early_exit`）：
   - 亏损 → `_consecutive_losses += 1`
   - 盈利 → `_consecutive_losses = 0`
3. 入场时的 effective_stake 计算：
   - 若 `_consecutive_losses >= streak_loss_threshold`：
     - `shrinks = min(_consecutive_losses - streak_loss_threshold + 1, streak_max_shrinks)`
     - `effective_stake *= streak_shrink_factor ** shrinks`
4. 启动时从DB查询最近N个窗口结果，恢复 `_consecutive_losses` 状态

### 第4阶段：过滤条件降级为DCA调节因子

**逻辑变化（仅 deviation_entry 模式下生效）**：

- `max_btc_cross_count`：不再作为入场拦截 → 通过 DCA 信心函数的 `cross_score` 连续调节加仓量
- `max_avg_btc_delta` (ATR)：不再作为入场拦截 → 通过 DCA 信心函数的 `atr_score` 连续调节加仓量
- `min_entry_updown_diff`：**降低至 0.10**（现有0.38是为第4分钟高价差设计的；60-180s时中位价差仅0.33-0.45）。低价差窗口不拦截入场，而是通过 DCA 信心函数的 `price_score` 缩减首单/加仓金额
  - 数据支持：UP/DOWN diff < 0.10 → 方向正确率仅52%（噪声），0.10-0.20 → 56%，开始有信号
- `minute_consistency`：在偏离模式下禁用（无固定分钟概念）
- `min_direction_diff`：被 `deviation_entry_threshold` 替代
- `cross_borderline_diff_multiplier`：在偏离模式下禁用
- `risk_diff_boost_*`：在偏离模式下禁用（风险通过连败缩仓+DCA控制）

原有固定时间模式 (`enable_deviation_entry=False`) 下这些参数行为完全不变。

### 第5阶段：集成测试和参数注册

按3处更新模式：

1. **`param_registry.py`**：注册所有新参数的 `ParamDef`
2. **`5m_trade.py` `__init__`**：新增构造函数参数 + 实例变量
3. **`restart_5m_trade.sh`**：新增环境变量 + 校验 + echo + CMD

## Todos

- `deviation-entry`: 实现偏离触发入场模式（参数+_clock_tick分支+方向判定）
- `dca-engine`: 实现DCA加仓引擎（参数+_dca_add_position+OpenPosition扩展+DB更新）
- `streak-sizing`: 实现连败缩仓（参数+连败追踪+stake计算+启动恢复）
- `filter-demotion`: 偏离模式下过滤条件降级为DCA调节因子
- `param-registration`: 所有新参数注册到param_registry+restart_5m_trade.sh
- `dry-run-validation`: dry-run模式验证全流程不报错

## 注意事项

- 所有新功能默认关闭 (`enable_xxx=False`)，不影响现有策略运行
- 新旧模式通过 `enable_deviation_entry` 开关切换，可随时回退
- DCA的仓位更新需要原子性（锁保护 + DB事务）
- `_dca_add_position()` 要复用 entry_ops 的流动性/滑点检查，不能跳过
- 连败缩仓的DB恢复查询在 `start()` 方法中执行
- 保留 exit_mode=hold 的默认退出策略
