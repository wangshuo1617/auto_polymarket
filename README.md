# Auto Polymarket

Polymarket 月度市场分析与 AI 顾问系统。系统自动分析持仓、生成 AI 交易建议（advisory），并通过 WebHook/邮件通知，由自动执行器按触发条件下单。

## 功能概述

### 📊 持仓分析与月度策略
- 自动获取 Polymarket 持仓和挂单，结合 BTC K 线、ETF 流入、稳定币流动性等宏观数据
- 使用 Google Gemini AI 生成结构化分析报告（市场快照、仓位建议、预警信号）
- 月初自动生成 BTC 价格市场建仓方案，通过邮件发送

### 🤖 AI Advisory 推荐系统
- 批量计算每个 Polymarket 市场的公平价值与边缘（fair value / edge）
- AI（Gemini）对高优先级市场给出 BUY / SELL / HOLD 建议，写入 PostgreSQL
- 自动执行器（recommendation_auto_executor）监听价格触发条件，按建议自动下单
- 支持校准、结算刷新、意图填充等完整交易生命周期管理

### 🌐 Web Dashboard
- Flask 实时看板：持仓、挂单、余额、BTC 价格、Advisory 建议一览
- 支持手动下单/撤单
- 基于 session 的登录鉴权，支持公网访问

### 📧 通知
- HTML 格式邮件：持仓分析报告
- 支持 SMTP 配置

---

## 项目结构

```
auto_polymarket/
├── position_analyze.py          # 持仓分析主程序（入口）
├── recommendation_auto_executor.py  # Advisory 自动执行器（长跑进程）
├── app.py                       # Flask Dashboard（入口）
├── config.py                    # 统一配置（读取 .env）
│
├── data/                        # 数据源层（纯拉取，无业务逻辑）
│   ├── polymarket.py            # Polymarket CLOB：持仓、挂单、下单
│   ├── binance.py               # Binance：BTC 价格、K 线、衍生品
│   ├── advisory_schema.py       # Advisory PostgreSQL schema 定义
│   ├── database.py              # PostgreSQL 连接池与 DDL 初始化
│   ├── deribit.py               # Deribit 期权数据
│   ├── defillama.py             # DefiLlama 稳定币流动性
│   ├── etf.py                   # SoSoValue ETF 净流入
│   └── rsi.py                   # RSI 指标
│
├── services/                    # 业务逻辑层
│   ├── advisory/                # Advisory 核心领域模块
│   │   ├── computer.py          # 公平价值计算（fair value）
│   │   ├── inputs.py            # 数据聚合与输入准备
│   │   ├── path_view.py         # AI PathView 推理
│   │   ├── intent_filler.py     # 意图填充（intent fill）
│   │   ├── settlement_adapter.py # 结算适配
│   │   ├── reconcile_v2.py      # 对账 v2
│   │   └── ...（其他 advisory 子模块）
│   ├── recommendation_db.py     # Advisory 推荐数据库操作
│   ├── recommendation_trigger/  # 价格触发引擎
│   │   ├── engine.py            # TriggerEngine：消费价格事件队列
│   │   └── parser.py            # 触发条件解析
│   ├── shared/
│   │   └── watchers.py          # BinanceBTCPriceWatcher（WebSocket aggTrade）
│   ├── position.py              # 持仓与挂单匹配、格式化
│   ├── market_sentiment.py      # 市场情绪聚合
│   └── wallet_transfer.py       # 钱包划转
│
├── ai/
│   ├── researcher.py            # Gemini API 调用
│   └── prompts.py               # 提示词与结构化输出 Schema
│
├── notifications/
│   ├── email.py                 # SMTP 邮件发送
│   └── html.py                  # HTML 报告模板
│
├── scripts/
│   ├── advisory_batch_runner.py       # Advisory 批量计算（主循环）
│   ├── advisory_settlement_refresher.py # 结算价格刷新
│   ├── advisory_calibration_monitor.py  # 校准监控
│   ├── advisory_edge_alerts.py          # 边缘预警
│   ├── advisory_fills_backfill.py       # 成交回填
│   ├── advisory_intent_filler.py        # 意图填充守护
│   ├── advisory_metrics.py              # 指标统计
│   ├── etf_volume_monitor.py            # ETF 成交量监控
│   ├── usdc_balance_monitor.py          # USDC 余额快照（月度账号）
│   ├── auto_polymarket.sh               # 触发持仓分析的快捷脚本
│   ├── restart_all.sh                   # 重启 app + usdc-monitor
│   ├── restart_advisory_batch.sh        # 重启 advisory batch 服务
│   ├── restart_advisory_settlement.sh   # 重启 advisory settlement 服务
│   ├── restart_recommendation_auto_executor.sh
│   ├── install_systemd.sh               # 安装/更新所有 systemd 单元
│   └── systemd/                         # systemd unit 文件
│       ├── auto-poly-app.service
│       ├── auto-poly-usdc-monitor.service
│       ├── auto-poly-advisory-batch.service
│       ├── auto-poly-advisory-settlement.service
│       ├── auto-poly-recommendation-executor.service
│       ├── auto-poly-etf-volume-monitor.service
│       ├── auto-poly-advisory-calibration.{service,timer}
│       ├── auto-poly-advisory-edge-alerts.{service,timer}
│       ├── auto-poly-advisory-fills-poller.{service,timer}
│       ├── auto-poly-advisory-intent-filler.{service,timer}
│       └── auto-poly-advisory-metrics.{service,timer}
│
└── pyproject.toml
```

