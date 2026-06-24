"""Simplified AI correction pipeline — full doc text + all occurrences → AI → apply directly."""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .claude_checker_and_summarizer import claude_correct_and_review

from docx import Document

from . import log_store
from .extraction import document_full_text, extract_placeholder_occurrences
from .openai_client import request_ai_corrections, _known_pitfalls_for_contexts
from .rules import load_rules_config, rules_health
from .types import PipelineCorrectionResult
from .verifier import contexts_from_occurrences, render_preview

logger = logging.getLogger(__name__)




def correct_document_two_model(
    doc: Document,
    slot_values: dict[str, str],
    prompt_ai: str,
    log_key: str | None = None,
    call_log: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, Any]:
    occurrences = extract_placeholder_occurrences(doc, slot_values)
    full_text = document_full_text(doc)
    rules = load_rules_config()

    gpt_occurrence_corrections = request_ai_corrections(
        full_text=full_text,
        occurrences=occurrences,
        rules=rules,
        prompt_ai=prompt_ai,
        placeholders=slot_values,
        log_key=log_key,
        call_log=call_log,
        timeout_seconds=timeout_seconds,
        persist_review_item=False,
        apply_verification_fallbacks=False,
    )
    gpt_response: dict[str, str] = {}
    for item in occurrences:
        placeholder = str(item.get("key") or item.get("placeholder") or "").strip()
        if not placeholder:
            continue
        try:
            occ_idx = int(item.get("occurrence_index", 0))
        except (TypeError, ValueError):
            continue
        corrected = gpt_occurrence_corrections.get((placeholder, occ_idx))
        if corrected is not None:
            gpt_response[placeholder] = str(corrected)
    original_params = {"template": full_text, "placeholders": dict(slot_values)}
    verifier_contexts = contexts_from_occurrences(occurrences, gpt_response)
    known_pitfalls = _known_pitfalls_for_contexts(verifier_contexts)
    claude_result = claude_correct_and_review(original_params, gpt_response, known_pitfalls=known_pitfalls)
    final_values = claude_result["corrected_values"]

    rendered_preview = render_preview(full_text, final_values)
    review_payload = {
        "document_name": str((call_log or {}).get("document_name") or (call_log or {}).get("filename") or ""),
        "log_key": log_key or "",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "original_params": original_params,
        "gpt_response": gpt_response,
        "claude_result": claude_result,
        "rendered_preview": rendered_preview,
        "corrections": claude_result.get("review_summary", {}).get("changes_from_gpt", []),
    }

    if call_log is not None:
        call_log["gpt_response"] = gpt_response
        call_log["claude_result"] = claude_result
        call_log["rendered_preview"] = rendered_preview

    return {
        "final_values": final_values,
        "gpt_response": gpt_response,
        "claude_result": claude_result,
        "review_payload": review_payload,
        "occurrences": occurrences,
    }


def startup_health() -> dict[str, Any]:
    data = rules_health()
    data["openai_api_key_present"] = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    return data


def correct_slot_values(
    doc: Document,
    slot_values: dict[str, str],
    prompt_ai: str,
    decline_func: Callable | None = None,  # kept for API compatibility, no longer used
    raw_ai_values: dict[str, Any] | None = None,  # kept for API compatibility, no longer used
    log_key: str | None = None,
    call_log: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
) -> PipelineCorrectionResult:
    t_start = time.perf_counter()

    if not slot_values:
        return PipelineCorrectionResult(slot_values, {})

    # 1. Extract where every placeholder appears in the document
    occurrences = extract_placeholder_occurrences(doc, slot_values)

    # 2. Get full labeled document text (headers + body + footers + tables)
    full_text = document_full_text(doc)

    # 3. Load rules config so AI gets governing phrases + abbreviations as context
    rules = load_rules_config()

    # 4. Send everything to AI — AI handles morphology, case, abbreviations, dates, dedup
    ai_corrections: dict[tuple[str, int], str] = {}
    ai_status = "ok"
    t_ai = time.perf_counter()
    try:
        ai_corrections = request_ai_corrections(
            full_text=full_text,
            occurrences=occurrences,
            rules=rules,
            prompt_ai=prompt_ai,
            placeholders=raw_ai_values or slot_values,
            log_key=log_key,
            call_log=call_log,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        ai_status = "ai_failed"
        logger.exception("AI correction failed: log_key=%s error=%s", log_key, exc)
        if call_log is not None:
            call_log["error"] = str(exc)
    t_ai_ms = round((time.perf_counter() - t_ai) * 1000)

    # 5. Build occurrence_values: (key, occurrence 1-based) → corrected_value
    #    AI returns (placeholder, occurrence_index 0-based); map to 1-based occurrence number
    index_to_target: dict[tuple[str, int], tuple[str, int]] = {
        (str(o.get("key")), int(o.get("occurrence_index", 0))): (str(o.get("key")), int(o.get("occurrence") or 0))
        for o in occurrences
    }
    occurrence_values: dict[tuple[str, int], str] = {}
    for (placeholder, occ_idx), corrected in ai_corrections.items():
        target = index_to_target.get((placeholder, occ_idx))
        if target and corrected is not None:
            occurrence_values[target] = str(corrected)

    t_total_ms = round((time.perf_counter() - t_start) * 1000)

    # Token usage
    tokens: dict[str, int] = {}
    if call_log is not None:
        resp_body = (call_log.get("response") or {}).get("body") or {}
        if isinstance(resp_body, dict):
            usage = resp_body.get("usage") or {}
            tokens = {k: int(v) for k, v in usage.items() if isinstance(v, (int, float))}

    # Correction log
    correction_details = []
    for o in occurrences:
        key = str(o.get("key") or "")
        occ_num = int(o.get("occurrence") or 0)
        original = str(o.get("value") or "")
        final = occurrence_values.get((key, occ_num), original)
        correction_details.append({
            "placeholder": key,
            "occurrence_index": int(o.get("occurrence_index", 0)),
            "original": original,
            "final": final,
            "changed": final != original,
            "context": str(o.get("context") or "")[:120],
        })

    try:
        log_store.append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "log_key": log_key or "",
            "status": ai_status,
            "model": os.environ.get("OPENAI_PLACEHOLDER_MODEL", os.environ.get("OPENAI_MODEL", "gpt-4o-mini")),
            "slot_count": len(slot_values),
            "occurrence_count": len(occurrences),
            "changed_count": sum(1 for d in correction_details if d["changed"]),
            "timing_ms": {"ai_call": t_ai_ms, "total": t_total_ms},
            "tokens": tokens,
            "corrections": correction_details,
        })
    except Exception as exc:
        logger.warning("Failed to write correction log: %s", exc)

    return PipelineCorrectionResult(slot_values, occurrence_values, occurrences, {})
