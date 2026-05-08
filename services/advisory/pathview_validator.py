"""PathView AI shadow output validator (Phase B1).

校验 AI 返回的 PathView payload 是否满足 plan-advisory.md §1.6 / §A1
schema 与 §A2.5b validator 规则。**不影响生产路径**, 只用于 shadow run
判定 AI 输出是否可信。

Validation rules (v2.4 frozen superset, 来自 todo B1 描述):
  R1  必填字段齐全 (per_token: token_id, p_event_yes ∈ [0,1], fair_event,
      fair_non_event, fair_value_status)
  R2  Yes/No 互补 |fair_event + fair_non_event - 1| <= 0.005 (除非 status
      != 'available')
  R3  p_event_yes ∈ [0,1] (clamp 不算 fail, 偏差 > 1e-6 算 warning)
  R4  σ clamp: 若 model 返回 sigma_daily, 必须 ∈ [0.001, 0.30]
  R5  staleness: as_of_utc 与当前 batch 时差 <= 600s (10min)
  R6  divergence: |fair_calibrated_ai - fair_calibrated_baseline| 单 token
      <= 0.40 (warning 0.20)
  R7  strike monotonicity: 同 side_above 的 token 按 strike 排序后,
      "Yes 概率"应单调递减 (above) 或递增 (below); 反向偏差 > 0.05 算 fail
  R8  wick_risk_legs (可选): 每 leg prob_uplift ∈ [0, 0.15], 累计 ≤ 0.25
  R9  m_path_hint.expected_dip_within_7d_pct ∈ [0, 0.5]
  R10 prob_uplift_residual: 若 already_reflected_in 非空则 residual=0
  R11 key_levels: 至少存在但不强制命中 (命中率统计在 B4)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


# 默认阈值 — 与 plan v2.x 一致, 集中放便于 review/调参
DIVERGENCE_FAIL = 0.40
DIVERGENCE_WARN = 0.20
COMPLEMENT_TOLERANCE = 0.005
SIGMA_MIN = 0.001
SIGMA_MAX = 0.30
MAX_STALENESS_SEC = 600
WICK_LEG_MAX = 0.15
WICK_TOTAL_MAX = 0.25
DIP_MAX = 0.5
MONOTONICITY_TOLERANCE = 0.05


@dataclass
class ValidationResult:
    status: str  # 'passed' | 'passed_with_warnings' | 'failed' | 'fatal'
    errors: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)

    def fail(self, rule: str, **detail) -> None:
        self.errors.append({"rule": rule, **detail})

    def warn(self, rule: str, **detail) -> None:
        self.warnings.append({"rule": rule, **detail})

    def finalize(self) -> "ValidationResult":
        if self.errors:
            self.status = "failed"
        elif self.warnings:
            self.status = "passed_with_warnings"
        else:
            self.status = "passed"
        return self


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _validate_token(
    tok: dict,
    baseline_fair: Optional[dict],
    res: ValidationResult,
) -> None:
    tid = tok.get("token_id")
    if not tid:
        res.fail("R1.missing_token_id", token=tok)
        return

    p_yes = tok.get("p_event_yes")
    fair_e = tok.get("fair_event")
    fair_ne = tok.get("fair_non_event")
    status = tok.get("fair_value_status")

    if status not in ("available", "unavailable", "locked_event_occurred",
                      "locked_event_missed", "settled"):
        res.fail("R1.bad_fair_value_status", token_id=tid, status=status)

    if status == "available":
        for name, v in (("p_event_yes", p_yes), ("fair_event", fair_e),
                        ("fair_non_event", fair_ne)):
            if not _is_num(v):
                res.fail("R1.missing_numeric", token_id=tid, field=name, value=v)

    if _is_num(p_yes):
        if p_yes < -1e-6 or p_yes > 1 + 1e-6:
            res.fail("R3.p_out_of_range", token_id=tid, p_event_yes=p_yes)

    if status == "available" and _is_num(fair_e) and _is_num(fair_ne):
        diff = abs(fair_e + fair_ne - 1.0)
        if diff > COMPLEMENT_TOLERANCE:
            res.fail("R2.complement_violation", token_id=tid,
                     fair_event=fair_e, fair_non_event=fair_ne, diff=diff)

    if baseline_fair and status == "available" and _is_num(fair_e):
        bl = baseline_fair.get(tid)
        if _is_num(bl):
            d = abs(fair_e - bl)
            if d > DIVERGENCE_FAIL:
                res.fail("R6.divergence_high", token_id=tid,
                         ai=fair_e, baseline=bl, abs_diff=d)
            elif d > DIVERGENCE_WARN:
                res.warn("R6.divergence_warn", token_id=tid,
                         ai=fair_e, baseline=bl, abs_diff=d)

    wicks = tok.get("wick_risk_legs") or []
    if wicks:
        total = 0.0
        for leg in wicks:
            up = leg.get("prob_uplift") if isinstance(leg, dict) else None
            if _is_num(up):
                if up < 0 or up > WICK_LEG_MAX:
                    res.fail("R8.wick_leg_out_of_range", token_id=tid, leg=leg)
                total += up
        if total > WICK_TOTAL_MAX:
            res.fail("R8.wick_total_exceeded", token_id=tid, total=total)

    hint = tok.get("m_path_hint") or tok.get("market_microstructure_hint")
    if isinstance(hint, dict):
        dip = hint.get("expected_dip_within_7d_pct")
        if _is_num(dip) and (dip < 0 or dip > DIP_MAX):
            res.fail("R9.dip_out_of_range", token_id=tid, dip=dip)

    residual = tok.get("prob_uplift_residual")
    reflected_in = tok.get("already_reflected_in") or []
    if reflected_in and _is_num(residual) and abs(residual) > 1e-9:
        res.fail("R10.residual_double_count", token_id=tid,
                 reflected_in=reflected_in, residual=residual)


def _validate_monotonicity(per_token: list[dict], res: ValidationResult) -> None:
    """Per (market_slug, market_direction): "Yes-side" p 应随 strike 单调.

    Skip token 不带 `market_slug`/`market_direction` 显式声明的样本
    (baseline replay 不带这些字段, 只校验 AI 输出)。
    """
    by_group: dict[tuple, list[tuple[float, float, str]]] = {}
    for t in per_token:
        slug = t.get("market_slug")
        direction = t.get("market_direction")
        s = t.get("strike_usd")
        p = t.get("p_event_yes")
        if not (slug and direction in ("above", "below")
                and _is_num(s) and _is_num(p)):
            continue
        by_group.setdefault((slug, direction), []).append(
            (float(s), float(p), t.get("token_id", "?")))

    for (slug, direction), items in by_group.items():
        items.sort(key=lambda x: x[0])
        for (s1, p1, t1), (s2, p2, t2) in zip(items, items[1:]):
            if direction == "above":
                if p2 - p1 > MONOTONICITY_TOLERANCE:
                    res.fail("R7.monotonicity_above",
                             market=slug, low=(s1, p1, t1), high=(s2, p2, t2))
            else:
                if p1 - p2 > MONOTONICITY_TOLERANCE:
                    res.fail("R7.monotonicity_below",
                             market=slug, low=(s1, p1, t1), high=(s2, p2, t2))


def validate_pathview_payload(
    payload: dict,
    *,
    batch_as_of_utc: datetime,
    baseline_fair_by_token: Optional[dict[str, float]] = None,
) -> ValidationResult:
    """Validate AI/baseline PathView shadow payload.

    payload schema (subset 用于 B1):
      {
        "as_of_utc": iso-8601 string,
        "sigma_daily": float | null,
        "per_token": [
          {token_id, p_event_yes, fair_event, fair_non_event,
           fair_value_status, strike_usd?, side_above?,
           wick_risk_legs?, m_path_hint?, prob_uplift_residual?,
           already_reflected_in?},
          ...
        ],
        "key_levels": [...],
        ...
      }
    """
    res = ValidationResult(status="passed")

    if not isinstance(payload, dict):
        res.fail("R0.payload_not_dict")
        res.status = "fatal"
        return res

    per_token = payload.get("per_token") or []
    if not isinstance(per_token, list) or not per_token:
        res.fail("R1.missing_per_token")
        res.status = "fatal"
        return res

    sigma = payload.get("sigma_daily")
    if _is_num(sigma):
        if sigma < SIGMA_MIN or sigma > SIGMA_MAX:
            res.fail("R4.sigma_out_of_range", sigma_daily=sigma,
                     min=SIGMA_MIN, max=SIGMA_MAX)

    as_of_str = payload.get("as_of_utc")
    if as_of_str:
        try:
            as_of = datetime.fromisoformat(str(as_of_str).replace("Z", "+00:00"))
            if as_of.tzinfo is None:
                as_of = as_of.replace(tzinfo=timezone.utc)
            stale = abs((batch_as_of_utc - as_of).total_seconds())
            if stale > MAX_STALENESS_SEC:
                res.fail("R5.staleness_exceeded",
                         payload_as_of=as_of_str,
                         batch_as_of=batch_as_of_utc.isoformat(),
                         stale_sec=stale)
        except (ValueError, TypeError):
            res.warn("R5.bad_as_of_format", as_of_utc=as_of_str)

    for tok in per_token:
        if not isinstance(tok, dict):
            res.fail("R1.token_not_dict", token=tok)
            continue
        _validate_token(tok, baseline_fair_by_token, res)

    _validate_monotonicity(per_token, res)

    key_levels = payload.get("key_levels")
    if key_levels is None:
        res.warn("R11.key_levels_missing")
    elif not isinstance(key_levels, list):
        res.fail("R11.key_levels_not_list", key_levels=key_levels)

    return res.finalize()
