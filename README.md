# Auto Polymarket

一个自动化 Polymarket 交易分析系统，集成 AI 市场研究、实时价格监控和持仓分析功能。

## 功能特性

### 🤖 AI 驱动的市场分析
- 使用 Google Gemini API 进行智能市场研究
- 基于持仓数据和 BTC 价格趋势生成交易建议
- 自动生成结构化的分析报告（市场快照、仓位与挂单操作建议、预警信号）

### 📊 实时价格监控
- 通过 Binance WebSocket 实时监控 BTC/USDT 价格
- 支持多种价格流类型（ticker、bookTicker、avgPrice）
- 支持基于 `@aggTrade` 的 15 秒滑窗 Volume Delta 实时判定
- 支持 BTC + Polymarket 5m 市场的逐秒对齐采样（写入 SQLite）
- 可配置的价格预警系统（上涨/下跌预警）
- 每小时自动发送价格报告邮件

### 💼 持仓分析
- 自动获取 Polymarket 持仓和挂单信息
- 智能匹配持仓与挂单关系
- 结合 BTC 4小时 K 线数据进行综合分析
- 生成 HTML 格式的详细分析报告

### 📅 月初建仓建议
- 月初自动汇总宏观流动性与技术面数据
- 生成新一月 BTC 价格预测市场的趋势判断与建仓方案
- 通过邮件发送月初策略建议

### 📧 邮件通知
- 支持 HTML 和纯文本邮件
- 自动发送持仓分析报告
- 价格预警通知
- 每小时价格报告

### 🔄 自动化执行
- 提供 Shell 脚本实现自动化流程
- 支持后台运行价格监控服务
- 自动重启和错误处理

## 项目结构

```
auto_polymarket/
├── 5m_trade.py              # BTC 5m up/down 策略交易服务（入口）
├── btc_1s_market_monitor.py  # BTC + Polymarket 5m 逐秒采样监控（入口）
├── position_analyze.py      # 持仓分析主程序（入口）
├── btc_price_watcher.py     # BTC 价格监控服务（入口）
├── btc_volume_delta_service.py # BTC 15秒滑窗 Volume Delta 服务（入口）
├── monthly_btc_strategy.py  # 月初建仓建议（入口）
├── config.py                # 配置文件
├── price_warn_config.py     # 价格预警配置（自动生成）
├── auto_polymarket.sh       # 自动化执行脚本
├── data/                    # 数据源层
│   ├── polymarket.py        # Polymarket 持仓与订单
│   ├── binance.py           # Binance 现货与衍生品
│   ├── etf.py               # SoSoValue ETF 流入
│   ├── rsi.py               # RSI 指标
│   └── defillama.py         # DefiLlama 稳定币流动性
├── services/                # 业务逻辑层
│   ├── position.py          # 持仓与挂单匹配、格式化
│   └── market_sentiment.py  # 市场情绪与资金面聚合
│   └── five_minute_trade/   # 5m_trade 领域模块（重构后）
│       ├── models.py        # TradeRecord/OpenPosition/日志过滤器
│       ├── watchers.py      # Binance/Polymarket WS 监听
│       ├── entry_ops.py     # 开仓与市场 token 选择
│       ├── execution_plans.py # 订单簿获取与执行质量评估
│       ├── position_close_ops.py # 平仓与仓位确认流程
│       └── reporting.py     # 盈亏报告统计与文本拼装
├── ai/                      # AI 分析层
│   ├── researcher.py       # Gemini 持仓/月度策略分析
│   └── prompts.py           # 提示词与 Schema
├── notifications/           # 通知层
│   ├── email.py             # 邮件发送
│   └── html.py              # HTML 报告模板
├── output/                  # 输出目录（自动生成，已 gitignore）
├── scripts/
│   ├── auto_polymarket.sh   # 主自动化脚本
│   └── restart_5m_trade.sh  # 5m_trade 重启脚本
└── pyproject.toml           # 项目依赖配置
```

## 环境要求

- Python >= 3.13
- uv (Python 包管理器)

## 安装步骤

### 1. 克隆项目

```bash
git clone <repository-url>
cd auto_polymarket
```

### 2. 安装依赖

使用 `uv` 安装项目依赖：

```bash
uv sync
```

### 3. 配置环境变量

创建 `.env` 文件并配置以下环境变量：

