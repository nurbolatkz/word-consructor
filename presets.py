"""
AI pipeline presets for the /api/ai/apply-prompts endpoint.

Each preset has either:
  "prompts": [str, ...]          — legacy, each string becomes a "text" step
  "steps":   [{"type":..., "prompt":...}, ...]  — rich pipeline with step types

Step types:
  "text"               — grammar/style correction applied to all paragraphs
  "translate_bilingual"— detects sequential KZ/RU layout and fills empty KZ paragraphs
"""
from __future__ import annotations

PRESETS = {
    "legal_order": {
        "name": "Юридический приказ",
        "prompts": [
            "Fix grammar, spelling, and word-boundary errors caused by template substitution "
            "(e.g. merged words like «ИвановымдолжностьИвановичем» → «Ивановым Иваном Ивановичем должность»). "
            "Keep all placeholders and legal references intact.",
            "Normalize punctuation: remove double spaces, standardize quotation marks to «», use em-dash —. "
            "Do not change wording.",
        ],
    },
    "grammar_only": {
        "name": "Только грамматика",
        "prompts": [
            "Fix grammar and spelling errors only. "
            "Do not change style, tone, structure, or any placeholder tokens.",
        ],
    },
    "formal_kz": {
        "name": "Официальный стиль (рус/каз)",
        "prompts": [
            "Fix grammar, spelling, and word-boundary errors caused by template substitution. "
            "Keep all placeholders intact.",
            "Rewrite in a formal, official document style appropriate for Kazakhstani government or enterprise documents. "
            "Preserve the original language of each paragraph (Russian stays Russian, Kazakh stays Kazakh).",
        ],
    },
    "cleanup_spaces": {
        "name": "Очистка пробелов и пунктуации",
        "prompts": [
            "Fix only spacing and punctuation issues: remove duplicate spaces, fix spaces around punctuation, "
            "standardize quotation marks to «», use em-dash — instead of hyphen where appropriate. "
            "Do not change any words, grammar, or placeholders.",
        ],
    },
    "bilingual_kz_ru": {
        "name": "Двуязычный документ (каз/рус)",
        "steps": [
            {
                "type": "text",
                "prompt": (
                    "Fix grammar, spelling, and word-boundary errors from template substitution. "
                    "Keep all [placeholders] intact. "
                    "Preserve the original language of each paragraph "
                    "(Russian stays Russian, Kazakh stays Kazakh)."
                ),
            },
            {
                "type": "translate_bilingual",
                "prompt": (
                    "Translate missing or incomplete Kazakh paragraphs from their Russian equivalents. "
                    "Use formal official Kazakhstani legal document style (іс қағаздары тілі). "
                    "Keep all placeholders like [Сотрудник], [должность], [ДатаПриказа] "
                    "identical and untranslated."
                ),
            },
        ],
    },
}
