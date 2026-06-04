"""共享交易经验教训读取与格式化。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


LESSONS_PATH = Path(__file__).resolve().parent.parent / "data" / "trading_lessons.json"


def _require_non_empty_list(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = data.get(key)
    if not isinstance(value, list) or not value:
        raise ValueError(f"{LESSONS_PATH} missing non-empty list: {key}")
    items: list[dict[str, Any]] = []
    for idx, item in enumerate(value):
        if not isinstance(item, dict):
            raise ValueError(f"{LESSONS_PATH} {key}[{idx}] must be an object")
        items.append(item)
    return items


def _require_text(item: dict[str, Any], key: str, context: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{LESSONS_PATH} {context}.{key} must be a non-empty string")
    return value.strip()


def load_trading_lessons() -> dict[str, Any]:
    """读取共享经验文件；prompt 依赖该文件，格式错误应显式失败。"""
    data = json.loads(LESSONS_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{LESSONS_PATH} root must be an object")

    phase_reminders = _require_non_empty_list(data, "phase_reminders")
    common_mistakes = _require_non_empty_list(data, "common_mistakes")
    for idx, item in enumerate(phase_reminders):
        context = f"phase_reminders[{idx}]"
        _require_text(item, "phase", context)
        _require_text(item, "dashboard_text", context)
        _require_text(item, "prompt_text", context)
    for idx, item in enumerate(common_mistakes):
        context = f"common_mistakes[{idx}]"
        _require_text(item, "wrong", context)
        _require_text(item, "correction", context)
        _require_text(item, "prompt_text", context)
    return data


def get_dashboard_trading_lessons() -> dict[str, Any]:
    """返回 Dashboard 可直接渲染的共享经验。"""
    data = load_trading_lessons()
    return {
        "schema_version": data.get("schema_version", 1),
        "source": str(LESSONS_PATH.relative_to(LESSONS_PATH.parent.parent)),
        "phase_reminders": data["phase_reminders"],
        "common_mistakes": data["common_mistakes"],
    }


def format_prompt_trading_lessons() -> str:
    """格式化为 system prompt 中的共享经验区块。"""
    data = load_trading_lessons()
    phase_lines = []
    for item in data["phase_reminders"]:
        phase = _require_text(item, "phase", "phase_reminders")
        text = _require_text(item, "prompt_text", f"phase_reminders.{phase}")
        phase_lines.append(f"- **{phase}**：{text}")

    mistake_lines = []
    for item in data["common_mistakes"]:
        wrong = _require_text(item, "wrong", "common_mistakes")
        correction = _require_text(item, "correction", f"common_mistakes.{wrong}")
        prompt_text = _require_text(item, "prompt_text", f"common_mistakes.{wrong}")
        mistake_lines.append(f"- **错误**：{wrong} → **修正**：{correction}。{prompt_text}")

    return "\n".join([
        "# Shared Trading Lessons (共享经验教训 - 单一来源 data/trading_lessons.json)",
        "## 阶段经验",
        *phase_lines,
        "## 常犯错误",
        *mistake_lines,
    ])


def get_phase_prompt_lesson(phase: str) -> str:
    """按阶段返回 prompt 用经验描述。"""
    target = str(phase or "").strip()
    data = load_trading_lessons()
    for item in data["phase_reminders"]:
        if _require_text(item, "phase", "phase_reminders") == target:
            return _require_text(item, "prompt_text", f"phase_reminders.{target}")
    raise ValueError(f"{LESSONS_PATH} missing phase reminder: {target}")
