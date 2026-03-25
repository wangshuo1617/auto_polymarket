from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .models import TradeRecord


def _calc_slippage_bps(trades: List[TradeRecord]) -> Optional[float]:
    values: List[float] = []
    for t in trades:
        if (
            t.entry_best_ask is None
            or t.entry_avg_fill_price is None
            or t.entry_best_ask <= 0
        ):
            continue
        values.append((t.entry_avg_fill_price - t.entry_best_ask) / t.entry_best_ask * 10000.0)
    if not values:
        return None
    return sum(values) / len(values)


def _calc_full_fill_rate(trades: List[TradeRecord]) -> Optional[float]:
    flags: List[bool] = []
    for t in trades:
        if t.entry_full_fill is not None:
            flags.append(bool(t.entry_full_fill))
        if t.exit_full_fill is not None:
            flags.append(bool(t.exit_full_fill))
    if not flags:
        return None
    return sum(1 for item in flags if item) / len(flags)


def _profit_factor(trades: List[TradeRecord]) -> Optional[float]:
    gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
    if gross_loss <= 1e-12:
        if gross_profit > 0:
            return float("inf")
        return None
    return gross_profit / gross_loss


def _ev_per_trade(trades: List[TradeRecord]) -> Optional[float]:
    if not trades:
        return None
    return sum(t.pnl for t in trades) / len(trades)


def _slippage_leakage(trades: List[TradeRecord]) -> float:
    return sum(float(t.exit_slippage_leakage or 0.0) for t in trades)


def _base_reason(reason: str) -> str:
    if reason.endswith("_partial"):
        return reason[: -len("_partial")]
    if reason.endswith("_residual"):
        return reason[: -len("_residual")]
    return reason


def _settlement_first_lines(
    report_interval_sec: int,
    api_hourly: Optional[Dict[str, Any]],
    api_cumulative: Optional[Dict[str, Any]],
) -> List[str]:
    """主口径：Gamma resolution（outcome 0/1）倒算经济盈亏；与 REDEEM 到账时间无关。"""
    out: List[str] = [
        "【结算盈亏 — Resolution 倒算（主口径）】",
        "  依据: Polymarket Gamma 市场 outcomePrices 已分胜负（Up/Down）；净头寸仅来自链上 **TRADE**（不含 REDEEM 调仓）。",
        "  持有至结算（无 CLOB 卖出）时: 单盘盈亏 ≈ 胜方净(TRADE)份额×1 USDC − 该盘买入成本；卖出收入视为 0。",
        "  若存在卖出，公式仍为: 胜方净份额×1 + 卖出收入 − 买入支出。",
        "  未分胜负 / Gamma 拉取失败 的盘不计入下方「合计」（与邮件标题主数字一致）。",
    ]
    if api_hourly is not None:
        hb = api_hourly.get("slug_blend_pnl_total_usdc")
        h_s = f"{float(hb):.2f}" if hb is not None else "N/A"
        out.append(f"- 本报告期（约 {report_interval_sec // 60} 分钟）已结算盘合计: {h_s} USDC")
    if api_cumulative is not None:
        cb = api_cumulative.get("slug_blend_pnl_total_usdc")
        c_s = f"{float(cb):.2f}" if cb is not None else "N/A"
        out.append(f"- 本进程时间窗内已结算盘合计: {c_s} USDC")
    out.append("")
    return out


