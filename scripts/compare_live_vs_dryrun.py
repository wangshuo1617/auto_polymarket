#!/usr/bin/env python3
"""对比两个策略实例在相同时间段中的交易行为异同。

用法:
  # 对比 ACC2 live vs long dry-run（默认）
  uv run scripts/compare_live_vs_dryrun.py

  # 指定时间范围
  uv run scripts/compare_live_vs_dryrun.py --since "2026-04-17 02:45:00"
  uv run scripts/compare_live_vs_dryrun.py --hours 6

  # 自定义两个数据源
  uv run scripts/compare_live_vs_dryrun.py \
    --dsn-a "host=127.0.0.1 dbname=polymarket_acc2 user=poly password=xxx" \
    --mode-a live --label-a "ACC2-live" \
    --dsn-b "host=127.0.0.1 dbname=polymarket user=poly password=xxx" \
    --mode-b dry-run --label-b "Long-dryrun"
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    print("需要 psycopg2: uv pip install psycopg2-binary")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 默认 DSN（从环境变量或 .env 读取）
# ---------------------------------------------------------------------------

def _load_env():
    """尝试从 .env 加载环境变量"""
    env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
    if not os.path.exists(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, v = line.split('=', 1)
            v = v.strip().strip('"').strip("'")
            if k.strip() not in os.environ:
                os.environ[k.strip()] = v

_load_env()

DEFAULT_DSN_A = os.environ.get('ACC2_PG_DSN', '')
DEFAULT_DSN_B = os.environ.get('PG_DSN', '')


def fetch_trades(dsn: str, mode: str, since: datetime, until: datetime | None):
    """从指定数据库拉取交易窗口记录，按 market_slug 排序"""
    conn = psycopg2.connect(dsn)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            sql = """
                SELECT market_slug, direction, entry_price, entry_size, entry_usdc,
                       btc_entry_price, status, pnl, exit_reason, entry_time, exit_time,
                       mode, entry_diagnostics
                FROM trade_window_summary
                WHERE mode = %s AND entry_time >= %s
            """
            params = [mode, since]
            if until:
                sql += " AND entry_time <= %s"
                params.append(until)
            sql += " ORDER BY market_slug"
            cur.execute(sql, params)
            return {r['market_slug']: dict(r) for r in cur.fetchall()}
    finally:
        conn.close()


def slug_to_window_ts(slug: str) -> int:
    """从 market_slug 提取窗口时间戳（末尾数字）"""
    parts = slug.rsplit('-', 1)
    try:
        return int(parts[-1])
    except (ValueError, IndexError):
        return 0


def fmt_price(v):
    return f"{v:.4f}" if v is not None else "-"


def fmt_pnl(v):
    if v is None:
        return "-"
    sign = "+" if v >= 0 else ""
    return f"{sign}{v:.4f}"


def print_separator(char='─', width=120):
    print(char * width)


def compare(trades_a: dict, trades_b: dict, label_a: str, label_b: str):
    """对比两组交易，输出详细报告"""
    all_slugs = sorted(set(trades_a.keys()) | set(trades_b.keys()), key=slug_to_window_ts)

    both = []       # 双方都入场
    only_a = []     # 仅 A 入场
    only_b = []     # 仅 B 入场

    for slug in all_slugs:
        a, b = trades_a.get(slug), trades_b.get(slug)
        if a and b:
            both.append((slug, a, b))
        elif a:
            only_a.append((slug, a))
        else:
            only_b.append((slug, b))

    # ── 汇总 ──
    print()
    print_separator('═')
    print(f"  策略交易行为对比: {label_a} vs {label_b}")
    print_separator('═')

    total_windows = len(all_slugs)
    print(f"\n  观察窗口总数: {total_windows}")
    print(f"  双方均入场:   {len(both)}")
    print(f"  仅 {label_a}: {len(only_a)}")
    print(f"  仅 {label_b}: {len(only_b)}")

    # ── PnL 汇总 ──
    pnl_a = sum(t.get('pnl') or 0 for t in trades_a.values())
    pnl_b = sum(t.get('pnl') or 0 for t in trades_b.values())
    won_a = sum(1 for t in trades_a.values() if t['status'] == 'won')
    won_b = sum(1 for t in trades_b.values() if t['status'] == 'won')
    lost_a = sum(1 for t in trades_a.values() if t['status'] == 'lost')
    lost_b = sum(1 for t in trades_b.values() if t['status'] == 'lost')

    print(f"\n  {'指标':<20} {label_a:>15} {label_b:>15}")
    print(f"  {'─'*20} {'─'*15} {'─'*15}")
    print(f"  {'总入场数':<20} {len(trades_a):>15} {len(trades_b):>15}")
    print(f"  {'胜':<20} {won_a:>15} {won_b:>15}")
    print(f"  {'负':<20} {lost_a:>15} {lost_b:>15}")
    print(f"  {'PnL':<20} {fmt_pnl(pnl_a):>15} {fmt_pnl(pnl_b):>15}")

    # ── 双方都入场的窗口详细对比 ──
    if both:
        print(f"\n{'─'*120}")
        print(f"  双方均入场的窗口 ({len(both)})")
        print(f"{'─'*120}")

        direction_match = 0
        price_diffs = []

        hdr = f"  {'窗口':<30} {'方向':^12} {'入场价':^20} {'状态':^20} {'PnL':^20}"
        print(hdr)
        sub = f"  {'':<30} {label_a+'/'+label_b:^12} {label_a+'/'+label_b:^20} {label_a+'/'+label_b:^20} {label_a+'/'+label_b:^20}"
        print(sub)
        print(f"  {'─'*118}")

        for slug, a, b in both:
            dir_a, dir_b = a['direction'], b['direction']
            dir_match = '✓' if dir_a == dir_b else '✗'
            if dir_a == dir_b:
                direction_match += 1

            dir_str = f"{dir_a}/{dir_b} {dir_match}"
            price_str = f"{fmt_price(a['entry_price'])}/{fmt_price(b['entry_price'])}"
            status_str = f"{a['status']}/{b['status']}"
            pnl_str = f"{fmt_pnl(a.get('pnl'))}/{fmt_pnl(b.get('pnl'))}"

            if a['entry_price'] and b['entry_price']:
                price_diffs.append(abs(a['entry_price'] - b['entry_price']))

            print(f"  {slug:<30} {dir_str:^12} {price_str:^20} {status_str:^20} {pnl_str:^20}")

        print(f"\n  方向一致率: {direction_match}/{len(both)} ({direction_match/len(both)*100:.1f}%)")
        if price_diffs:
            avg_diff = sum(price_diffs) / len(price_diffs)
            max_diff = max(price_diffs)
            print(f"  入场价差: 平均 {avg_diff:.4f}, 最大 {max_diff:.4f}")

    # ── 仅一方入场 ──
    for label, items, other_label in [(label_a, only_a, label_b), (label_b, only_b, label_a)]:
        if items:
            print(f"\n{'─'*120}")
            print(f"  仅 {label} 入场 ({len(items)}) — {other_label} 未入场")
            print(f"{'─'*120}")
            print(f"  {'窗口':<30} {'方向':>6} {'入场价':>10} {'状态':>12} {'PnL':>10} {'退出原因'}")
            print(f"  {'─'*118}")
            for slug, t in items:
                print(f"  {slug:<30} {t['direction']:>6} {fmt_price(t['entry_price']):>10}"
                      f" {t['status']:>12} {fmt_pnl(t.get('pnl')):>10} {t.get('exit_reason') or ''}")

    print()
    print_separator('═')


def main():
    parser = argparse.ArgumentParser(description='对比两个策略实例的交易行为')
    parser.add_argument('--dsn-a', default=DEFAULT_DSN_A, help='数据源 A 的 PG DSN')
    parser.add_argument('--mode-a', default='live', help='数据源 A 的 mode (默认: live)')
    parser.add_argument('--label-a', default='ACC2-live', help='数据源 A 的标签')
    parser.add_argument('--dsn-b', default=DEFAULT_DSN_B, help='数据源 B 的 PG DSN')
    parser.add_argument('--mode-b', default='dry-run', help='数据源 B 的 mode (默认: dry-run)')
    parser.add_argument('--label-b', default='Long-dryrun', help='数据源 B 的标签')
    parser.add_argument('--since', help='开始时间 (ISO 格式，默认: 数据源 A 最早入场时间)')
    parser.add_argument('--until', help='结束时间 (ISO 格式)')
    parser.add_argument('--hours', type=float, help='回看小时数 (与 --since 互斥)')
    args = parser.parse_args()

    if not args.dsn_a or not args.dsn_b:
        print("错误: 需要提供 --dsn-a 和 --dsn-b，或设置 ACC2_PG_DSN / PG_DSN 环境变量")
        sys.exit(1)

    # 确定时间范围
    now = datetime.now(timezone.utc)
    if args.hours:
        since = now - timedelta(hours=args.hours)
    elif args.since:
        since = datetime.fromisoformat(args.since)
        if since.tzinfo is None:
            since = since.replace(tzinfo=timezone.utc)
    else:
        # 默认: 取数据源 A 的最早记录时间
        conn = psycopg2.connect(args.dsn_a)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT min(entry_time) FROM trade_window_summary WHERE mode=%s", (args.mode_a,))
                row = cur.fetchone()
                if row and row[0]:
                    since = row[0]
                else:
                    since = now - timedelta(hours=1)
        finally:
            conn.close()

    until = None
    if args.until:
        until = datetime.fromisoformat(args.until)
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)

    print(f"  时间范围: {since.isoformat()} → {args.until or '现在'}")

    trades_a = fetch_trades(args.dsn_a, args.mode_a, since, until)
    trades_b = fetch_trades(args.dsn_b, args.mode_b, since, until)

    compare(trades_a, trades_b, args.label_a, args.label_b)


if __name__ == '__main__':
    main()
