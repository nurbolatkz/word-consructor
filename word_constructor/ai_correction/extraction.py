from __future__ import annotations

import re
from typing import Any

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from .types import Occurrence, SanityCheck, TextUnit
from .rules import RulesConfig, department_rule, is_department_placeholder

PLACEHOLDER_RE = re.compile(r"\{\{([^{}\n\r]{1,120})\}\}|\[([^\[\]\n\r]{1,120})\]")
_CONTEXT_CHARS_DEFAULT = 240


def match_key(match: re.Match) -> str:
    return (match.group(1) or match.group(2)).strip()


def para_full_text(para) -> str:
    return "".join(run.text for run in para.runs)


def cell_text(cell) -> str:
    parts: list[str] = []
    for para in cell.paragraphs:
        text = para_full_text(para)
        if text:
            parts.append(text)
    return "\n".join(parts)


def iter_text_units(doc: Document, include_headers_footers: bool = False) -> list[TextUnit]:
    units: list[TextUnit] = []
    paragraph_idx = 0
    table_idx = 0
    for child in doc.element.body:
        tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
        if tag == "p":
            para = Paragraph(child, doc)
            units.append(TextUnit("body_paragraph", f"paragraph[{paragraph_idx}]", para_full_text(para)))
            paragraph_idx += 1
        elif tag == "tbl":
            table = Table(child, doc)
            for row_idx, row in enumerate(table.rows):
                row_cell_texts = tuple(cell_text(row_cell) for row_cell in row.cells)
                for cell_idx, _cell in enumerate(row.cells):
                    units.append(TextUnit(
                        "table_cell",
                        f"table[{table_idx}].row[{row_idx}].cell[{cell_idx}]",
                        row_cell_texts[cell_idx],
                        table_index=table_idx,
                        row_index=row_idx,
                        cell_index=cell_idx,
                        row_cell_texts=row_cell_texts,
                    ))
            table_idx += 1

    if not include_headers_footers:
        return units

    for section_idx, section in enumerate(doc.sections):
        for source_type, part in (("header", section.header), ("footer", section.footer)):
            paragraphs = [para_full_text(para) for para in part.paragraphs if para_full_text(para).strip()]
            for table in part.tables:
                for row in table.rows:
                    for cell in row.cells:
                        text = cell_text(cell)
                        if text.strip():
                            paragraphs.append(text)
            text = "\n".join(paragraphs)
            if text.strip():
                units.append(TextUnit(source_type, f"{source_type}[section={section_idx}]", text, ai_excluded=True))
    return units


def document_plain_text(doc: Document) -> str:
    blocks: list[str] = []
    for unit in iter_text_units(doc):
        if unit.text.strip():
            blocks.append(f"[{unit.source_type} {unit.source_path}]\n{unit.text}")
    return "\n".join(blocks)


def document_placeholder_scan_text(doc: Document) -> str:
    return "\n".join(unit.text for unit in iter_text_units(doc) if unit.text.strip())


def context_snippet(text: str, match: re.Match, window: int = _CONTEXT_CHARS_DEFAULT) -> str:
    start = max(0, match.start() - window)
    end = min(len(text), match.end() + window)
    snippet = re.sub(r"\s+", " ", text[start:end]).strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet += "..."
    return snippet


_SIGNATURE_TITLE_RE = re.compile(
    r"\b(?:член[а-я]*|правлени[яею]|председател[яьюе]?|заместител[яьюе]?|директор[а-я]*|"
    r"руководител[яьюе]?|начальник[а-я]*|исполнительн[а-я]+|генеральн[а-я]+)\b",
    re.IGNORECASE,
)
_INITIAL_SURNAME_RE = re.compile(r"^[А-ЯЁA-Z]\.?* [А-ЯЁA-Z][А-ЯЁа-яёA-Za-z\-]+$".replace("\u0001* ", r"\s*"))
_FULL_NAME_RE = re.compile(r"^[А-ЯЁ][А-ЯЁа-яё\-]+(?:\s+[А-ЯЁ][А-ЯЁа-яё\-]+){1,3}$")
_VERB_HINT_RE = re.compile(r"\b(?:прошу|предоставить|назначить|уволить|перевести|согласовать|утвердить|является|составил|подписал|обязать|направить|принять)\b", re.IGNORECASE)
_SIGNATURE_KEY_RE = re.compile(r"(?:подпис|соглас|утверд|руковод|директор|председател|заместител|sign|signer)", re.IGNORECASE)
_SIGNATURE_TITLE_KEY_RE = re.compile(r"(?:должност|позици|руковод|директор|председател|заместител|title|position)", re.IGNORECASE)


