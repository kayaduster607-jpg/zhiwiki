"""分块预览 API 的原文定位、结构信息与无副作用边界。"""

import tempfile
import unittest
from unittest.mock import Mock

from fastapi.testclient import TestClient

from zhiwiki.api import create_app
from zhiwiki.config import Settings


class ChunkPreviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.provider_factory = Mock(side_effect=AssertionError("preview must not create a model provider"))
        self.app = create_app(Settings(data_dir=self.directory.name), provider_factory=self.provider_factory)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_default_preview_keeps_exact_original_offsets_and_no_side_effects(self):
        text = "  # 售后说明\n\n" + "质量问题需要核对来源。\n" * 120 + "\n最后保留空白。  "
        response = self.client.post("/api/chunk-preview", json={"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["source_length"], len(text))
        self.assertEqual((result["size"], result["overlap"], result["strategy"]), (900, 120, "fixed"))
        self.assertEqual(result["chunk_count"], len(result["chunks"]))
        self.assertGreater(result["chunk_count"], 1)
        for chunk in result["chunks"]:
            self.assertEqual(text[chunk["start_offset"]:chunk["end_offset"]], chunk["text"])
        self.assertEqual(result["duplicate_chars"], sum(len(chunk["text"]) for chunk in result["chunks"]) - len(text))
        self.assertEqual(self.app.state.store.list_documents(), [])
        self.assertEqual(self.app.state.store.list_wiki(), [])
        self.provider_factory.assert_not_called()

    def test_structure_preview_returns_heading_paths_and_section_offsets(self):
        text = "# 产品说明\n\n开场说明。\n\n## 保修\n\n" + "正常使用电路故障需保留检查记录。\n" * 30 + "\n## 运费\n\n标准配送由规则决定。"
        response = self.client.post("/api/chunk-preview", json={"text": text, "size": 240, "overlap": 40, "strategy": "structure"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["strategy"], "structure")
        paths = []
        for chunk in result["chunks"]:
            self.assertEqual(chunk["strategy"], "structure")
            self.assertEqual(text[chunk["start_offset"]:chunk["end_offset"]], chunk["text"])
            self.assertLessEqual(len(chunk["text"]), 240)
            self.assertGreaterEqual(chunk["start_offset"], chunk["section_start"])
            self.assertLessEqual(chunk["end_offset"], chunk["section_end"])
            self.assertIn(chunk["boundary"], {"section_end", "paragraph", "sentence", "fence_start", "hard_fence", "hard_limit"})
            self.assertIn("overlap_chars", chunk)
            paths.append(chunk["heading_path"])
        self.assertIn(["产品说明", "保修"], paths)
        self.assertIn(["产品说明", "运费"], paths)

    def test_preview_rejects_out_of_range_unknown_and_blank_inputs(self):
        invalid = [
            {"text": " "}, {"text": "a" * 20001}, {"text": "a\x00b"},
            {"text": "内容", "size": 199}, {"text": "内容", "size": 2001},
            {"text": "内容", "overlap": -1}, {"text": "内容", "overlap": 501},
            {"text": "内容", "size": 200, "overlap": 101},
            {"text": "内容", "strategy": "semantic"}, {"text": "内容", "path": "notes.md"},
            {"text": "内容", "size": True}, {"text": "内容", "overlap": "20"},
        ]
        for payload in invalid:
            with self.subTest(payload={key: value for key, value in payload.items() if key != "text"}):
                self.assertEqual(self.client.post("/api/chunk-preview", json=payload).status_code, 422)
        self.assertEqual(self.app.state.store.list_documents(), [])
        self.provider_factory.assert_not_called()

    def test_preview_accepts_declared_boundary_values(self):
        for parameters in ({"size": 200, "overlap": 100}, {"size": 2000, "overlap": 500}):
            with self.subTest(parameters=parameters):
                response = self.client.post("/api/chunk-preview", json=dict(text="文" * 20000, **parameters))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["source_length"], 20000)


if __name__ == "__main__":
    unittest.main()
