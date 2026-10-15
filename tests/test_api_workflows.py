"""Exercise explicit file identity and manual revisions through the real API."""
import tempfile
import unittest
from fastapi.testclient import TestClient

from zhiwiki.api import create_app
from zhiwiki.config import Settings


class WorkflowApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(Settings(data_dir=self.temp.name))
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def upload(self, text, **parameters):
        return self.client.post("/api/documents/upload", params=parameters,
                                files={"file": ("policy.md", text.encode("utf-8"), "text/markdown")})

    def test_same_filename_creates_two_independent_current_sources(self):
        first = self.upload("退货窗口七天", title="甲部门政策").json()
        second = self.upload("另一个部门的规则", title="乙部门政策").json()
        self.assertEqual((first["title"], second["title"]), ("甲部门政策", "乙部门政策"))
        self.assertNotEqual(first["source_key"], second["source_key"])
        self.assertEqual((first["version"], second["version"]), (1, 1))
        self.assertEqual(len(self.client.get("/api/documents").json()["documents"]), 2)

    def test_explicit_update_invalidates_wiki_and_rejects_stale_editor(self):
        first = self.upload("退货窗口七天", title="保留原题").json()
        page = self.app.state.store.create_wiki("policy", "政策", [
            {"text": "退货窗口七天", "evidence_ids": first["chunk_ids"]}])
        self.app.state.store.publish_wiki(page["id"])
        parameters = {"mode": "update", "target_document_id": first["id"], "expected_version": 1}
        updated = self.upload("退货窗口十天", **parameters)
        self.assertEqual(updated.status_code, 201, updated.text)
        self.assertEqual(updated.json()["source_key"], first["source_key"])
        self.assertEqual(updated.json()["version"], 2)
        self.assertEqual(updated.json()["title"], "保留原题")
        self.assertTrue(self.app.state.store.get_wiki(page["id"])["stale"])
        conflict = self.upload("过期页面的错误覆盖", **parameters)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(len(self.app.state.store.list_documents(include_history=True)), 2)

    def test_ambiguous_upload_parameters_cannot_mutate_documents(self):
        self.assertEqual(self.upload("正文", mode="update").status_code, 400)
        self.assertEqual(self.upload("正文", source_key="policy.md").status_code, 400)
        self.assertEqual(self.upload("正文", mode="new", target_document_id="old").status_code, 400)
        self.assertEqual(self.app.state.store.list_documents(), [])

    def test_revision_is_a_new_draft_until_explicitly_published(self):
        doc = self.upload("配送费用十二元，满二百元免运费。").json()
        page = self.app.state.store.create_wiki("shipping", "配送", [
            {"text": "配送费用十二元", "evidence_ids": doc["chunk_ids"]}])
        self.app.state.store.publish_wiki(page["id"])
        payload = {"expected_version": 1, "title": "配送与免邮条件", "claims": [
            {"text": "配送费用十二元，满二百元免运费。", "evidence_ids": doc["chunk_ids"]}],
            "revision_note": "补充免邮条件"}
        response = self.client.post("/api/wiki/" + page["id"] + "/revisions", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        revision = response.json()
        self.assertEqual((revision["mode"], revision["status"], revision["version"]), ("manual", "draft", 2))
        self.assertEqual(self.app.state.store.get_wiki(page["id"])["status"], "published")
        self.assertEqual(self.client.post("/api/wiki/" + page["id"] + "/revisions", json=payload).status_code, 409)
        self.assertEqual(self.client.post("/api/wiki/" + revision["id"] + "/publish").status_code, 200)
        self.assertEqual(self.app.state.store.get_wiki(page["id"])["status"], "archived")

    def test_retrieval_diagnostics_has_no_source_text(self):
        self.upload("不应出现在统计里的特殊正文")
        self.app.state.store.search("特殊正文")
        result = self.client.get("/api/system/retrieval")
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("特殊正文", result.text)
        self.assertTrue(result.json()["ready"])


if __name__ == "__main__":
    unittest.main()
