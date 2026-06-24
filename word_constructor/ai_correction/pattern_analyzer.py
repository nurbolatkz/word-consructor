from __future__ import annotations

import sys

if __name__ == "__main__" and sys.path and sys.path[0].replace("\\", "/").endswith("/word_constructor/ai_correction"):
    sys.path.pop(0)

import json
import logging
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from word_constructor.admin_views import build_rule_candidate_item, insert_rule_candidates

try:
    from .claude_checker import claude_available, claude_summarize_review_queue
    from .governing import GOVERNING_RULES
    from .log_store import _log_path
    from .rag_store import RagStore, make_entry
except ImportError:  # pragma: no cover - direct script execution
    from word_constructor.ai_correction.claude_checker import claude_available, claude_summarize_review_queue
    from word_constructor.ai_correction.governing import GOVERNING_RULES
    from word_constructor.ai_correction.log_store import _log_path
    from word_constructor.ai_correction.rag_store import RagStore, make_entry


CORRECTIONS_LOG_PATH = os.environ.get("AI_CORRECTION_LOG_PATH", str(_log_path()))
STATE_PATH = os.environ.get("AI_ANALYZER_STATE_PATH", "/tmp/kazuni_word_constructor/analyzer_state.json")
REVIEW_QUEUE_PATH = os.environ.get(
    "AI_REVIEW_QUEUE_PATH",
    "/tmp/kazuni_word_constructor/rule_candidates_review_queue.jsonl",
)
MIN_OCCURRENCES_TO_FLAG = int(os.environ.get("AI_ANALYZER_MIN_OCCURRENCES", "3"))
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RuleCandidate:
    candidate_type: str
    pattern_summary: str
    occurrence_count: int
    example_contexts: list[str]
    suggested_action: str
    created_at: str


def load_state(path: str = STATE_PATH) -> dict[str, Any]:
    state_path = Path(path)
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"last_processed_ts": None}
    return {"last_processed_ts": None}


def save_state(state: dict[str, Any], path: str = STATE_PATH) -> None:
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def load_new_log_entries(state: dict[str, Any], log_path: str = CORRECTIONS_LOG_PATH) -> list[dict[str, Any]]:
    path = Path(log_path)
    if not path.exists():
        return []

    last_ts = state.get("last_processed_ts")
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if last_ts is None or entry.get("ts", "") > last_ts:
            entries.append(entry)
    return entries


def extract_governing_phrase(context: str, placeholder_marker: str) -> str:
    marker = f"[{placeholder_marker}]"
    idx = (context or "").find(marker)
    if idx == -1:
        return ""
    before = re.sub(r"\[[^\]]+\]", " ", context[:idx]).strip()
    words = before.split()
    return " ".join(words[-3:]) if words else ""


def correction_governing_phrase(correction: dict[str, Any]) -> str:
    text_before = (correction.get("text_before") or "").strip()
    if text_before:
        return " ".join(text_before.split()[-3:])
    return extract_governing_phrase(correction.get("context", ""), correction.get("placeholder", ""))


def known_governing_phrases_from_rules() -> set[str]:
    phrases = set()
    for rule in GOVERNING_RULES:
        note = rule.note.lower()
        for quoted in _quoted_fragments(note):
            phrases.add(quoted.strip())
    phrases.update({"принять", "на должность", "на", "сектора", "департамента", "заявление от", "заявление"})
    return {phrase for phrase in phrases if phrase}


def _quoted_fragments(text: str) -> list[str]:
    fragments = []
    for quote in ('"', "'"):
        parts = text.split(quote)
        fragments.extend(parts[i] for i in range(1, len(parts), 2))
    return fragments


