from __future__ import annotations

import sys

if __name__ == "__main__" and sys.path and sys.path[0].replace("\\", "/").endswith("/word_constructor/ai_correction"):
    sys.path.pop(0)

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class Case(str, Enum):
    NOMINATIVE = "именительный"
    GENITIVE = "родительный"
    DATIVE = "дательный"
    ACCUSATIVE = "винительный"
    INSTRUMENTAL = "творительный"
    PREPOSITIONAL = "предложный"
    NO_CHANGE = "без_изменений"


@dataclass(frozen=True)
class GoverningRule:
    pattern: str
    case: Case
    note: str


GOVERNING_RULES: list[GoverningRule] = [
    GoverningRule(
        pattern=r"принять\s*$",
        case=Case.ACCUSATIVE,
        note='"Принять [кого]" -> accusative; animate nouns use genitive-looking form',
    ),
    GoverningRule(
        pattern=r"на\s+должность\s*$",
        case=Case.GENITIVE,
        note='"на должность [кого/чего]" -> genitive',
    ),
    GoverningRule(
        pattern=r"\bна\s*$",
        case=Case.GENITIVE,
        note='bare "на [Должность]" -> genitive',
    ),
    GoverningRule(
        pattern=r"сектора\s*$",
        case=Case.GENITIVE,
        note='"сектора [ПодразделениеОрганизации]" -> genitive',
    ),
    GoverningRule(
        pattern=r"департамента\s*$",
        case=Case.GENITIVE,
        note='"департамента [Наименование]" -> genitive',
    ),
    GoverningRule(
        pattern=r"заявление\s+от\s*$",
        case=Case.GENITIVE,
        note='"заявление от [ФИО]" -> genitive',
    ),
    GoverningRule(
        pattern=r"заявление\s*$",
        case=Case.GENITIVE,
        note='"заявление [ФИО]" -> genitive',
    ),
    GoverningRule(
        pattern=r"^\s*$",
        case=Case.NO_CHANGE,
        note="bare placeholder / signature label -> no case change",
    ),
]

DATIVE_GOVERNING_VERBS = [
    "предоставить",
    "назначить",
    "объявить",
    "выплатить",
    "выразить благодарность",
    "присвоить",
]

ACCUSATIVE_GOVERNING_VERBS = [
    "принять",
    "уволить",
    "перевести",
    "уведомить",
]


def normalize_context(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def detect_case_for_placeholder(text_before: str, text_after_in_sentence: str) -> tuple[Case, str]:
    norm_before = normalize_context(text_before)
    norm_after = normalize_context(text_after_in_sentence)

    if any(norm_after.startswith(verb) for verb in ACCUSATIVE_GOVERNING_VERBS):
        return Case.ACCUSATIVE, "accusative_verb_immediately_follows"

    if any(verb in norm_after for verb in DATIVE_GOVERNING_VERBS):
        return Case.DATIVE, "dative_verb_follows_in_sentence"

    for rule in GOVERNING_RULES:
        if rule.case == Case.NO_CHANGE:
            continue
        if re.search(rule.pattern, norm_before):
            return rule.case, rule.note

    if not norm_before and not any(v in norm_after for v in DATIVE_GOVERNING_VERBS):
        return Case.NO_CHANGE, "empty_context_no_governing_verb_treated_as_label"

    return Case.NO_CHANGE, "no_rule_matched_safe_default"


def log_unknown_governor(text_before: str, placeholder_key: str, log_path: str = "unknown_governors.jsonl") -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "placeholder": placeholder_key,
        "text_before": text_before,
    }
    path = Path(log_path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    test_cases = [
        ("", "принять с 15.12.2025 на должность", Case.ACCUSATIVE, "placeholder before принять"),
        ("на должность", "сектора", Case.GENITIVE, "на должность"),
        ("сектора", "департамента", Case.GENITIVE, "сектора"),
        ("заявление", "трудовой договор от", Case.GENITIVE, "заявление"),
        ("", "предоставить ежегодный трудовой отпуск", Case.DATIVE, "dative verb follows"),
        ("", "", Case.NO_CHANGE, "bare signature"),
        ("Генеральный директор / И.о. генерального директора", "", Case.NO_CHANGE, "ambiguous signature tail"),
    ]
    all_pass = True
    print(f"{'EXPECTED':15s} {'GOT':15s} NOTE")
    print("-" * 100)
    for before, after, expected, desc in test_cases:
        got, note = detect_case_for_placeholder(before, after)
        ok = got == expected
        all_pass = all_pass and ok
        status = "OK" if ok else "MISMATCH"
        print(f"{expected.value:15s} {got.value:15s} {status} ({desc}) -> {note}")
    print()
    print("ALL TESTS PASSED" if all_pass else "SOME TESTS FAILED - refine rules above")
