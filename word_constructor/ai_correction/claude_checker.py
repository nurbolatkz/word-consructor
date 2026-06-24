from __future__ import annotations

import sys

if __name__ == "__main__" and sys.path and sys.path[0].replace("\\", "/").endswith("/word_constructor/ai_correction"):
    sys.path.pop(0)

import json
import os
from dataclasses import dataclass
from typing import Any

try:  # Optional dependency; production enables this with ANTHROPIC_API_KEY.
    from anthropic import Anthropic

    _ANTHROPIC_AVAILABLE = True
except Exception:  # pragma: no cover - depends on deployment extras
    Anthropic = None
    _ANTHROPIC_AVAILABLE = False


CHECKER_SYSTEM_PROMPT = """Ты — независимый проверяющий для текста юридического/HR-документа на русском/казахском языке, ПОСЛЕ того как другая AI-система уже подставила и грамматически скорректировала значения плейсхолдеров.

Твоя ЕДИНСТВЕННАЯ задача — проверить готовый текст по фиксированному чек-листу и вернуть найденные проблемы. Ты НЕ исправляешь текст.

Тебе также может быть передан список known_pitfalls — примеры похожих ошибок из продакшена. Используй их как ориентир, но проверяй текущий текст самостоятельно.

ЧЕК-ЛИСТ:
1. has_duplication — повторяющиеся/почти повторяющиеся фразы рядом друг с другом.
2. has_fabricated_content — полное ФИО или другое значение заменено на сокращение/инициалы/другую информацию, которой не было во входных данных.
3. has_wrong_case_in_label — ФИО или должность в позиции подписи/метки без управляющего глагола выглядит склоненным не в именительном падеже.
4. has_other_grammar_issue — другая явная грамматическая ошибка.

Верни ТОЛЬКО валидный JSON в этом формате:
{
  "has_duplication": bool, "has_duplication_detail": str,
  "has_fabricated_content": bool, "has_fabricated_content_detail": str,
  "has_wrong_case_in_label": bool, "has_wrong_case_in_label_detail": str,
  "has_other_grammar_issue": bool, "has_other_grammar_issue_detail": str
}
Если проблемы нет — соответствующее поле false, а detail — пустая строка."""


REVIEW_QUEUE_SUMMARIZER_PROMPT = """Ты помогаешь человеку приоритизировать очередь кандидатов на новые грамматические правила для системы коррекции HR/юридических документов.

Для каждого кандидата дай:
1. confidence: "high" | "medium" | "low"
2. recommendation: что проверить или какое правило добавить, если человек согласится
3. risk_if_wrong: что может пойти не так, если правило неверное

Ты НЕ принимаешь решение и НЕ утверждаешь, что правило следует добавить автоматически.

Верни JSON: {"recommendations": [{"pattern_summary": str, "confidence": str, "recommendation": str, "risk_if_wrong": str}, ...]}"""


@dataclass(frozen=True)
class CheckResult:
    has_duplication: bool
    has_duplication_detail: str
    has_fabricated_content: bool
    has_fabricated_content_detail: str
    has_wrong_case_in_label: bool
    has_wrong_case_in_label_detail: str
    has_other_grammar_issue: bool
    has_other_grammar_issue_detail: str

    @property
    def needs_review(self) -> bool:
        return bool(
            self.has_duplication
            or self.has_fabricated_content
            or self.has_wrong_case_in_label
            or self.has_other_grammar_issue
        )

    def asdict(self) -> dict[str, Any]:
        return {
            "has_duplication": self.has_duplication,
            "has_duplication_detail": self.has_duplication_detail,
            "has_fabricated_content": self.has_fabricated_content,
            "has_fabricated_content_detail": self.has_fabricated_content_detail,
            "has_wrong_case_in_label": self.has_wrong_case_in_label,
            "has_wrong_case_in_label_detail": self.has_wrong_case_in_label_detail,
            "has_other_grammar_issue": self.has_other_grammar_issue,
            "has_other_grammar_issue_detail": self.has_other_grammar_issue_detail,
            "needs_review": self.needs_review,
        }


def claude_available() -> bool:
    return _ANTHROPIC_AVAILABLE and bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


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
    parts = []
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


def claude_verify(
    rendered_text: str,
    known_pitfalls: list[dict[str, Any]] | None = None,
    model: str | None = None,
    max_retries: int = 1,
) -> CheckResult:
    model = model or os.environ.get("ANTHROPIC_CHECKER_MODEL", "claude-sonnet-4-6")
    user_content: dict[str, Any] = {"rendered_text": rendered_text}
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
                max_tokens=500,
                system=CHECKER_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": json.dumps(user_content, ensure_ascii=False, indent=2)}],
            )
            parsed = json.loads(_strip_json_fence(_response_text(response)))
            return CheckResult(**parsed)
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                continue
    raise RuntimeError(f"Claude verification failed after {max_retries + 1} attempt(s): {last_error}")


def claude_summarize_review_queue(
    candidates: list[dict[str, Any]],
    model: str | None = None,
) -> dict[str, Any]:
    model = model or os.environ.get("ANTHROPIC_REVIEW_MODEL", os.environ.get("ANTHROPIC_CHECKER_MODEL", "claude-sonnet-4-6"))
    response = _client().messages.create(
        model=model,
        max_tokens=2000,
        system=REVIEW_QUEUE_SUMMARIZER_PROMPT,
        messages=[{"role": "user", "content": json.dumps(candidates, ensure_ascii=False, indent=2)}],
    )
    return json.loads(_strip_json_fence(_response_text(response)))


if __name__ == "__main__":
    rendered_text_with_bug = "Генеральный директор / И.о. генерального директора Есжанова З.С."
    known_pitfalls_example = [
        {
            "original_value": "Есжанова Зарина Серикалиевна",
            "corrected_value": "Есжанова З.С.",
            "note": "AI самопроизвольно сократил ФИО до инициалов в подписи — запрещено.",
        }
    ]
    print("Payload that WOULD be sent to Claude for verification:")
    print(json.dumps({"rendered_text": rendered_text_with_bug, "known_pitfalls": known_pitfalls_example}, ensure_ascii=False, indent=2))

    demo_candidates = [
        {
            "candidate_type": "case_drift",
            "pattern_summary": 'Placeholder "Должность" with original "кассир-повар" produced 3 different values',
            "occurrence_count": 3,
        }
    ]
    print("\nPayload that WOULD be sent to Claude for review-queue summarization:")
    print(json.dumps(demo_candidates, ensure_ascii=False, indent=2))
