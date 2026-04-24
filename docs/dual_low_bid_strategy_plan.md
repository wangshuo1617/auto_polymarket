# Dual Low Bid Strategy Design Plan

## Problem Statement

Current 5m BTC up/down strategy is purely directional (taker execution), suffering from:
- Stop losses are net negative (-$52 over 7 days)
- Direction prediction uncertainty causes losses
- Taker fills at high prices (0.65-0.85)

## 数据验证的关键结论

通过 28 天 7,717 个窗口的 228 万条 1s tick 数据回测，逐步排除了多种策略变体：

| 策略变体 | 日均 PnL | 失败原因 |
|----------|---------|----------|
| 对称挂单 (0.45/0.45) | **-$408** | 单腿 42%，方向 50/50 无优势 |
| 预测方向 + 非对称 | **-$378** | 方向预测仅 58-62% 准确 |
| 反应式 (先低后高) | **-$174** | 时序问题：underdog 便宜时 favorite 很贵，5s 后已错过 |
| Oracle 方向 (上界) | +$347 | 不可达：需要事先知道哪边是 underdog |
| **✅ 双边低价 + 单腿卖回** | **+$275** | 不需方向预测，单腿亏损极小 |

### 最终策略：Dual Low Bid + Sell-Back

**核心逻辑**：
1. t=0：在 UP 和 DOWN 两侧各挂 $0.38 限价买单（GTC）
2. 窗口内：价格波动，两侧的 ask 价格都有可能跌到 $0.38
3. t=270s：检查成交状态
   - **两边都成交**（33.3%）→ 持有到结算，成本 $0.76/对，赢 $1.00 → 净赚 $3.60（15股）
   - **只有一边成交**（66.4%）→ 立即以 bid 卖回 → 仅亏 ~$0.30 点差（$0.02/股）
   - **都没成交**（0.3%）→ 取消订单，零成本

**为什么有效**：
- 单腿成交时 88.5% 会输（因为成交的是 underdog），所以**必须卖回**而非持有
- 卖回成本极低（$0.02/股点差），回报/风险比 **12:1**
- 盈亏平衡点仅需 7.7% 双腿成交率，实际 33.3%（4.3 倍安全边际）
- **无需方向预测**，完全基于价格波动的统计特性

### 卖回时间点

| 策略 | 日均 PnL | 说明 |
|------|---------|------|
| 第一腿后等 60s | -$4 | 太早，误杀 96% 双腿窗口 |
| 第一腿后等 120s | $129 | 还会误杀 41% |
| 第一腿后等 180s | $223 | 误杀 14% |
| **窗口 270s 统一检查** | **$275** | 最优，零误杀 |

**结论**：不用复杂的计时逻辑，在 270s 统一处理即可。

### Queue Haircut 敏感度（LOW=0.38）

| 门槛 | 双腿成交率 | 日均 PnL |
|------|-----------|---------|
| 5 tick（乐观） | 39.7% | $344 |
| **10 tick（基准）** | **33.3%** | **$275** |
| 15 tick（保守） | 28.9% | $228 |
| 20 tick（极保守） | 24.3% | $179 |

### 最优价格分析（10-tick haircut，$0.02 点差）

| 价格 | 双腿率 | 日均 PnL | 说明 |
|------|-------|---------|------|
| 0.34 | 23.1% | $243 | 利润高但双腿率低 |
| 0.36 | 28.1% | $267 | |
| **0.38** | **33.3%** | **$275** | **最优** |
| 0.40 | 38.7% | $269 | 过了峰值，单笔利润太低 |
| 0.42 | 44.0% | $245 | |

## Architecture

### New entry script: `dual_maker_trade.py`

独立于 `5m_trade.py`（2500+ 行，深度耦合方向策略）。复用：
- `data/polymarket.py` — 下单、撤单、orderbook
- `services/five_minute_trade/watchers.py` — BTC 价格 + Polymarket book watchers
- `data/database.py` — PG 连接
- `config.py` — 环境配置

### New modules:
1. `services/dual_maker/__init__.py`
2. `services/dual_maker/strategy.py` — 核心策略类 DualLowBidTrader
3. `services/dual_maker/order_manager.py` — 订单生命周期（下单、监控成交、撤单、卖回）
4. `services/dual_maker/fill_simulator.py` — Dry-run 成交模拟（10-tick queue haircut）
5. `services/dual_maker/trade_db.py` — DB 读写

