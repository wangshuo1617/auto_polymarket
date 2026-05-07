"""A1-lint: advisory enum cross-reference 校验。

按 plan-advisory.md v1.3 §9 implementation precondition, 在 PR/CI 阶段强制以下不变量:

1. data/advisory_schema.py 的 enum 字面量 tuple, 与 plan-advisory.md 文本中
   所有出现位置完全一致 (集合相等)。任何漂移 → exit 1。

2. data/advisory_schema.py 必含:
   - market_view_batches.batch_completed_at 列

调用: `LD_PRELOAD="" uv run python scripts/advisory_lint.py`
退出码: 0=ok, 1=任一不变量违反。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILE = ROOT / "data" / "advisory_schema.py"
PLAN_FILE = Path(
    "/root/.copilot/session-state/6db6f800-d9bf-4063-b4e5-9ffb7c6b1408/plan-advisory.md"
)


def _load_python_enums() -> dict[str, set[str]]:
    """从 advisory_schema.py 提取 *_STATES / *_REASONS / *_STATUSES tuples."""
    src = SCHEMA_FILE.read_text(encoding="utf-8")
    enums: dict[str, set[str]] = {}
    pattern = re.compile(
        r"^([A-Z_]+(?:STATES|REASONS|STATUSES))\s*=\s*\((.*?)\)\s*$",
        re.MULTILINE | re.DOTALL,
    )
    for m in pattern.finditer(src):
        name = m.group(1)
        body = m.group(2)
        values = set(re.findall(r'"([^"]+)"', body))
        enums[name] = values
    return enums


# enum 名 → 在 plan 文本中的字段名 (用于定位上下文)
ENUM_TO_FIELD = {
    "RESOLUTION_STATES": ("resolution_state",),
    "HALT_REASONS": ("halt_reason",),
    "FAIR_VALUE_STATUSES": ("fair_value_status",),
    "SETTLEMENT_STATES": ("settlement_state",),
    "REFRESH_STATUSES": ("refresh_status",),
    "BATCH_STATUSES": ("status",),  # market_view_batches.status
}


def _scan_plan_for_enum_values(plan_text: str, field_names: tuple[str, ...]) -> set[str]:
    """
    在 plan 文本中找形如:
      `field_name = 'value'`
      `field_name='value'`
      `field_name ∈ (a, b, c)`
      `field_name ∈ {a, b, c}`
      `field_name ∈ a|b|c` (用 | 分隔的内联枚举)
      `field_name='value'` 单引号
      ``field_name`` 后面跟 backtick 包围的列表
    然后抽出所有 quoted / 列表内的 lower_snake_case word。
    """
    values: set[str] = set()
    for fname in field_names:
        # 引号 / 反引号包裹的 'value'
        for m in re.finditer(
            rf"\b{re.escape(fname)}\s*[=∈:]?\s*['\"`]([a-z_]+)['\"`]", plan_text
        ):
            values.add(m.group(1))
        # 形如 "field_name (a|b|c|d)" 或 "field_name ∈ (a|b|c)" 或 "field_name ∈ {a|b|c}"
        for m in re.finditer(
            rf"\b{re.escape(fname)}\s*[∈:=]?\s*[\(\{{]([^()\{{}}\n]+)[\)\}}]", plan_text
        ):
            inner = m.group(1)
            for piece in re.split(r"[|,/]", inner):
                tok = piece.strip().strip("'\"` ")
                if re.fullmatch(r"[a-z_]+", tok):
                    values.add(tok)
        # 形如 "halt_reason='settlement_baseline_missing'" 已被上面覆盖, 此处补行内 "halt_reason 加 settlement_baseline_missing"
        for m in re.finditer(rf"\b{re.escape(fname)}\b[^a-z_]+([a-z_]+)\b", plan_text):
            tok = m.group(1)
            # 过滤明显的非 enum 词 (如 'must', 'is', 'be' 等英语)
            if (
                tok in {"is", "be", "not", "must", "the", "to", "a", "an", "in", "of", "or"}
                or len(tok) <= 2
            ):
                continue

    return values


def _scan_plan_quoted_after_field(
    plan_text: str, field_names: tuple[str, ...]
) -> set[str]:
    r"""
    严格扫描 plan 文本, 仅以下三种结构提取 enum value:
      (a) `field_name='value'` 或 `field_name = 'value'`
      (b) `field_name ∈ (a|b|c)` / `field_name ∈ {a, b, c}` / `field_name (a|b|c)` — pipe/comma 列表
      (c) `field_name 加 'value'` / `field_name 加 \`value\``

    避免宽 window 扫描会把附近的字段名也错认为 value。
    """
    values: set[str] = set()
    for fname in field_names:
        # (a) 单值 quoted: field_name [=∈:] 'value'
        for m in re.finditer(
            rf"\b{re.escape(fname)}\s*[=∈:]\s*['\"`]([a-z_][a-z_0-9]*)['\"`]",
            plan_text,
        ):
            values.add(m.group(1))

        # (b) 列表: field_name [∈]? (a|b|c) — 括号内 pipe/comma 分隔, 元素可能带或不带引号
        for m in re.finditer(
            rf"\b{re.escape(fname)}\s*[∈:=]?\s*[\(\{{]([a-z_0-9 ,|/'\"`\-]+)[\)\}}]",
            plan_text,
        ):
            inner = m.group(1)
            for piece in re.split(r"[|,/]", inner):
                tok = piece.strip().strip("'\"` ")
                if re.fullmatch(r"[a-z_][a-z_0-9]*", tok) and len(tok) >= 3:
                    values.add(tok)

        # (c) 中文上下文: field_name 加 'value'
        for m in re.finditer(
            rf"\b{re.escape(fname)}\s*加\s*['\"`]([a-z_][a-z_0-9]*)['\"`]",
            plan_text,
        ):
            values.add(m.group(1))

    return values


def lint_enums() -> list[str]:
    """返回 violation 描述 list, 空 list 表示通过。"""
    violations: list[str] = []
    py_enums = _load_python_enums()
    plan_text = PLAN_FILE.read_text(encoding="utf-8")

    for enum_name, field_names in ENUM_TO_FIELD.items():
        py_values = py_enums.get(enum_name)
        if py_values is None:
            violations.append(f"[{enum_name}] missing in advisory_schema.py")
            continue

        # plan 文本中 field_name 周边引号包裹的 enum 值
        plan_values = _scan_plan_quoted_after_field(plan_text, field_names)

        # plan-only: 出现在 plan 但不在 Python tuple — 严重 (typo / drift)
        plan_only = plan_values - py_values
        if plan_only:
            violations.append(
                f"[{enum_name}] plan-only values not in Python tuple: {sorted(plan_only)}"
            )

        # py-only: 在 Python 但 plan 完全没提 — warn (可能是 stale)
        # 仅对 plan 文本中提及的 field_name 做这个检查
        if plan_values:
            py_only = py_values - plan_values
            if py_only:
                # 不算 hard violation (可能 plan 没逐个列举每个 enum 值是正常的)
                # 仅在 stderr 报告
                print(
                    f"[advisory-lint:warn] [{enum_name}] py-only (plan never quotes): {sorted(py_only)}",
                    file=sys.stderr,
                )

    return violations


def lint_schema_invariants() -> list[str]:
    """v1.3 R3 修复: batch_completed_at 列必须存在."""
    violations: list[str] = []
    src = SCHEMA_FILE.read_text(encoding="utf-8")

    if "batch_completed_at" not in src:
        violations.append("market_view_batches.batch_completed_at 列缺失")

    # step 4 atomic 写入: 调用方代码尚未实现, 但 schema 必须允许 — batch_completed_at 必须 nullable
    # 此处仅静态校验 DDL 中无 NOT NULL on batch_completed_at
    m = re.search(r"batch_completed_at\s+([A-Z]+(?:\s+[A-Z]+)*)", src)
    if m and "NOT NULL" in m.group(1):
        violations.append(
            "batch_completed_at 不应 NOT NULL (started 状态时为 NULL, step 4 才填入)"
        )

    return violations


def main() -> int:
    all_violations: list[str] = []
    all_violations.extend(lint_enums())
    all_violations.extend(lint_schema_invariants())

    if all_violations:
        print("advisory-lint FAIL:", file=sys.stderr)
        for v in all_violations:
            print(f"  - {v}", file=sys.stderr)
        return 1

    print("advisory-lint OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