---

## 环境要求

- Python ≥ 3.13
- [uv](https://github.com/astral-sh/uv) 包管理器
- PostgreSQL（advisory、recommendation、usdc_balance_snapshots 等表）

---

## 安装

```bash
git clone <repository-url>
cd auto_polymarket
uv sync
```

---

## 配置（.env）

所有配置通过 `.env` 文件注入，由 `config.py` 统一读取：

```env
# Google Gemini API
GOOGLE_API_KEY=your_google_api_key

# Polymarket 月度账号（advisory、position analyze、executor 使用）
MONTHLY_ACCOUNT_KEY=your_monthly_private_key
MONTHLY_ACCOUNT_WALLET_ADDRESS=your_monthly_wallet_address
# 默认 profile（应设为 analyze）
POLYMARKET_PROFILE=analyze

# PostgreSQL（advisory 系统使用）
PG_DSN=postgresql://user:pass@localhost:5432/dbname

# 邮件
TO_EMAIL=recipient@example.com
SMTP_SERVER=smtp.example.com
SMTP_PORT=465
FROM_EMAIL=sender@example.com
FROM_EMAIL_PASSWORD=your_email_password

# Flask Dashboard
DASHBOARD_PASSWORD=your_strong_password
DASHBOARD_SECRET_KEY=your_long_random_secret
DASHBOARD_HOST=0.0.0.0
DASHBOARD_PORT=5000
DASHBOARD_HTTPS_ONLY=false
```

---

## 使用方法

### 持仓分析（一次性）

```bash
uv run position_analyze.py
```

获取持仓 → AI 分析 → 发送 HTML 邮件报告。

### Web Dashboard

```bash
uv run app.py
# 或通过 systemd：
systemctl start auto-poly-app
```

访问 `http://<IP>:5000`，用 `DASHBOARD_PASSWORD` 登录。

### 初始化数据库

```bash
python -c "from data.database import init_db; init_db()"
```

---

## 服务运维（systemd）

所有长跑进程通过 systemd 管理，**不要手动 `nohup` 启动**。

### 安装/更新所有服务

```bash
sudo bash scripts/install_systemd.sh
```

### 常用命令

```bash
# 查看所有服务状态
systemctl status auto-poly-*

# 重启服务
systemctl restart auto-poly-app
systemctl restart auto-poly-advisory-batch
systemctl restart auto-poly-recommendation-executor

# 查看日志
journalctl -u auto-poly-app -f
journalctl -u auto-poly-advisory-batch -f
```

### 服务列表

| 服务 | 说明 |
|------|------|
| `auto-poly-app` | Flask Dashboard |
| `auto-poly-usdc-monitor` | USDC 余额快照（每分钟，月度账号） |
| `auto-poly-advisory-batch` | Advisory 批量公平价值计算 |
| `auto-poly-advisory-settlement` | 结算价格刷新 |
| `auto-poly-recommendation-executor` | 自动执行器（价格触发下单） |
| `auto-poly-etf-volume-monitor` | ETF 成交量监控 |
| `auto-poly-advisory-calibration` | 校准监控（timer 定时触发） |
| `auto-poly-advisory-edge-alerts` | 边缘预警（timer） |
| `auto-poly-advisory-fills-poller` | 成交回填（timer） |
| `auto-poly-advisory-intent-filler` | 意图填充守护（timer） |
| `auto-poly-advisory-metrics` | 指标统计（timer） |

---

## 注意事项

- `.env` 文件不要提交到版本控制
- 确保 `POLYMARKET_PROFILE=analyze` 使用月度账号
- 公网访问 Dashboard 建议配合 Nginx + HTTPS
- Advisory 系统依赖 PostgreSQL，启动前确保数据库可连接

---

## 归档说明

5m 交易策略、BTC 实时监控、双向做市商等历史功能已归档至 `archive` 分支，master 分支不再包含这些模块。


## 功能特性

### 🤖 AI 驱动的市场分析
- 使用 Google Gemini API 进行智能市场研究
- 基于持仓数据和 BTC 价格趋势生成交易建议
- 自动生成结构化的分析报告（市场快照、仓位与挂单操作建议、预警信号）

### 💼 持仓分析
- 自动获取 Polymarket 持仓和挂单信息
- 智能匹配持仓与挂单关系
- 结合 BTC 4小时 K 线数据进行综合分析
- 生成 HTML 格式的详细分析报告

### 📧 邮件通知
- 支持 HTML 和纯文本邮件
- 自动发送持仓分析报告
- AI advisory 推荐与触发计划通知

### 🔄 自动化执行
- 通过 systemd 管理常驻服务（advisory batch / settlement / executor / app / usdc-monitor）
- 自动重启和错误处理

## 项目结构

```
auto_polymarket/
├── position_analyze.py            # 月度账号持仓分析主入口
├── position_analyze_gold.py       # 黄金账号持仓分析入口
├── recommendation_auto_executor.py # AI advisory 推荐自动执行器
├── app.py                         # Flask Dashboard
├── config.py                      # 配置入口（读取 .env）
├── data/                          # 数据源层（Polymarket / Binance / ETF / DefiLlama 等）
├── services/                      # 业务逻辑层
│   ├── position.py                # 持仓与挂单匹配
│   ├── market_sentiment.py        # 市场情绪与资金面聚合
│   ├── market_archive.py          # 月度市场归档
│   ├── advisory/                  # AI advisory（path metrics、calibration 等）
│   ├── recommendation_db.py       # 推荐数据库（postgres）
│   ├── recommendation_trigger/    # 触发计划解析与执行
│   ├── manual_pending_orders.py   # 手动挂单管理与残仓恢复
│   ├── dual_maker/                # 双向做市
│   └── shared/                    # 公共组件（BinanceBTCPriceWatcher 等）
├── ai/                            # Gemini 分析与提示词
├── notifications/                 # 邮件与 HTML 报告
├── scripts/                       # 运维脚本与 systemd 单元
└── pyproject.toml
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

# Multi-account profiles (recommended, all in .env)
# monthly account (position/monthly analyze)
MONTHLY_ACCOUNT_KEY=your_monthly_private_key
MONTHLY_ACCOUNT_WALLET_ADDRESS=your_monthly_wallet_address
# process default profile: analyze
POLYMARKET_PROFILE=analyze

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

账号部署说明：
- 统一放在 `.env`（Gemini、邮件、Dashboard、monthly 账号）
- 变量命名建议使用：`MONTHLY_ACCOUNT_*`
- 代码中仍兼容旧变量：`PM_ANALYZE_*`

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

账号绑定说明：
- `position_analyze.py` / `position_analyze_gold.py` 显式使用 `analyze` profile

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

#### 运行 Web Dashboard（支持外网访问）

```bash
uv run app.py
```

说明：
- 服务监听 `0.0.0.0:5000`，可从同网络或公网映射后访问。
- 直接通过 IP 访问示例：`http://<你的公网IP>:5000`。
- 首次访问会进入登录页，输入 `DASHBOARD_PASSWORD` 后才可使用 Dashboard 和 API。
- 如需公网访问，建议结合云防火墙/反向代理（Nginx + HTTPS）仅开放必要端口。

systemd 账号绑定（已内置在 service 文件）：
- `auto-poly-app.service` 默认 `POLYMARKET_PROFILE=analyze`
- 所有服务从统一 `.env` 读取账号配置

服务运维约定（重要）：
- 后续重启相关服务请优先使用 `systemctl`，不要直接手动拉起 `uv run ...`，避免出现重复进程且确保 `Restart=` 自动拉起能力生效。
- 常用命令示例：
  - `sudo systemctl restart auto-poly-app.service`
  - `sudo systemctl restart auto-poly-recommendation-executor.service`
  - `sudo systemctl restart auto-poly-advisory-batch.service`
  - `sudo systemctl restart auto-poly-usdc-monitor.service`
  - `sudo systemctl status auto-poly-app.service --no-pager`

## 主要模块说明

### 入口脚本
- **position_analyze.py**：持仓分析主程序，整合持仓获取、订单匹配、AI 分析和报告生成
- **recommendation_auto_executor.py**：AI advisory 推荐自动执行器（含 BinanceBTCPriceWatcher 触发器）
- **app.py**：Flask Dashboard（手动下单、推荐查看、touch/calibration 监控）

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
