from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from word_constructor.ai_correction.claude_checker import CheckResult, _strip_json_fence, claude_available


class ClaudeCheckerTests(unittest.TestCase):
    def test_strip_json_fence_accepts_plain_and_fenced_json(self) -> None:
        self.assertEqual(_strip_json_fence('{"ok": true}'), '{"ok": true}')
        self.assertEqual(_strip_json_fence('```json\n{"ok": true}\n```'), '{"ok": true}')

    def test_check_result_needs_review_is_any_issue(self) -> None:
        result = CheckResult(
            has_duplication=False,
            has_duplication_detail="",
            has_fabricated_content=True,
            has_fabricated_content_detail="initials invented",
            has_wrong_case_in_label=False,
            has_wrong_case_in_label_detail="",
            has_other_grammar_issue=False,
            has_other_grammar_issue_detail="",
        )

        self.assertTrue(result.needs_review)
        self.assertTrue(result.asdict()["needs_review"])

    def test_claude_available_requires_api_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(claude_available())


if __name__ == "__main__":
    unittest.main()
