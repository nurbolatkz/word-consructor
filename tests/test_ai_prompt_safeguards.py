from __future__ import annotations

from io import StringIO
import unittest
from contextlib import redirect_stdout

from scripts.ai_correction_stability import print_stability_report
from word_constructor.ai_correction.openai_client import SYSTEM_PROMPT


class AiPromptSafeguardTests(unittest.TestCase):
    def test_prompt_forbids_signature_fio_declension_and_abbreviation(self) -> None:
        self.assertIn("STEP 2.5", SYSTEM_PROMPT)
        self.assertIn("NEVER decline ФИО", SYSTEM_PROMPT)
        self.assertIn("NEVER abbreviate", SYSTEM_PROMPT)
        self.assertIn("Есжанова Зарина Серикалиевна", SYSTEM_PROMPT)
        self.assertIn("Есжанова З.С.", SYSTEM_PROMPT)
        self.assertIn("case is ambiguous", SYSTEM_PROMPT)

    def test_stability_report_marks_unstable_values(self) -> None:
        report = {
            "ФИО": {
                "distinct_values": 2,
                "stable": False,
                "value_counts": {"Есжанова З.С.": 1, "Есжанова Зарина Серикалиевна": 1},
            }
        }
        output = StringIO()
        with redirect_stdout(output):
            print_stability_report(report)

        text = output.getvalue()
        self.assertIn("UNSTABLE", text)
        self.assertIn("Есжанова З.С.", text)


if __name__ == "__main__":
    unittest.main()
