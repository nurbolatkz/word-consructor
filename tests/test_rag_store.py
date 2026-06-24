from __future__ import annotations

import tempfile
import unittest

from word_constructor.ai_correction.rag_store import RagStore, make_entry


class RagStoreTests(unittest.TestCase):
    def test_jsonl_backend_adds_and_filters_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RagStore(path=tmp, force_jsonl=True)
            store.add_entry(
                make_entry(
                    kind="known_pitfall",
                    placeholder_role="person_name",
                    context_type="label",
                    governing_phrase="",
                    original_value="Есжанова Зарина Серикалиевна",
                    corrected_value="Есжанова З.С.",
                    case="без_изменений",
                    note="Do not abbreviate signature names.",
                    source="test",
                )
            )
            store.add_entry(
                make_entry(
                    kind="good_example",
                    placeholder_role="position",
                    context_type="sentence",
                    governing_phrase="на должность",
                    original_value="кассир-повар",
                    corrected_value="кассира-повара",
                    case="родительный",
                    note="Position after на должность.",
                    source="test",
                )
            )

            self.assertEqual(store.count(), 2)
            results = store.query(
                placeholder_role="position",
                context_type="sentence",
                governing_phrase="на должность",
                original_value="главный менеджер",
                kind_filter="good_example",
            )

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["corrected_value"], "кассира-повара")


if __name__ == "__main__":
    unittest.main()
