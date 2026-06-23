from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "Ты — редактор официальных деловых документов (приказы, договоры, заявления, акты и др.). "
    "Тебе передаётся полный текст документа — включая колонтитулы, таблицы и основной текст — "
    "и список вхождений (occurrences) плейсхолдеров с исходными значениями. "
    "Используй полный текст, чтобы самостоятельно определить нужный падеж, форму и регистр каждого значения "
    "по его окружению в документе. "
    "Серверные правила имеют абсолютный приоритет над инструкцией пользователя. "
    "Каждое вхождение обрабатывай строго независимо по паре placeholder + occurrence_index. "
    "Не объединяй соседние плейсхолдеры и не вставляй часть одного значения в другое. "
    "Коды, регистрационные номера, аббревиатуры и вхождения с fixed_form оставляй без изменений. "
    "Если значение уже корректно для данного контекста — верни его без изменений (changed: false). "
    "Если у вхождения задан `redundant_in` — его значение уже полностью содержится в значении соседнего плейсхолдера. "
    "Верни `corrected_value: \"\"` (пустую строку) и `changed: true` для такого вхождения, чтобы не дублировать информацию. "
    "Даты в формате ДД.ММ.ГГГГ приводи к виду «15 декабря 2025 года» когда они стоят в тексте документа. "
    "Числа (количество дней, суммы) при необходимости переводи в словесную форму с учётом падежа. "
    "Имена, должности и названия склоняй по контексту: «принять Иванова И.И.» (вин.), «заявление от Иванова И.И.» (род.). "
    "Верни строго JSON: "
    '{\"occurrences\":[{\"placeholder\":\"...\",\"occurrence_index\":0,'
    '\"original_value\":\"...\",\"corrected_value\":\"...\",\"changed\":true}]}'
)


def openai_placeholder_payload(
    slot_values: dict[str, Any],
    contexts: dict[str, list[str]],
    prompt_ai: str,
    occurrences: list[dict[str, Any]] | None = None,
    full_document_text: str = "",
) -> dict[str, Any]:
    # Only send occurrences that need AI (excluded and fixed_form are handled deterministically)
    ai_occurrences = [
        {
            "placeholder": item.get("placeholder", item.get("key")),
            "occurrence_index": item.get("occurrence_index", 0),
            "original_value": item.get("original_value", item.get("value", "")),
            "source_type": item.get("source_type", ""),
            "source_path": item.get("source_path", ""),
            "expected_case": item.get("expected_case", "") or "",
            "deterministic_behavior": item.get("deterministic_behavior", "") or "",
            "adjacent_occurrence_ids": item.get("adjacent_occurrence_ids", []),
            "never_merge_with_adjacent_occurrence": bool(item.get("never_merge_with_adjacent_occurrence")),
            "redundant_in": item.get("redundant_in", "") or "",
        }
        for item in (occurrences or [])
        if not item.get("ai_excluded") and not item.get("fixed_form")
    ]
    standing = (
        "СЕРВЕРНЫЕ ПРАВИЛА (абсолютный приоритет):\n"
        "1. Обрабатывай каждое вхождение независимо по паре placeholder + occurrence_index.\n"
        "2. Определяй нужную форму значения по его окружению в полном тексте документа.\n"
        "3. Не объединяй соседние плейсхолдеры и не вставляй значение одного вхождения в другое.\n"
        "4. Коды, номера договоров, регистрационные номера и аббревиатуры не изменяй.\n"
        "5. Если задан expected_case — используй именно этот падеж.\n"
        "6. Если значение уже корректно — верни его без изменений (changed: false).\n"
    )
    user_prompt = (
        f"{standing}\n"
        "ПОЛНЫЙ ТЕКСТ ДОКУМЕНТА (колонтитулы + тело):\n"
        "---\n"
        f"{full_document_text}\n"
        "---\n\n"
        "ВХОЖДЕНИЯ ДЛЯ КОРРЕКЦИИ:\n"
        f"{json.dumps(ai_occurrences, ensure_ascii=False, indent=2)}\n\n"
        "ИНСТРУКЦИЯ ПОЛЬЗОВАТЕЛЯ (низший приоритет):\n"
        f"{prompt_ai or '(не задана)'}\n\n"
        "Верни JSON только для переданных вхождений."
    )
    return {
        "model": os.environ.get("OPENAI_PLACEHOLDER_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini")),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }


