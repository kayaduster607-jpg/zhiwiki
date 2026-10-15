"""HTTP 边界与真实装配验收，不访问外部模型。"""

import tempfile
import unittest

from fastapi.testclient import TestClient

from zhiwiki.api import create_app
from zhiwiki.config import Settings


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.client = TestClient(create_app(Settings(data_dir=self.temp.name)))

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def test_offline_end_to_end_and_persistent_run(self):
        seeded = self.client.post("/api/examples").json()
        self.assertEqual(len(seeded["documents"]), 16)
        self.assertEqual(len(seeded["pages"]), 4)
        self.assertEqual(len(seeded["created_pages"]), 4)
        response = self.client.post("/api/query", json={"question": "阅读灯保修期多少个月？"})
        self.assertEqual(response.status_code, 200)
        run = response.json()
        self.assertEqual(run["mode"], "offline")
        self.assertGreater(len(run["citations"]), 0)
        self.assertEqual(run["usage"]["model_calls"], 0)
        stored = self.client.get("/api/runs/" + run["id"]).json()
        self.assertEqual(stored["answer"], run["answer"])

    def test_catalog_is_fixed_public_metadata_without_loading_data(self):
        response = self.client.get("/api/examples/catalog")
        self.assertEqual(response.status_code, 200)
        catalog = response.json()
        self.assertEqual(catalog["document_count"], 16)
        self.assertTrue(catalog["fictional"])
        for item in catalog["documents"]:
            self.assertEqual(set(item), {"file", "source_key", "title", "category", "updated_date", "fictional", "summary"})
            self.assertTrue(item["file"].startswith("documents/"))
            self.assertNotIn("C:", item["file"])
        self.assertEqual(self.client.get("/api/documents").json()["documents"], [])
        self.assertEqual(self.client.get("/api/wiki").json()["pages"], [])

    def test_repeated_example_loading_keeps_published_page_ids(self):
        first = self.client.post("/api/examples").json()
        second = self.client.post("/api/examples").json()
        self.assertEqual(second["created_pages"], [])
        self.assertEqual({item["id"] for item in first["pages"]}, {item["id"] for item in second["pages"]})
        self.assertTrue(all(item["unchanged"] for item in second["documents"]))
        self.assertEqual(len(second["skipped_pages"]), 4)

    def test_wiki_draft_publish_update_and_lint(self):
        original = {"title": "运费", "source_key": "shipping", "text": "标准运费为12元，满199元免运费。"}
        doc = self.client.post("/api/documents", json=original).json()
        draft = self.client.post("/api/wiki/draft", json={"slug": "shipping", "title": "运费知识", "document_ids": [doc["id"]]})
        self.assertEqual(draft.status_code, 201, draft.text)
        wiki_id = draft.json()["id"]
        self.assertEqual(self.client.post("/api/wiki/" + wiki_id + "/publish").status_code, 200)
        updated = dict(original, text="标准运费调整为15元，满199元免运费。")
        self.assertEqual(self.client.post("/api/documents", json=updated).status_code, 201)
        self.assertTrue(self.client.get("/api/wiki/" + wiki_id).json()["stale"])
        self.assertGreater(len(self.client.get("/api/wiki/lint").json()["issues"]), 0)
        self.assertIn("12元", self.client.get("/api/wiki/" + wiki_id + "/export").text)

    def test_stream_has_final_event_and_real_mode_label(self):
        self.client.post("/api/examples")
        response = self.client.post("/api/query/stream", json={"question": "保修"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: final", response.text)
        self.assertIn('"mode": "offline"', response.text)
        self.assertEqual(response.text.count("event: final"), 1)

    def test_upload_and_disallowed_file(self):
        good = self.client.post("/api/documents/upload", files={"file": ("policy.md", "退货期限7天".encode(), "text/markdown")})
        self.assertEqual(good.status_code, 201)
        bad = self.client.post("/api/documents/upload", files={"file": ("report.pdf", b"%PDF", "application/pdf")})
        self.assertEqual(bad.status_code, 400)

    def test_empty_query_and_unknown_fields(self):
        self.assertEqual(self.client.post("/api/query", json={"question": "   "}).status_code, 422)
        self.assertEqual(self.client.post("/api/query", json={"question": "退货", "secret": "x"}).status_code, 422)

    def test_missing_resource_and_oversized_body(self):
        self.assertEqual(self.client.get("/api/evidence/missing").status_code, 404)
        response = self.client.post("/api/documents", content=b"x" * (3 * 1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413)

    def test_external_origin_and_dns_rebinding_rejected(self):
        self.assertEqual(self.client.post("/api/examples", headers={"Origin": "https://untrusted.example"}).status_code, 403)
        self.assertEqual(self.client.get("/api/health", headers={"Host": "untrusted.example"}).status_code, 400)

    def test_console_and_openapi_have_no_cdn_dependency(self):
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)
        self.assertIn("/assets/app.js", response.text)
        self.assertNotIn("https://", response.text)
        self.assertEqual(self.client.get("/assets/app.js").status_code, 200)
        self.assertIn("/api/wiki/draft", self.client.get("/openapi.json").json()["paths"])


if __name__ == "__main__":
    unittest.main()
