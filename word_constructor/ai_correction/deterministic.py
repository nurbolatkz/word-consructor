from __future__ import annotations

import re
from typing import Any, Callable

from .morphology import decline_value
from .rules import RulesConfig, business_abbreviations, governing_phrases, is_department_placeholder

RU_MONTHS_GENT = [
    "", "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]


def format_ru_date_no_year_word(value: str) -> str:
    match = re.fullmatch(r"\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2}|\d{4})\s*", value or "")
    if not match:
        return value
    day, month, year = match.groups()
    month_idx = int(month)
    if not 1 <= month_idx <= 12:
        return value
    if len(year) == 2:
        year = f"20{year}"
    return f"{int(day):02d} {RU_MONTHS_GENT[month_idx]} {year}"


def format_ru_date_full(value: str) -> str:
    """'15.12.2025' → '15 декабря 2025 года'"""
    match = re.fullmatch(r"\s*(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2}|\d{4})\s*", value or "")
    if not match:
        return value
    day, month, year = match.groups()
    month_idx = int(month)
    if not 1 <= month_idx <= 12:
        return value
    if len(year) == 2:
        year = f"20{year}"
    return f"{int(day)} {RU_MONTHS_GENT[month_idx]} {year} года"


def format_number_words_ru(value: str, case: str = "nomn") -> str:
    """Convert numeric string to Russian words, optionally declined.

    '30' → 'тридцать' (nomn), 'тридцати' (gent / datv / loct / ablt).
    Falls back gracefully if num2words or pymorphy3 unavailable.
    """
    cleaned = re.sub(r"[\s ,]", "", str(value).strip())
    try:
        n = int(cleaned)
    except (ValueError, TypeError):
        return value
    try:
        from num2words import num2words as _n2w
        words_str = _n2w(n, lang="ru")
    except Exception:
        return value
    if case == "nomn":
        return words_str
    try:
        import pymorphy3 as _pm
        morph = _pm.MorphAnalyzer()
        declined: list[str] = []
        for word in words_str.split():
            parsed = morph.parse(word)
            best = parsed[0] if parsed else None
            if best:
                inflected = best.inflect({case})
                declined.append(inflected.word if inflected else word)
            else:
                declined.append(word)
        return " ".join(declined)
    except Exception:
        return words_str


def format_days_bracket(value: str) -> str:
    """'30' → '30 (тридцать)' — number with Russian prose in parentheses."""
    words = format_number_words_ru(value, case="nomn")
    if words == value:
        return value
    n = re.sub(r"[\s ,]", "", str(value).strip())
    return f"{n} ({words})"


def is_code_like_token(token: str) -> bool:
    cleaned = token.strip(".,;:()[]{}«»\"'")
    return bool(2 <= len(cleaned) <= 12 and re.search(r"[A-ZА-ЯЁҰҚІҒӘҺӨҮ]", cleaned) and cleaned.upper() == cleaned)


def normalize_common_business_abbreviations(value: str, rules: RulesConfig | None = None) -> str:
    replacements = business_abbreviations(rules)
    if not replacements:
        return value
    pattern = r"\b(?:" + "|".join(re.escape(k) for k in sorted(replacements, key=len, reverse=True)) + r")\b"
    return re.sub(pattern, lambda m: replacements.get(m.group(0).lower(), m.group(0)), value or "", flags=re.IGNORECASE)


def preserve_internal_abbreviations(original: str, corrected: str) -> str:
    if not original or not corrected:
        return corrected
    original_tokens = original.split()
    corrected_tokens = corrected.split()
    if not corrected_tokens:
        return corrected
    for token in original_tokens:
        if not is_code_like_token(token):
            continue
        stripped = token.strip(".,;:()[]{}«»\"'")
        if stripped not in corrected_tokens:
            lower_matches = [idx for idx, item in enumerate(corrected_tokens) if item.strip(".,;:()[]{}«»\"'").lower() == stripped.lower()]
            for idx in lower_matches:
                corrected_tokens[idx] = token
    return " ".join(corrected_tokens)


def is_title_or_department_key(key: str) -> bool:
    lower_key = key.lower()
    return any(part in lower_key for part in ("должност", "позици", "подраздел", "департамент", "отдел", "управлен", "title", "position", "department", "division"))


def should_preserve_ai_corrected_value(key: str, original: str, corrected: str, rules: RulesConfig | None = None) -> bool:
    if not corrected or corrected == original:
        return False
    lower_key = key.lower()
    if is_department_placeholder(key, rules) or any(part in lower_key for part in ("подраздел", "департамент", "отдел", "управлен", "department", "division")):
        if any(is_code_like_token(token) for token in original.split()):
            corrected_tokens = corrected.split()
            for token in original.split():
                if is_code_like_token(token) and token not in corrected_tokens:
                    return True
    return False


def normalize_signature_title(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    if not cleaned:
        return value
    lowered = cleaned.lower()
    return lowered[:1].upper() + lowered[1:]


def _title_word(word: str) -> str:
    if not word:
        return word
    return word[:1].upper() + word[1:].lower()


def normalize_signature_name(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    if not cleaned:
        return value

    # Already abbreviated initials + surname, e.g. Ф.ОХОНОВ -> Ф.Охонов.
    compact = re.fullmatch(r"(?P<initials>(?:[А-ЯЁA-Z]\.){1,3})(?P<surname>[А-ЯЁA-Z]{2,}(?:-[А-ЯЁA-Z]{2,})*)", cleaned)
    if compact:
        return compact.group("initials").upper() + _title_word(compact.group("surname"))

    spaced = re.fullmatch(r"(?P<initials>(?:[А-ЯЁA-Z]\.?\s*){1,3})\s+(?P<surname>[А-ЯЁA-Z]{2,}(?:-[А-ЯЁA-Z]{2,})*)", cleaned)
    if spaced:
        initials = "".join(part + "." for part in re.findall(r"[А-ЯЁA-Z]", spaced.group("initials"))).upper()
        return initials + _title_word(spaced.group("surname"))

    if "." in cleaned:
        return cleaned

    # Full surname name patronymic -> surname initials, e.g. Есжанова Зарина Серикалиевна -> Есжанова З.С.
    words = cleaned.split()
    if len(words) == 3 and all(re.fullmatch(r"[А-ЯЁA-ZӘІҢҒҮҰҚӨҺа-яёa-zәіңғүұқөһ\-]+", word) for word in words):
        surname, first, patronymic = words
        return f"{_title_word(surname)} {first[:1].upper()}.{patronymic[:1].upper()}."

    if 1 <= len(words) <= 4 and any(word.isupper() and len(word) > 1 for word in words):
        fixed_words = []
        for word in words:
            if re.fullmatch(r"(?:[А-ЯЁA-Z]\.){1,3}", word):
                fixed_words.append(word.upper())
            elif word.isupper() and len(word) > 1:
                fixed_words.append(_title_word(word))
            else:
                fixed_words.append(word)
        return " ".join(fixed_words)
    return cleaned


def _placeholder_context_pattern(pattern: str, key: str) -> str:
    return pattern.replace("{placeholder}", re.escape(key))


def case_hint_for_placeholder_occurrence(key: str, context: str, rules: RulesConfig | None = None) -> str | None:
    lower_context = (context or "").lower()
    for rule in governing_phrases(rules):
        placeholder_pattern = str(rule.get("placeholder_name_pattern") or ".*")
        try:
            if not re.fullmatch(placeholder_pattern, key, flags=re.IGNORECASE):
                continue
        except re.error:
            continue
        context_pattern = _placeholder_context_pattern(str(rule.get("context_pattern") or ""), key).lower()
        if not context_pattern:
            continue
        try:
            if re.search(context_pattern, lower_context, flags=re.IGNORECASE):
                return str(rule.get("behavior") or rule.get("case") or "") or None
        except re.error:
            continue
    return None


def _contains_adjacent_fragment(candidate: str, other: str) -> bool:
    candidate_lower = (candidate or "").lower()
    for token in re.findall(r"[A-Za-zА-Яа-яЁёӘәІіҢңҒғҮүҰұҚқӨөҺһ0-9]{4,}", other or ""):
        if token.lower() in candidate_lower:
            return True
    return False


def validate_no_adjacent_contamination(
    corrected: str,
    occurrence: dict[str, Any],
    occurrences_by_id: dict[str, dict[str, Any]],
    occurrence_values: dict[tuple[str, int], str],
) -> bool:
    for adjacent_id in occurrence.get("adjacent_occurrence_ids") or []:
        adjacent = occurrences_by_id.get(str(adjacent_id))
        if not adjacent:
            continue
        adjacent_original = str(adjacent.get("value") or "")
        adjacent_key = (str(adjacent.get("key") or ""), int(adjacent.get("occurrence") or 0))
        adjacent_corrected = occurrence_values.get(adjacent_key, "")
        if _contains_adjacent_fragment(corrected, adjacent_original) or (adjacent_corrected and _contains_adjacent_fragment(corrected, adjacent_corrected)):
            return False
    return True


def apply_deterministic_case_hints(
    occurrence_values: dict[tuple[str, int], str],
    occurrences: list[dict[str, Any]],
    decline_func: Callable[[str, str], str],
    rules: RulesConfig | None = None,
) -> None:
    occurrences_by_id = {str(item.get("id")): item for item in occurrences}
    for item in occurrences:
        key = str(item.get("key") or "")
        occurrence = int(item.get("occurrence") or 0)
        value = str(item.get("value") or "")
        if not key or not occurrence:
            continue
        target = (key, occurrence)
        if item.get("redundant_in"):
            occurrence_values[target] = ""
            continue
        if item.get("ai_excluded"):
            occurrence_values[target] = normalize_signature_title(value) if item.get("signature_title_normalize") else normalize_signature_name(value)
            continue
        if item.get("fixed_form") or is_department_placeholder(key, rules):
            fixed = normalize_common_business_abbreviations(value, rules)
            if item.get("preserve_internal_abbreviations"):
                fixed = preserve_internal_abbreviations(value, fixed)
            occurrence_values[target] = fixed
            continue
        if value and is_title_or_department_key(key):
            normalized = normalize_common_business_abbreviations(value, rules)
            occurrence_values.setdefault(target, normalized)
        hint = case_hint_for_placeholder_occurrence(key, str(item.get("context") or ""), rules)
        if not value or not hint:
            continue
        if hint == "preserve":
            occurrence_values[target] = value
        elif hint == "date_ru_no_year_word":
            occurrence_values[target] = format_ru_date_no_year_word(value)
        elif hint == "date_ru_full":
            occurrence_values[target] = format_ru_date_full(value)
        elif hint == "days_bracket":
            occurrence_values[target] = format_days_bracket(value)
        elif hint == "days_words_gent":
            occurrence_values[target] = format_number_words_ru(value, case="gent")
        elif hint == "days_words_nomn":
            occurrence_values[target] = format_number_words_ru(value, case="nomn")
        elif hint in {"gent", "datv", "accs", "loct", "ablt", "nomn"}:
            occurrence_values[target] = decline_value(value, hint, decline_func)

    for item in occurrences:
        key = str(item.get("key") or "")
        occurrence = int(item.get("occurrence") or 0)
        target = (key, occurrence)
        corrected = occurrence_values.get(target)
        if corrected is None:
            continue
        if item.get("never_merge_with_adjacent_occurrence") and not validate_no_adjacent_contamination(corrected, item, occurrences_by_id, occurrence_values):
            occurrence_values[target] = str(item.get("value") or "")