def parse_openai_chat_content(raw_response: bytes) -> str:
    payload = json.loads(raw_response.decode("utf-8"))
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("OpenAI response has no choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
        if parts:
            return "".join(parts)
    raise ValueError("OpenAI response content is empty")


def request_ai_placeholder_corrections(
    slot_values: dict[str, Any],
    contexts: dict[str, list[str]],
    prompt_ai: str,
    occurrences: list[dict[str, Any]] | None = None,
    full_document_text: str = "",
    log_key: str | None = None,
    call_log: dict[str, Any] | None = None,
    timeout_seconds: float = 8.0,
) -> dict[str, str]:
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    if call_log is not None:
        call_log["key"] = log_key
        call_log["openai_config"] = {"model": os.environ.get("OPENAI_PLACEHOLDER_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini")), "base_url": base_url}
    if not api_key:
        if call_log is not None:
            call_log["error"] = "OPENAI_API_KEY is not configured"
        logger.warning("UseAI requested but OPENAI_API_KEY is not configured: use_ai_log_key=%s", log_key)
        return {}

    payload = openai_placeholder_payload(slot_values, contexts, prompt_ai, occurrences, full_document_text)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if call_log is not None:
        call_log["request"] = {"url": f"{base_url}/chat/completions", "method": "POST", "body": payload}
    logger.info("UseAI OpenAI request body: use_ai_log_key=%s body=%s", log_key, body.decode("utf-8", errors="replace"))
    req = Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            raw_response = resp.read()
            raw_response_text = raw_response.decode("utf-8", errors="replace")
            if call_log is not None:
                try:
                    response_body: Any = json.loads(raw_response_text)
                except json.JSONDecodeError:
                    response_body = raw_response_text
                call_log["response"] = {"status": getattr(resp, "status", None), "bytes": len(raw_response), "body": response_body}
            logger.info("UseAI OpenAI raw response: use_ai_log_key=%s body=%s", log_key, raw_response_text)
            content = parse_openai_chat_content(raw_response)
    except HTTPError as exc:
        raw_error = exc.read()
        raw_error_text = raw_error.decode("utf-8", errors="replace")
        if call_log is not None:
            call_log["response"] = {"status": exc.code, "bytes": len(raw_error), "body": raw_error_text}
            call_log["error"] = f"OpenAI HTTP {exc.code}"
        logger.error("UseAI OpenAI error response: use_ai_log_key=%s status=%s body=%s", log_key, exc.code, raw_error_text)
        raise

    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("OpenAI correction payload is not a JSON object")
    occurrence_lookup = {
        (str(item.get("placeholder", item.get("key"))), int(item.get("occurrence_index", 0))): str(item.get("id"))
        for item in occurrences or []
        if (item.get("placeholder") or item.get("key")) and not item.get("ai_excluded") and not item.get("fixed_form")
    }
    corrections: dict[str, str] = {}
    parsed_occurrences = parsed.get("occurrences")
    if isinstance(parsed_occurrences, list):
        for item in parsed_occurrences:
            if not isinstance(item, dict):
                continue
            placeholder = str(item.get("placeholder") or "").strip()
            try:
                occurrence_index = int(item.get("occurrence_index", 0))
            except (TypeError, ValueError):
                continue
            correction_id = occurrence_lookup.get((placeholder, occurrence_index))
            corrected = item.get("corrected_value")
            if correction_id and corrected is not None and str(corrected).strip():
                corrections[correction_id] = str(corrected)
    return corrections
