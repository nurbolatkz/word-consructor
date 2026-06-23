"""AI corrections client — full document text + all occurrences → AI → corrected values."""
from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — редактор официальных деловых документов (приказы, договоры, заявления, акты и др.).

Получаешь:
1. Полный текст документа — колонтитулы, основной текст, таблицы, блок подписи.
2. Список вхождений плейсхолдеров с исходными значениями и окружающим контекстом.
3. Правила коррекции из конфига системы.

Используй полный текст документа, чтобы понять контекст каждого плейсхолдера и вернуть правильную форму значения.

ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА:

ПАДЕЖИ — склоняй по контексту предложения:
  «принять [ФИО]» → вин.п.: «Иванова Ивана Ивановича»
  «от [ФИО]», «заявление [ФИО]» → род.п.: «Иванова Ивана Ивановича»
  «предоставить [ФИО]» → дат.п.: «Иванову Ивану Ивановичу»
  «является [ФИО]» → тв.п.
  Должности в середине предложения тоже склоняй: «принять на должность кассира-повара».

АББРЕВИАТУРЫ — всегда прописными буквами:
  «hr», «Hr», «HR» в должностях → «HR»; аналогично IT, PR, CEO, CFO, CTO и другие бизнес-сокращения.

ДОЛЖНОСТИ — первая буква заглавная, остальные строчные:
  «генеральный Директор» → «Генеральный директор»
  «ГЛАВНЫЙ ИНЖЕНЕР» → «Главный инженер»
  Применяй ко всем должностям, включая блок подписи документа.

ДАТЫ — формат ДД.ММ.ГГГГ переводи в полную русскую форму с учётом падежа:
  «15.12.2025» в контексте «от ... года» → «15 декабря 2025 года»
  «с 01.01.2026» → «с 1 января 2026 года»

ЧИСЛА — при необходимости переводи в словесную форму с учётом падежа:
  «в течение 30 [дней]» → «в течение тридцати дней»
  «30 (прописью)» → «30 (тридцать)»

ДУБЛИКАТЫ — если значение одного плейсхолдера уже полностью содержится в значении соседнего плейсхолдера в том же тексте:
  верни corrected_value: "" и changed: true — чтобы не дублировать информацию.
  Пример: Должность «Главный менеджер департамента кадров» + Подразделение «Департамент кадров» → Подразделение вернуть пустым.

НЕИЗМЕНЯЕМЫЕ значения (вернуть точно как есть):
  Названия организаций, подразделений, отделов, департаментов.
  Коды, регистрационные номера, номера договоров.
  Всё, что помечено как fixed_form в правилах.

ЕСЛИ ЗНАЧЕНИЕ УЖЕ КОРРЕКТНО для данного контекста — вернуть без изменений, changed: false.

Обрабатывай каждое вхождение строго независимо по паре placeholder + occurrence_index.
Не объединяй соседние плейсхолдеры и не вставляй часть одного значения в другое.