### DB Schema: `dual_maker_trades`

```sql
CREATE TABLE dual_maker_trades (
    id SERIAL PRIMARY KEY,
    market_slug VARCHAR NOT NULL,
    mode VARCHAR NOT NULL DEFAULT 'dry-run',

    -- 挂单信息
    bid_price FLOAT NOT NULL,           -- 两侧相同挂单价（如 0.38）
    shares_per_side INTEGER NOT NULL,   -- 每侧股数（如 15）

    -- UP 侧
    up_order_id VARCHAR,                -- Polymarket order ID（live 模式）
    up_order_placed_at TIMESTAMP,
    up_filled BOOLEAN DEFAULT FALSE,
    up_fill_time TIMESTAMP,
    up_fill_price FLOAT,                -- 实际成交价（可能优于挂单价）

    -- DOWN 侧
    down_order_id VARCHAR,
    down_order_placed_at TIMESTAMP,
    down_filled BOOLEAN DEFAULT FALSE,
    down_fill_time TIMESTAMP,
    down_fill_price FLOAT,

    -- 结果
    status VARCHAR DEFAULT 'pending',
    -- pending → both_filled / single_leg_up / single_leg_down / no_fill
    -- → settled / sold_back
    outcome VARCHAR,                    -- won/lost/sold_back/no_fill
    winning_direction VARCHAR,
    sell_back_price FLOAT,              -- 单腿卖回价格
    sell_back_order_id VARCHAR,
    pnl FLOAT,

    -- 元数据
    window_open_time TIMESTAMP,
    settled_at TIMESTAMP,
    startup_id INTEGER,

    UNIQUE(market_slug, mode)
);
```

## Strategy Logic (2-Phase, Simplified)

### Phase 1: Place Orders (t=0s)
- 窗口开启，立即获取 UP/DOWN token IDs
- 在 UP 和 DOWN 两侧各下一个 GTC 限价买单，价格 = `bid_price`
- Dry-run：记录虚拟下单时间，开始监听 best_ask

### Phase 2: Monitor & Settle (t=0-270s)
- 持续监听两侧 best_ask（通过 PolymarketAssetPriceWatcher）
- 检测成交状态：
  - Live 模式：轮询 Activity API 或监听 WS fill events
  - Dry-run：当 best_ask ≤ bid_price 累计 ≥ N ticks，判定为成交
- **t=270s 决策点**：
  1. **两边都成交** → 取消无关订单（如果有），等待结算
  2. **只有一边成交** → 立即卖回：
     - Live：以当前 best_bid 下 FAK 卖单
     - Dry-run：记录 best_bid 作为卖回价
  3. **都没成交** → 取消两个挂单
- **t=300s**：结算，记录 PnL

### 无需的复杂逻辑（与旧方案对比）
- ❌ 观察期（省去了 60s，直接 t=0 下单）
- ❌ 方向预测（两侧价格相同）
- ❌ 非对称定价（需要知道 underdog）
- ❌ Chase 追价（固定价格不调整）
- ❌ DCA 加仓
- ❌ 止盈止损

## Key Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `bid_price` | 0.38 | 两侧相同的限价买入价 |
| `shares_per_side` | 15 | 每侧股数 |
| `cancel_at_sec` | 270 | 统一检查并处理的时间点 |
| `queue_haircut_ticks` | 10 | Dry-run 成交判定的最低 tick 数 |
| `mode` | dry-run | dry-run / live |
| `sell_back_method` | FAK | 单腿卖回的订单类型 |

## Dry-Run Fill Simulation

模拟规则（比旧策略更严格，考虑 queue priority）：
1. 虚拟下单后，开始计数 best_ask ≤ bid_price 的 tick 数
2. 累计达到 `queue_haircut_ticks`（默认 10）个 tick 后判定成交
3. 成交价 = bid_price（maker 挂单，不会比挂单价差）
4. 卖回模拟：best_bid 作为卖出价，点差约 $0.01-0.02

## Todos

### Phase A: 实现（Dry-Run）— ✅ 已完成