def is_sole_placeholder(full_text: str) -> re.Match | None:
    stripped = full_text.strip()
    if not stripped:
        return None
    matches = list(PLACEHOLDER_RE.finditer(stripped))
    if len(matches) == 1 and matches[0].group(0) == stripped:
        return matches[0]
    return None


def looks_like_signature_name_or_label(value: str) -> bool:
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    return bool(cleaned and (_INITIAL_SURNAME_RE.match(cleaned) or _FULL_NAME_RE.match(cleaned)))


def looks_like_signature_title(value: str) -> bool:
    cleaned = re.sub(r"\s+", " ", value or "").strip()
    return bool(cleaned and _SIGNATURE_TITLE_RE.search(cleaned) and not looks_like_signature_name_or_label(cleaned))


def is_signature_or_approval_table_cell(unit: TextUnit, key: str, value: str) -> bool:
    if unit.source_type != "table_cell":
        return False
    text = re.sub(r"\s+", " ", unit.text or "").strip()
    row_texts = [re.sub(r"\s+", " ", item or "").strip() for item in unit.row_cell_texts]
    row_joined = " | ".join(row_texts)
    if _VERB_HINT_RE.search(text):
        return False
    placeholder_only_or_short = bool(is_sole_placeholder(text)) or len(text) <= 120
    row_has_signature_title = bool(_SIGNATURE_TITLE_RE.search(row_joined) or _SIGNATURE_TITLE_RE.search(value) or _SIGNATURE_TITLE_KEY_RE.search(row_joined))
    key_or_value_is_signatory = bool(_SIGNATURE_KEY_RE.search(key)) or looks_like_signature_name_or_label(value)
    return placeholder_only_or_short and row_has_signature_title and key_or_value_is_signatory


def raw_placeholder_matches_from_doc(doc: Document, slot_values: dict[str, Any]) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    wanted = set(slot_values)
    for unit in iter_text_units(doc):
        if not unit.text:
            continue
        for match in PLACEHOLDER_RE.finditer(unit.text):
            key = match_key(match)
            if key not in wanted:
                continue
            matches.append({
                "placeholder": key,
                "source_type": unit.source_type,
                "source_path": unit.source_path,
                "text_repr": repr(unit.text),
                "context_text": context_snippet(unit.text, match),
            })
    return matches


def _mark_adjacent_occurrences(occurrences: list[Occurrence]) -> None:
    by_source: dict[str, list[Occurrence]] = {}
    for item in occurrences:
        by_source.setdefault(f"{item.source_type}:{item.source_path}", []).append(item)
    for items in by_source.values():
        for idx, item in enumerate(items):
            adjacent: list[str] = []
            if idx > 0:
                adjacent.append(items[idx - 1].id)
            if idx + 1 < len(items):
                adjacent.append(items[idx + 1].id)
            item.adjacent_occurrence_ids = adjacent


def extract_placeholder_occurrences(doc: Document, slot_values: dict[str, str], rules: RulesConfig | None = None) -> list[dict[str, Any]]:
    occurrences: list[Occurrence] = []
    if not slot_values:
        return []
    wanted = set(slot_values)
    counts: dict[str, int] = {}
    dept_rule = department_rule(rules)
    for unit in iter_text_units(doc):
        if not unit.text:
            continue
        for match in PLACEHOLDER_RE.finditer(unit.text):
            key = match_key(match)
            if key not in wanted:
                continue
            occurrence_index = counts.get(key, 0)
            counts[key] = occurrence_index + 1
            value = str(slot_values[key])
            context = context_snippet(unit.text, match)
            ai_excluded = is_signature_or_approval_table_cell(unit, key, value)
            signature_title = ai_excluded and looks_like_signature_title(value)
            fixed_department = is_department_placeholder(key, rules)
            occurrences.append(Occurrence(
                id=f"{key}#{occurrence_index}",
                key=key,
                placeholder=key,
                occurrence=occurrence_index + 1,
                occurrence_index=occurrence_index,
                value=value,
                original_value=value,
                context=context,
                context_text=context,
                context_with_value=context.replace(match.group(0), value, 1),
                source_type=unit.source_type,
                source_path=unit.source_path,
                literal_placeholder=match.group(0),
                ai_excluded=ai_excluded,
                ai_exclusion_reason="signature_or_approval_table" if ai_excluded else "",
                signature_title_normalize=signature_title,
                deterministic_behavior=str(dept_rule.get("behavior") or "") if fixed_department else "",
                expected_case=str(dept_rule.get("default_case") or "") if fixed_department else "",
                fixed_form=fixed_department,
                never_merge_with_adjacent_occurrence=bool(dept_rule.get("never_merge_with_adjacent_occurrence")) if fixed_department else False,
                preserve_internal_abbreviations=bool(dept_rule.get("preserve_internal_abbreviations")) if fixed_department else False,
            ))
    _mark_adjacent_occurrences(occurrences)
    return [item.to_dict() for item in occurrences]


