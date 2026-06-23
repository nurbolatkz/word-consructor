from __future__ import annotations

import logging
import os
from typing import Any, Callable

from docx import Document

from .deterministic import apply_deterministic_case_hints, should_preserve_ai_corrected_value
from .extraction import (
    document_plain_text,
    extract_header_footer_placeholder_occurrences,
    extract_placeholder_contexts,
    extract_placeholder_occurrences,
    sanity_check_occurrence_counts,
)
from .openai_client import request_ai_placeholder_corrections
from .rules import load_rules_config, rules_health
from .types import PipelineCorrectionResult

logger = logging.getLogger(__name__)


def startup_health() -> dict[str, Any]:
    data = rules_health()
    data["openai_api_key_present"] = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    return data


def correct_slot_values(
    doc: Document,
    slot_values: dict[str, str],
    prompt_ai: str,
    decline_func: Callable[[str, str], str],
    raw_ai_values: dict[str, Any] | None = None,
    log_key: str | None = None,
    call_log: dict[str, Any] | None = None,
    timeout_seconds: float = 8.0,
) -> PipelineCorrectionResult:
    if not slot_values:
        return PipelineCorrectionResult(slot_values, {})

    rules = load_rules_config()
    occurrences = extract_placeholder_occurrences(doc, slot_values, rules)
    sanity = sanity_check_occurrence_counts(doc, slot_values, occurrences)
    sanity_payload = {
        "use_ai_log_key": log_key,
        "full_text_raw_match_count": sanity.full_text_raw_match_count,
        "raw_match_count": sanity.raw_match_count,
        "occurrence_count": sanity.occurrence_count,
        "raw_matches": sanity.raw_matches,
        "occurrences": [
            {
                "placeholder": item.get("placeholder", item.get("key")),
                "occurrence_index": item.get("occurrence_index"),
                "source_type": item.get("source_type"),
                "source_path": item.get("source_path"),
                "ai_excluded": item.get("ai_excluded"),
                "ai_exclusion_reason": item.get("ai_exclusion_reason"),
                "fixed_form": item.get("fixed_form"),
                "context_text": item.get("context_text"),
            }
            for item in occurrences
        ],
    }
    if call_log is not None:
        call_log["occurrence_sanity_check"] = sanity_payload
        call_log["rules"] = rules_health()
    if not sanity.ok:
        logger.error(
            "UseAI placeholder occurrence mismatch before OpenAI: found %s raw [Placeholder] regex matches in full extracted document text, found %s source-aware raw matches, but added %s occurrences: %s",
            sanity.full_text_raw_match_count,
            sanity.raw_match_count,
            sanity.occurrence_count,
            sanity_payload,
        )
        if call_log is not None:
            call_log["error"] = "placeholder occurrence mismatch; AI correction skipped"
        return PipelineCorrectionResult(slot_values, {}, occurrences, sanity_payload, "placeholder_occurrence_mismatch")

    logger.debug(
        "UseAI placeholder occurrence count check passed: found %s raw [Placeholder] regex matches and added %s occurrences: %s",
        sanity.full_text_raw_match_count,
        sanity.occurrence_count,
        sanity_payload,
    )

    excluded = extract_header_footer_placeholder_occurrences(doc, slot_values)
    if excluded:
        logger.info("UseAI header/footer placeholders excluded from AI correction: use_ai_log_key=%s occurrences=%s", log_key, excluded)
    ai_excluded = [item for item in occurrences if item.get("ai_excluded")]
    if ai_excluded:
        logger.info("UseAI table placeholders excluded from AI correction: use_ai_log_key=%s occurrences=%s", log_key, ai_excluded)

    contexts = extract_placeholder_contexts(doc, slot_values)
    full_document_text = document_plain_text(doc)
    ai_values = raw_ai_values or slot_values
    corrections: dict[str, str] = {}
    try:
        corrections = request_ai_placeholder_corrections(
            ai_values,
            contexts,
            prompt_ai,
            occurrences,
            full_document_text,
            log_key,
            call_log,
            timeout_seconds,
        )
    except Exception as exc:
        logger.exception("AI placeholder correction failed; deterministic safeguards only: use_ai_log_key=%s error=%s", log_key, exc)
        if call_log is not None:
            call_log["error"] = f"AI placeholder correction failed: {exc}"

    occurrence_values: dict[tuple[str, int], str] = {}
    occurrence_keys = {str(item.get("id")): (str(item.get("key")), int(item.get("occurrence"))) for item in occurrences}
    occurrence_by_id = {str(item.get("id")): item for item in occurrences}
    for correction_id, corrected in corrections.items():
        if correction_id not in occurrence_keys or not str(corrected).strip():
            continue
        item = occurrence_by_id.get(correction_id, {})
        key = str(item.get("key") or occurrence_keys[correction_id][0])
        original = str(item.get("value") or "")
        if should_preserve_ai_corrected_value(key, original, str(corrected), rules):
            occurrence_values[occurrence_keys[correction_id]] = original
        else:
            occurrence_values[occurrence_keys[correction_id]] = str(corrected)

    apply_deterministic_case_hints(occurrence_values, occurrences, decline_func, rules)
    return PipelineCorrectionResult(slot_values, occurrence_values, occurrences, sanity_payload)
