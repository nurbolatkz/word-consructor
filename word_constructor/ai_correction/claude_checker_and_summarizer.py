from __future__ import annotations

import json
import logging
import os
from typing import Any

try:
    from anthropic import Anthropic
    _ANTHROPIC_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    Anthropic = None
    _ANTHROPIC_AVAILABLE = False

from .claude_checker import claude_available, claude_summarize_review_queue
from .openai_client import SYSTEM_PROMPT as _GPT_CORRECTION_RULES, _rules_context
from .rules import load_rules_config

logger = logging.getLogger(__name__)

CLAUDE_OCCURRENCE_CORRECTION_PROMPT = (
    "Ты — независимый редактор деловых документов на русском/казахском языке.\n"
    "Работаешь вторым проходом: первичная AI-система (GPT) уже предложила исправления, "
    "ты самостоятельно и независимо определяешь правильное значение для каждого вхождения — "
    "не просто проверяешь GPT, а принимаешь собственное решение. Claude wins on disagreement.\n\n"
    "Применяй те же правила коррекции:\n\n"
) + _GPT_CORRECTION_RULES + (
    "\n\n"
    "ВАЖНО: Описанный выше OUTPUT FORMAT относится к другой системе. "
    "Твой формат ответа — JSON, описанный ниже.\n\n"
    "ТВОЙ ФОРМАТ ОТВЕТА — строго JSON, никакого лишнего текста:\n"
    "{\n"
    '  "occurrences": [\n'
    '    {\n'
    '      "placeholder": "<ключ>",\n'
    '      "occurrence_index": <int, 0-based>,\n'
    '      "corrected_value": "<итоговое значение>",\n'
    '      "changed": <true если отличается от original_value>\n'
    "    }\n"
    "  ],\n"
    '  "summary": "<один абзац на русском>"\n'
    "}\n\n"
    "occurrences — ПОЛНЫЙ список: одна запись на каждое вхождение из входного списка, в том же порядке.\n"
    "changed — true если corrected_value отличается от original_value (не от gpt_corrected).\n"
    "summary — краткий абзац: что исправлено и почему, или "
    "«Все значения уже корректны, изменений не потребовалось.» если ничего не изменилось."
)

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


def claude_correct_values(
    full_text: str,
    slot_values: dict[str, str],
    prompt_ai: str = "",
    model: str | None = None,
    log_key: str | None = None,
    call_log: dict[str, Any] | None = None,
    timeout_seconds: float = 45.0,
) -> tuple[dict[str, str], str]:
    """Simple correction mirror of GPT: send {template, placeholders} → {key: corrected_value}.

    Uses the same SYSTEM_PROMPT as GPT so both models follow identical rules.
    Returns (corrected_dict, summary_note).
    """
    model = model or os.environ.get(
        "ANTHROPIC_CLAUDE_CORRECTION_MODEL",
        os.environ.get("ANTHROPIC_CHECKER_MODEL", "claude-sonnet-4-6"),
    )

    rules_ctx = _rules_context(load_rules_config())
    system = _GPT_CORRECTION_RULES
    if rules_ctx:
        system += "\n\nSystem rules from configuration:\n" + rules_ctx

    payload: dict[str, Any] = {
        "template": full_text,
        "placeholders": {str(k): str(v) for k, v in slot_values.items()},
    }
    if prompt_ai:
        payload["additional_instructions"] = str(prompt_ai)

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    logger.info("Claude correction: log_key=%s placeholders=%d model=%s", log_key, len(slot_values), model)
    if call_log is not None:
        call_log.setdefault("claude_pass", {}).update({"model": model, "placeholder_count": len(slot_values)})

    response = _client().messages.create(
        model=model,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": body}],
        temperature=0,
        timeout=timeout_seconds,
    )
    raw = _response_text(response)
    logger.info("Claude correction response: log_key=%s text=%s", log_key, raw[:300])

    if not raw:
        raise ValueError("Claude returned an empty response")

    parsed = json.loads(_strip_json_fence(raw))
    if not isinstance(parsed, dict):
        raise ValueError("Claude response is not a JSON object")

    corrected = {str(k): str(v) for k, v in parsed.items() if k in slot_values}
    missing = sorted(set(slot_values) - set(corrected))
    if missing:
        logger.warning("Claude missing keys: log_key=%s missing=%s", log_key, missing)
        for k in missing:
            corrected[k] = slot_values[k]

    summary = "Claude проверил все значения."
    if call_log is not None:
        call_log["claude_pass"]["done"] = True
    return corrected, summary


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


