# Fiber OB - 黄金 · 原油 · 比特币行情

黄金、WTI 原油与 BTC 价格网页。

## 跟踪品种

| 品种 | 代码 | 数据源 | 说明 |
|------|------|--------|------|
| 黄金 | GC=F | Yahoo Finance | COMEX 黄金期货，美元/盎司 |
| 原油 | CL=F | Yahoo Finance | WTI 原油期货，美元/桶 |
| 比特币 | BTCUSDT | Binance（公开 API） | 现货 24h 统计，USDT 计价 |

## 运行

在项目根目录执行：

```bash
python fiber_ob/app.py
```

浏览器访问：`http://127.0.0.1:5051`

## 环境变量

- `FIBER_OB_HOST`：监听地址，默认 `0.0.0.0`
- `FIBER_OB_PORT`：端口，默认 `5051`

## 数据源

- **Yahoo Finance**：黄金、原油（日线最近一根 K 线）
- **Binance** `data-api.binance.vision`：BTC 24h ticker（无需 API Key）
- 每 2 秒自动刷新（服务端对同一快照有约 2 秒缓存）
- 数据可能存在延迟，以交易所为准

## 功能

- 实时价格、涨跌幅
- 开盘价、最高价、最低价
- 成交量
- 响应式布局，支持手机端
