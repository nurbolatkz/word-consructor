from __future__ import annotations

import unittest

from word_constructor.ai_correction.openai_client import _apply_verification_fallbacks
from word_constructor.ai_correction.verifier import (
    PlaceholderContext,
    check_duplication_in_rendered_text,
    check_fabrication,
    check_label_case_drift,
    run_deterministic_verification,
)


class AiVerifierTests(unittest.TestCase):
    def test_fabrication_detects_full_name_to_initials_in_label(self) -> None:
        issue = check_fabrication(
            PlaceholderContext(
                key="РеквизитыРуководительФИО",
                original="Есжанова Зарина Серикалиевна",
                corrected="Есжанова З.С.",
                context_type="label",
            )
        )

        self.assertIsNotNone(issue)
        self.assertEqual(issue.issue_type, "fabrication")

    def test_label_case_drift_detects_instrumental_declension(self) -> None:
        issue = check_label_case_drift(
            PlaceholderContext(
                key="РеквизитыРуководительФИО",
                original="Есжанова Зарина Серикалиевна",
                corrected="Есжановой Зариной Серикалиевной",
                context_type="label",
            )
        )

        self.assertIsNotNone(issue)
        self.assertEqual(issue.issue_type, "wrong_case_in_label")

    def test_duplication_detects_adjacent_repeated_phrase(self) -> None:
        issues = check_duplication_in_rendered_text(
            "...главному менеджеру [Должность] [Подразделение]...",
            {
                "Должность": "департамента кадровой политики",
                "Подразделение": "департамента кадровой политики",
            },
        )

        self.assertTrue(any(issue.issue_type == "duplication" for issue in issues))

    def test_fallback_restores_original_for_placeholder_specific_issue(self) -> None:
        verification = run_deterministic_verification(
            "[РеквизитыРуководительФИО]",
            [
                PlaceholderContext(
                    key="РеквизитыРуководительФИО",
                    original="Есжанова Зарина Серикалиевна",
                    corrected="Есжанова З.С.",
                    context_type="label",
                )
            ],
        )

        safe = _apply_verification_fallbacks(
            {"РеквизитыРуководительФИО": "Есжанова З.С."},
            {"РеквизитыРуководительФИО": "Есжанова Зарина Серикалиевна"},
            verification,
        )

        self.assertTrue(verification["needs_review"])
        self.assertEqual(safe["РеквизитыРуководительФИО"], "Есжанова Зарина Серикалиевна")


if __name__ == "__main__":
    unittest.main()
