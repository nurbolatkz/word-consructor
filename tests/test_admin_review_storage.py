from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from app import create_app
from word_constructor.admin_storage import (
    insert_rule_candidate,
    load_approved_rules_log,
    load_review_items,
    load_rule_candidates,
)
from word_constructor.admin_views import build_review_item_from_check, insert_review_item


class AdminReviewStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"ADMIN_REVIEW_DB_PATH": f"{self.tmp.name}/admin.sqlite3"})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_review_routes_use_db_backed_status_update(self) -> None:
        item = build_review_item_from_check(
            document_name="order.docx",
            log_key="log-1",
            checker_result={"has_fabricated_content": True},
            corrections=[{"placeholder": "ФИО", "original": "Есжанова Зарина Серикалиевна", "final": "Есжанова З.С.", "context": "[ФИО]"}],
            rendered_preview="Есжанова З.С.",
        )
        insert_review_item(item)

        app = create_app()
        client = app.test_client()
        page = client.get("/admin/review")
        self.assertEqual(page.status_code, 200)
        self.assertIn("order.docx", page.get_data(as_text=True))

        resp = client.post(f"/admin/review/{item['id']}/decide", json={"status": "approved"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})
        self.assertEqual(load_review_items()[0]["status"], "approved")

    def test_rule_candidate_approval_only_writes_audit_log(self) -> None:
        candidate = insert_rule_candidate(
            {
                "candidate_type": "governing_phrase",
                "pattern_summary": 'Governing phrase "на период" not in GOVERNING_RULES table',
                "occurrence_count": 3,
                "example_contexts": ["на период [Дата]"],
            }
        )

        app = create_app()
        client = app.test_client()
        page = client.get("/admin/rule-candidates")
        self.assertEqual(page.status_code, 200)
        self.assertIn("на период", page.get_data(as_text=True))

        resp = client.post(f"/admin/rule-candidates/{candidate['id']}/decide", json={"status": "approved"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "ok"})
        self.assertEqual(load_rule_candidates()[0]["status"], "approved")
        log = load_approved_rules_log()
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["candidate_id"], candidate["id"])


if __name__ == "__main__":
    unittest.main()