def _activity_pnl_lines(
    label_period: str,
    label_session: str,
    api_hourly: Optional[Dict[str, Any]],
    api_cumulative: Optional[Dict[str, Any]],
    *,
    prefer_settlement_final: bool = False,
) -> List[str]:
    """链上 Activity 流水：净值 = 卖出 + 赎回 − 买入（资金流水对照）。"""
    title = (
        "【链上 Activity 流水净值】（含 REDEEM；**资金流水对照**，主结论见上「结算盈亏」）"
        if prefer_settlement_final
        else "【链上 Activity 净值】（含 BUY / SELL / REDEEM；与 CLOB 卖出平仓统计不同）"
    )
    out: List[str] = [
        title,
        "  公式: 流水净额 = 卖出收入 + 赎回收入 − 买入支出",
    ]
    if api_hourly is not None:
        h_net = float(api_hourly.get("net_pnl", 0.0))
        h_sell_inc = float(api_hourly.get("income_trade_sell", 0.0))
        h_redeem_inc = float(api_hourly.get("income_redeem", 0.0))
        h_exp = float(api_hourly.get("expense_trade_buy", 0.0))
        h_buy = int(api_hourly.get("count_trade_buy", 0) or 0)
        h_sell = int(api_hourly.get("count_trade_sell", 0) or 0)
        h_rd = int(api_hourly.get("count_redeem", 0) or 0)
        out.append(
            f"- {label_period}: 净 {h_net:.2f} USDC | 买入支出 {h_exp:.2f} | "
            f"卖出收入 {h_sell_inc:.2f} ({h_sell} 笔) | 赎回收入 {h_redeem_inc:.2f} ({h_rd} 笔) | 买入 {h_buy} 笔"
        )
    else:
        out.append(f"- {label_period}: 拉取失败")
    if api_cumulative is not None:
        c_net = float(api_cumulative.get("net_pnl", 0.0))
        c_sell_inc = float(api_cumulative.get("income_trade_sell", 0.0))
        c_redeem_inc = float(api_cumulative.get("income_redeem", 0.0))
        c_exp = float(api_cumulative.get("expense_trade_buy", 0.0))
        c_buy = int(api_cumulative.get("count_trade_buy", 0) or 0)
        c_sell = int(api_cumulative.get("count_trade_sell", 0) or 0)
        c_rd = int(api_cumulative.get("count_redeem", 0) or 0)
        out.append(
            f"- {label_session}: 净 {c_net:.2f} USDC | 买入支出 {c_exp:.2f} | "
            f"卖出收入 {c_sell_inc:.2f} ({c_sell} 笔) | 赎回收入 {c_redeem_inc:.2f} ({c_rd} 笔) | 买入 {c_buy} 笔"
        )
    else:
        out.append(f"- {label_session}: 拉取失败")
    out.append("")
    return out


def _slug_pnl_and_mtm_lines(api_cumulative: Optional[Dict[str, Any]], max_rows: int = 35) -> List[str]:
    """各 slug 明细（展示模式依 prefer_settlement_final 而异）。"""
    if not api_cumulative:
        return []
    summ = api_cumulative.get("slug_summary") or []
    if not summ:
        return []
    prefer = bool(api_cumulative.get("prefer_settlement_final"))
    if prefer:
        out = [
            "",
            "【各盘明细：结算最终结果】",
            "  已分胜负：settlement_final = 胜方净(TRADE)×1 + 卖出 − 买入（净头寸不含 REDEEM）。",
            "  未分胜负 / Gamma 失败：本条无结算数字，不计入邮件标题「结算盈亏」合计。",
            "  净Up/净Down(全量) 含 REDEEM 调整；括号内 trade_only 为仅 TRADE。",
        ]
    else:
        out = [
            "",
            "【各盘明细：真实赎回 vs 盘末反推】",
            "  净份额：本时间窗内链上 TRADE/可解析 REDEEM 推导。",
            "  已赎回：本条收益以 Activity 为准（net_pnl，含 redeem/sell）。",
            "  未赎回：拉 Gamma 盘末价；若已分胜负，则 应收≈净胜方份额×1 USDC，反推收益=应收−买入成本（非中间价 MTM）。",
            "  未分胜负或仅对部分 slug 拉取时，反推可能为空。启动前已有仓位时净份额可能低估。",
        ]
    c_profit = api_cumulative.get("slug_profit_count", 0)
    c_loss = api_cumulative.get("slug_loss_count", 0)
    c_flat = api_cumulative.get("slug_flat_count", 0)
    out.append(f"- slug 盈亏家数 盈利/亏损/持平: {c_profit} / {c_loss} / {c_flat}")
    tot_blend = api_cumulative.get("slug_blend_pnl_total_usdc")
    if tot_blend is None:
        tot_blend = api_cumulative.get("slug_mtm_total_usdc")
    if tot_blend is not None:
        if prefer:
            out.append(
                f"- Resolution 结算合计（settlement_final 求和）: {float(tot_blend):.2f} USDC "
                f"（未覆盖未分胜负的盘）"
            )
        else:
            out.append(
                f"- 混合合计（已赎回用真实 + 未赎回且已反推）: {float(tot_blend):.2f} USDC "
                f"（未覆盖未反推的盘）"
            )

    cap = max(1, int(max_rows))
    for row in summ[:cap]:
        slug = str(row.get("slug") or "")[:80]
        npnl = float(row.get("net_pnl", 0) or 0)
        nu = row.get("net_shares_up")
        nd = row.get("net_shares_down")
        nut = row.get("net_shares_up_trade_only")
        ndt = row.get("net_shares_down_trade_only")
        redeemed = bool(row.get("redeemed"))
        est = row.get("settlement_est_pnl_usdc")
        disp = row.get("display_round_pnl_usdc")
        winner = row.get("resolution_winner")
        note = str(row.get("settlement_note") or "")
        mode = str(row.get("pnl_display_mode") or "")
        est_s = f"{float(est):.2f}" if est is not None else "N/A"
        disp_s = f"{float(disp):.2f}" if disp is not None else "N/A"
        if prefer:
            pos_s = f"净Up {nu} 净Down {nd} (trade {nut}/{ndt})"
            out.append(
                f"  · {slug} | 结算 {disp_s} | Activity流水净 {npnl:.2f} | {pos_s} | "
                f"反推列 {est_s} | 胜方 {winner} | {mode} | {note}"
            )
        else:
            tag = "真实(已赎回)" if redeemed else "反推(未赎回)"
            out.append(
                f"  · {slug} | {tag} | Activity净 {npnl:.2f} | 净Up {nu} 净Down {nd} | "
                f"反推收益 {est_s} | 胜方 {winner} | {mode} | {note}"
            )
    if len(summ) > cap:
        out.append(f"  … 其余 {len(summ) - cap} 个 slug 略")
    return out


