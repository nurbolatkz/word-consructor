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

import re as _re

from .claude_checker import claude_available, claude_summarize_review_queue
from .openai_client import SYSTEM_PROMPT as _GPT_CORRECTION_RULES, _rules_context
from .rules import load_rules_config

logger = logging.getLogger(__name__)

CLAUDE_OCCURRENCE_CORRECTION_PROMPT = (
    "Ты — редактор деловых документов на русском/казахском языке.\n"
    "Для каждого вхождения плейсхолдера самостоятельно определи грамматически правильное значение "
    "с учётом контекста: управляющий глагол или предлог, позиция в документе (тело/подпись/таблица).\n\n"
    "Правила коррекции:\n\n"
) + _GPT_CORRECTION_RULES + (
    "\n\n"
    "ФОРМАТ ОТВЕТА: вызови инструмент corrected_occurrences.\n\n"
    "occurrences — ПОЛНЫЙ список: одна запись на каждое вхождение из входного списка, в том же порядке.\n"
    "reference_value (если присутствует): ранее предложенное значение — учти его как подсказку, но "
    "принимай собственное решение.\n"
    "changed — true если corrected_value отличается от original_value.\n"
    "summary — один абзац: что исправлено и почему, или "
    "«Все значения уже корректны.» если ничего не изменилось."
)

CLAUDE_CORRECT_SYSTEM_PROMPT = """Ты — независимый редактор русского/казахского HR и юридического документа.

Тебе дадут original_params, gpt_response и known_pitfalls.

Твоя задача:
- Для КАЖДОГО плейсхолдера независимо определить его правильное значение.
- Применяй правила из основного системного промпта: падежное управление, инициалы vs полное имя, регистр букв, И.О. как приставка, дублирование, казахские патронимы.
- Ключевые правила регистра:
    • Плейсхолдер содержит "Инициалы"/"Init"/"КраткоеФИО" → формат «Фамилия И.О.»; казахские -ұлы/-қызы → «Фамилия И.»
    • "И.О." перед должностью (И.О. директора) — сохранять точно, не раскрывать, склонять только саму должность
    • Должность в подписи/шапке — сохранять регистр как есть; в тексте предложения — sentence case
- Верни corrected_values с теми же ключами, что и gpt_response.
- Если твоё значение совпадает с GPT — верни то же; если нет — укажи в review_summary.changes_from_gpt ключ и краткую причину по-русски.
- review_summary.note всегда должен быть непустой.

Claude wins on disagreement."""

# Tool schema for claude_correct_occurrences — occurrences have a fixed structure so
# no key-mapping is needed here.
_OCCURRENCE_TOOL: dict[str, Any] = {
    "name": "corrected_occurrences",
    "description": "Return all occurrence corrections.",
    "input_schema": {
        "type": "object",
        "properties": {
            "occurrences": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "placeholder": {"type": "string"},
                        "occurrence_index": {"type": "integer"},
                        "corrected_value": {"type": "string"},
                        "changed": {"type": "boolean"},
                    },
                    "required": ["placeholder", "occurrence_index", "corrected_value", "changed"],
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["occurrences", "summary"],
    },
}


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


def _extract_json_object(text: str) -> str:
    """Find the first {...} JSON object in text, even when Claude adds prose around it."""
    stripped = _strip_json_fence(text)
    if stripped.startswith("{"):
        return stripped
    match = _re.search(r"\{.*\}", text, _re.DOTALL)
    if match:
        return match.group(0)
    return ""


def _response_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


def _extract_tool_input(response: Any) -> Any:
    """Extract the input dict from the first tool_use block in a Claude response."""
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "tool_use":
            return getattr(block, "input", None)
        if isinstance(block, dict) and block.get("type") == "tool_use":
            return block.get("input")
    return None


def _client() -> Any:
    if not _ANTHROPIC_AVAILABLE or Anthropic is None:
        raise RuntimeError("anthropic package is not installed")
    return Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY") or None)


