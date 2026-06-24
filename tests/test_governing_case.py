from __future__ import annotations

import unittest

from docx import Document

from word_constructor.ai_correction.extraction import extract_placeholder_occurrences
from word_constructor.ai_correction.governing import Case, detect_case_for_placeholder


class GoverningCaseTests(unittest.TestCase):
    def test_real_template_governing_phrases(self) -> None:
        cases = [
            ("", "принять с 15.12.2025 на должность", Case.ACCUSATIVE),
            ("на должность", "сектора", Case.GENITIVE),
            ("сектора", "департамента", Case.GENITIVE),
            ("заявление", "трудовой договор от", Case.GENITIVE),
            ("", "предоставить ежегодный трудовой отпуск", Case.DATIVE),
            ("", "", Case.NO_CHANGE),
            ("Генеральный директор / И.о. генерального директора", "", Case.NO_CHANGE),
        ]
        for text_before, text_after, expected in cases:
            with self.subTest(text_before=text_before, text_after=text_after):
                detected, _note = detect_case_for_placeholder(text_before, text_after)
                self.assertEqual(detected, expected)


    def test_extracted_occurrences_include_detected_case_hints(self) -> None:
        doc = Document()
        doc.add_paragraph("Принять [ФИО] на должность [Должность].")

        occurrences = extract_placeholder_occurrences(
            doc,
            {"ФИО": "Садық Ермек Жәнібекұлы", "Должность": "кассир-повар"},
        )
        by_key = {item["placeholder"]: item for item in occurrences}

        self.assertEqual(by_key["ФИО"]["detected_case"], Case.ACCUSATIVE.value)
        self.assertEqual(by_key["Должность"]["detected_case"], Case.GENITIVE.value)
        self.assertIn("Принять", by_key["ФИО"]["text_before"])
        self.assertIn("на должность", by_key["Должность"]["text_before"])

    def test_unknown_non_label_context_falls_back_to_no_change(self) -> None:
        detected, note = detect_case_for_placeholder("в связи с", "")

        self.assertEqual(detected, Case.NO_CHANGE)
        self.assertEqual(note, "no_rule_matched_safe_default")


if __name__ == "__main__":
    unittest.main()