def _activity_derived_key_stats(
    api: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """从 Activity 或（优先）结算口径推导 slug 统计。"""
    if not api:
        return None
    prefer = bool(api.get("prefer_settlement_final"))
    blend = api.get("slug_blend_pnl_total_usdc")
    if prefer and blend is not None:
        net = float(blend)
    else:
        net = float(api.get("net_pnl", 0) or 0)
    cb = int(api.get("count_trade_buy", 0) or 0)
    ev_per_buy = (net / cb) if cb > 0 else None

    slug_summ = api.get("slug_summary") or []
    if prefer:
        settled = [
            s
            for s in slug_summ
            if s.get("pnl_display_mode") == "settlement_final"
            and s.get("display_round_pnl_usdc") is not None
        ]
        n_slugs = len(settled)
        prof = sum(1 for s in settled if float(s.get("display_round_pnl_usdc") or 0) > 0)
        win_rate_slugs = (prof / n_slugs * 100.0) if n_slugs > 0 else None
        gross_profit = sum(
            float(s.get("display_round_pnl_usdc") or 0)
            for s in settled
            if float(s.get("display_round_pnl_usdc") or 0) > 0
        )
        gross_loss = abs(
            sum(
                float(s.get("display_round_pnl_usdc") or 0)
                for s in settled
                if float(s.get("display_round_pnl_usdc") or 0) < 0
            )
        )
    else:
        n_slugs = len(slug_summ)
        prof = int(api.get("slug_profit_count", 0) or 0)
        win_rate_slugs = (prof / n_slugs * 100.0) if n_slugs > 0 else None

        gross_profit = sum(
            float(s.get("net_pnl") or 0) for s in slug_summ if float(s.get("net_pnl") or 0) > 0
        )
        gross_loss = abs(
            sum(float(s.get("net_pnl") or 0) for s in slug_summ if float(s.get("net_pnl") or 0) < 0)
        )
    pf_slugs: Optional[float]
    if gross_loss > 1e-9:
        pf_slugs = gross_profit / gross_loss
    elif gross_profit > 0:
        pf_slugs = float("inf")
    else:
        pf_slugs = None

    loss_ct = (
        sum(
            1
            for s in slug_summ
            if s.get("pnl_display_mode") == "settlement_final"
            and s.get("display_round_pnl_usdc") is not None
            and float(s.get("display_round_pnl_usdc") or 0) < 0
        )
        if prefer
        else int(api.get("slug_loss_count", 0) or 0)
    )
    return {
        "net_pnl": net,
        "count_trade_buy": cb,
        "ev_per_buy": ev_per_buy,
        "n_slugs": n_slugs,
        "slug_profit_count": prof,
        "slug_loss_count": loss_ct,
        "win_rate_slugs": win_rate_slugs,
        "profit_factor_slugs": pf_slugs,
    }


def _format_pf(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    if val == float("inf"):
        return "INF"
    return f"{val:.4f}"


def _reason_breakdown(trades: List[TradeRecord]) -> Dict[str, Dict[str, float]]:
    groups: Dict[str, List[TradeRecord]] = {}
    for t in trades:
        key = _base_reason(t.reason)
        groups.setdefault(key, []).append(t)

    result: Dict[str, Dict[str, float]] = {}
    for key, items in groups.items():
        count = len(items)
        wins = sum(1 for item in items if item.pnl > 0)
        avg_pnl = sum(item.pnl for item in items) / count if count > 0 else 0.0
        avg_loss_items = [item.pnl for item in items if item.pnl < 0]
        avg_loss = sum(avg_loss_items) / len(avg_loss_items) if avg_loss_items else 0.0
        result[key] = {
            "count": float(count),
            "wins": float(wins),
            "win_rate": (wins / count * 100.0) if count > 0 else 0.0,
            "avg_pnl": avg_pnl,
            "avg_loss": avg_loss,
        }
    return result


def build_pnl_report_content_and_subject(
    *,
    report_interval_sec: int,
    new_trades: List[TradeRecord],
    all_trades: List[TradeRecord],
    latency_snapshot: Dict[str, List[float]],
    latency_indices: Dict[str, int],
    source_counts_snapshot: Dict[str, int],
    source_counts_index: Dict[str, int],
    format_latency_summary: Callable[[str, List[float]], str],
    api_pnl_hourly: Optional[Dict[str, Any]] = None,
    api_pnl_cumulative: Optional[Dict[str, Any]] = None,
    db_realized_hourly: Optional[Dict[str, Any]] = None,
    db_realized_cumulative: Optional[Dict[str, Any]] = None,
    db_entry_hourly: Optional[Dict[str, Any]] = None,
    db_entry_cumulative: Optional[Dict[str, Any]] = None,
    prev_hour_pending_slugs: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[str, str, List[Dict[str, Any]]]:
    hourly_pnl = sum(t.pnl for t in new_trades)
    hourly_count = len(new_trades)
    cumulative_pnl = sum(t.pnl for t in all_trades)
    cumulative_count = len(all_trades)

    lines: List[str] = []
    prefer_sf = bool(
        (api_pnl_cumulative or {}).get("prefer_settlement_final")
        or (api_pnl_hourly or {}).get("prefer_settlement_final")
    )
    if api_pnl_hourly is not None or api_pnl_cumulative is not None:
        if prefer_sf:
            lines.extend(
                _settlement_first_lines(
                    report_interval_sec,
                    api_pnl_hourly,
                    api_pnl_cumulative,
                )
            )
        lines.extend(
            _activity_pnl_lines(
                label_period=f"本报告期（约 {report_interval_sec // 60} 分钟窗口）",
                label_session="本进程启动至今",
                api_hourly=api_pnl_hourly,
                api_cumulative=api_pnl_cumulative,
                prefer_settlement_final=prefer_sf,
            )
        )
    if db_realized_hourly is not None and db_realized_cumulative is not None:
        lines.extend(
            [
                "【SQLite：仅 CLOB 卖出平仓】（每笔 = 卖出收回 − 开仓成本；若策略只靠 redeem 结算、不在订单簿卖出，此处通常接近 0）",
                (
                    f"- 本报告期（按平仓时间）: {db_realized_hourly['sell_count']} 笔平仓，"
                    f"已实现 {db_realized_hourly['total_pnl']:.2f} USDC "
                    f"(卖出收回 {db_realized_hourly['total_recovered_usdc']:.2f}，"
                    f"对应成本约 {db_realized_hourly['total_entry_cost_estimate']:.2f})"
                ),
                (
                    f"- 本进程启动至今（按平仓时间）: {db_realized_cumulative['sell_count']} 笔平仓，"
                    f"已实现 {db_realized_cumulative['total_pnl']:.2f} USDC "
                    f"(卖出收回 {db_realized_cumulative['total_recovered_usdc']:.2f}，"
                    f"对应成本约 {db_realized_cumulative['total_entry_cost_estimate']:.2f})"
                ),
            ]
        )
        if db_entry_hourly is not None:
            lines.append(
                f"- 同期开仓（按开仓时间，可与平仓不同窗）: {db_entry_hourly['buy_count']} 笔，"
                f"买入支出约 {db_entry_hourly['total_spent_usdc']:.2f} USDC"
            )
        if db_entry_cumulative is not None:
            lines.append(
                f"- 启动至今开仓（按开仓时间）: {db_entry_cumulative['buy_count']} 笔，"
                f"买入支出约 {db_entry_cumulative['total_spent_usdc']:.2f} USDC"
            )
        lines.append("")
    elif api_pnl_hourly is None and api_pnl_cumulative is None:
        lines.extend(
            [
                f"过去 {report_interval_sec // 60} 分钟新交易共 {hourly_count} 笔，总盈亏：{hourly_pnl:.2f} USDC",
                f"服务启动以来累计交易 {cumulative_count} 笔，累计盈亏：{cumulative_pnl:.2f} USDC",
                "（链上 Activity 拉取失败且未配置 SQLite 时仅显示内存口径）",
                "",
            ]
        )

    mem_hdr = "【本进程内存平仓】（重启后清零；与链上 redeem 结算无直接对应）"
    if prefer_sf:
        mem_hdr += " — **持有至结算策略请以「结算盈亏」为准，本节常为 0 属正常**"
    lines.extend(
        [
            mem_hdr,
            f"- 过去 {report_interval_sec // 60} 分钟新平仓 {hourly_count} 笔，总盈亏：{hourly_pnl:.2f} USDC",
            f"- 自启动以来平仓 {cumulative_count} 笔，累计盈亏：{cumulative_pnl:.2f} USDC",
            "",
        ]
    )

    recent_100 = all_trades[-100:]
    rolling_base = len(recent_100)
    rolling_tp_count = sum(1 for t in recent_100 if t.reason == "tp")
    rolling_win_rate = (rolling_tp_count / rolling_base * 100.0) if rolling_base > 0 else 0.0

    hourly_slippage_bps = _calc_slippage_bps(new_trades)
    cumulative_slippage_bps = _calc_slippage_bps(all_trades)
    hourly_full_fill_rate = _calc_full_fill_rate(new_trades)
    cumulative_full_fill_rate = _calc_full_fill_rate(all_trades)
    hourly_profit_factor = _profit_factor(new_trades)
    cumulative_profit_factor = _profit_factor(all_trades)
    hourly_ev = _ev_per_trade(new_trades)
    cumulative_ev = _ev_per_trade(all_trades)
    hourly_leakage = _slippage_leakage(new_trades)
    cumulative_leakage = _slippage_leakage(all_trades)
    hourly_reason_stats = _reason_breakdown(new_trades)
    cumulative_reason_stats = _reason_breakdown(all_trades)

    act_stats_h = _activity_derived_key_stats(api_pnl_hourly) if api_pnl_hourly else None
    act_stats_c = _activity_derived_key_stats(api_pnl_cumulative) if api_pnl_cumulative else None
    if act_stats_h is not None or act_stats_c is not None:
        if prefer_sf:
            lines.append("【关键策略统计 — Resolution 结算口径】（与上方「结算盈亏」合计一致）")
            lines.append(
                "- 口径: 仅统计 Gamma 已给出 settlement_final 的 slug；"
                "EV/已结算盘 = 结算合计 ÷ 已结算 slug 数（**非**链上 BUY 笔数，避免与持有至结算策略错位）；"
                "盘胜率 / PF 同基于 settlement_final。"
            )
        else:
            lines.append("【关键策略统计 — 链上 Activity】（结算型策略可对照本节）")
            lines.append(
                "- 口径: 与上方「链上 Activity 净值」同源；"
                "EV/买入笔 = 窗口流水净额 ÷ 链上 BUY 笔数；"
                "盘胜率 = 净额>0 的 slug 数 ÷ 有活动的 slug 总数；"
                "Profit Factor(slug) = 各 slug 净盈利之和 ÷ |各 slug 净亏损之和|。"
            )
        if prefer_sf:
            if act_stats_h is None:
                lines.append("- EV/已结算盘(本报告期): N/A")
            elif act_stats_h.get("ev_per_settled_slug") is None:
                lines.append(
                    f"- EV/已结算盘(本报告期): N/A（本窗口无已结算 slug，结算合计 {act_stats_h['net_pnl']:.2f} USDC）"
                )
            else:
                lines.append(
                    f"- EV/已结算盘(本报告期): {act_stats_h['ev_per_settled_slug']:.4f} USDC "
                    f"（结算合计 {act_stats_h['net_pnl']:.2f} / 已结算 {act_stats_h['n_slugs']} 盘）"
                )
            if act_stats_c is None:
                lines.append("- EV/已结算盘(启动至今): N/A")
            elif act_stats_c.get("ev_per_settled_slug") is None:
                lines.append(
                    f"- EV/已结算盘(启动至今): N/A（无已结算 slug，合计 {act_stats_c['net_pnl']:.2f} USDC）"
                )
            else:
                lines.append(
                    f"- EV/已结算盘(启动至今): {act_stats_c['ev_per_settled_slug']:.4f} USDC "
                    f"（结算合计 {act_stats_c['net_pnl']:.2f} / 已结算 {act_stats_c['n_slugs']} 盘）"
                )
        else:
            if act_stats_h is None:
                lines.append("- EV/买入笔(本报告期): N/A（本窗口未拉取 Activity 或数据为空）")
            elif act_stats_h["ev_per_buy"] is None:
                lines.append(
                    f"- EV/买入笔(本报告期): N/A（本窗口无链上 BUY，净盈亏 {act_stats_h['net_pnl']:.2f} USDC 可能主要来自 redeem/SELL）"
                )
            else:
                lines.append(
                    f"- EV/买入笔(本报告期): {act_stats_h['ev_per_buy']:.4f} USDC "
                    f"（净 {act_stats_h['net_pnl']:.2f} / 买 {act_stats_h['count_trade_buy']} 笔）"
                )
            if act_stats_c is None:
                lines.append("- EV/买入笔(启动至今): N/A")
            elif act_stats_c["ev_per_buy"] is None:
                lines.append(
                    f"- EV/买入笔(启动至今): N/A（无链上 BUY 记录，净 {act_stats_c['net_pnl']:.2f} USDC）"
                )
            else:
                lines.append(
                    f"- EV/买入笔(启动至今): {act_stats_c['ev_per_buy']:.4f} USDC "
                    f"（净 {act_stats_c['net_pnl']:.2f} / 买 {act_stats_c['count_trade_buy']} 笔）"
                )

        if act_stats_h is None or act_stats_h["win_rate_slugs"] is None:
            lines.append("- 盘胜率 slug 维度(本报告期): N/A")
        else:
            ah = act_stats_h
            lines.append(
                f"- 盘胜率 slug 维度(本报告期): {ah['win_rate_slugs']:.2f}% "
                f"（盈 {ah['slug_profit_count']}/总 {ah['n_slugs']}）"
            )
        if act_stats_c is None or act_stats_c["win_rate_slugs"] is None:
            lines.append("- 盘胜率 slug 维度(启动至今): N/A")
        else:
            ac = act_stats_c
            lines.append(
                f"- 盘胜率 slug 维度(启动至今): {ac['win_rate_slugs']:.2f}% "
                f"（盈 {ac['slug_profit_count']}/总 {ac['n_slugs']}）"
            )

        pf_h = act_stats_h["profit_factor_slugs"] if act_stats_h else None
        pf_c = act_stats_c["profit_factor_slugs"] if act_stats_c else None
        lines.append(f"- Profit Factor slug 净额(本报告期): {_format_pf(pf_h)}")
        lines.append(f"- Profit Factor slug 净额(启动至今): {_format_pf(pf_c)}")
        lines.append(
            "- 真实滑点率 / 订单全成率: 链上 Activity 无此字段；"
            "见下方「进程内 CLOB 平仓记录」或成交日志。"
        )
        lines.append("")

    lines.append(
        "【关键策略统计 — 进程内 CLOB 平仓记录】"
        "（仅写入内存 TradeRecord 的平仓；纯 redeem 结算、未走订单簿平仓时多为 N/A 属预期）"
    )
    if hourly_slippage_bps is None:
        lines.append("- 真实滑点率(本小时): N/A")
    else:
        lines.append(f"- 真实滑点率(本小时): {hourly_slippage_bps:.2f} bps")
    if cumulative_slippage_bps is None:
        lines.append("- 真实滑点率(累计): N/A")
    else:
        lines.append(f"- 真实滑点率(累计): {cumulative_slippage_bps:.2f} bps")

    lines.append(
        f"- 滚动100单胜率(tp占比): {rolling_win_rate:.2f}% ({rolling_tp_count}/{rolling_base})"
        "（仅内存平仓且 reason=tp；redeem 结算策略常为 0/0）"
    )

    if hourly_full_fill_rate is None:
        lines.append("- 订单成交率(本小时，全成占比): N/A")
    else:
        lines.append(
            f"- 订单成交率(本小时，全成占比): {hourly_full_fill_rate * 100:.2f}%"
        )
    if cumulative_full_fill_rate is None:
        lines.append("- 订单成交率(累计，全成占比): N/A")
    else:
        lines.append(
            f"- 订单成交率(累计，全成占比): {cumulative_full_fill_rate * 100:.2f}%"
        )

    if hourly_profit_factor is None:
        lines.append("- Profit Factor(本小时): N/A")
    elif hourly_profit_factor == float("inf"):
        lines.append("- Profit Factor(本小时): INF")
    else:
        lines.append(f"- Profit Factor(本小时): {hourly_profit_factor:.4f}")

    if cumulative_profit_factor is None:
        lines.append("- Profit Factor(累计): N/A")
    elif cumulative_profit_factor == float("inf"):
        lines.append("- Profit Factor(累计): INF")
    else:
        lines.append(f"- Profit Factor(累计): {cumulative_profit_factor:.4f}")

    lines.append(
        f"- EV/Trade(本小时): {(hourly_ev if hourly_ev is not None else 0.0):.4f} USDC"
        if hourly_ev is not None
        else "- EV/Trade(本小时): N/A"
    )
    lines.append(
        f"- EV/Trade(累计): {(cumulative_ev if cumulative_ev is not None else 0.0):.4f} USDC"
        if cumulative_ev is not None
        else "- EV/Trade(累计): N/A"
    )
    lines.append(f"- 滑点泄漏(本小时): {hourly_leakage:.4f} USDC")
    lines.append(f"- 滑点泄漏(累计): {cumulative_leakage:.4f} USDC")

    lines.append("- 原因细分(本小时):")
    if hourly_reason_stats:
        for reason_key in sorted(hourly_reason_stats.keys()):
            stats = hourly_reason_stats[reason_key]
            lines.append(
                f"  * {reason_key}: count={int(stats['count'])}, win_rate={stats['win_rate']:.2f}%, avg_pnl={stats['avg_pnl']:.4f}, avg_loss={stats['avg_loss']:.4f}"
            )
    else:
        lines.append("  * N/A")

    lines.append("- 原因细分(累计):")
    if cumulative_reason_stats:
        for reason_key in sorted(cumulative_reason_stats.keys()):
            stats = cumulative_reason_stats[reason_key]
            lines.append(
                f"  * {reason_key}: count={int(stats['count'])}, win_rate={stats['win_rate']:.2f}%, avg_pnl={stats['avg_pnl']:.4f}, avg_loss={stats['avg_loss']:.4f}"
            )
    else:
        lines.append("  * N/A")
    lines.append("")

    metric_order = [
        "prewarm_market",
        "market_event_fetch",
        "market_meta_fetch",
        "orderbook_buy_ws",
        "orderbook_sell_ws",
        "orderbook_buy",
        "orderbook_sell",
        "buy_submit",
        "sell_submit",
        "open_total",
        "close_total",
    ]
    hourly_latency_lines: List[str] = []
    cumulative_latency_lines: List[str] = []
    for metric in metric_order:
        values = latency_snapshot.get(metric) or []
        if not values:
            continue
        start_index = latency_indices.get(metric, 0)
        hourly_values = values[start_index:]
        if hourly_values:
            hourly_latency_lines.append(format_latency_summary(metric, hourly_values))
        cumulative_latency_lines.append(format_latency_summary(metric, values))

    lines.append("耗时统计（过去一小时）:")
    if hourly_latency_lines:
        lines.extend(hourly_latency_lines)
    else:
        lines.append("- 无新增耗时样本")

    hourly_source_lines = [
        f"- book_source.buy.ws={source_counts_snapshot['buy_ws'] - source_counts_index.get('buy_ws', 0)}",
        f"- book_source.buy.http={source_counts_snapshot['buy_http'] - source_counts_index.get('buy_http', 0)}",
        f"- book_source.sell.ws={source_counts_snapshot['sell_ws'] - source_counts_index.get('sell_ws', 0)}",
        f"- book_source.sell.http={source_counts_snapshot['sell_http'] - source_counts_index.get('sell_http', 0)}",
    ]

    hourly_buy_ws = source_counts_snapshot["buy_ws"] - source_counts_index.get("buy_ws", 0)
    hourly_buy_http = source_counts_snapshot["buy_http"] - source_counts_index.get("buy_http", 0)
    hourly_sell_ws = source_counts_snapshot["sell_ws"] - source_counts_index.get("sell_ws", 0)
    hourly_sell_http = source_counts_snapshot["sell_http"] - source_counts_index.get("sell_http", 0)

    hourly_buy_total = hourly_buy_ws + hourly_buy_http
    hourly_sell_total = hourly_sell_ws + hourly_sell_http
    hourly_buy_hit_rate = hourly_buy_ws / hourly_buy_total * 100 if hourly_buy_total > 0 else 0.0
    hourly_sell_hit_rate = hourly_sell_ws / hourly_sell_total * 100 if hourly_sell_total > 0 else 0.0

    lines.extend(hourly_source_lines)
    lines.append(
        f"- book_source.buy.ws_hit_rate={hourly_buy_hit_rate:.2f}% ({hourly_buy_ws}/{hourly_buy_total})"
    )
    lines.append(
        f"- book_source.sell.ws_hit_rate={hourly_sell_hit_rate:.2f}% ({hourly_sell_ws}/{hourly_sell_total})"
    )

    lines.append("")
    lines.append("耗时统计（服务启动以来）:")
    if cumulative_latency_lines:
        lines.extend(cumulative_latency_lines)
    else:
        lines.append("- 无耗时样本")
    lines.append(f"- book_source.buy.ws={source_counts_snapshot['buy_ws']}")
    lines.append(f"- book_source.buy.http={source_counts_snapshot['buy_http']}")
    lines.append(f"- book_source.sell.ws={source_counts_snapshot['sell_ws']}")
    lines.append(f"- book_source.sell.http={source_counts_snapshot['sell_http']}")

    cumulative_buy_total = source_counts_snapshot["buy_ws"] + source_counts_snapshot["buy_http"]
    cumulative_sell_total = source_counts_snapshot["sell_ws"] + source_counts_snapshot["sell_http"]
    cumulative_buy_hit_rate = (
        source_counts_snapshot["buy_ws"] / cumulative_buy_total * 100
        if cumulative_buy_total > 0
        else 0.0
    )
    cumulative_sell_hit_rate = (
        source_counts_snapshot["sell_ws"] / cumulative_sell_total * 100
        if cumulative_sell_total > 0
        else 0.0
    )
    lines.append(
        f"- book_source.buy.ws_hit_rate={cumulative_buy_hit_rate:.2f}% ({source_counts_snapshot['buy_ws']}/{cumulative_buy_total})"
    )
    lines.append(
        f"- book_source.sell.ws_hit_rate={cumulative_sell_hit_rate:.2f}% ({source_counts_snapshot['sell_ws']}/{cumulative_sell_total})"
    )
    lines.append("")

    for t in new_trades:
        lines.append(
            f"- {t.entry_time.isoformat(timespec='seconds')} -> {t.exit_time.isoformat(timespec='seconds')}, "
            f"slug={t.market_slug}, dir={t.direction}, size={t.size:.4f}, "
            f"entry={t.entry_price:.4f}, exit={t.exit_price:.4f}, pnl={t.pnl:.4f}, "
            f"invested={(t.entry_invested_usdc or 0.0):.4f}, recovered={(t.exit_recovered_usdc or 0.0):.4f}, "
            f"leakage={(t.exit_slippage_leakage or 0.0):.4f}, reason={t.reason}"
        )
    if not new_trades:
        lines.append("- 本小时无新平仓交易")

    lines.extend(_slug_pnl_and_mtm_lines(api_pnl_cumulative))

    current_buy_only_slugs: List[Dict[str, Any]] = []
    if api_pnl_hourly is not None:
        for slug_item in api_pnl_hourly.get("slug_summary", []):
            has_buy = int(slug_item.get("count_trade_buy", 0) or 0) > 0
            has_income = (
                int(slug_item.get("count_redeem", 0) or 0) > 0
                or int(slug_item.get("count_trade_sell", 0) or 0) > 0
            )
            if has_buy and not has_income:
                current_buy_only_slugs.append(slug_item)

    content = "\n".join(lines)

    ts_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    prefer_subj = bool(
        (api_pnl_cumulative or {}).get("prefer_settlement_final")
        or (api_pnl_hourly or {}).get("prefer_settlement_final")
    )
    api_h_net = float(api_pnl_hourly["net_pnl"]) if api_pnl_hourly is not None else None
    api_c_net = float(api_pnl_cumulative["net_pnl"]) if api_pnl_cumulative is not None else None
    h_blend = (
        api_pnl_hourly.get("slug_blend_pnl_total_usdc") if api_pnl_hourly is not None else None
    )
    c_blend = (
        api_pnl_cumulative.get("slug_blend_pnl_total_usdc") if api_pnl_cumulative is not None else None
    )
    if prefer_subj:
        # 主标题始终用 resolution 结算合计；无已结算盘时为 0.00，避免误显示「链上净值」作主结论
        hs = f"{float(h_blend if h_blend is not None else 0.0):.2f}"
        cs = f"{float(c_blend if c_blend is not None else 0.0):.2f}"
        subject = f"[BTC 5m] 结算盈亏(resolution): 本段 {hs} / 累计 {cs} USDC"
        if api_h_net is not None and api_c_net is not None:
            subject += f" | Activity流水 {api_h_net:.2f}/{api_c_net:.2f}"
    elif api_h_net is not None and api_c_net is not None:
        subject = (
            f"[BTC 5m] 链上净值(含redeem): 本段 {api_h_net:.2f} / 累计 {api_c_net:.2f} USDC"
        )
    elif api_h_net is not None:
        subject = f"[BTC 5m] 链上净值(含redeem): 本段 {api_h_net:.2f} USDC"
    elif api_c_net is not None:
        subject = f"[BTC 5m] 链上净值(含redeem): 累计 {api_c_net:.2f} USDC"
    elif db_realized_hourly is not None and db_realized_cumulative is not None:
        subject = (
            f"[BTC 5m] SQLite卖出: 本段 {db_realized_hourly['total_pnl']:.2f} / "
            f"累计 {db_realized_cumulative['total_pnl']:.2f} USDC"
        )
    else:
        subject = (
            f"[BTC 5m] 内存平仓: 本段 {hourly_pnl:.2f} / 累计 {cumulative_pnl:.2f} USDC"
        )
    # 补充：SQLite 卖出（与 redeem 主口径并存时便于对照）
    if (api_pnl_hourly is not None or api_pnl_cumulative is not None) and (
        db_realized_hourly is not None and db_realized_cumulative is not None
    ):
        subject += (
            f" | CLOB卖 {db_realized_hourly['total_pnl']:.2f}/{db_realized_cumulative['total_pnl']:.2f}"
        )
    # 非 settlement 主口径时，附录累计 settlement 合计便于对照
    if not prefer_subj:
        blend = None
        if api_pnl_cumulative is not None:
            blend = api_pnl_cumulative.get("slug_blend_pnl_total_usdc")
            if blend is None:
                blend = api_pnl_cumulative.get("slug_mtm_total_usdc")
        if blend is not None:
            subject += f" | 盘收益∑{float(blend):.2f}"
    subject += f" ({ts_str} UTC)"

    return content, subject, current_buy_only_slugs