```env
# Google Gemini API
GOOGLE_API_KEY=your_google_api_key

# Polymarket API
POLYMARKET_KEY=your_polymarket_private_key
WALLET_ADDRESS=your_wallet_address

# 邮件配置
TO_EMAIL=recipient@example.com
SMTP_SERVER=smtp.example.com
SMTP_PORT=465
FROM_EMAIL=sender@example.com
FROM_EMAIL_PASSWORD=your_email_password

# Dashboard 访问鉴权（用于外网访问）
DASHBOARD_PASSWORD=your_strong_password
# 可选：固定 Flask session 密钥（建议设置为随机长字符串）
DASHBOARD_SECRET_KEY=your_long_random_secret
# 可选：监听配置（默认 0.0.0.0:5000）
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=5000
# 仅在你已配置 HTTPS 时设为 true
DASHBOARD_HTTPS_ONLY=false
```

### 4. 配置参数

在 `config.py` 中可以调整以下参数：

- `REPORT_INTERVAL`: 报告发送间隔（秒），默认 3600（1小时）
- `GEMINI_MODEL_ID`: Gemini 模型 ID，默认 "gemini-3-pro-preview"

## 使用方法

### 方式一：使用自动化脚本（推荐）

运行自动化脚本，它会依次执行持仓分析和启动价格监控：

```bash
chmod +x auto_polymarket.sh
./auto_polymarket.sh
```

脚本会：
1. 运行持仓分析（`position_analyze.py`）
2. 停止现有的价格监控进程（如果存在）
3. 在后台启动新的价格监控服务

### 方式二：手动运行

#### 运行持仓分析

```bash
uv run position_analyze.py
```

这会：
- 获取当前 Polymarket 持仓和挂单
- 获取 BTC 4小时 K 线数据
- 使用 Gemini AI 进行分析
- 生成价格预警配置
- 发送 HTML 格式的分析报告邮件

#### 每 4 小时定时运行持仓分析

使用调度器在后台每 4 小时执行一次 `position_analyze.py`：

**Windows (PowerShell)：**
```powershell
cd d:\auto_polymarket
.\scripts\run_position_analyze_4h.ps1
```

**Linux / macOS：**
```bash
./scripts/run_position_analyze_4h.sh
```

或直接运行 Python 调度脚本（会立即执行一次，之后每 4 小时执行）：
```bash
uv run scripts/run_position_analyze_every_4h.py
```

日志目录：`logs/position_analyze_4h.log`。

#### 运行价格监控

```bash
uv run btc_price_watcher.py
```

#### 运行 Volume Delta 服务（45秒滑窗，45s预测，60s验证）

```bash
uv run btc_volume_delta_service.py --symbol btcusdt --window-seconds 45
```

说明：
- 每分钟 45 秒输出基于过去 45 秒 Delta 的方向预测（预测 60s 价格相对 0s 价格的方向）
- 到下一分钟 00 秒输出 BTC 实际较该分钟 0 秒价格的上涨/下跌，并统计累计预测命中率
- 当 `|Delta| <= 1` 时判定为“无法预测”，该次样本不计入命中率统计

#### 运行月初建仓建议

```bash
uv run monthly_btc_strategy.py
```

#### 运行 BTC 5m up/down 策略

直接运行：

```bash
uv run 5m_trade.py \
    --dry-run \
    --entry-minute 3 \
    --entry-preclose-sec 5 \
    --min-direction-diff 10 \
    --stake-usd 5.0 \
    --report-interval-sec 3600 \
    --max-entry-price 0.80 \
    --take-profit-spread 0.15 \
    --stop-loss-spread -0.20 \
    --trade-db-path logs/5m_trade.sqlite3
```

推荐使用重启脚本：

```bash
chmod +x scripts/restart_5m_trade.sh
./scripts/restart_5m_trade.sh --dry-run 3 5 10 5.0 3600 0.80 0.15 -0.20 60 logs/5m_trade.sqlite3 0.95 0.15 1.333333
```