1. ✅ 创建 `services/dual_maker/` 包结构
2. ✅ 实现 `fill_simulator.py` — dry-run 成交模拟（含 queue haircut）
3. ✅ 实现 `order_manager.py` — 下单/撤单/卖回（live + dry-run 两种路径）
4. ✅ 实现 `strategy.py` — DualLowBidTrader 核心循环
5. ✅ 实现 `trade_db.py` — DB 操作（写入/更新/查询）
6. ✅ 创建 `dual_maker_trade.py` 入口脚本（argparse + main loop）
7. ✅ 创建 `scripts/restart_dual_maker.sh`

### Phase B: Dry-Run 验证 — 进行中

8. 启动 dry-run，运行 24-48 小时
9. 验证 dry-run 结果：
   - 双腿成交率是否接近预期 33%？
   - 单腿卖回模拟点差是否 ~$0.02？
   - 日 PnL 是否在 $179-$344 区间（queue 敏感度范围）？
   - 检查边缘情况：窗口开盘延迟、数据中断、WebSocket 断连等
10. 如果 dry-run 数据异常，分析原因并调整参数（bid_price / queue_haircut_ticks）

### Phase C: 小额实盘校准

11. 用独立账号启动小额 live（5 shares/side, ~$3.80/窗口）
12. 运行 24-48 小时后，对比实盘 vs dry-run：
    - **真实双腿成交率** vs 模拟值（关键指标！）
    - **真实卖回点差** vs 模拟的 $0.02
    - **真实队列位置**：实际需要多少 ticks 才成交？用于校准 queue_haircut_ticks
    - **订单延迟**：从发送到出现在 book 的时间
13. 计算 **实盘/回测效率比**（例：实盘双腿率 25% / 回测 33% = 0.76）
    - 若 ≥ 0.60（双腿率 ≥20%）：策略仍盈利，继续
    - 若 < 0.60：需调低 bid_price 或放弃

### Phase D: 放量上线

14. 根据校准结果调整参数（bid_price、shares_per_side）
15. 增加到 15 shares/side 或更高
16. 建立持续监控：每日 PnL 报告、异常告警
17. 考虑多价位分层挂单（如 0.36 + 0.38 + 0.40）提升成交率

### 回测留存数据（已完成）

以下分析结论来自 28 天 7,717 窗口 228 万条 1s tick 的回测：

- **策略变体对比**：对称(-$408)、预测方向(-$378)、反应式(-$174)全部亏损；唯一盈利方案是双边低价+卖回
- **最优价格**：0.38（峰值 $275/天），价格曲线在 0.36-0.40 区间平坦
- **Queue 敏感度**：5-tick $344/天 → 10-tick $275 → 15-tick $228 → 20-tick $179
- **卖回时机**：窗口 270s 统一处理最优（等越久越好），不需要提前计时
- **单腿胜率**：仅 10-12%，必须卖回（持有到期日均-$397 vs 卖回+$275）
- **最大回撤**：$6.00（20 个连续单腿窗口，1.7 小时）
- **零亏损日**：29/29 天全部盈利，最差日 $130.80
- **方向预测准确率**：前 10s 仅 58.7%，不足以支撑非对称策略

## Risk Management

- **单腿保护**：立即卖回，仅亏 $0.30（$0.02/股 × 15），而非持有到期亏 $5.70
- **回报/风险比**：12:1（$3.60 vs $0.30）
- **盈亏平衡**：仅需 7.7% 双腿率（实际 33.3%）
- **资金效率**：每窗口最大 $11.40（两侧都成交时）
- **撤单纪律**：270s 必须处理所有未成交订单
- **Queue priority**：t=0 下单，5m 市场每个窗口新合约无预存订单，先到先得

## Recovery & Coexistence

- **崩溃恢复**：启动时检查是否有 pending 状态的挂单（GTC 会留在市场上），执行清理
- **账号隔离**：建议使用独立 Polymarket 账号，避免与 5m_trade.py 冲突
- **并行安全**：同一窗口不会重复下单（通过 market_slug 唯一约束）

## Financial Projections (28-day backtest)

| 场景 | 日均 PnL | 月均 PnL |
|------|---------|---------|
| 乐观（5-tick） | $344 | $10,320 |
| **基准（10-tick）** | **$275** | **$8,250** |
| 保守（15-tick） | $228 | $6,840 |
| 极保守（20-tick） | $179 | $5,370 |
