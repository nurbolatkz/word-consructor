from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from docx import Document

from . import log_store
from .deterministic import apply_deterministic_case_hints, should_preserve_ai_corrected_value
from .extraction import (
    document_full_text,
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
    t_total_start = time.perf_counter()

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
    full_document_text = document_full_text(doc)
    ai_values = raw_ai_values or slot_values
    corrections: dict[str, str] = {}
    ai_status = "ok"
    t_ai_start = time.perf_counter()
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
        ai_status = "ai_failed"
        logger.exception("AI placeholder correction failed; deterministic safeguards only: use_ai_log_key=%s error=%s", log_key, exc)
        if call_log is not None:
            call_log["error"] = f"AI placeholder correction failed: {exc}"
    t_ai_ms = round((time.perf_counter() - t_ai_start) * 1000)

    occurrence_values: dict[tuple[str, int], str] = {}
    occurrence_keys = {str(item.get("id")): (str(item.get("key")), int(item.get("occurrence"))) for item in occurrences}
    occurrence_by_id = {str(item.get("id")): item for item in occurrences}
    for correction_id, corrected in corrections.items():
        item = occurrence_by_id.get(correction_id, {})
        is_redundant = bool(item.get("redundant_in"))
        if correction_id not in occurrence_keys:
            continue
        if not str(corrected).strip() and not is_redundant:
            continue
        key = str(item.get("key") or occurrence_keys[correction_id][0])
        original = str(item.get("value") or "")
        if is_redundant and not str(corrected).strip():
            occurrence_values[occurrence_keys[correction_id]] = ""
        elif should_preserve_ai_corrected_value(key, original, str(corrected), rules):
            occurrence_values[occurrence_keys[correction_id]] = original
        else:
            occurrence_values[occurrence_keys[correction_id]] = str(corrected)

    # Snapshot AI-only values before deterministic overrides
    ai_occurrence_values = dict(occurrence_values)

    apply_deterministic_case_hints(occurrence_values, occurrences, decline_func, rules)

    t_total_ms = round((time.perf_counter() - t_total_start) * 1000)

    # Extract token usage if available
    tokens: dict[str, int] = {}
    if call_log is not None:
        response_body = (call_log.get("response") or {}).get("body") or {}
        if isinstance(response_body, dict):
            usage = response_body.get("usage") or {}
            if isinstance(usage, dict):
                tokens = {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}

    # Build per-occurrence correction details for pattern analysis
    ai_sent_ids = {
        str(item.get("id"))
        for item in occurrences
        if not item.get("ai_excluded") and not item.get("fixed_form")
    }
    dedup_count = sum(1 for item in occurrences if item.get("redundant_in"))
    correction_details = []
    for item in occurrences:
        oid = str(item.get("id"))
        key = str(item.get("key") or "")
        occ_idx = int(item.get("occurrence") or 0)
        original = str(item.get("value") or "")
        final = occurrence_values.get((key, occ_idx), original)
        ai_raw = ai_occurrence_values.get((key, occ_idx))
        if item.get("redundant_in"):
            source = "dedup"
        elif ai_raw is not None and ai_raw != original:
            source = "ai" if final == ai_raw else "deterministic"
        elif final != original:
            source = "deterministic"
        else:
            source = "unchanged"
        correction_details.append({
            "placeholder": key,
            "occurrence_index": occ_idx,
            "original": original,
            "ai_raw": ai_raw if ai_raw is not None else "",
            "final": final,
            "source": source,
            "changed": final != original,
            "context": str(item.get("context") or "")[:120],
        })

    model = os.environ.get("OPENAI_PLACEHOLDER_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    log_entry: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "log_key": log_key or "",
        "status": ai_status,
        "model": model,
        "slot_count": len(slot_values),
        "occurrence_count": len(occurrences),
        "ai_sent_count": len(ai_sent_ids),
        "dedup_count": dedup_count,
        "changed_count": sum(1 for d in correction_details if d["changed"]),
        "timing_ms": {"ai_call": t_ai_ms, "total": t_total_ms},
        "tokens": tokens,
        "corrections": correction_details,
    }
    try:
        log_store.append(log_entry)
    except Exception as exc:
        logger.warning("Failed to write correction log: %s", exc)

    return PipelineCorrectionResult(slot_values, occurrence_values, occurrences, sanity_payload)
