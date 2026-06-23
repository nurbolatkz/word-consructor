from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TextUnit:
    source_type: str
    source_path: str
    text: str
    table_index: int | None = None
    row_index: int | None = None
    cell_index: int | None = None
    row_cell_texts: tuple[str, ...] = ()
    ai_excluded: bool = False


@dataclass
class Occurrence:
    placeholder: str
    occurrence_index: int
    original_value: str
    source_type: str
    source_path: str
    context_text: str
    id: str = ""
    key: str = ""
    occurrence: int = 0
    value: str = ""
    context: str = ""
    context_with_value: str = ""
    literal_placeholder: str = ""
    ai_excluded: bool = False
    ai_exclusion_reason: str = ""
    signature_title_normalize: bool = False
    deterministic_behavior: str = ""
    expected_case: str = ""
    fixed_form: bool = False
    never_merge_with_adjacent_occurrence: bool = False
    preserve_internal_abbreviations: bool = False
    adjacent_occurrence_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.key:
            self.key = self.placeholder
        if not self.value:
            self.value = self.original_value
        if not self.context:
            self.context = self.context_text
        if not self.occurrence:
            self.occurrence = self.occurrence_index + 1
        if not self.id:
            self.id = f"{self.placeholder}#{self.occurrence_index}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "key": self.key,
            "placeholder": self.placeholder,
            "occurrence": self.occurrence,
            "occurrence_index": self.occurrence_index,
            "value": self.value,
            "original_value": self.original_value,
            "context": self.context,
            "context_text": self.context_text,
            "context_with_value": self.context_with_value,
            "source_type": self.source_type,
            "source_path": self.source_path,
            "literal_placeholder": self.literal_placeholder,
            "ai_excluded": self.ai_excluded,
            "ai_exclusion_reason": self.ai_exclusion_reason,
            "signature_title_normalize": self.signature_title_normalize,
            "deterministic_behavior": self.deterministic_behavior,
            "expected_case": self.expected_case,
            "fixed_form": self.fixed_form,
            "never_merge_with_adjacent_occurrence": self.never_merge_with_adjacent_occurrence,
            "preserve_internal_abbreviations": self.preserve_internal_abbreviations,
            "adjacent_occurrence_ids": list(self.adjacent_occurrence_ids),
        }


@dataclass
class CorrectionResult:
    placeholder: str
    occurrence_index: int
    original_value: str
    corrected_value: str
    changed: bool
    source: str
    rule_id: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SanityCheck:
    ok: bool
    full_text_raw_match_count: int
    raw_match_count: int
    occurrence_count: int
    raw_matches: list[dict[str, Any]]


@dataclass
class PipelineCorrectionResult:
    slot_values: dict[str, str]
    occurrence_values: dict[tuple[str, int], str]
    occurrences: list[dict[str, Any]] = field(default_factory=list)
    sanity_check: dict[str, Any] = field(default_factory=dict)
    ai_skipped_reason: str = ""
