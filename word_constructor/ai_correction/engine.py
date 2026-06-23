from __future__ import annotations

import re
from typing import Any

from .deterministic import (
    format_ru_date_no_year_word,
    is_code_like_token,
    normalize_common_business_abbreviations,
    normalize_signature_name,
    preserve_internal_abbreviations,
)
from .morphology import MorphologyProvider
from .rules import GoverningPhraseRule, GoverningPhraseRules
from .types import CorrectionResult, Occurrence

_CODE_VALUE_RE = re.compile(r"(?:\d+[/\-]\d+|№\s*\d+|[A-Za-zА-Яа-я0-9]+-[A-Za-zА-Яа-я0-9]+)")


class CorrectionEngine:
    def __init__(self, rules: GoverningPhraseRules, morphology: MorphologyProvider, openai_client=None):
        self.rules = rules
        self.morphology = morphology
        self.openai_client = openai_client

    def _placeholder_literal(self, occurrence: Occurrence) -> str:
        return occurrence.literal_placeholder or f"[{occurrence.placeholder}]"

    def _matches_department(self, placeholder: str) -> bool:
        for pattern in self.rules.department_name_patterns:
            try:
                if re.fullmatch(pattern, placeholder, flags=re.IGNORECASE):
                    return True
            except re.error:
                continue
        return False

    def _matching_rule(self, occurrence: Occurrence) -> GoverningPhraseRule | None:
        literal = re.escape(self._placeholder_literal(occurrence))
        bare = re.escape(occurrence.placeholder)
        for rule in self.rules.name_case_rules:
            pattern = rule.pattern.replace("{placeholder}", bare)
            # Public config stores patterns with literal square brackets around {placeholder}.
            pattern = pattern.replace(r"\[" + bare + r"\]", literal)
            try:
                if re.search(pattern, occurrence.context_text, flags=re.IGNORECASE):
                    return rule
            except re.error:
                continue
        return None

    def _business_abbreviation_result(self, occurrence: Occurrence) -> CorrectionResult | None:
        corrected = normalize_common_business_abbreviations(
            occurrence.original_value,
            _RulesAdapter(self.rules),
        )
        if corrected != occurrence.original_value:
            return CorrectionResult(
                occurrence.placeholder,
                occurrence.occurrence_index,
                occurrence.original_value,
                corrected,
                True,
                "deterministic",
                "business_abbreviation_casing",
            )
        return None

    def correct_occurrence(self, occurrence: Occurrence) -> CorrectionResult:
        """Correct one occurrence using deterministic rules first, then optional AI."""
        try:
            if _CODE_VALUE_RE.search(occurrence.original_value) or any(is_code_like_token(t) for t in occurrence.original_value.split() if any(ch.isdigit() for ch in t)):
                return CorrectionResult(occurrence.placeholder, occurrence.occurrence_index, occurrence.original_value, occurrence.original_value, False, "deterministic", "preserve_code")

            if self._matches_department(occurrence.placeholder):
                corrected = normalize_common_business_abbreviations(occurrence.original_value, _RulesAdapter(self.rules))
                corrected = preserve_internal_abbreviations(occurrence.original_value, corrected)
                return CorrectionResult(occurrence.placeholder, occurrence.occurrence_index, occurrence.original_value, corrected, corrected != occurrence.original_value, "deterministic", "department_fixed_form")

            if any(token.upper() in self.rules.preserve_abbreviations for token in occurrence.original_value.split()):
                corrected = preserve_internal_abbreviations(occurrence.original_value, occurrence.original_value)
                return CorrectionResult(occurrence.placeholder, occurrence.occurrence_index, occurrence.original_value, corrected, corrected != occurrence.original_value, "deterministic", "preserve_abbreviation")

            abbr = self._business_abbreviation_result(occurrence)
            if abbr is not None:
                return abbr

            if "дата" in occurrence.placeholder.lower() and re.search(r"(?:^|\s)от\s+" + re.escape(self._placeholder_literal(occurrence)) + r"\s*(?:года|г\.|год)", occurrence.context_text, flags=re.IGNORECASE):
                corrected = format_ru_date_no_year_word(occurrence.original_value)
                return CorrectionResult(occurrence.placeholder, occurrence.occurrence_index, occurrence.original_value, corrected, corrected != occurrence.original_value, "deterministic", "date_ru_no_year_word")

            rule = self._matching_rule(occurrence)
            if rule is not None:
                corrected = self.morphology.decline(occurrence.original_value, rule.case)
                return CorrectionResult(occurrence.placeholder, occurrence.occurrence_index, occurrence.original_value, corrected, corrected != occurrence.original_value, "deterministic", rule.id)

            if self.openai_client is not None:
                corrections = self.openai_client.correct([occurrence], "")
                corrected = corrections.get((occurrence.placeholder, occurrence.occurrence_index), occurrence.original_value)
                return CorrectionResult(occurrence.placeholder, occurrence.occurrence_index, occurrence.original_value, corrected, corrected != occurrence.original_value, "ai")

            return CorrectionResult(occurrence.placeholder, occurrence.occurrence_index, occurrence.original_value, occurrence.original_value, False, "fallback_unchanged")
        except Exception as exc:
            return CorrectionResult(occurrence.placeholder, occurrence.occurrence_index, occurrence.original_value, occurrence.original_value, False, "fallback_unchanged", error=str(exc))

    def correct_document(self, occurrences: list[Occurrence], promt_ai: str = "") -> list[CorrectionResult]:
        if self.openai_client is None:
            return [self.correct_occurrence(item) for item in occurrences]

        deterministic: list[CorrectionResult | None] = []
        ai_needed: list[Occurrence] = []
        for occurrence in occurrences:
            old_client = self.openai_client
            self.openai_client = None
            result = self.correct_occurrence(occurrence)
            self.openai_client = old_client
            if result.source == "fallback_unchanged":
                deterministic.append(None)
                ai_needed.append(occurrence)
            else:
                deterministic.append(result)

        ai_results: dict[tuple[str, int], str] = {}
        if ai_needed:
            ai_results = self.openai_client.correct(ai_needed, promt_ai)

        output: list[CorrectionResult] = []
        ai_iter = iter(ai_needed)
        for result in deterministic:
            if result is not None:
                output.append(result)
                continue
            occurrence = next(ai_iter)
            corrected = ai_results.get((occurrence.placeholder, occurrence.occurrence_index), occurrence.original_value)
            rule = self._matching_rule(occurrence)
            if rule is not None:
                corrected = self.morphology.decline(occurrence.original_value, rule.case)
                output.append(CorrectionResult(occurrence.placeholder, occurrence.occurrence_index, occurrence.original_value, corrected, corrected != occurrence.original_value, "deterministic", rule.id))
            else:
                output.append(CorrectionResult(occurrence.placeholder, occurrence.occurrence_index, occurrence.original_value, corrected, corrected != occurrence.original_value, "ai"))
        return output

    def normalize_signature_name(self, raw_name: str) -> str:
        """Handles both 'Ф.ОХОНОВ' and full surname-name-patronymic signature values."""
        return normalize_signature_name(raw_name)


class _RulesAdapter:
    def __init__(self, rules: GoverningPhraseRules):
        self.data = {
            "business_abbreviations": {item.lower(): item for item in rules.business_abbreviations},
            "department_name_rules": {"placeholder_name_patterns": rules.department_name_patterns},
        }
