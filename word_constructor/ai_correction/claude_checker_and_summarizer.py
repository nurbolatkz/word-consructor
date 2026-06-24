from __future__ import annotations

import json
import os
from typing import Any

try:
    from anthropic import Anthropic
    _ANTHROPIC_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    Anthropic = None
    _ANTHROPIC_AVAILABLE = False

from .claude_checker import claude_available, claude_summarize_review_queue

CLAUDE_CORRECT_SYSTEM_PROMPT = """Ты — независимый редактор русского/казахского HR и юридического документа.

Тебе дадут original_params, gpt_response и known_pitfalls.

Твоя задача:
- Для КАЖДОГО плейсхолдера independently определить его правильное значение.
- Используй правила правки HR-документов: управляющий падеж по контексту, label/signature context без склонения, не фабрикуй сокращения, не сокращай ФИО, не дублируй должность/подразделение.
- Верни corrected_values с теми же ключами, что и gpt_response.
- Если твое значение совпадает с GPT, просто верни то же значение.
- Если не совпадает, в review_summary.changes_from_gpt укажи только отличающиеся ключи и короткую причину по-русски.
- review_summary.note всегда должен быть непустой.

Claude wins on disagreement."""


def _strip_json_fence(raw_text: str) -> str:
    text = raw_text.strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    if lines and lines[0].strip().lower() == "json":
        lines = lines[1:]
    return "\n".join(lines).strip()


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


def _client() -> Any:
    if not _ANTHROPIC_AVAILABLE or Anthropic is None:
        raise RuntimeError("anthropic package is not installed")
    return Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or None)


def _schema_for_keys(keys: list[str]) -> dict[str, Any]:
    corrected_properties = {key: {"type": "string"} for key in keys}
    changed_item_schema = {
        "type": "object",
        "properties": {
            "placeholder": {"type": "string"},
            "gpt_value": {"type": "string"},
            "claude_value": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["placeholder", "gpt_value", "claude_value", "reason"],
        "additionalProperties": False,
    }
    return {
        "name": "claude_correct_and_review",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "corrected_values": {
                    "type": "object",
                    "properties": corrected_properties,
                    "required": keys,
                    "additionalProperties": False,
                },
                "review_summary": {
                    "type": "object",
                    "properties": {
                        "had_issues": {"type": "boolean"},
                        "changes_from_gpt": {"type": "array", "items": changed_item_schema},
                        "note": {"type": "string"},
                    },
                    "required": ["had_issues", "changes_from_gpt", "note"],
                    "additionalProperties": False,
                },
            },
            "required": ["corrected_values", "review_summary"],
            "additionalProperties": False,
        },
    }


def claude_correct_and_review(
    original_params: dict[str, Any],
    gpt_response: dict[str, str],
    known_pitfalls: list[dict[str, Any]] | None = None,
    model: str = "claude-sonnet-4-6",
    max_retries: int = 1,
) -> dict[str, Any]:
    template = str(original_params.get("template") or "")
    placeholders = original_params.get("placeholders") or {}
    if not isinstance(placeholders, dict):
        placeholders = {}

    keys = [str(key) for key in gpt_response.keys()]
    if not keys:
        return {"corrected_values": {}, "review_summary": {"had_issues": False, "changes_from_gpt": [], "note": "No placeholders to review."}}

    user_content: dict[str, Any] = {
        "original_params": {
            "template": template,
            "placeholders": {str(k): str(v) for k, v in placeholders.items()},
        },
        "gpt_response": {str(k): str(v) for k, v in gpt_response.items()},
    }
    if known_pitfalls:
        user_content["known_pitfalls"] = [
            {
                "original": item.get("original_value", ""),
                "wrong_corrected": item.get("corrected_value", ""),
                "note": item.get("note", ""),
            }
            for item in known_pitfalls
        ]

    schema = _schema_for_keys(keys)
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = _client().messages.create(
                model=model,
                max_tokens=2000,
                system=CLAUDE_CORRECT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(user_content, ensure_ascii=False, indent=2)}],
                temperature=0,
                response_format={"type": "json_schema", "json_schema": schema},
            )
            parsed = json.loads(_strip_json_fence(_response_text(response)))
            if not isinstance(parsed, dict):
                raise ValueError("Claude correction response is not a JSON object")
            corrected_values = parsed.get("corrected_values")
            review_summary = parsed.get("review_summary")
            if not isinstance(corrected_values, dict) or not isinstance(review_summary, dict):
                raise ValueError("Claude correction response missing required objects")
            expected = sorted(keys)
            got = sorted(str(k) for k in corrected_values.keys())
            if expected != got:
                raise ValueError(f"Claude correction key mismatch: expected {expected}, got {got}")
            cleaned_corrected = {str(k): str(corrected_values[k]) for k in keys}
            changes = review_summary.get("changes_from_gpt") or []
            if not isinstance(changes, list):
                raise ValueError("Claude review_summary.changes_from_gpt must be a list")
            note = str(review_summary.get("note") or "").strip() or "Claude reviewed GPT output."
            return {
                "corrected_values": cleaned_corrected,
                "review_summary": {
                    "had_issues": bool(review_summary.get("had_issues", False)),
                    "changes_from_gpt": changes,
                    "note": note,
                },
            }
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                continue
    raise RuntimeError(f"Claude correction failed after {max_retries + 1} attempt(s): {last_error}")


__all__ = ["claude_available", "claude_correct_and_review", "claude_summarize_review_queue"]
