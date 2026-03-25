# mp_new_trade

独立于现有交易进程：**不修改** `new_trade/`、`5m_trade.py`。

## 使用方式（省资源）

- **不设常驻服务**：由你在本机用 **cron / Windows 计划任务** 定时执行一次即可。
- **每批固定窗口数**：`settings.py` 里 `BATCH_WINDOWS = 500`（仅此一处改批量；策略数字在 `mispricing_core.py`）。
- **无多余 CLI 参数**：平时只跑 `python -m mp_new_trade`；仅维护时用 `--reset-cursor`。

## 固定配置

| 项 | 位置 |
|----|------|
| 每批窗口数 | `settings.py` → `BATCH_WINDOWS` |
| 源 tick 库 | 默认 `config.SQLITE_DB_PATH`，或环境变量 `MP_NEW_TRADE_SOURCE_DB` |
| 写入库 | 默认 `mp_new_trade/data/mp_batch.sqlite3`，或 `MP_NEW_TRADE_LOCAL_DB` |
| 阈值 / 分档 | `mispricing_core.py`（与实盘 `5m_trade_mispricing` 对齐，需手工同步） |

## 命令

```bash
# 仓库根目录：导入下一批 500 窗（或 BATCH_WINDOWS）
python -m mp_new_trade

# 安静模式（少日志）
python -m mp_new_trade.accumulate_cli -q

# 清空游标，下一批从最早窗口重跑（慎用）
python -m mp_new_trade.accumulate_cli --reset-cursor
```

## 游标

表 `mp_meta.last_window_sec_exclusive`：下次运行只处理 **更大** 的 `window_start_sec`，避免重复。

## 本地库

- `mp_batches`：批次元数据、`params_json`
- `mp_snapshots`：每窗建仓判断与 MP 快照

## 依赖

`pandas`、`numpy`；需能从仓库根 `import config`（模块方式运行即可）。
