from __future__ import annotations

import re
from typing import Callable

KAZAKH_SPECIFIC_RE = re.compile(r"[ҚқҒғҰұҮүҢңҺһӘәӨөІі]")
KAZAKH_PATRONYMIC_SUFFIXES = ("ұлы", "улы", "қызы", "кизы")


def has_kazakh_pattern(value: str) -> bool:
    words = (value or "").split()
    return bool(KAZAKH_SPECIFIC_RE.search(value or "") or any(word.lower().endswith(KAZAKH_PATRONYMIC_SUFFIXES) for word in words))


def fix_common_feminine_surname_case(original: str, declined: str, case: str) -> str:
    original_words = (original or "").split()
    declined_words = (declined or "").split()
    if len(original_words) < 2 or len(original_words) != len(declined_words):
        return declined
    surname = original_words[0]
    lower = surname.lower()
    replacement = None
    if lower.endswith(("ова", "ева", "ина")):
        stem = surname[:-1]
        if case == "accs":
            replacement = stem + "у"
        elif case in {"gent", "datv", "loct", "ablt"}:
            replacement = stem + "ой"
    elif lower.endswith("ая"):
        stem = surname[:-2]
        if case == "accs":
            replacement = stem + "ую"
        elif case in {"gent", "datv", "loct", "ablt"}:
            replacement = stem + "ой"
    if replacement:
        declined_words[0] = replacement
    return " ".join(declined_words)


def preserve_kazakh_patronymic_suffixes(original: str, declined: str) -> str:
    original_words = (original or "").split()
    declined_words = (declined or "").split()
    if len(original_words) != len(declined_words):
        return declined
    for idx, word in enumerate(original_words):
        if word.lower().endswith(KAZAKH_PATRONYMIC_SUFFIXES):
            declined_words[idx] = word
    return " ".join(declined_words)


def conservative_decline_kazakh_name(original: str, case: str, decline_func: Callable[[str, str], str]) -> str:
    words = (original or "").split()
    if len(words) < 2:
        return original
    declined_words: list[str] = []
    for word in words:
        lower = word.lower()
        if lower.endswith(KAZAKH_PATRONYMIC_SUFFIXES):
            declined_words.append(word)
            continue
        declined = decline_func(word, case)
        if not declined or len(declined.split()) != 1:
            declined = word
        declined_words.append(declined)
    changed_non_patronymic = any(
        a != b for a, b in zip(words, declined_words)
        if not a.lower().endswith(KAZAKH_PATRONYMIC_SUFFIXES)
    )
    return " ".join(declined_words) if changed_non_patronymic else original


def decline_value(value: str, case: str, decline_func: Callable[[str, str], str]) -> str:
    if has_kazakh_pattern(value):
        return conservative_decline_kazakh_name(value, case, decline_func)
    declined = decline_func(value, case)
    declined = fix_common_feminine_surname_case(value, declined, case)
    return preserve_kazakh_patronymic_suffixes(value, declined)
