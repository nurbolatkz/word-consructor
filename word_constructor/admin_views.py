from __future__ import annotations

import threading
import uuid
from dataclasses import asdict, is_dataclass
from typing import Any

from flask import Blueprint, jsonify, render_template, request

from .admin_storage import (
    get_rule_candidate,
    insert_approved_rule_log,
    insert_review_item,
    insert_rule_candidates,
    load_review_items,
    load_rule_candidates,
    save_review_items,
    save_rule_candidates,
    update_review_item_status,
    update_rule_candidate_status,
    utc_now_iso,
)

admin_reviews = Blueprint(
    "admin_reviews",
    __name__,
    template_folder="templates",
)


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "asdict"):
        return value.asdict()
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {}


def build_review_item_from_check(
    document_name: str,
    log_key: str,
    checker_result: dict[str, Any] | Any,
    corrections: list[dict[str, Any]],
    rendered_preview: str,
    status: str = "pending",
) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "document_name": str(document_name or ""),
        "log_key": str(log_key or ""),
        "timestamp": utc_now_iso(),
        "checker_result": _as_dict(checker_result),
        "corrections": corrections or [],
        "rendered_preview": str(rendered_preview or ""),
        "status": status,
        "decided_at": None,
    }


def build_rule_candidate_item(
    candidate: dict[str, Any],
    claude_recommendation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": str(candidate.get("id") or uuid.uuid4()),
        "candidate_type": str(candidate.get("candidate_type") or ""),
        "pattern_summary": str(candidate.get("pattern_summary") or ""),
        "occurrence_count": int(candidate.get("occurrence_count") or 0),
        "example_contexts": list(candidate.get("example_contexts") or []),
        "created_at": str(candidate.get("created_at") or utc_now_iso()),
        "claude_recommendation": claude_recommendation,
        "status": str(candidate.get("status") or "pending"),
        "decided_at": candidate.get("decided_at"),
    }


def append_review_item(item: dict[str, Any]) -> dict[str, Any]:
    return insert_review_item(item)


def apply_approved_rule_candidate(candidate_id: str) -> bool:
    candidate = get_rule_candidate(candidate_id)
    if not candidate:
        return False
    insert_approved_rule_log(candidate_id, candidate)
    return True




def _persist_background_review_log(review_payload: dict[str, Any]) -> dict[str, Any]:
    review_summary = (review_payload.get("claude_result") or {}).get("review_summary") or {}
    checker_result = {
        "type": "gpt_claude_review",
        "original_params": review_payload.get("original_params") or {},
        "gpt_response": review_payload.get("gpt_response") or {},
        "claude_result": review_payload.get("claude_result") or {},
        "review_summary": review_summary,
    }
    status = str(review_payload.get("status") or "pending")
    item = build_review_item_from_check(
        document_name=str(review_payload.get("document_name") or ""),
        log_key=str(review_payload.get("log_key") or ""),
        checker_result=checker_result,
        corrections=list(review_payload.get("corrections") or []),
        rendered_preview=str(review_payload.get("rendered_preview") or ""),
        status=status,
    )
    return insert_review_item(item)


def enqueue_background_review_log(
    original_params: dict[str, Any],
    gpt_response: dict[str, Any],
    claude_result: dict[str, Any],
    document: str,
    *,
    document_name: str = "",
    log_key: str = "",
    status: str = "pending",
) -> threading.Thread:
    review_payload = {
        "document_name": document_name,
        "log_key": log_key,
        "original_params": original_params,
        "gpt_response": gpt_response,
        "claude_result": claude_result,
        "rendered_preview": document,
        "corrections": (claude_result.get("review_summary") or {}).get("changes_from_gpt", []),
        "status": status,
    }
    worker = threading.Thread(target=_persist_background_review_log, args=(review_payload,), daemon=True)
    worker.start()
    return worker


def _status_from_request() -> str:
    payload = request.get_json(silent=True) if request.is_json else None
    if isinstance(payload, dict):
        return str(payload.get("status") or "").strip()
    return str(request.form.get("status") or "").strip()


@admin_reviews.get("/review")
def review_queue_page():
    from flask import request as _req
    show_all = _req.args.get("show_all") == "1"
    all_items = load_review_items()
    items = all_items if show_all else [i for i in all_items if i.get("status") == "pending"]
    return render_template("review_queue.html", items=items, show_all=show_all, total_count=len(all_items))


@admin_reviews.post("/review/<item_id>/decide")
def decide_review_item(item_id: str):
    status = _status_from_request()
    if status not in {"approved", "rejected", "pending"}:
        return jsonify({"error": "invalid status"}), 400
    if not update_review_item_status(item_id, status):
        return jsonify({"error": "review item not found"}), 404
    return jsonify({"status": "ok"})


@admin_reviews.get("/rule-candidates")
def rule_candidates_page():
    return render_template("rule_candidates.html", candidates=load_rule_candidates())


@admin_reviews.post("/rule-candidates/<candidate_id>/decide")
def decide_rule_candidate(candidate_id: str):
    status = _status_from_request()
    if status not in {"approved", "rejected", "pending"}:
        return jsonify({"error": "invalid status"}), 400
    if status == "approved" and not apply_approved_rule_candidate(candidate_id):
        return jsonify({"error": "rule candidate not found"}), 404
    if not update_rule_candidate_status(candidate_id, status):
        return jsonify({"error": "rule candidate not found"}), 404
    return jsonify({"status": "ok"})


@admin_reviews.post("/run-analysis")
def run_analysis_now():
    """Manually trigger one pattern-analysis pass. Returns the pass report as JSON."""
    try:
        from word_constructor.ai_correction.pattern_analyzer import run_analysis_pass
        report = run_analysis_pass()
        return jsonify({"status": "ok", "report": report})
    except Exception as exc:
        return jsonify({"status": "error", "detail": str(exc)}), 500


__all__ = [
    "admin_reviews",
    "append_review_item",
    "apply_approved_rule_candidate",
    "build_review_item_from_check",
    "build_rule_candidate_item",
    "insert_review_item",
    "enqueue_background_review_log",
    "insert_rule_candidates",
    "load_review_items",
    "load_rule_candidates",
    "save_review_items",
    "save_rule_candidates",
    "update_review_item_status",
    "update_rule_candidate_status",
]