def extract_header_footer_placeholder_occurrences(doc: Document, slot_values: dict[str, str]) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    wanted = set(slot_values)
    for unit in iter_text_units(doc, include_headers_footers=True):
        if unit.source_type not in {"header", "footer"}:
            continue
        for match in PLACEHOLDER_RE.finditer(unit.text):
            key = match_key(match)
            if key in wanted:
                occurrences.append({
                    "key": key,
                    "placeholder": key,
                    "source_type": unit.source_type,
                    "source_path": unit.source_path,
                    "context_text": context_snippet(unit.text, match),
                    "ai_excluded": True,
                    "ai_exclusion_reason": "header_footer",
                })
    return occurrences


def sanity_check_occurrence_counts(doc: Document, slot_values: dict[str, Any], occurrences: list[dict[str, Any]]) -> SanityCheck:
    wanted = set(slot_values)
    scan_text = document_placeholder_scan_text(doc)
    full_text_raw_count = sum(1 for match in PLACEHOLDER_RE.finditer(scan_text) if match_key(match) in wanted)
    raw_matches = raw_placeholder_matches_from_doc(doc, slot_values)
    raw_count = len(raw_matches)
    occurrence_count = len(occurrences)
    return SanityCheck(full_text_raw_count == occurrence_count and raw_count == occurrence_count, full_text_raw_count, raw_count, occurrence_count, raw_matches)


def extract_placeholder_contexts(doc: Document, slot_values: dict[str, str], max_snippets: int = 5) -> dict[str, list[str]]:
    contexts: dict[str, list[str]] = {key: [] for key in slot_values}
    wanted = set(slot_values)
    for unit in iter_text_units(doc):
        for match in PLACEHOLDER_RE.finditer(unit.text or ""):
            key = match_key(match)
            if key not in wanted:
                continue
            snippets = contexts.setdefault(key, [])
            if len(snippets) >= max_snippets:
                continue
            snippet = context_snippet(unit.text, match)
            if snippet and snippet not in snippets:
                snippets.append(snippet)
    return contexts


# Public notebook API -------------------------------------------------------

def walk_document(doc) -> list[TextUnit]:
    """Walk body paragraphs, every table cell's paragraphs, headers, footers.
    Merge adjacent runs into single text per unit before returning.
    """
    return iter_text_units(doc, include_headers_footers=True)


def find_occurrences(text_units: list[TextUnit], placeholders: dict[str, str]) -> list[Occurrence]:
    """Regex-scan TextUnits for [PlaceholderName] matches present in placeholders."""
    occurrences: list[Occurrence] = []
    counts: dict[str, int] = {}
    wanted = set(placeholders)
    for unit in text_units:
        for match in PLACEHOLDER_RE.finditer(unit.text or ""):
            key = match_key(match)
            if key not in wanted:
                continue
            index = counts.get(key, 0)
            counts[key] = index + 1
            context = context_snippet(unit.text, match)
            occurrences.append(Occurrence(
                placeholder=key,
                occurrence_index=index,
                original_value=str(placeholders[key]),
                source_type=unit.source_type,
                source_path=unit.source_path,
                context_text=context,
                context=context,
                context_with_value=context.replace(match.group(0), str(placeholders[key]), 1),
                literal_placeholder=match.group(0),
            ))
    return occurrences


def sanity_check(text_units: list[TextUnit], occurrences: list[Occurrence]) -> tuple[bool, list[str]]:
    """Compare raw placeholder regex matches across all text_units against len(occurrences)."""
    raw_count = sum(1 for unit in text_units for _ in PLACEHOLDER_RE.finditer(unit.text or ""))
    occurrence_count = len(occurrences)
    if raw_count == occurrence_count:
        return True, []
    return False, [f"raw placeholder regex matches={raw_count}, occurrences={occurrence_count}"]
