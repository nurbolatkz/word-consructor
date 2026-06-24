from __future__ import annotations

import json
import tempfile
import os
import unittest
from unittest.mock import patch
from pathlib import Path

from word_constructor.admin_storage import load_rule_candidates
from word_constructor.ai_correction.pattern_analyzer import (
    RuleCandidate,
    cluster_case_drift_incidents,
    cluster_duplication_incidents,
    cluster_unknown_governing_phrases,
    draft_review_queue_recommendations,
    run_analysis_pass,
)
from word_constructor.ai_correction.rag_store import RagStore


def _entry(ts: str, final: str) -> dict:
    return {
        "ts": ts,
        "corrections": [
            {
                "placeholder": "РеквизитыСотрудникДолжностьНаименование",
                "original": "кассир-повар",
                "final": final,
                "changed": True,
                "source": "ai",
                "context": "Принять [Х] на [РеквизитыСотрудникДолжностьНаименование] [Y]",
            }
        ],
    }


class PatternAnalyzerTests(unittest.TestCase):
    def test_clusters_case_drift_for_same_placeholder_and_original(self):
        candidates = cluster_case_drift_incidents(
            [
                _entry("2026-06-23T07:00:00+00:00", "кассира-повара"),
                _entry("2026-06-23T08:00:00+00:00", "кассиром-поваром"),
            ]
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].candidate_type, "case_drift")
        self.assertIn("кассир-повар", candidates[0].pattern_summary)

    def test_clusters_unknown_governing_phrase_after_threshold(self):
        entries = [_entry(f"2026-06-23T0{i}:00:00+00:00", "кассира-повара") for i in range(3)]

        candidates = cluster_unknown_governing_phrases(
            entries,
            known_phrases={"на должность"},
            min_occurrences=3,
        )

        self.assertEqual(len(candidates), 1)
        self.assertIn("принять на", candidates[0].pattern_summary.lower())


    def test_clusters_duplication_incidents_after_threshold(self):
        entries = [
            {
                "ts": f"2026-06-23T0{i}:00:00+00:00",
                "verification": {
                    "deterministic_issues": [
                        {
                            "issue_type": "duplication",
                            "detail": "департамента кадровой политики repeated",
                        }
                    ]
                },
            }
            for i in range(3)
        ]

        candidates = cluster_duplication_incidents(entries)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].candidate_type, "duplication_pattern")


    def test_draft_review_queue_recommendations_skips_without_claude_config(self):
        with patch.dict(os.environ, {}, clear=True):
            result = draft_review_queue_recommendations([
                RuleCandidate(
                    candidate_type="case_drift",
                    pattern_summary="unstable position correction",
                    occurrence_count=3,
                    example_contexts=["context"],
                    suggested_action="review",
                    created_at="2026-06-24T00:00:00+00:00",
                )
            ])

        self.assertIn("skipped", result)

    def test_analysis_pass_writes_candidates_and_skips_unstable_rag_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            log_path = tmp_path / "corrections.jsonl"
            state_path = tmp_path / "state.json"
            queue_path = tmp_path / "review.jsonl"
            rag_store = RagStore(path=str(tmp_path / "rag"), force_jsonl=True)
            entries = [
                _entry("2026-06-23T07:00:00+00:00", "кассира-повара"),
                _entry("2026-06-23T08:00:00+00:00", "кассиром-поваром"),
            ]
            log_path.write_text(
                "\n".join(json.dumps(entry, ensure_ascii=False) for entry in entries) + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"ADMIN_REVIEW_DB_PATH": str(tmp_path / "admin.sqlite3")}, clear=True):
                report = run_analysis_pass(
                    known_governing_phrases={"на должность"},
                    log_path=str(log_path),
                    state_path=str(state_path),
                    review_queue_path=str(queue_path),
                    rag_store=rag_store,
                )
                candidates = load_rule_candidates()

            self.assertEqual(report["processed"], 2)
            self.assertGreaterEqual(report["candidates"], 1)
            self.assertEqual(report["persisted_candidates"], report["candidates"])
            self.assertEqual(len(candidates), report["candidates"])
            self.assertEqual(report["rag_added"], 0)
            self.assertEqual(rag_store.count(), 0)
            self.assertTrue(queue_path.exists())
            self.assertTrue(state_path.exists())


if __name__ == "__main__":
    unittest.main()
