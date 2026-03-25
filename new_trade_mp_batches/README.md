# Mispricing 按批归档（每 500 窗口）

独立于现有服务：从源 tick 库读取 `btc-updown-5m-*`，用 **`new_trade`** 里的

- `indicators_4m.compute_indicators_4m`（经批量 tick 分组）
- `add_winning_direction`
- `backtest_mispricing.compute_mispricing`

生成与回测一致的 **双边 MP / pred / mispricing**，并按 **每 500 个窗口** 写入本目录下 **新的 SQLite 文件**。

## 路径

- 默认源库：`new_trade.config.get_db_path()`（一般为项目根 `tmp/trade.sqlite3`）
- 输出目录：本文件夹下 `data/`
- 文件名：`mp_batch_{batch_index:04d}_{first_ws}_{last_ws}.sqlite3`

## 用法

在项目根执行：

```bash
# 处理所有窗口，按 500 切分写入 data/
uv run python new_trade_mp_batches/export_mp_batches.py

# 指定源库
uv run python new_trade_mp_batches/export_mp_batches.py --source-db tmp/trade.sqlite3

# 只跑前 3 批（调试）
uv run python new_trade_mp_batches/export_mp_batches.py --max-batches 3

# 自定义批大小
uv run python new_trade_mp_batches/export_mp_batches.py --batch-size 500

# 已存在则跳过
uv run python new_trade_mp_batches/export_mp_batches.py --skip-existing
```

## 说明

- **与 `backtest_mispricing` 一致**：先对源库跑 `compute_all_windows_batch` → `add_winning_direction`，再按 `window_start_sec` **排序**，整表跑一次 `compute_mispricing`，最后按 **每 500 行** 切成多个 SQLite（滚动历史不会被批边界截断）。
- **内存**：会一次性载入所选窗口的 tick 并常驻一张完整指标+MP 表；数据量极大时请先用 `--max-windows` 试跑或在本机扩容内存。
- 表 `mp_windows` 为宽表；`export_meta` 记录批次、行数、源库路径、导出时间。
- **不修改**仓库内任何已有脚本，仅新增本目录。
