# cross_trade

基于 `5m_trade_mispricing` 基础设施衍生的独立策略目录，不改动原有策略代码。

## 策略逻辑

- 决策时点：第 4 分钟末（`entry_minute=4`）
- 过滤 1：`|trend_4m| > 0.04`
- 过滤 2：`cross_open_max <= 6`
  - `cross_open_max` 定义：前 4 分钟内，`up_best_bid` 相对开盘价的符号在相邻秒发生 `+1 <-> -1` 翻转的次数
- 方向选择：4 分钟末，买入 `bid` 更大的一侧（`up` 或 `down`）
- 交易可行性过滤：
  - 4 分钟末两侧 `bid` 均需 `> 0`
  - 两侧 `bid` 均需 `< 0.99`（避免接近满价的不可交易情况）
- 持仓管理：不主动平仓，持有到市场满期，等待 Polymarket 自动结算

## 运行

```bash
python -m cross_trade --dry-run
```

或：

```bash
python cross_trade/cross_trade_5m.py --dry-run
```

## 常用参数

- `--trend-th`：趋势阈值，默认 `0.04`
- `--cross-open-max-th`：穿越次数上限，默认 `6`
- `--max-end-bid`：4 分钟末 bid 上限，默认 `0.99`
- 其余参数沿用 `build_trade_arg_parser`（如 `--stake-usd`、`--trade-db-path` 等）

## 研究：cross 次数 vs 同侧率

可用脚本 `cross_trade/analyze_cross_count_same_side.py` 分析「前 4 分钟 cross 次数」对「4 分钟末同侧率」的影响：

```bash
python cross_trade/analyze_cross_count_same_side.py \
  --db-path tmp/trade.sqlite3 \
  --lookback-days 7 \
  --trend-th 0.04 \
  --max-cross-bucket 10
```

输出包含两类统计：

- `cross_bucket` 分桶统计：每个 cross 次数桶的样本量与同侧率；
- `cross <= k` 累计统计：用于挑选 `cross_open_max_th` 阈值。

可选参数：

- `--require-tradeable-end-bid`：仅保留 4 分钟末双边 bid 可交易窗口（>0 且 < `max_end_bid`）；
- `--max-scan-rows`：从库尾最多扫描行数（默认 1000000，适合历史库存在坏页时做容错读取）。
