from __future__ import annotations

import sys

if __name__ == "__main__" and sys.path and sys.path[0].replace("\\", "/").endswith("/word_constructor/ai_correction"):
    sys.path.pop(0)

import re
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlaceholderContext:
    key: str
    original: str
    corrected: str
    context_type: str
    text_before: str = ""
    text_after: str = ""


@dataclass(frozen=True)
class VerificationIssue:
    placeholder: str
    issue_type: str
    detail: str


NOMINATIVE_TO_NON_NOMINATIVE_PATTERNS = [
    ("на", "ной"),
    ("на", "не"),
    ("ва", "вой"),
    ("а", "ой"),
    ("а", "е"),
    ("я", "ей"),
    ("я", "е"),
]


def context_type_from_occurrence(item: dict[str, Any]) -> str:
    if item.get("ai_excluded"):
        return "label"
    if str(item.get("detected_case") or "") == "без_изменений":
        note = str(item.get("case_detection_note") or "")
        if "label" in note or "empty_context" in note or "no_rule_matched" in note:
            return "label"
    return "sentence"


def contexts_from_occurrences(
    occurrences: list[dict[str, Any]],
    corrected_by_key: dict[str, str],
) -> list[PlaceholderContext]:
    contexts: list[PlaceholderContext] = []
    for item in occurrences:
        key = str(item.get("key") or item.get("placeholder") or "")
        if not key:
            continue
        original = str(item.get("value") or item.get("original_value") or "")
        contexts.append(
            PlaceholderContext(
                key=key,
                original=original,
                corrected=str(corrected_by_key.get(key, original)),
                context_type=context_type_from_occurrence(item),
                text_before=str(item.get("text_before") or ""),
                text_after=str(item.get("text_after") or ""),
            )
        )
    return contexts


def check_fabrication(ctx: PlaceholderContext) -> VerificationIssue | None:
    orig_words = [w for w in re.split(r"\s+", ctx.original.strip()) if w]
    corrected_words = [w for w in re.split(r"\s+", ctx.corrected.strip()) if w]
    if ctx.context_type == "label" and len(corrected_words) < len(orig_words):
        return VerificationIssue(
            placeholder=ctx.key,
            issue_type="fabrication",
            detail=(
                f"Word count dropped from {len(orig_words)} to {len(corrected_words)}: "
                f"{ctx.original!r} -> {ctx.corrected!r}"
            ),
        )
    return None


def check_label_case_drift(ctx: PlaceholderContext) -> VerificationIssue | None:
    if ctx.context_type != "label" or ctx.corrected == ctx.original:
        return None
    orig_last_word = ctx.original.strip().split()[-1] if ctx.original.strip() else ""
    corrected_last_word = ctx.corrected.strip().split()[-1] if ctx.corrected.strip() else ""
    if orig_last_word == corrected_last_word:
        return None

    orig_lower = orig_last_word.lower()
    corr_lower = corrected_last_word.lower()
    for orig_ending, corr_ending in NOMINATIVE_TO_NON_NOMINATIVE_PATTERNS:
        if orig_lower.endswith(orig_ending) and corr_lower.endswith(corr_ending):
            orig_stem = orig_lower[: -len(orig_ending)]
            corr_stem = corr_lower[: -len(corr_ending)]
            if orig_stem and orig_stem == corr_stem:
                return VerificationIssue(
                    placeholder=ctx.key,
                    issue_type="wrong_case_in_label",
                    detail=f"Label-context value appears declined away from nominative: {ctx.original!r} -> {ctx.corrected!r}",
                )
    return None


def render_preview(template_text: str, placeholders: dict[str, str]) -> str:
    rendered = template_text
    for key, value in placeholders.items():
        rendered = rendered.replace(f"[{key}]", value)
    return rendered


def _loose_phrase_match(a: str, b: str) -> bool:
    a_words = a.split()
    b_words = b.split()
    if len(a_words) != len(b_words):
        return False
    matches = 0
    for wa, wb in zip(a_words, b_words):
        stem_a = wa[:-2] if len(wa) > 4 else wa
        stem_b = wb[:-2] if len(wb) > 4 else wb
        if stem_a == stem_b:
            matches += 1
    return matches / len(a_words) >= 0.8 if a_words else False


def check_duplication_in_rendered_text(template_text: str, all_corrected: dict[str, str]) -> list[VerificationIssue]:
    rendered = render_preview(template_text, all_corrected)
    cleaned = re.sub(r"[.,;:!?()«»\"]", " ", rendered)
    words = [w for w in cleaned.split() if w]
    issues: list[VerificationIssue] = []
    seen: set[str] = set()
    for window in (3, 4, 5):
        for i in range(len(words) - window * 2 + 1):
            phrase_a = " ".join(words[i:i + window]).lower()
            phrase_b = " ".join(words[i + window:i + window * 2]).lower()
            if _loose_phrase_match(phrase_a, phrase_b):
                detail = f"Adjacent repeated phrase detected: {phrase_a!r} / {phrase_b!r}"
                if detail in seen:
                    continue
                seen.add(detail)
                issues.append(VerificationIssue("<rendered_text>", "duplication", detail))
    return issues


def run_deterministic_verification(
    template_text: str,
    contexts: list[PlaceholderContext],
) -> dict[str, Any]:
    issues: list[VerificationIssue] = []
    for ctx in contexts:
        for issue in (check_fabrication(ctx), check_label_case_drift(ctx)):
            if issue:
                issues.append(issue)

    all_corrected = {ctx.key: ctx.corrected for ctx in contexts}
    issues.extend(check_duplication_in_rendered_text(template_text, all_corrected))
    return {
        "deterministic_issues": [issue.__dict__ for issue in issues],
        "needs_review": bool(issues),
    }


if __name__ == "__main__":
    ctx = PlaceholderContext(
        key="РеквизитыРуководительФИО",
        original="Есжанова Зарина Серикалиевна",
        corrected="Есжанова З.С.",
        context_type="label",
    )
    print("Fabrication check:", check_fabrication(ctx))

    ctx2 = PlaceholderContext(
        key="РеквизитыРуководительФИО",
        original="Есжанова Зарина Серикалиевна",
        corrected="Есжановой Зариной Серикалиевной",
        context_type="label",
    )
    print("Label case drift check:", check_label_case_drift(ctx2))

    dup_issues = check_duplication_in_rendered_text(
        "...главному менеджеру [Должность] [Подразделение]...",
        {
            "Должность": "департамента кадровой политики",
            "Подразделение": "департамента кадровой политики",
        },
    )
    print("Duplication check:", dup_issues)