def _safe_key_map(keys: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Map arbitrary keys to ASCII-safe names for Claude tool schemas.

    Claude tool schema property keys must match ^[a-zA-Z0-9_.-]{1,64}$, but
    placeholder names are often Cyrillic (e.g. ДолжностьЗамещающего).
    We use slot_N indices and include the original name in each property description.
    """
    orig_to_safe = {k: f"slot_{i}" for i, k in enumerate(keys)}
    safe_to_orig = {v: k for k, v in orig_to_safe.items()}
    return orig_to_safe, safe_to_orig


def claude_correct_values(
    full_text: str,
    slot_values: dict[str, str],
    prompt_ai: str = "",
    model: str | None = None,
    log_key: str | None = None,
    call_log: dict[str, Any] | None = None,
    timeout_seconds: float = 45.0,
) -> tuple[dict[str, str], str]:
    """Simple correction: {template, placeholders} → {key: corrected_value}.
    Returns (corrected_dict, summary_note).
    """
    model = model or os.environ.get(
        "ANTHROPIC_CLAUDE_CORRECTION_MODEL",
        os.environ.get("ANTHROPIC_CHECKER_MODEL", "claude-haiku-4-5-20251001"),
    )

    rules_ctx = _rules_context(load_rules_config())
    system = _GPT_CORRECTION_RULES
    if rules_ctx:
        system += "\n\nSystem rules from configuration:\n" + rules_ctx

    orig_keys = list(slot_values.keys())
    orig_to_safe, safe_to_orig = _safe_key_map(orig_keys)

    payload: dict[str, Any] = {
        "template": full_text,
        # Include original key names in the payload so Claude understands the semantics
        "placeholders": {str(k): str(v) for k, v in slot_values.items()},
        # Mapping tells Claude which safe key corresponds to which original placeholder
        "_key_map": {orig_to_safe[k]: str(k) for k in orig_keys},
    }
    if prompt_ai:
        payload["additional_instructions"] = str(prompt_ai)

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    logger.info("Claude correction: log_key=%s placeholders=%d model=%s", log_key, len(slot_values), model)
    if call_log is not None:
        call_log.setdefault("claude_pass", {}).update({"model": model, "placeholder_count": len(slot_values)})

    # Claude tool schema property keys must be ASCII — map Cyrillic keys to slot_N
    tool_schema: dict[str, Any] = {
        "name": "corrected_placeholders",
        "description": "Return every placeholder with its grammar-corrected value.",
        "input_schema": {
            "type": "object",
            "properties": {
                orig_to_safe[k]: {
                    "type": "string",
                    "description": f"Corrected value for placeholder '{k}'",
                }
                for k in orig_keys
            },
            "required": [orig_to_safe[k] for k in orig_keys],
            "additionalProperties": False,
        },
    }

    response = _client().messages.create(
        model=model,
        max_tokens=4000,
        system=system,
        messages=[{"role": "user", "content": body}],
        tools=[tool_schema],
        tool_choice={"type": "tool", "name": "corrected_placeholders"},
        temperature=0,
        timeout=timeout_seconds,
    )

    stop_reason = getattr(response, "stop_reason", None)
    logger.info(
        "Claude correction response: log_key=%s stop_reason=%s",
        log_key, stop_reason,
    )

    parsed: Any = _extract_tool_input(response)

    if parsed is None:
        raw_fallback = _response_text(response)
        logger.warning("Claude tool_use block missing, trying text fallback: log_key=%s raw=%r", log_key, raw_fallback[:300])
        json_str = _extract_json_object(raw_fallback)
        if not json_str:
            raise ValueError(f"Claude returned no tool_use block and no JSON (stop_reason={stop_reason!r})")
        parsed = json.loads(json_str)

    if not isinstance(parsed, dict):
        raise ValueError(f"Claude tool input is not a dict: {parsed!r}")

    # Remap safe keys back to original Cyrillic keys
    corrected: dict[str, str] = {}
    for safe_k, val in parsed.items():
        orig_k = safe_to_orig.get(safe_k)
        if orig_k is not None:
            corrected[orig_k] = str(val)

    missing = sorted(set(slot_values) - set(corrected))
    if missing:
        logger.warning("Claude missing keys: log_key=%s missing=%s", log_key, missing)
        for k in missing:
            corrected[k] = slot_values[k]

    summary = "Claude проверил все значения."
    if call_log is not None:
        call_log["claude_pass"]["done"] = True
    return corrected, summary


def _schema_for_keys(keys: list[str]) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
    """Build an Anthropic tool schema for claude_correct_and_review.

    Returns (tool_dict, orig_to_safe, safe_to_orig).  Cyrillic keys are mapped to
    ASCII slot_N names so they satisfy Claude's ^[a-zA-Z0-9_.-]{1,64}$ constraint.
    """
    orig_to_safe = {k: f"slot_{i}" for i, k in enumerate(keys)}
    safe_to_orig = {v: k for k, v in orig_to_safe.items()}

    corrected_properties = {
        orig_to_safe[k]: {
            "type": "string",
            "description": f"Corrected value for placeholder '{k}'",
        }
        for k in keys
    }
    changed_item_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "placeholder": {"type": "string"},
            "gpt_value": {"type": "string"},
            "claude_value": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["placeholder", "gpt_value", "claude_value", "reason"],
    }
    tool: dict[str, Any] = {
        "name": "claude_correct_and_review",
        "description": "Return corrected placeholder values and a review summary.",
        "input_schema": {
            "type": "object",
            "properties": {
                "corrected_values": {
                    "type": "object",
                    "properties": corrected_properties,
                    "required": list(orig_to_safe.values()),
                },
                "review_summary": {
                    "type": "object",
                    "properties": {
                        "had_issues": {"type": "boolean"},
                        "changes_from_gpt": {"type": "array", "items": changed_item_schema},
                        "note": {"type": "string"},
                    },
                    "required": ["had_issues", "changes_from_gpt", "note"],
                },
            },
            "required": ["corrected_values", "review_summary"],
        },
    }
    return tool, orig_to_safe, safe_to_orig


def claude_correct_and_review(
    original_params: dict[str, Any],
    gpt_response: dict[str, str],
    known_pitfalls: list[dict[str, Any]] | None = None,
    model: str = "claude-haiku-4-5-20251001",
    max_retries: int = 1,
) -> dict[str, Any]:
    template = str(original_params.get("template") or "")
    placeholders = original_params.get("placeholders") or {}
    if not isinstance(placeholders, dict):
        placeholders = {}

    keys = [str(key) for key in gpt_response.keys()]
    if not keys:
        return {"corrected_values": {}, "review_summary": {"had_issues": False, "changes_from_gpt": [], "note": "No placeholders to review."}}

    tool, orig_to_safe, safe_to_orig = _schema_for_keys(keys)

    user_content: dict[str, Any] = {
        "original_params": {
            "template": template,
            "placeholders": {str(k): str(v) for k, v in placeholders.items()},
        },
        "gpt_response": {str(k): str(v) for k, v in gpt_response.items()},
        # Let Claude know which safe key corresponds to which original placeholder
        "_key_map": {orig_to_safe[k]: k for k in keys},
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

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            response = _client().messages.create(
                model=model,
                max_tokens=2000,
                system=CLAUDE_CORRECT_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(user_content, ensure_ascii=False, indent=2)}],
                temperature=0,
                tools=[tool],
                tool_choice={"type": "tool", "name": "claude_correct_and_review"},
            )

            parsed = _extract_tool_input(response)
            if parsed is None:
                raise ValueError("Claude returned no tool_use block")
            if not isinstance(parsed, dict):
                raise ValueError(f"Claude tool input is not a dict: {parsed!r}")

            corrected_safe = parsed.get("corrected_values")
            review_summary = parsed.get("review_summary")
            if not isinstance(corrected_safe, dict) or not isinstance(review_summary, dict):
                raise ValueError("Claude correction response missing required objects")

            # Remap safe keys → original Cyrillic keys
            cleaned_corrected: dict[str, str] = {}
            for safe_k, val in corrected_safe.items():
                orig_k = safe_to_orig.get(safe_k)
                if orig_k is not None:
                    cleaned_corrected[orig_k] = str(val)

            missing = sorted(set(keys) - set(cleaned_corrected))
            if missing:
                raise ValueError(f"Claude correction key mismatch: missing {missing}")

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
    timeout_seconds: float = 15.0,
) -> tuple[dict[tuple[str, int], str], str]:
    """Independent Claude correction pass over all occurrences.

    gpt_occurrence_values: {(placeholder_key, occurrence_1based): corrected_value}
    Returns: ({(placeholder_key, occurrence_1based): corrected_value}, summary_str)
    Claude wins on disagreement — its output replaces GPT's per occurrence.
    """
    model = model or os.environ.get(
        "ANTHROPIC_CLAUDE_CORRECTION_MODEL",
        os.environ.get("ANTHROPIC_CHECKER_MODEL", "claude-haiku-4-5-20251001"),
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
        ref_value = gpt_occurrence_values.get((key, occ_1), original)
        idx_to_1based[(key, occ_idx_0)] = (key, occ_1)
        item: dict[str, Any] = {
            "placeholder": key,
            "occurrence_index": occ_idx_0,
            "original_value": original,
            "context": str(o.get("context") or "")[:300],
        }
        # Only include reference_value when it differs (keeps payload clean for primary-mode runs)
        if ref_value != original:
            item["reference_value"] = ref_value
        occurrence_items.append(item)

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
                tools=[_OCCURRENCE_TOOL],
                tool_choice={"type": "tool", "name": "corrected_occurrences"},
                timeout=timeout_seconds,
            )

            parsed = _extract_tool_input(response)

            if parsed is None:
                # Fallback: try parsing text response (older behavior)
                raw_text = _response_text(response)
                if not raw_text:
                    raise ValueError("Claude returned no tool_use block and no text content")
                logger.warning(
                    "Claude occurrence: no tool_use block, trying text fallback: log_key=%s raw=%r",
                    log_key, raw_text[:300],
                )
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