def claude_correct_occurrences(
    full_text: str,
    occurrences: list[dict[str, Any]],
    gpt_occurrence_values: dict[tuple[str, int], str],
    prompt_ai: str = "",
    model: str | None = None,
    log_key: str | None = None,
    call_log: dict[str, Any] | None = None,
    max_retries: int = 1,
    timeout_seconds: float = 50.0,
) -> tuple[dict[tuple[str, int], str], str]:
    """Independent Claude correction pass over all occurrences.

    gpt_occurrence_values: {(placeholder_key, occurrence_1based): corrected_value}
    Returns: ({(placeholder_key, occurrence_1based): corrected_value}, summary_str)
    Claude wins on disagreement — its output replaces GPT's per occurrence.
    """
    model = model or os.environ.get(
        "ANTHROPIC_CLAUDE_CORRECTION_MODEL",
        os.environ.get("ANTHROPIC_CHECKER_MODEL", "claude-sonnet-4-6"),
    )

    idx_to_1based: dict[tuple[str, int], tuple[str, int]] = {}
    occurrence_items: list[dict[str, Any]] = []
    for o in occurrences:
        key = str(o.get("key") or o.get("placeholder") or "")
        if not key:
            continue
        occ_1 = int(o.get("occurrence") or 0)
        occ_idx_0 = int(o.get("occurrence_index", 0))
        original = str(o.get("value") or "")
        gpt_value = gpt_occurrence_values.get((key, occ_1), original)
        idx_to_1based[(key, occ_idx_0)] = (key, occ_1)
        occurrence_items.append({
            "placeholder": key,
            "occurrence_index": occ_idx_0,
            "original_value": original,
            "gpt_corrected": gpt_value,
            "context": str(o.get("context") or "")[:300],
        })

    if not occurrence_items:
        return {}, "Нет вхождений для проверки."

    user_content: dict[str, Any] = {"template": full_text, "occurrences": occurrence_items}
    if prompt_ai:
        user_content["additional_instructions"] = str(prompt_ai)
    body = json.dumps(user_content, ensure_ascii=False, indent=2)

    logger.info(
        "Claude occurrence correction: log_key=%s occurrences=%d model=%s",
        log_key, len(occurrence_items), model,
    )
    if call_log is not None:
        call_log.setdefault("claude_pass", {}).update({
            "model": model,
            "occurrence_count": len(occurrence_items),
        })

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = _client().messages.create(
                model=model,
                max_tokens=4000,
                system=CLAUDE_OCCURRENCE_CORRECTION_PROMPT,
                messages=[{"role": "user", "content": body}],
                temperature=0,
                timeout=timeout_seconds,
            )
            raw_text = _response_text(response)
            logger.info("Claude occurrence response: log_key=%s text=%s", log_key, raw_text[:500])

            parsed = json.loads(_strip_json_fence(raw_text))
            if not isinstance(parsed, dict):
                raise ValueError("Claude occurrence response is not a JSON object")

            claude_occs = parsed.get("occurrences")
            summary = str(parsed.get("summary") or "").strip() or "Claude проверил все вхождения."

            if not isinstance(claude_occs, list):
                raise ValueError("Claude response missing 'occurrences' array")

            corrections: dict[tuple[str, int], str] = {}
            for item in claude_occs:
                if not isinstance(item, dict):
                    continue
                ph = str(item.get("placeholder") or "")
                try:
                    occ_idx_0 = int(item.get("occurrence_index", 0))
                except (TypeError, ValueError):
                    continue
                corrected = item.get("corrected_value")
                target = idx_to_1based.get((ph, occ_idx_0))
                if target and corrected is not None:
                    corrections[target] = str(corrected)

            changed_count = sum(1 for i in claude_occs if isinstance(i, dict) and i.get("changed"))
            logger.info("Claude occurrence corrections: log_key=%s changed=%d summary=%s", log_key, changed_count, summary[:120])
            if call_log is not None:
                call_log["claude_pass"]["changed_count"] = changed_count
                call_log["claude_pass"]["summary"] = summary

            return corrections, summary

        except Exception as exc:
            last_error = exc
            logger.warning("Claude occurrence correction attempt %d failed: log_key=%s error=%s", attempt, log_key, exc)
            if call_log is not None:
                call_log.setdefault("claude_pass", {})["error"] = str(exc)
            if attempt < max_retries:
                continue

    raise RuntimeError(f"Claude occurrence correction failed after {max_retries + 1} attempt(s): {last_error}")


__all__ = [
    "claude_available",
    "claude_correct_and_review",
    "claude_correct_occurrences",
    "claude_summarize_review_queue",
]