参数说明：
- `--dry-run`：仅模拟交易，不实际下单（脚本模式参数使用 `--dry-run|--live`）
- `--entry-minute`：在第几分钟做方向预判（1-4，默认 `3`）
- `--entry-preclose-sec`：该分钟 1m K 线收盘前多少秒触发“抢跑”建仓（默认 `5`）
- `--min-direction-diff`：预判价与窗口开盘价最小绝对差值（USDT，默认 `10`）
- `--stake-usd`：单笔仓位金额（USDC，默认 `5.0`）
- `--report-interval-sec`：盈亏报告发送间隔秒数（默认 `3600`）
- `--max-entry-price`：允许开仓的最高 best ask 价格（默认 `0.80`）
- `--take-profit-spread`：止盈价差（相对买入价，默认 `0.15`）
- `--stop-loss-spread`：止损价差（相对买入价，默认 `-0.20`）
- `tp_price_cap`（脚本第12位参数）：动态止盈价格上限（默认 `0.95`）
- `tp_value_cap`（脚本第13位参数）：动态止盈价差上限（默认 `0.15`）
- `sl_to_tp_ratio`（脚本第14位参数）：动态止损与止盈价差倍率（默认 `1.333333`）
- `--min-hold-before-close-sec`：最短持仓保护时间（秒，默认 `5`，`0` 表示关闭保护）
- `--trade-db-path`：交易事件 SQLite 文件路径（例如 `logs/5m_trade.sqlite3`）

重启脚本参数顺序：

```bash
./scripts/restart_5m_trade.sh [--dry-run|--live] [entry_minute] [entry_preclose_sec] [min_direction_diff] [stake_usd] [report_interval_sec] [max_entry_price] [take_profit_spread] [stop_loss_spread] [min_hold_before_close_sec] [trade_db_path] [tp_price_cap] [tp_value_cap] [sl_to_tp_ratio]
```

说明：
- 若未传动态参数，脚本默认 `tp_price_cap=0.95`、`tp_value_cap=0.15`、`sl_to_tp_ratio=1.333333`。
- 若未传 `min_hold_before_close_sec`，`restart_5m_trade.sh` 默认 `60`（脚本侧默认），与 `5m_trade.py` 直接运行默认值 `5` 不同。

切换实盘：

```bash
./scripts/restart_5m_trade.sh --live 3 5 10 5.0 3600 0.80 0.15 -0.20 60 logs/trade.sqlite3 0.95 0.15 1.333333
```

#### 5m_trade 模块化说明（重构后）

`5m_trade.py` 保留策略编排与入口逻辑，核心子能力拆分到 `services/five_minute_trade/`：

- `watchers.py`：Binance/Polymarket WebSocket 监听
- `entry_ops.py`：建仓链路与市场选择
- `execution_plans.py`：订单簿读取、滑点评估、成交计划日志
- `position_close_ops.py`：平仓提交、快慢通道对账、残仓恢复
- `reporting.py`：每小时/累计统计计算与报告文本生成

`btc_1s_market_monitor.py` 已直接依赖 `services.five_minute_trade.watchers.PolymarketAssetPriceWatcher`，不再通过动态加载 `5m_trade.py` 获取 watcher 类。

#### 运行 BTC + Polymarket 逐秒监控（SQLite）

```bash
uv run btc_1s_market_monitor.py --symbol btcusdt
```

说明：
- Binance 使用 WS 维护 BTC 最新价格；
- Polymarket 使用 WS 订阅当前 5m 市场 up/down 双边盘口；
- 服务每秒写入一条对齐快照到 `btc_poly_1s_ticks` 表，便于后续分析；
- 数据库路径由 `config.SQLITE_DB_PATH`（环境变量 `SQLITE_DB_PATH`）统一控制，默认 `logs/trade.sqlite3`。

快速查询示例：

```bash
uv run python -c "import sqlite3; c=sqlite3.connect('logs/trade.sqlite3'); print(c.execute('SELECT * FROM btc_poly_1s_ticks ORDER BY ts_sec DESC LIMIT 5').fetchall())"
```

#### 运行 5m_trade 参数回测（网格搜索）

当 `btc_poly_1s_ticks` 已积累足够历史秒级数据后，可以离线回测不同参数组合：

```bash
uv run scripts/backtest_5m_trade_params.py \
    --db-path logs/trade.sqlite3 \
    --entry-minute-grid 2,3,4 \
    --entry-preclose-sec-grid 4,5,6 \
    --min-direction-diff-grid 5,10,15,20 \
    --max-entry-price-grid 0.75,0.8,0.85,0.9 \
    --stake-usd-grid 5 \
    --min-hold-before-close-sec-grid 0,5,60 \
    --tp-price-cap-grid 0.9,0.95,0.99 \
    --tp-value-cap-grid 0.1,0.15,0.2 \
    --sl-to-tp-ratio-grid 1.0,1.333333,1.5 \
    --sort-by total_pnl \
    --top-k 20 \
    --output-csv output/5m_param_backtest.csv
```