def cluster_unknown_governing_phrases(
    entries: list[dict[str, Any]],
    known_phrases: set[str],
    min_occurrences: int = MIN_OCCURRENCES_TO_FLAG,
) -> list[RuleCandidate]:
    phrase_counter: Counter[str] = Counter()
    phrase_examples: dict[str, list[str]] = defaultdict(list)
    normalized_known = {phrase.lower().strip() for phrase in known_phrases}

    for entry in entries:
        for correction in entry.get("corrections", []):
            phrase = correction_governing_phrase(correction).lower().strip()
            if not phrase or phrase in normalized_known:
                continue
            phrase_counter[phrase] += 1
            if len(phrase_examples[phrase]) < 3:
                phrase_examples[phrase].append(correction.get("context") or correction.get("text_before") or "")

    candidates = []
    for phrase, count in phrase_counter.items():
        if count >= min_occurrences:
            candidates.append(
                RuleCandidate(
                    candidate_type="governing_phrase",
                    pattern_summary=f'Governing phrase "{phrase}" not in GOVERNING_RULES table',
                    occurrence_count=count,
                    example_contexts=phrase_examples[phrase],
                    suggested_action=(
                        f'Review whether "{phrase}" reliably governs a specific case. '
                        "If yes, add a deterministic GoverningRule entry."
                    ),
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )
    return candidates


def cluster_case_drift_incidents(entries: list[dict[str, Any]]) -> list[RuleCandidate]:
    value_variants: dict[tuple[str, str], set[str]] = defaultdict(set)
    value_contexts: dict[tuple[str, str], str] = {}

    for entry in entries:
        for correction in entry.get("corrections", []):
            key = (correction.get("placeholder", ""), correction.get("original", ""))
            value_variants[key].add(correction.get("final", ""))
            value_contexts.setdefault(key, correction.get("context", ""))

    candidates = []
    for (placeholder, original), variants in value_variants.items():
        if len(variants) > 1:
            candidates.append(
                RuleCandidate(
                    candidate_type="case_drift",
                    pattern_summary=(
                        f'Placeholder "{placeholder}" with original "{original}" produced '
                        f"{len(variants)} different corrected values"
                    ),
                    occurrence_count=len(variants),
                    example_contexts=[value_contexts.get((placeholder, original), "")] + sorted(variants),
                    suggested_action=(
                        "Review whether a deterministic case rule should be added for this context; "
                        "do not feed unstable variants into RAG."
                    ),
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )
    return candidates


def cluster_duplication_incidents(entries: list[dict[str, Any]]) -> list[RuleCandidate]:
    detail_counter: Counter[str] = Counter()
    detail_examples: dict[str, list[str]] = defaultdict(list)

    for entry in entries:
        candidate_issues = []
        verification = entry.get("verification") or {}
        candidate_issues.extend(verification.get("deterministic_issues") or [])
        candidate_issues.extend(entry.get("deterministic_issues") or [])

        for correction in entry.get("corrections", []):
            if correction.get("issue_type") == "duplication":
                candidate_issues.append(correction)

        for issue in candidate_issues:
            if issue.get("issue_type") != "duplication":
                continue
            detail = (issue.get("detail") or issue.get("pattern") or "duplication").strip()
            detail_counter[detail] += 1
            if len(detail_examples[detail]) < 3:
                detail_examples[detail].append(entry.get("rendered_text") or issue.get("context") or detail)

    candidates = []
    for detail, count in detail_counter.items():
        if count >= MIN_OCCURRENCES_TO_FLAG:
            candidates.append(
                RuleCandidate(
                    candidate_type="duplication_pattern",
                    pattern_summary=f"Recurring duplication incident: {detail}",
                    occurrence_count=count,
                    example_contexts=detail_examples[detail],
                    suggested_action=(
                        "Review whether this duplicate pair should become a deterministic "
                        "redundant-placeholder policy or a known RAG pitfall."
                    ),
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
            )
    return candidates


def unstable_pairs_from_candidates(candidates: list[RuleCandidate]) -> set[tuple[str, str]]:
    pairs = set()
    prefix = 'Placeholder "'
    marker = '" with original "'
    for candidate in candidates:
        if candidate.candidate_type != "case_drift" or not candidate.pattern_summary.startswith(prefix):
            continue
        rest = candidate.pattern_summary[len(prefix):]
        if marker not in rest:
            continue
        placeholder, tail = rest.split(marker, 1)
        original = tail.split('"', 1)[0]
        pairs.add((placeholder, original))
    return pairs


def write_review_queue(candidates: list[RuleCandidate], path: str = REVIEW_QUEUE_PATH) -> None:
    if not candidates:
        return
    queue_path = Path(path)
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with queue_path.open("a", encoding="utf-8") as fh:
        for candidate in candidates:
            fh.write(json.dumps(asdict(candidate), ensure_ascii=False) + "\n")


def feed_confirmed_corrections_to_rag(
    entries: list[dict[str, Any]],
    rag_store: RagStore,
    unstable_pairs: set[tuple[str, str]] | None = None,
) -> int:
    unstable_pairs = unstable_pairs or set()
    added = 0
    for entry in entries:
        for correction in entry.get("corrections", []):
            if not correction.get("changed"):
                continue
            placeholder = correction.get("placeholder", "")
            original = correction.get("original", "")
            if (placeholder, original) in unstable_pairs:
                continue
            context = correction.get("context", "")
            rag_store.add_entry(
                make_entry(
                    kind="good_example",
                    placeholder_role=correction.get("role", "unknown"),
                    context_type=correction.get("context_type") or ("sentence" if context.strip() else "label"),
                    governing_phrase=correction_governing_phrase(correction),
                    original_value=original,
                    corrected_value=correction.get("final", ""),
                    case=correction.get("detected_case", "unknown"),
                    note=f"Auto-ingested from production log, source={correction.get('source', 'unknown')}",
                    source="stage3_auto_ingest",
                )
            )
            added += 1
    return added


def draft_review_queue_recommendations(candidates: list[RuleCandidate]) -> dict[str, Any]:
    if not candidates:
        return {"recommendations": []}
    if not claude_available():
        return {
            "recommendations": [],
            "skipped": "anthropic package or ANTHROPIC_API_KEY is not configured",
        }
    return claude_summarize_review_queue([asdict(candidate) for candidate in candidates])


def _recommendations_by_summary(candidates: list[RuleCandidate]) -> dict[str, dict[str, Any]]:
    if not candidates:
        return {}
    try:
        summary = draft_review_queue_recommendations(candidates)
    except Exception as exc:
        logger.warning("Claude review-queue summarization failed: %s", exc)
        return {}
    recommendations = summary.get("recommendations") if isinstance(summary, dict) else None
    if not isinstance(recommendations, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for item in recommendations:
        if not isinstance(item, dict):
            continue
        pattern_summary = str(item.get("pattern_summary") or "")
        if pattern_summary:
            out[pattern_summary] = item
    return out


def persist_rule_candidates_with_recommendations(candidates: list[RuleCandidate]) -> int:
    if not candidates:
        return 0
    recommendations = _recommendations_by_summary(candidates)
    items = [
        build_rule_candidate_item(
            asdict(candidate),
            claude_recommendation=recommendations.get(candidate.pattern_summary),
        )
        for candidate in candidates
    ]
    try:
        insert_rule_candidates(items)
    except Exception as exc:
        logger.warning("Failed to persist rule candidates: %s", exc)
        return 0
    return len(items)


def run_analysis_pass(
    known_governing_phrases: set[str] | None = None,
    log_path: str = CORRECTIONS_LOG_PATH,
    state_path: str = STATE_PATH,
    review_queue_path: str = REVIEW_QUEUE_PATH,
    rag_store: RagStore | None = None,
) -> dict[str, Any]:
    known_governing_phrases = known_governing_phrases or known_governing_phrases_from_rules()
    state = load_state(state_path)
    new_entries = load_new_log_entries(state, log_path)
    if not new_entries:
        return {"processed": 0, "candidates": 0, "persisted_candidates": 0, "rag_added": 0}

    governing_candidates = cluster_unknown_governing_phrases(new_entries, known_governing_phrases)
    drift_candidates = cluster_case_drift_incidents(new_entries)
    duplication_candidates = cluster_duplication_incidents(new_entries)
    all_candidates = governing_candidates + drift_candidates + duplication_candidates
    write_review_queue(all_candidates, review_queue_path)
    persisted_candidates = persist_rule_candidates_with_recommendations(all_candidates)

    store = rag_store or RagStore()
    rag_added = feed_confirmed_corrections_to_rag(
        new_entries,
        store,
        unstable_pairs=unstable_pairs_from_candidates(drift_candidates),
    )

    latest_ts = max(entry.get("ts", "") for entry in new_entries)
    save_state({"last_processed_ts": latest_ts}, state_path)
    return {"processed": len(new_entries), "candidates": len(all_candidates), "persisted_candidates": persisted_candidates, "rag_added": rag_added}


def run_as_service(interval_seconds: int = 3600) -> None:
    import time

    while True:
        try:
            report = run_analysis_pass()
            print(json.dumps(report, ensure_ascii=False))
        except Exception as exc:
            print(f"Analysis pass failed: {exc}")
        time.sleep(interval_seconds)


if __name__ == "__main__":
    demo_entries = [
        {
            "ts": "2026-06-23T07:42:49+00:00",
            "corrections": [
                {
                    "placeholder": "РеквизитыСотрудникДолжностьНаименование",
                    "original": "кассир-повар",
                    "final": "кассира-повара",
                    "changed": True,
                    "source": "ai",
                    "context": "Принять [Х] на [РеквизитыСотрудникДолжностьНаименование] [Y]",
                }
            ],
        },
        {
            "ts": "2026-06-23T08:10:31+00:00",
            "corrections": [
                {
                    "placeholder": "РеквизитыСотрудникДолжностьНаименование",
                    "original": "кассир-повар",
                    "final": "кассиром-поваром",
                    "changed": True,
                    "source": "ai",
                    "context": "Принять [Х] на [РеквизитыСотрудникДолжностьНаименование] [Y]",
                }
            ],
        },
    ]
    print("=== Demo: case drift ===")
    for candidate in cluster_case_drift_incidents(demo_entries):
        print(f"[{candidate.candidate_type}] {candidate.pattern_summary}")

    print("\n=== Demo: unknown governing phrases ===")
    for candidate in cluster_unknown_governing_phrases(demo_entries * 3, {"на должность"}):
        print(f"[{candidate.candidate_type}] {candidate.pattern_summary}")
