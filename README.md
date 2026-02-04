# Auto Polymarket

一个自动化 Polymarket 交易分析系统，集成 AI 市场研究、实时价格监控和持仓分析功能。

## 功能特性

### 🤖 AI 驱动的市场分析
- 使用 Google Gemini API 进行智能市场研究
- 基于持仓数据和 BTC 价格趋势生成交易建议
- 自动生成结构化的分析报告（防守端、进攻端、预警信号）

### 📊 实时价格监控
- 通过 Binance WebSocket 实时监控 BTC/USDT 价格
- 支持多种价格流类型（ticker、bookTicker、avgPrice）
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
├── position_analyze.py      # 持仓分析主程序
├── btc_price_watcher.py     # BTC 价格监控服务
├── order_func.py            # Polymarket 订单操作
├── gemini_researcher.py     # Gemini AI 市场研究
├── email_alert.py           # 邮件发送服务
├── html_generator.py        # HTML 报告生成器
├── config.py                # 配置文件
├── price_warn_config.py     # 价格预警配置（自动生成）
├── auto_polymarket.sh       # 自动化执行脚本
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

#### 运行价格监控

```bash
uv run btc_price_watcher.py
```

#### 运行月初建仓建议

```bash
uv run monthly_btc_strategy.py
```

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

### position_analyze.py
持仓分析主程序，整合了持仓获取、订单匹配、AI 分析和报告生成功能。

### btc_price_watcher.py
BTC 价格监控服务，支持多种 WebSocket 流类型，自动重连，可配置的价格预警。

### gemini_researcher.py
使用 Google Gemini API 进行市场研究，支持 Google Search Grounding，生成结构化的分析结果。

### order_func.py
Polymarket 订单操作模块，支持：
- 买入订单 (`buy_order`)
- 卖出订单 (`sell_order`)
- 取消订单 (`cancel_order`)
- 获取挂单 (`get_open_orders`)
- 获取订单历史 (`get_order_history`)

### email_alert.py
邮件发送服务，支持纯文本和 HTML 格式邮件。

### html_generator.py
生成美观的 HTML 格式分析报告，包含持仓详情、AI 分析结果和预警信息。

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
