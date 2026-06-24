from __future__ import annotations

import json
import unittest

from word_constructor.ai_correction.openai_client import (
    _build_payload,
    _parse_corrected_placeholders,
)


class AiOpenAiClientPayloadTests(unittest.TestCase):
    def test_openai_payload_uses_template_placeholders_and_additional_instructions(self) -> None:
        payload = _build_payload(
            "Принять [ФИО] на [Должность].",
            {"ФИО": "Иванов Иван Иванович", "Должность": "hr бизнес-партнер"},
            {},
            "HR must stay uppercase",
        )

        self.assertEqual(payload["response_format"]["type"], "json_schema")
        schema = payload["response_format"]["json_schema"]
        self.assertTrue(schema["strict"])
        self.assertEqual(schema["schema"]["required"], ["ФИО", "Должность", "_review_needed"])
        self.assertFalse(schema["schema"]["additionalProperties"])
        self.assertEqual(payload["temperature"], 0)
        user_payload = json.loads(payload["messages"][1]["content"])
        self.assertEqual(
            user_payload,
            {
                "template": "Принять [ФИО] на [Должность].",
                "placeholders": {
                    "ФИО": "Иванов Иван Иванович",
                    "Должность": "hr бизнес-партнер",
                },
                "case_hints": [],
                "additional_instructions": "HR must stay uppercase",
            },
        )

    def test_corrected_placeholder_response_requires_exact_keys(self) -> None:
        corrected, review_needed = _parse_corrected_placeholders(
            {
                "ФИО": "Иванова Ивана Ивановича",
                "Должность": "HR бизнес-партнер",
                "_review_needed": True,
            },
            {"ФИО", "Должность"},
        )

        self.assertEqual(
            corrected,
            {
                "ФИО": "Иванова Ивана Ивановича",
                "Должность": "HR бизнес-партнер",
            },
        )
        self.assertTrue(review_needed)

    def test_corrected_placeholder_response_rejects_missing_or_extra_keys(self) -> None:
        with self.assertRaisesRegex(ValueError, "key mismatch"):
            _parse_corrected_placeholders({"ФИО": "Иванов"}, {"ФИО", "Должность"})


if __name__ == "__main__":
    unittest.main()