可选时间范围筛选：

```bash
uv run scripts/backtest_5m_trade_params.py \
    --db-path logs/trade.sqlite3 \
    --start-ts-sec 1772700000 \
    --end-ts-sec 1772775000
```

输出说明：
- 终端打印按排序指标输出 Top K 参数组合（总收益、胜率、回撤、成交率等）；
- 全量结果写入 `--output-csv` 对应文件；
- 回测使用 best ask/bid 做报价级模拟，不包含深度滑点路径还原；
- 动态止盈止损支持同时扫描 `TP价格上限`、`TP价差上限`、`SL/TP 倍率`。

#### 运行 Web Dashboard（支持外网访问）

```bash
uv run app.py
```

说明：
- 服务监听 `0.0.0.0:5000`，可从同网络或公网映射后访问。
- 直接通过 IP 访问示例：`http://<你的公网IP>:5000`。
- 首次访问会进入登录页，输入 `DASHBOARD_PASSWORD` 后才可使用 Dashboard 和 API。
- 如需公网访问，建议结合云防火墙/反向代理（Nginx + HTTPS）仅开放必要端口。

这会：
- 连接到 Binance WebSocket
- 实时监控 BTC/USDT 价格
- 根据配置发送价格预警邮件
- 每小时发送价格报告

### 查看日志

价格监控服务在后台运行时，日志会输出到 `btc_watcher.log`：

```bash
tail -f btc_watcher.log
```

### 停止价格监控

找到进程 ID 并停止：

```bash
# 查找进程
pgrep -f btc_price_watcher.py

# 停止进程
pkill -f btc_price_watcher.py
```

## 主要模块说明

### 入口脚本
- **position_analyze.py**：持仓分析主程序，整合持仓获取、订单匹配、AI 分析和报告生成
- **btc_price_watcher.py**：BTC 价格监控服务，支持 WebSocket 实时监控、价格预警
- **monthly_btc_strategy.py**：月初建仓建议，结合宏观与技术面生成策略方案

### services/ 业务逻辑
- **position.py**：持仓与挂单匹配、格式化
- **market_sentiment.py**：市场情绪与资金面数据聚合（恐惧贪婪、衍生品、RSI、ETF、稳定币）

### data/ 数据源
- **polymarket.py**：Polymarket 持仓、挂单、订单操作
- **binance.py**：BTC 价格、4h K 线、衍生品数据
- **etf.py**：SoSoValue ETF 流入爬虫
- **rsi.py**：RSI 指标计算
- **defillama.py**：稳定币宏观流动性

### ai/ AI 分析
- **researcher.py**：Gemini API 市场研究，支持 Google Search Grounding
- **prompts.py**：结构化输出 Schema 与提示词

### notifications/ 通知
- **email.py**：邮件发送服务（支持 HTML）
- **html.py**：分析报告 HTML 模板

## 价格预警配置

价格预警配置由 AI 分析自动生成，保存在 `price_warn_config.py` 文件中。配置格式：

```python
WARN_PRICE = [
    {
        "价格": "70000",
        "预警方向": "up_to",  # 或 "down_to"
        "操作建议": "建议操作...",
        "alert_status": False
    }
]
```

## 注意事项

1. **API 密钥安全**：请妥善保管 `.env` 文件，不要将其提交到版本控制系统
2. **试运行模式**：默认启用 `DRY_RUN=True`，实际交易前请确认配置
3. **网络连接**：确保能够访问 Binance API 和 Polymarket API
4. **邮件服务**：确保 SMTP 配置正确，某些邮箱服务商需要应用专用密码
5. **API 限制**：注意 Google Gemini API 和 Polymarket API 的调用频率限制

## 依赖项

主要依赖包：
- `google-genai`: Google Gemini API 客户端
- `py-clob-client`: Polymarket CLOB 客户端
- `websocket-client`: WebSocket 客户端
- `python-dotenv`: 环境变量管理

完整依赖列表请查看 `pyproject.toml`。

## 许可证

[添加许可证信息]

## 贡献

欢迎提交 Issue 和 Pull Request！

## 联系方式

[添加联系方式]
