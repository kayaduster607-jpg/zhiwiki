"""使用标准库验收 CLI，Python 3.9 无应用依赖环境也能执行。"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zhiwiki.cli import main
from zhiwiki.config import Settings
from zhiwiki.engine import AgentEngine
from zhiwiki.evaluation import evaluate
from zhiwiki.store import KnowledgeStore


class CliCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def command(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            status = main(["--data-dir", self.temp.name] + list(args))
        return status, json.loads(output.getvalue()) if output.getvalue() else None

    def test_seed_query_and_wiki_lifecycle(self):
        code, seed = self.command("seed")
        self.assertEqual(code, 0)
        self.assertEqual(len(seed["documents"]), 16)
        self.assertEqual(len(seed["pages"]), 4)
        self.assertTrue(all(page["mode"] == "sample" for page in seed["pages"]))
        code, result = self.command("ask", "阅读灯保修期是多少个月？")
        self.assertEqual(code, 0)
        self.assertEqual(result["mode"], "offline")
        self.assertEqual(result["usage"]["model_calls"], 0)
        selected = seed["documents"][0]["id"]
        code, draft = self.command("wiki", "draft", "cli-policy", "售后规则", selected)
        self.assertEqual(code, 0)
        self.assertEqual(draft["status"], "draft")
        code, published = self.command("wiki", "publish", draft["id"])
        self.assertEqual(code, 0)
        self.assertEqual(published["status"], "published")

    def test_seed_is_explicit_and_idempotent_in_cli(self):
        _, empty = self.command("documents")
        self.assertEqual(empty, [])
        _, first = self.command("seed")
        _, second = self.command("seed")
        self.assertEqual(len(first["created_pages"]), 4)
        self.assertEqual(second["created_pages"], [])
        self.assertEqual(len(second["pages"]), 4)

    def test_evaluation_is_isolated_and_excludes_published_sample_wiki(self):
        # 真实离线评测保留私有库，并在每个执行入口检查临时库只含原文。
        private_store = KnowledgeStore(self.temp.name)
        private = private_store.ingest("用户资料", "私有测试记录", "user:private")
        observed_calls = []

        def isolated_engine(store, settings):
            self.assertNotEqual(Path(settings.data_dir).resolve(), Path(self.temp.name).resolve())
            self.assertEqual(len(store.list_documents()), 16)
            self.assertEqual(store.list_wiki(), [])
            observed_calls.append(settings.data_dir)
            return AgentEngine(store, settings)

        with patch("zhiwiki.evaluation.AgentEngine", side_effect=isolated_engine):
            report = evaluate(Settings(data_dir=self.temp.name))
        self.assertEqual(report["dataset"], "24 fictional cases; 48 paired runs")
        self.assertEqual(len(report["cases"]), 48)
        self.assertEqual(len(observed_calls), 48)
        self.assertEqual(private_store.list_documents()[0]["id"], private["id"])
        self.assertEqual(len(private_store.list_documents()), 1)
        self.assertEqual(report["summary"]["baseline"]["cases"], 24)
        self.assertEqual(report["summary"]["agentic"]["model_calls"], 0)

    def test_missing_file_returns_nonzero(self):
        code, _ = self.command("ingest", self.temp.name + "/missing.md")
        self.assertEqual(code, 2)

    def test_configuration_rejects_environment_file_before_reading(self):
        code, _ = self.command("--config", self.temp.name + "/.ENV.local", "init")
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
