# 回测可直接用的排除 SQL 模板

## 模板1：按缺口区间排除窗口（推荐）

说明：当 5m 窗口 `[window_start, window_start+299]` 与任一缺口区间有交集时，排除该窗口。

```sql
WITH missing AS (
  -- 可替换成你导入的缺口表
  -- 这里给出示例结构
  SELECT unixepoch('2026-03-29 05:05:18') AS gap_start_sec, unixepoch('2026-03-29 14:28:02') AS gap_end_sec
  UNION ALL SELECT unixepoch('2026-03-28 22:10:20'), unixepoch('2026-03-29 03:22:16')
  UNION ALL SELECT unixepoch('2026-03-26 17:06:36'), unixepoch('2026-03-26 19:40:04')
),
candidate_windows AS (
  SELECT
    te.market_slug,
    CAST(substr(te.market_slug, length('btc-updown-5m-') + 1) AS INTEGER) AS window_start_sec
  FROM trade_events te
  WHERE te.side = 'buy'
)
SELECT cw.*
FROM candidate_windows cw
WHERE NOT EXISTS (
  SELECT 1
  FROM missing m
  WHERE
    -- 区间相交判定：
    -- window_end >= gap_start AND gap_end >= window_start
    (cw.window_start_sec + 299) >= m.gap_start_sec
    AND m.gap_end_sec >= cw.window_start_sec
);
```

## 模板2：按窗口内秒级数据完整性排除（更严格）

说明：要求窗口内至少有 N 条 tick（例如 `N=295`）。

```sql
WITH w_ticks AS (
  SELECT
    market_slug,
    window_start_ms / 1000 AS window_start_sec,
    COUNT(*) AS tick_cnt
  FROM btc_poly_1s_ticks
  GROUP BY market_slug, window_start_ms
),
candidate_windows AS (
  SELECT DISTINCT te.market_slug
  FROM trade_events te
  WHERE te.side = 'buy'
)
SELECT cw.market_slug
FROM candidate_windows cw
JOIN w_ticks wt ON wt.market_slug = cw.market_slug
WHERE wt.tick_cnt >= 295;  -- 可改成 300（最严）/ 290（更宽松）
```

## 使用建议

- 先用模板1（按主缺口时间段排除），可解释性最好。
- 再叠加模板2的 `tick_cnt` 下限，降低边缘缺失数据带来的噪声。
