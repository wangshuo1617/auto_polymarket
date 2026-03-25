# 黄金 (GC) Polymarket 持仓分析

针对 Polymarket 黄金类预测市场的持仓分析模块，仿照 `position_analyze_oil.py` 实现。

## 事件链接

- [黄金（GC）是否会在3月底之前达到__？](https://polymarket.com/zh/event/will-gold-gc-hit-by-end-of-march)

## 结算规则（必须严格遵守）

- **↑ (above) 型市场**：Yes = 月内任一日 CME Active Month GC 官方结算价 **≥** 目标价
- **↓ (below) 型市场**：Yes = 月内任一日 CME Active Month GC 官方结算价 **≤** 目标价
- **Active Month**：CME designated delivery-cycle 月份（Feb, Apr, Jun, Aug, Oct, Dec）中最近月且非 spot month
- **仅计官方结算价**：盘中成交价、最高/最低价、买卖盘、中间价均不计入
- **裁决来源**：CME Group 官网该交易日首次发布的 Settlement 价格
- **市场开放时间**：Mar 2, 2026, 6:22 PM ET

## 环境变量

| 变量 | 说明 |
|------|------|
| `POLYMARKET_GOLD_EVENT_SLUG` | 覆盖默认黄金事件 slug，如 `will-gold-gc-hit-by-end-of-march` |
| `TO_EMAIL` | 报告接收邮箱（与主项目共用 config） |
| `POLYMARKET_KEY` / `WALLET_ADDRESS` | Polymarket 账户（与主项目共用） |

## 运行方式

```bash
cd d:\auto_polymarket
python -m gold_analyze.position_analyze_gold
```

或直接运行：

```bash
python gold_analyze/position_analyze_gold.py
```

## 输出

- `gold_analyze/last_report_gold.json` - 上一轮报告（供 AI 延续参考）
- `price_warn_config_gold.py` - 预警价格配置
- `output/{time}_gold_email.html` - 邮件 HTML 内容
- 邮件发送至 `TO_EMAIL`（若已配置）

## 依赖

- `yfinance` - GC 期货价格与 K 线
- `config` 中的 Polymarket / 邮件配置
- 与主项目共用 `data.polymarket`、`ai.researcher`、`services.*` 等模块