Верни строго JSON (без markdown, без пояснений):
{"occurrences":[{"placeholder":"...","occurrence_index":0,"corrected_value":"...","changed":true}]}
"""


def _rules_context(rules: Any) -> str:
    """Format rules config as human-readable text for AI."""
    if not isinstance(rules, dict):
        return ""
    lines: list[str] = []

    abbrevs = rules.get("business_abbreviations") or {}
    if abbrevs:
        lines.append("Аббревиатуры (всегда ПРОПИСНЫМИ): " + ", ".join(sorted(abbrevs.values())))

    dept = (rules.get("department_name_rules") or {}).get("placeholder_name_patterns") or []
    if dept:
        lines.append("Плейсхолдеры подразделений/департаментов " + str(dept) + " — не изменять (fixed_form, вернуть точно как есть)")

    preserve = rules.get("preserve_code_placeholder_patterns") or []
    if preserve:
        lines.append("Плейсхолдеры с именами по шаблонам " + str(preserve) + " — не изменять (коды/номера)")

    for phrase in rules.get("governing_phrases") or []:
        pid = phrase.get("id", "")
        behavior = phrase.get("behavior") or phrase.get("case") or ""
        ctx_pat = phrase.get("context_pattern", "")
        ph_pat = phrase.get("placeholder_name_pattern", "")
        if behavior and ctx_pat:
            lines.append(f"Правило [{pid}]: плейсхолдер «{ph_pat}» в контексте «{ctx_pat}» → применить «{behavior}»")

    return "\n".join(lines)


def _build_payload(
    full_text: str,
    occurrences: list[dict[str, Any]],
    rules: Any,
    prompt_ai: str,
) -> dict[str, Any]:
    ai_occs = [
        {
            "placeholder": str(o.get("key") or o.get("placeholder", "")),
            "occurrence_index": int(o.get("occurrence_index", 0)),
            "original_value": str(o.get("value") or o.get("original_value", "")),
            "context": str(o.get("context") or o.get("context_text", ""))[:200],
        }
        for o in occurrences
    ]

    rules_ctx = _rules_context(rules)

    user_content = (
        "ПОЛНЫЙ ТЕКСТ ДОКУМЕНТА:\n---\n"
        f"{full_text}\n"
        "---\n\n"
        "ПЛЕЙСХОЛДЕРЫ (все вхождения):\n"
        f"{json.dumps(ai_occs, ensure_ascii=False, indent=2)}\n"
    )
    if rules_ctx:
        user_content += f"\nПРАВИЛА ИЗ КОНФИГА:\n{rules_ctx}\n"
    if prompt_ai:
        user_content += f"\nИНСТРУКЦИЯ ПОЛЬЗОВАТЕЛЯ: {prompt_ai}\n"

    return {
        "model": os.environ.get("OPENAI_PLACEHOLDER_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini")),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
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


def request_ai_corrections(
    full_text: str,
    occurrences: list[dict[str, Any]],
    rules: Any,
    prompt_ai: str,
    log_key: str | None = None,
    call_log: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[tuple[str, int], str]:
    """Call OpenAI and return {(placeholder, occurrence_index): corrected_value}."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")

    if not api_key:
        logger.warning("OPENAI_API_KEY not configured: log_key=%s", log_key)
        if call_log is not None:
            call_log["error"] = "OPENAI_API_KEY is not configured"
        return {}

    payload = _build_payload(full_text, occurrences, rules, prompt_ai)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    if call_log is not None:
        call_log["key"] = log_key
        call_log["request"] = {"url": f"{base_url}/chat/completions", "body": payload}

    logger.info("AI correction request: log_key=%s doc_chars=%d occurrences=%d", log_key, len(full_text), len(occurrences))

    req = Request(
        f"{base_url}/chat/completions",
        data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read()
            raw_text = raw.decode("utf-8", errors="replace")
            if call_log is not None:
                try:
                    resp_body: Any = json.loads(raw_text)
                except json.JSONDecodeError:
                    resp_body = raw_text
                call_log["response"] = {"status": getattr(resp, "status", None), "body": resp_body}
            logger.info("AI correction response: log_key=%s body=%s", log_key, raw_text)
            content = parse_openai_chat_content(raw)
    except HTTPError as exc:
        err_text = exc.read().decode("utf-8", errors="replace")
        if call_log is not None:
            call_log["response"] = {"status": exc.code, "body": err_text}
            call_log["error"] = f"OpenAI HTTP {exc.code}"
        logger.error("AI HTTP error: log_key=%s status=%s", log_key, exc.code)
        raise

    parsed = json.loads(content)
    if not isinstance(parsed, dict):
        raise ValueError("AI response is not a JSON object")

    corrections: dict[tuple[str, int], str] = {}
    for item in parsed.get("occurrences") or []:
        if not isinstance(item, dict):
            continue
        placeholder = str(item.get("placeholder") or "").strip()
        try:
            occ_idx = int(item.get("occurrence_index", 0))
        except (TypeError, ValueError):
            continue
        corrected = item.get("corrected_value")
        if placeholder and corrected is not None:
            corrections[(placeholder, occ_idx)] = str(corrected)

    return corrections
